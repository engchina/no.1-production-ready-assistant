"""NL2SQL 設定ファイルを read-only で監査し、stable JSON を出力する CLI。"""

from __future__ import annotations

import argparse
from pathlib import Path

from pr_backend_core.logging import configure_cli_logging

from app.config_audit import audit_configuration, default_audit_paths, stable_audit_json


def main() -> int:
    configure_cli_logging("nl2sql")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-dir", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    result = audit_configuration(default_audit_paths(args.backend_dir.resolve()))
    print(stable_audit_json(result))  # noqa: T201
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover - CLI boundary
    raise SystemExit(main())
