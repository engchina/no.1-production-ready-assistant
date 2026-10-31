#!/usr/bin/env bash
# git worktree（または新しい clone）の開発の準備を、触る製品の分だけで済ませる（#339）。
#
# 使い方（リポジトリ直下で）:
#   scripts/setup-worktree.sh rag              # platform の共有 UI の build + rag/frontend の npm ci
#   scripts/setup-worktree.sh nl2sql agent     # 複数の製品
#   BACKEND=1 scripts/setup-worktree.sh rag    # backend の uv sync --locked --dev も行う
#   scripts/setup-worktree.sh platform         # platform の npm ci と build だけ
# 製品を省略すると rag / nl2sql / agent のすべてを準備する。
#
# npm は --prefer-offline --no-audit --no-fund で、手元の npm cache を優先する。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="${BACKEND:-0}"
NPM_CI=(npm ci --prefer-offline --no-audit --no-fund)

products=("$@")
if [ "${#products[@]}" -eq 0 ]; then
  products=(rag nl2sql agent)
fi

for product in "${products[@]}"; do
  case "${product}" in
    rag | nl2sql | agent | platform) ;;
    *)
      echo "[setup-worktree] 不明な製品です: ${product}（rag / nl2sql / agent / platform）" >&2
      exit 2
      ;;
  esac
done

log() {
  echo "[setup-worktree] $*"
}

# 製品の frontend は file:../../platform/packages/{ui,system-settings} の dist を使うため、先に build する。
log "platform: npm ci と共有 UI の build"
(cd "${ROOT_DIR}/platform" && "${NPM_CI[@]}" && npm run build)

for product in "${products[@]}"; do
  [ "${product}" = "platform" ] && continue
  log "${product}/frontend: npm ci"
  (cd "${ROOT_DIR}/${product}/frontend" && "${NPM_CI[@]}")
  if [ "${BACKEND}" = "1" ]; then
    log "${product}/backend: uv sync --locked --dev"
    (cd "${ROOT_DIR}/${product}/backend" && uv sync --locked --dev)
  fi
done

log "完了: ${products[*]}"
for product in "${products[@]}"; do
  if [ "${product}" = "agent" ]; then
    # 主の dev server と同じ DB の Run の checkpoint に 2 つ目のプロセスが書く（#1916。agent/AGENTS.md「開発・検証」）。
    log "注意: worktree で Agent の backend を主の .env（同じ DB）のまま起動しない。確かめるのは pytest と主の dev server（8020）で。"
  fi
done
