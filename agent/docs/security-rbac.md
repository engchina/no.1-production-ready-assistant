# ログインと権限（#215 / #750）

Agent の画面は、RAG / NL2SQL と同じ共通認証（platform の `pr_system_settings.auth`）でログインします。
ユーザー・ロール・セッションは 3 製品で共有する `PLATFORM_*` テーブル、ロールに付ける Agent の権限と対象範囲（エージェント）は
`AGENT_ROLE_*` テーブルに保存します。local でも production でも同じ Oracle のテーブルを使います（#750）。
Agent 独自の header / JWT / 外部 policy の認可（旧 `AGENT_RBAC_*`）は #750 で削除しました。

## 1. 認証モード（`PLATFORM_AUTH_MODE`）

| 値 | 画面と API | ユーザー・ロール | 用途 |
|---|---|---|---|
| `local`（既定） | ログインを省略し、全権限のローカル利用者として使う（DB セッションを作らない） | Oracle の `PLATFORM_*` / `AGENT_ROLE_*`（RAG / NL2SQL と同じ） | 開発 |
| `production` | 共通認証のログインが必要（Cookie のセッション）。Cookie のないリクエストは **401**（`X-Agent-Roles` などの header は使わない） | 同上 | 本番（Compute の配備は常にこちら） |

- local でもユーザー管理・ロール管理・権限管理は共通 DB を読み書きします。共通 `.env` の `PLATFORM_ORACLE_*` が必要です
  （RAG / NL2SQL と同じ。旧版は local だけ InMemory で、画面が 0 件になり再起動で消えていました）。
- RAG / NL2SQL への呼出しはサービストークンです（§7）。

- 構成管理者（ログインユーザー ID は `system_admin` 固定）のパスワードは共通 `.env` の `PLATFORM_ADMIN_LOGIN_USER_PASSWORD`。
  3 製品で同じ値です。最初は構成管理者でログインし、ユーザー管理・ロール管理・権限管理で利用者を作ります。
- 認証ポリシー（セッションの有効期限・ロック・ログインの試行の回数の制限・パスワード長・Argon2 のコスト・`PLATFORM_AUTH_COOKIE_SECURE`）は共通 `.env` の `PLATFORM_AUTH_*`。
  ログインの失敗が多すぎる送信元（ログイン ID と送信元 IP の組・送信元 IP ごと）には、構成管理者を含めて `429 SECURITY_RATE_LIMITED` を返します（#1087。`PLATFORM_AUTH_LOGIN_ATTEMPT_*`）。
  失敗の回数は共通のテーブル `PLATFORM_LOGIN_ATTEMPTS` で 3 製品・全 worker が共有して数えます（#1173。ログイン ID・送信元 IP は HMAC だけを保存。
  テーブルが無い・DB の停止中はプロセス内で数える）。詳細と既存環境の更新手順は [platform/docs/deployment.md](../../platform/docs/deployment.md) の「ログインの試行の回数の共有（#1173）」。
- ログインのセッションは 3 製品で共通です（#1565）。Cookie 名は共通 `.env` の `PLATFORM_AUTH_SESSION_COOKIE_NAME`（既定 `pr_session`）/
  `PLATFORM_AUTH_CSRF_COOKIE_NAME`（既定 `pr_csrf`）で、1 台に配備した `/rag/`・`/nl2sql/`・`/agent/` は同じ Cookie を読むため、
  1 つの製品でログインすればほかの製品でもログイン済みになります（構成管理者 `system_admin` の token も 3 製品で共通）。
  ログアウトは共有のセッションを失効させ、開いているほかの製品・タブは `BroadcastChannel` で知らせを受けてログイン画面へ移ります
  （受けなくても次の API の 401 で移る）。ログインし直すと、ブラウザが持っていた古いセッションは失効します。
  権限はこれまでどおり製品ごとで、ロールに Agent の権限が無ければ 403（権限が無いことの画面）です。
- テーブルは 運用設定 > システムテーブル の「作成・更新」か、`cd agent/backend && uv run python -m app.cli.agent_system_schema --initialize` で作ります（`PLATFORM_ORACLE_*` で接続し、
  `PLATFORM_*` → `AGENT_ROLE_*` → 組み込み SYSTEM_ADMIN ロールの順に冪等に適用。ユーザーは作りません）。
  未適用のままログインすると `409 SECURITY_SCHEMA_MIGRATION_REQUIRED` になります。

## 2. 判定の順序

`/api` の全 route は `app.security.dependencies.authorize_api_request` を通ります。

1. local: ローカル利用者（全権限・対象範囲の制限なし）を `request.state.principal` に入れて通す。
2. production の公開 path: `/health`・`/ready`・`/ready/database`（画面の DB ゲート。#325）・`/auth/login`。
3. production で session Cookie あり: セッションを検証し、更新系（GET / HEAD / OPTIONS 以外）は `X-CSRF-Token` header と CSRF Cookie を照合し、
   強制パスワード変更中は `/auth/me`・`/auth/logout`・`/auth/password/change` 以外を 403 にし、権限 manifest の権限を確認します
   （manifest に登録のない API は 403）。Cookie が不正・期限切れなら 401。
4. production で session Cookie なし: 401（RAG / NL2SQL と同じ。header の自己申告は使わない）。

その後、router の判定（`require_viewer` などのロール、エージェントの対象範囲）が同じ利用者で動きます。
利用者（Cookie のセッションの利用者か、local のローカル利用者）から `ActorPolicy(roles, agent_ids)` を作り、利用者がいなければ何も許可しません。

## 3. 権限カタログ

### capability（従来の 5 ロールに対応）

グループは NL2SQL / RAG と同じ「参照権限 / 実行権限 / 管理権限」で、権限管理ではナビのメニュー権限の後ろに並びます（#791）。

| グループ | capability | 名前（権限管理） | 従来のロール | 内容 | 暗黙に含むメニュー |
|---|---|---|---|---|---|
| 参照権限 | `agent.runs.view` | 実行履歴の参照 | viewer | 利用できる範囲の Run・イベント・成果物の閲覧 | 実行履歴 |
| 参照権限 | `agent.audit.view` | 監査ログの参照 | auditor | Run の監査・ツール呼出し履歴・trace event（閲覧を含む） | 監査ログ |
| 実行権限 | `agent.runs.operate` | 業務 Agent の実行 | operator | チャットと、Run の作成・取消・再開・再実行（閲覧を含む） | 実行履歴・チャット |
| 実行権限 | `agent.approvals.decide` | 承認の判断 | approver | 自分の Run の承認・却下（閲覧を含む。ほかの利用者の Run の承認は system_admin だけ。#1889・#1928） | 承認 |
| 管理権限 | `agent.agents.build` | 自分の業務 Agent の作成 | （なし） | 自分の業務 Agent（個人の業務 Agent）の作成・編集・下書きで実行・評価・公開・削除と、共有の申請（#2523）。ほかの業務 Agent・スキル・設定は変えられない。持ち主の判定は router | 業務 Agent・品質評価（と、業務 Agent の実行） |
| 管理権限 | `agent.admin` | Agent 管理 | admin | 業務 Agent・スキル・運用設定の変更とすべての操作（システム設定のメニューも暗黙に含む）。対象範囲の制限なし | ユーザーとロール・権限管理以外のすべてのメニュー |

### メニュー権限（`agent/frontend` のナビと同じ並び）

グループ・名前・並びはサイドナビと同じです（#567。上に一般の利用者の画面、下に管理者の画面。#791）。

| グループ | コード |
|---|---|
| AI 活用 | `menu.chat`（チャット。`agent.runs.operate` が含む。#768）/ `menu.runs`（実行履歴）/ `menu.approvals`（承認） |
| Agent 構築 | `menu.agents`（業務 Agent）/ `menu.skills`（スキル）/ `menu.automations`（自動実行。#784） |
| 改善・運用 | `menu.evaluation`（品質評価。#776。評価の Run は始めた利用者の Run なので、業務 Agent の対象範囲も確かめる。下書きの評価は下書きの実行と同じく `agent.admin` だけで、それ以外の利用者の既定は公開中の版。#2522） / `menu.feedback`（フィードバック。#774。集計は Run の一覧と同じく利用できる業務 Agent の Run だけ。チャットの回答の評価は `agent.runs.operate` を持つ会話の本人が付け、管理者の評価（`PUT /api/runs/{id}/admin-review`）は `agent.admin` がだれの回答にも本人の評価とは別に付ける） / `menu.usage`（利用状況。#772。集計は Run の一覧と同じく利用できる業務 Agent の Run だけ）/ `menu.audit`（監査ログ） |
| セキュリティ設定 | `menu.security_permissions` |
| ユーザーとロール（3 製品共通） | `menu.security_users` / `menu.security_roles` |
| 運用設定 | `menu.settings_system_tables` / `menu.runtimes`（実行環境）/ `menu.settings_external_mcp`（MCP 接続） |
| システム設定（3 製品共通） | `menu.settings_oci` / `menu.settings_upload_storage` / `menu.settings_model` / `menu.settings_database` / `menu.settings_appearance` |

権限コードは保存値なので、ナビの名前・グループを変えてもコードは変えません（#791 で「Control Plane」グループを分けたときも同じ）。

画面の表示にはメニュー権限、実データの閲覧・操作には capability が必要です。たとえば実行履歴の画面は `menu.runs` で開けますが、
Run の一覧・詳細は `agent.runs.view`（または operate / decide / audit / admin）がないと 403 です。capability は関連メニューを
暗黙に含むため、capability だけを付けたロールでも画面を開けます。

ダッシュボード（`menu.dashboard`・グループ「概要」）は廃止しました（#262）。`/` は画面を持たず、ナビの並び順で最初に開ける画面へ移します
（未知の URL・ログイン後はチャットを開ければチャット。#791）。既存ロールに残る `menu.dashboard` はシステムテーブルの作成・更新（migration）が削除します（§8）。
削除前でも、カタログにないコードは実効権限・権限管理の表示から除かれ、権限管理で保存すると消えます。

### manifest（抜粋。正本は `backend/app/security/permissions.py`）

API は `(method, route template)` ごとに登録し、登録のない API は拒否します（前方一致では割り当てない。
共通のユーザー管理・ロール管理の API も同じ。#503）。

| API | 必要な権限（いずれか） | router の追加の判定 |
|---|---|---|
| `GET /runs` | `menu.runs` / `menu.approvals` | viewer 以上・対象範囲で絞る・system_admin 以外は自分の Run と自分の会話の Run だけ（§4.1）。要約のページ（#1897） |
| `GET /approvals`・`GET /approvals/{id}`（#1897） | `menu.runs` / `menu.approvals` | viewer 以上・対象範囲で絞る・system_admin 以外は自分の Run の承認だけ（§4.1。範囲外・ほかの利用者・無い承認の詳細は 404） |
| `POST /agents/draft-suggestion`（#2519。業務の説明から下書きを提案。保存しない） | `agent.admin` / `agent.agents.build` | 候補は有効なスキルと、編集者が使えるプロファイル（データの範囲の候補と同じく編集者のサービストークンで取得）。候補に無い ID・形の正しくない出力の項目はコードで外す |
| `POST /agents`・`PATCH`・`DELETE /agents/{id}`・`POST /agents/{id}/publish`・`POST /agents/{id}/versions/{version}/restore`（#770・#2523） | `agent.admin` / `agent.agents.build` | `agent.agents.build` だけの利用者は自分の個人の業務 Agent だけ（ほかは 403、見えない Agent は 404）。作った Agent の持ち主は作った人（Agent 管理が作れば組織の業務 Agent）。下書きで実行（`POST /runs` の `draft=true`）と下書きの評価、下書きの会話の一覧（`GET /threads?draft=true`。編集画面の「試す」。#2515）も Agent 管理と持ち主だけ。会話は始めた Run の種類（下書き / 公開中の版）に固定し、種類をまたぐ続きは 409 |
| `POST /agents/{id}/share-request`（#2523） | `agent.admin` / `agent.agents.build` | 持ち主だけ。公開した個人の業務 Agent の共有を申請する |
| `POST /agents/{id}/share-request/approve`・`reject`（#2523） | `agent.admin` | 承認で組織の業務 Agent（持ち主なし）になる。却下は理由（必須）を持ち主に返す。承認前の個人の Agent はロールに割り当てられない（`PUT /security/roles/{id}/access` が 409） |
| `GET /runs/{id}`・`/audit`・`/artifacts*`・`/figure-url` | `menu.runs` / `menu.approvals` / `menu.audit` | viewer 以上（監査は auditor）・ほかの利用者の Run は 404（§4.1。自分の会話の Run は読める）・範囲外は 403 |
| `GET /runs/{id}/events`（SSE） | `menu.runs` / `menu.approvals` | viewer 以上・ほかの利用者の Run は 404・範囲外は 403 |
| `GET /runs/{id}/trace`（チャットと実行の詳細の処理の経過。共通の span の表の snapshot。#1359・#3226） | `menu.chat` / `menu.runs` / `menu.approvals` / `menu.audit` | viewer 以上・ほかの利用者の Run は 404・範囲外は 403。span は Run を実行した利用者の span だけ（system_admin は同じ trace の全員の span） |
| `POST /runs`・`/runs/{id}/cancel`・`resume`・`replay`・`/runs/{id}/confirmations/{id}`・`PUT /runs/{id}/feedback` | `agent.runs.operate` / `agent.admin` | operator・ほかの利用者の Run・会話は 404（system_admin は全員。§4.1）・範囲外は 403・新しい Run は操作した利用者の Run |
| `GET /threads`・`GET`・`PATCH`・`DELETE /threads/{thread_id}`（チャットの会話。#768） | `menu.chat` | 会話の持ち主の会話だけ（別の利用者の会話は 404）。system_admin は全員の会話（利用者の表示名・送った人の表示名つき。§4.1）・範囲外の Agent の会話は出さない |
| `POST /approvals/{id}/decision` | `agent.approvals.decide` / `agent.admin` | approver・自分が実行した Run の承認だけ（ほかの利用者の Run は、自分の会話の Run でも 404。system_admin は全員。§4.1）・範囲外は 403・決定者は利用者 |
| `GET /audit/tool-calls(.csv)` | `menu.audit` | auditor・範囲で絞る |
| `GET /agents` | `menu.agents` / `menu.runs` | 利用できるエージェントだけ |
| `GET /runtime/status` | `menu.runtimes` / `menu.agents` | 組み込み Runtime の状態（API key は出さない） |
| 業務 Agent・スキル・Agent 固有の設定（MCP 接続）の変更 | `agent.admin` | admin |
| システム設定（OCI 認証・アップロード保存先・モデル・データベース）の保存・接続テスト・ADB 操作 | 各メニュー（`menu.settings_oci` / `menu.settings_upload_storage` / `menu.settings_model` / `menu.settings_database`。RAG / NL2SQL と同じ割り当て） | 同じメニュー権限（`require_system_settings_write`） |
| 設定の GET（MCP 接続（Skill の画面からも読む）・ツールの取得、システム設定） | 各メニュー（MCP 接続は `menu.settings_external_mcp`。`menu.settings_external_rag` / `menu.settings_external_nl2sql` は #757 で廃止し、migration 004 が既存ロールから消す） | — |
| `GET /tools` | `menu.audit` / `agent.admin` | — |
| `GET /observability/status`・`GET /settings/trace-policy` | `menu.audit`（画面からは使わない。運用スクリプトの確認用） | — |
| ナビに出さない設定（ツール権限） | `agent.admin` | — |
| ユーザー管理（`GET`・`POST /security/users`、`GET`・`PATCH`・`DELETE /security/users/{user_uuid}`、`POST /security/users/{user_uuid}/disable`・`enable`・`reset-password`・`unlock`） | `menu.security_users` | 共通の昇格防止 |
| ロールの参照（`GET /security/roles`・`GET /security/roles/{role_id}`） | `menu.security_users` / `menu.security_roles` / `menu.security_permissions` | — |
| ロールの変更（`POST /security/roles`、`PATCH`・`DELETE /security/roles/{role_id}`、`POST /security/roles/{role_id}/archive`・`restore`） | `menu.security_roles` | — |
| `GET /security/permissions`・`/security/access-targets/agents`、`PUT /security/roles/{id}/access` | `menu.security_permissions` | 昇格防止 |

## 4. 対象範囲（エージェント）

- ロールにエージェント ID を割り当てます。利用者の範囲は、有効なロールの割り当ての和集合です（割り当てがなければ何も見えない）。
- SYSTEM_ADMIN・構成管理者・`agent.admin` を持つ利用者は制限なし。`agent.admin` を含むロールの対象リストは保存時に空へ正規化します。
- エージェントを削除すると、全ロールの割り当て（`AGENT_ROLE_AGENTS`）からも外します。後始末に失敗して残った ID は、
  権限管理の保存で黙って外します（新しく足す未知の ID だけ 400。#750）。
- 検索・回答プロファイルは Agent の対象範囲にしません（#750）。RAG のツールは Run の利用者を `sub` にしたサービストークンで呼ぶため、
  検索・回答プロファイルの判定は RAG の権限管理（`RAG_ROLE_SEARCH_ANSWER_PROFILES`）がそのまま行います。旧版の `AGENT_ROLE_SEARCH_ANSWER_PROFILES` は使いません
  （テーブルの削除はシステムテーブルの Issue #751）。
- 対象範囲が制限された利用者の `GET /observability/events` は、範囲内の Run の event だけを返します。

### 4.1 「AI 活用」の Run・会話の見え方（#1889・#1954・#3244）

3 製品共通の規則（[backend-standard.md](../../platform/docs/backend-standard.md)「「AI 活用」の下の機能のデータの見え方」）:
**system_admin は全員のデータ、それ以外の利用者は自分のデータだけ**。判定は `pr_system_settings.auth.visibility`。

- Run の持ち主は Run を実行した利用者（actor。`RunState.created_by_user_uuid`）。自動実行の Run は実行する利用者（`run_as_user_uuid`）、品質評価の Run は評価を始めた利用者。持ち主の無い古い Run は system_admin だけが見られる。
- 会話の持ち主は会話を始めた利用者（`RunState.thread_owner_user_uuid`。記録の無い既存の Run は会話の最初の Run の `created_by_user_uuid`）。#3244 より前に system_admin が続けた往復があっても、会話の持ち主は変わらない。
- 「AI 活用」の API（実行履歴の `GET /runs`、Run の詳細・監査・成果物・図の URL・イベント・処理の段階、取消・再開・再実行・回答前の確認への回答・評価、チャットの会話）は、自分が実行した Run・自分の会話の Run か system_admin だけ（system_admin が他の利用者のデータにできる操作は下の「system_admin の操作」）。ほかの利用者の Run は 404 で、存在を漏らさない。対象範囲（業務 Agent）の判定はその上に重ねてかかる（自分の Run でも範囲外は 403）。
- `agent.admin`（Agent 管理）・`agent.audit.view`・`agent.approvals.decide` は、「AI 活用」の画面で全員を見る理由にしない。
- **実行履歴（#1954。3 製品で同じ規則）**: 行は実行した人と会話の持ち主の両方に出る（system_admin は全件）。一般の利用者には、自分が実行した Run と、自分の会話で他の人（system_admin）が実行した Run だけ（判定は `can_access_run` と同じ範囲。Agent は in-memory の store を router の `_run_visible_to` で絞る）。
  - API（`GET /runs` の行・`GET /runs/{id}`・会話の Run）: `executed_by_me`・`executor_display_name`（実行した人の表示名。見る人を問わず返す。引けなければ空）・`can_decide`（待ちの操作をできるか）。詳細と会話の Run は `owned_by_me`・`conversation_owner_display_name`（会話の無い Run は null）も返す（`RunViewer` / `RunView`。保存はしない）。
  - 絞り込み `GET /runs?executor=all|mine|others`（既定 `all`。`run_executor_matches`。`total` も同じ条件、`unfiltered_total` は見られる全件）。
  - 画面: 「実行者」の列（全員。system_admin の旧「利用者」の列を置き換えた。system_admin には `RunsData.creator_display_names` も返す）、実行した人が自分でない行の目標の下に「〈名前〉 が実行」、ツールバーに「実行者で絞り込み」（すべて / 自分が実行 / 他の人が実行。作業状態 `listFilter.runsExecutor`）、詳細に「実行者」「会話の持ち主」。
  - 利用量・API キーの上限・監査は実行した人（`created_by_user_uuid`）に数える。
- **承認（ユーザーの指示による設計の変更）**: 承認の権限（`agent.approvals.decide`）を持つ利用者は、自分が実行した Run の承認だけを見て判断する。以前は、業務 Agent の範囲のほかの利用者の Run も判断できた。ほかの利用者の Run の承認は system_admin だけが判断する。承認したツールは Run の利用者の権限（サービストークンの `sub`）で実行するため、自分の会話でも system_admin が実行した Run の承認は判断しない（一覧・詳細・決定は 404。画面は判断のボタンを出さない）。#3244 より前に system_admin が他の利用者の会話で始めた Run の承認は、system_admin の承認の一覧に出る（#1954）。
- **持ち主の側の待ち（#1954。#3244 より前のデータ）**: 再開・Agent 自身の確認への回答も承認と同じく実行した人と system_admin だけ（`can_decide=false`）。持ち主のチャット・実行の詳細は、system_admin が実行して承認・確認の待ちで止まった Run に「システム管理者（〈名前〉）の承認・回答を待っています」（info の Banner）を出し、操作のボタンは出さない（実行履歴の行メニューも承認・却下・再開を出さない）。チャットの「自分で質問し直す」は、その Run を止め（取消は持ち主もできる）、同じ質問を同じ会話で送る。新しい Run は持ち主の権限（`created_by_user_uuid`・業務 Agent の範囲・サービストークンの `sub`）で動く。持ち主の「再実行」（`POST /runs/{id}/replay`）も操作した持ち主の権限で、新しい会話の Run を作る。
- **system_admin の操作（ユーザーの指示による設計の変更。#3244。#1928 の「本人と同じすべての操作」を置き換えた）**: system_admin が他の利用者の会話・Run にできるのは、閲覧と運用の操作だけ。
  - 閲覧: 会話の一覧・詳細、Run の詳細・処理の経過（`GET /runs/{id}/trace`）・成果物・イベント、実行履歴。
  - 運用の操作（今どおり）: 取消（`POST /runs/{id}/cancel`。イベントの `cancelled_by`）、会話の削除（`DELETE /threads/{id}`）、承認の判断（`POST /approvals/{id}/decision`。`decided_by`）と再開（`POST /runs/{id}/resume`。ログ `agent_run_resume_requested` の `requested_by`）、回答前の確認をやめる（`POST /runs/{id}/confirmations/{cid}` の `action=cancel`）。承認・再開の続きは Run を実行した人の権限で動く（`_run_approvable_by`。実行した人と system_admin）。
  - 再実行（`POST /runs/{id}/replay`）は会話を引き継がず、操作した system_admin 自身の新しい会話の Run を作る（持ち主の会話には足さない）ので、今どおりできる。
  - **内容を足す操作は持ち主だけ**（403。`can_add_user_content`・`OTHERS_CONTENT_FORBIDDEN_MESSAGE`）: 続きの質問（`POST /runs` の `thread_id`。router の `_may_continue_thread`）、回答前の確認への答え・確認せずに回答（`POST /runs/{id}/confirmations/{cid}` の `answer` / `skip`）、評価（`PUT /runs/{id}/feedback`）、会話の名前の変更（`PATCH /threads/{id}`）。Run の持ち主は会話の持ち主（会話の無い Run は実行した人。`_run_content_addable_by`）。system_admin の権限・業務 Agent の範囲・サービストークンの `sub` で作った回答・ツールの出力が持ち主の会話に入り、持ち主が権限の外の内容を見るのを防ぐ。調べ直すときは system_admin が自分の会話で質問する。
  - 既存のデータ: #3244 より前に system_admin が持ち主の会話で続けた往復は消さない。会話の詳細は持ち主以外が送った往復に送った人の表示名（`ThreadData.sender_display_names`。Run の ID → 表示名）を返し、画面は質問の吹き出しの上に「〈名前〉 が送信」を出す（共有 UI の `ChatTurn` の `questionSender`）。持ち主はその往復も評価できる（自分の会話の内容）。
  - 画面: system_admin が他の利用者の会話（`ThreadData.owned=false`）を開くと、入力欄の代わりに閲覧だけの案内（3 製品共通の `ChatReadOnlyNotice`。info の Banner「他の利用者の会話です（閲覧のみ）」。文は `OTHERS_CONTENT_FORBIDDEN_MESSAGE` と同じ）を出し、回答の作成中・承認待ちなら案内の中に「停止」を置く。回答前の確認のカード・確認の取り消しの操作・評価・「自分で質問し直す」は出さない。承認の判断のボタンと会話の削除は出す。#1954 の送る前の案内（「この会話の持ち主にも…」）は外した。会話の履歴の行の「名前の変更」は出さない（共有 UI の `useChatHistoryActions` の `canRename`。削除は出す）。
  - 会話の添付（`/api/chat-attachments`。platform の共通の API）: 会話の添付の一覧は閲覧なので返し、会話への添付（`POST` の `conversation_id`）は内容を足す操作なので 403（持ち主の分からない会話も 403）。
  - 外部の MCP（API キー）も同じ: キーの持ち主が system_admin なら他の利用者の Run を読め（`agent_get_run`・`agent_get_run_observation`）、止められる（`agent_cancel_run`。運用の操作。#3240）が、会話の続き（`agent_chat` の `thread_id`）と確認への答え（`agent_answer_confirmation`）は 403 の `OTHERS_CONTENT_FORBIDDEN`。キーの業務 Agent の範囲（`resource_scopes["agent"]`）と回数の上限はそのままかかる。
- チャット: 一般の利用者は自分の会話だけを見て続ける（他人の会話の詳細・送信・操作は 404）。system_admin の会話の一覧は利用者の表示名・`owned` を返す。
- 規則の外（「改善・運用」）: フィードバック（`GET /feedback*`・管理者の評価 `PUT /runs/{id}/admin-review`）・品質評価のケースへの追加（`GET /runs/{id}/evaluation-case`）・利用状況・監査ログ（`GET /audit/tool-calls*`・`/observability/events`）は、従来どおり権限と業務 Agent の範囲で判定する。

### 4.2 保存した根拠の再表示（#3252）

Run は RAG から受け取った根拠（ツールの結果・`tool.completed` のイベント・成果物「検索の根拠」「回答の根拠」）を保存している。出すたびに、**画面を見ている利用者の今の権限**で確かめ直す（OWASP API1: 物を読むたびにその時点の権限で認可する。権限を外したら次に読むときから効く）。

- 対象の入口: `GET /runs/{id}`・`GET /threads/{id}`（会話の各 Run）・`GET /runs/{id}/events`（`follow` の無い保存したイベント）・`GET /runs/{id}/artifacts`・`/artifacts/{artifact_id}`・MCP の `agent_get_run`（`sources`）・`agent_get_run_observation`（`evidence`・`tool_calls[].evidence_count`。Run の状態・呼ばれた時期に関わらず毎回）。実行中の購読（`events?follow=true`）と、実行の直後の応答（`agent_chat`）は、見る人が今実行した結果なので確かめない。
- 確かめ方: Agent の backend が組み込みの接続 `rag` の内部のツール `rag_readable_documents`（`tools/list` に出さない。モデルには渡らない）を、`sub` = 見る人・claim `purpose=evidence_access` のサービストークンで呼び、根拠の文書のうち今見られる文書を受け取る（図・原文の URL と同じく Run の利用者の権限を借りない。system_admin が見るときは system_admin の権限）。根拠の無い Run と、見えない Run（404）では RAG を呼ばない。仕組みは `features/agent/evidence_access.py`。
- 見られない文書の根拠は、抜粋・場所・文書名・参考情報の原文（`references` の `quote`）を出さず、応答の `withheld_evidence_count`（`RunViewer`。MCP の `agent_get_run` / `agent_get_run_observation` は出力の `withheld_evidence_count`）に数だけを返す。画面は回答の根拠の折りたたみに「権限の無い根拠 N 件」（共有 UI の `EvidenceWithheldNote`。RAG と同じ）を出す。見られない文書 1 つを読んだツールの結果（`rag_read_source` など）は `{"evidence_withheld": true}` にする。保存した Run は変えない（応答の写しだけ）。
- 確かめられないとき（`rag` の接続が無い・サービストークンでない・RAG の失敗）は、RAG の根拠をすべて見られないものとして扱う（fail closed。ログ `agent_evidence_access_check_failed`）。会話の添付の根拠（`attachment:`）は RAG の文書ではないので確かめない。
- **既知の限界**: 回答の本文は保存した文のまま出す（その資料から作った文で、本文から該当の部分は消せない）。
- 入口 × 主体 × 範囲の行列は `tests/test_evidence_read_authz.py`（RAG は `rag/backend/tests/test_evidence_read_authz.py`）。

## 5. 権限昇格の防止

- `PUT /security/roles/{id}/access`: SYSTEM_ADMIN 以外は、自分が持たない権限・自分の範囲外のエージェントを
  ロールに足すと 403。新しく足す存在しないエージェント ID・未登録の権限コードは 400。組み込み / アーカイブ済みのロールは 409。
- ユーザーへのロールの割り当て・ロールの復元は、ロールの権限と対象範囲が操作者に収まる場合だけ（共通の判定と、他製品の権限テーブルの判定）。

## 6. Run のイベントの配信と承認の決定者

- Run のイベントの push は SSE（`GET /runs/{id}/events`）だけで、取消・再開・承認の判断は REST の API です。WebSocket は持ちません（#2260）。
  どちらも `/api` の HTTP の認可（Cookie のセッション・更新系の CSRF・manifest）を通ります。
- 承認の決定者（`decided_by`）は利用者の `login_user_id` です（HTTP の body の値は使わない。local は `local`）。
- 承認の「承認して今後は自動で実行」（`POST /api/approvals/{approval_id}/decision` の `always_allow=true`。#1832）はツール権限を変えるので、`agent.approvals.decide` に加えて `agent.admin` が要る（ない利用者は 403 で、承認もしない）。ツールのポリシーは全業務 Agent・全利用者に効く。Run のイベントとログ `agent_tool_policy_always_allow` に残す。
- MCP 接続の「ツールの注釈（readOnlyHint）を信頼する」（`trust_tool_annotations`。#1832・#1875）は、外部の接続では既定でオフです（新しい接続も、値を保存していない既存の接続も）。MCP の仕様どおり、信頼しないサーバーの注釈は untrusted として扱い、`readOnlyHint=true` のツールも承認を求めます（ツール権限で個別に指定しない限り）。オンにすると読み取り専用と申告したツールが承認なしで動くので、変えられるのは `agent.admin` だけで、画面は確認のダイアログでその注意を示します。標準の接続（RAG / NL2SQL）は常に信頼します（API でオフにすると 400）。

## 7. 画面以外からの呼出し

- 外部 Runtime からの呼出し（Binding の MCP）は #754 で削除しました（実行は組み込み Runtime）。
- 外部の MCP クライアント → Agent（#1874）: `POST /api/mcp` を共通の API キー（`Authorization: Bearer prak_…`。
  表 `PLATFORM_API_KEYS`、使える製品に `agent`）で呼びます。キーを発行した利用者として、画面と同じ capability
  （`agent_list` / `agent_get_run` / `agent_get_run_observation` は `agent.runs.view` / `agent.runs.operate` / `agent.admin`、`agent_chat` /
  `agent_answer_confirmation` / `agent_cancel_run` は `agent.runs.operate` / `agent.admin`）と対象範囲（使える業務 Agent）で判定し、キーに
  業務 Agent の範囲（`resource_scopes["agent"]`）があればさらに絞ります（範囲の外は 403 の `AGENT_OUT_OF_KEY_SCOPE`）。
  Run と会話は画面と同じ規則（§4.1）で、自分が実行した Run・自分の会話だけを読み、続け、答えられます（他人の Run は
  `RUN_NOT_FOUND`、他人の会話は `THREAD_NOT_FOUND`）。キーの持ち主が system_admin なら全員の Run を読めますが、他の利用者の
  会話の続きと確認への答えは 403 の `OTHERS_CONTENT_FORBIDDEN` です（#3244。キーの業務 Agent の範囲はそのまま）。MCP の Run は承認の要るツールを呼びません。
  キーの発行・一覧・削除は `/api/account/api-keys*`（ログインだけ）、全員のキーは `/api/security/api-keys*`（Agent 管理）。
  Agent 独自の旧 API キー（`/api/settings/api-keys`・`menu.settings_api_keys`・`AGENT_CONTROL_PLANE_ITEMS` の kind
  `api_key`）は #1809 で削除し、使いません（migration `20261014_011_remove_agent_mcp_api_keys`）。
- Control Plane → RAG / NL2SQL: 呼び先の `POST /api/mcp` を、Run の利用者を `sub` にした短命のサービストークンで呼びます
  （[agent-control-plane-design.md §4.1](./agent-control-plane-design.md#41-rag--nl2sql-の-mcp233)）。
- 旧版の `AGENT_RBAC_*`（`X-Agent-Roles` などの header・HMAC 署名 header・JWT bearer・外部 policy URL・
  `AGENT_RBAC_ACTOR_POLICIES_JSON`）は #750 で削除しました。設定が `.env` に残っていても読みません。

## 8. 既存環境の更新手順（#215）

1. 共通 `.env`（`platform/.env`）に `PLATFORM_ADMIN_LOGIN_USER_ID=system_admin` と `PLATFORM_ADMIN_LOGIN_USER_PASSWORD`
   （12〜30 文字、大文字・小文字・数字を含み、`admin` と `"` を含まない）があることを確認する（RAG / NL2SQL と共通）。
   HTTPS で配信している場合は `PLATFORM_AUTH_COOKIE_SECURE=true`。
2. `agent/backend/.env` に `PLATFORM_AUTH_MODE=authenticated` を書く。
3. テーブルを作る: `cd agent/backend && uv sync --locked --no-dev && uv run python -m app.cli.agent_system_schema --initialize`（画面の 運用設定 > システムテーブル でもよい）。
4. Nginx の Basic 認証を廃止する（以前の stack で配備した instance）。`/etc/nginx/sites-available/production-ready-agent` から
   `auth_basic` / `auth_basic_user_file` を削除し、`location /api/` の `proxy_set_header Host` を `$http_host` にして
   `sudo nginx -t && sudo systemctl reload nginx`。`/etc/nginx/production-ready-agent.htpasswd` と
   `/u01/aipoc/props/basic_auth_*` は削除してよい（`init_script.sh` を再実行しても同じ設定になる）。
5. `sudo systemctl restart production-ready-agent-backend`（開発は backend の `uv run` を起動し直す）。
6. `system_admin` でログインし、ロール管理でロールを作り、権限管理で Agent の権限・エージェントを割り当て、ユーザーに付ける。
7. （#262 以降に更新する環境）手順 3 のシステムテーブルの作成・更新を再実行し、既存ロールに残る廃止した権限コード
   `menu.dashboard` を削除する（何度実行してもよい。出力の `retired_permission_rows` が削除した行数）。削除しないと、RAG / NL2SQL の
   ユーザー管理から非 SYSTEM_ADMIN の管理者がそのロールを割り当てるとき、他製品の権限の判定（生のコードで比べる）で 403 になることがあります。
8. （#750 以降に更新する環境）`backend/.env` の `AGENT_RBAC_*` は読まれないので削除してよい。header / JWT で API を呼んでいた
   クライアントは 401 になるため、画面のログインに移す。local の開発環境も共通 `.env` の `PLATFORM_ORACLE_*` が必要。
   権限管理で検索・回答プロファイルを割り当てていたロールは、RAG の権限管理で検索・回答プロファイルを割り当てる。
9. （#1798 以降に更新する環境）手順 3 のシステムテーブルの作成・更新を再実行する。migration 008 が、プラグインの配布元の
   画面のメニュー権限を持つロールに `menu.plugins`（プラグイン）を付け直し、使われなくなった配布元の定義の行
   （`AGENT_CONTROL_PLANE_ITEMS`）を消す。導入済みのプラグインはそのまま残る。
10. （#1803 以降に更新する環境）手順 3 のシステムテーブルの作成・更新を再実行する。migration 010 は導入済みの
   プラグイン（`AGENT_CONTROL_PLANE_ITEMS` の `ITEM_KIND = 'plugin'`）とメニュー権限 `menu.plugins` を消す破壊的な
   migration なので、「運用設定 > システムテーブル」で内容を確認して承認する（CLI は `--allow-destructive`）。
   プラグインが配っていたスキル・MCP 接続を使い続けるなら、先にスキル（`AGENT_SKILLS_DIR` か画面）と MCP 接続として登録し直す。

## 9. 既知の制約

- 範囲が制限された利用者の `GET /observability/events` は、件数上限（limit）を適用した後に範囲で絞ります。
- local の Run は Run の利用者が `00000000-0000-0000-0000-000000000000`（ローカル利用者）です。production の RAG / NL2SQL を呼ぶと、
  その利用者は呼び先に存在しないため拒否されます（local 同士で使う）。

## 自動実行（スケジュール。#784）

- メニュー権限 `menu.automations`（Agent 構築 /「自動実行」）。作成・変更・削除・今すぐ実行は `agent.admin`。
- 自動実行は作った利用者として Run を作る（実行のたびに利用者の現在の権限と対象範囲を確かめ、実行できなければ「開始できず」）。前回の Run が終わっていなければ、その回は飛ばす。
- 起動はスケジュール（毎日・平日・毎週・毎時）と、画面の「今すぐ実行」だけ。外から自動実行を起動する公開の入口は持たない（#1799）。
- 無人の実行なので、ツール権限で承認が要るツールは呼ばない（承認待ちで止めない。#1813）。モデルへは使えないことを返し、Run の監査に `automation.approval_required` の step を残す。自動実行で使うには、ツール権限でそのツールを「自動実行」（allow）にする（承認の画面の「承認して今後は自動で実行」でもよい。#1832）。NL2SQL の `nl2sql_query` は既定で承認なし（#1832）なので、ツール権限で ask にしていなければ自動実行でも使える。編集画面の警告は `GET /api/automations/agents/{agent_id}/approval-tools`（`menu.automations`）。
- 「前回」は自動実行に保存した結果（Run を作った・飛ばした・開始できず）ではなく、Run を作った回はその Run の今の状態を表示のときに引いて出す（#1812）。
