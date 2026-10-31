"""Ontology query-session router runtime と persistence 接続のテスト。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

import app.features.nl2sql.ontology_router as ontology_router_module
from app.features.nl2sql.models import (
    AllowedObjects,
    ExplainPlanData,
    Nl2SqlProfile,
    PreviewData,
    PreviewRequest,
    QueryResults,
    SafetyReport,
    SchemaCatalog,
    SchemaCatalogHead,
    SchemaColumn,
    SchemaTable,
    StageTiming,
    TimingEnvelope,
)
from app.features.nl2sql.ontology_build import OntologyBuildService
from app.features.nl2sql.ontology_models import (
    ColumnQueryPolicy,
    GraphPatch,
    GraphPatchOperation,
    OntologyEdge,
    OntologyEdgeKind,
    OntologyNode,
    OntologyNodeKind,
    OntologyProvenance,
    OntologyReasoningStatus,
    OntologyReviewStatus,
    OntologyRevisionStatus,
    OntologySourceDocument,
    OntologySourceKind,
    OntologySourceRole,
    OntologySourceStatus,
    PhysicalMapping,
    QuerySessionStatus,
    SqlConfirmationRequest,
)
from app.features.nl2sql.ontology_reasoning import OntologyPublishService
from app.features.nl2sql.ontology_router import (
    GenerateSqlRequest,
    ImprovementProposalRequest,
    OntologyApiRuntime,
    OntologyContextSearchRequest,
    OntologyDraftRequest,
    OntologyMarkdownDraftPatch,
    OntologyProfileRecommendationRequest,
    OntologyPublishRequest,
    ProfileOntologyViewPatch,
    ProfileRecommendationConfirmationRequest,
    QuerySessionApiCreate,
    SqlBindingRequest,
    router,
)
from app.features.nl2sql.ontology_service import (
    OntologyGateBlockedError,
    OntologyNotFoundError,
    OntologyStateConflictError,
    OntologyVersionConflictError,
)
from app.features.nl2sql.ontology_sources import OntologySourceStorage
from app.features.nl2sql.ontology_store import (
    InMemoryOntologyStore,
    OntologyCollection,
    OntologyVersionConflict,
    stable_physical_id,
)
from app.main import app
from app.security.domain import Principal
from app.security.permissions import QUERY_GENERATE_PERMISSION, SQL_EXECUTE_PERMISSION
from app.settings import get_settings


class _FakeLegacyNl2SqlService:
    def __init__(self) -> None:
        self.profile = Nl2SqlProfile(
            id="sales",
            name="販売分析",
            allowed_tables=["APP.ORDERS"],
            default_row_limit=100,
        )
        self.catalog = SchemaCatalog(
            refreshed_at="2026-07-11T00:00:00Z",
            tables=[
                SchemaTable(
                    table_name="ORDERS",
                    logical_name="受注",
                    owner="APP",
                    comment="受注明細",
                    columns=[
                        SchemaColumn(
                            column_name="ID",
                            logical_name="受注 ID",
                            data_type="NUMBER",
                            nullable=False,
                        ),
                        SchemaColumn(
                            column_name="CUSTOMER_ID",
                            logical_name="顧客 ID",
                            data_type="NUMBER",
                            nullable=False,
                        ),
                        SchemaColumn(
                            column_name="AMOUNT",
                            logical_name="受注金額",
                            data_type="NUMBER",
                        ),
                    ],
                )
            ],
        )
        self.preview_requests: list[PreviewRequest] = []
        self.executed_sql: list[str] = []
        self.executed_row_limits: list[int | None] = []
        self.recorded_history: list[dict[str, Any]] = []

    def get_catalog(self) -> SchemaCatalog:
        return self.catalog

    def get_profile(self, profile_id: str) -> Nl2SqlProfile:
        if profile_id != self.profile.id:
            raise ValueError("profile not found")
        return self.profile

    def resolve_allowed_objects(
        self,
        profile_id: str,
        requested: AllowedObjects,
    ) -> AllowedObjects:
        self.get_profile(profile_id)
        if not requested.table_names:
            return AllowedObjects(table_names=["APP.ORDERS"], enforce_table_scope=True)
        accepted = [
            "APP.ORDERS"
            for name in requested.table_names
            if name.replace('"', "").upper() in {"ORDERS", "APP.ORDERS"}
        ]
        return AllowedObjects(
            table_names=accepted,
            columns={
                key: value
                for key, value in requested.columns.items()
                if key.replace('"', "").upper() in {"ORDERS", "APP.ORDERS"}
            },
            enforce_table_scope=True,
        )

    def preview(self, request: PreviewRequest) -> PreviewData:
        self.preview_requests.append(request)
        limit = request.row_limit
        sql = "SELECT COUNT(*) AS ORDER_COUNT FROM APP.ORDERS"
        if limit is not None:
            sql = f"{sql} FETCH FIRST {limit} ROWS ONLY"
        return PreviewData(
            sql=sql,
            executable_sql=sql,
            is_safe=True,
            row_limit=limit or 0,
            note="deterministic",
            engine_meta={
                "generation_elapsed_ms": 42,
                "engine_timings": [
                    {
                        "engine": "select_ai_agent",
                        "elapsed_ms": 42,
                        "status": "success",
                        "error": "",
                    }
                ],
            },
            timing=TimingEnvelope(
                created_at="2026-09-06T00:00:00+00:00",
                elapsed_ms=55,
                stage_timings=[StageTiming(stage="generate", elapsed_ms=55)],
            ),
        )

    def execute_sql(
        self,
        sql: str,
        allowed: AllowedObjects,
        row_limit: int | None,
    ) -> tuple[SafetyReport, str, QueryResults]:
        assert allowed.table_names == ["APP.ORDERS"]
        self.executed_sql.append(sql)
        self.executed_row_limits.append(row_limit)
        return (
            SafetyReport(
                is_safe=True,
                is_select_only=True,
                row_limit_applied=row_limit or 0,
                referenced_tables=["APP.ORDERS"],
            ),
            sql,
            QueryResults(columns=["ORDER_COUNT"], rows=[{"ORDER_COUNT": 3}], total=1),
        )

    def explain_sql(self, _sql: str) -> ExplainPlanData:
        return ExplainPlanData(
            available=True,
            total_cost=7,
            estimated_cardinality=3,
        )

    def record_ontology_history(self, **payload: Any) -> None:
        self.recorded_history.append(payload)


class _IncrementalCatalogLegacyService(_FakeLegacyNl2SqlService):
    uses_incremental_store = True

    def __init__(self) -> None:
        super().__init__()
        self.catalog_reads = 0
        self.head = SchemaCatalogHead(
            catalog_version=1,
            schema_fingerprint="catalog-head-fingerprint",
        )

    def get_catalog(self) -> SchemaCatalog:
        self.catalog_reads += 1
        return super().get_catalog()

    def get_catalog_head(self) -> SchemaCatalogHead:
        return self.head.model_copy(deep=True)


class _FakeOntologyEmbeddingClient:
    def is_configured(self) -> bool:
        return True

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [
            [1.0 if index == (len(text) % 8) else 0.0 for index in range(1536)] for text in texts
        ]


class _BatchRecordingStore(InMemoryOntologyStore):
    def __init__(self) -> None:
        super().__init__()
        self.individual_saves: list[OntologyCollection] = []
        self.atomic_saves: list[tuple[OntologyCollection, int]] = []

    def save_document(
        self,
        collection: OntologyCollection,
        document: Mapping[str, Any] | Any,
        *,
        expected_etag: str | None = None,
    ) -> dict[str, Any]:
        self.individual_saves.append(collection)
        return super().save_document(
            collection,
            document,
            expected_etag=expected_etag,
        )

    def save_documents_atomic(
        self,
        collection: OntologyCollection,
        documents: Sequence[tuple[Mapping[str, Any], str | None]],
    ) -> list[dict[str, Any]]:
        self.atomic_saves.append((collection, len(documents)))
        return super().save_documents_atomic(collection, documents)


class _OnePublishedEnforcingStore(InMemoryOntologyStore):
    """Oracle の active published unique index をテスト内で再現する store。"""

    def save_documents_atomic(
        self,
        collection: OntologyCollection,
        documents: Sequence[tuple[Mapping[str, Any], str | None]],
    ) -> list[dict[str, Any]]:
        if collection == "revisions":
            statuses = {
                str(document["revision_id"]): str(document["status"])
                for document in self.list_documents("revisions")
            }
            for document, _expected_etag in documents:
                revision_id = str(document["revision_id"])
                next_status = str(document["status"])
                if next_status == OntologyRevisionStatus.PUBLISHED.value:
                    active_revision_ids = [
                        current_revision_id
                        for current_revision_id, current_status in statuses.items()
                        if current_revision_id != revision_id
                        and current_status == OntologyRevisionStatus.PUBLISHED.value
                    ]
                    if active_revision_ids:
                        raise OntologyVersionConflict(
                            "An Ontology document was concurrently created."
                        )
                statuses[revision_id] = next_status
        return super().save_documents_atomic(collection, documents)


@pytest.fixture
def runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService]:
    # 既存の query-session 単体テストは Profile 確認以外を検証する。確認ゲートは専用テストで
    # 既定 true のまま検証する。
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_profile_confirmation_required", False)
    store = InMemoryOntologyStore()
    legacy = _FakeLegacyNl2SqlService()
    return OntologyApiRuntime(legacy_service=legacy, store=store), store, legacy


def _confirmation(data: Any) -> SqlConfirmationRequest:
    artifact = data.session.sql_artifacts[-1]
    return SqlConfirmationRequest(
        artifact_id=artifact.id,
        ontology_revision_id=artifact.ontology_revision_id,
        intent_version=artifact.intent_version,
        sql_hash=artifact.sql_hash,
        validation_hash=artifact.validation_report.validation_hash,
        generation_context_hash=artifact.generation_context_hash,
    )


def _generate_request(data: Any) -> GenerateSqlRequest:
    return GenerateSqlRequest(
        base_version=data.session.current_intent_version,
        intent_version=data.session.current_intent_version,
        ontology_revision_id=data.session.ontology_revision_id,
        confirm_intent=True,
    )


def _principal_with_permissions(permissions: set[str]) -> Principal:
    return Principal(
        user_uuid="user-1",
        login_user_id="user1",
        display_name="利用者",
        status="ACTIVE",
        force_password_change=False,
        role_codes=["ANALYST"],
        permissions=permissions,
        data_entitlements=[],
        allowed_profile_ids={"sales"},
        session_id="session-1",
        csrf_token_hash="csrf",
    )


def _request_with_principal(principal: Principal | None) -> Any:
    return SimpleNamespace(state=SimpleNamespace(principal=principal))


def _source_document(
    source_id: str,
    filename: str,
    *,
    profile_id: str = "sales",
    content: bytes = b"# rules",
    source_role: OntologySourceRole = OntologySourceRole.SOURCE,
) -> OntologySourceDocument:
    return OntologySourceDocument(
        id=source_id,
        profile_id=profile_id,
        filename=filename,
        media_type="text/markdown",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        storage_uri=f"memory://{profile_id}/{source_id}",
        status=OntologySourceStatus.STORED,
        source_role=source_role,
    )


def test_router_declares_complete_query_session_and_profile_view_api() -> None:
    declared_paths = {str(getattr(route, "path", "")) for route in router.routes}
    assert "/nl2sql/query-sessions" in declared_paths
    assert "/nl2sql/query-sessions/{session_id}" in declared_paths
    assert "/nl2sql/query-sessions/{session_id}/intent" in declared_paths
    assert "/nl2sql/query-sessions/{session_id}/generate-sql" in declared_paths
    assert "/nl2sql/query-sessions/{session_id}/confirm-sql" in declared_paths
    assert "/nl2sql/query-sessions/{session_id}/execute" in declared_paths
    assert "/nl2sql/query-sessions/{session_id}/improvement-proposal" in declared_paths
    assert "/nl2sql/profiles/{profile_id}/ontology-view" in declared_paths
    assert "/nl2sql/profiles/{profile_id}/ontology-view/materialize" in declared_paths
    assert "/nl2sql/ontology/revisions" in declared_paths
    assert "/nl2sql/ontology/revisions/current" in declared_paths
    assert "/nl2sql/ontology/revisions/{revision_id}/drafts" in declared_paths
    assert "/nl2sql/ontology/revisions/{revision_id}/publish" in declared_paths
    assert "/nl2sql/profiles/{profile_id}/ontology-source-documents" in declared_paths
    assert (
        "/nl2sql/profiles/{profile_id}/ontology-source-documents/{source_document_id}"
        in declared_paths
    )


def test_initial_profile_view_persists_graph_in_collection_batches() -> None:
    store = _BatchRecordingStore()
    api = OntologyApiRuntime(
        legacy_service=_FakeLegacyNl2SqlService(),
        store=store,
    )

    _view, ontology = api.profile_view("sales")

    assert set(store.individual_saves) == {"revisions"}
    assert store.atomic_saves == [
        ("nodes", len(ontology.nodes)),
        ("edges", len(ontology.edges)),
    ]
    assert len(store.list_documents("nodes", {"revision_id": ontology.revision.id})) == len(
        ontology.nodes
    )
    assert len(store.list_documents("edges", {"revision_id": ontology.revision.id})) == len(
        ontology.edges
    )


def test_published_graph_snapshot_for_job_narrows_to_allowed_objects(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api, _store, legacy = runtime
    legacy.profile.allowed_tables = ["APP.ORDERS", "APP.CUSTOMERS"]
    legacy.catalog.tables.append(
        SchemaTable(
            table_name="CUSTOMERS",
            logical_name="顧客",
            owner="APP",
            comment="顧客情報",
            columns=[
                SchemaColumn(
                    column_name="ID",
                    logical_name="顧客 ID",
                    data_type="NUMBER",
                    nullable=False,
                ),
                SchemaColumn(
                    column_name="NAME",
                    logical_name="顧客名",
                    data_type="VARCHAR2",
                ),
            ],
        )
    )

    draft = api.current_ontology()
    published = api.publish_ontology_revision(
        draft.revision.id,
        OntologyPublishRequest(etag=draft.revision.etag),
    )

    snapshot = api.published_graph_snapshot_for_job(
        profile=legacy.profile,
        allowed=AllowedObjects(table_names=["APP.ORDERS"], enforce_table_scope=True),
        release_id=published.revision.id,
    )

    assert snapshot["revision_id"] == snapshot["revision"]["id"]
    technical_names = {
        node.get("technical_name")
        for node in snapshot["nodes"]
        if node.get("kind") in {"table", "view", "column"}
    }
    assert any(str(name).startswith("APP.ORDERS") for name in technical_names)
    assert not any(str(name).startswith("APP.CUSTOMERS") for name in technical_names)
    node_ids = {node["id"] for node in snapshot["nodes"]}
    assert all(
        edge["source_node_id"] in node_ids and edge["target_node_id"] in node_ids
        for edge in snapshot["edges"]
    )


def test_incremental_catalog_signature_avoids_reloading_unchanged_catalog() -> None:
    service = _IncrementalCatalogLegacyService()
    api = OntologyApiRuntime(
        legacy_service=service,
        store=InMemoryOntologyStore(),
    )

    api.profile_view("sales")
    api.profile_view("sales")

    assert service.catalog_reads == 1
    assert api._synced_catalog_signature == (1, "catalog-head-fingerprint")  # noqa: SLF001

    service.head = service.head.model_copy(
        update={
            "catalog_version": 2,
            "schema_fingerprint": "next-catalog-head-fingerprint",
        }
    )
    api.profile_view("sales")
    api.profile_view("sales")

    assert service.catalog_reads == 2
    assert api._synced_catalog_signature == (  # noqa: SLF001
        2,
        "next-catalog-head-fingerprint",
    )


def test_cold_runtime_reuses_complete_deterministic_draft_revision() -> None:
    service = _IncrementalCatalogLegacyService()
    store = _BatchRecordingStore()
    first_runtime = OntologyApiRuntime(legacy_service=service, store=store)
    _first_view, first_ontology = first_runtime.profile_view("sales")
    store.individual_saves.clear()
    store.atomic_saves.clear()

    second_runtime = OntologyApiRuntime(legacy_service=service, store=store)
    _second_view, second_ontology = second_runtime.profile_view("sales")

    assert second_ontology.revision.id == first_ontology.revision.id
    assert store.individual_saves == []
    assert store.atomic_saves == []


def test_system_schema_reset_clears_incremental_catalog_signature() -> None:
    service = _IncrementalCatalogLegacyService()
    api = OntologyApiRuntime(
        legacy_service=service,
        store=InMemoryOntologyStore(),
    )
    api.profile_view("sales")

    api.reset_after_system_schema_change()

    assert api._synced_catalog_signature is None  # noqa: SLF001
    api.profile_view("sales")
    assert service.catalog_reads == 2


def test_ontology_revision_cache_keeps_active_and_latest_only(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api, _store, _legacy = runtime
    active = api.current_ontology()
    api._ontology_cache_max_revisions = 2  # noqa: SLF001 - bounded cache contract
    second = active.model_copy(
        update={"revision": active.revision.model_copy(update={"id": "revision-second"})},
        deep=True,
    )
    latest = active.model_copy(
        update={"revision": active.revision.model_copy(update={"id": "revision-latest"})},
        deep=True,
    )

    api._cache_ontology(second)  # noqa: SLF001 - bounded cache contract
    api._cache_ontology(latest)  # noqa: SLF001 - bounded cache contract

    assert set(api._ontologies) == {active.revision.id, latest.revision.id}  # noqa: SLF001


def test_column_business_names_reads_only_published_object_columns(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """詳細 lookup は全 graph ではなく published revision の対象列だけを読む。"""
    api, store, legacy = runtime
    draft = api.current_ontology()
    published = api.publish_ontology_revision(
        draft.revision.id,
        OntologyPublishRequest(etag=draft.revision.etag),
    )

    def fail_full_catalog() -> SchemaCatalog:
        raise AssertionError("object-level lookup must not load the schema catalog")

    def fail_full_ontology() -> Any:
        raise AssertionError("object-level lookup must not load the full ontology")

    monkeypatch.setattr(legacy, "get_catalog", fail_full_catalog)
    monkeypatch.setattr(api, "current_ontology", fail_full_ontology)
    original_list = store.list_documents
    calls: list[tuple[str, dict[str, Any], bool]] = []

    def recording_list(
        collection: Any,
        filters: Any = None,
        *,
        include_embedding: bool = True,
    ) -> list[dict[str, Any]]:
        calls.append((str(collection), dict(filters or {}), include_embedding))
        return original_list(
            collection,
            filters,
            include_embedding=include_embedding,
        )

    monkeypatch.setattr(store, "list_documents", recording_list)

    names = api.column_business_names(
        owner="APP",
        object_name="ORDERS",
        object_type="table",
    )

    assert names == {
        "AMOUNT": "受注金額",
        "CUSTOMER_ID": "顧客 ID",
        "ID": "受注 ID",
    }
    assert calls == [
        ("revisions", {"status": "published"}, True),
        (
            "nodes",
            {
                "revision_id": published.revision.id,
                "node_type": "column",
                "physical_id": stable_physical_id("table", "APP", "ORDERS"),
            },
            False,
        ),
    ]


def test_column_business_name_cache_is_revision_aware_and_resettable(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    """同一 revision/object はキャッシュし、schema reset で確実に破棄する。"""
    api, store, _legacy = runtime
    draft = api.current_ontology()
    published = api.publish_ontology_revision(
        draft.revision.id,
        OntologyPublishRequest(etag=draft.revision.etag),
    )
    physical_id = stable_physical_id("table", "APP", "ORDERS")

    first = api.column_business_names(
        owner="APP",
        object_name="ORDERS",
        object_type="table",
    )
    node_documents = store.list_documents(
        "nodes",
        {
            "revision_id": published.revision.id,
            "node_type": "column",
            "physical_id": physical_id,
        },
    )
    id_document = next(
        item for item in node_documents if item["payload"]["metadata"]["column_name"] == "ID"
    )
    id_document["payload"]["business_name_ja"] = "受注番号"
    store.save_document("nodes", id_document, expected_etag=str(id_document["etag"]))

    cached = api.column_business_names(
        owner="APP",
        object_name="ORDERS",
        object_type="table",
    )
    assert cached == first

    api.reset_after_system_schema_change()
    refreshed = api.column_business_names(
        owner="APP",
        object_name="ORDERS",
        object_type="table",
    )
    assert refreshed["ID"] == "受注番号"
    assert len(api._business_name_cache) == 1  # noqa: SLF001 - LRU cache contract
    assert api._business_name_cache_max_objects == 512  # noqa: SLF001


def test_column_business_names_does_not_wait_for_full_ontology_lock(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    """全量 ontology の同期中でも object-level lookup は独立して完了する。"""
    api, store, legacy = runtime
    draft = api.current_ontology()
    api.publish_ontology_revision(
        draft.revision.id,
        OntologyPublishRequest(etag=draft.revision.etag),
    )
    lookup = OntologyApiRuntime(legacy_service=legacy, store=store)
    completed = threading.Event()
    result: dict[str, str] = {}

    def load_names() -> None:
        result.update(
            lookup.column_business_names(
                owner="APP",
                object_name="ORDERS",
                object_type="table",
            )
        )
        completed.set()

    lookup._lock.acquire()  # noqa: SLF001 - full-ontology lock independence contract
    worker = threading.Thread(target=load_names)
    try:
        worker.start()
        assert completed.wait(timeout=1)
    finally:
        lookup._lock.release()  # noqa: SLF001
        worker.join(timeout=1)

    assert result["ID"] == "受注 ID"


def test_profile_scenario_version_changes_for_keyword_only_update(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api, _store, _legacy = runtime
    current, _ontology = api.profile_view("sales")

    updated, _ontology = api.patch_profile_view(
        "sales",
        ProfileOntologyViewPatch(
            base_etag=current.etag,
            activation_keywords=["受注", " 受注 ", "売上"],
        ),
    )

    assert updated.activation_keywords == ["受注", "売上"]
    assert updated.scenario_version == current.scenario_version + 1


def test_profile_view_is_materialized_only_explicitly_and_becomes_stale_after_profile_edit(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api, store, legacy = runtime
    preview, _ontology = api.profile_view("sales")

    assert api.profile_view_persistence_state(preview) == (False, False)
    assert store.list_documents("profile_views", {"profile_id": "sales"}) == []

    materialized = api.materialize_profile_view("sales")
    assert api.profile_view_persistence_state(materialized) == (True, False)
    stored_before = store.get_document(
        "profile_views",
        {"profile_id": "sales", "revision_id": materialized.ontology_revision_id},
    )
    assert stored_before is not None

    legacy.profile = legacy.profile.model_copy(
        update={"etag": "profile-etag-2", "allowed_views": ["APP.ORDER_VIEW"]},
        deep=True,
    )
    current_preview, _ontology = api.profile_view("sales")
    assert api.profile_view_persistence_state(current_preview) == (True, True)
    stored_after_edit = store.get_document(
        "profile_views",
        {"profile_id": "sales", "revision_id": materialized.ontology_revision_id},
    )
    assert stored_after_edit == stored_before

    rebuilt = api.materialize_profile_view("sales")
    assert rebuilt.source_profile_etag == "profile-etag-2"
    assert api.profile_view_persistence_state(rebuilt) == (True, False)


def test_profile_state_delete_removes_live_views_but_preserves_audit_documents(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api, store, _legacy = runtime
    api.materialize_profile_view("sales")
    store.save_document(
        "query_sessions",
        {
            "session_id": "audit-session-1",
            "profile_id": "sales",
            "payload": {"snapshot": True},
        },
    )

    assert api.delete_profile_state("sales") == 1
    assert store.list_documents("profile_views", {"profile_id": "sales"}) == []
    assert store.get_document("query_sessions", {"session_id": "audit-session-1"}) is not None


def test_published_revision_pins_queries_until_reviewed_draft_is_published(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api, _store, legacy = runtime
    base = api.current_ontology()
    published_base = api.publish_ontology_revision(
        base.revision.id,
        OntologyPublishRequest(etag=base.revision.etag),
    )
    table_node = next(node for node in base.nodes if node.kind == OntologyNodeKind.TABLE)
    business_node = OntologyNode(
        id="business_orders",
        revision_id=base.revision.id,
        kind=OntologyNodeKind.BUSINESS_ENTITY,
        technical_name="orders",
        business_name_ja="受注",
        physical_mappings=[PhysicalMapping(object_ref=table_node.physical_mappings[0].object_ref)],
        provenance=OntologyProvenance(source_kind=OntologySourceKind.MANUAL),
        review_status=OntologyReviewStatus.APPROVED,
    )
    draft = api.create_ontology_draft(
        published_base.revision.id,
        OntologyDraftRequest(
            base_etag=published_base.revision.etag,
            note="受注 entity を追加",
            node_upserts=[business_node],
        ),
    )

    before_publish = api.create_session(
        QuerySessionApiCreate(question="受注件数を表示", profile_id="sales")
    )
    assert before_publish.session.ontology_revision_id == published_base.revision.id

    published_draft = api.publish_ontology_revision(
        draft.revision.id,
        OntologyPublishRequest(etag=draft.revision.etag),
    )
    after_publish = api.create_session(
        QuerySessionApiCreate(question="受注件数を表示", profile_id="sales")
    )
    assert after_publish.session.ontology_revision_id == published_draft.revision.id
    assert any(node.id == business_node.id for node in after_publish.ontology_graph.nodes)

    # schema drift は新 draft を作るが、公開済み query scope を自動で置換しない。
    legacy.catalog = legacy.catalog.model_copy(
        update={
            "tables": [
                legacy.catalog.tables[0].model_copy(
                    update={
                        "columns": [
                            *legacy.catalog.tables[0].columns,
                            SchemaColumn(
                                column_name="CREATED_AT",
                                logical_name="作成日時",
                                data_type="TIMESTAMP",
                            ),
                        ]
                    }
                )
            ]
        },
        deep=True,
    )
    drift = api.current_ontology()
    assert drift.revision.status.value == "draft"
    assert drift.revision.parent_revision_id == published_draft.revision.id
    active_view, active_ontology = api.profile_view("sales")
    assert active_view.ontology_revision_id == published_draft.revision.id
    assert active_ontology.revision.id == published_draft.revision.id
    pinned = api.create_session(
        QuerySessionApiCreate(question="受注件数を表示", profile_id="sales")
    )
    assert pinned.session.ontology_revision_id == published_draft.revision.id


def test_publish_blocks_unreviewed_business_definition(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api, _store, _legacy = runtime
    base = api.current_ontology()
    table_node = next(node for node in base.nodes if node.kind == OntologyNodeKind.TABLE)
    proposal = OntologyNode(
        id="business_orders_proposal",
        revision_id=base.revision.id,
        kind=OntologyNodeKind.BUSINESS_ENTITY,
        technical_name="orders_proposal",
        business_name_ja="受注候補",
        physical_mappings=[PhysicalMapping(object_ref=table_node.physical_mappings[0].object_ref)],
        provenance=OntologyProvenance(source_kind=OntologySourceKind.MANUAL),
        review_status=OntologyReviewStatus.PROPOSED,
    )
    draft = api.create_ontology_draft(
        base.revision.id,
        OntologyDraftRequest(
            base_etag=base.revision.etag,
            node_upserts=[proposal],
        ),
    )

    with pytest.raises(OntologyGateBlockedError) as exc_info:
        api.publish_ontology_revision(
            draft.revision.id,
            OntologyPublishRequest(etag=draft.revision.etag),
        )

    assert exc_info.value.code == "ONTOLOGY_REVIEW_REQUIRED"


def test_runtime_executes_two_confirmation_flow_and_persists_every_artifact(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api, store, legacy = runtime
    created = api.create_session(
        QuerySessionApiCreate(
            question="受注件数を表示",
            profile_id="sales",
            allowed_objects=AllowedObjects(table_names=["APP.ORDERS"]),
        )
    )
    assert created.session.status.value == "awaiting_intent_confirmation"
    assert created.session.intents[-1].ambiguities == []

    generated = api.generate_sql(
        created.session.id,
        _generate_request(created),
    )
    assert generated.preview is not None
    assert generated.session.status.value == "awaiting_sql_confirmation"
    assert generated.session.sql_artifacts[-1].validation_report.is_valid is True
    confirmation = _confirmation(generated)

    confirmed = api.confirm_sql(created.session.id, confirmation)
    assert confirmed.session.sql_confirmation is not None
    executed = api.execute(created.session.id, confirmation, actor_user_uuid="user-1")
    assert executed.session.status.value == "done"
    assert executed.result.rows == [{"ORDER_COUNT": 3}]
    assert legacy.executed_sql == [generated.session.sql_artifacts[-1].sql]
    assert legacy.recorded_history[0]["session_id"] == created.session.id
    assert legacy.recorded_history[0]["actor_user_uuid"] == "user-1"
    assert legacy.recorded_history[0]["ontology_trace_summary"]["validation_hash"]
    assert legacy.recorded_history[0]["generation_elapsed_ms"] == 42
    assert legacy.recorded_history[0]["engine_timings"][0]["engine"] == "select_ai_agent"
    assert legacy.recorded_history[0]["stage_timings"][0].stage == "generate"

    assert len(store.list_revisions()) == 1
    assert len(store.list_nodes(generated.session.ontology_revision_id)) >= 4
    persisted_session = store.get_query_session(created.session.id)
    assert persisted_session is not None
    assert persisted_session["profile_view_snapshot"]["profile_id"] == "sales"
    assert store.get_query_session(created.session.id) is not None
    assert store.get_artifact(generated.session.sql_artifacts[-1].id) is not None


def test_runtime_keeps_unspecified_row_limit_out_of_intent_preview_and_execute(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api, _store, legacy = runtime
    created = api.create_session(
        QuerySessionApiCreate(
            question="受注件数を表示",
            profile_id="sales",
            allowed_objects=AllowedObjects(table_names=["APP.ORDERS"]),
        )
    )

    assert created.session.intents[-1].limit is None

    generated = api.generate_sql(created.session.id, _generate_request(created))
    confirmation = _confirmation(generated)
    api.confirm_sql(created.session.id, confirmation)
    executed = api.execute(created.session.id, confirmation, actor_user_uuid="user-1")

    assert generated.preview is not None
    assert generated.preview.row_limit == 0
    assert legacy.preview_requests[-1].row_limit is None
    assert legacy.executed_row_limits == [None]
    assert executed.result.has_more is False
    assert executed.result.truncated is False


def test_runtime_propagates_explicit_intent_limit_to_preview_and_execute(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api, _store, legacy = runtime
    created = api.create_session(
        QuerySessionApiCreate(
            question="受注件数を上位 10 件表示",
            profile_id="sales",
            allowed_objects=AllowedObjects(table_names=["APP.ORDERS"]),
        )
    )

    assert created.session.intents[-1].limit == 10

    generated = api.generate_sql(created.session.id, _generate_request(created))
    confirmation = _confirmation(generated)
    api.confirm_sql(created.session.id, confirmation)
    api.execute(created.session.id, confirmation, actor_user_uuid="user-1")

    assert generated.preview is not None
    assert generated.preview.row_limit == 10
    assert "FETCH FIRST 10 ROWS ONLY" in generated.preview.sql
    assert legacy.preview_requests[-1].row_limit == 10
    assert legacy.executed_row_limits == [10]


def test_generate_sql_includes_published_qa_sql_examples(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api, _store, legacy = runtime
    example_sql = "SELECT AMOUNT FROM APP.ORDERS ORDER BY ID"
    markdown = "\n".join(
        [
            "# Confirmed Markdown",
            "",
            "## Q/A SQL 例",
            "```jsonl",
            json.dumps(
                {
                    "question": "受注金額を確認したい",
                    "sql": example_sql,
                    "note_ja": "回帰テスト用の標準 SQL",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "```",
            "",
        ]
    )
    base = api.current_ontology().revision
    draft, _artifact = api.create_build_markdown_draft(
        profile_id="sales",
        base_revision_id=base.id,
        payloads=[],
        titles=[],
        markdown=markdown,
        note="QA SQL example context test",
    )
    api.publish_ontology_revision(
        draft.revision.id,
        OntologyPublishRequest(etag=draft.revision.etag),
    )
    api.copy_draft_markdown_to_published(draft.revision.id)

    created = api.create_session(
        QuerySessionApiCreate(
            question="受注金額を確認したい",
            profile_id="sales",
            allowed_objects=AllowedObjects(table_names=["APP.ORDERS"]),
        )
    )
    generated = api.generate_sql(created.session.id, _generate_request(created))
    context = legacy.preview_requests[-1].ontology_context

    assert generated.session.status == QuerySessionStatus.AWAITING_SQL_CONFIRMATION
    assert context is not None
    assert context.qa_sql_examples[0].question == "受注金額を確認したい"
    assert context.qa_sql_examples[0].sql == example_sql
    assert context.qa_sql_patterns[0]["physical_tables"] == ["APP.ORDERS"]
    assert context.qa_sql_patterns[0]["order_by"] == ["ID asc"]
    assert context.context_hash == generated.session.sql_artifacts[-1].generation_context_hash


@pytest.mark.parametrize("narrowing", ["columns", "tables"])
def test_legacy_published_markdown_is_not_sent_to_narrowed_session(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    narrowing: str,
) -> None:
    """旧形式の公開 Markdown は概念単位に絞れないため、範囲を絞った問い合わせには渡さない。"""
    api, _store, legacy = runtime
    markdown = "\n".join(
        [
            "# Confirmed Markdown",
            "",
            "## Q/A SQL 例",
            "```jsonl",
            json.dumps(
                {"question": "受注金額を確認したい", "sql": "SELECT AMOUNT FROM APP.ORDERS"},
                ensure_ascii=False,
            ),
            "```",
            "",
        ]
    )
    base = api.current_ontology().revision
    draft, _artifact = api.create_build_markdown_draft(
        profile_id="sales",
        base_revision_id=base.id,
        payloads=[],
        titles=[],
        markdown=markdown,
        note="legacy scope test",
    )
    api.publish_ontology_revision(
        draft.revision.id,
        OntologyPublishRequest(etag=draft.revision.etag),
    )
    api.copy_draft_markdown_to_published(draft.revision.id)
    if narrowing == "columns":
        allowed = AllowedObjects(table_names=["APP.ORDERS"], columns={"APP.ORDERS": ["ID"]})
    else:
        # Profile は 2 表を許可し、今回は 1 表だけに絞る。
        legacy.profile = legacy.profile.model_copy(
            update={"allowed_tables": ["APP.ORDERS", "APP.SALARY"]}
        )
        allowed = AllowedObjects(table_names=["APP.ORDERS"])

    created = api.create_session(
        QuerySessionApiCreate(
            question="受注金額を確認したい", profile_id="sales", allowed_objects=allowed
        )
    )
    api.generate_sql(created.session.id, _generate_request(created))
    context = legacy.preview_requests[-1].ontology_context

    assert context is not None
    assert context.qa_sql_examples == []


def test_generate_sql_reads_published_qa_sql_patterns_without_fixed_examples(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api, _store, legacy = runtime
    pattern = {
        "pattern_id": "qa_pattern_orders_amount",
        "source_role": "qa_sql_structure",
        "parse_complete": True,
        "statement_type": "select",
        "physical_tables": ["APP.ORDERS"],
        "filters": ["STATUS = 'ACTIVE'"],
        "order_by": ["ID asc"],
        "generation_rules_ja": [
            "使用表候補は APP.ORDERS を優先して確認する。",
            "WHERE/HAVING 条件候補は STATUS = 'ACTIVE' を業務制約として確認する。",
        ],
    }
    markdown = "\n".join(
        [
            "# Confirmed Markdown",
            "",
            "## Q/A 由来 SQL 生成ルール",
            "- 使用表候補は APP.ORDERS を優先して確認する。",
            "",
            "## Q/A SQL 構造パターン",
            "```jsonl",
            json.dumps(pattern, ensure_ascii=False, separators=(",", ":")),
            "```",
            "",
        ]
    )
    base = api.current_ontology().revision
    draft, _artifact = api.create_build_markdown_draft(
        profile_id="sales",
        base_revision_id=base.id,
        payloads=[],
        titles=[],
        markdown=markdown,
        note="QA SQL pattern context test",
    )
    api.publish_ontology_revision(
        draft.revision.id,
        OntologyPublishRequest(etag=draft.revision.etag),
    )
    api.copy_draft_markdown_to_published(draft.revision.id)

    created = api.create_session(
        QuerySessionApiCreate(
            question="受注金額を確認したい",
            profile_id="sales",
            allowed_objects=AllowedObjects(table_names=["APP.ORDERS"]),
        )
    )
    generated = api.generate_sql(created.session.id, _generate_request(created))
    context = legacy.preview_requests[-1].ontology_context

    assert generated.session.status == QuerySessionStatus.AWAITING_SQL_CONFIRMATION
    assert context is not None
    assert context.qa_sql_examples == []
    assert context.qa_sql_patterns[0]["physical_tables"] == ["APP.ORDERS"]
    assert context.qa_sql_patterns[0]["generation_rules_ja"][0].startswith("使用表候補")
    assert "question" not in context.qa_sql_patterns[0]
    assert context.context_hash == generated.session.sql_artifacts[-1].generation_context_hash


def test_stale_session_cache_reloads_store_state_before_execute(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api_a, store, legacy = runtime
    created = api_a.create_session(
        QuerySessionApiCreate(
            question="受注件数を表示",
            profile_id="sales",
            allowed_objects=AllowedObjects(table_names=["APP.ORDERS"]),
        )
    )
    api_b = OntologyApiRuntime(legacy_service=legacy, store=store)
    stale = api_b.get_session(created.session.id)
    assert stale.session.status == QuerySessionStatus.AWAITING_INTENT_CONFIRMATION

    generated = api_a.generate_sql(created.session.id, _generate_request(created))
    confirmation = _confirmation(generated)
    api_a.confirm_sql(created.session.id, confirmation)

    executed = api_b.execute(created.session.id, confirmation)

    assert executed.session.status == QuerySessionStatus.DONE
    assert executed.result.rows == [{"ORDER_COUNT": 3}]


def test_execute_query_session_route_requires_sql_execute_permission(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _store, legacy = runtime
    monkeypatch.setattr(ontology_router_module, "ontology_runtime", api)
    created = api.create_session(
        QuerySessionApiCreate(
            question="受注件数を表示",
            profile_id="sales",
            allowed_objects=AllowedObjects(table_names=["APP.ORDERS"]),
        ),
        # 持ち主の無い session は本人以外に開かない（#1126）。route を呼ぶ利用者の session にする。
        actor_user_uuid="user-1",
    )
    generated = api.generate_sql(created.session.id, _generate_request(created))
    confirmation = _confirmation(generated)
    api.confirm_sql(created.session.id, confirmation)
    payload = SqlBindingRequest(
        session_id=created.session.id,
        confirm_sql=True,
        **confirmation.model_dump(mode="python"),
    )

    with pytest.raises(HTTPException) as denied:
        ontology_router_module.execute_query_session(
            created.session.id,
            payload,
            _request_with_principal(_principal_with_permissions({QUERY_GENERATE_PERMISSION})),
            idempotency_key="route-execute-denied",
        )

    assert denied.value.status_code == 403
    assert "SQL を実行する権限" in str(denied.value.detail)
    assert legacy.executed_sql == []

    permitted = ontology_router_module.execute_query_session(
        created.session.id,
        payload,
        _request_with_principal(
            _principal_with_permissions({QUERY_GENERATE_PERMISSION, SQL_EXECUTE_PERMISSION})
        ),
        idempotency_key="route-execute-permitted",
    )
    assert permitted.data is not None
    assert permitted.data.result.rows == [{"ORDER_COUNT": 3}]
    assert legacy.executed_sql == [generated.session.sql_artifacts[-1].sql]


def test_runtime_rehydrates_complete_query_trace_and_profile_draft_after_restart(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api, store, legacy = runtime
    current_view, source_ontology = api.profile_view("sales")
    table_id = current_view.physical_objects[0].node_id
    updated_view, _ = api.patch_profile_view(
        "sales",
        ProfileOntologyViewPatch(
            base_etag=current_view.etag,
            table_usages_ja={table_id: "再起動後も保持する受注用途"},
            node_overrides=[{"node_id": table_id, "business_name_ja": "受注業務"}],
        ),
    )
    created = api.create_session(
        QuerySessionApiCreate(question="受注件数を表示", profile_id="sales")
    )
    generated = api.generate_sql(
        created.session.id,
        _generate_request(created),
    )
    assert generated.preview is not None
    confirmation = _confirmation(generated)
    api.confirm_sql(created.session.id, confirmation)
    api.execute(created.session.id, confirmation)
    proposal, proposal_data = api.create_proposal(
        created.session.id,
        ImprovementProposalRequest(
            title_ja="受注別名の改善",
            summary="受注語彙を保持",
        ),
    )

    restarted = OntologyApiRuntime(legacy_service=legacy, store=store)
    restored = restarted.get_session(created.session.id)
    restored_view, restored_ontology = restarted.profile_view("sales")

    assert restored.session == proposal_data.session
    assert restored.preview is not None
    assert restored.preview.executable_sql == generated.preview.executable_sql
    assert restored.result is not None
    assert restored.result.rows == [{"ORDER_COUNT": 3}]
    assert restored.performance_check is not None
    assert restored.performance_check.total_cost == 7
    assert restored.ontology_trace_summary["retrieved_node_ids"]
    assert restored.profile_ontology_view.id == created.profile_ontology_view.id
    assert restored_view.etag == updated_view.etag
    assert restored_view.table_usages_ja[table_id] == "再起動後も保持する受注用途"
    assert restored_view.draft_node_overrides[0]["business_name_ja"] == "受注業務"
    assert restored_ontology.revision.id == created.session.ontology_revision_id
    assert {node.id for node in restored_ontology.nodes} == {
        node.id for node in source_ontology.nodes
    }
    assert {edge.id for edge in restored_ontology.edges} == {
        edge.id for edge in source_ontology.edges
    }
    assert restarted.get_proposal(proposal.id) == proposal


def test_rehydrated_revision_is_parent_for_drift_and_preserves_orphan_mapping(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api, store, legacy = runtime
    previous = api.current_ontology()
    previous_view, _ = api.profile_view("sales")
    table_node = next(node for node in previous.nodes if node.kind == OntologyNodeKind.TABLE)
    amount_node = next(
        node
        for node in previous.nodes
        if node.kind == OntologyNodeKind.COLUMN and node.metadata.get("column_name") == "AMOUNT"
    )
    api.patch_profile_view(
        "sales",
        ProfileOntologyViewPatch(
            base_etag=previous_view.etag,
            table_usages_ja={table_node.id: "drift 後も保持する用途"},
            node_overrides=[{"node_id": table_node.id, "business_name_ja": "受注業務"}],
        ),
    )
    business_node = OntologyNode(
        id="business_order_amount",
        revision_id=previous.revision.id,
        kind=OntologyNodeKind.PROPERTY,
        technical_name="order_amount",
        business_name_ja="受注金額",
        physical_mappings=[
            PhysicalMapping(
                object_ref=table_node.physical_mappings[0].object_ref,
                column_refs=[amount_node.physical_mappings[0].column_refs[0]],
            )
        ],
        provenance=OntologyProvenance(source_kind=OntologySourceKind.MANUAL),
        review_status=OntologyReviewStatus.APPROVED,
    )
    store.save_node(
        {
            "revision_id": previous.revision.id,
            "node_id": business_node.id,
            "node_type": business_node.kind.value,
            "review_status": business_node.review_status.value,
            "physical_id": table_node.id,
            "embedding": None,
            "payload": business_node,
        }
    )
    mapping_edge = OntologyEdge(
        id="order_amount_mapping",
        revision_id=previous.revision.id,
        kind=OntologyEdgeKind.MAPS_TO,
        source_node_id=business_node.id,
        target_node_id=amount_node.id,
        relationship_name_ja="金額の物理対応",
        provenance=business_node.provenance,
        review_status=OntologyReviewStatus.APPROVED,
    )
    store.save_edge(
        {
            "revision_id": previous.revision.id,
            "edge_id": mapping_edge.id,
            "edge_type": mapping_edge.kind.value,
            "source_node_id": mapping_edge.source_node_id,
            "target_node_id": mapping_edge.target_node_id,
            "review_status": mapping_edge.review_status.value,
            "payload": mapping_edge,
        }
    )
    source_table = legacy.catalog.tables[0]
    legacy.catalog = legacy.catalog.model_copy(
        update={
            "tables": [
                source_table.model_copy(
                    update={
                        "columns": [
                            column
                            for column in source_table.columns
                            if column.column_name != "AMOUNT"
                        ]
                    }
                )
            ]
        },
        deep=True,
    )

    restarted = OntologyApiRuntime(legacy_service=legacy, store=store)
    drifted = restarted.current_ontology()
    migrated_view, _ = restarted.profile_view("sales")
    preserved = next(node for node in drifted.nodes if node.id == business_node.id)

    assert drifted.revision.version == previous.revision.version + 1
    assert drifted.revision.parent_revision_id == previous.revision.id
    assert preserved.review_status == OntologyReviewStatus.ORPHANED
    assert preserved.metadata["orphaned_mapping_node_ids"] == [amount_node.id]
    assert migrated_view.ontology_revision_id == previous.revision.id
    assert migrated_view.ontology_revision_id != drifted.revision.id
    assert migrated_view.table_usages_ja[table_node.id] == "drift 後も保持する用途"
    assert migrated_view.draft_node_overrides[0]["business_name_ja"] == "受注業務"
    assert migrated_view.draft_schema_fingerprint == ""
    retained_node = next(node for node in drifted.nodes if node.id == amount_node.id)
    retained_edge = next(edge for edge in drifted.edges if edge.id == mapping_edge.id)
    assert retained_node.review_status == OntologyReviewStatus.ORPHANED
    assert retained_edge.review_status == OntologyReviewStatus.ORPHANED
    assert retained_edge.metadata["orphaned_mapping_node_ids"] == [amount_node.id]
    # job の接地確認は、drift の後も job が確定した公開版だけを読む（同期しない。#1168）。
    snapshot = restarted.published_graph_snapshot_for_job(
        profile=legacy.profile,
        allowed=AllowedObjects(table_names=["APP.ORDERS"]),
        release_id=previous.revision.id,
    )
    assert snapshot["revision_id"] == previous.revision.id
    restored = OntologyApiRuntime(legacy_service=legacy, store=store).current_ontology()
    assert restored.revision.id == drifted.revision.id
    assert next(node for node in restored.nodes if node.id == amount_node.id) == retained_node
    assert next(edge for edge in restored.edges if edge.id == mapping_edge.id) == retained_edge


def test_sync_reuses_registered_revision_instead_of_conflict(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    """self._ontology が旧版へ巻き戻っても、既登録の drift revision を再登録せず採用する。

    revision id は (fingerprint, version) の決定論ハッシュのため、現在 revision が旧版 V を
    指したまま再 sync すると evolve は既登録の V+1 と同じ id を生成する。修正前は
    register_revision が REVISION_ALREADY_EXISTS(409)を投げ、AI 構築 job が
    「オントロジー構築に失敗しました」で落ちていた。
    """

    api, _store, legacy = runtime
    previous = api.current_ontology()

    # schema drift を発生させ、版 V+1 の revision を登録する。
    source_table = legacy.catalog.tables[0]
    legacy.catalog = legacy.catalog.model_copy(
        update={
            "tables": [
                source_table.model_copy(
                    update={
                        "columns": [
                            column
                            for column in source_table.columns
                            if column.column_name != "AMOUNT"
                        ]
                    }
                )
            ]
        },
        deep=True,
    )
    drifted = api.current_ontology()
    assert drifted.revision.version == previous.revision.version + 1

    # 現在 revision を旧版 V へ巻き戻す(復元直後などに起きうる状態 divergence を再現)。
    api._ontology = api._ontologies[previous.revision.id]

    # 再 sync しても 409 を投げず、既登録の V+1 revision を採用する(冪等)。
    resynced = api.current_ontology()
    assert resynced.revision.id == drifted.revision.id
    assert resynced.revision.version == drifted.revision.version
    # 自己修復: 現在 revision が V+1 に戻っている。
    assert api._ontology.revision.id == drifted.revision.id


def test_request_scope_is_intersection_and_unknown_profile_never_falls_back(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api, _store, _legacy = runtime
    with pytest.raises(OntologyGateBlockedError) as scope_error:
        api.create_session(
            QuerySessionApiCreate(
                question="受注件数を表示",
                profile_id="sales",
                allowed_objects=AllowedObjects(table_names=["OTHER.SECRETS"]),
            )
        )
    assert scope_error.value.code == "REQUEST_SCOPE_EMPTY"

    with pytest.raises(OntologyNotFoundError) as profile_error:
        api.create_session(
            QuerySessionApiCreate(
                question="受注件数を表示",
                profile_id="unknown",
            )
        )
    assert profile_error.value.code == "NL2SQL_PROFILE_NOT_FOUND"


def test_ontology_embedding_uses_oci_boundary_and_is_persisted_with_nodes(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api, store, legacy = runtime
    legacy._embedding_client = _FakeOntologyEmbeddingClient()  # type: ignore[attr-defined]

    created = api.create_session(
        QuerySessionApiCreate(question="受注件数を表示", profile_id="sales")
    )

    nodes = store.list_nodes(created.session.ontology_revision_id)
    embedded = [item for item in nodes if item.get("embedding")]
    assert embedded
    assert all(len(item["embedding"]) == 1536 for item in embedded)


def test_profile_view_patch_only_updates_business_metadata_and_checks_etag(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api, store, _legacy = runtime
    current, ontology = api.profile_view("sales")
    table_id = current.physical_objects[0].node_id
    column_id = next(
        node.id
        for node in ontology.nodes
        if node.kind.value == "column" and node.id in current.node_ids
    )
    request = ProfileOntologyViewPatch(
        base_etag=current.etag,
        table_usages_ja={table_id: "販売部門の受注件数分析に使用"},
        column_policies={column_id: ColumnQueryPolicy(aggregatable=True)},
        allowed_path_ids=[],
    )

    updated, _ = api.patch_profile_view("sales", request)

    assert updated.etag != current.etag
    assert updated.table_usages_ja[table_id] == "販売部門の受注件数分析に使用"
    assert updated.column_policies[column_id].aggregatable is True
    stored = store.get_profile_view("sales")
    assert stored is not None
    assert stored["payload"]["etag"] == updated.etag
    with pytest.raises(OntologyVersionConflictError) as exc_info:
        api.patch_profile_view("sales", request)
    assert exc_info.value.code == "PROFILE_VIEW_ETAG_MISMATCH"


def test_improvement_proposal_is_bound_to_session_and_persisted(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api, store, _legacy = runtime
    created = api.create_session(
        QuerySessionApiCreate(question="受注件数を表示", profile_id="sales")
    )
    proposal, session_data = api.create_proposal(
        created.session.id,
        ImprovementProposalRequest(
            title_ja="受注の別名追加",
            description_ja="質問で使われる語彙を提案する",
            patch=GraphPatch(
                base_version=1,
                operations=[
                    GraphPatchOperation(
                        op="add",
                        path="/entities/0",
                        value=created.session.intents[-1].entities[0].model_dump(),
                    )
                ],
            ),
        ),
    )

    assert proposal.session_id == created.session.id
    assert proposal.id in session_data.session.proposal_ids
    assert store.get_proposal(proposal.id) is not None


def test_query_session_proposal_rehydrates_without_preloaded_session(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api, store, legacy = runtime
    created = api.create_session(
        QuerySessionApiCreate(question="受注件数を表示", profile_id="sales")
    )
    proposal, _session_data = api.create_proposal(
        created.session.id,
        ImprovementProposalRequest(
            title_ja="受注別名の改善",
            summary="問い合わせから抽出した語彙を改善提案にする",
        ),
    )
    broken = proposal.model_copy(
        update={
            "id": "ontology_proposal_missing_session",
            "session_id": "query_session_missing",
        },
        deep=True,
    )
    store.save_proposal(
        {
            "proposal_id": broken.id,
            "session_id": broken.session_id,
            "ontology_revision_id": broken.base_revision_id,
            "profile_id": broken.profile_id,
            "status": broken.status.value,
            "payload": broken.model_dump(mode="json"),
        }
    )

    get_runtime = OntologyApiRuntime(legacy_service=legacy, store=store)
    list_runtime = OntologyApiRuntime(legacy_service=legacy, store=store)
    accept_runtime = OntologyApiRuntime(legacy_service=legacy, store=store)
    restored = get_runtime.get_proposal(proposal.id)
    listed = list_runtime.list_profile_proposals("sales")
    review = accept_runtime.accept_proposal(proposal.id)

    assert restored.id == proposal.id
    assert {item.id for item in listed} == {proposal.id}
    assert review.proposal.status.value == "accepted"
    assert review.draft is not None


@pytest.mark.asyncio
async def test_http_contract_accepts_frontend_confirmation_and_draft_payloads(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _store, _legacy = runtime
    monkeypatch.setattr(ontology_router_module, "ontology_runtime", api)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created_response = await client.post(
            "/api/nl2sql/query-sessions",
            headers={"Idempotency-Key": "test-create-session"},
            json={
                "question": "受注件数を表示",
                "profile_id": "sales",
                "allowed_objects": {"table_names": ["APP.ORDERS"], "columns": {}},
            },
        )
        assert created_response.status_code == 200
        created = created_response.json()["data"]["session"]

        generated_response = await client.post(
            f"/api/nl2sql/query-sessions/{created['id']}/generate-sql",
            headers={"Idempotency-Key": "test-generate-sql"},
            json={
                "base_version": 1,
                "intent_version": 1,
                "ontology_revision_id": created["ontology_revision_id"],
                "confirm_intent": True,
            },
        )
        assert generated_response.status_code == 200
        generated = generated_response.json()["data"]["session"]
        artifact = generated["sql_artifacts"][-1]
        binding = {
            "session_id": created["id"],
            "artifact_id": artifact["id"],
            "ontology_revision_id": artifact["ontology_revision_id"],
            "intent_version": artifact["intent_version"],
            "sql_hash": artifact["sql_hash"],
            "validation_hash": artifact["validation_report"]["validation_hash"],
            "generation_context_hash": artifact["generation_context_hash"],
            "confirm_sql": True,
        }

        unbound_response = await client.post(
            f"/api/nl2sql/query-sessions/{created['id']}/confirm-sql",
            json={key: value for key, value in binding.items() if key != "session_id"},
        )
        assert unbound_response.status_code == 422

        confirmed_response = await client.post(
            f"/api/nl2sql/query-sessions/{created['id']}/confirm-sql",
            headers={"Idempotency-Key": "test-confirm-sql"},
            json=binding,
        )
        assert confirmed_response.status_code == 200
        executed_response = await client.post(
            f"/api/nl2sql/query-sessions/{created['id']}/execute",
            headers={"Idempotency-Key": "test-execute-sql"},
            json={**binding, "confirm_sql": True},
        )
        assert executed_response.status_code == 200
        assert executed_response.json()["data"]["result"]["rows"] == [{"ORDER_COUNT": 3}]

        view_response = await client.get("/api/nl2sql/profiles/sales/ontology-view")
        view = view_response.json()["data"]["profile_ontology_view"]
        draft_response = await client.patch(
            "/api/nl2sql/profiles/sales/ontology-view",
            json={
                "base_etag": view["etag"],
                "node_overrides": [
                    {
                        "node_id": "business:APP:TABLE:ORDERS",
                        "business_name_ja": "受注",
                        "table_usage": "受注件数の分析",
                    }
                ],
                "edge_overrides": [],
                "schema_fingerprint": "client-draft",
                "physical_scope": {"table_names": ["APP.ORDERS"], "view_names": []},
            },
        )
        assert draft_response.status_code == 200
        assert (
            draft_response.json()["data"]["profile_ontology_view"]["draft_node_overrides"][0][
                "business_name_ja"
            ]
            == "受注"
        )


@pytest.mark.asyncio
async def test_http_ontology_build_persists_current_form_inputs(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api, store, _legacy = runtime
    settings = get_settings()
    monkeypatch.setattr(settings, "local_storage_dir", str(tmp_path))
    monkeypatch.setattr(settings, "upload_storage_backend", "local")
    monkeypatch.setattr(settings, "max_upload_bytes", 1024 * 1024)
    monkeypatch.setattr(settings, "nl2sql_ontology_worker_mode", "external")
    source_storage = OntologySourceStorage(settings)
    build_service = OntologyBuildService(
        api,
        source_storage=source_storage,
    )
    monkeypatch.setattr(ontology_router_module, "ontology_runtime", api)
    monkeypatch.setattr(ontology_router_module, "ontology_source_storage", source_storage)
    monkeypatch.setattr(ontology_router_module, "ontology_build_service", build_service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/nl2sql/profiles/sales/ontology-build",
            headers={"Idempotency-Key": "test-ontology-build-form-inputs"},
            data={
                "business_text": "受注は顧客に紐づく。",
                "run_schema_naming": "true",
                "run_qa_extraction": "true",
                "run_text_extraction": "true",
            },
            files=[
                (
                    "source_files",
                    ("rules.md", "# 受注ルール\n".encode(), "text/markdown"),
                ),
                (
                    "qa_file",
                    (
                        "qa_cases.csv",
                        "QUESTION,SQL\n受注件数,SELECT COUNT(*) FROM APP.ORDERS\n".encode(),
                        "text/csv",
                    ),
                ),
            ],
        )
        list_response = await client.get(
            "/api/nl2sql/profiles/sales/ontology-source-documents?limit=10"
        )

    assert response.status_code == 202
    job = response.json()["data"]["job"]
    assert len(job["source_document_ids"]) == 2
    assert [source["filename"] for source in job["sources"]] == ["rules.md", "qa_cases.csv"]
    persisted = store.get_job(job["id"])
    assert persisted is not None
    assert persisted["input_payload"]["business_text"] == "受注は顧客に紐づく。"
    assert persisted["payload"]["source_document_ids"] == job["source_document_ids"]
    stored_sources = store.list_documents("source_documents", {"profile_id": "sales"})
    assert {document["payload"]["filename"] for document in stored_sources} == {
        "rules.md",
        "qa_cases.csv",
    }
    assert {
        document["payload"]["filename"]: document["payload"]["source_role"]
        for document in stored_sources
    } == {
        "rules.md": "source",
        "qa_cases.csv": "qa",
    }
    assert list_response.status_code == 200
    listed_sources = list_response.json()["data"]["source_documents"]
    assert {document["filename"]: document["source_role"] for document in listed_sources} == {
        "rules.md": "source",
        "qa_cases.csv": "qa",
    }
    assert all("storage_uri" not in document for document in listed_sources)
    assert all("sha256" not in document for document in listed_sources)


@pytest.mark.asyncio
async def test_http_delete_profile_source_document_removes_only_matching_profile_file(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, store, _legacy = runtime

    class _RecordingSourceStorage:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def delete(self, source: OntologySourceDocument) -> None:
            self.deleted.append(source.id)

    source_storage = _RecordingSourceStorage()
    build_service = OntologyBuildService(api, source_storage=source_storage)  # type: ignore[arg-type]
    sales_source = _source_document("ontology_source_delete", "delete-me.md")
    sales_keep = _source_document("ontology_source_keep", "keep-me.md")
    finance_source = _source_document(
        "ontology_source_finance",
        "finance.md",
        profile_id="finance",
    )
    for source in (sales_source, sales_keep, finance_source):
        build_service._save_source_document(source)  # noqa: SLF001 - API cleanup contract setup
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(ontology_router_module, "ontology_runtime", api)
    monkeypatch.setattr(ontology_router_module, "ontology_build_service", build_service)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.delete(
            "/api/nl2sql/profiles/sales/ontology-source-documents/ontology_source_delete"
        )
        cross_profile_response = await client.delete(
            "/api/nl2sql/profiles/sales/ontology-source-documents/ontology_source_finance"
        )

    assert response.status_code == 200
    assert response.json()["data"] == {
        "source_document_id": "ontology_source_delete",
        "deleted": True,
    }
    assert cross_profile_response.status_code == 404
    assert source_storage.deleted == ["ontology_source_delete"]
    assert store.get_document("source_documents", {"source_document_id": sales_source.id}) is None
    assert store.get_document("source_documents", {"source_document_id": sales_keep.id}) is not None
    assert (
        store.get_document("source_documents", {"source_document_id": finance_source.id})
        is not None
    )


@pytest.mark.asyncio
async def test_http_ontology_build_cleans_uploaded_sources_when_start_fails(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _store, _legacy = runtime

    class _RecordingSourceStorage:
        def __init__(self) -> None:
            self.saved: list[OntologySourceDocument] = []

        async def save_upload(
            self,
            *,
            profile_id: str,
            upload: Any,
            source_role: OntologySourceRole = OntologySourceRole.SOURCE,
        ) -> OntologySourceDocument:
            content = await upload.read()
            source = OntologySourceDocument(
                id=f"ontology_source_{len(self.saved) + 1}",
                profile_id=profile_id,
                filename=str(upload.filename or "source.md"),
                media_type=str(upload.content_type or "application/octet-stream"),
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                storage_uri=f"memory://{upload.filename or 'source.md'}",
                status=OntologySourceStatus.STORED,
                source_role=source_role,
            )
            self.saved.append(source)
            return source

    class _FailingBuildService:
        def __init__(self) -> None:
            self.discarded: list[OntologySourceDocument] = []

        def start(self, *_args: Any, **_kwargs: Any) -> None:
            raise OntologyVersionConflictError(
                "IDEMPOTENCY_KEY_REUSED",
                "同じ Idempotency-Key が別の構築リクエストに使用されています。",
            )

        def discard_source_documents(self, sources: Sequence[OntologySourceDocument]) -> int:
            self.discarded.extend(sources)
            return len(sources)

    source_storage = _RecordingSourceStorage()
    build_service = _FailingBuildService()
    monkeypatch.setattr(get_settings(), "auth_mode", "local")
    monkeypatch.setattr(ontology_router_module, "ontology_runtime", api)
    monkeypatch.setattr(ontology_router_module, "ontology_source_storage", source_storage)
    monkeypatch.setattr(ontology_router_module, "ontology_build_service", build_service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/nl2sql/profiles/sales/ontology-build",
            headers={"Idempotency-Key": "test-ontology-build-conflict"},
            data={
                "business_text": "受注は顧客に紐づく。",
                "run_schema_naming": "true",
                "run_qa_extraction": "true",
                "run_text_extraction": "true",
            },
            files=[
                (
                    "source_files",
                    ("rules.md", "# 受注ルール\n".encode(), "text/markdown"),
                )
            ],
        )

    assert response.status_code == 409
    assert "IDEMPOTENCY_KEY_REUSED" in response.json()["error_messages"][0]
    assert [source.id for source in build_service.discarded] == [
        source.id for source in source_storage.saved
    ]


@pytest.mark.asyncio
async def test_http_ontology_build_idempotent_replay_discards_new_uploads(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, store, _legacy = runtime

    class _RecordingSourceStorage:
        def __init__(self) -> None:
            self.saved: list[OntologySourceDocument] = []
            self.deleted: list[str] = []

        async def save_upload(
            self,
            *,
            profile_id: str,
            upload: Any,
            source_role: OntologySourceRole = OntologySourceRole.SOURCE,
        ) -> OntologySourceDocument:
            content = await upload.read()
            source = OntologySourceDocument(
                id=f"ontology_source_{len(self.saved) + 1}",
                profile_id=profile_id,
                filename=str(upload.filename or "source.md"),
                media_type=str(upload.content_type or "application/octet-stream"),
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                storage_uri=f"memory://{upload.filename or 'source.md'}:{len(self.saved) + 1}",
                status=OntologySourceStatus.STORED,
                source_role=source_role,
            )
            self.saved.append(source)
            return source

        def delete(self, source: OntologySourceDocument) -> None:
            self.deleted.append(source.id)

    source_storage = _RecordingSourceStorage()
    build_service = OntologyBuildService(api, source_storage=source_storage)  # type: ignore[arg-type]
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "nl2sql_ontology_worker_mode", "external")
    monkeypatch.setattr(ontology_router_module, "ontology_runtime", api)
    monkeypatch.setattr(ontology_router_module, "ontology_source_storage", source_storage)
    monkeypatch.setattr(ontology_router_module, "ontology_build_service", build_service)

    async def post_build() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/api/nl2sql/profiles/sales/ontology-build",
                headers={"Idempotency-Key": "test-ontology-build-replay"},
                data={
                    "business_text": "受注は顧客に紐づく。",
                    "run_schema_naming": "true",
                    "run_qa_extraction": "true",
                    "run_text_extraction": "true",
                },
                files=[
                    (
                        "source_files",
                        ("rules.md", "# 受注ルール\n".encode(), "text/markdown"),
                    )
                ],
            )

    first = await post_build()
    second = await post_build()

    assert first.status_code == 202
    assert second.status_code == 202
    first_job = first.json()["data"]["job"]
    second_job = second.json()["data"]["job"]
    assert second_job["id"] == first_job["id"]
    assert [source.id for source in source_storage.saved] == [
        "ontology_source_1",
        "ontology_source_2",
    ]
    assert source_storage.deleted == ["ontology_source_2"]
    stored_sources = store.list_documents("source_documents", {"profile_id": "sales"})
    assert [document["source_document_id"] for document in stored_sources] == ["ontology_source_1"]


@pytest.mark.asyncio
async def test_http_ontology_build_replay_race_discards_uploads_not_used_by_returned_job(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """同じ Idempotency-Key の先行リクエストが upload 中に start を終えても、
    再送側の資料を残さない。"""

    api, store, _legacy = runtime
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "local_storage_dir", str(tmp_path))
    monkeypatch.setattr(settings, "upload_storage_backend", "local")
    monkeypatch.setattr(settings, "max_upload_bytes", 1024 * 1024)
    monkeypatch.setattr(settings, "nl2sql_ontology_worker_mode", "external")
    source_storage = OntologySourceStorage(settings)
    build_service = OntologyBuildService(api, source_storage=source_storage)
    original_save_upload = source_storage.save_upload
    first_request: dict[str, Any] = {}

    async def save_upload_racing_first_request(**kwargs: Any) -> OntologySourceDocument:
        document = await original_save_upload(**kwargs)
        if not first_request:
            # 先行リクエスト(同じ Idempotency-Key)が、この upload 中に job 投入を完了する
            first_request["job"] = build_service.start(
                "sales",
                source_documents=[
                    document.model_copy(
                        update={"id": "ontology_source_first", "storage_uri": "memory://first.md"}
                    )
                ],
                idempotency_key="test-ontology-build-replay-race",
            )
        return document

    monkeypatch.setattr(source_storage, "save_upload", save_upload_racing_first_request)
    monkeypatch.setattr(ontology_router_module, "ontology_runtime", api)
    monkeypatch.setattr(ontology_router_module, "ontology_source_storage", source_storage)
    monkeypatch.setattr(ontology_router_module, "ontology_build_service", build_service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/nl2sql/profiles/sales/ontology-build",
            headers={"Idempotency-Key": "test-ontology-build-replay-race"},
            # 画面と同じく 3 つの工程の指定を送る（送らない bool の form は False）。
            data={
                "run_schema_naming": "true",
                "run_qa_extraction": "true",
                "run_text_extraction": "true",
            },
            files=[("source_files", ("rules.md", "# 受注ルール\n".encode(), "text/markdown"))],
        )

    assert response.status_code == 202
    job = response.json()["data"]["job"]
    assert job["id"] == first_request["job"].id
    assert job["source_document_ids"] == ["ontology_source_first"]
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []
    stored_sources = store.list_documents("source_documents", {"profile_id": "sales"})
    assert [document["source_document_id"] for document in stored_sources] == [
        "ontology_source_first"
    ]


@pytest.mark.asyncio
async def test_http_ontology_build_discards_earlier_uploads_when_later_file_is_invalid(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """複数ファイルのうち後続が不正なとき、先に保存した資料の実体を孤児にしない。"""

    api, store, _legacy = runtime
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "local_storage_dir", str(tmp_path))
    monkeypatch.setattr(settings, "upload_storage_backend", "local")
    monkeypatch.setattr(settings, "max_upload_bytes", 1024 * 1024)
    monkeypatch.setattr(settings, "nl2sql_ontology_worker_mode", "external")
    source_storage = OntologySourceStorage(settings)
    monkeypatch.setattr(ontology_router_module, "ontology_runtime", api)
    monkeypatch.setattr(ontology_router_module, "ontology_source_storage", source_storage)
    monkeypatch.setattr(
        ontology_router_module,
        "ontology_build_service",
        OntologyBuildService(api, source_storage=source_storage),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/nl2sql/profiles/sales/ontology-build",
            headers={"Idempotency-Key": "test-ontology-build-invalid-second-file"},
            files=[
                ("source_files", ("ok.md", "# 受注ルール\n".encode(), "text/markdown")),
                ("source_files", ("bad.pdf", b"not a pdf", "application/pdf")),
            ],
        )

    assert response.status_code == 400
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []
    assert store.list_documents("source_documents", {"profile_id": "sales"}) == []


@pytest.mark.asyncio
async def test_http_ontology_build_rejects_too_many_source_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _UnexpectedSourceStorage:
        def __init__(self) -> None:
            self.calls = 0

        async def save_upload(self, **_kwargs: Any) -> None:
            self.calls += 1
            raise AssertionError("source files should be rejected before storage")

    source_storage = _UnexpectedSourceStorage()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(ontology_router_module, "ontology_source_storage", source_storage)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/nl2sql/profiles/sales/ontology-build",
            headers={"Idempotency-Key": "test-ontology-build-too-many-sources"},
            data={
                "business_text": "受注は顧客に紐づく。",
                "run_schema_naming": "true",
                "run_qa_extraction": "true",
                "run_text_extraction": "true",
            },
            files=[
                (
                    "source_files",
                    (f"source-{index}.md", f"# 資料 {index}\n".encode(), "text/markdown"),
                )
                for index in range(ontology_router_module.ONTOLOGY_SOURCE_FILE_MAX_COUNT + 1)
            ],
        )

    assert response.status_code == 400
    payload = response.json()
    assert "ONTOLOGY_SOURCE_FILE_COUNT_EXCEEDED" in payload["error_messages"][0]
    assert "最大 5 件" in payload["error_messages"][0]
    assert source_storage.calls == 0


@pytest.mark.asyncio
async def test_ontology_proposals_does_not_block_unrelated_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "environment", "local")
    monkeypatch.setattr(settings, "auth_mode", "local")

    started = threading.Event()
    release = threading.Event()

    class _BlockingOntologyRuntime:
        def list_profile_proposals(self, profile_id: str) -> list[Any]:
            assert profile_id == "sales"
            started.set()
            release.wait(timeout=1.0)
            return []

    monkeypatch.setattr(
        ontology_router_module,
        "ontology_runtime",
        _BlockingOntologyRuntime(),
    )
    timer = threading.Timer(0.5, release.set)
    timer.start()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        proposal_task = asyncio.create_task(
            client.get("/api/nl2sql/profiles/sales/ontology-proposals")
        )
        try:
            started_at = time.perf_counter()
            deadline = started_at + 1.0
            while not started.is_set() and time.perf_counter() < deadline:
                await asyncio.sleep(0.01)
            assert started.is_set()
            assert time.perf_counter() - started_at < 0.2

            permissions_response = await asyncio.wait_for(
                client.get("/api/security/permissions"),
                timeout=0.2,
            )
            assert permissions_response.status_code == 200
        finally:
            release.set()
            timer.cancel()
            timer.join(timeout=1.0)
        proposal_response = await asyncio.wait_for(proposal_task, timeout=1.0)
        assert proposal_response.status_code == 200
        assert proposal_response.json()["data"]["proposals"] == []


def test_pure_physical_published_follows_schema_drift_automatically(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    """空カタログで固定された純物理 published が、カタログ更新後に自動で追従する。"""

    api, _store, legacy = runtime
    populated_catalog = legacy.catalog
    legacy.catalog = SchemaCatalog(refreshed_at="2026-07-12T00:00:00Z", tables=[])

    empty_view, empty_ontology = api.profile_view("sales")
    assert empty_view.node_ids == []
    assert empty_ontology.revision.status.value == "published"

    # スキーマ refresh 相当: カタログにテーブルが入る
    legacy.catalog = populated_catalog
    view, ontology = api.profile_view("sales")

    assert ontology.revision.status.value == "published"
    assert ontology.revision.id != empty_ontology.revision.id
    assert any(node.kind == OntologyNodeKind.TABLE for node in ontology.nodes)
    assert view.node_ids  # APP.ORDERS が解決される
    assert any(item.object_name == "ORDERS" for item in view.physical_objects)


def test_business_definitions_keep_pinning_after_auto_follow_fix(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    """業務定義を含む published は schema drift しても自動置換されない(pin 仕様の回帰)。"""

    api, _store, legacy = runtime
    base = api.current_ontology()
    published_base = api.publish_ontology_revision(
        base.revision.id,
        OntologyPublishRequest(etag=base.revision.etag),
    )
    table_node = next(node for node in base.nodes if node.kind == OntologyNodeKind.TABLE)
    business_node = OntologyNode(
        id="business_orders_pin",
        revision_id=base.revision.id,
        kind=OntologyNodeKind.BUSINESS_ENTITY,
        technical_name="orders",
        business_name_ja="受注",
        physical_mappings=[PhysicalMapping(object_ref=table_node.physical_mappings[0].object_ref)],
        provenance=OntologyProvenance(source_kind=OntologySourceKind.MANUAL),
        review_status=OntologyReviewStatus.APPROVED,
    )
    draft = api.create_ontology_draft(
        published_base.revision.id,
        OntologyDraftRequest(base_etag=published_base.revision.etag, node_upserts=[business_node]),
    )
    published_with_business = api.publish_ontology_revision(
        draft.revision.id,
        OntologyPublishRequest(etag=draft.revision.etag),
    )

    legacy.catalog = legacy.catalog.model_copy(
        update={
            "tables": [
                legacy.catalog.tables[0].model_copy(
                    update={
                        "columns": [
                            *legacy.catalog.tables[0].columns,
                            SchemaColumn(
                                column_name="UPDATED_AT",
                                logical_name="更新日時",
                                data_type="TIMESTAMP",
                            ),
                        ]
                    }
                )
            ]
        },
        deep=True,
    )
    _view, active = api.profile_view("sales")
    assert active.revision.id == published_with_business.revision.id


def test_profile_view_warnings_report_unresolved_objects(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api, _store, legacy = runtime
    legacy.profile = legacy.profile.model_copy(
        update={"allowed_tables": ["APP.ORDERS", "APP.MISSING_TABLE"]}
    )

    view, _ontology = api.profile_view("sales")
    warnings = api.profile_view_warnings("sales", view)

    assert len(warnings) == 1
    assert "APP.MISSING_TABLE" in warnings[0]
    assert "公開済みオントロジー(スキーマ情報)" in warnings[0]


def test_profile_recommendation_requires_explicit_confirmation_and_binds_token(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _store, _legacy = runtime
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_profile_confirmation_required", True)
    recommendation = api.recommend_profiles(
        OntologyProfileRecommendationRequest(question="販売分析で受注件数を表示")
    )
    assert recommendation.candidates[0].profile_id == "sales"

    with pytest.raises(OntologyGateBlockedError) as missing_confirmation:
        api.create_session(
            QuerySessionApiCreate(question="販売分析で受注件数を表示", profile_id="sales")
        )
    assert missing_confirmation.value.code == "PROFILE_CONFIRMATION_REQUIRED"

    with pytest.raises(OntologyVersionConflictError):
        api.confirm_profile_recommendation(
            recommendation.id,
            ProfileRecommendationConfirmationRequest(
                selected_profile_id="sales",
                selected_revision_id="stale-revision",
            ),
        )
    confirmed, token = api.confirm_profile_recommendation(
        recommendation.id,
        ProfileRecommendationConfirmationRequest(
            selected_profile_id="sales",
            selected_revision_id=recommendation.ontology_revision_id,
        ),
    )
    assert confirmed.selected_profile_id == "sales"
    assert token

    created = api.create_session(
        QuerySessionApiCreate(
            question="販売分析で受注件数を表示",
            profile_id="sales",
            profile_confirmation_token=token,
        )
    )
    assert created.session.profile_id == "sales"
    persisted = api.store.get_document("query_sessions", {"session_id": created.session.id})
    assert persisted is not None
    assert persisted["runtime_context"]["profile_selection_source"] == "confirmed"

    with pytest.raises(OntologyGateBlockedError):
        api.create_session(
            QuerySessionApiCreate(
                question="同じ token を別の質問に使用",
                profile_id="sales",
                profile_confirmation_token=token,
            )
        )


def test_zero_match_recommendation_can_be_confirmed_manually(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _store, _legacy = runtime
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_profile_confirmation_required", True)
    recommendation = api.recommend_profiles(
        OntologyProfileRecommendationRequest(question="完全に無関係な質問")
    )
    assert recommendation.candidates == []
    confirmed, token = api.confirm_profile_recommendation(
        recommendation.id,
        ProfileRecommendationConfirmationRequest(
            selected_profile_id="sales",
            selected_revision_id=recommendation.ontology_revision_id,
        ),
    )
    assert confirmed.selected_profile_id == "sales"
    assert token


def test_profile_recommendation_uses_existing_oci_embedding_boundary(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api, _store, legacy = runtime

    class _MatchingEmbeddingClient:
        def is_configured(self) -> bool:
            return True

        def embed_texts(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0, 0.0] for _text in texts]

    legacy._embedding_client = _MatchingEmbeddingClient()  # type: ignore[attr-defined]
    recommendation = api.recommend_profiles(
        OntologyProfileRecommendationRequest(question="語彙一致のない意味検索")
    )
    assert recommendation.candidates[0].profile_id == "sales"
    assert any("意味類似度" in reason for reason in recommendation.candidates[0].reasons_ja)


def test_context_search_is_revision_pinned_profile_scoped_and_deterministic(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api, _store, _legacy = runtime
    view, ontology = api.profile_view("sales")
    request = OntologyContextSearchRequest(
        question="受注金額",
        ontology_revision_id=ontology.revision.id,
        top_k=8,
        max_hops=2,
    )
    first = api.search_ontology_context("sales", request)
    second = api.search_ontology_context("sales", request)

    assert first.context_hash == second.context_hash
    assert {node.id for node in first.nodes} <= set(view.node_ids)
    assert {edge.id for edge in first.edges} <= set(view.edge_ids)
    assert all(edge.review_status == OntologyReviewStatus.APPROVED for edge in first.edges)
    assert first.ontology_revision_id == ontology.revision.id
    assert ontology.revision.id in first.llm_markdown

    with pytest.raises(OntologyVersionConflictError):
        api.search_ontology_context(
            "sales",
            request.model_copy(update={"ontology_revision_id": "stale-revision"}),
        )


def test_inferred_context_expansion_never_leaves_profile_view(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api, store, _legacy = runtime
    view, ontology = api.profile_view("sales")
    source_id, target_id = view.node_ids[:2]
    outside_id = "outside-profile-node"
    store.save_artifact(
        {
            "artifact_id": "inferred-context-test",
            "session_id": ontology.revision.id,
            "artifact_type": "ontology_inferred_turtle",
            "content_hash": "hash",
            "created_at": "2026-07-19T00:00:00Z",
            "content": (
                "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
                f"<urn:nl2sql:ontology:node:{source_id}> rdfs:subClassOf "
                f"<urn:nl2sql:ontology:node:{target_id}> .\n"
                f"<urn:nl2sql:ontology:node:{target_id}> rdfs:subClassOf "
                f"<urn:nl2sql:ontology:node:{outside_id}> .\n"
            ),
        }
    )
    expanded = api._inferred_context_node_ids(
        "sales",
        ontology.revision.id,
        {source_id},
        allowed_node_ids=set(view.node_ids),
        max_hops=3,
    )
    assert target_id in expanded
    assert outside_id not in expanded

    store.save_artifact(
        {
            "artifact_id": "inferred-context-encoded-id",
            "session_id": ontology.revision.id,
            "artifact_type": "ontology_inferred_turtle",
            "content_hash": "hash-encoded",
            "created_at": "2026-07-19T00:00:01Z",
            "content": (
                "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
                "<urn:nl2sql:ontology:node:node%3Asource> rdfs:subClassOf "
                "<urn:nl2sql:ontology:node:node%3Atarget> .\n"
            ),
        }
    )
    assert api._inferred_context_node_ids(
        "sales",
        ontology.revision.id,
        {"node:source"},
        allowed_node_ids={"node:source", "node:target"},
        max_hops=1,
    ) == {"node:target"}


def test_async_semantic_publish_succeeds_and_is_idempotent(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, store, _legacy = runtime
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    base = api.current_ontology().revision
    confirmed_markdown = "# Confirmed Markdown\n\n- SQL 生成に使う公開済み語彙"
    draft, _artifact = api.create_build_markdown_draft(
        profile_id="sales",
        base_revision_id=base.id,
        payloads=[],
        titles=[],
        markdown=confirmed_markdown,
        note="publish markdown copy test",
    )
    revision = draft.revision
    publisher = OntologyPublishService(api)

    queued = publisher.start(revision.id, etag=revision.etag, idempotency_key="publish-1")
    joined = publisher.start(revision.id, etag=revision.etag, idempotency_key="publish-2")
    duplicate = publisher.start(revision.id, etag=revision.etag, idempotency_key="publish-1")
    assert joined.id == queued.id
    assert duplicate.id == queued.id
    finished = publisher.run_persisted(queued.id)

    assert finished.status.value == "succeeded"
    assert finished.requested_etag == revision.etag
    assert finished.shacl_conforms is True
    published = api.ontology_revision(revision.id)
    assert published.revision.status.value == "published"
    assert published.revision.reasoning_status.value == "ready"
    assert published.revision.rdf_graph_name.startswith("ONT_")
    artifacts = store.list_documents("artifacts", {"session_id": revision.id})
    assert {item["artifact_type"] for item in artifacts} >= {
        "ontology_owl_turtle",
        "ontology_shacl_turtle",
        "ontology_llm_markdown",
        "ontology_markdown_published",
        "ontology_shacl_report",
    }
    by_type = {item["artifact_type"]: item for item in artifacts}
    assert by_type["ontology_llm_markdown"]["content"] == confirmed_markdown
    assert by_type["ontology_markdown_published"]["content"] == confirmed_markdown
    assert (
        api.published_markdown_for_revision(revision.id, profile_id="sales") == confirmed_markdown
    )
    state = api.ontology_markdown_state("sales")
    assert state.draft_revision is not None
    assert state.draft_revision.id == revision.id
    assert state.draft_revision.status == OntologyRevisionStatus.PUBLISHED
    assert state.draft_version == 1
    assert state.draft_markdown == confirmed_markdown
    assert state.published_revision is not None
    assert state.published_revision.id == revision.id
    assert state.published_version == 1
    assert state.published_markdown == confirmed_markdown
    with pytest.raises(OntologyStateConflictError):
        api.save_ontology_markdown_draft(
            "sales",
            OntologyMarkdownDraftPatch(
                markdown="# Published revision must stay immutable",
                base_etag=state.draft_etag,
            ),
        )
    with pytest.raises(OntologyStateConflictError):
        publisher.start(revision.id, etag=revision.etag, idempotency_key="publish-after-ready")
    ignored = api.update_reasoning_status(revision.id, OntologyReasoningStatus.FAILED)
    assert ignored.revision.reasoning_status.value == "ready"


def test_profile_scoped_semantic_publish_does_not_publish_other_profile_markdown(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, store, _legacy = runtime
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    base = api.current_ontology().revision
    sales_markdown = "# Sales Markdown\n\n- 販売 profile 専用"
    finance_markdown = "# Finance Markdown\n\n- 経理 profile 専用"
    draft, _artifact = api.create_build_markdown_draft(
        profile_id="sales",
        base_revision_id=base.id,
        payloads=[],
        titles=[],
        markdown=sales_markdown,
        note="profile scoped publish",
    )
    api._save_markdown_artifact(  # noqa: SLF001 - profile-scoped fixture
        profile_id="finance",
        revision=draft.revision,
        artifact_type="ontology_markdown_draft",
        markdown=finance_markdown,
        profile_version=1,
    )
    publisher = OntologyPublishService(api)

    queued = publisher.start(
        draft.revision.id,
        etag=draft.revision.etag,
        idempotency_key="publish-sales-only",
        profile_id="sales",
    )
    duplicate = publisher.start(
        draft.revision.id,
        etag=draft.revision.etag,
        idempotency_key="publish-sales-only-2",
        profile_id="sales",
    )
    finished = publisher.run_persisted(queued.id)

    assert duplicate.id == queued.id
    assert queued.profile_id == "sales"
    assert finished.profile_id == "sales"
    assert finished.status.value == "succeeded"
    assert (
        api.published_markdown_for_revision(
            draft.revision.id,
            profile_id="sales",
        )
        == sales_markdown
    )
    assert (
        api.published_markdown_for_revision(
            draft.revision.id,
            profile_id="finance",
        )
        == ""
    )
    artifacts = store.list_documents("artifacts", {"session_id": draft.revision.id})
    published = [
        item for item in artifacts if item["artifact_type"] == "ontology_markdown_published"
    ]
    assert {
        api._artifact_profile_id(item): item["content"]  # noqa: SLF001 - persisted scope assertion
        for item in published
    } == {"sales": sales_markdown}
    llm_markdown = [item for item in artifacts if item["artifact_type"] == "ontology_llm_markdown"]
    assert {api._artifact_profile_id(item) for item in llm_markdown} == {"sales"}  # noqa: SLF001


def test_oracle_store_publish_uses_local_owl2rl_and_copies_markdown(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, store, _legacy = runtime
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_ontology_worker_mode", "external")
    monkeypatch.setattr(store, "mode", "oracle", raising=False)
    base = api.current_ontology().revision
    confirmed_markdown = "# Local OWL2RL Markdown\n\n- local OWL2RL で公開する"
    draft, _artifact = api.create_build_markdown_draft(
        profile_id="sales",
        base_revision_id=base.id,
        payloads=[],
        titles=[],
        markdown=confirmed_markdown,
        note="oracle local fallback publish markdown copy test",
    )
    publisher = OntologyPublishService(api)

    queued = publisher.start(
        draft.revision.id,
        etag=draft.revision.etag,
        idempotency_key="publish-oracle-local-fallback",
    )
    finished = publisher.run_persisted(queued.id)

    assert finished.status.value == "succeeded"
    assert api.ontology_revision(draft.revision.id).revision.reasoning_status.value == "ready"
    assert (
        api.published_markdown_for_revision(draft.revision.id, profile_id="sales")
        == confirmed_markdown
    )


def test_publish_validates_review_gate_before_materialization(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _store, _legacy = runtime
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    base = api.current_ontology()
    proposed = OntologyNode(
        id="unreviewed-term",
        revision_id=base.revision.id,
        kind=OntologyNodeKind.BUSINESS_TERM,
        business_name_ja="未確認用語",
        provenance=OntologyProvenance(source_kind=OntologySourceKind.MANUAL),
        review_status=OntologyReviewStatus.PROPOSED,
    )
    draft = api.create_ontology_draft(
        base.revision.id,
        OntologyDraftRequest(
            base_etag=base.revision.etag,
            node_upserts=[proposed],
        ),
    )
    materialized = False

    class _Materializer:
        def materialize(self, **_values: Any) -> str:
            nonlocal materialized
            materialized = True
            return ""

    publisher = OntologyPublishService(api, materializer=_Materializer())
    queued = publisher.start(
        draft.revision.id,
        etag=draft.revision.etag,
        idempotency_key="publish-review-gate",
    )
    finished = publisher.run_persisted(queued.id)

    assert finished.status.value == "failed"
    assert materialized is False
    assert api.ontology_revision(draft.revision.id).revision.reasoning_status.value == "failed"


def test_publish_can_skip_shacl_only_with_explicit_rollout_flag(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _store, _legacy = runtime
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_shacl_enabled", False)
    revision = api.current_ontology().revision
    publisher = OntologyPublishService(api)

    queued = publisher.start(
        revision.id,
        etag=revision.etag,
        idempotency_key="publish-shacl-rollout",
    )
    finished = publisher.run_persisted(queued.id)

    assert finished.status.value == "succeeded"
    assert finished.shacl_conforms is None
    assert finished.warnings_ja == ["段階導入設定により SHACL Core 検証をスキップしました。"]


def test_publish_materialization_failure_keeps_previous_revision_active(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, _store, _legacy = runtime
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    base = api.current_ontology()
    active = api.publish_ontology_revision(
        base.revision.id,
        OntologyPublishRequest(etag=base.revision.etag),
    )
    draft = api.create_ontology_draft(
        active.revision.id,
        OntologyDraftRequest(base_etag=active.revision.etag, note="失敗確認"),
    )

    class _FailingMaterializer:
        def materialize(self, **_values: Any) -> str:
            raise RuntimeError("Oracle OWL2RL unavailable")

    publisher = OntologyPublishService(api, materializer=_FailingMaterializer())
    queued = publisher.start(
        draft.revision.id,
        etag=draft.revision.etag,
        idempotency_key="publish-failure",
    )
    finished = publisher.run_persisted(queued.id)

    assert finished.status.value == "failed"
    assert finished.error_code == "ONTOLOGY_PUBLISH_FAILED"
    _view, still_active = api.profile_view("sales")
    assert still_active.revision.id == active.revision.id
    failed_revision = api.ontology_revision(draft.revision.id).revision
    assert failed_revision.status.value == "draft"
    assert failed_revision.reasoning_status.value == "failed"


def test_external_publish_worker_loads_persisted_draft_revision(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, store, legacy = runtime
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    base = api.current_ontology()
    published = api.publish_ontology_revision(
        base.revision.id,
        OntologyPublishRequest(etag=base.revision.etag),
    )
    draft = api.create_ontology_draft(
        published.revision.id,
        OntologyDraftRequest(base_etag=published.revision.etag, note="cold worker publish"),
    )

    queued = OntologyPublishService(api).start(
        draft.revision.id,
        etag=draft.revision.etag,
        idempotency_key="publish-cold-worker",
    )
    cold_runtime = OntologyApiRuntime(legacy_service=legacy, store=store)
    finished = OntologyPublishService(cold_runtime).run_persisted(queued.id)

    assert finished.status.value == "succeeded"
    revision = cold_runtime.ontology_revision(draft.revision.id).revision
    assert revision.status.value == "published"
    assert revision.reasoning_status.value == "ready"


def test_publish_refreshes_persisted_published_header_before_atomic_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_profile_confirmation_required", False)
    store = _OnePublishedEnforcingStore()
    legacy = _FakeLegacyNl2SqlService()
    api = OntologyApiRuntime(legacy_service=legacy, store=store)
    base = api.current_ontology()
    published = api.publish_ontology_revision(
        base.revision.id,
        OntologyPublishRequest(etag=base.revision.etag),
    )
    draft = api.create_ontology_draft(
        published.revision.id,
        OntologyDraftRequest(base_etag=published.revision.etag, note="reload published header"),
    )

    stale_runtime = OntologyApiRuntime(legacy_service=legacy, store=store)
    cached_draft = stale_runtime.ontology_revision(draft.revision.id)
    # 別 worker 起動直後など、draft だけを読んだ cache で published 読込済み扱いに
    # なっている状態でも、publish switch 直前に store から active header を再読込する。
    stale_runtime._ontology = cached_draft
    stale_runtime._published_revision_loaded = True

    updated = stale_runtime.publish_ontology_revision(
        draft.revision.id,
        OntologyPublishRequest(etag=draft.revision.etag),
    )

    assert updated.revision.status.value == "published"
    old_document = store.get_document("revisions", {"revision_id": published.revision.id})
    assert old_document is not None
    assert old_document["status"] == OntologyRevisionStatus.ARCHIVED.value


def test_atomic_revision_switch_failure_restores_in_memory_active_revision(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, store, _legacy = runtime
    base = api.current_ontology()
    published = api.publish_ontology_revision(
        base.revision.id,
        OntologyPublishRequest(etag=base.revision.etag),
    )
    draft = api.create_ontology_draft(
        published.revision.id,
        OntologyDraftRequest(base_etag=published.revision.etag, note="atomic failure"),
    )

    def fail_atomic(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("Oracle transaction failed")

    monkeypatch.setattr(store, "save_documents_atomic", fail_atomic)
    with pytest.raises(RuntimeError, match="Oracle transaction failed"):
        api.publish_ontology_revision(
            draft.revision.id,
            OntologyPublishRequest(etag=draft.revision.etag),
        )

    _view, active = api.profile_view("sales")
    assert active.revision.id == published.revision.id
    assert api.ontology_revision(draft.revision.id).revision.status.value == "draft"


def test_create_session_does_not_hold_global_lock_during_llm_call(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    """LLM 解釈(Enterprise AI HTTP)中はグローバルロックを保持しない
    (snapshot → 呼び出し → 再検証・書き戻し)。ハングした LLM 呼び出しが
    他の ontology エンドポイントを塞がないことの契約。"""

    api, _store, _legacy = runtime
    draft = api.current_ontology()
    api.publish_ontology_revision(
        draft.revision.id,
        OntologyPublishRequest(etag=draft.revision.etag),
    )

    llm_started = threading.Event()
    llm_release = threading.Event()

    class _BlockingClient:
        def is_configured(self) -> bool:
            return True

        def model_id(self) -> str:
            return "blocking-fake"

        def generate(self, **_kwargs: object) -> str:
            llm_started.set()
            assert llm_release.wait(timeout=10)
            return "{}"

    api.legacy_service._enterprise_ai_client = _BlockingClient()  # noqa: SLF001

    session_done = threading.Event()

    def create() -> None:
        api.create_session(QuerySessionApiCreate(question="受注件数を表示", profile_id="sales"))
        session_done.set()

    worker = threading.Thread(target=create, daemon=True)
    worker.start()
    try:
        assert llm_started.wait(timeout=5), "LLM 呼び出しに到達しなかった"
        # LLM がブロックしている間も、ロックを要する別 API が完了すること
        other_done = threading.Event()

        def read_revisions() -> None:
            api.list_ontology_revisions()
            other_done.set()

        reader = threading.Thread(target=read_revisions, daemon=True)
        reader.start()
        assert other_done.wait(timeout=2), "LLM 呼び出し中に他の ontology API がブロックされた"
    finally:
        llm_release.set()
    assert session_done.wait(timeout=10)


def test_generate_sql_does_not_hold_global_lock_during_preview(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api, _store, legacy = runtime
    created = api.create_session(
        QuerySessionApiCreate(question="受注件数を表示", profile_id="sales")
    )
    preview_started = threading.Event()
    preview_release = threading.Event()
    original_preview = legacy.preview

    def blocking_preview(request: PreviewRequest) -> PreviewData:
        preview_started.set()
        assert preview_release.wait(timeout=10)
        return original_preview(request)

    legacy.preview = blocking_preview  # type: ignore[method-assign]
    generated: list[Any] = []
    errors: list[BaseException] = []

    def generate() -> None:
        try:
            generated.append(api.generate_sql(created.session.id, _generate_request(created)))
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)

    worker = threading.Thread(target=generate, daemon=True)
    worker.start()
    try:
        assert preview_started.wait(timeout=5), "SQL preview 呼び出しに到達しなかった"
        read_done = threading.Event()
        statuses: list[QuerySessionStatus] = []

        def read_session() -> None:
            statuses.append(api.get_session(created.session.id).session.status)
            read_done.set()

        reader = threading.Thread(target=read_session, daemon=True)
        reader.start()
        assert read_done.wait(timeout=2), "SQL 生成中に他の ontology API がブロックされた"
        assert statuses == [QuerySessionStatus.GENERATING_SQL]
    finally:
        preview_release.set()
    worker.join(timeout=10)
    assert not errors
    assert generated and generated[0].session.status == QuerySessionStatus.AWAITING_SQL_CONFIRMATION


def test_execute_does_not_hold_global_lock_during_sql_execution(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api, _store, legacy = runtime
    created = api.create_session(
        QuerySessionApiCreate(question="受注件数を表示", profile_id="sales")
    )
    generated = api.generate_sql(created.session.id, _generate_request(created))
    confirmation = _confirmation(generated)
    api.confirm_sql(created.session.id, confirmation)
    execution_started = threading.Event()
    execution_release = threading.Event()
    original_execute_sql = legacy.execute_sql

    def blocking_execute_sql(
        sql: str,
        allowed: AllowedObjects,
        row_limit: int | None,
    ) -> tuple[SafetyReport, str, QueryResults]:
        execution_started.set()
        assert execution_release.wait(timeout=10)
        return original_execute_sql(sql, allowed, row_limit)

    legacy.execute_sql = blocking_execute_sql  # type: ignore[method-assign]
    executed: list[Any] = []
    errors: list[BaseException] = []

    def execute() -> None:
        try:
            executed.append(api.execute(created.session.id, confirmation))
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)

    worker = threading.Thread(target=execute, daemon=True)
    worker.start()
    try:
        assert execution_started.wait(timeout=5), "SQL 実行呼び出しに到達しなかった"
        read_done = threading.Event()
        statuses: list[QuerySessionStatus] = []

        def read_session() -> None:
            statuses.append(api.get_session(created.session.id).session.status)
            read_done.set()

        reader = threading.Thread(target=read_session, daemon=True)
        reader.start()
        assert read_done.wait(timeout=2), "SQL 実行中に他の ontology API がブロックされた"
        assert statuses == [QuerySessionStatus.EXECUTING]
    finally:
        execution_release.set()
    worker.join(timeout=10)
    assert not errors
    assert executed and executed[0].session.status == QuerySessionStatus.DONE


def test_execute_without_generated_sql_raises_state_conflict(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    api, _store, _legacy = runtime
    created = api.create_session(
        QuerySessionApiCreate(question="受注件数を表示", profile_id="sales")
    )

    with pytest.raises(OntologyStateConflictError) as exc_info:
        api.execute(
            created.session.id,
            SqlConfirmationRequest(
                artifact_id="missing-artifact",
                ontology_revision_id=created.session.ontology_revision_id,
                intent_version=created.session.current_intent_version,
                sql_hash="missing-sql-hash",
                validation_hash="missing-validation-hash",
                generation_context_hash="missing-context-hash",
            ),
        )

    assert exc_info.value.code == "SQL_ARTIFACT_NOT_GENERATED"


def test_independent_business_release_does_not_override_markdown_context(
    runtime: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    from app.features.nl2sql.ontology_definition_workspace import ProfileOntologyWorkspaceService
    from app.features.nl2sql.ontology_definitions import (
        BusinessDefinition,
        DefinitionMapping,
        ObjectTypeDefinition,
        PropertyDefinition,
    )

    api, _, legacy = runtime
    svc = ProfileOntologyWorkspaceService(api)
    definitions: list[BusinessDefinition] = [
        ObjectTypeDefinition(
            api_name="Contract",
            name_ja="顧客契約",
            primary_key=["Contract.id"],
            properties=["Contract.id"],
            grain_ja="契約",
            mappings=[DefinitionMapping(owner="APP", object_name="ORDERS")],
        ),
        PropertyDefinition(
            api_name="Contract.id",
            name_ja="契約番号",
            object_type="Contract",
            data_type="integer",
            required=True,
            mappings=[DefinitionMapping(owner="APP", object_name="ORDERS", column_name="ID")],
        ),
    ]

    def publish(job: str) -> dict[str, Any]:
        bundle = svc.save_build(
            profile_id="sales",
            job_id=job,
            definitions=definitions,
            schema_fingerprint="schema",
            source_revision_id="legacy",
        )
        bundle = svc.validate("sales", bundle.id, bundle.etag, None)
        bundle = svc.review(
            "sales", bundle.id, bundle.etag, [d.id for d in bundle.definitions], None
        )
        return svc.publish(
            "sales", bundle.id, bundle.etag, svc.head("sales")["release_id"], job, None
        )

    first = publish("first")
    session = api.create_session(QuerySessionApiCreate(profile_id="sales", question="受注を表示"))
    # 旧独立 release は Markdown 正本を上書きしない。
    assert session.session.business_release_id == ""
    second = publish("second")
    assert second["id"] != first["id"]
    api.generate_sql(session.session.id, _generate_request(session))
    context = legacy.preview_requests[-1].ontology_context
    assert context is not None and context.business_release_id == ""
    assert "顧客契約" not in context.llm_markdown
    assert second["id"] not in context.llm_markdown
