"""回答の注意書き（製品ごと。#2405・#2449）。

AI の回答の末尾に毎回同じ位置で出す固定の文（誤りの可能性・確かめ方・連絡先）。文と出す / 出さないは
製品ごとに持ち、各製品の運用設定の「回答の注意書き」で管理者が変えられる。

- 保存先は共通の表 `PLATFORM_ANSWER_NOTICE`（製品ごとに 1 行。キーは `answer_notice_setting_key`。
  DDL は `auth.migrations` の共通の DDL）。複数行の文で、保存の後に再起動しないで効かせるため
  `.env` には置かない。
- 文は「本文」と「窓口」（連絡先の名前・連絡の方法）の 2 つで、回答に付ける文は本文の後に
  窓口の案内の 1 行（`ANSWER_NOTICE_CONTACT_TEMPLATE`）を続ける。窓口を分けて持つのは、RAG の
  検索・回答プロファイルが窓口だけを上書きできるようにするため（#2400）。
- 未設定（行が無い・本文 / 窓口が NULL）のときは既定（有効・`DEFAULT_ANSWER_NOTICE_BODY` /
  `DEFAULT_ANSWER_NOTICE_CONTACT`）を使う。既定は DB に書き込まない（後から直せるようにする）。
- 回答に付ける文は `AnswerNoticeProvider.current_text()` で読む。短い cache（`CACHE_SECONDS`）を
  持ち、同じプロセスで保存したら次の回答から効く（同じ製品のほかのプロセスは cache の期限の後）。
  読めないとき（表が未作成・DB の停止中）は既定の文を返し、回答を失敗させない。
- 文はモデルに書かせない。製品の backend が回答の確定の後に、回答とは別の項目（`notice`）として
  付け、モデルへの指示・会話の履歴・回答の検証の対象には入れない。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from fastapi import APIRouter, params
from pr_backend_core.schemas import ApiResponse
from pydantic import BaseModel, Field, field_validator

from .auth.errors import SecurityMigrationRequired, missing_security_migration_object

logger = logging.getLogger(__name__)

ANSWER_NOTICE_TABLE = "PLATFORM_ANSWER_NOTICE"
# 表の行のキーの接頭辞。行は製品ごと（`answer_notice:rag` など）。
ANSWER_NOTICE_SETTING_KEY_PREFIX = "answer_notice"
ANSWER_NOTICE_MAX_CHARS = 500
ANSWER_NOTICE_CONTACT_MAX_CHARS = 200
# 既定の文。短く（3 行まで）、AI が生成したこと・誤りの可能性・確かめ方・連絡先を示す。
DEFAULT_ANSWER_NOTICE_BODY = (
    "この回答は AI が生成したもので、誤りや不足を含む場合があります。\n"
    "業務に使う前に、必ず回答の根拠（資料・SQL・結果）で内容を確かめてください。"
)
# 窓口（連絡先の名前・連絡の方法）。管理者が電話・URL などを書く。
DEFAULT_ANSWER_NOTICE_CONTACT = "サポート窓口"
# 本文の後に続ける窓口の案内の 1 行（`{contact}` に窓口が入る）。
ANSWER_NOTICE_CONTACT_TEMPLATE = "解決しない場合は、{contact}へお問い合わせください。"


def answer_notice_setting_key(product_key: str) -> str:
    """製品の注意書きの行のキー（`AuthService.product_key` から決める）。"""
    return f"{ANSWER_NOTICE_SETTING_KEY_PREFIX}:{product_key}"


def answer_notice_contact_line(contact: str, template: str = ANSWER_NOTICE_CONTACT_TEMPLATE) -> str:
    """窓口の案内の 1 行。"""
    return template.replace("{contact}", contact)


def compose_answer_notice(body: str, contact_line: str | None) -> str:
    """本文の後に窓口の案内の 1 行を続けた、回答に付ける文（行が無ければ本文だけ）。"""
    return "\n".join(part for part in (body, contact_line) if part)


DEFAULT_ANSWER_NOTICE_TEXT = compose_answer_notice(
    DEFAULT_ANSWER_NOTICE_BODY, answer_notice_contact_line(DEFAULT_ANSWER_NOTICE_CONTACT)
)
# 回答に付ける文の cache の秒数（3 製品・複数の worker の間で、保存がこの秒数のうちに行き渡る）。
CACHE_SECONDS = 10.0
# 改行・タブ以外の制御文字は保存しない（画面・コピー・書き出しで崩れる）。
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _now() -> datetime:
    return datetime.now(UTC)


def normalize_notice_text(value: str) -> str:
    """改行を `\\n` にそろえ、行末の空白と前後の空行を除く。"""
    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


def normalize_notice_contact(value: str) -> str:
    """窓口は 1 行にする（改行・連続の空白を 1 つの空白にそろえる）。"""
    return " ".join(value.split())


@dataclass(frozen=True)
class AnswerNoticeParts:
    """回答に付ける文の部品（本文と窓口）。製品は窓口を上書きして組み立てられる（#2400）。"""

    body: str
    contact: str

    def text(self, contact_line: str | None = None) -> str:
        """回答に付ける文。`contact_line` を省くと、この窓口の既定の案内の 1 行を続ける。"""
        return compose_answer_notice(
            self.body,
            answer_notice_contact_line(self.contact) if contact_line is None else contact_line,
        )


DEFAULT_ANSWER_NOTICE_PARTS = AnswerNoticeParts(
    body=DEFAULT_ANSWER_NOTICE_BODY, contact=DEFAULT_ANSWER_NOTICE_CONTACT
)


@dataclass(frozen=True)
class AnswerNoticeRecord:
    """保存した設定。`text`（本文）・`contact`（窓口）が None なら既定を使う。"""

    enabled: bool = True
    text: str | None = None
    contact: str | None = None
    updated_at: datetime | None = None

    @property
    def parts(self) -> AnswerNoticeParts:
        return AnswerNoticeParts(
            body=self.text or DEFAULT_ANSWER_NOTICE_BODY,
            contact=self.contact or DEFAULT_ANSWER_NOTICE_CONTACT,
        )

    @property
    def effective_text(self) -> str:
        return self.parts.text()


class AnswerNoticeStore(Protocol):
    def load(self) -> AnswerNoticeRecord | None: ...
    def save(self, record: AnswerNoticeRecord) -> AnswerNoticeRecord: ...


class InMemoryAnswerNoticeStore:
    """プロセスの中だけの保存（テスト・DB を使わない構成）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._record: AnswerNoticeRecord | None = None

    def load(self) -> AnswerNoticeRecord | None:
        with self._lock:
            return self._record

    def save(self, record: AnswerNoticeRecord) -> AnswerNoticeRecord:
        saved = AnswerNoticeRecord(
            enabled=record.enabled, text=record.text, contact=record.contact, updated_at=_now()
        )
        with self._lock:
            self._record = saved
        return saved


class OracleAnswerNoticeStore:
    """`PLATFORM_ANSWER_NOTICE` の 1 製品の行の保存。接続は共通認証の store と同じものを使う。"""

    def __init__(self, connection_factory: Any, setting_key: str) -> None:
        self._connection_factory = connection_factory
        self._setting_key = setting_key

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        try:
            with self._connection_factory() as connection:
                yield connection
        except SecurityMigrationRequired:
            raise
        except Exception as exc:
            # この store の SQL は PLATFORM_ANSWER_NOTICE だけを読むので、名前の無い ORA-00942 も
            # この表が無い（共通の DDL が未適用）とみなす。ORA-00904 は列が無い（#2400 で足した
            # CONTACT_TEXT の DDL が未適用）。
            message = str(exc).upper()
            if (
                missing_security_migration_object(exc, frozenset({ANSWER_NOTICE_TABLE})) is not None
                or "ORA-00942" in message
                or "ORA-00904" in message
            ):
                raise SecurityMigrationRequired(ANSWER_NOTICE_TABLE) from exc
            raise

    def load(self) -> AnswerNoticeRecord | None:
        with self._connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT ENABLED, NOTICE_TEXT, UPDATED_AT, CONTACT_TEXT FROM PLATFORM_ANSWER_NOTICE "
                "WHERE SETTING_KEY = :setting_key",
                {"setting_key": self._setting_key},
            )
            row = cursor.fetchone()
        if row is None:
            return None
        text = _read_text(row[1])
        updated_at = row[2]
        contact = _read_text(row[3]) if len(row) > 3 else None
        if isinstance(updated_at, datetime) and updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        return AnswerNoticeRecord(
            enabled=bool(int(row[0] or 0)),
            text=(normalize_notice_text(text) or None) if text is not None else None,
            contact=(normalize_notice_contact(contact) or None) if contact is not None else None,
            updated_at=updated_at if isinstance(updated_at, datetime) else None,
        )

    def save(self, record: AnswerNoticeRecord) -> AnswerNoticeRecord:
        now = _now()
        with self._connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                MERGE INTO PLATFORM_ANSWER_NOTICE target
                USING (SELECT :setting_key AS SETTING_KEY FROM DUAL) source
                   ON (target.SETTING_KEY = source.SETTING_KEY)
                 WHEN MATCHED THEN UPDATE SET
                      ENABLED = :enabled, NOTICE_TEXT = :notice_text,
                      CONTACT_TEXT = :contact_text, UPDATED_AT = :updated_at
                 WHEN NOT MATCHED THEN INSERT
                      (SETTING_KEY, ENABLED, NOTICE_TEXT, CONTACT_TEXT, UPDATED_AT)
                      VALUES (:setting_key, :enabled, :notice_text, :contact_text, :updated_at)
                """,
                {
                    "setting_key": self._setting_key,
                    "enabled": 1 if record.enabled else 0,
                    "notice_text": record.text,
                    "contact_text": record.contact,
                    "updated_at": now,
                },
            )
            conn.commit()
        return AnswerNoticeRecord(
            enabled=record.enabled, text=record.text, contact=record.contact, updated_at=now
        )


def _read_text(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "read"):  # CLOB などの LOB
        value = value.read()
    return str(value)


class AnswerNoticeProvider:
    """回答に付ける文を読む（短い cache 付き）。読めないときは既定の文を返す。"""

    def __init__(
        self,
        store: AnswerNoticeStore,
        *,
        cache_seconds: float = CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: tuple[float, AnswerNoticeParts | None] | None = None

    def load(self) -> AnswerNoticeRecord:
        """保存した設定（未設定なら既定）。表が無いときは `SecurityMigrationRequired` を送る。"""
        return self.store.load() or AnswerNoticeRecord()

    def save(self, record: AnswerNoticeRecord) -> AnswerNoticeRecord:
        saved = self.store.save(record)
        self.invalidate()
        return saved

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None

    def current_parts(self) -> AnswerNoticeParts | None:
        """回答に付ける文の部品（本文と窓口）。無効なら None。読めないときは既定。"""
        now = self._clock()
        with self._lock:
            cached = self._cached
            if cached is not None and now - cached[0] < self._cache_seconds:
                return cached[1]
        try:
            record = self.load()
            parts: AnswerNoticeParts | None = record.parts if record.enabled else None
        except Exception as exc:  # 表が未作成・DB の停止中でも回答は返す
            logger.warning(
                "answer_notice_load_failed",
                extra={"error_type": type(exc).__name__},
            )
            parts = DEFAULT_ANSWER_NOTICE_PARTS
        with self._lock:
            self._cached = (now, parts)
        return parts

    def current_text(self) -> str | None:
        """回答に付ける文（本文と既定の窓口の案内）。無効なら None。読めないときは既定の文。"""
        parts = self.current_parts()
        return parts.text() if parts is not None else None


# ---- API の入出力 -------------------------------------------------------------------


class AnswerNoticeSettingsData(BaseModel):
    """`GET /settings/answer-notice` の応答。"""

    enabled: bool
    # 管理者が書いた本文（未設定・既定に戻したときは null）。
    text: str | None
    default_text: str
    # 管理者が書いた窓口（未設定・既定に戻したときは null。#2400）。
    contact: str | None = None
    default_contact: str = DEFAULT_ANSWER_NOTICE_CONTACT
    # 本文の後に続ける窓口の案内の 1 行（`{contact}` に窓口が入る）。画面の表示の確認に使う。
    contact_template: str = ANSWER_NOTICE_CONTACT_TEMPLATE
    # 回答に付ける文（本文と窓口の案内）。無効のときも、有効にしたときの文を返す。
    effective_text: str
    max_chars: int
    contact_max_chars: int = ANSWER_NOTICE_CONTACT_MAX_CHARS
    updated_at: datetime | None = None

    @classmethod
    def from_record(cls, record: AnswerNoticeRecord) -> AnswerNoticeSettingsData:
        return cls(
            enabled=record.enabled,
            text=record.text,
            default_text=DEFAULT_ANSWER_NOTICE_BODY,
            contact=record.contact,
            effective_text=record.effective_text,
            max_chars=ANSWER_NOTICE_MAX_CHARS,
            updated_at=record.updated_at,
        )


class AnswerNoticeSettingsUpdate(BaseModel):
    """`PUT /settings/answer-notice` の入力。本文・窓口が null・空・既定なら既定に戻す。"""

    enabled: bool
    text: str | None = Field(default=None, max_length=ANSWER_NOTICE_MAX_CHARS * 2)
    contact: str | None = Field(default=None, max_length=ANSWER_NOTICE_CONTACT_MAX_CHARS * 2)

    @field_validator("contact")
    @classmethod
    def _contact(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_notice_contact(value)
        if _CONTROL_CHAR_RE.search(normalized):
            raise ValueError("制御文字は使えません。")
        if len(normalized) > ANSWER_NOTICE_CONTACT_MAX_CHARS:
            raise ValueError(f"窓口は {ANSWER_NOTICE_CONTACT_MAX_CHARS} 字以内で入力してください。")
        if not normalized or normalized == DEFAULT_ANSWER_NOTICE_CONTACT:
            return None
        return normalized

    @field_validator("text")
    @classmethod
    def _text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_notice_text(value)
        if _CONTROL_CHAR_RE.search(normalized):
            raise ValueError("改行とタブ以外の制御文字は使えません。")
        if len(normalized) > ANSWER_NOTICE_MAX_CHARS:
            raise ValueError(f"本文は {ANSWER_NOTICE_MAX_CHARS} 字以内で入力してください。")
        if not normalized or normalized == DEFAULT_ANSWER_NOTICE_BODY:
            return None
        return normalized


def build_answer_notice_router(
    *,
    load: Callable[[], AnswerNoticeRecord],
    save: Callable[[AnswerNoticeRecord], AnswerNoticeRecord],
    write_dependencies: Sequence[params.Depends] = (),
) -> APIRouter:
    """「回答の注意書き」の設定 API（`/answer-notice`。製品は `/settings` の下に置く）。

    `load` / `save` は製品の共通認証のサービス（`AuthService.load_answer_notice` /
    `save_answer_notice`）を渡す。表が未作成なら、サービスが 409（migration が必要）にする。
    権限は製品の API の manifest が判定し、`write_dependencies` は保存に足す製品の確認。
    """
    router = APIRouter()

    @router.get("/answer-notice", response_model=ApiResponse[AnswerNoticeSettingsData])
    def get_answer_notice() -> ApiResponse[AnswerNoticeSettingsData]:
        """回答の注意書きの設定（未設定なら既定の文）。"""
        return ApiResponse(data=AnswerNoticeSettingsData.from_record(load()))

    @router.put(
        "/answer-notice",
        response_model=ApiResponse[AnswerNoticeSettingsData],
        dependencies=list(write_dependencies),
    )
    def update_answer_notice(
        payload: AnswerNoticeSettingsUpdate,
    ) -> ApiResponse[AnswerNoticeSettingsData]:
        """回答の注意書きを保存する（本文・窓口が null なら既定に戻す）。次の回答から効く。"""
        saved = save(
            AnswerNoticeRecord(enabled=payload.enabled, text=payload.text, contact=payload.contact)
        )
        return ApiResponse(data=AnswerNoticeSettingsData.from_record(saved))

    return router
