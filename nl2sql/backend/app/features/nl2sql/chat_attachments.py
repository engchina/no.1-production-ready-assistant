"""チャットの添付（会話の期間だけ使う作業ファイル。#2517）。

保存・解析・見え方・期限は platform の `pr_system_settings.chat_attachments`。NL2SQL が持つのは:

- 画像の読み取り（既定の Vision モデル。`OciEnterpriseAiClient.generate_from_image`）。
- 添付の表: 会話の添付の Excel・CSV のシートを、SQL の WITH 句の行の表 `ATTACHMENT_<n>` に
  する（`JSON_TABLE` に行の JSON を CLOB の bind で渡す）。SQL の生成（Enterprise AI Direct）には
  表の名前・列・型・先頭の数行だけを渡し、業務の表と JOIN して照合できるようにする。実行のときに
  WITH 句を前に付ける。動的な `CREATE TABLE` はしない（共有の schema に利用者ごとの表を
  作らない・DeepSec の DATA USER に権限を足さない・#904 の接続の分け方を変えない）。
- 添付の文書（PDF・Word など）: 質問に関係する節を、SQL の生成の参考として渡す。
- 解析: 会話に送信した添付を、SQL の生成の材料にするときに共通のサービスで解析する（アップロード
  では解析しない。結果は保存して再利用する。#2652）。読めない添付は材料に入れずに続ける。

1 回のチャットの往復（ジョブ）の間は、会話の添付の表を context variable に置き
（`use_attachment_context`）、SQL の検証（`validation_sql`）と実行
（`executable_with_attachments`）が読む。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import unicodedata
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final

from pr_backend_core.observability.model_purpose import async_call_with_model_purpose
from pr_system_settings.chat_attachments import (
    IMAGE_READING_PROMPT,
    ChatAttachmentService,
    ChatAttachmentToolError,
    ChatAttachmentToolset,
)
from pr_system_settings.chat_attachments.models import TABULAR_KINDS, CellValue

from app.settings import enterprise_ai_vision_model_id, get_settings

logger = logging.getLogger(__name__)

ATTACHMENT_TABLE_PREFIX: Final = "ATTACHMENT_"
# SQL の生成に渡す先頭の行の数と、参考に渡す文書の文字数。
SAMPLE_ROWS: Final = 3
DOCUMENT_CONTEXT_CHARACTERS: Final = 4_000
# 列の名前（Oracle の引用付きの識別子。128 bytes まで）。
_MAX_COLUMN_NAME_BYTES: Final = 120
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_SERVICE: ChatAttachmentService | None = None
_SERVICE_LOCK = threading.Lock()


def _image_model_id() -> str | None:
    """画像を読み取る既定の Vision モデル（モデル・接続が無ければ None）。"""
    from pr_system_settings.model import enterprise_ai_connection_for_model

    settings = get_settings()
    model_id = enterprise_ai_vision_model_id(settings)
    if not model_id or not enterprise_ai_connection_for_model(settings, model_id).is_configured():
        return None
    return str(model_id)


def _image_reader() -> Callable[[bytes, str], Awaitable[str]] | None:
    """既定の Vision モデルで画像を文にする関数（Vision モデル・接続が無ければ None）。"""
    if not _image_model_id():
        return None

    async def read(data: bytes, media_type: str) -> str:
        from app.clients.oci_enterprise_ai import OciEnterpriseAiClient

        return await async_call_with_model_purpose(
            "document.read",
            OciEnterpriseAiClient(get_settings()).generate_from_image,
            data,
            IMAGE_READING_PROMPT,
            mime_type=media_type,
        )

    return read


def chat_attachment_service() -> ChatAttachmentService:
    """process で 1 つの添付のサービス（保存先は共通認証の store と同じ DB）。"""
    global _SERVICE
    if _SERVICE is None:
        with _SERVICE_LOCK:
            if _SERVICE is None:
                from app.security.service import get_security_service

                security = get_security_service()
                _SERVICE = ChatAttachmentService(
                    security.chat_attachment_store,
                    product="nl2sql",
                    settings=get_settings,
                    image_reader=_image_reader,
                    image_model_id=_image_model_id,
                    # 運用設定 › チャットの添付（製品ごと。既定は無効）。
                    enabled=security.chat_attachment_settings.enabled,
                    settings_failure=security.chat_attachment_settings.failure,
                )
    return _SERVICE


def set_chat_attachment_service(service: ChatAttachmentService | None) -> None:
    """テスト用: service を差し替える（None で既定に戻す）。"""
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = service


@dataclass(frozen=True)
class AttachmentColumn:
    name: str  # 引用付きの識別子の中身（`"..."` の中）
    sql_type: str  # NUMBER / DATE / VARCHAR2(4000)


@dataclass(frozen=True)
class AttachmentTable:
    """SQL の WITH 句で用意する添付の表（1 つのシート）。"""

    name: str
    attachment_id: str
    file_name: str
    sheet: str
    columns: tuple[AttachmentColumn, ...]
    rows: tuple[tuple[CellValue, ...], ...]

    @property
    def bind_name(self) -> str:
        return self.name.lower()

    def rows_json(self) -> str:
        return json.dumps([list(row) for row in self.rows], ensure_ascii=False)


@dataclass(frozen=True)
class AttachmentContext:
    """1 回のチャットの往復で使う会話の添付（表と、文書の参考の文）。"""

    tables: tuple[AttachmentTable, ...] = ()
    documents: tuple[tuple[str, str], ...] = field(default=())  # (ファイル名, 文)

    def __bool__(self) -> bool:
        return bool(self.tables or self.documents)


_ACTIVE: ContextVar[AttachmentContext | None] = ContextVar(
    "nl2sql_chat_attachment_context", default=None
)


@contextmanager
def use_attachment_context(context: AttachmentContext) -> Iterator[None]:
    """1 回のチャットの往復の間だけ、会話の添付の表を SQL の検証・実行に見せる。"""
    token = _ACTIVE.set(context)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def active_attachment_context() -> AttachmentContext:
    return _ACTIVE.get() or AttachmentContext()


def _column_name(label: str, used: set[str]) -> str:
    """列の名前を引用付きの識別子に使える形にする（`"` を除き、長さを切り、重複に番号）。"""
    base = unicodedata.normalize("NFKC", label).replace('"', "").strip() or "COL"
    while len(base.encode("utf-8")) > _MAX_COLUMN_NAME_BYTES:
        base = base[:-1]
    name = base
    index = 2
    while name.upper() in used:
        suffix = f"_{index}"
        name = base
        while len((name + suffix).encode("utf-8")) > _MAX_COLUMN_NAME_BYTES:
            name = name[:-1]
        name = f"{name}{suffix}"
        index += 1
    used.add(name.upper())
    return name


_NUMBER_RE = re.compile(r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")


def _number(value: CellValue) -> int | float | None:
    """数値か数値の文字（CSV の値・桁区切り）を数にする（数でなければ None）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    if isinstance(value, str) and _NUMBER_RE.match(value.strip()):
        text = value.strip().replace(",", "")
        # 先頭のゼロは識別子の一部として残す。整数を float にすると大きな値が丸められる。
        integer_part = text.lstrip("+-").split(".", 1)[0]
        if len(integer_part) > 1 and integer_part.startswith("0"):
            return None
        return float(text) if "." in text else int(text)
    return None


def _sql_type(values: Sequence[CellValue]) -> str:
    present = [value for value in values if value is not None and value != ""]
    if present and all(_number(value) is not None for value in present):
        return "NUMBER"
    if present and all(isinstance(value, str) and _ISO_DATE_RE.match(value) for value in present):
        try:
            for value in present:
                date.fromisoformat(str(value))
        except ValueError:
            return "VARCHAR2(4000)"
        return "DATE"
    return "VARCHAR2(4000)"


def _typed(value: CellValue, sql_type: str) -> CellValue:
    """JSON に入れる値（NUMBER の列は数、それ以外は文字。空は null）。"""
    if value is None or value == "":
        return None
    if sql_type == "NUMBER":
        return _number(value)
    return str(value) if not isinstance(value, str) else value


def _relevant_text(question: str, text: str, limit: int) -> str:
    """文書の節から質問に関係する部分（文字の一致の多い段落から）を `limit` 文字まで選ぶ。"""
    wanted = {question[index : index + 2] for index in range(max(len(question) - 1, 0))}
    paragraphs = [part for part in re.split(r"\n{2,}", text) if part.strip()]
    scored = sorted(
        paragraphs,
        key=lambda part: -sum(1 for gram in wanted if gram in part),
    )
    picked: list[str] = []
    total = 0
    for part in scored:
        if total + len(part) > limit:
            continue
        picked.append(part)
        total += len(part)
    return "\n\n".join(picked)


def attachment_context(
    conversation_id: str | None,
    question: str = "",
    *,
    prepared_toolset: ChatAttachmentToolset | None = None,
) -> AttachmentContext:
    """会話の添付の表と文書の参考（添付が無い・読めないときは空。SQL の生成は止めない）。"""
    if not conversation_id and prepared_toolset is None:
        return AttachmentContext()
    try:
        toolset = (
            prepared_toolset
            if prepared_toolset is not None
            else chat_attachment_service().toolset(conversation_id or "")
        )
        if not toolset:
            return AttachmentContext()
        tables: list[AttachmentTable] = []
        documents: list[tuple[str, str]] = []
        for record in toolset.records:
            try:
                parsed = toolset.parsed(record.attachment_id)
            except ChatAttachmentToolError as exc:
                logger.warning(
                    "nl2sql_chat_attachment_unreadable",
                    extra={"attachment_id": record.attachment_id, "reason": str(exc)},
                )
                continue
            if record.kind in TABULAR_KINDS:
                for sheet in parsed.sheets:
                    used: set[str] = set()
                    columns = tuple(
                        AttachmentColumn(
                            name=_column_name(label, used),
                            sql_type=_sql_type([row[index] for row in sheet.rows]),
                        )
                        for index, label in enumerate(sheet.columns)
                    )
                    tables.append(
                        AttachmentTable(
                            name=f"{ATTACHMENT_TABLE_PREFIX}{len(tables) + 1}",
                            attachment_id=record.attachment_id,
                            file_name=record.file_name,
                            sheet=sheet.sheet,
                            columns=columns,
                            rows=tuple(
                                tuple(
                                    _typed(value, column.sql_type)
                                    for value, column in zip(row, columns, strict=False)
                                )
                                for row in sheet.rows
                            ),
                        )
                    )
            elif parsed.sections:
                text = "\n\n".join(section.text for section in parsed.sections)
                documents.append(
                    (record.file_name, _relevant_text(question, text, DOCUMENT_CONTEXT_CHARACTERS))
                )
        return AttachmentContext(tables=tuple(tables), documents=tuple(documents))
    except Exception as exc:  # noqa: BLE001 - 添付を読めなくても SQL の生成は続ける
        logger.warning(
            "nl2sql_chat_attachments_unavailable",
            extra={"conversation_id": conversation_id, "error_type": type(exc).__name__},
        )
        return AttachmentContext()


def prompt_block(context: AttachmentContext) -> str:
    """SQL の生成（Enterprise AI Direct）に渡す添付の表と文書の参考。"""
    lines: list[str] = []
    if context.tables:
        lines.append(
            "attachment_tables: 利用者がこの会話に添付した表。WITH 句で用意するので、"
            "OWNER を付けずに表の名前のまま FROM / JOIN に書き、列は表の名前か別名で修飾して"
            "引用符付きで書く。業務の表との照合に使ってよい。"
        )
        for table in context.tables:
            lines.append(
                f"- table {table.name}（{table.file_name} のシート「{table.sheet}」、"
                f"{len(table.rows)} 行）"
            )
            for column in table.columns:
                lines.append(f'  - column "{column.name}" type={column.sql_type}')
            for row in table.rows[:SAMPLE_ROWS]:
                values = ", ".join(
                    f'"{column.name}"={value!r}'
                    for column, value in zip(table.columns, row, strict=False)
                )
                lines.append(f"  - sample: {values}")
    for file_name, text in context.documents:
        if text.strip():
            lines.append(
                f"attachment_document: 利用者が添付した文書「{file_name}」の抜粋"
                "（条件の参考。表ではない）"
            )
            lines.append(text)
    return "\n".join(lines)


def referenced_tables(sql: str, context: AttachmentContext) -> list[AttachmentTable]:
    """SQL が参照する添付の表（名前の語の一致）。"""
    upper = sql.upper()
    return [
        table
        for table in context.tables
        if re.search(rf"(?<![A-Z0-9_$#\"]){table.name}(?![A-Z0-9_$#])", upper)
    ]


def _cte(table: AttachmentTable, *, stub: bool) -> str:
    names = ", ".join(f'"{column.name}"' for column in table.columns)
    if stub:
        # 検証用: 列の名前と形だけを持つ空の行（bind・JSON_TABLE を検証に出さない）。
        values = ", ".join(
            "CAST(NULL AS NUMBER)"
            if column.sql_type == "NUMBER"
            else "CAST(NULL AS DATE)"
            if column.sql_type == "DATE"
            else "CAST(NULL AS VARCHAR2(4000))"
            for column in table.columns
        )
        # 表の名前は固定の ATTACHMENT_<n>、列の名前は `"` を除いた引用付きの識別子（_column_name）。
        return f"{table.name} ({names}) AS (SELECT {values} FROM DUAL)"  # nosec B608
    columns = ", ".join(
        f"\"{column.name}\" {column.sql_type} PATH '$[{index}]'"
        for index, column in enumerate(table.columns)
    )
    return (  # 行の値は bind で渡す。
        f"{table.name} ({names}) AS (SELECT * FROM JSON_TABLE(:{table.bind_name}, '$[*]' "  # nosec B608
        f"COLUMNS ({columns})))"
    )


_LEADING_WITH_RE = re.compile(r"^\s*WITH\s+", re.IGNORECASE)


def _prepend(sql: str, ctes: Sequence[str]) -> str:
    """CTE を前に付ける（SQL が WITH で始まれば、その WITH の最初に足す）。"""
    joined = ",\n".join(ctes)
    match = _LEADING_WITH_RE.match(sql)
    if match:
        return f"WITH {joined},\n{sql[match.end() :]}"
    return f"WITH {joined}\n{sql.lstrip()}"


def validation_sql(sql: str, context: AttachmentContext | None = None) -> str:
    """SQL の検証に使う形（参照した添付の表を、列の形だけの CTE で前に付ける）。"""
    tables = referenced_tables(sql, context or active_attachment_context())
    return _prepend(sql, [_cte(table, stub=True) for table in tables]) if tables else sql


def executable_with_attachments(
    sql: str, context: AttachmentContext | None = None
) -> tuple[str, dict[str, str]]:
    """実行する SQL（参照した添付の表の WITH 句を前に付ける）と、行の JSON の bind。"""
    tables = referenced_tables(sql, context or active_attachment_context())
    if not tables:
        return sql, {}
    return (
        _prepend(sql, [_cte(table, stub=False) for table in tables]),
        {table.bind_name: table.rows_json() for table in tables},
    )


def attachment_owner_principal(user_uuid: str, is_system_admin: bool) -> Any:
    """添付のサービスに渡す利用者（actor）。"""

    @dataclass(frozen=True)
    class _Principal:
        user_uuid: str
        is_system_admin: bool

    return _Principal(user_uuid=user_uuid, is_system_admin=is_system_admin)
