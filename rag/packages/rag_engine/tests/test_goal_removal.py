"""質問の目的（goal）の分類を取り除いた後の扱い（#3498）。

質問を 1 つの目的に分類せず、明示された要求の種類（手順・一覧・件数）と意味を尋ねる項目、根拠の種類で後段を
決める。どれの手がかりも無い質問に既定の目的を付けず、検索・回答・条件の読み取り・要求ごとの確認は続ける。
入力は合成の文で、外部の I/O は使わない。
"""

import json
import unittest
from unittest.mock import patch

import pytest
from test_answer_generation import _answer_output, _chunk_run_stub, _hybrid_result, _stored_child

from rag_engine.config import get_settings
from rag_engine.generation import grounded
from rag_engine.generation.answering import SIMPLE_RETRIEVAL_LABEL, STANDARD_ANSWER_FLOW_LABEL, answer_question_result
from rag_engine.generation.query_prompts import _prompt_task_contract
from rag_engine.retrieval.request_coverage import coverage_targets
from rag_engine.retrieval.task_contract import COUNT_REQUEST, LIST_REQUEST, STEPS_REQUEST, explicit_requests, task_contract

# どの要求の手がかりも無い合成の質問（要求は 2 件、文書の種類の手がかりを含む）。
PLAIN_QUESTION = "操作マニュアルの帳票Aについて。画面Bについて。"


@pytest.mark.parametrize(
    "question,requests,labels",
    [
        ("変更履歴を知りたい", set(), []),
        ("項目Aの件について。", set(), []),
        (PLAIN_QUESTION, set(), []),
        ("「休」の意味は？", set(), ["休"]),
        ("帳票Aを出力する方法を教えてください。", {STEPS_REQUEST}, []),
        ("項目Aを変更したい。", {STEPS_REQUEST}, []),
        ("対象は何件ありますか。", {COUNT_REQUEST}, []),
        ("対象者の一覧を出力したい。", {LIST_REQUEST}, []),
        ("「登録」列の意味と変更方法を教えてください", {STEPS_REQUEST}, ["登録"]),
        ("対象者の一覧を出力したい。何件ありますか。", {LIST_REQUEST, COUNT_REQUEST}, []),
    ],
    ids=["history", "topic-only", "two-requests", "definition", "method", "change", "count", "list", "def-and-steps", "list-and-count"],
)
def test_requests_are_explicit_and_have_no_default(question, requests, labels):
    contract = task_contract(question)
    assert "goal" not in contract
    assert explicit_requests(question) == requests
    assert [target["label"] for target in contract["definition_targets"]] == labels
    # 手順の観点は、手順の要求か一覧（件数ではない）の要求があるときだけ。
    assert ("procedure" in contract["required_aspects"]) is (STEPS_REQUEST in requests or requests == {LIST_REQUEST})


def _fake_tagger(question, span):
    return set(span.get("fake_tags", ()))


def test_pinned_evidence_follows_evidence_types_not_a_question_category():
    """定義の行は常に、操作の入口は手順・一覧・件数の要求があるときに必須根拠にする。質問の分類の既定値では選ばない。"""
    spans = [
        {"text": "項目A: 設定の状態を表す。", "evidence_id": "d", "page": 1, "fake_tags": ["definition"]},
        {"text": "画面Bで項目Aを選び、実行を押す。", "evidence_id": "o", "page": 2, "fake_tags": ["operation_entry"]},
        {"text": "補足の説明。", "evidence_id": "n", "page": 3, "fake_tags": ["completion"]},
    ]
    cases = [
        ("「項目A」の意味は？", [True, False, False]),
        ("項目Aを変更する方法は？", [True, True, False]),
        ("対象者の一覧を出力したい。", [True, True, False]),
        ("対象は何件ありますか。", [True, True, False]),
        # 要求の手がかりが無い質問は、定義の行だけ（操作の入口を既定で必須にしない）。
        ("項目Aについて。", [True, False, False]),
    ]
    for question, pinned in cases:
        with patch.object(grounded, "SPAN_TAGGERS", [_fake_tagger]):
            tagged = grounded.tag_spans(question, spans)
        assert [span["pinned"] for span in tagged] == pinned, question


def test_pinned_evidence_keeps_the_upper_bound():
    spans = [{"text": f"項目{n}: 説明。", "evidence_id": str(n), "page": n, "fake_tags": ["definition"]} for n in range(1, 8)]
    with patch.object(grounded, "SPAN_TAGGERS", [_fake_tagger]):
        tagged = grounded.tag_spans("項目について。", spans)
    assert sum(span["pinned"] for span in tagged) == grounded.MAX_PINNED


def test_llm_context_has_no_goal_and_keeps_requests():
    """生成・監査・検索文の生成へ渡す質問の情報に goal を入れず、要求単位は保つ。"""
    view = grounded._contract_view(PLAIN_QUESTION)
    assert "goal" not in view
    assert [unit["id"] for unit in view["request_units"]] == ["Q1", "Q2"]
    assert '"goal"' not in _prompt_task_contract("帳票Aを出力する方法を教えてください。")
    assert "goal" not in json.dumps(task_contract("対象は何件ありますか。"), ensure_ascii=False)
    for prompt in (grounded.GENERATE_SYSTEM_PROMPT, grounded.AUDIT_SYSTEM_PROMPT):
        assert "goal、" not in prompt
    assert "task_contract の current_request に答えていれば aligned" in grounded.AUDIT_SYSTEM_PROMPT


def test_per_request_coverage_targets_do_not_depend_on_a_category():
    targets = coverage_targets(PLAIN_QUESTION, lambda text: text.replace("。", " ").split())
    assert [target.id for target in targets] == ["Q1", "Q2"]


class GoalFreeAnswerFlowTests(unittest.TestCase):
    def setUp(self):
        from grounded_stub import stub_generation

        stub_generation(self)

    def _run(self, question):
        search_result = _hybrid_result(_stored_child("chunk-a", 1, "帳票Aは画面Bから出力します。"))
        with (
            patch("rag_engine.generation.answering.load_latest_or_source_chunk_run", return_value=_chunk_run_stub()),
            patch("rag_engine.generation.answering.check_adb_hybrid_search_ready"),
            patch("rag_engine.generation.answering.search_adb_hybrid_chunks", return_value=search_result) as search,
            patch(
                "rag_engine.generation.answering.parse_text_response",
                return_value=_answer_output(answer="帳票Aは画面Bから出力します。"),
            ),
        ):
            result = answer_question_result(
                question,
                "abcdef",
                ["docling"],
                get_settings(),
                query_strategy=SIMPLE_RETRIEVAL_LABEL,
                answer_flow=STANDARD_ANSWER_FLOW_LABEL,
            )
        return result, search

    def test_plain_question_continues_search_and_answer_without_a_purpose_label(self):
        result, search = self._run(PLAIN_QUESTION)
        # 検索は止めず、独立して読み取った条件（文書の種類の絞り込み）を渡す。
        search.assert_called()
        conditions = search.call_args.kwargs["inquiry_conditions"]
        self.assertTrue(conditions.metadata_filter.active)
        self.assertTrue(conditions.document_kinds)
        self.assertTrue(result.answer.startswith("帳票Aは画面Bから出力します。"))
        self.assertNotIn("goal", result.task_contract)
        display = result.question_display
        self.assertIn("結果: 答える要求は 2 件", display)
        self.assertIn("回答後、要求ごとに答えたかを確認します", display)
        self.assertIn("絞り込みに合う文書だけを検索します", display)
        self.assertNotIn("質問の目的", display)
        self.assertNotIn("規則・定義・可否", display)

    def test_desired_outcome_query_is_labeled_without_purpose_wording(self):
        """経緯と希望が混ざった質問の希望の部分の検索文は残し、出どころを「希望する結果」と示す。"""
        result, search = self._run("取消を試した。CSVを使って令和10年7月の予約を追加したい。")
        self.assertIn("CSVを使って令和10年7月の予約を追加したい", search.call_args.kwargs["retrieval_queries"])
        self.assertIn("[希望する結果] CSVを使って令和10年7月の予約を追加したい", result.question_display)
        self.assertNotIn("質問の目的", result.question_display)
