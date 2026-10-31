"""ASGI event loop と同期 I/O の境界を明示する helper。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from starlette.concurrency import run_in_threadpool


async def run_sync_io[T](
    function: Callable[..., T],
    *args: Any,
    **kwargs: Any,
) -> T:
    """同期 Oracle / file / SDK / CLOB 処理を ASGI event loop の外で実行する。"""

    return await run_in_threadpool(function, *args, **kwargs)
