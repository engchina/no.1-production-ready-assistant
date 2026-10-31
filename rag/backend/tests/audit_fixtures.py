"""旧形式の監査fixtureを明示的に再生する。通信や新wireの検証を成功扱いしない。"""

from typing import Any, cast

from pydantic import BaseModel
from rag_engine.models.audit_wire import AuditWireResult, is_audit_wire
from rag_engine.models.llm import GroundedAudit, GroundedFactAudit


def is_audit_schema(schema: type) -> bool:
    return schema in (GroundedAudit, GroundedFactAudit) or is_audit_wire(schema)


def legacy_audit_result(schema: type, data: dict[str, Any]) -> Any:
    if not is_audit_wire(schema):
        return cast(type[BaseModel], schema).model_validate(data)
    # 既存の意味判定fixtureを読むだけで、実際のwire形や転送を検証したとは記録しない。
    canonical_schema = cast(type[BaseModel], cast(Any, schema).canonical_schema)
    canonical = canonical_schema.model_validate(data)
    return AuditWireResult(
        audit=GroundedAudit.model_validate(canonical.model_dump(mode="json")),
        validation={
            "source": "legacy_fixture",
            "strict_wire_validation": "not_observed",
            "transport_not_observed": True,
        },
    )
