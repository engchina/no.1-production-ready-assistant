"""production-ready-backend-core: 3 サービス共通の FastAPI インフラ。

公開 API の入口。詳細は各サブモジュール（api / observability / security / config）参照。
"""

from .app import create_app
from .logging import configure_logging
from .schemas import ApiResponse, CursorPage, HealthData, Page, ReadinessStatus

__all__ = [
    "create_app",
    "configure_logging",
    "ApiResponse",
    "CursorPage",
    "HealthData",
    "Page",
    "ReadinessStatus",
]

__version__ = "0.2.0"
