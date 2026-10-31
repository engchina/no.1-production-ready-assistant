#!/usr/bin/env bash
set -euo pipefail

# 引数で RAG / Agent の start-frontend.sh も同じ契約で検証できる（既定は NL2SQL）。
TEST_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
START_FRONTEND_SCRIPT="$(cd "$(dirname "${1:-${TEST_SCRIPT_DIR}/../start-frontend.sh}")" && pwd)/$(basename "${1:-start-frontend.sh}")"
TEST_TMP_DIR="$(mktemp -d)"
trap 'rm -rf -- "${TEST_TMP_DIR}"' EXIT

APP_DIR="${TEST_TMP_DIR}/nl2sql"
PLATFORM_DIR="${TEST_TMP_DIR}/platform"
UI_DIR="${PLATFORM_DIR}/packages/ui"
MOCK_BIN_DIR="${TEST_TMP_DIR}/bin"
NPM_CALL_LOG="${TEST_TMP_DIR}/npm-calls.log"

mkdir -p \
  "${APP_DIR}/scripts" \
  "${APP_DIR}/frontend/node_modules/@production-ready/ui" \
  "${UI_DIR}" \
  "${MOCK_BIN_DIR}"
cp "${START_FRONTEND_SCRIPT}" "${APP_DIR}/scripts/start-frontend.sh"
cp "${TEST_SCRIPT_DIR}/fixtures/npm" "${MOCK_BIN_DIR}/npm"
chmod +x "${MOCK_BIN_DIR}/npm"

: > "${PLATFORM_DIR}/package.json"
: > "${PLATFORM_DIR}/package-lock.json"
: > "${UI_DIR}/package.json"
: > "${APP_DIR}/frontend/node_modules/@production-ready/ui/package.json"

PATH="${MOCK_BIN_DIR}:${PATH}" \
NPM_CALL_LOG="${NPM_CALL_LOG}" \
SHARED_PLATFORM_DIR="${PLATFORM_DIR}" \
SHARED_UI_DIR="${UI_DIR}" \
PORT=3997 \
  "${APP_DIR}/scripts/start-frontend.sh"

EXPECTED_CALLS="${PLATFORM_DIR}|ci
${PLATFORM_DIR}|run build
${APP_DIR}/frontend|run dev -- --host 0.0.0.0 --port 3997"
ACTUAL_CALLS="$(<"${NPM_CALL_LOG}")"

if [ "${ACTUAL_CALLS}" != "${EXPECTED_CALLS}" ]; then
  echo "npm 呼び出し順が期待値と一致しません。" >&2
  echo "expected:" >&2
  echo "${EXPECTED_CALLS}" >&2
  echo "actual:" >&2
  echo "${ACTUAL_CALLS}" >&2
  exit 1
fi

for artifact in index.js index.d.ts tokens.css; do
  if [ ! -f "${UI_DIR}/dist/${artifact}" ]; then
    echo "共有 UI の成果物が生成されていません: ${artifact}" >&2
    exit 1
  fi
done

# package-lock.json が前回インストール時より新しい場合は npm install で依存を更新する。
: > "${NPM_CALL_LOG}"
touch -d "2026-01-01 00:00:00" "${APP_DIR}/frontend/node_modules/.package-lock.json"
: > "${APP_DIR}/frontend/package.json"
: > "${APP_DIR}/frontend/package-lock.json"

PATH="${MOCK_BIN_DIR}:${PATH}" \
NPM_CALL_LOG="${NPM_CALL_LOG}" \
SHARED_PLATFORM_DIR="${PLATFORM_DIR}" \
SHARED_UI_DIR="${UI_DIR}" \
PORT=3997 \
  "${APP_DIR}/scripts/start-frontend.sh"

if ! grep -qx "${APP_DIR}/frontend|install" "${NPM_CALL_LOG}"; then
  echo "package-lock.json 更新後に npm install が呼ばれていません。" >&2
  cat "${NPM_CALL_LOG}" >&2
  exit 1
fi

# インストール済み（hidden lockfile が lock より新しい）なら npm install を呼ばない。
: > "${NPM_CALL_LOG}"
touch "${APP_DIR}/frontend/node_modules/.package-lock.json"
PATH="${MOCK_BIN_DIR}:${PATH}" \
NPM_CALL_LOG="${NPM_CALL_LOG}" \
SHARED_PLATFORM_DIR="${PLATFORM_DIR}" \
SHARED_UI_DIR="${UI_DIR}" \
PORT=3997 \
  "${APP_DIR}/scripts/start-frontend.sh"

if grep -qx "${APP_DIR}/frontend|install" "${NPM_CALL_LOG}"; then
  echo "依存が最新なのに npm install が呼ばれました。" >&2
  cat "${NPM_CALL_LOG}" >&2
  exit 1
fi

echo "start-frontend preflight test: ok"
