"""ID・行範囲の真正性と、意味支持の未検証を分離する合成回帰。"""

import hashlib
import json
from contextvars import copy_context

import pytest
from pydantic import ValidationError
from test_cite_lines import LONG, item
from test_grounded_answer import FakeModel, audit, context, draft, record

from rag_engine.config import get_settings
from rag_engine.generation import grounded
from rag_engine.generation.answering import _grounded_spans
from rag_engine.models.llm import GroundedCitation, GroundedDraft


def spans(text=LONG):
    return _grounded_spans("対象の操作手順は？", context(record(0, text, "対象の操作", source="sample.pdf")), (), None)


def check(proposal, evidence):
    entries, dropped = grounded.verify("対象の操作手順は？", GroundedDraft(items=[proposal], summary="", confidence="low"), evidence)
    return entries[0], dropped


def test_valid_range_binds_the_exact_span_without_a_second_text_match(monkeypatch):
    evidence = spans()
    displayed = grounded.format_functions(evidence)
    assert "2| " in displayed
    monkeypatch.setattr(grounded, "resolve_evidence", lambda *args, **kwargs: pytest.fail("提取済み原文を再照合しない"))
    entry, dropped = check(item(evidence[0]["label"], "2-3"), evidence)
    units = grounded.citation_units(evidence[0]["text"])
    assert entry.item.quote == evidence[0]["text"][units[1][0] : units[2][1]]
    proof = entry.citation_resolution
    assert proof["status"] == "extracted" and proof["authenticity"] == "verified"
    assert proof["requested_lines"] == "2-3" and proof["requested_range_status"] == "valid"
    assert proof["resolved_evidence_id"] == evidence[0]["evidence_id"]
    assert proof["source"] == "sample.pdf" and proof["source_id"] == evidence[0]["source_id"]
    assert proof["content_sha256"] == hashlib.sha256(evidence[0]["text"].encode()).hexdigest()
    assert proof["sent_view_status"] == "prepared"
    assert proof["prepared_view_sha256"] == proof["view_sha256"]
    assert entry.review_assessment == {} and not dropped


@pytest.mark.parametrize(
    "identity,lines,expected",
    [
        ("unknown", "1", "citation_id_invalid"),
        ("", "1", "citation_id_invalid"),
        (None, "", "citation_range_missing"),
        (None, "abc", "citation_range_invalid"),
        (None, "3-2", "citation_range_invalid"),
        (None, "0", "citation_range_out_of_bounds"),
        (None, "2-999", "citation_range_out_of_bounds"),
    ],
)
def test_extraction_failures_keep_the_actual_reason_in_marks_and_feedback(identity, lines, expected):
    evidence = spans()
    proposal = item(evidence[0]["label"] if identity is None else identity, lines)
    proposal = proposal.model_copy(update={"text": "資料にない別の説明"})
    entry, dropped = check(proposal, evidence)
    assert entry.citation_resolution["status"] == "failed"
    assert entry.citation_resolution["reason_code"] == expected
    assert entry.citation_resolution["requested_lines"] == lines
    assert [mark["code"] for mark in entry.marks] == [expected]
    assert dropped[0]["reason"] == entry.reasons[0]
    assert "quote がどの Evidence" not in entry.reasons[0]
    grounded.detach_unsourced([entry])
    assert entry.span is None


def test_ambiguous_id_does_not_choose_a_source_by_content():
    evidence = spans()
    evidence.append({**evidence[0], "evidence_id": "different"})
    entry, _ = check(item(evidence[0]["label"], "1"), evidence)
    assert entry.citation_resolution["reason_code"] == "citation_id_ambiguous"


def test_empty_evidence_is_extraction_failure_not_a_quote_mismatch():
    evidence = spans()
    evidence[0]["text"] = "  \n "
    entry, _ = check(item(evidence[0]["label"], "1"), evidence)
    assert entry.citation_resolution["reason_code"] == "citation_extraction_failed"


@pytest.mark.parametrize(
    "field,value", [("text", LONG + "変更された本文。"), ("source", "other.pdf"), ("label", "E9"), ("answer_context", "変更した補助情報")]
)
def test_changed_source_or_content_after_display_is_a_version_mismatch(field, value):
    evidence = spans()
    grounded.format_functions(evidence)
    identity = evidence[0]["evidence_id"]
    evidence[0][field] = value
    entry, _ = check(item(identity, "1"), evidence)
    assert entry.citation_resolution["reason_code"] == "citation_version_mismatch"
    assert entry.citation_resolution["sent_view_status"] == "mismatched"
    assert not entry.item.quote


def test_a_missing_sent_snapshot_is_not_reported_as_a_verified_sent_version():
    evidence = spans()
    entry, _ = check(item(evidence[0]["label"], "1"), evidence)
    assert entry.citation_resolution["authenticity"] == "verified"
    assert entry.citation_resolution["sent_view_status"] == "not_recorded"
    assert "sent_view_sha256" not in entry.citation_resolution


def test_forged_private_proof_cannot_turn_an_invalid_range_into_a_valid_quote():
    evidence = spans()
    proposal = item(evidence[0]["label"], "999")
    proposal.citations[0]._extraction = {"status": "extracted", "authenticity": "verified", "requested_lines": "1"}
    evidence[0]["_citation_view"] = "forged model metadata"
    entry, _ = check(proposal, evidence)
    assert entry.citation_resolution["reason_code"] == "citation_range_out_of_bounds"
    assert entry.citation_resolution["requested_lines"] == "999"
    assert entry.citation_resolution["authenticity"] == "unverified"
    with pytest.raises(ValidationError):
        GroundedCitation.model_validate({"evidence_id": "E1", "lines": "1", "_extraction": {"status": "extracted"}})
    assert "_extraction" not in GroundedCitation.model_json_schema()["properties"]


def test_display_snapshot_does_not_enter_span_json_or_model_schema():
    evidence = spans()
    before = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
    grounded.format_functions(evidence)
    assert json.dumps(evidence, ensure_ascii=False, sort_keys=True) == before
    assert "_extraction" not in item(evidence[0]["label"], "1").model_dump_json()


@pytest.mark.parametrize(
    "lines,origin,original_status", [("", "claim_inferred", "missing"), ("抽出条件を入力して抽出", "line_text", "invalid")]
)
def test_legacy_range_recovery_does_not_claim_the_original_range_was_valid(lines, origin, original_status):
    evidence = spans()
    proposal = item(evidence[0]["label"], lines).model_copy(update={"text": "抽出条件を入力して抽出ボタンを押します。"})
    cited, error = grounded.cited_quote(proposal, evidence)
    assert error == "" and cited.quote in LONG
    proof = grounded._citation_extraction(cited)
    assert proof["quote_origin"] == origin and proof["requested_range_status"] == original_status
    assert proof["authenticity"] == "verified"


def test_real_heading_quote_is_distinguished_from_missing_source():
    evidence = spans("対象の操作")
    evidence[0].update(section_path=["対象の操作"], section_path_sources=["section_header"])
    entry, _ = check(item(evidence[0]["label"]), evidence)
    assert entry.citation_resolution["reason_code"] == "citation_heading_only"
    assert entry.citation_resolution["authenticity"] == "verified"
    assert [mark["code"] for mark in entry.marks] == ["citation_heading_only"]


@pytest.mark.parametrize("review", ["not_run", "error", "unsupported"])
def test_authentic_extraction_does_not_upgrade_semantic_support(review):
    evidence = spans()
    proposal = item(evidence[0]["label"], "2")
    assessment = audit((0, "unsupported", "matched", "")) if review == "unsupported" else audit()
    model = FakeModel([draft(proposal.model_dump(), summary="")], [assessment])
    result = grounded.run_round(
        "対象の操作手順は？",
        evidence,
        get_settings(environ={}, dotenv_path=None),
        prompt=grounded.format_functions(evidence),
        image_paths=(),
        provider_id=None,
        parse_text=model,
        parse_images=lambda *args, **kwargs: pytest.fail("画像は不要"),
        audit_mode="none" if review == "not_run" else "detailed",
    )
    trace = result.trace()
    assert trace["items"][0]["citation_resolution"]["authenticity"] == "verified"
    assert trace["validation_dimensions"]["citation_validity"]["items"][0]["citations"][0]["status"] == "matched"
    semantic = trace["validation_dimensions"]["claim_support"]
    # このfixtureは主張だけを判定し、要求の判定は返さないため監査全体はpartial。
    assert semantic["execution"] == ("partial" if review == "unsupported" else review)
    assert semantic["items"][0]["status"] == "unknown"
    if review == "unsupported":
        observation = result.timing["semantic_audit_observation"]
        assert observation["adopted"] is False
        assert observation["audit"]["reviews"][0]["support"] == "unsupported"
    assert not any(mark["code"] in grounded.CITATION_EXTRACTION_CODES | {"quote_unmatched"} for mark in trace["items"][0].get("marks", []))


def test_display_snapshots_are_isolated_between_request_contexts():
    first, second = spans(), spans("別の本文の操作を行います。")
    local = copy_context()
    local.run(grounded.format_functions, first)
    grounded.format_functions(second)
    first_entry, _ = local.run(check, item(first[0]["label"], "1"), first)
    assert first_entry.citation_resolution["sent_view_status"] == "prepared"
    outside_entry, _ = check(item(first[0]["label"], "1"), first)
    assert outside_entry.citation_resolution["sent_view_status"] == "not_recorded"


def test_direct_display_id_is_not_ambiguous_because_of_another_sources_alias():
    evidence = spans()
    evidence.append({**evidence[0], "evidence_id": "other", "label": "E99", "source_id": evidence[0]["label"]})
    entry, _ = check(item(evidence[0]["label"], "1"), evidence)
    assert entry.citation_resolution["resolved_evidence_id"] == evidence[0]["evidence_id"]
    assert entry.citation_resolution["authenticity"] == "verified"


def test_a_recorded_view_cannot_restore_a_source_outside_the_current_scope():
    evidence = spans()
    grounded.format_functions(evidence)
    entry, _ = check(item(evidence[0]["label"], "1"), [])
    assert entry.citation_resolution["reason_code"] == "citation_id_invalid"
    assert entry.span is None


def test_old_private_proof_cannot_bypass_the_legacy_quote_match(monkeypatch):
    evidence = spans()
    proposal = item(evidence[0]["label"], quote="原文にない引用")
    proposal.citations[0]._extraction = {"status": "extracted", "authenticity": "verified"}
    called = []

    def resolve(*args, **kwargs):
        called.append(True)
        return None, "旧形式の引用を照合できません。"

    monkeypatch.setattr(grounded, "resolve_evidence", resolve)
    entry, _ = check(proposal, evidence)
    assert called and entry.unmatched
    assert entry.citation_resolution == {}


def test_width_normalization_in_the_display_does_not_change_raw_source_authenticity():
    evidence = spans()
    evidence[0]["text"] = "ＡＢＣ：" + LONG
    grounded.format_functions(evidence)
    source_hash = hashlib.sha256(evidence[0]["text"].encode()).hexdigest()
    entry, _ = check(item(evidence[0]["label"], "1"), evidence)
    assert entry.citation_resolution["content_sha256"] == source_hash
    assert entry.citation_resolution["sent_view_status"] == "prepared"
    assert entry.citation_resolution["authenticity"] == "verified"
    assert entry.citation_resolution["resolved_quote"].startswith("ＡＢＣ：")


def test_dropped_secondary_range_keeps_the_original_id_and_range_diagnostics():
    evidence = spans()
    proposal = item(evidence[0]["label"], "1")
    proposal = proposal.model_copy(
        update={"citations": [*proposal.citations, GroundedCitation(evidence_id=evidence[0]["label"], lines="999")]}
    )
    entry, dropped = check(proposal, evidence)
    assert not entry.marks and entry.citation_resolution["authenticity"] == "verified"
    [failure] = [note for note in dropped if note.get("citation_dropped")]
    assert failure["citation_resolution"]["requested_evidence_id"] == evidence[0]["label"]
    assert failure["citation_resolution"]["requested_lines"] == "999"
    assert failure["citation_resolution"]["reason_code"] == "citation_range_out_of_bounds"


def test_identical_quotes_from_multiple_sources_remain_bound_to_the_requested_ids():
    evidence = _grounded_spans(
        "対象の操作手順は？",
        context(record(0, LONG, "対象の操作", source="a.pdf"), record(1, LONG, "対象の操作", source="b.pdf")),
        (),
        None,
    )
    proposal = item(evidence[0]["label"], "1")
    proposal = proposal.model_copy(
        update={"citations": [*proposal.citations, GroundedCitation(evidence_id=evidence[1]["label"], lines="1")]}
    )
    entry, dropped = check(proposal, evidence)
    assert {entry.citation_resolution["resolved_evidence_id"], entry.also[0].citation_resolution["resolved_evidence_id"]} == {
        span["evidence_id"] for span in evidence
    }
    assert not dropped
