"""実際の要求IDを固定した監査wireと、部分検証済みcanonicalの境界。"""

import json
import re
from collections.abc import Mapping
from contextlib import suppress
from copy import deepcopy
from functools import lru_cache
from hashlib import sha256
from types import GenericAlias
from typing import Any, ClassVar, cast, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, create_model

from rag_engine.models.llm import GroundedAudit, GroundedFactAudit, GroundedItemReview, GroundedRequestReview, GuideRequirementReview
from rag_engine.resources.structured_failures import structured_failure_details


class AuditWireContractError(ValueError):
    """安全に部分利用できないwire。本文やkeyの値を例外本文へ含めない。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.failure_code = code


class AuditWireResult(BaseModel):
    """原wireの検証結果と採用可能なcanonicalを区別する。原文は私有属性だけに保持する。"""

    audit: GroundedAudit
    validation: dict[str, Any]
    _raw_text: str = PrivateAttr(default="")

    @property
    def raw_text(self) -> str:
        return self._raw_text


class _AuditWire(BaseModel):
    model_config = ConfigDict(extra="forbid")
    canonical_schema: ClassVar[type[GroundedAudit] | type[GroundedFactAudit]]
    expected_request_ids: ClassVar[tuple[str, ...]]
    request_value_schema: ClassVar[type[BaseModel]]
    requirement_value_schema: ClassVar[type[BaseModel]]
    expected_requirement_ids: ClassVar[tuple[str, ...]]


def is_audit_wire(schema: object) -> bool:
    return isinstance(schema, type) and issubclass(schema, _AuditWire)


def _value_schema(model: type[BaseModel], identity: str, name: str) -> type[BaseModel]:
    fields: dict[str, Any] = {key: (info.annotation, deepcopy(info)) for key, info in model.model_fields.items() if key != identity}
    return create_model(name, __config__=ConfigDict(extra="forbid"), **fields)


@lru_cache(maxsize=1)
def _review_wire_schema() -> type[BaseModel]:
    """業務範囲をモデルに再裁定させず、資料条件だけを出力契約にする。"""
    public = GroundedItemReview.model_json_schema()["properties"]
    fields: dict[str, Any] = {
        key: (info.annotation, deepcopy(info))
        for key, info in GroundedItemReview.model_fields.items()
        if key in public and key not in {"applicability", "condition", "source_applicability"}
    }
    fields["source_applicability"] = (
        GroundedItemReview.model_fields["applicability"].annotation,
        Field(..., description=GroundedItemReview.model_fields["source_applicability"].description),
    )
    return create_model("SourceItemReview", __config__=ConfigDict(extra="forbid"), **fields)


def _keys_schema(ids: tuple[str, ...], value: type[BaseModel], name: str) -> type[BaseModel]:
    fields: dict[str, Any] = {f"key_{i}": (value, Field(..., alias=key)) for i, key in enumerate(ids)}
    return create_model(name, __config__=ConfigDict(extra="forbid", populate_by_name=False), **fields)


@lru_cache(maxsize=128)
def audit_wire_schema(
    canonical: type[GroundedAudit] | type[GroundedFactAudit],
    expected_ids: tuple[str, ...],
    requirement_ids: tuple[str, ...] = (),
) -> type[_AuditWire]:
    if canonical not in {GroundedAudit, GroundedFactAudit}:
        raise AuditWireContractError("invalid_canonical_schema")
    for ids, maximum in [(expected_ids, 64), (requirement_ids, 200)]:
        if len(ids) > maximum or len(set(ids)) != len(ids) or any(not key for key in ids):
            raise AuditWireContractError("invalid_expected_ids")
    suffix = sha256(json.dumps([expected_ids, requirement_ids], ensure_ascii=False).encode()).hexdigest()[:16]
    request_value = _value_schema(GroundedRequestReview, "request_id", "BoundRequestValue")
    requirement_value = _value_schema(GuideRequirementReview, "requirement_id", "BoundRequirementValue")
    fields: dict[str, Any] = {
        key: (info.annotation, deepcopy(info))
        for key, info in canonical.model_fields.items()
        if key not in {"request_reviews", "guide_requirement_reviews"}
    }
    fields["reviews"] = (GenericAlias(list, _review_wire_schema()), deepcopy(canonical.model_fields["reviews"]))
    fields["request_reviews"] = (
        _keys_schema(expected_ids, request_value, "BoundRequestKeys_" + suffix),
        Field(..., description="固定された要求キーの判定内容。キーのIDを値に重ねて書かない。"),
    )
    fields["guide_requirement_reviews"] = (
        _keys_schema(requirement_ids, requirement_value, "BoundRequirementKeys_" + suffix),
        Field(..., description="固定されたガイド要件キーの観察。キーのIDを値に重ねて書かない。"),
    )
    wire = cast(type[_AuditWire], create_model(canonical.__name__ + "Bound_" + suffix, __base__=_AuditWire, **fields))
    wire.canonical_schema, wire.expected_request_ids = canonical, expected_ids
    wire.expected_requirement_ids = requirement_ids
    wire.request_value_schema, wire.requirement_value_schema = request_value, requirement_value
    return wire


class _Pairs(dict[str, Any]):
    def __init__(self, pairs: list[tuple[str, Any]]):
        super().__init__()
        self.raw_pairs = pairs
        self.key_sequence = tuple(key for key, _ in pairs)
        self.duplicates: set[str] = set()
        for key, value in pairs:
            if key in self or key in self.duplicates:
                self.duplicates.add(key)
                self.pop(key, None)
            else:
                self[key] = value


def _duplicate_paths(value: object, path: tuple[str | int, ...] = ()) -> list[tuple[str | int, ...]]:
    paths = [(*path, key) for key in value.duplicates] if isinstance(value, _Pairs) else []
    if isinstance(value, Mapping):
        for key, child in value.items():
            paths.extend(_duplicate_paths(child, (*path, key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(_duplicate_paths(child, (*path, index)))
    return paths


@lru_cache(maxsize=128)
def _field_schema(canonical: type[BaseModel], name: str) -> type[BaseModel]:
    info = canonical.model_fields[name]
    annotation = GenericAlias(list, _review_wire_schema()) if name == "reviews" else info.annotation
    fields: dict[str, Any] = {name: (annotation, deepcopy(info))}
    return create_model("AuditField_" + name, **fields)


def _strict_shape(value: Any, node: Mapping[str, Any], defs: Mapping[str, Any]) -> None:
    """SDKのstrict schemaと同じ必須プロパティ境界を確認する。値の型はPydanticで検証する。"""
    if "$ref" in node:
        _strict_shape(value, defs[str(node["$ref"]).split("/")[-1]], defs)
        return
    if "anyOf" in node:
        for child in node["anyOf"]:
            if value is None and child.get("type") == "null":
                return
            if value is not None and child.get("type") != "null":
                _strict_shape(value, child, defs)
                return
        return
    if isinstance(value, Mapping) and node.get("type") == "object":
        if isinstance(value, _Pairs) and value.duplicates:
            raise AuditWireContractError("duplicate_json_key")
        properties = node.get("properties", {})
        if not set(properties) <= set(value):
            raise AuditWireContractError("missing_value_field")
        if node.get("additionalProperties") is False and set(value) - set(properties):
            raise AuditWireContractError("extra_value_field")
        for key, child in properties.items():
            _strict_shape(value[key], child, defs)
    elif isinstance(value, list) and "items" in node:
        for child in value:
            _strict_shape(child, node["items"], defs)


def _validated_value(value: Any, model: type[BaseModel]) -> BaseModel:
    node = model.model_json_schema()
    _strict_shape(value, node, node.get("$defs", {}))
    return model.model_validate(value, strict=True)


def _safe_keys(keys: list[str], expected: tuple[str, ...]) -> list[str]:
    return [
        key if key in expected or re.fullmatch(r"Q[0-9]{1,8}", key) else "unknown-key-" + sha256(key.encode()).hexdigest()[:12]
        for key in keys[:256]
    ]


def _bound_rows(
    value: _Pairs, field: str, expected: tuple[str, ...], model: type[BaseModel], identity: str
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    containers = [child for key, child in value.raw_pairs if key == field]
    keys = [key for child in containers if isinstance(child, _Pairs) for key in child.key_sequence]
    raw = value.get(field)
    errors: list[dict[str, Any]] = []
    states: dict[str, str] = {}
    rows = []
    if field in value.duplicates:
        errors.append({"field": field, "code": "duplicate_json_key"})
    elif not isinstance(raw, _Pairs):
        errors.append({"field": field, "code": "invalid_bound_object"})
    else:
        extra = set(raw.key_sequence) - set(expected)
        if extra:
            errors.append({"field": field, "code": "extra_bound_keys", "count": len(extra)})
    for key in expected:
        if field in value.duplicates or isinstance(raw, _Pairs) and key in raw.duplicates:
            states[key] = "duplicate"
            errors.append({"field": field, "id": key, "code": "duplicate_json_key"})
        elif not isinstance(raw, _Pairs) or key not in raw:
            states[key] = "missing"
            errors.append({"field": field, "id": key, "code": "missing_bound_key"})
        else:
            try:
                parsed = _validated_value(raw[key], model)
                rows.append({identity: key, **parsed.model_dump()})
                states[key] = "valid"
            except (ValidationError, AuditWireContractError) as exc:
                states[key] = "invalid"
                errors.append({"field": field, "id": key, "code": "invalid_bound_value", "details": structured_failure_details(exc, model)})
    metadata = {
        "expected_ids": list(expected),
        "raw_key_sequence": _safe_keys(keys, expected),
        "raw_key_count": len(keys),
        "raw_keys_truncated": len(keys) > 256,
        "adopted_ids": [row[identity] for row in rows],
        "states": states,
    }
    return rows, metadata, errors


def decode_audit_wire(text: str, schema: type[BaseModel]) -> AuditWireResult:
    if not is_audit_wire(schema):
        raise TypeError("audit wire schema required")
    schema = cast(type[_AuditWire], schema)
    value = json.loads(text, object_pairs_hook=_Pairs)
    if not isinstance(value, _Pairs):
        raise AuditWireContractError("invalid_audit_wire")
    canonical = schema.canonical_schema
    errors: list[dict[str, Any]] = []
    normalized: dict[str, Any] = {}
    fields: dict[str, str] = {}
    field_rows: dict[str, Any] = {}
    unknown_top = set(value.key_sequence) - set(canonical.model_fields)
    if unknown_top:
        errors.append({"code": "extra_audit_fields", "count": len(unknown_top)})
    bound_fields = {"request_reviews", "guide_requirement_reviews"}
    for name in canonical.model_fields:
        if name in bound_fields:
            continue
        try:
            if name in value.duplicates:
                raise AuditWireContractError("duplicate_json_key")
            if name not in value:
                raise AuditWireContractError("missing_audit_field")
            field = _validated_value({name: value[name]}, _field_schema(canonical, name))
            normalized[name] = getattr(field, name)
            fields[name] = "valid"
        except (ValidationError, AuditWireContractError) as exc:
            fields[name] = "unverified"
            errors.append(
                {"field": name, "code": "invalid_audit_field", "details": structured_failure_details(exc, _field_schema(canonical, name))}
            )
            normalized[name] = (
                False
                if name == "summary_supported"
                else "partial"
                if name == "goal_alignment"
                else None
                if name == "summary_output_fields"
                else []
            )
            annotation = _field_schema(canonical, name).model_fields[name].annotation
            element = get_args(annotation)[0] if get_origin(annotation) is list else None
            if isinstance(element, type) and issubclass(element, BaseModel) and isinstance(value.get(name), list):
                kept, valid_indices, invalid_indices = [], [], []
                for index, entry in enumerate(value[name]):
                    try:
                        kept.append(_validated_value(entry, element))
                        valid_indices.append(index)
                    except (ValidationError, AuditWireContractError):
                        invalid_indices.append(index)
                field_rows[name] = {"valid_indices": valid_indices, "invalid_indices": invalid_indices}
                with suppress(ValidationError):
                    normalized[name] = getattr(_field_schema(canonical, name).model_validate({name: kept}, strict=True), name)
    requests, request_meta, request_errors = _bound_rows(
        value, "request_reviews", schema.expected_request_ids, schema.request_value_schema, "request_id"
    )
    requirements, requirement_meta, requirement_errors = _bound_rows(
        value, "guide_requirement_reviews", schema.expected_requirement_ids, schema.requirement_value_schema, "requirement_id"
    )
    normalized.update(request_reviews=requests, guide_requirement_reviews=requirements)
    errors.extend(request_errors)
    errors.extend(requirement_errors)
    normalized["reviews"] = [
        {
            **row.model_dump(),
            "applicability": row.source_applicability,
            "condition": row.source_condition,
        }
        for row in normalized.get("reviews", [])
    ]
    audit = GroundedAudit.model_validate(normalized)
    validation = {
        "status": "completed" if not errors else "partial" if all(e["code"] == "missing_bound_key" for e in errors) else "error",
        "strict_wire_validation": "failed" if errors else "passed",
        "fields": fields,
        "field_rows": field_rows,
        "requests": request_meta["states"],
        "requirements": requirement_meta["states"],
        "request_binding": request_meta,
        "requirement_binding": requirement_meta,
        "errors": errors[:128],
        "error_count": len(errors),
        "errors_truncated": len(errors) > 128,
        "raw_text_sha256": sha256(text.encode()).hexdigest(),
    }
    result = AuditWireResult(audit=audit, validation=validation)
    result._raw_text = text
    return result


def _schema_view(value: Any, node: Mapping[str, Any], defs: Mapping[str, Any]) -> Any:
    """明示的なテスト・保存応答移行で、モデル内部の方針fieldをwireへ混ぜない。"""
    if "$ref" in node:
        return _schema_view(value, defs[str(node["$ref"]).split("/")[-1]], defs)
    if "anyOf" in node and value is not None:
        node = next((child for child in node["anyOf"] if child.get("type") != "null"), node)
        return _schema_view(value, node, defs)
    if isinstance(value, Mapping) and node.get("type") == "object":
        return {key: _schema_view(value[key], child, defs) for key, child in node.get("properties", {}).items() if key in value}
    if isinstance(value, list) and "items" in node:
        return [_schema_view(child, node["items"], defs) for child in value]
    return value


def canonical_audit_result(value: object, schema: type[BaseModel]) -> AuditWireResult:
    """本番codecの結果または明示的なwire型だけを受ける。canonical listへ自動fallbackしない。"""
    if isinstance(value, AuditWireResult):
        return value
    if isinstance(value, schema) and is_audit_wire(schema):
        node = schema.model_json_schema()
        raw = _schema_view(value.model_dump(mode="json", by_alias=True), node, node.get("$defs", {}))
        result = decode_audit_wire(json.dumps(raw, ensure_ascii=False), schema)
        # 型付きstubの既定値展開を、実際のraw応答のstrict検証と混同しない。
        result.validation.update(source="typed_wire_value", transport_not_observed=True, strict_wire_validation="not_observed")
        return result
    raise AuditWireContractError("invalid_audit_wire")
