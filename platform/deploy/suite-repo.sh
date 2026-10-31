#!/usr/bin/env bash
# 配備する repository の中の、配備に含めない作業用のディレクトリ（#2307）。source して使う（単独では実行しない）。
#
#   SUITE_REPO_EXCLUDE_DIRS
#     repository の直下からの相対の path を空白で区切る（既定は開発の worktree と一時ファイル）。検証環境では同じ
#     checkout で開発の worktree が動き、配備の途中でファイルが増減する。repository 全体を辿る処理（restorecon -R・
#     chown -R）が消えたファイルで失敗しないよう、ここに挙げたディレクトリは辿らない。
#   suite_repo_exclude_paths <repo>
#     除外のディレクトリのうち、存在するものの絶対 path（1 行に 1 つ）。
#   suite_chown_repo <owner:group> <repo>
#     除外のディレクトリを辿らずに repository の所有者を変える（chown -R の代わり。symlink はリンクそのものを変える）。
#
# suite-init.sh と各製品の init_script.sh が、配備した repository の platform/deploy/ からこのファイルを source する。

SUITE_REPO_EXCLUDE_DIRS="${SUITE_REPO_EXCLUDE_DIRS-.claude/worktrees tmp}"

suite_repo_exclude_paths() {
  local repo="$1" dir
  for dir in ${SUITE_REPO_EXCLUDE_DIRS}; do
    if [ -d "${repo}/${dir}" ]; then
      printf '%s\n' "${repo}/${dir}"
    fi
  done
}

suite_chown_repo() {
  local owner="$1" repo="$2" path
  local -a prune=()
  while IFS= read -r path; do
    prune+=(-path "${path}" -prune -o)
  done < <(suite_repo_exclude_paths "${repo}")
  find "${repo}" "${prune[@]}" -exec chown -h "${owner}" {} +
}
