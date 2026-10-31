"""代表質問セットで RAG retrieval の命中状況を評価する。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rag_engine.adapters.oracle.store import (
    RETRIEVAL_SCOPE_CURRENT_CHUNK_RUN,
    AdbHybridSearchUnavailable,
    normalize_retrieval_scope,
)
from rag_engine.config import get_settings
from rag_engine.generation.answering import AnswerContext, build_adb_hybrid_answer_context
from rag_engine.knowledge.classification import ClassificationFilter, classification_filter_from_values


@dataclass(frozen=True)
class RagEvalCase:
    """retrieval eval dataset の 1 問と期待条件を表します。"""

    case_id: str
    question: str
    expected_parent_ids: tuple[str, ...] = ()
    expected_child_ids: tuple[str, ...] = ()
    expected_pages: tuple[int, ...] = ()
    expected_terms: tuple[str, ...] = ()
    run_id: str = ""
    reference_answer: str = ""
    classification_filter: ClassificationFilter = field(default_factory=classification_filter_from_values)


@dataclass(frozen=True)
class RagEvalRetrieval:
    """1 問に対する retrieval 結果と context trace を保持します。"""

    parent_ids: tuple[str, ...] = ()
    child_ids: tuple[str, ...] = ()
    pages: tuple[int, ...] = ()
    text: str = ""
    status: str = "ready"
    insufficient_reason: str = ""

    @classmethod
    def from_answer_context(cls, context: AnswerContext) -> RagEvalRetrieval:
        """AnswerContext から評価用 retrieval summary を作ります。"""
        parent_ids: list[str] = []
        child_ids: list[str] = []
        pages: list[int] = []

        for item in context.evidence_tree:
            parent_id = _record_id(item.record)
            if parent_id:
                parent_ids.append(parent_id)
            _append_page_span(pages, getattr(item.record, "page", 0), getattr(item.record, "page_end", 0))
            for child in item.children:
                if child.role == "retrieved_anchor":
                    child_id = _record_id(child.record)
                    if child_id:
                        child_ids.append(child_id)
                    _append_page_span(
                        pages,
                        getattr(child.record, "page", 0),
                        getattr(child.record, "page_end", 0),
                    )

        if not parent_ids and context.records:
            for record in context.records:
                record_id = _record_id(record)
                if record_id:
                    parent_ids.append(record_id)
                _append_page_span(pages, getattr(record, "page", 0), getattr(record, "page_end", 0))

        return cls(
            parent_ids=_dedupe_strings(parent_ids),
            child_ids=_dedupe_strings(child_ids),
            pages=tuple(sorted(set(page for page in pages if page > 0))),
            text=context.text,
            status=context.status,
            insufficient_reason=context.insufficient_reason,
        )


@dataclass(frozen=True)
class RagEvalCaseResult:
    """retrieval eval の 1 問ごとの hit 判定結果を保持します。"""

    case_id: str
    question: str
    retrieved_parent_ids: tuple[str, ...]
    retrieved_child_ids: tuple[str, ...]
    retrieved_pages: tuple[int, ...]
    parent_hit: bool
    child_hit: bool
    page_hit: bool
    term_hit: bool
    status: str = "ready"
    insufficient_reason: str = ""
    # 期待する根拠（child → parent → page の順で判定に使う）が最初に現れた順位（1 始まり）。無ければ None (#861)。
    first_hit_rank: int | None = None

    def to_payload(self) -> dict[str, Any]:
        """UI と trace 保存に使う JSON 互換 payload へ変換します。"""
        return {
            "case_id": self.case_id,
            "question": self.question,
            "retrieved_parent_ids": list(self.retrieved_parent_ids),
            "retrieved_child_ids": list(self.retrieved_child_ids),
            "retrieved_pages": list(self.retrieved_pages),
            "parent_hit": self.parent_hit,
            "child_hit": self.child_hit,
            "page_hit": self.page_hit,
            "term_hit": self.term_hit,
            "status": self.status,
            "insufficient_reason": self.insufficient_reason,
            "first_hit_rank": self.first_hit_rank,
        }


RagEvalRetriever = Callable[[RagEvalCase], AnswerContext | RagEvalRetrieval]


def load_rag_eval_dataset(path: str | Path) -> tuple[RagEvalCase, ...]:
    """retrieval eval dataset JSON を RagEvalCase の列へ読み込みます。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_cases = payload.get("cases") if isinstance(payload, dict) else payload
    if not isinstance(raw_cases, list):
        raise ValueError("RAG eval dataset must contain a cases list.")
    return tuple(_rag_eval_case(item) for item in raw_cases if isinstance(item, dict))


def evaluate_rag_cases(
    cases: Sequence[RagEvalCase],
    retriever: RagEvalRetriever,
) -> dict[str, Any]:
    """retriever を各 eval case に適用して hit 状況を集計します。"""
    results: list[RagEvalCaseResult] = []
    for case in cases:
        retrieval = _coerce_retrieval(retriever(case))
        results.append(_evaluate_case(case, retrieval))

    total = len(results)
    ranked_cases = [case for case in cases if case.expected_child_ids or case.expected_parent_ids or case.expected_pages]
    parent_cases = [case for case in cases if case.expected_parent_ids]
    child_cases = [case for case in cases if case.expected_child_ids]
    page_cases = [case for case in cases if case.expected_pages]
    term_cases = [case for case in cases if case.expected_terms]
    return {
        "case_count": total,
        "parent_cases": len(parent_cases),
        "child_cases": len(child_cases),
        "page_cases": len(page_cases),
        "term_cases": len(term_cases),
        "parent_hit_rate": _rate(sum(1 for result in results if result.parent_hit), len(parent_cases)),
        "child_hit_rate": _rate(sum(1 for result in results if result.child_hit), len(child_cases)),
        "page_hit_rate": _rate(sum(1 for result in results if result.page_hit), len(page_cases)),
        "term_hit_rate": _rate(sum(1 for result in results if result.term_hit), len(term_cases)),
        "ready_rate": _rate(sum(1 for result in results if result.status == "ready"), total),
        # 順位つきの指標。期待 ID / ページを持つ case だけを分母にする (#861)。
        "mrr": _rate(sum(1.0 / result.first_hit_rank for result in results if result.first_hit_rank), len(ranked_cases)),
        "hit_at_k": {
            str(k): _rate(sum(1 for result in results if result.first_hit_rank and result.first_hit_rank <= k), len(ranked_cases))
            for k in HIT_AT_K
        },
        "results": [result.to_payload() for result in results],
    }


def format_rag_eval_summary(summary: dict[str, Any]) -> str:
    """retrieval eval summary を Markdown 表示へ整形します。"""
    return "\n".join(
        [
            f"Parent-child RAG eval cases: {int(summary.get('case_count') or 0)}",
            f"- parent_hit={_pct(summary.get('parent_hit_rate'))} ({int(summary.get('parent_cases') or 0)} cases)",
            f"- child_hit={_pct(summary.get('child_hit_rate'))} ({int(summary.get('child_cases') or 0)} cases)",
            f"- page_hit={_pct(summary.get('page_hit_rate'))} ({int(summary.get('page_cases') or 0)} cases)",
            f"- term_hit={_pct(summary.get('term_hit_rate'))} ({int(summary.get('term_cases') or 0)} cases)",
            f"- mrr={float(summary.get('mrr') or 0.0):.3f}",
            "- hit@k=" + " ".join(f"{k}:{_pct(value)}" for k, value in (summary.get("hit_at_k") or {}).items()),
            f"- ready={_pct(summary.get('ready_rate'))}",
        ]
    )


def run_rag_eval(
    *,
    dataset_path: str | Path,
    run_id: str = "",
    preferred_engines: Sequence[str] = ("docling",),
    retrieval_scope: str = RETRIEVAL_SCOPE_CURRENT_CHUNK_RUN,
    top_k: int = 8,
    output_json_path: str | Path | None = None,
) -> dict[str, Any]:
    """設定済み ADB hybrid search で retrieval eval dataset を実行します。"""
    settings = get_settings()
    cases = load_rag_eval_dataset(dataset_path)
    scope = normalize_retrieval_scope(retrieval_scope)

    def retrieve(case: RagEvalCase) -> AnswerContext:
        case_run_id = case.run_id or run_id
        if scope == RETRIEVAL_SCOPE_CURRENT_CHUNK_RUN and not case_run_id:
            raise ValueError("run_id is required for current-file RAG eval cases.")
        return build_adb_hybrid_answer_context(
            case.question,
            case_run_id,
            preferred_engines,
            settings,
            top_k=top_k,
            retrieval_scope=scope,
            classification_filter=case.classification_filter,
        )

    summary = evaluate_rag_cases(cases, retrieve)
    if output_json_path is not None:
        Path(output_json_path).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 引数を解析し、retrieval eval を実行します。"""
    parser = argparse.ArgumentParser(description="Evaluate parent-child RAG retrieval hit rates.")
    parser.add_argument("--dataset", required=True, help="Path to a JSON/JSONL-style cases file.")
    parser.add_argument("--run-id", default="", help="Source run_id for current-file retrieval scope.")
    parser.add_argument("--engines", default="docling", help="Comma-separated engine ids.")
    parser.add_argument("--scope", default=RETRIEVAL_SCOPE_CURRENT_CHUNK_RUN, help="Retrieval scope.")
    parser.add_argument("--top-k", type=int, default=8, help="Child anchor top-k.")
    parser.add_argument("--json-out", default="", help="Optional path for the JSON summary.")
    args = parser.parse_args(argv)

    engines = tuple(part.strip() for part in str(args.engines or "").split(",") if part.strip())
    try:
        summary = run_rag_eval(
            dataset_path=args.dataset,
            run_id=args.run_id,
            preferred_engines=engines or ("docling",),
            retrieval_scope=args.scope,
            top_k=args.top_k,
            output_json_path=args.json_out or None,
        )
    except AdbHybridSearchUnavailable as exc:
        print(f"ADB hybrid search is unavailable: {exc}", file=sys.stderr)
        return 2
    print(format_rag_eval_summary(summary))
    return 0


def _rag_eval_case(raw: dict[str, Any]) -> RagEvalCase:
    classification = raw.get("classification_filter")
    if not isinstance(classification, dict):
        classification = {}
    return RagEvalCase(
        case_id=str(raw.get("case_id") or raw.get("id") or "").strip(),
        question=str(raw.get("question") or "").strip(),
        reference_answer=str(raw.get("reference_answer") or "").strip(),
        expected_parent_ids=tuple(_string_list(raw.get("expected_parent_ids"))),
        expected_child_ids=tuple(_string_list(raw.get("expected_child_ids"))),
        expected_pages=tuple(_int_list(raw.get("expected_pages"))),
        expected_terms=tuple(_string_list(raw.get("expected_terms"))),
        run_id=str(raw.get("run_id") or "").strip(),
        classification_filter=classification_filter_from_values(
            large_category=classification.get("large_category"),
            middle_category=classification.get("middle_category"),
            small_category=classification.get("small_category"),
        ),
    )


# hit@k を出す k。top_k（既定 8）以下の値と、広めの 10 を含める。
HIT_AT_K = (1, 3, 5, 10)


def _first_hit_rank(case: RagEvalCase, retrieval: RagEvalRetrieval) -> int | None:
    """期待 child / parent / ページのうち case が持つものの、検索結果での最初の順位（1 始まり）。"""
    if case.expected_child_ids:
        expected = {_key(value) for value in case.expected_child_ids if _key(value)}
        ranked = [_key(value) for value in retrieval.child_ids]
    elif case.expected_parent_ids:
        expected = {_key(value) for value in case.expected_parent_ids if _key(value)}
        ranked = [_key(value) for value in retrieval.parent_ids]
    elif case.expected_pages:
        expected = {int(page) for page in case.expected_pages}
        ranked = list(retrieval.pages)
    else:
        return None
    for rank, value in enumerate(ranked, start=1):
        if value in expected:
            return rank
    return None


def _evaluate_case(case: RagEvalCase, retrieval: RagEvalRetrieval) -> RagEvalCaseResult:
    return RagEvalCaseResult(
        case_id=case.case_id,
        question=case.question,
        retrieved_parent_ids=retrieval.parent_ids,
        retrieved_child_ids=retrieval.child_ids,
        retrieved_pages=retrieval.pages,
        parent_hit=_hit(case.expected_parent_ids, retrieval.parent_ids),
        child_hit=_hit(case.expected_child_ids, retrieval.child_ids),
        page_hit=_int_hit(case.expected_pages, retrieval.pages),
        term_hit=_terms_hit(case.expected_terms, retrieval.text),
        status=retrieval.status,
        insufficient_reason=retrieval.insufficient_reason,
        first_hit_rank=_first_hit_rank(case, retrieval),
    )


def _coerce_retrieval(value: AnswerContext | RagEvalRetrieval) -> RagEvalRetrieval:
    if isinstance(value, RagEvalRetrieval):
        return value
    return RagEvalRetrieval.from_answer_context(value)


def _record_id(record: Any) -> str:
    return str(getattr(record, "chunk_id", "") or getattr(record, "id", "") or "").strip()


def _append_page_span(pages: list[int], page_start: Any, page_end: Any) -> None:
    start = _int_value(page_start)
    end = _int_value(page_end) or start
    if start is None or start <= 0:
        return
    if end is None or end < start:
        end = start
    pages.extend(range(start, end + 1))


def _hit(expected: Sequence[str], retrieved: Sequence[str]) -> bool:
    expected_keys = {_key(value) for value in expected if _key(value)}
    if not expected_keys:
        return False
    retrieved_keys = {_key(value) for value in retrieved if _key(value)}
    return bool(expected_keys & retrieved_keys)


def _int_hit(expected: Sequence[int], retrieved: Sequence[int]) -> bool:
    expected_values = {int(value) for value in expected if int(value) > 0}
    if not expected_values:
        return False
    retrieved_values = {int(value) for value in retrieved if int(value) > 0}
    return bool(expected_values & retrieved_values)


def _terms_hit(expected: Sequence[str], text: str) -> bool:
    expected_terms = [_key(value) for value in expected if _key(value)]
    if not expected_terms:
        return False
    comparable = _key(text)
    return all(term in comparable for term in expected_terms)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        values = []
    return [str(item).strip() for item in values if str(item).strip()]


def _int_list(value: Any) -> list[int]:
    if isinstance(value, int):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        values = []
    items: list[int] = []
    for item in values:
        number = _int_value(item)
        if number is not None and number > 0:
            items.append(number)
    return items


def _dedupe_strings(values: Sequence[str]) -> tuple[str, ...]:
    selected: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = _key(text)
        if not key or key in seen:
            continue
        selected.append(text)
        seen.add(key)
    return tuple(selected)


def _key(value: Any) -> str:
    return "".join(str(value or "").casefold().split())


def _int_value(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _rate(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def _pct(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    return f"{number * 100:.1f}%"


if __name__ == "__main__":
    raise SystemExit(main())
