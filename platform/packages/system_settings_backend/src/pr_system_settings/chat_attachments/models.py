"""会話の添付の型（保存・解析・API・ツールで共有する。#2517）。

解析の状態（#2652）: アップロードは原本の保存だけで、解析は送信した後の読み取りで始める。状態は
`OUTLINE_JSON` の `parse_state`（列は足さない）。`parse_state` の無い行は #2652 より前に
アップロードの時点で解析した行で、解析済みとして読む。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ChatAttachmentProduct = Literal["rag", "nl2sql", "agent"]
ChatAttachmentKind = Literal["pdf", "word", "powerpoint", "excel", "csv", "text", "image"]
# 解析の状態（#2652）。pending = 未解析・parsing = 解析中（`lease_expires_at` まで）・ready = 完了
# （内容が空でも ready）・failed = 失敗（`error_code`）。
AttachmentParseStatus = Literal["pending", "parsing", "ready", "failed"]
# 添付を読むときの結果（`AttachmentReadResult.status`）。missing = 期限切れ・削除。
AttachmentReadStatus = Literal["ready", "parsing", "failed", "missing"]
# 表として読める種類（`chat_attachment_read_rows` と NL2SQL の照合の対象）。
TABULAR_KINDS: frozenset[str] = frozenset({"excel", "csv"})

# 1 つのセルの値。数値・真偽は保ち、日付・時刻は ISO 8601 の文字列にする。
CellValue = str | int | float | bool | None

# Excel の読み方の版（#2544）。解析の結果（`PARSED_JSON` / `OUTLINE_JSON`）の中に置き、古い版で
# 解析した Excel の添付は、使うときに原本から解析し直す（DB の列は足さない）。
# 3: 表の下の注記の行を表にせず「注記」の節にした（#3566）。
EXCEL_PARSER_VERSION = 3


class SourceLocation(BaseModel):
    """Excel の元の場所（#2544。シートの元の行番号・セル範囲。空の行を除いても振り直さない）。"""

    row_start: int
    row_end: int
    cell_range: str
    # 帳票の項目のラベルと値のセル（帳票だけ）。
    label_cell: str | None = None
    value_cell: str | None = None


class ParsedSection(BaseModel):
    """文書の 1 つの節（PDF は 1 頁、PowerPoint は 1 スライド、Word・テキストは見出しの区切り）。"""

    section_id: str
    title: str
    level: int = 1
    page: int | None = None
    text: str
    # Excel の節（手順書の手順・表頭より上の行）の、シートと元の場所（#2544）。
    sheet: str | None = None
    location: SourceLocation | None = None


class ParsedSheet(BaseModel):
    """表の 1 つのシート（「1 行 = 列名 + 値」。`rows` の各行は `columns` と同じ並び）。"""

    sheet: str
    columns: list[str]
    rows: list[list[CellValue]]
    # Excel の行ごとの元の場所（`rows` と同じ並び。#2544。CSV と古い解析の結果は None）。
    locations: list[SourceLocation] | None = None
    # Excel の読み方（table = 表 / form = 帳票の「項目・値」）と、元のシート名・範囲（#2544）。
    layout: str | None = None
    source_sheet: str | None = None
    region: str | None = None


class ParsedAttachment(BaseModel):
    """解析の結果（`PARSED_JSON`）。"""

    sections: list[ParsedSection] = Field(default_factory=list)
    sheets: list[ParsedSheet] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # 読み方の版（Excel だけ。#2544。None は版を持たない古い結果か Excel 以外）。
    parser_version: int | None = None
    # 文字を取り出せなかった頁（PDF。画像だけの頁など。#2652）。黙って捨てず、索引にも出す。
    pages_without_text: list[int] = Field(default_factory=list)
    # 内容を取り出したもの（`pdf@1` などの parser と版、画像は `vision:<モデル>@<指示の版>`）。
    extractor: str | None = None

    def is_empty(self) -> bool:
        """読める内容（節・行）が無いか（解析の失敗とは別。#2652）。"""
        return not any(section.text for section in self.sections) and not any(
            sheet.rows for sheet in self.sheets
        )

    def outline(self) -> AttachmentOutline:
        return AttachmentOutline(
            sections=[
                SectionOutline(
                    section_id=section.section_id,
                    title=section.title,
                    level=section.level,
                    page=section.page,
                    characters=len(section.text),
                    sheet=section.sheet,
                )
                for section in self.sections
            ],
            sheets=[
                SheetOutline(
                    sheet=sheet.sheet,
                    columns=sheet.columns,
                    row_count=len(sheet.rows),
                    layout=sheet.layout,
                    source_sheet=sheet.source_sheet,
                    region=sheet.region,
                )
                for sheet in self.sheets
            ],
            warnings=list(self.warnings),
            parser_version=self.parser_version,
            pages_without_text=list(self.pages_without_text),
            extractor=self.extractor,
        )


class SectionOutline(BaseModel):
    section_id: str
    title: str
    level: int = 1
    page: int | None = None
    characters: int
    # Excel の節のシート（#2544）。
    sheet: str | None = None


class SheetOutline(BaseModel):
    sheet: str
    columns: list[str]
    row_count: int
    # Excel の読み方・元のシート名・範囲（#2544。CSV と古い索引は None）。
    layout: str | None = None
    source_sheet: str | None = None
    region: str | None = None


class AttachmentParseState(BaseModel):
    """解析の状態（`OUTLINE_JSON` の `parse_state`。#2652）。

    書き換えは `revision` を比べて 1 つだけが成功する（同時のアクセス・再試行で二重に解析しない。
    古い試行の遅い完了は `revision` が合わずに捨てる）。
    """

    status: AttachmentParseStatus = "pending"
    # 書き換えのたびに 1 増やす（比べて書き換える。`store.replace_state`）。
    revision: int = 1
    # 解析の結果を再利用するキー（原本の hash・parser の版・出力に影響する解析の設定）。
    parse_key: str | None = None
    attempt_id: str | None = None
    # 同じ `parse_key` で続けて失敗した回数。
    attempts: int = 0
    # 解析中の試行の期限（過ぎたら終わった process の試行とみなし、引き継ぐ）。
    lease_expires_at: datetime | None = None
    # 失敗の後、もう一度試してよい時刻（None は試さない）。
    retry_after: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    updated_at: datetime | None = None


class AttachmentOutline(BaseModel):
    """節・シートの索引と解析の状態（`OUTLINE_JSON`。一覧・モデルへの索引に使い、本文を持たない）。"""

    sections: list[SectionOutline] = Field(default_factory=list)
    sheets: list[SheetOutline] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    parser_version: int | None = None
    pages_without_text: list[int] = Field(default_factory=list)
    extractor: str | None = None
    # 解析の状態（#2652）。None は #2652 より前にアップロードの時点で解析した行（解析済み）。
    parse_state: AttachmentParseState | None = None

    @property
    def parse_status(self) -> AttachmentParseStatus:
        return self.parse_state.status if self.parse_state is not None else "ready"

    @property
    def revision(self) -> int:
        return self.parse_state.revision if self.parse_state is not None else 0


class ChatAttachmentRecord(BaseModel):
    """保存した添付の 1 行（原本と解析の本文を除く）。"""

    attachment_id: str
    product: ChatAttachmentProduct
    owner_user_uuid: str
    uploaded_by_user_uuid: str
    conversation_id: str | None = None
    file_name: str
    media_type: str
    kind: ChatAttachmentKind
    size_bytes: int
    content_sha256: str
    outline: AttachmentOutline
    created_at: datetime
    expires_at: datetime


class ChatAttachmentData(BaseModel):
    """API が返す添付の要約。"""

    attachment_id: str
    file_name: str
    media_type: str
    kind: ChatAttachmentKind
    size_bytes: int
    conversation_id: str | None = None
    section_count: int
    sheets: list[SheetOutline] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # 解析の状態（#2652。アップロードの直後は pending。解析は送信した後に読むときに行う）。
    parse_status: AttachmentParseStatus = "pending"
    created_at: datetime
    expires_at: datetime
    # 添付した人が見る人でないとき（system_admin がほかの利用者の会話で添付した）だけ False。
    uploaded_by_me: bool = True

    @classmethod
    def from_record(
        cls, record: ChatAttachmentRecord, *, viewer_user_uuid: str
    ) -> ChatAttachmentData:
        return cls(
            attachment_id=record.attachment_id,
            file_name=record.file_name,
            media_type=record.media_type,
            kind=record.kind,
            size_bytes=record.size_bytes,
            conversation_id=record.conversation_id,
            section_count=len(record.outline.sections),
            sheets=record.outline.sheets,
            warnings=record.outline.warnings,
            parse_status=record.outline.parse_status,
            created_at=record.created_at,
            expires_at=record.expires_at,
            uploaded_by_me=record.uploaded_by_user_uuid == viewer_user_uuid,
        )


class ChatAttachmentLimitsData(BaseModel):
    """画面が添付の前に確かめる上限（`platform/.env` の共通の設定）。"""

    max_bytes: int
    max_files: int
    ttl_hours: int
    # 受け付ける拡張子（`.pdf` など。画面の `accept` と案内）。
    accept: list[str]
    # 製品の「運用設定 › チャットの添付」で有効か（無効なら画面は添付の操作を出さない）。
    enabled: bool = False
    # 画像を読み取れるモデル（既定の Vision モデル）があるか。画像は無くても添付でき（#2652）、
    # 読み取るときにモデルが無いことを示す。
    image_enabled: bool


class ChatAttachmentListData(BaseModel):
    items: list[ChatAttachmentData]
    limits: ChatAttachmentLimitsData


class ChatAttachmentReference(BaseModel):
    """発話の記録に残す添付（名前と種類。期限で添付が消えても発話の表示に残す）。"""

    attachment_id: str
    file_name: str
    kind: ChatAttachmentKind
    size_bytes: int = 0
    # 送信の時点の原本の hash（読む対象と版を固定する。#2652。古い発話の記録は None）。
    content_sha256: str | None = None


@dataclass(frozen=True)
class AttachmentReadResult:
    """添付を読むための解析の結果（`ensure_parsed`。#2652）。

    - ready: `parsed` を読める（内容が空なら `parsed.is_empty()`）。`reused` は保存の再利用。
    - parsing: ほかの試行が解析中（待つ時間を過ぎた）。
    - failed: 解析に失敗した・読み取りのモデルが無い（`error_code` / `message`）。
    - missing: 期限が切れた・削除された（読めない。別のファイルや版で補わない）。
    """

    attachment_id: str
    status: AttachmentReadStatus
    record: ChatAttachmentRecord | None = None
    parsed: ParsedAttachment | None = None
    reused: bool = False
    error_code: str | None = None
    message: str | None = None
