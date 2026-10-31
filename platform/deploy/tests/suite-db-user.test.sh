#!/usr/bin/env bash
# platform/deploy/suite-db-user.py（配備で ADMIN からアプリ用の DB ユーザーを作る。#1668）を、python-oracledb の stub
# （実行した SQL と接続を JSON に記録する）で外部依存なしに検証する。実 ADB での確認は #1668 の PR に書く。
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
script="${repo}/platform/deploy/suite-db-user.py"
task_dir="$(mktemp -d)"
trap 'rm -rf -- "${task_dir}"' EXIT
python_bin="${PYTHON:-python3}"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# python-oracledb の stub。状態（ユーザーの有無・パスワード・接続の失敗）は STUB_ORACLE_STATE の JSON。
mkdir -p "${task_dir}/stub/oracledb"
cat > "${task_dir}/stub/oracledb/__init__.py" <<'PY'
import json
import os
import re

ALL_PRIVS = ["CREATE TABLE", "CREATE END USER", "GRANT ANY DATA ROLE", "ADMINISTER ANY DATA GRANT", "SET USE DATA GRANTS ONLY"]


class DatabaseError(Exception):
    pass


def _load():
    with open(os.environ["STUB_ORACLE_STATE"], encoding="utf-8") as handle:
        return json.load(handle)


def _save(state):
    with open(os.environ["STUB_ORACLE_STATE"], "w", encoding="utf-8") as handle:
        json.dump(state, handle)


class _Cursor:
    def __init__(self, user):
        self.user = user
        self.rows = []

    def execute(self, sql, binds=None):
        state = _load()
        state["executed"].append({"user": self.user, "sql": sql})
        failure = state.get("fail_sql")
        if failure and failure["match"] in sql:
            _save(state)
            raise DatabaseError(failure["error"])
        upper = sql.upper()
        if upper.startswith("SELECT USER"):
            self.rows = [(self.user,)]
        elif "FROM DBA_PROFILES" in upper:
            self.rows = [(1 if state.get("has_app_profile", True) else 0,)]
        elif "FROM DBA_USERS" in upper:
            self.rows = [tuple(state["existing"])] if state.get("existing") else []
        elif "FROM SESSION_PRIVS" in upper:
            self.rows = [(name,) for name in state.get("session_privs", ALL_PRIVS)]
        elif upper.startswith("CREATE USER"):
            state["existing"] = ["OPEN", "N"]
            state["app_password"] = re.search(r'IDENTIFIED BY "([^"]*)"', sql).group(1)
        elif upper.startswith("ALTER USER") and "IDENTIFIED BY" in upper:
            state["app_password"] = re.search(r'IDENTIFIED BY "([^"]*)"', sql).group(1)
        _save(state)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _Connection:
    def __init__(self, user):
        self.user = user

    def cursor(self):
        return _Cursor(self.user)

    def close(self):
        pass


def connect(user, password, dsn, **kwargs):
    state = _load()
    state["connects"].append(
        {
            "user": user,
            "dsn": dsn,
            "wallet": kwargs.get("wallet_location"),
            "wallet_password": kwargs.get("wallet_password"),
            "retry_count": kwargs.get("retry_count"),
            "retry_delay": kwargs.get("retry_delay"),
            "tcp_connect_timeout": kwargs.get("tcp_connect_timeout"),
        }
    )
    if user.upper() == state["admin_user"]:
        if state.get("admin_connect_errors"):
            error = state["admin_connect_errors"].pop(0)
            _save(state)
            raise DatabaseError(error)
        if password != state["admin_password"]:
            _save(state)
            raise DatabaseError("ORA-01017: invalid credential or not authorized; logon denied")
    elif not state.get("existing") or password != state.get("app_password"):
        _save(state)
        raise DatabaseError("ORA-01017: invalid credential or not authorized; logon denied")
    _save(state)
    return _Connection(user.upper())
PY

admin_pass="Stub-Admin-Pass-1"
app_pass="Stub-App-Pass-22"
screen_pass="Stub-Screen-Pass-3"

write_case() {
  local dir="$1" app_user="${2:-ADB_APP_USER}" env_user="${3:-ADB_APP_USER}"
  mkdir -p "${dir}"
  printf "SUITE_DB_ADMIN_USER=ADMIN\nSUITE_DB_ADMIN_PASSWORD='%s'\nSUITE_DB_APP_USER='%s'\n" "${admin_pass}" "${app_user}" \
    > "${dir}/admin.env"
  printf "PLATFORM_ORACLE_USER='%s'\nPLATFORM_ORACLE_PASSWORD='%s'\nPLATFORM_ORACLE_DSN='suiteadb_high'\nPLATFORM_ORACLE_WALLET_PASSWORD='%s'\n" \
    "${env_user}" "${app_pass}" "${app_pass}" > "${dir}/platform.env"
}

# $1: case の directory、$2: stub の初期状態（JSON）、残り: suite-db-user.py apply の追加の引数。
run_apply() {
  local dir="$1" state="$2"
  shift 2
  printf '%s\n' "${state}" > "${dir}/state.json"
  STUB_ORACLE_STATE="${dir}/state.json" PYTHONPATH="${task_dir}/stub" "${python_bin}" "${script}" apply \
    --admin-env "${dir}/admin.env" --app-env "${dir}/platform.env" --wallet-dir "${dir}/wallet" \
    --connect-wait-seconds 0 "$@" > "${dir}/out.log" 2>&1
}

executed() {
  "${python_bin}" -c 'import json,sys; [print(e["sql"]) for e in json.load(open(sys.argv[1]))["executed"]]' "$1/state.json"
}

assert_no_secret() {
  local dir="$1" secret
  for secret in "${admin_pass}" "${app_pass}" "${screen_pass}"; do
    if grep -qF "${secret}" "${dir}/out.log"; then fail "log にパスワードが出た: $(cat "${dir}/out.log")"; fi
  done
}

base_state='{"admin_user": "ADMIN", "admin_password": "'"${admin_pass}"'", "executed": [], "connects": []}'

# 1. plan: 付ける権限の一覧（DB に繋がない）。
plan="$("${python_bin}" "${script}" plan --app-user adb_app_user)"
expected_plan='CREATE USER ADB_APP_USER IDENTIFIED BY "<password>" DEFAULT TABLESPACE DATA QUOTA UNLIMITED ON DATA PROFILE ORA_APP_PROFILE;
GRANT PDB_DBA TO ADB_APP_USER;
GRANT GRANT ANY DATA ROLE TO ADB_APP_USER;
GRANT ADMINISTER ANY DATA GRANT TO ADB_APP_USER;
GRANT SET USE DATA GRANTS ONLY TO ADB_APP_USER;'
[ "${plan}" = "${expected_plan}" ] || fail "plan: ${plan}"
"${python_bin}" "${script}" plan --app-user ADB_APP_USER --existing | head -n 1 \
  | grep -qxF 'ALTER USER ADB_APP_USER DEFAULT TABLESPACE DATA QUOTA UNLIMITED ON DATA PROFILE ORA_APP_PROFILE;' \
  || fail "既存のユーザーの plan"

# 2. 新しく作る: CREATE USER（アプリのパスワード）→ grant → アプリのユーザーで接続して権限を確かめる。
case_dir="${task_dir}/create"
write_case "${case_dir}" adb_app_user adb_app_user
run_apply "${case_dir}" "${base_state}" || fail "作成が失敗した: $(cat "${case_dir}/out.log")"
sql="$(executed "${case_dir}")"
printf '%s\n' "${sql}" | grep -qxF "CREATE USER ADB_APP_USER IDENTIFIED BY \"${app_pass}\" DEFAULT TABLESPACE DATA QUOTA UNLIMITED ON DATA PROFILE ORA_APP_PROFILE" \
  || fail "CREATE USER: ${sql}"
for grant in "GRANT PDB_DBA TO ADB_APP_USER" "GRANT GRANT ANY DATA ROLE TO ADB_APP_USER" "GRANT ADMINISTER ANY DATA GRANT TO ADB_APP_USER" \
  "GRANT SET USE DATA GRANTS ONLY TO ADB_APP_USER"; do
  printf '%s\n' "${sql}" | grep -qxF "${grant}" || fail "${grant} が無い: ${sql}"
done
if printf '%s\n' "${sql}" | grep -q "GRANT DBA "; then fail "ADB では DBA ではなく PDB_DBA を付ける"; fi
grep -qF '[suite-db-user] CREATE USER ADB_APP_USER IDENTIFIED BY "***"' "${case_dir}/out.log" || fail "CREATE の log: $(cat "${case_dir}/out.log")"
tail -n 1 "${case_dir}/out.log" | grep -qF "[suite-db-user] OK: ADB_APP_USER で接続でき" || fail "OK の行: $(cat "${case_dir}/out.log")"
"${python_bin}" - "${case_dir}" <<'PY' || fail "接続の引数"
import json, sys
state = json.load(open(sys.argv[1] + "/state.json"))
first = state["connects"][0]
assert first["user"] == "ADMIN" and first["dsn"] == "suiteadb_high", first
assert first["wallet"] == sys.argv[1] + "/wallet", first
# Wallet のパスワードは platform.env の PLATFORM_ORACLE_WALLET_PASSWORD（ADMIN のパスワードではない）。
assert first["wallet_password"] == "Stub-App-Pass-22", first
assert state["connects"][-1]["user"] == "ADB_APP_USER", state["connects"]
PY
assert_no_secret "${case_dir}"

# 3. 既にある・パスワードが違う: profile・quota を合わせ、パスワードを合わせて lock を外し、rollover を終える。
case_dir="${task_dir}/existing"
write_case "${case_dir}"
state="${base_state%\}}, \"existing\": [\"LOCKED\", \"N\"], \"app_password\": \"Old-Pass-123456\"}"
run_apply "${case_dir}" "${state}" || fail "既存のユーザーで失敗した: $(cat "${case_dir}/out.log")"
sql="$(executed "${case_dir}")"
if printf '%s\n' "${sql}" | grep -q "^CREATE USER"; then fail "既存なのに CREATE USER した"; fi
printf '%s\n' "${sql}" | grep -qxF "ALTER USER ADB_APP_USER DEFAULT TABLESPACE DATA QUOTA UNLIMITED ON DATA PROFILE ORA_APP_PROFILE" \
  || fail "profile・quota を合わせていない: ${sql}"
printf '%s\n' "${sql}" | grep -qxF "ALTER USER ADB_APP_USER IDENTIFIED BY \"${app_pass}\" ACCOUNT UNLOCK" || fail "パスワードを合わせていない: ${sql}"
printf '%s\n' "${sql}" | grep -qxF "ALTER USER ADB_APP_USER EXPIRE PASSWORD ROLLOVER PERIOD" || fail "rollover を終えていない: ${sql}"
grep -qF "（ORA-01017）" "${case_dir}/out.log" || fail "合わせた理由の log: $(cat "${case_dir}/out.log")"
assert_no_secret "${case_dir}"

# 4. 既にあり、パスワードも合っている: パスワードは変えない（再配備）。
case_dir="${task_dir}/rerun"
write_case "${case_dir}"
state="${base_state%\}}, \"existing\": [\"OPEN\", \"N\"], \"app_password\": \"${app_pass}\"}"
run_apply "${case_dir}" "${state}" || fail "再配備で失敗した: $(cat "${case_dir}/out.log")"
if executed "${case_dir}" | grep -q "IDENTIFIED BY"; then fail "合っているのにパスワードを変えた"; fi

# 5. 画面で保存した platform/.env のパスワードを優先する（同じユーザーのとき）。別のユーザーなら props の値。
case_dir="${task_dir}/screen"
write_case "${case_dir}"
printf "PLATFORM_ORACLE_USER='adb_app_user'\nPLATFORM_ORACLE_PASSWORD='%s'\n" "${screen_pass}" > "${case_dir}/current.env"
run_apply "${case_dir}" "${base_state}" --current-env "${case_dir}/current.env" || fail "画面の値で失敗した: $(cat "${case_dir}/out.log")"
executed "${case_dir}" | grep -qF "IDENTIFIED BY \"${screen_pass}\"" || fail "画面で保存したパスワードを使っていない"
assert_no_secret "${case_dir}"
printf "PLATFORM_ORACLE_USER='OTHER'\nPLATFORM_ORACLE_PASSWORD='%s'\n" "${screen_pass}" > "${case_dir}/current.env"
run_apply "${case_dir}" "${base_state}" --current-env "${case_dir}/current.env" || fail "別のユーザーの platform/.env で失敗した"
executed "${case_dir}" | grep -qF "IDENTIFIED BY \"${app_pass}\"" || fail "別のユーザーなら props のパスワード"

# 6. ADMIN のパスワードが違う: ORA の番号と手がかりを出して 1 で終わる（パスワードは出さない）。
case_dir="${task_dir}/admin-denied"
write_case "${case_dir}"
state='{"admin_user": "ADMIN", "admin_password": "Different-Pass-9", "executed": [], "connects": []}'
status=0
run_apply "${case_dir}" "${state}" || status=$?
[ "${status}" -eq 1 ] || fail "ADMIN の接続の失敗で 1 にならない（${status}）"
grep -qF "[suite-db-user] NG（ORA-01017）" "${case_dir}/out.log" || fail "NG の行: $(cat "${case_dir}/out.log")"
grep -qF "existing_adb_admin_password" "${case_dir}/out.log" || fail "手がかり: $(cat "${case_dir}/out.log")"
assert_no_secret "${case_dir}"

# 7. ADB に一時的に繋がらない（起動の直後など）: 試し直して成功する。繋がらないままなら失敗する。
case_dir="${task_dir}/retry"
write_case "${case_dir}"
state="${base_state%\}}, \"admin_connect_errors\": [\"DPY-6005: cannot connect to database\", \"DPY-6005: cannot connect to database\"]}"
run_apply "${case_dir}" "${state}" --connect-attempts 3 || fail "試し直さない: $(cat "${case_dir}/out.log")"
grep -qF "試し直します（2/3）" "${case_dir}/out.log" || fail "試し直しの log: $(cat "${case_dir}/out.log")"
status=0
run_apply "${case_dir}" "${state}" --connect-attempts 2 || status=$?
[ "${status}" -eq 1 ] || fail "繋がらないのに成功した"
grep -qF "NG（DPY-6005）" "${case_dir}/out.log" || fail "接続の NG の行: $(cat "${case_dir}/out.log")"

# 7b. Wallet の別名の TNS の再試行（retry_count=20）は外して接続する（DB の停止中に 1 回の接続で長く待たない。
#     試し直しは connect_admin の間隔に任せる。#1779）。記述子の DSN も同じ。log には元の DSN を出す。
case_dir="${task_dir}/tns-retry"
write_case "${case_dir}"
mkdir -p "${case_dir}/wallet"
printf 'suiteadb_high = (description= (retry_count=20)(retry_delay=3)(address=(protocol=tcps)(port=1522)(host=adb.example.com))(connect_data=(service_name=x_high.adb.oraclecloud.com))(security=(ssl_server_dn_match=yes)))\n' \
  > "${case_dir}/wallet/tnsnames.ora"
run_apply "${case_dir}" "${base_state}" || fail "TNS の再試行を外すと失敗した: $(cat "${case_dir}/out.log")"
"${python_bin}" - "${case_dir}" <<'PY' || fail "TNS の再試行を外した接続の引数"
import json, sys
state = json.load(open(sys.argv[1] + "/state.json"))
for connect in state["connects"]:
    assert connect["dsn"].startswith("(description="), connect
    assert "retry_count" not in connect["dsn"] and "retry_delay" not in connect["dsn"], connect
    assert "host=adb.example.com" in connect["dsn"], connect
    assert connect["retry_count"] == 0 and connect["retry_delay"] == 0, connect
    assert connect["tcp_connect_timeout"] == 10.0, connect
PY
grep -qF "（DSN suiteadb_high）" "${case_dir}/out.log" || fail "log の DSN: $(cat "${case_dir}/out.log")"
printf "PLATFORM_ORACLE_USER='ADB_APP_USER'\nPLATFORM_ORACLE_PASSWORD='%s'\nPLATFORM_ORACLE_DSN='(description=(retry_count=20)(retry_delay=3)(address=(protocol=tcps)(port=1522)(host=adb.example.com))(connect_data=(service_name=x_high.adb.oraclecloud.com)))'\n" \
  "${app_pass}" > "${case_dir}/platform.env"
rm -rf "${case_dir}/wallet"
run_apply "${case_dir}" "${base_state}" || fail "記述子の DSN で失敗した: $(cat "${case_dir}/out.log")"
"${python_bin}" -c 'import json,sys; c=json.load(open(sys.argv[1]))["connects"][0]; assert "retry_count" not in c["dsn"] and "host=adb.example.com" in c["dsn"], c' \
  "${case_dir}/state.json" || fail "記述子の DSN の TNS の再試行を外さない"
assert_no_secret "${case_dir}"

# 8. 設定の誤り: props のユーザーが食い違う・使えない名前・Oracle の管理のユーザー・grant の失敗・権限が効いていない。
case_dir="${task_dir}/mismatch"
write_case "${case_dir}" ADB_APP_USER OTHER
status=0
run_apply "${case_dir}" "${base_state}" || status=$?
[ "${status}" -eq 1 ] && grep -qF "PLATFORM_ORACLE_USER（OTHER）がアプリ用のユーザー（ADB_APP_USER）と違います" "${case_dir}/out.log" \
  || fail "食い違いを検出しない: $(cat "${case_dir}/out.log")"
for name in ADMIN SYS DEEPSEC_DATA_USER ORA_X 9USER 'A-B'; do
  case_dir="${task_dir}/name-${name//[^A-Za-z0-9]/_}"
  write_case "${case_dir}" "${name}" "${name}"
  status=0
  run_apply "${case_dir}" "${base_state}" || status=$?
  [ "${status}" -eq 1 ] || fail "${name} を受け入れた: $(cat "${case_dir}/out.log")"
done
case_dir="${task_dir}/maintained"
write_case "${case_dir}"
status=0
run_apply "${case_dir}" "${base_state%\}}, \"existing\": [\"OPEN\", \"Y\"]}" || status=$?
[ "${status}" -eq 1 ] && grep -qF "Oracle の管理のユーザー" "${case_dir}/out.log" || fail "Oracle の管理のユーザーを受け入れた"
case_dir="${task_dir}/grant-failed"
write_case "${case_dir}"
status=0
run_apply "${case_dir}" "${base_state%\}}, \"fail_sql\": {\"match\": \"GRANT PDB_DBA\", \"error\": \"ORA-01031: insufficient privileges\"}}" \
  || status=$?
[ "${status}" -eq 1 ] && grep -qF "NG（ORA-01031）: GRANT PDB_DBA TO ADB_APP_USER が失敗しました" "${case_dir}/out.log" \
  || fail "grant の失敗: $(cat "${case_dir}/out.log")"
case_dir="${task_dir}/missing-privs"
write_case "${case_dir}"
status=0
run_apply "${case_dir}" "${base_state%\}}, \"session_privs\": [\"CREATE TABLE\", \"CREATE END USER\"]}" || status=$?
[ "${status}" -eq 1 ] && grep -qF "権限が付いていません: ADMINISTER ANY DATA GRANT, GRANT ANY DATA ROLE, SET USE DATA GRANTS ONLY" \
  "${case_dir}/out.log" || fail "権限の不足を検出しない: $(cat "${case_dir}/out.log")"
case_dir="${task_dir}/password-has-user"
write_case "${case_dir}"
printf "PLATFORM_ORACLE_USER='ADB_APP_USER'\nPLATFORM_ORACLE_PASSWORD='Xadb_app_user-Pass-1'\nPLATFORM_ORACLE_DSN='suiteadb_high'\n" > "${case_dir}/platform.env"
status=0
run_apply "${case_dir}" "${base_state}" || status=$?
[ "${status}" -eq 1 ] && grep -qF "ユーザー名（ADB_APP_USER）を含めないでください" "${case_dir}/out.log" || fail "ユーザー名を含むパスワード"

echo "suite-db-user: pass"
