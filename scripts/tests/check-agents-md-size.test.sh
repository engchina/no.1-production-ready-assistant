#!/usr/bin/env bash
# scripts/check-agents-md-size.sh のテスト（#2276）。Suite / Scripts が実行する。
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
check="${root}/scripts/check-agents-md-size.sh"
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
failures=0

# <file> に <bytes> バイトの内容を書く。
fill() {
  mkdir -p "$(dirname "$1")"
  head -c "$2" /dev/zero | tr '\0' 'a' > "$1"
}

expect() {
  local name="$1" expected="$2" repo="$3" pattern="${4:-}"
  local actual=0
  bash "${check}" "${repo}" > "${work}/out.txt" 2>&1 || actual=$?
  if [ "${actual}" -ne "${expected}" ]; then
    echo "not ok - ${name}（終了 ${actual}、期待 ${expected}）"
    cat "${work}/out.txt"
    failures=$((failures + 1))
  elif [ -n "${pattern}" ] && ! grep -q -- "${pattern}" "${work}/out.txt"; then
    echo "not ok - ${name}（出力に「${pattern}」が無い）"
    cat "${work}/out.txt"
    failures=$((failures + 1))
  else
    echo "ok - ${name}"
  fi
}

# 上限ちょうどは通る。
repo="${work}/at-limit"
fill "${repo}/AGENTS.md" 20480
fill "${repo}/rag/AGENTS.md" 12288
expect "root 20480 と合計 32768 は通る" 0 "${repo}" "合計 32768"

# root が上限を 1 バイト超える。
repo="${work}/root-over"
fill "${repo}/AGENTS.md" 20481
expect "root が 20480 を超えると失敗" 1 "${repo}" "NG: AGENTS.md は 20481"

# root と製品の合計が上限を超える。
repo="${work}/total-over"
fill "${repo}/AGENTS.md" 18000
fill "${repo}/nl2sql/AGENTS.md" 10000
fill "${repo}/agent/AGENTS.md" 14769
expect "root + 製品の合計が 32768 を超えると失敗" 1 "${repo}" "NG: AGENTS.md + agent/AGENTS.md"

# 深いディレクトリは、途中のすべての AGENTS.md を足す。
repo="${work}/nested"
fill "${repo}/AGENTS.md" 15000
fill "${repo}/rag/AGENTS.md" 10000
fill "${repo}/rag/frontend/AGENTS.md" 8000
expect "途中の AGENTS.md も合計に入る" 1 "${repo}" "rag/AGENTS.md + rag/frontend/AGENTS.md"

# node_modules・tmp の下は数えない。
repo="${work}/ignored"
fill "${repo}/AGENTS.md" 1000
fill "${repo}/node_modules/pkg/AGENTS.md" 40000
fill "${repo}/tmp/work/AGENTS.md" 40000
expect "node_modules・tmp の下は見ない" 0 "${repo}"

# root の AGENTS.md が無い。
repo="${work}/missing"
mkdir -p "${repo}"
expect "root の AGENTS.md が無いと失敗" 2 "${repo}"

# この repo そのもの。
expect "この repo の AGENTS.md が上限の中" 0 "${root}"

if [ "${failures}" -gt 0 ]; then
  echo "${failures} 件の失敗"
  exit 1
fi
