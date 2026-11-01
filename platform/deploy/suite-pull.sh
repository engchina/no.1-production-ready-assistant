#!/usr/bin/env bash
# 配備した Compute の上で、suite の repository を origin の main の最新にする（#1566）。
#
# - repository は cloud-init が `git clone --depth 1` で入れ、各製品の init_script.sh が opc に chown する。
#   root のまま git を実行すると「dubious ownership」で失敗するため、git は repository の所有者で実行する。
# - 追跡ファイルに変更があれば中止する（手で直したファイルを黙って消さない）。未追跡のファイルは保ち、
#   取得したファイルと衝突するときは git が更新を拒否する。
# - 取得だけを行う。依存の同期・build・migration・再起動は suite-restart.sh（sudo）が行う（suite-update.sh は両方）。
set -euo pipefail

APP_ROOT="${APP_ROOT:-/u01/aipoc}"
SUITE_REPO_DIR="${SUITE_REPO_DIR:-${APP_ROOT}/no.1-production-ready-assistant}"
SUITE_GIT_REF="${SUITE_GIT_REF:-main}"

pull_log() { printf '[suite-pull] %s\n' "$*"; }
pull_fail() {
  printf '[suite-pull] ERROR: %s\n' "$*" >&2
  return 1
}

pull_usage() {
  cat <<'EOF'
Usage: platform/deploy/suite-pull.sh

suite の repository（既定 /u01/aipoc/no.1-production-ready-assistant）を origin の main の最新にします。
opc でも sudo でも実行できます（git は repository の所有者で実行します）。
追跡ファイルに変更があるときは中止します。取得したコードは、sudo で suite-restart.sh を実行すると
build・再起動されて反映されます（suite-update.sh は取得から build・再起動までをまとめて行います）。

Environment overrides:
  SUITE_REPO_DIR  repository（既定 /u01/aipoc/no.1-production-ready-assistant）
  SUITE_GIT_REF   取得する branch（既定 main）
EOF
}

# repository の所有者で git を実行する（root のときは sudo -u で降りる）。
suite_git() {
  local owner
  owner="$(stat -c '%U' "${SUITE_REPO_DIR}")"
  if [ "$(id -un)" = "${owner}" ]; then
    git -C "${SUITE_REPO_DIR}" "$@"
  elif [ "$(id -u)" -eq 0 ]; then
    sudo -n -H -u "${owner}" -- git -C "${SUITE_REPO_DIR}" "$@"
  else
    pull_fail "repository の所有者（${owner}）か root で実行してください: ${SUITE_REPO_DIR}"
  fi
}

suite_pull() {
  local changes before after
  command -v git >/dev/null || { pull_fail "git が見つかりません。"; return 1; }
  [ -d "${SUITE_REPO_DIR}/.git" ] || { pull_fail "git の repository がありません: ${SUITE_REPO_DIR}"; return 1; }
  suite_git remote get-url origin >/dev/null || { pull_fail "origin の remote がありません。"; return 1; }
  changes="$(suite_git status --porcelain --untracked-files=no)" || return 1
  if [ -n "${changes}" ]; then
    printf '%s\n' "${changes}" >&2
    pull_fail "追跡ファイルに変更があります。退避（git stash など）してから再実行してください。"
    return 1
  fi
  before="$(suite_git rev-parse --short HEAD)"
  pull_log "origin/${SUITE_GIT_REF} を取得します（${SUITE_REPO_DIR}、今は ${before}）。"
  # 配備の clone は depth 1 のため、取得した commit へ branch を付け替える（fast-forward の判定は履歴が要るので使わない）。
  # 履歴をすべて持つ clone（開発機を配備にも使う場合。worktree が同じ .git を共有する）は depth を付けずに取る。
  # depth を付けると .git が shallow になり、worktree の merge-base・merge が誤る。
  local depth_args=()
  if [ "$(suite_git rev-parse --is-shallow-repository)" = "true" ]; then
    depth_args=(--depth 1)
  fi
  suite_git fetch "${depth_args[@]}" origin "${SUITE_GIT_REF}" || { pull_fail "取得に失敗しました。"; return 1; }
  if ! suite_git checkout -B "${SUITE_GIT_REF}" FETCH_HEAD; then
    pull_fail "取得した commit へ切り替えられませんでした（未追跡のファイルとの衝突など）。上の git のエラーを確認してください。"
    return 1
  fi
  after="$(suite_git rev-parse --short HEAD)"
  if [ "${before}" = "${after}" ]; then
    pull_log "すでに最新です: $(suite_git log -1 --format='%h %cs %s')"
  else
    pull_log "${before} → $(suite_git log -1 --format='%h %cs %s')"
  fi
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  case "${1:-}" in
    -h | --help) pull_usage; exit 0 ;;
    "") suite_pull ;;
    *) pull_usage >&2; exit 2 ;;
  esac
fi
