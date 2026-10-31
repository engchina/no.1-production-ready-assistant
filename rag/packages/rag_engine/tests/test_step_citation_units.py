"""本文の手順の番号で始まる行に、それぞれの引用単位の番号を付ける（#3824）。"""

import hashlib
from dataclasses import replace
from typing import Any

import pytest

from rag_engine.generation import grounded
from rag_engine.generation.citation_wire import canonical_generation_draft, generation_draft_wire_model
from rag_engine.knowledge.prompt_files import neutralize_boundary_markers
from rag_engine.models.llm import GroundedItem
from rag_engine.retrieval.character_forms import fold_width

STEPS = "(1) 画面Aを開く。\n(2) 項目Bを選ぶ。\n(3) 実行を押す。"


def span(text: str) -> dict[str, Any]:
    return {
        "evidence_id": "stable-a",
        "label": "E1",
        "source_id": "parent-a",
        "source": "synthetic.pdf",
        "text": text,
        "function": ("synthetic.pdf", "合成の手順"),
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


@pytest.mark.parametrize(
    "markers",
    [("(1)", "(2)"), ("（１）", "（２）"), ("1.", "2."), ("１．", "２．"), ("①", "②"), ("⑴", "⑵"), ("  (1)", "　(2)")],
    ids=["paren", "fullwidth-paren", "dot", "fullwidth-dot", "circled", "parenthesized", "indented"],
)
def test_each_step_line_starts_its_own_unit(markers: tuple[str, str]) -> None:
    text = f"{markers[0]} 画面Aを開く。\n{markers[1]} 項目Bを選ぶ。"
    assert pieces(text) == [f"{markers[0]} 画面Aを開く。", f"{markers[1]} 項目Bを選ぶ。"]
    # 旧契約（explicit_units_v3）は短い行を 1 単位にまとめる。
    assert pieces(text, contract_version="explicit_units_v3") == [text]


def test_continuation_lines_stay_with_their_step_and_intro_is_separate() -> None:
    text = "次の手順で行う。\n①画面Aを開く。\n  続きの説明。\n②項目Bを選ぶ。"
    assert pieces(text, contract_version="explicit_units_v4") == ["次の手順で行う。", "①画面Aを開く。\n  続きの説明。", "②項目Bを選ぶ。"]
    # v5 は続きの行にも自分の番号を付ける（#3847）。
    assert pieces(text) == ["次の手順で行う。", "①画面Aを開く。", "  続きの説明。", "②項目Bを選ぶ。"]


@pytest.mark.parametrize(
    "text",
    ["1.5倍の値を入れる。\n2.0以上は対象外。", "文中の(1)は分けない。(2)も分けない。", "対象\n(注)補足の説明。", "値\n(1,000)円"],
    ids=["decimal", "inline-markers", "non-numeric-paren", "grouped-number"],
)
def test_decimal_inline_and_non_step_markers_are_not_split(text: str) -> None:
    assert pieces(text, contract_version="explicit_units_v4") == pieces(text, contract_version="explicit_units_v3")


def test_heading_before_steps_keeps_steps_numbered_and_long_body_protection() -> None:
    # 見出しの直後が手順の行なら、見出しへまとめず手順に自分の番号を付ける（箇条書きの印の行と同じ扱い）。
    assert pieces("■ 手順\n" + STEPS) == ["■ 手順", *STEPS.split("\n")]
    # 手順ではない分割できない本文は、#3685 のとおり見出しと同じ単位のまま。
    body = "対象の説明" * 40 + "。"
    assert pieces("■ 概要\n" + body) == ["■ 概要\n" + body]


def test_long_step_line_is_still_split_at_sentences() -> None:
    long_step = "(1) " + ("説明の文。" * 40)
    units = pieces(long_step + "\n(2) 次の操作。")
    assert units[-1] == "(2) 次の操作。"
    assert "".join(units[:-1]) == long_step
    assert len(units) > 2


def test_new_view_numbers_each_step_and_extracts_the_cited_step() -> None:
    evidence = span(STEPS)
    prompt = grounded.format_functions([evidence])
    view = grounded._citation_view(evidence)
    assert view.contract_version == "explicit_units_v5" and len(view.units) == 3
    assert "1| (1) 画面Aを開く。\n2| (2) 項目Bを選ぶ。\n3| (3) 実行を押す。" in prompt
    quoted, error = grounded.cited_quote(item(3, 3), [evidence])
    assert not error and quoted.quote == "(3) 実行を押す。"
    extraction = grounded._citation_extraction(quoted)
    assert extraction["citation_unit_contract"] == "explicit_units_v5"
    quoted, error = grounded.cited_quote(item(1, 3), [evidence])
    assert not error and quoted.quote == STEPS


def test_sent_v3_view_keeps_one_unit_and_rejects_the_step_number() -> None:
    evidence = span(STEPS)
    current = grounded._citation_view(evidence, explicit_units=True)
    units = tuple(grounded.citation_units(STEPS, contract_version="explicit_units_v3"))
    rendered = grounded._rendered_evidence_span(evidence, units=units, numbered=True)
    old = replace(
        current,
        units=units,
        contract_version="explicit_units_v3",
        rendered=rendered,
        view_sha256=hashlib.sha256(neutralize_boundary_markers(fold_width(rendered)).encode()).hexdigest(),
    )
    token = grounded._CITATION_VIEWS.set({id(evidence): (evidence, old)})
    try:
        view = grounded._citation_view(evidence)
        assert view.contract_version == "explicit_units_v3" and view.units == ((0, len(STEPS)),)
        quoted, error = grounded.cited_quote(item(1, 1), [evidence])
        assert not error and quoted.quote == STEPS
        assert grounded._citation_extraction(quoted)["citation_unit_contract"] == "explicit_units_v3"
        quoted, error = grounded.cited_quote(item(3, 3), [evidence])
        assert error and grounded._citation_extraction(quoted)["reason_code"] == "citation_range_out_of_bounds"
    finally:
        grounded._CITATION_VIEWS.reset(token)


def test_legacy_unnumbered_view_is_unchanged() -> None:
    legacy = grounded._citation_view(span(STEPS))
    assert legacy.contract_version == "legacy_threshold_v1"
    assert legacy.units == tuple(grounded._legacy_citation_units(STEPS)) == ((0, len(STEPS)),)
