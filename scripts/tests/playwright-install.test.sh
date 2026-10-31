#!/usr/bin/env bash
# .github/actions/playwright-browsers/install.sh のテスト（#3489）。Suite / Scripts が実行する。
# npx（Playwright）・sudo・apt-get・dpkg-query・dpkg を偽物に差し替え、apt を呼ぶ範囲・retry・時間の上限を確かめる。
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
script="${root}/.github/actions/playwright-browsers/install.sh"
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
failures=0

mkdir -p "${work}/bin"
# Playwright 1.64 の install-deps と同じく `sudo -- sh -c "apt-get update&& apt-get install …"` を呼ぶ。
cat > "${work}/bin/npx" <<'EOF'
#!/usr/bin/env bash
case " $* " in
  *" install-deps "*)
    echo "Installing dependencies..."
    [ "${FAKE_NPX_NO_SUDO:-0}" = "1" ] && exit 0
    exec sudo -- sh -c "apt-get update&& apt-get install -y --no-install-recommends ${FAKE_PW_PACKAGES}" ;;
  *" install "*)
    echo "browser $*" >> "${FAKE_LOG}"
    sleep "${FAKE_BROWSER_SLEEP:-0}" ;;
esac
EOF
cat > "${work}/bin/sudo" <<'EOF'
#!/usr/bin/env bash
[ "${1:-}" = "--" ] && shift
exec "$@"
EOF
cat > "${work}/bin/apt-get" <<'EOF'
#!/usr/bin/env bash
echo "apt-get $*" >> "${FAKE_LOG}"
if [ "$1" = "update" ] && [ -n "${FAKE_UPDATE_FAILURES:-}" ]; then
  n="$(cat "${FAKE_UPDATE_FAILURES}")"
  if [ "${n}" -gt 0 ]; then
    echo $((n - 1)) > "${FAKE_UPDATE_FAILURES}"
    exit 100
  fi
fi
sleep "${FAKE_APT_SLEEP:-0}"
EOF
# FAKE_INSTALLED の行は `<package> <version>`。
cat > "${work}/bin/dpkg-query" <<'EOF'
#!/usr/bin/env bash
format="${2#-f=}"
line="$(grep -E "^$3 " "${FAKE_INSTALLED}" || true)"
[ -n "${line}" ] || exit 1
case "${format}" in
  *Status-Abbrev*) printf 'ii ' ;;
  *Version*) printf '%s' "${line#* }" ;;
esac
EOF
cat > "${work}/bin/dpkg" <<'EOF'
#!/usr/bin/env bash
echo "dpkg $*" >> "${FAKE_LOG}"
EOF
chmod +x "${work}/bin/"*

reset() {
  rm -rf "${work}/state"
  mkdir -p "${work}/state/archives" "${work}/state/cache"
  : > "${work}/state/log"
  : > "${work}/state/installed"
}

run() {
  local expected="$1" name="$2"
  shift 2
  local actual=0
  env PATH="${work}/bin:${PATH}" FAKE_LOG="${work}/state/log" FAKE_INSTALLED="${work}/state/installed" \
    FAKE_PW_PACKAGES="libfoo fonts-bar" PW_APT_CACHE_DIR="${work}/state/cache" \
    PW_APT_ARCHIVES="${work}/state/archives" PW_APT_CONF="${work}/state/apt.conf" PW_INSTALL_BACKOFF=0 \
    "$@" bash "${script}" "${subcommand}" > "${work}/state/out" 2>&1 || actual=$?
  if [ "${actual}" -eq "${expected}" ]; then
    echo "ok - ${name}"
  else
    echo "not ok - ${name}（終了 ${actual}、期待 ${expected}）"
    cat "${work}/state/out"
    failures=$((failures + 1))
  fi
}

assert() {
  local name="$1"
  shift
  if "$@"; then
    echo "ok - ${name}"
  else
    echo "not ok - ${name}"
    echo "--- out"; cat "${work}/state/out"; echo "--- log"; cat "${work}/state/log"
    failures=$((failures + 1))
  fi
}

subcommand=packages
reset
run 0 "packages は Playwright が apt-get install に渡す package を出す"
assert "package の一覧" test "$(cat "${work}/state/out")" = $'libfoo\nfonts-bar'

subcommand=os-deps
reset
printf 'libfoo 1.0\nfonts-bar 1.0\n' > "${work}/state/installed"
run 0 "すべて入っていれば成功する"
assert "すべて入っていれば apt を呼ばない" test ! -s "${work}/state/log"

reset
printf 'libfoo 1.0\n' > "${work}/state/installed"
touch "${work}/state/cache/fonts-bar_1.0_all.deb"
run 0 "入っていない package だけを入れる"
assert "update・取得・install の順に、入っていない package だけを渡す" test "$(cat "${work}/state/log")" = "$(
  printf '%s\n' "apt-get update" \
    "apt-get install -y --no-install-recommends --download-only fonts-bar" \
    "apt-get install -y --no-install-recommends fonts-bar"
)"
assert "apt に retry と timeout を設定する" grep -q 'Acquire::Retries "3";' "${work}/state/apt.conf"
assert "cache の .deb を apt の置き場に戻す" test -f "${work}/state/archives/fonts-bar_1.0_all.deb"

reset
echo 1 > "${work}/state/update-failures"
run 0 "1 回目の apt-get update の失敗はやり直して成功する" FAKE_UPDATE_FAILURES="${work}/state/update-failures"
assert "apt-get update を 2 回呼ぶ" test "$(grep -c '^apt-get update$' "${work}/state/log")" -eq 2

reset
echo 9 > "${work}/state/update-failures"
run 1 "apt-get update が続けて失敗したら失敗する" FAKE_UPDATE_FAILURES="${work}/state/update-failures"
assert "試行は PW_INSTALL_ATTEMPTS 回まで" test "$(grep -c '^apt-get update$' "${work}/state/log")" -eq 3
assert "失敗の理由を ::error:: に出す" grep -q '^::error title=Playwright のインストール::apt-get update が終わらなかった（終了コード 100）' "${work}/state/out"

reset
run 1 "取得が遅ければ試行ごとの上限で止める" FAKE_APT_SLEEP=5 PW_INSTALL_ATTEMPT_TIMEOUT=1 PW_INSTALL_ATTEMPTS=2
assert "時間切れを理由に出す" grep -q '1 秒で時間切れ' "${work}/state/out"

reset
start="${SECONDS}"
run 1 "全体の予算を超えたら次の試行をしない" FAKE_APT_SLEEP=5 PW_INSTALL_ATTEMPT_TIMEOUT=2 PW_INSTALL_ATTEMPTS=5 \
  PW_INSTALL_BUDGET=6
assert "予算を使い切ったことを理由に出す" grep -q '全体の予算 6 秒を使い切った' "${work}/state/out"
assert "予算の近くで止まる" test $((SECONDS - start)) -lt 10

reset
run 1 "Playwright が sudo を呼ばなければ失敗する" FAKE_NPX_NO_SUDO=1
assert "理由を出す" grep -q 'install-deps が sudo を呼ばなかった' "${work}/state/out"

subcommand=browsers
reset
run 0 "browsers は headless shell だけを取得する"
assert "install --only-shell chromium" grep -q '^browser --no-install playwright install --only-shell chromium$' "${work}/state/log"

reset
run 1 "browser の取得が遅ければ止める" FAKE_BROWSER_SLEEP=5 PW_INSTALL_ATTEMPT_TIMEOUT=1 PW_INSTALL_ATTEMPTS=2
assert "2 回試す" test "$(grep -c '^browser ' "${work}/state/log")" -eq 2

subcommand=collect-debs
reset
printf 'libfoo 2:1.0\nfonts-bar 1.1\n' > "${work}/state/installed"
touch "${work}/state/archives/libfoo_2%3a1.0_amd64.deb" "${work}/state/archives/fonts-bar_1.0_all.deb"
run 0 "collect-debs は成功する" GITHUB_OUTPUT="${work}/state/github-output"
assert "入っている版の .deb だけを写す" test "$(ls "${work}/state/cache")" = "libfoo_2%3a1.0_amd64.deb"
assert "写した数を出力する" grep -qx 'count=1' "${work}/state/github-output"

[ "${failures}" -eq 0 ] || { echo "${failures} 件失敗"; exit 1; }
echo "すべて成功"
