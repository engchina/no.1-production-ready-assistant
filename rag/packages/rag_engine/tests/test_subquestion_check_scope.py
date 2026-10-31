"""小問の語の有無から、支持や不足を自動判定しない。"""

import pytest
from test_grounded_answer import context, draft, item, record

from rag_engine.generation import grounded
from rag_engine.generation.answering import _grounded_spans

QUESTION = "①取込画面で「E-SAMPLE」のエラーが出ます。対処方法は？②項目Aの入力方法は？③結果の印刷方法は？"
FIRST = "取込画面で接続を確認します。"
SECOND = "入力画面で項目Aに値を入力し、保存ボタンを押します。"
THIRD = "結果一覧画面で印刷ボタンを押します。"


def verify(question=QUESTION, second_id="Q2"):
    ctx = context(record(0, FIRST, "取込画面"), record(1, SECOND, "入力画面"), record(2, THIRD, "結果一覧画面"))
    spans = _grounded_spans(question, ctx, (), None)
    labels = {span["text"]: span["evidence_id"] for span in spans}
    generated = draft(
        item(labels[FIRST], FIRST, FIRST, request_id="Q1"),
        item(labels[SECOND], SECOND, SECOND, request_id=second_id),
        item(labels[THIRD], THIRD, THIRD, request_id="Q3"),
    )
    checked, dropped = grounded.verify(question, grounded.GroundedDraft.model_validate(generated), spans)
    assert not dropped
    return checked


def test_error_from_first_subquestion_does_not_warn_other_operations():
    checked = verify()
    assert not any("E-SAMPLE" in reason for reason in checked[0].reasons)
    for entry in checked[1:3]:
        assert not any("E-SAMPLE" in reason for reason in entry.reasons)


def test_child_request_uses_parent_subquestion():
    checked = verify(second_id="Q2.R1")
    assert not any("E-SAMPLE" in reason for reason in checked[1].reasons)


def test_unknown_request_id_does_not_create_word_checks():
    checked = verify(second_id="unknown")
    assert not any("E-SAMPLE" in reason for reason in checked[1].reasons)


def test_missing_error_word_does_not_invent_a_gap():
    checked = verify()
    gaps = [entry.item for entry in checked if entry.item.kind == "gap" and "E-SAMPLE" in entry.item.text]
    assert not gaps
    assert all(entry.request_id == "Q1" for entry in gaps)
    assert all("項目A" not in entry.text for entry in gaps)


def test_single_question_does_not_turn_absent_error_words_into_a_verdict():
    checked = verify(question="取込画面で「E-SAMPLE」のエラーが出ます。対処方法は？")
    assert not any("E-SAMPLE" in reason for reason in checked[1].reasons)


@pytest.mark.parametrize("suffix", ["", ".R1"])
def test_subquestion_retains_shared_preface_without_other_question(suffix):
    question = "管理者権限で操作しています。" + QUESTION
    unit = next(unit for unit in grounded.task_contract(question)["request_units"] if unit.get("subquestion") == "②")
    scoped = grounded.item_question(question, unit["id"] + suffix)
    assert "管理者権限" in scoped
    assert "項目A" in scoped
    assert "E-SAMPLE" not in scoped
