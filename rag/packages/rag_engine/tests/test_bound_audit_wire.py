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
from rag_engine.models.llm import GroundedAudit, GroundedFactAudit, GroundedItemReview, GroundedRequestReview


def sample_wire(*, fact: bool = False) -> tuple[type[Any], dict[str, Any]]:
    base = GroundedFactAudit if fact else GroundedAudit
    schema = audit_wire_schema(base, ("Q1", "Q2"))
    audit = GroundedAudit(
        reviews=[GroundedItemReview(index=0, support="supported", applicability="matched", reason="原文の操作")],
        request_reviews=[GroundedRequestReview(request_id=name, status="unknown", reason="未確認") for name in ["Q1", "Q2"]],
        summary_supported=False,
        goal_alignment="partial",
    )
    return schema, legacy_audit_to_wire_fixture(audit, schema)


def deref(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    return schema["$defs"][node["$ref"].split("/")[-1]] if "$ref" in node else node


def decode(value: dict[str, Any], schema: type[Any]) -> AuditWireResult:
    return decode_audit_wire(json.dumps(value, ensure_ascii=False), schema)


@pytest.mark.parametrize("fact", [False, True])
def test_sdk_strict_schema_binds_the_id_set_and_does_not_ask_for_child_ids(fact: bool) -> None:
    schema, payload = sample_wire(fact=fact)
    wire = cast(dict[str, Any], type_to_text_format_param(schema))["schema"]
    ids = ["Q1", "Q2"]
    node = deref(wire, wire["properties"]["request_reviews"])
    assert node["required"] == ids and node["additionalProperties"] is False
    value = deref(wire, node["properties"][ids[0]])
    assert "request_id" not in value["properties"]
    assert set(value["required"]) == set(value["properties"])
    result = decode(payload, schema)
    assert result.validation["strict_wire_validation"] == "passed"
    assert [row.request_id for row in result.audit.request_reviews] == ["Q1", "Q2"]
    assert audit_wire_schema(GroundedFactAudit if fact else GroundedAudit, ("Q1", "Q2")) is schema


@pytest.mark.parametrize("failure", ["missing", "enum", "missing_detail", "extra_identity"])
def test_bad_bound_value_isolated_without_discarding_other_results(failure: str) -> None:
    field, key = "request_reviews", "Q1"
    schema, payload = sample_wire()
    if failure == "missing":
        payload[field].pop(key)
    elif failure == "enum":
        payload[field][key]["status"] = "invalid"
    elif failure == "missing_detail":
        payload[field][key].pop("reason")
    else:
        payload[field][key]["request_id"] = key
    result = decode(payload, schema)
    assert result.validation["strict_wire_validation"] == "failed"
    assert result.validation["requests"][key] == ("missing" if failure == "missing" else "invalid")
    assert len(result.audit.reviews) == 1
    assert len(result.audit.request_reviews) == 1


@pytest.mark.parametrize("reverse", [False, True])
def test_raw_duplicate_key_never_last_wins(reverse: bool) -> None:
    field, key = "request_reviews", "Q1"
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
    assert result.validation["requests"][key] == "duplicate"
    binding = result.validation["request_binding"]
    assert binding["raw_key_sequence"].count(key) == 2 and key not in binding["adopted_ids"]
    assert len(result.audit.reviews) == 1


def test_nested_duplicate_isolates_only_its_request() -> None:
    schema, payload = sample_wire()
    raw = json.dumps(payload, ensure_ascii=False).replace('"reason": "未確認"', '"reason": "未確認", "reason": "別の判定"', 1)
    result = decode_audit_wire(raw, schema)
    assert result.validation["requests"] == {"Q1": "invalid", "Q2": "valid"}


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
    assert len(result.audit.request_reviews) == 2


def test_duplicate_top_container_is_local_not_global_or_last_wins() -> None:
    schema, payload = sample_wire()
    raw = json.dumps(payload)
    request = json.dumps(payload["request_reviews"])
    raw = raw.replace('"request_reviews": ' + request, '"request_reviews": ' + request + ', "request_reviews": {}')
    result = decode_audit_wire(raw, schema)
    assert result.validation["requests"] == {"Q1": "duplicate", "Q2": "duplicate"}
    assert not result.audit.request_reviews
    assert len(result.audit.reviews) == 1


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
    schema = audit_wire_schema(GroundedAudit, ())
    payload = legacy_audit_to_wire_fixture(GroundedAudit(reviews=[], summary_supported=False, goal_alignment="partial"), schema)
    assert payload["request_reviews"] == {}
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


def _any_of_branches(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, list):
        return [branch for child in node for branch in _any_of_branches(child)]
    if not isinstance(node, dict):
        return []
    return [*node.get("anyOf", []), *(branch for child in node.values() for branch in _any_of_branches(child))]


@pytest.mark.parametrize("fact", [False, True])
def test_sent_schema_gives_every_any_of_branch_a_type(fact: bool) -> None:
    """Optional[Model] の $ref だけの枝を参照先で展開し、枝ごとに type を送る（#168）。codec の検査は変わらない。"""
    from rag_engine.adapters.model_api import _send_request

    schema, payload = sample_wire(fact=fact)
    sdk = cast(dict[str, Any], type_to_text_format_param(schema))["schema"]
    assert any("type" not in branch for branch in _any_of_branches(sdk))
    request = _send_request({"model": "synthetic-model", "input": [], "text_format": schema})
    assert "text_format" not in request
    sent = request["text"]["format"]["schema"]
    branches = _any_of_branches(sent)
    assert branches and all("type" in branch for branch in branches)
    review = sent["$defs"]["SourceItemReview"]
    expanded = next(branch for branch in review["properties"]["output_fields"]["anyOf"] if branch["type"] != "null")
    assert expanded == sdk["$defs"][expanded["title"]]
    assert expanded["additionalProperties"] is False and set(expanded["required"]) == set(expanded["properties"])
    assert decode(payload, schema).validation["strict_wire_validation"] == "passed"


def test_self_reference_expands_once_without_recursing() -> None:
    from rag_engine.adapters.model_api import _typed_any_of

    defs = {"Node": {"type": "object", "properties": {"child": {"anyOf": [{"$ref": "#/$defs/Node"}, {"type": "null"}]}}}}
    out = _typed_any_of({"$defs": defs, "$ref": "#/$defs/Node"}, defs)
    inner = out["$defs"]["Node"]["properties"]["child"]["anyOf"][0]
    assert inner["type"] == "object"
    assert inner["properties"]["child"]["anyOf"][0] == {"$ref": "#/$defs/Node"}
