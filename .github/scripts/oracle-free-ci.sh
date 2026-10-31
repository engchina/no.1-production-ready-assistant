#!/usr/bin/env bash
# Integration Nightly（integration-nightly.yml）の Oracle AI Database Free の container の準備（#1595）。
#   1. container の log に "DATABASE IS READY TO USE" が出るまで待つ（container が止まったら失敗する）。
#   1b. VECTOR_MEMORY_SIZE を設定して instance を再起動する（ベクトル索引に要る）。
#   2. PDB（FREEPDB1）にテストの利用者 APP を作る（NL2SQL の tests/conftest.py が PLATFORM_ORACLE_USER=APP に
#      固定するので、3 製品とも APP を使う）。
#   3. 接続の設定（PLATFORM_ORACLE_*。Wallet なし）を platform/.env に書く。環境変数（GITHUB_ENV）には書かず、
#      migration の CLI はこのファイルを読む。3 製品のテストは platform/.env を読まない（#1972）ので、
#      実 Oracle のテストの step だけで環境変数に読み込む（oracle-integration.yml）。
#
# 使い方: ORACLE_CI_PASSWORD=<password> bash .github/scripts/oracle-free-ci.sh
#   ORACLE_CONTAINER（既定 oracle）・ORACLE_PORT（既定 1521）・ORACLE_ENV_FILE（既定 platform/.env）・
#   ORACLE_READY_TIMEOUT_SECONDS（既定 900）で変えられる（手元では podman でもよい: DOCKER=podman）。
set -euo pipefail

: "${ORACLE_CI_PASSWORD:?ORACLE_CI_PASSWORD が必要です}"
container="${ORACLE_CONTAINER:-oracle}"
port="${ORACLE_PORT:-1521}"
env_file="${ORACLE_ENV_FILE:-platform/.env}"
timeout_seconds="${ORACLE_READY_TIMEOUT_SECONDS:-900}"
docker_cmd="${DOCKER:-docker}"

deadline=$((SECONDS + timeout_seconds))
# grep -q は一致した時点で終わり、log を書き出し中の docker logs が SIGPIPE で失敗して、pipefail で
# 一致しなかった扱いになる（log が長いと起きる）。grep には最後まで読ませる。
until "${docker_cmd}" logs "${container}" 2>&1 | grep "DATABASE IS READY TO USE" > /dev/null; do
  if [ "$("${docker_cmd}" inspect -f '{{.State.Running}}' "${container}" 2>/dev/null)" != "true" ]; then
    echo "::error::Oracle の container（${container}）が止まりました。"
    "${docker_cmd}" logs "${container}" 2>&1 | tail -n 50
    exit 1
  fi
  if [ "${SECONDS}" -ge "${deadline}" ]; then
    echo "::error::Oracle の起動が ${timeout_seconds} 秒で終わりませんでした。"
    "${docker_cmd}" logs "${container}" 2>&1 | tail -n 50
    exit 1
  fi
  sleep 5
done
echo "Oracle の起動を確認しました（${SECONDS} 秒）。"

# Free の image は VECTOR_MEMORY_SIZE が 0 で、HNSW のベクトル索引を作る migration が ORA-51962 で失敗する。
# 動的には増やせない（ORA-51950）ので SPFILE に書いて instance を再起動する。
"${docker_cmd}" exec -i "${container}" sqlplus -s / as sysdba <<'SQL'
WHENEVER SQLERROR EXIT FAILURE
SET FEEDBACK OFF
ALTER SYSTEM SET vector_memory_size = 512M SCOPE = SPFILE;
SHUTDOWN IMMEDIATE
STARTUP
ALTER PLUGGABLE DATABASE ALL OPEN;
EXIT
SQL
echo "VECTOR_MEMORY_SIZE を 512M にして再起動しました。"

# password は標準入力で渡す（process の引数に出さない）。sqlplus は失敗した文を置換の後の値で表示するので、
# CREATE USER は PL/SQL の中で実行し、失敗の文言を差し替えて password を log に出さない（文言は英語。
# container の NLS では日本語が化ける）。
# lite の image の FREEPDB1 には USERS の tablespace が無く、既定の SYSTEM は ASSM ではない（SecureFile の LOB を
# 作れない）ので、USERS を作る。DB_DEVELOPER_ROLE は表・view・sequence・procedure・SQL ドメインなどの作成の
# 権限をまとめた role。
"${docker_cmd}" exec -i "${container}" sqlplus -s / as sysdba <<SQL
WHENEVER SQLERROR EXIT FAILURE
SET FEEDBACK OFF
SET VERIFY OFF
ALTER SESSION SET CONTAINER = FREEPDB1;
CREATE TABLESPACE USERS DATAFILE '/opt/oracle/oradata/FREE/FREEPDB1/users01.dbf'
  SIZE 256M AUTOEXTEND ON NEXT 64M;
BEGIN
  EXECUTE IMMEDIATE 'CREATE USER APP IDENTIFIED BY "${ORACLE_CI_PASSWORD}"'
    || ' DEFAULT TABLESPACE USERS QUOTA UNLIMITED ON USERS';
EXCEPTION
  WHEN OTHERS THEN
    RAISE_APPLICATION_ERROR(-20001, 'CREATE USER APP failed: ' || SQLERRM);
END;
/
GRANT DB_DEVELOPER_ROLE TO APP;
GRANT CREATE SESSION TO APP;
-- RAG はベクトル索引の構築の値（NEIGHBORS / EFCONSTRUCTION）を v\$vector_graph_index から読む（ADB の ADMIN は読める）。
GRANT SELECT ON SYS.V_\$VECTOR_GRAPH_INDEX TO APP;
EXIT
SQL
echo "テストの利用者 APP を作りました。"

settings=(
  "PLATFORM_ORACLE_USER=APP"
  "PLATFORM_ORACLE_PASSWORD=${ORACLE_CI_PASSWORD}"
  "PLATFORM_ORACLE_DSN=localhost:${port}/FREEPDB1"
  "PLATFORM_ORACLE_DRIVER_MODE=thin"
  "PLATFORM_ORACLE_CONNECTION_SECURITY=walletless_tls"
  "PLATFORM_ORACLE_CLIENT_LIB_DIR="
  "PLATFORM_ORACLE_WALLET_DIR="
  "PLATFORM_ORACLE_WALLET_PASSWORD="
)
mkdir -p "$(dirname "${env_file}")"
printf '%s\n' "${settings[@]}" > "${env_file}"
chmod 600 "${env_file}"
echo "接続の設定を ${env_file} に書きました。"
