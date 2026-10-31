"""13分類、自由編集の確認、原子的公開と同版 graph/context の回帰。"""

import json
from collections.abc import Awaitable, Callable
from typing import Any, NoReturn

import pytest
from fastapi import Request
from starlette.responses import Response
from test_nl2sql_ontology_definitions import runtime
from test_nl2sql_ontology_workspace import model

from app.features.nl2sql.ontology_definitions import BusinessDefinition
from app.features.nl2sql.ontology_markdown_workspace import (
    MarkdownConfirmRequest,
    MarkdownOntologyWorkspace,
)
from app.features.nl2sql.ontology_published_context import published_context
from app.features.nl2sql.ontology_router import OntologyApiRuntime, OntologyMarkdownDraftPatch
from app.features.nl2sql.ontology_service import OntologyNotFoundError, OntologyVersionConflictError
from app.features.nl2sql.ontology_unified_model import (
    CONCEPT_ORDER,
    DEFINITIONS,
    merge_definitions,
    project_graph,
    render_concepts,
)
from app.settings import get_settings


@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "auth_mode", "local")
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")


def all_concepts() -> list[BusinessDefinition]:
    base = [d.model_dump(mode="json") for d in model()]
    extra: list[dict[str, Any]] = [
        dict(
            kind="link_type",
            api_name="Related",
            source="Order",
            target="Order",
            cardinality="one_to_many",
        ),
        dict(kind="interface", api_name="Identified"),
        dict(
            kind="function", api_name="Calculate", return_type="number", dependencies=["Order.id"]
        ),
        dict(
            kind="action_type",
            api_name="Approve",
            object_type="Order",
            affected_properties=["Order.id"],
        ),
        # どのプロパティからも使われない AI の共有プロパティは下書きから外すため（#1734）、
        # 列の対応を持たせる。
        dict(
            kind="shared_property",
            api_name="SharedId",
            data_type="integer",
            mappings=[{"owner": "APP", "object_name": "ORDERS", "column_name": "ID"}],
        ),
        dict(kind="value_type", api_name="IdType", data_type="integer"),
        dict(
            kind="enumeration",
            api_name="Codes",
            property="Order.id",
            values=[{"code": "1", "label_ja": "有効"}],
        ),
        dict(
            kind="metric",
            api_name="Total",
            dependencies=["Order.id"],
            expression_sql="COUNT(APP.ORDERS.ID)",
            aggregation="count",
            grain=["Order.id"],
            unit="件",
            null_policy_ja="NULL を除外",
            time_policy_ja="全期間",
        ),
        dict(kind="business_rule", api_name="Valid", applies_to=["Order"]),
        dict(
            kind="business_event",
            api_name="Created",
            object_type="Order",
            timestamp_property="Order.id",
        ),
        dict(kind="object_set", api_name="Orders", object_type="Order"),
    ]
    return DEFINITIONS.validate_python(
        base + [dict(name_ja=e["api_name"], description_ja="業務定義", **e) for e in extra]
    )


def test_all_thirteen_types_share_identity_in_markdown_and_graph() -> None:
    rt, _ = runtime()
    definitions, conflicts = merge_definitions("sales", all_concepts())
    assert not conflicts
    assert tuple(d.kind for d in definitions) == CONCEPT_ORDER
    markdown = render_concepts(definitions)
    graph = project_graph(rt.profile_view("sales")[1], definitions)
    for d in definitions:
        assert d.api_name in markdown
        if d.kind == "link_type":
            assert (
                next(e for e in graph.edges if e.id == d.id).metadata["definition"]["kind"]
                == d.kind
            )
        else:
            node = next(n for n in graph.nodes if n.id == d.id)
            assert node.kind.value == d.kind
            assert node.metadata["definition"]["api_name"] == d.api_name
    assert not any(n.kind.value == "business_entity" for n in graph.nodes)


def test_identity_dedup_and_conflicts_do_not_merge_same_physical_object() -> None:
    first, prop = model()
    duplicate = first.model_copy(update={"aliases": ["注文"]})
    other = first.model_copy(update={"api_name": "Another", "name_ja": "別の業務概念"})
    values, conflicts = merge_definitions("sales", [first, prop, duplicate, other])
    assert len(values) == 3 and not conflicts
    assert next(v for v in values if v.api_name == "Order").aliases == ["注文"]
    changed = prop.model_copy(update={"data_type": "string"})
    _, conflicts = merge_definitions("sales", [prop, changed])
    # 競合は業務の名前で「何が問題か」と「どうすればよいか」を書く（#1729）。
    assert conflicts == [
        "『受注番号』の業務の型が 2 通り（整数（integer） と 文字列（string））作られました。"
        "整数（integer） を使います。違う場合は Markdown の『受注番号』の「業務の型」の行を"
        "直してください。"
    ]


def test_legacy_names_sharing_a_physical_table_remain_distinct_and_order_independent() -> None:
    from app.features.nl2sql.ontology_models import OntologyNodeKind
    from app.features.nl2sql.ontology_unified_model import legacy_definitions

    rt, _ = runtime()
    base = rt.profile_view("sales")[1]
    definitions, _ = merge_definitions("sales", model())
    graph = project_graph(base, definitions)
    obj = next(n for n in graph.nodes if n.kind.value == "object_type")
    legacy = obj.model_copy(
        update={
            "kind": OntologyNodeKind.BUSINESS_ENTITY,
            "metadata": {},
            "technical_name": "APP.ORDERS",
        }
    )
    other = legacy.model_copy(update={"id": "different-business-identity"})
    graphs = [
        graph.model_copy(update={"nodes": nodes, "edges": []})
        for nodes in ([legacy, other], [other, legacy])
    ]
    converted = [merge_definitions("sales", legacy_definitions(g))[0] for g in graphs]
    assert len(converted[0]) == 2
    assert {d.id: d.api_name for d in converted[0]} == {d.id: d.api_name for d in converted[1]}


def test_real_build_worker_renders_all_thirteen_once_before_saving() -> None:
    from test_nl2sql_ontology_build import _FakeEnterpriseAiClient, _wait_for_job

    from app.features.nl2sql.ontology_build import OntologyBuildService

    rt, legacy = runtime()
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(
        json.dumps({"definitions": [d.model_dump(mode="json") for d in all_concepts()]})
    )
    build = OntologyBuildService(rt)
    queued = build.start("sales", business_text="受注の業務定義")
    build.run_persisted(queued.id)
    job = _wait_for_job(build, queued.id)
    assert job.status == "succeeded", job.error_message_ja
    assert [c.kind for c in job.concept_coverage] == list(CONCEPT_ORDER)
    assert all(c.count == 1 for c in job.concept_coverage)
    assert job.definition_phases[-2].name == "markdown"
    assert job.definition_phases[-1].name == "save"
    assert job.definition_phases[-2].finished_at is not None
    assert job.definition_phases[-1].started_at is not None
    assert job.definition_phases[-2].finished_at <= job.definition_phases[-1].started_at
    for d in all_concepts():
        assert job.markdown_output.count(f"(`{d.api_name}`)") == 1


class Parser:
    def __init__(self, definitions: list[BusinessDefinition]) -> None:
        self.definitions = definitions
        self.calls = 0
        self.lines: list[dict[str, Any]] = [
            {
                "start_line": 1,
                "end_line": 1,
                "disposition": "definition",
                "definition_api_names": [d.api_name for d in definitions],
            }
        ]

    def is_configured(self) -> bool:
        return True

    def generate(self, **kwargs: Any) -> str:
        self.calls += 1
        return json.dumps(
            {
                "definitions": [d.model_dump(mode="json") for d in self.definitions],
                "coverage": [],
                "lines": self.lines,
            }
        )


def prepared_workspace() -> tuple[
    OntologyApiRuntime, MarkdownOntologyWorkspace, Parser, dict[str, Any]
]:
    rt, _ = runtime()
    base = rt.profile_view("sales")[1]
    rt.create_build_markdown_draft(
        profile_id="sales",
        base_revision_id=base.revision.id,
        payloads=[],
        titles=[],
        markdown="受注を受注番号で識別する。",
        note="確認",
    )
    parser = Parser(model())
    rt.legacy_service._enterprise_ai_client = parser
    svc = MarkdownOntologyWorkspace(rt)
    state = rt.ontology_markdown_state("sales")
    preparation = svc.prepare("sales", state.draft_etag, "prepare-one", None)
    svc.run_preparation("sales", preparation["id"])
    result = svc.preparation("sales", preparation["id"])
    return rt, svc, parser, result


def test_publish_uses_confirmed_snapshot_without_second_llm_and_keeps_history() -> None:
    rt, svc, parser, preparation = prepared_workspace()
    assert preparation["status"] == "ready", preparation
    req = MarkdownConfirmRequest(
        preparation_id=preparation["id"], draft_etag=preparation["draft_etag"], confirmed=True
    )
    job = svc.publish("sales", req, "publish-one", None)
    assert parser.calls == 1
    assert svc.publish("sales", req, "publish-one", None).id == job.id
    state = rt.ontology_markdown_state("sales")
    assert state.published_markdown == preparation["markdown"]
    view, graph = rt.profile_view("sales")
    assert graph.revision.id == job.revision_id
    assert any(n.kind.value == "object_type" for n in graph.nodes)
    # LLM には下書きの本文ではなく、公開した定義から作る業務の知識だけを渡す（#1720）。
    context = published_context(rt, "sales", job.revision_id)
    assert context.startswith("# 業務オントロジー\n") and "受注" in context
    assert preparation["markdown"] not in context
    for internal in ("verified", "source_id", "なし", "未設定", "下書き", "根拠"):
        assert internal not in context
    assert rt.profile_view("sales")[1].revision.id == job.revision_id  # repeated read
    assert rt.ontology_revision(job.revision_id).revision.id == job.revision_id
    with pytest.raises(OntologyNotFoundError):
        svc.snapshot("support", job.id)


def test_warning_only_publication_keeps_complete_diagnostics_without_revalidation() -> None:
    from app.features.nl2sql.ontology_markdown_workspace import PREPARATION

    rt, svc, parser, preparation = prepared_workspace()
    assert preparation["status"] == "ready"
    assert preparation["findings"]
    # 補えば精度が上がる項目は info、公開を止めない（#1720）。
    assert preparation["findings"]
    assert all(f["severity"] in {"warning", "info"} for f in preparation["findings"])
    # 読取り専用の標本検証レポートも公開版に固定する。
    preparation["data_report"] = {"errors": 0, "instance_count": 1, "sample_limit": 50}
    svc._write("sales", preparation["id"], PREPARATION, preparation)
    job = svc.publish(
        "sales",
        MarkdownConfirmRequest(
            preparation_id=preparation["id"], draft_etag=preparation["draft_etag"], confirmed=True
        ),
        "with-warnings",
        None,
    )
    expected = MarkdownOntologyWorkspace(rt).publication_diagnostics("sales", job.id)
    assert expected["available"] is True
    assert expected["findings"] == preparation["findings"]
    assert expected["data_report"] == preparation["data_report"]
    state = rt.ontology_markdown_state("sales")
    assert state.published_findings == preparation["findings"]
    assert state.published_data_report == preparation["data_report"]
    assert state.published_diagnostics_available
    assert parser.calls == 1
    with pytest.raises(OntologyNotFoundError):
        svc.publication_diagnostics("support", job.id)


def test_legacy_snapshot_recovers_saved_warning_diagnostics() -> None:
    from app.features.nl2sql.ontology_markdown_workspace import SNAPSHOT

    rt, svc, parser, preparation = prepared_workspace()
    job = svc.publish(
        "sales",
        MarkdownConfirmRequest(
            preparation_id=preparation["id"], draft_etag=preparation["draft_etag"], confirmed=True
        ),
        "legacy-diagnostics",
        None,
    )
    snapshot = svc.snapshot("sales", job.id)
    assert snapshot is not None
    snapshot.pop("findings")
    snapshot.pop("data_report")
    svc._write("sales", job.id, SNAPSHOT, snapshot)
    assert rt.ontology_markdown_state("sales").published_findings == preparation["findings"]
    assert parser.calls == 1
    snapshot.pop("preparation_id")
    svc._write("sales", job.id, SNAPSHOT, snapshot)
    assert not rt.ontology_markdown_state("sales").published_diagnostics_available


def test_sql_resolution_error_still_blocks_warning_publication() -> None:
    from app.features.nl2sql.ontology_definitions import DefinitionMapping

    rt, svc, parser, _ = prepared_workspace()
    parser.definitions[1] = parser.definitions[1].model_copy(
        update={
            "mappings": [
                DefinitionMapping(
                    owner="APP", object_name="ORDERS", expression_sql="APP.ORDERS.MISSING_COLUMN"
                )
            ]
        }
    )
    etag = rt.ontology_markdown_state("sales").draft_etag
    preparation = svc.prepare("sales", etag, "invalid-sql", None)
    svc.run_preparation("sales", preparation["id"])
    result = svc.preparation("sales", preparation["id"])
    assert result["status"] == "failed"
    assert any(f["severity"] == "error" for f in result["findings"])
    assert any(f["severity"] in {"warning", "info"} for f in result["findings"])
    with pytest.raises(OntologyVersionConflictError):
        svc.publish(
            "sales",
            MarkdownConfirmRequest(preparation_id=result["id"], draft_etag=etag, confirmed=True),
            "invalid-publication",
            None,
        )
    assert svc.snapshot("sales") is None


async def test_historical_diagnostics_api_enforces_profile_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from test_nl2sql_ontology_access import _principal

    from app.features.nl2sql.ontology_markdown_router import create_markdown_router
    from app.features.nl2sql.ontology_router import _raise_domain_error

    rt, svc, _, preparation = prepared_workspace()
    job = svc.publish(
        "sales",
        MarkdownConfirmRequest(
            preparation_id=preparation["id"], draft_etag=preparation["draft_etag"], confirmed=True
        ),
        "diagnostics-api",
        None,
    )
    monkeypatch.setattr(get_settings(), "auth_mode", "authenticated")
    app = FastAPI()

    @app.middleware("http")
    async def principal(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.principal = _principal({"sales"}, profile_manager=False)
        return await call_next(request)

    app.include_router(create_markdown_router(lambda: rt, _raise_domain_error))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/profiles/sales/ontology-markdown/publications/{job.id}/diagnostics"
        )
        assert response.status_code == 200
        assert response.json()["data"]["findings"] == preparation["findings"]
        forbidden = await client.get(
            f"/profiles/support/ontology-markdown/publications/{job.id}/diagnostics"
        )
        assert forbidden.status_code == 403


def test_historical_sql_context_keeps_original_markdown_after_new_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.features.nl2sql import ontology_router
    from app.features.nl2sql.models import JobCreateRequest
    from app.features.nl2sql.service import Nl2SqlService
    from app.features.nl2sql.store import MemoryNl2SqlStore

    rt, svc, _, first = prepared_workspace()
    old = svc.publish(
        "sales",
        MarkdownConfirmRequest(
            preparation_id=first["id"], draft_etag=first["draft_etag"], confirmed=True
        ),
        "original-context",
        None,
    )
    edited = rt.save_ontology_markdown_draft(
        "sales",
        OntologyMarkdownDraftPatch(
            markdown="新しい受注の公開定義",
            base_etag=rt.ontology_markdown_state("sales").draft_etag,
        ),
    )
    next_check = svc.prepare("sales", edited.draft_etag, "next-context", None)
    svc.run_preparation("sales", next_check["id"])
    latest = svc.publish(
        "sales",
        MarkdownConfirmRequest(
            preparation_id=next_check["id"],
            draft_etag=edited.draft_etag,
            expected_head=old.id,
            confirmed=True,
        ),
        "next-publication",
        None,
    )
    assert svc.publication_diagnostics("sales", old.id)["findings"] == first["findings"]
    assert svc.publication_diagnostics("sales", latest.id)["snapshot_id"] == latest.id
    monkeypatch.setattr(ontology_router, "ontology_runtime", rt)
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    request = JobCreateRequest(profile_id="sales", question="受注", use_ontology_context=True)
    profile = rt._strict_profile("sales")
    assert service._job_published_ontology_markdown(
        request=request, profile=profile, business_release_id=old.id
    ) == published_context(rt, "sales", old.id)
    assert service._job_published_ontology_markdown(
        request=request, profile=profile, business_release_id=latest.id
    ) == published_context(rt, "sales", latest.id)
    assert (
        service._job_published_ontology_markdown(
            request=request, profile=profile, business_release_id=""
        )
        is None
    )


def test_edit_invalidates_confirmation_and_parse_omission_blocks_publish() -> None:
    rt, svc, parser, preparation = prepared_workspace()
    rt.save_ontology_markdown_draft(
        "sales",
        OntologyMarkdownDraftPatch(markdown="新しい本文", base_etag=preparation["draft_etag"]),
    )
    with pytest.raises(OntologyVersionConflictError):
        svc.publish(
            "sales",
            MarkdownConfirmRequest(
                preparation_id=preparation["id"],
                draft_etag=preparation["draft_etag"],
                confirmed=True,
            ),
            "key",
            None,
        )
    assert svc.head("sales")["snapshot_id"] == ""
    state = rt.ontology_markdown_state("sales")
    parser.lines = []
    fresh = svc.prepare("sales", state.draft_etag, "second", None)
    svc.run_preparation("sales", fresh["id"])
    assert svc.preparation("sales", fresh["id"])["status"] == "failed"


def test_atomic_failure_leaves_previous_publication(monkeypatch: pytest.MonkeyPatch) -> None:
    rt, svc, _, preparation = prepared_workspace()
    req = MarkdownConfirmRequest(
        preparation_id=preparation["id"], draft_etag=preparation["draft_etag"], confirmed=True
    )

    def fail(*args: Any, **kwargs: Any) -> NoReturn:
        raise RuntimeError("storage failure")

    monkeypatch.setattr(svc.store, "save_documents_atomic", fail)
    with pytest.raises(RuntimeError, match="storage failure"):
        svc.publish("sales", req, "failed", None)
    assert svc.head("sales")["snapshot_id"] == ""
    assert rt.ontology_markdown_state("sales").published_markdown == ""


def test_publication_rechecks_head_after_expensive_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, svc, _, prepared = prepared_workspace()
    original = svc._validate_confirmation
    calls = 0

    def validate_then_change(*args: Any) -> None:
        nonlocal calls
        original(*args)
        calls += 1
        if calls == 2:
            monkeypatch.setattr(svc, "head", lambda _: {"snapshot_id": "concurrent", "etag": "new"})

    monkeypatch.setattr(svc, "_validate_confirmation", validate_then_change)
    with pytest.raises(OntologyVersionConflictError):
        svc.publish(
            "sales",
            MarkdownConfirmRequest(
                preparation_id=prepared["id"], draft_etag=prepared["draft_etag"], confirmed=True
            ),
            "race",
            None,
        )
    assert not any(
        d["artifact_type"] == "ontology_markdown_snapshot"
        for d in svc.store.list_artifacts("profile-ontology:sales")
    )


def test_concept_references_ground_function_and_link_names_in_same_profile() -> None:
    from app.features.nl2sql.ontology_catalog import retrieve_ontology_nodes

    rt, svc, parser, original = prepared_workspace()
    parser.definitions = all_concepts()
    parser.lines[0]["definition_api_names"] = [d.api_name for d in parser.definitions]
    preparation = svc.prepare("sales", original["draft_etag"], "references", None)
    svc.run_preparation("sales", preparation["id"])
    svc.publish(
        "sales",
        MarkdownConfirmRequest(
            preparation_id=preparation["id"], draft_etag=original["draft_etag"], confirmed=True
        ),
        "references-publish",
        None,
    )
    view, graph = rt.profile_view("sales")
    for question in ["Calculate", "Related"]:
        hits = retrieve_ontology_nodes(question, graph, view)
        object_ids = {n.id for n in graph.nodes if n.kind.value == "object_type"}
        assert object_ids & {h.node_id for h in hits}


def test_data_checks_keep_sql_permission_and_failed_checks_block_publication() -> None:
    from fastapi import HTTPException
    from test_nl2sql_ontology_access import _principal

    from app.features.nl2sql.ontology_definitions import DefinitionDataValidationRequest
    from app.features.nl2sql.ontology_markdown_workspace import PREPARATION
    from app.features.nl2sql.ontology_service import OntologyGateBlockedError

    _, svc, _, prepared = prepared_workspace()
    with pytest.raises(HTTPException) as denied:
        svc.validate_data(
            "sales",
            prepared["id"],
            DefinitionDataValidationRequest(confirmed=True),
            _principal({"sales"}, profile_manager=True),
        )
    assert denied.value.status_code == 403
    prepared["data_report"] = {"errors": 1, "acceptance_cases": [{"passed": False}]}
    svc._write("sales", prepared["id"], PREPARATION, prepared)
    with pytest.raises(OntologyGateBlockedError) as blocked:
        svc.publish(
            "sales",
            MarkdownConfirmRequest(
                preparation_id=prepared["id"], draft_etag=prepared["draft_etag"], confirmed=True
            ),
            "failed-data",
            None,
        )
    assert blocked.value.code == "MARKDOWN_DATA_VALIDATION_FAILED"
    assert svc.head("sales")["snapshot_id"] == ""


@pytest.mark.asyncio
async def test_retired_capability_mutations_check_access_and_preserve_history() -> None:
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from test_nl2sql_ontology_access import _principal

    from app.features.nl2sql.ontology_capability_router import create_capability_router
    from app.features.nl2sql.ontology_router import _raise_domain_error

    rt, _ = runtime()
    app = FastAPI()
    actor = _principal({"sales"}, profile_manager=True)

    @app.middleware("http")
    async def principal(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.principal = actor
        return await call_next(request)

    app.include_router(create_capability_router(lambda: rt, _raise_domain_error))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        root = "/profiles/sales/ontology-capabilities"
        for suffix, body in [
            ("invoke", {"release_id": "historical"}),
            ("preview", {"release_id": "historical"}),
            ("execute", {"preview_id": "prior", "confirmed": True}),
        ]:
            response = await client.post(
                root + "/function/" + suffix, json=body, headers={"Idempotency-Key": "no-execution"}
            )
            assert response.status_code == 410, response.text
        assert (await client.get(root)).status_code == 200
        actor = _principal({"other"})
        denied = await client.post(
            root + "/function/invoke",
            json={"release_id": "historical"},
            headers={"Idempotency-Key": "denied"},
        )
        assert denied.status_code == 403


def test_all_types_publish_edit_and_keep_the_original_snapshot() -> None:
    rt, svc, parser, original = prepared_workspace()
    parser.definitions = all_concepts()
    parser.lines[0]["definition_api_names"] = [d.api_name for d in parser.definitions]
    prepared = svc.prepare("sales", original["draft_etag"], "all-types", None)
    svc.run_preparation("sales", prepared["id"])
    ready = svc.preparation("sales", prepared["id"])
    assert ready["status"] == "ready", ready["findings"]
    assert [c["kind"] for c in ready["coverage"]] == list(CONCEPT_ORDER)
    request = MarkdownConfirmRequest(
        preparation_id=ready["id"], draft_etag=ready["draft_etag"], confirmed=True
    )
    first = svc.publish("sales", request, "all-publish", None)
    old = svc.snapshot("sales", first.id)
    graph = rt.profile_view("sales")[1]
    assert len([n for n in graph.nodes if n.metadata.get("definition")]) == 12
    assert len([e for e in graph.edges if e.kind.value == "link_type"]) == 1
    changed = rt.save_ontology_markdown_draft(
        "sales",
        OntologyMarkdownDraftPatch(
            markdown="受注の定義を更新する。",
            base_etag=rt.ontology_markdown_state("sales").draft_etag,
        ),
    )
    assert changed.draft_revision is not None
    assert changed.draft_version is not None and changed.published_version is not None
    assert changed.draft_revision.id != ready["source_revision_id"]
    assert changed.draft_version > changed.published_version
    assert svc.snapshot("sales", first.id) == old
    context = published_context(rt, "sales", first.id)
    assert context.startswith("# 業務オントロジー\n")
    assert original["markdown"] not in context
    assert "APP." not in published_context(rt, "sales", first.id, {})


async def test_removed_definition_import_api_is_not_exposed() -> None:
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    paths = app.openapi()["paths"]
    prefix = "/api/nl2sql/profiles/{profile_id}/ontology-markdown"
    assert f"{prefix}/prepare" in paths
    assert f"{prefix}/publish" in paths
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for suffix in ("migration-preview", "migrate"):
            assert f"{prefix}/{suffix}" not in paths
            response = await client.post(
                f"/api/nl2sql/profiles/sales/ontology-markdown/{suffix}", json={}
            )
            assert response.status_code == 404


def test_build_content_notes_are_saved_without_becoming_conflicts_or_job_warnings() -> None:
    from test_nl2sql_ontology_build import _FakeEnterpriseAiClient, _wait_for_job

    from app.features.nl2sql.ontology_build import OntologyBuildService

    notes = [
        "business_text_chunks はすべて検証エラー文のみで正の業務記述なし",
        "Order.id / evidence: 証拠の資料・位置・原文を照合できません。",
    ]
    operational_warning = "資料の一部で抽出に失敗しました。"
    definitions = [d.model_dump(mode="json") for d in all_concepts()]
    next(d for d in definitions if d["kind"] == "metric")["expression_sql"] = (
        "COUNT(APP.ORDERS.MISSING_COLUMN)"
    )
    rt, legacy = runtime()
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(
        json.dumps(
            {
                "definitions": definitions,
                "warnings_ja": [*notes, *notes, operational_warning],
            }
        )
    )
    build = OntologyBuildService(rt)
    queued = build.start("sales", business_text="受注の業務定義")
    build.run_persisted(queued.id)
    job = _wait_for_job(build, queued.id)
    assert job.status == "succeeded", job.error_message_ja
    assert operational_warning in job.warnings_ja
    assert not any(note in job.warnings_ja for note in notes)
    markdown = job.markdown_output
    from app.features.nl2sql.ontology_markdown_workspace import GENERATED
    from app.features.nl2sql.ontology_store import stable_ontology_id

    workspace = MarkdownOntologyWorkspace(rt)
    internal = workspace._read(
        "sales", stable_ontology_id(GENERATED, "sales", job.draft_revision_id), GENERATED
    )
    for note in notes:
        assert note not in markdown
        assert any(d.get("message_ja") == note for d in internal["diagnostics"])
    assert "profile_concept_" not in markdown
    assert "source_id:" not in markdown
    # AI の指標の SQL が確かめられなければ下書きに入れず、公開を止めない（best effort。#1729）。
    invalid = next(d for d in all_concepts() if d.kind == "metric")
    for definition in all_concepts():
        assert markdown.count(f"(`{definition.api_name}`)") == int(
            definition.api_name != invalid.api_name
        )
    assert any(
        f"『{invalid.name_ja}』の SQL の式を確かめられなかった" in str(d.get("message_ja"))
        for d in internal["diagnostics"]
    )
    preparation = workspace.prepare("sales", job.draft_etag, "check-invalid-sql", None)
    workspace.run_preparation("sales", preparation["id"])
    result = workspace.preparation("sales", preparation["id"])
    assert not any(f.get("code") == "SQL_EXPRESSION_INVALID" for f in result["findings"])
    saved = rt.ontology_markdown_state("sales")
    assert saved.draft_markdown == markdown


def test_content_notes_can_be_published_as_context_without_losing_definitions() -> None:
    rt, svc, parser, _ = prepared_workspace()
    markdown = (
        "受注を受注番号で識別する。\n\n## 記述範囲と補足\n"
        "- Order.id / evidence: 証拠の資料・位置・原文を照合できません。"
    )
    state = rt.save_ontology_markdown_draft(
        "sales",
        OntologyMarkdownDraftPatch(
            markdown=markdown, base_etag=rt.ontology_markdown_state("sales").draft_etag
        ),
    )
    parser.lines.append(
        {
            "start_line": 3,
            "end_line": 4,
            "disposition": "context",
            "reason_ja": "資料不足に関する補足であり業務定義ではありません。",
        }
    )
    preparation = svc.prepare("sales", state.draft_etag, "with-content-notes", None)
    svc.run_preparation("sales", preparation["id"])
    preparation = svc.preparation("sales", preparation["id"])
    assert preparation["status"] == "ready", preparation
    svc.publish(
        "sales",
        MarkdownConfirmRequest(
            preparation_id=preparation["id"], draft_etag=state.draft_etag, confirmed=True
        ),
        "publish-with-content-notes",
        None,
    )
    assert rt.ontology_markdown_state("sales").published_markdown == markdown
    snapshot = svc.snapshot("sales")
    assert snapshot is not None
    assert len(snapshot["definitions"]) == len(parser.definitions)


def test_guided_confirmation_uses_the_profile_markdown_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """推薦 → 確認 → session 作成が Markdown 公開版の revision で一貫する(422 にしない)。"""
    from app.features.nl2sql.models import AllowedObjects
    from app.features.nl2sql.ontology_router import (
        OntologyProfileRecommendationRequest,
        ProfileRecommendationConfirmationRequest,
        QuerySessionApiCreate,
    )

    monkeypatch.setattr(get_settings(), "nl2sql_ontology_profile_confirmation_required", True)
    rt, svc, _parser, preparation = prepared_workspace()
    job = svc.publish(
        "sales",
        MarkdownConfirmRequest(
            preparation_id=preparation["id"], draft_etag=preparation["draft_etag"], confirmed=True
        ),
        "publish-guided",
        None,
    )
    monkeypatch.setattr(rt.legacy_service, "_enterprise_ai_client", _RequirementLlmStub())
    monkeypatch.setattr(
        rt.legacy_service,
        "resolve_allowed_objects",
        lambda *_: AllowedObjects(table_names=["APP.ORDERS"], enforce_table_scope=True),
        raising=False,
    )
    question = "受注の一覧"
    recommendation = rt.recommend_profiles(
        OntologyProfileRecommendationRequest(question=question, limit=3)
    )
    candidate = next(item for item in recommendation.candidates if item.profile_id == "sales")
    assert candidate.ontology_revision_id == job.revision_id
    _confirmed, token = rt.confirm_profile_recommendation(
        recommendation.id,
        ProfileRecommendationConfirmationRequest(
            selected_profile_id="sales", selected_revision_id=candidate.ontology_revision_id
        ),
    )
    created = rt.create_session(
        QuerySessionApiCreate(
            profile_id="sales",
            question=question,
            clarification_mode="guided",
            profile_confirmation_token=token,
        )
    )
    assert created.session.ontology_revision_id == job.revision_id


def test_context_search_reads_inferred_closure_from_markdown_snapshot() -> None:
    """Markdown 公開版は推論結果を snapshot に固定しており、検索拡張もそれを使う。"""
    rt, svc, _parser, preparation = prepared_workspace()
    job = svc.publish(
        "sales",
        MarkdownConfirmRequest(
            preparation_id=preparation["id"], draft_etag=preparation["draft_etag"], confirmed=True
        ),
        "publish-inferred",
        None,
    )
    graph = rt._query_ontology("sales")
    assert graph.revision.id == job.revision_id
    from urllib.parse import unquote

    from rdflib import Graph, URIRef
    from rdflib.namespace import RDF, RDFS

    snapshot = svc.snapshot("sales", job.id)
    assert snapshot is not None
    inferred = Graph().parse(data=snapshot["artifacts"]["inferred_turtle"], format="turtle")
    prefix = "urn:nl2sql:ontology:node:"
    edges = [
        (unquote(str(left).removeprefix(prefix)), unquote(str(right).removeprefix(prefix)))
        for left, predicate, right in inferred
        if predicate in {RDF.type, RDFS.subClassOf, RDFS.domain, RDFS.range}
        and isinstance(left, URIRef)
        and isinstance(right, URIRef)
        and str(left).startswith(prefix)
        and str(right).startswith(prefix)
    ]
    node_ids = {node.id for node in graph.nodes}
    seed = next((left for left, right in edges if left in node_ids and right in node_ids), None)
    assert seed is not None, "推論結果に Profile 内の node 間の意味関係が無い"
    expanded = rt._inferred_context_node_ids(
        "sales", graph.revision.id, {seed}, allowed_node_ids=node_ids, max_hops=2
    )
    assert expanded


def test_column_scope_on_one_table_keeps_published_definitions_of_other_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """列を絞っていない許可表は全列扱い。他表だけ列を絞っても公開定義を prompt から落とさない。"""
    from app.features.nl2sql import ontology_router
    from app.features.nl2sql.models import AllowedObjects, JobCreateRequest
    from app.features.nl2sql.service import Nl2SqlService
    from app.features.nl2sql.store import MemoryNl2SqlStore

    rt, svc, _parser, preparation = prepared_workspace()
    job = svc.publish(
        "sales",
        MarkdownConfirmRequest(
            preparation_id=preparation["id"], draft_etag=preparation["draft_etag"], confirmed=True
        ),
        "publish-scope",
        None,
    )
    monkeypatch.setattr(ontology_router, "ontology_runtime", rt)
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    request = JobCreateRequest(profile_id="sales", question="q", use_ontology_context=True)
    profile = rt._strict_profile("sales")

    def render(allowed: AllowedObjects) -> str | None:
        return service._job_published_ontology_markdown(
            request=request, profile=profile, business_release_id=job.id, allowed=allowed
        )

    tables = ["APP.ORDERS", "APP.CUSTOMERS"]
    partial = render(AllowedObjects(table_names=tables, columns={"APP.CUSTOMERS": ["ID"]}))
    assert partial and "### 受注（APP.ORDERS）" in partial


@pytest.mark.parametrize("change", ["row_count", "comment", "glossary", "allowed_tables"])
def test_published_context_survives_statistics_and_profile_edits_outside_scope(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """統計・コメント・用語集の更新では公開版を止めず、対象 object の変更だけ再公開を求める。"""
    from app.features.nl2sql import ontology_router
    from app.features.nl2sql.models import AllowedObjects, JobCreateRequest
    from app.features.nl2sql.ontology_service import OntologyGateBlockedError
    from app.features.nl2sql.service import Nl2SqlService
    from app.features.nl2sql.store import MemoryNl2SqlStore

    rt, svc, _parser, preparation = prepared_workspace()
    job = svc.publish(
        "sales",
        MarkdownConfirmRequest(
            preparation_id=preparation["id"], draft_etag=preparation["draft_etag"], confirmed=True
        ),
        "publish-stats",
        None,
    )
    monkeypatch.setattr(ontology_router, "ontology_runtime", rt)
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    request = JobCreateRequest(profile_id="sales", question="q", use_ontology_context=True)
    allowed = AllowedObjects(table_names=["APP.ORDERS", "APP.CUSTOMERS"])
    legacy = rt.legacy_service
    if change == "row_count":
        legacy.catalog.tables[0].row_count = 12345
    elif change == "comment":
        legacy.catalog.tables[0].comment = "統計更新後のコメント"
    elif change == "glossary":
        legacy.profile = legacy.profile.model_copy(
            update={"glossary": {"売上": "受注金額"}, "etag": "changed"}
        )
    else:
        legacy.profile = legacy.profile.model_copy(
            update={"allowed_tables": [*legacy.profile.allowed_tables, "APP.EXTRA"]}
        )

    def render() -> str | None:
        return service._job_published_ontology_markdown(
            request=request,
            profile=rt._strict_profile("sales"),
            business_release_id=job.id,
            allowed=allowed,
        )

    if change == "allowed_tables":
        with pytest.raises(OntologyGateBlockedError):
            render()
    else:
        assert render()


def test_publication_with_legacy_full_scope_fingerprint_stays_usable() -> None:
    """指紋の対象を絞る前(Profile 全体・schema context 全体)の公開版も参照できる。"""
    from app.features.nl2sql.ontology_definition_service import definition_fingerprint
    from app.features.nl2sql.ontology_markdown_workspace import SNAPSHOT

    rt, svc, _parser, preparation = prepared_workspace()
    job = svc.publish(
        "sales",
        MarkdownConfirmRequest(
            preparation_id=preparation["id"], draft_etag=preparation["draft_etag"], confirmed=True
        ),
        "publish-legacy",
        None,
    )
    snapshot = svc.snapshot("sales", job.id)
    assert snapshot is not None
    schema = str(rt.prepare_build_schema_context("sales").schema_context)
    snapshot["profile_hash"] = definition_fingerprint(
        rt._strict_profile("sales").model_dump(mode="json")
    )
    snapshot["schema_hash"] = definition_fingerprint(schema)
    svc._write("sales", job.id, SNAPSHOT, snapshot)
    assert published_context(rt, "sales", job.revision_id).startswith("# 業務オントロジー\n")


@pytest.mark.parametrize(
    ("allowed", "expected"),
    [
        ({"table_names": ["APP.ORDERS", "APP.CUSTOMERS"]}, "旧形式の業務記述"),
        ({"table_names": ["APP.ORDERS"]}, None),
        ({"table_names": ["APP.ORDERS", "APP.CUSTOMERS"], "columns": {"APP.ORDERS": ["ID"]}}, None),
    ],
)
def test_job_sends_legacy_published_markdown_only_for_full_profile_scope(
    monkeypatch: pytest.MonkeyPatch, allowed: dict[str, Any], expected: str | None
) -> None:
    """旧形式の公開版(ontology_revision_*)は、Profile 全体を許可した job にだけ全文を渡す。"""
    from types import SimpleNamespace

    from app.features.nl2sql import ontology_router
    from app.features.nl2sql.models import AllowedObjects, JobCreateRequest, Nl2SqlProfile
    from app.features.nl2sql.service import Nl2SqlService
    from app.features.nl2sql.store import MemoryNl2SqlStore

    monkeypatch.setattr(
        ontology_router,
        "ontology_runtime",
        SimpleNamespace(published_markdown_for_revision=lambda *_a, **_k: "旧形式の業務記述"),
    )
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    profile = Nl2SqlProfile(id="sales", name="販売", allowed_tables=["APP.ORDERS", "APP.CUSTOMERS"])
    monkeypatch.setattr(
        service, "profile_allowed_object_names", lambda _p: ["APP.ORDERS", "APP.CUSTOMERS"]
    )

    assert (
        service._job_published_ontology_markdown(
            request=JobCreateRequest(profile_id="sales", question="q", use_ontology_context=True),
            profile=profile,
            business_release_id="ontology_revision_legacy",
            allowed=AllowedObjects(**allowed),
        )
        == expected
    )


def test_create_session_rejects_publication_between_graph_and_head_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """グラフ取得と公開 head の読み取りの間に公開が入ったら、版の混ざった session を作らない。"""
    from app.features.nl2sql.ontology_markdown_workspace import MarkdownOntologyWorkspace
    from app.features.nl2sql.ontology_router import QuerySessionApiCreate

    rt, svc, _parser, preparation = prepared_workspace()
    svc.publish(
        "sales",
        MarkdownConfirmRequest(
            preparation_id=preparation["id"], draft_etag=preparation["draft_etag"], confirmed=True
        ),
        "publish-race",
        None,
    )
    original_query = rt._query_ontology
    original_head = MarkdownOntologyWorkspace.head
    graph_read = {"done": False}

    def query_ontology(profile_id: str = "") -> Any:
        graph = original_query(profile_id)
        graph_read["done"] = True
        return graph

    def head(self: MarkdownOntologyWorkspace, profile_id: str) -> dict[str, Any]:
        value = original_head(self, profile_id)
        # グラフを読んだ直後に別の版が公開された状態
        return (
            {**value, "snapshot_id": "ontology_markdown_snapshot_newer"}
            if graph_read["done"]
            else value
        )

    monkeypatch.setattr(rt, "_query_ontology", query_ontology)
    monkeypatch.setattr(MarkdownOntologyWorkspace, "head", head)

    with pytest.raises(OntologyVersionConflictError):
        rt.create_session(QuerySessionApiCreate(profile_id="sales", question="受注の一覧"))


def test_job_reads_published_snapshot_once_without_runtime_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公開の snapshot の文脈は、snapshot を 1 回だけ読み、runtime の lock を待たずに作る。

    #1155。
    """
    import threading

    from app.features.nl2sql import ontology_router
    from app.features.nl2sql.models import AllowedObjects, JobCreateRequest
    from app.features.nl2sql.service import Nl2SqlService
    from app.features.nl2sql.store import MemoryNl2SqlStore

    rt, svc, _parser, preparation = prepared_workspace()
    job = svc.publish(
        "sales",
        MarkdownConfirmRequest(
            preparation_id=preparation["id"], draft_etag=preparation["draft_etag"], confirmed=True
        ),
        "publish-once",
        None,
    )
    monkeypatch.setattr(ontology_router, "ontology_runtime", rt)
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    request = JobCreateRequest(profile_id="sales", question="q", use_ontology_context=True)
    profile = rt._strict_profile("sales")
    assert rt.published_release_id("sales") == job.id

    reads: list[str] = []
    original_get_artifact = rt.store.get_artifact

    def get_artifact(artifact_id: str) -> Any:
        reads.append(artifact_id)
        return original_get_artifact(artifact_id)

    monkeypatch.setattr(rt.store, "get_artifact", get_artifact)
    outcome: dict[str, Any] = {}
    done = threading.Event()

    def render() -> None:
        outcome["markdown"] = service._job_published_ontology_markdown(
            request=request,
            profile=profile,
            business_release_id=job.id,
            allowed=AllowedObjects(table_names=["APP.ORDERS", "APP.CUSTOMERS"]),
        )
        done.set()

    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with rt._lock:  # lock を持ったまま DB を読む別の処理の代わり
            held.set()
            release.wait(30)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(5)
    try:
        threading.Thread(target=render, daemon=True).start()
        finished = done.wait(5)
    finally:
        release.set()
        holder.join(5)
    assert finished, "公開の snapshot の文脈がオントロジーの lock を待った"
    assert outcome["markdown"]
    assert reads.count(job.id) == 1


class _RequirementLlmStub:
    """回答前の確認の LLM のスタブ（APP.ORDERS の一覧の要件を返す。#1439）。"""

    def is_configured(self) -> bool:
        return True

    def model_id(self) -> str:
        return "stub"

    def generate(self, **_kwargs: object) -> str:
        item: dict[str, object] = {
            "reason_ja": "",
            "question_ja": "",
            "answer_kind": "free_text",
            "source": "question",
            "columns": [],
            "relationship_ids": [],
            "sql_hint": "",
            "period": None,
            "candidates": [],
        }
        return json.dumps(
            {
                "clarified_question_ja": "APP.ORDERS の一覧",
                "answerable": True,
                "unanswerable_reason_ja": "",
                "items": [
                    {
                        **item,
                        "key": "from_orders",
                        "section": "from",
                        "label_ja": "使う表",
                        "value_ja": "APP.ORDERS",
                        "status": "confirmed",
                        "tables": ["APP.ORDERS"],
                    },
                    {
                        **item,
                        "key": "select_all",
                        "section": "select",
                        "label_ja": "表示する列",
                        "value_ja": "すべての列",
                        "status": "confirmed",
                        "tables": [],
                    },
                ],
            },
            ensure_ascii=False,
        )
