"""ヘルスチェックエンドポイント。"""

import asyncio

from fastapi import APIRouter, Response, status
from pr_system_settings.database_status import (
    DatabaseSchemaProbeResult,
    build_database_status_router,
)

from app.clients.oracle import test_oracle_connection
from app.config import get_settings
from app.rag.system_schema import system_schema_manager
from app.readiness import readiness_checks, readiness_checks_are_ok
from app.schemas.common import ApiResponse, HealthData

router = APIRouter()


@router.get("/health", response_model=ApiResponse[HealthData])
async def health() -> ApiResponse[HealthData]:
    """サービス稼働状態を返す。"""
    settings = get_settings()
    return ApiResponse(
        data=HealthData(
            status="ok",
            version=settings.app_version,
            message="oci",
        )
    )


@router.get("/ready", response_model=ApiResponse[HealthData])
async def readiness(response: Response) -> ApiResponse[HealthData]:
    """依存設定を含めた readiness を返す。"""
    settings = get_settings()
    checks = readiness_checks(settings)
    ready = readiness_checks_are_ok(checks)
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ApiResponse(
        data=HealthData(
            status="ok" if ready else "degraded",
            version=settings.app_version,
            message="oci",
            checks=checks,
        )
    )


async def _system_schema_probe(_settings: object) -> DatabaseSchemaProbeResult:
    """RAG の system schema の状態（例外は共通部品が setup_required へ正規化する）。"""
    schema = await asyncio.to_thread(system_schema_manager.status)
    schema_status = schema["status"]
    if schema_status != "ready" or schema["operation_state"]["status"] == "running":
        return DatabaseSchemaProbeResult(status="setup_required", schema_status=schema_status)
    return DatabaseSchemaProbeResult(status="ok", schema_status="ready")


# データベースの利用可否（3製品共通の判定と契約。#325）。フロントの DB ゲートが
# 「設定ページ以外」を開く前に参照する。常に 200 で返し、status で
# ok / not_configured / unreachable / setup_required を区別する。
# 接続確認は module の `test_oracle_connection` を呼び出し時に参照する（テストで差し替える）。
router.include_router(
    build_database_status_router(
        get_settings=lambda: get_settings(),
        test_connection=lambda settings: test_oracle_connection(settings),
        schema_probe=_system_schema_probe,
    )
)
