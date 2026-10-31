"""同次監査の意味判定が、生成時の引用範囲を拡大しない境界。実サービス呼出0。"""

import json

from audit_fixtures import legacy_audit_result

from rag_engine.config import get_settings
from rag_engine.generation import grounded
from rag_engine.models.audit_wire import is_audit_wire
from rag_engine.models.llm import GroundedAudit, GroundedCitation, GroundedDraft, GroundedItem

QUESTION = "対象の設定を保存する方法は？"
OPEN = "設定画面を開きます。"
SAVE = "内容を保存します。"
FIRST = OPEN + "画面についての説明が続きます。" * 8
SECOND = SAVE + "操作についての説明が続きます。" * 8


def evidence(identity="e1", label="E1"):
    span = {
        "evidence_id": identity,
        "label": label,
        "source_id": identity,
        "source": "sample.pdf",
        "function": ("sample.pdf", "合成操作"),
        "text": FIRST + "\n" + SECOND,
    }
    assert len(grounded.citation_units(span["text"])) == 2
    grounded.format_functions([span])
    return span


class Model:
    def __init__(self, proposal, *, support, support_quote=""):
        self.proposal = proposal
        self.support = support
        self.support_quote = support_quote
        self.inputs = []

    def __call__(self, system, prompt, settings, schema, **options):
        observed = schema.canonical_schema if is_audit_wire(schema) else schema
        self.inputs.append((observed.__name__, prompt))
        if issubclass(schema, GroundedDraft):
            return GroundedDraft(items=[self.proposal], summary="", confidence="low")
        assert observed.__name__ == "GroundedFactAudit"
        parse_fixture = (lambda value: legacy_audit_result(value, schema)) if is_audit_wire(schema) else schema.model_validate
        return parse_fixture(
            {
                "reviews": [
                    {
                        "index": 0,
                        "support": self.support,
                        "support_quote": self.support_quote,
                        "applicability": "matched",
                        "reason": "合成の同次観察",
                    }
                ],
                "summary_supported": True,
                "goal_alignment": "aligned",
            }
        )


def run(proposal, *, support, support_quote=""):
    spans = [evidence()]
    model = Model(proposal, support=support, support_quote=support_quote)
    result = grounded.run_round(
        QUESTION,
        spans,
        get_settings(environ={}, dotenv_path=None),
        prompt="合成の質問",
        image_paths=[],
        provider_id=None,
        parse_text=model,
        parse_images=model,
        audit_mode="standard",
    )
    assert len(model.inputs) == 2
    assert model.inputs[0][0] in {"GroundedDraft", "GroundedDraftWire"}
    assert model.inputs[1][0] == "GroundedFactAudit"
    return next(row for row in result.checked if row.draft_index == 0), result, model


def proposal(*, lines="", quote="", text=SAVE):
    return GroundedItem(
        kind="operation", text=text, applies="matched", citations=[GroundedCitation(evidence_id="e1", lines=lines, quote=quote)]
    )


def test_supported_audit_cannot_recover_an_invalid_explicit_range_from_other_lines():
    entry, result, _model = run(proposal(lines="999-999"), support="supported", support_quote=SECOND)
    assert entry.item.text == SAVE and entry.published
    assert entry.item.lines == "999-999" and not entry.item.quote
    assert entry.citation_resolution["requested_lines"] == "999-999"
    assert entry.citation_resolution["reason_code"] == "citation_range_out_of_bounds"
    assert entry.review_assessment["observed_support"] == "supported"
    assert entry.review_assessment["effective_support"] == "unknown"
    assert not entry.confirmed
    assert any(mark["code"] == "citation_range_out_of_bounds" for mark in entry.marks)
    assert result.dropped and not any(row.get("ignored") for row in result.dropped)


def test_unsupported_claim_cannot_use_uncited_second_line_as_its_original():
    entry, result, model = run(proposal(lines="1"), support="unsupported", support_quote=SECOND)
    assert entry.item.text == SAVE and entry.published
    assert result.verified == 0
    assert entry.item.lines == "1" and entry.item.quote == FIRST
    assert entry.citation_resolution["requested_lines"] == "1"
    assert entry.citation_resolution["requested_range_status"] == "valid"
    assert entry.review_assessment["observed_support"] == "unsupported"
    assert entry.review_assessment["effective_support"] == "unknown"
    assert not any(mark["code"] == "unsupported" for mark in entry.marks)
    inputs = json.loads(model.inputs[-1][1])
    assert inputs["items"][0]["quote"] == FIRST


def test_untrusted_audit_does_not_recover_legacy_free_quote_from_other_text():
    entry, _result, _model = run(proposal(quote="設定内容を記録する操作です。"), support="supported", support_quote=SECOND)
    assert entry.item.text == SAVE and entry.published and not entry.confirmed
    assert entry.item.quote != SECOND and entry.item.lines == ""
    assert entry.unmatched
    assert any(mark["code"] == "quote_unmatched" for mark in entry.marks)
    assert entry.review_assessment["observed_support"] == "supported"
    assert entry.review_assessment["effective_support"] == "unknown"


def test_valid_secondary_citation_retains_its_own_range_provenance_and_marks():
    first, second = evidence(), evidence("e2", "E2")
    proposal_item = GroundedItem(
        kind="operation",
        text=OPEN,
        applies="matched",
        citations=[GroundedCitation(evidence_id="e1", lines="999-999"), GroundedCitation(evidence_id="e2", lines="1")],
    )
    checked, dropped = grounded.verify(QUESTION, GroundedDraft(items=[proposal_item], summary="", confidence="low"), [first, second])
    assert len(checked) == 1
    entry = checked[0]
    assert entry.span["evidence_id"] == "e2"
    assert entry.item.evidence_id == "e2" and entry.item.lines == "1" and entry.item.quote == FIRST
    assert entry.citation_resolution["resolved_evidence_id"] == "e2"
    assert entry.citation_resolution["requested_lines"] == "1" and entry.citation_resolution["requested_range_status"] == "valid"
    assert not any(mark["code"] == "citation_range_out_of_bounds" for mark in entry.marks)
    assert any(row.get("reason_code") == "citation_range_out_of_bounds" or "999" in str(row) for row in dropped)


def test_audited_secondary_promotion_preserves_its_source_and_range_provenance():
    first, second = evidence(), evidence("e2", "E2")
    proposal_item = GroundedItem(
        kind="operation",
        text=SAVE,
        applies="matched",
        citations=[GroundedCitation(evidence_id="e1", lines="1"), GroundedCitation(evidence_id="e2", lines="2")],
    )
    checked, dropped = grounded.verify(QUESTION, GroundedDraft(items=[proposal_item], summary="", confidence="low"), [first, second])
    assert len(checked) == 1 and len(checked[0].also) == 1
    entry, secondary = checked[0], checked[0].also[0]
    assessment = GroundedAudit.model_validate(
        {
            "reviews": [
                {"index": 0, "support": "unsupported", "applicability": "matched", "reason": "最初の引用は画面の表示だけ"},
                {"index": 1, "support": "supported", "applicability": "matched", "reason": "別の実引用が保存操作を支持"},
            ],
            "summary_supported": True,
            "goal_alignment": "aligned",
        }
    )
    accepted = grounded.settle_citation_reviews(checked, assessment, [(1, entry, secondary)], dropped)
    grounded.apply_audit(checked, accepted)
    assert entry.item.text == SAVE and entry.published and entry.confirmed
    assert entry.span["evidence_id"] == "e2" and entry.item.evidence_id == "e2"
    assert entry.item.lines == "2" and entry.item.quote == SECOND
    assert entry.citation_resolution["resolved_evidence_id"] == "e2"
    assert entry.citation_resolution["requested_lines"] == "2"
    assert entry.citation_resolution["content_sha256"] == secondary.citation_resolution["content_sha256"]
    assert entry.unmatched is secondary.unmatched is False
