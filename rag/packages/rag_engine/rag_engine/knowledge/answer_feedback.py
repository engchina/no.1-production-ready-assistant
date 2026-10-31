"""回答生成結果への feedback を JSONL と回答 payload へ記録する。"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ANSWER_FEEDBACK_SCHEMA_VERSION = 1
ANSWER_FEEDBACK_DIR_NAME = "answer_feedback"
ANSWER_FEEDBACK_FILE_NAME = "feedback.jsonl"
ANSWER_FEEDBACK_REVIEW_PENDING_STATUS = "review_pending"
ANSWER_FEEDBACK_TYPE_VALUES = {
    "correct",
    "incorrect_answer",
    "wrong_evidence",
    "missing_knowledge",
    "outdated_source",
    "ambiguous_question",
}
SAFE_PATH_PART_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


def answer_feedback_path(output_dir: str | Path) -> Path:
    """回答 feedback JSONL の保存パスを返します。"""
    return Path(output_dir) / ANSWER_FEEDBACK_DIR_NAME / ANSWER_FEEDBACK_FILE_NAME


def answer_payload_path(output_dir: str | Path, run_id: str, answer_id: str) -> Path:
    """個別回答 payload JSON の保存パスを返します。"""
    safe_run_id = _safe_path_part(run_id, "run_id")
    safe_answer_id = _safe_path_part(answer_id, "answer_id")
    return Path(output_dir) / safe_run_id / "answers" / f"{safe_answer_id}.json"


def create_answer_feedback_record(
    *,
    output_dir: str | Path,
    run_id: str,
    answer_id: str,
    feedback_type: str,
    question: str = "",
    comment: str = "",
    corrected_answer: str = "",
    classification_filter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """保存済み回答から feedback を作成し、正しい評価では不要な修正回答を記録しません。"""
    normalized_feedback_type = _normalize_feedback_type(feedback_type)
    answer_trace = load_answer_trace_summary(output_dir, run_id, answer_id)
    created_at = _utc_now()
    feedback_id = _create_feedback_id(
        run_id=run_id,
        answer_id=answer_id,
        feedback_type=normalized_feedback_type,
        created_at=created_at,
    )
    feedback_question = str(answer_trace.get("question") or "").strip() or _clean_text(question)
    feedback_classification_filter = _dict_or_empty(answer_trace.get("classification_filter")) or dict(classification_filter or {})
    return {
        "schema_version": ANSWER_FEEDBACK_SCHEMA_VERSION,
        "feedback_id": feedback_id,
        "created_at": created_at,
        "status": ANSWER_FEEDBACK_REVIEW_PENDING_STATUS,
        "feedback_type": normalized_feedback_type,
        "run_id": str(run_id or "").strip(),
        "answer_id": str(answer_id or "").strip(),
        "question": feedback_question,
        "comment": _clean_text(comment),
        "corrected_answer": "" if normalized_feedback_type == "correct" else _clean_text(corrected_answer),
        "classification_filter": feedback_classification_filter,
        "answer_trace": answer_trace,
        "review": {
            "decision": "",
            "promote_to": [],
            "reviewer": "",
            "reviewed_at": "",
            "notes": "",
        },
    }


def append_answer_feedback(output_dir: str | Path, record: dict[str, Any]) -> Path:
    """回答 feedbackを追記します。"""
    _validate_feedback_record(record)
    path = answer_feedback_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
        handle.write("\n")
    return path


def load_answer_trace_summary(output_dir: str | Path, run_id: str, answer_id: str) -> dict[str, Any]:
    """回答 trace summaryを読み込みます。"""
    path = answer_payload_path(output_dir, run_id, answer_id)
    if not path.exists():
        raise ValueError("回答 payload が見つかりません。回答生成後にフィードバックを送信してください。")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"回答 payload を読み込めません: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("回答 payload の形式が不正です。")

    evidence_items = payload.get("evidence_items") if isinstance(payload.get("evidence_items"), list) else []
    evidence_ids = [str(item.get("id")) for item in evidence_items if isinstance(item, dict) and str(item.get("id") or "").strip()]
    return {
        "answer_payload_path": path.as_posix(),
        "question": str(payload.get("question") or ""),
        "answer_text": str(payload.get("answer_text") or payload.get("answer") or ""),
        "confidence": str(payload.get("confidence") or ""),
        "needs_human_review": payload.get("needs_human_review"),
        # 「回答できない」回答を FAQ へ昇格しない判定に使う。
        "insufficient_reason": str(payload.get("insufficient_reason") or ""),
        "answer_flow": str(payload.get("answer_flow") or ""),
        "retrieval_scope": str(payload.get("retrieval_scope") or ""),
        # Knowledge Base 検索では、回答の保存先の run と根拠文書の run が異なる。
        "primary_source_run_id": str(payload.get("primary_source_run_id") or ""),
        "classification_filter": _dict_or_empty(payload.get("classification_filter")),
        "evidence_item_ids": evidence_ids,
        "retrieval_query_plan": _dict_or_empty(payload.get("retrieval_query_plan")),
        "runtime_knowledge": _dict_or_empty(payload.get("runtime_knowledge")),
        "query_understanding": _dict_or_empty(payload.get("query_understanding")),
        "generated_queries": _list_of_strings(payload.get("generated_queries")),
        "text_search_queries": _list_of_strings(payload.get("text_search_queries")),
    }


def _validate_feedback_record(record: dict[str, Any]) -> None:
    if int(record.get("schema_version") or 0) != ANSWER_FEEDBACK_SCHEMA_VERSION:
        raise ValueError("フィードバック schema_version が不正です。")
    _safe_path_part(str(record.get("run_id") or ""), "run_id")
    _safe_path_part(str(record.get("answer_id") or ""), "answer_id")
    _normalize_feedback_type(str(record.get("feedback_type") or ""))
    if str(record.get("status") or "") != ANSWER_FEEDBACK_REVIEW_PENDING_STATUS:
        raise ValueError("フィードバックは review_pending として保存してください。")


def _normalize_feedback_type(value: str) -> str:
    normalized = str(value or "").strip()
    if normalized not in ANSWER_FEEDBACK_TYPE_VALUES:
        raise ValueError("フィードバック種別を選択してください。")
    return normalized


def _safe_path_part(value: str, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} が空です。")
    if not SAFE_PATH_PART_PATTERN.fullmatch(text):
        raise ValueError(f"{label} に使用できない文字が含まれています。")
    return text


def _create_feedback_id(*, run_id: str, answer_id: str, feedback_type: str, created_at: str) -> str:
    # created_at は秒精度なので、同一秒の送信でも ID が重ならないよう time_ns を加える。
    seed = f"{run_id}\0{answer_id}\0{feedback_type}\0{created_at}\0{time.time_ns()}".encode()
    return hashlib.sha256(seed).hexdigest()[:16]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list_of_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]
