"""ツールの一括露出の基線と、段階的開示を見直す条件（#70）。

標準の業務 Agent（資料・データ・横断）が 1 回目のモデルの呼び出しに渡すツールの数と、ツールの定義の
見積もり（本番の span の `pr.context.estimate.tool_definitions_tokens` と同じ見積もり）を、RAG /
NL2SQL の公開の契約（`platform/contracts/mcp/*-tools.json`）の `tools/list` から測る。

#70 の判断: 今の標準の catalog（最大 13 ツール・約 7k tokens）では段階的開示（一覧 → 詳細 →
呼び出し）を採用しない（理由と比較は `docs/agents/mcp.md`「ツールの露出」）。この見積もりが
`REEVALUATE_TOOL_DEFINITION_TOKENS` を超えたら、判断を見直す（このテストが失敗して知らせる）。

`pytest -s` で表を出す（講義の基線の再現）。外部への通信はしない（MCP は `httpx.MockTransport`）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from agents.testing import ScriptedModel, assistant_message
from pr_system_settings.model import estimate_input_tokens
from pytest import MonkeyPatch

from app.features.agent import builtin_runtime
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.config import runtime_config_store
from app.features.agent.context_window import tools_text
from app.features.agent.runtime import BUILTIN_AGENT_IDS, RunCreateRequest, runtime_repository
from app.settings import get_settings

# 段階的開示を見直す、1 つの業務 Agent のツールの定義の見積もり（tokens）。今の最大の約 2 倍で、
# 128k の入力の上限のモデルの約 1 割。
REEVALUATE_TOOL_DEFINITION_TOKENS = 15_000
CONTRACTS = Path(__file__).resolve().parents[3] / "platform" / "contracts" / "mcp"
USER_UUID = "11111111-2222-3333-4444-555555550070"
_REAL_CLIENT = httpx.Client


def _contract_listing(server_id: str) -> list[dict[str, Any]]:
    tools = json.loads((CONTRACTS / f"{server_id}-tools.json").read_text(encoding="utf-8"))["tools"]
    # モデルに渡るのは名前・説明・inputSchema（outputSchema は渡さない）。
    return [{key: value for key, value in tool.items() if key != "outputSchema"} for tool in tools]


def _handle(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if "id" not in body:
        return httpx.Response(202)
    if body["method"] == "initialize":
        result: dict[str, Any] = {"capabilities": {}}
    else:
        result = {"tools": _contract_listing(request.url.host.split(".")[0])}
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


@pytest.fixture
def contract_mcp(monkeypatch: MonkeyPatch) -> Iterator[None]:
    for server_id in ("rag", "nl2sql"):
        config = runtime_config_store.get_mcp(server_id)
        monkeypatch.setitem(
            runtime_config_store._mcp_servers,  # noqa: SLF001 - テストの接続先の差し替え
            server_id,
            config.model_copy(update={"base_url": f"https://{server_id}.test/api/mcp"}),
        )
    monkeypatch.setattr(get_settings(), "app_service_token_secret", "t" * 40)
    monkeypatch.setattr(
        "app.features.agent.tools.httpx.Client",
        lambda **kwargs: _REAL_CLIENT(
            transport=httpx.MockTransport(_handle), timeout=kwargs["timeout"]
        ),
    )
    yield


def _first_model_call(monkeypatch: MonkeyPatch, agent_id: str) -> Any:
    model = ScriptedModel([[assistant_message("合成の回答")]])
    monkeypatch.setattr(
        builtin_runtime,
        "resolve_model_target",
        lambda model_id="": ModelTarget(
            model_id="test-model", endpoint="https://llm.example", project_id="", api_key="k"
        ),
    )
    monkeypatch.setattr(builtin_runtime, "model_factory", lambda _target: model)
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="合成の質問", agent_id=agent_id), created_by_user_uuid=USER_UUID
    )
    anyio.run(builtin_runtime.execute_run, run.id)
    assert model.calls, runtime_repository.get_run(run.id).events[-1].message
    return model.calls[0]


def test_standard_agents_stay_below_the_progressive_disclosure_threshold(
    monkeypatch: MonkeyPatch, contract_mcp: None
) -> None:
    rows = []
    for agent_id in BUILTIN_AGENT_IDS:
        call = _first_model_call(monkeypatch, agent_id)
        definitions = estimate_input_tokens(tools_text(call.tools))
        instructions = estimate_input_tokens(str(call.system_instructions))
        rows.append((agent_id, len(call.tools), definitions, instructions))
    print("\nagent | tools | tool_definitions_tokens | instructions_tokens")
    for agent_id, count, definitions, instructions in rows:
        print(f"{agent_id} | {count} | {definitions} | {instructions}")

    # Skill の許可の範囲だけを渡している（全接続の全ツールではない）。
    assert {agent_id: count for agent_id, count, _, _ in rows} == {
        "default": 13,
        "rag-research": 9,
        "data-query": 5,
    }
    for agent_id, _count, definitions, _instructions in rows:
        assert definitions < REEVALUATE_TOOL_DEFINITION_TOKENS, (
            f"{agent_id} のツールの定義が約 {definitions} tokens になりました。"
            "#70 の段階的開示の判断を見直してください（docs/agents/mcp.md「ツールの露出」）。"
        )
