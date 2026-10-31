"""parser マイクロサービス共通の FastAPI app factory。

各 parser サービス(docling/unstructured/dots_ocr)は本 factory を
使って同一の HTTP 契約(`POST /parse` / `GET /health`)を公開する。fastapi は
optional extra `service` でのみ要求し、core の依存(pydantic + charset-normalizer)は
軽量に保つ。
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import json
import os
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pr_backend_core.logging import configure_http_logging
from pydantic import ValidationError

from rag_parser_core.language import LanguagePack, LanguagePackError, bind_pack, pack_from_payload
from rag_parser_core.registry import ParserRegistryResult, run_external_adapter
from rag_parser_core.result import ParseHealth, ParseResponse
from rag_parser_core.source import SourceProfile

# service 系 backend(OCI クラウド)の /parse ハンドラ。bytes + content_type +
# source_profile + document_id + prompt を受け取り ParseResponse(StructuredExtraction
# wire 形式)を返す。document_id は OCI 入力 object 名の一意化、prompt は VLM 抽出指示に使う
# (DU など prompt 不要な backend は無視してよい)。
ServiceParseHandler = Callable[
    [bytes, str, "SourceProfile | None", str, str], Awaitable[ParseResponse]
]

_RUNTIME_HEALTH_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="parser-runtime-health",
)


def _detect_version(
    import_name: str,
    distribution_names: Sequence[str],
) -> tuple[bool, str | None]:
    """import 可否と配布 package version を返す(readiness 表示用)。"""
    installed = importlib.util.find_spec(import_name) is not None
    for distribution_name in distribution_names:
        try:
            return installed, importlib.metadata.version(distribution_name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return installed, None


def _parse_source_profile(raw: str | None) -> SourceProfile | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text or text.lower() == "null":
        return None
    try:
        return SourceProfile.model_validate(json.loads(text))
    except (ValidationError, json.JSONDecodeError):
        return None


def _runtime_health_ready(runtime_health: Callable[[], bool]) -> bool:
    timeout = float(os.environ.get("RAG_PARSER_RUNTIME_HEALTH_TIMEOUT_SECONDS", "2"))
    future = _RUNTIME_HEALTH_EXECUTOR.submit(runtime_health)
    try:
        return bool(future.result(timeout=timeout))
    except TimeoutError:
        future.cancel()
        return False
    except Exception:  # noqa: BLE001 - readiness 境界では degraded へ正規化する
        return False


def _language_pack(payload: str | None) -> LanguagePack | None:
    """要求の言語資源の差(JSON)を読む。形の誤りは 422(backend は検証した値だけを送る)。"""
    try:
        return pack_from_payload(payload)
    except (ValueError, LanguagePackError) as exc:
        raise HTTPException(status_code=422, detail=f"language_pack: {exc}") from exc


def create_parse_app(
    *,
    backend: str,
    import_name: str,
    distribution_names: Sequence[str] = (),
    runtime_health: Callable[[], bool] | None = None,
    title: str | None = None,
) -> FastAPI:
    """1 parser サービス用の FastAPI app を生成する。

    backend: adapter 名(docling/unstructured/dots_ocr)。
    import_name / distribution_names: readiness 表示の version 検出に使う。
    runtime_health: package 導入以外に必要な外部 runtime(vLLM 等)の疎通確認。
    """
    # API ドキュメント（/docs・/redoc・/openapi.json）は公開しない（#748）。
    app = FastAPI(
        title=title or f"parser-{backend}", docs_url=None, redoc_url=None, openapi_url=None
    )
    configure_http_logging(app, service_name=app.title)

    @app.get("/health", response_model=ParseHealth)
    async def health() -> ParseHealth:
        installed, version = _detect_version(import_name, distribution_names)
        runtime_ready = True
        if installed and runtime_health is not None:
            runtime_ready = _runtime_health_ready(runtime_health)
        return ParseHealth(
            status="ok" if installed and runtime_ready else "degraded",
            backend=backend,
            package_name=import_name,
            package_version=version,
        )

    # readiness は HEAD/GET いずれでも疎通確認できるよう alias を用意。
    @app.get("/api/ready", response_model=ParseHealth)
    async def ready() -> ParseHealth:
        return await health()

    @app.post("/parse", response_model=ParseResponse)
    async def parse(
        file: Annotated[UploadFile, File()],
        content_type: Annotated[str, Form()] = "",
        source_profile: Annotated[str | None, Form()] = None,
        language_pack: Annotated[str | None, Form()] = None,
    ) -> ParseResponse:
        source_bytes = await file.read()
        profile = _parse_source_profile(source_profile)
        effective_content_type = content_type or (
            profile.content_type if profile is not None else ""
        )
        # 言語資源の上書き(#2181)は、この要求の間だけ束ねる(to_thread は context を写す)。
        with bind_pack(_language_pack(language_pack)):
            result: ParserRegistryResult = await asyncio.to_thread(
                run_external_adapter,
                backend,
                source_bytes,
                profile,
                effective_content_type,
            )
        return ParseResponse.from_result(result)

    return app


def create_service_parse_app(
    *,
    backend: str,
    parse: ServiceParseHandler,
    is_configured: Callable[[], bool] | None = None,
    title: str | None = None,
) -> FastAPI:
    """OCI クラウド service 系 backend 用の FastAPI app を生成する。

    package readiness ではなく `is_configured`(OCI 設定の充足)で /health を返す。
    `parse` は OCI を呼んで ParseResponse を返す非同期ハンドラ(env 由来 config で構築)。
    """
    app = FastAPI(
        title=title or f"parser-{backend}", docs_url=None, redoc_url=None, openapi_url=None
    )
    configure_http_logging(app, service_name=app.title)

    @app.get("/health", response_model=ParseHealth)
    async def health() -> ParseHealth:
        configured = is_configured() if is_configured is not None else True
        return ParseHealth(
            status="ok" if configured else "degraded",
            backend=backend,
            package_name=None,
            package_version=None,
        )

    @app.get("/api/ready", response_model=ParseHealth)
    async def ready() -> ParseHealth:
        return await health()

    @app.post("/parse", response_model=ParseResponse)
    async def parse_endpoint(
        file: Annotated[UploadFile, File()],
        content_type: Annotated[str, Form()] = "",
        source_profile: Annotated[str | None, Form()] = None,
        document_id: Annotated[str | None, Form()] = None,
        prompt: Annotated[str, Form()] = "",
        language_pack: Annotated[str | None, Form()] = None,
    ) -> ParseResponse:
        source_bytes = await file.read()
        profile = _parse_source_profile(source_profile)
        effective_content_type = content_type or (
            profile.content_type if profile is not None else ""
        )
        effective_document_id = (
            (document_id or "").strip()
            or (profile.content_sha256 if profile is not None else "")
            or "document"
        )
        with bind_pack(_language_pack(language_pack)):
            return await parse(
                source_bytes, effective_content_type, profile, effective_document_id, prompt
            )

    return app
