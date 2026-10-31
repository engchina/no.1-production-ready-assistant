"""Oracle 永続 queue から NL2SQL job を実行する worker。"""

from __future__ import annotations

import logging
import signal
import socket
import time

from pr_backend_core import configure_logging
from pr_backend_core.observability.tracing import tracing_lifecycle

from app.features.nl2sql.service import nl2sql_service
from app.features.nl2sql.trace_spans import start_trace_span_store, stop_trace_span_store
from app.security.service import get_security_service
from app.settings import get_settings

logger = logging.getLogger(__name__)
_running = True


def _stop(_signum: int, _frame: object) -> None:
    global _running  # noqa: PLW0603
    _running = False


def main() -> None:
    with tracing_lifecycle(
        get_settings(),
        service_name=get_settings().service_name,
        capture_content=lambda: get_security_service().trace_recording.enabled(),
    ):
        settings = get_settings()
        configure_logging(
            settings.log_level,
            service_name=settings.service_name,
            service_version=settings.app_version,
            environment=settings.environment,
            component="job_worker",
        )
        worker_id = f"{socket.gethostname()}:nl2sql-job"
        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        # 処理の経過の span の書き手（#3226 の案 A）。実行中の span を共通の span の表に書く。
        start_trace_span_store()
        logger.info("nl2sql_job_worker_started", extra={"worker_id": worker_id})
        try:
            while _running:
                processed = nl2sql_service.run_next_nl2sql_job(worker_id=worker_id)
                if not processed:
                    time.sleep(max(0.1, settings.nl2sql_job_worker_poll_seconds))
        finally:
            stop_trace_span_store()
        logger.info("nl2sql_job_worker_stopped", extra={"worker_id": worker_id})


if __name__ == "__main__":
    main()
