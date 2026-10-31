"""FastAPI エントリポイント。共通 app factory で薄く構成する。"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.routing import APIRoute
from pr_backend_core import ApiResponse, configure_logging, create_app
from pr_backend_core.observability.tracing import tracing_lifecycle
from pr_system_settings.auth.errors import SecurityApiError, SecurityMigrationRequired
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, Response

from app.api.router import api_router
from app.features.agent import storage_bootstrap
from app.features.agent.runtime import AgentRuntimeStorageUnavailableError
from app.features.agent.trace_spans import trace_spans
from app.observability import (
    metrics_response,
    start_trace_export_retry_worker,
    stop_trace_export_retry_worker,
)
from app.readiness import readiness_checks
from app.security.service import get_security_service
from app.settings import get_settings

settings = get_settings()
configure_logging(
    settings.log_level,
    service_name=settings.service_name,
    service_version=settings.app_version,
    environment=settings.environment,
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Run の repository・定義・履歴は DB を待たずにバックグラウンドで読み込む（DB に接続できない
    # あいだも起動する。RAG / NL2SQL と同じ。#1212）。
    with tracing_lifecycle(
        get_settings(),
        service_name=get_settings().service_name,
        capture_content=lambda: get_security_service().trace_recording.enabled(),
    ):
        storage_bootstrap.start_background_load()
        await start_trace_export_retry_worker()
        # 処理の経過の span の書き手（共通の span の表。#3226 の案 A。in_process の実行が書く）。
        await asyncio.to_thread(trace_spans.start)
        # 業務 Agent の自動実行のスケジューラ（#784。gunicorn は 1 worker のため 1 つだけ動く）。
        from app.features.agent.automations import run_scheduler

        scheduler = asyncio.create_task(run_scheduler())
        try:
            yield
        finally:
            scheduler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler
            storage_bootstrap.stop_background_load()
            await stop_trace_export_retry_worker()
            # 待っている span を書いてから終える（書けなければ書き手がログに残す）。
            await asyncio.to_thread(trace_spans.stop)
            # まだ書いていない Run・業務 Agent の変更を書いてから終える（#1939。event loop の上の
            # 変更はバックグラウンドで書くため）。書けなければログに残す。
            await _flush_runtime_writes()
            # キューに残った Run の事実を書いてから終える（間に合わない分は次の起動の backfill）。
            from app.features.agent import run_facts_store

            await asyncio.to_thread(run_facts_store.flush, 5.0)
            # 共通 DB（PLATFORM_ORACLE_*）の接続 pool を閉じる（#793）。
            from app.oracle_connection import close_platform_oracle_pool

            await asyncio.to_thread(close_platform_oracle_pool)


async def _flush_runtime_writes() -> None:
    from app.features.agent.runtime import loaded_runtime_repository

    repository = loaded_runtime_repository()
    if repository is None:
        return
    try:
        await asyncio.wait_for(asyncio.to_thread(repository.flush_writes), timeout=10.0)
    except Exception:  # noqa: BLE001 - 終了を止めない
        logger.warning(
            "agent_runtime_writes_not_flushed",
            extra={"pending_writes": repository.pending_writes()},
            exc_info=True,
        )


app = create_app(
    service_name=settings.service_name,
    version=settings.app_version,
    cors_origins=settings.cors_origins,
    api_router=api_router,
    readiness_checks_getter=lambda: readiness_checks(get_settings()),
    lifespan=lifespan,
)


@app.exception_handler(StarletteHTTPException)
async def project_http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Control Plane error code を envelope のまま機械可読に保つ。

    error code は `error_code` に分けて返し、`error_messages` は利用者に見せる文だけにする
    （画面は `error_messages` をそのまま出すため、`agent_unpublished: …` のような code を
    前に付けない。#911）。
    """
    detail = exc.detail
    error_code: str | None = None
    error_details: dict[str, object] = {}
    if isinstance(detail, dict):
        raw_code = detail.get("code")
        raw_message = detail.get("message")
        error_code = raw_code if isinstance(raw_code, str) else None
        messages = [raw_message] if isinstance(raw_message, str) else [str(detail)]
        error_details = {
            str(key): value for key, value in detail.items() if key not in {"code", "message"}
        }
    elif isinstance(detail, list):
        messages = [str(item) for item in detail]
    else:
        messages = [str(detail)]
    body = ApiResponse[object](data=None, error_messages=messages).model_dump(mode="json")
    if error_code:
        body["error_code"] = error_code
        body["error_details"] = error_details
    headers = dict(exc.headers or {})
    request_id = getattr(request.state, "request_id", None)
    if isinstance(request_id, str):
        headers["X-Request-ID"] = request_id
    return JSONResponse(status_code=exc.status_code, content=body, headers=headers)


@app.exception_handler(AgentRuntimeStorageUnavailableError)
async def storage_unavailable_handler(
    request: Request, exc: AgentRuntimeStorageUnavailableError
) -> JSONResponse:
    """Run・定義の保存先を読み込めない（DB に接続できない）ときは 503（#1212）。

    画面は DB ゲートで案内する。DB に接続できるようになれば、再起動せずに読み込む。
    """
    body = ApiResponse[object](data=None, error_messages=[str(exc)]).model_dump(mode="json")
    body["error_code"] = "agent_storage_unavailable"
    body["error_details"] = {}
    headers = {"Retry-After": "15"}
    request_id = getattr(request.state, "request_id", None)
    if isinstance(request_id, str):
        headers["X-Request-ID"] = request_id
    return JSONResponse(status_code=503, content=body, headers=headers)


def _security_error_response(
    request: Request,
    *,
    status_code: int,
    detail: str,
    code: str,
    title: str | None,
    retryable: bool,
    field_errors: Sequence[Mapping[str, str]],
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """認証・認可のエラー。

    共通画面が使う `error_code` と `problem` を足す（NL2SQL / RAG と同じ形）。
    `headers` は 429 の `Retry-After` など、エラーが持つ header（#1087）。
    """
    request_id = getattr(request.state, "request_id", None)
    response_headers = dict(headers or {})
    if isinstance(request_id, str):
        response_headers["X-Request-ID"] = request_id
    body = ApiResponse[object](data=None, error_messages=[detail]).model_dump(mode="json")
    body["error_code"] = code
    body["problem"] = {
        "title": title,
        "status": status_code,
        "detail": detail,
        "code": code,
        "request_id": request_id if isinstance(request_id, str) else None,
        "retryable": retryable,
        "field_errors": [dict(item) for item in field_errors],
    }
    return JSONResponse(status_code=status_code, content=body, headers=response_headers)


@app.exception_handler(SecurityApiError)
async def security_api_error_handler(request: Request, exc: SecurityApiError) -> JSONResponse:
    """認証・認可・ユーザー / ロール操作のエラー（共通認証。#215）。"""
    return _security_error_response(
        request,
        status_code=exc.status_code,
        detail=exc.public_message,
        code=exc.code,
        title=exc.title,
        retryable=exc.retryable,
        field_errors=exc.field_errors,
        headers=exc.headers,
    )


@app.exception_handler(SecurityMigrationRequired)
async def security_migration_required_handler(
    request: Request, exc: SecurityMigrationRequired
) -> JSONResponse:
    """共通認証・Agent のロール権限の表が未作成（security migration が必要）。"""
    logger.error("security_schema_migration_required", extra={"database_object": exc.object_name})
    return _security_error_response(
        request,
        status_code=409,
        detail=(
            "認証・権限のテーブルが未作成です。"
            "運用設定 > システムテーブル で「作成・更新」を実行するか、"
            "`cd agent/backend && uv run python -m app.cli.agent_system_schema --initialize` を"
            "実行してから再試行してください。"
        ),
        code="SECURITY_SCHEMA_MIGRATION_REQUIRED",
        title="セキュリティ初期化が必要です",
        retryable=False,
        field_errors=(),
    )


async def metrics() -> Response:
    return metrics_response()


app.router.routes.insert(
    0,
    APIRoute("/metrics", metrics, methods=["GET"], include_in_schema=False),
)
