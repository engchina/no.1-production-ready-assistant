"""定義の派生要求を広げず、親 ID の説明を対象ごとに対応させる。"""

import pytest

from rag_engine.generation import grounded
from rag_engine.models.llm import GroundedItem
from rag_engine.retrieval.task_contract import definition_targets, request_units


def checked(text, *, request_id="Q1", kind="rule", removed=False):
    return grounded.CheckedItem(item=GroundedItem(request_id=request_id, kind=kind, text=text), span={"text": text}, removed=removed)


def statuses(question, entries):
    return {r.request_id: r.status for r in grounded.deterministic_request_audit(request_units(question), entries).request_reviews}


def test_definition_child_does_not_add_unrequested_display_conditions():
    child = next(u for u in request_units("検証項目Aの意味は？") if u["id"] == "Q1.D1")
    assert "全表示条件" not in child["text"]
    assert child["request_id"] == "Q1" and child["label"] == "検証項目A"


@pytest.mark.parametrize(("first", "second"), [("①", "②"), ("(1)", "(2)"), ("1)", "2)")])
def test_subquestion_markers_are_not_part_of_definition_labels(first, second):
    targets = definition_targets(f"{first}検証項目Aの意味は？{second}検証項目Bの意味は？")
    assert [t["label"] for t in targets] == ["検証項目A", "検証項目B"]


def test_parent_answer_satisfies_its_single_definition_child():
    assert statuses("検証項目Aの意味は？", [checked("検証項目Aは入力を識別します。")]) == {"Q1": "addressed", "Q1.D1": "addressed"}


def test_parent_answer_does_not_cover_an_unanswered_other_definition():
    result = statuses("「検証項目A」「検証項目B」の意味は？", [checked("検証項目Aは入力を識別します。")])
    assert result["Q1.D1"] == "addressed" and result["Q1.D2"] == "missing"


@pytest.mark.parametrize(
    "entry",
    [
        checked("検証項目Bは状態を示します。"),
        checked("検証項目Aは不明です。", kind="gap"),
        checked("検証項目Aは入力を識別します。", removed=True),
        checked("検証項目Aは入力を識別します。", request_id="Q2"),
    ],
    ids=["other-label", "gap", "removed", "other-request"],
)
def test_unanswered_definition_is_not_certified(entry):
    assert statuses("検証項目Aの意味は？", [entry])["Q1.D1"] == "missing"


def test_explicit_marker_is_not_covered_by_a_bare_field_definition():
    question = "「検証項目A」の＊の意味は？"
    assert statuses(question, [checked("検証項目Aは入力を識別します。")])["Q1.D1"] == "missing"
    assert statuses(question, [checked("検証項目Aの＊は入力の未確定を示します。")])["Q1.D1"] == "addressed"


def test_original_constraints_remain_available_without_expanding_the_child():
    question = "「検証項目A」の意味だけ説明してください。表示条件の説明は不要です。"
    child = next(unit for unit in request_units(question) if unit["id"] == "Q1.D1")
    assert child["original_question"] == "「検証項目A」の意味だけ説明してください。"
    assert "表示条件" not in child["text"]
    assert any("表示条件の説明は不要" in unit["text"] for unit in request_units(question))


def test_unconfirmed_parent_explanation_remains_partial():
    entry = checked("検証項目Aは入力を識別します。")
    entry.span = None
    entry.mark("unsupported", "合成の未確認な説明")
    assert statuses("検証項目Aの意味は？", [entry])["Q1.D1"] == "partial"
