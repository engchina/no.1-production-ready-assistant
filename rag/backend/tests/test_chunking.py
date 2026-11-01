"""チャンク分割のテスト。"""

import json

import pytest
from rag_pipeline_core.chunking import (
    TABLE_VISION_SUPPLEMENT_LABEL,
    chunk_extraction_with_strategy,
)

from app.rag.chunking import chunk_extraction, chunk_text
from app.schemas.extraction import DocumentElement, StructuredExtraction


def test_chunk_text_respects_overlap_and_offsets() -> None:
    text = "これは一つ目の文です。これは二つ目の文です。これは三つ目の文です。"
    chunks = chunk_text(text, chunk_size=18, overlap=4)
    assert len(chunks) >= 2
    assert chunks[0].index == 0
    assert chunks[1].text.startswith(chunks[0].text[-4:])


def test_sentence_that_does_not_fit_starts_next_chunk_instead_of_its_own() -> None:
    """収まらなかった短い文は次の chunk の先頭にし、その文だけの chunk を作らない(#3380)。"""
    sentence = "これは合成の本文の文です。"
    chunks = chunk_extraction(
        StructuredExtraction(
            raw_text=sentence * 80,
            elements=[DocumentElement(kind="text", text=sentence * 80)],
        ),
        chunk_size=300,
        overlap=0,
    )

    assert len(chunks) > 1
    assert all(len(chunk.text) > len(sentence) * 2 for chunk in chunks[:-1])
    assert all(len(chunk.text) <= 300 for chunk in chunks)
    assert "".join(chunk.text.replace(" ", "") for chunk in chunks) == sentence * 80


def test_chunk_text_does_not_apply_overlap_twice_to_long_sentence() -> None:
    chunks = chunk_text("ABCDEFGHIJKLMNOPQRSTUV", chunk_size=10, overlap=2)

    assert [chunk.text for chunk in chunks] == [
        "ABCDEFGHIJ",
        "IJ KLMNOPQRST",
        "ST UV",
    ]


def test_chunk_text_rejects_invalid_overlap() -> None:
    with pytest.raises(ValueError):
        chunk_text("テスト", chunk_size=10, overlap=10)


def test_chunk_text_adds_section_and_content_metadata() -> None:
    """見出し・箇条書き・表の構造 metadata を chunk に付ける。"""
    text = """# 経費申請
## 承認
- 部門長が承認します。
- 経理部が確認します。
## 支払条件
|項目|条件|
|期限|月末|
"""

    chunks = chunk_text(text, chunk_size=80, overlap=0)

    approval = next(chunk for chunk in chunks if "部門長" in chunk.text)
    assert approval.metadata["section_title"] == "承認"
    assert approval.metadata["section_path"] == "経費申請 > 承認"
    assert approval.metadata["section_level"] == 2
    assert approval.metadata["content_kind"] == "list"
    assert isinstance(approval.metadata["chunk_group_id"], str)
    assert approval.metadata["chunk_group_kind"] == "section"
    assert approval.metadata["chunk_part_index"] == 1
    approval_part_count = approval.metadata["chunk_part_count"]
    assert isinstance(approval_part_count, int)
    assert approval_part_count >= 1
    assert approval.metadata["text_chars"] == len(approval.text)
    assert isinstance(approval.metadata["text_sha256"], str)
    assert len(str(approval.metadata["text_sha256"])) == 64
    assert approval.metadata["chunk_size_target"] == 80
    assert approval.metadata["chunk_size_limit"] == 80
    assert approval.metadata["chunk_size_compliance"] == "within_limit"

    payment = next(chunk for chunk in chunks if "月末" in chunk.text)
    assert payment.metadata["section_title"] == "支払条件"
    assert payment.metadata["content_kind"] == "table"


def test_structured_extraction_infers_elements_from_raw_text() -> None:
    """raw_text だけでも title/list/table 要素へ軽量正規化する。"""
    extraction = StructuredExtraction(
        raw_text="""# 経費申請
- 部門長が承認します。
|項目|条件|
|期限|月末|
"""
    )

    assert [element.kind for element in extraction.elements] == [
        "title",
        "list",
        "table",
    ]
    assert extraction.elements[1].section_path == ["経費申請"]
    raw_start = extraction.elements[2].metadata["raw_start"]
    assert isinstance(raw_start, int)
    assert raw_start >= 0


def test_structured_extraction_infers_spaced_chapter_numbers_from_raw_text() -> None:
    """「第 3 章」(番号の前後に空白)も章の見出しにし、「3.1」はその子になる(#1517)。"""
    extraction = StructuredExtraction(
        raw_text="""第 3 章 設備ごとの点検
3.1 電源設備
分電盤の表示灯を確かめる。
第 4 章 点検を中止する条件
漏水を見つけたら中止する。
"""
    )

    body = [element for element in extraction.elements if element.kind != "title"]
    assert body[0].section_path == ["設備ごとの点検", "電源設備"]
    assert body[1].section_path == ["点検を中止する条件"]


def test_structured_extraction_infers_code_and_equation_blocks() -> None:
    """Markdown/LaTeX block を code/equation element として保持する。"""
    extraction = StructuredExtraction(
        raw_text="""# 実装メモ
```python
def answer() -> int:
    return 42
```

$$
E = mc^2
$$
"""
    )

    assert [element.kind for element in extraction.elements] == [
        "title",
        "code",
        "equation",
    ]
    code_element = extraction.elements[1]
    equation_element = extraction.elements[2]
    assert code_element.content_kind == "code"
    assert code_element.metadata["code_language"] == "python"
    assert "return 42" in code_element.text
    assert equation_element.content_kind == "equation"
    assert equation_element.metadata["equation_delimiter"] == "$$"

    chunks = chunk_extraction(extraction, chunk_size=80, overlap=0)

    code_chunk = next(chunk for chunk in chunks if "return 42" in chunk.text)
    equation_chunk = next(chunk for chunk in chunks if "E = mc^2" in chunk.text)
    assert code_chunk.metadata["content_kind"] == "code"
    assert code_chunk.metadata["element_kinds"] == "code"
    assert code_chunk.metadata["section_title"] == "実装メモ"
    assert code_chunk.metadata["code_language"] == "python"
    assert equation_chunk.metadata["content_kind"] == "equation"
    assert equation_chunk.metadata["element_kinds"] == "equation"
    assert equation_chunk.metadata["equation_delimiter"] == "$$"


def test_structured_extraction_builds_raw_text_from_elements() -> None:
    """elements だけの VLM 出力でも raw_text fallback を合成する。"""
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(kind="title", text="第1章 概要", page_number=1),
            DocumentElement(kind="text", text="本文です。", page_number=1),
        ]
    )

    assert "本文です。" in extraction.raw_text
    assert [element.order for element in extraction.elements] == [0, 1]
    assert extraction.elements[1].section_path == ["概要"]


def test_chunk_extraction_isolates_tables_and_adds_element_metadata() -> None:
    """表は他の本文と混ぜず、ページ・要素 metadata を付ける。"""
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(kind="title", text="## 支払条件", page_number=2),
            DocumentElement(kind="text", text="支払条件の概要です。", page_number=2),
            DocumentElement(
                kind="table",
                text="|項目|条件|\n|期限|月末|",
                page_number=2,
                metadata={
                    "element_id": "tbl-1",
                    "table_id": "sheet-a-table-1",
                    "row_count": 2,
                    "column_count": 2,
                },
            ),
            DocumentElement(kind="text", text="補足説明です。", page_number=3),
        ]
    )

    chunks = chunk_extraction(extraction, chunk_size=80, overlap=8)

    table_chunk = next(chunk for chunk in chunks if "|期限|月末|" in chunk.text)
    assert table_chunk.text == "|項目|条件|\n|期限|月末|"
    assert table_chunk.metadata["chunk_profile"] == "structure_v1"
    assert table_chunk.metadata["content_kind"] == "table"
    assert table_chunk.metadata["section_title"] == "支払条件"
    assert table_chunk.metadata["page_start"] == 2
    assert table_chunk.metadata["page_end"] == 2
    assert table_chunk.metadata["element_ids"] == "tbl-1"
    assert table_chunk.metadata["table_id"] == "sheet-a-table-1"
    assert table_chunk.metadata["table_row_count"] == 2
    assert table_chunk.metadata["table_column_count"] == 2
    assert table_chunk.metadata["text_chars"] == len(table_chunk.text)


def test_chunk_extraction_keeps_extracted_field_marks_apart_from_source_text() -> None:
    """項目抽出の値の要素（派生）は見出しに読み直さず、印を chunk に引き継ぎ、原文と結合しない。"""
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(kind="text", text="原文の段落です。", page_number=1, element_id="e1"),
            DocumentElement(
                kind="text",
                text="1. 項目 A: 値 A",
                page_number=1,
                element_id="field-2-a",
                content_kind="field",
                metadata={"extracted_field": True, "field_name": "1. 項目 A"},
            ),
        ]
    )

    chunks = chunk_extraction(extraction, chunk_size=200, overlap=0)

    assert [chunk.text for chunk in chunks] == ["原文の段落です。", "1. 項目 A: 値 A"]
    assert "extracted_field" not in chunks[0].metadata
    assert chunks[1].metadata["content_kind"] == "field"
    assert chunks[1].metadata["extracted_field"] is True
    assert extraction.elements[1].kind == "text"


def test_chunk_extraction_keeps_field_source_element_per_value() -> None:
    """項目抽出の値の元の要素を chunk に引き継ぎ、元の要素の違う値を結合しない（#2641）。

    検索で当たった値の chunk を根拠に渡すとき、この要素を含む原文の chunk を読む。
    """
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(kind="text", text="原文の段落です。", page_number=1, element_id="e1"),
            *(
                DocumentElement(
                    kind="text",
                    text=f"項目 {name}: 値 {name}",
                    page_number=1,
                    element_id=f"field-{order}-{name}",
                    content_kind="field",
                    metadata={"extracted_field": True, "field_source_element_id": source},
                )
                for order, (name, source) in enumerate([("A", "e1"), ("B", "e1"), ("C", "e9")])
            ),
        ]
    )

    chunks = chunk_extraction(extraction, chunk_size=200, overlap=0)

    assert [chunk.text for chunk in chunks] == [
        "原文の段落です。",
        "項目 A: 値 A\n項目 B: 値 B",
        "項目 C: 値 C",
    ]
    assert "field_source_element_id" not in chunks[0].metadata
    assert [chunk.metadata["field_source_element_id"] for chunk in chunks[1:]] == ["e1", "e9"]


def test_chunk_extraction_preserves_bbox_coordinate_metadata() -> None:
    """bbox の coordinate mode / unit は citation-to-preview 用 metadata に残す。"""
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(kind="title", text="## 料金表", page_number=2),
            DocumentElement(
                kind="table",
                text="交通費は1000円です。",
                page_number=2,
                bbox=[25, 10, 50, 40],
                metadata={
                    "element_id": "tbl-bbox",
                    "bbox_coordinate_mode": "x,y,width,height",
                    "bbox_unit": "percent",
                },
            ),
        ]
    )

    chunks = chunk_extraction(extraction, chunk_size=80, overlap=0)

    table_chunk = next(chunk for chunk in chunks if "交通費" in chunk.text)
    assert table_chunk.metadata["bbox"] == "[25.0,10.0,50.0,40.0]"
    assert table_chunk.metadata["bbox_coordinate_mode"] == "xywh"
    assert table_chunk.metadata["bbox_unit"] == "percent"


def test_chunk_extraction_unions_same_page_bbox() -> None:
    """同一ページ・xyxy・ratio の多要素チャンクは union bbox を持つ。"""
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(
                kind="text",
                text="一行目の本文です。",
                page_number=1,
                bbox=[0.1, 0.05, 0.4, 0.1],
                metadata={"bbox_coordinate_mode": "xyxy", "bbox_unit": "ratio"},
            ),
            DocumentElement(
                kind="text",
                text="二行目の本文です。",
                page_number=1,
                bbox=[0.1, 0.2, 0.6, 0.25],
                metadata={"bbox_coordinate_mode": "xyxy", "bbox_unit": "ratio"},
            ),
        ]
    )

    chunks = chunk_extraction(extraction, chunk_size=200, overlap=0)

    chunk = next(c for c in chunks if "一行目" in c.text and "二行目" in c.text)
    assert json.loads(str(chunk.metadata["bbox"])) == [0.1, 0.05, 0.6, 0.25]
    assert chunk.metadata["bbox_coordinate_mode"] == "xyxy"
    assert chunk.metadata["bbox_unit"] == "ratio"
    assert chunk.metadata.get("element_ids")


def test_union_and_parse_bbox_helpers() -> None:
    """union/parse の境界分岐(退化・不正 JSON)を直接押さえる。"""
    from rag_pipeline_core.chunking import _parse_bbox_json, _union_bbox

    assert _union_bbox([[0.1, 0.05, 0.4, 0.1], [0.1, 0.2, 0.6, 0.25]]) == [
        0.1,
        0.05,
        0.6,
        0.25,
    ]
    assert _union_bbox([[0.5, 0.5, 0.5, 0.5]]) is None  # 面積 0 は None
    assert _union_bbox([]) is None
    assert _parse_bbox_json("[1,2,3,4]") == [1.0, 2.0, 3.0, 4.0]
    assert _parse_bbox_json("[1,2]") is None  # 4 値未満
    assert _parse_bbox_json("not json") is None
    assert _parse_bbox_json(None) is None


def test_chunk_extraction_omits_bbox_across_pages() -> None:
    """跨頁チャンクは union が無意味なので bbox を出さず element_ids+page 範囲に委ねる。"""
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(
                kind="text",
                text="ページ1の本文。",
                page_number=1,
                element_id="e1",
                bbox=[0.1, 0.1, 0.2, 0.2],
                metadata={"bbox_coordinate_mode": "xyxy", "bbox_unit": "ratio"},
            ),
            DocumentElement(
                kind="text",
                text="ページ2の本文。",
                page_number=2,
                element_id="e2",
                bbox=[0.1, 0.1, 0.2, 0.2],
                metadata={"bbox_coordinate_mode": "xyxy", "bbox_unit": "ratio"},
            ),
        ]
    )

    chunks = chunk_extraction(extraction, chunk_size=200, overlap=0)

    for chunk in chunks:
        page_start = chunk.metadata.get("page_start")
        page_end = chunk.metadata.get("page_end")
        if page_start is not None and page_end is not None and page_start != page_end:
            assert "bbox" not in chunk.metadata
            assert chunk.metadata.get("element_ids")


def test_chunk_extraction_does_not_overlap_across_sections() -> None:
    """章節が変わるところでは overlap で前章末尾を混ぜない。"""
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(kind="title", text="# 第1章", page_number=1),
            DocumentElement(kind="text", text="前章の最後です。", page_number=1),
            DocumentElement(kind="title", text="# 第2章", page_number=1),
            DocumentElement(kind="text", text="次章の本文です。", page_number=1),
        ]
    )

    chunks = chunk_extraction(extraction, chunk_size=80, overlap=6)

    second = next(chunk for chunk in chunks if "次章の本文" in chunk.text)
    assert not second.text.startswith("最後です。")
    assert second.metadata["section_title"] == "第2章"


def test_chunk_extraction_groups_figure_with_caption() -> None:
    """図と図注は multimodal RAG 用の figure chunk として結合する。"""
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(kind="title", text="## アーキテクチャ", page_number=4),
            DocumentElement(
                kind="figure",
                text="検索・回答フロー構成図: 取込、検索、生成の流れ。",
                element_id="fig-1",
                page_number=4,
            ),
            DocumentElement(
                kind="figure_caption",
                text="図1: Oracle AI Database とモデルの API の連携。",
                element_id="fig-1-caption",
                parent_id="fig-1",
                page_number=4,
            ),
            DocumentElement(kind="text", text="本文の補足説明です。", page_number=4),
        ]
    )

    chunks = chunk_extraction(extraction, chunk_size=120, overlap=8)

    figure_chunk = next(chunk for chunk in chunks if "構成図" in chunk.text)
    assert "図1" in figure_chunk.text
    assert "本文の補足説明" not in figure_chunk.text
    assert figure_chunk.metadata["content_kind"] == "figure"
    assert figure_chunk.metadata["element_kinds"] == "figure,figure_caption"
    assert figure_chunk.metadata["element_ids"] == "fig-1,fig-1-caption"
    assert figure_chunk.metadata["parent_element_ids"] == "fig-1"
    assert figure_chunk.metadata["dependency_edge_count"] == 1
    assert figure_chunk.metadata["dependency_edges"] == (
        '[{"child_id":"fig-1-caption","parent_id":"fig-1"}]'
    )
    assert figure_chunk.metadata["section_title"] == "アーキテクチャ"
    assert figure_chunk.metadata["page_start"] == 4
    assert figure_chunk.metadata["page_end"] == 4
    # Vision が読んでいない図の本文は解析の OCR・キャプション（#1282）。
    assert figure_chunk.metadata["figure_text_source"] == "ocr"


@pytest.mark.parametrize(
    ("vision_status", "expected"),
    [("succeeded", "vision"), ("failed", "ocr"), ("skipped", "ocr")],
)
def test_chunk_extraction_records_whether_vlm_wrote_figure_text(
    vision_status: str, expected: str
) -> None:
    """図の本文を Vision（VLM）が書いたかを chunk に残す（根拠の evidence_type。#1282）。"""
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(
                kind="figure",
                text="申請画面。右上の「承認」ボタンを押す。",
                element_id="fig-1",
                page_number=2,
                metadata={"vision_status": vision_status},
            ),
            DocumentElement(kind="text", text="本文です。", page_number=2),
        ]
    )

    chunks = chunk_extraction(extraction, chunk_size=120, overlap=8)

    figure = next(chunk for chunk in chunks if "承認" in chunk.text)
    text = next(chunk for chunk in chunks if "本文です" in chunk.text)
    assert figure.metadata["figure_text_source"] == expected
    assert "figure_text_source" not in text.metadata


def test_chunk_extraction_adds_parent_group_metadata_to_split_table() -> None:
    """分割された表 chunk は同一 parent group と part 順序を保持する。"""
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(kind="title", text="## 料金表", page_number=1),
            DocumentElement(
                kind="table",
                text="|項目|金額|\n|A|100|\n|B|200|\n|C|300|",
                page_number=1,
                metadata={
                    "element_id": "tbl-price",
                    "table_id": "xlsx-sheet-1",
                    "row_count": 4,
                    "column_count": 2,
                    "chunk_template": "office_sheet",
                },
            ),
        ]
    )

    chunks = chunk_extraction(extraction, chunk_size=16, overlap=0)
    table_chunks = [chunk for chunk in chunks if chunk.metadata["content_kind"] == "table"]

    assert len(table_chunks) >= 2
    group_ids = {chunk.metadata["chunk_group_id"] for chunk in table_chunks}
    assert len(group_ids) == 1
    assert all(chunk.metadata["chunk_group_kind"] == "table" for chunk in table_chunks)
    part_indexes: list[int] = []
    for chunk in table_chunks:
        part_index = chunk.metadata["chunk_part_index"]
        assert isinstance(part_index, int)
        part_indexes.append(part_index)
    assert part_indexes == list(range(1, len(table_chunks) + 1))
    part_counts: set[int] = set()
    for chunk in table_chunks:
        part_count = chunk.metadata["chunk_part_count"]
        assert isinstance(part_count, int)
        part_counts.add(part_count)
    assert part_counts == {len(table_chunks)}
    assert all(chunk.metadata["element_ids"] == "tbl-price" for chunk in table_chunks)
    assert all(chunk.metadata["table_id"] == "xlsx-sheet-1" for chunk in table_chunks)
    assert all(chunk.metadata["table_row_count"] == 4 for chunk in table_chunks)
    assert all(chunk.metadata["table_column_count"] == 2 for chunk in table_chunks)
    assert all(chunk.metadata["table_row_tree_version"] == "row_tree_v1" for chunk in table_chunks)
    assert all(
        chunk.metadata["table_row_tree_format"] == "key_value_rows" for chunk in table_chunks
    )
    assert all(chunk.metadata["table_row_tree_column_count"] == 2 for chunk in table_chunks)
    assert all(chunk.metadata["table_row_tree_row_count"] == 1 for chunk in table_chunks)
    assert [chunk.metadata["table_cell_refs"] for chunk in table_chunks] == [
        "A2\nB2",
        "A3\nB3",
        "A4\nB4",
    ]
    assert all(chunk.metadata["table_cell_ref_format"] == "a1" for chunk in table_chunks)
    assert all(chunk.metadata["table_cell_ref_count"] == 2 for chunk in table_chunks)
    assert all(
        json.loads(str(chunk.metadata["table_row_tree_column_keys"])) == ["項目", "金額"]
        for chunk in table_chunks
    )
    assert all(
        isinstance(chunk.metadata["table_row_tree_kv_sha256"], str)
        and len(str(chunk.metadata["table_row_tree_kv_sha256"])) == 64
        for chunk in table_chunks
    )
    assert all(chunk.metadata["chunk_template"] == "table_preserve_rows" for chunk in table_chunks)
    assert all(chunk.metadata["source_chunk_template"] == "office_sheet" for chunk in table_chunks)
    assert all(chunk.metadata["chunk_size_target"] == 16 for chunk in table_chunks)
    assert all(
        chunk.metadata["chunk_size_compliance"] in {"within_limit", "overflow_justified"}
        for chunk in table_chunks
    )


def test_chunk_extraction_repeats_table_header_for_row_group_chunks() -> None:
    """長い表は後続 chunk にも表頭を入れ、列名を失わず QA できるようにする。"""
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(kind="title", text="## 経費明細", page_number=1),
            DocumentElement(
                kind="table",
                text=("|項目|金額|\n|---|---|\n|交通費|1000円|\n|宿泊費|2000円|\n|会議費|3000円|"),
                page_number=1,
                metadata={
                    "element_id": "tbl-expenses",
                    "row_count": 5,
                    "column_count": 2,
                },
            ),
        ]
    )

    chunks = chunk_extraction(extraction, chunk_size=28, overlap=0)
    table_chunks = [chunk for chunk in chunks if chunk.metadata["content_kind"] == "table"]

    assert len(table_chunks) >= 2
    assert table_chunks[0].text.startswith("|項目|金額|\n|---|---|")
    assert table_chunks[0].metadata["table_header_repeated"] is False
    assert table_chunks[0].metadata["table_data_row_start"] == 1
    assert table_chunks[0].metadata["table_id"] == "tbl-expenses"
    assert table_chunks[0].metadata["table_row_count"] == 5
    assert table_chunks[0].metadata["table_column_count"] == 2
    for chunk in table_chunks[1:]:
        assert chunk.text.startswith("|項目|金額|\n|---|---|\n")
        assert chunk.metadata["table_header_repeated"] is True
        assert chunk.metadata["element_ids"] == "tbl-expenses"
        assert chunk.metadata["chunk_group_id"] == table_chunks[0].metadata["chunk_group_id"]
    assert [chunk.metadata["table_data_row_start"] for chunk in table_chunks] == [
        1,
        2,
        3,
    ]
    assert [chunk.metadata["table_data_row_end"] for chunk in table_chunks] == [1, 2, 3]
    assert [chunk.metadata["table_row_tree_row_start"] for chunk in table_chunks] == [
        1,
        2,
        3,
    ]
    assert [chunk.metadata["table_row_tree_row_end"] for chunk in table_chunks] == [
        1,
        2,
        3,
    ]
    assert all(
        json.loads(str(chunk.metadata["table_row_tree_column_keys"])) == ["項目", "金額"]
        for chunk in table_chunks
    )


def test_chunk_extraction_repeats_table_caption_with_split_header() -> None:
    """caption 付き長表も table_preserve_rows のまま分割し、表題を各 part に残す。"""
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(
                kind="table",
                text=(
                    "表1: 経費明細\n"
                    "|項目|金額|\n"
                    "|---|---|\n"
                    "|交通費|1000円|\n"
                    "|宿泊費|2000円|\n"
                    "|会議費|3000円|"
                ),
                page_number=1,
                metadata={
                    "element_id": "tbl-captioned",
                    "table_id": "tbl-captioned",
                    "table_caption": "表1: 経費明細",
                    "row_count": 5,
                    "column_count": 2,
                },
            ),
        ]
    )

    chunks = chunk_extraction(extraction, chunk_size=36, overlap=0)
    table_chunks = [chunk for chunk in chunks if chunk.metadata["content_kind"] == "table"]

    assert len(table_chunks) >= 2
    for chunk in table_chunks:
        assert chunk.text.startswith("表1: 経費明細\n|項目|金額|\n|---|---|")
        assert chunk.metadata["table_caption"] == "表1: 経費明細"
        assert chunk.metadata["table_id"] == "tbl-captioned"
        assert chunk.metadata["chunk_group_kind"] == "table"
    assert table_chunks[0].metadata["table_header_repeated"] is False
    assert all(chunk.metadata["table_header_repeated"] is True for chunk in table_chunks[1:])
    assert [chunk.metadata["table_data_row_start"] for chunk in table_chunks] == [
        1,
        2,
        3,
    ]
    assert [chunk.metadata["table_data_row_end"] for chunk in table_chunks] == [1, 2, 3]


def test_chunk_extraction_links_cross_page_table_continuity() -> None:
    """同一 table_id が複数ページに続く場合は continuity group と全体行番号を付ける。"""
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(
                kind="table",
                text="|項目|金額|\n|---|---|\n|交通費|1000円|\n|宿泊費|2000円|",
                page_number=1,
                metadata={
                    "element_id": "tbl-p1",
                    "table_id": "tbl-expenses",
                    "row_count": 5,
                    "column_count": 2,
                },
            ),
            DocumentElement(
                kind="table",
                text="|項目|金額|\n|---|---|\n|会議費|3000円|\n|備品費|4000円|",
                page_number=2,
                metadata={
                    "element_id": "tbl-p2",
                    "table_id": "tbl-expenses",
                    "row_count": 5,
                    "column_count": 2,
                },
            ),
        ]
    )

    table_chunks = [
        chunk
        for chunk in chunk_extraction(extraction, chunk_size=200, overlap=0)
        if chunk.metadata["content_kind"] == "table"
    ]

    assert len(table_chunks) == 2
    assert {chunk.metadata["chunk_group_id"] for chunk in table_chunks} == {
        table_chunks[0].metadata["table_continuity_group_id"]
    }
    assert all(chunk.metadata["chunk_group_kind"] == "table_continuity" for chunk in table_chunks)
    assert all(chunk.metadata["table_cross_page"] is True for chunk in table_chunks)
    assert all(chunk.metadata["table_page_start"] == 1 for chunk in table_chunks)
    assert all(chunk.metadata["table_page_end"] == 2 for chunk in table_chunks)
    assert [chunk.metadata["chunk_part_index"] for chunk in table_chunks] == [1, 2]
    assert [chunk.metadata["table_continuation_index"] for chunk in table_chunks] == [
        1,
        2,
    ]
    assert [chunk.metadata["table_data_row_start"] for chunk in table_chunks] == [1, 3]
    assert [chunk.metadata["table_data_row_end"] for chunk in table_chunks] == [2, 4]
    assert table_chunks[0].metadata["table_header_repeated"] is False
    assert table_chunks[1].metadata["table_header_repeated"] is True


def test_split_sentences_keeps_closing_brackets_with_sentence() -> None:
    """終端句読点の後の閉じ括弧・閉じ引用は前の文に残す。"""
    from rag_pipeline_core.chunking import _split_sentences

    parts = _split_sentences("彼は「そうです。」と言った。次の文です。")
    assert parts == ["彼は「そうです。」", "と言った。", "次の文です。"]


def test_split_sentences_mixed_japanese_english() -> None:
    """日英混在でも終端記号ごとに分割し、記号は前の文に付く。"""
    from rag_pipeline_core.chunking import _split_sentences

    parts = _split_sentences("これは日本語です。This is English! 最後の文?")
    assert parts == ["これは日本語です。", "This is English!", "最後の文?"]


def test_split_sentences_without_terminal_punctuation_returns_whole_text() -> None:
    """終端句読点がないテキストは 1 文として返す。"""
    from rag_pipeline_core.chunking import _split_sentences

    parts = _split_sentences("句読点のないテキスト")
    assert parts == ["句読点のないテキスト"]


def _table_vision_extraction(*, table_first: bool = False) -> StructuredExtraction:
    """Docling と同じ形(raw_text は表を含まず、説明文は表の要素の metadata にだけある)。"""
    table = DocumentElement(
        kind="table",
        text="<table><tr><td>部品A</td></tr></table>",
        element_id="docling-p1-2",
        page_number=1,
        metadata={"table_vision_text": "登録ボタンの画面"},
    )
    after = DocumentElement(kind="text", text="登録後に確認します。", page_number=1)
    if table_first:
        return StructuredExtraction(raw_text=after.text, elements=[table, after])
    before = DocumentElement(kind="text", text="部品の一覧です。", page_number=1)
    return StructuredExtraction(
        raw_text=f"{before.text}\n\n{after.text}", elements=[before, table, after]
    )


@pytest.mark.parametrize(
    "strategy",
    ["recursive_character", "markdown_heading", "fixed_size", "fixed_delimiter"],
)
def test_raw_text_strategies_insert_table_vision_text_at_the_table(strategy: str) -> None:
    """raw_text を分割する方法も、表の位置(直前の要素の後ろ)に説明文の補足を入れる(#513)。"""
    extraction = _table_vision_extraction()

    chunks = chunk_extraction_with_strategy(
        extraction, strategy=strategy, chunk_size=800, overlap=120
    )

    joined = " ".join(chunk.text for chunk in chunks)
    before = joined.index("部品の一覧です。")
    supplement = joined.index(TABLE_VISION_SUPPLEMENT_LABEL)
    assert before < supplement < joined.index("登録後に確認します。")
    assert "登録ボタンの画面" in joined
    # 保存する抽出結果(表の本文と raw_text)は変えない。
    assert extraction.raw_text == "部品の一覧です。\n\n登録後に確認します。"
    assert extraction.elements[1].text == "<table><tr><td>部品A</td></tr></table>"


def test_raw_text_strategy_puts_table_vision_text_first_when_table_leads() -> None:
    chunks = chunk_extraction_with_strategy(
        _table_vision_extraction(table_first=True),
        strategy="fixed_delimiter",
        delimiter="\\n\\n",
    )

    assert [chunk.text for chunk in chunks] == [
        f"{TABLE_VISION_SUPPLEMENT_LABEL}\n登録ボタンの画面",
        "登録後に確認します。",
    ]


def test_table_vision_text_is_not_added_twice_when_body_has_it() -> None:
    """Docling 以外(Vision の段が本文と raw_text に補足を足した表)には足さない。"""
    body = f"| 品目 |\n| --- |\n| 部品A |\n{TABLE_VISION_SUPPLEMENT_LABEL}\n登録ボタンの画面"
    extraction = StructuredExtraction(
        raw_text=f"部品の一覧です。\n\n{body}",
        elements=[
            DocumentElement(kind="text", text="部品の一覧です。", page_number=1),
            DocumentElement(
                kind="table",
                text=body,
                page_number=1,
                metadata={"table_vision_text": "登録ボタンの画面"},
            ),
        ],
    )

    for strategy in ("structure_aware", "page_level", "recursive_character", "fixed_size"):
        chunks = chunk_extraction_with_strategy(
            extraction, strategy=strategy, chunk_size=800, overlap=120
        )
        joined = "\n".join(chunk.text for chunk in chunks)
        assert joined.count(TABLE_VISION_SUPPLEMENT_LABEL) == 1, strategy


def test_structure_aware_appends_table_vision_text_without_moving_offsets() -> None:
    """構造認識は表の本文の後ろに補足を足し、chunk の位置(offset)は変えない(#513)。"""
    extraction = _table_vision_extraction()
    plain = extraction.model_copy(
        update={
            "elements": [
                element.model_copy(update={"metadata": {}}) for element in extraction.elements
            ]
        }
    )

    chunks = chunk_extraction(extraction, chunk_size=800, overlap=0)
    plain_chunks = chunk_extraction(plain, chunk_size=800, overlap=0)

    table = next(chunk for chunk in chunks if chunk.metadata.get("content_kind") == "table")
    assert table.text == (
        f"<table><tr><td>部品A</td></tr></table>\n{TABLE_VISION_SUPPLEMENT_LABEL}\n登録ボタンの画面"
    )
    assert [(chunk.start_offset, chunk.end_offset) for chunk in chunks] == [
        (chunk.start_offset, chunk.end_offset) for chunk in plain_chunks
    ]


def test_table_vision_text_on_non_table_element_is_ignored() -> None:
    figure = DocumentElement(kind="figure", text="図", metadata={"table_vision_text": "説明"})

    chunks = chunk_extraction(StructuredExtraction(raw_text="図", elements=[figure]))

    assert "説明" not in chunks[0].text


def test_footer_role_survives_chunking_without_marking_neighboring_body() -> None:
    from rag_parser_core.extraction import DocumentElement, StructuredExtraction
    from rag_pipeline_core.chunking import chunk_extraction_with_strategy

    extraction = StructuredExtraction(
        raw_text="本文\n末尾",
        elements=[
            DocumentElement(kind="text", text="本文", element_id="body", section_path=["節"]),
            DocumentElement(
                kind="text",
                text="末尾",
                element_id="footer",
                section_path=["節"],
                metadata={"html_region_role": "footer"},
            ),
        ],
    )
    chunks = chunk_extraction_with_strategy(
        extraction, strategy="small_to_big", chunk_size=1000, overlap=0
    )
    footer = [chunk for chunk in chunks if chunk.metadata.get("html_region_role") == "footer"]
    assert footer and all("本文" not in chunk.text for chunk in footer)
