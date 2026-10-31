#!/usr/bin/env bash
# バックエンドとフロントエンドを同時に起動する。
# どちらかが終了するか Ctrl+C を押すと、両方のプロセスをまとめて停止する。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

BACKEND_HOST="${BACKEND_HOST:-0.0.0.0}"
BACKEND_READY_HOST="${BACKEND_READY_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8010}"
FRONTEND_PORT="${FRONTEND_PORT:-3001}"
BACKEND_URL="${BACKEND_URL:-http://${BACKEND_READY_HOST}:${BACKEND_PORT}}"
BACKEND_READY_URL="${BACKEND_READY_URL:-${BACKEND_URL}/api/health}"
# 負荷の高い端末でも backend の import と起動を待ち切れるよう、既定の上限を長めに取る（#1935）。
BACKEND_READY_TIMEOUT_SECONDS="${BACKEND_READY_TIMEOUT_SECONDS:-300}"
# 待つ間に経過を出す間隔（秒）。
BACKEND_READY_PROGRESS_SECONDS="${BACKEND_READY_PROGRESS_SECONDS:-15}"

pids=()
_cleaning=0

cleanup() {
  local exit_code="${1:-0}"
  # 再入防止: 最初の Ctrl+C で全トラップを解除し、二度目以降の INT は無視する。
  # (これをしないと cleanup 中の Ctrl+C で再入し「停止しています...」を繰り返す)
  if [ "${_cleaning}" -eq 1 ]; then
    return
  fi
  _cleaning=1
  trap - INT TERM EXIT

  echo ""
  echo "[start-all] 停止しています..."

  # まずプロセスグループへ SIGTERM(子の uvicorn --reload / vite も巻き取る)。
  for pid in "${pids[@]}"; do
    kill -- "-${pid}" 2>/dev/null || kill "${pid}" 2>/dev/null || true
  done

  # 最大 ~3 秒だけ graceful 終了を待ち、残っていれば SIGKILL で即時停止する。
  local waited=0
  while [ "${waited}" -lt 15 ]; do
    local alive=0
    for pid in "${pids[@]}"; do
      if kill -0 "${pid}" 2>/dev/null; then
        alive=1
      fi
    done
    if [ "${alive}" -eq 0 ]; then
      break
    fi
    sleep 0.2
    waited=$((waited + 1))
  done
  for pid in "${pids[@]}"; do
    if kill -0 "${pid}" 2>/dev/null; then
      kill -9 -- "-${pid}" 2>/dev/null || kill -9 "${pid}" 2>/dev/null || true
    fi
  done

  echo "[start-all] 停止しました。"
  exit "${exit_code}"
}
# INT/TERM は即時停止、EXIT は正常終了時の後始末(いずれも cleanup を 1 度だけ実行)。
trap 'cleanup 130' INT
trap 'cleanup 143' TERM
trap 'cleanup $?' EXIT

# 各スクリプトを独立したプロセスグループで起動する
set -m

wait_for_backend_ready() {
  local backend_pid="$1"
  local started="${SECONDS}"
  local deadline=$((started + BACKEND_READY_TIMEOUT_SECONDS))
  local next_progress=$((started + BACKEND_READY_PROGRESS_SECONDS))

  if ! command -v curl >/dev/null 2>&1; then
    echo "[start-all] curl が見つからないため backend readiness を確認できません。" >&2
    return 1
  fi

  echo "[start-all] backend readiness を待機します（上限 ${BACKEND_READY_TIMEOUT_SECONDS}s）: ${BACKEND_READY_URL}"
  while true; do
    # 起動したローカルの backend だけを確かめる。環境変数のプロキシ（NO_PROXY に 127.0.0.1 が無い
    # 端末）を経由すると 502 になり、ready を確認できないため、プロキシを使わない（#781）。
    if curl -fsS --noproxy "*" --max-time 2 "${BACKEND_READY_URL}" >/dev/null 2>&1; then
      echo "[start-all] backend ready を確認しました（$((SECONDS - started))s）。"
      return 0
    fi

    # 失敗にするのは backend のプロセスが終了したときと上限を過ぎたときだけ。負荷の高い端末では
    # import と起動に数分かかることがあるため、生きている間は待ち続ける（#1935）。
    if ! kill -0 "${backend_pid}" 2>/dev/null; then
      echo "[start-all] backend プロセスが ready 前に終了しました。" >&2
      return 1
    fi

    if [ "${SECONDS}" -ge "${deadline}" ]; then
      echo "[start-all] backend readiness の待機がタイムアウトしました (${BACKEND_READY_TIMEOUT_SECONDS}s)。BACKEND_READY_TIMEOUT_SECONDS で延ばせます。" >&2
      return 1
    fi

    if [ "${SECONDS}" -ge "${next_progress}" ]; then
      echo "[start-all] backend は起動中です（$((SECONDS - started))s 経過・上限 ${BACKEND_READY_TIMEOUT_SECONDS}s）..."
      next_progress=$((SECONDS + BACKEND_READY_PROGRESS_SECONDS))
    fi

    sleep 1
  done
}

echo "[start-all] バックエンドの依存を準備します（初回のダウンロード・構築には時間がかかります）..."
"${SCRIPT_DIR}/start-backend.sh" --prepare-only &
pids+=("$!")
if wait "${pids[0]}"; then
  pids=()
else
  status=$?
  echo "[start-all] バックエンドの依存準備に失敗しました。" >&2
  exit "${status}"
fi

echo "[start-all] バックエンドを起動します..."
HOST="${BACKEND_HOST}" PORT="${BACKEND_PORT}" "${SCRIPT_DIR}/start-backend.sh" --no-sync &
pids+=("$!")
backend_pid="$!"

wait_for_backend_ready "${backend_pid}"

echo "[start-all] フロントエンドを起動します..."
PORT="${FRONTEND_PORT}" BACKEND_URL="${BACKEND_URL}" "${SCRIPT_DIR}/start-frontend.sh" &
pids+=("$!")

echo "[start-all] backend: ${BACKEND_URL}/api/health  frontend: http://localhost:${FRONTEND_PORT}"
echo "[start-all] 停止するには Ctrl+C を押してください。"

# いずれかのプロセスが終了したら cleanup が走る
wait -n
