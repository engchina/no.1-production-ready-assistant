"""実体の 1 段の拡張で足した根拠（#1362）の扱い。

backend が metadata の ``entity_expansion`` に理由を付けた根拠（台帳の行・組織規程の略号の表など、質問の語と
重ならない橋渡しの根拠）は、rerank にかけたうえで、関連度が下限（``ENTITY_EXPANSION_MIN_RELEVANCE``）以上なら
rerank で位置を変えず、文書の選択で後回しにせず、context の親の枠を確保する。下限未満のもの（質問と関係の薄い
属性の chunk）は印の無い根拠と同じに扱う（#1390）。印の無い根拠は今までと同じ。
"""

from dataclasses import replace
from types import SimpleNamespace

import rag_engine.generation.answering as answering
from rag_engine.config import get_settings
from rag_engine.generation.answer_records import is_entity_expansion_record
from rag_engine.generation.answering import AnswerRecord, rerank_records, select_documents
from rag_engine.retrieval.context_builder import ContextBuildRequest, build_chunk_context_bundle
from rag_engine.retrieval.entity_expansion import (
    ENTITY_EXPANSION_CHUNK_SEED_MIN_RELEVANCE,
    ENTITY_EXPANSION_HOP_MIN_RELEVANCE,
    ENTITY_EXPANSION_MIN_RELEVANCE,
    entity_expansion_relevant,
    is_reserved_entity_expansion,
)

EXPANSION = {"entity": "経理部", "hop": 1}


def _child(name, *, source="規程.pdf", parent=None, expansion=False, score=None):
    metadata = {"section_path": ["第 1 章"]}
    if expansion:
        metadata["entity_expansion"] = dict(EXPANSION)
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
        parent_chunk_uid=f"uid:{parent or name}-p",
        source_run_id="run",
        metadata=metadata,
    )


def _parent(name, *, source="規程.pdf"):
    return AnswerRecord(
        id=f"{name}-p",
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=1,
        category="Chunk",
        text=f"{name} の親",
        source=source,
        chunk_level="parent",
        chunk_id=f"{name}-p",
        chunk_uid=f"uid:{name}-p",
        source_run_id="run",
        metadata={"section_path": ["第 1 章"]},
    )


def _stub_reverse_rerank(monkeypatch):
    """rerank を、候補を逆順にする（末尾を 1 位にする）ものに差し替える。"""
    monkeypatch.setattr(answering, "_rerank_configured", lambda settings: True)

    def fake(question, documents, settings, top_n=None):
        order = list(reversed(range(len(documents))))
        return [SimpleNamespace(index=index, relevance_score=1.0 - position / 100) for position, index in enumerate(order)]

    monkeypatch.setattr(answering, "rerank_text_with_scores", fake)


def _settings():
    return SimpleNamespace(rerank_model="rerank-test", rerank_enabled=True, document_selection_enabled=False)


def test_marker_is_read_from_metadata():
    assert is_entity_expansion_record(_child("x", expansion=True))
    assert not is_entity_expansion_record(_child("y"))
    assert not is_entity_expansion_record(SimpleNamespace(metadata={"entity_expansion": "x"}))


def test_rerank_keeps_entity_expansion_at_its_position(monkeypatch):
    _stub_reverse_rerank(monkeypatch)
    records = [_child("a1"), _child("a2"), _child("e1", expansion=True), _child("a3"), _child("a4")]

    ranked = rerank_records("質問", records, _settings(), enabled=True, business_domains=(), screen_terms=())

    # 拡張の根拠（関連度が下限以上）は 3 番目のまま。ほかの候補は rerank の順（逆順）に並ぶ。
    assert [record.id for record in ranked] == ["a4", "a3", "e1", "a2", "a1"]
    assert ranked[2].metadata["rerank"]["kept_position"] == "entity_expansion"
    # 拡張の根拠も rerank にかけ、関連度を持つ（回答の診断と MCP が下限と比べる。#1390）。
    assert ranked[2].metadata["rerank"]["relevance_score"] is not None


def _stub_scores(monkeypatch, scores):
    """rerank を、候補の本文ごとの関連度（``scores``）の高い順に並べるものに差し替える。"""
    monkeypatch.setattr(answering, "_rerank_configured", lambda settings: True)

    def fake(question, documents, settings, top_n=None):
        values = [next(score for name, score in scores.items() if f"{name} の本文" in doc) for doc in documents]
        order = sorted(range(len(documents)), key=lambda index: -values[index])
        return [SimpleNamespace(index=index, relevance_score=values[index]) for index in order]

    monkeypatch.setattr(answering, "rerank_text_with_scores", fake)


def test_low_relevance_entity_expansion_is_ordered_by_rerank(monkeypatch):
    """関連度が下限未満の拡張の根拠（質問と関係の薄い属性の chunk）は位置を保たず、分数の順に並ぶ（#1390）。"""
    _stub_scores(monkeypatch, {"a1": 0.9, "a2": 0.6, "ledger": 0.45, "org": 0.05, "a3": 0.3})
    records = [_child("a1"), _child("a2"), _child("ledger", expansion=True), _child("org", expansion=True), _child("a3")]

    ranked = rerank_records("質問", records, _settings(), enabled=True, business_domains=(), screen_terms=())

    # 台帳の行（関連度 0.45）は 3 番目のまま、組織規程（0.05）は分数の順で末尾。
    assert [record.id for record in ranked] == ["a1", "a2", "ledger", "a3", "org"]
    assert is_reserved_entity_expansion(ranked[2])
    assert not is_reserved_entity_expansion(ranked[4])
    assert "kept_position" not in ranked[4].metadata["rerank"]


def test_relevance_rule_keeps_expansion_when_rerank_did_not_run():
    assert ENTITY_EXPANSION_MIN_RELEVANCE == 0.24
    assert entity_expansion_relevant(None)
    assert entity_expansion_relevant(0.24, {"seed": "question", "hop": 0})
    assert not entity_expansion_relevant(0.23, {"seed": "question", "hop": 0})
    # 質問の実体の属性から 1 段でたどった根拠は、名寄せより少し高い下限。
    assert ENTITY_EXPANSION_HOP_MIN_RELEVANCE == 0.28
    assert not entity_expansion_relevant(0.27, {"seed": "question", "hop": 1})
    assert entity_expansion_relevant(0.28, {"seed": "question", "hop": 1})
    # 上位の chunk の実体から足した根拠は、検索で当たった chunk と同じ程度の関連度を求める。
    assert ENTITY_EXPANSION_CHUNK_SEED_MIN_RELEVANCE == 0.45
    assert not entity_expansion_relevant(0.44, {"seed": "chunk"})
    assert entity_expansion_relevant(0.45, {"seed": "chunk"})
    # rerank の関連度が無い拡張の根拠（rerank の無効・失敗）は確保する（#1362 と同じ）。
    assert is_reserved_entity_expansion(_child("e", expansion=True))
    assert not is_reserved_entity_expansion(_child("e", expansion=True, score=0.05))
    assert not is_reserved_entity_expansion(_child("plain", score=0.9))


def test_rerank_without_marker_is_unchanged(monkeypatch):
    _stub_reverse_rerank(monkeypatch)
    records = [_child("a1"), _child("a2"), _child("a3")]

    ranked = rerank_records("質問", records, _settings(), enabled=True, business_domains=(), screen_terms=())

    assert [record.id for record in ranked] == ["a3", "a2", "a1"]


def test_document_selection_does_not_defer_entity_expansion():
    settings = replace(
        get_settings(environ={}, dotenv_path=None),
        document_selection_enabled=True,
        document_selection_score_ratio=0.5,
    )
    children = [
        _child("a1", source="a.pdf", score=0.9),
        _child("a2", source="a.pdf", score=0.8),
        _child("ledger", source="ledger.xlsx", expansion=True),
        _child("c1", source="c.pdf", score=0.1),
    ]

    kept, deferred, _ = select_documents(children, settings, top_k=2)

    assert [record.id for record in kept] == ["a1", "a2", "ledger"]
    assert [record.id for record in deferred] == ["c1"]

    # 関連度が下限未満の拡張の根拠は、印の無い根拠と同じく後回しにする（#1390）。
    weak = [*children[:2], _child("org", source="org.pdf", expansion=True, score=0.05), children[3]]
    kept, deferred, _ = select_documents(weak, settings, top_k=2)
    assert [record.id for record in kept] == ["a1", "a2"]
    assert [record.id for record in deferred] == ["org", "c1"]


def test_context_reserves_parent_slots_for_entity_expansion():
    """分数の順で親の枠（max_records）から落ちる拡張の根拠の親を、枠の後ろと入れ替えて確保する。"""
    names = [f"a{index}" for index in range(5)]
    children = [_child(name) for name in names] + [_child("e1", expansion=True), _child("e2", expansion=True)]
    parents = [_parent(name) for name in [*names, "e1", "e2"]]

    def build(records):
        return build_chunk_context_bundle(
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

    bundle = build(children)
    ids = [record.id for record in bundle.records]
    # 4 枠のうち半分（2）までを拡張の親に入れ替える。検索で当たった上位の親は残す。
    assert ids[:2] == ["a0-p", "a1-p"]
    assert set(ids) == {"a0-p", "a1-p", "e1-p", "e2-p"}

    plain = build([_child(name) for name in [*names, "e1", "e2"]])
    assert [record.id for record in plain.records] == ["a0-p", "a1-p", "a2-p", "a3-p"]


def test_context_does_not_swap_hit_parents_for_low_relevance_expansion():
    """評価の ``br-budget-change-window``（「予算管理システムの本番の変更は、いつ作業できますか？」）の形。

    当たった親は変更手順書（第 4 章 作業の時間帯）・運用要領・定期保守計画（共通の保守枠）。拡張の根拠は、
    質問の「予算管理システム」の台帳の行（関連度 0.3）と、その行の担当部署・重要度から 1 段でたどった組織規程
    （略号・承認者。関連度 0.06〜0.08）と障害連絡規程（重要度 A から 1 段。実サービスの関連度 0.267。質問の
    「いつ作業できるか」と関係しない）。修正前は 3 つの拡張の親が枠の後ろの親と入れ替わり、定期保守計画の親
    （共通の保守枠）が文脈から落ちた。関連度の低い拡張の親は入れ替えない。
    """
    hits = [
        _child("change-ch4", score=0.8),
        _child("ops-ch9", score=0.7),
        _child("maintenance-common", score=0.6),
        _child("ops-ch43", score=0.5),
        _child("ops-ch59", score=0.45),
    ]
    expansions = [
        _child("ledger-sys108", expansion=True, score=0.3),
        _child("org-codes", expansion=True, score=0.08),
        _child("org-approvers", expansion=True, score=0.06),
        _child("incident-severity-a", expansion=True, score=0.267),
    ]
    names = [record.id for record in [*hits, *expansions]]
    parents = [_parent(name) for name in names]

    def build(records):
        bundle = build_chunk_context_bundle(
            ContextBuildRequest(
                question="予算管理システムの本番の変更は、いつ作業できますか？",
                ranked_children=records,
                active_records=[*records, *parents],
                top_k=10,
                neighbor_child_count=0,
                max_records=5,
                max_chars=5000,
            )
        )
        return [record.id for record in bundle.records]

    ids = build([*hits, *expansions])
    # 台帳の行の親だけを枠の後ろ（分数の最も低い当たりの親）と入れ替える。定期保守計画の親は残る。
    assert set(ids) == {"change-ch4-p", "ops-ch9-p", "maintenance-common-p", "ops-ch43-p", "ledger-sys108-p"}
    assert not {"org-codes-p", "org-approvers-p", "incident-severity-a-p"} & set(ids)
    # 関連度の分からない（rerank を実行しなかった）拡張の根拠は、今までどおり枠の半分まで入れ替える。
    unscored = [_child(record.id) for record in hits] + [_child(record.id, expansion=True) for record in expansions]
    assert "maintenance-common-p" not in build(unscored)


def test_anchor_first_seen_as_neighbor_keeps_its_rerank_relevance():
    """同じ親の 2 つの拡張の根拠（組織規程の略号の表と承認者）のうち、後の根拠が先の根拠の前後の文脈として
    先に入っても、起点の record は rerank の関連度を持つ候補にする。関連度が低ければ枠を確保しない（#1390）。
    """
    hits = [_child("h0", score=0.9), _child("h1", score=0.8), _child("h2", score=0.7)]
    codes = _child("org-codes", parent="org", expansion=True, score=0.05)
    approvers = _child("org-approvers", parent="org", expansion=True, score=0.05)
    # backend が渡す同じ chunk の record（前後の文脈の元。rerank の結果は持たない）。
    stored = [_child("org-codes", parent="org", expansion=True), _child("org-approvers", parent="org", expansion=True)]
    parents = [_parent("h0"), _parent("h1"), _parent("h2"), _parent("org")]

    bundle = build_chunk_context_bundle(
        ContextBuildRequest(
            question="質問",
            ranked_children=[*hits, codes, approvers],
            active_records=[*hits, *stored, *parents],
            top_k=10,
            neighbor_child_count=1,
            max_records=3,
            max_chars=5000,
        )
    )

    assert [record.id for record in bundle.records] == ["h0-p", "h1-p", "h2-p"]
