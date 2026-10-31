#!/usr/bin/env bash
# OCI Compute 上で手動のソース更新後に、安全な修復と再デプロイを行う。
# ソース管理、.env の内容、Wallet の内容、systemd unit、Nginx 設定は変更しない。
set -Eeuo pipefail

# uv と Node.js の公式 tarball は /usr/local/bin にある（Oracle Linux の sudo の secure_path には無い。#1611）。
PATH="${PATH}:/usr/local/bin"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="${SCRIPT_DIR}/$(basename "${BASH_SOURCE[0]}")"
DEFAULT_REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

APP_REPO_DIR="${APP_REPO_DIR:-${DEFAULT_REPO_DIR}}"
# APP_REPO_DIR は suite repository（no.1-production-ready-suite）の nl2sql/。APP_ROOT はその suite の親。
SUITE_REPO_DIR="$(dirname "${APP_REPO_DIR}")"
APP_ROOT="${APP_ROOT:-$(cd "${SUITE_REPO_DIR}/.." && pwd)}"
BACKEND_DIR="${APP_REPO_DIR}/backend"
BACKEND_ENV_FILE="${BACKEND_DIR}/.env"
FRONTEND_DIR="${APP_REPO_DIR}/frontend"
PLATFORM_REPO_DIR="${PLATFORM_REPO_DIR:-${SUITE_REPO_DIR}/platform}"
# 3製品共通の設定（PLATFORM_*。Wallet の場所を含む）は platform の共通 .env（#211）。
PLATFORM_ENV_FILE="${PLATFORM_REPO_DIR}/.env"
APP_USER="${APP_USER:-opc}"
APP_GROUP="${APP_GROUP:-opc}"
WALLET_DIR="${WALLET_DIR:-${APP_ROOT}/wallet}"
RECOVERY_ROOT="${RECOVERY_ROOT:-${APP_ROOT}/recovery}"
UPDATE_LOG_PATH="${UPDATE_LOG_PATH:-/var/log/nl2sql-update.log}"

# 3 製品は 1 台の Compute にセットで配備する（#1316）。frontend は /nl2sql/ を base に build し、Nginx 経由の health は
# /nl2sql/health。scheme と port は cloud-init の props（https_enabled.txt / https_port.txt / http_port.txt）から決める。
SUITE_PROPS_DIR="${SUITE_PROPS_DIR:-${APP_ROOT}/props}"
read_suite_prop() {
  tr -d '[:space:]' 2>/dev/null < "${SUITE_PROPS_DIR}/$1" || true
}
if [ "$(read_suite_prop https_enabled.txt)" = "false" ]; then
  DEFAULT_PUBLIC_HEALTH_ORIGIN="http://127.0.0.1:$(port="$(read_suite_prop http_port.txt)"; printf '%s' "${port:-80}")"
else
  DEFAULT_PUBLIC_HEALTH_ORIGIN="https://127.0.0.1:$(port="$(read_suite_prop https_port.txt)"; printf '%s' "${port:-443}")"
fi
FRONTEND_BASE_PATH="${FRONTEND_BASE_PATH:-/nl2sql/}"

# backend は 127.0.0.1:8010（RAG の 8000 と分ける。ローカルの開発と同じ。#1316）。
BACKEND_HEALTH_URL="${BACKEND_HEALTH_URL:-http://127.0.0.1:8010/api/health}"
PUBLIC_HEALTH_URL="${PUBLIC_HEALTH_URL:-${DEFAULT_PUBLIC_HEALTH_ORIGIN}/nl2sql/health}"
HEALTHCHECK_TIMEOUT_SECONDS="${HEALTHCHECK_TIMEOUT_SECONDS:-90}"
HEALTHCHECK_INTERVAL_SECONDS="${HEALTHCHECK_INTERVAL_SECONDS:-2}"
LOCK_FILE="${LOCK_FILE:-/tmp/production-ready-nl2sql-update.lock}"

SUDO_REEXEC_ENV_VARS=(
  APP_REPO_DIR
  APP_ROOT
  PLATFORM_REPO_DIR
  APP_USER
  APP_GROUP
  WALLET_DIR
  RECOVERY_ROOT
  UPDATE_LOG_PATH
  BACKEND_HEALTH_URL
  PUBLIC_HEALTH_URL
  FRONTEND_BASE_PATH
  SUITE_PROPS_DIR
  HEALTHCHECK_TIMEOUT_SECONDS
  HEALTHCHECK_INTERVAL_SECONDS
  LOCK_FILE
  UPDATE_AFTER_PULL_TEST_MODE
)

BACKEND_SERVICE="production-ready-nl2sql-backend.service"
WORKER_SERVICES=(
  "production-ready-nl2sql-schema-refresh-worker.service"
  "production-ready-nl2sql-synthetic-worker.service"
  "production-ready-nl2sql-quality-evaluation-worker.service"
  "production-ready-nl2sql-ontology-worker.service"
)
ALL_SERVICES=("${BACKEND_SERVICE}" "${WORKER_SERVICES[@]}")

ACTION="update"
CURRENT_STAGE="起動前チェック"
COMPILE_CACHE_DIR=""
RECOVERY_DIR=""
MAINTENANCE_STARTED=false
FRONTEND_STAGING_DIR="${FRONTEND_DIR}/.deploy-update-${$}"
FRONTEND_BACKUP_DIR="${FRONTEND_DIR}/.dist-backup-${$}"
FRONTEND_FAILED_DIR="${FRONTEND_DIR}/.dist-failed-${$}"
FRONTEND_PROMOTED=false
FRONTEND_HAD_PREVIOUS_DIST=false

log() {
  printf '[nl2sql-update] %s\n' "$*"
}

warn() {
  printf '[nl2sql-update] WARNING: %s\n' "$*" >&2
}

test_mode_enabled() {
  [ "${UPDATE_AFTER_PULL_TEST_MODE:-false}" = "true" ] || return 1
  case "${APP_ROOT}" in
    /tmp/*) ;;
    *) return 1 ;;
  esac
  [ "${APP_REPO_DIR}" = "${APP_ROOT}/no.1-production-ready-suite/nl2sql" ] && \
    [ "${WALLET_DIR}" = "${APP_ROOT}/wallet" ] && \
    [ "${RECOVERY_ROOT}" = "${APP_ROOT}/recovery" ] && \
    [ "${UPDATE_LOG_PATH}" = "${APP_ROOT}/update.log" ] && \
    [ "${LOCK_FILE}" = "${APP_ROOT}/update.lock" ]
}

fail() {
  printf '[nl2sql-update] ERROR: %s\n' "$*" >&2
  return 1
}

running_as_root() {
  [ "${EUID}" -eq 0 ]
}

run_privileged() {
  if running_as_root; then
    "$@"
    return
  fi
  sudo -n -- "$@"
}

run_as_app_user() {
  if [ "$(id -un)" = "${APP_USER}" ]; then
    "$@"
    return
  fi
  if running_as_root; then
    # Oracle Linux の sudo の secure_path は /usr/local/bin（uv・Node.js の公式 tarball）を含まないため、PATH を渡す。
    sudo -n -H -u "${APP_USER}" -- env PATH="${PATH}" "$@"
    return
  fi
  fail "${APP_USER} ユーザーとして実行できません。"
}

ensure_privileged_access() {
  if running_as_root; then
    return 0
  fi
  if sudo -n -v 2>/dev/null; then
    return 0
  fi
  fail "passwordless sudo が利用できません。パスワード入力は行わず、sudo ./scripts/update-after-pull.sh を実行してください。"
}

reexec_with_sudo_if_needed() {
  local env_name
  local -a env_args=()

  if running_as_root || [ "${ACTION}" = "check" ] || \
    [ "${NL2SQL_UPDATE_SUDO_REEXECED:-false}" = "true" ]; then
    return 0
  fi
  if ! sudo -n -v 2>/dev/null; then
    fail "passwordless sudo が利用できません。パスワード入力は行わず、sudo ${SCRIPT_PATH} を実行してください。"
    return "$?"
  fi
  for env_name in "${SUDO_REEXEC_ENV_VARS[@]}"; do
    if [ "${!env_name+x}" = "x" ]; then
      env_args+=("${env_name}=${!env_name}")
    fi
  done
  env_args+=("NL2SQL_UPDATE_SUDO_REEXECED=true")
  log "passwordless sudo を使用して root として再実行します。"
  exec sudo -n env "${env_args[@]}" "${SCRIPT_PATH}" "$@"
  fail "sudo による再実行に失敗しました。"
}

usage() {
  cat <<'EOF'
Usage: ./scripts/update-after-pull.sh [--check | --repair-only]

引数なし       ソース更新後の build、OCI 修復、migration、再起動、frontend 公開を行います。
--check        固定パス、Wallet 権限、systemd、system schema を読み取り専用で確認します。
--repair-only  build と frontend 公開を省略し、OCI 修復、migration、再起動だけを行います。

このスクリプト自身は Git 操作、schema --recreate、DeepSec foundation 適用を行いません。
opc 実行時は sudo -n のみを使用し、パスワード入力を要求しません。
update / --repair-only は passwordless sudo があれば自動的に root で再実行します。
passwordless sudo がない環境では sudo ./scripts/update-after-pull.sh で起動してください。
root 起動時も build、依存同期、database CLI は opc へ降権して実行します。

Environment overrides:
  PLATFORM_REPO_DIR             共有 platform directory (default: suite の platform/)
  BACKEND_HEALTH_URL            backend の直接 health URL (default: http://127.0.0.1:8010/api/health)
  PUBLIC_HEALTH_URL             Nginx 経由の health URL (default: props の scheme と port の /nl2sql/health)
  FRONTEND_BASE_PATH            frontend の base (default: /nl2sql/)
  HEALTHCHECK_TIMEOUT_SECONDS   health 待機上限秒 (default: 90)
  HEALTHCHECK_INTERVAL_SECONDS  health 再試行間隔秒 (default: 2)
EOF
}

parse_args() {
  if [ "$#" -gt 1 ]; then
    usage >&2
    return 2
  fi
  case "${1:-}" in
    "")
      ACTION="update"
      ;;
    --check)
      ACTION="check"
      ;;
    --repair-only)
      ACTION="repair-only"
      ;;
    -h | --help)
      usage
      return 10
      ;;
    *)
      usage >&2
      return 2
      ;;
  esac
}

require_command() {
  local command_name="$1"
  command -v "${command_name}" >/dev/null 2>&1 || fail "必要なコマンドがありません: ${command_name}"
}

require_file() {
  local path="$1"
  [ -f "${path}" ] || fail "必要なファイルがありません: ${path}"
}

validate_non_negative_integer() {
  local name="$1"
  local value="$2"
  [[ "${value}" =~ ^[0-9]+$ ]] || fail "${name} は 0 以上の整数で指定してください: ${value}"
}

wallet_env_value() {
  local line value
  line="$(awk -F= '$1 == "PLATFORM_ORACLE_WALLET_DIR" {print; found=1} END {if (!found) exit 1}' "${PLATFORM_ENV_FILE}")" || return 1
  line="$(printf '%s\n' "${line}" | tail -n 1)"
  value="${line#*=}"
  value="${value#\"}"
  value="${value%\"}"
  value="${value#\'}"
  value="${value%\'}"
  printf '%s\n' "${value}"
}

service_exists() {
  systemctl cat "$1" >/dev/null 2>&1
}

validate_fixed_oci_layout() {
  local configured_wallet resolved_root
  if ! test_mode_enabled; then
    [ "${APP_USER}" = "opc" ] || fail "実行ユーザー設定は opc である必要があります。"
    [ "${APP_GROUP}" = "opc" ] || fail "実行グループ設定は opc である必要があります。"
    [ "${APP_ROOT}" = "/u01/aipoc" ] || fail "APP_ROOT は /u01/aipoc 固定です。"
    [ "${APP_REPO_DIR}" = "/u01/aipoc/no.1-production-ready-suite/nl2sql" ] || \
      fail "アプリケーションパスが OCI の固定パスではありません。"
    [ "${BACKEND_ENV_FILE}" = "/u01/aipoc/no.1-production-ready-suite/nl2sql/backend/.env" ] || \
      fail "backend/.env が OCI の固定パスではありません。"
    [ "${PLATFORM_ENV_FILE}" = "/u01/aipoc/no.1-production-ready-suite/platform/.env" ] || \
      fail "platform/.env が OCI の固定パスではありません。"
    [ "${WALLET_DIR}" = "/u01/aipoc/wallet" ] || fail "Wallet path は /u01/aipoc/wallet 固定です。"
    [ "${RECOVERY_ROOT}" = "/u01/aipoc/recovery" ] || fail "recovery path は /u01/aipoc/recovery 固定です。"
    [ "${UPDATE_LOG_PATH}" = "/var/log/nl2sql-update.log" ] || \
      fail "更新ログは /var/log/nl2sql-update.log 固定です。"
  fi

  id "${APP_USER}" >/dev/null 2>&1 || fail "実行ユーザーが存在しません: ${APP_USER}"
  getent group "${APP_GROUP}" >/dev/null 2>&1 || fail "実行グループが存在しません: ${APP_GROUP}"
  [ -d "${APP_ROOT}" ] || fail "APP_ROOT が存在しません: ${APP_ROOT}"
  [ ! -L "${APP_ROOT}" ] || fail "APP_ROOT は symbolic link にできません。"
  resolved_root="$(readlink -f "${APP_ROOT}")"
  [ "${resolved_root}" = "${APP_ROOT}" ] || fail "APP_ROOT の解決先が一致しません。"
  [ -d "${APP_REPO_DIR}" ] || fail "アプリケーション repository が存在しません。"
  [ -d "${BACKEND_DIR}" ] || fail "backend directory が存在しません。"
  require_file "${BACKEND_ENV_FILE}"
  [ ! -L "${BACKEND_ENV_FILE}" ] || fail "backend/.env は symbolic link にできません。"
  require_file "${PLATFORM_ENV_FILE}"
  [ ! -L "${PLATFORM_ENV_FILE}" ] || fail "platform/.env は symbolic link にできません。"
  [ -d "${WALLET_DIR}" ] || fail "Wallet directory が存在しません: ${WALLET_DIR}"
  [ ! -L "${WALLET_DIR}" ] || fail "Wallet directory は symbolic link にできません。"
  configured_wallet="$(wallet_env_value)" || fail "platform/.env に PLATFORM_ORACLE_WALLET_DIR がありません。"
  if test_mode_enabled; then
    [ "${configured_wallet}" = "${WALLET_DIR}" ] || fail "test Wallet path が一致しません。"
  else
    [ "${configured_wallet}" = "/u01/aipoc/wallet" ] || \
      fail "PLATFORM_ORACLE_WALLET_DIR は /u01/aipoc/wallet 固定です。"
  fi
  # 旧デプロイには合成生成専用 unit が無い。backend の inprocess 実行で継続する。
  if ! service_exists "production-ready-nl2sql-synthetic-worker.service"; then
    WORKER_SERVICES=(
      "production-ready-nl2sql-schema-refresh-worker.service"
      "production-ready-nl2sql-quality-evaluation-worker.service"
      "production-ready-nl2sql-ontology-worker.service"
    )
    ALL_SERVICES=("${BACKEND_SERVICE}" "${WORKER_SERVICES[@]}")
  fi
  for service in "${ALL_SERVICES[@]}"; do
    service_exists "${service}" || fail "systemd unit がありません: ${service}"
  done
}

preflight() {
  local current_user command_name node_version

  current_user="$(id -un)"
  if running_as_root; then
    log "root 起動を検出しました。build と database CLI は ${APP_USER} ユーザーへ降権して実行します。"
  else
    [ "${current_user}" = "${APP_USER}" ] || \
      fail "実行ユーザーは ${APP_USER} または root である必要があります: ${current_user}"
  fi

  validate_non_negative_integer "HEALTHCHECK_TIMEOUT_SECONDS" "${HEALTHCHECK_TIMEOUT_SECONDS}"
  validate_non_negative_integer "HEALTHCHECK_INTERVAL_SECONDS" "${HEALTHCHECK_INTERVAL_SECONDS}"
  for command_name in awk curl find getent readlink stat systemctl tail; do
    require_command "${command_name}"
  done
  require_command sudo
  require_command uv
  require_file "${BACKEND_DIR}/pyproject.toml"
  require_file "${BACKEND_DIR}/uv.lock"
  validate_fixed_oci_layout

  if [ "${ACTION}" = "check" ]; then
    return 0
  fi

  for command_name in flock install mktemp tee; do
    require_command "${command_name}"
  done
  ensure_privileged_access

  if [ "${ACTION}" = "update" ]; then
    require_command node
    require_command npm
    node_version="$(node --version)"
    [[ "${node_version}" =~ ^v24\. ]] || fail "Node.js 24 が必要です: ${node_version}"
    require_file "${FRONTEND_DIR}/package.json"
    require_file "${FRONTEND_DIR}/package-lock.json"
    require_file "${PLATFORM_REPO_DIR}/package.json"
    require_file "${PLATFORM_REPO_DIR}/package-lock.json"
    require_file "${PLATFORM_REPO_DIR}/packages/ui/package.json"
  fi
}

unit_state() {
  local state
  state="$(systemctl is-active "$1" 2>/dev/null || true)"
  printf '%s' "${state:-unknown}"
}

report_deployment_state() {
  local service stale_paths
  log "時刻: $(date -Is)"
  stat -c '%U:%G %a %n' "${APP_ROOT}" "${WALLET_DIR}" "${BACKEND_ENV_FILE}" "${PLATFORM_ENV_FILE}"
  if [ -e "${APP_ROOT}/.wallet.install.lock" ]; then
    stat -c '%U:%G %a %n' "${APP_ROOT}/.wallet.install.lock"
  else
    warn "Wallet install lock はまだ存在しません。"
  fi
  stale_paths="$(
    find "${APP_ROOT}" -maxdepth 1 -mindepth 1 -type d \
      \( -name '.wallet.tmp-*' -o -name '.wallet.backup-*' \) -print
  )"
  if [ -n "${stale_paths}" ]; then
    warn "Wallet の一時/backup path を検出しました。自動削除しません:"
    printf '%s\n' "${stale_paths}" >&2
  fi
  for service in "${ALL_SERVICES[@]}"; do
    log "${service}: $(unit_state "${service}")"
  done
}

expect_stat() {
  local path="$1" expected_owner="$2" expected_group="$3" expected_mode="$4" actual
  actual="$(stat -c '%U:%G:%a' "${path}")"
  if [ "${actual}" != "${expected_owner}:${expected_group}:${expected_mode}" ]; then
    warn "${path}: expected=${expected_owner}:${expected_group}:${expected_mode} actual=${actual}"
    return 1
  fi
}

verify_wallet_permissions() {
  local app_root_owner=root failed=false invalid_wallet_path
  if test_mode_enabled; then
    app_root_owner="${APP_USER}"
  fi
  expect_stat "${APP_ROOT}" "${app_root_owner}" "${APP_GROUP}" 775 || failed=true
  expect_stat "${WALLET_DIR}" "${APP_USER}" "${APP_GROUP}" 700 || failed=true
  expect_stat "${BACKEND_ENV_FILE}" "${APP_USER}" "${APP_GROUP}" 600 || failed=true
  expect_stat "${PLATFORM_ENV_FILE}" "${APP_USER}" "${APP_GROUP}" 600 || failed=true
  if [ -L "${APP_ROOT}/.wallet.install.lock" ]; then
    warn "Wallet install lock は symbolic link にできません。"
    failed=true
  elif [ -e "${APP_ROOT}/.wallet.install.lock" ]; then
    expect_stat "${APP_ROOT}/.wallet.install.lock" "${APP_USER}" "${APP_GROUP}" 600 || failed=true
  else
    failed=true
  fi
  if ! run_as_app_user test -w "${APP_ROOT}"; then
    warn "${APP_USER} は Wallet 親 directory に書き込めません。"
    failed=true
  fi
  invalid_wallet_path="$(
    find "${WALLET_DIR}" \
      \( -type d ! -perm 0700 -o -type f ! -perm 0600 \
      -o ! -user "${APP_USER}" -o ! -group "${APP_GROUP}" \) -print -quit
  )"
  if [ -n "${invalid_wallet_path}" ]; then
    warn "Wallet 内に owner/mode が不正な path があります: ${invalid_wallet_path}"
    failed=true
  fi
  [ "${failed}" = "false" ]
}

check_system_schema_status() {
  log "NL2SQL system schema の状態を確認します。"
  (
    trap - ERR
    cd "${BACKEND_DIR}"
    run_as_app_user uv run --no-sync python -m app.cli.nl2sql_system_schema --status
  )
}

run_check() {
  report_deployment_state
  check_system_schema_status || warn "system schema status を取得できませんでした。"
  if verify_wallet_permissions; then
    log "Wallet storage permissions は正常です。"
    return 0
  fi
  fail "Wallet storage permissions の修復が必要です。--repair-only を実行してください。"
}

acquire_lock() {
  [ ! -L "${LOCK_FILE}" ] || fail "更新 lock は symbolic link にできません: ${LOCK_FILE}"
  if running_as_root; then
    touch "${LOCK_FILE}"
    chown root:root "${LOCK_FILE}"
    chmod 0600 "${LOCK_FILE}"
  fi
  exec 9>"${LOCK_FILE}"
  if ! flock -n 9; then
    if running_as_root; then
      chown "${APP_USER}:${APP_GROUP}" "${LOCK_FILE}" || true
      chmod 0600 "${LOCK_FILE}" || true
    fi
    fail "別の更新処理が実行中です: ${LOCK_FILE}"
  fi
  if running_as_root; then
    chown "${APP_USER}:${APP_GROUP}" "${LOCK_FILE}"
    chmod 0600 "${LOCK_FILE}"
  fi
}

setup_update_log() {
  if run_privileged test -L "${UPDATE_LOG_PATH}"; then
    fail "更新ログは symbolic link にできません。"
  fi
  if ! run_privileged test -e "${UPDATE_LOG_PATH}"; then
    run_privileged install -m 0600 -o root -g root /dev/null "${UPDATE_LOG_PATH}"
  else
    run_privileged chown root:root "${UPDATE_LOG_PATH}"
    run_privileged chmod 0600 "${UPDATE_LOG_PATH}"
  fi
  exec > >(run_privileged tee -a "${UPDATE_LOG_PATH}") 2>&1
}

remove_generated_path() {
  local path="$1"
  [ -n "${path}" ] || return 0
  case "${path}" in
    "${FRONTEND_DIR}"/.deploy-update-*|\
    "${FRONTEND_DIR}"/.dist-backup-*|\
    "${FRONTEND_DIR}"/.dist-failed-*|\
    /tmp/production-ready-nl2sql-compile.*)
      rm -rf -- "${path}"
      ;;
    *)
      warn "管理対象外の一時パスは削除しません: ${path}"
      ;;
  esac
}

rollback_frontend() {
  if [ "${FRONTEND_PROMOTED}" != "true" ]; then
    return 0
  fi
  log "フロントエンドを直前の dist へ戻します。"
  if [ -e "${FRONTEND_DIR}/dist" ] || [ -L "${FRONTEND_DIR}/dist" ]; then
    mv "${FRONTEND_DIR}/dist" "${FRONTEND_FAILED_DIR}" || return 1
  fi
  if [ "${FRONTEND_HAD_PREVIOUS_DIST}" = "true" ]; then
    mv "${FRONTEND_BACKUP_DIR}" "${FRONTEND_DIR}/dist" || return 1
  fi
  FRONTEND_PROMOTED=false
  remove_generated_path "${FRONTEND_FAILED_DIR}"
}

cleanup() {
  remove_generated_path "${COMPILE_CACHE_DIR}"
  remove_generated_path "${FRONTEND_STAGING_DIR}"
  remove_generated_path "${FRONTEND_FAILED_DIR}"
}

enter_degraded_mode() {
  local service
  set +e
  for service in "${WORKER_SERVICES[@]}"; do
    if service_exists "${service}"; then
      run_privileged systemctl disable --now "${service}"
    fi
  done
  if service_exists "${BACKEND_SERVICE}"; then
    run_privileged systemctl start "${BACKEND_SERVICE}"
  fi
  set -e
  warn "backend を復旧し、external worker を停止した degraded mode にしました。"
}

on_error() {
  local status="$1" line="$2"
  trap - ERR
  set +e
  rollback_frontend
  if [ "${MAINTENANCE_STARTED}" = "true" ]; then
    enter_degraded_mode
  fi
  warn "${CURRENT_STAGE} に失敗しました (line=${line}, status=${status})。"
  warn "確認例: sudo journalctl -u ${BACKEND_SERVICE} -n 200 --no-pager"
  if [ -n "${RECOVERY_DIR}" ]; then
    warn "復旧 snapshot: ${RECOVERY_DIR}"
  fi
  exit "${status}"
}

sync_and_compile_backend() {
  COMPILE_CACHE_DIR="$(run_as_app_user mktemp -d /tmp/production-ready-nl2sql-compile.XXXXXX)"
  log "backend の production 依存を同期します。"
  (
    trap - ERR
    cd "${BACKEND_DIR}"
    run_as_app_user uv sync --locked --no-dev --python 3.12
  )
  log "backend の Python ソースをコンパイル検証します。"
  (
    trap - ERR
    cd "${BACKEND_DIR}"
    run_as_app_user env PYTHONPYCACHEPREFIX="${COMPILE_CACHE_DIR}" \
      uv run --no-sync python -m compileall -q app
  )
}

build_frontend_staging() {
  log "共有 UI の依存を同期してビルドします。"
  (
    trap - ERR
    cd "${PLATFORM_REPO_DIR}"
    run_as_app_user npm ci
    # 共有 UI と共有システム設定画面を依存順に build する（platform の npm run build）。
    run_as_app_user npm run build
  )
  log "NL2SQL frontend を型検証し、一時 directory へビルドします。"
  remove_generated_path "${FRONTEND_STAGING_DIR}"
  (
    trap - ERR
    cd "${FRONTEND_DIR}"
    run_as_app_user npm ci
    run_as_app_user env FRONTEND_BASE_PATH="${FRONTEND_BASE_PATH}" \
      npm run build -- --outDir "${FRONTEND_STAGING_DIR}" --emptyOutDir
  )
  require_file "${FRONTEND_STAGING_DIR}/index.html"
  [ -d "${FRONTEND_STAGING_DIR}/assets" ] || \
    fail "frontend assets が生成されませんでした: ${FRONTEND_STAGING_DIR}/assets"
}

create_recovery_snapshot() {
  local service repair_stamp
  repair_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  run_privileged test ! -L "${RECOVERY_ROOT}" || \
    fail "recovery path は symbolic link にできません。"
  run_privileged install -d -m 0700 -o root -g root "${RECOVERY_ROOT}"
  RECOVERY_DIR="$(run_privileged mktemp -d "${RECOVERY_ROOT}/${repair_stamp}.XXXXXX")"
  run_privileged chown root:root "${RECOVERY_DIR}"
  run_privileged chmod 0700 "${RECOVERY_DIR}"
  run_privileged install -m 0600 -o root -g root \
    "${BACKEND_ENV_FILE}" "${RECOVERY_DIR}/backend.env"
  run_privileged install -m 0600 -o root -g root \
    "${PLATFORM_ENV_FILE}" "${RECOVERY_DIR}/platform.env"
  for service in "${ALL_SERVICES[@]}"; do
    run_privileged systemctl cat "${service}" | \
      run_privileged tee "${RECOVERY_DIR}/${service}.txt" >/dev/null
  done
  stat -c '%U:%G %a %n' "${APP_ROOT}" "${WALLET_DIR}" "${BACKEND_ENV_FILE}" "${PLATFORM_ENV_FILE}" | \
    run_privileged tee "${RECOVERY_DIR}/permissions-before.txt" >/dev/null
  log "復旧 snapshot: ${RECOVERY_DIR}"
}

enter_maintenance() {
  MAINTENANCE_STARTED=true
  log "external worker を停止します。"
  run_privileged systemctl stop "${WORKER_SERVICES[@]}"
  log "backend を停止します。"
  run_privileged systemctl stop "${BACKEND_SERVICE}"
}

repair_wallet_permissions() {
  local lock_path="${APP_ROOT}/.wallet.install.lock" test_path
  log "Wallet 親 directory、Wallet、lock、backend/.env、platform/.env の権限を修復します。"
  run_privileged chown "root:${APP_GROUP}" "${APP_ROOT}"
  run_privileged chmod 0775 "${APP_ROOT}"
  run_privileged chown -R "${APP_USER}:${APP_GROUP}" "${WALLET_DIR}"
  run_privileged find "${WALLET_DIR}" -type d -exec chmod 0700 {} +
  run_privileged find "${WALLET_DIR}" -type f -exec chmod 0600 {} +
  run_privileged test ! -L "${lock_path}" || \
    fail "Wallet install lock は symbolic link にできません。"
  run_privileged install -m 0600 -o "${APP_USER}" -g "${APP_GROUP}" /dev/null "${lock_path}"
  run_privileged chown "${APP_USER}:${APP_GROUP}" "${BACKEND_ENV_FILE}"
  run_privileged chmod 0600 "${BACKEND_ENV_FILE}"
  run_privileged chown "${APP_USER}:${APP_GROUP}" "${PLATFORM_ENV_FILE}"
  run_privileged chmod 0600 "${PLATFORM_ENV_FILE}"
  test_path="${APP_ROOT}/.wallet.permission-test.$$"
  trap 'rm -f -- "${test_path}"' RETURN
  run_as_app_user touch "${test_path}"
  run_as_app_user rm -f -- "${test_path}"
  trap - RETURN
  verify_wallet_permissions
}

run_database_migrations() {
  check_system_schema_status || warn "更新前の system schema status を取得できませんでした。"
  log "NL2SQL system schema を更新します。"
  (
    trap - ERR
    cd "${BACKEND_DIR}"
    run_as_app_user uv run --no-sync python -m app.cli.nl2sql_system_schema --initialize
  )
  log "security/RBAC migration を preview します。"
  (
    trap - ERR
    cd "${BACKEND_DIR}"
    run_as_app_user uv run --no-sync python -m app.cli.app_security_migrate
  )
  log "security/RBAC migration を適用します。"
  (
    trap - ERR
    cd "${BACKEND_DIR}"
    run_as_app_user uv run --no-sync python -m app.cli.app_security_migrate \
      --apply --skip-bootstrap
  )
}

wait_for_health() {
  local label="$1" url="$2" deadline=$((SECONDS + HEALTHCHECK_TIMEOUT_SECONDS))
  local last_error="" last_status=0
  local insecure=()
  # 同じ host の自己確認だけ、証明書の名前（公開 IP・private IP）と 127.0.0.1 の違いを無視する（#1316）。
  case "${url}" in
    https://127.0.0.1/* | https://127.0.0.1:*) insecure=(--insecure) ;;
  esac
  log "${label} health を待機します: ${url}"
  while true; do
    # 起動直後の一時的な接続失敗は retry し、stderr は期限切れ時だけ表示する。
    if last_error="$(curl -fsS ${insecure[@]+"${insecure[@]}"} --max-time 5 "${url}" 2>&1 >/dev/null)"; then
      log "${label} health を確認しました。"
      return 0
    else
      last_status=$?
    fi
    if [ "${SECONDS}" -ge "${deadline}" ]; then
      if [ -n "${last_error}" ]; then
        warn "${label} health の最終確認エラー (curl exit=${last_status}): ${last_error}"
      fi
      fail "${label} health が ${HEALTHCHECK_TIMEOUT_SECONDS} 秒以内に成功しませんでした (curl exit=${last_status}): ${url}"
      return 1
    fi
    sleep "${HEALTHCHECK_INTERVAL_SECONDS}"
  done
}

restart_services() {
  local service
  log "backend を enable/restart します。"
  run_privileged systemctl enable "${BACKEND_SERVICE}"
  run_privileged systemctl restart "${BACKEND_SERVICE}"
  wait_for_health "backend" "${BACKEND_HEALTH_URL}"
  log "worker を enable/restart します。"
  run_privileged systemctl enable "${WORKER_SERVICES[@]}"
  run_privileged systemctl restart "${WORKER_SERVICES[@]}"
  for service in "${ALL_SERVICES[@]}"; do
    run_privileged systemctl is-active --quiet "${service}" || \
      fail "systemd service が active ではありません: ${service}"
  done
  MAINTENANCE_STARTED=false
}

promote_frontend() {
  log "新しい frontend dist を公開します。"
  remove_generated_path "${FRONTEND_BACKUP_DIR}"
  if [ -e "${FRONTEND_DIR}/dist" ] || [ -L "${FRONTEND_DIR}/dist" ]; then
    mv "${FRONTEND_DIR}/dist" "${FRONTEND_BACKUP_DIR}"
    FRONTEND_HAD_PREVIOUS_DIST=true
  fi
  if ! mv "${FRONTEND_STAGING_DIR}" "${FRONTEND_DIR}/dist"; then
    if [ "${FRONTEND_HAD_PREVIOUS_DIST}" = "true" ]; then
      mv "${FRONTEND_BACKUP_DIR}" "${FRONTEND_DIR}/dist" || true
    fi
    fail "新しい frontend dist を公開できませんでした。"
  fi
  FRONTEND_PROMOTED=true
}

finalize_frontend() {
  FRONTEND_PROMOTED=false
  remove_generated_path "${FRONTEND_BACKUP_DIR}"
  FRONTEND_STAGING_DIR=""
  FRONTEND_BACKUP_DIR=""
}

run_maintenance() {
  CURRENT_STAGE="復旧 snapshot の作成"
  create_recovery_snapshot
  CURRENT_STAGE="maintenance mode への移行"
  enter_maintenance
  CURRENT_STAGE="Wallet storage 権限の修復"
  repair_wallet_permissions
  CURRENT_STAGE="データベース migration"
  run_database_migrations
  CURRENT_STAGE="systemd service の再起動"
  restart_services
}

main() {
  local parse_status
  set +e
  parse_args "$@"
  parse_status=$?
  set -e
  if [ "${parse_status}" -eq 10 ]; then
    return 0
  fi
  [ "${parse_status}" -eq 0 ] || return "${parse_status}"

  reexec_with_sudo_if_needed "$@" || return "$?"
  preflight
  if [ "${ACTION}" = "check" ]; then
    run_check
    return
  fi

  acquire_lock
  setup_update_log
  trap 'on_error "$?" "${LINENO}"' ERR
  trap cleanup EXIT

  if [ "${ACTION}" = "update" ]; then
    CURRENT_STAGE="backend の依存同期とコンパイル"
    sync_and_compile_backend
    CURRENT_STAGE="frontend の依存同期とビルド"
    build_frontend_staging
  fi

  run_maintenance

  if [ "${ACTION}" = "update" ]; then
    CURRENT_STAGE="frontend dist の公開"
    promote_frontend
  fi

  CURRENT_STAGE="Nginx 経由の health check"
  wait_for_health "public" "${PUBLIC_HEALTH_URL}"
  if [ "${ACTION}" = "update" ]; then
    finalize_frontend
  fi

  CURRENT_STAGE="完了"
  report_deployment_state
  log "${ACTION} が完了しました。"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
