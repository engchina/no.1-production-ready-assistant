"""会話の添付の入力の検査と、文字と節・表への解析（3 製品共通。#2517・#2652）。

- アップロードは `inspect_attachment`（種類・magic・空・Office の zip の構造と展開の上限）だけを
  行い、本文の解析・OCR・Vision は呼ばない（#2652）。解析は送信した後に読むときに
  `parse_document` / 画像の読み取りで行う（`service.ChatAttachmentService.ensure_parsed`）。
- 種類は拡張子と先頭の bytes（magic）の両方で決め、合わなければ 415。旧形式の binary
  （`.doc` / `.xls` / `.ppt`）と、ここに無い形式は受けない。
- PDF（`pypdf`）は 1 頁 = 1 節、Word は見出しの段落で節に分け、PowerPoint は 1 スライド = 1 節。
- CSV は「1 行 = 列名 + 値」（最初の空でない行を列名にする）。
- Excel は platform の共通の読み方（`pr_backend_core.spreadsheet.read_excel`。RAG の取込と同じ。
  #2544）で読む。表は「1 行 = 列名 + 値」、帳票は「項目・値」の表、手順書と表頭より上の行は節にし、
  1 シートの複数の表は表ごとのシートにする。行と節には元の行番号・セル範囲を持たせる。
- Office は zip と XML（`defusedxml`）で読み、マクロ（`vbaProject.bin` など）は読まず実行しない
  （警告 `macros_not_executed`）。展開の合計・部分の大きさ・件数に上限を掛ける（zip bomb）。
- 画像は製品が渡す読み取りの関数（既定の Vision モデル）で文にする（送信した後。結果は汎用の
  文字の抽出として、原本の hash・指示の版・モデルで再利用する）。
- 内容が無い（文字の無い PDF・空の表など）ことは失敗にせず、空の結果（警告 `no_content`）にする。

製品の解析（RAG の `rag_parser_core`・前処理のサービス）は使わない（製品同士は依存しない。
Issue #2517「依存の向き」）。Excel は platform の共通の読み方を使う。
"""

from __future__ import annotations

import csv
import hashlib
import io
import posixpath
import re
import unicodedata
import zipfile
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Final
from xml.etree.ElementTree import Element  # nosec B405 - 型だけ。解析は defusedxml

from charset_normalizer import from_bytes
from defusedxml import ElementTree as SafeElementTree  # type: ignore[import-untyped]

from .errors import ChatAttachmentError
from .limits import (
    MAX_CELL_CHARACTERS,
    MAX_PDF_PAGES,
    MAX_SLIDES,
    MAX_TABLE_COLUMNS,
    MAX_TABLE_ROWS,
    MAX_TEXT_CHARACTERS,
    MAX_ZIP_ENTRIES,
    MAX_ZIP_MEMBER_BYTES,
    MAX_ZIP_UNCOMPRESSED_BYTES,
    TEXT_SECTION_CHARACTERS,
)
from .models import (
    EXCEL_PARSER_VERSION,
    CellValue,
    ChatAttachmentKind,
    ParsedAttachment,
    ParsedSection,
    ParsedSheet,
    SourceLocation,
)

# 画像を文にする関数（画像の bytes, MIME type → 読み取った文）。製品が既定の Vision モデルで
# 実装する。
ImageReader = Callable[[bytes, str], Awaitable[str]]
# 画像の読み取りの指示（3 製品の読み取りの関数が Vision モデルに同じ文を渡す）。
IMAGE_READING_PROMPT: Final = (
    "この画像の内容を日本語の文章で書き出してください。"
    "画像の中の文字は省略せずにそのまま書き写し、表は 1 行ずつ「列名: 値」の形で書き、"
    "図・グラフ・画面は何が示されているかを書いてください。"
    "読み取れない部分は読み取れないと書き、推測で補わないでください。"
)

# 画像の読み取りの指示の版（指示を変えたら読み取り直す。#2652）。
IMAGE_READING_VERSION: Final = hashlib.sha256(IMAGE_READING_PROMPT.encode()).hexdigest()[:12]

# 解析の版（出力が変わる変更をしたら上げる。上げると送信済みの添付も読むときに解析し直す。#2652）。
DOCUMENT_PARSER_VERSIONS: Final[dict[str, int]] = {
    "pdf": 2,
    "word": 2,
    "powerpoint": 2,
    "excel": EXCEL_PARSER_VERSION,
    "csv": 2,
    "text": 2,
}

# 警告（`ParsedAttachment.warnings`。画面とツールの結果に出す）。
WARNING_MACROS_NOT_EXECUTED: Final = "macros_not_executed"
# 解析できたが、読める内容（文字・表の行）が無い（失敗とは区別する。#2652）。
WARNING_NO_CONTENT: Final = "no_content"
WARNING_PDF_PAGE_WITHOUT_TEXT: Final = "pdf_page_without_text"
WARNING_TEXT_TRUNCATED: Final = "text_truncated"
WARNING_ROWS_TRUNCATED: Final = "rows_truncated"
WARNING_COLUMNS_TRUNCATED: Final = "columns_truncated"
WARNING_PAGES_TRUNCATED: Final = "pages_truncated"
# Excel の読み方の警告・診断（`pr_backend_core.spreadsheet` の code に付ける接頭辞。#2544）。
WARNING_EXCEL_PREFIX: Final = "excel_"
# 帳票の「項目・値」の表の列名。
FORM_COLUMNS: Final = ("項目", "値")


@dataclass(frozen=True)
class _FormatSpec:
    kind: ChatAttachmentKind
    media_type: str


_ZIP_MAGIC = b"PK\x03\x04"
_FORMATS: Final[dict[str, _FormatSpec]] = {
    ".pdf": _FormatSpec("pdf", "application/pdf"),
    ".docx": _FormatSpec(
        "word", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    ".docm": _FormatSpec("word", "application/vnd.ms-word.document.macroEnabled.12"),
    ".pptx": _FormatSpec(
        "powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
    ".pptm": _FormatSpec(
        "powerpoint", "application/vnd.ms-powerpoint.presentation.macroEnabled.12"
    ),
    ".xlsx": _FormatSpec(
        "excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
    ".xlsm": _FormatSpec("excel", "application/vnd.ms-excel.sheet.macroEnabled.12"),
    ".csv": _FormatSpec("csv", "text/csv"),
    ".tsv": _FormatSpec("csv", "text/tab-separated-values"),
    ".txt": _FormatSpec("text", "text/plain"),
    ".md": _FormatSpec("text", "text/markdown"),
    ".markdown": _FormatSpec("text", "text/markdown"),
    ".json": _FormatSpec("text", "application/json"),
    ".log": _FormatSpec("text", "text/plain"),
    ".png": _FormatSpec("image", "image/png"),
    ".jpg": _FormatSpec("image", "image/jpeg"),
    ".jpeg": _FormatSpec("image", "image/jpeg"),
    ".gif": _FormatSpec("image", "image/gif"),
    ".webp": _FormatSpec("image", "image/webp"),
}
IMAGE_EXTENSIONS: Final = tuple(ext for ext, spec in _FORMATS.items() if spec.kind == "image")
DOCUMENT_EXTENSIONS: Final = tuple(ext for ext, spec in _FORMATS.items() if spec.kind != "image")
# zip の中で、その形式の本体があることを確かめる部分。
_OFFICE_MAIN_PART: Final = {
    "word": "word/document.xml",
    "powerpoint": "ppt/presentation.xml",
    "excel": "xl/workbook.xml",
}
_MACRO_PART_RE = re.compile(r"(^|/)(vbaProject\.bin|vbaData\.xml)$", re.IGNORECASE)


def accepted_extensions() -> list[str]:
    """受け付ける拡張子（画像は Vision モデルが無くても保存する。読み取りは送信の後。#2652）。"""
    return list(DOCUMENT_EXTENSIONS) + list(IMAGE_EXTENSIONS)


def _options_fingerprint() -> str:
    """出力に影響する解析の設定（上限の定数）の fingerprint。"""
    values = (
        MAX_TEXT_CHARACTERS,
        MAX_TABLE_ROWS,
        MAX_TABLE_COLUMNS,
        MAX_CELL_CHARACTERS,
        MAX_PDF_PAGES,
        MAX_SLIDES,
        TEXT_SECTION_CHARACTERS,
    )
    return hashlib.sha256(repr(values).encode()).hexdigest()[:12]


def document_parse_key(content_sha256: str, kind: ChatAttachmentKind) -> str:
    """文書の解析の結果を再利用するキー（原本の hash・parser の版・解析の設定。#2652）。"""
    return f"{content_sha256}:{kind}@{DOCUMENT_PARSER_VERSIONS[kind]}:{_options_fingerprint()}"


def image_parse_key(content_sha256: str, model_id: str) -> str:
    """画像の汎用の読み取り（文字の抽出）を再利用するキー（原本の hash・指示の版・モデル）。

    質問に応じた画像の解釈はこのキーで再利用しない（原画像をモデルへ渡す。#2650・#2652）。
    """
    return f"{content_sha256}:image@{IMAGE_READING_VERSION}:{model_id}"


def document_extractor(kind: ChatAttachmentKind) -> str:
    return f"{kind}@{DOCUMENT_PARSER_VERSIONS[kind]}"


def image_extractor(model_id: str) -> str:
    return f"vision:{model_id}@{IMAGE_READING_VERSION}"


def _unsupported(file_name: str) -> ChatAttachmentError:
    return ChatAttachmentError(
        415,
        f"「{file_name}」は添付できない種類です。PDF・Word・PowerPoint・Excel・CSV・テキスト・"
        "画像（PNG・JPEG・GIF・WebP）を添付してください。",
        code="CHAT_ATTACHMENT_UNSUPPORTED_TYPE",
    )


def _mismatch(file_name: str) -> ChatAttachmentError:
    return ChatAttachmentError(
        415,
        f"「{file_name}」の中身が拡張子の形式と一致しません。元の形式で保存し直してから添付してください。",
        code="CHAT_ATTACHMENT_TYPE_MISMATCH",
    )


def _unreadable(file_name: str, reason: str) -> ChatAttachmentError:
    return ChatAttachmentError(
        422, f"「{file_name}」を読み取れません。{reason}", code="CHAT_ATTACHMENT_UNREADABLE"
    )


def detect_format(file_name: str, data: bytes) -> _FormatSpec:
    """拡張子と先頭の bytes で種類を決める（合わなければ 415）。"""
    extension = posixpath.splitext(file_name.lower())[1]
    spec = _FORMATS.get(extension)
    if spec is None:
        raise _unsupported(file_name)
    if not data:
        raise _unreadable(file_name, "ファイルが空です。")
    if spec.kind == "pdf" and not data.startswith(b"%PDF-"):
        raise _mismatch(file_name)
    if spec.kind in _OFFICE_MAIN_PART and not data.startswith(_ZIP_MAGIC):
        raise _mismatch(file_name)
    if spec.kind == "image" and not _image_magic_matches(spec.media_type, data):
        raise _mismatch(file_name)
    if spec.kind in {"csv", "text"} and (b"\x00" in data[:8192] or data.startswith(_ZIP_MAGIC)):
        raise _mismatch(file_name)
    return spec


def _image_magic_matches(media_type: str, data: bytes) -> bool:
    if media_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if media_type == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if media_type == "image/webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


def inspect_attachment(file_name: str, data: bytes) -> tuple[_FormatSpec, list[str]]:
    """アップロードの入力の検査（#2652）。種類・magic・空と、Office の zip の構造・展開の上限。

    本文・表・OCR の抽出はしない（parser・Vision を呼ばない）。返す警告はマクロの有無だけ。
    """
    spec = detect_format(file_name, data)
    warnings: list[str] = []
    if spec.kind in _OFFICE_MAIN_PART:
        with _open_office_zip(file_name, data, spec.kind) as (_office, found):
            warnings.extend(found)
    return spec, warnings


async def read_image(
    file_name: str, data: bytes, media_type: str, *, image_reader: ImageReader, model_id: str
) -> ParsedAttachment:
    """画像の汎用の読み取り（文字の抽出。送信した後に、読むときだけ呼ぶ。#2652）。"""
    text = _clean((await image_reader(data, media_type)) or "")
    extractor = image_extractor(model_id)
    if not text:
        return ParsedAttachment(warnings=[WARNING_NO_CONTENT], extractor=extractor)
    return ParsedAttachment(
        sections=[ParsedSection(section_id="s1", title=file_name, text=text)], extractor=extractor
    )


def image_model_missing() -> ChatAttachmentError:
    return ChatAttachmentError(
        422,
        "画像を読み取れるモデルが設定されていません。システム設定 › モデルで、"
        "画像を読み取れるモデルを既定の Vision モデルにしてください。",
        code="CHAT_ATTACHMENT_IMAGE_MODEL_MISSING",
    )


def parse_document(file_name: str, data: bytes, kind: ChatAttachmentKind) -> ParsedAttachment:
    """画像以外の解析（同期。製品は threadpool で呼ぶ）。"""
    try:
        if kind == "pdf":
            parsed = _parse_pdf(file_name, data)
        elif kind == "word":
            parsed = _parse_docx(file_name, data)
        elif kind == "powerpoint":
            parsed = _parse_pptx(file_name, data)
        elif kind == "excel":
            parsed = _parse_xlsx(file_name, data)
        elif kind == "csv":
            parsed = _parse_csv(file_name, data)
        else:
            parsed = _parse_text(file_name, data)
    except ChatAttachmentError:
        raise
    except Exception as exc:  # 壊れたファイル（zip・XML・PDF の構造）は読み取れないとして返す
        raise _unreadable(
            file_name, "ファイルが壊れているか、対応していない形式の可能性があります。"
        ) from exc
    limited = _limit_text(parsed)
    warnings = list(limited.warnings)
    if limited.is_empty() and WARNING_NO_CONTENT not in warnings:
        warnings.append(WARNING_NO_CONTENT)
    return limited.model_copy(update={"warnings": warnings, "extractor": document_extractor(kind)})


# ---- 共通 ----


def _clean(text: str) -> str:
    """制御文字（改行・タブ以外）を除き、行末の空白と空行の連続を詰める。"""
    text = unicodedata.normalize("NFC", text)
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    cleaned: list[str] = []
    for line in lines:
        if not line and cleaned and not cleaned[-1]:
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def _limit_text(parsed: ParsedAttachment) -> ParsedAttachment:
    total = 0
    sections: list[ParsedSection] = []
    truncated = False
    for section in parsed.sections:
        remaining = MAX_TEXT_CHARACTERS - total
        if remaining <= 0:
            truncated = True
            break
        if len(section.text) > remaining:
            section = section.model_copy(update={"text": section.text[:remaining]})
            truncated = True
        total += len(section.text)
        sections.append(section)
    warnings = list(parsed.warnings)
    if truncated and WARNING_TEXT_TRUNCATED not in warnings:
        warnings.append(WARNING_TEXT_TRUNCATED)
    return parsed.model_copy(update={"sections": sections, "warnings": warnings})


def _numbered_sections(items: Iterable[tuple[str, int, int | None, str]]) -> list[ParsedSection]:
    sections: list[ParsedSection] = []
    for title, level, page, text in items:
        sections.append(
            ParsedSection(
                section_id=f"s{len(sections) + 1}", title=title, level=level, page=page, text=text
            )
        )
    return sections


def _decode_text(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        best = from_bytes(data).best()
        if best is None:
            return data.decode("utf-8", errors="replace")
        return str(best)


# ---- テキスト ----

_MARKDOWN_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _parse_text(file_name: str, data: bytes) -> ParsedAttachment:
    text = _clean(_decode_text(data))
    if not text:
        return ParsedAttachment()
    headings: list[tuple[str, int, list[str]]] = []
    preface: list[str] = []
    for line in text.split("\n"):
        match = _MARKDOWN_HEADING_RE.match(line)
        if match:
            headings.append((match.group(2), len(match.group(1)), []))
        elif headings:
            headings[-1][2].append(line)
        else:
            preface.append(line)
    if headings:
        items: list[tuple[str, int, int | None, str]] = []
        if "\n".join(preface).strip():
            items.append((file_name, 1, None, "\n".join(preface).strip()))
        for title, level, body in headings:
            items.append((title, level, None, (title + "\n" + "\n".join(body)).strip()))
        return ParsedAttachment(sections=_numbered_sections(items))
    chunks = list(_split_by_size(text, TEXT_SECTION_CHARACTERS))
    if len(chunks) == 1:
        return ParsedAttachment(sections=_numbered_sections([(file_name, 1, None, chunks[0])]))
    return ParsedAttachment(
        sections=_numbered_sections(
            (f"{file_name}（{index}/{len(chunks)}）", 1, None, chunk)
            for index, chunk in enumerate(chunks, start=1)
        )
    )


def _split_by_size(text: str, size: int) -> Iterator[str]:
    """行の区切りで `size` 文字ほどに区切る（1 行が長ければ途中で切る）。"""
    current: list[str] = []
    length = 0
    for line in text.split("\n"):
        while len(line) > size:
            if current:
                yield "\n".join(current)
                current, length = [], 0
            yield line[:size]
            line = line[size:]
        if length + len(line) + 1 > size and current:
            yield "\n".join(current)
            current, length = [], 0
        current.append(line)
        length += len(line) + 1
    if current and "\n".join(current).strip():
        yield "\n".join(current)


# ---- 表（CSV・Excel） ----


def _cell(value: Any) -> CellValue:
    if value is None:
        return None
    if isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() and abs(value) < 2**53 else value
    if isinstance(value, datetime):
        return value.isoformat(sep=" ") if value.time() != time(0) else value.date().isoformat()
    if isinstance(value, date | time):
        return value.isoformat()
    text = _clean(str(value))
    if not text:
        return None
    return text[:MAX_CELL_CHARACTERS]


def _column_names(header: list[CellValue]) -> list[str]:
    names: list[str] = []
    seen: dict[str, int] = {}
    for index, value in enumerate(header, start=1):
        base = str(value).replace("\n", " ").strip() if value is not None else ""
        base = base or f"列{index}"
        count = seen.get(base, 0) + 1
        seen[base] = count
        names.append(base if count == 1 else f"{base}_{count}")
    return names


@dataclass
class _TableBudget:
    rows_left: int = MAX_TABLE_ROWS
    rows_truncated: bool = False
    columns_truncated: bool = False


def _sheet_from_rows(
    name: str, rows: Iterable[Iterable[Any]], budget: _TableBudget
) -> ParsedSheet | None:
    """最初の空でない行を列名、以降を行にする（空の行は飛ばし、後ろの空の列を落とす）。"""
    header: list[CellValue] | None = None
    body: list[list[CellValue]] = []
    for raw in rows:
        values = [_cell(value) for value in raw]
        if len(values) > MAX_TABLE_COLUMNS:
            budget.columns_truncated = True
            values = values[:MAX_TABLE_COLUMNS]
        if all(value is None for value in values):
            continue
        if header is None:
            header = values
            continue
        if budget.rows_left <= 0:
            budget.rows_truncated = True
            break
        body.append(values)
        budget.rows_left -= 1
    if header is None:
        return None
    width = max([len(header), *(len(row) for row in body)])
    while (
        width > 0
        and header[width - 1] is None
        and all(len(row) < width or row[width - 1] is None for row in body)
    ):
        width -= 1
    columns = _column_names((header + [None] * width)[:width])
    padded = [(row + [None] * width)[:width] for row in body]
    return ParsedSheet(sheet=name, columns=columns, rows=padded)


def _table_warnings(budget: _TableBudget) -> list[str]:
    warnings: list[str] = []
    if budget.rows_truncated:
        warnings.append(WARNING_ROWS_TRUNCATED)
    if budget.columns_truncated:
        warnings.append(WARNING_COLUMNS_TRUNCATED)
    return warnings


def _parse_csv(file_name: str, data: bytes) -> ParsedAttachment:
    text = _decode_text(data)
    delimiter = "\t" if file_name.lower().endswith(".tsv") else ","
    if delimiter == ",":
        try:
            delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",\t;").delimiter
        except csv.Error:
            delimiter = ","
    budget = _TableBudget()
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    sheet = _sheet_from_rows(posixpath.splitext(file_name)[0] or file_name, reader, budget)
    if sheet is None:
        return ParsedAttachment(warnings=_table_warnings(budget))
    return ParsedAttachment(sheets=[sheet], warnings=_table_warnings(budget))


def _excel_value(value: str | None) -> CellValue:
    """Excel の表示の文字列を添付のセルの値にする（空は None、長すぎる値は切る）。"""
    if value is None:
        return None
    text = _clean(value)
    return text[:MAX_CELL_CHARACTERS] if text else None


def _unique_sheet_name(name: str, used: set[str]) -> str:
    candidate = name
    number = 2
    while candidate in used:
        candidate = f"{name}_{number}"
        number += 1
    used.add(candidate)
    return candidate


def _excel_sheet_name(records: Any, counts: dict[str, int]) -> str:
    """添付のシート名（1 シートの複数の表は「シート名（表 N）」）。"""
    if counts.get(records.name, 0) > 1 and records.table_index is not None:
        return f"{records.name}（表 {records.table_index}）"
    return str(records.name)


def _excel_warnings(result: Any) -> list[str]:
    """`read_excel` の警告・診断を、添付の警告（技術の code）にする。重ねない。"""
    codes: list[str] = []
    for warning in result.warnings:
        codes.append(f"{WARNING_EXCEL_PREFIX}{warning}")
    document = result.document
    for records in document.sheets if document is not None else []:
        for diagnostic in records.diagnostics:
            codes.append(f"{WARNING_EXCEL_PREFIX}{diagnostic.code}")
    for _skipped in document.skipped_sheets if document is not None else []:
        codes.append(f"{WARNING_EXCEL_PREFIX}sheet_skipped")
    return list(dict.fromkeys(codes))


def _parse_xlsx(file_name: str, data: bytes) -> ParsedAttachment:
    """Excel を platform の共通の読み方（RAG の取込と同じ）で読む（#2544）。"""
    from pr_backend_core.spreadsheet import ExcelOptions, read_excel

    with _open_office_zip(file_name, data, "excel") as (_archive, warnings):
        pass
    extension = posixpath.splitext(file_name)[1].lstrip(".").lower() or "xlsx"
    result = read_excel(data, extension=extension, options=ExcelOptions())
    document = result.document
    if document is None:
        raise _unreadable(file_name, "Excel の表を読み取れませんでした。")
    budget = _TableBudget()
    counts: dict[str, int] = {}
    for records in document.sheets:
        counts[records.name] = counts.get(records.name, 0) + 1
    used: set[str] = set()
    sheets: list[ParsedSheet] = []
    # 節の（見出し, 本文, 元の場所, シート）。
    section_items: list[tuple[str, str, SourceLocation, str]] = []
    for records in document.sheets:
        name = _unique_sheet_name(_excel_sheet_name(records, counts), used)
        if records.preamble:
            first, last = records.preamble[0], records.preamble[-1]
            start_cell = first.cell_range.split(":")[0]
            end_cell = last.cell_range.split(":")[-1]
            section_items.append(
                (
                    f"{name} の説明",
                    "\n".join(row.text for row in records.preamble),
                    SourceLocation(
                        row_start=first.row_number,
                        row_end=last.row_number,
                        cell_range=f"{start_cell}:{end_cell}",
                    ),
                    name,
                )
            )
        if records.notes:
            # 表の下の注記の行（#3566）。表の行にしない。
            first_note, last_note = records.notes[0], records.notes[-1]
            section_items.append(
                (
                    f"{name} の注記",
                    "\n".join(row.text for row in records.notes),
                    SourceLocation(
                        row_start=first_note.row_number,
                        row_end=last_note.row_number,
                        cell_range=(
                            f"{first_note.cell_range.split(':')[0]}:"
                            f"{last_note.cell_range.split(':')[-1]}"
                        ),
                    ),
                    name,
                )
            )
        if records.mode == "procedure":
            for index, block in enumerate(records.blocks, start=1):
                title = " / ".join(block.section_path) or f"{name} 手順 {index}"
                section_items.append(
                    (
                        title,
                        "\n".join(block.lines),
                        SourceLocation(
                            row_start=block.row_start,
                            row_end=block.row_end,
                            cell_range=block.cell_range,
                        ),
                        name,
                    )
                )
            continue
        if records.mode == "form":
            columns = list(FORM_COLUMNS)
            raw_rows = [[block.label, block.value] for block in records.blocks]
        else:
            columns = [column.name for column in records.columns][:MAX_TABLE_COLUMNS]
            if len(records.columns) > MAX_TABLE_COLUMNS:
                budget.columns_truncated = True
            raw_rows = [
                [block.values.get(column) for column in columns] for block in records.blocks
            ]
        rows: list[list[CellValue]] = []
        locations: list[SourceLocation] = []
        for block, raw in zip(records.blocks, raw_rows, strict=True):
            if budget.rows_left <= 0:
                budget.rows_truncated = True
                break
            rows.append([_excel_value(value) for value in raw])
            locations.append(
                SourceLocation(
                    row_start=block.row_start,
                    row_end=block.row_end,
                    cell_range=block.cell_range,
                    label_cell=block.label_cell,
                    value_cell=block.value_cell,
                )
            )
            budget.rows_left -= 1
        if not rows and not columns:
            continue
        sheets.append(
            ParsedSheet(
                sheet=name,
                columns=_column_names([*columns]) if columns else [],
                rows=rows,
                locations=locations,
                layout=records.mode,
                source_sheet=records.name,
                region=records.region,
            )
        )
    sections = [
        ParsedSection(
            section_id=f"s{index}", title=title, text=text, sheet=sheet_name, location=location
        )
        for index, (title, text, location, sheet_name) in enumerate(section_items, start=1)
    ]
    return ParsedAttachment(
        sections=sections,
        sheets=sheets,
        warnings=warnings + _table_warnings(budget) + _excel_warnings(result),
        parser_version=EXCEL_PARSER_VERSION,
    )


# ---- Office（zip） ----


class _OfficeZip:
    def __init__(self, archive: zipfile.ZipFile) -> None:
        self.archive = archive
        self.names = set(archive.namelist())

    def xml(self, name: str) -> Element | None:
        if name not in self.names:
            return None
        info = self.archive.getinfo(name)
        if info.file_size > MAX_ZIP_MEMBER_BYTES:
            raise ChatAttachmentError(
                422, "ファイルの中の部分が大きすぎます。", code="CHAT_ATTACHMENT_UNREADABLE"
            )
        with self.archive.open(info) as handle:
            root: Element = SafeElementTree.fromstring(handle.read(MAX_ZIP_MEMBER_BYTES + 1))
        return root

    def relationships(self, part: str) -> dict[str, str]:
        """`part` の rels（rId → 解決した部分の名前）。"""
        directory, base = posixpath.split(part)
        root = self.xml(posixpath.join(directory, "_rels", base + ".rels"))
        if root is None:
            return {}
        result: dict[str, str] = {}
        for rel in root:
            target = rel.get("Target") or ""
            rel_id = rel.get("Id") or ""
            if not rel_id or rel.get("TargetMode") == "External":
                continue
            resolved = (
                target.lstrip("/")
                if target.startswith("/")
                else posixpath.normpath(posixpath.join(directory, target))
            )
            result[rel_id] = resolved
        return result


class _office_zip_context:
    def __init__(self, file_name: str, data: bytes, kind: str) -> None:
        self.file_name = file_name
        self.data = data
        self.kind = kind
        self.archive: zipfile.ZipFile | None = None

    def __enter__(self) -> tuple[_OfficeZip, list[str]]:
        try:
            self.archive = zipfile.ZipFile(io.BytesIO(self.data))
        except zipfile.BadZipFile as exc:
            raise _mismatch(self.file_name) from exc
        infos = self.archive.infolist()
        if len(infos) > MAX_ZIP_ENTRIES or sum(info.file_size for info in infos) > (
            MAX_ZIP_UNCOMPRESSED_BYTES
        ):
            raise _unreadable(self.file_name, "展開した大きさが上限を超えています。")
        office = _OfficeZip(self.archive)
        if _OFFICE_MAIN_PART[self.kind] not in office.names:
            raise _mismatch(self.file_name)
        warnings: list[str] = []
        if any(_MACRO_PART_RE.search(name) for name in office.names):
            # マクロは読まず実行しない（受け付けて本文だけを読む）。
            warnings.append(WARNING_MACROS_NOT_EXECUTED)
        return office, warnings

    def __exit__(self, *_: object) -> None:
        if self.archive is not None:
            self.archive.close()


def _open_office_zip(file_name: str, data: bytes, kind: str) -> _office_zip_context:
    return _office_zip_context(file_name, data, kind)


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_HEADING_STYLE_RE = re.compile(r"^(?:heading|見出し)\s*(\d)$", re.IGNORECASE)


def _word_heading_levels(office: _OfficeZip) -> dict[str, int]:
    """style ID → 見出しの段（style の名前 `heading N` / `見出し N` か outline level）。"""
    root = office.xml("word/styles.xml")
    levels: dict[str, int] = {}
    if root is None:
        return levels
    for style in root.iter(f"{_W}style"):
        style_id = style.get(f"{_W}styleId") or ""
        name_node = style.find(f"{_W}name")
        name = name_node.get(f"{_W}val", "") if name_node is not None else ""
        outline = style.find(f"{_W}pPr/{_W}outlineLvl")
        match = _HEADING_STYLE_RE.match(name.strip()) or _HEADING_STYLE_RE.match(style_id)
        if match:
            levels[style_id] = int(match.group(1))
        elif outline is not None and (outline.get(f"{_W}val") or "").isdigit():
            level = int(outline.get(f"{_W}val") or "9") + 1
            if level <= 6:
                levels[style_id] = level
    return levels


def _word_paragraph_text(paragraph: Element) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        if node.tag == f"{_W}t" and node.text:
            parts.append(node.text)
        elif node.tag == f"{_W}tab":
            parts.append("\t")
        elif node.tag in {f"{_W}br", f"{_W}cr"}:
            parts.append("\n")
    return "".join(parts)


def _word_table_text(table: Element) -> str:
    """表は「列名: 値」の行の文にする（1 行目を列名とみなす）。"""
    rows: list[list[str]] = []
    for row in table.iter(f"{_W}tr"):
        cells = [
            " ".join(_word_paragraph_text(p).strip() for p in cell.iter(f"{_W}p")).strip()
            for cell in row.findall(f"{_W}tc")
        ]
        if any(cells):
            rows.append(cells)
    if not rows:
        return ""
    if len(rows) == 1:
        return " | ".join(rows[0])
    header = _column_names([cell or None for cell in rows[0]])
    lines = []
    for values in rows[1:]:
        pairs = [f"{name}: {value}" for name, value in zip(header, values, strict=False) if value]
        if pairs:
            lines.append(" / ".join(pairs))
    return "\n".join(lines)


def _parse_docx(file_name: str, data: bytes) -> ParsedAttachment:
    with _open_office_zip(file_name, data, "word") as (office, warnings):
        levels = _word_heading_levels(office)
        root = office.xml("word/document.xml")
        body = root.find(f"{_W}body") if root is not None else None
        items: list[tuple[str, int, int | None, list[str]]] = [(file_name, 1, None, [])]
        for block in list(body) if body is not None else []:
            if block.tag == f"{_W}p":
                text = _word_paragraph_text(block).strip()
                if not text:
                    continue
                style = block.find(f"{_W}pPr/{_W}pStyle")
                outline = block.find(f"{_W}pPr/{_W}outlineLvl")
                level = levels.get(style.get(f"{_W}val", "") if style is not None else "")
                if level is None and outline is not None:
                    raw = outline.get(f"{_W}val") or ""
                    level = int(raw) + 1 if raw.isdigit() and int(raw) < 6 else None
                if level is not None:
                    items.append((text.replace("\n", " "), level, None, [text]))
                else:
                    items[-1][3].append(text)
            elif block.tag == f"{_W}tbl":
                text = _word_table_text(block)
                if text:
                    items[-1][3].append(text)
    sections = _numbered_sections(
        (title, level, page, _clean("\n".join(lines)))
        for title, level, page, lines in items
        if _clean("\n".join(lines))
    )
    return ParsedAttachment(sections=sections, warnings=warnings)


def _slide_text(root: Element | None) -> tuple[str, str]:
    """スライドの（タイトル, 本文）。タイトルはタイトルのプレースホルダーの文。"""
    if root is None:
        return "", ""
    title_parts: list[str] = []
    body_parts: list[str] = []
    for shape in root.iter(f"{_P}sp"):
        placeholder = shape.find(f"{_P}nvSpPr/{_P}nvPr/{_P}ph")
        kind = placeholder.get("type", "") if placeholder is not None else ""
        if kind in {"sldNum", "dt", "ftr"}:
            continue
        paragraphs = [
            "".join(node.text or "" for node in paragraph.iter(f"{_A}t"))
            for paragraph in shape.iter(f"{_A}p")
        ]
        text = "\n".join(p for p in paragraphs if p.strip())
        if not text:
            continue
        (title_parts if kind in {"title", "ctrTitle"} else body_parts).append(text)
    for frame in root.iter(f"{_P}graphicFrame"):
        for row in frame.iter(f"{_A}tr"):
            cells = [
                "".join(node.text or "" for node in cell.iter(f"{_A}t")).strip()
                for cell in row.iter(f"{_A}tc")
            ]
            if any(cells):
                body_parts.append(" | ".join(cells))
    return " ".join(title_parts).strip(), "\n".join(body_parts)


def _parse_pptx(file_name: str, data: bytes) -> ParsedAttachment:
    with _open_office_zip(file_name, data, "powerpoint") as (office, warnings):
        presentation = office.xml("ppt/presentation.xml")
        rels = office.relationships("ppt/presentation.xml")
        slide_parts: list[str] = []
        id_list = presentation.find(f"{_P}sldIdLst") if presentation is not None else None
        for slide_id in list(id_list) if id_list is not None else []:
            part = rels.get(slide_id.get(f"{_R}id") or "")
            if part:
                slide_parts.append(part)
        items: list[tuple[str, int, int | None, str]] = []
        for number, part in enumerate(slide_parts[:MAX_SLIDES], start=1):
            title, body = _slide_text(office.xml(part))
            notes = ""
            for target in office.relationships(part).values():
                if "notesSlide" in target:
                    _, notes = _slide_text(office.xml(target))
            text_parts = [title, body] + ([f"ノート:\n{notes}"] if notes.strip() else [])
            text = _clean("\n".join(piece for piece in text_parts if piece))
            if text:
                label = f"スライド {number}" + (f": {title}" if title else "")
                items.append((label, 1, number, text))
    if len(slide_parts) > MAX_SLIDES:
        warnings.append(WARNING_PAGES_TRUNCATED)
    return ParsedAttachment(sections=_numbered_sections(items), warnings=warnings)


# ---- PDF ----


def _parse_pdf(file_name: str, data: bytes) -> ParsedAttachment:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(""):
        raise _unreadable(file_name, "パスワードを外してから添付してください。")
    warnings: list[str] = []
    items: list[tuple[str, int, int | None, str]] = []
    # 文字を取り出せなかった頁（画像だけの頁など）。黙って捨てず、頁の番号を結果に残す（#2652）。
    empty_pages: list[int] = []
    for number, page in enumerate(reader.pages, start=1):
        if number > MAX_PDF_PAGES:
            warnings.append(WARNING_PAGES_TRUNCATED)
            break
        text = _clean(page.extract_text() or "")
        if text:
            items.append((f"p.{number}", 1, number, text))
        else:
            empty_pages.append(number)
    if empty_pages:
        warnings.append(WARNING_PDF_PAGE_WITHOUT_TEXT)
    return ParsedAttachment(
        sections=_numbered_sections(items), warnings=warnings, pages_without_text=empty_pages
    )
