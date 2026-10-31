#!/usr/bin/env bash
# 1 台の Compute の HTTPS の証明書（#1316）。自作の Root CA と、IP アドレスのサーバー証明書を作る。
#
#   sudo bash platform/deploy/suite-tls.sh status
#   sudo bash platform/deploy/suite-tls.sh renew [--public-ip <IP>] [--private-ip <IP>]   # 作り直して Nginx を reload
#   sudo bash platform/deploy/suite-tls.sh renew-if-needed                                # 期限の 30 日前から作り直す（timer）
#
# 発行者・サーバーの subject・鍵長・有効期間は固定する（Terraform の変数にしない）。
#   CA:       C=JP / ST=Tokyo / L=Minato-ku / O=Oracle / OU=Production Ready Suite / CN=Production Ready Root CA
#   サーバー: C=JP / ST=Tokyo / L=Minato-ku / O=Oracle / OU=Production Ready Suite / CN=<公開 IP>
#             SAN = 公開 IP + private IP（公開 IP が分からないときは private IP だけ。CN も private IP）
#   RSA 3072・SHA-256・CA 3650 日・サーバー 397 日
# CA の秘密鍵は Compute の中だけに置く（root の 0600）。利用者に配るのは CA の証明書（/platform/ca.crt）だけ。
# CA は作り直さない（作り直すと、利用者が取り込んだ CA が使えなくなる）。サーバー証明書は、無い・期限が 30 日
# 以内・IP が変わった・今の CA で検証できないときに作り直す。
set -euo pipefail

SUITE_TLS_KEY_BITS=3072
SUITE_TLS_CA_DAYS=3650
SUITE_TLS_SERVER_DAYS=397
SUITE_TLS_RENEW_BEFORE_SECONDS=2592000
SUITE_TLS_CA_SUBJECT="/C=JP/ST=Tokyo/L=Minato-ku/O=Oracle/OU=Production Ready Suite/CN=Production Ready Root CA"
SUITE_TLS_SERVER_SUBJECT_PREFIX="/C=JP/ST=Tokyo/L=Minato-ku/O=Oracle/OU=Production Ready Suite/CN="
SUITE_TLS_DIR="${SUITE_TLS_DIR:-/u01/aipoc/ssl}"
SUITE_TLS_PROPS_DIR="${SUITE_TLS_PROPS_DIR:-/u01/aipoc/props}"
SUITE_TLS_IMDS_VNICS_URL="${SUITE_TLS_IMDS_VNICS_URL:-http://169.254.169.254/opc/v2/vnics/}"
# 公開 IP は OCI の IMDS に無いため、Compute に公開 IP があるとき（assign_public_ip.txt が true）だけ、
# 外向きの通信の送信元の IP を返すサービスに問い合わせる（IGW 経由なら公開 IP と同じ）。既存の製品
# （No.1-RAG / No.1-SQL-Assist の init_script.sh の `curl -s -m 10 http://whatismyip.akamai.com/`）と同じ akamai を先に使い、
# 取れなければ checkip.amazonaws.com を 1 つだけ試す。どちらも IPv4 でなければ使わず、private IP だけの証明書にする。
SUITE_TLS_PUBLIC_IP_URLS="${SUITE_TLS_PUBLIC_IP_URLS:-http://whatismyip.akamai.com/ https://checkip.amazonaws.com}"
SUITE_TLS_OPENSSL="${SUITE_TLS_OPENSSL:-openssl}"

suite_tls_log() {
  printf '[suite-tls] %s\n' "$*" >&2
}

suite_tls_is_ipv4() {
  local ip="$1" octet
  local IFS=.
  [[ "${ip}" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]] || return 1
  for octet in ${ip}; do
    [ "${octet}" -le 255 ] || return 1
  done
}

suite_tls_read_prop() {
  local name="$1"
  tr -d '[:space:]' 2>/dev/null < "${SUITE_TLS_PROPS_DIR}/${name}" || true
}

# private IP（OCI の IMDS v2 の 1 つ目の VNIC。取れなければ既定の経路の interface）。
suite_tls_detect_private_ip() {
  local ip=""
  if [ -n "${SUITE_TLS_PRIVATE_IP:-}" ]; then
    printf '%s\n' "${SUITE_TLS_PRIVATE_IP}"
    return 0
  fi
  ip="$(curl -fsS -m 5 -H "Authorization: Bearer Oracle" "${SUITE_TLS_IMDS_VNICS_URL}" 2>/dev/null \
    | grep -oE '"privateIp"[[:space:]]*:[[:space:]]*"[0-9.]+"' | head -n 1 | cut -d '"' -f 4 || true)"
  if suite_tls_is_ipv4 "${ip}"; then
    printf '%s\n' "${ip}"
    return 0
  fi
  ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "src") {print $(i + 1); exit}}' || true)"
  if suite_tls_is_ipv4 "${ip}"; then
    printf '%s\n' "${ip}"
    return 0
  fi
  return 1
}

# 公開 IP。明示（環境変数・props の public_ip.txt）を優先し、無ければ公開 IP がある Compute だけ外部に問い合わせる。
suite_tls_detect_public_ip() {
  local ip url
  ip="${SUITE_TLS_PUBLIC_IP:-$(suite_tls_read_prop public_ip.txt)}"
  if [ -n "${ip}" ]; then
    suite_tls_is_ipv4 "${ip}" || {
      suite_tls_log "公開 IP の指定が IPv4 ではありません: ${ip}"
      return 1
    }
    printf '%s\n' "${ip}"
    return 0
  fi
  if [ "$(suite_tls_read_prop assign_public_ip.txt)" != "true" ]; then
    return 1
  fi
  for url in ${SUITE_TLS_PUBLIC_IP_URLS}; do
    ip="$(curl -fsS -m 10 "${url}" 2>/dev/null | tr -d '[:space:]' || true)"
    if suite_tls_is_ipv4 "${ip}"; then
      printf '%s\n' "${ip}"
      return 0
    fi
  done
  return 1
}

suite_tls_ca_ready() {
  local dir="$1"
  [ -s "${dir}/ca.key" ] && [ -s "${dir}/ca.crt" ] \
    && "${SUITE_TLS_OPENSSL}" x509 -in "${dir}/ca.crt" -noout >/dev/null 2>&1
}

# CA を作る（既にあれば使う）。
suite_tls_ensure_ca() {
  local dir="$1"
  install -d -m 0755 "${dir}"
  if suite_tls_ca_ready "${dir}"; then
    if ! "${SUITE_TLS_OPENSSL}" x509 -in "${dir}/ca.crt" -noout -checkend "${SUITE_TLS_RENEW_BEFORE_SECONDS}" >/dev/null 2>&1; then
      suite_tls_log "WARNING: CA の証明書の期限が 30 日以内です。作り直す場合は ${dir}/ca.* を退避してから renew し、利用者に新しい CA を配ってください。"
    fi
    return 0
  fi
  suite_tls_log "自作の Root CA を作ります（${SUITE_TLS_CA_SUBJECT}）。"
  local tmp
  tmp="$(mktemp -d "${dir}/.ca.XXXXXX")"
  (
    umask 077
    "${SUITE_TLS_OPENSSL}" req -x509 -newkey "rsa:${SUITE_TLS_KEY_BITS}" -sha256 -nodes -utf8 \
      -days "${SUITE_TLS_CA_DAYS}" \
      -keyout "${tmp}/ca.key" -out "${tmp}/ca.crt" \
      -subj "${SUITE_TLS_CA_SUBJECT}" \
      -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
      -addext "keyUsage=critical,keyCertSign,cRLSign" \
      -addext "subjectKeyIdentifier=hash" >/dev/null 2>&1
  )
  chmod 0600 "${tmp}/ca.key"
  chmod 0644 "${tmp}/ca.crt"
  mv -f "${tmp}/ca.key" "${dir}/ca.key"
  mv -f "${tmp}/ca.crt" "${dir}/ca.crt"
  rm -rf -- "${tmp}"
  rm -f -- "${dir}/server.crt"
}

# 今のサーバー証明書の SAN の IP（並びを整えて 1 行）。
suite_tls_cert_ips() {
  local cert="$1"
  "${SUITE_TLS_OPENSSL}" x509 -in "${cert}" -noout -ext subjectAltName 2>/dev/null \
    | grep -oE 'IP Address:[0-9.]+' | cut -d: -f2 | sort -u | tr '\n' ' ' | sed 's/ $//'
}

suite_tls_wanted_ips() {
  printf '%s\n' "$@" | grep -v '^$' | sort -u | tr '\n' ' ' | sed 's/ $//'
}

# サーバー証明書を作り直す必要があるか（0 = 要る）。
suite_tls_server_needs_renewal() {
  local dir="$1"
  shift
  local wanted current
  [ -s "${dir}/server.crt" ] && [ -s "${dir}/server.key" ] || return 0
  "${SUITE_TLS_OPENSSL}" x509 -in "${dir}/server.crt" -noout -checkend "${SUITE_TLS_RENEW_BEFORE_SECONDS}" >/dev/null 2>&1 || return 0
  "${SUITE_TLS_OPENSSL}" verify -CAfile "${dir}/ca.crt" "${dir}/server.crt" >/dev/null 2>&1 || return 0
  if [ "$#" -gt 0 ]; then
    wanted="$(suite_tls_wanted_ips "$@")"
    current="$(suite_tls_cert_ips "${dir}/server.crt")"
    [ "${wanted}" = "${current}" ] || return 0
  fi
  return 1
}

# サーバー証明書を作る。$2: CN にする IP（公開 IP。無ければ private IP）、$3...: SAN の IP。
suite_tls_issue_server_cert() {
  local dir="$1"
  local common_name="$2"
  shift 2
  local san="" ip tmp
  for ip in $(suite_tls_wanted_ips "$@"); do
    san="${san:+${san},}IP:${ip}"
  done
  suite_tls_log "サーバー証明書を作ります（CN=${common_name}、SAN=${san}）。"
  tmp="$(mktemp -d "${dir}/.server.XXXXXX")"
  cat > "${tmp}/server.ext" <<EOF
authorityKeyIdentifier=keyid,issuer
subjectKeyIdentifier=hash
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=${san}
EOF
  (
    umask 077
    "${SUITE_TLS_OPENSSL}" req -new -newkey "rsa:${SUITE_TLS_KEY_BITS}" -sha256 -nodes -utf8 \
      -keyout "${tmp}/server.key" -out "${tmp}/server.csr" \
      -subj "${SUITE_TLS_SERVER_SUBJECT_PREFIX}${common_name}" >/dev/null 2>&1
    "${SUITE_TLS_OPENSSL}" x509 -req -in "${tmp}/server.csr" \
      -CA "${dir}/ca.crt" -CAkey "${dir}/ca.key" -CAserial "${dir}/ca.srl" -CAcreateserial \
      -out "${tmp}/server.crt" -days "${SUITE_TLS_SERVER_DAYS}" -sha256 \
      -extfile "${tmp}/server.ext" >/dev/null 2>&1
  )
  "${SUITE_TLS_OPENSSL}" verify -CAfile "${dir}/ca.crt" "${tmp}/server.crt" >/dev/null
  chmod 0600 "${tmp}/server.key" "${dir}/ca.srl"
  chmod 0644 "${tmp}/server.crt"
  mv -f "${tmp}/server.key" "${dir}/server.key"
  mv -f "${tmp}/server.crt" "${dir}/server.crt"
  rm -rf -- "${tmp}"
}

# CA とサーバー証明書をそろえる（必要なときだけ作る）。force=true なら作り直す。
#   $1: directory  $2: 公開 IP（空可）  $3: private IP  $4: force
suite_tls_ensure() {
  local dir="$1"
  local public_ip="$2"
  local private_ip="$3"
  local force="${4:-false}"
  local common_name="${public_ip:-${private_ip}}"
  if [ -z "${common_name}" ]; then
    suite_tls_log "Compute の IP アドレスが分かりません。"
    return 1
  fi
  suite_tls_ensure_ca "${dir}"
  if [ "${force}" = "true" ] || suite_tls_server_needs_renewal "${dir}" ${public_ip:+"${public_ip}"} ${private_ip:+"${private_ip}"}; then
    suite_tls_issue_server_cert "${dir}" "${common_name}" ${public_ip:+"${public_ip}"} ${private_ip:+"${private_ip}"}
  else
    suite_tls_log "サーバー証明書はそのまま使います（$(suite_tls_cert_ips "${dir}/server.crt")）。"
  fi
  chmod 0600 "${dir}/ca.key" "${dir}/server.key"
  chmod 0644 "${dir}/ca.crt" "${dir}/server.crt"
  chown root:root "${dir}/ca.key" "${dir}/server.key" 2>/dev/null || true
  if [ -z "${public_ip}" ]; then
    suite_tls_log "WARNING: 公開 IP が分からないため、証明書の CN と SAN は private IP（${private_ip}）だけです。"
    suite_tls_log "公開 IP で使う場合: sudo bash $(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/suite-tls.sh renew --public-ip <公開 IP>"
  fi
}

suite_tls_reload_nginx() {
  if command -v nginx >/dev/null 2>&1 && nginx -t >/dev/null 2>&1; then
    systemctl reload nginx || systemctl restart nginx
  fi
}

suite_tls_status() {
  local dir="$1"
  if ! suite_tls_ca_ready "${dir}"; then
    echo "CA: なし（${dir}）"
    return 1
  fi
  "${SUITE_TLS_OPENSSL}" x509 -in "${dir}/ca.crt" -noout -subject -enddate
  if [ -s "${dir}/server.crt" ]; then
    "${SUITE_TLS_OPENSSL}" x509 -in "${dir}/server.crt" -noout -subject -issuer -enddate -ext subjectAltName
  else
    echo "サーバー証明書: なし"
  fi
}

suite_tls_main() {
  local action="${1:-status}"
  shift || true
  local public_ip="" private_ip="" current_ips
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --public-ip) public_ip="$2"; shift 2 ;;
      --private-ip) private_ip="$2"; shift 2 ;;
      *) echo "unknown option: $1" >&2; return 2 ;;
    esac
  done
  case "${action}" in
    status)
      suite_tls_status "${SUITE_TLS_DIR}"
      ;;
    renew)
      [ -n "${private_ip}" ] || private_ip="$(suite_tls_detect_private_ip)" || private_ip=""
      [ -n "${public_ip}" ] || public_ip="$(suite_tls_detect_public_ip)" || public_ip=""
      if [ -n "${public_ip}" ]; then
        printf '%s\n' "${public_ip}" > "${SUITE_TLS_PROPS_DIR}/public_ip.txt"
      fi
      suite_tls_ensure "${SUITE_TLS_DIR}" "${public_ip}" "${private_ip}" true
      suite_tls_reload_nginx
      ;;
    renew-if-needed)
      # timer から呼ぶ。外部へ問い合わせず、今の証明書の IP のまま期限だけを見る。
      [ -s "${SUITE_TLS_DIR}/server.crt" ] || return 0
      if suite_tls_server_needs_renewal "${SUITE_TLS_DIR}"; then
        current_ips="$(suite_tls_cert_ips "${SUITE_TLS_DIR}/server.crt")"
        public_ip="$(suite_tls_read_prop public_ip.txt)"
        private_ip="$(suite_tls_detect_private_ip || true)"
        # shellcheck disable=SC2086
        suite_tls_issue_server_cert "${SUITE_TLS_DIR}" "${public_ip:-${private_ip}}" ${current_ips} ${public_ip} ${private_ip}
        suite_tls_reload_nginx
      fi
      ;;
    *)
      echo "usage: $0 status | renew [--public-ip IP] [--private-ip IP] | renew-if-needed" >&2
      return 2
      ;;
  esac
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  suite_tls_main "$@"
fi
