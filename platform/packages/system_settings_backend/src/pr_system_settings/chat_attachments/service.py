"""会話の添付のサービス（受け付け・見え方・会話への結び付け・期限・解析。#2517・#2652）。

- 持ち主: 会話を始める前の添付は添付した人、会話に結び付けたら会話の持ち主（system_admin が
  ほかの利用者の会話で添付したら持ち主はその利用者。`UPLOADED_BY_USER_UUID` は system_admin）。
- 見え方: 「AI 活用」の規則（`auth.visibility`）。他人の添付は 404。製品は自分の製品の行だけを扱う。
- 期限: 添付・送信に使った時刻 + `chat_attachment_ttl_hours`。期限の切れた行は、添付の操作のたびに
  `PURGE_BATCH_SIZE` 件ずつ消す（スケジューラーを持たない）。
- 会話の権限（会話が見えるか・持ち主）は製品が判定し、`conversation_owner` で渡す。
- 受け付け: 原本・種類・大きさ・hash・持ち主・期限を保存して ID を返す。入力の検査
  （`parsing.inspect_attachment`）はするが、本文の解析・OCR・Vision は呼ばない（#2652）。
- 送信: 送信の添付を認可して会話に結び付ける（`prepare_attachments` → `bind_references`）。
  結び付けるのは送信だけで、会話に結び付いた添付だけを読む（送る前の添付はモデルに見せない）。
- 解析: 送信した後に読むとき（`ensure_parsed`）に行い、状態（未解析・解析中・完了・失敗）を
  `OUTLINE_JSON` の `parse_state` に残す。結果は原本の hash・parser の版・解析の設定のキーで
  再利用し、同じ添付の同時のアクセス・再試行で二重に解析しない（process の中は lock、process
  の間は `parse_state.revision` を比べた書き換えと期限つきの lease）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import posixpath
import threading
import time
import uuid
import weakref
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from anyio import to_thread
from pr_backend_core.observability.tracing import observed_span

from ..auth.visibility import can_access_user_data
from .errors import ChatAttachmentError, attachment_not_found
from .limits import PURGE_BATCH_SIZE, ChatAttachmentLimits, chat_attachment_limits, file_too_large
from .models import (
    EXCEL_PARSER_VERSION,
    AttachmentOutline,
    AttachmentParseState,
    AttachmentReadResult,
    ChatAttachmentData,
    ChatAttachmentLimitsData,
    ChatAttachmentListData,
    ChatAttachmentProduct,
    ChatAttachmentRecord,
    ChatAttachmentReference,
    ParsedAttachment,
)
from .parsing import (
    ImageReader,
    accepted_extensions,
    document_parse_key,
    image_model_missing,
    image_parse_key,
    inspect_attachment,
    parse_document,
    read_image,
)
from .store import ChatAttachmentStore
from .tools import ChatAttachmentToolset

logger = logging.getLogger(__name__)

# 新しい版で解析し直せなかった（前の版の結果のまま使う。#2544・#2652）。
WARNING_EXCEL_REPARSE_FAILED = "excel_reparse_failed"

# 解析の試行の期限（過ぎたら終わった process の試行とみなして引き継ぐ）。
PARSE_LEASE_SECONDS = 300
# 一時的な失敗（モデルの応答・接続など）の再試行の回数と間隔（回数 × 間隔の後に試す）。
MAX_PARSE_ATTEMPTS = 3
PARSE_RETRY_SECONDS = 30
# ほかの試行の解析を待つ時間と、状態を読み直す間隔。
PARSE_WAIT_SECONDS = 30.0
PARSE_POLL_SECONDS = 0.5
# 一時的な失敗の code（入力のせいの失敗は `ChatAttachmentError` の code）。
PARSE_FAILED_CODE = "CHAT_ATTACHMENT_PARSE_FAILED"

MAX_FILE_NAME_CHARACTERS = 400


_MISSING_MESSAGE = "添付ファイルの期限が切れたか、削除されています。"
_PARSING_MESSAGE = "添付ファイルを解析しています。少し待ってから、もう一度読んでください。"
_RETRY_MESSAGE = "添付ファイルを解析できませんでした。しばらくしてからもう一度お試しください。"


class AttachmentPrincipal(Protocol):
    @property
    def user_uuid(self) -> str: ...

    @property
    def is_system_admin(self) -> bool: ...


def _now() -> datetime:
    return datetime.now(UTC)


def _run_coroutine[T](factory: Callable[[], Awaitable[T]]) -> T:
    """同期の処理から coroutine を実行する（event loop の thread なら別の thread で）。"""

    async def run() -> T:
        return await factory()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run())
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, run()).result()


def _safe_file_name(file_name: str) -> str:
    """path を除いた名前（制御文字を除き、長さを切る）。"""
    base = posixpath.basename(file_name.replace("\\", "/")).strip()
    base = "".join(ch for ch in base if ch.isprintable())
    if not base:
        raise ChatAttachmentError(
            422, "ファイル名がありません。", code="CHAT_ATTACHMENT_NAME_INVALID"
        )
    if len(base) > MAX_FILE_NAME_CHARACTERS:
        stem, extension = posixpath.splitext(base)
        base = stem[: MAX_FILE_NAME_CHARACTERS - len(extension)] + extension
    return base


def _too_many(max_files: int) -> ChatAttachmentError:
    return ChatAttachmentError(
        409,
        f"1 つの会話に添付できるファイルは {max_files} 件までです。不要な添付を外してください。",
        code="CHAT_ATTACHMENT_TOO_MANY",
    )


class ChatAttachmentService:
    def __init__(
        self,
        store: ChatAttachmentStore,
        *,
        product: ChatAttachmentProduct,
        settings: Callable[[], object],
        image_reader: Callable[[], ImageReader | None] = lambda: None,
        image_model_id: Callable[[], str | None] = lambda: None,
        enabled: Callable[[], bool] = lambda: True,
        settings_failure: Callable[[], ChatAttachmentError | None] = lambda: None,
        clock: Callable[[], datetime] = _now,
        parse_wait_seconds: float = PARSE_WAIT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.store = store
        # 製品の「運用設定 › チャットの添付」で有効か（`settings.ChatAttachmentSettingsProvider`）。
        self.enabled = enabled
        # 設定を読めなかった理由（`ChatAttachmentSettingsProvider.failure`。#2555）。
        self._settings_failure = settings_failure
        self.product: ChatAttachmentProduct = product
        self._settings = settings
        # 画像の汎用の読み取り（既定の Vision モデル）と、そのモデルの ID（結果の再利用のキー）。
        self._image_reader = image_reader
        self._image_model_id = image_model_id
        self._clock = clock
        self._parse_wait_seconds = parse_wait_seconds
        self._sleep = sleep
        # 同じ process の中で同じ添付を同時に解析しない（process の間は `parse_state` の lease）。
        self._parse_locks: weakref.WeakValueDictionary[str, threading.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._parse_locks_guard = threading.Lock()

    # ---- 設定 ----

    def limits(self) -> ChatAttachmentLimits:
        return chat_attachment_limits(self._settings())

    def limits_data(self) -> ChatAttachmentLimitsData:
        limits = self.limits()
        return ChatAttachmentLimitsData(
            max_bytes=limits.max_bytes,
            max_files=limits.max_files,
            ttl_hours=limits.ttl_hours,
            accept=accepted_extensions(),
            image_enabled=self._image_reading() is not None,
            enabled=self.enabled(),
        )

    def _image_reading(self) -> tuple[ImageReader, str] | None:
        """画像の読み取りの関数とモデルの ID（どちらかが無ければ None）。"""
        model_id = self._image_model_id()
        reader = self._image_reader() if model_id else None
        return (reader, model_id) if reader is not None and model_id else None

    def _require_enabled(self) -> None:
        if not self.enabled():
            # 設定を読めないときは、管理者が無効にしたとは案内しない（#2555）。
            failure = self._settings_failure()
            if failure is not None:
                raise failure
            raise ChatAttachmentError(
                403,
                "この製品ではチャットの添付を使えません。管理者が「運用設定 › チャットの添付」で"
                "有効にすると使えます。",
                code="CHAT_ATTACHMENT_DISABLED",
            )

    def _expires_at(self, now: datetime) -> datetime:
        return now + timedelta(hours=self.limits().ttl_hours)

    def purge_expired(self) -> int:
        """期限の切れた行を上限の件数まで消す（失敗しても操作は止めない）。"""
        try:
            return self.store.purge_expired(now=self._clock(), limit=PURGE_BATCH_SIZE)
        except ChatAttachmentError:
            raise
        except Exception:  # 消せなくても次の操作で消す
            logger.warning("chat_attachment_purge_failed", exc_info=True)
            return 0

    # ---- 見え方 ----

    def _visible(self, principal: AttachmentPrincipal, attachment_id: str) -> ChatAttachmentRecord:
        record = self.store.get(attachment_id)
        if (
            record is None
            or record.product != self.product
            or record.expires_at <= self._clock()
            or not can_access_user_data(principal, record.owner_user_uuid)
        ):
            raise attachment_not_found()
        return record

    # ---- 受け付け ----

    async def upload(
        self,
        principal: AttachmentPrincipal,
        *,
        file_name: str,
        data: bytes,
        conversation_id: str | None = None,
        conversation_owner_user_uuid: str | None = None,
    ) -> ChatAttachmentData:
        """ファイルを検査して原本を保存する（解析しない。#2652）。

        `conversation_id` は添付する会話（製品が会話の権限を確かめ、持ち主を
        `conversation_owner_user_uuid` で渡す）。会話への結び付けは送信のとき
        （`prepare_attachments` → `bind_references`）だけで、送る前の添付はモデルに見せない。
        """
        started = time.monotonic()
        self._require_enabled()
        limits = self.limits()
        if len(data) > limits.max_bytes:
            raise file_too_large(limits.max_bytes)
        name = _safe_file_name(file_name)
        await to_thread.run_sync(self.purge_expired)
        now = self._clock()
        existing = await to_thread.run_sync(
            lambda: self.store.list_pending(self.product, principal.user_uuid, now=now)
        )
        if len(existing) >= limits.max_files:
            raise _too_many(limits.max_files)
        spec, warnings = await to_thread.run_sync(lambda: inspect_attachment(name, data))
        owner = (
            conversation_owner_user_uuid
            if conversation_id and conversation_owner_user_uuid
            else principal.user_uuid
        )
        record = ChatAttachmentRecord(
            attachment_id=str(uuid.uuid4()),
            product=self.product,
            owner_user_uuid=owner,
            uploaded_by_user_uuid=principal.user_uuid,
            conversation_id=None,
            file_name=name,
            media_type=spec.media_type,
            kind=spec.kind,
            size_bytes=len(data),
            content_sha256=hashlib.sha256(data).hexdigest(),
            outline=AttachmentOutline(
                warnings=warnings, parse_state=AttachmentParseState(updated_at=now)
            ),
            created_at=now,
            expires_at=self._expires_at(now),
        )
        await to_thread.run_sync(lambda: self.store.insert(record, data, ParsedAttachment()))
        logger.info(
            "chat_attachment_uploaded",
            extra={
                "attachment_id": record.attachment_id,
                "product": self.product,
                "kind": record.kind,
                "size_bytes": record.size_bytes,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "model_calls": 0,
            },
        )
        return ChatAttachmentData.from_record(record, viewer_user_uuid=principal.user_uuid)

    def _existing(
        self, principal: AttachmentPrincipal, conversation_id: str | None, *, now: datetime
    ) -> list[ChatAttachmentRecord]:
        if conversation_id:
            return self.store.list_for_conversation(self.product, conversation_id, now=now)
        return self.store.list_pending(self.product, principal.user_uuid, now=now)

    # ---- 一覧・削除 ----

    def list_items(
        self, principal: AttachmentPrincipal, *, conversation_id: str | None = None
    ) -> ChatAttachmentListData:
        """会話の添付（`conversation_id` が無ければ、自分が添付して送る前のもの）。

        会話の権限は製品が先に確かめる。
        """
        self.purge_expired()
        records = self._existing(principal, conversation_id, now=self._clock())
        return ChatAttachmentListData(
            items=[
                ChatAttachmentData.from_record(record, viewer_user_uuid=principal.user_uuid)
                for record in records
            ],
            limits=self.limits_data(),
        )

    def delete(self, principal: AttachmentPrincipal, attachment_id: str) -> None:
        self._visible(principal, attachment_id)
        self.store.delete(attachment_id)

    def delete_conversation(self, conversation_id: str) -> int:
        """会話の削除に合わせて、その会話の添付を消す（会話の権限は製品が確かめる）。"""
        return self.store.delete_conversation(self.product, conversation_id)

    # ---- 送信 ----

    def prepare_attachments(
        self,
        principal: AttachmentPrincipal,
        attachment_ids: Sequence[str],
        *,
        conversation_id: str | None,
    ) -> list[ChatAttachmentReference]:
        """送信の添付を確かめ、発話に残す参照を返す（まだ会話に結び付けない）。

        会話を送信で作る製品（会話の ID が送信の後に決まる）は、これで確かめてから送信を受け付け、
        会話の ID が決まった後に `bind_references` で結び付ける。見えない・期限の切れた添付は 404、
        別の会話の添付は 409、会話の件数の上限を超えると 409。参照には原本の hash を残す（読む対象
        と版を固定する。#2652）。
        """
        self._require_enabled()
        self.purge_expired()
        unique_ids = list(dict.fromkeys(attachment_ids))
        records = [self._visible(principal, attachment_id) for attachment_id in unique_ids]
        for record in records:
            if record.conversation_id and record.conversation_id != conversation_id:
                raise ChatAttachmentError(
                    409,
                    f"「{record.file_name}」は別の会話の添付です。この会話に添付し直してください。",
                    code="CHAT_ATTACHMENT_OTHER_CONVERSATION",
                )
        limits = self.limits()
        current: set[str] = set()
        if conversation_id:
            current = {
                record.attachment_id
                for record in self.store.list_for_conversation(
                    self.product, conversation_id, now=self._clock()
                )
            }
        if len(current | set(unique_ids)) > limits.max_files:
            raise _too_many(limits.max_files)
        return [
            ChatAttachmentReference(
                attachment_id=record.attachment_id,
                file_name=record.file_name,
                kind=record.kind,
                size_bytes=record.size_bytes,
                content_sha256=record.content_sha256,
            )
            for record in records
        ]

    def bind_references(
        self,
        references: Sequence[ChatAttachmentReference],
        *,
        conversation_id: str,
        owner_user_uuid: str,
    ) -> None:
        """確かめた添付を会話に結び付け、会話の添付の期限を延ばす（`prepare_attachments` の後）。"""
        expires_at = self._expires_at(self._clock())
        if references:
            self.store.bind(
                [reference.attachment_id for reference in references],
                conversation_id=conversation_id,
                owner_user_uuid=owner_user_uuid,
                expires_at=expires_at,
            )
        self.store.touch_conversation(self.product, conversation_id, expires_at=expires_at)

    def attach_to_conversation(
        self,
        principal: AttachmentPrincipal,
        attachment_ids: Sequence[str],
        *,
        conversation_id: str,
        owner_user_uuid: str,
    ) -> list[ChatAttachmentReference]:
        """送信の添付を確かめて会話に結び付け、発話に残す参照を返す。

        会話の ID が送信の前に決まる製品が使う。
        """
        references = self.prepare_attachments(
            principal, attachment_ids, conversation_id=conversation_id
        )
        self.bind_references(
            references, conversation_id=conversation_id, owner_user_uuid=owner_user_uuid
        )
        return references

    # ---- 読む ----

    def conversation_records(self, conversation_id: str) -> list[ChatAttachmentRecord]:
        """会話に送信した期限内の添付（会話の権限は製品が確かめる。解析はしない）。"""
        return self.store.list_for_conversation(self.product, conversation_id, now=self._clock())

    def reading_status(self) -> Literal["done", "skipped", "failed"]:
        """添付の読み取りの可否。設定を読めない状態を管理者による無効と区別する。"""
        if self.enabled():
            return "done"
        return "failed" if self._settings_failure() is not None else "skipped"

    def toolset(self, conversation_id: str, *, observe: bool = True) -> ChatAttachmentToolset:
        """会話の添付を読むツール。既存の工程で計測する呼び出しは span を重ねない。"""
        context = observed_span(f"{self.product}.attachment_evidence") if observe else nullcontext()
        with context as span:
            status = self.reading_status()
            if span is not None:
                span.set_status(status)
            if status != "done":
                return ChatAttachmentToolset([], self.ensure_parsed)
            return ChatAttachmentToolset(
                self.conversation_records(conversation_id), self.ensure_parsed, self.content
            )

    def preparation_toolset(
        self,
        principal: AttachmentPrincipal,
        attachment_ids: Sequence[str],
        *,
        conversation_id: str | None,
    ) -> ChatAttachmentToolset:
        """送信された質問の確認に使う添付。認可し、会話への結び付けは確定時まで保留する。"""
        self.prepare_attachments(principal, attachment_ids, conversation_id=conversation_id)
        records = self.conversation_records(conversation_id) if conversation_id else []
        known = {record.attachment_id for record in records}
        records.extend(
            self._visible(principal, attachment_id)
            for attachment_id in dict.fromkeys(attachment_ids)
            if attachment_id not in known
        )
        return ChatAttachmentToolset(records, self.ensure_parsed, self.content)

    def content(self, attachment_id: str) -> bytes | None:
        """送信した期限内の添付の原本（会話の権限は製品が確かめる。無ければ None）。"""
        record = self.store.get(attachment_id)
        if record is None or record.product != self.product or record.expires_at <= self._clock():
            return None
        return self.store.content(attachment_id)

    async def ensure_parsed_async(self, attachment_id: str) -> AttachmentReadResult:
        """`ensure_parsed` を thread で実行する（event loop を止めない）。"""
        return await to_thread.run_sync(self.ensure_parsed, attachment_id)

    def _parse_key(self, record: ChatAttachmentRecord) -> str | None:
        """今の解析の結果を再利用するキー（画像を読み取るモデルが無ければ None）。"""
        if record.kind == "image":
            reading = self._image_reading()
            return image_parse_key(record.content_sha256, reading[1]) if reading else None
        return document_parse_key(record.content_sha256, record.kind)

    @staticmethod
    def _legacy_current(record: ChatAttachmentRecord) -> bool:
        """#2652 より前に解析した行（`parse_state` が無い）の結果をそのまま使えるか。"""
        if record.kind == "excel":
            return (record.outline.parser_version or 0) >= EXCEL_PARSER_VERSION
        return True

    def _parse_lock(self, attachment_id: str) -> threading.Lock:
        with self._parse_locks_guard:
            lock = self._parse_locks.get(attachment_id)
            if lock is None:
                lock = threading.Lock()
                self._parse_locks[attachment_id] = lock
            return lock

    def ensure_parsed(self, attachment_id: str) -> AttachmentReadResult:
        """送信した添付を読めるようにする（未解析なら解析し、結果を保存して再利用する。#2652）。

        会話の権限は製品が先に確かめる。同期で、文書は parser を、画像は既定の Vision モデルを
        呼ぶ（async の処理からは `ensure_parsed_async`）。
        """
        lock = self._parse_lock(attachment_id)
        if not lock.acquire(timeout=self._parse_wait_seconds):
            return AttachmentReadResult(
                attachment_id,
                "parsing",
                record=self.store.get(attachment_id),
                message=_PARSING_MESSAGE,
            )
        try:
            return self._ensure_parsed_locked(attachment_id)
        finally:
            lock.release()

    def _ensure_parsed_locked(self, attachment_id: str) -> AttachmentReadResult:
        deadline = time.monotonic() + self._parse_wait_seconds
        while True:
            now = self._clock()
            record = self.store.get(attachment_id)
            if record is None or record.product != self.product or record.expires_at <= now:
                return AttachmentReadResult(attachment_id, "missing", message=_MISSING_MESSAGE)
            state = record.outline.parse_state
            key = self._parse_key(record)
            if state is None and self._legacy_current(record):
                return self._stored(record)
            if state is not None and state.status == "ready" and state.parse_key == key:
                return self._stored(record)
            if key is None:
                error = image_model_missing()
                return AttachmentReadResult(
                    attachment_id,
                    "failed",
                    record=record,
                    error_code=error.code,
                    message=error.public_message,
                )
            if state is not None and state.parse_key == key:
                if (
                    state.status == "parsing"
                    and state.lease_expires_at is not None
                    and state.lease_expires_at > now
                ):
                    if time.monotonic() >= deadline:
                        return AttachmentReadResult(
                            attachment_id, "parsing", record=record, message=_PARSING_MESSAGE
                        )
                    self._sleep(PARSE_POLL_SECONDS)
                    continue
                if state.status == "failed" and (
                    state.retry_after is None or state.retry_after > now
                ):
                    return self._failed(record, state)
            attempts = state.attempts if state is not None and state.parse_key == key else 0
            claimed = self._claim(record, key, attempts, now=now)
            if claimed is None:
                continue  # ほかの試行が先に書き換えた。読み直す
            result = self._parse_claimed(claimed, key)
            if result is not None:
                return result

    def _stored(self, record: ChatAttachmentRecord) -> AttachmentReadResult:
        parsed = self.store.parsed(record.attachment_id)
        if parsed is None:
            return AttachmentReadResult(record.attachment_id, "missing", message=_MISSING_MESSAGE)
        return AttachmentReadResult(
            record.attachment_id, "ready", record=record, parsed=parsed, reused=True
        )

    def _failed(
        self, record: ChatAttachmentRecord, state: AttachmentParseState
    ) -> AttachmentReadResult:
        """失敗の結果。前の版の結果があれば、警告を付けてそれを読む（#2544）。"""
        previous = self.store.parsed(record.attachment_id)
        if previous is not None and not previous.is_empty():
            warnings = [*previous.warnings, WARNING_EXCEL_REPARSE_FAILED]
            return AttachmentReadResult(
                record.attachment_id,
                "ready",
                record=record,
                parsed=previous.model_copy(update={"warnings": warnings}),
                reused=True,
            )
        return AttachmentReadResult(
            record.attachment_id,
            "failed",
            record=record,
            error_code=state.error_code,
            message=state.error_message,
        )

    def _claim(
        self, record: ChatAttachmentRecord, key: str, attempts: int, *, now: datetime
    ) -> ChatAttachmentRecord | None:
        """解析の試行を始める（`parse_state` を解析中にする）。ほかの試行が先なら None。"""
        state = AttachmentParseState(
            status="parsing",
            revision=record.outline.revision + 1,
            parse_key=key,
            attempt_id=str(uuid.uuid4()),
            attempts=attempts,
            lease_expires_at=now + timedelta(seconds=PARSE_LEASE_SECONDS),
            updated_at=now,
        )
        outline = record.outline.model_copy(update={"parse_state": state})
        if not self.store.replace_state(
            record.attachment_id, outline, expected_revision=record.outline.revision, now=now
        ):
            return None
        return record.model_copy(update={"outline": outline})

    def _extract(self, record: ChatAttachmentRecord, content: bytes) -> ParsedAttachment:
        if record.kind != "image":
            return parse_document(record.file_name, content, record.kind)
        reading = self._image_reading()
        if reading is None:
            raise image_model_missing()
        reader, model_id = reading
        return _run_coroutine(
            lambda: read_image(
                record.file_name,
                content,
                record.media_type,
                image_reader=reader,
                model_id=model_id,
            )
        )

    def _parse_claimed(self, record: ChatAttachmentRecord, key: str) -> AttachmentReadResult | None:
        """始めた試行で解析して保存する。ほかの試行が引き継いでいれば None（読み直す）。"""
        state = record.outline.parse_state
        if state is None:  # `_claim` が付けるので来ない
            raise RuntimeError("parse state missing")
        started = time.monotonic()
        content = self.store.content(record.attachment_id)
        if content is None:
            return AttachmentReadResult(record.attachment_id, "missing", message=_MISSING_MESSAGE)
        parsed: ParsedAttachment | None = None
        error_code = PARSE_FAILED_CODE
        error_message = _RETRY_MESSAGE
        retry_after: datetime | None = None
        attempts = state.attempts
        try:
            parsed = self._extract(record, content)
        except ChatAttachmentError as exc:
            # 入力のせいの失敗（壊れた・暗号化・モデルが無い）。同じ原本と版では試し直さない。
            attempts += 1
            error_code = exc.code or PARSE_FAILED_CODE
            error_message = exc.public_message
        except Exception as exc:  # noqa: BLE001 - 一時的な失敗は回数と間隔を決めて試し直す
            attempts += 1
            if attempts < MAX_PARSE_ATTEMPTS:
                retry_after = self._clock() + timedelta(seconds=PARSE_RETRY_SECONDS * attempts)
            logger.warning(
                "chat_attachment_parse_error",
                extra={
                    "attachment_id": record.attachment_id,
                    "kind": record.kind,
                    "attempts": attempts,
                    "error_type": type(exc).__name__,
                },
            )
        now = self._clock()
        if parsed is not None:
            done = AttachmentParseState(
                status="ready",
                revision=state.revision + 1,
                parse_key=key,
                attempt_id=state.attempt_id,
                updated_at=now,
            )
            outline = parsed.outline().model_copy(update={"parse_state": done})
        else:
            done = AttachmentParseState(
                status="failed",
                revision=state.revision + 1,
                parse_key=key,
                attempt_id=state.attempt_id,
                attempts=attempts,
                retry_after=retry_after,
                error_code=error_code,
                error_message=error_message,
                updated_at=now,
            )
            # 前の結果の索引（#2652 より前の行・前の版）は残す（`_failed` が前の結果を読む）。
            outline = record.outline.model_copy(update={"parse_state": done})
        saved = self.store.replace_state(
            record.attachment_id,
            outline,
            expected_revision=state.revision,
            now=now,
            parsed=parsed,
        )
        logger.info(
            "chat_attachment_parsed",
            extra={
                "attachment_id": record.attachment_id,
                "product": self.product,
                "kind": record.kind,
                "status": done.status,
                "saved": saved,
                "extractor": parsed.extractor if parsed is not None else None,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "model_calls": 1 if record.kind == "image" else 0,
            },
        )
        if not saved:
            # 消えた・期限の切れた行は復活させない。ほかの試行が引き継いだなら、その結果を読む。
            return None
        updated = record.model_copy(update={"outline": outline})
        if parsed is not None:
            return AttachmentReadResult(
                record.attachment_id, "ready", record=updated, parsed=parsed
            )
        return self._failed(updated, done)
