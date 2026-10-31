"""要求ごとの根拠の覆域と、根拠の無い要求だけの再検索（#1279）。外部 I/O と LLM は使わない。"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

import pytest

from rag_engine.config import get_settings
from rag_engine.generation import answering as a
from rag_engine.generation.answer_models import AnswerContext, AnswerRecord
from rag_engine.knowledge.runtime_knowledge import RuntimeKnowledgeContext, RuntimeRule, RuntimeTerm
from rag_engine.retrieval.context_builder import ContextChildEvidence, ContextParentEvidence
from rag_engine.retrieval.request_coverage import (
    CoverageBudget,
    RequestTarget,
    TargetedRetrieval,
    coverage_targets,
    re_retrieval_skip_reason,
    request_hints,
    request_rows,
    run_targeted_retrievals,
    select_new_parents,
    targeted_queries,
)

_VOCABULARY = re.compile("請求書|請求|再発行|受注|登録|方法|業務|種別|追加|担当|インボイス|承認|取消|締め|語[0-9]")


def split(text: str) -> list[str]:
    """決定的な分かち書きのスタブ（語彙の一致だけ）。"""
    return _VOCABULARY.findall(text)


def record(uid: str, text: str, level: str = "parent") -> AnswerRecord:
    return AnswerRecord(
        id=uid,
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=1,
        category="Chunk",
        text=text,
        source="manual.pdf",
        source_run_id="source",
        chunk_id=uid,
        chunk_uid=f"run:{uid}",
        chunk_level=level,
    )


def parent(uid: str, text: str, child_text: str = "") -> ContextParentEvidence:
    children = (ContextChildEvidence(record(f"{uid}c", child_text, "child"), "retrieved_anchor", "top"),) if child_text else ()
    return ContextParentEvidence(record=record(uid, text), role="synthesis_parent", reason="top", children=children)


def context(*parents: ContextParentEvidence) -> AnswerContext:
    return AnswerContext(records=[p.record for p in parents], text="x", evidence_tree=tuple(parents))


QUESTION = "受注の登録方法は？請求書の再発行方法は？"


def rows_for(question: str, ctx: AnswerContext) -> tuple[list[RequestTarget], list[dict[str, Any]]]:
    targets = coverage_targets(question, split)
    return targets, request_rows(targets)


def test_request_registration_does_not_assess_evidence() -> None:
    targets, rows = rows_for(QUESTION, context(parent("p1", QUESTION)))
    assert [t.id for t in targets] == ["Q1", "Q2"]
    assert all(row["status"] == "unassessed" and row["assessment"] == "not_performed" for row in rows)
    assert all(not row["evidence_ids"] for row in rows)


def test_background_sentence_is_not_assessed() -> None:
    targets = coverage_targets("先月から担当が変わった。受注の登録方法を教えてください。", split)

    assert [t.kind for t in targets] == ["request"]


def test_parallel_targets_are_assessed_separately() -> None:
    # 「AとBを追加」は対象ごとに判定する（親の文は一方だけで足りてしまう）。
    targets = coverage_targets("業務と種別を追加したい。", split)

    assert {t.text for t in targets} >= {"業務の追加", "種別の追加"}


def test_only_explicit_answer_status_enables_search() -> None:
    rows = [{"id": "Q1", "status": "missing"}]
    assert re_retrieval_skip_reason(rows, CoverageBudget()) == "no_explicit_gap"
    rows[0]["answer_status"] = "missing"
    assert re_retrieval_skip_reason(rows, CoverageBudget()) == ""
    assert re_retrieval_skip_reason(rows, CoverageBudget(enabled=False)) == "disabled"
    assert re_retrieval_skip_reason(rows, CoverageBudget(max_queries=0)) == "no_budget"


def _missing(n: int) -> tuple[list[RequestTarget], list[dict[str, Any]]]:
    targets = [RequestTarget(f"Q{i}", "request", f"要求{i}", (f"語{i}",)) for i in range(1, n + 1)]
    return targets, [{"id": t.id, "status": "unassessed", "answer_status": "missing"} for t in targets]


def test_targeted_retrieval_respects_max_queries() -> None:
    targets, rows = _missing(3)
    calls: list[str] = []

    def retrieve(text: str, queries: tuple[str, ...]) -> AnswerContext:
        calls.append(text)
        return context(parent(f"new-{text}", text))

    results, stop = run_targeted_retrievals(rows, targets, budget=CoverageBudget(max_queries=2), retrieve=retrieve, tokenize=split)

    assert calls == ["要求1", "要求2"]
    assert [r.request_id for r in results] == ["Q1", "Q2"]
    assert stop == "max_queries"


def test_targeted_retrieval_stops_at_deadline() -> None:
    targets, rows = _missing(3)
    now = [0.0]
    calls: list[str] = []

    def retrieve(text: str, queries: tuple[str, ...]) -> AnswerContext:
        calls.append(text)
        now[0] += 6.0  # 1 回の検索に 6 秒かかる
        return context()

    results, stop = run_targeted_retrievals(
        rows, targets, budget=CoverageBudget(max_queries=5, deadline_seconds=10), retrieve=retrieve, tokenize=split, clock=lambda: now[0]
    )

    # 2 回目は 6 秒の時点で始まり（期限内）、3 回目は 12 秒の時点なので始めない。
    assert calls == ["要求1", "要求2"]
    assert [r.elapsed_ms for r in results] == [6000, 6000]
    assert stop == "deadline"


def test_targeted_retrieval_failure_is_recorded_and_continues() -> None:
    targets, rows = _missing(2)

    def retrieve(text: str, queries: tuple[str, ...]) -> AnswerContext:
        if text == "要求1":
            raise RuntimeError("search down")
        return context(parent("new", "語2"))

    results, stop = run_targeted_retrievals(rows, targets, budget=CoverageBudget(), retrieve=retrieve, tokenize=split)

    assert results[0].error.startswith("RuntimeError")
    assert results[0].parents == ()
    assert results[1].parents and not results[1].error
    assert stop == ""


def test_missing_requests_are_searched_before_weak_ones() -> None:
    targets = [RequestTarget(f"Q{i}", "request", f"要求{i}", (f"語{i}", "共通")) for i in range(1, 4)]
    rows = [
        {"id": "Q1", "status": "unassessed", "answer_status": "partial", "terms": ["語1", "共通"], "matched_terms": ["共通"]},
        {"id": "Q2", "status": "supported"},
        {"id": "Q3", "status": "unassessed", "answer_status": "missing"},
    ]
    calls: list[tuple[str, tuple[str, ...]]] = []

    def retrieve(text: str, queries: tuple[str, ...]) -> AnswerContext:
        calls.append((text, queries))
        return context()

    results, stop = run_targeted_retrievals(rows, targets, budget=CoverageBudget(max_queries=1), retrieve=retrieve, tokenize=split)

    # 枠が 1 回なら根拠の無い要求を先に検索する。
    assert [r.request_id for r in results] == ["Q3"]
    assert stop == "max_queries"
    calls.clear()
    run_targeted_retrievals(rows, targets, budget=CoverageBudget(max_queries=2), retrieve=retrieve, tokenize=split)
    # 一部だけの要求は、要求の文に加えて根拠に現れなかった語だけの文でも検索する。
    assert calls == [("要求3", ("要求3",)), ("要求1", ("要求1",))]


def test_single_request_is_searched_only_with_a_new_query() -> None:
    """1 要求の質問は、最初の検索と同じ文では検索せず、違う検索文があるときだけ検索する（#2810）。"""
    target = RequestTarget("Q1", "request", "請求書の再発行方法は？", ("請求書", "再発行", "締め"))
    calls: list[tuple[str, ...]] = []

    def retrieve(text: str, queries: tuple[str, ...]) -> AnswerContext:
        calls.append(queries)
        return context()

    searched = ("請求書の再発行方法は?",)  # 全角・半角の違いは同じ文として扱う
    missing = [{"id": "Q1", "status": "unassessed", "answer_status": "missing", "terms": list(target.terms), "matched_terms": []}]
    results, stop = run_targeted_retrievals(
        missing, [target], budget=CoverageBudget(), retrieve=retrieve, tokenize=split, searched_queries=searched
    )
    assert (results, stop, calls) == ([], "same_query", [])

    weak = [{"id": "Q1", "status": "unassessed", "answer_status": "partial", "terms": list(target.terms), "matched_terms": ["請求書"]}]
    results, stop = run_targeted_retrievals(
        weak, [target], budget=CoverageBudget(), retrieve=retrieve, tokenize=split, searched_queries=searched
    )
    assert calls == []
    assert results == [] and stop == "same_query"


def test_select_new_parents_round_robin_skips_existing_and_caps() -> None:
    first = TargetedRetrieval("Q1", ("a",), (parent("old", "x"), parent("a1", "x"), parent("a2", "x"), parent("a3", "x")))
    second = TargetedRetrieval("Q2", ("b",), (parent("b1", "x"), parent("a1", "x")))

    selected = select_new_parents([first, second], {"run:old"}, max_chunks=3)

    assert [(rid, p.record.chunk_uid) for rid, p in selected] == [("Q1", "run:a1"), ("Q2", "run:b1"), ("Q1", "run:a2")]


def test_hints_come_from_aliases_and_guide_triggers_sharing_request_terms() -> None:
    target = RequestTarget("Q2", "request", "請求書の再発行方法は？", ("請求書", "再発行"))
    knowledge = RuntimeKnowledgeContext(
        original_question=QUESTION,
        expanded_question=QUESTION,
        path=None,  # type: ignore[arg-type]
        matched_terms=(RuntimeTerm(term="請求書", aliases=("インボイス",)), RuntimeTerm(term="受注", aliases=("オーダー",))),
        matched_rules=(RuntimeRule(rule_id="g", title="t", triggers=("再発行 承認", "受注 取消")),),
    )

    hints = request_hints(target, knowledge, split)

    assert hints == ["インボイス", "再発行 承認"]
    assert targeted_queries(target, hints) == ("請求書の再発行方法は？", "請求書の再発行方法は？ インボイス 再発行 承認")
    assert targeted_queries(target, []) == ("請求書の再発行方法は？",)
    # 最初の検索の文と同じ文は除く。
    assert targeted_queries(target, hints, searched=["請求書の再発行方法は？"]) == ("請求書の再発行方法は？ インボイス 再発行 承認",)


def _settings(**changes: Any) -> Any:
    changes.setdefault("request_coverage_retrieval_enabled", True)
    return replace(get_settings(environ={}, dotenv_path=None), **changes)


def _patch_search(monkeypatch: pytest.MonkeyPatch, results: dict[str, AnswerContext]) -> list[tuple[str, tuple]]:
    calls: list[tuple[str, tuple]] = []

    def fake_build(question: str, run_id: Any, engines: Any, settings: Any, **kwargs: Any) -> AnswerContext:
        # 文書の選択と画面目録（LLM）は使わない。
        assert settings.document_selection_enabled is False and settings.screen_linking_enabled is False
        calls.append((question, tuple(kwargs["retrieval_queries"])))
        return results.get(question, context())

    monkeypatch.setattr(a, "build_adb_hybrid_answer_context", fake_build)
    monkeypatch.setattr(a, "_coverage_tokenizer", lambda settings: split)
    return calls


def _cover(
    ctx: AnswerContext, settings: Any, question: str = QUESTION, searched: tuple[str, ...] = ()
) -> tuple[AnswerContext, dict[str, Any]]:
    return a._cover_missing_requests(
        question,
        ctx,
        settings,
        run_id="run",
        preferred_engines=["docling"],
        top_k=8,
        neighbor_child_count=0,
        rerank_enabled=False,
        inquiry_conditions=None,
        retrieval_scope="knowledge_base",
        classification_filter=None,
        runtime_knowledge=None,
        searched_queries=searched,
        unanswered_requests=[{"request_id": "Q2", "status": "missing"}],
    )


def test_cover_missing_requests_adds_evidence_and_records_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_search(
        monkeypatch, {"請求書の再発行方法は？": context(parent("inv", "請求書の再発行は、請求一覧で再発行ボタンを押します。"))}
    )
    ctx = context(parent("p1", "受注番号を入力し、登録ボタンを押します。"))

    merged, trace = _cover(ctx, _settings())

    assert calls == [("請求書の再発行方法は？", ("請求書の再発行方法は？",))]
    # 既存の根拠を先に残し、足した根拠を後ろに置く。
    assert [r.chunk_uid for r in merged.records] == ["run:p1", "run:inv"]
    assert merged.evidence_tree[-1].reason == "request_coverage"
    assert trace["llm_calls"] == 0
    assert trace["counts"] == {"unassessed": 2}
    retrieval = trace["re_retrieval"]
    assert retrieval["applied"] is True
    assert retrieval["added_chunk_ids"] == ["run:inv"]
    assert retrieval["queries"][0]["request_id"] == "Q2"
    assert retrieval["queries"][0]["added_chunk_ids"] == ["run:inv"]
    q2 = next(row for row in trace["requests"] if row["id"] == "Q2")
    assert q2["status"] == "unassessed" and q2["retrieval_status"] == "candidates_found"


def test_before_generation_does_not_search_even_without_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_search(monkeypatch, {})
    ctx = context()
    _, trace = a._cover_missing_requests(
        QUESTION,
        ctx,
        _settings(),
        run_id="r",
        preferred_engines=[],
        top_k=8,
        neighbor_child_count=0,
        rerank_enabled=False,
        inquiry_conditions=None,
        retrieval_scope="knowledge_base",
        classification_filter=None,
        runtime_knowledge=None,
    )
    assert calls == []
    assert trace["re_retrieval"]["skipped"] == "no_explicit_gap"


def test_word_overlap_does_not_trigger_search(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_search(monkeypatch, {})
    ctx = context(parent("p", "取得済みの説明"))
    merged, trace = _cover(ctx, _settings(), "設定を変更する方法は？")
    assert merged is ctx and calls == []
    assert trace["re_retrieval"]["skipped"] == "unmapped_requests"


def test_cover_missing_requests_respects_disabled_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_search(monkeypatch, {})
    ctx = context(parent("p1", "受注番号を入力し、登録ボタンを押します。"))

    merged, trace = _cover(ctx, _settings(request_coverage_retrieval_enabled=False))

    assert calls == []
    assert merged is ctx
    assert trace["re_retrieval"]["skipped"] == "disabled"
    assert {row["id"]: row["status"] for row in trace["requests"]} == {"Q1": "unassessed", "Q2": "unassessed"}


def test_cover_missing_requests_caps_added_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_search(monkeypatch, {"請求書の再発行方法は？": context(*(parent(f"inv{i}", "請求書の再発行") for i in range(5)))})
    ctx = context(parent("p1", "受注番号を入力し、登録ボタンを押します。"))

    merged, trace = _cover(ctx, _settings(request_coverage_max_chunks=2))

    assert [r.chunk_uid for r in merged.records] == ["run:p1", "run:inv0", "run:inv1"]
    assert trace["re_retrieval"]["added_chunk_ids"] == ["run:inv0", "run:inv1"]


def test_cover_missing_requests_no_new_evidence_keeps_context(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = context(parent("p1", "受注番号を入力し、登録ボタンを押します。"))
    _patch_search(monkeypatch, {"請求書の再発行方法は？": ctx})

    merged, trace = _cover(ctx, _settings())

    assert merged is ctx
    assert trace["re_retrieval"]["applied"] is False
    assert trace["re_retrieval"]["added_chunk_ids"] == []
    assert "counts_after" not in trace


def test_subquestion_registration_does_not_trigger_search(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_search(monkeypatch, {})
    question = "対象の設定です。①作成方法は？②削除方法は？"
    _, trace = a._cover_missing_requests(
        question,
        context(),
        _settings(),
        run_id="r",
        preferred_engines=[],
        top_k=8,
        neighbor_child_count=0,
        rerank_enabled=False,
        inquiry_conditions=None,
        retrieval_scope="knowledge_base",
        classification_filter=None,
        runtime_knowledge=None,
    )
    assert calls == [] and len(trace["requests"]) == 2


def test_engine_settings_default_and_env() -> None:
    default = get_settings(environ={}, dotenv_path=None)
    custom = get_settings(
        environ={
            "RAG_ENGINE_REQUEST_COVERAGE_RETRIEVAL": "1",
            "RAG_ENGINE_REQUEST_COVERAGE_MAX_QUERIES": "1",
            "RAG_ENGINE_REQUEST_COVERAGE_MAX_CHUNKS": "3",
            "RAG_ENGINE_REQUEST_COVERAGE_DEADLINE_SECONDS": "2.5",
            "RAG_ENGINE_REQUEST_COVERAGE_MAX_SUBQUESTIONS": "3",
            "RAG_ENGINE_REQUEST_COVERAGE_CHUNKS_PER_SUBQUESTION": "1",
        },
        dotenv_path=None,
    )

    assert default.request_coverage_retrieval_enabled is False
    assert (default.request_coverage_max_queries, default.request_coverage_max_chunks) == (2, 4)
    assert default.request_coverage_deadline_seconds == 10.0
    assert custom.request_coverage_retrieval_enabled is True
    assert (custom.request_coverage_max_queries, custom.request_coverage_max_chunks) == (1, 3)
    assert custom.request_coverage_deadline_seconds == 2.5
    assert (default.request_coverage_max_subquestions, default.request_coverage_chunks_per_subquestion) == (5, 2)
    assert (custom.request_coverage_max_subquestions, custom.request_coverage_chunks_per_subquestion) == (3, 1)


def test_cover_missing_requests_counts_in_the_shared_extra_retrieval_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """再検索の回数は追加の検索の枠（#2395）から取り、枠の残りが無ければ再検索しない。"""
    from rag_engine.retrieval.extra_retrieval import ExtraRetrievalBudget

    calls = _patch_search(
        monkeypatch, {"請求書の再発行方法は？": context(parent("inv", "請求書の再発行は、請求一覧で再発行ボタンを押します。"))}
    )
    ctx = context(parent("p1", "受注番号を入力し、登録ボタンを押します。"))

    def cover(budget: ExtraRetrievalBudget) -> dict[str, Any]:
        return a._cover_missing_requests(
            QUESTION,
            ctx,
            _settings(),
            run_id="run",
            preferred_engines=["docling"],
            top_k=8,
            neighbor_child_count=0,
            rerank_enabled=False,
            inquiry_conditions=None,
            retrieval_scope="knowledge_base",
            classification_filter=None,
            runtime_knowledge=None,
            extra_budget=budget,
            unanswered_requests=[{"request_id": "Q2", "status": "missing"}],
        )[1]

    used = ExtraRetrievalBudget(max_queries=2, used={"guide_steps": 2})
    assert cover(used)["re_retrieval"] == {"applied": False, "skipped": "no_budget"}
    assert calls == []
    left = ExtraRetrievalBudget(max_queries=3, used={"guide_steps": 2})
    assert cover(left)["re_retrieval"]["applied"] is True
    assert left.used == {"guide_steps": 2, "request_coverage": 1} and left.remaining == 0


# #2665 の合成の事例: 番号つきの 3 要求の元の質問と、呼び出し元が検索用に言い換えた検索文（番号を除いた文・
# 検索語の連結）。
NUMBERED_ORIGINAL = "1. 受注の登録方法は？\n2. 請求書の再発行方法は？\n3. 承認の担当は？"
SHORTENED_QUERIES = ("受注を登録する。請求書を再発行する。承認の担当を知りたい。", "受注 登録 請求書 再発行 承認 担当")


def _answer_with_request_question(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, query: str, request_question: str | None
) -> tuple[Any, list[str], list[str]]:
    """検索は「承認」を含む文だけに根拠を返し（前半の 2 要求は根拠なし）、生成は渡された質問を記録する。"""
    from rag_engine.generation.answer_models import AnswerResponse, GroundedAnswer

    searched: list[str] = []
    generated: list[str] = []

    def fake_build(question: str, run_id: Any, engines: Any, settings: Any, **kwargs: Any) -> AnswerContext:
        searched.append(question)
        return context(parent("apr", "承認の担当は課長です。")) if "承認" in question else context()

    def fake_synthesize(question: str, ctx: AnswerContext, settings: Any, **options: Any) -> GroundedAnswer:
        generated.append(question)
        return GroundedAnswer(AnswerResponse(answer_text="承認の担当は課長です。"), ctx, (), options["image_prompt_mode"])

    monkeypatch.setattr(a, "check_adb_hybrid_search_ready", lambda **kwargs: None)
    monkeypatch.setattr(a, "build_adb_hybrid_answer_context", fake_build)
    monkeypatch.setattr(a, "_coverage_tokenizer", lambda settings: split)
    monkeypatch.setattr(a, "synthesize_grounded_answer", fake_synthesize)
    result = a.answer_question_result(
        query,
        "",
        ["docling"],
        replace(_settings(), output_dir=tmp_path),
        query_strategy=a.SIMPLE_RETRIEVAL_LABEL,
        answer_flow=a.STANDARD_ANSWER_FLOW,
        retrieval_scope=a.RETRIEVAL_SCOPE_KNOWLEDGE_BASE,
        request_question=request_question,
    )
    return result, searched, generated


@pytest.mark.parametrize("query", SHORTENED_QUERIES)
def test_requests_come_from_the_original_question_and_search_uses_the_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, query: str
) -> None:
    """要求の単位・要求ごとの根拠の確認・探し直し・回答の生成は元の質問で行い、主の検索は検索文で行う（#2665）。

    前半の 2 要求に根拠が無く最後の要求だけに根拠があるとき、全要求を満たした扱いにせず、要求ごとに不足を示す。
    """
    result, searched, generated = _answer_with_request_question(monkeypatch, tmp_path, query, NUMBERED_ORIGINAL)

    coverage = result.generation_trace["request_coverage"]
    statuses = {row["id"]: (row["kind"], row["status"]) for row in coverage["requests"]}
    assert statuses == {"Q1": ("request", "unassessed"), "Q2": ("request", "unassessed"), "Q3": ("request", "unassessed")}
    # 根拠の無い要求ごとに、元の質問の要求の文で探し直す（1 要求として省かない）。
    retrieval = coverage["re_retrieval"]
    assert retrieval["skipped"] == "no_explicit_gap"
    assert searched == [query]
    # 主の検索は検索文で行い、元の質問の全文では検索しない。
    assert searched[0] == query
    assert NUMBERED_ORIGINAL not in searched
    # 回答の生成と、結果の要求の単位（回答の検査・診断の正本）は元の質問。
    assert generated == [NUMBERED_ORIGINAL]
    assert [unit["kind"] for unit in result.task_contract["request_units"]] == ["request"] * 3


def test_without_request_question_requests_come_from_the_query(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """元の質問を渡さなければ、今までどおり検索文から要求を作り、検索文で回答する（#2665）。"""
    result, searched, generated = _answer_with_request_question(monkeypatch, tmp_path, NUMBERED_ORIGINAL, None)

    assert [row["id"] for row in result.generation_trace["request_coverage"]["requests"]] == ["Q1", "Q2", "Q3"]
    assert searched[0] == NUMBERED_ORIGINAL and generated == [NUMBERED_ORIGINAL]

    short = SHORTENED_QUERIES[1]
    result, _searched, generated = _answer_with_request_question(monkeypatch, tmp_path, short, None)
    assert generated == [short]
    assert result.task_contract == a.task_contract(short)


def test_confirmation_only_rules_do_not_supply_generation_retrieval_hints() -> None:
    target = RequestTarget("Q1", "request", "設定方法", ("設定",))
    knowledge = RuntimeKnowledgeContext(
        original_question="設定方法",
        expanded_question="設定方法",
        path=None,
        matched_rules=(
            RuntimeRule(rule_id="confirm", title="確認", triggers=("設定 確認専用",), purpose="confirmation"),
            RuntimeRule(rule_id="generate", title="生成", triggers=("設定 生成専用",), purpose="generation"),
        ),
    )
    assert request_hints(target, knowledge, lambda text: text.split()) == ["設定 生成専用"]
