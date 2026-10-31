#!/usr/bin/env bash
# バックエンド(FastAPI / Uvicorn)を開発モードで起動する。
# - 依存は uv sync で解決する。共有 platform パッケージ(pr_backend_core 等)も
#   uv のワークスペース解決でまとめて取り込まれる。
# - .env が無い場合でも、デフォルト設定(local 環境)で起動できる。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BACKEND_DIR="${ROOT_DIR}/backend"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8020}"

# 一括起動は依存準備を先に完了し、HTTP の待機時間に含めない。
mode="${1:-start}"
case "${mode}" in
  start|--prepare-only|--no-sync) ;;
  *) echo "使い方: $0 [--prepare-only|--no-sync]" >&2; exit 2 ;;
esac

if ! command -v uv >/dev/null 2>&1; then
  echo "[backend] uv が見つかりません。https://docs.astral.sh/uv/ を参照してインストールしてください。" >&2
  exit 1
fi

# 同じポートの listener だけを停止する（接続側の client / readiness は停止しない）。
kill_port() {
  local port="$1"
  local pids
  pids="$(lsof -nP -t -a -iTCP:"${port}" -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "${pids}" ]; then
    echo "[backend] ポート ${port} を使用中のプロセスを停止します (PID: ${pids//$'\n'/ })..."
    kill ${pids} 2>/dev/null || true
    sleep 1
    pids="$(lsof -nP -t -a -iTCP:"${port}" -sTCP:LISTEN 2>/dev/null || true)"
    if [ -n "${pids}" ]; then
      echo "[backend] 強制停止します (kill -9)..."
      kill -9 ${pids} 2>/dev/null || true
    fi
  fi
}

cd "${BACKEND_DIR}"

if [ "${mode}" != "--no-sync" ]; then
  echo "[backend] 依存を解決します (uv sync)..."
  uv sync
fi
if [ "${mode}" = "--prepare-only" ]; then
  exit 0
fi

kill_port "${PORT}"

# 環境のプロキシ（OCI のための HTTP_PROXY 等）を、同じマシンのサービス・MCP への呼び出しに使わない
# （#852）。backend のコードも内部の宛先ではプロキシを外す（pr_backend_core.internal_http）。
# ここは多重の防御として、既存の値を残したまま NO_PROXY / no_proxy に loopback を足す。
add_loopback_no_proxy() {
  local result="$1" host
  for host in localhost 127.0.0.1 ::1; do
    case ",${result}," in
      *",${host},"*) ;;
      *) result="${result:+${result},}${host}" ;;
    esac
  done
  printf '%s' "${result}"
}
NO_PROXY="$(add_loopback_no_proxy "${NO_PROXY:-${no_proxy:-}}")"
no_proxy="$(add_loopback_no_proxy "${no_proxy:-${NO_PROXY}}")"
export NO_PROXY no_proxy

echo "[backend] http://${HOST}:${PORT} で起動します（稼働確認: /api/health）..."
exec uv run --no-sync uvicorn app.main:app --reload --host "${HOST}" --port "${PORT}"
