"""表計算ファイルの記録のモデル（Excel の読み方の選択肢と、シートごとの記録。#1221・#2516・#2525）。

`pr_backend_core.spreadsheet.read_excel` が Excel（.xlsx / .xlsm / .xls）を、シートごとの
「表頭・元の行番号・セル範囲・表示の値」を持つ記録（`SheetRecordsDocument`）にする。製品は
この記録を自分の用途の形にする（RAG は構造化レコードの JSONL と検索の要素。チャットの添付は
会話の作業ファイル）。

- 記録の単位（`SheetBlock`）は 3 種類: `row`（表の 1 行）・`procedure_step`（手順書の 1 手順）・
  `form_field`（帳票の 1 項目。ラベル・値・それぞれのセル）。
- シートの版式（`SheetRecords.mode`）は `table` / `procedure` / `form`。判定の根拠と信頼度は
  `layout_detection` に残す。1 シートを複数の表に分けたときは表ごとに 1 つの `SheetRecords`
  （`table_index`・`region`・`title`）。
- 列の役割（`SheetColumn.role`。#1281）は設定値の表の「説明・既定値・例示・現在値・推奨値・
  設定できる範囲」の列。値の列は本文で列名の後に役割の表示（`column_display_name`）を付ける。
- 表の下の注記（`SheetRecords.notes`。#3566）は表の記録とは別の文の行にする。注記の印（「※1」など）
  をセルに含む表の行は、その注記の文を `SheetBlock.notes` にも持つ。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SHEET_RECORDS_FORMAT: Literal["sheet_records"] = "sheet_records"
SHEET_RECORDS_FORMAT_VERSION = 1
# 範囲の指定（`A3:F200`、`手順!A1:D80`、`'売上 2026'!B2:H90`）。
_RANGE = re.compile(
    r"^(?:(?:'(?P<quoted>(?:[^']|'')+)'|(?P<sheet>[^!']+))!)?"
    r"(?P<c1>[A-Za-z]{1,3})(?P<r1>\d{1,7}):(?P<c2>[A-Za-z]{1,3})(?P<r2>\d{1,7})$"
)


@dataclass(frozen=True)
class ExcelRange:
    """読む範囲（1 始まりの行・列。シート名が無ければ選んだすべてのシート）。"""

    sheet: str | None
    min_row: int
    min_col: int
    max_row: int
    max_col: int

    def contains(self, row: int, col: int) -> bool:
        return self.min_row <= row <= self.max_row and self.min_col <= col <= self.max_col


def _column_index(letters: str) -> int:
    index = 0
    for char in letters.upper():
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index


def parse_excel_range(text: str) -> ExcelRange:
    """`[シート名!]A1:F20` を読む（不正なら ValueError）。"""
    match = _RANGE.match(text.strip())
    if match is None:
        raise ValueError(f"範囲は「A3:F200」か「シート名!A3:F200」の形で指定してください: {text}")
    sheet = match.group("quoted")
    sheet = sheet.replace("''", "'") if sheet is not None else match.group("sheet")
    rows = sorted((int(match.group("r1")), int(match.group("r2"))))
    cols = sorted((_column_index(match.group("c1")), _column_index(match.group("c2"))))
    if rows[0] < 1 or cols[1] > 16384 or rows[1] > 1048576:
        raise ValueError(f"範囲が Excel のシートの外です: {text}")
    return ExcelRange(
        sheet=sheet.strip() if sheet and sheet.strip() else None,
        min_row=rows[0],
        min_col=cols[0],
        max_row=rows[1],
        max_col=cols[1],
    )


# 列の役割（#1281）。definition=説明・定義、default=既定値（出荷時・初期値）、
# example=例示（記入例・設定例・サンプル）、current=記載時点の設定値（現在値・本番値）、
# recommended=推奨値、allowed=設定できる範囲・選択肢。
ColumnRole = Literal["definition", "default", "example", "current", "recommended", "allowed"]
COLUMN_ROLES: tuple[str, ...] = (
    "definition",
    "default",
    "example",
    "current",
    "recommended",
    "allowed",
)
# 選択肢の `column_roles` で「役割を付けない」を指定する値（表頭の語で決まる役割を打ち消す）。
COLUMN_ROLE_NONE = "none"
ColumnRoleSetting = Literal[
    "definition", "default", "example", "current", "recommended", "allowed", "none"
]
# 本文で列名の後に付ける役割の表示。値の列だけに付ける（説明の列は値ではないので付けない）。
# 「資料の」「記載時点の」は、資料に書かれた値で、利用者の環境の今の値ではないことを示す。
COLUMN_ROLE_TEXT_LABELS: dict[str, str] = {
    "default": "資料の既定値",
    "example": "例示の値",
    "current": "記載時点の設定値",
    "recommended": "資料の推奨値",
    "allowed": "設定できる範囲",
}


def column_display_name(name: str, role: str | None) -> str:
    """本文に出す列名（値の列は役割の表示を付ける。例: ``既定値［資料の既定値］``）。"""
    label = COLUMN_ROLE_TEXT_LABELS.get(role or "")
    if not label or name.strip() == label:
        return name
    return f"{name}［{label}］"


# 表の下の注記の行の先頭の印（#3566）。行の先頭に当たる正規表現で、当たった文字が注記の印になる。
# 業務の語ではなく、注記に広く使う記号・書き方だけを置く（資料に合わせて選択肢 `note_markers` で
# 替えられる）。
DEFAULT_NOTE_MARKERS: tuple[str, ...] = (
    r"[※＊*]\s*\d*",
    r"[(（]\s*注\s*\d*\s*[)）]",
    r"注\s*\d*\s*[:：.．)）]",
    r"注\s*\d+",
    r"(?i:notes?)\s*\d*\s*[:：.)]",
    r"[(（]\s*\d+\s*[)）]",
    r"\d+\s*[)）]",
)
_NOTE_MARKER_MAX_CHARS = 200


class ExcelOptions(BaseModel):
    """Excel の前処理の選択肢（Document Recipe の `excel_options`）。"""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["auto", "table", "procedure", "form"] = Field(
        default="auto",
        description=(
            "シートの読み方。auto は帳票（ラベルのすぐ右の値と結合セルを主に、行ごとの位置の"
            "不揃いと列の狭さも見て決める。普通の列幅の帳票も含む）なら form、手順書（番号と"
            "題名の列）なら procedure、密な表なら table に決める。"
        ),
    )
    header_row: int | None = Field(
        default=None,
        ge=1,
        le=1000,
        description="表頭の行（1 始まり）。未指定は先頭の行から推定する。",
    )
    header_row_count: int = Field(
        default=1,
        ge=1,
        le=5,
        description=(
            "表頭の行数。指定しなければ、表頭の行の横の結合から 2 段の表頭を推定する（#2532）。"
        ),
    )
    sheets: list[str] = Field(
        default_factory=list,
        max_length=100,
        description="読むシート名（未指定は表示されているすべてのシート）。",
    )
    exclude_sheets: list[str] = Field(
        default_factory=list, max_length=100, description="読まないシート名。"
    )
    include_hidden_sheets: bool = Field(default=False, description="非表示のシートも読むか。")
    include_hidden_cells: bool = Field(
        default=False,
        description=(
            "非表示の行・列のセルも読むか（#2526）。既定は読まない（オートフィルターで隠れた行は読む）。"
        ),
    )
    exclude_columns: list[str] = Field(
        default_factory=list,
        max_length=200,
        description="読まない列（列名か列の記号。例: 備考、F）。",
    )
    ranges: list[str] = Field(
        default_factory=list,
        max_length=50,
        description=(
            "読む範囲（例: A3:F200＝選んだすべてのシート、手順!A1:D80＝そのシートだけ）。"
            "範囲の外のセルは表頭の推定にも記録にも使わない。未指定はシート全体。"
        ),
    )

    split_tables: bool = Field(
        default=True,
        description=(
            "1 つのシートの、空の行・空の列で分かれた複数の表を別々に読むか（#2516）。"
            "表ごとに表頭を推定する。"
        ),
    )
    table_gap_rows: int = Field(
        default=1, ge=1, le=20, description="表を分ける空の行の数（これ以上続いたら別の表）。"
    )
    table_gap_columns: int = Field(
        default=1, ge=1, le=20, description="表を分ける空の列の数（これ以上続いたら別の表）。"
    )
    form_detection_threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            "auto で帳票（form）と判定する点数の下限（結合セルの割合・ラベルのすぐ右の値・"
            "行の位置の不揃い・列幅の狭さから計算する。#2516・#2530）。"
        ),
    )

    column_role_detection: Literal["auto", "off"] = Field(
        default="auto",
        description=(
            "列の役割（説明・既定値・例示・現在値・推奨値・設定できる範囲）を表頭の語から"
            "決めるか（#1281）。auto は語が一致した列だけに付け、あいまいな列には付けない。"
            "off は column_roles の指定だけを使う。"
        ),
    )
    column_roles: dict[str, ColumnRoleSetting] = Field(
        default_factory=dict,
        max_length=200,
        description=(
            "列の役割の指定（列名か列の記号 → 役割。none は役割を付けない）。"
            "表頭の語の判定より優先する。"
        ),
    )

    note_markers: list[str] = Field(
        default_factory=lambda: list(DEFAULT_NOTE_MARKERS),
        max_length=50,
        description=(
            "表の下の注記の行の先頭の印（正規表現。行の先頭に当てる）。表の後ろの、セルが"
            "1 つだけの行がこの印で始まれば、表の行ではなく注記にする（#3566）。"
            "空なら印では決めない（長い文の行だけを注記にする）。"
        ),
    )

    @field_validator("note_markers")
    @classmethod
    def _valid_note_markers(cls, value: list[str]) -> list[str]:
        cleaned = [item for item in value if item.strip()]
        for item in cleaned:
            if len(item) > _NOTE_MARKER_MAX_CHARS:
                raise ValueError(f"注記の印は {_NOTE_MARKER_MAX_CHARS} 文字までです: {item[:40]}")
            try:
                re.compile(item)
            except re.error as error:
                raise ValueError(
                    f"注記の印の正規表現が正しくありません: {item}（{error}）"
                ) from error
        return cleaned

    def note_marker_patterns(self) -> list[re.Pattern[str]]:
        return [re.compile(item) for item in self.note_markers]

    @field_validator("column_roles")
    @classmethod
    def _clean_column_roles(cls, value: dict[str, str]) -> dict[str, str]:
        return {key.strip(): role for key, role in value.items() if key.strip()}

    @field_validator("ranges")
    @classmethod
    def _valid_ranges(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        for item in cleaned:
            parse_excel_range(item)
        return cleaned

    def parsed_ranges(self) -> list[ExcelRange]:
        return [parse_excel_range(item) for item in self.ranges]


class HeaderDetection(BaseModel):
    method: Literal["configured", "detected"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


SheetLayout = Literal["table", "procedure", "form"]


class LayoutDetection(BaseModel):
    """シートの版式の判定（#2516）。

    ``confidence`` は ``layout`` の確からしさ（帳票は帳票の点数、表・手順書は 1 − 帳票の点数）。
    ``features`` は判定に使った構造の特徴（0〜1）。
    """

    layout: SheetLayout
    method: Literal["configured", "detected"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    features: dict[str, float] = Field(default_factory=dict)


class SheetColumn(BaseModel):
    name: str
    column: str = Field(description="列の記号（A, B, …）。")
    role: ColumnRole | None = Field(
        default=None, description="列の役割（決められない列は None。#1281）。"
    )
    role_method: Literal["configured", "detected"] | None = Field(
        default=None, description="役割の決め方（選択肢の指定か、表頭の語の判定か）。"
    )
    role_term: str | None = Field(
        default=None, description="役割を決めた表頭の語（判定のとき）か、指定の key。"
    )
    header_path: list[str] | None = Field(
        default=None,
        description=(
            "列の表頭のセルの文字（複数段の表頭は上の段から。横に結合した見出しは覆う列のすべてに"
            "入る。表頭の文字の無い列は空）。`name` はこれを「 / 」でつなぎ、重複を区別した名前。"
            "None は表頭の段を記録していない（この項目より前に作った記録）。"
        ),
    )


class SheetPreambleRow(BaseModel):
    row_number: int = Field(ge=1)
    cell_range: str
    text: str
    shape_cells: list[str] = Field(
        default_factory=list, description="図形・テキストボックスの文字を置いたセル範囲（#2526）。"
    )


class SheetNoteRow(SheetPreambleRow):
    """表の下の注記の行（#3566）。``marker`` は先頭の注記の印（印の無い長い文の行は None）。"""

    marker: str | None = None


class SheetBlock(BaseModel):
    """記録の単位（表の 1 行・手順書の 1 手順・帳票の 1 項目）。"""

    kind: Literal["row", "procedure_step", "form_field"]
    row_start: int = Field(ge=1, description="シートの元の行番号（1 始まり）。")
    row_end: int = Field(ge=1)
    cell_range: str = Field(description="セルの範囲（例: A5:F5、A5:F9）。")
    section_path: list[str] = Field(
        default_factory=list, description="手順の章・題名（表の行は空）。"
    )
    values: dict[str, str] = Field(default_factory=dict, description="表の行の列名と値。")
    lines: list[str] = Field(default_factory=list, description="手順の本文の行。")
    label: str | None = Field(
        default=None, description="帳票の項目のラベル（ラベルの無い文は None）。"
    )
    value: str = Field(default="", description="帳票の項目の値。")
    label_cell: str | None = Field(default=None, description="ラベルのセル（例: B3:E3）。")
    value_cell: str | None = Field(default=None, description="値のセル（例: F3:N3）。")
    value_original: str | None = Field(
        default=None,
        description=("選択の記号を読んで値を選ばれた選択肢にしたときの原文（例: ■A □B。#2526）。"),
    )
    shape_cells: list[str] = Field(
        default_factory=list,
        description="記録のうち、図形・テキストボックスの文字を置いたセル範囲（#2526）。",
    )
    notes: list[str] = Field(
        default_factory=list,
        description=(
            "表の行のセルが注記の印（例: ※1）で指す、表の下の注記の文（#3566）。1 行 = 1 記録の"
            "本文に注記を含める。"
        ),
    )


class SheetDiagnostic(BaseModel):
    code: str
    cell: str | None = None
    detail: str | None = None


class SheetRecords(BaseModel):
    name: str
    header_row: int | None = Field(default=None, ge=1)
    header_row_count: int = 1
    header_detection: HeaderDetection | None = None
    mode: SheetLayout = "table"
    layout_detection: LayoutDetection | None = None
    table_index: int | None = Field(
        default=None, ge=1, description="1 シートを複数の表に分けたときの表の番号（1 始まり）。"
    )
    region: str | None = Field(default=None, description="分けた表のセル範囲（例: A12:F30）。")
    title: str | None = Field(default=None, description="分けた表の題名（表頭より上の最初の行）。")
    columns: list[SheetColumn] = Field(default_factory=list)
    preamble: list[SheetPreambleRow] = Field(default_factory=list)
    blocks: list[SheetBlock] = Field(default_factory=list)
    notes: list[SheetNoteRow] = Field(
        default_factory=list,
        description="表の下の注記の行（表の行・表頭にしない。#3566）。",
    )
    diagnostics: list[SheetDiagnostic] = Field(default_factory=list)


class SkippedSheet(BaseModel):
    name: str
    reason: str


class SheetRecordsDocument(BaseModel):
    format: Literal["sheet_records"] = SHEET_RECORDS_FORMAT
    format_version: int = SHEET_RECORDS_FORMAT_VERSION
    source_format: Literal["xlsx", "xls"]
    sheets: list[SheetRecords] = Field(default_factory=list)
    skipped_sheets: list[SkippedSheet] = Field(default_factory=list)
