"""製品横断の評価の基線（#3240 の段 1）の Run を、公開の Agent MCP の出力で再現する（段 2）。

共有の fixture `platform/contracts/mcp/fixtures/agent-evaluation-baseline.json` の各 Run は、
段 1 の合成の Run の記録（`source`）と、その Run を Agent の公開の MCP で読んだ出力
（`agent_get_run`・`agent_get_run_observation`、終わっていない Run は `agent_cancel_run`）を持つ。
このテストは記録を保存先に置いて本物の MCP の入口（API キー）で読み、出力が fixture と同じで、
契約（`agent-tools.json` の outputSchema）に合うことを確かめる。製品横断の評価の基線
（`suite/evaluation/tests/test_baseline.py`）は、この公開の出力だけで採点する。

Agent の観測・出力を意図して変えたら
`UPDATE_AGENT_EVALUATION_BASELINE=1 uv run pytest tests/test_evaluation_baseline_observation.py`
で fixture の公開の出力を書き直し、suite の基線の採点が変わらないことを確かめる。
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator
from mcp_support import fake_product_mcp
from pr_system_settings.auth.api_keys import api_key_rate_limiter
from pytest import MonkeyPatch
from security_support import client, enable_production_auth, login

from app.features.agent.runtime import (
    AgentProfile,
    Artifact,
    RunState,
    RunStatus,
    RunStep,
    RunUsage,
    StepStatus,
    runtime_repository,
)
from app.features.agent.tools import ToolCall, ToolResult
from app.security.permissions import RUNS_OPERATE, RUNS_VIEW
from app.security.service import set_security_service

CONTRACTS = Path(__file__).resolve().parents[3] / "platform/contracts/mcp"
FIXTURE = CONTRACTS / "fixtures/agent-evaluation-baseline.json"
CONTRACT = CONTRACTS / "agent-tools.json"
# fixture の業務 Agent の ID（テストの業務 Agent の ID をこの値に置き換えて比べる）。
AGENT_ID = "agent-baseline"
# 観測を 1 回で読む件数（suite の評価の adapter と同じ契約の上限）。
OBSERVATION_LIMIT = 100
T0 = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
# 終わっていない Run（suite の評価の adapter が時間切れで取り消す）。
UNFINISHED = frozenset({RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_CONFIRMATION})
PUBLIC_TOOLS = ("agent_get_run", "agent_get_run_observation", "agent_cancel_run")


@dataclass
class Setup:
    agent_id: str
    owner: str
    token: str


@pytest.fixture
def setup(monkeypatch: MonkeyPatch) -> Iterator[Setup]:
    auth = enable_production_auth(monkeypatch)
    suffix = uuid4().hex[:8]
    agent_id = f"{AGENT_ID}-{suffix}"
    runtime_repository.create_agent(
        AgentProfile(id=agent_id, name="Agent A", instructions="答える。")
    )
    owner = auth.user_with_permissions(
        f"baseline-{suffix}", [RUNS_VIEW, RUNS_OPERATE], agent_ids=[agent_id]
    )
    # 根拠の確かめ直し（#3252）は RAG の契約どおりの fake（すべて見られる）。
    fake_product_mcp(monkeypatch)
    api_key_rate_limiter.reset()
    created = client.post(
        "/api/account/api-keys",
        json={"name": "評価の機械", "products": ["agent"]},
        headers=login(owner.login_user_id),
    )
    assert created.status_code == 200, created.text
    try:
        yield Setup(agent_id=agent_id, owner=owner.user_uuid, token=created.json()["data"]["token"])
    finally:
        set_security_service(None)
        repository: Any = runtime_repository
        with repository._lock:  # noqa: SLF001 - テストの後始末
            for run_id in [
                run_id for run_id, run in repository._runs.items() if run.agent_id == agent_id
            ]:
                repository._runs.pop(run_id)
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent(agent_id)


def _run_state(source: Mapping[str, Any], setup: Setup) -> RunState:
    """段 1 の合成の Run の記録を、保存先の Run にする（時刻・ID は決まった値）。"""
    run_id = str(source["id"])
    steps: list[RunStep] = []
    for index, step in enumerate(source.get("steps", [])):
        call = step["tool_call"]
        result = step.get("tool_result")
        steps.append(
            RunStep(
                id=f"{run_id}-step-{index}",
                run_id=run_id,
                status=StepStatus(step["status"]),
                tool_call=ToolCall(name=call["name"], arguments=call.get("arguments") or {}),
                tool_result=(
                    ToolResult(
                        name=result["name"],
                        success=bool(result.get("success")),
                        output=result.get("output"),
                        error_code=result.get("error_code"),
                    )
                    if result is not None
                    else None
                ),
            )
        )
    usage = source.get("usage")
    return RunState(
        id=run_id,
        goal="合成の質問",
        agent_id=setup.agent_id,
        runtime_id="builtin",
        status=RunStatus(source["status"]),
        created_by_user_uuid=setup.owner,
        thread_id=source.get("thread_id"),
        usage=RunUsage(**usage) if isinstance(usage, Mapping) else RunUsage(),
        steps=steps,
        artifacts=[
            Artifact(name=artifact["kind"], kind=artifact["kind"], content=artifact["content"])
            for artifact in source.get("artifacts", [])
        ],
        created_at=T0,
        updated_at=T0 + timedelta(minutes=1),
    )


def _call(setup: Setup, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    response = client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": dict(arguments)},
        },
        headers={"authorization": f"Bearer {setup.token}"},
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()["result"]
    assert result.get("isError") is not True, result
    content: dict[str, Any] = result["structuredContent"]
    return content


def _normalized(value: dict[str, Any], agent_id: str) -> dict[str, Any]:
    """テストの業務 Agent の ID を fixture の ID にする（ほかの値は決まった値）。"""
    normalized: dict[str, Any] = json.loads(
        json.dumps(value, ensure_ascii=False).replace(agent_id, AGENT_ID)
    )
    return normalized


def _public_outputs(setup: Setup, source: Mapping[str, Any]) -> dict[str, Any]:
    run = _run_state(source, setup)
    repository: Any = runtime_repository
    with repository._lock:  # noqa: SLF001 - テスト用に Run を直接置く
        repository._runs[run.id] = run  # noqa: SLF001
    outputs = {
        "agent_get_run": _call(setup, "agent_get_run", {"run_id": run.id, "wait_seconds": 0}),
        "agent_get_run_observation": _call(
            setup, "agent_get_run_observation", {"run_id": run.id, "limit": OBSERVATION_LIMIT}
        ),
    }
    if run.status in UNFINISHED:
        outputs["agent_cancel_run"] = _call(setup, "agent_cancel_run", {"run_id": run.id})
    return _normalized(outputs, setup.agent_id)


def _validators() -> dict[str, Draft202012Validator]:
    tools = json.loads(CONTRACT.read_text(encoding="utf-8"))["tools"]
    return {
        tool["name"]: Draft202012Validator(tool["outputSchema"])
        for tool in tools
        if tool["name"] in PUBLIC_TOOLS
    }


def test_baseline_runs_read_through_the_public_mcp_match_the_shared_fixture(
    setup: Setup,
) -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    actual = {entry["run_id"]: _public_outputs(setup, entry["source"]) for entry in fixture["runs"]}
    if os.environ.get("UPDATE_AGENT_EVALUATION_BASELINE") == "1":
        for entry in fixture["runs"]:
            for tool in PUBLIC_TOOLS:
                entry.pop(tool, None)
            entry.update(actual[entry["run_id"]])
        FIXTURE.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", "utf-8")
    expected = {
        entry["run_id"]: {tool: entry[tool] for tool in PUBLIC_TOOLS if tool in entry}
        for entry in fixture["runs"]
    }
    assert actual == expected, (
        "公開の MCP の出力が共有の fixture と違います。意図した変更なら "
        "UPDATE_AGENT_EVALUATION_BASELINE=1 で書き直し、suite の評価の基線を確かめてください。"
    )


def test_shared_fixture_outputs_follow_the_public_contract() -> None:
    """fixture の公開の出力は契約の outputSchema に合う（suite の評価は契約の形だけを読む）。"""
    validators = _validators()
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert fixture["runs"]
    for entry in fixture["runs"]:
        assert {"agent_get_run", "agent_get_run_observation"} <= set(entry), entry["run_id"]
        for tool in PUBLIC_TOOLS:
            if tool in entry:
                errors = [error.message for error in validators[tool].iter_errors(entry[tool])]
                assert errors == [], (entry["run_id"], tool, errors)
        # 観測は 1 回で全件を読める大きさ（suite の基線の再生はページをたどらない）。
        assert entry["agent_get_run_observation"]["next_cursor"] is None, entry["run_id"]
