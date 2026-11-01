"""組み込み Runtime（OpenAI Agents SDK + OpenAI 互換のモデルの API の Responses API。#754）。

Control Plane の中で業務 Agent を実行する。外部の Runtime・Binding・Docker は使わない。

- モデル: システム設定 > モデル のモデルの API（Agent の `model_id`、無ければ既定の
  テキストモデル）。
  SDK の tracing は無効にする（業務データを外部へ送らない）。
- 指示: Agent の指示と、割り当てた Skill の指示を合わせる。指示の中の MCP のツールの素の名前は、
  モデルに渡す名前（`<接続>__<ツール>`）に書き直す。無い名前のツールを呼んだら、呼ぶ名前をモデルへ
  返して呼び直させる（Run は落とさない。#1303）。
- ツール: Skill が必要とするツールを function tool にする（#757）。`control-plane` は
  `tool_registry` のツール、それ以外の server_id は MCP 接続（RAG / NL2SQL / 外部 MCP）の
  `tools/list` のツール（名前は `<接続>__<ツール>`、一覧は Run の利用者の権限で絞られる）。
  実行は `tool_registry.invoke`（ポリシー・ガードレール・監査・サービストークン）を通す。
  ポリシーが「拒否」のツールは渡さない。「承認」のツールは SDK の `needs_approval` で中断する
  （MCP のツールは readOnlyHint が無ければ既定で承認が必要）。
- 承認: 中断した Run は SDK の状態を Run に保存して `waiting_approval` にする。すべての承認が
  決まったら状態を復元し、承認・却下を反映して再開する。無人の Run（品質評価・MCP）は
  承認待ちにせず、承認が要るツールを却下してモデルへ理由を返し、回答まで続ける（#776・#1813）。
- 入力の上限: モデルの呼び出しの直前に、入力（指示・ツールの定義・会話・ツールの結果）を登録モデルの
  入力の上限から出力の上限と余白を引いた予算に収める（#2383。古いツールの結果から縮める。縮め方は
  `context_window`）。収まらなければモデルを呼ばずに理由の分かる失敗にし、プロバイダが入力の超過を
  返したら縮めて 1 回だけ送り直す。
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
import sys
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Never, cast

from agents import (
    Agent,
    AgentHooks,
    FunctionTool,
    MaxTurnsExceeded,
    Model,
    ModelSettings,
    OpenAIResponsesModel,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    Runner,
    RunState,
    set_tracing_disabled,
)
from agents.items import ModelResponse
from agents.run_config import CallModelData, ModelInputData, ToolErrorFormatterArgs
from agents.tool_context import ToolContext
from openai import (
    DEFAULT_MAX_RETRIES,
    APIStatusError,
    AsyncOpenAI,
    BadRequestError,
    DefaultAsyncHttpxClient,
)
from openai.types.shared import Reasoning, ReasoningEffort
from opentelemetry import trace
from pr_backend_core.chat_history import (
    CHAT_HISTORY_UNTRUSTED_NOTE,
    ChatHistoryBudget,
    build_chat_history,
)
from pr_backend_core.chat_progress import ChatProgressStepDetails
from pr_backend_core.llm_request_params_httpx import asend_dropping_parameters
from pr_backend_core.observability.call_stream import CallStreamCapture
from pr_backend_core.observability.genai import record_genai_usage
from pr_backend_core.observability.mcp_trace import traceparent_links
from pr_backend_core.observability.model_purpose import model_purpose_attributes
from pr_backend_core.observability.request_context import bind_log_context
from pr_backend_core.observability.tracing import (
    ObservedSpan,
    SpanListener,
    SpanUpdate,
    attach_trace_context,
    finish_operation_trace,
    observed_span,
    operation_trace,
    record_span,
)
from pr_system_settings.enterprise_ai_errors import (
    EnterpriseAiIncomplete,
    enterprise_ai_incomplete_from_response,
    enterprise_ai_incomplete_message,
)
from pr_system_settings.model import (
    DEFAULT_ENTERPRISE_AI_MAX_INPUT_TOKENS,
    DEFAULT_ENTERPRISE_AI_MAX_OUTPUT_TOKENS,
    EnterpriseAiModelNotRegisteredError,
    enterprise_ai_connection_for_model,
    enterprise_ai_default_model_id,
    enterprise_ai_max_input_tokens,
    enterprise_ai_model_catalog,
    enterprise_ai_output_token_budget,
    enterprise_ai_reasoning_effort,
    lower_reasoning_effort,
    reasoning_effort_to_send,
)

from app.features.agent import (
    call_abort,
    data_scope,
    external_writes,
    run_budget,
    run_claim,
    run_config,
    tool_grounding,
    tool_loop_guard,
)
from app.features.agent.answer_evidence import (
    ANSWER_EVIDENCE_KIND,
    ANSWER_EVIDENCE_NAME,
    answer_evidence,
)
from app.features.agent.answer_identifiers import IdentifierCheck, identifier_index
from app.features.agent.answer_notice import answer_takes_notice, current_answer_notice
from app.features.agent.answer_outcome import answer_outcome
from app.features.agent.answer_passages import is_absence_only, is_clarification_only
from app.features.agent.answer_validation import (
    ANSWER_VALIDATION_KIND,
    ANSWER_VALIDATION_NAME,
    EVIDENCE_TOOLS,
    MAX_ANSWER_CHARS,
    MAX_QUERY_CHARS,
    NO_EVIDENCE_NOTICE,
    REASON_ABSENCE_ONLY,
    REASON_ANSWER_TOO_LONG,
    REASON_ATTACHMENT_EVIDENCE,
    REASON_CLARIFICATION_ONLY,
    REASON_CONNECTION_NOT_FOUND,
    REASON_EMPTY_ANSWER,
    REASON_NO_RAG_EVIDENCE,
    REASON_RAG_DECLINED,
    REASON_UNUSABLE_RESULT,
    REASON_VALIDATION_ERROR,
    REASON_VALIDATION_TIMED_OUT,
    REASON_VALIDATOR_UNAVAILABLE,
    RESULT_TIMED_OUT,
    STATUS_COMPLETED,
    STATUS_SKIPPED,
    STATUS_UNVALIDATED,
    VALIDATE_ANSWER_TOOL,
    carried_evidence,
    carried_evidence_groups,
    combine_validations,
    connection_check_inputs,
    run_evidence_groups,
    usable_result,
    validation_content,
    validation_data_results,
    validation_figure_readings,
    with_no_evidence_notice,
    with_unverified_notice,
    without_lookup_notes,
)
from app.features.agent.chat_progress import (
    APPROVAL_WAIT_SPAN,
    CONFIRMATION_WAIT_SPAN,
    EXECUTE_TOOL_SPAN,
    KIND_APPROVAL,
    KIND_CONFIRMATION,
    approval_rounds,
    approval_wait_display,
    confirmation_wait_display,
    model_turn_kind,
    recorded_wait_rounds,
    tool_attributes,
    tool_details,
    tool_display,
    tool_titles,
)
from app.features.agent.clarification_cancel import (
    CANCEL_METADATA_KEY,
    CANCEL_TRACE_PREFIX,
    CLARIFICATION_KINDS,
    KIND_NL2SQL,
    KIND_RAG,
    NL2SQL_CANCEL_TOOL,
    TRIGGER_BUTTON,
    TRIGGER_MESSAGE,
    button_cancel_kinds,
    cancel_instructions,
    cancel_record,
    declined_kinds,
    is_declined_tool,
    message_cancel_kinds,
    pending_of,
)
from app.features.agent.config import McpConnectionConfig, runtime_config_store
from app.features.agent.confirmation import (
    CONFIRMATION_TOOL,
    POLICY_ASSUME,
    RESPONSE_METADATA_KEY,
    SOURCE_AGENT,
    SOURCE_NL2SQL,
    SOURCE_RAG,
    ConfirmationBudget,
    ConfirmationRequest,
    agent_confirmation_skipped,
    card_answer,
    confirmation_gate,
    confirmation_tool,
    ground_options,
    is_confirmation_payload,
    item_from_arguments,
    items_from_pending,
    nl2sql_context,
    policy_instructions,
    rag_conditions,
    response_instructions,
    used_rounds,
    validation_premises,
)
from app.features.agent.context_window import (
    FALLBACK_PROVIDER_SCALE,
    CompactionResult,
    ContextBudget,
    ContextLimits,
    ProviderOverflow,
    compact_items,
    estimate_tokens,
    overflow_message,
    provider_overflow,
    provider_overflow_message,
    tools_text,
)
from app.features.agent.evaluation_aids import aid_enabled
from app.features.agent.guide_steps import unresolved_guide_steps, unresolved_guide_steps_message
from app.features.agent.job_cancel import (
    NL2SQL_CANCEL_JOB_TOOL,
    RUN_CANCEL_TRACE_PREFIX,
    in_flight_nl2sql_jobs,
)
from app.features.agent.model_call_observation import model_call_attributes
from app.features.agent.output_fields import (
    OUTPUT_RETRY_MESSAGE,
    AgentOutputField,
    output_recorded,
    output_retried,
    record_output_tool,
    without_output_record_sections,
)
from app.features.agent.rag_retrieval_state import retrieval_notice
from app.features.agent.run_claim import RunClaim
from app.features.agent.section_coverage import (
    COVERAGE_INCOMPLETE,
    RETRYABLE_REASONS,
    section_coverage,
    unread_sections_message,
    with_unread_sections_notice,
)
from app.features.agent.skills import (
    EVIDENCE_PLANNING_CALLER,
    reset_pinned_skills,
    resolve_skill,
    skill_registry,
    use_pinned_skills,
)
from app.features.agent.support_task import (
    ADJUSTED_LIMITS_HINT,
    BUDGET_EXCEEDED_CODE,
    DATA_LOOKUP_HINT,
    GUIDE_CHECK_TRACE_PREFIX,
    NL2SQL_NEEDS_CLARIFICATION,
    NL2SQL_QUERY,
    RAG_BUDGET_TOOLS,
    RAG_LOOKUP_GUIDES,
    RAG_READ_DOCUMENT,
    RAG_READ_SECTION,
    RAG_READ_SOURCE,
    RAG_RETRIEVE_EVIDENCE,
    RAG_SEARCH,
    SupportTaskBudget,
    adjusted_search_limits,
    agent_confirmation_answers,
    assume_step,
    budget_exceeded_message,
    build_support_task,
    caller_planned_arguments,
    caller_planned_schema,
    evidence_clarification_step,
    guide_check_note,
    has_support_task_activity,
    is_environment_tool,
    missing_data_lookup,
    nl2sql_clarification_pending,
    nl2sql_next_step,
    nl2sql_skip_step,
    profile_knowledge_summary,
    rag_next_step,
    rag_planned_arguments,
    rag_planned_schema,
    record_codes,
    repeated_query_note,
    search_limit_schema,
    section_read_note,
    superseded_versions_note,
    support_task_instructions,
    text_references,
    tool_clarification_pending,
    unresolved_hops,
    unresolved_hops_message,
)
from app.features.agent.tool_arguments import (
    INVALID_ARGUMENTS_CODE,
    ArgumentError,
    SchemaUnsupported,
    errors_payload,
    hidden_arguments,
    parse_arguments,
    validate_arguments,
)
from app.features.agent.tool_media import (
    model_tool_output,
    plan_image_delivery,
    strip_image_data,
)
from app.features.agent.tools import (
    MCP_TOOL_SEPARATOR,
    ExternalToolError,
    ToolCall,
    ToolDefinition,
    ToolHandler,
    ToolInvocationContext,
    ToolPolicy,
    ToolPolicyDecision,
    ToolResult,
    list_mcp_connection_tools,
    mcp_base_tool_name,
    mcp_function_name,
    mcp_tool_definition,
    mcp_tool_handler,
    tool_registry,
    tool_result_images,
    tool_title_scope,
    with_tool_result_images,
)
from app.features.agent.trace_spans import run_trace_owner, trace_spans
from app.observability import (
    CONTEXT_COMPACTIONS_TOTAL,
    MODEL_INCOMPLETE_RETRIES_TOTAL,
    TOOL_LOOP_STOPS_TOTAL,
    UNDECLARED_TOOL_CALLS_TOTAL,
)
from app.settings import get_settings

logger = logging.getLogger(__name__)
# RAG に渡す利用者の元の質問の文字数の上限（RAG の入力の上限と同じ。#2261）。
_ORIGINAL_QUESTION_MAX_CHARS = 8000

BUILTIN_RUNTIME_ID = "builtin"
SDK_PACKAGE = "openai-agents"
# Skill の requirement のうち、Control Plane のツール（`tool_registry`）を指す server_id。
CONTROL_PLANE_SERVER_ID = "control-plane"
# 業務データを含む trace を外部（OpenAI）へ送らない。
set_tracing_disabled(True)
# モデルの入力の上限に収まらない失敗の code（#2383）。
CONTEXT_OVERFLOW_CODE = "runtime.context_overflow"
# 業務 Agent（SDK の Agent）に付ける、入力を収める上限（`ContextLimits`）の属性の名前。
_CONTEXT_LIMITS_ATTR = "_agent_context_limits"
# 業務 Agent（SDK の Agent）に付ける、指示の変わらない部分（Run の状態を足す前の指示）の属性の
# 名前（#2583。モデルの呼び出しの span の指紋と構成の見積もりに使う）。
_STATIC_INSTRUCTIONS_ATTR = "_agent_static_instructions"
# いまモデルを呼んでいる Run の入力の管理（`_ContextWindow`）。呼び出しの直前に SDK の
# `call_model_input_filter` が入れ、プロバイダの入力の超過の送り直し
# （`_ModelApiResponses.create`）とモデルの呼び出しの span の属性（#2583）が読む。
_ACTIVE_CONTEXT_WINDOW: ContextVar[_ContextWindow | None] = ContextVar(
    "agent_context_window", default=None
)
# いま実行している Run 全体の実行予算（#68。`execute_run`・`resume_run` が入れる）。差し戻し・
# 承認の後の再開の SDK 実行も同じ予算に積む（モデルの呼び出しの前・応答の後・ツールの境界が読む）。
_ACTIVE_RUN_BUDGET: ContextVar[run_budget.RunBudget | None] = ContextVar(
    "agent_run_budget", default=None
)


class BuiltinRuntimeError(RuntimeError):
    """組み込み Runtime の失敗（画面とイベントに出す code と文言）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ModelTarget:
    """Run に使うモデル（API key は持つが、repr・ログには出さない）。"""

    model_id: str
    endpoint: str
    project_id: str
    api_key: str
    # 推論（思考）の量（#1425。システム設定 > モデル の登録モデルの値）。none は送らない。
    reasoning_effort: str = ""
    # 1 回の呼び出しの出力の上限（#1719。システム設定 > モデル の登録モデルの値。既定 32768）。
    max_output_tokens: int = DEFAULT_ENTERPRISE_AI_MAX_OUTPUT_TOKENS
    # 入力の上限（#2383。prompt と応答の合計。システム設定 > モデル の登録モデルの値。
    # 既定 128000）。
    max_input_tokens: int = DEFAULT_ENTERPRISE_AI_MAX_INPUT_TOKENS
    # 画像の入力に対応するか（#2650。システム設定 > モデル の登録モデルの「画像対応」）。対応しない
    # モデルにはツールの結果の画像を添付せず、読めなかったことを結果に書く。
    vision_enabled: bool = False
    # openai SDK の再試行の回数（#2004）。None は SDK の既定。上限つきの並行の実行器
    # （pr_backend_core.bounded_calls）を通す呼び出しは 0 にし、再試行を実行器に任せる。
    sdk_max_retries: int | None = None

    def __repr__(self) -> str:  # pragma: no cover - API key を出さない
        return f"ModelTarget(model_id={self.model_id!r}, endpoint={self.endpoint!r})"


def sdk_version() -> str:
    try:
        return version(SDK_PACKAGE)
    except PackageNotFoundError:  # pragma: no cover - 依存は lock で固定
        return "unknown"


def resolve_model_target(model_id: str = "") -> ModelTarget:
    """Agent のモデル（空なら既定のテキストモデル）と、その接続を決める。"""
    settings = get_settings()
    resolved = (model_id or "").strip() or enterprise_ai_default_model_id(settings)
    if not resolved:
        raise BuiltinRuntimeError(
            "runtime.model_not_configured",
            "使うモデルが決まっていません。"
            "システム設定 > モデル で既定のテキストモデルを設定してください。",
        )
    try:
        connection = enterprise_ai_connection_for_model(settings, resolved)
    except EnterpriseAiModelNotRegisteredError as exc:
        # 業務 Agent が選んだモデルが登録に無い（登録を消したなど）。別のモデル・接続へ暗黙に
        # 切り替えずに失敗させる（#3240 の不変条件 2）。
        raise BuiltinRuntimeError("runtime.model_not_registered", str(exc)) from exc
    if not connection.is_configured():
        raise BuiltinRuntimeError(
            "runtime.model_connection_not_configured",
            "モデルの API の接続（Endpoint URL と API key）が設定されていません。"
            "システム設定 > モデル で設定してください。",
        )
    return ModelTarget(
        model_id=resolved,
        # OpenAI 互換 API の base URL（Endpoint URL を入力どおり。#1440。platform の関数）。
        endpoint=connection.base_url,
        project_id=connection.project_id,
        api_key=connection.api_key,
        reasoning_effort=enterprise_ai_reasoning_effort(settings, resolved),
        # モデルの出力の上限と入力の上限の小さい方（#1719。prompt は会話で伸びるので見積もらない）。
        max_output_tokens=enterprise_ai_output_token_budget(settings, resolved),
        max_input_tokens=enterprise_ai_max_input_tokens(settings, resolved),
        vision_enabled=model_accepts_images(settings, resolved),
    )


def model_accepts_images(settings: Any, model_id: str) -> bool:
    """登録モデルが画像の入力に対応するか（#2650。登録モデルに無い ID は対応しないと扱う）。"""
    normalized = model_id.strip()
    return any(
        model.model_id == normalized and model.vision_enabled
        for model in enterprise_ai_model_catalog(settings)
    )


def model_settings_for(target: ModelTarget) -> ModelSettings:
    """Run・評価の判定に使う SDK の ModelSettings（#1425）。

    - モデルの推論（思考）の量を Responses API の `reasoning: {"effort": ...}` で送る
      （none は送らない。解釈は platform の `reasoning_effort_to_send`）
    - モデルの出力の上限を `max_output_tokens` で送る（#1719。SDK の `max_tokens`。送らないと
      endpoint の既定の上限になり、推論のあるモデルは推論だけで上限に達することがある）
    """
    max_tokens = max(1, int(target.max_output_tokens))
    effort = reasoning_effort_to_send(target.reasoning_effort)
    if not effort:
        return ModelSettings(store=False, max_tokens=max_tokens)
    # 値は共通のモデル設定（pr_system_settings）が SDK と同じ候補に絞って保存している。
    return ModelSettings(
        store=False,
        reasoning=Reasoning(effort=cast(ReasoningEffort, effort)),
        max_tokens=max_tokens,
    )


def _omit_empty_tools(kwargs: dict[str, Any]) -> dict[str, Any]:
    """ツールが無い呼び出しから `tools: []` と `tool_choice` を外す。

    モデルの API によっては、xAI のモデルが空の `tools` を「ツールなしで tool_choice を指定した」
    として 400 で拒否する（gpt-oss は受け付ける。2026-10-02 に実環境で確認）。SDK はツールが
    無くても空の `tools` を送るため、送る前に外す。
    """
    tools = kwargs.get("tools")
    if isinstance(tools, list) and not tools:
        return {
            key: value
            for key, value in kwargs.items()
            if key not in {"tools", "tool_choice", "parallel_tool_calls"}
        }
    return kwargs


def _omit_reasoning_text(kwargs: dict[str, Any]) -> dict[str, Any]:
    """`input` の reasoning item から本文（`content` の `reasoning_text`）を外す（#1215）。

    SDK は前の応答の reasoning item をそのまま次の呼び出しの `input` に入れる。モデルの API の
    `openai.gpt-oss-120b` は、`input` の `reasoning_text` を「reasoning_text is not supported by
    model」の 400 で拒否するため、ツールを呼んだ後の回答が作れない（2026-10-07 に実環境で確認）。
    item（`id`・`summary`）は残す（item ごと除いても回答できることも確認した）。
    """
    items = kwargs.get("input")
    if not isinstance(items, list):
        return kwargs
    changed = False
    sanitized: list[Any] = []
    for item in items:
        data = item if isinstance(item, dict) else None
        if data is None and hasattr(item, "model_dump"):
            data = item.model_dump(exclude_none=True)
        if isinstance(data, dict) and data.get("type") == "reasoning" and "content" in data:
            sanitized.append({key: value for key, value in data.items() if key != "content"})
            changed = True
        else:
            sanitized.append(item)
    return {**kwargs, "input": sanitized} if changed else kwargs


def _adapt_request(kwargs: dict[str, Any]) -> dict[str, Any]:
    """`responses.create` の引数をモデルの API が受け付ける形にする。"""
    return _omit_reasoning_text(_omit_empty_tools(kwargs))


# モデルの API が「再試行してください」と返す、モデルの出力の一時的な失敗
# （400 `model_output_invalid`）を同じ要求で再試行する回数（#1215）。
MODEL_OUTPUT_INVALID_RETRIES = 2
MODEL_OUTPUT_INVALID_CODE = "model_output_invalid"


def model_error_code(exc: BaseException) -> str | None:
    """OpenAI 互換 API のエラーの code（`invalid_value` など。無ければ None）。"""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        nested = body.get("error") if isinstance(body.get("error"), dict) else body
        value = nested.get("code") if isinstance(nested, dict) else None
        if isinstance(value, str) and value:
            return value
    return None


MODEL_OUTPUT_INCOMPLETE_CODE = "runtime.model_output_incomplete"


def _response_incomplete(
    response: Any, request: Mapping[str, Any]
) -> EnterpriseAiIncomplete | None:
    """応答が未完了（`status=incomplete`）なら、理由・上限・使った token（#1719）。

    完了なら None。
    """
    if getattr(response, "status", None) != "incomplete":
        return None
    data = response.model_dump() if hasattr(response, "model_dump") else response
    if not isinstance(data, Mapping):
        return None
    limit = request.get("max_output_tokens")
    return enterprise_ai_incomplete_from_response(
        data, max_output_tokens=limit if isinstance(limit, int) else None
    )


def _request_reasoning(request: Mapping[str, Any]) -> dict[str, Any]:
    """要求の `reasoning`（送っていなければ空）。SDK は `Reasoning` の model で渡す。"""
    reasoning: Any = request.get("reasoning")
    if reasoning is not None and hasattr(reasoning, "model_dump"):
        reasoning = reasoning.model_dump(exclude_none=True)
    return dict(reasoning) if isinstance(reasoning, Mapping) else {}


def _request_reasoning_effort(request: Mapping[str, Any]) -> str:
    """要求の `reasoning.effort`（送っていなければ ""）。"""
    return str(_request_reasoning(request).get("effort") or "")


def _with_lower_reasoning_effort(request: dict[str, Any]) -> dict[str, Any] | None:
    """推論の量を 1 段下げた要求（#2023。high → medium → low）。下げられなければ None。"""
    lower = lower_reasoning_effort(_request_reasoning_effort(request))
    if not lower:
        return None
    return {**request, "reasoning": {**_request_reasoning(request), "effort": lower}}


def _transient_incomplete(incomplete: EnterpriseAiIncomplete) -> bool:
    """出力の上限で未完了でも、使った出力が上限よりはるかに少ない（上限に達したはずがない）か。

    実環境（2026-10-18）で、上限 32768 tokens に対して 61 tokens の出力で `max_output_tokens` の
    未完了が返った（#2355）。呼び先の一時的な不具合として、同じ要求で送り直す。割合は
    `agent_model_incomplete_retry_output_ratio`（0 以下は送り直さない）。
    """
    ratio = float(get_settings().agent_model_incomplete_retry_output_ratio)
    limit, used = incomplete.max_output_tokens, incomplete.output_tokens
    return (
        incomplete.output_limit_reached
        and ratio > 0
        and isinstance(limit, int)
        and limit > 0
        and isinstance(used, int)
        and used < limit * ratio
    )


def _raise_for_incomplete(
    incomplete: EnterpriseAiIncomplete, request: Mapping[str, Any], *, lowered_effort: bool = False
) -> None:
    """未完了の応答を、理由・上限・使った token と直し方の分かる失敗にする（#1719・#2023）。

    SDK は未完了を `ModelBehaviorError`（型名だけが Run の失敗に残る）にするため、その前に
    platform の `enterprise_ai_incomplete_message` の文で失敗させる。送った上限はもうモデルの
    出力の上限（`max_output_tokens`）なので、広げて送り直さない（推論の量を下げる送り直しは
    `_ModelApiResponses.create`）。
    """
    raise BuiltinRuntimeError(
        MODEL_OUTPUT_INCOMPLETE_CODE,
        enterprise_ai_incomplete_message(
            incomplete,
            model_id=str(request.get("model") or ""),
            lowered_effort=lowered_effort,
            reasoning_effort=_request_reasoning_effort(request),
        ),
    )


class _ObservedStream:
    def __init__(self, inner: Any, span: Any) -> None:
        self._inner, self._span = inner, span
        self._exhausted = False
        self._capture = CallStreamCapture(span.set_call_output, enabled=span.call_content_enabled)

    def __aiter__(self) -> Any:
        return self

    async def __anext__(self) -> Any:
        try:
            with trace.use_span(
                self._span.span, record_exception=False, set_status_on_exception=False
            ):
                event = await self._inner.__anext__()
            self._capture.feed(event)
            kind = getattr(event, "type", "")
            if kind == "response.completed":
                # 応答がツールを求めたか・最終の回答かで、処理の経過の工程の種類を決める（#3226）。
                _set_turn_kind(self._span, getattr(event, "response", None))
            if kind in {"response.completed", "response.incomplete", "response.failed"}:
                self._exhausted = True
                if kind != "response.completed":
                    self._span.set_status("failed")
                    self._span.set_attributes(
                        {
                            "error.type": "IncompleteResponse"
                            if kind == "response.incomplete"
                            else "ProviderResponseFailed"
                        }
                    )
            response = getattr(event, "response", None)
            self._span.set_attributes({"gen_ai.response.model": getattr(response, "model", None)})
            usage = getattr(response, "usage", None)
            if usage is not None:
                record_genai_usage(
                    self._span, usage.model_dump() if hasattr(usage, "model_dump") else usage
                )
            return event
        except StopAsyncIteration:
            self._capture.flush(interrupted=not self._exhausted)
            self._exhausted = True
            self._span.finish()
            raise
        except BaseException as exc:
            self._capture.flush(interrupted=True)
            if not self._capture.has_content:
                self._span.set_call_output({"error_type": type(exc).__name__})
            self._span.set_attributes({"error.type": type(exc).__name__})
            self._span.set_status(
                "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            )
            self._exhausted = True
            self._span.finish()
            raise

    async def close(self) -> None:
        if not self._exhausted:
            self._capture.flush(interrupted=True)
            self._span.set_status("cancelled")
        try:
            await self._inner.close()
        finally:
            self._span.finish()

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _set_turn_kind(span: ObservedSpan, response: object) -> None:
    """モデルの呼び出しの span に工程の種類（進め方の検討・回答の作成）を付ける（#3226）。"""
    kind = model_turn_kind(getattr(response, "output", None))
    if kind is not None:
        span.set_display(kind=kind)


def _model_input_attributes(request: Mapping[str, Any]) -> dict[str, int | str]:
    """モデルに送る要求の指紋・構成の見積もり・予算（#2583。本文は含めない）。

    Run の中の呼び出しは Run の入力の管理（`_ContextWindow`）の予算・縮め方・指示の変わらない部分を
    使う。観測は補助なので、作れなくてもモデルは呼ぶ。
    """
    window = _ACTIVE_CONTEXT_WINDOW.get()
    try:
        if window is None:
            return model_call_attributes(request)
        return model_call_attributes(
            request,
            budget=window.budget,
            static_instructions=window.static_instructions,
            provider_limit=window.provider_limit,
            compaction=window.last_input,
        )
    except Exception as exc:  # noqa: BLE001 - 観測は補助。作れなくてもモデルは呼ぶ
        logger.warning(
            "builtin_runtime_model_input_observation_failed",
            extra={"exception_type": type(exc).__name__},
        )
        return {}


class _ObservedResponseManager:
    def __init__(self, inner: Any, request: Mapping[str, Any]) -> None:
        self._inner = inner
        self._request = request
        self._scope = observed_span(
            "gen_ai.chat",
            kind="client",
            attributes={
                "gen_ai.operation.name": "chat",
                **model_purpose_attributes(default="agent.choose_action"),
                "gen_ai.provider.name": "oracle",
                "gen_ai.request.model": str(request.get("model") or ""),
                **_model_input_attributes(request),
            },
        )

    async def __aenter__(self) -> Any:
        span = self._scope.__enter__()
        span.set_call_input(self._request)
        try:
            return await self._inner.__aenter__()
        except BaseException:
            self._scope.__exit__(*sys.exc_info())
            raise

    async def __aexit__(self, *args: Any) -> Any:
        try:
            return await self._inner.__aexit__(*args)
        finally:
            self._scope.__exit__(*args)


class _ModelApiStreamingResponses:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def create(self, **kwargs: Any) -> Any:
        request = _adapt_request(kwargs)
        return _ObservedResponseManager(self._inner.create(**request), request)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _ModelApiResponses:
    """`responses.create` をモデルの API 向けに直し、未完了の応答を扱う。

    ``lower_effort_on_incomplete`` が偽なら、出力の上限で未完了でも送り直さない（推論の量を下げる
    送り直しも、わずかな出力の未完了の送り直し〔#2355〕も。上限つきの並行の実行器
    `pr_backend_core.bounded_calls` を通す呼び出し。送り直しは実行器の ``adaptive`` の再試行に任せ、
    再試行の主体を二重にしない。#2023）。
    """

    def __init__(self, inner: Any, *, lower_effort_on_incomplete: bool = True) -> None:
        self._inner = inner
        self._lower_effort_on_incomplete = lower_effort_on_incomplete

    async def create(self, **kwargs: Any) -> Any:
        request = _adapt_request(kwargs)
        attempt = 0
        lowered_effort = False
        transient_retried = False
        overflow_retried = False
        send_attempt = 0
        while True:
            try:
                send_attempt += 1
                response = await self._send(request, send_attempt)
                incomplete = (
                    None if request.get("stream") else _response_incomplete(response, request)
                )
                if incomplete is None:
                    window = _ACTIVE_CONTEXT_WINDOW.get()
                    if window is not None:
                        window.measure(response)
                    return response
                if (
                    self._lower_effort_on_incomplete
                    and not transient_retried
                    and _transient_incomplete(incomplete)
                ):
                    # わずかな出力の未完了は呼び先の一時的な不具合として、同じ要求で 1 回だけ
                    # 送り直す（#2355）。送り直しも未完了なら下の扱い（推論の量を下げる / 失敗）。
                    transient_retried = True
                    model_name = str(request.get("model") or "")
                    MODEL_INCOMPLETE_RETRIES_TOTAL.labels(model=model_name).inc()
                    logger.warning(
                        "builtin_runtime_model_output_incomplete_retry",
                        extra={
                            "model": model_name,
                            "incomplete_reason": incomplete.reason,
                            "max_output_tokens": incomplete.max_output_tokens,
                            "output_tokens": incomplete.output_tokens,
                            "reasoning_tokens": incomplete.reasoning_tokens,
                        },
                    )
                    continue
                # 出力の上限で未完了なら、推論の量を 1 段下げて 1 回だけ送り直す（#2023。上限は
                # もうモデルの出力の上限なので広げない）。ほかの理由（content_filter など）は
                # 送り直さない。
                lowered = (
                    _with_lower_reasoning_effort(request)
                    if incomplete.output_limit_reached
                    and not lowered_effort
                    and self._lower_effort_on_incomplete
                    else None
                )
                logger.warning(
                    "builtin_runtime_model_output_incomplete",
                    extra={
                        "model": str(request.get("model") or ""),
                        "incomplete_reason": incomplete.reason,
                        "max_output_tokens": incomplete.max_output_tokens,
                        "reasoning_effort": _request_reasoning_effort(request) or "none",
                        "output_tokens": incomplete.output_tokens,
                        "reasoning_tokens": incomplete.reasoning_tokens,
                        "next_action": "lower_effort" if lowered is not None else "fail",
                    },
                )
                if lowered is None:
                    _raise_for_incomplete(incomplete, request, lowered_effort=lowered_effort)
                request, lowered_effort = cast(dict[str, Any], lowered), True
                continue
            except BadRequestError as exc:
                overflow = provider_overflow(model_error_code(exc), _model_error_message(exc))
                window = _ACTIVE_CONTEXT_WINDOW.get()
                if overflow is not None and window is not None:
                    # 入力の超過（#2383）: 返った token 数で見積もりを補正して縮め、1 回だけ
                    # 送り直す。送り直しも超過なら、利用者に分かる失敗にする（400 のままにしない）。
                    if overflow_retried:
                        window.fail_after_retry(overflow)
                    request = window.retry_request(request, overflow)
                    overflow_retried = True
                    continue
                if (
                    model_error_code(exc) != MODEL_OUTPUT_INVALID_CODE
                    or attempt >= MODEL_OUTPUT_INVALID_RETRIES
                ):
                    raise
                attempt += 1
                logger.warning(
                    "builtin_runtime_model_output_invalid_retry",
                    extra={"attempt": attempt, "max_retries": MODEL_OUTPUT_INVALID_RETRIES},
                )

    async def _send(self, request: dict[str, Any], attempt: int) -> Any:
        with observed_span(
            "gen_ai.chat",
            kind="client",
            finish_on_exit=False,
            attributes={
                "gen_ai.operation.name": "chat",
                **model_purpose_attributes(default="agent.choose_action"),
                "gen_ai.provider.name": "oracle",
                "gen_ai.request.model": str(request.get("model") or ""),
                "pr.attempt": attempt,
                **_model_input_attributes(request),
            },
        ) as span:
            span.set_call_input(request)
            response = await self._inner.create(**request)
            if request.get("stream"):
                return _ObservedStream(response, span)
            span.set_call_output(response)
            span.set_attributes({"gen_ai.response.model": getattr(response, "model", None)})
            usage = getattr(response, "usage", None)
            if usage is not None:
                record_genai_usage(
                    span, usage.model_dump() if hasattr(usage, "model_dump") else usage
                )
            if _response_incomplete(response, request) is not None:
                span.set_attributes({"error.type": "IncompleteResponse"})
                span.set_status("failed")
            else:
                # 応答がツールを求めたか・最終の回答かで、処理の経過の工程の種類を決める（#3226）。
                _set_turn_kind(span, response)
            span.finish()
            return response

    @property
    def with_streaming_response(self) -> _ModelApiStreamingResponses:
        return _ModelApiStreamingResponses(self._inner.with_streaming_response)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _ModelApiClient:
    """`responses.create` の引数だけをモデルの API 向けに直し、他はそのまま渡す。"""

    def __init__(self, inner: AsyncOpenAI, *, lower_effort_on_incomplete: bool = True) -> None:
        self._inner = inner
        self._lower_effort_on_incomplete = lower_effort_on_incomplete

    @property
    def responses(self) -> _ModelApiResponses:
        return _ModelApiResponses(
            self._inner.responses, lower_effort_on_incomplete=self._lower_effort_on_incomplete
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class ModelApiResponsesModel(OpenAIResponsesModel):
    """モデルの API（OpenAI 互換）の Responses API 用。

    SDK の `_get_client` を包む（SDK は版を固定する）。
    """

    # 出力の上限で未完了の応答を、推論の量を下げて送り直すか（#2023）。
    lower_effort_on_incomplete: bool = True

    def _get_client(self) -> AsyncOpenAI:
        return cast(
            AsyncOpenAI,
            _ModelApiClient(
                super()._get_client(),
                lower_effort_on_incomplete=self.lower_effort_on_incomplete,
            ),
        )


class _ParameterDroppingHttpClient(DefaultAsyncHttpxClient):
    """SDK の既定（timeout・接続の上限・redirect）の client に、受け付けない引数の扱いを足す。

    モデルが受け付けない引数（temperature など）の 400 は、外して 1 回だけ送り直し、以後この
    モデルには送らない（#3539。判定と記憶は platform の部品）。
    """

    async def send(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        parent = super().send
        return await asend_dropping_parameters(lambda item: parent(item, *args, **kwargs), request)


def enterprise_ai_http_client(timeout: float | None = None) -> DefaultAsyncHttpxClient:
    """Enterprise AI を呼ぶ `AsyncOpenAI` の HTTP の client（`timeout` が無ければ SDK の既定）。"""
    if timeout is None:
        return _ParameterDroppingHttpClient()
    return _ParameterDroppingHttpClient(timeout=timeout)


def _default_model_factory(target: ModelTarget) -> Model:
    headers = {"OpenAI-Project": target.project_id} if target.project_id else None
    client = AsyncOpenAI(
        base_url=target.endpoint,
        api_key=target.api_key,
        default_headers=headers,
        max_retries=(
            DEFAULT_MAX_RETRIES if target.sdk_max_retries is None else target.sdk_max_retries
        ),
        http_client=enterprise_ai_http_client(),
    )
    model = ModelApiResponsesModel(model=target.model_id, openai_client=client)
    # 上限つきの並行の実行器を通す呼び出し（SDK の再試行 0）は、送り直しを実行器に任せる（#2023）。
    model.lower_effort_on_incomplete = target.sdk_max_retries != 0
    return model


# テストは SDK の ScriptedModel を返す関数に差し替える。
model_factory: Callable[[ModelTarget], Model] = _default_model_factory


def _active_policy() -> ToolPolicy:
    config = runtime_config_store.get_tool_policy()
    return ToolPolicy(
        default_mode=config.default_mode,
        allow=set(config.allow),
        ask=set(config.ask),
        deny=set(config.deny),
    )


def agent_tool_names(skill_ids: list[str]) -> list[str]:
    """Agent の Skill が必要とする Control Plane のツール（登録済みのものだけ、重複なし）。"""
    names: list[str] = []
    for skill_id in skill_ids:
        skill = resolve_skill(skill_id)
        if skill is None or not skill.enabled:
            continue
        for requirement in skill.mcp_requirements:
            if requirement.server_id != CONTROL_PLANE_SERVER_ID:
                continue
            for name in requirement.tool_names:
                if name not in names and tool_registry.get(name) is not None:
                    names.append(name)
    return names


def agent_mcp_requirements(skill_ids: list[str]) -> dict[str, set[str] | None]:
    """Agent の Skill が必要とする MCP 接続と、使ってよいツール（None は接続のすべてのツール）。"""
    requirements: dict[str, set[str] | None] = {}
    for skill_id in skill_ids:
        skill = resolve_skill(skill_id)
        if skill is None or not skill.enabled:
            continue
        for requirement in skill.mcp_requirements:
            server_id = requirement.server_id.strip()
            if not server_id or server_id == CONTROL_PLANE_SERVER_ID:
                continue
            if not requirement.tool_names:
                requirements[server_id] = None
                continue
            current = requirements.get(server_id, set())
            if current is None:
                continue
            requirements[server_id] = current | set(requirement.tool_names)
    return requirements


def agent_caller_planned_connections(skill_ids: list[str]) -> frozenset[str]:
    """根拠の取得の検索文をモデルが作ると宣言したスキルの接続（#2651）。

    公開中の版の Run は固定したスキルの中身で決まる（`resolve_skill`。宣言の無い古い版を固定した
    公開版は、今までどおり RAG が検索文を計画する）。同じ接続を使うスキルのどれかが宣言すれば、
    その接続の取得はすべて呼び出し元の検索文にする（1 つの Run で振る舞いを混ぜない）。
    """
    connections: set[str] = set()
    for skill_id in skill_ids:
        skill = resolve_skill(skill_id)
        if skill is None or not skill.enabled:
            continue
        connections.update(
            requirement.server_id.strip()
            for requirement in skill.mcp_requirements
            if requirement.evidence_planning == EVIDENCE_PLANNING_CALLER
        )
    return frozenset(connections)


McpRuntimeTool = tuple[ToolDefinition, ToolHandler]


def discover_mcp_tools(
    skill_ids: list[str], *, context: ToolInvocationContext
) -> tuple[list[McpRuntimeTool], list[str]]:
    """Skill が必要とする MCP 接続のツールを取得する。取得できない接続は飛ばして理由を返す。"""
    tools: list[McpRuntimeTool] = []
    warnings: list[str] = []
    for server_id, allowed in agent_mcp_requirements(skill_ids).items():
        try:
            config = runtime_config_store.get_mcp(server_id)
        except KeyError:
            warnings.append(f"MCP 接続「{server_id}」は登録されていません。")
            continue
        try:
            listed = list_mcp_connection_tools(server_id, context=context)
        except ExternalToolError as exc:
            warnings.append(
                f"MCP 接続「{config.label or server_id}」のツールを取得できません: {exc}"
            )
            continue
        available = {tool.name for tool in listed.tools}
        if not available or (allowed is not None and not set(allowed).issubset(available)):
            warnings.append(
                f"MCP 接続「{config.label or server_id}」の必須ツールを取得できません。"
            )
        for tool in listed.tools:
            if allowed is not None and tool.name not in allowed:
                continue
            tools.append((mcp_tool_definition(config, tool), mcp_tool_handler(config, tool)))
    return tools, warnings


# 指示の中のツールの名前の前後に来ない文字（function tool の名前に使える文字。#1303）。
_TOOL_NAME_BOUNDARY = "A-Za-z0-9_-"


def instruction_tool_names(
    skill_ids: list[str],
    exposed: Collection[str],
    origins: Mapping[tuple[str, str], str] | None = None,
) -> dict[str, str]:
    """指示に書いた MCP のツールの素の名前 → モデルに渡す名前（`<接続>__<ツール>`。#1303）。

    Skill の指示は MCP のツールを接頭辞の無い名前（`rag_search`）で書くが、モデルに渡す
    function tool の名前は `<接続>__<ツール>`（#757）。指示の名前のまま呼ぶと SDK が
    `ModelBehaviorError` で Run を落とすため、指示を Run のツールの名前で書き直す。

    対象は Skill の requirement が名前で宣言したツールのうち、この Run でモデルに渡すもの
    だけ（ポリシーで拒否したもの・一覧に無いものは書き換えない）。同じ素の名前が複数の接続に
    あって 1 つに決まらないものと、素の名前のままのツールがあるものは書き換えない。

    `origins`（モデルに渡した MCP のツールの（接続, 元の名前）→ 名前。`exposed_mcp_origins`）を
    渡すと、名前を出自で引く（衝突を避けて変えた名前も、元のツールに対応させる。#2596）。
    """
    exposed_names = set(exposed)
    candidates: dict[str, set[str]] = {}
    for skill_id in skill_ids:
        skill = resolve_skill(skill_id)
        if skill is None or not skill.enabled:
            continue
        for requirement in skill.mcp_requirements:
            server_id = requirement.server_id.strip()
            if not server_id or server_id == CONTROL_PLANE_SERVER_ID:
                continue
            for tool_name in requirement.tool_names:
                function_name = (
                    origins.get((server_id, tool_name))
                    if origins is not None
                    else mcp_function_name(server_id, tool_name)
                )
                if (
                    function_name is not None
                    and function_name in exposed_names
                    and tool_name not in exposed_names
                ):
                    candidates.setdefault(tool_name, set()).add(function_name)
    return {name: next(iter(found)) for name, found in candidates.items() if len(found) == 1}


def render_tool_names(text: str, names: Mapping[str, str]) -> str:
    """指示の中の素のツールの名前を、モデルに渡す名前に置き換える（語の一部は置き換えない）。"""
    if not names or not text:
        return text
    alternatives = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    pattern = re.compile(
        rf"(?<![{_TOOL_NAME_BOUNDARY}])({alternatives})(?![{_TOOL_NAME_BOUNDARY}])"
    )
    return pattern.sub(lambda match: names[match.group(1)], text)


def compose_instructions(
    agent_instructions: str,
    skill_ids: list[str],
    exposed_tool_names: Collection[str] | None = None,
    origins: Mapping[tuple[str, str], str] | None = None,
) -> str:
    """Agent の指示と Skill の指示（AgentSkills の本文）を 1 つの system の指示にする。

    `exposed_tool_names`（この Run でモデルに渡すツールの名前）を渡すと、指示の中の MCP の
    ツールの素の名前を、モデルが呼ぶ名前（`<接続>__<ツール>`）に書き直す（#1303）。
    """
    exposed = exposed_tool_names or ()
    # 根拠・データのツールを渡す Run は、範囲の内外と根拠の有無をツールで調べた結果で判断させる
    # （#58。調べる前に範囲外と決めて拒答させない）。渡さない Run は調べる手段が無いので、確かめ
    # られない質問・範囲外の質問は答えられないと答える（#1835）。
    refusal = (
        tool_grounding.SEARCH_FIRST_INSTRUCTION
        if tool_grounding.grounding_tools(exposed)
        else "ツールで確かめられないとき・業務の範囲外の質問のときは、一般的な知識で答えず、"
        "確かめられないため答えられないと答えてください。"
    )
    sections = [
        "あなたは業務 Agent です。日本語で、根拠を示して簡潔に回答してください。"
        "ツールの結果に無いことは推測で補わず、分からないと答えてください。"
        # 業務の事実はツールの結果だけで答える（#1835。ツールを呼ばずに作った回答は Control Plane
        # が差し戻し、それでも呼ばなければ拒答にする）。
        "業務の事実（規程・手順・数値・状態・記録など）は、ツールの結果だけで答えてください。"
        + refusal
        + "質問が曖昧で対象が分からないときは、確認の質問をしてください。"
        "複数の資料を根拠にするときは、資料（ファイル）ごとにまとめて答え、"
        "別の資料の内容を交互に混ぜないでください。"
        # 使ったツール・根拠は回答とは別に示す（チャットの根拠の塊・MCP の sources。#2328）。
        "どのプロファイルで調べたか（名前・ID）は回答に書かないでください。"
        "使ったツールと根拠は回答とは別に示します。"
    ]
    if agent_instructions.strip():
        agent_names = instruction_tool_names(skill_ids, exposed, origins)
        sections.append(
            "# 業務の指示\n" + render_tool_names(agent_instructions.strip(), agent_names)
        )
    for skill_id in skill_ids:
        skill = resolve_skill(skill_id)
        if skill is None or not skill.enabled or not skill.instructions.strip():
            continue
        # Skill の指示は、その Skill が宣言した接続のツールの名前で書き直す。
        skill_names = instruction_tool_names([skill_id], exposed, origins)
        sections.append(
            f"# Skill: {skill.name}\n{render_tool_names(skill.instructions.strip(), skill_names)}"
        )
        # 本文へ参照文書を詰めず、パスの索引だけを渡す（progressive disclosure。#862 / #1803）。
        for reference in skill.references:
            sections.append(
                f"参照文書 {reference.path}（skill_id={skill.id}、{reference.characters} 文字）。"
                "必要な部分だけ skill_reference_read で読んでください。"
            )
    return "\n\n".join(sections)


def _strict_schema(schema: dict[str, Any]) -> bool:
    """SDK の strict mode に使えるか（全 property が required・追加 property なし）。"""
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return False
    required = set(schema.get("required") or [])
    return schema.get("additionalProperties") is False and required == set(properties)


# 業務ガイドの照合（#1322）に渡す条件・質問の上限（`rag_lookup_guides` の契約の上限）。
GUIDE_CHECK_MAX_CONDITIONS = 30
GUIDE_CHECK_MAX_QUERY_CHARS = 8000


def _connection_prefix(function_name: str) -> str | None:
    """`<接続>__<ツール>` の接続の部分（MCP 接続のツールでなければ None）。"""
    if MCP_TOOL_SEPARATOR not in function_name:
        return None
    return function_name[: -len(mcp_base_tool_name(function_name)) - len(MCP_TOOL_SEPARATOR)]


def _guide_consulted(steps: list[Any], prefix: str) -> bool:
    """この Run で、その接続の業務ガイドを引いた（`rag_lookup_guides`・`rag_search` が成功）か。"""
    for step in steps:
        call, result = step.tool_call, step.tool_result
        if call is None or result is None or not result.success:
            continue
        if _connection_prefix(call.name) != prefix:
            continue
        if mcp_base_tool_name(call.name) in {RAG_LOOKUP_GUIDES, RAG_SEARCH}:
            return True
    return False


def guide_check_query(goal: str, query: object) -> str:
    """業務ガイドの照合の質問（利用者の質問と、モデルが根拠を集めた質問。同じなら 1 つ）。

    条件の語（例: 検証用アカウント → 個別）は利用者の質問に、業務ガイドの語はモデルの質問に出る
    ことがあるので両方を渡す。
    """
    parts = [" ".join(goal.split())]
    if isinstance(query, str) and " ".join(query.split()) not in parts:
        parts.append(" ".join(query.split()))
    return "\n".join(part for part in parts if part)[:GUIDE_CHECK_MAX_QUERY_CHARS]


def support_task_goal(goal: str, previous: Mapping[str, Any] | None) -> str:
    """業務ガイドの照合の質問に使う利用者の質問（#1322）。

    前の Run で問いを確かめ中なら、利用者の新しい発言は問いへの答え（「個別の利用者に付けます」）
    なので、前の質問（目的）も含める（業務ガイドの語は前の質問に出る）。
    """
    if isinstance(previous, Mapping) and previous.get("pending_clarifications"):
        before = previous.get("goal")
        if isinstance(before, str) and before.strip() and before.strip() != goal.strip():
            return f"{before.strip()}\n{goal}"
    return goal


def support_task_known_conditions(previous: Mapping[str, Any] | None) -> dict[str, str]:
    """前の Run までに分かった条件（条件の id → 値）。"""
    known = previous.get("known_conditions") if isinstance(previous, Mapping) else None
    if not isinstance(known, Mapping):
        return {}
    conditions: dict[str, str] = {}
    for key, entry in known.items():
        value = entry.get("value") if isinstance(entry, Mapping) else None
        if isinstance(key, str) and isinstance(value, str) and value:
            conditions[key] = value
    return conditions


def _run_tool_context(run: Any) -> ToolInvocationContext:
    """Run の利用者として MCP を呼ぶ文脈（きっかけは呼び先の実行履歴の種類。#1986）。"""
    # run_facts は runtime を import するため、ここで読む（循環の import を避ける）。
    from app.features.agent.run_facts import run_source

    return ToolInvocationContext(
        run_id=run.id,
        agent_id=run.agent_id,
        user_uuid=run.created_by_user_uuid,
        run_source=run_source(run.metadata),
    )


# 呼び先の誤りの code として返す形（英数字と `_` `.` `-`。#2498）。
_CALLEE_ERROR_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
# モデルへ返す引数の違反の件数と、違反の場所・文の文字数の上限（#2498）。
MAX_ARGUMENT_VIOLATIONS = 5
_MAX_VIOLATION_LOCATION_CHARS = 100
_MAX_VIOLATION_MESSAGE_CHARS = 300


def tool_failure_payload(result: ToolResult) -> dict[str, Any]:
    """失敗したツールの結果としてモデルへ返す内容（#2498）。

    `error`・`error_code` に加えて、MCP の呼び先が返した誤りの code（`tool_error_code`）と、
    引数の検証の違反（`argument_errors`。場所 `argument` と文 `message`。最大
    `MAX_ARGUMENT_VIOLATIONS` 件、文字数に上限）を足す。モデルが違反の内容を読んで引数を直して
    呼び直せるようにする（一般の文だけでは同じ誤った呼び出しを繰り返す）。違反は呼び先が返した
    場所と文だけを使い、型の名前・元の文・入力の値・接続の情報は渡さない。
    """
    payload: dict[str, Any] = {
        "error": result.error or "tool failed",
        "error_code": result.error_code,
    }
    details = result.error_details if isinstance(result.error_details, dict) else {}
    if external_writes.is_outcome_unknown(result):
        # 届いた後の失敗（#72）: 未実行と扱わせず、繰り返さずに確かめる案内を足す。
        operation = details.get("operation_key") or result.audit_metadata.get("operation_key")
        key = operation if isinstance(operation, str) else None
        payload.update(external_writes.unknown_outcome_payload(key))

    callee_code = details.get("error_code")
    if isinstance(callee_code, str) and _CALLEE_ERROR_CODE.fullmatch(callee_code):
        payload["tool_error_code"] = callee_code
    checked = details.get("argument_errors")
    if result.error_code == INVALID_ARGUMENTS_CODE and isinstance(checked, list):
        # 実行の境界の引数の検証（#2630）。値を含まない場所・種類・直し方だけを返す。
        payload["argument_errors"] = [
            {
                "argument": str(item.get("argument") or "")[:_MAX_VIOLATION_LOCATION_CHARS],
                "code": str(item.get("code") or "")[:_MAX_VIOLATION_LOCATION_CHARS],
                "message": str(item.get("message") or "")[:_MAX_VIOLATION_MESSAGE_CHARS],
            }
            for item in checked[:MAX_ARGUMENT_VIOLATIONS]
            if isinstance(item, dict)
        ]
        return payload
    inner = details.get("details")
    errors = inner.get("errors") if isinstance(inner, dict) else None
    violations: list[dict[str, str]] = []
    for item in errors if isinstance(errors, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("message"), str):
            continue
        message = " ".join(item["message"].split())[:_MAX_VIOLATION_MESSAGE_CHARS]
        if not message:
            continue
        location = item.get("loc")
        if isinstance(location, list | tuple):
            location = ".".join(str(part) for part in location)
        location = " ".join(location.split()) if isinstance(location, str) else ""
        violations.append(
            {"argument": location[:_MAX_VIOLATION_LOCATION_CHARS], "message": message}
        )
        if len(violations) >= MAX_ARGUMENT_VIOLATIONS:
            break
    if violations:
        payload["argument_errors"] = violations
    return payload


# 取消が確定した Run のツール・モデルの呼び出し（#2609）。
RUN_CANCELLED_CODE = "runtime.run_cancelled"
RUN_CANCELLED_MESSAGE = "実行は取り消されました。このツールは実行していません。"
# 実行の直前に「承認」が要るツールになったが、この呼び出しの承認の記録が無い（#2610）。
APPROVAL_REQUIRED_CODE = "tool.approval_required"
APPROVAL_REQUIRED_MESSAGE = (
    "このツールは実行の前に承認が必要になったため、実行していません。"
    "必要なら、同じ引数でもう一度呼ぶと承認を求めます。"
)
# 同じ応答で回答前の確認（`ask_user_confirmation`）を出したため、実行しなかった呼び出し（#3352）。
CONFIRMATION_PENDING_CODE = "tool.confirmation_pending"
CONFIRMATION_PENDING_MESSAGE = (
    "利用者に回答前の確認を出したため、このツールは実行していません。"
    "確認の答えを受け取った後に、必要なら呼び直してください。"
)


class RunCancelledError(BuiltinRuntimeError):
    """取消が確定した Run で、モデルの呼び出しを始めずに SDK の実行を止める（#2609）。"""

    def __init__(self) -> None:
        super().__init__(RUN_CANCELLED_CODE, "実行は取り消されました。")


@dataclass(frozen=True)
class _ToolEntry:
    """モデルに渡したツール（呼び出しの境界で使う。#2630）。

    `definition` は呼び先の契約（`tool_registry.invoke` が引数を確かめる schema）、
    `visible_schema` はモデルに見せた schema（SDK の strict mode の変換の後）、`hidden` は呼び先の
    契約にあってモデルに見せていない引数（実行環境が埋める・上書きする）。`handler` は
    登録済みのツールなら None（`tool_registry` の handler で呼ぶ）。
    """

    definition: ToolDefinition
    handler: ToolHandler | None
    visible_schema: Mapping[str, Any]
    hidden: frozenset[str]


@dataclass(frozen=True)
class _PreparedArguments:
    """モデルの引数を確かめた結果（`unsupported` は schema で確かめられなかった理由）。"""

    arguments: dict[str, Any]
    adjusted_limits: dict[str, dict[str, int]]
    errors: list[ArgumentError]
    unsupported: str | None


@dataclass(frozen=True)
class _Authorization:
    """呼び先へ送る直前のツール権限・承認の判断（#2610。`tool_registry.invoke` に渡す）。

    `reason`: policy（自動実行）/ approval（承認の記録と結び付けた呼び出し）/ confirmation
    （回答前の確認）/ control_plane（Control Plane 自身の読み取り）/ approval_required
    （承認が無い）。
    """

    policy: ToolPolicy
    force: bool
    reason: str
    approval_id: str | None = None
    refused: bool = False


class _ToolRecorder:
    """function tool の実行を Run の step・イベント・成果物に記録する。

    `budget` があれば、呼ぶ前に支援タスクの予算を数え、超える呼び出しは実行せずに
    `budget_exceeded` の結果を返す（#1243）。

    `environment_tools` はこの Run でモデルに渡した、現場のデータを確かめる道具（RAG 以外の MCP
    接続のツール）。`rag_search` が回答を確定できないと返したら、モデルへの結果に次の手
    （`next_step`）を足す（#1283。記録する step の結果は RAG の結果のまま）。

    モデルが自分で `rag_lookup_guides` を呼んだときも、最上位の業務ガイドの判断から次の手
    （`next_step`。答える判断なら資料の根拠を集めてから答える）を結果に足す（#1322）。

    業務ガイドの照合（#1322）: モデルが業務ガイド（`rag_lookup_guides`・`rag_search`）を引かずに
    `rag_retrieve_evidence` で根拠を集めたら、Control Plane が同じ接続の `rag_lookup_guides` を
    1 回呼び（step に残す。予算には数えない）、最上位の業務ガイドと次の手（条件が分からなければ
    確認の質問）を根拠の結果に `guide_check` として足す。根拠を集める道具は業務ガイドの条件・分岐を
    見ないため、確かめる条件があってもモデルが分岐ごとに答えてしまう（#1317 の実環境の評価）。
    `guide_tools` はモデルに渡した `rag_lookup_guides`（接続の接頭辞 → 定義・handler）、
    `goal` は利用者の質問（前の Run で問いを確かめ中なら、その質問も含む）、`known_conditions` は
    前の Run までに分かった条件。
    """

    def __init__(self, run_id: str, budget: SupportTaskBudget | None = None) -> None:
        self.run_id = run_id
        self.budget = budget
        self.environment_tools: list[str] = []
        self.guide_tools: dict[str, tuple[ToolDefinition, ToolHandler | None]] = {}
        self.goal = ""
        self.known_conditions: dict[str, str] = {}
        # 業務 Agent のデータの範囲（#1378。接続 → 使えるプロファイル）。空は範囲なし。
        self.data_scopes: data_scope.AgentDataScopes = {}
        # 根拠の取得の検索文をモデルが作ると宣言したスキルの接続（#2651。`agent_caller_planned_
        # connections`）。この接続の `rag_retrieve_evidence` だけに planning=caller を付ける。
        self.caller_planned_connections: frozenset[str] = frozenset()
        # 照合した（する）接続。並列の呼び出しでも 1 回だけにする。
        self._guide_checked: set[str] = set()
        # 回答前の確認（#1839）。利用者に確認しない Run では、RAG の確認の次の手を「前提を示して
        # 答える」に変える。
        self.confirmation: ConfirmationBudget | None = None
        # モデルに渡したツール（名前 → 定義・handler・モデルに見せた schema。#2630）。
        self.entries: dict[str, _ToolEntry] = {}
        # 進展の無い呼び出しが上限に達した（#2580。以降の呼び出しは実行しない）。
        self.loop_stop: tool_loop_guard.LoopFinding | None = None
        # モデルが画像の入力に対応するか（#2650。対応しなければ結果の画像を添付しない）。
        self.image_input = False
        # 回答前の確認（#3352）: 確認の呼び出しで中断するか（呼び出し ID → 判断。応答ごとに 1 回
        # 決める）と、その判断を返す関数（`_approval_gate` が置く）。
        self.confirmation_decisions: dict[str, bool] = {}
        self.decide_confirmation: Callable[[Any, dict[str, Any], str], Awaitable[bool]] | None = (
            None
        )
        # 同じ応答で確認を出したため、実行しない呼び出しの ID（確認の答えを待つ間は実行しない）。
        self.held_calls: set[str] = set()
        # スキルが宣言したが、この Run ではモデルに渡さなかったツール（名前 → 理由。#3240）。
        # 渡していない名前の呼び出しの理由（`classify_undeclared_call`）に使う。
        self.excluded: dict[str, str] = {}

    async def hold_for_confirmation(self, context: Any, output: Sequence[Any]) -> None:
        """モデルの応答に回答前の確認があれば、ツールを実行する前に中断するかを決める（#3352）。

        確認で中断する応答では、同じ応答のほかのツールの呼び出しを実行しない（`held_calls`。
        確認の答えを待つ間に資料・業務データを調べて回答を進めない）。利用者への確認は Run の
        状態を変える中断（LangGraph の interrupt と同じ）で、確認の答えの後にモデルが続きを決める。
        SDK は同じ応答の呼び出しを並列に実行するので、中断の判断はここで先に決め、
        `needs_approval` はその判断を返す（実行の順で判断が変わらない）。
        """
        decide = self.decide_confirmation
        if decide is None:
            return
        calls = [
            item
            for item in output
            if getattr(item, "type", None) == "function_call"
            and isinstance(getattr(item, "call_id", None), str)
        ]
        interrupts = False
        for item in calls:
            if item.name != CONFIRMATION_TOOL:
                continue
            parsed, _errors = parse_arguments(getattr(item, "arguments", None))
            interrupts = await decide(context, parsed or {}, item.call_id) or interrupts
        if not interrupts:
            return
        held = {item.call_id for item in calls if item.name != CONFIRMATION_TOOL}
        if held:
            self.held_calls.update(held)
            logger.info(
                "builtin_runtime_tool_calls_held_for_confirmation",
                extra={"run_id": self.run_id, "held_calls": len(held)},
            )

    def held_result(self, name: str, call_id: str, arguments: str) -> str:
        """回答前の確認を出した応答のほかの呼び出し（呼び先へ送らない。#3352）。"""
        parsed, _errors = parse_arguments(arguments)
        record_skipped_tool(
            self.run_id, ToolCall(name=name, arguments=parsed or {}, trace_id=call_id)
        )
        return json.dumps(
            {"error": CONFIRMATION_PENDING_MESSAGE, "error_code": CONFIRMATION_PENDING_CODE},
            ensure_ascii=False,
        )

    def _skips_rag_clarification(self, name: str, definition: ToolDefinition | None) -> bool:
        if (
            definition is None
            or self.confirmation is None
            or not self.confirmation.skips_tool_clarification
            or mcp_base_tool_name(name) not in {RAG_SEARCH, RAG_RETRIEVE_EVIDENCE}
        ):
            return False
        properties = (definition.input_schema or {}).get("properties")
        # 確認を持たない RAG（引数の無い版）には送らない（未定義の引数は拒否される）。根拠の取得の
        # ルールの確認は、呼び出し元が検索文を作る取得（#2651）でだけ出る。
        return isinstance(properties, dict) and "skip_clarification" in properties

    def _original_question(self, name: str, definition: ToolDefinition | None) -> str | None:
        """RAG の根拠を集めるツールに渡す利用者の元の質問（Run の目標。#2261。渡さなければ None）。

        モデルは検索文を短いキーワードに言い換えることがあり、回答の要求の単位（番号つきの小問。
        RAG の `rag_search` は要求ごとの根拠の確認と回答をこの質問で行う。#2665）や、検索・回答
        プロファイルのルールと用語の照合の手がかりが落ちる。モデルに任せず決定的に付ける。
        引数を持たない RAG（古い版）には送らない。
        """
        if (
            definition is None
            or mcp_base_tool_name(name) not in {RAG_SEARCH, RAG_RETRIEVE_EVIDENCE}
            or not self.goal.strip()
        ):
            return None
        properties = (definition.input_schema or {}).get("properties")
        if not isinstance(properties, dict) or "original_question" not in properties:
            return None
        return self.goal.strip()[:_ORIGINAL_QUESTION_MAX_CHARS]

    def _without_asking(self, next_step: dict[str, Any] | None) -> dict[str, Any] | None:
        if self.confirmation is None or self.confirmation.asks:
            return next_step
        return assume_step(next_step)

    def caller_planned(self, name: str) -> bool:
        """この呼び出しの接続を、スキルが検索文を作る取得（planning=caller）と宣言したか。"""
        connection, separator, _tool = name.partition(MCP_TOOL_SEPARATOR)
        return bool(separator) and connection in self.caller_planned_connections

    def add_tool(self, entry: _ToolEntry) -> None:
        """この Run でモデルに渡すツール（呼び出しの境界で使う定義と、モデルに見せた schema）。"""
        self.entries[entry.definition.name] = entry

    def prepare_arguments(self, name: str, parsed: Mapping[str, Any]) -> _PreparedArguments:
        """モデルが渡した引数を、モデルに見せた schema で確かめる（#2630）。

        検索の件数（top_k・evidence_limit）は、確かめる前に既定・上限に直す（#1351・#1403。
        意図した変換。直した値は schema の範囲に入る）。実行環境が決める非表示の引数（データの範囲が
        1 つのときのプロファイルなど）は確かめない（呼び先の契約は `tool_registry.invoke` が
        確かめる）。
        """
        entry = self.entries.get(name)
        arguments: dict[str, Any] = dict(parsed)
        adjusted_limits: dict[str, dict[str, int]] = {}
        if entry is None:
            return _PreparedArguments(arguments, adjusted_limits, [], None)
        if entry.handler is not None and mcp_base_tool_name(name) in RAG_BUDGET_TOOLS:
            adjusted_limits = adjusted_search_limits(arguments, entry.definition.input_schema)
            if adjusted_limits:
                arguments = {
                    **arguments,
                    **{key: value["sent"] for key, value in adjusted_limits.items()},
                }
        checked = validate_arguments(entry.visible_schema, arguments, ignore=entry.hidden)
        if isinstance(checked, SchemaUnsupported):
            logger.warning(
                "builtin_runtime_tool_schema_unsupported",
                extra={"run_id": self.run_id, "tool_name": name, "reason": checked.reason},
            )
            return _PreparedArguments(arguments, adjusted_limits, [], checked.reason)
        return _PreparedArguments(arguments, adjusted_limits, checked, None)

    def needs_approval(self, name: str, parsed: Mapping[str, Any]) -> bool:
        """実行の直前の最新のツール権限で、この呼び出しに承認が要るか（#2610。SDK の
        `needs_approval`）。

        引数が schema に合わない呼び出しは承認に回さない（実行の境界で引数の誤りとして返す。
        #2630。誤った引数の承認を人に求めない）。
        """
        entry = self.entries.get(name)
        if entry is None:
            return True
        if self.prepare_arguments(name, parsed).errors:
            return False
        # 結果不明の書き込みと同じ操作は、自動実行のツールでも人の照合を求める（#72。承認の記録と
        # 同じく、データの範囲を当てた引数で照合する）。
        scoped = _scoped_call(ToolCall(name=name, arguments=dict(parsed)), self.data_scopes)
        if self.unknown_origin(name, entry.definition, scoped.arguments) is not None:
            return True
        return _active_policy().decide(entry.definition) == ToolPolicyDecision.ASK

    def unknown_origin(
        self, name: str, contract: ToolDefinition | None, arguments: dict[str, Any]
    ) -> Any | None:
        """同じツール・同じ引数の、結果不明のまま解消していない書き込みの step（#72）。"""
        if contract is None or name == CONFIRMATION_TOOL or not external_writes.guarded(contract):
            return None
        return external_writes.unresolved_unknown(_run_steps(self.run_id), name, arguments)

    def authorize(self, call: ToolCall, model_arguments: Mapping[str, Any]) -> _Authorization:
        """ツール権限と承認を、呼び先へ送る直前の最新のポリシーで確かめる（#2610）。

        - 「承認」（ASK）は、この Run・呼び出し ID・ツールの承認済みの記録があり、承認した引数
          （データの範囲を当てたもの）と今の引数が同じときだけ実行する。SDK を経由したことだけでは
          実行しない（構築の後に「自動実行」から「承認」に変わった呼び出しを素通しさせない）。
        - 「拒否」（DENY）は `tool_registry.invoke` が実行しない。
        - 回答前の確認（`ask_user_confirmation`）は承認ではなく確認の往復で止める（#1839）。
        """
        from app.features.agent.runtime import runtime_repository

        policy = _active_policy()
        entry = self.entries.get(call.name)
        contract = entry.definition if entry is not None else tool_registry.get(call.name)
        if call.name == CONFIRMATION_TOOL:
            return _Authorization(policy, force=True, reason="confirmation")
        resend = self.unknown_origin(call.name, contract, call.arguments) is not None
        if contract is None or (policy.decide(contract) != ToolPolicyDecision.ASK and not resend):
            return _Authorization(policy, force=False, reason="policy")
        approval = runtime_repository.approved_builtin_call(
            self.run_id, call.trace_id or "", call.name
        )
        expected = _scoped_call(
            ToolCall(name=call.name, arguments=dict(model_arguments), trace_id=call.trace_id),
            self.data_scopes,
        ).arguments
        if approval is not None and approval.tool_call.arguments == expected:
            return _Authorization(policy, force=True, reason="approval", approval_id=approval.id)
        logger.info(
            "builtin_runtime_tool_approval_missing",
            extra={
                "run_id": self.run_id,
                "tool_name": call.name,
                "call_id": call.trace_id,
                "approval_found": approval is not None,
            },
        )
        return _Authorization(policy, force=False, reason="approval_required", refused=True)

    def cancelled_result(self, name: str) -> str:
        """取消が確定した Run のツールの呼び出し（step を作らず、呼び先へ送らない。#2609）。"""
        logger.info(
            "builtin_runtime_tool_call_rejected",
            extra={"run_id": self.run_id, "tool_name": name, "error_code": RUN_CANCELLED_CODE},
        )
        return json.dumps(
            {"error": RUN_CANCELLED_MESSAGE, "error_code": RUN_CANCELLED_CODE}, ensure_ascii=False
        )

    def loop_stop_finding(self) -> tool_loop_guard.LoopFinding | None:
        """進展の無い呼び出しが上限に達したか（#2580。保存した step から判定する）。"""
        if self.loop_stop is None:
            self.loop_stop = tool_loop_guard.stop_finding(_run_steps(self.run_id))
        return self.loop_stop

    def loop_refused(self, name: str, finding: tool_loop_guard.LoopFinding) -> str:
        """上限に達した後の呼び出し（step を作らず、呼び先へ送らない。#2580）。"""
        logger.info(
            "builtin_runtime_tool_call_rejected",
            extra={"run_id": self.run_id, "tool_name": name, "error_code": finding.code},
        )
        return json.dumps(finding.refusal(), ensure_ascii=False)

    def with_loop_notice(self, payload: dict[str, Any], step_id: str) -> dict[str, Any]:
        """進展の無い呼び出しが上限の 1 回前なら、次も同じなら終えることを足す（#2580）。"""
        finding = tool_loop_guard.step_finding(_run_steps(self.run_id), step_id)
        if finding is None:
            return payload
        if finding.stopped:
            self.loop_stop = self.loop_stop or finding
            return payload
        if finding.warns:
            return {**payload, "loop_notice": finding.notice()}
        return payload

    async def invoke(self, name: str, arguments: str, call_id: str) -> str | list[Any]:
        """モデルが呼んだツールを、境界の順（取消 → 引数 → ツール権限・承認 → handler）で実行する。

        通常の実行・承認の再開・MCP 接続のツール・登録済みのツールで同じ境界を通る（#2609・#2610・
        #2630）。どの段で止めても呼び先へは送らない。
        """
        from app.features.agent.runtime import RunNotActiveError, runtime_repository

        entry = self.entries.get(name)
        definition = entry.definition if entry is not None and entry.handler is not None else None
        handler = entry.handler if entry is not None else None
        # 1. 取消: 取消が確定した Run では step を作らず、呼び先へ送らない（#2609）。
        if await asyncio.to_thread(runtime_repository.builtin_run_ended, self.run_id):
            return self.cancelled_result(name)
        if call_id and call_id in self.held_calls:
            # 同じ応答で回答前の確認を出した（#3352）。確認の答えを待つ間は実行しない。
            return self.held_result(name, call_id, arguments)
        # 進展の無い呼び出しが上限に達した Run では、同じ応答の残りの呼び出しも実行しない（#2580。
        # Run は次のモデルの呼び出しの前に失敗にする）。
        stop = await asyncio.to_thread(self.loop_stop_finding)
        if stop is not None:
            # 上限で実行しなかった呼び出しも処理の経過に残す（step は作らない。#3226）。
            record_skipped_tool(self.run_id, ToolCall(name=name, arguments={}, trace_id=call_id))
            return self.loop_refused(name, stop)
        # Run 全体の実行予算に達した後は、外部の操作を始めない（#68。step は作らず未実行に残す）。
        budget = _ACTIVE_RUN_BUDGET.get()
        exceeded = budget.reserve_tool_call() if budget is not None else None
        if exceeded is not None:
            record_skipped_tool(self.run_id, ToolCall(name=name, arguments={}, trace_id=call_id))
            logger.info(
                "builtin_runtime_tool_call_rejected",
                extra={
                    "run_id": self.run_id,
                    "tool_name": name,
                    "error_code": run_budget.RUN_BUDGET_EXCEEDED_CODE,
                },
            )
            return json.dumps(run_budget.tool_refusal(exceeded), ensure_ascii=False)
        # 2. 引数: 読めない JSON・object 以外を空の引数にせず、モデルに見せた schema で確かめる
        # （#2630）。
        parsed, parse_errors = parse_arguments(arguments)
        if parsed is None:
            return self.reject_call(
                ToolCall(name=name, arguments={}, trace_id=call_id), errors_payload(parse_errors)
            )
        prepared = self.prepare_arguments(name, parsed)
        call_arguments = prepared.arguments
        adjusted_limits = prepared.adjusted_limits
        if prepared.errors:
            return self.reject_call(
                ToolCall(name=name, arguments=call_arguments, trace_id=call_id),
                errors_payload(prepared.errors),
            )
        if adjusted_limits:
            # 既定より小さい件数（top_k・evidence_limit）は既定に引き上げ、上限を超える値は上限に
            # 丸めた（#1351・#1403）。step には送った値を残し、モデルへの結果に直したことを足す。
            logger.info(
                "builtin_runtime_search_limits_adjusted",
                extra={
                    "run_id": self.run_id,
                    "tool_name": name,
                    "adjusted_limits": adjusted_limits,
                },
            )
        if definition is not None and data_scope.takes_profile(name):
            # 確認で選んだ候補の名前をプロファイルの ID に引き直す（#2230）。
            call_arguments, resolved = data_scope.resolve_profile_name(
                name, call_arguments, list(runtime_repository.get_run(self.run_id).steps)
            )
            if resolved is not None:
                logger.info(
                    "builtin_runtime_profile_name_resolved",
                    extra={
                        "run_id": self.run_id,
                        "tool_name": name,
                        "profile_argument": resolved["argument"],
                        "profile_id": resolved["id"],
                    },
                )
        # データの範囲（#1378）: プロファイルを埋める・上書きする・範囲外を拒否する。MCP 接続の
        # ツールだけ（Control Plane のツールは範囲の対象ではない）。
        scoped = (
            data_scope.enforce(name, call_arguments, self.data_scopes)
            if definition is not None
            else None
        )
        if scoped is not None:
            call_arguments = scoped.arguments
        if self._skips_rag_clarification(name, definition):
            # 利用者に確認しない Run は、RAG にルールの回答前の確認を出させない（#2117。モデルに
            # 任せず決定的に付ける）。
            call_arguments = {**call_arguments, "skip_clarification": True}
        original_question = self._original_question(name, definition)
        if original_question is not None and not call_arguments.get("original_question"):
            call_arguments = {**call_arguments, "original_question": original_question}
        if definition is not None and self.caller_planned(name):
            # スキルが宣言したら、根拠の取得はモデルが作った検索文で RAG に推論を重ねさせずに
            # 検索する（#2651。モデルに任せず決定的に付け、モデルが渡した値は上書きする）。
            call_arguments = {
                **call_arguments,
                **caller_planned_arguments(
                    name, definition.input_schema, run_id=self.run_id, call_id=call_id
                ),
            }
        elif definition is not None:
            # 宣言の無い接続・Agent 主体の取得を許さない Run（#2676）: 検索文を作る主体は実行環境
            # だけが決める。モデルが caller の引数を渡しても送らず、RAG の既定（rag）で検索する。
            call_arguments = rag_planned_arguments(name, definition.input_schema, call_arguments)
        call = ToolCall(
            name=name,
            arguments=call_arguments,
            trace_id=call_id,
            data_scope=scoped.note if scoped is not None else None,
        )
        if scoped is not None and scoped.error is not None:
            # 範囲外は呼び先へ送らない（予算にも数えない）。step には拒否を残す。
            return self.reject_out_of_scope(call, scoped)
        contract = entry.definition if entry is not None else tool_registry.get(name)
        if contract is not None and name != CONFIRMATION_TOOL and external_writes.guarded(contract):
            # 同じ call ID の書き込みを既に送った（同じ承認の再開など。#72）: 送り直さない。
            sent = external_writes.sent_step(_run_steps(self.run_id), call_id)
            if sent is not None:
                return self.already_sent_result(sent)
        # 3. ツール権限・承認: 実行の直前の最新のポリシーで確かめる（#2610）。
        authorization = self.authorize(call, parsed)
        if authorization.refused:
            return self.reject_call(
                call,
                {
                    "error": APPROVAL_REQUIRED_MESSAGE,
                    "error_code": APPROVAL_REQUIRED_CODE,
                },
                policy_decision=ToolPolicyDecision.ASK,
                approval_required=True,
            )
        # 予算は await の前に数える（同じ応答の並列の呼び出しでも上限を超えない）。
        exceeded = self.budget.reserve(name) if self.budget is not None else None
        if (
            exceeded is None
            and contract is not None
            and name != CONFIRMATION_TOOL
            and external_writes.has_side_effects(contract)
        ):
            call = call.model_copy(update={"operation_key": self.operation_key_for(call, contract)})
        try:
            step_id, context = runtime_repository.start_builtin_tool_step(self.run_id, call)
        except RunNotActiveError:
            # 取消が判定の後に確定した（同じ lock で判定する。#2609）。
            return self.cancelled_result(name)
        if exceeded is not None:
            message = budget_exceeded_message(name, exceeded)
            exceeded_result = ToolResult(
                name=name,
                success=False,
                error=message,
                error_code=BUDGET_EXCEEDED_CODE,
                error_details={"budget": exceeded},
            )
            runtime_repository.finish_builtin_tool_step(self.run_id, step_id, exceeded_result)
            # 予算の上限で実行しなかった呼び出し（#3226）。
            record_skipped_tool(self.run_id, call, step_id=step_id, result=exceeded_result)
            return json.dumps(
                {"error": message, "error_code": BUDGET_EXCEEDED_CODE, "budget": exceeded},
                ensure_ascii=False,
            )
        if call.operation_key and contract is not None and external_writes.guarded(contract):
            # 送る前に、実行を始めた記録（step と ID）を保存先に書き切る（#72）。書けなければ
            # 送らない（落ちた後に、送ったかもしれない書き込みを見分けられなくなるため）。
            try:
                await asyncio.to_thread(runtime_repository.flush_writes)
            except Exception:  # noqa: BLE001 - 保存先の失敗の種類に依らず送らない
                logger.warning(
                    "builtin_runtime_tool_intent_not_persisted",
                    extra={"run_id": self.run_id, "tool_name": name},
                    exc_info=True,
                )
                not_sent = ToolResult(
                    name=name,
                    success=False,
                    error=external_writes.INTENT_NOT_PERSISTED_MESSAGE,
                    error_code=external_writes.INTENT_NOT_PERSISTED_CODE,
                )
                runtime_repository.finish_builtin_tool_step(self.run_id, step_id, not_sent)
                return json.dumps(tool_failure_payload(not_sent), ensure_ascii=False)
        base = mcp_base_tool_name(name)
        guide_note: dict[str, Any] | None = None

        async def check_guides(result: ToolResult) -> None:
            # 根拠の取得の結果に足す業務ガイドの照合（#1322）は、根拠の取得の一部なので、その
            # ツールの span の中で呼ぶ（処理の経過で根拠の取得の行の子になる。#3471）。
            nonlocal guide_note
            if (
                result.success
                and base == RAG_RETRIEVE_EVIDENCE
                and isinstance(result.output or {}, dict)
            ):
                guide_note = await self.check_guides(name, call.arguments)

        # 4. handler（呼び先へ送る。ツールの span は `execute` が測る）。
        result = await self.execute(
            step_id,
            call,
            context,
            definition=definition,
            handler=handler,
            scoped=scoped,
            authorization=authorization,
            follow_up=check_guides,
        )
        if result.success:
            output = result.output or {}
            if base == RAG_SEARCH and isinstance(output, dict):
                next_step = self._without_asking(rag_next_step(output, self.environment_tools))
                if next_step is not None:
                    output = {**output, "next_step": next_step}
            if base == NL2SQL_QUERY and isinstance(output, dict):
                # SQL の生成前の確認の項目（#1441）: SQL を作る前に、利用者に 1 問ずつ聞かせる。
                # 利用者が「確認せずに回答」を選んだ Run（#1903）は、聞かずに skip_clarification で
                # 呼び直させる。
                if self.confirmation is not None and self.confirmation.skipped:
                    next_step = nl2sql_skip_step(output, call.arguments, tool_name=name)
                else:
                    # 利用者がこの Run で既に答えた内容で答えられるなら、聞き直させない（#2223）。
                    next_step = nl2sql_next_step(
                        output,
                        call.arguments,
                        tool_name=name,
                        user_answers=agent_confirmation_answers(
                            list(runtime_repository.get_run(self.run_id).steps)
                        )
                        if output.get("status") == NL2SQL_NEEDS_CLARIFICATION
                        else (),
                    )
                if next_step is not None:
                    output = {**output, "next_step": next_step}
            if base == RAG_RETRIEVE_EVIDENCE and isinstance(output, dict):
                # 検索・回答プロファイルのルールの確認（#2651。検索文を作る取得だけが返す）。
                next_step = self._without_asking(evidence_clarification_step(output))
                if next_step is not None:
                    output = {**output, "next_step": next_step}
                knowledge = profile_knowledge_summary(output)
                if knowledge is not None:
                    # 検索・回答プロファイルのルール・用語は結果のまま（根拠と分けた項目で）モデルへ
                    # 渡す。渡したことを記録する（#2701）。
                    logger.info(
                        "builtin_runtime_profile_knowledge_passed",
                        extra={"run_id": self.run_id, "tool_name": name, **knowledge},
                    )
                if guide_note is not None:
                    output = {**output, "guide_check": guide_note}
            if base == RAG_LOOKUP_GUIDES and isinstance(output, dict):
                # モデルが自分で引いた業務ガイドにも次の手を足す（業務ガイドだけで答えさせない）。
                note = guide_check_note(
                    output,
                    evidence_gathered=False,
                    rag_calls_remaining=self.budget.rag_calls_remaining
                    if self.budget is not None
                    else None,
                )
                if note is not None:
                    output = {**output, "next_step": self._without_asking(note["next_step"])}
            if base in RAG_BUDGET_TOOLS and isinstance(output, dict):
                # 根拠 0 件が資料の不在を意味しない（安全チェックの未完了・補助の処理の縮退）とき
                # の案内（#3546）。
                notice = retrieval_notice(output)
                if notice is not None:
                    output = {**output, "retrieval_notice": notice}
            if isinstance(output, dict):
                output = self.hop_hints(
                    call, output, step_id=step_id, adjusted_limits=adjusted_limits
                )
                output = self.with_loop_notice(output, step_id)
            # 結果の画像は SDK の画像の出力で渡す（#2650。`deliver_images` が決めたもの）。
            return model_tool_output(
                json.dumps(output, ensure_ascii=False, default=str), tool_result_images(result)
            )
        return json.dumps(
            self.with_loop_notice(tool_failure_payload(result), step_id), ensure_ascii=False
        )

    def operation_key_for(self, call: ToolCall, contract: ToolDefinition) -> str:
        """送る呼び出しの論理操作の ID（#72）。結果不明の操作の送り直しは、最初の ID を引き継ぐ。"""
        origin = self.unknown_origin(call.name, contract, call.arguments)
        if origin is not None:
            return str(origin.tool_call.operation_key)
        from app.features.agent.runtime import runtime_repository

        return external_writes.operation_key(
            run_id=self.run_id,
            user_uuid=runtime_repository.get_run(self.run_id).created_by_user_uuid,
            tool_name=call.name,
            arguments=call.arguments,
            call_id=call.trace_id or "",
        )

    def already_sent_result(self, step: Any) -> str:
        """同じ call ID で既に送った書き込み（#72）。記録した結果を返し、呼び先へは送らない。"""
        logger.info(
            "builtin_runtime_tool_call_already_sent",
            extra={"run_id": self.run_id, "tool_name": step.tool_call.name, "step_id": step.id},
        )
        result = step.tool_result
        if result is not None and result.success:
            return json.dumps(
                {**(result.output or {}), "already_executed": True},
                ensure_ascii=False,
                default=str,
            )
        unknown = result if result is not None else external_writes.interrupted_result(step)
        return json.dumps(tool_failure_payload(unknown), ensure_ascii=False)

    def reject_out_of_scope(self, call: ToolCall, scoped: data_scope.ScopeEnforcement) -> str:
        """範囲の外のプロファイルの呼び出しを、呼び先へ送らずにツールのエラーにする（#1378）。"""
        return self.reject_call(call, scoped.error_payload())

    def reject_call(
        self,
        call: ToolCall,
        payload: dict[str, Any],
        *,
        policy_decision: ToolPolicyDecision = ToolPolicyDecision.ALLOW,
        approval_required: bool = False,
    ) -> str:
        """呼び先へ送らずにツールのエラーにする（step に拒否を残し、モデルへ案内を返す）。"""
        from app.features.agent.runtime import RunNotActiveError, runtime_repository

        error_code = str(payload.get("error_code") or "")
        try:
            step_id, _context = runtime_repository.start_builtin_tool_step(self.run_id, call)
        except RunNotActiveError:
            return self.cancelled_result(call.name)
        rejected = ToolResult(
            name=call.name,
            success=False,
            error=str(payload.get("error") or ""),
            error_code=error_code,
            error_details={key: value for key, value in payload.items() if key != "error"},
            policy_decision=policy_decision,
            approval_required=approval_required,
        )
        runtime_repository.finish_builtin_tool_step(self.run_id, step_id, rejected)
        # 呼び先へ送らなかった呼び出しも、実行しなかったツールとして処理の経過に残す（#3226）。
        record_skipped_tool(self.run_id, call, step_id=step_id, result=rejected)
        logger.info(
            "builtin_runtime_tool_call_rejected",
            extra={"run_id": self.run_id, "tool_name": call.name, "error_code": error_code},
        )
        return json.dumps(self.with_loop_notice(payload, step_id), ensure_ascii=False)

    def hop_hints(
        self,
        call: ToolCall,
        output: dict[str, Any],
        *,
        step_id: str,
        adjusted_limits: dict[str, dict[str, int]] | None = None,
    ) -> dict[str, Any]:
        """多段の質問の案内をモデルへの結果に足す（#1345。記録する step の結果は RAG のまま）。

        根拠を集める・読むツールの本文に同じ文書の別の箇所への参照があれば読む先（`references`）を、
        この Run で同じ（ほぼ同じ）query の検索を繰り返したら繰り返しを止める案内
        （`repeated_query`。#1351）を、query の実体に当たる台帳・一覧の行に略号・区分のような
        短い値があればその意味を引く次の段の案内（`record_codes`。#1365）を、Runtime が件数
        （`top_k`・`evidence_limit`）を既定に引き上げた・上限に丸めたらその値と次から省略する案内
        （`adjusted_limits`。#1403）を、質問が名指しする年度・版が当たった文書の旧版に当たり、
        旧版を検索していなければ旧版も含めた検索し直しの案内（`superseded_versions`。#1405）を、
        RAG の予算があればこの Run で残る検索の回数
        （`rag_calls_remaining`）を、根拠の章節が抜粋より長く質問が手順・一覧・条件のまとまりを
        求めるときは章節を読み切る案内（`section_reads`。#1438）を足す。
        """
        from app.features.agent.runtime import runtime_repository

        tool = mcp_base_tool_name(call.name)
        if tool not in RAG_BUDGET_TOOLS and tool not in {
            RAG_READ_SOURCE,
            RAG_READ_DOCUMENT,
            RAG_READ_SECTION,
        }:
            return output
        references = text_references(tool, output)
        if references:
            output = {**output, "references": references}
        section_note = section_read_note(tool, output, [self.goal, call.arguments.get("query")])
        if section_note is not None:
            output = {**output, "section_reads": section_note}
        if tool in RAG_BUDGET_TOOLS:
            steps = runtime_repository.get_run(self.run_id).steps
            repeated = repeated_query_note(
                call.name, call.arguments, steps, current_step_id=step_id
            )
            if repeated is not None:
                output = {**output, "repeated_query": repeated}
            codes = record_codes(
                output,
                call.arguments.get("query"),
                [
                    step.tool_call.arguments.get("query")
                    for step in steps
                    if step.tool_call is not None
                    and mcp_base_tool_name(step.tool_call.name) in RAG_BUDGET_TOOLS
                ],
            )
            if codes is not None:
                output = {**output, "record_codes": codes}
            if adjusted_limits:
                output = {
                    **output,
                    "adjusted_limits": {**adjusted_limits, "next_step": ADJUSTED_LIMITS_HINT},
                }
            superseded = superseded_versions_note(
                call.arguments, output, [self.goal, call.arguments.get("query")]
            )
            if superseded is not None:
                output = {**output, "superseded_versions": superseded}
        remaining = self.budget.rag_calls_remaining if self.budget is not None else None
        if remaining is not None:
            output = {**output, "rag_calls_remaining": remaining}
        return output

    async def check_guides(
        self, evidence_tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
        """根拠を集めた接続の業務ガイドを照合し、モデルへの案内を返す（#1322。しなければ None）。

        この Run でその接続の業務ガイドを引いていない（`rag_lookup_guides`・`rag_search` を呼んで
        いない）ときに 1 回だけ呼ぶ。照合の失敗は根拠の結果を変えない（案内を足さないだけ）。
        """
        from app.features.agent.runtime import runtime_repository

        prefix = _connection_prefix(evidence_tool)
        tool = self.guide_tools.get(prefix) if prefix is not None else None
        profile = arguments.get("search_answer_profile_id")
        if prefix is None or tool is None or prefix in self._guide_checked:
            return None
        if not isinstance(profile, str) or not profile.strip():
            return None
        self._guide_checked.add(prefix)
        if _guide_consulted(runtime_repository.get_run(self.run_id).steps, prefix):
            return None
        query = guide_check_query(self.goal, arguments.get("query"))
        if not query:
            return None
        provided = arguments.get("conditions")
        conditions = dict(self.known_conditions)
        if isinstance(provided, dict):
            conditions.update(
                {
                    key: value
                    for key, value in provided.items()
                    if isinstance(key, str) and isinstance(value, str) and value.strip()
                }
            )
        definition, handler = tool
        _step_id, result = await self.call(
            ToolCall(
                name=definition.name,
                arguments={
                    "query": query,
                    "search_answer_profile_id": profile,
                    "conditions": dict(list(conditions.items())[:GUIDE_CHECK_MAX_CONDITIONS]),
                    "limit": 1,
                },
                trace_id=f"{GUIDE_CHECK_TRACE_PREFIX}{self.run_id}_{prefix}",
            ),
            definition=definition,
            handler=handler,
        )
        if not result.success or not isinstance(result.output, dict):
            return None
        note = guide_check_note(
            result.output,
            rag_calls_remaining=self.budget.rag_calls_remaining
            if self.budget is not None
            else None,
        )
        if note is not None:
            note = {**note, "next_step": self._without_asking(note.get("next_step"))}
        return note

    async def call(
        self,
        call: ToolCall,
        *,
        definition: ToolDefinition | None = None,
        handler: ToolHandler | None = None,
        cleanup: bool = False,
    ) -> tuple[str, ToolResult]:
        """Control Plane が自分で呼ぶツール（回答の最終の検証。#1246）を step に記録して呼ぶ。

        予算は数えない。ポリシーの「拒否」は守り、「承認」は待たない（読み取りの検証のため。監査には
        `authorization=control_plane` を残す）。終わった Run では呼ばない（#2609）。`cleanup` は
        取消の後でも呼ぶ後始末（確認をやめた NL2SQL の session を閉じる）。
        """
        from app.features.agent.runtime import RunNotActiveError, runtime_repository

        if definition is not None and definition.title:
            runtime_repository.note_builtin_tool_titles(
                self.run_id, {definition.name: definition.title}
            )
        try:
            step_id, context = runtime_repository.start_builtin_tool_step(
                self.run_id, call, cleanup=cleanup
            )
        except RunNotActiveError:
            return "", ToolResult(
                name=call.name,
                success=False,
                error=RUN_CANCELLED_MESSAGE,
                error_code=RUN_CANCELLED_CODE,
            )
        result = await self.execute(
            step_id,
            call,
            context,
            definition=definition,
            handler=handler,
            authorization=_Authorization(_active_policy(), force=True, reason="control_plane"),
            # 後始末は取消の後にも送る（打ち切らない）。
            abortable=not cleanup,
        )
        return step_id, result

    async def execute(
        self,
        step_id: str,
        call: ToolCall,
        context: ToolInvocationContext,
        *,
        definition: ToolDefinition | None = None,
        handler: ToolHandler | None = None,
        scoped: data_scope.ScopeEnforcement | None = None,
        authorization: _Authorization,
        abortable: bool = True,
        follow_up: Callable[[ToolResult], Awaitable[None]] | None = None,
    ) -> ToolResult:
        """handler を呼び、結果を step に記録する。

        `abortable` の呼び出しは、Run の取消・終了で実行中の呼び先への送信を打ち切る（#2691。
        `_invoke_abortable`）。打ち切った呼び出しは失敗ではなく取消として step に残す。
        `follow_up` は、このツールの結果に続けて Runtime が行う処理（根拠の取得の後の業務ガイドの
        照合など）で、ツールの span の中で呼ぶ（その中の span はこのツールの子になる。#3471）。
        """
        from app.features.agent.runtime import runtime_repository

        if self.data_scopes:
            # 呼び先でも範囲を強制させる（サービストークンの claim `profile_ids`。#1379）。
            context = context.model_copy(
                update={"profile_scopes": data_scope.token_profile_scopes(self.data_scopes)}
            )
        if authorization.approval_id and not context.approval_id:
            context = context.model_copy(update={"approval_id": authorization.approval_id})
        # ツールの呼び出しの span（処理の経過のツールの行。#3226）。回答前の確認のツールは
        # 利用者の答えを返すだけなので span にしない（待ちの区間の span が表す）。実行中も
        # 呼んだ引数を開いて見られる。
        titles = runtime_repository.builtin_tool_titles(self.run_id)
        scope: AbstractContextManager[ObservedSpan | None] = (
            nullcontext(None)
            if call.name == CONFIRMATION_TOOL
            else observed_span(
                EXECUTE_TOOL_SPAN,
                attributes=tool_attributes(call.name, call.trace_id),
                display=tool_display(call, titles, run_step=step_id),
            )
        )
        # ツールの中の MCP の呼び出しの行も、ツールの行と同じ表示名で出す（#3468）。
        with scope as tool_span, tool_title_scope(call.name, titles.get(call.name)):
            if tool_span is not None:
                tool_span.set_call_input(
                    call.arguments, schema=definition.input_schema if definition else None
                )
            result = await self._execute_handler(
                step_id,
                call,
                context,
                definition=definition,
                handler=handler,
                scoped=scoped,
                authorization=authorization,
                abortable=abortable,
            )
            if tool_span is not None:
                if not result.success:
                    tool_span.set_status(
                        "cancelled"
                        if result.error_code == RUN_CANCELLED_CODE
                        or result.error_details.get("reason")
                        in {call_abort.REASON_RUN_CANCELLED, call_abort.REASON_TASK_CANCELLED}
                        else "failed"
                    )
                # 終わったツールの詳細（内部名・エラー・引数と結果）は終わりのイベントで送る
                # （#2373）。
                captured = (
                    tool_span.display.details.call
                    if tool_span.display and tool_span.display.details
                    else None
                )
                details = tool_details(call, result)
                if captured is not None:
                    tool_span.set_display(
                        details=(details or ChatProgressStepDetails()).model_copy(
                            update={"call": captured}
                        )
                    )
                    tool_span.set_call_output(result)
                else:
                    tool_span.set_display(details=details)
            if follow_up is not None:
                await follow_up(result)
        return result

    async def _execute_handler(
        self,
        step_id: str,
        call: ToolCall,
        context: ToolInvocationContext,
        *,
        definition: ToolDefinition | None,
        handler: ToolHandler | None,
        scoped: data_scope.ScopeEnforcement | None,
        authorization: _Authorization,
        abortable: bool,
    ) -> ToolResult:
        from app.features.agent.runtime import runtime_repository

        # 境界で確かめたのと同じポリシーで呼ぶ（`force` は承認の記録と結び付けた呼び出しなど、
        # 「承認」を実行してよいと判断したときだけ。#2610）。
        invoke = functools.partial(
            tool_registry.invoke,
            call,
            policy=authorization.policy,
            context=context,
            force=authorization.force,
            definition=definition,
            handler=handler,
            authorization=authorization.reason,
        )
        if abortable:
            result = await _invoke_abortable(self.run_id, call.name, invoke)
        else:
            result = await asyncio.to_thread(invoke)
        if scoped is not None and result.success and isinstance(result.output, dict):
            # 一覧・推薦の結果は範囲で絞ってから記録し、モデルへ返す（#1378）。
            output, removed = scoped.filter_output(result.output)
            if output is not result.output:
                result = result.model_copy(
                    update={
                        "output": output,
                        "audit_metadata": {
                            **result.audit_metadata,
                            "data_scope": {"filtered_out": removed},
                        },
                    }
                )
        result = self.deliver_images(call, result)
        runtime_repository.finish_builtin_tool_step(self.run_id, step_id, result)
        return result

    def deliver_images(self, call: ToolCall, result: ToolResult) -> ToolResult:
        """結果の画像のうちモデルに添付するものを決め、参照と監査に残す（#2650）。

        画像のデータは結果の非保存の属性だけに置き、step には参照（`content_images` の
        `model_input`）と見積もり（`audit_metadata.image_input`）だけを残す。ログにも数だけを出す。
        """
        images = tool_result_images(result)
        output = result.output
        if not result.success or not isinstance(output, dict):
            return result
        if not images and not output.get("content_images"):
            return result
        delivery = plan_image_delivery(output, images, image_input=self.image_input)
        logger.info(
            "builtin_runtime_tool_images",
            extra={"run_id": self.run_id, "tool_name": call.name, **delivery.summary},
        )
        updated = result.model_copy(
            update={
                "output": delivery.output,
                "audit_metadata": {**result.audit_metadata, "image_input": delivery.summary},
            }
        )
        return with_tool_result_images(updated, delivery.attached)


# 実行中のツールの呼び出しの間に、別のプロセスの取消を保存先で確かめる間隔（秒。#2691）。
# 同じプロセスの取消は `call_abort.abort_run_calls` ですぐに伝わる。
TOOL_CALL_CANCEL_POLL_SECONDS = 2.0


async def _invoke_abortable(
    run_id: str, tool_name: str, invoke: Callable[[], ToolResult]
) -> ToolResult:
    """ツールを thread で呼び、Run の取消・終了で呼び先への送信を打ち切る（#2691）。

    - 打ち切りの印を文脈に置いてから thread を始める（thread は文脈を引き継ぐ）。同じプロセスの
      取消は `runtime._cancel_run_locked` が、別のプロセスの取消はここでの定期の確認
      （`builtin_run_ended`）が印を立てる。この task が取り消されたとき（SDK のツールの timeout・
      停止）も立てる。
    - 印が立つと、MCP の送信は送る前に止まり、送信中の接続は切る（`tools._post_mcp_message`）。
    - 打ち切った呼び出しの結果は、失敗ではなく取消（`TOOL_CALL_ABORTED_CODE`）にする。打ち切りの前に
      届いた結果は事実のまま返す。
    """
    from app.features.agent.runtime import runtime_repository

    with call_abort.bind_call_abort(run_id) as abort:
        work = asyncio.ensure_future(asyncio.to_thread(invoke))

        async def watch() -> None:
            while not abort.aborted:
                await asyncio.sleep(TOOL_CALL_CANCEL_POLL_SECONDS)
                if await asyncio.to_thread(runtime_repository.builtin_run_ended, run_id):
                    abort.abort(call_abort.REASON_RUN_ENDED)

        watcher = asyncio.ensure_future(watch())
        try:
            result = await work
        except asyncio.CancelledError:
            if abort.abort(call_abort.REASON_TASK_CANCELLED):
                logger.info(
                    "builtin_runtime_tool_call_aborted",
                    extra={
                        "run_id": run_id,
                        "tool_name": tool_name,
                        "reason": call_abort.REASON_TASK_CANCELLED,
                    },
                )
            raise
        finally:
            watcher.cancel()
            await asyncio.wait({watcher})
    if not (abort.interrupted and not result.success):
        return result
    stopped = abort.seconds_since_abort()
    logger.info(
        "builtin_runtime_tool_call_aborted",
        extra={
            "run_id": run_id,
            "tool_name": tool_name,
            "reason": abort.reason,
            "error_code": result.error_code,
            # 取消から呼び出しが戻るまでの秒（業務データは残さない）。
            "stopped_after_seconds": None if stopped is None else round(stopped, 3),
        },
    )
    return result.model_copy(
        update={
            "error": call_abort.TOOL_CALL_ABORTED_MESSAGE,
            "error_code": call_abort.TOOL_CALL_ABORTED_CODE,
            "error_details": {
                "reason": abort.reason,
                "transport_error_code": result.error_code,
                **({"stopped_after_seconds": round(stopped, 3)} if stopped is not None else {}),
            },
        }
    )


def _run_steps(run_id: str) -> list[Any]:
    """Run の step（無い Run は空。進展の無い呼び出しの判定に使う。#2580）。"""
    from app.features.agent.runtime import runtime_repository

    try:
        return list(runtime_repository.get_run(run_id).steps)
    except KeyError:
        return []


def _tool_loop_stop(run_id: str) -> BuiltinRuntimeError | None:
    """進展の無いツールの呼び出しが上限に達した Run を終える理由（#2580。無ければ None）。

    理由は Run の失敗（イベント `runtime.failed` の `error_code` と文）・ログ・metric
    `agent_tool_loop_stops_total` に残す。引数・結果の値は残さない。
    """
    finding = tool_loop_guard.stop_finding(_run_steps(run_id))
    if finding is None:
        return None
    logger.warning(
        "builtin_runtime_tool_loop_stopped",
        extra={
            "run_id": run_id,
            "tool_name": finding.tool_name,
            "reason": finding.kind,
            "count": finding.count,
            "limit": finding.limit,
        },
    )
    TOOL_LOOP_STOPS_TOTAL.labels(reason=finding.kind).inc()
    return BuiltinRuntimeError(finding.code, finding.run_message())


# function tool に付ける、呼び出しの境界（`_ToolRecorder`）の属性の名前（`_run_sdk` が読む）。
_RECORDER_ATTR = "_agent_tool_recorder"


def _approval_gate(
    recorder: _ToolRecorder, name: str, confirmation: ConfirmationBudget | None
) -> Callable[[Any, dict[str, Any], str], Awaitable[bool]]:
    """function tool の `needs_approval`（呼び出しごとに、その時点のツール権限で決める。#2610）。

    回答前の確認（`ask_user_confirmation`）は確認の往復の残りで中断を決める（#1839）。どちらも
    引数が schema に合わない呼び出しは中断しない（実行の境界で引数の誤りとして返す。#2630）。
    確認の判断は応答ごとに 1 回だけ決め（`_ToolRecorder.hold_for_confirmation` が先に決める。
    #3352）、確認で中断する応答のほかの呼び出しは承認にも回さない（実行せずに返す）。
    """
    if name == CONFIRMATION_TOOL and confirmation is not None:
        confirm = confirmation_gate(confirmation)

        async def decide(context: Any, params: dict[str, Any], call_id: str) -> bool:
            if call_id in recorder.confirmation_decisions:
                return recorder.confirmation_decisions[call_id]
            if recorder.prepare_arguments(name, params).errors:
                decision = False
            else:
                decision = await confirm(context, params, call_id)
            if call_id:
                recorder.confirmation_decisions[call_id] = decision
            return decision

        recorder.decide_confirmation = decide
        return decide

    async def needs_approval(_context: Any, params: dict[str, Any], call_id: str) -> bool:
        if call_id and call_id in recorder.held_calls:
            return False
        return recorder.needs_approval(name, params)

    return needs_approval


def _tool_origin(definition: ToolDefinition) -> tuple[str, ...]:
    """ツールの出自（MCP 接続のツールは接続と元の名前、それ以外はツールの名前）。"""
    if definition.mcp_server_id is not None and definition.mcp_tool_name is not None:
        return ("mcp", definition.mcp_server_id, definition.mcp_tool_name)
    return ("local", definition.name)


def _origin_label(origin: tuple[str, ...]) -> str:
    if origin[0] == "mcp":
        return f"接続「{origin[1]}」のツール「{origin[2]}」"
    return f"ツール「{origin[1]}」"


def _conflicting_tool_names(
    entries: Sequence[tuple[ToolDefinition, Any]],
) -> dict[str, list[tuple[str, ...]]]:
    """別の出自のツールが同じ名前になったもの（名前 → 出自。#2596）。

    同じ出自の再列挙は重複として 1 つにまとめる（衝突ではない）。別の出自が同じ名前になったものは、
    どれか 1 つを一覧の順で選ぶと呼べる対象とツール権限の対応が順で変わるので、どれも渡さない。
    MCP の同じ接続の中の衝突は `_mcp_tools_from_result` が名前を分けるので、ここに来るのは
    接続の ID の区切りなどで別の接続・Control Plane のツールと重なった名前だけ。
    """
    origins: dict[str, list[tuple[str, ...]]] = {}
    for definition, _handler in entries:
        found = origins.setdefault(definition.name, [])
        origin = _tool_origin(definition)
        if origin not in found:
            found.append(origin)
    return {name: found for name, found in origins.items() if len(found) > 1}


def _note_tool_name_conflicts(run_id: str, conflicts: Mapping[str, list[tuple[str, ...]]]) -> None:
    """名前が衝突したツールを渡さないことを、ログと Run の警告に残す（黙って外さない）。"""
    if not conflicts:
        return
    from app.features.agent.runtime import runtime_repository

    for name, origins in sorted(conflicts.items()):
        logger.warning(
            "builtin_runtime_tool_name_conflict",
            extra={"run_id": run_id, "tool_name": name, "origins": [list(o) for o in origins]},
        )
        try:
            runtime_repository.note_builtin_warning(
                run_id,
                f"ツールの名前「{name}」が複数のツール"
                f"（{'、'.join(_origin_label(origin) for origin in origins)}）で重なるため、"
                "どれも使いません。MCP 接続の ID かツールの名前を変えてください。",
            )
        except Exception as exc:  # noqa: BLE001 - 記録は補助。残せなくてもツールの構築は続ける
            logger.warning(
                "builtin_runtime_tool_name_conflict_record_failed",
                extra={"run_id": run_id, "exception_type": type(exc).__name__},
            )


def exposed_mcp_origins(tools: Sequence[FunctionTool]) -> dict[tuple[str, str], str]:
    """モデルに渡した MCP 接続のツールの（接続の ID, 元の名前）→ モデルに渡した名前（#2596）。"""
    origins: dict[tuple[str, str], str] = {}
    for tool in tools:
        recorder = getattr(tool, _RECORDER_ATTR, None)
        entry = recorder.entries.get(tool.name) if isinstance(recorder, _ToolRecorder) else None
        if entry is None:
            continue
        definition = entry.definition
        if definition.mcp_server_id is not None and definition.mcp_tool_name is not None:
            origins[(definition.mcp_server_id, definition.mcp_tool_name)] = tool.name
    return origins


def build_function_tools(
    run_id: str,
    tool_names: list[str],
    mcp_tools: list[McpRuntimeTool] | None = None,
    reference_keys: list[tuple[str, str]] | None = None,
    budget: SupportTaskBudget | None = None,
    *,
    goal: str = "",
    known_conditions: Mapping[str, str] | None = None,
    data_scopes: data_scope.AgentDataScopes | None = None,
    declined: Collection[str] = (),
    confirmation: ConfirmationBudget | None = None,
    attachment_entries: list[tuple[ToolDefinition, ToolHandler]] | None = None,
    output_entries: list[tuple[ToolDefinition, ToolHandler]] | None = None,
    caller_planned_connections: Collection[str] = (),
    image_input: bool = False,
) -> list[FunctionTool]:
    """ツールを function tool にする。

    `declined` はこの Run で利用者が確認を取り消した種類（#1441・#1445。そのツールは渡さない）。
    `confirmation` は回答前の確認の往復（#1839）。確認できる Run にだけ `ask_user_confirmation` を
    渡し、呼び出しごとに中断するか（利用者の答えを待つか）を往復の残りで決める。
    `image_input` はモデルが画像の入力に対応するか（#2650。ツールの結果の画像を添付するか）。
    """
    policy = _active_policy()
    recorder = _ToolRecorder(run_id, budget)
    recorder.image_input = image_input
    recorder.data_scopes = dict(data_scopes or {})
    recorder.caller_planned_connections = frozenset(caller_planned_connections)
    recorder.confirmation = confirmation
    entries: list[tuple[ToolDefinition, ToolHandler | None]] = []
    for name in tool_names:
        registered = tool_registry.get(name)
        if registered is not None:
            entries.append((registered, None))
    entries.extend(mcp_tools or [])
    if reference_keys:
        from app.features.agent.skill_references import reference_tool

        entries.append(reference_tool(reference_keys))
    # 会話の添付を読むツール（#2517。添付のある会話の Run だけ）。
    entries.extend(attachment_entries or [])
    # 出力の項目の記録（#2549。出力の項目を持つ業務 Agent の Run だけ）。
    entries.extend(output_entries or [])
    if confirmation is not None and (confirmation.asks or confirmation.known_calls):
        # 答えた確認の再開では、上限に達していてもツールを渡す（SDK が同じ呼び出しを実行する）。
        entries.append(confirmation_tool(confirmation))
    tools: list[FunctionTool] = []
    seen: set[str] = set()
    conflicts = _conflicting_tool_names(entries)
    _note_tool_name_conflicts(run_id, conflicts)
    for definition, handler in entries:
        if definition.name in conflicts:
            recorder.excluded[definition.name] = UNDECLARED_CALL_NAME_CONFLICT
            continue
        if definition.name in seen:
            continue
        seen.add(definition.name)
        if is_declined_tool(definition.name, declined):
            recorder.excluded[definition.name] = UNDECLARED_CALL_DECLINED
            continue
        decision = policy.decide(definition)
        if decision == ToolPolicyDecision.DENY:
            recorder.excluded[definition.name] = UNDECLARED_CALL_POLICY_DENIED
            continue

        async def on_invoke(
            ctx: ToolContext[Any],
            arguments: str,
            *,
            _name: str = definition.name,
        ) -> str | list[Any]:
            # 引数の型を ToolContext にすると、SDK は呼び出し ID（承認の記録と結び付ける）を渡す。
            call_id = str(getattr(ctx, "tool_call_id", "") or "")
            return await recorder.invoke(_name, arguments, call_id)

        # データの範囲が 1 つなら、プロファイルの引数をモデルに見せない（#1378）。実行の定義
        # （`tool_registry.invoke` に渡す schema）は呼び先の契約のまま。
        # 根拠のツールの件数の説明には「多段の質問では省略する」を足す（#1403）。
        # 根拠の取得の検索文を作る主体（planning）と追跡 ID は実行環境が決める（#2651）。
        # Agent 主体の取得を許さない接続は、caller でだけ効く引数（queries など）も
        # 見せない（#2676）。
        schema = data_scope.scoped_input_schema(
            definition.name, dict(definition.input_schema), recorder.data_scopes
        )
        if recorder.caller_planned(definition.name):
            schema = caller_planned_schema(definition.name, schema)
        else:
            schema = rag_planned_schema(definition.name, schema)
        schema = search_limit_schema(definition.name, schema)
        tool = FunctionTool(
            name=definition.name,
            description=definition.description,
            params_json_schema=schema,
            on_invoke_tool=on_invoke,
            strict_json_schema=_strict_schema(schema),
            # 承認が要るかは構築の時点で固定せず、呼び出しごとに最新のツール権限で決める（#2610）。
            needs_approval=_approval_gate(recorder, definition.name, confirmation),
            timeout_seconds=max(definition.timeout_seconds, 1.0) * 2,
        )
        # 引数はモデルに見せた schema（SDK の strict mode の変換の後）で確かめる（#2630）。
        recorder.add_tool(
            _ToolEntry(
                definition=definition,
                handler=handler,
                visible_schema=tool.params_json_schema,
                hidden=hidden_arguments(definition.input_schema, tool.params_json_schema),
            )
        )
        setattr(tool, _RECORDER_ATTR, recorder)
        tools.append(tool)
    recorder.environment_tools = [tool.name for tool in tools if is_environment_tool(tool.name)]
    exposed = {tool.name for tool in tools}
    if (
        confirmation is not None
        and CONFIRMATION_TOOL in exposed
        and aid_enabled(run_id, "tool_grounding")
        and tool_grounding.grounding_tools(exposed)
    ):
        # 回答前の確認の前に、資料・業務データのツールで調べさせる（#1917。拒答のガード #1835 と
        # 同じ設定で切れる）。
        confirmation.tools_consulted = lambda: _tools_consulted(run_id)
    if confirmation is not None and CONFIRMATION_TOOL in exposed:
        # ツール（NL2SQL・RAG）の確認事項を聞き直させない（#1934）。
        confirmation.tool_clarification_pending = lambda: _tool_clarification_pending(
            run_id, recorder.known_conditions
        )
        # ツールのエラーの直後の確認を差し戻す（#2234）。
        confirmation.last_tool_failed = lambda: _last_tool_failed(run_id)
        if any(mcp_base_tool_name(name) == NL2SQL_QUERY for name in exposed):
            # 業務データの表・列を利用者に聞かせず、先に nl2sql_query を呼ばせる（#2223）。
            confirmation.nl2sql_unqueried = lambda: _nl2sql_unqueried(run_id)
    for definition, handler in entries:
        prefix = _connection_prefix(definition.name)
        if (
            prefix is not None
            and definition.name in exposed
            and mcp_base_tool_name(definition.name) == RAG_LOOKUP_GUIDES
        ):
            recorder.guide_tools.setdefault(prefix, (definition, handler))
    recorder.goal = goal
    recorder.known_conditions = dict(known_conditions or {})
    return tools


class _ConfirmationTurnHooks(AgentHooks[Any]):
    """モデルの応答ごとに、回答前の確認で中断するかをツールの実行の前に決める（#3352）。"""

    def __init__(self, recorder: _ToolRecorder) -> None:
        self.recorder = recorder

    async def on_llm_end(
        self, context: RunContextWrapper[Any], agent: Agent[Any], response: ModelResponse
    ) -> None:
        await self.recorder.hold_for_confirmation(context, list(response.output))


def build_sdk_agent(
    run_id: str,
    *,
    name: str,
    instructions: str,
    skill_ids: list[str],
    model_id: str,
    agent_id: str | None = None,
    user_uuid: str | None = None,
    budget: SupportTaskBudget | None = None,
    support_task: str = "",
    goal: str = "",
    known_conditions: Mapping[str, str] | None = None,
    data_scopes: data_scope.AgentDataScopes | None = None,
    cancellations: list[dict[str, Any]] | None = None,
    confirmation: ConfirmationBudget | None = None,
    confirmation_response: Mapping[str, Any] | None = None,
    thread_id: str | None = None,
    output_fields: Sequence[AgentOutputField] = (),
    caller_planning: bool = True,
) -> Agent[Any]:
    """SDK の Agent を作る（MCP 接続のツール一覧を HTTP で取るので、イベントループの外で呼ぶ）。

    `confirmation` は回答前の確認の往復（#1839）、`confirmation_response` はこの Run が答えた
    ツールの結果の確認（metadata `clarification_response`）。方針と答えを指示に足す。

    `support_task` は前の Run から引き継いだ支援タスクの状態（指示の末尾に足す。#1243）。
    `thread_id` の会話に添付があれば、添付を読むツールと添付の索引を足す（#2517）。
    `output_fields` があれば、その項目の値を記録する `record_output` を渡す（#2549）。
    `cancellations` はこの Run で利用者が取り消した確認（#1441・#1445）。その種類のツールを渡さず、
    ほかの手段で続けるか意図を聞き直す指示を足す。
    `caller_planning` が False（固定の中身の無い公開の版）なら、スキルの Agent 主体の根拠の取得の
    宣言に従わない（RAG が検索文を計画する。#2651）。
    """
    from app.features.agent.chat_attachments import (
        attachment_instructions,
        attachment_tools,
        thread_attachment_toolset,
    )
    from app.features.agent.runtime import runtime_repository
    from app.features.agent.skill_references import skill_reference_keys

    target = resolve_model_target(model_id)
    attachments = thread_attachment_toolset(thread_id)
    mcp_tools, warnings = discover_mcp_tools(
        skill_ids,
        context=ToolInvocationContext(run_id=run_id, agent_id=agent_id, user_uuid=user_uuid),
    )
    for warning in warnings:
        runtime_repository.note_builtin_warning(run_id, warning)
    if warnings:
        # 必須の接続が一部でも使えなければ、欠けたツールのまま回答させない。
        raise BuiltinRuntimeError(
            "runtime.mcp_unavailable",
            "必要な MCP 接続を利用できません。" + "\n" + "\n".join(warnings),
        )
    output_entries = [record_output_tool(run_id, output_fields)] if output_fields else []
    # ツールの表示名（MCP の `title`）は、画面の処理の段階の名前に使う（モデルには渡さない）。
    runtime_repository.note_builtin_tool_titles(
        run_id,
        {
            definition.name: definition.title
            for definition, _handler in [*mcp_tools, *output_entries]
            if definition.title
        },
    )
    tools = build_function_tools(
        run_id,
        agent_tool_names(skill_ids),
        mcp_tools,
        reference_keys=skill_reference_keys(skill_ids),
        budget=budget,
        goal=goal,
        known_conditions=known_conditions,
        data_scopes=data_scopes,
        declined=declined_kinds({CANCEL_METADATA_KEY: cancellations or []}),
        confirmation=confirmation,
        attachment_entries=(
            attachment_tools(attachments, image_input=target.vision_enabled)
            if attachments
            else None
        ),
        output_entries=output_entries or None,
        caller_planned_connections=(
            agent_caller_planned_connections(skill_ids) if caller_planning else frozenset()
        ),
        image_input=target.vision_enabled,
    )
    # 指示のツールの名前は、モデルに渡すツールの名前にそろえる（#1303）。
    exposed = [tool.name for tool in tools]
    origins = exposed_mcp_origins(tools)
    composed = compose_instructions(instructions, skill_ids, exposed, origins)
    # データの範囲の案内（#1378。強制は tool handler がする）。
    scope_text = data_scope.scope_instructions(data_scopes, exposed)
    if scope_text:
        scope_names = instruction_tool_names(skill_ids, exposed, origins)
        composed = f"{composed}\n\n{render_tool_names(scope_text, scope_names)}"
    # ここまでが業務 Agent の版で決まる部分。後ろに Run の状態を足す（#2583 の指紋と見積もり）。
    static_instructions = composed
    if support_task:
        support_names = instruction_tool_names(skill_ids, exposed, origins)
        composed = f"{composed}\n\n{render_tool_names(support_task, support_names)}"
    cancel_text = cancel_instructions(list(cancellations or []))
    if cancel_text:
        composed = f"{composed}\n\n{cancel_text}"
    if attachments:
        composed = f"{composed}\n\n{attachment_instructions(attachments)}"
    if confirmation is not None:
        # 回答前の確認の方針と、カードで答えた内容（#1839）。ツールの名前はモデルに渡す名前にする。
        confirmation_names = instruction_tool_names(skill_ids, exposed, origins)
        for text in (
            policy_instructions(confirmation, tool_exposed=CONFIRMATION_TOOL in exposed),
            response_instructions(confirmation_response),
        ):
            if text:
                composed = f"{composed}\n\n{render_tool_names(text, confirmation_names)}"
    recorder = next(
        (
            found
            for tool in tools
            if isinstance(found := getattr(tool, _RECORDER_ATTR, None), _ToolRecorder)
        ),
        None,
    )
    sdk_agent = Agent(
        name=name or "agent",
        instructions=composed,
        tools=list(tools),
        model=model_factory(target),
        model_settings=model_settings_for(target),
        hooks=_ConfirmationTurnHooks(recorder) if recorder is not None else None,
    )
    # 入力を収める上限（#2383。`_run_config` の `_ContextWindow` が読む）。
    setattr(
        sdk_agent,
        _CONTEXT_LIMITS_ATTR,
        ContextLimits(
            model_id=target.model_id,
            max_input_tokens=target.max_input_tokens,
            max_output_tokens=target.max_output_tokens,
        ),
    )
    setattr(sdk_agent, _STATIC_INSTRUCTIONS_ATTR, static_instructions)
    config = _run_configuration(
        skill_ids, static_instructions, tools, mcp_tools, target, data_scopes
    )
    # Run の無い組み立て（設定の確認など）では残さない。
    with suppress(KeyError):
        runtime_repository.note_run_config(run_id, config)
    return sdk_agent


def _run_configuration(
    skill_ids: list[str],
    static_instructions: str,
    tools: Sequence[Any],
    mcp_tools: Sequence[McpRuntimeTool],
    target: ModelTarget,
    data_scopes: data_scope.AgentDataScopes | None,
) -> dict[str, Any]:
    """Run の実行構成の指紋（#67。本文・資格情報は入れない）。"""
    skills: list[dict[str, Any]] = []
    for skill_id in skill_ids:
        resolved = resolve_skill(skill_id)
        if resolved is None:
            continue
        current = skill_registry.get(skill_id)
        contents = {item.path: item.content for item in (current.references if current else [])}
        dumped = resolved.model_dump(mode="json", exclude={"updated_at", "created_at"})
        dumped["references"] = [
            {
                "path": item.path,
                "sha256": run_config.sha256(contents[item.path]) if item.path in contents else None,
            }
            for item in resolved.references
        ]
        skills.append(dumped)
    return run_config.run_configuration(
        static_instructions=static_instructions,
        tools=tools,
        skills=skills,
        mcp_tools=[definition for definition, _handler in mcp_tools],
        model={
            "model_id": target.model_id,
            "reasoning_effort": target.reasoning_effort,
            "max_output_tokens": target.max_output_tokens,
            "max_input_tokens": target.max_input_tokens,
            "vision_enabled": target.vision_enabled,
        },
        data_scopes=data_scopes,
        sdk_version=sdk_version(),
    )


# 無いツールの呼び出しの返答で、候補として並べるツールの数の上限（#1303）。
_TOOL_NOT_FOUND_LIST_MAX = 20

# モデルに渡していない名前のツールの呼び出し（宣言の閉包の外の呼び出し）の理由（#3240）。
# どの理由でも実行せず（`tool_not_found_message` をモデルへ返す）、検知を理由に渡すツールを
# 広げない。理由は Run のイベント（`undeclared_tool_call`）・ログ・metric に残す。
# 渡したツールと同じツールを別の名前（素の名前・別の接頭辞）で呼んだ（モデルの名前違い）。
UNDECLARED_CALL_NAME_MISMATCH = "name_mismatch"
# スキルが宣言したが、ツール権限が「拒否」なので渡していない。
UNDECLARED_CALL_POLICY_DENIED = "policy_denied"
# スキルが宣言したが、この Run で利用者が確認を取り消した種類なので渡していない（#1441）。
UNDECLARED_CALL_DECLINED = "declined"
# スキルが宣言したが、別の出自のツールと名前が重なったので渡していない（#2596）。
UNDECLARED_CALL_NAME_CONFLICT = "name_conflict"
# 同じ会話の前の Run で呼んだツールだが、この Run では渡していない（前の文脈の名前）。
UNDECLARED_CALL_PREVIOUS_CONTEXT = "previous_context"
# 登録済みの MCP 接続・Control Plane のツールの名前だが、業務 Agent のスキルが宣言していない
# （スキルの設定の不足か、宣言の外への呼び出しの試み）。
UNDECLARED_CALL_NOT_DECLARED = "not_declared"
# どの接続・ツールの名前にも当たらない。
UNDECLARED_CALL_UNKNOWN = "unknown"
UNDECLARED_CALL_REASONS: tuple[str, ...] = (
    UNDECLARED_CALL_NAME_MISMATCH,
    UNDECLARED_CALL_POLICY_DENIED,
    UNDECLARED_CALL_DECLINED,
    UNDECLARED_CALL_NAME_CONFLICT,
    UNDECLARED_CALL_PREVIOUS_CONTEXT,
    UNDECLARED_CALL_NOT_DECLARED,
    UNDECLARED_CALL_UNKNOWN,
)
# 記録するツールの名前の長さの上限（モデルが作った名前なので、長い文を残さない）。
_UNDECLARED_NAME_MAX_CHARS = 128


def classify_undeclared_call(
    called: str,
    exposed: Collection[str],
    *,
    excluded: Mapping[str, str],
    previous_names: Collection[str],
    registered_connections: Collection[str],
    registered_tools: Collection[str],
) -> str:
    """モデルに渡していない名前のツールを呼んだ理由（`UNDECLARED_CALL_REASONS` の 1 つ。#3240）。

    先に、この Run で外したツール（スキルが宣言したもの）か、渡したツールの別名かを見る。次に
    同じ会話の前の Run で呼んだ名前か、登録済みの接続・Control Plane のツールの名前かを見る。
    """
    if called in excluded:
        return excluded[called]
    base = mcp_base_tool_name(called)
    if any(name != called and mcp_base_tool_name(name) == base for name in exposed):
        return UNDECLARED_CALL_NAME_MISMATCH
    if called in previous_names:
        return UNDECLARED_CALL_PREVIOUS_CONTEXT
    prefix = _connection_prefix(called)
    if called in registered_tools or (prefix is not None and prefix in registered_connections):
        return UNDECLARED_CALL_NOT_DECLARED
    return UNDECLARED_CALL_UNKNOWN


def _previous_tool_names(run_id: str) -> set[str]:
    """同じ会話の前の Run で呼んだツールの名前（`previous_context` の判定）。"""
    from app.features.agent.runtime import runtime_repository

    names: set[str] = set()
    for run in runtime_repository.thread_previous_runs(run_id):
        names.update(step.tool_call.name for step in run.steps if step.tool_call is not None)
    return names


def record_undeclared_call(
    run_id: str,
    *,
    called: str,
    call_id: str,
    exposed: Collection[str],
    excluded: Mapping[str, str],
) -> str:
    """渡していない名前の呼び出しを理由つきで Run のイベント・ログ・metric に残す（#3240）。

    記録は補助（失敗しても Run は続ける）。呼び出しは実行しない（実行しないことは呼び出し側の
    SDK の `tool_not_found` の扱い）。理由を返す。
    """
    from app.features.agent.runtime import runtime_repository

    try:
        previous = _previous_tool_names(run_id)
    except Exception:  # noqa: BLE001 - 前の Run が読めなくても理由は決める
        previous = set()
    reason = classify_undeclared_call(
        called,
        exposed,
        excluded=excluded,
        previous_names=previous,
        registered_connections={
            config.server_id for config in runtime_config_store.list_mcp_servers()
        },
        registered_tools=set(tool_registry.names()),
    )
    name = called[:_UNDECLARED_NAME_MAX_CHARS]
    UNDECLARED_TOOL_CALLS_TOTAL.labels(reason=reason).inc()
    logger.warning(
        "builtin_runtime_undeclared_tool_call",
        extra={"run_id": run_id, "tool_name": name, "call_id": call_id, "reason": reason},
    )
    try:
        runtime_repository.record_undeclared_tool_call(
            run_id, {"tool_name": name, "call_id": call_id, "reason": reason}
        )
    except Exception as exc:  # noqa: BLE001 - 記録は補助。残せなくてもモデルへ案内は返す
        logger.warning(
            "builtin_runtime_undeclared_tool_call_record_failed",
            extra={"run_id": run_id, "exception_type": type(exc).__name__},
        )
    return reason


def tool_not_found_message(called: str, exposed: Collection[str]) -> str:
    """モデルが渡していないツールを呼んだときに返す案内（呼ぶ名前を示す。#1303）。

    素の名前（`rag_search`）や別の接頭辞で呼んだときは、ツールの部分が同じ名前を候補にする。
    候補が 1 つなら呼び直す名前を、複数（同じツールの名前が複数の接続にある）なら候補を並べ、
    どれかを選ばせる（Control Plane がどれかに決めて実行することはしない）。
    """
    base = mcp_base_tool_name(called)
    names = sorted(set(exposed))
    candidates = [name for name in names if name != called and mcp_base_tool_name(name) == base]
    if len(candidates) == 1:
        return (
            f"ツール「{called}」はありません。呼ぶときの名前は「{candidates[0]}」です。"
            f"同じ引数で「{candidates[0]}」を呼び直してください。"
        )
    if candidates:
        return (
            f"ツール「{called}」はありません。同じ名前のツールが複数の接続にあります"
            f"（{'、'.join(candidates)}）。目的に合う接続のツールの名前で呼んでください。"
        )
    if not names:
        return f"ツール「{called}」はありません。この実行で使えるツールはありません。"
    listed = "、".join(names[:_TOOL_NOT_FOUND_LIST_MAX])
    more = "ほか" if len(names) > _TOOL_NOT_FOUND_LIST_MAX else ""
    return f"ツール「{called}」はありません。使えるツールの名前: {listed}{more}。"


# 縮めた結果に読み直すツールを案内する、RAG の根拠を返すツール（本文は `rag_read_source` で
# 読める）。
_RAG_EVIDENCE_OUTPUT_TOOLS = frozenset(
    {*RAG_BUDGET_TOOLS, RAG_READ_SOURCE, RAG_READ_DOCUMENT, RAG_READ_SECTION}
)


class _ContextWindow:
    """Run のモデルの入力を、モデルの入力の上限に収める（#2383。SDK の `call_model_input_filter`）。

    呼び出しの直前に入力を見積もり、予算を超えるときは古いツールの結果から縮める（縮め方は
    `context_window`）。縮めても収まらなければモデルを呼ばずに `runtime.context_overflow` で失敗
    させる。縮めたことは Run のイベント・metric・ログに残す（同じ縮め方が続く呼び出しでは
    1 回だけ）。
    プロバイダが入力の超過を返したら（`retry_request`）、返った prompt の token 数で Run の残りの
    見積もりを補正し、縮めた要求を返す。
    """

    def __init__(
        self,
        run_id: str,
        limits: ContextLimits,
        exposed: Collection[str],
        rerunnable: Collection[str] = (),
        *,
        static_instructions: str | None = None,
    ) -> None:
        settings = get_settings()
        self.run_id = run_id
        # 指示の変わらない部分（`build_sdk_agent`。モデルの呼び出しの span の指紋・見積もり。
        # #2583）。
        self.static_instructions = static_instructions
        self.enabled = aid_enabled(run_id, "context_compaction")
        self.budget = ContextBudget(
            limits,
            estimate_scale=float(settings.agent_context_token_estimate_scale),
            margin_ratio=float(settings.agent_context_safety_margin_ratio),
        )
        self._exposed = set(exposed)
        # 呼び直しても副作用の無いツール（信頼できる読み取り専用。#2632）。
        self._rerunnable = set(rerunnable)
        self._tools_text: str | None = None
        # プロバイダが返した入力の上限（設定より小さいとき。Run の残りの呼び出しにも使う）。
        self._provider_limit: int | None = None
        self._recorded: tuple[object, ...] | None = None
        # 直前のモデルの呼び出しの入力（見積もりと画像。実測と並べる。#2650）。
        self.last_input: CompactionResult | None = None

    @property
    def provider_limit(self) -> int | None:
        """プロバイダが返した入力の上限（設定より小さいときだけ。無ければ None）。"""
        return self._provider_limit

    def read_tool_for(self, tool_name: str) -> str | None:
        """縮めた結果の本文を読み直すツール（モデルに渡した名前。無ければ None）。"""
        prefix = _connection_prefix(tool_name)
        if prefix is None or mcp_base_tool_name(tool_name) not in _RAG_EVIDENCE_OUTPUT_TOOLS:
            return None
        name = f"{prefix}{MCP_TOOL_SEPARATOR}{RAG_READ_SOURCE}"
        return name if name in self._exposed else None

    def rerunnable(self, tool_name: str) -> bool:
        """縮めた結果の内容を得るために、同じツールを呼び直してよいか（#2632）。"""
        return tool_name in self._rerunnable

    def __call__(self, data: CallModelData[Any]) -> ModelInputData:
        _ACTIVE_CONTEXT_WINDOW.set(self)
        model_data = data.model_data
        if not self.enabled:
            return model_data
        if self._tools_text is None:
            self._tools_text = tools_text(list(data.agent.tools))
        result = compact_items(
            model_data.input,
            budget=self.budget,
            fixed_text=f"{model_data.instructions or ''}\n{self._tools_text}",
            read_tool_for=self.read_tool_for,
            rerunnable=self.rerunnable,
            max_input_tokens=self._provider_limit,
        )
        if not result.fits:
            self._fail(result, trigger="budget")
        if result.outputs:
            self._record(result, trigger="budget")
        self.last_input = result
        return ModelInputData(input=result.items, instructions=model_data.instructions)

    def measure(self, response: Any) -> None:
        """画像を添付した呼び出しの、入力の見積もりとプロバイダが数えた入力（実測）を残す（#2650）。

        画像の token の見積もりはモデルの画像の扱いに依らない多めの値なので、実測と並べてログに
        残し、見積もりの係数を直す根拠にする（画像・本文の値は出さない）。
        """
        last = self.last_input
        if last is None or not last.images:
            return
        usage = getattr(response, "usage", None)
        measured = getattr(usage, "input_tokens", None)
        logger.info(
            "builtin_runtime_model_input_measured",
            extra={
                "run_id": self.run_id,
                "model": self.budget.limits.model_id,
                "images": last.images,
                "estimated_image_tokens": last.image_tokens,
                "estimated_tokens": last.tokens_after,
                "measured_input_tokens": measured if isinstance(measured, int) else None,
            },
        )

    def retry_request(self, request: dict[str, Any], overflow: ProviderOverflow) -> dict[str, Any]:
        """プロバイダが入力の超過を返した要求を縮める（縮められなければ失敗させる）。"""
        items = request.get("input")
        if not isinstance(items, list):
            self.fail_after_retry(overflow)
        fixed = f"{request.get('instructions') or ''}\n{tools_text(request.get('tools') or [])}"
        estimated = estimate_tokens(items, budget=self.budget, fixed_text=fixed)
        # 見積もりを、プロバイダが数えた prompt の token 数に合わせる（読めなければ一定の倍率）。
        ratio = (
            overflow.prompt_tokens / estimated
            if overflow.prompt_tokens and estimated > 0
            else FALLBACK_PROVIDER_SCALE
        )
        if ratio <= 1.0 and overflow.limit_tokens is None:
            ratio = FALLBACK_PROVIDER_SCALE
        self.budget.estimate_scale *= max(ratio, 1.0)
        if overflow.limit_tokens is not None:
            self._provider_limit = overflow.limit_tokens
        result = compact_items(
            items,
            budget=self.budget,
            fixed_text=fixed,
            read_tool_for=self.read_tool_for,
            rerunnable=self.rerunnable,
            max_input_tokens=self._provider_limit,
        )
        logger.warning(
            "builtin_runtime_context_overflow_retry",
            extra={
                "run_id": self.run_id,
                "model": self.budget.limits.model_id,
                "provider_prompt_tokens": overflow.prompt_tokens,
                "provider_limit_tokens": overflow.limit_tokens,
                "estimate_scale": round(self.budget.estimate_scale, 3),
                "fits": result.fits,
                "compacted_outputs": len(result.outputs),
            },
        )
        if not result.fits:
            self._fail(result, trigger="provider_limit")
        if not result.outputs:
            # 縮める結果が無い（同じ要求を送り直しても同じ超過になる）。
            self.fail_after_retry(overflow)
        self._record(result, trigger="provider_limit")
        # 送り直す要求の入力（モデルの呼び出しの span の縮める前後。#2583）。
        self.last_input = result
        return {**request, "input": result.items}

    def fail_after_retry(self, overflow: ProviderOverflow) -> Never:
        """縮めて送り直しても（縮められずに）プロバイダが入力の超過を返したときの失敗。"""
        model = self.budget.limits.model_id
        CONTEXT_COMPACTIONS_TOTAL.labels(
            model=model, trigger="provider_limit", outcome="failed"
        ).inc()
        logger.warning(
            "builtin_runtime_context_overflow",
            extra={
                "run_id": self.run_id,
                "model": model,
                "trigger": "provider_limit",
                "provider_prompt_tokens": overflow.prompt_tokens,
                "provider_limit_tokens": overflow.limit_tokens,
            },
        )
        raise BuiltinRuntimeError(
            CONTEXT_OVERFLOW_CODE, provider_overflow_message(self.budget, overflow)
        )

    def _fail(self, result: CompactionResult, *, trigger: str) -> Never:
        model = self.budget.limits.model_id
        CONTEXT_COMPACTIONS_TOTAL.labels(model=model, trigger=trigger, outcome="failed").inc()
        logger.warning(
            "builtin_runtime_context_overflow",
            extra={"run_id": self.run_id, **self._summary(result, trigger)},
        )
        raise BuiltinRuntimeError(CONTEXT_OVERFLOW_CODE, overflow_message(result, self.budget))

    def _summary(self, result: CompactionResult, trigger: str) -> dict[str, Any]:
        limits = self.budget.limits
        return {
            "trigger": trigger,
            "model": limits.model_id,
            "max_input_tokens": limits.max_input_tokens,
            "max_output_tokens": limits.max_output_tokens,
            "provider_limit_tokens": self._provider_limit,
            "budget_tokens": result.budget_tokens,
            "fixed_tokens": result.fixed_tokens,
            "estimated_tokens_before": result.tokens_before,
            "estimated_tokens_after": result.tokens_after,
            "estimate_scale": round(self.budget.estimate_scale, 3),
            # 入力に添付した画像の数と見積もり（#2650）。
            "images": result.images,
            "estimated_image_tokens": result.image_tokens,
        }

    def _record(self, result: CompactionResult, *, trigger: str) -> None:
        from app.features.agent.runtime import runtime_repository

        key = (trigger, tuple((item.call_id, item.level) for item in result.outputs))
        if key == self._recorded:
            return
        self._recorded = key
        payload = {
            **self._summary(result, trigger),
            "outputs": [item.as_record() for item in result.outputs],
        }
        CONTEXT_COMPACTIONS_TOTAL.labels(
            model=self.budget.limits.model_id, trigger=trigger, outcome="compacted"
        ).inc()
        logger.info(
            "builtin_runtime_context_compacted",
            extra={
                "run_id": self.run_id,
                **self._summary(result, trigger),
                "compacted_outputs": len(result.outputs),
            },
        )
        try:
            runtime_repository.record_context_compaction(self.run_id, payload)
        except Exception as exc:  # noqa: BLE001 - 記録は補助。残せなくてもモデルは呼ぶ
            logger.warning(
                "builtin_runtime_context_compaction_record_failed",
                extra={"run_id": self.run_id, "exception_type": type(exc).__name__},
            )


def _rerunnable_tool_names(tools: Sequence[Any]) -> set[str]:
    """呼び直しても副作用の無いツール（モデルに渡した定義が信頼できる読み取り専用のもの。#2632）。"""
    names: set[str] = set()
    for tool in tools:
        recorder = getattr(tool, _RECORDER_ATTR, None)
        name = str(getattr(tool, "name", ""))
        entry = recorder.entries.get(name) if isinstance(recorder, _ToolRecorder) else None
        if entry is not None and entry.definition.trusted_read_only:
            names.add(name)
    return names


def _run_config(run_id: str, sdk_agent: Agent[Any]) -> RunConfig:
    """Run の SDK の設定。無いツールの呼び出しで Run を落とさず、モデルへ案内を返す（#1303）。

    SDK の既定（`raise_error`）は、モデルが渡していない名前のツールを 1 回呼んだだけで
    `ModelBehaviorError` にして Run 全体を失敗させる。呼ぶ名前を返して、モデルに呼び直させる。
    素の名前を Control Plane が別のツールへ読み替えて実行することはしない（呼び出し・承認・監査は
    モデルが呼んだ名前のまま。同じ名前が複数の接続にあるときに誤った接続を呼ばない）。
    """
    exposed = [str(getattr(tool, "name", "")) for tool in sdk_agent.tools]
    recorder = next(
        (
            found
            for tool in sdk_agent.tools
            if isinstance(found := getattr(tool, _RECORDER_ATTR, None), _ToolRecorder)
        ),
        None,
    )
    excluded = dict(recorder.excluded) if recorder is not None else {}

    async def format_error(args: ToolErrorFormatterArgs[Any]) -> str | None:
        if args.kind != "tool_not_found":
            return None
        # 渡していない名前の呼び出しは実行せず、理由つきで Run に残す（#3240）。
        await asyncio.to_thread(
            record_undeclared_call,
            run_id,
            called=args.tool_name,
            call_id=str(args.call_id or ""),
            exposed=exposed,
            excluded=excluded,
        )
        return tool_not_found_message(args.tool_name, exposed)

    limits = getattr(sdk_agent, _CONTEXT_LIMITS_ATTR, None)
    static_instructions = getattr(sdk_agent, _STATIC_INSTRUCTIONS_ATTR, None)
    window = (
        _ContextWindow(
            run_id,
            limits,
            exposed,
            _rerunnable_tool_names(sdk_agent.tools),
            static_instructions=static_instructions
            if isinstance(static_instructions, str)
            else None,
        )
        if isinstance(limits, ContextLimits)
        else None
    )

    async def model_input(data: CallModelData[Any]) -> ModelInputData:
        from app.features.agent.runtime import runtime_repository

        # 取消が確定した Run ではモデルを呼ばずに止める（#2609。別のプロセスの取消も保存先で判定）。
        if await asyncio.to_thread(runtime_repository.builtin_run_ended, run_id):
            raise RunCancelledError()
        # 進展の無いツールの呼び出しが上限に達したら、モデルを呼ばずに Run を終える（#2580）。
        stop = await asyncio.to_thread(_tool_loop_stop, run_id)
        if stop is not None:
            raise stop
        model_data = window(data) if window is not None else data.model_data
        # Run 全体の実行予算（#68）: 呼ぶ前に 1 回と入力の見積もりを予約する。超えるなら呼ばない。
        budget = _ACTIVE_RUN_BUDGET.get()
        if budget is not None:
            estimate = window.last_input.tokens_after if window and window.last_input else 0
            exceeded = budget.reserve_model_call(estimate)
            if exceeded is not None:
                raise _run_budget_stop(run_id, budget, exceeded)
        return model_data

    return RunConfig(
        tool_not_found_behavior="return_error_to_model",
        tool_error_formatter=format_error,
        call_model_input_filter=model_input,
    )


async def _run_sdk(
    run_id: str,
    sdk_agent: Agent[Any],
    run_input: Any,
    *,
    context: RunContextWrapper[Any] | None = None,
) -> Any:
    """Run の SDK の実行（設定は `_run_config`）。終わったら入力の管理の文脈を外す（#2383）。

    SDK は引数を読めない呼び出し（壊れた JSON・object 以外）を、ツール権限に関わらず承認待ちの
    中断にする。人に承認を求めず、引数の誤りとしてモデルへ返して続ける（#2630。
    `_settle_unreadable_calls`）。
    """
    try:
        config = _run_config(run_id, sdk_agent)
        hooks = _RunBudgetHooks()
        result = await Runner.run(
            sdk_agent,
            run_input,
            context=context,
            max_turns=_max_turns(),
            run_config=config,
            hooks=hooks,
        )
        for _ in range(_max_turns()):
            state = _settle_unreadable_calls(sdk_agent, result)
            if state is None:
                break
            result = await Runner.run(
                sdk_agent,
                state,
                context=context,
                max_turns=_max_turns(),
                run_config=config,
                hooks=hooks,
            )
        return result
    finally:
        _ACTIVE_CONTEXT_WINDOW.set(None)


class _RunBudgetHooks(RunHooks[Any]):
    """モデルの応答の usage で、Run 全体の実行予算の予約を精算する（#68）。"""

    async def on_llm_end(
        self, context: RunContextWrapper[Any], agent: Agent[Any], response: ModelResponse
    ) -> None:
        budget = _ACTIVE_RUN_BUDGET.get()
        if budget is not None:
            budget.settle(response.usage)


def _run_budget_stop(
    run_id: str, budget: run_budget.RunBudget, exceeded: Mapping[str, Any]
) -> BuiltinRuntimeError:
    """Run 全体の実行予算で Run を終える理由（モデルを呼ばずに決まった文を作る。#68）。"""
    logger.warning(
        "builtin_runtime_run_budget_exceeded",
        extra={
            "run_id": run_id,
            "scope": exceeded.get("scope"),
            "limit": exceeded.get("limit"),
            "used": exceeded.get("used"),
            "model_calls": budget.model_calls,
            "total_tokens": budget.total_tokens,
            "unmeasured_calls": budget.unmeasured_calls,
            "refused_tool_calls": budget.refused_tool_calls,
        },
    )
    return BuiltinRuntimeError(
        run_budget.RUN_BUDGET_EXCEEDED_CODE, run_budget.exceeded_message(exceeded, budget)
    )


def _begin_run_budget(run: Any) -> Any:
    """この実行の Run 全体の実行予算（前の実行の消費から続ける）を入れる（戻す token を返す）。"""
    budget = run_budget.RunBudget.for_run(
        run.metadata, run_budget.RunBudgetLimits.from_settings(get_settings())
    )
    return _ACTIVE_RUN_BUDGET.set(budget)


def _record_run_budget(run_id: str) -> None:
    """この実行までの Run 全体の実行予算の消費を Run に残す（区切りの書き込みの前に呼ぶ。#68）。

    結果の無い step（呼び先へ送った後に結果が届いていない）は、結果不明として数える。
    """
    from app.features.agent.runtime import StepStatus, runtime_repository

    budget = _ACTIVE_RUN_BUDGET.get()
    if budget is None:
        return
    unknown = sum(
        1
        for step in _run_steps(run_id)
        if step.status in {StepStatus.PENDING, StepStatus.RUNNING} and step.tool_result is None
    )
    try:
        runtime_repository.record_run_budget(run_id, budget.snapshot(unknown_steps=unknown))
    except Exception as exc:  # noqa: BLE001 - 記録は補助。残せなくても Run の結果は書く
        logger.warning(
            "builtin_runtime_run_budget_record_failed",
            extra={"run_id": run_id, "exception_type": type(exc).__name__},
        )


def _readable_arguments(raw: object) -> bool:
    """SDK が承認の判定に読める引数か（空でない JSON の object）。"""
    if not isinstance(raw, str) or not raw.strip():
        return False
    try:
        return isinstance(json.loads(raw), dict)
    except ValueError:
        return False


def _settle_unreadable_calls(sdk_agent: Agent[Any], result: Any) -> Any | None:
    """SDK が引数を読めずに承認待ちにした呼び出しを決める（決めたら次に渡す SDK の状態）。

    - 引数の誤り（壊れた JSON・object 以外・schema に合わない）は、承認に回さず step に残して
      モデルへ誤りを返す（却下にする。呼び先へは送らない）。
    - 引数なし（空の文字列）で schema に合い、最新のツール権限が承認を求めないものは、承認なしで
      実行させる（`_ToolRecorder.invoke` が同じ境界で確かめる）。
    - 承認が要るものは、そのまま承認待ちにする。
    """
    recorders: dict[str, _ToolRecorder] = {}
    for tool in sdk_agent.tools:
        recorder = getattr(tool, _RECORDER_ATTR, None)
        if isinstance(recorder, _ToolRecorder):
            recorders[str(getattr(tool, "name", ""))] = recorder
    decided: list[tuple[Any, str | None]] = []
    for item in list(getattr(result, "interruptions", []) or []):
        raw = getattr(item, "arguments", None)
        name = str(getattr(item, "tool_name", "") or getattr(item, "name", "") or "")
        recorder = recorders.get(name)
        if recorder is None or _readable_arguments(raw):
            continue
        parsed, errors = parse_arguments(raw if isinstance(raw, str) else None)
        if parsed is not None:
            errors = recorder.prepare_arguments(name, parsed).errors
        if errors:
            call_id = str(getattr(item, "call_id", "") or "")
            payload = errors_payload(errors)
            recorder.reject_call(
                ToolCall(name=name, arguments=parsed or {}, trace_id=call_id), payload
            )
            decided.append((item, json.dumps(payload, ensure_ascii=False)))
        elif name != CONFIRMATION_TOOL and not recorder.needs_approval(name, parsed or {}):
            decided.append((item, None))
    if not decided:
        return None
    state = result.to_state()
    for item, rejection in decided:
        if rejection is None:
            state.approve(item)
        else:
            state.reject(item, rejection_message=rejection)
    return state


def conversation_input(run_id: str, goal: str) -> str | list[Any]:
    """同じ会話の前の往復を付けた入力（会話が無ければ質問の文字列だけ。#768）。

    前の往復は共通の予算（`PLATFORM_CHAT_HISTORY_*`。新しい往復から詰め、古い往復を落とす）で
    選び、未信頼のデータ（`<conversation_history untrusted="true">`）として質問の前の利用者の
    メッセージに入れる（3 製品共通。#1818）。前の回答はツールの結果（文書・SQL の結果）に由来する
    文を含むので、assistant の発話としては渡さない。
    """
    from app.features.agent.runtime import runtime_repository

    history = build_chat_history(
        runtime_repository.thread_history(run_id), ChatHistoryBudget.from_settings(get_settings())
    )
    if not history:
        return goal
    return [
        {"role": "user", "content": f"{CHAT_HISTORY_UNTRUSTED_NOTE}\n{history}"},
        {"role": "user", "content": goal},
    ]


@dataclass
class _SupportTaskRun:
    """1 回の実行（開始・承認後の再開）の支援タスク（#1243）。"""

    goal: str
    previous: dict[str, Any] | None
    budget: SupportTaskBudget
    instructions: str
    max_rag_calls_per_run: int
    max_tool_calls_per_task: int
    # 回答前の確認（#1839）: 往復の残り・カードの答え（次の Run の metadata）・答えた RAG の条件。
    confirmation: ConfirmationBudget | None = None
    response: dict[str, Any] | None = None
    answered_conditions: dict[str, str] | None = None

    def known_conditions(self) -> dict[str, str]:
        """前の Run までに分かった条件と、この Run で利用者がカードで答えた条件。"""
        return {**support_task_known_conditions(self.previous), **(self.answered_conditions or {})}


def confirmation_budget(run: Any, agent: Any) -> ConfirmationBudget:
    """この Run の回答前の確認の往復（業務 Agent の方針・上限、無人の Run、確認せずに回答）。"""
    from app.features.agent.runtime import EVALUATION_DRY_RUN_KEY

    metadata = run.metadata if isinstance(run.metadata, dict) else {}
    response = metadata.get(RESPONSE_METADATA_KEY)
    confirmations = list(getattr(run, "confirmations", []) or [])
    # 利用者が「確認せずに回答」を選んだ Run は、聞き直さずに前提を示して答える: ツールの結果の
    # 確認のスキップで作った次の Run と、Agent 自身の確認のスキップで再開した Run（#1903。
    # その Run の残りでも、NL2SQL・RAG の確認を聞かない）。
    skipped = (
        isinstance(response, dict) and response.get("action") == "skip"
    ) or agent_confirmation_skipped(confirmations)
    budget = ConfirmationBudget(
        policy=str(getattr(agent, "clarification_policy", "confirm")),
        max_rounds=int(getattr(agent, "clarification_max_rounds", 0) or 0),
        used=used_rounds(metadata, confirmations),
        unattended=bool(metadata.get(EVALUATION_DRY_RUN_KEY)),
        skipped=skipped,
        evaluation=bool(metadata.get(EVALUATION_DRY_RUN_KEY)),
    )
    budget.known_calls = {
        item.call_id
        for item in confirmations
        if item.source == SOURCE_AGENT and item.call_id and item.status in {"answered", "skipped"}
    }
    return budget


def _support_task_run(run: Any, agent: Any = None) -> _SupportTaskRun:
    """前の Run の状態と、この Run の step から予算の残りを作る。

    再開した Run は、それまでの step を数えるので消費を 0 に戻さない。
    """
    from app.features.agent.runtime import runtime_repository

    settings = get_settings()
    max_rag = int(settings.agent_max_rag_calls_per_run)
    max_task = int(settings.agent_max_tool_calls_per_task)
    goal, previous = runtime_repository.support_task_context(run.id)
    response = run.metadata.get(RESPONSE_METADATA_KEY) if isinstance(run.metadata, dict) else None
    response = response if isinstance(response, dict) else None
    return _SupportTaskRun(
        goal=goal,
        previous=previous,
        budget=SupportTaskBudget.for_run(
            list(run.steps),
            previous,
            max_rag_calls_per_run=max_rag,
            max_tool_calls_per_task=max_task,
        ),
        instructions=support_task_instructions(
            previous,
            goal=goal,
            max_rag_calls_per_run=max_rag,
            max_tool_calls_per_task=max_task,
            # 範囲で 1 つに決まっている接続は、前のプロファイルを出さない（#1880）。
            fixed_profile_connections=data_scope.fixed_profile_connections(
                getattr(agent, "data_scopes", None)
            ),
        ),
        max_rag_calls_per_run=max_rag,
        max_tool_calls_per_task=max_task,
        confirmation=confirmation_budget(run, agent) if agent is not None else None,
        response=response,
        answered_conditions=rag_conditions(response or {}),
    )


def _save_support_task(run_id: str, task: _SupportTaskRun) -> dict[str, Any] | None:
    """支援タスクの状態を Run の成果物に残す（ツールを呼ばず、引き継ぐ状態も無ければ残さない）。

    状態は補助のため、作れなくても Run は止めない。残した状態を返す（残さなければ None）。
    """
    from app.features.agent.runtime import run_thread_owner, runtime_repository

    try:
        run = runtime_repository.get_run(run_id)
        cancellations = _run_cancellations(run)
        if (
            task.previous is None
            and not has_support_task_activity(run.steps)
            and not cancellations
            and not task.answered_conditions
        ):
            return None
        content = build_support_task(
            task.previous,
            steps=run.steps,
            run_id=run.id,
            thread_id=run.thread_id,
            # 状態は会話の持ち主のもの（system_admin が続けた Run も同じ会話の状態。#1928）。
            owner_user_uuid=run_thread_owner(run),
            goal=task.goal,
            max_rag_calls_per_run=task.max_rag_calls_per_run,
            max_tool_calls_per_task=task.max_tool_calls_per_task,
            cancellations=cancellations,
            answered_conditions=task.answered_conditions,
            ask_rag=task.confirmation is None or task.confirmation.asks,
            ask_nl2sql=task.confirmation is None or not task.confirmation.skipped,
        )
        runtime_repository.save_support_task(run_id, content)
        return content
    except Exception as exc:  # noqa: BLE001 - 状態は補助。残せなくても回答は返す
        logger.warning(
            "builtin_runtime_support_task_failed",
            extra={"run_id": run_id, "exception_type": type(exc).__name__},
        )
        return None


def _progress_listener(run: Any) -> SpanListener:
    """実行区間の span の知らせを、共通の span の表に書く listener（#3226 の案 A）。

    Run が受け付けた知らせ（終わった Run・置き換わった実行の知らせを除く）だけを、Run を実行した
    利用者を持ち主にして書く。表に入れない工程の詳細などは Run に残す（`record_builtin_span`）。
    """
    from app.features.agent.runtime import runtime_repository

    run_id = run.id
    owner = run_trace_owner(run_id, run.created_by_user_uuid)

    def listen(update: SpanUpdate) -> None:
        if runtime_repository.record_builtin_span(run_id, update) and owner is not None:
            trace_spans.submit(update, owner)

    return listen


def record_skipped_tool(
    run_id: str,
    call: ToolCall,
    *,
    step_id: str | None = None,
    result: ToolResult | None = None,
    rejected: bool = False,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> None:
    """実行しなかったツールの呼び出し（却下・方針で拒否・上限・引数の誤り）を、`skipped` の
    ツールの span として残す（#3226。処理の経過のツールの行）。回答前の確認のツールは残さない。
    """
    from app.features.agent.runtime import runtime_repository

    if call.name == CONFIRMATION_TOOL:
        return
    now = datetime.now(UTC)
    record_span(
        EXECUTE_TOOL_SPAN,
        started_at=started_at or now,
        finished_at=finished_at or now,
        status="skipped",
        attributes=tool_attributes(call.name, call.trace_id),
        display=tool_display(
            call,
            runtime_repository.builtin_tool_titles(run_id),
            result=result,
            run_step=step_id,
            rejected=rejected,
        ),
    )


def _record_wait_spans(run: Any) -> None:
    """再開した区間の始めに、終わった承認・回答前の確認の待ちを span で記録する（#3226）。

    待ちの区間（求めた時刻から決めた時刻まで）を、承認を求めた回・確認の回ごとに 1 回だけ残す
    （再開し直しても重ねない）。却下したツールは実行しなかったツールとして待ちの後に残す。
    """
    titles = tool_titles(run)
    steps = {step.id: step for step in run.steps}
    recorded = recorded_wait_rounds(run, KIND_APPROVAL)
    for index, round_ in enumerate(approval_rounds(list(run.approvals))):
        decided = [item.decided_at for item in round_ if item.decided_at is not None]
        if index + 1 in recorded or not decided or len(decided) < len(round_):
            continue
        record_span(
            APPROVAL_WAIT_SPAN,
            started_at=min(item.created_at for item in round_),
            finished_at=max(decided),
            display=approval_wait_display(round_, index, titles),
        )
        for approval in round_:
            if str(approval.status) != "rejected":
                continue
            step = steps.get(approval.step_id) if approval.step_id else None
            record_skipped_tool(
                run.id,
                step.tool_call if step is not None and step.tool_call else approval.tool_call,
                step_id=step.id if step is not None else None,
                result=step.tool_result if step is not None else None,
                rejected=True,
                started_at=approval.decided_at,
                finished_at=approval.decided_at,
            )
    answered = recorded_wait_rounds(run, KIND_CONFIRMATION)
    for confirmation in run.confirmations:
        if (
            confirmation.source != "agent"
            or confirmation.status not in {"answered", "skipped"}
            or confirmation.decided_at is None
            or max(1, confirmation.round) in answered
        ):
            continue
        record_span(
            CONFIRMATION_WAIT_SPAN,
            started_at=confirmation.created_at,
            finished_at=confirmation.decided_at,
            display=confirmation_wait_display(confirmation),
        )


async def execute_run(run_id: str, *, claim: RunClaim | None = None) -> None:
    """Run を最初から実行する（作成直後・dispatcher から呼ぶ）。

    `claim` は dispatcher が claim した所有の証明（#2606）。開始は今の claim に対する条件付きの
    遷移で、開始できなければ何もしない。開始した実行の書き戻しは、この実行の token で守る。
    """
    with bind_log_context(run_id=run_id):
        from app.features.agent.runtime import runtime_repository

        if claim is None:
            started = runtime_repository.begin_builtin_run(run_id)
        else:
            # 保存先の head を lock して判定・書き込みをする。event loop を塞がない。
            started = await asyncio.to_thread(
                runtime_repository.begin_builtin_run, run_id, claim=claim
            )
        if started is None:
            return
        run, agent = started
        # 最初の区間は新しい trace（`trace_context` は空）で、受け付けた要求の span は span link に
        # だけ残す（#3272。外部の要求の traceparent を Run の trace の親にしない）。
        with (
            attach_trace_context(run.trace_context),
            operation_trace(
                "agent.execute",
                operation_id=run.id,
                listener=_progress_listener(run),
                links=traceparent_links(run.trace_link_context.get("traceparent")),
            ),
        ):
            execution = run_claim.bind_execution(run.id, run.metadata)
            budget_token = _begin_run_budget(run)
            # 公開中の版に固定したスキルで動く（#1855）。
            pinned = use_pinned_skills(agent.pinned_skills)
            result: Any = None
            try:
                task = _support_task_run(run, agent)
                cancellations = await _apply_clarification_cancel(
                    run, task.previous, agent.data_scopes
                )
                sdk_agent = await asyncio.to_thread(
                    build_sdk_agent,
                    run_id,
                    name=agent.name,
                    instructions=agent.instructions,
                    skill_ids=agent.skill_ids,
                    model_id=agent.model_id,
                    agent_id=agent.id,
                    user_uuid=run.created_by_user_uuid,
                    budget=task.budget,
                    support_task=task.instructions,
                    goal=support_task_goal(run.goal, task.previous),
                    known_conditions=task.known_conditions(),
                    data_scopes=agent.data_scopes,
                    cancellations=cancellations,
                    confirmation=task.confirmation,
                    confirmation_response=task.response,
                    thread_id=run.thread_id,
                    output_fields=agent.output_fields,
                    caller_planning=agent.caller_planning_allowed,
                )
                result = await _run_sdk(run_id, sdk_agent, conversation_input(run_id, run.goal))
                result = await _unattended_approvals(run, sdk_agent, result, agent.data_scopes)
                result, refused = await _require_tool_grounding(
                    run, sdk_agent, result, agent.data_scopes, assume=_assumes(task.confirmation)
                )
                if not refused:
                    _raise_on_grounding_failure(run.id, result)
                    result = await _require_hops(
                        run, sdk_agent, result, agent.data_scopes, task=task
                    )
                    result = await _require_output(
                        run, sdk_agent, result, agent.data_scopes, agent.output_fields
                    )
                _record_usage(run_id, result, agent.model_id)
                await _finish(
                    run_id,
                    result,
                    task,
                    rag_tools=has_rag_evidence_tools(sdk_agent, agent.skill_ids, cancellations),
                    data_scopes=agent.data_scopes,
                    refusal=_refusal_text(agent, sdk_agent) if refused else None,
                )
            except Exception as exc:  # noqa: BLE001 - 実行の境界では失敗を Run に記録する
                _record_usage(run_id, getattr(exc, "run_data", None) or result, agent.model_id)
                _record_failure(run_id, exc)
            finally:
                final_status: str | None = None
                with suppress(KeyError):
                    final_status = runtime_repository.get_run(run_id).status.value
                if final_status == "cancelled":
                    # 取り消した Run が始めた NL2SQL のジョブを止める（#3318。取消の後に届いた
                    # nl2sql_query の結果のジョブも、ここで分かる）。
                    await cancel_run_nl2sql_jobs(run_id, agent.data_scopes)
                    finish_operation_trace(run_id, status="cancelled")
                reset_pinned_skills(pinned)
                _ACTIVE_RUN_BUDGET.reset(budget_token)
                run_claim.release_execution(execution)


async def resume_run(run_id: str, *, claim: RunClaim | None = None) -> None:
    """承認がすべて決まった Run を、保存した SDK の状態から再開する（`claim` は `execute_run`）。"""
    with bind_log_context(run_id=run_id):
        from app.features.agent.runtime import runtime_repository

        if claim is None:
            resumed = runtime_repository.begin_builtin_resume(run_id)
        else:
            resumed = await asyncio.to_thread(
                runtime_repository.begin_builtin_resume, run_id, claim=claim
            )
        if resumed is None:
            return
        run, agent, state_text, decisions = resumed
        with (
            attach_trace_context(run.trace_context),
            operation_trace("agent.resume", operation_id=run.id, listener=_progress_listener(run)),
        ):
            execution = run_claim.bind_execution(run.id, run.metadata)
            # 終わった承認・確認の待ちは、再開した区間の span として残す（#3226）。表示の記録
            # なので、残せなくても再開は止めない。
            try:
                _record_wait_spans(run)
            except Exception as exc:  # noqa: BLE001 - 処理の経過の記録は補助
                logger.warning(
                    "builtin_runtime_wait_span_failed",
                    extra={"run_id": run_id, "exception_type": type(exc).__name__},
                )
            budget_token = _begin_run_budget(run)
            pinned = use_pinned_skills(agent.pinned_skills)
            result: Any = None
            try:
                if decisions and not any(decisions.values()):
                    runtime_repository.complete_builtin_run(
                        run_id,
                        "承認が却下されたため、対象の処理は実行していません。",
                        outcome=tool_grounding.refusal_outcome(declined=True),
                    )
                    return
                task = _support_task_run(run, agent)
                # 取り消しは実行の始めに済んでいる（session も閉じた）。記録だけを使う。
                cancellations = _run_cancellations(run)
                sdk_agent = await asyncio.to_thread(
                    build_sdk_agent,
                    run_id,
                    name=agent.name,
                    instructions=agent.instructions,
                    skill_ids=agent.skill_ids,
                    model_id=agent.model_id,
                    agent_id=agent.id,
                    user_uuid=run.created_by_user_uuid,
                    budget=task.budget,
                    support_task=task.instructions,
                    goal=support_task_goal(run.goal, task.previous),
                    known_conditions=task.known_conditions(),
                    data_scopes=agent.data_scopes,
                    cancellations=cancellations,
                    confirmation=task.confirmation,
                    confirmation_response=task.response,
                    thread_id=run.thread_id,
                    output_fields=agent.output_fields,
                    caller_planning=agent.caller_planning_allowed,
                )
                rejected = not all(decisions.values())
                if rejected:
                    # 承認済みの同時呼出しだけを実行し、却下を再提案するモデルのターンを作らない。
                    sdk_agent.tool_use_behavior = "stop_on_first_tool"
                state = await RunState.from_string(sdk_agent, state_text)
                for item in state.get_interruptions():
                    call_id = str(getattr(item, "call_id", "") or "")
                    if decisions.get(call_id, False):
                        state.approve(item)
                    else:
                        state.reject(item)
                result = await _run_sdk(run_id, sdk_agent, state)
                if rejected:
                    _record_usage(run_id, result, agent.model_id)
                    runtime_repository.complete_builtin_run(
                        run_id,
                        "却下された処理は実行していません。承認された処理の結果は実行の経過で確認してください。",
                        outcome=tool_grounding.refusal_outcome(declined=True),
                    )
                    return
                result = await _unattended_approvals(run, sdk_agent, result, agent.data_scopes)
                result, refused = await _require_tool_grounding(
                    run, sdk_agent, result, agent.data_scopes, assume=_assumes(task.confirmation)
                )
                if not refused:
                    _raise_on_grounding_failure(run.id, result)
                    result = await _require_hops(
                        run, sdk_agent, result, agent.data_scopes, task=task
                    )
                    result = await _require_output(
                        run, sdk_agent, result, agent.data_scopes, agent.output_fields
                    )
                _record_usage(run_id, result, agent.model_id)
                await _finish(
                    run_id,
                    result,
                    task,
                    rag_tools=has_rag_evidence_tools(sdk_agent, agent.skill_ids, cancellations),
                    data_scopes=agent.data_scopes,
                    refusal=_refusal_text(agent, sdk_agent) if refused else None,
                )
            except Exception as exc:  # noqa: BLE001 - 実行の境界では失敗を Run に記録する
                _record_usage(run_id, getattr(exc, "run_data", None) or result, agent.model_id)
                _record_failure(run_id, exc)
            finally:
                final_status: str | None = None
                with suppress(KeyError):
                    final_status = runtime_repository.get_run(run_id).status.value
                if final_status == "cancelled":
                    # 取り消した Run が始めた NL2SQL のジョブを止める（#3318。取消の後に届いた
                    # nl2sql_query の結果のジョブも、ここで分かる）。
                    await cancel_run_nl2sql_jobs(run_id, agent.data_scopes)
                    finish_operation_trace(run_id, status="cancelled")
                reset_pinned_skills(pinned)
                _ACTIVE_RUN_BUDGET.reset(budget_token)
                run_claim.release_execution(execution)


def _run_cancellations(run: Any) -> list[dict[str, Any]]:
    """Run の記録の、利用者が取り消した確認（#1441・#1445）。"""
    records = run.metadata.get(CANCEL_METADATA_KEY) if isinstance(run.metadata, dict) else None
    if not isinstance(records, list):
        return []
    return [
        record
        for record in records
        if isinstance(record, dict) and record.get("kind") in CLARIFICATION_KINDS
    ]


async def _apply_clarification_cancel(
    run: Any,
    previous: Mapping[str, Any] | None,
    data_scopes: data_scope.AgentDataScopes | None = None,
) -> list[dict[str, Any]]:
    """この Run で取り消す確認を決め、NL2SQL の確認の session を閉じ、Run に記録する（#1441）。

    取り消しは、取り消しの操作で作った Run（`clarification_cancel`）と、確かめ中の問いがあるときの
    取り消しの文（「データベースは関係ない」など）。取り消す確認が無ければ何もしない。
    """
    from app.features.agent.runtime import runtime_repository

    triggers: dict[str, str] = {kind: TRIGGER_BUTTON for kind in button_cancel_kinds(run.metadata)}
    for kind in message_cancel_kinds(run.goal, previous):
        triggers.setdefault(kind, TRIGGER_MESSAGE)
    if not triggers:
        return []
    at = datetime.now(UTC).isoformat()
    records: list[dict[str, Any]] = []
    for kind in CLARIFICATION_KINDS:
        if kind not in triggers:
            continue
        pending = pending_of(previous, kind)
        record = cancel_record(kind, trigger=triggers[kind], run_id=run.id, at=at, pending=pending)
        if kind == KIND_NL2SQL:
            for index, session in enumerate(record["requirement_sessions"]):
                session_id = session["requirement_session_id"]
                connection = next(
                    (
                        str(item.get("connection") or "")
                        for item in pending
                        if item.get("requirement_session_id") == session_id
                    ),
                    "",
                )
                session["status"] = await _cancel_requirement_session(
                    run,
                    connection or data_scope.NL2SQL_CONNECTION_ID,
                    session_id,
                    trace_id=f"{CANCEL_TRACE_PREFIX}{run.id}_{index}",
                    data_scopes=data_scopes,
                )
        records.append(record)
    runtime_repository.record_clarification_cancel(run.id, records)
    logger.info(
        "builtin_runtime_clarification_cancelled",
        extra={
            "run_id": run.id,
            "kinds": [record["kind"] for record in records],
            "triggers": [record["trigger"] for record in records],
        },
    )
    return records


async def _control_plane_nl2sql_call(
    run: Any,
    connection: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    trace_id: str,
    data_scopes: data_scope.AgentDataScopes | None = None,
) -> ToolResult | None:
    """Control Plane が NL2SQL の後始末のツールを Run の利用者のサービストークンで呼ぶ。

    ポリシー・監査・step はモデルのツールと同じ境界で、予算には数えない。Run を取り消した後でも
    送る（`cleanup`。#2609）。接続・ツールが無ければ None。
    """
    function_name = f"{connection}{MCP_TOOL_SEPARATOR}{tool_name}"
    config = next(
        (
            item
            for item in runtime_config_store.list_mcp_servers()
            if mcp_function_name(item.server_id, tool_name) == function_name
        ),
        None,
    )
    if config is None:
        return None
    context = _run_tool_context(run)
    try:
        listed = await asyncio.to_thread(
            list_mcp_connection_tools, config.server_id, context=context
        )
    except (KeyError, ExternalToolError):
        return None
    tool = next((item for item in listed.tools if item.name == tool_name), None)
    if tool is None:
        return None
    definition = mcp_tool_definition(config, tool)
    recorder = _ToolRecorder(run.id)
    recorder.data_scopes = dict(data_scopes or {})
    _step_id, result = await recorder.call(
        ToolCall(name=definition.name, arguments=arguments, trace_id=trace_id),
        definition=definition,
        handler=mcp_tool_handler(config, tool),
        cleanup=True,
    )
    return result


async def _cancel_requirement_session(
    run: Any,
    connection: str,
    session_id: str,
    *,
    trace_id: str,
    data_scopes: data_scope.AgentDataScopes | None = None,
) -> str:
    """NL2SQL の SQL の生成前の確認の session を閉じる（`nl2sql_cancel_requirement`。#1441）。

    閉じられなくても Run は続ける（session は操作が途絶えると期限で閉じる）。確認をやめた後の
    session の後始末は、Run を取り消した後でも閉じる（#2609）。返すのは記録に残す状態
    （cancelled / closed / failed / unavailable）。
    """
    result = await _control_plane_nl2sql_call(
        run,
        connection,
        NL2SQL_CANCEL_TOOL,
        {"requirement_session_id": session_id},
        trace_id=trace_id,
        data_scopes=data_scopes,
    )
    if result is None:
        return "unavailable"
    if not result.success or not isinstance(result.output, dict):
        return "failed"
    status = result.output.get("status")
    return status if isinstance(status, str) and status else "cancelled"


async def cancel_run_nl2sql_jobs(
    run_id: str, data_scopes: data_scope.AgentDataScopes | None = None
) -> None:
    """取り消した Run が始めて、まだ終わっていない NL2SQL のジョブを止める（#3318）。

    Run の取消（#2691）では呼び先の NL2SQL のジョブは止まらないため、Control Plane が
    `nl2sql_cancel_job` を Run の利用者のサービストークンで送る（`job_cancel`）。`data_scopes` は
    Run が使う版のデータの範囲（ジョブを作ったときと同じ claim。省略すると Run の版から引く）。
    止められなくても取消は済んでいる（ジョブは worker の上限で終わる）。
    """
    from app.features.agent.runtime import RunStatus, agent_for_run, runtime_repository

    with bind_log_context(run_id=run_id):
        try:
            run = await asyncio.to_thread(runtime_repository.get_run, run_id)
            if run.status != RunStatus.CANCELLED:
                return
            jobs = in_flight_nl2sql_jobs(list(run.steps))
            if not jobs:
                return
            if data_scopes is None:
                agents = await asyncio.to_thread(runtime_repository.list_agents)
                agent = next((item for item in agents if item.id == run.agent_id), None)
                data_scopes = agent_for_run(agent, run).data_scopes if agent is not None else None
            cancelled: list[str] = []
            for job_id in jobs:
                result = await _control_plane_nl2sql_call(
                    run,
                    data_scope.NL2SQL_CONNECTION_ID,
                    NL2SQL_CANCEL_JOB_TOOL,
                    {"job_id": job_id},
                    trace_id=f"{RUN_CANCEL_TRACE_PREFIX}{run.id}_{job_id}",
                    data_scopes=data_scopes,
                )
                if result is not None and result.success:
                    cancelled.append(job_id)
            logger.info(
                "builtin_runtime_nl2sql_jobs_cancelled",
                extra={"run_id": run_id, "requested": len(jobs), "cancelled": len(cancelled)},
            )
        except Exception as exc:  # noqa: BLE001 - 止められなくても Run の取消は済んでいる
            logger.warning(
                "builtin_runtime_nl2sql_job_cancel_failed",
                extra={"run_id": run_id, "exception_type": type(exc).__name__},
            )


async def close_requirement_sessions(run_id: str, sessions: list[dict[str, Any]]) -> None:
    """「確認をやめる」で外した NL2SQL の SQL の生成前の確認の session を閉じる（#1839）。

    閉じられなくても利用者の操作は済んでいる（session は操作が途絶えると期限で閉じる）。
    """
    from app.features.agent.runtime import runtime_repository

    with bind_log_context(run_id=run_id):
        try:
            run = runtime_repository.get_run(run_id)
            agent = next(
                (item for item in runtime_repository.list_agents() if item.id == run.agent_id),
                None,
            )
            for index, session in enumerate(sessions):
                await _cancel_requirement_session(
                    run,
                    str(session.get("connection") or "") or data_scope.NL2SQL_CONNECTION_ID,
                    str(session.get("requirement_session_id") or ""),
                    trace_id=f"{CANCEL_TRACE_PREFIX}{run.id}_withdrawn_{index}",
                    data_scopes=agent.data_scopes if agent is not None else None,
                )
        except Exception as exc:  # noqa: BLE001 - 閉じられなくても期限で閉じる
            logger.warning(
                "builtin_runtime_withdrawn_session_close_failed",
                extra={"run_id": run_id, "exception_type": type(exc).__name__},
            )


def _scoped_call(call: ToolCall, data_scopes: data_scope.AgentDataScopes | None) -> ToolCall:
    """承認・dry-run に記録する呼び出しに、データの範囲を当てる（実行するときと同じ引数。#1378）。

    範囲外（拒否）の呼び出しはモデルの引数のまま記録する（実行するときにツールのエラーになる）。
    """
    scoped = data_scope.enforce(call.name, call.arguments, data_scopes)
    if scoped is None:
        return call
    arguments = call.arguments if scoped.error is not None else scoped.arguments
    return call.model_copy(update={"arguments": arguments, "data_scope": scoped.note})


async def _unattended_approvals(
    run: Any,
    sdk_agent: Agent[Any],
    result: Any,
    data_scopes: data_scope.AgentDataScopes | None = None,
) -> Any:
    """無人の Run は、承認が要るツールを実行せずに続ける（承認待ちにしない）。

    - 品質評価の Run（#776。dry-run）: 呼ぼうとしたツールは step に残し（ツールの選択の判定に
      使う）、モデルへは「評価中のため実行していない」と返して回答を完成させる。
    - 外部の MCP クライアントの Run（#1874。`metadata.source=mcp`）: 承認する人が画面に
      いるとは限らないため、承認が要るツールは呼ばない（モデルへは「使えない」と返し、
      確かめられなかった点として回答を完成させる）。step（監査）と Run の経過に、承認が
      要るため使わなかったことを残す。

    それ以外の Run（画面のチャット・実行）はそのまま返す（承認待ちにする）。
    """
    from app.features.agent.runtime import (
        EVALUATION_DRY_RUN_KEY,
        EVALUATION_DRY_RUN_MESSAGE,
        MCP_APPROVAL_SKIPPED_CODE,
        is_mcp_run,
        mcp_approval_message,
        runtime_repository,
    )

    evaluation = bool(run.metadata.get(EVALUATION_DRY_RUN_KEY))
    mcp = not evaluation and is_mcp_run(run.metadata)
    if not evaluation and not mcp:
        return result
    for _ in range(_max_turns()):
        interruptions = list(getattr(result, "interruptions", []) or [])
        if mcp:
            # MCP の Run は回答前の確認（`ask_user_confirmation`。#1839）を出せる（呼び出し元の
            # クライアントが agent_answer_confirmation で答える）。確認は残し、承認だけを断る
            # （SDK は決めていない中断を次の結果にもう一度返す）。
            interruptions = [
                item for item in interruptions if _interruption_call(item).name != CONFIRMATION_TOOL
            ]
        if not interruptions:
            return result
        calls = [_scoped_call(_interruption_call(item), data_scopes) for item in interruptions]
        if evaluation:
            runtime_repository.record_builtin_dry_run_steps(run.id, calls)
        else:
            runtime_repository.record_builtin_unattended_skips(run.id, calls)
        # 実行しなかったツールとして処理の経過に残す（#3226）。
        skipped_steps = {
            step.tool_call.trace_id: step
            for step in runtime_repository.builtin_tool_steps(
                run.id, [call.trace_id or "" for call in calls]
            )
            if step.tool_call is not None
        }
        for call in calls:
            skipped = skipped_steps.get(call.trace_id or "")
            record_skipped_tool(
                run.id,
                call,
                step_id=skipped.id if skipped is not None else None,
                result=skipped.tool_result if skipped is not None else None,
            )
            logger.info(
                "builtin_runtime_tool_call_rejected",
                extra={
                    "run_id": run.id,
                    "tool_names": [call.name for call in calls],
                    "error_code": MCP_APPROVAL_SKIPPED_CODE,
                },
            )
        state = result.to_state()
        for item, call in zip(interruptions, calls, strict=True):
            state.reject(
                item,
                rejection_message=(
                    EVALUATION_DRY_RUN_MESSAGE if evaluation else mcp_approval_message(call.name)
                ),
            )
        result = await _run_sdk(run.id, sdk_agent, state)
    return result


def _answer_text(output: object) -> str:
    return (
        output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
    )


def _assumes(budget: ConfirmationBudget | None) -> bool:
    """利用者に確認しない Run か（方針「前提を示して回答」・「確認せずに回答」を選んだ。#1903）。

    無人の Run（品質評価）も含める（#1959）。利用者に聞けないので、本文の聞き返し
    （「どの業務プロファイルを使いますか？」）も差し戻し、前提を示して答えさせる（#1839）。
    """
    return budget is not None and (budget.policy == POLICY_ASSUME or budget.unattended)


async def _require_tool_grounding(
    run: Any,
    sdk_agent: Agent[Any],
    result: Any,
    data_scopes: data_scope.AgentDataScopes | None = None,
    *,
    assume: bool = False,
) -> tuple[Any, bool]:
    """根拠・データのツールを呼ばずに作った回答を差し戻し、それでも呼ばなければ拒答にする（#1835）。

    根拠・データのツールを持つ Run で、モデルがどれも呼ばずに最終の回答を作ったら（確認の質問
    だけの回答は除く。`tool_grounding.needs_grounding`）、`tool_grounding.MAX_RETRIES` 回まで
    「ツールで確かめてから答える。確かめられないなら拒答する」を利用者のメッセージとして足して
    Run を続ける（同じ SDK の Agent・ツール・予算。利用量は前の分に積む）。続けた Run が承認で
    止まったら、そのまま返す（承認待ち。無人の Run は `_unattended_approvals` で続ける）。
    差し戻しても呼ばなければ、2 つ目の値を True にして返す（`_finish` が拒答の文に置き換える）。
    差し戻しと元の回答は Run の経過に残す。差し戻しの文は、この Run でモデルに回答前の確認の
    ツール（`ask_user_confirmation`）を渡したかで分ける（#1881。`tool_grounding.retry_message`）。
    差し戻しの後も、確認のツールを渡したのに本文で確認の質問だけを返した回答は、拒答にせず、
    Run の経過に残す。
    確認のツールを渡した Run（#1881）と `assume`（利用者に確認しない Run。#1903。`_assumes`）では、
    ツールを呼ばずに本文の確認の質問だけを返した回答も 1 回差し戻す（あいさつへの返事は除く）。
    `assume` の差し戻しの文は「確認の質問はせず、ツールで調べ、決まらない点は前提を示して答える」。
    差し戻しの後も本文の聞き返しだけなら、拒答にはせず、Run の経過に残してそのまま出す。
    回答の結論が範囲外・答えられないと述べる回答（調べずに判断した拒答。`tool_grounding.declined`。
    #2608）は、範囲の内外と根拠の有無をツールの結果で判断させる文で差し戻し、経過に `declined` を
    残す。
    """
    from app.features.agent.runtime import runtime_repository

    if not aid_enabled(run.id, "tool_grounding"):
        return result, False
    tool_names = [str(getattr(tool, "name", "")) for tool in sdk_agent.tools]
    if not tool_grounding.grounding_tools(tool_names):
        return result, False
    confirmable = CONFIRMATION_TOOL in tool_names
    # 確認のツールを渡した Run（#1881）と利用者に確認しない Run（#1903）は、本文の確認の質問だけの
    # 回答も差し戻す（あいさつへの返事は除く）。差し戻しの後も本文の聞き返しなら、拒答にせず出す。
    text_clarification_retried = confirmable or assume
    while not list(getattr(result, "interruptions", []) or []):
        answer = _answer_text(result.final_output)
        current = runtime_repository.get_run(run.id)
        if declined_kinds(current.metadata):
            # 利用者が確認を取り消し（「資料の検索は不要」など。#1441・#1445）、その手段を使わずに
            # 続けると決めた Run。ほかの手段で続けるか意図を聞き直す回答を拒答にしない。
            return result, False
        steps = list(current.steps)
        if not tool_grounding.needs_grounding(
            answer,
            steps=steps,
            tool_names=tool_names,
            clarification_exempt=not text_clarification_retried,
            question=str(getattr(current, "goal", "") or ""),
        ):
            if tool_grounding.retried(current.metadata) and tool_grounding.clarified_in_text(
                answer, steps=steps, tool_names=tool_names
            ):
                # 差し戻しの後も、確認のカードではなく本文で聞き返した（#1881）。拒答にはせず、
                # 経過に残す（差し戻していない回答のあいさつ・用件の聞き返しは残さない）。
                runtime_repository.record_tool_grounding(run.id, "text_clarification", answer)
            return result, False
        if _grounded_in_thread(run.id):
            # 同じ会話の前の Run がツールで答えた。前の回答の言い換え・整形を拒答にしない。
            return result, False
        if tool_grounding.retried(current.metadata) >= tool_grounding.MAX_RETRIES:
            if text_clarification_retried and is_clarification_only(answer):
                # 差し戻しの後も本文で聞き返した（#1881・#1903）。対象が曖昧な質問の聞き返しは
                # 拒答にせず、経過に残してそのまま出す。
                runtime_repository.record_tool_grounding(run.id, "text_clarification", answer)
                return result, False
            # 調べずに範囲外・答えられないと判断した拒答（#2608）は、ツールで確かめずに答えた回答の
            # 拒答と分けて残す（回答の対応の basis は `_finish` が metadata から決める）。
            declined = tool_grounding.declined(answer)
            runtime_repository.record_tool_grounding(run.id, "refused", answer, declined=declined)
            logger.info(
                "builtin_runtime_tool_grounding_refused",
                extra={"run_id": run.id, "tool_names": tool_names, "declined": declined},
            )
            return result, True
        declined = tool_grounding.declined(answer)
        runtime_repository.record_tool_grounding(run.id, "retry", answer, declined=declined)
        logger.info(
            "builtin_runtime_tool_grounding_retry",
            extra={"run_id": run.id, "tool_names": tool_names, "declined": declined},
        )
        # 利用量は新しい Run の文脈に前の分を足して続ける（承認待ちで保存する状態にも入る）。
        context: RunContextWrapper[Any] = RunContextWrapper(context=result.context_wrapper.context)
        context.usage.add(result.context_wrapper.usage)
        result = await _run_sdk(
            run.id,
            sdk_agent,
            tool_grounding.retry_input(
                result.to_input_list(),
                answer,
                confirmable=confirmable,
                assume=assume,
                declined=declined,
            ),
            context=context,
        )
        result = await _unattended_approvals(run, sdk_agent, result, data_scopes)
    return result, False


async def _require_hops(
    run: Any,
    sdk_agent: Agent[Any],
    result: Any,
    data_scopes: data_scope.AgentDataScopes | None = None,
    *,
    task: _SupportTaskRun | None = None,
) -> Any:
    """根拠の台帳・一覧の行の略号・別名を引かずに確定した回答を、1 回だけ差し戻す（#1958）。

    多段の質問で、1 回目の検索に出た台帳の行の略号（「担当部署: 安」）・略称（「HRM」）の意味を
    次の段で引かずに答えることがある（案内 `record_codes` を足しても従わない）。引いていない段が
    あれば（`support_task.unresolved_hops`）、その値を挙げて「要るなら引いてから答える」を利用者の
    メッセージとして足し、Run を続ける（拒答のガード #1835 と同じ形。同じ SDK の Agent・ツール・
    予算）。確認の質問だけの回答・承認で止まった Run・差し戻した後の Run は対象外。差し戻しと元の
    回答は Run の経過に残す。
    同じ 1 回の枠で、業務データのツールを持つのに引かずに資料に無いとして答えた回答
    （`support_task.missing_data_lookup`。#1979）も差し戻す（利用者が業務データの確認を取り消した
    Run は除く。経過には `data_retry`）。略号・別名が残っていれば、そちらを先にする（重ねない）。
    同じ枠で、照合した業務ガイドの手順のうち根拠を集めていない手順
    （`guide_steps.unresolved_guide_steps`。#2396。経過には `guide_step_retry`）も差し戻す
    （略号・別名と両方なら 1 つの差し戻しにまとめる）。
    同じ枠で、手順・一覧・条件のまとまりを求める質問で、回答に使った章節の続きを読んでいない
    （`section_coverage`。#2492。経過には `section_retry`）回答も差し戻す（ほかの差し戻しと両方なら
    1 つにまとめる）。この実行のツールの呼び出しが上限に達していれば差し戻さない（読めないので、
    `_finish` が読み切れていない章節を回答に示す）。取り消した Run は差し戻さない。
    """
    from app.features.agent.runtime import RunStatus, runtime_repository

    if list(getattr(result, "interruptions", []) or []):
        return result
    current = runtime_repository.get_run(run.id)
    if current.status == RunStatus.CANCELLED or tool_grounding.hops_retried(current.metadata):
        return result
    answer = _answer_text(result.final_output)
    if is_clarification_only(answer):
        return result
    pending = unresolved_hops(current.steps, answer, str(getattr(current, "goal", "") or ""))
    # 業務ガイドの手順のうち根拠を集めていない手順（#2396）。略号・別名と同じ 1 回の枠で、両方あれば
    # 1 つの差し戻しにまとめる。
    guide_pending = unresolved_guide_steps(current.steps)
    # 回答に使った章節の未読の続き（#2492）。予算が残っていて、読み直せるものだけ。
    sections = [
        item
        for item in _unread_sections(current, answer, task)
        if item["reason"] in RETRYABLE_REASONS
    ]
    if pending:
        head = unresolved_hops_message(pending)
        if guide_pending:
            head += "\n\n" + unresolved_guide_steps_message(guide_pending)
        if sections:
            head += "\n\n" + unread_sections_message(sections)
        runtime_repository.record_tool_grounding(run.id, "hop_retry", answer)
        logger.info(
            "builtin_runtime_unresolved_hops_retry",
            extra={"run_id": run.id, "values": [item["value"] for item in pending]},
        )
    elif guide_pending:
        head = unresolved_guide_steps_message(guide_pending)
        if sections:
            head += "\n\n" + unread_sections_message(sections)
        runtime_repository.record_tool_grounding(run.id, "guide_step_retry", answer)
        logger.info(
            "builtin_runtime_unresolved_guide_steps_retry",
            extra={"run_id": run.id, "steps": [item["id"] for item in guide_pending]},
        )
    elif sections:
        head = unread_sections_message(sections)
        runtime_repository.record_tool_grounding(run.id, "section_retry", answer)
        logger.info(
            "builtin_runtime_unread_sections_retry",
            extra={
                "run_id": run.id,
                "sections": [item["section_id"] for item in sections],
                "reasons": [item["reason"] for item in sections],
            },
        )
    elif KIND_NL2SQL not in declined_kinds(current.metadata) and missing_data_lookup(
        current.steps, answer, [str(getattr(tool, "name", "")) for tool in sdk_agent.tools]
    ):
        # 業務データを引かずに資料に無いとして答えた（#1979）。同じ 1 回の枠で差し戻す。
        head = DATA_LOOKUP_HINT
        runtime_repository.record_tool_grounding(run.id, "data_retry", answer)
        logger.info("builtin_runtime_data_lookup_retry", extra={"run_id": run.id})
    else:
        return result
    context: RunContextWrapper[Any] = RunContextWrapper(context=result.context_wrapper.context)
    context.usage.add(result.context_wrapper.usage)
    result = await _run_sdk(
        run.id,
        sdk_agent,
        tool_grounding.retry_input(result.to_input_list(), answer, head=head),
        context=context,
    )
    return await _unattended_approvals(run, sdk_agent, result, data_scopes)


async def _require_output(
    run: Any,
    sdk_agent: Agent[Any],
    result: Any,
    data_scopes: data_scope.AgentDataScopes | None,
    output_fields: Sequence[AgentOutputField],
) -> Any:
    """出力の項目を記録せずに作った回答を、1 回だけ差し戻す（#2549）。

    出力の項目を持つ業務 Agent で、`record_output` を呼ばずに回答した Run に、記録してから答え直す
    ことを利用者のメッセージとして足して続ける（ほかの差し戻しと同じ形。同じ SDK の Agent・ツール・
    予算）。確認の質問だけの回答・承認で止まった Run・取り消した Run・差し戻した後の Run は対象外。
    それでも記録しなければ、そのまま回答する（画面が記録の無いことを示す）。
    """
    from app.features.agent.runtime import RunStatus, runtime_repository

    if not output_fields or list(getattr(result, "interruptions", []) or []):
        return result
    current = runtime_repository.get_run(run.id)
    if (
        current.status == RunStatus.CANCELLED
        or output_retried(current.metadata)
        or output_recorded(current.artifacts)
    ):
        return result
    answer = _answer_text(result.final_output)
    if is_clarification_only(answer):
        return result
    runtime_repository.record_tool_grounding(run.id, "output_retry", answer)
    logger.info("builtin_runtime_output_retry", extra={"run_id": run.id})
    context: RunContextWrapper[Any] = RunContextWrapper(context=result.context_wrapper.context)
    context.usage.add(result.context_wrapper.usage)
    result = await _run_sdk(
        run.id,
        sdk_agent,
        tool_grounding.retry_input(result.to_input_list(), answer, head=OUTPUT_RETRY_MESSAGE),
        context=context,
    )
    return await _unattended_approvals(run, sdk_agent, result, data_scopes)


def _tool_budget_exhausted(budget: SupportTaskBudget | None) -> bool:
    """この実行（支援タスク）のツールの呼び出しが上限に達したか（上限が無ければ False）。"""
    return (
        budget is not None
        and budget.max_tool_calls_per_task > 0
        and budget.task_tool_calls >= budget.max_tool_calls_per_task
    )


def _coverage(run: Any, answer: str, task: _SupportTaskRun | None) -> dict[str, Any] | None:
    """回答に使った章節を終わりまで読んだか（#2492。`section_coverage`。判定しなければ None）。

    確認の質問だけの回答と、利用者が資料の検索を取り消した Run は判定しない。判定は補助なので、
    判定できなくても回答は止めない（None）。
    """
    if is_clarification_only(answer) or KIND_RAG in declined_kinds(run.metadata):
        return None
    try:
        return section_coverage(
            run.steps,
            answer,
            [run.goal, task.goal if task is not None else None],
            budget_exhausted=_tool_budget_exhausted(task.budget if task is not None else None),
        )
    except Exception as exc:  # noqa: BLE001 - 判定は補助。判定できなくても回答は返す
        logger.warning(
            "builtin_runtime_section_coverage_failed",
            extra={"run_id": run.id, "exception_type": type(exc).__name__},
        )
        return None


def _unread_sections(run: Any, answer: str, task: _SupportTaskRun | None) -> list[dict[str, Any]]:
    """回答に使ったのに終わりまで読めていない章節（#2492。無ければ空）。"""
    coverage = _coverage(run, answer, task)
    return list(coverage["sections"]) if coverage is not None else []


def _raise_on_grounding_failure(run_id: str, result: Any) -> None:
    """根拠・データのツールが 1 つも結果を返さず実行に失敗したまま確定した回答を、Run の失敗にする。

    #2475。
    検索・照会を終えられなかった回答を出すと、利用者は「資料に根拠が無い」と読む（最終の検証も根拠が
    無いので本文を外す）。ツールの失敗の理由を出して Run を失敗にし、再実行で直せることを示す。
    承認で止まった Run・確認の質問だけの回答・NL2SQL の SQL の生成前の確認で止まった Run は対象外。
    """
    from app.features.agent.runtime import runtime_repository

    if list(getattr(result, "interruptions", []) or []):
        return
    steps = list(runtime_repository.get_run(run_id).steps)
    if tool_grounding.is_exempt_answer(_answer_text(result.final_output), steps):
        return
    failed = tool_grounding.grounding_failure(steps)
    if failed is None:
        return
    tool_result = failed.tool_result
    logger.warning(
        "builtin_runtime_grounding_tool_failed",
        extra={
            "run_id": run_id,
            "tool_name": failed.tool_call.name if failed.tool_call is not None else None,
            "tool_error_code": tool_result.error_code if tool_result is not None else None,
            "tool_error_details": _safe_tool_error_details(tool_result),
        },
    )
    raise BuiltinRuntimeError(
        tool_grounding.GROUNDING_TOOL_FAILED_CODE, tool_grounding.grounding_failure_message(failed)
    )


def _safe_tool_error_details(result: ToolResult | None) -> dict[str, Any]:
    """ログに残すツールのエラーの分類（呼び先の error code・status・種類。本文は残さない）。"""
    if result is None:
        return {}
    details = result.error_details or {}
    raw = details.get("details")
    nested: dict[str, Any] = raw if isinstance(raw, dict) else {}
    picked: dict[str, Any] = {
        key: details.get(key) for key in ("error_code", "status") if details.get(key) is not None
    }
    picked.update(
        {
            key: nested.get(key)
            for key in ("kind", "error_type", "provider_status", "provider_code", "stage")
            if nested.get(key) is not None
        }
    )
    return picked


def _grounded_in_thread(run_id: str) -> bool:
    """会話の履歴の上限（`PLATFORM_CHAT_HISTORY_MAX_TURNS`。#1818）の中の前の Run のどれかが、
    根拠・データのツールで答えたか（#1835）。上限が 0（履歴を渡さない）なら前の Run を見ない。"""
    from app.features.agent.runtime import runtime_repository

    max_turns = ChatHistoryBudget.from_settings(get_settings()).max_turns
    if max_turns <= 0:
        return False
    previous = runtime_repository.thread_previous_runs(run_id)[-max_turns:]
    return tool_grounding.grounded_in_thread(list(item.steps) for item in previous)


def _refusal_declined(run_id: str) -> bool:
    """拒答にした Run が、ツールで調べずに範囲外・答えられないと判断した拒答か（#2608）。"""
    from app.features.agent.runtime import runtime_repository

    metadata = runtime_repository.get_run(run_id).metadata
    record = metadata.get(tool_grounding.METADATA_KEY) if isinstance(metadata, dict) else None
    return isinstance(record, dict) and record.get("declined") is True


def _refusal_text(agent: Any, sdk_agent: Agent[Any]) -> str:
    """拒答の文（業務 Agent の説明・スキルの表示名と、モデルに渡したツールから。#1835）。"""
    skill_names = [
        skill.name
        for skill_id in getattr(agent, "skill_ids", []) or []
        if (skill := resolve_skill(skill_id)) is not None and skill.enabled
    ]
    return tool_grounding.refusal_text(
        description=str(getattr(agent, "description", "") or ""),
        skill_names=skill_names,
        tool_names=[str(getattr(tool, "name", "")) for tool in sdk_agent.tools],
    )


def _interruption_call(item: Any) -> ToolCall:
    """SDK の中断（承認待ち）の項目を、記録する呼び出しにする。"""
    return ToolCall(
        name=str(getattr(item, "tool_name", "") or getattr(item, "name", "") or "tool"),
        arguments=_arguments(getattr(item, "arguments", None)),
        trace_id=str(getattr(item, "call_id", "") or ""),
    )


async def _finish(
    run_id: str,
    result: Any,
    task: _SupportTaskRun | None = None,
    *,
    rag_tools: bool = False,
    data_scopes: data_scope.AgentDataScopes | None = None,
    refusal: str | None = None,
) -> None:
    """Run を承認待ちにするか、回答を確定する。

    `refusal` があれば（根拠・データのツールを呼ばずに作った回答。#1835）、モデルの回答の代わりに
    その拒答の文を回答にし、最終の検証はせずに `skipped`（`no_tool_grounding`）を残す。回答の対応の
    basis は、調べずに範囲外・答えられないと判断した拒答なら `declined_without_tools`（#2608）。
    """
    from app.features.agent.runtime import runtime_repository

    # 承認待ちで止めるときも残す（再開した Run は同じ成果物を上書きする。#1243）。
    state_content = _save_support_task(run_id, task) if task is not None else None
    interruptions = list(getattr(result, "interruptions", []) or [])
    if interruptions:
        # 承認で見せる引数は、実行するときと同じくデータの範囲を当てたもの（#1378）。
        # Agent 自身の回答前の確認（#1839）は承認ではなく、利用者の答えを待つ確認にする。
        calls: list[ToolCall] = []
        confirmations: list[ConfirmationRequest] = []
        for item in interruptions:
            call = _interruption_call(item)
            if call.name == CONFIRMATION_TOOL:
                confirmations.append(_agent_confirmation(call, task, run_id))
            else:
                calls.append(_scoped_call(call, data_scopes))
        # 保存する状態には画像のデータを残さない（#2650。再開ではツールで取り直す）。
        state_text, removed_images = strip_image_data(result.to_state().to_string())
        if removed_images:
            logger.info(
                "builtin_runtime_state_images_removed",
                extra={"run_id": run_id, "images": removed_images},
            )
        runtime_repository.request_builtin_approvals(
            run_id, calls, state=state_text, confirmations=confirmations
        )
        return
    if refusal is not None:
        runtime_repository.save_builtin_artifact(
            run_id,
            kind=ANSWER_VALIDATION_KIND,
            name=ANSWER_VALIDATION_NAME,
            content=validation_content(
                STATUS_SKIPPED, reason=tool_grounding.REASON_NO_TOOL_GROUNDING
            ),
        )
        # 拒答も回答なので、回答の注意書きを付ける（#2405）。
        runtime_repository.complete_builtin_run(
            run_id,
            refusal,
            outcome=tool_grounding.refusal_outcome(declined=_refusal_declined(run_id)),
            notice=current_answer_notice(),
        )
        return
    answer = _answer_text(result.final_output)
    tool_confirmations = _tool_confirmations(task, state_content)
    if tool_confirmations and is_confirmation_payload(answer):
        # 確認のツールの引数の JSON を本文に書いた（#2236）。カードの問いの文にする。
        answer = card_answer(tool_confirmations)
    # 調べるのに使ったツール・プロファイル・内部の ID だけを述べる行は、資料の主張ではないので
    # 検証の前に外す（#2328。使ったツールと根拠は画面と MCP の sources が別に示す）。
    answer = without_lookup_notes(answer)
    if output_recorded(runtime_repository.get_run(run_id).artifacts):
        answer = without_output_record_sections(
            answer, get_settings().agent_output_record_section_titles
        )
    identifiers = identifier_index(runtime_repository.get_run(run_id).steps).answer(answer)
    answer = identifiers.text
    # モデルの最終の回答（最終の検証で段落を外す前。対応を決めるのに使う。#1305）。
    model_answer = answer
    validation: dict[str, Any] | None = None
    if aid_enabled(run_id, "final_validation"):
        try:
            answer, validation = await _validate_final_answer(
                run_id, answer, rag_tools=rag_tools, data_scopes=data_scopes
            )
            reverted = await _answer_before_hop_retry(
                run_id, model_answer, validation, rag_tools=rag_tools, data_scopes=data_scopes
            )
            if reverted is not None:
                model_answer, answer, validation = reverted
                identifiers = IdentifierCheck(
                    model_answer, **validation.get("identifier_check", {})
                )
        except Exception as exc:  # noqa: BLE001 - 検証の失敗で回答を落とさない
            logger.warning(
                "builtin_runtime_answer_validation_failed",
                extra={"run_id": run_id, "exception_type": type(exc).__name__},
            )
            # 確かめられなかったことを成果物にも残す（#1277。黙って通さない）。
            validation = validation_content(
                STATUS_UNVALIDATED,
                reason=REASON_VALIDATION_ERROR,
                message="資料との照合の途中で予期しない失敗が起きました。",
            )
            runtime_repository.save_builtin_artifact(
                run_id, kind=ANSWER_VALIDATION_KIND, name=ANSWER_VALIDATION_NAME, content=validation
            )
            answer = with_unverified_notice(answer)
    if identifiers.corrected or identifiers.ambiguous:
        validation = validation or validation_content(
            STATUS_SKIPPED, reason="identifier_check_only"
        )
        validation["identifier_check"] = identifiers.counts
        if identifiers.ambiguous:
            validation.update(
                status=STATUS_UNVALIDATED,
                valid=False,
                reason="identifier_ambiguous",
                message="資料に表記の異なる値があり、識別子を一意に照合できませんでした。",
            )
            answer = with_unverified_notice(answer)
        runtime_repository.save_builtin_artifact(
            run_id, kind=ANSWER_VALIDATION_KIND, name=ANSWER_VALIDATION_NAME, content=validation
        )
    # 主張の照合（最終の検証）とは別に、回答に使った章節を終わりまで読んだかを残し、読めていない
    # 章節は回答に示す（#2492。続きを読まずに確定した手順の不足を、照合は見つけられない）。
    coverage = _coverage(runtime_repository.get_run(run_id), model_answer, task)
    if coverage is not None:
        if validation is not None:
            validation = {**validation, "coverage": coverage}
            runtime_repository.save_builtin_artifact(
                run_id, kind=ANSWER_VALIDATION_KIND, name=ANSWER_VALIDATION_NAME, content=validation
            )
        if coverage["status"] == COVERAGE_INCOMPLETE:
            answer = with_unread_sections_notice(answer, coverage["sections"])
            logger.info(
                "builtin_runtime_unread_sections_noticed",
                extra={
                    "run_id": run_id,
                    "sections": [item["section_id"] for item in coverage["sections"]],
                    "reasons": [item["reason"] for item in coverage["sections"]],
                },
            )
    _save_answer_evidence(run_id, answer, validation)
    outcome = _answer_outcome(run_id, model_answer, validation, coverage)
    runtime_repository.complete_builtin_run(
        run_id,
        answer,
        outcome=outcome,
        confirmations=tool_confirmations,
        # 回答の注意書き（#2405）。最終の検証の後に、本文とは別の項目として付ける。回答前の確認
        # （カード）と確認の質問だけの回答には付けない。
        notice=(
            current_answer_notice()
            if answer_takes_notice(outcome, confirmations=bool(tool_confirmations))
            else None
        ),
    )


def _save_answer_evidence(run_id: str, answer: str, validation: Mapping[str, Any] | None) -> None:
    """回答の根拠（AI に渡した根拠と回答で引用した根拠。#2328）を成果物に残す。

    根拠を返すツールを呼ばなかった Run には残さない。
    """
    from app.features.agent.runtime import runtime_repository

    content = answer_evidence(runtime_repository.get_run(run_id).steps, answer, validation)
    if content["items"]:
        runtime_repository.save_builtin_artifact(
            run_id, kind=ANSWER_EVIDENCE_KIND, name=ANSWER_EVIDENCE_NAME, content=content
        )


def _withheld_rank(validation: Mapping[str, Any] | None) -> tuple[int, int, int] | None:
    """最終の検証の指摘の重さ（矛盾で外した段落の数, 未確認の段落の数, 警告の数。#1978・#2738）。

    警告の数は、警告の印の段落と回答全体の警告の和。判定が出なかった検証（`unvalidated`・
    `skipped`）は比べない（None）。
    """
    if not isinstance(validation, Mapping) or validation.get("status") != STATUS_COMPLETED:
        return None
    withheld = validation.get("withheld")
    withheld = withheld if isinstance(withheld, Mapping) else {}
    warnings = validation.get("answer_warnings")
    return (
        int(withheld.get("removed") or 0),
        int(withheld.get("unverified") or 0),
        int(withheld.get("warnings") or 0) + (len(warnings) if isinstance(warnings, list) else 0),
    )


async def _answer_before_hop_retry(
    run_id: str,
    answer: str,
    validation: dict[str, Any] | None,
    *,
    rag_tools: bool,
    data_scopes: data_scope.AgentDataScopes | None,
) -> tuple[str, str, dict[str, Any]] | None:
    """差し戻し（`hop_retry`。#1958）の後の回答が前の回答より悪ければ、前の回答を使う（#1978）。

    差し戻しを受けたモデルが、要らない区分を引き直して最初の回答の根拠から離れることがある。
    差し戻しの後の回答の最終の検証が指摘（矛盾で外した段落・未確認の段落・警告）を出したときだけ、
    経過に残した前の回答も同じ根拠で検証し、前の回答のほうが指摘が軽ければ（矛盾で外した段落 →
    未確認の段落 → 警告の数の順に少ない。#2738）、
    （モデルの回答, 利用者に見せる回答, 検証の内容）を前の回答のものにして返す。差し戻しの後の
    回答は経過（`tool_grounding` の `hop_reverted`）に残す。そうでなければ None（検証の成果物は
    差し戻しの後の回答のものに戻す）。
    """
    from app.features.agent.runtime import runtime_repository

    after = _withheld_rank(validation)
    if after is None or after == (0, 0, 0):
        return None
    original = tool_grounding.hop_original_answer(runtime_repository.get_run(run_id).events)
    original = without_lookup_notes(original) if original else original
    if not original or original.strip() == answer.strip() or is_clarification_only(original):
        return None
    identifiers = identifier_index(runtime_repository.get_run(run_id).steps).answer(original)
    original = identifiers.text
    published, before = await _validate_final_answer(
        run_id, original, rag_tools=rag_tools, data_scopes=data_scopes
    )
    rank = _withheld_rank(before)
    if rank is not None and rank < after:
        runtime_repository.record_tool_grounding(run_id, "hop_reverted", answer)
        logger.info(
            "builtin_runtime_hop_retry_reverted",
            extra={"run_id": run_id, "before": list(rank), "after": list(after)},
        )
        before["identifier_check"] = identifiers.counts
        return original, published, before
    if validation is not None:
        runtime_repository.save_builtin_artifact(
            run_id, kind=ANSWER_VALIDATION_KIND, name=ANSWER_VALIDATION_NAME, content=validation
        )
    return None


def _tools_consulted(run_id: str) -> bool:
    """この Run でモデルが MCP 接続のツールを呼んだか（回答前の確認の前の調べ。#1917）。"""
    from app.features.agent.runtime import runtime_repository

    return tool_grounding.tools_consulted(list(runtime_repository.get_run(run_id).steps))


def _last_tool_failed(run_id: str) -> bool:
    """この Run でモデルが最後に呼んだ MCP 接続のツールが失敗したか（#2234）。"""
    from app.features.agent.runtime import runtime_repository

    return tool_grounding.last_tool_failed(list(runtime_repository.get_run(run_id).steps))


def _nl2sql_unqueried(run_id: str) -> bool:
    """この Run でまだ nl2sql_query を呼んでいないか（表・列の確認の差し戻し。#2223）。"""
    from app.features.agent.runtime import runtime_repository

    return not any(
        step.tool_call is not None and mcp_base_tool_name(step.tool_call.name) == NL2SQL_QUERY
        for step in runtime_repository.get_run(run_id).steps
    )


def _tool_clarification_pending(run_id: str, known: Mapping[str, str] | None = None) -> bool:
    """この Run の最後の NL2SQL・RAG の結果が確認事項を返して止まっているか（#1934）。

    `known` は分かっている条件（業務ガイドが確かめる問いのうち、答えたものは数えない。#2726）。
    """
    from app.features.agent.runtime import runtime_repository

    return tool_clarification_pending(
        list(runtime_repository.get_run(run_id).steps), set(known or {})
    )


def _agent_confirmation(
    call: ToolCall, task: _SupportTaskRun | None, run_id: str = ""
) -> ConfirmationRequest:
    """`ask_user_confirmation` の中断を、利用者の答えを待つ確認にする（#1839）。

    資料・業務データのツールを呼ばずに出した確認（#1917。`ConfirmationBudget.free_text_calls`）は、
    モデルが作った選択肢を外して自由入力で聞く（選択肢をツールの結果から作れないため）。
    ツールを呼んだ後の確認も、選択肢はこの Run のツールの結果にあるものだけを残し、2 つ未満なら
    自由入力にする（#1934。`ground_options`）。
    """
    from app.features.agent.runtime import runtime_repository

    budget = task.confirmation if task is not None else None
    round_ = (budget.rounds.get(call.trace_id or "") or budget.used) if budget is not None else 1
    item = item_from_arguments(call.arguments)
    if budget is not None and (call.trace_id or "") in budget.free_text_calls:
        item = item.model_copy(update={"options": [], "selection": "single"})
    elif budget is not None and budget.tools_consulted is not None and run_id:
        steps = list(runtime_repository.get_run(run_id).steps)
        item = ground_options(item, tool_grounding.tool_output_texts(steps))
    return ConfirmationRequest(
        source=SOURCE_AGENT,
        call_id=call.trace_id or "",
        items=[item],
        round=max(round_, 1),
        max_rounds=budget.max_rounds if budget is not None else 1,
    )


def _pending_key(entry: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(entry.get("kind") or KIND_RAG),
        str(entry.get("condition_id") or ""),
        str(entry.get("requirement_session_id") or ""),
    )


def _tool_confirmations(
    task: _SupportTaskRun | None, content: Mapping[str, Any] | None
) -> list[ConfirmationRequest]:
    """この Run のツールの結果が返した確認事項を、画面の回答前の確認にする（#1839）。

    確認事項は呼び先（NL2SQL・RAG）が作ったまま受け渡す。この Run で新しく確かめ中になった問いだけ
    （前の Run から残っている問いは出し直さない）。無人の Run は出さない。RAG の確認は、利用者に
    確認できる Run だけ（方針が「前提を示して回答する」・上限に達した Run では前提を示して
    答えている）。
    NL2SQL の確認は NL2SQL の業務プロファイルの方針に従う（聞かれたものを出す）。スキップは
    次の Run で `skip_clarification` を付けて呼び直させる。
    利用者が「確認せずに回答」を選んだ Run（#1903）は、NL2SQL の確認も出さない（モデルが
    `skip_clarification` で呼び直さなかったときも、カードで聞き直さない）。
    """
    budget = task.confirmation if task is not None else None
    if (
        task is None
        or budget is None
        or budget.unattended
        or budget.skipped
        or not isinstance(content, Mapping)
    ):
        return []
    previous = task.previous.get("pending_clarifications") if task.previous else None
    seen = {_pending_key(entry) for entry in previous or [] if isinstance(entry, Mapping)}
    pending = [
        entry
        for entry in content.get("pending_clarifications") or []
        if isinstance(entry, Mapping) and _pending_key(entry) not in seen
    ]
    nl2sql = [entry for entry in pending if entry.get("kind") == KIND_NL2SQL]
    if nl2sql:
        items = items_from_pending(nl2sql[:1], SOURCE_NL2SQL)
        if items:
            return [
                ConfirmationRequest(
                    source=SOURCE_NL2SQL,
                    items=items,
                    round=budget.used + 1,
                    max_rounds=budget.max_rounds,
                    context=nl2sql_context(nl2sql[0]),
                )
            ]
    rag = [entry for entry in pending if entry.get("kind") != KIND_NL2SQL]
    if rag and budget.asks:
        items = items_from_pending(rag, SOURCE_RAG)
        if items:
            return [
                ConfirmationRequest(
                    source=SOURCE_RAG,
                    items=items,
                    round=budget.used + 1,
                    max_rounds=budget.max_rounds,
                )
            ]
    return []


def _answer_outcome(
    run_id: str,
    answer: str,
    validation: dict[str, Any] | None,
    coverage: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """回答の対応（#1305。`answer_outcome`）。決められなくても回答は保存する（対応は付けない）。"""
    from app.features.agent.runtime import runtime_repository

    try:
        steps = runtime_repository.get_run(run_id).steps
        return answer_outcome(answer, steps=steps, validation=validation, coverage=coverage)
    except Exception as exc:  # noqa: BLE001 - 対応は補助。決められなくても回答は返す
        logger.warning(
            "builtin_runtime_answer_outcome_failed",
            extra={"run_id": run_id, "exception_type": type(exc).__name__},
        )
        return None


def _validator_connection(evidence_tool: str) -> McpConnectionConfig | None:
    """根拠を返したツール（`<接続>__rag_search` など）の MCP 接続。"""
    base = mcp_base_tool_name(evidence_tool)
    for config in runtime_config_store.list_mcp_servers():
        if mcp_function_name(config.server_id, base) == evidence_tool:
            return config
    return None


def has_rag_evidence_tools(
    sdk_agent: Agent[Any] | None,
    skill_ids: list[str],
    cancellations: list[dict[str, Any]] | None = None,
) -> bool:
    """Agent が RAG の根拠のツール（`rag_search` / `rag_retrieve_evidence`）を持つか（#1277）。

    Run に渡したツールにあるか、Skill の requirement が名前で求めていれば持つとみなす（RAG の
    接続のツールを取れなかった Run でも、根拠を使えなかった回答として注記するため）。
    利用者が「資料の検索は不要」と取り消した Run（#1445）は持たないとみなす（資料を使わない）。
    """
    if KIND_RAG in declined_kinds({CANCEL_METADATA_KEY: cancellations or []}):
        return False
    tools = list(getattr(sdk_agent, "tools", []) or [])
    if any(mcp_base_tool_name(str(getattr(tool, "name", ""))) in EVIDENCE_TOOLS for tool in tools):
        return True
    return any(
        allowed is not None and bool(allowed & EVIDENCE_TOOLS)
        for allowed in agent_mcp_requirements(skill_ids).values()
    )


def _read_attachments(steps: list[Any]) -> bool:
    """この Run でモデルが添付を読むツールを呼んで結果を得たか（#2517）。"""
    from app.features.agent.chat_attachments import ATTACHMENT_READ_TOOLS

    return any(
        step.tool_call is not None
        and step.tool_call.name in ATTACHMENT_READ_TOOLS
        and step.tool_result is not None
        and step.tool_result.success
        for step in steps
    )


async def _validate_final_answer(
    run_id: str,
    answer: str,
    *,
    rag_tools: bool = False,
    data_scopes: data_scope.AgentDataScopes | None = None,
) -> tuple[str, dict[str, Any]]:
    """回答の最終の検証（#1246・#1277）。検証の結果を成果物に残し、利用者に見せる回答と、
    成果物の内容（回答の対応を決めるのに使う。#1305）を返す。

    モデルではなく Control Plane が、根拠を返した RAG の MCP 接続ごとに `rag_validate_answer` を
    呼ぶ（ポリシー・監査・Run の利用者のサービストークン・step はモデルのツールと同じ境界）。
    利用者が回答前の確認で答えた内容は前提（`premises`）として渡す（#1898）。
    判定が valid でなければ根拠と矛盾する段落を外し、確かめられない段落に印を付けて理由を足し
    （#2738）、検証が失敗したら
    照合を終えられなかったことを足す（回答は消さない。`answer_validation.combine_validations`）。
    `rag_tools` は Agent が RAG の根拠のツールを持つか（根拠の無い回答に注記するか）。

    今のターンに根拠が無いとき（#1879）: 資料に無い・答えられないことだけを述べる回答は注記せずに
    `skipped`（`absence_only`）。会話の続きで、今のターンが資料の根拠を得ずに答えた（前の回答の
    整形・言い換え。#1835 の `grounded_in_thread` で拒答にしない回答。検索を呼んで 0 件だった
    ときも同じ。#3314）なら、前のターンの検証で裏付けた根拠（`carried_evidence`）だけで検証する。
    """
    from app.features.agent.runtime import runtime_repository

    def save(content: dict[str, Any]) -> dict[str, Any]:
        runtime_repository.save_builtin_artifact(
            run_id, kind=ANSWER_VALIDATION_KIND, name=ANSWER_VALIDATION_NAME, content=content
        )
        return content

    run = runtime_repository.get_run(run_id)
    # 会話の続きでは、前のターン（モデルに渡す往復の数まで）の検証で回答の段落を裏付けた根拠も
    # 渡す（#1837。前のターンの事実を言い直した段落を、今のターンの根拠だけで外さない）。
    max_turns = ChatHistoryBudget.from_settings(get_settings()).max_turns
    previous = runtime_repository.thread_answer_validations(run_id) if max_turns > 0 else []
    carried = carried_evidence(previous[-max_turns:] if max_turns > 0 else [])
    # 回答が引用した根拠を先にして、上限で切っても検証に入れる（#1364）。
    groups = run_evidence_groups(run.steps, answer, carried=carried)
    if not groups:
        if KIND_RAG in declined_kinds(run.metadata):
            # 利用者が「資料の検索は不要」とした（#1445）。資料と照らし合わせない（注記しない）。
            return answer, save(validation_content(STATUS_SKIPPED, reason=REASON_RAG_DECLINED))
        if rag_tools and (is_clarification_only(answer) or nl2sql_clarification_pending(run.steps)):
            # 確認の質問だけの回答は資料の主張を含まない（「確かめていない」を足さない。#1306）。
            return answer, save(
                validation_content(STATUS_SKIPPED, reason=REASON_CLARIFICATION_ONLY)
            )
        if rag_tools and is_absence_only(answer):
            # 資料に無い・答えられないことだけを述べる回答（拒答）は資料の主張を含まない（#1879）。
            return answer, save(validation_content(STATUS_SKIPPED, reason=REASON_ABSENCE_ONLY))
        if carried and not _read_attachments(run.steps):
            # 今のターンで資料の根拠を得なかった回答（ツールを呼ばずに前の回答を整形・言い換えた。
            # #1879。検索・照会が 0 件だったときも同じ。#3314）は、前のターンで裏付けた根拠で
            # 確かめる（ツールが成功しただけでは根拠を得たことにしない）。添付を読んだ回答は下で
            # 資料と照らし合わせない。
            groups = carried_evidence_groups(carried, answer)
    if not groups and _read_attachments(run.steps):
        # 利用者が添付したファイルを読んで答えた（#2517）。資料と照らし合わせない（注記しない）。
        return answer, save(validation_content(STATUS_SKIPPED, reason=REASON_ATTACHMENT_EVIDENCE))
    if not groups:
        if rag_tools and answer.strip():
            content = validation_content(
                STATUS_UNVALIDATED, reason=REASON_NO_RAG_EVIDENCE, message=NO_EVIDENCE_NOTICE
            )
            return with_no_evidence_notice(answer), save(content)
        return answer, save(validation_content(STATUS_SKIPPED, reason=REASON_NO_RAG_EVIDENCE))
    refs = [ref for _tool, items in groups for ref in items]
    if not answer.strip():
        content = validation_content(STATUS_SKIPPED, reason=REASON_EMPTY_ANSWER, evidence=refs)
        return answer, save(content)
    validations = [
        await _validate_with_connection(run, answer, evidence_tool, items, data_scopes)
        for evidence_tool, items in groups
    ]
    # 図の見た目の段落は、この Run（と前のターンの検証）で図の画像がモデルへ届いたかで判定する
    # （#2678。画像の有無は Run の記録から決める）。
    figures = validation_figure_readings(run.steps, previous[-max_turns:] if max_turns > 0 else [])
    content, published = combine_validations(answer, validations, figures)
    return published, save(content)


async def _validate_with_connection(
    run: Any,
    answer: str,
    evidence_tool: str,
    refs: list[dict[str, Any]],
    data_scopes: data_scope.AgentDataScopes | None = None,
) -> dict[str, Any]:
    """1 つの MCP 接続の根拠で `rag_validate_answer` を呼び、接続ごとの内容を返す。

    判定が出たら `completed`、呼べない・失敗した・判定として使えない応答なら `unvalidated`。
    """

    def unvalidated(reason: str, message: str | None, **extra: Any) -> dict[str, Any]:
        return validation_content(
            STATUS_UNVALIDATED, reason=reason, message=message, evidence=refs, **extra
        )

    config = _validator_connection(evidence_tool)
    if config is None:
        return unvalidated(REASON_CONNECTION_NOT_FOUND, "根拠を返した MCP 接続が見つかりません。")
    connection: dict[str, Any] = {"connection": config.server_id}
    if len(answer) > MAX_ANSWER_CHARS:
        return unvalidated(
            REASON_ANSWER_TOO_LONG,
            f"回答が長すぎるため検証できません（{MAX_ANSWER_CHARS} 文字まで）。",
            **connection,
        )
    context = _run_tool_context(run)
    try:
        listed = await asyncio.to_thread(
            list_mcp_connection_tools, config.server_id, context=context
        )
    except KeyError:
        return unvalidated(
            REASON_CONNECTION_NOT_FOUND, "根拠を返した MCP 接続が見つかりません。", **connection
        )
    except ExternalToolError as exc:
        return unvalidated(exc.code, exc.message, **connection)
    tool = next((item for item in listed.tools if item.name == VALIDATE_ANSWER_TOOL), None)
    if tool is None:
        return unvalidated(
            REASON_VALIDATOR_UNAVAILABLE,
            f"MCP 接続「{config.label or config.server_id}」は資料との照合を提供していません。",
            **connection,
        )
    definition = mcp_tool_definition(config, tool)
    recorder = _ToolRecorder(run.id)
    # 根拠の読み直しも Run と同じデータの範囲で行う（サービストークンの claim。#1379）。
    recorder.data_scopes = dict(data_scopes or {})
    # 利用者が回答前の確認で答えた前提（#1898）。答えをそのまま述べた段落と、資料の規則を答えに
    # 当てはめた結論を、資料の根拠だけで「確かめられない」として外さない。
    premises = validation_premises(run.metadata, run.confirmations)
    # この Run の業務データ（NL2SQL）の照会の結果（#1950）。件数・金額・一覧の段落を、資料の根拠
    # だけで「確かめられない」として外さない。
    data_results = validation_data_results(run.steps)
    step_id, result = await recorder.call(
        ToolCall(
            name=definition.name,
            arguments={
                "query": run.goal[:MAX_QUERY_CHARS],
                "answer": answer,
                "evidence": refs,
                # 要求の充足・業務ガイドの手順と影響範囲の決定的な検査（#1276）。
                **connection_check_inputs(run.steps, evidence_tool),
                **({"premises": premises} if premises else {}),
                **({"data_results": data_results} if data_results else {}),
            },
            trace_id=f"answer_validation_{run.id}_{config.server_id}",
        ),
        definition=definition,
        handler=mcp_tool_handler(config, tool),
    )
    common: dict[str, Any] = {**connection, "tool_name": definition.name, "step_id": step_id}
    if not result.success or not isinstance(result.output, dict):
        reason = result.error_code or (
            "policy_denied" if result.policy_decision == ToolPolicyDecision.DENY else "tool_failed"
        )
        return unvalidated(reason, result.error, **common)
    if result.output.get("status") == RESULT_TIMED_OUT:
        # RAG が時間の上限までにどの段落も確かめ終わらなかった（#2700。回答は消さずに注記する）。
        return unvalidated(
            REASON_VALIDATION_TIMED_OUT,
            "資料との照合が時間の上限までに終わりませんでした。",
            result=result.output,
            **common,
        )
    if not usable_result(result.output):
        return unvalidated(
            REASON_UNUSABLE_RESULT,
            f"検証の結果（{result.output.get('status')}）を判定に使えません。",
            result=result.output,
            **common,
        )
    return validation_content(STATUS_COMPLETED, result=result.output, evidence=refs, **common)


def _record_usage(run_id: str, source: object, model_id: str) -> None:
    """SDK の累計の利用量を Run に記録する（#772）。

    `source` は `Runner.run` の結果か、失敗の `run_data`（SDK の例外が持つときだけ）。SDK の
    `Usage` は承認待ちで保存する RunState に入り、再開後も累計で続くため、毎回上書きでよい。
    """
    from app.features.agent.runtime import RunUsage, runtime_repository

    # Run 全体の実行予算の消費も、同じ区切り（完了・失敗・承認待ち）で残す（#68）。
    _record_run_budget(run_id)
    usage = getattr(getattr(source, "context_wrapper", None), "usage", None)
    if usage is None:
        return
    runtime_repository.record_builtin_usage(
        run_id,
        RunUsage(
            model=_model_label(model_id),
            requests=int(getattr(usage, "requests", 0) or 0),
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
        ),
    )


def _model_label(model_id: str) -> str:
    """利用量に残すモデル名（Agent の指定が空なら、その時点の既定のテキストモデル）。"""
    explicit = (model_id or "").strip()
    if explicit:
        return explicit
    try:
        return enterprise_ai_default_model_id(get_settings()) or ""
    except Exception:  # noqa: BLE001 - モデル名は記録の補助。取れなくても利用量は残す
        return ""


def _arguments(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value or "{}")
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _record_failure(run_id: str, exc: Exception) -> None:
    from app.features.agent.runtime import runtime_repository

    if isinstance(exc, RunCancelledError):
        # 取消が確定した Run を止めた（失敗ではない。Run は取消済みのまま。#2609）。
        logger.info("builtin_runtime_stopped_after_cancel", extra={"run_id": run_id})
        return
    if isinstance(exc, BuiltinRuntimeError):
        code, message = exc.code, exc.message
    elif isinstance(exc, MaxTurnsExceeded):
        code = "runtime.max_turns_exceeded"
        message = "ツールの呼び出しが上限の回数に達したため止めました。"
    else:
        code = "runtime.model_failed"
        # モデルの API の error code（`invalid_value` など）まで残す（型名だけでは原因が
        # 分からない。#1215）。
        model_code = model_error_code(exc)
        reason = f"{type(exc).__name__}: {model_code}" if model_code else type(exc).__name__
        message = f"モデルの呼び出しに失敗しました（{reason}）。"
    logger.warning(
        "builtin_runtime_failed",
        extra={
            "run_id": run_id,
            "error_code": code,
            "exception_type": type(exc).__name__,
            "model_error_code": model_error_code(exc),
            # モデルの API のメッセージ（モデル名と理由。業務データ・資格情報は含まない）。
            "model_error_message": _model_error_message(exc),
        },
    )
    runtime_repository.fail_builtin_run(run_id, code=code, detail=message)


def _model_error_message(exc: BaseException) -> str | None:
    if not isinstance(exc, APIStatusError):
        return None
    body = exc.body
    if isinstance(body, dict):
        nested = body.get("error") if isinstance(body.get("error"), dict) else body
        message = nested.get("message") if isinstance(nested, dict) else None
        if isinstance(message, str):
            return message[:500]
    return None


def _max_turns() -> int:
    return max(2, int(get_settings().agent_max_tool_calls_per_run) + 1)


def runtime_status() -> dict[str, Any]:
    """Runtime 画面の状態（API key は出さない）。"""
    status: dict[str, Any] = {
        "id": BUILTIN_RUNTIME_ID,
        "name": "組み込み Runtime",
        "sdk": SDK_PACKAGE,
        "sdk_version": sdk_version(),
        "model_provider": "OpenAI 互換の API（Responses API）",
        "model_id": "",
        "ready": False,
        "error_code": None,
        "message": None,
        # Agent ごとに選べるモデル（システム設定 > モデル の登録モデル。ID と表示名だけ）。
        "models": [
            {"model_id": model.model_id, "display_name": model.display_name or model.model_id}
            for model in enterprise_ai_model_catalog(get_settings())
            if model.model_id
        ],
    }
    try:
        target = resolve_model_target("")
    except BuiltinRuntimeError as exc:
        status.update(error_code=exc.code, message=exc.message)
        return status
    status.update(model_id=target.model_id, ready=True)
    return status
