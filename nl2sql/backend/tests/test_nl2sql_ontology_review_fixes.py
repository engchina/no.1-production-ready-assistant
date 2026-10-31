"""Issue #467: 公開ゲート・SQL 意味・型変換・変更解析・応答回復の回帰。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from test_nl2sql_ontology_build import _FakeEnterpriseAiClient
from test_nl2sql_ontology_capabilities import binding, ready, request
from test_nl2sql_ontology_definitions import runtime
from test_nl2sql_ontology_workspace import model, no_auth  # noqa: F401

from app.features.nl2sql.models import QueryResults
from app.features.nl2sql.ontology_definition_data_validation import check_data
from app.features.nl2sql.ontology_definition_validation import validate_definitions
from app.features.nl2sql.ontology_definition_workspace import ProfileOntologyWorkspaceService
from app.features.nl2sql.ontology_definitions import (
    DefinitionDataValidationRequest,
    DefinitionEvidence,
    DefinitionSource,
    MetricDefinitionV2,
    ProfileOntologyBundle,
)
from app.features.nl2sql.ontology_published_context import _expressions_visible
from app.features.nl2sql.ontology_service import OntologyGateBlockedError, OntologyNotFoundError


def saved(
    svc: ProfileOntologyWorkspaceService, definitions: Any = None, job: str = "first", **kwargs: Any
) -> ProfileOntologyBundle:
    return svc.save_build(
        profile_id="sales",
        job_id=job,
        definitions=definitions or model(),
        schema_fingerprint="schema",
        source_revision_id="legacy",
        **kwargs,
    )


def test_duplicate_id_rejected_without_changing_draft_and_static_publish_gate() -> None:
    rt, _ = runtime()
    svc = ProfileOntologyWorkspaceService(rt)
    b = saved(svc)
    duplicate = b.definitions[0].model_copy(update={"api_name": "Other"})
    with pytest.raises(OntologyGateBlockedError) as exc:
        svc.edit("sales", b.id, b.etag, [*b.definitions, duplicate], None)
    assert exc.value.code == "DUPLICATE_DEFINITION_ID"
    assert svc.get("sales", b.id) == b
    b.definitions.append(duplicate)
    assert "DUPLICATE_DEFINITION_ID" in {f.code for f in validate_definitions(b, {})}


def test_identical_rebuild_preserves_review_proof_and_can_publish() -> None:
    rt, _ = runtime()
    svc = ProfileOntologyWorkspaceService(rt)
    b = saved(svc)
    b = svc.review("sales", b.id, b.etag, [d.id for d in b.definitions], None)
    rebuilt = saved(svc, job="second")
    assert rebuilt.review_records == b.review_records
    assert all(d.review_status == "reviewed" for d in rebuilt.definitions)
    rebuilt = svc.validate("sales", rebuilt.id, rebuilt.etag, None)
    assert svc.publish("sales", rebuilt.id, rebuilt.etag, "", "publish", None)["id"]


def test_changed_evidence_requires_new_review() -> None:
    rt, _ = runtime()
    svc = ProfileOntologyWorkspaceService(rt)
    definitions = model()
    definitions[0].evidence = [
        DefinitionEvidence(source_id="doc", locator="line:1", excerpt_ja="受注")
    ]
    source = DefinitionSource(
        source_id="doc", locator="line:1", kind="manual", sha256="a" * 64, text="受注"
    )
    b = saved(svc, definitions, sources=[source])
    b = svc.review("sales", b.id, b.etag, [d.id for d in b.definitions], None)
    source.sha256 = "b" * 64
    rebuilt = saved(svc, definitions, "second", sources=[source])
    assert rebuilt.definitions[0].review_status == "unreviewed"
    assert not any(r["definition_id"] == rebuilt.definitions[0].id for r in rebuilt.review_records)


@pytest.mark.parametrize(
    "value,data_type,valid",
    [
        (1.5, "number", True),
        (1, "number", True),
        ("1.5", "number", False),
        ("2026-09-11", "date", True),
        ("2026-09-11T00:00:00", "date", True),
        ("2026-09-11T12:30:00", "datetime", True),
        ("invalid", "datetime", False),
        ("2026-02-30", "date", False),
        (True, "number", False),
    ],
)
def test_shacl_sample_types_match_oracle_json_without_hiding_invalid_values(
    value: Any, data_type: str, valid: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    rt, legacy = runtime()
    svc = ProfileOntologyWorkspaceService(rt)
    definitions = model()
    definitions[1].data_type = data_type
    b = saved(svc, definitions)
    prop = b.definitions[1]
    monkeypatch.setattr(
        legacy,
        "_oracle_adapter",
        SimpleNamespace(
            execute_select=lambda *_: QueryResults(
                columns=[prop.id], rows=[{prop.id: value}], total=1
            )
        ),
        raising=False,
    )
    report = check_data(rt, b, DefinitionDataValidationRequest(confirmed=True))
    assert report["instance_count"] == 1
    assert report["shacl_conforms"] is valid


@pytest.mark.parametrize(
    "sql,valid",
    [
        ("SELECT c.AMOUNT FROM APP.CUSTOMERS c", False),
        ("SELECT c.ID FROM APP.CUSTOMERS c", True),
        ("SELECT ID FROM APP.CUSTOMERS c JOIN APP.ORDERS o ON c.ID=o.CUSTOMER_ID", False),
        ("WITH c AS (SELECT ID FROM APP.CUSTOMERS) SELECT c.ID FROM c", True),
        ("WITH c AS (SELECT ID FROM APP.CUSTOMERS) SELECT c.AMOUNT FROM c", False),
        ("SELECT (SELECT c.AMOUNT FROM APP.CUSTOMERS c) FROM APP.ORDERS o", False),
        ("SELECT AMOUNT FROM OTHER.PRIVATE", False),
    ],
)
def test_mapping_sql_is_validated_with_alias_cte_and_column_scope(sql: str, valid: bool) -> None:
    rt, _ = runtime()
    svc = ProfileOntologyWorkspaceService(rt)
    definitions = model()
    definitions[1].mappings[0].expression_sql = sql
    b = saved(svc, definitions)
    schema = json.loads(rt.prepare_build_schema_context("sales").schema_context)
    errors = [f for f in validate_definitions(b, schema) if f.severity == "error"]
    assert (not errors) is valid, errors
    assert (
        _expressions_visible(
            b.definitions[1],
            {
                "APP.CUSTOMERS": {"ID", "NAME"},
                "APP.ORDERS": {"ID", "CUSTOMER_ID", "AMOUNT"},
            },
        )
        is valid
    )


@pytest.mark.parametrize(
    "group,distinct,expected",
    [
        ("o.ID", "o.ID", set()),
        ("o.CUSTOMER_ID", "o.ID", {"METRIC_GRAIN_MISMATCH"}),
        ("o.ID", "o.CUSTOMER_ID", {"METRIC_DISTINCT_MISMATCH"}),
    ],
)
def test_metric_keys_are_compared_to_mapped_physical_columns(
    group: str, distinct: str, expected: set[str]
) -> None:
    rt, _ = runtime()
    svc = ProfileOntologyWorkspaceService(rt)
    definitions = model()
    definitions.append(
        MetricDefinitionV2(
            api_name="Total",
            name_ja="集計",
            expression_sql=(
                f"SELECT {group}, COUNT(DISTINCT {distinct}) FROM APP.ORDERS o GROUP BY {group}"
            ),
            aggregation="count",
            grain=["Order.id"],
            distinct_keys=["Order.id"],
            unit="件",
            time_policy_ja="全期間",
            null_policy_ja="除外",
            additivity="non_additive",
        )
    )
    b = saved(svc, definitions)
    schema = json.loads(rt.prepare_build_schema_context("sales").schema_context)
    errors = {f.code for f in validate_definitions(b, schema) if f.severity == "error"}
    assert errors == expected


def test_analyze_deletion_is_explicit_reviewable_and_empty_output_preserves_model() -> None:
    rt, legacy = runtime()
    svc = ProfileOntologyWorkspaceService(rt)
    b = saved(svc)
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(json.dumps({"definitions": []}))
    empty = svc.analyze("sales", b.id, b.etag, "確認", "empty", None)
    assert len(empty["after"]) == 2
    removed = b.definitions[1].id
    updated = b.definitions[0].model_copy(update={"properties": [], "primary_key": []})
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(
        json.dumps(
            {"deleted_definition_ids": [removed], "definitions": [updated.model_dump(mode="json")]}
        )
    )
    diff = svc.analyze("sales", b.id, b.etag, "ID 属性を削除", "delete", None)
    assert len(diff["before"]) == 2 and len(diff["after"]) == 1
    assert diff["deleted_definition_ids"] == [removed]
    assert svc.get("sales", b.id) == b
    candidate = ProfileOntologyBundle.model_validate(
        {**b.model_dump(mode="json"), "definitions": diff["after"]}
    )
    applied = svc.edit("sales", b.id, b.etag, candidate.definitions, None)
    assert all(d.id != removed for d in applied.definitions)
    assert applied.definitions[0].review_status == "unreviewed"


@pytest.mark.parametrize("mode", ["foreign", "conflicting"])
def test_analyze_rejects_foreign_or_conflicting_deletions(mode: str) -> None:
    rt, legacy = runtime()
    svc = ProfileOntologyWorkspaceService(rt)
    b = saved(svc)
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(
        json.dumps(
            {
                "deleted_definition_ids": ["foreign" if mode == "foreign" else b.definitions[0].id],
                "definitions": [b.definitions[0].model_dump(mode="json")],
            }
        )
    )
    with pytest.raises(OntologyGateBlockedError):
        svc.analyze("sales", b.id, b.etag, "削除", "delete", None)
    assert svc.get("sales", b.id) == b


def test_action_outcome_lookup_is_read_only_and_recovers_original_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, adapter, release, ids = ready(monkeypatch)
    svc.bind("sales", ids["approve"], binding(release), "*", None)
    preview = svc.preview("sales", ids["approve"], request(release), "preview", None)
    assert svc.outcome("sales", ids["approve"], preview["id"], None) == {"status": "unresolved"}
    assert adapter.commits == 0
    result = svc.execute("sales", ids["approve"], preview["id"], "execute", None)
    assert svc.outcome("sales", ids["approve"], preview["id"], None) == {
        "status": "succeeded",
        "execution": result,
    }
    assert adapter.commits == 1
    with pytest.raises(OntologyGateBlockedError):
        svc.outcome("sales", "other-action", preview["id"], None)
    with pytest.raises(OntologyNotFoundError):
        svc.outcome("support", ids["approve"], preview["id"], None)


@pytest.mark.parametrize(
    "clause,valid",
    [
        ("WHERE o.AMOUNT > 0", True),
        ("WHERE o.AMOUNT > 0 AND o.ID > 0", True),
        ("WHERE o.AMOUNT > 0 OR o.ID > 0", False),
        ("", False),
    ],
)
def test_metric_filter_uses_alias_resolved_mandatory_predicate(clause: str, valid: bool) -> None:
    rt, _ = runtime()
    svc = ProfileOntologyWorkspaceService(rt)
    definitions = model()
    definitions.append(
        MetricDefinitionV2(
            api_name="Sales",
            name_ja="売上",
            expression_sql=f"SELECT o.ID, SUM(o.AMOUNT) FROM APP.ORDERS o {clause} GROUP BY o.ID",
            filter_sql="APP.ORDERS.AMOUNT > 0",
            aggregation="sum",
            grain=["Order.id"],
            unit="円",
            time_policy_ja="全期間",
            null_policy_ja="除外",
            additivity="additive",
        )
    )
    b = saved(svc, definitions)
    errors = [
        f
        for f in validate_definitions(
            b, json.loads(rt.prepare_build_schema_context("sales").schema_context)
        )
        if f.severity == "error"
    ]
    assert (not errors) is valid, errors


@pytest.mark.asyncio
async def test_outcome_api_enforces_profile_and_definition_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.features.nl2sql.ontology_capability_router import create_capability_router
    from app.features.nl2sql.ontology_router import _raise_domain_error

    svc, adapter, release, ids = ready(monkeypatch)
    svc.bind("sales", ids["approve"], binding(release), "*", None)
    preview = svc.preview("sales", ids["approve"], request(release), "preview", None)
    app = FastAPI()
    app.include_router(create_capability_router(lambda: svc.runtime, _raise_domain_error))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        url = (
            f"/profiles/sales/ontology-capabilities/{ids['approve']}"
            f"/previews/{preview['id']}/outcome"
        )
        response = await client.get(url)
        assert response.status_code == 200
        assert response.json()["data"] == {"status": "unresolved"}
        assert (await client.get(url.replace("/sales/", "/support/"))).status_code == 404
        assert (await client.get(url.replace(ids["approve"], "foreign"))).status_code != 200
        assert adapter.commits == 0
