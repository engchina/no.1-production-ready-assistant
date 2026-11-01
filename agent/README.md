# Production Ready Agent（業務 Agent Platform）

**業務・業界の Agent を作り、Skill と MCP（RAG・NL2SQL・その他）を安全に使わせる。**

業務 Agent・Skill を定義し、Control Plane の中の組み込み Runtime（OpenAI Agents SDK）で実行します。
モデルは OCI Enterprise AI の Responses API、ツールの実行は承認・監査を通します（#754）。

```text
Skill（SKILL.md・references/・宣言・画面）→ Skill Registry
                                         │
業務 Agent（指示・Skill・モデル）──────→ Skill → ツール（RAG / NL2SQL / 外部 MCP）
       │
       └─ Run → 組み込み Runtime（OpenAI Agents SDK + OCI Enterprise AI）→ 承認・監査・成果物
```

第三者の部品のライセンスの通知（Apache-2.0 の NOTICE・書体など）は root の [THIRD-PARTY-NOTICES.md](../THIRD-PARTY-NOTICES.md) にあります。

## 主要機能

- 業務 Agent: 業務指示、説明、有効状態、`skill_ids`、使うモデル（`model_id`。空なら既定のテキストモデル）。
- Skill: AgentSkills 互換指示、`mcp_requirements`、参照文書（SKILL.md と同じディレクトリの `references/`。必要なときだけ `skill_reference_read` で読む）。プラグインは持たない（#1803）。
- 組み込み Runtime: Agent と Skill の指示、Skill が必要とするツールを OpenAI Agents SDK に渡し、OCI Enterprise AI で実行する。
  承認が必要なツールで止まり、承認・却下の後に再開する。SDK の tracing は外部へ送らない。
- Common Run: 状態、Event、cancel、Artifact（回答・ツールの結果）、Approval、Audit。
- Dispatcher: 開発は in-process、本番は Oracle row lock + claim/lease worker。
- Snapshot v2: 業務 Agent・Run の Control Plane backup。
- ログインと権限: RAG / NL2SQL と同じ共通認証（`PLATFORM_AUTH_MODE=authenticated`）。ロールごとの権限と、
  エージェント単位の対象範囲（「セキュリティ設定 > 権限管理」）。local でもユーザー・ロールは共通 DB。詳細は
  [docs/security-rbac.md](docs/security-rbac.md)。

設計詳細は [docs/agent-control-plane-design.md](docs/agent-control-plane-design.md) を参照してください。

## ローカル開発

monorepo `no.1-production-ready-assistant` の `agent/` で作業します。共有 package は同じ repository の `../platform/` を相対パスで参照します。

```text
no.1-production-ready-assistant/
  platform/
  agent/
```

### 設定ファイル（#211）

| ファイル | 内容 | 雛形 |
|---|---|---|
| `../platform/.env` | 3製品共通の設定（`PLATFORM_*`）。アップロード保存先・モデル・データベース・構成管理者（`PLATFORM_ADMIN_*`）と認証ポリシー（`PLATFORM_AUTH_*`）。システム設定画面の保存先 | [`../platform/.env.example`](../platform/.env.example) |
| `backend/.env` | Agent 固有の設定（`AGENT_*`） | [`backend/.env.example`](backend/.env.example) |
| `../platform/model-settings.json` | モデル設定（画面から保存。3製品で共有） | — |

backend は共通 `.env` → `backend/.env` の順に読み、環境変数が最優先です。共通 `.env` の場所は
`PLATFORM_ENV_FILE` で変えられます。接頭辞のない旧名（`ORACLE_DSN` / `LOG_LEVEL` など）は読みません。

```bash
cp ../platform/.env.example ../platform/.env   # 共通（RAG / NL2SQL と同じファイル）
cp backend/.env.example backend/.env

# backend
cd backend
uv sync
uv run uvicorn app.main:app --reload --port 8120

# frontend
cd frontend
npm install
BACKEND_URL=http://127.0.0.1:8120 npm run dev
```

frontend の Vite は `BACKEND_URL` を明示したときだけ `/api` を backend へ proxy します（既定の接続先は持ちません）。
`BACKEND_URL` を渡さずに `npm run dev` すると hermetic モードになり、`/api` は proxy されず 404 を返します（起動時に警告を 1 行表示）。
`scripts/start-all.sh` / `scripts/start-frontend.sh` は `BACKEND_URL` を明示して起動します。

ローカル開発の既定は `PLATFORM_AUTH_MODE=local`（全権限のローカル利用者。ログイン不要）です。ログインを確認するときは
`PLATFORM_AUTH_MODE=authenticated` にし、共通 `.env` の `PLATFORM_ORACLE_*` と `PLATFORM_ADMIN_LOGIN_USER_PASSWORD` を設定して
運用設定 > システムテーブル か `cd backend && uv run python -m app.cli.agent_system_schema --initialize` でテーブルを作ります（[docs/security-rbac.md](docs/security-rbac.md)）。

業務 Agent・スキル・MCP 接続・実行の履歴・品質評価などの保存先は `backend/.env` の
`AGENT_RUNTIME_REPOSITORY_BACKEND` です。既定の `auto` は、共通 `.env` の `PLATFORM_ORACLE_*` がそろっていればデータベース
（`oracle_checkpoint`。テーブルはシステムテーブルが作る）、無ければメモリ（backend の再起動で消える）に保存します。保存先は起動時に
決まるので、起動の後にデータベースを設定したら backend を再起動してください。今の保存先は「運用設定 > 実行環境」の「保存先」で
確認でき、保存していないときは業務 Agent・実行履歴・MCP 接続などの画面の先頭に案内が出ます（#839）。
保存済みの実行の 1 件の不整合（終わった実行に承認待ちが残るなど）では起動を止めず、直せるものは直し、直せないものは
読み込まずに元の JSON のまま保存先に残します。件数は「保存先」のカードに出ます。データベースに接続できない（ADB の停止中など）
あいだも backend は起動し、画面は RAG / NL2SQL と同じく DB ゲートで案内します。接続できるようになると、再起動せずに読み込みます（#1212）。

既存 helper を使う場合:

```bash
scripts/start-all.sh
scripts/check-all.sh                                        # ローカルの既定は e2e と pip-audit を省く（#339）
SKIP_E2E=0 E2E_ARGS="e2e/auth-login.spec.ts" scripts/check-all.sh   # 関係する spec だけ e2e も実行
FULL=1 scripts/check-all.sh                                 # CI と同じく全部を実行
```

## 組み込み Runtime と dispatcher（#754）

業務 Agent は Control Plane の組み込み Runtime（`backend/app/features/agent/builtin_runtime.py`、`openai-agents` を版固定）
で実行します。Docker・外部の Runtime は使いません。

- モデル: 「システム設定 > モデル」の OCI Enterprise AI の接続と、Agent の `model_id`（空なら既定のテキストモデル）。
  Runtime 画面で SDK の版・既定のモデル・選べるモデル・実行できるかを確認できます。
- 実行: 開発は API のプロセス（`AGENT_RUNTIME_DISPATCH_MODE=in_process`）。本番で別プロセスにする場合は
  `uv run python -m app.features.agent.runtime_dispatcher` を起動します（Oracle の checkpoint の row lock と Run lease）。
- 外部 Runtime（OpenClaw / Hermes / DeerFlow）・Binding・`docker-compose.yml`・`.env.runtime.example` は #754 で削除しました。
  既存の環境で compose の Runtime を動かしていた場合は、`docker compose ... down` で止めてから volume を消してかまいません。
  `backend/.env` の `AGENT_RUNTIME_BINDINGS_DIR` / `AGENT_RUNTIME_SERVICE_CONTROL_*` / `AGENT_CONTROL_PLANE_PUBLIC_BASE_URL` /
  `AGENT_CONTROL_PLANE_MCP_TOKEN_SECRET` は読まれません（削除してよい）。

## 既存環境の更新手順（#566 既定のテキストモデルの必須化）

#566 で「システム設定 › モデル」の既定のテキストモデルを必須にしました（並びはテキスト → Vision）。既定のテキストモデルが未設定の環境は、
「システム設定 › モデル」で既定のテキストモデルを選んで保存します（以前と同じ動きにするなら、既定の Vision モデルと同じモデル）。
保存し直すまでは、従来どおり既定の Vision モデルを使います。詳細は [platform/README.md の「既存環境の更新手順（#566）」](../platform/README.md#既存環境の更新手順566-既定のテキストモデルの必須化)を参照してください。

## 既存環境の更新手順（#499 既定のモデルの変数名）

#499 で既定のモデルを「既定のテキストモデル」と「既定の Vision モデル」の 2 つに分け（#566 で 2 つとも必須にした）、
共通 `.env` の `PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_MODEL` / `_LLM_MODEL` / `_VLM_MODEL` を
`PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_TEXT_MODEL` / `PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_VISION_MODEL` に改名しました（旧名は読みません）。
`platform/.env` に既定のモデルを書いている環境は、backend を止めてから `platform/scripts/migrate_model_env_names.py`（確認 → `--apply`）で
書き換え、起動し直します。詳細は [platform/README.md の「既存環境の更新手順（#499）」](../platform/README.md#既存環境の更新手順499-既定のモデルの変数名)を参照してください。

## 既存環境の更新手順（#211）

#211 で設定を共通 `.env`（`platform/.env`、`PLATFORM_*`）と Agent の `backend/.env`（`AGENT_*`）に分けました。
旧名は読まないため、既存環境では更新後に 1 回だけ次を行います。

1. backend（systemd の `production-ready-agent-backend`、または開発の `uv run`）と、使っている場合は runtime-dispatcher を停止する。
2. 移行内容を確認する（書き換えない）。monorepo root で実行します。

   ```bash
   uv run --project platform/packages/backend_core \
       python platform/scripts/migrate_env_to_platform.py --product agent
   ```

3. 内容に問題がなければ `--apply` で書き換える（`<file>.bak-211` を作ります）。共通の変数は `platform/.env` へ移り、
   残りは `AGENT_` 接頭辞になります。

   ```bash
   uv run --project platform/packages/backend_core \
       python platform/scripts/migrate_env_to_platform.py --product agent --apply
   ```

4. スクリプトが扱わないものを手で直す。
   - （#754 以降は外部 Runtime を使わないため、compose の `agent/.env` と Binding の token の改名は不要です。）
5. 再配備または再起動する。
   - Compute に配備した instance: 手順 1〜4 を instance 上の
     `/u01/aipoc/no.1-production-ready-assistant` で（`git pull` と `agent/backend` の `uv sync --locked --no-dev` の後に）
     行い、`sudo systemctl restart production-ready-agent-backend` を実行する。
   - 開発: backend（と dispatcher）を `uv run` で起動し直す。

## 既存環境の更新手順（#215）

#215 で Agent も共通認証のログインと権限管理になり、配備の Nginx Basic 認証を廃止しました。
共通 `.env` の `PLATFORM_ADMIN_*` の確認、`PLATFORM_AUTH_MODE=authenticated`、システムテーブルの作成・更新、
Nginx の Basic 認証の削除の手順は [docs/security-rbac.md §8](docs/security-rbac.md#8-既存環境の更新手順215) を参照してください。

## 配備

OCI Resource Manager の Terraform stack による配備は廃止しました（#12）。Agent Control Plane は RAG / NL2SQL と同じ 1 台の Compute に、
`platform/deploy/` の配備のスクリプトで入れます（仕組みは [platform/docs/deployment.md](../platform/docs/deployment.md)）。
ログインは構成管理者 `system_admin`（RAG / NL2SQL と共通）と、ユーザー管理で作る DB ユーザーです。

## 主要 API

| Method | Path | 用途 |
|---|---|---|
| `GET` | `/api/health` / `/api/ready` | 稼働確認・readiness（ログイン不要） |
| `GET` | `/api/ready/database` | 画面の DB ゲートが使う DB の状態（3製品共通の判定と契約。常に 200。ログイン不要。#325）。`ok` / `not_configured` / `unreachable` / `setup_required`（システムテーブルの作成・更新が必要。#751）を返す |
| `POST` | `/api/auth/login` / `/api/auth/logout` / `/api/auth/password/change` | ログイン・ログアウト・パスワード変更（共通認証） |
| `GET` | `/api/auth/me` | ログイン中の利用者（実効権限・`allowed_agent_ids`） |
| `GET/POST/PATCH/DELETE` | `/api/security/users*` / `/api/security/roles*` | ユーザー管理・ロール管理（3製品共通） |
| `GET` | `/api/security/permissions` / `/api/security/access-targets/agents` | 権限カタログ・権限管理で選べるエージェント（`q` / `limit` / `offset` / `ids` で検索とページング。#608） |
| `PUT` | `/api/security/roles/{role_id}/access` | ロールの Agent 権限と対象範囲 |
| `GET` | `/api/runtime/status` | 組み込み Runtime の状態（SDK の版・既定のモデル・選べるモデル。API key は出さない） |
| `GET/POST/PATCH` | `/api/agents` | Business Agent |
| `GET/POST/PATCH` | `/api/skills` | Skill registry |
| `POST` | `/api/runs` | Run 作成（組み込み Runtime で実行） |
| `GET` | `/api/runs/{id}/events` | SSE event（Run のイベントの push は SSE だけ。取消・再開・承認は REST。#2260） |
| `POST` | `/api/runs/{id}/cancel` | Run の取消 |
| `GET` | `/api/runs/{id}/artifacts` | normalized Artifact |

`POST /api/runs` は `agent_id` と `goal` を受け取り、組み込み Runtime で実行します。旧エンジンの v1 の Run（`tool_calls`・
`X-Agent-API-Version: 1`）・planner・Memory は #756 で削除しました。

## セキュリティ境界

- production（`PLATFORM_AUTH_MODE=authenticated`）は全 API を既定拒否の権限 manifest で守ります。Cookie のないリクエストは 401 です
  （RAG / NL2SQL と同じ。`X-Agent-Roles` などの header は使いません）（[docs/security-rbac.md](docs/security-rbac.md)）。
- 承認の決定者はログイン中の利用者です。
- モデルの API key・外部の token は API/snapshot/log/Artifact に出しません。SDK の tracing は無効です。
- ツールは Skill が必要とするものだけをモデルに渡し、ポリシーが「拒否」のツールは渡しません。
- 業務 Agent が使う Skill は削除できません。
- Control Plane に別 LLM provider、外部 vector DB、新規 queue 製品を追加しません。

## 検証

```bash
cd backend
uv run ruff format --check .
uv run ruff check .
uv run mypy .
uv run pytest -q

cd ../frontend
npm run build
npm run test:e2e
```

## 共通ログ（JST）

API / worker の stderr、共有 tail / export の契約と保持・権限確認は [共通ログ仕様](../platform/docs/logging-standard.md) に従う（#858）。`scripts/tail-logs.sh` で製品の unit を読む。実ホストの保持値は変更前に確認する。
