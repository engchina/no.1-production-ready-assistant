"""区切り線の真正性と、説明を支える引用としての可用性を分離する。"""

from typing import Any

import pytest
from rag_parser_core.language import bind_pack, builtin_pack, resolve

from rag_engine.generation import grounded, passage_marks
from rag_engine.models.llm import GroundedAudit, GroundedCitation, GroundedDraft, GroundedItem

BODY = "対象を選択し、内容を入力して実行します。"


def evidence(text: str, label: str = "E1") -> dict[str, Any]:
    return {
        "evidence_id": f"stable-{label}",
        "label": label,
        "source_id": f"parent-{label}",
        "source": "synthetic.pdf",
        "function": ("synthetic.pdf", "合成操作"),
        "text": text,
    }


def check(text: str, *, lines: str = "1", interval: tuple[int | None, int | None] | None = (1, 1)) -> tuple[Any, list[dict[str, Any]]]:
    span = evidence(text)
    grounded.format_functions([span])
    citation = GroundedCitation(evidence_id="E1", lines=lines)
    citation._wire_range = interval
    proposal = GroundedItem(kind="operation", text=BODY, citations=[citation])
    checked, dropped = grounded.verify("操作方法は？", GroundedDraft(items=[proposal], summary="", confidence="low"), [span])
    return next(row for row in checked if row.item.text == BODY), dropped


@pytest.mark.parametrize("text", ["=" * 99, "===", "---", "***", "___", "|---|---|", "```", "===\n---\n***", "＝＝＝"])
def test_structural_only_quotes_keep_authenticity_but_are_unusable(text: str) -> None:
    entry, dropped = check(text)
    assert entry.published and not entry.confirmed and not entry.removed
    assert entry.quote_match == "exact"
    proof = entry.citation_resolution
    assert proof["status"] == "extracted" and proof["authenticity"] == "verified"
    assert proof["content_usability"] == "unusable"
    assert proof["reason_code"] == "citation_content_unusable"
    assert proof["requested_evidence_id"] == "E1" and proof["resolved_quote"]
    assert [mark["code"] for mark in entry.marks] == ["citation_content_unusable"]
    assert dropped[0]["kept"] and dropped[0]["citation_resolution"] == proof
    assert grounded.detach_unsourced([entry]) == 1
    assert entry.span is None and entry.item.text == BODY
    assert entry.citation_resolution == proof
    rendered = grounded.render_answer("", [entry])
    assert BODY in rendered.text
    assert "原文が根拠に見当たらない" not in rendered.text
    assert "資料が不足" not in rendered.text


def test_valid_integer_range_may_extract_only_a_separator_from_a_mixed_source() -> None:
    entry, _ = check("=" * 99 + "\n" + "後続する別の説明" * 40)
    assert entry.item.quote == "=" * 99
    assert entry.citation_resolution["requested_range_status"] == "valid"
    assert entry.citation_resolution["content_usability"] == "unusable"
    assert not entry.confirmed


@pytest.mark.parametrize("text", ["可", "0", "12", "=", "==", ">", "○", "→", "≠", "1 + 2 = 3", "a === b", "されます。\n---", BODY])
def test_short_content_and_meaningful_symbols_are_not_rejected(text: str) -> None:
    entry, dropped = check(text)
    assert entry.published and entry.quote_match == "exact"
    assert entry.citation_resolution["authenticity"] == "verified"
    assert entry.citation_resolution.get("content_usability") != "unusable"
    assert not any(mark["code"] == "citation_content_unusable" for mark in entry.marks)
    assert not dropped
    # 基本照合は内容の意味支持を証明しない。
    assert entry.review_assessment == {}


@pytest.mark.parametrize("applies", ["matched", "unverified"])
def test_valid_secondary_citation_is_preserved(applies: str) -> None:
    spans = [evidence("=" * 99), evidence(BODY, "E2")]
    grounded.format_functions(spans)
    proposal = GroundedItem.model_validate(
        {
            "kind": "operation",
            "text": BODY,
            "applies": applies,
            "citations": [{"evidence_id": "E1", "lines": "1"}, {"evidence_id": "E2", "lines": "1"}],
        }
    )
    checked, dropped = grounded.verify("操作方法は？", GroundedDraft(items=[proposal], summary="", confidence="low"), spans)
    entry = next(row for row in checked if row.item.text == BODY)
    assert entry.published and entry.span is not None
    assert entry.span["label"] == "E2" and entry.item.quote == BODY
    assert not any(mark["code"] == "citation_content_unusable" for mark in entry.marks)
    assert any(note.get("citation_resolution", {}).get("reason_code") == "citation_content_unusable" for note in dropped)
    assert grounded.detach_unsourced(checked) == 0
    assert entry.review_assessment == {}


@pytest.mark.parametrize("support", ["supported", "contradicted"])
def test_semantic_audit_cannot_promote_or_delete_from_an_unusable_quote(support: str) -> None:
    entry, _ = check("=" * 99)
    audit = GroundedAudit.model_validate(
        {
            "reviews": [{"index": 0, "support": support, "applicability": "matched", "reason": "合成の観測"}],
            "summary_supported": False,
            "goal_alignment": "partial",
        }
    )
    protected = grounded.citation_support_policy([entry], audit)
    assert protected.reviews[0].support == "unknown"
    assert protected.reviews[0].observed_support == support
    grounded.apply_audit([entry], protected)
    assert entry.published and not entry.removed and not entry.confirmed
    assert entry.review_assessment["effective_support"] == "unknown"
    assert entry.citation_resolution["authenticity"] == "verified"
    assert entry.citation_resolution["content_usability"] == "unusable"


def test_existing_language_pack_without_new_reason_uses_builtin_label() -> None:
    base = builtin_pack()
    old_labels = [
        f"{label}={code}"
        for label, code in base.mapping("answer_labels", "passage_reason_labels").items()
        if code != "citation_content_unusable"
    ]
    old_pack = resolve(base, {"answer_labels": {"passage_reason_labels": old_labels}})
    with bind_pack(old_pack):
        assert passage_marks.reason("citation_content_unusable")["label"] == "引用が区切り線・書式のみ"
