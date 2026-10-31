#!/usr/bin/env bash
# 時刻境界・破損行・相関フィルタ・安全な export を実ストリームで検証する。
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python3 - "${repo}/scripts/log_viewer.py" <<'PY'
import json, os, subprocess, sys
path = sys.argv[1]
records = [
    '{broken',
    json.dumps({"MESSAGE": [999, None, 97], "__REALTIME_TIMESTAMP": "invalid"}),
    json.dumps({"schema_version": 1, "timestamp": "2026-10-02T23:59:59.999Z", "level": "ERROR", "message": "診断", "request_id": "request-1", "authorization": "PRIVATE_SENTINEL"}),
    json.dumps({"timestamp_epoch": 1790985600, "message": "native", "level": "INFO"}),
]
env = dict(os.environ, TAIL_LOGS_JSON="true", TZ="UTC")
a = subprocess.check_output([sys.executable, path], input="\n".join(records).encode(), env=env).decode()
env["TZ"] = "Asia/Tokyo"
b = subprocess.check_output([sys.executable, path], input="\n".join(records).encode(), env=env).decode()
assert a == b and "PRIVATE_SENTINEL" not in a
rows = [json.loads(line) for line in a.splitlines()]
assert len(rows) == 4 and rows[2]["timestamp"] == "2026-10-03T08:59:59.999+09:00"
env["TAIL_LOGS_ID_FILTER"] = "request-id:request-1"
filtered = subprocess.check_output([sys.executable, path], input="\n".join(records).encode(), env=env).decode()
assert len(filtered.splitlines()) == 1
assert json.loads(filtered)["request_id"] == "request-1"
print("共有ログ viewer の契約: pass")
PY
