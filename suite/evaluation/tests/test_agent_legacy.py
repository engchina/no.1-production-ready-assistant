"""Agent の旧形式の回答の扱い（#1305・#1456・#2738。#3240 で RAG から移した）。

回答の節の見出しは公開の契約（`platform/contracts/answer-passages/unverified-section.json`）と同じで、
#2738 より前の Agent の回答の節（「資料で確認できず除いた内容」）も本文から除く。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pr_suite_evaluation.targets.agent_legacy import (
    AGENT_SECTION_HEADINGS,
    LEGACY_UNVERIFIED_HEADING,
    answer_body,
    asks_question,
    infer_outcome,
)

REPO = Path(__file__).resolve().parents[3]
CONTRACT: dict[str, Any] = json.loads(
    (REPO / "platform/contracts/answer-passages/unverified-section.json").read_text("utf-8")
)
SECTIONS: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parent / "fixtures/agent_baseline/answer_sections.json").read_text(
        "utf-8"
    )
)


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def test_section_headings_are_the_public_contract_headings() -> None:
    assert list(AGENT_SECTION_HEADINGS) == CONTRACT["headings"]
    assert LEGACY_UNVERIFIED_HEADING not in CONTRACT["headings"]


@pytest.mark.parametrize("case", CONTRACT["examples"], ids=lambda case: str(case["id"]))
def test_answer_body_excludes_the_sections_of_the_contract(case: dict[str, str]) -> None:
    assert _lines(answer_body(case["answer"])) == _lines(case["body"])


@pytest.mark.parametrize("case", SECTIONS["sections"], ids=lambda case: str(case["id"]))
def test_answer_body_of_saved_agent_answers_matches_the_baseline(case: dict[str, Any]) -> None:
    """保存した Agent の回答（#2738 の前と後）の節を除いた本文が基線と一致する。"""
    assert _lines(answer_body(case["answer"])) == case["body_lines"]


def test_question_in_a_system_section_is_not_the_answer_asking() -> None:
    answer = "設定は画面で変えます。\n\n**確認が必要な点**\n\n- 「対象は全員ですか？」"
    assert not asks_question(answer)
    assert asks_question("確認させてください。\n対象は全員ですか？")
    assert infer_outcome(answer, has_citations=True) == "answered"
    assert infer_outcome("", has_citations=True) == "insufficient_evidence"
