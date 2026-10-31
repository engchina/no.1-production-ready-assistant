"""health / ready エンドポイントのルーター factory（サービス横断で共通）。"""

from collections.abc import Callable, Mapping

from fastapi import APIRouter, Response, status

from ..schemas import ApiResponse, HealthData


def readiness_checks_are_ok(checks: Mapping[str, str]) -> bool:
    """readiness checks がすべて "ok" か判定する。"""
    return all(value == "ok" for value in checks.values())


def create_health_router(
    *,
    version_getter: Callable[[], str],
    readiness_checks_getter: Callable[[], Mapping[str, str]] | None = None,
    message: str | None = None,
) -> APIRouter:
    """`/health` と `/ready` を提供するルーターを生成する。

    Args:
        version_getter: アプリ version を返す callable。
        readiness_checks_getter: 依存設定の readiness check（name -> status）を返す callable。
            省略時は `/ready` は常に ok を返す（依存チェックなし）。各サービスは自分の
            OCI/Oracle 等のチェックをここで注入する。
        message: HealthData.message に載せる固定メッセージ（任意）。
    """
    router = APIRouter()

    @router.get("/health", response_model=ApiResponse[HealthData])
    async def health() -> ApiResponse[HealthData]:
        return ApiResponse(data=HealthData(status="ok", version=version_getter(), message=message))

    @router.get("/ready", response_model=ApiResponse[HealthData])
    async def readiness(response: Response) -> ApiResponse[HealthData]:
        checks = dict(readiness_checks_getter()) if readiness_checks_getter else {}
        ready = readiness_checks_are_ok(checks)
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ApiResponse(
            data=HealthData(
                status="ok" if ready else "degraded",
                version=version_getter(),
                message=message,
                checks=checks,
            )
        )

    return router
