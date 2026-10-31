"""構造化レコードの JSONL の契約（書き出し・厳格な読み取り・要素化）の検証（#2457）。

データはすべて合成。
"""

from __future__ import annotations

import json

import pytest

from rag_parser_core.capabilities import adapter_supports_source
from rag_parser_core.registry import run_external_adapter
from rag_parser_core.sheet_records import (
    SheetBlock,
    SheetColumn,
    SheetNoteRow,
    SheetPreambleRow,
    SheetRecords,
    SheetRecordsDocument,
    column_letters,
    sheet_records_extraction,
)
from rag_parser_core.structured_records import (
    STRUCTURED_RECORD_ID_KEY,
    STRUCTURED_RECORDS_ARTIFACT_KEY,
    STRUCTURED_RECORDS_CONTENT_TYPE,
    DocumentLine,
    RecordField,
    RecordLocation,
    RowLine,
    StructuredRecordsDocument,
    StructuredRecordsError,
    dump_structured_records,
    is_structured_records_content_type,
    parse_structured_records,
    sheet_records_to_lines,
    structured_records_extraction,
    structured_records_to_sheet_records,
)


def _csv_lines() -> list[DocumentLine | RowLine]:
    return [
        DocumentLine(
            record_id="document",
            source_type="csv",
            converter="csv_to_jsonl",
            converter_version="v2",
            header_row=1,
        ),
        RowLine(
            record_id="row-000001",
            source_type="csv",
            location=RecordLocation(row_start=2, row_end=3),
            fields=[
                RecordField(name="項目", position=1, value="値 A"),
                RecordField(name="備考", position=2, value="一行目\n二行目"),
            ],
        ),
        RowLine(
            record_id="row-000002",
            source_type="csv",
            location=RecordLocation(row_start=5, row_end=5),
            fields=[
                RecordField(name="項目", position=1, value="値 B"),
                RecordField(name="備考", position=2, value=""),
            ],
        ),
    ]


def _sheet_document() -> SheetRecordsDocument:
    return SheetRecordsDocument(
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
                    )
                ],
            ),
            SheetRecords(
                name="手順",
                header_row=1,
                mode="procedure",
                blocks=[
                    SheetBlock(
                        kind="procedure_step",
                        row_start=2,
                        row_end=4,
                        cell_range="A2:D4",
                        section_path=["章 1", "手順 1"],
                        lines=["作業項目: 手順 1", "作業内容: 確認する"],
                    )
                ],
            ),
        ],
    )


def _jsonl(lines: list[dict[str, object]]) -> bytes:
    return "".join(json.dumps(line, ensure_ascii=False) + "\n" for line in lines).encode()


def _raw(data: bytes) -> list[dict[str, object]]:
    return [json.loads(line) for line in data.decode("utf-8").split("\n")[:-1]]


def test_dump_writes_one_object_per_lf_line() -> None:
    data = dump_structured_records(_csv_lines())
    text = data.decode("utf-8")
    assert not data.startswith(b"\xef\xbb\xbf")
    assert text.endswith("\n") and "\r" not in text and "\n\n" not in text
    lines = text.split("\n")[:-1]
    assert len(lines) == 3
    # 値の改行は JSON のエスケープのまま 1 行に収まる。
    assert "一行目\\n二行目" in lines[1]
    assert all(isinstance(json.loads(line), dict) for line in lines)
    assert all(
        json.loads(line)["schema"] == "production-ready.structured-records" for line in lines
    )


def test_round_trip_and_csv_extraction() -> None:
    document = parse_structured_records(dump_structured_records(_csv_lines()))
    assert isinstance(document, StructuredRecordsDocument)
    rows = [line for line in document.lines if isinstance(line, RowLine)]
    assert rows[0].fields[1].value == "一行目\n二行目"

    extraction = structured_records_extraction(
        document, source_parser="structured_records", parser_backend="docling", parser_version="v1"
    )
    assert [element.text for element in extraction.elements] == [
        "項目: 値 A / 備考: 一行目\n二行目",
        "項目: 値 B",
    ]
    first = extraction.elements[0]
    assert first.content_kind == "record"
    assert first.element_id == "row-000001"
    assert first.metadata[STRUCTURED_RECORD_ID_KEY] == "row-000001"
    assert (first.metadata["row_start"], first.metadata["row_end"]) == (2, 3)
    assert first.metadata["csv_header_row"] == 1
    # 記録の番号（record）を記録していない古い記録は、物理行の範囲だけを持つ（#3262）。
    assert first.metadata["row_basis"] == "csv"
    assert (first.metadata["source_line_start"], first.metadata["source_line_end"]) == (2, 3)
    assert not {"table_row", "table_row_start", "table_cells_json"} & set(first.metadata)
    assert extraction.parser_artifacts[STRUCTURED_RECORDS_ARTIFACT_KEY] == 1
    assert extraction.parser_artifacts["structured_records_converter"] == "csv_to_jsonl"


@pytest.mark.parametrize(
    ("position", "letters"), [(1, "A"), (26, "Z"), (27, "AA"), (702, "ZZ"), (703, "AAA")]
)
def test_column_letters(position: int, letters: str) -> None:
    assert column_letters(position) == letters


def test_csv_row_cells_cite_the_original_header_text() -> None:
    """CSV の記録は、記録の番号・物理行と、値のあるセルの列の記号・表頭のセルの文字を持つ（#3262）。

    空・重複の表頭は記録の列名では補った名前になるが、表頭の段には原本の文字（空は空）を使う。
    表頭より右の列（列の多い行）は表頭の文字が無い。
    """
    lines = [
        DocumentLine(
            record_id="document",
            source_type="csv",
            converter="csv_to_jsonl",
            converter_version="v3",
            header_row=1,
            header=["項目", "", "項目"],
        ),
        RowLine(
            record_id="row-000001",
            source_type="csv",
            # 前に引用符の中の改行を含む記録があり、物理行（4）と記録の番号（3）がずれる。
            location=RecordLocation(row_start=4, row_end=5, record=3),
            fields=[
                RecordField(name="項目", position=1, value="値 A"),
                RecordField(name="col_2", position=2, value="値 B"),
                RecordField(name="項目_1", position=3, value=""),
                RecordField(name="col_4", position=4, value="値 D"),
            ],
        ),
    ]
    document = parse_structured_records(dump_structured_records(lines))
    assert isinstance(document, StructuredRecordsDocument)
    assert document.document.header == ["項目", "", "項目"]
    (element,) = structured_records_extraction(
        document, source_parser="s", parser_backend="docling", parser_version="v1"
    ).elements
    # 行は記録の番号（RFC 7111 の行）。複数の物理行の記録もセルの座標を持つ。
    assert (
        element.metadata["table_row"],
        element.metadata["table_row_start"],
        element.metadata["table_row_end"],
    ) == (3, 3, 3)
    assert (element.metadata["source_line_start"], element.metadata["source_line_end"]) == (4, 5)
    assert json.loads(str(element.metadata["table_cells_json"])) == [
        ["A", ["項目"]],
        ["B", []],
        ["D", []],
    ]


def test_excel_round_trip_keeps_sheet_records_and_elements() -> None:
    """Excel は行の記録と相互に変換でき、要素は今の `sheet_records_extraction` と同じ。"""
    original = _sheet_document()
    data = dump_structured_records(
        sheet_records_to_lines(original, converter="excel_to_jsonl", converter_version="v4")
    )
    raw = _raw(data)
    assert [item["record_type"] for item in raw] == [
        "document",
        "sheet",
        "preamble",
        "row",
        "sheet",
        "procedure_step",
    ]
    assert raw[3]["fields"] == [
        {"name": "名前", "position": 1, "column": "A", "value": "設定 A"},
        {"name": "既定値", "position": 3, "column": "C", "role": "default", "value": "30"},
    ]
    document = parse_structured_records(data)
    assert isinstance(document, StructuredRecordsDocument)
    assert structured_records_to_sheet_records(document) == original

    extraction = structured_records_extraction(
        document, source_parser="s", parser_backend="docling", parser_version="v1"
    )
    expected = sheet_records_extraction(
        original, source_parser="s", parser_backend="docling", parser_version="v1"
    )
    assert [element.element_id for element in extraction.elements] == [
        element.element_id for element in expected.elements
    ]
    assert [element.text for element in extraction.elements] == [
        element.text for element in expected.elements
    ]
    for element, before in zip(extraction.elements, expected.elements, strict=True):
        assert element.metadata == {**before.metadata, STRUCTURED_RECORD_ID_KEY: element.element_id}
    assert extraction.elements[1].text == "名前: 設定 A / 既定値［資料の既定値］: 30"
    assert extraction.parser_artifacts["sheet_records_format_version"] == 1
    assert extraction.parser_artifacts[STRUCTURED_RECORDS_ARTIFACT_KEY] == 1


def test_excel_table_notes_round_trip_as_note_records() -> None:
    """表の下の注記は note の記録になり、注記の印のある行は注記の文を本文に含める（#3566）。"""
    note = "※1 合成の注記の文。"
    original = SheetRecordsDocument(
        source_format="xlsx",
        sheets=[
            SheetRecords(
                name="表",
                header_row=1,
                columns=[
                    SheetColumn(name="名前", column="A", header_path=["名前"]),
                    SheetColumn(name="値", column="B", header_path=["値"]),
                ],
                blocks=[
                    SheetBlock(
                        kind="row",
                        row_start=2,
                        row_end=2,
                        cell_range="A2:B2",
                        values={"名前": "行 A", "値": "10※1"},
                        notes=[note],
                    ),
                    SheetBlock(
                        kind="row",
                        row_start=3,
                        row_end=3,
                        cell_range="A3:B3",
                        values={"名前": "行 B", "値": "20"},
                    ),
                ],
                notes=[SheetNoteRow(row_number=5, cell_range="A5:A5", text=note, marker="※1")],
            )
        ],
    )
    data = dump_structured_records(
        sheet_records_to_lines(original, converter="excel_to_jsonl", converter_version="v9")
    )
    raw = _raw(data)
    assert [item["record_type"] for item in raw] == ["document", "sheet", "row", "row", "note"]
    assert raw[2]["notes"] == [note]
    assert "notes" not in raw[3]
    assert raw[4]["record_id"] == "sheet-000-r5" and raw[4]["marker"] == "※1"
    document = parse_structured_records(data)
    assert isinstance(document, StructuredRecordsDocument)
    assert structured_records_to_sheet_records(document) == original
    elements = structured_records_extraction(
        document, source_parser="s", parser_backend="docling", parser_version="v1"
    ).elements
    assert [element.text for element in elements] == [
        f"名前: 行 A / 値: 10※1\n{note}",
        "名前: 行 B / 値: 20",
        note,
    ]
    assert elements[2].metadata["sheet_block_kind"] == "note"
    assert elements[2].metadata[STRUCTURED_RECORD_ID_KEY] == "sheet-000-r5"


def _base(**overrides: object) -> dict[str, object]:
    line: dict[str, object] = {
        "schema": "production-ready.structured-records",
        "schema_version": 1,
        "record_id": "document",
        "source_type": "csv",
        "record_type": "document",
        "converter": "csv_to_jsonl",
        "converter_version": "v2",
    }
    line.update(overrides)
    return line


def _row(**overrides: object) -> dict[str, object]:
    line: dict[str, object] = {
        "schema": "production-ready.structured-records",
        "schema_version": 1,
        "record_id": "row-000001",
        "source_type": "csv",
        "record_type": "row",
        "location": {"row_start": 2, "row_end": 2},
        "fields": [{"name": "a", "position": 1, "value": "1"}],
    }
    line.update(overrides)
    return line


@pytest.mark.parametrize(
    ("data", "code", "line"),
    [
        (b"", "empty", None),
        (b"\xef\xbb\xbf" + _jsonl([_base()]), "bom", None),
        (b"\xff\xfe", "not_utf8", None),
        (_jsonl([_base()]) + b"\n" + _jsonl([_row()]), "blank_line", 2),
        (_jsonl([_base()]) + b"{not json}\n", "invalid_json", 2),
        (_jsonl([_base()]) + b"[1, 2]\n", "not_object", 2),
        (_jsonl([_base(schema="other.schema")]), "unknown_schema", 1),
        (_jsonl([{"a": 1}]), "unknown_schema", 1),
        (_jsonl([_base(schema_version=2)]), "unsupported_schema_version", 1),
        (_jsonl([_base(schema_version=True)]), "unsupported_schema_version", 1),
        (_jsonl([_base(), _row(record_id="document")]), "duplicate_record_id", 2),
        (_jsonl([_row()]), "document_missing", 1),
        (_jsonl([_base(), _base(record_id="document-2")]), "document_not_first", 2),
        (_jsonl([_base(), _row(parent_id="sheet-000")]), "parent_not_found", 2),
        (_jsonl([_base(), _row(source_type="xlsx")]), "source_type_mismatch", 2),
        (_jsonl([_base(), _row(unknown=1)]), "invalid_record", 2),
        (_jsonl([_base(), _row(record_type="paragraph")]), "invalid_record", 2),
        (
            _jsonl([_base(), _row(location={"row_start": 3, "row_end": 2})]),
            "invalid_location",
            2,
        ),
        (
            _jsonl([_base(source_type="xlsx"), _row(source_type="xlsx")]),
            "parent_missing",
            2,
        ),
        (
            _jsonl(
                [
                    _base(),
                    {
                        **{key: value for key, value in _row().items() if key != "fields"},
                        "record_type": "note",
                        "text": "※1 注記",
                    },
                ]
            ),
            "unsupported_record_type",
            2,
        ),
    ],
)
def test_parse_rejects_invalid_jsonl(data: bytes, code: str, line: int | None) -> None:
    result = parse_structured_records(data)
    assert result == StructuredRecordsError(code, line)


def test_parse_accepts_a_document_without_records_and_missing_final_lf() -> None:
    result = parse_structured_records(_jsonl([_base(), _row()]).rstrip(b"\n"))
    assert isinstance(result, StructuredRecordsDocument)
    assert len(result.lines) == 1


def test_content_type_detection_ignores_parameters() -> None:
    assert is_structured_records_content_type(f"{STRUCTURED_RECORDS_CONTENT_TYPE}; charset=utf-8")
    assert not is_structured_records_content_type("application/json")
    assert not is_structured_records_content_type("application/x-ndjson")
    assert not is_structured_records_content_type(None)


def test_docling_and_unstructured_read_jsonl_with_the_shared_contract() -> None:
    """Docling / Unstructured のサービスは構造化レコードの JSONL を共通の契約で読む（#2457・#21）。

    外部の parser には渡さない（package が無くても読める）。
    """
    data = dump_structured_records(_csv_lines())
    for backend in ("docling", "unstructured"):
        assert adapter_supports_source(
            backend, source_profile=None, content_type=STRUCTURED_RECORDS_CONTENT_TYPE
        )
        result = run_external_adapter(backend, data, None, STRUCTURED_RECORDS_CONTENT_TYPE)
        assert result.extraction is not None
        assert result.fallback_used is False
        assert result.parser_backend == backend
        assert result.parser_version == "structured_records_v1"
    for removed in ("structured_records_jsonl", "xml_tree_json"):
        assert not adapter_supports_source(
            removed, source_profile=None, content_type=STRUCTURED_RECORDS_CONTENT_TYPE
        )
