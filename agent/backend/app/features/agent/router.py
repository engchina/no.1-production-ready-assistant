"""Agent Runtime API。

業務 RAG / NL2SQL は外部ツールとして扱い、このサービスは実行管理・
権限・監査・表示用の標準契約を提供する。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import stat
import unicodedata
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from csv import DictWriter
from datetime import UTC, datetime
from functools import partial
from importlib import import_module
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, Literal, NoReturn

import httpx
from anyio import fail_after
from anyio import to_thread as anyio_to_thread
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import Response, StreamingResponse
from pr_backend_core import ApiResponse, Page
from pr_backend_core.api import (
    DEFAULT_PAGE_LIMIT,
    OffsetParams,
    offset_params,
    paginate,
    paginate_slice,
)
from pr_backend_core.bounded_calls import BoundedCallAborted
from pr_backend_core.chat_history_window import (
    chat_history_cutoff,
    chat_history_days,
    within_chat_history,
)
from pr_backend_core.chat_progress import CHAT_PROGRESS_SSE_HEADERS
from pr_backend_core.llm_request_params_httpx import asend_dropping_parameters
from pr_backend_core.logging import safe_exception_fields
from pr_system_settings.answer_notice import build_answer_notice_router
from pr_system_settings.auth.visibility import (
    DEFAULT_RUN_EXECUTOR_FILTER,
    OTHERS_CONTENT_FORBIDDEN_MESSAGE,
    OTHERS_CONTENT_FORBIDDEN_STATUS,
    OWNER_NOT_FOUND_STATUS,
    RunExecutorFilter,
    can_access_user_data,
    can_add_user_content,
    can_view_all_users_data,
    run_executor_matches,
)
from pr_system_settings.chat_attachments import (
    ChatAttachmentReference,
    build_chat_attachment_router,
    build_chat_attachment_settings_router,
)
from pr_system_settings.database import build_database_router
from pr_system_settings.database_status import (
    DatabaseSchemaProbeResult,
    build_database_status_router,
)
from pr_system_settings.model import (
    DEFAULT_ENTERPRISE_AI_REASONING_EFFORT,
    EnterpriseAiConnection,
    EnterpriseAiModelSettings,
    ModelSettingsTestRequest,
    build_model_router,
    embedding_api_base_url,
    embedding_model_connection,
    enterprise_ai_connection_for_model,
    model_payload,
    output_token_budget,
    reasoning_payload,
)
from pr_system_settings.model_test_input import (
    MODEL_TEST_EMBEDDING_INPUT,
    MODEL_TEST_RERANK_DOCUMENTS,
    MODEL_TEST_RERANK_QUERY,
    MODEL_TEST_TEXT_PROMPT,
    MODEL_TEST_VISION_PROMPT,
    embedding_test_details,
    model_test_image_data_url,
    model_test_text_context,
    rerank_test_details,
)
from pr_system_settings.trace_recording import build_trace_recording_router
from pr_system_settings.upload_storage import (
    build_upload_storage_router,
)
from pydantic import BaseModel, Field, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool

import app.settings as app_settings
from app.features.agent import (
    builtin_runtime,
    control_plane_store,
    run_facts_store,
)
from app.features.agent.chat_attachments import chat_attachment_service
from app.features.agent.chat_progress import run_trace_events
from app.features.agent.config import McpAuthMode, McpConnectionConfig, runtime_config_store
from app.features.agent.confirmation import (
    SOURCE_AGENT,
    ConfirmationAnswerError,
    ConfirmationDecisionRequest,
)
from app.features.agent.control_plane_store import (
    ControlPlaneStoreError,
)
from app.features.agent.data_scope import (
    DATA_SCOPE_CONNECTIONS,
    AgentDataScopes,
    DataScopeCandidate,
    DataScopeCandidatesData,
    DataScopeCandidatesError,
    added_profile_ids,
    list_candidates,
    verify_editor_can_use,
)
from app.features.agent.drafting import (
    AgentDraftError,
    AgentDraftRequest,
    AgentDraftSuggestion,
    suggest_agent_draft,
)
from app.features.agent.evaluation import (
    EVALUATION_EXPECTED_MAX_CHARS,
    EVALUATION_EXPECTED_TOOLS_MAX,
    EVALUATION_JOBS_PAGE_SIZE,
    EVALUATION_JOBS_PAGE_SIZE_MAX,
    EVALUATION_MAX_CASES,
    CaseStatus,
    EvaluationBusyError,
    EvaluationCase,
    EvaluationCaseDraft,
    EvaluationCaseDuplicateError,
    EvaluationCaseResult,
    EvaluationCasesData,
    EvaluationComparison,
    EvaluationJob,
    EvaluationJobActiveError,
    EvaluationJobsData,
    EvaluationRequest,
    EvaluationSet,
    EvaluationSetFromTemplateRequest,
    EvaluationSetFullError,
    EvaluationSetInput,
    EvaluationSetsData,
    EvaluationSummary,
    JobStatus,
    cases_fingerprint,
    compare_jobs,
    draft_definition_hash,
    evaluation_set_store,
    evaluation_store,
    judge_fingerprint,
    paired_comparison,
    recorded_output_rows,
    run_evaluation_job,
    set_item,
)
from app.features.agent.evaluation_excel import (
    EXCEL_MAX_BYTES,
    EvaluationExcelError,
    export_set_xlsx,
    parse_cases_xlsx,
    template_xlsx,
)
from app.features.agent.evidence_access import (
    rag_document_ids,
    unreadable_documents,
    withheld_count,
    withhold_artifacts,
    withhold_events,
    withhold_run,
)
from app.features.agent.feedback import (
    FEEDBACK_PAGE_SIZE,
    FEEDBACK_PAGE_SIZE_MAX,
    FEEDBACK_PERIOD_DAYS,
    FeedbackReport,
    build_feedback_report,
)
from app.features.agent.mcp_url import (
    mask_url_credentials,
    url_credential_error,
    url_has_credentials,
)
from app.features.agent.rag_figures import (
    EVIDENCE_ARTIFACT_KINDS,
    RunFigureUrlData,
    RunFigureUrlError,
    RunSourcePreviewData,
    issue_run_figure_url,
    issue_run_source_preview,
)
from app.features.agent.run_facts import RUN_SOURCE_MCP, fact_from_run, run_source
from app.features.agent.run_signals import (
    SIGNAL_PAGE_SIZE,
    SIGNAL_PAGE_SIZE_MAX,
    SignalKind,
    SignalReport,
    build_signal_report,
)
from app.features.agent.runtime import (
    AGENT_UNPUBLISHED_MESSAGE,
    BUILTIN_RUNTIME_ID,
    MCP_API_KEY_NAME_KEY,
    AgentNotPublishedError,
    AgentProfile,
    AgentProfileCreate,
    AgentProfilePatch,
    AgentPublishRequest,
    AgentsData,
    AgentShareApprovalNotice,
    AgentShareRequest,
    ApprovalDecisionRequest,
    ApprovalDetailData,
    ApprovalsData,
    ApprovalStatus,
    ApprovalSummary,
    Artifact,
    ArtifactsData,
    ConfirmationClosedError,
    ConfirmationOutcome,
    FeedbackRating,
    FeedbackReason,
    RunCancellationView,
    RunCreateRequest,
    RunEvent,
    RunFeedback,
    RunFeedbackRequest,
    RunNotRatableError,
    RunsData,
    RunState,
    RunStatus,
    RunStep,
    RunSummary,
    RuntimeToolCallAuditData,
    RunView,
    RunViewer,
    ThreadBusyError,
    ThreadKindMismatchError,
    ThreadNotFoundError,
    ThreadsData,
    ThreadSummary,
    ThreadUpdateRequest,
    ThreadView,
    agent_unavailable_reason,
    builtin_resume_pending,
    max_published_versions,
    reserved_run_metadata_keys,
    run_cancel_record,
    run_summary,
    run_thread_owner,
    runtime_repository,
)
from app.features.agent.skill_export import export_skill
from app.features.agent.skill_updates import (
    AgentSkillUpdates,
    PublishedVersionRollbackRequest,
    SkillUpdatePublishData,
    SkillUpdatePublishRequest,
    SkillUpdatePublishResult,
    agent_skill_updates,
)
from app.features.agent.skills import (
    AgentSkillDefinition,
    AgentSkillListOutput,
    SkillMcpRequirement,
    reload_declared_skills,
    skill_registry,
)
from app.features.agent.storage_status import RuntimeStorageStatus, runtime_storage_status
from app.features.agent.templates import AGENT_TEMPLATES, AgentTemplatesData, find_template
from app.features.agent.tool_policy_catalog import ToolPolicyToolsData, tool_policy_catalog
from app.features.agent.tools import (
    MCP_TOOL_SEPARATOR,
    ExternalMcpToolsData,
    ExternalToolError,
    ToolCall,
    ToolDefinitionsData,
    ToolInvocationContext,
    ToolPolicy,
    ToolResult,
    list_mcp_connection_tools,
    mcp_tool_definition,
    tool_registry,
)
from app.features.agent.trace_spans import trace_spans
from app.features.agent.usage import (
    USAGE_PERIOD_DAYS,
    UsageReport,
    build_usage_report,
    resolve_timezone,
)
from app.features.agent.user_names import login_display_names, user_display_names
from app.observability import (
    ObservabilityStatus,
    TraceEventsData,
    TraceExportRetryData,
    TracePolicyPatch,
    TracePolicySettings,
    flush_trace_export_retry_queue,
    get_trace_policy,
    list_trace_events,
    patch_trace_policy,
    trace_exporter_status,
)
from app.oracle_connection import (
    close_platform_oracle_pool,
    oracle_connect_kwargs,
    platform_oracle_connection,
)
from app.security.dependencies import (
    actor_roles_for_principal,
    permission_route_path,
)
from app.security.domain import Principal
from app.security.permissions import (
    AGENTS_BUILD,
    CAPABILITY_ROLES,
    PERMISSION_CATALOG,
    UNCLASSIFIED_PERMISSION,
    permission_for_route,
)
from app.security.service import get_security_service
from app.settings import MODEL_SETTINGS_STORE, get_settings
from app.system_schema import system_schema_manager

router = APIRouter(tags=["agent-runtime"])
logger = logging.getLogger(__name__)

# 画面（チャット・実行履歴・承認）がそのまま出す理由。英語・内部の ID を出さない。
AGENT_NOT_FOUND_MESSAGE = "業務 Agent が見つかりません。"
DRAFT_RUN_FORBIDDEN_MESSAGE = (
    "下書きで実行できるのは、Agent 管理の権限がある利用者か、その業務 Agent の持ち主だけです。"
)
AGENT_FORBIDDEN_MESSAGE = "この業務 Agent を利用する権限がありません。"
RUN_NOT_FOUND_MESSAGE = "実行が見つかりません。"
RUN_TRACE_UNAVAILABLE_MESSAGE = "処理の経過を読み込めませんでした。時間をおいて再度お試しください。"
APPROVAL_NOT_FOUND_MESSAGE = "承認の依頼が見つかりません。"

PASSPHRASE_CONFIG_KEYS = frozenset({"pass_phrase", "passphrase", "key_password"})


# 接続 `rag` の既定（630 秒。RAG の回答の上限 600 秒より長い。#1425）を画面でも保存し直せる上限。
_MCP_TIMEOUT_MAX_SECONDS = 900


def _validate_mcp_timeout(value: float | None) -> float | None:
    """外部 MCP のタイムアウト秒（画面の「タイムアウト秒」と同じ規則・文言。#540）。

    省略（None）は既定値（作成）・変更なし（更新）。
    0 以下を保存すると呼び出しがすぐ失敗するため受け付けない。
    """
    if value is None:
        return None
    if not 0 < value <= _MCP_TIMEOUT_MAX_SECONDS:
        raise ValueError(
            f"タイムアウト秒は 0 より大きく {_MCP_TIMEOUT_MAX_SECONDS} 以下の"
            "数値を入力してください。"
        )
    return value


_MCP_CONNECTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")


def _validate_mcp_url(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if value and not re.match(r"^https?://[^\s/]+", value):
        raise ValueError("MCP の URL は http:// または https:// で始めてください。")
    # 資格情報は認証の欄（secret として保存し、一覧に出さない）へ。URL に書くと一覧に出る（#1056）。
    credential_error = url_credential_error(value) if value else None
    if credential_error:
        raise ValueError(credential_error)
    return value


class McpConnectionSettings(BaseModel):
    """MCP 接続の公開設定（#757。資格情報の値は返さず、設定済みかだけを返す）。"""

    server_id: str
    label: str | None = None
    # URL の userinfo・secret らしい query の値は `***` に伏せる（#1056）。
    base_url: str | None = None
    # 保存した URL に資格情報があり、base_url を伏せて返したか（画面は URL を変えない限り
    # base_url を送らない）。
    base_url_masked: bool = False
    auth_mode: McpAuthMode
    service_audience: str | None = None
    timeout_seconds: float
    # 由来: builtin(RAG / NL2SQL) / env / runtime。runtime だけ削除できる。
    source: str
    removable: bool
    # 配備（環境変数）が URL を決めた標準の接続か（#1325）。True なら URL は変えられない。
    base_url_locked: bool = False
    # URL と認証方式に必要な資格情報がそろい、呼び出せる状態か。
    configured: bool
    api_key_configured: bool = False
    oauth_configured: bool = False
    session_configured: bool = False
    # サービストークン: 共通 `.env` の PLATFORM_SERVICE_TOKEN_SECRET（値は返さない）。
    service_token_configured: bool = False
    # ツールの注釈（readOnlyHint）を既定の判定に使うか（#1832。標準の接続は常に True）。
    trust_tool_annotations: bool = False


class McpConnectionsData(BaseModel):
    connections: list[McpConnectionSettings] = Field(default_factory=list)


class McpConnectionPatch(BaseModel):
    label: str | None = None
    base_url: str | None = None
    auth_mode: McpAuthMode | None = None
    api_key: str | None = None
    timeout_seconds: float | None = None
    session_id: str | None = None
    oauth_token_url: str | None = None
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None
    oauth_scope: str | None = None
    service_audience: str | None = None
    # ツールの注釈（readOnlyHint）を既定の判定に使うか（#1832。標準の接続は常に使う）。
    trust_tool_annotations: bool | None = None

    @field_validator("timeout_seconds")
    @classmethod
    def _validate_timeout(cls, value: float | None) -> float | None:
        return _validate_mcp_timeout(value)

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str | None) -> str | None:
        return _validate_mcp_url(value)


class McpConnectionCreate(McpConnectionPatch):
    server_id: str

    @field_validator("server_id")
    @classmethod
    def _validate_server_id(cls, value: str) -> str:
        value = value.strip()
        # ID はモデルに渡すツール名（`<接続>__<ツール>`）の先頭になる。
        if not _MCP_CONNECTION_ID_PATTERN.match(value) or "__" in value or value == "control-plane":
            raise ValueError(
                "接続 ID は英数字で始まる 40 文字以内の英数字・「-」・「_」で入力してください"
                "（「__」と control-plane は使えません）。"
            )
        return value


# 画面・API で作るスキルの ID（URL の path に置くため `/`・空白などを断る。#926）。
# 宣言（SKILL.md・env）から読み込む既存の ID には当てない。
SKILL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
SKILL_NOT_FOUND_MESSAGE = "スキルが見つかりません。"


class AgentSkillCreate(BaseModel):
    id: str
    name: str
    description: str = ""
    instructions: str = ""
    mcp_requirements: list[SkillMcpRequirement] = Field(default_factory=list)
    enabled: bool = True
    tags: list[str] = Field(default_factory=list)


class AgentSkillPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    instructions: str | None = None
    mcp_requirements: list[SkillMcpRequirement] | None = None
    enabled: bool | None = None
    tags: list[str] | None = None


class ToolPolicySettings(BaseModel):
    default_mode: str = "approval"
    allow: list[str] = Field(default_factory=list)
    ask: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class ToolPolicySettingsPatch(BaseModel):
    default_mode: str | None = None
    allow: list[str] | None = None
    ask: list[str] | None = None
    deny: list[str] | None = None


class ToolAuditRecord(BaseModel):
    step_id: str
    tool_name: str
    status: str
    approval_id: str | None = None
    approval_status: str | None = None
    policy_decision: str | None = None
    permission_level: str | None = None
    side_effects: bool | None = None
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = None
    success: bool | None = None
    error: str | None = None
    error_code: str | None = None
    guardrail_warnings: list[str] = Field(default_factory=list)
    trace_id: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    audit_metadata: dict[str, object] = Field(default_factory=dict)


class RunAuditData(BaseModel):
    run_id: str
    goal: str
    status: str
    records: list[ToolAuditRecord]


class ToolCallAuditRecord(ToolAuditRecord):
    run_id: str
    run_goal: str
    run_status: str
    agent_id: str
    run_created_at: str
    run_updated_at: str
    # Run のきっかけ（user / evaluation / mcp / replay。#1874）と、MCP の Run の
    # API キーの名前（監査の「MCP（キーの名前）」）。
    run_source: str = "user"
    api_key_name: str | None = None


def _run_origin(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """監査の記録に足す Run のきっかけ（#1874）。"""
    source = run_source(metadata)
    name = metadata.get(MCP_API_KEY_NAME_KEY) if source == RUN_SOURCE_MCP else None
    return {"run_source": source, "api_key_name": name if isinstance(name, str) else None}


def _with_run_origin(record: ToolCallAuditRecord) -> ToolCallAuditRecord:
    """保存先の projection の記録に、Run の metadata からきっかけを足す（読めなければそのまま）。"""
    try:
        run = runtime_repository.get_run(record.run_id)
    except KeyError:
        return record
    return record.model_copy(update=_run_origin(run.metadata))


class ToolCallAuditData(Page[ToolCallAuditRecord]):
    """ツール監査の 1 ページ（共通の `Page` に絞り込みの条件とツール名を足した形。#1266）。"""

    filters: dict[str, object] = Field(default_factory=dict)
    # 見られる範囲の監査に記録されたツール名（絞り込みの条件に依らない）。
    # 画面のツール名の選択肢に使う。
    # MCP 接続のツール（`<接続>__<ツール>`）は `/api/tools` に出ないため（#983）。
    tool_names: list[str] = Field(default_factory=list)


class ActorPolicy(BaseModel):
    roles: set[str] = Field(default_factory=set)
    # 使える業務 Agent（None は制限なし）。ロールに割り当てた Agent と、持ち主の個人の
    # Agent（#2523）。
    agent_ids: set[str] | None = None
    user_uuid: str | None = None
    # 自分の業務 Agent を作れる（`agent.agents.build`。#2523）。
    can_build: bool = False


async def require_viewer(request: Request) -> None:
    _require_actor_roles(request, {"viewer", "operator", "approver", "auditor"})


async def require_operator(request: Request) -> None:
    _require_actor_roles(request, {"operator"})


async def require_approver(request: Request) -> None:
    _require_actor_roles(request, {"approver"})


async def require_auditor(request: Request) -> None:
    _require_actor_roles(request, {"auditor"})


async def require_admin(request: Request) -> None:
    _require_actor_roles(request, {"admin"})


AGENT_BUILD_FORBIDDEN_MESSAGE = (
    "業務 Agent を作れるのは、Agent 管理か、自分の業務 Agent の作成の権限がある利用者だけです。"
)
AGENT_MANAGE_FORBIDDEN_MESSAGE = "この業務 Agent を変更できるのは、Agent 管理か持ち主だけです。"


async def require_agent_builder(request: Request) -> None:
    """業務 Agent を作る・自分の業務 Agent を管理する（Agent 管理か `agent.agents.build`）。"""
    if _request_principal(request) is None:
        raise HTTPException(status_code=401, detail="ログインしてください。")
    policy = _actor_policy(request)
    if "admin" not in policy.roles and not policy.can_build:
        raise HTTPException(status_code=403, detail=AGENT_BUILD_FORBIDDEN_MESSAGE)


async def require_system_settings_write(request: Request) -> None:
    """共通のシステム設定の保存・操作（アップロード保存先・モデル・データベース。#215）。

    RAG / NL2SQL と同じくメニュー権限（manifest の同じ判定）で許可する。local のローカル利用者は
    全権限を持つ。
    """
    principal = _request_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="ログインしてください。")
    permissions = permission_for_route(request.method, permission_route_path(request))
    if (
        permissions is None
        or UNCLASSIFIED_PERMISSION in permissions
        or not principal.has_any_permission(set(permissions))
    ):
        # 共通の route の権限の拒否と同じ文にする（ログインユーザー ID や英語の内部の文を出さない。
        # error_code は付けない。#1108）。
        raise HTTPException(status_code=403, detail="この機能を利用する権限がありません。")


def _attachment_principal(request: Request) -> Principal:
    """添付の API の利用者（`/api` の認証が `state.principal` に入れる）。"""
    principal = _request_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="ログインしてください。")
    return principal


def _thread_attachment_owner(principal: Principal, thread_id: str) -> str:
    """添付する会話（thread）の持ち主の UUID。見えない会話は 404（「AI 活用」の見え方。#1889）。

    持ち主の分からない会話は空文字（platform の添付の API が添付を 403 にする。一覧は閲覧なので
    返す。#3244）。
    """
    try:
        thread = runtime_repository.get_thread(
            thread_id,
            user_uuid=principal.user_uuid,
            all_users=can_view_all_users_data(principal),
        )
    except ThreadNotFoundError as exc:
        raise HTTPException(status_code=404, detail="会話が見つかりません。") from exc
    return thread.owner_user_uuid or ""


# チャットの添付（会話の期間だけ使う作業ファイル。3 製品共通の実装。#2517）。
router.include_router(
    build_chat_attachment_router(
        get_service=lambda: chat_attachment_service(),
        current_principal=_attachment_principal,
        conversation_owner=_thread_attachment_owner,
    )
)
# アップロード保存先は3製品共通の実装（platform の pr_system_settings。#97）。
# 保存先は3製品共通の `.env`（app.settings.PLATFORM_ENV_FILE。#211）。
# テストで get_settings / PLATFORM_ENV_FILE を差し替えられるよう、呼出時に module の値を参照する。
router.include_router(
    build_upload_storage_router(
        get_settings=lambda: get_settings(),
        env_file=lambda: app_settings.PLATFORM_ENV_FILE,
        write_dependencies=[Depends(require_system_settings_write)],
    ),
    prefix="/settings",
)
# データベース設定も3製品共通の実装（pr_system_settings.database。#108）。
# 接続は Agent の Thin mode 実装を渡す。接続テストも管理者に限定する。
router.include_router(
    build_database_router(
        get_settings=lambda: get_settings(),
        env_file=lambda: app_settings.PLATFORM_ENV_FILE,
        test_connection=lambda candidate: _test_database_connection(candidate),
        # 接続先・Wallet を変えたら、古い設定の接続 pool を閉じる（RAG / NL2SQL と同じ。#793）。
        on_saved=lambda _settings: close_platform_oracle_pool(),
        write_dependencies=[Depends(require_system_settings_write)],
        action_dependencies=[Depends(require_system_settings_write)],
    ),
    prefix="/settings",
)
# 回答の注意書きも3製品共通の実装（pr_system_settings.answer_notice。#2405）。
# 保存先は共通の表のこの製品の行（#2449）。
router.include_router(
    build_answer_notice_router(
        load=lambda: get_security_service().load_answer_notice(),
        save=lambda record: get_security_service().save_answer_notice(record),
        write_dependencies=[Depends(require_system_settings_write)],
    ),
    prefix="/settings",
)
router.include_router(
    build_trace_recording_router(
        provider=lambda: get_security_service().trace_recording,
        write_dependencies=[Depends(require_system_settings_write)],
    ),
    prefix="/settings",
)
# チャットの添付を使うか（運用設定 › チャットの添付。設定は製品ごと。既定は無効。#2517）。
router.include_router(
    build_chat_attachment_settings_router(
        load=lambda: get_security_service().chat_attachment_settings.load(),
        save=lambda record: get_security_service().chat_attachment_settings.save(record),
        get_settings=lambda: get_settings(),
        write_dependencies=[Depends(require_system_settings_write)],
    ),
    prefix="/settings",
)
# DB の状態 API（`GET /api/ready/database`。3製品共通の判定と契約。#325）。画面の DB ゲートが使う。
# ログイン不要の公開 path（`app.security.permissions.PUBLIC_API_PATHS`）。RAG / NL2SQL と同じく、
# 設定の判定・接続確認に加えて Agent のシステムテーブルの状態を確かめる（#751）。local でも
# 共通認証のユーザー・ロールは共通 DB にあるため、短絡しない。
# 接続確認は保存済みの設定で pool から借りる（要求のたびに新しい接続を張らない。#793）。
router.include_router(
    build_database_status_router(
        get_settings=lambda: get_settings(),
        test_connection=lambda settings: _test_database_status_connection(settings),
        schema_probe=lambda settings: _system_schema_probe(settings),
    )
)


async def _system_schema_probe(_settings: object) -> DatabaseSchemaProbeResult:
    """Agent のシステムテーブルの状態（例外は共通部品が setup_required へ正規化する）。"""
    schema = await run_in_threadpool(system_schema_manager.status)
    schema_status = schema["status"]
    if schema_status != "ready" or schema["operation_state"]["status"] == "running":
        return DatabaseSchemaProbeResult(status="setup_required", schema_status=schema_status)
    return DatabaseSchemaProbeResult(status="ok", schema_status="ready")


# モデル設定も3製品共通の実装（pr_system_settings.model。#103）。
# 接続テストは外部へ通信するため管理者に限定する。
router.include_router(
    build_model_router(
        get_settings=lambda: get_settings(),
        store=MODEL_SETTINGS_STORE,
        run_model_test=lambda settings, request: _run_model_settings_test(settings, request),
        write_dependencies=[Depends(require_system_settings_write)],
        action_dependencies=[Depends(require_system_settings_write)],
    ),
    prefix="/settings",
)


def _settings_attr(name: str, default: object) -> object:
    return getattr(get_settings(), name, default)


def _settings_str_from(settings: object, name: str, default: str = "") -> str:
    value = getattr(settings, name, default)
    return str(default if value is None else value)


def _settings_str(name: str, default: str = "") -> str:
    return _settings_str_from(get_settings(), name, default)


def _settings_int_from(settings: object, name: str, default: int) -> int:
    value = getattr(settings, name, default)
    return _coerce_int(value, default)


def _coerce_int(value: object, default: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float | str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _settings_int(name: str, default: int) -> int:
    return _settings_int_from(get_settings(), name, default)


def _settings_float_from(settings: object, name: str, default: float) -> float:
    value = getattr(settings, name, default)
    return _coerce_float(value, default)


def _coerce_float(value: object, default: float) -> float:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _settings_float(name: str, default: float) -> float:
    return _settings_float_from(get_settings(), name, default)


def _is_present(value: str) -> bool:
    return bool(value.strip())


def _json_pointer_or_empty(value: str) -> str:
    normalized = value.strip()
    return normalized if not normalized or normalized.startswith("/") else ""


async def _run_model_settings_test(
    settings: Any,
    request: ModelSettingsTestRequest,
) -> dict[str, str | int | float | bool | None]:
    """対象モデルの実 API 呼び出しを行い、表示用 details を返す（共有 router の hook）。

    `settings` は保存前の入力値と対象モデルを反映した一時 Settings（#103）。
    Enterprise AI はテストするモデルの接続（Endpoint・Project・API key）で呼ぶ（#533）。
    """
    payload = model_payload(settings)
    enterprise_ai = payload.enterprise_ai
    if request.target_type in {"enterprise_text", "enterprise_vision"}:
        # LLM のモデル（種類 text）の接続で呼ぶ。埋め込み / 再ランクは下で、
        # そのモデルの接続で呼ぶ（#45）。
        connection = enterprise_ai_connection_for_model(settings, request.model_id)
        if request.target_type == "enterprise_text":
            text = await _run_enterprise_text_model_test(enterprise_ai, connection)
            return {"response_chars": len(text), "surface": "llm"}
        text = await _run_enterprise_vision_model_test(enterprise_ai, connection)
        return {"response_chars": len(text), "surface": "vision"}
    # 埋め込み / 再ランクは、テストするモデルの接続（Endpoint・API key）で呼ぶ（#45。
    # `settings` は対象モデルを既定の埋め込み / 再ランクのモデルにした一時 Settings）。
    if request.target_type == "embedding":
        return dict(
            embedding_test_details(
                await _run_embedding_model_test(settings), expected_dim=_embedding_dim(settings)
            )
        )
    return dict(rerank_test_details(await _run_rerank_model_test(settings)))


async def _run_enterprise_text_model_test(
    settings: EnterpriseAiModelSettings, connection: EnterpriseAiConnection
) -> str:
    payload = _enterprise_text_payload(
        settings,
        prompt=MODEL_TEST_TEXT_PROMPT,
        context=model_test_text_context("Production Ready Agent"),
        project_id=connection.project_id,
    )
    response = await _post_enterprise_ai(settings, connection, settings.api_path, payload)
    return _parse_enterprise_text_response(response, settings.text_response_path)


async def _run_enterprise_vision_model_test(
    settings: EnterpriseAiModelSettings, connection: EnterpriseAiConnection
) -> str:
    payload = _enterprise_vision_payload(
        settings,
        prompt=MODEL_TEST_VISION_PROMPT,
        project_id=connection.project_id,
    )
    response = await _post_enterprise_ai(settings, connection, settings.api_path, payload)
    return _parse_enterprise_text_response(response, settings.vision_response_path)


def _enterprise_text_model_id(settings: EnterpriseAiModelSettings) -> str:
    """画像を扱わない呼び出しのモデル（既定のテキストモデル、未設定なら既定の Vision モデル）。"""
    return settings.default_text_model_id or settings.default_vision_model_id


def _enterprise_text_payload(
    settings: EnterpriseAiModelSettings,
    *,
    prompt: str,
    context: str,
    project_id: str = "",
) -> dict[str, object]:
    system_prompt = "根拠に基づいて日本語で簡潔に回答してください。"
    user_message = f"{context}\n\n質問: {prompt}" if context else prompt
    max_output_tokens = _model_output_token_budget(settings, _enterprise_text_model_id(settings))
    values = {
        "model": _enterprise_text_model_id(settings),
        "project": project_id,
        "project_id": project_id,
        "prompt": prompt,
        "context": context,
        "system_prompt": system_prompt,
        "user_message": user_message,
        "input": [{"role": "user", "content": user_message}],
        "instructions": system_prompt,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0,
        "max_output_tokens": max_output_tokens,
    }
    if settings.text_payload_template.strip():
        return _render_model_payload_template(settings.text_payload_template, values)
    return _with_model_reasoning(
        settings,
        _enterprise_text_model_id(settings),
        {
            "model": _enterprise_text_model_id(settings),
            "instructions": system_prompt,
            "input": [{"role": "user", "content": user_message}],
            "temperature": 0,
            "max_output_tokens": max_output_tokens,
        },
    )


def _model_output_token_budget(settings: EnterpriseAiModelSettings, model_id: str) -> int:
    """テストするモデルの `max_output_tokens`（#1719。モデルの出力の上限と入力の上限の小さい方）。

    画面の保存前の値（`settings.models`）で決める（推論の量と同じ）。登録の無いモデルは
    既定の 32768。値の解釈は platform の `output_token_budget`。
    """
    model = next((item for item in settings.models if item.model_id == model_id), None)
    return output_token_budget(
        max_input_tokens=model.max_input_tokens if model else None,
        max_output_tokens=model.max_output_tokens if model else None,
    )


def _with_model_reasoning(
    settings: EnterpriseAiModelSettings, model_id: str, payload: dict[str, object]
) -> dict[str, object]:
    """テストするモデルの推論（思考）の量を `reasoning` に入れる（#1425。none は送らない）。

    画面の保存前の値（`settings.models`）で呼ぶので、選んだ値をモデルが受け付けるかを確かめられる。
    """
    effort = next(
        (model.reasoning_effort for model in settings.models if model.model_id == model_id),
        DEFAULT_ENTERPRISE_AI_REASONING_EFFORT,
    )
    reasoning = reasoning_payload(effort)
    if reasoning is not None:
        payload["reasoning"] = reasoning
    return payload


def _enterprise_vision_payload(
    settings: EnterpriseAiModelSettings,
    *,
    prompt: str,
    project_id: str = "",
) -> dict[str, object]:
    content = [
        {"type": "input_text", "text": prompt},
        {"type": "input_image", "image_url": model_test_image_data_url()},
    ]
    values = {
        "model": settings.default_vision_model_id,
        "project": project_id,
        "project_id": project_id,
        "prompt": prompt,
        "input": [{"role": "user", "content": content}],
        "messages": [{"role": "user", "content": content}],
        "max_output_tokens": _model_output_token_budget(settings, settings.default_vision_model_id),
    }
    if settings.vision_payload_template.strip():
        return _render_model_payload_template(settings.vision_payload_template, values)
    return _with_model_reasoning(
        settings,
        settings.default_vision_model_id,
        {
            "model": settings.default_vision_model_id,
            "input": [{"role": "user", "content": content}],
            "max_output_tokens": _model_output_token_budget(
                settings, settings.default_vision_model_id
            ),
        },
    )


def _render_model_payload_template(
    template: str,
    values: Mapping[str, object],
) -> dict[str, object]:
    try:
        rendered = template
        for key, value in values.items():
            rendered = rendered.replace("{{" + key + "}}", json.dumps(value, ensure_ascii=False))
            rendered = rendered.replace("{" + key + "}", str(value))
        parsed = json.loads(rendered)
    except ValueError as exc:
        raise ValueError("payload template は JSON object で入力してください。") from exc
    if not isinstance(parsed, dict):
        raise ValueError("payload template は JSON object で入力してください。")
    return parsed


async def _post_enterprise_ai(
    settings: EnterpriseAiModelSettings,
    connection: EnterpriseAiConnection,
    api_path: str,
    payload: Mapping[str, object],
) -> Mapping[str, object]:
    # Endpoint URL は入力どおり使い、API path を付ける（#1440。3 製品で platform の関数）。
    url = connection.request_url(api_path)
    api_key = _require_non_empty(connection.api_key, "Model API key")
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    if connection.project_id:
        headers["OpenAI-Project"] = connection.project_id
    async with httpx.AsyncClient(timeout=settings.timeout_seconds) as client:
        # モデルが受け付けない引数（temperature など）の 400 は、外して 1 回だけ送り直し、
        # 以後このモデルには送らない（#3539。外した引数は結果の詳細に出る）。
        response = await asend_dropping_parameters(
            client.send, client.build_request("POST", url, headers=headers, json=dict(payload))
        )
    if response.is_error:
        # 受け付けない推論の量（#1425）などの 400 は、応答の本文に理由があるので結果の詳細に出す。
        raise ValueError(f"Model API HTTP {response.status_code}: {response.text[:500]}")
    data = response.json()
    if not isinstance(data, Mapping):
        raise ValueError("Model API response は JSON object である必要があります。")
    return data


def _parse_enterprise_text_response(
    response: Mapping[str, object],
    response_path: str,
) -> str:
    if response_path.strip():
        value = _json_pointer_value(response, response_path)
        if isinstance(value, str) and value.strip():
            return value
        raise ValueError("Enterprise AI response path から回答 text を取得できませんでした。")
    # 思考の部分(Responses の reasoning item、`reasoning_text` / `thinking` などの content block、
    # Chat Completions の `reasoning_content`、本文の先頭の `<think>` タグ)は使わない(#1423)。
    output_text = response.get("output_text")
    if isinstance(output_text, str) and _strip_leading_thinking(output_text).strip():
        return _strip_leading_thinking(output_text)
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, Mapping):
            message = first.get("message")
            if isinstance(message, Mapping):
                content = message.get("content")
                if isinstance(content, str) and _strip_leading_thinking(content).strip():
                    return _strip_leading_thinking(content)
                if isinstance(content, list) and (text := _first_answer_part_text(content)):
                    return text
    output = response.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, Mapping) or _is_reasoning_part(item):
                continue
            content = item.get("content")
            if isinstance(content, list) and (text := _first_answer_part_text(content)):
                return text
    raise ValueError("Enterprise AI response から回答 text を取得できませんでした。")


# 推論(思考)の部分の type(#1423)。Responses の `output` の item(`type=reasoning`)と、content の
# block(`reasoning_text` / `summary_text`、Anthropic 形式の `thinking` / `redacted_thinking`)。
_REASONING_PART_TYPES = frozenset(
    {"reasoning", "reasoning_text", "summary_text", "thinking", "redacted_thinking"}
)
# 本文の先頭に思考をタグで入れるモデル(`<think>…</think>` / `<thinking>…</thinking>`)の思考の部分。
_LEADING_THINKING_TAG_RE = re.compile(r"^\s*<(think|thinking)>.*?</\1>\s*", re.I | re.S)


def _is_reasoning_part(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    return str(value.get("type", "")).strip().lower() in _REASONING_PART_TYPES


def _strip_leading_thinking(text: str) -> str:
    return _LEADING_THINKING_TAG_RE.sub("", text, count=1)


def _first_answer_part_text(content: list[object]) -> str:
    """content の配列から、思考ではない最初の本文を返す(無ければ空)。"""
    for part in content:
        if not isinstance(part, Mapping) or _is_reasoning_part(part):
            continue
        text = part.get("text")
        if isinstance(text, str) and _strip_leading_thinking(text).strip():
            return _strip_leading_thinking(text)
    return ""


def _json_pointer_value(document: Mapping[str, object], pointer: str) -> object:
    current: object = document
    for raw_part in pointer.strip().split("/")[1:]:
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            current = current.get(part)
            continue
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
            continue
        return None
    return current


async def _run_embedding_model_test(settings: object) -> list[list[float]]:
    """OpenAI 互換の `POST {base_url}/embeddings` で 1 件埋め込み、ベクトルを返す（#8 / #45）。

    Oracle の VECTOR 列に合わせるため `dimensions`（`genai_embedding_dim`）を必ず送る
    （次元の検証は `embedding_test_details`）。
    """
    url = f"{_embedding_base_url(settings, rerank=False)}/embeddings"
    body: dict[str, object] = {
        "model": _settings_str_from(settings, "genai_embedding_model"),
        "input": [MODEL_TEST_EMBEDDING_INPUT],
        "dimensions": _embedding_dim(settings),
    }
    api_key = _embedding_api_key(settings, rerank=False)
    timeout = _embedding_timeout(settings)
    payload = await anyio_to_thread.run_sync(
        lambda: _post_openai_compatible(url, body, api_key, timeout)
    )
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows:
        raise ValueError("embedding の応答に data がありません。")
    vector = rows[0].get("embedding") if isinstance(rows[0], Mapping) else None
    if not isinstance(vector, list):
        raise ValueError("embedding の応答の vector が不正です。")
    return [[float(value) for value in vector]]


async def _run_rerank_model_test(settings: object) -> list[tuple[int, float]]:
    """OpenAI 互換の `POST {base_url}/reranks` を呼ぶ（#8 / #45。複数形。`/rerank` は 404）。

    固定の query と 2 文書を渡し、(index, relevance_score) を順位の順に返す。
    """
    url = f"{_embedding_base_url(settings, rerank=True)}/reranks"
    body: dict[str, object] = {
        "model": _settings_str_from(settings, "genai_rerank_model"),
        "query": MODEL_TEST_RERANK_QUERY,
        "documents": list(MODEL_TEST_RERANK_DOCUMENTS),
        "top_n": len(MODEL_TEST_RERANK_DOCUMENTS),
    }
    api_key = _embedding_api_key(settings, rerank=True)
    timeout = _embedding_timeout(settings)
    payload = await anyio_to_thread.run_sync(
        lambda: _post_openai_compatible(url, body, api_key, timeout)
    )
    results = payload.get("results")
    if not isinstance(results, list):
        raise ValueError("rerank の応答に results がありません。")
    ranks: list[tuple[int, float]] = []
    for item in results:
        if not isinstance(item, Mapping):
            raise ValueError("rerank の応答の results が不正です。")
        index, score = item.get("index"), item.get("relevance_score")
        if not isinstance(index, int) or not isinstance(score, int | float):
            raise ValueError("rerank の応答に index と relevance_score がありません。")
        ranks.append((index, float(score)))
    return ranks


def _embedding_base_url(settings: object, *, rerank: bool) -> str:
    """埋め込み / 再ランクの OpenAI 互換 base URL（#8 / #45。`embedding_api_base_url`）。

    テストするモデルの接続の Endpoint URL。再ランクは `/compatible-mode/` を `/compatible-api/` に
    読み替える（百煉は再ランクだけ `/compatible-api/v1` 配下で、`/compatible-mode/v1` は 404）。
    """
    return _require_non_empty(
        embedding_api_base_url(settings, rerank=rerank),
        "埋め込み / 再ランクのモデルの接続の Endpoint URL",
    )


def _embedding_api_key(settings: object, *, rerank: bool) -> str:
    """API key（#45。テストするモデルの接続のものを使う）。"""
    return _require_non_empty(
        embedding_model_connection(settings, rerank=rerank).api_key,
        "埋め込み / 再ランクのモデルの接続の API key",
    )


def _embedding_dim(settings: object) -> int:
    """埋め込みの次元（`genai_embedding_dim`。Oracle の VECTOR 列に合わせた値）。"""
    return int(_settings_float_from(settings, "genai_embedding_dim", 0.0))


def _embedding_timeout(settings: object) -> int:
    return max(1, int(_settings_float_from(settings, "enterprise_ai_timeout_seconds", 60.0)))


def _post_openai_compatible(
    url: str, body: Mapping[str, object], api_key: str, timeout: int
) -> dict[str, Any]:
    """埋め込み / 再ランクの要求を 1 回送る（#8）。**HTTP を叩くのはここだけ。**"""
    request = urllib.request.Request(  # noqa: S310 — 設定の Base URL は https を期待する
        url,
        method="POST",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    # HTTPError はそのまま伝播させる（`status` を持つので共有 router が失敗を表示できる）。
    # file:// などを開かないよう、scheme を http(s) に限定する（bandit B310）。
    if request.type not in ("http", "https"):
        raise ValueError(f"URL は http(s) にしてください: {request.type}://…")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # nosec B310
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{url} の応答が object ではありません。")
    return payload


def _require_non_empty(value: str, label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{label} を設定してください。")
    return cleaned


async def _test_database_connection(candidate: Any) -> None:
    """共有 router の接続 hook。Agent は python-oracledb の Thin mode だけで接続する。"""
    await _test_oracle_connection(
        SimpleNamespace(
            oracle_user=_settings_str_from(candidate, "oracle_user"),
            oracle_dsn=_settings_str_from(candidate, "oracle_dsn"),
            oracle_password=_settings_str_from(candidate, "oracle_password"),
            oracle_wallet_dir=_settings_str_from(candidate, "resolved_oracle_wallet_dir"),
            oracle_wallet_password=_settings_str_from(candidate, "oracle_wallet_password"),
            oracle_tcp_connect_timeout_seconds=getattr(
                candidate, "oracle_tcp_connect_timeout_seconds", 10.0
            ),
            oracle_db_test_timeout_seconds=getattr(
                candidate, "oracle_db_test_timeout_seconds", 15.0
            ),
        )
    )


async def _test_oracle_connection(settings: SimpleNamespace) -> None:
    timeout_seconds = float(settings.oracle_db_test_timeout_seconds)
    try:
        with fail_after(timeout_seconds):
            # 時間切れで待つのをやめる（スレッドの接続の待ちは打ち切れない。wallet の接続記述子の
            # 再試行で 1 分以上かかることがある。NL2SQL の asyncio.wait_for と同じ。#1212）。
            await anyio_to_thread.run_sync(
                _test_oracle_connection_sync, settings, abandon_on_cancel=True
            )
    except TimeoutError as exc:
        raise OracleConnectionTimeoutError(
            f"Oracle AI Database の接続テストが {timeout_seconds:g} 秒でタイムアウトしました。"
            "データベースの起動状態、Wallet サービス名、ネットワーク到達性を確認してください。"
        ) from exc


async def _test_database_status_connection(settings: Any) -> None:
    """DB ゲートの接続確認。保存済みの設定の接続を pool から借りて `SELECT 1`（#793）。

    システム設定の「接続テスト」（保存前の候補の値）とは違い、新しい接続を毎回は張らない。
    pool の接続が切れていれば SELECT が失敗し（unreachable）、その接続は pool が捨てる。
    """
    timeout_seconds = _settings_float_from(settings, "oracle_db_test_timeout_seconds", 15.0)
    try:
        with fail_after(timeout_seconds):
            # 時間切れで待つのをやめる（上と同じ。DB の停止中も DB ゲートが応答する。#1212）。
            await anyio_to_thread.run_sync(
                _ping_platform_oracle_sync, settings, abandon_on_cancel=True
            )
    except TimeoutError as exc:
        raise OracleConnectionTimeoutError(
            f"Oracle AI Database の接続確認が {timeout_seconds:g} 秒でタイムアウトしました。"
            "データベースの起動状態、Wallet サービス名、ネットワーク到達性を確認してください。"
        ) from exc


def _ping_platform_oracle_sync(settings: Any) -> None:
    with platform_oracle_connection(settings) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM DUAL")
        cursor.fetchone()


def _test_oracle_connection_sync(settings: SimpleNamespace) -> None:
    oracledb = import_module("oracledb")
    connection = oracledb.connect(**_oracle_connect_kwargs(settings))
    try:
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT 1 FROM DUAL")
            cursor.fetchone()
        finally:
            cursor.close()
    finally:
        connection.close()


def _oracle_connect_kwargs(settings: SimpleNamespace) -> dict[str, object]:
    """接続テストの引数（共通の `app.oracle_connection` と同じ規則。#215）。"""
    return oracle_connect_kwargs(settings)


class OracleConnectionTimeoutError(RuntimeError):
    safe_for_user = True


def _pem_file_is_encrypted(path: Path) -> bool:
    try:
        head = path.read_bytes()[:4096]
    except OSError:
        return False
    text = head.decode("utf-8", errors="ignore").upper()
    return "BEGIN ENCRYPTED PRIVATE KEY" in text or "PROC-TYPE: 4,ENCRYPTED" in text


def _has_pass_phrase(config: Mapping[str, object]) -> bool:
    return any(str(config.get(key, "") or "").strip() for key in PASSPHRASE_CONFIG_KEYS)


def _zip_member_is_symlink(external_attr: int) -> bool:
    mode = external_attr >> 16
    return bool(mode and stat.S_ISLNK(mode))


@router.get("/observability/status", response_model=ApiResponse[ObservabilityStatus])
async def get_observability_status() -> ApiResponse[ObservabilityStatus]:
    settings = get_settings()
    policy = get_trace_policy()
    exporter = trace_exporter_status()
    return ApiResponse(
        data=ObservabilityStatus(
            metrics_enabled=settings.agent_metrics_enabled,
            prometheus_metrics_path="/metrics",
            trace_events_enabled=policy.trace_events_enabled,
            trace_events_buffer_size=policy.trace_events_buffer_size,
            trace_events_retention_seconds=policy.trace_events_retention_seconds,
            trace_sample_rate=policy.trace_sample_rate,
            trace_exporter_configured=exporter.configured,
            trace_exporter_last_success_at=exporter.last_success_at,
            trace_exporter_last_error=exporter.last_error,
            retry_queue_size=exporter.retry_queue_size,
            retry_queue_max_size=exporter.retry_queue_max_size,
            retry_max_attempts=exporter.retry_max_attempts,
            retry_worker_enabled=exporter.retry_worker_enabled,
            retry_worker_running=exporter.retry_worker_running,
            retry_worker_interval_seconds=exporter.retry_worker_interval_seconds,
            langfuse_configured=bool(
                settings.agent_langfuse_host
                and settings.agent_langfuse_public_key
                and settings.agent_langfuse_secret_key
            ),
            opentelemetry_configured=bool(settings.agent_opentelemetry_endpoint),
        )
    )


@router.get("/settings/trace-policy", response_model=ApiResponse[TracePolicySettings])
async def get_trace_policy_settings() -> ApiResponse[TracePolicySettings]:
    return ApiResponse(data=get_trace_policy())


@router.patch("/settings/trace-policy", response_model=ApiResponse[TracePolicySettings])
async def patch_trace_policy_settings(
    patch: TracePolicyPatch,
    _: None = Depends(require_admin),
) -> ApiResponse[TracePolicySettings]:
    try:
        policy = patch_trace_policy(patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApiResponse(data=policy)


@router.get("/observability/events", response_model=ApiResponse[TraceEventsData])
async def get_observability_events(
    request: Request,
    event_type: str | None = None,
    run_id: str | None = None,
    tool_name: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    _: None = Depends(require_auditor),
) -> ApiResponse[TraceEventsData]:
    data = list_trace_events(
        event_type=event_type,
        run_id=run_id,
        tool_name=tool_name,
        limit=limit,
    )
    if _request_agent_ids(request) is not None:
        # 対象範囲が制限された利用者には、範囲内の Run の event だけを返す（#215）。
        allowed_run_ids = {
            run.id
            for run in _filter_runs_for_actor(
                request, await run_in_threadpool(runtime_repository.list_run_summaries)
            )
        }
        events = [event for event in data.events if event.run_id in allowed_run_ids]
        data = TraceEventsData(total=len(events), events=events)
    return ApiResponse(data=data)


@router.post(
    "/observability/export-retry/flush",
    response_model=ApiResponse[TraceExportRetryData],
)
async def flush_observability_export_retry_queue(
    limit: int = Query(default=100, ge=1, le=1000),
    force: bool = False,
    _: None = Depends(require_admin),
) -> ApiResponse[TraceExportRetryData]:
    return ApiResponse(data=flush_trace_export_retry_queue(limit=limit, force=force))


@router.get("/tools", response_model=ApiResponse[ToolDefinitionsData])
async def list_tool_definitions() -> ApiResponse[ToolDefinitionsData]:
    """登録済みツールの v2 schema 一覧を返す。"""
    return ApiResponse(data=ToolDefinitionsData(tools=tool_registry.definitions()))


@router.get("/skills", response_model=ApiResponse[AgentSkillListOutput])
async def list_agent_skills(
    _: None = Depends(require_viewer),
) -> ApiResponse[AgentSkillListOutput]:
    skills = skill_registry.list()
    return ApiResponse(data=AgentSkillListOutput(skills=skills, metadata={"count": len(skills)}))


@router.post("/skills/reload", response_model=ApiResponse[AgentSkillListOutput])
async def reload_agent_skills(
    _: None = Depends(require_admin),
) -> ApiResponse[AgentSkillListOutput]:
    """中立ディレクトリ / JSON 宣言を再読込し、project / env 層を更新する。"""
    counts = reload_declared_skills()
    skills = skill_registry.list()
    return ApiResponse(
        data=AgentSkillListOutput(
            skills=skills, metadata={"count": len(skills), "reloaded": counts}
        )
    )


@router.get("/skills/{skill_id}", response_model=ApiResponse[AgentSkillDefinition])
async def get_agent_skill(
    skill_id: str,
    _: None = Depends(require_viewer),
) -> ApiResponse[AgentSkillDefinition]:
    """単一 skill の詳細(instructions / mcp_requirements)を返す(progressive disclosure)。"""
    skill = skill_registry.get(skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail=SKILL_NOT_FOUND_MESSAGE)
    return ApiResponse(data=skill)


@router.get("/skills/{skill_id}/export", response_class=Response)
async def export_agent_skill(
    skill_id: str,
    _: None = Depends(require_viewer),
) -> Response:
    """スキルを Agent Skills の形（`<name>/SKILL.md` ＋ `references/` の zip）で書き出す（#1874）。

    組み込みのスキルは `skills/<name>/` と同じ内容。外部の Agent（Claude Code など）に入れて使う。
    """
    skill = skill_registry.get(skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail=SKILL_NOT_FOUND_MESSAGE)
    exported = await run_in_threadpool(export_skill, skill)
    return Response(
        exported.zip_bytes(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{exported.name}.zip"'},
    )


@router.post("/skills", response_model=ApiResponse[AgentSkillDefinition])
async def create_agent_skill(
    payload: AgentSkillCreate,
    _: None = Depends(require_admin),
) -> ApiResponse[AgentSkillDefinition]:
    skill_id = payload.id.strip()
    if not skill_id:
        raise HTTPException(status_code=400, detail="スキルの ID を入力してください。")
    if not SKILL_ID_PATTERN.fullmatch(skill_id):
        raise HTTPException(
            status_code=422,
            detail=("スキルの ID は英数字で始め、英数字・_・-・. の 100 文字以内にしてください。"),
        )
    if not payload.name.strip():
        raise HTTPException(status_code=422, detail="スキルの名前を入力してください。")
    if skill_registry.get(skill_id) is not None:
        raise HTTPException(status_code=409, detail="同じ ID のスキルがあります。")
    skill = AgentSkillDefinition(
        id=skill_id,
        name=payload.name,
        description=payload.description,
        instructions=payload.instructions,
        mcp_requirements=payload.mcp_requirements,
        enabled=payload.enabled,
        tags=payload.tags,
        source="runtime",
    )
    try:
        saved = skill_registry.upsert_custom(skill)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _persist(lambda: control_plane_store.save_skill(saved))
    return ApiResponse(data=saved)


@router.patch("/skills/{skill_id}", response_model=ApiResponse[AgentSkillDefinition])
async def patch_agent_skill(
    skill_id: str,
    patch: AgentSkillPatch,
    _: None = Depends(require_admin),
) -> ApiResponse[AgentSkillDefinition]:
    current = skill_registry.get(skill_id)
    if current is None:
        raise HTTPException(status_code=404, detail=SKILL_NOT_FOUND_MESSAGE)
    if current.source != "runtime":
        raise HTTPException(
            status_code=400,
            detail=(
                "組み込み・ファイル・環境変数のスキルは画面から変更できません。"
                "読み込み元の定義を変更してください。"
            ),
        )
    if patch.name is not None and not patch.name.strip():
        raise HTTPException(status_code=422, detail="スキルの名前を入力してください。")
    updated = current.model_copy(
        update={
            "name": current.name if patch.name is None else patch.name,
            "description": (
                current.description if patch.description is None else patch.description
            ),
            "instructions": (
                current.instructions if patch.instructions is None else patch.instructions
            ),
            "mcp_requirements": (
                current.mcp_requirements
                if patch.mcp_requirements is None
                else patch.mcp_requirements
            ),
            "enabled": current.enabled if patch.enabled is None else patch.enabled,
            "tags": current.tags if patch.tags is None else patch.tags,
        }
    )
    try:
        saved = skill_registry.upsert_custom(updated)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _persist(lambda: control_plane_store.save_skill(saved))
    return ApiResponse(data=saved)


@router.delete("/skills/{skill_id}", response_model=ApiResponse[AgentSkillListOutput])
async def delete_agent_skill(
    skill_id: str,
    _: None = Depends(require_admin),
) -> ApiResponse[AgentSkillListOutput]:
    current = skill_registry.get(skill_id)
    if current is None:
        raise HTTPException(status_code=404, detail=SKILL_NOT_FOUND_MESSAGE)
    if current.source != "runtime":
        # 組み込み・宣言のスキルは参照の有無にかかわらず消せない（その理由を先に出す）。
        raise HTTPException(
            status_code=400,
            detail="組み込み・ファイル・環境変数のスキルは画面から削除できません。",
        )
    # 業務 Agent が使っているスキルを消すと、その業務 Agent が保存・公開できなくなり、公開中の版は
    # スキルを欠いて動く。使っていれば断る（#926）。
    users = _agents_using_skill(skill_id)
    if users:
        raise HTTPException(
            status_code=409,
            detail=(
                f"このスキルは業務 Agent（{'、'.join(users)}）が使っています。"
                "業務 Agent のスキルから外してから削除してください。"
            ),
        )
    try:
        skill_registry.remove(skill_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=SKILL_NOT_FOUND_MESSAGE) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _persist(lambda: control_plane_store.delete_skill(skill_id))
    skills = skill_registry.list()
    return ApiResponse(data=AgentSkillListOutput(skills=skills, metadata={"count": len(skills)}))


def _agents_using_skill(skill_id: str) -> list[str]:
    """スキルを下書きか公開中の版で使っている業務 Agent の名前（#926）。"""
    users: list[str] = []
    for agent in runtime_repository.list_agents():
        published = agent.published()
        if skill_id in agent.skill_ids or (
            published is not None and skill_id in published.skill_ids
        ):
            users.append(agent.name or agent.id)
    return users


@router.post("/tools/invoke", response_model=ApiResponse[ToolResult])
async def invoke_tool(
    call: ToolCall,
    request: Request,
    _: None = Depends(require_operator),
) -> ApiResponse[ToolResult]:
    """単発ツール呼び出し。承認が必要な場合は approval_required を返す。

    RAG / NL2SQL の MCP のツールは、呼び出したログイン中の利用者として呼ぶ（#233）。
    """
    context = ToolInvocationContext(
        trace_id=call.trace_id, user_uuid=_run_creator_user_uuid(request)
    )
    return ApiResponse(
        data=await run_in_threadpool(
            tool_registry.invoke, call, policy=_configured_tool_policy(), context=context
        )
    )


def _control_plane_agent(agent_id: str) -> AgentProfile:
    agent = next((item for item in runtime_repository.list_agents() if item.id == agent_id), None)
    if agent is None:
        raise KeyError(agent_id)
    return agent


class BuiltinRuntimeModel(BaseModel):
    model_id: str
    display_name: str


class BuiltinRuntimeStatus(BaseModel):
    """組み込み Runtime の状態（Runtime 画面。API key は出さない。#754）。"""

    id: str
    name: str
    sdk: str
    sdk_version: str
    model_provider: str
    model_id: str
    ready: bool
    error_code: str | None = None
    message: str | None = None
    models: list[BuiltinRuntimeModel] = Field(default_factory=list)


@router.get("/runtime/status", response_model=ApiResponse[BuiltinRuntimeStatus])
async def get_builtin_runtime_status() -> ApiResponse[BuiltinRuntimeStatus]:
    """組み込み Runtime（OpenAI Agents SDK + モデルの API）の SDK の版と使うモデル。

    権限は manifest（実行環境・業務 Agent のメニュー）だけで判定する。Run のデータ・秘密を
    含まないため、Run の capability は求めない（「実行環境」をメニュー権限で開ける。#1041）。
    """
    return ApiResponse(data=BuiltinRuntimeStatus.model_validate(builtin_runtime.runtime_status()))


@router.get("/runtime/storage", response_model=ApiResponse[RuntimeStorageStatus])
async def get_runtime_storage_status() -> ApiResponse[RuntimeStorageStatus]:
    """Run・業務 Agent と画面で変えた定義の保存先（再起動で消えるか。#839）。接続先は返さない。

    権限は manifest（保存先を案内する画面のメニュー）で判定する。
    """
    return ApiResponse(data=await run_in_threadpool(runtime_storage_status))


# 終了した Run（取り消せない。#911）。
_TERMINAL_RUN_STATUSES = frozenset({RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED})

# 実行中の組み込み Runtime の task（GC で消えないよう参照を持つ）。
_builtin_tasks: set[asyncio.Task[None]] = set()


def _schedule_builtin_run(run: RunState) -> None:
    """組み込み Runtime の Run を、このプロセスで実行・再開する（dispatcher のときは何もしない）。

    承認がすべて決まって queued に戻った Run（再開の状態を持つ）は再開し、それ以外は
    最初から実行する。
    本番の別プロセス（`runtime_dispatcher`）は同じ判定で Run を claim する。
    """
    if run.runtime_id != BUILTIN_RUNTIME_ID or run.status != RunStatus.QUEUED:
        return
    if get_settings().agent_runtime_dispatch_mode.strip().lower() != "in_process":
        return
    coroutine = (
        builtin_runtime.resume_run(run.id)
        if builtin_resume_pending(run)
        else builtin_runtime.execute_run(run.id)
    )
    task = asyncio.get_running_loop().create_task(coroutine)
    _builtin_tasks.add(task)
    task.add_done_callback(_builtin_tasks.discard)


def _schedule_nl2sql_job_cancel(before: RunStatus, run: RunState) -> None:
    """取り消した Run が始めた、まだ終わっていない NL2SQL のジョブを止める（#3318）。

    実行中（running）だった Run は、実行している側が Run の終わりに止める（取消の後に届いた
    `nl2sql_query` の結果のジョブも分かる。`builtin_runtime.execute_run`）。ここで扱うのは、
    実行している側の無い Run（承認・回答前の確認の待ち、再開の待ち）だけ。
    """
    if (
        before == RunStatus.RUNNING
        or run.status != RunStatus.CANCELLED
        or run.runtime_id != BUILTIN_RUNTIME_ID
    ):
        return
    task = asyncio.get_running_loop().create_task(builtin_runtime.cancel_run_nl2sql_jobs(run.id))
    _builtin_tasks.add(task)
    task.add_done_callback(_builtin_tasks.discard)


# 実行履歴・承認の一覧のページング（`limit` / `offset`。上限を超えると 422。#1266・#1897）。
_runs_paging = offset_params(default=DEFAULT_PAGE_LIMIT, max_limit=100)
_approvals_paging = offset_params(default=DEFAULT_PAGE_LIMIT, max_limit=100)

# 実行履歴の状態の絞り込み。`active` は実行中・待機中（queued / running / waiting_confirmation）、
# `waiting` は人の応答待ち（承認待ち・回答前の確認待ち。画面の共通の状態「応答待ち」。#1955）。
RunStatusFilter = Literal[
    "active",
    "waiting",
    "queued",
    "running",
    "waiting_approval",
    "waiting_confirmation",
    "completed",
    "failed",
    "cancelled",
]
_ACTIVE_RUN_STATUSES = frozenset(
    {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_CONFIRMATION}
)
_WAITING_RUN_STATUSES = frozenset({RunStatus.WAITING_APPROVAL, RunStatus.WAITING_CONFIRMATION})
# 実行履歴の種類（きっかけ。`run_facts.run_source`）の絞り込み（#1955）。`not_evaluation` は
# 品質評価の Run（`source = "evaluation"`）を除く（画面の既定「種類: 品質評価以外」。#1983）。
RunKindFilter = Literal["user", "evaluation", "mcp", "replay", "not_evaluation"]
_EVALUATION_RUN_SOURCE = "evaluation"
# 承認の状態の絞り込み。`decided` は判断済み（承認・却下・取り消し）。
ApprovalStatusFilter = Literal["pending", "decided"]


def _search_terms(query: str | None) -> list[str]:
    """検索語（空白区切り）。大文字・小文字と全角・半角は区別しない（画面の検索と同じ）。"""
    return unicodedata.normalize("NFKC", query or "").lower().split()


def _matches_terms(terms: list[str], fields: Iterable[str | None]) -> bool:
    if not terms:
        return True
    haystack = unicodedata.normalize("NFKC", "\n".join(field for field in fields if field)).lower()
    return all(term in haystack for term in terms)


def _run_status_matches(summary: RunSummary, status: RunStatusFilter | None) -> bool:
    if status is None:
        return True
    if status == "active":
        return summary.status in _ACTIVE_RUN_STATUSES
    if status == "waiting":
        return summary.status in _WAITING_RUN_STATUSES
    return summary.status.value == status


def _run_kind_matches(summary: RunSummary, kind: RunKindFilter | None) -> bool:
    if kind is None:
        return True
    if kind == "not_evaluation":
        return summary.source != _EVALUATION_RUN_SOURCE
    return summary.source == kind


def _run_since_matches(summary: RunSummary, since: datetime | None) -> bool:
    """期間の絞り込み（`since` 以後に始めた Run）。タイムゾーンの無い値は UTC とみなす。"""
    if since is None:
        return True
    bound = since if since.tzinfo is not None else since.replace(tzinfo=UTC)
    started = (
        summary.created_at
        if summary.created_at.tzinfo is not None
        else summary.created_at.replace(tzinfo=UTC)
    )
    return started >= bound


@router.get("/runs", response_model=ApiResponse[RunsData])
async def list_runs(
    request: Request,
    paging: Annotated[OffsetParams, Depends(_runs_paging)],
    status: RunStatusFilter | None = None,
    q: str | None = Query(default=None, max_length=500),
    executor: RunExecutorFilter = DEFAULT_RUN_EXECUTOR_FILTER,
    kind: RunKindFilter | None = None,
    since: datetime | None = None,
    _: None = Depends(require_viewer),
) -> ApiResponse[RunsData]:
    """実行履歴の一覧（新しい順・サーバー側のページング。#1897）。

    行は一覧に要る項目だけの要約（`RunSummary`）。step・Event・成果物は詳細
    （`GET /api/runs/{run_id}`）で読む。検索は目標・実行 ID・業務 Agent の ID と名前。
    system_admin は全員の Run（実行した利用者の表示名つき）、それ以外は自分が実行した Run と、
    自分の会話で他の人（system_admin）が実行した Run。どちらも業務 Agent の対象範囲で絞る
    （#1889・#1928・#1954）。`executor` は「すべて / 自分が実行 / 他の人が実行」（件数も同じ条件）。
    各行に実行した人の表示名・自分が実行したか・待ちの操作をできるかを付ける（#1954）。
    `kind` は種類（きっかけ）、`since` は期間（その日時以後に始めた Run）。`has_other_executors` は
    他の人が実行した Run を 1 件でも見られるか（画面の「実行者」の列と絞り込みを出す。#1955）。
    `kind=not_evaluation`（画面の既定「品質評価以外」）のときと、評価以外の 1 つの種類のときは、
    `unfiltered_total`（「全 N 件」）も品質評価の Run を除いて数える（既定の範囲で数える。#1983）。
    `evaluation_total` は見られる品質評価の Run の数（既定で隠れている数）。
    """
    runs = _ai_use_runs_for_actor(
        request, await run_in_threadpool(runtime_repository.list_run_summaries)
    )
    principal = _request_principal(request)
    terms = _search_terms(q)
    agent_names = (
        {agent.id: agent.name for agent in await run_in_threadpool(runtime_repository.list_agents)}
        if terms
        else {}
    )
    matched = [
        run
        for run in runs
        if _run_status_matches(run, status)
        and _run_kind_matches(run, kind)
        and _run_since_matches(run, since)
        and (
            principal is None or run_executor_matches(principal, run.created_by_user_uuid, executor)
        )
        and _matches_terms(terms, (run.goal, run.id, run.agent_id, agent_names.get(run.agent_id)))
    ]
    evaluation_total = sum(1 for run in runs if run.source == _EVALUATION_RUN_SOURCE)
    page = paginate_slice(matched, paging)
    names = await _run_people_names(page.items)
    creators: dict[str, str] = {}
    if _sees_all_users(request):
        creators = {
            run.created_by_user_uuid: names[run.created_by_user_uuid]
            for run in page.items
            if run.created_by_user_uuid and names.get(run.created_by_user_uuid)
        }
    return ApiResponse(
        data=RunsData(
            items=[
                run.model_copy(
                    update=_run_viewer(request, run, names).model_dump(
                        include={"executed_by_me", "executor_display_name", "can_decide"}
                    )
                )
                for run in page.items
            ],
            total=page.total,
            limit=page.limit,
            offset=page.offset,
            has_next=page.has_next,
            unfiltered_total=(
                len(runs)
                if kind in (None, _EVALUATION_RUN_SOURCE)
                else len(runs) - evaluation_total
            ),
            evaluation_total=evaluation_total,
            creator_display_names=creators,
            has_other_executors=_sees_all_users(request)
            or (
                principal is not None
                and any(
                    run_executor_matches(principal, run.created_by_user_uuid, "others")
                    for run in runs
                )
            ),
        )
    )


@router.get("/approvals", response_model=ApiResponse[ApprovalsData])
async def list_approvals(
    request: Request,
    paging: Annotated[OffsetParams, Depends(_approvals_paging)],
    status: ApprovalStatusFilter | None = None,
    q: str | None = Query(default=None, max_length=500),
    _: None = Depends(require_viewer),
) -> ApiResponse[ApprovalsData]:
    """承認の一覧（申請の新しい順・サーバー側のページング。#1897）。

    対象は実行履歴と同じ Run（業務 Agent の対象範囲と、system_admin でなければ自分の Run。
    #1889）の承認。検索は承認 ID・ツール名・目標・実行 ID。
    """
    summaries = await run_in_threadpool(runtime_repository.list_approval_summaries)
    approvals = [
        approval
        for approval in _filter_runs_for_actor(request, summaries)
        if _run_approvable_by(request, approval)
    ]
    terms = _search_terms(q)
    matched = [
        approval
        for approval in approvals
        if (status is None or (approval.status == ApprovalStatus.PENDING) == (status == "pending"))
        and _matches_terms(
            terms, (approval.id, approval.tool_name, approval.run_goal, approval.run_id)
        )
    ]
    page = paginate_slice(matched, paging)
    return ApiResponse(
        data=ApprovalsData(
            items=page.items,
            total=page.total,
            limit=page.limit,
            offset=page.offset,
            has_next=page.has_next,
            unfiltered_total=len(approvals),
        )
    )


@router.get("/approvals/{approval_id}", response_model=ApiResponse[ApprovalDetailData])
async def get_approval(
    approval_id: str,
    request: Request,
    _: None = Depends(require_viewer),
) -> ApiResponse[ApprovalDetailData]:
    """承認の詳細（承認と、その Run の要約。#1897）。

    使えない業務 Agent の承認と、system_admin でなければほかの利用者の Run の承認は 404（#1889）。
    """
    try:
        run = await run_in_threadpool(runtime_repository.get_run_for_approval, approval_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=APPROVAL_NOT_FOUND_MESSAGE) from exc
    if not _filter_runs_for_actor(request, [run]) or not _run_approvable_by(request, run):
        raise HTTPException(status_code=404, detail=APPROVAL_NOT_FOUND_MESSAGE)
    approval = next(item for item in run.approvals if item.id == approval_id)
    names = (
        await run_in_threadpool(login_display_names, [approval.decided_by])
        if approval.decided_by
        else {}
    )
    return ApiResponse(
        data=ApprovalDetailData(approval=approval, run=run_summary(run), user_display_names=names)
    )


@router.post("/runs", response_model=ApiResponse[RunState])
async def create_run(
    run_request: RunCreateRequest,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    _: None = Depends(require_operator),
) -> ApiResponse[RunState]:
    try:
        _require_agent_access(request, run_request.agent_id)
        # Control Plane の予約の metadata（再開の状態・評価・MCP の印）は
        # 利用者に付けさせない（#1130）。
        reserved = reserved_run_metadata_keys(run_request.metadata)
        if reserved:
            raise HTTPException(
                status_code=422,
                detail=(
                    "metadata に Control Plane が使う項目は指定できません: " + ", ".join(reserved)
                ),
            )
        # 旧エンジンの v1 の Run（`X-Agent-API-Version: 1`）は #756 で削除した。
        agent = _control_plane_agent(run_request.agent_id)
        if not agent.enabled:
            raise HTTPException(
                status_code=409,
                detail={"code": "agent_disabled", "message": "無効な業務 Agent は実行できません。"},
            )
        # 下書きで試すのは Agent 管理か、個人の Agent の持ち主だけ（#770・#2523）。
        if run_request.draft and not _policy_manages_agent(_actor_policy(request), agent):
            raise HTTPException(status_code=403, detail=DRAFT_RUN_FORBIDDEN_MESSAGE)
        # 続きの質問は会話の持ち主だけ（system_admin でも他の利用者の会話は 403。#3244）。
        if run_request.thread_id is not None and not await run_in_threadpool(
            _may_continue_thread, request, run_request.thread_id
        ):
            _raise_others_content_forbidden()
        # 添付（#2517）は Run を作る前に確かめ（見えない添付は 404・別の会話の添付は 409）、会話が
        # 決まった後に会話へ結び付ける（新しい会話の ID は Run を作るときに決まる）。
        attachments: list[ChatAttachmentReference] = []
        if run_request.attachment_ids:
            attachments = await run_in_threadpool(
                chat_attachment_service().prepare_attachments,
                _attachment_principal(request),
                run_request.attachment_ids,
                conversation_id=run_request.thread_id,
            )
        # 実行は組み込み Runtime（OpenAI Agents SDK + モデルの API。#754）。Run は操作した
        # 利用者として実行し、続ける会話は自分の会話だけ（他の利用者の会話は保存先も 404。#3244）。
        run = await run_in_threadpool(
            runtime_repository.create_builtin_run,
            run_request,
            created_by_user_uuid=_run_creator_user_uuid(request),
            attachments=attachments,
        )
        if attachments and run.thread_id:
            await run_in_threadpool(
                chat_attachment_service().bind_references,
                attachments,
                conversation_id=run.thread_id,
                owner_user_uuid=run.thread_owner_user_uuid or run.created_by_user_uuid or "",
            )
        _schedule_builtin_run(run)
        return ApiResponse(data=run)
    except AgentNotPublishedError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "agent_unpublished",
                "message": (
                    "公開していない業務 Agent は実行できません。"
                    "公開するか、下書きで実行してください。"
                ),
            },
        ) from exc
    except ThreadKindMismatchError as exc:
        # 会話は始めた Run の種類（下書き / 公開中の版）に固定する（#2515）。
        raise HTTPException(
            status_code=409,
            detail={
                "code": "thread_kind_mismatch",
                "message": (
                    "下書きで試した会話は公開中の版で、公開中の版の会話は下書きで続けられません。"
                    "新しい会話で質問してください。"
                ),
            },
        ) from exc
    except ThreadNotFoundError as exc:
        raise HTTPException(status_code=404, detail="会話が見つかりません。") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=AGENT_NOT_FOUND_MESSAGE) from exc
    except ValidationError:
        # 保存した値（添付など）を読めないのは業務 Agent の状態ではない。409 の「業務 Agent の
        # 状態を確認」にせず、想定外のエラーとして返す（#2548）。
        raise
    except ValueError as exc:
        # repository の内部の文（英語）は本文に出さず、「詳細」（error_details.reason）に残す。
        logger.warning(
            "run_create_rejected",
            extra={"agent_id": run_request.agent_id, "reason": str(exc)},
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "run_not_created",
                "message": (
                    "この業務 Agent では今は実行できません。業務 Agent の状態を確認してから、"
                    "もう一度実行してください。"
                ),
                "reason": str(exc),
            },
        ) from exc


_threads_paging = offset_params(default=DEFAULT_PAGE_LIMIT, max_limit=100)


@router.get("/threads", response_model=ApiResponse[ThreadsData])
async def list_threads(
    request: Request,
    paging: Annotated[OffsetParams, Depends(_threads_paging)],
    agent_id: str | None = None,
    draft: bool = False,
) -> ApiResponse[ThreadsData]:
    """会話の一覧（チャット。#768）。使えなくなった Agent の会話は出さない。

    既定は公開中の版の会話、`draft=true` は業務 Agent の編集画面で下書きを試した会話（#2515。
    下書きで実行できる Agent 管理だけ）。

    system_admin は全員の会話（利用者の表示名つき）、それ以外はログイン中の利用者の会話だけ
    （#1889）。会話の履歴は 3 製品と同じく、サーバー側でページングする
    （新しい順、共通の `offset_params` / `Page`。#1265 / #1266）。最後の更新が会話の履歴の期間
    （共通の設定 `PLATFORM_CHAT_HISTORY_DAYS`）の中の会話だけを、件数と 1 ページの両方で返す
    （期間の外の会話も `GET /threads/{thread_id}`・実行の詳細の「会話を開く」では開ける。#2340）。
    """
    policy = _actor_policy(request)
    if draft and "admin" not in policy.roles and not policy.can_build:
        raise HTTPException(
            status_code=403,
            detail=(
                "下書きの会話を見られるのは、Agent 管理か、自分の業務 Agent の作成の権限がある"
                "利用者だけです。"
            ),
        )
    viewer = _run_creator_user_uuid(request)
    all_users = _sees_all_users(request)
    history_days = chat_history_days(get_settings())
    updated_since = chat_history_cutoff(history_days)
    threads = [
        thread
        for thread in await run_in_threadpool(
            runtime_repository.list_threads,
            user_uuid=viewer,
            agent_id=agent_id,
            all_users=all_users,
            draft=draft,
        )
        if _policy_allows_agent(policy, thread.agent_id)
        and within_chat_history(thread.updated_at, updated_since)
    ]
    page = paginate_slice(threads, paging)
    owners = sorted({item.owner_user_uuid for item in page.items if item.owner_user_uuid})
    names = await run_in_threadpool(user_display_names, owners) if all_users and owners else {}
    return ApiResponse(
        data=ThreadsData(
            items=[
                item.model_copy(
                    update={
                        "owned": item.owner_user_uuid == viewer,
                        "actor_display_name": names.get(item.owner_user_uuid or "", ""),
                    }
                )
                for item in page.items
            ],
            total=page.total,
            limit=page.limit,
            offset=page.offset,
            has_next=page.has_next,
            history_days=history_days,
        )
    )


@router.get("/threads/{thread_id}", response_model=ApiResponse[ThreadView])
async def get_thread(thread_id: str, request: Request) -> ApiResponse[ThreadView]:
    """会話の Run（古い順）。持ち主と system_admin だけが読める（ほかは 404。#1889）。

    持ち主以外（system_admin）が送った往復には、送った利用者の表示名を添える（#1928）。各 Run には
    実行した人・自分が実行したか・待ちの操作をできるか（`RunViewer`）を付ける。画面は、持ち主が
    操作できない待ち（system_admin が実行した Run の承認・確認）に、待っている相手を出す（#1954）。
    """
    viewer = _run_creator_user_uuid(request)
    all_users = _sees_all_users(request)
    try:
        thread = await run_in_threadpool(
            runtime_repository.get_thread, thread_id, user_uuid=viewer, all_users=all_users
        )
    except ThreadNotFoundError as exc:
        raise HTTPException(status_code=404, detail="会話が見つかりません。") from exc
    _require_agent_access(request, thread.agent_id)
    owner = thread.owner_user_uuid
    senders = {
        run.id: run.created_by_user_uuid for run in thread.runs if run.created_by_user_uuid != owner
    }
    names = await _run_people_names(thread.runs)
    views = await _evidence_checked_views(request, thread.runs, names)
    return ApiResponse(
        data=ThreadView(
            thread_id=thread.thread_id,
            agent_id=thread.agent_id,
            runs=views,
            owner_user_uuid=owner,
            owned=owner == viewer,
            actor_display_name=names.get(owner or "", "") if all_users else "",
            sender_display_names={
                run_id: names.get(uuid or "", "") for run_id, uuid in senders.items()
            },
        )
    )


@router.patch("/threads/{thread_id}", response_model=ApiResponse[ThreadSummary])
async def rename_thread(
    thread_id: str, update: ThreadUpdateRequest, request: Request
) -> ApiResponse[ThreadSummary]:
    """会話の名前を変える（#1817）。会話の持ち主だけ。

    system_admin は他の利用者の会話を見られるが、名前は変えられない（403。#3244）。ほかの利用者の
    会話は 404（#1889）。
    """
    user_uuid = _run_creator_user_uuid(request)
    all_users = _sees_all_users(request)
    try:
        # 変更は保存先（Oracle の checkpoint）への書き込みを lock の中で待つ。event loop を
        # 塞がないよう threadpool で呼ぶ（#1935）。
        thread = await run_in_threadpool(
            runtime_repository.get_thread, thread_id, user_uuid=user_uuid, all_users=all_users
        )
        _require_agent_access(request, thread.agent_id)
        if not can_add_user_content(_request_principal(request), thread.owner_user_uuid):
            _raise_others_content_forbidden()
        summary = await run_in_threadpool(
            runtime_repository.rename_thread,
            thread_id,
            user_uuid=user_uuid,
            title=update.title,
        )
    except ThreadNotFoundError as exc:
        raise HTTPException(status_code=404, detail="会話が見つかりません。") from exc
    owner = summary.owner_user_uuid
    names = await run_in_threadpool(user_display_names, [owner]) if all_users and owner else {}
    return ApiResponse(
        data=summary.model_copy(
            update={"owned": owner == user_uuid, "actor_display_name": names.get(owner or "", "")}
        )
    )


def _delete_thread_attachments(thread_id: str) -> None:
    try:
        chat_attachment_service().delete_conversation(thread_id)
    except Exception as exc:  # noqa: BLE001 - 会話の削除は止めない
        logger.warning(
            "agent_thread_attachments_delete_failed",
            extra={"thread_id": thread_id, "error_type": type(exc).__name__},
        )


@router.delete("/threads/{thread_id}", response_model=ApiResponse[None])
async def delete_thread(thread_id: str, request: Request) -> ApiResponse[None]:
    """会話を消す（#1817）。作った利用者と system_admin だけ（ほかは 404。#1889）。

    Run は監査・フィードバック・評価のために残し、会話からの参照だけを外す。回答の作成中・
    承認待ちの Run がある会話は 409（止めてから消す）。
    """
    user_uuid = _run_creator_user_uuid(request)
    all_users = _sees_all_users(request)
    try:
        # 削除は会話の Run を書き換えて保存先（Oracle の checkpoint）へ書くまで lock を持つ。
        # 書き込みに数秒〜十数秒かかることがあり、event loop を塞ぐとほかの要求（作成中の回答の
        # stream など）も止まるため threadpool で呼ぶ（#1935）。
        thread = await run_in_threadpool(
            runtime_repository.get_thread, thread_id, user_uuid=user_uuid, all_users=all_users
        )
        _require_agent_access(request, thread.agent_id)
        await run_in_threadpool(
            runtime_repository.delete_thread,
            thread_id,
            user_uuid=user_uuid,
            all_users=all_users,
        )
        # 会話の添付も消す（#2517。消せなくても期限で消える）。
        await run_in_threadpool(_delete_thread_attachments, thread_id)
    except ThreadNotFoundError as exc:
        raise HTTPException(status_code=404, detail="会話が見つかりません。") from exc
    except ThreadBusyError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "回答の作成中・承認待ちの会話は削除できません。"
                "停止するか、回答が終わってから削除してください。"
            ),
        ) from exc
    return ApiResponse(data=None)


@router.put("/runs/{run_id}/feedback", response_model=ApiResponse[RunState])
async def put_run_feedback(
    run_id: str,
    feedback: RunFeedbackRequest,
    request: Request,
    _: None = Depends(require_operator),
) -> ApiResponse[RunState]:
    """チャットの回答への評価（#774）。

    Run の持ち主（会話の持ち主。会話の無い Run は実行した利用者）が付け、付け直すと上書きする。
    評価した利用者を `feedback.user_uuid` に残す。system_admin は他の利用者の Run を読めるが、評価は
    付けられない（403。#3244）。ほかの利用者の Run は 404。
    """
    run = _readable_run(request, run_id)
    if not _run_content_addable_by(request, run):
        _raise_others_content_forbidden()
    user_uuid = _run_creator_user_uuid(request)
    if user_uuid is None:
        raise HTTPException(status_code=401, detail="ログインしてください。")
    try:
        updated = await run_in_threadpool(
            runtime_repository.set_run_feedback,
            run_id,
            RunFeedback(
                rating=feedback.rating,
                reason=feedback.reason,
                comment=feedback.comment,
                user_uuid=user_uuid,
            ),
        )
    except RunNotRatableError as exc:
        raise HTTPException(
            status_code=409, detail="回答が出た Run にだけフィードバックを付けられます。"
        ) from exc
    return ApiResponse(data=updated)


@router.put("/runs/{run_id}/admin-review", response_model=ApiResponse[RunState])
async def put_run_admin_review(
    run_id: str,
    review: RunFeedbackRequest,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse[RunState]:
    """管理者の評価（#774）。Agent 管理の権限でだれの回答にも付けられ、本人の評価とは別に残す。"""
    try:
        run = await run_in_threadpool(runtime_repository.get_run, run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=RUN_NOT_FOUND_MESSAGE) from exc
    _require_agent_access(request, run.agent_id)
    try:
        updated = await run_in_threadpool(
            runtime_repository.set_run_feedback,
            run_id,
            RunFeedback(
                rating=review.rating,
                reason=review.reason,
                comment=review.comment,
                user_uuid=_run_creator_user_uuid(request),
            ),
            admin=True,
        )
    except RunNotRatableError as exc:
        raise HTTPException(
            status_code=409, detail="回答が出た Run にだけ評価を付けられます。"
        ) from exc
    return ApiResponse(data=updated)


# 一覧のページング（`limit` / `offset`。上限を超えると 422。#1266）。
_feedback_paging = offset_params(default=FEEDBACK_PAGE_SIZE, max_limit=FEEDBACK_PAGE_SIZE_MAX)


@router.get("/feedback", response_model=ApiResponse[FeedbackReport])
async def get_feedback_report(
    request: Request,
    paging: Annotated[OffsetParams, Depends(_feedback_paging)],
    days: int = Query(default=30),
    agent_id: str | None = Query(default=None, max_length=200),
    rating: FeedbackRating | None = None,
    reason: FeedbackReason | None = None,
) -> ApiResponse[FeedbackReport]:
    """フィードバックの集計と一覧（#774）。権限は middleware のメニュー権限（`menu.feedback`）。

    対象は Run の一覧と同じく利用できる業務 Agent の Run だけ。Oracle の構成は保存した Run の
    事実（`AGENT_RUN_FACTS`）を SQL で集計し、一覧はサーバー側でページングする（#794）。
    """
    if days not in FEEDBACK_PERIOD_DAYS:
        raise HTTPException(status_code=422, detail=_PERIOD_DETAIL)
    now = datetime.now(UTC)
    agent_names = {
        agent.id: agent.name for agent in await run_in_threadpool(runtime_repository.list_agents)
    }
    store = run_facts_store.reporting_store()
    if store is not None:
        try:
            return ApiResponse(
                data=await run_in_threadpool(
                    store.feedback_report,
                    days=days,
                    now=now,
                    agent_ids=_report_agent_scope(request),
                    agent_id=agent_id or None,
                    rating=rating,
                    reason=reason,
                    offset=paging.offset,
                    limit=paging.limit,
                    agent_names=agent_names,
                    user_names=user_display_names,
                )
            )
        except run_facts_store.RunFactsUnavailableError:
            logger.warning("agent_run_facts_table_missing", extra={"report": "feedback"})
        except Exception:  # noqa: BLE001 - DB の障害でも画面を出す（メモリの Run を集計する）
            logger.exception("agent_run_facts_report_failed", extra={"report": "feedback"})
    runs = _filter_runs_for_actor(request, await run_in_threadpool(runtime_repository.list_runs))
    report = await run_in_threadpool(
        build_feedback_report,
        [fact_from_run(run) for run in runs],
        days=days,
        now=now,
        agent_id=agent_id or None,
        rating=rating,
        reason=reason,
        agent_names=agent_names,
        user_names=user_display_names,
        offset=paging.offset,
        limit=paging.limit,
    )
    return ApiResponse(data=report)


_signal_paging = offset_params(default=SIGNAL_PAGE_SIZE, max_limit=SIGNAL_PAGE_SIZE_MAX)


@router.get("/feedback/signals", response_model=ApiResponse[SignalReport])
async def get_feedback_signals(
    request: Request,
    paging: Annotated[OffsetParams, Depends(_signal_paging)],
    days: int = Query(default=30),
    agent_id: str | None = Query(default=None, max_length=200),
    kind: SignalKind | None = None,
) -> ApiResponse[SignalReport]:
    """確かめるべき実行（暗黙のシグナル。#1822）。権限はフィードバックと同じ（`menu.feedback`）。

    評価の付いていない Run も、回答の検証の不合格・答えられなかった回答・承認の却下・ツールの
    失敗・実行の失敗から集める。対象は利用できる業務 Agent の Run だけ。Oracle の構成は保存した
    Run の事実（`AGENT_RUN_FACTS`）を SQL で集計し、一覧はサーバー側でページングする。
    """
    if days not in FEEDBACK_PERIOD_DAYS:
        raise HTTPException(status_code=422, detail=_PERIOD_DETAIL)
    now = datetime.now(UTC)
    agent_names = {
        agent.id: agent.name for agent in await run_in_threadpool(runtime_repository.list_agents)
    }
    store = run_facts_store.reporting_store()
    if store is not None:
        try:
            return ApiResponse(
                data=await run_in_threadpool(
                    store.signal_report,
                    days=days,
                    now=now,
                    agent_ids=_report_agent_scope(request),
                    agent_id=agent_id or None,
                    kind=kind,
                    offset=paging.offset,
                    limit=paging.limit,
                    agent_names=agent_names,
                    user_names=user_display_names,
                )
            )
        except run_facts_store.RunFactsUnavailableError:
            logger.warning("agent_run_facts_table_missing", extra={"report": "signals"})
        except Exception:  # noqa: BLE001 - DB の障害・未更新の列でも画面を出す（メモリの Run を集計）
            logger.exception("agent_run_facts_report_failed", extra={"report": "signals"})
    runs = _filter_runs_for_actor(request, await run_in_threadpool(runtime_repository.list_runs))
    report = await run_in_threadpool(
        build_signal_report,
        [fact_from_run(run) for run in runs],
        days=days,
        now=now,
        agent_id=agent_id or None,
        kind=kind,
        agent_names=agent_names,
        user_names=user_display_names,
        offset=paging.offset,
        limit=paging.limit,
    )
    return ApiResponse(data=report)


_PERIOD_DETAIL = (
    "期間は "
    + "・".join(str(days) for days in FEEDBACK_PERIOD_DAYS)
    + " 日のどれかにしてください。"
)


def _report_agent_scope(request: Request) -> frozenset[str] | None:
    """集計の対象の業務 Agent（None は制限なし。Run の一覧と同じ判定）。"""
    allowed = _request_agent_ids(request)
    return None if allowed is None else frozenset(allowed)


@router.get("/runs/{run_id}", response_model=ApiResponse[RunView])
async def get_run(
    run_id: str,
    request: Request,
    _: None = Depends(require_viewer),
) -> ApiResponse[RunView]:
    """Run の詳細（実行した利用者・会話の持ち主・system_admin だけ。ほかは 404。#1889・#1928）。

    実行者・会話の持ち主の表示名と、待ちの操作をできるか（`RunViewer`。#1954）を付ける。
    """
    run = _readable_run(request, run_id)
    names = await _run_people_names([run])
    [view] = await _evidence_checked_views(request, [run], names)
    return ApiResponse(data=view)


@router.get("/runs/{run_id}/audit", response_model=ApiResponse[RunAuditData])
async def get_run_audit(
    run_id: str,
    request: Request,
    _: None = Depends(require_auditor),
) -> ApiResponse[RunAuditData]:
    """実行履歴の詳細の監査（Run の詳細と同じく、実行した利用者と system_admin だけ。#1889）。"""
    return ApiResponse(data=_run_audit_data(_readable_run(request, run_id)))


@router.get("/agent-templates", response_model=ApiResponse[AgentTemplatesData])
async def list_agent_templates() -> ApiResponse[AgentTemplatesData]:
    """業種テンプレート（#780）。業務 Agent の新規作成の画面がフォームに入れる出発点。"""
    return ApiResponse(data=AgentTemplatesData(templates=list(AGENT_TEMPLATES)))


# 実行中の評価の task（GC で消えないよう参照を持つ。#776）。
_evaluation_tasks: set[asyncio.Task[None]] = set()
_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _evaluation_set_for_actor(request: Request, set_id: str) -> EvaluationSet:
    try:
        item = evaluation_set_store.get(set_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="評価セットが見つかりません。") from exc
    if not _agent_allowed(request, item.agent_id):
        raise HTTPException(status_code=404, detail="評価セットが見つかりません。")
    return item


def _require_evaluable_agent(request: Request, agent_id: str) -> AgentProfile:
    _require_agent_access(request, agent_id)
    try:
        return _control_plane_agent(agent_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="業務 Agent が見つかりません。") from exc


def _require_case_sources(cases: list[EvaluationCase], agent_id: str) -> None:
    """ケースの出どころ（`source_run_id`）は、同じ業務 Agent の Run だけ（#810）。"""
    for case in cases:
        if not case.source_run_id:
            continue
        try:
            source = runtime_repository.get_run(case.source_run_id)
        except KeyError as exc:
            raise HTTPException(status_code=422, detail="元の実行が見つかりません。") from exc
        if source.agent_id != agent_id:
            raise HTTPException(
                status_code=422, detail="元の実行と評価セットの業務 Agent が違います。"
            )


_CASE_DUPLICATE_DETAIL = "同じ質問の評価ケースが、この評価セットに既にあります。"
_SET_FULL_DETAIL = (
    f"評価セットのケースは {EVALUATION_MAX_CASES} 件までです。別の評価セットに追加してください。"
)


def _run_tool_names(run: RunState) -> list[str]:
    """Run が呼んだ（呼ぼうとした）ツールの名前（重複を除いた呼んだ順。上限は評価ケースと同じ）。"""
    names = [step.tool_call.name for step in run.steps if step.tool_call is not None]
    return list(dict.fromkeys(names))[:EVALUATION_EXPECTED_TOOLS_MAX]


@router.get("/runs/{run_id}/evaluation-case", response_model=ApiResponse[EvaluationCaseDraft])
async def get_run_evaluation_case(
    run_id: str, request: Request
) -> ApiResponse[EvaluationCaseDraft]:
    """Run（フィードバック・Run の詳細）から評価ケースの下書きを作る（保存しない。#810）。

    質問は Run のゴール、期待する回答の要点は管理者の評価のコメント（無ければ空）、期待するツールは
    Run が呼んだツール。権限は middleware のメニュー権限（`menu.evaluation`）。
    """
    try:
        run = await run_in_threadpool(runtime_repository.get_run, run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="実行が見つかりません。") from exc
    if not _agent_allowed(request, run.agent_id):
        raise HTTPException(status_code=404, detail="実行が見つかりません。")
    agent = _require_evaluable_agent(request, run.agent_id)
    review = run.admin_review
    return ApiResponse(
        data=EvaluationCaseDraft(
            agent_id=agent.id,
            agent_name=agent.name,
            source_run_id=run.id,
            question=run.goal.strip(),
            expected=(review.comment.strip() if review is not None else "")[
                :EVALUATION_EXPECTED_MAX_CHARS
            ],
            expected_tools=_run_tool_names(run),
            expected_output=recorded_output_rows(run),
            existing_set_ids=list(evaluation_set_store.sets_with_question(agent.id, run.goal)),
        )
    )


@router.post("/evaluation-sets/{set_id}/cases", response_model=ApiResponse[EvaluationSet])
async def append_evaluation_case(
    set_id: str, case: EvaluationCase, request: Request
) -> ApiResponse[EvaluationSet]:
    """評価セットにケースを 1 件足す（フィードバックから追加するとき。#810）。

    同じ質問は 409、上限（50 件）は 409。`source_run_id` の Run は同じ業務 Agent のものだけ。
    """
    item = _evaluation_set_for_actor(request, set_id)
    _require_evaluable_agent(request, item.agent_id)
    _require_case_sources([case], item.agent_id)
    try:
        updated = evaluation_set_store.append_case(item.id, case)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="評価セットが見つかりません。") from exc
    except EvaluationCaseDuplicateError as exc:
        raise HTTPException(status_code=409, detail=_CASE_DUPLICATE_DETAIL) from exc
    except EvaluationSetFullError as exc:
        raise HTTPException(status_code=409, detail=_SET_FULL_DETAIL) from exc
    except ControlPlaneStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ApiResponse(data=updated)


@router.post("/evaluation-sets/from-template", response_model=ApiResponse[EvaluationSet])
async def create_evaluation_set_from_template(
    payload: EvaluationSetFromTemplateRequest, request: Request
) -> ApiResponse[EvaluationSet]:
    """業務 Agent の作成に使った業種テンプレートの評価ケースで評価セットを作る（#810）。"""
    agent = _require_evaluable_agent(request, payload.agent_id)
    template = find_template(agent.template_id) if agent.template_id else None
    if template is None or not template.evaluation_cases:
        raise HTTPException(
            status_code=409,
            detail="この業務 Agent はテンプレートから作っていないため、評価ケースがありません。",
        )
    data = EvaluationSetInput(
        agent_id=agent.id,
        name=f"{template.name}（テンプレート）"[:100],
        description="業種テンプレートの評価ケースから作りました。",
        cases=[
            EvaluationCase(question=case.question, expected=case.expected)
            for case in template.evaluation_cases[:EVALUATION_MAX_CASES]
        ],
    )
    try:
        created = evaluation_set_store.create(data, created_by=_run_creator_user_uuid(request))
    except ControlPlaneStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ApiResponse(data=created)


@router.get("/evaluation-sets", response_model=ApiResponse[EvaluationSetsData])
async def list_evaluation_sets(
    request: Request, agent_id: str | None = Query(default=None, max_length=200)
) -> ApiResponse[EvaluationSetsData]:
    """評価セット（#776。利用できる業務 Agent のものだけ。新しい順）。"""
    items = [
        set_item(item)
        for item in evaluation_set_store.list(agent_id or None)
        if _agent_allowed(request, item.agent_id)
    ]
    return ApiResponse(data=EvaluationSetsData(sets=items))


@router.get("/evaluation-sets/template.xlsx")
async def evaluation_set_template() -> Response:
    """評価ケースの Excel のテンプレート。

    列は ケースID・質問・期待する回答の要点・期待するツール。
    """
    return Response(
        content=template_xlsx(),
        media_type=_XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="evaluation-cases-template.xlsx"'},
    )


@router.post("/evaluation-sets/parse-xlsx", response_model=ApiResponse[EvaluationCasesData])
async def parse_evaluation_cases(
    file: Annotated[UploadFile, File()],
) -> ApiResponse[EvaluationCasesData]:
    """Excel の評価ケースを読む（保存しない。画面が評価セットのフォームに入れる）。"""
    data = await file.read(EXCEL_MAX_BYTES + 1)
    try:
        cases = await run_in_threadpool(parse_cases_xlsx, data)
    except EvaluationExcelError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ApiResponse(data=EvaluationCasesData(cases=cases))


@router.post("/evaluation-sets", response_model=ApiResponse[EvaluationSet])
async def create_evaluation_set(
    payload: EvaluationSetInput, request: Request
) -> ApiResponse[EvaluationSet]:
    _require_evaluable_agent(request, payload.agent_id)
    _require_case_sources(payload.cases, payload.agent_id)
    try:
        created = evaluation_set_store.create(payload, created_by=_run_creator_user_uuid(request))
    except ControlPlaneStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ApiResponse(data=created)


@router.get("/evaluation-sets/{set_id}", response_model=ApiResponse[EvaluationSet])
async def get_evaluation_set(set_id: str, request: Request) -> ApiResponse[EvaluationSet]:
    return ApiResponse(data=_evaluation_set_for_actor(request, set_id))


@router.put("/evaluation-sets/{set_id}", response_model=ApiResponse[EvaluationSet])
async def update_evaluation_set(
    set_id: str, payload: EvaluationSetInput, request: Request
) -> ApiResponse[EvaluationSet]:
    current = _evaluation_set_for_actor(request, set_id)
    if payload.agent_id != current.agent_id:
        raise HTTPException(status_code=422, detail="評価セットの業務 Agent は変えられません。")
    try:
        updated = evaluation_set_store.update(set_id, payload)
    except ControlPlaneStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ApiResponse(data=updated)


@router.delete("/evaluation-sets/{set_id}", response_model=ApiResponse[None])
async def delete_evaluation_set(set_id: str, request: Request) -> ApiResponse[None]:
    """評価セットを削除する（評価の結果は残す）。実行中の評価が使っていれば 409。"""
    item = _evaluation_set_for_actor(request, set_id)
    if evaluation_store.has_active_for_set(item.id):
        raise HTTPException(
            status_code=409,
            detail="この評価セットで評価を実行しています。終わってから削除してください。",
        )
    try:
        evaluation_set_store.delete(item.id)
    except ControlPlaneStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ApiResponse(data=None)


@router.get("/evaluation-sets/{set_id}/cases.xlsx")
async def export_evaluation_set(set_id: str, request: Request) -> Response:
    item = _evaluation_set_for_actor(request, set_id)
    return Response(
        content=await run_in_threadpool(export_set_xlsx, item),
        media_type=_XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{item.id}.xlsx"'},
    )


@router.post("/evaluations", response_model=ApiResponse[EvaluationJob], status_code=202)
async def create_evaluation(
    evaluation: EvaluationRequest,
    request: Request,
) -> ApiResponse[EvaluationJob]:
    """品質評価を始める（#776）。権限は middleware のメニュー権限（`menu.evaluation`）。

    評価の Run は始めた利用者の Run として作るため、業務 Agent の対象範囲を確かめる。
    """
    item = _evaluation_set_for_actor(request, evaluation.set_id)
    # 評価セットの業務 Agent 以外でも評価できる（同じ評価ケースで Agent をまたいで比べる。#1845）。
    agent = _require_evaluable_agent(request, evaluation.agent_id or item.agent_id)
    if not agent.enabled:
        raise HTTPException(status_code=409, detail="この業務 Agent は実行できない状態です。")
    # 評価する版（#810）。下書きの評価は下書きの実行と同じく、Agent 管理か個人の Agent の持ち主
    # だけ（#2522・#2523）。省略時は、下書きを評価できて公開していない変更があれば下書き、
    # それ以外は公開中の版。
    manages = _policy_manages_agent(_actor_policy(request), agent)
    target = evaluation.agent_version or (
        "draft" if manages and agent.has_unpublished_changes() else "published"
    )
    if target == "draft" and not manages:
        raise HTTPException(status_code=403, detail=DRAFT_RUN_FORBIDDEN_MESSAGE)
    if target == "published" and agent.published_version is None:
        raise HTTPException(
            status_code=409,
            detail="公開した版がありません。下書きで評価するか、公開してから評価してください。",
        )
    # 反復の回数と実行の予算（#62）。ケース × 試行の数が予算を超える評価は始めない。
    budget = get_settings().agent_evaluation_max_trial_runs
    if len(item.cases) * evaluation.trials > budget:
        raise HTTPException(
            status_code=422,
            detail=(
                f"ケース {len(item.cases)} 件 × {evaluation.trials} 回の試行は、1 回の評価の"
                f"実行の予算（{budget} 回）を超えます。回数かケースを減らしてください。"
            ),
        )
    job = EvaluationJob(
        agent_id=agent.id,
        agent_name=agent.name,
        set_id=item.id,
        set_name=item.name,
        agent_version="draft" if target == "draft" else agent.published_version,
        definition_hash=draft_definition_hash(agent) if target == "draft" else None,
        trials=evaluation.trials,
        judge_fingerprint=judge_fingerprint(),
        created_by_user_uuid=_run_creator_user_uuid(request),
        # 試行の順に並べる（取消で止まっても、先の試行はすべてのケースがそろう。#62）。
        results=[
            EvaluationCaseResult(case=case, trial=trial)
            for trial in range(1, evaluation.trials + 1)
            for case in item.cases
        ],
    )
    try:
        created = evaluation_store.create(job)
    except EvaluationBusyError as exc:
        raise HTTPException(
            status_code=409, detail="ほかの評価を実行しています。終わってから始めてください。"
        ) from exc
    task = asyncio.get_running_loop().create_task(run_evaluation_job(created.id))
    _evaluation_tasks.add(task)
    task.add_done_callback(_evaluation_tasks.discard)
    return ApiResponse(data=evaluation_store.with_previous(created))


_evaluation_jobs_paging = offset_params(
    default=EVALUATION_JOBS_PAGE_SIZE, max_limit=EVALUATION_JOBS_PAGE_SIZE_MAX
)


@router.get("/evaluations", response_model=ApiResponse[EvaluationJobsData])
async def list_evaluations(
    request: Request,
    paging: Annotated[OffsetParams, Depends(_evaluation_jobs_paging)],
    set_id: str | None = Query(default=None, max_length=100),
) -> ApiResponse[EvaluationJobsData]:
    """評価の履歴（新しい順のページ。利用できる業務 Agent の評価だけ。無期限。#3235）。"""
    return ApiResponse(
        data=evaluation_store.page(
            set_id=set_id or None,
            allowed=lambda agent_id: _agent_allowed(request, agent_id),
            offset=paging.offset,
            limit=paging.limit,
        )
    )


def _evaluation_for_actor(request: Request, job_id: str) -> EvaluationJob:
    try:
        job = evaluation_store.get(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="評価が見つかりません。") from exc
    if not _agent_allowed(request, job.agent_id):
        raise HTTPException(status_code=404, detail="評価が見つかりません。")
    return job


@router.get("/evaluations/compare", response_model=ApiResponse[EvaluationComparison])
async def compare_evaluations(
    request: Request,
    base_job_id: str = Query(min_length=1, max_length=100),
    target_job_id: str = Query(min_length=1, max_length=100),
) -> ApiResponse[EvaluationComparison]:
    """2 つの評価をケースごとに並べて比べる（業務 Agent をまたいでよい。#1845）。

    権限は middleware のメニュー権限（`menu.evaluation`）。どちらの job も利用者が使える
    業務 Agent のものだけ（無ければ 404）。
    """
    if base_job_id == target_job_id:
        raise HTTPException(status_code=422, detail="違う評価を 2 つ選んでください。")
    base = _evaluation_for_actor(request, base_job_id)
    target = _evaluation_for_actor(request, target_job_id)
    return ApiResponse(data=compare_jobs(base, target))


@router.get("/evaluations/{job_id}", response_model=ApiResponse[EvaluationJob])
async def get_evaluation(job_id: str, request: Request) -> ApiResponse[EvaluationJob]:
    """評価の結果（同じ評価セットの前回の概要を添える）。"""
    return ApiResponse(data=evaluation_store.with_previous(_evaluation_for_actor(request, job_id)))


@router.post("/evaluations/{job_id}/cancel", response_model=ApiResponse[EvaluationJob])
async def cancel_evaluation(job_id: str, request: Request) -> ApiResponse[EvaluationJob]:
    """評価を取り消す（実行中のケースの Run も取り消し、残りのケースは実行しない）。"""
    job = _evaluation_for_actor(request, job_id)
    cancelled = evaluation_store.cancel(job.id)
    actor = _actor_login_id(request)
    for result in cancelled.results:
        if result.status == CaseStatus.RUNNING and result.run_id:
            with contextlib.suppress(KeyError):
                # 取り消した人を残す（実行の詳細の「取消: 〈利用者〉 · 画面」。#3348）。
                await run_in_threadpool(
                    runtime_repository.cancel_run, result.run_id, cancelled_by=actor
                )
    return ApiResponse(data=evaluation_store.with_previous(evaluation_store.get(job.id)))


@router.delete("/evaluations/{job_id}", response_model=ApiResponse[None])
async def delete_evaluation(job_id: str, request: Request) -> ApiResponse[None]:
    job = _evaluation_for_actor(request, job_id)
    try:
        evaluation_store.delete(job.id)
    except EvaluationJobActiveError as exc:
        raise HTTPException(
            status_code=409, detail="実行中の評価は削除できません。取り消してから削除してください。"
        ) from exc
    return ApiResponse(data=None)


@router.get("/usage", response_model=ApiResponse[UsageReport])
async def get_usage_report(
    request: Request,
    days: int = Query(default=30),
    timezone: str = Query(default="", max_length=64),
) -> ApiResponse[UsageReport]:
    """利用状況（#772）。利用できる業務 Agent の Run のモデル利用量を集計する。

    Oracle の構成は保存した Run の事実（`AGENT_RUN_FACTS`）を SQL で集計する（#794）。

    権限は middleware のメニュー権限（`menu.usage`）で確かめる。日は `timezone`（画面の
    ブラウザの IANA 名。省いたら共通の設定の時間帯 `PLATFORM_TIMEZONE`。#3255）で区切る。
    """
    if days not in USAGE_PERIOD_DAYS:
        raise HTTPException(status_code=422, detail=_PERIOD_DETAIL)
    try:
        tz = resolve_timezone(timezone)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    now = datetime.now(UTC)
    agent_names = {
        agent.id: agent.name for agent in await run_in_threadpool(runtime_repository.list_agents)
    }
    store = run_facts_store.reporting_store()
    if store is not None:
        try:
            return ApiResponse(
                data=await run_in_threadpool(
                    store.usage_report,
                    days=days,
                    now=now,
                    tz=tz,
                    agent_ids=_report_agent_scope(request),
                    agent_names=agent_names,
                    user_names=user_display_names,
                )
            )
        except run_facts_store.RunFactsUnavailableError:
            logger.warning("agent_run_facts_table_missing", extra={"report": "usage"})
        except Exception:  # noqa: BLE001 - DB の障害でも画面を出す（メモリの Run を集計する）
            logger.exception("agent_run_facts_report_failed", extra={"report": "usage"})
    runs = _filter_runs_for_actor(request, await run_in_threadpool(runtime_repository.list_runs))
    report = await run_in_threadpool(
        build_usage_report,
        [fact_from_run(run) for run in runs],
        days=days,
        now=now,
        tz=tz,
        agent_names=agent_names,
        user_names=user_display_names,
    )
    return ApiResponse(data=report)


# 画面の一覧は 1 ページ（既定 25 件（一覧の共通の既定。#1952）・上限 1,000 件）。
# CSV は 1 ページではなく、条件に合う記録を既定 1,000 件（上限 5,000 件）まで出力する。
_tool_call_audit_paging = offset_params(default=DEFAULT_PAGE_LIMIT, max_limit=1000)
_tool_call_audit_csv_paging = offset_params(default=1000, max_limit=5000)


@router.get("/audit/tool-calls", response_model=ApiResponse[ToolCallAuditData])
async def list_tool_call_audit(
    request: Request,
    paging: Annotated[OffsetParams, Depends(_tool_call_audit_paging)],
    run_id: str | None = None,
    tool_name: str | None = None,
    status: str | None = None,
    approval_status: str | None = None,
    error_code: str | None = None,
    has_guardrail_warnings: bool | None = None,
    _: None = Depends(require_auditor),
) -> ApiResponse[ToolCallAuditData]:
    return ApiResponse(
        data=_tool_call_audit_data(
            request=request,
            run_id=run_id,
            tool_name=tool_name,
            status=status,
            approval_status=approval_status,
            error_code=error_code,
            has_guardrail_warnings=has_guardrail_warnings,
            paging=paging,
        )
    )


@router.get("/audit/tool-calls.csv", response_class=Response)
async def export_tool_call_audit_csv(
    request: Request,
    paging: Annotated[OffsetParams, Depends(_tool_call_audit_csv_paging)],
    run_id: str | None = None,
    tool_name: str | None = None,
    status: str | None = None,
    approval_status: str | None = None,
    error_code: str | None = None,
    has_guardrail_warnings: bool | None = None,
    _: None = Depends(require_auditor),
) -> Response:
    data = _tool_call_audit_data(
        request=request,
        run_id=run_id,
        tool_name=tool_name,
        status=status,
        approval_status=approval_status,
        error_code=error_code,
        has_guardrail_warnings=has_guardrail_warnings,
        paging=paging,
    )
    return Response(
        _tool_call_audit_csv(data.items),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="agent-tool-call-audit.csv"'},
    )


@router.get("/runs/{run_id}/artifacts", response_model=ApiResponse[ArtifactsData])
async def list_run_artifacts(
    run_id: str,
    request: Request,
    _: None = Depends(require_viewer),
) -> ApiResponse[ArtifactsData]:
    run = _readable_run(request, run_id)
    try:
        artifacts = await run_in_threadpool(runtime_repository.list_artifacts, run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=RUN_NOT_FOUND_MESSAGE) from exc
    unreadable = await _unreadable_evidence(request, [run], artifacts)
    return ApiResponse(data=ArtifactsData(artifacts=withhold_artifacts(artifacts, unreadable)))


@router.get("/runs/{run_id}/artifacts/{artifact_id}", response_model=ApiResponse[Artifact])
async def get_run_artifact(
    run_id: str,
    artifact_id: str,
    request: Request,
    _: None = Depends(require_viewer),
) -> ApiResponse[Artifact]:
    run = _readable_run(request, run_id)
    try:
        artifact = await run_in_threadpool(runtime_repository.get_artifact, run_id, artifact_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="成果物が見つかりません。") from exc
    artifacts = (
        await run_in_threadpool(runtime_repository.list_artifacts, run_id)
        if artifact.kind == "answer"
        else [artifact]
    )
    unreadable = await _unreadable_evidence(request, [run], artifacts)
    checked = next(
        item for item in withhold_artifacts(artifacts, unreadable) if item.id == artifact_id
    )
    return ApiResponse(data=checked)


@router.get("/runs/{run_id}/figure-url", response_model=ApiResponse[RunFigureUrlData])
async def get_run_figure_url(
    run_id: str,
    request: Request,
    response: Response,
    document_id: Annotated[str, Query(min_length=1, max_length=128)],
    chunk_id: Annotated[str, Query(min_length=1, max_length=512)],
    _: None = Depends(require_viewer),
) -> ApiResponse[RunFigureUrlData]:
    """Run の RAG の図の根拠を開く短命の URL（#1311）。

    画面を見ている利用者として RAG に作らせる（Run の利用者の権限は借りない）。URL は保存せず、
    期限（5 分）が切れたら画面が取り直す。Run は実行した利用者と system_admin だけ（#1889）。
    """
    run = _readable_run(request, run_id)
    viewer = _run_creator_user_uuid(request)
    if not viewer:
        raise HTTPException(status_code=401, detail="ログインしてください。")
    artifacts = [
        await run_in_threadpool(runtime_repository.get_artifact, run_id, artifact.id)
        if artifact.content_ref is not None
        else artifact
        for artifact in run.artifacts
        if artifact.kind in EVIDENCE_ARTIFACT_KINDS
    ]
    try:
        data = await run_in_threadpool(
            partial(
                issue_run_figure_url,
                run,
                artifacts,
                document_id=document_id,
                chunk_id=chunk_id,
                viewer_user_uuid=viewer,
            )
        )
    except RunFigureUrlError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
    # URL はトークンを含むので、ブラウザ・中継の cache に残さない。
    response.headers["Cache-Control"] = "private, no-store"
    return ApiResponse(data=data)


@router.get("/runs/{run_id}/source-preview", response_model=ApiResponse[RunSourcePreviewData])
async def get_run_source_preview(
    run_id: str,
    request: Request,
    response: Response,
    document_id: Annotated[str, Query(min_length=1, max_length=128)],
    chunk_id: Annotated[str, Query(min_length=1, max_length=512)],
    _: None = Depends(require_viewer),
) -> ApiResponse[RunSourcePreviewData]:
    """Run の RAG の根拠の原文のプレビュー（#2389。ページの一覧と短命の URL）。

    図の URL（#1311）と同じく、画面を見ている利用者として RAG に作らせる（Run の利用者の権限は
    借りない。その文書を読めない利用者には RAG が作らない）。URL は保存せず、期限（5 分）が切れる
    前に画面が取り直す。Run は実行した利用者・会話の持ち主と system_admin だけ（#1889・#1954）。
    """
    run = _readable_run(request, run_id)
    viewer = _run_creator_user_uuid(request)
    if not viewer:
        raise HTTPException(status_code=401, detail="ログインしてください。")
    artifacts = [
        await run_in_threadpool(runtime_repository.get_artifact, run_id, artifact.id)
        if artifact.content_ref is not None
        else artifact
        for artifact in run.artifacts
        if artifact.kind in EVIDENCE_ARTIFACT_KINDS
    ]
    try:
        data = await run_in_threadpool(
            partial(
                issue_run_source_preview,
                run,
                artifacts,
                document_id=document_id,
                chunk_id=chunk_id,
                viewer_user_uuid=viewer,
            )
        )
    except RunFigureUrlError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
    # URL はトークンを含むので、ブラウザ・中継の cache に残さない。
    response.headers["Cache-Control"] = "private, no-store"
    return ApiResponse(data=data)


@router.get("/runs/{run_id}/events")
async def stream_run_events(
    run_id: str,
    request: Request,
    follow: bool = Query(default=False),
    after_event_id: str | None = Query(default=None),
    _: None = Depends(require_viewer),
) -> Response:
    run = _readable_run(request, run_id)
    try:
        events = await run_in_threadpool(
            runtime_repository.iter_events, run_id, after_event_id=after_event_id, follow=follow
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=RUN_NOT_FOUND_MESSAGE) from exc
    if not follow:
        # 保存したイベントのツールの結果も、見る人の今の権限で確かめ直す（#3252）。実行中の
        # 購読（follow）は、見る人が今見ている実行の結果なので確かめない。
        unreadable = await _unreadable_evidence(request, [run], await _evidence_artifacts(run))
        events = withhold_events(events, unreadable)
    # 中継（Nginx）の buffering と cache を切る header は、共通の進み具合の SSE と同じ（#1597）。
    if not follow:
        return Response(
            "".join(_sse_events(events)),
            media_type="text/event-stream",
            headers=CHAT_PROGRESS_SSE_HEADERS,
        )
    return StreamingResponse(
        _sse_events(events), media_type="text/event-stream", headers=CHAT_PROGRESS_SSE_HEADERS
    )


async def _evidence_artifacts(run: RunState) -> list[Artifact]:
    """中身を読み込んだ根拠の成果物（中身が別の保存先にある成果物は読み込む）。"""
    return [
        await run_in_threadpool(runtime_repository.get_artifact, run.id, artifact.id)
        if artifact.content_ref is not None
        else artifact
        for artifact in run.artifacts
        if artifact.kind in EVIDENCE_ARTIFACT_KINDS
    ]


async def _unreadable_evidence(
    request: Request, runs: list[RunState], artifacts: list[Artifact]
) -> frozenset[str]:
    """Run の RAG の根拠の文書のうち、見ている利用者が今見られない文書（#3252）。

    画面を見ている利用者として RAG に確かめる（Run の利用者の権限は借りない）。根拠の無い Run は
    RAG を呼ばない。
    """
    document_ids = rag_document_ids(runs, artifacts)
    if not runs or not document_ids:
        return frozenset()
    viewer = _run_creator_user_uuid(request)
    if not viewer:
        return frozenset(document_ids)
    return await run_in_threadpool(unreadable_documents, runs[0], viewer, document_ids)


async def _evidence_checked_views(
    request: Request, runs: list[RunState], names: Mapping[str, str]
) -> list[RunView]:
    """画面に返す Run（保存した根拠を見る人の今の権限で確かめ直し、見られない根拠は数だけ）。"""
    loaded = {run.id: await _evidence_artifacts(run) for run in runs}
    unreadable = await _unreadable_evidence(
        request, runs, [artifact for items in loaded.values() for artifact in items]
    )
    cancellations = await _run_cancellations(request, runs)
    views: list[RunView] = []
    for run in runs:
        viewer = _run_viewer(request, run, names)
        viewer.withheld_evidence_count = withheld_count(run, unreadable, loaded[run.id])
        viewer.cancellation = cancellations.get(run.id)
        views.append(RunView.of(withhold_run(run, unreadable), viewer))
    return views


async def _run_cancellations(
    request: Request, runs: Sequence[RunState]
) -> dict[str, RunCancellationView]:
    """取り消した Run の「取消」（取り消した人の表示名と入口。Run の ID → 事実。#3348）。

    取り消した人は `run.cancelled` のイベントのログイン ID から引く。MCP の取消の API キーの名前は、
    見ている利用者の権限で引ける既存のキーの一覧から引く（system_admin は全員のキー、それ以外は
    自分のキーだけ。見られないキー・削除したキーは空）。キーの ID は返さない。
    """
    records = {run.id: record for run in runs if (record := run_cancel_record(run)) is not None}
    if not records:
        return {}
    principal = _request_principal(request)
    logins = sorted({item.cancelled_by for item in records.values() if item.cancelled_by})
    key_ids = sorted({item.api_key_id for item in records.values() if item.api_key_id})
    names = await run_in_threadpool(login_display_names, logins) if logins else {}
    key_names: dict[str, str] = {}
    if key_ids and principal is not None:
        try:
            key_names = await run_in_threadpool(
                get_security_service().api_key_names, principal, key_ids
            )
        except Exception:  # noqa: BLE001 - キーの名前は表示の補助（引けなければ「MCP」だけ）
            logger.warning("agent_run_cancel_key_names_unavailable", exc_info=True)
    me = principal.login_user_id if principal is not None else None
    return {
        run_id: RunCancellationView(
            via=record.via,
            actor_display_name=names.get(record.cancelled_by, "") if record.cancelled_by else "",
            by_me=bool(record.cancelled_by) and record.cancelled_by == me,
            api_key_name=key_names.get(record.api_key_id, "") if record.api_key_id else "",
        )
        for run_id, record in records.items()
    }


async def _evidence_checked_run(request: Request, run: RunState) -> tuple[RunState, int]:
    """保存した根拠を見る人の今の権限で確かめ直した Run と、見られないため除いた根拠の数（画面の
    `withheld_evidence_count` と同じ数え方。MCP の `agent_get_run`。#3252）。"""
    artifacts = await _evidence_artifacts(run)
    unreadable = await _unreadable_evidence(request, [run], artifacts)
    if not unreadable:
        return run, 0
    loaded = {item.id: item for item in artifacts}
    hydrated = run.model_copy(
        update={"artifacts": [loaded.get(item.id, item) for item in run.artifacts]}
    )
    return withhold_run(hydrated, unreadable), withheld_count(run, unreadable, artifacts)


def _readable_run(request: Request, run_id: str) -> RunState:
    """「AI 活用」の画面が読む・操作する Run（#1889）。

    無い・ほかの利用者の Run（system_admin でない利用者）は 404（存在を漏らさない）、業務 Agent の
    対象外は 403。実行履歴・承認・チャットの Run の詳細・成果物・イベント・操作が同じ判定を使う。
    """
    try:
        run = runtime_repository.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=RUN_NOT_FOUND_MESSAGE) from exc
    if not _run_visible_to(request, run):
        raise HTTPException(status_code=OWNER_NOT_FOUND_STATUS, detail=RUN_NOT_FOUND_MESSAGE)
    _require_agent_access(request, run.agent_id)
    return run


class RunTraceData(BaseModel):
    """Run の処理の経過（共通の span の表の snapshot。#3226 の案 A）。"""

    target_id: str
    # 実行の全体の span の trace id（受け付けただけの Run・開始の前に終わった Run は無し）。
    trace_id: str | None = None
    # 処理の経過のイベント（span の段と、終わった Run の終端。3 製品共通の形）。
    events: list[dict[str, Any]] = Field(default_factory=list)
    # 表に書けなかった span の数（1 以上なら画面は「一部の処理だけを記録しています。」）。
    dropped_spans: int = 0


def _run_trace_viewer(request: Request, run: RunState) -> str | None:
    """表の span を絞る利用者（None は全員の span。system_admin）。

    Run の判定（`_readable_run`）を通った後に呼ぶ。system_admin 以外は、Run を実行した利用者が
    持ち主の span だけ（Run の span と、呼び先の MCP のサービストークンの `sub` の span）。会話の
    持ち主が system_admin の実行した Run を開いたときも、その Run の span だけを読む（ほかの利用者が
    同じ trace に書いた span は出さない）。
    """
    if _sees_all_users(request):
        return None
    return run.created_by_user_uuid or ""


@router.get("/runs/{run_id}/trace", response_model=ApiResponse[RunTraceData])
async def get_run_trace(
    run_id: str,
    request: Request,
    _: None = Depends(require_viewer),
) -> ApiResponse[RunTraceData]:
    """Run の処理の経過（共通の span の表の snapshot。3 製品共通の形。#3226 の案 A）。

    Run を今の判定で開き（ほかの利用者の Run は 404）、その Run の trace id だけで表を読む。最終の
    状態は Run の状態（正本）で、表で実行中のままの span は Run の終端に合わせて閉じる。工程の詳細
    （ツールの引数と結果など）は Run から足す。画面は実行中に読み直す
    （`useTraceProgressSnapshot`）。
    """
    run = await run_in_threadpool(_readable_run, request, run_id)
    run, _withheld = await _evidence_checked_run(request, run)
    spans: list[Any] = []
    if run.trace_id is not None:
        try:
            spans = await run_in_threadpool(
                trace_spans.fetch, run.trace_id, viewer_user_uuid=_run_trace_viewer(request, run)
            )
        except Exception as exc:
            logger.warning(
                "agent_run_trace_read_failed",
                extra={"run_id": run.id, **safe_exception_fields(exc)},
            )
            raise HTTPException(status_code=503, detail=RUN_TRACE_UNAVAILABLE_MESSAGE) from exc
    return ApiResponse(
        data=RunTraceData(
            target_id=run.id,
            trace_id=run.trace_id,
            events=run_trace_events(run, spans),
            dropped_spans=run.trace_dropped_spans,
        )
    )


@router.post("/runs/{run_id}/cancel", response_model=ApiResponse[RunState])
async def cancel_run(
    run_id: str,
    request: Request,
    _: None = Depends(require_operator),
) -> ApiResponse[RunState]:
    run = _readable_run(request, run_id)
    try:
        # 終了した Run は取り消さない（完了の結果を取消済みで上書きしない。#911）。
        if run.status in _TERMINAL_RUN_STATUSES:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "run_not_cancellable",
                    "message": "この実行はすでに終了しているため、取り消せません。",
                },
            )
        cancelled = await run_in_threadpool(
            runtime_repository.cancel_run, run_id, cancelled_by=_actor_login_id(request)
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=RUN_NOT_FOUND_MESSAGE) from exc
    _schedule_nl2sql_job_cancel(run.status, cancelled)
    return ApiResponse(data=cancelled)


@router.post(
    "/runs/{run_id}/confirmations/{confirmation_id}",
    response_model=ApiResponse[ConfirmationOutcome],
)
async def decide_confirmation(
    run_id: str,
    confirmation_id: str,
    decision: ConfirmationDecisionRequest,
    request: Request,
    _: None = Depends(require_operator),
) -> ApiResponse[ConfirmationOutcome]:
    """回答前の確認（#1839）に答える・確認せずに回答する・確認をやめる。

    答える・確認せずに回答するのは Run の持ち主（会話の持ち主）だけ。system_admin は他の利用者の
    Run を読めるが、答えは足せない（403。#3244）。ほかの利用者の Run は 404。Agent 自身の確認は
    同じ Run を再開し、ツールの結果の確認（NL2SQL・RAG）は答えを構造化した次の Run を同じ会話に
    作り、答えた利用者として実行する。Agent 自身の確認への答えは同じ Run を Run の利用者の権限で
    再開するので、加えて Run を実行した利用者だけ（会話の持ち主でも、ほかの人が実行した Run は
    404）。やめる（取り消し）は運用の操作で、Run を読める利用者ができる（質問は画面が入力欄に
    戻す）。
    """
    run = _readable_run(request, run_id)
    confirmation = next((item for item in run.confirmations if item.id == confirmation_id), None)
    if (
        confirmation is not None
        and confirmation.source == SOURCE_AGENT
        and decision.action != "cancel"
        and not _run_approvable_by(request, run)
    ):
        raise HTTPException(status_code=404, detail="回答前の確認が見つかりません。")
    if decision.action != "cancel" and not _run_content_addable_by(request, run):
        _raise_others_content_forbidden()
    if decision.action == "answer" or decision.action == "skip":
        # 次の Run を作る・再開する前に、業務 Agent が今も使えるかを確かめる（POST /runs と同じ）。
        try:
            reason = agent_unavailable_reason(_control_plane_agent(run.agent_id))
        except KeyError:
            reason = agent_unavailable_reason(None)
        if reason is not None and run.metadata.get("agent_version") != "draft":
            raise HTTPException(
                status_code=409, detail={"code": "agent_unavailable", "message": reason}
            )
    principal = _request_principal(request)
    try:
        outcome = await run_in_threadpool(
            runtime_repository.decide_confirmation,
            run_id,
            confirmation_id,
            action=decision.action,
            answers=decision.answers,
            decided_by=principal.login_user_id if principal is not None else None,
            actor_user_uuid=principal.user_uuid if principal is not None else None,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="回答前の確認が見つかりません。") from exc
    except ConfirmationClosedError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "confirmation_closed",
                "message": (
                    "この確認は答え済みか、会話が先へ進んだため答えられません。"
                    "会話を読み込み直してください。"
                ),
            },
        ) from exc
    except ConfirmationAnswerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AgentNotPublishedError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "agent_unpublished", "message": AGENT_UNPUBLISHED_MESSAGE},
        ) from exc
    if outcome.next_run is not None:
        _schedule_builtin_run(outcome.next_run)
    _schedule_builtin_run(outcome.run)
    if outcome.closed_sessions:
        # 「確認をやめる」: NL2SQL の SQL の生成前の確認の session を閉じる
        # （閉じられなければ期限で）。
        task = asyncio.get_running_loop().create_task(
            builtin_runtime.close_requirement_sessions(run_id, outcome.closed_sessions)
        )
        _builtin_tasks.add(task)
        task.add_done_callback(_builtin_tasks.discard)
    return ApiResponse(data=outcome)


@router.post("/runs/{run_id}/resume", response_model=ApiResponse[RunState])
async def resume_run(
    run_id: str,
    request: Request,
    _: None = Depends(require_operator),
) -> ApiResponse[RunState]:
    run = _readable_run(request, run_id)
    # Run の利用者の権限で実行し直すので、承認と同じく実行した利用者と system_admin だけ
    # （会話の持ち主でも、ほかの人が実行した Run は 404。#1928）。
    if not _run_approvable_by(request, run):
        raise HTTPException(status_code=OWNER_NOT_FOUND_STATUS, detail=RUN_NOT_FOUND_MESSAGE)
    try:
        # 組み込み Runtime は承認がすべて決まると自動で再開する。止まっている Run だけ再開を
        # 起動し直す。旧エンジンの Run（#756 で削除）は読み取りだけ。
        if not builtin_resume_pending(run):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "run_not_resumable",
                    "message": "承認待ちが残っているか、再開できる状態ではありません。",
                },
            )
        # 再開は Run の利用者のまま実行する。操作した人（system_admin のことがある）を
        # ログに残す（#1928）。
        logger.info(
            "agent_run_resume_requested",
            extra={"run_id": run.id, "requested_by": _actor_login_id(request)},
        )
        _schedule_builtin_run(run)
        return ApiResponse(data=run)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=RUN_NOT_FOUND_MESSAGE) from exc


@router.post("/runs/{run_id}/replay", response_model=ApiResponse[RunState])
async def replay_run(
    run_id: str,
    request: Request,
    _: None = Depends(require_operator),
) -> ApiResponse[RunState]:
    run = _readable_run(request, run_id)
    # 再実行は利用者の Run（公開中の版で実行する）。`POST /runs` と同じく、無効・移行が要る・
    # 公開した版の無い業務 Agent は理由を返す（500 にしない。#911）。
    try:
        reason = agent_unavailable_reason(_control_plane_agent(run.agent_id))
    except KeyError:
        reason = agent_unavailable_reason(None)
    if reason is not None:
        raise HTTPException(
            status_code=409, detail={"code": "agent_unavailable", "message": reason}
        )
    try:
        # 同じ Agent・ゴールの新しい Run として組み込み Runtime で実行する
        # （再実行を指示した利用者として。旧エンジンの Run も同じ）。元の Run の起点の印
        # （品質評価の dry-run・MCP など）は引き継がない（#911）。会話は引き継がず、
        # 再実行した利用者の新しい会話にする（system_admin が他の利用者の Run を再実行しても、
        # 持ち主の会話には足さない。#3244）。
        replayed = await run_in_threadpool(
            runtime_repository.create_builtin_run,
            RunCreateRequest(
                goal=run.goal,
                agent_id=run.agent_id,
                metadata={"replayed_from_run_id": run.id},
            ),
            created_by_user_uuid=_run_creator_user_uuid(request),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=AGENT_NOT_FOUND_MESSAGE) from exc
    except ValueError as exc:
        # 確かめた後に Agent が無効・非公開になった（AgentNotPublishedError を含む）。
        raise HTTPException(
            status_code=409,
            detail={
                "code": "agent_unavailable",
                "message": "この業務 Agent は実行できない状態です。",
            },
        ) from exc
    _schedule_builtin_run(replayed)
    return ApiResponse(data=replayed)


@router.post("/approvals/{approval_id}/decision", response_model=ApiResponse[RunState])
async def decide_approval(
    approval_id: str,
    request: ApprovalDecisionRequest,
    http_request: Request,
    _: None = Depends(require_approver),
) -> ApiResponse[RunState]:
    try:
        run = _run_for_approval(approval_id)
        # 承認は自分の Run の承認だけ（承認の権限を持つ人も、ほかの利用者の Run は 404）。
        # ほかの利用者の Run の承認は system_admin だけ（#1889・#1928）。
        if not _run_approvable_by(http_request, run):
            raise KeyError(approval_id)
        _require_agent_access(http_request, run.agent_id)
        always_allow = request.approved and request.always_allow
        if always_allow:
            # 「今後は自動で実行」はツール権限を変えるので Agent 管理だけ（#1832）。
            _require_actor_roles(http_request, {"admin"})
        elif request.always_allow:
            request = request.model_copy(update={"always_allow": False})
        principal = _request_principal(http_request)
        if principal is not None:
            # 決定者はログイン中の利用者（body の decided_by は使わない。なりすまし防止。#215）。
            request = request.model_copy(update={"decided_by": principal.login_user_id})
        if always_allow:
            decided = await run_in_threadpool(
                runtime_repository.decide_approval,
                approval_id,
                request,
                before_decision=lambda current: _allow_tool_from_approval(
                    current, approval_id, request.decided_by
                ),
            )
        else:
            decided = await run_in_threadpool(
                runtime_repository.decide_approval, approval_id, request
            )
        _schedule_builtin_run(decided)
        return ApiResponse(data=decided)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=APPROVAL_NOT_FOUND_MESSAGE) from exc


def _approval_status(run: RunState, approval_id: str) -> ApprovalStatus | None:
    return next((item.status for item in run.approvals if item.id == approval_id), None)


def _allow_tool_from_approval(run: RunState, approval_id: str, decided_by: str) -> None:
    """承認したツールをツール権限の「自動実行」（allow）にする（#1832）。

    Run のイベント（APPROVAL_DECIDED の `always_allow`）と、ログ
    `agent_tool_policy_always_allow` に残す。
    """
    approval = next(item for item in run.approvals if item.id == approval_id)
    tool_name = approval.tool_call.name
    runtime_config_store.allow_tool(
        tool_name,
        persist=lambda policy: _persist(lambda: control_plane_store.save_tool_policy(policy)),
    )
    logger.info(
        "agent_tool_policy_always_allow",
        extra={
            "run_id": run.id,
            "approval_id": approval_id,
            "tool_name": tool_name,
            "decided_by": decided_by,
        },
    )


@router.get("/agents", response_model=ApiResponse[AgentsData])
async def list_agents(request: Request) -> ApiResponse[AgentsData]:
    """業務 Agent の一覧。RBAC が有効なら利用者が使えるエージェントだけに絞る（#215）。"""
    all_agents = await run_in_threadpool(runtime_repository.list_agents)
    agents = [agent for agent in all_agents if _agent_allowed(request, agent.id)]
    data = await _agents_data(agents)
    actor = _run_creator_user_uuid(request)
    data.share_approvals = [
        AgentShareApprovalNotice(
            agent_id=agent.id, name=sharing.approved_name, decided_at=sharing.decided_at
        )
        for agent in all_agents
        if (sharing := agent.share_request) is not None
        and sharing.status == "approved"
        and sharing.requested_by_user_uuid == actor
        and not _agent_allowed(request, agent.id)
    ]
    return ApiResponse(data=data)


async def _agents_data(agents: list[AgentProfile]) -> AgentsData:
    """業務 Agent の一覧と、版の公開者の表示名（画面は表示名で出す。#1696）。"""
    publishers = sorted(
        {
            version.published_by
            for agent in agents
            for version in agent.versions
            if version.published_by
        }
    )
    names = await run_in_threadpool(login_display_names, publishers) if publishers else {}
    owners = sorted({agent.owner_user_uuid for agent in agents if agent.owner_user_uuid})
    owner_names = await run_in_threadpool(user_display_names, owners) if owners else {}
    return AgentsData(
        agents=agents,
        user_display_names=names,
        max_published_versions=max_published_versions(),
        owner_display_names=owner_names,
    )


# 画面・API で作る業務 Agent の ID（URL の path に置くため `/`・空白などを断る。#1033）。
# スキルと同じ形。宣言・スナップショットの既存の ID には当てない。
AGENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")


def _require_agent_name(name: str) -> None:
    """業務 Agent の名前は必須（画面と同じく空白だけも未入力。#925）。"""
    if not name.strip():
        raise HTTPException(status_code=422, detail="業務 Agent の名前を入力してください。")


@router.get(
    "/agent-data-scopes/{connection}/candidates",
    response_model=ApiResponse[DataScopeCandidatesData],
)
async def list_agent_data_scope_candidates(
    connection: str,
    request: Request,
    _: None = Depends(require_agent_builder),
) -> ApiResponse[DataScopeCandidatesData]:
    """業務 Agent のデータの範囲に選べるプロファイル（#1378）。

    MCP の一覧のツール（`nl2sql_list_profiles` / `rag_list_search_answer_profiles`）を
    **編集者**のサービストークンで呼ぶので、編集者が使えるプロファイルだけが返る。
    """
    try:
        data = await run_in_threadpool(
            partial(list_candidates, connection, user_uuid=_run_creator_user_uuid(request))
        )
    except DataScopeCandidatesError as exc:
        raise HTTPException(
            status_code=exc.status_code, detail={"code": exc.code, "message": exc.message}
        ) from None
    return ApiResponse(data=data)


async def _verify_data_scopes(
    request: Request, current: AgentDataScopes | None, requested: AgentDataScopes
) -> None:
    """保存で範囲に加えるプロファイルを、編集者が使えるかを MCP で確かめる（#1378）。

    使えない（見つからない）プロファイルは 400、候補を確かめられないときは候補の取得と同じ status。
    """
    added = added_profile_ids(current, requested)
    if not added:
        return
    try:
        await run_in_threadpool(
            partial(verify_editor_can_use, added, user_uuid=_run_creator_user_uuid(request))
        )
    except DataScopeCandidatesError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "code": exc.code,
                "message": f"データの範囲を確かめられないため保存できません。{exc.message}",
            },
        ) from None
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "agent_data_scope_forbidden", "message": str(exc)}
        ) from None


@router.post("/agents/draft-suggestion", response_model=ApiResponse[AgentDraftSuggestion])
async def suggest_agent_draft_from_description(
    payload: AgentDraftRequest,
    request: Request,
    _: None = Depends(require_agent_builder),
) -> ApiResponse[AgentDraftSuggestion]:
    """業務の説明から業務 Agent の下書きを提案する（#2519。保存はしない）。

    候補は有効なスキルと、**編集者**が使えるプロファイル（データの範囲の候補と同じ取得）。候補を
    取得できない接続は使わずに提案し、その理由を `notices` に返す。
    """
    skills = [skill for skill in skill_registry.list() if skill.enabled]
    user_uuid = _run_creator_user_uuid(request)
    candidates: dict[str, tuple[str, list[DataScopeCandidate]]] = {}
    notices: list[str] = []
    for connection in DATA_SCOPE_CONNECTIONS:
        try:
            data = await run_in_threadpool(
                partial(list_candidates, connection, user_uuid=user_uuid)
            )
        except DataScopeCandidatesError as exc:
            notices.append(exc.message)
            continue
        candidates[connection] = (data.label, data.profiles)
    try:
        suggestion = await suggest_agent_draft(
            payload.description, skills=skills, candidates=candidates, notices=notices
        )
    except AgentDraftError as exc:
        raise HTTPException(
            status_code=502, detail={"code": "agent_draft_failed", "message": str(exc)}
        ) from exc
    except BoundedCallAborted as exc:
        # モデル・接続の設定の誤り・権限・割り当て（待っても直らない）。
        raise HTTPException(
            status_code=503, detail={"code": "agent_draft_unavailable", "message": str(exc)}
        ) from exc
    except builtin_runtime.BuiltinRuntimeError as exc:
        raise HTTPException(
            status_code=503, detail={"code": exc.code, "message": exc.message}
        ) from exc
    return ApiResponse(data=suggestion)


@router.post("/agents", response_model=ApiResponse[AgentProfile])
async def create_agent(
    agent: AgentProfileCreate,
    request: Request,
    _: None = Depends(require_agent_builder),
) -> ApiResponse[AgentProfile]:
    _require_agent_name(agent.name)
    # ID を送らなければ backend が `agent_<uuid>` を作る（画面の作成）。送られた ID は、その後の
    # 取得・変更・削除の URL に置ける形だけを受け付ける（#1033）。
    agent_id = agent.id.strip()
    if not AGENT_ID_PATTERN.fullmatch(agent_id):
        raise HTTPException(
            status_code=422,
            detail=(
                "業務 Agent の ID は英数字で始め、英数字・_・-・. の 100 文字以内にしてください。"
            ),
        )
    # 画面・API で作る Agent は下書きから始める（公開するまで利用者の Run には使えない。#770）。
    # 版の項目は送られても使わない。
    policy = _actor_policy(request)
    # 保存・応答は `AgentProfile`（作成の要求の型はツールの直接の指定を拒むだけ。#3240）。
    draft = AgentProfile.model_validate(agent.model_dump()).model_copy(
        update={
            "id": agent_id,
            "versioned": True,
            "versions": [],
            "published_version": None,
            "source": "runtime",
            # Agent 管理が作れば組織の業務 Agent、自分の業務 Agent の作成の権限だけなら作った人の
            # 個人の業務 Agent（#2523。送られた持ち主・申請は使わない）。
            "owner_user_uuid": None if "admin" in policy.roles else policy.user_uuid,
            "share_request": None,
        }
    )
    # 作成に使った業種テンプレート（#810）。知らないテンプレートは保存しない（400）。
    if draft.template_id and find_template(draft.template_id) is None:
        raise HTTPException(status_code=400, detail="業種テンプレートが見つかりません。")
    await _verify_data_scopes(request, None, draft.data_scopes)
    try:
        return ApiResponse(data=await run_in_threadpool(runtime_repository.create_agent, draft))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _format_rate(value: float) -> str:
    return f"{round(value * 100)}%"


def _rate_uncertainty(summary: EvaluationSummary) -> str:
    """合格率の不確実性と欠測（#62。拒否の文に添える）。"""
    parts = []
    if summary.pass_rate_interval is not None:
        low, high = summary.pass_rate_interval
        parts.append(f"95% 区間 {_format_rate(low)}〜{_format_rate(high)}")
    parts.append(f"ケース {summary.judged_cases} 件")
    if summary.trials > 1:
        parts.append(f"各 {summary.trials} 回の試行")
    if summary.missing_trials:
        parts.append(f"欠測 {summary.missing_trials} 試行")
    return f"（{'、'.join(parts)}）"


def _publish_baseline(agent: AgentProfile, draft_job: EvaluationJob) -> EvaluationJob | None:
    """回帰の門の基準（#2569）: 下書きの評価と同じ評価セット・同じ評価ケースで、公開中の版を
    評価した最新の評価（完了して合格率のあるもの）。

    評価者（判定のモデルと判定の指示）が違うと分かる評価は基準にしない（業務 Agent の差と評価者の
    差を分けられないため。#62）。
    """
    fingerprint = cases_fingerprint(draft_job)
    return next(
        (
            job
            for job in evaluation_store.list()
            if job.agent_id == agent.id
            and job.agent_version == agent.published_version
            and job.status == JobStatus.COMPLETED
            and job.set_id == draft_job.set_id
            and job.summary.pass_rate is not None
            and cases_fingerprint(job) == fingerprint
            and not (
                job.judge_fingerprint
                and draft_job.judge_fingerprint
                and job.judge_fingerprint != draft_job.judge_fingerprint
            )
        ),
        None,
    )


def _paired_note(baseline: EvaluationJob, draft_job: EvaluationJob) -> str:
    """公開中の版との対応のある差（同じ定義のケースごとの成功率の差。#62）の文。"""
    paired = paired_comparison(baseline, draft_job)
    if paired is None:
        return ""
    low, high = paired.interval
    verdict = "差の向きは確か" if paired.conclusive else "区間が 0 をまたぐため差は不確か"
    return (
        f"同じケース {paired.cases} 件の対応比較では差 {round(paired.difference * 100):+d} pt"
        f"（95% 区間 {round(low * 100):+d}〜{round(high * 100):+d} pt、{verdict}）。"
    )


def _publish_evaluation_refusal(agent: AgentProfile) -> str | None:
    """公開前の評価の門（#2519・#2569）。満たさなければ理由の文、満たす・門が無ければ None。

    数えるのは、下書きを最後に変えた後に始めた、この業務 Agent の下書きの評価（完了したもの）の
    最新の 1 件。公開中の版と下書きが同じ（変更の無い再公開）なら求めない。門は、合格率の閾値
    （`AGENT_PUBLISH_MIN_PASS_RATE`）と、公開中の版との回帰（`AGENT_PUBLISH_MAX_PASS_RATE_DROP`）の
    2 つで、どちらかを使うときはケースの数の下限（`AGENT_PUBLISH_MIN_CASES`）も見る。
    """
    settings = get_settings()
    threshold = settings.agent_publish_min_pass_rate
    max_drop = settings.agent_publish_max_pass_rate_drop
    if (threshold <= 0 and max_drop is None) or not agent.has_unpublished_changes():
        return None
    since = agent.draft_updated_at or agent.created_at
    current_definition = draft_definition_hash(agent)
    latest = next(
        (
            job
            for job in evaluation_store.list()
            if job.agent_id == agent.id
            and job.agent_version == "draft"
            and job.status == JobStatus.COMPLETED
            and job.created_at >= since
            and job.definition_hash == current_definition
        ),
        None,
    )
    requirement = f"、合格率 {_format_rate(threshold)} 以上にして" if threshold > 0 else "て"
    if latest is None:
        return (
            f"公開するには、今の下書きを品質評価で確かめ{requirement}ください"
            "（下書きを変えた後の評価がありません）。"
        )
    judged = latest.summary.judged_cases
    if settings.agent_publish_min_cases and judged < settings.agent_publish_min_cases:
        return (
            f"今の下書きの品質評価（{latest.set_name}）で判定できたケースが {judged} 件で、"
            f"公開の判定に必要な {settings.agent_publish_min_cases} 件に足りません。"
            "評価セットにケースを足して評価し直してください。"
        )
    rate = latest.summary.pass_rate
    if rate is None:
        return (
            f"今の下書きの品質評価（{latest.set_name}）で判定できたケースがありません。"
            "評価し直してください。"
        )
    if threshold > 0 and rate < threshold:
        return (
            f"今の下書きの品質評価（{latest.set_name}）が合格率 {_format_rate(rate)}"
            f"{_rate_uncertainty(latest.summary)}で、"
            f"公開に必要な合格率 {_format_rate(threshold)} に届いていません。"
            "指示やスキルを直して評価し直してください。"
        )
    if max_drop is None or agent.published_version is None:
        return None
    baseline = _publish_baseline(agent, latest)
    if baseline is None:
        return (
            f"公開するには、公開中の版（v{agent.published_version}）も同じ評価セット"
            f"（{latest.set_name}）・同じ評価ケース・同じ評価者（判定のモデルと判定の指示）で評価して"
            "ください（下書きの合格率を、公開中の版と比べて確かめます）。"
        )
    base_rate = baseline.summary.pass_rate or 0.0
    # 浮動小数の誤差で、ちょうど許す下げ幅を断らない。
    if rate < base_rate - max_drop - 1e-9:
        return (
            f"今の下書きの合格率 {_format_rate(rate)}{_rate_uncertainty(latest.summary)} が、"
            f"公開中の版（v{agent.published_version}）の合格率 {_format_rate(base_rate)}"
            f"{_rate_uncertainty(baseline.summary)} から"
            "下がりすぎています（許す下げ幅は "
            f"{_format_rate(max_drop)}、評価セット {latest.set_name}）。"
            f"{_paired_note(baseline, latest)}"
            "指示やスキルを直して評価し直してください。"
        )
    return None


@router.post("/agents/{agent_id}/publish", response_model=ApiResponse[AgentProfile])
async def publish_agent(
    agent_id: str,
    payload: AgentPublishRequest,
    request: Request,
    _: None = Depends(require_agent_builder),
) -> ApiResponse[AgentProfile]:
    """下書きを新しい版として公開する（#770）。利用者の Run はこの版で実行する。

    公開前の評価の門（合格率の閾値 #2519・公開中の版との回帰とケースの数の下限 #2569）を使うときは、
    今の下書きの品質評価を求める（満たさなければ 409）。
    """
    current = await _managed_agent(request, agent_id)
    refusal = _publish_evaluation_refusal(current)
    if refusal is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "publish_evaluation_required", "message": refusal},
        )
    try:
        agent = await run_in_threadpool(
            runtime_repository.publish_agent,
            agent_id,
            note=payload.note,
            published_by=_actor_login_user_id(request),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=AGENT_NOT_FOUND_MESSAGE) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApiResponse(data=agent)


@router.get("/agents/{agent_id}/skill-updates", response_model=ApiResponse[AgentSkillUpdates])
async def get_agent_skill_updates(
    agent_id: str, request: Request
) -> ApiResponse[AgentSkillUpdates]:
    """公開中の版に固定したスキルと今のスキルの差（指示の差分・ツール・参照文書。#1867）。"""
    agent = next(
        (
            item
            for item in await run_in_threadpool(runtime_repository.list_agents)
            if item.id == agent_id and _agent_allowed(request, item.id)
        ),
        None,
    )
    if agent is None:
        raise HTTPException(status_code=404, detail=AGENT_NOT_FOUND_MESSAGE)
    return ApiResponse(data=await run_in_threadpool(agent_skill_updates, agent))


@router.post("/agent-skill-updates/publish", response_model=ApiResponse[SkillUpdatePublishData])
async def publish_agent_skill_updates(
    payload: SkillUpdatePublishRequest,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse[SkillUpdatePublishData]:
    """選んだ業務 Agent に、スキルの更新だけを取り込んだ新しい版を公開する（まとめて。#1867）。

    権限は公開と同じ（Agent 管理）。下書きの変更は公開しない。取り込めない業務 Agent（更新が無い・
    公開した版が無い・スキルが消えた）は skipped と理由を返し、ほかは続ける。結果の
    `previous_version` で `POST /agent-skill-updates/rollback` から元に戻せる。
    """
    actor = _actor_login_user_id(request)
    results: list[SkillUpdatePublishResult] = []
    for agent_id in dict.fromkeys(payload.agent_ids):
        try:
            agent, previous = await run_in_threadpool(
                runtime_repository.publish_skill_updates,
                agent_id,
                note=payload.note,
                published_by=actor,
            )
        except KeyError:
            results.append(
                SkillUpdatePublishResult(
                    agent_id=agent_id, status="skipped", reason=AGENT_NOT_FOUND_MESSAGE
                )
            )
            continue
        except ValueError as exc:
            results.append(
                SkillUpdatePublishResult(agent_id=agent_id, status="skipped", reason=str(exc))
            )
            continue
        results.append(
            SkillUpdatePublishResult(
                agent_id=agent.id,
                name=agent.name,
                status="published",
                version=agent.published_version,
                previous_version=previous,
            )
        )
    return ApiResponse(data=SkillUpdatePublishData(results=results))


@router.post("/agent-skill-updates/rollback", response_model=ApiResponse[AgentsData])
async def rollback_agent_skill_updates(
    payload: PublishedVersionRollbackRequest,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse[AgentsData]:
    """まとめての再公開を取り消す（公開中の版を前の版に戻す。下書きは変えない。#1867）。"""
    changed: list[AgentProfile] = []
    for item in payload.items:
        try:
            changed.append(
                await run_in_threadpool(
                    runtime_repository.set_published_version, item.agent_id, item.version
                )
            )
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="業務 Agent の版が見つかりません。"
            ) from exc
    return ApiResponse(data=await _agents_data(changed))


@router.post(
    "/agents/{agent_id}/versions/{version}/restore", response_model=ApiResponse[AgentProfile]
)
async def restore_agent_version(
    agent_id: str,
    version: int,
    request: Request,
    _: None = Depends(require_agent_builder),
) -> ApiResponse[AgentProfile]:
    """前の版を公開し直し、下書きもその内容にする（#770）。"""
    await _managed_agent(request, agent_id)
    try:
        agent = await run_in_threadpool(
            runtime_repository.restore_agent_version,
            agent_id,
            version,
            published_by=_actor_login_user_id(request),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="業務 Agent の版が見つかりません。") from exc
    return ApiResponse(data=agent)


class AgentShareRequestInput(BaseModel):
    """共有の申請に添える一言（#2523）。"""

    note: str = Field(default="", max_length=500)


class AgentShareRejectInput(BaseModel):
    """共有の申請を却下する理由（#2523。持ち主に見せる）。"""

    reason: str = Field(min_length=1, max_length=500)


@router.post("/agents/{agent_id}/share-request", response_model=ApiResponse[AgentProfile])
async def request_agent_share(
    agent_id: str,
    payload: AgentShareRequestInput,
    request: Request,
    _: None = Depends(require_agent_builder),
) -> ApiResponse[AgentProfile]:
    """個人の業務 Agent の共有を Agent 管理に申請する（持ち主。#2523）。"""
    agent = await _managed_agent(request, agent_id)
    if agent.owner_user_uuid is None:
        raise HTTPException(
            status_code=409, detail="組織の業務 Agent は、共有の申請をせずにロールで共有します。"
        )
    if agent.share_request is not None and agent.share_request.status == "requested":
        raise HTTPException(status_code=409, detail="共有を申請しています。結果を待ってください。")
    if agent.published_version is None:
        raise HTTPException(status_code=409, detail="公開してから共有を申請してください。")
    updated = await run_in_threadpool(
        partial(
            runtime_repository.update_agent_sharing,
            agent_id,
            owner_user_uuid=agent.owner_user_uuid,
            share_request=AgentShareRequest(
                note=payload.note.strip(), requested_by_user_uuid=agent.owner_user_uuid
            ),
        )
    )
    return ApiResponse(data=updated)


async def _requested_share(request: Request, agent_id: str) -> AgentProfile:
    agent = await _managed_agent(request, agent_id)
    if agent.share_request is None or agent.share_request.status != "requested":
        raise HTTPException(status_code=409, detail="この業務 Agent は共有を申請していません。")
    return agent


@router.post("/agents/{agent_id}/share-request/approve", response_model=ApiResponse[AgentProfile])
async def approve_agent_share(
    agent_id: str,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse[AgentProfile]:
    """共有の申請を承認する（Agent 管理。#2523）。

    組織の業務 Agent（持ち主なし）になり、権限管理でロールに割り当てて共有する。持ち主だった
    利用者も、割り当てたロールを通して使う。
    """
    agent = await _requested_share(request, agent_id)
    updated = await run_in_threadpool(
        partial(
            runtime_repository.update_agent_sharing,
            agent_id,
            owner_user_uuid=None,
            share_request=AgentShareRequest(
                status="approved",
                requested_by_user_uuid=agent.owner_user_uuid or "",
                requested_at=agent.share_request.requested_at
                if agent.share_request
                else datetime.now(UTC),
                decided_at=datetime.now(UTC),
                approved_name=agent.name,
            ),
        )
    )
    return ApiResponse(data=updated)


@router.post("/agents/{agent_id}/share-request/reject", response_model=ApiResponse[AgentProfile])
async def reject_agent_share(
    agent_id: str,
    payload: AgentShareRejectInput,
    request: Request,
    _: None = Depends(require_admin),
) -> ApiResponse[AgentProfile]:
    """共有の申請を却下する（Agent 管理。理由を持ち主に返す。#2523）。"""
    agent = await _requested_share(request, agent_id)
    share_request = agent.share_request
    if share_request is None:  # _requested_share が確かめた（型のため）
        raise HTTPException(status_code=409, detail="この業務 Agent は共有を申請していません。")
    rejected = share_request.model_copy(
        update={
            "status": "rejected",
            "reason": payload.reason.strip(),
            "decided_at": datetime.now(UTC),
        }
    )
    updated = await run_in_threadpool(
        partial(
            runtime_repository.update_agent_sharing,
            agent_id,
            owner_user_uuid=agent.owner_user_uuid,
            share_request=rejected,
        )
    )
    return ApiResponse(data=updated)


@router.patch("/agents/{agent_id}", response_model=ApiResponse[AgentProfile])
async def patch_agent(
    agent_id: str,
    patch: AgentProfilePatch,
    request: Request,
    _: None = Depends(require_agent_builder),
) -> ApiResponse[AgentProfile]:
    current = await _managed_agent(request, agent_id)
    if patch.name is not None:
        _require_agent_name(patch.name)
    if patch.data_scopes is not None:
        await _verify_data_scopes(request, current.data_scopes, patch.data_scopes)
    try:
        return ApiResponse(
            data=await run_in_threadpool(runtime_repository.patch_agent, agent_id, patch)
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=AGENT_NOT_FOUND_MESSAGE) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/agents/{agent_id}", response_model=ApiResponse[AgentsData])
async def delete_agent(
    agent_id: str,
    request: Request,
    _: None = Depends(require_agent_builder),
) -> ApiResponse[AgentsData]:
    await _managed_agent(request, agent_id)
    try:
        await run_in_threadpool(runtime_repository.delete_agent, agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=AGENT_NOT_FOUND_MESSAGE) from exc
    # 削除したエージェントをロールの対象範囲（AGENT_ROLE_AGENTS）から外す（#750）。
    # 失敗しても削除は成功のまま（権限管理の保存は、削除済みのエージェントを黙って外す）。
    try:
        await run_in_threadpool(get_security_service().remove_agent_assignments, agent_id)
    except Exception:
        logger.warning("agent_role_assignment_cleanup_failed", extra={"agent_id": agent_id})
    return ApiResponse(
        data=await _agents_data(
            [
                agent
                for agent in await run_in_threadpool(runtime_repository.list_agents)
                if _agent_allowed(request, agent.id)
            ]
        )
    )


def _mcp_connection_settings(config: McpConnectionConfig) -> McpConnectionSettings:
    settings = get_settings()
    mode = config.effective_auth_mode()
    oauth_configured = _mcp_oauth_configured(config)
    service_token_configured = len(settings.app_service_token_secret.strip()) >= 32
    credentials_ready = {
        "none": True,
        "api_key": bool(config.api_key),
        "oauth_client_credentials": oauth_configured,
        "service_token": service_token_configured,
    }[mode]
    return McpConnectionSettings(
        server_id=config.server_id,
        label=config.label,
        base_url=mask_url_credentials(config.base_url),
        base_url_masked=url_has_credentials(config.base_url),
        auth_mode=mode,
        service_audience=config.audience() if mode == "service_token" else None,
        timeout_seconds=config.timeout_seconds,
        source=config.source,
        removable=config.source == "runtime",
        base_url_locked=config.base_url_locked,
        configured=bool(config.base_url) and credentials_ready,
        api_key_configured=bool(config.api_key),
        oauth_configured=oauth_configured,
        session_configured=bool(config.session_id),
        service_token_configured=service_token_configured,
        trust_tool_annotations=config.annotations_trusted(),
    )


def _mcp_connections_response() -> McpConnectionsData:
    return McpConnectionsData(
        connections=[
            _mcp_connection_settings(config) for config in runtime_config_store.list_mcp_servers()
        ]
    )


# 標準の接続（RAG / NL2SQL）の URL を決める配備の環境変数（#1325）。
_PRODUCT_MCP_URL_ENV: dict[str, str] = {
    "rag": "AGENT_EXTERNAL_RAG_MCP_URL",
    "nl2sql": "AGENT_EXTERNAL_NL2SQL_MCP_URL",
}


def _changes_builtin_auth(current: McpConnectionConfig, patch: McpConnectionPatch) -> bool:
    """組み込みの接続（RAG / NL2SQL）の認証方式・audience を今と違う値にする変更か。"""
    if patch.auth_mode is not None and patch.auth_mode != current.effective_auth_mode():
        return True
    return patch.service_audience is not None and (
        (patch.service_audience.strip() or current.server_id) != current.audience()
    )


def _builtin_patch_error(current: McpConnectionConfig, patch: McpConnectionPatch) -> str | None:
    """標準の接続（RAG / NL2SQL）に変えられない項目を変える変更なら、その理由（#1325）。

    今と同じ値を送るのは受け付ける（画面は変えた項目だけを送るが、API の利用者は全項目を送りうる）。
    """
    if _changes_builtin_auth(current, patch):
        # Run の利用者のサービストークンで呼ぶ（再起動の後の復元も認証方式を戻す）。
        return "標準の接続（RAG / NL2SQL）の認証方式と audience は変えられません。"
    if patch.label is not None and (patch.label.strip() or None) != current.label:
        return "標準の接続（RAG / NL2SQL）の名前は変えられません。"
    if patch.trust_tool_annotations is False:
        # Agent の側がツールの性質を知っている接続なので、注釈は常に使う（#1832）。
        return "標準の接続（RAG / NL2SQL）はツールの注釈を常に使います。"
    if (
        current.base_url_locked
        and patch.base_url is not None
        and (patch.base_url or None) != current.base_url
    ):
        env_name = _PRODUCT_MCP_URL_ENV.get(current.server_id, "配備の環境変数")
        return (
            f"この接続の URL は配備（{env_name}）で設定しているため、画面・API では変えられません。"
            "変えるときは配備の設定を変えて再起動してください。"
        )
    return None


def _upsert_mcp_connection(server_id: str, payload: McpConnectionPatch) -> McpConnectionConfig:
    config = runtime_config_store.upsert_mcp_server(
        server_id,
        label=payload.label,
        base_url=payload.base_url,
        auth_mode=payload.auth_mode,
        api_key=payload.api_key,
        timeout_seconds=payload.timeout_seconds,
        session_id=payload.session_id,
        oauth_token_url=payload.oauth_token_url,
        oauth_client_id=payload.oauth_client_id,
        oauth_client_secret=payload.oauth_client_secret,
        oauth_scope=payload.oauth_scope,
        service_audience=payload.service_audience,
        trust_tool_annotations=payload.trust_tool_annotations,
    )
    _persist(lambda: control_plane_store.save_mcp_connection(config))
    return config


@router.get("/settings/mcp-connections", response_model=ApiResponse[McpConnectionsData])
async def list_mcp_connections() -> ApiResponse[McpConnectionsData]:
    """MCP 接続（RAG / NL2SQL / 外部 MCP。#757）を一覧する（資格情報は返さない）。"""
    return ApiResponse(data=_mcp_connections_response())


@router.post("/settings/mcp-connections", response_model=ApiResponse[McpConnectionSettings])
async def create_mcp_connection(
    payload: McpConnectionCreate,
    _: None = Depends(require_admin),
) -> ApiResponse[McpConnectionSettings]:
    existing = {config.server_id for config in runtime_config_store.list_mcp_servers()}
    if payload.server_id in existing:
        raise HTTPException(status_code=409, detail="MCP 接続の ID はすでに使われています。")
    config = _upsert_mcp_connection(payload.server_id, payload)
    return ApiResponse(data=_mcp_connection_settings(config))


@router.patch(
    "/settings/mcp-connections/{server_id}", response_model=ApiResponse[McpConnectionSettings]
)
async def patch_mcp_connection(
    server_id: str,
    patch: McpConnectionPatch,
    _: None = Depends(require_admin),
) -> ApiResponse[McpConnectionSettings]:
    try:
        current = runtime_config_store.get_mcp(server_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="MCP 接続が見つかりません。") from exc
    builtin_error = _builtin_patch_error(current, patch) if current.source == "builtin" else None
    if builtin_error:
        raise HTTPException(status_code=400, detail=builtin_error)
    config = _upsert_mcp_connection(server_id, patch)
    return ApiResponse(data=_mcp_connection_settings(config))


@router.delete(
    "/settings/mcp-connections/{server_id}", response_model=ApiResponse[McpConnectionsData]
)
async def delete_mcp_connection(
    server_id: str,
    _: None = Depends(require_admin),
) -> ApiResponse[McpConnectionsData]:
    try:
        runtime_config_store.remove_mcp_server(server_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="MCP 接続が見つかりません。") from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="標準の接続（RAG / NL2SQL）・宣言の MCP 接続は削除できません。",
        ) from exc
    _persist(lambda: control_plane_store.delete_mcp_connection(server_id))
    return ApiResponse(data=_mcp_connections_response())


@router.get(
    "/settings/mcp-connections/{server_id}/tools",
    response_model=ApiResponse[ExternalMcpToolsData],
)
async def list_mcp_connection_tool_definitions(
    server_id: str,
    request: Request,
) -> ApiResponse[ExternalMcpToolsData]:
    """接続のツール一覧（`tools/list`）。接続の確認を兼ね、ログイン中の利用者として呼ぶ。"""
    context = ToolInvocationContext(user_uuid=_run_creator_user_uuid(request))
    try:
        data = await asyncio.to_thread(list_mcp_connection_tools, server_id, context=context)
        config = runtime_config_store.get_mcp(server_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="MCP 接続が見つかりません。") from exc
    except ExternalToolError as exc:
        status_code = 400 if exc.code.endswith("_not_configured") else 502
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": exc.message, "details": exc.details},
        ) from exc
    # 「既定に従う」のときの判定（注釈の信頼・標準の接続の既定を含む。Run と同じ。#1832）。
    default_policy = ToolPolicy(default_mode=runtime_config_store.get_tool_policy().default_mode)
    for tool in data.tools:
        tool.default_decision = default_policy.decide(mcp_tool_definition(config, tool))
    return ApiResponse(data=data)


@router.get("/settings/tool-policy", response_model=ApiResponse[ToolPolicySettings])
async def get_tool_policy_settings() -> ApiResponse[ToolPolicySettings]:
    return ApiResponse(data=_tool_policy_settings_response())


@router.get("/settings/tool-policy/tools", response_model=ApiResponse[ToolPolicyToolsData])
async def list_tool_policy_tools(request: Request) -> ApiResponse[ToolPolicyToolsData]:
    """ツール権限の画面のツール（Control Plane のツールと MCP 接続のツール。#1814）。

    MCP 接続のツールはログイン中の利用者として `tools/list` で取る（取れない接続は理由を返す）。
    """
    context = ToolInvocationContext(user_uuid=_run_creator_user_uuid(request))
    return ApiResponse(data=await asyncio.to_thread(tool_policy_catalog, context))


@router.patch("/settings/tool-policy", response_model=ApiResponse[ToolPolicySettings])
async def patch_tool_policy_settings(
    patch: ToolPolicySettingsPatch,
    _: None = Depends(require_admin),
) -> ApiResponse[ToolPolicySettings]:
    try:
        _validate_tool_policy_patch(patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    policy = runtime_config_store.patch_tool_policy(
        default_mode=patch.default_mode,
        allow=patch.allow,
        ask=patch.ask,
        deny=patch.deny,
    )
    _persist(lambda: control_plane_store.save_tool_policy(policy))
    return ApiResponse(data=_tool_policy_settings_response())


def _sse_events(events: Iterable[RunEvent | None]) -> Iterable[str]:
    for event in events:
        if event is None:
            yield ": keepalive\n\n"
            continue
        payload = event.model_dump(mode="json")
        yield f"event: {event.type}\n"
        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _configured_tool_policy() -> ToolPolicy:
    config = runtime_config_store.get_tool_policy()
    return ToolPolicy(
        default_mode=config.default_mode,
        allow=config.allow,
        ask=config.ask,
        deny=config.deny,
    )


def _request_principal(connection: object) -> Principal | None:
    """認証済みの利用者（Cookie のセッション、または local のローカル利用者）。

    `/api` の HTTP は `app.security.dependencies.authorize_api_request` が `state.principal` に
    入れる。無ければ未認証。
    """
    state = getattr(connection, "state", None)
    principal = getattr(state, "principal", None) if state is not None else None
    return principal if isinstance(principal, Principal) else None


def _actor_policy(connection: object) -> ActorPolicy:
    """利用者から作る ActorPolicy（#215 / #750）。

    capability → 従来のロール、対象範囲は利用者の許可（None は制限なし）。RAG / NL2SQL と同じく
    共通認証の利用者だけで判定し、利用者がいなければ何も許可しない。
    """
    principal = _request_principal(connection)
    if principal is None:
        return ActorPolicy(roles=set(), agent_ids=set())
    agent_ids = (
        None
        if principal.allowed_agent_ids is None
        # 持ち主の個人の業務 Agent は、ロールに割り当てなくても使える（#2523）。
        else set(principal.allowed_agent_ids)
        | runtime_repository.owned_agent_ids(principal.user_uuid)
    )
    return ActorPolicy(
        roles=actor_roles_for_principal(principal),
        agent_ids=agent_ids,
        user_uuid=principal.user_uuid,
        can_build=principal.has_permission(AGENTS_BUILD),
    )


def _policy_allows_agent(policy: ActorPolicy, agent_id: str) -> bool:
    allowed = policy.agent_ids
    return allowed is None or agent_id in allowed


def _policy_has_roles(policy: ActorPolicy, allowed_roles: set[str]) -> bool:
    return "admin" in policy.roles or bool(policy.roles.intersection(allowed_roles))


def _policy_manages_agent(policy: ActorPolicy, agent: AgentProfile) -> bool:
    """業務 Agent を変更・下書きで実行・公開できるか（Agent 管理か、個人の Agent の持ち主）。"""
    if "admin" in policy.roles:
        return True
    return (
        policy.can_build
        and policy.user_uuid is not None
        and agent.owner_user_uuid == policy.user_uuid
    )


async def _managed_agent(request: Request, agent_id: str) -> AgentProfile:
    """変更する業務 Agent（使えなければ 404、変更できなければ 403。#2523）。"""
    agents = await run_in_threadpool(runtime_repository.list_agents)
    agent = next((item for item in agents if item.id == agent_id), None)
    if agent is None or not _agent_allowed(request, agent_id):
        raise HTTPException(status_code=404, detail=AGENT_NOT_FOUND_MESSAGE)
    if not _policy_manages_agent(_actor_policy(request), agent):
        raise HTTPException(status_code=403, detail=AGENT_MANAGE_FORBIDDEN_MESSAGE)
    return agent


def _actor_login_user_id(connection: object) -> str:
    """版の公開者として記録するログイン ID。画面は一覧の応答の表示名で出す（#1696）。"""
    principal = _request_principal(connection)
    return principal.login_user_id if principal is not None else "anonymous"


def _require_actor_roles(request: Request, allowed_roles: set[str]) -> None:
    if _request_principal(request) is None:
        raise HTTPException(status_code=401, detail="ログインしてください。")
    if _policy_has_roles(_actor_policy(request), allowed_roles):
        return
    # 経路の権限拒否（SECURITY_ROUTE_FORBIDDEN）ではないので error_code を付けない。
    raise HTTPException(status_code=403, detail=_capability_denied_message(allowed_roles))


def _capability_denied_message(allowed_roles: set[str]) -> str:
    """capability（従来のロール）が足りないときの文。権限の名前は権限管理と同じ（カタログの並び）。"""
    roles = allowed_roles | {"admin"}
    labels = [item.label for item in PERMISSION_CATALOG if CAPABILITY_ROLES.get(item.code) in roles]
    return f"この操作を行う権限がありません。必要な権限（いずれか）: {'、'.join(labels)}"


def _run_creator_user_uuid(request: Request) -> str | None:
    """Run を作る利用者（#233）。Cookie のセッションの利用者か、local のローカル利用者。"""
    principal = _request_principal(request)
    return principal.user_uuid if principal is not None else None


def _filter_runs_for_actor[RunLike: (RunState, RunSummary, ApprovalSummary)](
    request: Request, runs: list[RunLike]
) -> list[RunLike]:
    """利用者が見られる Run（・その要約・承認の要約）だけにする（業務 Agent の対象範囲）。"""
    policy = _actor_policy(request)
    if policy.agent_ids is None:
        return runs
    return [run for run in runs if _policy_allows_agent(policy, run.agent_id)]


def _sees_all_users(connection: object) -> bool:
    """「AI 活用」の画面で全員のデータを見られるか（system_admin だけ。#1889）。"""
    return can_view_all_users_data(_request_principal(connection))


def _run_visible_to(connection: object, run: RunState | RunSummary) -> bool:
    """「AI 活用」の画面で Run を見て、運用の操作（停止・削除）をしてよいか（#1889・#1954）。

    実行した利用者・その会話の持ち主（system_admin が持ち主の会話で実行した Run も、持ち主の
    会話の往復として読み、停止できる）・system_admin。`agent.admin` などの管理権限は理由にしない。
    持ち主の分からない古い Run は system_admin だけ。内容を足す操作（確認への回答・評価）は
    `_run_content_addable_by`（持ち主だけ。#3244）。
    """
    principal = _request_principal(connection)
    if can_access_user_data(principal, run.created_by_user_uuid):
        return True
    owner = run_thread_owner(run) if isinstance(run, RunState) else run.thread_owner_user_uuid
    return owner is not None and can_access_user_data(principal, owner)


def _run_approvable_by(connection: object, run: RunState | RunSummary | ApprovalSummary) -> bool:
    """Run の承認を見て判断してよいか（実行した利用者か system_admin。#1889・#1928）。

    承認したツールは Run の利用者の権限（サービストークンの `sub`）で実行するため、会話の持ち主
    でも、ほかの利用者（system_admin）が実行した Run の承認は判断しない。
    """
    return can_access_user_data(_request_principal(connection), run.created_by_user_uuid)


def _run_content_addable_by(connection: object, run: RunState) -> bool:
    """Run に内容（回答前の確認への答え・評価）を足してよいか（Run の持ち主だけ。#3244）。

    持ち主は会話の持ち主（会話の無い Run は実行した利用者）。system_admin も他の利用者の Run には
    足せない（閲覧と運用の操作は `_run_visible_to`）。
    """
    return can_add_user_content(_request_principal(connection), run_thread_owner(run))


def _may_continue_thread(connection: object, thread_id: str) -> bool:
    """会話に続きの質問を足してよいか（会話の持ち主だけ。#3244）。

    system_admin でない利用者は自分の会話しか見えないので True を返し、ほかの利用者の会話・無い
    会話は Run を作るときに保存先が 404（`ThreadNotFoundError`）にする。system_admin は、見える
    他の利用者の会話なら False（403）。
    """
    if not _sees_all_users(connection):
        return True
    principal = _request_principal(connection)
    try:
        thread = runtime_repository.get_thread(
            thread_id,
            user_uuid=principal.user_uuid if principal is not None else None,
            all_users=True,
        )
    except ThreadNotFoundError:
        return True
    return can_add_user_content(principal, thread.owner_user_uuid)


def _raise_others_content_forbidden() -> NoReturn:
    """他の利用者の会話・Run に内容を足そうとした（system_admin。#3244）。"""
    raise HTTPException(
        status_code=OTHERS_CONTENT_FORBIDDEN_STATUS, detail=OTHERS_CONTENT_FORBIDDEN_MESSAGE
    )


def _run_owner_of(run: RunState | RunSummary) -> str | None:
    """Run の会話の持ち主（会話の無い Run・古い Run は実行した利用者。#1928）。"""
    if isinstance(run, RunState):
        return run_thread_owner(run)
    return run.thread_owner_user_uuid or run.created_by_user_uuid


async def _run_people_names(runs: Sequence[RunState | RunSummary]) -> dict[str, str]:
    """Run を実行した人と会話の持ち主の表示名（UUID → 表示名。引けない利用者は入らない）。"""
    wanted = sorted(
        {uuid for run in runs for uuid in (run.created_by_user_uuid, _run_owner_of(run)) if uuid}
    )
    return await run_in_threadpool(user_display_names, wanted) if wanted else {}


def _run_viewer(
    connection: object, run: RunState | RunSummary, names: Mapping[str, str]
) -> RunViewer:
    """見ている利用者から見た Run の事実（実行者・会話の持ち主・待ちの操作の可否。#1954）。

    実行した人の表示名は見る人を問わず返す（持ち主の画面の「〈名前〉が実行」と、待っている相手）。
    """
    principal = _request_principal(connection)
    me = principal.user_uuid if principal is not None else None
    executor = run.created_by_user_uuid
    owner = _run_owner_of(run)
    in_thread = run.thread_id is not None
    return RunViewer(
        executed_by_me=bool(executor) and executor == me,
        executor_display_name=names.get(executor or "", ""),
        owned_by_me=(bool(owner) and owner == me) if in_thread else None,
        conversation_owner_display_name=names.get(owner or "", "") if in_thread else None,
        can_decide=_run_approvable_by(connection, run),
    )


def _actor_login_id(connection: object) -> str | None:
    """操作した利用者のログイン ID（イベント・ログに残す actor。#1928）。"""
    principal = _request_principal(connection)
    return principal.login_user_id if principal is not None else None


def _ai_use_runs_for_actor[RunLike: (RunState, RunSummary)](
    request: Request, runs: list[RunLike]
) -> list[RunLike]:
    """実行履歴の一覧の Run（業務 Agent の範囲と、system_admin でなければ自分の Run・自分の
    会話の Run。#1889・#1928）。

    改善・運用（フィードバック・利用状況・監査ログ）の集計は `_filter_runs_for_actor` のまま。
    """
    return [run for run in _filter_runs_for_actor(request, runs) if _run_visible_to(request, run)]


def _require_agent_access(request: Request, agent_id: str) -> None:
    if _agent_allowed(request, agent_id):
        return
    raise HTTPException(status_code=403, detail=AGENT_FORBIDDEN_MESSAGE)


def _agent_allowed(request: Request, agent_id: str) -> bool:
    return _policy_allows_agent(_actor_policy(request), agent_id)


def _request_agent_ids(request: Request) -> set[str] | None:
    """利用者が使えるエージェント（None は制限なし）。"""
    return _actor_policy(request).agent_ids


def _run_for_approval(approval_id: str) -> RunState:
    return runtime_repository.get_run_for_approval(approval_id)


def _tool_policy_settings_response() -> ToolPolicySettings:
    config = runtime_config_store.get_tool_policy()
    return ToolPolicySettings(
        default_mode=config.default_mode,
        allow=sorted(config.allow),
        ask=sorted(config.ask),
        deny=sorted(config.deny),
    )


def _validate_tool_policy_patch(patch: ToolPolicySettingsPatch) -> None:
    if patch.default_mode is not None and patch.default_mode not in {"approval", "deny"}:
        raise ValueError("default_mode must be approval or deny")
    registered_tools = set(tool_registry.names())
    connections = {config.server_id for config in runtime_config_store.list_mcp_servers()}
    unknown_tools = sorted(
        {
            name
            for names in (patch.allow, patch.ask, patch.deny)
            if names is not None
            for name in names
            if name not in registered_tools and not _is_mcp_function_name(name, connections)
        }
    )
    if unknown_tools:
        raise ValueError(f"unknown tool: {', '.join(unknown_tools)}")


def _persist(action: Callable[[], None]) -> None:
    """変更を保存する（#764）。保存できなければ 503（変更は再起動で失われる）。"""
    try:
        action()
    except ControlPlaneStoreError as exc:
        logger.warning("agent_control_plane_persist_failed", extra={**safe_exception_fields(exc)})
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _is_mcp_function_name(name: str, connections: set[str]) -> bool:
    """MCP 接続のツール（`<接続>__<ツール>`。#757）の名前か（ツールの存在は呼び先が決める）。"""
    server_id, separator, tool_name = name.partition(MCP_TOOL_SEPARATOR)
    return bool(separator and tool_name) and server_id in connections


def _mcp_oauth_configured(config: object) -> bool:
    return bool(
        getattr(config, "oauth_token_url", None)
        and getattr(config, "oauth_client_id", None)
        and getattr(config, "oauth_client_secret", None)
    )


def _normalized_command_prefixes(prefixes: list[str]) -> list[str]:
    return sorted({prefix.strip() for prefix in prefixes if prefix.strip()})


def _normalized_tool_names(tool_names: list[str]) -> list[str]:
    return sorted({name.strip() for name in tool_names if name.strip()})


def _step_tool_name(step: RunStep) -> str:
    """監査の記録のツール名（ツールの呼び出しの無い step は step の種類）。"""
    return step.tool_call.name if step.tool_call else step.kind


def _run_audit_data(run: RunState) -> RunAuditData:
    approvals = {approval.id: approval for approval in run.approvals}
    artifact_ids_by_step: dict[str, list[str]] = {}
    for event in run.events:
        if event.type != "artifact.created":
            continue
        step_id = event.payload.get("step_id")
        artifact_id = event.payload.get("artifact_id")
        if isinstance(step_id, str) and isinstance(artifact_id, str):
            artifact_ids_by_step.setdefault(step_id, []).append(artifact_id)

    records: list[ToolAuditRecord] = []
    for step in run.steps:
        tool_name = _step_tool_name(step)
        result = step.tool_result
        approval = approvals.get(step.approval_id or "")
        audit_metadata = result.audit_metadata if result is not None else {}
        records.append(
            ToolAuditRecord(
                step_id=step.id,
                tool_name=tool_name,
                status=step.status.value,
                approval_id=step.approval_id,
                approval_status=approval.status.value if approval else None,
                policy_decision=result.policy_decision.value if result else None,
                permission_level=_audit_text(audit_metadata, "permission_level"),
                side_effects=_audit_bool(audit_metadata, "side_effects"),
                started_at=step.started_at.isoformat() if step.started_at else None,
                completed_at=step.completed_at.isoformat() if step.completed_at else None,
                duration_ms=result.duration_ms if result else None,
                success=result.success if result else None,
                error=result.error if result else None,
                error_code=result.error_code if result else None,
                guardrail_warnings=result.guardrail_warnings if result else [],
                trace_id=step.tool_call.trace_id if step.tool_call else None,
                artifact_ids=artifact_ids_by_step.get(step.id, []),
                audit_metadata=audit_metadata,
            )
        )
    return RunAuditData(
        run_id=run.id,
        goal=run.goal,
        status=run.status.value,
        records=records,
    )


def _tool_call_audit_data(
    *,
    request: Request,
    run_id: str | None,
    tool_name: str | None,
    status: str | None,
    approval_status: str | None,
    error_code: str | None,
    has_guardrail_warnings: bool | None,
    paging: OffsetParams,
) -> ToolCallAuditData:
    filters = _audit_filters(
        run_id=run_id,
        tool_name=tool_name,
        status=status,
        approval_status=approval_status,
        error_code=error_code,
        has_guardrail_warnings=has_guardrail_warnings,
    )
    projection_reader = getattr(runtime_repository, "list_tool_call_audit_projection", None)
    if callable(projection_reader) and _request_agent_ids(request) is None:
        try:
            projection = projection_reader(
                run_id=run_id,
                tool_name=tool_name,
                status=status,
                approval_status=approval_status,
                error_code=error_code,
                has_guardrail_warnings=has_guardrail_warnings,
                offset=paging.offset,
                limit=paging.limit,
            )
            if isinstance(projection, RuntimeToolCallAuditData):
                return ToolCallAuditData(
                    **dict(
                        paginate(
                            [
                                _with_run_origin(
                                    ToolCallAuditRecord.model_validate(record.model_dump())
                                )
                                for record in projection.items
                            ],
                            total=projection.total,
                            limit=projection.limit,
                            offset=projection.offset,
                        )
                    ),
                    filters=filters,
                    tool_names=projection.tool_names,
                )
        except RuntimeError:
            pass

    records: list[ToolCallAuditRecord] = []
    tool_names: set[str] = set()
    for run in _filter_runs_for_actor(request, runtime_repository.list_runs()):
        tool_names.update(_step_tool_name(step) for step in run.steps)
        if run_id is not None and run.id != run_id:
            continue
        audit = _run_audit_data(run)
        for record in audit.records:
            enriched = ToolCallAuditRecord(
                **record.model_dump(),
                run_id=run.id,
                run_goal=run.goal,
                run_status=run.status.value,
                agent_id=run.agent_id,
                run_created_at=run.created_at.isoformat(),
                run_updated_at=run.updated_at.isoformat(),
                **_run_origin(run.metadata),
            )
            if _audit_record_matches(
                enriched,
                tool_name=tool_name,
                status=status,
                approval_status=approval_status,
                error_code=error_code,
                has_guardrail_warnings=has_guardrail_warnings,
            ):
                records.append(enriched)
    return ToolCallAuditData(
        **dict(paginate_slice(records, paging)),
        filters=filters,
        tool_names=sorted(tool_names),
    )


def _audit_filters(
    *,
    run_id: str | None,
    tool_name: str | None,
    status: str | None,
    approval_status: str | None,
    error_code: str | None,
    has_guardrail_warnings: bool | None,
) -> dict[str, object]:
    filters: dict[str, object] = {}
    if run_id is not None:
        filters["run_id"] = run_id
    if tool_name is not None:
        filters["tool_name"] = tool_name
    if status is not None:
        filters["status"] = status
    if approval_status is not None:
        filters["approval_status"] = approval_status
    if error_code is not None:
        filters["error_code"] = error_code
    if has_guardrail_warnings is not None:
        filters["has_guardrail_warnings"] = has_guardrail_warnings
    return filters


def _audit_record_matches(
    record: ToolCallAuditRecord,
    *,
    tool_name: str | None,
    status: str | None,
    approval_status: str | None,
    error_code: str | None,
    has_guardrail_warnings: bool | None,
) -> bool:
    if tool_name is not None and record.tool_name != tool_name:
        return False
    if status is not None and record.status != status:
        return False
    if approval_status is not None and record.approval_status != approval_status:
        return False
    if error_code is not None and record.error_code != error_code:
        return False
    return not (
        has_guardrail_warnings is not None
        and bool(record.guardrail_warnings) != has_guardrail_warnings
    )


def _tool_call_audit_csv(records: list[ToolCallAuditRecord]) -> str:
    fieldnames = [
        "run_id",
        "run_goal",
        "run_status",
        "agent_id",
        "step_id",
        "tool_name",
        "status",
        "approval_id",
        "approval_status",
        "policy_decision",
        "permission_level",
        "side_effects",
        "started_at",
        "completed_at",
        "duration_ms",
        "success",
        "error_code",
        "error",
        "guardrail_warnings",
        "trace_id",
        "artifact_ids",
        "run_created_at",
        "run_updated_at",
        "run_source",
        "api_key_name",
    ]
    output = StringIO()
    # BOM を付ける（日本語の Excel は BOM の無い CSV を Shift_JIS として読み、文字化けする。#983）。
    output.write("\ufeff")
    writer = DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for record in records:
        row = record.model_dump(mode="json")
        row["guardrail_warnings"] = "|".join(record.guardrail_warnings)
        row["artifact_ids"] = "|".join(record.artifact_ids)
        writer.writerow({key: _csv_cell(value) for key, value in row.items()})
    return output.getvalue()


# 表計算ソフトが数式として扱う先頭の文字（OWASP の CSV injection。#983）。
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value: object) -> object:
    """利用者の入力（実行の目標・エラーなど）が数式として実行されないよう、先頭に `'` を付ける。"""
    if isinstance(value, str) and value.startswith(_CSV_FORMULA_PREFIXES):
        return f"'{value}"
    return value


def _audit_text(metadata: dict[str, object], key: str) -> str | None:
    value = metadata.get(key)
    if isinstance(value, str):
        return value
    return None


def _audit_bool(metadata: dict[str, object], key: str) -> bool | None:
    value = metadata.get(key)
    if isinstance(value, bool):
        return value
    return None
