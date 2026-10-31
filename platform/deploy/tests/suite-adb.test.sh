#!/usr/bin/env bash
# platform/deploy/suite-adb.py（suite-restart.sh / suite-update.sh の前に、ADB が止まっていれば起動して AVAILABLE を
# 待つ。#1745）を、OCI SDK の stub（状態の並び・失敗・呼び出しを JSON に記録する）で外部依存なしに検証する。
# OCI の呼び出しは本物の pr_system_settings.oci_database（システム設定 › データベース と同じ client）を通す。
# 実 OCI の ADB での確認は #1745 の PR に書く。
#
# Python: SUITE_ADB_TEST_PYTHON（pr_system_settings を import できる Python。例: 製品の backend/.venv/bin/python）。
# 無ければ uv で platform/packages/system_settings_backend の lock どおりの一時の venv を作る（dev の依存は入れない。
# OCI SDK は stub を使うため入れない）。
# shellcheck disable=SC2015 # `A && B || fail` は「A か B が偽なら fail」の意図（if-then-else ではない）。
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
script="${repo}/platform/deploy/suite-adb.py"
task_dir="$(mktemp -d)"
trap 'rm -rf -- "${task_dir}"' EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

python_bin="${SUITE_ADB_TEST_PYTHON:-}"
if [ -z "${python_bin}" ]; then
  command -v uv >/dev/null 2>&1 || fail "uv か SUITE_ADB_TEST_PYTHON が要ります（pr_system_settings を import する）"
  UV_PROJECT_ENVIRONMENT="${task_dir}/venv" uv sync -q --frozen --no-dev \
    --project "${repo}/platform/packages/system_settings_backend" >/dev/null
  python_bin="${task_dir}/venv/bin/python"
fi

# ADB を停止する処理を持たない。
if grep -q "stop_autonomous_database" "${script}"; then fail "suite-adb.py が ADB を停止する"; fi

# OCI SDK の stub。状態は STUB_OCI_STATE の JSON:
#   states: get_autonomous_database が順に返す lifecycle_state（最後の値を返し続ける）
#   get_errors / start_errors: 先に返す失敗（{"status", "code"} か {"type"}。status が無ければ network の失敗）
#   calls: 呼び出しの記録（get / start / stop・client の region・signer・profile）
mkdir -p "${task_dir}/stub/oci/auth"
cat > "${task_dir}/stub/oci/__init__.py" <<'PY'
import json
import os


def load():
    with open(os.environ["STUB_OCI_STATE"], encoding="utf-8") as handle:
        return json.load(handle)


def save(state):
    with open(os.environ["STUB_OCI_STATE"], "w", encoding="utf-8") as handle:
        json.dump(state, handle)
PY
cat > "${task_dir}/stub/oci/exceptions.py" <<'PY'
class ServiceError(Exception):
    def __init__(self, status, code, message="stub", operation_name=None):
        super().__init__(f"{status} {code} {message}")
        self.status = status
        self.code = code
        self.message = message
        self.operation_name = operation_name
        self.headers = {"opc-request-id": "stub-request-id"}


class ConfigFileNotFound(Exception):
    pass


class ProfileNotFound(Exception):
    pass


class RequestException(Exception):
    pass
PY
cat > "${task_dir}/stub/oci/config.py" <<'PY'
import configparser
import os

from . import exceptions


def from_file(file_location, profile_name):
    if not os.path.isfile(file_location):
        raise exceptions.ConfigFileNotFound(f"Could not find config file at {file_location}")
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(file_location)
    if profile_name != "DEFAULT" and not parser.has_section(profile_name):
        raise exceptions.ProfileNotFound(f"Profile '{profile_name}' not found in config file")
    values = dict(parser[profile_name])
    values["_profile"] = profile_name
    return values
PY
cat > "${task_dir}/stub/oci/auth/__init__.py" <<'PY'
PY
cat > "${task_dir}/stub/oci/auth/signers.py" <<'PY'
class InstancePrincipalsSecurityTokenSigner:
    pass
PY
cat > "${task_dir}/stub/oci/database.py" <<'PY'
from types import SimpleNamespace

from . import exceptions, load, save


def _raise(error, operation):
    if "status" in error:
        raise exceptions.ServiceError(error["status"], error.get("code", ""), operation_name=operation)
    raise getattr(exceptions, error.get("type", "RequestException"))("stub failure")


class DatabaseClient:
    def __init__(self, config, signer=None, **kwargs):
        state = load()
        state["calls"].append(
            {"op": "client", "region": config.get("region"), "profile": config.get("_profile"), "signer": signer is not None}
        )
        save(state)

    def get_autonomous_database(self, autonomous_database_id):
        state = load()
        state["calls"].append({"op": "get", "id": autonomous_database_id})
        if state.get("get_errors"):
            error = state["get_errors"].pop(0)
            save(state)
            _raise(error, "GetAutonomousDatabase")
        current = state["states"][0]
        if len(state["states"]) > 1:
            state["states"].pop(0)
        save(state)
        return SimpleNamespace(
            data=SimpleNamespace(
                id=autonomous_database_id,
                display_name="SUITEADB",
                lifecycle_state=current,
                db_name="SUITEADB",
                cpu_core_count=2,
                data_storage_size_in_tbs=1,
            )
        )

    def start_autonomous_database(self, autonomous_database_id):
        state = load()
        state["calls"].append({"op": "start", "id": autonomous_database_id})
        if state.get("start_errors"):
            error = state["start_errors"].pop(0)
            save(state)
            _raise(error, "StartAutonomousDatabase")
        save(state)

    def stop_autonomous_database(self, autonomous_database_id):
        state = load()
        state["calls"].append({"op": "stop", "id": autonomous_database_id})
        save(state)
PY

adb_ocid="ocid1.autonomousdatabase.oc1.ap-osaka-1.stubadb"
db_pass="Stub-Db-Pass-1"
fingerprint="aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99"
home_dir="${task_dir}/home"
mkdir -p "${home_dir}/.oci"
printf -- '-----BEGIN PRIVATE KEY-----\nSTUBKEYMATERIAL\n-----END PRIVATE KEY-----\n' > "${home_dir}/.oci/oci_api_key.pem"
printf '[DEFAULT]\nuser=ocid1.user.oc1..stub\nfingerprint=%s\ntenancy=ocid1.tenancy.oc1..stub\nregion=us-chicago-1\nkey_file=~/.oci/oci_api_key.pem\n\n[SUITE]\nuser=ocid1.user.oc1..stub\nfingerprint=%s\ntenancy=ocid1.tenancy.oc1..stub\nregion=us-chicago-1\nkey_file=~/.oci/oci_api_key.pem\n' \
  "${fingerprint}" "${fingerprint}" > "${home_dir}/.oci/config"

# $1: case の directory、残り: platform/.env の行（既定の行の後に足す。同じ名前は後が勝つ）。
write_env() {
  local dir="$1"
  shift
  mkdir -p "${dir}"
  {
    printf "PLATFORM_ORACLE_USER='ADB_APP_USER'\nPLATFORM_ORACLE_PASSWORD='%s'\n" "${db_pass}"
    printf "PLATFORM_ORACLE_ADB_OCID=%s\nPLATFORM_ORACLE_ADB_REGION=ap-osaka-1\nPLATFORM_OCI_REGION='ap-osaka-1'\n" "${adb_ocid}"
    printf '%s\n' "$@"
  } > "${dir}/platform.env"
}

# $1: case の directory、$2: stub の初期状態（JSON）、残り: suite-adb.py の引数（ensure / status ...）。
run_adb() {
  local dir="$1" state="$2"
  shift 2
  printf '%s\n' "${state}" > "${dir}/state.json"
  HOME="${home_dir}" STUB_OCI_STATE="${dir}/state.json" PYTHONPATH="${task_dir}/stub" \
    "${python_bin}" "${script}" "$@" --env-file "${dir}/platform.env" > "${dir}/out.log" 2>&1
}

ensure_args=(ensure --timeout-seconds 5 --interval-seconds 0 --report-seconds 0)

calls() {
  "${python_bin}" -c 'import json,sys; [print(c["op"]) for c in json.load(open(sys.argv[1]))["calls"]]' "$1/state.json"
}

count_calls() {
  calls "$1" | grep -cx "$2" || true
}

last_line() {
  tail -n 1 "$1/out.log"
}

assert_no_secret() {
  local dir="$1" secret
  for secret in "${db_pass}" "${fingerprint}" "STUBKEYMATERIAL" "ocid1.user.oc1..stub" "ocid1.tenancy.oc1..stub"; do
    if grep -qF "${secret}" "${dir}/out.log"; then fail "log に認証の値が出た（${secret}）: $(cat "${dir}/out.log")"; fi
  done
  [ "$(count_calls "${dir}" stop)" -eq 0 ] || fail "ADB を停止した: $(calls "${dir}")"
}

state_json() {
  printf '{"states": %s, "calls": []%s}' "$1" "${2:+, $2}"
}

# 1. AVAILABLE: 起動を要求せずに進む。
case_dir="${task_dir}/available"
write_env "${case_dir}"
run_adb "${case_dir}" "$(state_json '["AVAILABLE"]')" "${ensure_args[@]}" || fail "AVAILABLE で失敗した: $(cat "${case_dir}/out.log")"
[ "$(count_calls "${case_dir}" start)" -eq 0 ] || fail "AVAILABLE なのに起動した"
last_line "${case_dir}" | grep -qxF "[suite-adb] OK: ADB SUITEADB（ap-osaka-1） は AVAILABLE です。" || fail "OK の行: $(cat "${case_dir}/out.log")"
"${python_bin}" - "${case_dir}" <<'PY' || fail "client の region・profile"
import json, sys
calls = json.load(open(sys.argv[1] + "/state.json"))["calls"]
client = calls[0]
# region は PLATFORM_ORACLE_ADB_REGION（config の region より優先。画面と同じ）、profile は既定の DEFAULT。
assert client == {"op": "client", "region": "ap-osaka-1", "profile": "DEFAULT", "signer": False}, client
PY
assert_no_secret "${case_dir}"

# 2. STOPPED: 起動を 1 回要求し、STARTING を待って AVAILABLE で進む。経過を log に出す。
case_dir="${task_dir}/stopped"
write_env "${case_dir}" "PLATFORM_OCI_CONFIG_PROFILE='SUITE'"
run_adb "${case_dir}" "$(state_json '["STOPPED", "STOPPED", "STARTING", "STARTING", "AVAILABLE"]')" "${ensure_args[@]}" \
  || fail "STOPPED から起動できない: $(cat "${case_dir}/out.log")"
[ "$(count_calls "${case_dir}" start)" -eq 1 ] || fail "起動の要求が 1 回でない: $(calls "${case_dir}")"
grep -qF "は STOPPED です。platform/.env の OCI の認証（config_file）で起動します。" "${case_dir}/out.log" || fail "起動の log: $(cat "${case_dir}/out.log")"
grep -qF "ADB の起動を要求しました。AVAILABLE になるまで待ちます（経過" "${case_dir}/out.log" || fail "要求の log"
grep -qE "は STARTING です。待ちます（経過 [0-9]+ 秒 / 上限 5 秒）。" "${case_dir}/out.log" || fail "待ちの経過の log: $(cat "${case_dir}/out.log")"
grep -qF "が AVAILABLE になりました" "${case_dir}/out.log" || fail "AVAILABLE の log"
last_line "${case_dir}" | grep -qF "[suite-adb] OK:" || fail "OK の行: $(cat "${case_dir}/out.log")"
"${python_bin}" -c 'import json,sys; c=json.load(open(sys.argv[1]))["calls"][0]; assert c["profile"] == "SUITE", c' "${case_dir}/state.json" \
  || fail "PLATFORM_OCI_CONFIG_PROFILE を使わない"
assert_no_secret "${case_dir}"

# 3. STARTING: 起動を要求せずに待つ。STOPPING: STOPPED を待ってから起動する。
case_dir="${task_dir}/starting"
write_env "${case_dir}"
run_adb "${case_dir}" "$(state_json '["STARTING", "AVAILABLE"]')" "${ensure_args[@]}" || fail "STARTING を待たない: $(cat "${case_dir}/out.log")"
[ "$(count_calls "${case_dir}" start)" -eq 0 ] || fail "STARTING なのに起動を要求した"
case_dir="${task_dir}/stopping"
write_env "${case_dir}"
run_adb "${case_dir}" "$(state_json '["STOPPING", "STOPPING", "STOPPED", "STARTING", "AVAILABLE"]')" "${ensure_args[@]}" \
  || fail "STOPPING の後に起動しない: $(cat "${case_dir}/out.log")"
[ "$(calls "${case_dir}" | tr '\n' ' ')" = "client get get get start get get " ] || fail "STOPPING の呼び出しの順: $(calls "${case_dir}" | tr '\n' ' ')"

# 4. DB を開いたままの状態（BACKUP_IN_PROGRESS など）はそのまま進む。待てる状態（UPDATING など）は待つ。
for busy in BACKUP_IN_PROGRESS SCALE_IN_PROGRESS AVAILABLE_NEEDS_ATTENTION; do
  case_dir="${task_dir}/busy-${busy}"
  write_env "${case_dir}"
  run_adb "${case_dir}" "$(state_json "[\"${busy}\"]")" "${ensure_args[@]}" || fail "${busy} で失敗した: $(cat "${case_dir}/out.log")"
  [ "$(count_calls "${case_dir}" start)" -eq 0 ] || fail "${busy} で起動した"
done
for waiting in UPDATING MAINTENANCE_IN_PROGRESS RESTARTING PROVISIONING; do
  case_dir="${task_dir}/wait-${waiting}"
  write_env "${case_dir}"
  run_adb "${case_dir}" "$(state_json "[\"${waiting}\", \"AVAILABLE\"]")" "${ensure_args[@]}" || fail "${waiting} を待たない"
  [ "$(count_calls "${case_dir}" start)" -eq 0 ] || fail "${waiting} で起動した"
done

# 5. 待っても使えない状態: 起動せずに、理由と手で起動する方法を出して止める。
for stuck in UNAVAILABLE TERMINATED INACCESSIBLE STANDBY RESTORE_FAILED SOMETHING_NEW; do
  case_dir="${task_dir}/stuck-${stuck}"
  write_env "${case_dir}"
  status=0
  run_adb "${case_dir}" "$(state_json "[\"${stuck}\"]")" "${ensure_args[@]}" || status=$?
  [ "${status}" -eq 1 ] || fail "${stuck} で 1 にならない（${status}）: $(cat "${case_dir}/out.log")"
  [ "$(count_calls "${case_dir}" start)" -eq 0 ] || fail "${stuck} で起動した"
  last_line "${case_dir}" | grep -qF "[suite-adb] NG: " || fail "${stuck} の NG の行: $(cat "${case_dir}/out.log")"
  last_line "${case_dir}" | grep -qF "（${stuck}）" || last_line "${case_dir}" | grep -qF "${stuck} で、自動では起動できません" \
    || fail "${stuck} の理由: $(cat "${case_dir}/out.log")"
  last_line "${case_dir}" | grep -qF "oci db autonomous-database start --autonomous-database-id ${adb_ocid}" \
    || fail "${stuck} の手で起動する方法: $(cat "${case_dir}/out.log")"
  assert_no_secret "${case_dir}"
done

# 6. 時間切れ: STARTING のまま上限を超えたら止める。
case_dir="${task_dir}/timeout"
write_env "${case_dir}"
status=0
run_adb "${case_dir}" "$(state_json '["STOPPED", "STARTING"]')" ensure --timeout-seconds 0.5 --interval-seconds 0.1 \
  --script-name /u01/aipoc/no.1-production-ready-suite/platform/deploy/suite-update.sh || status=$?
[ "${status}" -eq 1 ] || fail "時間切れで 1 にならない（${status}）"
last_line "${case_dir}" | grep -qF "ADB が 0.5 秒以内に AVAILABLE になりません（今の状態 STARTING）" || fail "時間切れの理由: $(cat "${case_dir}/out.log")"
last_line "${case_dir}" | grep -qF "起動すれば、そのまま DB を使えます（sudo /u01/aipoc/no.1-production-ready-suite/platform/deploy/suite-update.sh の再実行は要りません）" \
  || fail "実行し直すコマンド: $(cat "${case_dir}/out.log")"

# 7. 権限が無い・認証の失敗: 試し直さずに止める（404 は ADB が無いか inspect が無い、起動の 404 は use が無い）。
case_dir="${task_dir}/get-404"
write_env "${case_dir}"
status=0
run_adb "${case_dir}" "$(state_json '["AVAILABLE"]' '"get_errors": [{"status": 404, "code": "NotAuthorizedOrNotFound"}]')" "${ensure_args[@]}" || status=$?
[ "${status}" -eq 1 ] || fail "404 で 1 にならない"
[ "$(count_calls "${case_dir}" get)" -eq 1 ] || fail "404 を試し直した"
last_line "${case_dir}" | grep -qF "ADB が見つからないか、ADB を見る権限がありません（ServiceError / HTTP 404 / NotAuthorizedOrNotFound / opc-request-id stub-request-id）" \
  || fail "404 の理由: $(cat "${case_dir}/out.log")"
last_line "${case_dir}" | grep -qF "inspect autonomous-databases" || fail "404 の policy の手がかり"
assert_no_secret "${case_dir}"
case_dir="${task_dir}/get-401"
write_env "${case_dir}"
status=0
run_adb "${case_dir}" "$(state_json '["AVAILABLE"]' '"get_errors": [{"status": 401, "code": "NotAuthenticated"}]')" "${ensure_args[@]}" || status=$?
[ "${status}" -eq 1 ] && last_line "${case_dir}" | grep -qF "OCI の認証に失敗しました（ServiceError / HTTP 401 / NotAuthenticated" \
  || fail "401 の理由: $(cat "${case_dir}/out.log")"
assert_no_secret "${case_dir}"
case_dir="${task_dir}/start-404"
write_env "${case_dir}"
status=0
run_adb "${case_dir}" "$(state_json '["STOPPED"]' '"start_errors": [{"status": 404, "code": "NotAuthorizedOrNotFound"}]')" "${ensure_args[@]}" || status=$?
[ "${status}" -eq 1 ] && last_line "${case_dir}" | grep -qF "ADB を起動する権限がありません" \
  && last_line "${case_dir}" | grep -qF "use autonomous-databases" || fail "起動の 404 の理由: $(cat "${case_dir}/out.log")"

# 8. 一時の失敗（5xx・network）と、起動の 409（状態が変わった）は試し直す。続けば止める。
case_dir="${task_dir}/transient"
write_env "${case_dir}"
run_adb "${case_dir}" "$(state_json '["STOPPED", "STARTING", "AVAILABLE"]' '"get_errors": [{"status": 503, "code": "ServiceUnavailable"}, {"type": "RequestException"}], "start_errors": [{"status": 409, "code": "IncorrectState"}]')" \
  ensure --timeout-seconds 5 --interval-seconds 0 --report-seconds 0 || fail "一時の失敗で止まった: $(cat "${case_dir}/out.log")"
grep -qF "試し直します（2/5" "${case_dir}/out.log" || fail "試し直しの log: $(cat "${case_dir}/out.log")"
grep -qF "起動の要求が受け付けられませんでした（ServiceError / HTTP 409 / IncorrectState" "${case_dir}/out.log" || fail "409 の log"
case_dir="${task_dir}/transient-forever"
write_env "${case_dir}"
status=0
run_adb "${case_dir}" "$(state_json '["AVAILABLE"]' '"get_errors": [{"status": 500}, {"status": 500}, {"status": 500}, {"status": 500}, {"status": 500}, {"status": 500}]')" \
  "${ensure_args[@]}" || status=$?
[ "${status}" -eq 1 ] && last_line "${case_dir}" | grep -qF "ADB の状態の取得が続けて失敗しました" || fail "一時の失敗が続く: $(cat "${case_dir}/out.log")"

# 9. 設定が無い: ADB の OCID・OCI の config・profile・対応しない認証の方式・platform/.env。
case_dir="${task_dir}/no-ocid"
write_env "${case_dir}" "PLATFORM_ORACLE_ADB_OCID="
status=0
run_adb "${case_dir}" "$(state_json '["AVAILABLE"]')" "${ensure_args[@]}" || status=$?
[ "${status}" -eq 1 ] && last_line "${case_dir}" | grep -qF "PLATFORM_ORACLE_ADB_OCID がありません" \
  && last_line "${case_dir}" | grep -qF "「システム設定 › データベース」から起動できます" || fail "OCID が無い: $(cat "${case_dir}/out.log")"
case_dir="${task_dir}/no-config"
write_env "${case_dir}" "PLATFORM_OCI_CONFIG_FILE=${task_dir}/missing/config"
status=0
run_adb "${case_dir}" "$(state_json '["AVAILABLE"]')" "${ensure_args[@]}" || status=$?
[ "${status}" -eq 1 ] && last_line "${case_dir}" | grep -qF "OCI の認証の設定ファイル（${task_dir}/missing/config）がありません" \
  || fail "config が無い: $(cat "${case_dir}/out.log")"
case_dir="${task_dir}/no-profile"
write_env "${case_dir}" "PLATFORM_OCI_CONFIG_PROFILE=MISSING"
status=0
run_adb "${case_dir}" "$(state_json '["AVAILABLE"]')" "${ensure_args[@]}" || status=$?
[ "${status}" -eq 1 ] && last_line "${case_dir}" | grep -qF "を読めません（ProfileNotFound）" || fail "profile が無い: $(cat "${case_dir}/out.log")"
[ "$(count_calls "${case_dir}" get)" -eq 0 ] || fail "config を読めないのに OCI を呼んだ"
assert_no_secret "${case_dir}"
case_dir="${task_dir}/bad-mode"
write_env "${case_dir}" "PLATFORM_OCI_AUTH_MODE=resource_principal"
status=0
run_adb "${case_dir}" "$(state_json '["AVAILABLE"]')" "${ensure_args[@]}" || status=$?
[ "${status}" -eq 1 ] && last_line "${case_dir}" | grep -qF "PLATFORM_OCI_AUTH_MODE=resource_principal では ADB を操作できません" \
  || fail "対応しない認証の方式: $(cat "${case_dir}/out.log")"
case_dir="${task_dir}/no-env"
mkdir -p "${case_dir}"
status=0
run_adb "${case_dir}" "$(state_json '["AVAILABLE"]')" "${ensure_args[@]}" || status=$?
[ "${status}" -eq 1 ] && last_line "${case_dir}" | grep -qF "platform.env がありません" || fail "platform/.env が無い: $(cat "${case_dir}/out.log")"

# 10. instance_principal: ~/.oci/config を読まず、signer と ADB の region で client を作る。
case_dir="${task_dir}/instance-principal"
write_env "${case_dir}" "PLATFORM_OCI_AUTH_MODE=instance_principal" "PLATFORM_OCI_CONFIG_FILE=${task_dir}/missing/config"
run_adb "${case_dir}" "$(state_json '["STOPPED", "AVAILABLE"]')" "${ensure_args[@]}" || fail "instance principal: $(cat "${case_dir}/out.log")"
"${python_bin}" -c 'import json,sys; c=json.load(open(sys.argv[1]))["calls"][0]; assert c["signer"] is True and c["region"] == "ap-osaka-1", c' \
  "${case_dir}/state.json" || fail "instance principal の client"
[ "$(count_calls "${case_dir}" start)" -eq 1 ] || fail "instance principal で起動しない"

# 11. status: 見るだけ（起動しない）。AVAILABLE は 0、それ以外は 3、失敗は 1。
case_dir="${task_dir}/status"
write_env "${case_dir}"
status=0
run_adb "${case_dir}" "$(state_json '["STOPPED"]')" status || status=$?
[ "${status}" -eq 3 ] || fail "status で STOPPED が 3 にならない（${status}）"
[ "$(count_calls "${case_dir}" start)" -eq 0 ] || fail "status で起動した"
grep -qF "は STOPPED です（--status では起動しません" "${case_dir}/out.log" || fail "status の log: $(cat "${case_dir}/out.log")"
run_adb "${case_dir}" "$(state_json '["AVAILABLE"]')" status || fail "status で AVAILABLE が 0 にならない"
status=0
run_adb "${case_dir}" "$(state_json '["AVAILABLE"]' '"get_errors": [{"status": 404, "code": "NotAuthorizedOrNotFound"}]')" status || status=$?
[ "${status}" -eq 1 ] || fail "status の失敗が 1 にならない"
assert_no_secret "${case_dir}"

# 12. suite-restart.sh からの通し（#1756）: 本物の suite-adb.py を suite_run_adb で実行し、OCI の認証が無い・起動が NG の
#     ときも warning を出して build（ここでは stub）まで進み、終了コードを失敗にしない。認証があり STOPPED なら、起動して
#     AVAILABLE を待ってから build する。
(
  export APP_ROOT="${task_dir}/restart"
  export SUITE_REPO_DIR="${APP_ROOT}/no.1-production-ready-suite"
  export PROPS_DIR="${APP_ROOT}/props"
  export SUITE_OPS_INHERITED=true
  export SUITE_ADB_START_TIMEOUT_SECONDS=5 SUITE_ADB_POLL_INTERVAL_SECONDS=0
  mkdir -p "${PROPS_DIR}" "${SUITE_REPO_DIR}/platform"
  printf 'rag\n' > "${PROPS_DIR}/products.txt"
  export SUITE_PLATFORM_ENV_FILE="${SUITE_REPO_DIR}/platform/.env"
  # 製品の venv の Python の代わり（stub の oci と、この test の HOME の ~/.oci/config を使う）。
  export SUITE_ADB_PYTHON="${task_dir}/restart-python"
  printf '#!/usr/bin/env bash\nexec env HOME="%s" STUB_OCI_STATE="%s" PYTHONPATH="%s" "%s" "$@"\n' \
    "${home_dir}" "${task_dir}/restart-state.json" "${task_dir}/stub" "${python_bin}" > "${SUITE_ADB_PYTHON}"
  chmod +x "${SUITE_ADB_PYTHON}"
  # shellcheck source=../suite-restart.sh
  source "${repo}/platform/deploy/suite-restart.sh"
  calls="${task_dir}/restart-calls.log"
  id() { printf '0\n'; }
  # root の分岐（runuser -u opc -- env HOME=… PATH=… timeout …）を通す。runuser は "-u <user> --" を外して実行する。
  runuser() { shift 3; "$@"; }
  suite_rebuild_and_restart() { printf 'rebuild\n' >> "${calls}"; }
  suite_restart_services() { printf 'restart-services\n' >> "${calls}"; }
  suite_check_deployed() { printf 'check\n' >> "${calls}"; }
  run_restart() {
    printf '%s\n' "$1" > "${task_dir}/restart-state.json"
    : > "${calls}"
    suite_restart_main "${@:2}" > "${task_dir}/restart-out.log" 2>&1
  }

  # OCI の認証が無い（~/.oci/config が無い）: warning を出して build まで進む。
  write_env "${SUITE_REPO_DIR}/platform" "PLATFORM_OCI_CONFIG_FILE=${task_dir}/missing/config"
  mv "${SUITE_REPO_DIR}/platform/platform.env" "${SUITE_PLATFORM_ENV_FILE}"
  run_restart "$(state_json '["STOPPED"]')" || fail "OCI の認証が無いと失敗にした: $(cat "${task_dir}/restart-out.log")"
  grep -qF "[suite-adb] NG: OCI の認証の設定ファイル（${task_dir}/missing/config）がありません" "${task_dir}/restart-out.log" \
    || fail "認証が無い理由: $(cat "${task_dir}/restart-out.log")"
  grep -qF "[suite-restart] WARNING: ADB を使える状態にできませんでした" "${task_dir}/restart-out.log" || fail "認証が無いときの warning"
  [ "$(tr '\n' ' ' < "${calls}")" = "rebuild check " ] || fail "認証が無いと build に進まない: $(cat "${calls}")"
  grep -q '"op": "start"' "${task_dir}/restart-state.json" && fail "認証が無いのに起動した"

  # ADB の起動が NG（起動の権限が無い 404）: warning を出して build まで進む（--no-build も同じ）。
  write_env "${SUITE_REPO_DIR}/platform"
  mv "${SUITE_REPO_DIR}/platform/platform.env" "${SUITE_PLATFORM_ENV_FILE}"
  for args in "" "--no-build"; do
    run_restart "$(state_json '["STOPPED"]' '"start_errors": [{"status": 404, "code": "NotAuthorizedOrNotFound"}]')" ${args:+"${args}"} \
      || fail "起動の NG で失敗にした（${args:-build}）: $(cat "${task_dir}/restart-out.log")"
    grep -qF "[suite-adb] NG: ADB を起動する権限がありません" "${task_dir}/restart-out.log" || fail "起動の NG の理由: $(cat "${task_dir}/restart-out.log")"
    grep -qF "[suite-restart] WARNING: ADB を使える状態にできませんでした" "${task_dir}/restart-out.log" || fail "起動の NG の warning"
    expected="rebuild"
    [ -z "${args}" ] || expected="restart-services"
    [ "$(tr '\n' ' ' < "${calls}")" = "${expected} check " ] || fail "起動の NG で続けない（${args:-build}）: $(cat "${calls}")"
  done

  # 時間切れ（STARTING のまま）: warning を出して build まで進む。
  SUITE_ADB_START_TIMEOUT_SECONDS=0.3
  run_restart "$(state_json '["STARTING"]')" || fail "時間切れで失敗にした: $(cat "${task_dir}/restart-out.log")"
  grep -qF "以内に AVAILABLE になりません" "${task_dir}/restart-out.log" && [ "$(tr '\n' ' ' < "${calls}")" = "rebuild check " ] \
    || fail "時間切れで続けない: $(cat "${task_dir}/restart-out.log")"
  SUITE_ADB_START_TIMEOUT_SECONDS=5

  # 認証があり STOPPED: 起動して AVAILABLE を待ってから build する（warning は出さない）。
  run_restart "$(state_json '["STOPPED", "STARTING", "AVAILABLE"]')" || fail "STOPPED から起動できない: $(cat "${task_dir}/restart-out.log")"
  grep -q '"op": "start"' "${task_dir}/restart-state.json" || fail "STOPPED なのに起動しない"
  grep -qF "[suite-adb] OK: ADB SUITEADB（ap-osaka-1） は AVAILABLE です。" "${task_dir}/restart-out.log" || fail "OK の行"
  if grep -qF "WARNING" "${task_dir}/restart-out.log"; then fail "OK なのに warning を出した"; fi
  [ "$(tr '\n' ' ' < "${calls}")" = "rebuild check " ] || fail "OK の後に build しない: $(cat "${calls}")"
)

echo "suite-adb: pass"
