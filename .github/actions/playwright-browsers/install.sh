#!/usr/bin/env bash
# Playwright の chromium（headless shell）と OS の依存を、取得の遅延・停止に時間の上限を付けて入れる（#3489）。
# action.yml から、@playwright/test を npm ci 済みの frontend のディレクトリで呼ぶ。
#
#   install.sh browsers      browser だけを取得する（cache の miss のとき）。
#   install.sh os-deps       Playwright が求める OS の package のうち、入っていないものだけを apt で入れる。
#   install.sh packages      Playwright が求める OS の package の一覧を出す（確認用）。
#   install.sh collect-debs  apt が取得した .deb を cache の場所へ写す（main の保存の前）。
#
# `playwright install-deps` に任せない理由: apt-get を sudo の下で時間の上限なしに実行し、入っている package も
# upgrade するので、apt の mirror が遅いと job の時間切れまで止まる（#3474・#3484・#3486）。ここでは package の
# 一覧だけを Playwright から取り（版ごとの一覧を repo に書き写さない）、apt は `sudo timeout` で区切って呼ぶ。
#
# 環境変数（テストが差し替える。既定は GitHub の ubuntu の runner の値）:
#   PW_INSTALL_BUDGET           subcommand の全体の予算（秒。既定 300）。超えたら失敗する。
#   PW_INSTALL_ATTEMPTS         1 つの段の試行の回数（既定 3）
#   PW_INSTALL_ATTEMPT_TIMEOUT  1 回の試行の上限（秒。既定 120）
#   PW_INSTALL_BACKOFF          やり直しの前に待つ秒数の単位（n 回目の失敗の後は n 倍。既定 10）
#   PW_APT_CACHE_DIR            .deb の cache の場所（既定 ~/.cache/playwright-apt）
#   PW_APT_ARCHIVES             apt の .deb の置き場（既定 /var/cache/apt/archives）
#   PW_APT_CONF                 apt の設定を書くファイル（既定 /etc/apt/apt.conf.d/80playwright-ci）
#   PW_INSTALL_SUDO             root で実行するコマンド（既定 sudo）
set -euo pipefail

budget="${PW_INSTALL_BUDGET:-300}"
attempts="${PW_INSTALL_ATTEMPTS:-3}"
attempt_timeout="${PW_INSTALL_ATTEMPT_TIMEOUT:-120}"
backoff="${PW_INSTALL_BACKOFF:-10}"
cache_dir="${PW_APT_CACHE_DIR:-${HOME}/.cache/playwright-apt}"
archives="${PW_APT_ARCHIVES:-/var/cache/apt/archives}"
apt_conf="${PW_APT_CONF:-/etc/apt/apt.conf.d/80playwright-ci}"
read -r -a sudo_cmd <<< "${PW_INSTALL_SUDO:-sudo}"

deadline=$((SECONDS + budget))
retry_attempt=0
retry_reason=""
packages=()
missing=()

fail() {
  echo "::error title=Playwright のインストール::$*"
  exit 1
}

# retry <段の名前> <関数>: 関数を 1 回の上限（秒）を引数にして呼び、失敗したら backoff を置いてやり直す。
# subcommand の全体の予算を超える試行はしない。
retry() {
  local label="$1" fn="$2" status remaining limit
  retry_attempt=1
  while :; do
    remaining=$((deadline - SECONDS))
    if [ "${remaining}" -lt 5 ]; then
      retry_reason="全体の予算 ${budget} 秒を使い切った"
      return 1
    fi
    limit="${attempt_timeout}"
    [ "${remaining}" -lt "${limit}" ] && limit="${remaining}"
    echo "::group::${label}（${retry_attempt}/${attempts} 回目、上限 ${limit} 秒）"
    status=0
    "${fn}" "${limit}" || status=$?
    echo "::endgroup::"
    [ "${status}" -eq 0 ] && return 0
    if [ "${status}" -eq 124 ] || [ "${status}" -eq 137 ]; then
      retry_reason="${limit} 秒で時間切れ"
    else
      retry_reason="終了コード ${status}"
    fi
    echo "::warning title=Playwright のインストール::${label} が失敗した（${retry_attempt}/${attempts} 回目、${retry_reason}）"
    [ "${retry_attempt}" -ge "${attempts}" ] && return 1
    sleep $((backoff * retry_attempt))
    retry_attempt=$((retry_attempt + 1))
  done
}

fail_slow_network() {
  fail "$1 が終わらなかった（${retry_reason}）。runner の取得先（apt の mirror・Playwright の CDN）が遅いか応答していない。" \
    "コードの問題ではないので job を再実行する。続くなら #3489 を見直す。"
}

# Playwright が `install-deps` で apt-get に渡す package の一覧を読む。Playwright は `sudo -- sh -c "apt-get update&&
# apt-get install -y --no-install-recommends <package…>"` を実行するので、PATH の先頭に置いた sudo の shim でその
# 引数を受け取り、実行はしない。
read_playwright_packages() {
  local shim_dir capture cmd segment word
  local -a args words
  shim_dir="$(mktemp -d)"
  capture="${shim_dir}/args"
  cat > "${shim_dir}/sudo" <<'SHIM'
#!/usr/bin/env bash
printf '%s\0' "$@" > "${PW_SUDO_CAPTURE}"
SHIM
  chmod +x "${shim_dir}/sudo"
  if ! PATH="${shim_dir}:${PATH}" PW_SUDO_CAPTURE="${capture}" npx --no-install playwright install-deps chromium \
    > "${shim_dir}/out" 2>&1; then
    cat "${shim_dir}/out"
    fail "Playwright の install-deps の一覧を読めなかった（npx --no-install playwright install-deps chromium が失敗した）。"
  fi
  [ -s "${capture}" ] \
    || fail "Playwright の install-deps が sudo を呼ばなかった（root で実行しているか、Playwright の動きが変わった）。"
  mapfile -d '' args < "${capture}"
  cmd="${args[${#args[@]} - 1]}"
  rm -rf "${shim_dir}"
  packages=()
  while IFS= read -r segment; do
    read -r -a words <<< "${segment}"
    if [ "${#words[@]}" -lt 2 ] || [ "${words[0]}" != "apt-get" ] || [ "${words[1]}" != "install" ]; then
      continue
    fi
    for word in "${words[@]:2}"; do
      case "${word}" in -*) ;; *) packages+=("${word}") ;; esac
    done
  done <<< "${cmd//&&/$'\n'}"
  [ "${#packages[@]}" -gt 0 ] || fail "Playwright の install-deps のコマンドから package を読めなかった: ${cmd}"
}

find_missing_packages() {
  local package status
  missing=()
  for package in "${packages[@]}"; do
    # shellcheck disable=SC2016 # dpkg-query の書式（shell の展開ではない）
    status="$(dpkg-query -W -f='${db:Status-Abbrev}' "${package}" 2> /dev/null || true)"
    [ "${status}" = "ii " ] || missing+=("${package}")
  done
}

apt_as_root() {
  local limit="$1"
  shift
  "${sudo_cmd[@]}" env DEBIAN_FRONTEND=noninteractive timeout --kill-after=10 "${limit}" apt-get "$@"
}

apt_update() { apt_as_root "$1" update; }
apt_download() { apt_as_root "$1" install -y --no-install-recommends --download-only "${missing[@]}"; }
apt_install() {
  # 前の試行が dpkg の途中で止まっていたら、続きを片付けてから入れ直す。
  if [ "${retry_attempt}" -gt 1 ]; then
    "${sudo_cmd[@]}" dpkg --configure -a || true
  fi
  apt_as_root "$1" install -y --no-install-recommends "${missing[@]}"
}

configure_apt() {
  # Retries: 取得の失敗（接続の切断・5xx）をやり直す。Timeout: データが来ない時間の上限（遅いが途切れない取得は
  # 止めないので、試行ごとの `timeout` で区切る）。Keep-Downloaded-Packages: 取得した .deb を cache に写すために残す。
  "${sudo_cmd[@]}" tee "${apt_conf}" > /dev/null <<'CONF'
// .github/actions/playwright-browsers/install.sh（#3489）
Acquire::Retries "3";
Acquire::http::Timeout "30";
Acquire::https::Timeout "30";
APT::Keep-Downloaded-Packages "true";
CONF
}

seed_cached_debs() {
  local -a debs
  [ -d "${cache_dir}" ] || return 0
  mapfile -t debs < <(find "${cache_dir}" -maxdepth 1 -name '*.deb' -type f)
  [ "${#debs[@]}" -gt 0 ] || return 0
  "${sudo_cmd[@]}" cp "${debs[@]}" "${archives}/"
  echo "cache の .deb を ${#debs[@]} 個 ${archives} に戻した（版が合うものは mirror から取らない）。"
}

cmd_packages() {
  read_playwright_packages
  printf '%s\n' "${packages[@]}"
}

cmd_os_deps() {
  read_playwright_packages
  find_missing_packages
  if [ "${#missing[@]}" -eq 0 ]; then
    echo "Playwright の OS の依存（${#packages[@]} 個）はすべて入っている。apt は呼ばない。"
    return 0
  fi
  echo "入っていない OS の依存（${#missing[@]}/${#packages[@]} 個）: ${missing[*]}"
  configure_apt
  seed_cached_debs
  retry "apt-get update" apt_update || fail_slow_network "apt-get update"
  retry "OS の依存の取得（apt-get install --download-only）" apt_download || fail_slow_network "OS の依存の取得"
  retry "OS の依存のインストール（apt-get install）" apt_install || fail_slow_network "OS の依存のインストール"
}

browser_install() { timeout --kill-after=10 "$1" npx --no-install playwright install --only-shell chromium; }

cmd_browsers() {
  # headless の実行は chromium-headless-shell だけを使う（headed / channel 指定の spec は無い）。
  retry "chromium headless shell の取得" browser_install || fail_slow_network "chromium headless shell の取得"
}

# 入っている版と同じ .deb だけを写す（mirror で新しい版が出て使われなくなった .deb を cache に持ち越さない）。
# .deb の名前は `<package>_<version>_<arch>.deb`（version の `:` は `%3a`）。
cmd_collect_debs() {
  local deb name package version installed count=0
  rm -rf "${cache_dir}"
  mkdir -p "${cache_dir}"
  while IFS= read -r deb; do
    name="$(basename "${deb}" .deb)"
    package="${name%%_*}"
    version="${name#*_}"
    version="${version%_*}"
    version="${version//%3a/:}"
    # shellcheck disable=SC2016 # dpkg-query の書式（shell の展開ではない）
    installed="$(dpkg-query -W -f='${Version}' "${package}" 2> /dev/null || true)"
    if [ "${installed}" = "${version}" ]; then
      cp "${deb}" "${cache_dir}/"
      count=$((count + 1))
    fi
  done < <(find "${archives}" -maxdepth 1 -name '*.deb' -type f)
  echo "cache に写した .deb: ${count} 個（$(du -sh "${cache_dir}" | cut -f1)）"
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "count=${count}" >> "${GITHUB_OUTPUT}"
  fi
}

case "${1:-}" in
  browsers) cmd_browsers ;;
  os-deps) cmd_os_deps ;;
  packages) cmd_packages ;;
  collect-debs) cmd_collect_debs ;;
  *)
    echo "usage: $0 browsers|os-deps|packages|collect-debs" >&2
    exit 2
    ;;
esac
