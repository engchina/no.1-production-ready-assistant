"""固定質問と登録語 snapshot を使い、辞書有無の検索・回答を対照評価する CLI。"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from rag_engine.adapters.oracle.store import AdbHybridSearchUnavailable
from rag_engine.chunking import load_latest_or_source_chunk_run
from rag_engine.config import get_settings
from rag_engine.evaluation.rag_eval import RagEvalCase, load_rag_eval_dataset
from rag_engine.generation.answering import (
    AnswerContext,
    _synthesize_answer_from_context,
    build_adb_hybrid_answer_context,
    build_question_text_search_info,
)
from rag_engine.knowledge.domain_keywords import load_domain_keywords


def score_context(case: RagEvalCase, context: AnswerContext, top_k: int) -> dict[str, Any]:
    """近傍 chunk を除く anchor の実順位から Recall@K と reciprocal rank を算出する。

    child ID の正解ラベルがない場合は null とし、本文の語一致を正解率に代用しない。
    """
    anchors = sorted(
        (
            child
            for parent in context.evidence_tree
            for child in parent.children
            if child.role == "retrieved_anchor" and child.retrieval_rank is not None
        ),
        key=lambda child: child.retrieval_rank,
    )
    ranked = [{"child_id": str(child.record.chunk_id), "rank": child.retrieval_rank} for child in anchors if child.retrieval_rank <= top_k]
    expected = set(case.expected_child_ids)
    hits = {item["child_id"] for item in ranked} & expected
    ranks = [item["rank"] for item in ranked if item["child_id"] in expected]
    return {
        "status": context.status,
        "insufficient_reason": context.insufficient_reason,
        "ranked_anchors": ranked,
        "recall_at_k": len(hits) / len(expected) if expected else None,
        "reciprocal_rank": 1 / min(ranks) if ranks else (0.0 if expected else None),
        "evidence_term_hit": (
            all(term.casefold() in context.text.casefold() for term in case.expected_terms) if case.expected_terms else None
        ),
    }


def summarize_pairs(results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """両条件で値がある質問だけを集計し、未評価件数と退化事例を保持する。

    answer_correct は人手レビューで true/false を記入し、未評価は null とする。
    不正なラベル型は ValueError を送出する。
    """
    summary: dict[str, Any] = {}
    for metric in ("recall_at_k", "reciprocal_rank", "answer_correct"):
        pairs = []
        for row in results:
            left, right = row["without_keywords"].get(metric), row["with_keywords"].get(metric)
            if metric == "answer_correct" and any(v is not None and type(v) is not bool for v in (left, right)):
                raise ValueError("answer_correct must be true, false, or null")
            if left is not None and right is not None:
                pairs.append((row["case_id"], float(left), float(right)))
        count = len(pairs)
        summary[metric] = {
            "paired_cases": count,
            "unscored_cases": len(results) - count,
            "without_keywords": sum(p[1] for p in pairs) / count if count else None,
            "with_keywords": sum(p[2] for p in pairs) / count if count else None,
            "delta": sum(p[2] - p[1] for p in pairs) / count if count else None,
            "regressions": [p[0] for p in pairs if p[2] < p[1]],
            "improvements": [p[0] for p in pairs if p[2] > p[1]],
        }
    return summary


def run_keyword_comparison(
    dataset_path: str | Path,
    run_id: str,
    *,
    top_k: int = 8,
    engines: Sequence[str] = ("docling",),
    generate_answers: bool = False,
) -> dict[str, Any]:
    """ADB を読み取り、同じ chunk run・質問・設定で辞書有無を比較する。

    辞書は一度だけ読み、Settings の不変 override で各条件へ渡す。共有辞書は変更しない。
    回答生成を指定した場合は設定済み LLM に接続する。接続失敗は伝播し、零点に変換しない。
    評価中は対象 corpus を更新しないこと。LLM の非決定性は残るため回答は人手採点する。
    """
    if top_k < 1:
        raise ValueError("top_k must be positive")
    settings = get_settings()
    cases = load_rag_eval_dataset(dataset_path)
    if not cases or any(not case.case_id or not case.question for case in cases):
        raise ValueError("Dataset must contain nonempty case IDs and questions")
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("case_id must be unique")
    keywords = tuple(load_domain_keywords(settings.output_dir))
    if not keywords:
        raise ValueError("Register domain keywords before comparing with an empty dictionary")
    # latest の再解決で条件間の chunk run が変わらないよう、開始時に ID を固定する。
    pinned_runs = {}
    for source_id in {case.run_id or run_id for case in cases}:
        if not source_id:
            raise ValueError("run_id is required")
        chunk_run = load_latest_or_source_chunk_run(settings.output_dir, source_id)
        if chunk_run is None:
            raise ValueError(f"No chunk run for {source_id}")
        pinned_runs[source_id] = chunk_run.chunk_run_id
    results = []
    for index, case in enumerate(cases):
        row: dict[str, Any] = {
            "case_id": case.case_id,
            "question": case.question,
            "expected_child_ids": list(case.expected_child_ids),
            "reference_answer": case.reference_answer,
            "chunk_run_id": pinned_runs[case.run_id or run_id],
        }
        # 呼出順による cache・外部サービス負荷の偏りを抑える。
        conditions = ("without_keywords", "with_keywords")
        if index % 2:
            conditions = tuple(reversed(conditions))
        for condition in conditions:
            variant = replace(
                settings,
                domain_keywords_override=keywords if condition == "with_keywords" else (),
            )
            expansion_trace: dict[str, Any] = {}
            queries = (case.question,)
            info = build_question_text_search_info(case.question, variant, query_variants=queries)
            if info.error:
                raise ValueError(info.error)
            context = build_adb_hybrid_answer_context(
                case.question,
                row["chunk_run_id"],
                engines,
                variant,
                top_k=top_k,
                retrieval_queries=queries,
                rerank_enabled=settings.default_rerank_enabled,
                classification_filter=case.classification_filter,
                pinned_chunk_run_id=row["chunk_run_id"],
            )
            result = score_context(case, context, top_k)
            result.update(
                retrieval_queries=list(queries),
                tokenization_traces=list(info.tokenization_traces),
                domain_keyword_expansion=expansion_trace,
                evidence=context.text,
                answer=None,
                answer_correct=None,
                review_notes="",
            )
            if generate_answers:
                result["answer"] = (
                    _synthesize_answer_from_context(
                        case.question,
                        context,
                        variant,
                        image_prompt_mode="text_only",
                    ).answer_text
                    if context.text.strip()
                    else "検索結果なし"
                )
            row[condition] = result
        results.append(row)
    dataset_bytes = Path(dataset_path).read_bytes()
    return {
        "schema_version": 1,
        "manifest": {
            "dataset_sha256": hashlib.sha256(dataset_bytes).hexdigest(),
            "cases": [asdict(case) for case in cases],
            "keywords": list(keywords),
            "pinned_runs": pinned_runs,
            "top_k": top_k,
            "engines": list(engines),
            "rerank_enabled": settings.default_rerank_enabled,
            "rerank_model": settings.rerank_model,
            "rerank_min_relevance_score": settings.rerank_min_relevance_score,
            "embedding_model": settings.embedding_model,
            "embedding_output_dimensions": settings.embedding_output_dimensions,
            "image_embedding_enabled": settings.image_embedding_enabled,
            "image_embedding_rrf_weight": settings.image_embedding_rrf_weight,
            "tokenizer": info.tokenizer,
            "tokenizer_fingerprint": info.tokenizer_fingerprint,
            "generate_answers": generate_answers,
            "answer_model": settings.llm_providers[settings.default_answer_llm].model,
            "protocol": "current chunk run; fixed original query plus lexical rules; no LLM query expansion; text-only answers",
        },
        "review_rubric": "正解参照と根拠を照合し、質問への正確・十分な回答で根拠のない主張がなければ answer_correct=true。"
        "不正解は false、未評価は null。",
        "results": results,
        "summary": summarize_pairs(results),
    }


def main(argv: Sequence[str] | None = None) -> int:
    """比較実行または人手採点済み JSON の再集計を行い、指定ファイルへ保存する。"""
    parser = argparse.ArgumentParser(description="Compare domain keyword retrieval and answers.")
    parser.add_argument("--dataset")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--engines", default="docling")
    parser.add_argument("--generate-answers", action="store_true")
    parser.add_argument("--reviewed-report", help="Recompute metrics after filling answer_correct labels.")
    parser.add_argument("--json-out", required=True)
    args = parser.parse_args(argv)
    try:
        if args.reviewed_report:
            report = json.loads(Path(args.reviewed_report).read_text(encoding="utf-8"))
            report["summary"] = summarize_pairs(report["results"])
        else:
            if not args.dataset:
                parser.error("--dataset is required for comparison")
            engines = tuple(x.strip() for x in args.engines.split(",") if x.strip())
            if not engines:
                parser.error("--engines must contain at least one engine")
            report = run_keyword_comparison(
                args.dataset,
                args.run_id,
                top_k=args.top_k,
                engines=engines,
                generate_answers=args.generate_answers,
            )
        Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except (ValueError, OSError, AdbHybridSearchUnavailable) as exc:
        parser.exit(2, f"{exc}\n")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
