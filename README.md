# No.1 Production Ready Assistant

Oracle / OCI 上で動く本番品質の AI 業務アプリ群（**RAG / NL2SQL / Agent**）と、その共通基盤をまとめた monorepo です。

## ローカルで動かす（Oracle Autonomous Database Free コンテナ）

DB は docker の **Oracle Autonomous Database Free**（ADB Free）コンテナを使います。OCI のアカウントは要りません。

### 1. コンテナを起動する

イメージは Oracle Container Registry から**ログインなしで** pull できます（約 5 GB）。

```bash
docker pull container-registry.oracle.com/database/adb-free:latest-23ai
```

データを残す volume を作り、コンテナの中の `oracle` ユーザー（uid 1001）が書けるようにしておきます。所有者が root のままだと、起動時に `PermissionError: [Errno 13] Permission denied: '/u01/data/MYATP.pdb.partial'` で止まります。

```bash
docker volume create adb-free-data
docker run --rm -u 0 -v adb-free-data:/u01/data --entrypoint chown \
  container-registry.oracle.com/database/adb-free:latest-23ai -R 1001:1001 /u01/data
```

```bash
docker run -d --name adb-free \
  -p 1525:1522 -p 8543:8443 \
  -e WORKLOAD_TYPE=ATP \
  -e ADMIN_PASSWORD='<ADMIN のパスワード>' \
  -e WALLET_PASSWORD='<Wallet のパスワード>' \
  --cap-add SYS_ADMIN --device /dev/fuse \
  -v adb-free-data:/u01/data \
  container-registry.oracle.com/database/adb-free:latest-23ai
```

| host の port | コンテナの port | 用途 |
|---|---|---|
| `1525` | `1522` | SQL*Net（mTLS。Wallet で接続） |
| `8543` | `8443` | ORDS / Database Actions（`https://localhost:8543/ords/`） |

- 初回は PDB の展開で 2〜3 分かかります。`docker ps` の STATUS が `(healthy)` になったら使えます。
- `ADMIN_PASSWORD` と `WALLET_PASSWORD` は **12〜30 文字で、大文字・小文字・数字を含め、`admin` と二重引用符を含めない**必要があります（ADB の mandatory profile が強制し、満たさないと `ORA-28219` になります）。
- 起動・停止・状態は docker で行います（画面からは操作しません）。

```bash
docker start adb-free     # 起動
docker stop adb-free      # 停止
docker ps -a --filter name=adb-free   # 状態
docker logs --tail 50 adb-free        # ログ
```

### 2. Wallet を取り出す

Wallet はコンテナの中で作られます。host に取り出し、`tnsnames.ora` の port をコンテナの `1522` から host の `1525` に書き換えます。repository の外に置いてください。

```bash
docker cp adb-free:/u01/app/oracle/wallets/tls_wallet /u01/workspace/adb-free-wallet
sed -i 's/(port=1522)/(port=1525)/g' /u01/workspace/adb-free-wallet/tnsnames.ora
chmod 700 /u01/workspace/adb-free-wallet
```

サービス名は `myatp_low` / `myatp_medium` / `myatp_high` / `myatp_tp` / `myatp_tpurgent` です（mTLS。末尾が `_tls` のものは host に公開していない port を使うので使えません）。

### 3. アプリ用の DB ユーザーを作る

3 製品は ADMIN ではなくアプリ用のユーザーで接続し、システムの表はそのユーザーの schema に作ります。ADMIN で接続して作成します（例は python-oracledb。`sqlplus` / SQLcl / Database Actions でも同じ SQL です）。

```sql
CREATE USER ASSISTANT IDENTIFIED BY "<アプリのパスワード>"
  DEFAULT TABLESPACE DATA QUOTA UNLIMITED ON DATA PROFILE ORA_APP_PROFILE;
GRANT PDB_DBA TO ASSISTANT;
```

- パスワードの規則は ADMIN と同じです（ADB の mandatory profile。`PASSWORD_VERIFY_FUNCTION NULL` の profile を作っても回避できません）。
- `PDB_DBA` に `CTXAPP`（Oracle Text）・`DB_DEVELOPER_ROLE`（表・sequence・view・procedure・job の作成）が含まれます。

```bash
cd rag/backend && uv run python - <<'PY'
import oracledb
W = "/u01/workspace/adb-free-wallet"
c = oracledb.connect(user="ADMIN", password="<ADMIN のパスワード>", dsn="myatp_low",
                     config_dir=W, wallet_location=W, wallet_password="<Wallet のパスワード>")
cur = c.cursor()
cur.execute('CREATE USER ASSISTANT IDENTIFIED BY "<アプリのパスワード>" '
            'DEFAULT TABLESPACE DATA QUOTA UNLIMITED ON DATA PROFILE ORA_APP_PROFILE')
cur.execute("GRANT PDB_DBA TO ASSISTANT")
PY
```

### 4. `.env` を置く

```bash
cp platform/.env.example platform/.env
cp rag/backend/.env.example rag/backend/.env
cp nl2sql/backend/.env.example nl2sql/backend/.env
cp agent/backend/.env.example agent/backend/.env
```

`platform/.env`（3 製品共通。git には入りません）の次の値を書き換えます。

```dotenv
PLATFORM_ENVIRONMENT=local
PLATFORM_AUTH_COOKIE_SECURE=false            # http://localhost で動かすため
PLATFORM_ADMIN_LOGIN_USER_PASSWORD=<system_admin のパスワード>
PLATFORM_ORACLE_USER=ASSISTANT
PLATFORM_ORACLE_PASSWORD=<アプリのパスワード>
PLATFORM_ORACLE_DSN=myatp_low
PLATFORM_ORACLE_CONNECTION_SECURITY=wallet_mtls
PLATFORM_ORACLE_WALLET_DIR=/u01/workspace/adb-free-wallet
PLATFORM_ORACLE_WALLET_PASSWORD=<Wallet のパスワード>
```

`system_admin` のパスワードにも同じ規則（12〜30 文字・大文字/小文字/数字・`admin` を含まない）があり、満たさないと「構成管理者の認証情報が設定されていません」（503）になります。

### 5. システムの表を作る（初期化）

どれも冪等で、何度実行してもかまいません。

```bash
cd rag/backend    && uv sync --locked --dev && uv run python -m app.rag.system_schema_cli initialize
cd nl2sql/backend && uv sync --locked --dev && uv run python -m app.cli.nl2sql_system_schema --initialize \
                  && uv run python -m app.cli.app_security_migrate --apply --skip-bootstrap
cd agent/backend  && uv sync --locked --dev && uv run python -m app.cli.agent_system_schema --initialize
```

できているかの確認（ready なら 0 で終わります）:

```bash
cd rag/backend    && uv run python -m app.rag.system_schema_cli check
cd nl2sql/backend && uv run python -m app.cli.nl2sql_system_schema --check
cd agent/backend  && uv run python -m app.cli.agent_system_schema --check
```

### 6. 起動する

先に共有の UI を build します（各製品の frontend は `file:` で参照します）。

```bash
cd platform && npm ci && npm run build
```

製品ごとに backend と frontend をまとめて起動します（Ctrl+C で両方止まります）。

```bash
cd rag    && (cd frontend && npm ci) && bash scripts/start-all.sh
cd nl2sql && (cd frontend && npm ci) && bash scripts/start-all.sh
cd agent  && (cd frontend && npm ci) && bash scripts/start-all.sh
```

| 製品 | 画面 | backend |
|---|---|---|
| RAG | http://localhost:3000 | http://localhost:8000/api/health |
| NL2SQL | http://localhost:3001 | http://localhost:8010/api/health |
| Agent | http://localhost:3002 | http://localhost:8020/api/health |

`system_admin` でログインし、「設定 › データベース」の接続テストが成功すれば DB に届いています。

## 配備

OCI Resource Manager の Terraform stack（「Deploy to Oracle Cloud」ボタン）による配備は廃止しました（#12）。
ローカルで動かす手順は、上の「ローカルで動かす（Oracle Autonomous Database Free コンテナ）」を参照してください。
1 台の Compute に 3 製品を入れる配備のスクリプト（`platform/deploy/`）の DB・更新・確認の仕組みは
[platform/docs/deployment.md](./platform/docs/deployment.md) にあります。配備は reverse proxy（Nginx など）と TLS の証明書を作りません（#19。[各製品の公開](#各製品の公開reverse-proxy-は利用者の選択)）。

### アップロードした原本の保存先

アップロードした原本（RAG の文書・NL2SQL のオントロジーの資料）は、Compute のディスク（`/u01/data/production-ready-<製品>` の下。`platform/.env` の `PLATFORM_LOCAL_STORAGE_DIR`）に保存します。保存先は「システム設定 › アップロード保存先」でローカルのディレクトリだけを選べます（OCI Object Storage は使いません。#11）。

## 配備した後の初期設定（推奨のロールとユーザー）

配備の直後にログインできるのは、構成のシステム管理者（ログイン ID `system_admin`）だけです。
日々の管理と一般の利用には、次のロールを作ることを推奨します。`system_admin` は初期設定・システム設定の変更・緊急時だけに使います。

| ロールコード | ロール名 | 権限 |
|---|---|---|
| `APP_ADMIN` | アプリ管理者 | 3 製品の「システム設定」**以外**のすべての権限 |
| `APP_USER` | 一般利用者 | 3 製品の「AI 活用」の権限だけ |

- 「システム設定」（アップロード保存先・モデル・データベース）は `APP_ADMIN` にも付けず、`system_admin` だけが変えます（接続と認証の設定を日々の管理の操作で壊さないため）。

動作確認用のユーザー（配備の後に、ロールと権限が意図どおりかを確かめるためのもの）:

| ログイン ID | 表示名 | ロール |
|---|---|---|
| `app_admin` | アプリ管理者 | `APP_ADMIN` |
| `app_user` | 一般利用者 | `APP_USER` |

- **実際の利用では、このユーザーを共用せず、1 人に 1 つのユーザー（例 `taro.yamada`）を作って `APP_ADMIN` か `APP_USER` を付けてください。** 共用すると、実行履歴・承認の判断者・版の公開者などに誰の操作かが残りません。動作確認が済んだら、`app_admin` / `app_user` は「ユーザー管理」で無効にしてください。
- ロールコードは英大文字（画面で小文字を入れても大文字にそろえます）、ログイン ID は英小文字で書いています。

1. **ロールを作る**: どれか 1 つの製品に `system_admin` でログインし、「ユーザーとロール」→「ロール管理」で `APP_ADMIN` と `APP_USER` を作ります。ロールとユーザーは 3 製品で共通なので、1 回作れば 3 製品で使えます。
2. **権限を付ける**: 権限は製品ごとなので、**RAG・NL2SQL・Agent のそれぞれで**「セキュリティ設定」→「権限管理」を開き、`APP_ADMIN` には「システム設定」の項目以外のすべての権限を、`APP_USER` にはサイドナビの「AI 活用」の項目の権限だけを付けます。
   検索・回答プロファイル・業務プロファイル・エージェントなど「利用できる対象」を絞る項目があれば、`APP_USER` に使わせる対象を選びます。
3. **ユーザーを作る**: 「ユーザーとロール」→「ユーザー管理」で動作確認用の `app_admin`（ロール `APP_ADMIN`）と `app_user`（ロール `APP_USER`）を作り、それぞれでログインして、見えるメニューと操作を確かめます。

## 配備した後の運用

Compute（Oracle Linux 9）に `opc` で SSH して実行します（`ssh opc@<IP>`。repository は `/u01/aipoc/no.1-production-ready-assistant`）。

最新のコード（main）を取得して反映する（取得 → build → 全サービスの再起動）:

```bash
sudo /u01/aipoc/no.1-production-ready-assistant/platform/deploy/suite-update.sh
```

全サービスを再起動する（今のコードから build してから再起動。`suite-pull.sh` で取得したコードもこれで反映されます）:

```bash
sudo /u01/aipoc/no.1-production-ready-assistant/platform/deploy/suite-restart.sh
```

- build は、依存の同期・共有 UI と各製品の frontend・DB の migration・systemd まで行います。build せずに再起動だけするときは `--no-build`、状態だけを見るときは `--status`。
- 2 つのスクリプト（`--status` を除く）は、最初に ADB の状態を確かめ、止まっていれば `platform/.env` の OCI の認証（システム設定 › OCI 認証 で保存した `~/.oci/config`）で起動し、AVAILABLE になってから続けます（最大 600 秒）。起動できないとき（OCI の認証が無い・権限が無い・時間切れ）も止まらず、理由と手で起動する方法を warning として出し、build と再起動を続けます（DB に接続できなくてもアプリケーションは起動します。#1756）。DB に接続できない間も `system_admin` はログインでき、「システム設定 › データベース」から ADB を起動すれば、スクリプトを実行し直さなくてもそのまま DB を使えます（#1762）。要る IAM の権限は [platform/docs/deployment.md](./platform/docs/deployment.md#adb-の自動の起動1745) を参照してください。
- 取得だけを行うときは `platform/deploy/suite-pull.sh`（追跡ファイルに変更があると中止します）。
- 画面で保存した設定（各製品の `backend/.env`・`platform/.env`）と Wallet は保たれます。
- 時間がかかるので `tmux` の中で実行すると、SSH が切れても止まりません。log は `/var/log/suite-ops.log`。
- OS を再起動すると、各製品のサービスは自動で起動します。

## 各製品の公開（reverse proxy は利用者の選択）

配備は reverse proxy（Nginx など）と TLS の証明書を作りません（#19）。各製品は直接の port で動きます。前段に reverse proxy を置くか、HTTPS の証明書をどう用意するかは、利用者が環境に合わせて決めます。

| 製品 | backend（`uvicorn`） | frontend（開発 / preview） | frontend の base |
|---|---|---|---|
| RAG | 配備は `127.0.0.1:8000`（開発の `rag/scripts/start-backend.sh` は `8100`） | `3100`（`rag/scripts/start-frontend.sh`） | `/rag/` |
| NL2SQL | 配備は `127.0.0.1:8010`（開発の `nl2sql/scripts/start-backend.sh` は `8110`） | `3101` | `/nl2sql/` |
| Agent | 配備は `127.0.0.1:8020`（開発の `agent/scripts/start-backend.sh` は `8120`） | `3102` | `/agent/` |

- 開発: 各製品の `scripts/start-all.sh`（または `start-backend.sh` と `start-frontend.sh`）で、backend は `uv run uvicorn app.main:app --host <host> --port <port>`、frontend は Vite の dev server で動きます。frontend の `/api` は `BACKEND_URL=http://127.0.0.1:<backend の port>` を渡したときだけ backend へ proxy します。
- build した画面を出す: `cd <製品>/frontend && npm run build`（base を変えるときは `FRONTEND_BASE_PATH=/<製品>/`）の後、`BACKEND_URL=http://127.0.0.1:<backend の port> npx vite preview --host 0.0.0.0 --port <port>` で `dist/` を配信します。配備（`platform/deploy/suite-init.sh`）は `dist/` を `/<製品>/` の base で build し、backend を systemd で `127.0.0.1` に起動するところまでを行います。
- reverse proxy を置くときは、`/<製品>/` を各製品の `frontend/dist/`（SPA なので見つからない path は `index.html`）に、`/<製品>/api/` を prefix を外して各製品の backend に渡します。SSE（`text/event-stream`）は buffering を切り、回答の生成・評価・MCP の長い要求（最大 600 秒程度）に合わせて待ち時間を延ばし、アップロードの上限（`<製品>_MAX_UPLOAD_BYTES`）より本文の上限を大きくします。公開の URL は `platform/.env` の `PLATFORM_PUBLIC_BASE_URL` に書くと、API キーの画面の接続先に出ます。

## 構成

| ディレクトリ | 内容 |
|---|---|
| [`platform/`](./platform/) | 共通基盤。共有 UI `@production-ready/ui`、共有のシステム設定（`@production-ready/system-settings` / `pr_system_settings`）、共有 backend `pr_backend_core`、デザインシステム、3 製品共通の UX 契約（[`platform/docs/ux-contracts/`](./platform/docs/ux-contracts/README.md)） |
| [`rag/`](./rag/) | Production Ready RAG — 文書のナレッジ構築と、検索・回答プロファイルからの検索・回答 |
| [`nl2sql/`](./nl2sql/) | Production Ready NL2SQL — SQL 専用の自然言語問い合わせ |
| [`agent/`](./agent/) | Production Control Plane for AI Agents — Business Agent・Skill・Runtime の管理 |
| [`suite/`](./suite/evaluation/README.md) | 製品をまたぐスイートのアプリケーション。`suite/evaluation` は製品の公開の MCP だけで業務 Agent などを評価する CLI |
| [`skills/`](./skills/) | 組み込みのスキル（Agent Skills の形式 `skills/<name>/SKILL.md`）。Agent が読み、外部の Agent にも入れられる |

- 準備中（ロードマップ）の機能は各製品の README の「ロードマップ」にあります（RAG: OCI Document Understanding の解析は準備中で、まだ選べません。[rag/README.md](./rag/README.md#ロードマップ)）。
- 依存は `platform/` → 各製品・`suite/` の一方向です（suite は製品を公開の MCP だけで呼びます）。各製品は `platform/` を相対パスで参照し、backend / frontend / 実行環境（uv の venv）/ 配備はそれぞれ独立しています。
- 開発・起動方法は各ディレクトリの `README.md` を参照してください。

## Claude Code から RAG / NL2SQL / Agent を使う（MCP とスキル）

配備した Suite の 3 製品は MCP（`POST /<製品>/api/mcp`。backend を直接呼ぶときは `POST /api/mcp`）を公開しています。Claude Code などの外部の Agent は、利用者が自分で発行した API キーで接続し、`skills/` の 3 つのスキル（業務 RAG 調査・構造化データ照会・資料とデータの横断調査）と組み合わせて使います。以下の `https://<host>/<製品>/api/mcp` は、利用者が前段に置いた reverse proxy の URL です（[各製品の公開](#各製品の公開reverse-proxy-は利用者の選択)。reverse proxy を置かないときは `http://<host>:<backend の port>/api/mcp`）。Claude Code を動かす端末からその port に届くようにしておきます。

### 1. API キーを発行する

1. RAG・NL2SQL・Agent のどれかの画面にログインし、左下のアカウントの欄（名前・ロール）→「API キー」のタブ（`/account?tab=api-keys`）を開きます。
2. 名前（用途。例「会社の PC の Claude Code」）・使える製品（RAG / NL2SQL / Agent。既定は 3 つとも）・有効期限（30 / 90 / 365 日・無期限）を選んで発行します。Agent は使える業務 Agent を絞れます（Agent の画面で発行するとき）。
3. 秘密（`prak_…`）は **発行の直後に 1 回だけ** 表示されます。すぐに次の手順のファイルに保存します。

キーは発行した利用者の権限で動き、MCP の入口でだけ使えます（画面用の API には使えません）。

### 2. キーと接続の設定をファイルに置く

キーは repo・共有の設定に書かず、自分だけが読めるファイルに置きます。Claude Code にはシェルの環境変数（`~/.bashrc`）ではなく、Claude Code 自身の設定（`~/.claude/settings.json` の `env` と MCP の `headersHelper`）で渡します。Claude デスクトップ版（Code タブ）は `~/.bashrc` を読まずに起動するので、環境変数に頼るとターミナル版でしか動きません（デスクトップ版で `Missing environment variables: PR_API_KEY` になる）。

```bash
mkdir -p ~/.config/production-ready && chmod 700 ~/.config/production-ready
# キー（prak_... を発行したキーに置き換える）
cat > ~/.config/production-ready/env <<'EOF'
PR_API_KEY='prak_...'
EOF
chmod 600 ~/.config/production-ready/env
# MCP の Authorization ヘッダーを作るスクリプト（Claude Code が MCP に接続するときに実行する）
cat > ~/.config/production-ready/mcp-headers.sh <<'EOF'
#!/bin/sh
. "$HOME/.config/production-ready/env"
printf '{"Authorization":"Bearer %s"}\n' "$PR_API_KEY"
EOF
chmod 700 ~/.config/production-ready/mcp-headers.sh
```

ツールの待ち時間を `~/.claude/settings.json` の `env` に足します（ほかの設定は残します）。reverse proxy の HTTPS の証明書を自分の CA で署名したときは、`NODE_EXTRA_CA_CERTS` にその CA の証明書のパスも足します。

```bash
python3 - <<'EOF'
import json, pathlib
home = pathlib.Path.home()
path = home / ".claude" / "settings.json"
settings = json.loads(path.read_text()) if path.exists() else {}
settings.setdefault("env", {}).update({
    "MCP_TOOL_TIMEOUT": "660000",  # rag_search は 20 秒〜2 分かかる（既定の 60 秒では切れる）
})
path.parent.mkdir(exist_ok=True)
path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
EOF
```

手で書くなら、`~/.claude/settings.json` に次を足します（パスは絶対パス。`~` と `$HOME` は展開されません）。

```json
{
  "env": {
    "MCP_TOOL_TIMEOUT": "660000"
  }
}
```

### 3. MCP を登録する

スキルは MCP サーバーの名前を `rag` / `nl2sql` / `agent` として書いているので、この名前で登録します。キーは `headers` に書かず、`headersHelper` に手順 2 のスクリプトを指定します（設定の `~/.claude.json` にはスクリプトのパスだけが入ります）。`claude mcp add` には `headersHelper` の option が無いので、`claude mcp add-json` で登録します。

```bash
for p in rag nl2sql agent; do
  claude mcp add-json --scope user "$p" \
    "{\"type\":\"http\",\"url\":\"https://<host>/$p/api/mcp\",\"headersHelper\":\"$HOME/.config/production-ready/mcp-headers.sh\"}"
done
```

同じ名前で登録済みなら（`--header 'Authorization: Bearer ${PR_API_KEY}'` で登録した以前の手順など）、先に `claude mcp remove --scope user <名前>` で消します。以前の手順で `~/.bashrc` に足した `~/.config/production-ready/env` を読む行は消してかまいません（`env` のファイルは `export` の付いた以前の形のままでも動きます）。

### 4. スキルを入れる

```bash
claude plugin marketplace add https://github.com/engchina/no.1-production-ready-assistant.git --sparse .claude-plugin skills
claude plugin install production-ready-skills@production-ready-assistant
```

repo を clone 済みの環境（配備した Compute など）では、`claude plugin marketplace add <repo のパス>` でその checkout の `skills/` を使えます（配備の更新でスキルも新しくなります）。

### 5. 確かめる

```bash
# 環境変数を空にして、デスクトップ版と同じ条件で確かめる（rag・nl2sql・agent が ✔ Connected）
env -i HOME="$HOME" PATH="$PATH" claude mcp list
claude plugin list   # production-ready-skills@production-ready-assistant が ✔ enabled
# ツールの一覧（キー・URL の切り分け）
( . ~/.config/production-ready/env
  curl -sS -H "Authorization: Bearer $PR_API_KEY" -H 'Content-Type: application/json' \
    https://<host>/rag/api/mcp -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' )
```

設定を変えたら、Claude Code のセッションを新しく始めます（`env` は起動のときにだけ読まれ、開いているセッションには効きません。デスクトップ版も新しいセッションを開きます）。新しいセッションでは、`/mcp` で 3 つのサーバーが connected か、`/plugin` でスキルが入っているかを見たうえで、読み取りだけのツールを呼ぶ質問で確かめます（例「rag と nl2sql の MCP で、私が使える検索・回答プロファイルと業務プロファイルを一覧にしてください」「agent の MCP で、使える業務 Agent を一覧にしてください」）。

その後は普段の質問で使えます（質問に合うスキルを Claude Code が選び、RAG は `mcp__rag__rag_search`、NL2SQL は `mcp__nl2sql__nl2sql_query`、業務 Agent は `mcp__agent__agent_chat` などを呼びます）。非対話の `claude -p` では `--allowedTools "Skill,mcp__rag,mcp__nl2sql,mcp__agent"` を渡します。業務 Agent が使うスキルは `agent_skill_export` で Agent Skills の形で取り出し、`~/.claude/skills/<name>/` に置けます。

### 更新・キーの削除

- スキルの更新: `claude plugin marketplace update production-ready-assistant` の後に `claude plugin update production-ready-skills@production-ready-assistant`。
- キーを替える: 新しいキーを発行して `~/.config/production-ready/env` を書き換え（次の接続から使われます。開いているセッションは `/mcp` で接続し直します）、古いキーをアカウントの画面の「API キー」から削除します（削除はすぐに効き、元に戻せません）。全員のキーは「ユーザーとロール」→「API キー」で管理します。

応答の一覧（401 / 403 / 429）・呼び出しの上限・Agent のツールは [platform/docs/mcp-api-keys.md](./platform/docs/mcp-api-keys.md)、つまずく所・設定の JSON・長くかかるツール・Codex は [skills/README.md](./skills/README.md) を参照してください。

## ローカル開発の port

ローカルの開発（`<製品>/scripts/start-*.sh`・`npm run dev` / `npm run start`・`uvicorn --port`）の既定の port は、元の `no.1-production-ready-suite`（frontend `3000`〜`3002`・backend `8000` / `8010` / `8020`）と重ならない帯にしています。同じマシンで suite と併走できます（#25）。

| 製品 | frontend（dev / preview） | backend | e2e（Playwright の既定） |
|---|---|---|---|
| RAG | `3100` | `8100` | `3200` |
| NL2SQL | `3101` | `8110` | `3201`（preview の CI は fixture 用に `3202` も） |
| Agent | `3102` | `8120` | `3142` |

- 起動 script は `PORT` / `BACKEND_PORT` / `FRONTEND_PORT` / `BACKEND_URL` で上書きできます。
- OCI Compute への配備（`platform/deploy/` と各製品の `init_script.sh` の systemd）の backend は従来どおり `127.0.0.1:8000` / `8010` / `8020` です（1 台の Compute に suite と同居しないため変えていません）。
- 文書の解析・前処理のサービス（`127.0.0.1:18010`〜`18023`）は変えていません。

## 開発ルール

- 共通ルール（GitHub 運用・Issue / PR 規約・CI・デザインシステム）: [AGENTS.md](./AGENTS.md)。詳細は作業ごとに [docs/agents/](./docs/agents/) の文書を読む
- 製品固有ルール: 各ディレクトリの `AGENTS.md`（詳細は各製品の `docs/agents/`）
- `AGENTS.md` の大きさの上限（root は 20 KB、root と製品の合計は 32 KB。Codex の `project_doc_max_bytes` の既定 32 KiB に収める）は `scripts/check-agents-md-size.sh` が CI で確かめる
- Claude Code（2.1.277 以降）と Codex は `AGENTS.md` を直接読み込みます。`CLAUDE.md` は置いていません。

## ライセンスと第三者の部品

自前のコードは [LICENSE](./LICENSE)（MIT）です。3 製品が使う第三者の部品のうち、表示・原文の同梱などの義務があるもの（LGPL・MPL・AGPL の部品、Apache-2.0 の NOTICE、書体の OFL など）と、配備先での入手（`uv sync --locked`・`npm ci`・`dnf`）と再配布の違いは [THIRD-PARTY-NOTICES.md](./THIRD-PARTY-NOTICES.md) にまとめています（法的な助言ではありません）。frontend の build は、bundle の npm の package と書体のライセンスを成果物の `licenses/` に出します。

## CI

`.github/workflows/ci.yml` が変更のあった製品の job だけを実行します（`platform/` の変更時は全製品）。必須 check は `CI OK` です。
