"""言い換えた引用の再照合と本文の根拠ラベル（#3064・#3065）。"""

import json

import pytest
from test_grounded_answer import FakeModel, audit, context, draft, item, record

from rag_engine.config import get_settings
from rag_engine.dependencies import AnswerDependencies, bind_dependencies
from rag_engine.generation import grounded
from rag_engine.generation.answering import _grounded_spans, synthesize_grounded_answer
from rag_engine.models.llm import GroundedAudit, GroundedDraft

QUESTION = "一覧から対象を送信する方法"
ORIGINAL = "一覧画面を開き、対象の行を選択して送信ボタンを押します。"
PARAPHRASE = "一覧画面で対象の行を選択し、送信ボタンを押します。"


def _spans():
    return _grounded_spans(QUESTION, context(record(0, ORIGINAL, "一覧画面", page=4, source="手順書 A.pdf")), (), None)


def _run(*, quote=ORIGINAL, support="supported", mode="standard", suffix="", evidence_id="E1"):
    spans = _spans()
    generated = draft(item(evidence_id, PARAPHRASE + suffix, "対象を決めて提出してください。"), summary="操作の説明（E1）")
    response = audit((0, support, "matched", ""), requests=[("Q1", "addressed", "")])
    if mode == "standard":
        response.pop("unused_evidence_ids")
    response["reviews"][0]["support_quote"] = quote
    model = FakeModel([generated], [response])
    result = grounded.run_round(
        QUESTION,
        spans,
        get_settings(),
        prompt="合成の質問",
        image_paths=[],
        provider_id=None,
        parse_text=model,
        parse_images=model,
        audit_mode=mode,
    )
    return result, model, spans


@pytest.mark.parametrize("mode", ["standard", "detailed"])
def test_uncalibrated_original_does_not_replace_the_actual_quote(mode):
    result, model, spans = _run(mode=mode, suffix="（E1）", evidence_id="")
    entry = next(e for e in result.checked if e.draft_index == 0)
    assert not entry.confirmed and entry.quote_match != "audited_paraphrase"
    assert entry.item.quote == "対象を決めて提出してください。"
    assert entry.item.text == PARAPHRASE
    # 引用原文の不一致という基本検査は残る。
    assert any(mark["code"] == "quote_unmatched" for mark in entry.marks)
    assert grounded.detach_unsourced(result.checked) == 1
    inputs = json.loads(model.prompts[-1][1])
    assert inputs["items"][0]["quote_unmatched"] is True
    assert "当該 item が実際に引用した citations の原文集合と照合する" in grounded.audit_system_prompt(detailed=mode == "detailed")
    assert result.timing["procedure_quotes"]["paraphrase_verified"] == 0
    assert not result.dropped[0].get("ignored", False)
    observed = result.timing["semantic_audit_observation"]["audit"]
    assert observed["reviews"][0]["support_quote"] == ORIGINAL
    assert result.checked[0].review_assessment["effective_support"] == "unknown"
    assert "E1" not in result.summary
    assert len(model.prompts) == 2
    assert entry.span is None
    assert result.dropped[0]["evidence_id"] == spans[0]["label"]


@pytest.mark.parametrize(
    "support,quote",
    [
        ("unsupported", ORIGINAL),
        ("supported", "資料に無い操作です。"),
        ("supported", ""),
    ],
)
def test_unverified_claim_or_invalid_original_keeps_marks(support, quote):
    result, _, _ = _run(support=support, quote=quote)
    entry = next(e for e in result.checked if e.draft_index == 0)
    assert not entry.confirmed
    assert any(mark["code"] == "quote_unmatched" for mark in entry.marks)


def test_no_audit_does_not_promote_a_paraphrase():
    result, model, _ = _run(mode="none", suffix="（E1）")
    entry = next(e for e in result.checked if e.draft_index == 0)
    assert not entry.confirmed
    assert "E1" not in entry.item.text
    assert len(model.prompts) == 1


@pytest.mark.parametrize("suffix", ["（E1）", "(E1)", "[E1]", "【E1】", "（E1、E2）"])
def test_inline_labels_are_not_published(suffix):
    spans = _spans()
    generated = GroundedDraft.model_validate(draft(item("E1", PARAPHRASE + suffix, ORIGINAL)))
    checked, _ = grounded.verify(QUESTION, generated, spans)
    assert checked[0].item.text == PARAPHRASE
    assert checked[0].span is not None


def test_a_real_document_code_is_not_an_internal_label():
    spans = [{"text": "エラーコード E6（E6）は接続エラーです。", "evidence_id": "a", "label": "E1"}]
    assert grounded._clean_evidence_labels("エラーコード E6（E6）を確認します。（E1）", spans) == "エラーコード E6（E6）を確認します。"
    assert grounded._clean_evidence_labels("コード E60 を確認します。E6", [{"text": "コード E60"}]) == "コード E60 を確認します。"


@pytest.mark.parametrize("source", ["コード E6", "コード Ｅ６", "コード（Ｅ６）"])
@pytest.mark.parametrize("text", ["コード E6 を確認します。", "コード（E6）を確認します。", "コード [E6] を確認します。"])
def test_document_codes_survive_different_brackets_and_character_widths(source, text):
    spans = [{"text": source, "evidence_id": "document", "label": "E1"}]
    assert grounded._clean_evidence_labels(text + "（E1）", spans) == text
    cleaned = grounded._inline_evidence(GroundedDraft.model_validate(draft(item("", text, ""))).items[0], spans)
    assert cleaned.text == text
    assert cleaned.evidence_id == ""


def test_mixed_bracketed_codes_keep_real_codes_and_resolve_only_internal_labels():
    spans = [{"text": "コード Ｅ６", "evidence_id": "document", "label": "E1"}]
    cleaned = grounded._inline_evidence(GroundedDraft.model_validate(draft(item("", "コード（E6、E1）を確認します。", ""))).items[0], spans)
    assert cleaned.text == "コード（E6）を確認します。"
    assert cleaned.evidence_id == "E1"


def test_observed_recovery_keeps_body_without_promoting_an_invalid_quote_to_a_source():
    _, model, _ = _run(mode="detailed", suffix="（E1）", evidence_id="")

    def unused(*args, **kwargs):
        raise AssertionError("外部 I/O は使わない")

    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        result = synthesize_grounded_answer(
            QUESTION,
            context(record(0, ORIGINAL, "一覧画面", page=4, source="手順書 A.pdf")),
            get_settings(),
            image_prompt_mode="text_only",
        ).response
    assert PARAPHRASE in result.answer_text
    assert "E1" not in result.answer_text
    assert result.generation_trace["citation_lines"] == []
    assert result.generation_trace["procedure_quotes"]["paraphrase_verified"] == 0
    assert any(reason["code"] == "quote_unmatched" for mark in result.envelope["passage_marks"] for reason in mark["reasons"])
    assert result.generation_trace["semantic_audit_observation"]["audit"]["reviews"][0]["support_quote"] == ORIGINAL


def test_recovery_keeps_other_marks_and_rejects_duplicate_reviews():
    result, _, spans = _run(mode="none")
    entry = next(e for e in result.checked if e.draft_index == 0)
    entry.mark("unsupported", "別の未確認の主張")
    response = audit((0, "supported", "matched", ""))
    response["reviews"][0]["support_quote"] = ORIGINAL
    duplicate = GroundedAudit.model_validate({**response, "reviews": response["reviews"] * 2})
    assert not grounded.recover_audited_quotes(QUESTION, result.checked, duplicate, spans)
    grounded.recover_audited_quotes(QUESTION, result.checked, GroundedAudit.model_validate(response), spans)
    assert any(mark["code"] == "unsupported" for mark in entry.marks)
    assert not entry.confirmed
