"""会話の添付の上限（共通の設定と、解析の安全のための定数。#2517）。

設定は `platform/.env`（3 製品で同じ値。属性は製品の Settings の同じ名前）:

- `PLATFORM_CHAT_ATTACHMENT_TTL_HOURS`（`chat_attachment_ttl_hours`）: 最後に使ってから
  消すまでの時間。
- `PLATFORM_CHAT_ATTACHMENT_MAX_BYTES`（`chat_attachment_max_bytes`）: 1 つのファイルの上限。
- `PLATFORM_CHAT_ATTACHMENT_MAX_FILES`（`chat_attachment_max_files`）: 1 つの会話（と送る前の
  1 回）の件数。

解析の上限（文字数・行数・列数・zip の展開の大きさ）は、保存と解析の安全のための定数で設定にしない
（モデルへ渡す量はツールが 1 回に返す量で決まる）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .errors import ChatAttachmentError

CHAT_ATTACHMENT_DEFAULT_TTL_HOURS: Final = 24
CHAT_ATTACHMENT_TTL_HOURS_MIN: Final = 1
CHAT_ATTACHMENT_TTL_HOURS_MAX: Final = 720
CHAT_ATTACHMENT_DEFAULT_MAX_BYTES: Final = 20 * 1024 * 1024
CHAT_ATTACHMENT_MAX_BYTES_MIN: Final = 1024 * 1024
# 各製品のアップロードの上限（Agent 100 MiB・RAG / NL2SQL 200 MiB）に収まる値。
CHAT_ATTACHMENT_MAX_BYTES_MAX: Final = 100 * 1024 * 1024
CHAT_ATTACHMENT_DEFAULT_MAX_FILES: Final = 10
CHAT_ATTACHMENT_MAX_FILES_MIN: Final = 1
CHAT_ATTACHMENT_MAX_FILES_MAX: Final = 50

# 解析の上限（定数）。
MAX_TEXT_CHARACTERS: Final = 2_000_000
MAX_TABLE_ROWS: Final = 50_000
MAX_TABLE_COLUMNS: Final = 200
MAX_CELL_CHARACTERS: Final = 4_000
MAX_PDF_PAGES: Final = 2_000
MAX_SLIDES: Final = 1_000
# Office（zip）の展開の合計と 1 つの部分の上限（zip bomb）。
MAX_ZIP_UNCOMPRESSED_BYTES: Final = 300 * 1024 * 1024
MAX_ZIP_MEMBER_BYTES: Final = 100 * 1024 * 1024
MAX_ZIP_ENTRIES: Final = 20_000
# 見出しの無いテキストを区切る大きさ（文字）。
TEXT_SECTION_CHARACTERS: Final = 4_000
# 期限の切れた行を 1 回の操作で消す件数。
PURGE_BATCH_SIZE: Final = 100


@dataclass(frozen=True)
class ChatAttachmentLimits:
    ttl_hours: int
    max_bytes: int
    max_files: int


def _clamped(settings: object, name: str, default: int, low: int, high: int) -> int:
    raw = getattr(settings, name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return min(max(value, low), high)


def chat_attachment_limits(settings: object) -> ChatAttachmentLimits:
    """製品の Settings の共通の設定（無い属性は既定、範囲の外は寄せる）。"""
    return ChatAttachmentLimits(
        ttl_hours=_clamped(
            settings,
            "chat_attachment_ttl_hours",
            CHAT_ATTACHMENT_DEFAULT_TTL_HOURS,
            CHAT_ATTACHMENT_TTL_HOURS_MIN,
            CHAT_ATTACHMENT_TTL_HOURS_MAX,
        ),
        max_bytes=_clamped(
            settings,
            "chat_attachment_max_bytes",
            CHAT_ATTACHMENT_DEFAULT_MAX_BYTES,
            CHAT_ATTACHMENT_MAX_BYTES_MIN,
            CHAT_ATTACHMENT_MAX_BYTES_MAX,
        ),
        max_files=_clamped(
            settings,
            "chat_attachment_max_files",
            CHAT_ATTACHMENT_DEFAULT_MAX_FILES,
            CHAT_ATTACHMENT_MAX_FILES_MIN,
            CHAT_ATTACHMENT_MAX_FILES_MAX,
        ),
    )


def file_too_large(max_bytes: int) -> ChatAttachmentError:
    return ChatAttachmentError(
        413,
        f"添付できるファイルは 1 つ {format_bytes(max_bytes)} までです。",
        code="CHAT_ATTACHMENT_TOO_LARGE",
    )


def format_bytes(value: int) -> str:
    """画面と同じ「20 MB」の形（1024 の倍数）。"""
    if value >= 1024 * 1024:
        size = value / (1024 * 1024)
        return f"{size:.0f} MB" if size >= 10 or size.is_integer() else f"{size:.1f} MB"
    if value >= 1024:
        return f"{value / 1024:.0f} KB"
    return f"{value} B"
