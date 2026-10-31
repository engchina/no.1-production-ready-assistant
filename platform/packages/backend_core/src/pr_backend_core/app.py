"""共通 FastAPI アプリ factory。

新サービスは `create_app(...)` 一発で、CORS・request-id/メトリクス・例外 envelope・
health/ready・/metrics を備えたアプリを得る。RAG のような既成サービスは各部品
(`install_exception_handlers` / `MetricsMiddleware` / `create_health_router` 等)を
個別採用してもよい。
"""

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager

from fastapi import APIRouter, FastAPI

from .api.errors import DEFAULT_UNHANDLED_MESSAGE, install_exception_handlers
from .api.health import create_health_router
from .observability.metrics import MetricsMiddleware, metrics_asgi_app
from .security.cors import configure_cors

# lifespan の型（FastAPI 互換）。
type Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def create_app(
    *,
    service_name: str,
    version: str,
    cors_origins: Sequence[str],
    api_router: APIRouter | None = None,
    api_prefix: str = "/api",
    readiness_checks_getter: Callable[[], Mapping[str, str]] | None = None,
    health_message: str | None = None,
    lifespan: Lifespan | None = None,
    unhandled_message: str = DEFAULT_UNHANDLED_MESSAGE,
    enable_metrics: bool = True,
) -> FastAPI:
    """標準構成の FastAPI アプリを生成する。

    Args:
        service_name: OpenAPI title に使うサービス名（`app.openapi()` に反映）。
        version: アプリ version（health/ready と OpenAPI に反映）。
        cors_origins: CORS 許可オリジン。
        api_router: 業務ルーター（`api_prefix` 配下に include）。
        readiness_checks_getter: `/ready` 用の依存設定チェック。
        lifespan: 起動/終了フック。
        unhandled_message: 未処理例外時のユーザー向けメッセージ。
        enable_metrics: MetricsMiddleware と `/metrics` を有効化するか。
    """
    # API ドキュメント（/docs・/redoc・/openapi.json）は公開しない（#748）。
    # schema はテストなどから `app.openapi()` で得られる。
    app = FastAPI(
        title=service_name,
        version=version,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    configure_cors(app, origins=cors_origins)

    app.add_middleware(MetricsMiddleware, enable_metrics=enable_metrics)

    install_exception_handlers(app, unhandled_message=unhandled_message)

    health_router = create_health_router(
        version_getter=lambda: version,
        readiness_checks_getter=readiness_checks_getter,
        message=health_message,
    )
    app.include_router(health_router, prefix=api_prefix)
    if api_router is not None:
        app.include_router(api_router, prefix=api_prefix)

    if enable_metrics:
        app.mount("/metrics", metrics_asgi_app())

    return app
