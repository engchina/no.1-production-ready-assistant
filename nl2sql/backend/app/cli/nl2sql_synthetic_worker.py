"""合成データ生成 worker。受理済み操作のみ実行し不明な実行は対照する。"""

import signal
import threading

from pr_backend_core import configure_logging

from app.features.nl2sql.synthetic_service import worker_loop
from app.settings import get_settings


def main() -> None:
    settings = get_settings()
    configure_logging(
        settings.log_level,
        service_name=settings.service_name,
        service_version=settings.app_version,
        environment=settings.environment,
        component="synthetic_worker",
    )
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    worker_loop(stop)


if __name__ == "__main__":
    main()
