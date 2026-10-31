"""Excel の読み方（表・手順書・帳票・1 シートの複数の表）。

製品と共通の仕組み（チャットの添付など）が同じ読み方を使う（#2525）。

`read_excel(bytes, extension=..., options=...)` が `ExcelReadResult`（`SheetRecordsDocument` か
縮退の理由）を返す。openpyxl（.xlsx / .xlsm）と xlrd（.xls）は使う側が依存に入れる
（`openpyxl_available` / `xlrd_available` で確かめられる）。
"""

from pr_backend_core.spreadsheet.models import (
    COLUMN_ROLE_NONE,
    COLUMN_ROLE_TEXT_LABELS,
    COLUMN_ROLES,
    DEFAULT_NOTE_MARKERS,
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
from pr_backend_core.spreadsheet.reader import (
    ExcelReadResult,
    openpyxl_available,
    read_excel,
    xlrd_available,
)

__all__ = [
    "COLUMN_ROLES",
    "COLUMN_ROLE_NONE",
    "COLUMN_ROLE_TEXT_LABELS",
    "DEFAULT_NOTE_MARKERS",
    "SHEET_RECORDS_FORMAT",
    "SHEET_RECORDS_FORMAT_VERSION",
    "ColumnRole",
    "ColumnRoleSetting",
    "ExcelOptions",
    "ExcelRange",
    "ExcelReadResult",
    "HeaderDetection",
    "LayoutDetection",
    "SheetBlock",
    "SheetColumn",
    "SheetDiagnostic",
    "SheetLayout",
    "SheetNoteRow",
    "SheetPreambleRow",
    "SheetRecords",
    "SheetRecordsDocument",
    "SkippedSheet",
    "column_display_name",
    "openpyxl_available",
    "parse_excel_range",
    "read_excel",
    "xlrd_available",
]
