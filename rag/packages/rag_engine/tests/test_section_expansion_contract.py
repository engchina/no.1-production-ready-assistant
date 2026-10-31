"""章節の最低範囲と共有予算の契約を合成資料で検証する。"""

from dataclasses import replace

import pytest

from rag_engine.generation import section_expansion as sections
from tests.test_section_expansion import (
    Grader,
    SectionStub,
    _attempt,
    _child_ids,
    _expand,
    _members,
    _seed_context,
    _settings,
)


def test_every_retrieved_owner_is_read_instead_of_only_the_first_two():
    stub = SectionStub()
    result = _expand(_seed_context("c31a", "c32a", "c4a"), _settings(), Grader(needs=set()), stub)
    assert stub.read_calls == ["s3-1", "s3-2", "s4"]
    assert result.trace["minimum_scope_complete"]
    assert set(_members("s4")) <= set(_child_ids(result.context))


@pytest.mark.parametrize("mode", ["empty", "partial", "raise"])
def test_partial_reads_cannot_be_overruled_by_an_optimistic_grader(mode):
    stub = SectionStub()
    original = stub.chunks

    def chunks(**kwargs):
        if mode == "raise":
            raise PermissionError("unavailable")
        return sections.SectionRead(original(**kwargs)[:1] if mode == "partial" else [], complete=False)

    stub.chunks = chunks
    result = _expand(_seed_context("c31a"), _settings(), Grader(needs=set()), stub, last=_attempt(sufficient=True))
    assert not result.trace["minimum_scope_complete"]
    assert result.stop_reason != sections.STOP_SUFFICIENT
    assert result.unread


def test_minimum_scope_requires_every_owner_even_if_the_other_one_was_read():
    stub = SectionStub()
    original = stub.chunks
    stub.chunks = lambda **kw: sections.SectionRead([], complete=False) if kw["section_id"] == "s4" else original(**kw)
    result = _expand(_seed_context("c31a", "c4a"), _settings(), Grader(needs=set()), stub)
    assert not result.trace["minimum_scope_complete"]
    assert result.stop_reason != sections.STOP_SUFFICIENT
    assert "c31b" in _child_ids(result.context)


def test_standard_completion_has_no_sufficiency_grader_and_stops_at_owner():
    stub = SectionStub()
    result = sections.expand_sections(
        "操作方法",
        _seed_context("c31a"),
        _settings(),
        last_attempt=None,
        grade=None,
        section_outline=stub.outline,
        section_chunks=stub.chunks,
    )
    assert stub.read_calls == ["s3-1"]
    assert result.stop_reason == sections.STOP_SCOPE_READ
    assert result.trace["judgement"] == "not_requested"
    assert result.trace["minimum_scope_complete"]


def test_shared_reader_completes_new_owners_without_rereading_old_ones():
    stub = SectionStub()
    reader = sections.SectionReader(stub.chunks, set())
    first = _expand(_seed_context("c31a"), _settings(), None, stub, reader=reader)
    context = sections._merge(first.context, _seed_context("c4a").evidence_tree)
    second = _expand(context, _settings(), None, stub, reader=reader)
    assert stub.read_calls == ["s3-1", "s4"]
    assert second.trace["minimum_scope_complete"]
    ids = [record.chunk_uid for record in second.context.records]
    assert len(ids) == len(set(ids))


def test_same_uid_with_only_an_excerpt_is_replaced_by_full_original():
    stub = SectionStub()
    context = _seed_context("c31a", "c31b")
    short = replace(context.records[1], text="本文", body_text="本文")
    context = replace(
        context,
        records=[context.records[0], short],
        evidence_tree=(context.evidence_tree[0], replace(context.evidence_tree[1], record=short)),
    )
    result = _expand(context, _settings(), None, stub)
    assert result.trace["minimum_scope_complete"]
    records = {record.chunk_uid: record for record in result.context.records}
    assert len(records) == 2
    assert len(records[short.chunk_uid].text) > len(short.text)


@pytest.mark.parametrize("max_levels,expected", [(1, "max_levels"), (2, "max_levels"), (3, "sufficient")])
def test_owner_parent_and_grandparent_are_distinct_levels(max_levels, expected):
    stub = SectionStub()
    result = _expand(
        _seed_context("c31a"), _settings(section_expansion_max_levels=max_levels), Grader(needs=set(_members("document"))), stub
    )
    assert result.levels == max_levels
    assert result.stop_reason == expected
    assert stub.read_calls[:max_levels] == ["s3-1", "s3", "document"][:max_levels]


def test_comparative_benchmark_detects_false_sufficiency_and_budget_gaps():
    from tests.section_expansion_benchmark import benchmark

    result = benchmark()
    assert len(result["rows"]) == 25
    summary = result["summary"]
    assert summary["none"]["false_sufficient"] > summary["adaptive"]["false_sufficient"] == 0
    assert summary["owner"]["evidence_recall"] > summary["none"]["evidence_recall"]
    assert summary["owner"]["judge_calls"] == 0
    assert summary["adaptive"]["step_fulfillment"] > summary["none"]["step_fulfillment"]
    assert any(row["case"] == "budget" and row["unread"] for row in result["rows"])


def test_full_context_over_input_limit_is_not_accepted_as_complete():
    result = _expand(_seed_context("c31a", "c31b"), _settings(answer_max_input_tokens=400), Grader(needs=set()), SectionStub())
    assert not result.trace["minimum_scope_complete"]
    assert result.stop_reason != sections.STOP_SUFFICIENT
    assert result.unread


def test_generation_budget_does_not_exceed_limit_when_initial_context_is_too_big():
    from rag_engine.generation.answering import _section_evidence_budget

    context = _seed_context("c31a", "c31b")
    result = sections.SectionExpansionResult(context=context, applied=True)
    assert _section_evidence_budget(context, _settings(answer_max_input_tokens=400), result) <= 200


def test_full_replacement_keeps_new_children_and_original_anchor_roles():
    from rag_engine.generation.answer_records import _stored_chunk_answer_record
    from rag_engine.retrieval.context_builder import ContextChildEvidence
    from tests.test_section_expansion import _child

    context = _seed_context("c31a")
    parent = context.evidence_tree[0]
    sibling = ContextChildEvidence(_stored_chunk_answer_record(_child("c31b")), "section_context", "section_expansion")
    full = replace(parent.record, text=parent.record.text + "追加の原文", body_text=parent.record.text + "追加の原文")
    added = replace(parent, record=full, children=(*parent.children, sibling))
    merged = sections._merge(context, (added,))
    assert len(merged.records) == 1
    assert _child_ids(merged) == ["c31a", "c31b"]
    assert merged.evidence_tree[0].children[0].role == "retrieved_anchor"


@pytest.mark.parametrize("exhausted", ["tokens", "time"])
def test_new_search_results_share_the_existing_input_and_time_budget(exhausted):
    stub = SectionStub()
    reader = sections.SectionReader(stub.chunks, set())
    settings = _settings(answer_max_input_tokens=2000)
    first = _expand(_seed_context("c31a"), settings, None, stub, reader=reader, started_at=0.0, clock=lambda: 0.0)
    context = sections._merge(first.context, _seed_context("c4a").evidence_tree)
    second = _expand(
        context,
        settings,
        None,
        stub,
        reader=reader,
        started_at=0.0,
        clock=lambda: 500.0 if exhausted == "time" else 0.0,
        deadline_remaining=lambda: 60.0 if exhausted == "time" else None,
    )
    assert second.unread
    assert not second.trace["minimum_scope_complete"]
    assert second.used_tokens <= second.trace["input_token_limit"]
    assert stub.read_calls.count("s3-1") == 1
    if exhausted == "time":
        assert second.stop_reason == sections.STOP_TIME_LIMIT


def test_unreadable_child_after_a_budget_hit_is_not_marked_in_context():
    stub = SectionStub()
    original = stub.chunks
    stub.chunks = lambda **kw: sections.SectionRead([], complete=False) if kw["section_id"] == "s3-3" else original(**kw)
    result = _expand(
        _seed_context("c31a"), _settings(answer_max_input_tokens=1600), Grader(needs=None), stub, large_sections=lambda request: None
    )
    assert "s3-3" in {item.section_id for item in result.unread}
    assert "s3-3" not in {item["section_id"] for item in result.trace["completed_sections"]}
