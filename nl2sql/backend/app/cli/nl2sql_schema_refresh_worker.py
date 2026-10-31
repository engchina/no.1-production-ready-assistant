"""Lease 付き永続 schema refresh job worker。"""

from __future__ import annotations

import argparse
import time

from pr_backend_core import configure_logging

from app.features.nl2sql.service import nl2sql_service
from app.settings import get_settings


def main() -> int:
    settings = get_settings()
    configure_logging(
        settings.log_level,
        service_name=settings.service_name,
        service_version=settings.app_version,
        environment=settings.environment,
        component="schema_refresh_worker",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="1 件だけ確認して終了する")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args()

    while True:
        processed = nl2sql_service.run_next_schema_refresh_job()
        if args.once:
            return 0
        if not processed:
            time.sleep(max(0.2, args.poll_seconds))


if __name__ == "__main__":  # pragma: no cover - CLI boundary
    raise SystemExit(main())
