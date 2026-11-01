# 外部の MCP クライアントから RAG / NL2SQL / Agent を使う（API キー）

RAG（非構造化データ）・NL2SQL（構造化データ）・Agent（業務 Agent へのチャット）は、標準の MCP（Streamable HTTP の `POST /api/mcp`。`initialize` / `tools/list` / `tools/call`）を公開している。Claude Code・Codex・Cursor などの外部の MCP クライアントは、利用者が自分で発行した **API キー** で接続する（#1805・#1874）。

- RAG / NL2SQL: 資料の検索・業務データの照会のツール（契約は `platform/contracts/mcp/rag-tools.json` / `nl2sql-tools.json`）。
- Agent: 業務 Agent を「専門家」として呼ぶチャットのツール（`agent_list` / `agent_chat` / `agent_get_run` / `agent_answer_confirmation` / `agent_cancel_run` / `agent_get_run_observation` / `agent_skill_export`。契約は `platform/contracts/mcp/agent-tools.json`）。下の「Agent のツール」。

## 接続先

| 製品 | URL（前段の reverse proxy で `/<製品>/` に並べたとき） | 開発の dev server |
|---|---|---|
| RAG | `https://<host>/rag/api/mcp` | `http://localhost:8100/api/mcp` |
| NL2SQL | `https://<host>/nl2sql/api/mcp` | `http://localhost:8110/api/mcp` |
| Agent | `https://<host>/agent/api/mcp` | `http://localhost:8120/api/mcp` |

- 配備は reverse proxy と TLS の証明書を作らない（#19）。前段に reverse proxy を置くときは、`/<製品>/api/` を prefix を外して backend へ渡し、`Authorization` ヘッダーをそのまま渡す。RAG の `mcp` は LLM を待つため、待ち時間を長く（600 秒より長く）する。reverse proxy を置かないときは backend の `http://<host>:<port>/api/mcp` を直接呼ぶ。
- HTTPS の証明書は reverse proxy の側で用意する。自分の CA で署名したときは、クライアントの OS / ランタイムにその CA を信頼させる（Claude Code は `NODE_EXTRA_CA_CERTS`）。
- 画面の「API キー」の「接続先」に、製品ごとに **この画面の URL**（今開いている環境。Suite の配備では 3 製品、開発の dev server では開いている製品だけ）と **公開の URL** を出す（同じなら 1 行。どちらもコピーできる。#1870）。
- 公開の URL は `platform/.env` の `PLATFORM_PUBLIC_BASE_URL`（例 `https://203.0.113.10` / `https://203.0.113.10:8443`）+ `/<製品>/api/mcp`。配備は書かない（#19）。前段に置いた reverse proxy の URL を利用者が書く。backend は自分のキーの一覧（`GET /api/account/api-keys`）の `public_base_url` で返す（`scheme://host[:port]` 以外の値は空として扱う）。空なら画面はこの画面の URL だけを出す。
- 画面の「Claude Code の登録の例」は下の例と同じ形で、公開の URL があればその URL で書く。CA の証明書の配布は持たない（#19）。

## API キー

- **各利用者が自分で発行する**: 画面の左下のアカウントの欄（名前・ロール）から開くアカウントの画面の「API キー」のタブ（`/account?tab=api-keys`。#1873）。名前（用途。例「会社の PC の Claude Code」）・使える製品（RAG / NL2SQL / Agent。既定は 3 つとも）・有効期限（30 / 90 / 365 日・無期限）を選ぶ。メニュー権限は要らない。1 人 10 件まで。
- 秘密（`prak_<id>_<秘密>`）は **発行の直後に 1 回だけ** 表示する。DB には SHA-256 だけを保存する（表 `PLATFORM_API_KEYS`）。
- キーは **発行した利用者として動く**。呼び先は認証のたびに利用者の現在のロールから権限とデータの範囲（検索・回答プロファイル・ナレッジベース・業務プロファイル）を組み立てる。ツールの一覧（`tools/list`）も利用者の権限で決まる。
- 利用者が無効化・削除されたら、そのキーは使えない（削除ではキーも消える）。初回のパスワード変更が済んでいない利用者のキーも使えない。
- 削除: 自分のキーはアカウントの画面の「API キー」のタブから、全員のキーは「ユーザーとロール」→「API キー」（メニュー権限 `menu.security_api_keys`）から（画面の操作の名前は「削除」。#1873）。削除するとキーはすぐに使えなくなり、元に戻せない（監査に残す）。
- キーは **MCP の入口（`POST /api/mcp`）だけ** で使える。画面用の API（Cookie / CSRF）に付けると 401（`SECURITY_API_KEY_NOT_ALLOWED`）。
- **使える業務 Agent を絞れる**（最小権限。#1874）: Agent の画面で発行するとき「すべての業務 Agent」か「選んだ業務 Agent だけ」を選ぶ（共通の表の `RESOURCE_SCOPES` に `{"agent": [<業務 Agent の ID>…]}`）。範囲の外の業務 Agent は一覧に出ず、呼ぶと 403（`AGENT_OUT_OF_KEY_SCOPE`）。キーは利用者の権限を広げない（「利用者が使える業務 Agent ∩ キーの範囲」）。RAG / NL2SQL の画面で発行したキーは、Agent ではすべての業務 Agent。RAG / NL2SQL の対象（プロファイル）の範囲はまだ持たない。
- **呼び出しの上限**（乱用・誤用の防止。#1874）: キーごとに 1 分あたりの呼び出しの回数（`PLATFORM_API_KEY_CALLS_PER_MINUTE`。既定 60。3 製品の MCP。backend のプロセスごとに数える）と、直近 24 時間に Agent の MCP で作れる Run の数（`PLATFORM_API_KEY_RUNS_PER_DAY`。既定 200）。0 は上限なし。NL2SQL の `nl2sql_query`（SQL の実行）も回数の上限で抑える。
- **監査**: Agent の MCP から作った Run は、キーの ID と名前を Run に残す（実行履歴・監査に「MCP（キーの名前）」）。

## 応答

| 状況 | status | `error_code` |
|---|---|---|
| キーなし | 401 | `SECURITY_AUTHENTICATION_REQUIRED` 等（サービストークンとして検証） |
| 誤り・失効済み・期限切れ | 401 | `SECURITY_API_KEY_INVALID` |
| 使える製品にこの製品が無い | 403 | `SECURITY_API_KEY_PRODUCT_FORBIDDEN` |
| 利用者が無効・初回パスワード変更待ち | 403 | `SECURITY_PERMISSION_DENIED` |
| MCP 以外の API にキーを付けた | 401 | `SECURITY_API_KEY_NOT_ALLOWED` |
| ツールの権限が無い | 200（`isError: true`） | `structuredContent.error_code` = `MCP_TOOL_FORBIDDEN` |
| 1 分あたりの呼び出しの回数を超えた | 429（`Retry-After`） | `SECURITY_API_KEY_RATE_LIMITED` |
| Agent: 範囲の外の業務 Agent | 200（`isError: true`） | `AGENT_OUT_OF_KEY_SCOPE`（`status` 403） |
| Agent: 直近 24 時間の Run の数を超えた | 200（`isError: true`） | `API_KEY_RUN_LIMIT`（`status` 429・`details.retry_after_seconds`） |

## Agent のツール

| ツール | 何をするか |
|---|---|
| `agent_list` | 使える業務 Agent（ID・名前・説明・使うスキル）。質問に合う業務 Agent を選ぶ |
| `agent_chat` | 業務 Agent に質問する（`thread_id` で会話を続ける）。`wait_seconds`（既定 50 秒、最大 300 秒）まで回答を待ち、終わらなければ `in_progress=true` と `run_id` を返す |
| `agent_get_run` | Run の状態・回答の対応（`outcome`。RAG の `rag_search` と同じ語彙。完了した Run だけ）・回答・根拠（`sources`。RAG の `document_id` / `chunk_id` と引用の印 `cited`。今の権限で見られない根拠は除いて数を `withheld_evidence_count` に。#3240。本文は既定で全文で、応答の予算に入らない根拠は `text_omitted`、1 件の上限を超える根拠は境界で切って `truncated`・`next_offset`。#3321）・段落の印（`passage_marks`・`removed_passages`・`answer_warnings`。#2738）・回答前の確認（`confirmation`）・使ったツール。まだ終わっていなければ `wait_seconds`（既定 30 秒）まで待つ。出力は `agent_chat` / `agent_answer_confirmation` と同じ形（`schema_version` 4） |
| `agent_answer_confirmation` | 回答前の確認に答える（`action=answer` で確認事項ごとに選択肢の ID か自由入力）か、確認せずに回答させる（`action=skip`） |
| `agent_cancel_run` | 実行（`run_id`）を止める（承認・回答前の確認の待ちも閉じる）。冪等で、終わった Run はエラーにせず `result`（`cancelled` / `already_cancelled` / `already_finished`）と取消の後の `status` を返す。止められるのは画面の取消と同じ人（実行した利用者・会話の持ち主・system_admin）。Run を作らないので直近 24 時間の Run の数に数えない（#3240） |
| `agent_get_run_observation` | 評価・監査の機械が読む実行（`run_id`）の観測（#3240）。Run の情報（業務 Agent の公開の版 `agent_version`・`trace_id`・最終の検証の状態 `validation_status`・`created_at` / `finished_at`）、モデルの利用量（`usage`。OpenTelemetry の `gen_ai.usage.*` の名前）、ツールの呼び出しの経過（`tool_calls`。状態・失敗の分類 `error_code`・根拠の数・旧版も検索したか `include_superseded`・NL2SQL の状態と行の数だけ。引数・SQL・行・エラーの文は出さない）、調べた根拠の全件（`evidence`。読み取りのツールで読んだ chunk を含み、初めて現れた順 `rank`・RAG の検索の順位 `retrieval_rank`・引用 `cited` / `answer_use`・RAG と同じ名前の場所 `locator`）。根拠の本文は生成に渡した本文そのもの（切らない）で、件数（`limit`。既定 20・最大 100）と 1 ページの合計の予算でページに分け（続きは `next_cursor` を `cursor` に渡す）、1 件で予算を超える根拠は本文を省いて `text_omitted`（`rag_read_source` で読む）。根拠は呼ぶたびに今の権限で確かめ直し、見られない根拠は除いて数を `withheld_evidence_count` に返す。読めるのは `agent_get_run` と同じ人 |
| `agent_skill_export` | 使える業務 Agent が使うスキルを Agent Skills の形（`SKILL.md` と `references/` のファイル）で返す。`name` のフォルダに置くと外部の Agent でも使える |

- 業務 Agent は画面のチャットと同じく、公開中の版で RAG / NL2SQL などを調べて答える。会話（`thread_id`）は画面のチャットの会話と同じで、作った利用者だけが続けられる。
- MCP から作った Run は、**承認の要るツールを呼ばない**（承認する人が画面にいるとは限らないため）。業務 Agent は使えなかったツールを「確かめられなかった点」として答え、`skipped_approval_tools` に出す。
- 既定の待ち時間（50 秒）は Claude Code のツールの待ち時間の既定（60 秒）より短い。長くかかる質問は `agent_get_run` で続きを読むか、`MCP_TOOL_TIMEOUT` を延ばして `wait_seconds` を大きくする。

## 例

### Claude Code

```bash
export PR_API_KEY='prak_...'   # 発行したキー（repo や共有の設定に書かない）
claude mcp add --scope user --transport http rag https://<host>/rag/api/mcp \
  --header 'Authorization: Bearer ${PR_API_KEY}'
claude mcp add --scope user --transport http nl2sql https://<host>/nl2sql/api/mcp \
  --header 'Authorization: Bearer ${PR_API_KEY}'
claude mcp add --scope user --transport http agent https://<host>/agent/api/mcp \
  --header 'Authorization: Bearer ${PR_API_KEY}'
claude mcp list                 # rag・nl2sql・agent が ✔ Connected
export MCP_TOOL_TIMEOUT=660000   # rag_search は 20 秒〜2 分かかる（上限 600 秒。既定の 60 秒では切れる）
claude
```

- MCP サーバーの名前は `rag` / `nl2sql` / `agent` にする（組み込みのスキルがこの名前でツールを使う）。
- 長くかかるツール（`rag_search`。サーバーの上限 600 秒）の待ち時間は 660 秒を推奨する。client が `_meta.progressToken` と `Accept: text/event-stream` を付けると、3 製品の MCP（共通の `pr_backend_core.mcp`）は 10 秒ごとに進み具合（`notifications/progress`）を送る（#1864。詳細は [skills/README.md](../../skills/README.md)「長くかかるツール」）。
- `--header` は単引用符で渡す。設定（`~/.claude.json`）にはキーの値ではなく `${PR_API_KEY}` が保存され、Claude Code が起動のたびに環境変数から展開する。
- スキルの入れ方・`claude -p` の許可（`--allowedTools "Skill,mcp__rag,mcp__nl2sql"`）・設定の JSON・つまずく所は [skills/README.md](../../skills/README.md)。

### Codex

```bash
codex mcp add rag --url https://<host>/rag/api/mcp --bearer-token-env-var PR_API_KEY
codex mcp add nl2sql --url https://<host>/nl2sql/api/mcp --bearer-token-env-var PR_API_KEY
codex mcp add agent --url https://<host>/agent/api/mcp --bearer-token-env-var PR_API_KEY
```

### curl

```bash
URL=https://<host>/rag/api/mcp
H=(-H "Authorization: Bearer ${PR_API_KEY}" -H 'Content-Type: application/json')
curl -sS "${H[@]}" "$URL" -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}'
curl -sS "${H[@]}" "$URL" -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
curl -sS "${H[@]}" "$URL" -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"rag_list_search_answer_profiles","arguments":{"limit":10}}}'
```

NL2SQL の読み取りのツールの例は `nl2sql_list_profiles`、Agent は `agent_list`。

## local の認証モード

`PLATFORM_AUTH_MODE=local` などログインを省略する開発の構成でも、`Authorization: Bearer prak_…` を付けた要求はキーを検証する（素通りにしない）。local の画面（ローカル利用者）で発行したキーはローカル利用者として動き、local でだけ使える（production では 401）。キーを付けない要求は、従来どおり全権限のローカル利用者になる。そのため local では、MCP クライアントの接続の確認（`claude mcp list` の Connected）だけではキーを確かめられない。キーが使われたかは「API キー」の最終利用日時で確かめる。

## 実装（開発者向け）

- 共通: `pr_system_settings.auth.api_keys`（形式・保存・入出力・製品ごとの範囲 `resource_scopes`・1 分あたりの回数 `ApiKeyRateLimiter`）、`AuthService.create_api_key` / `list_api_keys` / `revoke_api_key` / `authenticate_api_key`、`authorize_request(..., api_key_paths={"/mcp"})`、`build_auth_router(..., api_keys=True)`（`/account/api-keys*` と `/security/api-keys*`）。DDL は共通認証の `PLATFORM_AUTH_DDL`。
- 画面: `@production-ready/system-settings` の `AccountPage`（`ACCOUNT_PATH`。「API キー」のタブの中身は `MyApiKeysPanel`）・`ApiKeysAdminPage`（`API_KEY_ADMIN_NAV_ITEM`）・`mcpEndpoints`。アカウントの画面へは `SidebarAccountSection` のアカウントの欄（`routes.account`）から移る（#1873）。
- 製品の manifest: `/account/api-keys*` はログインだけ（`OPEN_API_OPERATIONS`）、`/security/api-keys*` は RAG / NL2SQL が `menu.security_api_keys`、Agent は Agent 管理（`agent.admin`。画面は RAG / NL2SQL）。
- Agent: `features/agent/mcp_server.py`（ツールとキーの範囲・Run の数の上限）、`runtime.is_mcp_run`（承認の要るツールを呼ばない）。画面で業務 Agent を絞るのは `AccountPage` の `apiKeys.agentScopeOptions`（Agent の画面が、業務 Agent を読める利用者に渡す。#1873）。
- Agent → RAG / NL2SQL の連携は今までどおりサービストークン（`backend-standard.md`「製品間の連携」）。
- 残課題: 連携用の利用者（ログインできない利用者）として動くキー・RAG / NL2SQL のプロファイルでキーを絞る機能・worker をまたいだ回数の上限・OAuth 2.1（MCP の authorization 仕様）・A2A（Agent2Agent）プロトコル。
