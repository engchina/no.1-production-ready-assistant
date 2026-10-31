#!/usr/bin/env bash
# OCI Resource Manager の統合 stack（terraform/stack、#217）の cloud-init から呼ばれる agent の Compute 初期化スクリプト。
# Agent Control Plane を Docker なしで Nginx + systemd に直接配備する。
# ADB の DDL は持たない。Runtime 状態の table は backend 起動時に Oracle repository が作成し、
# 共通認証（PLATFORM_*）と Agent のシステムテーブル（AGENT_*）はアプリの CLI（agent_system_schema）が作成する（#751）。
# ログインは共通認証（PLATFORM_AUTH_MODE=authenticated。構成管理者 system_admin と DB ユーザー。#215）。
# 3 製品は 1 台の Compute にセットで配備する（#1316。単独では配備しない）。platform/deploy/suite-init.sh が呼び、
# Nginx の site は suite が 1 つだけ書く（ここでは書かない）。frontend は FRONTEND_BASE_PATH（/agent/）を base に build する。
set -euo pipefail

if [ "${AGENT_INIT_TEST_MODE:-false}" != "true" ]; then
  exec > >(tee -a /var/log/agent-init.log) 2>&1
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
# Agent と共有 platform は monorepo（no.1-production-ready-assistant）の agent/ と platform/ にある。
SUITE_REPO_DIR="${APP_ROOT}/no.1-production-ready-assistant"
APP_REPO_DIR="${SUITE_REPO_DIR}/agent"
PLATFORM_REPO_DIR="${SUITE_REPO_DIR}/platform"
BACKEND_DIR="${APP_REPO_DIR}/backend"
FRONTEND_DIR="${APP_REPO_DIR}/frontend"
DATA_DIR="${DATA_DIR:-/u01/data/production-ready-agent}"
WALLET_DIR="${APP_ROOT}/wallet"
PROPS_DIR="${PROPS_DIR:-${APP_ROOT}/props}"
# backend/.env の元（suite では製品ごとの <製品>.backend.env）。
BACKEND_ENV_SOURCE="${BACKEND_ENV_SOURCE:-${PROPS_DIR}/backend.env}"
# frontend の base（Vite の base・React Router の basename）。Nginx の /agent/ の下で配信する（#1316）。
FRONTEND_BASE_PATH="${FRONTEND_BASE_PATH:-/agent/}"
# true なら共有 UI（platform）を build し直さない（suite で先の製品が build したとき）。
PR_SUITE_SKIP_PLATFORM_UI_BUILD="${PR_SUITE_SKIP_PLATFORM_UI_BUILD:-false}"
BACKEND_HOST="127.0.0.1"
BACKEND_PORT="8020"
# checkpoint repository は process 内に状態を持つため、gunicorn は 1 worker に固定する。
BACKEND_WORKERS="1"
NODE_MAJOR="24"
NODEJS_OFFICIAL_RELEASE_BASE_URL="${NODEJS_OFFICIAL_RELEASE_BASE_URL:-https://nodejs.org/download/release/latest-v${NODE_MAJOR}.x}"
NODEJS_OFFICIAL_INSTALL_DIR="${NODEJS_OFFICIAL_INSTALL_DIR:-/usr/local/lib/nodejs}"
NODEJS_OFFICIAL_BIN_DIR="${NODEJS_OFFICIAL_BIN_DIR:-/usr/local/bin}"
SYSTEMD_UNIT_DIR="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
BACKEND_SERVICE="production-ready-agent-backend.service"
DATABASE_INITIALIZATION_READY=false

PATH="/usr/local/bin:/usr/bin:/bin:${PATH}"

log() {
  printf '[agent-init] %s\n' "$*"
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

# Agent が使う OS の package（Nginx・firewalld は platform/deploy/suite-init.sh が入れる）。
# gcc・make は wheel の無い Python の依存を build するため。curl は image にある。
SYSTEM_PACKAGES=(ca-certificates gcc gcc-c++ git make openssl tar unzip)

install_system_packages() {
  log "Installing system packages: ${SYSTEM_PACKAGES[*]}"
  dnf_cmd install "${SYSTEM_PACKAGES[@]}"
}

# Node.js は Oracle Linux の AppStream の module を優先し、失敗時は公式 tarball（SHASUMS256 検証付き）へ fallback する。
# RAG / NL2SQL と CI と同じ Node.js 24 系を使う（#1591。以前は 22 で、Agent だけを配備すると CI と版がずれた）。
# 1 台の Compute（#1316）では RAG / NL2SQL が先に Node.js 24 を入れる。Agent は 24 以上なら使う
# （入れ替えると先に build した製品と版がずれる）。
node_version_accepted() {
  local version="$1"
  local major
  major="$(printf '%s\n' "${version}" | sed -n 's/^v\([0-9][0-9]*\)\..*/\1/p')"
  [ -n "${major}" ] || return 1
  [ "${major}" -ge "${NODE_MAJOR}" ]
}

install_nodejs() {
  local installed_version
  local npm_version

  if command -v node >/dev/null 2>&1; then
    installed_version="$(node --version)" || return 1
    if command -v npm >/dev/null 2>&1 && node_version_accepted "${installed_version}"; then
      npm_version="$(npm --version)" || return 1
      log "Node.js ${installed_version} with npm ${npm_version} is already installed."
      return
    fi
  fi

  if install_nodejs_from_dnf_module; then
    return
  fi

  log "AppStream Node.js ${NODE_MAJOR} installation failed; falling back to official Node.js tarball."
  install_nodejs_from_official_tarball || return 1
}

# Node.js（NODE_MAJOR）は Oracle Linux の AppStream の module（nodejs:<NODE_MAJOR>）から入れる。
install_nodejs_from_dnf_module() {
  log "Installing Node.js ${NODE_MAJOR} from the AppStream module (nodejs:${NODE_MAJOR})."
  dnf_cmd module reset nodejs || return 1
  dnf_cmd module enable "nodejs:${NODE_MAJOR}" || return 1
  dnf_cmd install nodejs npm || return 1
  validate_nodejs || return 1
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

  log "Installing Node.js ${NODE_MAJOR} official tarball for ${node_platform}."
  if ! curl -fsSL "${NODEJS_OFFICIAL_RELEASE_BASE_URL%/}/SHASUMS256.txt" -o "${shasums_path}"; then
    rm -rf "${tmp_dir}"
    return 1
  fi

  tarball_name="$(
    awk -v node_platform="${node_platform}" -v node_major="${NODE_MAJOR}" '
      {
        filename = $2
        sub("^[*]", "", filename)
        sub("^\\./", "", filename)
      }
      filename ~ ("^node-v" node_major "[.][0-9]+[.][0-9]+-" node_platform "[.]tar[.]gz$") {
        print filename
        exit
      }
    ' "${shasums_path}"
  )" || {
    rm -rf "${tmp_dir}"
    return 1
  }
  if [ -z "${tarball_name}" ]; then
    log "No official Node.js ${NODE_MAJOR} tarball found for ${node_platform}."
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
  local executable
  for executable in node npm npx corepack; do
    if [ ! -x "${NODEJS_OFFICIAL_INSTALL_DIR}/${node_dir}/bin/${executable}" ]; then
      log "Official Node.js tarball is missing ${executable}."
      rm -rf "${tmp_dir}"
      return 1
    fi
    ln -sfn "${NODEJS_OFFICIAL_INSTALL_DIR}/${node_dir}/bin/${executable}" "${NODEJS_OFFICIAL_BIN_DIR}/${executable}" || {
      rm -rf "${tmp_dir}"
      return 1
    }
  done

  rm -rf "${tmp_dir}"
  hash -r || true
  validate_nodejs || return 1
}

validate_nodejs() {
  local installed_version
  local npm_version

  if ! command -v node >/dev/null 2>&1; then
    log "node command was not installed."
    return 1
  fi

  installed_version="$(node --version)" || return 1
  if ! printf '%s\n' "${installed_version}" | grep -q "^v${NODE_MAJOR}\."; then
    log "Expected Node.js v${NODE_MAJOR}.x after installation, got ${installed_version}."
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

prepare_filesystem() {
  log "Preparing application directories."
  if ! id "${APP_USER}" >/dev/null 2>&1; then
    log "Application user ${APP_USER} does not exist."
    return 1
  fi

  chown "root:${APP_GROUP}" "${APP_ROOT}"
  chmod 0775 "${APP_ROOT}"
  install -d -m 0755 -o "${APP_USER}" -g "${APP_GROUP}" \
    "${DATA_DIR}" "${DATA_DIR}/bindings" "${DATA_DIR}/artifacts"
  install -d -m 0700 -o "${APP_USER}" -g "${APP_GROUP}" "${WALLET_DIR}"
  # 開発の worktree・一時ファイルは辿らない（配備の途中で消えると chown が失敗する。#2307）。
  suite_chown_repo "${APP_USER}:${APP_GROUP}" "${SUITE_REPO_DIR}"
  chown -R "${APP_USER}:${APP_GROUP}" "${DATA_DIR}" "${WALLET_DIR}"
}

install_runtime_env() {
  log "Installing platform/backend environment and wallet."
  # 3製品共通の設定（PLATFORM_*）はリポジトリの platform/.env、Agent 固有（AGENT_*）は backend/.env（#211）。
  # platform/.env はシステム設定画面の保存先でもあるため、既にあれば上書きしない（画面で保存した値を消さない）。
  if [ -e "${PLATFORM_REPO_DIR}/.env" ]; then
    log "Keeping existing shared platform environment: ${PLATFORM_REPO_DIR}/.env"
  else
    install -m 0600 -o "${APP_USER}" -g "${APP_GROUP}" "${PROPS_DIR}/platform.env" "${PLATFORM_REPO_DIR}/.env"
  fi
  install -m 0600 -o "${APP_USER}" -g "${APP_GROUP}" "${BACKEND_ENV_SOURCE}" "${BACKEND_DIR}/.env"

  rm -rf "${WALLET_DIR}"
  install -d -m 0700 -o "${APP_USER}" -g "${APP_GROUP}" "${WALLET_DIR}"
  unzip -oq "${PROPS_DIR}/wallet.zip" -d "${WALLET_DIR}"
  chown -R "${APP_USER}:${APP_GROUP}" "${WALLET_DIR}"
  find "${WALLET_DIR}" -type d -exec chmod 0700 {} \;
  find "${WALLET_DIR}" -type f -exec chmod 0600 {} \;
}

# 組み込みのスキル（Agent Skills の形式。repo 直下の skills/<name>/SKILL.md）は backend が起動時に読む。
# repo を丸ごと配備するので通常はあるが、欠けると backend が起動で止まるため、配備の途中で先に知らせる（#1816）。
BUILTIN_SKILLS_DIR="${SUITE_REPO_DIR}/skills"
BUILTIN_SKILL_NAMES=(business-rag-research structured-data-query rag-and-structured-data)

check_builtin_skills() {
  local name
  for name in "${BUILTIN_SKILL_NAMES[@]}"; do
    if [ ! -r "${BUILTIN_SKILLS_DIR}/${name}/SKILL.md" ]; then
      log "ERROR: built-in skill ${BUILTIN_SKILLS_DIR}/${name}/SKILL.md is missing or unreadable."
      return 1
    fi
  done
}

install_backend() {
  check_builtin_skills
  log "Installing backend dependencies with uv."
  run_as_app_user_in_dir "${BACKEND_DIR}" "uv python install 3.12"
  run_as_app_user_in_dir "${BACKEND_DIR}" "uv sync --locked --no-dev --python 3.12"
}

# 共通認証（PLATFORM_*）と Agent のシステムテーブル（権限・Run の保存先・定義。AGENT_*）は
# agent_system_schema --initialize が冪等に作る（#215 / #751 / #764。接続は PLATFORM_ORACLE_*）。
# データを消す migration（旧版の検索・回答プロファイルの表の削除など）は自動では承認せず、
# 運用設定 > システムテーブル で内容を確認して承認する。
initialize_database_schema() {
  local security_ready=false

  log "Applying Agent system tables (idempotent)."
  if retry_command 5 run_as_app_user_in_dir "${BACKEND_DIR}" \
    "uv run python -m app.cli.agent_system_schema --initialize"; then
    security_ready=true
    log "Agent system tables are ready."
  else
    log "WARNING: Agent system table initialization failed. If it requires approval of a data-removing migration, open Operations settings > System tables. Login returns SECURITY_SCHEMA_MIGRATION_REQUIRED until the tables exist."
  fi

  if [ "${security_ready}" = "true" ]; then
    DATABASE_INITIALIZATION_READY=true
    return 0
  fi

  DATABASE_INITIALIZATION_READY=false
  log "WARNING: Database initialization is incomplete. Check ADB reachability, backend/.env and platform/.env (PLATFORM_ORACLE_*)."
  log "Recovery: cd ${BACKEND_DIR} && sudo -u ${APP_USER} /usr/local/bin/uv run python -m app.cli.agent_system_schema --initialize"
  log "Recovery: sudo systemctl restart ${BACKEND_SERVICE}"
  # backend は起動する（画面から直せる）が、suite-init.sh の最後の確認は schema を NG にして非 0 で終わる（#1648）。
  log "WARNING: The deployment check at the end of suite-init.sh reports 'check: agent schema NG' until the schema is ready."
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

  log "Building Agent frontend (base ${FRONTEND_BASE_PATH})."
  run_as_app_user_in_dir "${FRONTEND_DIR}" "npm ci"
  run_as_app_user_in_dir "${FRONTEND_DIR}" "FRONTEND_BASE_PATH='${FRONTEND_BASE_PATH}' npm run build"
}

configure_systemd() {
  log "Writing systemd unit."
  cat > "${SYSTEMD_UNIT_DIR}/${BACKEND_SERVICE}" <<EOF
[Unit]
Description=Production Ready Agent Control Plane backend
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
ExecStart=/usr/local/bin/uv run gunicorn app.main:app --worker-class uvicorn.workers.UvicornWorker --bind ${BACKEND_HOST}:${BACKEND_PORT} --workers ${BACKEND_WORKERS} --timeout 300
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "${BACKEND_SERVICE}"
  systemctl restart "${BACKEND_SERVICE}"
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
  if suite_wait_for_service_health "${BACKEND_SERVICE}" "http://${BACKEND_HOST}:${BACKEND_PORT}/api/health"; then
    return 0
  fi
  log "Backend did not become healthy on ${BACKEND_HOST}:${BACKEND_PORT}."
  if [ "${DATABASE_INITIALIZATION_READY}" != "true" ]; then
    log "The Agent Runtime Oracle repository was not ready; check ADB reachability and backend/.env."
  fi
  dump_service_diagnostics "${BACKEND_SERVICE}"
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
  log "Initialization complete. The URLs are listed at the end of /var/log/suite-init.log (suite: and each product). Log in as system_admin (PLATFORM_ADMIN_LOGIN_USER_PASSWORD)."
}

if [ "${AGENT_INIT_TEST_MODE:-false}" != "true" ]; then
  main "$@"
fi
