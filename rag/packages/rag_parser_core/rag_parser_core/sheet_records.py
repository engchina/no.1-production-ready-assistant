"""表計算ファイルの行の記録（前処理 `excel_to_jsonl` の中間の形）と、その要素化（#1221）。

記録のモデル（`ExcelOptions`・`SheetRecords` など）と Excel の読み方は platform の
`pr_backend_core.spreadsheet` にあり（#2525。チャットの添付など製品の外からも使う）、ここでは
RAG の要素化と、モデルの再公開だけを持つ。

前処理のサービスは Excel（.xlsx / .xls）を、シートごとの「表頭・元の行番号・セル範囲・表示の値」を
持つ行の記録（`SheetRecordsDocument`）にし、構造化レコードの JSONL
（`rag_parser_core.structured_records`。#2457）で parser に渡す。parser のサービス（どの adapter
でも）は、この JSONL を外部の parser に渡さず、`sheet_records_extraction` で記録の単位（block）を
1 つずつ要素にする。

- block は 3 種類（`row` / `procedure_step` は engchina/no.1-rag の前処理に合わせる）。
  - `row`: 表の 1 行（「列名: 値」）。
  - `procedure_step`: 手順書の 1 手順（番号と題名の行から次の手順までの複数行）。
  - `form_field`: 帳票（方眼紙）の 1 項目（「ラベル: 値」と、ラベル・値のセル。#2516）。
- 節はシート名と、表の題名（1 シートの複数の表を分けたとき）、手順なら章・手順の題名、帳票なら
  見出し（`section_path=[シート名, …]`）。表頭より上の行（説明など）と表の下の注記の行（#3566）は
  本文の要素にする。注記の印（例: ※1）をセルに含む表の行は、その注記の文を行の本文の後ろに続ける
  （1 行 = 1 chunk のまま、行の chunk だけで注記が読める）。
  シート名だけの見出しの要素は作らない。
- 1 シートの複数の表（#2516）: 前処理が空の行・列で分けた表は、それぞれ別の `SheetRecords`
  （同じシート名・`table_index`・`region`）になる。親子階層の親は表をまたがない
  （metadata の `sheet_table_index`）。
- 要素の metadata に `sheet_name`・`row_start` / `row_end`・`cell_range`・`cell_column_start` /
  `cell_column_end`・`header_row` を持ち、chunking が chunk の場所にする。表の 1 行の記録は、
  セルの座標（`table_row`・値のあるセルの列と表頭の段の `table_cells_json`。#3262）も持つ。
- 分割（#1349。engchina/no.1-rag の「1 行 = 1 chunk」に合わせる）: 記録（`content_kind=record`）は
  1 つずつ単独の chunk にし、他の記録・前書きと結合せず、途中で切らない
  （`rag_pipeline_core.chunking`）。
  各 chunk の本文は表頭の列名つきの値（「列名: 値 / 列名: 値」。複数行の表頭は前処理が「 / 」で
  つないだ列名）で、シート名は節（`section_path`）として検索の文脈の見出しに入る。親子階層では、
  同じシート（手順書は同じ章）の続く記録をまとめた表の一部を親にする
  （backend の `app.rag.chunking_small_to_big`）。
- 選択肢（`ExcelOptions`）は Document Recipe の `excel_options` から前処理へ渡る。
- 図形の文字・選択の記号（#2526）: テキストボックス・図形の文字は、前処理がアンカーの範囲を 1 つの
  セルの単位にして読み、そのセル範囲を記録の `shape_cells` に残す。帳票の値の選択の記号
  （「■A □B」）は前処理が選ばれた選択肢（「A」）を値にし、原文を `value_original` に残す。
- 列の役割（`SheetColumn.role`。#1281）: 設定値の表の「説明・既定値・例示・現在値・推奨値・
  設定できる範囲」の列を、前処理が表頭の語（または選択肢の `column_roles`）で決める。値の列は
  本文で列名の後に役割の表示（`既定値［資料の既定値］: 30`）を付け、回答の生成が資料の既定値・
  例を利用者の環境の現在の値と取り違えないようにする。決められない列には役割を付けない（推測しない）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Final

from pr_backend_core.spreadsheet.models import (
    COLUMN_ROLE_NONE,
    COLUMN_ROLE_TEXT_LABELS,
    COLUMN_ROLES,
    SHEET_RECORDS_FORMAT,
    SHEET_RECORDS_FORMAT_VERSION,
    ColumnRole,
    ColumnRoleSetting,
    ExcelOptions,
    ExcelRange,
    HeaderDetection,
    LayoutDetection,
    SheetBlock,
    SheetColumn,
    SheetDiagnostic,
    SheetLayout,
    SheetNoteRow,
    SheetPreambleRow,
    SheetRecords,
    SheetRecordsDocument,
    SkippedSheet,
    column_display_name,
    parse_excel_range,
)

from rag_parser_core.extraction import DocumentElement, StructuredExtraction

SHEET_RECORD_CONTENT_KIND = "record"
SHEET_PREAMBLE_CONTENT_KIND = "text"
# 1 つの要素の本文の上限（極端に長い行・手順で chunk を壊さない）。
_BLOCK_TEXT_MAX_CHARS = 8000
# 表頭の推定の信頼度が低いシートの warning（文書レシピを確認（REVIEW）で止める。#1229）。
EXCEL_HEADER_LOW_CONFIDENCE_WARNING = "excel_header_low_confidence"
# 表の 1 行の記録の、セルの座標の metadata（#3262。根拠の locator の table_row / table_cells）。
# ``table_row`` は記録が 1 つのデータの行のときのシートの行番号。``table_cells_json`` は値のある
# セルの列の記号と表頭の段（``[["B", ["上の段", "下の段"]], …]``。本文の順）で、表頭の段を記録して
# いない古い行の記録（``SheetColumn.header_path`` が None）からは作らない（推測しない）。
TABLE_ROW_KEY = "table_row"
TABLE_CELLS_KEY = "table_cells_json"
# 行の番号の基準（根拠の locator の row_basis。#3262）と、その基準の行の範囲
# （``table_row_start`` / ``table_row_end``）。表計算は ``sheet_name`` から決まり、行の範囲は
# ``row_start`` / ``row_end`` なので記録しない。CSV は記録の番号（RFC 7111 の行）で、原本の物理行の
# 範囲を ``source_line_start`` / ``source_line_end`` に持つ。Word は 1 つの表だけを含むまとまりの
# ``table_index`` の表の行。
ROW_BASIS_KEY = "row_basis"
ROW_BASIS_SHEET: Final = "sheet"
ROW_BASIS_CSV: Final = "csv"
ROW_BASIS_WORD_TABLE: Final = "word_table"
TABLE_INDEX_KEY = "table_index"
TABLE_ROW_START_KEY = "table_row_start"
TABLE_ROW_END_KEY = "table_row_end"
SOURCE_LINE_START_KEY = "source_line_start"
SOURCE_LINE_END_KEY = "source_line_end"
TABLE_LOCATOR_METADATA_KEYS: tuple[str, ...] = (
    TABLE_ROW_KEY,
    TABLE_CELLS_KEY,
    ROW_BASIS_KEY,
    TABLE_INDEX_KEY,
    TABLE_ROW_START_KEY,
    TABLE_ROW_END_KEY,
    SOURCE_LINE_START_KEY,
    SOURCE_LINE_END_KEY,
)


def table_cells_metadata(cells: list[tuple[str, list[str]]]) -> str:
    """値のあるセルの列の記号と表頭の段（``TABLE_CELLS_KEY`` の値。本文の順）。"""
    return json.dumps(
        [[column, path] for column, path in cells], ensure_ascii=False, separators=(",", ":")
    )


def column_letters(position: int) -> str:
    """列の位置（1 始まり）を表計算の列の記号にする（1 → A、27 → AA）。"""
    letters = ""
    while position > 0:
        position, remainder = divmod(position - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _block_text(
    block: SheetBlock,
    roles: Mapping[str, str] | None = None,
    *,
    max_chars: int | None = _BLOCK_TEXT_MAX_CHARS,
) -> str:
    """記録の本文（``max_chars`` が None なら切らない。大きさの判定に使う）。"""
    if block.kind == "form_field":
        text = f"{block.label}: {block.value}" if block.label else block.value
    elif block.kind == "procedure_step":
        # 手順の行は前処理が役割の表示を付けた列名で作る。
        text = "\n".join(line for line in block.lines if line.strip())
    else:
        roles = roles or {}
        text = " / ".join(
            f"{column_display_name(name, roles.get(name))}: {value}"
            for name, value in block.values.items()
            if value.strip()
        )
        if text and block.notes:
            # セルの注記の印が指す表の下の注記（#3566）。
            text = "\n".join([text, *block.notes])
    return text if max_chars is None else text[:max_chars]


def _column_roles_metadata(roles: Mapping[str, str]) -> str | None:
    """要素の metadata に残す列の役割（``列名=役割`` を ``; `` でつなぐ）。

    要素の metadata は JSON の scalar だけを持てるので、1 つの文字列にする。
    """
    if not roles:
        return None
    return "; ".join(f"{name}={role}" for name, role in roles.items())


def _column_bounds(cell_range: str) -> tuple[str | None, str | None]:
    start, _, end = cell_range.partition(":")
    letters_start = "".join(char for char in start if char.isalpha()) or None
    letters_end = "".join(char for char in (end or start) if char.isalpha()) or None
    return letters_start, letters_end


def _table_locator_metadata(
    block: SheetBlock, columns: Mapping[str, SheetColumn]
) -> dict[str, str | int]:
    """表の 1 行の記録のセルの座標（``TABLE_LOCATOR_METADATA_KEYS``）。

    手順・帳票・前書き・複数行にまたがる記録は表の 1 行ではないので作らない。
    """
    if block.kind != "row" or block.row_start != block.row_end:
        return {}
    metadata: dict[str, str | int] = {TABLE_ROW_KEY: block.row_start}
    cells: list[tuple[str, list[str]]] = []
    for name, value in block.values.items():
        column = columns.get(name)
        if not value.strip():
            continue
        if column is None or column.header_path is None:
            return metadata
        cells.append((column.column, column.header_path))
    if cells:
        metadata[TABLE_CELLS_KEY] = table_cells_metadata(cells)
    return metadata


def sheet_block_record_id(sheet_id: str, block: SheetBlock, mode: str) -> str:
    """記録の ID（要素の ID と JSONL の record_id）。

    表・手順書は行ごとに 1 つの記録なので ``<シート>-r<行>``。帳票は 1 行に複数の項目があるので
    先頭の列も付ける（``<シート>-r<行>-<列>``）。
    """
    if mode != "form" and block.kind != "form_field":
        return f"{sheet_id}-r{block.row_start}"
    column, _ = _column_bounds(block.cell_range)
    return f"{sheet_id}-r{block.row_start}-{column or 'A'}"


def sheet_section_path(sheet: SheetRecords) -> list[str]:
    """シートの記録の節の先頭（シート名と、分けた表の題名）。"""
    return [sheet.name, *([sheet.title] if sheet.title else [])]


def _sheet_metadata(sheet: SheetRecords) -> dict[str, str | int]:
    metadata: dict[str, str | int] = {"sheet_layout": sheet.mode}
    if sheet.table_index is not None:
        metadata["sheet_table_index"] = sheet.table_index
    if sheet.region:
        metadata["sheet_region"] = sheet.region
    return metadata


def _shape_metadata(shape_cells: list[str]) -> dict[str, str]:
    """図形の文字を置いたセル範囲（要素の metadata は scalar だけなので ``; `` でつなぐ）。"""
    return {"sheet_shape_cells": "; ".join(shape_cells)} if shape_cells else {}


def sheet_records_extraction(
    document: SheetRecordsDocument,
    *,
    source_parser: str,
    parser_backend: str,
    parser_version: str,
) -> StructuredExtraction:
    """シートを節、データの行を 1 行 1 要素にした抽出を作る。"""
    elements: list[DocumentElement] = []
    raw_lines: list[str] = []
    order = 0
    for sheet_index, sheet in enumerate(document.sheets):
        section_path = sheet_section_path(sheet)
        sheet_metadata = _sheet_metadata(sheet)
        roles = {column.name: column.role for column in sheet.columns if column.role}
        roles_metadata = _column_roles_metadata(roles)
        columns_by_name = {column.name: column for column in sheet.columns}
        # シート名は節（section_path）と chunk の文脈の見出しに入るので、見出しだけの要素は作らない
        # （シート名だけの場所の無い小さな chunk を作らない）。
        heading_id = f"sheet-{sheet_index:03d}"
        raw_lines.append(f"# {sheet.name}")
        for preamble in sheet.preamble:
            column_start, column_end = _column_bounds(preamble.cell_range)
            elements.append(
                DocumentElement(
                    kind="text",
                    text=preamble.text,
                    order=order,
                    element_id=f"{heading_id}-r{preamble.row_number}",
                    content_kind=SHEET_PREAMBLE_CONTENT_KIND,
                    source_parser=source_parser,
                    section_path=section_path,
                    metadata={
                        "sheet_name": sheet.name,
                        "row_start": preamble.row_number,
                        "row_end": preamble.row_number,
                        "cell_range": preamble.cell_range,
                        "cell_column_start": column_start,
                        "cell_column_end": column_end,
                        "sheet_block_kind": "preamble",
                        **sheet_metadata,
                        **_shape_metadata(preamble.shape_cells),
                    },
                )
            )
            raw_lines.append(preamble.text)
            order += 1
        for block in sheet.blocks:
            text = _block_text(block, roles)
            if not text:
                continue
            column_start, column_end = _column_bounds(block.cell_range)
            elements.append(
                DocumentElement(
                    kind="text",
                    text=text,
                    order=order,
                    element_id=sheet_block_record_id(heading_id, block, sheet.mode),
                    content_kind=SHEET_RECORD_CONTENT_KIND,
                    source_parser=source_parser,
                    section_path=[*section_path, *block.section_path],
                    confidence=1.0,
                    metadata={
                        "sheet_name": sheet.name,
                        "row_start": block.row_start,
                        "row_end": block.row_end,
                        "cell_range": block.cell_range,
                        "cell_column_start": column_start,
                        "cell_column_end": column_end,
                        "header_row": sheet.header_row,
                        "sheet_block_kind": block.kind,
                        **sheet_metadata,
                        **({"sheet_column_roles": roles_metadata} if roles_metadata else {}),
                        **({"form_label_cell": block.label_cell} if block.label_cell else {}),
                        **({"form_value_cell": block.value_cell} if block.value_cell else {}),
                        **(
                            {"form_value_original": block.value_original}
                            if block.value_original
                            else {}
                        ),
                        **_shape_metadata(block.shape_cells),
                        **_table_locator_metadata(block, columns_by_name),
                    },
                )
            )
            raw_lines.append(text)
            order += 1
        for note in sheet.notes:
            column_start, column_end = _column_bounds(note.cell_range)
            elements.append(
                DocumentElement(
                    kind="text",
                    text=note.text,
                    order=order,
                    element_id=f"{heading_id}-r{note.row_number}",
                    content_kind=SHEET_PREAMBLE_CONTENT_KIND,
                    source_parser=source_parser,
                    section_path=section_path,
                    metadata={
                        "sheet_name": sheet.name,
                        "row_start": note.row_number,
                        "row_end": note.row_number,
                        "cell_range": note.cell_range,
                        "cell_column_start": column_start,
                        "cell_column_end": column_end,
                        "sheet_block_kind": "note",
                        **sheet_metadata,
                        **_shape_metadata(note.shape_cells),
                    },
                )
            )
            raw_lines.append(note.text)
            order += 1
    diagnostics = [
        f"{sheet.name}:{item.code}" + (f"@{item.cell}" if item.cell else "")
        for sheet in document.sheets
        for item in sheet.diagnostics
    ]
    # 表頭の推定の信頼度が低いシートは取込の品質の warning にし、文書レシピを確認で止める（#1229）。
    warnings = [
        f"{EXCEL_HEADER_LOW_CONFIDENCE_WARNING}:{sheet.name}"
        for sheet in document.sheets
        if any(item.code == "header_low_confidence" for item in sheet.diagnostics)
    ]
    return StructuredExtraction(
        raw_text="\n".join(raw_lines),
        document_type="SPREADSHEET",
        confidence=1.0,
        warnings=warnings,
        elements=elements,
        parser_artifacts={
            "source_parser": source_parser,
            "parser_backend": parser_backend,
            "parser_version": parser_version,
            "sheet_records_format_version": document.format_version,
            "source_format": document.source_format,
            "sheet_count": len(document.sheets),
            "block_count": sum(len(sheet.blocks) for sheet in document.sheets),
            "form_field_count": sum(
                1
                for sheet in document.sheets
                for block in sheet.blocks
                if block.kind == "form_field"
            ),
            "skipped_sheet_count": len(document.skipped_sheets),
            "sheet_diagnostics": "\n".join(diagnostics)[:4000],
        },
    )


__all__ = [
    "COLUMN_ROLES",
    "COLUMN_ROLE_NONE",
    "COLUMN_ROLE_TEXT_LABELS",
    "EXCEL_HEADER_LOW_CONFIDENCE_WARNING",
    "SHEET_RECORDS_FORMAT",
    "SHEET_RECORDS_FORMAT_VERSION",
    "SHEET_RECORD_CONTENT_KIND",
    "SOURCE_LINE_END_KEY",
    "SOURCE_LINE_START_KEY",
    "ROW_BASIS_CSV",
    "ROW_BASIS_KEY",
    "ROW_BASIS_SHEET",
    "ROW_BASIS_WORD_TABLE",
    "TABLE_CELLS_KEY",
    "TABLE_INDEX_KEY",
    "TABLE_LOCATOR_METADATA_KEYS",
    "TABLE_ROW_END_KEY",
    "TABLE_ROW_KEY",
    "TABLE_ROW_START_KEY",
    "ColumnRole",
    "ColumnRoleSetting",
    "ExcelOptions",
    "ExcelRange",
    "HeaderDetection",
    "LayoutDetection",
    "SheetColumn",
    "SheetDiagnostic",
    "SheetNoteRow",
    "SheetPreambleRow",
    "SheetRecords",
    "SheetRecordsDocument",
    "SheetBlock",
    "SheetLayout",
    "SkippedSheet",
    "column_display_name",
    "column_letters",
    "parse_excel_range",
    "sheet_block_record_id",
    "sheet_records_extraction",
    "sheet_section_path",
    "table_cells_metadata",
]
