"""各段の予算と精排後の原文読取の契約。外部モデル/DBは呼ばない。"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from rag_engine.dependencies import AnswerDependencies, bind_dependencies
from rag_engine.generation.answer_models import AnswerRecord
from rag_engine.generation.answer_records import _retrieval_candidate_limit
from rag_engine.generation.answering import _bundle_context
from rag_engine.retrieval.candidate_budget import select_candidates


def child(uid: str, *, artifact: str = "a", text: str = "合成の本文", record: str = "r") -> AnswerRecord:
    return AnswerRecord(
        id=uid,
        engine="test",
        engine_label="test",
        page=1,
        seq_no=1,
        category="Chunk",
        text=text,
        source="source.pdf",
        chunk_uid=uid,
        chunk_id=uid,
        chunk_level="child",
        source_run_id="run",
        chunk_seq=1,
        source_record_refs=({"record_id": record},),
        metadata={"document_id": "document", "recipe_id": "recipe", "chunk_set_id": artifact, "chunk_strategy": "fixed"},
    )


def test_recall_candidates_are_independent_of_anchor_count():
    settings = SimpleNamespace(retrieval_candidate_limit=77)
    assert [_retrieval_candidate_limit(k, settings) for k in (1, 5, 50, 100)] == [77] * 4


@pytest.mark.parametrize("field", ["document_id", "recipe_id", "chunk_set_id", "artifact_id"])
def test_identical_text_in_another_artifact_is_retained(field):
    first = child("c1")
    other = replace(child("c2"), metadata={**first.metadata, field: "other"})
    selected, _ = select_candidates([first, other], 5)
    assert len(selected) == 2


def test_same_original_does_not_occupy_budget_but_another_source_range_does():
    rows = [child("c1"), child("duplicate"), child("c2", record="other"), child("c3", text="別の本文")]
    selected, trace = select_candidates(rows, 2)
    assert [row.id for row in selected] == ["c1", "c2"]
    assert trace["excluded"] == [
        {"id": "duplicate", "reason": "duplicate_original", "retained_id": "c1"},
        {"id": "c3", "reason": "rerank_input_budget"},
    ]


def test_no_source_provenance_is_not_a_duplicate():
    selected, _ = select_candidates([replace(child(uid), source_record_refs=()) for uid in ("c1", "c2")], 10)
    assert len(selected) == 2


def test_context_is_read_after_the_ranked_anchor_choice():
    ranked = [child("last", text="精排の最上位"), child("first", text="融合の最上位")]
    calls, stages = [], []

    def read(**options):
        calls.append([row.id for row in options["anchors"]])
        return []

    dependencies = AnswerDependencies(
        search=lambda **kw: None,
        check_ready=lambda **kw: None,
        parse_text=lambda *args, **kw: None,
        parse_images=lambda *args, **kw: None,
        rerank=lambda *args, **kw: [],
        context_chunks=read,
        record_retrieval=stages.append,
    )
    with bind_dependencies(dependencies):
        result = _bundle_context(
            "合成の質問",
            ranked,
            list(ranked),
            SimpleNamespace(),
            top_k=1,
            neighbor_child_count=0,
            max_records=5,
            max_chars=1000,
        )
    assert calls == [["last"]]
    assert [record.id for record in result.records] == ["last"]
    assert stages[-1]["anchor_limit"] == 1
    assert stages[-1]["character_limit"] == 1000


def test_default_total_budget_is_derived_from_existing_enabled_stages():
    rows = [
        replace(child(str(i), record=str(i)), metadata={**child(str(i)).metadata, "retrieval_admission_stage": stage})
        for stage, count in (("fusion", 150), ("diversity", 100), ("reference", 4))
        for i in range(
            {"fusion": 0, "diversity": 150, "reference": 250}[stage], {"fusion": 0, "diversity": 150, "reference": 250}[stage] + count
        )
    ]
    rows.append(
        replace(child("overflow", record="overflow"), metadata={**child("overflow").metadata, "retrieval_admission_stage": "reference"})
    )
    selected, trace = select_candidates(rows, 0, stage_limits={"fusion": 150, "diversity": 100, "reference": 4})
    assert len(selected) == trace["limit"] == 254
    assert trace["limit_policy"] == "sum_of_stage_limits"
    assert trace["excluded"][-1]["reason"] == "stage_admission_budget"


def test_parent_group_uses_one_anchor_slot_and_fills_the_next_original():
    from rag_engine.retrieval.context_builder import ContextBuildRequest, build_chunk_context_bundle

    first = replace(child("c1"), parent_chunk_uid="p1", metadata={**child("c1").metadata, "chunk_strategy": "small_to_big"})
    same = replace(first, id="c2", chunk_id="c2", chunk_uid="c2", text="同じ親の別の子")
    next_child = replace(first, id="c3", chunk_id="c3", chunk_uid="c3", parent_chunk_uid="p2", text="別の親")
    records = [first, same, next_child]
    result = build_chunk_context_bundle(
        ContextBuildRequest(
            question="合成の質問",
            ranked_children=records,
            active_records=records,
            top_k=2,
            neighbor_child_count=0,
            max_records=3,
            max_chars=1000,
            select_anchors_only=True,
        )
    )
    assert {item.record.id for parent in result.evidence_tree for item in parent.children} == {"c1", "c2", "c3"}


def test_unknown_stage_cannot_bypass_the_common_budget():
    row = replace(child("unknown"), metadata={"retrieval_admission_stage": "unknown"})
    selected, trace = select_candidates([row], 0, stage_limits={"fusion": 2})
    assert not selected
    assert trace["excluded"] == [{"id": "unknown", "reason": "stage_admission_budget", "stage": "unknown"}]


def test_explicit_total_override_does_not_change_per_stage_admission():
    rows = [replace(child(str(i), record=str(i)), metadata={"retrieval_admission_stage": "reference"}) for i in range(4)]
    selected, trace = select_candidates(rows, 2, stage_limits={"fusion": 100, "reference": 4})
    assert len(selected) == trace["limit"] == 2
    assert len(trace["excluded"]) == 2
    assert trace["limit_policy"] == "total_cap"


def test_same_local_identifier_in_another_artifact_is_not_a_duplicate():
    selected, _ = select_candidates([child("local", artifact="v1"), child("local", artifact="v2")], 10)
    assert len(selected) == 2


def test_deferred_parent_is_read_only_when_recovery_is_requested(monkeypatch):
    import rag_engine.generation.answering as answering
    from rag_engine.generation.answer_models import AnswerContext

    deferred = replace(child("c1"), parent_chunk_uid="p1", metadata={**child("c1").metadata, "chunk_strategy": "small_to_big"})
    parent = replace(deferred, id="p1", chunk_id="p1", chunk_uid="p1", chunk_level="parent", parent_chunk_uid="", text="保存済みの親原文")
    calls = []
    monkeypatch.setattr(answering, "_stored_chunk_answer_record", lambda record: record)
    deps = AnswerDependencies(
        search=lambda **kw: None,
        check_ready=lambda **kw: None,
        parse_text=lambda *args, **kw: None,
        parse_images=lambda *args, **kw: None,
        rerank=lambda *args, **kw: [],
        context_chunks=lambda **kw: calls.append([record.id for record in kw["anchors"]]) or [parent],
    )
    context = AnswerContext(records=[], text="", expansion_records=(deferred,), deferred_records=(deferred,))
    with bind_dependencies(deps):
        result = answering._deferred_document_records(context, 1)
    assert calls == [["c1"]]
    assert result.records == (parent,)


def test_deferred_nonparent_retains_the_original_hit_without_synthetic_parent():
    from rag_engine.generation.answer_models import AnswerContext
    from rag_engine.generation.answering import _deferred_document_records

    hit = replace(child("c1"), parent_chunk_uid="synthetic-unused")
    context = AnswerContext(records=[], text="", expansion_records=(hit,), deferred_records=(hit,))
    assert _deferred_document_records(context, 1).records == (hit,)
