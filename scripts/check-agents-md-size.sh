#!/usr/bin/env bash
# AGENTS.md の大きさを確かめる（#2276）。Suite / Scripts が実行する。
#
# Codex は repo の直下から起動したディレクトリまでの AGENTS.md を連結し、合計が project_doc_max_bytes
# （既定 32 KiB）に達すると以降を読まない（黙って切り捨てる）。Claude Code は直下の AGENTS.md を毎回読む。
# そのため次を超えたら失敗にする（単位は UTF-8 のバイト数）。
#   - 直下の AGENTS.md: 20 KiB（20,480 バイト）
#   - 直下から各ディレクトリの AGENTS.md までの合計（例: AGENTS.md + rag/AGENTS.md）: 32 KiB（32,768 バイト）
# 超えたら、詳細を docs/agents/（製品は <製品>/docs/agents/）の文書へ移し、AGENTS.md には規則の要約と案内だけを残す。
#
# 使い方: scripts/check-agents-md-size.sh [<repo の直下>]（省略時はこのスクリプトの 1 つ上）
set -euo pipefail

root="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
root_limit=20480
total_limit=32768
failures=0

size_of() {
  wc -c < "$1" | tr -d ' '
}

if [ ! -f "${root}/AGENTS.md" ]; then
  echo "ERROR: ${root}/AGENTS.md がありません" >&2
  exit 2
fi

root_size="$(size_of "${root}/AGENTS.md")"
if [ "${root_size}" -gt "${root_limit}" ]; then
  echo "NG: AGENTS.md は ${root_size} バイト（上限 ${root_limit}）。詳細を docs/agents/ へ移す"
  failures=$((failures + 1))
else
  echo "ok: AGENTS.md ${root_size} バイト（上限 ${root_limit}）"
fi

# 直下より下の AGENTS.md ごとに、直下からそのディレクトリまでの AGENTS.md の合計を数える。
# 依存・作業用のディレクトリ（node_modules・.venv・.git・.claude・tmp）は見ない。
while IFS= read -r file; do
  rel="${file#"${root}"/}"
  dir="$(dirname "${rel}")"
  total="${root_size}"
  chain="AGENTS.md"
  path=""
  IFS='/' read -r -a parts <<< "${dir}"
  for part in "${parts[@]}"; do
    path="${path:+${path}/}${part}"
    if [ -f "${root}/${path}/AGENTS.md" ]; then
      total=$((total + $(size_of "${root}/${path}/AGENTS.md")))
      chain="${chain} + ${path}/AGENTS.md"
    fi
  done
  if [ "${total}" -gt "${total_limit}" ]; then
    echo "NG: ${chain} は合計 ${total} バイト（上限 ${total_limit}）。製品の詳細を ${dir}/docs/agents/ へ移す"
    failures=$((failures + 1))
  else
    echo "ok: ${chain} 合計 ${total} バイト（上限 ${total_limit}）"
  fi
# -mindepth は使わない（その深さより浅い所で -prune も効かなくなる）。直下の AGENTS.md は -path で除く。
done < <(find "${root}" \
  \( -name node_modules -o -name .venv -o -name .git -o -name .claude -o -path "${root}/tmp" \) -prune \
  -o -type f -name AGENTS.md ! -path "${root}/AGENTS.md" -print | LC_ALL=C sort)

if [ "${failures}" -gt 0 ]; then
  exit 1
fi
