"""組み込み Runtime の Run を claim/lease で実行する専用 worker（本番の別プロセス。#754）。

    python -m app.features.agent.runtime_dispatcher

`AGENT_RUNTIME_DISPATCH_MODE` が `in_process` 以外のとき、API は Run を queued で作るだけで、
この worker が Oracle の checkpoint の row lock で Run を claim して実行する。承認がすべて決まって
queued に戻った Run は、保存した SDK の状態から再開する。

claim は所有者・一意な token・世代を lease に書き、実行の開始はその claim を渡した条件付きの
遷移にする（`app.features.agent.run_claim`。#2606）。lease の期限が切れて別の worker が claim し
直した Run は、旧所有者が開始しても受け付けない（同じ claim の 2 回目の開始も受け付けない）。

1 回の claim・実行の失敗（DB の一時的な障害など）で worker を止めない。失敗はログに残し、
待ち（poll の間隔から 2 倍ずつ、上限 60 秒）を置いて続ける（#853）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from uuid import uuid4

from pr_backend_core import configure_logging
from pr_backend_core.observability.request_context import bind_log_context
from pr_backend_core.observability.tracing import tracing_lifecycle

from app.features.agent import builtin_runtime, run_claim
from app.features.agent.runtime import builtin_resume_pending, runtime_repository
from app.features.agent.trace_spans import trace_spans
from app.observability import start_trace_export_retry_worker, stop_trace_export_retry_worker
from app.security.service import get_security_service
from app.settings import get_settings

logger = logging.getLogger(__name__)
# 失敗が続いたときの待ちの上限（秒）。
_MAX_ERROR_BACKOFF_SECONDS = 60.0
# 待ち（テストは待たない関数に差し替える）。
_sleep = asyncio.sleep


async def dispatch_once(worker_id: str) -> bool:
    """queued の Run を 1 件 claim して実行する（無ければ False）。"""
    settings = get_settings()
    # claim は保存先の head を lock して読み書きする。event loop を塞がない（#1939）。
    run = await asyncio.to_thread(
        runtime_repository.claim_control_plane_run,
        worker_id,
        lease_seconds=settings.agent_runtime_dispatch_lease_seconds,
    )
    if run is None:
        return False
    claim = run_claim.claim_of(run.id, run.metadata)
    with bind_log_context(run_id=run.id, worker_id=worker_id):
        if claim is None:
            # claim の結果に lease が無い（起きないはず）。所有を示せないので開始しない。
            logger.warning(
                "runtime_dispatcher_claim_without_lease",
                extra={"run_id": run.id, "worker_id": worker_id},
            )
            return True
        if builtin_resume_pending(run):
            await builtin_runtime.resume_run(run.id, claim=claim)
        else:
            await builtin_runtime.execute_run(run.id, claim=claim)
    return True


async def run_forever() -> None:
    settings = get_settings()
    worker_id = os.environ.get(
        "AGENT_RUNTIME_DISPATCHER_ID",
        f"{socket.gethostname()}-{uuid4().hex[:8]}",
    )
    poll_seconds = max(0.1, settings.agent_runtime_dispatch_poll_seconds)
    logger.info(
        "実行 dispatcher を開始しました",
        extra={"event": "runtime_dispatcher_started", "worker_id": worker_id},
    )
    failures = 0
    while True:
        try:
            claimed = await dispatch_once(worker_id)
        except Exception:  # noqa: BLE001 - 1 回の失敗で worker を止めない（#853）
            failures += 1
            delay = min(poll_seconds * (2.0 ** (failures - 1)), _MAX_ERROR_BACKOFF_SECONDS)
            logger.warning(
                "runtime_dispatcher_iteration_failed",
                extra={"worker_id": worker_id, "failures": failures, "delay_seconds": delay},
                exc_info=True,
            )
            await _sleep(delay)
            continue
        failures = 0
        if not claimed:
            await _sleep(poll_seconds)


async def _run_with_event_delivery() -> None:
    await start_trace_export_retry_worker()
    # 処理の経過の span の書き手（共通の span の表。#3226 の案 A。worker の起動の後に始める）。
    await asyncio.to_thread(trace_spans.start)
    try:
        await run_forever()
    finally:
        await asyncio.to_thread(trace_spans.stop)
        await stop_trace_export_retry_worker()


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
            component="runtime_dispatcher",
        )
        asyncio.run(_run_with_event_delivery())


if __name__ == "__main__":
    main()
