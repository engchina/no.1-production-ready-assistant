#!/usr/bin/env bash
# 配備の通し（Deploy Nightly。.github/scripts/deploy-run-through.sh）の verify が、各製品の system schema の確認の CLI を
# 「DB に繋がらないことだけで失敗した」と見なす判定（schema_summary_is_db_unreachable。#1768）を固定する。
#
# 判定の相手は、本物の pr_system_settings.system_schema.deployment_check_error_summary が返す要約（3 製品の CLI の
# --check / check が標準出力の最後の行に出すもの）。#1744 で接続の失敗の要約の文が変わり、通しの判定が合わなく
# なった（#1768）ため、両方を 1 つのテストで確かめる（要約の形を変えたら、ここで通しの判定とのずれが分かる）。
#
# Python: DEPLOY_RUN_THROUGH_TEST_PYTHON（pr_system_settings を import できる Python。例: 製品の backend/.venv/bin/python）。
# 無ければ uv で platform/packages/system_settings_backend の lock どおりの一時の venv を作る（dev の依存は入れない）。
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
task_dir="$(mktemp -d)"
trap 'rm -rf -- "${task_dir}"' EXIT
failures=0

# source しても何も実行しない（関数の定義だけ）。
# shellcheck source=../../../.github/scripts/deploy-run-through.sh
source "${repo}/.github/scripts/deploy-run-through.sh"

expect() {
  local name="$1" expected="$2" status="$3" summary="$4" actual=true
  schema_summary_is_db_unreachable "${status}" "${summary}" || actual=false
  if [ "${actual}" = "${expected}" ]; then
    echo "ok - ${name}"
  else
    echo "not ok - ${name}（判定 ${actual}、期待 ${expected}。終了 ${status}・要約 ${summary:-なし}）"
    failures=$((failures + 1))
  fi
}

# 1. 要約の形だけの判定。
unreachable='状態を読めません（SCHEMA_STATUS_UNAVAILABLE: DPY-6005: ADB が停止しているか、この接続元から DB に届きません。OCI コンソールか…）'
expect "接続の失敗（DPY-6005）は DB に繋がらない" true 1 "${unreachable}"
expect "接続の失敗（ORA のコード）も DB に繋がらない" true 1 \
  '状態を読めません（SCHEMA_STATUS_UNAVAILABLE: ORA-12541: DB の listener に届きません。…）'
expect "終了 0 は DB に繋がらないではない" false 0 "${unreachable}"
expect "時間切れ（124）は DB に繋がらないではない" false 124 "${unreachable}"
expect "要約が無い（module が無い・CLI が何も出さない）" false 1 ""
expect "venv が無い" false 1 'backend の venv がありません（/u01/aipoc/no.1-production-ready-suite/rag/backend/.venv）'
expect "確認の方法が無い" false 1 '確認の方法がありません（rag）'
expect "import の失敗（印に続けて例外の型）" false 1 \
  '状態を読めません（SCHEMA_STATUS_UNAVAILABLE: ModuleNotFoundError: No module named '"'"'oracledb'"'"'）'
expect "設定の不備（印に続けて例外の型）" false 1 '状態を読めません（SCHEMA_STATUS_UNAVAILABLE: ValueError: password=***）'
expect "ORA の失敗（権限の不足。印が無い）" false 1 '状態を読めません（ORA-01031）'
expect "#1744 の印の無い要約（#1768 の前の形）" false 1 \
  '状態を読めません（DPY-6005: ADB が停止しているか、この接続元から DB に届きません。…）'
expect "理由の無い要約" false 1 '状態を読めません（SCHEMA_STATUS_UNAVAILABLE: DPY-6005: ）'
expect "ready の要約" false 1 'status=ready'

# 2. 本物の deployment_check_error_summary の要約を、通しの判定に通す（secret を伏せることも確かめる）。
python_bin="${DEPLOY_RUN_THROUGH_TEST_PYTHON:-}"
if [ -z "${python_bin}" ]; then
  if ! command -v uv >/dev/null 2>&1; then
    echo "not ok - uv か DEPLOY_RUN_THROUGH_TEST_PYTHON が要ります（pr_system_settings を import する）"
    exit 1
  fi
  UV_PROJECT_ENVIRONMENT="${task_dir}/venv" uv sync -q --frozen --no-dev \
    --project "${repo}/platform/packages/system_settings_backend" >/dev/null
  python_bin="${task_dir}/venv/bin/python"
fi
if ! "${python_bin}" -I -c "import pr_system_settings.system_schema" >/dev/null 2>&1; then
  echo "not ok - ${python_bin} で pr_system_settings.system_schema を import できません"
  exit 1
fi

summary_of() {
  "${python_bin}" -I -c '
import sys

from pr_system_settings.system_schema import deployment_check_error_summary


class DatabaseError(Exception):
    """python-oracledb の例外の代わり（message だけを使う）。"""


kind, message = sys.argv[1], sys.argv[2]
exc = {"database": DatabaseError, "value": ValueError, "import": ModuleNotFoundError}[kind](message)
print(deployment_check_error_summary(exc))
' "$1" "$2" 2>/dev/null
}

secret="Pa55-word-x"
check_real() {
  local name="$1" expected="$2" kind="$3" message="$4" summary
  summary="$(summary_of "${kind}" "${message}" || true)"
  if printf '%s\n' "${summary}" | grep -qF "${secret}"; then
    echo "not ok - ${name}: 要約に secret が出た（${summary}）"
    failures=$((failures + 1))
    return
  fi
  expect "本物の要約: ${name}" "${expected}" 1 "${summary}"
}

check_real "DB が無い（DPY-6005）" true database \
  "DPY-6005: cannot connect to database (CONNECTION_ID=abc). password=${secret}
[Errno 111] Connection refused"
check_real "listener が無い（ORA-12541）" true database \
  "DPY-6005: cannot connect to database. ORA-12541: Cannot connect. No listener at host 127.0.0.1 port 1"
check_real "サービスが受け付けない（DPY-6001）" true database \
  "DPY-6001: Service \"db_high\" is not registered with the listener"
check_real "権限の不足（ORA-01031）" false database "ORA-01031: insufficient privileges; ${secret}"
check_real "設定の不備（ValueError）" false value "password=${secret}"
check_real "import の失敗" false import "No module named 'oracledb'"

if [ "${failures}" -ne 0 ]; then
  echo "FAIL: ${failures} 件"
  exit 1
fi
echo "PASS"
