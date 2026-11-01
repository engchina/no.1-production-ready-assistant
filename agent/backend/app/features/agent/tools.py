"""Agent Runtime の統一ツール契約。

Control Plane のツール（`tool_registry`）と、MCP 接続のツール（RAG / NL2SQL / 外部 MCP。#757）を
同じポリシー・ガードレール・監査で呼ぶ。業務 RAG / NL2SQL はこのプロジェクト内で実装しない。

依存は「業務 Agent → スキル → ツール」の一方向（#3240）。この module（とここから import する
module）はスキルの定義・ID・一覧を import・参照しない。どのツールを Run に渡すかはスキルの側
（`builtin_runtime.agent_mcp_requirements` / `agent_tool_names`）が決める
（`tests/test_skill_tool_boundary.py`）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from time import monotonic, perf_counter
from types import MappingProxyType
from typing import Any
from uuid import uuid4

import httpx
from pr_backend_core.internal_http import http_client_options
from pr_backend_core.observability.mcp_trace import mcp_trace_meta
from pr_backend_core.observability.request_context import outbound_correlation_headers
from pr_backend_core.observability.tracing import capture_trace_context, observed_span
from pr_system_settings.auth.errors import SecurityApiError
from pr_system_settings.auth.service_token import issue_service_token
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

from app.features.agent.call_abort import CallAbort, current_call_abort
from app.features.agent.config import (
    PRODUCT_MCP_SERVICE_NAMES,
    McpConnectionConfig,
    runtime_config_store,
)
from app.features.agent.mcp_url import mask_url_credentials, mask_urls_in_text
from app.features.agent.profile_name_migration import migrate_rag_call
from app.features.agent.tool_arguments import (
    INVALID_ARGUMENTS_CODE,
    INVALID_ARGUMENTS_MESSAGE,
    SchemaUnsupported,
    validate_arguments,
)
from app.features.agent.tool_media import (
    CONTENT_IMAGES_KEY,
    USER_ONLY,
    ToolImage,
    ToolOutputWithImages,
    decode_image,
)
from app.settings import get_settings

logger = logging.getLogger(__name__)

JsonObject = dict[str, Any]
_MCP_OAUTH_TOKEN_SKEW_SECONDS = 30.0
_mcp_oauth_token_cache: dict[str, tuple[str, float]] = {}


def _now() -> datetime:
    return datetime.now(UTC)


class ToolPermissionLevel(StrEnum):
    READ = "read"
    WRITE = "write"
    SENSITIVE = "sensitive"


class ToolPolicyDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class ExternalToolMode(StrEnum):
    EXECUTE = "execute"
    DRY_RUN = "dry_run"


class ToolCall(BaseModel):
    name: str
    arguments: JsonObject = Field(default_factory=dict)
    trace_id: str | None = None
    # 業務 Agent のデータの範囲を当てた記録（#1378。接続・使ったプロファイル・埋めた / 上書きした /
    # 拒否した / 絞った）。呼び先へは送らない（送るのは arguments だけ）。範囲が効かなければ None。
    data_scope: JsonObject | None = None
    # 外部への書き込みの論理操作の ID（#72。`external_writes.operation_key`）。副作用のあり得る
    # 呼び出しにだけ付け、MCP の `params._meta` で呼び先へ渡す（引数には足さない）。承認・再開・
    # 通信の再送・結果不明の後の承認済みの再送で同じ値を保つ。
    operation_key: str | None = None

    @model_validator(mode="before")
    @classmethod
    def migrate_saved_names(cls, value: object) -> object:
        return migrate_rag_call(value)


class ToolDefinition(BaseModel):
    name: str
    description: str
    # 人が読む表示名（MCP のツールの `title`）。画面の処理の経過が内部名の代わりに出す。モデルには
    # 渡さない（モデルが見るのは name と description）。無ければ None（画面は内部名を出す）。
    title: str | None = None
    input_schema: JsonObject
    output_schema: JsonObject
    permission_level: ToolPermissionLevel = ToolPermissionLevel.READ
    side_effects: bool = False
    timeout_seconds: float = 10.0
    max_retries: int = 0
    audit_tags: list[str] = Field(default_factory=list)
    # 読み取り専用（`permission_level=read` かつ副作用なし）の宣言を既定の判定に使えるか（#1832）。
    # Control Plane のツールは常に True。MCP 接続のツールは、接続が注釈を信頼するときだけ True。
    annotations_trusted: bool = True
    # 標準の接続（RAG / NL2SQL）のツールで、Agent が既定で承認なしに実行すると決めたもの（#1832。
    # `nl2sql_query`）。ツール権限で個別に指定（ask / deny を含む）すればそちらが優先する。
    standard_auto_approve: bool = False
    # MCP 接続のツールの出自（接続の ID と `tools/list` の name。それ以外のツールは None）。
    # モデルに渡す名前は衝突を避けて元の名前と変わることがあるので（#2596）、Skill の requirement・
    # 指示の名前との対応と、別のツールの名前の衝突の検出はこちらで行う。
    mcp_server_id: str | None = None
    mcp_tool_name: str | None = None
    # 同じ呼び出しを繰り返しても追加の副作用が無いという申告（MCP の `annotations.idempotentHint`。
    # #72）。信頼するのは `annotations_trusted` のときだけ（`resend_safe`）。
    idempotent: bool = False

    @property
    def resend_safe(self) -> bool:
        """届いた後の失敗（結果が不明）で同じ呼び出しを送り直してよいか（#2595・#72）。

        信頼できる読み取り専用か、信頼できる冪等の申告（`idempotentHint`）のツールだけ。
        """
        return self.trusted_read_only or (self.idempotent and self.annotations_trusted)

    @property
    def trusted_read_only(self) -> bool:
        """読み取り専用（副作用なし）で、その宣言を信頼できるか（#2595・#2632）。

        同じ呼び出しを繰り返しても副作用が無いと Control Plane が判断してよいのは、これが True の
        ツールだけ（承認の既定の判定・圧縮した結果の読み直しの案内）。管理者の個別の許可や利用者の
        承認は、繰り返しの安全の根拠にしない。
        """
        return (
            self.permission_level == ToolPermissionLevel.READ
            and not self.side_effects
            and self.annotations_trusted
        )


class ToolResult(BaseModel):
    name: str
    success: bool
    output: JsonObject | None = None
    error: str | None = None
    error_code: str | None = None
    error_details: JsonObject = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=_now)
    completed_at: datetime = Field(default_factory=_now)
    duration_ms: int = 0
    policy_decision: ToolPolicyDecision = ToolPolicyDecision.ALLOW
    approval_required: bool = False
    approval_id: str | None = None
    guardrail_warnings: list[str] = Field(default_factory=list)
    audit_metadata: JsonObject = Field(default_factory=dict)
    # 結果の画像（#2650）。保存・直列化しない（step・イベント・ログには `output` の参照だけ）。
    _images: tuple[ToolImage, ...] = PrivateAttr(default=())


def tool_result_images(result: ToolResult) -> tuple[ToolImage, ...]:
    """ツールの結果の画像（モデルへの入力に使う。保存しない）。"""
    return result._images  # noqa: SLF001 - 非保存の属性の読み出しはここだけ


def with_tool_result_images(result: ToolResult, images: tuple[ToolImage, ...]) -> ToolResult:
    """画像を付け替えた結果（元の結果は変えない）。"""
    copied = result.model_copy()
    copied._images = tuple(images)  # noqa: SLF001
    return copied


class ExternalMcpToolInfo(BaseModel):
    name: str
    # MCP のツールの `title`（表示名。古い版の仕様の `annotations.title` も読む）。
    title: str | None = None
    description: str = ""
    input_schema: JsonObject = Field(default_factory=dict)
    output_schema: JsonObject | None = None
    server_id: str | None = None
    # MCP の annotations.readOnlyHint。信頼した接続では、True なら既定で承認なしに呼べる。
    read_only: bool = False
    # MCP の annotations.idempotentHint（書き込みのツールだけ意味がある。#72）。信頼した接続では、
    # 届いた後の失敗でも同じ呼び出しを送り直す。
    idempotent: bool = False
    # ツール権限で個別に決めていないときの判定（接続のツールの一覧の画面に出す。#1832）。
    default_decision: ToolPolicyDecision | None = None
    # Agent のモデルに渡す function tool の名前（`<接続>__<ツール>`）。
    function_name: str | None = None
    metadata: JsonObject = Field(default_factory=dict)


class ExternalMcpToolsData(BaseModel):
    tools: list[ExternalMcpToolInfo] = Field(default_factory=list)
    metadata: JsonObject = Field(default_factory=dict)


class JsonRpcResponse(BaseModel):
    jsonrpc: str | None = None
    id: str | int | None = None
    result: JsonObject | None = None
    error: JsonObject | None = None


def freeze_profile_scopes(
    scopes: Mapping[str, Iterable[str]] | None,
) -> Mapping[str, tuple[str, ...]]:
    """接続 → プロファイルの ID を、書き換えられない形（読み取り専用の mapping と tuple）にする。"""
    return MappingProxyType({connection: tuple(ids) for connection, ids in (scopes or {}).items()})


class ToolInvocationContext(BaseModel):
    """1 回のツールの呼び出しの文脈（主体・範囲）。作った後は変えない（#3253）。

    主体（`user_uuid`）と範囲（`profile_scopes`）は認証と業務 Agent の定義から作り、モデルが渡す
    ツールの引数からは決めない。変えるときは `model_copy(update=...)` で新しい文脈を作る
    （`update` は検証を通らないので、`profile_scopes` は `freeze_profile_scopes` の値を渡す）。
    """

    model_config = ConfigDict(frozen=True)

    approval_id: str | None = None
    trace_id: str | None = None
    # 外部への書き込みの論理操作の ID（#72。`ToolCall.operation_key`）。
    operation_key: str | None = None
    agent_id: str | None = None
    # Run の ID と、Run を作った利用者（共通認証の user_uuid。#233）。RAG / NL2SQL の MCP は
    # この利用者の token で呼ぶ。利用者が無い呼び出しは `mcp.user_required` で失敗し、別の主体に
    # 置き換えない（#3251。サービスの主体で呼んでよい入口は無い）。
    run_id: str | None = None
    user_uuid: str | None = None
    # Run のきっかけ（`run_facts.run_source`。品質評価の Run は `evaluation`）。サービストークンの
    # claim `run_source` に入れ、呼び先の実行履歴が品質評価の実行を区別する（NL2SQL。#1986）。
    # 呼び先は見え方だけに使い、権限・対象範囲の判定には使わない。
    run_source: str | None = None
    # 業務 Agent のデータの範囲（標準の接続 → 使えるプロファイルの ID。#1379）。その接続の
    # サービストークンの claim `profile_ids` に入れ、呼び先でも範囲を強制させる。範囲の無い接続は
    # claim を付けない。
    profile_scopes: Mapping[str, tuple[str, ...]] = Field(
        default_factory=lambda: freeze_profile_scopes(None)
    )

    @field_validator("profile_scopes", mode="after")
    @classmethod
    def _freeze_profile_scopes(
        cls, value: Mapping[str, tuple[str, ...]]
    ) -> Mapping[str, tuple[str, ...]]:
        return freeze_profile_scopes(value)


# handler は文字の結果（JSON）か、画像を伴う結果（`ToolOutputWithImages`。#2650）を返す。
ToolHandler = Callable[[JsonObject, ToolInvocationContext], JsonObject | ToolOutputWithImages]


class ExternalToolError(RuntimeError):
    def __init__(self, code: str, message: str, details: JsonObject | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class ToolPolicy(BaseModel):
    default_mode: str = "approval"
    allow: set[str] = Field(default_factory=set)
    ask: set[str] = Field(default_factory=set)
    deny: set[str] = Field(default_factory=set)

    def decide(self, definition: ToolDefinition) -> ToolPolicyDecision:
        return self.explain(definition)[0]

    def explain(self, definition: ToolDefinition) -> tuple[ToolPolicyDecision, str]:
        """判定と、その理由（#1832。ツール権限の画面に出す）。

        理由: `policy`（ツール権限で個別に指定）/ `standard`（標準の接続で Agent が承認なしと
        決めたツール）/ `read_only`（読み取り専用の宣言。MCP のツールは信頼した接続の
        `readOnlyHint`）/ `untrusted_annotation`（読み取り専用の注釈はあるが、接続が注釈を信頼
        していないので既定の動作）/ `default_mode`（未指定ツールの既定の動作）。
        """
        if definition.name in self.deny:
            return ToolPolicyDecision.DENY, "policy"
        if definition.name in self.ask:
            return ToolPolicyDecision.ASK, "policy"
        if definition.name in self.allow:
            return ToolPolicyDecision.ALLOW, "policy"
        if definition.standard_auto_approve:
            return ToolPolicyDecision.ALLOW, "standard"
        if definition.trusted_read_only:
            return ToolPolicyDecision.ALLOW, "read_only"
        read_only = (
            definition.permission_level == ToolPermissionLevel.READ and not definition.side_effects
        )
        decision = (
            ToolPolicyDecision.DENY if self.default_mode == "deny" else ToolPolicyDecision.ASK
        )
        return decision, "untrusted_annotation" if read_only else "default_mode"


def _tool_result(
    *,
    name: str,
    success: bool,
    started_at: datetime,
    started_monotonic: float,
    output: JsonObject | None = None,
    error: str | None = None,
    error_code: str | None = None,
    error_details: JsonObject | None = None,
    policy_decision: ToolPolicyDecision = ToolPolicyDecision.ALLOW,
    approval_required: bool = False,
    approval_id: str | None = None,
    guardrail_warnings: list[str] | None = None,
    audit_metadata: JsonObject | None = None,
) -> ToolResult:
    completed_at = _now()
    metadata = dict(audit_metadata or {})
    metadata["success"] = success
    if error_code:
        metadata["error_code"] = error_code
    warnings = list(guardrail_warnings or [])
    # 失敗の文・詳細は呼び先の応答・例外の文を含むので、成功の出力と同じ規則で機微情報を伏せてから
    # 結果にする（#2611。モデルへの結果・Run の step・イベント・ログはすべてこの結果を使う）。
    error, details, masked = sanitize_tool_error(error, error_details)
    if masked:
        metadata["error_masked"] = masked
        warnings = sorted({*warnings, *masked})
    return ToolResult(
        name=name,
        success=success,
        output=output,
        error=error,
        error_code=error_code,
        error_details=details,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=max(0, round((perf_counter() - started_monotonic) * 1000)),
        policy_decision=policy_decision,
        approval_required=approval_required,
        approval_id=approval_id,
        guardrail_warnings=warnings,
        audit_metadata=metadata,
    )


def _tool_audit_metadata(
    *,
    definition: ToolDefinition,
    context: ToolInvocationContext,
    force: bool,
) -> JsonObject:
    return {
        "tool_name": definition.name,
        "permission_level": definition.permission_level.value,
        "side_effects": definition.side_effects,
        "timeout_seconds": definition.timeout_seconds,
        "max_retries": definition.max_retries,
        "audit_tags": list(definition.audit_tags),
        "trace_id": context.trace_id,
        "approval_id": context.approval_id,
        "force": force,
        **({"operation_key": context.operation_key} if context.operation_key else {}),
    }


class ToolRegistry:
    """名前 -> schema 化 Tool の registry。"""

    def __init__(self) -> None:
        self._definitions: dict[str, ToolDefinition] = {}
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, definition: ToolDefinition, handler: ToolHandler) -> None:
        self._definitions[definition.name] = definition
        self._handlers[definition.name] = handler

    def names(self) -> list[str]:
        return sorted(self._definitions)

    def definitions(self) -> list[ToolDefinition]:
        return [self._definitions[name] for name in self.names()]

    def get(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)

    def invoke(
        self,
        call: ToolCall,
        *,
        policy: ToolPolicy | None = None,
        context: ToolInvocationContext,
        force: bool = False,
        definition: ToolDefinition | None = None,
        handler: ToolHandler | None = None,
        authorization: str | None = None,
    ) -> ToolResult:
        """ツールを呼ぶ。`definition` / `handler` を渡すと、登録していないツール（MCP 接続の
        ツール。#757）も同じポリシー・ガードレール・監査で呼ぶ。

        境界の順序は「引数の検証 → ポリシー → handler」（#2630）。引数は呼び先の契約
        （`definition.input_schema`）と照合し、合わなければ handler を呼ばない。`force` は「承認」
        （ASK）のツールを実行してよいという呼ぶ側の判断（承認の記録と結び付けた呼び出し・Control
        Plane 自身の読み取り）で、「拒否」（DENY）は `force` でも実行しない。`authorization` は
        その判断の理由（監査に残す。#2610）。`context`（主体・範囲）は入口が必ず作って渡す（利用者
        の無い既定の文脈は作らない。#3251）。
        """
        started_at = _now()
        started_monotonic = perf_counter()
        if definition is None or handler is None:
            definition = self._definitions.get(call.name)
            handler = self._handlers.get(call.name)
        if definition is None or handler is None:
            return _tool_result(
                name=call.name,
                success=False,
                error="unknown tool",
                started_at=started_at,
                started_monotonic=started_monotonic,
                audit_metadata={
                    "tool_name": call.name,
                    "trace_id": context.trace_id,
                    "approval_id": context.approval_id,
                },
            )

        active_policy = policy or ToolPolicy(
            default_mode=get_settings().agent_permission_default_mode
        )
        decision = active_policy.decide(definition)
        audit_metadata = _tool_audit_metadata(
            definition=definition,
            context=context,
            force=force,
        )
        if authorization:
            audit_metadata["authorization"] = authorization
        checked = validate_arguments(definition.input_schema, call.arguments)
        if isinstance(checked, SchemaUnsupported):
            # 検証できない schema は検証済みと扱わない（監査とログに残す）。
            audit_metadata["argument_validation"] = f"unsupported:{checked.reason}"
            logger.warning(
                "tool_argument_schema_unsupported",
                extra={"tool_name": definition.name, "reason": checked.reason},
            )
        elif checked:
            audit_metadata["argument_validation"] = "failed"
            return _tool_result(
                name=call.name,
                success=False,
                error=INVALID_ARGUMENTS_MESSAGE,
                error_code=INVALID_ARGUMENTS_CODE,
                error_details={"argument_errors": [item.as_dict() for item in checked]},
                started_at=started_at,
                started_monotonic=started_monotonic,
                policy_decision=decision,
                audit_metadata=audit_metadata,
            )
        else:
            audit_metadata["argument_validation"] = "passed"
        if decision == ToolPolicyDecision.DENY:
            return _tool_result(
                name=call.name,
                success=False,
                error="tool invocation denied by policy",
                started_at=started_at,
                started_monotonic=started_monotonic,
                policy_decision=decision,
                audit_metadata=audit_metadata,
            )
        if decision == ToolPolicyDecision.ASK and not force:
            return _tool_result(
                name=call.name,
                success=False,
                started_at=started_at,
                started_monotonic=started_monotonic,
                policy_decision=decision,
                approval_required=True,
                error="approval required",
                audit_metadata=audit_metadata,
            )

        try:
            output = handler(
                call.arguments,
                context,
            )

        except ExternalToolError as exc:
            return _tool_result(
                name=call.name,
                success=False,
                error=exc.message,
                error_code=exc.code,
                error_details=exc.details,
                started_at=started_at,
                started_monotonic=started_monotonic,
                policy_decision=decision,
                audit_metadata=audit_metadata,
            )
        except Exception as exc:  # noqa: BLE001 - ツール境界では失敗を結果に正規化する
            return _tool_result(
                name=call.name,
                success=False,
                error=str(exc),
                error_code="tool.unhandled_error",
                started_at=started_at,
                started_monotonic=started_monotonic,
                policy_decision=decision,
                audit_metadata=audit_metadata,
            )
        images: tuple[ToolImage, ...] = ()
        if isinstance(output, ToolOutputWithImages):
            # 画像は文字の結果と分けて持つ（#2650。文字の結果だけを検査・記録する）。
            images = output.images
            output = output.output
        guarded_output, warnings = _guard_tool_output(definition, output)
        result = _tool_result(
            name=call.name,
            success=True,
            output=guarded_output,
            started_at=started_at,
            started_monotonic=started_monotonic,
            policy_decision=decision,
            guardrail_warnings=warnings,
            audit_metadata=audit_metadata,
        )
        return with_tool_result_images(result, images) if images else result


# 外部への書き込みの論理操作の ID を入れる `tools/call` の `params._meta` の key（#72）。
# 引数ではないので、対応しない呼び先は無視する（引数の schema を変えない）。
OPERATION_KEY_META = "production-ready/operation_key"
# 呼び先に届いた後の失敗で、相手で反映されたかが分からないことを表す詳細（#72。
# `error_details["outcome"]`）。書き込みのツール（`unsafe`）の失敗にだけ付ける。
OUTCOME_UNKNOWN = "unknown"

MCP_PROTOCOL_VERSION = "2025-06-18"
_MCP_ACCEPT = "application/json, text/event-stream"
_MCP_METHOD_NOT_FOUND = -32601


class McpRetryKind(StrEnum):
    """MCP の 1 メッセージの再試行の分類（#854）。

    - `protocol`: `initialize`・`notifications/initialized`・`tools/list`（状態を変えない手順）
    - `read_only`: 読み取り専用（`readOnlyHint=true`。注釈を信頼する接続だけ。#2595）のツールの
      `tools/call`
    - `unsafe`: それ以外の `tools/call`（書き込み・副作用があり得る）
    """

    PROTOCOL = "protocol"
    READ_ONLY = "read_only"
    UNSAFE = "unsafe"


# 再試行してよい HTTP status。429 / 503 は受け付ける前に断られた応答なので、どのメッセージでも
# 再試行する。502 / 504 は呼び先で処理が進んでいる可能性があるので、副作用の無いものだけ。
# 500 は手順のメッセージだけ（ツールの 500 は同じ入力で繰り返し失敗することが多い）。
_RETRY_STATUSES: dict[McpRetryKind, frozenset[int]] = {
    McpRetryKind.PROTOCOL: frozenset({429, 500, 502, 503, 504}),
    McpRetryKind.READ_ONLY: frozenset({429, 502, 503, 504}),
    McpRetryKind.UNSAFE: frozenset({429, 503}),
}
# 接続の確立の timeout の上限（秒）。残りの時間がこれより短ければ残りの時間。
_MCP_CONNECT_TIMEOUT_SECONDS = 10.0
# 再試行の待ち: 0.5 秒から 2 倍ずつ、上限 8 秒（後半を jitter）。Retry-After は上限 30 秒まで従う。
_MCP_RETRY_BASE_DELAY_SECONDS = 0.5
_MCP_RETRY_MAX_DELAY_SECONDS = 8.0
_MCP_RETRY_AFTER_MAX_SECONDS = 30.0
# 待った後に残っていてほしい時間（秒）。これより短くなるなら再試行しない。
_MCP_RETRY_MIN_REMAINING_SECONDS = 1.0
# 再試行の待ち（テストは待たない関数に差し替える。AGENTS.md の CI の規約）。
_retry_sleep: Callable[[float], None] = time.sleep
_jitter = secrets.SystemRandom()


def _mcp_backoff_seconds(attempt: int) -> float:
    delay = min(
        _MCP_RETRY_BASE_DELAY_SECONDS * (2.0 ** (attempt - 1)), _MCP_RETRY_MAX_DELAY_SECONDS
    )
    return delay / 2 + _jitter.uniform(0, delay / 2)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """`Retry-After`（秒か HTTP 日付）。無い・読めないときは None。"""
    value = response.headers.get("retry-after", "").strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - _now()).total_seconds()
    return max(0.0, seconds)


class McpSession:
    """1 回のツール呼び出しの間だけ使う MCP（Streamable HTTP の JSON 応答）の client。

    最初に `initialize` → `notifications/initialized` を送り、応答の `Mcp-Session-Id` を以降の
    request に付ける。固定の session id を設定した従来の gateway には `initialize` を送らない。

    期限（#854）: `initialize` から最後の request までの全体を `timeout_seconds`（接続の timeout）で
    区切る。各 POST の timeout は残りの時間で、再試行の待ちも期限に収まるときだけ行う。

    ponytail: セッションはツール呼び出しをまたいで再利用しない（呼び出しごとに initialize する）。
    SSE の応答は最後の JSON だけを読み（途中経過の通知は捨てる）、`nextCursor` の paging はしない。
    """

    def __init__(
        self,
        *,
        url: str,
        headers: dict[str, str],
        timeout_seconds: float,
        max_retries: int,
        service_code: str,
        service_label: str,
        session_id: str | None = None,
        target_name: str | None = None,
    ) -> None:
        self._url = url
        self._headers = dict(headers)
        self._timeout_seconds = timeout_seconds
        self._deadline = monotonic() + max(0.1, timeout_seconds)
        self._max_retries = max(0, max_retries)
        self._service_code = service_code
        self._service_label = service_label
        # 失敗のメッセージで「〜のサービスが起動しているか」に使う名前（RAG・NL2SQL など）。
        self._target_name = target_name or service_label
        self._session_id = session_id
        self._protocol_version = MCP_PROTOCOL_VERSION
        self._initialized = session_id is not None

    def request(
        self,
        method: str,
        params: JsonObject | None,
        *,
        request_id: str,
        idempotent: bool = True,
    ) -> JsonRpcResponse:
        """JSON-RPC の request を送る。`tools/call` は `idempotent`（読み取り専用のツール）で
        送信後の失敗（502 / 504）を再試行するかを決める。ほかのメソッドは手順として扱う。"""
        if not self._initialized:
            self._initialize()
        payload: JsonObject = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        if method != "tools/call":
            kind = McpRetryKind.PROTOCOL
        else:
            kind = McpRetryKind.READ_ONLY if idempotent else McpRetryKind.UNSAFE
        data, _headers = self._post(payload, kind=kind)
        if data is None:
            raise ExternalToolError(
                f"{self._service_code}.missing_result",
                f"{self._service_label}が空の応答を返しました。",
                {"method": method},
            )
        return _mcp_jsonrpc_response(data)

    def _initialize(self) -> None:
        self._initialized = True
        data, headers = self._post(
            {
                "jsonrpc": "2.0",
                "id": f"init_{uuid4().hex}",
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": "production-ready-agent",
                        "version": get_settings().app_version,
                    },
                },
            },
            kind=McpRetryKind.PROTOCOL,
        )
        response = _mcp_jsonrpc_response(data or {})
        if response.error is not None:
            # initialize を持たない従来の JSON-RPC gateway は、そのまま tools/* を呼ぶ。
            if response.error.get("code") == _MCP_METHOD_NOT_FOUND:
                return
            raise ExternalToolError(
                f"{self._service_code}.initialize_failed",
                f"{self._service_label}が MCP の initialize を拒否しました。",
                {"error": response.error},
            )
        session_id = headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id
        version = (response.result or {}).get("protocolVersion")
        if isinstance(version, str) and version:
            self._protocol_version = version
        self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, kind=McpRetryKind.PROTOCOL
        )

    def _request_headers(self) -> dict[str, str]:
        headers = {
            **self._headers,
            **outbound_correlation_headers(),
            "Accept": _MCP_ACCEPT,
            "MCP-Protocol-Version": self._protocol_version,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _post(self, payload: JsonObject, *, kind: McpRetryKind) -> tuple[JsonObject | None, Any]:
        return _post_mcp_message(
            service_code=self._service_code,
            service_label=self._service_label,
            target_name=self._target_name,
            url=self._url,
            payload=payload,
            headers=self._request_headers(),
            timeout_seconds=self._timeout_seconds,
            max_retries=self._max_retries,
            kind=kind,
            deadline=self._deadline,
        )


def _post_mcp_message(
    *,
    service_code: str,
    service_label: str,
    url: str,
    payload: JsonObject,
    headers: dict[str, str],
    timeout_seconds: float,
    max_retries: int,
    kind: McpRetryKind = McpRetryKind.PROTOCOL,
    deadline: float | None = None,
    target_name: str | None = None,
) -> tuple[JsonObject | None, Any]:
    """JSON-RPC の 1 メッセージを POST する。本文のない応答（通知への 202）は None。

    再試行（#854。MCP の意味に合わせる）:
    - 送信前の失敗（接続できない・接続の timeout・pool の待ち）と 429 / 503 は、どの分類でも
      再試行する
    - 送信後の失敗（502 / 504・通信の途中の切断）は `protocol` と `read_only` だけ。500・読み取りの
      timeout は `protocol` だけ（ツールは呼び先で処理が進んでいる可能性がある）
    - 待ちは指数 backoff + jitter、429 / 503 の `Retry-After` に従う。期限（`deadline`）を超える
      待ちはしない

    Run の取消（#2691。`call_abort`）: 打ち切った後は送らず・再試行せず、
    `<service_code>.call_aborted` にする。送信中の接続は、状態を変えない手順と読み取り専用の
    ツール（`unsafe` 以外）だけ切る（書き込みのツールは送った後は結果を待つ）。
    """
    abort = current_call_abort()
    try:
        return _post_mcp_attempts(
            service_code=service_code,
            service_label=service_label,
            url=url,
            payload=payload,
            headers=headers,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            kind=kind,
            deadline=deadline,
            target_name=target_name,
            abort=abort,
        )
    except ExternalToolError as exc:
        if abort is None or not abort.aborted or exc.code == f"{service_code}.call_aborted":
            raise
        # 打ち切りで切った接続の失敗（通信の途中の切断など）は、取消にする。
        raise _call_aborted_error(service_code, service_label, payload, abort) from exc


def _with_trace_meta(payload: JsonObject) -> JsonObject:
    """JSON-RPC の request の `params._meta` に今の span の traceparent を足す（#3226）。

    通知（`id` の無いメッセージ）と、traceparent の無いとき（span の外）はそのまま。`params` が
    object でない request も変えない。呼んだ側の `_meta` の値は残す。
    """
    meta = mcp_trace_meta()
    if not meta or "id" not in payload:
        return payload
    params = payload.get("params", {})
    if not isinstance(params, dict):
        return payload
    existing = params.get("_meta")
    merged = {**existing, **meta} if isinstance(existing, dict) else meta
    return {**payload, "params": {**params, "_meta": merged}}


# 実行中のツール（function tool の名前, 表示名〔MCP の `title`〕）。同じツールの `tools/call` の
# `mcp.request` の span に表示名を付ける（#3468）。
_TOOL_TITLE: ContextVar[tuple[str, str] | None] = ContextVar("agent_tool_title", default=None)
# `mcp.request` の span の、呼ぶツールの表示名の属性（画面の行の名前に使う。#3468）。
MCP_TOOL_TITLE_ATTRIBUTE = "pr.tool.title"


@contextmanager
def tool_title_scope(function_name: str, title: str | None) -> Iterator[None]:
    """この中の同じツールの `tools/call` の `mcp.request` の span に、表示名を付ける（#3468）。

    表示名はツールの span と同じ（Run の `tool_titles`。MCP の `title`）。無ければ何もしない。
    """
    if not title:
        yield
        return
    token = _TOOL_TITLE.set((function_name, title))
    try:
        yield
    finally:
        _TOOL_TITLE.reset(token)


def _current_tool_title(mcp_tool_name: str) -> str | None:
    """実行中のツールの表示名（呼ぶ MCP のツールが、そのツールのときだけ）。"""
    current = _TOOL_TITLE.get()
    if current is None:
        return None
    function_name, title = current
    if mcp_tool_name in (function_name, mcp_base_tool_name(function_name)):
        return title
    return None


def _mcp_request_attributes(payload: JsonObject) -> dict[str, object]:
    """`mcp.request` の span の属性（OpenTelemetry の MCP の semantic conventions。#3272）。

    JSON-RPC の method（`mcp.method.name`）と、`tools/call` なら呼ぶツールの名前
    （`gen_ai.tool.name`）と表示名（`pr.tool.title`。実行中のツールの表示名。#3468）。画面の処理の
    経過は `tools/call` だけを行にし、`initialize`・通知・`tools/list` は出さない。引数・結果は
    入れない。
    """
    method = payload.get("method")
    if not isinstance(method, str) or not method:
        return {}
    attributes: dict[str, object] = {"mcp.method.name": method}
    params = payload.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    if method == "tools/call" and isinstance(name, str) and name:
        attributes["gen_ai.tool.name"] = name
        if title := _current_tool_title(name):
            attributes[MCP_TOOL_TITLE_ATTRIBUTE] = title
    return attributes


def _call_aborted_error(
    service_code: str, service_label: str, payload: JsonObject, abort: CallAbort
) -> ExternalToolError:
    abort.note_interrupted()
    return ExternalToolError(
        f"{service_code}.call_aborted",
        f"実行の取消で、{service_label}の呼び出しを打ち切りました。",
        {"method": payload.get("method"), "reason": abort.reason, "retryable": False},
    )


def _post_mcp_attempts(
    *,
    service_code: str,
    service_label: str,
    url: str,
    payload: JsonObject,
    headers: dict[str, str],
    timeout_seconds: float,
    max_retries: int,
    kind: McpRetryKind,
    deadline: float | None,
    target_name: str | None,
    abort: CallAbort | None,
) -> tuple[JsonObject | None, Any]:
    target = target_name or service_label
    # 送信中に接続を切ってよいか（書き込みのツールは送った後は切らない）。
    trace = abort.httpcore_trace() if abort is not None and kind != McpRetryKind.UNSAFE else None
    request_options: dict[str, Any] = {"extensions": {"trace": trace}} if trace else {}
    end = deadline if deadline is not None else monotonic() + max(0.1, timeout_seconds)
    attempts = max(0, max_retries) + 1
    retry_statuses = _RETRY_STATUSES[kind]
    attempt = 0
    while True:
        attempt += 1
        if abort is not None and abort.aborted:
            raise _call_aborted_error(service_code, service_label, payload, abort)
        remaining = end - monotonic()
        if remaining <= 0:
            raise ExternalToolError(
                f"{service_code}.timeout",
                f"{service_label}が {timeout_seconds:g} 秒以内に応答しませんでした"
                f"（{target} のサービスの負荷や処理の時間を確認してください）。",
                {"attempts": attempt - 1, "retryable": False, "timeout_seconds": timeout_seconds},
            )
        timeout = httpx.Timeout(remaining, connect=min(remaining, _MCP_CONNECT_TIMEOUT_SECONDS))
        retry_after: float | None = None
        try:
            with (
                observed_span(
                    "mcp.request",
                    kind="client",
                    attributes={
                        "pr.attempt": attempt,
                        "http.request.method": "POST",
                        **_mcp_request_attributes(payload),
                    },
                ) as call_span,
                httpx.Client(timeout=timeout, **http_client_options(url)) as client,
            ):
                # traceparent は HTTP header と JSON-RPC の `params._meta`（OpenTelemetry の MCP の
                # 置き場所）の両方に入れる（#3226）。呼び先（RAG / NL2SQL）はサービストークンの要求
                # だけ親にし、呼び先の span がこの `mcp.request` の下につながる。
                call_span.set_call_input(payload)
                response = client.post(
                    url,
                    json=_with_trace_meta(payload),
                    headers={**headers, **capture_trace_context()},
                    **request_options,
                )
                response.raise_for_status()
                response_headers = response.headers
                if response.status_code == 202 or not response.content:
                    call_span.set_call_output(None)
                    return None, response_headers
                data = _response_json_object(
                    response,
                    service_code=service_code,
                    service_label=service_label,
                    attempt=attempt,
                )
                call_span.set_call_output(data)
            return data, response_headers
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            # 呼び先に届いていない（サービスが起動していない・URL が違う・ネットワーク）。
            retryable = True
            error = ExternalToolError(
                f"{service_code}.unreachable",
                f"{service_label}に接続できません（{target} のサービスが起動しているか、"
                "接続の URL が正しいかを確認してください）。",
                {"reason": type(exc).__name__},
            )
        except httpx.TimeoutException as exc:
            retryable = kind == McpRetryKind.PROTOCOL
            error = ExternalToolError(
                f"{service_code}.timeout",
                f"{service_label}が {timeout_seconds:g} 秒以内に応答しませんでした"
                f"（{target} のサービスの負荷や処理の時間を確認してください）。",
                {"reason": type(exc).__name__, "timeout_seconds": timeout_seconds},
            )
            _note_outcome_unknown(error, kind)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            retryable = status_code in retry_statuses
            if status_code in {429, 503}:
                retry_after = _retry_after_seconds(exc.response)
            error = ExternalToolError(
                f"{service_code}.http_error",
                _http_error_message(service_label, target, status_code),
                {"status_code": status_code, "body": _response_text(exc.response)},
            )
            if status_code >= 500 and status_code != 503:
                # 500 / 502 / 504 は呼び先（か途中の gateway）で処理が進んだかが分からない。
                _note_outcome_unknown(error, kind)
        except httpx.RequestError as exc:
            # 送った後に通信が切れた（応答の途中の切断など）。副作用の無いものだけ再試行する。
            retryable = kind != McpRetryKind.UNSAFE
            error = ExternalToolError(
                f"{service_code}.request_error",
                f"{service_label}との通信が途中で切れました（{target} のサービスの状態を"
                "確認してください）。",
                {"reason": type(exc).__name__},
            )
            _note_outcome_unknown(error, kind)
        except ExternalToolError:
            raise
        except ValueError as exc:
            error = ExternalToolError(
                f"{service_code}.invalid_json",
                f"{service_label}の応答が JSON ではありません。",
                {"attempt": attempt},
            )
            _note_outcome_unknown(error, kind)
            raise error from exc
        delay = 0.0
        if retryable and attempt < attempts:
            delay = (
                min(retry_after, _MCP_RETRY_AFTER_MAX_SECONDS)
                if retry_after is not None
                else _mcp_backoff_seconds(attempt)
            )
            if end - monotonic() - delay < _MCP_RETRY_MIN_REMAINING_SECONDS:
                retryable = False
        error.details.update(
            {
                "attempts": attempt,
                "max_retries": max_retries,
                "retryable": retryable and attempt < attempts,
            }
        )
        if not retryable or attempt >= attempts or (abort is not None and abort.aborted):
            raise error
        logger.warning(
            "mcp_request_retry",
            extra={
                "service": target,
                "method": payload.get("method"),
                "error_code": error.code,
                "attempt": attempt,
                "max_retries": max_retries,
                "delay_seconds": round(delay, 2),
            },
        )
        _retry_sleep(delay)


def _note_outcome_unknown(error: ExternalToolError, kind: McpRetryKind) -> None:
    """書き込みのツールの届いた後の失敗に「結果不明」を付ける（#72）。

    相手では反映済みかもしれないので、未実行とは扱わない（Runtime は同じ操作を承認なしに
    送り直さない。`external_writes`）。送信前の失敗・429 / 503・JSON-RPC の誤り・`isError` の
    結果は、相手が受け付けなかった・失敗を返したので付けない。
    """
    if kind == McpRetryKind.UNSAFE:
        error.details["outcome"] = OUTCOME_UNKNOWN


def _http_error_message(service_label: str, target: str, status_code: int) -> str:
    """HTTP のエラーの利用者向けの 1 文（直し方を添える）。"""
    prefix = f"{service_label}が HTTP {status_code} を返しました"
    if status_code in {401, 403}:
        return f"{prefix}（認証・権限の設定を確認してください）。"
    if status_code == 404:
        return f"{prefix}（接続の URL が正しいかを確認してください）。"
    if status_code == 429:
        return f"{prefix}（{target} が混み合っています。時間をおいて再実行してください）。"
    if status_code >= 500:
        return (
            f"{prefix}（{target} のサービスが一時的に利用できない可能性があります。"
            "時間をおいて再実行してください）。"
        )
    return f"{prefix}。"


def _response_json_object(
    response: httpx.Response,
    *,
    service_code: str,
    service_label: str,
    attempt: int,
) -> JsonObject:
    try:
        data = response.json()
    except ValueError:
        data = _stream_json_object(response.text)
    if not isinstance(data, dict):
        raise ExternalToolError(
            f"{service_code}.invalid_json",
            f"{service_label}の応答が JSON の object ではありません。",
            {"attempt": attempt},
        )
    return data


def _stream_json_object(text: str) -> JsonObject:
    candidates: list[JsonObject] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            line = line.removeprefix("data:").strip()
        if not line or line == "[DONE]":
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append(value)
    if not candidates:
        raise ValueError("stream response did not contain a JSON object")
    return candidates[-1]


def _response_text(response: httpx.Response) -> str:
    text = response.text.strip()
    if len(text) > 500:
        return f"{text[:500]}..."
    return text


def _mcp_jsonrpc_response(payload: JsonObject) -> JsonRpcResponse:
    try:
        return JsonRpcResponse.model_validate(payload)
    except ValidationError as exc:
        raise ExternalToolError(
            "external_mcp.invalid_response",
            "MCP の応答が JSON-RPC の形式ではありません（接続の URL が MCP のエンドポイントかを"
            "確認してください）。",
            {"errors": _validation_errors(exc)},
        ) from exc


def _mcp_schema_value(raw_tool: JsonObject, camel_key: str, snake_key: str) -> JsonObject | None:
    value = raw_tool.get(camel_key)
    if not isinstance(value, dict):
        value = raw_tool.get(snake_key)
    return value if isinstance(value, dict) else None


def _validation_errors(exc: ValidationError) -> list[JsonObject]:
    return [
        dict(error)
        for error in exc.errors(include_url=False, include_context=False, include_input=False)
    ]


def _mcp_oauth_bearer_token(
    *,
    token_url: str | None,
    client_id: str | None,
    client_secret: str | None,
    scope: str | None,
    timeout_seconds: float,
) -> str | None:
    if not token_url and not client_id and not client_secret:
        return None
    if not token_url or not client_id or not client_secret:
        raise ExternalToolError(
            "external_mcp.oauth_not_configured",
            "OAuth の資格情報（トークン URL・クライアント ID・クライアントシークレット）が"
            "そろっていません。MCP 接続の認証の設定を確認してください。",
            {
                "token_url_configured": bool(token_url),
                "client_id_configured": bool(client_id),
                "client_secret_configured": bool(client_secret),
            },
        )
    cache_key = _mcp_oauth_cache_key(token_url, client_id, client_secret, scope)
    cached = _mcp_oauth_token_cache.get(cache_key)
    if cached is not None:
        token, expires_at = cached
        if monotonic() < expires_at:
            return token
    token, expires_at = _fetch_mcp_oauth_bearer_token(
        token_url=token_url,
        client_id=client_id,
        client_secret=client_secret,
        scope=scope,
        timeout_seconds=timeout_seconds,
    )
    _mcp_oauth_token_cache[cache_key] = (token, expires_at)
    return token


def _mcp_oauth_cache_key(
    token_url: str,
    client_id: str,
    client_secret: str,
    scope: str | None,
) -> str:
    secret_fingerprint = hashlib.sha256(client_secret.encode("utf-8")).hexdigest()
    return "\n".join([token_url, client_id, scope or "", secret_fingerprint])


def _fetch_mcp_oauth_bearer_token(
    *,
    token_url: str,
    client_id: str,
    client_secret: str,
    scope: str | None,
    timeout_seconds: float,
) -> tuple[str, float]:
    form = {"grant_type": "client_credentials"}
    if scope:
        form["scope"] = scope
    try:
        with httpx.Client(timeout=timeout_seconds, **http_client_options(token_url)) as client:
            response = client.post(
                token_url,
                data=form,
                auth=(client_id, client_secret),
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            data = _response_json_object(
                response,
                service_code="external_mcp",
                service_label="OAuth のトークン URL",
                attempt=1,
            )
    except httpx.TimeoutException as exc:
        raise ExternalToolError(
            "external_mcp.oauth_timeout",
            f"OAuth のトークンの取得が {timeout_seconds:g} 秒以内に終わりませんでした"
            "（トークン URL のサービスの状態を確認してください）。",
            # トークン URL の userinfo・資格情報らしい query は伏せる（#1056）。
            {"token_url": mask_url_credentials(token_url)},
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise ExternalToolError(
            "external_mcp.oauth_http_error",
            _oauth_http_error_message(exc.response.status_code),
            {"status_code": exc.response.status_code, "body": _response_text(exc.response)},
        ) from exc
    except httpx.RequestError as exc:
        raise ExternalToolError(
            "external_mcp.oauth_request_error",
            "OAuth のトークン URL に接続できません（トークン URL が正しいかと、"
            "ネットワークを確認してください）。",
            {"reason": mask_urls_in_text(str(exc))},
        ) from exc
    except ExternalToolError:
        raise
    except ValueError as exc:
        raise ExternalToolError(
            "external_mcp.oauth_invalid_response",
            "OAuth のトークン URL の応答が JSON ではありません"
            "（トークン URL を確認してください）。",
        ) from exc

    access_token = data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ExternalToolError(
            "external_mcp.oauth_invalid_response",
            "OAuth のトークン URL の応答に access_token がありません"
            "（トークン URL と scope を確認してください）。",
        )
    expires_in = data.get("expires_in", 300)
    ttl_seconds = float(expires_in) if isinstance(expires_in, int | float) else 300.0
    expires_at = monotonic() + max(0.0, ttl_seconds - _MCP_OAUTH_TOKEN_SKEW_SECONDS)
    return access_token, expires_at


def _oauth_http_error_message(status_code: int) -> str:
    """OAuth のトークンの取得の HTTP エラーの利用者向けの 1 文（直し方を添える）。"""
    prefix = f"OAuth のトークン URL が HTTP {status_code} を返しました"
    if status_code in {400, 401, 403}:
        return f"{prefix}（クライアント ID・クライアントシークレット・scope を確認してください）。"
    if status_code == 404:
        return f"{prefix}（トークン URL が正しいかを確認してください）。"
    if status_code >= 500:
        return f"{prefix}（認証サービスが一時的に利用できない可能性があります）。"
    return f"{prefix}。"


def _echo_tool(arguments: JsonObject, _context: ToolInvocationContext) -> JsonObject:
    return {"echo": arguments}


_SENSITIVE_KEY_PATTERN = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|credential|authorization|access[_-]?key)",
    re.IGNORECASE,
)
# `api_key=…`・`password: …` に加えて、接頭辞の付いた名前（`access_token`・`client_secret`）と
# JSON の文（`"access_token": "…"`。呼び先の応答の本文）も対象にする。
_INLINE_SECRET_PATTERN = re.compile(
    r"\b([A-Za-z0-9_-]*?(?:password|passwd|secret|token|api[_-]?key|credential))"
    r"(\"?\s*[:=]\s*\"?)([^\s,;\"]+)",
    re.IGNORECASE,
)
_BEARER_TOKEN_PATTERN = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CREDIT_CARD_CANDIDATE_PATTERN = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_PROMPT_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "prompt_injection.ignore_instructions",
        re.compile(r"ignore (all )?(previous|prior) instructions", re.IGNORECASE),
    ),
    (
        "prompt_injection.system_prompt",
        re.compile(r"(system|developer) (prompt|message|instruction)", re.IGNORECASE),
    ),
    (
        "prompt_injection.tool_control",
        re.compile(r"(call|invoke|execute).{0,24}(tool|command|shell)", re.IGNORECASE),
    ),
    (
        "prompt_injection.data_exfiltration",
        re.compile(
            r"(exfiltrate|leak|dump).{0,24}(secret|token|credential|data)",
            re.IGNORECASE,
        ),
    ),
)
_NON_READONLY_SQL_PATTERN = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|merge|create|grant|revoke)\b",
    re.IGNORECASE,
)


def _guard_tool_output(
    definition: ToolDefinition,
    output: JsonObject,
) -> tuple[JsonObject, list[str]]:
    warnings: set[str] = set()
    guarded = _sanitize_value(output, warnings)
    # NL2SQL の SQL は監査用に受け取るだけ（この Runtime では実行しない）。
    if any(
        isinstance(guarded.get(key), str) and _NON_READONLY_SQL_PATTERN.search(guarded[key])
        for key in ("generated_sql", "executable_sql")
    ):
        warnings.add("nl2sql.non_readonly_sql_returned_as_audit_only")

    if warnings:
        metadata = guarded.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        metadata["agent_guardrail_warnings"] = sorted(warnings)
        guarded["metadata"] = metadata
    return guarded, sorted(warnings)


def sanitize_tool_error(
    error: str | None, details: JsonObject | None
) -> tuple[str | None, JsonObject, list[str]]:
    """失敗したツールの文（`error`）と詳細（`error_details`）から機微情報を伏せる（#2611）。

    規則は成功の出力（`_guard_tool_output`）と同じ（機微な名前の項目・文の中の資格情報・Bearer・
    個人情報）に、文の中の URL の資格情報（`mask_urls_in_text`）を足す。入れ子の詳細（呼び先の
    応答の本文・JSON-RPC の error）も対象にする。引数の検証の結果（`argument_errors`。値を含まない
    場所・種類・直し方。#2630）はそのまま残す。返す警告は伏せた種類と項目の名前だけ（元の値は
    含めない）。
    """
    warnings: set[str] = set()
    clean_error = _sanitize_value(error, warnings, error_text=True) if error else error
    clean_details: JsonObject = {}
    for key, item in (details or {}).items():
        if key == "argument_errors":
            clean_details[key] = item
        elif _SENSITIVE_KEY_PATTERN.search(str(key)) and not _is_flag(item):
            clean_details[key] = "***MASKED***"
            warnings.add(f"sensitive_field_masked:{key}")
        else:
            clean_details[key] = _sanitize_value(item, warnings, error_text=True)
    return clean_error, clean_details, sorted(warnings)


def _is_flag(value: Any) -> bool:
    """機微な名前の項目でも伏せない値（有無を表す真偽・空の値。値そのものは資格情報ではない）。"""
    return value is None or isinstance(value, bool)


def _sanitize_value(value: Any, warnings: set[str], *, error_text: bool = False) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if _SENSITIVE_KEY_PATTERN.search(str(key)) and not _is_flag(item):
                sanitized[key] = "***MASKED***"
                warnings.add(f"sensitive_field_masked:{key}")
                continue
            sanitized[key] = _sanitize_value(item, warnings, error_text=error_text)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_value(item, warnings, error_text=error_text) for item in value]
    if isinstance(value, str):
        for code, pattern in _PROMPT_INJECTION_PATTERNS:
            if pattern.search(value):
                warnings.add(code)
        if error_text:
            # 例外の文には接続の URL が入ることがある（userinfo・資格情報らしい query を伏せる）。
            masked_urls = mask_urls_in_text(value)
            if masked_urls != value:
                warnings.add("secret.url_credentials_masked")
            value = masked_urls
        return _sanitize_sensitive_text(value, warnings)
    return value


def sensitive_findings(value: Any, *, field_names: bool = True) -> list[str]:
    """値に含まれる資格情報・個人情報の種類（ツールの出力の検査と同じ規則。#67 の記録の拒否）。

    機微な名前の項目は、文字の値のときだけ数える（`max_output_tokens` のような数の設定は資格情報
    ではない）。`field_names=False` は名前を見ず、値の形（Bearer・URL の資格情報・メールなど）
    だけで数える（Run の記録の承認の理由・実行の token のような、機微な名前の正当な項目を除く）。
    指示の注入の疑い（`prompt_injection.*`）は機微情報ではないので含めない。
    """
    warnings: set[str] = set()

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if (
                    field_names
                    and _SENSITIVE_KEY_PATTERN.search(str(key))
                    and isinstance(child, str)
                    and child
                ):
                    warnings.add(f"sensitive_field:{key}")
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
        elif isinstance(item, str):
            _sanitize_sensitive_text(mask_urls_in_text(item), warnings)
            if mask_urls_in_text(item) != item:
                warnings.add("secret.url_credentials_masked")

    walk(value)
    return sorted(warnings)


def _sanitize_sensitive_text(value: str, warnings: set[str]) -> str:
    sanitized = _INLINE_SECRET_PATTERN.sub(
        lambda match: _mask_inline_secret(match, warnings),
        value,
    )
    sanitized = _BEARER_TOKEN_PATTERN.sub(
        lambda _match: _mask_pattern(
            "***MASKED_BEARER_TOKEN***",
            warnings,
            "secret.bearer_token_masked",
        ),
        sanitized,
    )
    sanitized = _EMAIL_PATTERN.sub(
        lambda _match: _mask_pattern("***MASKED_EMAIL***", warnings, "pii.email_masked"),
        sanitized,
    )
    sanitized = _SSN_PATTERN.sub(
        lambda _match: _mask_pattern("***MASKED_SSN***", warnings, "pii.ssn_masked"),
        sanitized,
    )
    return _CREDIT_CARD_CANDIDATE_PATTERN.sub(
        lambda match: _mask_credit_card(match, warnings),
        sanitized,
    )


def _mask_inline_secret(match: re.Match[str], warnings: set[str]) -> str:
    warnings.add(f"sensitive_inline_masked:{match.group(1).lower()}")
    return f"{match.group(1)}{match.group(2)}***MASKED***"


def _mask_pattern(mask: str, warnings: set[str], warning: str) -> str:
    warnings.add(warning)
    return mask


def _mask_credit_card(match: re.Match[str], warnings: set[str]) -> str:
    value = match.group(0)
    digits = re.sub(r"\D", "", value)
    if 13 <= len(digits) <= 19 and _luhn_valid(digits):
        warnings.add("pii.credit_card_masked")
        return "***MASKED_CREDIT_CARD***"
    return value


def _luhn_valid(digits: str) -> bool:
    total = 0
    parity = len(digits) % 2
    for index, char in enumerate(digits):
        number = int(char)
        if index % 2 == parity:
            number *= 2
            if number > 9:
                number -= 9
        total += number
    return total % 10 == 0


def _mcp_subject(context: ToolInvocationContext, *, code: str) -> str:
    """token の `sub`（Run の利用者・ログイン中の利用者の user_uuid）。利用者が無ければ失敗にする。

    利用者の無い呼び出しを別の主体（サービス利用者など）に黙って置き換えない（#3251。confused
    deputy を防ぐ）。今はサービスの主体で呼んでよい入口は無い。将来要るなら `sub` を替えず、
    呼ばれた側に見える別の主体（RFC 8693 の `act` など）として足す。
    """
    if context.user_uuid:
        return context.user_uuid
    raise ExternalToolError(
        f"{code}.user_required",
        "利用者の無い呼び出しです。RAG / NL2SQL の MCP は、実行の利用者かログイン中の利用者として"
        "だけ呼べます。",
    )


# ---------------------------------------------------------------------------
# MCP 接続（#757）。RAG / NL2SQL / 外部 MCP を同じ client で呼ぶ。
# ---------------------------------------------------------------------------

MCP_TOOL_SEPARATOR = "__"
_FUNCTION_NAME_PATTERN = re.compile(r"[^A-Za-z0-9_-]")
# OpenAI 互換の function tool の名前の上限。
_FUNCTION_NAME_MAX = 64


def mcp_function_name(server_id: str, tool_name: str, *, disambiguate: bool = False) -> str:
    """モデルに渡す function tool の名前（`<接続>__<ツール>`。英数字・`_`・`-`、64 文字以内）。

    `disambiguate=True` は、文字の置き換えで同じ接続の別のツールと同じ名前になるときの名前
    （元の名前の hash を後ろに付ける。#2596。付け方は `_mcp_tools_from_result`）。
    """
    raw = f"{server_id}{MCP_TOOL_SEPARATOR}{tool_name}"
    name = _FUNCTION_NAME_PATTERN.sub("_", raw)
    if len(name) <= _FUNCTION_NAME_MAX and not disambiguate:
        return name
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{name[: _FUNCTION_NAME_MAX - 9]}_{digest}"


def _mcp_function_names(server_id: str, tool_names: list[str]) -> dict[str, str]:
    """1 つの接続のツールの名前 → モデルに渡す名前（別のツールが同じ名前にならないように。#2596）。

    文字の置き換え（`read.item` → `read_item`）で別のツールと同じ名前になるものは、元の名前の
    hash を付けて分ける。置き換えの要らない名前（`read_item`）はそのままにするので、ツール権限・
    Skill の指示・保存済みの承認の名前は変わらない。一覧の順に依らず同じ結果になる。
    """
    plain = {name: mcp_function_name(server_id, name) for name in dict.fromkeys(tool_names)}
    owners: dict[str, set[str]] = {}
    for name, function_name in plain.items():
        owners.setdefault(function_name, set()).add(name)
    resolved: dict[str, str] = {}
    for name, function_name in plain.items():
        changed = function_name != f"{server_id}{MCP_TOOL_SEPARATOR}{name}"
        if changed and len(owners[function_name]) > 1:
            resolved[name] = mcp_function_name(server_id, name, disambiguate=True)
            logger.warning(
                "mcp_tool_function_name_disambiguated",
                extra={
                    "server_id": server_id,
                    "tool_name": name,
                    "function_name": resolved[name],
                },
            )
        else:
            resolved[name] = function_name
    return resolved


def mcp_base_tool_name(function_name: str) -> str:
    """`<接続>__<ツール>` のツールの部分（成果物の種類の判定に使う）。"""
    return function_name.split(MCP_TOOL_SEPARATOR, 1)[-1]


def _mcp_texts(result: JsonObject) -> list[str]:
    content = result.get("content")
    return [
        str(item.get("text"))
        for item in (content if isinstance(content, list) else [])
        if isinstance(item, dict) and item.get("type") == "text" and item.get("text")
    ]


def _serializes(text: str, structured: JsonObject) -> bool:
    """text の content が構造化データを直列化しただけのもの（互換用の text）か。"""
    try:
        return bool(json.loads(text) == structured)
    except ValueError:
        return False


def _for_model(block: JsonObject) -> bool:
    """content の対象にモデル（assistant）が入るか（annotations.audience が無ければ入る）。"""
    annotations = block.get("annotations")
    audience = annotations.get("audience") if isinstance(annotations, dict) else None
    return not isinstance(audience, list) or "assistant" in audience


# 画像の由来として構造化データから写す項目（RAG の `rag_read_source` の契約: `image.sha256` が
# content の画像と同じなら、根拠の ID・文書の版・図の領域 `image_ref`〔page・bbox〕）。
_IMAGE_SOURCE_KEYS = ("evidence_id", "document_id", "chunk_id", "chunk_set_id")


def _structured_image_source(structured: object, sha256: str) -> JsonObject:
    if not isinstance(structured, dict):
        return {}
    meta = structured.get("image")
    if not isinstance(meta, dict) or meta.get("sha256") != sha256:
        return {}
    source: JsonObject = {
        key: structured[key] for key in _IMAGE_SOURCE_KEYS if isinstance(structured.get(key), str)
    }
    region = structured.get("image_ref")
    if isinstance(region, dict):
        source["region"] = dict(region)
    return source


def parse_mcp_tool_result(
    result: JsonObject, *, server_id: str, tool_name: str
) -> ToolOutputWithImages:
    """MCP の `tools/call` の成功の結果を、文字の結果と画像に分ける（#2650）。

    - `structuredContent`（object）があればそれを結果にする。text の content のうち構造化データを
      直列化しただけのもの（互換用の text）は入れず（二重に入れない）、それ以外の説明文は
      `content_text` に残す。構造化データが無ければ text を `content` にする。
    - image の content は `ToolImage` にし、参照（`image_id`・MIME・大きさ・由来）を
      `content_images` に残す。対象が利用者だけ（annotations.audience に assistant が無い）・
      使えない形式・大きすぎる画像はモデルに渡さず、参照に理由を残す。
    - ほかの種類の content（音声・resource など）は渡さず、種類と数を `content_omitted` に残す。
    """
    structured = result.get("structuredContent")
    content = result.get("content")
    texts: list[str] = []
    images: list[ToolImage] = []
    refs: list[JsonObject] = []
    omitted: dict[str, int] = {}
    for index, block in enumerate(content if isinstance(content, list) else []):
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                if isinstance(structured, dict) and _serializes(text, structured):
                    continue
                texts.append(text)
            continue
        if kind != "image":
            name = str(kind or "unknown")
            omitted[name] = omitted.get(name, 0) + 1
            continue
        source: JsonObject = {
            "server_id": server_id,
            "tool_name": tool_name,
            "content_index": index,
        }
        if not _for_model(block):
            refs.append(
                {"mime_type": block.get("mimeType"), "source": source, "model_input": USER_ONLY}
            )
            continue
        decoded = decode_image(block.get("data"), block.get("mimeType"), source=source)
        if isinstance(decoded, ToolImage):
            provenance = _structured_image_source(structured, decoded.sha256)
            if provenance:
                decoded = ToolImage(
                    data=decoded.data, mime_type=decoded.mime_type, source={**source, **provenance}
                )
            images.append(decoded)
            refs.append(decoded.reference())
        else:
            refs.append(decoded)
    output: JsonObject
    if isinstance(structured, dict):
        output = dict(structured)
        if texts:
            output["content_text"] = "\n".join(texts)
    elif texts:
        output = {"content": "\n".join(texts)}
    elif refs:
        output = {}
    else:
        output = {"result": {key: value for key, value in result.items() if key != "content"}}
    if refs:
        output[CONTENT_IMAGES_KEY] = refs
    if omitted:
        output["content_omitted"] = [
            {"type": name, "count": count} for name, count in sorted(omitted.items())
        ]
    return ToolOutputWithImages(output=output, images=tuple(images))


class McpConnectionClient:
    """MCP 接続の client。認証は接続の方式（サービストークン・OAuth・API キー・なし）。

    サービストークンは呼び出しごとに作る短命の token（`sub` = Run の利用者かログイン中の利用者。
    無ければ `mcp.user_required`。`aud` = 接続の audience）。呼び先は `sub` の利用者の権限で実行
    する（#233・#3251）。
    """

    def __init__(
        self,
        config: McpConnectionConfig,
        *,
        context: ToolInvocationContext,
        extra_claims: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        self._context = context
        # サービストークンに足す claim（画面の操作の印 `purpose` など。#1311）。
        self._extra_claims = dict(extra_claims or {})

    @property
    def server_id(self) -> str:
        return self._config.server_id

    def list_tools(self, *, trace_id: str | None = None) -> ExternalMcpToolsData:
        response = self._session().request(
            "tools/list", None, request_id=trace_id or f"mcp_{uuid4().hex}"
        )
        if response.error is not None:
            raise ExternalToolError(
                "mcp.rpc_error",
                f"MCP 接続「{self._label}」が JSON-RPC のエラーを返しました。",
                {"error": response.error, "server_id": self.server_id, "method": "tools/list"},
            )
        return _mcp_tools_from_result(self.server_id, response)

    def call_tool(
        self,
        tool_name: str,
        arguments: JsonObject,
        *,
        idempotent: bool = False,
        trace_id: str | None = None,
        operation_key: str | None = None,
    ) -> JsonObject:
        """ツールを呼び、文字の結果（JSON）を返す（画像は参照だけ。画像も要るときは
        `call_tool_result`）。"""
        return self.call_tool_result(
            tool_name,
            arguments,
            idempotent=idempotent,
            trace_id=trace_id,
            operation_key=operation_key,
        ).output

    def call_tool_result(
        self,
        tool_name: str,
        arguments: JsonObject,
        *,
        idempotent: bool = False,
        trace_id: str | None = None,
        operation_key: str | None = None,
    ) -> ToolOutputWithImages:
        """ツールを呼ぶ。`idempotent=False`（既定。書き込みのツール）は、送信前の失敗と 429 / 503
        だけ再試行する。`idempotent=True`（読み取り専用のツール）は 502 / 504 も再試行する。
        読み取りの timeout はどちらも再試行しない（呼び先で LLM の処理が進んでいる可能性が
        ある）。結果は文字の結果と画像に分ける（`parse_mcp_tool_result`。#2650）。

        `operation_key`（書き込みの論理操作の ID。#72）は `params._meta` で渡す（引数には
        足さない）。再試行も同じ payload を送るので、同じ ID のまま。"""

        params: JsonObject = {"name": tool_name, "arguments": arguments}
        if operation_key:
            params["_meta"] = {OPERATION_KEY_META: operation_key}
        response = self._session().request(
            "tools/call",
            params,
            request_id=trace_id or f"mcp_{uuid4().hex}",
            idempotent=idempotent,
        )
        details: JsonObject = {"server_id": self.server_id, "tool_name": tool_name}
        if response.error is not None:
            raise ExternalToolError(
                "mcp.rpc_error",
                f"MCP 接続「{self._label}」が JSON-RPC のエラーを返しました。",
                {**details, "error": response.error},
            )
        result = response.result or {}
        if result.get("isError") is True:
            structured = result.get("structuredContent")
            body = structured if isinstance(structured, dict) else {}
            message = body.get("message")
            if not isinstance(message, str) or not message:
                texts = _mcp_texts(result)
                message = texts[0] if texts else f"ツール {tool_name} が失敗しました。"
            raise ExternalToolError(
                "mcp.tool_error",
                message,
                {
                    **details,
                    "error_code": body.get("error_code"),
                    "status": body.get("status"),
                    "details": body.get("details"),
                },
            )
        return parse_mcp_tool_result(result, server_id=self.server_id, tool_name=tool_name)

    @property
    def _label(self) -> str:
        return self._config.label or self._config.server_id

    @property
    def _target_name(self) -> str:
        """起動を確かめる呼び先の名前（標準の接続は製品名。#1325）。"""
        if self._config.source == "builtin":
            return PRODUCT_MCP_SERVICE_NAMES.get(self._config.server_id, self._label)
        return self._label

    def _session(self) -> McpSession:
        config = self._config
        if not config.base_url:
            raise ExternalToolError(
                "mcp.not_configured",
                f"MCP 接続「{self._label}」の URL が設定されていません。",
                {"server_id": config.server_id},
            )
        headers: dict[str, str] = {}
        mode = config.effective_auth_mode()
        if mode == "service_token":
            headers["Authorization"] = f"Bearer {self._service_token()}"
        elif mode == "oauth_client_credentials":
            token = _mcp_oauth_bearer_token(
                token_url=config.oauth_token_url,
                client_id=config.oauth_client_id,
                client_secret=config.oauth_client_secret,
                scope=config.oauth_scope,
                timeout_seconds=config.timeout_seconds,
            )
            if token is None:
                raise ExternalToolError(
                    "mcp.oauth_not_configured",
                    f"MCP 接続「{self._label}」の OAuth の資格情報が足りません。",
                    {"server_id": config.server_id},
                )
            headers["Authorization"] = f"Bearer {token}"
        elif mode == "api_key":
            if not config.api_key:
                raise ExternalToolError(
                    "mcp.api_key_not_configured",
                    f"MCP 接続「{self._label}」の API キーが設定されていません。",
                    {"server_id": config.server_id},
                )
            headers["Authorization"] = f"Bearer {config.api_key}"
        return McpSession(
            url=config.base_url.rstrip("/"),
            headers=headers,
            timeout_seconds=config.timeout_seconds,
            max_retries=get_settings().agent_external_mcp_max_retries,
            service_code="mcp",
            service_label=f"MCP 接続「{self._label}」",
            session_id=config.session_id,
            target_name=self._target_name,
        )

    def _service_token(self) -> str:
        subject = _mcp_subject(self._context, code="mcp")
        claims = {
            **self._extra_claims,
            "run_id": self._context.run_id,
            "agent_id": self._context.agent_id,
            # Run のきっかけ（品質評価の Run は `evaluation`。呼び先の実行履歴の種類。#1986）。
            "run_source": self._context.run_source,
        }
        # データの範囲（#1379）は標準の接続（RAG / NL2SQL）にだけ渡す（外部の MCP には送らない）。
        profile_ids = (
            self._context.profile_scopes.get(self._config.server_id)
            if self._config.source == "builtin"
            else None
        )
        try:
            return issue_service_token(
                get_settings().app_service_token_secret,
                subject=subject,
                audience=self._config.audience(),
                issuer="agent",
                claims={key: value for key, value in claims.items() if value},
                profile_ids=profile_ids or None,
            )
        except SecurityApiError as exc:
            raise ExternalToolError(
                "mcp.service_token_not_configured",
                exc.public_message,
                {"server_id": self._config.server_id},
            ) from exc


def _mcp_tools_from_result(server_id: str, response: JsonRpcResponse) -> ExternalMcpToolsData:
    result = response.result or {}
    raw_tools = result.get("tools")
    if not isinstance(raw_tools, list):
        raise ExternalToolError(
            "mcp.invalid_response",
            "MCP の tools/list の結果に tools[] がありません。",
            {"server_id": server_id},
        )
    tools: list[ExternalMcpToolInfo] = []
    valid = [
        raw_tool
        for raw_tool in raw_tools
        if isinstance(raw_tool, dict) and isinstance(raw_tool.get("name"), str) and raw_tool["name"]
    ]
    function_names = _mcp_function_names(server_id, [str(raw_tool["name"]) for raw_tool in valid])
    for raw_tool in valid:
        name = str(raw_tool["name"])
        description = raw_tool.get("description")
        annotations = raw_tool.get("annotations")
        read_only = isinstance(annotations, dict) and annotations.get("readOnlyHint") is True
        idempotent = (
            not read_only
            and isinstance(annotations, dict)
            and annotations.get("idempotentHint") is True
        )
        metadata = raw_tool.get("metadata")
        tools.append(
            ExternalMcpToolInfo(
                name=name,
                title=_mcp_tool_title(raw_tool),
                description=description if isinstance(description, str) else "",
                input_schema=_mcp_schema_value(raw_tool, "inputSchema", "input_schema") or {},
                output_schema=_mcp_schema_value(raw_tool, "outputSchema", "output_schema"),
                server_id=server_id,
                read_only=read_only,
                idempotent=idempotent,
                function_name=function_names[name],
                metadata=metadata if isinstance(metadata, dict) else {},
            )
        )
    next_cursor = result.get("nextCursor")
    return ExternalMcpToolsData(
        tools=tools,
        metadata={
            "server_id": server_id,
            "method": "tools/list",
            "next_cursor": next_cursor if isinstance(next_cursor, str) else None,
        },
    )


def _mcp_tool_title(raw_tool: JsonObject) -> str | None:
    """`tools/list` のツールの表示名（`title`、無ければ `annotations.title`）。無ければ None。"""
    annotations = raw_tool.get("annotations")
    for value in (
        raw_tool.get("title"),
        annotations.get("title") if isinstance(annotations, dict) else None,
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


# 組み込みの接続のツールのうち、結果が実行中のジョブ（`status` が pending / running と
# `job_id`）なら、完了まで続きを取るもの（接続 → ツール → 続きを取るツール。#848）。
_PRODUCT_JOB_FOLLOW_UPS: dict[str, dict[str, str]] = {
    "nl2sql": {"nl2sql_query": "nl2sql_get_job", "nl2sql_get_job": "nl2sql_get_job"},
}
_IN_FLIGHT_JOB_STATUSES = frozenset({"pending", "running"})
# 1 回の続きの取得で呼び先に待ってもらう秒数の上限。Run のキャンセルはこの間隔で確かめる。
_JOB_POLL_WAIT_SECONDS = 20
# 呼び先の待ち（wait_seconds）に足す、HTTP の往復とジョブの読み込みの余裕（秒）。
_JOB_POLL_MARGIN_SECONDS = 15


def _job_follow_up_tool(config: McpConnectionConfig, tool_name: str) -> str | None:
    """完了まで続きを取るツールの名前（組み込みの接続だけ。外部の MCP 接続は対象外）。"""
    if config.source != "builtin":
        return None
    return _PRODUCT_JOB_FOLLOW_UPS.get(config.server_id, {}).get(tool_name)


def _job_wait_budget_seconds() -> float:
    return max(0.0, float(get_settings().agent_nl2sql_job_wait_seconds))


# Run がキャンセル・終了したかの判定（ジョブの完了を待つのをやめる）。Run の保存先を持つ上の層
# （`runtime`）が import のときに差し込む（ツールの層は Run・スキルの module を import しない。
# #3240。`tests/test_skill_tool_boundary.py`）。差し込まれていなければ終わっていない扱い。
_run_finished_check: Callable[[str], bool] | None = None


def set_run_finished_check(check: Callable[[str], bool] | None) -> None:
    """Run が終わったかの判定を差し込む（`runtime` が呼ぶ）。"""
    global _run_finished_check
    _run_finished_check = check


def _run_is_finished(run_id: str | None) -> bool:
    """Run がキャンセル・終了していれば True（ジョブの完了を待つのをやめる）。"""
    if not run_id or _run_finished_check is None:
        return False
    return _run_finished_check(run_id)


def _job_in_flight(result: JsonObject) -> str | None:
    """結果が実行中のジョブなら job_id を返す。"""
    job_id = result.get("job_id")
    if result.get("status") in _IN_FLIGHT_JOB_STATUSES and isinstance(job_id, str) and job_id:
        return job_id
    return None


def wait_for_product_job(
    client: McpConnectionClient,
    follow_up_tool: str,
    result: JsonObject,
    *,
    context: ToolInvocationContext,
    timeout_seconds: float,
) -> JsonObject:
    """実行中のジョブの続きを、完了するか待つ合計の上限まで取る（#848）。

    モデルに「実行中」のまま答えさせず、ツールの呼び出しの回数も消費しない。待つ合計は
    `AGENT_NL2SQL_JOB_WAIT_SECONDS` で上限を持ち、1 回の待ちごとに Run がキャンセル・終了して
    いないかを確かめる。上限を超えた・Run が終わった・続きの取得に失敗したときは、最後の結果を
    そのまま返す（モデルが job_id で続きを取れる）。
    """
    deadline = monotonic() + _job_wait_budget_seconds()
    per_call_limit = max(
        1, min(_JOB_POLL_WAIT_SECONDS, int(timeout_seconds) - _JOB_POLL_MARGIN_SECONDS)
    )
    polls = 0
    while (job_id := _job_in_flight(result)) is not None:
        remaining = deadline - monotonic()
        if remaining < 1 or _run_is_finished(context.run_id):
            break
        polls += 1
        try:
            result = client.call_tool(
                follow_up_tool,
                {"job_id": job_id, "wait_seconds": max(1, min(per_call_limit, int(remaining)))},
                idempotent=True,
                trace_id=f"{context.trace_id}-wait-{polls}" if context.trace_id else None,
            )
        except ExternalToolError as exc:
            logger.warning(
                "mcp_job_wait_failed",
                extra={
                    "run_id": context.run_id,
                    "tool_name": follow_up_tool,
                    "error_code": exc.code,
                },
            )
            break
    return result


# 標準の接続のツールのうち、Agent が既定で承認なしに実行するもの（#1832。ユーザーの決定）。
# `nl2sql_query` は呼び先で SELECT だけを実行し（書き込みの文は監査だけ）、見える範囲は
# 業務プロファイル・利用者の権限で決まるため、既定を「自動実行」にする。
# `readOnlyHint` は false のまま（ジョブを作るので送信後の失敗では再送しない）。
# `nl2sql_cancel_requirement` は承認のまま。
STANDARD_AUTO_APPROVED_TOOLS: dict[str, frozenset[str]] = {
    "nl2sql": frozenset({"nl2sql_query"}),
}


def mcp_tool_definition(config: McpConnectionConfig, tool: ExternalMcpToolInfo) -> ToolDefinition:
    """MCP 接続のツールを Runtime のツール定義にする（承認の要否は readOnlyHint とポリシー）。"""
    schema = dict(tool.input_schema) or {"type": "object", "properties": {}}
    schema.setdefault("type", "object")
    label = config.label or config.server_id
    timeout_seconds = config.timeout_seconds
    if _job_follow_up_tool(config, tool.name) is not None:
        # ジョブの完了を待つ分（#848）。SDK の function tool の timeout はこの値から決まる。
        timeout_seconds += _job_wait_budget_seconds()
    return ToolDefinition(
        name=tool.function_name or mcp_function_name(config.server_id, tool.name),
        title=tool.title,
        description=f"[{label}] {tool.description}".strip(),
        input_schema=schema,
        output_schema=tool.output_schema or {"type": "object"},
        permission_level=ToolPermissionLevel.READ if tool.read_only else ToolPermissionLevel.WRITE,
        side_effects=not tool.read_only,
        timeout_seconds=timeout_seconds,
        max_retries=get_settings().agent_external_mcp_max_retries,
        audit_tags=["mcp", config.server_id],
        annotations_trusted=config.annotations_trusted(),
        standard_auto_approve=config.source == "builtin"
        and tool.name in STANDARD_AUTO_APPROVED_TOOLS.get(config.server_id, frozenset()),
        mcp_server_id=config.server_id,
        mcp_tool_name=tool.name,
        idempotent=tool.idempotent,
    )


def retry_after_delivery_allowed(config: McpConnectionConfig, tool: ExternalMcpToolInfo) -> bool:
    """呼び先に届いた後の失敗（応答が不明）で同じ `tools/call` を送り直してよいか（#2595）。

    読み取り専用の申告（readOnlyHint）を、接続が注釈を信頼するとき（標準の接続か、管理者が信頼を
    明示した接続）だけ根拠にする。ツール権限の個別の許可や利用者の承認は根拠にしない（実行してよい
    ことと、同じ操作を 2 回送ってよいことは別）。送信前の失敗と 429 / 503 は、これに依らず
    再試行する（`_post_mcp_message`）。

    冪等の申告（idempotentHint。#72）も同じ信頼の境界で根拠にする（同じ呼び出しは同じ
    `operation_key` を送るので、呼び先は ID でも重複を判定できる）。
    """
    return (tool.read_only or tool.idempotent) and config.annotations_trusted()


def mcp_tool_handler(config: McpConnectionConfig, tool: ExternalMcpToolInfo) -> ToolHandler:
    """MCP 接続のツールを呼ぶ handler（再試行は `_post_mcp_message`。#854）。

    組み込みの NL2SQL のジョブのツールは、結果が実行中なら完了まで続きを取る（#848）。
    """
    follow_up_tool = _job_follow_up_tool(config, tool.name)

    def handle(
        arguments: JsonObject, context: ToolInvocationContext
    ) -> JsonObject | ToolOutputWithImages:
        client = McpConnectionClient(config, context=context)
        # 送信後の失敗（502 / 504・通信の途中の切断）も再試行するのは、読み取り専用
        # （readOnlyHint）を信頼できる接続のツールだけ（#854・#2595）。注釈を信頼しない接続の
        # 申告だけでは再送しない（承認の既定の判定 `ToolPolicy.explain` と同じ信頼の境界）。
        result = client.call_tool_result(
            tool.name,
            arguments,
            idempotent=retry_after_delivery_allowed(config, tool),
            trace_id=context.trace_id,
            operation_key=context.operation_key,
        )

        if follow_up_tool is None:
            # 画像は文字の結果と分けて返す（#2650。`tool_registry.invoke` が分ける）。
            return result if result.images else result.output
        return wait_for_product_job(
            client,
            follow_up_tool,
            result.output,
            context=context,
            timeout_seconds=config.timeout_seconds,
        )

    return handle


def list_mcp_connection_tools(
    server_id: str, *, context: ToolInvocationContext, trace_id: str | None = None
) -> ExternalMcpToolsData:
    """MCP 接続のツール一覧（呼び先は利用者の権限で絞る）。未登録の接続は KeyError。"""
    config = runtime_config_store.get_mcp(server_id)
    return McpConnectionClient(config, context=context).list_tools(trace_id=trace_id)


class ToolsData(BaseModel):
    tools: list[str]


class ToolDefinitionsData(BaseModel):
    tools: list[ToolDefinition]


tool_registry = ToolRegistry()
tool_registry.register(
    ToolDefinition(
        name="echo",
        description="入力をそのまま返す疎通確認用ツール。",
        input_schema={"type": "object", "additionalProperties": True},
        output_schema={"type": "object", "additionalProperties": True},
        permission_level=ToolPermissionLevel.READ,
        audit_tags=["debug"],
    ),
    _echo_tool,
)
