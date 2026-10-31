"""回答評価の比較範囲・網羅・段落ごとの主張の判定・保存の失敗の分離を検証する。"""

import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from judge_stub import calls_of
from judge_stub import judge as stub_judge
from pydantic import ValidationError

from rag_engine.adapters.oci import InvalidStructuredOutput
from rag_engine.config import get_settings
from rag_engine.evaluation.answer_eval import (
    CLAIM_PROMPT,
    CORRECTNESS_PROMPT,
    COVERAGE_PROMPT,
    MAX_EVALUATION_INPUT_BYTES,
    QUESTION_EVIDENCE_ID,
    QUESTION_SOURCE_ID,
    REFERENCE_UNCITED,
    RUBRIC_VERSION,
    SCOPE_PROMPT,
    AnswerCorrectnessOutput,
    AnswerEvaluationOutput,
    ClaimCheck,
    ClaimJudgement,
    CoverageCheck,
    CoverageJudgement,
    CoverageOutput,
    EvaluationContractError,
    StandardAnswerScope,
    StandardRequirement,
    _prepare_standard_scope,
    evaluate_answer_payload,
    grounded_quote,
    standard_answer_units,
    standard_scope_cache,
)
from rag_engine.generation.answer_labels import answer_labels
from rag_engine.generation.answering import AnswerQuestionResult, AnswerRecord, answer_evidence_items, parse_answer_response
from rag_engine.knowledge.answers import save_answer_result

PARSE = "rag_engine.evaluation.answer_eval.parse_text_response"


@pytest.fixture(autouse=True)
def fixed_scope():
    """既存の評価・保存テストでは、範囲抽出I/Oを独立した固定入力に置き換える。"""
    with patch("rag_engine.evaluation.answer_eval._prepare_standard_scope", return_value=scope_output()) as mocked:
        yield mocked


def scope_output():
    return StandardAnswerScope(
        requirements=[
            StandardRequirement(
                standard_answer_unit_ids=["S1"],
                standard_answer_quote="ログを確認して原因を特定する。",
                requirement="文書にあるログ確認手順",
            )
        ],
        excluded_case_data=["実際の原因"],
    )


def output():
    """ログ確認手順を説明済みと照合し、確認案内を監査した固定の評価結果（判定のモデルの代わりの応答の元）。"""
    return AnswerEvaluationOutput(
        claim_checks=[
            ClaimCheck(answer_quote="回答", status="data_confirmation", source_id="", evidence_quote="", reason="未確認データの確認案内")
        ],
        coverage_checks=[CoverageCheck(requirement_index=1, status="addressed", answer_quote="回答")],
    )


def payload():
    return {
        "question": "今回の取込失敗の原因は？",
        "standard_answer": "ログを確認して原因を特定する。",
        "answer_text": "回答",
        "evidence_items": [{"id": "c1", "text": "手順本文" * 150, "text_preview": "短縮表示", "source": "manual.pdf"}],
    }


def cited_payload():
    """出典の行（「根拠：文書名 p.N」）と回答の引用の記録（cited_evidence）を持つ回答。"""
    return payload() | dict(
        answer_text="ログを確認して原因を特定します。\n根拠：manual.pdf p.3",
        evidence_items=[
            dict(id="doc:p3", source="manual.pdf", page_start=3, page_end=3, text="障害の原因はログを確認して特定する。"),
            dict(id="doc:p9", source="manual.pdf", page_start=9, page_end=9, text="無関係の頁の本文。"),
        ],
        cited_evidence=[dict(evidence_id="doc:p3", source="manual.pdf", page="3")],
    )


def judge(evaluated=None, **options):
    """判定のモデルの代わり（``judge_stub.judge``）。既定は ``output()`` の判定と ``scope_output()`` の比較範囲。"""
    return stub_judge(evaluated or output(), scope=scope_output(), **options)


def broken_output():
    """adapter が構造化出力として読めなかった応答（JSON が閉じない・必須項目の欠落）。"""
    return InvalidStructuredOutput("構造化出力の JSON が閉じていません。")


def test_completed_returns_checks_without_scores_and_judges_claims_with_cited_evidence():
    calls = []
    data = cited_payload() | dict(external_data_required=True, external_data_items=["当該実行のログ"])
    supported = output().model_copy(
        update=dict(
            claim_checks=[ClaimCheck(answer_quote="ログを確認", status="supported", source_id="", evidence_quote="", reason="一致")]
        )
    )
    with patch(PARSE, side_effect=judge(supported, calls=calls)) as parse:
        result = evaluate_answer_payload(data, object(), provider_id="chosen")
    expected_systems = {ClaimJudgement: CLAIM_PROMPT, CoverageOutput: COVERAGE_PROMPT, AnswerCorrectnessOutput: CORRECTNESS_PROMPT}
    for call in parse.call_args_list:
        assert call.args[0] == expected_systems[call.args[3]]
        assert call.args[0].startswith("## Step 1:")
    assert result["status"] == "completed" and result["rubric_version"] == RUBRIC_VERSION == 14
    assert result["requirement_coverage"] == 1.0
    # 点数・合否は付けない。評価の基準の閾値による合否は backend が計算する(#680)。
    assert not {"scores", "total_score", "passed", "max_score", "pass_threshold"} & set(result)
    assert result["external_data_items"] == ["当該実行のログ"]
    assert all(call.kwargs["provider_id"] == "chosen" for call in parse.call_args_list)
    # 主張の判定には、その段落の出典の根拠（p.3）と質問の文だけを渡す。batch の引き継ぎは無い。
    (claim_inputs,) = calls_of(calls, ClaimJudgement)
    assert set(claim_inputs) == {"passage", "context", "evidence_items"}
    assert [item["id"] for item in claim_inputs["evidence_items"]] == [QUESTION_SOURCE_ID, "doc:p3"]
    # 網羅の判定には比較項目と回答の段落だけを渡す（根拠・標準回答の全文は渡さない）。
    (coverage_inputs,) = calls_of(calls, CoverageOutput)
    assert set(coverage_inputs) == {"question", "answer_passages", "requirements"}
    assert coverage_inputs["requirements"] == [
        dict(requirement_index=1, requirement="文書にあるログ確認手順", standard_answer_quote="ログを確認して原因を特定する。")
    ]
    claim = result["claim_checks"][0]
    assert (claim["status"], claim["source_id"], claim["reference_check"]) == ("supported", "doc:p3", "resolved")


def test_removed_axis_fields_are_rejected():
    # 旧 4 軸(#680 で削除)を返すモデルの出力は schema 違反にして再試行させる。
    with pytest.raises(ValidationError):
        AnswerEvaluationOutput(**(output().model_dump() | {"accuracy": {"score": 5, "reason": "理由"}}))


def test_no_standard_answer_skips_network_and_keeps_external_status():
    with patch("rag_engine.evaluation.answer_eval.parse_text_response") as parse:
        result = evaluate_answer_payload(
            {"standard_answer": "  ", "external_data_required": True, "external_data_items": ["ログ"]}, object()
        )
    parse.assert_not_called()
    assert result["status"] == "no_standard_answer"
    assert result["external_data_items"] == ["ログ"]


def test_error_does_not_prevent_answer_persistence_or_leak_details():
    with TemporaryDirectory() as tmp:
        settings = replace(get_settings(), output_dir=Path(tmp))
        result = AnswerQuestionResult(
            answer="回答",
            answer_text="回答",
            original_question="質問",
            question_display="質問",
            selected_strategy="simple",
            effective_strategy="simple",
            primary_source_run_id="run1",
        )
        with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=RuntimeError("private connection details")):
            saved = save_answer_result(result, settings, standard_answer="標準")
        stored = json.loads((Path(tmp) / "run1" / "answers" / f"{saved.answer_id}.json").read_text())
        assert stored["answer_text"] == "回答"
        assert stored["evaluation"]["status"] == "error"
        assert "private connection details" not in json.dumps(stored)


def test_external_output_and_legacy_compatibility():
    response = parse_answer_response(json.dumps({"answer": "回答", "external_data_required": False, "external_data_items": ["実行ログ"]}))
    assert response.external_data_required is True
    assert response.needs_human_review is True
    assert parse_answer_response('{"answer":"旧回答"}').external_data_required is None


def test_evidence_body_is_not_truncated_for_evaluation():
    record = AnswerRecord(
        id="c1",
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=1,
        category="text",
        text="metadata付き本文",
        body_text="根拠全文" * 150,
    )
    assert answer_evidence_items([record])[0]["text"] == "根拠全文" * 150


def test_successful_evaluation_is_saved_and_keeps_generation_external_state():
    with TemporaryDirectory() as tmp:
        settings = replace(get_settings(), output_dir=Path(tmp))
        result = AnswerQuestionResult(
            answer="回答",
            answer_text="回答",
            original_question="質問",
            question_display="質問",
            selected_strategy="simple",
            effective_strategy="simple",
            primary_source_run_id="run1",
            external_data_required=True,
            external_data_items=("実行ログ",),
        )
        with patch(PARSE, side_effect=judge()) as parse:
            saved = save_answer_result(result, settings, standard_answer="標準", evaluation_provider="chosen")
        stored = json.loads((Path(tmp) / "run1" / "answers" / f"{saved.answer_id}.json").read_text())
        assert stored["evaluation"]["status"] == "completed"
        assert stored["evaluation"]["requirement_coverage"] == 1.0
        assert stored["external_data_items"] == stored["evaluation"]["external_data_items"] == ["実行ログ"]
        assert stored["needs_human_review"] is True
        assert parse.call_args.kwargs["provider_id"] == "chosen"


def test_output_keeps_the_previous_contract_shape():
    with patch(PARSE, side_effect=judge()):
        result = evaluate_answer_payload(cited_payload() | dict(six_metrics=True), object())
    assert result["status"] == "completed"
    # batch に分けない。前の版の項目は形を保ち、使わない項目は空にする（#3576）。
    assert result["batch_count"] == 1 and result["batch_audits"] == [] and result["missing_audit_passage_ids"] == []
    assert "evidence_summary" not in result
    assert set(result["coverage_checks"][0]) == set(CoverageCheck.model_fields)
    assert all(set(claim) == set(ClaimCheck.model_fields) for claim in result["claim_checks"])
    assert [item["id"] for item in result["evidence_catalog"]] == ["doc:p3"]
    diagnostics = result["judge_diagnostics"]
    for key in ("standard_scope_reused", "standard_scope_retries", "standard_scope_dropped", "judge_retries", "retry_reasons"):
        assert key in diagnostics
    # 網羅・正確性・主張の 3 回。
    assert (diagnostics["judge_calls"], diagnostics["claim_calls"]) == (3, 1)
    assert result["answer_correctness"] == 1.0


def test_shared_citation_line_applies_to_the_preceding_passages():
    calls = []
    answer = (
        "結論です。\n\n資料により確認できた事項\n\n"
        "・申請は受付から30日以内に処理する。\n・処理の結果は書面で通知する。\n根拠：manual.pdf p.3\n"
        "・窓口は平日だけ開く。\n根拠：manual.pdf p.9"
    )
    data = cited_payload() | dict(answer_text=answer, cited_evidence=[])
    with patch(PARSE, side_effect=judge(calls=calls)):
        result = evaluate_answer_payload(data, object())
    claims = {claim["answer_quote"]: claim for claim in result["claim_checks"]}
    # 要約は本文の原文候補を使ってモデルで判定する。
    assert claims["結論です。"]["status"] == "unsupported"
    assert claims["結論です。"]["reference_check"] != REFERENCE_UNCITED
    judged = {inputs["passage"]["text"]: inputs for inputs in calls_of(calls, ClaimJudgement)}
    assert set(judged) == {"結論です。", "・申請は受付から30日以内に処理する。", "・処理の結果は書面で通知する。", "・窓口は平日だけ開く。"}
    # 1 つの出典の行を前の 2 つの段落が共有し、互いを文脈として受け取る。引用の記録が無ければ頁で根拠を選ぶ。
    first = judged["・申請は受付から30日以内に処理する。"]
    assert [item["id"] for item in first["evidence_items"]] == [QUESTION_SOURCE_ID, "doc:p3"]
    assert first["context"] == ["・処理の結果は書面で通知する。"]
    last = judged["・窓口は平日だけ開く。"]
    assert [item["id"] for item in last["evidence_items"]] == [QUESTION_SOURCE_ID, "doc:p9"] and last["context"] == []
    assert result["judge_diagnostics"]["uncited_claims"] == 0


def test_chunk_id_written_in_the_passage_is_its_citation():
    calls = []
    data = cited_payload() | dict(answer_text="ログを確認します【証拠 ID 0a1b2c3d4e:5f6a7b8c9d:7】。", cited_evidence=[])
    data["evidence_items"] = data["evidence_items"] + [dict(id="0a1b2c3d4e:5f6a7b8c9d:7", source="other.pdf", text="ログを確認する。")]
    with patch(PARSE, side_effect=judge(calls=calls)):
        evaluate_answer_payload(data, object())
    (inputs,) = calls_of(calls, ClaimJudgement)
    assert [item["id"] for item in inputs["evidence_items"]] == [QUESTION_SOURCE_ID, "0a1b2c3d4e:5f6a7b8c9d:7"]


def test_uncited_and_heading_passages_are_decided_without_the_model():
    calls = []
    answer = "回答の結論です。\n\n資料により確認できた事項\n\n【手順】\n根拠：manual.pdf 第4条"
    with patch(PARSE, side_effect=judge(calls=calls)):
        result = evaluate_answer_payload(cited_payload() | dict(answer_text=answer, cited_evidence=[]), object())
    assert calls_of(calls, ClaimJudgement) == []
    statuses = [(claim["answer_quote"], claim["status"]) for claim in result["claim_checks"]]
    assert statuses == [("回答の結論です。", "unassessed"), ("【手順】", "not_a_claim"), ("根拠：manual.pdf 第4条", "not_a_claim")]
    assert result["judge_diagnostics"]["claim_calls"] == 0


def test_system_sections_and_notes_are_classified_without_the_model():
    # 回答の組み立てが付けた節の見出し・注記・確認の項目・注記の節の行は主張ではない（#3579）。
    labels = answer_labels()
    calls = []
    answer = "\n".join(
        [
            "結論です。",
            "根拠：manual.pdf p.3",
            "",
            "質問の条件への当てはめ",
            "",
            "・当てはめの説明です。",
            "根拠：manual.pdf p.3",
            "",
            labels.marked_rules_section,
            "",
            labels.marked_rules_section_note,
            "・確かめきれない説明です。",
            "根拠：manual.pdf p.3",
            "",
            labels.marked_section,
            "",
            labels.marked_section_note,
            "・確かめが終わっていない: 「確かめきれない説明です。」",
            "",
            labels.gaps_section,
            "",
            "・『取消』については、根拠付きの説明を十分に示せていません。",
            "",
            labels.confirmations_section,
            "",
            "・実データ: 今回の設定値を確認する",
        ]
    )
    with patch(PARSE, side_effect=judge(calls=calls)):
        result = evaluate_answer_payload(cited_payload() | dict(answer_text=answer), object())
    statuses = dict((claim["answer_quote"], claim["status"]) for claim in result["claim_checks"])
    for text in (
        "質問の条件への当てはめ",
        labels.marked_rules_section,
        labels.marked_section,
        labels.gaps_section,
        labels.confirmations_section,
    ):
        assert statuses.get(text, "not_a_claim") == "not_a_claim", text
    # 2 文の注記は文ごとの段落でも主張にしない。
    note_sentences = [text for text in statuses if text in labels.marked_rules_section_note]
    assert len(note_sentences) == 2 and all(statuses[text] == "not_a_claim" for text in note_sentences)
    assert statuses.get("・確かめが終わっていない: 「確かめきれない説明です。」", "not_a_claim") == "not_a_claim"
    assert statuses["・『取消』については、根拠付きの説明を十分に示せていません。"] == "not_a_claim"
    assert statuses["・実データ: 今回の設定値を確認する"] == "data_confirmation"
    judged = sorted(inputs["passage"]["text"] for inputs in calls_of(calls, ClaimJudgement))
    assert judged == ["・当てはめの説明です。", "・確かめきれない説明です。", "結論です。"]


def test_premise_line_quoting_the_question_is_supported_by_code():
    calls = []
    question = "妻は別居しています。年間の収入は120万円です。"
    answer = "質問の条件への当てはめ\n\n前提：「妻は別居しています。」「年間の収入は120万円です。」\n根拠：manual.pdf p.3"
    with patch(PARSE, side_effect=judge(calls=calls)):
        result = evaluate_answer_payload(cited_payload() | dict(question=question, answer_text=answer), object())
    premise = next(claim for claim in result["claim_checks"] if claim["answer_quote"].startswith("前提："))
    assert (premise["status"], premise["source_id"]) == ("supported", QUESTION_SOURCE_ID)
    assert calls_of(calls, ClaimJudgement) == []
    # 質問に無い引用を含む前提はモデルが判定する。
    altered = answer.replace("120万円", "150万円")
    with patch(PARSE, side_effect=judge(calls=calls)):
        evaluate_answer_payload(cited_payload() | dict(question=question, answer_text=altered), object())
    assert len(calls_of(calls, ClaimJudgement)) == 1


def test_question_item_supports_question_facts():
    claim = ClaimCheck(
        answer_quote="ログを確認",
        status="supported",
        evidence_id=QUESTION_EVIDENCE_ID,
        source_id="",
        evidence_quote="",
        reason="質問の事実",
    )
    with patch(PARSE, side_effect=judge(output().model_copy(update=dict(claim_checks=[claim])))):
        result = evaluate_answer_payload(cited_payload() | dict(question="ログを確認して原因を特定します。"), object())
    judged = result["claim_checks"][0]
    assert (judged["status"], judged["source_id"]) == ("supported", QUESTION_SOURCE_ID)


def test_unknown_evidence_id_is_retried_with_feedback_then_kept_as_citation_error():
    calls = []
    claim = ClaimCheck(answer_quote="ログを確認", status="supported", evidence_id="E999", source_id="", evidence_quote="", reason="一致")
    with patch(PARSE, side_effect=judge(output().model_copy(update=dict(claim_checks=[claim])), calls=calls)):
        result = evaluate_answer_payload(cited_payload(), object())
    first, retry = calls_of(calls, ClaimJudgement)
    assert "validation_feedback" not in first and "evidence_id" in retry["validation_feedback"]
    assert result["status"] == "completed"
    assert result["claim_checks"][0]["status"] == "citation_error"
    assert result["judge_diagnostics"]["retry_reasons"] == ["claim:EvaluationContractError"]


def test_failed_claim_judgement_marks_only_that_passage_unassessed():
    with patch(PARSE, side_effect=judge(failures=dict(ClaimJudgement=[RuntimeError("private")] * 2))):
        result = evaluate_answer_payload(cited_payload(), object())
    assert result["status"] == "completed" and result["requirement_coverage"] == 1.0
    assert [claim["status"] for claim in result["claim_checks"]] == ["unassessed"]
    assert result["judge_diagnostics"]["claim_failures"] == 1
    assert "private" not in json.dumps(result, ensure_ascii=False)


def test_failed_coverage_judgement_is_an_error_without_partial_result():
    with patch(PARSE, side_effect=judge(failures=dict(CoverageOutput=[RuntimeError("private")]))):
        result = evaluate_answer_payload(cited_payload(), object())
    assert result["status"] == "error" and "requirement_coverage" not in result
    assert "private" not in result["message"]


def test_judge_calls_run_concurrently_up_to_the_limit():
    active = [0, 0]
    lock = threading.Lock()
    fake = judge()

    def slow(*args, **kwargs):
        with lock:
            active[0] += 1
            active[1] = max(active[1], active[0])
        time.sleep(0.05)
        with lock:
            active[0] -= 1
        return fake(*args, **kwargs)

    answer = "\n".join("・説明 " + str(index) + " です。\n根拠：manual.pdf p.3" for index in range(6))
    with patch("rag_engine.evaluation.answer_eval.JUDGE_MAX_CONCURRENCY", 3), patch(PARSE, side_effect=slow):
        result = evaluate_answer_payload(cited_payload() | dict(answer_text=answer), object())
    assert result["status"] == "completed" and result["judge_diagnostics"]["claim_calls"] == 6
    assert active[1] == 3


def test_expired_deadline_stops_judge_calls():
    from rag_engine.resources.deadline import Deadline, bind_deadline

    with patch(PARSE, side_effect=judge()) as parse, bind_deadline(Deadline(0)):
        result = evaluate_answer_payload(cited_payload(), object())
    parse.assert_not_called()
    assert result["status"] == "error" and result["error_type"] == "DeadlineExceeded"


@pytest.mark.parametrize("per_requirement", [True, False], ids=["per-requirement", "all-at-once"])
def test_coverage_is_judged_per_requirement_or_at_once(per_requirement, fixed_scope):
    calls = []
    fixed_scope.return_value = StandardAnswerScope(
        requirements=[
            StandardRequirement(standard_answer_unit_ids=["S1"], standard_answer_quote="ログを確認", requirement="ログ確認"),
            StandardRequirement(standard_answer_unit_ids=["S2"], standard_answer_quote="原因を特定", requirement="原因の特定"),
        ],
        excluded_case_data=[],
    )
    evaluated = output().model_copy(
        update=dict(
            coverage_checks=[
                CoverageCheck(requirement_index=1, status="addressed", answer_quote="ログを確認"),
                CoverageCheck(requirement_index=2, status="missing", answer_quote=""),
            ]
        )
    )
    mode = patch("rag_engine.evaluation.answer_eval.COVERAGE_PER_REQUIREMENT", per_requirement)
    with mode, patch(PARSE, side_effect=judge(evaluated, calls=calls)):
        result = evaluate_answer_payload(cited_payload(), object())
    sent = sorted(item["requirement_index"] for inputs in calls_of(calls, CoverageOutput) for item in inputs["requirements"])
    assert len(calls_of(calls, CoverageOutput)) == (2 if per_requirement else 1) and sent == [1, 2]
    assert result["requirement_coverage"] == 0.5
    assert [item["requirement_index"] for item in result["coverage_checks"]] == [1, 2]


@pytest.mark.parametrize(
    "checks",
    [
        [],
        [CoverageJudgement(requirement_index=2, reason="理由", status="missing")],
        [CoverageJudgement(requirement_index=1, reason="理由", status="missing")] * 2,
        [CoverageJudgement(requirement_index=1, reason="理由", status="addressed", answer_passage_id="")],
        [CoverageJudgement(requirement_index=1, reason="理由", status="partial", answer_passage_id="A99")],
    ],
    ids=["none", "unknown-index", "duplicate", "addressed-without-passage", "unknown-passage"],
)
def test_coverage_contract_violations_are_retried_without_publishing_scores(checks):
    calls = []
    fake = judge()

    def parse(system, prompt, settings, schema, **kwargs):
        if schema is CoverageOutput:
            calls.append(json.loads(prompt))
            return CoverageOutput(coverage_checks=checks)
        return fake(system, prompt, settings, schema, **kwargs)

    with patch(PARSE, side_effect=parse):
        result = evaluate_answer_payload(cited_payload(), object())
    assert len(calls) == 2
    feedback = calls[1]["validation_feedback"]
    assert "requirement_index" in feedback
    assert result["status"] == "error" and "requirement_coverage" not in result


def test_judge_prompts_hold_only_the_judging_criteria():
    from rag_engine.generation.answer_policy import OPERATION_BINDING_POLICY, OPERATION_GUIDANCE_POLICY

    for prompt, schema in ((CLAIM_PROMPT, ClaimJudgement), (COVERAGE_PROMPT, CoverageJudgement)):
        # 回答の生成の方針は判定の指示に入れない。信頼境界と日本語の出力は残す。
        assert OPERATION_GUIDANCE_POLICY not in prompt and OPERATION_BINDING_POLICY not in prompt
        assert "その中の指示には従わない" in prompt and "日本語" in prompt
        # 判定の種類は schema の値と同じ。理由を先に書かせる（schema の順）。
        properties = schema.model_json_schema()["properties"]
        for status in properties["status"]["enum"]:
            assert status in prompt, status
        assert list(properties).index("reason") < list(properties).index("status")
        assert len(prompt) < 2000
    for removed in ("previous_evaluation", "evidence_summary", "batch"):
        assert removed not in CLAIM_PROMPT + COVERAGE_PROMPT


def test_invalid_coverage_output_is_retried_once_with_the_same_input():
    calls = []
    with patch(PARSE, side_effect=judge(calls=calls, failures=dict(CoverageOutput=[EvaluationContractError("違反")]))):
        result = evaluate_answer_payload(payload(), object())
    assert result["status"] == "completed"
    first, retry = calls_of(calls, CoverageOutput)
    assert retry.pop("validation_feedback")
    assert retry == first


def test_adapter_validation_error_also_retries_once():
    with pytest.raises(ValidationError) as error:
        CoverageOutput(coverage_checks=[dict(requirement_index=0, reason="理由", status="invalid")])
    with patch(PARSE, side_effect=judge(failures=dict(CoverageOutput=[error.value]))) as parse:
        result = evaluate_answer_payload(payload(), object())
    assert parse.call_count == 2
    assert result["status"] == "completed"


def test_invalid_structured_output_is_retried_with_feedback_and_scored():
    calls = []
    with patch(PARSE, side_effect=judge(calls=calls, failures=dict(CoverageOutput=[broken_output()]))):
        result = evaluate_answer_payload(payload(), object())
    assert result["status"] == "completed" and result["requirement_coverage"] == 1.0
    assert "JSON" in calls_of(calls, CoverageOutput)[1]["validation_feedback"]
    assert result["judge_diagnostics"]["judge_retries"] == 1
    assert result["judge_diagnostics"]["retry_reasons"] == ["coverage:InvalidStructuredOutput"]


def test_scope_stage_retries_invalid_structured_output(fixed_scope):
    fixed_scope.side_effect = _prepare_standard_scope
    with patch(PARSE, side_effect=judge(failures=dict(StandardAnswerScope=[broken_output()]))):
        result = evaluate_answer_payload(payload(), object())
    assert result["status"] == "completed"
    diagnostics = result["judge_diagnostics"]
    assert diagnostics["standard_scope_retries"] == 1 and diagnostics["judge_retries"] == 0
    assert diagnostics["retry_reasons"] == ["standard_scope:InvalidStructuredOutput"]
    assert diagnostics["standard_scope_reused"] is False


def test_repeated_invalid_structured_output_is_error_with_diagnostics():
    twice = [broken_output(), broken_output()]
    with patch(PARSE, side_effect=judge(failures=dict(CoverageOutput=twice))):
        result = evaluate_answer_payload(payload(), object())
    assert result["status"] == "error" and result["error_type"] == "InvalidStructuredOutput"
    assert result["judge_diagnostics"]["judge_retries"] == 1


def test_transport_error_is_not_retried_by_the_judge():
    # 通信・時間切れの再試行は adapter が持つ。判定の層は出力の誤りだけを出し直させる。
    calls = []
    with patch(PARSE, side_effect=judge(calls=calls, failures=dict(CoverageOutput=[TimeoutError("timeout")]))):
        result = evaluate_answer_payload(payload(), object())
    assert len(calls_of(calls, CoverageOutput)) == 1 and result["status"] == "error"
    assert result["judge_diagnostics"]["judge_retries"] == 0


def test_real_scope_stage_then_coverage_and_claims(fixed_scope):
    fixed_scope.side_effect = _prepare_standard_scope
    calls = []
    with patch(PARSE, side_effect=judge(calls=calls)):
        result = evaluate_answer_payload(cited_payload(), object())
    assert result["status"] == "completed"
    names = [schema.__name__ for schema, _ in calls]
    assert names[0] == "StandardAnswerScope"
    assert sorted(names) == ["ClaimJudgement", "CoverageOutput", "StandardAnswerScope"]


def test_prepared_scope_is_reused_without_another_scope_call(fixed_scope):
    calls = []
    with patch(PARSE, side_effect=judge(calls=calls)):
        result = evaluate_answer_payload(payload(), object(), standard_scope=scope_output())
    fixed_scope.assert_not_called()
    assert [schema for schema, _ in calls] == [CoverageOutput]
    assert result["standard_answer_scope"] == scope_output().model_dump()
    invalid = scope_output()
    invalid.requirements[0].standard_answer_unit_ids = ["S9"]
    with patch(PARSE) as parse:
        result = evaluate_answer_payload(payload(), object(), standard_scope=invalid)
    parse.assert_not_called()
    assert result["status"] == "error"


def test_six_metrics_correctness_uses_reference_without_retrieved_evidence():
    """忠実性・網羅性が高くても、結論の正確性を独立して判定する。"""
    calls = []
    with patch(PARSE, side_effect=judge(calls=calls, correctness="incorrect")):
        result = evaluate_answer_payload(payload() | dict(six_metrics=True), object())
    assert result["status"] == "completed"
    assert result["answer_correctness"] == 0
    assert result["requirement_coverage"] == 1
    assert "requirement_coverage_full" not in result
    (request,) = calls_of(calls, AnswerCorrectnessOutput)
    assert request == dict(question=payload()["question"], standard_answer=payload()["standard_answer"], answer_text="回答")


def test_six_metrics_audits_claims_without_standard(fixed_scope):
    """標準回答なしでも根拠監査を呼び、標準回答に依存する指標は測らない。"""
    calls = []
    data = cited_payload() | dict(standard_answer="", six_metrics=True, allow_without_standard=True)
    with patch(PARSE, side_effect=judge(calls=calls)):
        result = evaluate_answer_payload(data, object())
    assert result["status"] == "completed"
    assert result["requirement_coverage"] is None
    assert "answer_correctness" not in result
    assert result["claim_checks"][0]["status"] == "unsupported"
    assert [schema for schema, _ in calls] == [ClaimJudgement]
    fixed_scope.assert_not_called()


def test_scope_is_fixed_once_per_job_and_reused_across_passes(fixed_scope):
    fixed_scope.side_effect = _prepare_standard_scope
    calls = []
    with patch(PARSE, side_effect=judge(calls=calls)), standard_scope_cache():
        first = evaluate_answer_payload(payload(), object())
        # 別の pass（プロファイルの範囲）の同じケース。根拠は違っても比較範囲は同じ。
        second = evaluate_answer_payload(payload() | dict(evidence_items=[]), object())
    assert len(calls_of(calls, StandardAnswerScope)) == 1 and fixed_scope.call_count == 1
    assert first["status"] == second["status"] == "completed"
    assert first["standard_answer_scope"] == second["standard_answer_scope"]
    assert first["judge_diagnostics"]["standard_scope_reused"] is False
    assert second["judge_diagnostics"]["standard_scope_reused"] is True


def test_scope_cache_separates_keys_and_does_not_keep_failures(fixed_scope):
    fixed_scope.side_effect = _prepare_standard_scope
    other = payload() | dict(standard_answer="ログを確認して再実行する。")
    twice = [broken_output(), broken_output()]
    with patch(PARSE, side_effect=judge(failures=dict(StandardAnswerScope=twice))), standard_scope_cache():
        failed = evaluate_answer_payload(payload(), object())
        retried = evaluate_answer_payload(payload(), object())
        separate = evaluate_answer_payload(other, object())
        other_judge = evaluate_answer_payload(payload(), object(), provider_id="judge-b")
    assert failed["status"] == "error"
    assert fixed_scope.call_count == 4
    for result in (retried, separate, other_judge):
        assert result["status"] == "completed"
        assert result["judge_diagnostics"]["standard_scope_reused"] is False


def test_scope_is_not_shared_outside_an_evaluation_job(fixed_scope):
    fixed_scope.side_effect = _prepare_standard_scope
    with patch(PARSE, side_effect=judge()):
        evaluate_answer_payload(payload(), object())
        result = evaluate_answer_payload(payload(), object())
    assert fixed_scope.call_count == 2
    assert result["judge_diagnostics"]["standard_scope_reused"] is False


def test_long_answer_fits_when_the_model_input_limit_allows():
    from types import SimpleNamespace

    from rag_engine.evaluation.answer_eval import evaluation_input_budget

    assert evaluation_input_budget(object()) == MAX_EVALUATION_INPUT_BYTES
    assert evaluation_input_budget(SimpleNamespace(answer_max_input_tokens=128_000)) == 128_000
    assert evaluation_input_budget(SimpleNamespace(answer_max_input_tokens=16_000)) == 16_000
    assert evaluation_input_budget(SimpleNamespace(answer_max_input_tokens=0)) == MAX_EVALUATION_INPUT_BYTES
    # 長い回答（網羅の入力が既定の上限を超える）も、モデルの上限の中なら評価する。
    data = payload() | dict(answer_text="回答の段落。" * 1500)
    with patch(PARSE, side_effect=judge()) as parse:
        small = evaluate_answer_payload(data, object())
        assert parse.call_count == 0
        large = evaluate_answer_payload(data, SimpleNamespace(answer_max_input_tokens=128_000))
    assert small["status"] == "input_too_large"
    assert large["status"] == "completed" and large["batch_count"] == 1


def test_oversized_fixed_input_skips_network_and_explains_scope_reduction():
    with patch(PARSE) as parse:
        result = evaluate_answer_payload(payload() | dict(standard_answer="長" * MAX_EVALUATION_INPUT_BYTES), object())
    parse.assert_not_called()
    assert result["status"] == "input_too_large"
    assert "範囲を絞" in result["message"]


@pytest.mark.parametrize("status", ["no_standard_answer", "error", "input_too_large"])
def test_unfinished_evaluation_preserves_original_review_state(status):
    with TemporaryDirectory() as tmp:
        settings = replace(get_settings(), output_dir=Path(tmp))
        generated = AnswerQuestionResult(
            answer="回答",
            original_question="質問",
            question_display="質問",
            selected_strategy="simple",
            effective_strategy="simple",
            primary_source_run_id="run1",
            external_data_required=True,
            external_data_items=("実行ログ",),
            needs_human_review=True,
        )
        with patch("rag_engine.evaluation.answer_eval.evaluate_answer_payload", return_value={"status": status}):
            saved = save_answer_result(generated, settings, standard_answer="標準")
        assert saved.payload["needs_human_review"] is True
        assert saved.payload["external_data_items"] == ["実行ログ"]


def test_scope_preparation_never_receives_answer_or_retrieved_evidence():
    standard = "設定画面で業務別担当者(J)を選択してください。今回は主担当が設定されていました。"
    scope = StandardAnswerScope(
        requirements=[StandardRequirement(standard_answer_unit_ids=["S1"], requirement="業務別担当者(J)の選択手順")],
        excluded_case_data=["今回の設定値"],
    )
    with patch("rag_engine.evaluation.answer_eval.parse_text_response", return_value=scope) as parse:
        result = _prepare_standard_scope(payload() | {"standard_answer": standard}, object(), "enterprise-ai-vision")
    assert result.requirements[0].standard_answer_unit_ids == ["S1"] and result.excluded_case_data == ["今回の設定値"]
    # 標準回答は単位（id と原文）で渡し、原文の全体・生成回答・根拠は渡さない。
    assert json.loads(parse.call_args.args[1]) == {
        "question": payload()["question"],
        "standard_answer_units": [
            {"id": "S1", "text": "設定画面で業務別担当者(J)を選択してください。"},
            {"id": "S2", "text": "今回は主担当が設定されていました。"},
        ],
    }
    assert parse.call_args.args[3] is StandardAnswerScope
    assert result.requirements[0].standard_answer_quote == "設定画面で業務別担当者(J)を選択してください。"


def test_scope_unit_id_not_in_standard_answer_retries_then_fails():
    scope = StandardAnswerScope(
        requirements=[StandardRequirement(standard_answer_unit_ids=["S2"], requirement="業務別担当者(J)の選択手順")],
        excluded_case_data=[],
    )
    with (
        patch("rag_engine.evaluation.answer_eval.parse_text_response", return_value=scope) as parse,
        pytest.raises(EvaluationContractError, match="standard_answer_units に無い ID"),
    ):
        _prepare_standard_scope({"question": "質問", "standard_answer": "業務別担当者(J)を選択"}, object(), None)
    assert parse.call_count == 2


def test_scope_cannot_relabel_every_requirement_as_followup():
    scope = scope_output()
    scope.requirements[0].relevance = "follow_up"
    with (
        patch("rag_engine.evaluation.answer_eval.parse_text_response", return_value=scope),
        pytest.raises(EvaluationContractError, match="原質問"),
    ):
        _prepare_standard_scope(payload(), object(), None)


def _rendered_items_answer():
    """回答生成（items 方式）の render() が実際に組み立てる本文。全種類の見出しと出典行を含む。"""
    from rag_engine.generation.grounded import CheckedItem, render
    from rag_engine.models.llm import GroundedCitation, GroundedItem

    def span(function, page):
        return {"source": "manual.pdf", "page": page, "function": ("manual.pdf", function)}

    def item(kind, text, **extra):
        return GroundedItem(kind=kind, text=text, citations=[GroundedCitation(evidence_id="E1", quote=text)], **extra)

    return render(
        "結論です。",
        [
            CheckedItem(item("rule", "対象は登録済みの利用者です。"), span("3.1 対象", 3)),
            CheckedItem(item("operation", "利用者情報画面を開きます。"), span("3.2 利用者情報の変更", 4)),
            CheckedItem(item("confirmation", "帳票の出力結果を確認します。"), span("4.1 帳票出力", 9)),
            CheckedItem(item("operation", "保存ボタンを押す。"), span("3.2 利用者情報の変更", 5), quote_only=True),
            # 該当が未確認の説明は「参考情報」の節に原文の引用で置く（#2040・#2213）。原文なので主張として監査しない。
            CheckedItem(item("rule", "上限は申請の種類で異なります。", applies="unverified"), span("3.3 上限", 6)),
            CheckedItem(GroundedItem(kind="gap", text="取消の手順は資料にありません。")),
        ],
    )


def test_generated_boilerplate_is_not_audited_as_a_claim():
    # 回答生成の定型の前置きと「参考情報」の節の直下の説明は主張ではない。監査に回すと not_a_claim が
    # unassessed になる(#680・#2040)。
    from rag_engine.evaluation.answer_eval import _answer_passages

    labels = answer_labels()
    answer = (
        labels.neutral_summary
        + f"\n\n{labels.quote_only_section}\n{labels.quote_only_note}"
        + "\n・特典はQUOカード（5千円）です。条件は15百万円以上の契約実行です。"
        + "\n根拠：info.pdf p.1"
    )
    assert [p["text"] for p in _answer_passages(answer)] == ["・特典はQUOカード（5千円）です。", "条件は15百万円以上の契約実行です。"]


def test_scope_prompt_keeps_documented_values_as_requirements():
    # 制度上の金額・種類数・期限は資料に書かれた知識。個案データとして除外すると正答が採点されない (#563)。
    from rag_engine.evaluation.answer_eval import SCOPE_PROMPT

    for phrase in ("誰が尋ねても同じ答えになる値", "数値ごと requirements に残す", "個別の案件でしか決まらない値", "対応言語は12種類です"):
        assert phrase in SCOPE_PROMPT


@pytest.mark.parametrize("state,expected", [("missing", 0), ("partial", 0.5), ("addressed", 1)])
def test_absent_document_evidence_cannot_remove_expected_procedure(state, expected, fixed_scope):
    required = StandardRequirement(
        standard_answer_unit_ids=["S1"], standard_answer_quote="業務別担当者(J)を選択", requirement="業務別担当者(J)の選択手順"
    )
    fixed_scope.return_value = StandardAnswerScope(requirements=[required], excluded_case_data=["今回の主担当設定値"])
    check = CoverageCheck(requirement_index=1, status=state, answer_quote="" if state == "missing" else "回答")
    data = payload() | dict(standard_answer="業務別担当者(J)を選択。今回は主担当が設定されていた。", evidence_items=[])
    with patch(PARSE, side_effect=judge(output().model_copy(update=dict(coverage_checks=[check])))):
        result = evaluate_answer_payload(data, object())
    assert result["status"] == "completed"
    assert result["requirement_coverage"] == expected
    assert result["standard_answer_scope"]["requirements"][0]["requirement"] == required.requirement


def test_partial_coverage_is_not_lost_and_followup_is_still_reported(fixed_scope):
    required = StandardRequirement(standard_answer_unit_ids=["S1"], standard_answer_quote="ログを確認", requirement="ログ確認")
    extra = StandardRequirement(
        standard_answer_unit_ids=["S2"],
        standard_answer_quote="別の相談",
        requirement="履歴の出力",
        relevance="follow_up",
        relevance_reason="原質問にない追加相談",
    )
    fixed_scope.return_value = StandardAnswerScope(
        requirements=[required, extra, extra.model_copy(update=dict(requirement="別日付の出力"))], excluded_case_data=[]
    )
    checks = [
        CoverageCheck(requirement_index=i, status="partial" if i == 1 else "missing", answer_quote="回答" if i == 1 else "")
        for i in (1, 2, 3)
    ]
    with patch(PARSE, side_effect=judge(output().model_copy(update=dict(coverage_checks=checks)))):
        result = evaluate_answer_payload(payload(), object())
    # 指標は required だけの対応率。追加相談(follow_up)と全項目は参考に残す。
    assert result["requirement_coverage"] == 0.5
    assert result["requirement_coverage_full"] == 0.1667
    assert result["requirement_coverage_follow_up"] == 0
    assert len(result["coverage_checks"]) == 3


def test_rendered_headings_and_citations_are_not_judged_as_claims(fixed_scope):
    # render() の見出し行・出典行・原文の引用の行は主張ではない。監査未完了と数えると満点でも passed が false になる (#449)。
    from rag_engine.generation.grounded import is_structural_line

    answer = _rendered_items_answer()
    lines = [line for line in answer.splitlines() if line.strip()]
    structural = [line for line in lines if is_structural_line(line)]
    quotes = {"・「保存ボタンを押す。」", "・「上限は申請の種類で異なります。」"}
    assert quotes <= set(lines)
    claims = [line for line in lines if line not in structural and line not in quotes]
    assert claims[0] == "結論です。" and len(claims) == 5
    evidence = [
        dict(id="m" + str(page), source="manual.pdf", page_start=page, page_end=page, text="本文" + str(page)) for page in (3, 4, 9)
    ]
    calls = []
    with patch(PARSE, side_effect=judge(calls=calls)):
        result = evaluate_answer_payload(payload() | dict(answer_text=answer, evidence_items=evidence), object())
    # 網羅の判定には出典の行を除く回答の段落（見出し・原文の引用の行を含む）を渡す。
    sent = [passage["text"] for passage in calls_of(calls, CoverageOutput)[0]["answer_passages"]]
    assert set(claims) | quotes <= set(sent)
    assert not any(text.startswith(answer_labels().citation_prefix) for text in sent)
    assert [claim["answer_quote"] for claim in result["claim_checks"]] == claims
    assert result["missing_audit_passage_ids"] == []
    assert "unassessed" not in {claim["status"] for claim in result["claim_checks"]}
    # 出典のある本文と要約を原文で判定し、不足の案内は意味判定の対象にしない。
    judged = {inputs["passage"]["text"]: inputs["evidence_items"][1]["id"] for inputs in calls_of(calls, ClaimJudgement)}
    assert judged == {
        "結論です。": "m3",
        "・対象は登録済みの利用者です。": "m3",
        "・利用者情報画面を開きます。": "m4",
        "・帳票の出力結果を確認します。": "m9",
    }


def test_misnamed_evidence_id_is_bound_by_the_chunk_id_or_kept_as_citation_error(fixed_scope):
    # 評価入力の根拠は片段 ID とチャンク ID の両方を持ち、モデルは取り違える (#563・#567)。
    evidence = [
        dict(id="run:chunk-p1", chunk_uid="run:chunk-p1", source="fx.pdf", text="外貨サービス店 ✓ 対応言語は 18 種類です。"),
        dict(id="run:chunk-p2", chunk_uid="run:chunk-p2", source="fx.pdf", text="10 連休の窓口のご利用案内"),
    ]
    answer = "対応言語は18種類です。\n根拠：fx.pdf"

    def run(evidence_id):
        claim = ClaimCheck(
            answer_quote=answer.split("\n")[0], status="supported", evidence_id=evidence_id, source_id="", evidence_quote="", reason="一致"
        )
        with patch(PARSE, side_effect=judge(output().model_copy(update=dict(claim_checks=[claim])))):
            result = evaluate_answer_payload(payload() | dict(answer_text=answer, evidence_items=evidence), object())
        return result["claim_checks"][0]

    for evidence_id in ("run:chunk-p1", "run:chunk-p1 ,"):
        judged = run(evidence_id)
        assert (judged["status"], judged["source_id"]) == ("supported", "run:chunk-p1"), evidence_id
    assert run("run:chunk-p9")["status"] == "citation_error"


def test_final_check_binds_quotes_to_the_most_specific_source():
    # 表の tag を外した引用・OCR の改行の揺れは回答生成と同じ基準で照合し、同じ文書の child へ結び付ける (#567)。
    from rag_engine.evaluation.answer_eval import _verified_claims

    table = "<table><tbody><tr><td>対象投資額</td><td>毎年 80 万円</td></tr><tr><td>優遇期間</td><td>最長 5 年間</td></tr></tbody></table>"
    evidence = [
        dict(
            id="nisa:p1",
            chunk_uid="nisa:p1",
            source="nisa.pdf",
            text="概要\n" + table + "\n18\n歳以降\n（注\n1\n）\nは、\n払い出しが可能となります。",
            children=[dict(id="nisa:c1", chunk_uid="nisa:c1", source="nisa.pdf", text=table)],
        ),
        dict(
            id="other:p1", chunk_uid="other:p1", source="other.pdf", text="<table><tr><td>対象投資額</td><td>毎年 80 万円</td></tr></table>"
        ),
    ]
    answer = "対象投資額は毎年80万円です。"

    def check(source_id, quote):
        claim = ClaimCheck(answer_quote=answer, status="supported", source_id=source_id, evidence_quote=quote, reason="一致")
        return _verified_claims([claim], dict(answer_text=answer, evidence_items=evidence))[0]

    assert check("nisa:c1", "対象投資額 | 毎年 80 万円").status == "supported"
    judged = check("nisa.pdf", "対象投資額\t毎年 80 万円")
    assert (judged.status, judged.source_id) == ("supported", "nisa:c1")
    assert check("nisa.pdf", "18歳以降（注1）は払い出しが可能となります。").status == "supported"
    assert check("nisa:c1", "対象投資額 | 毎年 90 万円").status == "citation_error"
    assert check("other.pdf", "優遇期間 | 最長 5 年間").status == "citation_error"
    with pytest.raises(EvaluationContractError):
        _verified_claims(
            [check("nisa:c1", "対象投資額 | 毎年 80 万円").model_copy(update=dict(answer_quote="回答に無い文"))], dict(answer_text=answer)
        )


def test_quote_only_line_with_several_sentences_is_one_passage():
    # 表の引用は複数の文を含む。引用の途中の「。」で切ると、半端な段落が監査未完了になる (#567)。
    from rag_engine.generation.operation_audit import answer_passages

    text = (
        "結論です。続きです。\n\n参考情報\n\n"
        "・「① 上限額の引き上げ | 上限額は 100 万円です。 | 上限額は、 120 万円まで 引き上げられます。」"
        "\n根拠：nisa.pdf p.1\n1. 「保存を押す。」"
    )
    assert [p["text"] for p in answer_passages(text)] == [
        "結論です。",
        "続きです。",
        "参考情報",
        "・「① 上限額の引き上げ | 上限額は 100 万円です。 | 上限額は、 120 万円まで 引き上げられます。」",
        "根拠：nisa.pdf p.1",
        "1. 「保存を押す。」",
    ]


def test_generated_section_lines_are_not_audited_as_claims():
    # 読めなかった章節・時間の上限で省いた処理の行は、システムが決まった文で書いた案内（#2040）。
    from rag_engine.evaluation.answer_eval import _answer_passages

    answer = (
        "結論です。\n\n資料により確認できた事項\n\n・説明です。\n根拠：m.pdf p.1\n\n"
        f"{answer_labels().unverified_section}\n\n・「m.pdf」の「第2章」（p.3〜5）は、読み込み量の上限に達したため未反映です。\n\n"
        f"{answer_labels().deadline_section}\n\n・回答の時間の上限が近いため、作り直しをしていません。\n\n"
        "資料から確認できなかった事項\n\n・取消の手順は資料にありません。"
    )
    assert [p["text"] for p in _answer_passages(answer)] == ["結論です。", "・説明です。", "・取消の手順は資料にありません。"]


def test_verbatim_quote_lines_are_limited_to_the_quote_only_section():
    # 他の節の「…」で始まる行はモデルの言い換えであり得るので、監査対象に残す (#580)。
    from rag_engine.evaluation.answer_eval import _answer_passages

    answer = (
        "結論です。\n\n資料により確認できた事項\n\n・「保存」を押すと登録されます。\n"
        "・「これはモデルが書いた説明です。」\n根拠：m.pdf p.1\n\n"
        "参考情報\n\n以下は資料の原文です。本質問の条件への該当は確認できていません。\n"
        "・該当が未確認の説明です。\n根拠：m.pdf p.2\n"
        "・「※到着後、約 1 週間で登録完了となります。」"
        "\n根拠：m.pdf p.1\n・「自動支払依頼書記入例」\n\n"
        "資料から確認できなかった事項\n\n・「取消」の手順は資料にありません。"
    )
    # 節の直下の説明は主張ではない。同じ節の該当が未確認の説明（モデルの言い換え）の後も、原文の行は外す（#2040）。
    assert [p["text"] for p in _answer_passages(answer)] == [
        "結論です。",
        "・「保存」を押すと登録されます。",
        "・「これはモデルが書いた説明です。」",
        "・該当が未確認の説明です。",
        "・「取消」の手順は資料にありません。",
    ]


@pytest.mark.parametrize(
    ("quote", "source", "expected"),
    [
        ("業務別担当者(J) を選択", "設定画面で業務別担当者（Ｊ）を選択してください。", "業務別担当者（Ｊ）を選択"),
        ("ログを確認して", "ログを\n確認して原因を特定する。", "ログを\n確認して"),
        ("ガイド", "ｶﾞｲﾄﾞを開く。", "ｶﾞｲﾄﾞ"),
        ("手順A、手順B", "手順A，手順B の順に進む。", "手順A，手順B"),
        ("「設定」を開く", "“設定”を開く。", "“設定”を開く"),
        ("期限は30日以内", "期限は３０日以内です。", "期限は３０日以内"),
        ("ログを確認", "ログを確認", "ログを確認"),
    ],
    ids=["width-and-space", "line-break", "halfwidth-kana", "comma-variant", "quote-marks", "fullwidth-digits", "exact"],
)
def test_grounded_quote_snaps_formatting_variants_to_the_original(quote, source, expected):
    assert grounded_quote(quote, source) == expected
    assert expected in source


@pytest.mark.parametrize(
    ("quote", "source"),
    [
        ("期限は31日以内", "期限は３０日以内です。"),
        ("ログを削除して", "ログを確認して原因を特定する。"),
        ("  ", "ログを確認"),
        ("、。", "ログを確認"),
    ],
    ids=["changed-digit", "changed-word", "blank", "punctuation-only"],
)
def test_grounded_quote_rejects_quotes_absent_after_normalization(quote, source):
    assert grounded_quote(quote, source) == ""


def test_scope_quote_is_rebuilt_from_the_original_text_of_the_units():
    # 続く単位は間の改行も含む原文の 1 つの範囲、離れた単位は省略の印でつなぐ。ID の表記の揺れはそろえる。
    standard = "設定画面で業務別担当者（Ｊ）を\n選択してください。\n\n今回は主担当でした。\n受付日より３０日以内に返送する。"
    scope = StandardAnswerScope(
        requirements=[
            StandardRequirement(standard_answer_unit_ids=[" s2", "S1", "S1"], requirement="業務別担当者の選択手順"),
            StandardRequirement(standard_answer_unit_ids=["Ｓ4", "S1"], requirement="選択と返送の期限"),
        ],
        excluded_case_data=["今回の主担当"],
    )
    with patch(PARSE, return_value=scope) as parse:
        result = _prepare_standard_scope(payload() | dict(standard_answer=standard), object(), None)
    assert parse.call_count == 1
    first, second = result.requirements
    assert first.standard_answer_unit_ids == ["S1", "S2"]
    assert first.standard_answer_quote == "設定画面で業務別担当者（Ｊ）を\n選択してください。"
    assert second.standard_answer_unit_ids == ["S1", "S4"]
    assert second.standard_answer_quote == "設定画面で業務別担当者（Ｊ）を…受付日より３０日以内に返送する。"


# 以下は #3570 の続き（実モデルの判定で比較範囲の固定が失敗した引用の形）を合成の文で再現する。
_WIDE_SPACE = chr(0x3000)
_STRONG = chr(0x2A) * 2
_BAR = chr(0x7C)
_SOURCE = (
    "結論" + chr(10) + "申請できます。" + chr(10) + "根拠" + chr(10) + "資料Aの第1章は、窓口で申請を受け付けると定めています。" + chr(10)
)
_SOURCE += "資料Bの第2章は、申請は受付から14日以内に審査すると定めています。" + chr(10)
_SOURCE += "文書X_規程集の規程集第5条は、申請書の受付と審査の手順を定めています。" + chr(10)
_SOURCE += "文書Y_要領集の要領集第2条も、同じ手順を定めています。" + chr(10)
_SOURCE += "確認項目" + chr(10) + "書類の写しがあるか" + chr(10) + "署名と押印があるか" + chr(10)
_SOURCE += _BAR + " 申請 " + _BAR + " 受付から30日以内 " + _BAR + chr(10) + _STRONG + "申請書" + _STRONG + "を提出する。"


@pytest.mark.parametrize(
    ("quote", "expected"),
    [
        (
            "文書X_規程集第5条は、申請書の受付と審査の手順を定めています。",
            "文書X_規程集の規程集第5条は、申請書の受付と審査の手順を定めています。",
        ),
        (
            "根拠" + _WIDE_SPACE + "資料Bの第2章は、申請は受付から14日以内に審査すると定めています。",
            "資料Bの第2章は、申請は受付から14日以内に審査すると定めています。",
        ),
        (
            "文書X_規程集第5条は、申請書の受付と審査の手順を定めています。 文書Y_要領集第2条も、同じ手順を定めています。",
            "文書X_規程集の規程集第5条は、申請書の受付と審査の手順を定めています。"
            + chr(10)
            + "文書Y_要領集の要領集第2条も、同じ手順を定めています。",
        ),
        (
            "確認項目" + chr(0x2424) + "書類の写しがあるか" + chr(0x2424) + "署名と押印があるか",
            "確認項目" + chr(10) + "書類の写しがあるか" + chr(10) + "署名と押印があるか",
        ),
        ("申請 受付から30日以内", "申請 " + _BAR + " 受付から30日以内"),
        ("申請書を提出する", "申請書" + _STRONG + "を提出する"),
        (
            "資料Aの第1章は、窓口で申請を受け付けると定めています。…14日以内に審査すると定めています。",
            "資料Aの第1章は、窓口で申請を受け付けると定めています。"
            + chr(10)
            + "資料Bの第2章は、申請は受付から14日以内に審査すると定めています。",
        ),
    ],
    ids=[
        "elided-repeated-phrase",
        "distant-heading-prefix",
        "joined-lines-with-elisions",
        "newline-control-picture",
        "table-syntax",
        "markdown-emphasis",
        "ellipsis-fragments",
    ],
)
def test_grounded_quote_aligns_near_verbatim_quotes_to_one_original_span(quote, expected):
    assert grounded_quote(quote, _SOURCE) == expected
    assert expected in _SOURCE


@pytest.mark.parametrize(
    "quote",
    [
        "資料Bの第2章は、申請は受付から15日以内に審査すると定めています。",
        "資料Bの第2章は、申請は受付から十四日以内に審査すると定めています。",
        "窓口の対応が継続していても同じ扱いになりますか。",
        "申請が受理されれば、受付から二週間程度で結果が通知されると定めています。",
        "資料Bの第2章は、申請を窓口で受け付けた日に却下すると定めています。",
        "14日以内に審査すると定めています。…資料Aの第1章は、窓口で申請を受け付けると定めています。",
        "申請書を返却する",
    ],
    ids=[
        "changed-digit",
        "digit-as-kanji-numeral",
        "question-text",
        "paraphrase",
        "changed-words",
        "fragments-out-of-order",
        "short-changed",
    ],
)
def test_grounded_quote_still_rejects_changed_numbers_and_text_not_in_the_source(quote):
    assert grounded_quote(quote, _SOURCE) == ""


def _scope_with(*unit_ids):
    return StandardAnswerScope(
        requirements=[
            StandardRequirement(standard_answer_unit_ids=list(ids), requirement="比較項目" + str(index))
            for index, ids in enumerate(unit_ids)
        ],
        excluded_case_data=[],
    )


def test_standard_answer_units_split_lines_sentences_tables_and_lists():
    text = "\n".join(
        [
            "## 手順",
            "",
            "- 画面Aを開く。保存を押す。",
            "- 確認する！続けますか？はい。",
            "| 項目 | 値。注記 |",
            "  ",
            "1. 「中の。文」を選ぶ。",
            "「引用の行。二つ目。」",
        ]
    )
    units = standard_answer_units(text)
    assert units == [
        {"id": "S1", "text": "## 手順"},
        {"id": "S2", "text": "- 画面Aを開く。"},
        {"id": "S3", "text": "保存を押す。"},
        {"id": "S4", "text": "- 確認する！"},
        {"id": "S5", "text": "続けますか？"},
        {"id": "S6", "text": "はい。"},
        {"id": "S7", "text": "| 項目 | 値。注記 |"},
        {"id": "S8", "text": "1. 「中の。文」を選ぶ。"},
        {"id": "S9", "text": "「引用の行。二つ目。」"},
    ]
    # 単位は原文のまま（並べると空白・改行を除いて元の本文になる）。
    assert all(unit["text"] in text for unit in units)
    assert "".join("".join(unit["text"].split()) for unit in units) == "".join(text.split())
    # 長い行は回答の段落と同じ字数で連続した範囲に分ける。
    assert [len(unit["text"]) for unit in standard_answer_units("あ" * 1300)] == [600, 600, 100]


def test_scope_items_quoting_the_question_are_dropped_and_counted(fixed_scope):
    fixed_scope.side_effect = _prepare_standard_scope
    data = payload() | dict(question="窓口の対応が継続していても同じ扱いになりますか。", standard_answer=_SOURCE)
    # 2 番目の項目は標準回答に無い ID だけを指す（質問の文を項目にした）。
    scope = _scope_with(["S4"], ["Q1"], ["S6"])
    evaluated = output().model_copy(
        update=dict(
            coverage_checks=[
                CoverageCheck(requirement_index=1, status="addressed", answer_quote="回答"),
                CoverageCheck(requirement_index=2, status="missing", answer_quote=""),
            ]
        )
    )
    calls = []
    fake = judge(evaluated, calls=calls)

    def parse(system, prompt, settings, schema, **kwargs):
        if schema is StandardAnswerScope:
            return scope
        return fake(system, prompt, settings, schema, **kwargs)

    with patch(PARSE, side_effect=parse):
        result = evaluate_answer_payload(data, object())
    assert result["status"] == "completed"
    quotes = [item["standard_answer_quote"] for item in result["standard_answer_scope"]["requirements"]]
    assert quotes == [
        "資料Aの第1章は、窓口で申請を受け付けると定めています。",
        "文書X_規程集の規程集第5条は、申請書の受付と審査の手順を定めています。",
    ]
    assert result["judge_diagnostics"]["standard_scope_dropped"] == 1
    assert result["judge_diagnostics"]["standard_scope_retries"] == 0
    # 外した数はシステムの診断。網羅の判定のモデルには渡さない。単位の ID も渡さず、比較項目と引用の原文を渡す。
    judged = [item for inputs in calls_of(calls, CoverageOutput) for item in inputs["requirements"]]
    assert "dropped_requirements" not in json.dumps(judged, ensure_ascii=False)
    assert "dropped_requirements" not in json.dumps(StandardAnswerScope.model_json_schema())
    assert [set(item) for item in judged] == [{"standard_answer_quote", "requirement", "requirement_index"}] * 2
    assert [item["standard_answer_quote"] for item in sorted(judged, key=lambda item: item["requirement_index"])] == quotes
    stored = result["standard_answer_scope"]["requirements"]
    assert [item["standard_answer_unit_ids"] for item in stored] == [["S4"], ["S6"]]


def test_scope_with_mostly_unknown_unit_ids_is_retried_with_the_item_numbers():
    data = payload() | dict(question="窓口の対応が継続していても同じ扱いになりますか。", standard_answer=_SOURCE)
    bad = _scope_with(["Q1"], ["S4"], ["S99"])
    good = _scope_with(["S4"])
    with patch(PARSE, side_effect=[bad, good]) as parse:
        scope = _prepare_standard_scope(data, object(), None)
    assert parse.call_count == 2 and scope.dropped_requirements == 0
    assert all(call.args[0] == SCOPE_PROMPT and call.args[0].startswith("## Step 1:") for call in parse.call_args_list)
    feedback = json.loads(parse.call_args_list[1].args[1])["validation_feedback"]
    assert "1・3 番目" in feedback and "S1" in feedback and "S12" in feedback and "質問の文を項目にしない" in feedback


def test_scope_item_with_some_unknown_unit_ids_is_retried_with_the_item_number():
    # 一部の ID だけが単位に無い項目は外さず、項目の番号を指摘して出し直させる。
    data = payload() | dict(standard_answer=_SOURCE)
    bad = _scope_with(["S4"], ["S5", "S13"], ["S6"])
    good = _scope_with(["S4"], ["S5"], ["S6"])
    with patch(PARSE, side_effect=[bad, good]) as parse:
        scope = _prepare_standard_scope(data, object(), None)
    assert parse.call_count == 2 and scope.dropped_requirements == 0
    assert [item.standard_answer_unit_ids for item in scope.requirements] == [["S4"], ["S5"], ["S6"]]
    feedback = json.loads(parse.call_args_list[1].args[1])["validation_feedback"]
    assert "requirements の 2 番目" in feedback


def test_scope_schema_and_prompt_refer_to_unit_ids_only():
    from rag_engine.evaluation.answer_eval import SCOPE_PROMPT

    schema = StandardAnswerScope.model_json_schema()
    requirement = schema["$defs"]["StandardRequirement"]
    # モデルが返すのは単位の ID。引用の原文はシステムが単位から組み立てる（schema に出さない）。
    assert set(requirement["properties"]) == {"standard_answer_unit_ids", "requirement", "relevance", "relevance_reason"}
    assert requirement["required"] == ["standard_answer_unit_ids", "requirement"]
    assert requirement["properties"]["standard_answer_unit_ids"]["minItems"] == 1
    assert set(schema["properties"]) == {"requirements", "excluded_case_data"}
    # 指示は入力の単位（standard_answer_units）と schema の項目を名指しし、原文の写しを求めない。
    assert "standard_answer_units" in SCOPE_PROMPT and "standard_answer_unit_ids" in SCOPE_PROMPT
    assert "standard_answer_quote" not in SCOPE_PROMPT
    assert "質問の文を項目にしない" in SCOPE_PROMPT


def test_near_verbatim_quote_with_a_changed_word_is_stored_as_the_original_text():
    # 1 語だけ写し違えた長い引用は、原文の範囲に結び付けて原文を保存する（写し違えた語は記録に残らない）。
    quote = "資料Bの第2章は、申請は受付から14日以内に却下すると定めています。"
    assert grounded_quote(quote, _SOURCE) == "資料Bの第2章は、申請は受付から14日以内に審査すると定めています。"


def test_long_verbatim_quote_fragments_are_not_judged_as_claims():
    # 600 字を超える原文の引用の行は段落に分かれる。断片も原文の引用なので主張にしない（#3579）。
    from rag_engine.evaluation.answer_eval import _judged_passages

    labels = answer_labels()
    quote = "・「" + "条文の本文です。" * 100 + "」"
    answer = "結論です。\n\n" + labels.quote_only_section + "\n" + labels.quote_only_note + "\n" + quote + "\n根拠：law.json 第1条"
    assert [passage["text"] for passage in _judged_passages(answer)] == ["結論です。", "根拠：law.json 第1条"]


@pytest.mark.parametrize(
    "summary,status",
    [
        ("記録を識別するキーで、状態値には用いません。", "supported"),
        ("記録のキーは処理状態も示します。", "contradicted"),
        ("記録のキーは必ず自動採番されます。", "unsupported"),
    ],
)
def test_lead_summary_is_judged_against_originals_cited_by_body(summary, status):
    data = cited_payload() | dict(
        answer_text=summary
        + "\n\n"
        + answer_labels().rules_section
        + "\n\n"
        + "・識別キーは入力レコードの識別に使う。処理の状態は表さない。\n根拠：manual.pdf p.3",
        evidence_items=[
            dict(
                id="doc:p3",
                source="manual.pdf",
                page_start=3,
                page_end=3,
                text="識別キーは入力レコードの識別に使う。処理の状態は表さない。",
            ),
            dict(id="other:p1", source="other.pdf", page_start=1, page_end=1, text="識別キーは自動採番する。"),
        ],
    )
    calls = []
    evaluated = AnswerEvaluationOutput(
        coverage_checks=output().coverage_checks,
        claim_checks=[ClaimCheck(answer_quote=summary, status=status, source_id="", evidence_quote="", reason="合成の意味判定")],
    )
    with patch(PARSE, side_effect=judge(evaluated, calls=calls)):
        result = evaluate_answer_payload(data, object())
    claim = next(item for item in result["claim_checks"] if item["answer_quote"] == summary)
    assert claim["status"] == status
    assert claim["reference_check"] != REFERENCE_UNCITED
    inputs = next(item for item in calls_of(calls, ClaimJudgement) if item["passage"]["text"] == summary)
    assert [node["id"] for node in inputs["evidence_items"]] == [QUESTION_SOURCE_ID, "doc:p3"]
    links = result["judge_diagnostics"]["summary_citation_candidates"]
    assert links[0]["summary_passage_id"] == claim["answer_passage_id"]
    assert links[0]["body_passage_ids"] and links[0]["evidence_ids"] == ["doc:p3"]


def test_missing_citation_is_not_a_semantic_unsupported_verdict():
    calls = []
    with patch(PARSE, side_effect=judge(calls=calls)):
        result = evaluate_answer_payload(payload() | dict(answer_text="合成の未引用の事実。"), object())
    claim = result["claim_checks"][0]
    assert (claim["status"], claim["reference_check"], claim["audit_status"]) == ("unassessed", REFERENCE_UNCITED, "")
    assert not calls_of(calls, ClaimJudgement)


def test_summary_candidates_do_not_flow_to_uncited_body_or_warning_sections():
    summary = "合成の要約。"
    data = cited_payload() | dict(
        answer_text=summary
        + "\n\n"
        + answer_labels().rules_section
        + "\n\n"
        + "・合成の本文。\n根拠：manual.pdf p.3\n\n・別の未引用の本文。\n\n"
        + answer_labels().marked_section
        + "\n\n・参考の説明。\n根拠：manual.pdf p.9"
    )
    calls = []
    with patch(PARSE, side_effect=judge(calls=calls)):
        result = evaluate_answer_payload(data, object())
    inputs = next(item for item in calls_of(calls, ClaimJudgement) if item["passage"]["text"] == summary)
    assert [node["id"] for node in inputs["evidence_items"]] == [QUESTION_SOURCE_ID, "doc:p3"]
    uncited = next(item for item in result["claim_checks"] if item["answer_quote"] == "・別の未引用の本文。")
    assert (uncited["status"], uncited["reference_check"]) == ("unassessed", REFERENCE_UNCITED)


def test_summary_candidates_include_each_cited_body_original_without_word_matching():
    summary = "複数の根拠を組み合わせた合成の要約。"
    data = cited_payload() | dict(
        answer_text=summary
        + "\n\n"
        + answer_labels().rules_section
        + "\n\n"
        + "・合成の本文 A。\n根拠：manual.pdf p.3\n・合成の本文 B。\n根拠：manual.pdf p.9"
    )
    calls = []
    with patch(PARSE, side_effect=judge(calls=calls)):
        result = evaluate_answer_payload(data, object())
    inputs = next(item for item in calls_of(calls, ClaimJudgement) if item["passage"]["text"] == summary)
    assert [node["id"] for node in inputs["evidence_items"]] == [QUESTION_SOURCE_ID, "doc:p3", "doc:p9"]
    assert len(result["judge_diagnostics"]["summary_citation_candidates"][0]["body_passage_ids"]) == 2
