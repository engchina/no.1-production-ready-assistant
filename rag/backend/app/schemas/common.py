"""共通スキーマ。

汎用 envelope（ApiResponse / Page / HealthData / JsonValue）は共有 backend インフラ
`production-ready-backend-core`（pr_backend_core）へ移管した。3 サービスで同一契約を共有する。

DB の状態（`GET /api/ready/database`）の型も 3 製品共通（`pr_system_settings.database_status`。
#325）を re-export する。
- ok: 設定済みかつ実接続成功(検索・取込など DB 機能を利用できる)
- not_configured: 接続情報が未設定/不足(まず設定が必要)
- unreachable: 設定済みだが起動していない/到達できない(まず DB 起動が必要)
- setup_required: 接続済みだが RAG system schema の作成・更新が必要
"""

from pr_backend_core.schemas import ApiResponse, HealthData, JsonValue, Page
from pr_system_settings.database_status import DatabaseAvailability, DatabaseStatusData

# 互換のため re-export（既存の `from app.schemas.common import ApiResponse` 等を維持）。
__all__ = [
    "ApiResponse",
    "Page",
    "HealthData",
    "JsonValue",
    "DatabaseAvailability",
    "DatabaseStatusData",
]
