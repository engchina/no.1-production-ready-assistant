#!/usr/bin/env bash
set -euo pipefail

TEST_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PULL_SCRIPT="${TEST_SCRIPT_DIR}/../git-pull.sh"
TEST_TMP_DIR="$(mktemp -d)"
trap 'rm -rf -- "${TEST_TMP_DIR}"' EXIT
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
export GIT_AUTHOR_NAME='Pull Test' GIT_AUTHOR_EMAIL='pull-test@example.invalid'
export GIT_COMMITTER_NAME="${GIT_AUTHOR_NAME}" GIT_COMMITTER_EMAIL="${GIT_AUTHOR_EMAIL}"

fail_test() { printf 'git-pull test failed: %s\n' "$*" >&2; exit 1; }

# suite repository（nl2sql/ と platform/ を含む monorepo）を1つ用意する。
setup_case() {
  CASE_DIR="${TEST_TMP_DIR}/$1"
  SUITE_DIR="${CASE_DIR}/no.1-production-ready-suite"
  SEED_DIR="${CASE_DIR}/suite-seed"
  mkdir -p "${CASE_DIR}"
  git init --bare --quiet --initial-branch=main "${CASE_DIR}/suite.git"
  git init --quiet --initial-branch=main "${SEED_DIR}"
  mkdir -p "${SEED_DIR}/nl2sql/scripts" "${SEED_DIR}/platform"
  printf 'initial\n' > "${SEED_DIR}/nl2sql/version.txt"
  printf 'initial\n' > "${SEED_DIR}/platform/version.txt"
  cp "${PULL_SCRIPT}" "${SEED_DIR}/nl2sql/scripts/git-pull.sh"
  git -C "${SEED_DIR}" add .
  git -C "${SEED_DIR}" commit --quiet -m initial
  git -C "${SEED_DIR}" remote add origin "${CASE_DIR}/suite.git"
  git -C "${SEED_DIR}" push --quiet -u origin main
  git clone --quiet "${CASE_DIR}/suite.git" "${SUITE_DIR}"
  INITIAL_HEAD="$(git -C "${SUITE_DIR}" rev-parse HEAD)"
}

advance_remote() {
  printf 'remote update\n' >> "${SEED_DIR}/platform/version.txt"
  printf 'remote update\n' >> "${SEED_DIR}/nl2sql/version.txt"
  # 実行中の pull スクリプト自体が更新される場合も正常終了する。
  printf '\n# updated script\n' >> "${SEED_DIR}/nl2sql/scripts/git-pull.sh"
  git -C "${SEED_DIR}" add .
  git -C "${SEED_DIR}" commit --quiet -m remote-update
  git -C "${SEED_DIR}" push --quiet origin main
}

run_pull() (
  cd "${TEST_TMP_DIR}"
  # 呼出元 directory に依存せず、スクリプト位置から suite root を解決する。
  unset SUITE_REPO_DIR
  bash "${SUITE_DIR}/nl2sql/scripts/git-pull.sh"
)

expect_failure() {
  if run_pull >"${CASE_DIR}/output" 2>&1 && touch "${CASE_DIR}/deployed"; then
    fail_test 'failed pull allowed deployment'
  fi
  test ! -e "${CASE_DIR}/deployed"
  if grep -Fq 'suite repository の更新が完了' "${CASE_DIR}/output"; then
    fail_test 'failed pull reported success'
  fi
}

assert_unchanged() {
  [ "$(git -C "${SUITE_DIR}" rev-parse HEAD)" = "${INITIAL_HEAD}" ]
}

setup_case success
advance_remote
mkdir -p "${SUITE_DIR}/nl2sql/backend"
printf 'keep lock\n' > "${SUITE_DIR}/nl2sql/backend/..env.lock"
run_pull >"${CASE_DIR}/output" 2>&1
[ "$(git -C "${SUITE_DIR}" rev-parse HEAD)" = "$(git -C "${SEED_DIR}" rev-parse HEAD)" ]
grep -Fxq 'keep lock' "${SUITE_DIR}/nl2sql/backend/..env.lock"
grep -Fq 'suite: origin/main' "${CASE_DIR}/output"
grep -Fq 'suite repository の更新が完了' "${CASE_DIR}/output"
run_pull >"${CASE_DIR}/noop-output" 2>&1
grep -Fq 'suite repository の更新が完了' "${CASE_DIR}/noop-output"

for scenario in dirty staged branch detached missing missing-origin; do
  setup_case "${scenario}"
  advance_remote
  case "${scenario}" in
    dirty) printf 'local edit\n' >> "${SUITE_DIR}/nl2sql/version.txt" ;;
    staged)
      printf 'local edit\n' >> "${SUITE_DIR}/nl2sql/version.txt"
      git -C "${SUITE_DIR}" add nl2sql/version.txt
      ;;
    branch) git -C "${SUITE_DIR}" switch --quiet -c local-work ;;
    detached) git -C "${SUITE_DIR}" checkout --quiet --detach ;;
    missing) mv "${SUITE_DIR}/.git" "${CASE_DIR}/saved-git" ;;
    missing-origin) git -C "${SUITE_DIR}" remote remove origin ;;
  esac
  expect_failure
  case "${scenario}" in
    dirty|staged) grep -Fq 'local edit' "${SUITE_DIR}/nl2sql/version.txt" ;;
    missing) ;;
    *) assert_unchanged ;;
  esac
done

# nl2sql/ と platform/ がそろっていない repository（旧構成など）では更新しない。
setup_case missing-platform
advance_remote
git -C "${SUITE_DIR}" rm --quiet -r platform
git -C "${SUITE_DIR}" commit --quiet -m drop-platform
local_head="$(git -C "${SUITE_DIR}" rev-parse HEAD)"
expect_failure
[ "$(git -C "${SUITE_DIR}" rev-parse HEAD)" = "${local_head}" ]
grep -Fq 'platform/ がありません' "${CASE_DIR}/output"

setup_case fetch-failure
advance_remote
git -C "${SUITE_DIR}" remote set-url origin "${CASE_DIR}/unavailable.git"
expect_failure
assert_unchanged
grep -Fq 'suite: pull に失敗' "${CASE_DIR}/output"

for scenario in diverged ahead; do
  setup_case "${scenario}"
  printf 'local commit\n' > "${SUITE_DIR}/platform/local.txt"
  git -C "${SUITE_DIR}" add platform/local.txt
  git -C "${SUITE_DIR}" commit --quiet -m local-commit
  local_head="$(git -C "${SUITE_DIR}" rev-parse HEAD)"
  if [ "${scenario}" = diverged ]; then advance_remote; fi
  expect_failure
  [ "$(git -C "${SUITE_DIR}" rev-parse HEAD)" = "${local_head}" ]
done

setup_case untracked-conflict
printf 'remote file\n' > "${SEED_DIR}/platform/conflict.txt"
advance_remote
printf 'local file\n' > "${SUITE_DIR}/platform/conflict.txt"
expect_failure
assert_unchanged
grep -Fxq 'local file' "${SUITE_DIR}/platform/conflict.txt"

bash "${PULL_SCRIPT}" --help >"${TEST_TMP_DIR}/help"
grep -Fq 'Usage:' "${TEST_TMP_DIR}/help"
if bash "${PULL_SCRIPT}" --unknown >"${TEST_TMP_DIR}/invalid" 2>&1; then
  fail_test 'unknown argument succeeded'
fi
printf 'git-pull: success, no-op and 11 failure scenarios verified.\n'
