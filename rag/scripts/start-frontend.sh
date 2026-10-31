#!/usr/bin/env bash
# フロントエンド(Vite + React)を開発モードで起動する。
# - 共有 UI パッケージを検証・build してから Vite を起動する。
# - node_modules / 共有 UI のリンクが無い場合、または package.json / package-lock.json が
#   前回インストール時（node_modules/.package-lock.json）より新しい場合は npm install を実行する。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
FRONTEND_DIR="${ROOT_DIR}/frontend"
SHARED_PLATFORM_DIR="${SHARED_PLATFORM_DIR:-${ROOT_DIR}/../platform}"
SHARED_UI_DIR="${SHARED_UI_DIR:-${SHARED_PLATFORM_DIR}/packages/ui}"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-3000}"
# Vite は BACKEND_URL を明示したときだけ /api を proxy する（未指定なら 404 の hermetic モード）。
# このスクリプトはローカル backend と組み合わせる起動経路なので、接続先を明示して渡す。
export BACKEND_URL="${BACKEND_URL:-http://127.0.0.1:8000}"

# OS の CA（企業 CA を含む）も信頼する。明示された追加 CA は保持し、TLS 検証は無効化しない。
# CA bundle の場所は Debian / Ubuntu と RHEL / Oracle Linux で違うため、読める最初のものを使う。
if [ -z "${NODE_EXTRA_CA_CERTS:-}" ]; then
  for ca_bundle in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt; do
    if [ -r "${ca_bundle}" ]; then
      export NODE_EXTRA_CA_CERTS="${ca_bundle}"
      break
    fi
  done
fi

if ! command -v npm >/dev/null 2>&1; then
  echo "[frontend] npm が見つかりません。Node.js をインストールしてください。" >&2
  exit 1
fi

# 共有 UI（packages/ui と packages/system-settings）の成果物が、ソース・設定より新しいか。
# 変更が無いときは build を省く（起動中の別の製品の Vite が読む dist を消さない。#816）。
shared_ui_up_to_date() {
  [ "${FORCE_SHARED_UI_BUILD:-0}" != "1" ] || return 1
  local artifact
  for artifact in index.js index.d.ts tokens.css; do
    [ -f "${SHARED_UI_DIR}/dist/${artifact}" ] || return 1
  done
  local settings_dir="${SHARED_PLATFORM_DIR}/packages/system-settings"
  if [ -f "${settings_dir}/package.json" ] && [ ! -f "${settings_dir}/dist/index.js" ]; then
    return 1
  fi
  # vite が最初に書く index.js を基準にする（その後に変えたソースがあれば build し直す）。
  local stamp="${SHARED_UI_DIR}/dist/index.js"
  local newer
  newer="$(find "${SHARED_UI_DIR}" "${settings_dir}" "${SHARED_PLATFORM_DIR}/package.json" \
    "${SHARED_PLATFORM_DIR}/package-lock.json" \
    \( -name node_modules -o -name dist \) -prune -o -type f -newer "${stamp}" -print -quit 2>/dev/null || true)"
  [ -z "${newer}" ]
}

# 3 製品の start-all.sh を同時に起動すると、共有 UI の build（vite build は dist を空にしてから書く）が
# 重なり、成果物の確認が失敗する。build と確認を flock で排他にする（flock が無い環境は排他なし。#816）。
SHARED_UI_LOCK_FD=""
lock_shared_ui() {
  command -v flock >/dev/null 2>&1 || return 0
  local key
  key="$(printf '%s' "$(cd "${SHARED_PLATFORM_DIR}" && pwd)" | cksum | cut -d' ' -f1)"
  exec 9>"${TMPDIR:-/tmp}/production-ready-shared-ui-${key}.lock"
  SHARED_UI_LOCK_FD=9
  if ! flock -n 9; then
    echo "[frontend] 別の製品が共有 UI を build しています。終わるまで待ちます..."
    flock 9
  fi
}

unlock_shared_ui() {
  [ -n "${SHARED_UI_LOCK_FD}" ] || return 0
  flock -u 9 || true
  exec 9>&-
  SHARED_UI_LOCK_FD=""
}

prepare_shared_ui() {
  if [ ! -f "${SHARED_UI_DIR}/package.json" ]; then
    echo "[frontend] 共有 UI パッケージが見つかりません: ${SHARED_UI_DIR}" >&2
    echo "[frontend] monorepo(no.1-production-ready-assistant)の platform/ が rag/ と同じ階層にあることを確認してください。" >&2
    echo "[frontend] 別の場所に配置した場合は SHARED_PLATFORM_DIR または SHARED_UI_DIR を指定してください。" >&2
    return 1
  fi

  if [ ! -f "${SHARED_PLATFORM_DIR}/package.json" ]; then
    echo "[frontend] 共有 platform の package.json が見つかりません: ${SHARED_PLATFORM_DIR}" >&2
    return 1
  fi

  if [ ! -d "${SHARED_PLATFORM_DIR}/node_modules" ]; then
    if [ ! -f "${SHARED_PLATFORM_DIR}/package-lock.json" ]; then
      echo "[frontend] 共有 platform の package-lock.json が見つかりません: ${SHARED_PLATFORM_DIR}" >&2
      return 1
    fi
    echo "[frontend] 共有 UI の依存をインストールします (npm ci)..."
    (
      cd "${SHARED_PLATFORM_DIR}"
      npm ci
    )
  fi

  if shared_ui_up_to_date; then
    echo "[frontend] 共有 UI パッケージは最新です（build を省きます。常に build するには FORCE_SHARED_UI_BUILD=1）。"
  else
    echo "[frontend] 共有 UI パッケージをビルドします..."
    (
      cd "${SHARED_PLATFORM_DIR}"
      # 共有 UI と共有システム設定画面を依存順に build する（platform の npm run build）。
      npm run build
    )
  fi

  local artifact
  for artifact in index.js index.d.ts tokens.css; do
    if [ ! -f "${SHARED_UI_DIR}/dist/${artifact}" ]; then
      echo "[frontend] 共有 UI のビルド成果物が見つかりません: ${SHARED_UI_DIR}/dist/${artifact}" >&2
      return 1
    fi
  done
}

# 同じポートの listener だけを停止する（接続側の client / readiness は停止しない）。
kill_port() {
  local port="$1"
  local pids
  pids="$(lsof -nP -t -a -iTCP:"${port}" -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "${pids}" ]; then
    echo "[frontend] ポート ${port} を使用中のプロセスを停止します (PID: ${pids//$'\n'/ })..."
    kill ${pids} 2>/dev/null || true
    sleep 1
    pids="$(lsof -nP -t -a -iTCP:"${port}" -sTCP:LISTEN 2>/dev/null || true)"
    if [ -n "${pids}" ]; then
      echo "[frontend] 強制停止します (kill -9)..."
      kill -9 ${pids} 2>/dev/null || true
    fi
  fi
}

kill_port "${PORT}"

lock_shared_ui
prepare_shared_ui
unlock_shared_ui

cd "${FRONTEND_DIR}"

# git pull 等で依存が追加・更新されたのに node_modules が古いままだと、Vite が import を解決できない。
dependencies_changed() {
  local installed="node_modules/.package-lock.json"
  [ -f package-lock.json ] || return 1
  [ ! -f "${installed}" ] || [ package-lock.json -nt "${installed}" ] || [ package.json -nt "${installed}" ]
}

if [ ! -d node_modules ] || [ ! -e node_modules/@production-ready/ui/package.json ]; then
  echo "[frontend] 依存をインストールします (npm install)..."
  npm install
elif dependencies_changed; then
  echo "[frontend] package.json / package-lock.json が更新されているため依存を更新します (npm install)..."
  npm install
fi

echo "[frontend] http://localhost:${PORT} で起動します..."
exec npm run dev -- --host "${HOST}" --port "${PORT}"
