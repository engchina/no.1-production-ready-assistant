#!/usr/bin/env bash
# 配備した Compute の上で、今のコードから build して全サービスを再起動する（#1566・#1577）。
#
# 最初に（--status を除く）、ADB（Autonomous AI Database）が止まっていれば起動し、AVAILABLE を待つ（#1745）。
#   platform/deploy/suite-adb.py を、製品の backend の venv の Python で製品のプロセスと同じユーザー（opc）として実行する。
#   認証は platform/.env の OCI の認証（PLATFORM_OCI_AUTH_MODE。既定はシステム設定 › OCI 認証 の ~/.oci/config）、対象は
#   PLATFORM_ORACLE_ADB_OCID。確認・起動は best effort（#1756）: OCI の認証が無い・権限が無い・起動に失敗した・時間切れ
#   （既定 600 秒）なら、理由と手で起動する方法を warning として出し、build・再起動はそのまま続ける（DB に接続できなくても
#   アプリケーションは起動する。元の仕様）。ADB の NG だけでは終了コードを失敗にしない。--status は状態を見るだけ
#   （起動しない）。ADB を停止する処理は持たない。
# 既定（build して再起動）:
#   1. 各製品の今の backend/.env を props（cloud-init が置いた元）へ写す。suite-init.sh は props から backend/.env を
#      作り直すため、写さないと画面で保存した値（RAG の HuggingFace・解析後の処理、NL2SQL の Select AI Credential・
#      Deep Data Security など）が消える。元の props は recovery/<時刻>/ に退避する。platform/.env・Wallet・HTTPS の CA は
#      suite-init.sh がそのまま保つ。
#   2. suite-init.sh を再実行する（依存の同期・共有 UI と各製品の frontend の build・DB の初期化と migration・
#      systemd の unit・Nginx・サービスの再起動）。suite-pull.sh で取得した最新のコードは、ここで使われる。
#   3. 配備全体を確かめる（platform/deploy/suite-health.sh の suite_check_deployment。#1642）: 有効な
#      production-ready-* の service がすべて active か、配備した製品の backend の /api/health と、Nginx を通した
#      /<製品>/api/health が応答するか、Nginx が active か。製品ごとに 1 行の要約を出し、NG なら journal の末尾を出す。
# --no-build: build せず、有効（enabled）な production-ready-* の service と Nginx だけを再起動する。
#   RAG の前処理 / parser で画面から「停止」にしたもの（disabled）は起動しない。証明書の更新の oneshot も対象にしない。
# --status: 再起動せず、3. の確認だけを 1 回行う（待たない）。
#
# OS の再起動では、enabled の unit と Nginx が自動で起動する（WantedBy=multi-user.target）。
set -euo pipefail

SUITE_DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUITE_RESTART_SCRIPT="${SUITE_DEPLOY_DIR}/suite-restart.sh"
# shellcheck source=suite-nginx.sh
source "${SUITE_DEPLOY_DIR}/suite-nginx.sh"
# shellcheck source=suite-health.sh
source "${SUITE_DEPLOY_DIR}/suite-health.sh"

APP_ROOT="${APP_ROOT:-/u01/aipoc}"
SUITE_REPO_DIR="${SUITE_REPO_DIR:-${APP_ROOT}/no.1-production-ready-suite}"
PROPS_DIR="${PROPS_DIR:-${APP_ROOT}/props}"
SUITE_RECOVERY_DIR="${SUITE_RECOVERY_DIR:-${APP_ROOT}/recovery}"
SUITE_INIT_SCRIPT="${SUITE_INIT_SCRIPT:-${SUITE_DEPLOY_DIR}/suite-init.sh}"
# suite-update.sh と共有する lock と log。
SUITE_OPS_LOG="${SUITE_OPS_LOG:-/var/log/suite-ops.log}"
SUITE_OPS_LOCK_FILE="${SUITE_OPS_LOCK_FILE:-/run/lock/production-ready-suite-ops.lock}"
# 確認（suite_check_deployment）がすべて OK になるまで待つ上限。
SUITE_HEALTH_TIMEOUT_SECONDS="${SUITE_HEALTH_TIMEOUT_SECONDS:-180}"
# ADB の起動の確認（#1745）。check（既定）か skip（確かめない。OCI の認証を Compute に置かない環境で、ADB を手で
# 起動して運用するときだけ）。
SUITE_ADB_AUTOSTART="${SUITE_ADB_AUTOSTART:-check}"
# ADB が AVAILABLE になるまで待つ上限の秒数と、状態を問い合わせる間隔の秒数。
SUITE_ADB_START_TIMEOUT_SECONDS="${SUITE_ADB_START_TIMEOUT_SECONDS:-600}"
SUITE_ADB_POLL_INTERVAL_SECONDS="${SUITE_ADB_POLL_INTERVAL_SECONDS:-15}"
SUITE_ADB_SCRIPT="${SUITE_DEPLOY_DIR}/suite-adb.py"
SUITE_PLATFORM_ENV_FILE="${SUITE_PLATFORM_ENV_FILE:-${SUITE_REPO_DIR}/platform/.env}"
# 手で ADB を起動した後に実行し直すコマンド（suite-update.sh から呼ばれたときは suite-update.sh が渡す）。
SUITE_OPS_RERUN_COMMAND="${SUITE_OPS_RERUN_COMMAND:-}"

restart_log() { printf '[suite-restart] %s\n' "$*"; }
restart_fail() {
  printf '[suite-restart] ERROR: %s\n' "$*" >&2
  return 1
}

restart_usage() {
  cat <<'EOF'
Usage: sudo platform/deploy/suite-restart.sh [--no-build | --status]

引数なし     今のコードから build して再配備し（依存の同期・共有 UI と各製品の frontend の build・DB の migration・
             systemd・Nginx）、全サービスを再起動して、各製品の backend の health を待ちます。
             suite-pull.sh で取得した最新のコードは、これで反映されます。
--no-build  build せず、有効な production-ready-* の service と Nginx だけを再起動します。
--status    再起動せず、配備の確認（service の状態・各製品の backend と Nginx を通した health・Nginx）だけを
             1 回行います。

画面で保存した値（各製品の backend/.env・platform/.env）、Wallet、HTTPS の CA は保ちます。
log は /var/log/suite-ops.log（再配備の詳細は /var/log/suite-init.log）。

引数なしと --no-build は、最初に ADB の状態を確かめ、止まっていれば platform/.env の OCI の認証で起動して
AVAILABLE を待ちます。起動できないとき（OCI の認証が無い・権限が無い・時間切れ）は warning を出し、build・再起動は
そのまま続けます（各製品は DB に接続できないまま起動します）。--status は ADB の状態も見ます（起動しない）。

Environment overrides:
  SUITE_HEALTH_TIMEOUT_SECONDS      確認がすべて OK になるまで待つ上限の秒数（既定 180）
  SUITE_ADB_START_TIMEOUT_SECONDS   ADB が AVAILABLE になるまで待つ上限の秒数（既定 600）
  SUITE_ADB_AUTOSTART               check（既定）/ skip（ADB の状態を確かめない）
EOF
}

# suite-restart.sh / suite-update.sh の同時の実行を防ぐ。suite-update.sh から呼ばれたときは、lock と log を引き継ぐ。
suite_ops_begin() {
  local name="$1"
  if [ "${SUITE_OPS_INHERITED:-false}" = "true" ]; then
    return 0
  fi
  install -d -m 0755 "$(dirname "${SUITE_OPS_LOCK_FILE}")"
  exec 9>"${SUITE_OPS_LOCK_FILE}"
  flock -n 9 || { restart_fail "別の更新・再起動が実行中です（${SUITE_OPS_LOCK_FILE}）。"; return 1; }
  exec > >(tee -a "${SUITE_OPS_LOG}") 2>&1
  printf '[%s] 開始（%s）\n' "${name}" "$(date -Is)"
  export SUITE_OPS_INHERITED=true
}

# 有効（enabled）な suite の service（suite_enabled_units）は suite-health.sh にある。

# 配備した製品（props/products.txt）。読めなければ 3 製品。
suite_deployed_products() {
  local names=() name
  read -r -a names <<< "$(tr '\n' ' ' 2>/dev/null < "${PROPS_DIR}/products.txt" || true)"
  for name in "${SUITE_PRODUCTS_ORDER[@]}"; do
    if [ "${#names[@]}" -eq 0 ] || suite_contains "${name}" "${names[@]}"; then
      printf '%s\n' "${name}"
    fi
  done
}

# 配備全体の確認（suite-init.sh の最後と同じ。#1642）。
suite_check_deployed() {
  local products=()
  mapfile -t products < <(suite_deployed_products)
  SUITE_HEALTH_LOG=restart_log SUITE_CHECK_TIMEOUT_SECONDS="${SUITE_HEALTH_TIMEOUT_SECONDS}" \
    suite_check_deployment "$(suite_local_nginx_base_url "${PROPS_DIR}")" "${products[@]}"
}

# 各製品の今の backend/.env を props へ写す（suite-init.sh が props から作り直しても値が変わらないように）。
suite_preserve_backend_envs() {
  local product env_file prop backup_dir=""
  while read -r product; do
    env_file="${SUITE_REPO_DIR}/${product}/backend/.env"
    prop="${PROPS_DIR}/${product}.backend.env"
    [ -f "${env_file}" ] || continue
    if [ -z "${backup_dir}" ]; then
      install -d -m 0700 "${SUITE_RECOVERY_DIR}"
      backup_dir="$(mktemp -d "${SUITE_RECOVERY_DIR}/$(date -u +%Y%m%dT%H%M%SZ).XXXXXX")"
      chmod 0700 "${backup_dir}"
    fi
    if [ -f "${prop}" ]; then
      install -m 0600 "${prop}" "${backup_dir}/${product}.backend.env"
    fi
    install -m 0600 "${env_file}" "${prop}"
    restart_log "${product}: 今の backend/.env を保ちます（元の props は ${backup_dir}/ に退避）。"
  done < <(suite_deployed_products)
}

# ADB の操作（suite-adb.py）に使う Python: 配備した製品のうち、最初に venv がある製品の backend の venv
# （pr_system_settings と OCI SDK が入っている）。SUITE_ADB_PYTHON で変えられる（テスト用）。
suite_adb_python() {
  local product python
  if [ -n "${SUITE_ADB_PYTHON:-}" ]; then
    printf '%s\n' "${SUITE_ADB_PYTHON}"
    return 0
  fi
  while read -r product; do
    python="${SUITE_REPO_DIR}/${product}/backend/.venv/bin/python"
    if [ -x "${python}" ]; then
      printf '%s\n' "${python}"
      return 0
    fi
  done < <(suite_deployed_products)
  return 1
}

# suite-adb.py を製品のプロセスと同じユーザー（APP_USER。~/.oci/config はこのユーザーの home）で実行する。
# root でなければ（テスト）そのまま実行する。$1: ensure / status、残り: suite-adb.py の引数。
suite_run_adb() {
  local user="${APP_USER:-opc}"
  local home="${APP_HOME:-/home/${user}}"
  local python limit cmd=()
  if ! python="$(suite_adb_python)"; then
    restart_log "ADB: 製品の backend の venv（${SUITE_REPO_DIR}/<製品>/backend/.venv）がありません。配備（suite-init.sh）が終わっているか確認してください。"
    return 1
  fi
  # Python の側でも上限の時間で止まる。OCI の API が応答しないときのための余白を足す。
  limit=$(( ${SUITE_ADB_START_TIMEOUT_SECONDS%.*} + 120 ))
  cmd=(timeout "${limit}" "${python}" "${SUITE_ADB_SCRIPT}" "$@" --env-file "${SUITE_PLATFORM_ENV_FILE}")
  if [ "$(id -u)" -eq 0 ]; then
    (cd / && runuser -u "${user}" -- env HOME="${home}" PATH="/usr/local/bin:/usr/bin:/bin" "${cmd[@]}")
  else
    "${cmd[@]}"
  fi
}

# check（既定）なら 0、skip なら 1。それ以外の値は warning を出して check として扱う（止めない。#1756）。
suite_adb_autostart_enabled() {
  case "${SUITE_ADB_AUTOSTART}" in
    check) return 0 ;;
    skip) return 1 ;;
    *)
      restart_log "WARNING: SUITE_ADB_AUTOSTART は check か skip にしてください（${SUITE_ADB_AUTOSTART}）。check として扱います。"
      return 0
      ;;
  esac
}

# DB を使う処理（build の migration・system schema の初期化・サービスの起動）の前に、ADB が止まっていれば起動し、
# AVAILABLE を待つ（#1745）。best effort（#1756）: NG でも warning を出して 0 を返す（呼び出し元は build・再起動を
# 続ける。DB に接続できなくてもアプリケーションは起動する）。$1: 手で ADB を起動した後に実行し直すコマンド。
suite_ensure_adb_available() {
  local rerun="$1" status=0
  if ! suite_adb_autostart_enabled; then
    restart_log "ADB: 状態を確かめません（SUITE_ADB_AUTOSTART=skip）。止まっていれば、DB を使う処理が失敗します。"
    return 0
  fi
  restart_log "ADB の状態を確かめます（止まっていれば起動し、AVAILABLE を最大 ${SUITE_ADB_START_TIMEOUT_SECONDS} 秒待ちます）。"
  suite_run_adb ensure --timeout-seconds "${SUITE_ADB_START_TIMEOUT_SECONDS}" \
    --interval-seconds "${SUITE_ADB_POLL_INTERVAL_SECONDS}" --script-name "${rerun}" || status=$?
  if [ "${status}" -eq 0 ]; then
    return 0
  fi
  if [ "${status}" -eq 124 ]; then
    restart_log "[suite-adb] NG: ADB の確認が時間内に終わりませんでした（OCI の API が応答しません）。"
  fi
  restart_log "WARNING: ADB を使える状態にできませんでした（上の [suite-adb] NG の行に理由と手で起動する方法があります）。build・再起動は続けます。DB に接続できない間もアプリケーションは起動し、system_admin はログインできます。system_admin で「システム設定 › データベース」から ADB を起動すれば、そのまま DB を使えます（sudo ${rerun} の再実行は要りません。最後の確認は配備の時点の DB を見て schema を NG にします）。"
  return 0
}

# --status: ADB の状態を見るだけ（起動しない）。結果は確認の終了コードに含めない（OCI の認証を置かない環境もある）。
suite_report_adb_status() {
  suite_adb_autostart_enabled || return 0
  suite_run_adb status --script-name "${SUITE_RESTART_SCRIPT}" || true
}

# 今のコードで再配備する（build・migration・systemd・Nginx・再起動）。
suite_rebuild_and_restart() {
  local status=0
  suite_preserve_backend_envs
  restart_log "今のコードから build して再起動します（${SUITE_INIT_SCRIPT}。詳細は /var/log/suite-init.log）。"
  APP_ROOT="${APP_ROOT}" bash "${SUITE_INIT_SCRIPT}" || status=$?
  if [ "${status}" -ne 0 ]; then
    restart_log "失敗した製品があります（status=${status}）。/var/log/suite-init.log と /var/log/<製品>-init.log を確認してください。"
  fi
  return "${status}"
}

# build せずにサービスだけを再起動する。
suite_restart_services() {
  local units=()
  mapfile -t units < <(suite_enabled_units)
  if [ "${#units[@]}" -eq 0 ]; then
    restart_fail "有効な ${SUITE_UNIT_PATTERN} がありません。配備（suite-init.sh）が終わっているか確認してください。"
    return 1
  fi
  restart_log "再起動します: ${units[*]}"
  systemctl daemon-reload
  systemctl restart "${units[@]}"
  restart_log "Nginx の設定を確かめて再起動します。"
  nginx -t
  systemctl restart nginx
}

suite_restart_main() {
  local action="build" status=0
  case "${1:-}" in
    -h | --help) restart_usage; return 0 ;;
    --no-build) action="restart" ;;
    --status)
      action="status"
      # 状態の確認では待たない（1 回だけ問い合わせる）。
      SUITE_HEALTH_TIMEOUT_SECONDS=0
      SUITE_CHECK_SETTLE_SECONDS=0
      ;;
    "") ;;
    *) restart_usage >&2; return 2 ;;
  esac
  if [ "$(id -u)" -ne 0 ]; then
    restart_fail "root で実行してください: sudo ${SUITE_RESTART_SCRIPT} ${1:-}"
    return 1
  fi
  case "${action}" in
    build)
      suite_ops_begin suite-restart
      suite_ensure_adb_available "${SUITE_OPS_RERUN_COMMAND:-${SUITE_RESTART_SCRIPT}}"
      suite_rebuild_and_restart || status=$?
      ;;
    restart)
      suite_ops_begin suite-restart
      suite_ensure_adb_available "${SUITE_RESTART_SCRIPT} --no-build"
      suite_restart_services
      ;;
    status)
      suite_report_adb_status
      ;;
  esac
  if suite_check_deployed && [ "${status}" -eq 0 ]; then
    if [ "${action}" != "status" ]; then
      restart_log "完了しました。すべての service が active で、各製品が応答しています。"
    fi
    return 0
  fi
  restart_fail "失敗または応答しない製品があります（上の log を確認してください）。"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  suite_restart_main "$@"
  exit $?
fi
