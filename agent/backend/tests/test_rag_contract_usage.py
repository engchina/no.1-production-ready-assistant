"""Agent が RAG に依存してよいのは公開の契約だけ（#3240 の 3 章）。

公開の契約は platform/contracts/mcp/rag-tools.json（ツールの名前・inputSchema・outputSchema・
annotations・失敗の code の `errors`）と platform/contracts/answer-passages/**（段落の規則の事例と
語彙）。
ここでは、Agent が名前・code・語で RAG に結び付く所がすべて契約にあり、Agent のテストが使う RAG の
既定の出力（fake）が契約の outputSchema だけで作られていることを確かめる。RAG の内部の module・表・
画面の API を使わないことは test_product_boundaries.py が確かめる。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from mcp_support import DEFAULT_OUTPUTS, RAG_OUTPUT_SCHEMA_VERSION, RAG_OUTPUTS_FIXTURE

from app.features.agent import evidence_access, rag_figures, section_coverage, support_task
from app.features.agent.answer_passage_vocabulary import vocabulary
from app.features.agent.answer_passages import KIND_UNVERIFIED, non_claim_passages
from app.features.agent.answer_validation import VALIDATE_ANSWER_TOOL
from app.features.agent.clarification_cancel import KIND_RAG

CONTRACT = Path(__file__).resolve().parents[3] / "platform/contracts/mcp/rag-tools.json"


def _contract() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(CONTRACT.read_text(encoding="utf-8"))
    return loaded


def _tools() -> dict[str, dict[str, Any]]:
    return {tool["name"]: tool for tool in _contract()["tools"]}


def _closed(node: Any) -> Any:
    """properties を持つ object に `additionalProperties: false` を足した schema（契約に無い項目を
    誤りにする）。"""
    if isinstance(node, dict):
        if "properties" in node and "additionalProperties" not in node:
            node["additionalProperties"] = False
        for value in node.values():
            _closed(value)
    elif isinstance(node, list):
        for value in node:
            _closed(value)
    return node


def test_rag_tools_used_by_agent_are_in_the_contract() -> None:
    """Agent が名前で呼ぶ・結果を読む RAG のツールは、すべて契約にある。"""
    used = {
        support_task.RAG_SEARCH,
        support_task.RAG_RETRIEVE_EVIDENCE,
        support_task.RAG_LOOKUP_GUIDES,
        support_task.RAG_READ_SOURCE,
        support_task.RAG_READ_DOCUMENT,
        support_task.RAG_READ_SECTION,
        support_task._PROFILE_RECOMMEND_TOOLS[KIND_RAG],
        VALIDATE_ANSWER_TOOL,
        rag_figures.READ_SOURCE_TOOL,
        evidence_access.READABLE_DOCUMENTS_TOOL,
    }
    assert used <= set(_tools())


def test_rag_error_codes_used_by_agent_are_in_the_contract() -> None:
    """Agent が分岐に使う RAG のツールの失敗の code は、契約の `errors` に載っている（RAG の内部の
    定数を写さない）。"""
    codes = {item["code"] for item in _contract()["errors"]}
    used = (
        set(rag_figures._TOOL_ERRORS)
        | set(rag_figures._PREVIEW_TOOL_ERRORS)
        | set(section_coverage._STALE_CODES)
    )
    assert used
    assert used <= codes, sorted(used - codes)


def test_validate_answer_claims_carry_the_passage_range() -> None:
    """最終の検証が判定を段落に結び付ける位置（`answer_range`）は契約にある（段落の分け方を
    写さない。`answer_validation.align_claims`）。"""
    schema = _tools()[VALIDATE_ANSWER_TOOL]["outputSchema"]
    claim = schema["$defs"]["ValidatedClaim"]["properties"]
    assert "answer_range" in claim
    ranges = [item for item in claim["answer_range"]["anyOf"] if "$ref" in item]
    target = schema["$defs"][ranges[0]["$ref"].rsplit("/", 1)[-1]]
    assert set(target["required"]) == {"start", "end"}


def test_browser_url_path_prefixes_are_in_the_contract() -> None:
    """図・原文のプレビューを開く URL の path の始まり（Agent が付け替えてよい path の確かめ）は、
    契約の `pattern` にある（RAG の route を写さない）。"""
    defs = _tools()[rag_figures.READ_SOURCE_TOOL]["outputSchema"]["$defs"]
    assert defs["EvidenceImageUrl"]["properties"]["path"]["pattern"] == (
        f"^{rag_figures.FIGURE_PATH_PREFIX}"
    )
    preview = defs["EvidencePreview"]["properties"]
    assert preview["content_path"]["pattern"] == f"^{rag_figures.SOURCE_PREVIEW_PATH_PREFIX}"
    templates = [item for item in preview["page_path_template"]["anyOf"] if "pattern" in item]
    assert templates == [
        {"pattern": f"^{rag_figures.SOURCE_PREVIEW_PATH_PREFIX}", "type": "string"}
    ]


def test_search_limit_defaults_come_from_the_contract() -> None:
    """件数の既定（Agent が小さすぎる件数を引き上げる値）は契約の inputSchema にある。"""
    for name in (support_task.RAG_SEARCH, support_task.RAG_RETRIEVE_EVIDENCE):
        properties = _tools()[name]["inputSchema"]["properties"]
        for argument in support_task.SEARCH_LIMIT_ARGUMENTS:
            assert isinstance(properties[argument]["default"], int), (name, argument)


@pytest.mark.parametrize("title", vocabulary()["unverified_section_titles"])
def test_rag_answer_sections_are_recognized_from_the_shared_vocabulary(title: str) -> None:
    """RAG の回答の末尾の節（回答に入れられなかった範囲）の見出しは共有の語彙から読み、その節の
    行を主張として確かめない。"""
    answer = (
        f"申請は管理者が行います。\n\n**{title}**\n- 合成の対象の期限は資料に記載がありません。"
    )

    kinds = non_claim_passages(answer)

    assert kinds.get(f"**{title}**") is not None
    assert kinds.get("- 合成の対象の期限は資料に記載がありません。") is not None
    assert KIND_UNVERIFIED in kinds.values()
    assert "申請は管理者が行います。" not in kinds


def test_shared_rag_outputs_follow_the_contract_output_schema() -> None:
    """Agent のテストが使う RAG の既定の出力（共有の合成の例）は、契約の outputSchema に合い、
    契約に無い項目を持たない。"""
    fixture = json.loads(RAG_OUTPUTS_FIXTURE.read_text(encoding="utf-8"))
    tools = _tools()
    assert set(fixture["outputs"]) == set(tools)
    for name, output in fixture["outputs"].items():
        schema = _closed(copy.deepcopy(tools[name]["outputSchema"]))
        errors = list(jsonschema.Draft202012Validator(schema).iter_errors(output))
        assert not errors, (name, [error.message for error in errors[:5]])
        assert output["schema_version"] == schema["properties"]["schema_version"]["default"]
    assert fixture["schema_version"] == RAG_OUTPUT_SCHEMA_VERSION


def test_fake_rag_default_outputs_are_the_shared_fixture() -> None:
    """fake の RAG の既定の出力は共有の例そのもの（テストごとに足す出力は各テストが持つ）。"""
    fixture = json.loads(RAG_OUTPUTS_FIXTURE.read_text(encoding="utf-8"))
    for name, output in fixture["outputs"].items():
        if name == evidence_access.READABLE_DOCUMENTS_TOOL:
            # 引数と見られない文書から fake が作る。
            continue
        assert DEFAULT_OUTPUTS[name] == output, name
