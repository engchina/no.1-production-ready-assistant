"""データベース可用性 API（3製品共通の判定と契約。#325）。

判定の順と応答の形は `pr_system_settings.database_status` が持つ。NL2SQL が注入するのは次だけ。

- memory モード（deterministic + memory）の short circuit（DB を使わない）
- DeepSec の設定の判定（システム設定画面と同じ `deepsec_readiness`）
- incremental store の確認（migration の要否）と system schema の epoch の観測
- `context_id` の元にする値（実行モード・保存モード・接続先）
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from pr_backend_core.oracle_errors import is_oracle_connection_error
from pr_system_settings.database_status import (
    DatabaseSchemaProbeResult,
    DatabaseStatusData,
    build_database_status_router,
    safe_connection_error_detail,
    safe_schema_error_detail,
)

from app.api.concurrency import run_sync_io
from app.clients.oracle import test_oracle_connection
from app.clients.oracle_diagnostics import oracle_connection_diagnostics
from app.features.nl2sql.incremental_observability import record_ready_once
from app.features.nl2sql.service import nl2sql_service
from app.features.settings.system_schema_runtime import observe_system_schema_epoch
from app.readiness import READINESS_OK, deepsec_readiness, uses_oracle
from app.settings import Settings, get_settings

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


def _context_fields(settings: Settings) -> list[object]:
    """画面の作業状態を切り替える接続先の識別（生値は返さず hash にする）。"""
    return [
        settings.nl2sql_runtime_mode,
        settings.nl2sql_persistence_mode,
        settings.oracle_dsn,
        settings.oracle_user,
        settings.oracle_wallet_dir,
    ]


def _memory_short_circuit(settings: Settings) -> DatabaseStatusData | None:
    """deterministic + memory は DB を使わないため、接続を試さず ok を返す。"""
    if uses_oracle(settings):
        return None
    record_ready_once()
    return DatabaseStatusData(status="ok", check=READINESS_OK, detail="memory")


async def _incremental_store_probe(_settings: Any) -> DatabaseSchemaProbeResult:
    """incremental store の migration の要否を確認する（接続確認の成功後）。"""
    if nl2sql_service.uses_incremental_store:
        try:
            migrated, migration_detail = await run_sync_io(nl2sql_service.check_incremental_store)
        except Exception as exc:  # noqa: BLE001 - normalized readiness boundary
            logger.exception(
                "incremental_store_check_failed",
                extra={"exception_type": type(exc).__name__},
            )
            if is_oracle_connection_error(exc):
                # 接続・pool の失敗は「接続できない」。初期化の不足として案内しない（#820）。
                return DatabaseSchemaProbeResult(
                    status="unreachable",
                    check="migration_check_failed",
                    detail=safe_connection_error_detail(exc),
                )
            return DatabaseSchemaProbeResult(
                status="setup_required",
                check="migration_check_failed",
                detail=safe_schema_error_detail(exc),
            )
        if not migrated:
            # DB 接続設定は有効で probe も成功している。migration 未適用を
            # 接続情報の未設定として扱うと、設定画面の接続成功表示と矛盾する。
            return DatabaseSchemaProbeResult(
                status="setup_required",
                check="migration_required",
                detail=migration_detail,
            )

        try:
            await run_sync_io(observe_system_schema_epoch)
        except Exception as exc:  # noqa: BLE001 - readiness boundary で安全な状態へ正規化
            logger.warning(
                "system_schema_epoch_check_failed",
                extra={"exception_type": type(exc).__name__},
            )

    record_ready_once()
    return DatabaseSchemaProbeResult()


# 依存（get_settings / test_oracle_connection / nl2sql_service 等）は呼び出し時に module から
# 参照する（テストで差し替えるため）。
router.include_router(
    build_database_status_router(
        get_settings=lambda: get_settings(),
        test_connection=lambda settings: test_oracle_connection(settings),
        extra_readiness=deepsec_readiness,
        schema_probe=lambda settings: _incremental_store_probe(settings),
        short_circuit=lambda settings: _memory_short_circuit(settings),
        context_fields=_context_fields,
        connection_failure_log_extra=lambda exc: oracle_connection_diagnostics(exc),
    )
)
