#!/usr/bin/env bash
# platform/deploy/suite-init.sh（1 台の Compute への 3 製品の配備。#1316）の振る舞いを外部依存なしで検証する。
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
task_dir="$(mktemp -d)"
trap 'rm -rf -- "${task_dir}"' EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

prepare_case() {
  local case_dir="$1"
  mkdir -p "${case_dir}/app/props" "${case_dir}/app/no.1-production-ready-suite" "${case_dir}/units"
  ln -s "${repo}/platform" "${case_dir}/app/no.1-production-ready-suite/platform"
  export SUITE_INIT_TEST_MODE=true
  export APP_ROOT="${case_dir}/app"
  export SYSTEMD_UNIT_DIR="${case_dir}/units"
}

# 1. 製品の選択: 並びは rag → nl2sql → agent、未知の名前と 0 件は失敗。
(
  case_dir="${task_dir}/products"
  prepare_case "${case_dir}"
  # shellcheck source=../suite-init.sh
  source "${repo}/platform/deploy/suite-init.sh"
  printf 'agent rag\n' > "${PROPS_DIR}/products.txt"
  load_products >/dev/null
  [ "${SUITE_PRODUCTS[*]}" = "rag agent" ] || fail "製品の並び: ${SUITE_PRODUCTS[*]}"
  printf 'rag platform\n' > "${PROPS_DIR}/products.txt"
  if load_products >/dev/null 2>&1; then fail "未知の製品を受け入れた"; fi
  : > "${PROPS_DIR}/products.txt"
  if load_products >/dev/null 2>&1; then fail "製品 0 件を受け入れた"; fi
)

# 2. 製品の init_script.sh に渡す環境変数。
(
  case_dir="${task_dir}/env"
  prepare_case "${case_dir}"
  # shellcheck source=../suite-init.sh
  source "${repo}/platform/deploy/suite-init.sh"
  rag_env="$(product_init_env rag false)"
  if printf '%s\n' "$(product_init_env rag false)" | grep -q 'PR_SUITE_MODE'; then fail "単独の配備の mode は無い"; fi
  for expected in "FRONTEND_BASE_PATH=/rag/" "BACKEND_ENV_SOURCE=${PROPS_DIR}/rag.backend.env" \
    "PR_SUITE_SKIP_PLATFORM_UI_BUILD=false" "SERVICE_USER=opc" "SERVICE_GROUP=opc" "SERVICE_HOME=/home/opc"; do
    printf '%s\n' "${rag_env}" | grep -qxF "${expected}" || fail "RAG の環境変数に ${expected} が無い"
  done
  agent_env="$(product_init_env agent true)"
  printf '%s\n' "${agent_env}" | grep -qxF "FRONTEND_BASE_PATH=/agent/" || fail "Agent の base"
  printf '%s\n' "${agent_env}" | grep -qxF "PR_SUITE_SKIP_PLATFORM_UI_BUILD=true" || fail "共有 UI の build を飛ばす"
  if printf '%s\n' "${agent_env}" | grep -q '^SERVICE_USER='; then fail "SERVICE_USER は RAG だけ"; fi
)

# 3. 1 つの製品が失敗しても残りを配備し、共有 UI は最初に成功した製品だけが build する。
(
  case_dir="${task_dir}/deploy"
  prepare_case "${case_dir}"
  # shellcheck source=../suite-init.sh
  source "${repo}/platform/deploy/suite-init.sh"
  SUITE_PRODUCTS=(rag nl2sql agent)
  run_product_init() {
    printf '%s %s\n' "$1" "$2" >> "${case_dir}/calls.log"
    [ "$1" != "rag" ]
  }
  deploy_products >/dev/null
  [ "$(cat "${case_dir}/calls.log")" = "$(printf 'rag false\nnl2sql false\nagent true')" ] \
    || fail "製品の呼び出し: $(cat "${case_dir}/calls.log")"
  [ "${SUITE_FAILED_PRODUCTS[*]}" = "rag" ] || fail "失敗した製品: ${SUITE_FAILED_PRODUCTS[*]}"
)


# 5b. Oracle Linux 9（#1611）: dnf の package と SELinux の context。reverse proxy（Nginx）・firewalld・証明書は
#     扱わない（#19）。
(
  case_dir="${task_dir}/oracle-linux"
  prepare_case "${case_dir}"
  # shellcheck source=../suite-init.sh
  source "${repo}/platform/deploy/suite-init.sh"
  calls="${case_dir}/commands.log"
  dnf() { printf 'dnf %s\n' "$*" >> "${calls}"; }
  systemctl() { printf 'systemctl %s\n' "$*" >> "${calls}"; }
  selinuxenabled() { return 0; }
  semanage() { printf 'semanage %s\n' "$*" >> "${calls}"; }
  restorecon() { printf 'restorecon %s\n' "$*" >> "${calls}"; }
  setsebool() { printf 'setsebool %s\n' "$*" >> "${calls}"; }
  install_os_packages >/dev/null
  grep -qx "dnf -y install ${SUITE_OS_PACKAGES[*]}" "${calls}" || fail "OS の package を dnf で入れない"
  if grep -q 'module enable' "${calls}"; then fail "Nginx の module を有効にした"; fi
  printf ' %s ' "${SUITE_OS_PACKAGES[*]}" | grep -qF " policycoreutils-python-utils " || fail "policycoreutils-python-utils を入れない"
  for name in nginx firewalld; do
    if printf ' %s ' "${SUITE_OS_PACKAGES[*]}" | grep -qF " ${name} "; then fail "${name} を入れた（#19）"; fi
  done
  : > "${calls}"
  configure_selinux >/dev/null
  grep -qxF "semanage fcontext -a -t usr_t ${SUITE_REPO_DIR}(/.*)?" "${calls}" || fail "repository を usr_t にしない"
  grep -qxF "restorecon -R ${SUITE_REPO_DIR}" "${calls}" || fail "repository の context を戻さない"
  if grep -qE 'httpd_|cert_t|^semanage port|^setsebool' "${calls}"; then fail "Nginx・証明書の SELinux の設定が残っている: $(cat "${calls}")"; fi
  # 開発用の worktree と repository の中の一時ファイルは、あれば restorecon で辿らない（#2258）。
  mkdir -p "${SUITE_REPO_DIR}/.claude/worktrees/x"
  : > "${calls}"
  configure_selinux >/dev/null
  grep -qxF "restorecon -R -e ${SUITE_REPO_DIR}/.claude/worktrees ${SUITE_REPO_DIR}" "${calls}" \
    || fail "worktree を restorecon から外さない（無い tmp は渡さない）"
  mkdir -p "${SUITE_REPO_DIR}/tmp"
  : > "${calls}"
  configure_selinux >/dev/null
  grep -qxF "restorecon -R -e ${SUITE_REPO_DIR}/.claude/worktrees -e ${SUITE_REPO_DIR}/tmp ${SUITE_REPO_DIR}" "${calls}" \
    || fail "tmp を restorecon から外さない"
  : > "${calls}"
  SUITE_REPO_EXCLUDE_DIRS="" configure_selinux >/dev/null
  grep -qxF "restorecon -R ${SUITE_REPO_DIR}" "${calls}" || fail "除外を空にしても repository 全体を辿らない"
  rm -rf "${SUITE_REPO_DIR}/.claude" "${SUITE_REPO_DIR}/tmp"
  # SELinux が無効なら何もしない。
  selinuxenabled() { return 1; }
  : > "${calls}"
  configure_selinux >/dev/null
  [ ! -s "${calls}" ] || fail "SELinux が無効なのに設定した: $(cat "${calls}")"
  # main の順: package → SELinux → 製品の配備（venv・unit の前に context の規則を置く）。
  main_body="$(sed -n '/^main() {$/,/^}$/p' "${repo}/platform/deploy/suite-init.sh")"
  # アプリ用の DB ユーザー（#1668）は、製品の配備（system schema の初期化）の前に作る。
  # provision_db_app_user は失敗しても続ける（#1756）ため、`if ! provision_db_app_user; then` の形。
  printf '%s\n' "${main_body}" | sed -E 's/^  if ! (provision_db_app_user); then$/  \1/' \
    | grep -E '^  (configure_timezone|grow_root_filesystem|install_os_packages|configure_selinux|provision_db_app_user|deploy_products)$' \
    | awk '{printf "%s ", $1}' \
    | grep -qx 'configure_timezone grow_root_filesystem install_os_packages configure_selinux provision_db_app_user deploy_products ' \
    || fail "main の順が違う"
  # 最後に配備全体を確かめる（#1642。log の最後の check の行が結果）。完了の log の後、失敗の判定の前。
  printf '%s\n' "${main_body}" | grep -E '^  (deploy_products|report_urls|suite_check_deployment .*)$' \
    | awk '{printf "%s ", $1}' | grep -qx 'deploy_products report_urls suite_check_deployment ' \
    || fail "main の最後に配備の確認が無い"
  # Nginx・firewalld・証明書の段は無い（#19）。
  if grep -vE '^\s*#' "${repo}/platform/deploy/suite-init.sh" | grep -qiE 'nginx|firewall-cmd|suite-tls|tls-renew|configure_tls|configure_public_base_url'; then
    fail "suite-init.sh に Nginx・firewalld・証明書の段が残っている"
  fi
)

# 6. 完了の log（#19）: 製品ごとに backend の health の URL と frontend の dist を出す（reverse proxy の URL は出さない）。
(
  case_dir="${task_dir}/urls"
  prepare_case "${case_dir}"
  # shellcheck source=../suite-init.sh
  source "${repo}/platform/deploy/suite-init.sh"
  SUITE_PRODUCTS=(rag agent)
  expected="$(printf '%s\n' \
    "[suite-init]   rag: backend http://127.0.0.1:8000/api/health, frontend ${SUITE_REPO_DIR}/rag/frontend/dist (base /rag/)" \
    "[suite-init]   agent: backend http://127.0.0.1:8020/api/health, frontend ${SUITE_REPO_DIR}/agent/frontend/dist (base /agent/)")"
  [ "$(report_urls)" = "${expected}" ] || fail "完了の log: $(report_urls)"
  if report_urls | grep -qiE 'https|ca\.crt|nginx'; then fail "reverse proxy・証明書の URL を出した"; fi
)

# 6c. 時間帯（#3255・#3274）: platform/.env → props の platform.env → 既定（UTC）の順に PLATFORM_TIMEZONE を読み、
#     OS の時間帯を timedatectl で合わせる（同じなら変えない）。不正な値は失敗。timedatectl が使えなければ
#     /etc/localtime を置き換え、それもできなければ警告だけで続ける。
(
  case_dir="${task_dir}/timezone"
  prepare_case "${case_dir}"
  # shellcheck source=../suite-init.sh
  source "${repo}/platform/deploy/suite-init.sh"
  rm -f -- "${case_dir}/app/no.1-production-ready-suite/platform"
  mkdir -p "${case_dir}/app/no.1-production-ready-suite/platform" "${case_dir}/bin" "${case_dir}/zoneinfo/Asia" \
    "${case_dir}/zoneinfo/Etc"
  : > "${case_dir}/zoneinfo/Asia/Tokyo"
  : > "${case_dir}/zoneinfo/UTC"
  : > "${case_dir}/zoneinfo/Etc/GMT-9"
  SUITE_ZONEINFO_DIR="${case_dir}/zoneinfo"
  SUITE_LOCALTIME_PATH="${case_dir}/localtime"
  calls="${case_dir}/timedatectl.calls"
  current="${case_dir}/timedatectl.current"
  printf 'Asia/Tokyo\n' > "${current}"
  # timedatectl の代わり（show は今の時間帯、set-timezone は呼び出しを記録して今の時間帯を変える）。
  cat > "${case_dir}/bin/timedatectl" <<EOF
#!/usr/bin/env bash
if [ "\$1" = "show" ]; then cat "${current}"; exit 0; fi
if [ "\$1" = "set-timezone" ]; then
  [ "\${TIMEDATECTL_FAIL:-false}" = "true" ] && exit 1
  printf '%s\n' "\$*" >> "${calls}"; printf '%s\n' "\$2" > "${current}"; exit 0
fi
exit 2
EOF
  chmod +x "${case_dir}/bin/timedatectl"
  PATH="${case_dir}/bin:${PATH}"
  # 既定: どちらの .env にも key が無ければ UTC（#3274）。
  [ "$(suite_timezone)" = "UTC" ] || fail "既定の時間帯: $(suite_timezone)"
  configure_timezone >/dev/null || fail "既定の時間帯にできない"
  [ "$(cat "${calls}")" = "set-timezone UTC" ] || fail "timedatectl の呼び出し: $(cat "${calls}")"
  # 同じなら変えない（冪等）。
  : > "${calls}"
  configure_timezone | grep -qF '変更なし' || fail "同じ時間帯で変更なしと出さない"
  [ ! -s "${calls}" ] || fail "同じ時間帯なのに timedatectl を呼んだ"
  # props の platform.env（初回。Terraform の timezone）。quote は外す。
  printf "PLATFORM_ORACLE_USER=APP\nPLATFORM_TIMEZONE='Asia/Tokyo'\n" > "${PROPS_DIR}/platform.env"
  [ "$(suite_timezone)" = "Asia/Tokyo" ] || fail "props の時間帯: $(suite_timezone)"
  # repository の platform/.env（再配備）を優先する。key があって空なら既定。
  printf 'PLATFORM_TIMEZONE=Etc/GMT-9\n' > "${SUITE_REPO_DIR}/platform/.env"
  [ "$(suite_timezone)" = "Etc/GMT-9" ] || fail "platform/.env の時間帯: $(suite_timezone)"
  configure_timezone >/dev/null || fail "Etc/GMT-9 にできない"
  [ "$(cat "${current}")" = "Etc/GMT-9" ] || fail "OS の時間帯を変えない"
  printf 'PLATFORM_TIMEZONE=\n' > "${SUITE_REPO_DIR}/platform/.env"
  [ "$(suite_timezone)" = "UTC" ] || fail "空の値は既定: $(suite_timezone)"
  # 不正な値（固定の offset・tzdata に無い名前・相対の path）は失敗にし、OS を変えない。
  for bad in '+09:00' 'Asia/Osaka' '../../etc/passwd' 'Asia/Tokyo;id'; do
    printf 'PLATFORM_TIMEZONE=%s\n' "${bad}" > "${SUITE_REPO_DIR}/platform/.env"
    : > "${calls}"
    if configure_timezone >/dev/null 2>&1; then fail "不正な時間帯 ${bad} を受け入れた"; fi
    [ ! -s "${calls}" ] || fail "不正な時間帯 ${bad} で timedatectl を呼んだ"
  done
  # timedatectl が使えなければ /etc/localtime を置き換える。
  printf 'PLATFORM_TIMEZONE=Asia/Tokyo\n' > "${SUITE_REPO_DIR}/platform/.env"
  printf 'UTC\n' > "${current}"
  TIMEDATECTL_FAIL=true configure_timezone >/dev/null || fail "timedatectl が失敗したら続けない"
  [ "$(readlink "${SUITE_LOCALTIME_PATH}")" = "${SUITE_ZONEINFO_DIR}/Asia/Tokyo" ] || fail "/etc/localtime を置き換えない"
  # それもできなければ警告だけで続ける。
  SUITE_LOCALTIME_PATH="${case_dir}/missing/dir/localtime"
  TIMEDATECTL_FAIL=true configure_timezone | grep -qF 'WARNING: timezone' || fail "OS の時間帯を変えられないときに警告しない"
)

# 7. 製品の init_script.sh の契約（#1316。単独では配備しない）: reverse proxy の site を書かない・/<製品>/ を base に
#    build・backend/.env の元。reverse proxy（Nginx など）は配備しない（#19）。
for product in rag nl2sql agent; do
  source_text="$(cat "${repo}/${product}/init_script.sh")"
  for expected in "FRONTEND_BASE_PATH=\"\${FRONTEND_BASE_PATH:-/${product}/}\"" \
    "FRONTEND_BASE_PATH='\${FRONTEND_BASE_PATH}' npm run build" 'BACKEND_ENV_SOURCE="${BACKEND_ENV_SOURCE:-' \
    'PR_SUITE_SKIP_PLATFORM_UI_BUILD'; do
    case "${source_text}" in
      *"${expected}"*) ;;
      *) fail "${product}/init_script.sh に ${expected} が無い" ;;
    esac
  done
  for unexpected in 'configure_nginx' 'PR_SUITE_MODE' 'sites-available' 'proxy_pass' 'APPLICATION_PORT'; do
    case "${source_text}" in
      *"${unexpected}"*) fail "${product}/init_script.sh に単独の配備（${unexpected}）が残っている" ;;
    esac
  done
done

# 8. 権限の不足で表の初期化に失敗した配備（#1648）: 製品の init_script.sh は backend を起動して 0 で終わる（画面から
#    直せる）が、最後の確認（suite_check_deployment）の system schema が NG になり、main は 0 以外で終わる。
(
  case_dir="${task_dir}/schema-ng"
  prepare_case "${case_dir}"
  # shellcheck source=../suite-init.sh
  source "${repo}/platform/deploy/suite-init.sh"
  printf 'rag nl2sql\n' > "${PROPS_DIR}/products.txt"
  out="${case_dir}/main.log"
  install_os_packages() { :; }
  configure_selinux() { :; }
  report_urls() { :; }
  run_product_init() { log "deployed $1"; }
  systemctl() {
    case "$1" in
      list-unit-files)
        printf '%s\n' "production-ready-rag-backend.service enabled enabled" \
          "production-ready-nl2sql-backend.service enabled enabled"
        ;;
      is-active) printf 'active\n' ;;
      show) printf '0\n' ;;
    esac
  }
  curl() { return 0; }
  journalctl() { :; }
  # shellcheck disable=SC2034 # suite_check_deployment（source した suite-health.sh）が読む
  SUITE_CHECK_SETTLE_SECONDS=0
  # shellcheck disable=SC2034 # 同上
  SUITE_PRODUCT_LOG_DIR="${case_dir}"
  suite_run_schema_check() {
    case "$1" in
      nl2sql) printf 'status=missing・操作 failed・前回の操作の失敗 ORA-01031\n'; return 1 ;;
      *) printf 'status=ready\n' ;;
    esac
  }
  status=0
  main > "${out}" 2>&1 || status=$?
  [ "${status}" -ne 0 ] || fail "schema が NG なのに main が 0 で終わった: $(cat "${out}")"
  [ "${#SUITE_FAILED_PRODUCTS[@]}" -eq 0 ] || fail "製品の配備は成功している: ${SUITE_FAILED_PRODUCTS[*]}"
  grep -qxF '[suite-init] check: nl2sql schema NG（status=missing・操作 failed・前回の操作の失敗 ORA-01031）' "${out}" \
    || fail "schema の NG の行: $(cat "${out}")"
  grep -qF '[suite-init] check: nl2sql の DB ユーザー（PLATFORM_ORACLE_USER）の権限が足りない可能性があります' "${out}" \
    || fail "権限の不足の手がかり: $(cat "${out}")"
  tail -n 1 "${out}" | grep -q 'check: NG' || fail "最後の行が NG ではない: $(tail -n 1 "${out}")"
  # schema が ready なら 0 で終わる。
  suite_run_schema_check() { printf 'status=ready\n'; }
  main > "${out}" 2>&1 || fail "すべて OK なのに失敗した: $(cat "${out}")"
  tail -n 1 "${out}" | grep -q 'check: OK' || fail "最後の行が OK ではない: $(tail -n 1 "${out}")"
)

# アプリ用の DB ユーザー（#1668）: props の adb_admin.env があるときだけ、製品の配備の前に suite-db-user.py を
# uv run（固定の python-oracledb）で実行する。無ければ何もしない。失敗したら 0 以外を返し、Wallet の一時の展開を消す。
# main は失敗しても製品の配備と起動を続け、最後に失敗として返す（#1756。ADB に接続できなくてもアプリケーションは起動する）。
(
  case_dir="${task_dir}/db-user"
  prepare_case "${case_dir}"
  # shellcheck source=../suite-init.sh
  source "${repo}/platform/deploy/suite-init.sh"
  calls="${case_dir}/uv.calls"
  selinux_enabled() { return 1; }
  uv() {
    printf '%s\n' "$*" >> "${calls}"
    local arg previous=""
    for arg in "$@"; do
      if [ "${previous}" = "--wallet-dir" ]; then
        [ -d "${arg}" ] || fail "Wallet の一時の directory が無い"
        [ "$(stat -c %a "${arg}")" = "700" ] || fail "Wallet の一時の directory は 0700"
        printf '%s\n' "${arg}" > "${case_dir}/wallet-dir"
      fi
      previous="${arg}"
    done
    return "${UV_STATUS:-0}"
  }
  out="${case_dir}/out.log"
  # adb_admin.env が無い: 作らない（uv も呼ばない）。
  provision_db_app_user > "${out}" 2>&1 || fail "adb_admin.env が無いのに失敗した: $(cat "${out}")"
  [ ! -e "${calls}" ] || fail "adb_admin.env が無いのに uv を呼んだ"
  grep -q "アプリ用の DB ユーザーは作りません" "${out}" || fail "作らない旨の log: $(cat "${out}")"
  # adb_admin.env がある: suite-db-user.py apply を固定の oracledb で実行し、0600 にする。
  printf "SUITE_DB_ADMIN_USER=ADMIN\nSUITE_DB_ADMIN_PASSWORD='stub'\nSUITE_DB_APP_USER='ADB_APP_USER'\n" > "${PROPS_DIR}/adb_admin.env"
  chmod 0644 "${PROPS_DIR}/adb_admin.env"
  provision_db_app_user > "${out}" 2>&1 || fail "成功するはず: $(cat "${out}")"
  [ "$(stat -c %a "${PROPS_DIR}/adb_admin.env")" = "600" ] || fail "adb_admin.env は 0600"
  call="$(cat "${calls}")"
  for expected in "run --no-project --python 3.12 --with oracledb==${SUITE_DB_USER_ORACLEDB_VERSION}" \
    "suite-db-user.py apply" "--admin-env ${PROPS_DIR}/adb_admin.env" "--app-env ${PROPS_DIR}/platform.env" \
    "--current-env ${SUITE_REPO_DIR}/platform/.env"; do
    printf '%s\n' "${call}" | grep -qF -- "${expected}" || fail "uv の引数に ${expected} が無い: ${call}"
  done
  [ ! -e "$(cat "${case_dir}/wallet-dir")" ] || fail "Wallet の一時の directory を消していない"
  grep -qxF "[suite-init] check: db user OK" "${out}" || fail "OK の行: $(cat "${out}")"
  # 失敗: 0 以外で返し、NG の行を出し、一時の directory を消す。
  status=0
  UV_STATUS=1 provision_db_app_user > "${out}" 2>&1 || status=$?
  [ "${status}" -eq 1 ] || fail "失敗を返さない（${status}）"
  grep -qF "[suite-init] check: db user NG" "${out}" || fail "NG の行: $(cat "${out}")"
  [ ! -e "$(cat "${case_dir}/wallet-dir")" ] || fail "失敗しても Wallet の一時の directory を消す"

  # main: DB ユーザーの準備が失敗しても（ADB に接続できない）、製品の配備・確認まで進み、最後に失敗として返す。
  steps="${case_dir}/steps.log"
  for step in load_products grow_root_filesystem install_os_packages configure_selinux \
    deploy_products report_urls suite_check_deployment; do
    eval "${step}() { printf '%s\\n' ${step} >> \"\${steps}\"; }"
  done
  status=0
  UV_STATUS=1 main > "${out}" 2>&1 || status=$?
  [ "${status}" -eq 1 ] || fail "DB ユーザーの失敗を最後に返さない（${status}）: $(cat "${out}")"
  [ "$(tr '\n' ' ' < "${steps}")" = "load_products grow_root_filesystem install_os_packages configure_selinux deploy_products report_urls suite_check_deployment " ] \
    || fail "DB ユーザーの失敗で配備を止めた: $(tr '\n' ' ' < "${steps}")"
  grep -qF "WARNING: アプリ用の DB ユーザーを準備できませんでした。製品の配備と起動は続けます" "${out}" || fail "warning: $(cat "${out}")"
  tail -n 1 "${out}" | grep -qF "[suite-init] check: db user NG" || fail "最後の行: $(tail -n 1 "${out}")"
  # 成功すれば確認の結果を返す。
  : > "${steps}"
  main > "${out}" 2>&1 || fail "DB ユーザーが成功したのに失敗した: $(cat "${out}")"
  if grep -qF "WARNING: アプリ用の DB ユーザー" "${out}"; then fail "成功したのに warning を出した"; fi
)

# root の filesystem を広げる（#1984）: oci-growfs を -y で呼ぶ・無ければ飛ばす・失敗しても続ける、空きが少なければ警告。
(
  case_dir="${task_dir}/growfs"
  prepare_case "${case_dir}"
  # shellcheck source=../suite-init.sh
  source "${repo}/platform/deploy/suite-init.sh"
  out="${case_dir}/out.log"
  calls="${case_dir}/growfs.log"
  printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >> "%s"\nexit "${GROWFS_STATUS:-0}"\n' "${calls}" > "${case_dir}/oci-growfs"
  chmod +x "${case_dir}/oci-growfs"
  OCI_GROWFS="${case_dir}/oci-growfs"
  SUITE_ROOT_FS="${case_dir}"
  SUITE_MIN_ROOT_FREE_GB=0
  grow_root_filesystem > "${out}" 2>&1 || fail "広げられたのに失敗した"
  [ "$(cat "${calls}")" = "-y" ] || fail "oci-growfs を -y で呼ばない: $(cat "${calls}")"
  grep -qE "Free space on ${case_dir}: [0-9]+ GB\." "${out}" || fail "空きを log に出さない: $(cat "${out}")"
  if grep -qF "WARNING" "${out}"; then fail "成功したのに warning: $(cat "${out}")"; fi
  # 失敗しても配備は続け、直し方を出す。
  GROWFS_STATUS=1 grow_root_filesystem > "${out}" 2>&1 || fail "oci-growfs の失敗で配備を止めた"
  grep -qF "WARNING: ${OCI_GROWFS} -y が status 1 で終わりました" "${out}" || fail "失敗の warning: $(cat "${out}")"
  # 無ければ飛ばす。空きが目安より少なければ警告する。
  : > "${calls}"
  OCI_GROWFS="${case_dir}/missing-oci-growfs"
  SUITE_MIN_ROOT_FREE_GB=999999999
  grow_root_filesystem > "${out}" 2>&1 || fail "oci-growfs が無いと失敗した"
  [ ! -s "${calls}" ] || fail "無いのに呼んだ"
  grep -qF "Skipping the root filesystem growth" "${out}" || fail "飛ばしたことを出さない: $(cat "${out}")"
  grep -qF "の空きが" "${out}" || fail "空きが少ないのに警告しない: $(cat "${out}")"
)

# dnf の再試行（#1994）: 5 回とも失敗したら最後の status を返す（0 を返して配備を進めない）。成功すればそこで止める。
(
  case_dir="${task_dir}/dnf-retry"
  prepare_case "${case_dir}"
  # shellcheck source=../suite-init.sh
  source "${repo}/platform/deploy/suite-init.sh"
  calls="${case_dir}/dnf.log"
  sleep() { :; }
  dnf() { printf 'dnf %s\n' "$*" >> "${calls}"; return 7; }
  status=0
  suite_dnf install git > "${case_dir}/out.log" 2>&1 || status=$?
  [ "${status}" -eq 7 ] || fail "dnf の失敗の status を返さない（${status}）"
  [ "$(wc -l < "${calls}")" -eq 5 ] || fail "5 回試さない: $(cat "${calls}")"
  grep -qF "dnf failed with status 7 on attempt 5/5" "${case_dir}/out.log" || fail "status の log: $(cat "${case_dir}/out.log")"
  : > "${calls}"
  dnf() { printf 'dnf %s\n' "$*" >> "${calls}"; }
  suite_dnf install git > /dev/null 2>&1 || fail "成功したのに失敗した"
  [ "$(wc -l < "${calls}")" -eq 1 ] || fail "成功の後も試した"
)

# suite_chown_repo（suite-repo.sh。#2307）: 開発の worktree と一時ファイルを辿らずに所有者を変え、除外を空にすれば全体を変える。
(
  # shellcheck source=../suite-repo.sh
  source "${repo}/platform/deploy/suite-repo.sh"
  case_dir="$(mktemp -d "${task_dir}/chown.XXXXXX")"
  repo_dir="${case_dir}/suite"
  mkdir -p "${repo_dir}/.claude/worktrees/agent/frontend" "${repo_dir}/tmp/work" "${repo_dir}/rag/backend"
  touch "${repo_dir}/.claude/worktrees/agent/frontend/a.jpeg" "${repo_dir}/tmp/work/b.md" "${repo_dir}/rag/backend/c.py"
  ln -s rag "${repo_dir}/link"
  calls="${case_dir}/chown.log"
  # find -exec は関数を呼ばないので、PATH の先頭に chown の stub を置く（引数の -h と所有者を除いて path を記録する）。
  mkdir -p "${case_dir}/bin"
  printf '#!/usr/bin/env bash\nshift 2\nprintf "%%s\\n" "$@" >> "%s"\n' "${calls}" > "${case_dir}/bin/chown"
  chmod +x "${case_dir}/bin/chown"
  PATH="${case_dir}/bin:${PATH}"
  suite_chown_repo "app:app" "${repo_dir}"
  grep -qxF "${repo_dir}/rag/backend/c.py" "${calls}" || fail "repository のファイルの所有者を変えない"
  grep -qxF "${repo_dir}/link" "${calls}" || fail "symlink の所有者を変えない"
  if grep -qF -e "${repo_dir}/.claude/worktrees" -e "${repo_dir}/tmp" "${calls}"; then
    fail "worktree・一時ファイルを辿った: $(cat "${calls}")"
  fi
  : > "${calls}"
  SUITE_REPO_EXCLUDE_DIRS="" suite_chown_repo "app:app" "${repo_dir}"
  grep -qxF "${repo_dir}/.claude/worktrees/agent/frontend/a.jpeg" "${calls}" || fail "除外を空にしても全体を辿らない"
)

echo "suite-init: pass"
