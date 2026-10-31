"""同じ本文・引用を再監査した部分応答を、検証済みの観察ごとに結合する。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from pydantic import BaseModel

from rag_engine.models.llm import GroundedAudit

_ROW_FIELDS = {
    "reviews": ("index", None),
    "application_reviews": ("index", None),
    "request_reviews": ("request_id", "request_binding"),
    "guide_requirement_reviews": ("requirement_id", "requirement_binding"),
}
_SAFE_VALIDATION_KEYS = (
    "status",
    "strict_wire_validation",
    "fields",
    "field_rows",
    "requests",
    "requirements",
    "request_binding",
    "requirement_binding",
    "errors",
    "error_count",
    "errors_truncated",
    "raw_text_sha256",
    "source",
    "transport_not_observed",
)


def _attempt_validation(audit: GroundedAudit) -> dict[str, Any]:
    return {key: deepcopy(audit._wire_validation[key]) for key in _SAFE_VALIDATION_KEYS if key in audit._wire_validation}


def _rows(audit: GroundedAudit, field: str, identity: str, binding: str | None) -> dict[int | str, BaseModel]:
    rows = getattr(audit, field)
    counts = Counter(getattr(row, identity) for row in rows)
    states = audit._wire_validation.get(binding, {}).get("states") if binding else None
    return {
        key: row
        for row in rows
        if counts[key := getattr(row, identity)] == 1 and (not isinstance(states, Mapping) or states.get(key) == "valid")
    }


def _field_valid(audit: GroundedAudit, field: str) -> bool:
    # 明示的な旧fixtureではwire自体を観測していない。型検証済みの値は使っても、wire成功とはしない。
    state = audit._wire_validation.get("fields", {}).get(field)
    return state is None or state == "valid"


def _effective_binding(first: GroundedAudit, second: GroundedAudit, binding: str, adopted: list[int | str]) -> dict[str, Any]:
    expected = list(
        dict.fromkeys(key for audit in (first, second) for key in audit._wire_validation.get(binding, {}).get("expected_ids", ()))
    )
    return {
        "expected_ids": expected,
        "adopted_ids": adopted,
        "states": {key: "valid" if key in adopted else "unknown" for key in expected},
    }


def merge_audit_retry(first: GroundedAudit, second: GroundedAudit) -> GroundedAudit:
    """再応答の有効な行だけを更新し、欠落・破損で首応答の有効な観察を消さない。

    呼出側は同じ入力に対する応答だけを渡し、不正なanswer_quoteは先に除く。
    ここでは意味・引用・適用条件を再判定せず、未判定の要件も作り足さない。
    """
    updates: dict[str, Any] = {}
    row_sources: dict[str, list[dict[str, Any]]] = {}
    limited: dict[str, list[int | str]] = {}
    for field, (identity, binding) in _ROW_FIELDS.items():
        rows = _rows(first, field, identity, binding)
        sources = {key: 1 for key in rows}
        limit = next(meta.max_length for meta in GroundedAudit.model_fields[field].metadata if hasattr(meta, "max_length"))
        for key, row in _rows(second, field, identity, binding).items():
            if key not in rows and len(rows) >= limit:
                limited.setdefault(field, []).append(key)
                continue
            rows[key], sources[key] = row, 2
        updates[field] = [row.model_copy(deep=True) for row in rows.values()]
        row_sources[field] = [{"key": key, "attempt": sources[key]} for key in rows]
    field_sources = {}
    for field in GroundedAudit.model_fields:
        if field in _ROW_FIELDS:
            continue
        attempt = 2 if _field_valid(second, field) else 1 if _field_valid(first, field) else None
        updates[field] = deepcopy(getattr(first if attempt == 1 else second, field))
        field_sources[field] = {"attempt": attempt, "status": "valid" if attempt is not None else "unverified"}
    merged = second.model_copy(deep=True, update=updates)
    # 第2応答の原失敗はそのまま残す。有効な再利用の判定をraw wire成功へ読み替えない。
    merged._wire_validation["retry_merge"] = {
        "attempts": [{"attempt": index, "validation": _attempt_validation(audit)} for index, audit in enumerate((first, second), 1)],
        "row_sources": row_sources,
        "field_sources": field_sources,
        "limited_rows": limited,
        "effective_request_binding": _effective_binding(
            first, second, "request_binding", [row.request_id for row in merged.request_reviews]
        ),
        "effective_requirement_binding": _effective_binding(
            first, second, "requirement_binding", [row.requirement_id for row in merged.guide_requirement_reviews]
        ),
    }
    return merged
