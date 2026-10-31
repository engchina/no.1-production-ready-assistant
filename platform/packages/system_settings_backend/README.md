# production-ready-system-settings-backend（`pr_system_settings`）

3製品（RAG / NL2SQL / Agent）共通のシステム設定 API（#70）。NL2SQL の実装を基準にしている。

- `pr_system_settings.env_file`：`.env` の排他付き部分更新（`locked_env_file` / `replace_env_file` / `write_env_values`）
- `pr_system_settings.upload_storage`：アップロード保存先 API（`build_upload_storage_router`）
- `pr_system_settings.oci`：OCI 認証 API（`build_oci_router`。`~/.oci/config` の読み書き、秘密鍵の配置、接続テスト、Object Storage namespace 取得）。製品側からは `read_oci_config_text` / `parse_oci_config` / `read_runtime_oci_config` / `test_oci_config(verify_with_oci=)` も使える
- `pr_system_settings.auth`：3製品共通の認証基盤（#212）。ユーザー・ロール・ユーザーとロールの割り当て・セッションは `PLATFORM_*` テーブルで 3 製品が共有する
  - `store`（`InMemoryAuthStore` / `OracleAuthStore(connection_factory)`。ユーザーの一覧・ロールの一覧・principal のロールは、件数によらず一括で読む。製品のロールのデータは一括の hook `_roles_details`（`values_by_role_id` / `in_list_binds` で `ROLE_ID IN (...)`）で読む。#793）、`service`（`AuthService`：ログイン・ロック・セッション・CSRF・パスワード変更・構成管理者・ユーザー / ロール操作・最後の管理者の保護・製品をまたぐ権限昇格の防止）、`dependencies`（`authorize_request`）、`router`（`build_auth_router`）、`migrations`（`PLATFORM_AUTH_DDL` / `apply_platform_auth_schema`）
  - 製品は `AuthService` / `OracleAuthStore` を継承し、実効権限の組み立て（`_role_permissions` / `_build_principal`）と、ロールの製品データの読み書き（`_role_details` / `_replace_role_details` / `_before_delete_role`）を実装する。製品ごとの権限コードのテーブル（`NL2SQL_APP_ROLE_PERMISSIONS` / `RAG_ROLE_PERMISSIONS` / `AGENT_ROLE_PERMISSIONS`）は `PRODUCT_ROLE_PERMISSION_TABLES` に登録し、SYSTEM_ADMIN 以外のロール割り当てでは他製品の権限も操作者に収まることを確認する
  - 403 の `error_code`（#224）: 経路（API そのもの）の権限拒否は `SECURITY_ROUTE_FORBIDDEN`（manifest 未登録は `SECURITY_ROUTE_UNCLASSIFIED`。`errors.ROUTE_FORBIDDEN_CODES`）。CSRF の不一致は `SECURITY_CSRF_INVALID`、初回パスワード変更は `SECURITY_PASSWORD_CHANGE_REQUIRED`、権限の付与の制限などは `SECURITY_PERMISSION_DENIED`、製品の範囲外は製品のコード（例: RAG の `RAG_SCOPE_FORBIDDEN`）。frontend の `notifyAuthResponse` / `notifyAuthStatus`（`@production-ready/system-settings`）は、経路の権限拒否（とコードのない従来の 403）のときだけ権限なしの画面へ移し、それ以外は画面がその場で理由を表示する
- `pr_system_settings.users_roles`：ユーザー管理・ロール管理の API 契約（request / response の Pydantic model、`RoleData` の共通部分、パスワードポリシーと一時パスワード生成。#206）。永続化・認証・認可と、ロールに付ける権限（製品ごとに違う）は製品が持ち、製品は `RoleData` を継承して項目を足す
- `pr_system_settings.database_status`：DB の状態 API（`build_database_status_router`。`GET /ready/database`。#325）。画面の DB ゲートが使う `ok` / `not_configured` / `unreachable` / `setup_required` を、システム設定画面と同じ `database_readiness` → 製品の `test_connection` → 製品の `schema_probe` の順で判定する。`detail` は接続先を返さず ORA コード等だけにする。oracledb には依存せず、接続確認・準備状態の確認・DB を使わない構成の short circuit・`context_id` の元にする値は製品から注入する
- `pr_system_settings.system_schema`：システムテーブルの管理の骨格（`SystemSchemaManagerBase`。#325）。RAG と NL2SQL が共有する、状態の分類（`missing` / `partial` / `outdated` / `ready`）、操作の lease（`<製品>_SCHEMA_OPERATIONS`）と `schema_epoch`、適用済み migration の台帳（`<製品>_SCHEMA_MIGRATIONS`）、全再作成の確認語の検証、失敗の安全化（ORA コードだけ。ORA-00054 は 409 と `Retry-After`）、API の型（`SystemTableOperationState` / `SystemTablesInitializeRequest`）と状態を取得できないときの 503（`system_tables_status_error`）。manifest と DDL の正本、状態の組み立て（`_status_on`）と初期化の手順（`_initialize_on`）、実行中の job の確認、接続、製品名が入る文言と確認語は製品が持つ。oracledb には依存しない
- `pr_system_settings.oci_connectivity` / `oci_auth`：OCI 接続テストの段階判定と、OCI SDK config の非対話ロード（`oci` extra。SDK は遅延 import）

各製品は router を include し、製品ごとの差（Settings の取得、共通 `.env` の場所、書込み権限・操作権限の依存関係）を引数で渡す。
システム設定の保存先は3製品共通の `platform/.env`（`PLATFORM_*`。#211）で、製品は `platform_env_file(BACKEND_DIR)` の値を `env_file` に渡す。
モデル設定の `model-settings.json` と API key（`PLATFORM_OCI_ENTERPRISE_AI_API_KEY`、セカンダリ接続は `PLATFORM_OCI_ENTERPRISE_AI_SECONDARY_API_KEY`、ターシャリ接続は `PLATFORM_OCI_ENTERPRISE_AI_TERTIARY_API_KEY`）も共有する。`model-settings.json` はモデルの設定だけを持ち、製品固有の設定は置かない（他製品の保存で欠けるとその製品が読めなくなるため。RAG の文書解析の設定は RAG の `backend/parser-settings.json`。#1701）。
OCI Enterprise AI の接続はプライマリ接続・セカンダリ接続・ターシャリ接続（OpenAI / OpenAI 互換 API 向け。Project OCID は任意。#786）の 3 つまでで、モデルを呼ぶ製品は `enterprise_ai_connection_for_model(settings, model_id)` でモデルの接続（Endpoint・Project・API key）を引く（#533 / #542 / #786。[platform/README.md](../../README.md) の「OCI Enterprise AI のプライマリ接続・セカンダリ接続・ターシャリ接続」）。
既定のモデルは既定のテキストモデルと既定の Vision モデルの 2 つで、登録モデルがあれば 2 つとも必須（`validate_default_models`、空なら 422。#499 / #566）。実行時の解決（`enterprise_ai_default_model_id`）は、テキストが空の既存環境のために Vision → 登録モデルの先頭 の代替を残す。
OCI 認証の `action_dependencies` は、config 読込・接続テスト・namespace 取得に付ける依存関係（Agent は `require_admin`）。

```python
from pr_system_settings.upload_storage import build_upload_storage_router

router.include_router(
    build_upload_storage_router(
        get_settings=get_settings,
        env_file=lambda: PLATFORM_ENV_FILE,
        write_dependencies=[Depends(require_admin)],
    ),
    prefix="/settings",
)
```

Settings は pydantic の model で、`upload_storage_backend` / `local_storage_dir` / `object_storage_region` /
`object_storage_namespace` / `object_storage_bucket` / `max_upload_bytes`（任意で `oci_region`）を持つこと。

```bash
cd platform/packages/system_settings_backend
uv sync --locked --dev
uv run ruff format --check . && uv run ruff check . && uv run mypy src
uv run pytest && uv run bandit -r src && uv run pip-audit
```
