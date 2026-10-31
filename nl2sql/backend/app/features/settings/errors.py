"""設定 API の安全な構造化エラー。

Wallet の設置エラーは3製品共通の実装（pr_system_settings.database。#108）。
`app.main` の専用 handler が使うため re-export する。
"""

from __future__ import annotations

from pr_system_settings.database import DatabaseWalletOperationError as DatabaseWalletOperationError
