# NL2SQL Route Access Checks

NL2SQL の運用系 API は、ルート認可だけでなく profile 単位のデータ境界を必ず確認する。
`PROFILE_MANAGE_PERMISSION` を持つ利用者、または `PLATFORM_AUTH_MODE=local` の匿名実行は全 profile を
扱える。それ以外は `Principal.allowed_profile_ids` に含まれる profile だけを対象にする。
既存 history などで `profile_id` が空の record は、access check 上は `default` profile として扱う。

## 確認済みルート

| 系統 | ルート | profile 境界 |
|---|---|---|
| Feedback | `POST /api/nl2sql/feedback` | 自分の history、または feedback 管理者だけが保存できる actor 境界を確認する。 |
| Feedback | `GET /api/nl2sql/feedback` | `profile_id` 指定時は 403、未指定時は許可 profile の history だけを返す。 |
| Feedback | `DELETE /api/nl2sql/feedback/{history_id}` | 自分の history、または feedback 管理者だけが解除できる actor 境界を確認する。 |
| Feedback admin | `POST /api/nl2sql/feedback/admin-review` | 管理レビュー保存前に対象 history の `profile_id` を確認する。 |
| Feedback index | `GET/POST /api/nl2sql/feedback-index*` | status/rebuild/clear は許可 profile の履歴・indexed id だけを集計/更新する。 |
| Feedback entries | `GET/POST /api/nl2sql/feedback-entries*` | 一覧は許可 profile に絞り、削除対象に許可外 history が含まれたら 403。 |
| Classifier | `GET /api/nl2sql/classifier/training-data` | 許可 profile の training example だけを返す。 |
| Classifier | `PATCH/DELETE /api/nl2sql/classifier/training-data/{example_id}` | PATCH は現在の example と更新先 profile の両方を確認する。DELETE は現在の example を確認する。 |
| Classifier | `POST /api/nl2sql/classifier/training-data/import` | import 行は許可 profile だけを取り込み、`replace=true` は許可 profile の既存 example だけを置換する。 |
| Classifier | `GET /api/nl2sql/classifier/training-data/export.xlsx` | 許可 profile の training example だけを出力する。 |
| Classifier | `POST /api/nl2sql/classifier/train` | 許可 profile の training example だけで学習する。 |
| Classifier | `GET /api/nl2sql/classifier/training-candidates` | 許可 profile の feedback history だけから候補を導出する。 |
| Select AI assets | `POST /api/nl2sql/select-ai/profiles/refresh` | `profile_id` 省略時も default profile として確認する。 |
| Select AI assets | `POST /api/nl2sql/select-ai-agent/assets/refresh` | `profile_id` 省略時も default profile として確認する。 |
| Select AI assets | `POST /api/nl2sql/select-ai/assets/cleanup` | `profile_id` 省略時も default profile として確認する。 |
| Select AI assets | `POST /api/nl2sql/select-ai-agent/assets/cleanup` | `profile_id` 省略時も default profile として確認する。 |
| Quality evaluation | `GET /api/nl2sql/quality-evaluations` | 許可 profile の job だけを返し、許可後にのみ in-process worker を wake する。 |
| Quality evaluation | `GET /api/nl2sql/quality-evaluations/{job_id}*` | job record を wake せず peek して profile 確認後、必要な read のみ wake する。 |
| Quality evaluation | `POST/DELETE /api/nl2sql/quality-evaluations/{job_id}*` | profile 確認に加え、profile 管理者以外は `actor_user_uuid` の所有者一致を要求する。 |
| MCP | `POST /api/mcp` の `nl2sql_list_profiles` / `nl2sql_recommend_profile` | 画面の route と同じ `allowed_profile_ids_for_request` の範囲だけを返す（#231）。サービストークンの claim `profile_ids` があれば claim との積（#1379）。 |
| MCP | `POST /api/mcp` の `nl2sql_query` | `profile_id` を省略・使えない ID にしたときは `"default"` に切り替えず（#1379）、利用者の権限 ∩ claim の中から使える 1 つか質問の推薦で選ぶ（`auto_selected_profile`。決まらなければ 404 `NL2SQL_PROFILE_NOT_SELECTED`。#2355）。ジョブ作成前に claim の範囲（明示した ID が範囲外なら 403 `PROFILE_SCOPE_FORBIDDEN`）と `assert_profile_access` で確認し、token の利用者を actor にする。 |
| MCP | `POST /api/mcp` の `nl2sql_get_job` / `nl2sql_cancel_job` | 本人のジョブだけ（system_admin は全員。製品の管理権限は理由にしない）。他人・不明のジョブは 404。読む・止めるたびに、ジョブの `profile_id` を claim `profile_ids`（範囲外は 403 `PROFILE_SCOPE_FORBIDDEN`）と `assert_profile_access` で確認する（#3318）。 |

## 未確認時の原則

- 新しい NL2SQL route は、request payload / path / query から直接 profile を受ける場合は
  `_assert_profile_access(...)` を呼ぶ。
- route が profile を直接受けず、history / job / training example / indexed entry などの二次 ID を
  受ける場合は、永続化済み record を取得してから、その record の `profile_id` を確認する。
- list/export/train/rebuild のような集計・一括操作は、service 層に
  `allowed_profile_ids` を渡して許可範囲外の record を除外する。
- 非同期 job は、dispatch / wake / mutation より前に profile と actor のアクセスを確認する。
