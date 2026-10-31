#!/usr/bin/env bash
# platform/deploy/suite-nginx.sh が生成する 1 台の Compute の Nginx の site（#1316）を検証する。
#   1. 生成した設定の静的な検査（製品の組み合わせ・HTTPS on / off・/ の転送先）
#   2. 隔離した Nginx と偽の backend に実 HTTP / HTTPS を送る（prefix を外した URI・SSE・MCP・login の上限・
#      health・frontend の SPA・licenses の Content-Type・/platform/ca.crt・HTTP → HTTPS・TLS 1.2 / 1.3・HSTS が無いこと）
# Nginx が無い環境では 2 を飛ばす（CI は nginx を入れて実行する）。
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
nginx_bin="${NGINX_BIN:-nginx}"
task_dir="$(mktemp -d)"
backend_pids=()
cleanup() {
  if [ -f "${task_dir}/nginx.pid" ]; then
    kill "$(cat "${task_dir}/nginx.pid")" 2>/dev/null || true
  fi
  local pid
  for pid in "${backend_pids[@]}"; do
    kill "${pid}" 2>/dev/null || true
  done
  rm -rf -- "${task_dir}"
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

assert_contains() {
  local haystack="$1" needle="$2" message="$3"
  case "${haystack}" in
    *"${needle}"*) ;;
    *) fail "${message}: '${needle}' が無い" ;;
  esac
}

assert_not_contains() {
  local haystack="$1" needle="$2" message="$3"
  case "${haystack}" in
    *"${needle}"*) fail "${message}: '${needle}' がある" ;;
  esac
}

free_port() {
  python3 - <<'PY'
import socket
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    print(sock.getsockname()[1])
PY
}

# shellcheck source=../suite-nginx.sh
source "${repo}/platform/deploy/suite-nginx.sh"

# ---------------------------------------------------------------- 1. 静的な検査

site_all_https="$(suite_nginx_site true /srv/suite /srv/ssl 80 443 rag nl2sql agent)"
assert_contains "${site_all_https}" "listen 443 ssl http2 default_server;" "HTTPS は 443"
# HTTPS が on なら http_port（80）は listen せず、https への転送の server も置かない（#1638）。
assert_not_contains "${site_all_https}" "listen 80" "HTTPS が on なら 80 を listen しない"
assert_not_contains "${site_all_https}" "return 301 https" "HTTPS が on でも http から https へ転送しない"
assert_contains "${site_all_https}" "ssl_protocols TLSv1.2 TLSv1.3;" "TLS 1.2 / 1.3 だけ"
assert_not_contains "${site_all_https}" "Strict-Transport-Security" "HSTS は付けない"
assert_contains "${site_all_https}" "location = /platform/ca.crt" "CA 証明書を配る"
assert_contains "${site_all_https}" "alias /srv/ssl/ca.crt;" "CA 証明書の場所"
assert_not_contains "${site_all_https}" "ca.key" "CA の秘密鍵を配らない"
assert_contains "${site_all_https}" "return 302 /agent/;" "/ は /agent/ へ"
assert_contains "${site_all_https}" 'add_header Cache-Control "no-cache" always;' "index.html は毎回確かめる"
assert_contains "${site_all_https}" 'add_header Cache-Control "public, max-age=31536000, immutable" always;' "hash 付きの build の出力は長く cache"
for product in rag nl2sql agent; do
  assert_contains "${site_all_https}" "location /${product}/api/ {" "${product} の API"
  assert_contains "${site_all_https}" "location = /${product}/api/auth/login {" "${product} のログインの上限"
  assert_contains "${site_all_https}" "location = /${product}/health {" "${product} の health"
  assert_contains "${site_all_https}" "alias /srv/suite/${product}/frontend/dist/;" "${product} の frontend"
  assert_contains "${site_all_https}" "location /${product}/assets/ {" "${product} の build の出力"
  assert_contains "${site_all_https}" "alias /srv/suite/${product}/frontend/dist/assets/;" "${product} の build の出力の場所"
  assert_contains "${site_all_https}" "location /${product}/licenses/ {" "${product} のライセンスの一覧"
  assert_contains "${site_all_https}" "alias /srv/suite/${product}/frontend/dist/licenses/;" "${product} のライセンスの一覧の場所"
done
assert_contains "${site_all_https}" "proxy_pass http://127.0.0.1:8000\$pr_suite_upstream_uri;" "RAG の backend は 8000"
assert_contains "${site_all_https}" "proxy_pass http://127.0.0.1:8010\$pr_suite_upstream_uri;" "NL2SQL の backend は 8010"
assert_contains "${site_all_https}" "proxy_pass http://127.0.0.1:8020\$pr_suite_upstream_uri;" "Agent の backend は 8020"
assert_contains "${site_all_https}" "|mcp)\$ {" "RAG の MCP は長い timeout"

# 3 製品の実際の動きに合わせた値（#1597。根拠は suite-nginx.sh の SUITE_* のコメント）。
count_of() {
  printf '%s\n' "$1" | grep -cF -- "$2" || true
}
# 長い待ち時間: RAG 660 秒（回答の上限 600 秒・画面 630 秒より長く）・NL2SQL 2460 秒（LLM 600 秒 × 4 回 + 60）・
# Agent の tools/invoke 960 秒（外部 MCP の上限 900 秒 + 60）。ほかの API は 600 秒（SSE は heartbeat）。
assert_contains "${site_all_https}" "search-answer-profiles/[^/]+/support-guides/[^/]+/try|" "RAG の業務ガイドを試すは長い timeout"
assert_contains "${site_all_https}" "location ~ ^/nl2sql/api/(nl2sql/analyze|nl2sql/reverse/deep|" "NL2SQL の LLM の同期の API は長い timeout"
assert_contains "${site_all_https}" "nl2sql/db-admin-chat/conversations/[^/]+/turns/[^/]+/analyze-error" "保存したターンの分析は長い timeout"
assert_contains "${site_all_https}" "location ~ ^/agent/api/(tools/invoke)\$ {" "Agent の tools/invoke は長い timeout"
for value in 660 2460 960; do
  [ "$(count_of "${site_all_https}" "proxy_read_timeout ${value}s;")" = 1 ] || fail "proxy_read_timeout ${value}s が 1 か所ではない"
  [ "$(count_of "${site_all_https}" "proxy_send_timeout ${value}s;")" = 1 ] || fail "proxy_send_timeout ${value}s が 1 か所ではない"
done
assert_contains "${site_all_https}" "proxy_read_timeout 600s;" "既定の待ち時間"
# 本文の上限: backend の <製品>_MAX_UPLOAD_BYTES の既定値 + 10 MiB（RAG・NL2SQL 210M、Agent 110M）。製品の外は 10m。
[ "$(count_of "${site_all_https}" "client_max_body_size 210M;")" = 4 ] || fail "RAG・NL2SQL の API の本文の上限"
[ "$(count_of "${site_all_https}" "client_max_body_size 110M;")" = 2 ] || fail "Agent の API の本文の上限"
assert_contains "${site_all_https}" "client_max_body_size 10m;" "製品の外の本文の上限"
site_custom_body="$(SUITE_NL2SQL_CLIENT_MAX_BODY_SIZE=60M suite_nginx_site true /srv/suite /srv/ssl 80 443 nl2sql)"
[ "$(count_of "${site_custom_body}" "client_max_body_size 60M;")" = 2 ] || fail "NL2SQL の本文の上限を変えられない"
# 3 製品とも HTTP だけ（push は SSE、操作は REST。WebSocket は持たない。#2260）。Upgrade を渡さず、
# Connection を空にし、Host は 3 製品で同じ $host にする。
assert_not_contains "${site_all_https}" 'Upgrade' "WebSocket の Upgrade を渡さない"
assert_not_contains "${site_all_https}" 'pr_suite_connection_upgrade' "WebSocket の Connection の map を持たない"
assert_not_contains "${site_all_https}" '$http_host' "Host は 3 製品で同じ"
[ "$(count_of "${site_all_https}" 'proxy_set_header Connection "";')" = 9 ] || fail "API の location の Connection"
[ "$(count_of "${site_all_https}" 'proxy_set_header Host $host;')" = 12 ] || fail "API と health の location の Host"
# ストリーム: API の location（製品ごとに login・長い API・API 全体の 3 つ）は buffering・cache・gzip を切る。
for directive in "proxy_buffering off;" "proxy_cache off;" "gzip off;"; do
  [ "$(count_of "${site_all_https}" "${directive}")" = 9 ] || fail "API の location の ${directive}"
done
# header: 要求の header（共有の Cookie）と応答の header（Content-Disposition 等）の buffer。
# HTTPS が on なら server は HTTPS の 1 つだけ（http_port の転送の server は置かない。#1638）。
[ "$(count_of "${site_all_https}" "large_client_header_buffers 4 16k;")" = 1 ] || fail "HTTPS の server の large_client_header_buffers"
[ "$(count_of "${site_all_https}" "server {")" = 1 ] || fail "HTTPS が on なら server は 1 つだけ"
assert_contains "${site_all_https}" "proxy_buffer_size 16k;" "応答の header の buffer"
assert_contains "${site_all_https}" "proxy_buffers 8 16k;" "応答の buffer"

site_http="$(suite_nginx_site false /srv/suite /srv/ssl 80 443 rag nl2sql agent)"
assert_contains "${site_http}" "listen 80 default_server;" "HTTP は 80"
[ "$(count_of "${site_http}" "large_client_header_buffers 4 16k;")" = 1 ] || fail "HTTP の server の large_client_header_buffers"
assert_not_contains "${site_http}" "ssl" "HTTPS が off なら TLS を設定しない"
assert_not_contains "${site_http}" "location = /platform/ca.crt" "HTTPS が off なら CA 証明書を配らない"
assert_not_contains "${site_http}" "return 301 https" "HTTPS が off なら転送しない"

site_rag_only="$(suite_nginx_site true /srv/suite /srv/ssl 80 443 rag)"
assert_contains "${site_rag_only}" "return 302 /rag/;" "Agent が無いときは最初の製品へ"
assert_not_contains "${site_rag_only}" "/agent/api/" "配備しない製品の location は書かない"
assert_not_contains "${site_rag_only}" "/nl2sql/api/" "配備しない製品の location は書かない"
site_nl2sql_agent="$(suite_nginx_site true /srv/suite /srv/ssl 80 443 nl2sql agent)"
assert_contains "${site_nl2sql_agent}" "return 302 /agent/;" "Agent を優先する"
if suite_nginx_site true /srv/suite /srv/ssl 80 443 >/dev/null 2>&1; then
  fail "製品が 0 件なら失敗する"
fi
if suite_nginx_site true /srv/suite /srv/ssl 80 443 rag platform >/dev/null 2>&1; then
  fail "未知の製品なら失敗する"
fi

# port は変数（#1316）。HTTPS が on なら https_port だけを listen し、http_port は使わない（#1638）。
# HTTPS が off なら http_port で直接配信する。
site_custom="$(suite_nginx_site true /srv/suite /srv/ssl 8080 8443 rag nl2sql agent)"
assert_contains "${site_custom}" "listen 8443 ssl http2 default_server;" "https_port で listen する"
assert_not_contains "${site_custom}" "listen 8080" "HTTPS が on なら http_port を listen しない"
assert_not_contains "${site_custom}" "return 301 https" "HTTPS が on なら http_port から転送しない"
site_same_port="$(suite_nginx_site true /srv/suite /srv/ssl 80 80 rag)"
assert_contains "${site_same_port}" "listen 80 ssl http2 default_server;" "https_port が 80 なら 80 で HTTPS"
assert_not_contains "${site_same_port}" "return 301 https" "https_port と http_port が同じでも転送の server を置かない"
# HTTPS が on なら http_port は使わないので検査しない。off なら https_port は検査しない。
suite_nginx_site true /srv/suite /srv/ssl 22 443 rag >/dev/null 2>&1 || fail "HTTPS が on なのに http_port を検査した"
site_http_custom="$(suite_nginx_site false /srv/suite /srv/ssl 8080 22 rag)"
assert_contains "${site_http_custom}" "listen 8080 default_server;" "HTTPS が off なら http_port で配信する"
assert_not_contains "${site_http_custom}" "return 301 https" "HTTPS が off なら転送しない"
# 使えない port（範囲外・SSH・よく使われる service・Compute の中で使う port）は拒む。
for port in 0 65536 abc 22 25 53 111 1521 1522 3000 3001 3002 3306 5432 6379 9090 8000 8010 8020 18000 18010 18028 18099; do
  if suite_validate_public_port http_port "${port}" 2>/dev/null; then
    fail "使えない port ${port} を受け入れた"
  fi
  if suite_nginx_site true /srv/suite /srv/ssl 80 "${port}" rag >/dev/null 2>&1; then
    fail "https_port ${port} で site を作った"
  fi
done
for port in 80 443 8080 8443 17999 18100 65535; do
  suite_validate_public_port http_port "${port}" || fail "使える port ${port} を拒んだ"
done
echo "suite-nginx: 静的な検査: pass"

# ---------------------------------------------------------------- 2. 実 HTTP / HTTPS

if ! command -v "${nginx_bin}" >/dev/null 2>&1; then
  echo "suite-nginx: nginx が無いため、実 HTTP の検査を飛ばす（CI は実行する）"
  exit 0
fi

mime_types="${NGINX_MIME_TYPES:-/etc/nginx/mime.types}"
mime_include=""
if [ -f "${mime_types}" ]; then
  mime_include="include ${mime_types};"
fi

# 偽の backend: 受け取った method・URI（エンコードのまま）・header を JSON で返す。SSE の path は 2 回に分けて送る。
cat > "${task_dir}/backend.py" <<'PY'
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

name, port = sys.argv[1], int(sys.argv[2])

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *args):
        pass
    def _reply(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if "backend_limited=1" in self.path:
            body = b'{"error_code":"BACKEND_429"}'
            self.send_response(429)
        elif "big_header=1" in self.path:
            # 大きな応答の header（長いファイル名の Content-Disposition など）。既定の proxy_buffer_size（4k）なら 502。
            body = b'{"big_header":true}'
            self.send_response(200)
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + "%E6%97%A5" * 1000)
        elif self.path.endswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for chunk in (b"data: one\n\n", b"data: two\n\n"):
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
                time.sleep(0.2)
            self.wfile.write(b"0\r\n\r\n")
            return
        else:
            body = json.dumps({"backend": name, "method": self.command, "path": self.path, "headers": dict(self.headers), "body_bytes": length}).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    do_GET = do_POST = do_PUT = do_DELETE = _reply

ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
PY

export SUITE_RAG_BACKEND_PORT SUITE_NL2SQL_BACKEND_PORT SUITE_AGENT_BACKEND_PORT
SUITE_RAG_BACKEND_PORT="$(free_port)"
SUITE_NL2SQL_BACKEND_PORT="$(free_port)"
SUITE_AGENT_BACKEND_PORT="$(free_port)"
python3 "${task_dir}/backend.py" rag "${SUITE_RAG_BACKEND_PORT}" &
backend_pids+=("$!")
python3 "${task_dir}/backend.py" nl2sql "${SUITE_NL2SQL_BACKEND_PORT}" &
backend_pids+=("$!")
python3 "${task_dir}/backend.py" agent "${SUITE_AGENT_BACKEND_PORT}" &
backend_pids+=("$!")

# 偽の frontend の dist。
for product in rag nl2sql agent; do
  mkdir -p "${task_dir}/suite/${product}/frontend/dist/assets"
  printf '<!doctype html><title>%s</title>\n' "${product}" > "${task_dir}/suite/${product}/frontend/dist/index.html"
  printf 'console.log("%s");\n' "${product}" > "${task_dir}/suite/${product}/frontend/dist/assets/app.js"
  mkdir -p "${task_dir}/suite/${product}/frontend/dist/licenses/fonts"
  printf '# ライセンス %s\n' "${product}" > "${task_dir}/suite/${product}/frontend/dist/licenses/javascript.md"
  printf 'font license %s\n' "${product}" > "${task_dir}/suite/${product}/frontend/dist/licenses/fonts/font-a-OFL.txt"
done

# 証明書（private IP を 127.0.0.1 にして、検証付きの HTTPS で確かめる）。
# shellcheck source=../suite-tls.sh
source "${repo}/platform/deploy/suite-tls.sh"
suite_tls_ensure "${task_dir}/ssl" "" 127.0.0.1 false 2>/dev/null

run_case() {
  local https_enabled="$1"
  local http_port https_port
  http_port="$(free_port)"
  https_port="$(free_port)"
  export SUITE_LISTEN_ADDRESS=127.0.0.1
  export SUITE_NGINX_LOG_DIR="${task_dir}/logs-${https_enabled}"
  mkdir -p "${SUITE_NGINX_LOG_DIR}"
  suite_nginx_site "${https_enabled}" "${task_dir}/suite" "${task_dir}/ssl" "${http_port}" "${https_port}" \
    rag nl2sql agent > "${task_dir}/site.conf"
  cat > "${task_dir}/nginx.conf" <<CONF
pid ${task_dir}/nginx.pid;
error_log ${task_dir}/error.log warn;
events {}
http {
  ${mime_include}
  client_body_temp_path ${task_dir}/body;
  proxy_temp_path ${task_dir}/proxy;
  fastcgi_temp_path ${task_dir}/fastcgi;
  uwsgi_temp_path ${task_dir}/uwsgi;
  scgi_temp_path ${task_dir}/scgi;
  # Ubuntu の nginx.conf の gzip on に、JSON・SSE の圧縮を足した環境でも、API の応答は圧縮しない（#1597）。
  gzip on;
  gzip_types application/json text/event-stream;
  include ${repo}/platform/templates/nginx/logging.conf;
  include ${repo}/platform/templates/nginx/login-rate-limit.conf;
  include ${task_dir}/site.conf;
}
CONF
  "${nginx_bin}" -t -q -c "${task_dir}/nginx.conf" -p "${task_dir}"
  "${nginx_bin}" -c "${task_dir}/nginx.conf" -p "${task_dir}"
  python3 - "${https_enabled}" "${http_port}" "${https_port}" "${task_dir}" <<'PY'
import http.client, json, ssl, sys, time
https_enabled, http_port, https_port, task_dir = sys.argv[1] == "true", int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
ca = f"{task_dir}/ssl/ca.crt"

def connect(tls_version=None):
    if not https_enabled:
        return http.client.HTTPConnection("127.0.0.1", http_port, timeout=10)
    context = ssl.create_default_context(cafile=ca)
    if tls_version:
        context.minimum_version = context.maximum_version = tls_version
    return http.client.HTTPSConnection("127.0.0.1", https_port, timeout=10, context=context)

def call(path, method="GET", body=None, headers=None, conn=None):
    conn = conn or connect()
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    data = response.read()
    return response.status, {k.lower(): v for k, v in response.getheaders()}, data

def backend(path, method="GET"):
    status, headers, data = call(path, method, body=b"{}" if method == "POST" else None)
    assert status == 200, (path, status, data)
    return json.loads(data)

# / は /agent/ へ（相対の Location）。
status, headers, _ = call("/")
assert (status, headers["location"]) == (302, "/agent/"), (status, headers)

for product in ("rag", "nl2sql", "agent"):
    status, headers, _ = call(f"/{product}")
    assert (status, headers["location"]) == (301, f"/{product}/"), (product, status, headers)
    # frontend: SPA の画面の URL は index.html、assets はそのまま。
    for path in (f"/{product}/", f"/{product}/settings/oci", f"/{product}/documents/a/b"):
        status, _, data = call(path)
        assert status == 200 and f"<title>{product}</title>" in data.decode(), (path, status, data)
    status, headers, data = call(f"/{product}/assets/app.js")
    assert status == 200 and product in data.decode(), (product, status)
    assert headers["content-type"] == "application/javascript", headers
    # ライセンスの一覧（.md / .txt）はブラウザで読めるよう text/plain; charset=utf-8（#3420）。無いファイルは 404。
    status, headers, data = call(f"/{product}/licenses/javascript.md")
    assert status == 200 and f"ライセンス {product}" in data.decode(), (product, status, data)
    assert headers["content-type"] == "text/plain; charset=utf-8", headers
    assert headers["x-content-type-options"] == "nosniff" and headers["cache-control"] == "no-cache", headers
    assert "content-disposition" not in headers, headers
    status, headers, data = call(f"/{product}/licenses/fonts/font-a-OFL.txt")
    assert status == 200 and headers["content-type"] == "text/plain; charset=utf-8", (product, status, headers)
    assert call(f"/{product}/licenses/missing.md")[0] == 404, product
    # API: prefix だけを外し、エンコードと query はそのまま渡す。
    echo = backend(f"/{product}/api/documents/a%2Fb/files?x=1&y=%E6%97%A5")
    assert echo["backend"] == product, echo
    assert echo["path"] == "/api/documents/a%2Fb/files?x=1&y=%E6%97%A5", echo
    assert echo["headers"].get("X-Forwarded-Prefix") == f"/{product}", echo
    assert echo["headers"].get("X-Forwarded-Proto") == ("https" if https_enabled else "http"), echo
    assert backend(f"/{product}/api/")["path"] == "/api/", product
    status, headers, _ = call(f"/{product}/api")
    assert (status, headers["location"]) == (308, f"/{product}/api/"), (product, status, headers)
    # health は backend の /api/health。
    assert backend(f"/{product}/health")["path"] == "/api/health", product
    # MCP（他製品・外部から呼ぶ。Agent → RAG / NL2SQL は 127.0.0.1 の backend を直接呼ぶ）。
    assert backend(f"/{product}/api/mcp", "POST")["path"] == "/api/mcp", product

def echo_headers(path, headers):
    status, _, data = call(path, headers=headers)
    assert status == 200, (path, status, data)
    return json.loads(data)["headers"]
# 3 製品とも HTTP だけ（WebSocket は持たない。#2260）。Host は同じ $host（port なし）で、
# 要求に Upgrade があっても upstream へ渡さない（#1597）。
ws_headers = {"Upgrade": "websocket", "Connection": "Upgrade"}
for product in ("rag", "nl2sql", "agent"):
    assert backend(f"/{product}/api/runs")["headers"]["Host"] == "127.0.0.1", product
    other = echo_headers(f"/{product}/api/runs/r1/events", ws_headers)
    assert "upgrade" not in (other.get("Connection") or "").lower() and "Upgrade" not in other, (product, other)

# 大きな Cookie（16k の buffer の範囲）と大きな応答の header を通す（#1597）。
cookie = "pr_session=" + "a" * 43 + "; pr_csrf=" + "b" * 43 + "; other=" + "c" * 11000
assert echo_headers("/nl2sql/api/me", {"Cookie": cookie})["Cookie"] == cookie
status, headers, _ = call("/rag/api/documents/d1/content?big_header=1")
assert status == 200 and len(headers["content-disposition"]) > 8000, (status, len(headers.get("content-disposition", "")))

# 本文の上限: 製品の外の 10m を超える本文も、製品の API の location の上限（110M / 210M）の中なら通す。
status, _, data = call("/agent/api/settings/database/wallet", "POST", b"x" * (11 * 1024 * 1024))
assert status == 200 and json.loads(data)["body_bytes"] == 11 * 1024 * 1024, (status, data[:200])

# gzip を有効にした環境でも、API の応答（JSON・SSE）は圧縮しない。
status, headers, _ = call("/agent/api/runs", headers={"Accept-Encoding": "gzip"})
assert status == 200 and "content-encoding" not in headers, headers

# RAG の SSE: buffering せず、最初の event が終わりより先に届く。
conn = connect()
conn.request("POST", "/rag/api/search/stream", body=b"{}", headers={"Accept-Encoding": "gzip"})
response = conn.getresponse()
assert response.status == 200 and response.getheader("Content-Type") == "text/event-stream", response.status
assert response.getheader("Content-Encoding") is None, response.getheaders()
started = time.monotonic()
first = response.read1(64)
first_at = time.monotonic() - started
rest = response.read()
assert first.startswith(b"data: one"), first
assert first_at < 0.15 and b"data: two" in rest, (first_at, rest)

# 製品・platform の外は 404。/platform/ は予約（ca.crt 以外は 404）。
for path in ("/unknown", "/platform/", "/platform/index.html", "/api/health", "/ragx/"):
    assert call(path)[0] == 404, path

# CA 証明書（HTTPS が on のときだけ）。
status, headers, data = call("/platform/ca.crt")
if https_enabled:
    assert status == 200, status
    assert headers["content-type"] == "application/x-x509-ca-cert", headers
    assert headers["content-disposition"] == 'attachment; filename="production-ready-root-ca.crt"', headers
    assert data == open(ca, "rb").read()
    assert "strict-transport-security" not in headers, headers
    # TLS 1.2 / 1.3 の両方で接続でき、http_port は listen しない（HTTP から HTTPS へ転送しない。#1638）。
    for version in (ssl.TLSVersion.TLSv1_2, ssl.TLSVersion.TLSv1_3):
        assert call("/agent/", conn=connect(version))[0] == 200, version
    plain = http.client.HTTPConnection("127.0.0.1", http_port, timeout=10)
    try:
        plain.request("GET", "/rag/api/health?x=1")
        raise AssertionError(f"http_port {http_port} が listen されている: {plain.getresponse().status}")
    except ConnectionRefusedError:
        pass
else:
    assert status == 404, status

# ログインの上限（3 製品で同じ zone。送信元 IP ごとに 1 秒 1 回・burst 30）。backend の 429 はそのまま返す。
status, _, data = call("/agent/api/auth/login?backend_limited=1", "POST", b"{}")
assert (status, json.loads(data)) == (429, {"error_code": "BACKEND_429"}), (status, data)
statuses = [call("/rag/api/auth/login", "POST", b"{}")[0] for _ in range(40)]
assert statuses[:29] == [200] * 29 and 429 in statuses, statuses
status, headers, data = call("/nl2sql/api/auth/login", "POST", b"{}")
assert status == 429 and json.loads(data)["error_code"] == "SECURITY_RATE_LIMITED", (status, data)
assert headers.get("retry-after") == "60", headers
print(f"suite-nginx: 実 {'HTTPS' if https_enabled else 'HTTP'}: pass")
PY
  "${nginx_bin}" -s quit -c "${task_dir}/nginx.conf" -p "${task_dir}"
  for _ in $(seq 50); do
    [ -f "${task_dir}/nginx.pid" ] || break
    sleep 0.1
  done
  # 製品ごとの access log（tail-logs が読む）に、その製品の service_name で残る。
  python3 - "${SUITE_NGINX_LOG_DIR}" <<'PY'
import json, pathlib, sys
log_dir = pathlib.Path(sys.argv[1])
for product in ("rag", "nl2sql", "agent"):
    rows = [json.loads(line) for line in (log_dir / f"production-ready-{product}-access.log").read_text().splitlines()]
    assert rows and {row["service_name"] for row in rows} == {f"production-ready-{product}"}, (product, rows[:2])
    assert "/api/mcp" in {row["http_route"] for row in rows}, product
PY
}

run_case true
run_case false
echo "suite-nginx: pass"
