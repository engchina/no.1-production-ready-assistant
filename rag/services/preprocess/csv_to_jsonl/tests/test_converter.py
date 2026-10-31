"""CSV→構造化レコード JSONL 変換の決定論検証（合成の CSV だけを使う。#2457）。"""

from __future__ import annotations

import json

from rag_parser_core.structured_records import (
    STRUCTURED_RECORDS_CONTENT_TYPE,
    RowLine,
    StructuredRecordsDocument,
    parse_structured_records,
)

from app.converters import convert


def _document(source: bytes, profile: str = "csv_to_jsonl") -> StructuredRecordsDocument:
    outcome = convert(source, "text/csv", profile, None)
    assert outcome.converted is True
    assert outcome.derived_bytes is not None
    assert outcome.derived_content_type == STRUCTURED_RECORDS_CONTENT_TYPE
    document = parse_structured_records(outcome.derived_bytes)
    assert isinstance(document, StructuredRecordsDocument)
    return document


def _rows(document: StructuredRecordsDocument) -> list[RowLine]:
    return [line for line in document.lines if isinstance(line, RowLine)]


def _values(row: RowLine) -> dict[str, str]:
    return {item.name: item.value for item in row.fields}


def test_csv_to_jsonl_records_with_header_and_lines() -> None:
    document = _document(b"name,age\nAlice,30\nBob,25\n")
    assert document.document.source_type == "csv"
    assert document.document.header_row == 1
    rows = _rows(document)
    assert [_values(row) for row in rows] == [
        {"name": "Alice", "age": "30"},
        {"name": "Bob", "age": "25"},
    ]
    assert [(row.location.row_start, row.location.row_end) for row in rows] == [(2, 2), (3, 3)]
    assert [item.position for item in rows[0].fields] == [1, 2]
    # 表頭のセルの文字（根拠のセルの表頭。#3262）。
    assert document.document.header == ["name", "age"]


def test_csv_to_jsonl_numbers_records_like_rfc_7111() -> None:
    """記録の番号は RFC 7111 の行（表頭の記録が 1、空の行も 1 記録）、物理行は別に持つ（#3262）。"""
    document = _document(b'h1,h2\n"a\nb",1\n\nc,2\n')
    rows = _rows(document)
    assert [
        (row.location.record, row.location.row_start, row.location.row_end) for row in rows
    ] == [
        (2, 2, 3),
        (4, 5, 5),
    ]


def test_csv_to_jsonl_keeps_the_header_text_before_filling_names() -> None:
    """空・重複の列名は補うが、表頭のセルの文字は原本のまま残す（#3262）。"""
    document = _document(b"a,,a\n1,2,3\n")
    assert [item.name for item in _rows(document)[0].fields] == ["a", "col_2", "a_1"]
    assert document.document.header == ["a", "", "a"]


def test_csv_to_jsonl_is_valid_json_lines() -> None:
    outcome = convert(b"a,b\n1,2\n3,4\n", "text/csv", "csv_to_jsonl", None)
    assert outcome.derived_bytes is not None
    text = outcome.derived_bytes.decode("utf-8")
    assert not text.startswith("﻿")
    assert text.endswith("\n") and "\r" not in text
    lines = text.split("\n")[:-1]
    assert len(lines) == 3  # document + 2 行
    for line in lines:
        assert isinstance(json.loads(line), dict)


def test_csv_to_jsonl_keeps_multiline_values_on_one_line() -> None:
    source = 'id,note\n1,"一行目\n二行目"\n2,単一\n'.encode()
    outcome = convert(source, "text/csv", "csv_to_jsonl", None)
    assert outcome.derived_bytes is not None
    assert len(outcome.derived_bytes.decode("utf-8").split("\n")) == 4  # 3 行 + 末尾の LF
    rows = _rows(_document(source))
    assert _values(rows[0])["note"] == "一行目\n二行目"
    # 引用符の中の改行を含む記録は原本の物理行 2〜3。次の記録は 4 行目。
    assert (rows[0].location.row_start, rows[0].location.row_end) == (2, 3)
    assert (rows[1].location.row_start, rows[1].location.row_end) == (4, 4)


def test_csv_to_jsonl_skips_blank_rows_without_renumbering() -> None:
    rows = _rows(_document(b"\nx,y\n1,2\n,\n3,4\n"))
    assert _document(b"\nx,y\n1,2\n,\n3,4\n").document.header_row == 2
    assert [row.location.row_start for row in rows] == [3, 5]


def test_csv_to_jsonl_is_deterministic() -> None:
    source = b"a,b\n1,2\n3,4\n"
    first = convert(source, "text/csv", "csv_to_jsonl", None).derived_bytes
    second = convert(source, "text/csv", "csv_to_jsonl", None).derived_bytes
    assert first == second


def test_csv_to_jsonl_handles_short_rows_and_duplicate_headers() -> None:
    rows = _rows(_document(b"id,id,note\n1,2\n"))
    # 重複ヘッダは決定論で一意化、欠損セルは空文字で補完。
    assert _values(rows[0]) == {"id": "1", "id_1": "2", "note": ""}


def test_csv_to_jsonl_keeps_cells_beyond_the_header() -> None:
    outcome = convert(b"a,b\n1,2,3\n", "text/csv", "csv_to_jsonl", None)
    assert "csv_row_wider_than_header:1" in outcome.warnings
    rows = _rows(_document(b"a,b\n1,2,3\n"))
    assert _values(rows[0]) == {"a": "1", "b": "2", "col_3": "3"}


def test_csv_to_jsonl_detects_semicolon_delimiter() -> None:
    rows = _rows(_document(b"x;y\n10;20\n"))
    assert _values(rows[0]) == {"x": "10", "y": "20"}


def test_csv_to_jsonl_keeps_unicode() -> None:
    rows = _rows(_document("列A,列B\n値①,é\n".encode()))
    assert _values(rows[0]) == {"列A": "値①", "列B": "é"}


def test_header_only_passes_through() -> None:
    outcome = convert(b"a,b\n", "text/csv", "csv_to_jsonl", None)
    assert outcome.converted is False
    assert "csv_no_rows" in outcome.warnings


def test_non_csv_profile_passes_through() -> None:
    outcome = convert(b"a,b\n1,2\n", "text/csv", "office_to_pdf", None)
    assert outcome.converted is False


def test_empty_input_passes_through() -> None:
    outcome = convert(b"", "text/csv", "csv_to_jsonl", None)
    assert outcome.converted is False
    assert "csv_empty" in outcome.warnings
