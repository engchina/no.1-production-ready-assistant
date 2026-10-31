"""新しい表示の明示番号と、旧未番号の引用契約を混同しない。"""

from typing import Any

import pytest

from rag_engine.generation import grounded
from rag_engine.generation.citation_range_repair import CitationRangeRepair, apply_range_repair
from rag_engine.generation.citation_wire import canonical_generation_draft, generation_draft_wire_model
from rag_engine.models.llm import GroundedDraft, GroundedItem


def span(index: int, text: str) -> dict[str, Any]:
    return {
        "evidence_id": f"stable-{index}",
        "label": f"E{index}",
        "source_id": f"parent-{index}",
        "source": "synthetic.pdf",
        "text": text,
        "function": ("synthetic.pdf", "対象操作"),
        "source_order": (0, index, 0),
        "tags": [],
        "child_ranges": [{"id": f"child-{index}", "start": 0, "end": len(text)}],
    }


def draft(start: int | None, end: int | None, identity: str = "E1") -> GroundedDraft:
    value = generation_draft_wire_model().model_validate(
        {
            "items": [
                {
                    "kind": "operation",
                    "request_id": "Q1",
                    "text": "対象の操作を案内します。",
                    "citations": [{"evidence_id": identity, "line_start": start, "line_end": end}],
                }
            ],
            "summary": "合成の案内です。",
            "confidence": "low",
        }
    )
    return canonical_generation_draft(value)


def extract(proposal: GroundedDraft, evidence: list[dict[str, Any]]) -> tuple[GroundedItem, str]:
    return grounded.cited_quote(proposal.items[0], evidence)


def test_short_multiline_block_numbers_each_physical_line_and_quotes_the_original() -> None:
    evidence = [span(1, "開く\n保存")]
    prompt = grounded.format_functions(evidence)
    view = grounded._citation_view(evidence[0])
    assert len(view.units) == 2
    assert view.numbered is True
    assert view.contract_version == "explicit_units_v5"
    assert "1| 開く\n2| 保存" in prompt
    assert "3|" not in prompt
    cited, error = extract(draft(1, 2), evidence)
    assert not error and cited.quote == "開く\n保存"
    assert cited.citations[0]._extraction["sent_view_status"] == "prepared"
    cited, error = extract(draft(2, 2), evidence)
    assert not error and cited.quote == "保存"


@pytest.mark.parametrize("text", ["実行", "42", "項目 | 値\nA | 1"])
def test_short_button_number_and_table_remain_numbered_original_content(text: str) -> None:
    evidence = [span(1, text)]
    lines = text.split("\n")
    assert "\n".join(f"{number}| {line}" for number, line in enumerate(lines, 1)) in grounded.format_functions(evidence)
    cited, error = extract(draft(1, len(lines)), evidence)
    assert not error and cited.quote == text


def test_each_evidence_has_its_own_upper_bound_not_the_neighbour_bound() -> None:
    long_text = "\n".join(character * 220 for character in ("甲", "乙", "丙"))
    evidence = [span(1, long_text), span(2, "実行")]
    grounded.format_functions(evidence)
    assert len(grounded._citation_view(evidence[0]).units) == 3
    cited, error = extract(draft(3, 3), evidence)
    assert not error and cited.quote == "丙" * 220
    invalid, error = extract(draft(3, 3, "E2"), evidence)
    assert error and not invalid.quote
    assert invalid.citations[0]._extraction["reason_code"] == "citation_range_out_of_bounds"


def test_null_is_missing_in_new_view_but_legacy_uncached_short_block_still_accepts_null() -> None:
    displayed = [span(1, "開く\n保存")]
    grounded.format_functions(displayed)
    invalid, error = extract(draft(None, None), displayed)
    assert error and not invalid.quote
    assert invalid.citations[0]._extraction["reason_code"] == "citation_range_missing"

    legacy = [span(1, "開く\n保存")]
    assert grounded._citation_view(legacy[0]).numbered is False
    cited, error = extract(draft(None, None), legacy)
    assert not error and cited.quote == legacy[0]["text"]
    assert cited.citations[0]._extraction["sent_view_status"] == "not_recorded"


@pytest.mark.parametrize(
    "start,end,reason",
    [
        (0, 0, "citation_range_out_of_bounds"),
        (3, 3, "citation_range_out_of_bounds"),
        (4, 4, "citation_range_out_of_bounds"),
        (5, 5, "citation_range_out_of_bounds"),
        (None, 1, "citation_range_invalid"),
    ],
)
def test_bad_saved_ranges_stay_bad_without_expansion(start: int | None, end: int | None, reason: str) -> None:
    evidence = [span(1, "開く\n保存")]
    grounded.format_functions(evidence)
    original = draft(start, end)
    before = original.model_dump()
    cited, error = extract(original, evidence)
    assert error and not cited.quote
    assert cited.citations[0]._extraction["reason_code"] == reason
    assert cited.text == original.items[0].text
    assert original.model_dump() == before


def test_repair_uses_the_same_numbered_short_view_and_changes_only_the_range() -> None:
    evidence = [span(1, "開く\n保存")]
    grounded.format_functions(evidence)
    original = draft(0, 0)
    checked, dropped = grounded.verify("対象の操作は？", original, evidence)
    targets = grounded.citation_range_targets(original, evidence, checked, dropped)
    assert len(targets) == 1
    assert targets[0]["unit_count"] == 2 and targets[0]["numbered"] is True
    response = CitationRangeRepair.model_validate(
        {"ranges": [{"item_index": 0, "citation_index": 0, "evidence_id": "E1", "line_start": 1, "line_end": 2}]}
    )
    repaired, diagnostic = apply_range_repair(original, response, targets)
    assert diagnostic["status"] == "completed"
    assert repaired.items[0].text == original.items[0].text
    assert repaired.summary == original.summary
    cited, error = extract(repaired, evidence)
    assert not error and cited.quote == evidence[0]["text"]


def test_reordered_siblings_resolve_current_labels_and_never_old_view_content() -> None:
    first, second = span(1, "開く"), span(2, "保存")
    grounded.format_functions([first, second])
    previous, error = extract(draft(1, 1), [first, second])
    assert not error and previous.quote == "開く"
    reordered = [dict(second, label="E1"), dict(first, label="E2")]
    grounded.format_functions(reordered)
    current, error = extract(draft(1, 1), reordered)
    assert not error and current.quote == "保存"
    assert current.citations[0]._extraction["source_id"] == "parent-2"
    assert previous.quote == "開く"
    assert current.citations[0]._extraction["view_sha256"] != previous.citations[0]._extraction["view_sha256"]


def test_bad_primary_does_not_remove_valid_secondary_or_turn_it_into_claim_support() -> None:
    evidence = [span(1, "開く"), span(2, "保存")]
    grounded.format_functions(evidence)
    proposal = draft(5, 5)
    proposal.items[0].citations.append(draft(1, 1, "E2").items[0].citations[0])
    checked, _ = grounded.verify("対象の操作は？", proposal, evidence)
    assert len(checked) == 1
    citations = list(grounded.item_citations(checked[0]))
    assert any(own.item.quote == "保存" and own.citation_resolution["authenticity"] == "verified" for own in citations)
    assert checked[0].item.text == proposal.items[0].text
    assert checked[0].review_assessment == {}
