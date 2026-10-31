"""明示的なアプリ資源と、既存関数へ渡すリクエスト単位のスコープ。"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from rag_engine.resources.pool import ModelPool


@dataclass(frozen=True)
class ResourcePaths:
    """呼び出し元が所有する保存先。構築時にはファイルを作成しない。"""

    workspace: Path
    output: Path
    prompts: Path | None = None
    faq: Path | None = None
    evaluation: Path | None = None


@dataclass
class Runtime:
    """保存先・指示の上書きとモデルの寿命をアプリごとに分離する。"""

    paths: ResourcePaths
    prompt_overrides: tuple[tuple[str, str], ...] = ()
    model_pool: ModelPool | None = None

    @contextmanager
    def activate(self) -> Iterator["Runtime"]:
        """既存の同期関数へ資源を渡す。例外時も前のスコープを復元する。"""
        token = _current.set(self)
        try:
            yield self
        finally:
            _current.reset(token)

    def close(self) -> None:
        """このアプリが所有する常駐モデルを解放する。"""
        if self.model_pool is not None:
            self.model_pool.unload_all()


_current: ContextVar[Runtime | None] = ContextVar("rag_engine_runtime", default=None)


def current_runtime() -> Runtime | None:
    """有効なアプリ資源を返す。互換関数の直接呼び出し時は None。"""
    return _current.get()


def resolve_prompt(key: str, default: str) -> str:
    """有効な要求の指示を返す。保存値が無ければ組み込みの指示を使う。"""
    runtime = current_runtime()
    if runtime is None:
        return default
    return dict(runtime.prompt_overrides).get(key, default)


@contextmanager
def bind_prompt_overrides(overrides: dict[str, str]) -> Iterator[None]:
    """ファイルを作らず、親の資源と指示を引き継ぐ要求単位のスコープ。"""
    parent = current_runtime()
    paths = parent.paths if parent else ResourcePaths(workspace=Path.cwd(), output=Path.cwd())
    merged = {**dict(parent.prompt_overrides if parent else ()), **overrides}
    token = _prompt_snapshot.set(tuple(merged.items()))
    try:
        with Runtime(paths, tuple(merged.items()), parent.model_pool if parent else None).activate():
            yield
    finally:
        _prompt_snapshot.reset(token)


_prompt_snapshot: ContextVar[tuple[tuple[str, str], ...] | None] = ContextVar("rag_prompt_snapshot", default=None)


def saved_prompt_overrides() -> dict[str, str] | None:
    """要求の開始時に読んだ保存値。None はまだ読み込んでいない状態。"""
    snapshot = _prompt_snapshot.get()
    return dict(snapshot) if snapshot is not None else None
