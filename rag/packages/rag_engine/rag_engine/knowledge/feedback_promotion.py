"""レビュー済み回答 feedback を retrieval eval ケースへ昇格する。"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from rag_parser_core.language import current_pack
from rag_parser_core.language.text import alternation

PROJECT_ROOT = Path.cwd()
DEFAULT_RETRIEVAL_EVAL_DATASET = PROJECT_ROOT / "eval" / "retrieval_representative_questions.json"
APPROVED_FEEDBACK_STATUSES = {"approved", "accepted"}
APPROVED_REVIEW_DECISIONS = {"approved", "accept", "accepted", "promote_to_eval"}
INACTIVE_FEEDBACK_STATUSES = {"review_pending", "draft", "deprecated", "superseded"}
DEFAULT_EXPECTED_TERM_LIMIT = 8


def load_feedback_records(path: str | Path) -> list[dict[str, Any]]:
    """feedback JSONL から有効な record を読み込みます。"""
    source_path = Path(path)
    if not source_path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in source_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def reviewed_feedback_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """review 済みとして扱える feedback record だけを返します。"""
    return [record for record in records if is_feedback_review_approved(record)]


def is_feedback_review_approved(record: dict[str, Any]) -> bool:
    """feedback record が retrieval eval 昇格対象かを判定します。"""
    status = str(record.get("status") or "").strip().casefold()
    if status in APPROVED_FEEDBACK_STATUSES:
        return True
    if status in INACTIVE_FEEDBACK_STATUSES:
        return False
    review = record.get("review") if isinstance(record.get("review"), dict) else {}
    decision = str(review.get("decision") or "").strip().casefold()
    return decision in APPROVED_REVIEW_DECISIONS


def feedback_to_retrieval_eval_case(record: dict[str, Any]) -> dict[str, Any]:
    """回答 feedback を retrieval eval dataset の case 形式へ変換します。"""
    if not is_feedback_review_approved(record):
        raise ValueError("review approved の feedback だけを eval case に昇格できます。")

    feedback_id = _required_text(record, "feedback_id")
    answer_trace = record.get("answer_trace") if isinstance(record.get("answer_trace"), dict) else {}
    question = _clean_text(record.get("question") or answer_trace.get("question"))
    if not question:
        raise ValueError("feedback に question がありません。")
    corrected_answer = _clean_text(record.get("corrected_answer") or answer_trace.get("answer_text"), max_chars=2000)
    evidence_ids = _string_list(answer_trace.get("evidence_item_ids"))
    case = {
        "case_id": f"feedback-{feedback_id}",
        "question": question,
        "tags": _case_tags(record),
        "classification_filter": _classification_filter(record, answer_trace),
        "expected_terms": _expected_terms(corrected_answer),
        "source_feedback": {
            "feedback_id": feedback_id,
            "feedback_type": str(record.get("feedback_type") or "").strip(),
            "run_id": str(record.get("run_id") or "").strip(),
            "answer_id": str(record.get("answer_id") or "").strip(),
            "answer_payload_path": str(answer_trace.get("answer_payload_path") or "").strip(),
            "review": record.get("review") if isinstance(record.get("review"), dict) else {},
        },
    }
    if evidence_ids:
        case["expected_child_ids"] = evidence_ids
    if corrected_answer:
        case["expected_answer"] = corrected_answer
    return case


def append_retrieval_eval_cases(dataset_path: str | Path, cases: Sequence[dict[str, Any]]) -> int:
    """既存 dataset へ重複を避けて eval case を追記します。"""
    path = Path(dataset_path)
    payload = _load_eval_dataset(path)
    existing_ids = {str(case.get("case_id") or "") for case in payload.get("cases", []) if isinstance(case, dict)}
    added = 0
    for case in cases:
        case_id = str(case.get("case_id") or "").strip()
        if not case_id or case_id in existing_ids:
            continue
        payload["cases"].append(case)
        existing_ids.add(case_id)
        added += 1
    if added:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return added


def _load_eval_dataset(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise ValueError("retrieval eval dataset の形式が不正です。")
    return payload


def _case_tags(record: dict[str, Any]) -> list[str]:
    tags = ["feedback"]
    feedback_type = str(record.get("feedback_type") or "").strip()
    if feedback_type:
        tags.append(feedback_type)
    review = record.get("review") if isinstance(record.get("review"), dict) else {}
    promote_to = review.get("promote_to")
    if isinstance(promote_to, list):
        tags.extend(str(item) for item in promote_to if str(item).strip())
    return _ordered_unique(tags)


def _classification_filter(record: dict[str, Any], answer_trace: dict[str, Any]) -> dict[str, Any]:
    value = record.get("classification_filter")
    if isinstance(value, dict) and value:
        return dict(value)
    value = answer_trace.get("classification_filter")
    return dict(value) if isinstance(value, dict) else {}


def _expected_terms(answer: str, *, limit: int = DEFAULT_EXPECTED_TERM_LIMIT) -> list[str]:
    normalized = _normalize(answer)
    tokens = re.findall(r"[0-9A-Za-z_][0-9A-Za-z_.-]*", normalized)
    # 語の区切り（助詞・文末の語）と、語の末尾から外す画面の部品の語は言語資源（search_terms。#2181）。
    pack = current_pack()
    split_words = alternation(pack.words("search_terms", "feedback_split_words"))
    chunks = re.split(rf"[、。,.()\s]+|{split_words}", normalized)
    for chunk in chunks:
        text = re.sub(r"^[^ぁ-んァ-ン一-龯々ー]+|[^ぁ-んァ-ン一-龯々ー]+$", "", chunk)
        if len(text) >= 2:
            tokens.append(text)
            for suffix in pack.words("search_terms", "feedback_strip_suffixes"):
                if text.endswith(suffix) and len(text) > len(suffix) + 1:
                    tokens.append(text[: -len(suffix)])
    selected: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        key = token.casefold()
        if key in seen:
            continue
        selected.append(token)
        seen.add(key)
        if len(selected) >= limit:
            break
    return selected


def _required_text(record: dict[str, Any], key: str) -> str:
    value = _clean_text(record.get(key))
    if not value:
        raise ValueError(f"feedback に {key} がありません。")
    return value


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _ordered_unique(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        out.append(text)
        seen.add(key)
    return out


def _clean_text(value: Any, max_chars: int = 800) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip()


def _normalize(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or ""))
