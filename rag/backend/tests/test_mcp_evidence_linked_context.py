"""MCP の根拠の並びで、当たった chunk の章の参照の先・前後の文脈を evidence_limit の内に確保する
（#1390）。評価の検索の結果（citations）の並びの形を、決定論の根拠の列で確かめる。
"""

from __future__ import annotations

import json
from typing import Any

from app.mcp.tools import (
    LINKED_CONTEXT_LIMIT_SHARE,
    mcp_evidence_order,
)
from app.rag.cross_references import REFERENCE_FROM_KEY, REFERENCE_LABEL_KEY, REFERENCE_RANK_KEY
from app.schemas.search import RetrievedChunk

MAINTENANCE = "maintenance-plan"
OPERATIONS = "operations-guide"
INCIDENT = "incident-rules"


def _chunk(
    chunk_id: str,
    role: str,
    *,
    document: str,
    index: int,
    rank: int | None = None,
    rerank: float | None = None,
    section: str = "",
    targets: list[str] | None = None,
    group: str | None = None,
) -> RetrievedChunk:
    metadata: dict[str, Any] = {
        "evidence_role": role,
        "chunk_index": index,
        "chunk_set_id": f"{document}-set",
        "chunk_group_id": group or f"{document}-p1",
        "section_path": section,
    }
    if rank is not None:
        metadata["evidence_retrieval_rank"] = rank
    if targets is not None:
        metadata["reference_targets_json"] = json.dumps(
            [
                {"label": path.rsplit(" > ", 1)[-1][:4], "kind": "chapter", "key": "x"}
                | {"document_title": None, "section_path": path}
                for path in targets
            ],
            ensure_ascii=False,
        )
    return RetrievedChunk(
        document_id=document,
        chunk_id=chunk_id,
        text=chunk_id,
        score=0.0,
        rerank_score=rerank,
        metadata=metadata,
    )


def _ids(chunks: list[RetrievedChunk]) -> list[str]:
    return [chunk.chunk_id for chunk in chunks]


def _room_booking_citations() -> list[RetrievedChunk]:
    """評価の ``ir-room-booking-maintenance``（「会議室予約システム 定期保守」）の検索の結果の形。

    当たった上位は運用要領の第 7 章（会議室予約システム）と定期保守計画の第 1 章（個別の
    保守枠。「この章に無いシステムは、第 2 章の共通の保守枠で保守します」）。答えの共通の
    保守枠（第 2 章）は第 1 章と同じ親の前後の文脈で、citations では運用要領の多くの章の
    後ろにある。
    """
    plan = "定期保守計画 2026年度"
    ops = [_chunk("ops-ch7", "retrieved_anchor", document=OPERATIONS, index=7, rank=1, rerank=0.92)]
    maintenance_hits = [
        _chunk(
            "mt-ch1",
            "retrieved_anchor",
            document=MAINTENANCE,
            index=1,
            rank=2,
            rerank=0.76,
            section=f"{plan} > 第 1 章 個別の保守枠",
            targets=[f"{plan} > 第 2 章 共通の保守枠"],
        ),
        _chunk("mt-title", "retrieved_anchor", document=MAINTENANCE, index=0, rank=3, section=plan),
    ]
    other_hits = [
        _chunk(
            f"ops-hit-{rank}", "retrieved_anchor", document=OPERATIONS, index=rank + 20, rank=rank
        )
        for rank in range(4, 39)
    ]
    ops_context = [
        _chunk(f"ops-ch{index}", "neighbor_context", document=OPERATIONS, index=index)
        for index in [*range(0, 7), *range(8, 20)]
    ]
    maintenance_context = [
        _chunk(
            "mt-ch2-common",
            "neighbor_context",
            document=MAINTENANCE,
            index=2,
            section=f"{plan} > 第 2 章 共通の保守枠",
        ),
        _chunk(
            "mt-ch3-change",
            "neighbor_context",
            document=MAINTENANCE,
            index=3,
            section=f"{plan} > 第 3 章 保守枠の変更",
        ),
    ]
    return [
        *ops,
        *ops_context,
        *maintenance_hits,
        *maintenance_context,
        *other_hits,
    ]


def test_room_booking_keeps_the_referenced_chapter_inside_the_limit() -> None:
    citations = _room_booking_citations()

    ordered = mcp_evidence_order(citations, 20)
    head = _ids(ordered[:20])

    # 第 1 章が参照する第 2 章（共通の保守枠）が上限の内に入る（修正前は 54 位）。
    assert "mt-ch2-common" in head
    # 当たった上位の chunk は動かない。
    assert head[:3] == ["ops-ch7", "mt-ch1", "mt-title"]
    # 章の参照の先を、すぐ前・すぐ後の chunk より先に確保する。
    linked = [
        cid for cid in head if cid in {"mt-ch2-common", "ops-ch6", "ops-ch8", "mt-ch3-change"}
    ]
    assert linked[0] == "mt-ch2-common"
    assert len(ordered) == len(citations)


def test_incoming_reference_and_previous_chunk_of_the_hit_are_reserved() -> None:
    """評価の ``ir-incident-phone-unreachable`` / ``ir-access-review`` の形: 当たった
    第 3 章を参照する第 1 章の文（「連絡の手段は第 3 章を参照してください」）と、当たった
    章のすぐ前の章を上限の内に入れる。
    """
    rules = "障害連絡規程"
    hits = [
        _chunk(
            "in-ch3-means",
            "retrieved_anchor",
            document=INCIDENT,
            index=3,
            rank=1,
            rerank=0.9,
            section=f"{rules} > 第 3 章 連絡の手段",
        ),
        *(
            _chunk(f"hit-{rank}", "retrieved_anchor", document=OPERATIONS, index=rank, rank=rank)
            for rank in range(2, 24)
        ),
    ]
    context = [
        _chunk(f"ops-ctx-{n}", "neighbor_context", document=OPERATIONS, index=100 + n)
        for n in range(6)
    ]
    citing = _chunk(
        "in-ch1-first-contact",
        "neighbor_context",
        document=INCIDENT,
        index=1,
        section=f"{rules} > 第 1 章 最初の連絡",
        targets=[f"{rules} > 第 3 章 連絡の手段"],
    )
    previous = _chunk(
        "in-ch2-recovery",
        "neighbor_context",
        document=INCIDENT,
        index=2,
        section=f"{rules} > 第 2 章 復旧の目標",
    )

    ordered = mcp_evidence_order([*hits, *context, previous, citing], 20)
    head = _ids(ordered[:20])

    assert head.index("in-ch1-first-contact") < head.index("in-ch2-recovery")
    assert head[0] == "in-ch3-means"
    # 確保した 2 件の分だけ、末尾の当たりが上限の後ろへ移る（順位の順で残る）。
    assert _ids(ordered[18:20]) == ["in-ch1-first-contact", "in-ch2-recovery"]
    assert _ids(ordered[20:23]) == ["hit-19", "hit-20", "hit-21"]


def test_linked_context_share_is_capped_and_other_documents_are_not_linked() -> None:
    hits = [
        _chunk(
            f"hit-{rank}",
            "retrieved_anchor",
            document=f"doc-{rank}",
            index=5,
            rank=rank,
        )
        for rank in range(1, 26)
    ]
    # 上位 5 件の当たりのすぐ前・すぐ後（10 件）と、別の文書の同じ番号の chunk。
    neighbors = [
        _chunk(f"near-{rank}-{index}", "neighbor_context", document=f"doc-{rank}", index=index)
        for rank in range(1, 6)
        for index in (4, 6)
    ]
    unrelated = _chunk("other-doc", "neighbor_context", document="doc-99", index=4)

    ordered = mcp_evidence_order([*hits, unrelated, *neighbors], 20)
    head = _ids(ordered[:20])

    reserved = [cid for cid in head if cid.startswith("near-")]
    # 上限 20 の 3 割 = 6 件まで。
    assert LINKED_CONTEXT_LIMIT_SHARE == 0.3
    # 起点の順（1 位のすぐ前・すぐ後から）。
    assert reserved == ["near-1-4", "near-1-6", "near-2-4", "near-2-6", "near-3-4", "near-3-6"]
    assert "other-doc" not in head


def _named(
    chunk_id: str,
    role: str,
    *,
    file_name: str,
    section: str,
    index: int,
    text: str = "",
    rank: int | None = None,
    reference_from: str | None = None,
    reference_rank: int = 1,
) -> RetrievedChunk:
    """文書名・見出しの列・本文を持つ根拠（文書名の参照の照合に使う。#1400）。"""
    document = file_name.rsplit(".", 1)[0]
    metadata: dict[str, Any] = {
        "evidence_role": role,
        "chunk_index": index,
        "chunk_set_id": f"{document}-set",
        "chunk_group_id": f"{document}-p1",
        "section_path": section,
    }
    if rank is not None:
        metadata["evidence_retrieval_rank"] = rank
    if reference_from is not None:
        metadata[REFERENCE_FROM_KEY] = reference_from
        metadata[REFERENCE_LABEL_KEY] = "定期保守計画"
        metadata[REFERENCE_RANK_KEY] = reference_rank
    return RetrievedChunk(
        document_id=document,
        chunk_id=chunk_id,
        text=text or chunk_id,
        score=0.0,
        file_name=file_name,
        metadata=metadata,
    )


def _sales_change_window_citations() -> list[RetrievedChunk]:
    """評価の ``br-sales-change-window``（販売管理システムの変更作業の時間帯）の検索の結果の形。

    当たった 3 位は変更手順書の第 4 章（「時間帯は定期保守計画で確かめます」）。回答の検索は、
    運用要領の第 1 章（前後の文脈で、当たった上位ではない）の文書名の参照から、定期保守計画の
    質問に関連の高い chunk（1 件目が第 2 章 共通の保守枠、2 件目が第 3 章）を足した。第 2 章は
    同じ親の前後の文脈、第 3 章は関連度の順位 47 位で、どちらも上限 20 の外にあった（#1400。
    修正前の第 2 章は 93 位）。
    """
    plan = "定期保守計画 2026年度"
    origin = "ops-ch1"
    window = (
        "重要度 A のシステムの変更は、定期保守の時間帯にだけ作業します。"
        "時間帯は定期保守計画で確かめま す。"
    )
    hits = [
        _named("ops-ch54", "retrieved_anchor", file_name="ops.pdf", section="54", index=54, rank=1),
        _named("ops-ch20", "retrieved_anchor", file_name="ops.pdf", section="20", index=20, rank=2),
        _named(
            "change-ch4",
            "retrieved_anchor",
            file_name="change-procedure.pdf",
            section="システム変更手順書 > 第 4 章 作業の時間帯",
            index=4,
            rank=3,
            text=window,
        ),
        *[
            _named(
                f"ops-hit-{rank}",
                "retrieved_anchor",
                file_name="ops.pdf",
                section=str(rank + 30),
                index=rank + 30,
                rank=rank,
            )
            for rank in range(4, 40)
        ],
    ]
    contexts = [
        _named(f"ops-ctx-{n}", "neighbor_context", file_name="ops.pdf", section="9", index=n + 100)
        for n in range(30)
    ]
    plan_chunks = [
        _named(
            "mt-ch1",
            "neighbor_context",
            file_name="maintenance-plan.pdf",
            section=f"{plan} > 第 1 章 個別の保守枠",
            index=1,
        ),
        _named(
            "mt-ch2-common",
            "neighbor_context",
            file_name="maintenance-plan.pdf",
            section=f"{plan} > 第 2 章 共通の保守枠",
            index=2,
            reference_from=origin,
        ),
        _named(
            "mt-ch3-change",
            "retrieved_anchor",
            file_name="maintenance-plan.pdf",
            section=f"{plan} > 第 3 章 保守枠の変更",
            index=3,
            rank=47,
            reference_from=origin,
            reference_rank=2,
        ),
    ]
    # 参照先として足したが、当たった chunk が文書名で参照しない文書（システム台帳の行）。
    ledger = _named(
        "ledger-row",
        "neighbor_context",
        file_name="system-ledger.xlsx",
        section="システム台帳",
        index=53,
        reference_from=origin,
    )
    return [*hits, *contexts, *plan_chunks, ledger]


def test_sales_change_window_reserves_the_document_named_by_a_hit() -> None:
    """当たった chunk が文書名で参照する文書の、参照先として足した chunk を上限の内に確保する。"""
    citations = _sales_change_window_citations()

    ordered = mcp_evidence_order(citations, 20)
    head = _ids(ordered[:20])

    assert "mt-ch2-common" in head
    # 参照先 1 つにつき最も関連の高い 1 件だけを確保する（2 件目の第 3 章は関連度の順位のまま）。
    assert "mt-ch3-change" not in head
    # 当たった上位の chunk は動かない。
    assert head[:3] == ["ops-ch54", "ops-ch20", "change-ch4"]
    # 参照先として足していない章（質問に関連の高い chunk として選ばれなかった）は確保しない。
    assert "mt-ch1" not in head
    # 当たった chunk が文書名で参照しない文書は、参照先として足した chunk でも確保しない。
    assert "ledger-row" not in head
    assert len(ordered) == len(citations)


def test_document_name_link_needs_the_name_in_the_hit() -> None:
    """当たった chunk が文書名を挙げなければ、参照先として足した別の文書の chunk も確保しない。"""
    plain = "重要度 A のシステムの変更は、保守の時間帯に作業します。"
    citations = [
        chunk.model_copy(update={"text": plain}) if chunk.chunk_id == "change-ch4" else chunk
        for chunk in _sales_change_window_citations()
    ]

    head = _ids(mcp_evidence_order(citations, 20)[:20])

    assert "mt-ch2-common" not in head


def test_second_document_name_target_of_a_hit_is_not_reserved() -> None:
    """当たった chunk 自身が起点の文書名の参照でも、参照先の 2 件目以降は確保しない（#1400）。

    評価の ``tl-first-tuesday-department`` で、当たった運用要領の第 1 章（「システム台帳で
    確かめます」）の参照先の 2 件目（質問と別のシステムの台帳の行）が上限の内に入り、当たった
    台帳の行（SYS-121。関連度の順位 33 位）を上限の外へ押し出した形。
    """
    origin = _named(
        "ops-ch1",
        "retrieved_anchor",
        file_name="ops.pdf",
        section="システム運用要領 > 第 1 章 この要領の使い方",
        index=1,
        rank=1,
        text="担当部署・重要度・機密区分はシステム台帳で確かめます。",
    )
    hits = [
        _named(
            f"hit-{rank}",
            "retrieved_anchor",
            file_name="ops.pdf",
            section="x",
            index=rank + 10,
            rank=rank,
        )
        for rank in range(2, 30)
    ]
    first, second = (
        _named(
            f"ledger-{rank}",
            "neighbor_context",
            file_name="system-ledger.xlsx",
            section="システム台帳",
            index=50 + rank,
            reference_from="ops-ch1",
            reference_rank=rank,
        )
        for rank in (1, 2)
    )

    head = _ids(mcp_evidence_order([origin, *hits, first, second], 20)[:20])

    assert "ledger-1" in head
    assert "ledger-2" not in head
