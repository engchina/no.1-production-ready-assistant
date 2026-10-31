"""根拠の表の対応と取り違えた段落を、表の正しい値に直す（研究と改訂。#3005）。根拠・文は合成、LLM はスタブ。"""

import json
from typing import Any
from unittest.mock import patch

import pytest

from rag_engine.evaluation.answer_validation import (
    QUESTION_ID,
    SUPPORT_TABLE_CORRECTED,
    ClaimAuditOutput,
    validate_answer_claims,
)
from rag_engine.generation import grounded
from rag_engine.generation.table_revision import REVISION_SYSTEM_PROMPT, TableRevision, revise_table_passage
from rag_engine.models.llm import GroundedCitation, GroundedDraft, GroundedItem

TABLE = (
    "<table><tr><th>区分</th><th>選択肢の値</th></tr>"
    "<tr><td>区分Ⅰ</td><td>01</td></tr><tr><td>区分Ⅱ</td><td>02</td></tr><tr><td>区分Ⅲ</td><td>03</td></tr></table>"
)
REVISED = "区分Ⅱでは選択肢の値 02 を選びます。"
WRONG = "区分Ⅱでは選択肢の値 01 を選びます。"


def _reviser(text: str | Exception, seen: list | None = None):
    def parse(system: str, user: str, schema: Any) -> TableRevision:
        if seen is not None:
            seen.append((system, json.loads(user), schema))
        if isinstance(text, Exception):
            raise text
        return TableRevision(revised_text=text)

    return parse


def test_wrong_value_is_replaced_by_the_value_of_the_table() -> None:
    seen: list = []
    revised = revise_table_passage(WRONG, [TABLE], _reviser(REVISED, seen))
    assert revised is not None and revised.text == REVISED
    assert "『区分Ⅱ』の値は『02』" in revised.note
    [(system, inputs, schema)] = seen
    assert system == REVISION_SYSTEM_PROMPT and schema is TableRevision
    assert system.startswith("## Step 1:")
    assert "## Step 4:" in system
    # LLM には段落と表の正しい対応だけを渡す。
    assert inputs == {
        "passage": WRONG,
        "corrections": [{"wrong": "01", "correct": "02"}],
        "notes": ["『区分Ⅱ』の値は『02』（『01』は別の行の値）"],
    }


def test_value_of_another_column_is_replaced() -> None:
    table = "| 項目 | 改定前 | 改定後 |\n|---|---|---|\n| 申請の期限 | 30日以内 | 60日以内 |\n| 保存期間 | 3年 | 5年 |"
    revised = revise_table_passage("改定後の申請の期限は30日以内です。", [table], _reviser("改定後の申請の期限は60日以内です。"))
    assert revised is not None and revised.text == "改定後の申請の期限は60日以内です。"


@pytest.mark.parametrize(
    "output",
    [
        WRONG,  # 直していない
        "区分Ⅱでは選択肢の値 03 を選びます。",  # 別の行の値に変えた
        "区分Ⅱを選ぶときは、画面の右上のボタンから選択肢の一覧を開き、表の 2 行目の値である 02 を選択します。",  # 書き換えた
        "区分Ⅱの値は確認してください。",  # 正しい値を述べていない
        RuntimeError("synthetic failure"),
    ],
    ids=["unchanged", "other-row", "rewritten", "missing-value", "model-failure"],
)
def test_revisions_that_fail_the_check_are_not_used(output: Any) -> None:
    assert revise_table_passage(WRONG, [TABLE], _reviser(output)) is None


def test_passages_without_a_mismatch_are_not_sent() -> None:
    seen: list = []
    assert revise_table_passage(REVISED, [TABLE], _reviser(REVISED, seen)) is None
    assert seen == []


def test_generated_item_is_corrected_and_kept_with_a_mark() -> None:
    """回答の生成で、表の対応を取り違えた説明を正しい値に直して本文に残し、直した印と前後を出す。"""
    question = "区分Ⅱで選ぶ選択肢の値は？"
    spans = grounded.tag_spans(
        question, [{"evidence_id": "E1", "source_id": "c1", "source": "m.pdf", "page": 1, "text": TABLE, "section_path": []}]
    )
    item = GroundedItem(kind="rule", text=WRONG, citations=[GroundedCitation(evidence_id="E1", quote="区分Ⅱ 02")])
    entries, _ = grounded.verify(question, GroundedDraft(summary="", confidence="high", items=[item]), spans)
    entry = next(entry for entry in entries if entry.item.text == WRONG)
    assert entry.removed and entry.table_conflict
    assert grounded.revise_table_conflicts(entries, _reviser(REVISED)) == 1
    assert entry.published and not entry.removed and entry.item.text == REVISED
    assert [mark["code"] for mark in entry.marks] == ["table_corrected"]
    assert entry.marks[0]["detail"].startswith(f"「{WRONG}」を根拠の表の値に直しました")
    # 直せなければ外したまま。
    entries, _ = grounded.verify(question, GroundedDraft(summary="", confidence="high", items=[item]), spans)
    assert grounded.revise_table_conflicts(entries, _reviser(RuntimeError("x"))) == 0
    assert next(entry for entry in entries if entry.item.text == WRONG).removed


def test_final_validation_returns_the_corrected_passage() -> None:
    """最終の検証は、表の対応を取り違えた段落の直した文（revised_text）を返す。status は contradicted のまま。"""

    def parse(system, inputs, settings, schema, provider_id=None):
        if schema is TableRevision:
            return TableRevision(revised_text=REVISED)
        payload = json.loads(inputs)
        evidence = next(item["evidence_id"] for item in payload["evidence_items"] if item["id"] != QUESTION_ID)
        return ClaimAuditOutput.model_validate(
            {
                "claim_checks": [
                    {
                        "answer_quote": "段落",
                        "answer_passage_id": passage["id"],
                        "evidence_id": evidence,
                        "evidence_quote": "",
                        "applicability": "matched",
                        "status": "supported",
                        "source_id": "",
                        "reason": "資料に記載されている",
                    }
                    for passage in payload["answer_passages"]
                ]
            }
        )

    items = [{"id": "c1", "source": "manual.pdf", "page_start": 1, "page_end": 1, "text": TABLE}]
    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims("質問", WRONG, items, settings=None)
    [claim] = result["claim_checks"]
    assert (claim["status"], claim["support_check"], claim["revised_text"]) == ("contradicted", SUPPORT_TABLE_CORRECTED, REVISED)
    assert claim["reason"].startswith("根拠の表の値に直しました（『区分Ⅱ』の値は『02』")
