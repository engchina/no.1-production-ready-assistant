#!/usr/bin/env bash
# pre-commit の ruff（#339 / #345）。commit する Python ファイルだけを、その backend の設定と ruff の版で検査する。
#
# pre-commit から、対象の backend 配下の変更ファイルが引数で渡される。backend ごとに
# `uv run --frozen ruff format --check` と `uv run --frozen ruff check`（CI の `uv run ruff format --check .` /
# `uv run ruff check .` と同じ版・同じ pyproject.toml の設定）を実行する。ファイルは書き換えない。
# 整形の違反は `uv run ruff format <file>`（その backend の下で）で直す。
set -euo pipefail

BACKENDS=(
  rag/backend
  nl2sql/backend
  agent/backend
  platform/packages/backend_core
  platform/packages/system_settings_backend
)

if ! command -v uv >/dev/null 2>&1; then
  echo "[pre-commit ruff] uv が見つかりません。uv をインストールしてください（https://docs.astral.sh/uv/）。" >&2
  exit 1
fi

status=0
for backend in "${BACKENDS[@]}"; do
  files=()
  for path in "$@"; do
    case "${path}" in
      "${backend}"/*) files+=("${path#"${backend}"/}") ;;
    esac
  done
  [ "${#files[@]}" -gt 0 ] || continue
  # --force-exclude: pyproject.toml の exclude（.venv 等）を、明示したファイルにも適用する。
  # 整形と lint の両方の違反を一度に出すため、片方が失敗しても続ける。
  (cd "${backend}" && uv run --frozen ruff format --check --force-exclude "${files[@]}") || status=1
  (cd "${backend}" && uv run --frozen ruff check --force-exclude "${files[@]}") || status=1
done
exit "${status}"
