#!/usr/bin/env bash
set -euo pipefail

TEST_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${TEST_SCRIPT_DIR}/../.." && pwd)"
UPDATE_SCRIPT="${REPO_DIR}/scripts/update-after-pull.sh"
TEST_TMP_DIR="$(mktemp -d)"
trap 'rm -rf -- "${TEST_TMP_DIR}"' EXIT

fail_test() {
  printf 'update-after-pull test failed: %s\n' "$*" >&2
  exit 1
}

assert_before() {
  local first="$1" second="$2" log_file="$3" first_line second_line
  first_line="$(grep -n -m1 -E -- "${first}" "${log_file}" | cut -d: -f1)"
  second_line="$(grep -n -m1 -E -- "${second}" "${log_file}" | cut -d: -f1)"
  [ "${first_line}" -lt "${second_line}" ] || \
    fail_test "expected '${first}' before '${second}' in ${log_file}"
}

assert_old_frontend() {
  grep -Fq 'old frontend' "$1/no.1-production-ready-suite/nl2sql/frontend/dist/index.html" || \
    fail_test "old frontend was not preserved for $1"
}

assert_degraded() {
  local command_log="$1" service
  for service in \
    production-ready-nl2sql-schema-refresh-worker.service \
    production-ready-nl2sql-quality-evaluation-worker.service \
    production-ready-nl2sql-ontology-worker.service; do
    grep -Fq "systemctl|disable --now ${service}" "${command_log}" || \
      fail_test "${service} was not disabled after a maintenance failure"
  done
  grep -Fq 'systemctl|start production-ready-nl2sql-backend.service' "${command_log}" || \
    fail_test "backend recovery was not attempted"
}

make_fake_commands() {
  local fake_bin="$1"
  mkdir -p "${fake_bin}"

  cat > "${fake_bin}/uv" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'uv|%s|%s\n' "${PWD}" "$*" >> "${COMMAND_LOG}"
case "$*" in
  "sync --locked --no-dev --python 3.12")
    [ "${FAIL_STAGE:-}" != "backend-sync" ]
    ;;
  *"compileall -q app"*)
    [ "${FAIL_STAGE:-}" != "backend-compile" ]
    ;;
  *"nl2sql_system_schema --initialize"*)
    [ "${FAIL_STAGE:-}" != "system-migration" ]
    ;;
  *"app_security_migrate --apply --skip-bootstrap"*)
    [ "${FAIL_STAGE:-}" != "security-migration" ]
    ;;
esac
EOF

  cat > "${fake_bin}/npm" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'npm|%s|%s\n' "${PWD}" "$*" >> "${COMMAND_LOG}"
if [ "${FAIL_STAGE:-}" = "frontend-build" ] && \
   [ "${PWD}" = "${APP_REPO_DIR}/frontend" ] && [ "${1:-}" = "run" ] && [ "${2:-}" = "build" ]; then
  exit 1
fi
if [ "${PWD}" = "${APP_REPO_DIR}/frontend" ] && [ "${1:-}" = "run" ] && [ "${2:-}" = "build" ]; then
  while [ "$#" -gt 0 ]; do
    if [ "$1" = "--outDir" ]; then
      shift
      mkdir -p "$1/assets"
      printf 'new frontend\n' > "$1/index.html"
      printf 'asset\n' > "$1/assets/app.js"
      break
    fi
    shift
  done
fi
EOF

  cat > "${fake_bin}/node" <<'EOF'
#!/usr/bin/env bash
printf 'v24.0.0\n'
EOF

  cat > "${fake_bin}/sudo" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'sudo|%s\n' "$*" >> "${COMMAND_LOG}"
if [ "${1:-}" = "-n" ] && [ "${2:-}" = "-v" ]; then
  exit 0
fi
filtered=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    -n|-H|--)
      shift
      ;;
    -u|-o|-g)
      shift 2
      ;;
    *)
      filtered+=("$1")
      shift
      ;;
    esac
done
if [ "${filtered[0]:-}" = "chown" ]; then
  exit 0
fi
exec "${filtered[@]}"
EOF

  cat > "${fake_bin}/systemctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'systemctl|%s\n' "$*" >> "${COMMAND_LOG}"
if [ "${1:-}" = "cat" ]; then
  printf '[Unit]\nDescription=%s\n' "${2:-test.service}"
fi
if [ "${1:-}" = "is-active" ] && [ "${2:-}" != "--quiet" ]; then
  printf 'active\n'
fi
if [ "${1:-}" = "restart" ] && [ "${FAIL_STAGE:-}" = "worker-restart" ] && \
   printf '%s\n' "$*" | grep -q 'worker'; then
  exit 1
fi
exit 0
EOF

  cat > "${fake_bin}/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'curl|%s\n' "$*" >> "${COMMAND_LOG}"
url="${*: -1}"
if [ "${FAIL_STAGE:-}" = "backend-health" ] && [ "${url}" = "${BACKEND_HEALTH_URL}" ]; then
  exit 1
fi
if [ "${FAIL_STAGE:-}" = "public-health" ] && [ "${url}" = "${PUBLIC_HEALTH_URL}" ]; then
  exit 1
fi
exit 0
EOF

  chmod +x "${fake_bin}/uv" "${fake_bin}/npm" "${fake_bin}/node" \
    "${fake_bin}/sudo" "${fake_bin}/systemctl" "${fake_bin}/curl"
}

make_case() {
  local scenario="$1"
  local case_dir="${TEST_TMP_DIR}/${scenario}"
  local app_dir="${case_dir}/no.1-production-ready-suite/nl2sql"
  local platform_dir="${case_dir}/no.1-production-ready-suite/platform"

  mkdir -p \
    "${app_dir}/backend/app" \
    "${app_dir}/frontend/dist/assets" \
    "${platform_dir}/packages/ui" \
    "${case_dir}/wallet/nested" \
    "${case_dir}/fake-bin"
  printf 'PLATFORM_AUTH_MODE=authenticated\n' > "${app_dir}/backend/.env"
  # 共通の設定（Wallet の場所を含む）は platform/.env（#211）。
  printf 'PLATFORM_ORACLE_WALLET_DIR=%s\n' "${case_dir}/wallet" > "${platform_dir}/.env"
  printf 'PLATFORM_ORACLE_PASSWORD=$(touch %s/must-not-run)\n' "${case_dir}" >> "${platform_dir}/.env"
  : > "${app_dir}/backend/pyproject.toml"
  : > "${app_dir}/backend/uv.lock"
  : > "${app_dir}/frontend/package.json"
  : > "${app_dir}/frontend/package-lock.json"
  : > "${platform_dir}/package.json"
  : > "${platform_dir}/package-lock.json"
  : > "${platform_dir}/packages/ui/package.json"
  printf 'wallet\n' > "${case_dir}/wallet/cwallet.sso"
  printf 'nested\n' > "${case_dir}/wallet/nested/tnsnames.ora"
  printf 'lock\n' > "${case_dir}/.wallet.install.lock"
  printf 'old frontend\n' > "${app_dir}/frontend/dist/index.html"
  printf 'old asset\n' > "${app_dir}/frontend/dist/assets/app.js"
  : > "${case_dir}/commands.log"
  chmod 0775 "${case_dir}"
  chmod 0700 "${case_dir}/wallet" "${case_dir}/wallet/nested"
  chmod 0600 \
    "${case_dir}/wallet/cwallet.sso" \
    "${case_dir}/wallet/nested/tnsnames.ora" \
    "${case_dir}/.wallet.install.lock" \
    "${app_dir}/backend/.env" \
    "${platform_dir}/.env"
  make_fake_commands "${case_dir}/fake-bin"
  printf '%s\n' "${case_dir}"
}

run_case() {
  local case_dir="$1" action="${2:-}" fail_stage="${3:-}"
  local app_dir="${case_dir}/no.1-production-ready-suite/nl2sql"
  local platform_dir="${case_dir}/no.1-production-ready-suite/platform"
  local args=()
  [ -z "${action}" ] || args+=("${action}")

  env \
    PATH="${case_dir}/fake-bin:${PATH}" \
    UPDATE_AFTER_PULL_TEST_MODE=true \
    APP_REPO_DIR="${app_dir}" \
    PLATFORM_REPO_DIR="${platform_dir}" \
    APP_USER="$(id -un)" \
    APP_GROUP="$(id -gn)" \
    WALLET_DIR="${case_dir}/wallet" \
    RECOVERY_ROOT="${case_dir}/recovery" \
    UPDATE_LOG_PATH="${case_dir}/update.log" \
    COMMAND_LOG="${case_dir}/commands.log" \
    FAIL_STAGE="${fail_stage}" \
    LOCK_FILE="${case_dir}/update.lock" \
    BACKEND_HEALTH_URL="http://backend.test/api/health" \
    PUBLIC_HEALTH_URL="http://public.test/api/health" \
    HEALTHCHECK_TIMEOUT_SECONDS=0 \
    HEALTHCHECK_INTERVAL_SECONDS=0 \
    "${UPDATE_SCRIPT}" "${args[@]}"
}

test_check_dispatch_is_non_mutating() (
  local case_dir="${TEST_TMP_DIR}/check-dispatch"
  mkdir -p "${case_dir}"
  export UPDATE_AFTER_PULL_TEST_MODE=true
  # shellcheck source=/dev/null
  source "${UPDATE_SCRIPT}"
  preflight() { printf '%s\n' preflight >> "${case_dir}/events"; }
  report_deployment_state() { printf '%s\n' report >> "${case_dir}/events"; }
  check_system_schema_status() { printf '%s\n' status >> "${case_dir}/events"; }
  verify_wallet_permissions() { printf '%s\n' verify >> "${case_dir}/events"; }
  acquire_lock() { printf '%s\n' forbidden-lock >> "${case_dir}/events"; return 1; }
  setup_update_log() { printf '%s\n' forbidden-log >> "${case_dir}/events"; return 1; }
  create_recovery_snapshot() { printf '%s\n' forbidden-backup >> "${case_dir}/events"; return 1; }
  enter_maintenance() { printf '%s\n' forbidden-stop >> "${case_dir}/events"; return 1; }
  repair_wallet_permissions() { printf '%s\n' forbidden-repair >> "${case_dir}/events"; return 1; }
  run_database_migrations() { printf '%s\n' forbidden-migration >> "${case_dir}/events"; return 1; }

  main --check
  grep -Fxq preflight "${case_dir}/events"
  grep -Fxq report "${case_dir}/events"
  grep -Fxq status "${case_dir}/events"
  grep -Fxq verify "${case_dir}/events"
  ! grep -q '^forbidden-' "${case_dir}/events"
)

test_wallet_env_parser_does_not_source_secrets() (
  local case_dir="${TEST_TMP_DIR}/env-parser"
  mkdir -p "${case_dir}"
  export UPDATE_AFTER_PULL_TEST_MODE=true
  # shellcheck source=/dev/null
  source "${UPDATE_SCRIPT}"
  PLATFORM_ENV_FILE="${case_dir}/platform.env"
  printf 'PLATFORM_ORACLE_PASSWORD=$(touch %s/must-not-run)\n' "${case_dir}" > "${PLATFORM_ENV_FILE}"
  printf '%s\n' 'PLATFORM_ORACLE_WALLET_DIR="/u01/aipoc/wallet"' >> "${PLATFORM_ENV_FILE}"
  test "$(wallet_env_value)" = "/u01/aipoc/wallet"
  test ! -e "${case_dir}/must-not-run"
)

test_noninteractive_privilege_failure() (
  local case_dir="${TEST_TMP_DIR}/noninteractive-privilege"
  mkdir -p "${case_dir}"
  export UPDATE_AFTER_PULL_TEST_MODE=true
  # shellcheck source=/dev/null
  source "${UPDATE_SCRIPT}"
  running_as_root() { return 1; }
  sudo() {
    printf 'sudo|%s\n' "$*" >> "${case_dir}/events"
    return 1
  }

  if ensure_privileged_access; then
    fail_test "interactive sudo requirement was accepted"
  fi
  grep -Fxq 'sudo|-n -v' "${case_dir}/events"
)

test_passwordless_sudo_reexecs_update_actions() (
  local case_dir="${TEST_TMP_DIR}/passwordless-reexec"
  mkdir -p "${case_dir}"
  export UPDATE_AFTER_PULL_TEST_MODE=true
  export APP_ROOT="${case_dir}"
  export APP_REPO_DIR="${case_dir}/no.1-production-ready-suite/nl2sql"
  export PLATFORM_REPO_DIR="${case_dir}/no.1-production-ready-suite/platform"
  export WALLET_DIR="${case_dir}/wallet"
  export RECOVERY_ROOT="${case_dir}/recovery"
  export UPDATE_LOG_PATH="${case_dir}/update.log"
  export LOCK_FILE="${case_dir}/update.lock"
  export SECRET_SHOULD_NOT_LEAK="do-not-forward"
  # shellcheck source=/dev/null
  source "${UPDATE_SCRIPT}"
  running_as_root() { return 1; }
  sudo() {
    printf 'sudo|%s\n' "$*" >> "${case_dir}/events"
    [ "$*" = "-n -v" ]
  }
  exec() {
    printf 'exec|%s\n' "$*" >> "${case_dir}/events"
    return 77
  }

  ACTION="repair-only"
  set +e
  reexec_with_sudo_if_needed --repair-only >"${case_dir}/stdout" 2>"${case_dir}/stderr"
  local status=$?
  set -e
  [ "${status}" -ne 0 ]
  grep -Fxq 'sudo|-n -v' "${case_dir}/events"
  grep -Fq 'exec|sudo -n env' "${case_dir}/events"
  grep -Fq "APP_REPO_DIR=${APP_REPO_DIR}" "${case_dir}/events"
  grep -Fq "PLATFORM_REPO_DIR=${PLATFORM_REPO_DIR}" "${case_dir}/events"
  grep -Fq "UPDATE_AFTER_PULL_TEST_MODE=true" "${case_dir}/events"
  grep -Fq "NL2SQL_UPDATE_SUDO_REEXECED=true" "${case_dir}/events"
  grep -Fq "${SCRIPT_PATH} --repair-only" "${case_dir}/events"
  if grep -Fq "SECRET_SHOULD_NOT_LEAK" "${case_dir}/events"; then
    fail_test "unexpected environment variable was forwarded to sudo reexec"
  fi
)

test_auto_sudo_failure_does_not_continue() (
  local case_dir="${TEST_TMP_DIR}/auto-sudo-failure"
  mkdir -p "${case_dir}"
  export UPDATE_AFTER_PULL_TEST_MODE=true
  # shellcheck source=/dev/null
  source "${UPDATE_SCRIPT}"
  running_as_root() { return 1; }
  sudo() {
    printf 'sudo|%s\n' "$*" >> "${case_dir}/events"
    return 1
  }
  preflight() {
    printf '%s\n' preflight >> "${case_dir}/events"
  }

  set +e
  local status
  if main --repair-only >"${case_dir}/stdout" 2>"${case_dir}/stderr"; then
    status=0
  else
    status=$?
  fi
  set -e
  [ "${status}" -ne 0 ]
  grep -Fxq 'sudo|-n -v' "${case_dir}/events"
  grep -Fq 'passwordless sudo が利用できません' "${case_dir}/stderr"
  if grep -Fxq preflight "${case_dir}/events"; then
    fail_test "auto sudo failure reached preflight"
  fi
)

test_root_privilege_dispatch() (
  local case_dir="${TEST_TMP_DIR}/root-privilege"
  mkdir -p "${case_dir}"
  export UPDATE_AFTER_PULL_TEST_MODE=true
  # shellcheck source=/dev/null
  source "${UPDATE_SCRIPT}"
  running_as_root() { return 0; }
  record_command() { printf 'command|%s\n' "$*" >> "${case_dir}/events"; }
  sudo() { printf 'sudo|%s\n' "$*" >> "${case_dir}/events"; }

  run_privileged record_command privileged
  grep -Fxq 'command|privileged' "${case_dir}/events"
  if grep -q '^sudo|' "${case_dir}/events"; then
    fail_test "root privileged command unexpectedly called sudo"
  fi

  APP_USER=deployment-user
  run_as_app_user true
  grep -Fxq "sudo|-n -H -u deployment-user -- env PATH=${PATH} true" "${case_dir}/events"
)

test_root_lock_owner_transition() (
  local case_dir="${TEST_TMP_DIR}/root-lock"
  mkdir -p "${case_dir}"
  export UPDATE_AFTER_PULL_TEST_MODE=true
  # shellcheck source=/dev/null
  source "${UPDATE_SCRIPT}"
  running_as_root() { return 0; }
  chown() { printf 'chown|%s\n' "$*" >> "${case_dir}/events"; }
  chmod() {
    printf 'chmod|%s\n' "$*" >> "${case_dir}/events"
    command chmod "$@"
  }
  APP_USER="$(id -un)"
  APP_GROUP="$(id -gn)"
  LOCK_FILE="${case_dir}/update.lock"

  acquire_lock
  assert_before '^chown\|root:root ' "^chown\|${APP_USER}:${APP_GROUP} " \
    "${case_dir}/events"
  test "$(stat -c %a "${LOCK_FILE}")" = 600
  flock -u 9
  exec 9>&-
)

test_health_retry_logging() (
  local label="$1" scenario="$2"
  local case_dir="${TEST_TMP_DIR}/health-${label}-${scenario}"
  mkdir -p "${case_dir}"
  # shellcheck source=/dev/null
  source "${UPDATE_SCRIPT}"
  HEALTHCHECK_TIMEOUT_SECONDS=0
  HEALTHCHECK_INTERVAL_SECONDS=0
  if [ "${scenario}" = "recovering" ]; then
    HEALTHCHECK_TIMEOUT_SECONDS=10
  fi
  printf '0\n' > "${case_dir}/attempts"
  curl() {
    local attempts
    attempts="$(cat "${case_dir}/attempts")"
    attempts=$((attempts + 1))
    printf '%s\n' "${attempts}" > "${case_dir}/attempts"
    printf 'health-response-body\n'
    case "${scenario}" in
      immediate) return 0 ;;
      recovering)
        if [ "${attempts}" -gt 2 ]; then return 0; fi
        printf 'curl: (7) Failed to connect\n' >&2
        return 7
        ;;
      refused)
        printf 'curl: (7) Failed to connect\n' >&2
        return 7
        ;;
      http-error)
        printf 'curl: (22) The requested URL returned error: 503\n' >&2
        return 22
        ;;
      request-timeout)
        printf 'curl: (28) Operation timed out\n' >&2
        return 28
        ;;
    esac
  }

  local status=0
  wait_for_health "${label}" "http://${label}.test/api/health" \
    >"${case_dir}/output" 2>&1 || status=$?
  if grep -Fq 'health-response-body' "${case_dir}/output"; then
    fail_test "${label}/${scenario} leaked the health response body"
  fi
  grep -Fq "${label} health を待機します" "${case_dir}/output"
  case "${scenario}" in
    immediate|recovering)
      [ "${status}" -eq 0 ] || fail_test "${label}/${scenario} did not recover"
      grep -Fq "${label} health を確認しました" "${case_dir}/output"
      if grep -Eq 'curl:|WARNING:|ERROR:' "${case_dir}/output"; then
        fail_test "${label}/${scenario} leaked a transient health error"
      fi
      local expected_attempts=1
      if [ "${scenario}" = "recovering" ]; then expected_attempts=3; fi
      [ "$(cat "${case_dir}/attempts")" -eq "${expected_attempts}" ]
      ;;
    *)
      [ "${status}" -ne 0 ] || fail_test "${label}/${scenario} unexpectedly succeeded"
      grep -Fq "${label} health が 0 秒以内に成功しませんでした" "${case_dir}/output"
      local expected_code=7 expected_error='Failed to connect'
      case "${scenario}" in
        http-error) expected_code=22; expected_error='503' ;;
        request-timeout) expected_code=28; expected_error='Operation timed out' ;;
      esac
      grep -Fq "curl exit=${expected_code}" "${case_dir}/output"
      grep -Fq "${expected_error}" "${case_dir}/output"
      [ "$(grep -Fc 'curl:' "${case_dir}/output")" -eq 1 ]
      if grep -Fq 'health を確認しました' "${case_dir}/output"; then
        fail_test "${label}/${scenario} reported success on timeout"
      fi
      ;;
  esac
)

for health_label in backend public; do
  for health_scenario in immediate recovering refused http-error request-timeout; do
    test_health_retry_logging "${health_label}" "${health_scenario}"
  done
done

test_check_dispatch_is_non_mutating
test_wallet_env_parser_does_not_source_secrets
test_noninteractive_privilege_failure
test_passwordless_sudo_reexecs_update_actions
test_auto_sudo_failure_does_not_continue
test_root_privilege_dispatch
test_root_lock_owner_transition

check_case="$(make_case check)"
run_case "${check_case}" --check
check_log="${check_case}/commands.log"
test ! -e "${check_case}/update.log"
test ! -e "${check_case}/recovery"
if grep -q '^sudo|' "${check_log}"; then
  fail_test "--check called sudo"
fi
if grep -Eq '^systemctl\|(stop|start|restart|enable|disable)' "${check_log}"; then
  fail_test "--check mutated systemd"
fi
if grep -Eq 'nl2sql_system_schema --initialize|app_security_migrate' "${check_log}"; then
  fail_test "--check ran a migration"
fi

repair_case="$(make_case repair-only)"
run_case "${repair_case}" --repair-only
repair_log="${repair_case}/commands.log"
assert_old_frontend "${repair_case}"
if grep -q '^npm|' "${repair_log}" || grep -Eq '^uv\|.*(sync --locked|compileall)' "${repair_log}"; then
  fail_test "--repair-only ran a build step"
fi
grep -Fq 'uv|' "${repair_log}"
grep -Fq 'nl2sql_system_schema --initialize' "${repair_log}"
grep -Fq 'app_security_migrate --apply --skip-bootstrap' "${repair_log}"
grep -Fq 'curl|-fsS --max-time 5 http://public.test/api/health' "${repair_log}"
test -f "$(find "${repair_case}/recovery" -name backend.env -print -quit)"
test -f "$(find "${repair_case}/recovery" -name platform.env -print -quit)"
test "$(stat -c %a "${repair_case}")" = 775
test "$(stat -c %a "${repair_case}/wallet")" = 700
test "$(stat -c %a "${repair_case}/wallet/cwallet.sso")" = 600
test "$(stat -c %a "${repair_case}/.wallet.install.lock")" = 600
test "$(stat -c %a "${repair_case}/no.1-production-ready-suite/nl2sql/backend/.env")" = 600
test "$(stat -c %a "${repair_case}/no.1-production-ready-suite/platform/.env")" = 600
test ! -e "${repair_case}/must-not-run"

success_case="$(make_case success)"
run_case "${success_case}"
success_log="${success_case}/commands.log"
grep -Fq 'new frontend' "${success_case}/no.1-production-ready-suite/nl2sql/frontend/dist/index.html"
grep -Fq 'systemctl|enable production-ready-nl2sql-backend.service' "${success_log}"
grep -Fq 'systemctl|restart production-ready-nl2sql-backend.service' "${success_log}"
grep -Fq 'systemctl|restart production-ready-nl2sql-schema-refresh-worker.service production-ready-nl2sql-quality-evaluation-worker.service production-ready-nl2sql-ontology-worker.service' "${success_log}"
grep -Fq 'curl|-fsS --max-time 5 http://backend.test/api/health' "${success_log}"
grep -Fq 'curl|-fsS --max-time 5 http://public.test/api/health' "${success_log}"
assert_before '^uv\|.*sync --locked --no-dev' '^uv\|.*compileall -q app' "${success_log}"
assert_before 'compileall -q app' '^npm\|.*no.1-production-ready-suite/platform.*\|ci$' "${success_log}"
assert_before '^npm\|.*no.1-production-ready-suite/nl2sql/frontend.*run build' '^sudo\|-n -- mktemp -d .*/recovery/' "${success_log}"
assert_before '^sudo\|-n -- mktemp -d .*/recovery/' '^systemctl\|stop .*worker' "${success_log}"
assert_before '^systemctl\|stop production-ready-nl2sql-backend.service' \
  "^sudo\\|-n -- chown root:$(id -gn) ${success_case}$" "${success_log}"
assert_before 'nl2sql_system_schema --initialize' 'systemctl\|restart production-ready-nl2sql-backend.service' "${success_log}"
assert_before 'curl\|.*backend.test' 'systemctl\|restart .*worker' "${success_log}"
assert_before 'systemctl\|is-active --quiet production-ready-nl2sql-ontology-worker.service' 'curl\|.*public.test' "${success_log}"
test ! -e "${success_case}/must-not-run"

for failure_stage in backend-sync frontend-build; do
  failure_case="$(make_case "${failure_stage}")"
  if run_case "${failure_case}" "" "${failure_stage}"; then
    fail_test "${failure_stage} unexpectedly succeeded"
  fi
  assert_old_frontend "${failure_case}"
  if grep -Eq '^systemctl\|(stop|start|restart|enable|disable)' "${failure_case}/commands.log"; then
    fail_test "${failure_stage} entered the maintenance window"
  fi
done

for failure_stage in system-migration security-migration backend-health worker-restart; do
  failure_case="$(make_case "${failure_stage}")"
  if run_case "${failure_case}" "" "${failure_stage}"; then
    fail_test "${failure_stage} unexpectedly succeeded"
  fi
  assert_old_frontend "${failure_case}"
  assert_degraded "${failure_case}/commands.log"
done

public_health_case="$(make_case public-health)"
if run_case "${public_health_case}" "" public-health; then
  fail_test "public-health unexpectedly succeeded"
fi
assert_old_frontend "${public_health_case}"
if grep -Fq 'systemctl|disable --now' "${public_health_case}/commands.log"; then
  fail_test "public health failure disabled healthy workers"
fi
grep -Fq 'systemctl|restart production-ready-nl2sql-schema-refresh-worker.service production-ready-nl2sql-quality-evaluation-worker.service production-ready-nl2sql-ontology-worker.service' \
  "${public_health_case}/commands.log"

wallet_override_case="$(make_case wallet-override)"
printf 'PLATFORM_ORACLE_WALLET_DIR=%s/other-wallet\n' "${wallet_override_case}" > \
  "${wallet_override_case}/no.1-production-ready-suite/platform/.env"
printf 'PLATFORM_ORACLE_PASSWORD=$(touch %s/must-not-run)\n' "${wallet_override_case}" >> \
  "${wallet_override_case}/no.1-production-ready-suite/platform/.env"
chmod 0600 "${wallet_override_case}/no.1-production-ready-suite/platform/.env"
if run_case "${wallet_override_case}" --check; then
  fail_test "an overridden Wallet path was accepted"
fi
test ! -e "${wallet_override_case}/must-not-run"
if grep -q '^sudo|' "${wallet_override_case}/commands.log"; then
  fail_test "invalid Wallet path caused a mutation"
fi

grep -Fq '[ "${WALLET_DIR}" = "/u01/aipoc/wallet" ]' "${UPDATE_SCRIPT}"
grep -Fq 'run_privileged chown "root:${APP_GROUP}" "${APP_ROOT}"' "${UPDATE_SCRIPT}"
grep -Fq 'chmod 0775 "${APP_ROOT}"' "${UPDATE_SCRIPT}"
grep -Fq 'find "${WALLET_DIR}" -type f -exec chmod 0600 {} +' "${UPDATE_SCRIPT}"
grep -Fq 'app.cli.nl2sql_system_schema --initialize' "${UPDATE_SCRIPT}"
grep -Fq 'app.cli.app_security_migrate' "${UPDATE_SCRIPT}"
grep -Fq -- '--apply --skip-bootstrap' "${UPDATE_SCRIPT}"
if grep -Eq '(^|[[:space:]])git([[:space:]]|$)' "${UPDATE_SCRIPT}"; then
  fail_test "update script contains a Git command"
fi
if grep -Eq 'app\.cli\.nl2sql_system_schema[[:space:]]+--recreate' "${UPDATE_SCRIPT}"; then
  fail_test "update script invokes destructive schema recreation"
fi

echo "unified update and deployed OCI repair behavior verified."
