"""外部で運用される GPU 文書解析エンジンの native API クライアント。"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import math
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from itertools import batched
from typing import Any, Literal, cast
from urllib.parse import urljoin

import httpx
from pr_backend_core.internal_http import http_client_options
from rag_parser_core.extraction import ExtractionMetadataValue, ExtractionPage
from rag_parser_core.result import ParserRegistryResult

from app.clients.http_retry import request_with_retry, retry_config_from_settings
from app.config import Settings
from app.rag.mineru_license import (
    MINERU_BACKEND,
    MINERU_LICENSE_UNACKNOWLEDGED_REASON,
    MINERU_LICENSE_UNACKNOWLEDGED_WARNING,
    mineru_license_acknowledged,
)
from app.rag.pdf_backend import PdfSource, SourceOpenError, png_bytes
from app.schemas.document import SourceModality, SourceProfile

logger = logging.getLogger(__name__)

ExternalParserBackend = Literal["mineru", "dots_ocr"]
ExternalParserProtocol = Literal["mineru_v1", "openai_chat_completions"]
# MinerU の V1 の parse job（#1329）。ポーリングの待ちと時計はテストで差し替える（待たない）。
MINERU_TERMINAL_JOB_STATUSES = frozenset({"completed", "partial", "failed", "canceled"})
MINERU_POLL_INTERVAL_SECONDS = 2.0
MINERU_MAX_DOWNLOAD_REDIRECTS = 3
_poll_sleep: Callable[[float], None] = time.sleep
_poll_clock: Callable[[], float] = time.monotonic
ExternalParserStatusValue = Literal[
    "available", "unconfigured", "unreachable", "model_missing", "invalid_response"
]


@dataclass(frozen=True)
class ExternalParserEngineSpec:
    backend: ExternalParserBackend
    protocol: ExternalParserProtocol
    capabilities: tuple[str, ...]
    endpoint_field: str
    model_field: str | None
    api_key_field: str
    call: Callable[..., ExternalParserOutput]
    convert: Callable[..., ParserRegistryResult]


@dataclass(frozen=True)
class ExternalParserOutput:
    """解析エンジンの出力（共通の抽出の変換に渡す block の列と頁）。

    ``layout_records`` は親子階層（small-to-big）が読む LayoutRecord の容れ物
    （``parser_artifacts["layout_records"]``。Docling と同じ形）。作れないエンジン・文書は None。
    """

    rendered: object
    pages: list[ExtractionPage]
    layout_records: dict[str, Any] | None = None


ENGINE_SPECS: dict[ExternalParserBackend, ExternalParserEngineSpec]
EXTERNAL_PARSER_BACKENDS: tuple[ExternalParserBackend, ...]


@dataclass(frozen=True)
class ExternalParserConnection:
    backend: ExternalParserBackend
    protocol: ExternalParserProtocol
    endpoint: str
    model: str | None
    api_key: str

    @property
    def configured(self) -> bool:
        model_configured = self.model if self.protocol == "openai_chat_completions" else True
        return bool(self.endpoint and model_configured)


@dataclass(frozen=True)
class ExternalParserStatus:
    backend: ExternalParserBackend
    status: ExternalParserStatusValue
    version: str | None = None
    warning_code: str | None = None


class ExternalParserCallError(RuntimeError):
    safe_for_user = True

    def __init__(
        self,
        reason: str,
        *,
        status_code: int | None = None,
        warning_code: str | None = None,
    ) -> None:
        self.reason = reason
        self.status_code = status_code
        self.warning_code = warning_code
        super().__init__(warning_code or reason)


@dataclass(frozen=True)
class _RenderedPage:
    number: int
    png: bytes
    width: int
    height: int
    mime_type: str = "image/png"


def external_parser_connection(
    settings: Settings, backend: ExternalParserBackend
) -> ExternalParserConnection:
    spec = ENGINE_SPECS[backend]
    model = str(getattr(settings, spec.model_field, "") or "").strip() if spec.model_field else None
    return ExternalParserConnection(
        backend=backend,
        protocol=spec.protocol,
        endpoint=str(getattr(settings, spec.endpoint_field, "") or "").strip().rstrip("/"),
        model=model,
        api_key=str(getattr(settings, spec.api_key_field, "") or "").strip(),
    )


class ExternalParserClient:
    """設定済み外部 parser を呼び、共通抽出 schema へ変換する。"""

    def __init__(self, settings: Settings, *, dots_ocr_prompt: str | None = None) -> None:
        self._dots_ocr_prompt = dots_ocr_prompt
        self._settings = settings
        self._timeout = float(settings.rag_parser_service_timeout_seconds)
        self._retry = retry_config_from_settings(settings)

    def status(self, backend: ExternalParserBackend) -> ExternalParserStatus:
        connection = external_parser_connection(self._settings, backend)
        if not connection.configured:
            return ExternalParserStatus(
                backend,
                "unconfigured",
                warning_code="external_parser_unconfigured",
            )
        try:
            if connection.protocol == "mineru_v1":
                payload = self._request_json(
                    "GET",
                    f"{connection.endpoint}/v1/health",
                    connection,
                    timeout=self._settings.rag_service_status_probe_timeout_seconds,
                )
                if not isinstance(payload, dict):
                    raise ExternalParserCallError(
                        "invalid_response",
                        warning_code=f"{backend}_external_invalid_response",
                    )
                version = _optional_text(payload.get("version"))
                return ExternalParserStatus(backend, "available", version=version)
            payload = self._request_json(
                "GET",
                f"{_openai_base(connection.endpoint)}/models",
                connection,
                timeout=self._settings.rag_service_status_probe_timeout_seconds,
            )
            if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                raise ExternalParserCallError(
                    "invalid_response",
                    warning_code=f"{backend}_external_invalid_response",
                )
            model_ids = {
                str(item.get("id"))
                for item in payload["data"]
                if isinstance(item, dict) and item.get("id")
            }
            if connection.model not in model_ids:
                return ExternalParserStatus(
                    backend, "model_missing", warning_code="external_parser_model_missing"
                )
            return ExternalParserStatus(backend, "available", version=connection.model)
        except ExternalParserCallError as exc:
            status: ExternalParserStatusValue = (
                "invalid_response" if exc.reason == "invalid_response" else "unreachable"
            )
            return ExternalParserStatus(backend, status, warning_code=exc.warning_code)

    def parse(
        self,
        backend: ExternalParserBackend,
        source_bytes: bytes,
        source_profile: SourceProfile | None,
        content_type: str,
    ) -> ParserRegistryResult:
        if backend == MINERU_BACKEND and not mineru_license_acknowledged(self._settings):
            # 管理者が MinerU のライセンスの条件を確認するまで MinerU を呼ばない（#3359）。
            raise ExternalParserCallError(
                MINERU_LICENSE_UNACKNOWLEDGED_REASON,
                warning_code=MINERU_LICENSE_UNACKNOWLEDGED_WARNING,
            )
        connection = external_parser_connection(self._settings, backend)
        if not connection.configured:
            raise ExternalParserCallError("unconfigured", warning_code=f"{backend}_unconfigured")
        try:
            spec = ENGINE_SPECS[backend]
            output = spec.call(self, source_bytes, source_profile, content_type, connection)
            result = spec.convert(
                output.rendered,
                backend=backend,
                source_profile=source_profile,
                parser_version=f"external:{connection.model or 'native'}",
                pages=output.pages,
                extra_artifacts={
                    "external_protocol": connection.protocol,
                    "external_model": connection.model,
                },
            )
            if result.extraction is None:
                raise ExternalParserCallError(
                    "adapter_empty_result", warning_code=f"{backend}_adapter_empty"
                )
            if output.layout_records is not None:
                result = _with_layout_records(result, output.layout_records)
            return result
        except ExternalParserCallError:
            raise
        except httpx.TimeoutException as exc:
            raise ExternalParserCallError(
                "timeout", warning_code=f"{backend}_external_timeout"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise ExternalParserCallError(
                "http_error",
                status_code=exc.response.status_code,
                warning_code=f"{backend}_external_http_error",
            ) from exc
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ExternalParserCallError(
                "invalid_response", warning_code=f"{backend}_external_invalid_response"
            ) from exc

    def _parse_mineru(
        self,
        source_bytes: bytes,
        source_profile: SourceProfile | None,
        content_type: str,
        connection: ExternalParserConnection,
    ) -> ExternalParserOutput:
        """MinerU 4.x の V1 API で解析する（#1329）。

        upload → complete → parse job → 終了状態までポーリング → Middle JSON の取得の順に呼ぶ。
        3.x までの ``/file_parse`` は 4.0 で削除されたため呼ばない。Middle JSON は
        content list と同じ形の扁平な block へ写し、共通の抽出の変換に渡す。親子階層
        （small-to-big）の分割のために、同じ Middle JSON から LayoutRecord も作る（#1334）。
        """
        name = source_profile.sanitized_file_name if source_profile else "upload"
        mime_type = content_type or "application/octet-stream"
        file_id = self._mineru_upload(source_bytes, name, mime_type, connection)
        tier = _mineru_tier(self._settings.rag_parser_mineru_tier, source_profile)
        job = self._request_json(
            "POST",
            f"{connection.endpoint}/v1/parse/jobs",
            connection,
            json={
                "files": [{"source": {"type": "file_id", "file_id": file_id}}],
                "tier": tier,
                "output_formats": ["middle_json"],
            },
        )
        job = self._mineru_wait_job(job, connection)
        middle_file_id = _mineru_output_file_id(job)
        middle_json = self._mineru_download_json(middle_file_id, connection)
        rendered = mineru_middle_json_blocks(middle_json)
        if not rendered:
            raise ValueError("mineru output is empty")
        # 循環 import を避けて使うときに読む（mineru_layout は本モジュールの helper を使う）。
        from app.clients.mineru_layout import mineru_layout_records

        layout_records = mineru_layout_records(
            middle_json,
            source_bytes=source_bytes,
            content_type=content_type,
            source_profile=source_profile,
        )
        return ExternalParserOutput(rendered, _pages_from_elements(rendered), layout_records)

    def _mineru_upload(
        self,
        source_bytes: bytes,
        name: str,
        mime_type: str,
        connection: ExternalParserConnection,
    ) -> str:
        """原本を upload し、File の id を返す。同じ内容が既知なら本体の送信を省く。"""
        sha256sum = hashlib.sha256(source_bytes).hexdigest()
        upload = _mapping(
            self._request_json(
                "POST",
                f"{connection.endpoint}/v1/uploads",
                connection,
                json={
                    "filename": name,
                    "bytes": len(source_bytes),
                    "mime_type": mime_type,
                    "sha256sum": sha256sum,
                },
            )
        )
        if upload.get("status") == "completed":
            return _required_text(_mapping(upload.get("file")).get("id"), "mineru file id")
        upload_id = _required_text(upload.get("id"), "mineru upload id")
        upload_url = urljoin(
            f"{connection.endpoint}/",
            _required_text(upload.get("upload_url"), "mineru upload url"),
        )
        headers = {
            str(key): str(value) for key, value in _mapping(upload.get("upload_headers")).items()
        }
        self._request(
            str(upload.get("upload_method") or "PUT"),
            upload_url,
            connection,
            # 別 origin の upload URL（署名付き URL）へ MinerU の API key を送らない。
            send_api_key=_same_origin(upload_url, connection.endpoint),
            headers=headers,
            content=source_bytes,
        )
        completed = _mapping(
            self._request_json(
                "POST",
                f"{connection.endpoint}/v1/uploads/{upload_id}/complete",
                connection,
                json={"sha256sum": sha256sum},
            )
        )
        return _required_text(_mapping(completed.get("file")).get("id"), "mineru file id")

    def _mineru_wait_job(
        self, job: object, connection: ExternalParserConnection
    ) -> Mapping[str, object]:
        """parse job を終了状態まで待つ。時間切れは job を取り消して timeout にする。"""
        current = _mapping(job)
        job_id = _required_text(current.get("job_id"), "mineru job id")
        deadline = _poll_clock() + float(self._settings.rag_parser_mineru_job_timeout_seconds)
        while str(current.get("status") or "") not in MINERU_TERMINAL_JOB_STATUSES:
            if _poll_clock() >= deadline:
                self._mineru_cancel_job(current, job_id, connection)
                raise ExternalParserCallError(
                    "timeout", warning_code=f"{connection.backend}_external_timeout"
                )
            _poll_sleep(MINERU_POLL_INTERVAL_SECONDS)
            current = _mapping(
                self._request_json(
                    "GET", f"{connection.endpoint}/v1/parse/jobs/{job_id}", connection
                )
            )
        status = str(current.get("status"))
        if status == "canceled":
            raise ExternalParserCallError(
                "job_canceled", warning_code=f"{connection.backend}_external_job_canceled"
            )
        if status == "failed":
            raise ExternalParserCallError(
                "job_failed", warning_code=f"{connection.backend}_external_job_failed"
            )
        return current

    def _mineru_cancel_job(
        self,
        job: Mapping[str, object],
        job_id: str,
        connection: ExternalParserConnection,
    ) -> None:
        cancel = _mapping(job.get("links")).get("cancel")
        url = urljoin(
            f"{connection.endpoint}/",
            str(cancel or f"/v1/parse/jobs/{job_id}/cancel"),
        )
        try:
            self._request(
                "POST", url, connection, send_api_key=_same_origin(url, connection.endpoint)
            )
        except ExternalParserCallError:
            # 取り消しは後始末。失敗しても時間切れの扱いは変えない。
            logger.warning(
                "mineru_job_cancel_failed",
                extra={"parser_backend": connection.backend, "job_id": job_id},
            )

    def _mineru_download_json(self, file_id: str, connection: ExternalParserConnection) -> object:
        """出力の File を取得する。302 の転送先が別 origin なら API key を付けない。"""
        url = f"{connection.endpoint}/v1/files/{file_id}/content"
        send_api_key = True
        for _ in range(MINERU_MAX_DOWNLOAD_REDIRECTS + 1):
            response = self._request(
                "GET", url, connection, send_api_key=send_api_key, redirect_response=True
            )
            if not response.is_redirect:
                return response.json()
            url = urljoin(url, response.headers.get("location", ""))
            send_api_key = _same_origin(url, connection.endpoint)
        raise ValueError("mineru download redirected too many times")

    def _parse_dots(
        self,
        source_bytes: bytes,
        source_profile: SourceProfile | None,
        content_type: str,
        connection: ExternalParserConnection,
    ) -> ExternalParserOutput:
        rendered_pages = self._source_images(
            source_bytes, source_profile, content_type, self._settings.rag_parser_dots_ocr_dpi
        )
        workers = self._settings.rag_parser_dots_ocr_pdf_workers

        def parse_page(page: _RenderedPage) -> list[dict[str, object]]:
            text = self._openai_chat(
                connection,
                [(page.png, page.mime_type)],
                _DOTS_IMAGE_TOKENS + (self._dots_ocr_prompt or DOTS_OCR_PROMPT),
                max_tokens=32768,
                max_tokens_key="max_completion_tokens",
                temperature=0.1,
                extra={"top_p": 0.9},
                text_after_images=True,
            )
            return _dots_bboxes_to_page_px(_dots_elements(text, page.number), page)

        elements: list[dict[str, object]] = []
        pages: list[ExtractionPage] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for page_batch in batched(rendered_pages, workers):
                pages.extend(_extraction_pages(page_batch))
                elements.extend(
                    element
                    for page_elements in pool.map(parse_page, page_batch)
                    for element in page_elements
                )
        if not elements:
            raise ValueError("dots output is empty")
        return ExternalParserOutput(elements, pages)

    def _source_images(
        self,
        source_bytes: bytes,
        source_profile: SourceProfile | None,
        content_type: str,
        dpi: int,
    ) -> Iterator[_RenderedPage]:
        is_pdf = content_type.split(";", 1)[0].strip().casefold() == "application/pdf" or (
            source_profile is not None and (source_profile.extension or "").casefold() == ".pdf"
        )
        if not is_pdf:
            mime_type = content_type.split(";", 1)[0].strip().casefold()
            if not mime_type.startswith("image/"):
                mime_type = "image/png"
            width, height = _image_size(source_bytes)
            yield _RenderedPage(1, source_bytes, width, height, mime_type)
            return
        try:
            source = PdfSource(source_bytes)
        except SourceOpenError as exc:
            raise ExternalParserCallError(
                "adapter_invalid_input", warning_code="external_parser_pdf_render_failed"
            ) from exc
        with source:
            if source.page_count < 1:
                raise ExternalParserCallError(
                    "adapter_invalid_input", warning_code="external_parser_pdf_empty"
                )
            for index in range(source.page_count):
                try:
                    # 描画は PDFium の lock の中、PNG の符号化は lock の外で行う。
                    image = source.render(index, dpi / 72)
                    png = png_bytes(image)
                except Exception as exc:
                    raise ExternalParserCallError(
                        "adapter_invalid_input", warning_code="external_parser_pdf_render_failed"
                    ) from exc
                yield _RenderedPage(
                    number=index + 1, png=png, width=image.width, height=image.height
                )

    def _openai_chat(
        self,
        connection: ExternalParserConnection,
        images: list[tuple[bytes, str]],
        prompt: str,
        *,
        max_tokens: int,
        max_tokens_key: str = "max_tokens",
        temperature: float = 0.0,
        extra: dict[str, object] | None = None,
        text_after_images: bool = False,
    ) -> str:
        content: list[dict[str, object]] = []
        text_part: dict[str, object] = {"type": "text", "text": prompt}
        if not text_after_images:
            content.append(text_part)
        content.extend(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
                },
            }
            for data, mime in images
        )
        if text_after_images:
            content.append(text_part)
        body: dict[str, object] = {
            "model": connection.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": temperature,
            max_tokens_key: max_tokens,
            **(extra or {}),
        }
        payload = self._request_json(
            "POST",
            f"{_openai_base(connection.endpoint)}/chat/completions",
            connection,
            json=body,
        )
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("openai choices is empty")
        if choices[0].get("finish_reason") == "length":
            raise ExternalParserCallError(
                "invalid_response", warning_code=f"{connection.backend}_external_truncated"
            )
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ValueError("openai message is invalid")
        return _message_text(message.get("content"))

    def _request(
        self,
        method: str,
        url: str,
        connection: ExternalParserConnection,
        *,
        timeout: float | None = None,
        send_api_key: bool = True,
        redirect_response: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        """外部 parser へ 1 回の request を送る（失敗は ExternalParserCallError にそろえる）。

        ``send_api_key=False`` は別 origin の URL（署名付きの upload / download の URL）用。
        ``redirect_response=True`` は 3xx を失敗にせず返し、転送先の扱いを呼び出し側に任せる。
        """
        headers = dict(kwargs.pop("headers", {}) or {})
        if send_api_key and connection.api_key:
            headers["Authorization"] = f"Bearer {connection.api_key}"
        try:
            with httpx.Client(
                timeout=timeout or self._timeout, **http_client_options(url)
            ) as client:
                response = request_with_retry(
                    client,
                    method,
                    url,
                    retry=self._retry,
                    logger=logger,
                    log_extra={
                        "parser_backend": connection.backend,
                        "service_url": connection.endpoint,
                    },
                    headers=headers,
                    **kwargs,
                )
                if not (redirect_response and response.is_redirect):
                    response.raise_for_status()
                return response
        except httpx.TimeoutException as exc:
            raise ExternalParserCallError(
                "timeout", warning_code=f"{connection.backend}_external_timeout"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise ExternalParserCallError(
                "http_error",
                status_code=exc.response.status_code,
                warning_code=f"{connection.backend}_external_http_error",
            ) from exc
        except httpx.HTTPError as exc:
            raise ExternalParserCallError(
                "unreachable", warning_code=f"{connection.backend}_external_unreachable"
            ) from exc

    def _request_json(
        self,
        method: str,
        url: str,
        connection: ExternalParserConnection,
        *,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> object:
        response = self._request(method, url, connection, timeout=timeout, **kwargs)
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            raise ExternalParserCallError(
                "invalid_response", warning_code=f"{connection.backend}_external_invalid_response"
            ) from exc


def _openai_base(endpoint: str) -> str:
    endpoint = endpoint.rstrip("/")
    return endpoint if endpoint.endswith("/v1") else f"{endpoint}/v1"


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _positive_int(value: object, *, offset: int = 0) -> int:
    if not isinstance(value, str | bytes | bytearray | int | float):
        return 1
    try:
        return max(1, int(value) + offset)
    except (TypeError, ValueError):
        return 1


def _mapping(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}


def _list(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def _required_text(value: object, label: str) -> str:
    text = str(value or "").strip() if isinstance(value, str | int) else ""
    if not text:
        raise ValueError(f"{label} is missing")
    return text


def _same_origin(url: str, base: str) -> bool:
    """scheme・host・実効の port が同じか（同じ host でも port が違えば別 origin）。"""
    target, origin = httpx.URL(url), httpx.URL(base)
    return (
        target.scheme == origin.scheme
        and target.host == origin.host
        and _effective_port(target) == _effective_port(origin)
    )


def _effective_port(url: httpx.URL) -> int | None:
    return url.port or {"http": 80, "https": 443}.get(url.scheme)


def _mineru_tier(configured: str, source_profile: SourceProfile | None) -> str:
    """PDF・画像は設定の tier。それ以外（Office など）は MinerU が flash しか持たない（#1329）。"""
    modality = source_profile.modality if source_profile is not None else None
    if modality in {SourceModality.PDF, SourceModality.IMAGE, None}:
        return configured
    return "flash"


def _mineru_output_file_id(job: Mapping[str, object]) -> str:
    files = job.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("mineru job has no files")
    result = _mapping(files[0])
    if result.get("status") != "completed":
        raise ExternalParserCallError("job_failed", warning_code="mineru_external_job_failed")
    output = _mapping(_mapping(result.get("output_files")).get("middle_json"))
    return _required_text(output.get("file_id"), "mineru middle_json file id")


# Middle JSON 2.0（docvortex.middle）の block を、共通の抽出の変換が読む扁平な block へ写す。
# 頁のヘッダー・フッター・頁番号は本文ではないため出さない
# （Docling の Page-header / Page-footer と同じ扱い）。
_MINERU_SKIPPED_BLOCK_TYPES = frozenset({"header", "footer", "page_number"})
_MINERU_TITLE_BLOCK_TYPES = frozenset({"doc_title", "paragraph_title"})
_MINERU_VISUAL_BLOCK_TYPES = frozenset({"image", "table", "chart", "code"})


def mineru_middle_json_blocks(middle_json: object) -> list[dict[str, object]]:
    """MinerU の Middle JSON 2.0 を、頁・bbox・表の HTML を持つ扁平な block の列にする。

    bbox は Middle JSON の 0〜1 を content list と同じ 0〜1000 の整数にする（MinerU の
    content list の換算と同じ。下流の座標系の扱い（docs/rag-engine.md の「座標系」）を変えない）。
    ``element_id`` は頁と頁内の block の番号で決め、同じ解析の結果なら同じ値になる。
    """
    document = _mapping(middle_json)
    if document.get("schema") != "docvortex.middle" or not str(
        document.get("schema_version") or ""
    ).startswith("2."):
        raise ValueError("mineru middle_json schema is unsupported")
    blocks: list[dict[str, object]] = []
    for page in _list(document.get("pages")):
        page_info = _mapping(page)
        page_idx = page_info.get("page_idx")
        if not isinstance(page_idx, int) or isinstance(page_idx, bool) or page_idx < 0:
            raise ValueError("mineru page_idx is invalid")
        for block in _list(page_info.get("blocks")):
            item = _mineru_block(_mapping(block), page_number=page_idx + 1)
            if item is not None:
                blocks.append(item)
    return blocks


def _mineru_block(block: Mapping[str, object], *, page_number: int) -> dict[str, object] | None:
    block_type = str(block.get("type") or "")
    if not block_type or block_type in _MINERU_SKIPPED_BLOCK_TYPES:
        return None
    item: dict[str, object] = {"page_idx": page_number - 1, "page_number": page_number}
    index = block.get("index")
    if isinstance(index, int) and not isinstance(index, bool):
        item["element_id"] = f"mineru-p{page_number}-b{index}"
        item["mineru_block_index"] = index
    if bbox := _mineru_bbox(block.get("bbox")):
        item["bbox"] = bbox
    content = block.get("content")
    if block_type in _MINERU_TITLE_BLOCK_TYPES:
        item.update(type="title", text=_mineru_plain_text(content), level=block.get("level"))
    elif block_type == "equation":
        item.update(type="equation", text=str(content or "").strip(), text_format="latex")
    elif block_type in {"list", "index"}:
        item.update(type="list", text="\n".join(_mineru_leaf_texts(content)))
    elif block_type in _MINERU_VISUAL_BLOCK_TYPES:
        item.update(_mineru_visual_fields(block_type, content))
    else:
        # text・ref_text・aside_text・page_footnote など InlineSpan を持つ本文の block。
        item.update(type="text", text=_mineru_plain_text(content))
    text = str(item.get("text") or "").strip()
    if not text and not item.get("table_body") and block_type not in {"image", "chart"}:
        return None
    item["text"] = text
    return item


def _mineru_visual_fields(block_type: str, content: object) -> dict[str, object]:
    """image / table / chart / code の body と caption・footnote を content list の項目名で返す。"""
    body = ""
    captions: list[str] = []
    footnotes: list[str] = []
    for child in _list(content):
        child_info = _mapping(child)
        child_type = str(child_info.get("type") or "")
        child_content = child_info.get("content")
        if child_type.endswith("_body"):
            body = (
                child_content.strip()
                if isinstance(child_content, str)
                else _mineru_plain_text(child_content)
            )
        elif child_type.endswith("_caption"):
            captions.append(_mineru_plain_text(child_content))
        elif child_type.endswith("_footnote"):
            footnotes.append(_mineru_plain_text(child_content))
    captions = [value for value in captions if value]
    footnotes = [value for value in footnotes if value]
    fields: dict[str, object] = {"type": block_type}
    if block_type == "table":
        fields["table_caption"] = captions
        fields["table_footnote"] = footnotes
        if body:
            fields["table_body"] = body
            fields["text_as_html"] = body
        fields["text"] = "\n".join([*captions, body, *footnotes]).strip()
    elif block_type == "code":
        fields["code_body"] = body
        fields["text"] = "\n".join([*captions, body, *footnotes]).strip()
    else:
        fields[f"{block_type}_caption"] = captions
        fields[f"{block_type}_footnote"] = footnotes
        fields["text"] = "\n".join([*captions, *footnotes]).strip()
    return fields


def _mineru_leaf_texts(content: object) -> list[str]:
    """list / index の入れ子の block から、葉の本文を読み順に返す。"""
    texts: list[str] = []
    for child in _list(content):
        child_info = _mapping(child)
        child_content = child_info.get("content")
        if child_info.get("type") in {"list", "index"}:
            texts.extend(_mineru_leaf_texts(child_content))
        elif text := _mineru_plain_text(child_content):
            texts.append(text)
    return texts


def _mineru_plain_text(content: object) -> str:
    """InlineSpan の列（text / equation_inline / code_inline / hyperlink）の見える文字列。"""
    if isinstance(content, str):
        return content.strip()
    parts: list[str] = []
    for span in _list(content):
        span_info = _mapping(span)
        span_type = span_info.get("type")
        span_content = span_info.get("content")
        if span_type == "hyperlink":
            parts.append(_mineru_plain_text(span_content))
        elif span_type == "equation_inline" and isinstance(span_content, str):
            parts.append(f"${span_content}$")
        elif isinstance(span_content, str):
            parts.append(span_content)
    return "".join(parts).strip()


def _mineru_bbox(value: object) -> list[int] | None:
    if not isinstance(value, list | tuple) or len(value) != 4:
        return None
    if not all(isinstance(item, int | float) and not isinstance(item, bool) for item in value):
        return None
    return [int(float(item) * 1000) for item in value]


def _pages_from_elements(elements: list[dict[str, object]]) -> list[ExtractionPage]:
    return [
        ExtractionPage(page_number=number, label=f"page {number}")
        for number in sorted({_positive_int(element.get("page_number")) for element in elements})
    ]


def _extraction_pages(pages: Iterable[_RenderedPage]) -> list[ExtractionPage]:
    return [
        ExtractionPage(
            page_number=page.number,
            label=f"page {page.number}",
            width=float(page.width) if page.width else None,
            height=float(page.height) if page.height else None,
        )
        for page in pages
    ]


def _message_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            str(item.get("text") or "") for item in value if isinstance(item, dict)
        ).strip()
    raise ValueError("openai content is invalid")


def _image_size(data: bytes) -> tuple[int, int]:
    """画像ファイルの寸法(px)。読めなければ (0, 0)(bbox の換算とページの寸法を持たない)。"""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            return int(image.width), int(image.height)
    except Exception:
        return 0, 0


# dots.ocr の bbox の座標系(#502)。モデルへ入れた画像は、画像処理(HF の preprocessor_config.json
# の Qwen2VLImageProcessor: patch_size 14 x merge_size 2 = 28、min_pixels 3136、max_pixels
# 11289600)で smart_resize した寸法になり、出力の bbox はその寸法の px になる。公式の parser は
# post_process_cells で元の画像の px へ戻している(rednote-hilab/dots.ocr の
# dots_ocr/utils/layout_utils.py・image_utils.py・consts.py。commit 36d7248、2026-03-24 時点)。
# backend は OpenAI 互換 API を直接呼ぶので、同じ換算をここで行い、bbox を送った画像(PDF は
# 描いたページ画像、画像ファイルは元の画像)の px にそろえる。
# ai-foundations-lab(engchina/ai-foundations-lab、commit 572e9fa の 20260819/。#512)の検証とも
# 合う: 後継の dots.mocr も同じ preprocessor_config.json(HF rednote-hilab/dots.mocr、revision
# e539fbb)で、lab の README は 300 dpi の A4 の画像パッチを約 44,000(= 2492x3500 / 14^2。
# smart_resize で縮小されず 28 の倍数へ丸めるだけ)と記録し、prompt_grounding_ocr の bbox は
# 「processor 側の resize と合わない」としている。lab は bbox をページ画像の px としてそのまま
# 使うが、それは 300 dpi の A4 では丸めの差(0.5% 未満)しかないためで、ここの換算がその差も消す。
_DOTS_IMAGE_FACTOR = 28
_DOTS_MIN_PIXELS = 3136
_DOTS_MAX_PIXELS = 11289600


def _dots_model_input_size(width: int, height: int) -> tuple[int, int]:
    """dots.ocr の smart_resize と同じ規則で、モデルへ入る画像の寸法(幅, 高さ)を返す。"""
    factor = _DOTS_IMAGE_FACTOR
    h_bar = max(factor, round(height / factor) * factor)
    w_bar = max(factor, round(width / factor) * factor)
    if h_bar * w_bar > _DOTS_MAX_PIXELS:
        beta = math.sqrt((height * width) / _DOTS_MAX_PIXELS)
        h_bar = max(factor, math.floor(height / beta / factor) * factor)
        w_bar = max(factor, math.floor(width / beta / factor) * factor)
    elif h_bar * w_bar < _DOTS_MIN_PIXELS:
        beta = math.sqrt(_DOTS_MIN_PIXELS / (height * width))
        h_bar = math.ceil(height * beta / factor) * factor
        w_bar = math.ceil(width * beta / factor) * factor
        if h_bar * w_bar > _DOTS_MAX_PIXELS:
            beta = math.sqrt((h_bar * w_bar) / _DOTS_MAX_PIXELS)
            h_bar = max(factor, math.floor(h_bar / beta / factor) * factor)
            w_bar = max(factor, math.floor(w_bar / beta / factor) * factor)
    return w_bar, h_bar


def _dots_bboxes_to_page_px(
    elements: list[dict[str, object]], page: _RenderedPage
) -> list[dict[str, object]]:
    """dots.ocr の bbox(モデルの入力画像の px)を、送った画像の px(``page`` の寸法)へ戻す。

    寸法が分からない画像(読めない画像ファイル)は換算しない。
    """
    if page.width <= 0 or page.height <= 0:
        return elements
    input_width, input_height = _dots_model_input_size(page.width, page.height)
    sx = page.width / input_width
    sy = page.height / input_height
    for element in elements:
        x0, y0, x1, y1 = (float(value) for value in cast(list[float], element["bbox"]))
        element["bbox"] = [
            round(min(max(x0 * sx, 0.0), page.width), 2),
            round(min(max(y0 * sy, 0.0), page.height), 2),
            round(min(max(x1 * sx, 0.0), page.width), 2),
            round(min(max(y1 * sy, 0.0), page.height), 2),
        ]
    return elements


def _strip_fences(value: str) -> str:
    return "\n".join(
        line for line in value.splitlines() if not line.strip().startswith("```")
    ).strip()


def _dots_elements(value: str, page_number: int) -> list[dict[str, object]]:
    parsed = json.loads(_strip_fences(value))
    if isinstance(parsed, dict) and {"bbox", "category"} <= parsed.keys():
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise ValueError("dots layout must be a list")
    allowed = {
        "Caption",
        "Footnote",
        "Formula",
        "List-item",
        "Page-footer",
        "Page-header",
        "Picture",
        "Section-header",
        "Table",
        "Text",
        "Title",
    }
    result: list[dict[str, object]] = []
    for item in parsed:
        if not isinstance(item, dict) or item.get("category") not in allowed:
            raise ValueError("dots layout category is invalid")
        bbox = item.get("bbox")
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or not all(
                isinstance(number, int | float) and not isinstance(number, bool) for number in bbox
            )
        ):
            raise ValueError("dots layout bbox is invalid")
        if item["category"] != "Picture" and not isinstance(item.get("text"), str):
            raise ValueError("dots layout text is missing")
        mapped = dict(item)
        mapped["page_number"] = page_number
        if item["category"] == "Table":
            mapped["text_as_html"] = item.get("text")
        result.append(mapped)
    return result


# 外部の Dots.OCR API へページの画像と一緒に渡す指示（回答プロンプトの画面にも載せる。#2054）。
DOTS_OCR_PROMPT = (
    "## Step 1: Identify the layout elem"
    "ents\n"
    "Please output the layout informatio"
    "n from the PDF image, including eac"
    "h layout element's bbox, its catego"
    "ry, and the corresponding text cont"
    "ent within the bbox.\n"
    "\n"
    "## Step 2: Record bounding boxes\n"
    "Bbox format: [x1, y1, x2, y2]\n"
    "\n"
    "## Step 3: Classify each element\n"
    "Layout Categories: The possible cat"
    "egories are ['Caption', 'Footnote',"
    " 'Formula', 'List-item', 'Page-foot"
    "er', 'Page-header', 'Picture', 'Sec"
    "tion-header', 'Table', 'Text', 'Tit"
    "le'].\n"
    "\n"
    "## Step 4: Extract and format the o"
    "riginal text\n"
    "    - Picture: For the 'Picture' ca"
    "tegory, the text field should be om"
    "itted.\n"
    "    - Formula: Format its text as L"
    "aTeX.\n"
    "    - Table: Format its text as HTM"
    "L.\n"
    "    - All Others (Text, Title, etc."
    "): Format their text as Markdown.\n"
    "\n"
    "## Step 5: Check fidelity and readi"
    "ng order\n"
    "    - The output text must be the o"
    "riginal text from the image, with n"
    "o translation.\n"
    "    - All layout elements must be s"
    "orted according to human reading or"
    "der.\n"
    "\n"
    "## Step 6: Return the result\n"
    "Final Output: The entire output mus"
    "t be a single JSON object.\n"
)
_DOTS_IMAGE_TOKENS = "<|img|><|imgpad|><|endofimg|>"


def _with_layout_records(
    result: ParserRegistryResult, layout_records: dict[str, Any]
) -> ParserRegistryResult:
    """共通の抽出の変換の結果に、親子階層が読む LayoutRecord（#1334）を載せる。"""
    extraction = result.extraction
    if extraction is None:
        return result
    artifacts = {**extraction.parser_artifacts, "layout_records": layout_records}
    return replace(result, extraction=extraction.model_copy(update={"parser_artifacts": artifacts}))


def _convert_external_output(
    rendered: object,
    *,
    backend: ExternalParserBackend,
    source_profile: SourceProfile | None,
    parser_version: str,
    pages: list[ExtractionPage],
    extra_artifacts: dict[str, ExtractionMetadataValue],
) -> ParserRegistryResult:
    # backend 起動時は重い registry を load せず、実際の解析時だけ変換実装を読む。
    from rag_parser_core.registry import remap_external_ocr_output

    return remap_external_ocr_output(
        backend,
        rendered,
        source_profile=source_profile,
        parser_version=parser_version,
        pages=pages,
        extra_artifacts=extra_artifacts,
    )


# ponytail: 2 実装に必要な差分だけを登録し、動的 plugin/DSL は持たない。
ENGINE_SPECS = {
    "mineru": ExternalParserEngineSpec(
        backend="mineru",
        protocol="mineru_v1",
        capabilities=("pdf", "image", "office"),
        endpoint_field="rag_parser_mineru_api_host",
        model_field=None,
        api_key_field="rag_parser_mineru_api_key",
        call=ExternalParserClient._parse_mineru,
        convert=_convert_external_output,
    ),
    "dots_ocr": ExternalParserEngineSpec(
        backend="dots_ocr",
        protocol="openai_chat_completions",
        capabilities=("pdf", "image"),
        endpoint_field="rag_parser_dots_ocr_api_host",
        model_field="rag_parser_dots_ocr_model",
        api_key_field="rag_parser_dots_ocr_api_key",
        call=ExternalParserClient._parse_dots,
        convert=_convert_external_output,
    ),
}
EXTERNAL_PARSER_BACKENDS = tuple(ENGINE_SPECS)
