# アプリケーション認証・RBAC 運用ガイド

## 適用範囲

本機能は OCI IAM を使用せず、Oracle に永続化した local application user と role で認証・認可する。
ただし共通 `.env`（`platform/.env`）の `PLATFORM_ADMIN_LOGIN_USER_ID=system_admin` / `PLATFORM_ADMIN_LOGIN_USER_PASSWORD` に一致する構成管理者は、
認証 table を参照しない `SYSTEM_ADMIN` として扱う。`PLATFORM_ADMIN_LOGIN_USER_ID` は `system_admin` 固定・
大小文字区別であり、`System_Admin` / `SYSTEM_ADMIN` などは database user へ fallback しない。
`PLATFORM_ORACLE_USER` / `PLATFORM_ORACLE_PASSWORD` は database connection 専用であり、application login には使用しない。
アプリケーション機能権限は FastAPI の route manifest で default deny とし、画面表示制御に加えて API 側でも
毎回ユーザー状態、role、permission を再評価する。

RBAC は画面表示用の `menu.*` と、API/データ利用用の capability permission を分ける。たとえば
`menu.profiles` は「業務プロファイル」管理画面へ入る権限であり、SQL 生成画面で profile を選択・利用する
権限ではない。`menu.query` と `menu.sql_to_question` は認証時に `nl2sql.profiles.read` と
`nl2sql.schema.read` を継承し、業務 profile の summary / usage context と schema picker を読める。
一方で full profile detail、作成、更新、削除、import/export、Oracle sync は
`nl2sql.profiles.manage` を要求する。`nl2sql.profiles.manage` は `nl2sql.profiles.read` を継承する。
schema refresh は `nl2sql.schema.refresh`、schema 参照は `nl2sql.schema.read` とし、refresh は read を継承する。

同じ方針で、SQL 生成と SELECT SQL 実行も capability を分ける。`menu.query` は
`nl2sql.query.generate` / `nl2sql.sql.execute` / `nl2sql.feedback.write` を継承し、SQL 生成 API、
直接実行、本人履歴への feedback 登録を利用できる。`menu.direct_sql` は `nl2sql.sql.execute` だけを
継承し、`/api/nl2sql/jobs`、`recommend-profile`、`rewrite`、`similar-history` などの生成補助 API は
利用できない。feedback 管理一覧、admin review、feedback index/config は `nl2sql.feedback.manage` を
要求し、普通ユーザーは自分の履歴にだけ feedback を登録できる。sample data は
`nl2sql.sample_data.manage`、legacy learning material は `nl2sql.learning_material.manage`、
diagnostics は `nl2sql.system_status.read`、persistence recover は `nl2sql.persistence.recover` で制御する。
Ontology query-session 系も同じ境界に従う。`/api/nl2sql/query-sessions`、`generate-sql`、
`confirm-sql` は `nl2sql.query.generate` を要求し、実 DB へ SELECT を発行する
`/api/nl2sql/query-sessions/{session_id}/execute` は `POST /api/nl2sql/execute` と同じく
`nl2sql.sql.execute` を要求する。
ただし `GET /api/nl2sql/persistence` は業務画面起動時の readiness gate が利用する粗粒度状態であり、
ログイン済みユーザーなら capability なしで参照できる。

## 初期 migration と構成管理者

`PLATFORM_ADMIN_LOGIN_USER_ID=system_admin` / `PLATFORM_ADMIN_LOGIN_USER_PASSWORD` を共通 `.env`（`platform/.env`。#211）に設定すると、その構成管理者で
アプリケーションへログインできる。この `SYSTEM_ADMIN` ログインは `PLATFORM_USERS` /
`PLATFORM_AUTH_SESSIONS` を読まず、認証 table が未作成でも利用できる。通常の application user を追加して
使う場合は、DB 接続後に次を一度実行する。
処理は幂等であり、再実行できる。

```bash
cd backend
uv sync
uv run python -m app.cli.app_security_migrate --apply --skip-bootstrap
```

通常の application user は `PLATFORM_USERS` から照合される。構成管理者の password は application
password 変更画面から変更でき、変更結果は共通 `.env` の `PLATFORM_ADMIN_LOGIN_USER_PASSWORD` に書き戻される
（3製品で同じ構成管理者を使う）。旧キー（`APP_ADMIN_LOGIN_USER_*` / `APP_ADMIN_PASSWORD` など）は読まない。
既存環境は [configuration.md](./configuration.md) の「既存環境の更新手順（#211）」で移す。
通常 user の password はこれまでどおり DB hash として保存される。ユーザー管理 API/UI は
`system_admin` の大小文字違いを含む DB user 作成を拒否する。

`SYSTEM_ADMIN` role は構成管理者と旧 bootstrap user 専用とする。ユーザー管理 API/UI は、後続で
作成したユーザーへの新規付与・再付与を拒否する。旧版や手動操作で非 bootstrap user に
`SYSTEM_ADMIN` が残っている場合も migration では自動撤去せず、管理者が必要に応じて手動で解除する。

## 共通認証テーブルへの移行（#212）

ユーザー・ロール・ユーザーとロールの割り当て・セッションは、3 製品（RAG / NL2SQL / Agent）で共有する
`PLATFORM_*` テーブルに置く。ログイン・セッション・CSRF・構成管理者・ユーザー / ロール操作の実装も
platform の `pr_system_settings.auth` にあり、NL2SQL はその上に NL2SQL 固有の権限・業務プロファイル
利用権限・Data Grant を足している。

| 旧（NL2SQL） | 新（共通） |
|---|---|
| `NL2SQL_APP_USERS` | `PLATFORM_USERS` |
| `NL2SQL_APP_ROLES` | `PLATFORM_ROLES` |
| `NL2SQL_APP_USER_ROLES` | `PLATFORM_USER_ROLES` |
| `NL2SQL_AUTH_SESSIONS` | `PLATFORM_AUTH_SESSIONS` |

NL2SQL 固有の `NL2SQL_APP_ROLE_PERMISSIONS` / `NL2SQL_APP_ROLE_PROFILES` は名前を変えない。旧名との
互換は持たない（旧名のテーブルは読まない）。Deep Data Security の削除（#23）で読まなくなった
`NL2SQL_APP_DATA_ENTITLEMENTS` / `NL2SQL_DEEPSEC_MIGRATIONS` は、既存の環境では表をそのまま残す
（migration で落とさない。アプリは読まない）。新しい配備では作らない。

`app_security_migrate --apply` は `PLATFORM_*`（platform の DDL）と migration 004 の表を作るだけで、既存の表は変えない。
旧名・旧形からの移行（`RAG_APP_*` → `NL2SQL_APP_*` → `PLATFORM_*` の改名、旧監査ログの表の削除、権限の行の
削除・追加、列の追加）の migration は、初めての release の前に削除した（#1648。既存の ADB の同じ schema に旧版の
単独の製品などの同じ名前の表があっても、承認なしに改名・削除しないため）。旧名の表があれば、改名・削除・変更せずに
名前を標準エラーに出して残す。旧版の環境のデータを移す手順は持たない（新しい schema に配備し直す）。

なお、NL2SQL の業務プロファイルの対象一覧・管理 SQL は、`NL2SQL_` に加えて `PLATFORM_` / `RAG_` / `AGENT_`
で始まるオブジェクトをシステムオブジェクトとして扱い、表示・操作の対象にしない（3 製品が同じ schema を
共有するため）。

## ユーザー管理・ロール管理・権限管理の分担（#206）

| 画面 | URL | menu 権限 | 扱う内容 | 実装 |
|---|---|---|---|---|
| ユーザー管理 | `/settings/security/users` | `menu.security_users` | ユーザー、割り当てロール、ロック、一時パスワード | 3製品共通（`@production-ready/system-settings`） |
| ロール管理 | `/settings/security/roles` | `menu.security_roles` | ロールコード・名称・説明、アーカイブ・復元・削除 | 3製品共通（同上） |
| 権限管理 | `/settings/security/permissions` | `menu.security_permissions` | ロールごとの機能権限（`menu.*` / capability）と業務プロファイル利用権限 | NL2SQL 固有 |

- サイドナビは、3製品共通の「ユーザーとロール」（ユーザー管理・ロール管理）と、NL2SQL 固有の
  「セキュリティ設定」（権限管理。セクション名は 3 製品で同じ。#658）に分ける。
- API も同じ境界で分ける。`POST /api/security/roles` と `PATCH /api/security/roles/{role_id}` は
  基本情報（`role_code` / `display_name` / `description`）だけを受け取り、権限は変更しない
  （旧 client が `permissions` を送っても無視する）。権限の更新は
  `PUT /api/security/roles/{role_id}/permissions`（`version` / `permissions` / `allowed_profile_ids`）で、
  `menu.security_permissions` を要求する。`GET /api/security/permissions` と
  `/api/security/profile-access/profiles`（`q` / `limit` / `offset` / `ids` で検索とページングした `Page`。#608）も
  `menu.security_permissions` を要求する。
- どちらの更新も `SecurityService.update_role` を通り、省略した項目は現在値を保つ。権限昇格の防止
  （追加する実効権限は actor 自身の実効権限の部分集合）と業務プロファイル変更の `SYSTEM_ADMIN` 限定は
  権限の更新に対して従来どおり適用する。
- ロールの一覧・詳細の参照（`GET /api/security/roles*`）は、ユーザー管理・ロール管理・権限管理の
  いずれかの menu 権限で行える。アーカイブ済みを含む全ロールの参照はロール管理または権限管理を要求する。
- route manifest（`app/security/permissions.py` の `ROUTE_PERMISSIONS`）は、すべての API を
  `(method, route template)` ごとに登録する（`/security/users`・`/security/roles` 配下は
  `SECURITY_USER_ROLE_ROUTE_PERMISSIONS` にまとめて取り込む。#503）。`/nl2sql/*`・`/settings/*`・`/schema/*`・
  `/security/*` のどの範囲でも前方一致では割り当てないため、登録のない API は起動時の manifest の検査
  （`app.main._assert_route_manifest`）で失敗し、実行時も拒否する（#510）。権限なしで通す操作（公開・ログインだけ・
  MCP・`GET /nl2sql/persistence`）は `OPEN_API_OPERATIONS` に置く。API を追加するときは、同じ PR で
  `ROUTE_PERMISSIONS` に登録する。
- 旧 action 権限 `security.roles.manage` は、ロール管理と権限管理の両方へ正規化する（旧「ロール・権限管理」の
  ロールに `menu.security_permissions` を足していた migration 023 は #1648 で削除した）。
- ユーザー・ロールの request / response の形とパスワードポリシーは platform の
  `pr_system_settings.users_roles` を正とし、NL2SQL は `RoleData` に権限・Data Grant・業務プロファイル
  利用権限を足して返す。
- ユーザーとロールは 3 製品で共有する（#212）。SYSTEM_ADMIN 以外がロールを割り当てる・復元するときは、
  そのロールが他の製品の権限テーブル（`RAG_ROLE_PERMISSIONS` / `AGENT_ROLE_PERMISSIONS`）に持つ権限コードを、
  操作者が製品ごとにすべて持っていることも要求する（製品をまたぐ権限昇格の防止）。

## 「AI 活用」の下の機能のデータの見え方（#1889・#1954・#3244）

3 製品共通の規則（[backend-standard.md](../../platform/docs/backend-standard.md)「「AI 活用」の下の機能のデータの見え方」）:
**system_admin は全員のデータ、それ以外の利用者は自分のデータだけ**。判定は `pr_system_settings.auth.visibility`。
`nl2sql.feedback.manage` などの製品の管理権限は、「AI 活用」の画面で全員を見る理由にしない。他人のデータの詳細・操作は 404。
system_admin が他の利用者のデータにできるのは閲覧と運用の操作（停止・削除）だけで、新しい実行・内容を足す操作（続きの質問・SQL の実行・回答前の確認・評価・名前の変更）は持ち主だけ（見える他人のデータは 403 で `OTHERS_CONTENT_FORBIDDEN_MESSAGE`。#3244。#1928 を置き換え）。
実行履歴の行は、実行した人と会話の持ち主の両方に見える（#1954。判定は `can_access_run` と同じ範囲）。利用量・API キーの
上限（キーごと）・監査（実行履歴の `actor_user_uuid`・実行のログ）は実行した人に数える。

| 画面 | API | 一般の利用者 | system_admin |
|---|---|---|---|
| チャット | `GET /nl2sql/chats` | 本人の会話 | 全員の会話。応答の `actor_display_name`（共通の表示名。#1696）を、他の利用者の会話（`owned=false`）の履歴の日時に添える。`owned` で本人の会話かを返す（持ち主の UUID は出さない） |
| チャット | `GET`・`DELETE /nl2sql/chats/{id}` | 本人の会話（他人は 404） | 全員の会話を見て、削除ができる |
| チャット | `PATCH /nl2sql/chats/{id}`（名前の変更） | 本人の会話（他人は 404） | 本人の会話だけ。他の利用者の会話は 403（#3244） |
| チャット | `GET /nl2sql/chats/{id}`（入力欄） | — | 他の利用者の会話（`conversation.owned=false`）を開いているときは、入力欄・「実行」・履歴の名前の変更を出さず、入力欄の代わりに閲覧だけの案内（処理中なら「停止」）を出す（#3244） |
| チャット | `GET /nl2sql/chats/{id}` のターン | 本人の会話のすべてのターン（#3244 より前に system_admin が送ったターンを含む） | 同じ。持ち主以外が送ったターンには `sender_display_name`（共通の表示名。引けない利用者は空、持ち主のターンは null）を付け、画面は質問の吹き出しの上に「〈名前〉 が送信」と出す（送り手の UUID は出さない） |
| チャット | `POST /nl2sql/jobs`（続き）・`POST /jobs/{id}/execute` | 本人の会話（他人は 404）。持ち主は #3244 より前に system_admin が送ったターンの後にも続けられ、そのターンの SQL を自分の接続で実行できる | 本人の会話だけ。他の利用者の会話は 403（#3244。判定は `_assert_chat_owner_adds`。ジョブも実行も作らない） |
| チャット | `POST /jobs/{id}/execution-result`（ジョブの中で実行した結果の行） | 本人が送ったターン（ほかは 404） | 同じ。行は送った人の権限と接続で取ったものなので、送った人だけが受け取る（ほかの人は 404 で、「もう一度実行」で自分の権限と接続で実行する。受け取らない人の要求で送った人の行を消さない） |
| チャット・SQL 生成 | `GET /nl2sql/jobs/{id}`・`/trace`・`POST /jobs/{id}/cancel` | 本人の job（他人は 404） | 全員の job（チャットの job を含む。停止は運用の操作） |
| SQL 生成 | `POST /nl2sql/feedback`（実行結果の評価） | 本人の履歴（他人・持ち主の無い履歴は 404） | 本人の履歴だけ。他の利用者・持ち主の無い履歴は 403（#3244）。評価した人を `feedback_actor_user_uuid` に残す |
| SQL 生成・チャット | `/nl2sql/query-sessions/*`（回答前の確認） | 本人の session（他人・持ち主の無い session は 404） | 全員の session を見て（`GET`）取り消せる（`/cancel`）。回答・反映・意図の変更・SQL の生成・確定・実行・改善提案は本人の session だけで、他の利用者・持ち主の無い session は 403。他の利用者の会話の続きで確認を始める（`POST /query-sessions` の `previous_job_id`）のも 403（#3244） |
| DB 管理チャット | `/nl2sql/db-admin-chat/*` | 本人の会話（他人は 404） | 全員の会話を見て（会話・ターンの処理の経過）、削除できる。続きの質問（`/generate`）・実行（`/execute`）・SQL の置き換え（`PUT .../plan`）・エラーの分析（`.../analyze-error`）・名前の変更は本人の会話だけで、他の利用者の会話は 403（#3244） |
| 実行履歴 | `GET /nl2sql/history`・`GET /nl2sql/history/{history_id}`（詳細。見えない行は 404。#1955） | 本人が実行した行と、本人の会話で他の人（system_admin）が実行した行（#1954）。SQL 生成・SQL 直接実行など会話の無い実行は実行した人だけ | 全員の行。ログイン ID（`actor_login_user_id`・`actor_display_name`）は system_admin の応答だけ |
| SQL 直接実行・SQL から質問 | `POST /nl2sql/execute`・`/reverse*` | 保存しない（利用者のデータを返さない） | 同じ |

- 実行履歴（#1954）: 行に実行した人（`actor_user_uuid`）とチャットの往復なら会話の持ち主（`conversation_owner_user_uuid`。
  最初のターンの actor。行を書くときに残す。会話の無い実行は空）を保存し、一覧は
  `actor_user_uuid = :me OR conversation_owner_user_uuid = :me`（state document の JSON の条件。index は足さない）で絞る。
  応答の各行に `executed_by_me`・`executor_display_name`（実行した人の共通の表示名。見る人を問わず返す）・
  `conversation_owner_display_name`・`owned_by_me`（会話の無い実行は null）を添える。`executor=all|mine|others`
  （既定 all。`others` は実行した人の記録の無い行を含む）で絞り、`total` も同じ条件、`unfiltered_total` は見られる範囲。
  詳細は一覧の行のため、見られない行は一覧に出ない。行の評価（`POST /nl2sql/feedback`）は実行した人だけ（#3244）。
  #1954 より前に書いた行は持ち主を持たないため、実行した人にだけ出る。
- SQL の実行の接続は、要求した利用者で system_admin / それ以外に分ける（#904）。この規則は「だれのデータを扱えるか」で、
  接続の選び方は変えない（実行は要求した利用者の接続で、他の利用者の権限・接続を借りない。system_admin は他の利用者の会話では実行できない。#3244）。
- 規則の外: フィードバック管理（`GET /nl2sql/feedback`・`/feedback/admin-review`・`DELETE /feedback/{id}`）・品質評価・
  利用状況などの「改善・運用」は、それぞれの権限のまま。`POST /nl2sql/similar-history`（管理者が良いと評価した
  過去の SQL の候補）は、管理者が確かめた共有の知識なので、業務プロファイルの範囲で全員の物を返す。
- MCP（`nl2sql_get_job`・`nl2sql_cancel_job`・`nl2sql_cancel_requirement`）も同じ規則: 本人のジョブ・session だけ、キー（または
  サービストークン）の利用者が system_admin なら全員のジョブ・session を見て、ジョブ・確認を取り消せる（運用の操作。#3244。
  キーの製品・業務プロファイルの範囲の制限は残す）。
  `nl2sql_query` の `requirement_session_id` で確認を続ける（回答して SQL を作る）のは本人だけで、system_admin でも他の利用者の
  session は 403（#3244）。

## Agent からの呼び出し（MCP、#231）

Agent は NL2SQL を `POST /api/mcp`（MCP の Streamable HTTP、JSON 応答）で呼ぶ。共通の仕組みは
platform の `docs/backend-standard.md`「製品間の連携（MCP とサービストークン、#230）」を正とする。

- 認証: Cookie ではなく `Authorization: Bearer <サービストークン>`（audience `nl2sql`、署名鍵は共通 `.env` の
  `PLATFORM_SERVICE_TOKEN_SECRET`。空なら 503）。token の `sub`（Run の利用者の `user_uuid`）を
  `principal_for_worker` で現在のロール・権限・業務プロファイル利用権限から組み立てる。token なし・不正・
  期限切れ・audience 違いは 401、無効・初回パスワード変更待ちの利用者は 403。CSRF は照合しない。
- route manifest は `/mcp` を「認証済みなら通す」（`permission_for_route` が None）にし、権限はツールごとに
  判定する（`tools/list` は利用者が使えるツールだけを返す）。
- 利用者: ツールは Run の利用者として実行する。actor は同じ認可経路（`enter_actor`）で入り、
  ジョブの `actor_user_uuid` に残る。
- `PLATFORM_AUTH_MODE=local` かつ `PLATFORM_ENVIRONMENT=local` のローカル実行モードは、token なしで全権限の
  ローカル DEBUG 管理者として通す（他の API と同じ）。

| ツール | 必要な権限 | 画面の route と同じ判定 |
|---|---|---|
| `nl2sql_list_profiles` | `nl2sql.profiles.read` | `GET /api/nl2sql/profiles/search` と同じ業務プロファイルの範囲（claim があれば claim との積） |
| `nl2sql_recommend_profile` | `nl2sql.query.generate` | `POST /api/nl2sql/recommend-profile`。任意の `profile_ids`（候補の一覧）を渡すとその中からだけ推薦する（候補は利用者の範囲・claim との積。積が空なら推薦なし）。範囲外・しきい値未満の推薦は `null` |
| `nl2sql_query` | `nl2sql.query.generate` と `nl2sql.sql.execute` の両方 | `POST /api/nl2sql/jobs`。**`profile_id` を省略・空白・使えない ID にしても `"default"` に切り替えない**（#1379）。利用者の権限 ∩ claim の中の業務プロファイルが 1 つならそれ、そうでなければ質問の推薦で選び、結果の `auto_selected_profile` に示す（決まらなければ 404 `NL2SQL_PROFILE_NOT_SELECTED` と候補。#2355）。明示した ID が claim の範囲外なら 403（`PROFILE_SCOPE_FORBIDDEN`）。`row_limit`（既定 100、上限 1000）を必ずジョブに渡す。`wait_seconds`（既定 40、上限 45）まで待ち、終わらなければ `pending` / `running` と `job_id` を返す |
| `nl2sql_get_job` | `nl2sql.query.generate` | `GET /api/nl2sql/jobs/{job_id}`。本人のジョブだけ（フィードバック管理などの製品の管理権限があっても他人・不明は 404。system_admin は全員のジョブ）。読むたびに claim の範囲外・利用者が今は使えない業務プロファイルのジョブは 403（#3318） |
| `nl2sql_cancel_job` | `nl2sql.query.generate` | `POST /api/nl2sql/jobs/{job_id}/cancel` と同じ協調の取消（実行中の段階の区切りで止まり、`status=error`・`error_code=JOB_CANCELLED`）。判定は `nl2sql_get_job` と同じ（本人と system_admin。#3244 の運用の操作）。冪等で、終わったジョブもエラーにせず `result`（`cancelled` / `already_cancelled` / `already_finished`）で返す。取消の要求のドキュメント（`job_cancel_requests`）に操作した人（`actor_user_uuid`）と入口（`via=mcp`・API キーなら `api_key_id`・サービストークンなら `agent_run_id`）を残し、2 回目以降の要求は最初の記録を上書きしない。annotations は `destructiveHint` / `idempotentHint`（#3318） |
| `nl2sql_cancel_requirement` | `nl2sql.query.generate` | `POST /api/nl2sql/query-sessions/{session_id}/cancel`。本人の回答前の確認の session だけ（管理権限があっても他人・不明は 404）。claim の範囲外の業務プロファイルの session は 403 |

`nl2sql_query` は SQL を作る前に回答前の確認（`POST /api/nl2sql/query-sessions` の guided と同じ。#1441）を行い、確認の要る項目があれば
`status=needs_clarification` と `requirement.question` を返す（ジョブは作らない）。続きの `requirement_session_id` は本人の session だけで、
業務プロファイルは同じ `profile_id` に限る。業務プロファイルは呼び出し元が `profile_id` で明示して選ぶため、画面の推薦の確認（確認の token）は
求めない。OCI Enterprise AI を設定していない構成（`REQUIREMENT_AI_NOT_CONFIGURED`）の
ときは確かめずにジョブを作り、`requirement.status=skipped` を返す（質問が SQL の文でも飛ばさずに同じに確かめる。#1727）。
ジョブの `question` は利用者の質問のままにし、確かめた質問の文は生成にだけ使う（#1727）。回答前の確認の LLM の失敗（`REQUIREMENT_AI_FAILED` /
`REQUIREMENT_AI_INVALID_OUTPUT`）は、`structuredContent` の `details` に失敗の種類・HTTP ステータス・モデル ID・所要時間・request id を添える（#1715）。

`menu.query` はすべてのツールを使える。`menu.direct_sql` だけのロールはどのツールも使えない
（生成できないため）。`menu.evaluation` のように生成だけできるロールには `nl2sql_query` を出さない。

### 業務 Agent のデータの範囲（サービストークンの claim、#1379）

Agent の業務 Agent の定義でデータの範囲（使える業務プロファイル）を設定したとき、Agent はサービストークンの
任意の claim `profile_ids` にその業務プロファイルの ID を入れる（範囲を設定していない Agent は claim を付けない）。
NL2SQL は Agent の Runtime の強制とは別に、呼び先でも同じ範囲を強制する（多層の防御）。

- 使える業務プロファイルは「利用者の権限（`allowed_profile_ids_for_request`）∩ claim」。claim は利用者の権限を
  広げない（claim にあっても利用者が使えない業務プロファイルは、今までどおり権限の 403）。
- `nl2sql_query` の claim の範囲外の `profile_id` は 403（`structuredContent.error_code` が
  `PROFILE_SCOPE_FORBIDDEN`）。ジョブは作らない。
- `nl2sql_list_profiles` と `nl2sql_recommend_profile` は範囲で絞る（範囲の外を一覧・推薦しない）。
- `nl2sql_get_job`・`nl2sql_cancel_job` も、読む・止めるたびにその時点の claim で判定する（#3318）。ジョブの業務プロファイルが
  claim の範囲外なら、本人のジョブでも 403（`PROFILE_SCOPE_FORBIDDEN`。確認の session と同じ）。利用者の業務プロファイルの
  権限（`assert_profile_access`）も同じく読むたびに確かめる。持ち主の判定（他人・不明は 404）が先なので、403 で存在が分かるのは
  本人と system_admin だけ。
  - 決定の理由: #1379 では「本人のジョブだけの判定のまま」とだけ決め、claim を当てない理由（壊れる経路）は書いていなかった。
    OWASP API Security Top 10 の API1（Broken Object Level Authorization。物を読むたびにその時点の権限で判定する）に合わせ、
    `nl2sql_query`・確認の session と同じ範囲をジョブにも当てる（範囲の外の業務プロファイルの実行結果を、範囲を狭めた
    業務 Agent が読めないようにする）。
  - 壊れる経路の確認: Agent の Run は 1 回の Run の中で同じ版のデータの範囲を使い（`nl2sql_query` とジョブの続きの取得
    〔`tools.wait_for_product_job`〕・Run の取消のジョブの取消は同じ claim）、作ったジョブを同じ claim で読む。業務 Agent の範囲を
    変えた後の別の Run が、前の範囲で作ったジョブを読めなくなるのは、この決定の意図した振る舞い（新しい範囲の外の結果を
    返さない）で、続きは新しい範囲で `nl2sql_query` を呼び直す。claim の無い呼び出し（範囲を設定していない Agent・API キー）は
    利用者の権限だけで判定する。
- claim の無い呼び出し（範囲を設定していない Agent・画面の API）の判定は変えない。
- claim の形（1〜50 件の 128 文字以内の ID の一覧）が違う token は 401（壊れた範囲を「範囲なし」として通さない）。
  名前・形の正本は `pr_system_settings.auth.service_token`（`PROFILE_SCOPE_CLAIM`）。

### 実行履歴の種類（MCP・品質評価、#1986）

MCP の入口（`nl2sql_query` が作るジョブ）は、呼び出し元の認証の結果からジョブのきっかけ（`JobOrigin`）を決め、
ジョブの snapshot を通って実行履歴の行（`run_source`・`api_key_name`・`agent_run_id`）に残す。画面のジョブは空。

| 呼び出し元 | `run_source` | 実行履歴の種類 | 呼び出し元の記録 |
|---|---|---|---|
| 画面（SQL 生成・チャット） | 空 | SQL 生成 / チャット | — |
| 外部の MCP クライアント（API キー `prak_…`） | `mcp` | MCP（〈キーの名前〉） | `api_key_name` |
| Agent の Run（サービストークン。claim `run_source` が `evaluation` 以外か無い） | `mcp` | MCP（Agent） | `agent_run_id`（claim `run_id`） |
| Agent の品質評価の Run（claim `run_source` = `evaluation`） | `evaluation` | 品質評価 | `agent_run_id` |

- 実行履歴（`GET /nl2sql/history`）の `kind` は `all` / `not_evaluation`（画面の既定）/ `chat` / `query` / `mcp` /
  `evaluation`。`not_evaluation` と品質評価を含まない種類は、「全 M 件」（`unfiltered_total`）も品質評価の行を除いて
  数える。既定で見える行が 0 件のときだけ、隠している品質評価の行の数（`hidden_evaluation_total`）を返す。
- 値は見え方（種類）だけに使い、権限・業務プロファイル・利用量の判定には使わない。きっかけはジョブの作成の
  API（`JobCreateRequest`）の項目にせず、MCP のツールの引数からも決めない（画面・API キーの利用者が品質評価と偽れない）。
- 既存の行（#1986 より前）は移行で埋めない（`run_source` は空のまま。種類は会話の持ち主の有無で決まる）。JSON の
  payload の項目なので DB の列・migration は足さない。

## ユーザー・ロールの物理削除

削除 API は現在表示中の version を `If-Match: "<version>"` で受け取り、前提条件を同一
トランザクション内で再確認する。ユーザーは無効化済みで、ログイン中の操作者自身でも初期
システム管理者でもない場合に限り、セッション、ロール割り当て、ユーザー本体を物理削除する。
履歴や実行ジョブに保存済みの `actor_user_uuid` は監査識別子として保持する。

カスタムロールは、割り当てユーザーがなく、アーカイブ済みの場合に限り物理削除する。ロール削除 API は
Oracle DDL を実行せず、機能権限と業務プロファイル関連を削除してからロール本体を削除する。
組み込みロールは削除できない。

旧版で作成された 8 個の `RAG_*` security table が存在する場合、migration 005 がデータを保持したまま
`NL2SQL_*` へ table、constraint、index を rename し、entitlement resource code も移行する。新規環境は
migration 004 から `NL2SQL_*` object を直接作成する。005 に残る旧 prefix は移行元を識別するためだけの
versioned legacy reference であり、runtime object 名としては使用しない。

本番では少なくとも次を設定する（`NL2SQL_*` は `backend/.env`、`PLATFORM_*` は共通 `.env`。#211）。

```dotenv
PLATFORM_AUTH_MODE=authenticated
PLATFORM_AUTH_COOKIE_SECURE=true
PLATFORM_AUTH_IDLE_TIMEOUT_MINUTES=60
PLATFORM_AUTH_ABSOLUTE_TIMEOUT_HOURS=12
PLATFORM_AUTH_FAILED_LOGIN_LIMIT=5
PLATFORM_AUTH_LOCKOUT_MINUTES=15
PLATFORM_AUTH_LOGIN_ATTEMPT_LIMIT=5
PLATFORM_AUTH_LOGIN_IP_ATTEMPT_LIMIT=20
PLATFORM_AUTH_LOGIN_ATTEMPT_WINDOW_MINUTES=15
```

ログインの試行は、DB のユーザーのロックとは別に、ログインの入口で回数を制限する（#1087）。
窓（`PLATFORM_AUTH_LOGIN_ATTEMPT_WINDOW_MINUTES` 分）の中の失敗が、ログイン ID と送信元 IP の組ごとに
`PLATFORM_AUTH_LOGIN_ATTEMPT_LIMIT` 回、送信元 IP ごとに `PLATFORM_AUTH_LOGIN_IP_ATTEMPT_LIMIT` 回に達すると、
窓が過ぎるまで `429 SECURITY_RATE_LIMITED`（`Retry-After` 付き）を返す。DB のユーザーではない
`system_admin` も、存在しないログイン ID も同じ数え方・同じ応答にする。送信元 IP は Nginx の
`X-Forwarded-For` を uvicorn の proxy headers（gunicorn の `forwarded_allow_ips` の既定 127.0.0.1 / ::1）が
解決した値を使う。

失敗の記録は共通のテーブル `PLATFORM_LOGIN_ATTEMPTS` に置き、3 製品・gunicorn の全 worker・再起動を
またいで同じ回数を数える（#1173）。ログイン ID と送信元 IP は保存せず、共通 `.env` の
`PLATFORM_SERVICE_TOKEN_SECRET` から導いた鍵の HMAC-SHA256 だけを保存する（鍵が無いときは DB に記録せず、
プロセス内だけで数える）。テーブルが無い・DB に接続できないときは、プロセス内の記録に自動で切り替えて
制限を続け（ログ `auth_login_throttle_shared_store_unavailable`）、30 秒ごとに DB を試し直す。テーブルは
`app_security_migrate --apply` が作る。既存環境の更新手順と Nginx のログインの API の上限は
[platform/docs/deployment.md](../../platform/docs/deployment.md) の「ログインの試行の回数の共有（#1173）」を参照する。

既定では、通常ユーザーの無操作 timeout は 60 分、session の絶対有効期限は 12 時間とする。
業務端末が管理下にあり、無人端末リスクを組織として受容できる低リスク環境でだけ、
deployment 固有の `.env` で `PLATFORM_AUTH_IDLE_TIMEOUT_MINUTES=720` を明示して 12 時間の無操作
timeout に拡張できる。これは production 既定値ではない。

`system_admin` 構成管理者は認証 table 未作成時の bootstrap / 運用復旧用 identity であり、
`PLATFORM_AUTH_SESSIONS` を使わない署名 token として絶対有効期限のみを持つ。通常運用は DB に
永続化した application user を使い、`system_admin` は初期設定と復旧用途に限定する。

## 対象識別子の引用規則（Issue #560）

業務プロファイルの対象・スキーマカタログ・オントロジー・管理 SQL は、対象の owner / object と列を
`app/features/nl2sql/object_identity.py` の `canonical_object_part` の規則の **canonical token** で
検証・保存・照合する。frontend の `formatDbObjectName` / `normalizeDbObjectKey` も同じ規則である。

| 入力 | 保存値（token） | 意味 |
|---|---|---|
| `orders` / `ORDERS` / `"ORDERS"` | `ORDERS` | 引用が不要な名前。従来の保存値と同じ |
| `"Mixed_Case"` | `"Mixed_Case"` | 大文字小文字を保持する引用名。`MIXED_CASE` とは別の object |
| `Mixed_Case`（引用なし） | `MIXED_CASE` | Oracle と同じく大文字として解釈する |
| `DEPT@REMOTE`、`my table`（引用なし） | 拒否 | 非引用識別子にならない名前は推測で引用しない |

- 二重引用符・NUL・制御文字を含む名前、前後に空白がある引用名、引用符を含めて 128 バイトを超える token は拒否する。
- 辞書ビュー（`ALL_OBJECTS` / `ALL_TAB_COLUMNS` / `ALL_CONSTRAINTS` / `ALL_DEPENDENCIES`）の照合は、
  token から引用符を外したカタログ上の名前を bind 変数で渡す。
- 業務プロファイルの対象表も同じ規則で保存する（次節。#561）。

### 業務プロファイルの対象表（Issue #561）

業務プロファイルの対象表・ビュー（`allowed_tables` / `allowed_views`）も、保存・照合を上表と同じ規則にそろえた。
`app/features/nl2sql/service.py` の `_resolve_profile_object_name` は、`OWNER.OBJECT` を
`canonical_qualified_name`、owner を省略した名前を `canonical_object_part` で正規化する。

| 経路 | #561 以降の挙動 |
|---|---|
| 保存（`POST` / `PATCH /api/nl2sql/profiles`） | `SALES."Mixed_Case"` はそのまま保存し、`SALES.MIXED_CASE` とは別の対象として両方を保持できる。`"SALES"."MIXED_CASE"` / `sales.mixed_case` は従来どおり `SALES.MIXED_CASE`。非引用識別子にならない名前（`SALES.my table`）と引用符の壊れた名前は `ValueError`（API はエラー応答）で拒否する |
| owner 省略の旧形式 | `"Mixed_Case"` はカタログの引用名の表に解決する。引用なしの `mixed_case` は `MIXED_CASE` として解決し、引用名の表 `Mixed_Case` には解決しない |
| SQL 安全性検査（許可表の照合） | SQL の表参照は sqlglot の引用情報（`SqlTableReference.owner_quoted` / `name_quoted`）を使い、引用なしを大文字、引用ありを書かれたとおりに解釈する。`SALES.MIXED_CASE` だけを許可したとき `SELECT * FROM SALES."Mixed_Case"` を拒否し、逆も拒否する（修正前はどちらも `SALES.MIXED_CASE` として許可していた） |
| SQL 生成の schema context | Profile の対象から schema catalog の詳細を読むとき、大文字小文字だけが異なる表の定義は使わない（#563 で schema catalog の詳細取得自体が大文字小文字を区別するようになった） |
| オントロジー | 質問補完の代表値も引用規則どおりに Profile の対象へ絞る。node の識別と Profile の view は #563、AI 構築・業務定義・SQL 接地は #573 で引用名に対応した（次節） |
| 画面（業務プロファイル・スキーマ参照） | 対象の選択・件数・一括選択・未保存判定のキーを `normalizeDbObjectKey`（`dbObjectIdentity.ts`）にそろえ、引用名を大文字化しない |

#### 既存 Profile の互換と誤保存の検出

引用が不要な名前の保存値・SQL は変わらないため、自動移行はしない。
#561 より前は、画面の選択と保存の両方で引用符を外して大文字化していたため、引用名の表を選ぶと
**大文字の同名表 `SALES.MIXED_CASE` として保存されていた**（同名表が無い場合も `SALES.MIXED_CASE` として保存され、
SQL の生成時に object が見つからない）。保存値から利用者の意図は判別できず、別の表へ自動で付け替えると
Profile の公開範囲が変わるため、次の SQL（アプリの管理 schema で実行する参考例。実 Oracle では未検証）で
大文字小文字だけが異なる表・ビューを持つ Profile を洗い出し、画面で対象を選び直して保存し直すこと。

```sql
-- Profile の対象（引用なしの OWNER.OBJECT）と、小文字を含む引用名の object の大文字化が一致する Profile
SELECT p.PROFILE_ID, p.NAME, j.OBJECT_KIND, j.OBJECT_KEY,
       o.OWNER AS CATALOG_OWNER, o.OBJECT_NAME AS CATALOG_OBJECT, o.OBJECT_TYPE
  FROM NL2SQL_PROFILES p
 CROSS APPLY (
         SELECT 'TABLE' AS OBJECT_KIND, t.OBJECT_KEY
           FROM JSON_TABLE(p.PAYLOAD_JSON, '$.allowed_tables[*]'
                           COLUMNS (OBJECT_KEY VARCHAR2(512) PATH '$')) t
         UNION ALL
         SELECT 'VIEW', v.OBJECT_KEY
           FROM JSON_TABLE(p.PAYLOAD_JSON, '$.allowed_views[*]'
                           COLUMNS (OBJECT_KEY VARCHAR2(512) PATH '$')) v
       ) j
  JOIN ALL_OBJECTS o
    ON o.OWNER || '.' || UPPER(o.OBJECT_NAME) = j.OBJECT_KEY
   AND o.OBJECT_NAME <> UPPER(o.OBJECT_NAME)
 WHERE INSTR(j.OBJECT_KEY, '"') = 0
   AND o.OBJECT_TYPE IN ('TABLE', 'VIEW', 'MATERIALIZED VIEW')
 ORDER BY p.PROFILE_ID, j.OBJECT_KEY;
```

該当 Profile では、対象に残っている `SALES.MIXED_CASE` が意図した表か確認し、引用名の表を使う場合は
`SALES."Mixed_Case"` を選び直す。

### schema catalog・オントロジー・列単位の許可チェック（Issue #563）

#561 の時点で残っていた「大文字化した名前を一意キーにする」経路を、同じ引用規則にそろえた。
内部のキーは **辞書ビュー上の名前（大文字小文字を保持、引用符なし）**、API の入力は **引用規則の token**
（引用なしは大文字、`"Mixed_Case"` は大文字小文字を保持）とし、境界で `normalize_object_part` / `format_object_part`
（`object_match_key` / `catalog_match_key`）で変換する。

| 経路 | #563 以降の挙動 |
|---|---|
| schema refresh（`fetch_schema_manifest` / `fetch_catalog` / 増分 merge / 永続化） | manifest・変更 object・merge・`NL2SQL_SCHEMA_OBJECTS` / `COLUMNS` / `CONSTRAINTS` の key を辞書上の名前にした。修正前は `UPPER` した名前を bind していたため、`Mixed_Case` の列定義を取得できず（同名表があればその定義を保存し）、`MIXED_CASE` と 1 行に潰れていた |
| 詳細 `GET /api/schema/objects/{owner}/{object_name}` | path は引用規則で解釈する（`/SALES/%22Mixed_Case%22` は引用名の表、`/sales/mixed_case` は従来どおり `SALES.MIXED_CASE`）。repository と cache は大文字小文字を区別して完全一致する。引用符が壊れた path は 400 |
| 一覧 `GET /api/schema/objects` / `GET /api/nl2sql/db-admin/objects` | `owner` は引用規則で解釈し、辞書上の owner と完全一致させる。`profile_id` の絞り込みは Profile の対象を辞書上の `(owner, name)` の組にして完全一致させる（Oracle は `JSON_TABLE` の `$.owner` / `$.name`）。keyset cursor は辞書上の名前を保持する（大文字化した cursor では同名表を読み飛ばす・重複する） |
| 詳細の代表値（`fetch_metadata_sample_values`） | owner / object / 列を引用規則で解釈し、辞書ビューの照合と `"..."` 引用に辞書上の名前を使う。修正前は引用符を外して大文字化し、大文字の同名表・同名列の値を取得していた |
| オントロジーの node ID | `stable_physical_id` に渡す名前を `physical_identity_part` にした。大文字化しても変わらない名前（`ORDERS`、`売上`）は **既存の ID・technical_name・schema fingerprint を変えない**。小文字を含む名前だけ `"Mixed_Case"` を ID に含め、`technical_name` も `SALES."Mixed_Case"` にする。metadata / 物理参照の owner・object・column は辞書上の名前を保持する |
| Profile の view・物理 scope の絞り込み・draft scope・質問補完の column policy | 引用規則の照合キーで対応付け、引用名の Profile を引用名の node に対応付ける（#561 の「対象にしない」扱いを解除） |
| オントロジーの AI 構築 | #563 の時点では schema context と LLM 出力の参照解決が名前を大文字化して照合していたため、小文字を含む引用名の表・列・関係を対象外（警告）にしていた。#573 で引用規則に対応し、この除外を解除した（下記「オントロジーの AI 構築・業務定義・SQL 接地（Issue #573）」） |
| 列単位の許可チェック（`allowed_objects.columns`） | 列名は引用規則の token で照合する。SQL の列参照は sqlglot の引用情報（`SqlColumnReference.owner_quoted` / `table_quoted` / `name_quoted`）で解釈する。`"Amount"` だけを許可したとき `SELECT AMOUNT` を拒否し、`AMOUNT` だけを許可したとき `SELECT "Amount"` を拒否する（修正前はどちらも `AMOUNT` として許可していた）。同名表を JOIN したときの表名修飾（`"Mixed_Case"."Amount"`）も引用規則で解決する |
| 画面 | スキーマ参照の列詳細は token で要求する。業務プロファイルのスキーマ別グループは owner を大文字化せず、`"Sales"` と `SALES` を別グループとして件数・一括選択する（表示は SQL と同じ表記）。オントロジーの物理名表示とグラフの cluster も大文字化しない |

#### 既存データの互換

- 引用が不要な名前（辞書上の名前が大文字化しても変わるものがない名前）は、保存値・キー・SQL・API 応答・オントロジーの ID が従来と同じ。schema / migration の変更なし。
- 修正前に保存された schema catalog の行は、小文字を含む名前も大文字化されている（列定義は大文字の同名表のもの、または取得できていない）。**DB 構造の全件再取得**で manifest の key が辞書上の名前に揃い、引用名の表は新しい行として取得される。大文字化された行は、大文字の同名表が無ければ削除され、あればその表の行として比較・更新される（targeted refresh は対象 object だけを更新する）。
- オントロジーは、再取得後の catalog から作る次の revision で引用名の node が別 node になる。修正前の revision で 1 node に潰れていた同名表の業務定義・mapping は自動では付け替えない（どちらの表を意図したか判別できないため、ID が変わらない大文字の表の node に残る）。
- API の入力: 引用なしの小文字（`/api/schema/objects/sales/orders`、`owner=sales`、列 `amount`）は従来どおり大文字として解釈する。辞書上の小文字の名前をそのまま渡していたクライアント（`owner=Sales`）は、`owner="Sales"` と引用して渡す必要がある。

### オントロジーの AI 構築・業務定義・SQL 接地（Issue #573）

#563 で残した「owner / object / column 名を大文字化して照合する」経路を、同じ引用規則にそろえた。
照合キーは `object_identity.py` の `object_match_key`（SQL・LLM 出力・業務定義の mapping など、SQL と同じ表記の入力）と
`catalog_match_key`（カタログ上の名前）で作り、SQL parser の識別子は `sql_identifier_token`（引用の有無）で解釈する。
`SALES."Mixed_Case"` と大文字の同名表 `SALES.MIXED_CASE`、列 `"Amount"` と `AMOUNT` は常に別のものとして扱う。

| 経路 | #573 以降の挙動 |
|---|---|
| AI 構築の schema context（`build_schema_context_from_catalog` / `build_schema_context`） | 名前は ontology の technical_name と同じ表記（`physical_identity_part`: 小文字を含む名前だけ `"Mixed_Case"`）で LLM に渡す。Profile の引用名の表・列・外部キー・ビュー依存を除外しない（#563 の警告「オントロジーの AI 構築の対象にできません（未対応）」を廃止）。system prompt で「schema_context の表記どおり、引用符と大文字小文字を保つ」ことを指示する |
| LLM 出力・Q/A SQL の参照解決（`_ScopeResolver` / `_SchemaContextLookup` / `merge_build_extractions`） | LLM 出力は SQL と同じ表記として解釈する（引用しない `SALES.Mixed_Case` は Oracle と同じく `SALES.MIXED_CASE`）。Q/A SQL の表・列・Join 条件は SQL parser の引用情報で解決し、Join 候補の裏付け確認も列名の token で行う |
| 業務定義の静的検証・実データ検証・公開版の scope（`ontology_definition_validation` / `ontology_sql_validation` / `ontology_definition_data_validation` / `ontology_published_context`） | `schema_objects` と mapping のキー、定義 SQL の表・列の解決、qualify 用 schema を照合キーにした。mapping を SQL の `"..."` に埋め込む前にカタログ上の名前へ戻す（修正前は `amount` を `"amount"` として小文字の別列を参照していた）。SQL 生成に渡す公開版の許可 object / 列も大文字化しない |
| 能力（`ontology_capabilities`）・接地 graph（`ontology_unified_model`）・Mermaid（`ontology_mermaid`） | SQL の表・列、mapping、物理 node の索引（`physical_node_index`）を照合キーで引く。業務定義から作る物理参照の owner / object はカタログ上の名前にそろえる |
| SQL の 3 者照合（`ontology_service`） | Profile 範囲外の表（`SQL_OBJECT_OUTSIDE_PROFILE`）、質問の解釈にない表、指標・dimension・filter の対象列を引用規則で照合する。`SqlSemanticGraph` の `referenced_columns` は引用符を保持する（`m."Amount"`）。引用されていない部分は SQL に書かれたままで、従来と同じ値 |
| 逆生成の列ラベル・DB 管理のコメント / アノテーション SQL 用サンプル（`service.py`） | 引用識別子を 1 つの識別子として扱い、論理名・代表値を完全一致の列から引く |
| 画面の SQL 接地（`sqlGrounding.ts`） | SQL の表・列は backend の引用情報（`owner_quoted` / `table_quoted` / `name_quoted`）、無い旧 artifact は引用符を保持した SQL の表記で解釈し、ontology node はカタログ上の名前で照合する（`dbObjectKeyTokens` / `sqlIdentifierToken` / `formatDbObjectPart`）。同名表を参照する SQL で別の表を強調しない |

互換: 引用が不要な名前は、schema context の表記・`schema_objects` のキー・node ID・technical_name・schema fingerprint が
従来と同じ（schema / migration の変更なし）。保存済みの業務定義の mapping（`APP` / `ORDERS`、小文字の `app` / `orders`）も
従来どおり解決する。小文字を含む引用名の表を AI 構築の対象にした Profile は、schema context が変わるため公開済み業務版の
再検証（`BUSINESS_SCOPE_CHANGED`）が必要になる。修正前に大文字の同名表として作られた業務定義は自動では付け替えない。

### 画面表示

表名・列名は SQL と同じ表記（引用が必要な部分だけ `"..."`）で表示する。一覧で `SALES.MIXED_CASE` と
`SALES."Mixed_Case"` は別の候補として並び、選択後の見出し・許可列・プレビュー SQL も同じ表記になる。
入力規則に反する識別子の API エラーは「… は有効な Oracle identifier で指定してください。大文字小文字の混在や
記号を含む名前は "Mixed_Case" のように二重引用符で囲みます。」と返す。

## SQL 実行画面の安全境界

AI 活用の `/direct-sql`（SELECT SQL を実行）は `search.view` で表示し、実行には `search.execute` を
要求する。`/api/nl2sql/execute` だけを使用し、SELECT/WITH 以外と複数 statement はサーバー側の
SELECT-only guard で拒否する。許可された SQL は、アプリの DB ユーザーの接続（system_admin 用・
非 system_admin 用に分けた pool。#904）で実行する。

データ準備の `/admin-sql`（管理 SQL を実行）は `settings.database.sql_execute` permission を持つ
管理者向け機能である。単一の SELECT/WITH は `/api/nl2sql/db-admin/execute` から同じ data pool の
`execute_select()` に収束する。非 SELECT または複数 statement は管理 SQL として扱い、
`ADMIN_EXECUTE` 確認語、RBAC、監査を通した上で application DB user の管理の接続から実行する。
確認不要の判定には通常の SELECT-only guard を再利用し、WITH で始まる更新文も管理 SQL として扱う。
管理 SQL の実行（SELECT を含む）と止めた文は、表 `NL2SQL_ADMIN_AUDIT_LOG` に件数の上限なく残す（#2264）。

データ準備の `/db-admin-chat`（DB 管理チャット。#2264）は `menu.db_admin_chat` を持つ利用者（既定は
system_admin だけ）の画面で、OCI Enterprise AI Direct で生成した SQL を、SELECT を含めて application DB
user の管理の接続で実行する。SELECT 以外は `ADMIN_EXECUTE`（1 つの対象の
DROP / TRUNCATE は対象の名前）の確認語を要し、方針で止める文（`ALTER SYSTEM`・`DROP USER`・動的 SQL・
外部への通信のパッケージなど）はコードの規則で止める。API は画面だけで、MCP・API キーからは使えない。
会話の履歴（#2319。表 `NL2SQL_DB_ADMIN_CHATS` / `NL2SQL_DB_ADMIN_CHAT_TURNS`）は「AI 活用」と同じ見え方で、
system_admin は全員の会話、それ以外は自分の会話だけ（他人の会話は 404）。system_admin が他の利用者の会話にできるのは
閲覧と削除だけで、続きの質問・実行・SQL の置き換え・名前の変更は持ち主だけ（403。#3244）。
詳細は [db-admin-chat.md](./db-admin-chat.md)。

NL2SQL job の生成済み SELECT 実行は、actor の種類で pool を分けたアプリの接続を使う（#904）。

OCI Enterprise AI で生成した SQL と、利用者が入力・編集した SQL は、アプリの安全確認（SELECT-only guard・
危険な関数・bind の placeholder・未承認の関数・他製品のシステムの表・業務プロファイルの表と列の範囲）を
すべて掛ける（[sql-chat.md](./sql-chat.md)）。

## 主要な安全境界

- session token は 256-bit random value、DB には SHA-256 digest だけを保存する。
- session cookie は HttpOnly、CSRF は cookie/header/server digest の三者一致を要求する。
- session / CSRF の cookie（`pr_session` / `pr_csrf`。共通 `.env` の `PLATFORM_AUTH_*_COOKIE_NAME`）は 3 製品で共有し、1 回のログインで全製品を使える。ログアウトは共有の session を失効させ、ログインのときはブラウザが持っていた古い session を失効させる。権限は製品ごと（#1565）。
- password は Argon2id で保存し、平文 password、hash、session token、Oracle secret を応答・監査へ出さない。
- 業務データの接続は actor の種類（system_admin / 非 system_admin）で pool を分け、同じ接続を共有しない（#904）。
- background job は actor UUID を保存し、worker 実行時に user が active であることを再確認する。
