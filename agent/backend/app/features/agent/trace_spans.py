"""Agent の Run の span を、共通の span の表（`PLATFORM_TRACE_SPANS`。#3226 の案 A）に書いて読む。

- 書き込み: 保存先が Oracle（`storage_backend.resolved_backend()`）なら、共通の書き手
  `TraceSpanWriter` が `PLATFORM_ORACLE_*` の接続で書く。書き手は process ごとに 1 つで、API の
  lifespan と runtime dispatcher の起動の後に `start`、終了で `stop` する（import 時・fork 前に
  thread を作らない）。主の処理は queue に積むだけで DB を待たない。
- 保存先が memory / file（DB を持たない開発・テスト）なら、同じ形の span を process の中に持つ
  （`MemoryTraceSpans`）。memory / file の Run は 1 つの process で実行するので、同じ process の
  API から読める。表の行と同じく、工程の詳細（`SpanDisplay.details`）は入れない（Run に置く）。
- 読み出し: 画面は Run を今の判定で開き、その Run の trace id だけを渡す（任意の trace id を
  受け取らない）。`viewer_user_uuid` が None なら全員の span（system_admin）、それ以外はその利用者が
  持ち主の span だけ。
"""

from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict

from pr_backend_core.chat_progress import ChatProgressParamValue
from pr_backend_core.observability.trace_store import (
    StoredTraceSpan,
    TraceProduct,
    TraceSpanOwner,
    TraceSpanWriter,
    fetch_trace_spans,
    span_row,
)
from pr_backend_core.observability.tracing import SpanUpdate

logger = logging.getLogger(__name__)

# 表の製品の名前（`PRODUCT` 列）。
TRACE_PRODUCT: TraceProduct = "agent"
# memory / file の保存先で持つ trace の数の上限（古い trace から捨てる）。
MEMORY_MAX_TRACES = 2000
# 1 つの trace で読む span の上限（共通の読み出しの既定と同じ）。
FETCH_LIMIT = 1000
_STATUS_RANK = {"running": 0, "done": 1, "failed": 1, "cancelled": 1, "skipped": 1}


def _scalars(text: str | None) -> dict[str, ChatProgressParamValue]:
    if not text:
        return {}
    loaded = json.loads(text)
    return {
        key: value for key, value in loaded.items() if isinstance(value, (str, int, float, bool))
    }


class MemoryTraceSpans:
    """表と同じ形の span を process の中に持つ（memory / file の保存先。開発・テスト）。"""

    def __init__(self, max_traces: int = MEMORY_MAX_TRACES) -> None:
        self._lock = threading.Lock()
        self._max_traces = max_traces
        self._traces: OrderedDict[str, dict[str, StoredTraceSpan]] = OrderedDict()

    def submit(self, update: SpanUpdate, owner: TraceSpanOwner) -> None:
        # 表に書く値と同じ形（長さの上限・JSON の上限・工程の詳細を入れない）にしてから持つ。
        row = span_row(update, owner)
        span = StoredTraceSpan(
            trace_id=row["trace_id"],
            span_id=row["span_id"],
            parent_span_id=row["parent_span_id"],
            product=row["product"],
            operation_id=row["operation_id"],
            attempt=row["attempt"],
            root=bool(row["is_root"]),
            owner_user_uuid=row["owner_user_uuid"],
            name=row["name"],
            span_kind=row["span_kind"],
            status=row["status"],
            started_at=update.started_at,
            finished_at=update.finished_at,
            display_kind=row["display_kind"],
            display_params=_scalars(row["display_params_json"]),
            attributes=_scalars(row["attributes_json"]),
        )
        with self._lock:
            spans = self._traces.setdefault(span.trace_id, {})
            self._traces.move_to_end(span.trace_id)
            before = spans.get(span.span_id)
            if before is None or _STATUS_RANK[span.status] >= _STATUS_RANK[before.status]:
                spans[span.span_id] = span
            while len(self._traces) > self._max_traces:
                self._traces.popitem(last=False)

    def fetch(self, trace_id: str, *, viewer_user_uuid: str | None) -> list[StoredTraceSpan]:
        with self._lock:
            spans = list(self._traces.get(trace_id, {}).values())
        if viewer_user_uuid is not None:
            spans = [span for span in spans if span.owner_user_uuid == viewer_user_uuid]
        spans.sort(key=lambda span: (span.started_at, span.span_id))
        return spans[:FETCH_LIMIT]

    def clear(self) -> None:
        with self._lock:
            self._traces.clear()


class AgentTraceSpans:
    """Agent の process の span の書き手と読み出し（Oracle は共通の表、ほかは process の中）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._writer: TraceSpanWriter | None = None
        self.memory = MemoryTraceSpans()

    @property
    def uses_table(self) -> bool:
        """共通の span の表に書いているか（Oracle の保存先で `start` した process）。"""
        return self._writer is not None

    def start(self) -> None:
        """保存先が Oracle なら共通の書き手を始める（lifespan / worker の起動の後に呼ぶ）。"""
        from app.features.agent.storage_backend import ORACLE_BACKENDS, resolved_backend
        from app.oracle_connection import platform_oracle_connection
        from app.settings import get_settings

        if resolved_backend() not in ORACLE_BACKENDS:
            return
        settings = get_settings()
        with self._lock:
            if self._writer is not None:
                return
            writer = TraceSpanWriter(
                connection_factory=platform_oracle_connection,
                flush_seconds=settings.trace_store_flush_seconds,
                batch_size=settings.trace_store_batch_size,
                queue_size=settings.trace_store_queue_size,
                retention_days=settings.trace_retention_days,
            )
            writer.start()
            self._writer = writer

    def stop(self, timeout: float = 5.0) -> None:
        """待っている span を書いてから終える（終了の処理）。"""
        with self._lock:
            writer, self._writer = self._writer, None
        if writer is not None:
            writer.stop(timeout)

    def use_writer(self, writer: TraceSpanWriter | None) -> None:
        """書き手を差し替える（テスト）。"""
        with self._lock:
            self._writer = writer

    def submit(self, update: SpanUpdate, owner: TraceSpanOwner) -> None:
        writer = self._writer
        if writer is not None:
            writer.submit(update, owner)
        else:
            self.memory.submit(update, owner)

    def dropped(self, trace_id: str) -> int:
        """この process で書けなかった span の数（Run の確定のときに Run に残す）。"""
        writer = self._writer
        return writer.dropped(trace_id) if writer is not None else 0

    def fetch(self, trace_id: str, *, viewer_user_uuid: str | None) -> list[StoredTraceSpan]:
        """trace の span（`viewer_user_uuid` が None なら全員、それ以外はその利用者の span）。"""
        if self._writer is None:
            return self.memory.fetch(trace_id, viewer_user_uuid=viewer_user_uuid)
        from app.oracle_connection import platform_oracle_connection

        with platform_oracle_connection() as connection:
            return fetch_trace_spans(
                connection, trace_id, viewer_user_uuid=viewer_user_uuid, limit=FETCH_LIMIT
            )


def run_trace_owner(run_id: str, user_uuid: str | None) -> TraceSpanOwner | None:
    """Run の span の持ち主（Run を実行した利用者。呼び先の MCP のサービストークンの `sub`）。

    利用者の無い Run（古い Run）は表に書かない（持ち主の列は必須で、system_admin だけが見る Run）。
    """
    owner = (user_uuid or "").strip()
    if not owner:
        return None
    return TraceSpanOwner(product=TRACE_PRODUCT, operation_id=run_id, owner_user_uuid=owner)


trace_spans = AgentTraceSpans()


__all__ = [
    "FETCH_LIMIT",
    "MEMORY_MAX_TRACES",
    "TRACE_PRODUCT",
    "AgentTraceSpans",
    "MemoryTraceSpans",
    "run_trace_owner",
    "trace_spans",
]
