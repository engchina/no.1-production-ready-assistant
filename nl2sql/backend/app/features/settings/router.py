"""RAG 由来のシステム設定 API。

アップロード保存先、モデル、データベース設定画面が期待する
契約を NL2SQL プロジェクトにも提供する。値は現在の Settings インスタンスへ
反映し、secret 本文はレスポンスに返さない。
"""

import logging
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pr_backend_core import ApiResponse
from pr_backend_core.observability.model_purpose import async_call_with_model_purpose
from pr_system_settings.answer_notice import build_answer_notice_router
from pr_system_settings.chat_attachments import build_chat_attachment_settings_router
from pr_system_settings.database import build_database_router
from pr_system_settings.model import build_model_router
from pr_system_settings.model_test_input import (
    MODEL_TEST_EMBEDDING_INPUT,
    MODEL_TEST_IMAGE_BYTES,
    MODEL_TEST_IMAGE_MIME_TYPE,
    MODEL_TEST_RERANK_DOCUMENTS,
    MODEL_TEST_RERANK_QUERY,
    MODEL_TEST_TEXT_PROMPT,
    MODEL_TEST_VISION_PROMPT,
    embedding_test_details,
    model_test_text_context,
    rerank_test_details,
)
from pr_system_settings.system_schema import system_tables_status_error
from pr_system_settings.trace_recording import build_trace_recording_router
from pr_system_settings.upload_storage import (
    build_upload_storage_router,
)
from starlette.responses import JSONResponse

from app import settings as app_settings
from app.api.concurrency import run_sync_io
from app.api.problems import api_problem_response
from app.clients.enterprise_ai import EnterpriseAiClient
from app.clients.genai import GenAiClient
from app.clients.oracle import close_oracle_pool, test_oracle_connection
from app.clients.oracle_diagnostics import oracle_connection_diagnostics
from app.features.settings.system_schema import SystemSchemaError, system_schema_manager
from app.features.settings.system_schema_runtime import reset_system_schema_runtime
from app.schemas.settings import (
    ModelSettingsTestRequest,
    SystemTablesInitializeRequest,
    SystemTablesOperationData,
    SystemTablesStatusData,
)
from app.security.service import get_security_service
from app.settings import (
    MODEL_SETTINGS_STORE,
    Settings,
    get_settings,
)

router = APIRouter(prefix="/settings", tags=["settings"])
# アップロード保存先は3製品共通の実装（platform の pr_system_settings。#97）。
# 保存先は3製品共通の `.env`（platform/.env。#211）。
# テストで get_settings / PLATFORM_ENV_FILE を差し替えられるよう、呼出時に module の値を参照する。
router.include_router(
    build_upload_storage_router(
        get_settings=lambda: get_settings(),
        env_file=lambda: app_settings.PLATFORM_ENV_FILE,
    )
)
# モデル設定も3製品共通の実装（pr_system_settings.model。#103）。
router.include_router(
    build_model_router(
        get_settings=lambda: get_settings(),
        store=MODEL_SETTINGS_STORE,
        run_model_test=lambda settings, request: _run_model_settings_test(settings, request),
    )
)


# データベース設定も3製品共通の実装（pr_system_settings.database。#108）。
# 接続そのもの（adapter）と接続 pool の後始末は NL2SQL のものを渡す。
router.include_router(
    build_database_router(
        get_settings=lambda: get_settings(),
        env_file=lambda: app_settings.PLATFORM_ENV_FILE,
        test_connection=lambda candidate: test_oracle_connection(candidate),
        on_saved=lambda _settings: close_oracle_pool(),
        connection_failure_log_extra=lambda exc: oracle_connection_diagnostics(exc),
        connection_security_enabled=True,
        password_reveal_enabled=True,
    )
)
# 回答の注意書きも3製品共通の実装（pr_system_settings.answer_notice。#2405）。
# 保存先は共通の表のこの製品の行（#2449）。
router.include_router(
    build_answer_notice_router(
        load=lambda: get_security_service().load_answer_notice(),
        save=lambda record: get_security_service().save_answer_notice(record),
    )
)
router.include_router(
    build_trace_recording_router(provider=lambda: get_security_service().trace_recording),
)
# チャットの添付を使うか（運用設定 › チャットの添付。設定は製品ごと。既定は無効。#2517）。
router.include_router(
    build_chat_attachment_settings_router(
        load=lambda: get_security_service().chat_attachment_settings.load(),
        save=lambda record: get_security_service().chat_attachment_settings.save(record),
        get_settings=lambda: get_settings(),
    )
)
logger = logging.getLogger(__name__)
run_in_threadpool = run_sync_io


@router.get(
    "/database/system-tables",
    response_model=ApiResponse[SystemTablesStatusData],
)
def get_system_tables_status() -> ApiResponse[SystemTablesStatusData]:
    """NL2SQL system table の状態を DDL なしで取得する。"""

    try:
        data = system_schema_manager.status()
    except Exception as exc:
        raise system_tables_status_error(exc) from exc
    return ApiResponse(data=SystemTablesStatusData.model_validate(data))


@router.post(
    "/database/system-tables/initialize",
    response_model=ApiResponse[SystemTablesOperationData],
)
def initialize_system_tables(
    request: Request,
    payload: SystemTablesInitializeRequest,
) -> ApiResponse[SystemTablesOperationData] | JSONResponse:
    """明示操作として system table を作成・更新または全再作成する。"""

    try:
        data = system_schema_manager.initialize(
            recreate=payload.recreate,
            confirmation=payload.confirmation,
            allow_destructive=payload.allow_destructive,
        )
    except SystemSchemaError as exc:
        return api_problem_response(
            request,
            status_code=exc.status_code,
            detail=exc.public_message,
            code=exc.code,
            retryable=True if exc.retryable else None,
            headers=exc.retry_headers,
        )
    try:
        reset_system_schema_runtime(
            schema_epoch=int(data["operation_state"]["schema_epoch"]),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "システムテーブルは更新されましたが、実行中 API の再接続に失敗しました。"
                "状態を再取得してから再試行してください。"
            ),
        ) from exc
    return ApiResponse(data=SystemTablesOperationData.model_validate(data))


async def _run_model_settings_test(
    settings: Settings,
    request: ModelSettingsTestRequest,
) -> dict[str, str | int | float | bool | None]:
    """対象モデルの実 API 呼び出しを行い、表示用 details を返す（共有 router の hook）。"""
    if request.target_type == "enterprise_text":
        text = await async_call_with_model_purpose(
            "connection.check",
            EnterpriseAiClient(settings=settings).generate,
            MODEL_TEST_TEXT_PROMPT,
            model_test_text_context("Production Ready NL2SQL"),
        )
        return {"response_chars": len(text), "surface": "llm"}
    if request.target_type == "enterprise_vision":
        text = await async_call_with_model_purpose(
            "connection.check",
            EnterpriseAiClient(settings=settings).generate_from_image,
            MODEL_TEST_IMAGE_BYTES,
            MODEL_TEST_VISION_PROMPT,
            mime_type=MODEL_TEST_IMAGE_MIME_TYPE,
        )
        return {
            "surface": "vision",
            "response_chars": len(text),
        }
    if request.target_type == "embedding":
        vectors = await async_call_with_model_purpose(
            "connection.check",
            GenAiClient(settings=settings).embed,
            [MODEL_TEST_EMBEDDING_INPUT],
            input_type="SEARCH_QUERY",
        )
        # 次元が設定の値（Oracle の VECTOR 列）と同じかを確かめる（#45）。
        return dict(embedding_test_details(vectors, expected_dim=int(settings.genai_embedding_dim)))
    # 固定の query と 2 文書を並べ、index と relevance_score が返るかを確かめる（#45）。
    ranks = await async_call_with_model_purpose(
        "connection.check",
        GenAiClient(settings=settings).rerank,
        MODEL_TEST_RERANK_QUERY,
        list(MODEL_TEST_RERANK_DOCUMENTS),
        top_n=len(MODEL_TEST_RERANK_DOCUMENTS),
    )
    return dict(rerank_test_details(ranks))


def _expand(path: str) -> Path:
    return Path(path).expanduser()


def _secret_value(*, current: str, update: str | None, clear: bool) -> str:
    """secret の保持・更新・削除を判定する。"""
    if clear:
        return ""
    if update is not None and update != "":
        return update
    return current


def _elapsed_ms(started: float) -> int:
    """perf_counter の開始時刻から経過 ms を返す。"""
    return max(0, round((time.perf_counter() - started) * 1000))
