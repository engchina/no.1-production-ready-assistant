"""交差参照で足した根拠（#1280・#1400）を、rerank の後に参照元の直後へ置く規則（#1549）。

backend は回答の検索で、上位の候補が本文で参照する節・文書の chunk を参照元の直後に足し、その metadata の
``reference_expansion`` に参照元（``from_chunk_uid``）を付ける。参照先は参照元を経てたどる橋渡しの根拠で、
質問の語と重ならないことが多い（「時間帯は定期保守計画で確かめます」→ 定期保守計画の「共通の保守枠は、毎月
最終金曜日…」）。質問で rerank すると参照先は参照元よりずっと下になり、context の起点（上位 top_k 件）に
入らなかった（#1549 の kb1517 の記録: 参照元は rerank の 1 位、参照先は 28 位と 107 位）。

rag_engine は rerank の後、参照元より下にある参照先を参照元の直後へ移し（参照先の中は backend が付けた関連の
順）、文書の選択でも後回しにしない。参照元が下位なら参照先も下位のまま（参照元が context に入るときだけ
参照先も入る）。参照は 1 段で、参照先を足す数の上限は backend が決める（``rag_reference_expansion_max_chunks``）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

# backend が参照先の chunk の metadata に付ける印のキー（``app.rag.answer_engine._stored_child``）。
REFERENCE_EXPANSION_KEY = "reference_expansion"


def reference_source_uid(record: Any) -> str:
    """参照先として足した根拠の参照元の chunk_uid。印が無ければ空文字。"""
    metadata = getattr(record, "metadata", None)
    marker = metadata.get(REFERENCE_EXPANSION_KEY) if isinstance(metadata, dict) else None
    return str(marker.get("from_chunk_uid") or "") if isinstance(marker, dict) else ""


def is_followed_reference(record: Any) -> bool:
    """rerank の後に参照元の直後へ移した参照先の根拠か（文書の選択で後回しにしない。#1549）。"""
    metadata = getattr(record, "metadata", None)
    marker = metadata.get(REFERENCE_EXPANSION_KEY) if isinstance(metadata, dict) else None
    return isinstance(marker, dict) and marker.get("followed") is True


def _reference_rank(record: Any) -> int:
    marker = record.metadata.get(REFERENCE_EXPANSION_KEY)
    rank = marker.get("rank") if isinstance(marker, dict) else None
    return rank if isinstance(rank, int) and not isinstance(rank, bool) and rank > 0 else 1_000


def follow_reference_sources(records: Sequence[Any], step: Any | None = None) -> list[Any]:
    """参照元より下にある参照先の根拠を、参照元の直後へ移す（#1549）。ほかの候補の順は変えない。"""
    positions = {str(getattr(record, "chunk_uid", "") or ""): index for index, record in enumerate(records)}
    followers: dict[str, list[Any]] = {}
    for index, record in enumerate(records):
        source = reference_source_uid(record)
        if source and source in positions and positions[source] < index:
            followers.setdefault(source, []).append(record)
    if not followers:
        return list(records)
    moved = {id(record) for items in followers.values() for record in items}
    result: list[Any] = []
    emitted: set[int] = set()

    def emit(record: Any) -> None:
        if id(record) in emitted:
            return
        emitted.add(id(record))
        result.append(record)
        uid = str(getattr(record, "chunk_uid", "") or "")
        for follower in sorted(followers.get(uid, ()), key=_reference_rank):
            marker = dict(follower.metadata.get(REFERENCE_EXPANSION_KEY) or {})
            emit(replace(follower, metadata={**follower.metadata, REFERENCE_EXPANSION_KEY: {**marker, "followed": True}}))
            emitted.add(id(follower))

    for record in records:
        if id(record) not in moved:
            emit(record)
    if step is not None:
        step.add(f"参照先の根拠 {len(moved)} 件を参照元の直後へ置きます")
    return result


__all__ = [
    "REFERENCE_EXPANSION_KEY",
    "follow_reference_sources",
    "is_followed_reference",
    "reference_source_uid",
]
