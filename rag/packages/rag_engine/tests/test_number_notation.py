"""回答指示による数量の書式と、変更してはいけない原文・値を確かめる。"""

import pytest

from rag_engine.generation.number_notation import format_draft_numbers
from rag_engine.generation.value_grounding import unsupported_document_value_claim
from rag_engine.models.llm import GroundedCitation, GroundedDraft, GroundedItem

QUESTION = "上限は何件ですか。半角の算用数字と万・千を組み合わせた表記で答えてください。"
SOURCE = "上限は二万四千件です。"


def draft(text):
    return GroundedDraft(
        summary=text,
        confidence="high",
        items=[GroundedItem(kind="rule", text=text, citations=[GroundedCitation(quote=SOURCE, evidence_id="E1")], condition=SOURCE)],
    )


@pytest.mark.parametrize("number", ["二万四千", "24000", "24,000", "２４０００", "2万4千"])
def test_mixed_quantity_is_stable_and_preserves_quote(number):
    source = draft(f"上限は{number}件です。")
    result = format_draft_numbers(QUESTION, source, [])
    assert result.summary == "上限は2万4千件です。"
    assert result.items[0].text == result.summary
    assert result.items[0].quote == SOURCE
    assert result.items[0].condition == SOURCE
    assert source.summary == f"上限は{number}件です。"
    assert not unsupported_document_value_claim(result.summary, [{"origin": "document_text", "text": SOURCE}], QUESTION)


def test_wrong_value_is_not_corrected_or_hidden():
    result = format_draft_numbers(QUESTION, draft("上限は25000件です。"), [])
    assert result.summary == "上限は2万5千件です。"
    assert unsupported_document_value_claim(result.summary, [{"origin": "document_text", "text": SOURCE}], "")


def test_no_style_directive_keeps_original_object():
    original = draft(SOURCE)
    assert format_draft_numbers("上限は何件ですか。", original, []) is original
    assert format_draft_numbers("混在した数値が万件あります。上限を教えてください。", original, []) is original


def test_dates_identifiers_quotes_and_code_are_preserved():
    protected = "2026年5月1日、E24000件、001000件、1.24000円、「二万四千件」、`24000件`、https://example.invalid/24000件"
    result = format_draft_numbers(QUESTION, draft(protected + "。上限は24000件です。"), [])
    assert result.summary == protected + "。上限は2万4千件です。"


def test_verbatim_multi_condition_item_is_preserved():
    original = draft(SOURCE)
    result = format_draft_numbers(QUESTION, original, [{"evidence_id": "E1", "tags": ["multi_condition"]}])
    assert result.summary == "上限は2万4千件です。"
    assert result.items[0] == original.items[0]


def test_only_requested_place_units_are_used():
    question = "上限は何件ですか。数字と千を混ぜた表記で答えてください。"
    assert format_draft_numbers(question, draft(SOURCE), []).summary == "上限は24千件です。"


@pytest.mark.parametrize("number", ["二万四 千", "二万四\n千", "二万四　千"])
def test_layout_gap_is_read_as_a_whole_quantity(number):
    assert format_draft_numbers(QUESTION, draft(f"上限は{number}件です。"), []).summary == "上限は2万4千件です。"


def test_independent_numbers_and_identifier_fragments_are_not_joined():
    text = "2 4000件、E24000 千件。"
    assert format_draft_numbers(QUESTION, draft(text), []).summary == text
