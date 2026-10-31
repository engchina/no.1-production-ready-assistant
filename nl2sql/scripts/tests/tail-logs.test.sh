#!/usr/bin/env bash
set -euo pipefail

TEST_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${TEST_SCRIPT_DIR}/../.." && pwd)"
TAIL_SCRIPT="${REPO_DIR}/scripts/tail-logs.sh"
TEST_TMP_DIR="$(mktemp -d)"
trap 'rm -rf -- "${TEST_TMP_DIR}"' EXIT

BACKEND_UNIT="production-ready-nl2sql-backend.service"
SCHEMA_UNIT="production-ready-nl2sql-schema-refresh-worker.service"
SYNTHETIC_UNIT="production-ready-nl2sql-synthetic-worker.service"
QUALITY_UNIT="production-ready-nl2sql-quality-evaluation-worker.service"
ONTOLOGY_UNIT="production-ready-nl2sql-ontology-worker.service"

fail_test() {
  printf 'tail-logs test failed: %s\n' "$*" >&2
  exit 1
}

assert_equals() {
  [ "$1" = "$2" ] || fail_test "$3 (expected='$1' actual='$2')"
}

assert_contains() {
  printf '%s' "$1" | grep -Fq -- "$2" || fail_test "$3 (missing='$2' in='$1')"
}

assert_not_contains() {
  printf '%s' "$1" | grep -Fq -- "$2" && fail_test "$3 (unexpected='$2' in='$1')"
  return 0
}

# 引数を解析し、確定した追尾対象と表示設定を機械可読な形で出力する。
parse_case() (
  export TAIL_LOGS_TEST_MODE=true
  # shellcheck source=/dev/null
  source "${TAIL_SCRIPT}"
  parse_args "$@"
  resolve_sources
  printf 'action=%s\n' "${ACTION}"
  printf 'units=%s\n' "${SELECTED_UNITS[*]-}"
  printf 'files=%s\n' "${SELECTED_FILES[*]-}"
  printf 'follow=%s\n' "${FOLLOW}"
  printf 'lines=%s\n' "${LINES}"
  printf 'show_unit=%s\n' "${SHOW_UNIT}"
)

# journalctl へ渡す引数を "|" 区切りで出力する。
journal_args_case() (
  export TAIL_LOGS_TEST_MODE=true
  # shellcheck source=/dev/null
  source "${TAIL_SCRIPT}"
  parse_args "$@"
  resolve_sources
  RAW="${RAW}"
  PYTHON_BIN="python3"
  [ "${RAW}" = true ] && PYTHON_BIN=""
  build_journal_args
  local IFS='|'
  printf '%s\n' "${JOURNAL_ARGS[*]}"
)

format_case() (
  export TAIL_LOGS_TEST_MODE=true
  local min_level="$1" show_unit="$2"
  shift 2
  # shellcheck source=/dev/null
  source "${TAIL_SCRIPT}"
  MIN_LEVEL="${min_level}"
  SHOW_UNIT="${show_unit}"
  COLOR_ENABLED=false
  PYTHON_BIN="python3"
  printf '%s\n' "$@" | format_stream
)

run_script() (
  export TAIL_LOGS_TEST_MODE=false
  "${TAIL_SCRIPT}" "$@"
)

make_fake_commands() {
  local fake_bin="$1"
  mkdir -p "${fake_bin}"

  cat > "${fake_bin}/systemctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "is-active" ]; then
  case "${2:-}" in
    *ontology-worker.service) printf 'failed\n'; exit 3 ;;
    *) printf 'active\n' ;;
  esac
  exit 0
fi
exit 0
EOF

  cat > "${fake_bin}/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
for arg in "$@"; do
  case "${arg}" in
    *127.0.0.1:8010*) printf '200'; exit 0 ;;
    https://127.0.0.1:443/nl2sql/health) printf '000'; exit 7 ;;
  esac
done
printf '000'
exit 7
EOF

  chmod 755 "${fake_bin}/systemctl" "${fake_bin}/curl"
}

# --- 追尾対象の解決 -----------------------------------------------------------

result="$(parse_case)"
assert_contains "${result}" "units=${BACKEND_UNIT} ${SCHEMA_UNIT} ${SYNTHETIC_UNIT} ${QUALITY_UNIT} ${ONTOLOGY_UNIT}" \
  "引数なしでアプリ 5 unit が既定にならない"
assert_contains "${result}" "files=" "引数なしでファイルが対象に入っている"
assert_contains "${result}" "follow=true" "引数なしで追尾が無効になっている"
assert_contains "${result}" "lines=200" "既定行数が 200 ではない"
assert_contains "${result}" "show_unit=true" "複数 unit 追尾で unit 列が出ない"

result="$(parse_case --backend)"
assert_contains "${result}" "units=${BACKEND_UNIT}" "--backend で backend unit のみにならない"
assert_contains "${result}" "show_unit=false" "単一 unit なのに unit 列が出る"

result="$(parse_case --workers)"
assert_contains "${result}" "units=${SCHEMA_UNIT} ${SYNTHETIC_UNIT} ${QUALITY_UNIT} ${ONTOLOGY_UNIT}" \
  "--workers で worker 4 unit にならない"
assert_not_contains "${result}" "${BACKEND_UNIT}" "--workers に backend が混ざっている"

result="$(parse_case --unit ontology --unit backend --unit "${SCHEMA_UNIT}")"
assert_contains "${result}" "units=${ONTOLOGY_UNIT} ${BACKEND_UNIT} ${SCHEMA_UNIT}" \
  "--unit の短縮名・完全名の解決に失敗している"

result="$(parse_case --unit backend --unit backend --backend)"
assert_contains "${result}" "units=${BACKEND_UNIT}" "重複指定した unit が排除されていない"

result="$(NGINX_ACCESS_LOG=/tmp/a.log NGINX_ERROR_LOG=/tmp/e.log \
  INIT_LOG_PATH=/tmp/i.log UPDATE_LOG_PATH=/tmp/u.log parse_case --all)"
assert_contains "${result}" "units=${BACKEND_UNIT} ${SCHEMA_UNIT} ${SYNTHETIC_UNIT} ${QUALITY_UNIT} ${ONTOLOGY_UNIT}" \
  "--all にアプリ 5 unit が含まれない"
assert_contains "${result}" "files=nginx-access=/tmp/a.log nginx-error=/tmp/e.log init=/tmp/i.log update=/tmp/u.log" \
  "--all に Nginx / init / update ログが含まれない"

result="$(INIT_LOG_PATH=/tmp/i.log parse_case --init)"
assert_contains "${result}" "units=" "--init 単独指定なのに unit が追尾対象に入っている"
assert_contains "${result}" "files=init=/tmp/i.log" "--init でファイルが対象にならない"

result="$(NGINX_ACCESS_LOG=/tmp/a.log NGINX_ERROR_LOG=/tmp/e.log parse_case --nginx --backend)"
assert_contains "${result}" "units=${BACKEND_UNIT}" "--nginx と --backend の併用で unit が失われる"
assert_contains "${result}" "files=nginx-access=/tmp/a.log nginx-error=/tmp/e.log" \
  "--nginx と --backend の併用でファイルが失われる"

# --- journalctl 引数の組み立て -------------------------------------------------

result="$(journal_args_case)"
assert_equals "--no-pager|-n|200|-u|${BACKEND_UNIT}|-u|${SCHEMA_UNIT}|-u|${SYNTHETIC_UNIT}|-u|${QUALITY_UNIT}|-u|${ONTOLOGY_UNIT}|-o|json|-f" \
  "${result}" "既定の journalctl 引数が想定と異なる"

result="$(journal_args_case --backend --no-follow --since "-15 min" -n 50 --grep boom)"
assert_equals "--no-pager|-n|50|-u|${BACKEND_UNIT}|--since|-15 min|-g|boom|-o|json" \
  "${result}" "--no-follow / --since / --lines / --grep が反映されていない"

result="$(journal_args_case --backend --raw)"
assert_contains "${result}" "-o|short-iso" "--raw で生出力形式にならない"

# --- 引数エラー ---------------------------------------------------------------

status=0
output="$(run_script --help)" || status=$?
assert_equals "0" "${status}" "--help が異常終了した"
assert_contains "${output}" "Usage: ./scripts/tail-logs.sh" "--help に usage が出ない"
assert_contains "${output}" "--status" "--help に --status の説明が無い"

for bad_args in "--nope" "--lines abc" "--level bogus" "--unit unknown-unit" "--unit"; do
  status=0
  # shellcheck disable=SC2086
  run_script ${bad_args} >/dev/null 2>&1 || status=$?
  assert_equals "2" "${status}" "不正な引数 '${bad_args}' で exit 2 にならない"
done

# --- JSON 整形フィルタ ---------------------------------------------------------

app_error='{"_SYSTEMD_UNIT":"'"${BACKEND_UNIT}"'","MESSAGE":"{\"timestamp\":\"2026-08-31 15:08:12,123\",\"level\":\"ERROR\",\"name\":\"app.api\",\"message\":\"boom\",\"request_id\":\"abc\"}"}'
app_info='{"_SYSTEMD_UNIT":"'"${BACKEND_UNIT}"'","MESSAGE":"{\"timestamp\":\"2026-08-31 15:08:13\",\"level\":\"INFO\",\"name\":\"app.db\",\"message\":\"connected\"}"}'
plain_line='{"_SYSTEMD_UNIT":"'"${ONTOLOGY_UNIT}"'","MESSAGE":"Traceback (most recent call last):"}'

result="$(format_case "" false "${app_error}")"
assert_contains "${result}" "2026-09-01T00:08:12.123+09:00" "整形後にタイムスタンプが出ない"
assert_contains "${result}" "ERROR" "整形後に level が出ない"
assert_contains "${result}" "app.api" "整形後に logger 名が出ない"
assert_contains "${result}" "boom" "整形後に message が出ない"
assert_contains "${result}" 'request_id="abc"' "整形後に追加フィールドが出ない"
assert_not_contains "${result}" '{"timestamp"' "整形されず生の JSON が残っている"

result="$(format_case "" true "${app_error}")"
assert_contains "${result}" "backend" "複数 unit 追尾時に unit 列が出ない"

result="$(format_case "" false "${plain_line}")"
assert_equals "Traceback (most recent call last):" "${result}" "非 JSON 行がそのまま通過しない"

result="$(format_case ERROR false "${app_info}" "${plain_line}" "${app_error}")"
assert_not_contains "${result}" "connected" "--level ERROR で INFO 行が落ちていない"
assert_contains "${result}" "Traceback" "--level ERROR で解析できない行まで落ちている"
assert_contains "${result}" "boom" "--level ERROR で ERROR 行まで落ちている"

# --- --status -----------------------------------------------------------------

FAKE_BIN="${TEST_TMP_DIR}/bin"
make_fake_commands "${FAKE_BIN}"
result="$(PATH="${FAKE_BIN}:${PATH}" run_script --status)"
assert_contains "${result}" "${BACKEND_UNIT}" "--status に backend unit が出ない"
assert_contains "${result}" "${ONTOLOGY_UNIT}" "--status に ontology worker が出ない"
assert_contains "${result}" "active" "--status に稼働状態が出ない"
assert_contains "${result}" "failed" "--status に停止中 unit の状態が出ない"
assert_contains "${result}" "http://127.0.0.1:8010/api/health" "--status に backend ヘルス URL が出ない"
assert_contains "${result}" "200" "--status にヘルスチェックの HTTP status が出ない"
assert_contains "${result}" "unreachable" "--status で到達不可の URL が表示されない"

# --- 存在しないログファイル ----------------------------------------------------

result="$(
  export TAIL_LOGS_TEST_MODE=true
  # shellcheck source=/dev/null
  source "${TAIL_SCRIPT}"
  GREP_PATTERN=""
  FOLLOW=false
  start_file_stream "missing" "${TEST_TMP_DIR}/does-not-exist.log" 2>&1
  printf 'pids=%s\n' "${#PIDS[@]}"
)"
assert_contains "${result}" "WARNING" "存在しないログファイルで警告が出ない"
assert_contains "${result}" "pids=0" "存在しないログファイルが追尾対象に加えられている"

printf 'tail-logs tests passed\n'
