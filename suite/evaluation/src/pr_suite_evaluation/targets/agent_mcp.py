"""Target: 業務 Agent を公開の Agent MCP で呼ぶ adapter（#1289・#3240）。

rag の比較評価の CLI（段 2 まで）から移した。

- 呼ぶのは公開の MCP（`POST /api/mcp`。契約は `platform/contracts/mcp/agent-tools.json`）の
  `agent_chat`・`agent_get_run`・`agent_answer_confirmation`・`agent_get_run_observation`・
  `agent_cancel_run` だけで、利用者が発行した API キー（使える製品に `agent`）で呼ぶ。Agent の画面の
  API・Run の内部の記録・Agent のコードには依存しない。業務 Agent は公開済みのものを ID で指定する
  （作成・公開の MCP のツールは持たない。#3240 の Q4）。
- 複数往復のケース（`turns`）は、返答を同じ会話（`thread_id`）の次の `agent_chat` として送る。Run が
  回答の前の確認（`waiting_confirmation`）で止まったら、次の返答を
  `agent_answer_confirmation` で確認への答えとして送り、同じ Run の続きを次の往復として読む。
  確認は「利用者に条件を確かめた」対応（needs_clarification）として採点する。次の返答が無ければ
  確認で終わったケースとして Run を取り消す。
- ケースの既知の条件（`conditions`）は、Agent に構造化して渡す口が無いため、最初の質問の後ろに
  「既知の条件」の文として足す（業務ガイドを渡すと条件の名前で書く）。
- 時間内に終わらない Run は `agent_cancel_run` で取り消し、結果（`result`）を観測に残す。承認待ちで
  止まった Run は画面で判断されうるので取り消さない。

観測（`CaseObservation`）には公開の出力をそのまま保存し、採点の入力（`CaseView`）への変換
（`agent_case_view`）は採点のときに行う（規則を変えても、保存した観測から採点し直せる）。
"""

from __future__ import annotations

import contextlib
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from pr_evaluation_scoring import OUTCOMES, normalize_for_match

from pr_suite_evaluation.dataset import Case
from pr_suite_evaluation.mcp_client import McpClient, TargetError
from pr_suite_evaluation.observation import CaseObservation, TurnObservation
from pr_suite_evaluation.targets.agent_legacy import answer_body, infer_outcome
from pr_suite_evaluation.views import CaseFailure, CaseView, Citation, TurnView

TARGET_KIND = "agent_mcp"
# Agent の経路の規則の版（agent_v1 は段 1 の画面の API、agent_v2 は段 2 の公開の MCP。agent_v3 は
# suite へ移し、回答の前の確認に答え、outcome の無い Run・本文を省かれた根拠・見られない根拠を
# 評価不能にした版。#3240）。
RULES_VERSION = "agent_v3"
LEGACY_OUTCOME_RULES = "legacy_outcome"
DEFAULT_RUN_TIMEOUT_SECONDS = 900.0
REQUEST_TIMEOUT_SECONDS = 60.0
# `agent_chat` / `agent_get_run` / `agent_answer_confirmation` の `wait_seconds` の上限
# （契約の maximum）。
MAX_WAIT_SECONDS = 300
# 観測の根拠を 1 回に読む件数（契約の maximum）と、たどるページの上限。
OBSERVATION_PAGE_LIMIT = 100
MAX_OBSERVATION_PAGES = 1000
# 読める出力の版（契約 agent-tools.json の schema_version。知らない版は読まない）。
RUN_OUTPUT_SCHEMA_VERSION = 4
# 5: ツールの呼び出しに RAG の縮退（degraded_steps・guardrail_degraded・zero_evidence_stage）を、
# 根拠に table_binding を足した（#3546）。
# 6: RAG の契約から外した guardrail_degraded をツールの呼び出しから削除した（#37）。
OBSERVATION_SCHEMA_VERSION = 6
CANCEL_SCHEMA_VERSION = 1
# 確認への答えの上限（契約の answers の maxItems）。
MAX_CONFIRMATION_ANSWERS = 5
MCP_TOOL_SEPARATOR = "__"
EVIDENCE_TOOLS = frozenset({"rag_search", "rag_retrieve_evidence"})
APPROVAL_SKIPPED = "approval_skipped"
OUTCOME_SOURCE_ANSWER = "agent_answer"
OUTCOME_SOURCE_CONFIRMATION = "agent_confirmation"
OUTCOME_SOURCE_INFERRED = "inferred"
_STATUS_ERRORS = {
    "timeout": ("AgentRunTimeout", "Agent の Run が {timeout:g} 秒以内に終わりませんでした。"),
    "waiting_approval": ("AgentRunWaitingApproval", "Agent の Run が承認待ちで止まりました。"),
    "failed": ("AgentRunFailed", "Agent の Run が failed で終わりました。"),
    "cancelled": ("AgentRunCancelled", "Agent の Run が cancelled で終わりました。"),
    APPROVAL_SKIPPED: (
        "AgentRunApprovalSkipped",
        "Agent の Run が承認の要るツール（{tools}）を呼ばずに終わりました。",
    ),
}
_RUN_HINT = " run_id で Agent の実行履歴を確認してください。"


class AgentEvaluationError(TargetError):
    """Agent の公開の MCP を呼べない・出力を読めない。"""

    error_type = "AgentEvaluationError"


def _records(value: object) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def base_tool_name(name: str) -> str:
    """MCP 接続のツールの名前（`<接続>__<ツール>`）のツールの部分。"""
    return name.rsplit(MCP_TOOL_SEPARATOR, 1)[-1]


# ---- 公開の MCP ----------------------------------------------------------------------------


class AgentMcp:
    """Agent の公開の MCP のツール。

    待ちは MCP の `wait_seconds` に任せる（サーバーが待って返す）。
    """

    def __init__(self, mcp: McpClient, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._mcp = mcp
        self._clock = clock

    def _wait_seconds(self, deadline: float) -> int:
        return max(1, min(MAX_WAIT_SECONDS, math.ceil(deadline - self._clock())))

    def _run_output(self, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        return self._mcp.call(tool, arguments, schema_version=RUN_OUTPUT_SCHEMA_VERSION)

    def chat(
        self, *, agent_id: str, question: str, thread_id: str | None, deadline: float
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "agent_id": agent_id,
            "question": question,
            "wait_seconds": self._wait_seconds(deadline),
        }
        if thread_id:
            arguments["thread_id"] = thread_id
        return self._run_output("agent_chat", arguments)

    def answer_confirmation(
        self,
        *,
        run_id: str,
        confirmation_id: str,
        answers: Sequence[Mapping[str, Any]],
        deadline: float,
    ) -> dict[str, Any]:
        return self._run_output(
            "agent_answer_confirmation",
            {
                "run_id": run_id,
                "confirmation_id": confirmation_id,
                "action": "answer",
                "answers": [dict(answer) for answer in answers],
                "wait_seconds": self._wait_seconds(deadline),
            },
        )

    def get_run(self, run_id: str, *, deadline: float) -> dict[str, Any]:
        return self._run_output(
            "agent_get_run", {"run_id": run_id, "wait_seconds": self._wait_seconds(deadline)}
        )

    def wait_run(self, run: Mapping[str, Any], *, deadline: float) -> Mapping[str, Any] | None:
        """Run が終わる（`in_progress=false`）まで読む。期限までに終わらなければ None。"""
        while run.get("in_progress") is True:
            if self._clock() >= deadline:
                return None
            run = self.get_run(str(run["run_id"]), deadline=deadline)
        return run

    def observation(self, run_id: str) -> dict[str, Any]:
        """Run の観測。根拠は `next_cursor` をたどって全件を 1 つにまとめる。"""
        cursor: str | None = None
        merged: dict[str, Any] | None = None
        for _page in range(MAX_OBSERVATION_PAGES):
            arguments: dict[str, Any] = {"run_id": run_id, "limit": OBSERVATION_PAGE_LIMIT}
            if cursor is not None:
                arguments["cursor"] = cursor
            page = self._mcp.call(
                "agent_get_run_observation", arguments, schema_version=OBSERVATION_SCHEMA_VERSION
            )
            evidence = _records(page.get("evidence"))
            if merged is None:
                merged = {**page, "evidence": [dict(item) for item in evidence]}
            else:
                merged["evidence"].extend(dict(item) for item in evidence)
            next_cursor = page.get("next_cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                merged["next_cursor"] = None
                return merged
            cursor = next_cursor
        raise AgentEvaluationError(
            f"Agent の観測の根拠が {MAX_OBSERVATION_PAGES} ページで終わりませんでした。"
        )

    def cancel_run(self, run_id: str) -> str | None:
        """Run を取り消す（冪等）。結果（`result`）を返し、取り消せなかった失敗は None。"""
        with contextlib.suppress(TargetError, httpx.HTTPError):
            output = self._mcp.call(
                "agent_cancel_run", {"run_id": run_id}, schema_version=CANCEL_SCHEMA_VERSION
            )
            result = output.get("result")
            return result if isinstance(result, str) else None
        return None


# ---- 送る発話 ---------------------------------------------------------------------------------


def known_conditions_text(conditions: Mapping[str, str], labels: Mapping[str, str]) -> str:
    """ケースの既知の条件を、質問に足す文にする（Agent には構造化して渡す口が無い）。"""
    parts = [f"{labels.get(key, key)}は「{value}」" for key, value in conditions.items()]
    return f"（既知の条件: {'、'.join(parts)}）" if parts else ""


def case_utterances(case: Case, labels: Mapping[str, str]) -> list[tuple[str, dict[str, str]]]:
    """送る発話と、その発話までに分かった条件（確認への答えの選択肢を選ぶのに使う）。"""
    first = case.query
    if case.conditions:
        first = f"{first}\n{known_conditions_text(case.conditions, labels)}"
    conditions = dict(case.conditions)
    utterances = [(first, dict(conditions))]
    for turn in case.turns:
        conditions = {**conditions, **turn.conditions}
        utterances.append((turn.reply, dict(conditions)))
    return utterances


def confirmation_answers(
    confirmation: Mapping[str, Any], reply: str, conditions: Mapping[str, str]
) -> list[dict[str, Any]]:
    """回答の前の確認（`confirmation.items`）への答え。

    分かった条件の値と名前が同じ選択肢が 1 つだけあればそれを選び、無ければ返答の文を自由入力の
    答え（`other_text`）にする（評価セットの返答は、利用者が確認に答える文）。
    """
    values = {normalize_for_match(value) for value in conditions.values() if value.strip()}
    answers: list[dict[str, Any]] = []
    for item in _records(confirmation.get("items"))[:MAX_CONFIRMATION_ANSWERS]:
        matched = [
            str(option["id"])
            for option in _records(item.get("options"))
            if option.get("id") and normalize_for_match(str(option.get("label") or "")) in values
        ]
        if len(matched) == 1:
            answers.append({"item_id": str(item["id"]), "option_ids": matched})
        else:
            answers.append({"item_id": str(item["id"]), "option_ids": [], "other_text": reply})
    return answers


# ---- Target --------------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentTargetConfig:
    """対象構成（結果に記録し、hash を取る。URL のほかの資格情報は持たない）。"""

    mcp_url: str
    agent_id: str
    run_timeout_seconds: float = DEFAULT_RUN_TIMEOUT_SECONDS

    def provenance(self) -> dict[str, Any]:
        return {
            "kind": TARGET_KIND,
            "mcp_url": self.mcp_url,
            "agent_id": self.agent_id,
            "run_timeout_seconds": self.run_timeout_seconds,
            "output_schema_versions": {
                "agent_chat": RUN_OUTPUT_SCHEMA_VERSION,
                "agent_get_run": RUN_OUTPUT_SCHEMA_VERSION,
                "agent_answer_confirmation": RUN_OUTPUT_SCHEMA_VERSION,
                "agent_get_run_observation": OBSERVATION_SCHEMA_VERSION,
                "agent_cancel_run": CANCEL_SCHEMA_VERSION,
            },
        }


class AgentMcpTarget:
    """評価のケースを業務 Agent の Run として流し、公開の出力を観測として返す。

    `clock` は往復の時間を測る時計、`AgentMcp` の時計は Run の待ちの期限の時計（テストで
    差し替える）。
    """

    kind = TARGET_KIND

    def __init__(
        self,
        api: AgentMcp,
        config: AgentTargetConfig,
        *,
        labels: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        api_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._api = api
        self._config = config
        self._labels = dict(labels or {})
        self._clock = clock
        self._api_clock = api_clock

    def run_case(self, case: Case) -> CaseObservation:
        observation = CaseObservation(case_id=case.id, target_kind=TARGET_KIND)
        thread_id: str | None = None
        pending: Mapping[str, Any] | None = None
        utterances = case_utterances(case, self._labels)
        for index, (text, conditions) in enumerate(utterances):
            turn, run = self._turn(index, text, conditions, thread_id=thread_id, pending=pending)
            observation.turns.append(turn)
            thread_id = turn.refs.get("thread_id") or thread_id
            pending = None
            if turn.status == "confirmation" and run is not None:
                if index + 1 < len(utterances):
                    pending = run
                    continue
                # 確認で終わったケース（次の返答が無い）。確認の待ちを残さない。
                turn.refs["cancel_result"] = self._api.cancel_run(str(run["run_id"]))
                break
            if turn.status != "completed" or _approval_skipped(turn):
                break
        return observation

    def _turn(
        self,
        index: int,
        text: str,
        conditions: Mapping[str, str],
        *,
        thread_id: str | None,
        pending: Mapping[str, Any] | None,
    ) -> tuple[TurnObservation, Mapping[str, Any] | None]:
        started = self._clock()
        deadline = self._api_clock() + self._config.run_timeout_seconds
        request: dict[str, Any] = {"utterance": index}
        refs: dict[str, str | None] = {"run_id": None, "thread_id": thread_id}
        try:
            if pending is not None:
                answers = confirmation_answers(pending.get("confirmation") or {}, text, conditions)
                confirmation = pending.get("confirmation") or {}
                request.update(
                    tool="agent_answer_confirmation",
                    confirmation_id=str(confirmation.get("id") or ""),
                    answers=answers,
                )
                created = self._api.answer_confirmation(
                    run_id=str(pending["run_id"]),
                    confirmation_id=str(confirmation.get("id") or ""),
                    answers=answers,
                    deadline=deadline,
                )
            else:
                request.update(tool="agent_chat", agent_id=self._config.agent_id)
                created = self._api.chat(
                    agent_id=self._config.agent_id,
                    question=text,
                    thread_id=thread_id,
                    deadline=deadline,
                )
            refs["run_id"] = str(created["run_id"])
            refs["thread_id"] = str(created.get("thread_id") or "") or thread_id
            run = self._api.wait_run(created, deadline=deadline)
            elapsed = (self._clock() - started) * 1000
        except (TargetError, httpx.HTTPError, KeyError) as error:
            return self._error_turn(index, error, request=request, refs=refs), None
        if run is None:
            refs["cancel_result"] = self._api.cancel_run(str(refs["run_id"]))
            return (
                TurnObservation(
                    turn=index,
                    status="timeout",
                    target_status="running",
                    elapsed_ms=elapsed,
                    refs=refs,
                    request=request,
                ),
                None,
            )
        status = str(run.get("status"))
        payload: dict[str, Any] = {"agent_get_run": dict(run)}
        versions = {"agent_get_run": RUN_OUTPUT_SCHEMA_VERSION}
        if status == "completed":
            try:
                payload["agent_get_run_observation"] = self._api.observation(str(refs["run_id"]))
            except (TargetError, httpx.HTTPError) as error:
                return self._error_turn(index, error, request=request, refs=refs), None
            versions["agent_get_run_observation"] = OBSERVATION_SCHEMA_VERSION
        turn_status = {
            "completed": "completed",
            "waiting_confirmation": "confirmation",
            "failed": "failed",
            "cancelled": "cancelled",
            "waiting_approval": "waiting_approval",
        }.get(status, "failed")
        turn = TurnObservation(
            turn=index,
            status=turn_status,
            target_status=status,
            elapsed_ms=elapsed,
            refs=refs,
            output_versions=versions,
            request=request,
            payload=payload,
        )
        return turn, run

    @staticmethod
    def _error_turn(
        index: int,
        error: Exception,
        *,
        request: Mapping[str, Any],
        refs: Mapping[str, str | None],
    ) -> TurnObservation:
        error_type = getattr(error, "error_type", None) or type(error).__name__
        if isinstance(error, httpx.HTTPError | KeyError):
            error_type = AgentEvaluationError.error_type
        return TurnObservation(
            turn=index,
            status="target_error",
            elapsed_ms=0.0,
            refs=dict(refs),
            error_type=str(error_type),
            error_message=str(error)[:300],
            request=dict(request),
        )


# ---- 観測 → 採点の入力 --------------------------------------------------------------------


def skipped_approval_tools(run: Mapping[str, Any], observation: Mapping[str, Any]) -> list[str]:
    """承認が要るため呼ばなかったツール（`skipped_approval_tools` と観測の `approval_skipped`）。"""
    names: list[str] = []
    listed = run.get("skipped_approval_tools")
    observed = [
        call.get("name")
        for call in _records(observation.get("tool_calls"))
        if call.get("status") == APPROVAL_SKIPPED
    ]
    for name in [*(listed if isinstance(listed, list) else []), *observed]:
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    return names


def _approval_skipped(turn: TurnObservation) -> bool:
    return bool(
        skipped_approval_tools(
            turn.payload.get("agent_get_run") or {},
            turn.payload.get("agent_get_run_observation") or {},
        )
    )


def evidence_citations(observation: Mapping[str, Any]) -> tuple[Citation, ...]:
    """観測の根拠（`rank` の順・重複なし）。文書と chunk の ID の無い根拠は数えない。"""
    citations: list[Citation] = []
    seen: set[tuple[str, str]] = set()
    items = sorted(
        _records(observation.get("evidence")), key=lambda item: _int(item.get("rank")) or 0
    )
    for item in items:
        document_id = str(item.get("document_id") or "")
        chunk_id = str(item.get("chunk_id") or "")
        if not document_id or not chunk_id or (document_id, chunk_id) in seen:
            continue
        seen.add((document_id, chunk_id))
        locator = item.get("locator")
        citations.append(
            Citation(
                document_id=document_id,
                chunk_id=chunk_id,
                text=str(item.get("text") or ""),
                text_omitted=item.get("text_omitted") is True,
                locator=(
                    {key: value for key, value in locator.items() if value is not None}
                    if isinstance(locator, Mapping)
                    else {}
                ),
            )
        )
    return tuple(citations)


def answer_outcome(run: Mapping[str, Any]) -> str | None:
    """`agent_get_run` の対応（`outcome`。#1305）。無い・語彙に無ければ None。"""
    value = run.get("outcome")
    return value if isinstance(value, str) and value in OUTCOMES else None


def _completed_view(turn: TurnObservation, *, infer_missing_outcome: bool) -> TurnView:
    run = turn.payload.get("agent_get_run") or {}
    observation = turn.payload.get("agent_get_run_observation") or {}
    citations = evidence_citations(observation)
    outcome = answer_outcome(run)
    source: str | None = OUTCOME_SOURCE_ANSWER if outcome is not None else None
    if outcome is None and infer_missing_outcome:
        answer = run.get("answer")
        outcome = infer_outcome(
            answer if isinstance(answer, str) else "", has_citations=bool(citations)
        )
        source = OUTCOME_SOURCE_INFERRED
    withheld = max(
        _int(observation.get("withheld_evidence_count")) or 0,
        _int(run.get("withheld_evidence_count")) or 0,
    )
    text = run.get("answer")
    return TurnView(
        turn=turn.turn,
        trace_id=str(turn.refs.get("run_id") or ""),
        elapsed_ms=turn.elapsed_ms,
        outcome=outcome,
        outcome_source=source,
        citations=citations,
        withheld_evidence_count=withheld,
        answer=answer_body(text) if isinstance(text, str) else "",
    )


def _failure(
    turn: TurnObservation, *, run_timeout_seconds: float, tools: Sequence[str]
) -> CaseFailure:
    trace_id = str(turn.refs.get("run_id") or "")
    stage = f"turn_{turn.turn}"
    if turn.status == "target_error":
        return CaseFailure(
            error_type=turn.error_type or AgentEvaluationError.error_type,
            error_message=turn.error_message or "",
            error_stage=stage,
            trace_id=trace_id,
        )
    kind = APPROVAL_SKIPPED if tools else turn.status
    error_type, template = _STATUS_ERRORS.get(
        kind,
        (f"AgentRun{kind.title().replace('_', '')}", f"Agent の Run が {kind} で終わりました。"),
    )
    message = template.format(timeout=run_timeout_seconds, tools="・".join(tools))
    return CaseFailure(
        error_type=error_type,
        error_message=message + _RUN_HINT,
        error_stage=stage,
        trace_id=trace_id,
    )


def agent_case_view(
    observation: CaseObservation,
    *,
    infer_missing_outcome: bool = False,
    run_timeout_seconds: float = DEFAULT_RUN_TIMEOUT_SECONDS,
) -> CaseView:
    """保存した観測を採点の入力にする（規則は RULES_VERSION）。

    完了でない往復・承認の要るツールを呼ばなかった往復（#3240 の Q7）があれば、そこで止めてケースの
    失敗にする（それまでの往復は採点しない。時間は往復の和）。確認の往復は、引用の無い確認の質問
    （needs_clarification）として採点する。
    """
    views: list[TurnView] = []
    failure: CaseFailure | None = None
    for turn in observation.turns:
        if turn.status == "completed":
            tools = skipped_approval_tools(
                turn.payload.get("agent_get_run") or {},
                turn.payload.get("agent_get_run_observation") or {},
            )
            if tools:
                failure = _failure(turn, run_timeout_seconds=run_timeout_seconds, tools=tools)
                break
            views.append(_completed_view(turn, infer_missing_outcome=infer_missing_outcome))
        elif turn.status == "confirmation":
            views.append(
                TurnView(
                    turn=turn.turn,
                    trace_id=str(turn.refs.get("run_id") or ""),
                    elapsed_ms=turn.elapsed_ms,
                    outcome="needs_clarification",
                    outcome_source=OUTCOME_SOURCE_CONFIRMATION,
                )
            )
        else:
            failure = _failure(turn, run_timeout_seconds=run_timeout_seconds, tools=())
            break
    if failure is None and not views:
        failure = CaseFailure(
            error_type=AgentEvaluationError.error_type,
            error_message="観測した往復がありません。",
            error_stage=None,
            trace_id="",
        )
    return CaseView(
        case_id=observation.case_id,
        turns=tuple(views),
        failure=failure,
        elapsed_ms=round(sum(turn.elapsed_ms for turn in observation.turns), 3),
    )


# ---- Run の要約（Target の診断。採点には使わない） ----------------------------------------------


def tool_call_counts(observation: Mapping[str, Any]) -> dict[str, int]:
    """ツールごとの呼び出しの数（失敗・承認で呼ばなかった呼び出しを含む）。"""
    counts: dict[str, int] = {}
    for call in _records(observation.get("tool_calls")):
        if call.get("name"):
            name = base_tool_name(str(call["name"]))
            counts[name] = counts.get(name, 0) + 1
    return counts


def superseded_search_count(observation: Mapping[str, Any]) -> int:
    """旧版も含めて検索した（`include_superseded: true`）根拠のツールの成功した呼び出しの数。

    #1392。失敗した呼び出しと、根拠のツール以外は数えない。
    """
    return sum(
        1
        for call in _records(observation.get("tool_calls"))
        if base_tool_name(str(call.get("name") or "")) in EVIDENCE_TOOLS
        and call.get("status") == "completed"
        and call.get("include_superseded") is True
    )


def run_summaries(
    observations: Sequence[CaseObservation], *, infer_missing_outcome: bool = False
) -> list[dict[str, Any]]:
    """往復ごとの Run の要約（本文は含めない）。"""
    summaries: list[dict[str, Any]] = []
    for case in observations:
        for turn in case.turns:
            run = turn.payload.get("agent_get_run") or {}
            observation = turn.payload.get("agent_get_run_observation") or {}
            usage = observation.get("usage")
            skipped = skipped_approval_tools(run, observation)
            # 採点した往復だけ対応を出す（承認の要るツールを飛ばした往復はケースの失敗）。
            view = (
                _completed_view(turn, infer_missing_outcome=infer_missing_outcome)
                if turn.status == "completed" and not skipped
                else None
            )
            summaries.append(
                {
                    "case_id": case.case_id,
                    "turn": turn.turn,
                    "run_id": turn.refs.get("run_id"),
                    "thread_id": turn.refs.get("thread_id"),
                    "status": turn.status,
                    "target_status": turn.target_status,
                    "elapsed_ms": round(turn.elapsed_ms, 1),
                    "outcome": view.outcome if view else None,
                    "outcome_source": view.outcome_source
                    if view
                    else (OUTCOME_SOURCE_CONFIRMATION if turn.status == "confirmation" else None),
                    "validation_status": observation.get("validation_status")
                    if isinstance(observation.get("validation_status"), str)
                    else None,
                    "model_requests": _int(usage.get("requests"))
                    if isinstance(usage, Mapping)
                    else None,
                    "tool_calls": tool_call_counts(observation),
                    "superseded_searches": superseded_search_count(observation),
                    "agent_version": _int(observation.get("agent_version")),
                    "skipped_approval_tools": skipped,
                    "withheld_evidence_count": _int(observation.get("withheld_evidence_count"))
                    or 0,
                    "evidence_text_omitted": sum(
                        1
                        for item in _records(observation.get("evidence"))
                        if item.get("text_omitted") is True
                    ),
                    "cancel_result": turn.refs.get("cancel_result"),
                }
            )
    return summaries


def superseded_case_summary(
    cases: Sequence[Case], runs: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """旧版も検索して答えるケース（`include_superseded: true`。#1366）で、旧版を検索したか。"""
    searched = {run["case_id"] for run in runs if run.get("superseded_searches")}
    marked = [case.id for case in cases if case.include_superseded]
    return {
        "case_count": len(marked),
        "searched_case_ids": [case_id for case_id in marked if case_id in searched],
        "missed_case_ids": [case_id for case_id in marked if case_id not in searched],
    }


def target_summary(
    cases: Sequence[Case],
    observations: Sequence[CaseObservation],
    *,
    infer_missing_outcome: bool = False,
) -> dict[str, Any]:
    """Agent の Run の診断（使った版・対応の出所・旧版の検索・Run の要約）。"""
    runs = run_summaries(observations, infer_missing_outcome=infer_missing_outcome)
    sources: dict[str, int] = {}
    for run in runs:
        source = run.get("outcome_source")
        if isinstance(source, str):
            sources[source] = sources.get(source, 0) + 1
    return {
        "agent_versions": sorted(
            {run["agent_version"] for run in runs if run.get("agent_version") is not None}
        ),
        "outcome_sources": sources,
        "superseded_cases": superseded_case_summary(cases, runs),
        "runs": runs,
    }
