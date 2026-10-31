#!/usr/bin/env bash
# 配備した Compute の上で、最新のコード（origin の main）を取得し、build して全サービスを再起動する（#1566・#1577）。
#
#   1. suite-pull.sh で repository を main の最新にする（追跡ファイルに変更があれば中止）。
#   2. 取得した新しい suite-restart.sh を実行する（build と再起動の手順そのものも最新にする）。
#      suite-restart.sh は、ADB が止まっていれば起動して AVAILABLE を待ち（#1745）、今の backend/.env を保ったまま
#      suite-init.sh で再配備し、health を待つ。
# lock と log（/var/log/suite-ops.log）は suite-restart.sh と共有する。
set -euo pipefail

SUITE_DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=suite-pull.sh
source "${SUITE_DEPLOY_DIR}/suite-pull.sh"
# shellcheck source=suite-restart.sh
source "${SUITE_DEPLOY_DIR}/suite-restart.sh"

update_usage() {
  cat <<'EOF'
Usage: sudo platform/deploy/suite-update.sh

origin の main を取得し（suite-pull.sh）、取得した新しい suite-restart.sh で build して全サービスを再起動します。
build の前に ADB の状態を確かめ、止まっていれば platform/.env の OCI の認証で起動して AVAILABLE を待ちます。
画面で保存した値（各製品の backend/.env・platform/.env）、Wallet、HTTPS の CA は保ちます。
log は /var/log/suite-ops.log（再配備の詳細は /var/log/suite-init.log）。
EOF
}

suite_update_main() {
  case "${1:-}" in
    -h | --help) update_usage; return 0 ;;
    "") ;;
    *) update_usage >&2; return 2 ;;
  esac
  if [ "$(id -u)" -ne 0 ]; then
    printf '[suite-update] ERROR: root で実行してください: sudo %s\n' "$0" >&2
    return 1
  fi
  suite_ops_begin suite-update
  suite_pull
  # 取得した新しい suite-restart.sh で続ける（lock の fd 9 と log は引き継ぐ）。ADB を手で起動した後は、
  # suite-update.sh を実行し直してもらう（取得は済んでいるので、取得は「すでに最新です」で進む）。
  export SUITE_OPS_RERUN_COMMAND="${SUITE_DEPLOY_DIR}/suite-update.sh"
  exec bash "${SUITE_RESTART_SCRIPT}"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  suite_update_main "$@"
  exit $?
fi
