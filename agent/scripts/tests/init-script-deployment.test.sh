#!/usr/bin/env bash
# agent/init_script.sh（OCI Resource Manager stack の Compute 初期化）の振る舞いを外部依存なしで検証する。
set -euo pipefail

TEST_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${TEST_SCRIPT_DIR}/../.." && pwd)"
TEST_TMP_DIR="$(mktemp -d)"
trap 'rm -rf -- "${TEST_TMP_DIR}"' EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

prepare_case() {
  local case_dir="$1"
  mkdir -p \
    "${case_dir}/app/props" \
    "${case_dir}/app/no.1-production-ready-assistant/agent/backend" \
    "${case_dir}/units"
  export APP_ROOT="${case_dir}/app"
  export AGENT_INIT_TEST_MODE=true
  export SYSTEMD_UNIT_DIR="${case_dir}/units"
}

run_initialization_case() (
  local scenario="$1"
  local fail_pattern="$2"
  local case_dir="${TEST_TMP_DIR}/${scenario}"
  prepare_case "${case_dir}"
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
  local case_dir="${TEST_TMP_DIR}/systemd"
  prepare_case "${case_dir}"
  # shellcheck source=/dev/null
  source "${REPO_DIR}/init_script.sh"

  systemctl() {
    printf '%s\n' "$*" >> "${case_dir}/systemctl.log"
  }

  configure_systemd
)

run_install_env_case() (
  local case_dir="${TEST_TMP_DIR}/install-env"
  prepare_case "${case_dir}"
  mkdir -p "${case_dir}/app/no.1-production-ready-assistant/platform"
  APP_USER="$(id -un)"
  APP_GROUP="$(id -gn)"
  export APP_USER APP_GROUP
  # shellcheck source=/dev/null
  source "${REPO_DIR}/init_script.sh"

  printf 'PLATFORM_ORACLE_DSN=agentdb_high\nPLATFORM_ENVIRONMENT=staging\nPLATFORM_AUTH_MODE=authenticated\n' > "${PROPS_DIR}/platform.env"
  printf 'AGENT_LOG_LEVEL=INFO\n' > "${PROPS_DIR}/backend.env"
  python3 -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1], "w").writestr("tnsnames.ora", "agentdb_high=\n")' \
    "${PROPS_DIR}/wallet.zip"

  install_runtime_env
)

run_builtin_skills_case() (
  local case_dir="${TEST_TMP_DIR}/builtin-skills-$1"
  prepare_case "${case_dir}"
  # shellcheck source=/dev/null
  source "${REPO_DIR}/init_script.sh"
  local name
  for name in "${BUILTIN_SKILL_NAMES[@]}"; do
    if [ "$1" = "missing" ] && [ "${name}" = "structured-data-query" ]; then
      continue
    fi
    mkdir -p "${BUILTIN_SKILLS_DIR}/${name}"
    printf -- '---\nname: %s\n---\n' "${name}" > "${BUILTIN_SKILLS_DIR}/${name}/SKILL.md"
  done
  check_builtin_skills
)

# --- 組み込みのスキル（repo 直下の skills/。#1816） ---
run_builtin_skills_case present >/dev/null || fail "all built-in skills are present but the check failed"
if run_builtin_skills_case missing >/dev/null 2>&1; then
  fail "a missing built-in skill must stop the deployment"
fi
# 配備する repo の skills/ に、backend の BUILTIN_SKILL_IDS と同じ 3 つがある。
for name in business-rag-research structured-data-query rag-and-structured-data; do
  [ -f "${REPO_DIR}/../skills/${name}/SKILL.md" ] || fail "skills/${name}/SKILL.md is missing in the repo"
done

# --- 共通 .env（platform/.env）と Agent の backend/.env（#211） ---
run_install_env_case
install_suite="${TEST_TMP_DIR}/install-env/app/no.1-production-ready-assistant"
grep -qx 'PLATFORM_ORACLE_DSN=agentdb_high' "${install_suite}/platform/.env" \
  || fail "共通 .env が platform/.env に置かれていない"
test "$(stat -c '%a' "${install_suite}/platform/.env")" = "600" || fail "platform/.env の permission が 0600 ではない"
grep -qx 'AGENT_LOG_LEVEL=INFO' "${install_suite}/agent/backend/.env" \
  || fail "backend/.env が置かれていない"
test "$(stat -c '%a' "${install_suite}/agent/backend/.env")" = "600" || fail "backend/.env の permission が 0600 ではない"
if grep -q 'PLATFORM_' "${install_suite}/agent/backend/.env"; then
  fail "backend/.env に共通の設定が入っている"
fi
# 再実行では、画面で保存した共通 .env を上書きしない。
printf 'PLATFORM_ORACLE_DSN=saved_from_ui\n' > "${install_suite}/platform/.env"
run_install_env_case
grep -qx 'PLATFORM_ORACLE_DSN=saved_from_ui' "${install_suite}/platform/.env" \
  || fail "再実行で共通 .env が上書きされた"

# --- DB 初期化（共通認証・Agent の権限・Run の保存先・定義のシステムテーブル。#215 / #764） ---
run_initialization_case success ""
test "$(cat "${TEST_TMP_DIR}/success/ready")" = "true" || fail "DB 初期化成功時に ready にならない"
grep -Fq "uv run python -m app.cli.agent_system_schema --initialize" "${TEST_TMP_DIR}/success/commands.log" \
  || fail "システムテーブルの作成・更新が実行されていない"
# Run の保存先のテーブルもシステムテーブルが作る（Runtime repository の import で作らない）。
if grep -Fq "import app.features.agent.runtime" "${TEST_TMP_DIR}/success/commands.log"; then
  fail "Runtime repository の import でテーブルを作っている"
fi
test "$(grep -c 'attempts=5' "${TEST_TMP_DIR}/success/commands.log")" = "1" \
  || fail "DB 初期化の command が retry されていない"

run_initialization_case security-degraded 'agent_system_schema'
test "$(cat "${TEST_TMP_DIR}/security-degraded/ready")" = "false" \
  || fail "migration 失敗時に ready=false にならない"

# --- systemd: backend は 127.0.0.1:8020、1 worker ---
run_systemd_case
unit="${TEST_TMP_DIR}/systemd/units/production-ready-agent-backend.service"
test -f "${unit}" || fail "backend unit が作成されていない"
grep -Fq -- '--bind 127.0.0.1:8020 --workers 1' "${unit}" || fail "backend の bind/worker 数が想定と異なる"
grep -Fq 'User=opc' "${unit}" || fail "backend が opc user で動かない"
grep -q '^enable production-ready-agent-backend.service$' "${TEST_TMP_DIR}/systemd/systemctl.log" \
  || fail "backend unit が enable されていない"
if grep -q 'dispatcher' "${unit}"; then
  fail "stack は外部 dispatcher を起動しない"
fi

# --- reverse proxy: Agent は単独では配備せず（#1316）、suite も reverse proxy（Nginx）を配備しない（#19） ---
for unexpected in configure_nginx sites-available proxy_pass APPLICATION_PORT; do
  if grep -Fq "${unexpected}" "${REPO_DIR}/init_script.sh"; then
    fail "単独の配備の Nginx の設定（${unexpected}）が残っている"
  fi
done
grep -Fq 'FRONTEND_BASE_PATH="${FRONTEND_BASE_PATH:-/agent/}"' "${REPO_DIR}/init_script.sh" \
  || fail "frontend を /agent/ を base に build しない"

# --- 静的な不変条件 ---
if grep -Eq 'configure_basic_auth|basic_auth_(user|password)|htpasswd' "${REPO_DIR}/init_script.sh"; then
  fail "Basic 認証の設定が残っている"
fi
grep -Fq 'WALLET_DIR="${APP_ROOT}/wallet"' "${REPO_DIR}/init_script.sh"
grep -Fq 'find "${WALLET_DIR}" -type f -exec chmod 0600 {} \;' "${REPO_DIR}/init_script.sh"
if grep -Eq '^\s*(docker|docker-compose)\b' "${REPO_DIR}/init_script.sh"; then
  fail "直接配備に Docker を要求している"
fi
if grep -Eiq '^\s*[^#]*\b(CREATE|ALTER|DROP)\s+TABLE\b' "${REPO_DIR}/init_script.sh"; then
  fail "init_script.sh に DDL を埋め込んでいる"
fi

echo "agent init_script deployment behavior verified."
