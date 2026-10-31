"""共通の span の表（`PLATFORM_TRACE_SPANS`）の書き込みと読み出し（#3226 の案 A）。

3 製品は span の開始・終了を同じ表に書き、画面は trace id で読んで親子から木にする（業界の分散
tracing の
span store と同じ形）。実行中の span も読めるので、どの process・どの製品の span も実行中に見える。

- **正本は製品の記録**: 実行の最終の状態（完了・失敗・停止）は製品の記録（回答のメッセージ・
  ジョブ・Run・
  実行履歴）にある。この表は処理の経過の表示と調査のための記録で、
  書けなくても主の処理と製品の記録は止めない。
- **書き込み**: `TraceSpanWriter` は process ごとに 1 つ。主の処理は queue に積むだけで DB
  を待たない。
  span ごとに最新の状態だけを残して（開始と終了を 1 回の書き込みにまとめる）、
  間隔か件数でまとめて書く。
- **落としてはいけない span**: 実行の全体（root）と製品の境界（client の `mcp.request`・server の
  span）は
  別の queue に入れ、満杯で捨てない。書けなかった span は trace ごとに数え（`dropped`）、
  製品は実行を
  確定するときにその数を製品の記録に保存する。
- **内容**: 列は構造・時刻・状態・export と同じ allowlist の属性・工程の種類と名前の値（スカラー）
  だけ。
  工程の詳細（`SpanDisplay.details`。検索文・文書名・ツールの引数など）
  は入れない（製品の記録に置く）。
- **保存期限**: `retention_days` が 0（既定）なら消さない。正の値のときだけ、
  その日数を過ぎた行を消す。

共有パッケージは oracledb に直接依存しない（時刻の bind の型を指すときだけ import する）。接続は
製品が `connection_factory`（DB-API の接続を返す context manager）で渡す。
"""

from __future__ import annotations

import importlib
import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from ..chat_progress import ChatProgressParamValue
from .tracing import SpanListener, SpanUpdate

logger = logging.getLogger(__name__)

TRACE_SPANS_TABLE = "PLATFORM_TRACE_SPANS"
TraceProduct = Literal["rag", "nl2sql", "agent"]
type ConnectionFactory = Callable[[], AbstractContextManager[Any]]
# span を書いた製品が付ける、工程の利用者向けの名前（名詞。#3272）。`DISPLAY_PARAMS_JSON` の値の
# この key に入れる（列は足さない）。別の製品の画面は、自分の工程の定義で訳さずにこの名前を出す
# （Jaeger / Langfuse / Phoenix と同じく、span を作ったサービスの名前をそのまま出す）。
DISPLAY_NAME_PARAM = "display_name"
_DISPLAY_NAME_MAX = 128
# 工程の利用者向けの名前を決める関数（製品が書き手に渡す。知らない span は None）。
type SpanDisplayNamer = Callable[[SpanUpdate], str | None]

# JSON の列（VARCHAR2(4000)）に入れる上限。超えたら key を後ろから外して収める。
_JSON_MAX = 4000
_DISPLAY_KIND_MAX = 64
_STATUS_RANK = {"running": 0, "done": 1, "failed": 1, "cancelled": 1, "skipped": 1}

# 時刻は UTC の offset（'+00:00'）で書く。地域名（'UTC'）の時間帯で書くと、python-oracledb の thin
# mode がその値を読めない（DPY-3022。#3241）。
_MERGE_SQL = """
MERGE INTO PLATFORM_TRACE_SPANS t
USING (SELECT :trace_id AS TRACE_ID, :span_id AS SPAN_ID FROM DUAL) s
ON (t.TRACE_ID = s.TRACE_ID AND t.SPAN_ID = s.SPAN_ID)
WHEN MATCHED THEN UPDATE SET
  t.PARENT_SPAN_ID = :parent_span_id,
  t.STATUS = :status,
  t.FINISHED_AT = CASE WHEN :finished_at IS NULL THEN NULL
    ELSE FROM_TZ(CAST(:finished_at AS TIMESTAMP), '+00:00') END,
  t.DISPLAY_KIND = :display_kind,
  t.DISPLAY_PARAMS_JSON = :display_params_json,
  t.ATTRIBUTES_JSON = :attributes_json,
  t.UPDATED_AT = SYSTIMESTAMP
WHEN NOT MATCHED THEN INSERT (
  TRACE_ID, SPAN_ID, PARENT_SPAN_ID, PRODUCT, OPERATION_ID, ATTEMPT, IS_ROOT, OWNER_USER_UUID,
  NAME, SPAN_KIND, STATUS, STARTED_AT, FINISHED_AT, DISPLAY_KIND, DISPLAY_PARAMS_JSON,
  ATTRIBUTES_JSON, UPDATED_AT
) VALUES (
  :trace_id, :span_id, :parent_span_id, :product, :operation_id, :attempt, :is_root,
  :owner_user_uuid, :name, :span_kind, :status,
  FROM_TZ(CAST(:started_at AS TIMESTAMP), '+00:00'),
  CASE WHEN :finished_at IS NULL THEN NULL
    ELSE FROM_TZ(CAST(:finished_at AS TIMESTAMP), '+00:00') END,
  :display_kind, :display_params_json, :attributes_json, SYSTIMESTAMP
)
"""

_PURGE_SQL = (
    "DELETE FROM PLATFORM_TRACE_SPANS "
    "WHERE STARTED_AT < FROM_TZ(CAST(:cutoff AS TIMESTAMP), '+00:00') AND ROWNUM <= :batch"
)

# 時刻の bind。python-oracledb は `datetime` を既定で `DB_TYPE_DATE`（秒まで）で bind するため、
# `TIMESTAMP` で bind して秒より細かい部分（マイクロ秒）を残す（#3271）。
_TIMESTAMP_BINDS = ("started_at", "finished_at")


def _load_oracledb() -> Any:
    return importlib.import_module("oracledb")


def _timestamp_input_sizes(names: Iterable[str]) -> dict[str, Any]:
    timestamp = _load_oracledb().DB_TYPE_TIMESTAMP
    return {name: timestamp for name in names}


# 表の名前と列は固定の文字列（値はすべて bind。SQL の文字列に値を組み込まない）。時刻は
# `SYS_EXTRACT_UTC` で UTC の TIMESTAMP にして読む（時間帯の形に関わらず thin mode で読める。
# #3241）。
_SELECT_SQL = (
    "SELECT TRACE_ID, SPAN_ID, PARENT_SPAN_ID, PRODUCT, OPERATION_ID, ATTEMPT, IS_ROOT, "
    "OWNER_USER_UUID, NAME, SPAN_KIND, STATUS, SYS_EXTRACT_UTC(STARTED_AT) AS STARTED_AT, "
    "SYS_EXTRACT_UTC(FINISHED_AT) AS FINISHED_AT, DISPLAY_KIND, "
    "DISPLAY_PARAMS_JSON, ATTRIBUTES_JSON FROM PLATFORM_TRACE_SPANS WHERE TRACE_ID = :trace_id"
)


@dataclass(frozen=True, slots=True)
class TraceSpanOwner:
    """span を書く実行（製品・実行の id・試行・実行した利用者）。呼ばれた側はサービストークンの
    `sub`。"""

    product: TraceProduct
    operation_id: str
    owner_user_uuid: str
    attempt: int = 0

    def __post_init__(self) -> None:
        # 列は NOT NULL で、Oracle は空の文字列を NULL にする。空のまま積むと同じまとめの書き込み
        # ごと失敗するので、作るときに断る（利用者の分からない実行は、製品が書かないか、決まった値を
        # 持ち主にする）。
        if not self.operation_id.strip():
            raise ValueError("operation_id is required")
        if not self.owner_user_uuid.strip():
            raise ValueError("owner_user_uuid is required")


def is_boundary_span(update: SpanUpdate) -> bool:
    """落としてはいけない span（実行の全体と、製品の境界の client / server の span）。"""
    if update.root or update.span_kind == "server":
        return True
    return update.span_kind == "client" and update.operation_name == "mcp.request"


def _utc_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(UTC).replace(tzinfo=None)


def _bounded_json(values: Mapping[str, object] | None) -> str | None:
    if not values:
        return None
    items = list(values.items())
    while items:
        text = json.dumps(dict(items), ensure_ascii=False, separators=(",", ":"))
        if len(text.encode("utf-8")) <= _JSON_MAX:
            return text
        items.pop()
    return None


def _display_params(
    update: SpanUpdate, namer: SpanDisplayNamer | None
) -> Mapping[str, object] | None:
    """表に入れる名前の値。製品の名前（`DISPLAY_NAME_PARAM`）は先頭に置く（上限で後ろから外す）。"""
    params = dict(update.display.params) if update.display and update.display.params else {}
    name = namer(update) if namer is not None else None
    if isinstance(name, str) and name.strip():
        params.pop(DISPLAY_NAME_PARAM, None)
        return {DISPLAY_NAME_PARAM: name.strip()[:_DISPLAY_NAME_MAX], **params}
    return params


def span_row(
    update: SpanUpdate, owner: TraceSpanOwner, *, namer: SpanDisplayNamer | None = None
) -> dict[str, Any]:
    """書き込みの bind（工程の詳細は入れない）。`namer` は製品の工程の利用者向けの名前。"""
    display = update.display
    return {
        "trace_id": update.trace_id,
        "span_id": update.span_id,
        "parent_span_id": update.parent_span_id,
        "product": owner.product,
        "operation_id": owner.operation_id[:128],
        "attempt": max(0, owner.attempt),
        "is_root": 1 if update.root else 0,
        "owner_user_uuid": owner.owner_user_uuid[:36],
        "name": update.operation_name[:128],
        "span_kind": update.span_kind,
        "status": update.status,
        "started_at": _utc_naive(update.started_at),
        "finished_at": _utc_naive(update.finished_at),
        "display_kind": display.kind[:_DISPLAY_KIND_MAX] if display and display.kind else None,
        "display_params_json": _bounded_json(_display_params(update, namer)),
        "attributes_json": _bounded_json(update.attributes),
    }


@dataclass
class _Pending:
    row: dict[str, Any]
    boundary: bool
    attempts: int = 0
    first_failed_at: float | None = None


@dataclass
class TraceSpanWriter:
    """span の表の書き手（process ごとに 1 つ。lifespan / worker の起動の後に `start`）。

    - `submit` は queue に積むだけ（DB を待たない）。同じ span の知らせは最新の状態だけを残す。
    - 通常の queue が満杯なら、新しい span の開始の知らせを捨てて数える（終了の知らせは、
      まだ書いていない
      開始の知らせを押し出して入れる）。境界の span の queue は満杯でも捨てない。
    - 書き込みに失敗した行は間をおいて再送し、`retries` 回で諦めて数える。境界の span は
      `boundary_retry_seconds` まで再送を続ける。
    - `display_name` は製品の工程の利用者向けの名前（`DISPLAY_NAME_PARAM` に入れる。#3272）。
    """

    connection_factory: ConnectionFactory
    display_name: SpanDisplayNamer | None = None
    flush_seconds: float = 0.5
    batch_size: int = 100
    queue_size: int = 5000
    retries: int = 3
    retry_backoff_seconds: float = 1.0
    boundary_retry_seconds: float = 300.0
    retention_days: int = 0
    purge_interval_seconds: float = 3600.0
    purge_batch: int = 5000
    clock: Callable[[], float] = time.monotonic
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _wake: threading.Event = field(default_factory=threading.Event, repr=False)
    _stopping: threading.Event = field(default_factory=threading.Event, repr=False)
    _normal: OrderedDict[tuple[str, str], _Pending] = field(default_factory=OrderedDict, repr=False)
    _boundary: OrderedDict[tuple[str, str], _Pending] = field(
        default_factory=OrderedDict, repr=False
    )
    _dropped: OrderedDict[str, int] = field(default_factory=OrderedDict, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _next_purge_at: float = 0.0

    # ------------------------------------------------------------------ 主の処理から呼ぶ

    def submit(self, update: SpanUpdate, owner: TraceSpanOwner) -> None:
        key = (update.trace_id, update.span_id)
        boundary = is_boundary_span(update)
        row = span_row(update, owner, namer=self.display_name)
        with self._lock:
            target = self._boundary if boundary else self._normal
            before = target.get(key)
            if before is not None:
                if _STATUS_RANK[row["status"]] >= _STATUS_RANK[before.row["status"]]:
                    before.row = row
                return
            if (
                not boundary
                and len(self._normal) >= self.queue_size
                and (update.status == "running" or not self._evict_running_locked())
            ):
                self._count_dropped_locked(update.trace_id)
                return
            target[key] = _Pending(row=row, boundary=boundary)
        self._wake.set()

    def listener(self, owner: TraceSpanOwner) -> SpanListener:
        """`operation_trace(listener=...)` に渡す知らせの受け口。"""
        return lambda update: self.submit(update, owner)

    def dropped(self, trace_id: str) -> int:
        """書けなかった（捨てた）span の数。製品は実行の確定のときに製品の記録へ保存する。"""
        with self._lock:
            return self._dropped.get(trace_id, 0)

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="trace-span-writer", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stopping.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self.flush()

    def flush(self) -> None:
        """待っている行をすぐ書く（停止・テスト）。"""
        while True:
            batch = self._take_batch()
            if not batch:
                return
            if not self._write(batch):
                return

    # ------------------------------------------------------------------ 内部

    def _run(self) -> None:
        while not self._stopping.is_set():
            self._wake.wait(self.flush_seconds)
            self._wake.clear()
            self.flush()
            self._purge_if_due()

    def _take_batch(self) -> list[_Pending]:
        with self._lock:
            batch: list[_Pending] = []
            for source in (self._boundary, self._normal):
                while source and len(batch) < self.batch_size:
                    _, pending = source.popitem(last=False)
                    batch.append(pending)
            return batch

    def _write(self, batch: list[_Pending]) -> bool:
        try:
            with self.connection_factory() as connection:
                cursor = connection.cursor()
                cursor.setinputsizes(**_timestamp_input_sizes(_TIMESTAMP_BINDS))
                cursor.executemany(_MERGE_SQL, [pending.row for pending in batch])
                connection.commit()
            return True
        except Exception as exc:
            logger.warning("trace_span_write_failed", extra={"error_type": type(exc).__name__})
            self._requeue(batch)
            time.sleep(min(self.retry_backoff_seconds, self.flush_seconds * 4))
            return False

    def _requeue(self, batch: Iterable[_Pending]) -> None:
        now = self.clock()
        with self._lock:
            for pending in batch:
                pending.attempts += 1
                if pending.first_failed_at is None:
                    pending.first_failed_at = now
                key = (pending.row["trace_id"], pending.row["span_id"])
                if pending.boundary:
                    if now - pending.first_failed_at <= self.boundary_retry_seconds:
                        self._boundary.setdefault(key, pending)
                        continue
                elif pending.attempts < self.retries and len(self._normal) < self.queue_size:
                    self._normal.setdefault(key, pending)
                    continue
                self._count_dropped_locked(pending.row["trace_id"])

    def _evict_running_locked(self) -> bool:
        for key, pending in self._normal.items():
            if pending.row["status"] == "running":
                del self._normal[key]
                self._count_dropped_locked(key[0])
                return True
        return False

    def _count_dropped_locked(self, trace_id: str) -> None:
        self._dropped[trace_id] = self._dropped.get(trace_id, 0) + 1
        self._dropped.move_to_end(trace_id)
        while len(self._dropped) > 10_000:
            self._dropped.popitem(last=False)

    def _purge_if_due(self) -> None:
        if self.retention_days <= 0:
            return
        now = self.clock()
        if now < self._next_purge_at:
            return
        self._next_purge_at = now + self.purge_interval_seconds
        purge_trace_spans(
            self.connection_factory,
            retention_days=self.retention_days,
            batch=self.purge_batch,
        )


def purge_trace_spans(
    connection_factory: ConnectionFactory,
    *,
    retention_days: int,
    batch: int = 5000,
    now: datetime | None = None,
    max_batches: int = 100,
) -> int:
    """保存期限を過ぎた span を消す（0 以下は何もしない。既定は無期限）。消した行の数を返す。"""
    if retention_days <= 0:
        return 0
    cutoff = _utc_naive((now or datetime.now(UTC)) - timedelta(days=retention_days))
    deleted = 0
    try:
        with connection_factory() as connection:
            cursor = connection.cursor()
            for _ in range(max_batches):
                cursor.setinputsizes(**_timestamp_input_sizes(("cutoff",)))
                cursor.execute(_PURGE_SQL, {"cutoff": cutoff, "batch": batch})
                count = int(cursor.rowcount or 0)
                connection.commit()
                deleted += count
                if count < batch:
                    break
    except Exception as exc:
        logger.warning("trace_span_purge_failed", extra={"error_type": type(exc).__name__})
    return deleted


# ---------------------------------------------------------------------------
# 読み出し
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredTraceSpan:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    product: str
    operation_id: str
    attempt: int
    root: bool
    owner_user_uuid: str
    name: str
    span_kind: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    display_kind: str | None
    display_params: dict[str, ChatProgressParamValue]
    attributes: dict[str, ChatProgressParamValue]


def _aware(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _json_scalars(value: object) -> dict[str, ChatProgressParamValue]:
    if hasattr(value, "read"):
        value = value.read()
    if not isinstance(value, str) or not value:
        return {}
    try:
        loaded = json.loads(value)
    except ValueError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {
        key: item
        for key, item in loaded.items()
        if isinstance(key, str) and isinstance(item, (str, int, float, bool))
    }


def fetch_trace_spans(
    connection: Any,
    trace_id: str,
    *,
    viewer_user_uuid: str | None,
    limit: int = 1000,
) -> list[StoredTraceSpan]:
    """trace の span を読む。`viewer_user_uuid` が None なら全員の span（system_admin）、それ以外は
    その利用者が持ち主の span だけ（「AI 活用」の見え方。呼ばれた側の span はサービストークンの
    `sub`）。

    画面から任意の trace id を受け取らない。製品は自分の記録を今の判定で開き、その記録の trace id
    を渡す。
    """
    sql = _SELECT_SQL
    binds: dict[str, Any] = {"trace_id": trace_id, "limit": limit}
    if viewer_user_uuid is not None:
        sql += " AND OWNER_USER_UUID = :viewer"
        binds["viewer"] = viewer_user_uuid
    sql += " ORDER BY STARTED_AT, SPAN_ID FETCH FIRST :limit ROWS ONLY"
    cursor = connection.cursor()
    cursor.execute(sql, binds)
    spans: list[StoredTraceSpan] = []
    for row in cursor.fetchall():
        started = _aware(row[11])
        if started is None:
            continue
        spans.append(
            StoredTraceSpan(
                trace_id=str(row[0]),
                span_id=str(row[1]),
                parent_span_id=str(row[2]) if row[2] else None,
                product=str(row[3]),
                operation_id=str(row[4]),
                attempt=int(row[5] or 0),
                root=bool(row[6]),
                owner_user_uuid=str(row[7]),
                name=str(row[8]),
                span_kind=str(row[9]),
                status=str(row[10]),
                started_at=started,
                finished_at=_aware(row[12]),
                display_kind=str(row[13]) if row[13] else None,
                display_params=_json_scalars(row[14]),
                attributes=_json_scalars(row[15]),
            )
        )
    return spans


def latest_attempt_spans(spans: Sequence[StoredTraceSpan]) -> list[StoredTraceSpan]:
    """実行ごと（製品と実行の id）に最新の試行の span だけを残す（前の試行の木は出さない）。"""
    latest: dict[tuple[str, str], int] = {}
    for span in spans:
        key = (span.product, span.operation_id)
        latest[key] = max(latest.get(key, 0), span.attempt)
    return [span for span in spans if span.attempt == latest[(span.product, span.operation_id)]]


def trace_progress_events(
    spans: Sequence[StoredTraceSpan],
    *,
    target_id: str,
    terminal: Literal["done", "failed", "cancelled"] | None = None,
    details: Mapping[str, Mapping[str, Any]] | None = None,
    emitted_at: datetime | None = None,
) -> list[dict[str, Any]]:
    """表の span を処理の経過のイベント（#3227 の span の段と同じ形）
    にする（画面はそのまま木にする）。

    `seq` はこの一覧の中の番号（snapshot）。`terminal` は製品の記録の状態（正本）。
    製品が終端なのに表で実行中の
    span（書けなかった・process が落ちた）は、終端に合わせて閉じる。`details`
    は製品の記録にある工程の詳細
    （span id → 詳細。製品の span の分だけ）。
    """
    now = emitted_at or datetime.now(UTC)
    events: list[dict[str, Any]] = []
    closing = {"done": "done", "failed": "failed", "cancelled": "skipped"}
    for index, span in enumerate(latest_attempt_spans(spans), start=1):
        status = "skipped" if span.status == "cancelled" else span.status
        cancelled = span.status == "cancelled"
        finished = span.finished_at
        if status == "running" and terminal is not None:
            status = closing[terminal]
            cancelled = terminal == "cancelled"
            finished = finished or now
        event: dict[str, Any] = {
            "schema_version": 1,
            "seq": index,
            "target_id": target_id,
            "attempt": 0,
            "emitted_at": now.isoformat(),
            "type": "step",
            "step_id": span.span_id,
            "status": status,
            "started_at": span.started_at.isoformat(),
            "name": span.name,
            "trace_id": span.trace_id,
            "product": span.product,
        }
        if finished is not None and status != "running":
            event["finished_at"] = finished.isoformat()
        if span.parent_span_id:
            event["parent_id"] = span.parent_span_id
        if span.root:
            event["root"] = True
        if span.display_kind:
            event["kind"] = span.display_kind
        # 製品が付けた工程の名前は、製品の工程の定義の値（params）と分けて送る（#3272）。
        params = dict(span.display_params)
        display_name = params.pop(DISPLAY_NAME_PARAM, None)
        if isinstance(display_name, str) and display_name.strip():
            event["display_name"] = display_name.strip()[:_DISPLAY_NAME_MAX]
        if params:
            event["params"] = params
        if span.attributes:
            event["attributes"] = span.attributes
        if cancelled:
            event["cancelled"] = True
        if details and span.span_id in details:
            event["details"] = dict(details[span.span_id])
        events.append(event)
    if terminal is not None:
        events.append(
            {
                "schema_version": 1,
                "seq": len(events) + 1,
                "target_id": target_id,
                "attempt": 0,
                "emitted_at": now.isoformat(),
                "type": "terminal",
                "status": terminal,
            }
        )
    return events


__all__ = [
    "DISPLAY_NAME_PARAM",
    "TRACE_SPANS_TABLE",
    "ConnectionFactory",
    "SpanDisplayNamer",
    "StoredTraceSpan",
    "TraceProduct",
    "TraceSpanOwner",
    "TraceSpanWriter",
    "fetch_trace_spans",
    "is_boundary_span",
    "latest_attempt_spans",
    "purge_trace_spans",
    "span_row",
    "trace_progress_events",
]
