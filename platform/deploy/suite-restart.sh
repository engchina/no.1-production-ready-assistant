#!/usr/bin/env bash
# 配備した Compute の上で、今のコードから build して全サービスを再起動する（#1566・#1577）。
#
# DB の起動・停止は扱わない（#10。DB は ADB Free コンテナで、起動・停止は docker start / docker stop）。
# 既定（build して再起動）:
#   1. 各製品の今の backend/.env を props（cloud-init が置いた元）へ写す。suite-init.sh は props から backend/.env を
#      作り直すため、写さないと画面で保存した値（RAG の HuggingFace・解析後の処理
#      など）が消える。元の props は recovery/<時刻>/ に退避する。platform/.env・Wallet は
#      suite-init.sh がそのまま保つ。
#   2. suite-init.sh を再実行する（依存の同期・共有 UI と各製品の frontend の build・DB の初期化と migration・
#      systemd の unit・サービスの再起動）。suite-pull.sh で取得した最新のコードは、ここで使われる。
#   3. 配備全体を確かめる（platform/deploy/suite-health.sh の suite_check_deployment。#1642）: 有効な
#      production-ready-* の service がすべて active か、配備した製品の backend の /api/health が応答するか。
#      製品ごとに 1 行の要約を出し、NG なら journal の末尾を出す。
# --no-build: build せず、有効（enabled）な production-ready-* の service だけを再起動する。
#   RAG の前処理 / parser で画面から「停止」にしたもの（disabled）は起動しない。
# --status: 再起動せず、3. の確認だけを 1 回行う（待たない）。
#
# OS の再起動では、enabled の unit が自動で起動する（WantedBy=multi-user.target）。
# reverse proxy（Nginx など）は配備しない（#19）。前段に置いたときは、その再起動は利用者が行う。
set -euo pipefail

SUITE_DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUITE_RESTART_SCRIPT="${SUITE_DEPLOY_DIR}/suite-restart.sh"
# shellcheck source=suite-products.sh
source "${SUITE_DEPLOY_DIR}/suite-products.sh"
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

restart_log() { printf '[suite-restart] %s\n' "$*"; }
restart_fail() {
  printf '[suite-restart] ERROR: %s\n' "$*" >&2
  return 1
}

restart_usage() {
  cat <<'EOF'
Usage: sudo platform/deploy/suite-restart.sh [--no-build | --status]

引数なし     今のコードから build して再配備し（依存の同期・共有 UI と各製品の frontend の build・DB の migration・
             systemd）、全サービスを再起動して、各製品の backend の health を待ちます。
             suite-pull.sh で取得した最新のコードは、これで反映されます。
--no-build  build せず、有効な production-ready-* の service だけを再起動します。
--status    再起動せず、配備の確認（service の状態・各製品の backend の health）だけを 1 回行います。

画面で保存した値（各製品の backend/.env・platform/.env）と Wallet は保ちます。
log は /var/log/suite-ops.log（再配備の詳細は /var/log/suite-init.log）。

DB（ADB Free コンテナ）の起動・停止はしません。DB が止まっていれば、docker start で起動してから実行してください。

Environment overrides:
  SUITE_HEALTH_TIMEOUT_SECONDS      確認がすべて OK になるまで待つ上限の秒数（既定 180）
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
    suite_check_deployment "${products[@]}"
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

# 今のコードで再配備する（build・migration・systemd・再起動）。
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
      suite_rebuild_and_restart || status=$?
      ;;
    restart)
      suite_ops_begin suite-restart
      suite_restart_services
      ;;
    status) ;;
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
