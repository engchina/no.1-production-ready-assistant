"""チャットの処理の経過（3 製品共通のイベント。#1359）を、Run の trace（span の木）から作る。

処理の経過は trace の投影（#3226）で、span は共通の span の表（`PLATFORM_TRACE_SPANS`。
案 A）にある: 組み込み Runtime の実行区間（`agent.execute` / `agent.resume`）の
`operation_trace(listener=...)` が知らせる span の開始・表示用のデータの変更・終了を、
process の書き手（`trace_spans`）が表に書く。Run には表に入れないものだけを残す
（`note_run_span`。Run の lock の中）: 実行の全体の span の trace id（`trace_id`）、工程の詳細
（`span_details`。span id → 詳細。ツールの引数と結果など）、記録した待ちの回
（`trace_wait_rounds`）。Run を確定するときに、書けなかった span の数
（`trace_dropped_spans`）を残す。画面の API は Run の trace id で表を読み、Run の状態（完了・
失敗・停止。正本）と工程の詳細を合わせてイベントにする（`run_trace_events`）。

工程を測る span の表示用のデータ（`SpanDisplay`。画面の名前は frontend の段の定義が `kind` から
付ける）:

- `gen_ai.chat`（モデルの呼び出し）: 応答がツールを求めたら kind `plan`（進め方の検討）、最終の
  回答を作ったら kind `respond`（回答の作成）。呼び出しの後に決める（`model_turn_kind`）。
- `gen_ai.execute_tool`（ツールの呼び出し）: kind `tool`、params `{"tool": 名前, "run_step":
  Run の step の id}`（表示名〔MCP の `title`。Run の metadata `tool_titles`〕があれば `title`
  も）、details はツールの内部名・エラー・引数と結果（`tool_details`。Run の `span_details`）。
  実行しなかったツール（却下・方針で拒否・上限・引数の誤り）も `skipped` の span で残し、承認の
  却下は params に `rejected: true`。
- `agent.approval_wait`（kind `approval`、params `{"tools": 承認を待ったツール名, "round": 回}`）
  と `agent.confirmation_wait`（kind `confirmation`、params `{"round": 回}`）: 承認・Agent
  自身の回答前の確認（`ask_user_confirmation`。#1839）の待ちの区間。待ちが終わって再開した区間で、
  終わった区間として記録する（`record_span`）。待っている間は、見出しの状態と承認・確認の
  カードで示す。
- 回答前の確認のツールの呼び出しそのものは span にしない（利用者の答えを返すだけ。待ちの span が
  表す）。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any, Literal

from pr_backend_core.chat_progress import (
    ChatProgressParamValue,
    ChatProgressStepDetails,
    chat_progress_details,
)
from pr_backend_core.observability.trace_store import StoredTraceSpan, trace_progress_events
from pr_backend_core.observability.tracing import SpanDisplay, SpanUpdate

if TYPE_CHECKING:
    from app.features.agent.confirmation import ConfirmationRequest
    from app.features.agent.runtime import ApprovalRequest, RunState, RunStep
    from app.features.agent.tools import ToolCall, ToolResult

# 工程の種類（画面の段の定義の key と同じ）。
KIND_PLAN = "plan"
KIND_TOOL = "tool"
KIND_APPROVAL = "approval"
KIND_RESPOND = "respond"
KIND_CONFIRMATION = "confirmation"
# 承認・確認の待ちの span の名前。
APPROVAL_WAIT_SPAN = "agent.approval_wait"
CONFIRMATION_WAIT_SPAN = "agent.confirmation_wait"
# ツールの呼び出しの span の名前（OpenTelemetry GenAI semantic conventions）。
EXECUTE_TOOL_SPAN = "gen_ai.execute_tool"
# 回答前の確認のツール（span にしない）。
CONFIRMATION_TOOL = "ask_user_confirmation"
# 承認を待つツール名をつなぐ記号（画面の補足にそのまま出す）。
TOOL_NAME_SEPARATOR = "、"
# Run に渡したツールの表示名（function tool の名前 → MCP の `title`）を残す metadata の key。
TOOL_TITLES_METADATA_KEY = "tool_titles"
# ツールの span の params の、Run の step の id の key（画面がツールの step の記録と結ぶ）。
RUN_STEP_PARAM = "run_step"

# 記録する Run の Runtime（組み込み Runtime だけ。旧エンジンの Run は実行しない）。
BUILTIN_RUNTIME_ID = "builtin"
# Run の状態 → 処理の経過の終端（正本は Run の状態。表で実行中のままの span はこれで閉じる）。
RUN_TERMINAL_STATUSES: dict[str, Literal["done", "failed", "cancelled"]] = {
    "completed": "done",
    "failed": "failed",
    "cancelled": "cancelled",
}
# 記録した待ちの回を残す工程の種類（再開し直しても同じ待ちを重ねて記録しない）。
_WAIT_KINDS = (KIND_APPROVAL, KIND_CONFIRMATION)


def note_run_span(run: RunState, update: SpanUpdate) -> bool:
    """span の知らせから、表に入れないものを Run に残す（変えたら True）。Run の lock の中で呼ぶ。

    - 実行の全体の span（root）の trace id（最初の区間の trace。再開の区間も同じ trace）。
    - span の工程の詳細（`SpanDisplay.details`。表には入れない）。実行中のツールの引数も開ける
      ように、開始の知らせの詳細も残す（終わりの知らせで結果を足した詳細に置き換わる）。
    - 終わった待ちの span の回（`trace_wait_rounds`）。
    """
    changed = False
    if update.root and run.trace_id is None:
        run.trace_id = update.trace_id
        changed = True
    display = update.display
    if display is None:
        return changed
    if display.details is not None and run.span_details.get(update.span_id) != display.details:
        run.span_details[update.span_id] = display.details
        changed = True
    if update.status == "running":
        return changed
    round_ = (display.params or {}).get("round")
    if display.kind in _WAIT_KINDS and isinstance(round_, int) and not isinstance(round_, bool):
        rounds = run.trace_wait_rounds.setdefault(display.kind, [])
        if round_ not in rounds:
            rounds.append(round_)
            changed = True
    return changed


def run_progress_terminal(run: RunState) -> Literal["done", "failed", "cancelled"] | None:
    """Run の状態の終端（終わっていなければ None。承認・確認の待ちは終端ではない）。"""
    return RUN_TERMINAL_STATUSES.get(str(run.status))


def run_trace_events(run: RunState, spans: Sequence[StoredTraceSpan]) -> list[dict[str, Any]]:
    """表の span と Run の状態・工程の詳細から、処理の経過のイベントの一覧（snapshot）を作る。

    終わった Run は終端のイベントで閉じる（表で実行中のままの span も終端に合わせて閉じる）。
    trace の無い Run（受け付けただけ・開始の前に終わった）は空。
    """
    if run.trace_id is None:
        return []
    details = {
        span_id: value.model_dump(mode="json", exclude_none=True)
        for span_id, value in run.span_details.items()
    }
    return trace_progress_events(
        spans, target_id=run.id, terminal=run_progress_terminal(run), details=details
    )


def model_turn_kind(output: Iterable[Any] | None) -> str | None:
    """モデルの応答の工程の種類（ツールを求めたら進め方の検討、最終の回答なら回答の作成）。

    `output` は Responses API の出力の要素（object か dict）。どちらでもなければ None。
    """
    kinds = {_item_type(item) for item in output or ()}
    if "function_call" in kinds:
        return KIND_PLAN
    if "message" in kinds:
        return KIND_RESPOND
    return None


def _item_type(item: Any) -> str:
    value = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
    return value if isinstance(value, str) else ""


def tool_params(
    name: str,
    titles: dict[str, str],
    *,
    run_step: str | None = None,
    rejected: bool = False,
) -> dict[str, ChatProgressParamValue]:
    """ツールの span の名前の値（内部名・表示名・Run の step・却下）。"""
    params: dict[str, ChatProgressParamValue] = {"tool": name}
    title = titles.get(name)
    if title:
        params["title"] = title
    if run_step:
        params[RUN_STEP_PARAM] = run_step
    if rejected:
        params["rejected"] = True
    return params


def tool_display(
    call: ToolCall,
    titles: dict[str, str],
    *,
    result: ToolResult | None = None,
    run_step: str | None = None,
    rejected: bool = False,
) -> SpanDisplay:
    """ツールの span の表示用のデータ（kind `tool`・名前の値・開いて見る詳細）。"""
    return SpanDisplay(
        kind=KIND_TOOL,
        params=tool_params(call.name, titles, run_step=run_step, rejected=rejected),
        details=tool_details(call, result),
    )


def tool_attributes(name: str, call_id: str | None) -> dict[str, object]:
    """ツールの span の属性（OpenTelemetry GenAI の execute_tool）。"""
    return {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": name,
        "gen_ai.tool.call.id": call_id or None,
    }


# ツールの段の詳細の見出し（画面の `chat.progress.details.*` と同じ文言）。
_DETAIL_TOOL = "ツール"
_DETAIL_ERROR = "エラー"
_DETAIL_ARGUMENTS = "引数"
_DETAIL_RESULT = "結果"


def tool_details(
    call: ToolCall | None, result: ToolResult | None = None
) -> ChatProgressStepDetails | None:
    """ツールの段の詳細（内部名・エラー・引数と結果の JSON）。

    上限で切り、秘密らしい値を伏せる（共通の規則）。データの範囲の記録は画面が Run の step から
    足す。
    """
    fields: list[tuple[str, object] | tuple[str, object, bool]] = []
    if call is not None and call.name:
        fields.append((_DETAIL_TOOL, call.name, True))
    if result is not None and result.error:
        fields.append((_DETAIL_ERROR, result.error))
    code: list[tuple[str | None, str, Literal["json", "sql", "text"] | None]] = []
    if call is not None and call.arguments:
        code.append((_DETAIL_ARGUMENTS, _json_text(call.arguments), "json"))
    if result is not None and result.output is not None:
        code.append((_DETAIL_RESULT, _json_text(result.output), "json"))
    return chat_progress_details(fields=fields, code=code)


def tool_step_details(step: RunStep) -> ChatProgressStepDetails | None:
    """Run の step のツールの段の詳細（`tool_details` と同じ）。"""
    return tool_details(step.tool_call, step.tool_result)


def _json_text(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return str(value)


def approval_wait_display(
    round_: Sequence[ApprovalRequest], index: int, titles: dict[str, str]
) -> SpanDisplay:
    """承認の待ちの span の表示用のデータ（承認を待ったツールの名前と、何回目の承認か）。"""
    return SpanDisplay(
        kind=KIND_APPROVAL, params={"tools": _tool_names(round_, titles), "round": index + 1}
    )


def confirmation_wait_display(confirmation: ConfirmationRequest) -> SpanDisplay:
    """Agent 自身の回答前の確認の待ちの span の表示用のデータ（何回目の確認か）。"""
    return SpanDisplay(kind=KIND_CONFIRMATION, params={"round": max(1, confirmation.round)})


def recorded_wait_rounds(run: RunState, kind: str) -> set[int]:
    """記録済みの待ちの span の回（同じ待ちを再開のたびに重ねて記録しない）。"""
    return set(run.trace_wait_rounds.get(kind, []))


def _tool_names(approvals: Sequence[ApprovalRequest], titles: dict[str, str]) -> str:
    """承認を待つツールの名前（表示名があれば表示名）。"""
    names: list[str] = []
    for approval in approvals:
        name = titles.get(approval.tool_call.name) or approval.tool_call.name
        if name not in names:
            names.append(name)
    return TOOL_NAME_SEPARATOR.join(names)


def tool_titles(run: RunState) -> dict[str, str]:
    """Run に渡したツールの表示名（function tool の名前 → MCP の `title`）。無ければ空。"""
    value = run.metadata.get(TOOL_TITLES_METADATA_KEY)
    if not isinstance(value, dict):
        return {}
    return {str(name): title for name, title in value.items() if isinstance(title, str) and title}


def approval_rounds(approvals: list[ApprovalRequest]) -> list[list[ApprovalRequest]]:
    """承認を求めた回（同じ中断で求めた承認）に分ける。

    前の回の承認がすべて決まった後に求めた承認は、次の回にする（1 回の中断で求めた承認は、
    すべて決まるまで次の中断が起きない）。
    """
    rounds: list[list[ApprovalRequest]] = []
    for approval in sorted(approvals, key=lambda item: item.created_at):
        current = rounds[-1] if rounds else None
        closed = current is not None and all(
            str(item.status) != "pending"
            and item.decided_at is not None
            and item.decided_at <= approval.created_at
            for item in current
        )
        if current is None or closed:
            rounds.append([approval])
        else:
            current.append(approval)
    return rounds


__all__ = [
    "APPROVAL_WAIT_SPAN",
    "BUILTIN_RUNTIME_ID",
    "CONFIRMATION_WAIT_SPAN",
    "EXECUTE_TOOL_SPAN",
    "KIND_APPROVAL",
    "KIND_CONFIRMATION",
    "KIND_PLAN",
    "KIND_RESPOND",
    "KIND_TOOL",
    "RUN_STEP_PARAM",
    "TOOL_TITLES_METADATA_KEY",
    "approval_rounds",
    "approval_wait_display",
    "confirmation_wait_display",
    "RUN_TERMINAL_STATUSES",
    "model_turn_kind",
    "note_run_span",
    "recorded_wait_rounds",
    "run_progress_terminal",
    "run_trace_events",
    "tool_attributes",
    "tool_details",
    "tool_display",
    "tool_params",
    "tool_step_details",
    "tool_titles",
]
