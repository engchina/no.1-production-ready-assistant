"""Ontology を利用する NL2SQL query-session API。

既存 ``/preview`` / ``/execute`` を互換 API として残しつつ、新 UI はこの router の
二段階確認フローを利用する。router 自体は独立しており、application root 側では
``ontology_router.router`` を include するだけでよい。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from threading import RLock
from typing import Annotated, Any, Literal, NoReturn, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from pr_backend_core import ApiResponse
from pr_backend_core.observability.model_purpose import call_with_model_purpose
from pr_backend_core.observability.model_usage import RunModelUsage, collect_model_usage
from pr_backend_core.timezones import local_today
from pr_system_settings.auth.visibility import (
    OTHERS_CONTENT_FORBIDDEN_MESSAGE,
    OTHERS_CONTENT_FORBIDDEN_STATUS,
    OWNER_NOT_FOUND_STATUS,
    can_access_user_data,
    can_add_user_content,
    can_view_all_users_data,
)
from pydantic import Field, ValidationError, model_validator

from app.api.concurrency import run_sync_io
from app.security.domain import Principal
from app.security.permissions import PROFILE_MANAGE_PERMISSION, SQL_EXECUTE_PERMISSION
from app.security.request_actor import actor_scope
from app.settings import get_settings

from .chat_attachments import AttachmentContext
from .models import (
    AllowedObjects,
    ExplainPlanData,
    JobCreateRequest,
    Nl2SqlEngine,
    Nl2SqlProfile,
    PreviewData,
    PreviewRequest,
    QueryResults,
    SqlRequirementSpec,
    StoredNl2SqlEngine,
)
from .object_identity import catalog_match_key, object_match_key, object_name_tokens
from .ontology_build import (
    OntologyBuildService,
    build_schema_context_from_catalog,
    qa_sql_patterns_from_markdown,
    qa_sql_patterns_from_pairs,
    select_qa_sql_examples_from_markdown,
)
from .ontology_catalog import (
    SchemaOntology,
    build_schema_ontology,
    evolve_schema_ontology,
    find_bounded_shortest_paths,
    interpret_question_deterministically,
    migrate_profile_ontology_view,
    retrieve_ontology_nodes,
)
from .ontology_markdown_router import create_markdown_router
from .ontology_mermaid import render_mermaid_er
from .ontology_models import (
    ClarificationMode,
    ColumnQueryPolicy,
    GraphPatch,
    MetricDefinition,
    OntologyBuildJob,
    OntologyContextHit,
    OntologyContextSearchResult,
    OntologyContract,
    OntologyEdge,
    OntologyEdgeKind,
    OntologyNode,
    OntologyNodeKind,
    OntologyProposal,
    OntologyProposalKind,
    OntologyProposalPayload,
    OntologyProposalStatus,
    OntologyPublishJob,
    OntologyReasoningStatus,
    OntologyReviewStatus,
    OntologyRevision,
    OntologyRevisionStatus,
    OntologySourceDocument,
    OntologySourceKind,
    OntologySourceRole,
    OntologySourceStatus,
    OntologySqlGenerationContext,
    ProfileOntologyView,
    ProfileRecommendation,
    ProfileRecommendationCandidateV2,
    QuerySession,
    QuerySessionCreate,
    QuerySessionStatus,
    QuestionIntentGraph,
    RequirementAnswerRecord,
    RequirementConversationTurn,
    RequirementState,
    SqlConfirmationRequest,
    utc_now,
)
from .ontology_observability import (
    observe_stage,
    record_clarification,
    record_context_hits,
    record_findings,
    record_profile_recommendation,
    record_transition,
)
from .ontology_reasoning import OntologyPublishService
from .ontology_semantics import build_semantic_artifacts
from .ontology_service import (
    QUERY_SESSION_EXPIRED_CODE,
    QUERY_SESSION_EXPIRED_MESSAGE_JA,
    OntologyGateBlockedError,
    OntologyIntegrityError,
    OntologyNotFoundError,
    OntologyQuerySessionService,
    OntologyServiceError,
    OntologyStateConflictError,
    OntologyVersionConflictError,
)
from .ontology_sources import OntologySourceStorage
from .ontology_store import (
    IDEMPOTENCY_KEY_STORAGE_MAX_BYTES,
    InMemoryOntologyStore,
    OntologyCollection,
    OntologyStore,
    OntologyVersionConflict,
    OracleOntologyStore,
    canonical_json,
    compute_etag,
    physical_identity_part,
    stable_ontology_id,
    stable_physical_id,
)
from .profile_access import assert_profile_access
from .service import OthersContentForbiddenError, nl2sql_service
from .sql_requirement import (
    RequirementAiUnavailableError,
    RequirementAnswerInvalidError,
    RequirementContext,
    added_item_section,
    apply_answer,
    build_draft,
    build_requirement_context,
    build_requirement_state,
    call_requirement_llm,
    generation_spec,
    unresolved_items,
)
from .structured_outputs import format_schema, strict_schema

logger = logging.getLogger(__name__)

# 操作が途絶えた query session の掃除（#1274）。実行中（executing）と終了済みは対象にしない。
_EXPIRABLE_QUERY_SESSION_STATUSES = (
    QuerySessionStatus.INTERPRETING,
    QuerySessionStatus.AWAITING_INTENT_CONFIRMATION,
    QuerySessionStatus.GENERATING_SQL,
    QuerySessionStatus.AWAITING_SQL_CONFIRMATION,
)
# 掃除は session の作成のたびではなく、1 プロセスでこの間隔に 1 回だけ行う。
_QUERY_SESSION_SWEEP_INTERVAL_SECONDS = 900.0


ONTOLOGY_SOURCE_FILE_MAX_COUNT = 5
_EXPECTED_ETAG_UNSET = object()

_STORE_IDENTITY_FIELDS: dict[OntologyCollection, tuple[str, ...]] = {
    "revisions": ("revision_id",),
    "nodes": ("revision_id", "node_id"),
    "edges": ("revision_id", "edge_id"),
    "profile_views": ("profile_id", "revision_id"),
    "query_sessions": ("session_id",),
    "artifacts": ("artifact_id",),
    "proposals": ("proposal_id",),
    "idempotency": ("operation", "idempotency_key"),
    "source_documents": ("source_document_id",),
    "jobs": ("job_id",),
    "recommendations": ("recommendation_id",),
}
_STORE_OPTIMISTIC_COLLECTIONS: frozenset[OntologyCollection] = frozenset(
    {"query_sessions", "proposals"}
)

_BUSINESS_NODE_KINDS = frozenset(
    {
        OntologyNodeKind.OBJECT_TYPE,
        OntologyNodeKind.INTERFACE,
        OntologyNodeKind.FUNCTION,
        OntologyNodeKind.ACTION_TYPE,
        OntologyNodeKind.SHARED_PROPERTY,
        OntologyNodeKind.VALUE_TYPE,
        OntologyNodeKind.ENUMERATION,
        OntologyNodeKind.OBJECT_SET,
        OntologyNodeKind.BUSINESS_ENTITY,
        OntologyNodeKind.BUSINESS_EVENT,
        OntologyNodeKind.PROPERTY,
        OntologyNodeKind.METRIC,
        OntologyNodeKind.BUSINESS_TERM,
        OntologyNodeKind.BUSINESS_RULE,
        OntologyNodeKind.ENUM_VALUE,
    }
)
_BUSINESS_EDGE_KINDS = frozenset(
    {
        OntologyEdgeKind.LINK_TYPE,
        OntologyEdgeKind.USES,
        OntologyEdgeKind.BUSINESS_RELATIONSHIP,
        OntologyEdgeKind.MAPS_TO,
        OntologyEdgeKind.IS_A,
        OntologyEdgeKind.DOMAIN,
        OntologyEdgeKind.RANGE,
        OntologyEdgeKind.INSTANCE_OF,
        OntologyEdgeKind.HAS_VALUE,
        OntologyEdgeKind.GOVERNS,
    }
)
# 業務プロファイルの削除で消す、終わったオントロジーのジョブの状態（#1743）。
_FINISHED_JOB_STATUSES = frozenset({"succeeded", "succeeded_with_warnings", "failed", "cancelled"})
_MARKDOWN_DRAFT_ARTIFACT_TYPE = "ontology_markdown_draft"
_MARKDOWN_PUBLISHED_ARTIFACT_TYPE = "ontology_markdown_published"
_MARKDOWN_LLM_ARTIFACT_TYPE = "ontology_llm_markdown"
_MARKDOWN_RENDERER_VERSION = "markdown_ontology_v1"
_QUESTION_INTENT_SCHEMA_VERSION = "question_intent_graph_v1"
_QUESTION_INTENT_PROMPT_VERSION = "question_intent_interpreter_v2"
_INTENT_FALLBACK_LOG_MAX_FIELDS = 20
_LEGACY_AMBIGUITY_KIND_CODES: Mapping[str, str] = {
    "extract_items": "OUTPUT_UNCLEAR",
    "output": "OUTPUT_UNCLEAR",
    "outputs": "OUTPUT_UNCLEAR",
    "conditions": "FILTER_VALUE_UNCLEAR",
    "condition": "FILTER_VALUE_UNCLEAR",
    "filter": "FILTER_VALUE_UNCLEAR",
    "filters": "FILTER_VALUE_UNCLEAR",
    "time_range": "TIME_RANGE_UNCLEAR",
    "date_range": "TIME_RANGE_UNCLEAR",
    "granularity": "GRANULARITY_UNCLEAR",
    "relationship_path": "RELATIONSHIP_PATH_UNCLEAR",
    "relationship": "RELATIONSHIP_PATH_UNCLEAR",
    "business_meaning": "BUSINESS_MEANING_UNCLEAR",
}


def _idempotency_storage_key(request_key: str) -> str:
    """Oracle の key 列へ収まる決定論的な storage identity を返す。"""

    if len(request_key.encode("utf-8")) <= IDEMPOTENCY_KEY_STORAGE_MAX_BYTES:
        return request_key
    return f"sha256:{hashlib.sha256(request_key.encode('utf-8')).hexdigest()}"


def _question_intent_json_schema() -> dict[str, Any]:
    schema = QuestionIntentGraph.model_json_schema()
    schema["properties"].pop("created_at", None)
    # Any の filter 値は wire では JSON 文字列。domain へ戻す際に型を復元する。
    fields = schema["$defs"]["IntentFilter"]["properties"]
    fields.pop("value")
    fields["value_json"] = {
        "type": "string",
        "description": "JSON encoded filter value: string, number, boolean, null, array or object.",
    }
    return strict_schema(schema)


def _question_intent_response_format() -> dict[str, Any]:
    return format_schema(_question_intent_json_schema(), _QUESTION_INTENT_SCHEMA_VERSION)


def _decode_question_intent_output(raw: str) -> dict[str, Any]:
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("intent must be an object")
    for item in parsed.get("filters", []):
        if not isinstance(item, dict):
            raise ValueError("filter must be an object")
        if "value_json" in item:
            item["value"] = json.loads(item.pop("value_json"))
    return parsed


def _question_intent_minimum_example(
    *,
    question: str,
    ontology_revision_id: str,
    profile_view_id: str,
) -> dict[str, Any]:
    return {
        "version": 1,
        "question_original": question,
        "question_effective": question,
        "profile_view_id": profile_view_id,
        "ontology_revision_id": ontology_revision_id,
        "entities": [],
        "metrics": [],
        "dimensions": [],
        "filters": [],
        "time_range": None,
        "granularity": "",
        "sorts": [],
        "limit": None,
        "candidate_paths": [],
        "selected_path_id": None,
        "ambiguities": [],
        "confidence": 0.0,
    }


def _normalize_question_intent_payload(payload: object) -> object:
    if not isinstance(payload, Mapping):
        return payload
    normalized = dict(payload)
    ambiguities = normalized.get("ambiguities")
    if isinstance(ambiguities, list):
        normalized["ambiguities"] = [_normalize_intent_ambiguity(item) for item in ambiguities]
    return normalized


def _normalize_intent_ambiguity(value: object) -> object:
    if not isinstance(value, Mapping):
        return value
    normalized = dict(value)
    raw_kind = normalized.get("kind")
    if isinstance(raw_kind, str):
        kind = raw_kind.strip()
        if kind in _LEGACY_AMBIGUITY_KIND_CODES:
            normalized.pop("kind", None)
            raw_code = normalized.get("code")
            if not isinstance(raw_code, str) or not raw_code.strip():
                normalized["code"] = _LEGACY_AMBIGUITY_KIND_CODES[kind]
    options = normalized.get("options")
    if isinstance(options, list):
        normalized["options"] = [_normalize_intent_ambiguity_option(item) for item in options]
    return normalized


def _normalize_intent_ambiguity_option(value: object) -> object:
    if isinstance(value, str):
        return value
    if not isinstance(value, Mapping):
        return value
    if not set(value).issubset({"label_ja", "value"}):
        return value
    label = value.get("label_ja")
    if isinstance(label, str) and label.strip():
        return label.strip()
    return value


def _validation_error_paths(exc: ValidationError) -> list[str]:
    paths: list[str] = []
    for item in exc.errors(include_input=False):
        loc = item.get("loc", ())
        if isinstance(loc, tuple | list):
            paths.append(".".join(str(part) for part in loc))
        else:
            paths.append(str(loc))
    return paths


def _log_intent_enterprise_ai_fallback(
    *,
    reason: str,
    model: str,
    exc: BaseException | None = None,
    violation_kind: str = "",
    violation_count: int = 0,
) -> None:
    extra: dict[str, Any] = {
        "fallback_reason": reason,
        "error_type": exc.__class__.__name__ if exc is not None else "",
        "model": model,
        "prompt_version": _QUESTION_INTENT_PROMPT_VERSION,
        "schema_version": _QUESTION_INTENT_SCHEMA_VERSION,
    }
    if isinstance(exc, ValidationError):
        paths = _validation_error_paths(exc)
        extra["field_error_count"] = len(paths)
        extra["field_paths"] = paths[:_INTENT_FALLBACK_LOG_MAX_FIELDS]
    if violation_kind:
        extra["scope_violation_kind"] = violation_kind
        extra["scope_violation_count"] = violation_count
    logger.warning("ontology_intent_enterprise_ai_fallback", extra=extra)


class QuerySessionApiCreate(OntologyContract):
    question: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    allowed_objects: AllowedObjects = Field(default_factory=AllowedObjects)
    row_limit: int | None = Field(default=None, ge=1, le=5000)
    engine: Nl2SqlEngine = Nl2SqlEngine.ENTERPRISE_AI_DIRECT
    profile_confirmation_token: str = ""
    clarification_mode: ClarificationMode = ClarificationMode.REVIEW_ONLY
    # チャットの回答前の確認（#1442）。業務プロファイルはチャットの画面で利用者が選んでいるので、
    # 推薦の確認（profile_confirmation_token）は求めない。`previous_job_id` は同じ会話の前の
    # ターンで、その会話の履歴（前の質問と SQL）を要件の検査に渡す。
    chat: bool = False
    previous_job_id: str | None = Field(default=None, max_length=64)
    attachment_ids: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_chat(self) -> QuerySessionApiCreate:
        if self.chat and self.clarification_mode != ClarificationMode.GUIDED:
            raise ValueError("チャットでは回答前の確認（guided）だけを使えます。")
        if self.previous_job_id and not self.chat:
            raise ValueError("会話の続きはチャットの回答前の確認で指定できます。")
        if self.attachment_ids and (
            not self.chat or self.engine != Nl2SqlEngine.ENTERPRISE_AI_DIRECT
        ):
            raise ValueError("添付ファイルは Enterprise AI のチャットで使えます。")
        return self


class ClarificationAnswerRequest(OntologyContract):
    """回答前の確認の 1 つの項目への回答（質問への回答と、確認済みの一覧からの直し。#1439）。"""

    base_version: int = Field(ge=1)
    item_id: str = Field(min_length=1)
    selected_candidate_ids: list[str] = Field(default_factory=list)
    free_text: str = Field(default="", max_length=2000)


class RequirementApplyRequest(OntologyContract):
    base_version: int = Field(ge=1)


class OntologyProfileRecommendationRequest(OntologyContract):
    question: str = Field(min_length=1)
    limit: int = Field(default=3, ge=1, le=3)


class OntologyProfileRecommendationData(OntologyContract):
    recommendation: ProfileRecommendation


class ProfileRecommendationConfirmationRequest(OntologyContract):
    selected_profile_id: str = Field(min_length=1)
    selected_revision_id: str = Field(min_length=1)


class ProfileRecommendationConfirmationData(OntologyContract):
    recommendation: ProfileRecommendation
    confirmation_token: str = Field(min_length=1)


class OntologyContextSearchRequest(OntologyContract):
    question: str = Field(min_length=1)
    ontology_revision_id: str = Field(min_length=1)
    top_k: int = Field(default=8, ge=1, le=24)
    max_hops: int = Field(default=2, ge=1, le=3)


class GenerateSqlRequest(OntologyContract):
    intent_version: int = Field(ge=1)
    base_version: int = Field(ge=1)
    ontology_revision_id: str = Field(min_length=1)
    confirm_intent: Literal[True]


class ImprovementProposalRequest(OntologyContract):
    title_ja: str = ""
    description_ja: str = ""
    kind: OntologyProposalKind = OntologyProposalKind.QUERY_EXAMPLE
    proposal_payload: OntologyProposalPayload = Field(default_factory=OntologyProposalPayload)
    patch: GraphPatch | None = None
    base_revision_id: str = ""
    intent_version: int | None = Field(default=None, ge=1)
    summary: str = ""


class OntologyDraftRequest(OntologyContract):
    """既存 revision から業務 node/edge だけを変更した新 draft を作る。"""

    base_etag: str = Field(min_length=1)
    note: str = ""
    node_upserts: list[OntologyNode] = Field(default_factory=list)
    edge_upserts: list[OntologyEdge] = Field(default_factory=list)
    remove_node_ids: list[str] = Field(default_factory=list)
    remove_edge_ids: list[str] = Field(default_factory=list)


class OntologyMarkdownState(OntologyContract):
    draft_markdown: str = ""
    published_markdown: str = ""
    draft_revision: OntologyRevision | None = None
    published_revision: OntologyRevision | None = None
    draft_version: int | None = Field(default=None, ge=1)
    published_version: int | None = Field(default=None, ge=1)
    draft_etag: str = ""
    published_at: datetime | None = None
    published_findings: list[dict[str, Any]] = Field(default_factory=list)
    published_data_report: dict[str, Any] | None = None
    published_diagnostics_available: bool = False


class OntologyMarkdownDraftPatch(OntologyContract):
    markdown: str = ""
    base_etag: str = Field(min_length=1)


class OntologyPublishRequest(OntologyContract):
    etag: str = Field(min_length=1)
    profile_id: str = ""


class SqlBindingRequest(OntologyContract):
    session_id: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    ontology_revision_id: str = Field(min_length=1)
    intent_version: int = Field(ge=1)
    sql_hash: str = Field(min_length=1)
    validation_hash: str = Field(min_length=1)
    generation_context_hash: str = Field(min_length=1)
    confirm_sql: Literal[True]

    def binding(self) -> SqlConfirmationRequest:
        return SqlConfirmationRequest(
            artifact_id=self.artifact_id,
            ontology_revision_id=self.ontology_revision_id,
            intent_version=self.intent_version,
            sql_hash=self.sql_hash,
            validation_hash=self.validation_hash,
            generation_context_hash=self.generation_context_hash,
        )


class ProfileOntologyViewPatch(OntologyContract):
    """Profile view で編集可能な業務 metadata だけを更新する。"""

    base_etag: str = Field(min_length=1)
    table_usages_ja: dict[str, str] | None = None
    column_policies: dict[str, ColumnQueryPolicy] | None = None
    allowed_path_ids: list[str] | None = None
    node_overrides: list[dict[str, Any]] | None = None
    edge_overrides: list[dict[str, Any]] | None = None
    schema_fingerprint: str | None = None
    physical_scope: dict[str, list[str]] | None = None
    activation_scenarios_ja: list[str] | None = None
    activation_keywords: list[str] | None = None


class QueryRuntimeContext(OntologyContract):
    allowed_objects: AllowedObjects
    row_limit: int | None = Field(default=None, ge=1, le=5000)
    engine: StoredNl2SqlEngine = Nl2SqlEngine.ENTERPRISE_AI_DIRECT
    retrieved_node_ids: list[str] = Field(default_factory=list)
    profile_recommendation_id: str = ""
    profile_selection_source: str = "legacy"
    # 回答前の確認で、公開版のオントロジーの業務の概念を使うか（公開版が古ければ使わない。#1439）。
    requirement_ontology_current: bool = True
    attachment_ids: list[str] = Field(default_factory=list)


class OntologyGraphData(OntologyContract):
    revision: OntologyRevision
    nodes: list[OntologyNode] = Field(default_factory=list)
    edges: list[OntologyEdge] = Field(default_factory=list)


class OntologyPublishJobData(OntologyContract):
    job: OntologyPublishJob


class OntologyRevisionListData(OntologyContract):
    revisions: list[OntologyRevision] = Field(default_factory=list)
    active_revision_id: str = ""


class ProfileOntologyViewData(OntologyContract):
    profile_ontology_view: ProfileOntologyView
    ontology_graph: OntologyGraphData
    materialized: bool = False
    stale: bool = False
    # 公開オントロジーに解決できなかった対象オブジェクト名の診断(応答のみ、永続化しない)
    warnings_ja: list[str] = Field(default_factory=list)


class QuerySessionData(OntologyContract):
    session: QuerySession
    profile_ontology_view: ProfileOntologyView
    ontology_graph: OntologyGraphData
    preview: PreviewData | None = None
    result: QueryResults | None = None
    performance_check: ExplainPlanData | None = None
    ontology_trace_summary: dict[str, Any] = Field(default_factory=dict)
    requirement_state: RequirementState | None = None


class QueryExecutionData(QuerySessionData):
    result: QueryResults


class OntologyProposalReviewData(OntologyContract):
    proposal: OntologyProposal
    draft: OntologyGraphData | None = None


class OntologyApiRuntime:
    """既存 NL2SQL service と versioned Ontology domain/store を接続する。"""

    def __init__(
        self,
        *,
        legacy_service: Any = nl2sql_service,
        store: OntologyStore | None = None,
        session_service: OntologyQuerySessionService | None = None,
    ) -> None:
        self.legacy_service = legacy_service
        self.store = store or self._default_store(legacy_service)
        self.sessions = session_service or OntologyQuerySessionService()
        self._lock = RLock()
        # テーブル詳細の論理名 lookup は全量 ontology graph の lock と分離する。
        self._business_name_lock = RLock()
        self._business_name_cache: OrderedDict[tuple[str, str], dict[str, str]] = OrderedDict()
        self._business_name_cache_max_objects = 512
        self._business_name_cache_generation = 0
        # SQL の生成のジョブの接地確認が読む公開版のグラフ（版は不変）。全量 graph の `_lock` と
        # 分け、lock を持ったまま DB を読む別の処理をジョブが待たない（#1168）。
        # 値は（所有する業務プロファイルの ID。旧方式の共有の版は空、グラフ）。
        self._release_graph_lock = RLock()
        self._release_graph_cache: OrderedDict[str, tuple[str, SchemaOntology]] = OrderedDict()
        self._release_graph_cache_generation = 0
        self._store_ready = False
        self._published_revision_loaded = False
        self._revision_headers_loaded = False
        self._revision_headers: dict[str, OntologyRevision] = {}
        self._ontology: SchemaOntology | None = None
        self._ontologies: dict[str, SchemaOntology] = {}
        self._synced_catalog_signature: tuple[int, str] | None = None
        settings = get_settings()
        self._ontology_cache_order: OrderedDict[str, None] = OrderedDict()
        self._ontology_cache_max_revisions = max(
            1, settings.nl2sql_ontology_graph_cache_max_revisions
        )
        self._ontology_cache_max_bytes = (
            max(1, settings.nl2sql_ontology_graph_cache_max_megabytes) * 1024 * 1024
        )
        self._session_views: dict[str, ProfileOntologyView] = {}
        self._profile_view_overrides: dict[tuple[str, str], ProfileOntologyView] = {}
        self._contexts: dict[str, QueryRuntimeContext] = {}
        self._previews: dict[str, PreviewData] = {}
        self._results: dict[str, QueryResults] = {}
        self._plans: dict[str, ExplainPlanData] = {}
        self._session_sweep_lock = RLock()
        self._last_session_sweep_at: float | None = None
        self._embeddings: dict[str, dict[str, list[float]]] = {}
        self._store_etags: dict[tuple[OntologyCollection, tuple[str, ...]], str] = {}

    def reset_after_system_schema_change(self) -> None:
        """Schema recreate / migration 後に永続 store 由来 cache を破棄する。"""

        with self._lock:
            self.sessions = OntologyQuerySessionService()
            self._store_ready = False
            self._published_revision_loaded = False
            self._revision_headers_loaded = False
            self._revision_headers.clear()
            self._ontology = None
            self._ontologies.clear()
            self._synced_catalog_signature = None
            self._ontology_cache_order.clear()
            self._session_views.clear()
            self._profile_view_overrides.clear()
            self._contexts.clear()
            self._previews.clear()
            self._results.clear()
            self._plans.clear()
            self._embeddings.clear()
            self._store_etags.clear()
        with self._business_name_lock:
            self._business_name_cache.clear()
            self._business_name_cache_generation += 1
        with self._release_graph_lock:
            self._release_graph_cache.clear()
            self._release_graph_cache_generation += 1

    @staticmethod
    def _default_store(legacy_service: Any) -> OntologyStore:
        settings = get_settings()
        if settings.nl2sql_persistence_mode.strip().lower() != "oracle":
            return InMemoryOntologyStore()
        adapter = getattr(legacy_service, "_oracle_adapter", None)
        # オントロジーの状態（`NL2SQL_ONTOLOGY_*`）は pool から借りる（#830）。
        connection_factory = getattr(adapter, "state_connection", None)
        if not callable(connection_factory):
            raise RuntimeError("Oracle Ontology store 用の connection factory がありません。")
        return OracleOntologyStore(connection_factory=connection_factory)

    def current_ontology(self) -> SchemaOntology:
        with self._lock:
            return self._sync_ontology()

    def column_business_names(
        self,
        *,
        owner: str,
        object_name: str,
        object_type: Literal["table", "view"],
    ) -> dict[str, str]:
        """Published ontology から対象 object の列業務名だけを取得する。

        テーブル詳細は全 graph や schema catalog を必要としない。published revision の
        header と indexed physical_id に一致する column node だけを読み、全量 graph 用
        ``_lock`` を取得しないことで、ontology 初期化中も詳細表示を待たせない。
        """

        normalized_type: Literal["table", "view"] = "view" if object_type == "view" else "table"
        # owner / object_name はカタログ上の名前。引用名を大文字の同名表の ID にしない（#563）。
        physical_id = stable_physical_id(
            normalized_type,
            physical_identity_part(owner),
            physical_identity_part(object_name),
        )
        with self._business_name_lock:
            self._ensure_store()
            cache_generation = self._business_name_cache_generation
        revision_documents = self.store.list_documents(
            "revisions",
            {"status": OntologyRevisionStatus.PUBLISHED.value},
        )
        revisions = [
            OntologyRevision.model_validate(self._stored_payload(document, collection="revision"))
            for document in revision_documents
        ]
        if not revisions:
            return {}
        active = max(
            revisions,
            key=lambda item: (
                item.version,
                item.published_at or item.created_at,
                item.id,
            ),
        )
        cache_key = (active.id, physical_id)
        with self._business_name_lock:
            cached = self._business_name_cache.get(cache_key)
            if cached is not None:
                self._business_name_cache.move_to_end(cache_key)
                return dict(cached)

        node_documents = self.store.list_documents(
            "nodes",
            {
                "revision_id": active.id,
                "node_type": OntologyNodeKind.COLUMN.value,
                "physical_id": physical_id,
            },
            include_embedding=False,
        )
        names: dict[str, str] = {}
        for document in node_documents:
            node = OntologyNode.model_validate(self._stored_payload(document, collection="node"))
            # key はカタログ上の列名。`Amount` と `AMOUNT` を潰さない（#563）。
            column_name = str(node.metadata.get("column_name") or "").strip()
            if not column_name:
                parts = node.technical_name.split(".")
                column_name = parts[-1] if len(parts) >= 3 else ""
            if column_name:
                names[column_name] = node.business_name_ja

        with self._business_name_lock:
            if cache_generation == self._business_name_cache_generation:
                self._business_name_cache[cache_key] = names
                self._business_name_cache.move_to_end(cache_key)
                while len(self._business_name_cache) > self._business_name_cache_max_objects:
                    self._business_name_cache.popitem(last=False)
        return dict(names)

    def list_ontology_revisions(self) -> tuple[list[OntologyRevision], str]:
        with self._lock:
            self._sync_ontology()
            self._load_revision_headers()
            revisions = sorted(
                self._revision_headers.values(),
                key=lambda item: (item.version, item.created_at, item.id),
                reverse=True,
            )
            active_revision_id = self._query_ontology().revision.id
            return [item.model_copy(deep=True) for item in revisions], active_revision_id

    @staticmethod
    def _markdown_artifact_id(
        *,
        artifact_type: str,
        profile_id: str,
        revision_id: str,
    ) -> str:
        return stable_ontology_id(
            "ontology_markdown",
            artifact_type,
            profile_id,
            revision_id,
            length=32,
        )

    @staticmethod
    def _artifact_profile_id(document: Mapping[str, Any]) -> str:
        profile_id = str(document.get("profile_id") or "")
        if profile_id:
            return profile_id
        payload = document.get("payload")
        if isinstance(payload, Mapping):
            return str(payload.get("profile_id") or "")
        return ""

    @staticmethod
    def _artifact_content(document: Mapping[str, Any] | None) -> str:
        if document is None:
            return ""
        content = document.get("content")
        if isinstance(content, str):
            return content
        payload = document.get("payload")
        if isinstance(payload, Mapping):
            payload_content = payload.get("content")
            if isinstance(payload_content, str):
                return payload_content
        return ""

    @staticmethod
    def _artifact_revision_id(document: Mapping[str, Any]) -> str:
        revision_id = str(document.get("session_id") or "")
        if revision_id:
            return revision_id
        payload = document.get("payload")
        if isinstance(payload, Mapping):
            return str(payload.get("ontology_revision_id") or payload.get("revision_id") or "")
        return ""

    @staticmethod
    def _artifact_profile_revision_version(document: Mapping[str, Any]) -> int | None:
        for source in (document, document.get("payload")):
            if not isinstance(source, Mapping):
                continue
            for key in ("profile_revision_version", "profile_version"):
                try:
                    version = int(source.get(key) or 0)
                except (TypeError, ValueError):
                    continue
                if version >= 1:
                    return version
        return None

    def _profile_markdown_artifacts(self, profile_id: str) -> list[dict[str, Any]]:
        # 種類で絞って読む。全成果物（公開の snapshot・Turtle など。共有の DB で約 12 MB）を
        # 読んでから絞らない（#1155）。並びは使わない（版の番号は作成の順で決める）。
        return [
            document
            for artifact_type in (_MARKDOWN_DRAFT_ARTIFACT_TYPE, _MARKDOWN_PUBLISHED_ARTIFACT_TYPE)
            for document in self.store.list_documents("artifacts", {"artifact_type": artifact_type})
            if self._artifact_profile_id(document) == profile_id
        ]

    def _profile_markdown_revision_versions(self, profile_id: str) -> dict[str, int]:
        grouped: dict[str, dict[str, Any]] = {}
        for document in self._profile_markdown_artifacts(profile_id):
            revision_id = self._artifact_revision_id(document)
            if not revision_id:
                continue
            group = grouped.setdefault(
                revision_id,
                {
                    "sort_key": (
                        str(document.get("created_at") or document.get("updated_at") or ""),
                        str(document.get("artifact_id") or ""),
                    ),
                    "direct_version": None,
                },
            )
            group["sort_key"] = min(
                group["sort_key"],
                (
                    str(document.get("created_at") or document.get("updated_at") or ""),
                    str(document.get("artifact_id") or ""),
                ),
            )
            direct_version = self._artifact_profile_revision_version(document)
            if direct_version is not None:
                current_direct = group["direct_version"]
                group["direct_version"] = (
                    direct_version
                    if current_direct is None
                    else min(int(current_direct), direct_version)
                )

        versions: dict[str, int] = {
            revision_id: int(group["direct_version"])
            for revision_id, group in grouped.items()
            if group["direct_version"] is not None
        }
        used_versions = set(versions.values())
        next_version = 1
        missing = [
            (revision_id, group["sort_key"])
            for revision_id, group in grouped.items()
            if group["direct_version"] is None
        ]
        for revision_id, _sort_key in sorted(missing, key=lambda item: item[1]):
            while next_version in used_versions:
                next_version += 1
            versions[revision_id] = next_version
            used_versions.add(next_version)
            next_version += 1
        return versions

    def _profile_markdown_revision_version(
        self,
        profile_id: str,
        document: Mapping[str, Any] | None,
    ) -> int | None:
        if document is None:
            return None
        direct_version = self._artifact_profile_revision_version(document)
        if direct_version is not None:
            return direct_version
        revision_id = self._artifact_revision_id(document)
        if not revision_id:
            return None
        return self._profile_markdown_revision_versions(profile_id).get(revision_id)

    def _next_profile_markdown_revision_version(self, profile_id: str) -> int:
        return max(self._profile_markdown_revision_versions(profile_id).values(), default=0) + 1

    def _markdown_artifact_for_revision(
        self,
        *,
        profile_id: str,
        revision_id: str,
        artifact_type: str,
    ) -> dict[str, Any] | None:
        artifact_id = self._markdown_artifact_id(
            artifact_type=artifact_type,
            profile_id=profile_id,
            revision_id=revision_id,
        )
        stable_document = self.store.get_artifact(artifact_id)
        if stable_document is not None:
            return stable_document
        candidates = [
            document
            for document in self.store.list_artifacts(revision_id)
            if document.get("artifact_type") == artifact_type
            and self._artifact_profile_id(document) in {"", profile_id}
        ]
        return (
            max(
                candidates,
                key=lambda item: (
                    str(item.get("updated_at") or item.get("created_at") or ""),
                    str(item.get("artifact_id") or ""),
                ),
            )
            if candidates
            else None
        )

    def _save_markdown_artifact(
        self,
        *,
        profile_id: str,
        revision: OntologyRevision,
        artifact_type: str,
        markdown: str,
        expected_etag: str | None = None,
        profile_version: int | None = None,
    ) -> dict[str, Any]:
        artifact_id = self._markdown_artifact_id(
            artifact_type=artifact_type,
            profile_id=profile_id,
            revision_id=revision.id,
        )
        current = self.store.get_artifact(artifact_id)
        if expected_etag and current is None:
            # クライアントが etag を提示しているのに artifact 行が無い場合、黙って
            # 無条件上書きすると並行編集の lost update になるため conflict にする。
            raise OntologyVersionConflictError(
                "ONTOLOGY_MARKDOWN_ARTIFACT_MISSING",
                "Markdown 成果物が見つからないため保存できません。再読込して再実行してください。",
            )
        now = utc_now()
        resolved_profile_version = (
            profile_version
            or self._profile_markdown_revision_version(profile_id, current)
            or self._next_profile_markdown_revision_version(profile_id)
        )
        document = {
            "artifact_id": artifact_id,
            "session_id": revision.id,
            "artifact_type": artifact_type,
            "content_hash": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
            "content": markdown,
            "profile_id": profile_id,
            "revision_version": revision.version,
            "profile_revision_version": resolved_profile_version,
            "renderer_version": _MARKDOWN_RENDERER_VERSION,
            "created_at": current.get("created_at") if current is not None else now,
            "updated_at": now,
        }
        return self.store.save_artifact(
            document,
            expected_etag=expected_etag if current is not None else None,
        )

    def _latest_profile_markdown_artifact(
        self,
        *,
        profile_id: str,
        artifact_type: str,
        statuses: set[OntologyRevisionStatus] | None = None,
    ) -> tuple[dict[str, Any], OntologyRevision] | None:
        documents = [
            document
            for document in self.store.list_documents("artifacts", {"artifact_type": artifact_type})
            if self._artifact_profile_id(document) == profile_id
        ]
        candidates: list[tuple[dict[str, Any], OntologyRevision]] = []
        profile_versions = self._profile_markdown_revision_versions(profile_id)
        for document in documents:
            revision_id = self._artifact_revision_id(document)
            if not revision_id:
                continue
            ontology = self._load_ontology_revision(revision_id)
            if ontology is None:
                continue
            revision = ontology.revision
            if statuses is not None and revision.status not in statuses:
                continue
            candidates.append((document, revision))
        return (
            max(
                candidates,
                key=lambda item: (
                    profile_versions.get(item[1].id, item[1].version),
                    str(item[0].get("updated_at") or item[0].get("created_at") or ""),
                    item[1].id,
                ),
            )
            if candidates
            else None
        )

    def published_release_id(self, profile_id: str) -> str:
        """SQL の生成のジョブが固定する、業務プロファイルの公開版の ID（無ければ空）。

        `ontology_markdown_state` の公開版（Markdown の公開の snapshot、無ければ公開中の版の
        Markdown）と同じ版を選ぶ。ただし下書き・診断・版のグラフ（nodes / edges / embedding。
        1 版で数千行）を読まず、成果物と版の header だけを読む。runtime の lock も取らない
        （キャッシュに触れず、store を読むだけ）。ジョブの準備の段階が、版のグラフの読み込みと、
        lock を持ったまま DB を読む別の処理を待たないため（#1155）。
        """

        from .ontology_markdown_workspace import MarkdownOntologyWorkspace

        snapshot_id = MarkdownOntologyWorkspace(self).published_snapshot_id(profile_id)
        if snapshot_id:
            return snapshot_id
        self._ensure_store()
        published = {
            header.id: header
            for header in (
                OntologyRevision.model_validate(
                    self._stored_payload(document, collection="revision")
                )
                for document in self.store.list_documents(
                    "revisions", {"status": OntologyRevisionStatus.PUBLISHED.value}
                )
            )
        }
        if not published:
            return ""
        candidates = [
            (document, published[revision_id])
            for document in self.store.list_documents(
                "artifacts", {"artifact_type": _MARKDOWN_PUBLISHED_ARTIFACT_TYPE}
            )
            if self._artifact_profile_id(document) == profile_id
            and (revision_id := self._artifact_revision_id(document)) in published
        ]
        if not candidates:
            return ""
        if len({revision.id for _document, revision in candidates}) == 1:
            return candidates[0][1].id
        # 公開中の版は所有者ごとに 1 つのため、複数になるのは旧共有版と業務プロファイルの版が
        # 両方公開中のときだけ。`_latest_profile_markdown_artifact` と同じ順で選ぶ。
        profile_versions = self._profile_markdown_revision_versions(profile_id)
        return max(
            candidates,
            key=lambda item: (
                profile_versions.get(item[1].id, item[1].version),
                str(item[0].get("updated_at") or item[0].get("created_at") or ""),
                item[1].id,
            ),
        )[1].id

    def ontology_markdown_state(self, profile_id: str) -> OntologyMarkdownState:
        with self._lock:
            self._ensure_store()
            self._strict_profile(profile_id)
            self._load_published_revision()
            self._load_revision_headers()
            # 公開元の Markdown 下書きは、公開後も version 表示用の履歴として残す。
            draft_match = self._latest_profile_markdown_artifact(
                profile_id=profile_id,
                artifact_type=_MARKDOWN_DRAFT_ARTIFACT_TYPE,
                statuses={OntologyRevisionStatus.DRAFT, OntologyRevisionStatus.PUBLISHED},
            )
            draft_document: dict[str, Any] | None = None
            draft_revision: OntologyRevision | None = None
            if draft_match is not None:
                draft_document, draft_revision = draft_match

            published_document: dict[str, Any] | None = None
            published_revision: OntologyRevision | None = None
            published_match = self._latest_profile_markdown_artifact(
                profile_id=profile_id,
                artifact_type=_MARKDOWN_PUBLISHED_ARTIFACT_TYPE,
                statuses={OntologyRevisionStatus.PUBLISHED},
            )
            if published_match is not None:
                published_document, published_revision = published_match

            from .ontology_markdown_workspace import MarkdownOntologyWorkspace

            workspace = MarkdownOntologyWorkspace(self)
            snapshot = workspace.snapshot(profile_id)
            diagnostics = workspace._snapshot_diagnostics(profile_id, snapshot) if snapshot else {}
            if snapshot:
                published_document = {"content": snapshot["markdown"]}
                published_revision = SchemaOntology.model_validate(snapshot["graph"]).revision
            return OntologyMarkdownState(
                draft_markdown=self._artifact_content(draft_document),
                published_markdown=self._artifact_content(published_document),
                draft_revision=draft_revision,
                published_revision=published_revision,
                draft_version=self._profile_markdown_revision_version(profile_id, draft_document),
                published_version=(
                    snapshot["display_version"]
                    if snapshot
                    else self._profile_markdown_revision_version(profile_id, published_document)
                ),
                draft_etag=str(draft_document.get("etag") or "") if draft_document else "",
                published_at=published_revision.published_at if published_revision else None,
                published_findings=diagnostics.get("findings", []),
                published_data_report=diagnostics.get("data_report"),
                published_diagnostics_available=diagnostics.get("available", False),
            )

    def save_ontology_markdown_draft(
        self,
        profile_id: str,
        request: OntologyMarkdownDraftPatch,
    ) -> OntologyMarkdownState:
        with self._lock:
            state = self.ontology_markdown_state(profile_id)
            if state.draft_etag != request.base_etag:
                raise OntologyVersionConflictError(
                    "ONTOLOGY_ETAG_MISMATCH", "下書きが更新されました。最新情報を取得してください。"
                )
            from .ontology_markdown_workspace import MarkdownOntologyWorkspace

            published = MarkdownOntologyWorkspace(self).snapshot(profile_id)
            if (
                state.draft_revision
                and published
                and published.get("source_revision_id") == state.draft_revision.id
                and request.markdown != state.draft_markdown
            ):
                # 公開済み本文の出典 artifact は固定し、以後の自由編集は次の草稿版へ保存する。
                self.create_build_markdown_draft(
                    profile_id=profile_id,
                    base_revision_id=published["id"],
                    payloads=[],
                    titles=[],
                    markdown=request.markdown,
                    note="公開 Markdown から次の下書きを作成",
                )
                return self.ontology_markdown_state(profile_id)
            if (
                state.draft_revision is None
                or state.draft_revision.status != OntologyRevisionStatus.DRAFT
                or not state.draft_etag
            ):
                raise OntologyStateConflictError(
                    "ONTOLOGY_MARKDOWN_DRAFT_NOT_FOUND",
                    "保存できる Markdown 下書きがありません。AI 構築を実行してください。",
                )
            self._save_markdown_artifact(
                profile_id=profile_id,
                revision=state.draft_revision,
                artifact_type=_MARKDOWN_DRAFT_ARTIFACT_TYPE,
                markdown=request.markdown,
                expected_etag=request.base_etag,
            )
            return self.ontology_markdown_state(profile_id)

    def published_markdown_for_revision(self, revision_id: str, *, profile_id: str = "") -> str:
        """公開版の Markdown を返す（store を読むだけ。runtime の lock を取らない）。

        SQL の生成のジョブの準備の段階が使う。lock を持ったまま DB を読む別の処理を
        待たない（#1155）。
        """

        self._ensure_store()
        snapshot_doc = self.store.get_artifact(revision_id)
        if snapshot_doc and snapshot_doc.get("artifact_type") == "ontology_markdown_snapshot":
            if profile_id and snapshot_doc.get("profile_id") != profile_id:
                return ""
            return str(json.loads(snapshot_doc["content"])["markdown"])
        if profile_id:
            published_document = self._markdown_artifact_for_revision(
                profile_id=profile_id,
                revision_id=revision_id,
                artifact_type=_MARKDOWN_PUBLISHED_ARTIFACT_TYPE,
            )
            markdown = self._artifact_content(published_document)
            if markdown:
                return markdown
        artifacts = self.store.list_artifacts(revision_id)
        if profile_id:
            # 他 profile の published markdown を fallback で返さない
            # (cross-profile リーク防止)。
            # profile 非依存(profile_id 空)の artifact のみ fallback を許す。
            artifacts = [
                document
                for document in artifacts
                if self._artifact_profile_id(document) in ("", profile_id)
            ]
        published_documents = [
            document
            for document in artifacts
            if document.get("artifact_type") == _MARKDOWN_PUBLISHED_ARTIFACT_TYPE
        ]
        if published_documents:
            return self._artifact_content(
                max(
                    published_documents,
                    key=lambda item: (
                        str(item.get("updated_at") or item.get("created_at") or ""),
                        str(item.get("artifact_id") or ""),
                    ),
                )
            )
        fallback_documents = [
            document
            for document in artifacts
            if document.get("artifact_type") == _MARKDOWN_LLM_ARTIFACT_TYPE
        ]
        if fallback_documents:
            return self._artifact_content(
                max(
                    fallback_documents,
                    key=lambda item: (
                        str(item.get("updated_at") or item.get("created_at") or ""),
                        str(item.get("artifact_id") or ""),
                    ),
                )
            )
        return ""

    def draft_markdown_for_revision(self, revision_id: str, *, profile_id: str = "") -> str:
        with self._lock:
            self._ensure_store()
            draft_documents = [
                document
                for document in self.store.list_artifacts(revision_id)
                if document.get("artifact_type") == _MARKDOWN_DRAFT_ARTIFACT_TYPE
                and (not profile_id or self._artifact_profile_id(document) == profile_id)
            ]
            if not draft_documents:
                return ""
            return self._artifact_content(
                max(
                    draft_documents,
                    key=lambda item: (
                        str(item.get("updated_at") or item.get("created_at") or ""),
                        str(item.get("artifact_id") or ""),
                    ),
                )
            )

    def copy_draft_markdown_to_published(
        self,
        revision_id: str,
        *,
        profile_id: str = "",
    ) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_store()
            ontology = self.ontology_revision(revision_id)
            draft_documents = [
                document
                for document in self.store.list_artifacts(revision_id)
                if document.get("artifact_type") == _MARKDOWN_DRAFT_ARTIFACT_TYPE
                and (not profile_id or self._artifact_profile_id(document) == profile_id)
            ]
            saved: list[dict[str, Any]] = []
            published_documents = [
                item
                for item in self.store.list_artifacts(revision_id)
                if item.get("artifact_type") == _MARKDOWN_PUBLISHED_ARTIFACT_TYPE
            ]
            for document in draft_documents:
                document_profile_id = self._artifact_profile_id(document)
                if not document_profile_id:
                    continue
                existing = next(
                    (
                        item
                        for item in published_documents
                        if self._artifact_profile_id(item) == document_profile_id
                    ),
                    None,
                )
                if existing is not None:
                    if self._artifact_content(existing) != self._artifact_content(document):
                        raise OntologyVersionConflictError(
                            "PUBLISHED_MARKDOWN_MISMATCH",
                            "公開済み Markdown が下書きと異なるため、自動で上書きできません。",
                        )
                    saved.append(existing)
                    continue
                saved.append(
                    self._save_markdown_artifact(
                        profile_id=document_profile_id,
                        revision=ontology.revision,
                        artifact_type=_MARKDOWN_PUBLISHED_ARTIFACT_TYPE,
                        markdown=self._artifact_content(document),
                        profile_version=self._profile_markdown_revision_version(
                            document_profile_id,
                            document,
                        ),
                    )
                )
            return saved

    def ontology_revision(self, revision_id: str) -> SchemaOntology:
        with self._lock:
            self._ensure_store()
            ontology = self._load_ontology_revision(revision_id)
            if ontology is None:
                raise OntologyNotFoundError(
                    "ONTOLOGY_REVISION_NOT_FOUND",
                    "指定された Ontology revision が見つかりません。",
                )
            return ontology.model_copy(deep=True)

    def update_reasoning_status(
        self,
        revision_id: str,
        status: OntologyReasoningStatus,
        **metadata: Any,
    ) -> SchemaOntology:
        """公開切替を行わず、対象 revision の推論進捗だけを永続化する。"""

        with self._lock:
            self._sync_ontology()
            ontology = self._load_ontology_revision(revision_id)
            if ontology is None:
                raise OntologyNotFoundError(
                    "ONTOLOGY_REVISION_NOT_FOUND",
                    "推論対象の Ontology revision が見つかりません。",
                )
            if (
                status == OntologyReasoningStatus.FAILED
                and ontology.revision.status == OntologyRevisionStatus.PUBLISHED
            ):
                logger.warning(
                    "ontology_reasoning_failed_status_ignored_for_published_revision",
                    extra={"revision_id": revision_id},
                )
                return ontology.model_copy(deep=True)
            revision = ontology.revision.model_copy(
                update={"reasoning_status": status, **metadata},
                deep=True,
            )
            updated = ontology.model_copy(update={"revision": revision}, deep=True)
            self._cache_ontology(updated)
            if self._ontology is not None and self._ontology.revision.id == revision_id:
                self._ontology = updated
            self._persist_ontology(updated, include_graph=False)
            return updated.model_copy(deep=True)

    def validate_ontology_for_publish(
        self,
        revision_id: str,
        *,
        etag: str,
    ) -> SchemaOntology:
        """semantic publish より前に contract・参照閉包・物理 mapping・Profile 範囲を検証する。"""

        with self._lock:
            self._sync_ontology()
            ontology = self._load_ontology_revision(revision_id)
            if ontology is None:
                raise OntologyNotFoundError(
                    "ONTOLOGY_REVISION_NOT_FOUND",
                    "公開する Ontology revision が見つかりません。",
                )
            if ontology.revision.status != OntologyRevisionStatus.DRAFT:
                raise OntologyStateConflictError(
                    "ONTOLOGY_DRAFT_REQUIRED",
                    "Draft 状態の Ontology revision だけを公開できます。",
                )
            if ontology.revision.etag != etag:
                raise OntologyVersionConflictError(
                    "REVISION_ETAG_MISMATCH",
                    "Ontology revision が更新されています。再読込してください。",
                )
            node_by_id = {node.id: node for node in ontology.nodes}
            missing_endpoints = sorted(
                edge.id
                for edge in ontology.edges
                if edge.source_node_id not in node_by_id or edge.target_node_id not in node_by_id
            )
            if missing_endpoints:
                raise OntologyGateBlockedError(
                    "ONTOLOGY_REFERENCE_CLOSURE_INVALID",
                    "参照先が存在しない Ontology relation があります。",
                    finding_codes=missing_endpoints,
                )
            unresolved = [
                item.id
                for item in [*ontology.nodes, *ontology.edges]
                if (
                    (isinstance(item, OntologyNode) and item.kind in _BUSINESS_NODE_KINDS)
                    or (isinstance(item, OntologyEdge) and item.kind in _BUSINESS_EDGE_KINDS)
                )
                and item.review_status != OntologyReviewStatus.APPROVED
            ]
            if unresolved:
                raise OntologyGateBlockedError(
                    "ONTOLOGY_REVIEW_REQUIRED",
                    "未承認または orphan の業務 node/relation があるため公開できません。",
                    finding_codes=unresolved,
                )
            for node in ontology.nodes:
                if node.kind in _BUSINESS_NODE_KINDS:
                    self._validate_business_node_mapping(node, node_by_id)
            self._validate_metric_definitions_for_publish(ontology)
            self._validate_typed_semantics_for_publish(ontology)
            # 各 Profile view を候補 revision に対して再投影し、許可範囲外参照を事前に排除する。
            for profile in self._active_profiles():
                view = self._base_profile_view(profile, ontology)
                if set(view.node_ids) - set(node_by_id):
                    raise OntologyIntegrityError(
                        "PROFILE_ONTOLOGY_SCOPE_INVALID",
                        "プロファイル範囲に範囲外 node が含まれています。",
                    )
            return ontology.model_copy(deep=True)

    def create_ontology_draft(
        self,
        base_revision_id: str,
        request: OntologyDraftRequest,
        *,
        prepared_base: SchemaOntology | None = None,
    ) -> SchemaOntology:
        """物理 schema node を不変に保ち、業務定義だけを新 revision へ反映する。"""

        with self._lock:
            if prepared_base is None:
                self._sync_ontology()
                base = self._ontologies.get(base_revision_id)
            elif prepared_base.revision.id == base_revision_id:
                base = prepared_base
            else:
                base = None
            if base is None:
                raise OntologyNotFoundError(
                    "ONTOLOGY_REVISION_NOT_FOUND",
                    "元になる Ontology revision が見つかりません。",
                )
            if base.revision.etag != request.base_etag:
                raise OntologyVersionConflictError(
                    "REVISION_ETAG_MISMATCH",
                    "Ontology revision が更新されています。再読込してください。",
                )

            base_nodes = {node.id: node for node in base.nodes}
            base_edges = {edge.id: edge for edge in base.edges}
            remove_nodes = set(request.remove_node_ids)
            remove_edges = set(request.remove_edge_ids)
            illegal_node_removals = sorted(
                node_id
                for node_id in remove_nodes
                if node_id not in base_nodes or base_nodes[node_id].kind not in _BUSINESS_NODE_KINDS
            )
            illegal_edge_removals = sorted(
                edge_id
                for edge_id in remove_edges
                if edge_id not in base_edges or base_edges[edge_id].kind not in _BUSINESS_EDGE_KINDS
            )
            if illegal_node_removals or illegal_edge_removals:
                raise OntologyIntegrityError(
                    "PHYSICAL_ONTOLOGY_IMMUTABLE",
                    "物理 schema node/edge は業務 Ontology draft から削除できません。",
                )

            node_map = {
                node_id: node.model_copy(deep=True)
                for node_id, node in base_nodes.items()
                if node_id not in remove_nodes
            }
            for node in request.node_upserts:
                existing_node = base_nodes.get(node.id)
                if node.kind not in _BUSINESS_NODE_KINDS or (
                    existing_node is not None and existing_node.kind not in _BUSINESS_NODE_KINDS
                ):
                    raise OntologyIntegrityError(
                        "PHYSICAL_ONTOLOGY_IMMUTABLE",
                        "業務 draft では BusinessEntity/Event/Property/Metric/Term "
                        "だけを変更できます。",
                    )
                if node.review_status not in {
                    OntologyReviewStatus.PROPOSED,
                    OntologyReviewStatus.REVIEWED,
                    OntologyReviewStatus.APPROVED,
                }:
                    raise OntologyIntegrityError(
                        "BUSINESS_NODE_REVIEW_STATUS_INVALID",
                        "業務 node の review status が draft として不正です。",
                    )
                self._validate_business_node_mapping(node, base_nodes)
                # AI 構築由来(source_id="ontology_build:<job_id>" 等)の provenance は保持する。
                # source_id が無い(手編集)場合のみ MANUAL + base revision を刻む。
                node_map[node.id] = node.model_copy(
                    deep=True,
                    update={
                        "provenance": (
                            node.provenance.model_copy(deep=True)
                            if node.provenance.source_id
                            else node.provenance.model_copy(
                                update={
                                    "source_kind": OntologySourceKind.MANUAL,
                                    "source_id": base.revision.id,
                                }
                            )
                        )
                    },
                )

            edge_map = {
                edge_id: edge.model_copy(deep=True)
                for edge_id, edge in base_edges.items()
                if edge_id not in remove_edges
                and edge.source_node_id not in remove_nodes
                and edge.target_node_id not in remove_nodes
            }
            for edge in request.edge_upserts:
                existing_edge = base_edges.get(edge.id)
                if edge.kind not in _BUSINESS_EDGE_KINDS or (
                    existing_edge is not None and existing_edge.kind not in _BUSINESS_EDGE_KINDS
                ):
                    raise OntologyIntegrityError(
                        "PHYSICAL_ONTOLOGY_IMMUTABLE",
                        "業務 draft では BusinessRelationship/MapsTo だけを変更できます。",
                    )
                if edge.source_node_id not in node_map or edge.target_node_id not in node_map:
                    raise OntologyIntegrityError(
                        "BUSINESS_EDGE_ENDPOINT_NOT_FOUND",
                        "業務 relation の始点または終点 node が存在しません。",
                    )
                source_kind = node_map[edge.source_node_id].kind
                target_kind = node_map[edge.target_node_id].kind
                if edge.kind == OntologyEdgeKind.BUSINESS_RELATIONSHIP and (
                    source_kind not in _BUSINESS_NODE_KINDS
                    or target_kind not in _BUSINESS_NODE_KINDS
                ):
                    raise OntologyIntegrityError(
                        "BUSINESS_RELATIONSHIP_ENDPOINT_INVALID",
                        "BusinessRelationship は業務 node 同士を接続してください。",
                    )
                if edge.kind == OntologyEdgeKind.MAPS_TO and (
                    (source_kind in _BUSINESS_NODE_KINDS) == (target_kind in _BUSINESS_NODE_KINDS)
                ):
                    raise OntologyIntegrityError(
                        "BUSINESS_MAPPING_ENDPOINT_INVALID",
                        "MapsTo は業務 node と物理 schema node を 1 つずつ接続してください。",
                    )
                if edge.kind == OntologyEdgeKind.IS_A and (
                    source_kind
                    not in {
                        OntologyNodeKind.BUSINESS_ENTITY,
                        OntologyNodeKind.BUSINESS_EVENT,
                        OntologyNodeKind.OBJECT_TYPE,
                        OntologyNodeKind.INTERFACE,
                    }
                    or target_kind
                    not in {
                        OntologyNodeKind.BUSINESS_ENTITY,
                        OntologyNodeKind.BUSINESS_EVENT,
                        OntologyNodeKind.OBJECT_TYPE,
                        OntologyNodeKind.INTERFACE,
                    }
                ):
                    raise OntologyIntegrityError(
                        "ONTOLOGY_IS_A_ENDPOINT_INVALID",
                        "IsA は業務エンティティまたは業務イベント同士を接続してください。",
                    )
                if edge.kind == OntologyEdgeKind.DOMAIN and (
                    source_kind != OntologyNodeKind.PROPERTY
                    or target_kind
                    not in {
                        OntologyNodeKind.BUSINESS_ENTITY,
                        OntologyNodeKind.BUSINESS_EVENT,
                        OntologyNodeKind.OBJECT_TYPE,
                        OntologyNodeKind.INTERFACE,
                    }
                ):
                    raise OntologyIntegrityError(
                        "ONTOLOGY_DOMAIN_ENDPOINT_INVALID",
                        "Domain は業務プロパティから業務クラスへ接続してください。",
                    )
                if edge.kind == OntologyEdgeKind.GOVERNS and (
                    source_kind != OntologyNodeKind.BUSINESS_RULE
                    or target_kind not in _BUSINESS_NODE_KINDS
                ):
                    raise OntologyIntegrityError(
                        "ONTOLOGY_GOVERNS_ENDPOINT_INVALID",
                        "Governs は業務ルールから対象の業務概念へ接続してください。",
                    )
                if edge.review_status not in {
                    OntologyReviewStatus.PROPOSED,
                    OntologyReviewStatus.REVIEWED,
                    OntologyReviewStatus.APPROVED,
                }:
                    raise OntologyIntegrityError(
                        "BUSINESS_EDGE_REVIEW_STATUS_INVALID",
                        "業務 relation の review status が draft として不正です。",
                    )
                if edge.kind == OntologyEdgeKind.BUSINESS_RELATIONSHIP and not edge.join_conditions:
                    raise OntologyIntegrityError(
                        "BUSINESS_RELATIONSHIP_JOIN_REQUIRED",
                        "業務 relation には明示的な Join 条件が必要です。",
                    )
                ordinals = sorted(condition.ordinal for condition in edge.join_conditions)
                if ordinals != list(range(1, len(ordinals) + 1)):
                    raise OntologyIntegrityError(
                        "BUSINESS_JOIN_ORDINAL_INVALID",
                        "複合 Join 条件の ordinal は 1 から連続させてください。",
                    )
                for condition in edge.join_conditions:
                    self._validate_physical_column_ref(condition.left, base_nodes)
                    self._validate_physical_column_ref(condition.right, base_nodes)
                if any(join_type.value == "cross" for join_type in edge.allowed_join_types):
                    raise OntologyIntegrityError(
                        "BUSINESS_CROSS_JOIN_NOT_ALLOWED",
                        "業務 relation に CROSS JOIN を許可できません。",
                    )
                edge_map[edge.id] = edge.model_copy(
                    deep=True,
                    update={
                        "provenance": edge.provenance.model_copy(
                            update={
                                "source_kind": OntologySourceKind.MANUAL,
                                "source_id": base.revision.id,
                            }
                        )
                    },
                )

            next_version = (
                max(
                    (item.revision.version for item in self._ontologies.values()),
                    default=base.revision.version,
                )
                + 1
            )
            revision_id = stable_ontology_id(
                "ontology_revision",
                base.revision.id,
                request.model_dump(mode="json"),
                next_version,
            )
            revision = OntologyRevision(
                id=revision_id,
                version=next_version,
                status=OntologyRevisionStatus.DRAFT,
                schema_fingerprint=base.revision.schema_fingerprint,
                parent_revision_id=base.revision.id,
                note=request.note or "業務 Ontology draft",
            )
            nodes = sorted(
                (
                    node.model_copy(update={"revision_id": revision_id}, deep=True)
                    for node in node_map.values()
                ),
                key=lambda item: item.id,
            )
            edges = sorted(
                (
                    edge.model_copy(update={"revision_id": revision_id}, deep=True)
                    for edge in edge_map.values()
                ),
                key=lambda item: item.id,
            )
            registered = self.sessions.register_revision(revision, nodes=nodes, edges=edges)
            ontology = SchemaOntology(revision=registered, nodes=nodes, edges=edges)
            self._cache_ontology(ontology)
            self._ontology = ontology
            self._persist_ontology(ontology)
            return ontology.model_copy(deep=True)

    def publish_ontology_revision(
        self,
        revision_id: str,
        request: OntologyPublishRequest,
        *,
        semantic_metadata: Mapping[str, Any] | None = None,
        publication_guard: Callable[[], Any] | None = None,
    ) -> SchemaOntology:
        with self._lock:
            # 新形式の候補を旧 Markdown/global 公開入口でレビュー迂回させない。
            for document in self.store.list_documents(
                "artifacts", {"artifact_type": "profile_ontology_bundle_v2"}
            ):
                definition_bundle = json.loads(document["content"])
                if definition_bundle.get(
                    "source_revision_id"
                ) == revision_id and definition_bundle.get("definitions"):
                    raise OntologyGateBlockedError(
                        "TYPED_REVIEW_REQUIRED",
                        "型付き作業領域でこの版をレビュー・公開してください。",
                    )
            ontology = self.validate_ontology_for_publish(revision_id, etag=request.etag)
            self._load_revision_headers(force=True)
            refreshed = self._load_ontology_revision(revision_id)
            if refreshed is None:
                raise OntologyNotFoundError(
                    "ONTOLOGY_REVISION_NOT_FOUND",
                    "公開する Ontology revision が見つかりません。",
                )
            if refreshed.revision.status != OntologyRevisionStatus.DRAFT:
                raise OntologyStateConflictError(
                    "ONTOLOGY_DRAFT_REQUIRED",
                    "Draft 状態の Ontology revision だけを公開できます。",
                )
            if refreshed.revision.etag != request.etag:
                raise OntologyVersionConflictError(
                    "REVISION_ETAG_MISMATCH",
                    "Ontology revision が更新されています。再読込してください。",
                )
            ontology = refreshed
            for header in list(self._revision_headers.values()):
                if header.id != revision_id and header.status == OntologyRevisionStatus.PUBLISHED:
                    self._load_ontology_revision(header.id)
            original_headers = [
                item.revision.model_copy(deep=True)
                for item in self._ontologies.values()
                if item.revision.id == revision_id
                or item.revision.status == OntologyRevisionStatus.PUBLISHED
            ]
            if publication_guard is not None:
                publication_guard()
            published = self.sessions.publish_revision(
                revision_id,
                etag=request.etag,
                updates=semantic_metadata,
            )
            updated = SchemaOntology(
                revision=published,
                nodes=ontology.nodes,
                edges=ontology.edges,
            )
            archived_ontologies: list[SchemaOntology] = []
            for archived in self.sessions.archive_published_revisions_except(revision_id):
                previous = self._ontologies.get(archived.id)
                if previous is not None:
                    archived_ontologies.append(
                        previous.model_copy(update={"revision": archived}, deep=True)
                    )
            try:
                # Oracle では旧 published を先に archive、新 revision を最後に publish し、
                # unique active index と同一 transaction で単一 active を保証する。
                self._persist_revision_headers_atomic([*archived_ontologies, updated])
            except Exception:
                self.sessions.restore_revision_headers(original_headers)
                raise
            for archived_ontology in archived_ontologies:
                self._cache_ontology(archived_ontology)
            self._cache_ontology(updated)
            if self._ontology is not None and self._ontology.revision.id == revision_id:
                self._ontology = updated
            return updated.model_copy(deep=True)

    def finalize_semantic_publish(
        self,
        revision_id: str,
        *,
        etag: str,
        semantic_metadata: Mapping[str, Any],
        publication_guard: Callable[[], Any] | None = None,
    ) -> SchemaOntology:
        # 新 revision に必要な全 active profile view が揃うまで published head を
        # 切り替えない。生成失敗時は旧 published revision がそのまま提供される。
        self.materialize_profile_views_for_revision(revision_id)
        return self.publish_ontology_revision(
            revision_id,
            OntologyPublishRequest(etag=etag),
            semantic_metadata=semantic_metadata,
            publication_guard=publication_guard,
        )

    def _validate_typed_semantics_for_publish(self, ontology: SchemaOntology) -> None:
        node_by_id = {node.id: node for node in ontology.nodes}
        finding_codes: list[str] = []
        for node in ontology.nodes:
            definition = node.business_rule_definition
            if definition is not None:
                rule_node_id = node.id
                for target_id in definition.applies_to_node_ids:
                    if target_id not in node_by_id:
                        finding_codes.append(f"{node.id}:BUSINESS_RULE_TARGET_UNKNOWN")

                def validate_expression(
                    expression: Any,
                    *,
                    owner_node_id: str = rule_node_id,
                ) -> None:
                    if expression.property_node_id:
                        target = node_by_id.get(expression.property_node_id)
                        if target is None or target.kind != OntologyNodeKind.PROPERTY:
                            finding_codes.append(f"{owner_node_id}:BUSINESS_RULE_PROPERTY_UNKNOWN")
                    for child in expression.children:
                        validate_expression(child)

                if definition.expression is not None:
                    validate_expression(definition.expression)
            enum_definition = node.enum_value_definition
            if enum_definition is not None:
                target = node_by_id.get(enum_definition.property_node_id)
                if target is None or target.kind not in {
                    OntologyNodeKind.PROPERTY,
                    OntologyNodeKind.SHARED_PROPERTY,
                }:
                    finding_codes.append(f"{node.id}:ENUM_PROPERTY_UNKNOWN")
        if finding_codes:
            raise OntologyGateBlockedError(
                "ONTOLOGY_TYPED_SEMANTICS_INVALID",
                "業務ルールまたは列挙値に未解決の Ontology 参照があります。",
                finding_codes=finding_codes,
            )

    def _validate_metric_definitions_for_publish(self, ontology: SchemaOntology) -> None:
        node_by_id = {node.id: node for node in ontology.nodes}
        invalid_codes: list[str] = []
        dangerous_tokens = (
            ";",
            " INSERT ",
            " UPDATE ",
            " DELETE ",
            " MERGE ",
            " DROP ",
            " ALTER ",
            " CREATE ",
            " BEGIN ",
            " EXEC ",
            " CALL ",
        )
        for node in ontology.nodes:
            if node.kind != OntologyNodeKind.METRIC:
                continue
            raw = node.metadata.get("metric_definition")
            if not isinstance(raw, Mapping):
                continue
            try:
                definition = MetricDefinition.model_validate(raw)
            except Exception:
                invalid_codes.append(f"{node.id}:METRIC_DEFINITION_INVALID")
                continue
            if any(
                token in f" {sql.upper()} "
                for sql in (definition.expression_sql, definition.filter_sql)
                for token in dangerous_tokens
            ):
                invalid_codes.append(f"{node.id}:METRIC_DEFINITION_SQL_UNSAFE")
            missing_columns = [
                column_id
                for column_id in definition.base_column_node_ids
                if (column := node_by_id.get(column_id)) is None
                or column.kind != OntologyNodeKind.COLUMN
            ]
            if missing_columns:
                invalid_codes.append(f"{node.id}:METRIC_BASE_COLUMN_UNKNOWN")
            missing_grain = [
                grain_id for grain_id in definition.grain_node_ids if grain_id not in node_by_id
            ]
            if missing_grain:
                invalid_codes.append(f"{node.id}:METRIC_GRAIN_UNKNOWN")
        if invalid_codes:
            raise OntologyGateBlockedError(
                "METRIC_DEFINITION_INVALID",
                "正式指標定義に未解決の列参照または危険な SQL 断片があります。",
                finding_codes=invalid_codes,
            )

    @staticmethod
    def _has_business_elements(ontology: SchemaOntology) -> bool:
        return any(node.kind in _BUSINESS_NODE_KINDS for node in ontology.nodes) or any(
            edge.kind in _BUSINESS_EDGE_KINDS for edge in ontology.edges
        )

    def _query_ontology(self, profile_id: str = "") -> SchemaOntology:
        """Schema drift draft は、次の publish まで確認済み query scope を置換しない。

        例外として、published にもドラフトにも業務定義が 1 件も無い(純物理)場合は
        schema drift を自動 publish する。守るべき承認済み定義が無いのに古い(空の)
        published へ固定され続けると、profile view が永遠に空になるため。
        """

        if profile_id:
            from .ontology_markdown_workspace import MarkdownOntologyWorkspace

            snapshot = MarkdownOntologyWorkspace(self).snapshot(profile_id)
            if snapshot:
                graph = SchemaOntology.model_validate(snapshot["graph"])
                try:
                    self.sessions.get_revision(graph.revision.id)
                except OntologyNotFoundError:
                    self.sessions.register_revision(
                        graph.revision, nodes=graph.nodes, edges=graph.edges
                    )
                return graph
        latest = self._sync_ontology()
        published = [
            item
            for item in self._ontologies.values()
            if item.revision.status == OntologyRevisionStatus.PUBLISHED
        ]
        if not published:
            if latest.revision.status == OntologyRevisionStatus.PUBLISHED:
                return latest
            bootstrap = self.sessions.publish_revision(
                latest.revision.id,
                etag=latest.revision.etag,
            )
            bootstrapped = SchemaOntology(
                revision=bootstrap,
                nodes=latest.nodes,
                edges=latest.edges,
            )
            self._cache_ontology(bootstrapped)
            self._ontology = bootstrapped
            # nodes/edges は _sync_ontology の register 時に永続化済み(header のみ更新)
            self._persist_ontology(bootstrapped, include_graph=False)
            return bootstrapped
        best = max(
            published,
            key=lambda item: (
                item.revision.version,
                item.revision.published_at or item.revision.created_at,
                item.revision.id,
            ),
        )
        if (
            latest.revision.id != best.revision.id
            and latest.revision.status != OntologyRevisionStatus.PUBLISHED
            and latest.revision.schema_fingerprint != best.revision.schema_fingerprint
            and not self._has_business_elements(best)
            and not self._has_business_elements(latest)
        ):
            return self.publish_ontology_revision(
                latest.revision.id,
                OntologyPublishRequest(etag=latest.revision.etag),
            )
        return best

    @staticmethod
    def _validate_business_node_mapping(
        node: OntologyNode,
        physical_nodes: Mapping[str, OntologyNode],
    ) -> None:
        if (
            node.kind
            in {
                OntologyNodeKind.BUSINESS_ENTITY,
                OntologyNodeKind.PROPERTY,
                OntologyNodeKind.METRIC,
                OntologyNodeKind.BUSINESS_EVENT,
            }
            and not node.physical_mappings
            and not node.metadata.get("definition")
        ):
            raise OntologyIntegrityError(
                "BUSINESS_NODE_MAPPING_REQUIRED",
                "業務 entity/property/metric には物理 mapping が必要です。",
            )
        for mapping in node.physical_mappings:
            object_node = physical_nodes.get(mapping.object_ref.node_id)
            if object_node is None or object_node.kind not in {
                OntologyNodeKind.TABLE,
                OntologyNodeKind.VIEW,
            }:
                raise OntologyIntegrityError(
                    "BUSINESS_OBJECT_MAPPING_INVALID",
                    "業務 node の物理 object mapping が schema Ontology と一致しません。",
                )
            canonical = object_node.physical_mappings[0].object_ref
            if mapping.object_ref.model_dump() != canonical.model_dump():
                raise OntologyIntegrityError(
                    "BUSINESS_OBJECT_MAPPING_SPOOFED",
                    "業務 node の owner/object mapping が安定 ID と一致しません。",
                )
            for column in mapping.column_refs:
                OntologyApiRuntime._validate_physical_column_ref(column, physical_nodes)

    @staticmethod
    def _validate_physical_column_ref(
        column: Any,
        physical_nodes: Mapping[str, OntologyNode],
    ) -> None:
        column_node = physical_nodes.get(column.node_id)
        if column_node is None or column_node.kind != OntologyNodeKind.COLUMN:
            raise OntologyIntegrityError(
                "BUSINESS_COLUMN_MAPPING_INVALID",
                "業務定義の列 mapping が schema Ontology と一致しません。",
            )
        canonical = column_node.physical_mappings[0].column_refs[0]
        if column.model_dump() != canonical.model_dump():
            raise OntologyIntegrityError(
                "BUSINESS_COLUMN_MAPPING_SPOOFED",
                "業務定義の owner/object/column が安定 ID と一致しません。",
            )

    def ensure_profile(self, profile_id: str) -> Nl2SqlProfile:
        """profile の存在検証のみ(オントロジー同期を伴わない軽量チェック)。

        runtime の lock を取らない（業務プロファイルを読むだけで、runtime のキャッシュに触れない）。
        ジョブの準備の段階が、lock を持ったまま DB を読む別の処理を待たないため（#1155）。
        """

        return self._strict_profile(profile_id)

    def profile_view(self, profile_id: str) -> tuple[ProfileOntologyView, SchemaOntology]:
        with self._lock:
            profile = self._strict_profile(profile_id)
            ontology = self._query_ontology(profile_id)
            view = self._base_profile_view(profile, ontology)
            return view.model_copy(deep=True), ontology.model_copy(deep=True)

    @staticmethod
    def _profile_view_unresolved_object_warnings(
        profile: Nl2SqlProfile,
        view: ProfileOntologyView,
        *,
        source_label: str = "公開済みオントロジー(スキーマ情報)",
    ) -> list[str]:
        resolved = {
            key
            for item in view.physical_objects
            for key in (
                catalog_match_key(item.object_name),
                catalog_match_key(item.owner, item.object_name),
            )
        }

        def normalize(value: str) -> str:
            # 引用名は大文字小文字を保つ。`SALES."Mixed_Case"` を大文字の同名表で
            # 解決済みにしない（#561）。
            return object_match_key(value)

        return [
            f"「{name}」を {source_label} に解決できません。"
            "スキーマ情報を更新するか、オブジェクト名(owner 付き)を確認してください。"
            for name in [*profile.allowed_tables, *profile.allowed_views]
            if normalize(name) and normalize(name) not in resolved
        ]

    def prepare_build_schema_context(self, profile_id: str) -> Any:
        """AI 構築 input は Ontology ではなく Profile + DB schema catalog から作る。

        runtime の lock を取らない（業務プロファイルとカタログを読むだけで、runtime のキャッシュに
        触れない）。ジョブの準備の段階（公開版の業務定義を prompt に絞る）が、lock を持ったまま
        DB を読む別の処理を待たないため（#1155）。
        """

        profile = self._strict_profile(profile_id)
        catalog = self.legacy_service.get_catalog()
        return build_schema_context_from_catalog(profile, catalog)

    def build_proposal_scope(
        self,
        profile_id: str,
        *,
        schema_fingerprint: str,
    ) -> tuple[ProfileOntologyView, SchemaOntology]:
        """AI 抽出結果を proposal 化するため、同じ DB schema 世代の revision を用意する。"""

        with self._lock:
            profile = self._strict_profile(profile_id)
            latest = self._sync_ontology()
            current_schema = self.prepare_build_schema_context(profile_id)
            if schema_fingerprint and current_schema.schema_fingerprint != schema_fingerprint:
                raise OntologyStateConflictError(
                    "ONTOLOGY_BUILD_SCHEMA_CHANGED",
                    "AI 構築中に DB schema catalog が更新されました。"
                    "同じ入力で再実行してください。",
                )
            # 全域の最新 draft は別 Profile の作業かもしれない。物理層だけを
            # 再利用し、業務層はこの Profile の Markdown 出典から取り出す。
            state = self.ontology_markdown_state(profile_id)
            from .ontology_markdown_workspace import MarkdownOntologyWorkspace

            snapshot = MarkdownOntologyWorkspace(self).snapshot(profile_id)
            source = None
            if snapshot and (
                not state.draft_revision
                or state.draft_revision.id == snapshot["source_revision_id"]
            ):
                source = SchemaOntology.model_validate(snapshot["graph"])
            elif state.draft_revision:
                source = self.ontology_revision(state.draft_revision.id)
            else:
                # Profile 所有 Markdown 導入前の公開業務層だけは互換移行する。
                owned_revisions = {
                    self._artifact_revision_id(doc)
                    for doc in self.store.list_documents("artifacts", {})
                    if doc.get("artifact_type")
                    in {"ontology_markdown_draft", "ontology_markdown_published"}
                    and self._artifact_profile_id(doc)
                }
                legacy = [
                    g
                    for g in self._ontologies.values()
                    if g.revision.status == OntologyRevisionStatus.PUBLISHED
                    and g.revision.id not in owned_revisions
                ]
                if legacy:
                    source = max(legacy, key=lambda g: g.revision.version)
                    source_view = self._base_profile_view(profile, source)
                    source = source.model_copy(
                        update={
                            "nodes": [n for n in source.nodes if n.id in source_view.node_ids],
                            "edges": [e for e in source.edges if e.id in source_view.edge_ids],
                        }
                    )
            nodes = [n for n in latest.nodes if n.kind not in _BUSINESS_NODE_KINDS]
            if source:
                nodes.extend(n for n in source.nodes if n.kind in _BUSINESS_NODE_KINDS)
            ids = {n.id for n in nodes}
            edges = [e for e in latest.edges if e.kind not in _BUSINESS_EDGE_KINDS]
            if source:
                edges.extend(e for e in source.edges if e.kind in _BUSINESS_EDGE_KINDS)
            base_id = stable_ontology_id(
                "ontology_revision",
                "profile_build",
                profile.model_dump(mode="json"),
                latest.revision.schema_fingerprint,
                source.revision.id if source else "",
            )
            existing = self._load_ontology_revision(base_id)
            if existing:
                return self._base_profile_view(profile, existing), existing.model_copy(deep=True)
            scoped = latest.model_copy(
                update={
                    "revision": OntologyRevision(
                        id=base_id,
                        version=latest.revision.version + 1,
                        status=OntologyRevisionStatus.DRAFT,
                        schema_fingerprint=latest.revision.schema_fingerprint,
                        parent_revision_id=source.revision.id if source else latest.revision.id,
                        note="Profile の Markdown 構築基準",
                    ),
                    "nodes": [n.model_copy(update={"revision_id": base_id}) for n in nodes],
                    "edges": [
                        e.model_copy(update={"revision_id": base_id})
                        for e in edges
                        if e.source_node_id in ids and e.target_node_id in ids
                    ],
                }
            )
            scoped.revision = self.sessions.register_revision(
                scoped.revision, nodes=scoped.nodes, edges=scoped.edges
            )
            self._cache_ontology(scoped)
            self._persist_ontology(scoped)
            view = self._base_profile_view(profile, scoped)
            return view.model_copy(deep=True), scoped

    def profile_view_persistence_state(self, view: ProfileOntologyView) -> tuple[bool, bool]:
        """現在 revision の永続 view 有無と、元 Profile からの stale 状態を返す。"""

        with self._lock:
            document = self.store.get_document(
                "profile_views",
                {
                    "profile_id": view.profile_id,
                    "revision_id": view.ontology_revision_id,
                },
            )
            if document is None:
                return False, False
            stored = ProfileOntologyView.model_validate(
                self._stored_payload(document, collection="profile view")
            )
            stale = (
                stored.source_profile_etag != view.source_profile_etag
                or stored.source_profile_scope_fingerprint != view.source_profile_scope_fingerprint
            )
            return True, stale

    def profile_view_warnings(self, profile_id: str, view: ProfileOntologyView) -> list[str]:
        """公開オントロジーに解決できなかった対象オブジェクト名の診断 warning(応答用)。"""

        with self._lock:
            profile = self._strict_profile(profile_id)
        return self._profile_view_unresolved_object_warnings(profile, view)

    def patch_profile_view(
        self,
        profile_id: str,
        request: ProfileOntologyViewPatch,
    ) -> tuple[ProfileOntologyView, SchemaOntology]:
        """物理 FK / node / edge を変更せず profile 射影 metadata だけを更新する。"""

        with self._lock:
            profile = self._strict_profile(profile_id)
            ontology = self._query_ontology(profile_id)
            current = self._base_profile_view(profile, ontology)
            if request.base_etag != current.etag:
                raise OntologyVersionConflictError(
                    "PROFILE_VIEW_ETAG_MISMATCH",
                    "プロファイル範囲が更新されています。最新版を再読込してください。",
                )
            node_by_id = {node.id: node for node in ontology.nodes}
            edge_by_id = {edge.id: edge for edge in ontology.edges}
            if request.table_usages_ja is not None:
                unknown_usage_ids = set(request.table_usages_ja) - set(current.node_ids)
                if unknown_usage_ids:
                    raise OntologyIntegrityError(
                        "PROFILE_VIEW_USAGE_NODE_UNKNOWN",
                        "用途を設定した object がプロファイル範囲外です。",
                    )
            if request.column_policies is not None:
                allowed_column_keys = {
                    value
                    for node_id in current.node_ids
                    if (node := node_by_id.get(node_id)) is not None and node.kind.value == "column"
                    for value in (node.id, node.technical_name)
                }
                unknown_policy_keys = set(request.column_policies) - allowed_column_keys
                if unknown_policy_keys:
                    raise OntologyIntegrityError(
                        "PROFILE_VIEW_COLUMN_UNKNOWN",
                        "列 policy の対象がプロファイル範囲外です。",
                    )
            if request.allowed_path_ids is not None:
                invalid_paths = [
                    path_id
                    for path_id in request.allowed_path_ids
                    if path_id not in current.edge_ids
                    or (edge := edge_by_id.get(path_id)) is None
                    or edge.review_status.value != "approved"
                ]
                if invalid_paths:
                    raise OntologyIntegrityError(
                        "PROFILE_VIEW_PATH_NOT_APPROVED",
                        "未承認または範囲外の関係 path は許可できません。",
                    )
            if request.physical_scope is not None:
                # 引用規則で照合する。大文字化すると `SALES."Mixed_Case"` だけの view に
                # 大文字の同名表 `SALES.MIXED_CASE` を含められる（#563）。
                requested_scope = {
                    object_match_key(str(value))
                    for values in request.physical_scope.values()
                    for value in values
                }
                current_scope = {
                    catalog_match_key(item.object_name) for item in current.physical_objects
                } | {
                    catalog_match_key(item.owner, item.object_name)
                    for item in current.physical_objects
                }
                if requested_scope - current_scope:
                    raise OntologyIntegrityError(
                        "PROFILE_DRAFT_SCOPE_OUTSIDE_VIEW",
                        "Draft の物理 object がプロファイル範囲外です。",
                    )
            updates: dict[str, Any] = {}
            if request.table_usages_ja is not None:
                updates["table_usages_ja"] = request.table_usages_ja
            if request.column_policies is not None:
                updates["column_policies"] = request.column_policies
            if request.allowed_path_ids is not None:
                updates["allowed_path_ids"] = sorted(set(request.allowed_path_ids))
            if request.node_overrides is not None:
                updates["draft_node_overrides"] = request.node_overrides
            if request.edge_overrides is not None:
                updates["draft_edge_overrides"] = request.edge_overrides
            if request.schema_fingerprint is not None:
                updates["draft_schema_fingerprint"] = request.schema_fingerprint
            if request.physical_scope is not None:
                updates["draft_physical_scope"] = request.physical_scope
            if request.activation_scenarios_ja is not None:
                updates["activation_scenarios_ja"] = sorted(
                    {item.strip() for item in request.activation_scenarios_ja if item.strip()}
                )
            if request.activation_keywords is not None:
                updates["activation_keywords"] = sorted(
                    {item.strip() for item in request.activation_keywords if item.strip()}
                )
            if (
                request.activation_scenarios_ja is not None
                or request.activation_keywords is not None
            ):
                updates["scenario_version"] = current.scenario_version + 1
            etag_payload = {
                "view_id": current.id,
                "base_etag": current.etag,
                **{
                    key: value.model_dump(mode="json") if hasattr(value, "model_dump") else value
                    for key, value in updates.items()
                },
            }
            updates["etag"] = compute_etag(etag_payload, 1)
            updates["updated_at"] = datetime.now(UTC)
            updated = current.model_copy(update=updates, deep=True)
            self.sessions.register_profile_view(updated)
            self._profile_view_overrides[(profile.id, ontology.revision.id)] = updated
            self._persist_profile_view(updated)
            return updated.model_copy(deep=True), ontology.model_copy(deep=True)

    def _active_profiles(self) -> list[Nl2SqlProfile]:
        list_profiles = getattr(self.legacy_service, "list_profiles", None)
        if callable(list_profiles):
            try:
                profiles = list_profiles(include_archived=False)
            except TypeError:
                profiles = list_profiles()
            return [profile for profile in profiles if not profile.archived]
        profile = getattr(self.legacy_service, "profile", None)
        return [profile] if isinstance(profile, Nl2SqlProfile) and not profile.archived else []

    @staticmethod
    def _recommendation_key(value: str) -> str:
        return "".join(value.casefold().split())

    def recommend_profiles(
        self,
        request: OntologyProfileRecommendationRequest,
        *,
        allowed_profile_ids: set[str] | None = None,
    ) -> ProfileRecommendation:
        with self._lock:
            ontology = self._query_ontology()
            question_key = self._recommendation_key(request.question)
            entries: list[tuple[Nl2SqlProfile, ProfileOntologyView, list[str], list[str]]] = []
            revision_by_profile: dict[str, str] = {}
            for profile in self._active_profiles():
                if allowed_profile_ids is not None and profile.id not in allowed_profile_ids:
                    continue
                # Markdown 公開版がある profile は、問い合わせ(create_session)と
                # 同じ公開版を基準にする。
                profile_ontology = self._query_ontology(profile.id)
                revision_by_profile[profile.id] = profile_ontology.revision.id
                node_by_id = {node.id: node for node in profile_ontology.nodes}
                view = self._base_profile_view(profile, profile_ontology)
                scenario_terms = [
                    *view.activation_scenarios_ja,
                    *view.activation_keywords,
                    profile.name,
                    profile.category,
                    profile.description,
                    *profile.glossary.keys(),
                ]
                node_terms = [
                    value
                    for node_id in view.node_ids
                    if (node := node_by_id.get(node_id)) is not None
                    for value in [
                        node.business_name_ja,
                        node.description_ja,
                        *node.aliases,
                    ]
                    if value
                ]
                entries.append((profile, view, scenario_terms, node_terms))

            embedding_scores: dict[str, float] = {}
            embedding_client = getattr(self.legacy_service, "_embedding_client", None)
            configured = getattr(embedding_client, "is_configured", None)
            embed = getattr(embedding_client, "embed_texts", None)
            if entries and callable(configured) and configured() and callable(embed):
                contexts = [
                    "\n".join(
                        item for item in [*scenario_terms, *node_terms] if item and item.strip()
                    )
                    for _profile, _view, scenario_terms, node_terms in entries
                ]
                try:
                    vectors = cast(list[list[float]], embed([request.question, *contexts]))
                    query_vector = vectors[0]

                    def cosine(right: list[float]) -> float:
                        denominator = math.sqrt(
                            sum(value * value for value in query_vector)
                        ) * math.sqrt(sum(value * value for value in right))
                        if denominator <= 0.0:
                            return 0.0
                        return max(
                            0.0,
                            min(
                                1.0,
                                sum(
                                    left * value
                                    for left, value in zip(query_vector, right, strict=True)
                                )
                                / denominator,
                            ),
                        )

                    embedding_scores = {
                        profile.id: cosine(vector)
                        for (profile, _view, _scenarios, _nodes), vector in zip(
                            entries, vectors[1:], strict=True
                        )
                    }
                except Exception:
                    logger.warning(
                        "ontology_profile_recommendation_embedding_failed", exc_info=True
                    )

            candidates: list[ProfileRecommendationCandidateV2] = []
            for profile, view, scenario_terms, node_terms in entries:
                matched_scenarios = sorted(
                    {
                        term
                        for term in view.activation_scenarios_ja
                        if len(self._recommendation_key(term)) >= 2
                        and self._recommendation_key(term) in question_key
                    }
                )
                matched_terms = sorted(
                    {
                        term
                        for term in [*scenario_terms, *node_terms]
                        if len(self._recommendation_key(term)) >= 2
                        and self._recommendation_key(term) in question_key
                    },
                    key=lambda value: (-len(value), value),
                )[:12]
                embedding_score = embedding_scores.get(profile.id, 0.0)
                if not matched_terms and embedding_score < 0.45:
                    continue
                raw_score = sum(1.5 if term in matched_scenarios else 1.0 for term in matched_terms)
                score = max(min(1.0, raw_score / 4.0), embedding_score * 0.8)
                reasons = []
                if matched_scenarios:
                    reasons.append(
                        f"適用場面「{'、'.join(matched_scenarios[:2])}」と一致しました。"
                    )
                if matched_terms:
                    reasons.append(f"用語「{'、'.join(matched_terms[:4])}」が一致しました。")
                if embedding_score >= 0.45:
                    reasons.append(
                        f"場面・説明の意味類似度は {round(embedding_score * 100)}% です。"
                    )
                candidates.append(
                    ProfileRecommendationCandidateV2(
                        profile_id=profile.id,
                        profile_name=profile.name,
                        ontology_revision_id=revision_by_profile[profile.id],
                        score=score,
                        matched_scenarios_ja=matched_scenarios,
                        matched_terms=matched_terms,
                        reasons_ja=reasons,
                    )
                )
            candidates.sort(key=lambda item: (-item.score, item.profile_id))
            now = utc_now()
            recommendation = ProfileRecommendation(
                id=f"ontology_recommendation_{uuid4().hex}",
                question_hash=hashlib.sha256(request.question.strip().encode("utf-8")).hexdigest(),
                ontology_revision_id=ontology.revision.id,
                candidates=candidates[: request.limit],
                created_at=now,
                expires_at=now
                + timedelta(seconds=get_settings().nl2sql_ontology_confirmation_ttl_seconds),
            )
            self.store.save_document(
                "recommendations",
                {
                    "recommendation_id": recommendation.id,
                    "question_hash": recommendation.question_hash,
                    "status": "pending",
                    "payload": recommendation.model_dump(mode="json"),
                    "confirmation_token_hash": "",  # nosec B105
                },
            )
            record_profile_recommendation(
                "with_candidates" if recommendation.candidates else "no_candidates"
            )
            return recommendation

    def materialize_profile_view(self, profile_id: str) -> ProfileOntologyView:
        """互換 API から active revision のプロファイル範囲を明示的に再生成する。"""

        with self._lock:
            ontology = self._query_ontology(profile_id)
            profile = self._strict_profile(profile_id)
            view = self._base_profile_view(profile, ontology)
            self._persist_profile_view(view)
            return view.model_copy(deep=True)

    def delete_profile_state(self, profile_id: str) -> int:
        """Profile hard delete 後に live view/cache を除去する。監査 snapshot は保持する。"""

        with self._lock:
            deleted = 0
            # Oracle は Profile repository の同一 transaction + FK cascade で削除済み。
            # memory store には FK が無いためここで明示的に全 revision を削除する。
            if self.store.mode == "memory":
                deleted = self.store.delete_documents(
                    "profile_views",
                    {"profile_id": profile_id},
                )
                deleted += self._delete_memory_profile_ontology_records(profile_id)
            for key in [key for key in self._profile_view_overrides if key[0] == profile_id]:
                self._profile_view_overrides.pop(key, None)
            self.sessions.evict_profile_views(profile_id)
            return deleted

    def _delete_memory_profile_ontology_records(self, profile_id: str) -> int:
        """memory store の、業務プロファイルの成果物と終わったジョブの記録を消す（#1743）。

        Oracle は業務プロファイルの削除と同じ transaction で消す（`incremental_store`）。
        """

        from .ontology_definition_service import profile_ontology_session_id

        session = profile_ontology_session_id(profile_id)
        deleted = 0
        for document in self.store.list_documents("artifacts", {}):
            if document.get("profile_id") == profile_id or document.get("session_id") == session:
                deleted += self.store.delete_documents(
                    "artifacts", {"artifact_id": document["artifact_id"]}
                )
        for document in self.store.list_documents("jobs", {"profile_id": profile_id}):
            if document.get("status") in _FINISHED_JOB_STATUSES:
                deleted += self.store.delete_documents("jobs", {"job_id": document["job_id"]})
        return deleted

    def materialize_profile_views_for_revision(self, revision_id: str) -> list[ProfileOntologyView]:
        """Publish 前に全 active profile view を対象 revision へ物化する。"""

        with self._lock:
            self._ensure_store()
            ontology = self._load_ontology_revision(revision_id)
            if ontology is None:
                raise OntologyNotFoundError(
                    "ONTOLOGY_REVISION_NOT_FOUND",
                    "指定された Ontology revision が見つかりません。",
                )
            views: list[ProfileOntologyView] = []
            for profile in nl2sql_service.list_profiles(include_archived=False):
                view = self._base_profile_view(profile, ontology)
                self._persist_profile_view(view)
                views.append(view.model_copy(deep=True))
            return views

    def confirm_profile_recommendation(
        self,
        recommendation_id: str,
        request: ProfileRecommendationConfirmationRequest,
    ) -> tuple[ProfileRecommendation, str]:
        with self._lock:
            document = self.store.get_document(
                "recommendations", {"recommendation_id": recommendation_id}
            )
            if document is None:
                raise OntologyNotFoundError(
                    "PROFILE_RECOMMENDATION_NOT_FOUND",
                    "業務プロファイル推薦が見つかりません。",
                )
            recommendation = ProfileRecommendation.model_validate(document["payload"])
            if recommendation.expires_at <= utc_now():
                raise OntologyStateConflictError(
                    "PROFILE_RECOMMENDATION_EXPIRED",
                    "業務プロファイル推薦の有効期限が切れました。再判定してください。",
                )
            profile = self._strict_profile(request.selected_profile_id)
            # 推薦候補は profile ごとの基準 revision を持つ(Markdown 公開版がある profile は
            # 公開版)。候補外を手動で選んだ場合は推薦全体の revision と照合し、保存は profile の
            # 現在の基準にする。
            candidate = next(
                (item for item in recommendation.candidates if item.profile_id == profile.id),
                None,
            )
            ontology = self._query_ontology(profile.id)
            if candidate is not None:
                expected_revision_id = candidate.ontology_revision_id
                current_revision_id = ontology.revision.id
            else:
                expected_revision_id = recommendation.ontology_revision_id
                current_revision_id = self._query_ontology().revision.id
            if (
                request.selected_revision_id != expected_revision_id
                or request.selected_revision_id != current_revision_id
            ):
                raise OntologyVersionConflictError(
                    "PROFILE_RECOMMENDATION_REVISION_CHANGED",
                    "Ontology revision が更新されています。再判定してください。",
                )
            token = secrets.token_urlsafe(32)
            confirmed = recommendation.model_copy(
                update={
                    "selected_profile_id": profile.id,
                    "selected_revision_id": ontology.revision.id,
                    "confirmed_at": utc_now(),
                },
                deep=True,
            )
            self.store.save_document(
                "recommendations",
                {
                    "recommendation_id": recommendation.id,
                    "question_hash": recommendation.question_hash,
                    "status": "confirmed",
                    "payload": confirmed.model_dump(mode="json"),
                    "confirmation_token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                },
                expected_etag=str(document["etag"]),
            )
            recommended_profile_id = (
                recommendation.candidates[0].profile_id if recommendation.candidates else ""
            )
            record_profile_recommendation(
                "accepted" if recommended_profile_id == profile.id else "manually_changed"
            )
            return confirmed, token

    def _validate_profile_confirmation(
        self,
        request: QuerySessionApiCreate,
        ontology: SchemaOntology,
    ) -> str:
        if not get_settings().nl2sql_ontology_profile_confirmation_required:
            return ""
        token_hash = hashlib.sha256(request.profile_confirmation_token.encode("utf-8")).hexdigest()
        question_hash = hashlib.sha256(request.question.strip().encode("utf-8")).hexdigest()
        for document in self.store.list_documents("recommendations", {"status": "confirmed"}):
            if not secrets.compare_digest(
                str(document.get("confirmation_token_hash") or ""), token_hash
            ):
                continue
            recommendation = ProfileRecommendation.model_validate(document["payload"])
            if recommendation.expires_at <= utc_now():
                break
            if (
                recommendation.question_hash != question_hash
                or recommendation.selected_profile_id != request.profile_id
                or recommendation.selected_revision_id != ontology.revision.id
            ):
                break
            return recommendation.id
        raise OntologyGateBlockedError(
            "PROFILE_CONFIRMATION_REQUIRED",
            "推薦された業務プロファイルを確認してから問い合わせを開始してください。",
        )

    def search_ontology_context(
        self,
        profile_id: str,
        request: OntologyContextSearchRequest,
    ) -> OntologyContextSearchResult:
        # スナップショット(ロック下)→ 検索・推論・埋め込み HTTP(ロック外)。
        # LLM/埋め込み呼び出し中にグローバルロックを保持すると、1 つのハング呼び出しが
        # 全 ontology API を塞ぐため、状態参照だけをロック下で行う。
        with self._lock:
            profile = self._strict_profile(profile_id)
            ontology = self._query_ontology(profile_id)
            if (
                ontology.revision.id != request.ontology_revision_id
                or ontology.revision.status != OntologyRevisionStatus.PUBLISHED
            ):
                raise OntologyVersionConflictError(
                    "ONTOLOGY_CONTEXT_REVISION_CHANGED",
                    "指定した公開オントロジー revision は現在有効ではありません。",
                )
            view = self._base_profile_view(profile, ontology)
        retrieval_hits = retrieve_ontology_nodes(
            request.question,
            ontology,
            view,
            profile=profile,
            embedding_callback=lambda text, candidates, limit: self._embedding_hits(
                ontology.revision.id, text, candidates, limit
            ),
            limit=request.top_k,
        )
        node_by_id = {node.id: node for node in ontology.nodes}
        selected_node_ids = {hit.node_id for hit in retrieval_hits}
        inferred_node_ids = self._inferred_context_node_ids(
            profile.id,
            ontology.revision.id,
            selected_node_ids,
            allowed_node_ids=set(view.node_ids),
            max_hops=request.max_hops,
        )
        selected_node_ids.update(inferred_node_ids)
        selected_edge_ids: set[str] = set()
        hit_ids = sorted(selected_node_ids)
        for index, source_id in enumerate(hit_ids):
            for target_id in hit_ids[index + 1 :]:
                for path in find_bounded_shortest_paths(
                    ontology,
                    view,
                    source_id,
                    target_id,
                    max_hops=request.max_hops,
                    max_paths=2,
                ):
                    selected_node_ids.update(path.node_ids)
                    selected_edge_ids.update(path.edge_ids)
        semantic_kinds = {
            OntologyEdgeKind.IS_A,
            OntologyEdgeKind.DOMAIN,
            OntologyEdgeKind.RANGE,
            OntologyEdgeKind.INSTANCE_OF,
            OntologyEdgeKind.HAS_VALUE,
            OntologyEdgeKind.GOVERNS,
        }
        for edge in ontology.edges:
            if (
                edge.id in view.edge_ids
                and edge.review_status == OntologyReviewStatus.APPROVED
                and edge.source_node_id in selected_node_ids
                and edge.target_node_id in selected_node_ids
                and (edge.id in view.allowed_path_ids or edge.kind in semantic_kinds)
            ):
                selected_edge_ids.add(edge.id)
        nodes = [
            node_by_id[node_id]
            for node_id in sorted(selected_node_ids)
            if node_id in node_by_id and node_id in view.node_ids
        ]
        edges = [
            edge
            for edge in sorted(ontology.edges, key=lambda item: item.id)
            if edge.id in selected_edge_ids
        ]
        narrowed = view.model_copy(
            update={
                "node_ids": [node.id for node in nodes],
                "edge_ids": [edge.id for edge in edges],
            },
            deep=True,
        )
        artifacts = build_semantic_artifacts(ontology, narrowed)
        hits = [
            OntologyContextHit(
                node=node_by_id[hit.node_id],
                score=min(hit.score, 1.0),
                matched_terms=hit.matched_terms,
                sources=list(hit.sources),
                inference_source=(
                    "owl2rl_local"
                    if node_by_id[hit.node_id].provenance.inferred_by == "owl2rl_local"
                    else "asserted"
                ),
            )
            for hit in retrieval_hits
            if hit.node_id in node_by_id
        ]
        existing_hit_ids = {hit.node.id for hit in hits}
        hits.extend(
            OntologyContextHit(
                node=node_by_id[node_id],
                score=0.35,
                matched_terms=[],
                sources=["inference"],
                inference_source="owl2rl_local",
            )
            for node_id in sorted(inferred_node_ids - existing_hit_ids)
            if node_id in node_by_id
        )
        published_markdown = self.published_markdown_for_revision(
            ontology.revision.id,
            profile_id=profile_id,
        )
        from .ontology_markdown_workspace import MarkdownOntologyWorkspace
        from .ontology_published_context import published_context

        business_release_id = str(MarkdownOntologyWorkspace(self).head(profile_id)["snapshot_id"])
        if business_release_id:
            published_markdown = published_context(self, profile_id, business_release_id)
        context_hash = hashlib.sha256(
            canonical_json(
                {
                    "profile_id": profile_id,
                    "revision_id": ontology.revision.id,
                    "question_hash": hashlib.sha256(request.question.encode("utf-8")).hexdigest(),
                    "node_ids": [node.id for node in nodes],
                    "edge_ids": [edge.id for edge in edges],
                    "artifact_hashes": artifacts.hashes,
                    "published_markdown_hash": (
                        hashlib.sha256(published_markdown.encode("utf-8")).hexdigest()
                        if published_markdown
                        else ""
                    ),
                }
            ).encode("utf-8")
        ).hexdigest()
        result = OntologyContextSearchResult(
            profile_id=profile_id,
            profile_view_id=view.id,
            ontology_revision_id=ontology.revision.id,
            business_release_id=business_release_id,
            hits=hits,
            nodes=nodes,
            edges=edges,
            mermaid=artifacts.mermaid,
            llm_markdown=published_markdown or artifacts.llm_markdown,
            owl_turtle=artifacts.owl_turtle,
            shacl_turtle=artifacts.shacl_turtle,
            context_hash=context_hash,
        )
        record_context_hits(len(result.nodes))
        return result

    def _inferred_context_node_ids(
        self,
        profile_id: str,
        revision_id: str,
        seed_node_ids: set[str],
        *,
        allowed_node_ids: set[str],
        max_hops: int,
    ) -> set[str]:
        """物化 closure を Profile 内の検索拡張だけに利用する。"""

        if not seed_node_ids:
            return set()
        inferred_documents = [
            document
            for document in self.store.list_documents("artifacts", {"session_id": revision_id})
            if document.get("artifact_type") == "ontology_inferred_turtle"
        ]
        content = ""
        if inferred_documents:
            inferred_documents.sort(
                key=lambda document: (
                    str(document.get("updated_at") or document.get("created_at") or ""),
                    str(document.get("artifact_id") or ""),
                )
            )
            content = str(inferred_documents[-1].get("content") or "")
        else:
            # Markdown 公開版は推論結果を別 artifact にせず、公開 snapshot 本体に固定している。
            from .ontology_markdown_workspace import MarkdownOntologyWorkspace

            try:
                snapshot = MarkdownOntologyWorkspace(self).snapshot(profile_id, revision_id)
            except OntologyNotFoundError:
                snapshot = None
            if snapshot:
                content = str((snapshot.get("artifacts") or {}).get("inferred_turtle") or "")
        if not content:
            return set()
        try:
            from urllib.parse import unquote

            from rdflib import Graph, URIRef
            from rdflib.namespace import RDF, RDFS

            graph = Graph().parse(data=content, format="turtle")
            semantic_predicates = {RDF.type, RDFS.subClassOf, RDFS.domain, RDFS.range}
            adjacency: dict[str, set[str]] = {}
            iri_prefix = "urn:nl2sql:ontology:node:"
            for subject, predicate, object_ in graph:
                if predicate not in semantic_predicates:
                    continue
                if not isinstance(subject, URIRef) or not isinstance(object_, URIRef):
                    continue
                subject_value = str(subject)
                object_value = str(object_)
                if not subject_value.startswith(iri_prefix) or not object_value.startswith(
                    iri_prefix
                ):
                    continue
                left = unquote(subject_value.removeprefix(iri_prefix))
                right = unquote(object_value.removeprefix(iri_prefix))
                if left not in allowed_node_ids or right not in allowed_node_ids:
                    continue
                adjacency.setdefault(left, set()).add(right)
                adjacency.setdefault(right, set()).add(left)
        except Exception:
            logger.warning("ontology_inferred_context_load_failed", exc_info=True)
            return set()

        visited = set(seed_node_ids)
        frontier = set(seed_node_ids)
        for _hop in range(max_hops):
            next_frontier = {
                target
                for node_id in frontier
                for target in adjacency.get(node_id, set())
                if target not in visited
            }
            if not next_frontier:
                break
            visited.update(next_frontier)
            frontier = next_frontier
        return visited - seed_node_ids

    def create_session(
        self,
        request: QuerySessionApiCreate,
        *,
        actor_user_uuid: str = "",
        actor_is_system_admin: bool = False,
        profile_confirmed: bool = False,
    ) -> QuerySessionData:
        """`profile_confirmed` は、呼び出し元が業務プロファイルを明示して選んだ（MCP の
        `nl2sql_query` の `profile_id`。#1441）ときに、推薦の確認（確認の token）を省く。"""

        # スナップショット(ロック下)→ LLM 解釈(ロック外)→ 書き戻し(ロック下+再検証)。
        # Enterprise AI 呼び出し中にグローバルロックを保持すると、1 つのハング呼び出しが
        # 全 ontology API を最大 timeout×retry 分塞ぐため、HTTP はロック外で行う。
        from .ontology_markdown_workspace import MarkdownOntologyWorkspace
        from .ontology_published_context import published_context

        self._maybe_expire_idle_sessions()
        with self._lock:
            profile = self._strict_profile(request.profile_id)
            ontology = self._query_ontology(request.profile_id)
            # 公開 head は ontology を取った後に読み、同じ版を指すことを確かめる。
            # 別々に読むと、間に公開が入ったとき新しい版のグラフと古い版の公開文脈が混ざる。
            business_release_id = str(
                MarkdownOntologyWorkspace(self).head(request.profile_id)["snapshot_id"]
            )
            if business_release_id and business_release_id != ontology.revision.id:
                raise OntologyVersionConflictError(
                    "ONTOLOGY_REVISION_CHANGED",
                    "Ontology revision が更新されました。もう一度実行してください。",
                )
            ontology_current = True
            if business_release_id:
                try:
                    published_context(self, request.profile_id, business_release_id)
                except OntologyGateBlockedError:
                    # 回答前の確認はオントロジーが「あれば使う」（#1439）。公開版が Profile・
                    # Schema の変更より古いときは、業務の概念を使わずに Schema と業務プロファイルで
                    # 確かめる。
                    if request.clarification_mode != ClarificationMode.GUIDED:
                        raise
                    ontology_current = False
            # チャットは業務プロファイルを画面で選んでいる（推薦の確認は無い。#1442）。MCP の
            # nl2sql_query も profile_id を明示する（profile_confirmed。#1441）。
            recommendation_id = (
                ""
                if profile_confirmed or request.chat
                else self._validate_profile_confirmation(request, ontology)
            )
            base_view = self._base_profile_view(profile, ontology)
            allowed = self.legacy_service.resolve_allowed_objects(
                profile.id,
                request.allowed_objects,
            )
            if request.allowed_objects.table_names and not allowed.table_names:
                raise OntologyGateBlockedError(
                    "REQUEST_SCOPE_EMPTY",
                    "今回指定した object は業務プロファイルの許可範囲に含まれていません。",
                )
            view = self._narrow_profile_view(base_view, ontology, allowed)
            self.sessions.register_profile_view(view)
        if request.clarification_mode == ClarificationMode.GUIDED:
            return self._create_requirement_session(
                request,
                profile=profile,
                ontology=ontology,
                view=view,
                allowed=allowed,
                business_release_id=business_release_id,
                recommendation_id=recommendation_id,
                actor_user_uuid=actor_user_uuid,
                actor_is_system_admin=actor_is_system_admin,
                ontology_current=ontology_current,
            )
        with observe_stage("interpret"):
            intent = self._interpret_question(
                request.question,
                profile,
                ontology,
                view,
            )
        with self._lock:
            # LLM 呼び出し中に revision が公開・置換されていたら stale session を作らない
            current_ontology = self._query_ontology(request.profile_id)
            if current_ontology.revision.id != ontology.revision.id:
                raise OntologyVersionConflictError(
                    "ONTOLOGY_REVISION_CHANGED",
                    "Ontology revision が更新されました。もう一度実行してください。",
                )
            row_limit = request.row_limit
            if intent.limit is None and row_limit is not None:
                intent.limit = row_limit
            session = self.sessions.create_session(
                QuerySessionCreate(
                    question=request.question,
                    profile_id=profile.id,
                    profile_view_id=view.id,
                    ontology_revision_id=ontology.revision.id,
                    business_release_id=business_release_id,
                    intent=intent,
                    clarification_mode=request.clarification_mode,
                    actor_user_uuid=actor_user_uuid,
                    actor_is_system_admin=actor_is_system_admin,
                )
            )
            context = QueryRuntimeContext(
                allowed_objects=allowed,
                row_limit=row_limit,
                engine=request.engine,
                retrieved_node_ids=sorted(
                    {item.ontology_node_id for item in intent.entities if item.ontology_node_id}
                    | {item.ontology_node_id for item in intent.metrics if item.ontology_node_id}
                    | {item.ontology_node_id for item in intent.dimensions if item.ontology_node_id}
                ),
                profile_recommendation_id=recommendation_id,
                profile_selection_source="confirmed" if recommendation_id else "legacy",
            )
            self._session_views[session.id] = view
            self._contexts[session.id] = context
            self._persist_session(session, context=context)
            record_transition(
                session_id=session.id,
                revision_id=session.ontology_revision_id,
                state=session.status.value,
            )
            return self._session_data(session)

    # --- 回答前の確認（#1439） -------------------------------------------------

    def _requirement_today(self) -> date:
        """相対的な期間の基準日（実行日）。共通の設定の時間帯の今日（#3255）。テストで差し替える。"""

        return local_today(get_settings())

    def _requirement_max_review_rounds(self, profile_id: str = "") -> int:
        """AI の再検査の上限。業務プロファイルの確認の上限（往復）があればそれ - 1（#1839）。"""

        default = int(get_settings().nl2sql_requirement_max_review_rounds)
        if not profile_id:
            return default
        try:
            rounds = self._strict_profile(profile_id).clarification_max_rounds
        except OntologyNotFoundError:
            return default
        return max(1, rounds - 1) if rounds else default

    def _requirement_context(
        self,
        *,
        question: str,
        profile: Nl2SqlProfile,
        ontology: SchemaOntology,
        view: ProfileOntologyView,
        allowed: AllowedObjects,
        conversation: list[RequirementConversationTurn] | None = None,
        attachments: AttachmentContext | None = None,
    ) -> RequirementContext:
        try:
            # SQL の生成と同じく、業務プロファイルの範囲の Schema を読む（増分の store では表ごとの
            # 詳細。全体の catalog の cache は範囲の表を含まないことがある）。
            profile_catalog = getattr(self.legacy_service, "_generation_schema_catalog", None)
            tables = (
                profile_catalog(profile, allowed).tables
                if callable(profile_catalog)
                else self.legacy_service.get_catalog().tables
            )
        except Exception as exc:
            raise RequirementAiUnavailableError(
                "REQUIREMENT_SCHEMA_UNAVAILABLE",
                "データベースの Schema を読み込めないため、回答前の確認を開始できません。",
            ) from exc
        return build_requirement_context(
            question=question,
            profile=profile,
            catalog_tables=tables,
            allowed=allowed,
            ontology=ontology,
            view=view,
            today=self._requirement_today(),
            conversation=[turn.model_dump(mode="json") for turn in conversation or []],
            attachments=attachments,
        )

    def _requirement_conversation(
        self,
        request: QuerySessionApiCreate,
        *,
        actor_user_uuid: str,
        actor_is_system_admin: bool = False,
    ) -> list[RequirementConversationTurn]:
        """チャットの同じ会話の前のターン（質問と SQL）を読む（#1442。結果の行は読まない）。

        確認は会話の続きの一部なので、会話の持ち主だけが始められる（#3244。system_admin が他の
        利用者の会話で始めるのは `OthersContentForbiddenError` で 403）。
        """

        if not request.chat:
            return []
        reader = getattr(self.legacy_service, "chat_requirement_history", None)
        if not callable(reader):
            return []
        if not request.previous_job_id:
            if not actor_user_uuid:
                raise OntologyStateConflictError(
                    "CHAT_ACTOR_REQUIRED", "会話には認証済みの利用者が必要です。"
                )
            return []
        try:
            turns = reader(
                question=request.question,
                profile_id=request.profile_id,
                previous_job_id=request.previous_job_id,
                actor_user_uuid=actor_user_uuid,
                actor_is_system_admin=actor_is_system_admin,
            )
        except PermissionError as exc:
            # 見えない他の利用者の会話は、存在を漏らさず 404 にする（#1889）。
            raise OntologyNotFoundError(
                "CHAT_CONVERSATION_NOT_FOUND", "会話が見つかりません。"
            ) from exc
        except ValueError as exc:
            raise OntologyStateConflictError("CHAT_CONVERSATION_INVALID", str(exc)) from exc
        return [
            RequirementConversationTurn(
                question=str(turn.get("query") or "")[:10000],
                sql=str(turn.get("sql") or "")[:16000],
                status=str(turn.get("status") or ""),
            )
            for turn in turns
        ]

    def _requirement_attachments(
        self, request: QuerySessionApiCreate, *, actor_user_uuid: str, actor_is_system_admin: bool
    ) -> AttachmentContext:
        if not request.chat or (not request.attachment_ids and not request.previous_job_id):
            return AttachmentContext()
        reader = getattr(self.legacy_service, "chat_requirement_attachments", None)
        if not callable(reader):
            if request.attachment_ids:
                raise RequirementAiUnavailableError(
                    "REQUIREMENT_ATTACHMENTS_UNAVAILABLE", "添付の表の構造を読み込めません。"
                )
            return AttachmentContext()
        return cast(
            AttachmentContext,
            reader(
                JobCreateRequest(
                    question=request.question,
                    profile_id=request.profile_id,
                    chat=True,
                    previous_job_id=request.previous_job_id,
                    attachment_ids=request.attachment_ids,
                    engine=request.engine,
                ),
                actor_user_uuid=actor_user_uuid,
                actor_is_system_admin=actor_is_system_admin,
            ),
        )

    def _requirement_client(self) -> Any:
        return getattr(self.legacy_service, "_enterprise_ai_client", None)

    def _requirement_llm_options(self) -> dict[str, Any]:
        """回答前の確認の LLM の待ち時間・再試行・出力の上限（#1715）。"""

        settings = get_settings()
        return {
            "timeout_seconds": float(settings.nl2sql_requirement_timeout_seconds),
            "max_retries": int(settings.nl2sql_requirement_max_retries),
            "max_output_tokens": int(settings.nl2sql_requirement_max_output_tokens),
        }

    def _create_requirement_session(
        self,
        request: QuerySessionApiCreate,
        *,
        profile: Nl2SqlProfile,
        ontology: SchemaOntology,
        view: ProfileOntologyView,
        allowed: AllowedObjects,
        business_release_id: str,
        recommendation_id: str,
        actor_user_uuid: str,
        actor_is_system_admin: bool,
        ontology_current: bool = True,
    ) -> QuerySessionData:
        """Schema・業務プロファイル・オントロジーで、LLM 1 回の SQL の要件の下書きを作る。"""

        conversation = self._requirement_conversation(
            request,
            actor_user_uuid=actor_user_uuid,
            actor_is_system_admin=actor_is_system_admin,
        )
        context = self._requirement_context(
            question=request.question,
            profile=profile,
            ontology=ontology,
            view=view,
            allowed=allowed,
            conversation=conversation,
            attachments=self._requirement_attachments(
                request,
                actor_user_uuid=actor_user_uuid,
                actor_is_system_admin=actor_is_system_admin,
            ),
        )
        if not ontology_current:
            context.ontology_concepts = []
        if not context.tables:
            raise OntologyGateBlockedError(
                "REQUEST_SCOPE_EMPTY",
                "業務プロファイルの範囲に、使える表がありません。",
            )
        # 確認の LLM の呼び出しはジョブの trace の外なので、ここで利用量を集めて session に残す
        # （実行履歴の行でジョブの利用量と合算する。#3552）。
        with observe_stage("requirement_draft"), collect_model_usage() as usage:
            output, model = call_requirement_llm(
                self._requirement_client(),
                question=request.question,
                context=context,
                mode="draft",
                **self._requirement_llm_options(),
            )
        draft = build_draft(output, context, question=request.question, model=model)
        if not ontology_current:
            draft.warnings_ja.append(
                "公開版のオントロジーが業務プロファイル・Schema の変更より古いため、"
                "オントロジーの業務の概念を使わずに確認しました。"
            )
        with self._lock:
            current_ontology = self._query_ontology(request.profile_id)
            if current_ontology.revision.id != ontology.revision.id:
                raise OntologyVersionConflictError(
                    "ONTOLOGY_REVISION_CHANGED",
                    "Ontology revision が更新されました。もう一度実行してください。",
                )
            session = self.sessions.create_requirement_session(
                QuerySessionCreate(
                    question=request.question,
                    profile_id=profile.id,
                    profile_view_id=view.id,
                    ontology_revision_id=ontology.revision.id,
                    business_release_id=business_release_id,
                    clarification_mode=ClarificationMode.GUIDED,
                    actor_user_uuid=actor_user_uuid,
                    actor_is_system_admin=actor_is_system_admin,
                    chat=request.chat,
                    chat_previous_job_id=request.previous_job_id,
                    conversation_history=conversation,
                ),
                draft=draft,
                usage=usage.summary(),
            )
            runtime_context = QueryRuntimeContext(
                allowed_objects=allowed,
                row_limit=request.row_limit,
                engine=request.engine,
                profile_recommendation_id=recommendation_id,
                profile_selection_source="confirmed" if recommendation_id else "legacy",
                requirement_ontology_current=ontology_current,
                attachment_ids=list(request.attachment_ids),
            )
            self._session_views[session.id] = view
            self._contexts[session.id] = runtime_context
            self._persist_session(session, context=runtime_context)
            record_transition(
                session_id=session.id,
                revision_id=session.ontology_revision_id,
                state=session.status.value,
            )
            record_clarification(outcome="started")
            return self._session_data(session)

    def create_session_idempotent(
        self,
        request: QuerySessionApiCreate,
        *,
        idempotency_key: str,
        actor_user_uuid: str = "",
        actor_is_system_admin: bool = False,
    ) -> QuerySessionData:
        return self._run_session_idempotent(
            "create_query_session",
            idempotency_key,
            {
                "request": request.model_dump(mode="json"),
                "actor_user_uuid": actor_user_uuid,
                "actor_is_system_admin": actor_is_system_admin,
            },
            lambda: self.create_session(
                request,
                actor_user_uuid=actor_user_uuid,
                actor_is_system_admin=actor_is_system_admin,
            ),
        )

    def answer_clarification_idempotent(
        self,
        session_id: str,
        request: ClarificationAnswerRequest,
        *,
        idempotency_key: str,
    ) -> QuerySessionData:
        return self._run_session_idempotent(
            "answer_query_clarification",
            idempotency_key,
            {"session_id": session_id, "request": request.model_dump(mode="json")},
            lambda: self.answer_clarification(session_id, request),
        )

    def apply_requirement_idempotent(
        self,
        session_id: str,
        request: RequirementApplyRequest,
        *,
        idempotency_key: str,
    ) -> QuerySessionData:
        return self._run_session_idempotent(
            "apply_query_requirement",
            idempotency_key,
            {"session_id": session_id, "request": request.model_dump(mode="json")},
            lambda: self.apply_requirement(session_id, request),
        )

    def cancel_session_idempotent(
        self,
        session_id: str,
        *,
        idempotency_key: str,
    ) -> QuerySessionData:
        return self._run_session_idempotent(
            "cancel_query_session",
            idempotency_key,
            {"session_id": session_id},
            lambda: self.cancel_session(session_id),
        )

    def generate_sql_idempotent(
        self,
        session_id: str,
        request: GenerateSqlRequest,
        *,
        idempotency_key: str,
    ) -> QuerySessionData:
        return self._run_session_idempotent(
            "generate_query_sql",
            idempotency_key,
            {"session_id": session_id, "request": request.model_dump(mode="json")},
            lambda: self.generate_sql(session_id, request),
        )

    def published_graph_snapshot_for_job(
        self,
        *,
        profile: Nl2SqlProfile,
        allowed: AllowedObjects,
        release_id: str,
    ) -> dict[str, Any]:
        """SQL の生成のジョブの接地確認に同梱する、公開版のグラフ（業務プロファイル・要求の範囲）。

        ジョブが準備の段階で確定した公開版（`release_id`。Markdown の公開の snapshot、または
        旧方式の公開中の版）だけを読む。公開版が無いジョブの接地確認はしない（カタログから
        グラフをその場で同期・構築しない。prompt の文脈と同じ規則。#1168）。

        runtime の lock を取らない。公開版のグラフは専用のキャッシュ（短い lock）に置き、
        版のキャッシュ・profile view のキャッシュ・query session には触れない。lock を
        持ったまま DB を読む別の処理（オントロジーの画面の同期など）を待たないため。
        範囲の絞り込みは `_base_profile_view` と同じ `migrate_profile_ontology_view` で行う
        （保存済みの profile view の上書きは用途・列の方針・下書きだけで、ノード・辺の範囲を
        変えない）。
        """

        if not release_id:
            raise OntologyNotFoundError(
                "ONTOLOGY_RELEASE_NOT_PUBLISHED",
                "業務プロファイルに公開されたオントロジーがありません。",
            )
        with observe_stage("job_profile_ontology_graph_snapshot"):
            ontology = self._published_release_graph(release_id, profile_id=profile.id)
            base_view = migrate_profile_ontology_view(
                self._profile_view_migration_profile(profile), ontology, strict=False
            )
            view = self._narrow_profile_view(base_view, ontology, allowed)
            node_ids = set(view.node_ids)
            edge_ids = set(view.edge_ids)
            return {
                "revision_id": ontology.revision.id,
                "revision": ontology.revision.model_dump(mode="json"),
                "nodes": [
                    node.model_dump(mode="json") for node in ontology.nodes if node.id in node_ids
                ],
                "edges": [
                    edge.model_dump(mode="json") for edge in ontology.edges if edge.id in edge_ids
                ],
            }

    def _published_release_graph(self, release_id: str, *, profile_id: str) -> SchemaOntology:
        """公開版のグラフ（版は不変なのでキャッシュする。runtime の lock は取らない）。"""

        with self._release_graph_lock:
            cached = self._release_graph_cache.get(release_id)
            if cached is not None:
                self._release_graph_cache.move_to_end(release_id)
            generation = self._release_graph_cache_generation
        if cached is None:
            cached = self._read_published_release_graph(release_id, profile_id=profile_id)
            with self._release_graph_lock:
                if generation == self._release_graph_cache_generation:
                    self._release_graph_cache[release_id] = cached
                    self._release_graph_cache.move_to_end(release_id)
                    while len(self._release_graph_cache) > self._ontology_cache_max_revisions:
                        self._release_graph_cache.popitem(last=False)
        owner_profile_id, ontology = cached
        # Markdown の公開の snapshot は業務プロファイルの所有物。別の業務プロファイルの ID では
        # キャッシュにあっても使わない（旧方式の版は業務プロファイル間で共有する）。
        if owner_profile_id and owner_profile_id != profile_id:
            raise OntologyNotFoundError("ONTOLOGY_ARTIFACT_NOT_FOUND", "成果物が見つかりません。")
        return ontology

    def _read_published_release_graph(
        self, release_id: str, *, profile_id: str
    ) -> tuple[str, SchemaOntology]:
        """store から公開版のグラフを読む（所有する業務プロファイルの ID と組で返す）。"""

        from .ontology_markdown_workspace import SNAPSHOT, MarkdownOntologyWorkspace

        self._ensure_store()
        record = self.store.get_artifact(release_id)
        if record and record.get("artifact_type") == SNAPSHOT:
            snapshot = MarkdownOntologyWorkspace(self).snapshot_from_record(profile_id, record)
            return profile_id, SchemaOntology.model_validate(snapshot["graph"])
        document = self.store.get_document("revisions", {"revision_id": release_id})
        if document is None:
            raise OntologyNotFoundError(
                "ONTOLOGY_REVISION_NOT_FOUND",
                "指定された Ontology revision が見つかりません。",
            )
        header = OntologyRevision.model_validate(
            self._stored_payload(document, collection="revision")
        )
        nodes = [
            OntologyNode.model_validate(self._stored_payload(item, collection="node"))
            for item in self.store.list_documents("nodes", {"revision_id": release_id})
        ]
        edges = [
            OntologyEdge.model_validate(self._stored_payload(item, collection="edge"))
            for item in self.store.list_documents("edges", {"revision_id": release_id})
        ]
        return "", SchemaOntology(revision=header, nodes=nodes, edges=edges)

    def confirm_sql_idempotent(
        self,
        session_id: str,
        request: SqlConfirmationRequest,
        *,
        idempotency_key: str,
    ) -> QuerySessionData:
        return self._run_session_idempotent(
            "confirm_query_sql",
            idempotency_key,
            {"session_id": session_id, "request": request.model_dump(mode="json")},
            lambda: self.confirm_sql(session_id, request),
        )

    def execute_idempotent(
        self,
        session_id: str,
        request: SqlConfirmationRequest,
        *,
        idempotency_key: str,
        actor_user_uuid: str = "",
        actor_is_system_admin: bool = False,
    ) -> QueryExecutionData:
        data = self._run_session_idempotent(
            "execute_query_session",
            idempotency_key,
            {
                "session_id": session_id,
                "request": request.model_dump(mode="json"),
                "actor_user_uuid": actor_user_uuid,
                "actor_is_system_admin": actor_is_system_admin,
            },
            lambda: self.execute(
                session_id,
                request,
                actor_user_uuid=actor_user_uuid,
                actor_is_system_admin=actor_is_system_admin,
            ),
        )
        payload = data.model_dump()
        if payload.get("result") is None and self._results.get(session_id) is not None:
            payload["result"] = self._results[session_id]
        return QueryExecutionData.model_validate(payload)

    def _run_session_idempotent(
        self,
        operation: str,
        idempotency_key: str,
        request_payload: Mapping[str, Any],
        callback: Callable[[], QuerySessionData],
    ) -> QuerySessionData:
        request_key = idempotency_key.strip()
        if not request_key:
            raise OntologyIntegrityError(
                "IDEMPOTENCY_KEY_REQUIRED",
                "Idempotency-Key header を指定してください。",
            )
        key = _idempotency_storage_key(request_key)
        self._ensure_store()
        request_hash = hashlib.sha256(
            canonical_json({"operation": operation, "payload": request_payload}).encode("utf-8")
        ).hexdigest()
        existing = self.store.get_document(
            "idempotency",
            {"operation": operation, "idempotency_key": key},
        )
        if existing is not None:
            if existing.get("request_hash") != request_hash:
                raise OntologyVersionConflictError(
                    "IDEMPOTENCY_KEY_REUSED",
                    "同じ Idempotency-Key が異なる payload で再利用されました。",
                )
            resource_id = str(existing.get("resource_id") or "")
            if resource_id:
                return self.get_session(resource_id)
        data = callback()
        self.store.save_document(
            "idempotency",
            {
                "operation": operation,
                "idempotency_key": key,
                "request_hash": request_hash,
                "resource_id": data.session.id,
                "status": data.session.status.value,
                "payload": {
                    "operation": operation,
                    "idempotency_key": key,
                    "request_hash": request_hash,
                    "resource_id": data.session.id,
                    "status": data.session.status.value,
                },
            },
            expected_etag=None,
        )
        return data

    def _compile_sql_generation_context(
        self,
        *,
        session: QuerySession,
        intent: QuestionIntentGraph,
        view: ProfileOntologyView,
        ontology: SchemaOntology,
        runtime_context: QueryRuntimeContext,
    ) -> OntologySqlGenerationContext:
        node_by_id = {node.id: node for node in ontology.nodes}
        edge_by_id = {edge.id: edge for edge in ontology.edges}

        allowed_object_names = sorted(
            runtime_context.allowed_objects.table_names
            or [
                (f"{item.owner}.{item.object_name}" if item.owner else item.object_name)
                for item in view.physical_objects
            ]
        )
        # 列を絞った表だけ許可列で上書きし、列指定の無い表は view の全列を残す。
        # 絞った表しか残さないと、他の許可表の公開定義が prompt から丸ごと消える。
        allowed_column_names: dict[str, list[str]] = {}
        for node_id in view.node_ids:
            node = node_by_id.get(node_id)
            if node is None or node.kind != OntologyNodeKind.COLUMN:
                continue
            mapping = node.physical_mappings[0] if node.physical_mappings else None
            column = mapping.column_refs[0] if mapping and mapping.column_refs else None
            if column is None:
                continue
            object_name = (
                f"{column.owner}.{column.object_name}" if column.owner else column.object_name
            )
            allowed_column_names.setdefault(object_name, []).append(column.column_name)
        allowed_column_names = {
            key: sorted(set(values)) for key, values in allowed_column_names.items()
        }
        for table, columns in runtime_context.allowed_objects.columns.items():
            if not columns:
                continue
            key = object_match_key(str(table))
            for existing in [
                name for name in allowed_column_names if object_match_key(name) == key
            ]:
                allowed_column_names.pop(existing)
            allowed_column_names[str(table)] = sorted({str(column) for column in columns})

        selected_path = next(
            (path for path in intent.candidate_paths if path.id == intent.selected_path_id),
            None,
        )
        selected_edge_ids = selected_path.edge_ids if selected_path is not None else []
        join_summaries: list[str] = []
        approved_join_edge_ids: list[str] = []
        for edge_id in selected_edge_ids:
            edge = edge_by_id.get(edge_id)
            if edge is None:
                continue
            if (
                edge.review_status == OntologyReviewStatus.APPROVED
                and edge.id in view.allowed_path_ids
            ):
                approved_join_edge_ids.append(edge.id)
            for condition in sorted(edge.join_conditions, key=lambda item: item.ordinal):
                left = condition.left
                right = condition.right
                join_summaries.append(
                    (
                        f"{left.owner}.{left.object_name}.{left.column_name} "
                        f"{condition.operator} "
                        f"{right.owner}.{right.object_name}.{right.column_name}"
                    ).replace("..", ".")
                )

        metric_definitions: list[MetricDefinition] = []
        warnings: list[str] = []
        for metric in intent.metrics:
            node = node_by_id.get(metric.ontology_node_id)
            if node is None:
                warnings.append(f"指標 {metric.name_ja} の Ontology node を解決できません。")
                continue
            definition_raw = node.metadata.get("metric_definition")
            typed_definition = node.metadata.get("definition")
            if isinstance(typed_definition, Mapping) and typed_definition.get("kind") == "metric":
                # 初期の読み取り投影は filter_sql を落としていた。公開時の完全な
                # 型付き定義から再投影し、保存済み snapshot 自体は変更しない。
                definition_raw = typed_definition
            if isinstance(definition_raw, Mapping):
                try:
                    if definition_raw.get("kind") == "metric":
                        # 統合初版で保存された v2 snapshot も書換えず読み取る。
                        from .ontology_unified_model import (
                            DEFINITIONS,
                            metric_definition_projection,
                            physical_node_index,
                        )

                        definition = DEFINITIONS.validate_python([definition_raw])[0]
                        if definition.kind == "metric":
                            metric_definitions.append(
                                metric_definition_projection(
                                    definition,
                                    {n.technical_name: n.id for n in ontology.nodes},
                                    physical_node_index(ontology.nodes),
                                )
                            )
                            continue
                    metric_definitions.append(MetricDefinition.model_validate(definition_raw))
                    continue
                except Exception:
                    warnings.append(f"指標 {node.business_name_ja} の正式定義が不正です。")
            if metric.expression_sql.strip():
                metric_definitions.append(
                    MetricDefinition(
                        id=metric.metric_definition_id
                        or stable_ontology_id("metric_definition", metric.ontology_node_id),
                        metric_node_id=metric.ontology_node_id,
                        expression_sql=metric.expression_sql,
                        aggregation=(metric.aggregation or "none").lower(),
                        grain_node_ids=metric.grain_node_ids,
                        description_ja=metric.formula_description_ja,
                    )
                )
                continue
            mapped_columns = [
                column for mapping in node.physical_mappings for column in mapping.column_refs
            ]
            if mapped_columns:
                column = mapped_columns[0]
                expression = (
                    f"{column.owner}.{column.object_name}.{column.column_name}"
                    if column.owner
                    else f"{column.object_name}.{column.column_name}"
                )
                metric_definitions.append(
                    MetricDefinition(
                        id=stable_ontology_id("metric_definition", metric.ontology_node_id),
                        metric_node_id=metric.ontology_node_id,
                        expression_sql=expression,
                        aggregation=(metric.aggregation or "none").lower(),
                        base_column_node_ids=[column.node_id] if column.node_id else [],
                        grain_node_ids=metric.grain_node_ids,
                        description_ja=metric.formula_description_ja,
                    )
                )
            else:
                warnings.append(f"指標 {node.business_name_ja} に物理列 mapping がありません。")

        filter_summaries = [
            f"{item.label_ja} {item.operator} {item.value}" for item in intent.filters
        ]
        time_summary = ""
        if intent.time_range is not None:
            time_range = intent.time_range
            if time_range.relative_expression:
                time_summary = f"{time_range.label_ja}: {time_range.relative_expression}"
            else:
                time_summary = (
                    f"{time_range.label_ja}: {time_range.start or ''} - {time_range.end or ''}"
                )
        sort_summaries = [f"{item.target_id} {item.direction}" for item in intent.sorts]
        payload: dict[str, Any] = {
            "session_id": session.id,
            "profile_id": session.profile_id,
            "profile_view_id": view.id,
            "ontology_revision_id": session.ontology_revision_id,
            "intent_version": intent.version,
            "question_effective": intent.question_effective,
            "allowed_object_names": allowed_object_names,
            "allowed_column_names": allowed_column_names,
            "entity_node_ids": [
                item.ontology_node_id for item in intent.entities if item.ontology_node_id
            ],
            "metric_node_ids": [
                item.ontology_node_id for item in intent.metrics if item.ontology_node_id
            ],
            "dimension_node_ids": [
                item.ontology_node_id for item in intent.dimensions if item.ontology_node_id
            ],
            "filter_summaries_ja": filter_summaries,
            "time_range_summary_ja": time_summary,
            "granularity": intent.granularity,
            "sort_summaries_ja": sort_summaries,
            "limit": (intent.limit if intent.limit is not None else runtime_context.row_limit),
            "selected_path_id": intent.selected_path_id or "",
            "approved_join_edge_ids": approved_join_edge_ids,
            "join_condition_summaries": join_summaries,
            "metric_definitions": [item.model_dump(mode="json") for item in metric_definitions],
            "warnings_ja": warnings,
        }
        from .ontology_published_context import legacy_markdown_in_scope

        profile = self._strict_profile(session.profile_id)
        profile_object_names = getattr(self.legacy_service, "profile_allowed_object_names", None)
        profile_objects = (
            profile_object_names(profile)
            if callable(profile_object_names)
            else [*profile.allowed_tables, *profile.allowed_views]
        )
        published_markdown = legacy_markdown_in_scope(
            self.published_markdown_for_revision(
                ontology.revision.id,
                profile_id=session.profile_id,
            ),
            # session の view は今回の範囲に絞り込み済みなので、Profile 全体の許可 object と比べる。
            profile_objects=profile_objects,
            allowed_tables=runtime_context.allowed_objects.table_names,
            allowed_columns=runtime_context.allowed_objects.columns,
        )
        if session.business_release_id:
            from .ontology_published_context import published_context

            published_markdown = published_context(
                self, session.profile_id, session.business_release_id, allowed_column_names
            )
        payload["business_release_id"] = session.business_release_id
        qa_sql_examples = select_qa_sql_examples_from_markdown(
            published_markdown,
            intent.question_effective,
        )
        qa_sql_patterns = qa_sql_patterns_from_markdown(published_markdown)
        if not qa_sql_patterns:
            qa_sql_patterns = qa_sql_patterns_from_pairs(qa_sql_examples)
        payload["qa_sql_examples"] = [item.model_dump(mode="json") for item in qa_sql_examples]
        payload["qa_sql_patterns"] = qa_sql_patterns
        payload["llm_markdown"] = (
            published_markdown
            or build_semantic_artifacts(
                ontology,
                view,
            ).llm_markdown
        )
        payload["context_hash"] = hashlib.sha256(
            canonical_json(payload).encode("utf-8")
        ).hexdigest()
        # hash 計算後に追加する(mermaid は表現であり契約ではないため hash 非対象)。
        payload["mermaid_er"] = render_mermaid_er(ontology, view)
        return OntologySqlGenerationContext.model_validate(payload)

    def _interpret_question(
        self,
        question: str,
        profile: Nl2SqlProfile,
        ontology: SchemaOntology,
        view: ProfileOntologyView,
    ) -> QuestionIntentGraph:
        """Enterprise AI structured intent。障害時は決定論 draft をそのまま返す。

        確認だけの session（review_only）の解釈。回答前の確認（guided）は sql_requirement が担う
        （#1439。規則の下書き・閾値によるあいまいさの判定を使わない）。
        """

        deterministic = interpret_question_deterministically(
            question,
            ontology,
            view,
            profile=profile,
            embedding_callback=lambda text, candidates, limit: self._embedding_hits(
                ontology.revision.id,
                text,
                candidates,
                limit,
            ),
        )
        client = getattr(self.legacy_service, "_enterprise_ai_client", None)
        configured = getattr(client, "is_configured", None)
        generate = getattr(client, "generate", None)
        if not callable(configured) or not configured() or not callable(generate):
            return deterministic

        visible_nodes = [node for node in ontology.nodes if node.id in view.node_ids]
        visible_edges = [edge for edge in ontology.edges if edge.id in view.edge_ids]
        context_payload: dict[str, Any] = {
            "profile_id": profile.id,
            "ontology_revision_id": ontology.revision.id,
            "profile_view_id": view.id,
            "intent_contract": {
                "schema_version": _QUESTION_INTENT_SCHEMA_VERSION,
                "prompt_version": _QUESTION_INTENT_PROMPT_VERSION,
                "json_schema": _question_intent_json_schema(),
                "valid_minimum_example": _question_intent_minimum_example(
                    question=question,
                    ontology_revision_id=ontology.revision.id,
                    profile_view_id=view.id,
                ),
            },
            "allowed_nodes": [
                {
                    "id": node.id,
                    "kind": node.kind.value,
                    "business_name_ja": node.business_name_ja,
                    "technical_name": node.technical_name,
                    "aliases": node.aliases,
                }
                for node in visible_nodes
            ],
            "allowed_relationships": [
                {
                    "id": edge.id,
                    "source_node_id": edge.source_node_id,
                    "target_node_id": edge.target_node_id,
                    "name_ja": edge.relationship_name_ja,
                    "approved": edge.id in view.allowed_path_ids,
                }
                for edge in visible_edges
            ],
            "deterministic_draft": deterministic.model_dump(mode="json"),
        }
        context = json.dumps(
            context_payload,
            ensure_ascii=False,
            sort_keys=True,
        )
        system_prompt = (
            "あなたは NL2SQL の質問解釈器です。QuestionIntentGraph の JSON オブジェクトだけを返し、"
            "説明文や Markdown を付けないでください。allowed_nodes / allowed_relationships にない "
            "ID を作らず、業務上確定できない内容は blocking ambiguity として残してください。"
            "profile_view_id と ontology_revision_id は入力値を厳密に維持してください。"
            f"intent_contract.schema_version={_QUESTION_INTENT_SCHEMA_VERSION} の JSON Schema に"
            "従い、"
            "ambiguities[] は id/code/message_ja/options を必ず使ってください。"
            "kind field は使わず、options は string 配列だけにしてください。"
            "filters の値は value_json に JSON 文字列として格納してください。"
        )
        model_id = ""
        model_id_fn = getattr(client, "model_id", None)
        if callable(model_id_fn):
            try:
                model_id = str(model_id_fn())
            except Exception:
                model_id = ""
        try:
            with observe_stage("interpret_enterprise_ai"):
                raw = call_with_model_purpose(
                    "query.route",
                    generate,
                    prompt=question,
                    context=context,
                    system_prompt=system_prompt,
                    response_format=_question_intent_response_format(),
                )
            cleaned = str(raw).strip()
            try:
                parsed = _decode_question_intent_output(cleaned)
            except (ValueError, TypeError) as exc:
                _log_intent_enterprise_ai_fallback(
                    reason="invalid_json",
                    model=model_id,
                    exc=exc,
                )
                return deterministic
            try:
                intent = QuestionIntentGraph.model_validate(
                    _normalize_question_intent_payload(parsed)
                )
            except ValidationError as exc:
                _log_intent_enterprise_ai_fallback(
                    reason="schema_validation",
                    model=model_id,
                    exc=exc,
                )
                return deterministic
            referenced_node_ids = (
                {item.ontology_node_id for item in intent.entities if item.ontology_node_id}
                | {item.ontology_node_id for item in intent.metrics if item.ontology_node_id}
                | {item.ontology_node_id for item in intent.dimensions if item.ontology_node_id}
            )
            node_scope_violations = referenced_node_ids - set(view.node_ids)
            if node_scope_violations:
                _log_intent_enterprise_ai_fallback(
                    reason="scope_violation",
                    model=model_id,
                    violation_kind="node",
                    violation_count=len(node_scope_violations),
                )
                return deterministic
            referenced_edge_ids = {
                edge_id for path in intent.candidate_paths for edge_id in path.edge_ids
            }
            edge_scope_violations = referenced_edge_ids - set(view.edge_ids)
            if edge_scope_violations:
                _log_intent_enterprise_ai_fallback(
                    reason="scope_violation",
                    model=model_id,
                    violation_kind="edge",
                    violation_count=len(edge_scope_violations),
                )
                return deterministic
            return intent.model_copy(
                update={
                    "version": 1,
                    "question_original": question,
                    "profile_view_id": view.id,
                    "ontology_revision_id": ontology.revision.id,
                },
                deep=True,
            )
        except Exception as exc:
            _log_intent_enterprise_ai_fallback(
                reason="enterprise_ai_error",
                model=model_id,
                exc=exc,
            )
            return deterministic

    def _embedding_hits(
        self,
        revision_id: str,
        question: str,
        candidates: Any,
        limit: int,
    ) -> list[tuple[str, float]]:
        client = getattr(self.legacy_service, "_embedding_client", None)
        configured = getattr(client, "is_configured", None)
        embed = getattr(client, "embed_texts", None)
        if not callable(configured) or not configured() or not callable(embed):
            return []
        candidate_list = list(candidates)
        # revision 単位の埋め込みキャッシュは graph cache と同じ上限で LRU 破棄する
        # (`EMBEDDING_DIMENSIONS` 個の float × node 数 × revision 数の無制限成長を防ぐ)。
        max_revisions = max(1, int(get_settings().nl2sql_ontology_graph_cache_max_revisions))
        if revision_id in self._embeddings:
            self._embeddings[revision_id] = self._embeddings.pop(revision_id)
        while len(self._embeddings) >= max_revisions and revision_id not in self._embeddings:
            self._embeddings.pop(next(iter(self._embeddings)))
        vectors = self._embeddings.setdefault(revision_id, {})
        missing = [node for node in candidate_list if node.id not in vectors]
        try:
            if missing:
                embedded = embed(
                    [
                        "\n".join(
                            filter(
                                None,
                                [
                                    node.business_name_ja,
                                    node.technical_name,
                                    node.description_ja,
                                    *node.aliases,
                                ],
                            )
                        )
                        for node in missing
                    ]
                )
                vectors.update(
                    {node.id: vector for node, vector in zip(missing, embedded, strict=True)}
                )
                # Profile snapshot の埋め込みを全域 revision に誤保存しない。
                persisted_ontology = self._load_ontology_revision(revision_id)
                if persisted_ontology is not None:
                    for node in missing:
                        self._persist_node(persisted_ontology, node)
            query_vector = embed([question])[0]
        except Exception:
            logger.warning("ontology_embedding_retrieval_failed", exc_info=True)
            return []

        try:
            store_hits = self.store.search_node_embeddings(
                revision_id=revision_id,
                query_embedding=query_vector,
                candidate_node_ids=[node.id for node in candidate_list],
                limit=limit,
            )
            if store_hits:
                return store_hits
        except Exception:
            logger.warning("ontology_oracle_vector_search_failed", exc_info=True)

        def cosine(left: list[float], right: list[float]) -> float:
            denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(
                sum(value * value for value in right)
            )
            if denominator == 0:
                return 0.0
            return sum(a * b for a, b in zip(left, right, strict=True)) / denominator

        ranked = [
            (node.id, max(0.0, cosine(query_vector, vectors[node.id])))
            for node in candidate_list
            if node.id in vectors
        ]
        return sorted(ranked, key=lambda item: (-item[1], item[0]))[:limit]

    def get_session(self, session_id: str) -> QuerySessionData:
        with self._lock:
            self._ensure_store()
            return self._session_data(self._ensure_session_loaded(session_id))

    def _ensure_session_loaded(self, session_id: str) -> QuerySession:
        """指定 session だけを store から復元する。"""

        document = self.store.get_document("query_sessions", {"session_id": session_id})
        if document is not None:
            self._remember_store_etag("query_sessions", document)
        else:
            try:
                return self.sessions.get_session(session_id)
            except OntologyNotFoundError:
                pass
            raise OntologyNotFoundError(
                "QUERY_SESSION_NOT_FOUND",
                "query session が見つかりません。",
            )
        session = QuerySession.model_validate(
            self._stored_payload(document, collection="query session")
        )
        ontology = self._load_ontology_revision(session.ontology_revision_id)
        if ontology is None:
            raise OntologyIntegrityError(
                "RESTORED_SESSION_REVISION_MISSING",
                "永続化 query session の Ontology revision を復元できません。",
            )
        snapshot_raw = document.get("profile_view_snapshot")
        if isinstance(snapshot_raw, Mapping):
            view = ProfileOntologyView.model_validate(snapshot_raw)
        else:
            view_document = self.store.get_document(
                "profile_views",
                {
                    "profile_id": session.profile_id,
                    "revision_id": session.ontology_revision_id,
                },
            )
            if view_document is None:
                raise OntologyIntegrityError(
                    "RESTORED_SESSION_VIEW_MISSING",
                    "永続化 query session のプロファイル範囲を復元できません。",
                )
            view = ProfileOntologyView.model_validate(
                self._stored_payload(view_document, collection="profile view")
            )
        self.sessions.register_profile_view(view)
        self._session_views[session.id] = view
        context_raw = document.get("runtime_context")
        if isinstance(context_raw, Mapping):
            self._contexts[session.id] = QueryRuntimeContext.model_validate(context_raw)
        else:
            self._contexts.pop(session.id, None)
        preview_raw = document.get("preview")
        if isinstance(preview_raw, Mapping):
            self._previews[session.id] = PreviewData.model_validate(preview_raw)
        else:
            self._previews.pop(session.id, None)
        result_raw = document.get("result")
        if isinstance(result_raw, Mapping):
            self._results[session.id] = QueryResults.model_validate(result_raw)
        else:
            self._results.pop(session.id, None)
        plan_raw = document.get("performance_check")
        if isinstance(plan_raw, Mapping):
            self._plans[session.id] = ExplainPlanData.model_validate(plan_raw)
        else:
            self._plans.pop(session.id, None)
        return self.sessions.replace_session(session)

    def patch_intent(self, session_id: str, patch: GraphPatch) -> QuerySessionData:
        with self._lock:
            self._ensure_store()
            self._ensure_session_loaded(session_id)
            session = self.sessions.apply_intent_patch(session_id, patch)
            self._previews.pop(session_id, None)
            self._results.pop(session_id, None)
            self._plans.pop(session_id, None)
            self._persist_session(session)
            record_transition(
                session_id=session.id,
                revision_id=session.ontology_revision_id,
                state=session.status.value,
            )
            return self._session_data(session)

    def answer_clarification(
        self,
        session_id: str,
        request: ClarificationAnswerRequest,
    ) -> QuerySessionData:
        """1 つの項目への回答を反映する。全部の質問に答えたら、LLM で要件のまとめを再検査する。"""

        with self._lock:
            self._ensure_store()
            current = self._ensure_session_loaded(session_id)
            # 終了した（期限切れを含む）session には、LLM を呼ぶ前に答えを返す。
            OntologyQuerySessionService._assert_session_mutable(current)
            if (
                current.clarification_mode != ClarificationMode.GUIDED
                or current.requirement is None
            ):
                raise OntologyStateConflictError(
                    "GUIDED_CLARIFICATION_NOT_ENABLED",
                    "この query session では AI 要件確認を開始していません。",
                )
            if request.base_version != current.current_intent_version:
                raise OntologyVersionConflictError(
                    "INTENT_VERSION_CONFLICT",
                    "確認の内容が別の操作で更新されています。最新版を再読込してください。",
                )
            view = self._session_views.get(session_id)
            if view is None:
                view = self.sessions.get_profile_view(current.profile_view_id)
            ontology = self._ontologies.get(current.ontology_revision_id)
            if ontology is None:
                ontology = self.ontology_revision(current.ontology_revision_id)
            profile = self._strict_profile(current.profile_id)
            runtime_context = self._contexts.get(session_id)
            allowed = runtime_context.allowed_objects if runtime_context else AllowedObjects()
            max_rounds = self._requirement_max_review_rounds(current.profile_id)

        # Schema の読み込みと LLM の再検査は lock の外で行う（別の session の閲覧・中止を止めない）
        # 。
        context = self._requirement_context(
            question=current.original_question,
            profile=profile,
            ontology=ontology,
            view=view,
            allowed=allowed,
            conversation=current.conversation_history,
            attachments=self._requirement_attachments(
                QuerySessionApiCreate(
                    question=current.original_question,
                    profile_id=current.profile_id,
                    chat=current.chat,
                    previous_job_id=current.chat_previous_job_id,
                    engine=runtime_context.engine
                    if runtime_context
                    else Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
                    clarification_mode=ClarificationMode.GUIDED,
                    attachment_ids=runtime_context.attachment_ids if runtime_context else [],
                ),
                actor_user_uuid=current.actor_user_uuid,
                actor_is_system_admin=current.actor_is_system_admin,
            ),
        )
        if runtime_context is not None and not runtime_context.requirement_ontology_current:
            context.ontology_concepts = []
        try:
            draft = apply_answer(
                current.requirement,
                item_id=request.item_id,
                selected_candidate_ids=request.selected_candidate_ids,
                free_text=request.free_text,
                context=context,
            )
        except RequirementAnswerInvalidError as exc:
            raise OntologyIntegrityError("CLARIFICATION_ANSWER_INVALID", str(exc)) from exc
        review_round = current.requirement_review_round
        review_usage: RunModelUsage | None = None
        if not unresolved_items(draft) and review_round < max_rounds:
            # 原則 2: 全部の質問に答えたら、要件のまとめを LLM でもう一度検査する。利用量は session
            # に足す（#3552）。
            with observe_stage("requirement_review"), collect_model_usage() as usage:
                output, model = call_requirement_llm(
                    self._requirement_client(),
                    question=current.original_question,
                    context=context,
                    mode="review",
                    draft=draft,
                    **self._requirement_llm_options(),
                )
            draft = build_draft(
                output,
                context,
                question=current.original_question,
                model=model,
                previous=draft,
            )
            review_round += 1
            review_usage = usage.summary()
        item = next(
            (value for value in current.requirement.items if value.id == request.item_id), None
        )
        if item is None and (added := added_item_section(request.item_id)) is not None:
            # 「条件を追加」で足した項目（#1718）。
            item = next(
                (
                    value
                    for value in draft.items
                    if value.section == added and value.answered and value.source.value == "user"
                ),
                None,
            )

        with self._lock:
            # 別 request / worker が更新・取消した状態を読み直し、版を再検証してから保存する。
            self._ensure_session_loaded(session_id)
            session = self.sessions.update_requirement(
                session_id,
                base_version=request.base_version,
                draft=draft,
                answer=RequirementAnswerRecord(
                    item_id=request.item_id,
                    item_key=item.key if item else "",
                    selected_candidate_ids=request.selected_candidate_ids,
                    free_text=request.free_text.strip(),
                    draft_version=draft.version,
                ),
                review_round=review_round,
                usage=review_usage,
            )
            self._persist_session(session)
            record_transition(
                session_id=session.id,
                revision_id=session.ontology_revision_id,
                state=session.status.value,
            )
            state = build_requirement_state(session, max_review_rounds=max_rounds)
            record_clarification(
                outcome="ready" if state.can_apply and not state.unresolved_items else "answered",
                category=item.section.value if item else "none",
                turns=len(session.requirement_answers) if state.can_apply else None,
            )
            return self._session_data(session)

    def apply_requirement(
        self,
        session_id: str,
        request: RequirementApplyRequest,
    ) -> QuerySessionData:
        """確認した要件を SQL の生成に反映し、session を閉じる。"""

        with self._lock:
            self._ensure_store()
            current = self._ensure_session_loaded(session_id)
            OntologyQuerySessionService._assert_session_mutable(current)
            state = build_requirement_state(
                current, max_review_rounds=self._requirement_max_review_rounds(current.profile_id)
            )
            if not state.can_apply:
                raise OntologyGateBlockedError(
                    "GUIDED_CLARIFICATION_INCOMPLETE",
                    state.message_ja,
                    finding_codes=[*state.minimum_missing_ja]
                    or [item.label_ja for item in state.unresolved_items],
                )
            session = self.sessions.apply_requirement(session_id, base_version=request.base_version)
            self._persist_session(session)
            record_transition(
                session_id=session.id,
                revision_id=session.ontology_revision_id,
                state=session.status.value,
            )
            return self._session_data(session)

    def applied_requirement_spec(
        self,
        session_id: str,
        *,
        actor_user_uuid: str,
        actor_is_system_admin: bool,
        profile_id: str,
        question: str,
        chat: bool = False,
        previous_job_id: str | None = None,
    ) -> SqlRequirementSpec:
        """SQL の生成の job に渡す、反映済みの回答前の確認の構造化した要件（#1439）。

        本人の session・同じ業務プロファイル・反映済み・確かめた時の利用者の質問の文と同じ質問だけを
        受け付ける（画面で質問の文を書き換えたら、要件は渡さない）。チャットのジョブ（#1442）は、
        チャットで確かめた session・同じ会話の続き（前のターン）だけを受け付ける。両方の画面とも
        利用者の質問の文は書き換えず、生成だけを確かめた質問の文で行う（#1727）。
        """

        with self._lock:
            self._ensure_store()
            session = self._ensure_session_loaded(session_id)
        if session.clarification_mode != ClarificationMode.GUIDED or session.requirement is None:
            raise OntologyStateConflictError(
                "GUIDED_CLARIFICATION_NOT_ENABLED",
                "指定した session は AI 要件確認ではありません。",
            )
        if not actor_is_system_admin and session.actor_user_uuid.strip() != actor_user_uuid:
            raise OntologyStateConflictError(
                "REQUIREMENT_SESSION_FORBIDDEN",
                "他のユーザーの回答前の確認は使えません。",
            )
        if session.status != QuerySessionStatus.DONE or session.requirement_applied_at is None:
            raise OntologyStateConflictError(
                "REQUIREMENT_NOT_APPLIED",
                "回答前の確認の結果がまだ反映されていません。",
            )
        if session.profile_id != (profile_id or ""):
            raise OntologyStateConflictError(
                "REQUIREMENT_PROFILE_MISMATCH",
                "回答前の確認の業務プロファイルと、SQL を生成する業務プロファイルが違います。",
            )
        if session.chat != chat:
            raise OntologyStateConflictError(
                "REQUIREMENT_SESSION_KIND_MISMATCH",
                "チャットで確かめた要件はチャットに、SQL 生成の画面で確かめた要件は"
                " SQL 生成の画面にだけ使えます。",
            )
        if chat and (session.chat_previous_job_id or None) != (previous_job_id or None):
            raise OntologyStateConflictError(
                "REQUIREMENT_CONVERSATION_CHANGED",
                "会話が回答前の確認の後に変わりました。もう一度質問を送ってください。",
            )
        # 両方の画面とも、利用者が入力した質問の文（入力欄の値）を送る。確かめた質問の文は
        # 生成にだけ使い（`JobCreateRequest.generation_question`）、利用者の入力を書き換えない
        # （#1727）。
        if question.strip() != session.original_question.strip():
            raise OntologyStateConflictError(
                "REQUIREMENT_QUESTION_CHANGED",
                "質問の文が回答前の確認で確かめた質問と違います。"
                + (
                    "もう一度質問を送ってください。"
                    if chat
                    else "要件を外して生成するか、確認をやり直してください。"
                ),
            )
        return generation_spec(session)

    def cancel_session(self, session_id: str) -> QuerySessionData:
        with self._lock:
            self._ensure_store()
            self._ensure_session_loaded(session_id)
            session = self.sessions.cancel_session(session_id)
            self._persist_session(session)
            record_transition(
                session_id=session.id,
                revision_id=session.ontology_revision_id,
                state=session.status.value,
            )
            if session.clarification_mode == ClarificationMode.GUIDED:
                record_clarification(
                    outcome="cancelled",
                    turns=len(session.requirement_answers),
                )
            return self._session_data(session)

    def _maybe_expire_idle_sessions(self) -> None:
        """前回の掃除から間隔が空いていれば、操作が途絶えた session を終了する。"""

        if not self._session_sweep_lock.acquire(blocking=False):
            return
        try:
            now = time.monotonic()
            last = self._last_session_sweep_at
            if last is not None and now - last < _QUERY_SESSION_SWEEP_INTERVAL_SECONDS:
                return
            self._last_session_sweep_at = now
            try:
                self.expire_idle_sessions()
            except Exception:
                logger.warning("nl2sql_query_session_expiry_failed", exc_info=True)
        finally:
            self._session_sweep_lock.release()

    def expire_idle_sessions(self, *, now: datetime | None = None) -> int:
        """最後の更新から TTL を過ぎた未完了の session を cancelled にし、件数を返す（#1274）。

        画面の中止（閉じる・unmount・pagehide）が届かなかった session を残さない。保存は
        一覧で読んだ ETag の CAS で行い、同時に更新された session は飛ばす（次の掃除で見直す）。
        """

        self._ensure_store()
        current_time = now or utc_now()
        deadline = current_time - timedelta(
            seconds=int(get_settings().nl2sql_query_session_idle_ttl_seconds)
        )
        expired = 0
        for status in _EXPIRABLE_QUERY_SESSION_STATUSES:
            documents = self.store.list_documents(
                "query_sessions", {"status": status.value}, include_embedding=False
            )
            for document in documents:
                try:
                    session = QuerySession.model_validate(
                        self._stored_payload(document, collection="query session")
                    )
                except (OntologyIntegrityError, ValidationError):
                    logger.warning(
                        "nl2sql_query_session_expiry_invalid_payload",
                        extra={"session_id": document.get("session_id")},
                    )
                    continue
                updated_at = session.updated_at
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=UTC)
                if updated_at > deadline:
                    continue
                session.status = QuerySessionStatus.CANCELLED
                session.cancelled_at = current_time
                session.updated_at = current_time
                session.error_code = QUERY_SESSION_EXPIRED_CODE
                session.error_message_ja = QUERY_SESSION_EXPIRED_MESSAGE_JA
                replacement = {
                    key: value for key, value in document.items() if key not in {"version", "etag"}
                }
                replacement["status"] = session.status.value
                replacement["payload"] = session.model_dump(mode="json")
                try:
                    self._save("query_sessions", replacement, expected_etag=str(document["etag"]))
                except OntologyVersionConflict:
                    continue
                expired += 1
                with self._lock:
                    # この process の cache を捨てる。次の操作は store から読み直す。
                    self._previews.pop(session.id, None)
                    self._results.pop(session.id, None)
                    self._plans.pop(session.id, None)
                record_transition(
                    session_id=session.id,
                    revision_id=session.ontology_revision_id,
                    state=session.status.value,
                )
                if session.clarification_mode == ClarificationMode.GUIDED:
                    record_clarification(outcome="expired", turns=len(session.requirement_answers))
        if expired:
            logger.info("nl2sql_query_sessions_expired", extra={"count": expired})
        return expired

    def generate_sql(
        self,
        session_id: str,
        request: GenerateSqlRequest,
    ) -> QuerySessionData:
        with self._lock:
            self._ensure_store()
            current = self._ensure_session_loaded(session_id)
            if request.base_version != current.current_intent_version:
                raise OntologyVersionConflictError(
                    "INTENT_VERSION_CONFLICT",
                    "質問の解釈が別の操作で更新されています。最新版を再読込してください。",
                )
            if request.ontology_revision_id != current.ontology_revision_id:
                raise OntologyIntegrityError(
                    "ONTOLOGY_REVISION_MISMATCH",
                    "確認対象の Ontology revision が query session と一致しません。",
                )
            if current.clarification_mode == ClarificationMode.GUIDED:
                # 回答前の確認の session は、反映して SQL 生成の画面の job で生成する（#1439）。
                raise OntologyStateConflictError(
                    "GUIDED_SESSION_GENERATES_IN_JOB",
                    "回答前の確認の結果は、反映してから SQL 生成の画面で生成してください。",
                )
            confirmed = self.sessions.confirm_intent(
                session_id,
                intent_version=request.intent_version,
            )
            self._persist_session(confirmed)
            record_transition(
                session_id=confirmed.id,
                revision_id=confirmed.ontology_revision_id,
                state=confirmed.status.value,
            )
            intent = confirmed.intents[-1]
            context = self._require_context(session_id)
            view = self._session_views.get(session_id)
            if view is None:
                view = self.sessions.get_profile_view(confirmed.profile_view_id)
            ontology = self._ontologies.get(confirmed.ontology_revision_id)
            if ontology is None:
                ontology = self.ontology_revision(confirmed.ontology_revision_id)
            generation_context = self._compile_sql_generation_context(
                session=confirmed,
                intent=intent,
                view=view,
                ontology=ontology,
                runtime_context=context,
            )

        with observe_stage("generate_sql"):
            preview = self.legacy_service.preview(
                PreviewRequest(
                    question=intent.question_effective,
                    engine=context.engine,
                    profile_id=confirmed.profile_id,
                    allowed_objects=context.allowed_objects,
                    row_limit=(intent.limit if intent.limit is not None else context.row_limit),
                    ontology_context=generation_context,
                )
            )

        with self._lock:
            current = self._ensure_session_loaded(session_id)
            if (
                current.status != QuerySessionStatus.GENERATING_SQL
                or current.current_intent_version != confirmed.current_intent_version
                or current.intent_confirmed_version != request.intent_version
            ):
                raise OntologyVersionConflictError(
                    "QUERY_SESSION_CHANGED_DURING_SQL_GENERATION",
                    "SQL 生成中に query session が更新されています。最新版を再読込してください。",
                )
            if current.ontology_revision_id != confirmed.ontology_revision_id:
                raise OntologyIntegrityError(
                    "ONTOLOGY_REVISION_CHANGED_DURING_SQL_GENERATION",
                    "SQL 生成中に Ontology revision が変更されています。再読込してください。",
                )
            sql = preview.executable_sql.strip() or preview.sql
            with observe_stage("validate_sql"):
                session = self.sessions.register_generated_sql(
                    session_id,
                    sql,
                    generation_context_hash=generation_context.context_hash,
                )
            explain = getattr(self.legacy_service, "explain_sql", None)
            if callable(explain):
                with observe_stage("explain_plan"):
                    self._plans[session_id] = ExplainPlanData.model_validate(explain(sql))
            self._previews[session_id] = preview
            self._persist_session(session)
            artifact = session.sql_artifacts[-1]
            self._persist_artifact(session.id, artifact)
            record_findings(list(artifact.validation_report.findings))
            record_transition(
                session_id=session.id,
                revision_id=session.ontology_revision_id,
                state=session.status.value,
            )
            return self._session_data(session)

    def confirm_sql(
        self,
        session_id: str,
        request: SqlConfirmationRequest,
    ) -> QuerySessionData:
        with self._lock:
            self._ensure_store()
            self._ensure_session_loaded(session_id)
            session = self.sessions.confirm_sql(session_id, request)
            self._persist_session(session)
            return self._session_data(session)

    def execute(
        self,
        session_id: str,
        request: SqlConfirmationRequest,
        *,
        actor_user_uuid: str = "",
        actor_is_system_admin: bool = False,
    ) -> QueryExecutionData:
        with self._lock:
            self._ensure_store()
            before = self._ensure_session_loaded(session_id)
            artifact = next(
                (
                    item
                    for item in before.sql_artifacts
                    if item.id == before.current_sql_artifact_id
                ),
                None,
            )
            if artifact is None:
                raise OntologyStateConflictError(
                    "SQL_ARTIFACT_NOT_GENERATED",
                    "実行する SQL がまだ生成されていません。"
                    "SQL を生成・確認してから実行してください。",
                )
            context = self._require_context(session_id)
            confirmed_intent_version = before.intent_confirmed_version
            current_intent = next(
                (item for item in before.intents if item.version == confirmed_intent_version),
                before.intents[-1] if before.intents else None,
            )
            execution_row_limit = (
                current_intent.limit
                if current_intent is not None and current_intent.limit is not None
                else context.row_limit
            )
            self.sessions.authorize_execution(session_id, request, sql=artifact.sql)
            executing = self.sessions.get_session(session_id)
            self._persist_session(executing)
            record_transition(
                session_id=executing.id,
                revision_id=executing.ontology_revision_id,
                state=executing.status.value,
            )

        try:
            with observe_stage("execute"):
                allowed = self.legacy_service.resolve_allowed_objects(
                    executing.profile_id,
                    context.allowed_objects,
                )
                with actor_scope(
                    actor_user_uuid,
                    is_system_admin=actor_is_system_admin,
                ):
                    safety, executable_sql, result = self.legacy_service.execute_sql(
                        sql=artifact.sql,
                        allowed=allowed,
                        row_limit=execution_row_limit,
                    )
            if not safety.is_safe:
                with self._lock:
                    failed = self.sessions.fail_session(
                        session_id,
                        code="LEGACY_SAFETY_BLOCKED",
                        message_ja=(
                            safety.blocked_reason or "既存 SQL safety gate が実行を阻止しました。"
                        ),
                    )
                    self._persist_session(failed)
                raise OntologyGateBlockedError(
                    "LEGACY_SAFETY_BLOCKED",
                    safety.blocked_reason or "既存 SQL safety gate が実行を阻止しました。",
                )
        except OntologyServiceError:
            raise
        except Exception as exc:
            with self._lock:
                failed = self.sessions.fail_session(
                    session_id,
                    code="SQL_EXECUTION_FAILED",
                    message_ja="SQL の実行に失敗しました。",
                )
                self._persist_session(failed)
            raise OntologyGateBlockedError(
                "SQL_EXECUTION_FAILED",
                "SQL の実行に失敗しました。",
            ) from exc

        with self._lock:
            current = self._ensure_session_loaded(session_id)
            if (
                current.status != QuerySessionStatus.EXECUTING
                or current.current_sql_artifact_id != artifact.id
            ):
                raise OntologyStateConflictError(
                    "QUERY_SESSION_CHANGED_DURING_SQL_EXECUTION",
                    "SQL 実行中に query session が更新されています。最新版を再読込してください。",
                )
            session = self.sessions.complete_execution(
                session_id,
                row_count=result.total,
                result_ref=f"query-session:{session_id}",
            )
            self._results[session_id] = result
            self._persist_session(session, result=result)
            record_transition(
                session_id=session.id,
                revision_id=session.ontology_revision_id,
                state=session.status.value,
            )
            data = self._session_data(session)

        record_history = getattr(self.legacy_service, "record_ontology_history", None)
        if callable(record_history):
            elapsed_ms: int | None = None
            if session.execution is not None and session.execution.finished_at is not None:
                elapsed_ms = max(
                    0,
                    int(
                        (
                            session.execution.finished_at - session.execution.started_at
                        ).total_seconds()
                        * 1000
                    ),
                )
            generation_elapsed_ms: int | None = None
            engine_timings: list[dict[str, object]] = []
            stage_timings: list[object] = []
            preview = self._previews.get(session.id)
            if preview is not None:
                raw_generation_elapsed_ms = preview.engine_meta.get("generation_elapsed_ms")
                if isinstance(raw_generation_elapsed_ms, int):
                    generation_elapsed_ms = raw_generation_elapsed_ms
                raw_engine_timings = preview.engine_meta.get("engine_timings")
                if isinstance(raw_engine_timings, list):
                    engine_timings = [item for item in raw_engine_timings if isinstance(item, dict)]
                if preview.timing is not None:
                    stage_timings = list(preview.timing.stage_timings)
            try:
                record_history(
                    session_id=session.id,
                    question=session.original_question,
                    rewritten_question=session.intents[-1].question_effective,
                    engine=context.engine,
                    generated_sql=artifact.sql,
                    executable_sql=executable_sql,
                    profile_id=session.profile_id,
                    result=result,
                    ontology_trace_summary=data.ontology_trace_summary,
                    elapsed_ms=elapsed_ms,
                    generation_elapsed_ms=generation_elapsed_ms,
                    engine_timings=engine_timings,
                    stage_timings=stage_timings,
                    actor_user_uuid=actor_user_uuid,
                )
            except Exception:
                # SQL は既に実行済みなので history 投影障害で結果を失わせない。
                logger.warning(
                    "ontology_history_projection_failed",
                    extra={"session_id": session.id},
                    exc_info=True,
                )
        payload = data.model_dump()
        payload["result"] = result
        return QueryExecutionData.model_validate(payload)

    def create_proposal(
        self,
        session_id: str,
        request: ImprovementProposalRequest,
    ) -> tuple[OntologyProposal, QuerySessionData]:
        with self._lock:
            self._ensure_store()
            session_before = self._ensure_session_loaded(session_id)
            if request.base_revision_id and (
                request.base_revision_id != session_before.ontology_revision_id
            ):
                raise OntologyIntegrityError(
                    "PROPOSAL_REVISION_MISMATCH",
                    "改善提案の Ontology revision が query session と一致しません。",
                )
            if request.intent_version and (
                request.intent_version != session_before.current_intent_version
            ):
                raise OntologyVersionConflictError(
                    "INTENT_VERSION_CONFLICT",
                    "改善提案の元になった質問解釈が更新されています。",
                )
            patch = request.patch
            if patch is None:
                current_intent = session_before.intents[-1]
                from .ontology_models import GraphPatchOperation

                patch = GraphPatch(
                    base_version=current_intent.version,
                    summary_ja=request.summary or "query session からの Ontology 改善提案",
                    operations=[
                        GraphPatchOperation(
                            op="replace",
                            path="/question_effective",
                            value=current_intent.question_effective,
                            reason_ja="確認済みの質問解釈を改善提案として記録",
                        )
                    ],
                )
            proposal = self.sessions.create_improvement_proposal(
                session_id,
                title_ja=request.title_ja or request.summary or "Ontology 改善提案",
                description_ja=request.description_ja,
                patch=patch,
                kind=request.kind,
                proposal_payload=request.proposal_payload.model_copy(
                    update={"kind": request.kind},
                    deep=True,
                ),
            )
            session = self.sessions.get_session(session_id)
            self._persist_proposal(proposal)
            self._persist_session(session)
            return proposal, self._session_data(session)

    def get_proposal(self, proposal_id: str) -> OntologyProposal:
        with self._lock:
            self._ensure_store()
            return self._ensure_proposal_loaded(proposal_id)

    def _ensure_proposal_loaded(self, proposal_id: str) -> OntologyProposal:
        document = self.store.get_document("proposals", {"proposal_id": proposal_id})
        if document is not None:
            self._remember_store_etag("proposals", document)
        else:
            try:
                return self.sessions.get_proposal(proposal_id)
            except OntologyNotFoundError:
                pass
            raise OntologyNotFoundError(
                "ONTOLOGY_PROPOSAL_NOT_FOUND",
                "Ontology proposal が見つかりません。",
            )
        proposal = OntologyProposal.model_validate(
            self._stored_payload(document, collection="proposal")
        )
        return self._restore_persisted_proposal(proposal)

    def _restore_persisted_proposal(self, proposal: OntologyProposal) -> OntologyProposal:
        ontology = self._load_ontology_revision(proposal.base_revision_id)
        if ontology is None:
            raise OntologyIntegrityError(
                "RESTORED_PROPOSAL_REVISION_MISSING",
                "永続化 proposal の Ontology revision を復元できません。",
            )
        if not proposal.session_id.startswith("ontology_build:"):
            self._ensure_session_loaded(proposal.session_id)
        return self.sessions.replace_proposal(proposal)

    def list_profile_proposals(self, profile_id: str) -> list[OntologyProposal]:
        with self._lock:
            self._ensure_store()
            self._strict_profile(profile_id)
            for document in self.store.list_documents("proposals", {"profile_id": profile_id}):
                try:
                    self._remember_store_etag("proposals", document)
                    proposal = OntologyProposal.model_validate(
                        self._stored_payload(document, collection="proposal")
                    )
                    self._restore_persisted_proposal(proposal)
                except Exception:
                    logger.warning(
                        "ontology_proposal_restore_skipped",
                        exc_info=True,
                        extra={
                            "profile_id": profile_id,
                            "proposal_id": str(document.get("proposal_id") or ""),
                            "session_id": str(document.get("session_id") or ""),
                        },
                    )
            return self.sessions.list_proposals_by_profile(profile_id)

    def supersede_profile_proposals(self, profile_id: str) -> None:
        """当該 profile の既存提案を一掃(SUPERSEDED)し、永続化して durable 化する。

        新規 AI 構築の実行ごとにレビュー一覧をリセットするために呼ぶ。"""

        with self._lock:
            self._ensure_store()
            self._strict_profile(profile_id)
            for proposal in self.sessions.supersede_proposals_by_profile(profile_id):
                self._persist_proposal(proposal)

    def create_build_proposal(
        self,
        *,
        profile_id: str,
        job_id: str,
        title_ja: str,
        description_ja: str,
        kind: OntologyProposalKind,
        proposal_payload: OntologyProposalPayload,
        base_revision_id: str | None = None,
    ) -> OntologyProposal:
        """AI 構築 job の生成物を承認フローへ登録する(query session 非依存)。"""

        with self._lock:
            self._ensure_store()
            profile = self._strict_profile(profile_id)
            if base_revision_id:
                ontology = self._ontologies.get(base_revision_id)
                if ontology is None:
                    ontology = self._load_ontology_revision(base_revision_id)
                if ontology is None:
                    raise OntologyNotFoundError(
                        "ONTOLOGY_REVISION_NOT_FOUND",
                        "提案の基準 Ontology revision が見つかりません。",
                    )
            else:
                ontology = self._query_ontology(profile_id)
            proposal = self.sessions.create_build_proposal(
                session_id=f"ontology_build:{job_id}",
                profile_id=profile.id,
                base_revision_id=ontology.revision.id,
                title_ja=title_ja or "AI オントロジー提案",
                description_ja=description_ja,
                kind=kind,
                proposal_payload=proposal_payload,
            )
            self._persist_proposal(proposal)
            return proposal

    def _accept_base_revision(
        self,
        proposals: list[OntologyProposal] | None = None,
    ) -> SchemaOntology:
        """提案を積み上げる基準 revision。

        通常は現行 published と同じ schema fingerprint の revision(published + そこから
        派生した draft)の中で最新を選ぶ。AI 構築が stale published を避けて最新
        schema revision から proposal を作った場合は、proposal の base revision と同じ
        schema fingerprint の系列を選ぶ。永続化 store には過去のスキーマ世代の draft が
        残り得るため、単純な max(version) だと古い物理 schema の draft を拾って upsert
        検証が矛盾(409)する。fingerprint で系列を固定して防ぐ。
        """

        fingerprint = ""
        if proposals:
            self._sync_ontology()
            base_fingerprints: set[str] = set()
            for proposal in proposals:
                ontology = self._ontologies.get(proposal.base_revision_id)
                if ontology is None:
                    ontology = self._load_ontology_revision(proposal.base_revision_id)
                if ontology is None:
                    raise OntologyNotFoundError(
                        "ONTOLOGY_REVISION_NOT_FOUND",
                        "提案の基準 Ontology revision が見つかりません。",
                    )
                base_fingerprints.add(ontology.revision.schema_fingerprint)
            if len(base_fingerprints) != 1:
                raise OntologyStateConflictError(
                    "ONTOLOGY_PROPOSAL_SCHEMA_MIXED",
                    "異なるスキーマ世代の提案は同時に承認できません。AI 構築を再実行してください。",
                )
            fingerprint = next(iter(base_fingerprints))
        else:
            published = self._query_ontology()
            fingerprint = published.revision.schema_fingerprint
        candidates = [
            item
            for item in self._ontologies.values()
            if item.revision.schema_fingerprint == fingerprint
            and item.revision.status != OntologyRevisionStatus.ARCHIVED
        ]
        if not candidates:
            if proposals:
                raise OntologyNotFoundError(
                    "ONTOLOGY_REVISION_NOT_FOUND",
                    "提案の基準 Ontology revision が見つかりません。",
                )
            return published
        return max(
            candidates,
            key=lambda item: (
                item.revision.version,
                item.revision.created_at,
                item.revision.id,
            ),
        )

    def _proposal_payloads_upsert_draft_request(
        self,
        payloads: list[OntologyProposalPayload],
        base: SchemaOntology,
        *,
        titles: list[str] | None = None,
        note: str = "",
    ) -> OntologyDraftRequest:
        """複数 payload の node/edge upserts を 1 つの承認済み draft request へ合成する。"""

        base_node_ids = {node.id for node in base.nodes}
        node_map: dict[str, OntologyNode] = {}
        synthetic_ids: set[str] = set()
        edge_map: dict[str, OntologyEdge] = {}
        for payload in payloads:
            values = payload.values
            for raw in values.get("node_upserts") or []:
                node = OntologyNode.model_validate(raw)
                is_synthetic = bool(node.metadata.get("synthetic_endpoint"))
                # 関係提案の合成 endpoint は、実在ノードや命名提案の upsert を上書きしない。
                if is_synthetic and (
                    node.id in base_node_ids
                    or (node.id in node_map and node.id not in synthetic_ids)
                ):
                    continue
                node_map[node.id] = node.model_copy(
                    update={
                        "revision_id": base.revision.id,
                        "review_status": OntologyReviewStatus.APPROVED,
                    },
                    deep=True,
                )
                if is_synthetic:
                    synthetic_ids.add(node.id)
                else:
                    synthetic_ids.discard(node.id)
            for raw in values.get("edge_upserts") or []:
                edge = OntologyEdge.model_validate(raw)
                edge_map[edge.id] = edge.model_copy(
                    update={
                        "revision_id": base.revision.id,
                        "review_status": OntologyReviewStatus.APPROVED,
                    },
                    deep=True,
                )
        title_text = "、".join((titles or [])[:5])
        return OntologyDraftRequest(
            base_etag=base.revision.etag,
            note=note or f"AI 提案を承認: {title_text}",
            node_upserts=sorted(node_map.values(), key=lambda node: node.id),
            edge_upserts=sorted(edge_map.values(), key=lambda edge: edge.id),
        )

    def _proposals_upsert_draft_request(
        self,
        proposals: list[OntologyProposal],
        base: SchemaOntology,
    ) -> OntologyDraftRequest:
        """複数 proposal の node/edge upserts を 1 つの承認済み draft request へ合成する。"""

        return self._proposal_payloads_upsert_draft_request(
            [proposal.proposal_payload for proposal in proposals],
            base,
            titles=[proposal.title_ja for proposal in proposals],
        )

    def _draft_for_accepted_proposals(
        self,
        proposals: list[OntologyProposal],
    ) -> SchemaOntology:
        for proposal in proposals:
            draft_revision_id = str(proposal.proposal_payload.values.get("draft_revision_id") or "")
            if not draft_revision_id:
                continue
            draft = self._load_ontology_revision(draft_revision_id)
            if draft is not None:
                return draft
        return self._accept_base_revision(proposals)

    def create_build_markdown_draft(
        self,
        *,
        profile_id: str,
        base_revision_id: str,
        payloads: list[OntologyProposalPayload],
        titles: list[str],
        markdown: str,
        note: str,
        prepared_base: SchemaOntology | None = None,
        unified_definitions: list[Any] | None = None,
        on_progress: Callable[[str], None] | None = None,
        definition_conflicts: list[str] | None = None,
        definition_diagnostics: list[Any] | None = None,
    ) -> tuple[SchemaOntology, dict[str, Any]]:
        """AI 構築結果を proposal 登録せず、承認済み draft revision として保存する。"""

        with self._lock:
            self._ensure_store()
            self._strict_profile(profile_id)
            if on_progress is not None:
                on_progress("Markdown 下書きの基準 revision を確認しています…")
            base = (
                prepared_base
                if prepared_base is not None and prepared_base.revision.id == base_revision_id
                else self._load_ontology_revision(base_revision_id)
            )
            if base is None:
                raise OntologyNotFoundError(
                    "ONTOLOGY_REVISION_NOT_FOUND",
                    "Markdown 下書きの基準オントロジー revision が見つかりません。",
                )
            request = self._proposal_payloads_upsert_draft_request(
                payloads,
                base,
                titles=titles,
                note=note or "AI 構築から Markdown 下書きを生成",
            )
            if unified_definitions is not None:
                from .ontology_unified_model import project_graph

                candidates = base.model_copy(
                    update={
                        "nodes": list(
                            {n.id: n for n in [*base.nodes, *request.node_upserts]}.values()
                        ),
                        "edges": list(
                            {e.id: e for e in [*base.edges, *request.edge_upserts]}.values()
                        ),
                    }
                )
                unified = project_graph(candidates, unified_definitions)
                ids = {n.id for n in unified.nodes}
                edge_ids = {e.id for e in unified.edges}
                request = request.model_copy(
                    update={
                        "node_upserts": [
                            n for n in unified.nodes if n.kind in _BUSINESS_NODE_KINDS
                        ],
                        "edge_upserts": [
                            e for e in unified.edges if e.kind in _BUSINESS_EDGE_KINDS
                        ],
                        "remove_node_ids": [
                            n.id
                            for n in base.nodes
                            if n.kind in _BUSINESS_NODE_KINDS and n.id not in ids
                        ],
                        "remove_edge_ids": [
                            e.id
                            for e in base.edges
                            if e.kind in _BUSINESS_EDGE_KINDS and e.id not in edge_ids
                        ],
                    }
                )
            if on_progress is not None:
                on_progress("下書き revision を保存しています…")
            draft = self.create_ontology_draft(
                base.revision.id,
                request,
                prepared_base=base,
            )
            if on_progress is not None:
                on_progress(
                    f"下書き revision v{draft.revision.version} を保存しました。"
                    "Markdown 成果物を保存しています…"
                )
            artifact = self._save_markdown_artifact(
                profile_id=profile_id,
                revision=draft.revision,
                artifact_type=_MARKDOWN_DRAFT_ARTIFACT_TYPE,
                markdown=markdown,
            )
            if unified_definitions is not None:
                from .ontology_markdown_workspace import MarkdownOntologyWorkspace

                MarkdownOntologyWorkspace(self).save_generated(
                    profile_id,
                    draft.revision.id,
                    markdown,
                    unified_definitions,
                    definition_conflicts or [],
                    definition_diagnostics or [],
                )
            if on_progress is not None:
                on_progress("Markdown 成果物を保存しました。")
            return draft, artifact

    def accept_proposals(
        self, proposal_ids: list[str]
    ) -> tuple[list[OntologyProposal], SchemaOntology]:
        """複数 proposal を 1 つの draft revision へまとめて承認する(N 回の draft 生成を回避)。"""

        with self._lock:
            self._ensure_store()
            unique_proposal_ids = list(dict.fromkeys(proposal_ids))
            proposals = [self._ensure_proposal_loaded(pid) for pid in unique_proposal_ids]
            if not proposals:
                raise OntologyIntegrityError(
                    "ONTOLOGY_PROPOSAL_IDS_REQUIRED", "承認する提案を指定してください。"
                )
            submitted: list[OntologyProposal] = []
            already_accepted: list[OntologyProposal] = []
            for proposal in proposals:
                if proposal.status == OntologyProposalStatus.SUBMITTED:
                    submitted.append(proposal)
                elif proposal.status == OntologyProposalStatus.ACCEPTED:
                    already_accepted.append(proposal)
                else:
                    self._assert_proposal_reviewable(proposal, action_ja="承認")
            if not submitted:
                return proposals, self._draft_for_accepted_proposals(already_accepted)
            base = self._accept_base_revision(submitted)
            request = self._proposals_upsert_draft_request(submitted, base)
            try:
                draft = self.create_ontology_draft(base.revision.id, request)
            except OntologyIntegrityError as exc:
                # 提案が現在のスキーマ世代と一致しない(古い提案など)場合の案内。
                raise OntologyStateConflictError(
                    "ONTOLOGY_PROPOSAL_STALE",
                    "提案を現在の Ontology に適用できません。"
                    "スキーマ情報を更新し、AI 構築を再実行してください。"
                    f"(詳細: {exc.message_ja})",
                ) from exc
            accepted_list: list[OntologyProposal] = []
            for proposal in submitted:
                accepted = proposal.model_copy(
                    update={
                        "status": OntologyProposalStatus.ACCEPTED,
                        "proposal_payload": proposal.proposal_payload.model_copy(
                            update={
                                "values": {
                                    **proposal.proposal_payload.values,
                                    "draft_revision_id": draft.revision.id,
                                }
                            },
                            deep=True,
                        ),
                    },
                    deep=True,
                )
                accepted_list.append(accepted)
            self._persist_proposals_atomic(accepted_list)
            for accepted in accepted_list:
                self.sessions.update_proposal(accepted)
            accepted_by_id = {proposal.id: proposal for proposal in accepted_list}
            return [
                accepted_by_id.get(proposal.id, proposal)
                for proposal in proposals
                if proposal.status == OntologyProposalStatus.ACCEPTED
                or proposal.id in accepted_by_id
            ], draft

    def accept_proposal(self, proposal_id: str) -> OntologyProposalReviewData:
        with self._lock:
            self._ensure_store()
            proposal = self._ensure_proposal_loaded(proposal_id)
            self._assert_proposal_reviewable(proposal, action_ja="承認")
            accepted_list, draft = self.accept_proposals([proposal_id])
            return OntologyProposalReviewData(
                proposal=accepted_list[0],
                draft=OntologyGraphData(
                    revision=draft.revision,
                    nodes=draft.nodes,
                    edges=draft.edges,
                ),
            )

    def reject_proposal(self, proposal_id: str) -> OntologyProposalReviewData:
        with self._lock:
            self._ensure_store()
            proposal = self._ensure_proposal_loaded(proposal_id)
            self._assert_proposal_reviewable(proposal, action_ja="却下")
            rejected = proposal.model_copy(
                update={"status": OntologyProposalStatus.REJECTED},
                deep=True,
            )
            rejected = self.sessions.update_proposal(rejected)
            self._persist_proposal(rejected)
            return OntologyProposalReviewData(proposal=rejected)

    @staticmethod
    def _assert_proposal_reviewable(
        proposal: OntologyProposal,
        *,
        action_ja: str,
    ) -> None:
        if proposal.status == OntologyProposalStatus.SUBMITTED:
            return
        raise OntologyStateConflictError(
            "ONTOLOGY_PROPOSAL_ALREADY_REVIEWED",
            f"{action_ja}できるのは未処理の Ontology 改善提案だけです。",
        )

    def _sync_ontology(self) -> SchemaOntology:
        self._ensure_store()
        self._load_published_revision()
        catalog_signature: tuple[int, str] | None = None
        if bool(getattr(self.legacy_service, "uses_incremental_store", False)):
            head = self.legacy_service.get_catalog_head()
            catalog_signature = (head.catalog_version, head.schema_fingerprint)
            if self._ontology is not None and catalog_signature == self._synced_catalog_signature:
                return self._ontology
        catalog = self.legacy_service.get_catalog()
        if self._ontology is None:
            ontology = build_schema_ontology(catalog)
        else:
            ontology = evolve_schema_ontology(catalog, self._ontology)
            if ontology.revision.id == self._ontology.revision.id:
                self._synced_catalog_signature = catalog_signature
                return self._ontology
            # 決定論採番のため「同一 id == 同一物理内容」。過去実行の復元や再評価で同じ
            # revision に到達しても再登録は no-op なので、既登録なら 409 にせず採用する
            # （既存 revision は published/draft の状態と承認済み業務定義を保持する）。
            existing = self._ontologies.get(ontology.revision.id)
            if existing is None:
                existing = self._load_matching_persisted_ontology(ontology)
            if existing is not None:
                self._ontology = existing
                self._synced_catalog_signature = catalog_signature
                return existing
        self.sessions.register_revision(
            ontology.revision,
            nodes=ontology.nodes,
            edges=ontology.edges,
        )
        self._persist_ontology(ontology)
        self._ontology = ontology
        self._cache_ontology(ontology)
        self._synced_catalog_signature = catalog_signature
        return ontology

    def _base_profile_view(
        self,
        profile: Nl2SqlProfile,
        ontology: SchemaOntology,
    ) -> ProfileOntologyView:
        view_key = (profile.id, ontology.revision.id)
        if view_key not in self._profile_view_overrides:
            document = self.store.get_document(
                "profile_views",
                {"profile_id": profile.id, "revision_id": ontology.revision.id},
            )
            if document is not None:
                restored = ProfileOntologyView.model_validate(
                    self._stored_payload(document, collection="profile view")
                )
                self._profile_view_overrides[view_key] = restored
        view = migrate_profile_ontology_view(
            self._profile_view_migration_profile(profile), ontology, strict=False
        )
        override = self._profile_view_overrides.get(view_key)
        if override is not None:
            current_node_by_id = {node.id: node for node in ontology.nodes}
            current_column_keys = {
                value
                for node_id in view.node_ids
                if (node := current_node_by_id.get(node_id)) is not None
                and node.kind.value == "column"
                for value in (node.id, node.technical_name)
            }
            retained_updates: dict[str, Any] = {
                "table_usages_ja": {
                    key: value
                    for key, value in override.table_usages_ja.items()
                    if key in view.node_ids
                },
                "column_policies": {
                    key: value
                    for key, value in override.column_policies.items()
                    if key in current_column_keys
                },
                "allowed_path_ids": [
                    path_id for path_id in override.allowed_path_ids if path_id in view.edge_ids
                ],
                "draft_node_overrides": list(override.draft_node_overrides),
                "draft_edge_overrides": list(override.draft_edge_overrides),
                "draft_schema_fingerprint": override.draft_schema_fingerprint,
                "draft_physical_scope": dict(override.draft_physical_scope),
                "activation_scenarios_ja": list(override.activation_scenarios_ja),
                "activation_keywords": list(override.activation_keywords),
                "scenario_version": override.scenario_version,
                "updated_at": override.updated_at,
            }
            if override.ontology_revision_id == view.ontology_revision_id:
                retained_updates["etag"] = override.etag
            else:
                previous = self._ontologies.get(override.ontology_revision_id)
                if not retained_updates["draft_schema_fingerprint"] and previous is not None:
                    retained_updates["draft_schema_fingerprint"] = (
                        previous.revision.schema_fingerprint
                    )
                retained_updates["updated_at"] = datetime.now(UTC)
                retained_updates["etag"] = compute_etag(
                    {
                        "view_id": view.id,
                        "ontology_revision_id": view.ontology_revision_id,
                        **{
                            key: (
                                value.model_dump(mode="json")
                                if hasattr(value, "model_dump")
                                else value
                            )
                            for key, value in retained_updates.items()
                            if key != "updated_at"
                        },
                    },
                    1,
                )
            view = view.model_copy(
                update=retained_updates,
                deep=True,
            )
            self._profile_view_overrides[view_key] = view
        self.sessions.register_profile_view(view)
        # Read path is pure. Profile mutation / publish paths persist materialized views.
        return view

    def _profile_view_migration_profile(self, profile: Nl2SqlProfile) -> Nl2SqlProfile:
        """対象の表・ビューを持たない業務プロファイルは、カタログの全体を範囲にする。"""

        if profile.allowed_tables or profile.allowed_views:
            return profile
        tables = self.legacy_service.get_catalog().tables
        return profile.model_copy(
            update={
                "allowed_tables": [
                    table.table_name for table in tables if "view" not in table.table_type.lower()
                ],
                "allowed_views": [
                    table.table_name for table in tables if "view" in table.table_type.lower()
                ],
            }
        )

    @staticmethod
    def _narrow_profile_view(
        base: ProfileOntologyView,
        ontology: SchemaOntology,
        allowed: AllowedObjects,
    ) -> ProfileOntologyView:
        if not allowed.table_names:
            return base
        requested_full: set[str] = set()
        requested_short: set[str] = set()
        for raw_name in allowed.table_names:
            # 引用規則の照合キー。大文字化すると `SALES."Mixed_Case"` の要求で大文字の同名表
            # `SALES.MIXED_CASE` も選ぶ（#563）。
            normalized = object_match_key(raw_name)
            try:
                part_count = len(object_name_tokens(raw_name))
            except ValueError:
                part_count = 0
            if part_count >= 2:
                requested_full.add(normalized)
            else:
                requested_short.add(normalized)
        for short_name in requested_short:
            matches = [
                item
                for item in base.physical_objects
                if catalog_match_key(item.object_name) == short_name
            ]
            if len(matches) > 1:
                raise OntologyIntegrityError(
                    "REQUEST_OBJECT_AMBIGUOUS",
                    "owner のない object 名が複数 schema に一致します。owner を指定してください。",
                )
        selected_objects = [
            item
            for item in base.physical_objects
            if (
                catalog_match_key(item.owner, item.object_name) in requested_full
                or catalog_match_key(item.object_name) in requested_short
            )
        ]
        selected_ids = {item.node_id for item in selected_objects}
        nodes = [
            node
            for node in ontology.nodes
            if node.id in base.node_ids
            and (
                node.id in selected_ids
                or any(
                    mapping.object_ref.node_id in selected_ids for mapping in node.physical_mappings
                )
                or (
                    node.kind.value == "schema"
                    and str(node.metadata.get("owner", ""))
                    in {item.owner for item in selected_objects}
                )
            )
        ]
        node_ids = {node.id for node in nodes}
        # 補助概念は物理 mapping を持たない場合もある。参照先がすべて今回の
        # 範囲に収まる型付き定義だけを残し、別 object の情報を間接参照で混ぜない。
        typed = {
            node.id: node
            for node in ontology.nodes
            if node.id in base.node_ids and isinstance(node.metadata.get("definition"), dict)
        }
        safe = {
            identity
            for identity, node in typed.items()
            if all(m.object_ref.node_id in selected_ids for m in node.physical_mappings)
        }
        changed = True
        while changed:
            changed = False
            for edge in ontology.edges:
                if (
                    edge.source_node_id in safe
                    and edge.kind
                    in {
                        OntologyEdgeKind.USES,
                        OntologyEdgeKind.IS_A,
                        OntologyEdgeKind.DOMAIN,
                        OntologyEdgeKind.RANGE,
                        OntologyEdgeKind.GOVERNS,
                    }
                    and edge.target_node_id not in safe | (node_ids - typed.keys())
                ):
                    safe.remove(edge.source_node_id)
                    changed = True
        node_ids = (node_ids - typed.keys()) | safe
        # 列挙メンバーは定義から派生するため、親が範囲内の場合だけ含める。
        for node in ontology.nodes:
            parent = node.metadata.get("derived_from_definition_id")
            if parent:
                node_ids.discard(node.id)
                if node.id in base.node_ids and parent in safe:
                    node_ids.add(node.id)
        edges = [
            edge
            for edge in ontology.edges
            if edge.id in base.edge_ids
            and edge.source_node_id in node_ids
            and edge.target_node_id in node_ids
        ]
        edge_ids = {edge.id for edge in edges}
        if selected_ids == {item.node_id for item in base.physical_objects}:
            return base
        view_id = stable_ontology_id(
            "query_profile_view",
            base.id,
            *sorted(selected_ids),
        )
        payload = {
            "id": view_id,
            "profile_id": base.profile_id,
            "ontology_revision_id": base.ontology_revision_id,
            "node_ids": sorted(node_ids),
            "edge_ids": sorted(edge_ids),
            "physical_objects": selected_objects,
            "table_usages_ja": {
                key: value for key, value in base.table_usages_ja.items() if key in selected_ids
            },
            "column_policies": base.column_policies,
            "allowed_path_ids": [
                edge_id for edge_id in base.allowed_path_ids if edge_id in edge_ids
            ],
            "archived": base.archived,
        }
        return ProfileOntologyView(
            **payload,
            etag=compute_etag(payload, 1),
        )

    def _strict_profile(self, profile_id: str) -> Nl2SqlProfile:
        try:
            return Nl2SqlProfile.model_validate(self.legacy_service.get_profile(profile_id))
        except (KeyError, ValueError) as exc:
            raise OntologyNotFoundError(
                "NL2SQL_PROFILE_NOT_FOUND",
                "指定された業務プロファイルが見つからないか、利用できません。",
            ) from exc

    def _require_context(self, session_id: str) -> QueryRuntimeContext:
        context = self._contexts.get(session_id)
        if context is None:
            raise OntologyNotFoundError(
                "QUERY_SESSION_CONTEXT_NOT_FOUND",
                "query session の実行 context が見つかりません。",
            )
        return context

    def _session_data(self, session: QuerySession) -> QuerySessionData:
        view = self._session_views.get(session.id)
        if view is None:
            view = self.sessions.get_profile_view(session.profile_view_id)
        self._sync_ontology()
        ontology = self._ontologies.get(session.ontology_revision_id)
        if ontology is None:
            # Markdown 公開 snapshot は Profile 固有 artifact であり、global revision
            # cache の公開候補には入れない。新規・復元 session とも固定 ID から読む。
            ontology = self._load_ontology_revision(session.ontology_revision_id)
        if ontology is None:
            raise OntologyNotFoundError(
                "SESSION_ONTOLOGY_REVISION_NOT_FOUND",
                "query session が固定した Ontology revision を読み込めません。",
            )
        artifact = session.sql_artifacts[-1] if session.sql_artifacts else None
        report = artifact.validation_report if artifact is not None else None
        context = self._contexts.get(session.id)
        requirement_state = (
            build_requirement_state(
                session, max_review_rounds=self._requirement_max_review_rounds(session.profile_id)
            )
            if session.clarification_mode == ClarificationMode.GUIDED
            else None
        )
        return QuerySessionData(
            session=session,
            profile_ontology_view=view,
            ontology_graph=OntologyGraphData(
                revision=ontology.revision,
                nodes=[node for node in ontology.nodes if node.id in view.node_ids],
                edges=[edge for edge in ontology.edges if edge.id in view.edge_ids],
            ),
            preview=self._previews.get(session.id),
            result=self._results.get(session.id),
            performance_check=self._plans.get(session.id),
            requirement_state=requirement_state,
            ontology_trace_summary={
                "session_id": session.id,
                "ontology_revision_id": session.ontology_revision_id,
                "business_release_id": session.business_release_id,
                "intent_version": session.current_intent_version,
                "sql_artifact_id": session.current_sql_artifact_id,
                "sql_hash": artifact.sql_hash if artifact else "",
                "validation_hash": report.validation_hash if report else "",
                "generation_context_hash": artifact.generation_context_hash if artifact else "",
                "blocker_count": report.blocker_count if report else 0,
                "warning_count": report.warning_count if report else 0,
                "retrieved_node_ids": context.retrieved_node_ids if context else [],
            },
        )

    def _ensure_store(self) -> None:
        if self._store_ready:
            return
        self.store.ensure_schema()
        self._store_ready = True

    def _load_published_revision(self) -> None:
        """通常 read path では published header 1 系統とその graph だけを復元する。"""

        if self._published_revision_loaded:
            return
        documents = self.store.list_documents(
            "revisions", {"status": OntologyRevisionStatus.PUBLISHED.value}
        )
        headers = [
            OntologyRevision.model_validate(self._stored_payload(document, collection="revision"))
            for document in documents
        ]
        if headers:
            active = max(
                headers,
                key=lambda item: (
                    item.version,
                    item.published_at or item.created_at,
                    item.id,
                ),
            )
            self._revision_headers[active.id] = active
            self._ontology = self._load_ontology_revision(active.id)
        self._published_revision_loaded = True

    def _load_revision_headers(self, *, force: bool = False) -> None:
        """Revision 一覧 API のときだけ全 header を読む。graph は必要な revision のみ。"""

        if self._revision_headers_loaded and not force:
            return
        documents = self.store.list_documents("revisions")
        headers = [
            OntologyRevision.model_validate(self._stored_payload(document, collection="revision"))
            for document in documents
        ]
        if force:
            self._revision_headers.clear()
        self._revision_headers.update({header.id: header for header in headers})
        for header in headers:
            cached = self._ontologies.get(header.id)
            if cached is None or cached.revision.etag == header.etag:
                continue
            updated = cached.model_copy(update={"revision": header}, deep=True)
            self.sessions.register_revision(header, nodes=updated.nodes, edges=updated.edges)
            self._cache_ontology(updated)
            if self._ontology is not None and self._ontology.revision.id == header.id:
                self._ontology = updated
        active_candidates = [
            header for header in headers if header.status == OntologyRevisionStatus.PUBLISHED
        ] or headers
        if active_candidates and self._ontology is None:
            active = max(
                active_candidates,
                key=lambda item: (
                    item.version,
                    item.published_at or item.created_at,
                    item.id,
                ),
            )
            self._ontology = self._load_ontology_revision(active.id)
        self._published_revision_loaded = True
        self._revision_headers_loaded = True

    def _load_ontology_revision(self, revision_id: str) -> SchemaOntology | None:
        snapshot_doc = self.store.get_artifact(revision_id)
        if snapshot_doc and snapshot_doc.get("artifact_type") == "ontology_markdown_snapshot":
            snapshot_graph = SchemaOntology.model_validate(
                json.loads(snapshot_doc["content"])["graph"]
            )
            try:
                self.sessions.get_revision(snapshot_graph.revision.id)
            except OntologyNotFoundError:
                self.sessions.register_revision(
                    snapshot_graph.revision, nodes=snapshot_graph.nodes, edges=snapshot_graph.edges
                )
            return snapshot_graph
        cached = self._ontologies.get(revision_id)
        header = self._revision_headers.get(revision_id)
        if cached is not None:
            if header is not None and cached.revision.etag != header.etag:
                cached = cached.model_copy(update={"revision": header}, deep=True)
                self.sessions.register_revision(header, nodes=cached.nodes, edges=cached.edges)
                self._cache_ontology(cached)
                if self._ontology is not None and self._ontology.revision.id == revision_id:
                    self._ontology = cached
            self._ontology_cache_order.pop(revision_id, None)
            self._ontology_cache_order[revision_id] = None
            return cached
        if header is None:
            document = self.store.get_document("revisions", {"revision_id": revision_id})
            if document is None:
                return None
            header = OntologyRevision.model_validate(
                self._stored_payload(document, collection="revision")
            )
            self._revision_headers[header.id] = header
        node_documents = self.store.list_documents("nodes", {"revision_id": revision_id})
        edge_documents = self.store.list_documents("edges", {"revision_id": revision_id})
        nodes = [
            OntologyNode.model_validate(self._stored_payload(document, collection="node"))
            for document in node_documents
        ]
        edges = [
            OntologyEdge.model_validate(self._stored_payload(document, collection="edge"))
            for document in edge_documents
        ]
        self.sessions.register_revision(header, nodes=nodes, edges=edges)
        ontology = SchemaOntology(revision=header, nodes=nodes, edges=edges)
        vectors = {
            str(document["node_id"]): [float(value) for value in document["embedding"]]
            for document in node_documents
            if document.get("embedding") is not None
        }
        self._cache_ontology(ontology, embeddings=vectors)
        return ontology

    def _load_matching_persisted_ontology(
        self,
        expected: SchemaOntology,
    ) -> SchemaOntology | None:
        """同じ決定論 revision が完全保存済みなら再生成 payload で上書きせず復元する。"""

        revision_id = expected.revision.id
        revision_document = self.store.get_document(
            "revisions",
            {"revision_id": revision_id},
        )
        if revision_document is None:
            return None
        node_documents = self.store.list_documents("nodes", {"revision_id": revision_id})
        edge_documents = self.store.list_documents("edges", {"revision_id": revision_id})
        if {str(document["node_id"]) for document in node_documents} != {
            node.id for node in expected.nodes
        }:
            return None
        if {str(document["edge_id"]) for document in edge_documents} != {
            edge.id for edge in expected.edges
        }:
            return None

        header = OntologyRevision.model_validate(
            self._stored_payload(revision_document, collection="revision")
        )
        nodes = [
            OntologyNode.model_validate(self._stored_payload(document, collection="node"))
            for document in node_documents
        ]
        edges = [
            OntologyEdge.model_validate(self._stored_payload(document, collection="edge"))
            for document in edge_documents
        ]
        ontology = SchemaOntology(revision=header, nodes=nodes, edges=edges)
        self.sessions.register_revision(header, nodes=nodes, edges=edges)
        self._revision_headers[header.id] = header
        vectors = {
            str(document["node_id"]): [float(value) for value in document["embedding"]]
            for document in node_documents
            if document.get("embedding") is not None
        }
        self._cache_ontology(ontology, embeddings=vectors)
        return ontology

    def _cache_ontology(
        self,
        ontology: SchemaOntology,
        *,
        embeddings: dict[str, list[float]] | None = None,
    ) -> None:
        """Revision graph を最大件数・概算 memory の両方で bounded LRU 管理する。"""

        revision_id = ontology.revision.id
        self._ontologies[revision_id] = ontology
        if embeddings is not None:
            if embeddings:
                self._embeddings[revision_id] = embeddings
            else:
                self._embeddings.pop(revision_id, None)
        self._ontology_cache_order.pop(revision_id, None)
        self._ontology_cache_order[revision_id] = None

        def estimated_bytes() -> int:
            graph_bytes = sum(
                len(item.model_dump_json().encode("utf-8")) for item in self._ontologies.values()
            )
            vector_bytes = sum(
                len(vector) * 8
                for revision_vectors in self._embeddings.values()
                for vector in revision_vectors.values()
            )
            return graph_bytes + vector_bytes

        active_id = self._ontology.revision.id if self._ontology is not None else ""
        protected_ids = {
            item.revision.id
            for item in self._ontologies.values()
            if item.revision.status == OntologyRevisionStatus.PUBLISHED
        }
        protected_ids.update({revision_id, active_id})
        while (
            len(self._ontologies) > self._ontology_cache_max_revisions
            or estimated_bytes() > self._ontology_cache_max_bytes
        ):
            evict_id = next(
                (
                    candidate
                    for candidate in self._ontology_cache_order
                    if candidate not in protected_ids
                ),
                None,
            )
            if evict_id is None:
                # Active と今回 request 中の revision 自体は処理中に解放しない。
                break
            self._ontology_cache_order.pop(evict_id, None)
            self._ontologies.pop(evict_id, None)
            self._embeddings.pop(evict_id, None)
            for session_id in self.sessions.evict_revision(evict_id):
                self._session_views.pop(session_id, None)
                self._contexts.pop(session_id, None)
                self._previews.pop(session_id, None)
                self._results.pop(session_id, None)
                self._plans.pop(session_id, None)

    @staticmethod
    def _stored_payload(
        document: Mapping[str, Any],
        *,
        collection: str,
    ) -> Mapping[str, Any]:
        payload = document.get("payload")
        if not isinstance(payload, Mapping):
            raise OntologyIntegrityError(
                "ONTOLOGY_STORE_PAYLOAD_INVALID",
                f"永続化された {collection} payload が JSON オブジェクトではありません。",
            )
        return payload

    def _store_etag_key(
        self,
        collection: OntologyCollection,
        identity: Mapping[str, Any],
    ) -> tuple[OntologyCollection, tuple[str, ...]]:
        return (
            collection,
            tuple(str(identity[field]) for field in _STORE_IDENTITY_FIELDS[collection]),
        )

    def _remember_store_etag(
        self,
        collection: OntologyCollection,
        document: Mapping[str, Any],
    ) -> None:
        if collection not in _STORE_OPTIMISTIC_COLLECTIONS:
            return
        etag = str(document.get("etag") or "")
        if not etag:
            return
        identity = {field: document[field] for field in _STORE_IDENTITY_FIELDS[collection]}
        self._store_etags[self._store_etag_key(collection, identity)] = etag

    def _expected_store_etag(
        self,
        collection: OntologyCollection,
        identity: Mapping[str, Any],
    ) -> str | None:
        if collection in _STORE_OPTIMISTIC_COLLECTIONS:
            cached = self._store_etags.get(self._store_etag_key(collection, identity))
            if cached is not None:
                return cached
        current = self.store.get_document(collection, identity)
        if current is None:
            return None
        self._remember_store_etag(collection, current)
        return str(current["etag"])

    def _persist_ontology(self, ontology: SchemaOntology, *, include_graph: bool = True) -> None:
        """revision と(必要なら)nodes/edges を永続化する。

        nodes/edges は revision 登録時に同一 revision_id で保存済みかつ不変のため、
        publish/archive のような revision header だけの変更では include_graph=False で
        再永続化を省く。初回 graph 保存は collection ごとの単一 transaction にまとめ、
        大規模スキーマの read path で node/edge 件数分の接続を作らない。

        revision header は nodes/edges の後に保存する。読み手は header 経由でしか
        revision を発見しないため、途中クラッシュしても「node だけの revision」を
        観測させない(残った graph 行は再実行時に同一 identity で上書きされる)。
        """

        def save_revision_header() -> None:
            self._save(
                "revisions",
                {
                    "revision_id": ontology.revision.id,
                    "status": ontology.revision.status.value,
                    "schema_fingerprint": ontology.revision.schema_fingerprint,
                    "payload": ontology.revision,
                },
            )
            self._revision_headers[ontology.revision.id] = ontology.revision.model_copy(deep=True)

        if not include_graph:
            save_revision_header()
            return
        self._save_graph_documents_atomic(
            "nodes",
            ontology.revision.id,
            [
                {
                    "revision_id": ontology.revision.id,
                    "node_id": node.id,
                    "node_type": node.kind.value,
                    "review_status": node.review_status.value,
                    "physical_id": (
                        node.physical_mappings[0].object_ref.node_id
                        if node.physical_mappings
                        else ""
                    ),
                    "embedding": self._embeddings.get(ontology.revision.id, {}).get(node.id),
                    "payload": node,
                }
                for node in ontology.nodes
            ],
        )
        self._save_graph_documents_atomic(
            "edges",
            ontology.revision.id,
            [
                {
                    "revision_id": ontology.revision.id,
                    "edge_id": edge.id,
                    "source_node_id": edge.source_node_id,
                    "target_node_id": edge.target_node_id,
                    "review_status": edge.review_status.value,
                    "payload": edge,
                }
                for edge in ontology.edges
            ],
        )
        save_revision_header()

    def _save_graph_documents_atomic(
        self,
        collection: Literal["nodes", "edges"],
        revision_id: str,
        documents: list[dict[str, Any]],
    ) -> None:
        """Immutable graph documents を既存 ETag 付きの一括 transaction で保存する。"""

        if not documents:
            return
        self._ensure_store()
        identity_fields = _STORE_IDENTITY_FIELDS[collection]
        existing = {
            tuple(str(item[field]) for field in identity_fields): item
            for item in self.store.list_documents(
                collection,
                {"revision_id": revision_id},
            )
        }
        pending: list[tuple[dict[str, Any], str | None]] = []
        for document in documents:
            identity = tuple(str(document[field]) for field in identity_fields)
            current = existing.get(identity)
            if current is not None:
                current_content = {
                    key: value for key, value in current.items() if key not in {"version", "etag"}
                }
                if collection == "nodes" and "embedding" not in current_content:
                    current_content["embedding"] = None
                if canonical_json(current_content) == canonical_json(document):
                    continue
            pending.append(
                (
                    document,
                    str(current["etag"]) if current is not None else None,
                )
            )
        if pending:
            self.store.save_documents_atomic(collection, pending)

    def _persist_revision_headers_atomic(self, ontologies: list[SchemaOntology]) -> None:
        self._ensure_store()
        documents: list[tuple[dict[str, Any], str | None]] = []
        for ontology in ontologies:
            document = {
                "revision_id": ontology.revision.id,
                "status": ontology.revision.status.value,
                "schema_fingerprint": ontology.revision.schema_fingerprint,
                "payload": ontology.revision,
            }
            current = self.store.get_document("revisions", {"revision_id": ontology.revision.id})
            documents.append((document, str(current["etag"]) if current is not None else None))
        self.store.save_documents_atomic("revisions", documents)
        for ontology in ontologies:
            self._revision_headers[ontology.revision.id] = ontology.revision.model_copy(deep=True)

    def _persist_node(self, ontology: SchemaOntology, node: OntologyNode) -> None:
        physical_id = node.physical_mappings[0].object_ref.node_id if node.physical_mappings else ""
        self._save(
            "nodes",
            {
                "revision_id": ontology.revision.id,
                "node_id": node.id,
                "node_type": node.kind.value,
                "review_status": node.review_status.value,
                "physical_id": physical_id,
                "embedding": self._embeddings.get(ontology.revision.id, {}).get(node.id),
                "payload": node,
            },
        )

    def _persist_profile_view(self, view: ProfileOntologyView) -> None:
        self._save(
            "profile_views",
            {
                "profile_id": view.profile_id,
                "revision_id": view.ontology_revision_id,
                "payload": view,
            },
        )

    def _persist_session(
        self,
        session: QuerySession,
        *,
        context: QueryRuntimeContext | None = None,
        result: QueryResults | None = None,
    ) -> None:
        resolved_context = context or self._contexts.get(session.id)
        resolved_result = result or self._results.get(session.id)
        resolved_view = self._session_views.get(session.id)
        if resolved_view is None:
            try:
                resolved_view = self.sessions.get_profile_view(session.profile_view_id)
            except OntologyNotFoundError:
                resolved_view = None
        self._save(
            "query_sessions",
            {
                "session_id": session.id,
                "ontology_revision_id": session.ontology_revision_id,
                "profile_id": session.profile_id,
                "status": session.status.value,
                "intent_version": session.current_intent_version,
                "sql_version": len(session.sql_artifacts),
                "payload": session,
                "runtime_context": resolved_context,
                "profile_view_snapshot": resolved_view,
                "preview": self._previews.get(session.id),
                "result": resolved_result,
                "performance_check": self._plans.get(session.id),
            },
        )

    def _persist_artifact(self, session_id: str, artifact: Any) -> None:
        self._save(
            "artifacts",
            {
                "artifact_id": artifact.id,
                "session_id": session_id,
                "artifact_type": "sql_semantic_graph",
                "content_hash": artifact.sql_hash,
                "payload": artifact,
            },
        )

    def _persist_proposal(self, proposal: OntologyProposal) -> None:
        self._save("proposals", self._proposal_document(proposal))

    def _persist_proposals_atomic(self, proposals: list[OntologyProposal]) -> None:
        if not proposals:
            return
        documents = [self._proposal_document(proposal) for proposal in proposals]
        saved = self.store.save_documents_atomic(
            "proposals",
            [
                (
                    document,
                    self._expected_store_etag(
                        "proposals",
                        {field: document[field] for field in _STORE_IDENTITY_FIELDS["proposals"]},
                    ),
                )
                for document in documents
            ],
        )
        for document in saved:
            self._remember_store_etag("proposals", document)

    @staticmethod
    def _proposal_document(proposal: OntologyProposal) -> dict[str, Any]:
        return {
            "proposal_id": proposal.id,
            "session_id": proposal.session_id,
            "ontology_revision_id": proposal.base_revision_id,
            "profile_id": proposal.profile_id,
            "status": proposal.status.value,
            "payload": proposal,
        }

    def _save(
        self,
        collection: OntologyCollection,
        document: dict[str, Any],
        *,
        expected_etag: str | None | object = _EXPECTED_ETAG_UNSET,
    ) -> dict[str, Any]:
        self._ensure_store()
        identity = {field: document[field] for field in _STORE_IDENTITY_FIELDS[collection]}
        resolved_expected_etag: str | None
        if expected_etag is _EXPECTED_ETAG_UNSET:
            resolved_expected_etag = self._expected_store_etag(collection, identity)
        elif isinstance(expected_etag, str):
            resolved_expected_etag = expected_etag
        else:
            resolved_expected_etag = None
        saved = self.store.save_document(
            collection,
            document,
            expected_etag=resolved_expected_etag,
        )
        self._remember_store_etag(collection, saved)
        return saved


ontology_runtime = OntologyApiRuntime()
ontology_source_storage = OntologySourceStorage()
ontology_build_service = OntologyBuildService(
    ontology_runtime,
    source_storage=ontology_source_storage,
)
ontology_publish_service = OntologyPublishService(ontology_runtime)


def _require_persistence() -> None:
    """nl2sql/router.py と同じ persistence ゲート(未準備時は 503 + Retry-After)。"""

    nl2sql_service.ensure_persistence_available()


router = APIRouter(
    prefix="/nl2sql",
    tags=["nl2sql-ontology"],
    dependencies=[Depends(_require_persistence)],
)


def _run_runtime_sync[T](
    function: Callable[..., T],
    *args: Any,
    **kwargs: Any,
) -> T:
    """同期 route 内で runtime 呼び出し境界を読みやすく保つ。"""

    return function(*args, **kwargs)


def _principal_from_request(request: Request) -> Principal | None:
    principal = getattr(request.state, "principal", None)
    return principal if isinstance(principal, Principal) else None


def _allowed_profile_ids_for_request(request: Request) -> set[str] | None:
    principal = _principal_from_request(request)
    if principal is None or principal.has_permission(PROFILE_MANAGE_PERMISSION):
        return None
    return set(principal.allowed_profile_ids)


def _query_session_actor(request: Request) -> tuple[str, bool]:
    principal = _principal_from_request(request)
    if principal is None:
        return "", True
    return principal.user_uuid, principal.is_system_admin


def _assert_sql_execute_permission(request: Request) -> None:
    principal = _principal_from_request(request)
    if principal is None or principal.has_permission(SQL_EXECUTE_PERMISSION):
        return
    raise HTTPException(status_code=403, detail="SQL を実行する権限がありません。")


def _ensure_query_session_access(
    data: QuerySessionData, request: Request, *, adding: bool = False
) -> QuerySessionData:
    """SQL の生成前の確認の session の判定（「AI 活用」の規則。#1889・#3244）。

    system_admin は全員の session、それ以外は本人の session だけ（他人の session は 404 で、
    存在を漏らさない）。持ち主の無い session（認証無効の期間に作ったもの）も本人の物とみなさない
    （#1126）。`adding`（確認への回答・反映・SQL の生成と実行など、session に内容を足す操作）は
    持ち主だけで、見える他の利用者の session（system_admin）は 403。見る・取り消すは `adding` なし。
    """
    principal = _principal_from_request(request)
    if principal is None:
        return data
    owner = data.session.actor_user_uuid.strip()
    if not can_access_user_data(principal, owner):
        raise HTTPException(
            status_code=OWNER_NOT_FOUND_STATUS,
            detail="指定された query session が見つかりません。",
        )
    if adding and not can_add_user_content(principal, owner):
        raise HTTPException(
            status_code=OTHERS_CONTENT_FORBIDDEN_STATUS, detail=OTHERS_CONTENT_FORBIDDEN_MESSAGE
        )
    if not can_view_all_users_data(principal):
        assert_profile_access(request, data.session.profile_id)
    return data


def _load_authorized_query_session(
    session_id: str, request: Request, *, adding: bool = False
) -> QuerySessionData:
    return _ensure_query_session_access(
        _run_runtime_sync(ontology_runtime.get_session, session_id),
        request,
        adding=adding,
    )


def _raise_domain_error(exc: Exception) -> NoReturn:
    if isinstance(exc, HTTPException):
        raise exc
    if isinstance(exc, OthersContentForbiddenError):
        # 他の利用者の会話の続きで確認を始めた system_admin（#3244）。
        raise HTTPException(
            status_code=OTHERS_CONTENT_FORBIDDEN_STATUS, detail=OTHERS_CONTENT_FORBIDDEN_MESSAGE
        ) from exc
    if isinstance(exc, OntologyNotFoundError):
        status_code = 404
    elif isinstance(
        exc,
        (
            OntologyVersionConflictError,
            OntologyStateConflictError,
            OntologyIntegrityError,
            OntologyVersionConflict,
        ),
    ):
        status_code = 409
    elif isinstance(exc, OntologyGateBlockedError):
        status_code = 422
    elif isinstance(exc, RequirementAiUnavailableError):
        status_code = 503
    else:
        status_code = 500
    # アプリ共通の exception handler が detail を error_messages[0] へ文字列化するため、
    # dict ではなく読みやすい日本語メッセージ 1 本にする(code は括弧で併記)。
    code = getattr(exc, "code", type(exc).__name__)
    message_ja = str(getattr(exc, "message_ja", "") or str(exc) or "処理に失敗しました。")
    if isinstance(exc, RequirementAiUnavailableError) and exc.details:
        # 回答前の確認の LLM の失敗は、理由（1 行）と「詳細」（種類・HTTP ステータス・モデル ID・
        # 所要時間・request id）を分けて返す（#1715。code は problem の code と「詳細」に出す）。
        raise HTTPException(
            status_code=status_code,
            detail={"code": code, "message_ja": message_ja, "error_details": exc.details},
        ) from exc
    detail = f"{message_ja}({code})"
    finding_codes = getattr(exc, "finding_codes", None)
    if finding_codes:
        detail = f"{detail} 対象: {', '.join(str(item) for item in finding_codes)}"
    raise HTTPException(status_code=status_code, detail=detail) from exc


@router.get(
    "/ontology/revisions",
    response_model=ApiResponse[OntologyRevisionListData],
)
def list_ontology_revisions() -> ApiResponse[OntologyRevisionListData]:
    try:
        revisions, active_revision_id = _run_runtime_sync(ontology_runtime.list_ontology_revisions)
        return ApiResponse(
            data=OntologyRevisionListData(
                revisions=revisions,
                active_revision_id=active_revision_id,
            )
        )
    except Exception as exc:
        _raise_domain_error(exc)


@router.get(
    "/ontology/revisions/current",
    response_model=ApiResponse[OntologyGraphData],
)
def get_current_ontology_revision() -> ApiResponse[OntologyGraphData]:
    try:
        ontology = _run_runtime_sync(ontology_runtime.current_ontology)
        return ApiResponse(
            data=OntologyGraphData(
                revision=ontology.revision,
                nodes=ontology.nodes,
                edges=ontology.edges,
            )
        )
    except Exception as exc:
        _raise_domain_error(exc)


@router.get(
    "/ontology/revisions/{revision_id}",
    response_model=ApiResponse[OntologyGraphData],
)
def get_ontology_revision(revision_id: str) -> ApiResponse[OntologyGraphData]:
    try:
        ontology = _run_runtime_sync(ontology_runtime.ontology_revision, revision_id)
        return ApiResponse(
            data=OntologyGraphData(
                revision=ontology.revision,
                nodes=ontology.nodes,
                edges=ontology.edges,
            )
        )
    except Exception as exc:
        _raise_domain_error(exc)


@router.post(
    "/ontology/revisions/{revision_id}/drafts",
    response_model=ApiResponse[OntologyGraphData],
)
def create_ontology_revision_draft(
    revision_id: str,
    request: OntologyDraftRequest,
) -> ApiResponse[OntologyGraphData]:
    try:
        ontology = _run_runtime_sync(
            ontology_runtime.create_ontology_draft,
            revision_id,
            request,
        )
        return ApiResponse(
            data=OntologyGraphData(
                revision=ontology.revision,
                nodes=ontology.nodes,
                edges=ontology.edges,
            )
        )
    except Exception as exc:
        _raise_domain_error(exc)


@router.post(
    "/ontology/revisions/{revision_id}/publish",
    response_model=ApiResponse[OntologyPublishJobData],
    status_code=202,
)
def publish_ontology_revision(
    revision_id: str,
    request: OntologyPublishRequest,
    http_request: Request,
    if_match: str = Header(..., alias="If-Match"),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> ApiResponse[OntologyPublishJobData]:
    try:
        if request.profile_id.strip():
            assert_profile_access(http_request, request.profile_id)
        normalized_match = if_match.strip().removeprefix("W/").strip('"')
        if normalized_match != request.etag:
            raise OntologyVersionConflictError(
                "REVISION_ETAG_MISMATCH",
                "If-Match と request etag が一致しません。",
            )
        if request.profile_id.strip():
            raise OntologyGateBlockedError(
                "MARKDOWN_PREPARATION_REQUIRED", "Markdown の公開前確認を実行してください。"
            )
        job = _run_runtime_sync(
            ontology_publish_service.start,
            revision_id,
            etag=request.etag,
            idempotency_key=idempotency_key,
            profile_id=request.profile_id,
        )
        return ApiResponse(data=OntologyPublishJobData(job=job))
    except Exception as exc:
        _raise_domain_error(exc)


@router.get(
    "/ontology-publish/{job_id}",
    response_model=ApiResponse[OntologyPublishJobData],
)
def get_ontology_publish_job(
    job_id: str,
    http_request: Request,
) -> ApiResponse[OntologyPublishJobData]:
    snapshot_doc = ontology_runtime.store.get_artifact(job_id)
    if snapshot_doc and snapshot_doc.get("artifact_type") == "ontology_markdown_snapshot":
        assert_profile_access(http_request, str(snapshot_doc["profile_id"]))
        return ApiResponse(
            data=OntologyPublishJobData(
                job=OntologyPublishJob.model_validate(
                    json.loads(snapshot_doc["content"])["publish_job"]
                )
            )
        )
    job = _run_runtime_sync(ontology_publish_service.peek, job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "ONTOLOGY_PUBLISH_JOB_NOT_FOUND",
                "message_ja": "Ontology 公開 job が見つかりません。",
            },
        )
    if job.profile_id:
        assert_profile_access(http_request, job.profile_id)
    recovered = _run_runtime_sync(ontology_publish_service.get, job_id)
    return ApiResponse(data=OntologyPublishJobData(job=recovered or job))


@router.get(
    "/profiles/{profile_id}/ontology-markdown",
    response_model=ApiResponse[OntologyMarkdownState],
)
def get_profile_ontology_markdown(
    profile_id: str,
    http_request: Request,
) -> ApiResponse[OntologyMarkdownState]:
    assert_profile_access(http_request, profile_id)
    try:
        state = _run_runtime_sync(ontology_runtime.ontology_markdown_state, profile_id)
        return ApiResponse(data=state)
    except Exception as exc:
        _raise_domain_error(exc)


@router.patch(
    "/profiles/{profile_id}/ontology-markdown/draft",
    response_model=ApiResponse[OntologyMarkdownState],
)
def save_profile_ontology_markdown_draft(
    profile_id: str,
    request: OntologyMarkdownDraftPatch,
    http_request: Request,
) -> ApiResponse[OntologyMarkdownState]:
    assert_profile_access(http_request, profile_id)
    try:
        state = _run_runtime_sync(
            ontology_runtime.save_ontology_markdown_draft,
            profile_id,
            request,
        )
        return ApiResponse(data=state)
    except Exception as exc:
        _raise_domain_error(exc)


@router.get(
    "/profiles/{profile_id}/ontology-view",
    response_model=ApiResponse[ProfileOntologyViewData],
)
def get_profile_ontology_view(
    profile_id: str,
    http_request: Request,
) -> ApiResponse[ProfileOntologyViewData]:
    assert_profile_access(http_request, profile_id)
    try:
        view, ontology = _run_runtime_sync(ontology_runtime.profile_view, profile_id)
        warnings = _run_runtime_sync(
            ontology_runtime.profile_view_warnings,
            profile_id,
            view,
        )
        materialized, stale = _run_runtime_sync(
            ontology_runtime.profile_view_persistence_state,
            view,
        )
        return ApiResponse(
            data=ProfileOntologyViewData(
                profile_ontology_view=view,
                ontology_graph=OntologyGraphData(
                    revision=ontology.revision,
                    nodes=[node for node in ontology.nodes if node.id in view.node_ids],
                    edges=[edge for edge in ontology.edges if edge.id in view.edge_ids],
                ),
                materialized=materialized,
                stale=stale,
                warnings_ja=warnings,
            )
        )
    except Exception as exc:
        _raise_domain_error(exc)


@router.post(
    "/profiles/{profile_id}/ontology-view/materialize",
    response_model=ApiResponse[ProfileOntologyViewData],
)
def materialize_profile_ontology_view(
    profile_id: str,
    http_request: Request,
) -> ApiResponse[ProfileOntologyViewData]:
    """互換 API として現在の Profile scope を明示的に永続化する。"""

    assert_profile_access(http_request, profile_id)
    try:
        view = _run_runtime_sync(ontology_runtime.materialize_profile_view, profile_id)
        _current_view, ontology = _run_runtime_sync(ontology_runtime.profile_view, profile_id)
        warnings = _run_runtime_sync(
            ontology_runtime.profile_view_warnings,
            profile_id,
            view,
        )
        return ApiResponse(
            data=ProfileOntologyViewData(
                profile_ontology_view=view,
                ontology_graph=OntologyGraphData(
                    revision=ontology.revision,
                    nodes=[node for node in ontology.nodes if node.id in view.node_ids],
                    edges=[edge for edge in ontology.edges if edge.id in view.edge_ids],
                ),
                materialized=True,
                stale=False,
                warnings_ja=warnings,
            )
        )
    except Exception as exc:
        _raise_domain_error(exc)


class ProfileOntologyMermaidData(OntologyContract):
    profile_id: str
    ontology_revision_id: str
    mermaid: str


@router.get(
    "/profiles/{profile_id}/ontology-view/mermaid",
    response_model=ApiResponse[ProfileOntologyMermaidData],
)
def get_profile_ontology_mermaid(
    profile_id: str,
    http_request: Request,
) -> ApiResponse[ProfileOntologyMermaidData]:
    """Profile スコープの erDiagram(SQL 生成プロンプトへ注入するものと同じ表現)。"""

    assert_profile_access(http_request, profile_id)
    try:
        view, ontology = _run_runtime_sync(ontology_runtime.profile_view, profile_id)
        return ApiResponse(
            data=ProfileOntologyMermaidData(
                profile_id=profile_id,
                ontology_revision_id=ontology.revision.id,
                mermaid=render_mermaid_er(ontology, view),
            )
        )
    except Exception as exc:
        _raise_domain_error(exc)


@router.post(
    "/ontology/profile-recommendations",
    response_model=ApiResponse[OntologyProfileRecommendationData],
)
def recommend_ontology_profile(
    request: OntologyProfileRecommendationRequest,
    http_request: Request,
) -> ApiResponse[OntologyProfileRecommendationData]:
    try:
        return ApiResponse(
            data=OntologyProfileRecommendationData(
                recommendation=_run_runtime_sync(
                    ontology_runtime.recommend_profiles,
                    request,
                    allowed_profile_ids=_allowed_profile_ids_for_request(http_request),
                )
            )
        )
    except Exception as exc:
        _raise_domain_error(exc)


@router.post(
    "/ontology/profile-recommendations/{recommendation_id}/confirm",
    response_model=ApiResponse[ProfileRecommendationConfirmationData],
)
def confirm_ontology_profile_recommendation(
    recommendation_id: str,
    request: ProfileRecommendationConfirmationRequest,
    http_request: Request,
) -> ApiResponse[ProfileRecommendationConfirmationData]:
    assert_profile_access(http_request, request.selected_profile_id)
    try:
        recommendation, token = _run_runtime_sync(
            ontology_runtime.confirm_profile_recommendation,
            recommendation_id,
            request,
        )
        return ApiResponse(
            data=ProfileRecommendationConfirmationData(
                recommendation=recommendation,
                confirmation_token=token,
            )
        )
    except Exception as exc:
        _raise_domain_error(exc)


@router.post(
    "/profiles/{profile_id}/ontology-context/search",
    response_model=ApiResponse[OntologyContextSearchResult],
)
def search_profile_ontology_context(
    profile_id: str,
    request: OntologyContextSearchRequest,
    http_request: Request,
) -> ApiResponse[OntologyContextSearchResult]:
    assert_profile_access(http_request, profile_id)
    try:
        return ApiResponse(
            data=_run_runtime_sync(
                ontology_runtime.search_ontology_context,
                profile_id,
                request,
            )
        )
    except Exception as exc:
        _raise_domain_error(exc)


@router.patch(
    "/profiles/{profile_id}/ontology-view",
    response_model=ApiResponse[ProfileOntologyViewData],
)
def patch_profile_ontology_view(
    profile_id: str,
    request: ProfileOntologyViewPatch,
    http_request: Request,
) -> ApiResponse[ProfileOntologyViewData]:
    assert_profile_access(http_request, profile_id)
    try:
        view, ontology = _run_runtime_sync(
            ontology_runtime.patch_profile_view,
            profile_id,
            request,
        )
        warnings = _run_runtime_sync(
            ontology_runtime.profile_view_warnings,
            profile_id,
            view,
        )
        return ApiResponse(
            data=ProfileOntologyViewData(
                profile_ontology_view=view,
                ontology_graph=OntologyGraphData(
                    revision=ontology.revision,
                    nodes=[node for node in ontology.nodes if node.id in view.node_ids],
                    edges=[edge for edge in ontology.edges if edge.id in view.edge_ids],
                ),
                materialized=True,
                stale=False,
                warnings_ja=warnings,
            )
        )
    except Exception as exc:
        _raise_domain_error(exc)


@router.post(
    "/query-sessions",
    response_model=ApiResponse[QuerySessionData],
)
def create_query_session(
    request: QuerySessionApiCreate,
    http_request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> ApiResponse[QuerySessionData]:
    assert_profile_access(http_request, request.profile_id)
    try:
        actor_user_uuid, actor_is_system_admin = _query_session_actor(http_request)
        return ApiResponse(
            data=_run_runtime_sync(
                ontology_runtime.create_session_idempotent,
                request,
                idempotency_key=idempotency_key,
                actor_user_uuid=actor_user_uuid,
                actor_is_system_admin=actor_is_system_admin,
            )
        )
    except Exception as exc:
        _raise_domain_error(exc)


@router.get(
    "/query-sessions/{session_id}",
    response_model=ApiResponse[QuerySessionData],
)
def get_query_session(session_id: str, http_request: Request) -> ApiResponse[QuerySessionData]:
    try:
        return ApiResponse(data=_load_authorized_query_session(session_id, http_request))
    except Exception as exc:
        _raise_domain_error(exc)


@router.post(
    "/query-sessions/{session_id}/clarification-answers",
    response_model=ApiResponse[QuerySessionData],
)
def answer_query_clarification(
    session_id: str,
    request: ClarificationAnswerRequest,
    http_request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> ApiResponse[QuerySessionData]:
    try:
        _load_authorized_query_session(session_id, http_request, adding=True)
        return ApiResponse(
            data=_run_runtime_sync(
                ontology_runtime.answer_clarification_idempotent,
                session_id,
                request,
                idempotency_key=idempotency_key,
            )
        )
    except OntologyVersionConflictError as exc:
        try:
            current = (_run_runtime_sync(ontology_runtime.get_session, session_id)).session
        except Exception:
            _raise_domain_error(exc)
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.code,
                "message_ja": exc.message_ja,
                "current_version": current.current_intent_version,
                "session": current.model_dump(mode="json"),
            },
        ) from exc
    except Exception as exc:
        _raise_domain_error(exc)


@router.post(
    "/query-sessions/{session_id}/apply",
    response_model=ApiResponse[QuerySessionData],
)
def apply_query_requirement(
    session_id: str,
    request: RequirementApplyRequest,
    http_request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> ApiResponse[QuerySessionData]:
    """回答前の確認の結果を反映し、session を閉じる（#1439）。"""

    try:
        _load_authorized_query_session(session_id, http_request, adding=True)
        return ApiResponse(
            data=_run_runtime_sync(
                ontology_runtime.apply_requirement_idempotent,
                session_id,
                request,
                idempotency_key=idempotency_key,
            )
        )
    except Exception as exc:
        _raise_domain_error(exc)


@router.post(
    "/query-sessions/{session_id}/cancel",
    response_model=ApiResponse[QuerySessionData],
)
def cancel_query_session(
    session_id: str,
    http_request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> ApiResponse[QuerySessionData]:
    try:
        _load_authorized_query_session(session_id, http_request)
        return ApiResponse(
            data=_run_runtime_sync(
                ontology_runtime.cancel_session_idempotent,
                session_id,
                idempotency_key=idempotency_key,
            )
        )
    except Exception as exc:
        _raise_domain_error(exc)


@router.patch(
    "/query-sessions/{session_id}/intent",
    response_model=ApiResponse[QuerySessionData],
)
def patch_query_intent(
    session_id: str,
    patch: GraphPatch,
    http_request: Request,
) -> ApiResponse[QuerySessionData]:
    try:
        _load_authorized_query_session(session_id, http_request, adding=True)
        return ApiResponse(data=_run_runtime_sync(ontology_runtime.patch_intent, session_id, patch))
    except OntologyVersionConflictError as exc:
        try:
            current = (_run_runtime_sync(ontology_runtime.get_session, session_id)).session
        except Exception:
            _raise_domain_error(exc)
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.code,
                "message_ja": exc.message_ja,
                "current_version": current.current_intent_version,
                "session": current.model_dump(mode="json"),
            },
        ) from exc
    except Exception as exc:
        _raise_domain_error(exc)


@router.post(
    "/query-sessions/{session_id}/generate-sql",
    response_model=ApiResponse[QuerySessionData],
)
def generate_query_sql(
    session_id: str,
    request: GenerateSqlRequest,
    http_request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> ApiResponse[QuerySessionData]:
    try:
        _load_authorized_query_session(session_id, http_request, adding=True)
        return ApiResponse(
            data=_run_runtime_sync(
                ontology_runtime.generate_sql_idempotent,
                session_id,
                request,
                idempotency_key=idempotency_key,
            )
        )
    except Exception as exc:
        _raise_domain_error(exc)


@router.post(
    "/query-sessions/{session_id}/confirm-sql",
    response_model=ApiResponse[QuerySessionData],
)
def confirm_query_sql(
    session_id: str,
    request: SqlBindingRequest,
    http_request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> ApiResponse[QuerySessionData]:
    try:
        _load_authorized_query_session(session_id, http_request, adding=True)
        if request.session_id != session_id:
            raise OntologyIntegrityError(
                "SESSION_BINDING_MISMATCH",
                "確認 binding の session ID が URL と一致しません。",
            )
        return ApiResponse(
            data=_run_runtime_sync(
                ontology_runtime.confirm_sql_idempotent,
                session_id,
                request.binding(),
                idempotency_key=idempotency_key,
            )
        )
    except Exception as exc:
        _raise_domain_error(exc)


@router.post(
    "/query-sessions/{session_id}/execute",
    response_model=ApiResponse[QueryExecutionData],
)
def execute_query_session(
    session_id: str,
    payload: SqlBindingRequest,
    http_request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> ApiResponse[QueryExecutionData]:
    try:
        _assert_sql_execute_permission(http_request)
        _load_authorized_query_session(session_id, http_request, adding=True)
        if payload.session_id != session_id:
            raise OntologyIntegrityError(
                "SESSION_BINDING_MISMATCH",
                "実行 binding の session ID が URL と一致しません。",
            )
        principal = getattr(http_request.state, "principal", None)
        return ApiResponse(
            data=_run_runtime_sync(
                ontology_runtime.execute_idempotent,
                session_id,
                payload.binding(),
                idempotency_key=idempotency_key,
                actor_user_uuid=str(getattr(principal, "user_uuid", "")),
                actor_is_system_admin=bool(getattr(principal, "is_system_admin", False)),
            )
        )
    except Exception as exc:
        _raise_domain_error(exc)


@router.post(
    "/query-sessions/{session_id}/improvement-proposal",
    response_model=ApiResponse[OntologyProposal],
)
def create_ontology_improvement_proposal(
    session_id: str,
    request: ImprovementProposalRequest,
    http_request: Request,
) -> ApiResponse[OntologyProposal]:
    try:
        _load_authorized_query_session(session_id, http_request, adding=True)
        proposal, _session = _run_runtime_sync(
            ontology_runtime.create_proposal,
            session_id,
            request,
        )
        return ApiResponse(data=proposal)
    except Exception as exc:
        _raise_domain_error(exc)


@router.get(
    "/ontology/proposals/{proposal_id}",
    response_model=ApiResponse[OntologyProposal],
)
def get_ontology_proposal(proposal_id: str, http_request: Request) -> ApiResponse[OntologyProposal]:
    try:
        proposal = _run_runtime_sync(ontology_runtime.get_proposal, proposal_id)
        assert_profile_access(http_request, proposal.profile_id)
        return ApiResponse(data=proposal)
    except Exception as exc:
        _raise_domain_error(exc)


@router.post(
    "/ontology/proposals/{proposal_id}/accept",
    response_model=ApiResponse[OntologyProposalReviewData],
)
def accept_ontology_proposal(
    proposal_id: str,
    http_request: Request,
) -> ApiResponse[OntologyProposalReviewData]:
    try:
        proposal = _run_runtime_sync(ontology_runtime.get_proposal, proposal_id)
        assert_profile_access(http_request, proposal.profile_id)
        return ApiResponse(data=_run_runtime_sync(ontology_runtime.accept_proposal, proposal_id))
    except Exception as exc:
        _raise_domain_error(exc)


@router.post(
    "/ontology/proposals/{proposal_id}/reject",
    response_model=ApiResponse[OntologyProposalReviewData],
)
def reject_ontology_proposal(
    proposal_id: str,
    http_request: Request,
) -> ApiResponse[OntologyProposalReviewData]:
    try:
        proposal = _run_runtime_sync(ontology_runtime.get_proposal, proposal_id)
        assert_profile_access(http_request, proposal.profile_id)
        return ApiResponse(data=_run_runtime_sync(ontology_runtime.reject_proposal, proposal_id))
    except Exception as exc:
        _raise_domain_error(exc)


class OntologyProposalBatchAcceptRequest(OntologyContract):
    proposal_ids: list[str] = Field(min_length=1)


class OntologyProposalBatchReviewData(OntologyContract):
    proposals: list[OntologyProposal]
    draft: OntologyGraphData


@router.post(
    "/ontology/proposals/batch-accept",
    response_model=ApiResponse[OntologyProposalBatchReviewData],
)
def batch_accept_ontology_proposals(
    request: OntologyProposalBatchAcceptRequest,
    http_request: Request,
) -> ApiResponse[OntologyProposalBatchReviewData]:
    """複数提案を 1 つの draft revision へまとめて承認する(一括承認)。"""

    try:
        for proposal_id in request.proposal_ids:
            target = _run_runtime_sync(ontology_runtime.get_proposal, proposal_id)
            assert_profile_access(http_request, target.profile_id)
        proposals, draft = _run_runtime_sync(
            ontology_runtime.accept_proposals,
            request.proposal_ids,
        )
        return ApiResponse(
            data=OntologyProposalBatchReviewData(
                proposals=proposals,
                draft=OntologyGraphData(
                    revision=draft.revision,
                    nodes=draft.nodes,
                    edges=draft.edges,
                ),
            )
        )
    except Exception as exc:
        _raise_domain_error(exc)


# --- AI オントロジー構築 -----------------------------------------------------------------------


class OntologyBuildJobData(OntologyContract):
    job: OntologyBuildJob


class OntologyBuildJobListData(OntologyContract):
    jobs: list[OntologyBuildJob]


class OntologySourceDocumentSummary(OntologyContract):
    id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    source_role: OntologySourceRole = OntologySourceRole.SOURCE
    media_type: str = "application/octet-stream"
    size_bytes: int = Field(ge=0)
    status: OntologySourceStatus = OntologySourceStatus.STORED
    extracted_chunk_count: int = Field(default=0, ge=0)
    warnings_ja: list[str] = Field(default_factory=list)
    error_message_ja: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @classmethod
    def from_document(cls, source: OntologySourceDocument) -> OntologySourceDocumentSummary:
        return cls(
            id=source.id,
            profile_id=source.profile_id,
            filename=source.filename,
            source_role=source.source_role,
            media_type=source.media_type,
            size_bytes=source.size_bytes,
            status=source.status,
            extracted_chunk_count=source.extracted_chunk_count,
            warnings_ja=list(source.warnings_ja),
            error_message_ja=source.error_message_ja,
            created_at=source.created_at,
            updated_at=source.updated_at,
        )


class OntologySourceDocumentListData(OntologyContract):
    source_documents: list[OntologySourceDocumentSummary]


class OntologySourceDocumentDeleteData(OntologyContract):
    source_document_id: str
    deleted: bool = True


class OntologyProposalListData(OntologyContract):
    proposals: list[OntologyProposal]


@router.post(
    "/profiles/{profile_id}/ontology-build",
    response_model=ApiResponse[OntologyBuildJobData],
    status_code=202,
)
async def start_ontology_build(
    profile_id: str,
    http_request: Request,
    business_text: Annotated[str, Form()] = "",
    # 送られない bool の form は False（HTML の checkbox と同じ。FastAPI 0.147 から既定の値を
    # 使わない）。画面は 3 つとも送る。
    run_schema_naming: Annotated[bool, Form()] = False,
    run_qa_extraction: Annotated[bool, Form()] = False,
    run_text_extraction: Annotated[bool, Form()] = False,
    qa_file: Annotated[UploadFile | None, File()] = None,
    source_files: Annotated[list[UploadFile] | None, File()] = None,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> ApiResponse[OntologyBuildJobData]:
    """資料を保存し、永続 AI オントロジー構築 job を投入する。"""

    assert_profile_access(http_request, profile_id)
    # 互換 qa_file も source_files と同じ永続資料として保存し、解析は worker だけで行う。
    # 上限判定は qa_file を含む総数で行う(qa_file による上限回避を防ぐ)。
    uploads: list[tuple[UploadFile, OntologySourceRole]] = [
        (source_file, OntologySourceRole.SOURCE) for source_file in source_files or []
    ]
    if qa_file is not None:
        uploads.append((qa_file, OntologySourceRole.QA))
    if len(uploads) > ONTOLOGY_SOURCE_FILE_MAX_COUNT:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "ONTOLOGY_SOURCE_FILE_COUNT_EXCEEDED",
                "message_ja": (
                    f"構築資料は最大 {ONTOLOGY_SOURCE_FILE_MAX_COUNT} 件までアップロードできます。"
                    "ファイルを減らして再度実行してください。"
                ),
            },
        )

    # 未知 profile のときにファイルだけ残さない(保存前に profile を検証する)
    try:
        await run_sync_io(ontology_runtime.ensure_profile, profile_id)
    except Exception as exc:
        _raise_domain_error(exc)

    stored_sources = []
    for source_file, source_role in uploads:
        try:
            stored_sources.append(
                await ontology_source_storage.save_upload(
                    profile_id=profile_id,
                    upload=source_file,
                    source_role=source_role,
                )
            )
        except Exception as exc:
            # 後続ファイルが不正でも、先に保存した資料の実体を孤児として残さない
            if stored_sources:
                try:
                    await run_sync_io(
                        ontology_build_service.discard_source_documents, stored_sources
                    )
                except Exception:
                    logger.warning(
                        "ontology_build_source_cleanup_failed",
                        exc_info=True,
                        extra={"profile_id": profile_id},
                    )
            code = str(getattr(exc, "code", "ONTOLOGY_SOURCE_INVALID"))
            message = str(getattr(exc, "message_ja", str(exc)))
            raise HTTPException(
                status_code=400,
                detail={"code": code, "message_ja": message},
            ) from exc
    try:
        job = await run_sync_io(
            ontology_build_service.start,
            profile_id,
            business_text=business_text,
            qa_pairs=[],
            run_schema_naming=run_schema_naming,
            run_qa_extraction=run_qa_extraction,
            run_text_extraction=run_text_extraction,
            initial_warnings=[],
            source_documents=stored_sources,
            idempotency_key=idempotency_key,
        )
        # 同じ Idempotency-Key の再送(先行リクエストとの競合を含む)で既存 job が返った場合、
        # 今回保存した資料は job に使われないため破棄する
        used_source_ids = set(job.source_document_ids)
        unused_sources = [source for source in stored_sources if source.id not in used_source_ids]
        if unused_sources:
            await run_sync_io(ontology_build_service.discard_source_documents, unused_sources)
        return ApiResponse(data=OntologyBuildJobData(job=job))
    except Exception as exc:
        if stored_sources:
            try:
                await run_sync_io(ontology_build_service.discard_source_documents, stored_sources)
            except Exception:
                logger.warning(
                    "ontology_build_source_cleanup_failed",
                    exc_info=True,
                    extra={"profile_id": profile_id},
                )
        _raise_domain_error(exc)


@router.get(
    "/ontology-build/{job_id}",
    response_model=ApiResponse[OntologyBuildJobData],
)
def get_ontology_build_job(job_id: str, http_request: Request) -> ApiResponse[OntologyBuildJobData]:
    job = _run_runtime_sync(ontology_build_service.peek, job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "ONTOLOGY_BUILD_JOB_NOT_FOUND",
                "message_ja": "AI オントロジー構築 job が見つかりません。",
            },
        )
    assert_profile_access(http_request, job.profile_id)
    recovered = _run_runtime_sync(ontology_build_service.get, job_id)
    return ApiResponse(data=OntologyBuildJobData(job=recovered or job))


@router.get(
    "/profiles/{profile_id}/ontology-build-jobs",
    response_model=ApiResponse[OntologyBuildJobListData],
)
def list_profile_ontology_build_jobs(
    profile_id: str,
    http_request: Request,
    limit: int = 5,
) -> ApiResponse[OntologyBuildJobListData]:
    """リロード復旧・履歴表示用の build job 一覧(新しい順)。"""

    assert_profile_access(http_request, profile_id)
    try:
        jobs = _run_runtime_sync(
            ontology_build_service.list_profile_jobs,
            profile_id,
            limit=max(1, min(limit, 20)),
        )
        return ApiResponse(data=OntologyBuildJobListData(jobs=jobs))
    except Exception as exc:
        _raise_domain_error(exc)


@router.get(
    "/profiles/{profile_id}/ontology-source-documents",
    response_model=ApiResponse[OntologySourceDocumentListData],
)
def list_profile_ontology_source_documents(
    profile_id: str,
    http_request: Request,
    limit: int = 20,
) -> ApiResponse[OntologySourceDocumentListData]:
    """profile に保存済みの AI オントロジー構築資料を返す。"""

    assert_profile_access(http_request, profile_id)
    try:
        source_documents = _run_runtime_sync(
            ontology_build_service.list_profile_source_documents,
            profile_id,
            limit=max(1, min(limit, 50)),
        )
        return ApiResponse(
            data=OntologySourceDocumentListData(
                source_documents=[
                    OntologySourceDocumentSummary.from_document(source)
                    for source in source_documents
                ]
            )
        )
    except Exception as exc:
        _raise_domain_error(exc)


@router.delete(
    "/profiles/{profile_id}/ontology-source-documents/{source_document_id}",
    response_model=ApiResponse[OntologySourceDocumentDeleteData],
)
def delete_profile_ontology_source_document(
    profile_id: str,
    source_document_id: str,
    http_request: Request,
) -> ApiResponse[OntologySourceDocumentDeleteData]:
    """profile に保存済みの AI オントロジー構築資料を 1 件削除する。"""

    assert_profile_access(http_request, profile_id)
    try:
        deleted_source = _run_runtime_sync(
            ontology_build_service.delete_profile_source_document,
            profile_id,
            source_document_id,
        )
        return ApiResponse(
            data=OntologySourceDocumentDeleteData(
                source_document_id=deleted_source.id,
                deleted=True,
            )
        )
    except Exception as exc:
        _raise_domain_error(exc)


@router.post(
    "/ontology-build/{job_id}/cancel",
    response_model=ApiResponse[OntologyBuildJobData],
)
def cancel_ontology_build_job(
    job_id: str, http_request: Request
) -> ApiResponse[OntologyBuildJobData]:
    current = _run_runtime_sync(ontology_build_service.peek, job_id)
    if current is not None:
        assert_profile_access(http_request, current.profile_id)
    try:
        job = _run_runtime_sync(ontology_build_service.cancel, job_id)
        return ApiResponse(data=OntologyBuildJobData(job=job))
    except Exception as exc:
        _raise_domain_error(exc)


@router.post(
    "/ontology-build/{job_id}/retry",
    response_model=ApiResponse[OntologyBuildJobData],
    status_code=202,
)
def retry_ontology_build_job(
    job_id: str, http_request: Request
) -> ApiResponse[OntologyBuildJobData]:
    """failed/cancelled job を保存済み入力から再実行する(新規 job を返す)。"""

    current = _run_runtime_sync(ontology_build_service.peek, job_id)
    if current is not None:
        assert_profile_access(http_request, current.profile_id)
    try:
        job = _run_runtime_sync(ontology_build_service.retry, job_id)
        return ApiResponse(data=OntologyBuildJobData(job=job))
    except Exception as exc:
        _raise_domain_error(exc)


@router.get(
    "/profiles/{profile_id}/ontology-proposals",
    response_model=ApiResponse[OntologyProposalListData],
)
def list_profile_ontology_proposals(
    profile_id: str,
    http_request: Request,
) -> ApiResponse[OntologyProposalListData]:
    assert_profile_access(http_request, profile_id)
    try:
        return ApiResponse(
            data=OntologyProposalListData(
                proposals=_run_runtime_sync(
                    ontology_runtime.list_profile_proposals,
                    profile_id,
                )
            )
        )
    except Exception as exc:
        _raise_domain_error(exc)


__all__ = [
    "GenerateSqlRequest",
    "ImprovementProposalRequest",
    "OntologyApiRuntime",
    "OntologyGraphData",
    "ProfileOntologyViewPatch",
    "ProfileOntologyViewData",
    "QueryExecutionData",
    "QuerySessionApiCreate",
    "QuerySessionData",
    "SqlBindingRequest",
    "ontology_runtime",
    "router",
]


@router.get("/profiles/{profile_id}/ontology-results")
def get_profile_ontology_results(profile_id: str, request: Request) -> ApiResponse[Any]:
    from .ontology_definition_service import ProfileOntologyDefinitionService

    assert_profile_access(request, profile_id)
    try:
        results = ProfileOntologyDefinitionService(ontology_runtime).list_results(profile_id)
        from .ontology_presentation import bundle_view

        return ApiResponse(
            data={"results": [bundle_view(ontology_runtime.store, item) for item in results]}
        )
    except Exception as exc:
        _raise_domain_error(exc)


@router.get("/profiles/{profile_id}/ontology-results/{result_id}")
def get_profile_ontology_result(
    profile_id: str, result_id: str, request: Request
) -> ApiResponse[Any]:
    from .ontology_definition_service import ProfileOntologyDefinitionService

    assert_profile_access(request, profile_id)
    try:
        result = ProfileOntologyDefinitionService(ontology_runtime).get(profile_id, result_id)
        from .ontology_presentation import bundle_view

        return ApiResponse(data=bundle_view(ontology_runtime.store, result))
    except Exception as exc:
        _raise_domain_error(exc)


from .ontology_workspace_router import create_workspace_router  # noqa: E402

router.include_router(create_workspace_router(lambda: ontology_runtime, _raise_domain_error))

from .ontology_capability_router import create_capability_router  # noqa: E402

router.include_router(create_capability_router(lambda: ontology_runtime, _raise_domain_error))


router.include_router(create_markdown_router(lambda: ontology_runtime, _raise_domain_error))
