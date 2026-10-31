"""旧監査の決定論fixtureを明示的に移す。本番のraw wire検証成功にはしない。"""

from collections.abc import Mapping
from typing import Any, cast

from pydantic import BaseModel

from rag_engine.models.audit_wire import AuditWireContractError, AuditWireResult, _schema_view, is_audit_wire
from rag_engine.models.llm import GroundedAudit


def legacy_audit_result(value: Mapping[str, Any] | BaseModel, schema: type[BaseModel]) -> AuditWireResult:
    if not is_audit_wire(schema):
        raise TypeError("audit wire schema required")
    wire = cast(Any, schema)
    raw = value.model_dump() if isinstance(value, BaseModel) else dict(value)
    audit = GroundedAudit.model_validate(raw)
    validation: dict[str, Any] = {
        "source": "legacy_fixture",
        "transport_not_observed": True,
        "strict_wire_validation": "not_observed",
        "status": "completed",
        "fields": {},
        "errors": [],
    }
    for field, identity, expected, key, binding in [
        ("request_reviews", "request_id", wire.expected_request_ids, "requests", "request_binding"),
        ("guide_requirement_reviews", "requirement_id", wire.expected_requirement_ids, "requirements", "requirement_binding"),
    ]:
        ids = [row[identity] for row in raw.get(field, [])]
        states = {name: "valid" if ids.count(name) == 1 else "duplicate" if ids.count(name) > 1 else "missing" for name in expected}
        validation[key] = states
        validation[binding] = {
            "expected_ids": list(expected),
            "raw_key_sequence": ids,
            "raw_key_count": len(ids),
            "raw_keys_truncated": False,
            "adopted_ids": [name for name in expected if states[name] == "valid"],
            "states": states,
        }
        if set(ids) - set(expected) or any(state != "valid" for state in states.values()):
            validation["status"] = "partial"
    return AuditWireResult(audit=audit, validation=validation)


def legacy_audit_to_wire_fixture(value: Mapping[str, Any] | BaseModel, schema: type[BaseModel]) -> dict[str, Any]:
    """完全な正向fixtureの値を固定キーへ変換。不足キーは勝手に判定して埋めない。"""
    raw = value.model_dump() if isinstance(value, BaseModel) else dict(value)
    audit = GroundedAudit.model_validate(raw)
    result = audit.model_dump(mode="json")
    # 旧fixtureが明示した資料適用の観察を新wireへ移す。実応答の欠落値は補わない。
    for row in result["reviews"]:
        if row.get("source_applicability") is None:
            row["source_applicability"] = row["applicability"]
            row["source_condition"] = row["condition"]
    for field, identity in [("request_reviews", "request_id"), ("guide_requirement_reviews", "requirement_id")]:
        rows = result[field]
        ids = [row[identity] for row in rows]
        if len(set(ids)) != len(ids):
            raise AuditWireContractError("duplicate_legacy_ids")
        result[field] = {row[identity]: {key: val for key, val in row.items() if key != identity} for row in rows}
    if "unused_evidence_ids" not in schema.model_fields:
        result.pop("unused_evidence_ids", None)
    node = schema.model_json_schema()
    return cast(dict[str, Any], _schema_view(result, node, node.get("$defs", {})))
