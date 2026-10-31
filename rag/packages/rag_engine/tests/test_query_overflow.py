"""検索超限の要求 ID・実行事実・共有予算を守る回帰。"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from rag_engine.retrieval.extra_retrieval import ExtraRetrievalBudget
from rag_engine.retrieval.query_overflow import search_overflow_requests
from rag_engine.retrieval.request_coverage import CoverageBudget, CoverageUsage, RequestTarget
from rag_engine.retrieval.text_search_tokenizer import TextSearchTokenizationResult


def _tokens(text):
    return TextSearchTokenizationResult(tokens=(text,), oracle_text_query="{語}", truncated_count=int(text == "原質問"))


def _target(key, query):
    return RequestTarget(id=key, text=query, kind="request", terms=())


def _run(targets, *, retrieve=None, maximum=8, usage=None, deadline=10, clock=lambda: 0, tokenize=_tokens):
    budget = ExtraRetrievalBudget(maximum)
    used = usage or CoverageUsage()
    with patch("rag_engine.retrieval.query_overflow.coverage_targets", return_value=targets):
        results, trace = search_overflow_requests(
            "原質問",
            tokenize=tokenize,
            retrieve=retrieve or (lambda text: SimpleNamespace(records=[text])),
            extra_budget=budget,
            coverage_budget=CoverageBudget(deadline_seconds=deadline),
            usage=used,
            clock=clock,
        )
    return results, trace, budget, used


def test_same_query_different_request_ids_reuses_result_and_budget():
    calls = []

    def retrieve(query):
        calls.append(query)
        return SimpleNamespace(records=["候補"])

    results, trace, budget, usage = _run([_target("Q1", "条件A 要求A"), _target("Q2", "条件A 要求A")], retrieve=retrieve)
    assert calls == ["条件A 要求A"]
    assert [result.request_id for result in results] == ["Q1", "Q2"]
    assert [row["status"] for row in trace["requests"]] == ["candidate_found", "reused"]
    assert budget.remaining == 7
    assert usage.queries == 1
    assert all(row["answerability"] == "unassessed" for row in trace["requests"])


def test_shared_budget_and_previously_searched_query_remain_visible():
    _, trace, budget, used = _run(
        [_target("Q1", "既検索"), _target("Q2", "新検索"), _target("Q3", "未検索")],
        maximum=1,
        usage=CoverageUsage(searched_queries=["既検索"]),
    )
    assert [row["status"] for row in trace["requests"]] == ["already_searched", "candidate_found", "unsearched_budget"]
    assert budget.used == {"query_overflow": 1}
    assert used.searched_queries == ["既検索", "新検索"]


def test_failures_empty_results_and_deadline_are_distinct():
    times = iter([0, 0, 0, 11, 11, 11])

    def retrieve(query):
        if query == "失敗":
            raise RuntimeError("秘密の詳細")
        return SimpleNamespace(records=[])

    _, trace, budget, usage = _run(
        [_target("Q1", "失敗"), _target("Q2", "空"), _target("Q3", "期限")], retrieve=retrieve, clock=lambda: next(times)
    )
    assert [row["status"] for row in trace["requests"]] == ["failed", "no_candidates", "unsearched_deadline"]
    assert budget.used == {"query_overflow": 2}
    assert usage.elapsed_seconds == 11
    assert "秘密" not in str(trace)


def test_protected_overflow_does_not_silently_recombine_conditions():
    def tokenize(text):
        return replace(_tokens(text), omitted_protected_tokens=("ID-999",))

    results, trace, budget, _ = _run([_target("Q1", "条件と要求")], tokenize=tokenize)
    assert not results
    assert trace["requests"][0]["status"] == "unsearched_query_overflow"
    assert trace["requests"][0]["omitted_protected_tokens"] == ["ID-999"]
    assert budget.remaining == 8


def test_no_overflow_does_not_run_extra_search():
    results, trace, budget, _ = _run([_target("Q1", "対象")], tokenize=lambda text: replace(_tokens(text), truncated_count=0))
    assert not results
    assert trace["triggered"] is False
    assert not trace["requests"]
    assert budget.remaining == 8


def test_original_request_and_shared_preamble_are_used_without_model():
    question = "対象は ID-123、版は v2.3 です。\n(1) 接続の手順を教えてください。\n(2) 設定の確認方法を教えてください。"
    queries = []

    def tokenize(text):
        return TextSearchTokenizationResult(tokens=("語",), oracle_text_query="{語}", truncated_count=int(text == question))

    results, trace = search_overflow_requests(
        question,
        tokenize=tokenize,
        retrieve=lambda text: queries.append(text) or SimpleNamespace(records=[]),
        extra_budget=ExtraRetrievalBudget(8),
        coverage_budget=CoverageBudget(),
        usage=CoverageUsage(),
    )
    assert len(results) == 2
    assert len({result.request_id for result in results}) == 2
    assert all("ID-123" in query and "v2.3" in query for query in queries)
    assert trace["llm_calls"] == 0


def test_answer_flow_keeps_scope_existing_evidence_and_overflow_trace(monkeypatch):
    from grounded_stub import echo_model
    from test_answer_generation import _chunk_run_stub
    from test_request_coverage import context, parent

    from rag_engine.config import get_settings
    from rag_engine.generation import answering as a
    from rag_engine.retrieval.scope import RETRIEVAL_SCOPE_KNOWLEDGE_BASE

    question = "対象は ID-123 です。\n(1) 接続の手順を教えてください。\n(2) 設定の確認方法を教えてください。"
    original = context(parent("old", "既存の根拠"))
    retrieved = context(parent("new", "追加した操作手順の根拠"))
    calls = []

    def tokenize(text, **kwargs):
        return TextSearchTokenizationResult(tokens=("語",), oracle_text_query="{語}", truncated_count=int(text == question))

    def search(text, *args, **kwargs):
        calls.append((text, args, kwargs))
        return original if len(calls) == 1 else retrieved

    monkeypatch.setattr(a, "load_latest_or_source_chunk_run", lambda *a, **k: _chunk_run_stub())
    monkeypatch.setattr(a, "check_adb_hybrid_search_ready", lambda *a, **k: None)
    monkeypatch.setattr(a, "tokenize_text_search_query_with_trace", tokenize)
    monkeypatch.setattr(a, "build_adb_hybrid_answer_context", search)
    monkeypatch.setattr(a, "parse_text_response", echo_model)
    result = a.answer_question_result(
        question,
        "abcdef",
        ["docling"],
        replace(get_settings(environ={}, dotenv_path=None), extra_retrieval_max_queries=1),
        query_strategy=a.SIMPLE_RETRIEVAL_LABEL,
        answer_flow=a.STANDARD_ANSWER_FLOW_LABEL,
        retrieval_scope=RETRIEVAL_SCOPE_KNOWLEDGE_BASE,
        generate_answer=False,
    )
    assert len(calls) == 2
    assert "ID-123" in calls[1][0]
    assert calls[1][2]["retrieval_scope"] == RETRIEVAL_SCOPE_KNOWLEDGE_BASE
    assert calls[1][1][2].document_selection_enabled is False
    trace = result.generation_trace["query_overflow"]
    assert [row["status"] for row in trace["requests"]] == ["candidate_found", "unsearched_budget"]
    assert trace["added_chunk_ids"] == ["run:new"]
    assert {item["chunk_uid"] for item in result.evidence_items} >= {"run:old", "run:new"}


def test_failed_queries_are_not_executed_again_for_another_request():
    calls = []

    def retrieve(query):
        calls.append(query)
        raise RuntimeError("詳細は公開しない")

    _, trace, budget, _ = _run([_target("Q1", "同じ検索"), _target("Q2", "同じ検索")], retrieve=retrieve)
    assert calls == ["同じ検索"]
    assert [row["status"] for row in trace["requests"]] == ["failed", "reused_failed"]
    assert budget.remaining == 7


def test_caller_planned_queries_are_not_implicitly_expanded():
    def tokenize(text):
        raise AssertionError("呼び出さない")

    results, trace = search_overflow_requests(
        "原質問",
        tokenize=tokenize,
        retrieve=lambda _: None,
        extra_budget=ExtraRetrievalBudget(8),
        coverage_budget=CoverageBudget(),
        usage=CoverageUsage(),
        allow_split=False,
    )
    assert results == []
    assert trace["skipped"] == "caller_planned"


def test_exhausted_evidence_budget_does_not_spend_queries():
    results, trace, budget, _ = _run([_target("Q1", "新検索")], usage=CoverageUsage(chunks=4))
    assert not results
    assert trace["requests"][0]["status"] == "unsearched_evidence_budget"
    assert budget.remaining == 8


def test_alias_expansion_overflow_can_search_the_unexpanded_original_request():
    queries = []
    with patch("rag_engine.retrieval.query_overflow.coverage_targets", return_value=[_target("Q1", "元の要求")]):
        results, trace = search_overflow_requests(
            "元の要求",
            tokenize=lambda text: replace(_tokens(text), truncated_count=0),
            retrieve=lambda text: queries.append(text) or SimpleNamespace(records=["根拠"]),
            extra_budget=ExtraRetrievalBudget(8),
            coverage_budget=CoverageBudget(),
            usage=CoverageUsage(),
            searched_queries=["元の要求 大量の登録別名"],
            initial_query_overflowed=True,
        )
    assert queries == ["元の要求"]
    assert results[0].request_id == "Q1"
    assert trace["initial_query_overflowed"] is True
