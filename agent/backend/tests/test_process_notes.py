"""作業の経過だけを述べる文（業務ガイドが見つからなかった。#1452）の決定論テスト。

#1362 の deepseek-flash の評価で、回答の「補足：この内容に当たる公開の業務ガイドは見つかりません
でした（`rag_lookup_guides` の結果は0件）。」が、業務ガイドの検索の結果が検証の根拠に無いため
`unsupported` で外れ、資料の答えは正しいのに回答が conditional になっていた。事例は RAG と共有する
（platform/contracts/answer-passages/process-notes.json）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.features.agent.answer_outcome import answer_outcome
from app.features.agent.answer_passages import (
    KIND_PROCESS,
    is_guide_not_found,
    non_claim_passages,
    passage_kind,
)
from app.features.agent.answer_validation import (
    STATUS_COMPLETED,
    combine_validations,
    validation_content,
)
from app.features.agent.runtime import RunStep, StepStatus
from app.features.agent.tools import ToolCall, ToolResult

CASES_PATH = "platform/contracts/answer-passages/process-notes.json"
CASES: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[3] / CASES_PATH).read_text("utf-8")
)
EVIDENCE = [{"document_id": "doc-1", "chunk_id": "chunk-1"}]


@pytest.mark.parametrize("case", CASES["process_passages"], ids=lambda case: str(case["id"]))
def test_guide_not_found_passages_are_not_claims(case: dict[str, str]) -> None:
    text = case["passage"]
    assert is_guide_not_found(text)
    assert passage_kind(text) == KIND_PROCESS
    assert non_claim_passages(text).get(text) == KIND_PROCESS


@pytest.mark.parametrize("case", CASES["claim_passages"], ids=lambda case: str(case["id"]))
def test_guide_passages_with_content_stay_claims(case: dict[str, str]) -> None:
    text = case["passage"]
    assert not is_guide_not_found(text)
    assert text not in non_claim_passages(text)


def test_guide_not_found_passage_is_kept_and_does_not_make_the_answer_conditional() -> None:
    """`da-regional-report-export`（`run_d24bd72a9a854a798d22624bcaa3c5f2`）の形: 資料の答えは
    裏付けられ、業務ガイドが見つからなかった段落を検証が unsupported にした。段落は外さず、回答の
    対応は rag_search の answered のまま（業務ガイドが無いことは回答の不足ではない）。"""
    fact = "地域別集計表は「集計」画面の「CSV 出力」から出力します。"
    note = CASES["process_passages"][0]["passage"]
    answer = f"{fact}\n\n{note}"
    result = {
        "valid": False,
        "status": "completed",
        "counts": {"supported": 1, "unsupported": 1},
        "claims": [
            {"answer_quote": fact, "status": "supported", "reason": "根拠に記載。"},
            {"answer_quote": note, "status": "unsupported", "reason": "根拠に無い。"},
        ],
        "missing_evidence": [],
        "stale_evidence": [],
        "evidence_truncated": False,
    }
    content, published = combine_validations(
        answer, [validation_content(STATUS_COMPLETED, result=result, evidence=EVIDENCE)]
    )
    assert published == answer
    assert (content["passage_marks"], content["removed_passages"]) == ([], [])

    name = "rag__rag_search"
    search = RunStep(
        run_id="run-1452",
        status=StepStatus.COMPLETED,
        tool_call=ToolCall(name=name, arguments={"query": "地域別集計表 出力"}),
        tool_result=ToolResult(
            name=name, success=True, output={"outcome": "answered", "evidence": EVIDENCE}
        ),
    )
    outcome = answer_outcome(answer, steps=[search], validation=content)
    assert (outcome["value"], outcome["basis"]) == ("answered", "rag_search")
    assert outcome["signals"] == []
