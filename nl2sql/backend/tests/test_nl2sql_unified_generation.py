"""統合概念生成・補完・任意の表示プロパティの回帰。外部APIは使用しない。"""

import json
from typing import Any

import pytest
from test_nl2sql_ontology_build import _FakeEnterpriseAiClient, _wait_for_job
from test_nl2sql_ontology_definitions import definition_payload, runtime

from app.features.nl2sql.ontology_build import OntologyBuildService, merge_build_extractions
from app.features.nl2sql.ontology_definition_service import ProfileOntologyDefinitionService
from app.features.nl2sql.ontology_definition_validation import validate_definitions
from app.features.nl2sql.ontology_definitions import (
    DEFINITION_KINDS,
    ObjectTypeDefinition,
    ProfileOntologyBundle,
    PropertyDefinition,
)
from app.features.nl2sql.ontology_models import (
    OntologyBuildExtraction,
    OntologyConceptExtraction,
    QaPair,
)
from app.features.nl2sql.ontology_unified_model import merge_definitions
from app.features.nl2sql.structured_outputs import response_format, validate_json_output
from app.settings import get_settings


def all_concepts() -> list[dict[str, Any]]:
    definitions = definition_payload()
    definitions[2]["join_expression_sql"] = "APP.ORDERS.CUSTOMER_ID = APP.ORDERS.ID"
    return [
        *definitions,
        # 使われない AI の共有プロパティは外すため、列の対応を持たせる（#1734）。
        {
            "kind": "shared_property",
            "api_name": "Amount",
            "name_ja": "金額",
            "data_type": "number",
            "mappings": [{"owner": "APP", "object_name": "ORDERS", "column_name": "ID"}],
        },
        {"kind": "value_type", "api_name": "Money", "name_ja": "金額型", "data_type": "number"},
        {"kind": "enumeration", "api_name": "Status", "name_ja": "状態", "property": "Order.id"},
        {
            "kind": "metric",
            "api_name": "Total",
            "name_ja": "合計",
            "description_ja": "受注の合計。",
        },
        {
            "kind": "business_rule",
            "api_name": "Rule",
            "name_ja": "受注条件",
            "applies_to": ["Order"],
        },
        {
            "kind": "business_event",
            "api_name": "Created",
            "name_ja": "受注作成",
            "object_type": "Order",
        },
        {"kind": "object_set", "api_name": "Orders", "name_ja": "受注集合", "object_type": "Order"},
    ]


@pytest.mark.parametrize("source", ["schema", "text", "qa", "mixed"])
def test_unified_worker_builds_thirteen_kinds_once_per_input(
    source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_extraction_gleaning_passes", 0)
    rt, legacy = runtime()

    class Client(_FakeEnterpriseAiClient):
        def generate(
            self, *, prompt: str, context: str, system_prompt: str, response_format: Any = None
        ) -> str:
            self.calls.append(prompt)
            payload = json.loads(context)
            if payload.get("business_text_chunks"):
                chunk = payload["business_text_chunks"][0]
                evidence = {
                    "source_id": chunk["source_id"],
                    "locator": chunk["locator"],
                    "excerpt_ja": chunk["text"],
                }
            elif payload.get("qa_pairs"):
                pair = payload["qa_pairs"][0]
                evidence = {
                    "source_id": pair["source_id"],
                    "locator": pair["locator"],
                    "excerpt_ja": pair["question"],
                }
            else:
                evidence = {"source_id": "schema", "locator": "APP.ORDERS", "excerpt_ja": "ORDERS"}
            definitions = all_concepts()
            definitions[0]["evidence"] = [evidence, evidence]
            definitions[0]["aliases"] = ["注文", "注文"]
            return json.dumps({"definitions": definitions})

    client = Client("")
    legacy._enterprise_ai_client = client
    service = OntologyBuildService(rt)
    job = _wait_for_job(
        service,
        service.start(
            "sales",
            run_schema_naming=source in {"schema", "mixed"},
            business_text="受注の定義" if source in {"text", "mixed"} else "",
            qa_pairs=(
                [
                    QaPair(
                        question="受注",
                        sql="SELECT o.ID FROM APP.ORDERS o JOIN APP.ORDERS p ON o.CUSTOMER_ID=p.ID",
                    )
                ]
                if source in {"qa", "mixed"}
                else []
            ),
        ).id,
    )
    assert job.status == "succeeded", job.error_message_ja
    assert len(client.calls) == (3 if source == "mixed" else 1)
    assert [p.name for p in job.definition_phases] == [
        "freeze",
        "evidence",
        "concepts",
        "validation",
        "markdown",
        "save",
    ]
    bundle = ProfileOntologyDefinitionService(rt).get("sales", job.result_bundle_id)
    assert len(bundle.definitions) == 13
    assert {d.kind for d in bundle.definitions} == set(DEFINITION_KINDS)
    assert all(c.count == 1 for c in bundle.coverage)
    obj = next(d for d in bundle.definitions if isinstance(d, ObjectTypeDefinition))
    assert obj.properties == ["Order.id"] and obj.primary_key == ["Order.id"]
    assert (
        next(d for d in bundle.definitions if isinstance(d, PropertyDefinition)).object_type
        == obj.api_name
    )
    assert obj.aliases == ["注文"]
    assert len(obj.evidence) == (3 if source == "mixed" else 1)
    assert all(
        any(e.source_id == s.source_id and e.locator == s.locator for s in bundle.sources)
        for e in obj.evidence
    )
    assert all(
        e.phase == "evidence" for e in job.events if "スキーマ情報を準備しました" in e.message_ja
    )
    assert all(
        e.phase == "markdown"
        for e in job.events
        if "Markdown 下書きをレンダリングしています" in e.message_ja
    )
    assert all(e.phase == "concepts" for e in job.events if "抽出候補を検証" in e.message_ja)
    assert all(e.phase == "concepts" for e in job.events if "を整理しました" in e.message_ja)
    assert "候補 13 件" in job.events[-1].message_ja or "候補 13 件" in job.events[-2].message_ja


def test_gleaning_completes_existing_fields_keeps_conflicts_and_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_extraction_gleaning_passes", 1)
    rt, legacy = runtime()

    class Client(_FakeEnterpriseAiClient):
        def generate(
            self, *, prompt: str, context: str, system_prompt: str, response_format: Any = None
        ) -> str:
            self.calls.append(prompt)
            assert "title_property は任意" in system_prompt
            assert set(response_format["schema"]["properties"]) == {
                "definitions",
                "coverage",
                "warnings_ja",
            }
            if "【追加パス" in prompt:
                return json.dumps(
                    {
                        "definitions": [
                            {
                                "kind": "object_type",
                                "api_name": "Order",
                                "name_ja": "受注",
                                "grain_ja": "受注単位",
                                # 表の決まらないオブジェクト型は公開に含めない（#1729）。
                                "mappings": [{"owner": "APP", "object_name": "ORDERS"}],
                            },
                            {
                                "kind": "shared_property",
                                "api_name": "Amount",
                                "name_ja": "金額",
                                "data_type": "number",
                                "mappings": [
                                    {"owner": "APP", "object_name": "ORDERS", "column_name": "ID"}
                                ],
                            },
                        ],
                        "warnings_ja": ["受注金額の通貨に資料間の矛盾があります。"],
                    }
                )
            return json.dumps(
                {"definitions": [{"kind": "object_type", "api_name": "Order", "name_ja": "受注"}]}
            )

    client = Client("")
    legacy._enterprise_ai_client = client
    service = OntologyBuildService(rt)
    job = _wait_for_job(service, service.start("sales").id)
    bundle = ProfileOntologyDefinitionService(rt).get("sales", job.result_bundle_id)
    assert len(client.calls) == 2
    assert len(bundle.definitions) == 2
    assert next(d for d in bundle.definitions if d.kind == "object_type").grain_ja == "受注単位"
    assert "受注金額の通貨に資料間の矛盾があります。" in job.warnings_ja

    base = OntologyBuildExtraction(
        definitions=[ObjectTypeDefinition(api_name="Order", name_ja="受注", grain_ja="受注単位")]
    )
    repeated, count = merge_build_extractions(base, base)
    assert len(repeated.definitions) == 1 and count == 0
    changed = base.model_copy(deep=True)
    assert isinstance(changed.definitions[0], ObjectTypeDefinition)
    changed.definitions[0].grain_ja = "明細単位"
    combined, _ = merge_build_extractions(base, changed)
    merged, conflicts = merge_definitions("sales", combined.definitions)
    assert len(merged) == 1 and conflicts


@pytest.mark.parametrize(
    "title,owner,code",
    [
        ("", "Order", None),
        ("Order.label", "Order", None),
        ("missing", "Order", "REFERENCE_INVALID"),
        ("Order.label", "Other", "PROPERTY_OWNER_MISMATCH"),
        ("Order", "Order", "REFERENCE_INVALID"),
    ],
)
def test_optional_title_property_checks_reference_and_owner(
    title: str, owner: str, code: str | None
) -> None:
    obj = ObjectTypeDefinition(id="order", api_name="Order", name_ja="受注", title_property=title)
    prop = PropertyDefinition(
        id="label", api_name="Order.label", name_ja="表示名", data_type="string", object_type=owner
    )
    bundle = ProfileOntologyBundle(
        id="b",
        job_id="job",
        profile_id="sales",
        schema_fingerprint="s",
        profile_fingerprint="p",
        definitions=[obj, prop],
    )
    findings = validate_definitions(bundle, {})
    title_findings = [f for f in findings if f.field == "title_property"]
    if code:
        assert any(f.code == code for f in title_findings), title_findings
    else:
        assert not title_findings
    assert {"OBJECT_IDENTITY_REQUIRED", "OBJECT_MAPPING_REQUIRED"} <= {f.code for f in findings}
    wire = response_format(OntologyConceptExtraction)
    output = OntologyConceptExtraction(definitions=[obj, prop]).model_dump_json()
    validate_json_output(output, wire)
    assert (
        "任意"
        in wire["schema"]["$defs"]["ObjectTypeDefinition"]["properties"]["title_property"][
            "description"
        ]
    )
