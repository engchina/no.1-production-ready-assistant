"""希望する結果の検索を試行経緯から分離し、原要求は捨てない。"""

import pytest

from rag_engine.generation.answering import QueryExpansionResult
from rag_engine.retrieval.evidence_selection import evidence_relevance
from rag_engine.retrieval.task_contract import STEPS_REQUEST, explicit_requests, goal_retrieval_queries, task_contract


def test_trial_and_hypothesis_do_not_replace_the_goal_or_delete_question():
    question = "予約取消を試したが、元の予約が表示されていた。予約期間を延長したい。削除して作り直すしかないと思うが削除方法はあるか。"
    contract = task_contract(question)
    parts = contract["intent_parts"]
    assert parts["desired_outcomes"] == ["予約期間を延長したい"]
    assert parts["tried_actions"] == ["予約取消を試した"]
    assert parts["observed_states"] == ["元の予約が表示されていた"]
    assert len(parts["hypothesized_means"]) == 1
    assert "削除方法" in contract["request_units"][-1]["text"]
    queries = QueryExpansionResult(question, "simple", "simple").retrieval_queries
    assert queries == (question, "予約期間を延長したい")
    assert evidence_relevance(question, "予約期間を延長するには終了日を入力して更新します。") > evidence_relevance(
        question, "予約取消の操作で元の予約を削除して作り直す方法。"
    )


@pytest.mark.parametrize(
    "question",
    [
        "予約を削除したい。",
        "予約を削除したいのではないかと思う。",
        "追加したかったが今は不要。削除を試した。",
        "取消した場合、登録済みの履歴は残るか。",
    ],
)
def test_no_goal_query_is_invented_without_a_separate_explicit_goal(question):
    assert goal_retrieval_queries(question) == ()


def test_explicit_method_constraints_and_values_remain_verbatim():
    question = "取消を試した。CSVを使って令和10年7月の予約を追加したい。"
    assert goal_retrieval_queries(question) == ("CSVを使って令和10年7月の予約を追加したい",)
    assert task_contract(question)["original_question"] == question


def test_multiple_goals_are_retained_in_contract_with_bounded_extra_queries():
    question = "削除を試した。予約期間を延長したい。参加者を追加したい。人数を変更したい。"
    contract = task_contract(question)
    assert len(contract["intent_parts"]["desired_outcomes"]) == 3
    assert len(goal_retrieval_queries(question)) == 2
    assert len(contract["request_units"]) == 4


def test_registered_state_does_not_imply_a_user_trial():
    parts = task_contract("予約は登録済み。予約期間を延長したい。")["intent_parts"]
    assert parts["tried_actions"] == []
    assert parts["observed_states"] == ["予約は登録済み"]


@pytest.mark.parametrize("verb", ["追加", "削除", "延長", "取消"])
def test_explicit_operation_wish_is_not_classified_as_a_definition(verb):
    contract = task_contract(f"予約を{verb}したい。")
    assert STEPS_REQUEST in explicit_requests(f"予約を{verb}したい。")
    assert verb in contract["requested_actions"]
    assert STEPS_REQUEST not in explicit_requests(f"{verb}料金の意味は？")
    assert [t["label"] for t in task_contract(f"{verb}料金の意味は？")["definition_targets"]]


def test_goal_action_precedes_trial_action_without_losing_other_requests():
    contract = task_contract("登録を試した。予約日を追加したかったが存在しない。削除するしかないと思うが方法は？")
    assert contract["requested_actions"][0] == "追加"
    assert {"追加", "削除", "登録"} <= set(contract["requested_actions"])


def test_embedded_goal_has_independent_coverage_without_rewriting_parent():
    q = "取消を試した。会場は変更できたが、予約日を追加したかったが一覧に存在しない。削除するしかないと思うが方法は？"
    units = task_contract(q)["request_units"]
    child = next(u for u in units if u.get("kind") == "desired_outcome")
    assert child == dict(id="Q2.G1", text="予約日を追加したかった", kind="desired_outcome", request_id="Q2")
    assert units[1]["text"] == "会場は変更できたが、予約日を追加したかったが一覧に存在しない。"
    assert "削除" in units[2]["text"]
    assert not any(u.get("kind") == "desired_outcome" for u in task_contract("取消を試した。予約日を追加したい。")["request_units"])


def test_state_suffix_is_not_a_tried_or_requested_action():
    # 「取消済み伝票」はデータの状態。試行操作にも要求操作にもしない。
    question = (
        "地区別売上集計表を出力したところ、前月の取消済み伝票が集計に含まれてしまいます。"
        "除外する設定はありますか。また、CSV で出力する方法も教えてください。"
    )
    contract = task_contract(question)
    assert contract["intent_parts"]["tried_actions"] == []
    assert contract["requested_actions"] == ["出力"]
    assert task_contract("予約は登録済み。予約期間を延長したい。")["requested_actions"] == ["延長"]


def test_state_report_at_clause_end_remains_a_tried_action():
    parts = task_contract("該当の伝票は取消済みです。それでも集計に残るのはなぜですか。")["intent_parts"]
    assert parts["tried_actions"] == ["該当の伝票は取消済みです"]
