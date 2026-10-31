"""判定の仕事（監査・CRAG の評価）の時間の短縮 (#1528) の検証。

監査の推論の量、CRAG の評価の入力の圧縮、CRAG の評価を曖昧な帯だけで呼ぶ条件、章節の拡張が読む範囲（文書全体）まで
読んだ後の評価し直しの省略を、LLM と DB を置き換えて確かめる。並列でも直列でも通るように、共有の状態を持たない。
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from audit_fixtures import legacy_audit_result
from test_answer_llm_latency import _settings as _engine_settings
from test_enumeration_sufficiency import BUSINESS_DAY_FIVE, DO_NOT_TOUCH, REPORT, RESERVE, _items
from test_section_expansion import Grader, SectionStub, _attempt, _expand, _members, _seed_context
from test_section_expansion import _settings as _section_settings

import rag_engine.generation.answering as generation
from rag_engine.config import ENTERPRISE_AI_LLM_PROVIDER, LLM_PURPOSE_AUDIT, get_settings, llm_reasoning_effort_note
from rag_engine.generation import crag_support
from rag_engine.generation import section_expansion as sections
from rag_engine.generation.answer_models import AnswerContext, AnswerRecord
from rag_engine.models.audit_wire import is_audit_wire
from rag_engine.models.llm import CragRetrievalGradeOutput
from rag_engine.retrieval.context_builder import ContextChildEvidence, ContextParentEvidence
from rag_engine.retrieval.enumeration import ENUMERATION_ASPECT


def test_env_values_for_the_judge_settings():
    settings = get_settings(
        environ={
            "RAG_ENGINE_AUDIT_REASONING_EFFORT": " Low ",
            "RAG_ENGINE_CRAG_COMPACT_INPUT": "1",
            "RAG_ENGINE_SECTION_SKIP_REGRADE_AT_SCOPE": "true",
        },
        dotenv_path=None,
    )
    assert settings.audit_reasoning_effort == "low"
    assert settings.crag_compact_input is True and settings.section_skip_regrade_at_scope is True
    defaults = get_settings(environ={}, dotenv_path=None)
    # 既定（空）は監査もモデルの登録値のまま。圧縮・評価し直しの省略は off。
    assert defaults.audit_reasoning_effort == ""
    assert defaults.crag_compact_input is False and defaults.section_skip_regrade_at_scope is False


# ---- 監査の推論の量 ---------------------------------------------------------------------------------------


def test_audit_uses_its_own_effort_separately_from_generation():
    from rag_engine.generation import grounded

    settings = _engine_settings(generation_reasoning_effort="high", audit_reasoning_effort="low")
    seen = []

    def fake_round(question, spans, round_settings, **options):
        seen.append(
            (
                round_settings.llm_providers[ENTERPRISE_AI_LLM_PROVIDER].reasoning_effort,
                options["audit_settings"].llm_providers[ENTERPRISE_AI_LLM_PROVIDER].reasoning_effort,
            )
        )
        raise RuntimeError("stop after the first round")

    context = generation.AnswerContext(records=[_parent("r1", "経費精算の申請方法")], text="経費精算の申請方法")
    with patch.object(grounded, "run_round", side_effect=fake_round), pytest.raises(RuntimeError):
        generation.synthesize_grounded_answer("経費精算の申請方法は？", context, settings, image_prompt_mode="text_only")
    # 生成は回答文の生成の量（high）、監査は監査の量（low）で呼ぶ。
    assert seen == [("high", "low")]
    assert llm_reasoning_effort_note(settings, LLM_PURPOSE_AUDIT) == "推論の量: low（回答の監査の設定。モデルの登録値は medium）"


def test_run_round_calls_the_audit_with_the_audit_settings():
    from rag_engine.generation import grounded
    from rag_engine.generation.citation_wire import generation_draft_wire_model
    from rag_engine.models.llm import GroundedAudit, GroundedDraft

    generation_settings = get_settings(environ={}, dotenv_path=None)
    audit_settings = replace(generation_settings, audit_reasoning_effort="low")
    calls = []

    def parse_text(system, prompt, settings, schema, **options):
        calls.append((schema.canonical_schema if is_audit_wire(schema) else schema, settings))
        if schema in (GroundedDraft, generation_draft_wire_model()):
            return GroundedDraft(summary="", confidence="low", items=[])
        result = GroundedAudit(
            reviews=[],
            request_reviews=[{"request_id": "R1", "status": "missing", "reason": "なし"}],
            summary_supported=False,
            goal_alignment="partial",
        )
        return legacy_audit_result(result, schema) if is_audit_wire(schema) else result

    question = "経費精算の申請方法は？"
    context = AnswerContext(
        records=[_parent("r1", "経費精算の申請方法: 経費精算画面で申請します。")], text="経費精算の申請方法: 経費精算画面で申請します。"
    )
    spans = generation._grounded_spans(question, context, (), None)
    grounded.run_round(
        question,
        spans,
        generation_settings,
        prompt="p",
        image_paths=(),
        provider_id=None,
        parse_text=parse_text,
        parse_images=None,
        audit_settings=audit_settings,
    )
    assert [(schema.__name__, settings) for schema, settings in calls] == [
        ("GroundedDraftWire", generation_settings),
        ("GroundedAudit", audit_settings),
    ]
    calls.clear()
    # 渡さなければ監査も生成と同じ設定（従来の動き）。
    grounded.run_round(
        question, spans, generation_settings, prompt="p", image_paths=(), provider_id=None, parse_text=parse_text, parse_images=None
    )
    assert [settings for _schema, settings in calls] == [generation_settings, generation_settings]


# ---- CRAG の評価の入力の圧縮 ---------------------------------------------------------------------------------


def _parent(uid: str, text: str, *, source: str = "規程.pdf", metadata: dict | None = None) -> AnswerRecord:
    return AnswerRecord(
        id=uid,
        chunk_id=f"local-{uid}",
        chunk_uid=uid,
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=1,
        category="Chunk",
        text=text,
        source=source,
        chunk_level="parent",
        parent_chunk_id="",
        metadata={"section_path": ["規程", "第 2 章"], **(metadata or {})},
    )


def _tree_context(count: int) -> AnswerContext:
    tree = []
    for n in range(count):
        parent = _parent(f"p{n}", f"経費精算の申請の規則 {n}。申請は月末までに行います。")
        child = replace(
            parent,
            id=f"c{n}",
            chunk_id=f"local-c{n}",
            chunk_uid=f"c{n}",
            chunk_level="child",
            parent_chunk_uid=f"p{n}",
            parent_chunk_id=f"local-p{n}",
        )
        tree.append(
            ContextParentEvidence(
                record=parent,
                role="synthesis_parent",
                reason="retrieved",
                children=(ContextChildEvidence(record=child, role="retrieved_anchor", reason="retrieved", retrieval_rank=n + 1),),
            )
        )
    records = [item.record for item in tree]
    return AnswerContext(records=records, text="\n".join(r.text for r in records), evidence_tree=tuple(tree))


def _candidates_json(prompt: str) -> str:
    begin, end = crag_support.UNTRUSTED_CRAG_CANDIDATES_BEGIN, crag_support.UNTRUSTED_CRAG_CANDIDATES_END
    return prompt.split(begin + "\n", 1)[1].split("\n" + end, 1)[0]


def test_compact_prompt_keeps_only_chunk_uid_and_needed_fields_without_spaces():
    context = _tree_context(15)
    question = "経費精算の申請の期限は？"
    full = crag_support._build_crag_grade_prompt(question, question, [question], context)
    compact = crag_support._build_crag_grade_prompt(question, question, [question], context, compact=True, max_candidates=12)

    shown = json.loads(_candidates_json(compact))
    # 生成に渡す親の数（12）にそろえ、ID は chunk_uid だけ（局所 ID・引用の文字列・子の entry を載せない）。
    assert [item["chunk_uid"] for item in shown] == [f"p{n}" for n in range(12)]
    assert all(item["anchor_uids"] == [f"c{n}"] for n, item in enumerate(shown))
    assert set(shown[0]) == {"chunk_uid", "source", "page", "section", "category", "text", "anchor_uids"}
    for removed in ("local-p0", "image_id", "parent_chunk_id", "citation", "context_children", "seq_no"):
        assert removed not in compact
    assert "\n  " not in _candidates_json(compact) and ", " not in _candidates_json(compact).replace("、", "")
    # 従来の入力は変えない（30 件まで・indent=2・全欄）。
    assert len(json.loads(_candidates_json(full))) == 15 and "\n  " in _candidates_json(full)
    assert len(compact) < len(full) // 2


def test_compact_input_keeps_exclusion_missing_aspects_and_enumerated_items():
    question = BUSINESS_DAY_FIVE
    other = _parent("x1", "サーバー室の空調の温度は 25 度以下に保ちます。")
    records = (RESERVE, REPORT, other)
    tree = tuple(ContextParentEvidence(record=item, role="synthesis_parent", reason="retrieved") for item in records)
    context = AnswerContext(records=list(records), text="\n".join(r.text for r in records), evidence_tree=tree)
    output = CragRetrievalGradeOutput(
        candidate_verdicts=[
            {"chunk_uid": "p2", "relevant": True, "reason": "関連"},
            {"chunk_uid": "p12", "relevant": True, "reason": "関連"},
            {"chunk_uid": "x1", "relevant": False, "reason": "話題が別"},
        ],
        enumerated_items=_items(("p2", "5 営業日前までに"), ("p2", "3 営業日前までに"), ("p12", "3 営業日以内に")),
        aspect_checks=[
            {
                "aspect": "business_object",
                "status": "supported",
                "source_ids": ["p2"],
                "evidence": [{"source_id": "p2", "quote": RESERVE.text}],
                "reason": "関連",
            },
            {"aspect": ENUMERATION_ASPECT, "status": "missing", "source_ids": [], "reason": "5件のうち3件しか確認できない"},
        ],
        reason="足りる",
        sufficient=True,
        confidence=0.9,
        rewritten_query="",
    )
    settings = SimpleNamespace(crag_compact_input=True)
    with patch.object(generation, "parse_text_response", return_value=output) as parse:
        attempt = generation._grade_crag_retrieval(1, question, question, [question], context, settings, max_candidates=12)
    assert "\n  " not in parse.call_args.args[1]
    # 候補の除外（relevant=false）・関連の候補・列挙の項目の数え直し（3 件 < 5 件で欠け）は詰めた入力でも同じ。
    assert attempt.rejected_chunk_ids == ("x1",)
    assert set(attempt.relevant_chunk_ids) == {"p2", "p12"}
    assert ENUMERATION_ASPECT in attempt.missing_aspects
    entry = next(item for item in attempt.aspect_checks if item["aspect"] == ENUMERATION_ASPECT)
    assert entry["counted_items"] == 3 and entry["status"] == "missing"


# ---- CRAG の評価の候補の短い ID (#1864) -------------------------------------------------------------------------


def test_short_candidate_ids_replace_chunk_uids_in_the_compact_prompt():
    context = _tree_context(15)
    question = "経費精算の申請の期限は？"
    aliases: dict[str, str] = {}
    prompt = crag_support._build_crag_grade_prompt(
        question, question, [question], context, compact=True, max_candidates=12, id_aliases=aliases
    )
    shown = json.loads(_candidates_json(prompt))
    # 候補は並びの順に C1…、検索で当たった子は親の ID に枝番を付ける。短い ID → chunk_uid を返す。
    assert [item["chunk_uid"] for item in shown] == [f"C{n + 1}" for n in range(12)]
    assert all(item["anchor_uids"] == [f"C{n + 1}-1"] for n, item in enumerate(shown))
    assert aliases["C1"] == "p0" and aliases["C1-1"] == "c0" and aliases["C12"] == "p11"
    assert '"p0"' not in _candidates_json(prompt) and '"c0"' not in _candidates_json(prompt)
    # 候補の本文・欄は短い ID のとき以外と同じ（ID だけが違う）。
    plain_prompt = crag_support._build_crag_grade_prompt(question, question, [question], context, compact=True, max_candidates=12)
    plain = json.loads(_candidates_json(plain_prompt))
    assert [{**item, "chunk_uid": "", "anchor_uids": []} for item in shown] == [
        {**item, "chunk_uid": "", "anchor_uids": []} for item in plain
    ]


def test_short_candidate_ids_map_back_to_chunk_uids_in_the_grade():
    question = BUSINESS_DAY_FIVE
    other = _parent("x1", "サーバー室の空調の温度は 25 度以下に保ちます。")
    records = (RESERVE, REPORT, other)
    tree = tuple(ContextParentEvidence(record=item, role="synthesis_parent", reason="retrieved") for item in records)
    context = AnswerContext(records=list(records), text="\n".join(r.text for r in records), evidence_tree=tree)
    # 評価器は短い ID（C1 = RESERVE、C2 = REPORT、C3 = x1）で返す。
    output = CragRetrievalGradeOutput(
        candidate_verdicts=[
            {"chunk_uid": "C1", "relevant": True, "reason": "関連"},
            {"chunk_uid": "C2", "relevant": True, "reason": "関連"},
            {"chunk_uid": "C3", "relevant": False, "reason": "話題が別"},
        ],
        enumerated_items=_items(("C1", "5 営業日前までに"), ("C1", "3 営業日前までに"), ("C2", "3 営業日以内に")),
        aspect_checks=[
            {
                "aspect": "business_object",
                "status": "supported",
                "source_ids": ["C1"],
                "evidence": [{"source_id": "C1", "quote": RESERVE.text}],
                "reason": "関連",
            },
            {"aspect": ENUMERATION_ASPECT, "status": "missing", "source_ids": [], "reason": "5件のうち3件のみ"},
        ],
        reason="足りる",
        sufficient=True,
        confidence=0.9,
        rewritten_query="",
        recovery_anchor_ids=["C2"],
    )
    settings = SimpleNamespace(crag_compact_input=True, crag_short_candidate_ids=True)
    with patch.object(generation, "parse_text_response", return_value=output) as parse:
        attempt = generation._grade_crag_retrieval(1, question, question, [question], context, settings, max_candidates=12)
    assert f'"chunk_uid":"{REPORT.chunk_uid}"' not in parse.call_args.args[1]
    assert '"chunk_uid":"C2"' in parse.call_args.args[1]
    # 短い ID は chunk_uid に戻るので、候補の除外・関連の候補・観点の根拠・列挙の数え直しは長い ID のときと同じ。
    assert attempt.rejected_chunk_ids == ("x1",)
    assert set(attempt.relevant_chunk_ids) == {"p2", "p12"}
    assert [item["source_ids"] for item in attempt.aspect_checks if item["aspect"] == "business_object"] == [["p2"]]
    assert attempt.recovery_anchor_ids == ("p12",)
    assert [verdict["chunk_uid"] for verdict in attempt.candidate_verdicts] == ["p2", "p12", "x1"]
    entry = next(item for item in attempt.aspect_checks if item["aspect"] == ENUMERATION_ASPECT)
    assert entry["counted_items"] == 3 and entry["status"] == "missing"


def test_short_candidate_ids_are_off_by_default_in_the_engine():
    assert get_settings(environ={}, dotenv_path=None).crag_short_candidate_ids is False
    on = get_settings(environ={"RAG_ENGINE_CRAG_SHORT_CANDIDATE_IDS": "1"}, dotenv_path=None)
    assert on.crag_short_candidate_ids is True


# ---- CRAG の評価を曖昧な帯だけで呼ぶ -----------------------------------------------------------------------------

QUESTION = "経費精算の申請の期限と承認者を教えてください"


def _scored(*records: AnswerRecord, score: float = 0.9) -> AnswerContext:
    first = replace(records[0], metadata={**records[0].metadata, "rerank": {"rank": 1, "relevance_score": score}})
    records = (first, *records[1:])
    return AnswerContext(records=list(records), text="\n".join(r.text for r in records), ranked_candidates=(first,))


def _check(context: AnswerContext, question: str = QUESTION, **overrides):
    settings = _engine_settings(crag_skip_on_strong_signals=True, crag_skip_min_rerank_score=0.8, **overrides)
    return generation._crag_grade_skip_check(question, context, settings)


def test_strong_signals_with_partly_matched_requests_skip_the_grade():
    # 多段の問は要求の語が 1 つの根拠にそろわない（一部一致 = weak）。根拠の無い要求が無ければ飛ばす。
    context = _scored(_parent("p1", "経費精算の申請は月末までに行います。"), _parent("p2", "承認は部長が行います。", source="承認規程.pdf"))
    check = _check(context)
    assert check["skip"] is False, check
    assert "充足性の判定は行わない" in check["reason"]


@pytest.mark.parametrize(
    "name, context, question",
    [
        ("low-score", _scored(_parent("p1", "経費精算の申請の期限は月末で、承認者は部長です。"), score=0.5), QUESTION),
        (
            "missing-request",
            _scored(_parent("p1", "経費精算の申請は月末までに行います。")),
            "経費精算の申請の期限と、出張旅費の上限額を教えてください",
        ),
        (
            "enumeration",
            _scored(_parent("p1", "経費精算の申請の期限は月末で、承認者は部長です。")),
            "経費精算の申請で決められた期限をすべて挙げてください",
        ),
        ("open-document-enumeration", _scored(_parent("p1", "作業者は配線に触れません。")), DO_NOT_TOUCH),
        (
            "error-message",
            _scored(_parent("p1", "経費精算で「E0123 申請できません」と表示されたら再申請します。")),
            "「E0123 申請できません」と表示されたときの対処は？",
        ),
        (
            "superseded",
            _scored(_parent("p1", "経費精算の申請の期限は月末で、承認者は部長です。", metadata={"document_superseded": True})),
            QUESTION,
        ),
        (
            "duplicate",
            _scored(
                _parent("p1", "経費精算の申請の期限は月末で、承認者は部長です。" * 3, source="規程.pdf"),
                _parent("p9", "経費精算の申請の期限は月末で、承認者は部長です。" * 3, source="規程-2023.pdf"),
            ),
            QUESTION,
        ),
    ],
    ids=lambda value: value if isinstance(value, str) and len(value) < 30 else "",
)
def test_ambiguous_or_must_call_questions_call_the_grade(name, context, question):
    check = _check(context, question)
    assert check["skip"] is False and check["reason"], name


def test_not_skipped_grade_records_the_reason_in_the_step():
    context = _scored(_parent("p1", "経費精算の申請の期限は月末で、承認者は部長です。"), score=0.5)
    settings = _engine_settings(crag_skip_on_strong_signals=True, crag_skip_min_rerank_score=0.8)
    grade = CragRetrievalGradeOutput(
        candidate_verdicts=[{"chunk_uid": "p1", "relevant": True, "reason": "関連"}],
        aspect_checks=[],
        reason="足りる",
        sufficient=True,
        confidence=0.9,
        rewritten_query="",
    )
    lines: list = []
    token = generation._execution_lines.set(lines)
    try:
        with (
            patch.object(generation, "load_latest_or_source_chunk_run", return_value=SimpleNamespace(run_id="r")),
            patch.object(generation, "build_adb_hybrid_answer_context", return_value=context),
            patch.object(generation, "parse_text_response", return_value=grade) as parse,
        ):
            generation.build_crag_answer_context(QUESTION, "abcdef", ["docling"], settings)
    finally:
        generation._execution_lines.reset(token)
    assert parse.call_count == 1
    display = generation.format_question_display(QUESTION, lines)
    assert "LLM の評価を省略しない理由: rerank の最上位の score 0.500 が閾値 0.800 未満" in display


# ---- 読む範囲（文書全体）まで読んだ後の評価し直しの省略 ----------------------------------------------------------------


def test_open_document_enumeration_skips_the_regrade_after_reading_the_document():
    stub, grader = SectionStub(), Grader(needs=set())

    result = _expand(
        _seed_context("c31a"),
        _section_settings(section_skip_regrade_at_scope=True),
        grader,
        stub,
        last=_attempt(sufficient=True, missing=(ENUMERATION_ASPECT, "applicability")),
        question=DO_NOT_TOUCH,
    )

    # leaf → 第3章 → 文書全体まで読み、文書全体を読んだ後も評価器を呼ばない（上に読む章節が無い）。
    assert stub.read_calls == ["s3-1", "s3", "document"]
    assert grader.calls == []
    assert result.stop_reason == sections.STOP_SCOPE_READ and result.applied and result.unread == []
    assert set(_members("document")) <= {child.record.chunk_uid for parent in result.context.evidence_tree for child in parent.children}
    assert [level.get("grade_skipped") for level in result.trace["levels"]] == ["minimum_scope", "minimum_scope", "scope_reached"]
    # 生成に渡す既知の欠落から、広げる前の判定の内容の観点（列挙の欠け）を除く。適用の観点は残す。
    attempts = (_attempt(sufficient=True, missing=(ENUMERATION_ASPECT, "applicability")),)
    assert generation._known_gaps(attempts, result) == ("applicability",)


def test_leaf_scope_and_counted_enumerations_still_regrade():
    stub, grader = SectionStub(), Grader(needs=set(_members("s3-1")))
    leaf = _expand(
        _seed_context("c31a"),
        _section_settings(section_skip_regrade_at_scope=True),
        grader,
        stub,
        last=_attempt(sufficient=True),
        question="第3章の設備ごとの点検の項目をすべて教えて",
    )
    # 当たった章節が範囲の問は、評価器が足りないと判定すれば親へ広げるので評価し直す。
    assert grader.calls == [1] and leaf.stop_reason == sections.STOP_SUFFICIENT

    stub2, grader2 = SectionStub(), Grader(needs=set(_members("s3")), missing=(ENUMERATION_ASPECT,))
    counted = _expand(
        _seed_context("c31a"),
        _section_settings(section_skip_regrade_at_scope=True),
        grader2,
        stub2,
        last=_attempt(sufficient=True, missing=(ENUMERATION_ASPECT,)),
        question=BUSINESS_DAY_FIVE,
    )
    # 件数のある問は数えた結果で決めるので、読む範囲の規則を使わず評価し直す。
    assert grader2.calls == [1, 2] and counted.stop_reason == sections.STOP_SUFFICIENT


def test_regrade_is_kept_when_the_setting_is_off():
    stub, grader = SectionStub(), Grader(needs=set())
    result = _expand(_seed_context("c31a"), _section_settings(), grader, stub, last=_attempt(sufficient=True), question=DO_NOT_TOUCH)
    assert grader.calls == [3] and result.stop_reason == sections.STOP_SUFFICIENT
