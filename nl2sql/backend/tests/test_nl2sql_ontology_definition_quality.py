"""根拠・再構築・batch 復旧の回帰テスト。実 OCI/Oracle は使用しない。"""

from __future__ import annotations

import json

import pytest
from test_nl2sql_ontology_build import _FakeEnterpriseAiClient, _wait_for_job
from test_nl2sql_ontology_definitions import definition_payload, runtime

from app.features.nl2sql.ontology_build import OntologyBuildService, _OntologyBuildLlmTask
from app.features.nl2sql.ontology_definition_quality import inspect_definition_quality
from app.features.nl2sql.ontology_definition_service import (
    ProfileOntologyDefinitionService,
    definition_fingerprint,
)
from app.features.nl2sql.ontology_definitions import (
    DefinitionEvidence,
    DefinitionSource,
    MetricDefinitionV2,
    ObjectTypeDefinition,
)
from app.features.nl2sql.ontology_models import OntologyBuildStepName
from app.settings import get_settings


def test_evidence_must_match_profile_source_location_hash_and_verbatim_excerpt() -> None:
    source = DefinitionSource(
        source_id="manual",
        locator="line:1",
        kind="manual",
        sha256="a" * 64,
        text="契約には更新期限がある。",
    )
    item = ObjectTypeDefinition(
        api_name="Contract",
        name_ja="契約",
        evidence=[
            DefinitionEvidence(source_id="manual", locator="line:1", excerpt_ja="更新期限がある")
        ],
    )
    assert not any(
        f.code == "INFERENCE_WITHOUT_EVIDENCE" for f in inspect_definition_quality([item], [source])
    )
    assert item.evidence[0].verified
    item.evidence[0].locator = "line:2"
    unresolved = [
        f
        for f in inspect_definition_quality([item], [source])
        if f.code == "INFERENCE_WITHOUT_EVIDENCE"
    ]
    # 照合できない根拠は定義ごとに 1 件の info（公開を止めない。#1720）。
    assert [(f.severity, f.definition_id) for f in unresolved] == [("info", "Contract")]
    assert not item.evidence[0].verified
    item.evidence[0].locator = "line:1"
    item.evidence[0].source_sha256 = "b" * 64
    inspect_definition_quality([item], [source])
    assert not item.evidence[0].verified


@pytest.mark.parametrize(
    "locator,verified",
    [
        ("ADMIN.V_DEPT_PROJECT", True),
        ("ADMIN.V_DEPT_PROJECT.PROJECT_NAME, ADMIN.V_DEPT_PROJECT.DEPARTMENT_NAME", True),
        ("ADMIN.V_DEPT_PROJECT.*", True),
        ("ADMIN.V_DEPT_PROJECT.MISSING", False),
        ("ADMIN.OUTSIDE", False),
        ("line:1", False),
    ],
    ids=["object", "columns", "wildcard", "missing-column", "outside", "not-physical"],
)
def test_schema_evidence_is_verified_when_the_physical_names_are_in_the_profile(
    locator: str, verified: bool
) -> None:
    """DB の schema を根拠にした evidence は、資料の検証値が無くても物理名で照合する（#1720）。"""

    item = ObjectTypeDefinition(
        api_name="v_dept_project",
        name_ja="部門別プロジェクト",
        evidence=[
            DefinitionEvidence(
                source_id="schema",
                locator=locator,
                source_kind="database",
                source_sha256="N/A",
                excerpt_ja="SQL Assist sample view",
            )
        ],
    )
    physical = {"ADMIN.V_DEPT_PROJECT": {"PROJECT_NAME", "DEPARTMENT_NAME"}}
    findings = inspect_definition_quality([item], [], physical)
    assert item.evidence[0].verified is verified
    assert any(f.code == "INFERENCE_WITHOUT_EVIDENCE" for f in findings) is not verified


def test_missing_identity_and_types_do_not_block_publication() -> None:
    """識別子・粒度・型など graph を作れる項目の不足は info（公開を止めない。#1720）。"""

    from app.features.nl2sql.ontology_definitions import PropertyDefinition

    findings = inspect_definition_quality(
        [
            ObjectTypeDefinition(api_name="v_emp_dept", name_ja="従業員部門ビュー"),
            PropertyDefinition(api_name="name", name_ja="名前", object_type="v_emp_dept"),
        ],
        [],
    )
    assert {f.code for f in findings} >= {
        "OBJECT_IDENTITY_REQUIRED",
        "OBJECT_GRAIN_REQUIRED",
        "PROPERTY_TYPE_REQUIRED",
    }
    assert all(f.severity == "info" for f in findings)
    assert all(f.definition_id for f in findings)


def test_metric_incomplete_semantics_are_reported_instead_of_assumed() -> None:
    item = MetricDefinitionV2(api_name="sales", name_ja="売上", expression_sql="SUM(amount)")
    fields = {f.field for f in inspect_definition_quality([item], [])}
    assert {
        "grain",
        "aggregation",
        "unit",
        "null_policy_ja",
        "time_policy_ja",
        "additivity",
    } <= fields


def test_rebuild_keeps_reviewed_identity_and_manual_edits_even_when_unchanged() -> None:
    rt, _ = runtime()
    service = ProfileOntologyDefinitionService(rt)
    first = service.save_build(
        profile_id="sales",
        job_id="first",
        definitions=[ObjectTypeDefinition(api_name="Contract", name_ja="契約")],
        schema_fingerprint="schema",
        source_revision_id="legacy",
    )
    first.definitions[0].origin = "manual"
    first.definitions[0].review_status = "reviewed"
    first.review_records = [
        {
            "definition_id": first.definitions[0].id,
            "hash": definition_fingerprint(first.definitions[0].model_dump(mode="json")),
            "actor": "reviewer",
        }
    ]
    record = rt.store.get_artifact(first.id)
    assert record is not None
    record["content"] = first.model_dump_json()
    rt.store.save_artifact(record, expected_etag=record["etag"])
    second = service.save_build(
        profile_id="sales",
        job_id="second",
        definitions=[ObjectTypeDefinition(api_name="Contract", name_ja="契約")],
        schema_fingerprint="schema",
        source_revision_id="legacy",
    )
    assert second.definitions[0].origin == "manual"
    assert second.definitions[0].review_status == "reviewed"
    assert second.review_records == first.review_records
    assert not second.conflicts
    third = service.save_build(
        profile_id="sales",
        job_id="third",
        definitions=[
            ObjectTypeDefinition(api_name="Agreement", aliases=["Contract"], name_ja="合意")
        ],
        schema_fingerprint="schema",
        source_revision_id="legacy",
    )
    assert third.definitions[0].id == first.definitions[0].id
    assert third.definitions[0].name_ja == "契約"
    assert len(third.conflicts) == 1


def test_staged_worker_and_checkpoint_reuse_after_service_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rt, legacy = runtime()
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(
        json.dumps({"definitions": definition_payload()})
    )
    service = OntologyBuildService(rt)
    job = _wait_for_job(service, service.start("sales", business_text="受注を承認する。").id)
    assert [p.name for p in job.definition_phases] == [
        "freeze",
        "evidence",
        "concepts",
        "validation",
        "markdown",
        "save",
    ]
    assert all(phase.status == "succeeded" for phase in job.definition_phases)
    task = _OntologyBuildLlmTask(
        name=OntologyBuildStepName.TEXT_EXTRACTION,
        prompt="検査",
        context="固定入力",
        progress_ja="検査",
    )
    # 完了済み実行には追記せず、所有権を持つ実行の checkpoint を明示 retry へ継承する。
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    checkpoint_job = service.start("sales", business_text="検査用入力")
    assert service._claim_execution(checkpoint_job.id)
    results, warnings = service._execute_llm_task(
        checkpoint_job.id, legacy._enterprise_ai_client, task, label="検査"
    )
    service.cancel(checkpoint_job.id)
    assert results and not warnings
    restarted = OntologyBuildService(rt)

    def forbidden(*args: object, **kwargs: object) -> str:
        raise AssertionError("saved batch must not invoke LLM")

    monkeypatch.setattr(restarted, "_generate_extraction", forbidden)
    retried = restarted.retry(checkpoint_job.id)
    assert restarted._claim_execution(retried.id)
    restored, warnings = restarted._execute_llm_task(
        retried.id, legacy._enterprise_ai_client, task, label="検査"
    )
    assert restored[0].extraction == results[0].extraction
    assert not warnings
    bundle = ProfileOntologyDefinitionService(rt).get("sales", job.result_bundle_id)
    assert any(source.kind == "manual" for source in bundle.sources)
