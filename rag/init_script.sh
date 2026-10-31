#!/usr/bin/env bash
# OCI Resource Manager の統合 stack（terraform/stack、#217）の cloud-init から呼ばれる rag の Compute 初期化スクリプト。
# RAG は Docker を使わず、NL2SQL / Agent と同じくネイティブ（uv の venv + systemd + Nginx）で動かす（#286）。
#   - backend / ingestion-worker: rag/backend の venv。backend は 127.0.0.1:8000 だけで listen する
#   - 前処理 / CPU parser: サービスごとの venv（rag/services/*/*/.venv）と systemd の unit。127.0.0.1:<port>
#     （unit の定義は rag/scripts/rag-systemd.sh。開発環境の rag/scripts/rag-services.sh と共通）
#   - frontend: host の Node.js で静的 build する。配信と /rag/api/ の proxy は suite の Nginx（platform/deploy）
# アプリのプロセスは専用のユーザー（SERVICE_USER）で動かし、backend にはサービス管理画面から操作する
# unit の systemctl / journalctl だけを sudoers で許可する。
# 前処理 / parser の起動 / 停止は利用者が最後に操作した状態（systemd の enable / disable）を保ち、
# 再配備・再起動でもその状態に戻す。初めて配備する unit は起動する（既定は停止のサービスを除く）。
# ADB の DDL は持たない。RAG の system schema はアプリの CLI（app.rag.system_schema_cli）で適用する。
# 3 製品は 1 台の Compute にセットで配備する（#1316。単独では配備しない）。platform/deploy/suite-init.sh が呼び、
# Nginx の site は suite が 1 つだけ書く（ここでは書かない）。frontend は FRONTEND_BASE_PATH（/rag/）を base に build し、
# プロセスは NL2SQL / Agent と同じユーザー（SERVICE_USER=opc。suite が渡す）で動かす（共通の platform/.env・Wallet を共有するため）。
set -euo pipefail

if [ "${RAG_INIT_TEST_MODE:-false}" != "true" ]; then
  exec > >(tee -a /var/log/rag-init.log) 2>&1
fi

INIT_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source-path=SCRIPTDIR source=scripts/rag-systemd.sh
source "${INIT_SCRIPT_DIR}/scripts/rag-systemd.sh"
# backend の起動の待ち方（suite_wait_for_service_health。#1642）は platform の配備の共通の helper（製品は単独では配備しない）。
# shellcheck source-path=SCRIPTDIR source=../platform/deploy/suite-health.sh
source "${INIT_SCRIPT_DIR}/../platform/deploy/suite-health.sh"
# shellcheck source-path=SCRIPTDIR source=../platform/deploy/suite-repo.sh
source "${INIT_SCRIPT_DIR}/../platform/deploy/suite-repo.sh"

APP_ROOT="${APP_ROOT:-/u01/aipoc}"
# リポジトリの所有者（git pull・uv sync・frontend の build を行う）。
APP_USER="${APP_USER:-opc}"
APP_GROUP="${APP_GROUP:-${APP_USER}}"
# RAG のプロセスを動かす専用のユーザー。APP_USER（OCI の Oracle Linux の opc は sudo を無制限に持つ）とは分け、
# sudo は sudoers で許可した unit の操作だけにする。
SERVICE_USER="${SERVICE_USER:-ragsvc}"
SERVICE_GROUP="${SERVICE_GROUP:-${SERVICE_USER}}"
SERVICE_HOME="${SERVICE_HOME:-/var/lib/production-ready-rag}"
# RAG と共有 platform は monorepo（no.1-production-ready-suite）の rag/ と platform/ にある。
SUITE_REPO_DIR="${APP_ROOT}/no.1-production-ready-suite"
APP_REPO_DIR="${SUITE_REPO_DIR}/rag"
BACKEND_DIR="${APP_REPO_DIR}/backend"
# 3製品共通の設定（PLATFORM_*）の共通 .env（#211）。システム設定画面の保存先（model-settings.json も同じ場所）。
PLATFORM_DIR="${SUITE_REPO_DIR}/platform"
PLATFORM_ENV_FILE="${PLATFORM_DIR}/.env"
FRONTEND_DIR="${APP_REPO_DIR}/frontend"
# アップロード原本の保存先（backend の PLATFORM_LOCAL_STORAGE_DIR の既定値）。
DATA_DIR="${DATA_DIR:-/u01/data/production-ready-rag}"
# サービス管理が起動 / 再起動の前に書く、マイクロサービスの実行用 env（backend の RAG_SERVICE_RUNTIME_ENV_FILE の既定値）。
SERVICE_RUNTIME_ENV_FILE="${BACKEND_DIR}/service-runtime.env"
WALLET_DIR="${APP_ROOT}/wallet"
PROPS_DIR="${PROPS_DIR:-${APP_ROOT}/props}"
# backend/.env の元（suite では製品ごとの <製品>.backend.env）。
BACKEND_ENV_SOURCE="${BACKEND_ENV_SOURCE:-${PROPS_DIR}/backend.env}"
# frontend の base（Vite の base・React Router の basename）。Nginx の /rag/ の下で配信する（#1316）。
FRONTEND_BASE_PATH="${FRONTEND_BASE_PATH:-/rag/}"
# true なら共有 UI（platform）を build し直さない（suite で先の製品が build したとき）。
PR_SUITE_SKIP_PLATFORM_UI_BUILD="${PR_SUITE_SKIP_PLATFORM_UI_BUILD:-false}"
BACKEND_HOST="127.0.0.1"
BACKEND_PORT="8000"
SYSTEMD_UNIT_DIR="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
SUDOERS_DIR="${SUDOERS_DIR:-/etc/sudoers.d}"
# uv が入れる Python は、SERVICE_USER からも読める共有の場所に置く（APP_USER の home は他のユーザーが読めない）。
UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-/opt/uv/python}"
BACKEND_UNIT="production-ready-rag-backend.service"
WORKER_UNIT="production-ready-rag-ingestion-worker.service"
# 取込 worker の停止: SIGTERM を受けたら実行中の job を grace まで待ち、終わらなければ子を止めて
# （SIGTERM から 10 秒で SIGKILL）自分の lease の job を QUEUED に戻す（#357）。systemd の
# TimeoutStopSec は grace + 子の停止待ち + 戻す DB の処理より長くする（テストで照合する）。
WORKER_SHUTDOWN_GRACE_SECONDS=60
WORKER_TIMEOUT_STOP_SEC=90
NODEJS_OFFICIAL_RELEASE_BASE_URL="${NODEJS_OFFICIAL_RELEASE_BASE_URL:-https://nodejs.org/download/release/latest-v24.x}"
NODEJS_OFFICIAL_INSTALL_DIR="${NODEJS_OFFICIAL_INSTALL_DIR:-/usr/local/lib/nodejs}"
NODEJS_OFFICIAL_BIN_DIR="${NODEJS_OFFICIAL_BIN_DIR:-/usr/local/bin}"
# 以前の Docker Compose の配備（#286 より前）。見つかれば止めて、データを移す。
LEGACY_COMPOSE_UNIT="production-ready-rag.service"
LEGACY_COMPOSE_WRAPPER="${LEGACY_COMPOSE_WRAPPER:-/usr/local/bin/rag-compose}"
LEGACY_COMPOSE_PROJECT="production-ready-rag"
DOCKER_VOLUMES_DIR="${DOCKER_VOLUMES_DIR:-/var/lib/docker/volumes}"
DATABASE_INITIALIZATION_READY=false

# この stack が配備してよい前処理 / parser（CPU / OCI だけ。並びは scripts/rag-systemd.sh の RAG_MICROSERVICES と同じ）。
# rag_services.txt に、提供するサービス（RAG_MICROSERVICES）のうちこれ以外の名前があれば失敗させる。
# 提供しなくなったサービス（RAG_MICROSERVICES に無い名前）と準備中（ロードマップ）のサービス
# （rag-systemd.sh の RAG_ROADMAP_SERVICES。#3374）は、以前の stack が書いた rag_services.txt に
# 残っていても読み飛ばす（その unit は configure_systemd が止めて消し、sudoers からも外す）。
ALLOWED_RAG_SERVICES=(
  preprocess-office-to-pdf
  preprocess-csv-to-jsonl
  preprocess-excel-to-jsonl
  preprocess-word-to-jsonl
  parser-docling
  parser-unstructured
  parser-structured-records-jsonl
  parser-xml-tree-json
)
# 必ず配備する parser（既定の解析エンジンの Docling と、使用エンジンに選べる構造化レコード JSONL・XML ツリー JSON）と、
# Terraform の stack が常に配備する前処理 CSV→JSONL・Excel→JSONL・Word→JSONL（#2457。terraform/stack/locals.tf の rag_services）。
# rag_services.txt に無ければ足す（以前の stack が書いた一覧や、#2484 の前の再配備で CSV / Excel が消えた一覧でも、再配備で unit が入る。
# #2486）。初めて配備したときは起動する。XML ツリー JSON は既定は停止のサービスで、初回は起動しない（#2453）。
REQUIRED_RAG_SERVICES=(parser-docling parser-structured-records-jsonl parser-xml-tree-json preprocess-csv-to-jsonl preprocess-excel-to-jsonl preprocess-word-to-jsonl)
# 改名したサービス（旧名|新名。#2457 の CSV→JSONL・Excel→JSONL）。以前の stack が書いた rag_services.txt の旧名は新名として読み、
# 旧名の unit の起動 / 停止の状態を新名の unit に引き継ぐ（configure_systemd。旧名の unit は配備しなくなった unit として消す）。
RENAMED_RAG_SERVICES=(
  "preprocess-csv-to-json|preprocess-csv-to-jsonl"
  "preprocess-excel-to-json|preprocess-excel-to-jsonl"
)
RAG_SERVICES=()

PATH="/usr/local/bin:/usr/bin:/bin:${PATH}"

log() {
  printf '[rag-init] %s\n' "$*"
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
  runuser -u "${APP_USER}" -- env PATH="${PATH}" UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR}" "$@"
}

run_as_app_user_in_dir() {
  local workdir="$1"
  shift
  run_as_app_user bash -lc "cd '${workdir}' && $*"
}

run_as_service_user_in_dir() {
  local workdir="$1"
  shift
  runuser -u "${SERVICE_USER}" -- env PATH="${PATH}" HOME="${SERVICE_HOME}" \
    bash -c "cd '${workdir}' && $*"
}

contains_value() {
  local needle="$1"
  shift
  local value
  for value in "$@"; do
    if [ "${value}" = "${needle}" ]; then
      return 0
    fi
  done
  return 1
}

# 改名したサービスの新名（旧名でなければ失敗）。
rag_service_new_name() {
  local entry
  for entry in "${RENAMED_RAG_SERVICES[@]}"; do
    if [ "${entry%%|*}" = "$1" ]; then
      printf '%s\n' "${entry#*|}"
      return 0
    fi
  done
  return 1
}

# 改名したサービスの旧名（新名でなければ失敗）。
rag_service_old_name() {
  local entry
  for entry in "${RENAMED_RAG_SERVICES[@]}"; do
    if [ "${entry#*|}" = "$1" ]; then
      printf '%s\n' "${entry%%|*}"
      return 0
    fi
  done
  return 1
}

# 配備する前処理 / parser（Terraform の rag_services）。以前の stack の compose_services.txt も読む
# （backend / ingestion-worker は常に配備するため、その名前は読み飛ばす）。
load_rag_services() {
  local services_file="${PROPS_DIR}/rag_services.txt"
  local names=()
  local name new_name

  if [ ! -f "${services_file}" ] && [ -f "${PROPS_DIR}/compose_services.txt" ]; then
    log "Reading the service list written by an older stack: ${PROPS_DIR}/compose_services.txt"
    services_file="${PROPS_DIR}/compose_services.txt"
  fi
  read -r -a names <<< "$(tr '\n' ' ' < "${services_file}" 2>/dev/null || true)"
  RAG_SERVICES=()
  for name in "${names[@]}"; do
    case "${name}" in
      backend | ingestion-worker)
        continue
        ;;
    esac
    if new_name="$(rag_service_new_name "${name}")"; then
      log "Reading renamed RAG service as its new name: ${name} -> ${new_name}"
      name="${new_name}"
    fi
    if ! rag_service_definition "${name}" >/dev/null; then
      log "Skipping RAG service that is no longer provided: ${name}"
      continue
    fi
    if rag_service_roadmap "${name}"; then
      log "Skipping RAG service that is on the roadmap (not available yet): ${name}"
      continue
    fi
    if ! contains_value "${name}" "${ALLOWED_RAG_SERVICES[@]}"; then
      log "RAG service is not allowed by this stack: ${name}"
      return 1
    fi
    if ! contains_value "${name}" "${RAG_SERVICES[@]}"; then
      RAG_SERVICES+=("${name}")
    fi
  done
  for name in "${REQUIRED_RAG_SERVICES[@]}"; do
    if ! contains_value "${name}" "${RAG_SERVICES[@]}"; then
      log "Adding required RAG service missing from ${services_file}: ${name}"
      RAG_SERVICES+=("${name}")
    fi
  done
  log "RAG services: ${RAG_SERVICES[*]}"
  # 一覧を直したら rag_services.txt にも書き戻す（次の再配備・調査で今の配備と同じ一覧が見えるように）。
  # 以前の stack の compose_services.txt は書き換えない。書けなくても配備は続ける（読み込みで毎回直すため）。
  if [ "${services_file}" = "${PROPS_DIR}/rag_services.txt" ] && [ -f "${services_file}" ] \
    && [ "${names[*]}" != "${RAG_SERVICES[*]}" ]; then
    if write_rag_services_file "${services_file}"; then
      log "Updated ${services_file}: ${RAG_SERVICES[*]}"
    else
      log "WARNING: Could not update ${services_file}; continuing with: ${RAG_SERVICES[*]}"
    fi
  fi
}

# rag_services.txt を RAG_SERVICES で置き換える（同じディレクトリの一時ファイルから mv で原子的に。所有者と mode を保つ）。
write_rag_services_file() {
  local services_file="$1"
  local tmp_file
  tmp_file="$(mktemp "${services_file}.XXXXXX")" || return 1
  if printf '%s\n' "${RAG_SERVICES[*]}" > "${tmp_file}" \
    && chmod --reference="${services_file}" -- "${tmp_file}" \
    && { [ "$(id -u)" -ne 0 ] || chown --reference="${services_file}" -- "${tmp_file}"; } \
    && mv -f -- "${tmp_file}" "${services_file}"; then
    return 0
  fi
  rm -f -- "${tmp_file}"
  return 1
}

# 前処理 / parser が使う OS のコマンド（LibreOffice・poppler・tesseract・CJK フォント）。Oracle Linux 9 の
# AppStream / BaseOS の package（EPEL・CodeReady Builder は要らない。#1611）。
service_os_packages() {
  case "$1" in
    preprocess-office-to-pdf)
      printf '%s\n' libreoffice-core libreoffice-writer libreoffice-impress libreoffice-calc \
        google-noto-sans-cjk-ttc-fonts google-noto-serif-cjk-ttc-fonts
      ;;
    preprocess-word-to-jsonl)
      # .doc を .docx にするだけなので Writer だけでよい（#2457）。
      printf '%s\n' libreoffice-core libreoffice-writer
      ;;
    parser-unstructured)
      printf '%s\n' poppler-utils tesseract tesseract-langpack-eng tesseract-langpack-jpn
      ;;
  esac
}

# RAG が使う OS の package（Nginx・firewalld は platform/deploy/suite-init.sh が入れる）。gcc・make は wheel の
# 無い Python の依存を build するため。curl は image にある。
BASE_SYSTEM_PACKAGES=(ca-certificates gcc gcc-c++ git make openssl sudo tar unzip)

# 入れる package（基本のものと、配備する前処理 / parser が使うもの）。1 行に 1 つ。
system_packages() {
  local packages=("${BASE_SYSTEM_PACKAGES[@]}")
  local service package
  for service in "${RAG_SERVICES[@]}"; do
    while IFS= read -r package; do
      if [ -n "${package}" ] && ! contains_value "${package}" "${packages[@]}"; then
        packages+=("${package}")
      fi
    done < <(service_os_packages "${service}")
  done
  printf '%s\n' "${packages[@]}"
}

install_system_packages() {
  local packages=()
  mapfile -t packages < <(system_packages)
  log "Installing system packages: ${packages[*]}"
  # 推奨の package（weak dependency）は入れない（以前の apt の --no-install-recommends と同じ）。
  dnf_cmd install --setopt=install_weak_deps=False "${packages[@]}"
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
  # 全サービスの venv が使う Python 3.12 を、SERVICE_USER も読める共有の場所に入れる。
  install -d -m 0755 "${UV_PYTHON_INSTALL_DIR}"
  retry_command 3 env UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR}" uv python install "${RAG_PYTHON_VERSION}"
  chmod -R a+rX "${UV_PYTHON_INSTALL_DIR}"
}

# RAG のプロセスを動かす専用のユーザー（login できない system user）。
prepare_service_user() {
  if id "${SERVICE_USER}" >/dev/null 2>&1; then
    log "Service user ${SERVICE_USER} already exists."
  else
    log "Creating service user ${SERVICE_USER}."
    useradd --system --user-group --create-home --home-dir "${SERVICE_HOME}" \
      --shell /usr/sbin/nologin "${SERVICE_USER}"
  fi
  install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${SERVICE_HOME}"
}

prepare_filesystem() {
  log "Preparing application directories."
  if ! id "${APP_USER}" >/dev/null 2>&1; then
    log "Application user ${APP_USER} does not exist."
    return 1
  fi

  chown "root:${APP_GROUP}" "${APP_ROOT}"
  chmod 0775 "${APP_ROOT}"
  install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${DATA_DIR}"
  # git pull・uv sync・frontend の build は APP_USER が行う。
  # 開発の worktree・一時ファイルは辿らない（配備の途中で消えると chown が失敗する。#2307）。
  suite_chown_repo "${APP_USER}:${APP_GROUP}" "${SUITE_REPO_DIR}"
  # 画面から保存する .env（backend/.env・platform/.env・model-settings.json・backend/parser-settings.json）は、同じディレクトリの
  # 一時ファイルと lock で置き換えるため、SERVICE_USER が backend/ と platform/ に書けるようにする。
  chgrp "${SERVICE_GROUP}" "${BACKEND_DIR}" "${PLATFORM_DIR}"
  chmod 2775 "${BACKEND_DIR}" "${PLATFORM_DIR}"
  restore_service_owned_files
}

# SERVICE_USER が書くファイル（画面の保存先・サービス実行用 env）の所有者を戻す（chown -R の後）。
restore_service_owned_files() {
  local path
  for path in \
    "${PLATFORM_ENV_FILE}" \
    "${PLATFORM_DIR}/model-settings.json" \
    "${PLATFORM_DIR}/model-settings.json.lock" \
    "${BACKEND_DIR}/.env" \
    "${BACKEND_DIR}/parser-settings.json" \
    "${BACKEND_DIR}/parser-settings.json.lock" \
    "${SERVICE_RUNTIME_ENV_FILE}"; do
    if [ -e "${path}" ]; then
      chown "${SERVICE_USER}:${SERVICE_GROUP}" "${path}"
    fi
  done
}

# 以前の Docker Compose の配備（production-ready-rag.service と rag-compose）を止め、コンテナの
# named volume に残したデータ（アップロード原本・OCI の設定）を host の場所へ移す（#286）。
# Docker 自体と image / volume は消さない（移行を確かめた後に利用者が消す。rag/docs/deployment.md）。
migrate_from_docker_compose() {
  local legacy_unit_path="${SYSTEMD_UNIT_DIR:?}/${LEGACY_COMPOSE_UNIT:?}"
  if [ -f "${legacy_unit_path}" ]; then
    log "Stopping the previous Docker Compose deployment (${LEGACY_COMPOSE_UNIT})."
    systemctl disable --now "${LEGACY_COMPOSE_UNIT}" || true
    rm -f -- "${legacy_unit_path}"
    systemctl daemon-reload
  fi
  if [ -x "${LEGACY_COMPOSE_WRAPPER}" ]; then
    # restart policy 付きのコンテナが port 8000 を掴んだまま残らないよう、コンテナを消す（volume は残す）。
    "${LEGACY_COMPOSE_WRAPPER}" down --remove-orphans || true
    mv -f "${LEGACY_COMPOSE_WRAPPER}" "${LEGACY_COMPOSE_WRAPPER}.legacy-docker" || true
  fi
  copy_legacy_volume "${LEGACY_COMPOSE_PROJECT}_backend-local-storage" "${DATA_DIR}"
  copy_legacy_volume "${LEGACY_COMPOSE_PROJECT}_oci-config" "${SERVICE_HOME}/.oci"
}

copy_legacy_volume() {
  local volume="$1"
  local dest="$2"
  local source_dir="${DOCKER_VOLUMES_DIR}/${volume}/_data"

  if [ ! -d "${source_dir}" ]; then
    return 0
  fi
  if [ -d "${dest}" ] && find "${dest}" -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    log "Keeping ${dest}; it already has data (legacy Docker volume ${volume} is left as is)."
    return 0
  fi
  log "Copying legacy Docker volume ${volume} to ${dest}."
  install -d -m 0700 "${dest}"
  cp -a "${source_dir}/." "${dest}/"
  chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "${dest}"
}

# instance 上で1回だけ生成し、再実行でも同じ値を使う（ログイン中の session を保つため）。
ensure_generated_secret() {
  local path="$1"
  if [ ! -s "${path}" ]; then
    (umask 077 && openssl rand -hex 32 > "${path}")
  fi
  chmod 0600 "${path}"
  tr -d '[:space:]' < "${path}"
}

set_env_value() {
  local env_file="$1"
  local key="$2"
  local value="$3"
  if grep -q "^${key}=" "${env_file}"; then
    sed -i "s|^${key}=.*$|${key}=${value}|" "${env_file}"
  else
    printf '%s=%s\n' "${key}" "${value}" >> "${env_file}"
  fi
}

# rag/backend/.env（RAG_*）は Resource Manager の入力から作り、空の RAG_AUDIT_CONTEXT_HASH_SALT だけを
# instance 上で生成した値で補う。platform/.env（PLATFORM_*、3製品共通）はシステム設定画面の保存先でもあるため、
# 既にあれば上書きしない（画面で保存した API key などを再実行で消さない）。どちらも SERVICE_USER だけが読む。
install_runtime_env() {
  local env_file="${BACKEND_DIR}/.env"
  local current

  if [ -e "${PLATFORM_ENV_FILE}" ]; then
    log "Keeping existing shared platform environment: ${PLATFORM_ENV_FILE}"
  else
    log "Installing shared platform environment."
    install -m 0600 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${PROPS_DIR}/platform.env" "${PLATFORM_ENV_FILE}"
  fi
  chown "${SERVICE_USER}:${SERVICE_GROUP}" "${PLATFORM_ENV_FILE}"
  chmod 0600 "${PLATFORM_ENV_FILE}"

  log "Installing backend environment."
  install -m 0600 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${BACKEND_ENV_SOURCE}" "${env_file}"

  current="$(sed -n 's/^RAG_AUDIT_CONTEXT_HASH_SALT=//p' "${env_file}" | tail -n 1)"
  if [ -z "${current}" ]; then
    set_env_value "${env_file}" RAG_AUDIT_CONTEXT_HASH_SALT "$(ensure_generated_secret "${PROPS_DIR}/audit_context_hash_salt")"
  fi
}

install_wallet() {
  log "Installing wallet for ${SERVICE_USER}."
  rm -rf -- "${WALLET_DIR:?}"
  install -d -m 0700 -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${WALLET_DIR}"
  unzip -oq "${PROPS_DIR}/wallet.zip" -d "${WALLET_DIR}"
  chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "${WALLET_DIR}"
  find "${WALLET_DIR}" -type d -exec chmod 0700 {} \;
  find "${WALLET_DIR}" -type f -exec chmod 0600 {} \;
}

# backend と前処理 / parser の venv をサービスごとに作る（uv の lock どおり。Python 3.12）。
install_service_venvs() {
  local service dir model_command
  local sync_args=()

  log "Installing backend dependencies with uv."
  retry_command 3 run_as_app_user_in_dir "${BACKEND_DIR}" "uv sync --locked --no-dev --python ${RAG_PYTHON_VERSION}"

  for service in "${RAG_SERVICES[@]}"; do
    dir="${APP_REPO_DIR}/$(rag_service_dir "${service}")"
    mapfile -t sync_args < <(rag_uv_sync_args "${service}")
    log "Installing ${service} dependencies with uv."
    retry_command 3 run_as_app_user_in_dir "${dir}" "uv ${sync_args[*]}"
    model_command="$(rag_service_model_command "${service}")"
    if [ -n "${model_command}" ] && ! retry_command 3 run_as_service_user_in_dir "${dir}" "${model_command}"; then
      log "WARNING: ${service} could not download its models now; it will download them on first use."
    fi
  done
}

# 画面から保存した global の設定（backend/.env・parser-settings.json）の旧い名前・値を移す（冪等）。
# ファイル準備 csv_to_json / excel_to_json の改名と、固定分割符の既定の変更（#2457）。DB の保存値は
# system schema の migration が移す。
migrate_saved_settings() {
  log "Migrating saved RAG settings (idempotent)."
  if ! run_as_service_user_in_dir "${BACKEND_DIR}" \
    ".venv/bin/python -m app.rag.structured_records_settings_migration --apply"; then
    log "WARNING: The saved RAG settings could not be migrated. See docs/deployment.md (#2457)."
  fi
}

# RAG の system schema（table / vector index / Oracle Text）はアプリの CLI が冪等に作成・更新する。
# 失敗しても backend は起動でき、画面（システム設定 > データベース > RAG システムテーブル）からも初期化できる。
initialize_database_schema() {
  log "Initializing RAG system schema (idempotent)."
  if retry_command 5 run_as_service_user_in_dir "${BACKEND_DIR}" \
    ".venv/bin/python -m app.rag.system_schema_cli initialize"; then
    DATABASE_INITIALIZATION_READY=true
    log "RAG system schema is ready."
    return 0
  fi

  DATABASE_INITIALIZATION_READY=false
  log "WARNING: RAG system schema initialization failed. Check ADB reachability and ${PLATFORM_ENV_FILE}."
  log "Recovery: cd ${BACKEND_DIR} && sudo -u ${SERVICE_USER} HOME=${SERVICE_HOME} .venv/bin/python -m app.rag.system_schema_cli initialize"
  log "Recovery: or open System settings > Database > RAG system tables in the application."
  # データを削除する未適用の migration は承認が無ければ適用しない（#619。docs/deployment.md）。
  log "If status shows pending_destructive_migrations, export the data first (docs/deployment.md), then run initialize --allow-destructive."
  # backend は起動する（画面から直せる）が、suite-init.sh の最後の確認は schema を NG にして非 0 で終わる（#1648）。
  log "WARNING: The deployment check at the end of suite-init.sh reports 'check: rag schema NG' until the schema is ready."
  return 0
}

# frontend は file:../../platform/packages/ui に依存するため、platform を先に build する。
build_frontend() {
  if [ "${PR_SUITE_SKIP_PLATFORM_UI_BUILD}" = "true" ]; then
    log "Shared UI package was already built by another product."
  else
    log "Building shared UI package."
    run_as_app_user_in_dir "${PLATFORM_DIR}" "npm ci"
    run_as_app_user_in_dir "${PLATFORM_DIR}" "npm run build"
  fi

  log "Building RAG frontend (base ${FRONTEND_BASE_PATH})."
  run_as_app_user_in_dir "${FRONTEND_DIR}" "npm ci"
  run_as_app_user_in_dir "${FRONTEND_DIR}" "FRONTEND_BASE_PATH='${FRONTEND_BASE_PATH}' npm run build"
  test -f "${FRONTEND_DIR}/dist/index.html"
}

# backend と ingestion-worker の unit。環境と認証は platform/.env の共通設定を読む。
write_backend_unit() {
  local unit_path="$1"
  local description="$2"
  local exec_start="$3"
  local extra="$4"

  cat > "${unit_path}" <<EOF
# rag/init_script.sh が生成する（手で編集しない）。#286
[Unit]
Description=${description}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=${BACKEND_DIR}
Environment=HOME=${SERVICE_HOME}
Environment=PYTHONUNBUFFERED=1
Environment=RAG_INGESTION_QUEUE_DEDICATED_WORKER_ENABLED=true
Environment=RAG_PARSER_SERVICE_TIMEOUT_SECONDS=1230
Environment=RAG_PREPROCESS_ENABLED=true
${extra}ExecStart=${exec_start}

[Install]
WantedBy=multi-user.target
EOF
}

# 前処理 / parser の unit の起動 / 停止は、利用者が最後に操作した状態（systemd の enable / disable）を保つ。
#   - 初めて配備する unit（unit ファイルが無かった）: enable --now（既定のサービスを起動する）。
#     既定は停止のサービス（rag-systemd.sh の RAG_DEFAULT_STOPPED_SERVICES）は enable も起動もしない
#     改名したサービス（RENAMED_RAG_SERVICES）で旧名の unit が有れば、旧名の unit の enable / disable を引き継ぐ
#   - 以前から有る unit で enable（最後に「起動」した）: 新しいコードで restart
#   - 以前から有る unit で disable（最後に「停止」した）: 停止のまま
# reboot では enable の unit だけが起動する（WantedBy=multi-user.target）。
configure_systemd() {
  local service unit
  local new_units=()
  local renamed_started_units=()
  local renamed_stopped_units=()
  local keep_units=("${BACKEND_UNIT}" "${WORKER_UNIT}")
  local controllable_units=()
  local path name old_name old_unit

  log "Writing systemd units."
  write_backend_unit \
    "${SYSTEMD_UNIT_DIR}/${BACKEND_UNIT}" \
    "Production Ready RAG backend" \
    "${BACKEND_DIR}/.venv/bin/gunicorn app.main:app --worker-class uvicorn.workers.UvicornWorker --bind ${BACKEND_HOST}:${BACKEND_PORT} --workers 2 --timeout 60 --graceful-timeout 30 --keep-alive 5 --error-logfile - --no-control-socket" \
    "Environment=RAG_INGESTION_QUEUE_INPROCESS_WORKER_ENABLED=false
Environment=RAG_PARSER_READINESS_PROBE_ENABLED=true
Restart=always
RestartSec=5
"
  # 取込キューの consumer はこの unit だけ（backend の in-process worker は上で無効にする）。
  # SIGTERM は main process だけに送り、子プロセスは worker が後始末する（#305 / #357）。
  write_backend_unit \
    "${SYSTEMD_UNIT_DIR}/${WORKER_UNIT}" \
    "Production Ready RAG ingestion worker" \
    "${BACKEND_DIR}/.venv/bin/python -m app.rag.ingestion_worker" \
    "Environment=RAG_INGESTION_QUEUE_SHUTDOWN_GRACE_SECONDS=${WORKER_SHUTDOWN_GRACE_SECONDS}
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=${WORKER_TIMEOUT_STOP_SEC}
Restart=on-failure
RestartSec=5
"

  for service in "${RAG_SERVICES[@]}"; do
    unit="$(rag_unit_name "${service}")"
    if [ ! -f "${SYSTEMD_UNIT_DIR}/${unit}" ]; then
      new_units+=("${unit}")
      # 旧名の unit の状態は、下で旧名の unit を消す前に読む。
      if old_name="$(rag_service_old_name "${service}")"; then
        old_unit="$(rag_unit_name "${old_name}")"
        if [ -f "${SYSTEMD_UNIT_DIR}/${old_unit}" ]; then
          if systemctl is-enabled --quiet "${old_unit}"; then
            renamed_started_units+=("${unit}")
          else
            renamed_stopped_units+=("${unit}")
          fi
        fi
      fi
    fi
    rag_write_microservice_unit "${SYSTEMD_UNIT_DIR}/${unit}" "${service}" "${APP_REPO_DIR}" \
      "${SERVICE_USER}" "${SERVICE_GROUP}" "${SERVICE_HOME}" "${SERVICE_RUNTIME_ENV_FILE}" \
      "${PLATFORM_ENV_FILE}" "${BACKEND_DIR}/.env"
    keep_units+=("${unit}")
    controllable_units+=("${unit}")
  done

  # stack で選ばなくなったサービスの unit は止めて消す。
  for path in "${SYSTEMD_UNIT_DIR:?}/${RAG_UNIT_PREFIX:?}"*.service; do
    [ -e "${path}" ] || continue
    name="$(basename "${path}")"
    if ! contains_value "${name}" "${keep_units[@]}"; then
      log "Removing systemd unit that is no longer deployed: ${name}"
      systemctl disable --now "${name}" || true
      rm -f -- "${path}"
    fi
  done

  systemctl daemon-reload
  systemctl enable "${BACKEND_UNIT}" "${WORKER_UNIT}"
  systemctl restart "${BACKEND_UNIT}" "${WORKER_UNIT}"

  for service in "${RAG_SERVICES[@]}"; do
    unit="$(rag_unit_name "${service}")"
    if contains_value "${unit}" "${renamed_stopped_units[@]}"; then
      log "Leaving ${unit} stopped (renamed; last operation on the old unit: stop)."
    elif contains_value "${unit}" "${renamed_started_units[@]}"; then
      log "Starting ${unit} (renamed; last operation on the old unit: start)."
      systemctl enable --now "${unit}"
    elif contains_value "${unit}" "${new_units[@]}" && rag_service_default_stopped "${service}"; then
      log "Leaving ${unit} stopped (first deployment; stopped by default)."
    elif contains_value "${unit}" "${new_units[@]}"; then
      log "Starting ${unit} (first deployment)."
      systemctl enable --now "${unit}"
    elif systemctl is-enabled --quiet "${unit}"; then
      log "Restarting ${unit} (last operation: start)."
      systemctl restart "${unit}"
    else
      log "Keeping ${unit} stopped (last operation: stop)."
      systemctl stop "${unit}" || true
    fi
  done

  configure_sudoers "${controllable_units[@]}"
}

# backend（SERVICE_USER）に、画面から操作する unit の systemctl / journalctl だけを許可する。
configure_sudoers() {
  local target="${SUDOERS_DIR}/${RAG_SUDOERS_FILE_NAME}"
  local tmp
  tmp="$(mktemp)"
  rag_write_sudoers "${tmp}" "${SERVICE_USER}" "$@"
  if ! visudo -cf "${tmp}" >/dev/null; then
    rm -f -- "${tmp:?}"
    log "Generated sudoers for the service management screen is invalid."
    return 1
  fi
  install -d -m 0750 "${SUDOERS_DIR}"
  install -m 0440 "${tmp}" "${target}"
  chown root:root "${target}" 2>/dev/null || true
  rm -f -- "${tmp:?}"
  log "Installed ${target}."
}

dump_service_diagnostics() {
  local unit="$1"
  log "Diagnostics for ${unit}: systemctl status"
  systemctl --no-pager --full status "${unit}" || true
  log "Diagnostics for ${unit}: recent journal"
  journalctl -u "${unit}" -n 160 --no-pager || true
}

# /api/health は OCI / AI の設定前でも 200 を返す（/api/ready は設定が揃うまで 503）。
# 一定の間隔で待ち、backend が failed になるか再起動を繰り返したら、上限を待たずに status と journal を出す。
wait_for_backend() {
  if suite_wait_for_service_health "${BACKEND_UNIT}" "http://${BACKEND_HOST}:${BACKEND_PORT}/api/health"; then
    return 0
  fi
  log "Backend did not become healthy on ${BACKEND_HOST}:${BACKEND_PORT}."
  if [ "${DATABASE_INITIALIZATION_READY}" != "true" ]; then
    log "The RAG system schema was not initialized; check ADB reachability and ${PLATFORM_ENV_FILE}."
  fi
  dump_service_diagnostics "${BACKEND_UNIT}"
  return 1
}

main() {
  log "Starting Compute initialization (native: uv + systemd + Nginx)."
  load_rag_services
  install_system_packages
  install_nodejs
  install_uv
  prepare_service_user
  prepare_filesystem
  migrate_from_docker_compose
  install_runtime_env
  install_wallet
  install_service_venvs
  migrate_saved_settings
  initialize_database_schema
  build_frontend
  configure_systemd
  wait_for_backend
  # URL（scheme・IP・port）は suite-init.sh の最後に出す（製品は HTTPS・公開の port を知らない。#1639）。
  log "Initialization complete. The URLs are listed at the end of /var/log/suite-init.log (suite: and each product). Log in with the login user."
  log "Then configure OCI authentication and models in System settings."
}

if [ "${RAG_INIT_TEST_MODE:-false}" != "true" ]; then
  main "$@"
fi
