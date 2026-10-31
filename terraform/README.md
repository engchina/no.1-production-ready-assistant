# Production Ready Suite — OCI Resource Manager stack

`terraform/stack/` は、3製品（RAG / NL2SQL / Agent Control Plane）を OCI Resource Manager から配備する **1つだけの** Terraform stack です（#217）。
以前は製品ごとに `rag/` `nl2sql/` `agent/` の下に stack がありましたが、この stack に統合しました。

作成・設定するもの:

- Oracle Autonomous AI Database を **1つ**（新規作成、または既存 ADB の選択）と、その Wallet。選んだ製品すべてで共有する
- OCI Compute を **1台だけ**（Oracle Linux 9。ログインのユーザーは `opc`。#1316・#1611）。選んだ製品（`deploy_rag` / `deploy_nl2sql` / `deploy_agent`）をすべてこの 1 台に入れる
- Compute の cloud-init。suite monorepo を1回 clone し、[`platform/deploy/suite-init.sh`](../platform/deploy/suite-init.sh) が
  選んだ製品の `init_script.sh`（[`rag/`](../rag/init_script.sh) / [`nl2sql/`](../nl2sql/init_script.sh) / [`agent/`](../agent/init_script.sh)）を順に呼ぶ。
  reverse proxy（Nginx など）と TLS の証明書は作らない（#19。前段に置くかは利用者の選択。[README](../README.md#各製品の公開reverse-proxy-は利用者の選択)）
- 3製品で共通のサービス間 token の署名鍵（`random_password`。共通 `.env` の `PLATFORM_SERVICE_TOKEN_SECRET`。#233）
- 任意で bastion の Compute を 1 台（`bastion_enabled`、既定 off。VNC のデスクトップと Google Chrome 入り。製品は入れない。下の「bastion の Compute」。#2670）

```text
              ┌──────────────────────────────┐
              │ Autonomous AI Database（1つ） │  PLATFORM_* / RAG_* / NL2SQL_* / AGENT_* の table
              └──────────────┬───────────────┘
                             │ Wallet（1つ）
  ┌──────────────────────────┴───────────────────────────────────────────────┐
  │ Compute（1台。PRODUCTION_READY_SUITE）                                    │
  │   RAG    backend 127.0.0.1:8000・frontend の dist（base /rag/）           │
  │   NL2SQL backend 127.0.0.1:8010・frontend の dist（base /nl2sql/）        │
  │   Agent  backend 127.0.0.1:8020・frontend の dist（base /agent/）         │
  │   （reverse proxy は配備しない。前段に置くかは利用者の選択。#19）              │
  │   systemd: 各製品の backend・worker、RAG の前処理 / parser（127.0.0.1:18010〜） │
  └──────────────────────────────────────────────────────────────────────────┘
```

## 対応リージョン

- **stack は `ap-osaka-1`（大阪）、`ap-tokyo-1`（東京）、`us-chicago-1`（Chicago）をサポートします。** Compute の image（`instance_image_source_id`）は、この 3 つのリージョンの Oracle Linux 9 の platform image（`Oracle-Linux-9.8-2026.09.18-0`）の OCID に固定しています（#1611）。
  - `ap-osaka-1`（既定）: `ocid1.image.oc1.ap-osaka-1.aaaaaaaabdp5adn6fy2h63s5iizgqv77wz4fqrjzosackszdo2gn5rn436ha`
  - `ap-tokyo-1`: `ocid1.image.oc1.ap-tokyo-1.aaaaaaaawxmm5rja45shcpoactn4avvfv2sty55ap5jnaxqmpzbgtdc7qcsa`
  - `us-chicago-1`: `ocid1.image.oc1.us-chicago-1.aaaaaaaaltnu52rsd7s6p7yepblc6cdq7b7orqkndiwlz2eggv5ja2ltn52a`
  - image を新しくするときは、`schema.yaml`・`variables.tf`・`terraform/scripts/verify_stack_contract.py`（`ORACLE_LINUX_IMAGES`）を同じ PR でそろえます。
  これら以外のリージョンで stack を作ると、`region` の validation で plan が失敗します。
- 共通 `.env` の `PLATFORM_ORACLE_ADB_REGION` には stack のリージョンが入ります（OCI からの Wallet の取得に使います）。システム設定 > データベース は ADB の起動・停止・状態の取得を扱いません（#10）。

## フォームの入力順

設定は用途別に、次の順に表示します。新規 / 既存 ADB と bastion の設定は選択に応じて表示します。

1. 配備先のコンパートメント
2. ADB の利用方法、新規 / 既存 ADB、ADB のネットワーク、アプリ用の DB ユーザー
3. 主 Compute の構成、ネットワークと SSH、HTTPS
4. bastion の作成、専用の構成、ネットワークと接続
5. アプリケーション管理者、RAG の追加 parser、NL2SQL Deep Data Security

非表示の内部設定と製品の選択は表示しません。主 Compute と bastion の image 候補は大阪・東京・Chicago の順で、既定はどちらも大阪です。
配備後の output は、アプリケーションの入口、接続、ADB 情報の順に表示します。

## 配備する製品

Resource Manager の画面では製品を選ばず、RAG / NL2SQL / Agent Control Plane の 3 製品をすべて 1 台の Compute に配備します（#1562。Suite はセットで配備する）。
「配備する製品」のグループと `deploy_rag` / `deploy_nl2sql` / `deploy_agent` は画面に出しません（`visible: false`。既定はすべて `true`）。

- 変数は残しています。Terraform の CLI で一部の製品を外すときは `-var deploy_<製品>=false` を渡します（最低 1 つ。1 つも選ばないと plan が precondition で失敗します）。
  外した製品の入力グループ（`RAG` / `NL2SQL Deep Data Security`）は画面に出ません。
- `app_admin_login_user_password`（構成管理者 `system_admin`、3 製品で共通）は必須です。
  NL2SQL で Deep Data Security を有効にする場合は `nl2sql_oracle_deepsec_data_user_password` も必要です。
- 製品の選択は Compute の cloud-init に入るため、選択を変えて apply すると **Compute は作り直されます**（OCI provider は `user_data` の変更で instance を置き換える）。データの正本は ADB で、
  Compute の中のローカルの保存（アップロードの原本・`platform/.env` を画面で変えた値）は失われます。
- 配備が終わると、`/var/log/suite-init.log` に製品ごとの backend の health の URL と frontend の dist の場所を出します。
  その後に配備全体の確認の結果（`check:` の行）を出します（下の「配備が正常かの確認」）。

## Autonomous AI Database（全製品で共有）

- **対応バージョン**: 3製品とも Oracle AI Database 26ai 以降が前提です（`VECTOR` 型と Oracle AI Vector Search を使うため）。`adb_db_version` は既定の `26ai` のまま使ってください。
  NL2SQL の SQL ドメインと annotation は旧 Oracle Database 23ai で入った機能で、それより前のバージョンでは NL2SQL がその機能を使いません。
  リポジトリの文書・画面・コメントでは製品名にバージョンを入れず（#564）、対応バージョンはここだけに書きます。
- `adb_deployment_mode` で「新規 Autonomous AI Database の作成」か「既存の Autonomous AI Database を選択」を選びます。
  - 新規: 既定の DB 名は `SUITEADB`、workload は `LH`（Lakehouse。#1558）、ECPU は `2`。
    選べる workload は `OLTP` / `AJD` / `LH` です。APEX Service は SQL*Net 接続を提供しないため、フォームと Terraform CLI では選べません。
    3製品を載せる場合は、負荷に合わせて ECPU 数とストレージを上げてください。
  - 既存: SQL*Net 接続が使える ADB の OCID を選びます（APEX Service は利用できません）。`PLATFORM_ORACLE_DSN` は空なら `<db_name>_high` です。
    stack は既存 ADB から Wallet を生成するだけで、ネットワーク・アクセス・mTLS・ACL は変更しません。
    アプリ用の DB ユーザーを使う（既定）ときは ADMIN のパスワード（`existing_adb_admin_password`）を、使わないときは
    全製品の `PLATFORM_ORACLE_USER` / `PLATFORM_ORACLE_PASSWORD` に書く値（`existing_oracle_user` / `existing_oracle_password`）を入力します。
- **3 製品は既定で、アプリ用の DB ユーザー（ADMIN ではないユーザー。既定の名前は `ADB_APP_USER`）で接続し、システムの表もそのユーザーの schema に作ります**
  （下の「[アプリ用の DB ユーザー](#アプリ用の-db-ユーザー16681675)」。#1668）。新規 / 既存の ADB のどちらでも使えます。
  使わない（`adb_app_user_enabled=false`）ときは、新規の ADB は `ADMIN`、既存の ADB は `existing_oracle_user` で接続します。
  製品のテーブルは `RAG_` / `NL2SQL_` / `AGENT_`、3製品で共有するユーザー・ロール・セッションのテーブルは `PLATFORM_` の接頭辞で分かれているため、同じスキーマでも衝突しません（#212）。
- ネットワーク・アクセス（既定はプライベート・エンドポイント）の画面構成は Autonomous AI Database の作成画面に合わせています。
  ACL（許可された IP および VCN）を使う場合は、Compute の subnet（または VCN）を許可してください。
  プライベート・エンドポイントの場合は、ADB のプライベート・エンドポイントの subnet の security list（stack の外）に、
  **送信元 = Compute の subnet の CIDR、TCP `1522`** の ingress の規則を足してください（同じ subnet なら、その subnet 自身の CIDR から）。
  規則の一覧は直下の [README.md](../README.md#配備の前に-vcn-の-security-list) にあります。
- DB の password / user / DSN に single quote は使えません（共通 `.env` では値を single quote で囲むため）。
- **時間帯（#3255・#3274）**: 時刻は UTC で保存します（3 製品の DB の session の時間帯は、Compute の OS の時間帯に関わらず
  `'+00:00'` に固定します。`pr_backend_core.oracle_session`）。stack の `timezone`（`UTC`〔既定。OCI の Database の Time Zone の既定〕か `Asia/Tokyo`〔日本時間: 東京・大阪など〕）は Compute の OS と
  共通の設定 `PLATFORM_TIMEZONE`（今日〔検索・期間の基準日〕・既定の時間帯・画面の日時の表示・NL2SQL の業務データの SQL の今の日時。#3265）に使い、ADB の時間帯（`DBTIMEZONE`）は変えません
  （OCI の Terraform provider の `oci_database_autonomous_database` に時間帯の引数が無く、stack は DB に接続しないため）。
  ADB の時間帯も合わせるときは、ADMIN で次を実行し、ADB を再起動します（再起動の間は 3 製品が DB を使えません。共有の ADB では
  使っている全員に確かめてから）。
  ```sql
  SELECT DBTIMEZONE FROM DUAL;                -- 今の値（既定 +00:00）
  ALTER DATABASE SET TIME_ZONE = 'Asia/Tokyo'; -- PLATFORM_TIMEZONE と同じ IANA の地域名
  -- OCI コンソールで ADB を再起動する（「その他のアクション › 再起動」）。その後:
  SELECT DBTIMEZONE FROM DUAL;                -- Asia/Tokyo
  ```
  - `ALTER DATABASE SET TIME_ZONE` は `TIMESTAMP WITH LOCAL TIME ZONE` の列がある DB では `ORA-30079` で失敗します（3 製品の表には無い）。
  - DB の時間帯を変えても `SYSDATE` / `SYSTIMESTAMP` は UTC のままです（ADB の `SYSDATE_AT_DBTIMEZONE` の既定 `FALSE`）。
    **`SYSDATE_AT_DBTIMEZONE` を `TRUE` にしない**でください（3 製品は `SYSTIMESTAMP` で書いた時間帯の無い `TIMESTAMP` の列を UTC として
    読むため、時刻が 9 時間ずれます）。
  - 参考: [Autonomous AI Database の SYSDATE_AT_DBTIMEZONE](https://docs.oracle.com/en/cloud/paas/autonomous-database/serverless/adbsb/autonomous-initialization-parameters.html)、[OCI Database の Time Zone](https://docs.cloud.oracle.com/Content/Database/References/timezones.htm)。
- ADB の DDL は Terraform にも cloud-init にも持ちません。各製品の `init_script.sh` がアプリの CLI（冪等）で作成・更新します。
  作成・更新に失敗しても backend は起動します（画面の システムテーブル から直せる）が、配備の最後の確認が `check: <製品> schema NG` で失敗します（[配備が正常かの確認](#配備が正常かの確認1642)）。

### アプリ用の DB ユーザー（#1668・#1675）

3 製品が ADB に接続するユーザーです。**既定（`adb_app_user_enabled=true`。推奨）では、ADMIN ではないアプリ用のユーザー（既定の名前 `ADB_APP_USER`）を作って使います。**
ADMIN を常用しないためです（アプリの不具合・NL2SQL の管理 SQL の誤りを DB 全体の管理者の権限で実行させない。ADMIN のパスワードを `opc` が読む `.env` に置かない）。

| `adb_app_user_enabled` | 新規の ADB | 既存の ADB |
|---|---|---|
| `true`（既定・推奨） | `adb_app_user_name`（既定 `ADB_APP_USER`）を作って接続する。ADMIN のパスワードは `adb_password` | 同じ。ADMIN のパスワードは `existing_adb_admin_password` |
| `false` | `ADMIN`（`adb_password`）で接続する（#1668 の前と同じ） | `existing_oracle_user` / `existing_oracle_password` で接続する（下の「[アプリ用の DB ユーザーを使わないとき](#アプリ用の-db-ユーザーを使わないとき自分で用意したユーザー1648)」） |

フォーム（`schema.yaml`）では、「Autonomous AI Database」の group の「ADBの利用方法」の直後に「アプリ用の DB ユーザーを使う」があり、
新規 / 既存の ADB の group（ADMIN のパスワード、使わないときは既存の DB のユーザー名・パスワード）とネットワーク・アクセスの後に、
「アプリ用の DB ユーザー」の group（名前・パスワード）が出ます。

#### 作り方（Compute の上で行う）

- cloud-init が ADMIN のパスワードとユーザー名を `/u01/aipoc/props/adb_admin.env`（root の 0600）に置き、`platform/deploy/suite-init.sh` が
  製品の配備（各製品の system schema の初期化）の前に、**Compute から** ADMIN で ADB に接続して `platform/deploy/suite-db-user.py` を実行します
  （Wallet・mTLS は製品と同じ。`uv run --no-project --with oracledb==<版>`）。
- ADB はプライベート・エンドポイント・ACL で Compute からしか接続できないため、Terraform（provider・`local-exec`）では DB に接続しません
  （`verify_stack_contract.py` が stack に provisioner が無いことを確かめます）。
- **冪等**: ユーザーが無ければ `CREATE USER`、あれば profile・quota を合わせます。アプリのパスワードで接続できなければ（`ORA-01017` など）
  パスワードを合わせて lock を外します。パスワードは `platform/.env`（画面の「システム設定 > データベース」で保存した値）を優先し、無ければ stack の入力です。
  grant は毎回付け直し、最後にアプリ用のユーザーで接続して権限が効いていることを確かめます。
- **失敗したとき**: ORA の番号と手がかりを `/var/log/suite-init.log` に出し（`[suite-db-user] NG（ORA-01017）: …`・`[suite-init] check: db user NG`）、
  warning を出して製品の配備と起動を続け、最後に失敗（0 以外）を返します（ADB に接続できなくてもアプリケーションは起動する。#1756）。
  ユーザーが無い間は 3 製品とも DB に接続できません。直したら `sudo .../platform/deploy/suite-restart.sh` でやり直します。
- **ADMIN のパスワードの置き場所**: `platform/.env`・各製品の `backend/.env`・Wallet のパスワード（`PLATFORM_ORACLE_WALLET_PASSWORD` はアプリ用のユーザーのパスワード）には書きません。
  `/u01/aipoc/props/adb_admin.env`（root の 0600）にだけ置き、配備の再実行（`suite-restart.sh` / `suite-update.sh`）のたびに、新しい版が要る権限を付け直します
  （使った後に消すと、再配備で権限を足せなくなるため残します）。値は Terraform の state と、Compute の cloud-init の user_data（インスタンスのメタデータ）にも入るため、
  ほかの secret と同じく機密として扱ってください。不要になったら `sudo rm /u01/aipoc/props/adb_admin.env` で消せます（その後の再配備はユーザーの準備を飛ばします）。

#### 付ける権限

`python3 platform/deploy/suite-db-user.py plan` で表示できます。

```sql
CREATE USER ADB_APP_USER IDENTIFIED BY "<password>" DEFAULT TABLESPACE DATA QUOTA UNLIMITED ON DATA PROFILE ORA_APP_PROFILE;
GRANT PDB_DBA TO ADB_APP_USER;                       -- Autonomous AI Database の DBA 相当
GRANT GRANT ANY DATA ROLE TO ADB_APP_USER;           -- Deep Data Security: DATA ROLE を END USER に付ける
GRANT ADMINISTER ANY DATA GRANT TO ADB_APP_USER;     -- Deep Data Security: Data Grant の作成・削除
GRANT SET USE DATA GRANTS ONLY TO ADB_APP_USER;      -- Deep Data Security: 表を Data Grant だけで読ませる
```

- **`DBA` ではなく `PDB_DBA`**: Autonomous AI Database の ADMIN は `DBA` を `WITH ADMIN OPTION` で持たず、他のユーザーに grant できません
  （[ADMIN User Roles and Privileges](https://docs.oracle.com/en/cloud/paas/autonomous-database/serverless/adbsb/autonomous-admin-user-roles.html)）。
  実 ADB では `GRANT DBA TO <user>` はエラーにならず、`PDB_DBA` と `UNLIMITED TABLESPACE` が付きました。ADMIN は `PDB_DBA` を `WITH ADMIN OPTION` で持ちます。
- **`PDB_DBA` に無く、足すもの**: Deep Data Security の `GRANT DATA ROLE … TO <END USER>`（`GRANT ANY DATA ROLE`）、`SET USE DATA GRANTS ONLY ON <表>`
  （`SET USE DATA GRANTS ONLY`）、`CREATE DATA GRANT`（`ADMINISTER ANY DATA GRANT`）。無いと `ORA-01031` です。表領域は `PDB_DBA` に
  `UNLIMITED TABLESPACE` が無いため `QUOTA UNLIMITED ON DATA` にします。
- **profile は `ORA_APP_PROFILE`**（`PASSWORD_LIFE_TIME UNLIMITED`・`FAILED_LOGIN_ATTEMPTS UNLIMITED`）。既定の `DEFAULT` だと 360 日でパスワードが切れて
  3 製品が接続できなくなり、10 回の失敗で lock されます。`ORA_APP_PROFILE` は `PASSWORD_ROLLOVER_TIME`（90 日）のあいだ古いパスワードでも接続できるため、
  パスワードを合わせた後は `ALTER USER … EXPIRE PASSWORD ROLLOVER PERIOD` で新しいパスワードだけにします。profile が無い DB では `DEFAULT` のままにし、log に出します。

この権限で 3 製品の機能が動くことを、実 ADB（26ai）で一時のアプリ用のユーザーに同じ権限を付けて、そのユーザーで実行して確かめました（#1668・#1675。
コードの `DBMS_*`・`CTX_*`・`UTL_*`・dictionary view・DDL・`GRANT` の使い方を網羅的に洗い出した上で、使っているものをすべて実行した。
業務データは別の schema の表・view・materialized view で確かめた）。

| 製品・機能 | 実行して確かめたもの | 結果 |
|---|---|---|
| 3 製品の system schema（初期化・確認） | RAG `system_schema_cli check / initialize`、NL2SQL `nl2sql_system_schema --check / --initialize` と `app_security_migrate --apply`、Agent `agent_system_schema --check / --initialize`（表・index・sequence・IDENTITY・`IS JSON`・SQL ドメイン・package（`AUTHID DEFINER`）・`ALTER SESSION`） | すべて ready |
| RAG の検索 | `VECTOR` 列・HNSW の vector index・`VECTOR_DISTANCE`・`V$VECTOR_GRAPH_INDEX`、Oracle Text（`CTX_DDL.CREATE_PREFERENCE` / `CREATE_STOPLIST`・`CTXSYS.CONTEXT` の索引・`CONTAINS`・`CTX_USER_PREFERENCES`） | OK |
| NL2SQL の DB 構造の取得 | 他の schema の `ALL_TABLES` / `ALL_TAB_COLS`（`DOMAIN_NAME`）/ `ALL_TAB_COMMENTS` / `ALL_COL_COMMENTS` / `ALL_CONSTRAINTS` / `ALL_DEPENDENCIES` / `ALL_DOMAINS` / `ALL_DOMAIN_COLS` / `ALL_ANNOTATIONS_USAGE` / `ALL_TRIGGERS` / `ALL_MVIEWS` / `ALL_USERS`、業務表の `SELECT`、`EXPLAIN PLAN`（他の schema）と `PLAN_TABLE`、`DBMS_METADATA.GET_DDL`（他の schema の view・MV） | OK |
| NL2SQL のデータ準備（コメント・アノテーション・ドメイン・DB 管理・CSV） | 他の schema への `COMMENT ON TABLE / COLUMN / MATERIALIZED VIEW`、`ALTER TABLE / VIEW / MATERIALIZED VIEW … ANNOTATIONS`（列も）、`CREATE DOMAIN IF NOT EXISTS`・`ALTER DOMAIN`・`ALTER TABLE MODIFY … ADD / DROP DOMAIN`・`DROP DOMAIN FORCE`、`CREATE TABLE` / `CREATE VIEW`（管理 SQL・AI の DDL）、`INSERT` / `UPDATE` / `DELETE`、`TRUNCATE TABLE`・`DROP TABLE PURGE`・`DROP VIEW` | OK |
| NL2SQL の Select AI | `DBMS_CLOUD.CREATE_CREDENTIAL`（API キーの形）/ `DROP_CREDENTIAL`、`DBMS_CLOUD_AI.CREATE_PROFILE`（`object_list` は他の schema）/ `DROP_PROFILE`、`DBMS_CLOUD_AI.GENERATE`（`showsql` / `showprompt`。実の OCI Generative AI で SQL が返る）、`USER_CLOUD_AI_PROFILES` / `_ATTRIBUTES`、`ALL_PROCEDURES`（package の可視性） | OK |
| NL2SQL のフィードバック | `DBMS_CLOUD_AI.FEEDBACK`（`ADD` / `DELETE`。`sql_text` と `response` で、`select ai` を実行していない文でも登録できる。feedback の vector 表 `<profile>_FEEDBACK_VECINDEX$VECTAB` を Oracle が作る）、`DBMS_CLOUD_AI.UPDATE_VECTOR_INDEX`、`DBMS_LOCK.REQUEST / RELEASE`（無名 block） | OK |
| NL2SQL の合成データ | `DBMS_CLOUD_AI.GENERATE_SYNTHETIC_DATA`（他の schema の表へ。実の LLM で 3 行）、`DBA_LOAD_OPERATIONS`・`V$SESSION`、preview の `CREATE TABLE … AS SELECT`（元は他の schema）・`ADD CONSTRAINT … ENABLE NOVALIDATE`・`LOCK TABLE <他の schema> IN SHARE ROW EXCLUSIVE MODE NOWAIT` | OK |
| NL2SQL の Select AI Agent | `DBMS_CLOUD_AI_AGENT.CREATE_TOOL / CREATE_AGENT / CREATE_TASK / CREATE_TEAM`、`RUN_TOOL`・`RUN_TEAM`（実の LLM。`params` の `conversation_id` で同じ会話を続ける）、`DROP_* (force => TRUE)`、`DBMS_SQL_TRANSLATOR.DROP_PROFILE`、会話の `DBMS_CLOUD_AI.CREATE_CONVERSATION`・`USER_CLOUD_AI_CONVERSATION_PROMPTS`・`USER_AI_AGENT_TEAM_HISTORY`（#1681） | OK |
| NL2SQL のオントロジーの操作 | 業務表への `SELECT … FOR UPDATE WAIT 5` と `UPDATE`（他の schema） | OK |
| NL2SQL の Deep Data Security | `CREATE ROLE`・`GRANT CREATE SESSION TO <role>`・`GRANT SELECT ON <他の schema の表> TO <role>` と `REVOKE`（`DBA_TAB_PRIVS` の確認を含む無名 block）、`CREATE DATA ROLE`・`CREATE / ALTER / DROP END USER`・`GRANT DATA ROLE`・`CREATE OR REPLACE CONTEXT … USING`、`SET USE DATA GRANTS ONLY … ENABLED / DISABLED`・`CREATE OR REPLACE DATA GRANT`（自分と他の schema の表）、`DBA_END_USERS` / `DBA_DATA_ROLES` / `DBA_ROLES` / `DBA_CONTEXT` / `DBA_DATA_GRANTS` / `DBA_POLICIES`、END USER で接続して Data Grant どおりの行だけが見えること、解除（`DROP DATA GRANT` / `CONTEXT` / `END USER` / `DATA ROLE` / `ROLE`） | OK（3 つの追加の権限が無いと `ORA-01031`） |
| Agent・platform | 自分の schema の表・`MERGE`・`LOCK TABLE`・`SELECT … FOR UPDATE SKIP LOCKED`・`USER_*` の参照だけ（上の system schema の初期化で確かめた） | OK |

- **使っていないもの**（コードを grep して確かめた）: `DBMS_SCHEDULER`・`DBMS_VECTOR` / `DBMS_VECTOR_CHAIN`（DB の中の embedding）・`DBMS_CLOUD` の Object Storage
  （`COPY_DATA`・`PUT_OBJECT`・外部表。Object Storage は OCI SDK で使う）・`DBMS_CLOUD_PIPELINE`・`UTL_*`・PUBLIC SYNONYM・DIRECTORY・DATABASE LINK・
  `ALTER SYSTEM`・`CREATE USER`（アプリのコードとして）。Select AI の network ACL（`DBMS_NETWORK_ACL_ADMIN.APPEND_HOST_ACE`）は、provider が OCI Generative AI で
  [公式の文書](https://docs.oracle.com/en/cloud/paas/autonomous-database/serverless/adbsb/select-ai-manage-profiles.html)が不要と書くため付けません。
  resource principal（`DBMS_CLOUD_ADMIN.ENABLE_RESOURCE_PRINCIPAL`）も、credential が API キーの形だけなので使いません。
- **stored PL/SQL の注意**: role 経由の権限は definer-rights の stored PL/SQL では効きません（例: package の中の `DBMS_LOCK` は `PLS-00201`）。
  今のコードが作る package（`NL2SQL_DEEPSEC_CTX_PKG`・`NL2SQL_ONT_ACTION_TX`）は自分の表と `DBMS_SESSION`（PUBLIC）だけを参照するため問題ありません。
  今後 package の中から `DBMS_*` や `DBA_*` を使うときは、`EXECUTE` / `SELECT` を直接 grant する必要があります（`suite-db-user.py` の `APP_USER_GRANTS` に足す）。
- **NL2SQL の管理 SQL**: 確認語を入れた管理者は、アプリ用のユーザーの権限（`PDB_DBA` を含む）で任意の SQL を実行できます。画面の権限（`menu.db_admin` など）を絞ってください。

#### 配備の後の確認

ADMIN で接続して実行します。

```sql
SELECT USERNAME, ACCOUNT_STATUS, PROFILE, DEFAULT_TABLESPACE FROM DBA_USERS WHERE USERNAME = 'ADB_APP_USER';
SELECT GRANTED_ROLE FROM DBA_ROLE_PRIVS WHERE GRANTEE = 'ADB_APP_USER';          -- PDB_DBA
SELECT PRIVILEGE FROM DBA_SYS_PRIVS WHERE GRANTEE = 'ADB_APP_USER' ORDER BY 1;   -- 足した 3 つ
SELECT TABLESPACE_NAME, MAX_BYTES FROM DBA_TS_QUOTAS WHERE USERNAME = 'ADB_APP_USER';  -- DATA / -1
SELECT COUNT(*) FROM DBA_TABLES WHERE OWNER = 'ADB_APP_USER' AND TABLE_NAME LIKE 'PLATFORM\_%' ESCAPE '\';
```

`/var/log/suite-init.log` に `[suite-db-user] OK: ADB_APP_USER で接続でき、権限が付いています（profile ORA_APP_PROFILE、quota UNLIMITED ON DATA）。` と
`[suite-init] check: db user OK` が出て、最後の `check:` の行で 3 製品の schema が `ready` になります。

#### ADMIN で配備していた環境

表は移しません（未リリースのため移行の仕組みは持ちません）。アプリ用のユーザーで配備し直すと、新しい schema に空の表を作ります
（ADMIN の schema の `PLATFORM_*` などは残ります。要らなければ ADMIN で消してください）。NL2SQL の Deep Data Security の object のうち、
role `NL2SQL_APP_DB_ROLE`・DATA ROLE `NL2SQL_APP_DATA_ROLE`・END USER `DEEPSEC_DATA_USER`・context `NL2SQL_APP_USER_CTX` は DB 全体で 1 つの名前です。
ADMIN で Deep Data Security を適用していた ADB では、先に ADMIN の構成で解除（`ADMIN_RESET`）してから、新しいユーザーで適用し直してください。

#### アプリ用の DB ユーザーを使わないとき（自分で用意したユーザー。#1648）

`adb_app_user_enabled=false` で、既存の ADB の `existing_oracle_user` に ADMIN 以外のユーザーを指定する場合です（新規の ADB は `ADMIN` で接続します）。

- **3 製品の専用の DB ユーザー（schema）を作ることを推奨します。** 3 製品は同じ schema に `PLATFORM_` / `RAG_` / `NL2SQL_` / `AGENT_` の表・索引・sequence・package を作ります。
  業務データや別のアプリケーション（旧版の単独の製品を含む）と同じ schema（とくに `ADMIN`）に入れると、同じ名前の表と衝突し、
  NL2SQL の業務プロファイル・管理 SQL の対象にも 3 製品以外の object が並びます。
- 初期化は、旧名の表（統合の前の `RAG_APP_*` / `NL2SQL_APP_*` など）を改名・削除しません（NL2SQL はその名前を log に出して残します）。
  RAG / Agent の初期化は、台帳（`RAG_SCHEMA_MIGRATIONS` / `AGENT_SCHEMA_MIGRATIONS`）に記録の無い schema に残る廃止した表を、
  承認（`--allow-destructive`、画面では削除の確認）が無ければ削除せずに止めます。
- 権限は、上の「付ける権限」の一式（`PDB_DBA` と Deep Data Security の 3 つ、`QUOTA UNLIMITED ON DATA`）を ADMIN で付けるのが確実です
  （上の表で全機能を確かめた構成）。最小にしたいときは、CI の実 Oracle の結合テスト（`.github/scripts/oracle-free-ci.sh`）と同じ次の構成に、
  使う機能の分を足します。

  ```sql
  CREATE USER <user> IDENTIFIED BY "<password>" DEFAULT TABLESPACE DATA QUOTA UNLIMITED ON DATA;
  -- 表・索引・sequence・view・procedure（NL2SQL の package）・SQL ドメインの作成
  GRANT DB_DEVELOPER_ROLE TO <user>;
  GRANT CREATE SESSION TO <user>;
  -- RAG: ベクトル索引の構築の値（NEIGHBORS / EFCONSTRUCTION）を読む
  GRANT SELECT ON SYS.V_$VECTOR_GRAPH_INDEX TO <user>;
  ```

  - `DB_DEVELOPER_ROLE` を使わない場合は、`CREATE TABLE`・`CREATE SEQUENCE`・`CREATE VIEW`・`CREATE PROCEDURE` と、RAG の全文検索（Oracle Text の索引・lexer・stoplist）の `CTXAPP`（または `EXECUTE ON CTXSYS.CTX_DDL`）を付与します。
  - 表領域の quota が無いと `ORA-01950`、権限が無いと `ORA-01031`（Oracle Text は `PLS-00201`）で初期化が失敗し、配備の最後の確認が NG になります。
  - NL2SQL が SQL を生成・実行する業務データが別の schema にあるときは、その表・view の `SELECT` を付与します。コメント・アノテーション・ドメイン・
    DB 管理・CSV・合成データ・オントロジーの操作で他の schema を変えるときは、その表への `ALTER` / `INSERT` / `UPDATE` / `DELETE` などか、
    `COMMENT ANY TABLE`・`ALTER ANY TABLE`・`CREATE ANY DOMAIN`・`LOCK ANY TABLE` などの ANY 権限が要ります。
  - NL2SQL の機能ごとに使うとき: フィードバックのベクトルの再構築は `EXECUTE ON DBMS_LOCK`、Select AI は `EXECUTE ON DBMS_CLOUD_AI`・`EXECUTE ON DBMS_CLOUD`
    （Select AI Agent は `EXECUTE ON DBMS_CLOUD_AI_AGENT`）、合成データの生成は `SELECT ON SYS.DBA_LOAD_OPERATIONS` と `SELECT ON SYS.V_$SESSION`。
  - NL2SQL の Deep Data Security（`NL2SQL_ORACLE_DEEPSEC_ENABLED=true`）: `CREATE ROLE`・`GRANT ANY ROLE`・`GRANT ANY OBJECT PRIVILEGE`・`CREATE END USER`・
    `ALTER END USER`・`DROP END USER`・`CREATE DATA ROLE`・`DROP DATA ROLE`・`CREATE ANY CONTEXT`・`DROP ANY CONTEXT`・`CREATE PROCEDURE`・
    `GRANT ANY DATA ROLE`・`ADMINISTER ANY DATA GRANT`・`SET USE DATA GRANTS ONLY` と、DeepSec の metadata（`DBA_END_USERS`・`DBA_DATA_ROLES`・`DBA_ROLES`・`DBA_CONTEXT`・
    `DBA_DATA_GRANTS`・`DBA_TAB_PRIVS`・`DBA_POLICIES`。`SELECT_CATALOG_ROLE`）の参照。詳しくは [nl2sql/docs/security-rbac-deepsec.md](../nl2sql/docs/security-rbac-deepsec.md)「DeepSec V001 の前提」。

## 製品ごとの入力と instance 上の構成

AI の設定（OCI 認証、OCI Enterprise AI、埋め込み / リランクなど）は stack では受け取りません。起動後に各アプリのシステム設定で行います。
stack は secret を cloud-init に埋め込んで `backend/.env` と共通 `platform/.env` を作るため、Resource Manager の stack・job 履歴・state は機密として扱ってください。

### 設定ファイル（#211）

Compute には `platform/.env` を 1 つと、製品ごとの `backend/.env` を置きます。cloud-init が `/u01/aipoc/props/`
（`platform.env`・`<製品>.backend.env`）に書き、各製品の `init_script.sh` がリポジトリへ配置します（0600）。

| ファイル | 内容 | 接頭辞 |
|---|---|---|
| `platform/.env` | 3製品共通の設定（ADB 接続・OCI の region / compartment・アップロード保存先・構成管理者 `PLATFORM_ADMIN_*`・サービス間 token の署名鍵 `PLATFORM_SERVICE_TOKEN_SECRET`・`PLATFORM_AUTH_COOKIE_SECURE`（`https_enabled` と同じ値）・時間帯 `PLATFORM_TIMEZONE`（`timezone` と同じ値。#3255）・ログインの Cookie 名 `PLATFORM_AUTH_SESSION_COOKIE_NAME` / `PLATFORM_AUTH_CSRF_COOKIE_NAME`）。1 台の Compute では 3 製品が同じファイルを読み書きする（システム設定画面の保存先）。既にあれば上書きしない | `PLATFORM_` |
| `<製品>/backend/.env` | その製品だけの設定 | `RAG_` / `NL2SQL_` / `AGENT_` |

`model-settings.json` は `platform/.env` と同じ場所の 1 つを 3 製品で共有し、アップロードの原本などのデータは製品ごとの
`/u01/data/production-ready-<製品>`（各製品の Settings の既定値）に置きます。3 製品のプロセスは同じユーザー `opc` で動きます
（共通の `platform/.env`・Wallet・`~/.oci/config` を画面から読み書きするため。#1316）。

変数名の規則と既存環境の移行は [AGENTS.md](../AGENTS.md) の「設定（`.env`）とデータベース object の命名」と各製品の配備ドキュメントを参照してください。

**LLM の出力の上限（#1719）**: 出力の上限は「システム設定 › モデル」の登録モデルごとの「出力の上限（tokens）」（既定 32768、入力の上限以下）です。
#1719 より前の共通の上限（`llm_max_output_tokens`。既定 1200）は読み捨て、`PLATFORM_OCI_ENTERPRISE_AI_LLM_MAX_OUTPUT_TOKENS` /
`_VLM_MAX_OUTPUT_TOKENS` も読みません。既存の環境を更新したら、システム設定 › モデル で各モデルの出力の上限が 32768（以上・入力の上限以下）
になっていることを確かめて「登録モデル: 保存」で保存し直してください（`model-settings.json` が version 6 になります）。推論のあるモデルは推論の
token も出力の上限に数えるため、1200 のような小さい値ではコメント管理・回答前の確認などの Structured Outputs が未完了になります。

### RAG（`deploy_rag`）

RAG は NL2SQL / Agent と同じく、Docker を使わずネイティブ（uv の venv + systemd）で動かします（#286）。
文書の前処理と解析は、依存が大きく異なるためサービスごとに uv の venv を分け、それぞれを systemd の unit にします。
詳細と既存環境（Docker Compose で配備した Compute）からの移行は [rag/docs/deployment.md](../rag/docs/deployment.md) を参照してください。

- ログイン: 共通認証（`PLATFORM_AUTH_MODE=authenticated`。#214）。最初は構成管理者 `system_admin`（`app_admin_login_user_password`）でログインし、
  「ユーザーとロール」でユーザーとロールを作り、「セキュリティ設定 > 権限管理」でロールごとのメニュー・検索・回答プロファイル・ナレッジベースを設定します。
  Cookie を HTTPS 限定にするか（`PLATFORM_AUTH_COOKIE_SECURE`）は `https_enabled` から決まります。`RAG_AUDIT_CONTEXT_HASH_SALT` は instance 上で生成します。
- 前処理 4 つと CPU の parser を配備します（出力 `rag_services`）。GPU の parser（MinerU / Dots.OCR）は含めず、
  起動後に「取込・検索・回答設定 > 文書解析」で外部 API として指定します。音声・動画のファイルは取り込みません。

  | service | 既定 | 入力 |
  |---|---|---|
  | `parser-docling` | 常に配備 | 既定の解析エンジン（`RAG_PARSER_ADAPTER_BACKEND=docling`。PDF と画像。#286） |
  | `parser-unstructured` | 配備する（#1559。文書解析の設定でも有効にする: `RAG_PARSER_UNSTRUCTURED_ENABLED`） | `rag_enable_parser_unstructured`（テキスト・HTML・Office・メールなど Docling が扱えない形式を、処理レシピで Unstructured を選んで取り込む場合） |
  | `parser-structured-records-jsonl` | 常に配備（#2457） | 構造化レコード JSONL の解析エンジン（`RAG_PARSER_ADAPTER_BACKEND=structured_records_jsonl`。ファイル準備の CSV→JSON・Excel→JSON が作る構造化レコードの JSONL）。文書解析の既定か処理レシピの使用エンジンで選んだときだけ使います。CSV / Excel の前処理と同じく、初めて配備したときに起動します |
  | `parser-xml-tree-json` | 常に配備・既定は停止（#2453） | XML ツリー JSON の解析エンジン（`RAG_PARSER_ADAPTER_BACKEND=xml_tree_json`。対応する形の `.json`）。文書解析の既定か処理レシピの使用エンジンで選んだときだけ使います（#2042）。初めて配備しても起動しないので、使うときは「運用設定 > サービス管理」で起動します |
  | `parser-oci-document-understanding` | 配備しない（準備中〔ロードマップ〕。#3374） | なし（入力 `rag_enable_oci_cloud_parsers` は既定 false のまま画面に出さず、値に関わらず配備しません） |

  Docling を配備するかを選ぶ入力（以前の `rag_enable_parser_docling`）は廃止しました（Docling は既定の解析エンジンのため常に配備します）。
  Marker の parser（`parser-marker`）と入力 `rag_enable_parser_marker` は削除しました（#270）。
  OCI Generative AI（Vision）の parser（`parser-oci-genai-vision`）と GPU の ASR の parser（`parser-asr`）、前処理の
  スキャン画像の補正（`preprocess-image-enhance`）と URL→Markdown（`preprocess-url-to-markdown`）は削除しました（#2042）。
  前処理の「PDF を画像として読み直す」（`preprocess-pdf-to-page-images`）も削除しました（#3347。スキャンの PDF は Docling が OCR で読みます）。
  取込時の「PIIマスク」（`preprocess-pii-redact`）と、その spaCy の日本語モデルの取得も削除しました（#3358）。
  OCI Document Understanding の parser は準備中（ロードマップ。#3374）で、まだ使えません。入力 `rag_enable_oci_cloud_parsers` は残しますが、Resource Manager の画面には出さず、
  値に関わらず配備しません（以前の配備で入れた unit は、再配備で止めて〔`disable --now`〕消し、sudoers からも外します）。既存の Compute は
  `sudo platform/deploy/suite-update.sh` の再配備で `parser-structured-records-jsonl`（起動します）と `parser-xml-tree-json`（起動はしません）の unit が入り、削除したサービスの unit は止めて消されます
  （以前の stack が書いた `/u01/aipoc/props/rag_services.txt` も今の一覧に書き戻します）。

- 前処理 / parser の起動 / 停止は「運用設定 > サービス管理」画面から行えます（`RAG_SERVICE_CONTROL_ENABLED=true`）。backend の実行ユーザーには、
  配備した前処理 / parser の unit の `systemctl enable --now / disable --now / restart` と `journalctl -u <unit>` を
  sudoers（`/etc/sudoers.d/production-ready-rag-services`）で許可します。利用者が最後に操作した起動 / 停止の状態は、再起動・再配備でも保たれます
  （初めて配備する unit は起動します。既定は停止の `parser-xml-tree-json` は起動しません）。
- RAG も NL2SQL / Agent と同じユーザー `opc` で動きます（#1316・#1611）。

| 項目 | 値 |
|---|---|
| backend | `production-ready-rag-backend.service`（`127.0.0.1:8000`）と `production-ready-rag-ingestion-worker.service`。`frontend/dist` は `/rag/` を base に build する（配信と `/rag/api/` の proxy は利用者が置く reverse proxy。#19） |
| 前処理 / parser | `production-ready-rag-<service>.service`（`rag/services/*/*/.venv`、`127.0.0.1:18010〜18028`） |
| 実行ユーザー | `opc`（1 台の Compute。#1316・#1611） |
| 設定 | `/u01/aipoc/no.1-production-ready-suite/rag/backend/.env` と `platform/.env`（`0600`） |
| ログ | `/var/log/rag-init.log`、`sudo journalctl -u production-ready-rag-backend.service -f` |

```bash
sudo tail -f /var/log/rag-init.log
systemctl list-units --all 'production-ready-rag-*'
# RAG の system schema の初期化（冪等）を手動で再実行する
cd /u01/aipoc/no.1-production-ready-suite/rag/backend
sudo -u opc .venv/bin/python -m app.rag.system_schema_cli initialize
sudo systemctl restart production-ready-rag-backend.service
```

### NL2SQL（`deploy_nl2sql`）

- ログイン: 構成管理者 `SYSTEM_ADMIN`（ユーザー ID は `system_admin` 固定、RAG と共通）。`app_admin_login_user_password`（12〜30 文字、`admin` と `"` を含まない）。
- Deep Data Security: `nl2sql_oracle_deepsec_enabled`（既定 `false`。使う環境だけがオンにします。#1707）。オンの場合は `nl2sql_oracle_deepsec_data_user_password` が必要です。
  配備した後に「セキュリティ設定 > Deep Data Security」で DATA USER のパスワードを保存しても有効になります（`backend/.env` に `NL2SQL_ORACLE_DEEPSEC_ENABLED=true` を書きます）。
- systemd に直接配備します（Docker は使いません）。backend は `127.0.0.1:8010`（ローカルの開発と同じ。#1316 で 8000 から変えた）。
- instance 上の構成・更新手順・障害対応は [nl2sql/docs/compute-operations.md](../nl2sql/docs/compute-operations.md) を参照してください。

### Agent Control Plane（`deploy_agent`）

- ログイン: 共通認証（`PLATFORM_AUTH_MODE=authenticated`。#215）。最初は構成管理者 `system_admin`（`app_admin_login_user_password`。RAG / NL2SQL と共通）で
  ログインし、「ユーザーとロール」でユーザーとロールを作り、「セキュリティ設定 > 権限管理」でロールごとのメニュー・実行 / 承認 / 監査 / 管理の権限・
  エージェントを設定します（検索・回答プロファイルは RAG の権限管理）。`init_script.sh` が `python -m app.cli.agent_system_schema --initialize` で認証・権限のテーブルを作ります。
  Nginx の Basic 認証は廃止しました。
  Cookie を HTTPS 限定にするか（`PLATFORM_AUTH_COOKIE_SECURE`）は `https_enabled` から決まります。
  Cookie のないリクエストは 401 です（header / JWT の認可は #750 で削除。[agent/docs/security-rbac.md](../agent/docs/security-rbac.md)）。
- RAG / NL2SQL との連携（#233）: 同じ stack で RAG / NL2SQL も配備すると、Agent の `backend/.env` に
  `AGENT_EXTERNAL_RAG_MCP_URL` / `AGENT_EXTERNAL_NL2SQL_MCP_URL`（同じ Compute の backend を直接呼ぶ `http://127.0.0.1:8000/api/mcp` /
  `http://127.0.0.1:8010/api/mcp`。reverse proxy を通さない。#1316）を書きます。
  これは Agent の「MCP 接続」の `rag` / `nl2sql` の初期値です（#757）。配備しなかった製品の URL は空で、後から画面の「MCP 接続」で設定できます。Agent は Run を作った利用者として
  呼び、認証は共通 `.env` の `PLATFORM_SERVICE_TOKEN_SECRET`（stack が生成して全 Compute に同じ値を配る）で署名した短命の token です。
  あわせて `AGENT_EXTERNAL_RAG_PUBLIC_URL=/rag` を書きます。Agent の画面で RAG の図の根拠・根拠の原文を開く短命の URL（#1311・#2389）を、
  同じ origin の `/rag` の下（前段の reverse proxy が `/rag/api/` を RAG の backend へ渡す構成。#19）に付け替えます（#1316）。
- Agent の実行は Control Plane の組み込み Runtime（OpenAI Agents SDK と OCI Enterprise AI の Responses API。#754）。外部の Runtime と Docker は使いません。
  モデルは「システム設定 > モデル」の OCI Enterprise AI の接続と既定のテキストモデルです。
- Runtime 状態（Run・業務 Agent）と画面で変えた定義（Skill・MCP 接続・ツール権限）は Oracle に保存します（`oracle_checkpoint`。接続は共通の `PLATFORM_ORACLE_*`、table は `init_script.sh` の `agent_system_schema --initialize` が作成。#764）。旧 `AGENT_RUNTIME_ORACLE_*` は書きません。gunicorn は 1 worker、dispatcher は `in_process` に固定します。

| 項目 | 値 |
|---|---|
| backend | `production-ready-agent-backend.service`（`127.0.0.1:8020`、1 worker） |
| 設定 | `/u01/aipoc/no.1-production-ready-suite/agent/backend/.env` |
| データ | `/u01/data/production-ready-agent`（model 設定、Binding、Artifact） |
| ログ | `/var/log/agent-init.log`、`journalctl -u production-ready-agent-backend` |

```bash
sudo tail -f /var/log/agent-init.log
sudo journalctl -u production-ready-agent-backend -f
curl -i http://127.0.0.1:8020/api/health
# システムテーブル（認証・権限）の作成・更新（冪等）を手動で再実行する。画面の 運用設定 > システムテーブル でもできる
cd /u01/aipoc/no.1-production-ready-suite/agent/backend && sudo -u opc /usr/local/bin/uv run python -m app.cli.agent_system_schema --initialize
```

既存の Agent の instance（Basic 認証で配備したもの）を更新する手順は [agent/docs/security-rbac.md §8](../agent/docs/security-rbac.md#8-既存環境の更新手順215) を参照してください。

### Compute（3 製品で共通。#1316）

- 3 製品を入れる Compute は 1 台です（bastion の Compute は別。下の「bastion の Compute」）。既定は **4 OCPU / 32 GB / boot volume 200 GB**。RAG の CPU の parser（Docling）が最も多く使うので、
  文書の取込が多いときは OCPU とメモリを上げてください。
- root の filesystem（`/`）は、最初の起動で boot volume いっぱいに広げます（#1984）。Oracle Linux 9 の image の root（LVM の
  `ocivolume-root`）は boot volume を大きくしても image の大きさ（約 30 GB）のままで、3 製品の Python の環境・RAG の parser・
  LibreOffice が入りきらないためです。cloud-init の `bootcmd` が package と配備の前に `/usr/libexec/oci-growfs -y` を実行し
  （無ければ `oci-utils` を入れる）、`suite-init.sh` も最初に同じことをして `/` の空きを log に出します（空きが 30 GB 未満なら警告）。
  この変更の前に作った Compute で `df -h /` が 30 GB のまま満杯なら、`sudo /usr/libexec/oci-growfs -y` で広げてから
  `sudo .../platform/deploy/suite-restart.sh` で配備をやり直してください。
- **reverse proxy と HTTPS（#19）**: 配備は Nginx などの reverse proxy・TLS の証明書（以前の自作の Root CA と IP のサーバー証明書・
  更新の timer・`/platform/ca.crt`）を作らず、OS の firewall（firewalld）の port も開けません。各製品は backend を `127.0.0.1` の
  製品の port（RAG 8000・NL2SQL 8010・Agent 8020）で動かし、frontend は `FRONTEND_BASE_PATH=/<製品>/` で `dist/` を build します。
  利用者に公開するときは、前段に reverse proxy を置くかを利用者が決めます（手順と注意は [README](../README.md#各製品の公開reverse-proxy-は利用者の選択)）。
  stack の `https_enabled`・`http_port`・`https_port` の入力と `ca_certificate_url` の output は、この stack の削除（#12）まで残りますが、配備では使いません。
  `PLATFORM_PUBLIC_BASE_URL`（API キーの画面の「公開の URL」）も配備は書きません。reverse proxy の URL を利用者が `platform/.env` に書きます。
- **subnet の security list または NSG**（stack の外で管理。stack は ingress の規則・NSG を作りません。#259）で、利用者の端末からの
  TCP は、前段に置いた reverse proxy の port だけを許可してください。
  SSH の 22 と ADB への 1522 を含む規則の一覧は直下の [README.md](../README.md#配備の前に-vcn-の-security-list) にあります。
  OS の firewall（firewalld）も、reverse proxy の port を利用者が開けます。製品間の通信（Agent → RAG / NL2SQL の MCP）は同じ Compute の
  127.0.0.1 なので、security list は要りません。
- ログインのセッションは 3 製品で共通です（#1565。下の「ログインのセッションの共有（#1565）」）。`/`（Agent、無ければ最初の製品）を含め、
  どの製品の `/login` でログインしても全製品を使えます。
- Wallet は `/u01/aipoc/wallet`、ログは `/var/log/cloud-init-custom.log`（bootstrap）・`/var/log/suite-init.log`（全体）・`/var/log/<製品>-init.log`。
- apply 後の output に、`ssh_to_instance`、bastion を作ったときは `bastion_vnc_address`・`ssh_to_bastion` が出ます。

#### bastion の Compute（任意。#2670）

- フォームの「Bastion Compute」（`bastion_enabled`、既定 off）をオンにすると、Suite の Compute とは別に bastion の Compute を 1 台作ります
  （`terraform/stack/bastion.tf`）。3 製品は入れません。private subnet の Compute の画面や、プライベート・エンドポイントの ADB に、
  GUI のブラウザ・SQL の道具で届くための踏み台です。
- 既定は **2 OCPU / 16 GB / boot volume 100 GB / 10 VPUs/GB**（`bastion_flex_shape_ocpus` / `bastion_flex_shape_memory` /
  `bastion_boot_volume_size` / `bastion_boot_volume_vpus`）。shape（`bastion_shape`）・image（Oracle Linux 9 の `bastion_image_source_id`）・
  SSH キー（`bastion_ssh_authorized_keys`）は bastion 専用に設定します。主 Compute からの継承はしません。
  shape は独立した既定の E5 Flex、image は大阪の x86 Oracle Linux 9 です。image の候補は大阪・東京・Chicago の順です。
  `bastion_compartment_ocid` / `bastion_availability_domain` / `bastion_vcn_id` / `bastion_subnet_id` で配置先を指定します。
  bastion を有効にした場合は配置先・subnet と SSH 公開キーを入力してください。未指定のままでは plan が失敗します。
  以前の入力で主 Compute の値を継承していた stack も、次の更新前に bastion 専用の値を明示してください。
  公開 IP は subnet から自動で決めます（public subnet なら付ける）。
- 最初の起動で cloud-init（`terraform/stack/cloud_init/bastion.sh`）が、root の filesystem を広げ、GNOME（「Server with GUI」）・TigerVNC
  （display `:1` = TCP `5901`、ユーザー `opc`）・Google Chrome（Google の dnf repository。署名を確かめて入れる）・日本語の font を入れ、
  firewalld で `5901` を開けます。再起動のたびに `ensure-vnc.service` が VNC の起動と firewalld の `5901` を確かめます。
  ログは `/var/log/bastion-init.log`。GNOME の package が多いため、使えるまで 10〜20 分ほどかかります。
- VNC のパスワードはフォームの `bastion_vnc_password`（ちょうど 8 文字。TigerVNC は先頭の 8 文字だけを使うため、それより長い値は受けない）で、instance metadata
  （`bastion_vnc_password_b64`）で渡します。スクリプトには書きません。値は Terraform の state と instance metadata に残ります。
- apply 後の output に `bastion_vnc_address`（`<IP>:5901`）と `ssh_to_bastion` が出ます。VNC のクライアントで `<IP>:5901` に接続します。
  5901 を開けたくないときは、SSH の port forward（`ssh -L 5901:localhost:5901 opc@<bastion の IP>`）で `localhost:5901` に接続します。
- **security list**（stack は規則を作りません。#259）に、次の ingress（ステートフル・TCP）を足してください。直下の
  [README.md](../README.md#配備の前に-vcn-の-security-list) の表にもあります。

  | 足す security list | 送信元 CIDR | 宛先の port（TCP） | 用途 |
  |---|---|---|---|
  | bastion の subnet | 管理者の端末の CIDR（`0.0.0.0/0` にしない。VNC の認証はパスワードだけ） | `22`・`5901` | bastion への SSH と VNC |
  | Compute の subnet | bastion の subnet の CIDR | reverse proxy の port・`22` | bastion の Chrome から 3 製品の画面、bastion から Compute への SSH |
  | ADB のプライベート・エンドポイントの subnet | bastion の subnet の CIDR | `1522` | bastion から ADB への接続 |

  - bastion と Compute（または ADB）が同じ subnet でも、その subnet の security list に「送信元 = その subnet 自身の CIDR」の規則が要ります。
  - ADB が「許可された IP および VCN 限定のセキュア・アクセス」（ACL）なら、security list ではなく ADB の ACL で bastion の subnet（または VCN）を許可します。
  - egress: bastion は dnf と Google Chrome の取得（`dl.google.com`）で外部へ HTTPS で出ます。private subnet なら NAT gateway の経路を用意します。

#### Compute の OS（Oracle Linux 9。#1611）

| 項目 | Oracle Linux 9 の Compute |
|---|---|
| 時間帯 | stack の `timezone`（`UTC`〔既定〕か `Asia/Tokyo`。値は OCI と同じ IANA の地域名。#3255・#3274）。配備した後は `platform/.env` の `PLATFORM_TIMEZONE` に任意の IANA の地域名を書ける（`suite-restart.sh` で OS にも反映）。cloud-init の `timezone` と、`suite-init.sh` の `configure_timezone`（`platform/.env` の `PLATFORM_TIMEZONE` を `timedatectl set-timezone` で。同じなら変えない。再配備でも効く）が合わせる。不正な値は配備の最初で失敗する。時刻の保存は UTC のまま |
| ログイン・実行ユーザー | `opc`（`ssh opc@<IP>`。3 製品のプロセスと repository の所有者。sudo は OCI の image の設定のまま無制限） |
| package | `dnf`。BaseOS / AppStream だけを使い、EPEL・CodeReady Builder は有効にしない。Node.js は module `nodejs:24`（入らなければ公式の tarball を SHASUMS256 で確かめて `/usr/local/lib/nodejs`）。Python は今までどおり uv の `uv python install 3.12` |
| reverse proxy・firewall | 配備しない（#19）。前段に置くときは利用者が入れて port を開ける |
| SELinux | enforcing のまま（無効にしない）。`suite-init.sh` の `configure_selinux` が次を設定する |
| RAG の前処理 / parser の OS の依存 | LibreOffice（`libreoffice-core` / `-writer` / `-impress` / `-calc`、AppStream の 7.1）・`google-noto-sans-cjk-ttc-fonts` / `google-noto-serif-cjk-ttc-fonts`・`poppler-utils`・`tesseract` / `tesseract-langpack-eng` / `tesseract-langpack-jpn` |

SELinux の設定（`semanage` は `policycoreutils-python-utils`）:

- `semanage fcontext -a -t usr_t '/u01/aipoc/no.1-production-ready-suite(/.*)?'` と `restorecon -R`: systemd（`init_t`）は `/u01` の既定の
  `default_t` のファイルを実行も読みもできないため、repository を `usr_t` にする（RAG の unit の `.venv/bin/gunicorn` と `EnvironmentFile`）。
  新しく作るファイル（venv・`node_modules`・`git pull` の分）は親の `usr_t` を受け継ぐ。
- `restorecon -v /usr/local/bin/uv /usr/local/bin/uvx`（各製品の `init_script.sh` の `install_uv`。#1642）: uv の installer は一時の場所から
  移すため `bin_t` にならず、systemd が NL2SQL / Agent の unit の `ExecStart=/usr/local/bin/uv` を実行できない（`status=203/EXEC`）。
  suite-v0.1.3 までの Compute は、`sudo restorecon -v /usr/local/bin/uv /usr/local/bin/uvx` の後、systemd の自動の再起動で起動する。
- reverse proxy を同じ Compute に置くときは、その reverse proxy の SELinux の設定（dist の `httpd_sys_content_t`・`httpd_can_network_connect` など）を利用者が行います（#19 で配備から外した）。
- Ubuntu で配備した Compute は更新しません。Oracle Linux 9 にするには stack を apply して Compute を作り直してください（データの正本は ADB。
  Compute の中だけにあるものは下の「製品ごとの Compute の stack からの更新」と同じく引き継がれません）。

#### 製品ごとの Compute の stack からの更新（#1316）

- 製品ごとの Compute（`oci_core_instance.product` / `oci_core_instance.agent`）は、apply で削除され、1 台の Compute（`oci_core_instance.suite`）が作られます。
  データの正本は ADB なので、DB のデータ（ユーザー・ロール・各製品のテーブル）はそのまま使えます。
- Compute の中だけにあるもの（アップロードの原本などのローカルの保存、画面で変えた `platform/.env` / `model-settings.json` の値、`~/.oci/config`）は
  引き継がれません。必要なら apply の前に退避し、新しい Compute で各製品の「システム設定」から設定し直してください（data migration の仕組みは持ちません）。
- 入力の変更: `application_port`・`<製品>_instance_*`・`rag_app_auth_cookie_secure` / `nl2sql_app_auth_cookie_secure` / `agent_app_auth_cookie_secure` は
  廃止し、`instance_display_name`・`instance_flex_shape_ocpus` / `_memory`・`instance_boot_volume_size`・`https_enabled`・`http_port`・`https_port` を足しました。
- 製品の `init_script.sh` を単独で実行する配備（製品ごとの Nginx の site）はやめました（3 製品はこの suite でセットで配備します）。
- URL が `http://<製品ごとの IP>/` から `https://<IP>/<製品>/` に変わります。ブックマークを更新してください。

### 配備した後の更新と再起動（#1566・#1577）

| スクリプト（`platform/deploy/`） | 実行 | 内容 |
|---|---|---|
| `suite-pull.sh` | opc か `sudo` | repository の所有者（opc）で `origin/main` を取得し、`main` を付け替える（clone は depth 1）。追跡ファイルに変更があれば中止し、未追跡のファイルは保つ。取得だけで、build・再起動はしない |
| `suite-restart.sh` | `sudo` | DB の起動・停止はしない（#10）。既定: 今のコードから build して再配備する（`suite-init.sh` の再実行。依存の同期・共有 UI と各製品の frontend の build・DB の初期化と migration・systemd・再起動）。最後に配備全体を確かめる（下の「配備が正常かの確認」）。`--no-build` は build せずに、有効（enabled）な `production-ready-*` の service だけを再起動する。`--status` は再起動せず、確認だけを 1 回行う |
| `suite-update.sh` | `sudo` | `suite-pull.sh` で取得し、取得した新しい `suite-restart.sh`（既定）を実行する |

- `suite-init.sh` は props（Resource Manager の入力）から、各製品の `backend/.env` を作り直す。
  - そのため `suite-restart.sh` は、build の前に今の `backend/.env` を `/u01/aipoc/props/<製品>.backend.env` へ写す（画面で保存した値を保つ）。
  - 元の props は `/u01/aipoc/recovery/<時刻>/` に退避する。
  - `platform/.env`・Wallet は、`suite-init.sh` がそのまま保つ。
- 画面の「運用設定 › システムテーブル」の「作成・更新」で、保存済みのデータを書き換える migration（Agent の
  業務 Agent のスキルの ID の改名など）を適用したとき、Agent の backend は同じプロセスのメモリを保存先から読み直す
  （再起動は要らない。#1844）。結果に「再起動が必要なプロセスがあります」と出たら、挙げたプロセス（別に動かす
  dispatcher・読み直せなかった backend）を再起動する（backend は `suite-restart.sh --no-build`）。CLI で適用したときは、
  動いている backend を再起動する（`suite-restart.sh` は再起動まで行う）。
- **配備が途中で失敗したときのやり直し**（#1994）: 先に `sudo tail -n 50 /var/log/cloud-init-custom.log` と `/var/log/suite-init.log` で止まった所を確かめ、
  原因（`/` の空き〔`df -h /`。#1984〕・ネットワーク・ADB など）を直してから、repo（`/u01/aipoc/no.1-production-ready-suite`）があれば
  `sudo .../platform/deploy/suite-update.sh`（最新の main を取得して再配備）か `suite-restart.sh`（今のコードで再配備）を実行する。
  repo の clone の前に止まったときだけ `sudo bash /u01/aipoc/bootstrap-suite.sh` を実行する（repo があれば所有者〔opc〕で更新してから
  `suite-init.sh` を実行する）。stack の再 apply はしない（cloud-init の user_data が変わると Compute が作り直される）。
- 2 つのスクリプトは lock を共有し、同時の実行を防ぐ。log は `/var/log/suite-ops.log`（再配備の詳細は `/var/log/suite-init.log`）。
- cloud-init は `git` と `tmux` を入れる。
- 製品の `init_script.sh` は backend の `/api/health` を 3 秒ごとに最大 300 秒待ち（`SUITE_BACKEND_HEALTH_TIMEOUT_SECONDS`）、15 秒ごとに経過・状態・
  再起動の回数を出す。service が `failed` になるか再起動を 3 回繰り返したら、すぐに status と journal を出して失敗にする（#1642）。
- OS の再起動では、enabled の unit（`WantedBy=multi-user.target`）が自動で起動する。
  - enabled の unit は、各製品の backend・worker と、起動にした RAG の前処理 / parser。
  - DB は Compute の外にあるため、OS の再起動では起動しない。配備のスクリプト（`suite-restart.sh`・`suite-update.sh`）と
    システム設定の画面は DB の起動・停止を扱わない（#10）。止まっていれば OCI のコンソール（ADB Free コンテナなら
    `docker start <コンテナ名>`）から起動する。

### 配備が正常かの確認（#1642）

`suite-init.sh` の最後（`suite-restart.sh` の最後と `--status` も同じ。`platform/deploy/suite-health.sh` の `suite_check_deployment`）に、次を確かめる。

- 有効な `production-ready-*` の service がすべて `active` で、再起動を繰り返していないか
- 配備した製品の backend の `/api/health`（`127.0.0.1:<port>`）。reverse proxy は配備しないので確かめない（#19）
- 各製品の system schema が ready か（#1648。製品の schema の CLI の `check`。DDL は実行しない）。`init_script.sh` は表の作成・更新に
  失敗しても backend を起動する（画面から直せる）ため、health だけでは OK に見える。NG なら CLI の要約（状態・不足の object・
  前回の失敗の ORA コード）、製品の初期化の log（`/var/log/<製品>-init.log`）の最後の ORA コード、権限の不足の手がかりを出す

結果は製品ごとに 1 行で出す。1 つでも NG なら、NG の service の journal の末尾を出し、0 以外で終わる。

```
[suite-init] check: rag backend OK / services 4/4 active
[suite-init] check: nl2sql backend OK / services 5/5 active
[suite-init] check: agent backend OK / services 1/1 active
[suite-init] check: rag schema OK（status=ready）
[suite-init] check: nl2sql schema OK（status=ready）
[suite-init] check: agent schema OK（status=ready）
[suite-init] check: OK（すべての service が active で、各製品の backend の health が応答し、system schema が ready です）。
```

schema の CLI（backend の directory で `sudo -u opc .venv/bin/python <下の引数>`。ready なら 0）:
RAG `-m app.rag.system_schema_cli check`・NL2SQL `-m app.cli.nl2sql_system_schema --check`（`app_security_migrate` の表も見る）・
Agent `-m app.cli.agent_system_schema --check`。

- 配備の直後は `sudo tail -n 20 /var/log/suite-init.log` の最後の `check:` の行を見る。
- 後から確かめるときは `sudo /u01/aipoc/no.1-production-ready-suite/platform/deploy/suite-restart.sh --status`（再起動しない）。

### ログインの試行の回数の共有（#1173）

ログインの試行の回数の制限（#1087。`PLATFORM_AUTH_LOGIN_*`）は、失敗の記録を共通のテーブル `PLATFORM_LOGIN_ATTEMPTS` に置き、
3 製品・gunicorn の全 worker・再起動をまたいで同じ回数を数えます（構成管理者 `system_admin` のパスワードを、製品・worker の数だけ
多く試せないようにする）。

- テーブル: `KEY_HASH`（`VARCHAR2(64)`）・`ATTEMPT_ID`（`VARCHAR2(36)`）・`ATTEMPTED_AT`（`TIMESTAMP`、DB の UTC）。主キー `PK_PLATFORM_LOGIN_ATTEMPTS`
  （`KEY_HASH, ATTEMPT_ID`）と index `IX_PLATFORM_LOGIN_ATTEMPTS_AT`（`ATTEMPTED_AT`）。ログイン ID と送信元 IP は保存せず、共通 `.env` の
  `PLATFORM_SERVICE_TOKEN_SECRET` から導いた鍵の HMAC-SHA256 だけを保存します（テーブルを読めても利用者の ID・IP は分からない）。
  `PLATFORM_SERVICE_TOKEN_SECRET` が空（32 文字未満）のときは DB に記録せず、従来どおりプロセス内だけで数えます（起動時にログ
  `auth_login_throttle_shared_store_disabled`）。
- 1 回の失敗で 2 行（ログイン ID＋送信元 IP・送信元 IP）を足します。窓（`PLATFORM_AUTH_LOGIN_ATTEMPT_WINDOW_MINUTES`）を過ぎた行は、
  失敗を記録するついでに 1 回 1,000 行まで消すので、テーブルには窓の中の失敗の分しか残りません。成功した試行・429 で拒否した試行は残しません。
- DB が使えない（接続できない・テーブルが無い）ときは、プロセス内の記録に自動で切り替えて制限を続け、ログ
  `auth_login_throttle_shared_store_unavailable`（`error_code`・`hint` 付き）を出します。30 秒ごとに DB を試し直し、戻れば DB で数えます
  （停止中にプロセス内で数えた失敗も、窓の中なら合わせて数える）。ログインは止まりません（構成管理者は DB の停止中もログインできる）。
- テーブルは各製品のシステムテーブルの初期化（RAG `app.rag.system_schema_cli initialize`・NL2SQL `app.cli.app_security_migrate --apply`・
  Agent `app.cli.agent_system_schema --initialize`。どれも `init_script.sh` が実行する）が、`PLATFORM_USERS` などと一緒に冪等に作ります。
- reverse proxy: 配備は reverse proxy を作らないので、ログインの API（`/api/auth/login`）の送信元 IP ごとの上限は backend だけが持ちます（#19）。
  前段に reverse proxy を置くときは、backend が利用者の IP を見られるように（`X-Forwarded-For` と uvicorn の proxy headers など）してください
  （見られないと、全員が 1 つの送信元として数えられる）。必要なら reverse proxy の側でも `/api/auth/login` に緩い上限を掛けます。

既存環境の更新（3 製品は同じ schema を共有するので、テーブルの作成はどれか 1 つの製品で行えばよい）:

1. 各 Compute のコードを更新し、backend を再起動します。テーブルを作るまでは、プロセス内で数えてログに上の警告が出ます。
2. テーブルを作ります（冪等。既存の `PLATFORM_*` は変えません）。

   ```bash
   # RAG
   cd /u01/aipoc/no.1-production-ready-suite/rag/backend
   sudo -u opc .venv/bin/python -m app.rag.system_schema_cli initialize
   # NL2SQL
   cd /u01/aipoc/no.1-production-ready-suite/nl2sql/backend
   sudo -u opc /usr/local/bin/uv run python -m app.cli.app_security_migrate --apply --skip-bootstrap
   # Agent
   cd /u01/aipoc/no.1-production-ready-suite/agent/backend
   sudo -u opc /usr/local/bin/uv run python -m app.cli.agent_system_schema --initialize
   ```

3. 確認: backend のログに `auth_login_throttle_shared_store_unavailable` が出ないこと。存在しないログイン ID で 1 回ログインに失敗すると、
   `SELECT COUNT(*) FROM PLATFORM_LOGIN_ATTEMPTS` が 2 増えること（窓を過ぎると、次の失敗のときに消える）。

### ログインのセッションの共有（#1565）

1 台の Compute の 3 製品（`/rag/`・`/nl2sql/`・`/agent/`）は同じ origin なので、ログインの Cookie を共有し、1 回のログインで全製品を使えます。

- Cookie: セッション `pr_session`（HttpOnly）と CSRF `pr_csrf`（`Path=/`・`SameSite=Lax`、HTTPS なら `Secure`）。名前は共通 `.env` の
  `PLATFORM_AUTH_SESSION_COOKIE_NAME` / `PLATFORM_AUTH_CSRF_COOKIE_NAME`（変えるなら frontend の `CSRF_COOKIE_NAME`
  （`@production-ready/system-settings`）も同じにする）。製品ごとの `RAG_` / `NL2SQL_` / `AGENT_APP_AUTH_*_COOKIE_NAME` は読みません。
- セッションは共通のテーブル `PLATFORM_AUTH_SESSIONS` にあり、どの製品の backend でも検証できます。構成管理者 `system_admin` の token
  （DB の行を持たない署名付きの token）も、接頭辞 `platform-system-admin-v1` と署名の鍵（共通の `PLATFORM_ADMIN_LOGIN_*` から導く）が 3 製品で同じです。
- ログインし直すと、ブラウザが持っていた古いセッションは失効します。ログアウトは共有のセッションを失効させ、開いているほかの製品・タブは
  `BroadcastChannel` で知らせを受けてログイン画面へ移ります（受けなくても次の API の 401 で移る）。
- 権限はこれまでどおり製品ごとです（ロールに権限の無い製品は 403 の画面）。

既存環境の更新:

1. コードを更新します。`platform/.env` に次を足します（無くても既定値は同じ）。製品の `backend/.env` の
   `RAG_APP_AUTH_SESSION_COOKIE_NAME` / `RAG_APP_AUTH_CSRF_COOKIE_NAME`・`NL2SQL_APP_AUTH_*_COOKIE_NAME`・`AGENT_APP_AUTH_*_COOKIE_NAME` は
   もう読まないので消します。

   ```bash
   PLATFORM_AUTH_SESSION_COOKIE_NAME=pr_session
   PLATFORM_AUTH_CSRF_COOKIE_NAME=pr_csrf
   ```

2. 3 製品の frontend を build し直し（`@production-ready/system-settings` の CSRF の Cookie 名が変わる）、backend を再起動します
   （「配備した後の更新と再起動（#1566）」の `suite-update.sh` で行える。`backend/.env` に残った旧名の行は読まれないだけで害はない）。
3. 利用者は一度ログインし直します。古い Cookie（`rag_session` など）は読まれず、期限（`PLATFORM_AUTH_ABSOLUTE_TIMEOUT_HOURS`）で
   ブラウザから消えます。古い Cookie のセッションの行は、期限が過ぎると使えなくなります。構成管理者の古い token（`rag-system-admin-v1` など）も使えません。

## パッケージと検証

monorepo root から実行します。

```bash
python terraform/scripts/package_stack.py
python terraform/scripts/verify_stack_contract.py terraform/dist/production-ready-suite-terraform-stack.zip
```

出力は `terraform/dist/production-ready-suite-terraform-stack.zip` です。この zip を Resource Manager へ upload して stack を作成します。
`verify_stack_contract.py` は、フォーム（`schema.yaml`）と Terraform 変数の一致、製品選択と 1 台の Compute の入力の契約、製品ごとの `backend/.env` の key が各製品の Settings にあること、
backend の port（RAG 8000・NL2SQL 8010・Agent 8020）が init_script.sh・`platform/deploy/suite-products.sh`・MCP の URL で一致し衝突しないこと、
RAG の前処理 / parser（`rag/scripts/rag-systemd.sh` の unit の定義と uv.lock があること）、各製品の `init_script.sh` の配備契約
（Docker を入れないこと、RAG の sudoers・状態の保持を含む）と、Dockerfile・compose が無いこと（#356 / #754）を検証します。

CI（`.github/workflows/ci.yml` の `Suite / Deploy`。中身は `.github/workflows/suite-deploy.yml` で、release の workflow も再利用する。#1592）は、
`terraform fmt` / `terraform validate`（Terraform 1.5.7）と上の2つに加え、
`platform/deploy/tests/`（suite-init の振る舞い・配備全体の確認）と、各製品の `init_script.sh` と配備・更新のスクリプトのテスト（`<製品>/scripts/tests/`）を 1 つの job で実行します。
製品の frontend・共有 UI・Markdown だけの変更では動きません。実テナンシーへの配備確認は手動で行います。

## release

`.github/workflows/terraform-release.yml` が `suite-v*` の tag（例: `suite-v0.1.0`）から release を作り、
`production-ready-suite-terraform-stack.zip` と `.sha256` を公開し、その release を GitHub の Latest にします。次のボタンは最新の release を配備します。

[![Deploy to Oracle Cloud](https://oci-resourcemanager-plugin.plugins.oci.oraclecloud.com/latest/deploy-to-oracle-cloud.svg)](https://cloud.oracle.com/resourcemanager/stacks/create?region=ap-osaka-1&zipUrl=https://github.com/engchina/no.1-production-ready-suite/releases/latest/download/production-ready-suite-terraform-stack.zip)

root の [README.md](../README.md)「OCI へのデプロイ」のボタンも `releases/latest` を指すので、release のたびに書き換えません。版ごとの変更は release のノートに書きます。

製品ごとの release（`rag-v*` / `nl2sql-v*` / `agent-v*`）は作りません。既に公開済みの release（例: `nl2sql-v0.1.32`）はそのまま残ります。

## 製品ごとの stack からの移行

既存の製品ごとの stack で作った環境は、そのまま動き続けます。統合 stack へ移る場合は、新しい stack を作って「既存の Autonomous AI Database を選択」で
今の ADB を指定し、配備する製品を選んで apply します（Compute は新しく作られます）。新しい Compute で動作を確認してから、古い stack の Compute を destroy してください。
古い stack が ADB を作っていた場合、その stack を destroy すると ADB も削除されます。

入力の名前は、製品固有のものに製品名の接頭辞を付けました。

| 旧（製品ごとの stack） | 新（統合 stack） |
|---|---|
| RAG の `app_login_user` / `app_login_password` / `app_auth_cookie_secure` | 廃止（ログインは構成管理者 `app_admin_login_user_password` と DB ユーザー。#214） / 廃止 / `rag_app_auth_cookie_secure` |
| RAG の `enable_parser_docling` / `enable_parser_marker` / `enable_oci_cloud_parsers` | 廃止（Docling は常に配備。#286） / 廃止（Marker は削除。#270） / `rag_enable_oci_cloud_parsers` |
| NL2SQL の `app_admin_login_user_password` / `oracle_deepsec_enabled` / `oracle_deepsec_data_user_password` | `app_admin_login_user_password`（RAG と共通） / `nl2sql_oracle_deepsec_enabled` / `nl2sql_oracle_deepsec_data_user_password` |
| NL2SQL の `app_environment` / `app_auth_cookie_secure` / `app_admin_login_user_id` | `platform_environment` / `https_enabled` / `app_admin_login_user_id`（RAG と共通） |
| Agent の `app_basic_auth_user` / `app_basic_auth_password` | 廃止（ログインは構成管理者 `app_admin_login_user_password` と DB ユーザー。#215） |
| 統合 stack の `agent_app_basic_auth_user` / `agent_app_basic_auth_password`（output の `agent_basic_auth_user`） | 廃止（`app_admin_login_user_password` を Agent にも使う。#215）。Cookie を HTTPS 限定にする `agent_app_auth_cookie_secure` を新設 |
| `instance_display_name` / `instance_flex_shape_ocpus` / `instance_flex_shape_memory` / `instance_boot_volume_size` | `<製品>_instance_display_name` / `<製品>_instance_flex_shape_ocpus` / `<製品>_instance_flex_shape_memory` / `<製品>_instance_boot_volume_size` |
| `adb_name` の既定 `RAGADB` / `NL2SQLADB` / `AGENTADB` | `SUITEADB` |
| NL2SQL の `adb_workload` の既定 `LH` | `LH`（#1558。統合の当初は `OLTP` だった） |

## Always Free ADB を手動作成して利用する場合

この stack は Always Free の Compute / ADB を作成しません。
Compute の新規作成は x86 の E4 / E5 Flex、新規 ADB は有料構成です。

Always Free ADB が必要な場合は、OCI コンソールで事前に手動作成し、フォームで
「既存の Autonomous AI Database を選択」を指定してください。

1. tenancy のホーム・リージョンで Always Free ADB を作成します。Suite の SQL*Net 接続が使える
   workload と、必要な製品機能に対応する DB バージョンを選びます。
   APEX Service は SQL*Net 接続を提供しません。Vector Search を利用する場合は対応する DB バージョンが必要です。
2. Always Free ADB は private endpoint 非対応です。パブリック・エンドポイントの ACL、
   Compute からの接続経路（Service Gateway または固定出口 IP）、mTLS 接続の条件を手動で設定します。
   stack の新規 ADB 用ネットワーク入力は、既存 ADB の設定を変更しません。
3. `adb_compartment_ocid` にその ADB のコンパートメント、`adb_deployment_mode` に `USE_EXISTING`、
   `existing_adb_ocid` に手動作成した ADB の OCID を指定します。
4. 既定のアプリ用 DB ユーザーを使う場合は、`existing_adb_admin_password` と
   `adb_app_user_name` / `adb_app_user_password` を指定します。使わない場合は、必要な権限を持つ
   `existing_oracle_user` / `existing_oracle_password` を指定します。
   `existing_oracle_dsn` は空欄なら選択した ADB の DB 名から `<db_name>_high` を使います。

stack は既存 ADB の Wallet を生成して接続先へ渡します。既存 ADB の無料 / 有料の種別、
CPU・容量・ネットワークは変更しません。無料枠の残量と制約、実際の接続・初期化・同時接続数・性能は
利用する環境で確認してください。Compute や AI の利用料が無料になる設定ではありません。

公式資料:

- [Always Free Autonomous AI Database の制約](https://docs.oracle.com/en-us/iaas/autonomous-database-serverless/doc/autonomous-always-free.html)
- [Autonomous AI Database の作成](https://docs.oracle.com/en/cloud/paas/autonomous-database/serverless/adbsb/autonomous-provision.html)
- [APEX Service の接続制約](https://docs.oracle.com/en/cloud/paas/apex/gsadd/restrictions-and-limitations.html)
