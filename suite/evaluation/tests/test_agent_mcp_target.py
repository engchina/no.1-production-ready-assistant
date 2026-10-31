"""Target: 公開の Agent MCP の adapter（#1289・#3240）。

Agent の MCP（`POST /api/mcp`）は MockTransport の台本で、出力は契約
（`platform/contracts/mcp/agent-tools.json`）の形の合成の値。送る引数は契約の inputSchema で
確かめる。
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from baseline_support import check_arguments

from pr_suite_evaluation.cli import DEFAULT_API_KEY_ENV, main
from pr_suite_evaluation.dataset import Dataset, LoadedDataset
from pr_suite_evaluation.experiment import ExperimentRecord, ScorerConfig, run_experiment
from pr_suite_evaluation.mcp_client import MCP_PROTOCOL_VERSION, McpClient
from pr_suite_evaluation.targets.agent_mcp import (
    OBSERVATION_PAGE_LIMIT,
    OBSERVATION_SCHEMA_VERSION,
    AgentEvaluationError,
    AgentMcp,
    AgentMcpTarget,
    AgentTargetConfig,
    confirmation_answers,
    evidence_citations,
    known_conditions_text,
)

THREAD = "thread_" + "a" * 32
API_KEY = "prak_synthetic"
URL = "http://agent/api/mcp"


def _evidence(document_id: str, chunk_id: str, text: str, **fields: Any) -> dict[str, Any]:
    """観測の根拠（`agent_get_run_observation` の `evidence[]`）。"""
    return {
        "rank": 0,
        "tool_call_index": 0,
        "source": "search",
        "document_id": document_id,
        "chunk_id": chunk_id,
        "file_name": "manual.pdf",
        "chunk_set_id": None,
        "evidence_type": "text",
        "origin": "source",
        "superseded": False,
        "retrieval_rank": 1,
        "cited": False,
        "answer_use": None,
        "locator": {
            "section_path": ["3. 権限の付与"],
            "page_start": 1,
            "page_end": 1,
            "page_label_start": None,
            "page_label_end": None,
            "sheet_name": None,
            "row_start": None,
            "row_end": None,
            "cell_range": None,
            "element_locator": None,
        },
        "text": text,
        "text_length": len(text),
        "text_omitted": False,
        "truncated": False,
        **fields,
    }


def _call(
    name: str, *, status: str = "completed", include_superseded: bool | None = None
) -> dict[str, Any]:
    return {
        "index": 0,
        "call_id": "step-1",
        "name": f"rag__{name}",
        "status": status,
        "error_code": None,
        "started_at": None,
        "completed_at": None,
        "evidence_count": None,
        "include_superseded": include_superseded,
        "job_status": None,
        "row_count": None,
        "rows_truncated": None,
    }


def _run(
    run_id: str,
    *,
    answer: str,
    status: str = "completed",
    outcome: str | None = None,
    confirmation: dict[str, Any] | None = None,
    skipped: list[str] | None = None,
) -> dict[str, Any]:
    """`agent_chat` / `agent_get_run` / `agent_answer_confirmation` の出力（RunOutput）。"""
    return {
        "schema_version": 4,
        "run_id": run_id,
        "agent_id": "agent-1",
        "thread_id": THREAD,
        "status": status,
        "outcome": outcome if status == "completed" else None,
        "in_progress": status in {"queued", "running"},
        "answer": answer,
        "confirmation": confirmation,
        "sources": [],
        "sources_text_omitted": 0,
        "withheld_evidence_count": 0,
        "passage_marks": [],
        "removed_passages": [],
        "answer_warnings": [],
        "tools_used": [],
        "skipped_approval_tools": skipped or [],
        "pending_approvals": 0,
        "message": "合成の案内",
        "notice": None,
    }


def _observation(
    run_id: str,
    *,
    evidence: list[dict[str, Any]] | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """`agent_get_run_observation` の出力（RunObservation）。"""
    items = evidence or []
    for rank, item in enumerate(items, start=1):
        item["rank"] = rank
    return {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "run_id": run_id,
        "agent_id": "agent-1",
        "thread_id": THREAD,
        "agent_version": 3,
        "trace_id": None,
        "status": "completed",
        "outcome": None,
        "validation_status": "completed",
        "created_at": "2026-10-25T01:00:00Z",
        "finished_at": "2026-10-25T01:01:00Z",
        "usage": {"model": None, "requests": 3, "input_tokens": 0, "output_tokens": 0},
        "tool_calls": tool_calls or [],
        "tool_call_total": len(tool_calls or []),
        "evidence": items,
        "evidence_total": len(items),
        "withheld_evidence_count": 0,
        "next_cursor": None,
        **fields,
    }


CONFIRMATION = {
    "id": "confirm-1",
    "source": "agent",
    "items": [
        {
            "id": "target",
            "prompt": "付与先はどれですか？",
            "reason": "",
            "selection": "single",
            "options": [
                {"id": "opt-individual", "label": "個別", "description": ""},
                {"id": "opt-group", "label": "グループ", "description": ""},
            ],
        }
    ],
    "round": 1,
    "max_rounds": 2,
    "can_skip": True,
}


class FakeAgentMcp:
    """台本の Agent の MCP。

    台本は 1 回の `agent_chat` / `agent_answer_confirmation` ごとに（状態の列, 観測）。状態の列の
    先頭を呼び出しが返し、残りを `agent_get_run` が順に返す（最後の状態を返し続ける）。
    """

    def __init__(self, scripts: list[tuple[list[dict[str, Any]], dict[str, Any] | None]]) -> None:
        self.scripts = scripts
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.headers: list[httpx.Headers] = []
        self.methods: list[str] = []
        self.cancelled: list[str] = []
        self.polls: dict[str, list[dict[str, Any]]] = {}
        self.observations: dict[str, dict[str, Any] | None] = {}
        self.overrides: dict[str, dict[str, Any]] = {}
        self._next = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(request.headers)
        message = json.loads(request.content)
        self.methods.append(message["method"])
        if message["method"] == "initialize":
            return self._reply(message, {"protocolVersion": MCP_PROTOCOL_VERSION})
        tool = message["params"]["name"]
        arguments = message["params"]["arguments"]
        check_arguments(tool, arguments)
        self.calls.append((tool, arguments))
        if tool in self.overrides:
            return self._reply(message, self.overrides[tool])
        if tool in {"agent_chat", "agent_answer_confirmation"}:
            states, observation = self.scripts[self._next]
            self._next += 1
            run_id = states[0]["run_id"]
            self.polls[run_id] = list(states[1:]) or [states[0]]
            self.observations[run_id] = observation
            return self._result(message, states[0])
        run_id = arguments["run_id"]
        if tool == "agent_cancel_run":
            self.cancelled.append(run_id)
            return self._result(
                message,
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "status": "cancelled",
                    "result": "cancelled",
                    "message": "実行を取り消しました。",
                },
            )
        if tool == "agent_get_run_observation":
            observation = self.observations[run_id]
            assert observation is not None
            return self._result(message, observation)
        queue = self.polls[run_id]
        state = queue[0] if len(queue) == 1 else queue.pop(0)
        return self._result(message, state)

    def tools(self) -> list[str]:
        return [tool for tool, _ in self.calls]

    @staticmethod
    def _reply(message: dict[str, Any], result: dict[str, Any]) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

    def _result(self, message: dict[str, Any], content: dict[str, Any]) -> httpx.Response:
        return self._reply(
            message,
            {
                "content": [{"type": "text", "text": json.dumps(content, ensure_ascii=False)}],
                "structuredContent": content,
                "isError": False,
            },
        )


def _mcp(handler: Callable[[httpx.Request], httpx.Response]) -> McpClient:
    return McpClient(
        httpx.Client(transport=httpx.MockTransport(handler)),
        URL,
        api_key=API_KEY,
        label="Agent",
        error_class=AgentEvaluationError,
    )


def _dataset(
    cases: list[dict[str, Any]], thresholds: dict[str, float] | None = None
) -> LoadedDataset:
    return LoadedDataset(
        dataset=Dataset.model_validate({"cases": cases, "thresholds": thresholds}),
        name="synthetic",
        sha256="0" * 64,
    )


def _evaluate(
    fake: FakeAgentMcp,
    cases: list[dict[str, Any]],
    *,
    labels: dict[str, str] | None = None,
    timeout: float = 60,
    api_clock: Callable[[], float] | None = None,
    infer_missing_outcome: bool = False,
) -> ExperimentRecord:
    clock_kwargs: dict[str, Any] = {"clock": api_clock} if api_clock else {}
    config = AgentTargetConfig(mcp_url=URL, agent_id="agent-1", run_timeout_seconds=timeout)
    target = AgentMcpTarget(
        AgentMcp(_mcp(fake), **clock_kwargs),
        config,
        labels=labels,
        **({"api_clock": api_clock} if api_clock else {}),
    )
    return run_experiment(
        _dataset(cases),
        target,
        target_config=config.provenance(),
        scorer=ScorerConfig(
            infer_missing_outcome=infer_missing_outcome, run_timeout_seconds=timeout
        ),
        log=lambda _: None,
    )


GRANT_CASE: dict[str, Any] = {
    "id": "mt-grant",
    "category": "clarification_required",
    "split": "dev",
    "query": "権限を付けたいのですが、手順を教えてください。",
    "relevant_document_ids": ["doc-manual"],
    "expected_outcomes": ["needs_clarification", "conditional"],
    "turns": [
        {
            "reply": "個別の利用者に付けます。",
            "conditions": {"target": "個別"},
            "expected_outcomes": ["answered", "conditional"],
        }
    ],
    "required_evidence": [
        {
            "id": "grant-individual",
            "document_id": "doc-manual",
            "text": "個別の利用者に付与する場合",
        }
    ],
}
ANSWER_EVIDENCE = _evidence("doc-manual", "c1", "個別の利用者に付与する場合: 利用者の詳細画面")


def test_multi_turn_case_follows_the_thread() -> None:
    clarify = _run(
        "run-1", answer="付与先は個別ですか、グループですか？", outcome="needs_clarification"
    )
    answered = _run("run-2", answer="個別の利用者は詳細画面で付与します。", outcome="answered")
    fake = FakeAgentMcp(
        [
            # 1 往復目は待ちの時間内に終わらず、agent_get_run で続きを読む。
            (
                [{**clarify, "status": "running", "in_progress": True, "outcome": None}, clarify],
                _observation("run-1", tool_calls=[_call("rag_search")]),
            ),
            (
                [answered],
                _observation("run-2", evidence=[ANSWER_EVIDENCE], tool_calls=[_call("rag_search")]),
            ),
        ]
    )
    record = _evaluate(fake, [GRANT_CASE])
    chats = [args for tool, args in fake.calls if tool == "agent_chat"]
    assert [{k: v for k, v in chat.items() if k != "wait_seconds"} for chat in chats] == [
        {"agent_id": "agent-1", "question": GRANT_CASE["query"]},
        {"agent_id": "agent-1", "question": "個別の利用者に付けます。", "thread_id": THREAD},
    ]
    assert fake.tools() == [
        "agent_chat",
        "agent_get_run",
        "agent_get_run_observation",
        "agent_chat",
        "agent_get_run_observation",
    ]
    result = record.report.case_results[0]
    assert result.status == "success"
    assert result.response_decision_accuracy == 1
    assert [turn.observed_outcome for turn in result.turn_results] == [
        "needs_clarification",
        "answered",
    ]
    assert result.required_evidence_recall == 1.0
    assert record.report.metrics_version == "six_metrics_v1+agent_v3"
    assert [run["agent_version"] for run in record.target["runs"]] == [3, 3]
    # 観測は公開の出力そのものを保存する（採点し直せる）。
    turn = record.observations[0].turns[1]
    assert turn.payload["agent_get_run"]["run_id"] == "run-2"
    assert turn.output_versions == {
        "agent_get_run": 4,
        "agent_get_run_observation": OBSERVATION_SCHEMA_VERSION,
    }


def test_pre_answer_confirmation_is_answered_with_the_next_reply() -> None:
    """回答の前の確認に、次の返答を agent_answer_confirmation で答え、同じ Run の続きを読む。"""
    waiting = _run("run-1", answer="", status="waiting_confirmation", confirmation=CONFIRMATION)
    answered = _run("run-1", answer="個別の利用者は詳細画面で付与します。", outcome="answered")
    fake = FakeAgentMcp(
        [
            ([waiting], None),
            ([answered], _observation("run-1", evidence=[ANSWER_EVIDENCE])),
        ]
    )
    record = _evaluate(fake, [GRANT_CASE])
    assert fake.tools() == ["agent_chat", "agent_answer_confirmation", "agent_get_run_observation"]
    answer = dict(fake.calls[1][1])
    answer.pop("wait_seconds")
    # 分かった条件の値（target=個別）と同じ名前の選択肢を選ぶ。
    assert answer == {
        "run_id": "run-1",
        "confirmation_id": "confirm-1",
        "action": "answer",
        "answers": [{"item_id": "target", "option_ids": ["opt-individual"]}],
    }
    assert fake.cancelled == []
    result = record.report.case_results[0]
    assert result.status == "success"
    assert [(turn.observed_outcome, turn.outcome_source) for turn in result.turn_results] == [
        ("needs_clarification", "agent_confirmation"),
        ("answered", "agent_answer"),
    ]
    assert result.response_decision_accuracy == 1.0
    assert result.required_evidence_recall == 1.0
    assert [turn.status for turn in record.observations[0].turns] == ["confirmation", "completed"]


def test_confirmation_without_a_next_reply_ends_the_case_and_cancels_the_run() -> None:
    waiting = _run("run-1", answer="", status="waiting_confirmation", confirmation=CONFIRMATION)
    fake = FakeAgentMcp([([waiting], None)])
    case = {"id": "one-shot", "query": "権限の付け方は？", "expected_outcomes": ["answered"]}
    record = _evaluate(fake, [case])
    assert fake.cancelled == ["run-1"]
    result = record.report.case_results[0]
    # 確認で終わった（期待は回答）ので、判断の誤り（ケースの失敗ではない）。
    assert result.status == "success"
    assert result.observed_outcome == "needs_clarification"
    assert result.response_decision_accuracy == 0.0
    assert result.failure_reasons == ["unexpected_handling"]
    assert record.observations[0].turns[0].refs["cancel_result"] == "cancelled"


def test_confirmation_answers_use_free_text_when_no_option_matches() -> None:
    answers = confirmation_answers(CONFIRMATION, "部署の全員に付けます。", {"scope": "部署"})
    assert answers == [
        {"item_id": "target", "option_ids": [], "other_text": "部署の全員に付けます。"}
    ]


def test_requests_follow_the_mcp_transport_with_the_api_key() -> None:
    fake = FakeAgentMcp(
        [([_run("run-1", answer="答え", outcome="answered")], _observation("run-1"))] * 2
    )
    _evaluate(fake, [{"id": "a", "query": "q1"}, {"id": "b", "query": "q2"}])
    assert fake.methods[0] == "initialize"
    assert fake.methods.count("initialize") == 1
    assert {headers["authorization"] for headers in fake.headers} == {f"Bearer {API_KEY}"}
    assert {headers["mcp-protocol-version"] for headers in fake.headers} == {MCP_PROTOCOL_VERSION}
    chats = [args for tool, args in fake.calls if tool == "agent_chat"]
    assert all(1 <= chat["wait_seconds"] <= 300 for chat in chats)


def test_superseded_cases_record_whether_the_agent_searched_old_versions() -> None:
    searched = _observation(
        "run-1",
        tool_calls=[
            _call("rag_retrieve_evidence", include_superseded=False),
            _call("rag_retrieve_evidence", include_superseded=True),
            _call("rag_search", status="failed", include_superseded=True),
            _call("rag_read_source"),
        ],
    )
    missed = _observation("run-2", tool_calls=[_call("rag_search", include_superseded=False)])
    cases: list[dict[str, Any]] = [
        {"id": "versions-a", "query": "期限は旧版から変わった？", "include_superseded": True},
        {"id": "versions-b", "query": "改定前との違いは？", "include_superseded": True},
        {"id": "current", "query": "期限は？"},
    ]
    fake = FakeAgentMcp(
        [
            (
                [_run("run-1", answer="旧版は 5 日、今の版は 3 日です。", outcome="answered")],
                searched,
            ),
            ([_run("run-2", answer="今の版は月 1 回です。", outcome="answered")], missed),
            (
                [_run("run-3", answer="今の版は 3 日です。", outcome="answered")],
                _observation("run-3"),
            ),
        ]
    )
    record = _evaluate(fake, cases)
    assert [run["superseded_searches"] for run in record.target["runs"]] == [1, 0, 0]
    assert record.target["runs"][0]["tool_calls"] == {
        "rag_retrieve_evidence": 2,
        "rag_search": 1,
        "rag_read_source": 1,
    }
    assert record.target["superseded_cases"] == {
        "case_count": 2,
        "searched_case_ids": ["versions-a"],
        "missed_case_ids": ["versions-b"],
    }


def test_known_conditions_are_added_to_the_first_question_with_guide_labels() -> None:
    case = {
        "id": "known",
        "query": "権限を付与したいです。どの画面で操作しますか？",
        "conditions": {"target": "グループ"},
        "expected_outcomes": ["answered", "conditional"],
    }
    run = _run("run-1", answer="グループの詳細画面で付与します。")
    observation = _observation(
        "run-1", evidence=[_evidence("doc-manual", "c2", "承認を得てください")]
    )
    fake = FakeAgentMcp([([run], observation)])
    record = _evaluate(fake, [case], labels={"target": "付与先"})
    chat = next(args for tool, args in fake.calls if tool == "agent_chat")
    assert chat["question"].endswith("（既知の条件: 付与先は「グループ」）")
    assert known_conditions_text({"approved": "はい"}, {}) == "（既知の条件: approvedは「はい」）"
    # outcome の無い完了の Run は、既定では推定せず判断の正しさを評価不能にする。
    result = record.report.case_results[0]
    assert (result.observed_outcome, result.outcome_source) == (None, None)
    assert result.metric_status["response_decision_accuracy"].reason == "outcome_missing"


@pytest.mark.parametrize(
    ("answer", "with_evidence", "expected"),
    [
        ("確認させてください。\n部門長の承認は得ていますか？", False, "needs_clarification"),
        (
            "付与先は個別ですか？\n\nこの回答は資料の根拠を使っておらず、資料と照らし合わせて確かめていません。",
            False,
            "needs_clarification",
        ),
        ("グループの詳細画面です。", False, "insufficient_evidence"),
        ("グループの詳細画面です。", True, "answered"),
    ],
    ids=["question", "question-before-notice", "no-evidence", "answered"],
)
def test_legacy_outcome_inference(answer: str, with_evidence: bool, expected: str) -> None:
    """--infer-missing-outcome は旧形式の規則で対応を推定し、推定と残す（#1305）。"""
    evidence = [_evidence("doc-manual", "c1", "本文")] if with_evidence else []
    fake = FakeAgentMcp(
        [([_run("run-1", answer=answer)], _observation("run-1", evidence=evidence))]
    )
    record = _evaluate(fake, [{"id": "a", "query": "q"}], infer_missing_outcome=True)
    result = record.report.case_results[0]
    assert (result.observed_outcome, result.outcome_source) == (expected, "inferred")
    assert record.report.metrics_version == "six_metrics_v1+agent_v3+legacy_outcome"


def test_outcome_outside_the_vocabulary_is_not_used() -> None:
    fake = FakeAgentMcp(
        [([_run("run-1", answer="付与先は？", outcome="maybe")], _observation("run-1"))]
    )
    record = _evaluate(fake, [{"id": "a", "query": "q", "expected_outcomes": ["answered"]}])
    assert record.report.case_results[0].observed_outcome is None


def test_failed_timed_out_and_waiting_runs_are_case_errors() -> None:
    failed = _run("run-1", answer="", status="failed")
    running = _run("run-2", answer="", status="running")
    approval = _run("run-3", answer="", status="waiting_approval")
    fake = FakeAgentMcp([([failed], None), ([running], None), ([approval], None)])
    ticks = iter(range(0, 10_000, 100))
    record = _evaluate(
        fake,
        [
            {"id": "a", "query": "q1", "expected_outcomes": ["answered"]},
            {"id": "b", "query": "q2", "expected_outcomes": ["answered"]},
            {"id": "c", "query": "q3", "expected_outcomes": ["answered"]},
        ],
        timeout=150,
        api_clock=lambda: float(next(ticks)),
    )
    report = record.report
    assert report.error_count == 3
    assert report.passed is False
    assert [result.error_type for result in report.case_results] == [
        "AgentRunFailed",
        "AgentRunTimeout",
        "AgentRunWaitingApproval",
    ]
    assert report.failure_reason_counts == {"case_error": 3}
    # 待ちきれなかった Run だけ取り消す（承認待ちは画面で判断されうる）。
    assert fake.cancelled == ["run-2"]
    assert report.denominators.final_statuses == {"failed": 1, "timeout": 1, "waiting_approval": 1}
    assert "agent_get_run_observation" not in fake.tools()


def test_run_that_skipped_an_approval_tool_is_a_case_error() -> None:
    run = _run("run-1", answer="記録しました。", outcome="answered", skipped=["ext__write"])
    fake = FakeAgentMcp([([run], _observation("run-1"))])
    record = _evaluate(fake, [{"id": "a", "query": "q", "expected_outcomes": ["answered"]}])
    result = record.report.case_results[0]
    assert (result.status, result.error_type, result.error_stage) == (
        "error",
        "AgentRunApprovalSkipped",
        "turn_0",
    )
    assert result.error_message is not None and "ext__write" in result.error_message


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        (
            {
                "content": [{"type": "text", "text": "上限です。"}],
                "structuredContent": {"error_code": "API_KEY_RUN_LIMIT", "message": "上限です。"},
                "isError": True,
            },
            "API_KEY_RUN_LIMIT",
        ),
        (
            {
                "content": [],
                "structuredContent": {**_run("run-1", answer="答え"), "schema_version": 99},
                "isError": False,
            },
            "schema_version=99",
        ),
    ],
    ids=["tool-error", "unknown-schema-version"],
)
def test_tool_errors_and_unknown_output_versions_are_case_errors(
    override: dict[str, Any], expected: str
) -> None:
    """ツールのエラーと、知らない形の版の出力は推測で読まず、ケースの失敗にする。"""
    fake = FakeAgentMcp([])
    fake.overrides["agent_chat"] = override
    record = _evaluate(fake, [{"id": "a", "query": "q"}])
    result = record.report.case_results[0]
    assert (result.status, result.error_type, result.error_stage) == (
        "error",
        "AgentEvaluationError",
        "turn_0",
    )
    assert result.error_message is not None and expected in result.error_message
    assert record.report.denominators.final_statuses == {"target_error": 1}


def test_observation_pages_are_followed_until_the_last_cursor() -> None:
    first = _observation(
        "run-1",
        evidence=[_evidence("doc-1", "c-1", "本文 1")],
        next_cursor=base64.urlsafe_b64encode(b"page-2").decode(),
    )
    second = _observation("run-1", evidence=[_evidence("doc-2", "c-2", "本文 2")])
    second["evidence"][0]["rank"] = 2
    pages = iter([first, second])
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        if message["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": {}})
        requests.append(message["params"])
        content = next(pages)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {"structuredContent": content, "isError": False},
            },
        )

    observation = AgentMcp(_mcp(handler)).observation("run-1")
    assert [params["arguments"] for params in requests] == [
        {"run_id": "run-1", "limit": OBSERVATION_PAGE_LIMIT},
        {"run_id": "run-1", "limit": OBSERVATION_PAGE_LIMIT, "cursor": first["next_cursor"]},
    ]
    assert [(item.document_id, item.text) for item in evidence_citations(observation)] == [
        ("doc-1", "本文 1"),
        ("doc-2", "本文 2"),
    ]


def test_observed_evidence_becomes_citations_with_locations() -> None:
    ledger = _evidence(
        "doc-ledger",
        "c-ledger-6",
        "行6: ID: X-103 / 区分: A",
        source="read",
        locator={
            "section_path": ["台帳"],
            "page_start": None,
            "sheet_name": "台帳",
            "row_start": 6,
            "row_end": 6,
            "cell_range": "A6:F6",
            "element_locator": "doc:doc-ledger/ext:er_1/el:sheet-000-r6",
        },
    )
    pdf = _evidence("doc-pdf", "c-pdf-2", "部署の承認者")
    no_ids = _evidence("", "", "ID の無い根拠")
    duplicate = _evidence("doc-pdf", "c-pdf-2", "部署の承認者")
    observation = _observation("run-1", evidence=[pdf, ledger, no_ids, duplicate])
    observation["evidence"].reverse()
    citations = evidence_citations(observation)
    assert [item.chunk_id for item in citations] == ["c-pdf-2", "c-ledger-6"]
    assert citations[1].locator == {
        "section_path": ["台帳"],
        "sheet_name": "台帳",
        "row_start": 6,
        "row_end": 6,
        "cell_range": "A6:F6",
        "element_locator": "doc:doc-ledger/ext:er_1/el:sheet-000-r6",
    }


def test_evidence_with_omitted_text_is_unevaluable_not_a_miss() -> None:
    """場所の合う根拠の本文が省かれていたら、取れなかったとせず評価不能にする。"""
    omitted = _evidence("doc-manual", "c1", "", text_omitted=True, text_length=90_000)
    fake = FakeAgentMcp(
        [
            (
                [_run("run-1", answer="答え", outcome="answered")],
                _observation("run-1", evidence=[omitted]),
            )
        ]
    )
    case = {
        "id": "a",
        "query": "q",
        "relevant_document_ids": ["doc-manual"],
        "required_evidence": [{"id": "e1", "document_id": "doc-manual", "text": "個別の利用者"}],
    }
    record = _evaluate(fake, [case])
    result = record.report.case_results[0]
    assert result.required_evidence_recall is None
    assert result.undetermined_evidence == ["e1"]
    assert result.missing_evidence == []
    assert "evidence_miss" not in result.failure_reasons
    assert result.metric_status["required_evidence_recall"].reason == "evidence_text_omitted"
    # 順位は文書の ID で測れる。
    assert result.mrr == 1.0
    assert record.report.denominators.metrics["required_evidence_recall"].unevaluable_reasons == {
        "evidence_text_omitted": 1
    }
    assert record.target["runs"][0]["evidence_text_omitted"] == 1


def test_withheld_evidence_makes_misses_unevaluable() -> None:
    """見られない根拠があるとき、取れなかった根拠・1 位でない順位は評価不能にする。"""
    observation = _observation(
        "run-1", evidence=[_evidence("doc-other", "c9", "別の本文")], withheld_evidence_count=2
    )
    fake = FakeAgentMcp([([_run("run-1", answer="答え", outcome="answered")], observation)])
    case = {
        "id": "a",
        "query": "q",
        "relevant_document_ids": ["doc-manual"],
        "required_evidence": [{"id": "e1", "document_id": "doc-manual", "text": "本文"}],
    }
    record = _evaluate(fake, [case], infer_missing_outcome=False)
    result = record.report.case_results[0]
    assert (result.mrr, result.required_evidence_recall) == (None, None)
    assert result.metric_status["mrr"].reason == "evidence_withheld"
    assert result.metric_status["required_evidence_recall"].reason == "evidence_withheld"
    assert result.failure_reasons == []


def test_thresholded_metric_with_unevaluable_cases_does_not_pass() -> None:
    fakes: list[tuple[list[dict[str, Any]], dict[str, Any] | None]] = [
        ([_run("run-1", answer="答え", outcome="answered")], _observation("run-1")),
        ([_run("run-2", answer="答え")], _observation("run-2")),
    ]
    config = AgentTargetConfig(mcp_url=URL, agent_id="agent-1")
    target = AgentMcpTarget(AgentMcp(_mcp(FakeAgentMcp(fakes))), config)
    cases = [
        {"id": "a", "query": "q", "expected_outcomes": ["answered"]},
        {"id": "b", "query": "q", "expected_outcomes": ["answered"]},
    ]
    record = run_experiment(
        _dataset(cases, {"response_decision_accuracy": 0.5}),
        target,
        target_config=config.provenance(),
        scorer=ScorerConfig(),
        log=lambda _: None,
    )
    assert record.report.response_decision_accuracy == 1.0
    assert record.report.passed is None


# ---- CLI --------------------------------------------------------------------------------------


def _golden(tmp_path: Path) -> Path:
    golden = tmp_path / "set.json"
    golden.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "a",
                        "split": "dev",
                        "query": "q",
                        "relevant_document_ids": ["file:m.pdf"],
                        "expected_outcomes": ["answered"],
                    },
                    {
                        "id": "b",
                        "split": "holdout",
                        "query": "q",
                        "expected_outcomes": ["answered"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return golden


def _document_map(tmp_path: Path) -> Path:
    path = tmp_path / "map.json"
    path.write_text(json.dumps({"documents": {"m.pdf": "doc-m"}}), encoding="utf-8")
    return path


def test_main_runs_scores_and_compares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    observation = _observation("run-1", evidence=[_evidence("doc-m", "c1", "本文")])
    fake = FakeAgentMcp([([_run("run-1", answer="答え", outcome="answered")], observation)])
    real_client = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        kwargs.pop("trust_env", None)
        return real_client(transport=httpx.MockTransport(fake), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setenv(DEFAULT_API_KEY_ENV, API_KEY)
    output = tmp_path / "out.json"
    args = [
        "run",
        str(_golden(tmp_path)),
        "--document-map",
        str(_document_map(tmp_path)),
        "--target",
        "agent-mcp",
        "--mcp-url",
        URL,
        "--agent-id",
        "agent-eval",
        "--split",
        "dev",
        "--output",
        str(output),
    ]
    assert main(args) == 0
    record = ExperimentRecord.model_validate_json(output.read_text(encoding="utf-8"))
    assert record.report.case_count == 1
    assert record.report.case_results[0].mrr == 1.0
    assert record.provenance["target"]["agent_id"] == "agent-eval"
    assert record.provenance["dataset"]["case_ids"] == ["a"]
    assert API_KEY not in output.read_text(encoding="utf-8")
    assert {tool for tool, _ in fake.calls} <= {
        "agent_chat",
        "agent_get_run",
        "agent_get_run_observation",
        "agent_cancel_run",
        "agent_answer_confirmation",
    }
    # 保存した観測を、製品を呼ばずに採点し直す（--infer-missing-outcome を変えても呼ばない）。
    calls = len(fake.calls)
    rescored = tmp_path / "rescored.json"
    score_args = [
        "score",
        str(output),
        "--dataset",
        str(_golden(tmp_path)),
        "--document-map",
        str(_document_map(tmp_path)),
        "--infer-missing-outcome",
        "--output",
        str(rescored),
    ]
    assert main(score_args) == 0
    assert len(fake.calls) == calls
    again = ExperimentRecord.model_validate_json(rescored.read_text(encoding="utf-8"))
    assert again.report.case_results[0].mrr == 1.0
    assert again.provenance["scorer"]["config"]["infer_missing_outcome"] is True
    capsys.readouterr()
    assert main(["compare", f"D={output}", f"E={rescored}"]) == 0
    assert "| 指標 | D | E |" in capsys.readouterr().out


def test_main_requires_the_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(DEFAULT_API_KEY_ENV, raising=False)
    output = tmp_path / "o.json"
    args = ["run", str(_golden(tmp_path)), "--document-map", str(_document_map(tmp_path))]
    assert main([*args, "--agent-id", "x", "--output", str(output)]) == 2
    assert DEFAULT_API_KEY_ENV in capsys.readouterr().err
    assert not output.exists()
    # 業務 Agent を作る口は無い。
    with pytest.raises(SystemExit):
        main(["run", str(_golden(tmp_path)), "--create-agent", "--output", str(output)])


def test_main_rejects_unresolved_file_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(DEFAULT_API_KEY_ENV, API_KEY)
    output = tmp_path / "o.json"
    assert main(["run", str(_golden(tmp_path)), "--agent-id", "x", "--output", str(output)]) == 2
    assert "file:" in capsys.readouterr().err
    assert not output.exists()
