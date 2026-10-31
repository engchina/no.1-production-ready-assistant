#!/usr/bin/env bash
# .github/scripts/pytest-skip-gate.py のテスト（#1595）。Suite / Scripts が実行する。
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
gate="${root}/.github/scripts/pytest-skip-gate.py"
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
failures=0

check() {
  local name="$1" expected="$2"
  shift 2
  local actual=0
  python3 "${gate}" "$@" > "${work}/out.txt" 2>&1 || actual=$?
  if [ "${actual}" -eq "${expected}" ]; then
    echo "ok - ${name}"
  else
    echo "not ok - ${name}（終了 ${actual}、期待 ${expected}）"
    cat "${work}/out.txt"
    failures=$((failures + 1))
  fi
}

cat > "${work}/pass.xml" <<'XML'
<testsuites><testsuite name="pytest" tests="2">
  <testcase classname="tests.test_a" name="test_one"/>
  <testcase classname="tests.test_a" name="test_two"/>
</testsuite></testsuites>
XML
cat > "${work}/skip.xml" <<'XML'
<testsuites><testsuite name="pytest" tests="2">
  <testcase classname="tests.test_a" name="test_one"/>
  <testcase classname="tests.test_select_ai" name="test_live"><skipped message="isolated Select AI opt-in"/></testcase>
</testsuite></testsuites>
XML
cat > "${work}/empty.xml" <<'XML'
<testsuites><testsuite name="pytest" tests="0"></testsuite></testsuites>
XML

check "skip が無ければ成功" 0 "${work}/pass.xml"
check "許可していない skip は失敗" 1 "${work}/skip.xml"
check "許可した skip は成功" 0 "${work}/skip.xml" --allow "test_select_ai::test_live"
check "別のテストの許可では失敗" 1 "${work}/skip.xml" --allow "test_other"
check "0 件は失敗" 1 "${work}/empty.xml"

if grep -q "::error::許可していない skip: tests.test_select_ai::test_live（isolated Select AI opt-in）" \
  < <(python3 "${gate}" "${work}/skip.xml" 2>&1 || true); then
  echo "ok - 失敗の理由に test と skip の理由を出す"
else
  echo "not ok - 失敗の理由に test と skip の理由を出す"
  failures=$((failures + 1))
fi

[ "${failures}" -eq 0 ] || { echo "${failures} 件失敗"; exit 1; }
echo "すべて成功"
