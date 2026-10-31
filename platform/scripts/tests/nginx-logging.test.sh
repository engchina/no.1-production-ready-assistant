#!/usr/bin/env bash
# 隔離した Nginx に実 HTTP を送り、poll と secret の wire 契約を検証する。
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
nginx_bin="${NGINX_BIN:-nginx}"
task_dir="$(mktemp -d)"
trap 'if [ -f "${task_dir}/nginx.pid" ]; then kill "$(cat "${task_dir}/nginx.pid")" 2>/dev/null || true; fi; rm -rf -- "${task_dir}"' EXIT
port="$(python3 - <<'PY'
import socket
with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    print(sock.getsockname()[1])
PY
)"
cat > "${task_dir}/nginx.conf" <<CONF
pid ${task_dir}/nginx.pid;
error_log ${task_dir}/error.log warn;
events {}
http {
  client_body_temp_path ${task_dir}/body;
  proxy_temp_path ${task_dir}/proxy;
  fastcgi_temp_path ${task_dir}/fastcgi;
  uwsgi_temp_path ${task_dir}/uwsgi;
  scgi_temp_path ${task_dir}/scgi;
  include ${repo}/templates/nginx/logging.conf;
  server {
    listen 127.0.0.1:${port};
    set \$pr_service_name production-ready-test;
    access_log ${task_dir}/access.log production_ready_json if=\$pr_loggable;
    location / { if (\$arg_fail) { return 503; } return 200; }
  }
}
CONF
"${nginx_bin}" -t -c "${task_dir}/nginx.conf" -p "${task_dir}"
"${nginx_bin}" -c "${task_dir}/nginx.conf" -p "${task_dir}"
curl --noproxy '*' -s -o /dev/null "http://127.0.0.1:${port}/api/services/private-service/status?secret=PRIVATE_SENTINEL"
curl --noproxy '*' -s -o /dev/null -H 'X-Request-ID: request-1' -H 'Authorization: Bearer PRIVATE_SENTINEL' "http://127.0.0.1:${port}/api/services/private-service/status?fail=1"
curl --noproxy '*' -s -o /dev/null -H 'X-Request-ID: invalid@id' "http://127.0.0.1:${port}/api/mcp?question=PRIVATE_SENTINEL"
"${nginx_bin}" -s quit -c "${task_dir}/nginx.conf" -p "${task_dir}"
python3 - "${task_dir}/access.log" <<'PY'
import json, pathlib, re, sys
wire = pathlib.Path(sys.argv[1]).read_text()
assert 'PRIVATE_SENTINEL' not in wire and 'private-service' not in wire
rows = [json.loads(line) for line in wire.splitlines()]
assert len(rows) == 2, rows
assert rows[0]['http_status'] == 503 and rows[0]['level'] == 'ERROR'
assert rows[0]['request_id'] == 'request-1'
assert rows[0]['http_route'] == '/api/services/{service_id}/status'
assert re.fullmatch('[0-9a-f]{32}', rows[1]['request_id'])
assert rows[1]['http_route'] == '/api/mcp'
assert isinstance(rows[1]['timestamp_epoch'], (float, int))
print('Nginx JSON / poll / 脱機密化: pass')
PY
