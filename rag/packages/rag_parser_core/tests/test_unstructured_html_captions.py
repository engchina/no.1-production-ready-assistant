"""Unstructured の HTML 表題の欠落を、原本の表の格子と照合して補う。"""

from rag_parser_core.extraction import (
    DocumentElement,
    ExtractionTable,
    ExtractionTableCell,
    StructuredExtraction,
)
from rag_parser_core.registry import _restore_html_table_captions
from rag_parser_core.result import UNSTRUCTURED_TEXT_CONTRACT


def extraction(*, caption=None, cells=None):
    cells = cells or [
        ExtractionTableCell(row=0, col=0, text="項目"),
        ExtractionTableCell(row=1, col=0, text="Ａ"),
    ]
    return StructuredExtraction(
        raw_text="項目 Ａ",
        elements=[
            DocumentElement(
                kind="table",
                text="| 項目 |\n| Ａ |",
                element_id="table-a",
                source_parser="unstructured_adapter",
                metadata={"parser_backend": "unstructured"},
            )
        ],
        tables=[
            ExtractionTable(table_id="table-a", element_id="table-a", caption=caption, cells=cells)
        ],
        parser_artifacts={"external_adapter": "unstructured"},
    )


def source(caption="合成の表題Ａ", value="Ａ"):
    return (
        f"<table><caption>{caption}</caption>"
        f"<tr><th>項目</th></tr><tr><td>{value}</td></tr></table>"
    ).encode()


def test_caption_is_bound_to_original_grid_and_keeps_adapter_lineage():
    result = _restore_html_table_captions(extraction(), source())
    assert result.tables[0].caption == "合成の表題Ａ"
    assert result.tables[0].metadata["caption_element_id"] == "table-a-html-caption"
    assert result.elements[0].metadata["table_caption"] == "合成の表題Ａ"
    assert "合成の表題Ａ" in result.raw_text
    assert result.elements[1].kind == "table_caption"
    assert result.elements[1].parent_id == "table-a"
    assert all(e.source_parser == "unstructured_adapter" for e in result.elements)
    assert result.parser_artifacts["external_adapter"] == "unstructured"
    assert result.tables[0].cells == extraction().tables[0].cells
    assert not result.warnings


def test_equal_grids_with_different_captions_are_not_guessed_by_order():
    result = _restore_html_table_captions(extraction(), source("表題Ａ") + source("表題Ｂ"))
    assert result.tables[0].caption is None
    assert not any(e.kind == "table_caption" for e in result.elements)
    assert result.warnings
    assert result.parser_artifacts["html_captions_unresolved"] == 1


def test_different_character_width_is_not_the_same_grid():
    result = _restore_html_table_captions(extraction(), source(value="A"))
    assert result.tables[0].caption is None
    assert result.warnings


def test_existing_caption_is_not_duplicated():
    original = extraction(caption="既存の表題")
    result = _restore_html_table_captions(original, source())
    assert result is original
    assert result.tables[0].caption == "既存の表題"


def test_merged_cell_grid_keeps_spans():
    original = extraction(cells=[ExtractionTableCell(row=0, col=0, text="項目", col_span=2)])
    result = _restore_html_table_captions(
        original,
        b"<table><caption>Caption A</caption>"
        b'<tr><th colspan="2">&#38917;&#30446;</th></tr></table>',
    )
    assert result.tables[0].caption == "Caption A"
    assert result.tables[0].cells[0].col_span == 2


def test_empty_caption_changes_nothing():
    original = extraction()
    assert _restore_html_table_captions(original, source("")) is original


def test_unstructured_html_adapter_restores_caption_from_original(monkeypatch):
    import sys
    from types import ModuleType, SimpleNamespace

    from rag_parser_core.registry import _unstructured_adapter_result

    package = ModuleType("unstructured.partition.auto")
    package.partition = lambda **kwargs: [
        SimpleNamespace(
            id="table-a",
            category="Table",
            text="項目 Ａ",
            metadata=SimpleNamespace(
                text_as_html="<table><tr><th>項目</th><th>値</th></tr><tr><td>Ａ</td><td>2</td></tr></table>"
            ),
        )
    ]
    monkeypatch.setitem(sys.modules, "unstructured.partition.auto", package)
    data = (
        source()
        .decode()
        .replace("</th>", "</th><th>値</th>")
        .replace("</td>", "</td><td>2</td>")
        .encode()
    )
    result = _unstructured_adapter_result(data, source_profile=None, content_type="text/html")
    assert result.parser_backend == "unstructured"
    assert (
        result.extraction.parser_artifacts["unstructured_text_contract"]
        == UNSTRUCTURED_TEXT_CONTRACT
    )
    assert result.extraction.tables[0].caption == "合成の表題Ａ"
    assert "合成の表題Ａ" in result.extraction.raw_text


def test_equal_grid_without_caption_is_ambiguous():
    result = _restore_html_table_captions(extraction(), source("表題Ａ") + source(""))
    assert result.tables[0].caption is None
    assert result.warnings
