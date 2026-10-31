# Backend standard（RAG を正本）

3 サービス（RAG / NL2SQL / Agent）の backend は同一スタックと同一プラットフォーム挙動を共有する。
正本は RAG backend。新サービスは技術選定をやり直さず、本標準と
[`packages/backend_core`](../packages/backend_core)（`production-ready-backend-core`）から派生する。

## 確定スタック

| 層 | 採用 |
|---|---|
| Language / PM | Python 3.12 / **uv** |
| Web | FastAPI（本番: Gunicorn + `uvicorn.workers.UvicornWorker` / 開発: `uvicorn --reload`） |
| Validation / Config | Pydantic v2 + pydantic-settings（`.env` + 任意 JSON 設定） |
| HTTP client | httpx |
| DB / Vector | Oracle AI Database + python-oracledb（Oracle AI Vector Search / Oracle Text）。対応バージョンは [deployment.md](./deployment.md) |
| LLM・VLM | OCI Enterprise AI |
| Embedding・Rerank | OCI Generative AI（Cohere） |
| Observability | Prometheus metrics + JSON logging + request-id |
| Test / Lint / Type | pytest(+asyncio,cov) / Ruff（lint・整形。black は使わない）/ mypy strict |
| Security | Bandit + pip-audit + gitleaks |
| CI | GitHub Actions（`uv sync --locked`） |

> ⚠️ LLM/VLM = Enterprise AI、embedding/rerank = OCI GenAI、ベクトル DB = Oracle AI Database。
> 外部ベクトル DB・別 LLM provider は導入しない（各業務 repo のルールに従う）。

## 役割分担（backend_core ↔ 業務 repo）

| backend_core（共通） | 業務 repo（features/*） |
|---|---|
| app factory `create_app` / CORS / security | ドメイン API ルーター |
| `/api/health` `/api/ready` / `/metrics` | readiness の業務依存チェック追加 |
| JSON logging / request-id / trace | プロジェクト固有 DB schema |
| 例外 → `ApiResponse` 統一 / pagination | RAG: ingestion/chunking/retrieval/eval |
| `ApiResponse` / `Page` / `HealthData` envelope | NL2SQL: NL→SQL / SQL 検証・実行 |
| `BaseServiceSettings` | Agent: run orchestration / tool registry |

## 統一 API レイアウト

```
backend/
  app/
    main.py            # create_app で薄く構成
    settings.py        # BaseServiceSettings を継承
    readiness.py       # 依存設定チェック
    api/router.py      # 業務ルーター集約（/api 配下）
    features/<domain>/ # RAG / NL2SQL / Agent の業務
  tests/
  pyproject.toml
  uv.lock
  .env.example
```

Dockerfile は持たない（自前のコードは Docker イメージを作らない。#286 / #356）。本番は各製品の `init_script.sh` が
作る systemd の unit で、uv の venv（`uv sync --locked --no-dev`）の Gunicorn + UvicornWorker として動かす。

共通エンドポイント: `GET /api/health` `GET /api/ready` `GET /metrics`。
業務エンドポイントは `features/<domain>` 配下に置く。

FastAPI の API ドキュメント（Swagger UI `/docs`・ReDoc `/redoc`・`/openapi.json`）は、環境を問わず公開しない（#748）。
`create_app` は `docs_url` / `redoc_url` / `openapi_url` を `None` にする。`create_app` を使わずに `FastAPI(...)` を作るアプリ
（RAG の backend・parser / 前処理 / pipeline のサービス）も同じ 3 つを `None` で渡す。schema が要るテストは `app.openapi()` を使う。

### DB の状態 API（`GET /api/ready/database`、#325）

画面の DB ゲートが使う DB の状態は、3製品とも `pr_system_settings.database_status.build_database_status_router` で提供する（製品が独自に実装しない）。ログイン不要の公開 path にする（各製品の `PUBLIC_API_PATHS`）。

- 応答: `{status: ok|not_configured|unreachable|setup_required, check, detail, context_id, schema_status}`。常に HTTP 200。
- 判定の順: `short_circuit`（DB を使わない構成）→ システム設定画面と同じ `database_readiness`（`ok` 以外は `not_configured`。接続は試さない）→ 製品の `test_connection`（bounded。失敗は `unreachable`）→ 製品の `schema_probe`（準備状態の確認）→ `ok`。
- `schema_probe` の例外は、接続・pool のエラー（`pr_backend_core.oracle_errors.is_oracle_connection_error`。`DPY-4005`・`DPY-6xxx`・`DPY-4011`・`ORA-125xx` / `12170`・`ORA-03113` / `03114`・`ORA-01017` など。`raise ... from` の原因の連鎖も見る）なら `unreachable`、それ以外（辞書・DDL・権限など）だけを `setup_required`（`check=schema_check_failed`）にする（#820）。接続確認の後に pool が接続を返さないとき、「初期化が必要」と案内しない。製品の `schema_probe` が自分で例外を正規化するとき（NL2SQL の `migration_check_failed`）も同じ関数で分ける。
- `unreachable` のときも OCI の制御 API へ DB の状態を問い合わせない（#10。DB は ADB Free コンテナで、起動・停止は docker 側の責務）。画面は接続設定と Wallet の確認を案内する。
- `detail` は接続先・資格情報・Wallet の path を返さない（ORA / DPY / DPI のコードだけ）。`context_id` は接続先の値の SHA-256 で、生値は返さない。
- 製品が注入するもの: `test_connection`（接続 pool と接続処理は製品が持つ）、`extra_readiness`（システム設定画面の `build_database_router` と同じもの）、`schema_probe`（RAG の system schema、NL2SQL の incremental store）、`short_circuit`（NL2SQL の memory モード、Agent のローカル認証）、`context_fields`（NL2SQL は実行モード・保存モードも含める）。
- `ok` の結果だけを、接続先と資格情報の指紋（hash。生値は持たない）ごとに 30 秒サーバー側で cache する（`ok_cache_seconds`。#793）。設定の判定は毎回行い、`unreachable` / `setup_required` は cache しない。DB 設定の保存（`build_database_router`）とシステムテーブルの操作（`SystemSchemaManagerBase.initialize` / `delete_orphaned_rows`）で `clear_database_status_cache` が捨てる。テストは autouse の fixture で捨てる。
- 接続 pool の回復（#820）: DB の停止中に作った python-oracledb の pool は、DB の起動後も接続を作らず `DPY-4005`（pool の待ちの timeout）を返し続けることがある（プロセスを再起動するまで直らなかった）。`SharedOraclePool.acquire` は、判定 `recover_pool_error(pool, exc)` に従い、`DPY-4005` で貸し出し中の接続が上限に達していない（本当の枯渇ではない）なら pool を捨てて新しい pool で 1 回だけやり直し、やり直しも失敗したらその例外（本当の接続エラー）を返す。接続できない（`DPY-6005` など）ときは同じ要求の中ではやり直さず、pool だけを捨てて例外を返す（次の要求は新しい pool から）。本当の枯渇と SQL のエラーでは pool を捨てない。捨てた pool は別のスレッドで閉じる（`pool.close()` は試している接続が終わるまで戻らず、接続先が応答しないと `tcp_connect_timeout` の間止まる。手元で 16 秒を観測）。RAG の共有 pool（`app.clients.oracle` の `acquire_connection`。`getmode` を `TIMEDWAIT`・30 秒にした）も同じ判定を使う。NL2SQL の DeepSec の pool（`OraclePoolManager`）は未対応。
- 接続の失敗の診断（#1744）: pool は接続の作成の失敗を背景のスレッドで握り、`acquire` には `DPY-4005` しか返さないことがある（ADB の停止・password の誤り・account lock・network・Wallet のどれでも同じ）。3 製品の system schema の CLI（`status` / `initialize` / `check` など）は、`pr_system_settings.system_schema` の `operation_failure_fields` / `deployment_check_error_summary` に接続引数の関数を渡し、`pr_backend_core.oracle_connect_diagnosis.diagnose_oracle_connect_failure` で理由と対処を日本語で出す（`code` は本当の ORA / DPY のコード、`cause` は分類、`hint` は理由と対処、`detail` は secret を伏せた本当の例外の文）。`DPY-4005` のときだけ、pool を使わずに 1 回だけ直接接続して（TNS の再試行を外し、`tcp_connect_timeout` を付ける。再試行しないので account lock を招かない）本当の例外から判定する。password・Wallet の password・DSN の資格情報は出さない。CLI（`configure_cli_logging`）では、python-oracledb の pool の背景のスレッドの例外（4.0.2〜26.0.1 の `_post_process_request` の `ValueError: list.remove(x)`）を、traceback ではなく 1 行の warning（`oracledb_background_thread_error`）にまとめる。
- CLI の接続（#1779）: 配備（`init_script.sh` の `retry_command`）が呼ぶ 1 回で終わる CLI（3 製品の system schema の CLI・NL2SQL の `app_security_migrate`）は、最初に `pr_backend_core.oracle_dsn.enable_cli_connect_mode()` を呼ぶ。製品の接続 pool は作る前に接続引数を `apply_cli_connect_mode` に通し、CLI のプロセスだけ、Wallet の別名と接続記述子の TNS の再試行（`(retry_count=20)(retry_delay=3)`）を外し（`dsn_without_retry`）、`retry_count=0`・`tcp_connect_timeout` を `CLI_TCP_CONNECT_TIMEOUT_SECONDS`（5 秒）以下にする。DB の停止中・届かないときも数秒〜20 秒ほどで #1744 の理由つきで終わる（試し直しは配備の `retry_command` が持つ）。常駐の server・worker は呼ばない（RAG の共有 pool は Wallet の別名の再試行を持ったまま、一時の切断に強い）。テストは各製品の conftest がテストごとに `disable_cli_connect_mode()` で戻す。配備の `suite-db-user.py` も同じ規則で TNS の再試行を外し、試し直しは自分の間隔（20 秒）に任せる。
- Wallet / mTLS の ADB では 1 回の接続に handshake と認証の往復がかかるため、要求ごとに新しい接続を張らないようにする。Agent の接続確認・準備状態の確認は `pr_backend_core.oracle_pool.SharedOraclePool` の pool から借りる（#793）。RAG / NL2SQL の `test_connection` は単発の接続（Wallet の retry を外した DSN）のままで、上の cache で回数を減らす。システム設定の「接続テスト」は保存前の候補を試すため、3 製品とも単発の接続のまま。NL2SQL の状態の保存先（業務プロファイル・ジョブ・履歴・オントロジー・評価の `NL2SQL_*` の表）は `nl2sql-state` の pool から借りる（`OracleNl2SqlAdapter.state_connection`、上限は `NL2SQL_ORACLE_STATE_POOL_MAX`。#830）。状態の保存先の接続は JSON CLOB を文字列で fetch し（行ごとの LOB の read の往復を無くす）、書き込みの commit は最後の文の実行に載せる（`autocommit`。#904）。NL2SQL の業務データの読み取り（SELECT の実行）と Select AI の生成（`DBMS_CLOUD_AI.GENERATE`。profile は引数で渡し session の profile を変えない）は、アプリの接続（system_admin と DeepSec 無効のとき）なら pool から借りる（`OracleNl2SqlAdapter.runtime_connection`、上限は pool ごとに `NL2SQL_ORACLE_RUNTIME_POOL_MAX`。#904）。pool は system_admin 用（`nl2sql-runtime-admin`）と非 system_admin 用（`nl2sql-runtime-user`）に分け、同じ pool・同じ接続を共有しない。actor の無い処理は単発の接続（`connection()`）。DeepSec 有効の非 system_admin は今までどおり DeepSec の DATA USER の pool（借りるたびに利用者の context を設定し、返すときに消す。消せなければ捨てる）で、アプリの接続の pool・状態の保存先の pool と共有しない。session の状態を変え得る処理（ALTER SESSION・`DBMS_CLOUD_AI.SET_PROFILE`・DDL / DML・Select AI Agent・オントロジーの操作のトランザクション）は単発の接続（`connection()`）のまま。
- 各製品の `/api/ready` の `oracle` check も同じ `database_readiness` を使う。
- 画面側は `@production-ready/system-settings` の `DatabaseGate` / `useDatabaseStatus` / `DatabaseUnavailableNotice` を使う（製品は API・導線・製品名の入る文言と、利用者がデータベース設定・システムテーブルを開けるか（`canManageDatabase` / `canManageSystemTables`。#820）だけを渡す。状態ごとの案内は UX 契約 messaging §3.4.1）。ゲートを通さない画面は3製品ともシステム設定の 5 画面（OCI 認証・アップロード保存先・モデル・データベース・外観と証明書）だけ。NL2SQL の保存領域の確認のような製品固有の確認は `secondaryGate` で差し込む。

### システムテーブルの管理（`/api/settings/database/system-tables`、#325）

製品のシステムテーブル（versioned Oracle system schema）の状態の取得・作成 / 更新・全再作成は、`pr_system_settings.system_schema.SystemSchemaManagerBase` の上に製品の manager を作る（RAG の `app.rag.system_schema`、NL2SQL の `app.features.settings.system_schema`、Agent の `app.system_schema`。#751）。

- API: `GET /api/settings/database/system-tables`（DDL を実行しない。取得できなければ 503、公開するのは ORA コードだけ）と `POST /api/settings/database/system-tables/initialize`（`{recreate, confirmation}`）。応答の骨格は `{status, schema_head, applied_versions, pending_versions, expected_/existing_object_count, expected_/existing_table_count, missing_objects, tables, operation_state}` で、製品が項目を足す（RAG の `schema_version` / `retired_objects`、NL2SQL の `objects`）。
- 骨格が持つもの: 状態の分類（業務テーブルが無い → `missing`、必須 object の不足 → `partial`、廃止 object の残り・未適用 / checksum 不一致の migration → `outdated`、それ以外 → `ready`）、操作の lease（`<製品>_SCHEMA_OPERATIONS` の 1 行。期限切れは奪える。成功で `schema_epoch` を 1 増やす）、台帳（`<製品>_SCHEMA_MIGRATIONS` の MERGE と読み込み）、確認語の検証（DB に触る前に完全一致だけを通す。不一致は 422）、失敗の記録と安全化（`LAST_ERROR_CODE` に ORA コード。ORA-00054 は 409 と `Retry-After: 5`）、`ALTER SESSION SET DDL_LOCK_TIMEOUT`（0〜120 秒）。
- 製品が持つもの: manifest（テーブル・索引などの一覧）と DDL の正本、migration の適用方法、`_status_on`（状態の組み立て）・`_ensure_control_schema`・`_initialize_on`（lease を取った後の手順）、実行中の job の確認、接続 pool、製品名が入る文言と確認語（`RECREATE_<製品>_SYSTEM_TABLES`）。
- 画面側は `@production-ready/system-settings` の `SystemTablesCard`（と `useSystemTablesStatus` / `useInitializeSystemTables`）を使う（NL2SQL の見た目が基準）。製品は API・権限（`canManage`）・確認語・製品名の入る文言・成功時に捨てる cache を渡す。NL2SQL は所有者付きの object 名の表示（`renderObjectName`）、RAG は確認語に加えた確認ダイアログ（`confirmRecreate`）を差し込む。
- 外部キー（製品が `managed_foreign_keys` を渡したときだけ。今は RAG）: 正本の `CREATE TABLE` の FK と USER_CONSTRAINTS を定義（表・列・参照先）で比べ、不足（#505）・削除規則の違い・無効化（DISABLED。#511）を `outdated` にして、状態の `missing_foreign_keys` / `mismatched_foreign_keys` / `disabled_foreign_keys` に出す。作成・更新で、追加・DROP と正本の ADD・ENABLE で直す（参照先のない行があれば `NOVALIDATE`。既存の行は消さない）。参照先のない行が残る FK は `orphaned_foreign_keys` に警告として出す。行の削除は利用者の明示操作だけで行う（`SystemSchemaManagerBase.delete_orphaned_rows`。RAG は `POST /api/settings/database/system-tables/orphaned-rows/delete` の `{constraint_name, expected_orphan_rows}` と CLI の `delete-orphans`。権限は作成・更新と同じ）。確認した件数より増えていたら 409 で削除せず、削除の後に FK を `VALIDATE` する。画面は `SystemTablesCard` の `confirmDeleteOrphans`（確認ダイアログ）を渡したときだけ削除の操作を出す。
- 列（3 製品。#2450・#2458）: 製品は正本の表の列を `managed_columns` で渡す。列は `columns_from_statements` で DDL の正本から作る（`CREATE TABLE` に加えて、後の版の `ALTER TABLE … ADD / DROP / RENAME COLUMN`・`DROP TABLE`・PL/SQL の `EXECUTE IMMEDIATE` の文字列リテラルを順に当てる。RAG / Agent は最新の形の DDL、NL2SQL は版ごとの migration）。製品で別の解析を書かない。`_status_on` は `_missing_columns` で USER_TAB_COLUMNS と比べ、台帳が適用済みでも欠けた列があれば `outdated` にして状態の `missing_columns` に出す（`classify_system_schema_status(columns_current=…)`）。作成・更新の後も欠けていれば `_require_columns_present` が列の名前を出して `SCHEMA_POSTCONDITION_FAILED` で止める（台帳が適用済みなので再試行では直らない。列を補う migration と既存環境の更新手順を足す）。

## 製品間の連携（MCP とサービストークン、#230）

Agent が RAG / NL2SQL を呼ぶときは、呼び先の `POST /api/mcp`（MCP の Streamable HTTP、JSON 応答）を使う。製品同士はコードで依存しない。

外部の MCP クライアント（Claude Code など）は、同じ `POST /api/mcp` を利用者が自分で発行した API キー（`Authorization: Bearer prak_…`）で呼ぶ（3 製品。Agent は業務 Agent へのチャットのツール。#1874）。手順と仕組みは [mcp-api-keys.md](./mcp-api-keys.md)（#1805）。

- サーバー: `pr_backend_core.mcp` の `McpServer` / `McpTool`（入力は Pydantic model、`permissions` はグループをすべて満たす・グループ内はどれか）と `mcp_http_response`。`initialize` / `ping` / `tools/list` / `tools/call` だけを扱う。`listed=False` のツールは `tools/list` に出さず名前でだけ呼べる（製品間の画面の操作の内部のツール。RAG の `rag_readable_documents`。#3252）。`annotations` は `readOnlyHint`（`read_only`）を常に出し、`destructiveHint` / `idempotentHint` / `openWorldHint` は `destructive` / `idempotent` / `open_world` を指定したツールだけ出す（Agent の `agent_cancel_run`。#3240。NL2SQL の `nl2sql_cancel_job`。#3318）。`HTTPException` / `SecurityApiError` は `isError: true` の `structuredContent.error_code` / `message` / `status` になる。長くかかるツールは、client が `tools/call` の `params._meta.progressToken` を渡し `Accept` に `text/event-stream` を含めたときだけ、応答を SSE にして進み具合（`notifications/progress`。10 秒ごとと、ツールが `report_tool_progress(段階の名前)` で知らせたとき）を送り、最後に結果を送る（#1864。RAG の `rag_search` は回答フローの工程の名前を知らせる）。progressToken の無い呼び出しは今までどおり JSON で応答する。
- 認証: 呼び出し元は `pr_system_settings.auth.service_token.issue_service_token` で `sub` = 利用者の `user_uuid`、`aud` = 呼び先（`rag` / `nl2sql`）の短命の token（HS256、既定 60 秒）を作り、`Authorization: Bearer` で送る。鍵は共通 `.env` の `PLATFORM_SERVICE_TOKEN_SECRET`（32 文字以上。空なら 503）。
- 呼び先は `authorize_request(..., service_token_paths={"/mcp"}, service_token_audience="<製品>")` を渡す。その path では Cookie の代わりに token の利用者を `principal_for_worker` で組み立てる（現在のロール・権限・対象範囲を使い、無効・初回パスワード変更待ちの利用者は 403）。Cookie を使わないので CSRF は照合しない。claims（`run_id` など）は `request.state.service_token_claims`。
- ツールの権限と対象範囲（検索・回答プロファイル / KB / 業務プロファイル / DeepSec）は、画面と同じ service 層で判定する。
- 呼び出し元の用途による範囲（#1379）: Agent は業務 Agent の定義でデータの範囲（使える RAG の検索・回答プロファイル /
  NL2SQL の業務プロファイル）を設定したとき、その接続（= `aud`）の ID を任意の claim `profile_ids` に入れる
  （`issue_service_token(..., profile_ids=...)`。範囲が未設定なら claim を付けない。名前と形の正本は
  `pr_system_settings.auth.service_token` の `PROFILE_SCOPE_CLAIM`）。claim は 1〜50 件の 128 文字以内の ID の一覧で、
  形が違う token は 401（壊れた範囲を「範囲なし」として通さない）。
  - 呼び先は「利用者の権限 ∩ claim」で判定する（`service_token_profile_scope(request)` と `narrow_to_profile_scope`）。
    claim は利用者の権限を広げない。claim の無い呼び出しの判定は変えない。
  - claim の範囲外のプロファイルを指定した呼び出し（と、範囲があるのにプロファイルの指定が無い呼び出し）は 403
    （`structuredContent.error_code` が `PROFILE_SCOPE_FORBIDDEN`。`PROFILE_SCOPE_FORBIDDEN_CODE`）。一覧・推薦のツールは
    範囲で絞る。RAG はナレッジベースも範囲のプロファイルの参照先に絞り、文書を読むツールも範囲の中だけを読む
    （RAG の `docs/rag-architecture.md`「Agent からの呼び出し（MCP）」、NL2SQL の `docs/security-rbac-deepsec.md`
    「業務 Agent のデータの範囲」）。
  - 呼び出し元（Agent の組み込み Runtime）も同じ範囲を tool handler で強制する（多層の防御。Agent の
    `features/agent/data_scope.py`）。呼び先の強制は、Runtime の不具合・新しいツールの追加漏れ・Runtime を通さない
    呼び出しでも範囲を越えさせないためのもの。
- 呼び出しのきっかけ（#1986）: Agent は Run のきっかけ（`run_facts.run_source`。`user` / `evaluation` /
  `mcp` / `replay`）を任意の claim `run_source` に入れる（`ToolInvocationContext.run_source` → `McpConnectionClient._service_token`。
  Run の外の呼び出し〔画面の単発のツール呼び出しなど〕は付けない）。呼び先は**見え方だけ**に使う: NL2SQL は MCP の入口で
  claim `run_source` = `evaluation` の実行を実行履歴の種類「品質評価」、それ以外のサービストークンと API キーの呼び出しを
  「MCP」として行に残し、実行履歴の既定（品質評価以外）で品質評価を隠す（`JobOrigin.for_mcp`）。権限・対象範囲・
  利用量の判定には使わない。claim は `PLATFORM_SERVICE_TOKEN_SECRET` で署名した token にしか載らず、API キー
  （`prak_…`）の呼び出しは claim を持たないので、外部の MCP クライアントは品質評価と偽れない（偽れても変わるのは
  一覧の種類だけ）。RAG は claim を読まない（MCP・品質評価の実行は RAG の実行履歴に残さない）。local の認証
  （token を検証しない）は claim が無く、すべて「MCP」になる。
- 呼び先がブラウザで開く短命の URL を返すとき（RAG の図の根拠 #1311・根拠の原文のプレビュー #2389）は、呼び出し元の画面の
  操作だけに限るため claim の `purpose`（`figure_url` / `source_preview`）を確かめ、Run の中でモデルが呼んだ token（`purpose`
  無し）では作らない。URL のトークンはサービストークンの鍵そのものでは署名せず、`PLATFORM_SERVICE_TOKEN_SECRET` から
  HKDF-SHA256 で用途の印を変えて導いた鍵で署名し、利用者・対象・版・期限（5 分以内）に縛る（用途ごとに鍵が違うので、別の
  用途のトークンとしては使えない）。トークンは URL の path に置いてアクセスログで伏せ、読むたびに利用者の今の権限と対象の版を
  確かめ直す（RAG の `app/rag/signed_url.py`・`figure_url.py`・`source_preview.py`、`app/api/routes/figures.py`・
  `source_previews.py`）。発行済みの URL を即時に失効させる仕組みは持たず、権限の変更は次に読むときから効く（期限と読むたびの
  確かめ直しが契約。RAG の `docs/rag-architecture.md`「署名つきの URL の契約」。#3252）。
- 呼び出しの取消（#2691）: 呼び先の `mcp_http_response` は、読み取り専用（`McpTool.read_only`）のツールの `tools/call` の処理中に client の切断（`http.disconnect`）を受けたら、ツールの task を取り消して 499 を返す（SSE の進み具合の応答も次の通知を待たずに止める。ログ `mcp_tool_call_cancelled`）。切れた接続の応答を後で受け取る仕組み（SSE の再開）を持たないので、切断を取消と同じに扱う。書き込みのツールは止めずに最後まで実行する。呼び出し元（Agent）は Run の取消で接続を切り（書き込みのツールは送った後は切らない）、独自の取消の API・`notifications/cancelled` は使わない。呼び先の worker で動くジョブ（NL2SQL の `nl2sql_query` のジョブ）は切断では止まらないため、呼び先はジョブを止めるツール（`nl2sql_cancel_job`。冪等で、終わったジョブは `result` で返す）を持ち、Agent は取り消した Run が始めたジョブにだけそれを送る（#3318）。
- 呼び出し元の再試行と timeout（#854。Agent の `McpSession`）: 送信前の失敗（接続できない・接続の timeout）と 429 / 503 はどのメッセージも、502 / 504 は状態を変えない手順（`initialize`・`tools/list`）と読み取り専用（`annotations.readOnlyHint=true`）のツールの `tools/call` だけ、上限付きの指数 backoff + jitter（`Retry-After` に従う）で再試行する。書き込みのツールは呼び先に届いた可能性がある失敗（読み取りの timeout・500 / 502 / 504）では再送しない。1 回のツールの呼び出し全体は接続の timeout に収める。呼び先は、副作用のあるツールに `readOnlyHint` を付けない・429 / 503 は処理を始める前にだけ返す（`Retry-After` を付けてよい）こと。
- ツールの契約（名前・`inputSchema`・`outputSchema`（`McpTool.output_model`。#250）・`annotations`）の正本は `platform/contracts/mcp/<製品>-tools.json`（#248。Agent の外部向けのツールは `agent-tools.json`。#1874）。3 製品の `tests/test_mcp_contract.py` が実装と一致を、Agent の `tests/test_product_mcp.py` が組み込み Skill の使うツールが契約にあることと、契約どおりの fake の MCP（`tests/mcp_support.py`）の引数が契約（必須の項目・項目の名前）と合うことを確かめる。ツールを変えたら呼び先の製品で `UPDATE_MCP_CONTRACT=1 uv run pytest tests/test_mcp_contract.py` で契約を書き直し、Agent のテストも通す。

## 「AI 活用」の下の機能のデータの見え方（#1889・#1954・#3244）

3 製品のサイドナビの「AI 活用」の下の機能（RAG: チャット・RAG 検索・実行履歴、NL2SQL: チャット・SQL 生成・SQL 直接実行・SQL から質問・実行履歴、Agent: チャット・承認・実行履歴）は、**system_admin は全員のデータ、それ以外の利用者は自分のデータだけ**を、一覧・詳細・操作で扱う。判定は `pr_system_settings.auth.visibility` の関数に 1 つだけ置き、製品はそれを使う。

- system_admin は SYSTEM_ADMIN のロールを持つ利用者（構成管理者 `system_admin` と local のローカル利用者を含む）。`can_view_all_users_data(principal)` が判定する。製品の管理権限（`agent.admin`・`rag.feedback.manage` など）やメニュー権限は、全員を見る理由にしない。
- 一覧は backend の SQL / store の条件で絞る（`owner_filter_user_uuid(principal)`。None は絞らない）。画面で隠すだけにしない。
- 詳細・操作は `can_access_user_data(principal, owner_user_uuid)`。他人のデータは 404（`OWNER_NOT_FOUND_STATUS`。存在を漏らさない）。持ち主の分からない古いデータは system_admin だけが扱える。
- **system_admin が他の利用者のデータにできるのは、閲覧と運用の操作だけ**（#3244。#1928 を置き換え）: 閲覧は一覧・詳細・処理の経過・実行履歴、運用の操作は停止・取り消し・削除と、Agent の承認待ちの判断（承認・却下）と再開（続きは Run を実行した人の権限で動く）。**新しい実行・内容を足す操作は持ち主だけ**: 続きの質問・SQL の実行・再実行・回答前の確認や明確化への回答・回答の評価・会話の名前の変更は、system_admin でも他の利用者のデータには 403（`can_add_user_content`・`OTHERS_CONTENT_FORBIDDEN_STATUS` / `OTHERS_CONTENT_FORBIDDEN_MESSAGE`）。持ち主の分からないデータ（記録の無い古い行）には誰も足せない。外部の MCP（API キー）も同じで、ツールのエラーの code は 3 製品で `OTHERS_CONTENT_FORBIDDEN_CODE`（`OTHERS_CONTENT_FORBIDDEN`）。会話の添付（platform の `/api/chat-attachments`）は、一覧が閲覧（見える会話なら返す）、会話への添付が内容を足す操作（製品の `conversation_owner` が返す持ち主の UUID で `can_add_user_content` を判定し、他人・持ち主の分からない会話は 403）。system_admin の権限・対象範囲（業務プロファイル・検索・回答プロファイル・業務 Agent・NL2SQL の system_admin 用の接続）で作った回答・SQL・ツールの出力が持ち主の会話に入り、持ち主が権限の外の内容を見るのを防ぐ。調べ直すときは system_admin が自分の会話で再現する（Agent の replay のように system_admin 自身の新しい Run・会話を作る操作は可）。本人として操作する仕組み（impersonation）は持たない。
- **保存した根拠は、出すたびに見る人の今の権限で確かめ直す**（#3252。RAG のチャットの会話・実行履歴・保存された回答と、Agent の Run・会話）: 今見られない文書の根拠は抜粋・場所・文書名を返さず、件数（`withheld_evidence_count`）だけを返し、画面は共有 UI の `EvidenceWithheldNote`（「権限の無い根拠 N 件」）で出す。system_admin の閲覧も system_admin の今の権限で確かめる。回答の本文は保存した文のまま（既知の限界）。仕組みは RAG の `docs/rag-architecture.md`「保存した根拠の再表示」、Agent の `docs/security-rbac.md`「保存した根拠の再表示」。
- **運用の操作は actor を記録し、持ち主は変えない**: 停止・削除・承認の判断・監査には操作した人を残す。すでに持ち主の会話に入っている system_admin の発話・Run（#3244 より前）は消さず、送った人の表示名を添えて出す（応答の `sent_by_owner` / `sender_display_name`、画面は `ChatTurn` の `questionSender`・`ChatUserMessage` の `senderLabel`）。
- **画面**: system_admin が他の利用者の会話を開いたときは、入力欄・続きの操作（再実行・回答・評価・名前の変更）を出さず、閲覧だけの案内を出す（停止・削除は出す）。
- system_admin の一覧には「利用者」の列（共通の表示名。`AuthService.display_identities`。#1696）を出し、自分だけを見る利用者には出さない。
- 「AI 活用」の外（改善・運用の監査・利用状況・フィードバック管理・品質評価など）は、この規則の対象外で、それぞれの画面の権限のまま。製品ごとの割り当て（例外を含む）は各製品の権限の文書（RAG `docs/agents/ui.md`、NL2SQL `docs/security-rbac-deepsec.md`、Agent `docs/security-rbac.md`）に書く。

## チャットの処理の段階の配信（#1359）

チャットの回答の処理の段階（画面の `ChatProgress`）は、3 製品とも `pr_backend_core.chat_progress` の追記型のイベントで記録・配信する。段階の一覧の snapshot を置き換えて配らない（古い snapshot が新しい表示を上書きしても、受け取る側が見分けられない）。

- **契約**: `platform/contracts/chat-progress/chat-progress-events.json`（`tests/test_chat_progress_contract.py` が model から生成し一致を確かめる。形を変えたら `UPDATE_CHAT_PROGRESS_CONTRACT=1`）。段階のイベント（`seq`〔対象ごとに 1 から連続〕・`target_id`・`attempt`・`step_id`・`kind`・`status` pending / running / done / failed / skipped・`started_at` / `finished_at`・`detail`・`params`）と終端のイベント（`status` done / failed / cancelled）。段階のイベントはその段階の今の状態の全体を持つ。名前（文言）は入れず、画面が製品の段階の定義（`kind` → i18n）で付ける。`detail` は件数などの短い補足だけ（SQL・ORA コード・例外の種類は入れない）。
- **記録**: `ChatProgressRecorder(target_id, attempt=, events=保存済み, sink=)`。製品は「どこで `declare`（待機中として先に出す。並びを決める）・`start`（`exclusive=True` でほかの実行中を完了）・`finish`・`update` するか」と、終わりの `complete(done / failed / cancelled)` だけを書く。番号・時刻・状態を戻さない（完了した段階を実行中に戻す呼び出しは何もしない）・終端の確定は記録が持つ。保存は製品の既存の保存（RAG: 回答のメッセージの `progress_json`、NL2SQL: ジョブ、Agent: Run）に `events` を書く。保存済みの `events` から作り直せば続きの番号から記録する（引き継いだ実行は `new_attempt`）。補足・値の引数は既定の `UNCHANGED` で前の値のまま、`None` で消す。
- **配信**: polling は `GET …/progress?since=<seq>` で `chat_progress_page`（`ChatProgressPage`: `events` / `last_seq` / `terminal`）、SSE は `GET …/progress/stream` で `chat_progress_sse_response(fetch, since=, last_event_id=)`（`id:` に `seq`、`event: chat_progress`、イベントの無い間は 10 秒ごとに `event: heartbeat`、終端を送ったら閉じ、終わった対象を続きから求められたら 204）。SSE は保存先を読み直して送る（1 秒ごと）ので、別の worker・プロセスが記録していても届く。読み直し・heartbeat・1 本の接続の最長（600 秒）の間隔は `configure_chat_progress_sse(...)`（起動時・テスト。テストは `restore_chat_progress_sse` で戻す）か引数で変える。イベントの値の無い任意の項目は、API の応答・保存・SSE のどれでも省く（null を出さない。契約の `absent_fields: omitted`）。応答の header は `X-Accel-Buffering: no`（Nginx の buffering を切る）。対象の権限の判定（404 / 403）は製品が `fetch` の前に行う。
- 画面は `@production-ready/ui` の `useChatProgressStream`（[components-reference.md](./design-system/components-reference.md)「useChatProgressStream」）。

## チャットの会話の履歴を LLM に渡す量（#1818）

3 製品のチャットは、同じ会話の前の往復を LLM に渡して続きの質問（「それを多い順に」など）を解く。渡す量と書き方は `pr_backend_core.chat_history` に 1 つだけ置き、製品は 1 往復の中身だけを決める。

| 製品 | 1 往復の中身（`ChatHistoryTurn` の部分） | 使う所 |
|---|---|---|
| RAG | 質問（`question`）・回答（`answer`。出典は渡さない） | 質問の書き換え（`history_rewrite`） |
| NL2SQL | 質問（回答前の確認で確かめた文があればそれ）・生成した SQL（`sql`）・状態（`status`）。結果の表は渡さない | SQL の生成（全方式）と回答前の確認（JSON の `conversation_history`） |
| Agent | 質問・回答（`kind="answer"` の成果物）・使ったツールの名前（`tools`。結果は渡さない） | 組み込み Runtime の入力（質問の前の利用者のメッセージ） |

- **予算（共通の設定。`platform/.env`）**: `PLATFORM_CHAT_HISTORY_MAX_TURNS`（既定 **8** 往復。0〜50、0 で渡さない）・`PLATFORM_CHAT_HISTORY_MAX_CHARS_PER_TURN`（既定 **4,000** 文字。200〜32,000）・`PLATFORM_CHAT_HISTORY_MAX_CHARS`（既定 **16,000** 文字。1,000〜200,000）。3 製品が同じ値を読む。
- **詰め方**: 新しい往復から順に、1 往復を上限まで切り詰めて詰め、ターン数か合計の文字数を超えるところで止める（入らない古い往復は落とし、途中を飛ばして古い往復を入れない）。1 往復の切り詰めは、短い部分（質問・ツールの名前・状態）を全文で残し、長い部分（回答・SQL）の末尾を「…（以下省略）」にする。
- **未信頼のデータ**: 前の往復は `<conversation_history untrusted="true"><turn><question>…</question>…</turn></conversation_history>` に入れ、中のタグに見える `<`（`</`・`<a`・`<!`・`<?`）を `&lt;` にする（囲みを閉じて外へ指示を書けない。SQL の `<`・`<>`・`<=` は変えない）。指示ではないことを `CHAT_HISTORY_UNTRUSTED_NOTE` で伝える。Agent も前の回答を assistant の発話としては渡さない（回答はツールの結果＝文書・SQL の結果に由来する文を含む）。
- **値の根拠**:
  - 方式は主要な実装の「直近を残して古いものを落とす」trimming にそろえた: OpenAI Agents SDK の Session の trimming（cookbook「Context Engineering - Short-Term Memory Management with Sessions」の `TrimmingSession`。1 ターン＝利用者の 1 発話とその後の応答・ツール。既定 `max_turns=8`）、LangChain の `trim_messages(strategy="last")` と token buffer memory（`ConversationTokenBufferMemory` の既定 `max_token_limit=2000`）、LlamaIndex の memory の token 上限。ターン数だけでは長い回答で入力が膨らみ、token だけでは短い往復が際限なく続くので、両方で上限を掛ける。
  - 8 往復は OpenAI の trimming の既定と同じ。続きの質問の省略を補うのは直近の数往復で足り、古い往復ほど現在の質問から外れた指示に引きずられやすい（cookbook の指摘）。
  - 合計 16,000 文字は、日本語はおおむね 1 文字が 1 token 前後なので約 16k token。128k の文脈の 1 割強に留め、検索の根拠・スキーマ・ツールの結果に文脈を残す（Anthropic の context engineering の「文脈は有限の資源で、少ない高信号の token にする」）。1 往復 4,000 文字は、長い回答・SQL があっても 4 往復以上が入る値。文字数で数えるのは、モデルごとに tokenizer が違っても 3 製品で同じ値を使うため。
  - ツールの結果・SQL の結果の表は渡さない（Anthropic の context editing が古いツールの結果から消すのと同じ。結果は大きく、個人情報・権限の範囲を含みうる）。
- **compaction（要約）はしない**: 古い往復の要約（OpenAI の `SummarizingSession`、OCI の会話の compaction）は、1 回ごとに LLM の呼び出しが増え、要約の誤りが後に残るため今回は入れない（残課題）。
- テストは `platform/packages/backend_core/tests/test_chat_history.py`（予算・切り詰め・0 で無効・タグの無害化）と、各製品のチャットのテスト。

## チャットの「会話の履歴」の期間（#2340）

4 つのチャット（RAG チャット・NL2SQL チャット・NL2SQL DB 管理チャット・Agent チャット）の左の「会話の履歴」は、最後の更新が直近 N 日の会話だけを出す。期間の計算は `pr_backend_core.chat_history_window` に 1 つだけ置く。

- **値（共通の設定。`platform/.env`）**: `PLATFORM_CHAT_HISTORY_DAYS`（属性 `chat_history_days`。既定 **90** 日、0〜3,650。**0 は期間で絞らない**）。3 製品が同じ値を読む。
- **表示の絞り込みで、会話は消さない**: 一覧の API（RAG `GET /api/chat/conversations`、NL2SQL `GET /api/nl2sql/chats`・`GET /api/nl2sql/db-admin-chat/conversations`、Agent `GET /api/threads`）だけが絞る。期間の外の会話も、ID で開く API（会話の詳細・実行の詳細の「会話を開く」・直接のリンク）では今のまま開け、続けて質問すると最後の更新が進んで一覧に戻る。データの見え方（system_admin は全員・それ以外は本人。backend-standard「「AI 活用」の下の機能のデータの見え方」）は変えない。
- **件数とページを同じ条件で**: 期間の条件は件数（`total`）と 1 ページの両方に掛ける（ページ送りの件数がずれない）。応答は共通の `ChatHistoryPage`（`Page` に `history_days` を足したもの。0 は絞っていない）。
- **最後の更新**: RAG は会話の `updated_at`（質問・名前の変更で進む）、NL2SQL のチャットは会話のジョブ（最初のターン・続きのターン）の記録の更新時刻の最も新しいもの（会話の ID〔最初のジョブの ID〕を、直近 N 日に更新したジョブの `conversation_id` の中から探す。会話の最初のジョブに書き足さないので、投入の往復は増えない。並びは今までどおり）、DB 管理チャットは会話の `UPDATED_AT`（ターンを足す・名前を変えると進む）、Agent は会話の `updated_at`（最後の Run）。Oracle の `SYSTIMESTAMP` で書く列は `SYSTIMESTAMP - NUMTODSINTERVAL(:days, 'DAY')` と DB の時刻で比べ、アプリが UTC の時刻を書く列（RAG）とメモリの一覧は `chat_history_cutoff` の時刻で比べる。
- **画面**: 一覧の応答の `history_days` から `@production-ready/ui` の `chatHistoryWindowLabels` が「最近 N 日」（`ChatLayout` の `historyCaption`）と 0 件の文「最近 N 日の会話はありません」を作る（4 つのチャットで同じ文言）。
- テストは `platform/packages/backend_core/tests/test_chat_history_window.py` と、各製品の一覧の API のテスト（RAG `tests/test_chat_api.py`、NL2SQL `tests/test_chat_history_days.py`、Agent `tests/test_builtin_runtime.py`）。

## チャットの添付（会話の期間だけ使う作業ファイル。#2517）

3 製品のチャットで、利用者は手元のファイル（PDF・Word・PowerPoint・Excel・CSV・テキスト・画像）を会話に添付して質問できる。保存・解析・見え方・期限・読むツールの中核は `pr_system_settings.chat_attachments` に 1 つだけ置き、製品は会話への結び付けと、添付の使い方（Agent はツール、RAG は根拠、NL2SQL は SQL の表）だけを持つ。

- **使うか（製品ごと。既定は無効）**: 各製品の「運用設定 › チャットの添付」（メニュー権限 `menu.settings_chat_attachments`、API `GET` / `PUT /api/settings/chat-attachments`、画面は `@production-ready/system-settings` の `ChatAttachmentSettingsPage`）。保存先は共通の表 `PLATFORM_CHAT_ATTACHMENT_SETTINGS` の製品の行（`AuthService.chat_attachment_settings`。10 秒の cache、読めなければ無効）。無効の製品は添付の API が 403（`CHAT_ATTACHMENT_DISABLED`）、入力欄に添付の操作を出さず、会話に残る添付もツールに渡さない。
- **上限（共通の設定。`platform/.env`）**: `PLATFORM_CHAT_ATTACHMENT_TTL_HOURS`（既定 24。1〜720。最後に添付・送信に使ってからの時間）・`PLATFORM_CHAT_ATTACHMENT_MAX_BYTES`（既定 20 MiB。1〜100 MiB。Nginx の `client_max_body_size` に収まる）・`PLATFORM_CHAT_ATTACHMENT_MAX_FILES`（既定 10。1〜50。1 つの会話と送る前の 1 回）。解析の上限（文字数・行数・列数・zip の展開）は `limits.py` の定数。
- **保存**: Oracle AI Database の `PLATFORM_CHAT_ATTACHMENTS`（原本の BLOB、解析の本文 `PARSED_JSON`、索引 `OUTLINE_JSON`）。外部の保存先（アップロード保存先・Object Storage）は使わない。DDL は共通の DDL（`auth.migrations`）。認証の表（`PLATFORM_AUTH_TABLES`）には入れず、未作成でも添付の API だけが 409（`CHAT_ATTACHMENT_SCHEMA_REQUIRED`）。製品の全再作成で消さない（`CHAT_ATTACHMENT_TABLES` を保持する表に足す）。
- **期限**: 期限の切れた行は添付の操作（添付・一覧・送信）のたびに上限の件数ずつ消す（スケジューラーを持たない）。会話の削除の API はその会話の添付も消す。
- **見え方**: 「AI 活用」の規則（`auth.visibility`）。持ち主は会話の持ち主（会話の前は添付した人）、`UPLOADED_BY_USER_UUID` は添付した人。他人の添付は 404。
- **API（3 製品で同じ。`build_chat_attachment_router`）**: `POST /api/chat-attachments`（multipart `file`・任意 `conversation_id`）・`GET /api/chat-attachments?conversation_id=`（無ければ送る前の添付。応答に上限と `enabled`）・`DELETE /api/chat-attachments/{id}`。チャットの送信は `attachment_ids` を受け、会話の ID が送信の後に決まる製品は `prepare_attachments`（確かめる）→ 会話を作る → `bind_references`（結び付ける）、先に決まる製品は `attach_to_conversation`。発話の記録には `ChatAttachmentReference`（名前・種類）を残す。
- **解析**: 拡張子と先頭の bytes で種類を決める（合わなければ 415。旧形式の binary は受けない）。PDF は `pypdf` で 1 頁 = 1 節、Word は見出しで節、PowerPoint は 1 スライド = 1 節、CSV は「1 行 = 列名 + 値」、Excel は RAG の取込と同じ共通の読み方（`pr_backend_core.spreadsheet.read_excel`。表は「1 行 = 列名 + 値」、帳票は「項目・値」の表、手順書と表頭より上の行は節、1 シートの複数の表は表ごとのシート。行と節に元の行番号・セル範囲、帳票の項目にラベルと値のセル。#2544）、画像は製品が渡す読み取りの関数（既定の Vision モデル。指示は `IMAGE_READING_PROMPT`）。Office は zip と `defusedxml` で読み、マクロは読まず実行しない（警告 `macros_not_executed`）。製品の解析（RAG の `rag_parser_core`・前処理のサービス）は使わない（製品同士は依存しない）。Excel の読み方の版（`EXCEL_PARSER_VERSION`）は解析の結果の JSON に置き、古い版で解析した Excel の添付は、使うときに保存した原本から 1 回だけ解析し直して `PARSED_JSON` / `OUTLINE_JSON` を置き換える（解析し直せなければ古い結果のまま、警告 `excel_reparse_failed`。列は足さない）。
- **読むツール（名前と結果の形は Issue #2517 の契約）**: `chat_attachment_list`・`chat_attachment_read_section`（8,000 文字ずつ）・`chat_attachment_read_rows`（200 行まで）・`chat_attachment_find`。`ChatAttachmentToolset` が会話の添付だけを読む。モデルにはファイル全体を渡さず、索引（`index_lines`）だけを指示に入れる。
- **製品の使い方**: Agent は会話に添付がある Run にだけ 4 つの読むツールを Control Plane のツールとして渡す（`tool_registry.invoke` を通す。添付を読んで答えた回答は最終の検証を `attachment_evidence` で `skipped`）。RAG は会話の添付の節・行を質問との関連（rerank、無ければ文字の一致）で選び、検索の根拠と同じ chunk（`document_id` は `attachment:<ID>`、metadata `chat_attachment_id`）にして回答の検索の結果に足す。NL2SQL は会話の添付の Excel・CSV のシートを WITH 句の行の表 `ATTACHMENT_<n>`（`JSON_TABLE` に行の JSON を CLOB の bind で渡す。動的な `CREATE TABLE` はしない）にし、SQL の生成（Enterprise AI Direct だけ。Select AI の方式は添付の表を知らないので、添付を付けた送信は止める）に表の名前・列・型・先頭の行を渡し、検証は列の形だけの CTE、実行は本当の CTE を前に付ける。
- **画面**: `@production-ready/ui` の `ChatComposer` の `attachments`（「ファイルを添付」・ドロップ・貼り付け・外す）と `useChatComposerAttachments`（製品は API の関数と文言だけを渡す）、発話の下の `ChatTurn` の `questionAttachments`、根拠のカードの `sourceUnavailable`（添付の根拠は原文のプレビューを開かない）。
- テストは `platform/packages/system_settings_backend/tests/test_chat_attachments.py`・`platform/packages/ui/tests/chat-attachments.test.tsx`・各製品のチャットの添付のテスト。

## 内部の HTTP と環境のプロキシ（#852）

OCI に届くために `HTTP_PROXY` / `HTTPS_PROXY`（社内プロキシ）を設定した環境でも、同じマシン・private network の
サービスへの HTTP は環境のプロキシを通さない。プロキシは 127.0.0.1 などへ届けられず `502 cannotconnect` などを返し、
未起動のサービスへの「未到達」が「サービスの応答した失敗」に見えてしまう（#852 で RAG の pipeline の stage が
in-process へ縮退せず、MCP の `rag_search` が止まった）。

- httpx の client（`Client` / `AsyncClient`）は、宛先の URL を `pr_backend_core.internal_http.http_client_options(url)`
  に通した引数で作る: `httpx.Client(timeout=..., **http_client_options(url))`。宛先が内部（loopback `127.0.0.0/8`・`::1`・
  `localhost` / `*.localhost`・`0.0.0.0`・link-local・RFC 1918 / ULA の private・`*.local` / `*.internal`・unix socket）なら
  `trust_env=False`（環境のプロキシ・`SSL_CERT_FILE`・`.netrc` を読まない）、外部（OCI・外部の MCP など）なら空で、
  今までどおり環境のプロキシを使う。DNS は引かず、名前の形と IP の値だけで判定する（`is_internal_url`）。
- 対象: RAG の前処理・parser・外部 parser・pipeline の stage・サービス管理の health・parser の readiness・trace の送信・
  評価 / 検証 / 負荷の CLI、Agent の MCP 接続（RAG / NL2SQL / 外部）と OAuth の token・trace の
  送信。宛先が設定値の接続も、内部の宛先のときだけプロキシを外す。OCI Enterprise AI など外部だけへの client は対象外。
- 多重の防御として、各製品の `scripts/start-backend.sh` は既存の値を残したまま `NO_PROXY` / `no_proxy` に
  `localhost,127.0.0.1,::1` を足して backend を起動する（起動の readiness の確認は #781 の `curl --noproxy "*"`）。
- 内部の宛先をホスト名（例: VCN の DNS 名）で設定する場合は、名前からは判定できないため `NO_PROXY` に加える。RAG の
  pipeline の stage は、内部と判定できない宛先で環境のプロキシを通り 502 / 504 が返ったときだけ、プロキシの応答として
  未到達（in-process へ縮退）にする。サービス自身が応答した失敗は今までどおり縮退せず止める。

## 統一レスポンス envelope

```jsonc
// 成功
{ "data": { /* ... */ }, "error_messages": [], "warning_messages": [] }
// 失敗（HTTPException / 検証 / 未処理例外を統一）
{ "data": null, "error_messages": ["..."], "warning_messages": [] }
```

`X-Request-ID` ヘッダを常に付与（受信値を検証し、無ければ発行）。フロントの共通エラーハンドリングはこの形を前提にできる。

### 一覧のページング（#1266）

一覧の endpoint は `pr_backend_core.api` の部品で受け取り・返す。製品で `Page(...)` を手で組み立てない、limit を黙って clamp しない、カーソルの codec と `OFFSET … FETCH NEXT` の文を書き写さない。

| 型 | query の受け取り | 応答 | 組み立て |
|---|---|---|---|
| offset 型（任意のページへ移る一覧） | `paging: Annotated[OffsetParams, Depends(offset_params(default=50, max_limit=200))]`（`limit` 1..max は `Query(le=)` で 422、`offset` 0 以上） | `Page[T]`（`items` / `total` / `limit` / `offset` / `has_next`） | DB のページ: `paginate(items, total=, limit=paging.limit, offset=paging.offset)`。メモリ上の全件: `paginate_slice(items, paging)`。縮退の fallback: `empty_page(paging)` |
| カーソル型（「前へ / 次へ」と「さらに読み込む」） | `paging: Annotated[CursorParams, Depends(cursor_params(default=50, max_limit=100))]`（`cursor` は最初のページで None、最大 512 文字） | `CursorPage[T]`（`items` / `next_cursor: str \| None` / `total: int \| None`）。製品の付加情報（件数の内訳・version）は継承して足す | `cursor_page(items, next_cursor=, total=)`。`next_cursor` は続きが無ければ `None`（空文字は使わない） |

- カーソルの文字列は `encode_cursor(dict)` / `decode_cursor(cursor, required=(...))`（base64url の JSON）か、offset をそのまま続きの印にする `encode_offset_cursor` / `decode_offset_cursor` / `next_offset_cursor`。壊れたカーソルは `InvalidCursorError` で、router が 422 にする。
- Oracle の `OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY` は `offset_fetch_clause()`（bind 名は `offset_bind` / `limit_bind` で変えられる）と `offset_fetch_binds()`。
- 画面の一覧の既定の `limit` は共通の `DEFAULT_PAGE_LIMIT`（25 件。frontend の `DEFAULT_PAGE_SIZE` と同じ。#1952）。画面の一覧の endpoint は `offset_params(default=DEFAULT_PAGE_LIMIT, …)` にし、製品で 10 などの数を書かない。
- 権限管理の「利用できる対象」の一覧の上限は共通の `ACCESS_TARGET_PAGE_LIMIT_MAX`（100）。
- frontend は共通の `OffsetPagination` / `CursorPagination`（ルートの `docs/agents/ui.md`「読み込み中・一覧・ページング」）。

### 入力の検証エラー（422）の文（#1065）

FastAPI / Pydantic の検証エラー（`RequestValidationError`）は、3 製品とも `pr_backend_core.api.validation` で整形する（共通の handler は `install_exception_handlers`。RAG は `validation_error_response`、NL2SQL は problem 契約の `api_problem_response` から同じ関数を使う）。

- `error_messages` は利用者向けの日本語の文だけにする。
  - 自前の検証（`ValueError` / `assert` / `PydanticCustomError`）の日本語の文は、位置も `Value error, ` の接頭辞も付けずにそのまま出す。欄の名前は文に書く（例「ケースの id が重複しています。」）。英語の文の `ValueError` は利用者に見せず、位置と「入力内容を確認してください。」にする。
  - Pydantic の組み込みのエラーは `type` ごとの日本語の文に、利用者が書いた JSON の位置（`cases[0].query`。`body` / `query` などの出どころと union の候補の型の名前は外す）を前に付ける（例 `cases[0].query: 必須の項目です。入力してください。`）。
- `error_code` は `REQUEST_VALIDATION_FAILED`、`problem` は NL2SQL の problem 契約と同じ形（`title` / `status` / `detail` / `code` / `request_id` / `retryable` / `field_errors`）。
- `problem.field_errors[]` は欄ごとに `pointer`（JSON Pointer）・`code`（Pydantic の type）・`message`（位置を含まない文）・`location`・`raw_location`（`body.cases.0.query`）・`raw_message`（Pydantic の原文）を持つ。画面は `message` を欄の横に、`raw_*` を「詳細」（`ApiErrorBanner`）に出す。入力値（`input`）は応答に含めない。
- MCP のツールの引数の誤り（`MCP_TOOL_ARGUMENTS_INVALID`）の `details.errors[]` も同じ整形（`loc` / `message` / `type` / `raw_message`）にする。

## 依存方法（dev は path source）

各 backend の `pyproject.toml`:

```toml
dependencies = ["production-ready-backend-core"]

[tool.uv.sources]
production-ready-backend-core = { path = "../../platform/packages/backend_core", editable = true }
```

> path source 変更時は **`uv lock` 再生成**。monorepo なので CI もローカルも同じ相対パス（`../../platform/…`）で解決する。

## 共通の診断ログ

API・worker・RAG の独立微サービス・browser の共通 schema、JST 時刻、相関、例外の安全化、HTTP summary と運用方法は [logging-standard.md](./logging-standard.md) を正本とする（#858）。`configure_logging` を各 process の bootstrap で呼び、製品の自由な handler / format を追加しない。

## 設定（`.env`）の変数名（#211）

規則の要点（共通の `platform/.env` と製品の `backend/.env` の分け方）はルートの `docs/agents/naming.md`（設定（`.env`）とデータベース object の命名）。

- 環境変数名は Settings の属性名から `pr_backend_core.config.product_settings_config` が決める。共通の属性（`PLATFORM_SETTING_FIELDS`）は `PLATFORM_` + 属性名（先頭の `app_` は除く）、それ以外は製品の接頭辞（`RAG_` / `NL2SQL_` / `AGENT_`）+ 属性名（接頭辞で始まる属性には重ねない）。
- 3 製品共通の設定を足すときは、`PLATFORM_SETTING_FIELDS` と `platform/.env.example` に加える。
- 既存環境の `.env` は `platform/scripts/migrate_env_to_platform.py` で移す。ユーザー・ロール・セッションのテーブルは `PLATFORM_*` へ移した（#212）。
- 共通 `.env` の場所は `PLATFORM_ENV_FILE`、製品の `backend/.env` の場所は `<接頭辞>ENV_FILE`（`RAG_ENV_FILE` / `NL2SQL_ENV_FILE` / `AGENT_ENV_FILE`）で変えられる（`platform_env_file` / `product_env_file`）。
- **テストは手元の `.env` を読まない**（#1972）: 3 製品の `tests/conftest.py` は、`app` を import する前に `pr_backend_core.config.isolate_env_files_for_tests("<接頭辞>")` を呼ぶ（共通・製品の `.env` を無い場所に向ける。CI と同じ条件になり、開発機の構成管理者の資格情報・共有の開発の DB に左右されない）。`.env` の読み書きを確かめるテストは、製品の module の `PLATFORM_ENV_FILE` / `BACKEND_ENV_FILE` を `tmp_path` に差し替える。実 Oracle の結合テストの接続は環境変数（`PLATFORM_ORACLE_*`）で渡す（CI の `oracle-integration.yml` は `platform/.env` を環境変数に読み込む。手元ではテスト用の DB の接続を環境変数で渡す）。

### 時刻と時間帯（#3255）

- **時刻は UTC で保存する。** DB の session の時間帯は `pr_backend_core.oracle_session.init_oracle_session` が `'+00:00'` に固定する（python-oracledb は OS の時間帯の offset を session の時間帯にし、`datetime` の tzinfo を無視して bind するため、OS の時間帯で `TIMESTAMP WITH TIME ZONE` の値がずれる）。新しい接続の経路も、pool の `session_callback` か単発の接続の直後にこれを当てる。時間帯は名前（`'UTC'`）ではなく offset で書く（名前の時間帯の値は thin mode で読めない: DPY-3022。#3241）。
- **「今日」と既定の時間帯は共通の設定 `PLATFORM_TIMEZONE`（属性 `timezone`。既定 `UTC`）から決める**（`pr_backend_core.timezones.local_today(settings)` / `configured_timezone(settings)`）。`date.today()`・`datetime.now()`（時間帯なし）・時間帯の名前の定数を使わない。
- **画面の日時はこの時間帯で表示する（#3265）。** `GET /api/auth/me` とログインの応答の `timezone`（`pr_system_settings.auth.router.DisplayTimezoneData`。製品の `CurrentUserData` が継承する）を `AuthProvider` が `@production-ready/ui` の `setDisplayTimeZone` に渡し、日時は `formatDisplayDateTime` / `displayDateTimeFormat` / `formatRunDateTime` などで書く（ブラウザの時間帯・`timeZone` の直書きを使わない）。
- **業務データの SQL の「今」はこの時間帯（#3265）。** 業務データの SELECT を実行する間だけ `local_session_time_zone(connection, session_time_zone_offset(settings))` で session の時間帯を offset にし、抜けると `'+00:00'` に戻す（pool に返す接続とアプリの表は UTC のまま）。`CURRENT_DATE` / `CURRENT_TIMESTAMP` が設定の時間帯の今を返す（NL2SQL は生成した SQL の `SYSDATE` / `SYSTIMESTAMP` をこれに置き換える）。
- ADB の `SYSDATE` / `SYSTIMESTAMP` は UTC を返す。`SYSDATE_AT_DBTIMEZONE` を `TRUE` にしない（手順と理由は [deployment.md](./deployment.md)「Autonomous AI Database（全製品で共有）」）。

### 別の製品・別の worker が保存した共通の設定の反映（#465・#2092・#2102）

システム設定の画面の保存は共通の `platform/.env`（と `model-settings.json`）に書き、保存を受けたプロセスの Settings だけを書き換える。ほかの製品の backend・同じ製品の別の worker（本番の gunicorn は 2 worker）・取込などの worker は、次の `get_settings()` で `.env` の変化（mtime・size）を見て読み直す。再起動は要らない。

- 読み直すのは、前回読んだ `.env` の内容から**変わった項目だけ**（プロセスの中で変えた値を、別の行の変更で戻さない）。環境変数で与えた項目は読み直さない。RAG は `app.config.reload_env_settings_if_changed` で `.env` の全体、NL2SQL・Agent は `pr_backend_core.config.EnvFieldReloader` で共通の項目（`PLATFORM_SETTING_FIELDS`）すべて。読み直した項目があれば `model-settings.json` のモデル設定をかけ直す（JSON を `.env` より優先する）。`model-settings.json` 自体の変化は `ModelSettingsStore.reload_if_changed` が読み直す。
- 設定から作った object は、作ったときの設定と今の設定を比べて作り直す（古い接続・client を使い続けない）。新しく長く持つ object を作るときも同じにする。

| 画面 | 項目 | ほかのプロセスでの反映 |
|---|---|---|
| OCI 認証 | config file・profile・region（`PLATFORM_OCI_*`）、OCI config のファイルと鍵 | 次の要求から。OCI の client は要求・job ごとに作る。NL2SQL のフィードバックの埋め込みの client（長く持つ）は、設定と OCI config のファイルの状態（mtime・size）で作り直す |
| OCI 認証 › Object Storage | region・namespace（`PLATFORM_OBJECT_STORAGE_*`） | 次の要求から |
| アップロード保存先 | 保存先・ローカルのディレクトリ・bucket | 次の要求から（#2092） |
| モデル | `model-settings.json`・API key（`PLATFORM_OCI_ENTERPRISE_AI_*_API_KEY`） | 次の要求から（JSON の mtime） |
| データベース | user・password・DSN・Wallet・接続のセキュリティ（NL2SQL） | 次に接続を借りるときに pool を作り直す: `SharedOraclePool`（接続引数の指紋）、RAG の共有 pool（`_shared_pool_settings_key`）、NL2SQL の DeepSec の管理・DATA USER の pool（`OraclePoolManager._pool_identity`）。古い pool は閉じる |
| データベース | Instant Client の場所（`PLATFORM_ORACLE_CLIENT_LIB_DIR`）・driver mode（NL2SQL） | **再起動が要る**（python-oracledb の Thick mode の初期化はプロセスで 1 回だけ） |
| データベース（Agent） | 起動時に DB が未設定で、後から設定した | **再起動が要る**（`AGENT_RUNTIME_REPOSITORY_BACKEND=auto` の判定と、メモリに読んだ保存データは起動時のまま） |
| ADB 管理 | ADB の OCID・region | 次の要求から |
| （画面から変えない） | 認証ポリシー（`PLATFORM_AUTH_*`）・`PLATFORM_SERVICE_TOKEN_SECRET`・`PLATFORM_PUBLIC_BASE_URL` など | `.env` を書き換えれば次の要求から。ただしログインの試行の回数の鍵（`PLATFORM_SERVICE_TOKEN_SECRET` から作る）は起動時のまま（変えたら 3 製品を再起動する） |

## テストの並列実行（pytest-xdist、#344）

CI は backend の pytest を pytest-xdist で並列に実行する（`uv run pytest -n auto`。Agent は `scripts/check-all.sh` に `PYTEST_ARGS="-n auto"` を渡す）。テストは並列でも直列でも通るように書く。

- 書き出すファイルは `tmp_path` に置く（backend 直下など固定のパスを、別のテストと共有しない）。
- `parametrize` のテスト ID を、実行のたびに変わる値（作成時刻を含む zip / xlsx / docx の bytes 等）から作らない。worker ごとに ID が変わり収集が失敗するので、`ids=` で固定する。
- retry / backoff の待ちは `time.sleep` を直接呼ばず、module 変数などで差し替えられるようにし、テストは conftest で待たない関数にする（例: NL2SQL の `reverse_generation._retry_sleep`）。
- 同時に動かせないテスト（RAG の実 Oracle のテスト）は `xdist_group` で 1 つの worker にまとめる（RAG は `--dist loadgroup` を pyproject の `addopts` に入れている）。

## 共通 trace の基盤

`pr_backend_core.observability.tracing` は専用の OpenTelemetry provider を持つ。API は lifespan、別プロセスの実行 worker は main の起動後に `tracing_lifecycle` を使う。import 時や fork 前に送信用の thread を作らず、他の SDK のグローバル provider を置き換えない。Agent SDK の外部向け tracing を有効にする仕組みではない。

共通の設定は `TracingSettings` と `platform/.env.example` の `PLATFORM_TRACE_*` が正本。設定の変更は各 API / worker の再起動で反映する。OTLP/HTTP の endpoint は traces の完全な URL を指定し、空なら外部へ送らない。送信には SDK の件数に上限のある `BatchSpanProcessor` を使い、アプリの処理を collector の応答待ちにしない。終了の待ち時間にも上限を設ける。実運用の再送・保存・tail sampling は collector 側の構成とあわせて検証する（[SDK の送信処理](https://opentelemetry-python.readthedocs.io/en/stable/sdk/trace.export.html)）。

`observed_span` は実際の実行の範囲に置き、既存の終了イベントから開始・終了を作らない。属性はコードの allowlist だけとし、質問・プロンプト・SQL・引数・結果・例外の本文は含めない。例外は種類だけを記録し、取消は失敗と区別する。usage が取得できない呼び出しの値をゼロで補わない。

プロセスや HTTP の境界へ渡す値は `capture_trace_context` / `attach_trace_context` の traceparent だけとし、認証・actor・baggage と混ぜない。受信した span は親、処理中のローカル span は現在の span としてログに出し分ける。HTTP の SERVER span は最終 body・SSE の終了・切断まで保持する。再開や非同期処理では、親の HTTP span が閉じた後も受け取った context を使える。

`operation_trace` の表示用 `TraceSummary` は、export の sampling と独立して件数の上限まで保持し、欠落・上限超過・外部送信の抽出状態を区別する。永続化するときは製品の既存の所有者判定・排他・fencing の内側で `finish_operation_trace` を使う。異なる実行の記録は確定しない。**trace が処理の唯一の記録で、画面の「処理の経過」はその投影にする（#3226）。** 工程は span で測り、工程の説明・件数・LLM の回数などの表示用のデータは `ObservedSpan.set_display`（`SpanDisplay`: 工程の種類 `kind`・名前の値 `params`・開いて見る詳細 `details`）で span に付ける。`display` は画面の記録（`TraceSummary` と処理の経過のイベント）だけに入れ、OTel へ export しない（export は属性の allowlist だけのまま）。`display` に入れてよいのは処理の経過の段の詳細と同じ範囲（質問から作った検索文・文書名と見出し・抽出した条件など）で、回答の全文・SQL の結果の行・文書の本文は入れない。保存先と見える人は処理の経過と同じ（所有者と system_admin の判定の内側）。実行中は `operation_trace(listener=...)` が span の開始・表示用のデータの変更・終了を知らせ、製品は共通の span の表に書く（下の「共通の span の表」）。実行の外で過ぎた時間（ジョブの待機・承認の待ち）は `record_span` で終わった区間として記録する。表示用の記録は export の設定（無効・endpoint の有無・sampling）に左右されず、export を設定していない process でも作る。span の無い古い形式の記録は実測として扱わず、画面は古い形式であることを示す。


**共通の span の表（`PLATFORM_TRACE_SPANS`。#3226 の案 A）**: 3 製品の span は 1 つの表に書き、画面は製品の記録の trace id で読んで `PARENT_SPAN_ID` で木にする（業界の分散 tracing の span store と同じ形。実行中の span も、別の process・別の製品の span も読める）。書き込みは `pr_backend_core.observability.trace_store.TraceSpanWriter`（process ごとに 1 つ。lifespan / worker の起動の後に開始）が受け持ち、主の処理は queue に積むだけで DB を待たない。span ごとに最新の状態だけを残してまとめて書き（`PLATFORM_TRACE_STORE_FLUSH_SECONDS` / `_BATCH_SIZE`）、通常の queue（`PLATFORM_TRACE_STORE_QUEUE_SIZE`）が満杯なら span の開始の知らせを捨てる。実行の全体の span と製品の境界の span（`mcp.request` と受けた側の server の span）は別の queue に入れて捨てず、書けなかった span は trace ごとに数え（`TraceSpanWriter.dropped`）、製品は実行を確定するときにその数を製品の記録に保存する（画面は「一部の処理だけを記録しています」）。**最終の状態の正本は製品の記録**で、表に書けなくても製品の記録の状態で見出しを出し、表で実行中のままの span は製品の終端に合わせて閉じる（`trace_progress_events`）。表に入れるのは構造・時刻・状態・allowlist の属性・工程の種類と名前の値（スカラー）だけで、工程の詳細（`SpanDisplay.details`）は製品の記録に置き、行を開いたときに製品の API が判定の内側で返す。読むときは製品の記録を今の判定（「AI 活用」: system_admin は全員、それ以外は自分）で開き、その記録の trace id だけを `fetch_trace_spans(viewer_user_uuid=...)` に渡す（system_admin は全員の span、それ以外は自分が持ち主の span。呼ばれた側の span の持ち主はサービストークンの `sub`）。保存期限は `PLATFORM_TRACE_RETENTION_DAYS`（既定 0 = 無期限。正の値のときだけ、その日数を過ぎた span を消す）。量の目安は 1 回答 20〜50 span・1 日 1,000 回答で 1 年 約 8〜18 GB。

**製品をまたぐ MCP の trace**: Agent は `mcp.request` で W3C の `traceparent` を HTTP header と JSON-RPC の `params._meta`（`mcp_trace_meta`）に入れる。RAG / NL2SQL の `POST /api/mcp` は `mcp_server_trace` の中で処理し、**サービストークンで認証した要求の traceparent だけを親にする**（`params._meta` を優先し、無ければ header）。受けた server の span（`mcp.server`）を表に書くので、Agent の木の下に呼ばれた側の span がつながる。API キーの外部の MCP クライアントとブラウザの traceparent は親にせず新しい trace を始め、来た span context は OpenTelemetry の span link にだけ残す（export する trace の関連。表には入れない。他人の trace に span を差し込めないようにする）。製品の実行（回答・検索・ジョブ）の全体の span は `operation_trace_parent` の中で始め（信頼した MCP の要求の中なら今の trace を続け、それ以外は新しい trace と span link）、別の process で実行するジョブ・Agent の Run は受け付けた要求の traceparent を `traceparent_links` で span link にする（製品ごとに作らない）。MCP の client / server の span は OpenTelemetry の MCP の semantic conventions の `mcp.method.name`（`tools/call` なら `gen_ai.tool.name`）を持ち、利用者向けの処理の経過の木は `tools/call` だけを行にする（#3272）。**span の名前は作った製品が付ける（#3272）**: 製品は `TraceSpanWriter(display_name=...)` に工程の利用者向けの名前（名詞。その製品の画面の工程の名前と同じ）を返す関数を渡し、書き手が `DISPLAY_PARAMS_JSON` の `display_name` に入れる（列は足さない）。処理の経過のイベントは span を書いた製品（`product`）と工程の名前（`display_name`。`params` とは分ける）を持ち、共通の部品は別の製品の span を画面の製品の工程の定義で訳さずに `display_name` で出し、製品の境目の行に製品名を添える（画面の製品は `createTraceDetailsLabels(t, { product })`）。画面は共通の `useTraceProgressSnapshot`（実行中の読み直し・1 回の読み出しの上限・終わった直後の書き終わりを待つ読み直し・`dropped_spans`）で読み、一部だけの記録は `ChatProgress` / `RunTimelineSection` の `partial` に渡す。

GenAI の属性は semantic conventions 1.44.0 の `gen_ai.operation.name` / `gen_ai.provider.name` / model / tool / usage を採用する。仕様は Development のため、互換性の判断と属性の allowlist は共通層に置く。`cached_tokens` は入力の内数、`reasoning_tokens` は出力の内数として扱い、合計へ重ねて足さない。DB 内の Select AI は呼び出せた DB の区間だけを記録し、見えない provider の token を推定しない。

Agent の既存の終了イベントは実行 span とは別の log として扱う。`AGENT_OPENTELEMETRY_ENDPOINT` はイベントの OTLP/HTTP logs（`/v1/logs`）へ送り、実測 trace は `PLATFORM_TRACE_OTLP_ENDPOINT`（`/v1/traces`）へ送る。従来の traces の URL を設定していた場合、イベントの送信先を logs に変更する。webhook / Langfuse のイベント連携も、通常送信から件数に上限のある queue を使う。再送は失敗した送信先に限り、停止では共通の待ち時間の上限まで待つ。配送の満杯・再送・送信完了は `agent_trace_deliveries_total` で確認する。サンプルで落とした失敗イベントだけを完全な trace として残す例外は設けない。

表示用の span summary は Run / job / 実行履歴の既存 JSON に保存し、既存の所有者と system_admin の参照判定を通す。RAG の回答保存中の snapshot は未終了の span を作らず partial とする。実行履歴と完了した応答には実測済みの記録を渡す。Agent の承認前後の実行区間は `trace_segments` に保存し、同じ Run ID と trace ID で対応付ける。保存する区間と span の上限は `PLATFORM_TRACE_UI_MAX_SEGMENTS` / `PLATFORM_TRACE_UI_MAX_SPANS` で設定し、欠落を完全として表示しない。


## 実行環境とログイン認証

3 製品は `pr_backend_core.config.RuntimeSettings` を使う。環境は共通の `platform/.env` の `PLATFORM_ENVIRONMENT`（`local` / `staging` / `production`）、認証方式は `PLATFORM_AUTH_MODE`（`local` / `authenticated`）で指定する。コードと `.env.example` の既定値は `staging` + `authenticated`。環境変数で渡す場合も同じ名前を使う。

- `local` + `local` を明示したときだけ、全権限のローカル利用者でログインを省略する。
- `staging` と `production` は `authenticated` と `PLATFORM_AUTH_COOKIE_SECURE=true` を必須にする。readiness の安全検査とサービス制御の可否もこの区分を使う。
- `is_production` は正式な本番の識別、`is_deployed` は検証・本番に共通の安全検査に使う。個別に文字列を判定しない。
- `dev` / `development` / `prod` や認証方式の `production` は受け付けない。旧い製品別の環境・認証・DEBUG 変数は読まない。

### 既存環境の更新

1. 共通 `.env` に `PLATFORM_ENVIRONMENT=staging`（正式な本番は `production`）、`PLATFORM_AUTH_MODE=authenticated`、`PLATFORM_AUTH_COOKIE_SECURE=true` を設定する。
2. 各製品の `.env` から `RAG_ENVIRONMENT` / `NL2SQL_ENVIRONMENT` / `AGENT_ENVIRONMENT`、`RAG_AUTH_MODE` / `AGENT_AUTH_MODE`、`NL2SQL_APP_AUTH_ENABLED` / `NL2SQL_DEBUG` を削除する。旧値への互換は持たない。
3. 古いコードが `.env` を再読込して免ログインに切り替わらないよう、旧変数の削除はコード更新とサービスの停止・再起動を一体で行う。秘密の値は表示・commit しない。
4. HTTPS と3製品のログイン、`/api/ready`、サービス管理の環境表示を確認する。

配備の環境は3製品共通の `PLATFORM_ENVIRONMENT` を使う。

チャット添付の初期配備は 3 製品とも無効です。製品ごとに設定 API で有効化した保存値は再配備でも維持します。`.env` に有効化の既定値は置きません。

### 軌跡の本文を記録するか（#3901）

運用設定「軌跡の記録」は3製品で1つの値を共有する。`PLATFORM_TRACE_RECORDING_SETTINGS` の単一の行を使い、未設定・読み出し失敗は無効。短い cache で読み、新しい span の開始時に判断を固定する。入力・出力・返却された思考・Schema と本文の診断詳細を対象とし、無効時はコピー・機密除去・serialize の前に止める。構造・時刻・状態・モデル・usage・内容を含まないエラー種別は記録する。通常の業務記録と既存の trace は削除しない。API / worker の tracing lifecycle に共通の provider を注入し、backend_core は system_settings_backend を import しない。
