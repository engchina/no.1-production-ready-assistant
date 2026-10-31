"""会話の添付を読むツールの中核（3 製品共通。#2517）。

名前と結果の形は Issue #2517 で固定した契約（後の「出力 schema・抽出→照合のテンプレート」が使う）。
ツールは渡された会話の添付（送信した添付）だけを読む（会話の権限の判定は製品が先に行う）。
添付は送信した後に初めて読むときに解析する（#2652。`service.ChatAttachmentService.ensure_parsed`）。
一覧は各添付の解析の状態を返し、読めない添付（解析中・失敗・期限切れ）は理由を返す。

- `chat_attachment_list`: 添付の索引（節・シート）と解析の状態。
- `chat_attachment_read_section`: 節の本文を `READ_SECTION_CHARACTERS` 文字ずつ。
- `chat_attachment_read_rows`: シートの行を「列名 → 値」で `READ_ROWS_MAX` 行まで。
- `chat_attachment_find`: 文字の一致で節と行を探す。
- `chat_attachment_view_image`: 画像の原本を、質問に必要な観点（`focus`）とともに読む（#2652）。
  この中核は画像の参照と原本のバイト列を返し、モデルの入力に画像を渡すのは製品（Agent の
  ツールの結果の画像。#2650）。画像の解釈は質問ごとにモデルが行い、原本だけをキーに再利用しない。
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from .models import (
    TABULAR_KINDS,
    AttachmentReadResult,
    ChatAttachmentRecord,
    ParsedAttachment,
    ParsedSheet,
    SourceLocation,
)

LIST_TOOL_NAME: Final = "chat_attachment_list"
READ_SECTION_TOOL_NAME: Final = "chat_attachment_read_section"
READ_ROWS_TOOL_NAME: Final = "chat_attachment_read_rows"
FIND_TOOL_NAME: Final = "chat_attachment_find"
VIEW_IMAGE_TOOL_NAME: Final = "chat_attachment_view_image"
CHAT_ATTACHMENT_TOOL_NAMES: Final = (
    LIST_TOOL_NAME,
    READ_SECTION_TOOL_NAME,
    READ_ROWS_TOOL_NAME,
    FIND_TOOL_NAME,
    VIEW_IMAGE_TOOL_NAME,
)
READ_SECTION_CHARACTERS: Final = 8_000
READ_ROWS_MAX: Final = 200
FIND_MAX: Final = 50
_SNIPPET_CHARACTERS: Final = 160


@dataclass(frozen=True)
class ChatAttachmentToolSpec:
    name: str
    title: str
    description: str
    input_schema: dict[str, Any]


_ATTACHMENT_ID = {"type": "string", "description": "chat_attachment_list の attachment_id。"}
CHAT_ATTACHMENT_TOOL_SPECS: Final[tuple[ChatAttachmentToolSpec, ...]] = (
    ChatAttachmentToolSpec(
        name=LIST_TOOL_NAME,
        title="添付の一覧",
        description=(
            "この会話に利用者が添付したファイルの一覧と、各ファイルの解析の状態、節（section_id・見出し・"
            "頁・文字数）と表のシート（列名・行数）を返す。本文は返さない。"
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    ),
    ChatAttachmentToolSpec(
        name=READ_SECTION_TOOL_NAME,
        title="添付の節の読み取り",
        description=(
            f"添付ファイルの 1 つの節の本文を {READ_SECTION_CHARACTERS} 文字ずつ返す。"
            "続きがあれば next_offset を返す。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "attachment_id": _ATTACHMENT_ID,
                "section_id": {
                    "type": "string",
                    "description": "chat_attachment_list の section_id。",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "読み始める文字の位置。最初は 0、続きは前の結果の next_offset。",
                },
            },
            "required": ["attachment_id", "section_id", "offset"],
            "additionalProperties": False,
        },
    ),
    ChatAttachmentToolSpec(
        name=READ_ROWS_TOOL_NAME,
        title="添付の行の読み取り",
        description=(
            "添付の Excel・CSV のシートの行を「列名 → 値」で返す。"
            "続きがあれば next_start_row を返す。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "attachment_id": _ATTACHMENT_ID,
                "sheet": {"type": "string", "description": "chat_attachment_list の sheet。"},
                "start_row": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "最初の行の番号（列名の行を除いたデータの行。1 始まり）。",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": READ_ROWS_MAX,
                    "description": f"返す行の数（1〜{READ_ROWS_MAX}）。",
                },
            },
            "required": ["attachment_id", "sheet", "start_row", "limit"],
            "additionalProperties": False,
        },
    ),
    ChatAttachmentToolSpec(
        name=FIND_TOOL_NAME,
        title="添付の中の検索",
        description=(
            "添付ファイルの本文と表の行から、文字が一致する箇所（節・行と前後の文）を探す。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "minLength": 1,
                    "description": "探す文字（大文字と小文字・全角と半角は区別しない）。",
                },
                "attachment_id": {
                    "type": ["string", "null"],
                    "description": "探す添付ファイル。null なら会話のすべての添付。",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": FIND_MAX,
                    "description": f"返す箇所の数（1〜{FIND_MAX}）。",
                },
            },
            "required": ["text", "attachment_id", "limit"],
            "additionalProperties": False,
        },
    ),
    ChatAttachmentToolSpec(
        name=VIEW_IMAGE_TOOL_NAME,
        title="添付の画像の確認",
        description=(
            "添付の画像（kind が image）の原画像を読む。画像の中の文字・図・表・画面の細部を、"
            "質問に照らして確かめるときに使う。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "attachment_id": _ATTACHMENT_ID,
                "focus": {
                    "type": "string",
                    "minLength": 1,
                    "description": "画像で確かめたいこと（質問に答えるのに要る観点を具体的に）。",
                },
            },
            "required": ["attachment_id", "focus"],
            "additionalProperties": False,
        },
    ),
)


@dataclass(frozen=True)
class AttachmentImage:
    """`chat_attachment_view_image` の結果（文字の結果と、原画像のバイト列）。"""

    output: dict[str, Any]
    data: bytes
    media_type: str


class ChatAttachmentToolError(ValueError):
    """モデルが読んで呼び直せる、引数の誤り。"""


# 解析の状態の表示（索引の行）。
_STATUS_LABELS: Final = {"pending": "未解析", "parsing": "解析中", "failed": "解析に失敗"}
# 画像から読み取った文の由来（#2652。原画像そのものではない）。
IMAGE_TRANSCRIPTION_SOURCE: Final = "image_transcription"

# Excel だけの索引の項目（#2544）。値の無い添付（Excel 以外・古い索引）では一覧に出さない。
_EXCEL_SECTION_KEYS: Final = ("sheet",)
_EXCEL_SHEET_KEYS: Final = ("layout", "source_sheet", "region")


def _without_empty(item: dict[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if key not in keys or value is not None}


def _location_fields(location: SourceLocation | None) -> dict[str, Any]:
    """Excel の元の場所（#2544。行番号・セル範囲、帳票はラベルと値のセル）。無ければ空。"""
    if location is None:
        return {}
    return location.model_dump(mode="json", exclude_none=True)


def _row_location(sheet: ParsedSheet, index: int) -> SourceLocation | None:
    """シートの `index` 番目（0 始まり）の行の元の場所。"""
    locations = sheet.locations or []
    return locations[index] if index < len(locations) else None


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _int_argument(
    arguments: Mapping[str, Any], name: str, *, minimum: int, maximum: int | None = None
) -> int:
    value = arguments.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChatAttachmentToolError(f"{name} は整数で指定してください。")
    if value < minimum or (maximum is not None and value > maximum):
        upper = f"{maximum} 以下" if maximum is not None else ""
        raise ChatAttachmentToolError(f"{name} は {minimum} 以上{upper}で指定してください。")
    return value


def _str_argument(arguments: Mapping[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ChatAttachmentToolError(f"{name} を指定してください。")
    return value.strip()


class ChatAttachmentToolset:
    """会話の添付を読むツール（`records` はその会話の期限内の送信した添付）。

    `load` は添付を読めるようにする関数（未解析なら解析する。`ensure_parsed`）。
    """

    def __init__(
        self,
        records: Sequence[ChatAttachmentRecord],
        load: Callable[[str], AttachmentReadResult],
        content: Callable[[str], bytes | None] = lambda _attachment_id: None,
    ) -> None:
        self._records = {record.attachment_id: record for record in records}
        self._order = [record.attachment_id for record in records]
        self._load = load
        # 原本（期限内の行だけ。画像を読むツールが使う）。
        self._content = content
        self._ready: dict[str, AttachmentReadResult] = {}

    @property
    def records(self) -> list[ChatAttachmentRecord]:
        return [self._records[attachment_id] for attachment_id in self._order]

    def __bool__(self) -> bool:
        return bool(self._order)

    def read(self, attachment_id: str) -> AttachmentReadResult:
        """添付を読めるようにする（解析の結果。読めた結果はこのツールの間は覚える）。"""
        if attachment_id not in self._records:
            raise ChatAttachmentToolError(
                "この会話に無い attachment_id です。"
                "chat_attachment_list の attachment_id を指定してください。"
            )
        cached = self._ready.get(attachment_id)
        if cached is not None:
            return cached
        result = self._load(attachment_id)
        if result.status == "ready" and result.parsed is not None:
            self._ready[attachment_id] = result
            if result.record is not None:
                self._records[attachment_id] = result.record
        return result

    def parsed(self, attachment_id: str) -> ParsedAttachment:
        result = self.read(attachment_id)
        if result.status != "ready" or result.parsed is None:
            raise ChatAttachmentToolError(_unreadable_message(result))
        return result.parsed

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name == LIST_TOOL_NAME:
            return self.list_attachments()
        if name == READ_SECTION_TOOL_NAME:
            return self.read_section(
                _str_argument(arguments, "attachment_id"),
                _str_argument(arguments, "section_id"),
                _int_argument(arguments, "offset", minimum=0),
            )
        if name == READ_ROWS_TOOL_NAME:
            return self.read_rows(
                _str_argument(arguments, "attachment_id"),
                _str_argument(arguments, "sheet"),
                _int_argument(arguments, "start_row", minimum=1),
                _int_argument(arguments, "limit", minimum=1, maximum=READ_ROWS_MAX),
            )
        if name == VIEW_IMAGE_TOOL_NAME:
            return self.view_image(
                _str_argument(arguments, "attachment_id"), _str_argument(arguments, "focus")
            ).output
        if name == FIND_TOOL_NAME:
            attachment_id = arguments.get("attachment_id")
            if attachment_id is not None and not isinstance(attachment_id, str):
                raise ChatAttachmentToolError("attachment_id は文字列か null で指定してください。")
            return self.find(
                _str_argument(arguments, "text"),
                attachment_id=attachment_id or None,
                limit=_int_argument(arguments, "limit", minimum=1, maximum=FIND_MAX),
            )
        raise ChatAttachmentToolError(f"{name} は添付のツールではありません。")

    def list_attachments(self) -> dict[str, Any]:
        return {"attachments": [self._listed(attachment_id) for attachment_id in self._order]}

    def _listed(self, attachment_id: str) -> dict[str, Any]:
        record = self._records[attachment_id]
        if record.kind == "image" and attachment_id not in self._ready:
            # 画像は一覧で読み取らない（原画像は `chat_attachment_view_image` で質問に応じて読む）。
            return {
                "attachment_id": record.attachment_id,
                "file_name": record.file_name,
                "kind": record.kind,
                "size_bytes": record.size_bytes,
                "media_type": record.media_type,
                "parse_status": record.outline.parse_status,
                "message": f"原画像は {VIEW_IMAGE_TOOL_NAME} で読めます。",
            }
        result = self.read(attachment_id)
        item: dict[str, Any] = {
            "attachment_id": record.attachment_id,
            "file_name": record.file_name,
            "kind": record.kind,
            "size_bytes": record.size_bytes,
            "parse_status": result.status,
        }
        if result.status != "ready" or result.parsed is None:
            return {**item, "message": _unreadable_message(result)}
        outline = result.parsed.outline()
        if result.parsed.is_empty():
            item["message"] = "読める文字・表の行がありません。"
        if outline.pages_without_text:
            item["pages_without_text"] = list(outline.pages_without_text)
        if record.kind == "image":
            item["content_source"] = IMAGE_TRANSCRIPTION_SOURCE
        return {
            **item,
            "warnings": list(outline.warnings),
            "sections": [
                _without_empty(section.model_dump(mode="json"), _EXCEL_SECTION_KEYS)
                for section in outline.sections
            ],
            "sheets": [
                _without_empty(sheet.model_dump(mode="json"), _EXCEL_SHEET_KEYS)
                for sheet in outline.sheets
            ],
        }

    def read_section(self, attachment_id: str, section_id: str, offset: int) -> dict[str, Any]:
        parsed = self.parsed(attachment_id)
        section = next((item for item in parsed.sections if item.section_id == section_id), None)
        if section is None:
            raise ChatAttachmentToolError(
                "この添付に無い section_id です。"
                "chat_attachment_list の section_id を指定してください。"
            )
        content = section.text[offset : offset + READ_SECTION_CHARACTERS]
        end = offset + len(content)
        return {
            "attachment_id": attachment_id,
            "file_name": self._records[attachment_id].file_name,
            "section_id": section.section_id,
            "title": section.title,
            "page": section.page,
            **({"sheet": section.sheet} if section.sheet else {}),
            **_location_fields(section.location),
            **(
                {"content_source": IMAGE_TRANSCRIPTION_SOURCE}
                if self._records[attachment_id].kind == "image"
                else {}
            ),
            "content": content,
            "next_offset": end if end < len(section.text) else None,
        }

    def _sheet(self, attachment_id: str, sheet_name: str) -> ParsedSheet:
        record = self._records.get(attachment_id)
        if record is not None and record.kind not in TABULAR_KINDS:
            raise ChatAttachmentToolError(
                "この添付は表（Excel・CSV）ではありません。"
                "chat_attachment_read_section で読んでください。"
            )
        parsed = self.parsed(attachment_id)
        sheet = next((item for item in parsed.sheets if item.sheet == sheet_name), None)
        if sheet is None:
            names = "、".join(item.sheet for item in parsed.sheets)
            raise ChatAttachmentToolError(f"この添付に無いシートです。シート: {names}")
        return sheet

    def read_rows(
        self, attachment_id: str, sheet_name: str, start_row: int, limit: int
    ) -> dict[str, Any]:
        sheet = self._sheet(attachment_id, sheet_name)
        start = start_row - 1
        rows = sheet.rows[start : start + limit]
        end = start + len(rows)
        return {
            "attachment_id": attachment_id,
            "file_name": self._records[attachment_id].file_name,
            "sheet": sheet.sheet,
            "columns": list(sheet.columns),
            "rows": [
                {
                    "row_number": start + index + 1,
                    **_location_fields(_row_location(sheet, start + index)),
                    "values": dict(zip(sheet.columns, row, strict=False)),
                }
                for index, row in enumerate(rows)
            ],
            "row_count": len(sheet.rows),
            "next_start_row": end + 1 if end < len(sheet.rows) else None,
        }

    def find(self, text: str, *, attachment_id: str | None, limit: int) -> dict[str, Any]:
        needle = _normalized(text)
        targets = [attachment_id] if attachment_id else list(self._order)
        matches: list[dict[str, Any]] = []
        unreadable: list[dict[str, Any]] = []
        truncated = False
        for target in targets:
            if attachment_id is None:
                record = self._records[target]
                if record.kind == "image" and record.outline.parse_status != "ready":
                    # 読み取っていない画像は文字で探さない（原画像は画像を読むツールで）。
                    continue
                result = self.read(target)
                if result.status != "ready":
                    unreadable.append(
                        {
                            "attachment_id": target,
                            "file_name": self._records[target].file_name,
                            "parse_status": result.status,
                            "message": _unreadable_message(result),
                        }
                    )
                    continue
            parsed = self.parsed(target)
            file_name = self._records[target].file_name
            for section in parsed.sections:
                haystack = _normalized(section.text)
                position = haystack.find(needle)
                while position >= 0:
                    if len(matches) >= limit:
                        truncated = True
                        break
                    matches.append(
                        {
                            "attachment_id": target,
                            "file_name": file_name,
                            "section_id": section.section_id,
                            "sheet": section.sheet,
                            "row_number": None,
                            **_location_fields(section.location),
                            "snippet": _snippet(section.text, position, len(text)),
                        }
                    )
                    position = haystack.find(needle, position + max(len(needle), 1))
                if truncated:
                    break
            for sheet in parsed.sheets if not truncated else []:
                for index, row in enumerate(sheet.rows, start=1):
                    line = " / ".join(
                        f"{column}: {value}"
                        for column, value in zip(sheet.columns, row, strict=False)
                        if value is not None
                    )
                    position = _normalized(line).find(needle)
                    if position < 0:
                        continue
                    if len(matches) >= limit:
                        truncated = True
                        break
                    matches.append(
                        {
                            "attachment_id": target,
                            "file_name": file_name,
                            "section_id": None,
                            "sheet": sheet.sheet,
                            "row_number": index,
                            **_location_fields(_row_location(sheet, index - 1)),
                            "snippet": _snippet(line, position, len(text)),
                        }
                    )
                if truncated:
                    break
            if truncated:
                break
        found: dict[str, Any] = {"matches": matches, "truncated": truncated}
        if unreadable:
            found["unreadable"] = unreadable
        return found

    def view_image(self, attachment_id: str, focus: str) -> AttachmentImage:
        """画像の原本と参照（送信した期限内の画像だけ。解釈はしない）。"""
        record = self._records.get(attachment_id)
        if record is None:
            raise ChatAttachmentToolError(
                "この会話に無い attachment_id です。"
                "chat_attachment_list の attachment_id を指定してください。"
            )
        if record.kind != "image":
            raise ChatAttachmentToolError(
                "この添付は画像ではありません。chat_attachment_read_section か "
                "chat_attachment_read_rows で読んでください。"
            )
        data = self._content(attachment_id)
        if data is None:
            raise ChatAttachmentToolError("添付ファイルの期限が切れたか、削除されています。")
        return AttachmentImage(
            output={
                "attachment_id": attachment_id,
                "file_name": record.file_name,
                "kind": record.kind,
                "media_type": record.media_type,
                "content_sha256": record.content_sha256,
                "focus": focus,
            },
            data=data,
            media_type=record.media_type,
        )

    def index_lines(self) -> list[str]:
        """モデルへ渡す索引（1 つの添付 = 1 行。本文は入れない）。"""
        lines: list[str] = []
        for record in self.records:
            parts = [f"attachment_id={record.attachment_id}", f"種類={record.kind}"]
            status = record.outline.parse_status
            if status != "ready":
                parts.append(_STATUS_LABELS.get(status, status))
            elif record.outline.sections:
                parts.append(f"節 {len(record.outline.sections)}")
            for sheet in record.outline.sheets:
                parts.append(f"シート「{sheet.sheet}」{sheet.row_count} 行")
            lines.append(f"- {record.file_name}（{'、'.join(parts)}）")
        return lines


def _unreadable_message(result: AttachmentReadResult) -> str:
    """読めない添付の理由（モデルが読んで次の手を決める文）。"""
    if result.message:
        return result.message
    if result.status == "missing":
        return "添付ファイルの期限が切れたか、削除されています。"
    return "添付ファイルを読めませんでした。"


def _snippet(text: str, position: int, length: int) -> str:
    half = max((_SNIPPET_CHARACTERS - length) // 2, 20)
    start = max(position - half, 0)
    end = min(position + length + half, len(text))
    snippet = text[start:end].replace("\n", " ")
    return ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else "")
