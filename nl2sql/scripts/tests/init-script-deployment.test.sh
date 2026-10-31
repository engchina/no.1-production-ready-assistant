#!/usr/bin/env bash
set -euo pipefail

TEST_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${TEST_SCRIPT_DIR}/../.." && pwd)"
TEST_TMP_DIR="$(mktemp -d)"
trap 'rm -rf -- "${TEST_TMP_DIR}"' EXIT

run_initialization_case() (
  local scenario="$1"
  local fail_pattern="$2"
  local case_dir="${TEST_TMP_DIR}/${scenario}"
  mkdir -p "${case_dir}/app/no.1-production-ready-suite/nl2sql/backend"
  export APP_ROOT="${case_dir}/app"
  export NL2SQL_INIT_TEST_MODE=true
  export APPLICATION_PORT=80
  # shellcheck source=/dev/null
  source "${REPO_DIR}/init_script.sh"

  retry_command() {
    local attempts="$1"
    shift
    printf '%s | attempts=%s\n' "$*" "${attempts}" >> "${case_dir}/commands.log"
    if [ -n "${fail_pattern}" ] && printf '%s\n' "$*" | grep -q "${fail_pattern}"; then
      return 1
    fi
    return 0
  }

  initialize_database_schema
  printf '%s\n' "${DATABASE_INITIALIZATION_READY}" > "${case_dir}/ready"
)

run_systemd_case() (
  local scenario="$1"
  local ready="$2"
  local case_dir="${TEST_TMP_DIR}/${scenario}"
  mkdir -p "${case_dir}/app/no.1-production-ready-suite/nl2sql/backend" "${case_dir}/units"
  export APP_ROOT="${case_dir}/app"
  export NL2SQL_INIT_TEST_MODE=true
  export APPLICATION_PORT=80
  export SYSTEMD_UNIT_DIR="${case_dir}/units"
  # shellcheck source=/dev/null
  source "${REPO_DIR}/init_script.sh"

  systemctl() {
    printf '%s\n' "$*" >> "${case_dir}/systemctl.log"
  }

  DATABASE_INITIALIZATION_READY="${ready}"
  configure_systemd
)

run_initialization_case success ""
test "$(cat "${TEST_TMP_DIR}/success/ready")" = "true"
grep -q 'nl2sql_system_schema --initialize' "${TEST_TMP_DIR}/success/commands.log"
grep -q 'app_security_migrate --apply --skip-bootstrap' "${TEST_TMP_DIR}/success/commands.log"
test "$(grep -n 'nl2sql_system_schema' "${TEST_TMP_DIR}/success/commands.log" | cut -d: -f1)" -lt \
  "$(grep -n 'app_security_migrate' "${TEST_TMP_DIR}/success/commands.log" | cut -d: -f1)"

run_initialization_case degraded-security app_security_migrate
test "$(cat "${TEST_TMP_DIR}/degraded-security/ready")" = "false"

run_initialization_case degraded-system nl2sql_system_schema
test "$(cat "${TEST_TMP_DIR}/degraded-system/ready")" = "false"

run_systemd_case workers-enabled true
grep -q '^enable --now production-ready-nl2sql-schema-refresh-worker.service production-ready-nl2sql-synthetic-worker.service production-ready-nl2sql-quality-evaluation-worker.service production-ready-nl2sql-ontology-worker.service$' \
  "${TEST_TMP_DIR}/workers-enabled/systemctl.log"
if grep -q '^disable --now .*worker' "${TEST_TMP_DIR}/workers-enabled/systemctl.log"; then
  echo "初期化成功時に worker が disable されました。" >&2
  exit 1
fi

run_systemd_case workers-degraded false
grep -q '^disable --now production-ready-nl2sql-schema-refresh-worker.service production-ready-nl2sql-synthetic-worker.service production-ready-nl2sql-quality-evaluation-worker.service production-ready-nl2sql-ontology-worker.service$' \
  "${TEST_TMP_DIR}/workers-degraded/systemctl.log"
if grep -q '^enable --now .*worker' "${TEST_TMP_DIR}/workers-degraded/systemctl.log"; then
  echo "degraded mode で worker が enable されました。" >&2
  exit 1
fi

grep -Fq 'WALLET_DIR="${APP_ROOT}/wallet"' "${REPO_DIR}/init_script.sh"
grep -Fq 'chown "root:${APP_GROUP}" "${APP_ROOT}"' "${REPO_DIR}/init_script.sh"
grep -Fq 'chmod 0775 "${APP_ROOT}"' "${REPO_DIR}/init_script.sh"
grep -Fq 'install -d -m 0700 -o "${APP_USER}" -g "${APP_GROUP}" "${WALLET_DIR}"' \
  "${REPO_DIR}/init_script.sh"
grep -Fq 'find "${WALLET_DIR}" -type f -exec chmod 0600 {} \;' "${REPO_DIR}/init_script.sh"

echo "init_script deployment behavior verified."
