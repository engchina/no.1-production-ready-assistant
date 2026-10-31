# No.1 Production Ready Suite

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

## OCI へのデプロイ

ボタンを押すと、大阪リージョン（`ap-osaka-1`）を選んだ状態で OCI Resource Manager の stack の作成画面が開きます。1 つの Autonomous AI Database と、3 製品（RAG / NL2SQL / Agent）をまとめて入れる 1 台の Compute を作ります。

- **latest**: 東京（`ap-tokyo-1`）/ 大阪（`ap-osaka-1`）リージョンだけをサポートします（既定は大阪）。

  [![Deploy to Oracle Cloud](https://oci-resourcemanager-plugin.plugins.oci.oraclecloud.com/latest/deploy-to-oracle-cloud.svg)](https://cloud.oracle.com/resourcemanager/stacks/create?region=ap-osaka-1&zipUrl=https://github.com/engchina/no.1-production-ready-suite/releases/latest/download/production-ready-suite-terraform-stack.zip)

ボタンは[最新の release](https://github.com/engchina/no.1-production-ready-suite/releases/latest) の `production-ready-suite-terraform-stack.zip` を読み込みます。

入力の変数・手動でのアップロード・配備後の確認・トラブルシューティングは [terraform/README.md](./terraform/README.md) を参照してください。

### 配備の前に: VCN の security list

stack は VCN・subnet・security list・NSG を作らず、フォームで選んだ既存の subnet をそのまま使います（ingress の規則も足しません。#259）。配備の前に、OCI のコンソールの「ネットワーキング › 仮想クラウド・ネットワーク › 〈VCN〉 › セキュリティ・リスト」で、次の ingress の規則（ステートフル・TCP）を足してください（subnet を NSG で管理しているなら、同じ規則を NSG に足します）。足さないと、画面・MCP にも DB にも届きません。

| 足す security list | 送信元 CIDR | 宛先の port（TCP） | 用途 |
|---|---|---|---|
| Compute の subnet | 利用者の端末の CIDR（社内の出口の範囲など。どこからでも使わせるなら `0.0.0.0/0`） | 公開の port: フォームの `https_port`（既定 `443`）。HTTPS を off にしたときは `http_port`（既定 `80`） | 3 製品の画面と MCP（`https://<host>/<製品>/`） |
| Compute の subnet | 管理者の端末の CIDR | `22` | SSH（`ssh opc@<IP>`。[配備した後の運用](#配備した後の運用)） |
| ADB のプライベート・エンドポイントの subnet | **Compute の subnet の CIDR**（例 `10.0.1.0/24`） | `1522` | Compute（3 製品と配備の `suite-init.sh`）から ADB への接続 |
| bastion の subnet（`bastion_enabled` のときだけ） | 管理者の端末の CIDR（`0.0.0.0/0` にしない） | `22`・`5901` | bastion への SSH と VNC（[terraform/README.md「bastion の Compute」](./terraform/README.md#bastion-の-compute任意2670)） |
| Compute の subnet（`bastion_enabled` のときだけ） | **bastion の subnet の CIDR** | 公開の port（既定 `443`）・`22` | bastion の Chrome から 3 製品の画面、bastion から Compute への SSH |
| ADB のプライベート・エンドポイントの subnet（`bastion_enabled` のときだけ） | **bastion の subnet の CIDR** | `1522` | bastion（SQL の道具など）から ADB への接続 |

- 公開の port は、フォームで選んだ値を開けます（443 以外にしたら URL は `https://<IP>:<port>/`）。HTTPS が on のとき `http_port` は Compute で使わないので開けません。Compute の OS の firewall（firewalld）は配備のスクリプトが同じ port を開けます。
- ADB の 1522 は、ADB のアクセス・タイプが「プライベート・エンドポイント・アクセスのみ」（既定）のときの規則です。Compute と ADB のプライベート・エンドポイントが同じ subnet でも、その subnet の security list に「送信元 = その subnet 自身の CIDR、port `1522`」を足します（同じ subnet の中の通信にも security list の規則が要ります）。
  - 「許可された IP および VCN 限定のセキュア・アクセス」（ACL）なら、security list ではなく ADB の ACL で Compute の subnet（または VCN）を許可します。「すべての場所からのセキュア・アクセス」なら足すものはありません。
- egress: Compute は配備と実行で外部（GitHub・dnf・uv / npm の取得、OCI Generative AI・OCI Enterprise AI・Object Storage）へ HTTPS で出ます。Compute の subnet の security list の egress（既定は `0.0.0.0/0` のすべて）を絞っているなら TCP `443` の egress を残し、private subnet なら NAT gateway（と Service gateway）の経路を用意します。
- 製品間の通信（Agent → RAG / NL2SQL の MCP）は同じ Compute の `127.0.0.1` なので、security list は要りません。
- bastion を Compute と同じ subnet に置く（`bastion_subnet_id` を空にする）ときも、上の bastion の規則は「送信元 = その subnet 自身の CIDR」で足します。ADB が ACL（許可された IP および VCN）なら、ADB の ACL で bastion の subnet（または VCN）も許可します。

### 配備の前に: Object Storage の bucket

アップロードした原本（RAG の文書・NL2SQL のオントロジーの資料）は、配備の直後は Compute のディスク（`/u01/data/production-ready-<製品>` の下）に保存します（stack が `platform/.env` に `PLATFORM_UPLOAD_STORAGE_BACKEND=local` を書きます）。保存先を OCI Object Storage にするときは、stack が bucket も IAM の policy も作らないので、次を用意してください。ローカルのまま使うなら要りません。

| 用意するもの | 内容 |
|---|---|
| bucket | OCI のコンソールの「ストレージ › オブジェクト・ストレージとアーカイブ・ストレージ › バケット」で作ります。名前は `platform/.env` の既定の `production-ready`（別の名前なら配備の後に画面で指定します）。リージョンは既定では stack と同じ（stack が `PLATFORM_OBJECT_STORAGE_REGION` に stack の `region` を書きます）。3 製品は OCI SDK の署名付きの要求で読み書きするので、bucket は公開しなくてかまいません。 |
| IAM の policy | 「システム設定 › OCI 認証」で設定する API キーのユーザーのグループに、bucket の参照と object の読み書き・削除を許します: `Allow group <group> to read buckets in compartment <bucket の compartment>` と `Allow group <group> to manage objects in compartment <bucket の compartment>` |

- 配備の後に `system_admin` でログインし、「システム設定 › OCI 認証」の「Object Storage」で、namespace を「取得」ボタンで取得して（手入力はできません）リージョンとともに保存します。続けて「システム設定 › アップロード保存先」で「OCI Object Storage」を選び、bucket を指定して「接続を確認」を押します。設定は 3 製品で共通です。
- 「接続を確認」と保存のときは、bucket の参照（`read buckets`）と、確認用の小さな object の書き込み・読み取り・削除（`manage objects`）を試します。bucket が無い・権限が足りないときは保存しません（#2101）。
- Object Storage へは Compute から HTTPS（TCP `443`）で出ます。private subnet なら NAT gateway か Service gateway の経路を用意します（上の egress）。

## 配備した後の初期設定（推奨のロールとユーザー）

配備の直後にログインできるのは、構成のシステム管理者（ログイン ID `system_admin`）だけです。
日々の管理と一般の利用には、次のロールを作ることを推奨します。`system_admin` は初期設定・システム設定の変更・緊急時だけに使います。

| ロールコード | ロール名 | 権限 |
|---|---|---|
| `APP_ADMIN` | アプリ管理者 | 3 製品の「システム設定」**以外**のすべての権限 |
| `APP_USER` | 一般利用者 | 3 製品の「AI 活用」の権限だけ |

- 「システム設定」（OCI 認証・アップロード保存先・モデル・データベース・外観と証明書）は `APP_ADMIN` にも付けず、`system_admin` だけが変えます（接続と認証の設定を日々の管理の操作で壊さないため）。

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

Compute（Oracle Linux 9）に `opc` で SSH して実行します（`ssh opc@<IP>`。output の `ssh_to_instance`。repository は `/u01/aipoc/no.1-production-ready-suite`）。

最新のコード（main）を取得して反映する（取得 → build → 全サービスの再起動）:

```bash
sudo /u01/aipoc/no.1-production-ready-suite/platform/deploy/suite-update.sh
```

全サービスを再起動する（今のコードから build してから再起動。`suite-pull.sh` で取得したコードもこれで反映されます）:

```bash
sudo /u01/aipoc/no.1-production-ready-suite/platform/deploy/suite-restart.sh
```

- build は、依存の同期・共有 UI と各製品の frontend・DB の migration・systemd・Nginx まで行います。build せずに再起動だけするときは `--no-build`、状態だけを見るときは `--status`。
- 2 つのスクリプト（`--status` を除く）は、最初に ADB の状態を確かめ、止まっていれば `platform/.env` の OCI の認証（システム設定 › OCI 認証 で保存した `~/.oci/config`）で起動し、AVAILABLE になってから続けます（最大 600 秒）。起動できないとき（OCI の認証が無い・権限が無い・時間切れ）も止まらず、理由と手で起動する方法を warning として出し、build と再起動を続けます（DB に接続できなくてもアプリケーションは起動します。#1756）。DB に接続できない間も `system_admin` はログインでき、「システム設定 › データベース」から ADB を起動すれば、スクリプトを実行し直さなくてもそのまま DB を使えます（#1762）。要る IAM の権限は [terraform/README.md](./terraform/README.md#adb-の自動の起動1745) を参照してください。
- 取得だけを行うときは `platform/deploy/suite-pull.sh`（追跡ファイルに変更があると中止します）。
- 画面で保存した設定（各製品の `backend/.env`・`platform/.env`）、Wallet、HTTPS の CA は保たれます。
- 時間がかかるので `tmux` の中で実行すると、SSH が切れても止まりません。log は `/var/log/suite-ops.log`。
- OS を再起動すると、各製品のサービスと Nginx は自動で起動します。

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

配備した Suite の 3 製品は MCP（`POST /<製品>/api/mcp`）を公開しています。Claude Code などの外部の Agent は、利用者が自分で発行した API キーで接続し、`skills/` の 3 つのスキル（業務 RAG 調査・構造化データ照会・資料とデータの横断調査）と組み合わせて使います。以下の `<host>` は配備した Compute のアドレス（例 `203.0.113.10`。公開の port が 443 以外なら `203.0.113.10:8443`）です。Claude Code を動かす端末から公開の port に届くよう、[security list](#配備の前に-vcn-の-security-list) で許可しておきます。

### 1. API キーを発行する

1. RAG・NL2SQL・Agent のどれかの画面にログインし、左下のアカウントの欄（名前・ロール）→「API キー」のタブ（`/account?tab=api-keys`）を開きます。
2. 名前（用途。例「会社の PC の Claude Code」）・使える製品（RAG / NL2SQL / Agent。既定は 3 つとも）・有効期限（30 / 90 / 365 日・無期限）を選んで発行します。Agent は使える業務 Agent を絞れます（Agent の画面で発行するとき）。
3. 秘密（`prak_…`）は **発行の直後に 1 回だけ** 表示されます。すぐに次の手順のファイルに保存します。

キーは発行した利用者の権限で動き、MCP の入口でだけ使えます（画面用の API には使えません）。

### 2. キーと接続の設定をファイルに置く

キーは repo・共有の設定に書かず、自分だけが読めるファイルに置きます。Claude Code にはシェルの環境変数（`~/.bashrc`）ではなく、Claude Code 自身の設定（`~/.claude/settings.json` の `env` と MCP の `headersHelper`）で渡します。Claude デスクトップ版（Code タブ）は `~/.bashrc` を読まずに起動するので、環境変数に頼るとターミナル版でしか動きません（デスクトップ版で `Missing environment variables: PR_API_KEY`・`UNABLE_TO_VERIFY_LEAF_SIGNATURE` になる）。

```bash
mkdir -p ~/.config/production-ready && chmod 700 ~/.config/production-ready
# 自作の CA の証明書（配備の既定の HTTPS。正規の証明書に替えた環境では不要）
curl -sk https://<host>/platform/ca.crt -o ~/.config/production-ready/ca.crt
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

CA とツールの待ち時間を `~/.claude/settings.json` の `env` に足します（ほかの設定は残します）。

```bash
python3 - <<'EOF'
import json, pathlib
home = pathlib.Path.home()
path = home / ".claude" / "settings.json"
settings = json.loads(path.read_text()) if path.exists() else {}
settings.setdefault("env", {}).update({
    "NODE_EXTRA_CA_CERTS": str(home / ".config/production-ready/ca.crt"),  # 自作の CA を信頼させる（正規の証明書なら不要）
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
    "NODE_EXTRA_CA_CERTS": "<ホームの絶対パス>/.config/production-ready/ca.crt",
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
claude plugin marketplace add https://github.com/engchina/no.1-production-ready-suite.git --sparse .claude-plugin skills
claude plugin install production-ready-skills@production-ready-suite
```

repo を clone 済みの環境（配備した Compute など）では、`claude plugin marketplace add <repo のパス>` でその checkout の `skills/` を使えます（配備の更新でスキルも新しくなります）。

### 5. 確かめる

```bash
# 環境変数を空にして、デスクトップ版と同じ条件で確かめる（rag・nl2sql・agent が ✔ Connected）
env -i HOME="$HOME" PATH="$PATH" claude mcp list
claude plugin list   # production-ready-skills@production-ready-suite が ✔ enabled
# ツールの一覧（キー・CA・URL の切り分け）
( . ~/.config/production-ready/env
  curl -sS --cacert ~/.config/production-ready/ca.crt -H "Authorization: Bearer $PR_API_KEY" -H 'Content-Type: application/json' \
    https://<host>/rag/api/mcp -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' )
```

設定を変えたら、Claude Code のセッションを新しく始めます（CA は起動のときにだけ読まれ、開いているセッションには効きません。デスクトップ版も新しいセッションを開きます）。新しいセッションでは、`/mcp` で 3 つのサーバーが connected か、`/plugin` でスキルが入っているかを見たうえで、読み取りだけのツールを呼ぶ質問で確かめます（例「rag と nl2sql の MCP で、私が使える検索・回答プロファイルと業務プロファイルを一覧にしてください」「agent の MCP で、使える業務 Agent を一覧にしてください」）。

その後は普段の質問で使えます（質問に合うスキルを Claude Code が選び、RAG は `mcp__rag__rag_search`、NL2SQL は `mcp__nl2sql__nl2sql_query`、業務 Agent は `mcp__agent__agent_chat` などを呼びます）。非対話の `claude -p` では `--allowedTools "Skill,mcp__rag,mcp__nl2sql,mcp__agent"` を渡します。業務 Agent が使うスキルは `agent_skill_export` で Agent Skills の形で取り出し、`~/.claude/skills/<name>/` に置けます。

### 更新・キーの削除

- スキルの更新: `claude plugin marketplace update production-ready-suite` の後に `claude plugin update production-ready-skills@production-ready-suite`。
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
- OCI Compute への配備（`init_script.sh` の systemd・Nginx・Terraform）の backend は従来どおり `127.0.0.1:8000` / `8010` / `8020` です（1 台の Compute に suite と同居しないため変えていません）。
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
