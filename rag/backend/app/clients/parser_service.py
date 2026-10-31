"""parser マイクロサービスを呼ぶ HTTP クライアント。

各 adapter は parser サービスへ HTTP で委譲する。接続失敗・timeout・retry 後の 5xx 時は
通常 warning 付き fallback(`extraction=None`)を返す。ユーザーが明示選択した backend は
fail-fast にし、友好的なエラーで取込を止める。

`parse_with_registry(..., external_adapter_runner=client.runner)` の形で注入する。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

import httpx
from pr_backend_core.internal_http import http_client_options
from pr_backend_core.logging import safe_exception_fields, safe_url
from pr_backend_core.observability.request_context import outbound_correlation_headers
from rag_parser_core.capabilities import supported_modalities
from rag_parser_core.language import overrides_payload
from rag_parser_core.result import ParseResponse, ParserRegistryResult

from app.clients.http_retry import request_with_retry, retry_config_from_settings
from app.config import Settings
from app.rag.language_pack_releases import active_release
from app.schemas.document import SourceProfile

logger = logging.getLogger(__name__)

# 解析中のページの進捗を読む timeout(秒)。進捗は補助なので短くし、再試行しない(#1999)。
_PROGRESS_TIMEOUT_SECONDS = 3.0
# 解析サービスへ送る進捗の id(#1999)。``asyncio.to_thread`` の thread も context を引き継ぐ。
_PARSE_PROGRESS_ID: ContextVar[str | None] = ContextVar("rag_parse_progress_id", default=None)


@contextmanager
def parse_progress_id(progress_id: str | None) -> Iterator[None]:
    """この中の解析サービスの呼び出しに進捗の id を付ける(#1999)。"""
    token = _PARSE_PROGRESS_ID.set(progress_id)
    try:
        yield
    finally:
        _PARSE_PROGRESS_ID.reset(token)


# 設定の service URL フィールド名(backend ごと)。
_SERVICE_URL_FIELDS: dict[str, str] = {
    "docling": "rag_parser_docling_service_url",
    "unstructured": "rag_parser_unstructured_service_url",
}


_SERVICE_LABELS: dict[str, str] = {
    "docling": "Docling",
    "unstructured": "Unstructured",
}

_MODALITY_LABELS: dict[str, str] = {
    "pdf": "PDF",
    "image": "画像",
    "text": "テキスト",
    "html": "HTML",
    "email": "メール",
    "office": "Office",
}


def supported_formats_label(backend: str) -> str:
    """backend の対応形式を「PDF・画像」形式の日本語ラベルにする(未宣言は空文字)。"""
    modalities = supported_modalities(backend)
    return "・".join(
        _MODALITY_LABELS.get(modality.value, modality.value) for modality in modalities
    )


class ParserServiceUnavailableError(RuntimeError):
    """明示選択された parser サービスを利用できないため取込を止めるエラー。"""

    safe_for_user = True

    def __init__(
        self,
        backend: str,
        reason: str,
        *,
        service_url: str | None = None,
        status_code: int | None = None,
        attempts: int = 1,
        warning_code: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        self.backend = backend
        self.reason = reason
        self.service_url = service_url
        self.status_code = status_code
        self.attempts = attempts
        self.warning_code = warning_code
        # parser サービスが warning の ``<code>: <detail>`` に添えた失敗の型名(または
        # safe_for_user の説明)。利用者向けの文言に「原因」として添える(#310)。
        self.error_detail = error_detail
        super().__init__(
            _service_unavailable_message(
                backend,
                reason,
                service_url=service_url,
                status_code=status_code,
                attempts=attempts,
                warning_code=warning_code,
                error_detail=error_detail,
            )
        )


class ParserServiceClient:
    """設定された parser サービス群を呼ぶ同期 HTTP クライアント。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._timeout = float(settings.rag_parser_service_timeout_seconds)
        self._retry = retry_config_from_settings(settings)
        # 解析中の進捗の id → 解析サービスの URL(#1999)。解析の呼び出しの間だけ持つ。
        self._progress_urls: dict[str, str] = {}

    def service_url(self, backend: str) -> str | None:
        field = _SERVICE_URL_FIELDS.get(backend)
        if field is None:
            return None
        # 既定は 127.0.0.1:<port>(ネイティブ配備)。稼働プローブと同じ解決を使う。
        from app.services.catalog import resolve_service_base_url

        url = resolve_service_base_url(self._settings, field)
        return url or None

    def runner(
        self,
        backend: str,
        source_bytes: bytes,
        source_profile: SourceProfile | None,
        content_type: str,
        *,
        fail_fast: bool = False,
    ) -> ParserRegistryResult:
        """`ExternalAdapterRunner` 互換: 1 backend を HTTP で実行する。

        ``parse_progress_id`` の中で呼ぶと、その id を解析サービスへ送り、解析の間は
        ``fetch_progress`` でページの進捗を読めるようにする(#1999)。
        """
        url = self.service_url(backend)
        if url is None:
            if fail_fast:
                raise ParserServiceUnavailableError(
                    backend,
                    "unconfigured",
                    service_url=url,
                )
            return _fallback(backend, f"{backend}_adapter_service_unconfigured")
        files = {
            "file": (
                (source_profile.sanitized_file_name if source_profile is not None else "upload"),
                source_bytes,
                content_type or "application/octet-stream",
            )
        }
        data = {
            "content_type": content_type,
            "source_profile": (
                source_profile.model_dump_json() if source_profile is not None else "null"
            ),
        }
        if release := active_release():
            # 公開時の完全な値を送る。サービス側の後日の既定値を重ねない。
            data["language_pack"] = json.dumps({"snapshot": release.effective}, ensure_ascii=False)
        elif overrides := overrides_payload():
            # 画面で上書きした言語資源(#2181)をサービスへ渡す(組み込みとの差だけ)。
            data["language_pack"] = json.dumps(overrides, ensure_ascii=False)
        progress_id = _PARSE_PROGRESS_ID.get()
        if progress_id is not None:
            data["progress_id"] = progress_id
        try:
            payload = self._post_parse_json(
                backend, url, files=files, data=data, progress_id=progress_id
            )
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            logger.warning(
                "parser service returned error status",
                extra={
                    "parser_backend": backend,
                    "service_url": safe_url(url),
                    "status_code": status_code,
                    "attempts": self._retry.attempts,
                    **safe_exception_fields(exc),
                },
            )
            if fail_fast:
                raise ParserServiceUnavailableError(
                    backend,
                    "http_error",
                    service_url=url,
                    status_code=status_code,
                    attempts=self._retry.attempts,
                ) from exc
            return _fallback(backend, f"{backend}_adapter_service_unreachable")
        except httpx.TimeoutException as exc:
            logger.warning(
                "parser service call timed out",
                extra={
                    "parser_backend": backend,
                    "service_url": safe_url(url),
                    "attempts": self._retry.attempts,
                    **safe_exception_fields(exc),
                },
            )
            if fail_fast:
                raise ParserServiceUnavailableError(
                    backend,
                    "timeout",
                    service_url=url,
                    attempts=self._retry.attempts,
                ) from exc
            return _fallback(backend, f"{backend}_adapter_service_unreachable")
        except httpx.HTTPError as exc:
            logger.warning(
                "parser service call failed",
                extra={
                    "parser_backend": backend,
                    "service_url": safe_url(url),
                    "attempts": self._retry.attempts,
                    **safe_exception_fields(exc),
                },
            )
            if fail_fast:
                raise ParserServiceUnavailableError(
                    backend,
                    "unreachable",
                    service_url=url,
                    attempts=self._retry.attempts,
                ) from exc
            return _fallback(backend, f"{backend}_adapter_service_unreachable")
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "parser service returned invalid JSON",
                extra={
                    "parser_backend": backend,
                    "service_url": safe_url(url),
                    "attempts": self._retry.attempts,
                    **safe_exception_fields(exc),
                },
            )
            if fail_fast:
                raise ParserServiceUnavailableError(
                    backend,
                    "invalid_response",
                    service_url=url,
                    attempts=self._retry.attempts,
                ) from exc
            return _fallback(backend, f"{backend}_adapter_service_invalid_response")
        try:
            result = ParseResponse.model_validate(payload).to_result()
        except ValueError as exc:
            logger.warning(
                "parser service returned invalid payload",
                extra={
                    "parser_backend": backend,
                    "service_url": safe_url(url),
                    **safe_exception_fields(exc),
                },
            )
            if fail_fast:
                raise ParserServiceUnavailableError(
                    backend,
                    "invalid_response",
                    service_url=url,
                    attempts=self._retry.attempts,
                ) from exc
            return _fallback(backend, f"{backend}_adapter_service_invalid_response")
        if fail_fast and result.extraction is None:
            warning_code, error_detail = (
                split_parser_warning(result.warnings[0]) if result.warnings else (None, None)
            )
            raise ParserServiceUnavailableError(
                backend,
                _parser_result_failure_reason(result),
                service_url=url,
                attempts=self._retry.attempts,
                warning_code=warning_code,
                error_detail=error_detail,
            )
        return result

    def _post_parse_json(
        self,
        backend: str,
        url: str,
        *,
        files: Mapping[str, tuple[str, bytes, str]],
        data: Mapping[str, str],
        progress_id: str | None = None,
    ) -> object:
        if progress_id is not None:
            self._progress_urls[progress_id] = url
        try:
            return self._post_parse_json_once(backend, url, files=files, data=data)
        finally:
            if progress_id is not None:
                self._progress_urls.pop(progress_id, None)

    def fetch_progress(self, progress_id: str) -> Mapping[str, object] | None:
        """解析中の ``progress_id`` のページの進捗を解析サービスから読む(#1999)。

        解析中でない・進捗を返さない解析サービス(旧版は 404)・接続の失敗は None。進捗は
        補助の表示なので、再試行せず短い timeout で読む。
        """
        url = self._progress_urls.get(progress_id)
        if url is None:
            return None
        try:
            with httpx.Client(
                timeout=_PROGRESS_TIMEOUT_SECONDS, **http_client_options(url)
            ) as client:
                response = client.get(
                    f"{url}/parse/progress/{progress_id}",
                    headers=outbound_correlation_headers(),
                )
            if response.status_code != 200:
                return None
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        return payload if isinstance(payload, Mapping) else None

    def _post_parse_json_once(
        self,
        backend: str,
        url: str,
        *,
        files: Mapping[str, tuple[str, bytes, str]],
        data: Mapping[str, str],
    ) -> object:
        with httpx.Client(timeout=self._timeout, **http_client_options(url)) as client:
            response = request_with_retry(
                client,
                "POST",
                f"{url}/parse",
                retry=self._retry,
                logger=logger,
                log_extra={
                    "parser_backend": backend,
                    "service_url": safe_url(url),
                },
                headers=outbound_correlation_headers(),
                files=files,
                data=data,
            )
            response.raise_for_status()
            return response.json()


def _fallback(backend: str, warning: str) -> ParserRegistryResult:
    return ParserRegistryResult(
        extraction=None,
        parser_backend=backend,
        parser_version="service_unavailable",
        fallback_used=True,
        template=f"{backend}_fallback",
        warnings=(warning,),
    )


def split_parser_warning(warning: str) -> tuple[str, str | None]:
    """parser の warning を ``(code, detail)`` に分ける。

    parser サービスは失敗の warning に型名や説明を添えることがある(#310)。
    - ``docling_adapter_failed: ImportError``(rag_parser_core の adapter 実行)
    - ``docling_unavailable: ImportError``(``service_failure_warning``。
      safe_for_user の例外は型名ではなく説明文)
    - ``docling_adapter_failed``(従来の形式。detail なし)
    detail は空白を畳み、長すぎる場合は切り詰める。
    """
    code, separator, detail = warning.partition(":")
    code = code.strip()
    if not separator:
        return code, None
    normalized = " ".join(detail.split())[:400]
    return code, normalized or None


def _parser_result_failure_reason(result: ParserRegistryResult) -> str:
    """HTTP 200 でも parser が extraction を返せない理由を user-facing reason へ寄せる。"""
    warnings = tuple(split_parser_warning(warning)[0] for warning in result.warnings)
    if any(warning.endswith("_adapter_package_missing") for warning in warnings):
        return "adapter_package_missing"
    if any(warning.endswith("_adapter_source_unsupported") for warning in warnings):
        return "adapter_source_unsupported"
    if any(warning.endswith("_adapter_feature_flag_disabled") for warning in warnings):
        return "adapter_feature_flag_disabled"
    if any(warning.endswith("_adapter_service_unconfigured") for warning in warnings):
        return "unconfigured"
    if any(warning.endswith("_adapter_service_invalid_response") for warning in warnings):
        return "invalid_response"
    if any(warning.endswith("_adapter_service_unreachable") for warning in warnings):
        return "unreachable"
    if any(warning.endswith("_adapter_invalid_input") for warning in warnings):
        return "adapter_invalid_input"
    if any(warning.endswith("_adapter_failed") for warning in warnings):
        return "adapter_failed"
    # ``service_failure_warning`` の ``<backend>_unavailable: <detail>``(解析中の例外)。
    if any(warning.endswith("_unavailable") for warning in warnings):
        return "adapter_failed"
    if result.unsupported_reason:
        return "adapter_source_unsupported"
    return "adapter_empty_result"


def _service_unavailable_message(
    backend: str,
    reason: str,
    *,
    service_url: str | None = None,
    status_code: int | None = None,
    attempts: int = 1,
    warning_code: str | None = None,
    error_detail: str | None = None,
) -> str:
    label = _SERVICE_LABELS.get(backend, backend)
    service_id = f"parser-{backend.replace('_', '-')}"
    retry_suffix = f"{attempts} 回試行しました。" if attempts > 1 else ""
    warning_suffix = f" エラーコード: {warning_code}" if warning_code else ""
    # 例外の型名(例: ImportError)。ログを開く前に失敗の種類が分かるようにする(#310)。
    detail_suffix = f"（原因: {error_detail}）" if error_detail else ""
    if reason == "engine_removed":
        return (
            f"この文書レシピが参照する解析エンジン（{label}）は削除されました。"
            "旧結果は保持されていますが、新しい処理は開始できません。"
            "文書レシピで利用可能な解析エンジンを選び直してください。"
        )
    if reason == "unconfigured":
        return (
            f"選択した文書解析サービス（{label}）の接続先 URL が未設定です。"
            "「運用設定 › サービス管理」でサービスの URL を確認してから再実行してください。"
        )
    if reason == "adapter_source_unsupported":
        formats = supported_formats_label(backend)
        formats_suffix = f"（対応形式: {formats}）" if formats else ""
        # 既定の Docling は PDF と画像だけを解析する(#286)。取込前の判定(parser_source_guard)と
        # 同じ対処を案内する(ファイル準備で形式を変えた場合などは、ここで止まる)。
        remedy = (
            "それ以外の形式は、この文書の「処理レシピ」で「文書解析」を Unstructured に変えて"
            "（「運用設定 › サービス管理」で Unstructured を起動して）から再実行してください。"
            if backend == "docling"
            else "別の解析エンジンを選ぶか、対応形式に変換してから再実行してください。"
        )
        return (
            f"選択した文書解析サービス（{label}）はこのファイル形式を処理できません"
            f"{formats_suffix}。{remedy}{warning_suffix}"
        )
    if reason == "adapter_package_missing":
        return (
            f"選択した文書解析サービス（{label}）の実行に必要なパッケージが"
            "サービス内に見つかりません。"
            f"サービス管理画面で {service_id} のイメージ・依存関係を確認し、"
            "再ビルドまたは再起動してから再実行してください。"
            f"{warning_suffix}"
        )
    if reason == "adapter_invalid_input":
        return (
            f"選択した文書解析サービス（{label}）でファイルを読み取れませんでした。"
            "ファイルが破損しているか、この形式として読み取れません。"
            "元ファイルが正しく開けるか確認し、開ける場合は別の解析エンジンを試すか、"
            "ファイルを再作成・再変換してから再アップロードしてください。"
            f"{warning_suffix}"
        )
    if reason == "adapter_feature_flag_disabled":
        return (
            f"選択した文書解析サービス（{label}）が無効になっています。"
            "ナレッジベースまたはシステム設定でこの解析エンジンを有効にしてから"
            f"再実行してください。{warning_suffix}"
        )
    if reason == "adapter_failed":
        return (
            f"選択した文書解析サービス（{label}）で解析処理が失敗しました{detail_suffix}。"
            f"サービス管理画面で {service_id} のログを確認し、原因を修正してから"
            f"再実行してください。{warning_suffix}"
        )
    if reason == "adapter_empty_result":
        return (
            f"選択した文書解析サービス（{label}）が抽出結果を返しませんでした{detail_suffix}。"
            f"サービス管理画面で {service_id} のログを確認してから再実行してください。"
            f"{warning_suffix}"
        )
    if reason == "timeout":
        suffix = f" 接続先: {service_url}" if service_url else ""
        return (
            f"選択した文書解析サービス（{label}）の応答がタイムアウトしました。"
            f"{retry_suffix}サービス管理画面で {service_id} が running か、"
            "対象ファイルの解析に時間がかかっていないか確認してください。"
            f"{suffix}"
        )
    if reason == "http_error":
        status_text = f"HTTP {status_code}" if status_code is not None else "HTTP error"
        suffix = f" 接続先: {service_url}" if service_url else ""
        return (
            f"選択した文書解析サービス（{label}）の /parse が {status_text} を返しました。"
            f"{retry_suffix}/health が OK でも、解析処理側で一時的なエラーや依存関係の"
            "初期化失敗が発生している可能性があります。"
            f"サービス管理画面で {service_id} のログを確認し、必要なら再起動してから"
            "再実行してください。"
            f"{suffix}"
        )
    if reason == "invalid_response":
        return (
            f"選択した文書解析サービス（{label}）から不正な応答を受信しました。"
            f"{retry_suffix}サービス管理画面で {service_id} の状態を確認し、"
            "サービスを再起動してから"
            "再実行してください。"
        )
    suffix = f" 接続先: {service_url}" if service_url else ""
    return (
        f"選択した文書解析サービス（{label}）に接続できません。"
        f"{retry_suffix}サービス管理画面で {service_id} を起動し、running になってから"
        "再実行してください。"
        f"{suffix}"
    )
