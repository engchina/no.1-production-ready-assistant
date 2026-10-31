"""AI オントロジー構築(ontology_build)のテスト。

LLM は fake、store は InMemoryOntologyStore。job → Markdown 下書き → publish と、
既存 proposal endpoint の accept → publish 経路、スコープ外候補の warnings 落ちを検証する。
"""

from __future__ import annotations

import hashlib
import io
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import app.features.nl2sql.ontology_build as ontology_build_module
from app.features.nl2sql.models import (
    Nl2SqlProfile,
    SchemaCatalog,
    SchemaColumn,
    SchemaConstraintDetail,
    SchemaTable,
    SchemaViewDependency,
)
from app.features.nl2sql.ontology_build import (
    OntologyBuildService,
    build_schema_context_from_catalog,
    qa_sql_examples_from_markdown,
    qa_sql_patterns_from_markdown,
    render_ontology_build_markdown,
    select_qa_sql_examples_from_markdown,
)
from app.features.nl2sql.ontology_catalog import SchemaOntology, catalog_schema_fingerprint
from app.features.nl2sql.ontology_definitions import DefinitionPhase
from app.features.nl2sql.ontology_models import (
    BusinessRuleDefinition,
    BusinessRuleExecutionMode,
    BusinessRuleKind,
    BusinessRuleSeverity,
    EnumValueDefinition,
    JoinCondition,
    OntologyBuildJob,
    OntologyBuildStatus,
    OntologyBuildStep,
    OntologyBuildStepName,
    OntologyBuildStepStatus,
    OntologyEdge,
    OntologyEdgeKind,
    OntologyNode,
    OntologyNodeKind,
    OntologyProposalKind,
    OntologyProposalStatus,
    OntologyProvenance,
    OntologyReviewStatus,
    OntologyRevision,
    OntologyRevisionStatus,
    OntologySourceDocument,
    OntologySourceKind,
    OntologySourceRole,
    OntologySourceStatus,
    PhysicalColumnRef,
    PhysicalMapping,
    PhysicalObjectRef,
    ProfileOntologyView,
    QaPair,
    RelationshipCardinality,
)
from app.features.nl2sql.ontology_router import (
    OntologyApiRuntime,
    OntologyDraftRequest,
    OntologyMarkdownDraftPatch,
    OntologyPublishRequest,
)
from app.features.nl2sql.ontology_service import (
    OntologyNotFoundError,
    OntologyStateConflictError,
    OntologyVersionConflictError,
)
from app.features.nl2sql.ontology_store import InMemoryOntologyStore, OntologyVersionConflict
from app.settings import get_settings


class _FakeLegacyNl2SqlService:
    def __init__(self) -> None:
        self._enterprise_ai_client: Any = None
        self.profile = Nl2SqlProfile(
            id="sales",
            name="販売分析",
            allowed_tables=["APP.ORDERS", "APP.CUSTOMERS"],
            default_row_limit=100,
        )
        self.catalog = SchemaCatalog(
            refreshed_at="2026-07-11T00:00:00Z",
            tables=[
                SchemaTable(
                    table_name="ORDERS",
                    logical_name="受注",
                    owner="APP",
                    columns=[
                        SchemaColumn(column_name="ID", logical_name="受注 ID", data_type="NUMBER"),
                        SchemaColumn(
                            column_name="CUSTOMER_ID", logical_name="顧客 ID", data_type="NUMBER"
                        ),
                        SchemaColumn(
                            column_name="AMOUNT", logical_name="受注金額", data_type="NUMBER"
                        ),
                    ],
                    constraint_details=[
                        SchemaConstraintDetail(
                            constraint_name="FK_ORDERS_CUSTOMER",
                            constraint_type="R",
                            owner="APP",
                            table_name="ORDERS",
                            columns=["CUSTOMER_ID"],
                            referenced_owner="APP",
                            referenced_table="CUSTOMERS",
                            referenced_columns=["ID"],
                        )
                    ],
                ),
                SchemaTable(
                    table_name="CUSTOMERS",
                    logical_name="顧客",
                    owner="APP",
                    columns=[
                        SchemaColumn(column_name="ID", logical_name="顧客 ID", data_type="NUMBER"),
                        SchemaColumn(
                            column_name="NAME", logical_name="顧客名", data_type="VARCHAR2"
                        ),
                    ],
                    constraint_details=[
                        SchemaConstraintDetail(
                            constraint_name="PK_CUSTOMERS",
                            constraint_type="P",
                            owner="APP",
                            table_name="CUSTOMERS",
                            columns=["ID"],
                        )
                    ],
                ),
            ],
        )

    def get_catalog(self) -> SchemaCatalog:
        return self.catalog

    def get_profile(self, profile_id: str) -> Nl2SqlProfile:
        if profile_id != self.profile.id:
            raise ValueError("profile not found")
        return self.profile


class _FakeEnterpriseAiClient:
    def __init__(self, payload: str, *, configured: bool = True) -> None:
        self.payload = payload
        self.configured = configured
        self.calls: list[str] = []
        self.contexts: list[str] = []

    def is_configured(self) -> bool:
        return self.configured

    def model_id(self) -> str:
        return "fake-enterprise-ai"

    def generate(
        self,
        *,
        prompt: str,
        context: str,
        system_prompt: str,
        response_format: object | None = None,
    ) -> str:
        self.calls.append(prompt)
        self.contexts.append(context)
        return self.payload


class _FakeOntologySourceStorage:
    def __init__(self, contents: dict[str, bytes]) -> None:
        self.contents = contents
        self.deleted: list[str] = []

    def load(self, document: OntologySourceDocument) -> bytes:
        return self.contents[document.id]

    def delete(self, document: OntologySourceDocument) -> None:
        self.deleted.append(document.id)
        self.contents.pop(document.id, None)


_EXTRACTION = {
    "entities": [
        {
            "object_name": "APP.ORDERS",
            "business_name_ja": "受注",
            "description_ja": "受注トランザクション",
            "aliases": ["注文"],
            "confidence": 0.9,
        },
        {
            "object_name": "APP.SECRET",
            "business_name_ja": "秘密",
            "description_ja": "profile 範囲外",
            "aliases": [],
            "confidence": 0.9,
        },
    ],
    "relationships": [
        {
            "source_object": "APP.ORDERS",
            "target_object": "APP.CUSTOMERS",
            "relationship_name_ja": "顧客を参照",
            "cardinality": "many_to_one",
            "join_conditions": [
                {"left": "APP.ORDERS.CUSTOMER_ID", "right": "APP.CUSTOMERS.ID", "operator": "="}
            ],
            "evidence_ja": "Q/A の JOIN 句",
            "confidence": 0.8,
        }
    ],
    "metrics": [
        {
            "metric_name_ja": "受注金額合計",
            "expression_sql": "SUM(AMOUNT)",
            "aggregation": "sum",
            "base_columns": ["APP.ORDERS.AMOUNT"],
            "unit": "円",
            "description_ja": "受注金額の合計",
            "evidence_ja": "",
            "confidence": 0.7,
        }
    ],
    "synonyms": [{"target": "APP.ORDERS", "aliases": ["オーダー"], "evidence_ja": ""}],
    "warnings_ja": [],
}
_STRUCTURED_PAYLOAD = json.dumps(_EXTRACTION, ensure_ascii=False)

_QA_SQL = (
    "SELECT C.NAME, SUM(O.AMOUNT) FROM APP.ORDERS O "
    "JOIN APP.CUSTOMERS C ON O.CUSTOMER_ID = C.ID GROUP BY C.NAME"
)


def _source_document(
    source_id: str,
    filename: str,
    content: bytes,
    *,
    media_type: str = "text/markdown",
) -> OntologySourceDocument:
    return OntologySourceDocument(
        id=source_id,
        profile_id="sales",
        filename=filename,
        media_type=media_type,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        storage_uri=f"/tmp/{filename}",
    )


# 実行 thread の終了を待つ上限。通常は 1 秒もかからないが、coverage と pytest-xdist で
# CPU を取り合う CI では数倍遅くなる（#1914）。待つのは終了の合図までで、上限は詰まったときの保険。
_JOB_WAIT_TIMEOUT_SECONDS = 120.0

_FINISHED_STATUSES = {
    OntologyBuildStatus.SUCCEEDED,
    OntologyBuildStatus.SUCCEEDED_WITH_WARNINGS,
    OntologyBuildStatus.FAILED,
}


def _wait_for_job(service: OntologyBuildService, job_id: str) -> Any:
    """inprocess の実行 thread の終了（完了の合図）を待ってから結果を読む。

    以前は 10ms × 500 回（約 5 秒）の固定の回数で状態を見ていたため、coverage の下で
    処理が 5 秒を超えると完了前に失敗していた（#1914）。
    """
    deadline = time.monotonic() + _JOB_WAIT_TIMEOUT_SECONDS
    worker_name = ontology_build_module.inprocess_worker_thread_name(job_id)
    for thread in threading.enumerate():
        if thread.name == worker_name:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
    # thread が無い（既に終わった・external の worker）ときも、上限までは状態を見る。
    while True:
        job = service.get(job_id)
        if job is not None and job.status in _FINISHED_STATUSES:
            return job
        if time.monotonic() >= deadline:
            raise AssertionError("ontology build job did not finish")
        time.sleep(0.01)


def _seed_build_proposals(runtime: OntologyApiRuntime) -> list[Any]:
    from app.features.nl2sql.ontology_build import convert_extraction_to_proposals, parse_extraction

    view, ontology = runtime.profile_view("sales")
    drafts, warnings = convert_extraction_to_proposals(
        parse_extraction(_STRUCTURED_PAYLOAD),
        ontology=ontology,
        view=view,
        job_id="seed-job",
        inferred_by="test",
        qa_sql_texts=[_QA_SQL],
    )
    assert warnings
    return [
        runtime.create_build_proposal(
            profile_id="sales",
            job_id="seed-job",
            title_ja=draft.title_ja,
            description_ja=draft.description_ja,
            kind=draft.kind,
            proposal_payload=draft.payload,
            base_revision_id=ontology.revision.id,
        )
        for draft in drafts
    ]


def test_profile_delete_cancels_queued_build_and_worker_cannot_restart_it(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, store, legacy = harness
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    service = OntologyBuildService(runtime)
    started = service.start("sales", business_text="販売業務")

    assert service.cancel_profile_jobs("sales") == 1
    cancelled = service.run_persisted(started.id)

    assert cancelled.status == OntologyBuildStatus.CANCELLED
    assert all(step.status == OntologyBuildStepStatus.SKIPPED for step in cancelled.steps)
    assert store.list_documents("proposals", {"profile_id": "sales"}) == []


@pytest.fixture
def harness() -> tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService]:
    store = InMemoryOntologyStore()
    legacy = _FakeLegacyNl2SqlService()
    return OntologyApiRuntime(legacy_service=legacy, store=store), store, legacy


def test_markdown_state_does_not_show_global_published_revision_before_profile_work(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    runtime, _store, _legacy = harness

    _view, ontology = runtime.profile_view("sales")
    state = runtime.ontology_markdown_state("sales")

    assert ontology.revision.status == OntologyRevisionStatus.PUBLISHED
    assert state.draft_revision is None
    assert state.draft_version is None
    assert state.published_revision is None
    assert state.published_version is None
    assert state.published_markdown == ""


def test_markdown_state_uses_profile_local_versions_for_draft_and_publish(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _store, legacy = harness
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)
    service = OntologyBuildService(runtime)

    first = _wait_for_job(service, service.start("sales", business_text="受注は顧客に紐づく。").id)
    first_state = runtime.ontology_markdown_state("sales")
    assert first_state.draft_revision is not None
    assert first_state.draft_revision.id == first.draft_revision_id
    assert first_state.draft_version == 1

    from test_nl2sql_markdown_unification import Parser
    from test_nl2sql_ontology_workspace import model

    from app.features.nl2sql.ontology_markdown_workspace import (
        MarkdownConfirmRequest,
        MarkdownOntologyWorkspace,
    )
    from app.settings import get_settings

    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    parser = Parser(model())
    first_state = runtime.save_ontology_markdown_draft(
        "sales",
        OntologyMarkdownDraftPatch(
            markdown="受注を受注番号で識別する。", base_etag=first_state.draft_etag
        ),
    )
    parser.lines[0]["end_line"] = len(first_state.draft_markdown.splitlines())
    legacy._enterprise_ai_client = parser
    workspace = MarkdownOntologyWorkspace(runtime)
    prepared = workspace.prepare("sales", first_state.draft_etag, "version-check", None)
    workspace.run_preparation("sales", prepared["id"])
    published = workspace.publish(
        "sales",
        MarkdownConfirmRequest(
            preparation_id=prepared["id"], draft_etag=first_state.draft_etag, confirmed=True
        ),
        "publish-version",
        None,
    )
    published_state = runtime.ontology_markdown_state("sales")
    assert published_state.draft_revision is not None
    assert first_state.draft_revision is not None
    assert published_state.draft_revision.id == first_state.draft_revision.id
    assert published_state.draft_revision.status == OntologyRevisionStatus.DRAFT
    assert published_state.draft_version == 1
    assert published_state.draft_markdown == first_state.draft_markdown
    assert published_state.published_revision is not None
    assert published_state.published_revision.id == published.revision_id
    assert published_state.published_version == 1

    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "inprocess")
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)
    second = _wait_for_job(
        service,
        service.start("sales", business_text="受注は顧客に紐づく。").id,
    )
    second_state = runtime.ontology_markdown_state("sales")

    assert second_state.draft_revision is not None
    assert second_state.draft_revision.id == second.draft_revision_id
    assert second_state.draft_version == 2
    assert second_state.published_version == 1


# --- DB catalog schema context ---------------------------------------------------------------


def test_build_schema_context_from_profile_and_db_catalog_includes_fk_and_views() -> None:
    catalog = SchemaCatalog(
        refreshed_at="2026-07-12T00:00:00Z",
        tables=[
            SchemaTable(
                owner="APP",
                table_name="CUSTOMERS",
                logical_name="顧客",
                columns=[
                    SchemaColumn(column_name="ID", logical_name="顧客 ID", data_type="NUMBER")
                ],
                constraint_details=[
                    SchemaConstraintDetail(
                        constraint_name="PK_CUSTOMERS",
                        constraint_type="P",
                        owner="APP",
                        table_name="CUSTOMERS",
                        columns=["ID"],
                    )
                ],
            ),
            SchemaTable(
                owner="APP",
                table_name="ORDERS",
                logical_name="受注",
                columns=[
                    SchemaColumn(column_name="ID", logical_name="受注 ID", data_type="NUMBER"),
                    SchemaColumn(
                        column_name="CUSTOMER_ID", logical_name="顧客 ID", data_type="NUMBER"
                    ),
                ],
                constraint_details=[
                    SchemaConstraintDetail(
                        constraint_name="FK_ORDERS_CUSTOMER",
                        constraint_type="R",
                        owner="APP",
                        table_name="ORDERS",
                        columns=["CUSTOMER_ID"],
                        referenced_owner="APP",
                        referenced_table="CUSTOMERS",
                        referenced_columns=["ID"],
                    )
                ],
            ),
            SchemaTable(
                owner="APP",
                table_name="V_ORDER_CUSTOMER",
                table_type="view",
                logical_name="受注顧客ビュー",
                columns=[
                    SchemaColumn(column_name="ORDER_ID", logical_name="受注 ID", data_type="NUMBER")
                ],
            ),
        ],
        view_dependencies=[
            SchemaViewDependency(
                owner="APP",
                view_name="V_ORDER_CUSTOMER",
                referenced_owner="APP",
                referenced_name="ORDERS",
                referenced_type="TABLE",
            )
        ],
    )
    profile = Nl2SqlProfile(
        id="sales",
        name="販売分析",
        allowed_tables=["APP.ORDERS", "APP.CUSTOMERS"],
        allowed_views=["APP.V_ORDER_CUSTOMER"],
    )

    prepared = build_schema_context_from_catalog(profile, catalog)
    context = json.loads(prepared.schema_context)

    assert prepared.errors == []
    assert {item["object"] for item in context["objects"]} == {
        "APP.CUSTOMERS",
        "APP.ORDERS",
        "APP.V_ORDER_CUSTOMER",
    }
    assert any(
        relationship["kind"] == "foreign_key"
        and relationship["join_conditions"][0]["expression"]
        == "APP.ORDERS.CUSTOMER_ID = APP.CUSTOMERS.ID"
        for relationship in context["relationships"]
    )
    assert any(
        relationship["kind"] == "view_dependency"
        and relationship["source_object"] == "APP.V_ORDER_CUSTOMER"
        and relationship["target_object"] == "APP.ORDERS"
        for relationship in context["relationships"]
    )


def test_build_schema_context_fails_ambiguous_unqualified_profile_object() -> None:
    catalog = SchemaCatalog(
        refreshed_at="2026-07-12T00:00:00Z",
        tables=[
            SchemaTable(owner="APP", table_name="ORDERS", logical_name="受注"),
            SchemaTable(owner="CRM", table_name="ORDERS", logical_name="CRM 受注"),
        ],
    )
    profile = Nl2SqlProfile(id="sales", name="販売分析", allowed_tables=["ORDERS"])

    prepared = build_schema_context_from_catalog(profile, catalog)

    assert prepared.object_count == 0
    assert any("複数 object" in error for error in prepared.errors)


def test_qa_context_adds_schema_resolved_sql_references(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    _runtime, _store, legacy = harness
    schema_payload = json.loads(
        build_schema_context_from_catalog(legacy.profile, legacy.catalog).schema_context
    )
    sql = (
        "SELECT C.NAME, SUM(O.AMOUNT) AS TOTAL FROM APP.ORDERS O "
        "JOIN APP.CUSTOMERS C ON O.CUSTOMER_ID = C.ID "
        "GROUP BY C.NAME ORDER BY TOTAL DESC"
    )

    context = json.loads(
        ontology_build_module._dump_qa_context(
            schema_payload,
            [QaPair(question="顧客別売上", sql=sql)],
        )
    )
    sent_pair = context["qa_pairs"][0]

    assert sent_pair["schema_unresolved_references"] == []
    assert sent_pair["schema_projection_aliases"] == ["TOTAL"]
    assert sent_pair["schema_ignored_sql_aliases"] == ["TOTAL"]
    assert sent_pair["schema_sql_aliases"] == [
        {"alias": "O", "object": "APP.ORDERS"},
        {"alias": "C", "object": "APP.CUSTOMERS"},
    ]
    assert {"sql": "O.AMOUNT", "column": "APP.ORDERS.AMOUNT", "clause": "select"} in sent_pair[
        "schema_resolved_columns"
    ]
    assert {
        "condition_sql": "O.CUSTOMER_ID = C.ID",
        "resolved_columns": ["APP.CUSTOMERS.ID", "APP.ORDERS.CUSTOMER_ID"],
    } in sent_pair["schema_resolved_join_conditions"]


def test_qa_context_resolves_current_schema_table_and_ignores_output_alias(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    _runtime, _store, legacy = harness
    schema_payload = json.loads(
        build_schema_context_from_catalog(legacy.profile, legacy.catalog).schema_context
    )

    context = json.loads(
        ontology_build_module._dump_qa_context(
            schema_payload,
            [
                QaPair(
                    question="売上合計",
                    sql="SELECT SUM(AMOUNT) AS TOTAL FROM ORDERS ORDER BY TOTAL DESC",
                )
            ],
        )
    )
    sent_pair = context["qa_pairs"][0]

    assert sent_pair["schema_resolved_objects"] == [
        {"sql_table": "ORDERS", "object": "APP.ORDERS", "alias": ""}
    ]
    assert {"sql": "AMOUNT", "column": "APP.ORDERS.AMOUNT", "clause": "select"} in sent_pair[
        "schema_resolved_columns"
    ]
    assert sent_pair["schema_ignored_sql_aliases"] == ["TOTAL"]
    assert sent_pair["schema_unresolved_references"] == []


def test_convert_extraction_resolves_alias_columns_from_qa_sql(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    from app.features.nl2sql.ontology_build import convert_extraction_to_proposals
    from app.features.nl2sql.ontology_models import OntologyBuildExtraction

    runtime, _store, _legacy = harness
    view, ontology = runtime.profile_view("sales")
    extraction = OntologyBuildExtraction.model_validate(
        {
            "relationships": [
                {
                    "source_object": "O",
                    "target_object": "C",
                    "relationship_name_ja": "顧客を参照",
                    "cardinality": "many_to_one",
                    "join_conditions": [{"left": "O.CUSTOMER_ID", "right": "C.ID"}],
                    "evidence_ja": "Q/A SQL の JOIN 句",
                    "confidence": 0.8,
                }
            ],
            "metrics": [
                {
                    "metric_name_ja": "受注金額合計",
                    "expression_sql": "SUM(O.AMOUNT)",
                    "aggregation": "sum",
                    "base_columns": ["O.AMOUNT"],
                    "description_ja": "受注金額の合計",
                    "evidence_ja": "Q/A SQL の SELECT 句",
                    "confidence": 0.8,
                }
            ],
        }
    )

    drafts, warnings = convert_extraction_to_proposals(
        extraction,
        ontology=ontology,
        view=view,
        job_id="job-1",
        inferred_by="test",
        qa_sql_texts=[_QA_SQL],
    )

    assert not any("profile 範囲内に解決できません" in warning for warning in warnings)
    assert {OntologyProposalKind.RELATIONSHIP, OntologyProposalKind.METRIC_DEFINITION} <= {
        draft.kind for draft in drafts
    }


# --- job → Markdown 下書き ---------------------------------------------------------------------


def test_markdown_summarizes_qa_sql_without_fixed_examples() -> None:
    primary_sql = (
        "WITH CURRENT_FACTS AS ("
        "SELECT A.RESOURCE_ID, SUM(A.AMOUNT) AS TOTAL_AMOUNT "
        "FROM APP.FACT_VALUES A WHERE A.STATUS_CODE = 'ACTIVE' GROUP BY A.RESOURCE_ID"
        ") SELECT A.RESOURCE_ID, B.RESOURCE_NAME, A.TOTAL_AMOUNT "
        "FROM CURRENT_FACTS A, APP.RESOURCES B "
        "WHERE B.RESOURCE_ID = A.RESOURCE_ID ORDER BY A.RESOURCE_ID"
    )
    markdown = render_ontology_build_markdown(
        profile_id="HR",
        schema_context=json.dumps({"objects": [], "relationships": []}),
        drafts=[],
        warnings=[],
        source_count=1,
        qa_pair_count=2,
        business_text_present=False,
        qa_pairs=[
            QaPair(question="有効な金額集計を確認したい", sql=primary_sql),
            QaPair(question="リソース一覧", sql="SELECT RESOURCE_ID FROM APP.RESOURCES"),
            QaPair(question="有効な金額集計を確認したい", sql=primary_sql),
        ],
    )

    examples = qa_sql_examples_from_markdown(markdown)
    patterns = qa_sql_patterns_from_markdown(markdown, limit=20)

    assert "## Q/A SQL 例" not in markdown
    assert "## Q/A 由来 SQL 生成ルール" in markdown
    assert "## Q/A SQL 構造パターン" in markdown
    assert primary_sql not in markdown
    assert "SELECT RESOURCE_ID FROM APP.RESOURCES" not in markdown
    assert examples == []
    assert len(patterns) == 2
    assert "question" not in patterns[0]
    assert set(patterns[0]["physical_tables"]) == {"APP.FACT_VALUES", "APP.RESOURCES"}
    assert "ACTIVE" in " ".join(patterns[0]["filters"])
    assert patterns[0]["cte_names"] == ["CURRENT_FACTS"]
    assert any("使用表候補" in rule for rule in patterns[0]["generation_rules_ja"])


def test_legacy_markdown_qa_sql_examples_remain_readable() -> None:
    primary_sql = (
        "WITH CURRENT_FACTS AS ("
        "SELECT A.RESOURCE_ID, SUM(A.AMOUNT) AS TOTAL_AMOUNT "
        "FROM APP.FACT_VALUES A WHERE A.STATUS_CODE = 'ACTIVE' GROUP BY A.RESOURCE_ID"
        ") SELECT A.RESOURCE_ID, B.RESOURCE_NAME, A.TOTAL_AMOUNT "
        "FROM CURRENT_FACTS A, APP.RESOURCES B "
        "WHERE B.RESOURCE_ID = A.RESOURCE_ID ORDER BY A.RESOURCE_ID"
    )
    markdown = "\n".join(
        [
            "# Confirmed Markdown",
            "",
            "## Q/A SQL 例",
            "```jsonl",
            json.dumps(
                {
                    "question": "有効な金額集計を確認したい",
                    "sql": primary_sql,
                    "note_ja": "旧 Markdown 互換",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            json.dumps(
                {
                    "question": "リソース一覧",
                    "sql": "SELECT RESOURCE_ID FROM APP.RESOURCES",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "```",
            "",
        ]
    )

    selected = select_qa_sql_examples_from_markdown(markdown, "有効金額の集計を見たい")

    assert selected[0].question == "有効な金額集計を確認したい"
    assert "APP.FACT_VALUES" in selected[0].sql
    assert "ACTIVE" in selected[0].sql
    assert select_qa_sql_examples_from_markdown(markdown, "別件", min_score=0.9) == []


def test_internal_schema_resolved_diagnostics_are_not_user_warnings() -> None:
    internal_warning = "schema_resolved_join_conditionsが空のため自己結合は明示せず。"
    actionable_warning = "cardinality が未確定のため確認してください。"
    rejection_warning = "命名候補 APP.SECRET を profile 範囲内に解決できません。"

    actionable, rejections = ontology_build_module._split_ontology_build_warnings(
        [internal_warning, actionable_warning, rejection_warning]
    )
    markdown = render_ontology_build_markdown(
        profile_id="HR",
        schema_context=json.dumps({"objects": [], "relationships": []}),
        drafts=[],
        warnings=actionable,
        proposal_rejections=rejections,
        source_count=0,
        qa_pair_count=0,
        business_text_present=False,
    )

    assert actionable == [actionable_warning]
    assert rejections == [rejection_warning]
    assert "schema_resolved_join_conditions" not in markdown
    assert actionable_warning in markdown
    assert rejection_warning in markdown


@pytest.mark.parametrize(
    ("phases", "expected"),
    [
        ([("freeze", "succeeded"), ("concepts", "running")], "concepts"),
        ([("concepts", "running"), ("save", "running")], "save"),
        ([("objects", "running")], None),
        ([("freeze", "succeeded")], None),
        ([], None),
    ],
    ids=["event-phase", "last-running", "not-event-phase", "none-running", "empty"],
)
def test_running_event_phase_maps_only_event_phases(
    phases: list[tuple[Any, Any]], expected: str | None
) -> None:
    """phase の無いイベントには、実行中の定義のフェーズのうち付けられるものだけを付ける(#1661)。"""

    definition_phases = [DefinitionPhase(name=name, status=status) for name, status in phases]
    assert ontology_build_module._running_event_phase(definition_phases) == expected


def test_build_job_creates_markdown_draft_and_drops_outside_candidates(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    runtime, _store, legacy = harness
    client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)
    legacy._enterprise_ai_client = client
    service = OntologyBuildService(runtime)

    job = service.start(
        "sales",
        business_text="受注は顧客に紐づく。売上は受注金額の合計。",
        qa_pairs=[QaPair(question="顧客別売上", sql=_QA_SQL)],
    )
    finished = _wait_for_job(service, job.id)

    assert finished.status == OntologyBuildStatus.SUCCEEDED
    assert [step.name for step in finished.steps] == [
        OntologyBuildStepName.SCHEMA_CONTEXT,
        OntologyBuildStepName.SCHEMA_NAMING,
        OntologyBuildStepName.QA_EXTRACTION,
        OntologyBuildStepName.TEXT_EXTRACTION,
        OntologyBuildStepName.PROPOSAL_REGISTRATION,
    ]
    assert all(step.status == OntologyBuildStepStatus.SUCCEEDED for step in finished.steps)
    # 各ステップに開始・終了時刻が入り、アクティビティタイムラインが時系列で積まれる
    assert all(
        step.started_at is not None and step.finished_at is not None for step in finished.steps
    )
    assert finished.started_at is not None
    assert len(finished.events) >= 5
    event_times = [event.at for event in finished.events]
    assert event_times == sorted(event_times)
    assert any("スキーマ情報を準備しました" in event.message_ja for event in finished.events)
    assert any("Markdown 下書き" in event.message_ja for event in finished.events)
    # スコープ外(APP.SECRET)は draft graph 化せず、利用者向け warning ではなく採用外候補に残す
    assert not any("APP.SECRET" in warning for warning in finished.warnings_ja)
    assert finished.proposal_ids == []
    assert finished.draft_revision_id
    assert finished.draft_etag
    schema_context = json.loads(client.contexts[0])
    assert {item["object"] for item in schema_context["objects"]} == {
        "APP.CUSTOMERS",
        "APP.ORDERS",
    }
    orders = next(item for item in schema_context["objects"] if item["object"] == "APP.ORDERS")
    assert any(
        column["column"] == "CUSTOMER_ID" and column["data_type"] == "NUMBER"
        for column in orders["columns"]
    )
    assert any(
        relationship["source_object"] == "APP.ORDERS"
        and relationship["target_object"] == "APP.CUSTOMERS"
        and relationship["join_conditions"][0]["expression"]
        == "APP.ORDERS.CUSTOMER_ID = APP.CUSTOMERS.ID"
        for relationship in schema_context["relationships"]
    )
    # 下書きの見出し・入力の要約・中身の無い節は書かない（#1720）。
    assert finished.markdown_output.startswith("## Q/A 由来 SQL 生成ルール")
    assert "- なし" not in finished.markdown_output
    assert "## 入力サマリー" not in finished.markdown_output
    assert "## 物理オブジェクト" not in finished.markdown_output
    assert "## Q/A SQL 例" not in finished.markdown_output
    assert "## Q/A 由来 SQL 生成ルール" in finished.markdown_output
    assert _QA_SQL not in finished.markdown_output
    assert qa_sql_examples_from_markdown(finished.markdown_output) == []
    qa_patterns = qa_sql_patterns_from_markdown(finished.markdown_output)
    assert qa_patterns
    assert "question" not in qa_patterns[0]
    assert "APP.ORDERS" in qa_patterns[0]["physical_tables"]
    assert "#### オブジェクト型（Object Type）" in finished.markdown_output
    assert "受注 (`APP.ORDERS`)" in finished.markdown_output
    assert "#### リンク型（Link Type）" in finished.markdown_output
    assert "顧客を参照" in finished.markdown_output
    assert "APP.ORDERS.CUSTOMER_ID = APP.CUSTOMERS.ID" in finished.markdown_output
    assert "#### 指標（Metric）" in finished.markdown_output
    assert "受注金額合計" in finished.markdown_output
    assert "APP.ORDERS.AMOUNT" in finished.markdown_output
    assert "#### 業務ルール（Business Rule）" not in finished.markdown_output
    assert "別名（Alias）:" in finished.markdown_output
    assert "オーダー" in finished.markdown_output
    assert "解決が必要な定義の競合" not in finished.markdown_output
    assert "## 採用外候補" not in finished.markdown_output
    assert "APP.SECRET" not in finished.markdown_output

    assert runtime.list_profile_proposals("sales") == []
    state = runtime.ontology_markdown_state("sales")
    assert state.draft_revision is not None
    assert state.draft_revision.id == finished.draft_revision_id
    assert state.draft_etag == finished.draft_etag
    assert state.draft_markdown == finished.markdown_output
    saved_state = runtime.save_ontology_markdown_draft(
        "sales",
        OntologyMarkdownDraftPatch(
            markdown=f"{state.draft_markdown}\n## 手動メモ\n- 確認済み\n",
            base_etag=state.draft_etag,
        ),
    )
    assert saved_state.draft_etag != state.draft_etag
    assert "手動メモ" in saved_state.draft_markdown
    with pytest.raises((OntologyVersionConflict, OntologyVersionConflictError)):
        runtime.save_ontology_markdown_draft(
            "sales",
            OntologyMarkdownDraftPatch(markdown="stale", base_etag=state.draft_etag),
        )
    draft = runtime.ontology_revision(finished.draft_revision_id)
    kinds = {node.kind for node in draft.nodes}
    assert OntologyNodeKind.OBJECT_TYPE in kinds
    assert OntologyNodeKind.METRIC in kinds
    relationship_edges = [edge for edge in draft.edges if edge.kind == OntologyEdgeKind.LINK_TYPE]
    assert len(relationship_edges) == 1
    assert relationship_edges[0].review_status == OntologyReviewStatus.APPROVED
    orders_entity = next(
        node
        for node in draft.nodes
        if node.kind == OntologyNodeKind.OBJECT_TYPE and node.technical_name == "APP.ORDERS"
    )
    assert set(orders_entity.aliases) >= {"注文", "オーダー"}


def test_build_job_reuses_prepared_scope_revision_when_saving_markdown_draft(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _store, legacy = harness
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)
    original_create_build_markdown_draft = runtime.create_build_markdown_draft
    prepared_bases: list[SchemaOntology | None] = []

    def capture_create_build_markdown_draft(**kwargs: Any) -> Any:
        prepared_base = kwargs.get("prepared_base")
        prepared_bases.append(prepared_base)
        if prepared_base is None:
            raise TimeoutError("unexpected baseline revision reload")
        assert prepared_base.revision.id == kwargs["base_revision_id"]
        return original_create_build_markdown_draft(**kwargs)

    monkeypatch.setattr(
        runtime,
        "create_build_markdown_draft",
        capture_create_build_markdown_draft,
    )
    service = OntologyBuildService(runtime)
    job = service.start("sales", business_text="受注は顧客に紐づく。", run_schema_naming=False)
    finished = _wait_for_job(service, job.id)

    assert finished.status == OntologyBuildStatus.SUCCEEDED
    assert prepared_bases and prepared_bases[0] is not None


def test_build_job_batches_all_source_chunks_without_omission(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _store, legacy = harness
    client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)
    legacy._enterprise_ai_client = client
    # 資料の入る量（上限 − 抽出の system prompt）を一定にする。system prompt の文が増えても
    # 分割の数（LLM の呼び出しの数）と所要時間が変わらないようにする（#1741）。
    monkeypatch.setattr(
        ontology_build_module,
        "_ONTOLOGY_BUILD_LLM_CONTEXT_MAX_CHARS",
        len(ontology_build_module._EXTRACTION_SYSTEM_PROMPT) + 3_428,
    )
    # このテストは「全 chunk が漏れなく 1 回処理される」ことの契約。gleaning の追加パスは
    # 同じ context を再送するため chunk 数の勘定から除外する
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_extraction_gleaning_passes", 0)
    # 送った context を呼んだ順に連結して確かめるため、組を 1 件ずつ呼ぶ（並行は #2004 のテスト）。
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_build_concurrency", 1)

    contents: dict[str, bytes] = {}
    source_documents: list[OntologySourceDocument] = []
    markers: list[str] = []
    for source_index in range(6):
        lines: list[str] = []
        for chunk_index in range(3):
            marker = f"FULL_SOURCE_MARKER_{source_index}_{chunk_index}"
            markers.append(marker)
            lines.append(f"{marker} " + ("受注は顧客に紐づく。" * 80))
        content = "\n".join(lines).encode()
        source_id = f"ontology_source_{source_index}"
        contents[source_id] = content
        source_documents.append(_source_document(source_id, f"source-{source_index}.md", content))

    # 18 chunk を何度も LLM に送るため、このファイルで最も重い。thread で走らせて実時間で
    # 待たず、独立 worker と同じ入口で同期に実行する（#1914）。
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    service = OntologyBuildService(
        runtime,
        source_storage=_FakeOntologySourceStorage(contents),  # type: ignore[arg-type]
    )
    queued = service.start(
        "sales",
        run_schema_naming=False,
        run_qa_extraction=False,
        source_documents=source_documents,
    )
    finished = service.run_persisted(queued.id)

    assert finished.status == OntologyBuildStatus.SUCCEEDED
    text_contexts = [context for context in client.contexts if "business_text_chunks" in context]
    assert len(text_contexts) > 1
    assert all(
        ontology_build_module._llm_call_chars(client.calls[index], context)
        <= ontology_build_module._ONTOLOGY_BUILD_LLM_CONTEXT_MAX_CHARS
        for index, context in enumerate(client.contexts)
    )
    combined_context = "\n".join(text_contexts)
    for marker in markers:
        assert marker in combined_context
    # 統合工程で各資料を一度だけ読み、分割後も欠落・重複がない。
    texts: dict[str, str] = {}
    for context in text_contexts:
        for chunk in json.loads(context)["business_text_chunks"]:
            source_id = chunk["source_id"]
            texts[source_id] = texts.get(source_id, "") + chunk["text"]
    expected_texts = {
        source_id: "".join(content.decode().split()) for source_id, content in contents.items()
    }
    assert {key: "".join(value.split()) for key, value in texts.items()} == expected_texts
    assert any("chunk batch" in event.message_ja for event in finished.events)


def test_build_job_batches_more_than_two_hundred_qa_pairs(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _store, legacy = harness
    client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)
    legacy._enterprise_ai_client = client
    # 抽出の指示（system prompt）が長くなった分の余裕を持たせる（#1720）。
    monkeypatch.setattr(ontology_build_module, "_ONTOLOGY_BUILD_LLM_CONTEXT_MAX_CHARS", 32_000)
    qa_pairs = [
        QaPair(
            question=f"顧客別売上 {index}",
            sql=(
                "SELECT C.NAME, SUM(O.AMOUNT) FROM APP.ORDERS O "
                "JOIN APP.CUSTOMERS C ON O.CUSTOMER_ID = C.ID "
                f"WHERE O.ID >= {index} GROUP BY C.NAME"
            ),
        )
        for index in range(205)
    ]
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    service = OntologyBuildService(runtime)

    queued = service.start(
        "sales",
        qa_pairs=qa_pairs,
        run_schema_naming=False,
        run_text_extraction=False,
    )
    finished = service.run_persisted(queued.id)

    assert finished.status == OntologyBuildStatus.SUCCEEDED
    qa_contexts = [context for context in client.contexts if '"qa_pairs"' in context]
    assert len(qa_contexts) > 1
    sent_pairs = [pair for context in qa_contexts for pair in json.loads(context)["qa_pairs"]]
    assert len(sent_pairs) == 205 * 2  # 統合抽出 + 取りこぼし確認（既定1回）
    assert {p["question"] for p in sent_pairs} == {p.question for p in qa_pairs}
    assert sent_pairs[-1]["question"] == "顧客別売上 204"
    assert any("Q/A batch" in event.message_ja for event in finished.events)


def test_build_job_fails_when_pdf_page_requires_ocr_without_image_runner(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    from pypdf import PdfWriter

    runtime, _store, legacy = harness
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    content = buffer.getvalue()
    source_id = "ontology_source_blank_pdf"
    source = _source_document(
        source_id,
        "scan.pdf",
        content,
        media_type="application/pdf",
    )
    service = OntologyBuildService(
        runtime,
        source_storage=_FakeOntologySourceStorage({source_id: content}),  # type: ignore[arg-type]
    )

    queued = service.start(
        "sales",
        run_schema_naming=False,
        run_qa_extraction=False,
        source_documents=[source],
    )
    finished = _wait_for_job(service, queued.id)

    assert finished.status == OntologyBuildStatus.FAILED
    assert "page:1" in finished.error_message_ja
    assert "OCR 設定" in finished.error_message_ja
    source_progress = finished.sources[0]
    assert source_progress.status == OntologySourceStatus.FAILED
    assert "page:1" in source_progress.error_message_ja


def test_markdown_draft_merges_profile_view_overrides_rules_and_enums() -> None:
    provenance = OntologyProvenance(source_kind=OntologySourceKind.MANUAL, source_id="test")
    revision = OntologyRevision(
        id="ontology_revision_test",
        version=4,
        status=OntologyRevisionStatus.PUBLISHED,
        schema_fingerprint="fp-test",
        etag="rev-etag-test",
    )
    orders_ref = PhysicalObjectRef(
        node_id="table-orders",
        owner="APP",
        object_name="ORDERS",
        object_type="table",
    )
    customers_ref = PhysicalObjectRef(
        node_id="table-customers",
        owner="APP",
        object_name="CUSTOMERS",
        object_type="table",
    )
    status_column = PhysicalColumnRef(
        node_id="column-order-status",
        owner="APP",
        object_name="ORDERS",
        column_name="STATUS",
        ordinal=3,
    )
    customer_id_column = PhysicalColumnRef(
        node_id="column-order-customer",
        owner="APP",
        object_name="ORDERS",
        column_name="CUSTOMER_ID",
        ordinal=2,
    )
    customer_pk_column = PhysicalColumnRef(
        node_id="column-customer-id",
        owner="APP",
        object_name="CUSTOMERS",
        column_name="ID",
        ordinal=1,
    )
    orders = OntologyNode(
        id="table-orders",
        revision_id=revision.id,
        kind=OntologyNodeKind.TABLE,
        technical_name="APP.ORDERS",
        business_name_ja="受注",
        description_ja="受注トランザクション",
        physical_mappings=[PhysicalMapping(object_ref=orders_ref)],
        provenance=provenance,
        review_status=OntologyReviewStatus.APPROVED,
    )
    customers = OntologyNode(
        id="table-customers",
        revision_id=revision.id,
        kind=OntologyNodeKind.TABLE,
        technical_name="APP.CUSTOMERS",
        business_name_ja="顧客",
        description_ja="顧客マスタ",
        physical_mappings=[PhysicalMapping(object_ref=customers_ref)],
        provenance=provenance,
        review_status=OntologyReviewStatus.APPROVED,
    )
    status_property = OntologyNode(
        id="property-order-status",
        revision_id=revision.id,
        kind=OntologyNodeKind.PROPERTY,
        technical_name="APP.ORDERS.STATUS",
        business_name_ja="受注状態",
        description_ja="受注の状態",
        physical_mappings=[PhysicalMapping(object_ref=orders_ref, column_refs=[status_column])],
        provenance=provenance,
        review_status=OntologyReviewStatus.APPROVED,
    )
    rule = OntologyNode(
        id="business-rule-order-status",
        revision_id=revision.id,
        kind=OntologyNodeKind.BUSINESS_RULE,
        technical_name="order_status_required",
        business_name_ja="受注状態必須",
        description_ja="受注状態を空にしない。",
        provenance=provenance,
        review_status=OntologyReviewStatus.APPROVED,
        business_rule_definition=BusinessRuleDefinition(
            rule_kind=BusinessRuleKind.VALIDATION,
            statement_ja="受注状態を必須にします。",
            applies_to_node_ids=["table-orders"],
            severity=BusinessRuleSeverity.WARNING,
            execution_mode=BusinessRuleExecutionMode.DOCUMENTATION,
        ),
    )
    enum_value = OntologyNode(
        id="enum-order-confirmed",
        revision_id=revision.id,
        kind=OntologyNodeKind.ENUM_VALUE,
        technical_name="CONFIRMED",
        business_name_ja="確定済み",
        description_ja="確定した受注。",
        provenance=provenance,
        review_status=OntologyReviewStatus.APPROVED,
        enum_value_definition=EnumValueDefinition(
            code="CONFIRMED",
            label_ja="確定済み",
            aliases=["確定"],
            physical_literal="C",
            property_node_id="property-order-status",
        ),
    )
    relationship = OntologyEdge(
        id="edge-order-customer",
        revision_id=revision.id,
        kind=OntologyEdgeKind.FOREIGN_KEY,
        source_node_id="table-orders",
        target_node_id="table-customers",
        relationship_name_ja="受注の顧客",
        description_ja="Oracle FK_ORDERS_CUSTOMER",
        cardinality=RelationshipCardinality.MANY_TO_ONE,
        join_conditions=[
            JoinCondition(left=customer_id_column, right=customer_pk_column, operator="=")
        ],
        provenance=provenance,
        review_status=OntologyReviewStatus.APPROVED,
    )
    ontology = SchemaOntology(
        revision=revision,
        nodes=[orders, customers, status_property, rule, enum_value],
        edges=[relationship],
    )
    view = ProfileOntologyView(
        id="profile-view-test",
        profile_id="sales",
        ontology_revision_id=revision.id,
        etag="view-etag-test",
        node_ids=[node.id for node in ontology.nodes],
        edge_ids=[relationship.id],
        physical_objects=[orders_ref, customers_ref],
        table_usages_ja={"table-orders": "受注分析の主表"},
        allowed_path_ids=[relationship.id],
        draft_node_overrides=[{"node_id": "table-orders", "business_name_ja": "受注明細"}],
        draft_edge_overrides=[
            {
                "edge_id": relationship.id,
                "cardinality": "one_to_many",
                "allowed_path": True,
            }
        ],
    )

    markdown = render_ontology_build_markdown(
        profile_id="sales",
        schema_context=json.dumps({"objects": [], "relationships": []}),
        drafts=[],
        warnings=["APP.MISSING を公開オントロジーに解決できません。"],
        source_count=0,
        qa_pair_count=0,
        business_text_present=False,
        ontology=ontology,
        profile_view=view,
    )

    assert "## 物理オブジェクト" in markdown
    assert "`APP.ORDERS` (table)" in markdown
    assert "業務名: 受注明細" in markdown
    assert "用途: 受注分析の主表" in markdown
    assert "受注明細 (`APP.ORDERS`)" in markdown
    assert "## 関係 / Join" in markdown
    assert "多重度: one_to_many" in markdown
    assert "検索利用: 利用可" in markdown
    assert "`APP.ORDERS.CUSTOMER_ID = APP.CUSTOMERS.ID`" in markdown
    assert "## 業務ルール / 列挙値" in markdown
    assert "ルール文: 受注状態を必須にします。" in markdown
    assert "適用対象: 受注明細" in markdown
    assert "コード: CONFIRMED" in markdown
    assert "物理値: `C`" in markdown
    assert "属性: 受注状態" in markdown
    assert "APP.MISSING" in markdown


def test_build_job_uses_ontology_schema_fingerprint_for_registration_conflict(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    """catalog head の fingerprint 口径差だけでは schema drift 扱いにしない。"""

    runtime, _store, legacy = harness
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)
    legacy.catalog = legacy.catalog.model_copy(
        update={"schema_fingerprint": "oracle-style-different-hash"},
        deep=True,
    )
    assert legacy.catalog.schema_fingerprint != catalog_schema_fingerprint(legacy.catalog)
    service = OntologyBuildService(runtime)

    job = service.start("sales", business_text="受注は顧客に紐づく。")
    finished = _wait_for_job(service, job.id)

    assert finished.status == OntologyBuildStatus.SUCCEEDED
    assert finished.proposal_ids == []
    assert finished.draft_revision_id
    assert not (
        finished.error_message_ja
        and "AI 構築中に DB schema catalog が更新されました" in finished.error_message_ja
    )
    assert runtime.ontology_markdown_state("sales").draft_revision is not None
    assert runtime.list_profile_proposals("sales") == []


def test_convert_extraction_warns_on_unknown_cardinality(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    """cardinality 未確定(unknown)の関係候補は提案化しつつ warning を付ける。"""

    from app.features.nl2sql.ontology_build import convert_extraction_to_proposals
    from app.features.nl2sql.ontology_models import OntologyBuildExtraction

    runtime, _store, _legacy = harness
    view, ontology = runtime.profile_view("sales")
    extraction = OntologyBuildExtraction.model_validate(
        {
            "relationships": [
                {
                    "source_object": "APP.ORDERS",
                    "target_object": "APP.CUSTOMERS",
                    "relationship_name_ja": "顧客を参照",
                    "cardinality": "unknown",
                    "join_conditions": [
                        {"left": "APP.ORDERS.CUSTOMER_ID", "right": "APP.CUSTOMERS.ID"}
                    ],
                }
            ]
        }
    )
    drafts, warnings = convert_extraction_to_proposals(
        extraction,
        ontology=ontology,
        view=view,
        job_id="job-1",
        inferred_by="test",
    )
    assert any(draft.kind == OntologyProposalKind.RELATIONSHIP for draft in drafts)
    assert any("cardinality が未確定" in warning for warning in warnings)


def test_extraction_prompt_contains_playground_rules() -> None:
    """Playground 由来の抽出ルール(名詞/動詞・cardinality 必須・主識別子)を明文化する。"""

    from app.features.nl2sql.ontology_build import _EXTRACTION_SYSTEM_PROMPT

    assert "名詞をオブジェクト" in _EXTRACTION_SYSTEM_PROMPT
    assert "one_to_one / one_to_many / many_to_one / many_to_many" in _EXTRACTION_SYSTEM_PROMPT
    assert "主識別子" in _EXTRACTION_SYSTEM_PROMPT


def test_build_job_fails_gracefully_without_enterprise_ai(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    runtime, _store, _legacy = harness
    service = OntologyBuildService(runtime)
    job = service.start("sales", business_text="テスト")
    finished = _wait_for_job(service, job.id)

    assert finished.status == OntologyBuildStatus.FAILED
    assert "Enterprise AI" in finished.error_message_ja
    assert finished.proposal_ids == []
    assert all(step.status == OntologyBuildStepStatus.SKIPPED for step in finished.steps)


def test_build_job_survives_invalid_llm_json(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    runtime, _store, legacy = harness
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient("これは JSON ではありません")
    service = OntologyBuildService(runtime)
    job = service.start("sales", business_text="テスト", run_schema_naming=False)
    finished = _wait_for_job(service, job.id)

    text_step = next(
        step for step in finished.steps if step.name == OntologyBuildStepName.TEXT_EXTRACTION
    )
    assert text_step.status == OntologyBuildStepStatus.FAILED
    assert any("抽出に失敗" in warning for warning in finished.warnings_ja)
    assert finished.proposal_ids == []


def test_build_job_fails_gracefully_when_markdown_draft_save_times_out(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _store, legacy = harness
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)

    def fail_create_build_markdown_draft(**kwargs: Any) -> Any:
        on_progress = kwargs.get("on_progress")
        if callable(on_progress):
            on_progress("下書き revision を保存しています…")
        raise TimeoutError("Oracle round-trip timeout")

    monkeypatch.setattr(
        runtime,
        "create_build_markdown_draft",
        fail_create_build_markdown_draft,
    )
    service = OntologyBuildService(runtime)
    job = service.start("sales", business_text="受注は顧客に紐づく。", run_schema_naming=False)
    finished = _wait_for_job(service, job.id)

    assert finished.status == OntologyBuildStatus.FAILED
    registration = next(
        step for step in finished.steps if step.name == OntologyBuildStepName.PROPOSAL_REGISTRATION
    )
    assert registration.status == OntologyBuildStepStatus.FAILED
    assert registration.detail_ja == "Markdown 下書きの保存に失敗しました。"
    assert "Oracle round-trip timeout" in finished.error_message_ja
    assert any("下書き revision を保存しています" in event.message_ja for event in finished.events)


def test_build_job_fails_gracefully_when_final_status_save_times_out(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, store, legacy = harness
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)
    original_save_document = store.save_document

    def fail_final_status_save(
        collection: str,
        document: Any,
        *,
        expected_etag: str | None = None,
    ) -> dict[str, Any]:
        if collection == "jobs" and document.get("status") == OntologyBuildStatus.SUCCEEDED.value:
            raise TimeoutError("Oracle final job timeout")
        return original_save_document(collection, document, expected_etag=expected_etag)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "save_document", fail_final_status_save)
    service = OntologyBuildService(runtime)
    job = service.start("sales", business_text="受注は顧客に紐づく。", run_schema_naming=False)
    finished = _wait_for_job(service, job.id)

    assert finished.status == OntologyBuildStatus.FAILED
    registration = next(
        step for step in finished.steps if step.name == OntologyBuildStepName.PROPOSAL_REGISTRATION
    )
    assert registration.status == OntologyBuildStepStatus.FAILED
    assert registration.detail_ja == "構築 job の完了状態の保存に失敗しました。"
    assert "Oracle final job timeout" in finished.error_message_ja
    assert runtime.ontology_markdown_state("sales").draft_revision is not None


def test_get_build_job_normalizes_succeeded_markdown_job_with_running_final_step(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    runtime, store, _legacy = harness
    stuck = OntologyBuildJob(
        id="ontology_build_stuck_final_step",
        profile_id="sales",
        status=OntologyBuildStatus.SUCCEEDED,
        steps=[
            OntologyBuildStep(
                name=OntologyBuildStepName.SCHEMA_CONTEXT,
                status=OntologyBuildStepStatus.SUCCEEDED,
            ),
            OntologyBuildStep(
                name=OntologyBuildStepName.PROPOSAL_REGISTRATION,
                status=OntologyBuildStepStatus.RUNNING,
                detail_ja="構築 job の完了状態を保存しています…",
            ),
        ],
        draft_revision_id="ontology_revision_draft_stuck",
        draft_etag="markdown-etag-stuck",
        markdown_output="# オントロジー下書き\n",
    )
    stuck.started_at = stuck.created_at
    stuck.finished_at = stuck.created_at
    for step in stuck.steps:
        step.started_at = stuck.created_at
    store.save_document(
        "jobs",
        {
            "job_id": stuck.id,
            "job_type": "build",
            "profile_id": stuck.profile_id,
            "status": stuck.status.value,
            "payload": stuck.model_dump(mode="json"),
            "input_payload": {},
        },
    )
    service = OntologyBuildService(runtime)

    normalized = service.get(stuck.id)

    assert normalized is not None
    assert normalized.status == OntologyBuildStatus.SUCCEEDED
    registration = next(
        step
        for step in normalized.steps
        if step.name == OntologyBuildStepName.PROPOSAL_REGISTRATION
    )
    assert registration.status == OntologyBuildStepStatus.SUCCEEDED
    assert registration.detail_ja == "Markdown 下書きを生成しました。"
    assert registration.finished_at == normalized.finished_at
    raw_document = store.get_document("jobs", {"job_id": stuck.id})
    assert raw_document is not None
    raw_job = OntologyBuildJob.model_validate(raw_document["payload"])
    raw_registration = next(
        step for step in raw_job.steps if step.name == OntologyBuildStepName.PROPOSAL_REGISTRATION
    )
    assert raw_registration.status == OntologyBuildStepStatus.RUNNING


def test_list_profile_jobs_returns_newest_first_with_limit(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    from datetime import timedelta

    from app.features.nl2sql.ontology_models import utc_now

    runtime, store, _legacy = harness
    service = OntologyBuildService(runtime)
    base = utc_now()
    first = OntologyBuildJob(
        id="ontology_build_list_first",
        profile_id="sales",
        status=OntologyBuildStatus.SUCCEEDED,
        created_at=base,
        finished_at=base,
    )
    second = OntologyBuildJob(
        id="ontology_build_list_second",
        profile_id="sales",
        status=OntologyBuildStatus.SUCCEEDED,
        created_at=base + timedelta(seconds=1),
        finished_at=base + timedelta(seconds=1),
    )
    third = OntologyBuildJob(
        id="ontology_build_list_third",
        profile_id="sales",
        status=OntologyBuildStatus.SUCCEEDED,
        created_at=base + timedelta(seconds=2),
        finished_at=base + timedelta(seconds=2),
    )
    for job in [first, second, third]:
        store.save_document(
            "jobs",
            {
                "job_id": job.id,
                "job_type": "build",
                "profile_id": job.profile_id,
                "status": job.status.value,
                "payload": job.model_dump(mode="json"),
                "input_payload": {},
            },
        )

    jobs = service.list_profile_jobs("sales", limit=2)
    assert [job.id for job in jobs] == [third.id, second.id]
    all_jobs = service.list_profile_jobs("sales", limit=10)
    assert [job.id for job in all_jobs] == [third.id, second.id, first.id]


def test_list_profile_source_documents_returns_newest_first_with_legacy_role(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    runtime, store, _legacy = harness
    service = OntologyBuildService(runtime)
    old_source = OntologySourceDocument(
        id="ontology_source_old",
        profile_id="sales",
        filename="old-rules.md",
        media_type="text/markdown",
        size_bytes=12,
        sha256="0" * 64,
        storage_uri="/tmp/old-rules.md",
    )
    qa_source = OntologySourceDocument(
        id="ontology_source_qa",
        profile_id="sales",
        filename="qa-cases.csv",
        source_role=OntologySourceRole.QA,
        media_type="text/csv",
        size_bytes=48,
        sha256="1" * 64,
        storage_uri="/tmp/qa-cases.csv",
    )
    legacy_payload = old_source.model_dump(mode="json")
    legacy_payload.pop("source_role")
    store.save_document(
        "source_documents",
        {
            "source_document_id": old_source.id,
            "profile_id": old_source.profile_id,
            "status": old_source.status.value,
            "sha256": old_source.sha256,
            "payload": legacy_payload,
        },
    )
    service._save_source_document(qa_source)  # noqa: SLF001 - persisted source fixture

    listed = service.list_profile_source_documents("sales", limit=1)

    assert [(source.filename, source.source_role) for source in listed] == [
        ("qa-cases.csv", OntologySourceRole.QA)
    ]
    all_listed = service.list_profile_source_documents("sales", limit=10)
    assert {source.filename: source.source_role for source in all_listed} == {
        "old-rules.md": OntologySourceRole.SOURCE,
        "qa-cases.csv": OntologySourceRole.QA,
    }


def test_list_profile_source_documents_skips_mismatched_payload_profile(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    runtime, store, _legacy = harness
    service = OntologyBuildService(runtime)
    valid_source = OntologySourceDocument(
        id="ontology_source_valid_sales",
        profile_id="sales",
        filename="sales-rules.md",
        media_type="text/markdown",
        size_bytes=12,
        sha256="2" * 64,
        storage_uri="/tmp/sales-rules.md",
    )
    mismatched_source = OntologySourceDocument(
        id="ontology_source_mismatched",
        profile_id="finance",
        filename="finance-rules.md",
        media_type="text/markdown",
        size_bytes=12,
        sha256="3" * 64,
        storage_uri="/tmp/finance-rules.md",
    )
    service._save_source_document(valid_source)  # noqa: SLF001 - persisted source fixture
    store.save_document(
        "source_documents",
        {
            "source_document_id": mismatched_source.id,
            "profile_id": "sales",
            "status": mismatched_source.status.value,
            "sha256": mismatched_source.sha256,
            "payload": mismatched_source.model_dump(mode="json"),
        },
    )

    listed = service.list_profile_source_documents("sales", limit=10)

    assert [source.filename for source in listed] == ["sales-rules.md"]
    with pytest.raises(RuntimeError, match="業務プロファイル"):
        service._get_source_document(mismatched_source.id, profile_id="sales")  # noqa: SLF001


def test_cancel_single_job_is_idempotent_and_blocks_worker(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    runtime, store, legacy = harness
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    service = OntologyBuildService(runtime)
    job = service.start("sales", business_text="販売業務")

    cancelled = service.cancel(job.id)
    assert cancelled.status == OntologyBuildStatus.CANCELLED
    assert any("利用者の操作" in event.message_ja for event in cancelled.events)
    # CANCELLED への再キャンセルは no-op 成功
    assert service.cancel(job.id).status == OntologyBuildStatus.CANCELLED
    # worker が後から実行しても cancelled のまま(proposal 生成なし)
    result = service.run_persisted(job.id)
    assert result.status == OntologyBuildStatus.CANCELLED
    assert store.list_documents("proposals", {"profile_id": "sales"}) == []


def test_cancel_finished_or_missing_job_raises(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    from app.features.nl2sql.ontology_service import (
        OntologyNotFoundError,
        OntologyStateConflictError,
    )

    runtime, _store, legacy = harness
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)
    service = OntologyBuildService(runtime)
    job = service.start("sales", business_text="販売業務")
    _wait_for_job(service, job.id)

    with pytest.raises(OntologyStateConflictError):
        service.cancel(job.id)
    with pytest.raises(OntologyNotFoundError):
        service.cancel("ontology_build_missing")


def test_retry_reuses_persisted_inputs_and_toggles(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    runtime, _store, legacy = harness
    # 1 回目は Enterprise AI 未設定で失敗させる
    service = OntologyBuildService(runtime)
    failed = service.start(
        "sales",
        business_text="受注は顧客に紐づく。",
        run_schema_naming=False,
    )
    finished = _wait_for_job(service, failed.id)
    assert finished.status == OntologyBuildStatus.FAILED

    # 設定を直して retry → 保存済み入力(業務テキスト・トグル)で新規 job が成功する
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)
    retried = service.retry(failed.id)
    assert retried.id != failed.id
    result = _wait_for_job(service, retried.id)
    assert result.status == OntologyBuildStatus.SUCCEEDED
    step_names = {step.name for step in result.steps}
    assert OntologyBuildStepName.SCHEMA_NAMING not in step_names
    assert OntologyBuildStepName.TEXT_EXTRACTION in step_names


def test_failed_retry_job_can_be_retried_again_as_new_job(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    runtime, _store, _legacy = harness
    service = OntologyBuildService(runtime)
    failed = _wait_for_job(service, service.start("sales", business_text="販売業務").id)
    assert failed.status == OntologyBuildStatus.FAILED

    retry_failed = _wait_for_job(service, service.retry(failed.id).id)
    assert retry_failed.status == OntologyBuildStatus.FAILED

    reretry = service.retry(retry_failed.id)

    assert reretry.id not in {failed.id, retry_failed.id}
    assert _wait_for_job(service, reretry.id).status == OntologyBuildStatus.FAILED


def test_retry_rejects_non_terminal_and_missing_jobs(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.features.nl2sql.ontology_service import (
        OntologyNotFoundError,
        OntologyStateConflictError,
    )

    runtime, _store, legacy = harness
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    service = OntologyBuildService(runtime)
    queued = service.start("sales", business_text="販売業務")

    with pytest.raises(OntologyStateConflictError):
        service.retry(queued.id)
    with pytest.raises(OntologyNotFoundError):
        service.retry("ontology_build_missing")


def test_get_prefers_store_in_external_worker_mode(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API プロセスの古い in-memory コピーではなく worker が書いた store 進捗を返す。"""

    runtime, store, _legacy = harness
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    service = OntologyBuildService(runtime)
    job = service.start("sales", business_text="販売業務")

    # worker 側の進捗書込を模擬(store の status を running へ)
    document = store.get_document("jobs", {"job_id": job.id})
    assert document is not None
    payload = dict(document["payload"])
    payload["status"] = OntologyBuildStatus.RUNNING.value
    store.save_document(
        "jobs",
        {
            **{k: v for k, v in document.items() if k not in {"etag", "created_at", "updated_at"}},
            "status": OntologyBuildStatus.RUNNING.value,
            "payload": payload,
        },
        expected_etag=str(document["etag"]),
    )

    refreshed = service.get(job.id)
    assert refreshed is not None
    assert refreshed.status == OntologyBuildStatus.RUNNING


def test_start_rejects_unknown_profile(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    runtime, _store, _legacy = harness
    service = OntologyBuildService(runtime)
    with pytest.raises(OntologyNotFoundError):
        service.start("unknown-profile")


def test_start_rejects_second_active_profile_job_across_workers(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, store, legacy = harness
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    service_a = OntologyBuildService(runtime)
    started = service_a.start("sales", business_text="販売業務")
    runtime_b = OntologyApiRuntime(legacy_service=legacy, store=store)
    service_b = OntologyBuildService(runtime_b)

    with pytest.raises(OntologyStateConflictError) as exc_info:
        service_b.start("sales", business_text="別タブからの二重実行")

    assert exc_info.value.code == "ONTOLOGY_BUILD_JOB_ALREADY_RUNNING"
    assert started.id in exc_info.value.message_ja

    assert service_a.cancel(started.id).status == OntologyBuildStatus.CANCELLED
    restarted = service_b.start("sales", business_text="取消後の再実行")
    assert restarted.id != started.id


def test_start_rejects_starting_profile_lock_before_job_is_visible(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.features.nl2sql.ontology_build import _PROFILE_ACTIVE_LOCK_OPERATION

    runtime, store, legacy = harness
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    store.save_idempotency(
        {
            "operation": _PROFILE_ACTIVE_LOCK_OPERATION,
            "idempotency_key": "sales",
            "request_hash": hashlib.sha256(b"sales").hexdigest(),
            "resource_id": "ontology_build_starting",
            "status": "active",
            "payload": {
                "operation": _PROFILE_ACTIVE_LOCK_OPERATION,
                "profile_id": "sales",
                "job_id": "ontology_build_starting",
                "created_at_epoch": time.time(),
            },
        }
    )
    runtime_b = OntologyApiRuntime(legacy_service=legacy, store=store)
    service_b = OntologyBuildService(runtime_b)

    with pytest.raises(OntologyStateConflictError) as exc_info:
        service_b.start("sales", business_text="別 worker の開始直後")

    assert exc_info.value.code == "ONTOLOGY_BUILD_JOB_ALREADY_RUNNING"
    assert "ontology_build_starting" in exc_info.value.message_ja


# --- accept → draft → publish → 再起動復元 -----------------------------------------------------


def test_accept_applies_upserts_accumulates_and_publishes(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    runtime, store, legacy = harness
    proposals = _seed_build_proposals(runtime)
    relationship = next(
        proposal for proposal in proposals if proposal.kind == OntologyProposalKind.RELATIONSHIP
    )
    mapping = next(
        proposal for proposal in proposals if proposal.kind == OntologyProposalKind.MAPPING
    )
    metric = next(
        proposal
        for proposal in proposals
        if proposal.kind == OntologyProposalKind.METRIC_DEFINITION
    )

    # 関係提案の accept → 業務ノード + 承認済み関係が draft に入る
    review = runtime.accept_proposal(relationship.id)
    assert review.draft is not None
    draft = review.draft
    business_edges = [
        edge for edge in draft.edges if edge.kind == OntologyEdgeKind.BUSINESS_RELATIONSHIP
    ]
    assert len(business_edges) == 1
    assert business_edges[0].review_status == OntologyReviewStatus.APPROVED
    assert business_edges[0].join_conditions[0].left.column_name == "CUSTOMER_ID"

    # 続けて命名提案を accept → 直前の draft に積み上がり、合成 endpoint が上書きされる
    review2 = runtime.accept_proposal(mapping.id)
    assert review2.draft is not None
    draft2 = review2.draft
    entity_nodes = [node for node in draft2.nodes if node.kind == OntologyNodeKind.BUSINESS_ENTITY]
    orders_entity = next(node for node in entity_nodes if node.technical_name == "APP.ORDERS")
    assert orders_entity.business_name_ja == "受注"
    assert "オーダー" in orders_entity.aliases
    assert not orders_entity.metadata.get("synthetic_endpoint")
    # 関係提案の内容も残っている(最新 revision へ積み上げ)
    assert any(edge.kind == OntologyEdgeKind.BUSINESS_RELATIONSHIP for edge in draft2.edges)

    # 指標提案も accept → metric_definition が node metadata に入る
    review3 = runtime.accept_proposal(metric.id)
    assert review3.draft is not None
    metric_nodes = [node for node in review3.draft.nodes if node.kind == OntologyNodeKind.METRIC]
    assert len(metric_nodes) == 1
    assert metric_nodes[0].metadata["metric_definition"]["expression_sql"] == "SUM(AMOUNT)"

    # publish は全業務要素 APPROVED のため成功する
    published = runtime.publish_ontology_revision(
        review3.draft.revision.id,
        OntologyPublishRequest(etag=review3.draft.revision.etag),
    )
    assert published.revision.status.value == "published"

    # 再起動(同じ store)でも ontology_build 由来 proposal の復元が落ちない
    restarted = OntologyApiRuntime(legacy_service=legacy, store=store)
    restored = restarted.list_profile_proposals("sales")
    assert {proposal.id for proposal in restored} >= {relationship.id, mapping.id, metric.id}


def test_stale_proposal_cache_reloads_store_state_before_review(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    runtime_a, store, legacy = harness
    proposal = _seed_build_proposals(runtime_a)[0]
    runtime_b = OntologyApiRuntime(legacy_service=legacy, store=store)
    cached = runtime_b.get_proposal(proposal.id)
    assert cached.status == OntologyProposalStatus.SUBMITTED

    accepted = runtime_a.accept_proposal(proposal.id)
    assert accepted.proposal.status == OntologyProposalStatus.ACCEPTED

    with pytest.raises(OntologyStateConflictError) as exc_info:
        runtime_b.reject_proposal(proposal.id)

    assert exc_info.value.code == "ONTOLOGY_PROPOSAL_ALREADY_REVIEWED"


def test_batch_accept_skips_already_accepted_proposals_after_partial_retry(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    runtime, store, _legacy = harness
    proposals = _seed_build_proposals(runtime)[:2]
    first = runtime.accept_proposal(proposals[0].id).proposal
    assert first.status == OntologyProposalStatus.ACCEPTED

    accepted, draft = runtime.accept_proposals([proposal.id for proposal in proposals])

    assert draft.revision.status == OntologyRevisionStatus.DRAFT
    assert [proposal.status for proposal in accepted] == [
        OntologyProposalStatus.ACCEPTED,
        OntologyProposalStatus.ACCEPTED,
    ]
    persisted = [
        store.get_document("proposals", {"proposal_id": proposal.id}) for proposal in proposals
    ]
    assert all(document is not None for document in persisted)
    assert [document["status"] for document in persisted if document is not None] == [
        OntologyProposalStatus.ACCEPTED.value,
        OntologyProposalStatus.ACCEPTED.value,
    ]


def test_build_job_fails_fast_when_db_profile_scope_is_empty(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    """DB catalog で profile scope が空のときは LLM を呼ばずに明確なエラーで失敗する。"""

    runtime, _store, legacy = harness
    client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)
    legacy._enterprise_ai_client = client
    # 空カタログ → profile の対象オブジェクトが DB schema catalog に解決できない状態
    legacy.catalog = SchemaCatalog(refreshed_at="2026-07-12T00:00:00Z", tables=[])

    service = OntologyBuildService(runtime)
    job = service.start("sales", business_text="受注は顧客に紐づく。")
    finished = _wait_for_job(service, job.id)

    assert finished.status == OntologyBuildStatus.FAILED
    assert "DB schema catalog" in finished.error_message_ja
    assert "DB 構造を再取得" in finished.error_message_ja
    assert client.calls == []
    schema_step = next(
        step for step in finished.steps if step.name == OntologyBuildStepName.SCHEMA_CONTEXT
    )
    assert schema_step.status == OntologyBuildStepStatus.FAILED
    assert schema_step.detail_ja == "profile 範囲に DB 表・ビューがありません。"
    assert all(
        step.status == OntologyBuildStepStatus.SKIPPED
        for step in finished.steps
        if step.name != OntologyBuildStepName.SCHEMA_CONTEXT
    )


def test_build_job_uses_latest_schema_when_published_profile_scope_is_stale(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    """published Ontology が古くても AI input は DB catalog から作る。"""

    runtime, _store, legacy = harness
    _view, published_base = runtime.profile_view("sales")
    table_node = next(
        node
        for node in published_base.nodes
        if node.kind == OntologyNodeKind.TABLE and node.technical_name == "APP.ORDERS"
    )
    business_node = OntologyNode(
        id="business_orders_pinned_for_build",
        revision_id=published_base.revision.id,
        kind=OntologyNodeKind.BUSINESS_ENTITY,
        technical_name="APP.ORDERS",
        business_name_ja="受注",
        physical_mappings=[PhysicalMapping(object_ref=table_node.physical_mappings[0].object_ref)],
        provenance=OntologyProvenance(source_kind=OntologySourceKind.MANUAL),
        review_status=OntologyReviewStatus.APPROVED,
    )
    draft = runtime.create_ontology_draft(
        published_base.revision.id,
        OntologyDraftRequest(
            base_etag=published_base.revision.etag,
            node_upserts=[business_node],
        ),
    )
    published_with_business = runtime.publish_ontology_revision(
        draft.revision.id,
        OntologyPublishRequest(etag=draft.revision.etag),
    )

    invoice_table = SchemaTable(
        table_name="INVOICES",
        logical_name="請求",
        owner="APP",
        columns=[
            SchemaColumn(column_name="ID", logical_name="請求 ID", data_type="NUMBER"),
            SchemaColumn(column_name="AMOUNT", logical_name="請求金額", data_type="NUMBER"),
        ],
    )
    legacy.catalog = legacy.catalog.model_copy(
        update={"tables": [*legacy.catalog.tables, invoice_table]}
    )
    legacy.profile = legacy.profile.model_copy(update={"allowed_tables": ["APP.INVOICES"]})

    pinned_view, pinned_ontology = runtime.profile_view("sales")
    assert pinned_ontology.revision.id == published_with_business.revision.id
    pinned_node_ids = set(pinned_view.node_ids)
    assert not any(
        node.kind in {OntologyNodeKind.TABLE, OntologyNodeKind.VIEW} and node.id in pinned_node_ids
        for node in pinned_ontology.nodes
    )

    invoice_extraction = {
        "entities": [
            {
                "object_name": "APP.INVOICES",
                "business_name_ja": "請求",
                "description_ja": "顧客への請求を表す。",
                "aliases": ["インボイス"],
                "confidence": 0.9,
            }
        ],
        "relationships": [],
        "metrics": [
            {
                "metric_name_ja": "請求金額合計",
                "expression_sql": "SUM(AMOUNT)",
                "aggregation": "sum",
                "base_columns": ["APP.INVOICES.AMOUNT"],
                "unit": "円",
                "description_ja": "請求金額の合計",
                "evidence_ja": "業務説明",
                "confidence": 0.8,
            }
        ],
        "synonyms": [],
        "warnings_ja": [],
    }
    client = _FakeEnterpriseAiClient(json.dumps(invoice_extraction, ensure_ascii=False))
    legacy._enterprise_ai_client = client
    service = OntologyBuildService(runtime)
    job = service.start("sales", business_text="請求は顧客への請求金額を管理する。")
    finished = _wait_for_job(service, job.id)

    assert finished.status == OntologyBuildStatus.SUCCEEDED
    assert not any("公開オントロジー" in warning for warning in finished.warnings_ja)
    assert not any("公開オントロジー" in event.message_ja for event in finished.events)
    assert any("DB から業務プロファイル範囲" in event.message_ja for event in finished.events)
    schema_context = json.loads(client.contexts[0])
    assert {item["object"] for item in schema_context["objects"]} == {"APP.INVOICES"}
    assert runtime.list_profile_proposals("sales") == []
    state = runtime.ontology_markdown_state("sales")
    assert state.draft_revision is not None
    assert state.draft_revision.id == finished.draft_revision_id
    assert state.draft_revision.schema_fingerprint != (
        published_with_business.revision.schema_fingerprint
    )

    query_view_after, query_ontology_after = runtime.profile_view("sales")
    assert query_ontology_after.revision.id == published_with_business.revision.id
    assert query_view_after.ontology_revision_id == published_with_business.revision.id


def test_build_job_does_not_read_profile_view_for_ai_schema_input(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    """AI input の schema は DB catalog 直読みにし、profile view API へ依存しない。"""

    runtime, _store, legacy = harness
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)

    class _NoProfileViewRuntime:
        def __getattr__(self, name: str) -> Any:
            return getattr(runtime, name)

        def profile_view(self, profile_id: str) -> Any:
            raise AssertionError(f"profile_view must not be used for AI build: {profile_id}")

        def profile_view_for_build(self, profile_id: str) -> Any:
            raise AssertionError(
                f"profile_view_for_build must not be used for AI build: {profile_id}"
            )

    service = OntologyBuildService(_NoProfileViewRuntime())
    job = service.start("sales", business_text="受注は顧客に紐づく。")
    finished = _wait_for_job(service, job.id)

    assert finished.status == OntologyBuildStatus.SUCCEEDED


def test_start_returns_immediately_even_if_build_schema_context_is_slow(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    """start() は重い DB schema context 準備を待たずに job を返す。"""

    import threading as _threading

    runtime, _store, legacy = harness
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)
    release = _threading.Event()
    original_prepare_build_schema_context = runtime.prepare_build_schema_context

    class _SlowRuntime:
        def __getattr__(self, name: str) -> Any:
            return getattr(runtime, name)

        def prepare_build_schema_context(self, profile_id: str) -> Any:
            release.wait(timeout=5)
            return original_prepare_build_schema_context(profile_id)

    service = OntologyBuildService(_SlowRuntime())
    started = time.monotonic()
    job = service.start("sales", business_text="受注は顧客に紐づく。")
    elapsed = time.monotonic() - started

    try:
        # prepare_build_schema_context がブロックしていても POST(start)は即時に返る
        assert elapsed < 1.0
        assert job.status in {OntologyBuildStatus.QUEUED, OntologyBuildStatus.RUNNING}
        assert job.steps[0].name == OntologyBuildStepName.SCHEMA_CONTEXT
        # ポーリングで「スキーマ情報の準備」が実行中と観測できる
        for _ in range(100):
            snapshot = service.get(job.id)
            assert snapshot is not None
            schema_step = snapshot.steps[0]
            if schema_step.status == OntologyBuildStepStatus.RUNNING:
                break
            time.sleep(0.01)
        assert schema_step.status == OntologyBuildStepStatus.RUNNING
    finally:
        release.set()
    finished = _wait_for_job(service, job.id)
    assert finished.status == OntologyBuildStatus.SUCCEEDED


def test_accept_ignores_stale_drafts_from_previous_schema_generations(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    """過去スキーマ世代の draft が store に残っていても accept が 409 にならない。"""

    runtime, _store, legacy = harness
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)
    # 旧スキーマ世代で提案を承認して draft を作る(store に残留する)
    old_proposal = _seed_build_proposals(runtime)[0]
    runtime.accept_proposal(old_proposal.id)

    # スキーマ drift(列追加で fingerprint が変わる)→ 公開世代が変わる
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
                ),
                *legacy.catalog.tables[1:],
            ]
        },
        deep=True,
    )
    # 新世代で提案を作る
    new_proposals = _seed_build_proposals(runtime)
    assert new_proposals

    # 旧世代 draft(business 定義持ち)が store に残っていても、新提案の accept は成功する
    review = runtime.accept_proposal(new_proposals[0].id)
    assert review.draft is not None
    _view, published_graph = runtime.profile_view("sales")
    assert published_graph is not None
    published_fp = published_graph.revision.schema_fingerprint
    assert review.draft.revision.schema_fingerprint == published_fp


def test_batch_accept_creates_single_draft_for_all_proposals(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    runtime, _store, _legacy = harness
    proposals = _seed_build_proposals(runtime)
    assert len(proposals) >= 3

    accepted, draft = runtime.accept_proposals([proposal.id for proposal in proposals])

    # すべて同じ draft revision に反映され、全提案が accepted になる
    assert {proposal.status.value for proposal in accepted} == {"accepted"}
    assert {proposal.proposal_payload.values["draft_revision_id"] for proposal in accepted} == {
        draft.revision.id
    }
    # 命名提案の業務名が合成 endpoint に上書きされない
    entity_nodes = [node for node in draft.nodes if node.kind == OntologyNodeKind.BUSINESS_ENTITY]
    orders_entity = next(node for node in entity_nodes if node.technical_name == "APP.ORDERS")
    assert orders_entity.business_name_ja == "受注"
    assert any(edge.kind == OntologyEdgeKind.BUSINESS_RELATIONSHIP for edge in draft.edges)
    # publish まで通る
    published = runtime.publish_ontology_revision(
        draft.revision.id, OntologyPublishRequest(etag=draft.revision.etag)
    )
    assert published.revision.status.value == "published"


def test_reviewed_proposal_cannot_be_accepted_or_rejected_again(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    runtime, _store, _legacy = harness
    proposal = _seed_build_proposals(runtime)[0]

    review = runtime.accept_proposal(proposal.id)

    assert review.proposal.status == OntologyProposalStatus.ACCEPTED
    with pytest.raises(OntologyStateConflictError) as accept_conflict:
        runtime.accept_proposal(proposal.id)
    assert accept_conflict.value.code == "ONTOLOGY_PROPOSAL_ALREADY_REVIEWED"
    with pytest.raises(OntologyStateConflictError) as reject_conflict:
        runtime.reject_proposal(proposal.id)
    assert reject_conflict.value.code == "ONTOLOGY_PROPOSAL_ALREADY_REVIEWED"


def test_batch_accept_dedupes_proposal_ids(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    runtime, _store, _legacy = harness
    proposal = _seed_build_proposals(runtime)[0]

    accepted, draft = runtime.accept_proposals([proposal.id, proposal.id])

    assert len(accepted) == 1
    assert accepted[0].id == proposal.id
    assert accepted[0].proposal_payload.values["draft_revision_id"] == draft.revision.id


def test_rerun_replaces_markdown_draft_without_creating_proposals(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    """AI 構築を再実行すると proposal は作らず、最新 Markdown 下書きが差し替わる。"""

    runtime, store, legacy = harness
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)
    service = OntologyBuildService(runtime)

    first = _wait_for_job(service, service.start("sales", business_text="受注は顧客に紐づく。").id)
    assert first.proposal_ids == []
    first_state = runtime.ontology_markdown_state("sales")
    assert first_state.draft_revision is not None
    assert first_state.draft_revision.id == first.draft_revision_id

    second = _wait_for_job(service, service.start("sales", business_text="受注は顧客に紐づく。").id)
    assert second.status == OntologyBuildStatus.SUCCEEDED
    assert second.proposal_ids == []
    second_state = runtime.ontology_markdown_state("sales")
    assert second_state.draft_revision is not None
    assert second_state.draft_revision.id == second.draft_revision_id
    assert second_state.draft_revision.id != first_state.draft_revision.id
    assert second_state.draft_markdown == second.markdown_output
    assert runtime.list_profile_proposals("sales") == []

    # 再起動(同じ store)でも proposal は復活せず、最新 Draft を読める。
    restarted = OntologyApiRuntime(legacy_service=legacy, store=store)
    assert restarted.list_profile_proposals("sales") == []
    assert restarted.ontology_markdown_state("sales").draft_revision is not None


def test_start_prunes_oldest_finished_jobs(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    from datetime import timedelta

    from app.features.nl2sql.ontology_build import _MAX_FINISHED_JOBS
    from app.features.nl2sql.ontology_models import OntologyBuildJob, utc_now

    runtime, _store, legacy = harness
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)
    service = OntologyBuildService(runtime)

    # 完了 job を上限 +2 件、実行中 job を 1 件直接注入する(実 job を回すと遅いため)。
    base = utc_now()
    for index in range(_MAX_FINISHED_JOBS + 2):
        job = OntologyBuildJob(
            id=f"ontology_build_old_{index:03d}",
            profile_id="sales",
            status=OntologyBuildStatus.SUCCEEDED,
            finished_at=base + timedelta(seconds=index),
        )
        service._jobs[job.id] = job
    running = OntologyBuildJob(
        id="ontology_build_running",
        profile_id="support",
        status=OntologyBuildStatus.RUNNING,
    )
    service._jobs[running.id] = running

    started = service.start("sales", business_text="受注は顧客に紐づく。")

    # 最古の完了 2 件だけが prune され、実行中・新規 job は保護される。
    assert service.get("ontology_build_old_000") is None
    assert service.get("ontology_build_old_001") is None
    assert service.get(f"ontology_build_old_{_MAX_FINISHED_JOBS + 1:03d}") is not None
    assert service.get(running.id) is not None
    assert service.get(started.id) is not None
    # worker thread の終了を待って teardown を安定させる。
    _wait_for_job(service, started.id)


def test_external_worker_rehydrates_persisted_build_input(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, store, legacy = harness
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    api_service = OntologyBuildService(runtime)
    queued = api_service.start(
        "sales",
        business_text="受注は顧客に紐づく。",
        qa_pairs=[QaPair(question="顧客別売上", sql=_QA_SQL)],
        idempotency_key="external-build-1",
    )
    assert queued.status == OntologyBuildStatus.QUEUED
    persisted = store.get_job(queued.id)
    assert persisted is not None
    assert persisted["input_payload"]["business_text"] == "受注は顧客に紐づく。"

    worker_service = OntologyBuildService(runtime)
    finished = worker_service.run_persisted(queued.id)
    assert finished.status == OntologyBuildStatus.SUCCEEDED
    assert finished.proposal_ids == []
    assert finished.draft_revision_id
    assert runtime.ontology_markdown_state("sales").draft_revision is not None


def test_start_persists_business_text_and_source_document_refs(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, store, _legacy = harness
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    service = OntologyBuildService(runtime)
    source_documents = [
        OntologySourceDocument(
            id="ontology_source_rules",
            profile_id="sales",
            filename="rules.md",
            media_type="text/markdown",
            size_bytes=12,
            sha256="0" * 64,
            storage_uri="/tmp/rules.md",
        ),
        OntologySourceDocument(
            id="ontology_source_qa",
            profile_id="sales",
            filename="qa_cases.csv",
            media_type="text/csv",
            size_bytes=48,
            sha256="1" * 64,
            storage_uri="/tmp/qa_cases.csv",
        ),
    ]

    queued = service.start(
        "sales",
        business_text="受注は顧客に紐づく。",
        run_qa_extraction=True,
        run_text_extraction=True,
        source_documents=source_documents,
        idempotency_key="persist-build-inputs-1",
    )

    assert queued.source_document_ids == ["ontology_source_rules", "ontology_source_qa"]
    assert [source.filename for source in queued.sources] == ["rules.md", "qa_cases.csv"]
    persisted = store.get_job(queued.id)
    assert persisted is not None
    assert persisted["input_payload"]["business_text"] == "受注は顧客に紐づく。"
    assert persisted["payload"]["source_document_ids"] == [
        "ontology_source_rules",
        "ontology_source_qa",
    ]
    stored_sources = store.list_documents("source_documents", {"profile_id": "sales"})
    assert {document["payload"]["filename"] for document in stored_sources} == {
        "rules.md",
        "qa_cases.csv",
    }


def test_start_idempotency_mismatch_raises_domain_conflict(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _store, _legacy = harness
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    service = OntologyBuildService(runtime)
    service.start(
        "sales",
        business_text="受注は顧客に紐づく。",
        idempotency_key="build-idempotency-conflict",
    )

    with pytest.raises(OntologyVersionConflictError) as exc_info:
        service.start(
            "sales",
            business_text="顧客は受注に紐づく。",
            idempotency_key="build-idempotency-conflict",
        )

    assert exc_info.value.code == "IDEMPOTENCY_KEY_REUSED"


def test_discard_source_documents_removes_only_requested_sources(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    runtime, store, _legacy = harness
    content_a = b"# rules"
    content_b = b"# keep"
    source_a = _source_document("ontology_source_discard", "discard.md", content_a)
    source_b = _source_document("ontology_source_keep", "keep.md", content_b)
    storage = _FakeOntologySourceStorage(
        {
            source_a.id: content_a,
            source_b.id: content_b,
        }
    )
    service = OntologyBuildService(runtime, source_storage=storage)  # type: ignore[arg-type]
    service._save_source_document(source_a)  # noqa: SLF001 - cleanup contract setup
    service._save_source_document(source_b)  # noqa: SLF001 - cleanup contract setup

    deleted = service.discard_source_documents([source_a])

    assert deleted == 1
    assert storage.deleted == [source_a.id]
    assert store.get_document("source_documents", {"source_document_id": source_a.id}) is None
    assert store.get_document("source_documents", {"source_document_id": source_b.id}) is not None
    assert source_b.id in storage.contents


# --- Phase 3: 部分成功・再試行・構造化コード・出力予算・provenance ---------------------------


class _SelectiveFailClient(_FakeEnterpriseAiClient):
    """特定の目印を含む呼び出しだけ壊れた応答を返す fake client。"""

    def __init__(self, payload: str, *, fail_when: str) -> None:
        super().__init__(payload)
        self.fail_when = fail_when

    def generate(
        self,
        *,
        prompt: str,
        context: str,
        system_prompt: str,
        response_format: object | None = None,
    ) -> str:
        self.calls.append(prompt)
        self.contexts.append(context)
        if self.fail_when in prompt or self.fail_when in context:
            return "これは JSON ではありません"
        return self.payload


class _BudgetRecordingClient(_FakeEnterpriseAiClient):
    """max_output_tokens 対応の fake client(受け取った予算を記録する)。"""

    def __init__(self, payload: str) -> None:
        super().__init__(payload)
        self.budgets: list[int | None] = []

    def generate(
        self,
        *,
        prompt: str,
        context: str,
        system_prompt: str,
        max_output_tokens: int | None = None,
        response_format: object | None = None,
    ) -> str:
        self.calls.append(prompt)
        self.budgets.append(max_output_tokens)
        return self.payload


def test_partial_batch_failure_completes_with_warnings(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    """1 batch の失敗で全成果を破棄しない(部分成功 = succeeded_with_warnings)。"""

    runtime, _store, legacy = harness
    # qa_pairs を含む呼び出しだけ失敗し、schema_naming は成功する
    legacy._enterprise_ai_client = _SelectiveFailClient(_STRUCTURED_PAYLOAD, fail_when="qa_pairs")
    service = OntologyBuildService(runtime)
    job = service.start(
        "sales",
        qa_pairs=[QaPair(question="顧客別の売上は?", sql=_QA_SQL)],
        run_text_extraction=False,
    )
    finished = _wait_for_job(service, job.id)

    assert finished.status == OntologyBuildStatus.SUCCEEDED_WITH_WARNINGS
    assert finished.draft_revision_id
    assert any("qa_extraction" in warning for warning in finished.warnings_ja)
    qa_step = next(
        step for step in finished.steps if step.name == OntologyBuildStepName.QA_EXTRACTION
    )
    assert qa_step.code == "BATCH_EXTRACTION_FAILED"
    naming_step = next(
        step for step in finished.steps if step.name == OntologyBuildStepName.SCHEMA_NAMING
    )
    assert naming_step.status == OntologyBuildStepStatus.SUCCEEDED


def test_batch_split_recovers_good_half(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    """壊れた行を含む batch は二分割し、正常な半分の成果を保存する。"""

    runtime, _store, legacy = harness
    legacy._enterprise_ai_client = _SelectiveFailClient(_STRUCTURED_PAYLOAD, fail_when="毒入り質問")
    service = OntologyBuildService(runtime)
    job = service.start(
        "sales",
        qa_pairs=[
            QaPair(question="顧客別の売上は?", sql=_QA_SQL),
            QaPair(question="毒入り質問", sql=_QA_SQL),
        ],
        run_schema_naming=False,
        run_text_extraction=False,
    )
    finished = _wait_for_job(service, job.id)

    assert finished.status == OntologyBuildStatus.SUCCEEDED_WITH_WARNINGS
    assert finished.draft_revision_id
    assert any(event.code == "EXTRACTION_SPLIT" for event in finished.events)
    assert any("qa_extraction" in warning for warning in finished.warnings_ja)


def test_all_batches_failed_marks_job_failed_with_code(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    runtime, _store, legacy = harness
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient("これは JSON ではありません")
    service = OntologyBuildService(runtime)
    job = service.start("sales", business_text="受注は顧客に紐づく。", run_schema_naming=False)
    finished = _wait_for_job(service, job.id)

    assert finished.status == OntologyBuildStatus.FAILED
    assert finished.error_code == "LLM_EXTRACTION_FAILED"


def test_schema_scope_failure_sets_error_codes(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _store, legacy = harness
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)
    from app.features.nl2sql.ontology_build import OntologyBuildSchemaContext

    monkeypatch.setattr(
        runtime,
        "prepare_build_schema_context",
        lambda _profile_id: OntologyBuildSchemaContext(
            schema_context="",
            object_count=0,
            column_count=0,
            schema_fingerprint="fp",
        ),
    )
    service = OntologyBuildService(runtime)
    job = service.start("sales", business_text="テスト", run_schema_naming=False)
    finished = _wait_for_job(service, job.id)

    assert finished.status == OntologyBuildStatus.FAILED
    assert finished.error_code == "SCHEMA_SCOPE_UNRESOLVED"
    schema_step = next(
        step for step in finished.steps if step.name == OntologyBuildStepName.SCHEMA_CONTEXT
    )
    assert schema_step.code == "SCHEMA_SCOPE_EMPTY"


def test_extraction_budget_passed_to_supporting_client(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    """max_output_tokens 対応クライアントには抽出用の出力予算を渡す。"""

    runtime, _store, legacy = harness
    client = _BudgetRecordingClient(_STRUCTURED_PAYLOAD)
    legacy._enterprise_ai_client = client
    service = OntologyBuildService(runtime)
    job = service.start("sales", business_text="受注は顧客に紐づく。", run_schema_naming=False)
    finished = _wait_for_job(service, job.id)

    assert finished.status == OntologyBuildStatus.SUCCEEDED
    from app.settings import get_settings

    expected = int(get_settings().nl2sql_ontology_extraction_max_output_tokens)
    assert client.budgets
    assert all(budget == expected for budget in client.budgets)


def test_build_draft_preserves_inferred_provenance(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    """AI 構築由来の provenance(INFERRED + ontology_build:<job_id>)を draft 作成で保持する。"""

    runtime, _store, legacy = harness
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)
    service = OntologyBuildService(runtime)
    job = service.start("sales", business_text="受注は顧客に紐づく。", run_schema_naming=True)
    finished = _wait_for_job(service, job.id)

    assert finished.status == OntologyBuildStatus.SUCCEEDED
    draft = runtime.ontology_revision(finished.draft_revision_id)
    business_nodes = [
        node
        for node in draft.nodes
        if node.kind == OntologyNodeKind.OBJECT_TYPE and node.business_name_ja == "受注"
    ]
    assert business_nodes, "AI 構築由来の業務エンティティが draft に存在すること"
    provenance = business_nodes[0].provenance
    assert provenance.source_kind == OntologySourceKind.INFERRED
    assert provenance.source_id == f"ontology_build:{finished.id}"


def test_purge_profile_source_documents_removes_rows_and_blobs(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    tmp_path: Path,
) -> None:
    """Profile 削除時の cleanup で source_documents 行とローカル実体を残さない。"""

    runtime, store, _legacy = harness
    blob = tmp_path / "sources" / "doc" / "rules.md"
    blob.parent.mkdir(parents=True)
    blob.write_text("# 業務ルール", encoding="utf-8")
    document = OntologySourceDocument(
        id="ontology_source_purge",
        profile_id="sales",
        filename="rules.md",
        media_type="text/markdown",
        size_bytes=blob.stat().st_size,
        sha256="0" * 64,
        storage_uri=str(blob),
    )
    store.save_document(
        "source_documents",
        {
            "source_document_id": document.id,
            "profile_id": document.profile_id,
            "status": document.status.value,
            "sha256": document.sha256,
            "payload": document.model_dump(mode="json"),
        },
    )
    service = OntologyBuildService(runtime)

    removed = service.purge_profile_source_documents("sales")

    assert removed == 1
    assert not blob.exists()
    assert store.list_documents("source_documents", {"profile_id": "sales"}) == []


# --- Best-practice 適用: gleaning・部分成功(資料)・バッチ本文上限 ---------------------------


class _GleaningAwareClient(_FakeEnterpriseAiClient):
    """通常呼び出しと gleaning(追加パス)呼び出しで別の応答を返す fake client。"""

    def __init__(self, payload: str, gleaning_payload: str) -> None:
        super().__init__(payload)
        self.gleaning_payload = gleaning_payload
        self.gleaning_calls = 0

    def generate(
        self,
        *,
        prompt: str,
        context: str,
        system_prompt: str,
        response_format: object | None = None,
    ) -> str:
        self.calls.append(prompt)
        self.contexts.append(context)
        if "追加パス" in prompt:
            self.gleaning_calls += 1
            return self.gleaning_payload
        return self.payload


_GLEANING_ADDITION = json.dumps(
    {
        "entities": [],
        "relationships": [],
        "metrics": [
            {
                "metric_name_ja": "受注件数",
                "expression_sql": "COUNT(APP.ORDERS.ID)",
                "aggregation": "count",
                "base_columns": ["APP.ORDERS.ID"],
                "unit": "件",
                "description_ja": "受注の件数",
                "evidence_ja": "追加パスで回収",
                "confidence": 0.7,
            }
        ],
        "synonyms": [],
        "warnings_ja": [],
    },
    ensure_ascii=False,
)


def test_gleaning_pass_recovers_missed_candidates(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    runtime, _store, legacy = harness
    client = _GleaningAwareClient(_STRUCTURED_PAYLOAD, _GLEANING_ADDITION)
    legacy._enterprise_ai_client = client
    service = OntologyBuildService(runtime)
    job = service.start("sales", business_text="受注は顧客に紐づく。", run_schema_naming=False)
    finished = _wait_for_job(service, job.id)

    assert finished.status == OntologyBuildStatus.SUCCEEDED
    assert client.gleaning_calls >= 1
    assert any(event.code == "EXTRACTION_GLEANING" for event in finished.events)
    # 追加パスで回収した指標が Markdown 下書きに含まれる
    assert "受注件数" in (finished.markdown_output or "")


def test_gleaning_failure_keeps_primary_extraction(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    runtime, _store, legacy = harness
    client = _GleaningAwareClient(_STRUCTURED_PAYLOAD, "これは JSON ではありません")
    legacy._enterprise_ai_client = client
    service = OntologyBuildService(runtime)
    job = service.start("sales", business_text="受注は顧客に紐づく。", run_schema_naming=False)
    finished = _wait_for_job(service, job.id)

    # gleaning の失敗は本体の抽出結果に影響しない
    assert finished.status == OntologyBuildStatus.SUCCEEDED
    assert finished.draft_revision_id


def test_merge_build_extractions_dedupes_by_stable_keys() -> None:
    from app.features.nl2sql.ontology_build import merge_build_extractions, parse_extraction

    base = parse_extraction(_STRUCTURED_PAYLOAD)
    duplicated, added_dup = merge_build_extractions(base, base)
    assert added_dup == 0
    assert len(duplicated.entities) == len(base.entities)

    merged, added = merge_build_extractions(base, parse_extraction(_GLEANING_ADDITION))
    assert added == 1
    assert any(metric.metric_name_ja == "受注件数" for metric in merged.metrics)


def test_partial_source_failure_continues_with_warnings(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    """1 資料の抽出失敗はジョブを止めず、残り資料で succeeded_with_warnings になる。"""

    runtime, _store, legacy = harness
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)
    good = "受注は顧客に紐づく。".encode()
    broken = b"not-a-workbook"
    good_source = _source_document("ontology_source_good", "rules.md", good)
    broken_source = _source_document(
        "ontology_source_broken",
        "broken.xlsx",
        broken,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    service = OntologyBuildService(
        runtime,
        source_storage=_FakeOntologySourceStorage(
            {"ontology_source_good": good, "ontology_source_broken": broken}
        ),  # type: ignore[arg-type]
    )
    job = service.start(
        "sales",
        run_schema_naming=False,
        run_qa_extraction=False,
        source_documents=[good_source, broken_source],
    )
    finished = _wait_for_job(service, job.id)

    assert finished.status == OntologyBuildStatus.SUCCEEDED_WITH_WARNINGS
    assert finished.draft_revision_id
    source_step = next(
        step for step in finished.steps if step.name == OntologyBuildStepName.SOURCE_EXTRACTION
    )
    assert source_step.code == "SOURCE_PARTIAL_FAILED"
    assert any("broken.xlsx" in warning for warning in finished.warnings_ja)
    broken_progress = next(item for item in finished.sources if item.filename == "broken.xlsx")
    assert broken_progress.status == OntologySourceStatus.FAILED


def test_batch_max_chars_limits_content_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.features.nl2sql.ontology_build import _batch_text_units, _BuildTextUnit
    from app.features.nl2sql.ontology_models import OntologyEvidenceLocatorKind

    monkeypatch.setattr(get_settings(), "nl2sql_ontology_extraction_batch_max_chars", 1000)
    schema_payload = {"objects": []}  # type: ignore[var-annotated]
    units = [
        _BuildTextUnit(
            source_document=None,
            source_label="doc",
            locator_kind=OntologyEvidenceLocatorKind.LINE,
            locator=f"line:{index}",
            text="受注データの説明 " * 50,  # 約 450 字/чанк
        )
        for index in range(1, 7)
    ]
    batches = _batch_text_units(schema_payload, units, "抽出してください。")
    assert len(batches) >= 3  # 1000 字上限なら 2 chunk/batch 程度に分かれる
    for batch in batches:
        assert sum(len(unit.text) for unit in batch) <= 1000


def test_shutdown_interrupts_owned_job_and_late_worker_cannot_revive_it(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import threading

    runtime, store, _legacy = harness
    service = OntologyBuildService(runtime)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def blocked_run(job_id: str, _text: str, _pairs: list[QaPair]) -> None:
        service._update(job_id, lambda job: setattr(job, "status", OntologyBuildStatus.RUNNING))
        service._set_definition_phase(job_id, "save", "running")
        entered.set()
        try:
            assert release.wait(5)
            service._update(
                job_id, lambda job: setattr(job, "status", OntologyBuildStatus.SUCCEEDED)
            )
        finally:
            finished.set()

    monkeypatch.setattr(service, "_run_safely", blocked_run)
    job = service.start("sales")
    try:
        assert entered.wait(5)
        reader = OntologyBuildService(runtime)
        assert reader.get(job.id) is not None
        reader.shutdown()
        record = store.get_document("jobs", {"job_id": job.id})
        assert record is not None
        assert record["status"] == "running"
        service.shutdown()
        record = store.get_document("jobs", {"job_id": job.id})
        assert record is not None
        assert record["status"] == "cancelled"
        assert record["payload"]["error_code"] == "ONTOLOGY_BUILD_PROCESS_STOPPED"
        assert record["payload"]["finished_at"]
        assert all(p["status"] == "skipped" for p in record["payload"]["definition_phases"])
        assert "再実行してください" in record["payload"]["error_message_ja"]
        assert any(
            getattr(r, "event", "") == "ontology_build_process_stopped" for r in caplog.records
        )
    finally:
        release.set()
        assert finished.wait(5)
    record = store.get_document("jobs", {"job_id": job.id})
    assert record is not None
    assert record["status"] == "cancelled"


def test_shutdown_keeps_external_job_and_finished_job(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, store, _legacy = harness
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    service = OntologyBuildService(runtime)
    job = service.start("sales")
    service.shutdown()
    record = store.get_document("jobs", {"job_id": job.id})
    assert record is not None
    assert record["status"] == "queued"
    service._update(job.id, lambda item: setattr(item, "status", OntologyBuildStatus.SUCCEEDED))
    service._inprocess_jobs.add(job.id)
    service.shutdown()
    record = store.get_document("jobs", {"job_id": job.id})
    assert record is not None
    assert record["status"] == "succeeded"


def test_shutdown_retries_concurrent_progress_write(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, store, _legacy = harness
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    service = OntologyBuildService(runtime)
    job = service.start("sales")
    service._inprocess_jobs.add(job.id)
    original = service._save_cancelled
    calls = 0

    def conflict_once(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OntologyVersionConflict("concurrent progress")
        original(*args, **kwargs)

    monkeypatch.setattr(service, "_save_cancelled", conflict_once)
    service.shutdown()
    assert calls == 2
    record = store.get_document("jobs", {"job_id": job.id})
    assert record is not None
    assert record["status"] == "cancelled"


def test_restored_inprocess_job_refreshes_after_independent_worker_completes(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, store, _legacy = harness
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    writer = OntologyBuildService(runtime)
    job = writer.start("sales")
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "inprocess")
    reader = OntologyBuildService(runtime)
    snapshot = reader.get(job.id)
    assert snapshot is not None
    assert snapshot.status == OntologyBuildStatus.QUEUED
    record = store.get_document("jobs", {"job_id": job.id})
    assert record is not None
    record["status"] = "succeeded"
    record["payload"]["status"] = "succeeded"
    store.save_document("jobs", record, expected_etag=record["etag"])
    snapshot = reader.get(job.id)
    assert snapshot is not None
    assert snapshot.status == OntologyBuildStatus.SUCCEEDED


def test_source_deleted_during_extraction_is_not_resurrected(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    """抽出中に利用者が資料を削除しても、進捗更新で source_documents 行を復活させない。"""

    runtime, store, legacy = harness
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)
    content = "受注は顧客ごとに管理する。".encode()
    source = _source_document("ontology_source_deleted", "deleted.md", content)
    holder: dict[str, OntologyBuildService] = {}

    class _DeletingStorage(_FakeOntologySourceStorage):
        def load(self, document: OntologySourceDocument) -> bytes:
            data = super().load(document)
            holder["service"].delete_profile_source_document("sales", document.id)
            return data

    service = OntologyBuildService(
        runtime,
        source_storage=_DeletingStorage({source.id: content}),  # type: ignore[arg-type]
    )
    holder["service"] = service
    job = service.start(
        "sales",
        business_text="受注は顧客に紐づく。",
        run_schema_naming=False,
        run_qa_extraction=False,
        source_documents=[source],
    )
    finished = _wait_for_job(service, job.id)

    assert store.get_document("source_documents", {"source_document_id": source.id}) is None
    assert service.list_profile_source_documents("sales") == []
    progress = next(item for item in finished.sources if item.source_document_id == source.id)
    assert progress.status == OntologySourceStatus.FAILED
    assert "削除" in progress.error_message_ja
    # 削除済み資料の証拠は構築に混ぜず、他の入力(業務説明)で継続する
    assert finished.status == OntologyBuildStatus.SUCCEEDED_WITH_WARNINGS


def test_source_deleted_before_its_turn_is_skipped_and_job_continues(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
) -> None:
    """後続資料が実行中に削除されても、ジョブ全体を失敗させず残りの資料で継続する。"""

    runtime, store, legacy = harness
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(_STRUCTURED_PAYLOAD)
    first_content = "受注は顧客ごとに管理する。".encode()
    second_content = "顧客は地域に属する。".encode()
    first = _source_document("ontology_source_first", "first.md", first_content)
    second = _source_document("ontology_source_second", "second.md", second_content)
    holder: dict[str, OntologyBuildService] = {}

    class _DeletingNextStorage(_FakeOntologySourceStorage):
        def load(self, document: OntologySourceDocument) -> bytes:
            data = super().load(document)
            if document.id == first.id:
                holder["service"].delete_profile_source_document("sales", second.id)
            return data

    service = OntologyBuildService(
        runtime,
        source_storage=_DeletingNextStorage(  # type: ignore[arg-type]
            {first.id: first_content, second.id: second_content}
        ),
    )
    holder["service"] = service
    job = service.start(
        "sales",
        run_schema_naming=False,
        run_qa_extraction=False,
        source_documents=[first, second],
    )
    finished = _wait_for_job(service, job.id)

    assert finished.status == OntologyBuildStatus.SUCCEEDED_WITH_WARNINGS
    progress = {item.source_document_id: item.status for item in finished.sources}
    assert progress == {
        first.id: OntologySourceStatus.EXTRACTED,
        second.id: OntologySourceStatus.FAILED,
    }
    assert any("second.md" in warning for warning in finished.warnings_ja)
    assert store.get_document("source_documents", {"source_document_id": second.id}) is None
    # ジョブは終了の状態を保存してから profile のロックを外すため、終了を見た直後はまだ
    # ロックが残っていることがある（負荷が高いと並列実行で起きる。#344）。外れるまで待つ。
    for _ in range(500):
        if store.get_idempotency("build_ontology_profile_active", "sales") is None:
            break
        time.sleep(0.01)
    assert store.get_idempotency("build_ontology_profile_active", "sales") is None


def test_unexpected_failure_terminalizes_running_steps_and_phases(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """予期しない例外で FAILED にするとき、running/pending の step・phase も終端化する。"""

    runtime, _store, _legacy = harness
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    service = OntologyBuildService(runtime)
    job = service.start("sales", business_text="販売業務")
    first_step = job.steps[0].name

    def exploding_run(job_id: str, _text: str, _pairs: list[QaPair]) -> None:
        service._update(job_id, lambda item: setattr(item, "status", OntologyBuildStatus.RUNNING))
        service._set_definition_phase(job_id, "freeze", "running")
        service._set_step(job_id, first_step, OntologyBuildStepStatus.RUNNING, "処理中")
        raise ValueError("boom")

    monkeypatch.setattr(service, "_run", exploding_run)
    failed = service.run_persisted(job.id)

    assert failed.status == OntologyBuildStatus.FAILED
    assert failed.finished_at is not None
    assert "boom" in failed.error_message_ja
    assert not [
        step
        for step in failed.steps
        if step.status in {OntologyBuildStepStatus.PENDING, OntologyBuildStepStatus.RUNNING}
    ]
    assert all(step.finished_at is not None for step in failed.steps)
    assert not [
        phase for phase in failed.definition_phases if phase.status in {"pending", "running"}
    ]
    freeze = next(phase for phase in failed.definition_phases if phase.name == "freeze")
    assert freeze.status == "failed"


def test_cancel_profile_jobs_retries_concurrent_progress_write(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Profile 削除の一括中止は、実行中 job の進捗書込と ETag 競合しても再読して中止する。"""

    runtime, store, _legacy = harness
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    service = OntologyBuildService(runtime)
    job = service.start("sales", business_text="販売業務")
    original = service._save_cancelled
    calls = 0

    def conflict_once(document: dict[str, Any], *args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            # 一覧取得後に worker が進捗を書いた状態を再現する(ETag が進む)
            store.save_document(
                "jobs",
                {
                    key: value
                    for key, value in document.items()
                    if key not in {"etag", "created_at", "updated_at"}
                },
                expected_etag=str(document["etag"]),
            )
            raise OntologyVersionConflict("concurrent progress")
        original(document, *args, **kwargs)

    monkeypatch.setattr(service, "_save_cancelled", conflict_once)

    assert service.cancel_profile_jobs("sales") == 1
    assert calls == 2
    record = store.get_document("jobs", {"job_id": job.id})
    assert record is not None
    assert record["status"] == "cancelled"


# ---- 上限つきの並行（#2004） --------------------------------------------------------------


class _ThrottledEnterpriseAiClient(_FakeEnterpriseAiClient):
    """thread safe の偽の client。最初の ``failures`` 回は ``status`` の HTTP の失敗を返す。"""

    def __init__(self, payload: str, *, failures: int = 0, status: int = 429) -> None:
        super().__init__(payload)
        self.failures = failures
        self.status = status
        self.failed = 0
        self.lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0
        self.max_retries: list[object] = []
        self.delay = 0.02

    def generate(
        self,
        *,
        prompt: str,
        context: str,
        system_prompt: str,
        response_format: object | None = None,
        max_retries: int | None = None,
    ) -> str:
        from pr_system_settings.enterprise_ai_errors import classify_enterprise_ai_http_failure

        from app.features.nl2sql.enterprise_ai_client import EnterpriseAiDirectError

        with self.lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            self.max_retries.append(max_retries)
            fail = self.failed < self.failures
            if fail:
                self.failed += 1
        try:
            time.sleep(self.delay)
            if fail:
                raise EnterpriseAiDirectError(
                    f"OCI Enterprise AI HTTP {self.status}",
                    code="http",
                    http_failure=classify_enterprise_ai_http_failure(self.status, ""),
                    headers={"retry-after": "0"},
                )
            with self.lock:
                self.calls.append(prompt)
                self.contexts.append(context)
            return self.payload
        finally:
            with self.lock:
                self.in_flight -= 1


def _many_sources(count: int) -> tuple[dict[str, bytes], list[OntologySourceDocument]]:
    contents: dict[str, bytes] = {}
    documents: list[OntologySourceDocument] = []
    for index in range(count):
        content = (f"SOURCE_{index} " + ("受注は顧客に紐づく。" * 300)).encode()
        source_id = f"ontology_source_{index}"
        contents[source_id] = content
        documents.append(_source_document(source_id, f"source-{index}.md", content))
    return contents, documents


def _run_build(
    runtime: OntologyApiRuntime,
    contents: dict[str, bytes],
    documents: list[OntologySourceDocument],
) -> OntologyBuildJob:
    service = OntologyBuildService(
        runtime,
        source_storage=_FakeOntologySourceStorage(contents),  # type: ignore[arg-type]
    )
    queued = service.start(
        "sales", run_schema_naming=False, run_qa_extraction=False, source_documents=documents
    )
    return service.run_persisted(queued.id)


@pytest.fixture
def _no_ai_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.features.nl2sql import ai_calls

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(ai_calls, "_sleep", no_wait)
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_extraction_gleaning_passes", 0)
    monkeypatch.setattr(
        ontology_build_module,
        "_ONTOLOGY_BUILD_LLM_CONTEXT_MAX_CHARS",
        len(ontology_build_module._EXTRACTION_SYSTEM_PROMPT) + 4_000,
    )


def test_build_extraction_runs_bounded_and_429_waits_globally(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
    _no_ai_wait: None,
) -> None:
    """抽出の組を上限まで同時に呼び、429 は実行器が再試行して、組の再試行・分割をしない。"""
    runtime, _store, legacy = harness
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_build_concurrency", 3)
    client = _ThrottledEnterpriseAiClient(_STRUCTURED_PAYLOAD, failures=2)
    legacy._enterprise_ai_client = client
    contents, documents = _many_sources(6)

    finished = _run_build(runtime, contents, documents)

    assert finished.status == OntologyBuildStatus.SUCCEEDED
    assert len(client.calls) >= 4
    assert 2 <= client.peak <= 3
    assert client.failed == 2
    # 再試行は実行器が受け持ち（client の再試行は切る）、組の再試行・分割にはならない。
    assert set(client.max_retries) == {0}
    codes = {event.code for event in finished.events}
    assert "EXTRACTION_RETRY" not in codes
    assert "EXTRACTION_SPLIT" not in codes


def test_build_permission_error_stops_job_without_retry(
    harness: tuple[OntologyApiRuntime, InMemoryOntologyStore, _FakeLegacyNl2SqlService],
    monkeypatch: pytest.MonkeyPatch,
    _no_ai_wait: None,
) -> None:
    """権限の誤り（403）は再試行・分割をせず、残りの組を呼ばずに job を失敗にする。"""
    runtime, _store, legacy = harness
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_build_concurrency", 1)
    client = _ThrottledEnterpriseAiClient(_STRUCTURED_PAYLOAD, failures=99, status=403)
    legacy._enterprise_ai_client = client
    contents, documents = _many_sources(4)

    finished = _run_build(runtime, contents, documents)

    assert finished.status == OntologyBuildStatus.FAILED
    assert finished.error_code == "LLM_CALL_ABORTED"
    assert finished.error_message_ja and "権限" in finished.error_message_ja
    assert client.failed == 1
    assert client.calls == []
