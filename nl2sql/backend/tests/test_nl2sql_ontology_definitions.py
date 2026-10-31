"""型付き構築結果の worker 接続・所有境界・安定性。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError
from test_nl2sql_ontology_build import (
    _FakeEnterpriseAiClient,
    _FakeLegacyNl2SqlService,
    _wait_for_job,
)

from app.features.nl2sql.ontology_build import OntologyBuildService, parse_extraction
from app.features.nl2sql.ontology_definition_service import ProfileOntologyDefinitionService
from app.features.nl2sql.ontology_definitions import (
    DEFINITION_KINDS,
    FunctionDefinition,
    ObjectTypeDefinition,
)
from app.features.nl2sql.ontology_models import OntologyBuildExtraction
from app.features.nl2sql.ontology_router import OntologyApiRuntime
from app.features.nl2sql.ontology_service import OntologyNotFoundError
from app.features.nl2sql.ontology_store import InMemoryOntologyStore
from app.features.nl2sql.structured_outputs import response_format


def definition_payload() -> list[dict[str, Any]]:
    return [
        {
            "kind": "object_type",
            "api_name": "Order",
            "name_ja": "受注",
            "primary_key": ["Order.id"],
            "properties": ["Order.id"],
            "grain_ja": "受注単位",
            # 表の決まらないオブジェクト型は公開に含めないため、対応する表を持たせる（#1729）。
            "mappings": [{"owner": "APP", "object_name": "ORDERS"}],
        },
        {
            "kind": "property",
            "api_name": "Order.id",
            "name_ja": "受注番号",
            "object_type": "Order",
            "data_type": "integer",
            "mappings": [{"owner": "APP", "object_name": "ORDERS", "column_name": "ID"}],
        },
        {
            "kind": "link_type",
            "api_name": "related",
            "name_ja": "関連受注",
            "source": "Order",
            "target": "Order",
        },
        {
            "kind": "function",
            "api_name": "calculate",
            "name_ja": "金額計算",
            "return_type": "number",
        },
        {"kind": "action_type", "api_name": "approve", "name_ja": "承認", "object_type": "Order"},
        # 名前だけの空の定義は AI の雛形として除くため、中身（説明）を持たせる（#1720）。
        {
            "kind": "interface",
            "api_name": "Approvable",
            "name_ja": "承認可能",
            "description_ja": "承認の操作を持つ対象。",
        },
    ]


def runtime() -> tuple[OntologyApiRuntime, _FakeLegacyNl2SqlService]:
    legacy = _FakeLegacyNl2SqlService()
    return OntologyApiRuntime(legacy_service=legacy, store=InMemoryOntologyStore()), legacy


def test_full_concepts_flow_through_real_build_worker() -> None:
    rt, legacy = runtime()
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(
        json.dumps({"definitions": definition_payload()})
    )
    service = OntologyBuildService(rt)
    job = _wait_for_job(service, service.start("sales", business_text="受注を承認する。").id)
    assert job.status == "succeeded", job.error_message_ja
    bundle = ProfileOntologyDefinitionService(rt).get("sales", job.result_bundle_id)
    assert len(bundle.definitions) == 6
    assert len(bundle.coverage) == len(DEFINITION_KINDS)
    assert all(item.review_status == "unreviewed" for item in bundle.definitions)
    assert sum(item.count for item in bundle.coverage) == 6
    assert (
        next(item for item in bundle.coverage if item.kind == "metric").status
        == "insufficient_evidence"
    )
    assert job.draft_revision_id  # 旧成果物も生成する


def test_profile_identity_and_artifact_access_are_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    rt, legacy = runtime()
    profiles = {
        name: legacy.profile.model_copy(update={"id": name}) for name in ("sales", "support")
    }
    monkeypatch.setattr(rt, "ensure_profile", lambda profile_id: profiles[profile_id])
    monkeypatch.setattr(rt, "_strict_profile", lambda profile_id: profiles[profile_id])
    service = ProfileOntologyDefinitionService(rt)
    bundles = [
        service.save_build(
            profile_id=name,
            job_id="same-job",
            definitions=[ObjectTypeDefinition(api_name="Order", name_ja=name)],
            schema_fingerprint="schema",
            source_revision_id="legacy",
        )
        for name in profiles
    ]
    assert bundles[0].id != bundles[1].id
    assert bundles[0].definitions[0].id != bundles[1].definitions[0].id
    assert len(service.list_results("sales")) == 1
    with pytest.raises(OntologyNotFoundError):
        service.get("support", bundles[0].id)


def test_rebuild_retains_stable_identity_and_reports_conflict() -> None:
    rt, _ = runtime()
    service = ProfileOntologyDefinitionService(rt)
    first = service.save_build(
        profile_id="sales",
        job_id="one",
        definitions=[ObjectTypeDefinition(api_name="Order", name_ja="受注")],
        schema_fingerprint="schema",
        source_revision_id="legacy",
    )
    duplicate = service.save_build(
        profile_id="sales",
        job_id="one",
        definitions=[],
        schema_fingerprint="schema",
        source_revision_id="legacy",
    )
    assert duplicate == first
    second = service.save_build(
        profile_id="sales",
        job_id="two",
        definitions=[
            ObjectTypeDefinition(api_name="Order", name_ja="受注"),
            ObjectTypeDefinition(api_name="Order", name_ja="発注"),
        ],
        schema_fingerprint="schema",
        source_revision_id="legacy",
    )
    assert second.definitions[0].id == first.definitions[0].id
    assert second.definitions[0].name_ja == "受注"
    assert len(second.conflicts) == 1
    assert second.parent_id == first.id


def test_model_forbids_arbitrary_executable_code_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        FunctionDefinition.model_validate(
            {"api_name": "bad", "name_ja": "不正", "python_code": "print(1)"}
        )
    with pytest.raises(ValidationError):
        parse_extraction(
            json.dumps({"definitions": [{"kind": "unknown", "api_name": "x", "name_ja": "不明"}]})
        )


def test_structured_output_wire_schema_supports_all_concepts() -> None:
    schema = response_format(OntologyBuildExtraction)["schema"]
    assert "definitions" in schema["properties"]
    assert all(
        item.get("additionalProperties") is False
        for item in schema["$defs"].values()
        if item.get("type") == "object"
    )
