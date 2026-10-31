"""CSV→構造化レコード JSONL 前処理マイクロサービスの変換実装（#2457）。

CSV を決定論で構造化レコードの JSONL（`rag_parser_core.structured_records`）にする。1 データ行を
1 記録にし、表頭の列名つきの値（`fields`）と原本の物理行の位置（`location.row_start` / `row_end`。
引用符の中の改行を含む記録は複数行）を持つ。入力の列の区切り（`,`・タブ・`;`・`|`）と JSONL の行の
区切り（LF）と chunk の境界は別のもので、入力の区切りは JSONL に残さない。純 Python（`csv` +
pydantic）のみで完結し、重い変換依存（LibreOffice など）を持たない軽量サービス。

- 表頭は最初の空でない行。空・重複の列名は決定論で補う（`col_<位置>`・`<名前>_<n>`）。表頭の
  セルの文字（補う前）は `document` の `header` に残す（根拠のセルの表頭。#3262）。
- 記録の番号（`location.record`）は RFC 7111 の行（表頭の記録を含め、空の行も 1 記録として先頭から
  数える。表計算のソフトで開いたときの行）。物理行の範囲（`row_start` / `row_end`）とは引用符の中の
  改行を含む記録の後ろでずれる（#3262）。
- 表頭より列の多い行は、余った値を `col_<位置>` の列にして捨てない
  （診断 `csv_row_wider_than_header`）。
- 空の行は記録にしない（元の行番号は振り直さない）。

未対応・空・行数 0・CSV として読めないときは passthrough(変換せず原本を使う)へ縮退する。
"""

from __future__ import annotations

import csv
import io

from rag_parser_core.preprocess import ConvertOutcome
from rag_parser_core.source import SourceProfile
from rag_parser_core.structured_records import (
    STRUCTURED_RECORDS_CONTENT_TYPE,
    ContentLine,
    DocumentLine,
    RecordDiagnostic,
    RecordField,
    RecordLocation,
    RowLine,
    dump_structured_records,
)

CONVERTER_NAME = "csv_to_jsonl"
CONVERTER_VERSION = "v3"
# CSV として扱える上限デリミタ候補(Sniffer 失敗時の決定論フォールバックはカンマ)。
_DELIMITER_CANDIDATES = ",\t;|"


def convert(
    source_bytes: bytes,
    content_type: str,
    preprocess_profile: str,
    source_profile: SourceProfile | None,
) -> ConvertOutcome:
    """選択プリセットで変換する。csv_to_jsonl 以外・失敗・空は passthrough へ縮退する。"""
    if preprocess_profile != "csv_to_jsonl":
        return ConvertOutcome.passthrough(
            reason=f"preprocess_unsupported_profile:{preprocess_profile}"
        )
    return _csv_to_records(source_bytes)


def _decode_csv(source_bytes: bytes) -> tuple[str | None, list[str]]:
    """CSV バイト列を文字列へ復号する(charset-normalizer フォールバック付き)。"""
    warnings: list[str] = []
    try:
        return source_bytes.decode("utf-8-sig"), warnings
    except UnicodeDecodeError:
        try:
            from charset_normalizer import from_bytes

            best = from_bytes(source_bytes).best()
            if best is not None:
                warnings.append("csv_charset_fallback")
                return str(best), warnings
        except Exception:  # noqa: BLE001 - 判定失敗は utf-8(置換)へ縮退する
            pass
        warnings.append("csv_charset_fallback")
        return source_bytes.decode("utf-8", errors="replace"), warnings


def _sniff_delimiter(sample: str) -> str:
    """先頭サンプルからデリミタを推定する(失敗時はカンマで決定論固定)。"""
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=_DELIMITER_CANDIDATES)
        return str(dialect.delimiter)
    except csv.Error:
        return ","


def _read_rows(text: str, delimiter: str) -> list[tuple[int, int, int, list[str]]]:
    """CSV の記録を、記録の番号と原本の物理行の範囲（どちらも 1 始まり）と一緒に読む。

    記録の番号は RFC 7111 の行（表頭の記録を含め、空の行も 1 つの記録として先頭から数える。
    表計算のソフトで開いたときの行と同じ）。引用符の中の改行を含む記録は 1 つの記録で、物理行は
    複数になる。
    """
    # newline="" で \r\n / \r / \n を行の区切りにし、引用符の中の改行は値に残す。
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    rows: list[tuple[int, int, int, list[str]]] = []
    previous = 0
    for number, row in enumerate(reader, start=1):
        rows.append((number, previous + 1, reader.line_num, row))
        previous = reader.line_num
    return rows


def _csv_to_records(source_bytes: bytes) -> ConvertOutcome:
    if not source_bytes:
        return ConvertOutcome.passthrough(reason="csv_empty")
    text, warnings = _decode_csv(source_bytes)
    if text is None or not text.strip():
        return ConvertOutcome.passthrough(reason="csv_empty")
    delimiter = _sniff_delimiter(text[:8192])
    try:
        rows = _read_rows(text, delimiter)
    except csv.Error:
        return ConvertOutcome.passthrough(reason="csv_parse_failed")
    rows = [item for item in rows if any(cell.strip() for cell in item[3])]
    if not rows:
        return ConvertOutcome.passthrough(reason="csv_no_rows")

    _, header_start, _, header = rows[0]
    header_cells = [cell.strip() for cell in header]
    # ヘッダが空/重複のときは決定論で列名を補完(col_1, col_2, ...)。
    columns = _resolve_columns(header_cells)
    wide_rows: list[int] = []
    records: list[ContentLine] = []
    for index, (number, row_start, row_end, row) in enumerate(rows[1:], start=1):
        if len(row) > len(columns):
            wide_rows.append(row_start)
            columns = _resolve_columns([*columns, *("" for _ in range(len(row) - len(columns)))])
        records.append(
            RowLine(
                record_id=f"row-{index:06d}",
                source_type="csv",
                location=RecordLocation(row_start=row_start, row_end=row_end, record=number),
                fields=[
                    RecordField(
                        name=column,
                        position=position,
                        value=row[position - 1] if position <= len(row) else "",
                    )
                    for position, column in enumerate(columns, start=1)
                ],
            )
        )
    if not records:
        return ConvertOutcome.passthrough(reason="csv_no_rows")
    diagnostics = [
        RecordDiagnostic(code="csv_row_wider_than_header", detail=f"row={row}")
        for row in wide_rows[:100]
    ]
    if wide_rows:
        warnings.append(f"csv_row_wider_than_header:{len(wide_rows)}")
    document = DocumentLine(
        record_id="document",
        source_type="csv",
        converter=CONVERTER_NAME,
        converter_version=CONVERTER_VERSION,
        header_row=header_start,
        header=header_cells,
        diagnostics=diagnostics,
    )
    return ConvertOutcome(
        converted=True,
        converter_name=CONVERTER_NAME,
        converter_version=CONVERTER_VERSION,
        derived_bytes=dump_structured_records([document, *records]),
        derived_content_type=STRUCTURED_RECORDS_CONTENT_TYPE,
        warnings=tuple(warnings),
    )


def _resolve_columns(header: list[str]) -> list[str]:
    """空・重複のないユニークな列名を決定論で確定する。"""
    columns: list[str] = []
    used: set[str] = set()
    for index, name in enumerate(header):
        base = name or f"col_{index + 1}"
        candidate = base
        suffix = 0
        while candidate in used:
            suffix += 1
            candidate = f"{base}_{suffix}"
        used.add(candidate)
        columns.append(candidate)
    return columns
