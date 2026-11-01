#!/usr/bin/env bash
# 1 台の Compute に、選んだ製品（RAG / NL2SQL / Agent）を配備する（#1316）。
# 配備の入力（/u01/aipoc/props）を置いてから root で実行する（以前の Terraform stack による配備は #12 で削除）。
#
# - 3 製品はこの suite でセットで配備する（単独では配備しない）。各製品の配備（uv の venv・systemd の unit・
#   DB の初期化・frontend の build）は各製品の init_script.sh が持ち、ここから順に呼ぶ。
#   frontend は /<製品>/ を base に build させる（FRONTEND_BASE_PATH）。backend・worker・port は製品ごとに独立
#   （RAG 8000・NL2SQL 8010・Agent 8020。RAG の前処理 / parser は 18010〜18028。すべて 127.0.0.1）。
# - reverse proxy（Nginx など）と TLS の証明書は配備しない（#19）。前段に reverse proxy を置くか、どう公開するかは
#   利用者が決める（各製品の backend の port と frontend の dist を、その reverse proxy から使う）。
#
# 配備の前に /u01/aipoc/props に置くもの:
#   products.txt（配備する製品。空白区切り）
#   platform.env（3 製品で共通の platform/.env）・<製品>.backend.env（各製品の backend/.env）・rag_services.txt・wallet.zip など
#   adb_admin.env（アプリ用の DB ユーザーを使うときだけ。ADMIN のパスワードとアプリ用のユーザーの名前。root の 0600。#1668）
#
# アプリ用の DB ユーザー（#1668）: adb_admin.env があれば、製品の配備（各製品の system schema の初期化）の前に、
# この Compute から ADMIN で ADB に接続し、アプリ用のユーザーを作って権限を付ける（platform/deploy/suite-db-user.py）。
# 失敗しても（ADB に接続できない・ADMIN のパスワードの誤りなど）製品の配備と起動は続け、最後に失敗として返す
# （DB が使えなくてもアプリケーションは起動する。製品の system schema の初期化の失敗と同じ扱い。#1756）。
# ADB は Compute からしか接続できない（プライベート・エンドポイント・ACL）ため、Compute の外からは
# 行わない。3 製品は platform/.env の PLATFORM_ORACLE_USER（このユーザー）で接続する。ADMIN のパスワードは
# platform/.env・各製品の backend/.env に書かない。
#
# Compute の OS は Oracle Linux 9（#1611）。package は dnf、SELinux は enforcing のまま、次の設定で動かす
# （configure_selinux）:
#   - suite の repository を usr_t にする（systemd が venv の実行ファイルと EnvironmentFile を読めるように。
#     /u01 の既定の default_t は systemd が読めない）。新しく作るファイルは親の usr_t を受け継ぐ。
set -euo pipefail

if [ "${SUITE_INIT_TEST_MODE:-false}" != "true" ]; then
  exec > >(tee -a /var/log/suite-init.log) 2>&1
fi

SUITE_DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=suite-products.sh
source "${SUITE_DEPLOY_DIR}/suite-products.sh"
# shellcheck source=suite-repo.sh
source "${SUITE_DEPLOY_DIR}/suite-repo.sh"
# shellcheck source=suite-health.sh
source "${SUITE_DEPLOY_DIR}/suite-health.sh"

APP_ROOT="${APP_ROOT:-/u01/aipoc}"
# 3 製品のプロセスは同じユーザーで動かす（共通の platform/.env・model-settings.json・Wallet を
# 画面から読み書きするため。#1316。製品ごとの Compute では RAG だけ専用の ragsvc で動かしていた）。
APP_USER="${APP_USER:-opc}"
APP_GROUP="${APP_GROUP:-${APP_USER}}"
APP_HOME="${APP_HOME:-/home/${APP_USER}}"
SUITE_REPO_DIR="${SUITE_REPO_DIR:-${APP_ROOT}/no.1-production-ready-assistant}"
PROPS_DIR="${PROPS_DIR:-${APP_ROOT}/props}"
SYSTEMD_UNIT_DIR="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
# suite-init.sh が dnf で入れる OS の package（製品の init_script.sh は製品が使うものだけを入れる）。
SUITE_OS_PACKAGES=(git policycoreutils-python-utils tar unzip)
# 時間帯（#3255）: PLATFORM_TIMEZONE が無いときの既定（backend の pr_backend_core.timezones と同じ）。
SUITE_DEFAULT_TIMEZONE="UTC"
SUITE_ZONEINFO_DIR="${SUITE_ZONEINFO_DIR:-/usr/share/zoneinfo}"
SUITE_LOCALTIME_PATH="${SUITE_LOCALTIME_PATH:-/etc/localtime}"
# アプリ用の DB ユーザーを作る script（suite-db-user.py）を動かす python-oracledb と Python（#1668）。製品の venv の
# 前に動かすため、uv run --no-project で一時の環境に入れる。Python は製品と同じ場所（/opt/uv/python）に入れる。
SUITE_DB_USER_ORACLEDB_VERSION="4.0.2"
SUITE_DB_USER_PYTHON_VERSION="3.12"
SUITE_UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-/opt/uv/python}"
SUITE_PRODUCTS=()
SUITE_FAILED_PRODUCTS=()
# アプリ用の DB ユーザーの準備が失敗したか（#1756。配備は続け、最後に失敗として返す）。
SUITE_DB_USER_FAILED=false

PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH}"

log() {
  printf '[suite-init] %s\n' "$*"
}

# 配備する製品（products.txt）。並びは rag → nl2sql → agent に固定する。
load_products() {
  local names=() name
  read -r -a names <<< "$(tr '\n' ' ' 2>/dev/null < "${PROPS_DIR}/products.txt" || true)"
  for name in "${names[@]}"; do
    if ! suite_contains "${name}" "${SUITE_PRODUCTS_ORDER[@]}"; then
      log "Unknown product in ${PROPS_DIR}/products.txt: ${name}"
      return 1
    fi
  done
  SUITE_PRODUCTS=()
  for name in "${SUITE_PRODUCTS_ORDER[@]}"; do
    if suite_contains "${name}" "${names[@]}"; then
      SUITE_PRODUCTS+=("${name}")
    fi
  done
  if [ "${#SUITE_PRODUCTS[@]}" -eq 0 ]; then
    log "No product is selected in ${PROPS_DIR}/products.txt."
    return 1
  fi
  log "Products: ${SUITE_PRODUCTS[*]}"
}

# .env の 1 つの key の値（最後の行。前後の空白と、囲む single / double quote を除く）。key が無ければ失敗する。
suite_env_value() {
  local file="$1" key="$2" line value
  if [ ! -f "${file}" ] || ! grep -qE "^${key}=" "${file}"; then
    return 1
  fi
  line="$(grep -E "^${key}=" "${file}" | tail -n 1)"
  value="${line#*=}"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  case "${value}" in
    \'*\')
      value="${value#\'}"
      value="${value%\'}"
      ;;
    \"*\")
      value="${value#\"}"
      value="${value%\"}"
      ;;
  esac
  printf '%s\n' "${value}"
}

# 時間帯（#3255・#3274）: 3 製品の共通の設定 PLATFORM_TIMEZONE（IANA の地域名。既定 UTC。日本時間は Asia/Tokyo）。
# repository の platform/.env（再配備。手で変えた値）→ props の platform.env（初回。配備の入力）の順に、
# key のある最初のファイルの値を使う。空なら backend と同じ既定。
suite_timezone() {
  local file value
  for file in "${SUITE_REPO_DIR}/platform/.env" "${PROPS_DIR}/platform.env"; do
    if value="$(suite_env_value "${file}" PLATFORM_TIMEZONE)"; then
      printf '%s\n' "${value:-${SUITE_DEFAULT_TIMEZONE}}"
      return 0
    fi
  done
  printf '%s\n' "${SUITE_DEFAULT_TIMEZONE}"
}

# IANA の地域名か（backend の pr_backend_core.timezones と同じ形で、tzdata にあるもの）。
# 固定の offset（+09:00）・相対の path は受けない。
suite_valid_timezone() {
  local name="$1"
  [[ "${name}" =~ ^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*$ ]] || return 1
  [ -f "${SUITE_ZONEINFO_DIR}/${name}" ]
}

# Compute の OS の時間帯を PLATFORM_TIMEZONE に合わせる（#3255。同じなら変えない。再配備でも効く）。
# 3 製品の DB の session の時間帯は OS に左右されず UTC（pr_backend_core.oracle_session）なので、時刻の保存は変わらない。
# 不正な値は配備の前に失敗にする（backend の Settings も起動で拒む）。OS の時間帯を変えられなくても配備は続ける。
configure_timezone() {
  local name current
  name="$(suite_timezone)"
  if ! suite_valid_timezone "${name}"; then
    log "timezone: PLATFORM_TIMEZONE=${name} は IANA の地域名ではありません（例: Asia/Tokyo。+09:00 のような固定の offset は使えません）。platform/.env を直してください。"
    return 1
  fi
  current="$(timedatectl show --property=Timezone --value 2>/dev/null || true)"
  if [ "${current}" = "${name}" ]; then
    log "timezone: ${name}（変更なし）"
    return 0
  fi
  if timedatectl set-timezone "${name}" 2>/dev/null; then
    log "timezone: ${current:-unknown} -> ${name}"
    return 0
  fi
  # timedated の無い環境（container など）は /etc/localtime を直接置き換える（timedatectl と同じ symlink）。
  if ln -sfn "${SUITE_ZONEINFO_DIR}/${name}" "${SUITE_LOCALTIME_PATH}" 2>/dev/null; then
    log "timezone: ${current:-unknown} -> ${name}（${SUITE_LOCALTIME_PATH}）"
    return 0
  fi
  log "WARNING: timezone: OS の時間帯を ${name} にできませんでした。配備は続けます（DB の時刻は UTC のまま）。"
}

suite_dnf() {
  local attempt status=0
  for attempt in 1 2 3 4 5; do
    # `if` の後の `$?` は 0 になるので、失敗の status は else の中で読む（#1994）。
    if dnf -y "$@"; then
      return 0
    else
      status="$?"
    fi
    log "dnf failed with status ${status} on attempt ${attempt}/5: dnf -y $*"
    # 最後の失敗の後は待たない（#1779）。
    if [ "${attempt}" -lt 5 ]; then
      sleep $((attempt * 5))
    fi
  done
  return "${status}"
}

# root の filesystem を boot volume いっぱいに広げる（#1984）。Oracle Linux 9 の platform image の root（LVM）は、
# boot volume を大きくしても image の大きさ（約 30〜40 GB）のままで、3 製品の Python の環境・RAG の parser・
# LibreOffice が入りきらない。Oracle Linux の oci-growfs（OCI の image にある）で広げる（広げる余地が無ければ何もしない。何度実行してもよい）。
# 失敗しても配備は続ける（空きが足りなければ後の dnf が止まる）。広げた後の空きを log に出す。
OCI_GROWFS="${OCI_GROWFS:-/usr/libexec/oci-growfs}"
SUITE_ROOT_FS="${SUITE_ROOT_FS:-/}"
SUITE_MIN_ROOT_FREE_GB="${SUITE_MIN_ROOT_FREE_GB:-30}"
grow_root_filesystem() {
  local status=0 free_gb
  if [ -x "${OCI_GROWFS}" ]; then
    log "Growing the root filesystem to the boot volume size (${OCI_GROWFS} -y)."
    "${OCI_GROWFS}" -y || status=$?
    if [ "${status}" -ne 0 ]; then
      log "WARNING: ${OCI_GROWFS} -y が status ${status} で終わりました。df -h / で空きを確かめ、足りなければ sudo ${OCI_GROWFS} -y を実行してから配備をやり直してください。"
    fi
  else
    log "Skipping the root filesystem growth (${OCI_GROWFS} is not available)."
  fi
  free_gb="$(df -BG --output=avail "${SUITE_ROOT_FS}" 2>/dev/null | tail -n 1 | tr -dc '0-9')"
  log "Free space on ${SUITE_ROOT_FS}: ${free_gb:-unknown} GB."
  if [ -n "${free_gb}" ] && [ "${free_gb}" -lt "${SUITE_MIN_ROOT_FREE_GB}" ]; then
    log "WARNING: ${SUITE_ROOT_FS} の空きが ${free_gb} GB です（3 製品の配備の目安は ${SUITE_MIN_ROOT_FREE_GB} GB 以上）。boot volume を大きくして sudo ${OCI_GROWFS} -y で広げてください。"
  fi
  return 0
}

# OS の package（git・SELinux の道具など）。
install_os_packages() {
  log "Installing OS packages: ${SUITE_OS_PACKAGES[*]}."
  suite_dnf install "${SUITE_OS_PACKAGES[@]}"
}

# SELinux（Oracle Linux 9 の既定の enforcing のまま動かす）。無効の環境では何もしない。
selinux_enabled() {
  command -v selinuxenabled >/dev/null 2>&1 && selinuxenabled
}

# restorecon -R に渡す、repository の中で配備に使わないディレクトリ（suite-repo.sh の SUITE_REPO_EXCLUDE_DIRS。開発用の
# worktree〔#2032〕と一時ファイル〔#1616〕）の -e（1 行に 1 つずつ。-e と path の 2 行）。systemd の service は
# 読まないので辿らない（辿る間に消えると restorecon が失敗して配備が止まる。#2258）。
selinux_exclude_args() {
  local path
  while IFS= read -r path; do
    printf '%s\n%s\n' -e "${path}"
  done < <(suite_repo_exclude_paths "${SUITE_REPO_DIR}")
}

# 製品の配備（venv・systemd の unit）の前に行う SELinux の設定。file context の規則は、同じ規則があれば
# semanage が置き換える（再実行してよい）。
configure_selinux() {
  local -a excludes=()
  if ! selinux_enabled; then
    log "SELinux is disabled; skipping SELinux contexts."
    return 0
  fi
  log "Configuring SELinux contexts (repository usr_t)."
  semanage fcontext -a -t usr_t "${SUITE_REPO_DIR}(/.*)?"
  mapfile -t excludes < <(selinux_exclude_args)
  restorecon -R "${excludes[@]}" "${SUITE_REPO_DIR}"
}

# uv（製品の init_script.sh の install_uv と同じ入れ方。後の製品は入っていれば入れ直さない）。SELinux が有効なら
# /usr/local/bin/uv の context を bin_t に戻す（#1642）。
suite_install_uv() {
  local attempt
  if ! command -v uv >/dev/null 2>&1; then
    log "Installing uv."
    for attempt in 1 2 3; do
      if curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh; then
        break
      fi
      if [ "${attempt}" -eq 3 ]; then
        log "Failed to install uv."
        return 1
      fi
      sleep $((attempt * 5))
    done
    chmod 0755 /usr/local/bin/uv
  fi
  if selinux_enabled; then
    restorecon -v /usr/local/bin/uv /usr/local/bin/uvx 2>/dev/null || true
  fi
}

# アプリ用の DB ユーザー（#1668）。props の adb_admin.env が無ければ（adb_app_user_enabled=false。新規の ADB は
# ADMIN、既存の ADB は existing_oracle_user で接続する）何もしない。Wallet は製品の配備の前なので、props の
# wallet.zip を root だけが読める一時の directory に展開して使い、終わったら消す。失敗しても 0 以外を返すだけで、
# main は製品の配備と起動を続ける（#1756。ユーザーが無ければ製品は DB に接続できないが、画面は開ける）。
# log の最後の行（[suite-db-user] OK / NG）が結果。
provision_db_app_user() {
  local admin_env="${PROPS_DIR}/adb_admin.env" wallet_dir status=0
  if [ ! -f "${admin_env}" ]; then
    log "db user: ${admin_env} がありません。アプリ用の DB ユーザーは作りません（platform/.env の PLATFORM_ORACLE_USER で接続します）。"
    return 0
  fi
  chmod 0600 "${admin_env}"
  suite_install_uv
  wallet_dir="$(mktemp -d "${PROPS_DIR}/.db-user-wallet.XXXXXX")"
  chmod 0700 "${wallet_dir}"
  if [ -f "${PROPS_DIR}/wallet.zip" ]; then
    unzip -oq "${PROPS_DIR}/wallet.zip" -d "${wallet_dir}"
  fi
  UV_PYTHON_INSTALL_DIR="${SUITE_UV_PYTHON_INSTALL_DIR}" uv run --no-project --python "${SUITE_DB_USER_PYTHON_VERSION}" \
    --with "oracledb==${SUITE_DB_USER_ORACLEDB_VERSION}" \
    python "${SUITE_DEPLOY_DIR}/suite-db-user.py" apply \
    --admin-env "${admin_env}" \
    --app-env "${PROPS_DIR}/platform.env" \
    --current-env "${SUITE_REPO_DIR}/platform/.env" \
    --wallet-dir "${wallet_dir}" || status=$?
  rm -rf -- "${wallet_dir}"
  if [ "${status}" -ne 0 ]; then
    log "check: db user NG（ADMIN でアプリ用の DB ユーザーを作れませんでした。上の [suite-db-user] NG の行を見てください）。"
    return "${status}"
  fi
  log "check: db user OK"
}

# 製品の init_script.sh に渡す環境変数（1 行に 1 つ）。
product_init_env() {
  local product="$1"
  local skip_platform_ui_build="$2"
  printf '%s\n' \
    "APP_ROOT=${APP_ROOT}" \
    "APP_USER=${APP_USER}" \
    "APP_GROUP=${APP_GROUP}" \
    "FRONTEND_BASE_PATH=/${product}/" \
    "BACKEND_ENV_SOURCE=${PROPS_DIR}/${product}.backend.env" \
    "PR_SUITE_SKIP_PLATFORM_UI_BUILD=${skip_platform_ui_build}"
  if [ "${product}" = "rag" ]; then
    printf '%s\n' \
      "SERVICE_USER=${APP_USER}" \
      "SERVICE_GROUP=${APP_GROUP}" \
      "SERVICE_HOME=${APP_HOME}"
  fi
}

run_product_init() {
  local product="$1"
  local skip_platform_ui_build="$2"
  local init_script="${SUITE_REPO_DIR}/${product}/init_script.sh"
  local env_args=()
  if [ ! -f "${init_script}" ]; then
    log "Missing ${init_script}."
    return 1
  fi
  mapfile -t env_args < <(product_init_env "${product}" "${skip_platform_ui_build}")
  log "Deploying ${product} via ${init_script} (${env_args[*]})."
  env "${env_args[@]}" bash "${init_script}"
}

# 製品を順に配備する。1 つが失敗しても残りの製品は配備し、最後に失敗として返す。
deploy_products() {
  local product skip_platform_ui_build=false
  for product in "${SUITE_PRODUCTS[@]}"; do
    if run_product_init "${product}" "${skip_platform_ui_build}"; then
      # 共有 UI（platform）は最初に成功した製品が build した。後の製品は build し直さない。
      skip_platform_ui_build=true
    else
      log "WARNING: ${product} deployment failed. See /var/log/${product}-init.log."
      SUITE_FAILED_PRODUCTS+=("${product}")
    fi
  done
}

# 各製品の backend の port と frontend の dist（reverse proxy は配備しない。#19）。
report_urls() {
  local product
  for product in "${SUITE_PRODUCTS[@]}"; do
    log "  ${product}: backend http://127.0.0.1:$(suite_backend_port "${product}")/api/health, frontend ${SUITE_REPO_DIR}/${product}/frontend/dist (base /${product}/)"
  done
}

main() {
  log "Starting single-Compute deployment."
  load_products
  # OS の時間帯（#3255）。不正な PLATFORM_TIMEZONE は何かを入れる前に失敗にする。
  configure_timezone
  grow_root_filesystem
  install_os_packages
  configure_selinux
  # ADB の接続のユーザー（製品の初期化の前。#1668）。失敗しても製品の配備と起動は続ける（#1756）。
  SUITE_DB_USER_FAILED=false
  if ! provision_db_app_user; then
    SUITE_DB_USER_FAILED=true
    log "WARNING: アプリ用の DB ユーザーを準備できませんでした。製品の配備と起動は続けます（DB に接続できるようになったら sudo ${SUITE_DEPLOY_DIR}/suite-restart.sh を実行してください）。"
  fi
  deploy_products
  log "Deployment finished. Log in as system_admin (PLATFORM_ADMIN_LOGIN_USER_PASSWORD)."
  report_urls
  # 配備全体の確認（service・backend の health。#1642）。log の最後の check の行が結果。
  local check_status=0
  suite_check_deployment "${SUITE_PRODUCTS[@]}" || check_status=$?
  if [ "${SUITE_DB_USER_FAILED}" = "true" ]; then
    log "check: db user NG（上の [suite-db-user] NG の行を見てください）"
  fi
  if [ "${#SUITE_FAILED_PRODUCTS[@]}" -gt 0 ]; then
    log "Failed products: ${SUITE_FAILED_PRODUCTS[*]}"
    return 1
  fi
  if [ "${SUITE_DB_USER_FAILED}" = "true" ]; then
    return 1
  fi
  return "${check_status}"
}

if [ "${SUITE_INIT_TEST_MODE:-false}" != "true" ]; then
  main "$@"
fi
