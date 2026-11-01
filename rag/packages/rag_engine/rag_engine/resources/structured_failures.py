"""構造化出力の失敗を、入力値や例外の本文を残さず分類する。"""

from collections.abc import Mapping
from json import JSONDecodeError
from typing import Any

from pydantic import BaseModel, ValidationError

_MAX_CHAIN = 4
_MAX_ERRORS = 12
_MAX_LOCATION = 8
_CONTRACT_CODES = {
    "duplicate_request_id",
    "duplicate_request_ids",
    "missing_request_id",
    "missing_request_ids",
    "extra_request_id",
    "extra_request_ids",
    "invalid_request_id",
    "invalid_request_ids",
    "citation_id_missing",
    "citation_id_invalid",
    "citation_id_ambiguous",
    "citation_range_missing",
    "citation_range_invalid",
    "citation_range_out_of_bounds",
    "citation_extraction_failed",
    "citation_version_mismatch",
    "evidence_id_invalid",
    "evidence_id_ambiguous",
    "evidence_range_invalid",
}
_VALIDATION_TYPES = {
    "missing",
    "extra_forbidden",
    "enum",
    "literal_error",
    "json_invalid",
    "json_type",
    "model_type",
    "model_attributes_type",
    "dict_type",
    "list_type",
    "tuple_type",
    "set_type",
    "string_type",
    "string_too_short",
    "string_too_long",
    "string_pattern_mismatch",
    "string_unicode",
    "bool_type",
    "bool_parsing",
    "int_type",
    "int_parsing",
    "int_from_float",
    "int_parsing_size",
    "float_type",
    "float_parsing",
    "finite_number",
    "greater_than",
    "greater_than_equal",
    "less_than",
    "less_than_equal",
    "multiple_of",
    "too_short",
    "too_long",
    "none_required",
    "value_error",
    "assertion_error",
    "union_tag_invalid",
    "union_tag_not_found",
}
_PROVIDER_STATUSES = {"incomplete", "completed", "failed", "cancelled", "queued", "in_progress"}
_INCOMPLETE_REASONS = {"max_output_tokens", "content_filter"}
# 出力の暴走で呼び出しを打ち切った理由（`rag_engine.adapters.model_api.LlmRunawayOutput.reason`。#2966・#3835）。
_CUTOFF_REASONS = {"runaway_whitespace", "runaway_repetition"}


def _get(value: object, name: str) -> object:
    try:
        return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)
    except Exception:
        return None


def _schema_node(node: Mapping[str, Any], root: Mapping[str, Any]) -> Mapping[str, Any]:
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        target = root.get("$defs", {}).get(ref.removeprefix("#/$defs/"))
        if isinstance(target, Mapping):
            return target
    return node


def _location(location: object, root: Mapping[str, Any]) -> list[str | int]:
    """schema のプロパティと配列位置だけを残す。動的な辞書キーは公開しない。"""
    if not isinstance(location, (tuple, list)):
        return []
    nodes: list[Mapping[str, Any]] = [root] if root else []
    result: list[str | int] = []
    for part in location[:_MAX_LOCATION]:
        expanded: list[Mapping[str, Any]] = []
        for node in nodes:
            node = _schema_node(node, root)
            expanded.append(node)
            for branch in node.get("anyOf", []) + node.get("oneOf", []):
                if isinstance(branch, Mapping):
                    expanded.append(_schema_node(branch, root))
        next_nodes: list[Mapping[str, Any]] = []
        if type(part) is int and 0 <= part <= 1_000_000:
            for node in expanded:
                child = node.get("items")
                if isinstance(child, Mapping):
                    next_nodes.append(child)
            result.append(part if next_nodes or not root else "<redacted>")
        elif isinstance(part, str) and len(part) <= 80:
            for node in expanded:
                child = node.get("properties", {}).get(part)
                if isinstance(child, Mapping):
                    next_nodes.append(child)
            result.append(part if next_nodes else "<redacted>")
        else:
            result.append("<redacted>")
        nodes = next_nodes
    return result


def structured_failure_details(exc: BaseException, schema: type[BaseModel] | Mapping[str, Any] | None = None) -> dict[str, Any]:
    """同じ失敗の原因連鎖を調べる。形式の失敗から材料不足を推測しない。

    provider_status / incomplete_reason、または response.status /
    response.incomplete_details.reason はアダプターが得た実際の状態だけを渡す。
    code / failure_code は定義済みの決定論的な契約違反に限って採用する。
    出力の暴走で打ち切った呼び出しは、打ち切りの理由（``cutoff_reason``）と分類 ``runaway_output`` を残す（#3835）。
    """
    root = schema.model_json_schema() if isinstance(schema, type) and issubclass(schema, BaseModel) else schema or {}
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(chain) < _MAX_CHAIN:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    result: dict[str, Any] = {
        "error_class": type(exc).__name__[:80],
        "cause_classes": [type(cause).__name__[:80] for cause in chain[1:]],
        "category": "structure_unknown",
    }
    errors: list[dict[str, Any]] = []
    invalid_json = False
    contract_code = None
    status = None
    reason = None
    cutoff = None
    for cause in chain:
        invalid_json = invalid_json or isinstance(cause, JSONDecodeError)
        candidate_cutoff = _get(cause, "reason")
        if cutoff is None and isinstance(candidate_cutoff, str) and candidate_cutoff in _CUTOFF_REASONS:
            cutoff = candidate_cutoff
        for attr in ("failure_code", "code"):
            code = _get(cause, attr)
            if isinstance(code, str) and code in _CONTRACT_CODES and contract_code is None:
                contract_code = code
        response = _get(cause, "response")
        candidate_status = _get(cause, "provider_status") or _get(response, "status")
        candidate_reason = _get(cause, "incomplete_reason") or _get(_get(response, "incomplete_details"), "reason")
        if (
            isinstance(candidate_status, str)
            and candidate_status in _PROVIDER_STATUSES
            and (status is None or candidate_status == "incomplete")
        ):
            status = candidate_status
            reason = candidate_reason if isinstance(candidate_reason, str) and candidate_reason in _INCOMPLETE_REASONS else None
        if isinstance(cause, ValidationError):
            for error in cause.errors(include_url=False, include_context=False, include_input=False)[:_MAX_ERRORS]:
                kind = error["type"]
                invalid_json = invalid_json or kind == "json_invalid"
                if len(errors) < _MAX_ERRORS:
                    errors.append({"type": kind if kind in _VALIDATION_TYPES else "validation_error", "loc": _location(error["loc"], root)})
    if errors:
        result["validation_errors"] = errors
    if status is not None:
        result["provider_status"] = status
    if status == "incomplete" and reason is not None:
        result["incomplete_reason"] = reason
    if cutoff is not None:
        result["cutoff_reason"] = cutoff
        result["category"] = "runaway_output"
    elif status == "incomplete":
        result["category"] = "truncated_output" if reason == "max_output_tokens" else "incomplete_output"
    elif contract_code is not None:
        result["category"] = "request_id_contract" if "request_id" in contract_code else "evidence_binding"
    elif invalid_json:
        result["category"] = "invalid_json"
    elif errors:
        result["category"] = "schema_enum" if any(e["type"] in {"enum", "literal_error"} for e in errors) else "schema_field"
    if contract_code is not None:
        result["contract_code"] = contract_code
    return result
