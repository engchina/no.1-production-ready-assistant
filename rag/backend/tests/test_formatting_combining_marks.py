"""抽出の正規化で、書式の飾りの結合の線（下線・上線）を除き、文字の一部の結合文字は残す（#3354）。

章節の木・保存済みの章節・章節と chunk の対応の表から読む名前も同じ規則で除く（#3434）。
"""

from __future__ import annotations

import unicodedata

import pytest
from rag_engine.generation import section_expansion

from app.clients.oracle import _chunk_search_text
from app.clients.section_map_store import _section_node
from app.rag.chunking import chunk_extraction
from app.rag.chunking_small_to_big import build_parent_child_chunks
from app.rag.navigation import build_navigation_tree
from app.rag.pdf_segment_merge import ContinuationGeometry, consolidate_document_extraction
from app.rag.section_rules import (
    PARSER_NAVIGATION_ARTIFACT,
    SectionRulesStore,
    extraction_sections,
    parser_navigation,
)
from app.schemas.document import DocumentSection
from app.schemas.extraction import (
    DocumentElement,
    DocumentNavigationNode,
    ExtractionTable,
    ExtractionTableCell,
    StructuredExtraction,
    strip_formatting_marks,
)
from tests.fixtures.synthetic_pdf import blank_pdf

_LINES = ("\u0305", "\u0332", "\u0333", "\u033f")


@pytest.mark.parametrize(
    "mark", _LINES, ids=["overline", "low-line", "double-low-line", "double-overline"]
)
def test_strips_formatting_line_marks(mark: str) -> None:
    assert strip_formatting_marks(f"Name{mark}02 テスト{mark}") == "Name02 テスト"


@pytest.mark.parametrize(
    "text",
    [
        "か\u3099き\u309a",  # 結合の濁点・半濁点（分解した仮名）
        "cafe\u0301",  # アクセント
        "a\u0304 b\u0331",  # マクロン・下のマクロン
        "x\u0338",  # 斜線の重ね（否定の記号の分解）
        "Plain_text ＿ ‾",  # 下線・上線の文字そのもの
    ],
    ids=["dakuten", "acute", "macron", "solidus", "spacing-lines"],
)
def test_keeps_marks_that_belong_to_characters(text: str) -> None:
    assert strip_formatting_marks(text) == text
    assert unicodedata.normalize("NFC", strip_formatting_marks(text)) == unicodedata.normalize(
        "NFC", text
    )


def test_extraction_schema_strips_lines_from_text_paths_and_tables() -> None:
    extraction = StructuredExtraction(
        raw_text="Heading\u033202\n\nBody\u0305 text.",
        elements=[
            DocumentElement(
                kind="title",
                text="Heading\u033202",
                element_id="title-1",
                section_path=["Chapter\u0333 A", "Heading\u033202"],
            ),
            DocumentElement(
                kind="text",
                text="Body\u0305 text.",
                element_id="body-1",
                section_path="Chapter\u0333 A/Heading\u033202",
            ),
            # 線だけの章節名は空として落とす。
            DocumentElement(
                kind="text", text="Tail.", element_id="tail-1", section_path=["\u0332", "Other"]
            ),
        ],
        tables=[
            ExtractionTable(
                table_id="t1",
                caption="Table\u0332 1",
                cells=[ExtractionTableCell(row=0, col=0, text="Cell\u033f")],
            )
        ],
    )
    assert extraction.raw_text == "Heading02\n\nBody text."
    assert [element.text for element in extraction.elements] == [
        "Heading02",
        "Body text.",
        "Tail.",
    ]
    assert extraction.elements[0].section_path == ["Chapter A", "Heading02"]
    assert extraction.elements[1].section_path == ["Chapter A", "Heading02"]
    assert extraction.elements[2].section_path == ["Other"]
    assert extraction.tables[0].caption == "Table 1"
    assert extraction.tables[0].cells[0].text == "Cell"
    # 保存した payload を読み戻しても同じ（冪等）。
    again = StructuredExtraction.model_validate(extraction.to_document_payload())
    assert again.elements == extraction.elements
    assert again.raw_text == extraction.raw_text


def _record(record_id: str, category: str, seq_no: int, text: str) -> dict[str, object]:
    return {
        "id": record_id,
        "engine": "docling",
        "category": category,
        "page": 1,
        "seq_no": seq_no,
        "text": text,
        "bbox": [50.0, 50.0, 300.0, 80.0],
        "page_width": 595.0,
        "page_height": 842.0,
        "coord_system": "image_top_left",
        "raw": {"text": text},
    }


def test_unverified_lines_from_pdf_do_not_reach_sections_chunks_or_search_text() -> None:
    """原文で裏付けられない線（文字層の無い合成の PDF）も、章節・本文・検索の文に残さない。"""
    pdf = blank_pdf()
    title = "Heading\u033202"
    body = "Synthetic body\u0332 paragraph with an underlined word\u0332."
    records = [_record("title-1", "Section-header", 1, title), _record("body-1", "Text", 2, body)]
    extraction = StructuredExtraction(
        raw_text=f"{title}\n\n{body}",
        elements=[
            DocumentElement(kind="title", text=title, element_id="title-1", page_number=1),
            DocumentElement(kind="text", text=body, element_id="body-1", page_number=1),
        ],
        parser_artifacts={"layout_records": {"records": records}},
    )
    consolidated = consolidate_document_extraction(
        extraction, source_bytes=pdf, geometry=ContinuationGeometry(enabled=False)
    )
    # 解析エンジンの元の出力は診断のため layout_records に残す。
    assert "\u0332" in str(consolidated.parser_artifacts["layout_records"])
    assert all(element.section_path == ["Heading02"] for element in consolidated.elements)
    assert consolidated.elements[1].text == "Synthetic body paragraph with an underlined word."
    assert "\u0332" not in consolidated.raw_text
    assert build_navigation_tree(consolidated)[0].title == "Heading02"
    chunks = [
        *chunk_extraction(consolidated, chunk_size=500, overlap=0),
        *build_parent_child_chunks(consolidated),
    ]
    assert chunks
    for chunk in chunks:
        searched = chunk.text + str(chunk.metadata) + _chunk_search_text(chunk)
        assert not any(mark in searched for mark in _LINES)
    assert (
        consolidate_document_extraction(
            consolidated, source_bytes=pdf, geometry=ContinuationGeometry(enabled=False)
        )
        == consolidated
    )


def _stored_node(section_id: str, path: list[str]) -> dict[str, object]:
    """線を除く前に保存した章節の木の 1 件（検証を通さない JSON の形）。"""
    return {
        "section_id": section_id,
        "title": path[-1],
        "section_path": path,
        "depth": len(path),
        "page_start": 1,
        "page_end": 2,
        "summary": f"Summary\u0332 of {path[-1]}",
    }


def test_navigation_node_strips_lines_and_keeps_character_marks() -> None:
    node = DocumentNavigationNode.model_validate(
        _stored_node("nav-stored", ["Chapter\u0333 A", "Heading\u033202 か\u3099", "\u0332"])
    )
    assert node.title == ""
    assert node.section_path == ["Chapter A", "Heading02 か\u3099"]
    assert node.summary == "Summary of"
    # 章節の ID は保存時の値のまま（人の修正・章節と chunk の対応が参照する）。
    assert node.section_id == "nav-stored"
    titled = DocumentNavigationNode.model_validate(
        _stored_node("nav-2", ["Heading\u0305 か\u3099き\u309a"])
    )
    assert titled.title == "Heading か\u3099き\u309a"
    # 読み戻しても同じ（冪等）。
    assert DocumentNavigationNode.model_validate(titled.model_dump()) == titled


def test_stored_navigation_is_clean_without_reparse() -> None:
    """線を除く前に保存した抽出結果（章節の木と parser_navigation の artifact）も読むときに直る。"""
    stored_nodes = [
        _stored_node("nav-1", ["Chapter\u0332 1"]),
        _stored_node("nav-2", ["Chapter\u0332 1", "Section\u033f 1.1"]),
    ]
    payload = StructuredExtraction(raw_text="Body.").model_dump(mode="json")
    payload["navigation"] = stored_nodes
    payload["parser_artifacts"] = {PARSER_NAVIGATION_ARTIFACT: stored_nodes}
    extraction = StructuredExtraction.model_validate(payload)

    expected_paths = [["Chapter 1"], ["Chapter 1", "Section 1.1"]]
    assert [node.section_path for node in extraction.navigation] == expected_paths
    assert [node.section_path for node in parser_navigation(extraction)] == expected_paths
    sections = extraction_sections(extraction, "parser", SectionRulesStore())
    assert [(section.id, section.title) for section in sections] == [
        ("nav-1", "Chapter 1"),
        ("nav-2", "Section 1.1"),
    ]
    assert extraction_sections(extraction, "parser", SectionRulesStore()) == sections


def test_saved_sections_and_section_map_titles_are_clean() -> None:
    """人の修正の章節と、章節と chunk の対応の表の名前も、作り直しを待たずに線を除く。"""
    saved = DocumentSection.model_validate(
        {"id": "nav-1", "title": "Chapter\u0332 1 か\u3099", "level": 1}
    )
    assert saved.title == "Chapter 1 か\u3099"

    rows = [
        {"section_id": "document", "title": "doc.pdf", "section_level": 0, "section_ordinal": 0},
        {
            "section_id": "nav-1",
            "title": "Chapter\u0332 1",
            "section_level": 1,
            "section_ordinal": 1,
            "parent_section_id": "document",
            "page_start": 1,
            "page_end": 2,
        },
    ]
    nodes = [_section_node(row) for row in rows]
    assert nodes[1].title == "Chapter 1"
    outline = section_expansion.SectionOutline(
        document_id="doc-1",
        chunk_set_id="cs-1",
        owner_section_id="nav-1",
        sections=tuple(
            section_expansion.SectionInfo(
                section_id=node.section_id,
                title=node.title,
                level=node.level,
                parent_section_id=node.parent_section_id,
                page_start=node.page_start,
                page_end=node.page_end,
            )
            for node in nodes
        ),
        file_name="doc.pdf",
    )
    label = section_expansion.section_label(outline, outline.sections[1])
    assert "Chapter 1" in label
    assert not any(mark in label for mark in _LINES)
