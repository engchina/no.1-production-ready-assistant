"""語の一致と回答可能性を分離し、生成後の不足だけを共有予算で再検索する。"""

from dataclasses import replace
from typing import Any

import pytest
from test_grounded_answer import FakeModel, draft, ids, item, observed_audit
from test_request_coverage import _patch_search, context, parent

from rag_engine.config import get_settings
from rag_engine.dependencies import AnswerDependencies, bind_dependencies
from rag_engine.generation import answering as a
from rag_engine.retrieval.extra_retrieval import ExtraRetrievalBudget
from rag_engine.retrieval.request_coverage import CoverageUsage, RequestTarget, request_rows

QUESTION = "対象Aの設定を変更する方法は？"
OLD_TEXT = "対象Aの設定を変更する機能の概要。手順の記載はありません。"
NEW_TEXT = "対象Aの設定画面を開き、項目を選択して保存ボタンを押します。"


def settings(**kwargs: Any) -> Any:
    kwargs.setdefault("request_coverage_retrieval_enabled", True)
    return replace(get_settings(environ={}, dotenv_path=None), **kwargs)


def cover(ctx, config, *, usage=None, unanswered=None, extra=None, question=QUESTION):
    return a._cover_missing_requests(
        question,
        ctx,
        config,
        run_id="run",
        preferred_engines=["docling"],
        top_k=8,
        neighbor_child_count=0,
        rerank_enabled=False,
        inquiry_conditions=None,
        retrieval_scope="knowledge_base",
        classification_filter=None,
        runtime_knowledge=None,
        searched_queries=("別の初回検索文",),
        usage=usage,
        unanswered_requests=unanswered,
        extra_budget=extra,
    )


def test_high_word_overlap_does_not_assert_answerability():
    target = RequestTarget("Q1", "request", "要求", tuple(f"語{i}" for i in range(8)))
    row = request_rows([target])[0]
    assert row["status"] == "unassessed" and "best_ratio" not in row
    assert row["assessment"] == "not_performed" and row["answerability"] == "unassessed"


def test_post_generation_missing_overrides_high_word_overlap_without_using_reason(monkeypatch):
    calls = _patch_search(monkeypatch, {QUESTION: context(parent("new", NEW_TEXT))})
    monkeypatch.setattr(a, "_coverage_tokenizer", lambda _: lambda text: [term for term in ("対象A", "設定", "変更") if term in text])
    old = context(parent("old", OLD_TEXT))
    merged, trace = cover(old, settings(), unanswered=[{"request_id": "Q1", "status": "missing", "reason": "検索に入れない文"}])
    assert trace["requests"][0]["status"] == "unassessed"
    assert trace["requests"][0]["answer_status"] == "missing"
    assert trace["phase"] == "after_generation"
    assert calls == [(QUESTION, (QUESTION,))]
    assert {r.chunk_uid for r in merged.records} == {"run:old", "run:new"}


def test_pre_and_post_generation_share_all_limits(monkeypatch):
    from test_request_coverage import QUESTION as two_requests
    from test_request_coverage import _settings
    from test_request_coverage import parent as p

    calls = _patch_search(monkeypatch, {"請求書の再発行方法は？": context(p("new", "請求書 再発行"))})
    usage, extra = CoverageUsage(), ExtraRetrievalBudget(5)
    old = context(p("old", "受注 登録"))
    first, _ = cover(
        old,
        _settings(request_coverage_max_queries=1),
        usage=usage,
        extra=extra,
        question=two_requests,
        unanswered=[{"request_id": "Q2", "status": "missing"}],
    )
    assert usage.queries == 1
    second, trace = cover(
        first,
        _settings(request_coverage_max_queries=1),
        usage=usage,
        extra=extra,
        question=two_requests,
        unanswered=[{"request_id": "Q2", "status": "missing"}],
    )
    assert trace["re_retrieval"]["skipped"] == "no_budget"
    assert second is first and len(calls) == 1
    assert extra.used == {"request_coverage": 1}
    assert usage.chunks == 1 and usage.searched_queries


@pytest.mark.parametrize("limit", ["chunks", "deadline", "global"])
def test_exhausted_limits_prevent_search(monkeypatch, limit):
    calls = _patch_search(monkeypatch, {})
    usage = CoverageUsage(chunks=4 if limit == "chunks" else 0, elapsed_seconds=10 if limit == "deadline" else 0)
    _, trace = cover(
        context(parent("old", OLD_TEXT)),
        settings(),
        usage=usage,
        extra=ExtraRetrievalBudget(0 if limit == "global" else 5),
        unanswered=[{"request_id": "Q1", "status": "missing"}],
    )
    assert trace["re_retrieval"]["skipped"] in {"no_budget", "deadline"}
    assert not calls


def _synthesize(monkeypatch, first, recover, **changes):
    old = context(parent("old", OLD_TEXT))
    merged = context(parent("old", OLD_TEXT), parent("new", NEW_TEXT))
    eid = ids(merged, QUESTION)[NEW_TEXT]
    model = FakeModel([first, draft(item(eid, NEW_TEXT, NEW_TEXT, request_id="Q1"), summary="資料に記載の操作です。")], [])
    unused = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected I/O"))  # noqa: E731
    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        result = a.synthesize_grounded_answer(
            QUESTION,
            old,
            settings(**{"answer_audit_mode": "none", **changes}),
            image_prompt_mode="text_only",
            recover_requests=recover,
        )
    return result, model


def gap():
    return draft({"kind": "gap", "text": "操作の根拠がありません。", "request_id": "Q1", "citations": []}, summary="根拠が不足しています。")


def test_generated_gap_can_retrieve_and_regenerate_without_enabling_audit(monkeypatch):
    calls = []

    def recover(ctx, unanswered):
        calls.append(unanswered)
        return context(parent("old", OLD_TEXT), parent("new", NEW_TEXT)), {"applied": True}

    result, model = _synthesize(monkeypatch, gap(), recover)
    assert calls == [[{"request_id": "Q1", "status": "missing"}]]
    assert NEW_TEXT in result.response.answer_text
    assert NEW_TEXT in model.prompts[1][1]
    assert len(model.prompts) == 2
    assert result.response.generation_trace["rounds"][0]["remedy"]["remedy"] == "retrieval"


@pytest.mark.parametrize("kind", ["no_new_evidence", "failure", "zero_rounds"])
def test_recovery_does_not_repeat_generation_without_new_evidence(monkeypatch, kind):
    calls = []

    def recover(ctx, unanswered):
        calls.append(unanswered)
        if kind == "failure":
            raise RuntimeError("検索の例外の本文は記録しない")
        return ctx, {"applied": False}

    result, model = _synthesize(monkeypatch, gap(), recover, answer_max_correction_rounds=0 if kind == "zero_rounds" else 2)
    assert len(model.prompts) == 1
    assert len(calls) == (0 if kind == "zero_rounds" else 1)
    assert "操作の根拠がありません" in result.response.answer_text
    if kind == "failure":
        trace = result.response.generation_trace["rounds"][0]["request_retrieval"]
        assert trace == {"error": "RuntimeError", "applied": False}
        decision = result.response.generation_trace["rounds"][0]["request_recovery"]["decisions"][0]
        assert decision["execution"] == "error" and decision["stop_reason"] == "retrieval_error"


def test_missing_audit_does_not_trigger_search_when_draft_has_no_gap(monkeypatch):
    old = context(parent("old", OLD_TEXT))
    eid = ids(old, QUESTION)[OLD_TEXT]
    calls = []
    result, model = _synthesize(monkeypatch, draft(item(eid, OLD_TEXT, OLD_TEXT, request_id="Q1")), lambda *args: calls.append(args))
    assert len(model.prompts) == 1 and not calls
    assert result.response.answer_text


@pytest.mark.parametrize("enabled", [False, True])
def test_main_answer_flow_wires_recovery_and_respects_disabled_setting(monkeypatch, tmp_path, enabled):
    old = context(parent("old", OLD_TEXT))
    merged = context(parent("old", OLD_TEXT), parent("new", NEW_TEXT))
    eid = ids(merged, QUESTION)[NEW_TEXT]
    model = FakeModel([gap(), draft(item(eid, NEW_TEXT, NEW_TEXT, request_id="Q1"), summary="資料に記載の操作です。")], [])
    calls = []

    def search(question, *args, **kwargs):
        calls.append((question, kwargs.get("retrieval_queries")))
        return old if len(calls) == 1 else context(parent("new", NEW_TEXT))

    monkeypatch.setattr(a, "check_adb_hybrid_search_ready", lambda **kwargs: None)
    monkeypatch.setattr(a, "build_adb_hybrid_answer_context", search)
    monkeypatch.setattr(a, "_coverage_tokenizer", lambda _: lambda text: [term for term in ("対象A", "設定", "変更") if term in text])
    unused = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected I/O"))  # noqa: E731
    config = settings(
        request_coverage_retrieval_enabled=enabled,
        output_dir=tmp_path,
        answer_audit_mode="none",
        section_expansion_enabled=False,
        domain_keywords_override=(),
    )
    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        result = a.answer_question_result(
            "初回に指定された検索文",
            "run",
            ["docling"],
            config,
            query_strategy=a.SIMPLE_RETRIEVAL_LABEL,
            answer_flow=a.STANDARD_ANSWER_FLOW,
            retrieval_scope=a.RETRIEVAL_SCOPE_KNOWLEDGE_BASE,
            request_question=QUESTION,
        )
    assert len(calls) == (2 if enabled else 1)
    assert len(model.prompts) == (2 if enabled else 1)
    if enabled:
        assert calls[1][1] == (QUESTION,)
        trace = result.generation_trace["request_coverage"]["after_generation"][0]
        assert trace["re_retrieval"]["applied"] is True
        assert NEW_TEXT in result.answer
    else:
        assert result.generation_trace["request_coverage"]["re_retrieval"]["skipped"] == "disabled"


def test_same_focused_query_is_not_repeated_across_phases(monkeypatch):
    calls = _patch_search(monkeypatch, {})
    monkeypatch.setattr(a, "_coverage_tokenizer", lambda _: lambda text: [term for term in ("対象A", "設定", "変更") if term in text])
    usage = CoverageUsage()
    old = context(parent("old", OLD_TEXT))
    for _ in range(2):
        _, trace = cover(old, settings(), usage=usage, unanswered=[{"request_id": "Q1", "status": "missing"}])
    assert len(calls) == 1 and usage.queries == 1
    assert trace["re_retrieval"]["skipped"] == "same_query"


def test_raw_audit_material_shortage_does_not_trigger_retrieval_without_generated_gap(monkeypatch):
    from test_grounded_answer import audit

    old = context(parent("old", OLD_TEXT))
    merged = context(parent("old", OLD_TEXT), parent("new", NEW_TEXT))
    old_id, new_id = ids(old, QUESTION)[OLD_TEXT], ids(merged, QUESTION)[NEW_TEXT]
    model = FakeModel(
        [draft(item(old_id, OLD_TEXT, OLD_TEXT, request_id="Q1")), draft(item(new_id, NEW_TEXT, NEW_TEXT, request_id="Q1"))],
        [
            audit((0, "supported", "matched", ""), requests=[("Q1", "partial")]),
            audit((0, "supported", "matched", ""), requests=[("Q1", "addressed")]),
        ],
    )
    model.audits[0]["request_reviews"][0]["evidence_status"] = "missing"
    calls = []

    def recover(ctx, unanswered):
        calls.append(unanswered)
        return merged, {"applied": True}

    unused = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected I/O"))  # noqa: E731
    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        result = a.synthesize_grounded_answer(
            QUESTION, old, settings(answer_audit_mode="detailed"), image_prompt_mode="text_only", recover_requests=recover
        )
    assert calls == []
    assert OLD_TEXT in result.response.answer_text and NEW_TEXT not in result.response.answer_text
    assert len(model.prompts) == 2
    [raw] = observed_audit(result.response)["request_reviews"]
    assert raw["status"] == "partial" and raw["evidence_status"] == "missing"
    [decision] = result.response.generation_trace["rounds"][0]["request_recovery"]["decisions"]
    assert decision["action"] == "retain" and decision["evidence_ids"] == []


def test_unknown_request_does_not_search_unrelated_targets(monkeypatch):
    calls = _patch_search(monkeypatch, {})
    _, trace = cover(context(parent("old", OLD_TEXT)), settings(), unanswered=[{"request_id": "unknown", "status": "missing"}])
    assert not calls
    assert trace["re_retrieval"]["skipped"] == "unmapped_requests"


def test_focused_query_preserves_explicit_target_context():
    from rag_engine.retrieval.request_coverage import CoverageBudget, run_targeted_retrievals

    question = "対象Bについて、設定を変更する方法は？"
    target = RequestTarget("Q1", "request", "設定を変更する方法は？", ("設定", "変更"), query=question, context_terms=("対象b",))
    rows = [{"id": "Q1", "status": "supported", "answer_status": "missing"}]
    calls = []
    run_targeted_retrievals(
        rows,
        [target],
        budget=CoverageBudget(),
        retrieve=lambda text, queries: calls.append((text, queries)),
        tokenize=lambda text: [],
        searched_queries=("別の初回検索文",),
    )
    assert calls == [(question, (question,))]


def test_unknown_child_id_is_not_assigned_to_original_request(monkeypatch):
    calls = _patch_search(monkeypatch, {})
    _, trace = cover(context(parent("old", OLD_TEXT)), settings(), unanswered=[{"request_id": "Q1.fabricated", "status": "missing"}])
    assert calls == []
    assert trace["unmapped_request_ids"] == ["Q1.fabricated"]
    assert trace["re_retrieval"]["skipped"] == "unmapped_requests"


def test_same_query_is_shared_between_distinct_request_ids():
    from rag_engine.retrieval.request_coverage import CoverageBudget, run_targeted_retrievals

    calls = []
    targets = [RequestTarget("Q1", "request", "対象の作成方法は？", ()), RequestTarget("Q2", "request", "対象の作成方法は？", ())]
    rows = [{"id": target.id, "status": "unassessed", "answer_status": "missing"} for target in targets]

    def retrieve(text, queries):
        calls.append(queries)
        return context(parent("shared", NEW_TEXT))

    results, _ = run_targeted_retrievals(rows, targets, budget=CoverageBudget(), retrieve=retrieve, tokenize=lambda text: ())
    assert len(calls) == 1
    assert [result.request_id for result in results] == ["Q1", "Q2"]
    assert [result.reused for result in results] == [False, True]
    assert results[0].parents == results[1].parents


def test_equal_token_sets_preserve_distinct_original_request_ids(monkeypatch):
    from rag_engine.retrieval.request_coverage import coverage_targets

    targets = coverage_targets("設定を作成する方法は？設定を削除する方法は？", lambda text: ["設定"])
    assert [target.id for target in targets] == ["Q1", "Q2"]


@pytest.mark.parametrize("audit_mode", ["none", "standard", "detailed"])
@pytest.mark.parametrize("evidence_status", ["unknown", "available"])
def test_available_or_unverified_material_never_triggers_another_search(monkeypatch, audit_mode, evidence_status):
    from test_grounded_answer import audit

    shown_text = "対象Aの設定画面を開きます。"
    missing_text = "項目を選択して保存ボタンを押します。"
    source_text = shown_text + missing_text
    old = context(parent("old", source_text))
    eid = ids(old, QUESTION)[source_text]
    first = draft(item(eid, shown_text, shown_text, request_id="Q1"))
    assessment = audit((0, "supported", "matched", ""), requests=[("Q1", "partial")])
    assessment["request_reviews"][0].update(
        evidence_status=evidence_status,
        evidence_ids=[eid] if evidence_status == "available" else [],
        missing_content=missing_text,
        evidence_support=[{"evidence_id": eid, "source": old.records[0].source, "quote": missing_text}]
        if evidence_status == "available"
        else [],
    )
    completed = audit((0, "supported", "matched", ""), requests=[("Q1", "addressed")])
    if audit_mode == "standard":
        assessment.pop("unused_evidence_ids")
        completed.pop("unused_evidence_ids")
    model = FakeModel([first], [assessment, completed])
    searches = []
    unused = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected I/O"))  # noqa: E731
    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        result = a.synthesize_grounded_answer(
            QUESTION,
            old,
            settings(answer_audit_mode=audit_mode),
            image_prompt_mode="text_only",
            recover_requests=lambda *args: searches.append(args),
        )
    assert not searches
    generation_calls = [schema for schema, _ in model.prompts if schema == "GroundedDraftWire"]
    assert len(generation_calls) == 1
    assert shown_text in result.response.answer_text
    if audit_mode == "none":
        assert all(schema == "GroundedDraftWire" for schema, _ in model.prompts)
    else:
        decision = result.response.generation_trace["rounds"][0]["request_recovery"]["decisions"][0]
        assert decision["action"] == "retain" and decision["evidence_ids"] == []
        [raw] = observed_audit(result.response)["request_reviews"]
        assert raw["evidence_status"] == evidence_status
        assert raw["evidence_ids"] == ([eid] if evidence_status == "available" else [])
        if evidence_status == "available":
            assert raw["missing_content"] == missing_text
            assert raw["evidence_support"] == [{"evidence_id": eid, "source": old.records[0].source, "quote": missing_text}]
        assert result.response.generation_trace["rounds"][0]["audit_calls"] == 1


@pytest.mark.parametrize("coverage_enabled", [False, True])
@pytest.mark.parametrize("extra_limit", [0, 1])
def test_recovery_setting_and_shared_budget_are_independent_switches(monkeypatch, tmp_path, coverage_enabled, extra_limit):
    old = context(parent("old", OLD_TEXT))
    merged = context(parent("old", OLD_TEXT), parent("new", NEW_TEXT))
    eid = ids(merged, QUESTION)[NEW_TEXT]
    model = FakeModel([gap(), draft(item(eid, NEW_TEXT, NEW_TEXT, request_id="Q1"))], [])
    searches = []

    def search(question, *args, **kwargs):
        searches.append(kwargs.get("retrieval_queries"))
        return old if len(searches) == 1 else context(parent("new", NEW_TEXT))

    monkeypatch.setattr(a, "check_adb_hybrid_search_ready", lambda **kwargs: None)
    monkeypatch.setattr(a, "build_adb_hybrid_answer_context", search)
    monkeypatch.setattr(a, "_coverage_tokenizer", lambda _: lambda text: [term for term in ("対象A", "設定", "変更") if term in text])
    unused = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected I/O"))  # noqa: E731
    config = settings(
        request_coverage_retrieval_enabled=coverage_enabled,
        extra_retrieval_max_queries=extra_limit,
        output_dir=tmp_path,
        answer_audit_mode="none",
        section_expansion_enabled=False,
        domain_keywords_override=(),
    )
    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        result = a.answer_question_result(
            "初回に指定された検索文",
            "run",
            ["docling"],
            config,
            query_strategy=a.SIMPLE_RETRIEVAL_LABEL,
            answer_flow=a.STANDARD_ANSWER_FLOW,
            retrieval_scope=a.RETRIEVAL_SCOPE_KNOWLEDGE_BASE,
            request_question=QUESTION,
        )
    recovered = coverage_enabled and extra_limit > 0
    assert len(searches) == (2 if recovered else 1)
    assert len(model.prompts) == (2 if recovered else 1)
    assert result.answer


def test_crag_consumption_is_shared_with_post_generation_retrieval(monkeypatch):
    old = context(parent("old", OLD_TEXT))
    calls = []
    extra, usage = ExtraRetrievalBudget(1), CoverageUsage()
    first = a.CragRetrievalAttempt(1, QUESTION, (QUESTION,), False, rewritten_query="対象Aの設定を保存する手順は？")
    monkeypatch.setattr(a, "build_adb_hybrid_answer_context", lambda *args, **kwargs: calls.append(kwargs["retrieval_queries"]) or old)
    monkeypatch.setattr(a, "_grade_crag_retrieval", lambda *args, **kwargs: first)
    a.build_crag_answer_context(QUESTION, "run", ["docling"], settings(), extra_budget=extra, coverage_usage=usage)
    assert len(calls) == 2
    assert calls[0] == (QUESTION,)
    assert calls[1] == ("対象Aの設定を保存する手順は？",)
    assert extra.used == {"crag": 1}
    assert usage.elapsed_seconds >= 0
    _, trace = cover(old, settings(), usage=usage, extra=extra, unanswered=[{"request_id": "Q1", "status": "missing"}])
    assert len(calls) == 2
    assert trace["re_retrieval"]["skipped"] == "no_budget"


@pytest.mark.parametrize("audit_mode", ["standard", "detailed"])
def test_invalid_material_references_remain_unverified_without_search_or_rewrite(monkeypatch, audit_mode):
    from test_grounded_answer import audit

    old = context(parent("old", OLD_TEXT))
    eid = ids(old, QUESTION)[OLD_TEXT]
    assessment = audit((0, "supported", "matched", ""), requests=[("Q1", "partial")])
    assessment["request_reviews"][0].update(evidence_status="available", evidence_ids=["fabricated"])
    if audit_mode == "standard":
        assessment.pop("unused_evidence_ids")
    model = FakeModel([draft(item(eid, OLD_TEXT, OLD_TEXT, request_id="Q1"))], [assessment])
    unused = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected I/O"))  # noqa: E731
    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        result = a.synthesize_grounded_answer(
            QUESTION, old, settings(answer_audit_mode=audit_mode), image_prompt_mode="text_only", recover_requests=unused
        )
    assert len([schema for schema, _ in model.prompts if schema == "GroundedDraftWire"]) == 1
    decision = result.response.generation_trace["rounds"][0]["request_recovery"]["decisions"][0]
    assert decision["cause"] == "review_incomplete" and decision["action"] == "retain"
    [raw] = observed_audit(result.response)["request_reviews"]
    # 原応答の不正 ID は診断で追跡し、採用側は未確認・空の根拠へ分離する。
    assert raw["evidence_status"] == "available" and raw["evidence_ids"] == ["fabricated"]
    assert decision["evidence_ids"] == [] and decision["status"] == "unknown"
    assert OLD_TEXT in result.response.answer_text


def test_standard_audit_material_shortage_is_diagnostic_without_search(monkeypatch):
    from test_grounded_answer import audit

    old = context(parent("old", OLD_TEXT))
    merged = context(parent("old", OLD_TEXT), parent("new", NEW_TEXT))
    old_id, new_id = ids(old, QUESTION)[OLD_TEXT], ids(merged, QUESTION)[NEW_TEXT]
    first = audit((0, "supported", "matched", ""), requests=[("Q1", "partial")])
    first["request_reviews"][0]["evidence_status"] = "missing"
    second = audit((0, "supported", "matched", ""), requests=[("Q1", "addressed")])
    for assessment in (first, second):
        assessment.pop("unused_evidence_ids")
    model = FakeModel(
        [draft(item(old_id, OLD_TEXT, OLD_TEXT, request_id="Q1")), draft(item(new_id, NEW_TEXT, NEW_TEXT, request_id="Q1"))],
        [first, second],
    )
    calls = []

    def recover(ctx, unanswered):
        calls.append(unanswered)
        return merged, {"applied": True}

    unused = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected I/O"))  # noqa: E731
    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        result = a.synthesize_grounded_answer(
            QUESTION, old, settings(answer_audit_mode="standard"), image_prompt_mode="text_only", recover_requests=recover
        )
    assert calls == []
    assert len(model.prompts) == 2
    assert OLD_TEXT in result.response.answer_text and NEW_TEXT not in result.response.answer_text
    [raw] = observed_audit(result.response)["request_reviews"]
    assert raw["status"] == "partial" and raw["evidence_status"] == "missing"
    [decision] = result.response.generation_trace["rounds"][0]["request_recovery"]["decisions"]
    assert decision["action"] == "retain" and decision["evidence_ids"] == []


@pytest.mark.parametrize("crag_enabled", [False, True])
@pytest.mark.parametrize("coverage_enabled", [False, True])
def test_two_stage_switch_matrix_uses_existing_generation_and_optional_grading(monkeypatch, tmp_path, crag_enabled, coverage_enabled):
    initial_text = "対象Aの設定画面を開きます。続きの操作の記載はありません。"
    old = context(parent("old", initial_text))
    merged = context(parent("old", initial_text), parent("new", NEW_TEXT))
    new_id = ids(merged, QUESTION)[NEW_TEXT]
    model = FakeModel([gap(), draft(item(new_id, NEW_TEXT, NEW_TEXT, request_id="Q1"))], [])
    searches, grades = [], []

    def search(*args, **kwargs):
        searches.append(kwargs.get("retrieval_queries"))
        return old if len(searches) == 1 else context(parent("new", NEW_TEXT))

    def grade(*args, **kwargs):
        grades.append(1)
        return a.CragRetrievalAttempt(
            1,
            QUESTION,
            tuple(args[3]),
            True,
            generation_usable=True,
            answerable_parts=(
                {
                    "aspect": "requested_result",
                    "kind": "operation",
                    "claim": initial_text,
                    "status": "supported",
                    "evidence": [{"source_id": old.records[0].chunk_uid, "quote": initial_text}],
                },
            ),
        )

    monkeypatch.setattr(a, "check_adb_hybrid_search_ready", lambda **kwargs: None)
    monkeypatch.setattr(a, "build_adb_hybrid_answer_context", search)
    monkeypatch.setattr(a, "_grade_crag_retrieval", grade)
    monkeypatch.setattr(a, "_coverage_tokenizer", lambda _: lambda text: ("設定",))
    unused = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected I/O"))  # noqa: E731
    config = settings(
        request_coverage_retrieval_enabled=coverage_enabled,
        extra_retrieval_max_queries=1,
        output_dir=tmp_path,
        answer_audit_mode="none",
        section_expansion_enabled=False,
        domain_keywords_override=(),
    )
    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        result = a.answer_question_result(
            "初回の検索文",
            "run",
            ["docling"],
            config,
            query_strategy=a.SIMPLE_RETRIEVAL_LABEL,
            answer_flow=a.CRAG_ANSWER_FLOW if crag_enabled else a.STANDARD_ANSWER_FLOW,
            retrieval_scope=a.RETRIEVAL_SCOPE_KNOWLEDGE_BASE,
            request_question=QUESTION,
        )
    assert len(grades) == int(crag_enabled)
    assert len(searches) == 1 + int(coverage_enabled)
    assert len(model.prompts) == 1 + int(coverage_enabled)
    assert result.answer
