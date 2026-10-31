"""認証済み application actor を request / worker 境界で伝播する。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True)
class ActorContext:
    """DB 実行時に必要な application actor 情報。"""

    user_uuid: str = ""
    is_system_admin: bool = False


_ACTOR_CONTEXT: ContextVar[ActorContext | None] = ContextVar("nl2sql_actor_context", default=None)


def current_actor_context() -> ActorContext:
    return _ACTOR_CONTEXT.get() or ActorContext()


def current_actor_user_uuid() -> str:
    return current_actor_context().user_uuid


def current_actor_is_system_admin() -> bool:
    return current_actor_context().is_system_admin


def set_actor_context(
    user_uuid: str,
    *,
    is_system_admin: bool = False,
) -> Token[ActorContext | None]:
    return _ACTOR_CONTEXT.set(
        ActorContext(user_uuid=user_uuid.strip(), is_system_admin=bool(is_system_admin))
    )


def reset_actor_context(token: Token[ActorContext | None]) -> None:
    _ACTOR_CONTEXT.reset(token)


def set_actor_user_uuid(user_uuid: str) -> Token[ActorContext | None]:
    return set_actor_context(user_uuid)


def reset_actor_user_uuid(token: Token[ActorContext | None]) -> None:
    reset_actor_context(token)


@contextmanager
def actor_scope(user_uuid: str, *, is_system_admin: bool = False) -> Iterator[None]:
    token = set_actor_context(user_uuid, is_system_admin=is_system_admin)
    try:
        yield
    finally:
        reset_actor_context(token)
