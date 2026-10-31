"""呼び出し元（業務 Agent）のデータの範囲を MCP で強制する（#1379）。

Agent は業務 Agent の定義でデータの範囲（使える検索・回答プロファイル）を設定したとき、サービス
トークンの任意の claim `profile_ids` にその ID を入れる（範囲なしの Agent は claim を付けない）。
RAG は Agent の Runtime の強制とは別に、呼び先でも同じ範囲を強制する（多層の防御）。

- 使える検索・回答プロファイルは「利用者の権限 ∩ claim」。使えるナレッジベースは「利用者の権限 ∩
  範囲のプロファイルの参照先」。ツールはこの 2 つに絞った監査 context（`AuditRequestContext`）の中で
  実行するので、一覧・検索・文書を読むツール（`rag_read_source` / `rag_outline` /
  `rag_read_document`）・回答の検証は、画面と同じ判定のまま範囲の中だけを見る。
- 検索（`rag_search` / `rag_retrieve_evidence`）は `search_answer_profile_id` が要る
  （`knowledge_base_ids` だけの検索はしない）。範囲の外のプロファイルと、指定したプロファイルの
  参照先の外の `knowledge_base_ids` は 403（`PROFILE_SCOPE_FORBIDDEN`）。
- claim の無い呼び出し（範囲なしの Agent・local）は今までどおり（何も絞らない）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from types import MappingProxyType

from fastapi import HTTPException, Request
from pr_system_settings.auth.dependencies import service_token_profile_scope
from pr_system_settings.auth.errors import SecurityApiError
from pr_system_settings.auth.service_token import (
    PROFILE_SCOPE_FORBIDDEN_CODE,
    narrow_to_profile_scope,
)

from app.clients.oracle import OracleClient
from app.rag.request_context import (
    current_audit_request_context,
    reset_audit_request_context,
    set_audit_request_context,
)

PROFILE_SCOPE_UNAVAILABLE_MESSAGE = (
    "この呼び出し元のデータの範囲（検索・回答プロファイル）を読み込めませんでした。"
    "時間をおいて再度お試しください。"
)


@dataclass(frozen=True)
class ProfileScope:
    """1 回の MCP の呼び出しのデータの範囲。

    `profile_ids` は claim の ID（利用者の権限とは積を取る前）。`knowledge_base_ids` は、利用者が
    使える範囲のプロファイルごとの参照先のナレッジベース（アーカイブ済みのプロファイルは含めない）。
    作った後は入れ子まで書き換えられない（#3253）。
    """

    profile_ids: frozenset[str]
    knowledge_base_ids: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_ids", frozenset(self.profile_ids))
        object.__setattr__(
            self,
            "knowledge_base_ids",
            MappingProxyType(
                {profile_id: tuple(ids) for profile_id, ids in self.knowledge_base_ids.items()}
            ),
        )

    def all_knowledge_base_ids(self) -> frozenset[str]:
        return frozenset(item for ids in self.knowledge_base_ids.values() for item in ids)


def profile_scope_forbidden(message: str) -> SecurityApiError:
    return SecurityApiError(403, message, code=PROFILE_SCOPE_FORBIDDEN_CODE)


def _out_of_scope(search_answer_profile_id: str) -> SecurityApiError:
    return profile_scope_forbidden(
        f"検索・回答プロファイル「{search_answer_profile_id}」は、この呼び出し元（業務 Agent）の"
        "データの範囲の外です。rag_list_search_answer_profiles で使える検索・回答プロファイルを"
        "確かめてください。"
    )


async def resolve_profile_scope(request: Request) -> ProfileScope | None:
    """サービストークンの claim から範囲を作る。claim が無ければ None（範囲なし）。"""
    claimed = service_token_profile_scope(request)
    if claimed is None:
        return None
    context = current_audit_request_context()
    visible: frozenset[str] = (
        narrow_to_profile_scope(context.allowed_search_answer_profile_ids, claimed) or frozenset()
    )
    knowledge_base_ids: dict[str, tuple[str, ...]] = {}
    # 利用者の権限と claim の積の範囲でプロファイルを読む（範囲外は Oracle の SQL が返さない）。
    token = set_audit_request_context(replace(context, allowed_search_answer_profile_ids=visible))
    try:
        oracle = OracleClient()
        for profile_id in sorted(visible):
            view = await oracle.get_search_answer_profile(profile_id)
            if view is None or getattr(view.status, "value", view.status) == "ARCHIVED":
                continue
            knowledge_base_ids[profile_id] = tuple(view.config.normalized_knowledge_base_ids())
    except (HTTPException, SecurityApiError):
        raise
    except Exception as exc:  # noqa: BLE001 - 範囲を読めないときは範囲なしで通さない（fail-closed）
        raise HTTPException(status_code=503, detail=PROFILE_SCOPE_UNAVAILABLE_MESSAGE) from exc
    finally:
        reset_audit_request_context(token)
    return ProfileScope(profile_ids=claimed, knowledge_base_ids=knowledge_base_ids)


def check_profile(scope: ProfileScope | None, search_answer_profile_id: str | None) -> None:
    """プロファイルを使う呼び出しの判定（範囲が無ければ何もしない）。"""
    if scope is None:
        return
    if not search_answer_profile_id:
        raise profile_scope_forbidden(
            "この呼び出し元（業務 Agent）はデータの範囲が決まっているため、"
            "search_answer_profile_id を指定してください"
            "（knowledge_base_ids だけでは検索できません）。"
        )
    if search_answer_profile_id not in scope.profile_ids:
        raise _out_of_scope(search_answer_profile_id)


def check_search(
    scope: ProfileScope | None,
    search_answer_profile_id: str | None,
    knowledge_base_ids: Iterable[str],
) -> None:
    """検索の判定: プロファイルが範囲の中で、明示した KB がそのプロファイルの参照先の中。"""
    if scope is None:
        return
    check_profile(scope, search_answer_profile_id)
    assert search_answer_profile_id is not None
    referenced = scope.knowledge_base_ids.get(search_answer_profile_id)
    requested = list(knowledge_base_ids)
    if not requested or referenced is None:
        # 利用者が使えないプロファイルは、今までどおり検索の判定（404）に任せる。
        return
    outside = [item for item in requested if item not in referenced]
    if outside:
        raise profile_scope_forbidden(
            "指定したナレッジベースは、検索・回答プロファイル"
            f"「{search_answer_profile_id}」の参照先の外です: {'、'.join(outside)}。"
            "knowledge_base_ids を省略すると、検索・回答プロファイルの参照先を検索します。"
        )


@contextmanager
def scoped_access(scope: ProfileScope | None) -> Iterator[None]:
    """監査 context の対象範囲を、範囲（プロファイル・参照先の KB）との積に絞る。"""
    if scope is None:
        yield
        return
    context = current_audit_request_context()
    token = set_audit_request_context(
        replace(
            context,
            allowed_search_answer_profile_ids=narrow_to_profile_scope(
                context.allowed_search_answer_profile_ids, scope.profile_ids
            ),
            allowed_knowledge_base_ids=narrow_to_profile_scope(
                context.allowed_knowledge_base_ids, scope.all_knowledge_base_ids()
            ),
        )
    )
    try:
        yield
    finally:
        reset_audit_request_context(token)


def within_profile_scope[A, R](
    request: Request,
    handler: Callable[[A], Awaitable[R]],
    check: Callable[[ProfileScope | None, A], None] | None = None,
) -> Callable[[A], Awaitable[R]]:
    """ツールの handler を、範囲の判定と絞った監査 context の中で実行する。"""

    async def wrapped(arguments: A) -> R:
        scope = await resolve_profile_scope(request)
        if check is not None:
            check(scope, arguments)
        with scoped_access(scope):
            return await handler(arguments)

    return wrapped
