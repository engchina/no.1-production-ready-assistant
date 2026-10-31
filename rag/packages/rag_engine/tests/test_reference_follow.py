"""交差参照で足した参照先の根拠（#1280・#1400）を、rerank の後に参照元の直後へ置く（#1549）。

backend は参照先の chunk の metadata の ``reference_expansion`` に参照元（``from_chunk_uid``）を付ける。参照先は
質問の語と重ならない橋渡しの根拠で、質問で rerank すると参照元よりずっと下に並び、context の起点に入らなかった
（kb1517 の br-budget-change-window: 参照元の変更手順書は 1 位、定期保守計画の共通の保守枠は 107 位）。
"""

from dataclasses import replace
from types import SimpleNamespace

import rag_engine.generation.answering as answering
from rag_engine.config import get_settings
from rag_engine.generation.answering import AnswerRecord, rerank_records, select_documents
from rag_engine.retrieval.reference_follow import (
    follow_reference_sources,
    is_followed_reference,
    reference_source_uid,
)


def _child(name, *, source="規程.pdf", ref_from=None, rank=None, score=None):
    metadata = {"section_path": ["第 1 章"]}
    if ref_from is not None:
        metadata["reference_expansion"] = {
            "from_chunk_uid": f"uid:{ref_from}",
            "label": "定期保守計画",
            **({"rank": rank} if rank is not None else {}),
        }
    if score is not None:
        metadata["rerank"] = {"relevance_score": score}
    return AnswerRecord(
        id=name,
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=1,
        category="Chunk",
        text=f"{name} の本文",
        source=source,
        chunk_level="child",
        chunk_id=name,
        chunk_uid=f"uid:{name}",
        parent_chunk_uid=f"uid:{name}-p",
        source_run_id="run",
        metadata=metadata,
    )


def _ids(records):
    return [record.id for record in records]


def test_follow_moves_references_after_source_in_reference_rank_order():
    records = [
        _child("c"),
        _child("window"),
        _child("plan2", ref_from="window", rank=2),
        _child("a"),
        _child("b"),
        _child("plan3", ref_from="window", rank=1),
    ]

    followed = follow_reference_sources(records)

    # 参照元より下の参照先を、参照元の直後へ参照先の中の関連の順（backend の rank）に移す。ほかの順は保つ。
    assert _ids(followed) == ["c", "window", "plan3", "plan2", "a", "b"]
    assert [is_followed_reference(record) for record in followed] == [False, False, True, True, False, False]
    assert reference_source_uid(followed[2]) == "uid:window"


def test_follow_keeps_order_when_reference_is_above_source_or_source_missing():
    above = [_child("plan", ref_from="window"), _child("a"), _child("window")]
    orphan = [_child("a"), _child("plan", ref_from="gone"), _child("b")]

    assert _ids(follow_reference_sources(above)) == ["plan", "a", "window"]
    assert _ids(follow_reference_sources(orphan)) == ["a", "plan", "b"]
    assert not any(is_followed_reference(record) for record in follow_reference_sources(above))


def test_rerank_places_reference_after_its_source(monkeypatch):
    """質問の rerank で参照先が最下位になっても、参照元（1 位）の直後に置く。"""
    monkeypatch.setattr(answering, "_rerank_configured", lambda settings: True)
    scores = {"window": 0.67, "a": 0.63, "b": 0.6, "c": 0.5, "plan2": 0.29}

    def fake(question, documents, settings, top_n=None):
        order = sorted(range(len(documents)), key=lambda index: -scores[documents[index].split(" の本文")[0].rsplit("\n", 1)[-1]])
        return [
            SimpleNamespace(index=index, relevance_score=scores[documents[index].split(" の本文")[0].rsplit("\n", 1)[-1]])
            for index in order
        ]

    monkeypatch.setattr(answering, "rerank_text_with_scores", fake)
    settings = SimpleNamespace(rerank_model="rerank-test", rerank_enabled=True, document_selection_enabled=False)
    records = [_child("a"), _child("window"), _child("plan2", ref_from="window", rank=1), _child("b"), _child("c")]

    ranked = rerank_records("質問", records, settings, enabled=True, business_domains=(), screen_terms=())

    assert _ids(ranked) == ["window", "plan2", "a", "b", "c"]
    assert ranked[1].metadata["rerank"]["relevance_score"] == 0.29


def test_document_selection_does_not_defer_followed_reference():
    settings = replace(
        get_settings(environ={}, dotenv_path=None),
        document_selection_enabled=True,
        document_selection_score_ratio=0.5,
    )
    children = follow_reference_sources(
        [
            _child("window", source="change.pdf", score=0.9),
            _child("a", source="change.pdf", score=0.8),
            _child("c1", source="c.pdf", score=0.1),
            _child("plan2", source="plan.pdf", ref_from="window", score=0.1),
        ]
    )

    kept, deferred, _ = select_documents(children, settings, top_k=2)

    assert _ids(kept) == ["window", "plan2", "a"]
    assert _ids(deferred) == ["c1"]


def _parent(name):
    return AnswerRecord(
        id=f"{name}-p",
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=1,
        category="Chunk",
        text=f"{name} の親",
        source="規程.pdf",
        chunk_level="parent",
        chunk_id=f"{name}-p",
        chunk_uid=f"uid:{name}-p",
        source_run_id="run",
        metadata={"section_path": ["第 1 章"]},
    )


def test_context_reserves_parent_slots_for_followed_reference():
    """参照先の親は分数が低く、親を分数の順に切ると枠の外に落ちる。参照元が枠の内なら確保する（#1549）。

    kb1517 の br-budget-change-window: 定期保守計画が章ごとの親に分かれ、第 1 章（「この章に無いシステムは、
    第 2 章の共通の保守枠で保守します」）の親が 12 枠の外に落ちた。
    """
    from rag_engine.retrieval.context_builder import ContextBuildRequest, build_chunk_context_bundle

    hits = [_child(f"a{index}", score=0.6 - index / 100) for index in range(4)]
    window = _child("window", score=0.7)
    plan = [_child("plan2", ref_from="window", rank=1, score=0.29), _child("plan1", ref_from="window", rank=2, score=0.1)]
    orphan = _child("orphan", ref_from="gone", score=0.05)
    records = follow_reference_sources([window, *hits, *plan, orphan])
    parents = [_parent(record.id) for record in records]

    bundle = build_chunk_context_bundle(
        ContextBuildRequest(
            question="質問",
            ranked_children=records,
            active_records=[*records, *parents],
            top_k=10,
            neighbor_child_count=0,
            max_records=4,
            max_chars=5000,
        )
    )

    ids = [record.id for record in bundle.records]
    # 4 枠のうち半分（2）までを参照先の親に入れ替える。参照元の無い印（orphan）は確保しない。
    assert set(ids) == {"window-p", "a0-p", "plan2-p", "plan1-p"}
    assert ids[0] == "window-p"
