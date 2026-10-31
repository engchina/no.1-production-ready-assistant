"""構造化レコードの JSON Lines（前処理の成果物の共通の契約。#2457）。

CSV・Excel・Word の前処理は、原本を 1 行 1 オブジェクトの JSON Lines にする。parser はこの JSONL を
外部の parser に渡さず、記録を 1 つずつ要素にする（場所・列名・表頭・見出しを chunk まで残す）。

形式（`STRUCTURED_RECORDS_CONTENT_TYPE`。製品独自の vendor type で、JSONL の MIME type は標準化
されていない）:

- UTF-8（BOM なし）、1 物理行に 1 つの JSON オブジェクト、行の区切りは LF、末尾は LF、空行なし。
  文字列の中の改行は JSON のエスケープ（``\\n``）のまま 1 行に収める（pretty-print しない）。
- すべての行が ``schema``・``schema_version``・``record_id``（ファイルの中で一意）・
  ``source_type``・``record_type`` を持つ。
- ``record_type``:
  - ``document``: 先頭の 1 行だけ。変換器・版・読まなかったシート・表頭の行と表頭のセルの文字
    （CSV）・診断。Word は位置の基準（``location_basis``）と、.doc を .docx にして読んだときの
    原本と派生物の対応（``conversion``）も持つ。
  - ``sheet``: Excel のシートの文脈（表頭の行・推定の信頼度・読み方・版式の判定・列と役割・診断。
    1 シートを複数の表に分けたときは表ごとに 1 行で、``table_index``・``region``・``title``
    を持つ）。
  - ``row``: 表の 1 行。``fields`` は列名・列の位置・役割・値の順序付きの配列。
  - ``procedure_step``: 手順書の 1 手順（``lines``）。
  - ``form_field``: 帳票（方眼紙）の 1 項目（``label``・``value``・ラベルと値のセル。#2516）。
    選択の記号を読んで値を選ばれた選択肢にしたときは、原文を ``value_original`` に持つ（#2526）。
  - ``preamble``: 表頭より上の行（``text``）。
  - ``note``: 表の下の注記の行（``text``・先頭の注記の印 ``marker``。#3566）。表の行のうち注記の印を
    セルに含む行は、その注記の文を ``row`` の ``notes`` にも持つ。
  - ``section``: Word の見出し（見出しの階層 ``heading_path``・段落の位置）。``parent_id`` は上位の
    見出し。
  - ``passage``: Word の見出しの下の意味のまとまり（段落・リスト・表と、表の前後の説明）。表示の行
    ごとには分けない。大きい節を複数の記録に分けたときは ``part``（まとまりの ID・順番・全体の位置）
    を持つ。位置（``WordLocation``）は本文の順の番号で、原本に無いページ番号は持たない。
  CSV・Excel の記録は ``parent_id`` で文脈の行（Excel は ``sheet``）を指し、``location``
  （元の行番号・セル範囲）と ``section_path`` を持つ。Excel の記録のうち図形・テキストボックスの
  文字を置いたセル範囲は ``shape_cells`` に持つ（#2526）。CSV の ``row_start`` / ``row_end`` は
  原本の物理行（引用符の中の改行を含む記録は複数行）で、``record`` は記録の番号（RFC 7111 の行）。
- 読み取り（`parse_structured_records`）は厳格にし、不正な JSON・オブジェクトでない行・空行・BOM・
  未知の schema・未対応の版・record_id の重複・document の行の欠落・存在しない parent_id を、行番号
  つきの診断で拒否する（任意の JSONL を構造化レコードとみなさない）。

Excel の記録は `rag_parser_core.sheet_records` の行の記録（`SheetRecordsDocument`）と相互に変換し、
要素化は `sheet_records_extraction` を使う（要素の ID・metadata・分割の挙動を変えない）。

解析エンジン「構造化レコード JSONL」（`STRUCTURED_RECORDS_JSONL_BACKEND`）は、要素の本文を記録の
境界のマーカー（`JSONL_RECORD_BOUNDARY`）で区切った検索用テキストを ``raw_text`` にする
（`with_record_boundaries`）。マーカーは記録と記録の間に独立した行で置き、先頭・末尾には置かない。
JSONL のファイルには置かない。原文の同じ文字列は ``<<<`` + k 個の ``\\`` +
``JSONL_RECORD_BOUNDARY>>>`` を k+1 個にする可逆のエスケープで区別する
（`escape_record_boundary` / `unescape_record_boundary`）。固定分割（``fixed_delimiter``）は記録の
境界で 1 記録 1 chunk にし、本文はエスケープを戻した原文にする（`split_record_boundary_text`。
マーカーと境界の改行は chunk に残さない）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from rag_parser_core.extraction import DocumentElement, StructuredExtraction
from rag_parser_core.sheet_records import (
    _BLOCK_TEXT_MAX_CHARS,
    ROW_BASIS_CSV,
    ROW_BASIS_KEY,
    ROW_BASIS_WORD_TABLE,
    SHEET_RECORD_CONTENT_KIND,
    SOURCE_LINE_END_KEY,
    SOURCE_LINE_START_KEY,
    TABLE_CELLS_KEY,
    TABLE_INDEX_KEY,
    TABLE_ROW_END_KEY,
    TABLE_ROW_KEY,
    TABLE_ROW_START_KEY,
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
    _block_text,
    column_letters,
    sheet_block_record_id,
    sheet_records_extraction,
    table_cells_metadata,
)

STRUCTURED_RECORDS_CONTENT_TYPE = "application/vnd.production-ready.structured-records+jsonl"
STRUCTURED_RECORDS_SCHEMA = "production-ready.structured-records"
STRUCTURED_RECORDS_SCHEMA_VERSION = 1
# 抽出の parser_artifacts に刻む schema の版の key。
STRUCTURED_RECORDS_ARTIFACT_KEY = "structured_records_schema_version"
# 要素の metadata に残す記録の ID（JSONL の行と要素・chunk を対応させる）。
STRUCTURED_RECORD_ID_KEY = "structured_record_id"
STRUCTURED_RECORDS_FILE_EXTENSION = ".jsonl"
# 1 つの要素の本文の上限（`sheet_records` と同じ。極端に長い行で chunk を壊さない）。
_RECORD_TEXT_MAX_CHARS = _BLOCK_TEXT_MAX_CHARS
# 解析エンジン「構造化レコード JSONL」（parser-structured-records-jsonl のサービス。#2457）。
STRUCTURED_RECORDS_JSONL_BACKEND = "structured_records_jsonl"
STRUCTURED_RECORDS_JSONL_PARSER_VERSION = "structured_records_jsonl_v1"
# 構造化レコードの JSONL を、構造化レコード JSONL 以外の解析エンジンが受けたときの診断。
STRUCTURED_RECORDS_REQUIRES_JSONL_BACKEND_WARNING = (
    "structured_records_requires_structured_records_jsonl"
)
# 検索用テキスト（raw_text）の記録と記録の間に、独立した行で置く境界のマーカー（ASCII の完全一致）。
JSONL_RECORD_BOUNDARY = "<<<JSONL_RECORD_BOUNDARY>>>"
# 抽出の parser_artifacts に刻む、raw_text の境界のマーカーと境界の数（記録数 − 1）。
STRUCTURED_RECORDS_BOUNDARY_ARTIFACT_KEY = "structured_records_record_boundary"
STRUCTURED_RECORDS_BOUNDARY_COUNT_ARTIFACT_KEY = "structured_records_record_boundary_count"
_BOUNDARY_SEPARATOR = f"\n{JSONL_RECORD_BOUNDARY}\n"
# ``<<<`` + k 個の ``\`` + ``JSONL_RECORD_BOUNDARY>>>``（k = 0 がマーカーそのもの）。
_BOUNDARY_FAMILY = re.compile(r"<<<(\\*)JSONL_RECORD_BOUNDARY>>>")
_ESCAPED_BOUNDARY_FAMILY = re.compile(r"<<<\\(\\*)JSONL_RECORD_BOUNDARY>>>")

SourceType = Literal["csv", "xlsx", "xls", "doc", "docx"]
_SPREADSHEET_SOURCE_TYPES = frozenset({"xlsx", "xls"})
_WORD_SOURCE_TYPES = frozenset({"doc", "docx"})


class _Line(BaseModel):
    """すべての行に共通の項目。"""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_: Literal["production-ready.structured-records"] = Field(
        default=STRUCTURED_RECORDS_SCHEMA, alias="schema"
    )
    schema_version: Literal[1] = STRUCTURED_RECORDS_SCHEMA_VERSION
    record_id: str = Field(min_length=1, max_length=200)
    source_type: SourceType


class RecordDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    cell: str | None = None
    detail: str | None = None


class SkippedPart(BaseModel):
    """読まなかった部分（Excel のシート）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    reason: str


class SourceConversion(BaseModel):
    """原本と、要素化に使った派生物の対応（Word の .doc を .docx にして読んだとき）。"""

    model_config = ConfigDict(extra="forbid")

    original_type: Literal["doc"]
    original_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    derived_type: Literal["docx"]
    derived_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    converter: str = Field(min_length=1)


class DocumentLine(_Line):
    """先頭の 1 行。文書全体の文脈。"""

    record_type: Literal["document"] = "document"
    converter: str = Field(min_length=1)
    converter_version: str = Field(min_length=1)
    header_row: int | None = Field(
        default=None, ge=1, description="CSV の表頭の物理行（1 始まり）。"
    )
    header: list[str] | None = Field(
        default=None,
        description=(
            "CSV の表頭の行のセルの文字（原本の列の順。空のセルは空文字）。記録の ``fields`` の"
            "``name`` は空・重複を補った名前で、表頭の文字はこちら。None は記録していない"
            "（この項目より前の変換器の記録）。"
        ),
    )
    location_basis: Literal["original", "derived"] | None = Field(
        default=None,
        description=(
            "Word の位置の基準。original は原本（.docx）、derived は .doc から変換した .docx の"
            "本文の順。"
        ),
    )
    conversion: SourceConversion | None = None
    skipped: list[SkippedPart] = Field(default_factory=list)
    diagnostics: list[RecordDiagnostic] = Field(default_factory=list)


class SheetLine(_Line):
    """Excel のシートの文脈。"""

    record_type: Literal["sheet"] = "sheet"
    sheet_index: int = Field(ge=0)
    name: str
    header_row: int | None = Field(default=None, ge=1)
    header_row_count: int = Field(default=1, ge=1)
    header_detection: HeaderDetection | None = None
    mode: SheetLayout = "table"
    layout_detection: LayoutDetection | None = None
    table_index: int | None = Field(default=None, ge=1)
    region: str | None = None
    title: str | None = None
    columns: list[SheetColumn] = Field(default_factory=list)
    diagnostics: list[RecordDiagnostic] = Field(default_factory=list)


class RecordLocation(BaseModel):
    """原本の場所（行番号は 1 始まりの元の番号。空行を除いても振り直さない）。"""

    model_config = ConfigDict(extra="forbid")

    row_start: int = Field(ge=1)
    row_end: int = Field(ge=1)
    cell_range: str | None = Field(default=None, description="Excel のセル範囲（例: A5:F5）。")
    record: int | None = Field(
        default=None,
        ge=1,
        description=(
            "CSV の記録の番号（RFC 7111 の行。表頭の記録を含め、空の行も 1 記録として先頭から"
            "数える。表計算のソフトで開いたときの行）。None は記録していない（この項目より前の"
            "変換器の記録）。"
        ),
    )


class RecordField(BaseModel):
    """表の 1 つのセル（列名・列の位置・役割・値）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    position: int = Field(ge=1, description="原本の列の位置（1 始まり）。")
    column: str | None = Field(default=None, description="Excel の列の記号（A, B, …）。")
    role: str | None = Field(default=None, description="列の役割（Excel。#1281）。")
    value: str


class _RecordLine(_Line):
    parent_id: str | None = None
    location: RecordLocation
    section_path: list[str] = Field(default_factory=list)
    shape_cells: list[str] | None = Field(
        default=None,
        description="Excel の図形・テキストボックスの文字を置いたセル範囲（無ければ省く。#2526）。",
    )


class RowLine(_RecordLine):
    record_type: Literal["row"] = "row"
    fields: list[RecordField] = Field(default_factory=list)
    notes: list[str] | None = Field(
        default=None,
        description=(
            "Excel の表の行のセルが注記の印（例: ※1）で指す、表の下の注記の文"
            "（無ければ省く。#3566）。"
        ),
    )


class ProcedureStepLine(_RecordLine):
    record_type: Literal["procedure_step"] = "procedure_step"
    lines: list[str] = Field(default_factory=list)


class FormFieldLine(_RecordLine):
    """帳票の 1 項目（``label`` の無い行は、ラベルと組にならなかった文）。"""

    record_type: Literal["form_field"] = "form_field"
    label: str | None = None
    value: str
    label_cell: str | None = None
    value_cell: str | None = None
    value_original: str | None = None


class PreambleLine(_RecordLine):
    record_type: Literal["preamble"] = "preamble"
    text: str


class NoteLine(_RecordLine):
    """Excel の表の下の注記の行（#3566）。``marker`` は先頭の注記の印（印の無い長い文は None）。"""

    record_type: Literal["note"] = "note"
    text: str
    marker: str | None = None


# ---- Word（形式別の拡張。source_type が doc / docx の行だけが使う）----------------------------


class WordLocation(BaseModel):
    """Word の本文の中の位置（要素化した .docx の本文の順。1 始まり。空の段落も数える）。

    ``body_*`` は本文の直下の段落と表を通して数えた番号、``paragraph_*`` は本文の直下の段落だけ
    （表の中の段落は数えない）、``table_indices`` は本文の表の番号。原本に無いページ番号は持たない。
    """

    model_config = ConfigDict(extra="forbid")

    body_start: int = Field(ge=1)
    body_end: int = Field(ge=1)
    paragraph_start: int | None = Field(default=None, ge=1)
    paragraph_end: int | None = Field(default=None, ge=1)
    table_indices: list[int] = Field(default_factory=list)


class WordParagraph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["paragraph"] = "paragraph"
    text: str
    body_index: int = Field(ge=1)
    paragraph_index: int = Field(ge=1)


class WordListItem(BaseModel):
    """リストの 1 項目（``marker`` は番号・記号の表示。番号の無い記号のリストは None）。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["list_item"] = "list_item"
    text: str
    body_index: int = Field(ge=1)
    paragraph_index: int = Field(ge=1)
    level: int = Field(default=0, ge=0, le=8)
    ordered: bool = False
    marker: str | None = None


class WordTable(BaseModel):
    """表（``rows`` はセルの文字。先頭の ``header_rows`` 行が表頭）。

    大きい表を分けたときは、各部分に表頭の行を繰り返し、``row_start`` / ``row_end`` に元の表の
    行の番号（1 始まり。表頭を含めて数える）を残す。
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["table"] = "table"
    body_index: int = Field(ge=1)
    table_index: int = Field(ge=1)
    header_rows: int = Field(default=0, ge=0)
    rows: list[list[str]] = Field(min_length=1)
    row_start: int = Field(default=1, ge=1)
    row_end: int = Field(ge=1)


WordBlock = Annotated[WordParagraph | WordListItem | WordTable, Field(discriminator="kind")]


class RecordPart(BaseModel):
    """大きい節を複数の記録に分けたときの部分（``group_id`` は分ける前のまとまりの ID）。"""

    model_config = ConfigDict(extra="forbid")

    group_id: str = Field(min_length=1, max_length=200)
    index: int = Field(ge=1)
    count: int = Field(ge=2)
    group_location: WordLocation


class SectionLine(_Line):
    """Word の見出し（``level`` は 0 が表題、1〜9 が見出しの階層）。"""

    record_type: Literal["section"] = "section"
    parent_id: str | None = None
    heading: str = Field(min_length=1)
    level: int = Field(ge=0, le=9)
    heading_path: list[str] = Field(min_length=1)
    location: WordLocation


class PassageLine(_Line):
    """Word の見出しの下の意味のまとまり（段落・リスト・表）。``parent_id`` は見出しの section。"""

    record_type: Literal["passage"] = "passage"
    parent_id: str | None = None
    section_path: list[str] = Field(default_factory=list)
    location: WordLocation
    blocks: list[WordBlock] = Field(default_factory=list)
    part: RecordPart | None = None


StructuredRecordLine = Annotated[
    DocumentLine
    | SheetLine
    | RowLine
    | ProcedureStepLine
    | FormFieldLine
    | PreambleLine
    | NoteLine
    | SectionLine
    | PassageLine,
    Field(discriminator="record_type"),
]
_LINE_ADAPTER: TypeAdapter[
    DocumentLine
    | SheetLine
    | RowLine
    | ProcedureStepLine
    | FormFieldLine
    | PreambleLine
    | NoteLine
    | SectionLine
    | PassageLine
] = TypeAdapter(StructuredRecordLine)
ContentLine = RowLine | ProcedureStepLine | FormFieldLine | PreambleLine | NoteLine | PassageLine
ContextLine = SheetLine | SectionLine


@dataclass(frozen=True)
class StructuredRecordsDocument:
    """読み取った JSONL（先頭の document の行と、続く行を順に持つ）。"""

    document: DocumentLine
    lines: tuple[ContextLine | ContentLine, ...]

    @property
    def source_type(self) -> str:
        return self.document.source_type


@dataclass(frozen=True)
class StructuredRecordsError:
    """読めなかった理由（`code`）と行番号（1 始まり。ファイル全体なら None）。"""

    code: str
    line: int | None = None

    def warning(self) -> str:
        suffix = f"@line{self.line}" if self.line is not None else ""
        return f"structured_records_invalid:{self.code}{suffix}"


def is_structured_records_content_type(content_type: str | None) -> bool:
    """前処理の構造化レコードの JSONL か（parameter の charset などは無視する）。"""
    normalized = (content_type or "").split(";", 1)[0].strip().casefold()
    return normalized == STRUCTURED_RECORDS_CONTENT_TYPE


def dump_structured_records(
    lines: Iterable[DocumentLine | ContextLine | ContentLine],
) -> bytes:
    """行を JSON Lines（UTF-8・LF・末尾 LF・1 行 1 オブジェクト）にする。"""
    encoded = [line.model_dump_json(by_alias=True, exclude_none=True) + "\n" for line in lines]
    return "".join(encoded).encode("utf-8")


def parse_structured_records(
    data: bytes,
) -> StructuredRecordsDocument | StructuredRecordsError:
    """JSONL を厳格に読む。契約に合わなければ `StructuredRecordsError` を返す。"""
    if data.startswith(b"\xef\xbb\xbf"):
        return StructuredRecordsError("bom")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return StructuredRecordsError("not_utf8")
    physical = text.split("\n")
    if physical and physical[-1] == "":
        physical.pop()
    if not physical:
        return StructuredRecordsError("empty")
    document: DocumentLine | None = None
    lines: list[ContextLine | ContentLine] = []
    seen_ids: set[str] = set()
    sheet_ids: set[str] = set()
    section_ids: set[str] = set()
    for number, raw in enumerate(physical, start=1):
        if not raw.strip():
            return StructuredRecordsError("blank_line", number)
        try:
            payload = json.loads(raw)
        except ValueError:
            return StructuredRecordsError("invalid_json", number)
        if not isinstance(payload, dict):
            return StructuredRecordsError("not_object", number)
        error = _schema_error(payload)
        if error is not None:
            return StructuredRecordsError(error, number)
        try:
            line = _LINE_ADAPTER.validate_python(payload)
        except ValidationError:
            return StructuredRecordsError("invalid_record", number)
        if line.record_id in seen_ids:
            return StructuredRecordsError("duplicate_record_id", number)
        seen_ids.add(line.record_id)
        if isinstance(line, DocumentLine):
            if number != 1:
                return StructuredRecordsError("document_not_first", number)
            if not _valid_source_conversion(line):
                return StructuredRecordsError("invalid_source_conversion", number)
            document = line
            continue
        if document is None:
            return StructuredRecordsError("document_missing", number)
        if line.source_type != document.source_type:
            return StructuredRecordsError("source_type_mismatch", number)
        word = document.source_type in _WORD_SOURCE_TYPES
        if isinstance(line, SectionLine | PassageLine):
            error = _word_line_error(line, word=word, section_ids=section_ids)
            if error is not None:
                return StructuredRecordsError(error, number)
            if isinstance(line, SectionLine):
                section_ids.add(line.record_id)
        elif word:
            # Word の JSONL は見出し（section）と意味のまとまり（passage）だけを持つ。
            return StructuredRecordsError("unsupported_record_type", number)
        elif isinstance(line, SheetLine):
            if document.source_type not in _SPREADSHEET_SOURCE_TYPES:
                return StructuredRecordsError("unsupported_record_type", number)
            sheet_ids.add(line.record_id)
        else:
            if isinstance(line, ProcedureStepLine | FormFieldLine | NoteLine) and (
                document.source_type not in _SPREADSHEET_SOURCE_TYPES
            ):
                return StructuredRecordsError("unsupported_record_type", number)
            if line.parent_id is not None and line.parent_id not in sheet_ids:
                return StructuredRecordsError("parent_not_found", number)
            if document.source_type in _SPREADSHEET_SOURCE_TYPES and line.parent_id is None:
                return StructuredRecordsError("parent_missing", number)
            if line.location.row_end < line.location.row_start:
                return StructuredRecordsError("invalid_location", number)
        lines.append(line)
    if document is None:
        return StructuredRecordsError("document_missing", 1)
    return StructuredRecordsDocument(document=document, lines=tuple(lines))


def _valid_source_conversion(line: DocumentLine) -> bool:
    """位置の基準と原本・派生物の対応が形式に合うか。

    .doc は派生物・.docx は原本の位置で、表計算（CSV・Excel）は位置の基準を持たない。
    """
    if line.source_type == "doc":
        return line.location_basis == "derived" and line.conversion is not None
    if line.source_type == "docx":
        return line.location_basis == "original" and line.conversion is None
    return line.location_basis is None and line.conversion is None


def _valid_word_location(location: WordLocation) -> bool:
    if location.body_end < location.body_start:
        return False
    start, end = location.paragraph_start, location.paragraph_end
    if (start is None) != (end is None):
        return False
    return start is None or end is None or end >= start


def _word_line_error(
    line: SectionLine | PassageLine, *, word: bool, section_ids: set[str]
) -> str | None:
    """Word の見出し・まとまりの行の診断（無ければ None）。"""
    if not word:
        return "unsupported_record_type"
    if line.parent_id is not None and line.parent_id not in section_ids:
        return "parent_not_found"
    if not _valid_word_location(line.location):
        return "invalid_location"
    if isinstance(line, PassageLine):
        if line.part is not None and (
            line.part.index > line.part.count or not _valid_word_location(line.part.group_location)
        ):
            return "invalid_part"
        if any(
            isinstance(block, WordTable) and block.row_end < block.row_start
            for block in line.blocks
        ):
            return "invalid_location"
        if not line.blocks and not line.section_path:
            return "empty_passage"
    return None


def _schema_error(payload: Mapping[str, Any]) -> str | None:
    """schema・版が本方式のものか（任意の JSONL を構造化レコードとみなさない）。"""
    if payload.get("schema") != STRUCTURED_RECORDS_SCHEMA:
        return "unknown_schema"
    version = payload.get("schema_version")
    if isinstance(version, bool) or version != STRUCTURED_RECORDS_SCHEMA_VERSION:
        return "unsupported_schema_version"
    return None


# ---- Excel（行の記録）との相互変換 ------------------------------------------------------


def _sheet_record_id(sheet_index: int) -> str:
    return f"sheet-{sheet_index:03d}"


def _column_position(letters: str) -> int:
    index = 0
    for char in letters.upper():
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index


def _diagnostics(items: Iterable[SheetDiagnostic]) -> list[RecordDiagnostic]:
    return [RecordDiagnostic(code=item.code, cell=item.cell, detail=item.detail) for item in items]


def sheet_records_to_lines(
    document: SheetRecordsDocument,
    *,
    converter: str,
    converter_version: str,
) -> list[DocumentLine | SheetLine | ContentLine]:
    """Excel の行の記録を JSONL の行にする（シートの文脈の行・前書き・記録・表の下の注記の順）。

    記録の ID は要素の ID（`sheet_records_extraction`）と同じにし、JSONL の行と要素を対応させる。
    """
    source_type = document.source_format
    lines: list[DocumentLine | SheetLine | ContentLine] = [
        DocumentLine(
            record_id="document",
            source_type=source_type,
            converter=converter,
            converter_version=converter_version,
            skipped=[
                SkippedPart(name=item.name, reason=item.reason) for item in document.skipped_sheets
            ],
        )
    ]
    for sheet_index, sheet in enumerate(document.sheets):
        sheet_id = _sheet_record_id(sheet_index)
        lines.append(
            SheetLine(
                record_id=sheet_id,
                source_type=source_type,
                sheet_index=sheet_index,
                name=sheet.name,
                header_row=sheet.header_row,
                header_row_count=sheet.header_row_count,
                header_detection=sheet.header_detection,
                mode=sheet.mode,
                layout_detection=sheet.layout_detection,
                table_index=sheet.table_index,
                region=sheet.region,
                title=sheet.title,
                columns=sheet.columns,
                diagnostics=_diagnostics(sheet.diagnostics),
            )
        )
        for preamble in sheet.preamble:
            lines.append(
                PreambleLine(
                    record_id=f"{sheet_id}-r{preamble.row_number}",
                    source_type=source_type,
                    parent_id=sheet_id,
                    location=RecordLocation(
                        row_start=preamble.row_number,
                        row_end=preamble.row_number,
                        cell_range=preamble.cell_range,
                    ),
                    shape_cells=preamble.shape_cells or None,
                    text=preamble.text,
                )
            )
        columns = {column.name: column for column in sheet.columns}
        for block in sheet.blocks:
            location = RecordLocation(
                row_start=block.row_start, row_end=block.row_end, cell_range=block.cell_range
            )
            record_id = sheet_block_record_id(sheet_id, block, sheet.mode)
            if block.kind == "form_field":
                lines.append(
                    FormFieldLine(
                        record_id=record_id,
                        source_type=source_type,
                        parent_id=sheet_id,
                        location=location,
                        section_path=block.section_path,
                        shape_cells=block.shape_cells or None,
                        label=block.label,
                        value=block.value,
                        label_cell=block.label_cell,
                        value_cell=block.value_cell,
                        value_original=block.value_original,
                    )
                )
                continue
            if block.kind == "procedure_step":
                lines.append(
                    ProcedureStepLine(
                        record_id=record_id,
                        source_type=source_type,
                        parent_id=sheet_id,
                        location=location,
                        section_path=block.section_path,
                        shape_cells=block.shape_cells or None,
                        lines=block.lines,
                    )
                )
                continue
            fields: list[RecordField] = []
            for name, value in block.values.items():
                column = columns.get(name)
                letter = column.column if column is not None else None
                fields.append(
                    RecordField(
                        name=name,
                        position=_column_position(letter) if letter else len(fields) + 1,
                        column=letter,
                        role=column.role if column is not None else None,
                        value=value,
                    )
                )
            lines.append(
                RowLine(
                    record_id=record_id,
                    source_type=source_type,
                    parent_id=sheet_id,
                    location=location,
                    section_path=block.section_path,
                    shape_cells=block.shape_cells or None,
                    fields=fields,
                    notes=block.notes or None,
                )
            )
        for note in sheet.notes:
            lines.append(
                NoteLine(
                    record_id=f"{sheet_id}-r{note.row_number}",
                    source_type=source_type,
                    parent_id=sheet_id,
                    location=RecordLocation(
                        row_start=note.row_number,
                        row_end=note.row_number,
                        cell_range=note.cell_range,
                    ),
                    shape_cells=note.shape_cells or None,
                    text=note.text,
                    marker=note.marker,
                )
            )
    return lines


def _form_field_block(line: FormFieldLine) -> SheetBlock:
    return SheetBlock(
        kind="form_field",
        row_start=line.location.row_start,
        row_end=line.location.row_end,
        cell_range=line.location.cell_range or "",
        section_path=line.section_path,
        label=line.label,
        value=line.value,
        label_cell=line.label_cell,
        value_cell=line.value_cell,
        value_original=line.value_original,
        shape_cells=line.shape_cells or [],
    )


def structured_records_to_sheet_records(
    document: StructuredRecordsDocument,
) -> SheetRecordsDocument:
    """Excel の JSONL を行の記録に戻す（要素化を `sheet_records_extraction` に任せる）。"""
    sheets: list[SheetRecords] = []
    by_id: dict[str, SheetRecords] = {}
    for line in document.lines:
        if isinstance(line, SectionLine | PassageLine):
            continue
        if isinstance(line, SheetLine):
            sheet = SheetRecords(
                name=line.name,
                header_row=line.header_row,
                header_row_count=line.header_row_count,
                header_detection=line.header_detection,
                mode=line.mode,
                layout_detection=line.layout_detection,
                table_index=line.table_index,
                region=line.region,
                title=line.title,
                columns=line.columns,
                diagnostics=[
                    SheetDiagnostic(code=item.code, cell=item.cell, detail=item.detail)
                    for item in line.diagnostics
                ],
            )
            sheets.append(sheet)
            by_id[line.record_id] = sheet
            continue
        sheet = by_id[str(line.parent_id)]
        if isinstance(line, PreambleLine):
            sheet.preamble.append(
                SheetPreambleRow(
                    row_number=line.location.row_start,
                    cell_range=line.location.cell_range or "",
                    text=line.text,
                    shape_cells=line.shape_cells or [],
                )
            )
        elif isinstance(line, NoteLine):
            sheet.notes.append(
                SheetNoteRow(
                    row_number=line.location.row_start,
                    cell_range=line.location.cell_range or "",
                    text=line.text,
                    marker=line.marker,
                    shape_cells=line.shape_cells or [],
                )
            )
        elif isinstance(line, FormFieldLine):
            sheet.blocks.append(_form_field_block(line))
        elif isinstance(line, ProcedureStepLine):
            sheet.blocks.append(
                SheetBlock(
                    kind="procedure_step",
                    row_start=line.location.row_start,
                    row_end=line.location.row_end,
                    cell_range=line.location.cell_range or "",
                    section_path=line.section_path,
                    lines=line.lines,
                    shape_cells=line.shape_cells or [],
                )
            )
        else:
            sheet.blocks.append(
                SheetBlock(
                    kind="row",
                    row_start=line.location.row_start,
                    row_end=line.location.row_end,
                    cell_range=line.location.cell_range or "",
                    section_path=line.section_path,
                    values={item.name: item.value for item in line.fields},
                    shape_cells=line.shape_cells or [],
                    notes=line.notes or [],
                )
            )
    source_format: Literal["xlsx", "xls"] = "xls" if document.source_type == "xls" else "xlsx"
    return SheetRecordsDocument(
        source_format=source_format,
        sheets=sheets,
        skipped_sheets=[
            SkippedSheet(name=item.name, reason=item.reason) for item in document.document.skipped
        ],
    )


# ---- 要素化 -----------------------------------------------------------------------------


def _csv_row_text(line: RowLine, *, max_chars: int | None = _RECORD_TEXT_MAX_CHARS) -> str:
    text = " / ".join(f"{item.name}: {item.value}" for item in line.fields if item.value.strip())
    return text if max_chars is None else text[:max_chars]


def _csv_table_metadata(line: RowLine, header: list[str] | None) -> dict[str, str | int]:
    """CSV の 1 データ行の場所の基準とセルの座標（#3262）。

    行の番号は記録の番号（RFC 7111 の行。表頭の記録を含めて先頭から数え、表計算のソフトで開いた
    ときの行）で、``table_row`` と ``table_row_start`` / ``table_row_end`` にする（引用符の中の
    改行を含む記録も 1 行）。原本の物理行の範囲は ``source_line_start`` / ``source_line_end``。
    記録の番号を記録していない古い記録は行の番号もセルの座標も持たず、表頭のセルの文字を記録して
    いない古い記録（``header`` が None）からは、補った列名で表頭を作らない。
    """
    metadata: dict[str, str | int] = {
        ROW_BASIS_KEY: ROW_BASIS_CSV,
        SOURCE_LINE_START_KEY: line.location.row_start,
        SOURCE_LINE_END_KEY: line.location.row_end,
    }
    record = line.location.record
    if record is None:
        return metadata
    metadata[TABLE_ROW_KEY] = record
    metadata[TABLE_ROW_START_KEY] = record
    metadata[TABLE_ROW_END_KEY] = record
    if header is None:
        return metadata
    cells = [
        (
            column_letters(item.position),
            [text] if item.position <= len(header) and (text := header[item.position - 1]) else [],
        )
        for item in line.fields
        if item.value.strip()
    ]
    if cells:
        metadata[TABLE_CELLS_KEY] = table_cells_metadata(cells)
    return metadata


def _csv_extraction(
    document: StructuredRecordsDocument,
    *,
    source_parser: str,
    parser_backend: str,
    parser_version: str,
) -> StructuredExtraction:
    """CSV の 1 データ行を 1 要素（列名つきの値。原本の物理行の位置）にする。"""
    elements: list[DocumentElement] = []
    for line in document.lines:
        if not isinstance(line, RowLine):
            continue
        text = _csv_row_text(line)
        if not text:
            continue
        elements.append(
            DocumentElement(
                kind="text",
                text=text,
                order=len(elements),
                element_id=line.record_id,
                content_kind=SHEET_RECORD_CONTENT_KIND,
                source_parser=source_parser,
                section_path=line.section_path,
                confidence=1.0,
                metadata={
                    STRUCTURED_RECORD_ID_KEY: line.record_id,
                    "row_start": line.location.row_start,
                    "row_end": line.location.row_end,
                    "record_kind": "row",
                    **(
                        {"csv_header_row": document.document.header_row}
                        if document.document.header_row is not None
                        else {}
                    ),
                    **_csv_table_metadata(line, document.document.header),
                },
            )
        )
    return StructuredExtraction(
        raw_text="\n".join(element.text for element in elements),
        document_type="SPREADSHEET",
        confidence=1.0,
        warnings=[],
        elements=elements,
        parser_artifacts={
            "source_parser": source_parser,
            "parser_backend": parser_backend,
            "parser_version": parser_version,
            "source_format": "csv",
            "record_count": len(elements),
        },
    )


def _markdown_cell(value: str) -> str:
    return " ".join(value.split()).replace("|", "\\|")


def _word_table_text(table: WordTable) -> str:
    """表を Markdown の表にする（表頭が無ければ先頭の行を見出しの行の位置に置く）。"""
    width = max(len(row) for row in table.rows)
    rows = [[*row, *([""] * (width - len(row)))] for row in table.rows]
    header_count = max(1, min(table.header_rows, len(rows)))
    header = [
        " / ".join(
            dict.fromkeys(cell for row in rows[:header_count] if (cell := _markdown_cell(row[i])))
        )
        for i in range(width)
    ]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in range(width)) + " |",
    ]
    lines.extend(
        "| " + " | ".join(_markdown_cell(cell) for cell in row) + " |"
        for row in rows[header_count:]
        if any(cell.strip() for cell in row)
    )
    return "\n".join(lines)


def _word_block_text(block: WordParagraph | WordListItem | WordTable) -> str:
    if isinstance(block, WordTable):
        return _word_table_text(block)
    if isinstance(block, WordListItem):
        indent = "  " * block.level
        return f"{indent}{block.marker or '-'} {block.text}"
    return block.text


def word_passage_text(line: PassageLine) -> str:
    """Word のまとまりの本文（節の見出しを先頭の行に置き、段落・リスト・表を続ける）。"""
    parts = [line.section_path[-1]] if line.section_path else []
    parts.extend(_word_block_text(block) for block in line.blocks)
    return "\n".join(part for part in parts if part.strip())


def _word_content_kind(line: PassageLine) -> str:
    kinds = {block.kind for block in line.blocks}
    if kinds == {"table"}:
        return "table"
    if kinds == {"list_item"}:
        return "list"
    return "text"


def _word_metadata(
    line: PassageLine, *, location_basis: str | None
) -> dict[str, str | int | float | bool | None]:
    location = line.location
    metadata: dict[str, str | int | float | bool | None] = {
        STRUCTURED_RECORD_ID_KEY: line.record_id,
        "record_kind": "passage",
        "section_level": len(line.section_path),
        "word_location_basis": location_basis,
        "word_body_start": location.body_start,
        "word_body_end": location.body_end,
        "word_block_kinds": ",".join(dict.fromkeys(block.kind for block in line.blocks)),
    }
    if location.paragraph_start is not None:
        metadata["word_paragraph_start"] = location.paragraph_start
        metadata["word_paragraph_end"] = location.paragraph_end
    if location.table_indices:
        metadata["word_table_indices"] = ",".join(str(item) for item in location.table_indices)
    tables = [block for block in line.blocks if isinstance(block, WordTable)]
    if len(tables) == 1:
        # 1 つの表（か、大きい表を分けた部分）だけを含むまとまりは、その表の行の範囲を持つ
        # （#3262）。複数の表を含むまとまりは、1 つの範囲で示せないので持たない。
        metadata[ROW_BASIS_KEY] = ROW_BASIS_WORD_TABLE
        metadata[TABLE_INDEX_KEY] = tables[0].table_index
        metadata[TABLE_ROW_START_KEY] = tables[0].row_start
        metadata[TABLE_ROW_END_KEY] = tables[0].row_end
    if line.part is not None:
        metadata["word_part_group"] = line.part.group_id
        metadata["word_part_index"] = line.part.index
        metadata["word_part_count"] = line.part.count
        metadata["word_part_body_start"] = line.part.group_location.body_start
        metadata["word_part_body_end"] = line.part.group_location.body_end
    return metadata


def _word_extraction(
    document: StructuredRecordsDocument,
    *,
    source_parser: str,
    parser_backend: str,
    parser_version: str,
) -> StructuredExtraction:
    """Word の意味のまとまり（passage）を 1 要素（見出しの階層・本文の位置）にする。"""
    elements: list[DocumentElement] = []
    sections = 0
    for line in document.lines:
        if isinstance(line, SectionLine):
            sections += 1
            continue
        if not isinstance(line, PassageLine):
            continue
        text = word_passage_text(line)[:_RECORD_TEXT_MAX_CHARS]
        if not text.strip():
            continue
        elements.append(
            DocumentElement(
                kind="text",
                text=text,
                order=len(elements),
                element_id=line.record_id,
                parent_id=line.parent_id,
                content_kind=_word_content_kind(line),
                source_parser=source_parser,
                section_path=line.section_path,
                confidence=1.0,
                metadata=_word_metadata(line, location_basis=document.document.location_basis),
            )
        )
    conversion = document.document.conversion
    return StructuredExtraction(
        raw_text="\n".join(element.text for element in elements),
        document_type="Word",
        confidence=1.0,
        warnings=[],
        elements=elements,
        parser_artifacts={
            "source_parser": source_parser,
            "parser_backend": parser_backend,
            "parser_version": parser_version,
            "source_format": document.source_type,
            "record_count": len(elements),
            "section_count": sections,
            "structured_records_location_basis": document.document.location_basis,
            **(
                {"structured_records_source_conversion": conversion.model_dump(mode="json")}
                if conversion is not None
                else {}
            ),
        },
    )


def structured_records_extraction(
    document: StructuredRecordsDocument,
    *,
    source_parser: str,
    parser_backend: str,
    parser_version: str,
) -> StructuredExtraction:
    """構造化レコードの JSONL を、記録を 1 つずつ要素にした抽出にする。"""
    if document.source_type in _SPREADSHEET_SOURCE_TYPES:
        extraction = sheet_records_extraction(
            structured_records_to_sheet_records(document),
            source_parser=source_parser,
            parser_backend=parser_backend,
            parser_version=parser_version,
        )
        # 要素の ID は記録の ID と同じ（`sheet_records_to_lines`）。JSONL の行と要素を対応させる。
        record_ids = {line.record_id for line in document.lines}
        extraction = extraction.model_copy(
            update={
                "elements": [
                    element.model_copy(
                        update={
                            "metadata": {
                                **element.metadata,
                                STRUCTURED_RECORD_ID_KEY: element.element_id,
                            }
                        }
                    )
                    if element.element_id in record_ids
                    else element
                    for element in extraction.elements
                ]
            }
        )
    elif document.source_type in _WORD_SOURCE_TYPES:
        extraction = _word_extraction(
            document,
            source_parser=source_parser,
            parser_backend=parser_backend,
            parser_version=parser_version,
        )
    else:
        extraction = _csv_extraction(
            document,
            source_parser=source_parser,
            parser_backend=parser_backend,
            parser_version=parser_version,
        )
    return extraction.model_copy(
        update={
            "parser_artifacts": {
                **extraction.parser_artifacts,
                STRUCTURED_RECORDS_ARTIFACT_KEY: STRUCTURED_RECORDS_SCHEMA_VERSION,
                "structured_records_converter": document.document.converter,
                "structured_records_converter_version": document.document.converter_version,
            }
        }
    )


# ---- 記録の境界（解析エンジン「構造化レコード JSONL」の検索用テキスト）-----------------------


class RecordBoundaryError(ValueError):
    """検索用テキストの記録の境界が契約に合わない（`code` は診断の理由）。"""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(
            f"structured_records_boundary_invalid:{code}" + (f" {detail}" if detail else "")
        )


def escape_record_boundary(text: str) -> str:
    """原文の ``<<<`` + k 個の ``\\`` + ``JSONL_RECORD_BOUNDARY>>>`` を k+1 個にする（可逆）。"""
    return _BOUNDARY_FAMILY.sub(
        lambda match: f"<<<\\{match.group(1)}JSONL_RECORD_BOUNDARY>>>", text
    )


def unescape_record_boundary(text: str) -> str:
    """`escape_record_boundary` を戻す（k+1 個の ``\\`` を k 個にする）。"""
    return _ESCAPED_BOUNDARY_FAMILY.sub(
        lambda match: f"<<<{match.group(1)}JSONL_RECORD_BOUNDARY>>>", text
    )


def join_record_boundary_text(texts: Iterable[str]) -> str:
    """記録の本文をエスケープし、独立した行のマーカーで区切る（先頭・末尾には置かない）。"""
    return _BOUNDARY_SEPARATOR.join(escape_record_boundary(text) for text in texts)


def has_record_boundaries(extraction: StructuredExtraction) -> bool:
    """raw_text が記録の境界のマーカーで区切られた抽出か（構造化レコード JSONL の解析）。"""
    return extraction.parser_artifacts.get(STRUCTURED_RECORDS_BOUNDARY_ARTIFACT_KEY) == (
        JSONL_RECORD_BOUNDARY
    )


@dataclass(frozen=True)
class RecordBoundaryPart:
    """境界で分けた 1 記録（``text`` はエスケープを戻した原文。offset は raw_text の位置）。"""

    text: str
    start_offset: int
    end_offset: int


def split_record_boundary_text(raw_text: str) -> list[RecordBoundaryPart]:
    """検索用テキストを記録の境界で分け、エスケープを戻す（マーカーと境界の改行は含めない）。

    マーカーが独立した行でない・空の記録があるときは `RecordBoundaryError` を出す
    （黙って結合・分割しない）。
    """
    text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    parts: list[RecordBoundaryPart] = []
    cursor = 0
    for index, piece in enumerate(text.split(_BOUNDARY_SEPARATOR)):
        if JSONL_RECORD_BOUNDARY in piece:
            raise RecordBoundaryError("boundary_not_on_own_line", f"record={index + 1}")
        leading = len(piece) - len(piece.lstrip())
        body = piece.strip()
        if not body:
            raise RecordBoundaryError("empty_record", f"record={index + 1}")
        parts.append(
            RecordBoundaryPart(
                text=unescape_record_boundary(body),
                start_offset=cursor + leading,
                end_offset=cursor + leading + len(body),
            )
        )
        cursor += len(piece) + len(_BOUNDARY_SEPARATOR)
    return parts


def record_boundary_plain_text(extraction: StructuredExtraction) -> str:
    """raw_text からマーカーを除き、エスケープを戻した原文（記録を改行でつなぐ）。

    境界の無い抽出はそのまま返す。境界が契約に合わなければ、raw_text からマーカーの行だけを
    除く（LLM の入力・表示にマーカーを渡さない。分割の診断は固定分割が行う）。
    """
    if not has_record_boundaries(extraction):
        return extraction.raw_text
    try:
        return "\n".join(part.text for part in split_record_boundary_text(extraction.raw_text))
    except RecordBoundaryError:
        lines = extraction.raw_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        return unescape_record_boundary(
            "\n".join(line for line in lines if line.strip() != JSONL_RECORD_BOUNDARY)
        )


def oversized_record_line(document: StructuredRecordsDocument) -> int | None:
    """本文が 1 要素の上限を超える記録の JSONL の行番号（無ければ None）。

    既存の要素化は上限で本文を切る。構造化レコード JSONL の解析は黙って切らずに診断で止める。
    行番号は document の行（1 行目）を含めた 1 始まり。
    """
    roles_by_sheet: dict[str, dict[str, str]] = {}
    for index, line in enumerate(document.lines):
        number = index + 2
        if isinstance(line, SheetLine):
            roles_by_sheet[line.record_id] = {
                column.name: column.role for column in line.columns if column.role
            }
            continue
        if isinstance(line, SectionLine):
            continue
        if isinstance(line, PassageLine):
            text = word_passage_text(line)
        elif isinstance(line, PreambleLine | NoteLine):
            text = line.text
        elif document.source_type not in _SPREADSHEET_SOURCE_TYPES:
            text = _csv_row_text(line, max_chars=None) if isinstance(line, RowLine) else ""
        elif isinstance(line, FormFieldLine):
            text = _block_text(_form_field_block(line), max_chars=None)
        else:
            block = SheetBlock(
                kind="procedure_step" if isinstance(line, ProcedureStepLine) else "row",
                row_start=line.location.row_start,
                row_end=line.location.row_end,
                cell_range=line.location.cell_range or "",
                section_path=line.section_path,
                lines=line.lines if isinstance(line, ProcedureStepLine) else [],
                values=(
                    {item.name: item.value for item in line.fields}
                    if isinstance(line, RowLine)
                    else {}
                ),
                notes=(line.notes or []) if isinstance(line, RowLine) else [],
            )
            text = _block_text(block, roles_by_sheet.get(str(line.parent_id)), max_chars=None)
        if len(text) > _RECORD_TEXT_MAX_CHARS:
            return number
    return None


def with_record_boundaries(extraction: StructuredExtraction) -> StructuredExtraction:
    """記録の要素の本文を境界のマーカーで区切り、検索用テキスト（raw_text）にする。"""
    texts = [
        element.text
        for element in extraction.elements
        if element.metadata.get(STRUCTURED_RECORD_ID_KEY) and element.text.strip()
    ]
    return extraction.model_copy(
        update={
            "raw_text": join_record_boundary_text(texts),
            "parser_artifacts": {
                **extraction.parser_artifacts,
                STRUCTURED_RECORDS_BOUNDARY_ARTIFACT_KEY: JSONL_RECORD_BOUNDARY,
                STRUCTURED_RECORDS_BOUNDARY_COUNT_ARTIFACT_KEY: max(0, len(texts) - 1),
            },
        }
    )


__all__ = [
    "JSONL_RECORD_BOUNDARY",
    "STRUCTURED_RECORDS_ARTIFACT_KEY",
    "STRUCTURED_RECORDS_BOUNDARY_ARTIFACT_KEY",
    "STRUCTURED_RECORDS_BOUNDARY_COUNT_ARTIFACT_KEY",
    "STRUCTURED_RECORDS_JSONL_BACKEND",
    "STRUCTURED_RECORDS_REQUIRES_JSONL_BACKEND_WARNING",
    "STRUCTURED_RECORDS_JSONL_PARSER_VERSION",
    "STRUCTURED_RECORDS_CONTENT_TYPE",
    "STRUCTURED_RECORDS_FILE_EXTENSION",
    "STRUCTURED_RECORDS_SCHEMA",
    "STRUCTURED_RECORDS_SCHEMA_VERSION",
    "STRUCTURED_RECORD_ID_KEY",
    "ContentLine",
    "ContextLine",
    "DocumentLine",
    "NoteLine",
    "PassageLine",
    "PreambleLine",
    "ProcedureStepLine",
    "RecordBoundaryError",
    "RecordBoundaryPart",
    "RecordDiagnostic",
    "RecordField",
    "RecordLocation",
    "RecordPart",
    "RowLine",
    "SectionLine",
    "FormFieldLine",
    "SheetLine",
    "SkippedPart",
    "SourceConversion",
    "StructuredRecordLine",
    "StructuredRecordsDocument",
    "StructuredRecordsError",
    "dump_structured_records",
    "escape_record_boundary",
    "has_record_boundaries",
    "is_structured_records_content_type",
    "join_record_boundary_text",
    "oversized_record_line",
    "parse_structured_records",
    "record_boundary_plain_text",
    "split_record_boundary_text",
    "sheet_records_to_lines",
    "structured_records_extraction",
    "structured_records_to_sheet_records",
    "unescape_record_boundary",
    "with_record_boundaries",
    "word_passage_text",
    "WordBlock",
    "WordListItem",
    "WordLocation",
    "WordParagraph",
    "WordTable",
]
