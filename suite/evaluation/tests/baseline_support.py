"""製品横断の評価の基線（`fixtures/agent_baseline/`）の再生（テストの補助）。

共有の fixture `platform/contracts/mcp/fixtures/agent-evaluation-baseline.json` の公開の
Agent MCP の出力（Agent の `tests/test_evaluation_baseline_observation.py` が本物の MCP の入口で
作り、契約に合うことを確かめる）を `httpx.MockTransport` で再生し、suite の Target・Evaluator・
Report に流す。再生の条件は fixture の README.md「再生の条件」。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from pr_suite_evaluation.dataset import Dataset, LoadedDataset, canonical_sha256
from pr_suite_evaluation.experiment import ExperimentRecord, ScorerConfig, run_experiment
from pr_suite_evaluation.mcp_client import McpClient
from pr_suite_evaluation.targets.agent_mcp import (
    AgentEvaluationError,
    AgentMcp,
    AgentMcpTarget,
    AgentTargetConfig,
)

REPO = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "agent_baseline"
CONTRACTS = REPO / "platform" / "contracts" / "mcp"
PUBLIC_FIXTURE = CONTRACTS / "fixtures" / "agent-evaluation-baseline.json"
AGENT_CONTRACT = CONTRACTS / "agent-tools.json"
# 再生の条件（README.md「再生の条件」）。時間切れの Run はこの秒数で打ち切る。
RUN_TIMEOUT_SECONDS = 2.0
AGENT_ID = "agent-baseline"
API_KEY = "prak_synthetic"
MCP_URL = "http://agent/api/mcp"
# suite が読む公開の出力（共有の fixture の Run ごとの項目）。
PUBLIC_TOOLS = ("agent_get_run", "agent_get_run_observation", "agent_cancel_run")
OBSERVATIONS = frozenset(
    {"completed", "failed", "timeout", "cancelled", "waiting_approval", "api_error"}
)


def load_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_case_fixtures() -> dict[str, dict[str, Any]]:
    fixtures: dict[str, dict[str, Any]] = {}
    for path in sorted((FIXTURES / "cases").glob("*.json")):
        fixture = json.loads(path.read_text(encoding="utf-8"))
        fixtures[fixture["id"]] = fixture
    return fixtures


def load_public_runs() -> dict[str, dict[str, Any]]:
    """共有の fixture の Run ごとの公開の出力（suite が読む項目だけ）。"""
    data = json.loads(PUBLIC_FIXTURE.read_text(encoding="utf-8"))
    return {
        entry["run_id"]: {tool: entry[tool] for tool in PUBLIC_TOOLS if tool in entry}
        for entry in data["runs"]
    }


CASE_FIXTURES = load_case_fixtures()
PUBLIC_RUNS = load_public_runs()
SUMMARIES: list[dict[str, Any]] = load_json("summaries.json")["suites"]
RAG_CONTRACT = CONTRACTS / "rag-tools.json"


def _tools(path: Path) -> dict[str, dict[str, Any]]:
    return {tool["name"]: tool for tool in json.loads(path.read_text(encoding="utf-8"))["tools"]}


AGENT_TOOLS = _tools(AGENT_CONTRACT)
RAG_TOOLS = _tools(RAG_CONTRACT)


def check_arguments(
    tool: str, arguments: Mapping[str, Any], tools: Mapping[str, Mapping[str, Any]] = AGENT_TOOLS
) -> None:
    """送る引数が契約（agent-tools.json / rag-tools.json の inputSchema）に合う。"""
    schema = tools[tool]["inputSchema"]
    properties: dict[str, Any] = schema["properties"]
    assert set(schema.get("required", [])) <= set(arguments), (tool, arguments)
    assert set(arguments) <= set(properties), (tool, arguments)
    for name, value in arguments.items():
        options = properties[name].get("anyOf", [properties[name]])
        matched = False
        for option in options:
            kind = option.get("type")
            if kind == "null":
                matched = matched or value is None
            elif kind == "string" and isinstance(value, str):
                pattern = option.get("pattern")
                enum = option.get("enum")
                matched = matched or (
                    (pattern is None or re.fullmatch(pattern, value) is not None)
                    and (enum is None or value in enum)
                )
            elif kind == "integer" and isinstance(value, int):
                low, high = option.get("minimum", value), option.get("maximum", value)
                matched = matched or low <= value <= high
            elif kind == "array" and isinstance(value, list):
                matched = matched or len(value) <= option.get("maxItems", len(value))
            elif kind == "boolean" and isinstance(value, bool):
                matched = True
            elif kind == "object" and isinstance(value, dict):
                limit = option.get("maxProperties", len(value))
                matched = matched or (
                    len(value) <= limit and all(isinstance(item, str) for item in value.values())
                )
        assert matched, (tool, name, value)


def tool_result(content: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(content, ensure_ascii=False)}],
        "structuredContent": dict(content),
        "isError": False,
    }


class ReplayAgentMcp:
    """Agent の公開の MCP を、共有の fixture の公開の出力で再生する。

    `agent_chat` ごとに fixture の次の往復を出し（`api_error` は HTTP の失敗）、ほかのツールはその
    往復の Run の公開の出力を返す（時間切れの Run の `agent_get_run` は running のまま）。
    """

    def __init__(
        self,
        fixtures: Sequence[Mapping[str, Any]],
        runs: Mapping[str, Mapping[str, Any]] = PUBLIC_RUNS,
    ) -> None:
        self._turns: Iterator[Mapping[str, Any]] = iter(
            [turn for fixture in fixtures for turn in fixture["turns"]]
        )
        self._runs = runs
        self.chat_requests: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self.tools: list[str] = []
        self.authorizations: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/mcp"
        self.authorizations.add(request.headers.get("authorization", ""))
        message = json.loads(request.content)
        if message["method"] == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"protocolVersion": message["params"]["protocolVersion"]},
                },
            )
        assert message["method"] == "tools/call"
        tool = message["params"]["name"]
        arguments = message["params"]["arguments"]
        check_arguments(tool, arguments)
        self.tools.append(tool)
        if tool == "agent_chat":
            self.chat_requests.append(
                {key: value for key, value in arguments.items() if key != "wait_seconds"}
            )
            turn = next(self._turns)
            if turn["observation"] == "api_error":
                return httpx.Response(int(turn["http_status"]), text="synthetic failure")
            content = self._runs[turn["run_id"]]["agent_get_run"]
        else:
            run_id = arguments["run_id"]
            if tool == "agent_cancel_run":
                self.cancelled.append(run_id)
            content = self._runs[run_id][tool]
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": message["id"], "result": tool_result(content)}
        )


def turn_clock(fixtures: Sequence[Mapping[str, Any]]) -> Callable[[], float]:
    """往復の時計。往復ごとに 0 秒と、fixture の elapsed_ms の時刻を返す。

    Target は往復の始まりと Run の終わりで 1 回ずつ時計を読む（Run を作れなかった往復は始まりの
    1 回だけ）。
    """
    ticks: list[float] = []
    for fixture in fixtures:
        for turn in fixture["turns"]:
            ticks.append(0.0)
            if turn["observation"] != "api_error":
                ticks.append(float(turn["elapsed_ms"]) / 1000)
    queue = iter(ticks)
    return lambda: next(queue)


def api_clock() -> Callable[[], float]:
    """Run の待ちの時計（読むたびに 1 秒進む）。時間切れの Run は数回の取得で打ち切る。"""
    state = {"now": 0.0}

    def clock() -> float:
        state["now"] += 1.0
        return state["now"]

    return clock


@dataclass
class Replay:
    dataset: LoadedDataset
    record: ExperimentRecord
    agent: ReplayAgentMcp


def baseline_dataset(
    fixtures: Sequence[Mapping[str, Any]], thresholds: Mapping[str, float] | None = None
) -> LoadedDataset:
    dataset = Dataset.model_validate(
        {"cases": [fixture["case"] for fixture in fixtures], "thresholds": thresholds}
    )
    return LoadedDataset(
        dataset=dataset, name="baseline", sha256=canonical_sha256(dataset.model_dump(mode="json"))
    )


def replay(
    fixtures: Sequence[Mapping[str, Any]],
    thresholds: Mapping[str, float] | None = None,
    runs: Mapping[str, Mapping[str, Any]] = PUBLIC_RUNS,
    *,
    infer_missing_outcome: bool = False,
) -> Replay:
    agent = ReplayAgentMcp(fixtures, runs)
    wait_clock = api_clock()
    mcp = McpClient(
        httpx.Client(transport=httpx.MockTransport(agent)),
        MCP_URL,
        api_key=API_KEY,
        label="Agent",
        error_class=AgentEvaluationError,
    )
    config = AgentTargetConfig(
        mcp_url=MCP_URL, agent_id=AGENT_ID, run_timeout_seconds=RUN_TIMEOUT_SECONDS
    )
    target = AgentMcpTarget(
        AgentMcp(mcp, clock=wait_clock),
        config,
        clock=turn_clock(fixtures),
        api_clock=wait_clock,
    )
    dataset = baseline_dataset(fixtures, thresholds)
    record = run_experiment(
        dataset,
        target,
        target_config=config.provenance(),
        scorer=ScorerConfig(
            infer_missing_outcome=infer_missing_outcome, run_timeout_seconds=RUN_TIMEOUT_SECONDS
        ),
        log=lambda _: None,
    )
    assert agent.authorizations <= {f"Bearer {API_KEY}"}
    return Replay(dataset=dataset, record=record, agent=agent)


def run_ids(fixture: Mapping[str, Any]) -> list[str]:
    return [turn["run_id"] for turn in fixture["turns"] if "run_id" in turn]


def final_observation(fixture: Mapping[str, Any]) -> str:
    return str(fixture["turns"][-1]["observation"])
