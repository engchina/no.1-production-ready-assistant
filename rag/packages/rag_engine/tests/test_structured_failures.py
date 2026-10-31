"""合成のエラー連鎖で、安全な診断の境界を確認する。モデルは呼ばない。"""

import json
from enum import StrEnum
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError

from rag_engine.resources.structured_failures import structured_failure_details


class Choice(StrEnum):
    FIRST = "first"
    SECOND = "second"


class Row(BaseModel):
    model_config = ConfigDict(extra="forbid")
    choice: Choice
    count: int


class Output(BaseModel):
    rows: list[Row]
    dynamic: dict[str, int] = {}


class ProviderFailure(RuntimeError):
    def __init__(self, response: object) -> None:
        super().__init__("secret provider body")
        self.response = response


class ContractFailure(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__("secret request identifier")
        self.failure_code = code


def validation_error(payload: dict[str, Any]) -> ValidationError:
    with pytest.raises(ValidationError) as caught:
        Output.model_validate(payload)
    return caught.value


def test_safe_schema_fields_and_indices_are_kept_without_input_or_messages() -> None:
    error = validation_error({"rows": [{"choice": "secret-value", "count": "secret-token", "secret-key": "secret"}]})
    error.__cause__ = ValueError("another-secret")
    details = structured_failure_details(error, Output)
    assert details["error_class"] == "ValidationError"
    assert details["cause_classes"] == ["ValueError"]
    assert details["category"] == "schema_enum"
    assert {tuple(row["loc"]) for row in details["validation_errors"]} == {
        ("rows", 0, "choice"),
        ("rows", 0, "count"),
        ("rows", 0, "<redacted>"),
    }
    assert "secret" not in json.dumps(details)
    assert not any(key in row for row in details["validation_errors"] for key in ("msg", "input", "ctx", "url"))


def test_dynamic_dictionary_keys_are_not_schema_properties() -> None:
    error = validation_error({"rows": [], "dynamic": {"rows": "secret", "secret-key": "secret"}})
    details = structured_failure_details(error, Output)
    assert [row["loc"] for row in details["validation_errors"]] == [["dynamic", "<redacted>"], ["dynamic", "<redacted>"]]


def test_no_schema_redacts_all_field_names() -> None:
    details = structured_failure_details(validation_error({"rows": [{"choice": "first"}]}))
    assert details["validation_errors"][0]["loc"] == ["<redacted>", 0, "<redacted>"]
    assert details["category"] == "schema_field"


def test_cause_chain_keeps_json_failure_without_guessing_truncation() -> None:
    with pytest.raises(json.JSONDecodeError) as caught:
        json.loads('{"secret-key":')
    outer = RuntimeError("secret output")
    outer.__cause__ = caught.value
    details = structured_failure_details(outer, Output)
    assert details == {"error_class": "RuntimeError", "cause_classes": ["JSONDecodeError"], "category": "invalid_json"}


def test_pydantic_json_invalid_is_not_automatically_truncated() -> None:
    with pytest.raises(ValidationError) as caught:
        Output.model_validate_json('{"rows":')
    details = structured_failure_details(caught.value, Output)
    assert details["category"] == "invalid_json"
    assert details["validation_errors"] == [{"type": "json_invalid", "loc": []}]


@pytest.mark.parametrize(
    ("status", "reason", "category"),
    [
        ("incomplete", "max_output_tokens", "truncated_output"),
        ("incomplete", "content_filter", "incomplete_output"),
        ("incomplete", "secret-reason", "incomplete_output"),
        ("completed", "max_output_tokens", "structure_unknown"),
    ],
)
def test_only_explicit_provider_metadata_establishes_incompleteness(status: str, reason: str, category: str) -> None:
    error = ProviderFailure(SimpleNamespace(status=status, incomplete_details={"reason": reason}))
    details = structured_failure_details(error)
    assert details["category"] == category
    assert details["provider_status"] == status
    assert "secret" not in json.dumps(details)


@pytest.mark.parametrize("code", ["duplicate_request_ids", "missing_request_id", "extra_request_id", "invalid_request_ids"])
def test_typed_request_code_is_classified_without_identifier_values(code: str) -> None:
    details = structured_failure_details(ContractFailure(code))
    assert details["category"] == "request_id_contract"
    assert details["contract_code"] == code
    assert "secret" not in json.dumps(details)
    assert "missing" not in details.get("material_status", "")


@pytest.mark.parametrize("code", ["citation_id_ambiguous", "citation_range_invalid", "citation_version_mismatch"])
def test_deterministic_evidence_codes_do_not_imply_semantic_support(code: str) -> None:
    details = structured_failure_details(ContractFailure(code))
    assert details["category"] == "evidence_binding"
    assert "support" not in details
    assert "evidence_status" not in details


def test_exception_text_cannot_create_contract_or_provider_facts() -> None:
    error = ContractFailure("secret-code")
    error.args = ("duplicate_request_ids max_output_tokens available missing",)
    assert structured_failure_details(error) == {"error_class": "ContractFailure", "cause_classes": [], "category": "structure_unknown"}


def test_untrusted_custom_error_type_and_location_are_redacted_and_bounded() -> None:
    errors: list[InitErrorDetails] = [
        {
            "type": PydanticCustomError("secret-error", "secret message", {"secret": "value"}),
            "loc": ("secret-key",) * 20,
            "input": "secret input",
        }
        for _ in range(30)
    ]
    error = ValidationError.from_exception_data("secret-title", errors)
    details = structured_failure_details(error, Output.model_json_schema())
    assert len(details["validation_errors"]) == 12
    assert all(row == {"type": "validation_error", "loc": ["<redacted>"] * 8} for row in details["validation_errors"])
    assert "secret" not in json.dumps(details)


def test_cycle_in_cause_chain_is_bounded() -> None:
    first = RuntimeError("secret")
    second = ValueError("secret")
    first.__cause__ = second
    second.__cause__ = first
    assert structured_failure_details(first)["cause_classes"] == ["ValueError"]
