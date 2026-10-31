"""FastAPI エントリポイント。共通 app factory で薄く構成する。"""

import logging
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pr_backend_core import configure_logging, create_app
from pr_backend_core.api.validation import (
    DEFAULT_VALIDATION_MESSAGE,
    VALIDATION_ERROR_CODE,
    validation_error_messages,
)
from pr_backend_core.observability.tracing import tracing_lifecycle
from pr_system_settings.auth.visibility import (
    OTHERS_CONTENT_FORBIDDEN_MESSAGE,
    OTHERS_CONTENT_FORBIDDEN_STATUS,
)
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from app.api.problems import api_problem_response, request_id_for, validation_field_problems
from app.api.router import api_router
from app.clients.oracle_runtime import close_oracle_pools
from app.features.nl2sql.ontology_definition_data_validation import shutdown_validation_jobs
from app.features.nl2sql.ontology_markdown_workspace import shutdown_markdown_preparations
from app.features.nl2sql.ontology_router import (
    OntologyApiRuntime,
    ontology_build_service,
    ontology_publish_service,
    ontology_runtime,
)
from app.features.nl2sql.service import (
    SCHEMA_CATALOG_EMPTY_ERROR_CODE,
    DbAdminOperationFailed,
    Nl2SqlPersistenceUnavailable,
    Nl2SqlRepositoryOperationFailed,
    Nl2SqlService,
    OthersContentForbiddenError,
    SchemaCatalogEmptyError,
    nl2sql_service,
)
from app.features.nl2sql.trace_spans import start_trace_span_store, stop_trace_span_store
from app.features.settings.errors import DatabaseWalletOperationError
from app.readiness import readiness_checks
from app.security.permissions import UNCLASSIFIED_PERMISSION, permission_for_route
from app.security.service import SecurityApiError, get_security_service
from app.security.store import SecurityMigrationRequired
from app.settings import get_settings

settings = get_settings()
configure_logging(
    settings.log_level,
    service_name=settings.service_name,
    service_version=settings.app_version,
    environment=settings.environment,
)
logger = logging.getLogger(__name__)

_ORACLE_RAW_DETAIL_RE = re.compile(r"\s*:?[ ]*ORA-\d{5}.*$", re.IGNORECASE)


def _runtime_readiness_checks() -> dict[str, str]:
    """汎用 /ready は event loop を塞がない軽量設定チェックだけを返す。"""

    return readiness_checks(get_settings())


@dataclass(frozen=True, slots=True)
class ServiceContainer:
    """DB I/O を行わずに構成できる request service container。"""

    nl2sql: Nl2SqlService
    ontology: OntologyApiRuntime


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    # Startup is intentionally DB-free. A fresh Compute can boot before the
    # database, wallet, or any application table is ready; those failures belong
    # to readiness probes or the first operation that actually needs them.
    with tracing_lifecycle(
        get_settings(),
        service_name=get_settings().service_name,
        capture_content=lambda: get_security_service().trace_recording.enabled(),
    ):
        application.state.services = ServiceContainer(
            nl2sql=nl2sql_service,
            ontology=ontology_runtime,
        )
        runtime_settings = get_settings()
        if runtime_settings.local_debug_enabled:
            logger.warning("local_debug_auth_bypass_enabled")
        # 処理の経過の span の書き手（#3226 の案 A）。thread は起動の後に作る（DB には触れない。
        # 書けなければ数えて捨て、主の処理は止めない）。
        start_trace_span_store()
        try:
            yield
        finally:
            await run_in_threadpool(shutdown_markdown_preparations, ontology_runtime)
            await run_in_threadpool(ontology_build_service.shutdown)
            await run_in_threadpool(ontology_publish_service.shutdown)
            await run_in_threadpool(shutdown_validation_jobs, ontology_runtime)
            # 待っている span を書いてから pool を閉じる。
            await run_in_threadpool(stop_trace_span_store)
            close_oracle_pools()


def _assert_route_manifest(application: FastAPI) -> None:
    missing: list[str] = []
    for path, operations in application.openapi().get("paths", {}).items():
        if not path.startswith("/api"):
            continue
        route_path = path.removeprefix("/api")
        for method in operations:
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            permissions = permission_for_route(method, route_path)
            if permissions and UNCLASSIFIED_PERMISSION in permissions:
                missing.append(f"{method.upper()} {path}")
    if missing:
        raise RuntimeError("未登録の API 権限があります: " + ", ".join(sorted(missing)))


app = create_app(
    service_name=settings.service_name,
    version=settings.app_version,
    cors_origins=settings.cors_origins,
    api_router=api_router,
    readiness_checks_getter=_runtime_readiness_checks,
    lifespan=lifespan,
    enable_metrics=settings.enable_metrics,
)
_assert_route_manifest(app)


@app.exception_handler(SchemaCatalogEmptyError)
async def schema_catalog_empty_handler(
    request: Request,
    exc: SchemaCatalogEmptyError,
) -> JSONResponse:
    """schema 未整備を表示文言そのままで機械判定可能な error_code 付き 400 にする。"""
    return api_problem_response(
        request,
        status_code=400,
        detail=str(exc),
        code=SCHEMA_CATALOG_EMPTY_ERROR_CODE,
    )


@app.exception_handler(OthersContentForbiddenError)
async def others_content_forbidden_handler(
    request: Request,
    exc: OthersContentForbiddenError,
) -> JSONResponse:
    """他の利用者の会話・実行に内容を足そうとした system_admin（#3244）。route が 403 に変え損ねた
    経路も、500 にせず同じ 403 にする。"""
    return api_problem_response(
        request,
        status_code=OTHERS_CONTENT_FORBIDDEN_STATUS,
        detail=OTHERS_CONTENT_FORBIDDEN_MESSAGE,
    )


@app.exception_handler(Nl2SqlPersistenceUnavailable)
async def nl2sql_persistence_unavailable_handler(
    request: Request,
    exc: Nl2SqlPersistenceUnavailable,
) -> JSONResponse:
    """永続化障害を統一 ApiResponse と retry hint へ正規化する。"""
    return api_problem_response(
        request,
        status_code=503,
        detail=exc.public_message,
        code=exc.reason_code,
        retryable=True,
        headers={"Retry-After": "5"},
    )


@app.exception_handler(Nl2SqlRepositoryOperationFailed)
async def nl2sql_repository_operation_failed_handler(
    request: Request,
    exc: Nl2SqlRepositoryOperationFailed,
) -> JSONResponse:
    """SQL 実装/互換性エラーを DB 全体の停止と誤認させず局所化する。"""
    return api_problem_response(
        request,
        status_code=500,
        detail=exc.public_message,
        code=exc.reason_code,
    )


@app.exception_handler(DbAdminOperationFailed)
async def db_admin_operation_failed_handler(
    request: Request,
    exc: DbAdminOperationFailed,
) -> JSONResponse:
    """DB 管理操作の失敗を、画面で復旧できる構造化情報として返す。"""
    request_id = request_id_for(request)
    logger.error(
        "db_admin_operation_failed",
        extra={
            "request_id": request_id,
            "error_code": exc.error_code,
            "target_name": exc.target_name,
            "target_type": exc.target_type,
            "operation": exc.operation,
            "raw_error": exc.raw_message,
        },
    )
    return api_problem_response(
        request,
        status_code=500,
        detail=exc.summary,
        code=exc.error_code,
        extra={
            "error_details": {
                "summary": exc.summary,
                "cause": exc.cause,
                "actions": exc.actions,
                "target_name": exc.target_name,
                "target_type": exc.target_type,
                "operation": exc.operation,
            }
        },
    )


@app.exception_handler(SecurityApiError)
async def security_api_error_handler(
    request: Request,
    exc: SecurityApiError,
) -> JSONResponse:
    if exc.code == "SECURITY_SCHEMA_MIGRATION_REQUIRED":
        logger.error(
            "security_schema_migration_request_failed",
            extra={
                "request_id": request_id_for(request),
                "error_code": exc.code,
                "path": request.url.path,
            },
        )
    return api_problem_response(
        request,
        status_code=exc.status_code,
        detail=exc.public_message,
        code=exc.code,
        title=exc.title,
        retryable=exc.retryable,
        field_errors=exc.field_errors,
        # 429 の `Retry-After` など、エラーが持つ header を足す（#1087）。
        headers=exc.headers,
    )


@app.exception_handler(SecurityMigrationRequired)
async def security_migration_required_handler(
    request: Request,
    exc: SecurityMigrationRequired,
) -> JSONResponse:
    """Oracle store から直接伝播した schema 未適用も同じ 409 契約へ変換する。"""

    raw_message = str(exc.__cause__ or "").upper()
    code_match = re.search(r"\bORA-\d{5}\b", raw_message)
    logger.error(
        "security_schema_migration_required",
        extra={
            "request_id": request_id_for(request),
            "database_object": exc.object_name,
            "oracle_error_code": code_match.group(0) if code_match else "ORA-00942",
            "error_code": "SECURITY_SCHEMA_MIGRATION_REQUIRED",
            "path": request.url.path,
        },
    )
    return api_problem_response(
        request,
        status_code=409,
        title="セキュリティ初期化が必要です",
        detail=(
            "アプリケーション認証/RBAC の schema migration が未適用です。"
            "`uv run python -m app.cli.app_security_migrate --apply --skip-bootstrap` "
            "を実行してから再試行してください。"
        ),
        code="SECURITY_SCHEMA_MIGRATION_REQUIRED",
        retryable=False,
    )


@app.exception_handler(DatabaseWalletOperationError)
async def database_wallet_operation_error_handler(
    request: Request,
    exc: DatabaseWalletOperationError,
) -> JSONResponse:
    """Wallet install の 500 を安全な理由・復旧手順・問題コードで返す。"""

    logger.error(
        "database_wallet_operation_failed",
        extra={
            "request_id": request_id_for(request),
            "error_code": exc.code,
            "wallet_stage": exc.stage,
            "exception_type": type(exc.__cause__).__name__ if exc.__cause__ else None,
        },
    )
    return api_problem_response(
        request,
        status_code=500,
        detail=exc.public_message,
        code=exc.code,
        retryable=exc.retryable,
    )


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    """FastAPI/Pydantic の入力エラーを field pointer 付きで返す。

    `error_messages` は 3 製品共通の利用者向けの日本語の文（位置 + 文。自前の検証の文は
    そのまま）、`problem.field_errors` は欄ごとの文と技術的な原文（#1065）。
    """

    errors = exc.errors()
    messages = validation_error_messages(errors) or [DEFAULT_VALIDATION_MESSAGE]
    return api_problem_response(
        request,
        status_code=422,
        detail="\n".join(messages),
        code=VALIDATION_ERROR_CODE,
        field_errors=validation_field_problems(errors),
        extra={"error_messages": messages},
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_problem_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    """汎用 HTTP error も同じ problem 契約へ正規化する。"""

    code: str | None = None
    field_errors: list[Mapping[str, str]] = []
    error_details: Mapping[str, Any] | None = None
    if exc.status_code == 500:
        detail = "サーバー内部でエラーが発生しました。時間をおいて再試行してください。"
    elif isinstance(exc.detail, str) and exc.detail.strip():
        detail = _ORACLE_RAW_DETAIL_RE.sub("", exc.detail.strip()).rstrip(" :。")
        if _ORACLE_RAW_DETAIL_RE.search(exc.detail):
            detail += "。データベースの状態と実行権限を確認して再試行してください。"
    elif isinstance(exc.detail, dict):
        raw_code = exc.detail.get("code")
        code = raw_code if isinstance(raw_code, str) and raw_code else None
        raw_field_errors = exc.detail.get("field_errors")
        if isinstance(raw_field_errors, list):
            field_errors = [
                {
                    "pointer": str(item.get("pointer") or ""),
                    "code": str(item.get("code") or ""),
                    "message": str(item.get("message") or ""),
                }
                for item in raw_field_errors
                if isinstance(item, Mapping)
            ]
        detail = next(
            (
                value
                for key in ("message_ja", "message", "error")
                if isinstance((value := exc.detail.get(key)), str) and value.strip()
            ),
            "リクエストの処理に失敗しました。入力内容を確認してください。",
        )
        raw_error_details = exc.detail.get("error_details")
        if isinstance(raw_error_details, Mapping) and raw_error_details:
            # 画面の「詳細」に出す値（回答前の確認の LLM の失敗など。#1715）。code は problem の
            # code と「詳細」に出すので、本文の先頭に付けない。
            error_details = dict(raw_error_details)
        elif code and code not in detail:
            detail = f"{code}: {detail}"
    else:
        detail = "リクエストの処理に失敗しました。入力内容を確認してください。"
    return api_problem_response(
        request,
        status_code=exc.status_code,
        detail=detail,
        code=code,
        field_errors=field_errors,
        headers=exc.headers,
        extra={"error_details": error_details} if error_details else None,
    )


@app.exception_handler(Exception)
async def unhandled_problem_handler(request: Request, exc: Exception) -> JSONResponse:
    """未処理例外は詳細を返さず request id でログと相関する。"""

    logger.exception(
        "unhandled_api_error",
        extra={
            "request_id": getattr(request.state, "request_id", None),
            "method": request.method,
            "path": request.url.path,
            "exception_type": type(exc).__name__,
        },
    )
    return api_problem_response(
        request,
        status_code=500,
        detail="サーバー内部でエラーが発生しました。時間をおいて再試行してください。",
        code="INTERNAL_SERVER_ERROR",
    )
