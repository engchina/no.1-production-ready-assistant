"""構造化レコードの JSONL の Word の拡張（section / passage）の契約の検証（#2457）。

データはすべて合成。
"""

from __future__ import annotations

import json

import pytest

from rag_parser_core.registry import run_external_adapter
from rag_parser_core.structured_records import (
    JSONL_RECORD_BOUNDARY,
    STRUCTURED_RECORD_ID_KEY,
    DocumentLine,
    PassageLine,
    RecordLocation,
    RecordPart,
    RowLine,
    SectionLine,
    SourceConversion,
    StructuredRecordsDocument,
    StructuredRecordsError,
    WordListItem,
    WordLocation,
    WordParagraph,
    WordTable,
    dump_structured_records,
    parse_structured_records,
    split_record_boundary_text,
    word_passage_text,
)

BACKEND = "structured_records_jsonl"
_SHA = "0" * 64


def _location(start: int, end: int | None = None) -> WordLocation:
    return WordLocation(
        body_start=start,
        body_end=end or start,
        paragraph_start=start,
        paragraph_end=end or start,
    )


def _document(source_type: str = "docx") -> DocumentLine:
    if source_type == "doc":
        return DocumentLine(
            record_id="document",
            source_type="doc",
            converter="word_to_jsonl",
            converter_version="v1",
            location_basis="derived",
            conversion=SourceConversion(
                original_type="doc",
                original_sha256=_SHA,
                derived_type="docx",
                derived_sha256="1" * 64,
                converter="libreoffice",
            ),
        )
    return DocumentLine(
        record_id="document",
        source_type="docx",
        converter="word_to_jsonl",
        converter_version="v1",
        location_basis="original",
    )


def _word_lines(source_type: str = "docx") -> list[DocumentLine | SectionLine | PassageLine]:
    return [
        _document(source_type),
        SectionLine(
            record_id="s0001",
            source_type=source_type,  # type: ignore[arg-type]
            heading="1 概要",
            level=1,
            heading_path=["1 概要"],
            location=_location(1),
        ),
        PassageLine(
            record_id="p0001",
            source_type=source_type,  # type: ignore[arg-type]
            parent_id="s0001",
            section_path=["1 概要"],
            location=WordLocation(
                body_start=2, body_end=4, paragraph_start=2, paragraph_end=3, table_indices=[1]
            ),
            blocks=[
                WordParagraph(text="表の導入", body_index=2, paragraph_index=2),
                WordTable(
                    body_index=3,
                    table_index=1,
                    header_rows=1,
                    rows=[["名前", "値"], ["a|b", "1"], ["", ""]],
                    row_start=1,
                    row_end=3,
                ),
                WordListItem(
                    text="項目",
                    body_index=4,
                    paragraph_index=3,
                    level=1,
                    ordered=True,
                    marker="(1)",
                ),
            ],
        ),
        SectionLine(
            record_id="s0002",
            source_type=source_type,  # type: ignore[arg-type]
            parent_id="s0001",
            heading="1.1 詳細",
            level=2,
            heading_path=["1 概要", "1.1 詳細"],
            location=_location(5),
        ),
        PassageLine(
            record_id="p0002",
            source_type=source_type,  # type: ignore[arg-type]
            parent_id="s0002",
            section_path=["1 概要", "1.1 詳細"],
            location=_location(6),
            blocks=[WordParagraph(text="前半の本文", body_index=6, paragraph_index=5)],
            part=RecordPart(
                group_id="s0002-content", index=1, count=2, group_location=_location(6, 7)
            ),
        ),
        PassageLine(
            record_id="p0003",
            source_type=source_type,  # type: ignore[arg-type]
            parent_id="s0002",
            section_path=["1 概要", "1.1 詳細"],
            location=_location(7),
            blocks=[
                WordParagraph(
                    text=f"原文の {JSONL_RECORD_BOUNDARY} を含む", body_index=7, paragraph_index=6
                )
            ],
            part=RecordPart(
                group_id="s0002-content", index=2, count=2, group_location=_location(6, 7)
            ),
        ),
    ]


def _parse(lines: list[object]) -> StructuredRecordsDocument | StructuredRecordsError:
    return parse_structured_records(dump_structured_records(lines))  # type: ignore[arg-type]


def _raw(lines: list[object]) -> list[dict[str, object]]:
    return [json.loads(line) for line in dump_structured_records(lines).decode().splitlines()]  # type: ignore[arg-type]


def _dump_raw(payloads: list[dict[str, object]]) -> bytes:
    return "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in payloads).encode()


def test_word_lines_round_trip() -> None:
    document = _parse(_word_lines())
    assert isinstance(document, StructuredRecordsDocument)
    assert document.source_type == "docx"
    assert [line.record_type for line in document.lines] == [
        "section",
        "passage",
        "section",
        "passage",
        "passage",
    ]
    doc = _parse(_word_lines("doc"))
    assert isinstance(doc, StructuredRecordsDocument)
    assert doc.document.conversion is not None


def test_passage_text_renders_heading_table_and_list() -> None:
    document = _parse(_word_lines())
    assert isinstance(document, StructuredRecordsDocument)
    passage = document.lines[1]
    assert isinstance(passage, PassageLine)
    assert word_passage_text(passage) == (
        "1 概要\n表の導入\n| 名前 | 値 |\n| --- | --- |\n| a\\|b | 1 |\n  (1) 項目"
    )


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        # .doc は派生物の位置と原本・派生物の対応を必ず持つ。
        (lambda rows: rows[0].pop("conversion"), "invalid_source_conversion"),
        (lambda rows: rows[0].update(location_basis="original"), "invalid_source_conversion"),
        (lambda rows: rows[3].update(parent_id="s9999"), "parent_not_found"),
        (lambda rows: rows[2].update(parent_id="s0002"), "parent_not_found"),
        (
            lambda rows: rows[2]["location"].update(body_start=9, body_end=2),  # type: ignore[union-attr]
            "invalid_location",
        ),
        (lambda rows: rows[4]["part"].update(index=3), "invalid_part"),  # type: ignore[union-attr]
        (lambda rows: rows[2].update(page_number=1), "invalid_record"),
        (lambda rows: rows[2].update(source_type="docx"), "source_type_mismatch"),
    ],
)
def test_invalid_word_lines_are_rejected(mutate: object, code: str) -> None:
    rows = _raw(_word_lines("doc"))
    mutate(rows)  # type: ignore[operator]
    result = parse_structured_records(_dump_raw(rows))
    assert isinstance(result, StructuredRecordsError)
    assert result.code == code


def test_word_and_spreadsheet_record_types_do_not_mix() -> None:
    row = RowLine(
        record_id="row-1",
        source_type="docx",
        location=RecordLocation(row_start=1, row_end=1),
    )
    result = _parse([_document(), row])
    assert isinstance(result, StructuredRecordsError) and result.code == "unsupported_record_type"
    section = SectionLine(
        record_id="s0001",
        source_type="csv",
        heading="h",
        level=1,
        heading_path=["h"],
        location=_location(1),
    )
    csv = DocumentLine(
        record_id="document", source_type="csv", converter="csv_to_jsonl", converter_version="v2"
    )
    result = _parse([csv, section])
    assert isinstance(result, StructuredRecordsError) and result.code == "unsupported_record_type"
    # 表計算の document の行は Word の位置の基準を持たない。
    rows = _raw([csv])
    rows[0]["location_basis"] = "original"
    result = parse_structured_records(_dump_raw(rows))
    assert isinstance(result, StructuredRecordsError) and result.code == "invalid_source_conversion"


def test_empty_passage_without_heading_is_rejected() -> None:
    passage = PassageLine(record_id="p0001", source_type="docx", location=_location(1), blocks=[])
    result = _parse([_document(), passage])
    assert isinstance(result, StructuredRecordsError) and result.code == "empty_passage"


def test_parser_makes_one_element_per_passage_with_boundaries() -> None:
    result = run_external_adapter(BACKEND, dump_structured_records(_word_lines()), None, "")
    extraction = result.extraction
    assert extraction is not None
    assert [element.element_id for element in extraction.elements] == ["p0001", "p0002", "p0003"]
    first, second, third = extraction.elements
    assert first.parent_id == "s0001"
    assert first.section_path == ["1 概要"]
    assert first.metadata[STRUCTURED_RECORD_ID_KEY] == "p0001"
    assert first.metadata["word_body_start"] == 2
    assert first.metadata["word_body_end"] == 4
    assert first.metadata["word_table_indices"] == "1"
    assert first.metadata["word_location_basis"] == "original"
    # 1 つの表を含むまとまりは、その表の番号と行の範囲を持つ（#3262）。表の無いまとまりは持たない。
    assert (
        first.metadata["row_basis"],
        first.metadata["table_index"],
        first.metadata["table_row_start"],
        first.metadata["table_row_end"],
    ) == ("word_table", 1, 1, 3)
    assert "row_basis" not in second.metadata and "table_index" not in second.metadata
    assert second.metadata["word_part_group"] == "s0002-content"
    assert (second.metadata["word_part_index"], second.metadata["word_part_count"]) == (1, 2)
    assert (second.metadata["word_part_body_start"], second.metadata["word_part_body_end"]) == (
        6,
        7,
    )
    assert "page_number" not in first.model_dump(exclude_none=True)
    # 記録の境界で 1 記録ずつに戻り、原文の同じ文字列は保つ（マーカーと誤らない）。
    parts = split_record_boundary_text(extraction.raw_text)
    assert [part.text for part in parts] == [element.text for element in extraction.elements]
    assert JSONL_RECORD_BOUNDARY in third.text


def test_parser_rejects_oversized_word_record() -> None:
    lines = _word_lines()
    big = PassageLine(
        record_id="p0009",
        source_type="docx",
        parent_id="s0001",
        section_path=["1 概要"],
        location=_location(9),
        blocks=[WordParagraph(text="長" * 9000, body_index=9, paragraph_index=9)],
    )
    result = run_external_adapter(BACKEND, dump_structured_records([*lines, big]), None, "")
    assert result.extraction is None
    assert any("record_too_large" in warning for warning in result.warnings)


def _table_passage(record_id: str, tables: list[WordTable]) -> PassageLine:
    return PassageLine(
        record_id=record_id,
        source_type="docx",
        parent_id="s0001",
        section_path=["1 概要"],
        location=WordLocation(
            body_start=10,
            body_end=10 + len(tables),
            table_indices=[table.table_index for table in tables],
        ),
        blocks=tables,
    )


def test_word_table_part_and_multiple_tables_rows() -> None:
    """大きい表を分けた部分は元の表の行の範囲、複数の表のまとまりは行の範囲を持たない（#3262）。"""
    header = ["列 1", "列 2"]
    part = WordTable(
        body_index=10,
        table_index=3,
        header_rows=1,
        rows=[header, ["値 A", "1"], ["値 B", "2"]],
        row_start=8,
        row_end=9,
    )
    other = WordTable(
        body_index=11, table_index=4, header_rows=0, rows=[["x", "y"]], row_start=1, row_end=1
    )
    lines: list[object] = [
        *_word_lines()[:2],
        _table_passage("p0101", [part]),
        _table_passage("p0102", [part.model_copy(update={"body_index": 12}), other]),
    ]
    data = dump_structured_records(lines)  # type: ignore[arg-type]
    result = run_external_adapter(BACKEND, data, None, "")
    assert result.extraction is not None
    single, multiple = result.extraction.elements
    assert single.content_kind == "table"
    assert (
        single.metadata["row_basis"],
        single.metadata["table_index"],
        single.metadata["table_row_start"],
        single.metadata["table_row_end"],
    ) == ("word_table", 3, 8, 9)
    assert not {"row_basis", "table_index", "table_row_start"} & set(multiple.metadata)
