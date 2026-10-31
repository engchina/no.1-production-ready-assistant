#!/usr/bin/env bash
# PR を main へ merge してよいかを判定する（merge はしない。merge は `gh pr merge <PR> --merge` で明示的に行う）。
#
# 使い方（リポジトリの中で）:
#   scripts/pr-merge-check.sh <PR>             # PR の最新 commit の CI を待ってから判定する
#   scripts/pr-merge-check.sh --update <PR>    # needs-update-branch なら `gh pr update-branch` も行う
#   scripts/pr-merge-check.sh --no-wait <PR>   # CI を待たない（実行中なら ci-pending）
#
# 判定（標準出力の 1 行目と終了コード）:
#   ready-to-merge        0  CI OK が成功し、CI の開始後の main の変更が PR と重ならない（変わっていないときを含む）
#   ci-failed             1  CI の run が成功しなかった、または CI OK が成功ではない
#   needs-update-branch   3  CI の開始後の main の変更が PR と重なる。main を取り込んで CI をやり直す
#   ci-pending            4  --no-wait で、CI がまだ終わっていない（または run がまだ無い）
#   (エラー)              2  使い方の誤り・PR が OPEN でない・待ちの時間切れ
#
# 「重なる」（AGENTS.md「main の取り込み」。#1581）: CI の開始の時点の main（ci_base）から origin/main までに
# 変わったファイルと、PR の変更（merge-base..PR の head）を比べ、次のどちらかなら重なるとする。
#   (a) 同じファイルが両方で変わっている。
#   (b) main で全体に効くファイル（下の GLOBAL_PATTERNS）が変わっている。
# それ以外の main の変更は、merge の後の main の push の CI（失敗すれば ci-failure の Issue）で確かめる。
# 「CI の開始」は PR の最新 commit の CI の run の createdAt で、その時点の main（first-parent）を ci_base とする。
#
# 環境変数: PR_MERGE_CHECK_INTERVAL（待ちの間隔の秒。既定 30）、PR_MERGE_CHECK_TIMEOUT（待ちの上限の秒。既定 5400）、
#           PR_MERGE_CHECK_REPO_DIR（git を実行するディレクトリ。既定はこのリポジトリ）。
set -euo pipefail

# main で変わると、PR が触らないファイルでも PR の CI の結果が変わりうるファイル（bash の [[ == ]] のパターン。
# `*` は `/` もまたぐ）。ここに当たる変更が main にあれば、PR の変更と重ならなくても取り込んでやり直す。
GLOBAL_PATTERNS=(
  # 依存の解決（npm / uv / Python の版）。依存や lock が変わると、build・型・テストの前提が変わる。
  # platform/packages/*/package.json（共有 UI などの依存と exports）も "*/package.json" に含まれる。
  "package.json" "*/package.json"
  "package-lock.json" "*/package-lock.json"
  "uv.lock" "*/uv.lock"
  "pyproject.toml" "*/pyproject.toml"
  ".python-version" "*/.python-version"
  # CI の定義そのもの。job の選び方・手順・共有の action が変わると、PR の CI の検査の範囲が変わる。
  ".github/workflows/*"
  ".github/actions/*"
  # frontend の build・型・テストの設定。製品のすべてのファイルの検査に効く。
  "*/playwright.config.*"
  "*/vite.config.*"
  "*/vitest.config.*"
  "*/tsconfig*.json"
  # e2e の fixture・共通の helper。製品のすべての spec が読む。
  "rag/frontend/e2e/fixtures/*"
  "agent/frontend/e2e/fixtures/*"
  "nl2sql/frontend/tests/e2e/_helpers/*"
  # PR の e2e の spec の選び方（e2e-impact）。変わると、PR で流すべき spec の集合が変わる。
  "platform/scripts/e2e_impact.py"
  "platform/scripts/e2e-impact/*"
)

# 製品の下の依存のファイル（その製品のその部分だけに効く。#1634）。
PRODUCT_DEP_PATTERNS=(
  "rag/*/package.json" "rag/*/package-lock.json" "rag/*/uv.lock" "rag/*/pyproject.toml"
  "nl2sql/*/package.json" "nl2sql/*/package-lock.json" "nl2sql/*/uv.lock" "nl2sql/*/pyproject.toml"
  "agent/*/package.json" "agent/*/package-lock.json" "agent/*/uv.lock" "agent/*/pyproject.toml"
)

# is_global <path> <PR の変更の一覧>: 全体に効くなら 0。
# 製品の下の依存のファイル（例: agent/backend/uv.lock）は、PR がそのディレクトリの下を触るときだけ全体に効くとみなす
# （別の製品・別の backend の lock は、その PR の build・テストの前提を変えない。#1634）。
is_global() {
  local file="$1" touched="$2" pattern dir
  for pattern in "${PRODUCT_DEP_PATTERNS[@]}"; do
    # shellcheck disable=SC2053 # 右辺はパターンとして照合する。
    if [[ "${file}" == ${pattern} ]]; then
      dir="${file%/*}/"
      awk -v d="${dir}" 'index($0, d) == 1 { found = 1 } END { exit !found }' <<<"${touched}" && return 0
      return 1
    fi
  done
  for pattern in "${GLOBAL_PATTERNS[@]}"; do
    # shellcheck disable=SC2053 # 右辺はパターンとして照合する。
    [[ "${file}" == ${pattern} ]] && return 0
  done
  return 1
}

usage() {
  sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

wait_ci=1
update=0
pr=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --update) update=1 ;;
    --no-wait) wait_ci=0 ;;
    -h | --help) usage ;;
    -*)
      echo "不明なオプションです: $1" >&2
      usage
      ;;
    *)
      [ -z "${pr}" ] || usage
      pr="$1"
      ;;
  esac
  shift
done
[[ "${pr}" =~ ^[0-9]+$ ]] || usage

interval="${PR_MERGE_CHECK_INTERVAL:-30}"
timeout="${PR_MERGE_CHECK_TIMEOUT:-5400}"
repo_dir="${PR_MERGE_CHECK_REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

log() { echo "[pr-merge-check] $*" >&2; }

info="$(gh pr view "${pr}" --json state,headRefName,headRefOid -q '"\(.state) \(.headRefName) \(.headRefOid)"')"
read -r state branch head_sha <<<"${info}"
if [ "${state}" != "OPEN" ]; then
  log "PR #${pr} は ${state} です"
  echo "not-open"
  exit 2
fi
log "PR #${pr} ${branch} ${head_sha}"

# PR の最新 commit の CI の run（push の直後はまだ無く、ブランチの最新の run が前の commit のことがある）。
find_run() {
  gh run list --workflow CI --branch "${branch}" --limit 20 \
    --json databaseId,headSha,createdAt,status,conclusion \
    -q ".[] | select(.headSha==\"${head_sha}\") | \"\(.databaseId) \(.createdAt) \(.status) \(.conclusion)\"" |
    head -1
}

deadline=$((SECONDS + timeout))
while :; do
  run="$(find_run)"
  run_status=""
  if [ -n "${run}" ]; then
    read -r run_id created_at run_status conclusion <<<"${run}"
    [ "${run_status}" = "completed" ] && break
  fi
  if [ "${wait_ci}" = 0 ]; then
    log "CI はまだ終わっていません（${run_status:-run がまだありません}）"
    echo "ci-pending"
    exit 4
  fi
  if [ "${SECONDS}" -ge "${deadline}" ]; then
    log "CI の待ちが ${timeout} 秒を超えました（${run_status:-run がまだありません}）"
    echo "timeout"
    exit 2
  fi
  sleep "${interval}"
done
log "CI の run ${run_id}（${created_at} 開始）: ${conclusion}"

ci_ok="$(gh pr view "${pr}" --json statusCheckRollup \
  -q '.statusCheckRollup[] | select(.name=="CI OK") | .conclusion' | sort -u | tr '\n' ' ')"
if [ "${conclusion}" != "success" ] || [ "${ci_ok}" != "SUCCESS " ]; then
  log "CI OK: ${ci_ok:-無し}"
  latest="$(gh pr view "${pr}" --json headRefOid -q .headRefOid)"
  if [ "${latest}" != "${head_sha}" ]; then
    log "待っている間に新しい commit（${latest:0:12}）が push されました。もう一度実行してください"
  fi
  echo "ci-failed"
  exit 1
fi

git -C "${repo_dir}" fetch -q origin main
ci_base="$(git -C "${repo_dir}" rev-list -1 --first-parent --before="${created_at}" origin/main)"
# rename は削除と追加に分け、旧名と新名の両方で比べる。
changed="$(git -C "${repo_dir}" diff --no-renames --name-only "${ci_base}..origin/main")"
if [ -z "${changed}" ]; then
  log "CI の開始後に main は変わっていません（${ci_base:0:12}..origin/main）"
  echo "ready-to-merge"
  exit 0
fi

# PR の変更（merge-base..PR の head）。head を取得できなければ、GitHub の PR の差分（同じ範囲）を使う。
if git -C "${repo_dir}" fetch -q origin "${head_sha}" 2>/dev/null; then
  touched="$(git -C "${repo_dir}" diff --no-renames --name-only "origin/main...${head_sha}")"
else
  log "PR の head（${head_sha:0:12}）を取得できないため、gh pr diff の一覧で比べます"
  touched="$(gh pr diff "${pr}" --name-only)"
fi

same="$(comm -12 <(sort -u <<<"${changed}") <(sort -u <<<"${touched}") | sed '/^$/d')"
global=""
while IFS= read -r file; do
  if is_global "${file}" "${touched}"; then
    global+="${file}"$'\n'
  fi
done <<<"${changed}"
global="${global%$'\n'}"

count="$(wc -l <<<"${changed}")"
if [ -z "${same}" ] && [ -z "${global}" ]; then
  log "CI の開始後に main は変わったが重ならない（${count} ファイル。${ci_base:0:12}..origin/main）"
  echo "ready-to-merge"
  exit 0
fi

log "CI の開始後の main の変更が PR と重なります（main の ${count} ファイル。${ci_base:0:12}..origin/main）"
if [ -n "${same}" ]; then
  log "PR と同じファイル:"
  head -20 <<<"${same}" | sed 's/^/  /' >&2
fi
if [ -n "${global}" ]; then
  log "全体に効くファイル:"
  head -20 <<<"${global}" | sed 's/^/  /' >&2
fi
if [ "${update}" = 1 ]; then
  gh pr update-branch "${pr}" >&2
  log "main を取り込みました。新しい commit の CI を待って、もう一度実行してください"
fi
echo "needs-update-branch"
exit 3
