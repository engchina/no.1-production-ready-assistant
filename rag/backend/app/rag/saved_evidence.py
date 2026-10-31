"""保存した根拠を出し直すときに、見る人の今の権限で確かめ直す（#3252）。

会話の回答の引用（チャットの会話の取得）・実行履歴の詳細・保存された回答の詳細は、回答を作った
ときの根拠を保存している。出すたびに、見る人の今の範囲（tenant・文書 / 分類 / ナレッジベース。
検索と同じ ``accessible_document_ids``）で文書を確かめ、今見られない文書の根拠は抜粋・場所・
文書名を返さず、件数（``withheld_evidence_count``）だけを返す（OWASP API1: 物を読むたびに
その時点の権限で認可する。権限を外したら次に読むときから効く）。

- 削除した文書の根拠も、今の範囲を確かめられないので同じく件数だけにする。
- 会話の添付の根拠（``document_id`` が ``attachment:``）は文書の表に無く、会話を見られる人の
  物なので確かめない（会話の見え方は会話の判定のまま）。
- 回答の本文は保存した文のまま返す（その資料から作った文で、本文から該当の部分は消せない。
  既知の限界。docs/rag-architecture.md「保存した根拠の再表示」）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from pr_backend_core.observability.call_details import withhold_call_details

from app.rag.chat_attachments import ATTACHMENT_DOCUMENT_PREFIX
from app.rag.request_context import current_audit_request_context
from app.schemas.search import RetrievedChunk


class _ReadableDocuments(Protocol):
    async def accessible_document_ids(self, document_ids: Sequence[str]) -> set[str]: ...


def _is_attachment(citation: RetrievedChunk) -> bool:
    return citation.document_id.startswith(ATTACHMENT_DOCUMENT_PREFIX)


async def readable_document_ids(
    oracle: _ReadableDocuments, citation_groups: Sequence[Sequence[RetrievedChunk]]
) -> set[str]:
    """引用の文書のうち、見る人が今見られる文書の ID（1 回の問い合わせで確かめる）。"""
    document_ids = sorted(
        {
            citation.document_id
            for citations in citation_groups
            for citation in citations
            if citation.document_id and not _is_attachment(citation)
        }
    )
    if not document_ids:
        return set()
    return await oracle.accessible_document_ids(document_ids)


def visible_citations(
    citations: Sequence[RetrievedChunk], readable: set[str]
) -> tuple[list[RetrievedChunk], int]:
    """見る人が今見られる引用と、見られない引用の数（並びは保存した順のまま）。"""
    kept = [
        citation
        for citation in citations
        if _is_attachment(citation) or citation.document_id in readable
    ]
    return kept, len(citations) - len(kept)


async def withhold_unreadable(
    oracle: _ReadableDocuments, citations: Sequence[RetrievedChunk]
) -> tuple[list[RetrievedChunk], int]:
    """1 件の回答の引用を確かめ直す（``readable_document_ids`` と ``visible_citations``）。"""
    readable = await readable_document_ids(oracle, [citations])
    return visible_citations(citations, readable)


def _document_id(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    document_id = value.get("document_id")
    if not isinstance(document_id, str) or not document_id:
        return None
    return None if document_id.startswith(ATTACHMENT_DOCUMENT_PREFIX) else document_id


def referenced_document_ids(value: object) -> set[str]:
    """記録（回答の診断など）の中の、文書を指す要素（``document_id`` を持つ dict）の文書の ID。"""
    found: set[str] = set()
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("capture_scope") == "generation_citation_trace":
                found.update(_capture_document_ids(node))
            document_id = _document_id(node)
            if document_id is not None:
                found.add(document_id)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def _capture_document_ids(node: dict[str, object]) -> set[str]:
    """生応答は複数の根拠を含むため、参照全体を一括して認可する。"""
    values = node.get("document_ids")
    if not isinstance(values, list):
        return set()
    return {
        value
        for value in values
        if isinstance(value, str) and value and not value.startswith(ATTACHMENT_DOCUMENT_PREFIX)
    }


def prune_unreadable(value: object, readable: set[str]) -> object:
    """記録の中の、見る人が今見られない文書を指す要素を除く（一覧の要素は除き、項目は消す）。

    回答の診断の「読んでいない節」（文書へのリンク）などに、見られない文書の場所・名前を残さない。
    """

    def unreadable(node: object) -> bool:
        if isinstance(node, dict) and node.get("capture_scope") == "generation_citation_trace":
            return (
                node.get("document_scope_status") != "complete"
                or not _capture_document_ids(node) <= readable
            )
        document_id = _document_id(node)
        return document_id is not None and document_id not in readable

    if isinstance(value, dict):
        if value.get("capture_scope") == "generation_citation_trace" and unreadable(value):
            return {"capture_scope": "generation_citation_trace", "status": "withheld"}
        return {
            key: prune_unreadable(item, readable)
            for key, item in value.items()
            if not unreadable(item)
        }
    if isinstance(value, list):
        return [prune_unreadable(item, readable) for item in value if not unreadable(item)]
    return value


async def withhold_saved_answer(
    oracle: _ReadableDocuments,
    citations: Sequence[RetrievedChunk],
    diagnostics: object,
) -> tuple[list[RetrievedChunk], int, object]:
    """保存した回答の引用と診断を、見る人の今の権限で確かめ直す（1 回の問い合わせ）。"""
    document_ids = {
        citation.document_id for citation in citations if not _is_attachment(citation)
    } | referenced_document_ids(diagnostics)
    readable = await oracle.accessible_document_ids(sorted(document_ids)) if document_ids else set()
    kept, withheld = visible_citations(citations, readable)
    return (
        kept,
        withheld,
        withhold_call_details(
            prune_unreadable(diagnostics, readable),
            allow_restricted=current_audit_request_context().all_users_visible,
        ),
    )
