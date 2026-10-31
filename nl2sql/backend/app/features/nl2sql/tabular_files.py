"""CSV と新旧 Excel 取込で共有する workbook 読取契約。"""

from __future__ import annotations

import importlib
import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

OPENXML_WORKBOOK_SUFFIXES = frozenset({".xlsx", ".xlsm"})
LEGACY_WORKBOOK_SUFFIXES = frozenset({".xls"})
WORKBOOK_SUFFIXES = OPENXML_WORKBOOK_SUFFIXES | LEGACY_WORKBOOK_SUFFIXES
CORE_TABULAR_SUFFIXES = frozenset({".csv", ".xlsx", ".xls"})
XLS_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


class TabularFileReadError(ValueError):
    """利用者がファイルを差し替えることで回復できる workbook 読取エラー。"""


class TabularSheetSelectionError(ValueError):
    """利用者が Sheet 名を修正することで回復できる workbook 選択エラー。"""


@dataclass(frozen=True)
class TabularSheet:
    title: str
    rows: list[list[Any]]
    active: bool = False


def _workbook_error(format_name: str) -> str:
    return (
        f"{format_name} の読込に失敗しました。"
        "ファイルが破損、暗号化、または拡張子と内容が不一致でないか確認してください。"
    )


def _read_openxml_workbook(content: bytes) -> list[TabularSheet]:
    openpyxl = importlib.import_module("openpyxl")
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        raise TabularFileReadError(_workbook_error("XLSX")) from exc
    try:
        active_title = str(workbook.active.title)
        return [
            _openxml_sheet_to_tabular(sheet, active=str(sheet.title) == active_title)
            for sheet in workbook.worksheets
        ]
    finally:
        _close_workbook(workbook)


def _read_openxml_workbook_sheet(
    content: bytes,
    requested_name: str,
    *,
    require_requested_name: bool,
) -> tuple[TabularSheet, list[str]]:
    openpyxl = importlib.import_module("openpyxl")
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        raise TabularFileReadError(_workbook_error("XLSX")) from exc
    try:
        sheet_names = [str(name) for name in workbook.sheetnames]
        selected, warnings = _select_workbook_sheet_name(
            sheet_names,
            active_title=str(workbook.active.title),
            requested_name=requested_name,
            require_requested_name=require_requested_name,
        )
        sheet = workbook[selected]
        return (
            _openxml_sheet_to_tabular(sheet, active=str(sheet.title) == str(workbook.active.title)),
            warnings,
        )
    finally:
        _close_workbook(workbook)


def _openxml_sheet_to_tabular(sheet: Any, *, active: bool) -> TabularSheet:
    return TabularSheet(
        title=str(sheet.title),
        rows=[list(row) for row in sheet.iter_rows(values_only=True)],
        active=active,
    )


def _close_workbook(workbook: Any) -> None:
    close = getattr(workbook, "close", None)
    if callable(close):
        close()


def _normalize_xls_cell(xlrd: Any, cell: Any, datemode: int) -> Any:
    if cell.ctype in {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK}:
        return None
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)
    if cell.ctype == xlrd.XL_CELL_DATE:
        return xlrd.xldate_as_datetime(cell.value, datemode)
    if cell.ctype == xlrd.XL_CELL_NUMBER:
        number = float(cell.value)
        return int(number) if number.is_integer() else number
    if cell.ctype == xlrd.XL_CELL_ERROR:
        return str(xlrd.error_text_from_code.get(cell.value, f"Excel error {cell.value}"))
    return cell.value


def _read_legacy_workbook(content: bytes) -> list[TabularSheet]:
    if not content.startswith(XLS_OLE_SIGNATURE):
        raise TabularFileReadError(_workbook_error("XLS"))
    xlrd = importlib.import_module("xlrd")
    try:
        workbook = xlrd.open_workbook(file_contents=content, on_demand=True)
        sheets = [
            TabularSheet(
                title=str(sheet.name),
                rows=[
                    [
                        _normalize_xls_cell(
                            xlrd,
                            sheet.cell(row_index, column_index),
                            workbook.datemode,
                        )
                        for column_index in range(sheet.ncols)
                    ]
                    for row_index in range(sheet.nrows)
                ],
                active=index == 0,
            )
            for index, sheet in enumerate(workbook.sheets())
        ]
        workbook.release_resources()
        return sheets
    except TabularFileReadError:
        raise
    except Exception as exc:
        raise TabularFileReadError(_workbook_error("XLS")) from exc


def _read_legacy_workbook_sheet(
    content: bytes,
    requested_name: str,
    *,
    require_requested_name: bool,
) -> tuple[TabularSheet, list[str]]:
    if not content.startswith(XLS_OLE_SIGNATURE):
        raise TabularFileReadError(_workbook_error("XLS"))
    xlrd = importlib.import_module("xlrd")
    workbook = None
    try:
        workbook = xlrd.open_workbook(file_contents=content, on_demand=True)
        sheet_names = [str(name) for name in workbook.sheet_names()]
        selected, warnings = _select_workbook_sheet_name(
            sheet_names,
            active_title=sheet_names[0] if sheet_names else "",
            requested_name=requested_name,
            require_requested_name=require_requested_name,
        )
        sheet = workbook.sheet_by_name(selected)
        return (
            _legacy_sheet_to_tabular(
                xlrd,
                sheet,
                workbook.datemode,
                active=selected == (sheet_names[0] if sheet_names else ""),
            ),
            warnings,
        )
    except (TabularFileReadError, TabularSheetSelectionError):
        raise
    except Exception as exc:
        raise TabularFileReadError(_workbook_error("XLS")) from exc
    finally:
        if workbook is not None:
            workbook.release_resources()


def _legacy_sheet_to_tabular(xlrd: Any, sheet: Any, datemode: int, *, active: bool) -> TabularSheet:
    return TabularSheet(
        title=str(sheet.name),
        rows=[
            [
                _normalize_xls_cell(
                    xlrd,
                    sheet.cell(row_index, column_index),
                    datemode,
                )
                for column_index in range(sheet.ncols)
            ]
            for row_index in range(sheet.nrows)
        ],
        active=active,
    )


def read_workbook_sheets(filename: str, content: bytes) -> list[TabularSheet]:
    """拡張子に対応する reader で workbook 全 sheet を決定論的に読み取る。"""

    suffix = Path(filename).suffix.lower()
    if suffix in OPENXML_WORKBOOK_SUFFIXES:
        sheets = _read_openxml_workbook(content)
    elif suffix in LEGACY_WORKBOOK_SUFFIXES:
        sheets = _read_legacy_workbook(content)
    else:
        raise TabularFileReadError(
            f"{suffix or '拡張子なし'} は未対応です。CSV、XLSX、XLS のいずれかを指定してください。"
        )
    if not sheets:
        raise TabularFileReadError(
            "Excel workbook に読み取り可能な Sheet がありません。"
            "Sheet を追加して再試行してください。"
        )
    return sheets


def read_workbook_sheet(
    filename: str,
    content: bytes,
    requested_name: str = "",
    *,
    require_requested_name: bool = False,
) -> tuple[TabularSheet, list[str]]:
    """指定 Sheet、または active/先頭 Sheet だけを読み取る。"""

    suffix = Path(filename).suffix.lower()
    if suffix in OPENXML_WORKBOOK_SUFFIXES:
        return _read_openxml_workbook_sheet(
            content,
            requested_name,
            require_requested_name=require_requested_name,
        )
    if suffix in LEGACY_WORKBOOK_SUFFIXES:
        return _read_legacy_workbook_sheet(
            content,
            requested_name,
            require_requested_name=require_requested_name,
        )
    raise TabularFileReadError(
        f"{suffix or '拡張子なし'} は未対応です。CSV、XLSX、XLS のいずれかを指定してください。"
    )


def select_workbook_sheet(
    sheets: list[TabularSheet],
    requested_name: str = "",
    *,
    require_requested_name: bool = False,
) -> tuple[TabularSheet, list[str]]:
    """指定 Sheet、または reader が示す active/先頭 Sheet を返す。"""

    warnings: list[str] = []
    if require_requested_name and not requested_name.strip():
        available = _available_sheet_names_message(sheets)
        raise TabularSheetSelectionError(
            "Excel workbook の Sheet 名は必須です。"
            f"Sheet 名を入力して再試行してください。{available}"
        )
    if requested_name:
        selected = next((sheet for sheet in sheets if sheet.title == requested_name), None)
        if selected is not None:
            return selected, warnings
        if require_requested_name:
            available = _available_sheet_names_message(sheets)
            raise TabularSheetSelectionError(
                f"{requested_name}: Sheet が見つかりません。"
                "Sheet 名を修正するか、ファイル内の Sheet 名を確認して"
                f"再試行してください。{available}"
            )
        warnings.append(
            f"{requested_name}: Sheet が見つからないため active または先頭 Sheet を使用しました。"
        )
    selected = next((sheet for sheet in sheets if sheet.active), sheets[0])
    return selected, warnings


def _select_workbook_sheet_name(
    sheet_names: list[str],
    *,
    active_title: str,
    requested_name: str,
    require_requested_name: bool,
) -> tuple[str, list[str]]:
    warnings: list[str] = []
    if not sheet_names:
        raise TabularFileReadError(
            "Excel workbook に読み取り可能な Sheet がありません。"
            "Sheet を追加して再試行してください。"
        )
    requested = requested_name.strip()
    if require_requested_name and not requested:
        available = _available_sheet_names_text(sheet_names)
        raise TabularSheetSelectionError(
            "Excel workbook の Sheet 名は必須です。"
            f"Sheet 名を入力して再試行してください。{available}"
        )
    if requested:
        if requested in sheet_names:
            return requested, warnings
        if require_requested_name:
            available = _available_sheet_names_text(sheet_names)
            raise TabularSheetSelectionError(
                f"{requested}: Sheet が見つかりません。"
                "Sheet 名を修正するか、ファイル内の Sheet 名を確認して"
                f"再試行してください。{available}"
            )
        warnings.append(
            f"{requested}: Sheet が見つからないため active または先頭 Sheet を使用しました。"
        )
    if active_title in sheet_names:
        return active_title, warnings
    return sheet_names[0], warnings


def _available_sheet_names_message(sheets: list[TabularSheet]) -> str:
    return _available_sheet_names_text([sheet.title for sheet in sheets])


def _available_sheet_names_text(sheet_names: list[str]) -> str:
    joined = "、".join(sheet_names)
    return f"利用可能な Sheet: {joined}。" if joined else ""


def normalize_workbook_scalar(value: Any) -> str:
    """CSV reader へ再マップするときの workbook scalar 表現を統一する。"""

    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    return str(value)


def validate_tabular_text_signature(content: bytes) -> None:
    """Office/PDF を CSV 等の text として誤読しないための軽量 signature 検査。"""

    looks_binary = (
        content.startswith(XLS_OLE_SIGNATURE)
        or content.startswith(ZIP_SIGNATURES)
        or content.startswith(b"%PDF-")
        or b"\x00" in content[:4096]
    )
    if looks_binary:
        raise TabularFileReadError(
            "CSV/TXT の内容をテキストとして確認できません。"
            "拡張子と実際の形式を一致させ、CSV、XLSX、XLS の正しいファイルを再選択してください。"
        )
