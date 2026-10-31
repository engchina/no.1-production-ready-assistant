#!/usr/bin/env bash
# Compute の OS（Oracle Linux 9。#1611）で、配備のスクリプトが使う package・command・Nginx の設定を確かめる。
# CI の `Suite / Oracle Linux 9`（container: oraclelinux:9、root）が実行する。Oracle Linux 以外では何もしない。
#   1. 配備のスクリプト（cloud-init・suite-init.sh・3 製品の init_script.sh）が dnf で入れる package の名前が、
#      Oracle Linux 9 の repository（BaseOS・AppStream。EPEL・CodeReady Builder は使わない）と module
#      （nginx:1.24・nodejs:24）で解決できる（`dnf install --assumeno` で依存まで解決し、入れない）
#   2. 配備のスクリプトが前提にする command が image にある・bash -n
#   3. suite-init.sh の configure_tls / configure_nginx が書く本番と同じ配置（/etc/nginx/nginx.conf・conf.d）を、
#      Oracle Linux の Nginx が読み（package の既定の server と衝突しない）、HTTPS で /agent/ へ転送する
# 実機（OCI）の SELinux・firewalld・systemd の確認は手動（PR の手順）で行う。
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
task_dir="$(mktemp -d)"
cleanup() {
  if [ -f /run/nginx.pid ]; then
    nginx -s quit 2>/dev/null || true
  fi
  rm -rf -- "${task_dir}"
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

if ! grep -q '^ID="ol"' /etc/os-release 2>/dev/null; then
  echo "oracle-linux: Oracle Linux ではないため飛ばす（CI は oraclelinux:9 の container で実行する）"
  exit 0
fi
grep -q '^VERSION_ID="9\.' /etc/os-release || fail "Oracle Linux 9 ではない: $(grep '^VERSION_ID=' /etc/os-release)"
[ "$(id -u)" -eq 0 ] || fail "root で実行する（dnf と /etc/nginx を使う）"

# ---------------------------------------------------------------- 1. package の名前

# cloud-init の packages（terraform/stack/cloud_init/bootstrap.template.yaml）。
mapfile -t cloud_init_packages < <(
  awk '/^packages:$/ {in_list = 1; next} in_list && /^  - / {print $2; next} in_list {exit}' \
    "${repo}/terraform/stack/cloud_init/bootstrap.template.yaml"
)
[ "${#cloud_init_packages[@]}" -gt 0 ] || fail "cloud-init の packages が読めない"

# 各スクリプトの package の一覧（test mode で source して変数・関数から取る。実行はしない）。
mapfile -t suite_packages < <(
  SUITE_INIT_TEST_MODE=true bash -c 'source "$1/platform/deploy/suite-init.sh"; printf "%s\n" "${SUITE_OS_PACKAGES[@]}"; echo "MODULE nginx:${NGINX_MODULE_STREAM}"' _ "${repo}"
)
mapfile -t nl2sql_packages < <(
  NL2SQL_INIT_TEST_MODE=true bash -c 'source "$1/nl2sql/init_script.sh"; printf "%s\n" "${SYSTEM_PACKAGES[@]}"' _ "${repo}"
)
mapfile -t agent_packages < <(
  AGENT_INIT_TEST_MODE=true bash -c 'source "$1/agent/init_script.sh"; printf "%s\n" "${SYSTEM_PACKAGES[@]}"; echo "MODULE nodejs:${NODE_MAJOR}"' _ "${repo}"
)
# RAG は配備してよい前処理 / parser をすべて選んだときの一覧。
mapfile -t rag_packages < <(
  RAG_INIT_TEST_MODE=true bash -c 'source "$1/rag/init_script.sh"; RAG_SERVICES=("${ALLOWED_RAG_SERVICES[@]}"); system_packages' _ "${repo}"
)

modules=()
packages=(nodejs npm)
for name in "${cloud_init_packages[@]}" "${suite_packages[@]}" "${nl2sql_packages[@]}" "${agent_packages[@]}" "${rag_packages[@]}"; do
  case "${name}" in
    "MODULE "*) modules+=("${name#MODULE }") ;;
    "") ;;
    *) packages+=("${name}") ;;
  esac
done
for module in "${modules[@]}"; do
  case "${module}" in
    nginx:*) ;;
    nodejs:*) ;;
    *) fail "未知の module: ${module}" ;;
  esac
done
# 配備のスクリプトが使う module（nginx:1.24・3 製品の nodejs:24。#1591）が repository にあること。
for module in "${modules[@]}" nodejs:24; do
  dnf -q module info "${module}" >/dev/null 2>&1 || fail "module が無い: ${module}"
done
# 3 製品が同じ Compute で使う組み合わせ（nginx:1.24 と RAG / NL2SQL の nodejs:24）で、すべての package を解決する。
dnf -y -q module reset nodejs nginx >/dev/null
dnf -y -q module enable "$(printf '%s\n' "${modules[@]}" | grep '^nginx:')" nodejs:24 >/dev/null
mapfile -t packages < <(printf '%s\n' "${packages[@]}" | sort -u)
echo "oracle-linux: 解決する package: ${packages[*]}"
set +e
resolve_output="$(dnf install --assumeno --setopt=install_weak_deps=False "${packages[@]}" 2>&1)"
set -e
if ! printf '%s\n' "${resolve_output}" | grep -q 'Operation aborted'; then
  printf '%s\n' "${resolve_output}" >&2
  fail "dnf が package を解決できない（名前の誤り・repository に無い・依存の衝突）"
fi
if printf '%s\n' "${resolve_output}" | grep -Eq 'No match for argument|Unable to find a match'; then
  printf '%s\n' "${resolve_output}" >&2
  fail "repository に無い package がある"
fi
# Ubuntu の package 名が残っていないこと。
for name in "${packages[@]}"; do
  case "${name}" in
    build-essential | netfilter-persistent | gnupg | fonts-noto-cjk | tesseract-ocr* | libreoffice-core-nogui)
      fail "Ubuntu の package 名が残っている: ${name}"
      ;;
  esac
done
echo "oracle-linux: package の名前: pass"

# ---------------------------------------------------------------- 2. command と構文

# image にあり、スクリプトが入れずに使う command。
for command_name in bash curl install mktemp runuser sha256sum stat timeout useradd; do
  command -v "${command_name}" >/dev/null 2>&1 || fail "image に ${command_name} が無い"
done
for script in "${repo}"/platform/deploy/*.sh "${repo}"/rag/init_script.sh "${repo}"/rag/scripts/rag-systemd.sh \
  "${repo}"/rag/scripts/rag-services.sh "${repo}"/nl2sql/init_script.sh "${repo}"/nl2sql/scripts/update-after-pull.sh \
  "${repo}"/agent/init_script.sh; do
  bash -n "${script}" || fail "bash -n: ${script}"
done
echo "oracle-linux: command と構文: pass"

# ---------------------------------------------------------------- 3. 本番と同じ配置の Nginx

dnf -y -q install nginx openssl python3 >/dev/null
nginx -v 2>&1 | grep -q 'nginx/1\.24\.' || fail "Nginx が module の 1.24 ではない: $(nginx -v 2>&1)"
# package の nginx.conf には port 80 の既定の server がある（suite はそれを外した nginx.conf に置き換える）。
grep -q 'listen       80;' /etc/nginx/nginx.conf || fail "package の nginx.conf の前提が変わった（platform/templates/nginx/nginx.conf を見直す）"
id nginx >/dev/null 2>&1 || fail "nginx ユーザーが無い"

# Nginx の worker（nginx ユーザー）が dist と CA 証明書を辿れるようにする（mktemp の directory は 0700）。
chmod 0755 "${task_dir}"
app_root="${task_dir}/app"
mkdir -p "${app_root}/props" "${app_root}/no.1-production-ready-suite"
ln -s "${repo}/platform" "${app_root}/no.1-production-ready-suite/platform"
for product in rag nl2sql agent; do
  mkdir -p "${app_root}/no.1-production-ready-suite/${product}/frontend/dist"
  printf '<!doctype html><title>%s</title>\n' "${product}" > "${app_root}/no.1-production-ready-suite/${product}/frontend/dist/index.html"
done
chmod -R a+rX "${app_root}"
(
  export SUITE_INIT_TEST_MODE=true APP_ROOT="${app_root}" SUITE_SSL_DIR="${app_root}/ssl"
  export SUITE_TLS_PRIVATE_IP=127.0.0.1 SUITE_TLS_PUBLIC_IP=""
  # shellcheck source=../suite-init.sh
  source "${repo}/platform/deploy/suite-init.sh"
  systemctl() { printf 'systemctl %s\n' "$*" >> "${task_dir}/systemctl.log"; }
  # shellcheck disable=SC2034 # source した suite-init.sh の configure_nginx が読む。
  SUITE_PRODUCTS=(rag nl2sql agent)
  configure_tls >/dev/null 2>&1
  configure_nginx >/dev/null
)
[ -f /etc/nginx/conf.d/production-ready-suite.conf ] || fail "site を /etc/nginx/conf.d に置かない"
if grep -q 'server_name  _;' /etc/nginx/nginx.conf; then fail "nginx.conf に package の既定の server が残っている"; fi
nginx_test="$(nginx -t 2>&1)" || { printf '%s\n' "${nginx_test}" >&2; fail "nginx -t が失敗する"; }
if printf '%s\n' "${nginx_test}" | grep -q 'conflicting server name'; then
  printf '%s\n' "${nginx_test}" >&2
  fail "既定の server と衝突している"
fi
nginx
python3 - "${app_root}/ssl/ca.crt" <<'PY'
import http.client, ssl, sys
# HTTPS が on（既定）なら 80 は listen しない（HTTP から HTTPS へ転送しない。#1638）。
plain = http.client.HTTPConnection("127.0.0.1", 80, timeout=10)
try:
    plain.request("GET", "/rag/")
    raise AssertionError(f"80 が listen されている: {plain.getresponse().status}")
except ConnectionRefusedError:
    pass
context = ssl.create_default_context(cafile=sys.argv[1])
conn = http.client.HTTPSConnection("127.0.0.1", 443, timeout=10, context=context)
conn.request("GET", "/")
response = conn.getresponse()
response.read()
assert (response.status, response.getheader("Location")) == (302, "/agent/"), (response.status, response.getheaders())
for product in ("rag", "nl2sql", "agent"):
    conn.request("GET", f"/{product}/")
    response = conn.getresponse()
    body = response.read().decode()
    assert response.status == 200 and f"<title>{product}</title>" in body, (product, response.status, body)
conn.request("GET", "/platform/ca.crt")
response = conn.getresponse()
assert response.status == 200 and response.read() == open(sys.argv[1], "rb").read(), response.status
print("oracle-linux: Nginx（nginx ユーザー・/etc/nginx/conf.d・HTTPS）: pass")
PY
nginx -s quit
echo "oracle-linux: pass"
