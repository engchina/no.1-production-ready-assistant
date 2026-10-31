"""backend の起動の契約（#165）。本番は init_script.sh が作る systemd の unit（#286 / #356）。"""

import re
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[2]
INIT_SCRIPT = AGENT_ROOT / "init_script.sh"


def test_backend_unit_runs_a_single_worker() -> None:
    """Runtime の状態は process の中にあるため、複数 worker にすると worker ごとに分かれる。"""
    init_script = INIT_SCRIPT.read_text(encoding="utf-8")

    assert 'BACKEND_WORKERS="1"' in init_script
    exec_start = re.search(r"(?m)^ExecStart=.*gunicorn app\.main:app.*$", init_script)
    assert exec_start is not None, "backend の unit の ExecStart が見つからない"
    assert "--worker-class uvicorn.workers.UvicornWorker" in exec_start.group(0)
    assert "--workers ${BACKEND_WORKERS}" in exec_start.group(0)


def test_own_code_has_no_container_image() -> None:
    """自前のコードは Docker イメージを作らない（#356）。外部 Runtime の compose も無い（#754）。"""
    ignored = {"node_modules", ".venv", "dist"}
    leftovers = [
        path
        for pattern in (
            "Dockerfile*",
            "docker-compose*.yml",
            "compose*.yml",
            ".env.runtime.example",
        )
        for path in AGENT_ROOT.rglob(pattern)
        if not ignored.intersection(path.relative_to(AGENT_ROOT).parts)
    ]
    assert leftovers == []
