"""処理の経過の記録（共通の span の表 `PLATFORM_TRACE_SPANS`。#3226 の案 A）。

処理の経過は trace（span の木）の投影で、span は共通の表に書く（`pr_backend_core.observability.
trace_store`）。RAG はこのモジュールで次を持つ。

- **書き手**: process ごとの `TraceSpanWriter`。API の lifespan の起動の後に
  `start_trace_span_writer`、終了で `stop_trace_span_writer`。接続は RAG の Oracle の接続（共通の
  `PLATFORM_*` と同じ schema）。
- **1 回の回答・検索の記録**（`AnswerTrace`）: span の知らせを表の書き手へ渡し、root の span の
  trace id と工程の詳細（`SpanDisplay.details`。表には入れない）を集める。製品の記録（チャットの回
  答の `progress_json`・実行履歴の `stages_json.trace`・回答の記録の `diagnostics_json.trace`）に
  は `TraceRecord`（`trace_id`・`trace_dropped_spans`・`span_details`）だけを保存する。
- **読み出し**（`trace_snapshot`）: 製品の記録を今の判定で開いた後に、その記録の trace id で表を読
  み、処理の経過のイベント（span の段）にする。終端は製品の記録の状態（正本）。画面から任意の
  trace id は受け取らない。
- **trace の親**: 画面の要求・外部の MCP クライアントの traceparent は表の親子に使わない（新しい
  trace を始め、来た span は OpenTelemetry の span link にだけ残す）。サービストークン（Agent）の
  MCP の要求の中だけ、`mcp_request_trace` が Agent の trace を続ける。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Literal
from uuid import uuid4

from fastapi import Request
from pr_backend_core.observability import request_id_var
from pr_backend_core.observability.call_details import withhold_call_details
from pr_backend_core.observability.mcp_trace import (
    incoming_mcp_traceparent,
    mcp_server_trace,
    operation_trace_parent,
)
from pr_backend_core.observability.model_usage import ModelUsageCollector, RunModelUsage
from pr_backend_core.observability.trace_store import (
    TraceSpanOwner,
    TraceSpanWriter,
    fetch_trace_spans,
    trace_progress_events,
)
from pr_backend_core.observability.tracing import SpanListener, SpanUpdate
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings
from app.rag.request_context import current_audit_request_context
from app.rag.span_display import span_display_name

logger = logging.getLogger(__name__)

TRACE_PRODUCT: Literal["rag"] = "rag"
type TraceTerminal = Literal["done", "failed", "cancelled"]

_WRITER: TraceSpanWriter | None = None
_WRITER_LOCK = threading.Lock()


# --------------------------------------------------------------------------- 書き手（process ごと）
# ---------------------------------------------------------------------------


@contextmanager
def _trace_connection() -> Iterator[Any]:
    from app.clients.oracle import OracleClient

    connection = OracleClient().acquire_connection()
    try:
        yield connection
    finally:
        with contextlib.suppress(Exception):
            connection.close()


def start_trace_span_writer(settings: Settings) -> TraceSpanWriter | None:
    """このプロセスの書き手を始める（DB の接続の設定が無ければ何もしない）。"""
    from app.clients.oracle import OracleClient

    global _WRITER
    with _WRITER_LOCK:
        if _WRITER is not None:
            return _WRITER
        if not OracleClient(settings).is_connection_configured():
            return None
        writer = TraceSpanWriter(
            connection_factory=_trace_connection,
            flush_seconds=settings.trace_store_flush_seconds,
            batch_size=settings.trace_store_batch_size,
            queue_size=settings.trace_store_queue_size,
            retention_days=settings.trace_retention_days,
            display_name=span_display_name,
        )
        writer.start()
        _WRITER = writer
        return writer


def stop_trace_span_writer() -> None:
    """このプロセスの書き手を止める（待っている span を書いてから）。"""
    global _WRITER
    with _WRITER_LOCK:
        writer, _WRITER = _WRITER, None
    if writer is not None:
        writer.stop()


def trace_span_writer() -> TraceSpanWriter | None:
    return _WRITER


def set_trace_span_writer(writer: TraceSpanWriter | None) -> TraceSpanWriter | None:
    """書き手を差し替える（テスト）。前の書き手を返す。"""
    global _WRITER
    with _WRITER_LOCK:
        previous, _WRITER = _WRITER, writer
    return previous


def _owner_listener(operation_id: str, owner_user_uuid: str | None) -> SpanListener | None:
    writer = _WRITER
    if writer is None or not owner_user_uuid:
        return None
    return writer.listener(
        TraceSpanOwner(
            product=TRACE_PRODUCT, operation_id=operation_id, owner_user_uuid=owner_user_uuid
        )
    )


# --------------------------------------------------------------------------- 製品の記録に置く値
# ---------------------------------------------------------------------------


class TraceRecord(BaseModel):
    """製品の記録に置く処理の経過の値（表の span を引く trace id・書けなかった数・工程の詳細）。"""

    trace_id: str | None = Field(default=None, pattern="^[0-9a-f]{32}$")
    trace_dropped_spans: int = Field(default=0, ge=0)
    # span id → 工程の詳細（`ChatProgressStepDetails` の JSON。表には入れない）。
    span_details: dict[str, dict[str, Any]] = Field(default_factory=dict)


def parse_trace_record(value: object) -> TraceRecord | None:
    """保存した値を読む。古い形式（段のイベントの一覧など）・形の違う値は None。"""
    if not isinstance(value, Mapping):
        return None
    try:
        return TraceRecord.model_validate(dict(value))
    except ValidationError:
        return None


class AnswerTrace:
    """1 回の回答・検索の処理の経過（span の表の書き込みと、製品の記録に置く値）。

    `listener` を `RagPipeline.run(trace=...)` が `operation_trace` に渡す。span の知らせは表の書
    き手へ渡し（queue に積むだけ）、root の trace id と工程の詳細をここに集める。回答の thread か
    らも呼ばれる。trace id が分かった・工程の詳細が増えたら `on_change` を呼ぶ（チャットの作成中の
    記録の保存の予約）。
    """

    def __init__(
        self,
        operation_id: str,
        *,
        owner_user_uuid: str | None,
        model_id: str = "",
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.operation_id = operation_id
        self.owner_user_uuid = owner_user_uuid
        self.model_id = model_id
        self._on_change = on_change
        self._lock = threading.Lock()
        self._trace_id: str | None = None
        self._details: dict[str, dict[str, Any]] = {}
        self._version = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        # 実行履歴・失敗の「詳細」に出す RAG の実行 id（`new_trace_id`。表の trace id とは別）。
        self.run_id: str | None = None
        # 実行のモデルの呼び出しの回数と利用量（実行履歴の概要。同じ span の知らせから集める。
        # #3419）。
        self._model_usage = ModelUsageCollector()

    @property
    def message_id(self) -> str:
        """対象の id（チャットは作成中の回答のメッセージの id）。"""
        return self.operation_id

    @property
    def trace_id(self) -> str | None:
        """表の trace id（root の span の W3C trace id。root が始まるまで None）。"""
        with self._lock:
            return self._trace_id

    @property
    def version(self) -> int:
        """記録の値が変わった回数（保存し直すかの判定）。"""
        with self._lock:
            return self._version

    def attach(self, *, on_change: Callable[[], None] | None) -> None:
        self._on_change = on_change

    def listener(self) -> SpanListener:
        """`operation_trace(listener=...)` に渡す listener（回答の thread からも呼ばれる）。

        イベントループの中で作る（`on_change` をループで呼ぶ）。
        """
        with contextlib.suppress(RuntimeError):
            self._loop = asyncio.get_running_loop()
            self._loop_thread = threading.get_ident()
        writer_listener = _owner_listener(self.operation_id, self.owner_user_uuid)

        def listen(update: SpanUpdate) -> None:
            if writer_listener is not None:
                writer_listener(update)
            self._model_usage.observe(update)
            if self._collect(update):
                self._notify()
            _log_step(update, self)

        return listen

    def record(self) -> dict[str, Any]:
        """製品の記録に置く値（書けなかった span の数は、呼んだ時点の書き手の数）。"""
        with self._lock:
            trace_id = self._trace_id
            details = {key: dict(value) for key, value in self._details.items()}
        writer = _WRITER
        dropped = writer.dropped(trace_id) if writer is not None and trace_id else 0
        return TraceRecord(
            trace_id=trace_id, trace_dropped_spans=dropped, span_details=details
        ).model_dump(mode="json")

    def model_usage(
        self, *, primary_model: str | None = None, truncated: bool = False
    ) -> RunModelUsage | None:
        """この実行のモデルの呼び出しの回数と利用量（trace が始まっていなければ None。#3419）。"""
        return self._model_usage.summary(primary_model=primary_model, truncated=truncated)

    def _collect(self, update: SpanUpdate) -> bool:
        changed = False
        with self._lock:
            if update.root and self._trace_id is None:
                self._trace_id = update.trace_id
                changed = True
            display = update.display
            if display is not None and display.details is not None:
                details = display.details.model_dump(mode="json", exclude_none=True)
                if self._details.get(update.span_id) != details:
                    self._details[update.span_id] = details
                    changed = True
            if changed:
                self._version += 1
        return changed

    def _notify(self) -> None:
        callback = self._on_change
        loop = self._loop
        if callback is None:
            return
        if loop is None or threading.get_ident() == self._loop_thread:
            callback()
            return
        # 回答の thread の span。ループが閉じた後（取り消し）の知らせは捨てる。
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(callback)


def _log_step(update: SpanUpdate, trace: AnswerTrace) -> None:
    """工程の span（表示用の種類がある）の終了をログに出す（技術の span・root は出さない）。"""
    display = update.display
    if update.status == "running" or update.root or display is None or not display.kind:
        return
    duration_ms = (
        round((update.finished_at - update.started_at).total_seconds() * 1000, 3)
        if update.finished_at is not None
        else None
    )
    logger.log(
        logging.WARNING if update.status == "failed" else logging.INFO,
        "チャットの段階が終わりました",
        extra={
            "event": "chat_progress_step",
            "request_id": request_id_var.get(),
            "trace_id": trace.run_id,
            "model_id": trace.model_id,
            "message_id": trace.operation_id,
            "step_id": update.span_id,
            "step_kind": display.kind,
            "step_name": (display.params or {}).get("name"),
            "status": update.status,
            "duration_ms": duration_ms,
        },
    )


# --------------------------------------------------------------------------- trace の親（画面・外
# 部の traceparent を表の親子に使わない）
# ---------------------------------------------------------------------------


@contextmanager
def mcp_request_trace(request: Request, message: Mapping[str, Any]) -> Iterator[None]:
    """`POST /api/mcp` の `tools/call` を server の span（`mcp.server`）の中で処理する。

    サービストークン（Agent）の要求だけ、来た traceparent（`params._meta` を優先し、無ければ HTTP
    header）を親にする。API キーの外部の MCP クライアントは新しい trace（span link だけ）。server
    の span の持ち主はサービストークンの `sub`・API キーの利用者。
    """
    claims = getattr(request.state, "service_token_claims", None)
    trusted = isinstance(claims, dict)
    params = message.get("params")
    meta = params.get("_meta") if isinstance(params, Mapping) else None
    traceparent = incoming_mcp_traceparent(
        meta if isinstance(meta, Mapping) else None, request.headers.get("traceparent")
    )
    operation_id = str(getattr(request.state, "request_id", "") or "") or uuid4().hex
    listener = _owner_listener(operation_id, current_audit_request_context().user_uuid)
    # 中の回答・検索は共通の `operation_trace_parent` で、信頼した要求なら Agent の trace を続ける。
    tool_name = params.get("name") if isinstance(params, Mapping) else None
    with mcp_server_trace(
        operation_id=operation_id,
        trusted=trusted,
        traceparent=traceparent,
        listener=listener,
        method="tools/call",
        tool_name=tool_name if isinstance(tool_name, str) else None,
    ):
        yield


# --------------------------------------------------------------------------- 読み出し
# ---------------------------------------------------------------------------


class TraceSnapshot(BaseModel):
    """処理の経過の snapshot（span の段のイベントの一覧。画面の `useTraceProgressSnapshot`
    が読む）。"""

    events: list[dict[str, Any]] = Field(default_factory=list)
    # 書けなかった span があった（1 以上のときだけ。画面は「一部の処理だけを記録しています。」）。
    dropped_spans: int | None = None


# 表の名前と列は固定の文字列（値はすべて bind。共通の `TRACE_SPANS_TABLE` と同じ表）。
_LOOKUP_SQL = (
    "SELECT TRACE_ID FROM PLATFORM_TRACE_SPANS "
    "WHERE PRODUCT = :product AND OPERATION_ID = :operation_id AND IS_ROOT = 1"
)


def _viewer() -> tuple[bool, str | None]:
    """（読めるか、絞り込みの利用者）。

    system_admin は全員の span（None）、それ以外は自分の span。
    """
    context = current_audit_request_context()
    if context.all_users_visible:
        return True, None
    return bool(context.user_uuid), context.user_uuid


def _lookup_trace_id(connection: Any, operation_id: str, viewer: str | None) -> str | None:
    sql = _LOOKUP_SQL
    binds: dict[str, Any] = {"product": TRACE_PRODUCT, "operation_id": operation_id}
    if viewer is not None:
        sql += " AND OWNER_USER_UUID = :viewer"
        binds["viewer"] = viewer
    sql += " ORDER BY ATTEMPT DESC, STARTED_AT DESC FETCH FIRST 1 ROWS ONLY"
    cursor = connection.cursor()
    cursor.execute(sql, binds)
    rows = cursor.fetchall()
    return str(rows[0][0]) if rows and rows[0][0] else None


def _read_snapshot(
    record: TraceRecord | None,
    *,
    target_id: str,
    terminal: TraceTerminal | None,
    lookup_operation_id: str | None,
    viewer: str | None,
) -> TraceSnapshot:
    trace_id = record.trace_id if record is not None else None
    with _trace_connection() as connection:
        if trace_id is None and lookup_operation_id is not None:
            trace_id = _lookup_trace_id(connection, lookup_operation_id, viewer)
        if trace_id is None:
            return TraceSnapshot()
        spans = fetch_trace_spans(connection, trace_id, viewer_user_uuid=viewer)
    if not spans:
        return TraceSnapshot()
    dropped = record.trace_dropped_spans if record is not None else 0
    return TraceSnapshot(
        events=trace_progress_events(
            spans,
            target_id=target_id,
            terminal=terminal,
            details=withhold_call_details(record.span_details, allow_restricted=viewer is None)
            if record is not None
            else None,
        ),
        dropped_spans=dropped or None,
    )


async def trace_snapshot(
    saved: object,
    *,
    target_id: str,
    terminal: TraceTerminal | None,
    lookup_operation_id: str | None = None,
) -> TraceSnapshot:
    """製品の記録（呼び出し側が今の判定で開いた記録）の処理の経過の snapshot。

    - 古い形式の記録（`TraceRecord` で読めない値）: 終わった実行は終端のイベントだけ（画面は古い形
      式の案内を出す）、実行中は空。
    - trace id がまだ無い（root の span が始まる前）: `lookup_operation_id` があれば表を実行の id
      で引く（記録を作る前の RAG 検索の実行）。無ければ空。
    - 表に span が無い（見えない・保存期限を過ぎた・書けなかった）: 空（終端は製品の記録の状態で出
      す）。
    """
    record = parse_trace_record(saved)
    if record is None and lookup_operation_id is None:
        return TraceSnapshot(
            events=trace_progress_events([], target_id=target_id, terminal=terminal)
            if terminal is not None
            else []
        )
    readable, viewer = _viewer()
    if not readable:
        return TraceSnapshot()
    return await asyncio.to_thread(
        _read_snapshot,
        record,
        target_id=target_id,
        terminal=terminal,
        lookup_operation_id=lookup_operation_id,
        viewer=viewer,
    )


_RUN_TERMINAL: dict[str, TraceTerminal] = {"success": "done", "blocked": "done", "error": "failed"}


async def run_trace_snapshot(oracle: Any, row: Mapping[str, Any]) -> TraceSnapshot:
    """実行履歴の行（呼び出し側が実行履歴の見え方で開いた行）の処理の経過の snapshot。

    チャットの実行は、会話の回答の記録（同じ trace。実行の記録の後の工程の詳細まで持つ）を優先する。
    終端は実行の状態（正本）。
    """
    raw_stages = row.get("stages_json")
    stages = raw_stages if isinstance(raw_stages, Mapping) else {}
    saved: object = stages.get("trace")
    conversation_id = row.get("conversation_id")
    message_id = row.get("message_id")
    if conversation_id and message_id:
        message = await oracle.get_chat_message(str(conversation_id), str(message_id))
        from_message = parse_trace_record(message.progress) if message is not None else None
        from_run = parse_trace_record(saved)
        if (
            from_message is not None
            and from_message.trace_id is not None
            and (from_run is None or from_run.trace_id in (None, from_message.trace_id))
        ):
            saved = message.progress
    return await trace_snapshot(
        saved,
        target_id=str(row.get("run_id") or ""),
        terminal=_RUN_TERMINAL.get(str(row.get("status") or ""), "done"),
    )


__all__ = [
    "TRACE_PRODUCT",
    "AnswerTrace",
    "TraceRecord",
    "TraceSnapshot",
    "mcp_request_trace",
    "operation_trace_parent",
    "parse_trace_record",
    "run_trace_snapshot",
    "set_trace_span_writer",
    "start_trace_span_writer",
    "stop_trace_span_writer",
    "trace_snapshot",
    "trace_span_writer",
]
