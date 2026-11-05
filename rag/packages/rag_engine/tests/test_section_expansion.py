"""当たった chunk の章節から親の章節へ段階的に広げる（#1436）。

章節の一覧と章節の chunk はスタブ（``SectionStub``）、足りるかの判定（CRAG の評価器）はスタブの関数で、LLM と DB を
呼ばない。並列でも直列でも通るように、テストごとにスタブを作る（共有の状態を持たない）。
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from audit_fixtures import legacy_audit_result

from rag_engine.config import get_settings
from rag_engine.dependencies import AnswerDependencies, bind_dependencies
from rag_engine.generation import grounded
from rag_engine.generation import section_expansion as sections
from rag_engine.generation.answer_models import AnswerContext, CragRetrievalAttempt
from rag_engine.generation.answer_records import _stored_chunk_answer_record
from rag_engine.generation.answering import (
    _grounded_spans,
    _known_gaps,
    _section_evidence_budget,
    synthesize_grounded_answer,
)
from rag_engine.generation.citation_wire import generation_draft_wire_model
from rag_engine.models.audit_wire import is_audit_wire
from rag_engine.models.llm import GroundedDraft
from rag_engine.models.storage import StoredChunk
from rag_engine.retrieval.context_builder import (
    ContextChildEvidence,
    ContextParentEvidence,
    context_bundle_from_parent_evidence,
)
from rag_engine.retrieval.evidence_selection import evidence_spans

DOC = "doc-1"
CHUNK_SET = "cs-1"
FILE = "点検要領.pdf"
# 1 chunk の本文（かな・漢字 300 字 = 見積もり 300 token）。
CHUNK_CHARS = 300

# 章節: 文書全体 > 第2章（p.2）・第3章（3.1 p.5 / 3.2 p.6 / 3.3 p.7）・第4章（p.8）。leaf は 2 chunk ずつ。
LEAVES = {
    "s2": ["c2a", "c2b"],
    "s3-1": ["c31a", "c31b"],
    "s3-2": ["c32a", "c32b"],
    "s3-3": ["c33a", "c33b"],
    "s4": ["c4a", "c4b"],
}
PAGES = {"s2": 2, "s3-1": 5, "s3-2": 6, "s3-3": 7, "s4": 8}


def _sections() -> tuple[sections.SectionInfo, ...]:
    def info(section_id, title, level, parent, members, page_start, page_end):
        return sections.SectionInfo(
            section_id=section_id,
            title=title,
            level=level,
            parent_section_id=parent,
            page_start=page_start,
            page_end=page_end,
            chunk_count=len(members),
            chars=len(members) * CHUNK_CHARS,
        )

    return (
        info("document", FILE, 0, None, _members("document"), 1, 8),
        info("s2", "第2章 点検の前の準備", 1, "document", _members("s2"), 2, 2),
        info("s3", "第3章 設備ごとの点検", 1, "document", _members("s3"), 5, 7),
        info("s3-1", "3.1 空調", 2, "s3", _members("s3-1"), 5, 5),
        info("s3-2", "3.2 電源", 2, "s3", _members("s3-2"), 6, 6),
        info("s3-3", "3.3 消火設備", 2, "s3", _members("s3-3"), 7, 7),
        info("s4", "第4章 点検を中止する条件", 1, "document", _members("s4"), 8, 8),
    )


def _members(section_id: str) -> list[str]:
    if section_id == "document":
        return [chunk for leaf in ("s2", "s3-1", "s3-2", "s3-3", "s4") for chunk in LEAVES[leaf]]
    if section_id == "s3":
        return [*LEAVES["s3-1"], *LEAVES["s3-2"], *LEAVES["s3-3"]]
    return list(LEAVES[section_id])


def _owner(chunk_id: str) -> str:
    return next(section for section, members in LEAVES.items() if chunk_id in members)


def _page(chunk_id: str) -> int:
    return PAGES[_owner(chunk_id)]


def _text(chunk_id: str) -> str:
    head = f"本文{chunk_id}："
    return head + "点" * (CHUNK_CHARS - len(head))


def _child(chunk_id: str) -> StoredChunk:
    return StoredChunk(
        chunk_uid=chunk_id,
        chunk_id=chunk_id,
        chunk_level="child",
        chunk_seq=0,
        parent_chunk_uid=f"{DOC}:{chunk_id}",
        parent_chunk_id=f"{DOC}:{chunk_id}",
        child_chunk_ids=(),
        text=_text(chunk_id),
        retrieval_text=_text(chunk_id),
        source_run_id="run",
        source_file_name=FILE,
        source_engine_id="docling",
        source_engine_label="Docling",
        page_start=_page(chunk_id),
        page_end=_page(chunk_id),
        source_seq_ranges=(),
        source_record_refs=(),
        metadata={"section_path": [_owner(chunk_id)], "document_id": DOC},
    )


def _parent(chunk_id: str) -> StoredChunk:
    child = _child(chunk_id)
    return replace(
        child,
        chunk_uid=f"{DOC}:{chunk_id}",
        chunk_id=f"{DOC}:{chunk_id}",
        chunk_level="parent",
        parent_chunk_uid="",
        parent_chunk_id="",
        child_chunk_ids=(chunk_id,),
    )


class SectionStub:
    """章節の一覧と章節の chunk のスタブ（呼び出しを記録する）。"""

    def __init__(self, *, mapped: bool = True) -> None:
        self.mapped = mapped
        self.outline_calls: list[str] = []
        self.read_calls: list[str] = []

    def outline(self, *, chunk_uid: str, **_: object) -> sections.SectionOutline | None:
        self.outline_calls.append(chunk_uid)
        if not self.mapped:
            return None
        return sections.SectionOutline(
            document_id=DOC, chunk_set_id=CHUNK_SET, owner_section_id=_owner(chunk_uid), sections=_sections(), file_name=FILE
        )

    def chunks(self, *, document_id: str, chunk_set_id: str, section_id: str, **_: object) -> list[StoredChunk]:
        assert (document_id, chunk_set_id) == (DOC, CHUNK_SET)
        self.read_calls.append(section_id)
        members = _members(section_id)
        return [*(_child(chunk) for chunk in members), *(_parent(chunk) for chunk in members)]


class Grader:
    """足りるかの判定のスタブ。``needs`` の chunk が全部文脈にあれば足りる（呼び出しを数える）。"""

    def __init__(self, needs: set[str] | None = None, *, missing: tuple[str, ...] = ("requested_result",)) -> None:
        self.needs = needs
        self.missing = missing
        self.calls: list[int] = []

    def __call__(self, context: AnswerContext, level: int) -> CragRetrievalAttempt:
        self.calls.append(level)
        present = {child.record.chunk_uid for parent in context.evidence_tree for child in parent.children}
        enough = self.needs is not None and self.needs <= present
        return _attempt(sufficient=True, missing=() if enough else self.missing)


def _attempt(*, sufficient: bool, missing: tuple[str, ...] = (), relevant: tuple[str, ...] = ()) -> CragRetrievalAttempt:
    return CragRetrievalAttempt(
        attempt=1, query="q", retrieval_queries=("q",), sufficient=sufficient, missing_aspects=missing, relevant_chunk_ids=relevant
    )


def _seed_context(*chunk_ids: str) -> AnswerContext:
    """検索で当たった chunk（retrieved_anchor）を親ごとに 1 つずつ持つ文脈。"""
    tree = tuple(
        ContextParentEvidence(
            record=_stored_chunk_answer_record(_parent(chunk_id)),
            role="synthesis_parent",
            reason="retrieved",
            children=(
                ContextChildEvidence(
                    record=_stored_chunk_answer_record(_child(chunk_id)), role="retrieved_anchor", reason="retrieved", retrieval_rank=rank
                ),
            ),
        )
        for rank, chunk_id in enumerate(chunk_ids, 1)
    )
    bundle = context_bundle_from_parent_evidence(tree, max_chars=24000)
    return AnswerContext(records=list(bundle.records), text=bundle.text, evidence=bundle.evidence, evidence_tree=bundle.evidence_tree)


def _settings(**overrides: object) -> SimpleNamespace:
    values = {
        "section_expansion_enabled": True,
        "section_expansion_max_levels": 4,
        "section_expansion_input_token_percent": 50,
        "evidence_budget_chars": 48000,
        "answer_max_input_tokens": 128_000,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# 列挙・網羅の問（「すべて」。#1503 の読む範囲の規則）ではない質問。列挙の問は test_enumeration_sufficiency.py。
QUESTION = "第3章の設備ごとの点検の項目を教えて"


def _expand(
    context: AnswerContext,
    settings: SimpleNamespace,
    grader,
    stub: SectionStub,
    *,
    last: CragRetrievalAttempt | None = None,
    question: str = QUESTION,
    **options: object,
) -> sections.SectionExpansionResult:
    return sections.expand_sections(
        question,
        context,
        settings,
        last_attempt=last if last is not None else _attempt(sufficient=True, missing=("requested_result",)),
        grade=grader,
        section_outline=stub.outline,
        section_chunks=stub.chunks,
        **options,
    )


def _child_ids(context: AnswerContext) -> list[str]:
    return [child.record.chunk_uid for parent in context.evidence_tree for child in parent.children]


def test_reads_first_section_even_when_crag_judged_enough():
    stub, grader = SectionStub(), Grader(needs=set(_members("s3-1")))
    context = _seed_context("c31a")

    result = _expand(context, _settings(), grader, stub, last=_attempt(sufficient=True))

    assert result.stop_reason == sections.STOP_SUFFICIENT
    assert result.applied and result.levels == 1
    assert result.trace["minimum_scope"] == "leaf"
    assert stub.read_calls == ["s3-1"] and grader.calls == [1]
    assert set(_child_ids(result.context)) == set(_members("s3-1"))


def test_initial_sufficient_grade_does_not_skip_a_missing_followup_in_the_parent():
    stub, grader = SectionStub(), Grader(needs=set(_members("s3")))

    result = _expand(_seed_context("c31a"), _settings(), grader, stub, last=_attempt(sufficient=True))

    assert result.stop_reason == sections.STOP_SUFFICIENT
    assert stub.read_calls == ["s3-1", "s3"] and grader.calls == [1, 2]
    assert set(_child_ids(result.context)) == set(_members("s3"))


def test_first_section_already_in_context_does_not_climb_to_parent_when_enough():
    stub, grader = SectionStub(), Grader()
    context = _seed_context(*_members("s3-1"))

    result = _expand(context, _settings(), grader, stub, last=_attempt(sufficient=True))

    assert result.stop_reason == sections.STOP_SUFFICIENT and result.levels == 1
    assert result.context is context and not result.applied
    assert stub.read_calls == ["s3-1"] and grader.calls == []
    assert result.trace["levels"][0]["sections"][0]["read"] == "in_context"


def test_applicability_alone_does_not_expand_beyond_the_first_section():
    stub, grader = SectionStub(), Grader(missing=("applicability",))

    result = _expand(_seed_context("c31a"), _settings(), grader, stub, last=_attempt(sufficient=True, missing=("applicability",)))

    assert result.stop_reason == sections.STOP_SUFFICIENT
    assert stub.read_calls == ["s3-1"] and grader.calls == [1]


def test_climbs_leaf_then_parent_and_stops_when_enough():
    stub = SectionStub()
    grader = Grader(needs=set(_members("s3")))

    result = _expand(_seed_context("c31a"), _settings(), grader, stub)

    assert result.stop_reason == sections.STOP_SUFFICIENT
    # leaf（3.1）→ 親（第3章）の 2 段で足りる。文書全体には上がらない。
    assert stub.read_calls == ["s3-1", "s3"]
    assert grader.calls == [1, 2] and result.levels == 2
    assert result.unread == []


def test_keeps_most_relevant_first_and_reading_order_with_chunk_ids():
    stub = SectionStub()
    grader = Grader(needs=set(_members("s3")))

    result = _expand(_seed_context("c31a"), _settings(), grader, stub)

    # 当たった chunk（最も関連の高い根拠）を先頭に、章節の chunk を章節の中の読み順で後ろに置く。
    assert _child_ids(result.context) == ["c31a", "c31b", "c32a", "c32b", "c33a", "c33b"]
    # 引用の ID は chunk のまま（章節の id や合成の id を作らない）。
    added = [child for parent in result.context.evidence_tree[1:] for child in parent.children]
    assert {child.role for child in added} == {sections.SECTION_CONTEXT_ROLE}
    assert all(child.record.chunk_id == child.record.chunk_uid for child in added)
    assert [record.chunk_uid for record in result.context.records][0] == f"{DOC}:c31a"


def test_stops_at_max_levels_and_reports_unread_section():
    stub, grader = SectionStub(), Grader(needs=None)

    result = _expand(_seed_context("c31a"), _settings(section_expansion_max_levels=1), grader, stub)

    assert result.stop_reason == sections.STOP_MAX_LEVELS and result.stop_reason in sections.LIMIT_STOPS
    assert stub.read_calls == ["s3-1"] and grader.calls == [1]
    assert [item.section_id for item in result.unread] == ["s3"]
    assert result.gap_lines == [f"「{FILE}」の「第3章 設備ごとの点検」（p.5〜7）は、読み込み量の上限に達したため未反映です。"]


def test_never_climbs_more_than_max_levels():
    stub, grader = SectionStub(), Grader(needs=None)

    result = _expand(_seed_context("c31a"), _settings(section_expansion_max_levels=4), grader, stub)

    # 3.1 → 第3章 → 文書全体で根に着く（3 段）。上限の 4 段を超えて読まない。
    assert stub.read_calls == ["s3-1", "s3", "document"]
    assert result.stop_reason == sections.STOP_DOCUMENT and len(grader.calls) <= 4
    assert result.unread == []


def test_token_budget_selects_relevant_child_sections_and_stays_under_limit():
    stub, grader = SectionStub(), Grader(needs=None)
    # 入力の上限 1,200 token（モデルの入力の上限 2,400 × 50%）。当たった chunk 300 + 3.1 の残り 300 の後は、
    # 第3章の全体（残り 1,200）は入らず、子の章節（600 ずつ）が 1 つだけ入る。
    settings = _settings(answer_max_input_tokens=2400)

    def prefer_last(question, documents, settings, top_n=None):
        # 見出しの関連度: 3.3（消火設備）を先に。
        return [SimpleNamespace(index=index) for index in reversed(range(len(documents)))]

    result = _expand(_seed_context("c31a"), settings, grader, stub, rerank=prefer_last)

    limit = sections.SectionExpansionLimits.from_settings(settings).input_tokens
    assert limit == 1200
    assert sections.context_tokens(result.context) <= limit
    # 入るかは、章節の chunk のうち文脈に無い分で確かめる（#1504。第3章の残りは 1,200 で入らない）。
    # 関連の高い子の章節（3.3）を全体で読み、入らない 3.2 は文脈に入れない。
    assert stub.read_calls == ["s3-1", "s3", "s3-3", "s3-2"]
    assert "c33a" in _child_ids(result.context) and "c32a" not in _child_ids(result.context)
    assert result.stop_reason == sections.STOP_TOKEN_BUDGET
    # 3.2 は当たった章節（3.1）の兄弟で起点の chunk を含まないので、回答には出さず診断にだけ残す（#2816）。
    assert result.unread == [] and result.gap_lines == []
    assert [item.section_id for item in result.omitted] == ["s3-2"]
    omitted = result.trace["unread_omitted"][0]
    assert "読み込み量の上限" in omitted["line"]
    assert (omitted["document_id"], omitted["reason"], omitted["page"]) == (DOC, sections.STOP_TOKEN_BUDGET, 6)
    # 全体を読む必要があるのに入らない章節は、第 3 段階（#1437）の差し込み口に渡る（今は未実装）。
    assert result.trace["large_section_handoff"] == "extract_then_integrate_unavailable"


def test_token_budget_keeps_unread_sections_even_when_grader_says_enough():
    stub = SectionStub()
    grader = Grader(needs={"c33a", "c33b"})
    # 読む範囲が文書全体の網羅の問では、どの章節も読む範囲に入るので、読めなかった兄弟の章節も回答に出す。
    question = "点検要領で、作業者がしないと決められていることをすべて挙げてください。"
    assert sections.minimum_scope(question) == "document"

    result = _expand(
        _seed_context("c31a"),
        _settings(answer_max_input_tokens=2400),
        grader,
        stub,
        question=question,
        rerank=lambda q, docs, s, top_n=None: [SimpleNamespace(index=i) for i in reversed(range(len(docs)))],
    )

    assert result.stop_reason == sections.STOP_TOKEN_BUDGET
    assert [item.section_id for item in result.unread] == ["s3-2"] and result.omitted == []
    assert "読み込み量の上限" in result.gap_lines[0]
    # 本文の行と同じ文と、原文を開くための文書・頁を回答の診断に載せる（#2040）。
    unread = result.trace["unread"][0]
    assert unread["line"] == result.gap_lines[0]
    assert (unread["document_id"], unread["reason"], unread["page"]) == (DOC, sections.STOP_TOKEN_BUDGET, 6)


def test_unread_sibling_without_seed_does_not_block_sufficient():
    # 兄弟の章節（3.2）が予算で読めなくても、読んだ根拠で足りると判定されれば足りるで止める（#2816）。
    stub = SectionStub()
    grader = Grader(needs={"c33a", "c33b"})

    result = _expand(
        _seed_context("c31a"),
        _settings(answer_max_input_tokens=2400),
        grader,
        stub,
        rerank=lambda q, docs, s, top_n=None: [SimpleNamespace(index=i) for i in reversed(range(len(docs)))],
    )

    assert result.stop_reason == sections.STOP_SUFFICIENT
    assert result.unread == [] and [item.section_id for item in result.omitted] == ["s3-2"]


class OwnerStub(SectionStub):
    """当たった chunk の所有の章節を、子の章節を持つ章節にするスタブ（章の前書きに当たった chunk など）。"""

    def __init__(self, owner: str) -> None:
        super().__init__()
        self.owner = owner

    def outline(self, *, chunk_uid: str, **options: object) -> sections.SectionOutline | None:
        outline = super().outline(chunk_uid=chunk_uid, **options)
        return replace(outline, owner_section_id=self.owner) if outline is not None else None


def test_unread_child_of_the_owner_section_is_still_reported():
    # 所有の章節（読む範囲）の子の章節は、起点の chunk を含まなくても読む範囲なので、読めなければ回答に出す。
    stub, grader = OwnerStub("s3"), Grader(needs=None)

    result = _expand(
        _seed_context("c31a"),
        _settings(answer_max_input_tokens=2400, section_expansion_max_levels=1),
        grader,
        stub,
        rerank=lambda q, docs, s, top_n=None: [SimpleNamespace(index=i) for i in reversed(range(len(docs)))],
    )

    # 第3章の全体（残り 1,500）は入らない。3.3（600）を読み、3.2 は入らず、3.1 の残り（300）を読む。
    assert stub.read_calls[:2] == ["s3", "s3-3"]
    assert [item.section_id for item in result.unread] == ["s3-2"] and result.omitted == []


def test_read_failure_of_a_sibling_is_still_reported():
    # 読取の失敗は、起点を含まない兄弟の章節でも回答に出す（予算の上限だけを診断に回す。#2816）。
    class FailingStub(SectionStub):
        def chunks(self, *, section_id: str, **options: object) -> list[StoredChunk]:
            if section_id == "s3-3":
                self.read_calls.append(section_id)
                raise RuntimeError("read failed")
            return super().chunks(section_id=section_id, **options)

    stub, grader = FailingStub(), Grader(needs=None)

    result = _expand(
        _seed_context("c31a"),
        _settings(answer_max_input_tokens=2400),
        grader,
        stub,
        rerank=lambda q, docs, s, top_n=None: [SimpleNamespace(index=i) for i in reversed(range(len(docs)))],
    )

    assert ("s3-3", sections.STOP_READ_ERROR) in [(item.section_id, item.reason) for item in result.unread]
    assert "s3-3" not in [item.section_id for item in result.omitted]


def test_later_phase_expands_new_seeds_before_retrying_earlier_ones():
    # 上限 1,000（モデルの入力の上限 2,000 × 50%）。後の段は、前の段で起点にしなかった起点の章節を先に広げる（#2816）。
    stub, grader = SectionStub(), Grader(needs=None)
    settings = _settings(answer_max_input_tokens=2000, section_expansion_max_levels=1)
    reader = sections.SectionReader(read_chunks=stub.chunks, known_ids=set())

    # 前の段: 上限 300 なので、3.1 の残り（300）も入らない。
    first = _expand(
        _seed_context("c31a"), _settings(answer_max_input_tokens=600, section_expansion_max_levels=1), grader, stub, reader=reader
    )
    assert "c31b" not in _child_ids(first.context)

    # 後の段: 新しい起点（第4章の c4a）の章節を先に読み、残り 100 では 3.1 の残りは入らない。
    second = _expand(_seed_context("c31a", "c4a"), settings, grader, stub, reader=reader)
    assert "c4b" in _child_ids(second.context) and "c31b" not in _child_ids(second.context)
    assert [item.section_id for item in second.unread] == ["s3-1"]


def test_large_section_handler_receives_sections_that_do_not_fit():
    stub, grader = SectionStub(), Grader(needs=None)
    handed: list[sections.LargeSectionRequest] = []

    def handler(request):
        handed.append(request)
        return None

    _expand(
        _seed_context("c31a"),
        _settings(answer_max_input_tokens=2400),
        grader,
        stub,
        large_sections=handler,
        rerank=lambda q, docs, s, top_n=None: [SimpleNamespace(index=i) for i in reversed(range(len(docs)))],
    )

    # 入らない子の章節（3.2）は、広げている章節（第3章）の要求としてまとめて渡す。
    assert [request.section.section_id for request in handed] == ["s3"]
    assert [section.section_id for section in handed[0].unread_sections] == ["s3-2"]
    # 渡すのは、章節の親（章節の中の読み順）。まだ文脈に無い親（3.2）に加えて、すでに文脈にある章節の親
    # （当たった chunk 3.1a・1 段目に読んだ 3.1b）も渡し、その分を残りの入力 token に戻す（#1504。二重に数えない）。
    # 2 段目に全体で読んだ 3.3 は、この段で足した根拠なので渡さない。
    assert [item.record.chunk_uid for item in handed[0].tree] == [f"{DOC}:c31a", f"{DOC}:c31b", f"{DOC}:c32a", f"{DOC}:c32b"]
    assert handed[0].context_keys == {f"{DOC}:c31a", f"{DOC}:c31b"}
    # 上限 1,200 − 文脈（3.1）− 3.3 に、文脈の 3.1 の分を戻す（= 上限 − 3.3）。
    section_3_3 = sum(sections.estimate_tokens(_text(chunk)) for chunk in LEAVES["s3-3"])
    assert handed[0].remaining_tokens == 1200 - section_3_3


def test_elapsed_time_does_not_impose_a_separate_expansion_deadline():
    stub = SectionStub()
    grader = Grader(needs=None)
    result = _expand(_seed_context("c31a"), _settings(), grader, stub, started_at=0.0, clock=lambda: 900.0)
    assert result.applied
    assert result.stop_reason != sections.STOP_TIME_LIMIT


def test_answer_deadline_also_limits_time():
    stub, grader = SectionStub(), Grader(needs=None)

    result = _expand(_seed_context("c31a"), _settings(), grader, stub, started_at=0.0, clock=lambda: 10.0, deadline_remaining=lambda: 60.0)

    assert result.stop_reason == sections.STOP_TIME_LIMIT and stub.read_calls == []


def test_answer_deadline_leaves_one_generation_round():
    """回答の期限の残りが、1 段の分と生成の分（1 組の見積もり + 確定の分。#1516）より少なければ上がらない。"""
    reserve = sections.GENERATION_RESERVE_SECONDS
    assert reserve == 240.0 + 30.0
    for remaining, climbs in ((reserve + sections.DEFAULT_LEVEL_SECONDS - 1.0, False), (reserve + sections.DEFAULT_LEVEL_SECONDS, True)):
        stub, grader = SectionStub(), Grader(needs=None)
        result = _expand(
            _seed_context("c31a"),
            _settings(),
            grader,
            stub,
            started_at=0.0,
            clock=lambda: 10.0,
            deadline_remaining=lambda remaining=remaining: remaining,
        )
        assert (stub.read_calls != []) is climbs, remaining
        if not climbs:
            assert result.stop_reason == sections.STOP_TIME_LIMIT


def test_time_estimate_uses_measured_grade_seconds():
    stub = SectionStub()
    now = [0.0]

    def grade(context, level):
        now[0] += 150.0  # 判定 1 回に 150 秒
        return _attempt(sufficient=True, missing=("requested_result",))

    result = _expand(
        _seed_context("c31a"), _settings(), grade, stub, started_at=0.0, clock=lambda: now[0], deadline_remaining=lambda: 600.0 - now[0]
    )

    # 1 段目の後: 経過 150 秒、残り 450 秒 ≧ 判定 150 + 生成の分 270 秒（1 組の見積もり 240 + 確定 30。#1516）の
    # 420 秒なので 2 段目を読み、2 段目の後は残り 300 秒で止める。上限 600 秒を超えない。
    assert sections.GENERATION_RESERVE_SECONDS == 270.0
    assert stub.read_calls == ["s3-1", "s3"]
    assert result.stop_reason == sections.STOP_TIME_LIMIT
    assert now[0] <= 600.0


def test_without_section_map_does_nothing():
    stub, grader = SectionStub(mapped=False), Grader()
    context = _seed_context("c31a")

    result = _expand(context, _settings(), grader, stub)

    assert result.stop_reason == sections.STOP_NO_SECTIONS and result.context is context
    assert stub.read_calls == [] and grader.calls == []


def test_disabled_setting_does_nothing():
    stub, grader = SectionStub(), Grader()

    result = _expand(_seed_context("c31a"), _settings(section_expansion_enabled=False), grader, stub)

    assert result.stop_reason == sections.STOP_DISABLED and stub.outline_calls == []


def test_grade_error_keeps_read_sections():
    stub = SectionStub()

    def broken(context, level):
        raise RuntimeError("評価器の失敗")

    result = _expand(_seed_context("c31a"), _settings(), broken, stub)

    assert result.stop_reason == sections.STOP_GRADE_ERROR and result.applied
    assert "c31b" in _child_ids(result.context) and result.unread == []
    # 例外の型の名前だけを残し、本文は残さない（#3259）。
    assert [level.get("grade_error") for level in result.trace["levels"] if "grade_error" in level] == ["RuntimeError"]


def test_two_seed_sections_share_the_parent_once():
    stub = SectionStub()
    grader = Grader(needs=set(_members("s3")))

    result = _expand(_seed_context("c31a", "c33a"), _settings(), grader, stub)

    # 起点の章節は 3.1 と 3.3。1 段目に両方を読み、2 段目の第3章は 1 回だけ読む。
    assert stub.read_calls == ["s3-1", "s3-3", "s3"]
    assert result.stop_reason == sections.STOP_SUFFICIENT
    assert len(_child_ids(result.context)) == len(set(_child_ids(result.context)))


def test_estimate_tokens():
    assert sections.estimate_tokens("点検") == 2
    assert sections.estimate_tokens("abcd") == 1
    assert sections.estimate_tokens("UPS の点検") == 1 + 3  # "UPS " は 4 字で 1 token


def test_known_gaps_and_budget_follow_the_last_grade():
    attempts = (_attempt(sufficient=False, missing=("procedure",)),)
    expansion = SimpleNamespace(trace={"grades": [{"missing_aspects": ["requested_result"]}]}, applied=True)
    assert _known_gaps(attempts, None) == ("procedure",)
    assert _known_gaps(attempts, expansion) == ("requested_result",)
    context = _seed_context("c31a", "c32a")
    settings = _settings(evidence_budget_chars=100)
    # 広げたときの原文の予算は、入力 token の上限（モデルの入力の上限 × 50%）を超えない。
    assert _section_evidence_budget(context, settings, expansion) == 100
    assert _section_evidence_budget(context, settings, None) is None


def test_whole_records_keep_section_text_when_it_fits():
    records = [_stored_chunk_answer_record(_parent(chunk)) for chunk in ("c31a", "c31b", "c32a")]
    records[0] = replace(records[0], text=records[0].text * 4)  # 1,200 字の親（平均より長い）
    cut = evidence_spans("消火設備", records, budget=2400)
    whole = evidence_spans("消火設備", records, budget=2400, whole_records=True)
    assert sum(len(span["text"]) for span in cut) < sum(len(r.text) for r in records)
    assert sum(len(span["text"]) for span in whole) == sum(len(r.text) for r in records)
    # 入らないときは通常の配分（予算を超えない）。
    over = evidence_spans("消火設備", records, budget=1000, whole_records=True)
    assert sum(len(span["text"]) for span in over) <= 1000


def test_unread_sections_make_the_answer_conditional():
    context = _seed_context("c31a")
    spans = _grounded_spans("質問", context, (), None)
    evidence_id = spans[0]["evidence_id"]
    quote = spans[0]["text"][:20]
    draft = {
        "summary": "空調の点検の項目です。",
        "confidence": "high",
        "items": [{"kind": "rule", "text": quote, "citations": [{"evidence_id": evidence_id, "quote": quote}]}],
    }
    audit = {
        "goal_alignment": "aligned",
        "summary_supported": True,
        "reviews": [{"index": 0, "support": "supported", "applicability": "matched", "condition": "", "reason": "r"}],
        "request_reviews": [{"request_id": "Q1", "status": "addressed", "reason": "合成の要求に回答済み"}],
        "unused_evidence_ids": [],
    }

    def model(system, prompt, settings, schema, **options):
        if schema in (GroundedDraft, generation_draft_wire_model()):
            # 既存quote fixtureのcanonical再生。wire出力の検証とは別。
            return GroundedDraft.model_validate(draft)
        if is_audit_wire(schema):
            return legacy_audit_result(audit, schema)
        return schema.model_validate(audit)

    unread = [f"「{FILE}」の「第3章 設備ごとの点検」（p.5〜7）は、読み込み量の上限に達したため未反映です。"]
    unused = lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected I/O"))  # noqa: E731
    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        plain = synthesize_grounded_answer("質問", context, get_settings(), image_prompt_mode="text_only")
        limited = synthesize_grounded_answer("質問", context, get_settings(), image_prompt_mode="text_only", unread_sections=unread)
    assert plain.response.envelope["outcome"] == "conditional"
    assert limited.response.envelope["outcome"] == "conditional"
    assert unread[0] in limited.response.envelope["gaps"]
    assert f"{grounded.answer_labels().unverified_section}\n\n・{unread[0]}" in limited.response.answer_text
    assert limited.response.needs_human_review is True


@pytest.mark.parametrize(
    "percent, model, expected",
    [(50, 128_000, 64_000), (25, 128_000, 32_000), (90, 200_000, 180_000), (50, None, 64_000)],
    ids=["50", "25", "90-large-model", "default-model"],
)
def test_input_token_limit_is_share_of_model_input(percent, model, expected):
    """根拠の入力の上限は、回答のモデルの入力の上限（共通のモデル設定。#1437）に対する割合。"""
    overrides = {"section_expansion_input_token_percent": percent}
    if model is not None:
        overrides["answer_max_input_tokens"] = model
    settings = _settings(**overrides)
    if model is None:
        del settings.answer_max_input_tokens
    limits = sections.SectionExpansionLimits.from_settings(settings)
    assert limits.input_tokens == expected
    # 根拠の文字数の予算（evidence_budget_chars）には依らない（#1436 の暫定の推定を置き換えた）。
    assert sections.SectionExpansionLimits.from_settings(_settings(**overrides, evidence_budget_chars=1000)).input_tokens == (
        expected if model else 64_000
    )


def test_engine_settings_read_section_expansion_environment():
    settings = get_settings(
        environ={
            "RAG_ENGINE_SECTION_EXPANSION": "0",
            "RAG_ENGINE_SECTION_EXPANSION_MAX_LEVELS": "2",
            "RAG_ENGINE_SECTION_EXPANSION_INPUT_TOKEN_PERCENT": "30",
        },
        dotenv_path=None,
    )
    assert (
        settings.section_expansion_enabled,
        settings.section_expansion_max_levels,
        settings.section_expansion_input_token_percent,
    ) == (False, 2, 30)
    defaults = get_settings(environ={}, dotenv_path=None)
    assert defaults.section_expansion_enabled and defaults.section_expansion_max_levels == 4


def test_unread_siblings_are_reported_when_no_sibling_was_read():
    # より上の兄弟の章節を 1 つも読めなければ、起点を含まない兄弟の章節も回答に出す（欠けの手がかりを消さない）。
    stub, grader = SectionStub(), Grader(needs=None)

    result = _expand(_seed_context("c31a"), _settings(answer_max_input_tokens=1200), grader, stub)

    assert result.stop_reason == sections.STOP_TOKEN_BUDGET
    assert {item.section_id for item in result.unread} == {"s3-2", "s3-3"} and result.omitted == []


# 別の文書（#2828）。
OTHER_DOC = "doc-2"
OTHER_FILE = "別の文書.pdf"
OTHER_LEAVES = {"o1": ["o1a", "o1b"], "o2": ["o2a", "o2b"]}


def _unread(document_id: str, section_id: str) -> sections.UnreadSection:
    return sections.UnreadSection(document_id, section_id, f"「{document_id}」の「{section_id}」", sections.STOP_TOKEN_BUDGET, 1)


def test_answer_unread_view_keeps_cited_documents_only():
    unread = [_unread(DOC, "s3-2"), _unread(OTHER_DOC, "o1"), _unread("doc-3", "x1"), "文の行。"]

    view = sections.answer_unread_view(unread, {DOC})

    assert view.lines == (unread[0].gap_line(), "文の行。")
    assert [item.section_id for item in view.hidden] == ["o1", "x1"] and view.overflow == 0
    # 回答が根拠にした文書が無ければ（根拠を示せなかった回答）絞らない。
    assert len(sections.answer_unread_view(unread, set()).lines) == 4


def test_answer_unread_view_caps_the_lines():
    unread = [_unread(DOC, f"s{index}") for index in range(sections.MAX_UNREAD_LINES + 3)]

    view = sections.answer_unread_view(unread, {DOC})

    assert len(view.lines) == sections.MAX_UNREAD_LINES + 1 and view.overflow == 3
    assert view.lines[-1] == "ほか 3 件の関連章節も未反映です。"
    assert view.to_payload() == {"shown": sections.MAX_UNREAD_LINES + 1, "hidden": [], "overflow": 3}


def test_answer_hides_unread_sections_of_documents_it_does_not_cite():
    context = _seed_context("c31a")
    spans = _grounded_spans("質問", context, (), None)
    evidence_id, quote = spans[0]["evidence_id"], spans[0]["text"][:20]
    draft = {
        "summary": "空調の点検の項目です。",
        "confidence": "high",
        "items": [{"kind": "rule", "text": quote, "citations": [{"evidence_id": evidence_id, "quote": quote}]}],
    }
    audit = {
        "goal_alignment": "aligned",
        "summary_supported": True,
        "reviews": [{"index": 0, "support": "supported", "applicability": "matched", "condition": "", "reason": "r"}],
        "request_reviews": [{"request_id": "Q1", "status": "addressed", "reason": "合成の要求に回答済み"}],
        "unused_evidence_ids": [],
    }

    def model(system, prompt, settings, schema, **options):
        if schema in (GroundedDraft, generation_draft_wire_model()):
            # 既存quote fixtureのcanonical再生。wire出力の検証とは別。
            return GroundedDraft.model_validate(draft)
        if is_audit_wire(schema):
            return legacy_audit_result(audit, schema)
        return schema.model_validate(audit)

    unused = lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected I/O"))  # noqa: E731
    cited, other = _unread(DOC, "s3-2"), _unread(OTHER_DOC, "o1")
    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        only_other = synthesize_grounded_answer("質問", context, get_settings(), image_prompt_mode="text_only", unread_sections=[other])
        both = synthesize_grounded_answer("質問", context, get_settings(), image_prompt_mode="text_only", unread_sections=[cited, other])

    # 回答が根拠にしない別の文書の章節は出さず、条件つきにもしない。診断に残す。
    assert only_other.response.envelope["outcome"] == "conditional"
    assert grounded.answer_labels().unverified_section not in only_other.response.answer_text
    assert only_other.response.generation_trace["unread_sections"]["hidden"][0]["document_id"] == OTHER_DOC
    assert both.response.envelope["outcome"] == "conditional"
    assert cited.gap_line() in both.response.answer_text and other.gap_line() not in both.response.answer_text


def test_unavailable_extraction_is_explicitly_identified_as_disabled_setting():
    from time import monotonic

    from rag_engine.generation.answering import _expand_context_by_sections

    stub = SectionStub()
    unused = lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected I/O"))  # noqa: E731
    settings = replace(
        get_settings(environ={}, dotenv_path=None),
        section_extraction_enabled=False,
        section_expansion_max_levels=4,
        section_expansion_input_token_percent=50,
        answer_max_input_tokens=800,
    )
    with bind_dependencies(
        AnswerDependencies(unused, unused, unused, unused, unused, section_outline=stub.outline, section_chunks=stub.chunks)
    ):
        _, result = _expand_context_by_sections(
            QUESTION,
            _seed_context("c31a"),
            settings,
            (),
            answer_llm_provider=None,
            rerank_enabled=False,
            started_at=monotonic(),
            use_grader=False,
        )
    assert result.trace["extraction_enabled"] is False
    assert result.trace["large_section_handoff"] == "extract_then_integrate_unavailable"
    assert result.trace["large_section_handoff_reason"] == "disabled_by_setting"
    assert result.unread or result.omitted
