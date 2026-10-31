"""モデルを呼ぶ処理が明示した用途。本文や時系列からは推定しない。"""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

MODEL_CALL_PURPOSES = frozenset(
    {
        "guide.draft",
        "answer.validate",
        "answer.recheck",
        "answer.premise_consistency",
        "sql.structure",
        "sql.logical_structure",
        "answer.generate",
        "answer.audit",
        "answer.repair",
        "answer.scope",
        "answer.resolve_conflicts",
        "evidence.grade",
        "evidence.select_screen",
        "evidence.extract",
        "query.expand",
        "query.route",
        "query.rewrite",
        "query.conditions",
        "query.embed",
        "document.embed",
        "sql.generate",
        "sql.repair",
        "sql.analyze",
        "sql.describe",
        "sql.reverse",
        "schema.comments",
        "schema.relations",
        "ontology.build",
        "ontology.review",
        "requirement.clarify",
        "evaluation.judge",
        "document.read",
        "document.extract",
        "document.summarize",
        "document.infer_headings",
        "context.compact",
        "agent.choose_action",
        "connection.check",
    }
)
_MODEL_PURPOSE: ContextVar[str | None] = ContextVar("model_call_purpose", default=None)


@contextmanager
def model_call_purpose(purpose: str | None) -> Iterator[None]:
    """固定の操作種別だけを記録し、入れ子・別タスク・例外の後で元に戻す。"""
    value = (
        _MODEL_PURPOSE.get()
        if purpose is None
        else purpose
        if purpose in MODEL_CALL_PURPOSES
        else None
    )
    token = _MODEL_PURPOSE.set(value)
    try:
        yield
    finally:
        _MODEL_PURPOSE.reset(token)


def model_purpose_attributes(*, default: str | None = None) -> dict[str, str]:
    purpose = _MODEL_PURPOSE.get() or (default if default in MODEL_CALL_PURPOSES else None)
    return {"pr.gen_ai.purpose": purpose} if purpose else {}


def call_with_model_purpose[**P, T](
    purpose: str, operation: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
) -> T:
    """同期の呼出側が用途を明示する。provider の引数・応答・再試行は変えない。"""
    with model_call_purpose(purpose):
        return operation(*args, **kwargs)


async def async_call_with_model_purpose[**P, T](
    purpose: str, operation: Callable[P, Awaitable[T]], /, *args: P.args, **kwargs: P.kwargs
) -> T:
    """非同期の呼出しが終わるまで用途を保ち、別タスクには漏らさない。"""
    with model_call_purpose(purpose):
        return await operation(*args, **kwargs)
