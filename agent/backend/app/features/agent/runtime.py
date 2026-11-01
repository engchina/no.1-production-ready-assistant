"""業務 Agent の Run・Event・Artifact・Approval の保存と、組み込み Runtime の実行の記録。

永続 DB 導入前でも API / 権限 / 承認 / SSE を検証できるよう、
append-only event log をプロセス内 repository として実装する。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from threading import Condition, Lock, RLock
from typing import Any, Literal, Protocol, TypeVar, cast
from uuid import uuid4

from pr_backend_core import Page
from pr_backend_core.api import offset_fetch_binds, offset_fetch_clause, paginate
from pr_backend_core.chat_history import ChatHistoryTurn
from pr_backend_core.chat_history_window import ChatHistoryPage
from pr_backend_core.chat_progress import ChatProgressStepDetails
from pr_backend_core.observability.mcp_trace import operation_trace_parent
from pr_backend_core.observability.tracing import (
    SpanUpdate,
    capture_trace_context,
    finish_operation_trace,
    validated_trace_context,
)
from pr_backend_core.oracle_errors import is_oracle_connection_error, oracle_error_codes
from pr_backend_core.version_retention import DEFAULT_MAX_PUBLISHED_VERSIONS, versions_to_prune
from pr_system_settings.chat_attachments import ChatAttachmentReference
from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    computed_field,
    field_serializer,
    field_validator,
    model_validator,
)

from app.features.agent import call_abort, run_claim, storage_backend
from app.features.agent import runtime_checkpoint_store as checkpoint_store
from app.features.agent.chat_progress import note_run_span
from app.features.agent.clarification_cancel import (
    CANCEL_METADATA_KEY,
    button_record,
    kind_label,
)
from app.features.agent.config import runtime_config_store
from app.features.agent.confirmation import (
    MAX_ROUNDS_LIMIT,
    POLICY_CONFIRM,
    RESPONSE_METADATA_KEY,
    ROUND_METADATA_KEY,
    SOURCE_AGENT,
    WITHDRAWN_METADATA_KEY,
    ClarificationPolicy,
    ConfirmationAnswer,
    ConfirmationRequest,
    response_goal,
    response_record,
    validate_answers,
    validate_max_rounds,
)
from app.features.agent.confirmation import METADATA_KEYS as CONFIRMATION_METADATA_KEYS
from app.features.agent.data_scope import AgentDataScopes, normalize_data_scopes
from app.features.agent.output_fields import AgentOutputField, validate_output_fields
from app.features.agent.run_budget import RUN_BUDGET_METADATA_KEY
from app.features.agent.run_claim import RunClaim
from app.features.agent.skills import (
    BUSINESS_RAG_RESEARCH_SKILL_ID,
    RAG_AND_STRUCTURED_DATA_SKILL_ID,
    STRUCTURED_DATA_QUERY_SKILL_ID,
    PinnedSkill,
    pin_skill,
    skill_registry,
)
from app.features.agent.support_task import (
    SUPPORT_TASK_KIND,
    SUPPORT_TASK_NAME,
    abandoned_run_consumption,
    with_abandoned_consumption,
)
from app.features.agent.tools import (
    ToolCall,
    ToolInvocationContext,
    ToolPolicy,
    ToolPolicyDecision,
    ToolResult,
    mcp_base_tool_name,
    set_run_finished_check,
)
from app.observability import record_runtime_event
from app.oracle_connection import connect_platform_oracle
from app.settings import get_settings

logger = logging.getLogger(__name__)
# Run・業務 Agent の Oracle のテーブル（作成はシステムテーブル。`app.system_schema`。#764）。
RUNTIME_CHECKPOINT_TABLE = "AGENT_RUNTIME_CHECKPOINTS"
RUNTIME_PROJECTION_PREFIX = "AGENT_RUNTIME"
JsonObject = dict[str, Any]
OracleConnectFactory = Callable[[], Any]

# 組み込み Runtime（#754）の Runtime ID と、承認待ちの Run に保存する SDK の状態の key。
BUILTIN_RUNTIME_ID = "builtin"
_T = TypeVar("_T")
_BUILTIN_STATE_KEY = "_builtin_sdk_state"
# backend の再起動で実行が中断した Run の失敗（#1894。in_process の実行は backend のプロセスの
# task だけで、再起動の後に拾い直す仕組みが無い）。
INTERRUPTED_ERROR_CODE = "runtime.interrupted"
INTERRUPTED_MESSAGE = "サーバーの再起動で実行が中断されました。もう一度送信してください。"


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    """時差の無い日時（古い保存の値）は UTC とみなす（比べられるようにする）。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    # 回答前の確認（#1839）: Agent 自身の確認（`ask_user_confirmation`）で利用者の答えを待つ。
    WAITING_CONFIRMATION = "waiting_confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class RunEventType(StrEnum):
    RUN_CREATED = "run.created"
    RUN_REPLAYED = "run.replayed"
    RUN_STATUS_CHANGED = "run.status_changed"
    STEP_STARTED = "step.started"
    TOOL_APPROVAL_REQUIRED = "tool.approval_required"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    TOOL_GUARDRAIL_WARNING = "tool.guardrail_warning"
    SKILL_PLANNED = "skill.planned"
    APPROVAL_DECIDED = "approval.decided"
    ARTIFACT_CREATED = "artifact.created"
    RUN_COMPLETED = "run.completed"
    RUN_CANCELLED = "run.cancelled"
    RUNTIME_SUBMITTED = "runtime.submitted"
    RUNTIME_EVENT = "runtime.event"
    RUNTIME_FAILED = "runtime.failed"
    RUNTIME_DISPATCH_CLAIMED = "runtime.dispatch_claimed"
    # 旧エンジン（#756 で削除）が記録したイベント。保存済みの Run を読み込むためだけに残す。
    MEMORY_WRITTEN = "memory.written"
    PLANNER_COMPLETED = "planner.completed"


class ArtifactContentRef(BaseModel):
    backend: str
    uri: str
    content_type: str = "application/json"
    size_bytes: int | None = None
    sha256: str | None = None


class Artifact(BaseModel):
    id: str = Field(default_factory=lambda: f"artifact_{uuid4().hex}")
    name: str
    kind: str
    content: JsonObject
    content_ref: ArtifactContentRef | None = None
    created_at: datetime = Field(default_factory=_now)


class RunEvent(BaseModel):
    id: str = Field(default_factory=lambda: f"event_{uuid4().hex}")
    run_id: str
    type: RunEventType
    message: str
    payload: JsonObject = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=_now)


class RunStep(BaseModel):
    id: str = Field(default_factory=lambda: f"step_{uuid4().hex}")
    run_id: str
    kind: str = "tool"
    status: StepStatus = StepStatus.PENDING
    tool_call: ToolCall | None = None
    tool_result: ToolResult | None = None
    approval_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class ApprovalRequest(BaseModel):
    id: str = Field(default_factory=lambda: f"approval_{uuid4().hex}")
    run_id: str
    step_id: str
    tool_call: ToolCall
    status: ApprovalStatus = ApprovalStatus.PENDING
    reason: str
    decided_by: str | None = None
    decided_at: datetime | None = None
    created_at: datetime = Field(default_factory=_now)


# スキルの更新の扱い（#1867）。pinned = 公開の版に固定した中身で動く（既定・推奨。#1855）、
# latest = 公開中の版でも今のスキルで動く（開発・検証用。更新の印を出さない）。
SkillPolicy = Literal["pinned", "latest"]
SKILL_POLICY_PINNED: SkillPolicy = "pinned"
SKILL_POLICY_LATEST: SkillPolicy = "latest"

# 版に残す業務 Agent の項目（下書き = AgentProfile の同名の項目。#770）。
AGENT_VERSIONED_FIELDS: tuple[str, ...] = (
    "name",
    "description",
    "instructions",
    "skill_ids",
    "model_id",
    "data_scopes",
    "clarification_policy",
    "clarification_max_rounds",
    "skill_policy",
    "output_fields",
)


def _unique_skill_ids(value: list[str]) -> list[str]:
    """同じスキルを 2 回持たない（順序は保つ。改名の migration で重なった ID も 1 つ。#1816）。"""
    return list(dict.fromkeys(value))


class AgentVersion(BaseModel):
    """公開した業務 Agent の版（#770）。利用者の Run は公開中の版の内容で実行する。"""

    version: int
    name: str
    description: str = ""
    instructions: str = ""
    skill_ids: list[str] = Field(default_factory=list)
    model_id: str = ""
    data_scopes: AgentDataScopes = Field(default_factory=dict)
    clarification_policy: ClarificationPolicy = POLICY_CONFIRM
    clarification_max_rounds: int = Field(default=0, ge=0, le=MAX_ROUNDS_LIMIT)
    _check_clarification_max_rounds = field_validator("clarification_max_rounds")(
        validate_max_rounds
    )
    # スキルの更新の扱い（#1867。latest は公開中の版でも今のスキルで動く）。
    skill_policy: SkillPolicy = SKILL_POLICY_PINNED
    # 出力の項目（#2549）。
    output_fields: list[AgentOutputField] = Field(default_factory=list)
    note: str = ""
    published_at: datetime = Field(default_factory=_now)
    published_by: str | None = None
    # 公開のときに固定したスキルの中身（#1855）。None は固定していない版（#1855 より前の版・
    # 起動時に作った版）で、Run は今のスキルで動く。
    skills: list[PinnedSkill] | None = None

    @field_validator("skill_ids")
    @classmethod
    def _unique_skills(cls, value: list[str]) -> list[str]:
        return _unique_skill_ids(value)


class AgentShareRequest(BaseModel):
    """個人の業務 Agent の共有の申請と、申請者へ返す承認の記録。"""

    status: Literal["requested", "rejected", "approved"] = "requested"
    note: str = Field(default="", max_length=500)
    requested_at: datetime = Field(default_factory=_now)
    requested_by_user_uuid: str = ""
    approved_name: str = ""
    # 却下の理由と時刻（status が rejected のとき）。
    reason: str = Field(default="", max_length=500)
    decided_at: datetime | None = None


# 業務 Agent に直接は書けないツールの指定（#3240）。業務 Agent が使うツールは、選んだスキルの
# `mcp_requirements` からだけ決める（`builtin_runtime.agent_mcp_requirements`）。作成・変更の API は
# これらの項目を黙って捨てずに 422 で拒む（未対応の指定を成功にしない）。
DIRECT_TOOL_FIELDS: tuple[str, ...] = ("tool_names", "mcp_requirements")


def reject_direct_tool_fields(value: Any) -> Any:
    """作成・変更の要求に、業務 Agent へのツールの直接の指定があれば拒む（#3240）。"""
    if isinstance(value, Mapping):
        found = [name for name in DIRECT_TOOL_FIELDS if name in value]
        if found:
            raise ValueError(
                f"業務 Agent にツール（{'・'.join(found)}）は指定できません。"
                "使うツールはスキルで宣言し、skill_ids でスキルを選んでください。"
            )
    return value


class AgentProfile(BaseModel):
    id: str = Field(default_factory=lambda: f"agent_{uuid4().hex}")
    name: str
    description: str = ""
    instructions: str = ""
    skill_ids: list[str] = Field(default_factory=list)
    # 組み込み Runtime で使うモデル（モデルの API の model_id）。
    # 空なら既定のテキストモデル（#754）。
    model_id: str = ""
    # データの範囲（MCP 接続 → 使えるプロファイルの一覧と既定。#1378）。空の接続は範囲なし
    # （Run の利用者が使えるすべてのプロファイルから、モデルが選ぶ）。
    data_scopes: AgentDataScopes = Field(default_factory=dict)
    # 回答前の確認（#1839）。曖昧な質問の扱い（`confirm` = 確認してから回答する、`assume` = 前提を
    # 示して回答する）と、確認の上限（往復。最初の確認を 1 回目に数える。0 は既定の 3 回、2〜6。
    # NL2SQL の業務プロファイルと同じ名前・意味）。
    clarification_policy: ClarificationPolicy = POLICY_CONFIRM
    clarification_max_rounds: int = Field(default=0, ge=0, le=MAX_ROUNDS_LIMIT)
    _check_clarification_max_rounds = field_validator("clarification_max_rounds")(
        validate_max_rounds
    )
    # スキルの更新の扱い（#1867）。pinned（既定）は公開の版に固定したスキル、latest は今のスキル。
    # 版の項目なので、変えたら公開して反映する。
    skill_policy: SkillPolicy = SKILL_POLICY_PINNED
    # 出力の項目（#2549。key・表示名・型・必須。Run に `record_output` を渡す）。
    output_fields: list[AgentOutputField] = Field(default_factory=list)
    # 使うツールは持たない（スキルの `mcp_requirements` からだけ決める。#3240）。直接の指定は
    # 作成・変更の API が 422 で拒み（`reject_direct_tool_fields`）、保存済みの旧い値
    # （`tool_names`・`migration_required`）は読むときに無視する（未リリースのため互換を持たない）。
    enabled: bool = True
    # 由来層: builtin(default)/ runtime(UI/API)。
    source: str = "runtime"
    # 作成に使った業種テンプレート（#780 / #810）。
    # テンプレートの評価ケースで評価セットを作るときに使う。
    # 空はテンプレートを使っていない（#810 より前の Agent も空のまま読める）。
    template_id: str = Field(default="", max_length=100)
    # 版（#770）。上の名前〜モデルは下書きで、公開すると版になる。利用者の Run は公開中の版で動く。
    # versioned=False は #770 より前の Agent（読み込み時に現在の内容を v1 として公開する）。
    versioned: bool = False
    versions: list[AgentVersion] = Field(default_factory=list)
    published_version: int | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    # 下書き（版の項目）を最後に変えた時刻（#2519）。公開前の評価の門は、この時刻より後に始めた
    # 下書きの評価だけを数える。None は #2519 より前の業務 Agent（`created_at` を使う）。
    draft_updated_at: datetime | None = None
    # 個人の業務 Agent の持ち主（#2523。`agent.agents.build` だけの利用者が作った Agent）。
    # None は組織の業務 Agent（管理は Agent 管理、利用はロールへの割り当て）。持ち主は自分の
    # Agent を管理し、使える。
    owner_user_uuid: str | None = None
    share_request: AgentShareRequest | None = None
    # Run が使う版に固定したスキル（`agent_for_run` が入れる。保存・応答には出さない。#1855）。
    pinned_skills: list[PinnedSkill] | None = Field(default=None, exclude=True)
    # Run が使うスキルの宣言（Agent 主体の根拠の取得。#2651）に従ってよいか（`agent_for_run` が
    # 入れる。保存・応答には出さない）。固定の中身の無い公開の版は宣言を確かめられないので False。
    caller_planning_allowed: bool = Field(default=True, exclude=True)

    @field_validator("output_fields")
    @classmethod
    def _check_output_fields(cls, value: list[AgentOutputField]) -> list[AgentOutputField]:
        return validate_output_fields(value)

    @field_validator("data_scopes")
    @classmethod
    def _normalize_data_scopes(cls, value: AgentDataScopes) -> AgentDataScopes:
        return normalize_data_scopes(value)

    @field_validator("skill_ids")
    @classmethod
    def _unique_skills(cls, value: list[str]) -> list[str]:
        return _unique_skill_ids(value)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def skill_updates(self) -> list[str]:
        """公開中の版に固定した後に中身が変わった（消えた・無効にした）スキルの ID（#1855）。

        固定していない版（#1855 より前）は比べない（空）。再公開で今のスキルを固定し直す。
        """
        published = self.published()
        # 常に最新を使う版（#1867）は今のスキルで動くので、更新の印を出さない。
        if (
            published is None
            or published.skills is None
            or published.skill_policy == SKILL_POLICY_LATEST
        ):
            return []
        return skill_updates(published.skills)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def draft_changed(self) -> bool:
        """下書きの項目（名前・指示・スキルの選択など）が公開中の版と違うか（スキルの更新は含めない）。"""
        published = self.published()
        return published is None or any(
            getattr(self, field) != getattr(published, field) for field in AGENT_VERSIONED_FIELDS
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def skills_pinned(self) -> bool:
        """公開中の版がスキルの中身を固定しているか（False は #1855 より前の版。再公開で固定）。"""
        published = self.published()
        return published is not None and published.skills is not None

    def published(self) -> AgentVersion | None:
        return next(
            (item for item in self.versions if item.version == self.published_version), None
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def unpublished_changes(self) -> bool:
        """下書きに公開していない変更があるか（公開した版が無いときも True）。"""
        return self.has_unpublished_changes()

    def has_unpublished_changes(self) -> bool:
        published = self.published()
        if published is None:
            return True
        # 固定した後に変わったスキルも「公開していない変更」（下書きは今のスキルで動く。#1855）。
        return self.draft_changed or bool(self.skill_updates)


class AgentNotPublishedError(ValueError):
    """公開した版の無い業務 Agent を、下書きではない Run で実行しようとした（#770）。"""


AGENT_UNPUBLISHED_MESSAGE = (
    "公開していない業務 Agent は実行できません。公開してから使ってください。"
)


def agent_unavailable_reason(agent: AgentProfile | None) -> str | None:
    """利用者の Run（チャット・MCP）で使えない理由。使えるなら None（#792）。

    利用者の Run は公開中の版で実行する（#770）ため、無効・公開した版が無い
    業務 Agent は使えない。品質評価と管理者の「下書きで実行」はこの判定を使わない。
    """
    if agent is None:
        return "業務 Agent が見つかりません。"
    if not agent.enabled:
        return "この業務 Agent は実行できない状態です。"
    if agent.published() is None:
        return AGENT_UNPUBLISHED_MESSAGE
    return None


class AgentPublishRequest(BaseModel):
    note: str = Field(default="", max_length=500)


class AgentProfileCreate(AgentProfile):
    """業務 Agent の作成の要求（`POST /api/agents`）。ツールの直接の指定を拒む（#3240）。

    保存・応答は `AgentProfile`（保存済みの旧い項目は読むときに無視する）。
    """

    @model_validator(mode="before")
    @classmethod
    def _reject_direct_tool_fields(cls, value: Any) -> Any:
        return reject_direct_tool_fields(value)


class AgentProfilePatch(BaseModel):
    name: str | None = None
    description: str | None = None
    instructions: str | None = None
    skill_ids: list[str] | None = None
    model_id: str | None = None
    data_scopes: AgentDataScopes | None = None
    clarification_policy: ClarificationPolicy | None = None
    clarification_max_rounds: int | None = Field(default=None, ge=0, le=MAX_ROUNDS_LIMIT)
    _check_clarification_max_rounds = field_validator("clarification_max_rounds")(
        validate_max_rounds
    )
    skill_policy: SkillPolicy | None = None
    output_fields: list[AgentOutputField] | None = None
    enabled: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _reject_direct_tool_fields(cls, value: Any) -> Any:
        return reject_direct_tool_fields(value)

    @field_validator("data_scopes")
    @classmethod
    def _normalize_data_scopes(cls, value: AgentDataScopes | None) -> AgentDataScopes | None:
        return normalize_data_scopes(value) if value is not None else None

    @field_validator("output_fields")
    @classmethod
    def _check_output_fields(
        cls, value: list[AgentOutputField] | None
    ) -> list[AgentOutputField] | None:
        return validate_output_fields(value) if value is not None else None


class RunCreateRequest(BaseModel):
    goal: str
    agent_id: str = "default"
    metadata: JsonObject = Field(default_factory=dict)
    # 公開前の下書きで実行する（管理者が試すとき。#770）。既定は公開中の版。
    draft: bool = False
    # 会話（スレッド）を続けるときの ID（#768）。省略すると新しい会話を始める。
    thread_id: str | None = Field(default=None, pattern=r"^thread_[0-9a-f]{32}$")
    # チャットの確認の取り消しの操作（#1441・#1445）。`nl2sql` は「データベースの検索は不要」、
    # `rag` は「資料の検索は不要」。この Run ではその種類のツールを使わずに続ける。
    clarification_cancel: Literal["nl2sql", "rag"] | None = None
    # 質問に添付するファイル（#2517。`POST /api/chat-attachments` の `attachment_id`）。送信で会話に
    # 結び付け、会話の続きの Run も読める。件数の上限は共通の設定（API が確かめる）。
    attachment_ids: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("goal")
    @classmethod
    def _require_goal(cls, value: str) -> str:
        # ゴールは画面と同じく必須（#540）。空白だけも未入力として扱う
        # Oracle の goal 列は NOT NULL。。
        if not value.strip():
            raise ValueError("ゴールを入力してください。")
        return value


class FeedbackRating(StrEnum):
    """回答の評価（RAG と同じ値。#774）。"""

    HELPFUL = "helpful"
    NOT_HELPFUL = "not_helpful"


class FeedbackReason(StrEnum):
    """役に立たなかった理由（RAG の回答の理由に、Agent のツールの使い方を足したもの。#774）。"""

    INCORRECT = "incorrect"
    INCOMPLETE = "incomplete"
    NOT_RELEVANT = "not_relevant"
    ANSWER_UNTRUSTED = "answer_untrusted"
    AMBIGUOUS_QUESTION = "ambiguous_question"
    WRONG_ACTION = "wrong_action"


FEEDBACK_COMMENT_MAX_CHARS = 1000


class RunFeedbackRequest(BaseModel):
    """チャットの回答への評価。役に立たなかったときは理由が必須（RAG と同じ。#774）。"""

    rating: FeedbackRating
    reason: FeedbackReason | None = None
    comment: str = Field(default="", max_length=FEEDBACK_COMMENT_MAX_CHARS)

    @model_validator(mode="after")
    def _reason_for_not_helpful(self) -> RunFeedbackRequest:
        if self.rating == FeedbackRating.NOT_HELPFUL:
            if self.reason is None:
                raise ValueError("役に立たなかった理由を選んでください。")
            self.comment = self.comment.strip()
        else:
            # 役に立った評価には理由・コメントを残さない（RAG と同じ）。
            self.reason = None
            self.comment = ""
        return self


class RunFeedback(BaseModel):
    """Run（チャットの 1 往復）の回答への評価。付け直すと上書きする（#774）。"""

    rating: FeedbackRating
    reason: FeedbackReason | None = None
    comment: str = ""
    user_uuid: str | None = None
    updated_at: datetime = Field(default_factory=_now)


class RunNotRatableError(ValueError):
    """回答の無い Run（完了していない Run）には評価を付けられない（#774）。"""


# 評価の Run（#776）で、承認が要るツールを実行しなかったときにモデルへ返す文。
EVALUATION_DRY_RUN_MESSAGE = (
    "品質評価の実行中のため、このツールは実行していません（承認が要る操作）。"
    "実行した場合の結果は分からないものとして、ここまでの情報で回答を完成させてください。"
)
# 評価の Run の印（`metadata`）。組み込み Runtime は承認が要るツールを実行せずに続ける。
EVALUATION_DRY_RUN_KEY = "evaluation_dry_run"

# 外部の MCP クライアント（API キー）から作った Run の印（`metadata`。#1874）。キーの ID と名前は
# 実行履歴・監査の「MCP（キーの名前）」と、キーごとの Run の数の上限に使う。
RUN_SOURCE_KEY = "source"
MCP_RUN_SOURCE = "mcp"
MCP_API_KEY_ID_KEY = "api_key_id"
MCP_API_KEY_NAME_KEY = "api_key_name"
# 回答前の確認の答えで作る次の Run に引き継ぐ出所（MCP の Run の続きも MCP の Run）。
RUN_ORIGIN_METADATA_KEYS = (RUN_SOURCE_KEY, MCP_API_KEY_ID_KEY, MCP_API_KEY_NAME_KEY)
# MCP の Run で、承認が要るため使わなかったツールの step の error_code（#1874）。
MCP_APPROVAL_SKIPPED_CODE = "mcp.approval_required"
# 取消のイベント（`run.cancelled`）に残す入口の項目（MCP の `agent_cancel_run` は via=mcp と
# キーの ID。#3240）。
CANCEL_VIA_KEY = "via"
_CANCEL_VIA_KEYS = frozenset({CANCEL_VIA_KEY, MCP_API_KEY_ID_KEY})


def is_mcp_run(metadata: Mapping[str, Any]) -> bool:
    """外部の MCP クライアントから作った Run か（#1874）。"""
    return metadata.get(RUN_SOURCE_KEY) == MCP_RUN_SOURCE


def mcp_approval_message(tool_name: str) -> str:
    """MCP の Run で承認が要るツールを呼んだときにモデルへ返す文（#1874）。"""
    return (
        f"外部の MCP クライアントからの実行では、承認の要るツール（{tool_name}）を使えません。"
        "管理者がツールのポリシーで許可すると使えます。"
        "このツールで確かめる予定だったことは、回答の「確かめられなかった点」に書き、"
        "ここまでの情報で回答を完成させてください。"
    )


# Control Plane が Run の `metadata` に付ける予約の key（#1130）。利用者が `POST /api/runs` で
# 付けると、再開の状態（`_builtin_sdk_state`）・評価の dry-run・MCP の出所を偽れる
# ため、API の入口で拒否する。`_` で始まる key もすべて予約とする。品質評価・MCP・
# 再実行は `create_builtin_run` を直接呼ぶので付けられる。
RESERVED_RUN_METADATA_KEYS = frozenset(
    {
        "agent_version",
        EVALUATION_DRY_RUN_KEY,
        "evaluation_job_id",
        "evaluation_case_id",
        RUN_SOURCE_KEY,
        "mcp_session",
        MCP_API_KEY_ID_KEY,
        MCP_API_KEY_NAME_KEY,
        "replayed_from_run_id",
        # 確認の取り消し（#1441・#1445）。`RunCreateRequest.clarification_cancel` から作る。
        CANCEL_METADATA_KEY,
        # 回答前の確認（#1839）。確認の答えの操作から作る。
        *CONFIRMATION_METADATA_KEYS,
        # Run 全体の実行予算の消費（#68）。利用者が少ない消費を書いて予算を増やせないようにする。
        RUN_BUDGET_METADATA_KEY,
    }
)


def reserved_run_metadata_keys(metadata: Mapping[str, object]) -> list[str]:
    """利用者が付けた `metadata` のうち、Control Plane の予約の key（並びは安定させる）。"""
    return sorted(
        key for key in metadata if key.startswith("_") or key in RESERVED_RUN_METADATA_KEYS
    )


class RunUsage(BaseModel):
    """Run が使ったモデルの量（#772）。承認待ちからの再開を含めた累計（SDK の `Usage`）。"""

    model: str = ""
    requests: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class RunState(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def migrate_saved_checkpoint(cls, value: object) -> object:
        from app.features.agent.profile_name_migration import migrate_sdk_checkpoint

        if isinstance(value, dict) and isinstance(value.get("metadata"), dict):
            metadata = value["metadata"]
            checkpoint = metadata.get(_BUILTIN_STATE_KEY)
            if isinstance(checkpoint, str):
                return {
                    **value,
                    "metadata": {
                        **metadata,
                        _BUILTIN_STATE_KEY: migrate_sdk_checkpoint(checkpoint),
                    },
                }
        return value

    id: str
    goal: str
    agent_id: str
    runtime_id: str = "legacy-native"
    # 実行の区間（`agent.execute` / `agent.resume`）の親の trace context。最初の区間は空（新しい
    # trace。#3272）で、再開の区間は前の区間の root を親にする。
    trace_context: dict[str, str] = Field(default_factory=dict)
    # Run を受け付けた要求の trace context（画面・API キーの外部の MCP クライアント。#3272）。親には
    # せず、最初の区間の root の span link にだけ残す（`mcp_trace` の規則。外部の要求が Run の
    # trace id を決められないようにする）。
    trace_link_context: dict[str, str] = Field(default_factory=dict)
    # 処理の経過の span を共通の span の表（`PLATFORM_TRACE_SPANS`。#3226 の案 A）に
    # 書く Run か。
    # これより前の Run（古い形式の記録）は False で、画面は古い形式の案内を出す。
    trace_store: bool = False
    # 実行の全体の span（`agent.execute`）の trace id。再開の区間（`agent.resume`）も同じ trace。
    # 画面の API はこの trace id で表を読む（画面から任意の trace id を受け取らない）。
    trace_id: str | None = Field(default=None, pattern="^[0-9a-f]{32}$")
    # 表に書けなかった span の数（実行の区切りを確定したときの書き手の数。1 以上なら一部
    # だけの記録）。
    trace_dropped_spans: int = Field(default=0, ge=0)
    # 工程の詳細（span id → `SpanDisplay.details`。ツールの引数と結果など）。表には入れない。
    span_details: dict[str, ChatProgressStepDetails] = Field(default_factory=dict)
    # 記録した承認・確認の待ちの span の回（工程の種類 → 回。再開し直しても重ねて記録しない）。
    trace_wait_rounds: dict[str, list[int]] = Field(default_factory=dict)
    status: RunStatus
    steps: list[RunStep] = Field(default_factory=list)
    events: list[RunEvent] = Field(default_factory=list)
    approvals: list[ApprovalRequest] = Field(default_factory=list)
    # 回答前の確認（#1839）。Agent 自身の確認（Run は `waiting_confirmation` で待つ）と、
    # ツールの結果の確認（完了した Run に出し、答えで次の Run を作る）。
    confirmations: list[ConfirmationRequest] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    pending_tool_calls: list[ToolCall] = Field(default_factory=list)
    metadata: JsonObject = Field(default_factory=dict)
    # Run を作った利用者（共通認証の user_uuid。#233）。RAG / NL2SQL の MCP はこの利用者として呼ぶ。
    # この項目がない既存の Run は None。
    created_by_user_uuid: str | None = None
    # 会話（スレッド。#768）。チャットの 1 往復が 1 Run。#768 より前の Run は None。
    thread_id: str | None = None
    # 会話の持ち主（会話を始めた利用者。#1928）。`created_by_user_uuid` は Run を実行した利用者
    # （actor）で、system_admin が他の利用者の会話を続けた Run では持ち主と異なる。新しい会話の
    # Run は `created_by_user_uuid` と同じ。この項目がない既存の Run は None（`run_thread_owner`）。
    thread_owner_user_uuid: str | None = None
    # 質問に添付したファイル（#2517。名前と種類。添付が期限で消えても発話の表示に残す）。
    attachments: list[ChatAttachmentReference] = Field(default_factory=list)
    # 回答への評価（#774）。評価していない Run は None。
    feedback: RunFeedback | None = None
    # 管理者の評価（Agent 管理の権限。だれの回答にも付けられ、本人の評価とは別に残す。#774）。
    admin_review: RunFeedback | None = None
    # モデルの利用量（#772）。組み込み Runtime がモデルを呼ぶ前の Run・#772 より前の Run は None。
    usage: RunUsage | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    @field_validator("trace_context", "trace_link_context", mode="before")
    @classmethod
    def _parse_trace_context(cls, value: object) -> dict[str, str]:
        return validated_trace_context(value)

    @field_validator("span_details", mode="before")
    @classmethod
    def _parse_span_details(cls, value: object) -> object:
        # 形の違う詳細は捨てる（壊れた記録で Run の読み込みを止めない・退避しない）。
        if not isinstance(value, dict):
            return {}
        details: dict[str, object] = {}
        for span_id, item in value.items():
            if isinstance(item, ChatProgressStepDetails):
                details[str(span_id)] = item
                continue
            try:
                details[str(span_id)] = ChatProgressStepDetails.model_validate(item)
            except ValidationError:
                continue
        return details

    @field_serializer("span_details")
    def _dump_span_details(self, value: dict[str, ChatProgressStepDetails]) -> JsonObject:
        # 値の無い項目は出さない（処理の経過のイベントの詳細と同じ形）。
        return {
            span_id: item.model_dump(mode="json", exclude_none=True)
            for span_id, item in value.items()
        }


class ConfirmationClosedError(ValueError):
    """答えていない回答前の確認ではない（答え済み・古い・取り消し済み。#1839）。"""


class RunNotActiveError(RuntimeError):
    """終わった Run（取消・完了・失敗）では新しいツールを始めない（#2609）。

    取消が先に確定した Run に新しい step を作らず、handler も呼ばない（`start_builtin_tool_step`）。
    """

    def __init__(self, run_id: str, status: RunStatus) -> None:
        super().__init__(f"run {run_id} is {status.value}")
        self.run_id = run_id
        self.status = status


class ConfirmationOutcome(BaseModel):
    """回答前の確認の操作の結果（#1839）。"""

    run: RunState
    # ツールの結果の確認に答えた・スキップしたときに作った次の Run（実行は呼ぶ側が始める）。
    next_run: RunState | None = None
    # 「確認をやめる」で入力欄に戻す質問。
    question: str | None = None
    # 「確認をやめる」で閉じる NL2SQL の SQL の生成前の確認（接続・requirement_session_id）。
    closed_sessions: list[JsonObject] = Field(default_factory=list)


class ThreadNotFoundError(LookupError):
    """会話が無い・別の利用者や別の Agent の会話（#768）。"""


class ThreadKindMismatchError(ThreadNotFoundError):
    """下書きの会話を公開中の版で続ける・公開中の版の会話に下書きの Run を足す（#2515）。

    会話は始めた Run の種類に固定する。種類の違う会話は無い会話として扱い、画面の API だけが
    理由を返す。
    """


class ThreadBusyError(ValueError):
    """回答の作成中・承認待ちの Run がある会話は削除しない（#1817）。"""


# 会話の名前（利用者が変えたもの。#1817）。会話の最初の Run の metadata に置く
# （Run と一緒に保存される）。
THREAD_TITLE_METADATA_KEY = "thread_title"
# 会話から外した Run の元の会話（#1817）。Run は監査・フィードバック・評価のために残す。
DELETED_THREAD_METADATA_KEY = "deleted_thread_id"
# 会話の名前の上限（RAG の会話名と同じ。#1817）。
THREAD_TITLE_MAX_LENGTH = 80


class ThreadUpdateRequest(BaseModel):
    """会話の名前の変更（#1817。RAG の `ConversationUpdateRequest` と同じ規則）。"""

    title: str = Field(..., min_length=1, max_length=THREAD_TITLE_MAX_LENGTH)

    @field_validator("title", mode="before")
    @classmethod
    def _strip_title(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ThreadSummary(BaseModel):
    thread_id: str
    agent_id: str
    title: str
    run_count: int
    last_status: RunStatus
    created_at: datetime
    updated_at: datetime
    # 会話の持ち主（応答には出さない。表示名の解決と本人の会話の判定に使う。#1889）。
    owner_user_uuid: str | None = Field(default=None, exclude=True)
    # 見ている利用者の会話か（system_admin が他の利用者の会話を見るときは false。画面は一覧に
    # 持ち主の表示名を添える。#1889。system_admin は見るだけで、続けられない。#3244）。
    owned: bool = True
    # system_admin の一覧・詳細だけに付く利用者の表示名（#1889・#1696。引けない利用者は空）。
    actor_display_name: str = ""


class ThreadsData(ChatHistoryPage[ThreadSummary]):
    """会話の一覧の 1 ページ（新しい順。共通の Page。#1265 / #1266）。

    `total` は利用者が見られる会話のうち、最後の更新が会話の履歴の期間（`history_days`。#2340）の
    中の会話の件数。
    """


CancelVia = Literal["screen", "mcp", "system"]


class RunCancellationView(BaseModel):
    """実行の詳細の「取消」（取り消した人と入口。応答だけに付ける。保存しない。#3348）。

    `via` は画面の操作（`screen`）・外部の MCP の `agent_cancel_run`（`mcp`）・操作した人の記録の
    無い自動の取消（`system`。品質評価の後始末など）。API キーの名前は、見ている利用者が見られる
    キーだけ（`AuthService.api_key_names`。見られなければ空で、画面は「MCP」だけを出す）。
    """

    via: CancelVia
    # 取り消した人の表示名（引けない利用者は ""。`system` は ""）。
    actor_display_name: str = ""
    # 取り消した人が見ている利用者か。
    by_me: bool = False
    api_key_name: str = ""


@dataclass(frozen=True, slots=True)
class RunCancelRecord:
    """取消のイベント（`run.cancelled`）に残した、取り消した人と入口（#3240・#3348）。"""

    # 取り消した人のログイン ID（記録の無い自動の取消は None）。
    cancelled_by: str | None
    via: CancelVia
    api_key_id: str | None


def run_cancel_record(run: RunState) -> RunCancelRecord | None:
    """取り消した Run の最後の取消のイベントの記録（取り消していない Run は None）。"""
    if run.status != RunStatus.CANCELLED:
        return None
    event = next(
        (item for item in reversed(run.events) if item.type == RunEventType.RUN_CANCELLED), None
    )
    payload = event.payload if event is not None else {}
    cancelled_by = payload.get("cancelled_by")
    actor = cancelled_by if isinstance(cancelled_by, str) and cancelled_by else None
    key_id = payload.get(MCP_API_KEY_ID_KEY)
    via: CancelVia = (
        "mcp" if payload.get(CANCEL_VIA_KEY) == MCP_RUN_SOURCE else "screen" if actor else "system"
    )
    return RunCancelRecord(
        cancelled_by=actor,
        via=via,
        api_key_id=key_id if via == "mcp" and isinstance(key_id, str) and key_id else None,
    )


class RunViewer(BaseModel):
    """見ている利用者から見た Run の事実（応答だけに付ける。保存しない。#1954）。

    Run を実行した人（`created_by_user_uuid`）と会話の持ち主は、system_admin が他の利用者の会話で
    実行した Run では異なる。画面はこの値で「〈名前〉が実行」と、待ちの操作の可否を決める。
    """

    # 見ている利用者が実行した Run か。
    executed_by_me: bool = True
    # 実行した人の表示名（見る人を問わず返す。引けなければ ""）。
    executor_display_name: str = ""
    # 会話の持ち主が見ている利用者か（会話の無い Run は None）。
    owned_by_me: bool | None = None
    # 会話の持ち主の表示名（会話の無い Run は None。引けなければ ""）。
    conversation_owner_display_name: str | None = None
    # 承認・再開・Agent 自身の確認への回答を、見ている利用者ができるか（実行した人と
    # system_admin だけ。会話の持ち主でも、ほかの人が実行した Run は False。#1928）。
    can_decide: bool = True
    # 保存した根拠のうち、見ている利用者が今の権限で見られない根拠の数（抜粋・文書は返さない。
    # `evidence_access`。#3252）。
    withheld_evidence_count: int = 0
    # 取り消した人と入口（取り消した Run だけ。`run.cancelled` のイベントから作る。#3348）。
    cancellation: RunCancellationView | None = None


class RunView(RunViewer, RunState):
    """画面に返す Run（`RunState` と、見ている利用者から見た事実。#1954）。"""

    @classmethod
    def of(cls, run: RunState, viewer: RunViewer) -> RunView:
        """検証し直さずに組み立てる（Run の step・Event を複製しない）。"""
        values = {name: getattr(run, name) for name in RunState.model_fields}
        values.update(viewer.model_dump())
        return cls.model_construct(_fields_set=set(values), **values)


class ThreadData(BaseModel):
    thread_id: str
    agent_id: str
    runs: list[RunState] = Field(default_factory=list)
    # 会話の持ち主（応答には出さない。表示名の解決と本人の会話の判定に使う。#1889）。
    owner_user_uuid: str | None = Field(default=None, exclude=True)
    # 見ている利用者の会話か（system_admin が他の利用者の会話を見るときは false。#1889）。
    owned: bool = True
    # system_admin の一覧・詳細だけに付く利用者の表示名（#1889・#1696。引けない利用者は空）。
    actor_display_name: str = ""
    # 持ち主以外が送った往復の、送った利用者の表示名（Run の ID → 表示名。#1928）。system_admin が
    # 他の利用者の会話を続けた Run だけに付き、画面は質問の吹き出しに送った人を添える。引けない
    # 利用者は空の文字列（画面は「削除済みのユーザー」と出す）。
    sender_display_names: dict[str, str] = Field(default_factory=dict)


class ThreadView(ThreadData):
    """画面に返す会話（Run は見ている利用者から見た事実つきの `RunView`。#1954）。"""

    runs: list[RunView] = Field(default_factory=list)  # type: ignore[assignment]


class PendingApprovalRef(BaseModel):
    """一覧の行から承認・却下するための、承認待ちの承認（最初の 1 件）。"""

    id: str
    tool_name: str


class RunSummary(BaseModel):
    """実行履歴の一覧の 1 行（#1897）。

    一覧は step・Event・成果物を持たない。詳細は `GET /api/runs/{run_id}` の `RunState`。
    """

    id: str
    goal: str
    agent_id: str
    runtime_id: str
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    created_by_user_uuid: str | None = None
    thread_id: str | None = None
    # 会話の持ち主（持ち主の実行履歴にも、system_admin がその会話で実行した Run を出す。#1928）。
    thread_owner_user_uuid: str | None = None
    pending_approval_count: int = 0
    pending_approval: PendingApprovalRef | None = None
    # 承認がすべて決まり、保存した SDK の状態から再開を待つ（手動の「再開」を出す。#1119）。
    resume_pending: bool = False
    # 一覧の「きっかけ」（`run_facts.run_source`。#1874）。MCP の Run は API キーの名前を添える。
    source: str = "user"
    api_key_name: str | None = None
    # 終了日時と所要時間（終わった Run だけ。実行中・待ちは None で、画面は「—」）と、
    # モデルの token の合計（記録の無い Run は None）。一覧の「所要時間」「token」の列（#1955）。
    finished_at: datetime | None = None
    duration_ms: int | None = None
    total_tokens: int | None = None
    # 見ている利用者から見た事実（一覧の応答で付ける。#1954）。`RunViewer` と同じ意味。
    executed_by_me: bool = True
    executor_display_name: str = ""
    can_decide: bool = True


class ApprovalSummary(BaseModel):
    """承認の一覧の 1 行（#1897）。詳細は `GET /api/approvals/{approval_id}`。"""

    id: str
    run_id: str
    tool_name: str
    status: ApprovalStatus
    created_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    run_goal: str
    run_status: RunStatus
    agent_id: str
    created_by_user_uuid: str | None = None


def run_summary(run: RunState) -> RunSummary:
    # run_facts は runtime を import するため、ここで読む（循環の import を避ける）。
    from app.features.agent.run_facts import run_finished_at, run_source

    pending = [item for item in run.approvals if item.status == ApprovalStatus.PENDING]
    api_key_name = run.metadata.get("api_key_name")
    finished_at = run_finished_at(run)
    return RunSummary(
        id=run.id,
        goal=run.goal,
        agent_id=run.agent_id,
        runtime_id=run.runtime_id,
        status=run.status,
        created_at=run.created_at,
        updated_at=run.updated_at,
        created_by_user_uuid=run.created_by_user_uuid,
        thread_id=run.thread_id,
        thread_owner_user_uuid=run_thread_owner(run),
        pending_approval_count=len(pending),
        pending_approval=(
            PendingApprovalRef(id=pending[0].id, tool_name=pending[0].tool_call.name)
            if pending
            else None
        ),
        resume_pending=builtin_resume_pending(run),
        source=run_source(run.metadata),
        api_key_name=api_key_name if isinstance(api_key_name, str) else None,
        finished_at=finished_at,
        duration_ms=(
            max(0, int((finished_at - run.created_at).total_seconds() * 1000))
            if finished_at is not None
            else None
        ),
        total_tokens=run.usage.total_tokens if run.usage is not None else None,
    )


def approval_summary(run: RunState, approval: ApprovalRequest) -> ApprovalSummary:
    return ApprovalSummary(
        id=approval.id,
        run_id=run.id,
        tool_name=approval.tool_call.name,
        status=approval.status,
        created_at=approval.created_at,
        decided_by=approval.decided_by,
        decided_at=approval.decided_at,
        run_goal=run.goal,
        run_status=run.status,
        agent_id=run.agent_id,
        created_by_user_uuid=run.created_by_user_uuid,
    )


def run_answer_text(run: RunState) -> str | None:
    """組み込み Runtime の最終回答（`kind="answer"` の成果物）。"""
    for artifact in reversed(run.artifacts):
        if artifact.kind == "answer" and isinstance(artifact.content, dict):
            text = artifact.content.get("text")
            if isinstance(text, str):
                return text
    return None


def run_answer_notice(run: RunState) -> str | None:
    """最終回答に付けた回答の注意書き（#2405。付けていない回答・古い Run は None）。"""
    for artifact in reversed(run.artifacts):
        if artifact.kind == "answer" and isinstance(artifact.content, dict):
            notice = artifact.content.get("notice")
            return notice if isinstance(notice, str) and notice else None
    return None


def is_withdrawn_run(run: RunState) -> bool:
    """「確認をやめる」で会話から外した Run か（回答前の確認。#1839）。"""
    return run.metadata.get(WITHDRAWN_METADATA_KEY) is True


def run_thread_owner(run: RunState) -> str | None:
    """Run の会話の持ち主（#1928）。記録の無い既存の Run は Run を実行した利用者。"""
    return run.thread_owner_user_uuid or run.created_by_user_uuid


def _thread_owner(runs: Sequence[RunState]) -> str | None:
    """会話（作成順の Run）の持ち主。会話を始めた Run の持ち主（#1928）。"""
    return run_thread_owner(runs[0]) if runs else None


def _new_run_trace_contexts() -> dict[str, dict[str, str]]:
    """新しい Run の trace の親と span link（#3272。RAG / NL2SQL の実行と同じ規則）。

    信頼した MCP の要求（サービストークン。`mcp_server_trace` の trusted）の中なら今の trace を
    続ける。それ以外（画面の要求・API キーの外部の MCP クライアント）は新しい trace にし、受け
    付けた要求の span（来た traceparent を親にした HTTP の span）は span link にだけ残す。
    """
    request_context = capture_trace_context()
    with operation_trace_parent() as links:
        parent = capture_trace_context()
    return {
        "trace_context": parent,
        "trace_link_context": request_context if links else {},
    }


def _is_draft_run(run: RunState) -> bool:
    """下書きで実行した Run か（`metadata.agent_version` が "draft"。#770）。"""
    return run.metadata.get("agent_version") == "draft"


def _is_draft_thread(runs: Sequence[RunState]) -> bool:
    """下書きの会話か（会話を始めた Run の種類。#2515）。"""
    return bool(runs) and _is_draft_run(runs[0])


def _thread_summary(
    thread_id: str, runs: list[RunState], *, owner_user_uuid: str | None
) -> ThreadSummary:
    """会話の一覧の 1 行（`runs` は作成順。`owner_user_uuid` は会話の持ち主）。

    名前は利用者が変えた名前、無ければ最初の質問の 1 行目。
    """
    custom = runs[0].metadata.get(THREAD_TITLE_METADATA_KEY)
    title = (
        custom.strip()
        if isinstance(custom, str) and custom.strip()
        else runs[0].goal.strip().splitlines()[0][:THREAD_TITLE_MAX_LENGTH]
    )
    return ThreadSummary(
        thread_id=thread_id,
        agent_id=runs[0].agent_id,
        owner_user_uuid=owner_user_uuid,
        title=title,
        run_count=len(runs),
        last_status=runs[-1].status,
        created_at=runs[0].created_at,
        updated_at=max(run.updated_at for run in runs),
    )


def run_history_turn(run: RunState, answer: str) -> ChatHistoryTurn:
    """前の Run を LLM に渡す 1 往復にする（質問・回答・使ったツールの名前。#1818）。

    ツールの結果は大きく、次の質問にはほとんど要らないので名前だけにする（Anthropic の
    context editing が古いツールの結果から消すのと同じ考え方）。
    """
    tool_names: list[str] = []
    for step in run.steps:
        name = step.tool_call.name if step.tool_call is not None else ""
        if name and name not in tool_names:
            tool_names.append(name)
    return ChatHistoryTurn.of(
        ("question", run.goal), ("answer", answer), ("tools", "、".join(tool_names))
    )


def run_support_task(run: RunState) -> JsonObject | None:
    """Run の支援タスクの状態（`kind="support_task"` の成果物。#1243）。"""
    for artifact in reversed(run.artifacts):
        if artifact.kind == SUPPORT_TASK_KIND and isinstance(artifact.content, dict):
            return artifact.content
    return None


class ApprovalDecisionRequest(BaseModel):
    approved: bool
    decided_by: str = "user"
    comment: str | None = None
    # 承認して、今後このツールは自動で実行（ツール権限の allow に足す。#1832）。Agent 管理だけ。
    always_allow: bool = False


class RunsData(Page[RunSummary]):
    """実行履歴の一覧の 1 ページ（新しい順。共通の Page。#1897）。

    `total` は絞り込んだ件数、`unfiltered_total` は利用者が見られる全件数（「3 / 12 件」）。
    種類の絞り込みが品質評価を含まない（既定の「品質評価以外」など）ときは、`unfiltered_total` も
    品質評価の Run を除いて数える（#1983）。
    """

    unfiltered_total: int = 0
    # 見られる品質評価の Run の数（既定の「種類: 評価以外」で隠れている数。#1983）。
    evaluation_total: int = 0
    # Run を実行した利用者（UUID）→ 表示名。system_admin が全員の Run を見るときだけ、このページの
    # 行の分を返す（「利用者」の列。#1889）。
    creator_display_names: dict[str, str] = Field(default_factory=dict)
    # 見ている利用者が、他の人が実行した Run を 1 件でも見られるか（system_admin・自分の会話で
    # system_admin が実行した Run を持つ利用者）。画面は「実行者」の列と絞り込みをこのときだけ
    # 出す（#1955）。
    has_other_executors: bool = False


class ApprovalsData(Page[ApprovalSummary]):
    """承認の一覧の 1 ページ（申請の新しい順。#1897）。`unfiltered_total` は実行履歴と同じ。"""

    unfiltered_total: int = 0


class ApprovalDetailData(BaseModel):
    """承認の詳細（承認と、その Run の要約。Run の step・Event は読まない。#1897）。"""

    approval: ApprovalRequest
    run: RunSummary
    # 承認の判断者（ログイン ID）→ 表示名。画面は判断者を表示名で出す（保存しない。#1696）。
    user_display_names: dict[str, str] = Field(default_factory=dict)
    # Run を実行した利用者（UUID）→ 表示名。system_admin が全員の Run を見るときだけ返す
    # （「利用者」の列。#1889）。
    creator_display_names: dict[str, str] = Field(default_factory=dict)


class AgentShareApprovalNotice(BaseModel):
    agent_id: str
    name: str
    decided_at: datetime | None = None


class AgentsData(BaseModel):
    agents: list[AgentProfile]
    share_approvals: list[AgentShareApprovalNotice] = Field(default_factory=list)
    # 版の公開者（ログイン ID）→ 表示名。画面は公開者を表示名で出す（保存しない。#1696）。
    user_display_names: dict[str, str] = Field(default_factory=dict)
    # 版の履歴に残す版の数（`AGENT_MAX_PUBLISHED_VERSIONS`。画面の版の履歴の案内に出す。#2356）。
    max_published_versions: int = DEFAULT_MAX_PUBLISHED_VERSIONS
    # 個人の業務 Agent の持ち主（user_uuid）→ 表示名（#2523。画面は持ち主を表示名で出す）。
    owner_display_names: dict[str, str] = Field(default_factory=dict)


class ArtifactsData(BaseModel):
    artifacts: list[Artifact]


class QuarantinedSnapshotRecord(BaseModel):
    """保存先の読み込みで直せず、読み込まなかった Run・業務 Agent（#853）。

    元の JSON（`raw`）をそのまま持ち、次の保存でも保存先の退避の記録に残す（黙って消さない）。
    """

    kind: str
    id: str | None = None
    reasons: list[str] = Field(default_factory=list)
    quarantined_at: datetime = Field(default_factory=_now)
    raw: Any = None


class AgentRuntimeSnapshot(BaseModel):
    version: str = "agent-control-plane.snapshot.v2"
    exported_at: datetime = Field(default_factory=_now)
    runs: list[RunState] = Field(default_factory=list)
    agents: list[AgentProfile] = Field(default_factory=list)
    # 読み込みで直せず退避した Run・業務 Agent の元の JSON（#853）。
    quarantined: list[QuarantinedSnapshotRecord] = Field(default_factory=list)
    # 旧 Memory（#756 で削除）。読み込んでも使わない。
    memory: list[JsonObject] = Field(default_factory=list)
    # 旧版（#754 より前）の外部 Runtime と Binding。読み込んでも使わない。
    control_plane_state: JsonObject = Field(default_factory=dict)


SUPPORTED_SNAPSHOT_VERSIONS = frozenset(
    {"agent-runtime.snapshot.v1", "agent-control-plane.snapshot.v2"}
)


class AgentRuntimeSnapshotCorruptError(RuntimeError):
    """snapshot 全体が読めない（JSON として壊れている・未対応の版。#853）。

    1 件の Run の不整合ではこの例外にしない（Run 単位で直すか退避する）。
    """


class RepairedRun(BaseModel):
    run_id: str
    repairs: list[str]


class SnapshotLoadReport(BaseModel):
    """保存先の読み込みで直した Run と、退避した Run・業務 Agent（#853）。"""

    repaired: list[RepairedRun] = Field(default_factory=list)
    quarantined: list[QuarantinedSnapshotRecord] = Field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.repaired or self.quarantined)


class RuntimeStorageHealth(BaseModel):
    """起動時の読み込みの結果（`GET /api/runtime/storage`。#853）。"""

    # 起動時の読み込みで整合しない状態を直した Run の数。
    repaired_runs: int = 0
    # 直せずに読み込まず、保存先に元の JSON のまま残している Run・業務 Agent の数。
    skipped_runs: int = 0
    skipped_agents: int = 0


class RuntimeToolCallAuditRecord(BaseModel):
    run_id: str
    run_goal: str
    run_status: str
    agent_id: str
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
    audit_metadata: JsonObject = Field(default_factory=dict)
    run_created_at: str
    run_updated_at: str


class RuntimeToolCallAuditData(Page[RuntimeToolCallAuditRecord]):
    """projection のツール監査の 1 ページ（共通の `Page`。#1266）。"""

    # 監査に記録されたツール名（絞り込みの条件に依らない。画面のツール名の選択肢。#983）。
    tool_names: list[str] = Field(default_factory=list)


class AgentRuntimeRepositoryContract(Protocol):
    def claim_control_plane_run(self, worker_id: str, *, lease_seconds: int) -> RunState | None: ...
    def persist_control_plane_state(self) -> None: ...
    def flush_writes(self, ticket: int | None = None) -> None: ...
    def pending_writes(self) -> int: ...
    def create_builtin_run(
        self,
        request: RunCreateRequest,
        *,
        created_by_user_uuid: str | None = None,
        agent_version: int | None = None,
        extra_metadata: JsonObject | None = None,
        all_users: bool = False,
        attachments: list[ChatAttachmentReference] | None = None,
    ) -> RunState: ...
    def begin_builtin_run(
        self, run_id: str, *, claim: RunClaim | None = None
    ) -> tuple[RunState, AgentProfile] | None: ...
    def begin_builtin_resume(
        self, run_id: str, *, claim: RunClaim | None = None
    ) -> tuple[RunState, AgentProfile, str, dict[str, bool]] | None: ...
    def builtin_run_ended(self, run_id: str) -> bool: ...
    def approved_builtin_call(
        self, run_id: str, call_id: str, tool_name: str
    ) -> ApprovalRequest | None: ...
    def start_builtin_tool_step(
        self, run_id: str, call: ToolCall, *, cleanup: bool = False
    ) -> tuple[str, ToolInvocationContext]: ...
    def finish_builtin_tool_step(self, run_id: str, step_id: str, result: ToolResult) -> None: ...
    def request_builtin_approvals(
        self,
        run_id: str,
        calls: Sequence[ToolCall],
        *,
        state: str,
        confirmations: Sequence[ConfirmationRequest] = (),
    ) -> RunState: ...
    def complete_builtin_run(
        self,
        run_id: str,
        answer: str,
        *,
        outcome: JsonObject | None = None,
        confirmations: Sequence[ConfirmationRequest] = (),
        notice: str | None = None,
    ) -> RunState: ...
    def decide_confirmation(
        self,
        run_id: str,
        confirmation_id: str,
        *,
        action: str,
        answers: Sequence[ConfirmationAnswer] = (),
        decided_by: str | None = None,
        actor_user_uuid: str | None = None,
    ) -> ConfirmationOutcome: ...
    def fail_builtin_run(self, run_id: str, *, code: str, detail: str) -> RunState: ...
    def fail_interrupted_runs(self, *, updated_before: datetime) -> list[str]: ...
    def note_builtin_warning(self, run_id: str, message: str) -> None: ...
    def note_builtin_tool_titles(self, run_id: str, titles: Mapping[str, str]) -> None: ...
    def builtin_tool_titles(self, run_id: str) -> dict[str, str]: ...
    def builtin_tool_steps(self, run_id: str, call_ids: Collection[str]) -> list[RunStep]: ...
    def builtin_tool_step(self, run_id: str, step_id: str) -> RunStep | None: ...
    def record_builtin_span(self, run_id: str, update: SpanUpdate) -> bool: ...
    def record_tool_grounding(
        self, run_id: str, action: str, answer: str, *, declined: bool = False
    ) -> None: ...
    def record_clarification_cancel(self, run_id: str, records: list[JsonObject]) -> None: ...
    def record_context_compaction(self, run_id: str, payload: JsonObject) -> None: ...
    def record_undeclared_tool_call(self, run_id: str, payload: JsonObject) -> None: ...
    def set_run_feedback(
        self, run_id: str, feedback: RunFeedback, *, admin: bool = False
    ) -> RunState: ...
    def thread_history(self, run_id: str) -> list[ChatHistoryTurn]: ...
    def thread_previous_runs(self, run_id: str) -> list[RunState]: ...
    def thread_answer_validations(self, run_id: str) -> list[JsonObject]: ...
    def support_task_context(self, run_id: str) -> tuple[str, JsonObject | None]: ...
    def save_support_task(self, run_id: str, content: JsonObject) -> None: ...
    def save_builtin_artifact(
        self, run_id: str, *, kind: str, name: str, content: JsonObject
    ) -> None: ...
    def list_threads(
        self,
        *,
        user_uuid: str | None,
        agent_id: str | None = None,
        all_users: bool = False,
        draft: bool = False,
    ) -> list[ThreadSummary]: ...
    def get_thread(
        self, thread_id: str, *, user_uuid: str | None, all_users: bool = False
    ) -> ThreadData: ...
    def rename_thread(
        self, thread_id: str, *, user_uuid: str | None, title: str, all_users: bool = False
    ) -> ThreadSummary: ...
    def delete_thread(
        self, thread_id: str, *, user_uuid: str | None, all_users: bool = False
    ) -> None: ...
    def record_builtin_dry_run_steps(self, run_id: str, calls: Sequence[ToolCall]) -> None: ...
    def record_builtin_unattended_skips(self, run_id: str, calls: Sequence[ToolCall]) -> None: ...
    def run_statuses(self, run_ids: Collection[str]) -> dict[str, RunStatus]: ...
    def record_builtin_usage(self, run_id: str, usage: RunUsage) -> None: ...
    def record_run_budget(self, run_id: str, snapshot: JsonObject) -> None: ...
    def list_runs(self) -> list[RunState]: ...
    def list_run_summaries(self) -> list[RunSummary]: ...
    def list_approval_summaries(self) -> list[ApprovalSummary]: ...
    def get_run_for_approval(self, approval_id: str) -> RunState: ...
    def get_run(self, run_id: str) -> RunState: ...
    def list_artifacts(self, run_id: str) -> list[Artifact]: ...
    def get_artifact(self, run_id: str, artifact_id: str) -> Artifact: ...
    def iter_events(
        self,
        run_id: str,
        *,
        after_event_id: str | None = None,
        follow: bool = False,
        idle_timeout_seconds: float = 15.0,
    ) -> Iterator[RunEvent | None]: ...
    def cancel_run(
        self,
        run_id: str,
        *,
        cancelled_by: str | None = None,
        via: Mapping[str, str] | None = None,
    ) -> RunState: ...
    def decide_approval(
        self,
        approval_id: str,
        request: ApprovalDecisionRequest,
        *,
        before_decision: Callable[[RunState], None] | None = None,
    ) -> RunState: ...
    def list_agents(self) -> list[AgentProfile]: ...
    def create_agent(self, agent: AgentProfile) -> AgentProfile: ...
    def patch_agent(self, agent_id: str, patch: AgentProfilePatch) -> AgentProfile: ...
    def owned_agent_ids(self, user_uuid: str | None) -> frozenset[str]: ...
    def update_agent_sharing(
        self,
        agent_id: str,
        *,
        owner_user_uuid: str | None,
        share_request: AgentShareRequest | None,
    ) -> AgentProfile: ...
    def publish_agent(
        self, agent_id: str, *, note: str = "", published_by: str | None = None
    ) -> AgentProfile: ...
    def restore_agent_version(
        self, agent_id: str, version: int, *, published_by: str | None = None
    ) -> AgentProfile: ...
    def publish_skill_updates(
        self, agent_id: str, *, note: str = "", published_by: str | None = None
    ) -> tuple[AgentProfile, int]: ...
    def set_published_version(self, agent_id: str, version: int) -> AgentProfile: ...
    def delete_agent(self, agent_id: str) -> None: ...
    def storage_health(self) -> RuntimeStorageHealth: ...


# ---- 変更の保存（#1939） ---------------------------------------------------------------
#
# repository の変更は lock（`_lock`）の中でメモリを変え、変えた Run・業務 Agent に印を付けて
# `_persist_locked()` で保存の予定（`_StagedChanges`）に積む。保存先への書き込み（I/O）は lock を
# 放した後に行う（`_RepositoryLock.__exit__`）。書き込みの間も、ほかの要求（一覧・回答の stream）は
# メモリを読める。
#
# - event loop のスレッド（組み込み Runtime の実行・`async def` の handler の中）からの変更は、
#   書き込みを待たずに戻り、バックグラウンドの書き込みのスレッドが書く（event loop を塞がない）。
#   書けなければ予定に残して間をおいて書き直す。
# - それ以外のスレッド（`run_in_threadpool` の handler・CLI・dispatcher）からの変更は、書き終えて
#   から戻る（失敗は例外。予定は残し、次の書き込みで書き直す）。
# - 書き込みはプロセスの中で `_write_lock` で 1 つずつ、積んだ順に行う。

# event loop のスレッドからの変更をバックグラウンドで書くか（テストは conftest で False にし、
# 書き込みを待つ。バックグラウンドの書き込みを確かめるテストだけ True にする）。
BACKGROUND_WRITES = True
# 処理の経過の span の記録をまとめて保存の予定に積むまでの時間（秒。#3226）。
SPAN_PERSIST_DELAY_SECONDS = 0.2
# 印の付け忘れの検出（テストの conftest が True にする）。保存のたびに全 Run・業務 Agent の JSON を
# 比べ、変わったのに印の無いものがあれば AssertionError（本番では比べない。O(Run 数) のため）。
VERIFY_CHANGE_TRACKING = False
# バックグラウンドの書き込みの失敗の後の待ち（秒。失敗が続くと上限まで延ばす）。
_BACKGROUND_RETRY_SECONDS = 1.0
_BACKGROUND_RETRY_MAX_SECONDS = 30.0
# 遅い書き込みのログ（ms）。
_SLOW_WRITE_MS = 1000.0

EntityKey = tuple[str, str]
RUN_ENTITY = checkpoint_store.RUN
AGENT_ENTITY = checkpoint_store.AGENT


@dataclass(slots=True)
class _StagedChanges:
    """保存の予定（積んだ時点の JSON。`None` は削除）。"""

    ticket: int
    entities: dict[EntityKey, str | None] = dataclass_field(default_factory=dict)
    # 読み込みで退避した記録（変わったときだけ。JSON の配列）。
    quarantined: list[Any] | None = None

    def merge(self, later: _StagedChanges) -> None:
        """後に積んだ予定を重ねる（同じ Run・業務 Agent は後の値）。"""
        self.ticket = later.ticket
        self.entities.update(later.entities)
        if later.quarantined is not None:
            self.quarantined = later.quarantined


class _RepositoryLock:
    """repository の lock。lock の中で積んだ保存の予定を、lock を放した後に書く（#1939）。

    `with` で取ったときだけ、放した後に書く（`threading.Condition` の `wait` の中の放し・取り直しは
    `acquire` / `release` で、書かない）。
    """

    def __init__(
        self, flush: Callable[[int], None], on_enter: Callable[[], None] | None = None
    ) -> None:
        self._lock = Lock()
        self._local = threading.local()
        self._flush = flush
        self._on_enter = on_enter
        # lock を持つスレッド（span の知らせが lock の中から届いたかの判定。#3226）。
        self._owner: int | None = None

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        acquired = self._lock.acquire(blocking, timeout)
        if acquired:
            self._owner = threading.get_ident()
        return acquired

    def release(self) -> None:
        self._owner = None
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    def held_by_current_thread(self) -> bool:
        """今のスレッドが lock を持っているか。"""
        return self._owner == threading.get_ident() and self._lock.locked()

    def note_staged(self, ticket: int) -> None:
        """lock を持つスレッドが保存の予定を積んだ（放した後に `ticket` まで書く）。"""
        self._local.ticket = max(int(getattr(self._local, "ticket", 0)), ticket)

    def __enter__(self) -> _RepositoryLock:
        self.acquire()
        if self._on_enter is not None:
            try:
                self._on_enter()
            except BaseException:
                self.release()
                raise
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()
        ticket = int(getattr(self._local, "ticket", 0))
        if not ticket:
            return
        self._local.ticket = 0
        try:
            self._flush(ticket)
        except Exception:
            if exc_type is None:
                raise
            # 元の例外を優先する（書けなかった予定は残り、次の書き込みで書き直す）。
            logger.warning("agent_runtime_storage_write_failed", exc_info=True)


def _on_event_loop_thread() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class AgentRuntimeRepository:
    def __init__(self, snapshot_path: str | Path | None = None) -> None:
        self._lock = _RepositoryLock(self._flush_after_release, self._on_lock_entered)
        self._condition = Condition(self._lock)  # type: ignore[arg-type]
        # 保存先への書き込み（プロセスの中で 1 つずつ。`_lock` より先に取る）。
        self._write_lock = RLock()
        # 前回の保存の予定から変えた Run・業務 Agent（`_persist_locked` で予定に積む）。
        self._changed: set[EntityKey] = set()
        self._quarantine_changed = False
        self._pending: list[_StagedChanges] = []
        self._staged_ticket = 0
        self._written_ticket = 0
        self._writer_wake = threading.Event()
        self._writer_thread: threading.Thread | None = None
        # span の記録をまとめて保存する予定があるか（`record_builtin_span`。#3226）。
        self._span_persist_scheduled = False
        self._tracking_baseline: dict[EntityKey, str] = {}
        self._runs: dict[str, RunState] = {}
        self._approvals: dict[str, ApprovalRequest] = {}
        self._agents: dict[str, AgentProfile] = {
            agent.id: _ensure_versioned(agent) for agent in _builtin_agents()
        }
        # 読み込みで直せず退避した Run・業務 Agent（保存のたびに snapshot に残す。#853）。
        self._quarantined: list[QuarantinedSnapshotRecord] = []
        self._load_report = SnapshotLoadReport()
        self._snapshot_path = Path(snapshot_path) if snapshot_path else None
        self._reset_change_tracking_locked()
        if self._snapshot_path is not None and self._snapshot_path.exists():
            self._load_snapshot_from_disk()

    # ---- 変更の印と保存の予定（#1939） ----

    def _mark_run_changed(self, run: RunState | str) -> None:
        """Run を変えた（次の `_persist_locked` で保存する。lock の中で呼ぶ）。"""
        state = self._runs.get(run) if isinstance(run, str) else run
        if state is not None and (
            _is_terminal(state.status)
            or state.status in {RunStatus.WAITING_APPROVAL, RunStatus.WAITING_CONFIRMATION}
        ):
            status = (
                "failed"
                if state.status == RunStatus.FAILED
                else ("cancelled" if state.status == RunStatus.CANCELLED else "done")
            )
            summary = finish_operation_trace(state.id, status=status)
            if summary is not None:
                # 再開の区間（`agent.resume`）を同じ trace の、この区間の子にする。
                state.trace_context = capture_trace_context()
                # 表に書けなかった span の数（#3226）。この process の書き手が trace ごとに数える。
                # 別の process で再開した区間の数とは足さず、多い方を残す（1 以上なら一部だけの
                # 記録）。
                from app.features.agent.trace_spans import trace_spans

                trace_id = state.trace_id or summary.telemetry_trace_id
                if trace_id:
                    state.trace_dropped_spans = max(
                        state.trace_dropped_spans, trace_spans.dropped(trace_id)
                    )
        self._changed.add((RUN_ENTITY, run if isinstance(run, str) else run.id))

    def _mark_agent_changed(self, agent_id: str) -> None:
        self._changed.add((AGENT_ENTITY, agent_id))

    def _stores_changes(self) -> bool:
        """保存先に書くか（memory は書かない）。"""
        return self._snapshot_path is not None

    def _entity_json_locked(self, key: EntityKey) -> str | None:
        kind, entity_id = key
        item: BaseModel | None = (
            self._runs.get(entity_id) if kind == RUN_ENTITY else self._agents.get(entity_id)
        )
        return item.model_dump_json() if item is not None else None

    def _all_entity_json_locked(self) -> dict[EntityKey, str | None]:
        entities: dict[EntityKey, str | None] = {
            (RUN_ENTITY, run_id): run.model_dump_json() for run_id, run in self._runs.items()
        }
        entities.update(
            {
                (AGENT_ENTITY, agent_id): agent.model_dump_json()
                for agent_id, agent in self._agents.items()
            }
        )
        return entities

    def _quarantine_json_locked(self) -> list[Any]:
        return [record.model_dump(mode="json") for record in self._quarantined]

    def _persist_locked(self, *, background: bool = False) -> bool:
        """印を付けた変更を保存の予定に積む（書き込みは lock を放した後。lock の中で呼ぶ）。

        `background` なら、lock を放したスレッドで書かずに background の書き込みに任せる（True を
        返したら、呼んだ側が lock を放した後に `_request_background_write` を呼ぶ）。
        """
        if VERIFY_CHANGE_TRACKING:
            self._verify_change_tracking_locked()
        if not self._stores_changes():
            self._clear_change_marks_locked()
            return False
        if not (self._changed or self._quarantine_changed):
            return False
        self._staged_ticket += 1
        staged = _StagedChanges(
            ticket=self._staged_ticket,
            entities={key: self._entity_json_locked(key) for key in self._changed},
        )
        if self._quarantine_changed:
            staged.quarantined = self._quarantine_json_locked()
        self._pending.append(staged)
        self._clear_change_marks_locked()
        if background and BACKGROUND_WRITES:
            return True
        self._lock.note_staged(staged.ticket)
        return False

    def _clear_change_marks_locked(self) -> None:
        self._changed.clear()
        self._quarantine_changed = False

    def _on_lock_entered(self) -> None:
        # 印の付け忘れの検出は、lock を取ってからの変更だけを見る（テストが lock の外で直接変えた
        # 値を、repository の付け忘れとして扱わない）。
        self._reset_change_tracking_locked()

    def _reset_change_tracking_locked(self) -> None:
        """印の付け忘れの検出の基準（lock を取った時点・保存先と同じになった時点のメモリ）。"""
        if VERIFY_CHANGE_TRACKING:
            self._tracking_baseline = {
                key: text
                for key, text in self._all_entity_json_locked().items()
                if text is not None
            }

    def _verify_change_tracking_locked(self) -> None:
        current = {
            key: text for key, text in self._all_entity_json_locked().items() if text is not None
        }
        changed = {
            key
            for key in set(current) | set(self._tracking_baseline)
            if current.get(key) != self._tracking_baseline.get(key)
        }
        missing = sorted(changed - self._changed)
        self._tracking_baseline = current
        if missing:
            raise AssertionError(
                f"変えた Run・業務 Agent に保存の印がありません（#1939）: {missing[:10]}"
            )

    def _flush_after_release(self, ticket: int) -> None:
        """lock を放した後に、積んだ保存の予定を書く（event loop の上ならバックグラウンドで）。"""
        if BACKGROUND_WRITES and _on_event_loop_thread():
            self._request_background_write()
            return
        self.flush_writes(ticket)

    def flush_writes(self, ticket: int | None = None) -> None:
        """保存の予定を `ticket`（None なら今まで積んだ分）まで書く。書けなければ例外。

        予定は書けるまで残る（次の書き込みで、後に積んだ予定と重ねて書き直す）。
        """
        with self._write_lock:
            target = self._staged_ticket if ticket is None else ticket
            if self._written_ticket >= target:
                return
            self._lock.acquire()
            try:
                if not self._pending:
                    self._written_ticket = max(self._written_ticket, target)
                    return
                batch = self._pending.pop(0)
                while self._pending:
                    batch.merge(self._pending.pop(0))
            finally:
                self._lock.release()
            started = time.perf_counter()
            try:
                self._write_staged(batch)
            except Exception:
                self._lock.acquire()
                try:
                    # 後に積んだ予定より前に戻す（次の書き込みで順に重ねる）。
                    self._pending.insert(0, batch)
                finally:
                    self._lock.release()
                raise
            self._written_ticket = max(self._written_ticket, batch.ticket)
            duration_ms = (time.perf_counter() - started) * 1000
            logger.log(
                logging.WARNING if duration_ms >= _SLOW_WRITE_MS else logging.DEBUG,
                "agent_runtime_storage_written",
                extra={
                    "duration_ms": round(duration_ms, 1),
                    "entities": len(batch.entities),
                },
            )

    def pending_writes(self) -> int:
        """まだ書いていない保存の予定の数。"""
        with self._write_lock:
            return max(0, self._staged_ticket - self._written_ticket)

    def _write_staged(self, batch: _StagedChanges) -> None:
        """保存の予定を保存先に書く（file は全体を書き直す。Oracle は上書き）。"""
        if self._snapshot_path is None:
            return
        self._lock.acquire()
        try:
            text = self._export_snapshot_locked().model_dump_json(indent=2)
        finally:
            self._lock.release()
        self._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._snapshot_path.with_name(f".{self._snapshot_path.name}.tmp")
        temp_path.write_text(text, encoding="utf-8")
        os.replace(temp_path, self._snapshot_path)

    def _request_background_write(self) -> None:
        thread = self._writer_thread
        if thread is None or not thread.is_alive():
            thread = threading.Thread(
                target=self._background_writer,
                name="agent-runtime-storage-writer",
                daemon=True,
            )
            self._writer_thread = thread
            thread.start()
        self._writer_wake.set()

    def _background_writer(self) -> None:
        delay = _BACKGROUND_RETRY_SECONDS
        while True:
            self._writer_wake.wait()
            self._writer_wake.clear()
            try:
                self.flush_writes()
            except Exception:  # noqa: BLE001 - 予定は残す。間をおいて書き直す
                logger.warning(
                    "agent_runtime_background_write_failed",
                    extra={"pending_writes": self.pending_writes(), "retry_seconds": delay},
                    exc_info=True,
                )
                time.sleep(delay)
                delay = min(delay * 2, _BACKGROUND_RETRY_MAX_SECONDS)
                self._writer_wake.set()
                continue
            delay = _BACKGROUND_RETRY_SECONDS

    def list_runs(self) -> list[RunState]:
        with self._lock:
            return [run.model_copy(deep=True) for run in self._sorted_runs_locked()]

    def list_run_summaries(self) -> list[RunSummary]:
        """実行履歴の一覧の行（新しい順）。Run を複製せず、一覧に要る項目だけを作る（#1897）。"""
        with self._lock:
            return [run_summary(run) for run in self._sorted_runs_locked()]

    def list_approval_summaries(self) -> list[ApprovalSummary]:
        """承認の一覧の行（申請の新しい順。#1897）。"""
        with self._lock:
            rows = [
                approval_summary(run, approval)
                for run in self._runs.values()
                for approval in run.approvals
            ]
        return sorted(rows, key=lambda row: row.created_at, reverse=True)

    def get_run_for_approval(self, approval_id: str) -> RunState:
        """承認の ID から、その承認を持つ Run を 1 件読む（全 Run を複製しない。#1897）。"""
        with self._lock:
            approval = self._approvals.get(approval_id)
            run = self._runs.get(approval.run_id) if approval is not None else None
            if run is None or not any(item.id == approval_id for item in run.approvals):
                run = next(
                    (
                        item
                        for item in self._runs.values()
                        if any(entry.id == approval_id for entry in item.approvals)
                    ),
                    None,
                )
            if run is None:
                raise KeyError(approval_id)
            return run.model_copy(deep=True)

    def get_run(self, run_id: str) -> RunState:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise KeyError(run_id)
            return run.model_copy(deep=True)

    def claim_control_plane_run(self, worker_id: str, *, lease_seconds: int) -> RunState | None:
        with self._lock:
            run = self._claim_control_plane_run_locked(worker_id, lease_seconds=lease_seconds)
            if run is None:
                return None
            self._persist_locked()
            return run.model_copy(deep=True)

    def _claim_control_plane_run_locked(
        self, worker_id: str, *, lease_seconds: int
    ) -> RunState | None:
        now = _now()
        for run in reversed(self._sorted_runs_locked()):
            # 組み込み Runtime の queued の Run（新規・承認の決定後の再開。#754）だけを claim する。
            if run.runtime_id != BUILTIN_RUNTIME_ID or run.status != RunStatus.QUEUED:
                continue
            if run_claim.lease_active(run.metadata, now):
                continue
            # 所有者・一意な token・次の世代を持つ lease（期限の切れた lease は置き換える。#2606）。
            lease = run_claim.new_lease(
                run.metadata, worker_id, now=now, lease_seconds=lease_seconds
            )
            run.metadata[run_claim.LEASE_METADATA_KEY] = lease
            run.updated_at = now
            self._append_event(
                run,
                RunEventType.RUNTIME_DISPATCH_CLAIMED,
                "runtime-dispatcher が Run を claim しました。",
                {
                    "worker_id": worker_id,
                    "lease_expires_at": lease["expires_at"],
                    "generation": lease["generation"],
                },
            )
            return run
        return None

    def persist_control_plane_state(self) -> None:
        """まだ書いていない保存の予定を書き切る（変更は都度、変えたものだけを書く）。"""
        self.flush_writes()

    def events_for_run(self, run_id: str) -> list[RunEvent]:
        return self.get_run(run_id).events

    def list_artifacts(self, run_id: str) -> list[Artifact]:
        with self._lock:
            run = self._require_run(run_id)
            return [artifact.model_copy(deep=True) for artifact in run.artifacts]

    def get_artifact(self, run_id: str, artifact_id: str) -> Artifact:
        with self._lock:
            run = self._require_run(run_id)
            for artifact in run.artifacts:
                if artifact.id == artifact_id:
                    return _hydrate_artifact_content(artifact)
            raise KeyError(artifact_id)

    def storage_health(self) -> RuntimeStorageHealth:
        with self._lock:
            return RuntimeStorageHealth(
                repaired_runs=len(self._load_report.repaired),
                skipped_runs=sum(1 for record in self._quarantined if record.kind == "run"),
                skipped_agents=sum(1 for record in self._quarantined if record.kind == "agent"),
            )

    def iter_events(
        self,
        run_id: str,
        *,
        after_event_id: str | None = None,
        follow: bool = False,
        idle_timeout_seconds: float = 15.0,
    ) -> Iterator[RunEvent | None]:
        with self._condition:
            run = self._runs.get(run_id)
            if run is None:
                raise KeyError(run_id)
            next_index = _event_index_after(run.events, after_event_id)

        while True:
            with self._condition:
                run = self._require_run(run_id)
                if next_index < len(run.events):
                    event = run.events[next_index].model_copy(deep=True)
                    next_index += 1
                else:
                    if not follow or _is_terminal(run.status):
                        return
                    self._condition.wait(timeout=idle_timeout_seconds)
                    if next_index >= len(run.events):
                        event = None
                    else:
                        continue
            yield event

    def cancel_run(
        self,
        run_id: str,
        *,
        cancelled_by: str | None = None,
        via: Mapping[str, str] | None = None,
    ) -> RunState:
        """終わっていない Run を取り消す。`cancelled_by` は操作した利用者のログイン ID（#1928）。

        `via` は取消の入口（外部の MCP の `agent_cancel_run` は `{"via": "mcp", "api_key_id": …}`。
        #3240）。イベントに残し、画面の取消は付けない。
        """
        with self._lock:
            run = self._require_run(run_id)
            if _is_terminal(run.status):
                # 終了した Run（完了・失敗・取消済み）は変えない。一覧の再取得の前に取消を
                # 送った、品質評価の取消と完了が重なった、などで来ても、完了の結果を
                # 取消済みで上書きしない（#911）。
                return run.model_copy(deep=True)
            self._cancel_run_locked(run, cancelled_by=cancelled_by, via=via)
            self._persist_locked()
            self._record_fact_locked(run)
            return run.model_copy(deep=True)

    def _cancel_run_locked(
        self,
        run: RunState,
        *,
        cancelled_by: str | None = None,
        via: Mapping[str, str] | None = None,
    ) -> None:
        """終わっていない Run を取り消す（承認・回答前の確認の待ちも取り消す）。

        利用者の操作の取消は、操作した人（`cancelled_by`。Run の利用者と異なることがある。
        system_admin の操作。#1928）と入口（`via`。MCP とキーの ID。#3240）をイベントに残す。

        取消の後は新しいツールを始めない（`start_builtin_tool_step`。#2609）。取消の時点で既に
        呼び先へ送っていた（実行中の）step は `in_flight_step_ids` に残し、未開始の step と
        分ける。送った操作は取り消せないので、その結果は届いたときに事実のまま記録する
        （`finish_builtin_tool_step`）。このプロセスで実行中の読み取りの呼び出しは、呼び先への
        接続を切って打ち切る（`call_abort`。#2691）。
        """
        cancelled_approval_ids: list[str] = []
        in_flight_step_ids = [step.id for step in run.steps if step.status == StepStatus.RUNNING]
        run.status = RunStatus.CANCELLED
        run.updated_at = _now()
        run.pending_tool_calls.clear()
        for step in run.steps:
            if step.status in {
                StepStatus.PENDING,
                StepStatus.RUNNING,
                StepStatus.WAITING_APPROVAL,
            }:
                step.status = StepStatus.CANCELLED
                step.completed_at = _now()
        for approval in run.approvals:
            if approval.status == ApprovalStatus.PENDING:
                approval.status = ApprovalStatus.CANCELLED
                approval.decided_by = "system"
                approval.decided_at = _now()
                cancelled_approval_ids.append(approval.id)
        for confirmation in run.confirmations:
            if confirmation.status == "pending":
                confirmation.status = "cancelled"
                confirmation.decided_at = _now()
        self._append_event(
            run,
            RunEventType.RUN_CANCELLED,
            "実行をキャンセルしました。",
            {
                "cancelled_approval_ids": cancelled_approval_ids,
                **({"in_flight_step_ids": in_flight_step_ids} if in_flight_step_ids else {}),
                **({"cancelled_by": cancelled_by} if cancelled_by else {}),
                **{key: value for key, value in (via or {}).items() if key in _CANCEL_VIA_KEYS},
            },
        )
        call_abort.abort_run_calls(run.id, call_abort.REASON_RUN_CANCELLED)

    def decide_approval(
        self,
        approval_id: str,
        request: ApprovalDecisionRequest,
        *,
        before_decision: Callable[[RunState], None] | None = None,
    ) -> RunState:
        with self._lock:
            approval = self._approvals.get(approval_id)
            if approval is None:
                raise KeyError(approval_id)
            run = self._require_run(approval.run_id)
            step = self._require_step(run, approval.step_id)
            if _is_terminal(run.status):
                if approval.status == ApprovalStatus.PENDING:
                    approval.status = ApprovalStatus.CANCELLED
                    approval.decided_by = request.decided_by
                    approval.decided_at = _now()
                    self._append_event(
                        run,
                        RunEventType.APPROVAL_DECIDED,
                        "終了済み実行のためツール承認を無効化しました。",
                        {
                            "approval_id": approval.id,
                            "approved": False,
                            "comment": request.comment,
                            "tool_name": step.tool_call.name if step.tool_call else step.kind,
                            "run_status": run.status.value,
                        },
                    )
                    self._persist_locked()
                return run.model_copy(deep=True)
            if approval.status != ApprovalStatus.PENDING:
                return run.model_copy(deep=True)

            # 採用する判断だけが外部設定を保存する。失敗したら承認は pending のまま再試行できる。
            if before_decision is not None:
                before_decision(run.model_copy(deep=True))
            approval.status = (
                ApprovalStatus.APPROVED if request.approved else ApprovalStatus.REJECTED
            )
            approval.decided_by = request.decided_by
            approval.decided_at = _now()
            self._append_event(
                run,
                RunEventType.APPROVAL_DECIDED,
                (
                    "ツール実行を承認し、今後このツールは自動で実行にしました。"
                    if request.approved and request.always_allow
                    else "ツール実行を承認しました。"
                    if request.approved
                    else "ツール実行を拒否しました。"
                ),
                {
                    "approval_id": approval.id,
                    "approved": request.approved,
                    "comment": request.comment,
                    "tool_name": step.tool_call.name if step.tool_call else step.kind,
                    # 監査: 承認と同時にツール権限を「自動実行」に変えたか（#1832）。
                    "always_allow": bool(request.approved and request.always_allow),
                },
            )
            if run.runtime_id == BUILTIN_RUNTIME_ID:
                # 組み込み Runtime（#754）は SDK が承認済みのツールを再開時に実行する。
                # ここでは決定だけを記録し、承認待ちが残らなければ再開を待つ状態（queued）に
                # する（再開は router が起動する）。
                if request.approved:
                    step.status = StepStatus.PENDING
                else:
                    step.status = StepStatus.CANCELLED
                    step.completed_at = _now()
                if _pending_approval_count(run) == 0:
                    # 回答前の確認（#1839）が残っていれば、利用者の答えを待つ。
                    run.status = (
                        RunStatus.WAITING_CONFIRMATION
                        if _pending_agent_confirmations(run)
                        else RunStatus.QUEUED
                    )
                    run.updated_at = _now()
                self._persist_locked()
                self._record_fact_locked(run)
                return run.model_copy(deep=True)
            # 旧エンジンの Run（#756 で実行を削除）は再開しない。決定だけを記録し、step を閉じる。
            step.status = StepStatus.CANCELLED
            step.completed_at = _now()
            self._persist_locked()
            return run.model_copy(deep=True)

    def decide_confirmation(
        self,
        run_id: str,
        confirmation_id: str,
        *,
        action: str,
        answers: Sequence[ConfirmationAnswer] = (),
        decided_by: str | None = None,
        actor_user_uuid: str | None = None,
    ) -> ConfirmationOutcome:
        """回答前の確認（#1839）に答える・スキップする・やめる。

        `actor_user_uuid` は答えた利用者（ツールの結果の確認の次の Run を実行する利用者。#1928）。
        省略すると確認を出した Run の利用者。呼ぶ側が Run を操作してよいかを確かめてから呼ぶ。

        - Agent 自身の確認（`ask_user_confirmation`）: 答え・スキップを記録し、待ちが残らなければ
          再開を待つ状態（queued）にする（再開は呼ぶ側が起動する）。
        - ツールの結果の確認（NL2SQL・RAG）: 答え・スキップを記録し、同じ会話に次の Run を作る
          （metadata `clarification_response` に答えを構造化して残す）。
        - やめる: 確認を出した Run と、同じ質問の確認の往復の Run を会話から外し、最初の質問を返す
          （入力欄に戻す）。Agent 自身の確認で待っている Run は取り消す。

        確認が無ければ KeyError、答えていない確認でなければ ConfirmationClosedError、答えが
        確認事項に合わなければ `ConfirmationAnswerError`。
        """
        with self._lock:
            run = self._require_run(run_id)
            confirmation = next(
                (item for item in run.confirmations if item.id == confirmation_id), None
            )
            if confirmation is None:
                raise KeyError(confirmation_id)
            if confirmation.status != "pending":
                raise ConfirmationClosedError(confirmation_id)
            if action == "cancel":
                return self._withdraw_confirmation_locked(run, confirmation, decided_by=decided_by)
            normalized = validate_answers(confirmation, answers) if action == "answer" else []
            confirmation.answers = normalized
            confirmation.status = "answered" if action == "answer" else "skipped"
            confirmation.decided_at = _now()
            self._append_event(
                run,
                RunEventType.RUNTIME_EVENT,
                (
                    "利用者が回答前の確認に答えました。"
                    if action == "answer"
                    else "利用者が確認せずに回答を求めました。"
                ),
                {
                    "confirmation_id": confirmation.id,
                    "source": confirmation.source,
                    "action": action,
                    "decided_by": decided_by,
                },
            )
            next_run: RunState | None = None
            if confirmation.source == SOURCE_AGENT:
                if (
                    run.status == RunStatus.WAITING_CONFIRMATION
                    and _pending_approval_count(run) == 0
                    and not _pending_agent_confirmations(run)
                ):
                    run.status = RunStatus.QUEUED
                run.updated_at = _now()
            else:
                question_run = self._question_run_locked(run)
                draft = run.metadata.get("agent_version") == "draft"
                next_run = self._create_builtin_run_locked(
                    RunCreateRequest(
                        goal=response_goal(confirmation, action),
                        agent_id=run.agent_id,
                        thread_id=run.thread_id,
                        draft=draft,
                    ),
                    # 次の Run は答えた利用者として実行する（答えるのは会話の持ち主だけ。呼ぶ側が
                    # 確かめる。#3244）。
                    created_by_user_uuid=actor_user_uuid or run.created_by_user_uuid,
                    agent_version=None,
                    # 会話を操作してよいかは呼ぶ側が確かめた（同じ会話の続き）。
                    all_users=True,
                    extra_metadata={
                        RESPONSE_METADATA_KEY: response_record(
                            confirmation,
                            action=action,
                            source_run_id=run.id,
                            question_run_id=question_run.id,
                        ),
                        ROUND_METADATA_KEY: confirmation.round,
                        # MCP の Run の続きは MCP の Run（承認の扱い・Run の数・監査。#1874）。
                        **{
                            key: run.metadata[key]
                            for key in RUN_ORIGIN_METADATA_KEYS
                            if key in run.metadata
                        },
                    },
                )
            self._persist_locked()
            self._record_fact_locked(run)
            if next_run is not None:
                self._record_fact_locked(next_run)
            return ConfirmationOutcome(
                run=run.model_copy(deep=True),
                next_run=next_run.model_copy(deep=True) if next_run is not None else None,
            )

    def _question_run_locked(self, run: RunState) -> RunState:
        """確認の往復の始まりの Run（利用者が質問を書いた Run）。"""
        response = run.metadata.get(RESPONSE_METADATA_KEY)
        question_run_id = response.get("question_run_id") if isinstance(response, dict) else None
        if isinstance(question_run_id, str) and question_run_id in self._runs:
            return self._runs[question_run_id]
        return run

    def _withdraw_confirmation_locked(
        self, run: RunState, confirmation: ConfirmationRequest, *, decided_by: str | None = None
    ) -> ConfirmationOutcome:
        """「確認をやめる」: 確認の往復を会話から外し、最初の質問を返す（#1839）。"""
        confirmation.status = "cancelled"
        confirmation.decided_at = _now()
        question_run = self._question_run_locked(run)
        chain = [run]
        if run.thread_id is not None and question_run.id != run.id:
            chain = [
                item
                for item in self._thread_runs_locked(run.thread_id)
                if question_run.created_at <= item.created_at <= run.created_at
            ]
        sessions: list[JsonObject] = []
        for item in chain:
            item.metadata[WITHDRAWN_METADATA_KEY] = True
            item.updated_at = _now()
            self._mark_run_changed(item)
            for pending in item.confirmations:
                if pending.status == "pending":
                    pending.status = "cancelled"
                    pending.decided_at = _now()
        if confirmation.source != SOURCE_AGENT:
            session_id = confirmation.context.get("requirement_session_id")
            if isinstance(session_id, str) and session_id:
                sessions.append(
                    {
                        "connection": str(confirmation.context.get("connection") or ""),
                        "requirement_session_id": session_id,
                    }
                )
        if not _is_terminal(run.status):
            self._cancel_run_locked(run, cancelled_by=decided_by)
        self._append_event(
            run,
            RunEventType.RUNTIME_EVENT,
            "利用者が回答前の確認をやめ、質問を入力欄に戻しました。",
            {
                "confirmation_id": confirmation.id,
                "withdrawn_run_ids": [item.id for item in chain],
                "decided_by": decided_by,
            },
        )
        self._persist_locked()
        for item in chain:
            self._record_fact_locked(item)
        return ConfirmationOutcome(
            run=run.model_copy(deep=True), question=question_run.goal, closed_sessions=sessions
        )

    # ---- 組み込み Runtime（#754。実行は builtin_runtime、記録はここ） ----

    def create_builtin_run(
        self,
        request: RunCreateRequest,
        *,
        created_by_user_uuid: str | None = None,
        agent_version: int | None = None,
        extra_metadata: JsonObject | None = None,
        all_users: bool = False,
        attachments: list[ChatAttachmentReference] | None = None,
    ) -> RunState:
        """組み込み Runtime の Run を投入する（実行は builtin_runtime.execute_run）。

        `agent_version` は公開した版を指定して実行する（品質評価が始めたときの版に固定する。#810）。
        API の `RunCreateRequest` からは指定できない。`extra_metadata` は Control Plane が付ける
        予約の metadata（回答前の確認の答え。#1839）。続ける会話（`thread_id`）は
        `created_by_user_uuid` の会話だけ（ほかは `ThreadNotFoundError`。#3244）。`all_users` は
        持ち主の確かめを呼ぶ側が済ませたときだけ渡す（会話の持ち主は変えない）。
        """
        with self._lock:
            run = self._create_builtin_run_locked(
                request,
                created_by_user_uuid=created_by_user_uuid,
                agent_version=agent_version,
                extra_metadata=extra_metadata,
                all_users=all_users,
                attachments=attachments,
            )
            self._persist_locked()
            self._record_fact_locked(run)
            return run.model_copy(deep=True)

    def _create_builtin_run_locked(
        self,
        request: RunCreateRequest,
        *,
        created_by_user_uuid: str | None,
        agent_version: int | None,
        extra_metadata: JsonObject | None,
        all_users: bool = False,
        attachments: list[ChatAttachmentReference] | None = None,
    ) -> RunState:
        agent = self._agents.get(request.agent_id)
        if agent is None:
            raise KeyError(request.agent_id)
        if not agent.enabled:
            raise ValueError("agent disabled")
        if not request.draft and agent.published() is None:
            raise AgentNotPublishedError(request.agent_id)
        if (
            not request.draft
            and agent_version is not None
            and not any(item.version == agent_version for item in agent.versions)
        ):
            raise AgentNotPublishedError(request.agent_id)
        # 使う版（#770）。下書きで試すときは "draft"。
        used_version: int | str = (
            "draft" if request.draft else agent_version or agent.published_version or "draft"
        )
        thread_owner = created_by_user_uuid
        if request.thread_id is not None:
            # 続ける会話は、自分の会話・同じ Agent のものだけ（他人の会話へ書き込ませない。
            # system_admin も同じ。#3244）。`all_users` は呼ぶ側が持ち主を確かめた続き。
            thread_runs = self._require_thread_locked(
                request.thread_id,
                user_uuid=created_by_user_uuid,
                agent_id=request.agent_id,
                all_users=all_users,
            )
            thread_owner = _thread_owner(thread_runs)
            # 会話は始めた Run の種類（下書き / 公開中の版）に固定する（#2515）。版の違う回答を
            # 1 つの会話の履歴に混ぜない。
            if _is_draft_thread(thread_runs) != request.draft:
                raise ThreadKindMismatchError(request.thread_id)
        run = RunState(
            id=f"run_{uuid4().hex}",
            goal=request.goal,
            agent_id=request.agent_id,
            runtime_id=BUILTIN_RUNTIME_ID,
            **_new_run_trace_contexts(),
            trace_store=True,
            created_by_user_uuid=created_by_user_uuid,
            thread_id=request.thread_id or f"thread_{uuid4().hex}",
            thread_owner_user_uuid=thread_owner,
            attachments=list(attachments or []),
            status=RunStatus.QUEUED,
            metadata={
                **request.metadata,
                "agent_version": used_version,
                **(
                    {CANCEL_METADATA_KEY: [button_record(request.clarification_cancel)]}
                    if request.clarification_cancel is not None
                    else {}
                ),
                **deepcopy(extra_metadata or {}),
            },
        )
        if request.thread_id is not None:
            # 会話が先へ進んだら、前の回答の回答前の確認（ツールの結果。#1839）は古い確認にする
            # （画面は最後の回答の答えていない確認だけを出す）。
            for previous in self._thread_runs_locked(request.thread_id):
                for confirmation in previous.confirmations:
                    if confirmation.status == "pending" and _is_terminal(previous.status):
                        confirmation.status = "expired"
                        confirmation.decided_at = _now()
                        self._mark_run_changed(previous)
        self._runs[run.id] = run
        self._append_event(
            run,
            RunEventType.RUN_CREATED,
            "実行を作成しました。",
            {"agent_id": request.agent_id, "runtime_id": BUILTIN_RUNTIME_ID},
        )
        return run

    def _thread_runs_locked(self, thread_id: str) -> list[RunState]:
        return sorted(
            (run for run in self._runs.values() if run.thread_id == thread_id),
            key=lambda run: run.created_at,
        )

    def _require_thread_locked(
        self,
        thread_id: str,
        *,
        user_uuid: str | None,
        agent_id: str | None = None,
        all_users: bool = False,
    ) -> list[RunState]:
        """会話の Run。持ち主の会話だけ（`all_users`〔system_admin。#1889〕なら全員）。"""
        runs = self._thread_runs_locked(thread_id)
        if (
            not runs
            or (not all_users and _thread_owner(runs) != user_uuid)
            or (agent_id is not None and runs[0].agent_id != agent_id)
        ):
            raise ThreadNotFoundError(thread_id)
        return runs

    def thread_history(self, run_id: str) -> list[ChatHistoryTurn]:
        """同じ会話の前の往復（質問・回答・使ったツールの名前。完了したものだけ、古い順）。

        渡す量（ターン数・文字数）は呼ぶ側が共通の予算で決める（#1818）。ツールの結果は渡さない。
        """
        with self._lock:
            run = self._require_run(run_id)
            if run.thread_id is None:
                return []
            return [
                run_history_turn(previous, answer)
                for previous in self._thread_runs_locked(run.thread_id)
                if previous.id != run.id
                and previous.created_at <= run.created_at
                and previous.status == RunStatus.COMPLETED
                and not is_withdrawn_run(previous)
                and (answer := run_answer_text(previous)) is not None
            ]

    def thread_previous_runs(self, run_id: str) -> list[RunState]:
        """同じ会話の、この Run より前の完了した Run（回答のあるもの、古い順。複製）。

        `thread_history`（モデルへ渡す前の往復）と同じ Run を選ぶ。根拠・データのツールを呼ばずに
        前の回答を言い換え・整形する Run を拒答にしないかの判定に使う（#1835）。
        """
        with self._lock:
            run = self._require_run(run_id)
            if run.thread_id is None:
                return []
            return [
                previous.model_copy(deep=True)
                for previous in self._thread_runs_locked(run.thread_id)
                if previous.id != run.id
                and previous.created_at <= run.created_at
                and previous.status == RunStatus.COMPLETED
                and run_answer_text(previous) is not None
            ]

    def thread_answer_validations(self, run_id: str) -> list[JsonObject]:
        """同じ会話の前の完了した Run の、最終の検証の成果物の内容（古い順。#1837）。

        前のターンで根拠で確かめた事実の根拠を、今のターンの最終の検証にも渡すのに使う。読むのは
        `thread_history` と同じ、同じ会話のこの Run より前の完了した Run だけ（会話は作った利用者・
        同じ Agent だけが続けられる）。成果物の種類は `answer_validation.ANSWER_VALIDATION_KIND`。
        """
        with self._lock:
            run = self._require_run(run_id)
            if run.thread_id is None:
                return []
            return [
                deepcopy(artifact.content)
                for previous in self._thread_runs_locked(run.thread_id)
                if previous.id != run.id
                and previous.created_at <= run.created_at
                and previous.status == RunStatus.COMPLETED
                for artifact in previous.artifacts
                if artifact.kind == "answer_validation" and isinstance(artifact.content, dict)
            ]

    def support_task_context(self, run_id: str) -> tuple[str, JsonObject | None]:
        """支援タスクの目的（会話の最初の質問）と、前の完了した Run の状態（#1243）。

        読むのは同じ会話で、同じ持ち主（会話の持ち主。#1928）の、この Run より前の Run だけ。
        会話を続けられるのは持ち主だけ（#768・#3244）。#3244 より前に system_admin が続けた Run も
        同じ会話の状態を引き継ぐ。保存先の不整合で別の会話の Run が混ざっても、その状態は使わない。
        前の完了した Run より後の失敗・取消の Run の消費は、状態の会話の通しの予算に足す
        （#1277。失敗・取消を繰り返して予算を超えさせない）。
        """
        with self._lock:
            run = self._require_run(run_id)
            if run.thread_id is None:
                return run.goal, None
            owner = run_thread_owner(run)
            owned = [
                previous
                for previous in self._thread_runs_locked(run.thread_id)
                if run_thread_owner(previous) == owner and previous.created_at <= run.created_at
            ]
            goal = owned[0].goal if owned else run.goal
            abandoned: list[JsonObject] = []
            state: JsonObject | None = None
            for previous in reversed(owned):
                if previous.id == run.id:
                    continue
                if is_withdrawn_run(previous):
                    # 「確認をやめる」で外した往復（#1839）は、状態を引き継がない。
                    continue
                if previous.status in {RunStatus.FAILED, RunStatus.CANCELLED}:
                    consumed = abandoned_run_consumption(previous.steps)
                    # ツールを呼ばずに止まった Run は、引き継ぐ状態を作らない。
                    if consumed["tool_calls"]:
                        abandoned.append(consumed)
                    continue
                if previous.status != RunStatus.COMPLETED:
                    continue
                content = run_support_task(previous)
                if (
                    content is not None
                    and content.get("thread_id") == run.thread_id
                    and content.get("owner_user_uuid") == owner
                ):
                    state = deepcopy(content)
                    break
            return goal, with_abandoned_consumption(
                state,
                abandoned,
                thread_id=run.thread_id,
                owner_user_uuid=owner,
            )

    def save_builtin_artifact(
        self, run_id: str, *, kind: str, name: str, content: JsonObject
    ) -> None:
        """Control Plane が作る成果物（回答の検証など。#1246）を残す（同じ Run・種類は上書き）。"""
        with self._lock:
            run = self._require_run(run_id)
            if _is_terminal(run.status):
                return
            existing = next((item for item in run.artifacts if item.kind == kind), None)
            if existing is not None:
                existing.content = deepcopy(content)
            else:
                artifact = Artifact(name=name, kind=kind, content=deepcopy(content))
                run.artifacts.append(artifact)
                self._append_event(
                    run,
                    RunEventType.ARTIFACT_CREATED,
                    f"{name}を保存しました。",
                    {"artifact_id": artifact.id, "kind": artifact.kind},
                )
            run.updated_at = _now()
            self._mark_run_changed(run)
            self._persist_locked()

    def save_support_task(self, run_id: str, content: JsonObject) -> None:
        """支援タスクの状態を Run の成果物に残す（同じ Run では 1 つを上書きする。#1243）。"""
        with self._lock:
            run = self._require_run(run_id)
            if _is_terminal(run.status):
                return
            existing = next(
                (item for item in run.artifacts if item.kind == SUPPORT_TASK_KIND), None
            )
            if existing is not None:
                existing.content = deepcopy(content)
            else:
                artifact = Artifact(
                    name=SUPPORT_TASK_NAME, kind=SUPPORT_TASK_KIND, content=deepcopy(content)
                )
                run.artifacts.append(artifact)
                self._append_event(
                    run,
                    RunEventType.ARTIFACT_CREATED,
                    "支援タスクの状態を保存しました。",
                    {"artifact_id": artifact.id, "kind": artifact.kind},
                )
            run.updated_at = _now()
            self._mark_run_changed(run)
            self._persist_locked()

    def list_threads(
        self,
        *,
        user_uuid: str | None,
        agent_id: str | None = None,
        all_users: bool = False,
        draft: bool = False,
    ) -> list[ThreadSummary]:
        """利用者の会話の一覧（新しい順）。

        会話は持ち主だけが見る（`all_users`〔system_admin。#1889〕なら全員の会話）。持ち主は
        会話を始めた利用者で、system_admin が続けた Run も持ち主の会話に入る（#1928）。
        `draft` は下書きの会話だけ、既定は公開中の版の会話だけ（#2515）。
        """
        with self._lock:
            grouped: dict[str, list[RunState]] = {}
            for run in self._runs.values():
                if run.thread_id is None:
                    continue
                if agent_id is not None and run.agent_id != agent_id:
                    continue
                grouped.setdefault(run.thread_id, []).append(run)
            summaries = []
            for thread_id, runs in grouped.items():
                runs.sort(key=lambda item: item.created_at)
                if _is_draft_thread(runs) != draft:
                    continue
                owner = _thread_owner(runs)
                if not all_users and owner != user_uuid:
                    continue
                # 「確認をやめる」で外した Run（#1839）は会話に出さない（すべて外れた会話も）。
                visible = [run for run in runs if not is_withdrawn_run(run)]
                if visible:
                    summaries.append(_thread_summary(thread_id, visible, owner_user_uuid=owner))
            return sorted(summaries, key=lambda item: item.updated_at, reverse=True)

    def get_thread(
        self, thread_id: str, *, user_uuid: str | None, all_users: bool = False
    ) -> ThreadData:
        with self._lock:
            runs = self._require_thread_locked(thread_id, user_uuid=user_uuid, all_users=all_users)
            return ThreadData(
                thread_id=thread_id,
                agent_id=runs[0].agent_id,
                owner_user_uuid=_thread_owner(runs),
                # 「確認をやめる」で外した Run（#1839）は会話に出さない。
                runs=[run.model_copy(deep=True) for run in runs if not is_withdrawn_run(run)],
            )

    def rename_thread(
        self, thread_id: str, *, user_uuid: str | None, title: str, all_users: bool = False
    ) -> ThreadSummary:
        """会話の名前を変える（作った利用者だけ。#1817。`all_users` は system_admin。#1889）。

        名前は最初の Run の metadata に置く。
        """
        with self._lock:
            runs = self._require_thread_locked(thread_id, user_uuid=user_uuid, all_users=all_users)
            runs[0].metadata[THREAD_TITLE_METADATA_KEY] = title.strip()[:THREAD_TITLE_MAX_LENGTH]
            self._mark_run_changed(runs[0])
            self._persist_locked()
            return _thread_summary(thread_id, runs, owner_user_uuid=_thread_owner(runs))

    def delete_thread(
        self, thread_id: str, *, user_uuid: str | None, all_users: bool = False
    ) -> None:
        """会話を消す（作った利用者だけ。#1817。`all_users` は system_admin。#1889）。

        Run は監査・フィードバック・評価のケース・利用量の元なので消さず、会話からの参照
        （`thread_id`）だけを外す。会話の一覧・詳細・続きから見えなくなる。元の会話は
        metadata の `deleted_thread_id` に残す。回答の作成中・承認待ちの Run があれば消さない。
        """
        with self._lock:
            runs = self._require_thread_locked(thread_id, user_uuid=user_uuid, all_users=all_users)
            if any(not _is_terminal(run.status) for run in runs):
                raise ThreadBusyError(thread_id)
            for run in runs:
                run.metadata[DELETED_THREAD_METADATA_KEY] = thread_id
                run.thread_id = None
                self._mark_run_changed(run)
            self._persist_locked()
            for run in runs:
                self._record_fact_locked(run)

    def begin_builtin_run(
        self, run_id: str, *, claim: RunClaim | None = None
    ) -> tuple[RunState, AgentProfile] | None:
        """実行を始める（開始できない・取消済み・終了済みなら None）。

        queued の Run を、`claim`（dispatcher）が今の lease と一致するとき・claim なし（in_process）
        なら期限内の lease を別の worker が持たないときだけ、1 回だけ開始する（#2606）。
        """
        with self._lock:
            return self._begin_builtin_run_locked(run_id, claim)

    def _begin_builtin_run_locked(
        self, run_id: str, claim: RunClaim | None
    ) -> tuple[RunState, AgentProfile] | None:
        run = self._require_run(run_id)
        if not self._may_begin_locked(run, claim):
            return None
        agent = self._agents.get(run.agent_id)
        if agent is None:
            self._fail_builtin_locked(
                run, "runtime.agent_not_found", "業務 Agent が見つかりません。"
            )
            return None
        expected_definition = run.metadata.get("_evaluation_definition_hash")
        if isinstance(expected_definition, str):
            from app.features.agent.evaluation import draft_definition_hash

            if draft_definition_hash(agent) != expected_definition:
                self._fail_builtin_locked(
                    run,
                    "evaluation.definition_changed",
                    "評価の開始後に実行条件が変わったため、実行しません。評価し直してください。",
                )
                return None
        self._set_builtin_running_locked(run, "実行を開始しました。", claim)
        return run.model_copy(deep=True), agent_for_run(agent, run)

    def begin_builtin_resume(
        self, run_id: str, *, claim: RunClaim | None = None
    ) -> tuple[RunState, AgentProfile, str, dict[str, bool]] | None:
        """承認の決定を反映して再開する（開始できない・承認待ちが残る・状態が無いときは None）。

        開始の条件は `begin_builtin_run` と同じ（queued・今の claim。#2606）。
        """
        with self._lock:
            return self._begin_builtin_resume_locked(run_id, claim)

    def _begin_builtin_resume_locked(
        self, run_id: str, claim: RunClaim | None
    ) -> tuple[RunState, AgentProfile, str, dict[str, bool]] | None:
        run = self._require_run(run_id)
        if not self._may_begin_locked(run, claim):
            return None
        agent = self._agents.get(run.agent_id)
        state_text = run.metadata.get(_BUILTIN_STATE_KEY)
        if _pending_approval_count(run) > 0 or _pending_agent_confirmations(run):
            return None
        if agent is None or not isinstance(state_text, str):
            self._fail_builtin_locked(
                run, "runtime.resume_state_missing", "再開に必要な実行の状態がありません。"
            )
            return None
        decisions = {
            approval.tool_call.trace_id or "": approval.status == ApprovalStatus.APPROVED
            for approval in run.approvals
            if approval.tool_call.trace_id
            and approval.status in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}
        }
        # 回答前の確認（#1839）: 答え・スキップした確認は実行し、ツールの結果で答えを返す。
        decisions.update(
            {
                item.call_id: True
                for item in run.confirmations
                if item.source == SOURCE_AGENT
                and item.call_id
                and item.status in {"answered", "skipped"}
            }
        )
        run.metadata.pop(_BUILTIN_STATE_KEY, None)
        self._set_builtin_running_locked(
            run, "承認・確認の答えを反映して実行を再開しました。", claim
        )
        return run.model_copy(deep=True), agent_for_run(agent, run), state_text, decisions

    def builtin_run_ended(self, run_id: str) -> bool:
        """この実行が新しいツール・モデルの呼び出しを始めてはいけないか（#2609・#2606）。

        Run が終わった（取消・完了・失敗）か、この文脈の実行が別の実行に置き換わった（実行の
        token が違う）ときに True。別のプロセス（API と runtime-dispatcher）の取消・開始も、
        保存先の Run の行を読み直して判定する（`_refresh_run_from_storage`）。I/O があるので
        event loop の外で呼ぶ。無い Run は False（取消とは扱わない）。
        """
        stored_token = self._refresh_run_from_storage(run_id)
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return False
            return self._run_closed_for_tools_locked(run, stored_token)

    def _run_closed_for_tools_locked(self, run: RunState, stored_token: str | None = None) -> bool:
        """終わった Run・置き換わった実行か（新しいツールを始めない。#2609・#2606）。"""
        if _is_terminal(run.status):
            return True
        mine = run_claim.current_execution_token(run.id)
        if mine is not None and stored_token is not None and stored_token != mine:
            # 別のプロセスで次の実行が始まった（このプロセスのメモリはまだ古い）。
            logger.warning(
                "agent_runtime_stale_execution_write_ignored",
                extra={"run_id": run.id, "write": "tool_start", "superseded": True},
            )
            return True
        return self._stale_execution_locked(run, "tool_start", segment_end=False)

    def approved_builtin_call(
        self, run_id: str, call_id: str, tool_name: str
    ) -> ApprovalRequest | None:
        """この Run・呼び出し ID・ツールに対する承認済みの記録（無ければ None。#2610）。

        「承認」のツールを実行してよいのは、この記録がある呼び出しだけ（SDK を経由したことだけでは
        実行しない）。引数の照合は呼ぶ側が行う。
        """
        if not call_id:
            return None
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return None
            approval = next(
                (
                    item
                    for item in run.approvals
                    if item.status == ApprovalStatus.APPROVED
                    and item.tool_call.trace_id == call_id
                    and item.tool_call.name == tool_name
                ),
                None,
            )
            return approval.model_copy(deep=True) if approval is not None else None

    def _refresh_run_from_storage(self, run_id: str) -> str | None:
        """別のプロセスが保存先で取り消した Run をメモリに反映し、保存先の実行の token を返す
        （memory / file は 1 プロセスなので何もせず None）。"""
        return None

    def _adopt_stored_cancel_locked(self, run: RunState, stored: RunState) -> bool:
        """保存先で取り消し済みの Run を、このプロセスのメモリでも取り消しにする（#2609）。

        取消は確定した操作なので、ほかのプロセスの古い「実行中」で上書きしない（取消が勝つ）。
        反映したら True。
        """
        if stored.status != RunStatus.CANCELLED or _is_terminal(run.status):
            return False
        cancelled = next(
            (
                event
                for event in reversed(stored.events)
                if event.type == RunEventType.RUN_CANCELLED
            ),
            None,
        )
        payload = cancelled.payload if cancelled is not None else {}
        cancelled_by = payload.get("cancelled_by")
        self._cancel_run_locked(
            run,
            cancelled_by=cancelled_by if isinstance(cancelled_by, str) else None,
            via={
                key: value
                for key, value in payload.items()
                if key in _CANCEL_VIA_KEYS and isinstance(value, str)
            },
        )
        self._mark_run_changed(run)
        logger.info(
            "agent_runtime_run_cancelled_by_another_process",
            extra={"run_id": run.id, "writer_id": checkpoint_store.WRITER_ID},
        )
        return True

    def start_builtin_tool_step(
        self, run_id: str, call: ToolCall, *, cleanup: bool = False
    ) -> tuple[str, ToolInvocationContext]:
        """ツールの step を始める（承認済みの step があれば、それを実行中にする）。

        終わった Run（取消・完了・失敗）と、別の実行に置き換わった実行（#2606）では step を作らずに
        `RunNotActiveError`（#2609）。取消と同じ lock の中で判定するので、取消が先に確定した
        呼び出しは始まらない。`cleanup` は取消の後でも呼んでよい後始末（確認をやめた NL2SQL の
        session を閉じる。#1441）だけに使う。
        """
        with self._lock:
            run = self._require_run(run_id)
            if not cleanup and self._run_closed_for_tools_locked(run):
                raise RunNotActiveError(run_id, run.status)
            step = next(
                (
                    item
                    for item in run.steps
                    if call.trace_id
                    and item.tool_call is not None
                    and item.tool_call.trace_id == call.trace_id
                    and item.status in {StepStatus.PENDING, StepStatus.WAITING_APPROVAL}
                ),
                None,
            )
            if step is None:
                step = RunStep(run_id=run_id, tool_call=call)
                run.steps.append(step)
            elif step.tool_call is not None and call.data_scope is not None:
                # 承認済みの step は、データの範囲を当てて実際に送る引数を残す（#1378）。
                step.tool_call = step.tool_call.model_copy(
                    update={"arguments": call.arguments, "data_scope": call.data_scope}
                )
            step.status = StepStatus.RUNNING
            step.started_at = _now()
            self._append_event(
                run,
                RunEventType.STEP_STARTED,
                f"ツール {call.name} を開始しました。",
                {"step_id": step.id, "tool_name": call.name},
            )
            context = self._tool_invocation_context_locked(run, call, approval_id=step.approval_id)
            self._persist_locked()
            return step.id, context

    def finish_builtin_tool_step(self, run_id: str, step_id: str, result: ToolResult) -> None:
        """ツールの結果を step・イベント・成果物に記録する。

        失敗しても Run は続ける（結果を受け取ったモデルが判断する）。取消の前に送った操作の結果が
        取消の後に届いたら、届いた結果を事実のまま記録し（取り消せたとは扱わない）、イベントに
        Run の状態（`run_status`）を残す（#2609）。取消で打ち切った呼び出し（#2691。
        `TOOL_CALL_ABORTED_CODE`）は、失敗ではなく取消の step にする。
        """
        with self._lock:
            run = self._require_run(run_id)
            step = self._require_step(run, step_id)
            step.tool_result = result
            step.completed_at = _now()
            call_name = step.tool_call.name if step.tool_call else step.kind
            after_end = {"run_status": run.status.value} if _is_terminal(run.status) else {}
            if after_end:
                logger.info(
                    "agent_runtime_tool_result_after_run_end",
                    extra={
                        "run_id": run_id,
                        "step_id": step_id,
                        "tool_name": call_name,
                        "run_status": run.status.value,
                        "success": result.success,
                    },
                )
            if result.success:
                step.status = StepStatus.COMPLETED
                self._record_tool_success_artifacts(run, step, result)
                self._append_event(
                    run,
                    RunEventType.TOOL_COMPLETED,
                    f"ツール {call_name} が完了しました。",
                    {
                        "step_id": step.id,
                        "tool_name": call_name,
                        "output": result.output,
                        "duration_ms": result.duration_ms,
                        "guardrail_warnings": result.guardrail_warnings,
                        "audit_metadata": result.audit_metadata,
                        **after_end,
                    },
                )
                self._append_guardrail_events(run, step, result)
            else:
                aborted = result.error_code == call_abort.TOOL_CALL_ABORTED_CODE
                step.status = StepStatus.CANCELLED if aborted else StepStatus.FAILED
                self._append_event(
                    run,
                    RunEventType.TOOL_FAILED,
                    f"ツール {call_name} の呼び出しを取消で打ち切りました。"
                    if aborted
                    else f"ツール {call_name} が失敗しました。",
                    {
                        "step_id": step.id,
                        "tool_name": call_name,
                        "error": result.error,
                        "error_code": result.error_code,
                        "error_details": result.error_details,
                        "duration_ms": result.duration_ms,
                        "audit_metadata": result.audit_metadata,
                        **after_end,
                    },
                )
            run.updated_at = _now()
            self._persist_locked()

    def request_builtin_approvals(
        self,
        run_id: str,
        calls: Sequence[ToolCall],
        *,
        state: str,
        confirmations: Sequence[ConfirmationRequest] = (),
    ) -> RunState:
        """承認が必要なツールで中断した Run を承認待ちにし、再開に使う状態を保存する。

        `confirmations` は Agent 自身の回答前の確認（`ask_user_confirmation`。#1839）。承認が無く
        確認だけなら `waiting_confirmation` にする（承認と確認が両方あれば承認待ち）。
        """
        with self._lock:
            run = self._require_run(run_id)
            if _is_terminal(run.status) or self._stale_execution_locked(
                run, "request_approvals", segment_end=True
            ):
                return run.model_copy(deep=True)
            for confirmation in confirmations:
                run.confirmations.append(confirmation.model_copy(deep=True))
                self._append_event(
                    run,
                    RunEventType.RUNTIME_EVENT,
                    "回答前の確認で、利用者の答えを待っています。",
                    {
                        "confirmation_id": confirmation.id,
                        "source": confirmation.source,
                        "round": confirmation.round,
                    },
                )
            for call in calls:
                step = RunStep(
                    run_id=run_id,
                    status=StepStatus.WAITING_APPROVAL,
                    tool_call=call,
                    started_at=_now(),
                )
                approval = ApprovalRequest(
                    run_id=run_id,
                    step_id=step.id,
                    tool_call=call,
                    reason=f"{call.name} は承認が必要です。",
                )
                step.approval_id = approval.id
                run.steps.append(step)
                run.approvals.append(approval)
                self._approvals[approval.id] = approval
                self._append_event(
                    run,
                    RunEventType.TOOL_APPROVAL_REQUIRED,
                    f"ツール {call.name} は承認待ちです。",
                    {
                        "approval_id": approval.id,
                        "step_id": step.id,
                        "tool_name": call.name,
                        "policy_decision": "ask",
                    },
                )
            run.metadata[_BUILTIN_STATE_KEY] = state
            run.status = RunStatus.WAITING_APPROVAL if calls else RunStatus.WAITING_CONFIRMATION
            run.updated_at = _now()
            self._mark_run_changed(run)
            self._persist_locked()
            self._record_fact_locked(run)
            return run.model_copy(deep=True)

    def complete_builtin_run(
        self,
        run_id: str,
        answer: str,
        *,
        outcome: JsonObject | None = None,
        confirmations: Sequence[ConfirmationRequest] = (),
        notice: str | None = None,
    ) -> RunState:
        """モデルの最終回答を成果物に残して完了にする。

        `outcome` は回答の対応（#1305。`answer_outcome.answer_outcome`）。あれば成果物の
        `outcome` に残す。`confirmations` はツールの結果の回答前の確認（NL2SQL・RAG。#1839）で、
        完了した Run に出し、利用者の答えで次の Run を作る。`notice` は回答の注意書き（#2405）で、
        本文（`text`。会話の履歴に使う）とは別の項目に残す。
        """
        with self._lock:
            run = self._require_run(run_id)
            if _is_terminal(run.status) or self._stale_execution_locked(
                run, "complete", segment_end=True
            ):
                return run.model_copy(deep=True)
            for confirmation in confirmations:
                run.confirmations.append(confirmation.model_copy(deep=True))
            content: JsonObject = {"text": answer}
            if outcome is not None:
                content["outcome"] = outcome
            if notice:
                content["notice"] = notice
            artifact = Artifact(name="回答", kind="answer", content=content)
            run.artifacts.append(artifact)
            self._append_event(
                run,
                RunEventType.ARTIFACT_CREATED,
                "回答を保存しました。",
                {"artifact_id": artifact.id, "kind": artifact.kind},
            )
            run.status = RunStatus.COMPLETED
            run.updated_at = _now()
            self._append_event(run, RunEventType.RUN_COMPLETED, "実行を完了しました。")
            self._persist_locked()
            self._record_fact_locked(run)
            return run.model_copy(deep=True)

    def fail_builtin_run(self, run_id: str, *, code: str, detail: str) -> RunState:
        with self._lock:
            run = self._require_run(run_id)
            if not _is_terminal(run.status) and not self._stale_execution_locked(
                run, "fail", segment_end=True
            ):
                self._fail_builtin_locked(run, code, detail)
            return run.model_copy(deep=True)

    def fail_interrupted_runs(self, *, updated_before: datetime) -> list[str]:
        """前のプロセスが実行していた `running` / `queued` の Run を失敗にする（#1894）。

        in_process の backend の起動で、保存先を読み込んだ後に 1 回だけ呼ぶ（呼び出しは
        `storage_bootstrap`）。`updated_before`（このプロセスの起動の時刻）より後に作った・
        変えた Run は、このプロセスか別のプロセスが動かしている途中かもしれないので触らない。
        承認待ち・確認待ちは保存した SDK の状態から再開できるので触らない。
        失敗にした Run の ID を返す。
        """
        with self._lock:
            interrupted = [
                run
                for run in self._sorted_runs_locked()
                if run.status in {RunStatus.RUNNING, RunStatus.QUEUED}
                and _aware(run.updated_at) < _aware(updated_before)
            ]
            for run in interrupted:
                self._fail_interrupted_locked(run)
            if interrupted:
                self._persist_locked()
                for run in interrupted:
                    self._record_fact_locked(run)
            return [run.id for run in interrupted]

    def _fail_interrupted_locked(self, run: RunState) -> None:
        now = _now()
        previous_status = run.status.value
        for step in run.steps:
            if step.status in {StepStatus.PENDING, StepStatus.RUNNING}:
                step.status = StepStatus.FAILED
                step.completed_at = step.completed_at or now
        run.pending_tool_calls.clear()
        run.metadata.pop(run_claim.LEASE_METADATA_KEY, None)
        run.metadata.pop(_BUILTIN_STATE_KEY, None)
        run.status = RunStatus.FAILED
        run.updated_at = now
        self._append_event(
            run,
            RunEventType.RUNTIME_FAILED,
            INTERRUPTED_MESSAGE,
            {
                "error_code": INTERRUPTED_ERROR_CODE,
                "runtime_id": run.runtime_id,
                "previous_status": previous_status,
                "source": "startup",
            },
        )

    def set_run_feedback(
        self, run_id: str, feedback: RunFeedback, *, admin: bool = False
    ) -> RunState:
        """回答への評価を保存する（上書き。#774）。完了していない Run は RunNotRatableError。

        `admin` は管理者の評価（本人の評価とは別の項目）。
        """
        with self._lock:
            run = self._require_run(run_id)
            if run.status != RunStatus.COMPLETED or run_answer_text(run) is None:
                raise RunNotRatableError(run_id)
            if admin:
                run.admin_review = feedback
            else:
                run.feedback = feedback
            self._mark_run_changed(run)
            self._persist_locked()
            self._record_fact_locked(run)
            return run.model_copy(deep=True)

    def record_builtin_dry_run_steps(self, run_id: str, calls: Sequence[ToolCall]) -> None:
        """評価の Run で実行しなかった（承認が要る）ツールの呼び出しを step に残す（#776）。

        モデルがどのツールをどの引数で呼ぼうとしたかを、品質評価のツールの選択の判定に使う。
        """
        self._record_unexecuted_steps(
            run_id,
            calls,
            error=lambda _call: EVALUATION_DRY_RUN_MESSAGE,
            error_code="evaluation.dry_run",
            event=lambda call: (
                f"評価中のため、承認が要るツール {call.name} を実行しませんでした。",
                {"tool_name": call.name, "evaluation_dry_run": True},
            ),
        )

    def record_builtin_unattended_skips(self, run_id: str, calls: Sequence[ToolCall]) -> None:
        """無人の Run で、承認が要るため使わなかったツールの呼び出しを残す。

        外部の MCP クライアントの Run（#1874）。step（監査ログのツールの呼び出し）と Run の
        経過のイベントに、呼ばなかったことを残す。
        """
        code = MCP_APPROVAL_SKIPPED_CODE
        self._record_unexecuted_steps(
            run_id,
            calls,
            error=lambda call: mcp_approval_message(call.name),
            error_code=code,
            event=lambda call: (
                f"外部の MCP クライアントからの実行のため、"
                f"承認が要るツール {call.name} を使いませんでした。",
                {"tool_name": call.name, "severity": "warning", "error_code": code},
            ),
        )

    def _record_unexecuted_steps(
        self,
        run_id: str,
        calls: Sequence[ToolCall],
        *,
        error: Callable[[ToolCall], str],
        error_code: str,
        event: Callable[[ToolCall], tuple[str, JsonObject]],
    ) -> None:
        """承認が要るため実行しなかったツールの呼び出しを、取り消しの step とイベントに残す。"""
        now = _now()
        with self._lock:
            run = self._require_run(run_id)
            for call in calls:
                run.steps.append(
                    RunStep(
                        run_id=run.id,
                        status=StepStatus.CANCELLED,
                        tool_call=call,
                        tool_result=ToolResult(
                            name=call.name,
                            success=False,
                            error=error(call),
                            error_code=error_code,
                            policy_decision=ToolPolicyDecision.ASK,
                            started_at=now,
                            completed_at=now,
                        ),
                        started_at=now,
                        completed_at=now,
                    )
                )
                message, payload = event(call)
                self._append_event(run, RunEventType.RUNTIME_EVENT, message, payload)
            run.updated_at = now
            self._mark_run_changed(run)
            self._persist_locked()

    def run_statuses(self, run_ids: Collection[str]) -> dict[str, RunStatus]:
        """Run の状態をまとめて引く（一覧の「前回」。#1812）。

        Run を 1 件ずつ複製せずに、ロックを 1 回だけ取って状態だけを返す。無い Run は含めない。
        """
        with self._lock:
            return {
                run_id: run.status
                for run_id in run_ids
                if (run := self._runs.get(run_id)) is not None
            }

    def record_builtin_usage(self, run_id: str, usage: RunUsage) -> None:
        """モデルの利用量（再開を含めた累計）を Run に記録する（#772）。"""
        with self._lock:
            run = self._runs.get(run_id)
            # 別の実行が始まった後の遅れた利用量は、新しい実行の累計を上書きしない（#2606）。
            if run is None or self._stale_execution_locked(run, "usage", segment_end=False):
                return
            run.usage = usage
            self._mark_run_changed(run)
            self._persist_locked()
            self._record_fact_locked(run)

    def record_run_budget(self, run_id: str, snapshot: JsonObject) -> None:
        """Run 全体の実行予算の消費（再開を含めた累計。#68 `run_budget`）を metadata に残す。

        取り消し・失敗で終えた Run にも残す（消費は戻らない）。別の実行が始まった後の遅れた
        書き戻しは、新しい実行の累計を上書きしない（#2606）。
        """
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or self._stale_execution_locked(run, "run_budget", segment_end=False):
                return
            run.metadata = {**run.metadata, RUN_BUDGET_METADATA_KEY: deepcopy(snapshot)}
            self._mark_run_changed(run)
            self._persist_locked()

    def note_builtin_warning(self, run_id: str, message: str) -> None:
        """実行は続けるが利用者に伝えたいこと（取得できなかった MCP 接続など）をイベントに残す。"""
        with self._lock:
            run = self._require_run(run_id)
            self._append_event(run, RunEventType.RUNTIME_EVENT, message, {"severity": "warning"})
            self._persist_locked()

    def note_builtin_tool_titles(self, run_id: str, titles: Mapping[str, str]) -> None:
        """Run に渡したツールの表示名（MCP の `title`）を metadata に残す（処理の段階の名前）。

        承認の後の再開で取り直した分は足す（同じ名前は新しい表示名にする）。
        """
        from app.features.agent.chat_progress import TOOL_TITLES_METADATA_KEY

        if not titles:
            return
        with self._lock:
            run = self._require_run(run_id)
            current = run.metadata.get(TOOL_TITLES_METADATA_KEY)
            merged = dict(current) if isinstance(current, dict) else {}
            merged.update(titles)
            if merged == current:
                return
            run.metadata[TOOL_TITLES_METADATA_KEY] = merged
            self._mark_run_changed(run)
            self._persist_locked()

    def builtin_tool_titles(self, run_id: str) -> dict[str, str]:
        """Run に渡したツールの表示名（処理の経過のツールの span の名前の値。無い Run は空）。"""
        from app.features.agent.chat_progress import tool_titles

        with self._lock:
            run = self._runs.get(run_id)
            return tool_titles(run) if run is not None else {}

    def builtin_tool_steps(self, run_id: str, call_ids: Collection[str]) -> list[RunStep]:
        """呼び出し ID のツールの step（処理の経過のツールの span の詳細。記録の順）。"""
        wanted = {call_id for call_id in call_ids if call_id}
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or not wanted:
                return []
            return [
                step.model_copy(deep=True)
                for step in run.steps
                if step.tool_call is not None and step.tool_call.trace_id in wanted
            ]

    def builtin_tool_step(self, run_id: str, step_id: str) -> RunStep | None:
        """ツールの step（処理の経過のツールの span の詳細。無ければ None）。"""
        with self._lock:
            run = self._runs.get(run_id)
            step = (
                next((item for item in run.steps if item.id == step_id), None)
                if run is not None
                else None
            )
            return step.model_copy(deep=True) if step is not None else None

    def record_builtin_span(self, run_id: str, update: SpanUpdate) -> bool:
        """実行区間の span の知らせを受け付けるか決め、表に入れないものを Run に残す（#3226）。

        受け付けた知らせ（True）は、呼んだ側が共通の span の表に書く（`trace_spans`）。受け付けない
        のは、Run が無い・組み込み Runtime の Run でない・置き換わった実行の知らせと、終わった Run
        の知らせ（停止の後に終わった span。表では実行中のまま残り、画面は Run の終端に合わせて
        閉じる）。実行の全体の span（root）の終わりは、終わった Run でも受け付ける。

        Run に残すのは trace id・工程の詳細・記録した待ちの回（`note_run_span`）。
        `operation_trace` の listener から呼ぶ。知らせは event loop・ツールのスレッドのどちらからも
        届き、Run の lock の中（実行区間を確定する `finish_operation_trace` が root の span を
        閉じる）からも届くので、lock を持っていればそのまま記録する（保存は lock を持つ側が
        積む）。lock を取って Run を変えたときは、少し待ってまとめて保存の予定に積み、background
        の書き込みに任せる（ツールのスレッドで保存先への書き込みを待たない。
        `SPAN_PERSIST_DELAY_SECONDS`）。
        """
        if self._lock.held_by_current_thread():
            accepted, _changed = self._record_builtin_span_locked(run_id, update)
            return accepted
        schedule = False
        with self._lock:
            accepted, changed = self._record_builtin_span_locked(run_id, update)
            if not changed:
                return accepted
            self._condition.notify_all()
            if not (BACKGROUND_WRITES and self._stores_changes()):
                self._persist_locked()
                return accepted
            # span の知らせは 1 回の実行で多いので、少し待ってまとめて保存の予定に積む（Run の
            # JSON を知らせのたびに作らない）。メモリの Run はすぐ変わるので、同じプロセスの
            # 読み取りは待たない。
            schedule = not self._span_persist_scheduled
            self._span_persist_scheduled = True
        if schedule:
            timer = threading.Timer(SPAN_PERSIST_DELAY_SECONDS, self._persist_span_changes)
            timer.daemon = True
            timer.start()
        return accepted

    def _persist_span_changes(self) -> None:
        """まとめた span の記録を保存の予定に積み、background の書き込みに任せる。"""
        with self._lock:
            self._span_persist_scheduled = False
            staged = self._persist_locked(background=True)
        if staged:
            self._request_background_write()

    def _record_builtin_span_locked(self, run_id: str, update: SpanUpdate) -> tuple[bool, bool]:
        """知らせを受け付けるか（1 つ目）と、Run を変えたか（2 つ目）。"""
        run = self._runs.get(run_id)
        if run is None or run.runtime_id != BUILTIN_RUNTIME_ID:
            return False, False
        if self._stale_execution_locked(run, "progress_span", segment_end=False):
            return False, False
        if _is_terminal(run.status) and not update.root:
            return False, False
        if not note_run_span(run, update):
            return True, False
        self._changed.add((RUN_ENTITY, run.id))
        return True, True

    def record_tool_grounding(
        self, run_id: str, action: str, answer: str, *, declined: bool = False
    ) -> None:
        """根拠・データのツールを呼ばずに作った回答の差し戻し・拒答を残す（#1835）。

        `action` は `retry`（モデルへ差し戻した）か `hop_retry`（根拠の台帳の行の略号・別名を
        引かずに答えたため差し戻した。#1958）か `data_retry`（業務データを引かずに資料に無いとして
        答えたため差し戻した。#1979）か `guide_step_retry`（業務ガイドの手順の根拠を集めずに答えた
        ため差し戻した。#2396）か `section_retry`（回答に使った章節の続きを読まずに答えたため差し
        戻した。#2492）か `hop_reverted`（差し戻しの後の回答が前より悪いため、
        前の回答を使った。残すのは使わなかった差し戻しの後の回答。#1978）か `refused`（拒答の文に
        置き換えた）か
        `text_clarification`（回答前の確認のツールを渡したのに本文で確認の質問だけを返した。
        回答はそのまま出す。#1881）。元の回答は利用者には出さず、Run の経過（イベント）にだけ
        残す。差し戻しの回数は metadata に残し、承認待ちから再開した Run でも 2 回目を差し戻さない。
        `declined` は、ツールで調べずに範囲外・答えられないと判断した回答（`retry` と `refused`。
        #2608）。イベントに `declined` を付け、拒答なら metadata にも残す（回答の対応の basis を
        `declined_without_tools` にするのに使う）。
        """
        from app.features.agent.tool_grounding import METADATA_KEY, retried

        with self._lock:
            run = self._require_run(run_id)
            record = run.metadata.get(METADATA_KEY)
            record = dict(record) if isinstance(record, dict) else {}
            if action == "retry":
                record["retries"] = retried(run.metadata) + 1
                message = (
                    (
                        "根拠・データのツールで調べずに範囲外・答えられないと判断したため、"
                        "ツールで調べてから判断するようモデルに差し戻しました。"
                    )
                    if declined
                    else (
                        "根拠・データのツールで確かめずに回答を作ったため、"
                        "ツールで確かめるようモデルに差し戻しました。"
                    )
                )
            elif action == "hop_retry":
                # 引いていない段（略号・別名）を残して回答したため差し戻した（#1958。1 回だけ）。
                record["hop_retries"] = 1
                message = (
                    "根拠の台帳・一覧の行の略号・別名を確かめずに回答を作ったため、"
                    "次の段を引くようモデルに差し戻しました。"
                )
            elif action == "data_retry":
                # 業務データを引かずに資料に無いとして回答したため差し戻した（#1979。#1958 と
                # 同じ 1 回の枠）。
                record["hop_retries"] = 1
                record["data_retry"] = True
                message = (
                    "資料に無いとした値を業務データで確かめずに回答を作ったため、"
                    "業務データを引くようモデルに差し戻しました。"
                )
            elif action == "guide_step_retry":
                # 業務ガイドの手順の根拠を集めずに回答したため差し戻した（#2396。#1958 と同じ
                # 1 回の枠）。
                record["hop_retries"] = 1
                record["guide_step_retry"] = True
                message = (
                    "業務ガイドの手順の根拠を集めずに回答を作ったため、"
                    "手順の根拠を引くようモデルに差し戻しました。"
                )
            elif action == "section_retry":
                # 回答に使った章節の続きを読まずに回答したため差し戻した（#2492。#1958 と同じ
                # 1 回の枠）。
                record["hop_retries"] = 1
                record["section_retry"] = True
                message = (
                    "回答に使った章節を終わりまで読まずに回答を作ったため、"
                    "章節の続きを読むようモデルに差し戻しました。"
                )
            elif action == "output_retry":
                # 出力の項目を記録せずに回答したため差し戻した（#2549。ほかの差し戻しとは別の
                # 1 回の枠）。
                record["output_retry"] = True
                message = (
                    "出力の項目を記録せずに回答を作ったため、"
                    "項目の値を記録するようモデルに差し戻しました。"
                )
            elif action == "hop_reverted":
                # 差し戻しの後の回答が前の回答より悪かったため、前の回答を使った（#1978）。
                record["hop_reverted"] = True
                message = (
                    "差し戻しの後の回答は最終の検証で外した内容が前の回答より多かったため、"
                    "差し戻しの前の回答を使いました。"
                )
            elif action == "refused":
                record["refused"] = True
                if declined:
                    record["declined"] = True
                message = (
                    (
                        "差し戻しの後も根拠・データのツールで調べずに範囲外・答えられないと"
                        "判断したため、お答えできない旨の文に置き換えました。"
                    )
                    if declined
                    else (
                        "根拠・データのツールで確かめられなかったため、"
                        "回答を出さずにお答えできない旨の文に置き換えました。"
                    )
                )
            else:
                record["text_clarification"] = True
                message = (
                    "資料・業務データのツールも回答前の確認のカードも使わずに、"
                    "本文で確認の質問を返しました。"
                )
            run.metadata[METADATA_KEY] = record
            self._append_event(
                run,
                RunEventType.RUNTIME_EVENT,
                message,
                {
                    "tool_grounding": {
                        "action": action,
                        "original_answer": answer[:20000],
                        **({"declined": True} if declined else {}),
                    }
                },
            )
            run.updated_at = _now()
            self._persist_locked()

    def record_context_compaction(self, run_id: str, payload: JsonObject) -> None:
        """モデルの入力の上限に収めるためにツールの結果を縮めたことをイベントに残す（#2383）。

        `payload` はモデル・予算・見積もりの前後・縮めた結果（`context_window` の記録）。縮めたのは
        モデルに渡す入力だけで、step のツールの結果は全文のまま。
        """
        outputs = payload.get("outputs")
        count = len(outputs) if isinstance(outputs, list) else 0
        with self._lock:
            run = self._require_run(run_id)
            self._append_event(
                run,
                RunEventType.RUNTIME_EVENT,
                f"モデルの入力の上限に収めるため、ツールの結果 {count} 件を縮めて"
                "モデルに渡しました。",
                {"context_compaction": deepcopy(payload)},
            )
            run.updated_at = _now()
            self._persist_locked()

    def record_undeclared_tool_call(self, run_id: str, payload: JsonObject) -> None:
        """モデルに渡していない名前のツールの呼び出しを、理由つきでイベントに残す（#3240）。

        `payload` は `tool_name`（モデルが呼んだ名前）・`call_id`・`reason`
        （`builtin_runtime.UNDECLARED_CALL_REASONS`）。呼び出しは実行していない。
        """
        with self._lock:
            run = self._require_run(run_id)
            self._append_event(
                run,
                RunEventType.RUNTIME_EVENT,
                f"渡していないツール「{payload.get('tool_name')}」の呼び出しを実行しませんでした"
                f"（理由: {payload.get('reason')}）。",
                {"severity": "warning", "undeclared_tool_call": deepcopy(payload)},
            )
            run.updated_at = _now()
            self._persist_locked()

    def record_clarification_cancel(self, run_id: str, records: list[JsonObject]) -> None:
        """利用者が取り消した確認（#1441・#1445）を Run の metadata とイベントに残す。"""
        with self._lock:
            run = self._require_run(run_id)
            run.metadata[CANCEL_METADATA_KEY] = deepcopy(records)
            names = "、".join(kind_label(str(record.get("kind"))) for record in records)
            self._append_event(
                run,
                RunEventType.RUNTIME_EVENT,
                f"利用者が確認を取り消しました（{names}）。",
                {"clarification_cancel": deepcopy(records)},
            )
            run.updated_at = _now()
            self._persist_locked()

    def _may_begin_locked(self, run: RunState, claim: RunClaim | None) -> bool:
        """queued の Run を今の claim で開始できるか（#2606）。

        claim なし（in_process）は、期限内の lease を別の worker が持たないこと。running の Run
        （同じ claim・別の入口の 2 回目の開始）・承認待ち・確認待ち・終了した Run は開始しない。
        期限の切れた・claim し直された旧所有者の claim も開始しない。
        """
        if run.status != RunStatus.QUEUED:
            reason: str | None = run_claim.REJECT_NOT_QUEUED
        else:
            reason = run_claim.begin_rejection(run.metadata, claim, now=_now())
        if reason is None:
            return True
        logger.log(
            logging.WARNING if claim is not None else logging.INFO,
            "agent_runtime_begin_rejected",
            extra={
                "run_id": run.id,
                "status": run.status.value,
                "reason": reason,
                "worker_id": claim.worker_id if claim is not None else None,
                "claim_generation": claim.generation if claim is not None else None,
                "current_generation": run_claim.generation_of(run.metadata),
            },
        )
        return False

    def _set_builtin_running_locked(
        self, run: RunState, message: str, claim: RunClaim | None = None
    ) -> None:
        # 実行を始めたら dispatcher の lease は要らない（running は claim しない）。
        # 残すと、承認の決定で queued に戻った Run を lease の期限まで claim できない。
        # lease の token・世代は実行の記録に移し、書き戻しの fencing に使う（#2606）。
        now = _now()
        execution = run_claim.start_execution(run.metadata, claim, now=now)
        run.status = RunStatus.RUNNING
        run.updated_at = now
        self._append_event(
            run,
            RunEventType.RUN_STATUS_CHANGED,
            message,
            {
                "status": RunStatus.RUNNING.value,
                "runtime_id": BUILTIN_RUNTIME_ID,
                "generation": execution["generation"],
            },
        )
        self._persist_locked()
        self._record_fact_locked(run)

    def _stale_execution_locked(self, run: RunState, write: str, *, segment_end: bool) -> bool:
        """この文脈の実行の書き戻しが、もう受け付けられないか（#2606）。

        実行の外（API の操作・テストの直接の呼び出し）は検査しない。実行の中では、Run の実行の
        記録の token が自分の token と違えば（別の実行が始まった）受け付けない。`segment_end`
        （完了・失敗・承認待ちの保存）は、running でなければ（自分の実行が区切りを終えた後の
        遅れた書き戻し）も受け付けない。終了した Run の扱いは各書き戻しの既存の判定に任せる。
        """
        token = run_claim.current_execution_token(run.id)
        if token is None:
            return False
        current = run_claim.execution_token(run.metadata)
        stale = current != token or (
            segment_end and run.status != RunStatus.RUNNING and not _is_terminal(run.status)
        )
        if stale:
            logger.warning(
                "agent_runtime_stale_execution_write_ignored",
                extra={
                    "run_id": run.id,
                    "write": write,
                    "status": run.status.value,
                    "current_generation": run_claim.generation_of(run.metadata),
                    "superseded": current != token,
                },
            )
        return stale

    def _fail_builtin_locked(self, run: RunState, code: str, detail: str) -> None:
        run.status = RunStatus.FAILED
        run.updated_at = _now()
        run.metadata.pop(_BUILTIN_STATE_KEY, None)
        self._append_event(
            run,
            RunEventType.RUNTIME_FAILED,
            detail,
            {"error_code": code, "runtime_id": BUILTIN_RUNTIME_ID},
        )
        self._persist_locked()
        self._record_fact_locked(run)

    def list_agents(self) -> list[AgentProfile]:
        with self._lock:
            return [agent.model_copy(deep=True) for agent in self._sorted_agents_locked()]

    def create_agent(self, agent: AgentProfile) -> AgentProfile:
        with self._lock:
            if agent.id in self._agents:
                raise ValueError("同じ ID の業務 Agent があります。")
            self._validate_agent_skills(agent.skill_ids)
            now = _now()
            agent.created_at = now
            agent.updated_at = now
            agent.draft_updated_at = now
            self._agents[agent.id] = _ensure_versioned(agent)
            self._mark_agent_changed(agent.id)
            self._persist_locked()
            return self._agents[agent.id].model_copy(deep=True)

    def publish_agent(
        self, agent_id: str, *, note: str = "", published_by: str | None = None
    ) -> AgentProfile:
        """下書きを新しい版として公開する（#770）。"""
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                raise KeyError(agent_id)
            self._validate_agent_skills(agent.skill_ids)
            version = max((item.version for item in agent.versions), default=0) + 1
            agent.versions.append(
                AgentVersion(
                    version=version,
                    note=note.strip(),
                    published_by=published_by,
                    # 使うスキルの今の中身を版に固定する（#1855）。
                    skills=pin_skills(agent.skill_ids),
                    **{field: getattr(agent, field) for field in AGENT_VERSIONED_FIELDS},
                )
            )
            agent.published_version = version
            agent.versioned = True
            _prune_agent_versions(agent)
            agent.updated_at = _now()
            self._mark_agent_changed(agent.id)
            self._persist_locked()
            return agent.model_copy(deep=True)

    def publish_skill_updates(
        self, agent_id: str, *, note: str = "", published_by: str | None = None
    ) -> tuple[AgentProfile, int]:
        """公開中の版にスキルの更新だけを取り込んだ新しい版を公開する（#1867）。

        新しい版は公開中の版の項目（指示・スキルの選択など）のまま、スキルの今の中身を固定し直す。
        下書きの変更は公開しない（まとめての再公開で、確かめていない下書きを出さない）。
        戻り値は（業務 Agent、前の公開中の版の番号）。取り込む更新が無ければ ValueError。
        """
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                raise KeyError(agent_id)
            published = agent.published()
            if published is None:
                raise ValueError("公開した版がありません。")
            if not agent.skill_updates:
                raise ValueError("取り込むスキルの更新がありません。")
            self._validate_agent_skills(published.skill_ids)
            previous = published.version
            version = max((item.version for item in agent.versions), default=0) + 1
            agent.versions.append(
                published.model_copy(
                    deep=True,
                    update={
                        "version": version,
                        "note": note.strip() or f"スキルの更新を取り込む（v{previous} から）",
                        "published_at": _now(),
                        "published_by": published_by,
                        "skills": pin_skills(published.skill_ids),
                    },
                )
            )
            agent.published_version = version
            # 前の公開中の版も残す（Toast の「取り消す」で戻す。#1867）。
            _prune_agent_versions(agent, keep=(previous,))
            agent.updated_at = _now()
            self._mark_agent_changed(agent.id)
            self._persist_locked()
            return agent.model_copy(deep=True), previous

    def set_published_version(self, agent_id: str, version: int) -> AgentProfile:
        """公開中の版だけを前の版にする（まとめての再公開を戻す。下書きは変えない。#1867）。"""
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                raise KeyError(agent_id)
            if not any(item.version == version for item in agent.versions):
                raise KeyError(f"{agent_id}@{version}")
            agent.published_version = version
            agent.updated_at = _now()
            self._mark_agent_changed(agent.id)
            self._persist_locked()
            return agent.model_copy(deep=True)

    def restore_agent_version(
        self, agent_id: str, version: int, *, published_by: str | None = None
    ) -> AgentProfile:
        """前の版を公開し直し、下書きもその内容にする（ロールバック。#770）。"""
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                raise KeyError(agent_id)
            target = next((item for item in agent.versions if item.version == version), None)
            if target is None:
                raise KeyError(f"{agent_id}@{version}")
            for field in AGENT_VERSIONED_FIELDS:
                setattr(agent, field, deepcopy(getattr(target, field)))
            agent.published_version = version
            agent.updated_at = _now()
            agent.draft_updated_at = agent.updated_at
            self._mark_agent_changed(agent.id)
            self._persist_locked()
            return agent.model_copy(deep=True)

    def owned_agent_ids(self, user_uuid: str | None) -> frozenset[str]:
        """利用者が持ち主の個人の業務 Agent（#2523。対象範囲に足す）。"""
        if not user_uuid:
            return frozenset()
        with self._lock:
            return frozenset(
                agent.id for agent in self._agents.values() if agent.owner_user_uuid == user_uuid
            )

    def update_agent_sharing(
        self,
        agent_id: str,
        *,
        owner_user_uuid: str | None,
        share_request: AgentShareRequest | None,
    ) -> AgentProfile:
        """持ち主と共有の申請を変える（申請・承認・却下。#2523。下書きの時刻は進めない）。"""
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                raise KeyError(agent_id)
            agent.owner_user_uuid = owner_user_uuid
            agent.share_request = share_request
            agent.updated_at = _now()
            self._mark_agent_changed(agent.id)
            self._persist_locked()
            return agent.model_copy(deep=True)

    def patch_agent(self, agent_id: str, patch: AgentProfilePatch) -> AgentProfile:
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                raise KeyError(agent_id)
            # 送られた項目を検証済みの型のまま入れる（`model_dump` の dict にしない）。
            # AgentProfile は代入を検証しないので、dict にするとデータの範囲・出力の項目が
            # モデルでなくなり、属性で読む処理（次の保存の範囲の確認・record_output）が
            # 失敗する（#2559・#2694）。
            data: dict[str, Any] = {
                key: deepcopy(getattr(patch, key)) for key in patch.model_fields_set
            }
            skill_ids = data.get("skill_ids")
            if isinstance(skill_ids, list):
                self._validate_agent_skills(skill_ids)
            before = {field: getattr(agent, field) for field in AGENT_VERSIONED_FIELDS}
            for key, value in data.items():
                setattr(agent, key, value)
            agent.updated_at = _now()
            # 有効 / 無効の切り替えなど、版の項目を変えない保存では下書きの時刻を進めない（#2519）。
            if any(getattr(agent, field) != before[field] for field in AGENT_VERSIONED_FIELDS):
                agent.draft_updated_at = agent.updated_at
            self._mark_agent_changed(agent.id)
            self._persist_locked()
            return agent.model_copy(deep=True)

    def delete_agent(self, agent_id: str) -> None:
        with self._lock:
            if agent_id in BUILTIN_AGENT_IDS:
                raise ValueError("組み込みの業務 Agent は削除できません。")
            if agent_id not in self._agents:
                raise KeyError(agent_id)
            del self._agents[agent_id]
            self._mark_agent_changed(agent_id)
            self._persist_locked()

    def _record_fact_locked(self, run: RunState) -> None:
        """Run の事実（集計用。#794）をバックグラウンドの保存のキューに入れる。

        Oracle の構成だけ保存する（memory / file では何もしない）。失敗しても Run は続ける。
        """
        try:
            from app.features.agent import run_facts_store

            agent = self._agents.get(run.agent_id)
            run_facts_store.record_run(run, agent_name=agent.name if agent is not None else "")
        except Exception:  # noqa: BLE001 - 集計用の事実で Run を止めない
            logger.warning("agent_run_fact_not_queued", extra={"run_id": run.id})

    def _sorted_runs_locked(self) -> list[RunState]:
        return sorted(self._runs.values(), key=lambda run: run.created_at, reverse=True)

    def _sorted_agents_locked(self) -> list[AgentProfile]:
        return sorted(self._agents.values(), key=lambda agent: agent.created_at)

    def _export_snapshot_locked(self) -> AgentRuntimeSnapshot:
        return AgentRuntimeSnapshot(
            runs=[run.model_copy(deep=True) for run in self._sorted_runs_locked()],
            agents=[agent.model_copy(deep=True) for agent in self._sorted_agents_locked()],
            quarantined=[record.model_copy(deep=True) for record in self._quarantined],
        )

    def _replace_state_locked(self, snapshot: AgentRuntimeSnapshot) -> None:
        self._runs = {run.id: run.model_copy(deep=True) for run in snapshot.runs}
        self._agents = {
            agent.id: _migrate_builtin_agent(_ensure_versioned(agent)) for agent in snapshot.agents
        }
        # 保存済みの snapshot に無い組み込みの業務 Agent は足す（#1573 で 3 つにした）。
        for builtin in _builtin_agents():
            if builtin.id not in self._agents:
                self._agents[builtin.id] = _ensure_versioned(builtin)
        self._approvals = {
            approval.id: approval for run in self._runs.values() for approval in run.approvals
        }
        self._quarantined = [record.model_copy(deep=True) for record in snapshot.quarantined]
        # 状態を丸ごと置き換えた（前の印は意味を持たない。呼ぶ側が要る印を付ける）。
        self._clear_change_marks_locked()
        self._reset_change_tracking_locked()

    def _mark_load_repairs_locked(self, report: SnapshotLoadReport) -> None:
        """読み込みで直した Run と退避した Run・業務 Agent に印を付ける（#853・#1939）。

        退避したものは保存先の行を消し（削除の印）、元の JSON を退避の記録（header）に移す。
        """
        for repaired in report.repaired:
            self._mark_run_changed(repaired.run_id)
        for record in report.quarantined:
            if record.id and record.kind in {RUN_ENTITY, AGENT_ENTITY}:
                self._changed.add((record.kind, record.id))
        if report.quarantined:
            self._quarantine_changed = True

    def _apply_loaded_snapshot(self, snapshot_json: str | bytes, *, source: str) -> None:
        """保存先の snapshot を Run 単位で読み込む（#853）。

        1 件の Run の不整合で起動を止めない。直せるものは直し、直せないものは退避して、
        変わったら読み込みの直後に保存する（保存の失敗では起動を止めない。次の変更で保存する）。
        snapshot 全体が読めないときだけ `AgentRuntimeSnapshotCorruptError`。
        """
        snapshot, report = load_snapshot_tolerant(snapshot_json, source=source)
        # 直した結果の保存は lock を放した後に書き、失敗しても起動を止めない（予定は残り、次の
        # 書き込みで書き直す）。
        self._lock.acquire()
        try:
            self._replace_state_locked(snapshot)
            self._load_report = report
            if not report.changed:
                return
            self._mark_load_repairs_locked(report)
            self._persist_locked()
        finally:
            self._lock.release()
        try:
            self.flush_writes()
        except Exception:  # noqa: BLE001 - 直した結果の保存の失敗で起動を止めない
            logger.warning(
                "agent_runtime_snapshot_repair_not_saved",
                extra={"source": source},
                exc_info=True,
            )

    def _load_snapshot_from_disk(self) -> None:
        if self._snapshot_path is None:
            return
        try:
            self._apply_loaded_snapshot(
                self._snapshot_path.read_text(encoding="utf-8"), source="file"
            )
        except AgentRuntimeSnapshotCorruptError as exc:
            raise RuntimeError(
                f"Agent の保存先の snapshot（{self._snapshot_path}）を読み込めません（{exc}）。"
                "データを守るため起動を止めました。ファイルを退避してから削除するか、"
                "内容を直して、backend を再起動してください。"
            ) from exc

    def _record_tool_success_artifacts(
        self,
        run: RunState,
        step: RunStep,
        result: ToolResult,
    ) -> None:
        if step.tool_call is None or result.output is None:
            return
        # MCP 接続のツール（`<接続>__<ツール>`。#757）は、ツールの部分で成果物の種類を決める。
        artifact_kind_by_tool = {
            "rag_search": "rag_evidence",
            "nl2sql_query": "structured_table",
            "nl2sql_get_job": "structured_table",
        }
        kind = artifact_kind_by_tool.get(mcp_base_tool_name(step.tool_call.name))
        if kind is None:
            return
        artifact = Artifact(
            name=f"{step.tool_call.name}:{step.id}",
            kind=kind,
            content=result.output,
        )
        artifact = _maybe_externalize_artifact_content(run.id, artifact)
        run.artifacts.append(artifact)
        if artifact.content_ref is not None:
            result.output = artifact.content
        self._append_event(
            run,
            RunEventType.ARTIFACT_CREATED,
            "ツール結果 artifact を保存しました。",
            {"artifact_id": artifact.id, "kind": artifact.kind, "step_id": step.id},
        )

    def _tool_invocation_context_locked(
        self,
        run: RunState,
        call: ToolCall,
        *,
        approval_id: str | None = None,
    ) -> ToolInvocationContext:
        # run_facts は runtime を import するため、ここで読む（循環の import を避ける）。
        from app.features.agent.run_facts import run_source

        # 承認後の再実行もこの context を使うため、承認者ではなく Run の利用者として呼ぶ（#233）。
        return ToolInvocationContext(
            approval_id=approval_id,
            trace_id=call.trace_id,
            agent_id=run.agent_id,
            run_id=run.id,
            user_uuid=run.created_by_user_uuid,
            # 呼び先の実行履歴で品質評価の Run の実行を区別する（#1986）。
            run_source=run_source(run.metadata),
        )

    def _append_guardrail_events(
        self,
        run: RunState,
        step: RunStep,
        result: ToolResult,
    ) -> None:
        if not result.guardrail_warnings:
            return
        self._append_event(
            run,
            RunEventType.TOOL_GUARDRAIL_WARNING,
            "ツール出力の安全検査で警告を検出しました。",
            {
                "step_id": step.id,
                "tool_name": step.tool_call.name if step.tool_call else step.kind,
                "warnings": result.guardrail_warnings,
            },
        )

    def _append_event(
        self,
        run: RunState,
        event_type: RunEventType,
        message: str,
        payload: JsonObject | None = None,
    ) -> None:
        event = RunEvent(
            run_id=run.id,
            type=event_type,
            message=message,
            payload=payload or {},
        )
        run.events.append(event)
        self._mark_run_changed(run)
        record_runtime_event(event.type.value, {"run_id": run.id, **event.payload})
        self._condition.notify_all()

    def _require_run(self, run_id: str) -> RunState:
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    @staticmethod
    def _validate_agent_skills(skill_ids: list[str]) -> None:
        from app.features.agent.skills import skill_registry

        unknown = sorted(
            {skill_id for skill_id in skill_ids if skill_registry.get(skill_id) is None}
        )
        if unknown:
            # 画面の保存の失敗の帯にそのまま出る（#925）。
            raise ValueError(
                f"登録されていないスキルがあります: {', '.join(unknown)}。"
                "スキルの選択から外してください。"
            )

    @staticmethod
    def _require_step(run: RunState, step_id: str) -> RunStep:
        for step in run.steps:
            if step.id == step_id:
                return step
        raise KeyError(step_id)


class AgentRuntimeCheckpointCorruptError(AgentRuntimeSnapshotCorruptError):
    """Oracle の checkpoint 全体が読めない（直し方を含むメッセージ。#853）。"""


class AgentRuntimeOracleCheckpointRepository(AgentRuntimeRepository):
    """Oracle に Run・業務 Agent を 1 件 = 1 行で保存する repository（#1939）。

    メモリの状態機（`AgentRuntimeRepository`）を使い、変えた Run・業務 Agent の行だけを書く
    （`runtime_checkpoint_store`。以前は全体の snapshot を 1 行で書き戻した）。書くときは保存先の
    head の行を lock し、前回の同期の後に別のプロセスが書いた行を読み、このプロセスが変えていない
    ものは保存先の値にそろえる（両方が同じ 1 件を変えていたときは、このプロセスの値で書く。#1916）。
    """

    def __init__(
        self,
        *,
        checkpoint_key: str = "default",
        connect_factory: OracleConnectFactory | None = None,
    ) -> None:
        # 接続は共通の PLATFORM_ORACLE_*、テーブルはシステムテーブルが作る（#764）。
        self._oracle_table_name = RUNTIME_CHECKPOINT_TABLE
        self._oracle_checkpoint_key = checkpoint_key
        self._oracle_connect_factory = connect_factory or connect_platform_oracle
        # 最後に保存先と同期した head の版（None は未同期。次の書き込みで全体を読む）。
        self._synced_version: int | None = None
        super().__init__(snapshot_path=None)
        self._load_snapshot_from_oracle()

    def _connect_oracle(self) -> Any:
        return self._oracle_connect_factory()

    def _stores_changes(self) -> bool:
        return True

    def _migration_required(self, detail: str) -> AgentRuntimeMigrationRequiredError:
        return AgentRuntimeMigrationRequiredError(
            "Agent の保存先（Run・業務 Agent）が、Run ごとの行に移す前の形です。運用設定 > "
            "システムテーブルで更新（migration 20261015_016）を適用してください。適用すると、"
            f"再起動せずに読み込みます（{detail}）。"
        )

    def _read_storage_cursor(
        self, cursor: Any
    ) -> tuple[checkpoint_store.CheckpointHead | None, list[checkpoint_store.EntityRow]]:
        """head と今ある Run・業務 Agent の行（移す前の形なら migration が要るという例外）。"""
        try:
            head = checkpoint_store.read_head(
                cursor, self._oracle_table_name, self._oracle_checkpoint_key
            )
        except checkpoint_store.CheckpointMigrationRequired as exc:
            raise self._migration_required(str(exc)) from exc
        except ValueError as exc:
            raise AgentRuntimeCheckpointCorruptError(self._corrupt_message(str(exc))) from exc
        try:
            rows = checkpoint_store.read_entities(cursor, self._oracle_checkpoint_key)
        except Exception as exc:
            if checkpoint_store.is_missing_table_error(exc):
                raise self._migration_required(
                    f"{checkpoint_store.ENTITY_TABLE} がありません"
                ) from exc
            raise
        return head, rows

    def _corrupt_message(self, detail: str) -> str:
        return (
            f"Agent の保存先の checkpoint（{self._oracle_table_name} の "
            f"CHECKPOINT_KEY='{self._oracle_checkpoint_key}'）を読み込めません（{detail}）。"
            "データを守るため上書きせずに止めました。行の SNAPSHOT_JSON を退避してから"
            "行を削除するか、内容を直して、"
            "backend を再起動してください（AGENT_RUNTIME_REPOSITORY_BACKEND=auto なら、"
            "保存しない状態で起動して画面で案内します）。"
        )

    def _read_storage(
        self,
    ) -> tuple[checkpoint_store.CheckpointHead | None, list[checkpoint_store.EntityRow]] | None:
        """保存先の head と行（システムテーブルを作る前なら None）。"""
        try:
            with self._connect_oracle() as connection, connection.cursor() as cursor:
                return self._read_storage_cursor(cursor)
        except AgentRuntimeStorageUnavailableError:
            raise
        except AgentRuntimeSnapshotCorruptError:
            raise
        except Exception as exc:
            if not _is_oracle_table_missing_error(exc):
                raise
            # システムテーブルを作る前でも起動できるようにする（画面は作成へ案内する）。
            logger.warning(
                "agent_runtime_tables_missing",
                extra={"table": self._oracle_table_name},
            )
            return None

    def _load_snapshot_from_oracle(self) -> None:
        stored = self._read_storage()
        head, rows = stored if stored is not None else (None, [])
        snapshot_json, unreadable = _entities_snapshot_json(head, rows)
        self._lock.acquire()
        try:
            self._synced_version = head.version if head is not None else 0
        finally:
            self._lock.release()
        self._apply_loaded_snapshot(snapshot_json, source="oracle")
        if unreadable:
            self._quarantine_unreadable_rows(unreadable, source="oracle")

    def _quarantine_unreadable_rows(
        self, records: list[QuarantinedSnapshotRecord], *, source: str
    ) -> None:
        """JSON として読めない行を退避する（行は消し、元の文字列を退避の記録に移す。#853）。"""
        for record in records:
            logger.warning(
                "agent_runtime_snapshot_record_quarantined",
                extra={
                    "source": source,
                    "kind": record.kind,
                    "record_id": record.id,
                    "reasons": record.reasons,
                },
            )
        self._lock.acquire()
        try:
            for record in records:
                if record.id is None:
                    continue
                if record.kind == RUN_ENTITY:
                    self._runs.pop(record.id, None)
                elif record.kind == AGENT_ENTITY:
                    self._agents.pop(record.id, None)
                self._changed.add((record.kind, record.id))
            self._quarantined.extend(records)
            self._load_report.quarantined.extend(records)
            self._quarantine_changed = True
            self._restore_builtin_agents_locked()
            self._approvals = {
                approval.id: approval for run in self._runs.values() for approval in run.approvals
            }
            self._persist_locked()
        finally:
            self._lock.release()
        try:
            self.flush_writes()
        except Exception:  # noqa: BLE001 - 退避の保存の失敗で起動を止めない（次の書き込みで書く）
            logger.warning(
                "agent_runtime_snapshot_repair_not_saved", extra={"source": source}, exc_info=True
            )

    def _restore_builtin_agents_locked(self) -> None:
        for builtin in _builtin_agents():
            if builtin.id not in self._agents:
                self._agents[builtin.id] = _ensure_versioned(builtin)

    @contextmanager
    def storage_rewrite(self) -> Iterator[Callable[[], None]]:
        """保存先を外から書き換える間（データを書き換える migration。#1844）、変更と保存を止める。

        先にこのプロセスの保存の予定を書き切り、書き込み（`_write_lock`）と変更（`_lock`）を止める
        （メモリの古い値で migration の結果を上書きしない）。呼び出し側は書き換えの後に、渡された
        関数で保存先から読み直してから抜ける。待っていた変更は、読み直した状態の上で行う。
        """
        with self._write_lock:
            self.flush_writes()
            with self._condition:
                yield self._reload_from_oracle_locked
                self._condition.notify_all()

    def _reload_from_oracle_locked(self) -> None:
        """保存先を読み直して状態を置き換える（行・テーブルが無ければ組み込みだけにする）。

        全再作成の後は行が無いので、組み込みの業務 Agent だけに戻る（保存先と同じにする）。
        読めない保存先は例外（状態は変えない）。
        """
        stored = self._read_storage()
        head, rows = stored if stored is not None else (None, [])
        snapshot_json, unreadable = _entities_snapshot_json(head, rows)
        try:
            snapshot, report = load_snapshot_tolerant(snapshot_json, source="oracle_reload")
        except AgentRuntimeSnapshotCorruptError as exc:
            raise AgentRuntimeCheckpointCorruptError(self._corrupt_message(str(exc))) from exc
        self._pending.clear()
        self._written_ticket = self._staged_ticket
        self._replace_state_locked(snapshot)
        self._load_report = report
        self._synced_version = head.version if head is not None else 0
        if unreadable:
            for record in unreadable:
                if record.id is not None:
                    self._changed.add((record.kind, record.id))
            self._quarantined.extend(unreadable)
            report.quarantined.extend(unreadable)
            self._quarantine_changed = True
        if report.changed:
            self._mark_load_repairs_locked(report)
            self._persist_locked()

    def claim_control_plane_run(self, worker_id: str, *, lease_seconds: int) -> RunState | None:
        """head の行を lock して別のプロセスの変更をそろえ、Run の lease を原子的に取る。"""

        def claim() -> RunState | None:
            run = self._claim_control_plane_run_locked(worker_id, lease_seconds=lease_seconds)
            self._persist_locked()
            return run.model_copy(deep=True) if run is not None else None

        return self._storage_transition(claim)

    def begin_builtin_run(
        self, run_id: str, *, claim: RunClaim | None = None
    ) -> tuple[RunState, AgentProfile] | None:
        """dispatcher の開始は、保存先の今の claim と状態に対する条件付きの遷移にする（#2606）。

        別のプロセスの dispatcher が期限の切れた lease を claim し直していれば、その変更を読んでから
        判定するので、旧所有者は開始できない。in_process の開始（claim なし）は 1 プロセスなので
        メモリで判定する（event loop の上で保存先を待たない）。
        """
        if claim is None:
            return super().begin_builtin_run(run_id)
        return self._storage_transition(lambda: self._begin_builtin_run_locked(run_id, claim))

    def begin_builtin_resume(
        self, run_id: str, *, claim: RunClaim | None = None
    ) -> tuple[RunState, AgentProfile, str, dict[str, bool]] | None:
        """承認の後の再開も、dispatcher のときは保存先に対する条件付きの遷移にする（#2606）。"""
        if claim is None:
            return super().begin_builtin_resume(run_id)
        return self._storage_transition(lambda: self._begin_builtin_resume_locked(run_id, claim))

    def _storage_transition(self, mutate: Callable[[], _T]) -> _T:
        """head の行を lock し、別のプロセスの変更をそろえてから `mutate`（判定と変更）を行い、
        積んだ保存の予定を同じ transaction で書く（claim・dispatcher の開始。#1939・#2606）。

        head の lock の中で判定と書き込みをするので、別のプロセスの claim・開始と重ならない。
        書けなければ、この遷移で変えた分は次の書き込みで保存先の値に戻す（メモリだけを進めた
        ままにしない。保存先に書いていない開始で、実行の無い running を残さない）。
        """
        table, key = self._oracle_table_name, self._oracle_checkpoint_key
        with self._write_lock:
            self.flush_writes()
            earlier: _StagedChanges | None = None
            batch: _StagedChanges | None = None
            try:
                with self._connect_oracle() as connection, connection.cursor() as cursor:
                    head = self._lock_head_cursor(cursor)
                    others, full = self._changed_rows_cursor(cursor, head)
                    self._lock.acquire()
                    try:
                        self._reset_change_tracking_locked()
                        self._apply_storage_changes_locked(
                            others, full=full, written=set(), head=head
                        )
                        self._sync_quarantine_locked(head.header.get("quarantined"))
                        # flush の後に別のスレッドが積んだ予定（この遷移の変更とは分けて持つ）。
                        earlier = self._take_pending_locked()
                        result = mutate()
                        ours = self._take_pending_locked()
                        batch = earlier
                        if ours is not None:
                            batch = ours if batch is None else _merged_batch(batch, ours)
                    finally:
                        self._lock.release()
                    version = head.version
                    if batch is not None:
                        version += 1
                        header: str | None = None
                        if batch.quarantined is not None:
                            header = checkpoint_store.header_json(
                                quarantined=list(
                                    _merge_quarantine(
                                        batch.quarantined, head.header.get("quarantined")
                                    )
                                )
                            )
                        checkpoint_store.write_entities(
                            cursor, key, batch.entities, version=version
                        )
                        checkpoint_store.update_head(
                            cursor, table, key, version=version, text=header
                        )
                        self._write_projection_cursor(cursor, batch)
                    connection.commit()
            except Exception:
                self._lock.acquire()
                try:
                    # 別のスレッドの予定は書き直す。この遷移の変更は書き直さず、次の書き込みで
                    # 保存先の全体を読んで保存先の値に戻す。
                    if earlier is not None:
                        self._pending.insert(0, earlier)
                    if batch is not None:
                        self._synced_version = None
                finally:
                    self._lock.release()
                raise
            self._synced_version = version
            if batch is not None:
                self._written_ticket = max(self._written_ticket, batch.ticket)
            return result

    def _take_pending_locked(self) -> _StagedChanges | None:
        """積んだ保存の予定を 1 つに重ねて取り出す（lock の中）。"""
        if not self._pending:
            return None
        batch = self._pending.pop(0)
        while self._pending:
            batch.merge(self._pending.pop(0))
        return batch

    def _lock_head_cursor(self, cursor: Any) -> checkpoint_store.CheckpointHead:
        """head の行を lock して読む（無ければ版 0 で作る）。"""
        table, key = self._oracle_table_name, self._oracle_checkpoint_key
        for _attempt in range(2):
            try:
                head = checkpoint_store.read_head(cursor, table, key, for_update=True)
            except checkpoint_store.CheckpointMigrationRequired as exc:
                raise self._migration_required(str(exc)) from exc
            except ValueError as exc:
                raise AgentRuntimeCheckpointCorruptError(self._corrupt_message(str(exc))) from exc
            if head is not None:
                return head
            text = checkpoint_store.header_json(quarantined=[])
            if checkpoint_store.insert_head(cursor, table, key, text):
                return checkpoint_store.CheckpointHead(
                    header=json.loads(text), version=0, writer_id=checkpoint_store.WRITER_ID
                )
        raise RuntimeError(
            f"Agent の保存先の head（{table} の CHECKPOINT_KEY='{key}'）を作れません。"
        )

    def _changed_rows_cursor(
        self, cursor: Any, head: checkpoint_store.CheckpointHead
    ) -> tuple[list[checkpoint_store.EntityRow], bool]:
        """前回の同期の後に別のプロセスが書いた行（と、全体を読み直したか）。"""
        synced = self._synced_version
        if synced is not None and head.version == synced:
            return [], False
        if synced is None:
            # 未同期: 今ある行を全部読む。
            return checkpoint_store.read_entities(cursor, self._oracle_checkpoint_key), True
        return (
            checkpoint_store.read_entities(
                cursor, self._oracle_checkpoint_key, after_version=synced
            ),
            False,
        )

    def _refresh_run_from_storage(self, run_id: str) -> str | None:
        """別のプロセスが取り消した Run を、保存先の 1 行を読んでメモリに反映する（#2609）。

        API のプロセスの取消を、Run を実行している runtime-dispatcher のプロセスが次のツール・
        モデルの呼び出しの前に知る。読めないときは今のメモリで判定する（書き込みのときにも
        取消を勝たせる。`_carry_stored_cancels_locked`）。保存先の実行の token を返す（別の
        プロセスで次の実行が始まったかの判定。#2606）。
        """
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or _is_terminal(run.status):
                return None
        try:
            with self._connect_oracle() as connection, connection.cursor() as cursor:
                row = checkpoint_store.read_entity(
                    cursor, self._oracle_checkpoint_key, RUN_ENTITY, run_id
                )
        except Exception:  # noqa: BLE001 - 読めなければ今のメモリで判定する
            logger.warning(
                "agent_runtime_run_refresh_failed", extra={"run_id": run_id}, exc_info=True
            )
            return None
        stored_token = _stored_execution_token(row)
        stored = _stored_cancelled_run(row)
        if stored is None:
            return stored_token
        with self._lock:
            run = self._runs.get(run_id)
            if run is not None and self._adopt_stored_cancel_locked(run, stored):
                self._persist_locked()
                self._record_fact_locked(run)
        return stored_token

    def _carry_stored_cancels_locked(
        self, batch: _StagedChanges, rows: list[checkpoint_store.EntityRow]
    ) -> None:
        """書く Run を別のプロセスが先に取り消していたら、取消を残して書く（取消が勝つ。#2609）。"""
        for row in rows:
            key = row.key
            if key not in batch.entities or row.kind != RUN_ENTITY:
                continue
            stored = _stored_cancelled_run(row)
            run = self._runs.get(row.entity_id)
            if stored is None or run is None:
                continue
            if self._adopt_stored_cancel_locked(run, stored):
                batch.entities[key] = run.model_dump_json()
                # 書く内容に入れたので、次の保存の予定には積まない。
                self._changed.discard(key)

    def _write_staged(self, batch: _StagedChanges) -> None:
        """保存の予定を書く（head を lock し、別のプロセスの変更を読んでから。#1916・#1939）。"""
        table, key = self._oracle_table_name, self._oracle_checkpoint_key
        with self._connect_oracle() as connection, connection.cursor() as cursor:
            head = self._lock_head_cursor(cursor)
            others, full = self._changed_rows_cursor(cursor, head)
            if others:
                self._lock.acquire()
                try:
                    self._carry_stored_cancels_locked(batch, others)
                finally:
                    self._lock.release()
            version = head.version + 1
            header: str | None = None
            # 書いた後の保存先の退避の記録。
            stored_quarantine: object = head.header.get("quarantined")
            checkpoint_store.write_entities(cursor, key, batch.entities, version=version)
            if batch.quarantined is not None:
                stored_quarantine = _merge_quarantine(batch.quarantined, stored_quarantine)
                header = checkpoint_store.header_json(quarantined=list(stored_quarantine))
            checkpoint_store.update_head(cursor, table, key, version=version, text=header)
            self._write_projection_cursor(cursor, batch)
            connection.commit()
        self._lock.acquire()
        try:
            self._apply_storage_changes_locked(
                others, full=full, written=set(batch.entities), head=head
            )
            self._sync_quarantine_locked(stored_quarantine)
            self._synced_version = version
        finally:
            self._lock.release()

    def _write_projection_cursor(self, cursor: Any, batch: _StagedChanges) -> None:
        """監査用の projection（normalized だけ）。"""
        return None

    def _sync_quarantine_locked(self, stored: object) -> None:
        """退避の記録を保存先にそろえる（このプロセスがまだ書いていない変更があれば残す）。"""
        if self._quarantine_changed or any(
            staged.quarantined is not None for staged in self._pending
        ):
            return
        self._quarantined = _previous_quarantine(stored)

    def _apply_storage_changes_locked(
        self,
        rows: list[checkpoint_store.EntityRow],
        *,
        full: bool,
        written: set[EntityKey],
        head: checkpoint_store.CheckpointHead,
    ) -> None:
        """別のプロセスが書いた Run・業務 Agent をメモリにそろえる（lock の中。#1916）。

        このプロセスが変えたもの（今書いたもの・まだ書いていない予定・印のあるもの）は、
        このプロセスの値を残す（両方が変えていれば、ログに残す）。`full` は保存先の全体を
        読んだ（保存先に無いものは、このプロセスが変えていなければメモリから消す）。
        """
        if not rows and not full:
            return
        mine = set(written) | set(self._changed)
        for staged in self._pending:
            mine.update(staged.entities)
        overlapping: list[EntityKey] = []
        taken: dict[str, list[Any]] = {RUN_ENTITY: [], AGENT_ENTITY: []}
        unreadable: list[QuarantinedSnapshotRecord] = []
        dropped: list[EntityKey] = []
        stored_keys = {row.key for row in rows}
        for row in rows:
            if row.key in mine:
                # 全体を読んだときは、別のプロセスが変えたかは分からない（重なりにしない）。
                if not full:
                    overlapping.append(row.key)
                continue
            if row.entity_json is None:
                dropped.append(row.key)
                continue
            try:
                taken[row.kind].append(json.loads(row.entity_json))
            except ValueError:
                unreadable.append(_unreadable_record(row))
        if full:
            current = {(RUN_ENTITY, run_id) for run_id in self._runs} | {
                (AGENT_ENTITY, agent_id) for agent_id in self._agents
            }
            dropped.extend(sorted(current - stored_keys - mine))
        for kind, entity_id in dropped:
            if kind == RUN_ENTITY:
                self._runs.pop(entity_id, None)
            else:
                self._agents.pop(entity_id, None)
        loaded, report = load_snapshot_tolerant(
            json.dumps(
                {
                    "version": checkpoint_store.SNAPSHOT_VERSION,
                    "runs": taken[RUN_ENTITY],
                    "agents": taken[AGENT_ENTITY],
                }
            ),
            source="oracle_merge",
        )
        for record in [*report.quarantined, *unreadable]:
            # 保存先の値が読めない 1 件は、メモリの古い値を残さず退避する（元の JSON は退避の
            # 記録に移し、次の保存で行を消す）。
            if record.id and record.kind == RUN_ENTITY:
                self._runs.pop(record.id, None)
            elif record.id and record.kind == AGENT_ENTITY:
                self._agents.pop(record.id, None)
            if record.id and record.kind in {RUN_ENTITY, AGENT_ENTITY}:
                self._changed.add((record.kind, record.id))
            self._quarantined.append(record)
            self._quarantine_changed = True
        for run in loaded.runs:
            self._runs[run.id] = run
        for agent in loaded.agents:
            self._agents[agent.id] = _migrate_builtin_agent(_ensure_versioned(agent))
        self._restore_builtin_agents_locked()
        self._approvals = {
            approval.id: approval for run in self._runs.values() for approval in run.approvals
        }
        self._reset_change_tracking_locked()
        self._condition.notify_all()
        # 本番の 1 プロセス（in_process）では起きない。dispatcher を別プロセスにした構成では、
        # dispatcher と API のプロセスが交互に書くため、ふつうに起きる。
        in_process = get_settings().agent_runtime_dispatch_mode.strip().lower() == "in_process"
        table, key = self._oracle_table_name, self._oracle_checkpoint_key
        logger.log(
            logging.WARNING if in_process else logging.INFO,
            "agent_runtime_checkpoint_concurrent_write",
            extra={
                "table": table,
                "checkpoint_key": key,
                "writer_id": checkpoint_store.WRITER_ID,
                "other_writer_id": head.writer_id,
                "synced_version": self._synced_version,
                "current_version": head.version,
                "full_reload": full,
                "runs_from_storage": len(loaded.runs),
                "agents_from_storage": len(loaded.agents),
                "removed": len(dropped),
                "overlapping": [f"{kind}:{entity_id}" for kind, entity_id in overlapping[:20]],
                "reason": (
                    f"別のプロセス（{head.writer_id or '不明'}）が同じ保存先（{table} の "
                    f"CHECKPOINT_KEY='{key}'）に先に書きました。上書きせず、Run・業務 Agent の "
                    "単位でそろえてから保存しました。同じ .env（同じデータベース）で 2 つ目の "
                    "Agent の backend を起動していないか確かめてください。"
                ),
            },
        )


def _stored_execution_token(row: checkpoint_store.EntityRow | None) -> str | None:
    """保存先の Run の行の実行の token（#2606。無い・読めなければ None）。"""
    if row is None or row.entity_json is None:
        return None
    try:
        value = json.loads(row.entity_json)
    except ValueError:
        return None
    metadata = value.get("metadata") if isinstance(value, dict) else None
    return run_claim.execution_token(metadata) if isinstance(metadata, dict) else None


def _stored_cancelled_run(row: checkpoint_store.EntityRow | None) -> RunState | None:
    """保存先の Run の行が取り消し済みなら、その Run（それ以外・読めなければ None。#2609）。"""
    if row is None or row.entity_json is None:
        return None
    try:
        value = json.loads(row.entity_json)
    except ValueError:
        return None
    if not isinstance(value, dict) or value.get("status") != RunStatus.CANCELLED.value:
        return None
    try:
        return RunState.model_validate(value)
    except ValidationError:
        return None


def _entities_snapshot_json(
    head: checkpoint_store.CheckpointHead | None, rows: list[checkpoint_store.EntityRow]
) -> tuple[str, list[QuarantinedSnapshotRecord]]:
    """保存先の行を、読み込み（`load_snapshot_tolerant`）の snapshot の JSON にする。

    JSON として読めない行は、退避の記録にして返す（snapshot には入れない）。
    """
    runs: list[Any] = []
    agents: list[Any] = []
    unreadable: list[QuarantinedSnapshotRecord] = []
    for row in rows:
        if row.entity_json is None:
            continue
        try:
            item = json.loads(row.entity_json)
        except ValueError:
            unreadable.append(_unreadable_record(row))
            continue
        (runs if row.kind == RUN_ENTITY else agents).append(item)
    header = head.header if head is not None else {}
    snapshot = {
        "version": checkpoint_store.SNAPSHOT_VERSION,
        "runs": runs,
        "agents": agents,
        "quarantined": header.get("quarantined") or [],
    }
    return json.dumps(snapshot), unreadable


def _unreadable_record(row: checkpoint_store.EntityRow) -> QuarantinedSnapshotRecord:
    return QuarantinedSnapshotRecord(
        kind=row.kind,
        id=row.entity_id,
        reasons=["保存先の行が JSON として読めません"],
        raw=row.entity_json,
    )


def _merged_batch(first: _StagedChanges, later: _StagedChanges) -> _StagedChanges:
    """2 つの保存の予定を重ねた新しい予定（元の予定は変えない）。"""
    merged = _StagedChanges(
        ticket=first.ticket, entities=dict(first.entities), quarantined=first.quarantined
    )
    merged.merge(later)
    return merged


def _merge_quarantine(ours: list[Any], theirs: object) -> list[Any]:
    """退避の記録をまとめる（このプロセスの記録に、別のプロセスだけが持つ記録を足す）。"""
    merged = list(ours)
    seen = {json.dumps(item, sort_keys=True, ensure_ascii=False) for item in ours}
    for item in theirs if isinstance(theirs, list) else []:
        text = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if text not in seen:
            seen.add(text)
            merged.append(item)
    return merged


class AgentRuntimeOracleNormalizedRepository(AgentRuntimeOracleCheckpointRepository):
    """Oracle checkpoint と正規化 projection tables を同時に更新する repository。"""

    def __init__(
        self,
        *,
        checkpoint_key: str = "default",
        projection_retention_days: int = 0,
        projection_write_mode: str = "replace",
        connect_factory: OracleConnectFactory | None = None,
    ) -> None:
        self._oracle_projection_prefix = RUNTIME_PROJECTION_PREFIX
        self._oracle_projection_tables = _oracle_projection_tables(self._oracle_projection_prefix)
        self._oracle_projection_retention_days = max(0, projection_retention_days)
        self._oracle_projection_write_mode = projection_write_mode.strip().lower() or "replace"
        if self._oracle_projection_write_mode not in {"replace", "incremental"}:
            raise ValueError("projection_write_mode must be replace or incremental")
        super().__init__(checkpoint_key=checkpoint_key, connect_factory=connect_factory)

    def _write_projection_cursor(self, cursor: Any, batch: _StagedChanges) -> None:
        """監査用の projection を、書いた Run の分だけ書き直す（#1939）。

        `replace` は Run の行を消して入れ直し、`incremental` は上書きする（消した Run の行は消す）。
        """
        runs = [
            RunState.model_validate_json(text)
            for (kind, _entity_id), text in batch.entities.items()
            if kind == RUN_ENTITY and text is not None
        ]
        run_ids = [
            entity_id
            for (kind, entity_id), text in batch.entities.items()
            if kind == RUN_ENTITY
            and (text is None or self._oracle_projection_write_mode != "incremental")
        ]
        self._delete_projection_runs_cursor(cursor, run_ids)
        if self._oracle_projection_write_mode == "incremental":
            self._upsert_runs(cursor, runs)
        else:
            self._insert_runs(cursor, runs)
        self._apply_projection_retention_cursor(cursor)

    def list_tool_call_audit_projection(
        self,
        *,
        run_id: str | None = None,
        tool_name: str | None = None,
        status: str | None = None,
        approval_status: str | None = None,
        error_code: str | None = None,
        has_guardrail_warnings: bool | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> RuntimeToolCallAuditData:
        """正規化 projection tables からツール監査ログを読み出す。"""
        with self._connect_oracle() as connection, connection.cursor() as cursor:
            total = self._projection_tool_call_count(
                cursor,
                run_id=run_id,
                tool_name=tool_name,
                status=status,
                approval_status=approval_status,
                error_code=error_code,
                has_guardrail_warnings=has_guardrail_warnings,
            )
            rows = self._projection_tool_call_rows(
                cursor,
                run_id=run_id,
                tool_name=tool_name,
                status=status,
                approval_status=approval_status,
                error_code=error_code,
                has_guardrail_warnings=has_guardrail_warnings,
                offset=offset,
                limit=limit,
            )
            artifact_ids_by_step = self._projection_artifact_ids_by_step(
                cursor, [(str(row[0]), str(row[7])) for row in rows]
            )
            # CLOB の列は接続を閉じる前に読む（閉じた後に読むと DPY-1001。#765）。
            records: list[RuntimeToolCallAuditRecord] = []
            for row in rows:
                record = _runtime_audit_record_from_projection_row(
                    row,
                    artifact_ids_by_step=artifact_ids_by_step,
                )
                if record is None:
                    continue
                records.append(record)
            tool_names = self._projection_tool_names(cursor)

        return RuntimeToolCallAuditData(
            **dict(paginate(records, total=total, limit=limit, offset=offset)),
            tool_names=tool_names,
        )

    def _projection_tool_names(self, cursor: Any) -> list[str]:
        """監査に記録されたツール名（一覧と同じく Run のある step だけ。#983）。"""
        tables = self._oracle_projection_tables
        cursor.execute(
            f"""
            SELECT DISTINCT s.tool_name
            FROM {tables["steps"]} s
            JOIN {tables["runs"]} r ON r.run_id = s.run_id
            WHERE s.tool_name IS NOT NULL
            ORDER BY s.tool_name
            """
        )
        return [str(row[0]) for row in cursor.fetchall() if row[0]]

    def _projection_tool_call_rows(
        self,
        cursor: Any,
        *,
        run_id: str | None,
        tool_name: str | None,
        status: str | None,
        approval_status: str | None,
        error_code: str | None,
        has_guardrail_warnings: bool | None,
        offset: int,
        limit: int,
    ) -> list[tuple[Any, ...]]:
        tables = self._oracle_projection_tables
        where_sql, params = _projection_tool_call_where(
            run_id=run_id,
            tool_name=tool_name,
            status=status,
            approval_status=approval_status,
            error_code=error_code,
            has_guardrail_warnings=has_guardrail_warnings,
        )
        params.update(offset_fetch_binds(offset=offset, limit=limit))
        pagination_sql = offset_fetch_clause()
        cursor.execute(
            f"""
            SELECT
                r.run_id,
                r.goal,
                r.status,
                r.agent_id,
                r.metadata_json,
                r.created_at,
                r.updated_at,
                s.step_id,
                s.status,
                s.tool_name,
                s.approval_id,
                s.tool_call_json,
                s.tool_result_json,
                s.started_at,
                s.completed_at,
                a.status AS approval_status
            FROM {tables["runs"]} r
            JOIN {tables["steps"]} s ON s.run_id = r.run_id
            LEFT JOIN {tables["approvals"]} a ON a.approval_id = s.approval_id
            {where_sql}
            ORDER BY r.created_at DESC, s.started_at ASC, s.step_id ASC
            {pagination_sql}
            """,
            **params,
        )
        return list(cursor.fetchall())

    def _projection_tool_call_count(
        self,
        cursor: Any,
        *,
        run_id: str | None,
        tool_name: str | None,
        status: str | None,
        approval_status: str | None,
        error_code: str | None,
        has_guardrail_warnings: bool | None,
    ) -> int:
        tables = self._oracle_projection_tables
        where_sql, params = _projection_tool_call_where(
            run_id=run_id,
            tool_name=tool_name,
            status=status,
            approval_status=approval_status,
            error_code=error_code,
            has_guardrail_warnings=has_guardrail_warnings,
        )
        cursor.execute(
            f"""
            SELECT COUNT(*)
            FROM {tables["runs"]} r
            JOIN {tables["steps"]} s ON s.run_id = r.run_id
            LEFT JOIN {tables["approvals"]} a ON a.approval_id = s.approval_id
            {where_sql}
            """,
            **params,
        )
        row = cursor.fetchone()
        return int(row[0]) if row is not None else 0

    def _projection_artifact_ids_by_step(
        self, cursor: Any, steps: Sequence[tuple[str, str]]
    ) -> dict[str, list[str]]:
        if not steps:
            return {}
        import oracledb

        table = self._oracle_projection_tables["events"]
        cursor.setinputsizes(audit_page=oracledb.DB_TYPE_CLOB)
        cursor.execute(
            f"""
            SELECT e.payload_json
            FROM {table} e
            JOIN JSON_TABLE(:audit_page, '$[*]' COLUMNS (
                run_id VARCHAR2(200) PATH '$.run_id',
                step_id VARCHAR2(200) PATH '$.step_id'
            )) p ON p.run_id = e.run_id
            WHERE e.event_type = :event_type
              AND JSON_VALUE(e.payload_json, '$.step_id' RETURNING VARCHAR2(200)) = p.step_id
            ORDER BY e.created_at, e.event_id
            """,
            event_type=RunEventType.ARTIFACT_CREATED.value,
            audit_page=_json_dump(
                [{"run_id": run_id, "step_id": step_id} for run_id, step_id in sorted(set(steps))]
            ),
        )
        artifact_ids_by_step: dict[str, list[str]] = {}
        for (payload_json,) in cursor.fetchall():
            payload = _json_load_object(payload_json)
            step_id = payload.get("step_id")
            artifact_id = payload.get("artifact_id")
            if isinstance(step_id, str) and isinstance(artifact_id, str):
                artifact_ids_by_step.setdefault(step_id, []).append(artifact_id)
        return artifact_ids_by_step

    def _delete_projection_runs_cursor(self, cursor: Any, run_ids: list[str]) -> None:
        """Run の projection の行を消す（子の表から）。"""
        if not run_ids:
            return
        for table in reversed(list(self._oracle_projection_tables.values())):
            cursor.executemany(
                f"DELETE FROM {table} WHERE run_id = :run_id",  # nosec B608 - 固定の表名
                [{"run_id": run_id} for run_id in run_ids],
            )

    def _apply_projection_retention_cursor(self, cursor: Any) -> None:
        if self._oracle_projection_retention_days <= 0:
            return
        tables = self._oracle_projection_tables
        params = {"retention_days": self._oracle_projection_retention_days}
        # Delete children first so a future FK-backed schema can reuse the same order.
        for table, column in (
            (tables["artifacts"], "created_at"),
            (tables["events"], "created_at"),
            (tables["approvals"], "created_at"),
            (tables["steps"], "completed_at"),
            (tables["runs"], "created_at"),
        ):
            cursor.execute(
                f"""
                DELETE FROM {table}
                WHERE {column} IS NOT NULL
                  AND {column} < SYSTIMESTAMP - NUMTODSINTERVAL(:retention_days, 'DAY')
                """,
                **params,
            )

    def _insert_runs(self, cursor: Any, runs: list[RunState]) -> None:
        tables = self._oracle_projection_tables
        for run in runs:
            cursor.execute(
                f"""
                INSERT INTO {tables["runs"]} (
                    run_id,
                    agent_id,
                    status,
                    goal,
                    metadata_json,
                    pending_tool_calls_json,
                    created_at,
                    updated_at
                ) VALUES (
                    :run_id,
                    :agent_id,
                    :status,
                    :goal,
                    :metadata_json,
                    :pending_tool_calls_json,
                    :created_at,
                    :updated_at
                )
                """,
                run_id=run.id,
                agent_id=run.agent_id,
                status=run.status.value,
                goal=run.goal,
                metadata_json=_json_dump(run.metadata),
                pending_tool_calls_json=_model_list_json(run.pending_tool_calls),
                created_at=run.created_at,
                updated_at=run.updated_at,
            )
            self._insert_events(cursor, run)
            self._insert_steps(cursor, run)
            self._insert_approvals(cursor, run)
            self._insert_artifacts(cursor, run)

    def _insert_events(self, cursor: Any, run: RunState) -> None:
        table = self._oracle_projection_tables["events"]
        for event in run.events:
            cursor.execute(
                f"""
                INSERT INTO {table} (
                    event_id,
                    run_id,
                    event_type,
                    message,
                    payload_json,
                    created_at
                ) VALUES (
                    :event_id,
                    :run_id,
                    :event_type,
                    :message,
                    :payload_json,
                    :created_at
                )
                """,
                event_id=event.id,
                run_id=event.run_id,
                event_type=event.type.value,
                message=event.message,
                payload_json=_json_dump(event.payload),
                created_at=event.created_at,
            )

    def _insert_steps(self, cursor: Any, run: RunState) -> None:
        table = self._oracle_projection_tables["steps"]
        for step in run.steps:
            cursor.execute(
                f"""
                INSERT INTO {table} (
                    step_id,
                    run_id,
                    kind,
                    status,
                    tool_name,
                    approval_id,
                    tool_call_json,
                    tool_result_json,
                    started_at,
                    completed_at
                ) VALUES (
                    :step_id,
                    :run_id,
                    :kind,
                    :status,
                    :tool_name,
                    :approval_id,
                    :tool_call_json,
                    :tool_result_json,
                    :started_at,
                    :completed_at
                )
                """,
                step_id=step.id,
                run_id=step.run_id,
                kind=step.kind,
                status=step.status.value,
                tool_name=step.tool_call.name if step.tool_call else None,
                approval_id=step.approval_id,
                tool_call_json=step.tool_call.model_dump_json() if step.tool_call else None,
                tool_result_json=step.tool_result.model_dump_json() if step.tool_result else None,
                started_at=step.started_at,
                completed_at=step.completed_at,
            )

    def _insert_approvals(self, cursor: Any, run: RunState) -> None:
        table = self._oracle_projection_tables["approvals"]
        for approval in run.approvals:
            cursor.execute(
                f"""
                INSERT INTO {table} (
                    approval_id,
                    run_id,
                    step_id,
                    tool_name,
                    status,
                    reason,
                    decided_by,
                    decided_at,
                    created_at,
                    tool_call_json
                ) VALUES (
                    :approval_id,
                    :run_id,
                    :step_id,
                    :tool_name,
                    :status,
                    :reason,
                    :decided_by,
                    :decided_at,
                    :created_at,
                    :tool_call_json
                )
                """,
                approval_id=approval.id,
                run_id=approval.run_id,
                step_id=approval.step_id,
                tool_name=approval.tool_call.name,
                status=approval.status.value,
                reason=approval.reason,
                decided_by=approval.decided_by,
                decided_at=approval.decided_at,
                created_at=approval.created_at,
                tool_call_json=approval.tool_call.model_dump_json(),
            )

    def _insert_artifacts(self, cursor: Any, run: RunState) -> None:
        table = self._oracle_projection_tables["artifacts"]
        for artifact in run.artifacts:
            cursor.execute(
                f"""
                INSERT INTO {table} (
                    artifact_id,
                    run_id,
                    name,
                    kind,
                    content_json,
                    created_at
                ) VALUES (
                    :artifact_id,
                    :run_id,
                    :name,
                    :kind,
                    :content_json,
                    :created_at
                )
                """,
                artifact_id=artifact.id,
                run_id=run.id,
                name=artifact.name,
                kind=artifact.kind,
                content_json=_json_dump(artifact.content),
                created_at=artifact.created_at,
            )

    def _upsert_runs(self, cursor: Any, runs: list[RunState]) -> None:
        tables = self._oracle_projection_tables
        for run in runs:
            _merge_projection_row(
                cursor,
                table=tables["runs"],
                key_column="run_id",
                values={
                    "run_id": run.id,
                    "agent_id": run.agent_id,
                    "status": run.status.value,
                    "goal": run.goal,
                    "metadata_json": _json_dump(run.metadata),
                    "pending_tool_calls_json": _model_list_json(run.pending_tool_calls),
                    "created_at": run.created_at,
                    "updated_at": run.updated_at,
                },
            )
            self._upsert_events(cursor, run)
            self._upsert_steps(cursor, run)
            self._upsert_approvals(cursor, run)
            self._upsert_artifacts(cursor, run)

    def _upsert_events(self, cursor: Any, run: RunState) -> None:
        table = self._oracle_projection_tables["events"]
        for event in run.events:
            _merge_projection_row(
                cursor,
                table=table,
                key_column="event_id",
                values={
                    "event_id": event.id,
                    "run_id": event.run_id,
                    "event_type": event.type.value,
                    "message": event.message,
                    "payload_json": _json_dump(event.payload),
                    "created_at": event.created_at,
                },
            )

    def _upsert_steps(self, cursor: Any, run: RunState) -> None:
        table = self._oracle_projection_tables["steps"]
        for step in run.steps:
            _merge_projection_row(
                cursor,
                table=table,
                key_column="step_id",
                values={
                    "step_id": step.id,
                    "run_id": step.run_id,
                    "kind": step.kind,
                    "status": step.status.value,
                    "tool_name": step.tool_call.name if step.tool_call else None,
                    "approval_id": step.approval_id,
                    "tool_call_json": step.tool_call.model_dump_json() if step.tool_call else None,
                    "tool_result_json": (
                        step.tool_result.model_dump_json() if step.tool_result else None
                    ),
                    "started_at": step.started_at,
                    "completed_at": step.completed_at,
                },
            )

    def _upsert_approvals(self, cursor: Any, run: RunState) -> None:
        table = self._oracle_projection_tables["approvals"]
        for approval in run.approvals:
            _merge_projection_row(
                cursor,
                table=table,
                key_column="approval_id",
                values={
                    "approval_id": approval.id,
                    "run_id": approval.run_id,
                    "step_id": approval.step_id,
                    "tool_name": approval.tool_call.name,
                    "status": approval.status.value,
                    "reason": approval.reason,
                    "decided_by": approval.decided_by,
                    "decided_at": approval.decided_at,
                    "created_at": approval.created_at,
                    "tool_call_json": approval.tool_call.model_dump_json(),
                },
            )

    def _upsert_artifacts(self, cursor: Any, run: RunState) -> None:
        table = self._oracle_projection_tables["artifacts"]
        for artifact in run.artifacts:
            _merge_projection_row(
                cursor,
                table=table,
                key_column="artifact_id",
                values={
                    "artifact_id": artifact.id,
                    "run_id": run.id,
                    "name": artifact.name,
                    "kind": artifact.kind,
                    "content_json": _json_dump(artifact.content),
                    "created_at": artifact.created_at,
                },
            )


def _event_index_after(events: list[RunEvent], event_id: str | None) -> int:
    if event_id is None:
        return 0
    for index, event in enumerate(events):
        if event.id == event_id:
            return index + 1
    return 0


def _is_terminal(status: RunStatus) -> bool:
    return status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}


def _normalize_runtime_status(value: str) -> RunStatus | None:
    normalized = value.strip().lower().replace("-", "_")
    if normalized in {"queued", "pending", "created", "submitted"}:
        return RunStatus.QUEUED
    if normalized in {"running", "in_progress", "streaming", "active", "working"}:
        return RunStatus.RUNNING
    if normalized in {"completed", "complete", "succeeded", "success", "done"}:
        return RunStatus.COMPLETED
    if normalized in {"failed", "failure", "error", "errored"}:
        return RunStatus.FAILED
    if normalized in {"cancelled", "canceled", "stopped", "aborted"}:
        return RunStatus.CANCELLED
    if normalized in {"waiting_approval", "requires_action", "interrupted"}:
        return RunStatus.WAITING_APPROVAL
    if normalized == "waiting_confirmation":
        return RunStatus.WAITING_CONFIRMATION
    return None


_RUNTIME_SECRET_KEY = re.compile(
    r"(?:authorization|cookie|password|passwd|secret|token|api[_-]?key|credential)",
    re.IGNORECASE,
)


def _runtime_event_metadata(payload: JsonObject) -> JsonObject:
    value = _safe_runtime_value(payload)
    return value if isinstance(value, dict) else {}


def _safe_runtime_value(value: object, *, depth: int = 0) -> object:
    if depth >= 5:
        return "[truncated]"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:2000]
    if isinstance(value, list | tuple):
        return [_safe_runtime_value(item, depth=depth + 1) for item in value[:50]]
    if isinstance(value, dict):
        result: JsonObject = {}
        for raw_key, item in list(value.items())[:50]:
            key = str(raw_key)[:128]
            if _RUNTIME_SECRET_KEY.search(key):
                result[key] = "[REDACTED]"
            elif key.lower() in {"uri", "url", "href"} and isinstance(item, str):
                result[key] = item.split("?", 1)[0].split("#", 1)[0][:2000]
            else:
                result[key] = _safe_runtime_value(item, depth=depth + 1)
        return result
    return str(value)[:2000]


def _runtime_event_key(payload: JsonObject, cursor: str | None) -> str:
    for key in ("id", "event_id", "eventId", "cursor"):
        value = payload.get(key)
        if isinstance(value, str | int):
            return f"{key}:{value}"
    if cursor:
        return f"cursor:{cursor}"
    canonical = json.dumps(
        _runtime_event_metadata(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _runtime_artifact(run_id: str, payload: JsonObject, index: int) -> Artifact:
    external_id = str(payload.get("id") or payload.get("artifact_id") or index)
    digest = hashlib.sha256(f"{run_id}:{external_id}".encode()).hexdigest()[:24]
    name = str(payload.get("name") or payload.get("title") or f"Runtime artifact {index + 1}")
    kind = str(payload.get("kind") or payload.get("type") or "runtime_artifact")
    return Artifact(
        id=f"artifact_runtime_{digest}",
        name=name[:256],
        kind=kind[:128],
        content=_runtime_event_metadata(payload),
    )


def builtin_resume_pending(run: RunState) -> bool:
    """承認がすべて決まり、保存した SDK の状態から再開を待つ組み込み Runtime の Run か。"""
    return (
        run.runtime_id == BUILTIN_RUNTIME_ID
        and run.status == RunStatus.QUEUED
        and isinstance(run.metadata.get(_BUILTIN_STATE_KEY), str)
    )


def _pending_approval_count(run: RunState) -> int:
    return sum(1 for approval in run.approvals if approval.status == ApprovalStatus.PENDING)


def _pending_agent_confirmations(run: RunState) -> list[ConfirmationRequest]:
    """Run が待っている Agent 自身の確認（#1839）。"""
    return [
        item
        for item in run.confirmations
        if item.source == SOURCE_AGENT and item.status == "pending"
    ]


def _validate_oracle_identifier(identifier: str) -> str:
    normalized = identifier.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_$#]{0,127}", normalized):
        raise ValueError(f"invalid Oracle identifier: {identifier}")
    return normalized


def _oracle_projection_tables(prefix: str) -> dict[str, str]:
    suffixes = {
        "runs": "RUNS",
        "events": "EVENTS",
        "steps": "STEPS",
        "approvals": "APPROVALS",
        "artifacts": "ARTIFACTS",
    }
    return {
        key: _validate_oracle_identifier(f"{prefix}_{suffix}") for key, suffix in suffixes.items()
    }


def _merge_projection_row(
    cursor: Any,
    *,
    table: str,
    key_column: str,
    values: dict[str, Any],
) -> None:
    safe_table = _validate_oracle_identifier(table)
    safe_key = _validate_oracle_identifier(key_column)
    safe_columns = [_validate_oracle_identifier(column) for column in values]
    source_select = ", ".join(f":{column} AS {column}" for column in safe_columns)
    update_columns = [column for column in safe_columns if column != safe_key]
    update_sql = ", ".join(f"target.{column} = source.{column}" for column in update_columns)
    insert_columns = ", ".join(safe_columns)
    insert_values = ", ".join(f"source.{column}" for column in safe_columns)
    cursor.execute(
        f"""
        MERGE INTO {safe_table} target
        USING (SELECT {source_select} FROM dual) source
        ON (target.{safe_key} = source.{safe_key})
        WHEN MATCHED THEN UPDATE SET {update_sql}
        WHEN NOT MATCHED THEN INSERT ({insert_columns})
        VALUES ({insert_values})
        """,
        **values,
    )


def _projection_tool_call_where(
    *,
    run_id: str | None,
    tool_name: str | None,
    status: str | None,
    approval_status: str | None,
    error_code: str | None,
    has_guardrail_warnings: bool | None,
) -> tuple[str, JsonObject]:
    clauses: list[str] = []
    params: JsonObject = {}
    if run_id is not None:
        clauses.append("r.run_id = :run_id")
        params["run_id"] = run_id
    if tool_name is not None:
        clauses.append("s.tool_name = :tool_name")
        params["tool_name"] = tool_name
    if status is not None:
        clauses.append("s.status = :status")
        params["status"] = status
    if approval_status is not None:
        clauses.append("a.status = :approval_status")
        params["approval_status"] = approval_status
    if error_code is not None:
        clauses.append("JSON_VALUE(s.tool_result_json, '$.error_code') = :error_code")
        params["error_code"] = error_code
    if has_guardrail_warnings is True:
        clauses.append("JSON_EXISTS(s.tool_result_json, '$.guardrail_warnings[*]')")
    elif has_guardrail_warnings is False:
        clauses.append("NOT JSON_EXISTS(s.tool_result_json, '$.guardrail_warnings[*]')")
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where_sql, params


def _is_oracle_table_missing_error(exc: Exception) -> bool:
    text = str(exc)
    return "ORA-00942" in text or "table or view does not exist" in text


def _oracle_lob_to_text(value: object) -> str:
    read = getattr(value, "read", None)
    if callable(read):
        return str(read())
    return str(value)


def _json_dump(value: JsonObject | list[JsonObject]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _maybe_externalize_artifact_content(run_id: str, artifact: Artifact) -> Artifact:
    backend = get_settings().agent_artifact_storage_backend.strip().lower()
    if backend in {"", "inline"}:
        return artifact
    if artifact.kind != "command_output":
        return artifact
    if backend != "filesystem":
        return _artifact_with_storage_warning(
            artifact,
            f"unsupported artifact storage backend: {backend}",
        )
    try:
        content_ref = _write_filesystem_artifact_content(run_id, artifact)
    except OSError as exc:
        return _artifact_with_storage_warning(
            artifact,
            f"artifact storage write failed: {exc.__class__.__name__}",
        )
    return artifact.model_copy(
        update={
            "content": _artifact_summary_content(artifact.content, content_ref),
            "content_ref": content_ref,
        },
        deep=True,
    )


def _hydrate_artifact_content(artifact: Artifact) -> Artifact:
    hydrated = artifact.model_copy(deep=True)
    if hydrated.content_ref is None:
        return hydrated
    if hydrated.content_ref.backend != "filesystem":
        return hydrated
    try:
        hydrated.content = _read_filesystem_artifact_content(hydrated.content_ref)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        hydrated.content = _with_metadata(
            hydrated.content,
            {
                "artifact_storage_error": f"{exc.__class__.__name__}",
            },
        )
    return hydrated


def _write_filesystem_artifact_content(
    run_id: str,
    artifact: Artifact,
) -> ArtifactContentRef:
    payload = _json_dump(artifact.content).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    root = _artifact_storage_root()
    relative_path = Path(_safe_artifact_path_part(run_id)) / (
        f"{_safe_artifact_path_part(artifact.id)}.json"
    )
    path = (root / relative_path).resolve()
    if not _path_is_within(path, root):
        raise OSError("artifact path is outside storage root")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_bytes(payload)
    os.replace(temp_path, path)
    return ArtifactContentRef(
        backend="filesystem",
        uri=relative_path.as_posix(),
        size_bytes=len(payload),
        sha256=digest,
    )


def _read_filesystem_artifact_content(content_ref: ArtifactContentRef) -> JsonObject:
    root = _artifact_storage_root()
    relative_path = Path(content_ref.uri)
    if relative_path.is_absolute():
        raise ValueError("artifact content ref must be relative")
    path = (root / relative_path).resolve()
    if not _path_is_within(path, root):
        raise ValueError("artifact content ref points outside storage root")
    payload = path.read_bytes()
    if content_ref.sha256 and hashlib.sha256(payload).hexdigest() != content_ref.sha256:
        raise ValueError("artifact content checksum mismatch")
    loaded = json.loads(payload.decode("utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("artifact content must be a JSON object")
    return loaded


def _artifact_storage_root() -> Path:
    return Path(get_settings().agent_artifact_storage_path).expanduser().resolve()


def _artifact_summary_content(content: JsonObject, content_ref: ArtifactContentRef) -> JsonObject:
    stdout = content.get("stdout")
    stderr = content.get("stderr")
    summary: JsonObject = {
        key: value for key, value in content.items() if key not in {"stdout", "stderr", "metadata"}
    }
    summary["stdout_bytes"] = _text_size_bytes(stdout)
    summary["stderr_bytes"] = _text_size_bytes(stderr)
    summary["metadata"] = _with_metadata(
        _metadata_object(content.get("metadata")),
        {
            "artifact_storage_backend": content_ref.backend,
            "artifact_storage_uri": content_ref.uri,
            "artifact_content_ref": content_ref.model_dump(mode="json"),
        },
    )
    return summary


def _artifact_with_storage_warning(artifact: Artifact, warning: str) -> Artifact:
    return artifact.model_copy(
        update={"content": _with_metadata(artifact.content, {"artifact_storage_warning": warning})},
        deep=True,
    )


def _with_metadata(content: JsonObject, metadata: JsonObject) -> JsonObject:
    current = dict(content)
    current["metadata"] = {
        **_metadata_object(current.get("metadata")),
        **metadata,
    }
    return current


def _metadata_object(value: object) -> JsonObject:
    return dict(value) if isinstance(value, dict) else {}


def _text_size_bytes(value: object) -> int:
    return len(value.encode("utf-8")) if isinstance(value, str) else 0


def _safe_artifact_path_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", value).strip("._")
    return safe[:160] or "artifact"


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _json_load_object(value: object | None) -> JsonObject:
    if value is None:
        return {}
    try:
        loaded = json.loads(_oracle_lob_to_text(value))
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _json_load_string_list(value: object | None) -> list[str]:
    if value is None:
        return []
    try:
        loaded = json.loads(_oracle_lob_to_text(value))
    except (TypeError, ValueError):
        return []
    if not isinstance(loaded, list):
        return []
    return [item for item in loaded if isinstance(item, str)]


def _model_list_json(values: Sequence[BaseModel]) -> str:
    return json.dumps(
        [value.model_dump(mode="json") for value in values],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _runtime_audit_record_from_projection_row(
    row: tuple[Any, ...],
    *,
    artifact_ids_by_step: dict[str, list[str]],
) -> RuntimeToolCallAuditRecord | None:
    (
        run_id,
        run_goal,
        run_status,
        agent_id,
        metadata_json,
        run_created_at,
        run_updated_at,
        step_id,
        step_status,
        row_tool_name,
        approval_id,
        tool_call_json,
        tool_result_json,
        started_at,
        completed_at,
        row_approval_status,
    ) = row
    tool_call = _projection_tool_call(tool_call_json)
    result = _projection_tool_result(tool_result_json)
    tool_name = str(row_tool_name or (tool_call.name if tool_call is not None else "tool"))
    audit_metadata = result.audit_metadata if result is not None else {}
    permission_level = _audit_text(audit_metadata, "permission_level")
    side_effects = _audit_bool(audit_metadata, "side_effects")

    return RuntimeToolCallAuditRecord(
        run_id=str(run_id),
        run_goal=str(run_goal),
        run_status=str(run_status),
        agent_id=str(agent_id),
        step_id=str(step_id),
        tool_name=tool_name,
        status=str(step_status),
        approval_id=str(approval_id) if approval_id is not None else None,
        approval_status=str(row_approval_status) if row_approval_status is not None else None,
        policy_decision=result.policy_decision.value if result is not None else None,
        permission_level=permission_level,
        side_effects=side_effects,
        started_at=_datetime_text(started_at),
        completed_at=_datetime_text(completed_at),
        duration_ms=result.duration_ms if result is not None else None,
        success=result.success if result is not None else None,
        error=result.error if result is not None else None,
        error_code=result.error_code if result is not None else None,
        guardrail_warnings=result.guardrail_warnings if result is not None else [],
        trace_id=(
            tool_call.trace_id if tool_call is not None else _audit_text(audit_metadata, "trace_id")
        ),
        artifact_ids=artifact_ids_by_step.get(str(step_id), []),
        audit_metadata=audit_metadata,
        run_created_at=_datetime_text(run_created_at) or "",
        run_updated_at=_datetime_text(run_updated_at) or "",
    )


def _projection_tool_call(value: object | None) -> ToolCall | None:
    if value is None:
        return None
    try:
        return ToolCall.model_validate_json(_oracle_lob_to_text(value))
    except ValueError:
        return None


def _projection_tool_result(value: object | None) -> ToolResult | None:
    if value is None:
        return None
    try:
        return ToolResult.model_validate_json(_oracle_lob_to_text(value))
    except ValueError:
        return None


def _datetime_text(value: object | None) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)


def _audit_text(metadata: JsonObject, key: str) -> str | None:
    value = metadata.get(key)
    return value if isinstance(value, str) else None


def _audit_bool(metadata: JsonObject, key: str) -> bool | None:
    value = metadata.get(key)
    return value if isinstance(value, bool) else None


def _tool_call_signature(call: ToolCall) -> str:
    return json.dumps(
        {"name": call.name, "arguments": call.arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def load_snapshot_tolerant(
    snapshot_json: str | bytes, *, source: str = "snapshot"
) -> tuple[AgentRuntimeSnapshot, SnapshotLoadReport]:
    """保存先の snapshot を Run・業務 Agent の 1 件ずつ読み込む（#853）。

    - snapshot 全体が読めない（JSON でない・object でない・未対応の版・runs / agents が配列でない）
      ときだけ `AgentRuntimeSnapshotCorruptError`。
    - schema に合わない・id が空 / 重複・直しても整合しない Run・業務 Agent は読み込まず、
      元の JSON と理由を `quarantined` に退避する（保存のたびに残す）。
    - 安全に直せる不整合（`_repair_run`）は直し、Run の Event に残す。
    """
    try:
        data = json.loads(snapshot_json)
    except (TypeError, ValueError) as exc:
        raise AgentRuntimeSnapshotCorruptError("snapshot が JSON として読めません") from exc
    if not isinstance(data, dict):
        raise AgentRuntimeSnapshotCorruptError("snapshot が JSON の object ではありません")
    version = data.get("version", AgentRuntimeSnapshot.model_fields["version"].default)
    if version not in SUPPORTED_SNAPSHOT_VERSIONS:
        raise AgentRuntimeSnapshotCorruptError(f"未対応の snapshot の版です: {version!r}")
    raw_runs = data.get("runs") or []
    raw_agents = data.get("agents") or []
    if not isinstance(raw_runs, list) or not isinstance(raw_agents, list):
        raise AgentRuntimeSnapshotCorruptError("snapshot の runs / agents が配列ではありません")

    report = SnapshotLoadReport()
    quarantined = _previous_quarantine(data.get("quarantined"))

    def quarantine(kind: str, raw: object, reasons: list[str]) -> None:
        record_id = raw.get("id") if isinstance(raw, dict) else None
        record = QuarantinedSnapshotRecord(
            kind=kind,
            id=record_id if isinstance(record_id, str) else None,
            reasons=reasons,
            raw=raw,
        )
        quarantined.append(record)
        report.quarantined.append(record)
        logger.warning(
            "agent_runtime_snapshot_record_quarantined",
            extra={"source": source, "kind": kind, "record_id": record.id, "reasons": reasons},
        )

    agents: list[AgentProfile] = []
    agent_ids: set[str] = set()
    for raw in raw_agents:
        try:
            agent = AgentProfile.model_validate(raw)
        except ValidationError as exc:
            quarantine("agent", raw, _validation_reasons(exc))
            continue
        if not agent.id or agent.id in agent_ids:
            quarantine("agent", raw, ["agent id が空か、ほかの業務 Agent と重複しています"])
            continue
        agent_ids.add(agent.id)
        agents.append(agent)

    runs: list[RunState] = []
    run_ids: set[str] = set()
    event_ids: set[str] = set()
    artifact_ids: set[str] = set()
    for raw in raw_runs:
        try:
            run = RunState.model_validate(raw)
        except ValidationError as exc:
            quarantine("run", raw, _validation_reasons(exc))
            continue
        if not run.id or run.id in run_ids:
            quarantine("run", raw, ["run id が空か、ほかの Run と重複しています"])
            continue
        repairs = _repair_run(run)
        errors: list[str] = []
        _validate_run_snapshot(run, errors)
        own_events = [event.id for event in run.events]
        own_artifacts = [artifact.id for artifact in run.artifacts]
        if event_ids.intersection(own_events):
            errors.append("Event の id がほかの Run と重複しています")
        if artifact_ids.intersection(own_artifacts):
            errors.append("成果物の id がほかの Run と重複しています")
        if errors:
            quarantine("run", raw, errors)
            continue
        run_ids.add(run.id)
        event_ids.update(own_events)
        artifact_ids.update(own_artifacts)
        runs.append(run)
        if repairs:
            report.repaired.append(RepairedRun(run_id=run.id, repairs=repairs))
            logger.warning(
                "agent_runtime_snapshot_run_repaired",
                extra={"source": source, "run_id": run.id, "repairs": repairs},
            )

    exported_at = data.get("exported_at")
    snapshot = AgentRuntimeSnapshot(
        version=version,
        runs=runs,
        agents=agents,
        quarantined=quarantined,
    )
    if isinstance(exported_at, str):
        with suppress(ValueError):
            snapshot.exported_at = datetime.fromisoformat(exported_at)
    return snapshot, report


def _previous_quarantine(value: object) -> list[QuarantinedSnapshotRecord]:
    """前の読み込みで退避した記録（形が崩れていても元の値ごと残す）。"""
    if not isinstance(value, list):
        return []
    records: list[QuarantinedSnapshotRecord] = []
    for item in value:
        try:
            records.append(QuarantinedSnapshotRecord.model_validate(item))
        except ValidationError:
            records.append(
                QuarantinedSnapshotRecord(
                    kind="unknown", reasons=["退避した記録の形が読めません"], raw=item
                )
            )
    return records


def _validation_reasons(exc: ValidationError) -> list[str]:
    """Pydantic の検証の失敗の要約（値は出さない。項目の場所と種類だけ）。"""
    reasons = [
        f"{'.'.join(str(part) for part in error.get('loc', ())) or '(root)'}: {error.get('type')}"
        for error in exc.errors()[:5]
    ]
    if exc.error_count() > 5:
        reasons.append(f"ほか {exc.error_count() - 5} 件")
    return reasons


# 読み込みで直した内容（Run の Event・ログ・画面の件数に出す。#853）。
REPAIR_RUN_ID_MISMATCH = "run_id_mismatch_fixed"
REPAIR_MISSING_APPROVAL_REFERENCE = "missing_approval_reference_cleared"
REPAIR_TERMINAL_PENDING_APPROVALS = "terminal_run_pending_approvals_cancelled"
REPAIR_WAITING_WITHOUT_APPROVALS = "waiting_approval_without_pending_failed"
REPAIR_WAITING_WITHOUT_CONFIRMATIONS = "waiting_confirmation_without_pending_failed"
REPAIR_TERMINAL_PENDING_CONFIRMATIONS = "terminal_run_pending_confirmations_cancelled"
_REPAIR_ACTOR = "system:storage-repair"


def _repair_run(run: RunState) -> list[str]:
    """1 件の Run の、安全に直せる不整合を直す（直した内容の code の一覧を返す。#853）。

    実行を進める方向には直さない（承認を「承認」にしない・再開しない）。待ちを終わらせる方向
    （承認の取消・Run の失敗）だけにする。
    """
    repairs: list[str] = []
    now = _now()
    # Event / step / 承認の run_id のずれ（入っている Run が正しい）。
    items: list[RunEvent | RunStep | ApprovalRequest] = [*run.events, *run.steps, *run.approvals]
    mismatched = [item for item in items if item.run_id != run.id]
    for item in mismatched:
        item.run_id = run.id
    if mismatched:
        repairs.append(REPAIR_RUN_ID_MISMATCH)
    # 無い承認を指す step（承認待ちの step は取り消す）。
    approval_ids = {approval.id for approval in run.approvals}
    dangling = [
        step
        for step in run.steps
        if step.approval_id is not None and step.approval_id not in approval_ids
    ]
    for step in dangling:
        step.approval_id = None
        if step.status == StepStatus.WAITING_APPROVAL:
            step.status = StepStatus.CANCELLED
            step.completed_at = step.completed_at or now
    if dangling:
        repairs.append(REPAIR_MISSING_APPROVAL_REFERENCE)
    # 終わった Run に残った pending の承認（もう決められない）は取り消す。
    pending = [approval for approval in run.approvals if approval.status == ApprovalStatus.PENDING]
    if _is_terminal(run.status) and pending:
        pending_ids = {approval.id for approval in pending}
        for approval in pending:
            approval.status = ApprovalStatus.CANCELLED
            approval.decided_by = _REPAIR_ACTOR
            approval.decided_at = now
        for step in run.steps:
            if step.approval_id in pending_ids and step.status in {
                StepStatus.PENDING,
                StepStatus.WAITING_APPROVAL,
            }:
                step.status = StepStatus.CANCELLED
                step.completed_at = step.completed_at or now
        run.metadata.pop(_BUILTIN_STATE_KEY, None)
        repairs.append(REPAIR_TERMINAL_PENDING_APPROVALS)
    # 承認待ちなのに pending の承認が無い Run は、再開せずに失敗にする。
    if run.status == RunStatus.WAITING_APPROVAL and not any(
        approval.status == ApprovalStatus.PENDING for approval in run.approvals
    ):
        run.status = RunStatus.FAILED
        run.metadata.pop(_BUILTIN_STATE_KEY, None)
        run.events.append(
            RunEvent(
                run_id=run.id,
                type=RunEventType.RUNTIME_FAILED,
                message="承認待ちの承認が見つからないため、実行を失敗にしました。",
                payload={"error_code": "runtime.inconsistent_state", "source": "storage_repair"},
            )
        )
        repairs.append(REPAIR_WAITING_WITHOUT_APPROVALS)
    # 終わった Run に残った Agent 自身の確認（もう答えられない）は取り消す（#1839。ツールの結果の
    # 確認は完了した Run に出すものなので残す）。
    if _is_terminal(run.status) and _pending_agent_confirmations(run):
        for confirmation in _pending_agent_confirmations(run):
            confirmation.status = "cancelled"
            confirmation.decided_at = now
        repairs.append(REPAIR_TERMINAL_PENDING_CONFIRMATIONS)
    # 確認待ちなのに答えていない確認が無い Run は、再開せずに失敗にする（#1839）。
    if run.status == RunStatus.WAITING_CONFIRMATION and not _pending_agent_confirmations(run):
        run.status = RunStatus.FAILED
        run.metadata.pop(_BUILTIN_STATE_KEY, None)
        run.events.append(
            RunEvent(
                run_id=run.id,
                type=RunEventType.RUNTIME_FAILED,
                message="確認待ちの回答前の確認が見つからないため、実行を失敗にしました。",
                payload={"error_code": "runtime.inconsistent_state", "source": "storage_repair"},
            )
        )
        repairs.append(REPAIR_WAITING_WITHOUT_CONFIRMATIONS)
    if repairs:
        run.updated_at = now
        run.events.append(
            RunEvent(
                run_id=run.id,
                type=RunEventType.RUNTIME_EVENT,
                message="保存先の読み込みで、整合しない実行の状態を直しました。",
                payload={"source": "storage_repair", "repairs": list(repairs)},
            )
        )
    return repairs


def _append_duplicate_errors(label: str, values: list[str], errors: list[str]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
            continue
        seen.add(value)
    if duplicates:
        separator = " " if label[-1:].isascii() else ""
        errors.append(
            f"ID が重複している{label}{separator}があります: {', '.join(sorted(duplicates))}"
        )


def _validate_run_snapshot(run: RunState, errors: list[str]) -> None:
    if not run.id:
        errors.append("ID が空の実行があります。")
    step_ids = [step.id for step in run.steps]
    approval_ids = [approval.id for approval in run.approvals]
    step_id_set = set(step_ids)
    approval_id_set = set(approval_ids)
    _append_duplicate_errors(f"実行 {run.id} のステップ", step_ids, errors)
    _append_duplicate_errors(f"実行 {run.id} の承認", approval_ids, errors)

    for event in run.events:
        if event.run_id != run.id:
            errors.append(
                f"イベント {event.id} の実行 ID（{event.run_id}）が、"
                f"含まれている実行（{run.id}）と違います。"
            )
    for step in run.steps:
        _validate_step_snapshot(run, step, approval_id_set, errors)
    for approval in run.approvals:
        _validate_approval_snapshot(run, approval, step_id_set, errors)

    pending_approvals = [
        approval.id for approval in run.approvals if approval.status == ApprovalStatus.PENDING
    ]
    if _is_terminal(run.status) and pending_approvals:
        errors.append(
            f"終了した実行 {run.id} に承認待ちの承認があります: {', '.join(pending_approvals)}"
        )
    if run.status == RunStatus.WAITING_APPROVAL and not pending_approvals:
        errors.append(f"実行 {run.id} は承認待ちですが、承認待ちの承認がありません。")
    if run.status == RunStatus.WAITING_CONFIRMATION and not _pending_agent_confirmations(run):
        errors.append(f"実行 {run.id} は確認待ちですが、答えていない回答前の確認がありません。")


def _validate_step_snapshot(
    run: RunState,
    step: RunStep,
    approval_ids: set[str],
    errors: list[str],
) -> None:
    if step.run_id != run.id:
        errors.append(
            f"ステップ {step.id} の実行 ID（{step.run_id}）が、"
            f"含まれている実行（{run.id}）と違います。"
        )
    if step.approval_id is not None and step.approval_id not in approval_ids:
        errors.append(f"ステップ {step.id} が、無い承認 {step.approval_id} を参照しています。")


def _validate_approval_snapshot(
    run: RunState,
    approval: ApprovalRequest,
    step_ids: set[str],
    errors: list[str],
) -> None:
    if approval.run_id != run.id:
        errors.append(
            f"承認 {approval.id} の実行 ID（{approval.run_id}）が、"
            f"含まれている実行（{run.id}）と違います。"
        )
    if approval.step_id not in step_ids:
        errors.append(f"承認 {approval.id} が、無いステップ {approval.step_id} を参照しています。")


DEFAULT_AGENT_ID = "default"
RAG_RESEARCH_AGENT_ID = "rag-research"
DATA_QUERY_AGENT_ID = "data-query"
# 組み込みの業務 Agent（#1573）。削除できず、保存済みの snapshot に無ければ読み込みで足す。
# 既定の Agent（default）が先頭（一覧は作成日時の順）。
BUILTIN_AGENT_IDS: tuple[str, ...] = (DEFAULT_AGENT_ID, RAG_RESEARCH_AGENT_ID, DATA_QUERY_AGENT_ID)
# #1573 より前の既定の Agent の名前。保存済みの default がこの名前のままなら、新しい内容にする。
_LEGACY_DEFAULT_AGENT_NAME = "汎用業務 Agent"


def _builtin_agents() -> list[AgentProfile]:
    """組み込みの業務 Agent（資料・データ・その両方）。並びは BUILTIN_AGENT_IDS と同じ。"""
    return [
        AgentProfile(
            id=DEFAULT_AGENT_ID,
            name="資料・データ横断アシスタント",
            description=(
                "社内の資料（業務 RAG）と業務データ（NL2SQL）を、問いに合わせて使い分けて"
                "調べる既定の業務 Agent。"
            ),
            instructions=(
                "問いと途中の結果から、資料の根拠と業務データの値のどちらが要るかを判断して"
                "ツールを呼ぶ。根拠と監査情報を残す。"
            ),
            skill_ids=[
                RAG_AND_STRUCTURED_DATA_SKILL_ID,
                BUSINESS_RAG_RESEARCH_SKILL_ID,
                STRUCTURED_DATA_QUERY_SKILL_ID,
            ],
            source="builtin",
        ),
        AgentProfile(
            id=RAG_RESEARCH_AGENT_ID,
            name="資料調査アシスタント",
            description="社内の資料（業務 RAG）を調べ、根拠の文書と場所を示して答える業務 Agent。",
            instructions="資料の根拠に基づいて答え、根拠の文書名と場所を示す。",
            skill_ids=[BUSINESS_RAG_RESEARCH_SKILL_ID],
            source="builtin",
        ),
        AgentProfile(
            id=DATA_QUERY_AGENT_ID,
            name="データ照会アシスタント",
            description="業務データ（NL2SQL）を照会し、集計の条件と結果を示して答える業務 Agent。",
            instructions="業務データの照会の結果に基づいて答え、集計の条件（期間・対象）を示す。",
            skill_ids=[STRUCTURED_DATA_QUERY_SKILL_ID],
            source="builtin",
        ),
    ]


def _migrate_builtin_agent(agent: AgentProfile) -> AgentProfile:
    """#1573 より前の既定の Agent（名前が「汎用業務 Agent」のまま）を、新しい内容の版にする。

    名前を変えた default は利用者の内容として残す（Skill の ID の置き換えだけ）。
    新しい内容は次の版として公開し、前の版は残す。
    """
    if agent.id != DEFAULT_AGENT_ID or agent.name != _LEGACY_DEFAULT_AGENT_NAME:
        return agent
    builtin = next(item for item in _builtin_agents() if item.id == DEFAULT_AGENT_ID)
    content = {
        "name": builtin.name,
        "description": builtin.description,
        "instructions": builtin.instructions,
        "skill_ids": list(builtin.skill_ids),
    }
    published = agent.published()
    versions = [item.model_copy(deep=True) for item in agent.versions]
    published_version = agent.published_version
    if published is None or published.name == _LEGACY_DEFAULT_AGENT_NAME:
        published_version = max((item.version for item in versions), default=0) + 1
        base = published if published is not None else agent
        versions.append(
            AgentVersion(
                version=published_version,
                note="初期の業務 Agent の見直し（#1573）",
                model_id=base.model_id,
                data_scopes=deepcopy(base.data_scopes),
                **content,
            )
        )
    return agent.model_copy(
        deep=True,
        update={
            **content,
            "versioned": True,
            "versions": versions,
            "published_version": published_version,
        },
    )


def _ensure_versioned(agent: AgentProfile) -> AgentProfile:
    """#770 より前の Agent（版を持たない）は、現在の内容を v1 として公開する。"""
    if agent.versioned:
        return agent
    return agent.model_copy(
        update={
            "versioned": True,
            "versions": [
                AgentVersion(
                    version=1,
                    note="版の導入（#770）の前の内容",
                    published_at=agent.updated_at,
                    **{field: getattr(agent, field) for field in AGENT_VERSIONED_FIELDS},
                )
            ],
            "published_version": 1,
        }
    )


def max_published_versions() -> int:
    """業務 Agent の版の履歴に残す版の数（``AGENT_MAX_PUBLISHED_VERSIONS``。#2356）。"""
    return max(1, int(get_settings().agent_max_published_versions))


def _prune_agent_versions(agent: AgentProfile, *, keep: tuple[int, ...] = ()) -> None:
    """上限を超えた古い版を消す（公開中の版と ``keep`` は残す。番号は振り直さない。#2356）。

    版を参照する記録（Run の ``metadata.agent_version``・品質評価の ``agent_version``）は番号だけを
    持つので、版を消しても消えない。
    """
    pruned = set(
        versions_to_prune(
            (item.version for item in agent.versions),
            keep=(agent.published_version, *keep),
            limit=max_published_versions(),
        )
    )
    if pruned:
        agent.versions = [item for item in agent.versions if item.version not in pruned]


def agent_for_run(agent: AgentProfile, run: RunState) -> AgentProfile:
    """Run が使う版の内容を重ねた Agent（下書きの Run・版の無い古い Run は現在の内容）。

    版の履歴の上限で消えた版を使う Run（承認を待つ間に公開が続いたときなど。#2356）は、下書きでは
    なく公開中の版で続ける（利用者の Run に公開していない下書きを使わない）。
    """
    version = run.metadata.get("agent_version")
    target = next(
        (item for item in agent.versions if isinstance(version, int) and item.version == version),
        None,
    )
    if target is None and isinstance(version, int):
        target = agent.published()
    if target is None:
        return agent.model_copy(deep=True)
    return agent.model_copy(
        deep=True,
        update={
            **{field: getattr(target, field) for field in AGENT_VERSIONED_FIELDS},
            # 版に固定したスキル（#1855）。固定なし・常に最新の版は None で今のスキル（#1867）。
            "pinned_skills": (
                None if target.skill_policy == SKILL_POLICY_LATEST else deepcopy(target.skills)
            ),
            # 固定の中身の無い版（#1855 より前の版・組み込みの業務 Agent の版）は今のスキルで
            # 動くが、公開した時点のスキルが Agent 主体の取得を宣言していたかは分からない。
            # 今のスキルの宣言で配備の時点に振る舞いを変えないよう、RAG が検索文を計画する
            # （#2651）。利用者が「常に最新」を選んだ版は今のスキルに従う。
            "caller_planning_allowed": (
                target.skill_policy == SKILL_POLICY_LATEST or target.skills is not None
            ),
        },
    )


def _active_tool_policy() -> ToolPolicy:
    config = runtime_config_store.get_tool_policy()
    return ToolPolicy(
        default_mode=config.default_mode,
        allow=config.allow,
        ask=config.ask,
        deny=config.deny,
    )


class AgentRuntimeStorageUnavailableError(RuntimeError):
    """保存先のデータベースに接続できず、Run の repository を読み込めない（#1212）。

    起動は止めない。次に使うとき（API の要求・バックグラウンドの再試行・dispatcher の次の claim）に
    読み込み直す。
    """

    safe_for_user = True


class AgentRuntimeMigrationRequiredError(AgentRuntimeStorageUnavailableError):
    """保存先が Run ごとの行に移す前の形（システムテーブルの更新が要る。#1939）。

    業務の API は 503 で、この文を返す。システムテーブルの更新を適用すると、再起動せずに読み込む。
    """


def build_runtime_repository() -> AgentRuntimeRepositoryContract:
    """設定の保存先の Run の repository を作る（Oracle・file は保存済みの状態を読み込む）。

    Oracle に接続できないときは `AgentRuntimeStorageUnavailableError`（`auto` でも memory に縮退
    しない。DB が戻れば次に使うときに読み込む。#1212）。
    """
    settings = get_settings()
    # `auto`（既定）は DB の設定がそろっていれば oracle_checkpoint、無ければ memory（#839）。
    backend = storage_backend.resolved_backend()
    if backend in storage_backend.ORACLE_BACKENDS:
        # 共通の PLATFORM_ORACLE_* で接続する（#764。旧 AGENT_RUNTIME_ORACLE_* は読まない）。
        try:
            if backend == "oracle_normalized":
                return AgentRuntimeOracleNormalizedRepository(
                    projection_retention_days=settings.agent_runtime_projection_retention_days,
                    projection_write_mode=settings.agent_runtime_projection_write_mode,
                )
            return AgentRuntimeOracleCheckpointRepository()
        except AgentRuntimeSnapshotCorruptError:
            # checkpoint 全体が読めない。明示した Oracle はデータを守って止める（直し方は
            # 例外のメッセージ）。`auto` は checkpoint を上書きしないよう memory にし、
            # 画面（保存先のカード）で案内する（#853）。
            if not storage_backend.is_auto():
                raise
            logger.error("agent_runtime_checkpoint_corrupt_use_memory", exc_info=True)
            storage_backend.fall_back_to_memory(reason=storage_backend.FALLBACK_CHECKPOINT_INVALID)
            backend = "memory"
        except Exception as exc:
            if not is_oracle_connection_error(exc):
                raise
            raise AgentRuntimeStorageUnavailableError(
                f"Agent の保存先のデータベースに接続できません（{_oracle_error_summary(exc)}）。"
                "データベースが起動しているか、システム設定 > データベースを確認してください。"
                "接続できるようになると、再起動せずに読み込みます。"
            ) from exc
    if backend in {"memory", "in_memory", "file", "file_snapshot"}:
        snapshot_path = (
            settings.agent_runtime_snapshot_path
            if backend in {"file", "file_snapshot"} or settings.agent_runtime_snapshot_path
            else None
        )
        return AgentRuntimeRepository(snapshot_path=snapshot_path)
    raise RuntimeError(f"unsupported Agent Runtime repository backend: {backend}")


def _oracle_error_summary(exc: BaseException) -> str:
    """ORA / DPY のコードだけ（接続先・資格情報は出さない）。"""
    codes = oracle_error_codes(exc)
    return ", ".join(codes) if codes else type(exc).__name__


# Run の repository は import 時に作らず、最初に使うときに作る（#1212）。import 時に Oracle の
# checkpoint を読むと、DB に接続できないあいだ backend が起動しない（uvicorn は app の import の
# 後に listen する）。作れなかったときは保持せず、次に使うときに作り直す。
_repository_lock = Lock()
_repository: AgentRuntimeRepositoryContract | None = None


def get_runtime_repository() -> AgentRuntimeRepositoryContract:
    """Run の repository（未作成なら作る）。

    DB に接続できなければ `AgentRuntimeStorageUnavailableError`。
    """
    global _repository
    repository = _repository
    if repository is not None:
        return repository
    with _repository_lock:
        if _repository is None:
            _repository = build_runtime_repository()
        return _repository


def runtime_repository_loaded() -> bool:
    """Run の repository を作り終えているか（作らない。接続を試さない）。"""
    return _repository is not None


def loaded_runtime_repository() -> AgentRuntimeRepositoryContract | None:
    """作り終えた Run の repository（未作成なら None。作らない。接続を試さない）。"""
    return _repository


def set_runtime_repository(repository: AgentRuntimeRepositoryContract | None) -> None:
    """Run の repository を差し替える（テスト。None で次に使うときに設定から作り直す）。"""
    global _repository
    with _repository_lock:
        _repository = repository


class _LazyRuntimeRepository:
    """`runtime_repository` の遅延の参照（属性の読み書きを、その時の repository へ渡す）。

    利用側は今までどおり `from app.features.agent.runtime import runtime_repository` で使える。
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        return getattr(get_runtime_repository(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(get_runtime_repository(), name, value)

    def __delattr__(self, name: str) -> None:
        delattr(get_runtime_repository(), name)

    def __repr__(self) -> str:
        loaded = _repository
        return f"<lazy runtime repository: {type(loaded).__name__ if loaded else 'not loaded'}>"


runtime_repository: AgentRuntimeRepositoryContract = cast(
    AgentRuntimeRepositoryContract, _LazyRuntimeRepository()
)


def _run_finished(run_id: str) -> bool:
    """Run がキャンセル・終了したか（ツールの層のジョブの待ちが使う。#3240）。"""
    try:
        status = runtime_repository.get_run(run_id).status
    except KeyError:
        return True
    except Exception:  # noqa: BLE001 - 保存先の一時的な失敗では待ちを止めない
        logger.warning("mcp_job_wait_run_status_failed", extra={"run_id": run_id}, exc_info=True)
        return False
    return status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}


set_run_finished_check(_run_finished)


def pin_skills(skill_ids: list[str]) -> list[PinnedSkill]:
    """業務 Agent が使うスキルの今の中身（公開の版に固定する。#1855）。"""
    return [pin_skill(skill) for skill_id in skill_ids if (skill := skill_registry.get(skill_id))]


def skill_updates(pins: list[PinnedSkill]) -> list[str]:
    """固定した中身から変わった・消えたスキルの ID（#1855）。"""
    changed: list[str] = []
    for pin in pins:
        if skill_registry.content_hash(pin.id) != pin.content_hash:
            changed.append(pin.id)
    return changed
