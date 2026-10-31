"""サービス管理エンドポイント。

前処理 / Parser マイクロサービスの稼働状態の可視化(GET)と、systemd の unit の起動/停止(POST)
を提供する(#286)。制御はカタログ allowlist + feature flag + sudoers で三重に保護する。
"""

import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.config import Settings, get_settings
from app.schemas.common import ApiResponse
from app.schemas.service_management import (
    DeploymentMode,
    ServiceCatalogData,
    ServiceCatalogItemData,
    ServiceControlResultData,
    ServiceLogsData,
    ServiceModelCacheData,
    ServiceStatusData,
)
from app.services.catalog import (
    SERVICE_CATALOG,
    ServiceCatalogEntry,
    effective_execution_policy,
    get_catalog_entry,
    is_dev_mode,
    service_health_url,
)
from app.services.control import (
    ServiceAction,
    ServiceControlClient,
    ServiceControlError,
    ServiceFailureReason,
    ServiceLogsError,
    read_service_logs,
)
from app.services.status import probe_service_status

router = APIRouter()
logger = logging.getLogger(__name__)

_control_client = ServiceControlClient()


def _control_enabled(settings: Settings) -> bool:
    """制御の実効可否。dev(uv)は自動有効化し、prod は明示フラグを要する。"""
    return is_dev_mode(settings) or bool(settings.rag_service_control_enabled)


def _model_cache(entry: ServiceCatalogEntry) -> ServiceModelCacheData | None:
    """モデル DL を行うサービスのキャッシュの場所(読み取り専用)を作る。

    サービスは backend と同じ実行ユーザーで動くため、backend から見た ``~/.cache`` を示す。
    """
    if entry.model_cache_path is None:
        return None
    return ServiceModelCacheData(path=str(Path(entry.model_cache_path).expanduser()))


# 失敗の理由 → HTTP status。unit が無いは 404、sudoers / systemd が使えないは 503、それ以外は 502。
_FAILURE_STATUS: dict[ServiceFailureReason, int] = {
    "unit_not_found": status.HTTP_404_NOT_FOUND,
    "permission_denied": status.HTTP_503_SERVICE_UNAVAILABLE,
    "unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
    "failed": status.HTTP_502_BAD_GATEWAY,
}


def _catalog_item(settings: Settings, entry: ServiceCatalogEntry) -> ServiceCatalogItemData:
    """稼働状態を問い合わせず、カタログ情報だけを返す。"""
    return ServiceCatalogItemData(
        service_id=entry.service_id,
        category=entry.category,
        profile=entry.profile,
        label_key=entry.label_key,
        execution_policy=effective_execution_policy(settings, entry),
        configured=bool(service_health_url(settings, entry)),
        systemd_unit=entry.systemd_unit,
        model_cache=_model_cache(entry),
    )


def _deployment_mode(settings: Settings) -> DeploymentMode:
    return settings.environment


@router.get("/catalog", response_model=ApiResponse[ServiceCatalogData])
async def list_service_catalog() -> ApiResponse[ServiceCatalogData]:
    """稼働プローブを行わず、サービス一覧と制御可否だけを返す。"""
    settings = get_settings()
    return ApiResponse(
        data=ServiceCatalogData(
            control_enabled=_control_enabled(settings),
            deployment_mode=_deployment_mode(settings),
            services=[_catalog_item(settings, entry) for entry in SERVICE_CATALOG],
        )
    )


@router.get("/{service_id}/status", response_model=ApiResponse[ServiceStatusData])
async def get_service_status(service_id: str) -> ApiResponse[ServiceStatusData]:
    """1 サービスの稼働状態を返す。"""
    settings = get_settings()
    entry = get_catalog_entry(service_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="指定したサービスが見つかりません。",
        )
    service_status = await probe_service_status(settings, entry)
    return ApiResponse(
        data=ServiceStatusData(
            **_catalog_item(settings, entry).model_dump(),
            status=service_status,
        )
    )


@router.get("/{service_id}/logs", response_model=ApiResponse[ServiceLogsData])
async def get_service_logs(
    service_id: str,
    lines: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> ApiResponse[ServiceLogsData]:
    """1 サービスのログ末尾を返す(journalctl -u <unit>)。"""
    settings = get_settings()
    entry = get_catalog_entry(service_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="指定したサービスが見つかりません。",
        )
    try:
        logs = await read_service_logs(settings, entry, lines)
    except ServiceLogsError as exc:
        logger.warning(
            "service_logs_failed",
            extra={"service_id": service_id, "lines": lines, "reason": exc.reason},
        )
        raise HTTPException(
            status_code=_FAILURE_STATUS[exc.reason],
            detail=str(exc) or "ログを取得できませんでした。",
        ) from exc
    return ApiResponse(
        data=ServiceLogsData(
            service_id=logs.service_id,
            source=logs.source,
            lines=logs.lines,
            content=logs.content,
        )
    )


async def _control(service_id: str, action: ServiceAction) -> ApiResponse[ServiceControlResultData]:
    """共通の起動/停止ハンドラ(flag 確認 → allowlist 照合 → 実行 → 再プローブ)。"""
    settings = get_settings()
    if not _control_enabled(settings):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="サービスの起動/停止は無効化されています(rag_service_control_enabled)。",
        )
    entry = get_catalog_entry(service_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="指定したサービスが見つかりません。",
        )
    try:
        await _control_client.control(settings, entry, action)
    except ServiceControlError as exc:
        logger.warning(
            "service_control_failed",
            extra={
                "service_id": entry.service_id,
                "action": action,
                "exit_code": exc.result.exit_code,
                "reason": exc.result.reason,
            },
        )
        raise HTTPException(
            status_code=_FAILURE_STATUS[exc.result.reason or "failed"],
            detail=exc.result.detail or "サービスの操作に失敗しました。",
        ) from exc
    new_status = await probe_service_status(settings, entry)
    return ApiResponse(
        data=ServiceControlResultData(
            service_id=service_id,
            action=action,
            status=new_status,
        )
    )


@router.post("/{service_id}/start", response_model=ApiResponse[ServiceControlResultData])
async def start_service(service_id: str) -> ApiResponse[ServiceControlResultData]:
    """サービスを起動し、起動時にも起動するようにする(systemctl enable --now)。"""
    return await _control(service_id, "start")


@router.post("/{service_id}/stop", response_model=ApiResponse[ServiceControlResultData])
async def stop_service(service_id: str) -> ApiResponse[ServiceControlResultData]:
    """サービスを停止し、起動時にも起動しないようにする(systemctl disable --now)。"""
    return await _control(service_id, "stop")


@router.post("/{service_id}/restart", response_model=ApiResponse[ServiceControlResultData])
async def restart_service(service_id: str) -> ApiResponse[ServiceControlResultData]:
    """サービスを再起動する(systemctl restart。起動時の自動起動の設定は変えない)。"""
    return await _control(service_id, "restart")
