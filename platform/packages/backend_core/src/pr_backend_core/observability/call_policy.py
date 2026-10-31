"""本文を記録するかの注入口。設定の保存先は呼び出し側が持つ。"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar


def _disabled() -> bool:
    return False


_provider: Callable[[], bool] = _disabled
_reading: ContextVar[bool] = ContextVar("trace_content_policy_reading", default=False)


def capture_call_content() -> bool:
    if _reading.get():
        return False
    token = _reading.set(True)
    try:
        return bool(_provider())
    except Exception:
        return False
    finally:
        _reading.reset(token)


@contextmanager
def call_content_policy(provider: Callable[[], bool]) -> Iterator[None]:
    global _provider
    previous, _provider = _provider, provider
    try:
        yield
    finally:
        _provider = previous
