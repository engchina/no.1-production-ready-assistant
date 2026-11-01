"""Agent の MCP のツールの契約（platform/contracts/mcp/agent-tools.json）と実装の一致（#1874）。

契約は外部の MCP クライアント（Claude Code・Codex など）が呼ぶツールの名前・inputSchema・
outputSchema・annotations の正本。ツールを変えたら
`UPDATE_MCP_CONTRACT=1 uv run pytest tests/test_mcp_contract.py` で契約を書き直す。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, cast

from fastapi import Request

from app.features.agent.answer_outcome import ANSWER_OUTCOMES
from app.features.agent.mcp_server import build_agent_mcp_server

CONTRACT = Path(__file__).resolve().parents[3] / "platform/contracts/mcp/agent-tools.json"


def test_agent_mcp_tools_match_platform_contract() -> None:
    # ツールの定義は request を使わない（request は実行時の利用者の判定だけに使う）。
    server = build_agent_mcp_server(cast(Request, None))
    actual = {
        "server": server.name,
        "tools": sorted(
            (tool.descriptor() for tool in server.tools.values()), key=lambda t: t["name"]
        ),
    }
    if os.environ.get("UPDATE_MCP_CONTRACT") == "1":
        text = json.dumps(actual, ensure_ascii=False, indent=2) + "\n"
        CONTRACT.write_text(text, encoding="utf-8")
    assert json.loads(CONTRACT.read_text(encoding="utf-8")) == actual, (
        "MCP ツールの定義が契約と違います。"
        "意図した変更なら UPDATE_MCP_CONTRACT=1 で契約を更新してください。"
    )


def test_instructions_are_step_format() -> None:
    """MCP の instructions は、目的の文と処理順の `## Step N:` の見出し（docs/agents/prompts.md。
    #3830）。見出しはすべて Step で、1 から順に置く。"""
    text = build_agent_mcp_server(cast(Request, None)).instructions or ""
    assert not text.startswith("#")
    headings = re.findall(r"^#+ .*$", text, re.M)
    steps = [int(number) for number in re.findall(r"^## Step (\d+): \S", text, re.M)]
    assert steps and len(steps) == len(headings), headings
    assert steps == list(range(1, len(steps) + 1)), steps


RAG_CONTRACT = CONTRACT.with_name("rag-tools.json")


def _tools(path: Path) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {tool["name"]: tool for tool in data["tools"]}


def _enum(schema: dict[str, Any]) -> list[str]:
    """`enum` か、`anyOf` の enum（null を許す Literal）の値。"""
    if "enum" in schema:
        return list(schema["enum"])
    return [value for item in schema.get("anyOf", []) for value in item.get("enum", [])]


def test_run_output_uses_the_names_and_vocabulary_of_the_rag_contract() -> None:
    """RunOutput の outcome は rag_search の outcome と同じ語彙、根拠の ID は RAG の根拠と同じ名前
    （#3240。RAG が名前・語彙を変えたらここで落ちる）。"""
    rag = _tools(RAG_CONTRACT)["rag_search"]["outputSchema"]
    for name in ("agent_chat", "agent_get_run", "agent_answer_confirmation"):
        run_output = _tools(CONTRACT)[name]["outputSchema"]
        outcome = run_output["properties"]["outcome"]
        assert _enum(outcome) == _enum(rag["properties"]["outcome"]), name
        assert {"type": "null"} in outcome["anyOf"], name
        assert set(_enum(outcome)) == ANSWER_OUTCOMES
        rag_evidence = rag["$defs"]["RagEvidence"]["properties"]
        source = run_output["$defs"]["SourceOutput"]["properties"]
        for field in ("document_id", "chunk_id"):
            assert rag_evidence[field]["type"] == "string"
            assert {"type": "string"} in source[field]["anyOf"], (name, field)


def test_every_agent_tool_output_has_a_schema_version() -> None:
    """3 製品の MCP の出力の外枠をそろえる（形の版を記録できる。#3240）。"""
    for name, tool in _tools(CONTRACT).items():
        assert "schema_version" in tool["outputSchema"]["properties"], name


def test_run_observation_uses_the_names_of_the_rag_contract() -> None:
    """観測の根拠は RAG の根拠（RagEvidence）・場所（EvidenceLocator）と同じ名前・型の部分集合、
    outcome は rag_search の outcome と同じ語彙（#3240。RAG が名前を変えたらここで落ちる）。"""
    rag = _tools(RAG_CONTRACT)["rag_search"]["outputSchema"]
    observation = _tools(CONTRACT)["agent_get_run_observation"]["outputSchema"]
    assert _enum(observation["properties"]["outcome"]) == _enum(rag["properties"]["outcome"])
    rag_locator = rag["$defs"]["EvidenceLocator"]["properties"]
    locator = observation["$defs"]["ObservedLocator"]["properties"]
    assert set(locator) <= set(rag_locator)
    assert "bbox" not in locator
    for field, schema in locator.items():
        assert schema.get("type") == rag_locator[field].get("type"), field
        assert schema.get("anyOf") == rag_locator[field].get("anyOf"), field
    # 表のセルの座標（#3262）: RAG の EvidenceTableCell と同じ名前・型で写す（場所と同じく、セルの
    # 領域の bbox は写さない。#3429）。
    assert {"table_row", "table_cells"} <= set(locator)
    rag_cell = rag["$defs"]["EvidenceTableCell"]
    cell = observation["$defs"]["ObservedTableCell"]
    assert "bbox" in rag_cell["properties"]
    assert set(cell["properties"]) == set(rag_cell["properties"]) - {"bbox"}
    assert set(cell["required"]) == set(rag_cell["required"])
    for field, schema in cell["properties"].items():
        assert schema.get("type") == rag_cell["properties"][field].get("type"), field
        assert schema.get("items") == rag_cell["properties"][field].get("items"), field
    rag_evidence = rag["$defs"]["RagEvidence"]["properties"]
    evidence = observation["$defs"]["ObservedEvidence"]["properties"]
    shared = {
        "document_id",
        "chunk_id",
        "file_name",
        "chunk_set_id",
        "superseded",
        "retrieval_rank",
        "answer_use",
        "text_omitted",
        "text_length",
        "truncated",
    }
    assert shared <= set(evidence) and shared <= set(rag_evidence)
    assert _enum(evidence["answer_use"]) == _enum(rag_evidence["answer_use"])
    # 表の行・列の対応の検査が読めるか・縮退・根拠 0 件の段（#3546）: RAG と同じ名前・語彙。
    assert _enum(evidence["table_binding"]) == _enum(rag_evidence["table_binding"])
    call = observation["$defs"]["ObservedToolCall"]["properties"]
    assert set(observation["$defs"]["ObservedDegradedStep"]["properties"]) == set(
        rag["$defs"]["DegradedStep"]["properties"]
    )
    assert _enum(call["zero_evidence_stage"]) == _enum(
        rag["$defs"]["ZeroEvidence"]["properties"]["stage"]
    )
    assert "degraded_steps" in call and "degraded_steps" in rag["properties"]
    assert "guardrail_degraded" not in call and "guardrail_degraded" not in rag["properties"]
    # スコアは出さない（内部の算法に依存させない）。
    assert not {"score", "rerank_score"} & set(evidence)
