"""明示された短い見出しを、後続する分割不能な本文から孤立させない。"""

import hashlib
from dataclasses import replace
from typing import Any

import pytest

from rag_engine.generation import grounded
from rag_engine.knowledge.prompt_files import neutralize_boundary_markers
from rag_engine.models.llm import GroundedCitation, GroundedItem
from rag_engine.retrieval.character_forms import fold_width

BODY = "対象の説明" * 40 + "。"


def span(text: str) -> dict[str, Any]:
    return {
        "evidence_id": "stable-a",
        "label": "E1",
        "source_id": "parent-a",
        "source": "synthetic.pdf",
        "text": text,
        "function": ("synthetic.pdf", "合成の説明"),
        "tags": [],
    }


@pytest.mark.parametrize("heading", ["■ 概要", "◆ 説明", "● 補足"])
def test_marked_heading_and_long_indivisible_sentence_share_original_unit(heading: str) -> None:
    text = heading + "\n" + BODY
    assert len(BODY) > grounded.CITE_UNIT_MAX_CHARS
    assert grounded.citation_units(text) == [(0, len(text))]


def test_combined_length_overflow_does_not_leave_a_standalone_heading() -> None:
    body = "あ" * (grounded.CITE_UNIT_MAX_CHARS - 5) + "。"
    text = "■ 概要\n" + body
    assert len(body) <= grounded.CITE_UNIT_MAX_CHARS < len(text)
    assert grounded.citation_units(text) == [(0, len(text))]


@pytest.mark.parametrize("prefix", ["概要", "実行", "42", "項目 | 値", "■ 項目 | 値", "■ 説明します。"])
def test_unproven_heading_table_button_and_completed_sentence_are_not_forced_together(prefix: str) -> None:
    text = prefix + "\n" + BODY
    units = grounded.citation_units(text)
    assert text[units[0][0] : units[0][1]] == prefix
    assert text[units[1][0] : units[1][1]] == BODY


def test_blank_paragraph_boundary_does_not_join_marked_heading() -> None:
    text = "■ 概要\n\n" + BODY
    units = grounded.citation_units(text)
    assert text[units[0][0] : units[0][1]] == "■ 概要"
    assert len(units) == 2


@pytest.mark.parametrize("body", ["■ " + "あ" * 155, "欄 | " + "値" * 155, "欄\t" + "値" * 155])
def test_next_marker_or_table_is_not_joined_to_previous_heading(body: str) -> None:
    text = "■ 概要\n" + body
    units = grounded.citation_units(text)
    assert text[units[0][0] : units[0][1]] == "■ 概要"
    assert text[units[1][0] : units[1][1]] == body


def test_offsets_and_new_view_quote_use_same_original_heading_and_body() -> None:
    text = "■ 概要\n" + BODY + "\n別の説明です。"
    evidence = span(text)
    prompt = grounded.format_functions([evidence])
    view = grounded._citation_view(evidence)
    assert view.contract_version == "explicit_units_v5"
    assert prompt.count("1| ") == 1 and prompt.count("2| ") == 1
    assert view.units == ((0, len("■ 概要\n" + BODY)), (len("■ 概要\n" + BODY) + 1, len(text)))
    item = GroundedItem(kind="operation", text="対象の説明を案内します。", citations=[GroundedCitation(evidence_id="E1", lines="1")])
    quoted, error = grounded.cited_quote(item, [evidence])
    assert not error and quoted.quote == "■ 概要\n" + BODY
    assert quoted.citations[0]._extraction["citation_unit_contract"] == "explicit_units_v5"


def test_uncached_legacy_and_cached_v2_keep_old_unit_interpretation() -> None:
    text = "■ 概要\n" + BODY
    evidence = span(text)
    legacy = grounded._citation_view(evidence)
    assert legacy.contract_version == "legacy_threshold_v1"
    assert len(legacy.units) == 2
    assert text[legacy.units[0][0] : legacy.units[0][1]] == "■ 概要"
    # 保存済みの版の表示と切分けをそのまま再生する。現在の再生成で番号を変更しない。
    rendered = grounded._rendered_evidence_span(evidence, units=legacy.units, numbered=True)
    old = replace(
        legacy,
        contract_version="explicit_units_v2",
        numbered=True,
        rendered=rendered,
        view_sha256=hashlib.sha256(neutralize_boundary_markers(fold_width(rendered)).encode()).hexdigest(),
    )
    token = grounded._CITATION_VIEWS.set({id(evidence): (evidence, old)})
    try:
        current = grounded._citation_view(evidence)
        assert current.contract_version == "explicit_units_v2" and current.units == legacy.units
        item = GroundedItem(kind="operation", text="対象の説明を案内します。", citations=[GroundedCitation(evidence_id="E1", lines="1")])
        quoted, error = grounded.cited_quote(item, [evidence])
        assert not error and quoted.quote == "■ 概要"
        assert quoted.citations[0]._extraction["citation_unit_contract"] == "explicit_units_v2"
    finally:
        grounded._CITATION_VIEWS.reset(token)


@pytest.mark.parametrize("text", ["実行", "42", "項目 | 値\nA | 1"])
def test_standalone_short_content_is_preserved_without_deletion(text: str) -> None:
    assert grounded.citation_units(text, contract_version="explicit_units_v4") == [(0, len(text))]
    # v5 は物理行の少ない本文を物理行ごとの単位に分ける。分けても原文は欠けない。
    units = grounded.citation_units(text)
    assert [text[start:end] for start, end in units] == text.split("\n")
