"""実行環境が任意に注入する観測。要求・応答は制限付きの保存先へ渡し、telemetry の属性へ入れない。

工程の観測（``observe_call("stage", 工程名)``）は、工程の表示用のデータ（``display``: 種類・名前の値・開いて見る
詳細）も受け取る。実行環境は工程の span に付けて画面の処理の経過にする（#3226）。詳細には工程の記録の見出しと値・行
（``{"fields": [{"label", "value"}], "lines": [...]}``）だけを渡し、回答の本文・文書の本文は渡さない。
"""

from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from typing import Protocol


class CallObservation(Protocol):
    def request(self, value: object, *, schema: object = None) -> None: ...

    def response(self, value: object) -> None: ...

    def usage(self, value: object) -> None: ...

    def display(
        self,
        kind: str | None = None,
        params: Mapping[str, object] | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        """工程の表示用のデータを足す（``params`` は前の値に重ねる）。"""
        ...


class _NoObservation:
    def request(self, value: object, *, schema: object = None) -> None:
        pass

    def response(self, value: object) -> None:
        pass

    def usage(self, value: object) -> None:
        pass

    def display(
        self,
        kind: str | None = None,
        params: Mapping[str, object] | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        pass


Observer = Callable[[str, str], AbstractContextManager[CallObservation]]
_OBSERVER: ContextVar[Observer | None] = ContextVar("engine_trace_observer", default=None)


@contextmanager
def bind_trace_observer(observer: Observer) -> Iterator[None]:
    token = _OBSERVER.set(observer)
    try:
        yield
    finally:
        _OBSERVER.reset(token)


@contextmanager
def observe_call(operation: str, model: str) -> Iterator[CallObservation]:
    observer = _OBSERVER.get()
    if observer is None:
        yield _NoObservation()
    else:
        with observer(operation, model) as observation:
            yield observation
