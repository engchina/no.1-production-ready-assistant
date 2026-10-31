"""全文検索の超限を原要求の単位で補う。根拠の十分性は判断しない。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from time import monotonic
from typing import Any

from rag_engine.dependencies import report_degraded
from rag_engine.retrieval.extra_retrieval import ExtraRetrievalBudget
from rag_engine.retrieval.request_coverage import CoverageBudget, CoverageUsage, coverage_targets
from rag_engine.retrieval.text_search_tokenizer import TextSearchTokenizationResult, normalize_text_search_text

OVERFLOW_KIND = "query_overflow"


@dataclass(frozen=True)
class OverflowSearch:
    request_id: str
    query: str
    context: Any


def _key(query: str) -> str:
    return " ".join(normalize_text_search_text(query).split()).rstrip("。?？!！")


def search_overflow_requests(
    question: str,
    *,
    tokenize: Callable[[str], TextSearchTokenizationResult],
    retrieve: Callable[[str], Any],
    extra_budget: ExtraRetrievalBudget,
    coverage_budget: CoverageBudget,
    usage: CoverageUsage,
    searched_queries: Sequence[str] = (),
    allow_split: bool = True,
    initial_query_overflowed: bool = False,
    skip_reason: str = "caller_planned",
    clock: Callable[[], float] = monotonic,
) -> tuple[list[OverflowSearch], dict[str, Any]]:
    """元の ID・前置きを保った検索を、追加検索の同じ回数・期限の中で行う。"""
    if not allow_split:
        return [], {"schema_version": 1, "triggered": False, "skipped": skip_reason, "requests": [], "llm_calls": 0}
    try:
        original = tokenize(question)
    except Exception as exc:  # noqa: BLE001 - 語の抽出の失敗で既存根拠を失わない。
        report_degraded("query_overflow", exc)
        return [], {"schema_version": 1, "triggered": False, "skipped": "tokenization_failed", "requests": [], "llm_calls": 0}
    trace: dict[str, Any] = {
        "schema_version": 1,
        "triggered": original.truncated or initial_query_overflowed,
        "initial_query_overflowed": initial_query_overflowed,
        "assessment": "not_performed",
        "llm_calls": 0,
        "original_query_chars": len(original.oracle_text_query),
        "requests": [],
    }
    if not trace["triggered"]:
        return [], trace
    targets = [target for target in coverage_targets(question, lambda text: tokenize(text).tokens) if "." not in target.id]
    started = clock()
    rows = trace["requests"]
    results: list[OverflowSearch] = []
    initial_keys = {_key(query) for query in (*searched_queries, *usage.searched_queries)}
    cache: dict[str, Any] = {}
    deadline = usage.remaining(coverage_budget).deadline_seconds
    for target in targets:
        query = target.query or target.text
        try:
            tokens = tokenize(query)
        except Exception as exc:  # noqa: BLE001 - 未検索を根拠なしと区別する。
            report_degraded("query_overflow", exc)
            rows.append({"request_id": target.id, "query": query, "status": "unsearched_tokenization_failed"})
            continue
        row: dict[str, Any] = {
            "request_id": target.id,
            "query": query,
            "oracle_text_query": tokens.oracle_text_query,
            "query_chars": len(tokens.oracle_text_query),
            "omitted_tokens": list(tokens.truncated_tokens),
            "omitted_protected_tokens": list(tokens.omitted_protected_tokens),
            "answerability": "unassessed",
        }
        rows.append(row)
        key = _key(query)
        if key in cache:
            result = cache[key]
            row.update(status="reused" if result is not None else "reused_failed", candidates=len(getattr(result, "records", ()) or ()))
            if result is not None:
                results.append(OverflowSearch(target.id, query, result))
            continue
        if key in initial_keys:
            row["status"] = "already_searched"
            continue
        # 1 要求と共有条件だけでも識別子が収まらない場合、条件を勝手に組み替えて検索しない。
        if tokens.omitted_protected_tokens or not tokens.oracle_text_query:
            row["status"] = "unsearched_query_overflow"
            continue
        if coverage_budget.max_chunks <= usage.chunks:
            row["status"] = "unsearched_evidence_budget"
            continue
        if extra_budget.remaining <= 0:
            row["status"] = "unsearched_budget"
            continue
        if deadline <= 0 or clock() - started >= deadline:
            row["status"] = "unsearched_deadline"
            continue
        extra_budget.take(OVERFLOW_KIND)
        usage.queries += 1
        usage.searched_queries.append(query)
        cache[key] = None
        try:
            result = retrieve(query)
        except Exception as exc:  # noqa: BLE001 - 失敗を根拠なしにせず記録し、既存の検索結果を維持する。
            row.update(status="failed", error_type=type(exc).__name__)
            continue
        cache[key] = result
        count = len(getattr(result, "records", ()) or ())
        row.update(status="candidate_found" if count else "no_candidates", candidates=count)
        results.append(OverflowSearch(target.id, query, result))
    usage.elapsed_seconds += max(0.0, clock() - started)
    trace["budget"] = extra_budget.to_payload()
    trace["elapsed_ms"] = int(max(0.0, clock() - started) * 1000)
    return results, trace
