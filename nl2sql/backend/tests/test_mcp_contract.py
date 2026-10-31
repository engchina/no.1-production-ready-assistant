"""MCP ツールの契約（platform/contracts/mcp/nl2sql-tools.json）と実装の一致（#248）。

契約は Agent が呼ぶツールの名前と inputSchema の正本。ツールを変えたら
`UPDATE_MCP_CONTRACT=1 uv run pytest tests/test_mcp_contract.py` で契約を書き直し、
Agent のテスト（`agent/backend/tests/test_product_mcp.py`。fake の MCP は `tests/mcp_support.py`）も
通ることを確認する。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import cast

from fastapi import Request

from app.features.mcp.tools import _INSTRUCTIONS, MCP_OUTPUT_SCHEMA_VERSION, build_mcp_server

CONTRACT = Path(__file__).resolve().parents[3] / "platform/contracts/mcp/nl2sql-tools.json"


def test_nl2sql_mcp_tools_match_platform_contract() -> None:
    # ツールの定義は request を使わない（request は実行時の利用者の判定だけに使う）。
    server = build_mcp_server(cast(Request, None))
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


def test_every_tool_output_has_schema_version() -> None:
    """3 製品の MCP の出力の外枠をそろえ、どのツールの出力も形の版を持つ（#3240）。"""
    server = build_mcp_server(cast(Request, None))
    versions = {
        tool.name: tool.descriptor()["outputSchema"]["properties"]["schema_version"]["default"]
        for tool in server.tools.values()
    }
    assert versions == dict.fromkeys(server.tools, MCP_OUTPUT_SCHEMA_VERSION)


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
    server = build_mcp_server(cast(Request, None))
    texts = [_INSTRUCTIONS]
    for tool in server.tools.values():
        descriptor = tool.descriptor()
        texts += [descriptor["description"], *_descriptions(descriptor["inputSchema"])]
    assert [text for text in texts if _ISSUE_REF.search(text)] == []


def test_instructions_are_step_format() -> None:
    """MCP の instructions は、目的の文と処理順の `## Step N:` の見出し（docs/agents/prompts.md。
    #3830）。見出しはすべて Step で、1 から順に置く。"""
    text = _INSTRUCTIONS
    assert not text.startswith("#")
    headings = re.findall(r"^#+ .*$", text, re.M)
    steps = [int(number) for number in re.findall(r"^## Step (\d+): \S", text, re.M)]
    assert steps and len(steps) == len(headings), headings
    assert steps == list(range(1, len(steps) + 1)), steps
