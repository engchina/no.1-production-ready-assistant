"""FastAPI エントリポイント。共通 app factory で薄く構成する。"""

from pr_backend_core import configure_logging, create_app

from app.api.router import api_router
from app.readiness import readiness_checks
from app.settings import get_settings

settings = get_settings()
configure_logging(settings.log_level, service_name="production-ready-service",
                  service_version=settings.app_version, environment=settings.environment)

app = create_app(
    service_name=settings.service_name,
    version=settings.app_version,
    cors_origins=settings.cors_origins,
    api_router=api_router,
    readiness_checks_getter=lambda: readiness_checks(get_settings()),
)
