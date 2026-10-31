"""同次監査の局所的支持を、元の説明と実引用の範囲に限って保持する。"""

from dataclasses import replace

import pytest
from test_grounded_answer import FakeModel, audit, context, draft, ids, item, observed_audit, record

from rag_engine.config import get_settings
from rag_engine.dependencies import AnswerDependencies, bind_dependencies
from rag_engine.generation import grounded
from rag_engine.generation.answering import synthesize_grounded_answer
from rag_engine.models.llm import GroundedAudit, GroundedCitation, GroundedItem

OPEN = "対象画面を開きます。"
BAD = "内容を保存します。"
SOURCE = OPEN + "内容は保存しません。"
COMPOUND = OPEN + BAD


def checked():
    return grounded.CheckedItem(
        GroundedItem(kind="operation", text=COMPOUND, citations=[GroundedCitation(evidence_id="E1", quote=SOURCE)]),
        span={"evidence_id": "E1", "label": "E1", "text": SOURCE},
        citation_resolution={"authenticity": "verified", "method": "exact"},
    )


def fragment(text=OPEN, quote=OPEN, evidence_id="E1", support="supported"):
    return {"text": text, "support": support, "citations": [{"evidence_id": evidence_id, "quote": quote}]}


def apply(entry, fragments, *, support="contradicted"):
    verdict = GroundedAudit.model_validate(
        {
            "summary_supported": False,
            "goal_alignment": "aligned",
            "reviews": [
                {
                    "index": 0,
                    "support": support,
                    "applicability": "matched",
                    "condition": "",
                    "reason": "後半の保存は原文の禁止と矛盾",
                    "supported_fragments": fragments,
                }
            ],
        }
    )
    grounded.apply_audit([entry], verdict)


def test_supported_original_fragment_survives_without_reindexing():
    entry = checked()
    original_proof = dict(entry.citation_resolution)
    apply(entry, [fragment()])
    assert entry.item.text == OPEN
    assert entry.confirmed and not entry.removed
    assert entry.review_assessment["support"] == "contradicted"
    assert entry.review_assessment["effective_support"] == "supported"
    salvage = entry.review_assessment["partial_salvage"]
    assert salvage["original_text"] == COMPOUND and salvage["retained_text"] == OPEN
    assert salvage["original_support"] == "contradicted"
    assert salvage["retained_sources"] == [{"evidence_id": "E1", "quote": SOURCE}]
    assert entry.citation_resolution == original_proof
    assert entry.item.quote == SOURCE  # 抽出provenanceが表す実引用自体は変更しない。


@pytest.mark.parametrize(
    "piece",
    [
        fragment(text="対象画面へ進みます。"),
        fragment(evidence_id="E2"),
        fragment(quote="存在しない引用"),
        fragment(support="unknown"),
        fragment(text=COMPOUND),
    ],
)
def test_invented_fragment_or_nonactual_citation_cannot_restore_claim(piece):
    entry = checked()
    apply(entry, [piece])
    assert entry.removed
    assert entry.item.text == COMPOUND
    assert entry.review_assessment["effective_support"] == "contradicted"


def test_same_span_but_unquoted_text_cannot_be_used_as_support():
    entry = checked()
    entry.item = entry.item.model_copy(update={"citations": [GroundedCitation(evidence_id="E1", quote="内容は保存しません。")]})
    apply(entry, [fragment()])
    assert entry.removed


def test_whitespace_quote_does_not_prove_a_fragment_even_when_present():
    entry = checked()
    entry.item = entry.item.model_copy(update={"citations": [GroundedCitation(evidence_id="E1", quote=SOURCE + " ")]})
    entry.span = {**entry.span, "text": SOURCE + " "}
    apply(entry, [fragment(quote=" ")])
    assert entry.removed


def test_unknown_overall_review_does_not_promote_a_fragment():
    entry = checked()
    apply(entry, [fragment()], support="unknown")
    assert entry.item.text == COMPOUND
    assert entry.review_assessment["effective_support"] == "unknown"
    assert entry.state and not entry.confirmed


def test_unknown_extraction_does_not_become_a_supported_fragment():
    entry = checked()
    entry.citation_resolution["authenticity"] = "unverified"
    apply(entry, [fragment()])
    assert entry.removed


def test_one_fragment_may_be_jointly_supported_by_two_actual_citations():
    entry = checked()
    first = "対象画面を開き、"
    second = "内容を確認します。"
    entry.item = GroundedItem(kind="operation", text=first + second + BAD, citations=[GroundedCitation(evidence_id="E1", quote=first)])
    entry.span = {"evidence_id": "E1", "text": first}
    other = grounded.CheckedItem(
        GroundedItem(kind="operation", text=entry.item.text, citations=[GroundedCitation(evidence_id="E2", quote=second)]),
        span={"evidence_id": "E2", "text": second},
    )
    entry.also = [other]
    piece = {
        "text": first + second,
        "support": "supported",
        "citations": [{"evidence_id": "E1", "quote": first}, {"evidence_id": "E2", "quote": second}],
    }
    apply(entry, [piece])
    assert entry.item.text == first + second
    assert {value.item.evidence_id for value in grounded.item_citations(entry)} == {"E1", "E2"}
    assert entry.confirmed


def test_uncalibrated_partial_contradiction_keeps_original_body_and_summary():
    question = "対象画面を開く操作と保存の可否は？"
    ctx = context(record(0, SOURCE, "設定", source="操作説明.pdf"))
    eid = ids(ctx, question)[SOURCE]
    verdict = audit((0, "contradicted", "matched", ""), requests=[("Q1", "partial")], summary_supported=True)
    verdict["reviews"][0]["supported_fragments"] = [fragment(evidence_id=eid)]
    model = FakeModel([draft(item(eid, COMPOUND, SOURCE, request_id="Q1"), summary=COMPOUND)], [verdict])

    def unused(*args, **kwargs):
        raise AssertionError("外部I/Oは不要")

    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        response = synthesize_grounded_answer(
            question,
            ctx,
            replace(get_settings(environ={}, dotenv_path=None), answer_max_correction_rounds=0),
            image_prompt_mode="text_only",
        ).response
    assert COMPOUND in response.answer_text
    assert response.answer_text.startswith(COMPOUND)
    assert len([name for name, _prompt in model.prompts if name in {"GroundedAudit", "GroundedFactAudit"}]) == 1
    trace = response.generation_trace["rounds"][0]
    assert trace["draft"]["items"][0]["text"] == COMPOUND
    assert "partial_salvage" not in trace["items"][0]["review_assessment"]
    assert trace["items"][0]["review_assessment"]["effective_support"] == "unknown"
    raw = observed_audit(response)["reviews"][0]
    assert raw["support"] == "contradicted"
    assert raw["supported_fragments"][0]["text"] == OPEN
    assert not response.envelope["removed_passages"]
