"""通常回答と FAQ 直答の保存・履歴管理を共有する。"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rag_engine.knowledge.approved_faq import (
    APPROVED_FAQ_DIRECT_RETRIEVAL_SCOPE,
    DEFAULT_APPROVED_FAQ_DIRECT_MATCH_MIN_SCORE,
    ApprovedFaqSuggestion,
)
from rag_engine.ports import ArtifactStore
from rag_engine.resources.filesystem import FileArtifactStore

APPROVED_FAQ_DIRECT_ANSWER_FLOW = "approved_faq_direct"

APPROVED_FAQ_DIRECT_ANSWER_FLOW_LABEL = "Approved FAQ直接回答"

APPROVED_FAQ_DIRECT_RETRIEVAL_SCOPE_LABEL = "Approved FAQ"


def _approved_faq_direct_answer_run_id(suggestion: ApprovedFaqSuggestion, question: str) -> str:
    seed = f"{suggestion.record.id}\0{question}".encode()
    return f"approved-faq-{hashlib.sha256(seed).hexdigest()[:16]}"


def _approved_faq_direct_answer_payload(
    suggestion: ApprovedFaqSuggestion,
    *,
    answer_id: str,
    run_id: str,
    question: str,
    standard_answer: str,
    classification_filter: Any,
) -> dict[str, Any]:
    record = suggestion.record
    question_text = str(question or record.question).strip()
    answer_text = record.approved_answer.strip()
    classification_metadata = (
        classification_filter.to_metadata() if hasattr(classification_filter, "to_metadata") else dict(classification_filter or {})
    )
    match_summary = (
        f"Approved FAQ ID: {record.id} / matched QUESTION: {suggestion.matched_question} "
        f"/ method: {suggestion.match_method} / score: {suggestion.score:.2f} "
        f"/ threshold: {DEFAULT_APPROVED_FAQ_DIRECT_MATCH_MIN_SCORE:.2f}"
    )
    return {
        "schema_version": 1,
        "answer_id": answer_id,
        "run_id": run_id,
        "question": question_text,
        "question_display": question_text,
        "standard_answer": str(standard_answer or ""),
        "answer": answer_text,
        "answer_text": answer_text,
        "confidence": "approved_faq",
        "question_type": ["approved_faq"],
        "cited_evidence_ids": [],
        "cited_evidence": [],
        "image_evidence": [],
        "image_prompt_mode": "text_only",
        "primary_source_run_id": "",
        "reasoning_summary": match_summary,
        "insufficient_reason": "",
        "needs_human_review": False,
        "answer_flow": APPROVED_FAQ_DIRECT_ANSWER_FLOW,
        "answer_flow_label": APPROVED_FAQ_DIRECT_ANSWER_FLOW_LABEL,
        "retrieval_scope": APPROVED_FAQ_DIRECT_RETRIEVAL_SCOPE,
        "retrieval_scope_label": APPROVED_FAQ_DIRECT_RETRIEVAL_SCOPE_LABEL,
        "classification_filter": classification_metadata,
        "selected_strategy": APPROVED_FAQ_DIRECT_ANSWER_FLOW,
        "effective_strategy": APPROVED_FAQ_DIRECT_ANSWER_FLOW,
        "selected_strategy_label": APPROVED_FAQ_DIRECT_ANSWER_FLOW_LABEL,
        "effective_strategy_label": APPROVED_FAQ_DIRECT_ANSWER_FLOW_LABEL,
        "generated_queries": [question_text],
        "query_expansion_summary": "",
        "routing_reason": "Approved FAQ候補で高閾値一致したため、RAG生成を行わず承認済み回答を返しました。",
        "question_plan": {},
        "runtime_knowledge": {},
        "query_understanding": {},
        "inquiry_conditions": {},
        "text_search_tokenizer": "",
        "text_search_tokenizer_label": "",
        "text_search_tokenizer_fingerprint": "",
        "text_search_tokens": [],
        "text_search_query": "",
        "text_search_queries": [question_text],
        "text_search_query_source": APPROVED_FAQ_DIRECT_RETRIEVAL_SCOPE,
        "text_search_tokenization_error": "",
        "crag_attempts": [],
        "prompt_injection_risk": False,
        "prompt_injection_warnings": [],
        "rerank_scores": [],
        "retrieval_query_plan": {
            "source": APPROVED_FAQ_DIRECT_RETRIEVAL_SCOPE,
            "direct_match_min_score": DEFAULT_APPROVED_FAQ_DIRECT_MATCH_MIN_SCORE,
            "matched_record_id": record.id,
            "matched_question": suggestion.matched_question,
            "match_method": suggestion.match_method,
            "score": suggestion.score,
            "text_score": suggestion.text_score,
            "semantic_score": suggestion.semantic_score,
            "scope_score": suggestion.scope_score,
            "scope_matches": list(suggestion.scope_matches),
        },
        "approved_faq_match": {
            "record_id": record.id,
            "question": record.question,
            "matched_question": suggestion.matched_question,
            "match_method": suggestion.match_method,
            "score": suggestion.score,
            "text_score": suggestion.text_score,
            "semantic_score": suggestion.semantic_score,
            "scope_score": suggestion.scope_score,
            "scope_matches": list(suggestion.scope_matches),
            "source": record.source or {},
        },
        "evaluation": {"status": "not_evaluated", "message": "Approved FAQ の直接回答は自動評価の対象外です。"},
        "evidence_items": [],
    }


def _create_answer_id(run_id: str, question: str) -> str:
    seed = f"{run_id}\0{time.time_ns()}\0{question}".encode()
    return hashlib.sha256(seed).hexdigest()[:16]


def write_answer_payload(output_dir: Path, run_id: str, payload: dict[str, Any], *, store: ArtifactStore | None = None) -> str:
    """明示された保存先に回答 JSON を原子的に保存する。既存形式を維持する。"""
    answer_id = str(payload.get("answer_id") or "").strip()
    if not answer_id or not run_id or any(part in {".", ".."} or "/" in part or "\\" in part for part in (run_id, answer_id)):
        raise ValueError("run_id and answer_id must be nonempty path components")
    target = store if store is not None else FileArtifactStore(Path(output_dir))
    target.write(
        f"{run_id}/answers/{answer_id}.json",
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
    )
    return answer_id


@dataclass(frozen=True)
class SavedAnswer:
    """保存された回答の識別子と既存 viewer 互換データ。"""

    run_id: str
    answer_id: str
    payload: dict[str, Any]


def save_answer_result(
    result,
    settings,
    *,
    run_id: str = "",
    question: str = "",
    standard_answer: str = "",
    store: ArtifactStore | None = None,
    evaluation_provider: str | None = None,
    evaluation_standard_scope=None,
    evaluation_enabled: bool = True,
) -> SavedAnswer | None:
    """標準回答があれば指定 provider（未指定時は既定 LLM）で評価し、回答と履歴を保存する。

    evaluation_standard_scope は同じ質問・標準回答のモデル比較で再利用する事前評価範囲。
    evaluation_enabled=False は LLM 評価を省略し、標準回答・生成時の確認状態を保持する。
    既存の直接呼び出しとの互換性のため既定値は True。UI は明示的に指定する。
    評価失敗でも回答は保存する。出典 run のない不足結果は保存しない。
    """
    from rag_engine.generation.answering import answer_result_payload, format_answer_response

    source_id = str(run_id or result.primary_source_run_id or "").strip()
    if not source_id:
        return None
    answer_id = _create_answer_id(source_id, result.original_question)
    payload = answer_result_payload(
        result, answer_id=answer_id, run_id=source_id, question=question or result.original_question, standard_answer=standard_answer
    )
    if evaluation_enabled:
        from rag_engine.evaluation.answer_eval import evaluate_answer_payload

        # 同じ質問・標準回答のモデル比較では、事前に固定した評価範囲を共有できる。
        payload["evaluation"] = evaluate_answer_payload(
            payload, settings, provider_id=evaluation_provider, standard_scope=evaluation_standard_scope
        )
    else:
        payload["evaluation"] = {
            "status": "not_evaluated",
            "message": "LLM による回答評価がオフのため、評価していません。",
        }
    evaluation = payload["evaluation"]
    if evaluation["status"] in {"completed", "not_applicable"}:
        # 評価で訂正された外部確認を UI と feedback が同じ値で参照する。
        payload["external_data_required"] = evaluation["external_data_required"]
        payload["external_data_items"] = list(evaluation["external_data_items"])
        external_label = "外部データ確認が必要"
        payload["question_type"] = [value for value in payload["question_type"] if value != external_label]
        if evaluation["external_data_required"]:
            payload["needs_human_review"] = True
            payload["question_type"].append(external_label)
        if evaluation.get("evaluation_reliable") is False:
            payload["needs_human_review"] = True
        # 外部確認が不要でも、根拠不足・専門判断などによる既存の人手確認は解除しない。
        # 旧クライアントが読む整形済み answer にも古い人工確認ラベルを残さない。
        payload["answer"] = format_answer_response(
            json.dumps(
                {
                    "answer": payload["answer_text"],
                    **{
                        key: payload[key]
                        for key in (
                            "confidence",
                            "question_type",
                            "cited_evidence",
                            "reasoning_summary",
                            "insufficient_reason",
                            "needs_human_review",
                            "external_data_required",
                            "external_data_items",
                        )
                    },
                },
                ensure_ascii=False,
            )
        )
    write_answer_payload(settings.output_dir, source_id, payload, store=store)
    return SavedAnswer(source_id, answer_id, payload)


def save_faq_answer(
    suggestion: ApprovedFaqSuggestion, settings, *, question: str, standard_answer: str = "", classification_filter=None
) -> SavedAnswer:
    """FAQ の直答を既存形式で保存する。RAG や回答モデルは呼び出さない。"""
    run_id = _approved_faq_direct_answer_run_id(suggestion, question)
    answer_id = _create_answer_id(run_id, question)
    payload = _approved_faq_direct_answer_payload(
        suggestion,
        answer_id=answer_id,
        run_id=run_id,
        question=question,
        standard_answer=standard_answer,
        classification_filter=classification_filter,
    )
    write_answer_payload(settings.output_dir, run_id, payload)

    return SavedAnswer(run_id, answer_id, payload)
