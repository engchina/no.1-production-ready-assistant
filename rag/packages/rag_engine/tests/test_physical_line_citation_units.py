"""通常の本文は空でない物理行ごとに番号を付け、OCR・表の断片だけを短い行でまとめる（explicit_units_v5。#3847）。"""

import hashlib
from dataclasses import replace
from typing import Any

import pytest

from rag_engine.generation import grounded
from rag_engine.generation.citation_wire import canonical_generation_draft, generation_draft_wire_model
from rag_engine.knowledge.prompt_files import neutralize_boundary_markers
from rag_engine.models.llm import GroundedItem
from rag_engine.retrieval.character_forms import fold_width

TWO = "画面Aで対象を選ぶ。\n保存を押して閉じる。"


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


def pieces(text: str, **kwargs: Any) -> list[str]:
    return [text[start:end] for start, end in grounded.citation_units(text, **kwargs)]


def item(start: int, end: int) -> GroundedItem:
    raw = {
        "items": [
            {
                "kind": "operation",
                "request_id": "Q1",
                "text": "合成の操作を案内します。",
                "citations": [{"evidence_id": "E1", "line_start": start, "line_end": end}],
            }
        ],
        "summary": "合成の案内です。",
        "confidence": "low",
    }
    return canonical_generation_draft(generation_draft_wire_model().model_validate(raw)).items[0]


@pytest.mark.parametrize("count", [2, 3, 5, 18], ids=["two", "three", "five", "eighteen"])
def test_every_non_blank_physical_line_is_its_own_unit(count: int) -> None:
    lines = [f"説明の行{index}で対象と条件を確かめます。" for index in range(count)]
    text = "\n".join(lines)
    assert pieces(text) == lines
    # v4 は短い行を CITE_UNIT_MAX_CHARS までまとめる（送信済みの視図の読み直し用）。
    assert len(pieces(text, contract_version="explicit_units_v4")) < count


def short_fragments(count: int) -> str:
    return "\n".join(f"欄{index}" for index in range(count))


def test_many_very_short_fragment_lines_keep_the_v4_merge() -> None:
    # OCR・表の 1 セルずつの断片が続く本文は、字数の分布で見分けて v4 と同じくまとめる（番号で入力を膨らませない）。
    text = short_fragments(grounded.CITE_FRAGMENT_MIN_LINES)
    assert grounded._fragment_lines(text)
    assert pieces(text) == pieces(text, contract_version="explicit_units_v4") == [text]
    # 行が少ないか、行の字数の中央値が上限を超える本文は、通常の本文として物理行ごとに分ける。
    fewer = short_fragments(grounded.CITE_FRAGMENT_MIN_LINES - 1)
    assert not grounded._fragment_lines(fewer) and pieces(fewer) == fewer.split("\n")
    longer = "\n".join("あ" * (grounded.CITE_UNIT_MAX_CHARS // 10) for _ in range(grounded.CITE_FRAGMENT_MIN_LINES))
    assert not grounded._fragment_lines(longer) and len(pieces(longer)) == grounded.CITE_FRAGMENT_MIN_LINES


@pytest.mark.parametrize(
    "fields", [{"origin": "image_extraction"}, {"origin": "unclassified", "tags": ["image_generated"]}], ids=["origin", "tag"]
)
def test_image_text_keeps_the_v4_merge(fields: dict[str, Any]) -> None:
    evidence = {**span(TWO), **fields}
    assert grounded._image_text(evidence)
    view = grounded._citation_view(evidence, explicit_units=True)
    assert view.contract_version == "explicit_units_v5"
    assert view.units == tuple(grounded.citation_units(TWO, contract_version="explicit_units_v4")) == ((0, len(TWO)),)
    assert not grounded._image_text(span(TWO))


def test_blank_lines_are_skipped_and_indentation_is_kept() -> None:
    text = "説明の行です。\n\n  字下げの続きです。"
    assert pieces(text) == ["説明の行です。", "  字下げの続きです。"]


def test_sentence_pieces_of_one_long_line_merge_only_within_that_line() -> None:
    long_line = "長い説明の文です。" * 30
    units = pieces(long_line)
    assert units == pieces(long_line, contract_version="explicit_units_v4")
    assert "".join(units) == long_line and all("\n" not in unit for unit in units)
    # 長い行の最後の断片と次の短い行は、v4 ではまとまるが v5 では別の単位になる。
    text = long_line + "\n続きの短い行です。"
    assert all("\n" not in unit for unit in pieces(text)) and pieces(text)[-1] == "続きの短い行です。"
    assert "\n" in pieces(text, contract_version="explicit_units_v4")[-1]


def test_heading_keeps_its_long_body_but_short_heading_and_line_are_numbered() -> None:
    body = "対象の説明" * 40 + "。"
    assert pieces("■ 概要\n" + body) == ["■ 概要\n" + body]  # #3685: 見出しを長い本文から孤立させない
    assert pieces("■ 概要\n短い説明。") == ["■ 概要", "短い説明。"]


def test_unknown_contract_is_rejected() -> None:
    with pytest.raises(ValueError, match="explicit_units_v2"):
        grounded.citation_units(TWO, contract_version="explicit_units_v2")


def test_new_view_numbers_each_physical_line_and_extracts_the_cited_lines() -> None:
    evidence = span(TWO)
    prompt = grounded.format_functions([evidence])
    view = grounded._citation_view(evidence)
    assert view.contract_version == grounded.CITATION_UNIT_CONTRACT == "explicit_units_v5"
    assert "1| 画面Aで対象を選ぶ。\n2| 保存を押して閉じる。" in prompt
    quoted, error = grounded.cited_quote(item(1, 2), [evidence])
    assert not error and quoted.quote == TWO
    assert grounded._citation_extraction(quoted)["citation_unit_contract"] == "explicit_units_v5"
    quoted, error = grounded.cited_quote(item(2, 2), [evidence])
    assert not error and quoted.quote == "保存を押して閉じる。"


def test_sent_v4_view_keeps_one_unit_and_rejects_the_physical_line_number() -> None:
    evidence = span(TWO)
    current = grounded._citation_view(evidence, explicit_units=True)
    units = tuple(grounded.citation_units(TWO, contract_version="explicit_units_v4"))
    rendered = grounded._rendered_evidence_span(evidence, units=units, numbered=True)
    old = replace(
        current,
        units=units,
        contract_version="explicit_units_v4",
        rendered=rendered,
        view_sha256=hashlib.sha256(neutralize_boundary_markers(fold_width(rendered)).encode()).hexdigest(),
    )
    token = grounded._CITATION_VIEWS.set({id(evidence): (evidence, old)})
    try:
        view = grounded._citation_view(evidence)
        assert view.contract_version == "explicit_units_v4" and view.units == ((0, len(TWO)),)
        quoted, error = grounded.cited_quote(item(1, 1), [evidence])
        assert not error and quoted.quote == TWO
        # 送信済みの v4 の表示に物理行を数えた範囲は、切り詰めずに範囲外のまま（#3778）。
        quoted, error = grounded.cited_quote(item(1, 2), [evidence])
        assert error and grounded._citation_extraction(quoted)["reason_code"] == "citation_range_out_of_bounds"
    finally:
        grounded._CITATION_VIEWS.reset(token)
