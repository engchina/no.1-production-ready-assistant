"""MCP ツールの契約（platform/contracts/mcp/rag-tools.json）と実装の一致（#248）。

契約は Agent が呼ぶツールの名前と inputSchema の正本。ツールを変えたら
`UPDATE_MCP_CONTRACT=1 uv run pytest tests/test_mcp_contract.py` で契約を書き直し、
Agent のテスト（`agent/backend/tests/test_product_mcp.py`。fake の MCP は `tests/mcp_support.py`）も
通ることを確認する。
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import re
from pathlib import Path
from typing import cast

from fastapi import Request

from app.mcp import tools as mcp_tools
from app.mcp.tools import (
    INSTRUCTIONS,
    MCP_ERROR_CODES,
    MCP_OUTPUT_SCHEMA_VERSION,
    build_rag_mcp_server,
)

CONTRACT = Path(__file__).resolve().parents[3] / "platform/contracts/mcp/rag-tools.json"


def test_rag_mcp_tools_match_platform_contract() -> None:
    # ツールの定義は request を使わない（request は実行時の rate limit だけに使う）。
    server = build_rag_mcp_server(cast(Request, None))
    actual = {
        "server": server.name,
        "tools": sorted(
            (tool.descriptor() for tool in server.tools.values()), key=lambda t: t["name"]
        ),
        # ツールの失敗の code と意味（#3240。呼び出し側はここに載せた code だけで分岐する）。
        "errors": [
            {"code": code, "description": description}
            for code, description in sorted(MCP_ERROR_CODES.items())
        ],
    }
    if os.environ.get("UPDATE_MCP_CONTRACT") == "1":
        text = json.dumps(actual, ensure_ascii=False, indent=2) + "\n"
        CONTRACT.write_text(text, encoding="utf-8")
    assert json.loads(CONTRACT.read_text(encoding="utf-8")) == actual, (
        "MCP ツールの定義が契約と違います。"
        "意図した変更なら UPDATE_MCP_CONTRACT=1 で契約を更新してください。"
    )


def test_rag_mcp_tool_outputs_have_schema_version() -> None:
    """ツールの出力はすべて出力の形の版（schema_version）を持つ（handoff §10。#1276）。"""
    server = build_rag_mcp_server(cast(Request, None))
    for tool in server.tools.values():
        schema = tool.descriptor()["outputSchema"]
        version = schema["properties"].get("schema_version")
        assert version is not None, tool.name
        assert version["default"] == MCP_OUTPUT_SCHEMA_VERSION, tool.name


_ISSUE_REF = re.compile(r"#\d{3,}")


def _descriptions(node: object) -> list[str]:
    if isinstance(node, dict):
        found = [node["description"]] if isinstance(node.get("description"), str) else []
        return found + [text for value in node.values() for text in _descriptions(value)]
    if isinstance(node, list):
        return [text for item in node for text in _descriptions(item)]
    return []


def test_model_visible_texts_have_no_issue_numbers() -> None:
    """モデルに渡す文（instructions・ツールの説明・inputSchema）に Issue 番号を書かない（#2275）。

    経緯はコードのコメントに置く（docs/agents/prompts.md）。
    """
    server = build_rag_mcp_server(cast(Request, None))
    texts = [INSTRUCTIONS]
    for tool in server.tools.values():
        descriptor = tool.descriptor()
        texts += [descriptor["description"], *_descriptions(descriptor["inputSchema"])]
    assert [text for text in texts if _ISSUE_REF.search(text)] == []


def test_instructions_are_step_format() -> None:
    """MCP の instructions は、目的の文と処理順の `## Step N:` の見出し（docs/agents/prompts.md。
    #3830）。見出しはすべて Step で、1 から順に置く。"""
    text = INSTRUCTIONS
    assert not text.startswith("#")
    headings = re.findall(r"^#+ .*$", text, re.M)
    steps = [int(number) for number in re.findall(r"^## Step (\d+): \S", text, re.M)]
    assert steps and len(steps) == len(headings), headings
    assert steps == list(range(1, len(steps) + 1)), steps


def test_tool_error_codes_are_in_the_contract() -> None:
    """ツールが raise する code（`McpToolError` の最初の引数）と `*_CODE` の定数は、すべて
    契約の `errors`（`MCP_ERROR_CODES`）に載る（#3240。載っていない code で呼び出し側が分岐
    しない）。
    """
    tree = ast.parse(inspect.getsource(mcp_tools))
    raised: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "McpToolError"
            and node.args
        ):
            first = node.args[0]
            if isinstance(first, ast.Name):
                raised.add(getattr(mcp_tools, first.id))
            elif isinstance(first, ast.Constant) and isinstance(first.value, str):
                raised.add(first.value)
            else:
                raise AssertionError(
                    f"code を静的に決められない McpToolError（{ast.unparse(node)}）"
                )
    constants = {
        value
        for name, value in vars(mcp_tools).items()
        if name.endswith("_CODE") and isinstance(value, str)
    }
    assert raised
    missing = (raised | constants) - set(MCP_ERROR_CODES)
    assert not missing, sorted(missing)


FIXTURE = CONTRACT.parent / "fixtures" / "rag-tool-outputs.json"


def test_shared_output_fixture_is_a_valid_rag_output() -> None:
    """Agent のテストが使う共有の出力の例（fixtures/rag-tool-outputs.json）は、RAG の出力の型で
    読めて、型が書き出す形（既定を含むすべての項目）と同じ（#3240。Agent のテストを公開の契約で
    動かす）。"""
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    server = build_rag_mcp_server(cast(Request, None))
    assert fixture["schema_version"] == MCP_OUTPUT_SCHEMA_VERSION
    assert set(fixture["outputs"]) == set(server.tools)
    for name, tool in server.tools.items():
        assert tool.output_model is not None, name
        output = fixture["outputs"][name]
        dumped = tool.output_model.model_validate(output).model_dump(mode="json")
        assert dumped == output, name
