# 組み込みのスキル（Agent Skills）

Production Ready RAG・NL2SQL の MCP を使って調べるスキル。組み込みの Agent が読み、Claude Code・Codex などの外部の Agent にも入れられる（形式は [Agent Skills](https://agentskills.io) の `<name>/SKILL.md`）。

| スキル | 使う MCP サーバー | 使う場面 |
|---|---|---|
| [`business-rag-research`](./business-rag-research/SKILL.md)（業務 RAG 調査） | `rag` | 社内の規程・手順書・台帳を検索し、根拠（文書名と節・頁）を示して答える |
| [`structured-data-query`](./structured-data-query/SKILL.md)（構造化データ照会） | `nl2sql` | 業務データの件数・金額・一覧を SQL で照会する |
| [`rag-and-structured-data`](./rag-and-structured-data/SKILL.md)（資料とデータの横断調査） | `rag`・`nl2sql` | 資料と業務データの両方が要る調査 |
| [`article-catalog-lookup`](./article-catalog-lookup/SKILL.md)（条の目録から根拠を探す） | `rag` | 条で構成された文書（法令・規程の XML ツリー JSON など）の根拠の条が検索で当たらないとき、条の目録から選んで読む（`business-rag-research` と一緒に使う） |

このディレクトリは Claude Code の plugin `production-ready-skills`（[`.claude-plugin/plugin.json`](./.claude-plugin/plugin.json)）でもあり、repo 直下の [`.claude-plugin/marketplace.json`](../.claude-plugin/marketplace.json)（marketplace `production-ready-suite`）が配る。

## 外部の Agent（Claude Code）で使う

用意するもの:

- RAG・NL2SQL・Agent の画面の左下のアカウントの欄から開くアカウントの画面の「API キー」のタブで発行した自分のキー（使える製品に RAG と NL2SQL を選ぶ）。キーの扱い・応答は [platform/docs/mcp-api-keys.md](../platform/docs/mcp-api-keys.md)。
- 接続先: `https://<host>/rag/api/mcp` と `https://<host>/nl2sql/api/mcp`（開発の dev server は `http://localhost:8000/api/mcp` と `http://localhost:8010/api/mcp`）。
- 業務 Agent に質問するなら Agent の `https://<host>/agent/api/mcp`（開発は `http://localhost:8020/api/mcp`。キーの使える製品に Agent を選ぶ）。下の「業務 Agent を専門家として呼ぶ」。

### 1. スキルを入れる

```bash
claude plugin marketplace add https://github.com/engchina/no.1-production-ready-suite.git --sparse .claude-plugin skills
claude plugin install production-ready-skills@production-ready-suite
claude plugin list   # production-ready-skills@production-ready-suite が enabled
```

- Claude Code の中では `/plugin marketplace add …` と `/plugin install production-ready-skills@production-ready-suite`。入れた後に始めたセッションから使える（今のセッションは `/reload-plugins`）。
- `--sparse .claude-plugin skills` で clone を 2 つのディレクトリに絞る（1 MB 未満）。付けないと monorepo 全体（約 72 MB）を clone する。
- 1 行の `claude plugin install production-ready-skills --marketplace engchina/no.1-production-ready-suite` でも入る（monorepo 全体を clone する。`owner/repo` の形は SSH（`git@github.com:…`）で clone したので、SSH の鍵が無い環境では上の https の URL を使う）。
- スキルの名前には plugin の名前が前に付く（`production-ready-skills:business-rag-research`）。質問に合うスキルは Claude Code が `description` から選ぶので、名前を書かなくてよい。
- 更新は `claude plugin marketplace update production-ready-suite` の後に `claude plugin update production-ready-skills@production-ready-suite`。

### 2. MCP を設定する

スキルは MCP サーバーの名前を **`rag` と `nl2sql`** として書いているので、この名前で登録する（Claude Code ではツールが `mcp__rag__rag_search` などになる）。

```bash
export PR_API_KEY='prak_...'   # 発行したキー（シェルの起動ファイルや secret の管理に置き、repo に書かない）
claude mcp add --scope user --transport http rag https://<host>/rag/api/mcp \
  --header 'Authorization: Bearer ${PR_API_KEY}'
claude mcp add --scope user --transport http nl2sql https://<host>/nl2sql/api/mcp \
  --header 'Authorization: Bearer ${PR_API_KEY}'
claude mcp list   # rag・nl2sql が ✔ Connected
```

- `--header` は **単引用符** で渡す。`${PR_API_KEY}` のまま設定（`~/.claude.json`）に保存され、Claude Code が起動のたびに環境変数から展開する（キーの値を設定ファイルに書かない）。二重引用符だと、シェルが展開したキーの値がそのまま保存される。環境変数が無いと `claude mcp list` が `Missing environment variables: PR_API_KEY` と出す。
- 設定の JSON（プロジェクトの `.mcp.json`・`claude --mcp-config <file>`）で書くときも同じ形にする:

```json
{
  "mcpServers": {
    "rag": {
      "type": "http",
      "url": "https://<host>/rag/api/mcp",
      "headers": { "Authorization": "Bearer ${PR_API_KEY}" }
    },
    "nl2sql": {
      "type": "http",
      "url": "https://<host>/nl2sql/api/mcp",
      "headers": { "Authorization": "Bearer ${PR_API_KEY}" }
    }
  }
}
```

### 3. 質問する

```bash
export MCP_TOOL_TIMEOUT=660000   # MCP のツールの待ち時間（ミリ秒）。既定の 60 秒では rag_search が切れる
claude
```

長くかかるツール（`rag_search`）の待ち時間は、下の「長くかかるツール」を参照。

非対話（`claude -p`）では、MCP のツールとスキルの使用を許可して渡す:

```bash
claude -p "経費の精算の申請期限はいつまでですか？期限を過ぎた場合の扱いも含めて、社内の規程で確かめて出典を示してください。" \
  --allowedTools "Skill,mcp__rag,mcp__nl2sql"
```

- 検索・回答プロファイル（RAG）・業務プロファイル（NL2SQL）は、質問に名前を書かなくてよい。スキルが推薦のツール（`rag_recommend_search_answer_profile`・`nl2sql_recommend_profile`）で質問に合うものを選ぶ。候補が違う資料に割れたときは、候補の名前を挙げてどれで調べるかを聞き返すので、選んで答える。決まっているときは名前を書くと確実（例「検索・回答プロファイル『社内規程』の資料で調べてください。…」）。

### つまずく所

| 症状 | 原因と対処 |
|---|---|
| `rag_search` が 60 秒で `The operation timed out.` | Claude Code の MCP のツールの待ち時間の既定が 60 秒。`rag_search`（回答を作る）は 20 秒〜2 分かかることがある（上限 600 秒）ので、`MCP_TOOL_TIMEOUT=660000` を `~/.claude/settings.json` の `env` に置く（下の「長くかかるツール」） |
| スキルは選ばれるが MCP のツールが呼ばれない・見つからない | MCP サーバーの名前が `rag` / `nl2sql` でない（`production-ready-rag` など）。名前を合わせて登録し直す |
| `claude -p` でツールが使われずに終わる | 非対話では許可の確認ができない。`--allowedTools "Skill,mcp__rag,mcp__nl2sql"` を渡す |
| 401 `SECURITY_API_KEY_INVALID` / 403 `SECURITY_API_KEY_PRODUCT_FORBIDDEN` | キーの誤り・削除・期限切れ、または使える製品にその製品が無い（[応答の一覧](../platform/docs/mcp-api-keys.md#応答)） |
| HTTPS で証明書のエラー（`UNABLE_TO_VERIFY_LEAF_SIGNATURE`） | 配備の既定の自作の CA を信頼させる: `~/.claude/settings.json` の `env` に `NODE_EXTRA_CA_CERTS`（ダウンロードした ca.crt の絶対パス。CA は `https://<host>/platform/ca.crt`）を置き、新しいセッションを始める（CA は起動のときにだけ読まれる）。シェルの環境変数（`~/.bashrc`）は Claude デスクトップ版では読まれない |
| `Missing environment variables: PR_API_KEY`（ターミナルでは動き、Claude デスクトップ版で失敗する） | デスクトップ版は `~/.bashrc` を読まないので、`headers` の `${PR_API_KEY}` が展開できない。キーのファイルを読む `headersHelper` で登録し直す（[README の手順](../README.md#claude-code-から-rag--nl2sql--agent-を使うmcp-とスキル)）。確かめるのは `env -i HOME="$HOME" PATH="$PATH" claude mcp list` |
| プロファイルを聞き返される・違うプロファイルで答える | 推薦の候補が違う資料に割れた・当たりが無い。候補から選ぶか、質問にプロファイルの名前を書く（`rag_recommend_search_answer_profile` は利用者が使えるプロファイルの資料を 1 回検索して決める） |
| スキルが出ない | `claude plugin list` で enabled か確かめ、新しいセッションを始める（今のセッションは `/reload-plugins`） |

開発の `*_AUTH_MODE=local` では、キーを付けない要求もローカル利用者として通る（`claude mcp list` の Connected だけではキーを確かめられない）。キーが使われたかは画面の「API キー」の最終利用日時で確かめる。

### 長くかかるツール

- `rag_search` は回答を作るので、モデルを 3〜8 回呼ぶ（質問拡張・根拠の評価・章節の拡張の判定・回答文の生成と監査）。#1864 の計測（`openai.gpt-oss-120b`、代表の 9 問）では 1 回 18〜112 秒（平均 47 秒）。サーバーの上限は 600 秒（`RAG_ANSWER_TIMEOUT_SECONDS`）で、超えると `isError: true`（`HTTP_504`）を返す。
- **推奨の待ち時間は 660 秒**（`MCP_TOOL_TIMEOUT=660000`）。サーバーの上限（600 秒）より長くし、時間切れはサーバーのエラー（どの工程で切れたか付き）で受け取る。配備の Nginx の待ち時間（660 秒）と同じ。
- **進み具合の通知（progress）**: client が `tools/call` に `_meta.progressToken` を付け、`Accept` に `text/event-stream` を含めると、応答は SSE になり、10 秒ごとと工程が変わるたびに `notifications/progress`（`message` は「経過 30 秒: 根拠確認（1回目）」の形）を送ってから結果を送る。進み具合を受けて待ち時間を延ばす client（MCP の SDK の `resetTimeoutOnProgress`）なら、待ち時間が短くても切れない。Claude Code が進み具合で待ち時間を延ばすかは確かめていない（未確認）ので、`MCP_TOOL_TIMEOUT` は付ける。
- 回答を Agent が自分で組み立てるなら、`rag_retrieve_evidence`（回答を作らず根拠だけ。モデルを呼ばない。4〜8 秒）で根拠を集め、公開の前に `rag_validate_answer` で監査する。

## 業務 Agent を専門家として呼ぶ（Agent の MCP）

業務 Agent（画面で作った、社内の資料・業務データを調べて答える Agent）を、外部の Agent からチャットのツールで呼べる（#1874）。スキルは要らない。MCP サーバーの名前は `agent` にする。

```bash
claude mcp add --scope user --transport http agent https://<host>/agent/api/mcp \
  --header 'Authorization: Bearer ${PR_API_KEY}'
claude -p "業務 Agent の一覧を確かめ、経理の業務 Agent に今月の経費の精算の期限を聞いてください。" \
  --allowedTools "mcp__agent"
```

- ツールは `agent_list` → `agent_chat`（`thread_id` で続き）→ 確認が返ったら `agent_answer_confirmation` → 終わっていなければ `agent_get_run`。詳細は [platform/docs/mcp-api-keys.md](../platform/docs/mcp-api-keys.md#agent-のツール)。
- キーで使える業務 Agent を絞るときは、Agent の画面でキーを発行する（「選んだ業務 Agent だけ」）。

## 画面で作ったスキルを外部の Agent に入れる

Agent の画面の「スキル」で、スキルの操作の「Agent Skills の形で書き出す」を選ぶと、`<name>/SKILL.md`（と `references/`）の zip を保存できる（#1874。API は `GET /api/skills/{skill_id}/export`、MCP では `agent_skill_export`）。組み込みのスキルはこのフォルダと同じ内容になる。

```bash
unzip sales-report.zip -d ~/.claude/skills/   # Claude Code の個人のスキル（~/.claude/skills/<name>/SKILL.md）
```

- `name` はスキルの ID から作る（小文字・数字・ハイフン。仕様に合わない文字はハイフンにする）。画面の名前・使う MCP のツール・タグは `metadata` の `production-ready-*` に入る（Agent に入れ直すときも同じ形で読める）。
- スキルは MCP サーバーの名前 `rag` / `nl2sql` を前提にするので、上の「MCP を設定する」と同じ名前で登録する。

## Codex で使う

```bash
codex mcp add rag --url https://<host>/rag/api/mcp --bearer-token-env-var PR_API_KEY
codex mcp add nl2sql --url https://<host>/nl2sql/api/mcp --bearer-token-env-var PR_API_KEY
codex mcp list
```

キーは環境変数 `PR_API_KEY` から読む（`~/.codex/config.toml` には変数の名前だけを書く）。スキルは `skills/<name>/` をフォルダごと Codex のスキルのディレクトリに写す（置き場所は Codex の文書に従う）。

## スキルを変えるとき

- 書き方は [docs/agents/prompts.md](../docs/agents/prompts.md)（プロンプト（LLM への指示・skill・ツールの説明）の書き方）と [agent/docs/agent-control-plane-design.md](../agent/docs/agent-control-plane-design.md)。検証は `agent/backend/tests/test_skill_md_spec.py`（`skills-ref` と、本文の識別子が MCP の契約にあること）。
- 外部の Agent へ配った後に内容を変えたら、[`.claude-plugin/plugin.json`](./.claude-plugin/plugin.json) の `version` を上げる（Claude Code は `version` ごとに plugin を cache するため、上げないと `claude plugin update` で新しい内容が入らない）。
