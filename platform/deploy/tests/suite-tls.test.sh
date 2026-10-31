#!/usr/bin/env bash
# platform/deploy/suite-tls.sh の証明書（自作の Root CA と IP のサーバー証明書。#1316）を openssl で検証する。
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
task_dir="$(mktemp -d)"
trap 'rm -rf -- "${task_dir}"' EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

assert_contains() {
  local haystack="$1" needle="$2" message="$3"
  case "${haystack}" in
    *"${needle}"*) ;;
    *) fail "${message}: ${needle} が無い: ${haystack}" ;;
  esac
}

# shellcheck source=../suite-tls.sh
source "${repo}/platform/deploy/suite-tls.sh"
openssl_bin="${SUITE_TLS_OPENSSL}"
dir="${task_dir}/ssl"
export SUITE_TLS_PROPS_DIR="${task_dir}/props"
mkdir -p "${SUITE_TLS_PROPS_DIR}"

# 1. 公開 IP と private IP の証明書を作る。
suite_tls_ensure "${dir}" 203.0.113.10 10.0.1.23 false 2>/dev/null

ca_subject="$("${openssl_bin}" x509 -in "${dir}/ca.crt" -noout -subject -nameopt RFC2253)"
[ "${ca_subject}" = "subject=CN=Production Ready Root CA,OU=Production Ready Suite,O=Oracle,L=Minato-ku,ST=Tokyo,C=JP" ] \
  || fail "CA の subject が違う: ${ca_subject}"
ca_issuer="$("${openssl_bin}" x509 -in "${dir}/ca.crt" -noout -issuer -nameopt RFC2253)"
[ "${ca_issuer}" = "issuer=CN=Production Ready Root CA,OU=Production Ready Suite,O=Oracle,L=Minato-ku,ST=Tokyo,C=JP" ] \
  || fail "CA は自己署名にする: ${ca_issuer}"
ca_text="$("${openssl_bin}" x509 -in "${dir}/ca.crt" -noout -text)"
assert_contains "${ca_text}" "Public-Key: (3072 bit)" "CA の鍵は RSA 3072"
assert_contains "${ca_text}" "sha256WithRSAEncryption" "CA の署名は SHA-256"
assert_contains "${ca_text}" "CA:TRUE, pathlen:0" "CA の basicConstraints"
assert_contains "${ca_text}" "Certificate Sign, CRL Sign" "CA の keyUsage"
case "${ca_text}" in *"No.1"*) fail "subject に No.1 を入れない" ;; esac

server_subject="$("${openssl_bin}" x509 -in "${dir}/server.crt" -noout -subject -nameopt RFC2253)"
[ "${server_subject}" = "subject=CN=203.0.113.10,OU=Production Ready Suite,O=Oracle,L=Minato-ku,ST=Tokyo,C=JP" ] \
  || fail "サーバーの subject が違う: ${server_subject}"
server_text="$("${openssl_bin}" x509 -in "${dir}/server.crt" -noout -text)"
assert_contains "${server_text}" "Public-Key: (3072 bit)" "サーバーの鍵は RSA 3072"
assert_contains "${server_text}" "IP Address:10.0.1.23, IP Address:203.0.113.10" "SAN は公開 IP と private IP"
assert_contains "${server_text}" "CA:FALSE" "サーバーの basicConstraints"
assert_contains "${server_text}" "Digital Signature, Key Encipherment" "サーバーの keyUsage"
assert_contains "${server_text}" "TLS Web Server Authentication" "サーバーの extendedKeyUsage"
"${openssl_bin}" verify -CAfile "${dir}/ca.crt" "${dir}/server.crt" >/dev/null || fail "CA で検証できない"

# 有効期間: CA 3650 日・サーバー 397 日。
python3 - "${openssl_bin}" "${dir}" <<'PY'
import datetime, subprocess, sys
openssl, d = sys.argv[1:]
def days(path):
    out = subprocess.check_output([openssl, "x509", "-in", path, "-noout", "-startdate", "-enddate"], text=True)
    values = dict(line.split("=", 1) for line in out.strip().splitlines())
    fmt = "%b %d %H:%M:%S %Y %Z"
    start = datetime.datetime.strptime(" ".join(values["notBefore"].split()), fmt)
    end = datetime.datetime.strptime(" ".join(values["notAfter"].split()), fmt)
    return (end - start).days
assert days(f"{d}/ca.crt") == 3650, days(f"{d}/ca.crt")
assert days(f"{d}/server.crt") == 397, days(f"{d}/server.crt")
PY

# 秘密鍵は 0600、証明書は 0644（Nginx の worker が CA の証明書を配る）。
[ "$(stat -c '%a' "${dir}/ca.key")" = "600" ] || fail "CA の秘密鍵は 0600"
[ "$(stat -c '%a' "${dir}/server.key")" = "600" ] || fail "サーバーの秘密鍵は 0600"
[ "$(stat -c '%a' "${dir}/ca.crt")" = "644" ] || fail "CA の証明書は 0644"
[ "$(stat -c '%a' "${dir}/server.crt")" = "644" ] || fail "サーバー証明書は 0644"

# 2. 同じ IP で再実行しても、CA もサーバー証明書も作り直さない。
ca_before="$(sha256sum "${dir}/ca.crt")"
server_before="$(sha256sum "${dir}/server.crt")"
suite_tls_ensure "${dir}" 203.0.113.10 10.0.1.23 false 2>/dev/null
[ "${ca_before}" = "$(sha256sum "${dir}/ca.crt")" ] || fail "CA を作り直した"
[ "${server_before}" = "$(sha256sum "${dir}/server.crt")" ] || fail "IP が同じなのにサーバー証明書を作り直した"

# 3. IP が変わったらサーバー証明書だけ作り直す（CA は保つ）。
suite_tls_ensure "${dir}" 203.0.113.99 10.0.1.23 false 2>/dev/null
[ "${ca_before}" = "$(sha256sum "${dir}/ca.crt")" ] || fail "IP の変更で CA を作り直した"
[ "${server_before}" != "$(sha256sum "${dir}/server.crt")" ] || fail "IP が変わったのにサーバー証明書を作り直さない"
assert_contains "$(suite_tls_cert_ips "${dir}/server.crt")" "203.0.113.99" "新しい公開 IP が SAN に入る"

# 4. 公開 IP が分からないときは private IP だけ（CN も private IP）。
dir2="${task_dir}/ssl-private"
suite_tls_ensure "${dir2}" "" 10.0.1.23 false 2>/dev/null
[ "$("${openssl_bin}" x509 -in "${dir2}/server.crt" -noout -subject -nameopt RFC2253)" \
  = "subject=CN=10.0.1.23,OU=Production Ready Suite,O=Oracle,L=Minato-ku,ST=Tokyo,C=JP" ] || fail "private IP の CN"
[ "$(suite_tls_cert_ips "${dir2}/server.crt")" = "10.0.1.23" ] || fail "private IP だけの SAN"

# 5. 公開 IP の検出: 明示 > props の public_ip.txt > （公開 IP がある Compute だけ）外部への問い合わせ。
printf 'false\n' > "${SUITE_TLS_PROPS_DIR}/assign_public_ip.txt"
if SUITE_TLS_PUBLIC_IP_URLS="http://127.0.0.1:9/never" suite_tls_detect_public_ip >/dev/null; then
  fail "公開 IP が無い Compute で外部に問い合わせた"
fi
[ "$(SUITE_TLS_PUBLIC_IP=198.51.100.7 suite_tls_detect_public_ip)" = "198.51.100.7" ] || fail "明示した公開 IP"
printf '198.51.100.8\n' > "${SUITE_TLS_PROPS_DIR}/public_ip.txt"
[ "$(suite_tls_detect_public_ip)" = "198.51.100.8" ] || fail "props の公開 IP"
# 既定の問い合わせ先は既存の製品（No.1-RAG / No.1-SQL-Assist）と同じ akamai を先に、checkip.amazonaws.com を 1 つだけ。
[ "${SUITE_TLS_PUBLIC_IP_URLS}" = "http://whatismyip.akamai.com/ https://checkip.amazonaws.com" ] \
  || fail "公開 IP の問い合わせ先: ${SUITE_TLS_PUBLIC_IP_URLS}"
# 公開 IP がある Compute では、問い合わせの結果が IPv4 でなければ次へ進み、どれも取れなければ失敗（private IP だけにする）。
rm -f "${SUITE_TLS_PROPS_DIR}/public_ip.txt"
printf 'true\n' > "${SUITE_TLS_PROPS_DIR}/assign_public_ip.txt"
fake_bin="${task_dir}/fake-bin"
mkdir -p "${fake_bin}"
cat > "${fake_bin}/curl" <<'CURL'
#!/usr/bin/env bash
for arg in "$@"; do
  case "${arg}" in
    http://whatismyip.akamai.com/) printf '<html>blocked</html>'; exit 0 ;;
    https://checkip.amazonaws.com) printf '198.51.100.9\n'; exit 0 ;;
  esac
done
exit 7
CURL
chmod +x "${fake_bin}/curl"
[ "$(PATH="${fake_bin}:${PATH}" suite_tls_detect_public_ip)" = "198.51.100.9" ] || fail "akamai の後に checkip.amazonaws.com を試さない"
if PATH="${fake_bin}:${PATH}" SUITE_TLS_PUBLIC_IP_URLS="http://whatismyip.akamai.com/" suite_tls_detect_public_ip >/dev/null 2>&1; then
  fail "IPv4 でない応答を公開 IP にした"
fi
if SUITE_TLS_PUBLIC_IP="not-an-ip" suite_tls_detect_public_ip >/dev/null 2>&1; then
  fail "IPv4 でない指定を受け入れた"
fi
suite_tls_is_ipv4 10.0.0.256 && fail "256 は IPv4 の値ではない"

echo "suite-tls: pass"
