"""3製品共通の認証基盤（ユーザー・ロール・セッション・ログイン。#212）。

- `domain`: ドメイン型（製品は `RoleRecord` / `Principal` を継承して項目を足す）
- `store`: `PLATFORM_*` テーブルの永続化
  （`InMemoryAuthStore` / `OracleAuthStore`）
- `service`: ログイン・セッション・構成管理者・ユーザー / ロール操作・
  権限昇格の防止（`AuthService`）
- `dependencies`: fail-closed の認可（`authorize_request`）
- `router`: 認証 API とユーザー管理・ロール管理 API（`build_auth_router`）
- `migrations`: `PLATFORM_*` の DDL（`apply_platform_auth_schema`）
- `service_token`: 製品間の呼び出しに使う短命の署名付き token（#230）
- `visibility`: 「AI 活用」の下の機能のデータの見え方（system_admin は全員、それ以外は自分だけ。
  #1889）
"""
