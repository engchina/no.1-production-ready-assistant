"""Profile の正本・review・公開 pointer の分離。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_nl2sql_ontology_definitions import runtime

from app.features.nl2sql.ontology_definition_artifacts import render_definition_artifacts
from app.features.nl2sql.ontology_definition_validation import (
    checked_expression,
    validate_definitions,
)
from app.features.nl2sql.ontology_definition_workspace import ProfileOntologyWorkspaceService
from app.features.nl2sql.ontology_definitions import (
    DefinitionMapping,
    InterfaceDefinition,
    InterfaceImplementation,
    MetricDefinitionV2,
    ObjectTypeDefinition,
    PropertyDefinition,
    TypedParameter,
)
from app.features.nl2sql.ontology_service import (
    OntologyGateBlockedError,
    OntologyNotFoundError,
    OntologyVersionConflictError,
)
from app.settings import get_settings


@pytest.fixture(autouse=True)
def no_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "auth_mode", "local")


def model() -> list[Any]:
    return [
        ObjectTypeDefinition(
            api_name="Order",
            name_ja="受注",
            properties=["Order.id"],
            primary_key=["Order.id"],
            grain_ja="受注",
            mappings=[DefinitionMapping(owner="APP", object_name="ORDERS")],
        ),
        PropertyDefinition(
            api_name="Order.id",
            name_ja="受注番号",
            object_type="Order",
            data_type="integer",
            required=True,
            mappings=[DefinitionMapping(owner="APP", object_name="ORDERS", column_name="ID")],
        ),
    ]


def test_review_validate_publish_are_distinct_and_release_is_immutable() -> None:
    rt, _ = runtime()
    svc = ProfileOntologyWorkspaceService(rt)
    bundle = svc.save_build(
        profile_id="sales",
        job_id="one",
        definitions=model(),
        schema_fingerprint="schema",
        source_revision_id="legacy",
    )
    with pytest.raises(OntologyGateBlockedError):
        svc.publish("sales", bundle.id, bundle.etag, "", "before-review", None)
    bundle = svc.validate("sales", bundle.id, bundle.etag, None)
    assert not [f for f in bundle.findings if f.severity == "error"]
    assert bundle.validation_report["instance_count"] == 0
    with pytest.raises(OntologyGateBlockedError):
        svc.publish("sales", bundle.id, bundle.etag, "", "unreviewed", None)
    bundle = svc.review("sales", bundle.id, bundle.etag, [d.id for d in bundle.definitions], None)
    bundle = svc.validate("sales", bundle.id, bundle.etag, None)
    release = svc.publish("sales", bundle.id, bundle.etag, "", "publish", None)
    assert svc.head("sales")["release_id"] == release["id"]
    assert svc.publish("sales", bundle.id, bundle.etag, "", "publish", None) == release
    assert svc.release("sales") == release
    saved = svc.get("sales", bundle.id)
    assert saved.status == "published"
    assert release["bundle"]["etag"] == saved.etag
    assert release["artifacts"] == render_definition_artifacts(saved)
    with pytest.raises(OntologyVersionConflictError):
        svc.edit("sales", saved.id, saved.etag, model(), None)


def test_two_profiles_publish_independently_and_stale_scope_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rt, legacy = runtime()
    profiles = {p: legacy.profile.model_copy(update={"id": p}) for p in ("sales", "support")}
    monkeypatch.setattr(rt, "ensure_profile", lambda p: profiles[p])
    monkeypatch.setattr(rt, "_strict_profile", lambda p: profiles[p])
    svc = ProfileOntologyWorkspaceService(rt)
    releases = []
    for profile_id in profiles:
        definitions = model()
        definitions[0].name_ja = profile_id
        b = svc.save_build(
            profile_id=profile_id,
            job_id="one",
            definitions=definitions,
            schema_fingerprint="schema",
            source_revision_id="legacy",
        )
        b = svc.review(profile_id, b.id, b.etag, [d.id for d in b.definitions], None)
        b = svc.validate(profile_id, b.id, b.etag, None)
        releases.append(svc.publish(profile_id, b.id, b.etag, "", "same-key", None))
    assert svc.release("sales") == releases[0]
    assert svc.release("support") == releases[1]
    with pytest.raises(OntologyNotFoundError):
        svc.release("support", releases[0]["id"])
    b = svc.save_build(
        profile_id="sales",
        job_id="two",
        definitions=model(),
        schema_fingerprint="schema",
        source_revision_id="legacy",
    )
    b = svc.review("sales", b.id, b.etag, [d.id for d in b.definitions], None)
    b = svc.validate("sales", b.id, b.etag, None)
    profiles["sales"] = profiles["sales"].model_copy(update={"name": "変更"})
    with pytest.raises(OntologyGateBlockedError, match="再検証"):
        svc.publish("sales", b.id, b.etag, releases[0]["id"], "stale", None)
    assert svc.release("support") == releases[1]


def test_etag_and_notes_preserve_definitions_and_reports() -> None:
    rt, _ = runtime()
    svc = ProfileOntologyWorkspaceService(rt)
    b = svc.save_build(
        profile_id="sales",
        job_id="one",
        definitions=model(),
        schema_fingerprint="schema",
        source_revision_id="legacy",
    )
    updated = svc.notes("sales", b.id, b.etag, "担当者へのメモ", None)
    assert b.definitions == updated.definitions
    assert b.findings == updated.findings
    with pytest.raises(OntologyVersionConflictError):
        svc.edit("sales", b.id, b.etag, model(), None)


def test_ast_filter_and_interface_contract_detect_semantic_mismatch() -> None:
    rt, _ = runtime()
    svc = ProfileOntologyWorkspaceService(rt)
    definitions = model()
    definitions[0].implements = [InterfaceImplementation(interface="Identified")]
    definitions.append(
        InterfaceDefinition(
            api_name="Identified",
            name_ja="識別可能",
            properties=[TypedParameter(api_name="id", name_ja="番号", data_type="integer")],
        )
    )
    definitions.append(
        MetricDefinitionV2(
            api_name="ConfirmedSales",
            name_ja="確定売上",
            expression_sql="SUM(APP.ORDERS.AMOUNT)",
            filter_sql="APP.ORDERS.STATUS = 'CONFIRMED'",
            aggregation="sum",
            grain=["Order.id"],
            unit="円",
            time_policy_ja="受注日",
            null_policy_ja="0",
            additivity="additive",
        )
    )
    b = svc.save_build(
        profile_id="sales",
        job_id="one",
        definitions=definitions,
        schema_fingerprint="schema",
        source_revision_id="legacy",
    )
    findings = validate_definitions(
        b, json.loads(rt.prepare_build_schema_context("sales").schema_context)
    )
    assert {"INTERFACE_PROPERTY_MISMATCH", "METRIC_FILTER_MISMATCH"} <= {f.code for f in findings}
    for unsafe in (
        "DELETE FROM APP.ORDERS",
        "SELECT * FROM APP.ORDERS FOR UPDATE",
        "SELECT UTL_HTTP.REQUEST('https://example.invalid') FROM DUAL",
        "SELECT 1 FROM DUAL; SELECT 2 FROM DUAL",
    ):
        with pytest.raises(ValueError):
            checked_expression(unsafe, query=True)


def test_publish_transaction_failure_preserves_draft_and_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rt, _ = runtime()
    svc = ProfileOntologyWorkspaceService(rt)
    b = svc.save_build(
        profile_id="sales",
        job_id="one",
        definitions=model(),
        schema_fingerprint="schema",
        source_revision_id="legacy",
    )
    b = svc.review("sales", b.id, b.etag, [d.id for d in b.definitions], None)
    b = svc.validate("sales", b.id, b.etag, None)

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("transaction unavailable")

    monkeypatch.setattr(rt.store, "save_documents_atomic", fail)
    with pytest.raises(RuntimeError):
        svc.publish("sales", b.id, b.etag, "", "publish", None)
    assert svc.get("sales", b.id).status == "draft"
    assert svc.head("sales")["release_id"] == ""


@pytest.mark.asyncio
async def test_workspace_api_etag_body_and_profile_ownership() -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.features.nl2sql.ontology_router import _raise_domain_error
    from app.features.nl2sql.ontology_workspace_router import create_workspace_router

    rt, _ = runtime()
    svc = ProfileOntologyWorkspaceService(rt)
    b = svc.save_build(
        profile_id="sales",
        job_id="one",
        definitions=model(),
        schema_fingerprint="schema",
        source_revision_id="legacy",
    )
    app = FastAPI()
    app.include_router(create_workspace_router(lambda: rt, _raise_domain_error))
    # Resolving the nested request schemas must work in OpenAPI as well as requests.
    assert app.openapi()["paths"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        path = f"/profiles/sales/ontology-results/{b.id}"
        response = await client.get(path + "/workspace")
        assert response.status_code == 200
        assert response.json()["data"]["bundle"]["profile_id"] == "sales"
        assert (
            await client.post(path + "/review", json={"definition_ids": [], "confirmed": True})
        ).status_code == 422
        assert (
            await client.post(
                path + "/review",
                json={"definition_ids": [], "confirmed": False},
                headers={"If-Match": b.etag},
            )
        ).status_code == 422
        response = await client.post(
            path + "/notes", json={"notes_ja": "メモ"}, headers={"If-Match": b.etag}
        )
        assert response.status_code == 410
        assert svc.get("sales", b.id).etag == b.etag
        assert (
            await client.post(
                path + "/notes", json={"notes_ja": "古い版"}, headers={"If-Match": b.etag}
            )
        ).status_code == 410


def test_data_validation_job_reports_actual_sample_and_missing_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import re
    from types import SimpleNamespace

    from app.features.nl2sql.models import QueryResults
    from app.features.nl2sql.ontology_definition_data_validation import (
        read_validation_job,
        run_validation_job,
        start_validation_job,
    )
    from app.features.nl2sql.ontology_definitions import (
        DefinitionAcceptanceCase,
        DefinitionDataValidationRequest,
    )

    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    rt, legacy = runtime()
    svc = ProfileOntologyWorkspaceService(rt)
    b = svc.save_build(
        profile_id="sales",
        job_id="one",
        definitions=model(),
        schema_fingerprint="schema",
        source_revision_id="legacy",
    )
    b = svc.validate("sales", b.id, b.etag, None)
    request = DefinitionDataValidationRequest(
        confirmed=True,
        acceptance_cases=[
            DefinitionAcceptanceCase(
                question_ja="受注の識別子は何か", expected_concepts=["Order.id"]
            )
        ],
    )
    job = start_validation_job(rt, "sales", b.id, b.etag, request, "one", None)
    assert (
        start_validation_job(rt, "sales", b.id, b.etag, request, "one", None)["job_id"]
        == job["job_id"]
    )
    run_validation_job(rt, job["job_id"])
    assert read_validation_job(rt, "sales", job["job_id"])["status"] == "failed"

    def execute_select(sql: str, row_limit: int) -> QueryResults:
        names = re.findall(r'AS "([^"]+)"', sql)
        assert row_limit == 50
        return QueryResults(columns=names, rows=[{name: 1 for name in names}], total=1)

    monkeypatch.setattr(
        legacy, "_oracle_adapter", SimpleNamespace(execute_select=execute_select), raising=False
    )
    job = start_validation_job(rt, "sales", b.id, b.etag, request, "two", None)
    run_validation_job(rt, job["job_id"])
    result = read_validation_job(rt, "sales", job["job_id"])
    assert result["status"] == "succeeded", result
    assert result["report"]["instance_count"] == 1
    assert result["report"]["shacl_conforms"] is True
    assert result["report"]["entire_database_validated"] is False
    assert result["report"]["acceptance_cases"][0]["passed"] is True


def test_rollback_is_version_bound_and_idempotent() -> None:
    rt, _ = runtime()
    svc = ProfileOntologyWorkspaceService(rt)
    releases = []
    for job_id in ("one", "two"):
        b = svc.save_build(
            profile_id="sales",
            job_id=job_id,
            definitions=model(),
            schema_fingerprint="schema",
            source_revision_id="legacy",
        )
        b = svc.review("sales", b.id, b.etag, [d.id for d in b.definitions], None)
        b = svc.validate("sales", b.id, b.etag, None)
        releases.append(
            svc.publish("sales", b.id, b.etag, svc.head("sales")["release_id"], job_id, None)
        )
    head = svc.head("sales")
    with pytest.raises(OntologyVersionConflictError):
        svc.rollback(
            "sales",
            releases[0]["id"],
            head["release_id"],
            None,
            expected_etag="stale",
            key="invalid",
        )
    restored = svc.rollback(
        "sales",
        releases[0]["id"],
        head["release_id"],
        None,
        expected_etag=head["etag"],
        key="rollback",
    )
    assert restored == releases[0]
    assert (
        svc.rollback(
            "sales",
            releases[0]["id"],
            head["release_id"],
            None,
            expected_etag=head["etag"],
            key="rollback",
        )
        == restored
    )


def test_profile_manager_does_not_gain_sql_execute_permission() -> None:
    from fastapi import HTTPException
    from test_nl2sql_ontology_access import _principal

    from app.features.nl2sql.ontology_definition_workspace import authorize_definition_operation
    from app.security.permissions import SQL_EXECUTE_PERMISSION

    with pytest.raises(HTTPException) as exc:
        authorize_definition_operation(
            "sales", _principal({"sales"}, profile_manager=True), SQL_EXECUTE_PERMISSION
        )
    assert exc.value.status_code == 403
