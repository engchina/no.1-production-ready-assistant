"""業務ガイドの手順の説明の充足を、最終の検証で確かめる（#2708）。

`rag_validate_answer` の `guide_steps`（RAG の回答と同じ規則の手順ごとの状態）で、説明を確かめられ
なかった必須の手順を回答の不足として示し、対応を conditional にする。支援タスクの状態では、共有の
chunk だけを根拠に持つ手順を区別する。合成の fixture。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from mcp_support import DEFAULT_OUTPUTS

from app.features.agent.answer_outcome import SIGNAL_GUIDE_STEPS, answer_outcome
from app.features.agent.answer_validation import (
    REVIEW_HEADING,
    REVIEW_INTRO,
    STATUS_COMPLETED,
    combine_validations,
    merge_results,
    validation_content,
)
from app.features.agent.guide_steps import guide_step_states
from app.features.agent.runtime import RunStep, StepStatus
from app.features.agent.tools import ToolCall, ToolResult

EVIDENCE = [{"document_id": "doc-1", "chunk_id": "chunk-1"}]
ANSWER = "1. 設定画面を開き、設定 A に値を入れてください。\n2. 最後は保存ボタンで画面を閉じます。"
GAP = "業務ガイドの手順「内容の確定」は、取得した資料で説明を確かめられませんでした。"


def _result(steps: list[dict[str, Any]], *, valid: bool = True) -> dict[str, Any]:
    result: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_validate_answer"])
    lines = ANSWER.split("\n")
    result.update(
        valid=valid,
        counts={"supported": len(lines)},
        claims=[
            {"answer_quote": line, "status": "supported", "chunk_id": "chunk-1", "reason": "記載"}
            for line in lines
        ],
        checks=["guide", "guide_steps", "impact"],
        guide_steps=steps,
        findings=[
            {
                "check": "guide_steps",
                "code": "guide_step_unexplained",
                "severity": "warning",
                "message": GAP.replace("内容の確定", step["title"]),
                "request_id": None,
                "step_id": step["step_id"],
                "related_step_id": None,
            }
            for step in steps
            if step["required"] and step["status"] != "explained"
        ],
    )
    return result


def _step(step_id: str, title: str, status: str, *, required: bool = True) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "title": title,
        "required": required,
        "status": status,
        "validation": {"body_coverage": {"execution": "completed", "status": "missing"}},
    }


def _combined(*results: dict[str, Any]) -> tuple[dict[str, Any], str]:
    return combine_validations(
        ANSWER,
        [
            validation_content(STATUS_COMPLETED, result=result, evidence=EVIDENCE)
            for result in results
        ],
    )


def test_unexplained_required_step_is_shown_and_makes_the_answer_conditional() -> None:
    """裏付けのある回答でも、説明を確かめられない必須の手順は不足として示し、answered にしない。

    本文は外さず、回答全体の警告（guide_check。文は RAG の回答の不足の行と同じ）にして、節
    「確認が必要な点」に示す（#2708・#2738）。
    """
    result = _result([_step("s1", "値の登録", "explained"), _step("s2", "内容の確定", "unused")])
    content, published = _combined(result)
    label = "業務ガイドの手順の確認で問題が見つかった"
    assert published == (f"{ANSWER}\n\n**{REVIEW_HEADING}**\n\n{REVIEW_INTRO}\n- {label}: {GAP}")
    assert content["answer_warnings"] == [{"code": "guide_check", "label": label, "detail": GAP}]
    assert content["passage_marks"] == []
    assert content["withheld"] == {
        "removed": 0,
        "unverified": 0,
        "warnings": 0,
        "all": False,
        "listed": 1,
    }
    assert content["notice"]["items"] == 1
    outcome = answer_outcome(ANSWER, steps=[], validation=content)
    assert outcome["value"] == "conditional"
    assert SIGNAL_GUIDE_STEPS in outcome["signals"]


def test_all_steps_explained_keep_the_answer_answered() -> None:
    result = _result([_step("s1", "値の登録", "explained"), _step("s2", "内容の確定", "explained")])
    content, published = _combined(result)
    assert published == ANSWER
    outcome = answer_outcome(ANSWER, steps=[], validation=content)
    assert (outcome["value"], outcome["signals"]) == ("answered", [])
    # 必須でない（条件で決まらない分岐の）手順は不足にしない。
    optional = _result(
        [_step("s1", "値の登録", "explained"), _step("s2", "内容の確定", "unused", required=False)]
    )
    content, published = _combined(optional)
    assert published == ANSWER
    assert answer_outcome(ANSWER, steps=[], validation=content)["value"] == "answered"


def test_unexplained_steps_are_listed_with_marked_paragraphs() -> None:
    """根拠で確かめられない段落は本文に残して印を付け、手順の不足と同じ節に並べる（#2738）。"""
    result = _result([_step("s1", "値の登録", "no_evidence")], valid=False)
    result["claims"][1]["status"] = "unsupported"
    result["counts"] = {"supported": 1, "unsupported": 1}
    content, published = _combined(result)
    body, section = published.split(f"\n\n**{REVIEW_HEADING}**\n\n")
    assert body == ANSWER
    assert "- 根拠で確かめられない: 「2. 最後は保存ボタンで画面を閉じます。」" in section
    # 手順の資料未取得は説明不足の確認ではない。主張の未支持の印は独立して保つ。
    assert GAP.replace("内容の確定", "値の登録") not in section
    assert content["answer_warnings"] == []
    assert [(item["text"], item["state"]) for item in content["passage_marks"]] == [
        ("2. 最後は保存ボタンで画面を閉じます。", "unverified")
    ]


def test_merge_takes_the_best_status_of_each_step_across_connections() -> None:
    first = _result([_step("s1", "値の登録", "unused"), _step("s2", "内容の確定", "no_evidence")])
    second = _result([_step("s1", "値の登録", "explained"), _step("s2", "内容の確定", "unused")])
    merged = merge_results([first, second])
    assert [(step["step_id"], step["status"]) for step in merged["guide_steps"]] == [
        ("s1", "explained"),
        ("s2", "unused"),
    ]
    # 説明を確かめた手順の「説明が無い」の finding は残さない（同じ指摘は 1 つにまとめる）。
    assert [(item["step_id"], item["message"]) for item in merged["findings"]] == [("s2", GAP)]


def _lookup_step(steps: list[dict[str, Any]]) -> RunStep:
    output: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_lookup_guides"])
    guide = output["guides"][0]
    guide.update(guide_id="g-1", decision="answer", steps=steps, branches=[])
    return RunStep(
        run_id="run-1",
        status=StepStatus.COMPLETED,
        tool_call=ToolCall(name="rag__rag_lookup_guides", arguments={"query": "q"}),
        tool_result=ToolResult(name="rag__rag_lookup_guides", success=True, output=output),
    )


def _retrieve_step(*items: tuple[str, int]) -> RunStep:
    evidence = []
    for chunk_id, page in items:
        item: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_retrieve_evidence"]["evidence"][0])
        item.update(document_id="doc-x", chunk_id=chunk_id, excerpt="本文")
        item["locator"] = {**item["locator"], "page_start": page, "page_end": page}
        evidence.append(item)
    return RunStep(
        run_id="run-1",
        status=StepStatus.COMPLETED,
        tool_call=ToolCall(name="rag__rag_retrieve_evidence", arguments={"query": "設定"}),
        tool_result=ToolResult(
            name="rag__rag_retrieve_evidence", success=True, output={"evidence": evidence}
        ),
    )


def test_step_state_marks_evidence_that_is_only_a_shared_chunk() -> None:
    """同じ頁の chunk を複数の手順が参照するとき、根拠の有無だけで説明を確かめたことにしない。"""

    def step(step_id: str, pages: tuple[int, int]) -> dict[str, Any]:
        return {
            "id": step_id,
            "title": f"手順 {step_id}",
            "retrieval_hints": [],
            "references": [
                {
                    "document_id": "doc-x",
                    "page_ranges": [{"page_start": pages[0], "page_end": pages[1]}],
                }
            ],
        }

    lookup = _lookup_step([step("s1", (3, 3)), step("s2", (3, 4))])
    states = guide_step_states([lookup, _retrieve_step(("c3", 3))])
    assert [(item["id"], item["evidence"], item["shared"]) for item in states] == [
        ("s1", True, True),
        ("s2", True, True),
    ]
    # 手順 2 だけの頁の chunk もあれば、手順 2 は共有だけではない。
    states = guide_step_states([lookup, _retrieve_step(("c3", 3), ("c4", 4))])
    assert [(item["id"], item["shared"]) for item in states] == [("s1", True), ("s2", False)]
