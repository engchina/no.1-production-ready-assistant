"""API 共通 response model。

DB の状態（`GET /api/ready/database`）の型は 3 製品共通（`pr_system_settings.database_status`。
#325）を re-export する。
"""

from pr_system_settings.database_status import DatabaseStatusData

__all__ = ["DatabaseStatusData"]
