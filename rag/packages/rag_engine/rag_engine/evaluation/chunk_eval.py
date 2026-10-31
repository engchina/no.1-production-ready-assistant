"""チャンク分割サイズを同じ解析入力で比較する、ADB を更新しない監査・OCI 検索評価 CLI。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from rag_engine.adapters.oci import embed_query, embed_texts, rerank_text_with_scores
from rag_engine.chunking import (
    CHILD_CHUNK_LEVEL,
    ChunkingConfig,
    DocumentChunk,
    audit_chunk_retrieval_text,
    build_small_to_big_chunks,
    load_latest_chunk_run,
)
from rag_engine.config import Settings, get_settings
from rag_engine.generation.answering import _chunk_answer_record, _rerank_document

TokenCounter = Callable[[str], int]


def load_token_counter(path: str | Path) -> tuple[TokenCounter, str]:
    """指定モデル用のローカル tokenizer JSON を読み、切捨てなしの計数器と SHA-256 を返します。

    tokenizers optional dependency が必要です。モデルの対応確認は呼び出し元の責任で、
    ネットワーク取得はしません。計数には tokenizer 定義の special tokens を含みます。
    """
    from tokenizers import Tokenizer

    tokenizer_path = Path(path)
    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return lambda text: len(tokenizer.encode(text).ids), hashlib.sha256(tokenizer_path.read_bytes()).hexdigest()


def audit_variants(
    payloads: Sequence[dict[str, Any]],
    *,
    engines: Sequence[str],
    sizes: Sequence[int] = (700, 900, 1200),
    embed_counter: TokenCounter | None = None,
    rerank_counter: TokenCounter | None = None,
) -> tuple[dict[str, Any], dict[int, list[DocumentChunk]]]:
    """同じ入力と親設定で child サイズを比較します。ファイル保存・外部 API 呼出しはしません。"""
    report: dict[str, Any] = {"variants": {}, "source_count": len(payloads), "engines": list(engines)}
    variants: dict[int, list[DocumentChunk]] = {}
    for size in sizes:
        config = replace(ChunkingConfig(), child_target_chars=size).validate()
        chunks = [
            chunk
            for payload in payloads
            for chunk in build_small_to_big_chunks(
                payload,
                source_run_id=payload["run_id"],
                selected_engine_ids=engines,
                config=config,
            )
        ]
        children = [chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]
        variants[size] = children
        row = audit_chunk_retrieval_text(
            chunks,
            child_search_text_max_chars=config.child_search_text_max_chars,
        )
        row["config"] = config.to_dict()
        row["parent_count"] = len(chunks) - len(children)
        row["row_group_count"] = sum(
            any(
                isinstance(table, dict) and table.get("chunking_strategy") == "row_group"
                for table in child.metadata.get("table_context", [])
            )
            for child in children
        )
        row["embed_text_tokens"] = _token_stats([child.retrieval_text for child in children], embed_counter)
        row["rerank_document_tokens"] = _token_stats(
            [_rerank_document(_chunk_answer_record(child)) for child in children],
            rerank_counter,
        )
        report["variants"][str(size)] = row
    return report, variants


def evidence_metrics(
    ranked: Sequence[DocumentChunk],
    expected: Sequence[dict[str, Any]],
) -> dict[str, float | None]:
    """run/page/本文抜粋で安定した根拠を照合し、根拠 recall と最初の命中の逆順位を返します。

    未注釈は None です。異なるチャンク分割で変化する chunk ID や補足 metadata は照合に使いません。
    """
    if not expected:
        return {"evidence_recall": None, "reciprocal_rank": None}
    covered: set[int] = set()
    first_rank = 0
    for rank, chunk in enumerate(ranked, 1):
        matches = {
            index
            for index, evidence in enumerate(expected)
            if chunk.source_run_id == evidence["run_id"]
            and chunk.page_start <= evidence["page"] <= chunk.page_end
            and _normalized(evidence["text"]) in _normalized(chunk.text)
        }
        covered.update(matches)
        if matches and not first_rank:
            first_rank = rank
    return {
        "evidence_recall": len(covered) / len(expected),
        "reciprocal_rank": 1 / first_rank if first_rank else 0.0,
    }


def evaluate_cohere_variants(
    variants: dict[int, list[DocumentChunk]],
    cases: Sequence[dict[str, Any]],
    settings: Settings,
    *,
    candidate_k: int = 80,
    top_k: int = 20,
) -> dict[str, Any]:
    """OCI embedding + cosine 候補 + Rerank を比較し、ADB には保存しません。

    OCI API の課金・通信が発生します。API 障害は伝播し、成功値に置換しません。
    本番の Oracle Text / image vector / 回答生成は含めず、回答正解率は測定しません。
    """
    if not 1 <= top_k <= candidate_k:
        raise ValueError("Require 1 <= top_k <= candidate_k.")
    _validate_cases(cases, variants)
    query_started = time.perf_counter()
    queries = [embed_query(case["question"], settings) for case in cases]
    query_seconds = time.perf_counter() - query_started
    results = {}
    for size, children in variants.items():
        started = time.perf_counter()
        # 本番の保存（store._retrieval_text）と同じ入力で評価する。
        vectors = embed_texts([child.retrieval_text or child.text for child in children], settings)
        embedding_seconds = time.perf_counter() - started
        if len(vectors) != len(children):
            raise ValueError("Embedding count does not match candidate count.")
        rows = []
        for case, query in zip(cases, queries, strict=False):
            started = time.perf_counter()
            order = sorted(range(len(children)), key=lambda i: _cosine(query, vectors[i]), reverse=True)
            candidates = [children[i] for i in order[:candidate_k]]
            retrieval_seconds = time.perf_counter() - started
            started = time.perf_counter()
            ranks = rerank_text_with_scores(
                case["question"],
                [_rerank_document(_chunk_answer_record(child)) for child in candidates],
                settings,
                top_n=top_k,
            )
            rerank_seconds = time.perf_counter() - started
            indices = [rank.index for rank in ranks]
            if (
                len(indices) != min(top_k, len(candidates))
                or len(set(indices)) != len(indices)
                or any(index < 0 or index >= len(candidates) for index in indices)
            ):
                raise ValueError("Rerank returned incomplete or invalid ranks.")
            reranked = [candidates[index] for index in indices]
            expected = case["expected_evidence"]
            rows.append(
                {
                    "case_id": case["case_id"],
                    "candidate": evidence_metrics(candidates, expected),
                    "vector_top_k": evidence_metrics(candidates[:top_k], expected),
                    "rerank_top_k": evidence_metrics(reranked, expected),
                    "retrieval_seconds": retrieval_seconds,
                    "rerank_seconds": rerank_seconds,
                }
            )
        results[str(size)] = {
            "embedding_seconds": embedding_seconds,
            "candidate_recall": _mean([row["candidate"]["evidence_recall"] for row in rows]),
            "rerank_recall_at_k": _mean([row["rerank_top_k"]["evidence_recall"] for row in rows]),
            "rerank_mrr_at_k": _mean([row["rerank_top_k"]["reciprocal_rank"] for row in rows]),
            "answer_accuracy": None,
            "cases": rows,
        }
    return {
        "mode": "oci_text_vector_cosine_rerank",
        "candidate_k": candidate_k,
        "top_k": top_k,
        "query_embedding_seconds": query_seconds,
        "variants": results,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """保存済み解析を読み、監査 JSON を出力します。--live-cohere のみ OCI を呼びます。"""
    parser = argparse.ArgumentParser(description="Audit chunk sizes without changing active chunks or ADB.")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--run-id", action="append", required=True)
    parser.add_argument("--engines", default="docling")
    parser.add_argument("--sizes", default="700,900,1200")
    parser.add_argument("--json-out", required=True)
    parser.add_argument("--embed-tokenizer", help="Local tokenizer JSON matching the configured embedding model.")
    parser.add_argument("--rerank-tokenizer", help="Local tokenizer JSON matching the configured rerank model.")
    parser.add_argument("--live-cohere", action="store_true")
    parser.add_argument("--dataset", help="Reviewed cases with run_id/page/text expected_evidence.")
    parser.add_argument("--candidate-k", type=int, default=80)
    parser.add_argument("--top-k", type=int, default=20)
    args = parser.parse_args(argv)
    from rag_engine.chunking import validate_run_id

    run_ids = list(dict.fromkeys(validate_run_id(run_id) for run_id in args.run_id))
    sources = [Path(args.output_dir) / run_id / "viewer-data.json" for run_id in run_ids]
    payloads = [dict(json.loads(path.read_text(encoding="utf-8")), run_id=run_id) for path, run_id in zip(sources, run_ids, strict=False)]
    counters = {}
    tokenizer_info = {}
    for name, path in (("embed", args.embed_tokenizer), ("rerank", args.rerank_tokenizer)):
        if path:
            counters[name], fingerprint = load_token_counter(path)
            tokenizer_info[name] = {"sha256": fingerprint, "method": "local_model_tokenizer"}
        else:
            tokenizer_info[name] = {"method": "unavailable", "reason": "No matching tokenizer supplied."}
    report, variants = audit_variants(
        payloads,
        engines=args.engines.split(","),
        sizes=[int(size) for size in args.sizes.split(",")],
        embed_counter=counters.get("embed"),
        rerank_counter=counters.get("rerank"),
    )
    report["sources"] = [
        {"run_id": run_id, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path, run_id in zip(sources, run_ids, strict=False)
    ]
    report["tokenizers"] = tokenizer_info
    report["token_count_note"] = "Document-only counts; rerank query/protocol overhead is not included."
    existing = []
    report["saved_latest_errors"] = []
    report["saved_latest_missing"] = []
    for run_id in run_ids:
        try:
            run = load_latest_chunk_run(args.output_dir, run_id)
            existing.append(run)
            if run is None:
                report["saved_latest_missing"].append(run_id)
        except (ValueError, TypeError, KeyError) as exc:
            # 古い未対応 schema は新規監査を妨げず、比較対象外として明示する。
            report["saved_latest_errors"].append({"run_id": run_id, "error": str(exc)})
    report["saved_latest"] = audit_chunk_retrieval_text([chunk for run in existing if run for chunk in run.chunks])
    report["saved_latest"]["run_count"] = sum(run is not None for run in existing)
    report["evaluation"] = {"mode": "not_run", "answer_accuracy": None}
    if args.live_cohere:
        if not args.dataset:
            parser.error("--live-cohere requires --dataset with reviewed evidence.")
        settings = get_settings()
        cases = json.loads(Path(args.dataset).read_text(encoding="utf-8"))["cases"]
        report["models"] = {
            "embedding": settings.embedding_model,
            "rerank": settings.rerank_model,
            "dimensions": settings.embedding_output_dimensions,
        }
        report["dataset_sha256"] = hashlib.sha256(Path(args.dataset).read_bytes()).hexdigest()
        report["evaluation"] = evaluate_cohere_variants(
            variants,
            cases,
            settings,
            candidate_k=args.candidate_k,
            top_k=args.top_k,
        )
    Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "report": args.json_out,
                "source_count": len(sources),
                "variants": list(report["variants"]),
                "evaluation": report["evaluation"]["mode"],
            }
        )
    )
    return 0


def _validate_cases(cases: Sequence[dict[str, Any]], variants: dict[int, list[DocumentChunk]]) -> None:
    """通信前に corpus と根拠注釈を検証し、未注釈評価を成功率に混ぜないようにします。"""
    if not cases or not variants or any(not children for children in variants.values()):
        raise ValueError("Evaluation needs nonempty cases and candidate corpora.")
    run_ids = {chunk.source_run_id for children in variants.values() for chunk in children}
    case_ids = set()
    for case in cases:
        if not case.get("case_id") or not case.get("question") or case["case_id"] in case_ids:
            raise ValueError("Each case needs a unique case_id and question.")
        case_ids.add(case["case_id"])
        expected = case.get("expected_evidence")
        if not isinstance(expected, list) or not expected:
            raise ValueError("Each case needs reviewed expected_evidence, not just expected_terms.")
        for evidence in expected:
            if (
                not isinstance(evidence, dict)
                or evidence.get("run_id") not in run_ids
                or not isinstance(evidence.get("page"), int)
                or evidence["page"] < 1
                or not isinstance(evidence.get("text"), str)
                or not evidence["text"].strip()
            ):
                raise ValueError("Evidence requires a corpus run_id, positive page, and nonempty exact text.")


def _token_stats(texts: Sequence[str], counter: TokenCounter | None) -> dict[str, Any] | None:
    if counter is None:
        return None
    counts = [counter(text) for text in texts]
    return {"total": sum(counts), "max": max(counts, default=0), "mean": _mean(counts)}


def _normalized(text: str) -> str:
    return "".join(text.casefold().split())


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("Query and document embedding dimensions differ.")
    denominator = math.sqrt(sum(x * x for x in left) * sum(x * x for x in right))
    return sum(x * y for x, y in zip(left, right, strict=False)) / denominator if denominator else 0.0


if __name__ == "__main__":
    raise SystemExit(main())
