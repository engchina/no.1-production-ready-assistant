"""CRAG の評価済み起点から、既取得 pool の文脈を限定的に回復する。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from rag_engine.retrieval.evidence_cues import evidence_cues
from rag_engine.retrieval.operation_context import operation_labels


@dataclass(frozen=True)
class ContextRecovery:
    """採用した追加根拠と、監査可能な起点・停止理由。"""

    records: tuple[Any, ...] = ()
    anchor_ids: tuple[str, ...] = ()
    reason: str = ""


def _uid(record: Any) -> str:
    return str(getattr(record, "chunk_uid", "") or "")


def _scope(record: Any) -> tuple[str, ...]:
    """局所IDの偶然の一致で文書やchunk版を跨がせない。"""
    return (str(record.source), str(record.source_run_id), str(record.engine), _uid(record).partition(":")[0])


def _unit(record: Any) -> tuple[str, ...]:
    """機能ユニット（section_path の番号付き見出しまで）。番号見出しがなければ空。"""
    from rag_engine.chunking import _section_unit

    return _section_unit((getattr(record, "metadata", {}) or {}).get("section_path") or [])


def _compatible(anchor: Any, candidate: Any) -> bool:
    labels = operation_labels(anchor)
    # 「開始日」「操作説明」だけの一致は機能の同一性を証明しない。
    identified = len(labels) == 1 and bool(
        re.search(rf"[{evidence_cues().chars('function_number_dashes')}]\([0-9A-Z]+\).+|\([A-Z]{1, 3}[0-9]{6, 8}\)", next(iter(labels)))
    )
    return (
        identified
        and bool(_uid(anchor))
        and bool(anchor.source)
        and _scope(anchor) == _scope(candidate)
        and len(labels) == 1
        and labels == operation_labels(candidate)
    )


def recover_definition_records(
    question: str, visible: Sequence[Any], pool: Sequence[Any], *, max_records: int = 6, max_chars: int = 24000
) -> ContextRecovery:
    """既取得poolの同一機能から明示項目の定義原文を回復する。

    物理的な一つ隣だけでは図表分割により定義へ届かないため、同じ文書・版・
    機能内を照合する。質問ID・ページ・業務名による特例は使わず、既に見えている
    本文と重複する定義は追加しない。外部I/Oは行わない。
    """
    from rag_engine.retrieval.definition_evidence import definition_labels, definition_ranges

    labels = definition_labels(question)
    if not labels:
        return ContextRecovery(reason="not_a_definition_request")
    selected, anchors, used = [], set(), 0
    for candidate in sorted(pool, key=lambda r: (r.chunk_level != "child", len(r.text), _uid(r))):
        definitions = definition_ranges(candidate.text, labels)
        if not definitions:
            continue
        compatible = [r for r in visible if _compatible(r, candidate)]
        body = candidate.text.strip()
        if (
            not compatible
            or not body
            or any(_scope(candidate) == _scope(r) and body in r.text for r in (*visible, *selected))
            or len(selected) >= max_records
            or used + len(body) > max_chars
        ):
            continue
        if all(any(_scope(candidate) == _scope(r) and d["text"] in r.text for r in (*visible, *selected)) for d in definitions):
            continue
        selected.append(candidate)
        anchors.add(_uid(compatible[0]))
        used += len(body)
    return ContextRecovery(tuple(selected), tuple(sorted(anchors)), "definition_recovered" if selected else "no_new_compatible_definition")


def recover_context_records(
    visible: Sequence[Any],
    pool: Sequence[Any],
    anchor_ids: Sequence[str],
    mode: str,
    *,
    max_records: int = 6,
    max_chars: int = 24000,
) -> ContextRecovery:
    """閲覧済みの一意IDを起点に child/parent/parent近傍を取得する。

    I/O や再解析は行わない。文書・版・機能が曖昧な候補は採用せず、全文を
    件数・文字数予算内で返す。本文に既に含まれる child は新規根拠と数えない。

    same_function は、隣接に限らず同じ文書・版・番号付き機能に属する parent を起点に近い順で
    返す。図や表の parent が間に入り、同じ機能の説明が1つ隣に無い場合のための最後の手段で、
    機能見出しが一意に識別できる場合（_compatible）だけ働く。

    same_unit は、起点と同じ機能ユニット（section_path の番号付き見出しまで。#660 で親はこの単位を
    またがない）の parent を起点に近い順で返す (#666)。1 機能が複数の親に割れたとき、検索が選ばなかった
    残りの手順を根拠に補うための兜底で、同じ文書・版・chunk 版とユニットの一致だけを条件にする。
    """
    if mode not in {"neighbor_children", "parent", "neighbor_parents", "same_function", "same_unit"}:
        return ContextRecovery(reason="no_context_recovery_requested")
    anchors = []
    for value in dict.fromkeys(anchor_ids):
        matches = [r for r in visible if value and value == _uid(r)]
        identities = {(_scope(r), str(r.text), tuple(sorted(operation_labels(r)))) for r in matches}
        if len(identities) == 1:
            anchors.append(matches[0])
    if not anchors:
        return ContextRecovery(reason="no_valid_visible_anchor")
    selected: dict[str, Any] = {}
    used = 0
    for anchor in anchors[:3]:
        peers = sorted(
            (r for r in pool if _scope(r) == _scope(anchor) and r.chunk_level == ("child" if mode == "neighbor_children" else "parent")),
            key=lambda r: (r.chunk_seq, _uid(r)),
        )
        parent_uid = anchor.parent_chunk_uid if anchor.chunk_level == "child" else _uid(anchor)
        if mode == "neighbor_children":
            positions = [
                i
                for i, r in enumerate(peers)
                if _uid(r) == _uid(anchor) or (anchor.chunk_level == "parent" and r.parent_chunk_uid == _uid(anchor))
            ]
        else:
            positions = [i for i, r in enumerate(peers) if _uid(r) == parent_uid]
        candidates = []
        if mode == "same_function":
            origin = positions[0] if positions else 0
            candidates = sorted(peers, key=lambda r: (abs(peers.index(r) - origin), peers.index(r)))
            positions = []
        elif mode == "same_unit":
            origin = positions[0] if positions else 0
            unit = _unit(anchor)
            candidates = sorted(
                (r for r in peers if unit and _unit(r) == unit), key=lambda r: (abs(peers.index(r) - origin), peers.index(r))
            )
            positions = []
        for position in positions:
            # 途中の別機能を飛び越えて似た見出しへ連結しない。
            offsets = (0,) if mode == "parent" else (0, -1, 1)
            for offset in offsets:
                index = position + offset
                if 0 <= index < len(peers):
                    candidates.append(peers[index])
        for candidate in candidates:
            uid = _uid(candidate)
            compatible = _scope(anchor) == _scope(candidate) if mode == "same_unit" else _compatible(anchor, candidate)
            if not uid or uid in selected or not compatible:
                continue
            body = str(candidate.text or "").strip()
            if not body or any(_scope(candidate) == _scope(r) and body in str(r.text) for r in (*visible, *selected.values())):
                continue
            if len(selected) >= max_records or used + len(body) > max_chars:
                continue
            selected[uid] = candidate
            used += len(body)
    return ContextRecovery(
        tuple(selected.values()), tuple(_uid(r) for r in anchors[:3]), "expanded" if selected else "no_new_compatible_evidence"
    )
