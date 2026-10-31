"""固定IDのstrict wireと局所隔離。意味の正しさをcodecで証明しない。"""

import json
from copy import deepcopy
from typing import Any, cast

import pytest
from audit_fixtures import legacy_audit_result, legacy_audit_to_wire_fixture
from openai.lib._parsing._responses import type_to_text_format_param

from rag_engine.models.audit_wire import (
    AuditWireContractError,
    AuditWireResult,
    audit_wire_schema,
    canonical_audit_result,
    decode_audit_wire,
)
from rag_engine.models.llm import GroundedAudit, GroundedFactAudit, GroundedItemReview, GroundedRequestReview, GuideRequirementReview


def sample_wire(*, fact: bool = False) -> tuple[type[Any], dict[str, Any]]:
    base = GroundedFactAudit if fact else GroundedAudit
    schema = audit_wire_schema(base, ("Q1", "Q2"), ("step:0", "step:1"))
    audit = GroundedAudit(
        reviews=[GroundedItemReview(index=0, support="supported", applicability="matched", reason="原文の操作")],
        request_reviews=[GroundedRequestReview(request_id=name, status="unknown", reason="未確認") for name in ["Q1", "Q2"]],
        guide_requirement_reviews=[
            GuideRequirementReview(requirement_id=name, answer_state="not_mentioned", item_indexes=[], reason="未説明")
            for name in ["step:0", "step:1"]
        ],
        summary_supported=False,
        goal_alignment="partial",
    )
    return schema, legacy_audit_to_wire_fixture(audit, schema)


def deref(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    return schema["$defs"][node["$ref"].split("/")[-1]] if "$ref" in node else node


def decode(value: dict[str, Any], schema: type[Any]) -> AuditWireResult:
    return decode_audit_wire(json.dumps(value, ensure_ascii=False), schema)


@pytest.mark.parametrize("fact", [False, True])
def test_sdk_strict_schema_binds_both_id_sets_and_does_not_ask_for_child_ids(fact: bool) -> None:
    schema, payload = sample_wire(fact=fact)
    wire = cast(dict[str, Any], type_to_text_format_param(schema))["schema"]
    for field, ids, absent in [
        ("request_reviews", ["Q1", "Q2"], "request_id"),
        ("guide_requirement_reviews", ["step:0", "step:1"], "requirement_id"),
    ]:
        node = deref(wire, wire["properties"][field])
        assert node["required"] == ids and node["additionalProperties"] is False
        value = deref(wire, node["properties"][ids[0]])
        assert absent not in value["properties"]
        assert set(value["required"]) == set(value["properties"])
    result = decode(payload, schema)
    assert result.validation["strict_wire_validation"] == "passed"
    assert [row.request_id for row in result.audit.request_reviews] == ["Q1", "Q2"]
    assert [row.requirement_id for row in result.audit.guide_requirement_reviews] == ["step:0", "step:1"]
    assert audit_wire_schema(GroundedFactAudit if fact else GroundedAudit, ("Q1", "Q2"), ("step:0", "step:1")) is schema


@pytest.mark.parametrize(
    "field, key, state", [("request_reviews", "Q1", "requests"), ("guide_requirement_reviews", "step:0", "requirements")]
)
@pytest.mark.parametrize("failure", ["missing", "enum", "missing_detail", "extra_identity"])
def test_bad_bound_value_isolated_without_discarding_other_results(field: str, key: str, state: str, failure: str) -> None:
    schema, payload = sample_wire()
    if failure == "missing":
        payload[field].pop(key)
    elif failure == "enum":
        payload[field][key]["status" if state == "requests" else "answer_state"] = "invalid"
    elif failure == "missing_detail":
        payload[field][key].pop("reason")
    else:
        payload[field][key]["request_id" if state == "requests" else "requirement_id"] = key
    result = decode(payload, schema)
    assert result.validation["strict_wire_validation"] == "failed"
    assert result.validation[state][key] == ("missing" if failure == "missing" else "invalid")
    assert len(result.audit.reviews) == 1
    assert len(result.audit.request_reviews) == (1 if state == "requests" else 2)
    assert len(result.audit.guide_requirement_reviews) == (1 if state == "requirements" else 2)


@pytest.mark.parametrize(
    "field,key,states", [("request_reviews", "Q1", "requests"), ("guide_requirement_reviews", "step:0", "requirements")]
)
@pytest.mark.parametrize("reverse", [False, True])
def test_raw_duplicate_key_never_last_wins(field: str, key: str, states: str, reverse: bool) -> None:
    schema, payload = sample_wire()
    original = json.dumps(payload[field][key])
    other = deepcopy(payload[field][key])
    other["reason"] = "異なる判定"
    values = [original, json.dumps(other)]
    if reverse:
        values.reverse()
    encoded = json.dumps(payload)
    token = json.dumps(key) + ": " + original
    assert token in encoded
    raw = encoded.replace(token, json.dumps(key) + ": " + values[0] + ", " + json.dumps(key) + ": " + values[1], 1)
    result = decode_audit_wire(raw, schema)
    assert result.validation[states][key] == "duplicate"
    binding = result.validation["request_binding" if states == "requests" else "requirement_binding"]
    assert binding["raw_key_sequence"].count(key) == 2 and key not in binding["adopted_ids"]
    assert len(result.audit.reviews) == 1


def test_nested_duplicate_isolates_only_its_request() -> None:
    schema, payload = sample_wire()
    raw = json.dumps(payload, ensure_ascii=False).replace('"reason": "未確認"', '"reason": "未確認", "reason": "別の判定"', 1)
    result = decode_audit_wire(raw, schema)
    assert result.validation["requests"] == {"Q1": "invalid", "Q2": "valid"}
    assert len(result.audit.guide_requirement_reviews) == 2


def test_unknown_key_is_not_rebound_or_disclosed() -> None:
    schema, payload = sample_wire()
    payload["request_reviews"]["private-canary-input"] = payload["request_reviews"].pop("Q1")
    result = decode(payload, schema)
    assert result.validation["requests"]["Q1"] == "missing"
    assert [row.request_id for row in result.audit.request_reviews] == ["Q2"]
    assert "private-canary-input" not in json.dumps(result.validation)
    assert result.validation["request_binding"]["raw_key_sequence"][0] == "Q2"


def test_invalid_claim_row_preserves_other_claims_and_bound_results() -> None:
    schema, payload = sample_wire()
    payload["reviews"].append({**payload["reviews"][0], "index": 1, "support": "invalid"})
    result = decode(payload, schema)
    assert result.validation["fields"]["reviews"] == "unverified"
    assert [row.index for row in result.audit.reviews] == [0]
    assert result.validation["field_rows"]["reviews"] == {"valid_indices": [0], "invalid_indices": [1]}
    assert len(result.audit.request_reviews) == 2 and len(result.audit.guide_requirement_reviews) == 2


def test_duplicate_top_container_is_local_not_global_or_last_wins() -> None:
    schema, payload = sample_wire()
    raw = json.dumps(payload)
    request = json.dumps(payload["request_reviews"])
    raw = raw.replace('"request_reviews": ' + request, '"request_reviews": ' + request + ', "request_reviews": {}')
    result = decode_audit_wire(raw, schema)
    assert result.validation["requests"] == {"Q1": "duplicate", "Q2": "duplicate"}
    assert not result.audit.request_reviews
    assert len(result.audit.reviews) == 1 and len(result.audit.guide_requirement_reviews) == 2


def test_canonical_legacy_lists_are_never_a_silent_runtime_wire_fallback() -> None:
    schema, payload = sample_wire()
    result = decode(payload, schema)
    with pytest.raises(AuditWireContractError):
        canonical_audit_result(result.audit, schema)
    fixture = legacy_audit_result(result.audit, schema)
    assert fixture.validation["strict_wire_validation"] == "not_observed"
    assert fixture.validation["transport_not_observed"] is True
    assert canonical_audit_result(fixture, schema) is fixture
    assert "raw_text" not in result.model_dump()
    assert result.raw_text == json.dumps(payload, ensure_ascii=False)


def test_empty_expected_sets_are_required_empty_objects() -> None:
    schema = audit_wire_schema(GroundedAudit, (), ())
    payload = legacy_audit_to_wire_fixture(GroundedAudit(reviews=[], summary_supported=False, goal_alignment="partial"), schema)
    assert payload["request_reviews"] == payload["guide_requirement_reviews"] == {}
    assert decode(payload, schema).validation["strict_wire_validation"] == "passed"


def test_typed_wire_fixture_is_not_misreported_as_observed_raw_success() -> None:
    schema, payload = sample_wire()
    payload["request_reviews"]["Q1"].pop("evidence_status")
    typed = schema.model_validate(payload)
    result = canonical_audit_result(typed, schema)
    assert result.validation["source"] == "typed_wire_value"
    assert result.validation["transport_not_observed"] is True
    assert result.validation["strict_wire_validation"] == "not_observed"
    assert decode(payload, schema).validation["requests"]["Q1"] == "invalid"
