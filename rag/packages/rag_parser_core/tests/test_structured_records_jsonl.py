"""解析エンジン「構造化レコード JSONL」の検索用テキストと記録の境界の検証（#2457）。

データはすべて合成。
"""

from __future__ import annotations

import pytest

from rag_parser_core.capabilities import ADAPTER_CAPABILITIES, adapter_supports_source
from rag_parser_core.registry import run_external_adapter
from rag_parser_core.sheet_records import (
    LayoutDetection,
    SheetBlock,
    SheetColumn,
    SheetPreambleRow,
    SheetRecords,
    SheetRecordsDocument,
)
from rag_parser_core.structured_records import (
    JSONL_RECORD_BOUNDARY,
    STRUCTURED_RECORD_ID_KEY,
    STRUCTURED_RECORDS_BOUNDARY_ARTIFACT_KEY,
    STRUCTURED_RECORDS_BOUNDARY_COUNT_ARTIFACT_KEY,
    STRUCTURED_RECORDS_CONTENT_TYPE,
    DocumentLine,
    FormFieldLine,
    RecordBoundaryError,
    RecordField,
    RecordLocation,
    RowLine,
    StructuredRecordsError,
    dump_structured_records,
    escape_record_boundary,
    has_record_boundaries,
    join_record_boundary_text,
    parse_structured_records,
    record_boundary_plain_text,
    sheet_records_to_lines,
    split_record_boundary_text,
    unescape_record_boundary,
)

BACKEND = "structured_records_jsonl"


def _csv(*values: str) -> bytes:
    lines: list[DocumentLine | RowLine] = [
        DocumentLine(
            record_id="document",
            source_type="csv",
            converter="csv_to_jsonl",
            converter_version="v2",
            header_row=1,
        )
    ]
    for index, value in enumerate(values, start=1):
        lines.append(
            RowLine(
                record_id=f"row-{index:06d}",
                source_type="csv",
                location=RecordLocation(row_start=index + 1, row_end=index + 1),
                fields=[
                    RecordField(name="項目", position=1, value=f"値 {index}"),
                    RecordField(name="備考", position=2, value=value),
                ],
            )
        )
    return dump_structured_records(lines)


def _sheet() -> bytes:
    document = SheetRecordsDocument(
        source_format="xlsx",
        sheets=[
            SheetRecords(
                name="表 1",
                header_row=2,
                columns=[
                    SheetColumn(name="名前", column="A"),
                    SheetColumn(name="既定値", column="C", role="default", role_method="detected"),
                ],
                preamble=[SheetPreambleRow(row_number=1, cell_range="A1:A1", text="説明")],
                blocks=[
                    SheetBlock(
                        kind="row",
                        row_start=3,
                        row_end=3,
                        cell_range="A3:C3",
                        values={"名前": "設定 A", "既定値": "30"},
                    ),
                    SheetBlock(
                        kind="row",
                        row_start=4,
                        row_end=4,
                        cell_range="A4:C4",
                        values={"名前": "設定 B", "既定値": "60"},
                    ),
                ],
            )
        ],
    )
    return dump_structured_records(
        sheet_records_to_lines(document, converter="excel_to_jsonl", converter_version="v4")
    )


@pytest.mark.parametrize(
    "text",
    [
        "本文",
        JSONL_RECORD_BOUNDARY,
        "<<<\\JSONL_RECORD_BOUNDARY>>>",
        "<<<\\\\\\JSONL_RECORD_BOUNDARY>>>",
        f"前 {JSONL_RECORD_BOUNDARY} 後\n{JSONL_RECORD_BOUNDARY}\n<<<{JSONL_RECORD_BOUNDARY}>>>",
    ],
)
def test_escape_is_reversible_and_hides_the_marker(text: str) -> None:
    escaped = escape_record_boundary(text)
    assert JSONL_RECORD_BOUNDARY not in escaped
    assert unescape_record_boundary(escaped) == text


def test_escape_adds_one_backslash_per_level() -> None:
    assert escape_record_boundary(JSONL_RECORD_BOUNDARY) == "<<<\\JSONL_RECORD_BOUNDARY>>>"
    assert (
        escape_record_boundary("<<<\\JSONL_RECORD_BOUNDARY>>>") == "<<<\\\\JSONL_RECORD_BOUNDARY>>>"
    )


def test_split_keeps_original_text_equal_to_the_marker() -> None:
    texts = ["一件目", f"原文の {JSONL_RECORD_BOUNDARY} を含む", JSONL_RECORD_BOUNDARY, "四件目"]
    raw = join_record_boundary_text(texts)
    assert raw.count(f"\n{JSONL_RECORD_BOUNDARY}\n") == 3
    assert not raw.startswith(JSONL_RECORD_BOUNDARY) and not raw.endswith(JSONL_RECORD_BOUNDARY)
    parts = split_record_boundary_text(raw)
    assert [part.text for part in parts] == texts
    for part in parts:
        assert raw[part.start_offset : part.end_offset] == escape_record_boundary(part.text)


def test_split_without_boundary_is_one_record() -> None:
    assert [part.text for part in split_record_boundary_text("一件だけ")] == ["一件だけ"]


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (f"一件目 {JSONL_RECORD_BOUNDARY}\n二件目", "boundary_not_on_own_line"),
        (f"一件目\n{JSONL_RECORD_BOUNDARY}\n\n{JSONL_RECORD_BOUNDARY}\n三件目", "empty_record"),
    ],
)
def test_split_rejects_broken_boundaries(raw: str, code: str) -> None:
    with pytest.raises(RecordBoundaryError) as error:
        split_record_boundary_text(raw)
    assert error.value.code == code


def test_csv_extraction_separates_records_with_boundaries() -> None:
    result = run_external_adapter(BACKEND, _csv("一行目\n二行目", JSONL_RECORD_BOUNDARY), None, "")
    extraction = result.extraction
    assert extraction is not None
    assert result.parser_backend == BACKEND
    assert result.parser_version == "structured_records_jsonl_v1"
    assert has_record_boundaries(extraction)
    assert extraction.parser_artifacts[STRUCTURED_RECORDS_BOUNDARY_ARTIFACT_KEY] == (
        JSONL_RECORD_BOUNDARY
    )
    assert extraction.parser_artifacts[STRUCTURED_RECORDS_BOUNDARY_COUNT_ARTIFACT_KEY] == 1
    assert extraction.raw_text == (
        "項目: 値 1 / 備考: 一行目\n二行目\n"
        f"{JSONL_RECORD_BOUNDARY}\n"
        "項目: 値 2 / 備考: <<<\\JSONL_RECORD_BOUNDARY>>>"
    )
    # 要素の本文は原文のまま（エスケープしない）。
    assert extraction.elements[1].text == f"項目: 値 2 / 備考: {JSONL_RECORD_BOUNDARY}"
    assert extraction.elements[0].metadata[STRUCTURED_RECORD_ID_KEY] == "row-000001"
    assert (
        extraction.elements[0].metadata["row_start"],
        extraction.elements[0].metadata["row_end"],
    ) == (2, 2)
    assert record_boundary_plain_text(extraction) == "\n".join(
        element.text for element in extraction.elements
    )


def test_single_record_has_no_boundary() -> None:
    extraction = run_external_adapter(BACKEND, _csv("一件"), None, "").extraction
    assert extraction is not None
    assert JSONL_RECORD_BOUNDARY not in extraction.raw_text
    assert extraction.parser_artifacts[STRUCTURED_RECORDS_BOUNDARY_COUNT_ARTIFACT_KEY] == 0


def test_excel_extraction_keeps_record_metadata() -> None:
    extraction = run_external_adapter(BACKEND, _sheet(), None, "").extraction
    assert extraction is not None
    assert [element.element_id for element in extraction.elements] == [
        "sheet-000-r1",
        "sheet-000-r3",
        "sheet-000-r4",
    ]
    assert extraction.raw_text.count(f"\n{JSONL_RECORD_BOUNDARY}\n") == 2
    record = extraction.elements[1].metadata
    assert record["sheet_name"] == "表 1"
    assert record["cell_range"] == "A3:C3"
    assert record["sheet_column_roles"] == "既定値=default"
    assert record[STRUCTURED_RECORD_ID_KEY] == "sheet-000-r3"


def test_oversized_record_is_rejected_without_truncation() -> None:
    result = run_external_adapter(BACKEND, _csv("短い", "長" * 9000), None, "")
    assert result.extraction is None
    assert result.warnings == ("structured_records_invalid:record_too_large@line3",)


def test_invalid_jsonl_is_rejected() -> None:
    result = run_external_adapter(BACKEND, b'{"items": [1]}\n', None, "application/json")
    assert result.extraction is None
    assert result.warnings == ("structured_records_invalid:unknown_schema@line1",)


def test_capability_accepts_preprocessed_jsonl_and_is_ordered_before_xml_tree() -> None:
    assert adapter_supports_source(
        BACKEND, source_profile=None, content_type=STRUCTURED_RECORDS_CONTENT_TYPE
    )
    assert not adapter_supports_source(BACKEND, source_profile=None, content_type="application/pdf")
    order = list(ADAPTER_CAPABILITIES)
    assert order.index("unstructured") < order.index(BACKEND) < order.index("xml_tree_json")


def _form_sheet(value: str = "値 A") -> bytes:
    """帳票（#2516）の 1 行に 2 つの項目と、分けた表の番号を持つシート。"""
    document = SheetRecordsDocument(
        source_format="xlsx",
        sheets=[
            SheetRecords(
                name="様式",
                mode="form",
                layout_detection=LayoutDetection(
                    layout="form",
                    method="detected",
                    confidence=0.9,
                    reason="合成",
                    features={"merged": 1.0},
                ),
                table_index=1,
                region="B1:N3",
                blocks=[
                    SheetBlock(
                        kind="form_field",
                        row_start=3,
                        row_end=3,
                        cell_range="B3:F3",
                        section_path=["見出し"],
                        label="項目 A",
                        value=value,
                        label_cell="B3:C3",
                        value_cell="D3:F3",
                    ),
                    SheetBlock(
                        kind="form_field",
                        row_start=3,
                        row_end=3,
                        cell_range="H3:N3",
                        label="項目 B",
                        value="値 B",
                        label_cell="H3:I3",
                        value_cell="J3:N3",
                        value_original="■値 B □値 C",
                        shape_cells=["J3:N3"],
                    ),
                ],
            )
        ],
    )
    return dump_structured_records(
        sheet_records_to_lines(document, converter="excel_to_jsonl", converter_version="v5")
    )


def test_form_fields_round_trip_as_records_with_cells() -> None:
    parsed = parse_structured_records(_form_sheet())
    assert not isinstance(parsed, StructuredRecordsError)
    fields = [line for line in parsed.lines if isinstance(line, FormFieldLine)]
    assert [(line.record_id, line.label, line.value_cell) for line in fields] == [
        ("sheet-000-r3-B", "項目 A", "D3:F3"),
        ("sheet-000-r3-H", "項目 B", "J3:N3"),
    ]
    extraction = run_external_adapter(BACKEND, _form_sheet(), None, "").extraction
    assert extraction is not None
    assert [element.text for element in extraction.elements] == ["項目 A: 値 A", "項目 B: 値 B"]
    first = extraction.elements[0]
    assert first.section_path == ["様式", "見出し"]
    assert first.metadata["form_label_cell"] == "B3:C3"
    assert first.metadata["sheet_table_index"] == 1
    assert first.metadata["sheet_region"] == "B1:N3"
    assert first.metadata[STRUCTURED_RECORD_ID_KEY] == "sheet-000-r3-B"
    # 選択の記号を読む前の原文と、図形の文字を置いたセル範囲（#2526）。
    assert "form_value_original" not in first.metadata
    assert "sheet_shape_cells" not in first.metadata
    assert (fields[1].value_original, fields[1].shape_cells) == ("■値 B □値 C", ["J3:N3"])
    assert fields[0].shape_cells is None
    second = extraction.elements[1]
    assert second.metadata["form_value_original"] == "■値 B □値 C"
    assert second.metadata["sheet_shape_cells"] == "J3:N3"


def test_oversized_form_field_is_rejected_and_csv_cannot_have_form_fields() -> None:
    result = run_external_adapter(BACKEND, _form_sheet("長" * 9000), None, "")
    assert result.extraction is None
    assert result.warnings == ("structured_records_invalid:record_too_large@line3",)
    lines: list[DocumentLine | FormFieldLine] = [
        DocumentLine(
            record_id="document",
            source_type="csv",
            converter="csv_to_jsonl",
            converter_version="v2",
        ),
        FormFieldLine(
            record_id="field-1",
            source_type="csv",
            location=RecordLocation(row_start=2, row_end=2),
            label="項目",
            value="値",
        ),
    ]
    parsed = parse_structured_records(dump_structured_records(lines))
    assert parsed == StructuredRecordsError("unsupported_record_type", 2)
