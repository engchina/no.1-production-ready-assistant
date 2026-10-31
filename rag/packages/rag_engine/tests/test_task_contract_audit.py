"""目的逸脱、監査漏れ、原文ID、修正回数と出典保存の反例を検証する。"""

import json
from unittest.mock import patch

import pytest
from judge_stub import judge
from test_evidence_first_answering import PARSE, evaluation_case

from rag_engine.evaluation.answer_eval import _evidence_fragments, evaluate_answer_payload
from rag_engine.generation.answering import (
    AnswerContext,
    AnswerRecord,
    _grade_crag_retrieval,
    _refine_crag_context,
    build_crag_answer_context,
)
from rag_engine.models.llm import CragRetrievalGradeOutput
from rag_engine.retrieval.evidence_selection import evidence_spans
from rag_engine.retrieval.task_contract import filter_queries, task_contract


def record(uid, text="入口を押すと編集画面が開く。保存ボタンで確定する。", source="guide.pdf", metadata=None):
    return AnswerRecord(
        id="local",
        chunk_id="local",
        chunk_uid=uid,
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=1,
        category="Chunk",
        text=text,
        source=source,
        metadata=dict(metadata or {}),
    )


@pytest.mark.parametrize(
    "question,bad,good",
    [
        ("調査対象者を確認したい", "調査加入人数の集計", "対象者一覧の取得方法"),
        ("項目の選択肢に該当するものがない", "項目が表示されない原因", "候補にない値の入力方法"),
        ("顧客からCSV提供の依頼があった", "地域別CSVの出力", "CSVの出力方法"),
        ("顧客から在庫数をCSVで受け取れるかの問い合わせがあった", "地域別在庫CSVの出力", "在庫CSVの出力方法"),
        ("申請の更新ができない", "ソフトウェアのバージョン更新", "申請の更新条件"),
    ],
)
def test_query_guard_preserves_goal_without_disabling_valid_expansion(question, bad, good):
    accepted, rejected = filter_queries(question, [question, bad, good])
    assert accepted == (question, good)
    assert rejected[0]["query"] == bad and rejected[0]["reason"]


def test_explicit_count_request_and_regional_filter_are_not_rejected():
    question = "地域別の取引先数を確認したい"
    assert filter_queries(question, ["地域別の件数集計"])[1] == ()
    assert task_contract("登録がないためではないかと思う。履歴を確認したい")["hypotheses"]


def test_duplicate_parse_is_consolidated_without_merging_different_versions():
    same_document = {"document_id": "doc-a"}
    a = record("run-a:1", metadata=same_document)
    b = record("run-b:1", metadata=same_document)
    changed = record("run-c:1", "取消ボタンで戻る。", metadata=same_document)
    spans = evidence_spans("保存操作", [a, b, changed])
    assert len(spans) == 2
    assert spans[0]["source_aliases"] == ["run-a:1", "run-b:1"]
    assert spans[0]["evidence_id"] != spans[1]["evidence_id"]
    assert a.text[spans[0]["start"] : spans[0]["end"]] == spans[0]["text"]
    assert len(evidence_spans("保存操作", [record("x", source=""), record("y", source="")])) == 2
    # 文書の識別子が無い根拠は、表示名が同じでも同じ文書と確かめられないのでまとめない（#2603）。
    assert len(evidence_spans("保存操作", [record("run-a:1"), record("run-b:1")])) == 2


def test_crag_keeps_support_when_one_candidate_is_selected():
    a, b = record("a"), record("b", "入力不可の場合は別の条件を確認する。")
    context = AnswerContext(records=[a, b], text=a.text + b.text)
    result = _refine_crag_context(context, ["a"], max_chars=2000)
    assert {r.chunk_uid for r in result.records} == {"a", "b"}


def test_crag_high_confidence_does_not_prove_required_coverage():
    r = record("a")
    grade = CragRetrievalGradeOutput(
        sufficient=True,
        confidence=0.99,
        candidate_verdicts=[dict(chunk_uid="a", relevant=True, reason="関連")],
        reason="関連",
        rewritten_query="",
        aspect_checks=[],
    )
    with patch("rag_engine.generation.answering.parse_text_response", return_value=grade):
        result = _grade_crag_retrieval(1, "登録方法", "登録方法", ["登録方法"], AnswerContext(records=[r], text=r.text), object())
    assert not result.generation_usable and result.required_aspect_coverage == "unknown"
    assert result.grade_execution == "partial"


def test_crag_does_not_resolve_ambiguous_local_ids():
    a, b = record("a", source="a.pdf"), record("b", source="b.pdf")
    checks = [
        dict(aspect=k, status="supported", source_ids=["local"], reason="関連") for k in task_contract("登録方法")["required_aspects"]
    ]
    grade = CragRetrievalGradeOutput(
        sufficient=True,
        confidence=0.99,
        candidate_verdicts=[dict(chunk_uid="local", relevant=True, reason="関連")],
        reason="関連",
        rewritten_query="",
        aspect_checks=checks,
    )
    with patch("rag_engine.generation.answering.parse_text_response", return_value=grade):
        result = _grade_crag_retrieval(1, "登録方法", "登録方法", ["登録方法"], AnswerContext(records=[a, b], text="text"), object())
    assert result.relevant_chunk_ids == () and not result.generation_usable
    assert result.required_aspect_coverage == "unknown"
    assert all(not c["source_ids"] for c in result.aspect_checks)


def test_identical_retry_stops_before_another_grade_call():
    a = record("a")
    context = AnswerContext(records=[a], text=a.text)
    grade = CragRetrievalGradeOutput(
        sufficient=False,
        confidence=0.1,
        reason="不足",
        rewritten_query="入力の前提",
        aspect_checks=[
            dict(aspect=k, status="missing", source_ids=[], reason="必要な操作の説明なし")
            for k in task_contract("登録方法")["required_aspects"]
        ],
    )
    with (
        patch("rag_engine.generation.answering.build_adb_hybrid_answer_context", return_value=context) as search,
        patch("rag_engine.generation.answering.parse_text_response", return_value=grade) as parse,
    ):
        result, _, attempts = build_crag_answer_context("登録方法", "", [], object())
    assert search.call_count == 2 and parse.call_count == 1
    assert attempts[-1].stop_reason == "no_new_evidence" and result.records


def test_uncited_passage_is_unassessed_without_the_model():
    data, scope, output = evaluation_case("data_confirmation")
    data["answer_text"] += "\n\n主档を変更する。"
    with patch(PARSE, side_effect=judge(output)):
        result = evaluate_answer_payload(data, object(), standard_scope=scope)
    assert result["status"] == "completed" and result["missing_audit_passage_ids"] == []
    uncited = next(claim for claim in result["claim_checks"] if claim["answer_quote"] == "主档を変更する。")
    assert (uncited["status"], uncited["reference_check"]) == ("unassessed", "uncited")


def test_evaluation_binds_source_text_by_stable_id():
    data, scope, output = evaluation_case("supported")
    span = list(_evidence_fragments(data["evidence_items"]))[0]
    output.claim_checks[0].evidence_id = span["evidence_id"]
    with patch(PARSE, side_effect=judge(output)):
        result = evaluate_answer_payload(data, object(), standard_scope=scope)
    assert result["claim_checks"][0]["evidence_quote"] == data["evidence_items"][0]["text"]
    assert result["claim_checks"][0]["status"] == "supported"


def test_missing_coverage_can_cite_refusal_without_getting_credit():
    from rag_engine.evaluation.answer_eval import CoverageCheck

    data, scope, output = evaluation_case("supported")
    output.coverage_checks = [CoverageCheck(requirement_index=1, status="missing", answer_quote="", answer_passage_id="A1")]
    with patch(PARSE, side_effect=judge(output)):
        result = evaluate_answer_payload(data, object(), standard_scope=scope)
    assert result["status"] == "completed" and result["requirement_coverage"] == 0
    assert result["coverage_checks"][0]["answer_passage_id"] == "A1"


def test_recipient_goal_reserves_list_operations_over_repeated_report_labels():
    from rag_engine.retrieval.evidence_selection import evidence_excerpt

    question = "四半期集計表の会員来店数，店舗・曜日別の対象者を確認したい。"
    text = (
        "画面/メニュー: " + ("会員来店数 店舗 曜日 四半期集計表 / " * 12) + "\n"
        "操作: 会員別の一覧表にチェックし条件を選択、CSV出力を選んで実行ボタンを押す。\n"
        "検索語: " + ("四半期集計表 会員来店数 店舗 曜日 対象者 / " * 12)
    )
    selected = evidence_excerpt(question, text, 180)
    assert "会員別の一覧表にチェックし条件を選択" in selected
    assert "CSV出力を選んで実行ボタンを押す。" in selected


def test_extra_coverage_item_is_retried_with_explicit_fixed_ids_and_feedback():
    from rag_engine.evaluation.answer_eval import CoverageJudgement, CoverageOutput

    data, scope, valid = evaluation_case("supported")
    fake = judge(valid)
    calls = []

    def parse(system, prompt, settings, schema, **kwargs):
        if schema is not CoverageOutput:
            return fake(system, prompt, settings, schema, **kwargs)
        inputs = json.loads(prompt)
        calls.append(inputs)
        assert [item["requirement_index"] for item in inputs["requirements"]] == [1]
        if len(calls) == 1:
            extra = CoverageJudgement(requirement_index=2, reason="理由", status="missing")
            return CoverageOutput(coverage_checks=[*fake(system, prompt, settings, schema).coverage_checks, extra])
        assert "（1）" in inputs["validation_feedback"]
        return fake(system, prompt, settings, schema)

    with patch(PARSE, side_effect=parse):
        result = evaluate_answer_payload(data, object(), standard_scope=scope)
    assert result["status"] == "completed" and len(result["coverage_checks"]) == 1
    assert len(calls) == 2 and len(result["standard_answer_scope"]["requirements"]) == 1


def test_requester_terms_use_generic_language_resources():
    from rag_engine.retrieval import task_contract as module

    assert module.task_contract("顧客から一覧のCSV提供の依頼があった")["requester_mentions"]
    assert not module.task_contract("組織Aから一覧のCSV提供の依頼があった")["requester_mentions"]
