"""サービス稼働状態プローブ(``systemctl show`` + /health。#286)。

各マイクロサービスの systemd の unit の状態と ``GET /health``(bounded timeout)を並列に問い合わせ、
稼働状態を正規化する。systemd を使えない環境(コンテナ・systemd なしの開発環境)では
/health だけで判定する(従来どおり)。
"""

from __future__ import annotations

import logging
from typing import Literal

import httpx
from pr_backend_core.internal_http import http_client_options

from app.config import Settings
from app.services.catalog import (
    ServiceCatalogEntry,
    service_health_url,
)
from app.services.control import read_unit_state

logger = logging.getLogger(__name__)

# - running: unit が active で /health が status=ok(正常稼働)
# - degraded: 到達したが status!=ok(例: LibreOffice 未導入)
# - starting: unit は active / activating だが /health にまだ到達できない(起動中・モデル読込中)
# - failed: unit が失敗して止まった(journalctl -u <unit> で原因を確認する)
# - stopped: unit が止まっている(systemd を使えない環境では /health の接続拒否 / timeout)。
#   unit の外のプロセスが同じポートで応答しているときは、その /health の状態を返す
# - not_installed: systemd の unit が登録されていない(配備で選ばなかったサービスなど)
# - unconfigured: URL 未設定
# - roadmap: 準備中(ロードマップ)のサービス。配備しないので問い合わせない(#3374。エラーではない)
ServiceRuntimeStatus = Literal[
    "running",
    "degraded",
    "starting",
    "failed",
    "stopped",
    "not_installed",
    "unconfigured",
    "roadmap",
]
HealthStatus = Literal["ok", "degraded", "unreachable"]
_ACTIVE_STATES = {"active", "reloading", "activating"}  # unit が動いている(起動中を含む)


async def probe_health(settings: Settings, url: str, service_id: str) -> HealthStatus:
    """/health を 1 回だけ問い合わせる(例外は unreachable へ縮退)。"""
    timeout = float(settings.rag_service_status_probe_timeout_seconds)
    try:
        async with httpx.AsyncClient(timeout=timeout, **http_client_options(url)) as client:
            response = await client.get(f"{url}/health")
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:  # noqa: BLE001 - 到達不可は unreachable へ正規化する境界
        logger.debug(
            "service status probe failed: service=%s url=%s error=%s", service_id, url, exc
        )
        return "unreachable"
    status = str(payload.get("status", "")).strip().lower() if isinstance(payload, dict) else ""
    return "ok" if status == "ok" else "degraded"


def _from_health(health: HealthStatus) -> ServiceRuntimeStatus:
    if health == "ok":
        return "running"
    if health == "degraded":
        return "degraded"
    return "stopped"


async def probe_service_status(
    settings: Settings, entry: ServiceCatalogEntry
) -> ServiceRuntimeStatus:
    """1 サービスの unit の状態と /health を問い合わせて稼働状態を返す。"""
    if entry.is_roadmap:
        return "roadmap"
    url = service_health_url(settings, entry)
    if not url:
        return "unconfigured"
    try:
        unit_state = await read_unit_state(settings, entry)
    except Exception as exc:  # noqa: BLE001 - systemd を使えない環境は /health だけで判定する
        logger.debug("systemctl show failed: service=%s error=%s", entry.service_id, exc)
        return _from_health(await probe_health(settings, url, entry.service_id))
    health = await probe_health(settings, url, entry.service_id)
    if unit_state.active_state in _ACTIVE_STATES:
        return "starting" if health == "unreachable" else _from_health(health)
    # unit の外(手元で uv run したプロセスなど)が同じポートで応答している場合は、その状態を出す。
    if health != "unreachable":
        return _from_health(health)
    if not unit_state.installed:
        return "not_installed"
    if unit_state.active_state == "failed":
        return "failed"
    return "stopped"
