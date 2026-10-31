#!/usr/bin/env bash
# Compute の OS（Oracle Linux 9。#1611）で、配備のスクリプトが使う package・command を確かめる。
# CI の `Suite / Oracle Linux 9`（container: oraclelinux:9、root）が実行する。Oracle Linux 以外では何もしない。
#   1. 配備のスクリプト（cloud-init・suite-init.sh・3 製品の init_script.sh）が dnf で入れる package の名前が、
#      Oracle Linux 9 の repository（BaseOS・AppStream。EPEL・CodeReady Builder は使わない）と module
#      （nodejs:24）で解決できる（`dnf install --assumeno` で依存まで解決し、入れない）
#   2. 配備のスクリプトが前提にする command が image にある・bash -n
# reverse proxy（Nginx）と TLS の証明書は配備しない（#19）。実機の SELinux・systemd の確認は手動（PR の手順）で行う。
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

if ! grep -q '^ID="ol"' /etc/os-release 2>/dev/null; then
  echo "oracle-linux: Oracle Linux ではないため飛ばす（CI は oraclelinux:9 の container で実行する）"
  exit 0
fi
grep -q '^VERSION_ID="9\.' /etc/os-release || fail "Oracle Linux 9 ではない: $(grep '^VERSION_ID=' /etc/os-release)"
[ "$(id -u)" -eq 0 ] || fail "root で実行する（dnf を使う）"

# ---------------------------------------------------------------- 1. package の名前

# cloud-init の packages（terraform/stack/cloud_init/bootstrap.template.yaml）。
mapfile -t cloud_init_packages < <(
  awk '/^packages:$/ {in_list = 1; next} in_list && /^  - / {print $2; next} in_list {exit}' \
    "${repo}/terraform/stack/cloud_init/bootstrap.template.yaml"
)
[ "${#cloud_init_packages[@]}" -gt 0 ] || fail "cloud-init の packages が読めない"

# 各スクリプトの package の一覧（test mode で source して変数・関数から取る。実行はしない）。
mapfile -t suite_packages < <(
  SUITE_INIT_TEST_MODE=true bash -c 'source "$1/platform/deploy/suite-init.sh"; printf "%s\n" "${SUITE_OS_PACKAGES[@]}"' _ "${repo}"
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
    nodejs:*) ;;
    *) fail "未知の module: ${module}" ;;
  esac
done
# 配備のスクリプトが使う module（3 製品の nodejs:24。#1591）が repository にあること。
for module in "${modules[@]}" nodejs:24; do
  dnf -q module info "${module}" >/dev/null 2>&1 || fail "module が無い: ${module}"
done
# 3 製品が同じ Compute で使う組み合わせ（nodejs:24）で、すべての package を解決する。
dnf -y -q module reset nodejs >/dev/null
dnf -y -q module enable nodejs:24 >/dev/null
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

echo "oracle-linux: pass"
