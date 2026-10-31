"""レイアウト・表記の違いと出力指示を資料の不足にしない。"""

import pytest

from rag_engine.generation.value_grounding import unsupported_document_value_claim
from rag_engine.retrieval.task_contract import task_contract


@pytest.mark.parametrize("value", ["1万4千", "1万4000", "14,000"])
def test_mixed_numbers_match_source(value):
    assert (
        unsupported_document_value_claim(f"上限は{value}円です。", [{"origin": "document_text", "text": "上限は一万四千円です。"}], "")
        == ""
    )


def test_wrong_mixed_number_still_warns():
    assert unsupported_document_value_claim("上限は1万5千円です。", [{"origin": "document_text", "text": "上限は一万四千円です。"}], "")


def test_layout_gap_matches_source():
    assert (
        unsupported_document_value_claim("上限は48000件です。", [{"origin": "document_text", "text": "上限は四万八 千件です。"}], "") == ""
    )


@pytest.mark.parametrize(
    "instruction",
    [
        "資料にない値は不明と明記してください。",
        "確認できなければ分からないと書いてください。",
        "答えは不明と表記してください。",
        "数字と万・千の混ざった表記で答えてください。",
        "添付を読んで回答してください。",
        "項目と値を出力の項目に記録してください。",
    ],
)
def test_output_instruction_is_not_a_document_request(instruction):
    units = task_contract("上限と期限を教えてください。" + instruction)["request_units"]
    assert units[0]["kind"] == "request"
    assert units[-1]["kind"] == "context"
    assert instruction in units[-1]["text"]
    assert not any(unit.get("request_id") == units[-1]["id"] for unit in units)


def test_factual_question_about_writing_is_kept():
    assert task_contract("記載方法を教えてください。")["request_units"][0]["kind"] == "request"


def test_question_about_a_written_value_is_still_factual():
    assert task_contract("資料に不明と明記されていますか？")["request_units"][0]["kind"] == "request"


@pytest.mark.parametrize(
    "question",
    [
        "不明と明記する方法を教えてください。",
        "出力の項目に記録する方法を教えてください。",
        "どの形式で回答されていますか？",
        "添付を読んで回答する手順を教えてください。",
    ],
)
def test_question_about_an_answer_instruction_remains_factual(question):
    assert task_contract(question)["request_units"][0]["kind"] == "request"


@pytest.mark.parametrize(
    "value, instruction",
    [
        ("2万4千", "数字と万・千の混ざった表記で答えてください。"),
        ("24000", "半角数字で答えてください。"),
        ("24,000", "桁区切り付きの数字で答えてください。"),
        ("二万四千", "漢数字で答えてください。"),
    ],
)
def test_requested_number_spelling_preserves_source_and_grounding(value, instruction):
    from test_grounded_answer import FakeModel, audit, context, draft, record, run

    from rag_engine.generation.answering import _grounded_spans

    question = "上限は何件ですか。" + instruction
    source = "保存件数の上限は二万四千件です。"
    ctx = context(record(0, source, "設定", page=2, source="document-a.pdf"))
    evidence_id = _grounded_spans(question, ctx, (), None)[0]["evidence_id"]
    text = f"保存件数の上限は{value}件です。"
    model = FakeModel(
        [
            draft(
                {"kind": "rule", "request_id": "Q1", "text": text, "citations": [{"evidence_id": evidence_id, "quote": source}]},
                summary=text,
            )
        ],
        [audit((0, "supported", "matched", ""), requests=[("Q1", "addressed")])],
    )
    result = run(model, ctx, question).response
    assert text in result.answer_text
    assert not result.envelope["passage_marks"]
    assert source == ctx.records[0].text
    generation_prompt = next(prompt for schema, prompt in model.prompts if schema == "GroundedDraftWire")
    assert instruction in generation_prompt
    assert '"kind": "context"' in generation_prompt


@pytest.mark.parametrize("two_values", [False, True])
def test_factual_sentences_classified_as_titles_keep_their_sources(two_values):
    """解析が事実の文を Title としても、原文照合済みの文の出典を失わない（#3599）。"""
    from dataclasses import replace

    from test_grounded_answer import FakeModel, context, draft, ids, record

    from rag_engine.config import get_settings
    from rag_engine.dependencies import AnswerDependencies, bind_dependencies
    from rag_engine.generation import grounded
    from rag_engine.generation.answering import synthesize_grounded_answer

    first = "モードAの待機時間は45秒です。"
    second = "モードBの待機時間は75秒です。"
    texts = [first, second] if two_values else [first]
    question = "各モードの待機時間を教えてください。" if two_values else "モードAの待機時間を教えてください。"
    ctx = context(*(record(i, text, text, source="document-a.txt") for i, text in enumerate(texts)))
    labels = ids(ctx, question)
    items = [
        {"kind": "rule", "request_id": "Q1", "text": text, "citations": [{"evidence_id": labels[text], "quote": text}]} for text in texts
    ]
    model = FakeModel([draft(*items, summary="")], [])
    unused = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected I/O"))  # noqa: E731
    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        result = synthesize_grounded_answer(
            question,
            ctx,
            replace(get_settings(), answer_audit_mode="none"),
            image_prompt_mode="text_only",
        ).response
    assert result.envelope["outcome"] == "answered"
    assert not result.envelope["passage_marks"]
    assert len(result.evidence_facts) == len(texts)
    assert result.generation_trace["procedure_quotes"]["unmatched"] == 0
    assert not result.needs_human_review
    assert all(text in result.answer_text for text in texts)
    assert all(fact["quote"] in texts for fact in result.evidence_facts)
    assert len([line for line in result.answer_text.splitlines() if line.startswith(grounded.answer_labels().citation_prefix)]) >= 1

    # 生成の出典をそのまま品質評価へ渡す。出典なしとしてモデルを呼ばず 0 点にする経路へ落ちない。
    from unittest.mock import patch

    from judge_stub import judge

    from rag_engine.evaluation.answer_eval import (
        AnswerEvaluationOutput,
        ClaimCheck,
        CoverageCheck,
        StandardAnswerScope,
        StandardRequirement,
        evaluate_answer_payload,
    )
    from rag_engine.generation.answering import answer_evidence_items

    judged = AnswerEvaluationOutput(
        claim_checks=[
            ClaimCheck(answer_quote=text, status="supported", source_id="", evidence_quote="", reason="合成の支持判定") for text in texts
        ],
        coverage_checks=[CoverageCheck(requirement_index=1, status="addressed", answer_quote=first)],
    )
    scope = StandardAnswerScope(
        excluded_case_data=[],
        requirements=[
            StandardRequirement(
                standard_answer_unit_ids=["S1"],
                standard_answer_quote=first,
                requirement="モードAの待機時間",
            )
        ],
    )
    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=judge(judged)):
        evaluated = evaluate_answer_payload(
            {
                "question": question,
                "standard_answer": "\n".join(texts),
                "answer_text": result.answer_text,
                "evidence_items": answer_evidence_items(ctx.records),
                "cited_evidence": list(result.cited_evidence),
            },
            object(),
            standard_scope=scope,
        )
    assert evaluated["status"] == "completed"
    claims = [claim for claim in evaluated["claim_checks"] if claim["status"] != "not_a_claim"]
    assert len(claims) == len(texts) and all(claim["status"] == "supported" for claim in claims)
    assert evaluated["judge_diagnostics"]["uncited_claims"] == 0
    assert evaluated["judge_diagnostics"]["claim_calls"] == len(texts)


@pytest.mark.parametrize("sources", [None, [], ["promoted_text"], ["running_head"], ["route_caption"], ["unknown"]])
def test_heading_path_without_confirmed_origin_does_not_invalidate_a_verbatim_quote(sources):
    from rag_engine.generation import grounded
    from rag_engine.models.llm import GroundedItem

    text = "モードAの待機時間は45秒です。"
    span = {"evidence_id": "E1", "text": text, "section_path": [text]}
    if sources is not None:
        span["section_path_sources"] = sources
    item = GroundedItem.model_validate({"kind": "rule", "text": text, "citations": [{"evidence_id": "E1", "quote": text}]})
    found, quote = grounded.resolve_evidence(item, [span])
    assert found is span and quote == text
    assert not grounded.heading_only_quote(quote, span)


def test_partial_heading_provenance_is_not_guessed():
    from rag_engine.generation import grounded

    span = {"section_path": ["文書A", "モードAの待機時間は45秒です。"], "section_path_sources": ["title"]}
    assert not grounded.heading_only_quote(span["section_path"][1], span)
