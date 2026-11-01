#!/usr/bin/env bash
set -euo pipefail

if [ "${NL2SQL_INIT_TEST_MODE:-false}" != "true" ]; then
  exec > >(tee -a /var/log/nl2sql-init.log) 2>&1
fi

INIT_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# backend の起動の待ち方（suite_wait_for_service_health。#1642）は platform の配備の共通の helper（製品は単独では配備しない）。
# shellcheck source-path=SCRIPTDIR source=../platform/deploy/suite-health.sh
source "${INIT_SCRIPT_DIR}/../platform/deploy/suite-health.sh"
# shellcheck source-path=SCRIPTDIR source=../platform/deploy/suite-repo.sh
source "${INIT_SCRIPT_DIR}/../platform/deploy/suite-repo.sh"

APP_ROOT="${APP_ROOT:-/u01/aipoc}"
APP_USER="${APP_USER:-opc}"
APP_GROUP="${APP_GROUP:-${APP_USER}}"
# NL2SQL と共有 platform は monorepo（no.1-production-ready-suite）の nl2sql/ と platform/ にある。
SUITE_REPO_DIR="${APP_ROOT}/no.1-production-ready-suite"
APP_REPO_DIR="${SUITE_REPO_DIR}/nl2sql"
PLATFORM_REPO_DIR="${SUITE_REPO_DIR}/platform"
BACKEND_DIR="${APP_REPO_DIR}/backend"
FRONTEND_DIR="${APP_REPO_DIR}/frontend"
DATA_DIR="/u01/data/production-ready-nl2sql"
LEGACY_DATA_DIR="/u01/production-ready-nl2sql"
WALLET_DIR="${APP_ROOT}/wallet"
BACKEND_HOST="127.0.0.1"
# 1 台の Compute に RAG（8000）・Agent（8020）と
# 一緒に置くため、以前の 8000 から変えた（#1316）。ローカルの開発の port（8110。#25）とは別。
BACKEND_PORT="8010"
# backend/.env の元（suite では製品ごとの <製品>.backend.env）。
BACKEND_ENV_SOURCE="${BACKEND_ENV_SOURCE:-${APP_ROOT}/props/backend.env}"
# frontend の base（Vite の base・React Router の basename）。reverse proxy の /nl2sql/ の下で配信する前提（#1316）。
FRONTEND_BASE_PATH="${FRONTEND_BASE_PATH:-/nl2sql/}"
# true なら共有 UI（platform）を build し直さない（suite で先の製品が build したとき）。
PR_SUITE_SKIP_PLATFORM_UI_BUILD="${PR_SUITE_SKIP_PLATFORM_UI_BUILD:-false}"
NODEJS_OFFICIAL_RELEASE_BASE_URL="${NODEJS_OFFICIAL_RELEASE_BASE_URL:-https://nodejs.org/download/release/latest-v24.x}"
NODEJS_OFFICIAL_INSTALL_DIR="${NODEJS_OFFICIAL_INSTALL_DIR:-/usr/local/lib/nodejs}"
NODEJS_OFFICIAL_BIN_DIR="${NODEJS_OFFICIAL_BIN_DIR:-/usr/local/bin}"
SYSTEMD_UNIT_DIR="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
DATABASE_INITIALIZATION_READY=false

PATH="/usr/local/bin:/usr/bin:/bin:${PATH}"

log() {
  printf '[nl2sql-init] %s\n' "$*"
}

trap 'status=$?; log "Initialization failed at line ${LINENO} with status ${status}: ${BASH_COMMAND}"; exit "${status}"' ERR

retry_command() {
  local attempts="$1"
  shift
  local attempt status
  status=0
  for attempt in $(seq 1 "${attempts}"); do
    set +e
    "$@"
    status="$?"
    set -e
    if [ "${status}" -eq 0 ]; then
      return 0
    fi
    log "Command failed with status ${status} on attempt ${attempt}/${attempts}: $*"
    # 最後の失敗の後は待たない（DB の停止中などに、配備を無駄に延ばさない。#1779）。
    if [ "${attempt}" -lt "${attempts}" ]; then
      sleep $((attempt * 5))
    fi
  done
  return "${status}"
}

# OS の package は dnf で入れる（Compute の OS は Oracle Linux 9。#1611）。dnf は他の dnf の lock を待つ。
dnf_cmd() {
  retry_command 5 dnf -y "$@"
}

run_as_app_user() {
  runuser -u "${APP_USER}" -- env PATH="${PATH}" "$@"
}

run_as_app_user_in_dir() {
  local workdir="$1"
  shift
  run_as_app_user bash -lc "cd '${workdir}' && $*"
}

# NL2SQL が使う OS の package（suite 共通の package は platform/deploy/suite-init.sh が入れる）。
# gcc・make は wheel の無い Python の依存を build するため。curl は image にある。
SYSTEM_PACKAGES=(ca-certificates gcc gcc-c++ git make tar unzip)

install_system_packages() {
  log "Installing system packages: ${SYSTEM_PACKAGES[*]}"
  dnf_cmd install "${SYSTEM_PACKAGES[@]}"
}

install_nodejs() {
  local installed_version
  local npm_version

  if command -v node >/dev/null 2>&1; then
    installed_version="$(node --version)" || return 1
    if printf '%s\n' "${installed_version}" | grep -q '^v24\.' && command -v npm >/dev/null 2>&1; then
      npm_version="$(npm --version)" || return 1
      log "Node.js ${installed_version} with npm ${npm_version} is already installed."
      return
    fi
  fi

  if install_nodejs_from_dnf_module; then
    return
  fi

  log "AppStream Node.js 24 installation failed; falling back to official Node.js tarball."
  install_nodejs_from_official_tarball || return 1
}

# Node.js 24 は Oracle Linux の AppStream の module（nodejs:24）から入れる。
install_nodejs_from_dnf_module() {
  log "Installing Node.js 24 from the AppStream module (nodejs:24)."
  dnf_cmd module reset nodejs || return 1
  dnf_cmd module enable "nodejs:24" || return 1
  dnf_cmd install nodejs npm || return 1
  validate_nodejs_24 || return 1
}

resolve_nodejs_official_platform() {
  local architecture

  NODEJS_OFFICIAL_PLATFORM=""
  architecture="$(uname -m)" || return 1

  case "${architecture}" in
    amd64 | x86_64)
      NODEJS_OFFICIAL_PLATFORM="linux-x64"
      ;;
    arm64 | aarch64)
      NODEJS_OFFICIAL_PLATFORM="linux-arm64"
      ;;
    *)
      log "Unsupported architecture for official Node.js tarball: ${architecture}"
      return 1
      ;;
  esac
}

install_nodejs_from_official_tarball() {
  local node_platform
  local node_dir
  local shasums_path
  local tarball_name
  local tmp_dir

  resolve_nodejs_official_platform || return 1
  node_platform="${NODEJS_OFFICIAL_PLATFORM}"
  tmp_dir="$(mktemp -d)" || return 1
  shasums_path="${tmp_dir}/SHASUMS256.txt"

  log "Installing Node.js 24 official tarball for ${node_platform}."
  if ! curl -fsSL "${NODEJS_OFFICIAL_RELEASE_BASE_URL%/}/SHASUMS256.txt" -o "${shasums_path}"; then
    rm -rf "${tmp_dir}"
    return 1
  fi

  tarball_name="$(
    awk -v node_platform="${node_platform}" '
      {
        filename = $2
        sub("^[*]", "", filename)
        sub("^\\./", "", filename)
      }
      filename ~ ("^node-v24[.][0-9]+[.][0-9]+-" node_platform "[.]tar[.]gz$") {
        print filename
        exit
      }
    ' "${shasums_path}"
  )" || {
    rm -rf "${tmp_dir}"
    return 1
  }
  if [ -z "${tarball_name}" ]; then
    log "No official Node.js 24 tarball found for ${node_platform}."
    rm -rf "${tmp_dir}"
    return 1
  fi

  if ! curl -fsSL "${NODEJS_OFFICIAL_RELEASE_BASE_URL%/}/${tarball_name}" -o "${tmp_dir}/${tarball_name}"; then
    rm -rf "${tmp_dir}"
    return 1
  fi
  if ! (cd "${tmp_dir}" && sha256sum --ignore-missing -c SHASUMS256.txt); then
    log "Official Node.js tarball checksum verification failed: ${tarball_name}"
    rm -rf "${tmp_dir}"
    return 1
  fi

  node_dir="${tarball_name%.tar.gz}"
  install -d -m 0755 "${NODEJS_OFFICIAL_INSTALL_DIR}" "${NODEJS_OFFICIAL_BIN_DIR}" || {
    rm -rf "${tmp_dir}"
    return 1
  }
  if ! tar -xzf "${tmp_dir}/${tarball_name}" -C "${NODEJS_OFFICIAL_INSTALL_DIR}"; then
    rm -rf "${tmp_dir}"
    return 1
  fi
  if ! symlink_official_nodejs_binaries "${NODEJS_OFFICIAL_INSTALL_DIR}/${node_dir}"; then
    rm -rf "${tmp_dir}"
    return 1
  fi

  rm -rf "${tmp_dir}"
  hash -r || true
  validate_nodejs_24 || return 1
}

symlink_official_nodejs_binaries() {
  local executable
  local node_home="$1"

  for executable in node npm npx corepack; do
    if [ ! -x "${node_home}/bin/${executable}" ]; then
      log "Official Node.js tarball is missing ${executable}: ${node_home}/bin/${executable}"
      return 1
    fi
    ln -sfn "${node_home}/bin/${executable}" "${NODEJS_OFFICIAL_BIN_DIR}/${executable}" || return 1
  done
}

validate_nodejs_24() {
  local installed_version
  local npm_version

  if ! command -v node >/dev/null 2>&1; then
    log "node command was not installed."
    return 1
  fi

  installed_version="$(node --version)" || return 1
  if ! printf '%s\n' "${installed_version}" | grep -q '^v24\.'; then
    log "Expected Node.js v24.x after installation, got ${installed_version}."
    return 1
  fi

  if ! command -v npm >/dev/null 2>&1; then
    log "npm was not installed with Node.js ${installed_version}."
    return 1
  fi

  npm_version="$(npm --version)" || return 1
  log "Installed Node.js ${installed_version} with npm ${npm_version}."
}

# installer（astral.sh の install.sh）は一時の場所に展開してから /usr/local/bin へ移すため、SELinux が有効だと
# /usr/local/bin/uv の context が bin_t にならない（移動は元の label を保つ）。systemd（init_t）は bin_t でない file を
# ExecStart で実行できず 203/EXEC（Permission denied）になる（NL2SQL・Agent の unit は uv を ExecStart にする）。
# 既に入っている場合も戻す（既存の配備は suite-restart.sh の再配備で直る）。#1642
# UV_BIN_DIR の上書きは test が隔離した場所を使うためのもので、本番では変えない。
restore_uv_selinux_context() {
  local bin_dir="${UV_BIN_DIR:-/usr/local/bin}" paths=() path
  if ! command -v selinuxenabled >/dev/null 2>&1 || ! selinuxenabled; then
    return 0
  fi
  for path in "${bin_dir}/uv" "${bin_dir}/uvx"; do
    if [ -e "${path}" ]; then
      paths+=("${path}")
    fi
  done
  if [ "${#paths[@]}" -gt 0 ]; then
    restorecon -v "${paths[@]}"
  fi
}

install_uv() {
  if command -v uv >/dev/null 2>&1; then
    log "uv $(uv --version) is already installed."
  else
    log "Installing uv."
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
    chmod 0755 /usr/local/bin/uv
    uv --version
  fi
  restore_uv_selinux_context
}

data_dir_has_entries() {
  local path="$1"
  [ -d "${path}" ] && find "${path}" -mindepth 1 -maxdepth 1 -print -quit | grep -q .
}

backup_legacy_data_dir() {
  local backup_dir
  local suffix
  local timestamp

  timestamp="$(date +%Y%m%d%H%M%S)"
  backup_dir="${LEGACY_DATA_DIR}.legacy-${timestamp}"
  suffix=0
  while [ -e "${backup_dir}" ] || [ -L "${backup_dir}" ]; do
    suffix=$((suffix + 1))
    backup_dir="${LEGACY_DATA_DIR}.legacy-${timestamp}.${suffix}"
  done
  mv "${LEGACY_DATA_DIR}" "${backup_dir}"
  printf '%s\n' "${backup_dir}"
}

link_legacy_data_dir() {
  if [ -e "${LEGACY_DATA_DIR}" ] || [ -L "${LEGACY_DATA_DIR}" ]; then
    return
  fi

  ln -s "${DATA_DIR}" "${LEGACY_DATA_DIR}"
  chown -h "${APP_USER}:${APP_USER}" "${LEGACY_DATA_DIR}" || true
}

migrate_legacy_data_dir() {
  local backup_dir
  local legacy_target

  if [ -L "${LEGACY_DATA_DIR}" ]; then
    legacy_target="$(readlink "${LEGACY_DATA_DIR}")" || legacy_target=""
    if [ "${legacy_target}" = "${DATA_DIR}" ]; then
      log "Legacy data directory already points to ${DATA_DIR}."
    else
      log "Legacy data path ${LEGACY_DATA_DIR} is a symlink to ${legacy_target}; leaving it unchanged."
    fi
    return
  fi

  if [ ! -d "${LEGACY_DATA_DIR}" ]; then
    link_legacy_data_dir
    return
  fi

  if data_dir_has_entries "${DATA_DIR}"; then
    log "Both ${DATA_DIR} and legacy ${LEGACY_DATA_DIR} contain entries; skipping legacy migration to avoid overwrite."
    return
  fi

  log "Migrating legacy data directory ${LEGACY_DATA_DIR} to ${DATA_DIR}."
  cp -a "${LEGACY_DATA_DIR}/." "${DATA_DIR}/"
  backup_dir="$(backup_legacy_data_dir)"
  link_legacy_data_dir
  log "Legacy data directory moved to ${backup_dir}; ${LEGACY_DATA_DIR} now links to ${DATA_DIR}."
}

prepare_filesystem() {
  log "Preparing application directories."
  if ! id "${APP_USER}" >/dev/null 2>&1; then
    log "Application user ${APP_USER} does not exist."
    return 1
  fi

  # Wallet の lock/tmp/backup は Wallet 自身ではなく APP_ROOT 直下に作る。
  # root 所有を維持しつつ app group にだけ書き込みを許可する。
  chown "root:${APP_GROUP}" "${APP_ROOT}"
  chmod 0775 "${APP_ROOT}"
  install -d -m 0755 -o "${APP_USER}" -g "${APP_GROUP}" "${DATA_DIR}"
  migrate_legacy_data_dir
  install -d -m 0700 -o "${APP_USER}" -g "${APP_GROUP}" "${WALLET_DIR}"
  # git-pull.sh を APP_USER で実行できるよう、.git を含む suite 全体を APP_USER の所有にする。
  # 開発の worktree・一時ファイルは辿らない（配備の途中で消えると chown が失敗する。#2307）。
  suite_chown_repo "${APP_USER}:${APP_GROUP}" "${SUITE_REPO_DIR}"
  chown -R "${APP_USER}:${APP_GROUP}" "${DATA_DIR}" "${WALLET_DIR}"
  chown -h "${APP_USER}:${APP_GROUP}" "${LEGACY_DATA_DIR}" 2>/dev/null || true
}

install_runtime_env() {
  log "Installing backend environment, platform environment and wallet."
  install -m 0600 -o "${APP_USER}" -g "${APP_GROUP}" "${BACKEND_ENV_SOURCE}" "${BACKEND_DIR}/.env"
  # 3製品共通の設定（PLATFORM_*）は platform の共通 .env（#211）。システム設定画面の保存先でもあるため、
  # 既にあれば上書きしない（画面で保存した値を消さない）。
  if [ -e "${PLATFORM_REPO_DIR}/.env" ]; then
    log "Keeping existing shared platform environment: ${PLATFORM_REPO_DIR}/.env"
  else
    install -m 0600 -o "${APP_USER}" -g "${APP_GROUP}" "${APP_ROOT}/props/platform.env" "${PLATFORM_REPO_DIR}/.env"
  fi

  rm -rf "${WALLET_DIR}"
  install -d -m 0700 -o "${APP_USER}" -g "${APP_GROUP}" "${WALLET_DIR}"
  unzip -oq "${APP_ROOT}/props/wallet.zip" -d "${WALLET_DIR}"
  chown -R "${APP_USER}:${APP_GROUP}" "${WALLET_DIR}"
  find "${WALLET_DIR}" -type d -exec chmod 0700 {} \;
  find "${WALLET_DIR}" -type f -exec chmod 0600 {} \;
}

install_backend() {
  log "Installing backend dependencies with uv."
  run_as_app_user_in_dir "${BACKEND_DIR}" "uv python install 3.12"
  run_as_app_user_in_dir "${BACKEND_DIR}" "uv sync --locked --no-dev --python 3.12"
}

initialize_database_schema() {
  local security_ready=false
  local system_ready=false

  log "Initializing NL2SQL system schema with an idempotent migration."
  if retry_command 5 run_as_app_user_in_dir "${BACKEND_DIR}" \
    "uv run python -m app.cli.nl2sql_system_schema --initialize"; then
    system_ready=true
  else
    log "WARNING: NL2SQL system schema initialization is incomplete."
  fi

  log "Applying application security/RBAC migrations without bootstrapping users."
  if retry_command 5 run_as_app_user_in_dir "${BACKEND_DIR}" \
    "uv run python -m app.cli.app_security_migrate --apply --skip-bootstrap"; then
    security_ready=true
  else
    log "WARNING: Application security/RBAC migration is incomplete."
  fi

  if [ "${system_ready}" = "true" ] && [ "${security_ready}" = "true" ]; then
    DATABASE_INITIALIZATION_READY=true
    log "Database initialization completed; external workers will be enabled."
    return 0
  fi

  DATABASE_INITIALIZATION_READY=false
  log "WARNING: Continuing in degraded mode so the web application remains available."
  log "WARNING: External workers remain stopped until both schema commands succeed."
  log "Recovery: cd ${BACKEND_DIR} && sudo -u ${APP_USER} /usr/local/bin/uv run python -m app.cli.nl2sql_system_schema --initialize"
  log "Recovery: cd ${BACKEND_DIR} && sudo -u ${APP_USER} /usr/local/bin/uv run python -m app.cli.app_security_migrate --apply --skip-bootstrap"
  # backend は起動する（画面から直せる）が、suite-init.sh の最後の確認は schema を NG にして非 0 で終わる（#1648）。
  log "WARNING: The deployment check at the end of suite-init.sh reports 'check: nl2sql schema NG' until the schema is ready."
  return 0
}

build_frontend() {
  if [ "${PR_SUITE_SKIP_PLATFORM_UI_BUILD}" = "true" ]; then
    log "Shared UI package was already built by another product."
  else
    log "Building shared UI package."
    run_as_app_user_in_dir "${PLATFORM_REPO_DIR}" "npm ci"
    run_as_app_user_in_dir "${PLATFORM_REPO_DIR}" "npm run build"
  fi

  log "Building NL2SQL frontend (base ${FRONTEND_BASE_PATH})."
  run_as_app_user_in_dir "${FRONTEND_DIR}" "npm ci"
  run_as_app_user_in_dir "${FRONTEND_DIR}" "FRONTEND_BASE_PATH='${FRONTEND_BASE_PATH}' npm run build"
}

write_systemd_unit() {
  local unit_path="$1"
  local exec_start="$2"
  local description="$3"

  cat > "${unit_path}" <<EOF
[Unit]
Description=${description}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_GROUP}
WorkingDirectory=${BACKEND_DIR}
Environment=HOME=/home/${APP_USER}
Environment=PYTHONUNBUFFERED=1
Environment=UV_NO_PROGRESS=1
ExecStart=${exec_start}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
}

configure_systemd() {
  log "Writing systemd units."
  write_systemd_unit \
    "${SYSTEMD_UNIT_DIR}/production-ready-nl2sql-backend.service" \
    "/usr/local/bin/uv run gunicorn app.main:app --worker-class uvicorn.workers.UvicornWorker --bind ${BACKEND_HOST}:${BACKEND_PORT} --workers 2 --timeout 300" \
    "Production Ready NL2SQL backend"

  write_systemd_unit \
    "${SYSTEMD_UNIT_DIR}/production-ready-nl2sql-schema-refresh-worker.service" \
    "/usr/local/bin/uv run python -m app.cli.nl2sql_schema_refresh_worker --poll-seconds 1" \
    "Production Ready NL2SQL schema refresh worker"

  write_systemd_unit \
    "${SYSTEMD_UNIT_DIR}/production-ready-nl2sql-quality-evaluation-worker.service" \
    "/usr/local/bin/uv run python -m app.cli.nl2sql_quality_evaluation_worker" \
    "Production Ready NL2SQL quality evaluation worker"

  write_systemd_unit \
    "${SYSTEMD_UNIT_DIR}/production-ready-nl2sql-ontology-worker.service" \
    "/usr/local/bin/uv run python -m app.features.nl2sql.ontology_worker" \
    "Production Ready NL2SQL ontology worker"

  # 合成データの worker は機能ごと削除した（#7）。以前の配備の unit が残っていれば止めて消す。
  if [ -f "${SYSTEMD_UNIT_DIR}/production-ready-nl2sql-synthetic-worker.service" ]; then
    systemctl disable --now production-ready-nl2sql-synthetic-worker.service || true
    rm -f "${SYSTEMD_UNIT_DIR}/production-ready-nl2sql-synthetic-worker.service"
  fi

  systemctl daemon-reload
  systemctl enable production-ready-nl2sql-backend.service
  systemctl restart production-ready-nl2sql-backend.service
  if [ "${DATABASE_INITIALIZATION_READY}" = "true" ]; then
    systemctl enable --now \
      production-ready-nl2sql-schema-refresh-worker.service \
      production-ready-nl2sql-quality-evaluation-worker.service \
      production-ready-nl2sql-ontology-worker.service
  else
    systemctl disable --now \
      production-ready-nl2sql-schema-refresh-worker.service \
      production-ready-nl2sql-quality-evaluation-worker.service \
      production-ready-nl2sql-ontology-worker.service || true
  fi
}

dump_service_diagnostics() {
  local service="$1"
  log "Diagnostics for ${service}: systemctl status"
  systemctl --no-pager --full status "${service}" || true
  log "Diagnostics for ${service}: recent journal"
  journalctl -u "${service}" -n 160 --no-pager || true
}

# 一定の間隔で待ち、backend が failed になるか再起動を繰り返したら、上限を待たずに status と journal を出す。
wait_for_backend() {
  if suite_wait_for_service_health production-ready-nl2sql-backend.service "http://${BACKEND_HOST}:${BACKEND_PORT}/api/health"; then
    return 0
  fi
  log "Backend did not become healthy on ${BACKEND_HOST}:${BACKEND_PORT}."
  dump_service_diagnostics production-ready-nl2sql-backend.service
  return 1
}

main() {
  log "Starting direct Compute initialization."
  install_system_packages
  install_nodejs
  install_uv
  prepare_filesystem
  install_runtime_env
  install_backend
  initialize_database_schema
  build_frontend
  configure_systemd
  wait_for_backend
  # URL（scheme・IP・port）は suite-init.sh の最後に出す（製品は HTTPS・公開の port を知らない。#1639）。
  log "Initialization complete. The URLs are listed at the end of /var/log/suite-init.log (suite: and each product)."
}

if [ "${NL2SQL_INIT_TEST_MODE:-false}" != "true" ]; then
  main "$@"
fi
