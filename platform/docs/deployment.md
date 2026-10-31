# 1 台の Compute の配備（platform/deploy）

1 台の Compute（Oracle Linux 9、ユーザー `opc`）に 3 製品（RAG / NL2SQL / Agent Control Plane）をまとめて入れる配備の、
DB・更新・確認の仕組みです。中身は [`platform/deploy/`](../deploy/) のスクリプトで、
`platform/deploy/suite-init.sh` が各製品の `init_script.sh` を順に呼びます。reverse proxy（Nginx など）と TLS の証明書は配備しません（#19。前段に置くかは利用者の選択。[README](../../README.md#各製品の公開reverse-proxy-は利用者の選択)）。

> OCI Resource Manager の Terraform stack（以前の `terraform/`）による配備は #12 で削除しました。この文書は、その README のうち
> `platform/deploy/` と 3 製品に共通する部分を移したものです。配備の入力（`/u01/aipoc/props/` の `platform.env`・
> `<製品>.backend.env`・`products.txt`・`adb_admin.env` など）は `platform/deploy/suite-init.sh` の冒頭のコメントにあります。

## Autonomous AI Database（全製品で共有）

- **対応バージョン**: 3製品とも Oracle AI Database 26ai 以降が前提です（`VECTOR` 型と Oracle AI Vector Search を使うため）。ADB は 26ai で作ってください。
  NL2SQL の SQL ドメインと annotation は旧 Oracle Database 23ai で入った機能で、それより前のバージョンでは NL2SQL がその機能を使いません。
  リポジトリの文書・画面・コメントでは製品名にバージョンを入れず（#564）、対応バージョンはここだけに書きます。
- **3 製品は既定で、アプリ用の DB ユーザー（ADMIN ではないユーザー。既定の名前は `ADB_APP_USER`）で接続し、システムの表もそのユーザーの schema に作ります**
  （下の「[アプリ用の DB ユーザー](#アプリ用の-db-ユーザー16681675)」。#1668）。
  製品のテーブルは `RAG_` / `NL2SQL_` / `AGENT_`、3製品で共有するユーザー・ロール・セッションのテーブルは `PLATFORM_` の接頭辞で分かれているため、同じスキーマでも衝突しません（#212）。
- DB の password / user / DSN に single quote は使えません（共通 `.env` では値を single quote で囲むため）。
- **時間帯（#3255・#3274）**: 時刻は UTC で保存します（3 製品の DB の session の時間帯は、Compute の OS の時間帯に関わらず
  `'+00:00'` に固定します。`pr_backend_core.oracle_session`）。3 製品の共通の設定 `PLATFORM_TIMEZONE`（`platform/.env`。`UTC`〔既定。OCI の Database の Time Zone の既定〕か `Asia/Tokyo`〔日本時間: 東京・大阪など〕などの IANA の地域名）は
  Compute の OS と、今日〔検索・期間の基準日〕・既定の時間帯・画面の日時の表示・NL2SQL の業務データの SQL の今の日時（#3265）に使い、ADB の時間帯（`DBTIMEZONE`）は変えません。
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
- ADB の DDL は配備のスクリプトに持ちません。各製品の `init_script.sh` がアプリの CLI（冪等）で作成・更新します。
  作成・更新に失敗しても backend は起動します（画面の システムテーブル から直せる）が、配備の最後の確認が `check: <製品> schema NG` で失敗します（[配備が正常かの確認](#配備が正常かの確認1642)）。

### アプリ用の DB ユーザー（#1668・#1675）

3 製品が ADB に接続するユーザーです。**既定（推奨）では、ADMIN ではないアプリ用のユーザー（既定の名前 `ADB_APP_USER`）を作って使います。**
ADMIN を常用しないためです（アプリの不具合・NL2SQL の管理 SQL の誤りを DB 全体の管理者の権限で実行させない。ADMIN のパスワードを `opc` が読む `.env` に置かない）。
配備の props に `adb_admin.env`（ADMIN のパスワードとアプリ用のユーザーの名前）があるときに作ります。無ければ、`platform/.env` の `PLATFORM_ORACLE_USER` でそのまま接続します（下の「[アプリ用の DB ユーザーを使わないとき](#アプリ用の-db-ユーザーを使わないとき自分で用意したユーザー1648)」）。

#### 作り方（Compute の上で行う）

- 配備の props の `/u01/aipoc/props/adb_admin.env`（root の 0600。ADMIN のパスワードとユーザー名）があれば、`platform/deploy/suite-init.sh` が
  製品の配備（各製品の system schema の初期化）の前に、**Compute から** ADMIN で ADB に接続して `platform/deploy/suite-db-user.py` を実行します
  （Wallet・mTLS は製品と同じ。`uv run --no-project --with oracledb==<版>`）。
- ADB はプライベート・エンドポイント・ACL で Compute からしか接続できないため、Compute の外からは DB に接続しません。
- **冪等**: ユーザーが無ければ `CREATE USER`、あれば profile・quota を合わせます。アプリのパスワードで接続できなければ（`ORA-01017` など）
  パスワードを合わせて lock を外します。パスワードは `platform/.env`（画面の「システム設定 > データベース」で保存した値）を優先し、無ければ配備の props の値です。
  grant は毎回付け直し、最後にアプリ用のユーザーで接続して権限が効いていることを確かめます。
- **失敗したとき**: ORA の番号と手がかりを `/var/log/suite-init.log` に出し（`[suite-db-user] NG（ORA-01017）: …`・`[suite-init] check: db user NG`）、
  warning を出して製品の配備と起動を続け、最後に失敗（0 以外）を返します（ADB に接続できなくてもアプリケーションは起動する。#1756）。
  ユーザーが無い間は 3 製品とも DB に接続できません。直したら `sudo .../platform/deploy/suite-restart.sh` でやり直します。
- **ADMIN のパスワードの置き場所**: `platform/.env`・各製品の `backend/.env`・Wallet のパスワード（`PLATFORM_ORACLE_WALLET_PASSWORD` はアプリ用のユーザーのパスワード）には書きません。
  `/u01/aipoc/props/adb_admin.env`（root の 0600）にだけ置き、配備の再実行（`suite-restart.sh` / `suite-update.sh`）のたびに、新しい版が要る権限を付け直します
  （使った後に消すと、再配備で権限を足せなくなるため残します）。ほかの secret と同じく機密として扱ってください。不要になったら `sudo rm /u01/aipoc/props/adb_admin.env` で消せます（その後の再配備はユーザーの準備を飛ばします）。

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

配備の props に `adb_admin.env` を置かず、`platform/.env` の `PLATFORM_ORACLE_USER` に ADMIN 以外の自分で用意したユーザーを指定する場合です。

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
    `DBA_DATA_GRANTS`・`DBA_TAB_PRIVS`・`DBA_POLICIES`。`SELECT_CATALOG_ROLE`）の参照。詳しくは [nl2sql/docs/security-rbac-deepsec.md](../../nl2sql/docs/security-rbac-deepsec.md)「DeepSec V001 の前提」。

## Compute（3 製品で共通。#1316）

- 3 製品を入れる Compute は 1 台です。目安は **4 OCPU / 32 GB / boot volume 200 GB**。RAG の CPU の parser（Docling）が最も多く使うので、
  文書の取込が多いときは OCPU とメモリを上げてください。
- root の filesystem（`/`）は、配備で boot volume いっぱいに広げます（#1984）。Oracle Linux 9 の image の root（LVM の
  `ocivolume-root`）は boot volume を大きくしても image の大きさ（約 30 GB）のままで、3 製品の Python の環境・RAG の parser・
  LibreOffice が入りきらないためです。`suite-init.sh` が最初に `/usr/libexec/oci-growfs -y` を実行して `/` の空きを log に出します（空きが 30 GB 未満なら警告）。
  この変更の前に作った Compute で `df -h /` が 30 GB のまま満杯なら、`sudo /usr/libexec/oci-growfs -y` で広げてから
  `sudo .../platform/deploy/suite-restart.sh` で配備をやり直してください。
- **reverse proxy と HTTPS（#19）**: 配備は Nginx などの reverse proxy・TLS の証明書（以前の自作の Root CA と IP のサーバー証明書・
  更新の timer・`/platform/ca.crt`）を作らず、OS の firewall（firewalld）の port も開けません。各製品は backend を `127.0.0.1` の
  製品の port（RAG 8000・NL2SQL 8010・Agent 8020）で動かし、frontend は `FRONTEND_BASE_PATH=/<製品>/` で `dist/` を build します。
  利用者に公開するときは、前段に reverse proxy を置くかを利用者が決めます（手順と注意は [README](../../README.md#各製品の公開reverse-proxy-は利用者の選択)）。
  `PLATFORM_PUBLIC_BASE_URL`（API キーの画面の「公開の URL」）も配備は書きません。reverse proxy の URL を利用者が `platform/.env` に書きます。
- **subnet の security list または NSG**で、利用者の端末からの TCP は、前段に置いた reverse proxy の port だけを許可してください。
  OS の firewall（firewalld）も、reverse proxy の port を利用者が開けます。製品間の通信（Agent → RAG / NL2SQL の MCP）は同じ Compute の
  127.0.0.1 なので、security list は要りません。
- ログインのセッションは 3 製品で共通です（#1565。下の「ログインのセッションの共有（#1565）」）。`/`（Agent、無ければ最初の製品）を含め、
  どの製品の `/login` でログインしても全製品を使えます。
- Wallet は `/u01/aipoc/wallet`、ログは `/var/log/suite-init.log`（全体）・`/var/log/<製品>-init.log`。

### Compute の OS（Oracle Linux 9。#1611）

| 項目 | Oracle Linux 9 の Compute |
|---|---|
| 時間帯 | `platform/.env` の `PLATFORM_TIMEZONE`（既定 `UTC`。任意の IANA の地域名。#3255・#3274。`suite-restart.sh` で OS にも反映）。`suite-init.sh` の `configure_timezone`（`platform/.env` の `PLATFORM_TIMEZONE` を `timedatectl set-timezone` で。同じなら変えない。再配備でも効く）で合わせる。不正な値は配備の最初で失敗する。時刻の保存は UTC のまま |
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
- Ubuntu で配備した Compute は更新しません。Oracle Linux 9 の Compute を作り直してください（データの正本は ADB。
  アップロードの原本・画面で変えた `platform/.env` など、Compute の中だけにあるものは引き継がれません）。

## 配備した後の更新と再起動（#1566・#1577）

| スクリプト（`platform/deploy/`） | 実行 | 内容 |
|---|---|---|
| `suite-pull.sh` | opc か `sudo` | repository の所有者（opc）で `origin/main` を取得し、`main` を付け替える（clone は depth 1）。追跡ファイルに変更があれば中止し、未追跡のファイルは保つ。取得だけで、build・再起動はしない |
| `suite-restart.sh` | `sudo` | DB の起動・停止はしない（#10）。既定: 今のコードから build して再配備する（`suite-init.sh` の再実行。依存の同期・共有 UI と各製品の frontend の build・DB の初期化と migration・systemd・再起動）。最後に配備全体を確かめる（下の「配備が正常かの確認」）。`--no-build` は build せずに、有効（enabled）な `production-ready-*` の service だけを再起動する。`--status` は再起動せず、確認だけを 1 回行う |
| `suite-update.sh` | `sudo` | `suite-pull.sh` で取得し、取得した新しい `suite-restart.sh`（既定）を実行する |

- `suite-init.sh` は props（`/u01/aipoc/props/`）から、各製品の `backend/.env` を作り直す。
  - そのため `suite-restart.sh` は、build の前に今の `backend/.env` を `/u01/aipoc/props/<製品>.backend.env` へ写す（画面で保存した値を保つ）。
  - 元の props は `/u01/aipoc/recovery/<時刻>/` に退避する。
  - `platform/.env`・Wallet は、`suite-init.sh` がそのまま保つ。
- 画面の「運用設定 › システムテーブル」の「作成・更新」で、保存済みのデータを書き換える migration（Agent の
  業務 Agent のスキルの ID の改名など）を適用したとき、Agent の backend は同じプロセスのメモリを保存先から読み直す
  （再起動は要らない。#1844）。結果に「再起動が必要なプロセスがあります」と出たら、挙げたプロセス（別に動かす
  dispatcher・読み直せなかった backend）を再起動する（backend は `suite-restart.sh --no-build`）。CLI で適用したときは、
  動いている backend を再起動する（`suite-restart.sh` は再起動まで行う）。
- **配備が途中で失敗したときのやり直し**（#1994）: 先に `sudo tail -n 50 /var/log/suite-init.log` で止まった所を確かめ、
  原因（`/` の空き〔`df -h /`。#1984〕・ネットワーク・ADB など）を直してから、repo（`/u01/aipoc/no.1-production-ready-suite`）があれば
  `sudo .../platform/deploy/suite-update.sh`（最新の main を取得して再配備）か `suite-restart.sh`（今のコードで再配備）を実行する。
- 2 つのスクリプトは lock を共有し、同時の実行を防ぐ。log は `/var/log/suite-ops.log`（再配備の詳細は `/var/log/suite-init.log`）。
- 製品の `init_script.sh` は backend の `/api/health` を 3 秒ごとに最大 300 秒待ち（`SUITE_BACKEND_HEALTH_TIMEOUT_SECONDS`）、15 秒ごとに経過・状態・
  再起動の回数を出す。service が `failed` になるか再起動を 3 回繰り返したら、すぐに status と journal を出して失敗にする（#1642）。
- OS の再起動では、enabled の unit（`WantedBy=multi-user.target`）が自動で起動する。
  - enabled の unit は、各製品の backend・worker と、起動にした RAG の前処理 / parser。
  - DB は Compute の外にあるため、OS の再起動では起動しない。配備のスクリプト（`suite-restart.sh`・`suite-update.sh`）と
    システム設定の画面は DB の起動・停止を扱わない（#10）。止まっていれば OCI のコンソール（ADB Free コンテナなら
    `docker start <コンテナ名>`）から起動する。

## 配備が正常かの確認（#1642）

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

## ログインの試行の回数の共有（#1173）

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

## ログインのセッションの共有（#1565）

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
