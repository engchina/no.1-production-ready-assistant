"""XML ツリー JSON の解析(#2024)。

判定(対応するスキーマだけ)・階層の見出し・番号つきの単位と段落・細目の番号・枝番・本文の部と附属の部の
区別・引用ラベル(漢数字と算用数字)・表・目次・ルビ・前文・別表・文書のメタデータを確かめる。
fixture は合成の XML ツリー JSON(tests/fixtures/xml_tree_sample.json。実データは使わない)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rag_parser_core.registry import run_external_adapter
from rag_parser_core.xml_tree_json import (
    XML_TREE_DOCUMENT_ARTIFACT_KEY,
    arabic_number_phrases,
    is_xml_tree_json,
    is_xml_tree_json_extraction,
    kanji_number,
    looks_like_xml_tree_json,
    parse_kanji_number,
    query_citation_keys,
    revision_ref_labels,
    xml_tree_citation_key,
    xml_tree_json_extraction,
    xml_tree_lookup_keys,
    xml_tree_search_text,
)

FIXTURE = Path(__file__).parent / "fixtures" / "xml_tree_sample.json"
TITLE = "サンプル規程"


def _extraction() -> Any:
    extraction = xml_tree_json_extraction(FIXTURE.read_bytes())
    assert extraction is not None
    return extraction


def _by_label(extraction: Any) -> dict[str, Any]:
    return {
        element.metadata["xml_tree_citation_label_arabic"]: element
        for element in extraction.elements
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, "一"),
        (10, "十"),
        (28, "二十八"),
        (44, "四十四"),
        (104, "百四"),
        (115, "百十五"),
        (2054, "二千五十四"),
    ],
    ids=["1", "10", "28", "44", "104", "115", "2054"],
)
def test_kanji_number_round_trip(value: int, expected: str) -> None:
    assert kanji_number(value) == expected
    assert parse_kanji_number(expected) == value


def test_parse_kanji_number_reads_digit_by_digit_form() -> None:
    assert parse_kanji_number("一〇四") == 104
    assert parse_kanji_number("五七") == 57
    assert parse_kanji_number("２８") == 28
    assert parse_kanji_number("abc") is None


def test_revision_ref_labels_short_forms() -> None:
    assert revision_ref_labels("令和二年三月四日規程第一〇四号") == (
        "令和二年規程第百四号",
        "令和2年規程第104号",
    )
    assert revision_ref_labels("平成元年三月三一日規程第八〇号") == (
        "平成元年規程第八十号",
        "平成元年規程第80号",
    )
    assert revision_ref_labels("不明") is None


def test_detection_accepts_only_supported_tree_json() -> None:
    assert is_xml_tree_json(FIXTURE.read_bytes())
    other = json.dumps({"law_info": {}, "revision_info": {}, "law_full_text": {"tag": "Other"}})
    assert looks_like_xml_tree_json(other.encode())
    assert not is_xml_tree_json(other.encode())
    assert not is_xml_tree_json(b'{"items": [1, 2, 3]}')
    assert not is_xml_tree_json(b"[1, 2]")
    assert not is_xml_tree_json("本文".encode())
    assert xml_tree_json_extraction(b'{"items": []}') is None


def test_paragraph_elements_keep_hierarchy_labels_and_items() -> None:
    extraction = _extraction()
    assert is_xml_tree_json_extraction(extraction)
    paragraph = _by_label(extraction)[f"{TITLE} 第7条第1項"]
    assert paragraph.section_path == [
        TITLE,
        "第二章 対象",
        "第一節 範囲",
        "第七条（対象の範囲）",
    ]
    lines = paragraph.text.splitlines()
    # 先頭の行はラベル(文書名・引用ラベル・単位の見出し)。細目は 1 行ずつで、番号と本文を分ける。
    assert lines[0] == f"{TITLE} 第七条第一項（対象の範囲）"
    assert lines[1] == "次の各号のいずれかに当たるものを対象とする。"
    assert lines[2] == "一　第一の区分に当たるもの"
    assert lines[3] == "二　第二の区分に当たるもの"
    assert lines[4] == "　イ　さらに細かい区分"
    assert lines[5] == "三　第三の区分に当たるもの"
    metadata = paragraph.metadata
    assert metadata["xml_tree_citation_label"] == f"{TITLE} 第七条第一項"
    assert metadata["xml_tree_parent_label_arabic"] == f"{TITLE} 第7条"
    assert metadata["xml_tree_part"] == "main"
    assert metadata["xml_tree_part_label"] == "本則"
    assert metadata["xml_tree_item_labels"] == "第一号,第二号,第三号"
    assert metadata["xml_tree_item_labels_arabic"] == "第1号,第2号,第3号"
    assert metadata["xml_tree_document_id"] == "SAMPLE0000000001"


def test_second_paragraph_and_ruby() -> None:
    second = _by_label(_extraction())[f"{TITLE} 第7条第2項"]
    # 段落の番号は本文に入れずラベルにする。ルビの読みは捨てる。
    assert second.text.splitlines()[1] == "前段の定めにかかわらず、例外とする。"
    assert "れい" not in second.text


def test_branch_number_single_paragraph_label_and_table() -> None:
    branch = _by_label(_extraction())[f"{TITLE} 第7条の2"]
    # 段落が 1 つの単位は単位だけのラベル(「第一項」を付けた形は別の書き方に持つ)。
    assert branch.metadata["xml_tree_citation_label"] == f"{TITLE} 第七条の二"
    assert branch.metadata["xml_tree_parent_num"] == "7_2"
    assert branch.metadata["xml_tree_label_aliases"].splitlines() == [
        "第七条の二第一項",
        "第7条の2第1項",
    ]
    # 表は列見出しを保った Markdown の表で、備考は表の後ろ。
    assert "| 区分 | 値 |" in branch.text
    assert "| --- | --- |" in branch.text
    assert "| 第一区分 | 十 |" in branch.text
    assert "備考" in branch.text and "値は別に定める。" in branch.text


def test_supplement_parts_are_distinguished() -> None:
    elements = _by_label(_extraction())
    original = elements[f"{TITLE} 附則（制定時）第3条"]
    assert original.metadata["xml_tree_part"] == "supplement_original"
    assert original.metadata["xml_tree_citation_label"] == f"{TITLE} 附則（制定時）第三条"
    assert original.section_path == [TITLE, "附則（制定時）（抜粋）", "第三条（範囲の特例）"]
    # 最初の版の附属の部は、添えた語を付けない書き方でも引ける。
    assert "附則第3条" in original.metadata["xml_tree_label_aliases"].splitlines()
    revision = elements[f"{TITLE} 附則（令和2年規程第104号）第1項"]
    assert revision.metadata["xml_tree_part"] == "supplement_revision"
    assert revision.metadata["xml_tree_part_ref"] == "令和二年三月四日規程第一〇四号"
    assert (
        revision.metadata["xml_tree_citation_label"]
        == f"{TITLE} 附則（令和二年規程第百四号）第一項"
    )
    # 段落の見出し(単位の無い附属の段落)は本文の先頭に残す。
    assert revision.text.splitlines()[1] == "（適用の日）"
    assert f"{TITLE} 附則（令和2年規程第104号）第2項" in elements


def test_toc_is_not_body_and_statement_and_appendix_are_units() -> None:
    extraction = _extraction()
    texts = "\n".join(element.text for element in extraction.elements)
    assert "目次" not in texts
    assert "（第一条）" not in texts
    elements = _by_label(extraction)
    assert elements[f"{TITLE} 前文"].text.endswith("旧サンプル規程の全部を改める。")
    appendix = elements[f"{TITLE} 別表第1"]
    assert appendix.metadata["xml_tree_citation_label"] == f"{TITLE} 別表第一"
    assert appendix.content_kind == "table"
    assert appendix.text.splitlines()[0] == f"{TITLE} 別表第一（第七条の二関係）"
    # 階層の見出しだけの要素は作らない(見出しは section_path と章節の木に入る)。
    assert all(not element.text.startswith("第一章") for element in extraction.elements)


def test_document_metadata_fields_and_navigation() -> None:
    extraction = _extraction()
    document = extraction.parser_artifacts[XML_TREE_DOCUMENT_ARTIFACT_KEY]
    assert isinstance(document, dict)
    assert document["document_id"] == "SAMPLE0000000001"
    assert document["document_number"] == "令和元年規程第一号"
    assert document["document_type"] == "Rule"
    assert document["abbrev"] == "サ規"
    fields = {field.name: field.value for field in extraction.fields}
    assert fields["文書名"] == TITLE
    assert fields["文書の種類"] == "Rule"
    titles = [node.title for node in extraction.navigation]
    assert titles[0] == TITLE
    assert "第二章 対象" in titles and "附則（制定時）（抜粋）" in titles
    assert extraction.navigation[0].depth == 0 and extraction.navigation[0].child_section_ids


def test_labels_are_unique() -> None:
    labels = [
        element.metadata["xml_tree_citation_label_arabic"] for element in _extraction().elements
    ]
    assert len(labels) == len(set(labels))


def test_search_text_and_lookup_keys_cover_both_numeral_forms() -> None:
    paragraph = _by_label(_extraction())[f"{TITLE} 第7条第1項"]
    search_text = xml_tree_search_text(paragraph.metadata, paragraph.text)
    assert "第七条第一項" in search_text and "第7条第1項" in search_text
    assert "第7条第1項第2号" in search_text
    keys = xml_tree_lookup_keys(paragraph.metadata)
    for label in ("第七条第一項", "第7条第1項", f"{TITLE} 第７条 第１項", "第七条第一項第二号"):
        assert xml_tree_citation_key(label) in keys
    assert xml_tree_citation_key("第7条の2") != xml_tree_citation_key("第7条")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("六十五歳以上の者にあつては", ["65歳以上"]),
        ("二十歳以上六十歳未満の者", ["20歳以上", "60歳未満"]),
        ("二　被保険者（以下「第二号被保険者」という。）", ["第2号被保険者"]),
        ("第七条第一項第二号の規定の適用", []),
        ("十分に一部を改正し、一年ごとに", ["1年"]),
        ("第一項又は第二項の規定、前条第一項各号のいずれか", []),
    ],
    ids=[
        "age",
        "range",
        "term-with-number",
        "citation-only",
        "not-number-words",
        "citation-conjunction",
    ],
)
def test_arabic_number_phrases(text: str, expected: list[str]) -> None:
    """本文の漢数字の数を算用数字の形にする(#2148)。条の番号だけ・数でない語は足さない。"""
    assert arabic_number_phrases(text) == expected


def test_search_text_has_arabic_numbers_of_body() -> None:
    search_text = xml_tree_search_text({"xml_tree_document_title": "テスト法"}, "六十五歳以上の者")
    assert "数: 65歳以上" in search_text
    assert "数:" not in xml_tree_search_text({}, "数の無い本文")


def test_registry_runs_xml_tree_json_adapter() -> None:
    result = run_external_adapter("xml_tree_json", FIXTURE.read_bytes(), None, "application/json")
    assert result.parser_backend == "xml_tree_json"
    assert result.extraction is not None
    assert not result.fallback_used
    other = run_external_adapter("xml_tree_json", b'{"items": []}', None, "application/json")
    assert other.extraction is None
    assert other.warnings == ("xml_tree_json_unsupported_tree",)


def test_query_citation_keys_prefer_titled_label_and_normalize_numerals() -> None:
    assert query_citation_keys(f"{TITLE} 第七条第一項の範囲は") == [
        f"{TITLE}第7条第1項",
        "第7条第1項",
    ]
    assert query_citation_keys("附則第3条") == ["附則第3条"]
    assert query_citation_keys("第7条の2第1項第2号") == ["第7条の2第1項第2号"]
    assert query_citation_keys("対象の範囲はどこまでか") == []
