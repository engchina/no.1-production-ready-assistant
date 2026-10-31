#!/usr/bin/env bash
# scripts/pr-merge-check.sh の判定を、偽の gh と一時的な git リポジトリで確かめる（GitHub には接続しない）。
set -euo pipefail

script="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/pr-merge-check.sh"
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

# --- origin（bare）と 2 つの clone。author で main と PR の commit を作って push し、repo（判定する側）は
# fetch だけで受け取る（PR の head を origin から取得する経路を通す）。
git init -q --bare -b main "${work}/origin.git"
git clone -q "${work}/origin.git" "${work}/author" 2>/dev/null
git clone -q "${work}/origin.git" "${work}/repo" 2>/dev/null
author() { git -C "${work}/author" -c user.name=t -c user.email=t@example.invalid "$@"; }
commit() { # commit <ISO 時刻> <path>（path が mv:<旧>:<新> なら rename）
  if [[ "$2" == mv:* ]]; then
    local from="${2#mv:}"
    author mv "${from%%:*}" "${from#*:}"
  else
    mkdir -p "$(dirname "${work}/author/$2")"
    echo "$1 $2" >>"${work}/author/$2"
    author add "$2"
  fi
  GIT_AUTHOR_DATE="$1" GIT_COMMITTER_DATE="$1" author commit -q -m "$2"
}
commit 2026-10-01T00:00:00Z README.md
commit 2026-10-01T00:00:00Z rag/old.py
author push -q origin HEAD:main
base="$(author rev-parse HEAD)"

# set_main <path…>: main を base から作り直し、CI の開始（01:00）より後の 02:00 に path を変える。
set_main() {
  author checkout -q --detach "${base}"
  local path
  for path in "$@"; do
    commit 2026-10-01T02:00:00Z "${path}"
  done
  author push -q -f origin HEAD:main
}
# set_pr <path…>: PR の head を base から作り（00:30）、refs/pull/1/head に push して FAKE_PR_INFO に入れる。
set_pr() {
  author checkout -q --detach "${base}"
  local path
  for path in "$@"; do
    commit 2026-10-01T00:30:00Z "${path}"
  done
  author push -q -f origin HEAD:refs/pull/1/head
  FAKE_PR_INFO="OPEN codex/1-x $(author rev-parse HEAD)"
}

# --- 偽の gh。応答は FAKE_* の環境変数で決め、呼ばれた操作を log に残す。
mkdir -p "${work}/bin"
cat >"${work}/bin/gh" <<'EOF'
#!/usr/bin/env bash
echo "$*" >>"${FAKE_LOG}"
case "$1 $2" in
  "pr view")
    if [[ "$*" == *statusCheckRollup* ]]; then
      printf '%s\n' "${FAKE_CI_OK}"
    elif [[ "$*" == *"-q .headRefOid"* ]]; then
      # 判定の後の今の head（FAKE_HEAD_NOW が無ければ最初と同じ）。
      echo "${FAKE_HEAD_NOW:-${FAKE_PR_INFO##* }}"
    else
      echo "${FAKE_PR_INFO}"
    fi
    ;;
  "run list")
    # FAKE_RUNS は「;」区切りの応答の列。呼ばれるたびに次へ進み、最後の応答を繰り返す。
    n="$(cat "${FAKE_LOG}.runs" 2>/dev/null || echo 0)"
    IFS=';' read -r -a runs <<<"${FAKE_RUNS}"
    i=$((n < ${#runs[@]} ? n : ${#runs[@]} - 1))
    echo $((n + 1)) >"${FAKE_LOG}.runs"
    [ "${#runs[@]}" -gt 0 ] && [ -n "${runs[$i]}" ] && echo "${runs[$i]}"
    exit 0
    ;;
  # PR の head を取得できないときだけ使う（FAKE_FILES）。
  "pr diff") printf '%s\n' "${FAKE_FILES}" ;;
  "pr update-branch") echo "updated" ;;
  *)
    echo "想定外の gh の呼び出し: $*" >&2
    exit 99
    ;;
esac
EOF
chmod +x "${work}/bin/gh"

failures=0
fail() {
  echo "FAIL $*"
  failures=$((failures + 1))
}
# run_case <名前> <期待の出力> <期待の終了コード> [引数…]（FAKE_* は呼び出し前に設定する）
run_case() {
  local name="$1" want_out="$2" want_code="$3"
  shift 3
  : >"${work}/gh.log"
  rm -f "${work}/gh.log.runs"
  local out code=0
  out="$(PATH="${work}/bin:${PATH}" FAKE_LOG="${work}/gh.log" PR_MERGE_CHECK_REPO_DIR="${work}/repo" \
    PR_MERGE_CHECK_INTERVAL=0 PR_MERGE_CHECK_TIMEOUT=5 FAKE_PR_INFO="${FAKE_PR_INFO}" \
    bash "${script}" "$@" 2>"${work}/stderr")" || code=$?
  if [ "${out}" = "${want_out}" ] && [ "${code}" = "${want_code}" ]; then
    echo "ok   ${name}"
  else
    fail "${name}: 出力=${out} 終了=${code}（期待 ${want_out} / ${want_code}）"
    sed 's/^/     /' "${work}/stderr"
  fi
  if grep -q "^pr merge" "${work}/gh.log"; then
    fail "${name}: merge を呼んだ"
  fi
}
# expect_log <名前> <stderr に含まれるべき文字列>
expect_log() {
  grep -qF -- "$2" "${work}/stderr" || fail "$1: log に「$2」が無い"
}

export FAKE_CI_OK="SUCCESS"
export FAKE_RUNS="11 2026-10-01T01:00:00Z completed success"
export FAKE_FILES=""

# --- 重ならない main の変更は ready-to-merge のまま（#1581）。
set_main nl2sql/x.py docs/note.md
set_pr rag/a.py
run_case "main は変わったが重ならない" ready-to-merge 0 1
expect_log "重ならない" "main は変わったが重ならない（2 ファイル"
if grep -q "^pr diff" "${work}/gh.log"; then
  fail "PR の head を取得できるのに gh pr diff を呼んだ"
fi

set_main rag/x.py
set_pr platform/a.ts .github/scripts/a.sh
run_case "platform を触る PR でも、別のファイルの変更なら重ならない" ready-to-merge 0 1

set_main platform/packages/ui/src/a.ts AGENTS.md
set_pr rag/a.py
run_case "main の platform のソースの変更は全体に効くファイルではない" ready-to-merge 0 1

# 全体に効くファイルに似ているが当たらないもの。
set_main rag/frontend/e2e/chat.spec.ts .github/ISSUE_TEMPLATE/issue.md .github/dependabot.yml \
  platform/scripts/log_viewer.py rag/frontend/src/package-info.ts nl2sql/frontend/tests/e2e/chat.spec.ts \
  rag/backend/pyproject.md
set_pr terraform/a.tf
run_case "全体に効くファイルに似た名前は重ならない" ready-to-merge 0 1

# --- (a) 同じファイル。
set_main rag/a.py nl2sql/x.py
set_pr rag/a.py
run_case "同じファイルが main と PR で変わった" needs-update-branch 3 1
expect_log "同じファイル" "  rag/a.py"
if grep -q "  nl2sql/x.py" "${work}/stderr"; then
  fail "同じファイル: 重ならないファイルを重なりとして出した"
fi

set_main mv:rag/old.py:rag/new.py
set_pr rag/old.py
run_case "main で rename されたファイルを PR が変えた" needs-update-branch 3 1

# --- (b) main で全体に効くファイルが変わった（PR は別のファイルだけを触る）。
for path in package.json platform/package-lock.json .python-version \
  .github/workflows/ci.yml .github/actions/platform-ui/action.yml \
  agent/frontend/playwright.config.ts nl2sql/frontend/vite.config.ts rag/frontend/vitest.config.ts \
  rag/frontend/tsconfig.json platform/packages/ui/tsconfig.build.json \
  rag/frontend/e2e/fixtures/test.ts agent/frontend/e2e/fixtures/mock-api.ts \
  nl2sql/frontend/tests/e2e/_helpers/test.ts platform/packages/ui/package.json \
  platform/scripts/e2e_impact.py platform/scripts/e2e-impact/fixture.ts; do
  set_main docs/note.md "${path}"
  set_pr terraform/a.tf
  run_case "全体に効くファイル ${path}" needs-update-branch 3 1
  expect_log "全体に効くファイル ${path}" "  ${path}"
done

# --- 製品の下の依存のファイル（#1634）: PR がそのディレクトリの下を触るときだけ、全体に効く。
for path in rag/frontend/package.json rag/frontend/package-lock.json rag/backend/uv.lock \
  rag/services/parsers/docling/uv.lock nl2sql/backend/pyproject.toml agent/backend/uv.lock; do
  dir="${path%/*}"
  set_main docs/note.md "${path}"
  set_pr terraform/a.tf
  run_case "別の場所の製品の依存 ${path}" ready-to-merge 0 1
  set_main docs/note.md "${path}"
  set_pr "${dir}/src/a.txt"
  run_case "同じ場所の製品の依存 ${path}" needs-update-branch 3 1
  expect_log "同じ場所の製品の依存 ${path}" "  ${path}"
done
# 別の製品の backend の lock は、ほかの製品の PR に効かない。
set_main agent/backend/uv.lock nl2sql/backend/uv.lock
set_pr rag/backend/uv.lock
run_case "別の製品の lock だけが変わった" ready-to-merge 0 1

# --- CI の開始の時点と、PR の head の取得。
set_main rag/a.py
set_pr rag/a.py
FAKE_RUNS="11 2026-10-01T04:00:00Z completed success" \
  run_case "CI の開始が main の変更より後" ready-to-merge 0 1
expect_log "CI の開始が main の変更より後" "main は変わっていません"

set_main rag/a.py
FAKE_PR_INFO="OPEN codex/1-x 1111111111111111111111111111111111111111" FAKE_FILES="rag/a.py" \
  run_case "PR の head を取得できなければ gh pr diff で比べる" needs-update-branch 3 1
expect_log "gh pr diff で比べる" "gh pr diff の一覧で比べます"
FAKE_PR_INFO="OPEN codex/1-x 1111111111111111111111111111111111111111" FAKE_FILES="agent/a.py" \
  run_case "gh pr diff の一覧でも重ならなければ ready-to-merge" ready-to-merge 0 1

# --- --update。
set_main rag/a.py
set_pr rag/a.py
run_case "--update で update-branch する" needs-update-branch 3 --update 1
grep -q "^pr update-branch 1" "${work}/gh.log" || fail "--update で gh pr update-branch を呼んでいない"
run_case "--update が無ければ update-branch しない" needs-update-branch 3 1
if grep -q "^pr update-branch" "${work}/gh.log"; then
  fail "--update が無いのに update-branch を呼んだ"
fi
set_pr agent/a.py
run_case "重ならなければ --update でも update-branch しない" ready-to-merge 0 --update 1
if grep -q "^pr update-branch" "${work}/gh.log"; then
  fail "重ならないのに update-branch を呼んだ"
fi

# --- CI の失敗・待ち（main と PR の重なりは見ない）。
FAKE_PR_INFO="OPEN codex/1-x abc123"
FAKE_RUNS="11 2026-10-01T04:00:00Z completed failure" \
  run_case "CI の run が失敗" ci-failed 1 1
FAKE_CI_OK="FAILURE" FAKE_RUNS="11 2026-10-01T04:00:00Z completed success" \
  run_case "CI OK が成功ではない" ci-failed 1 1
FAKE_CI_OK="" FAKE_RUNS="11 2026-10-01T04:00:00Z completed success" \
  run_case "CI OK が無い" ci-failed 1 1
if grep -q "新しい commit" "${work}/stderr"; then
  fail "head が変わっていないのに新しい commit の案内を出した"
fi
FAKE_CI_OK="" FAKE_HEAD_NOW="def456" \
  FAKE_RUNS="11 2026-10-01T04:00:00Z completed cancelled" \
  run_case "待つ間に push されて run が取り消された" ci-failed 1 1
expect_log "待つ間に push" "新しい commit（def456）"

FAKE_RUNS="11 2026-10-01T01:00:00Z in_progress " \
  run_case "--no-wait で実行中" ci-pending 4 --no-wait 1
FAKE_RUNS="" run_case "--no-wait で run がまだ無い" ci-pending 4 --no-wait 1

set_main nl2sql/x.py
set_pr rag/a.py
FAKE_RUNS=";11 2026-10-01T01:00:00Z queued ;11 2026-10-01T01:00:00Z completed success" \
  run_case "run が現れて終わるまで待つ" ready-to-merge 0 1

FAKE_PR_INFO="MERGED codex/1-x abc123" run_case "OPEN でない PR" not-open 2 1
run_case "PR 番号が無い" "" 2

if [ "${failures}" -gt 0 ]; then
  echo "pr-merge-check: ${failures} 件失敗"
  exit 1
fi
echo "pr-merge-check の判定: pass"
