#!/usr/bin/env bash
# 公開する診断には数値の資源 ID とロック以外を含めない。
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
python3 - "${root}" <<'PY'
import importlib.util
import sys
from pathlib import Path

path = Path(sys.argv[1]) / '.github/scripts/oracle-deadlock-summary.py'
spec = importlib.util.spec_from_file_location('summary', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
resource = 'TM-00000001-00000000 1 2 SX SSX 3 4 SX SSX'
assert module.summarize([
    resource, resource,
    'SQL: DELETE FROM confidential WHERE value = 123',
    'environment=private-host',
    'TM-00000001-00000000 1 2 private 3 4',
    'TX-00000001-00000000 1 2 X 3 4 X; SELECT secret',
]) == [resource]
print('ok - 数値資源とロックだけを許可し SQL・値・環境識別子を出さない')
PY
