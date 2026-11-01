"""処理の経過の span を共通の span の表（`PLATFORM_TRACE_SPANS`）に書いて読む（#3226 の案 A）。

- 書き込みは process ごとに 1 つの書き手（`pr_backend_core` の `TraceSpanWriter`）。API の
  lifespan とジョブの worker の起動の後に `start_trace_span_store`、終了で
  `stop_trace_span_store` を呼ぶ。主の処理は queue に積むだけで DB を待たない（ジョブの文書の
  保存の往復は増えない。#904）。接続は共通認証と同じ pool（`PLATFORM_*` と同じ schema・同じ
  接続。状態の保存先・業務データの接続とは混ぜない）。
- 状態の保存先が memory（テスト・local）のときは書き手を入れない（span の表は無い）。テストは
  `install_trace_span_store` で fake の接続に差し替える。
- 読むときは、製品の記録（ジョブ・実行履歴・DB 管理チャットのターン）を今の判定で開いた後に、
  その記録の trace id で読み、その実行（`OPERATION_ID`）の span だけを返す。最終の状態（完了・
  失敗・停止）は製品の記録が正本で、表で実行中のままの span は記録の終端に合わせて閉じる。
  工程の詳細は記録に置いた `span_details` から足す（表には入れない）。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Literal

from opentelemetry import trace
from pr_backend_core.chat_progress import parse_chat_progress_events
from pr_backend_core.observability.mcp_trace import traceparent_links
from pr_backend_core.observability.trace_store import (
    ConnectionFactory,
    TraceSpanOwner,
    TraceSpanWriter,
    fetch_trace_spans,
    trace_progress_events,
)
from pr_backend_core.observability.tracing import (
    SpanListener,
    SpanUpdate,
    validated_trace_context,
)

from app.settings import get_settings

from .models import TraceProgressData

logger = logging.getLogger(__name__)

TRACE_PRODUCT: Literal["nl2sql"] = "nl2sql"
# 実行した利用者が分からない実行（認証が無効の期間）の持ち主。列は NOT NULL で、空の文字列は
# Oracle では NULL になり、同じまとめの書き込みごと失敗するため。
ANONYMOUS_TRACE_OWNER = "anonymous"

type TraceTerminal = Literal["done", "failed", "cancelled"]
type SpanDetails = dict[str, dict[str, Any]]

# 工程（span の属性 `pr.stage`）の利用者向けの名前（名詞。#3272）。共通の span の表の書き手が span
# の名前の値 `display_name` に書き、別の製品の画面（Agent の実行の詳細）は NL2SQL の工程の定義を
# 持たずにこの名前を出す。NL2SQL の画面の工程の名前（frontend の i18n の
# `chat.progress.<工程>.idle`）と同じにする（frontend の `sql-chat-progress.test.ts` が照合する）。
STAGE_DISPLAY_NAMES: dict[str, str] = {
    "queue": "処理の開始",
    "prepare_context": "質問と参照できる表を整理する",
    "generate_sql": "SQL の生成",
    "safety_check": "SQL の種類と参照範囲を検査する",
    "execute_sql": "SQL の実行",
    "format_results": "SQL の実行結果を表示用に整える",
}


# MCP の `nl2sql_query` の回答前の確認（#1441）の工程。ジョブの外の工程なので NL2SQL の画面の処理の
# 経過（ジョブの span）には出ず、呼び出し元（Agent の実行の詳細）だけが出す。確認の LLM の呼び出しは
# この span の子にする（#3477）。名前は 3 製品の総称（platform の UX 契約
# confirm-before-answer.md）。
REQUIREMENT_STAGE = "requirement"
REQUIREMENT_SPAN = "nl2sql.requirement"
MCP_STAGE_DISPLAY_NAMES: dict[str, str] = {REQUIREMENT_STAGE: "回答前の確認"}


def span_display_name(update: SpanUpdate) -> str | None:
    """NL2SQL の span の利用者向けの名前（工程の span だけ。技術の span・知らない段は None）。"""
    stage = update.attributes.get("pr.stage")
    if not isinstance(stage, str):
        return None
    return STAGE_DISPLAY_NAMES.get(stage) or MCP_STAGE_DISPLAY_NAMES.get(stage)


class TraceSpanReadError(RuntimeError):
    """span の表を読めなかった（DB の一時的な障害など）。画面は前の内容を残して読み直す。"""


@dataclass(frozen=True, slots=True)
class TraceSpanStore:
    writer: TraceSpanWriter
    connection_factory: ConnectionFactory


_STORE: TraceSpanStore | None = None
_STORE_LOCK = threading.Lock()


def _oracle_connection() -> AbstractContextManager[Any]:
    # 呼ぶたびに今の設定で接続する（DB の設定の保存で pool が作り直される）。
    from .oracle_adapter import OracleNl2SqlAdapter

    return OracleNl2SqlAdapter(get_settings()).pooled_connection()


def install_trace_span_store(
    connection_factory: ConnectionFactory, *, start: bool = True
) -> TraceSpanStore:
    """書き手と読み出しの接続を入れる（前のものは止める）。

    テストは `start=False` で入れて `flush` を呼ぶ。
    """

    settings = get_settings()
    store = TraceSpanStore(
        writer=TraceSpanWriter(
            connection_factory,
            flush_seconds=settings.trace_store_flush_seconds,
            batch_size=settings.trace_store_batch_size,
            queue_size=settings.trace_store_queue_size,
            retention_days=settings.trace_retention_days,
            display_name=span_display_name,
        ),
        connection_factory=connection_factory,
    )
    global _STORE
    with _STORE_LOCK:
        previous, _STORE = _STORE, store
    if previous is not None:
        previous.writer.stop()
    if start:
        store.writer.start()
    return store


def reset_trace_span_store() -> None:
    """書き手を止めて外す（テスト）。"""

    global _STORE
    with _STORE_LOCK:
        previous, _STORE = _STORE, None
    if previous is not None:
        previous.writer.stop()


def current_trace_span_store() -> TraceSpanStore | None:
    with _STORE_LOCK:
        return _STORE


def start_trace_span_store() -> None:
    """process の起動の後に書き手を始める（状態の保存先が memory なら何もしない）。"""

    if get_settings().nl2sql_persistence_mode.strip().lower() == "memory":
        return
    store = current_trace_span_store()
    if store is not None:
        store.writer.start()
        return
    install_trace_span_store(_oracle_connection)


def stop_trace_span_store() -> None:
    """process の終了で、待っている span を書いてから書き手を止める。"""

    store = current_trace_span_store()
    if store is not None:
        store.writer.stop()


def trace_span_listener(
    *, operation_id: str, owner_user_uuid: str, attempt: int = 0
) -> SpanListener | None:
    """実行（ジョブ・ターン・MCP の要求）の span を表に書く listener（書き手が無ければ None）。"""

    store = current_trace_span_store()
    if store is None:
        return None
    return store.writer.listener(
        TraceSpanOwner(
            product=TRACE_PRODUCT,
            operation_id=operation_id,
            owner_user_uuid=owner_user_uuid or ANONYMOUS_TRACE_OWNER,
            attempt=max(0, attempt),
        )
    )


def trace_span_dropped(trace_id: str) -> int:
    """この process が書けなかった（捨てた）span の数（実行を確定するときに記録へ保存する）。"""

    store = current_trace_span_store()
    if store is None or not trace_id:
        return 0
    return store.writer.dropped(trace_id)


def flush_trace_spans() -> None:
    """待っている span をすぐ書く。

    同期の要求の終わりに呼ぶ（画面は応答の直後に記録の処理の経過を読む）。
    """

    store = current_trace_span_store()
    if store is not None:
        store.writer.flush()


def combined_listener(
    *listeners: SpanListener | None,
) -> SpanListener | None:
    """複数の listener を順に呼ぶ（None は飛ばす。1 つも無ければ None）。"""

    active = [listener for listener in listeners if listener is not None]
    if not active:
        return None
    if len(active) == 1:
        return active[0]

    def listen(update: SpanUpdate) -> None:
        for listener in active:
            listener(update)

    return listen


def trace_links(carrier: Mapping[str, str]) -> list[trace.Link]:
    """要求の span context を span link にする（表の親子にはしない。形の違う値は捨てる）。

    画面の要求で作ったジョブは、ブラウザが送った traceparent を親にせず新しい trace で実行し、
    要求との関係は export する trace の span link にだけ残す（共通の `traceparent_links`）。
    """
    return traceparent_links(validated_trace_context(carrier).get("traceparent"))


def span_details_of(update: SpanUpdate) -> dict[str, Any] | None:
    """span の工程の詳細（`SpanDisplay.details`。製品の記録に置く。表には入れない）。"""

    display = update.display
    if display is None or display.details is None:
        return None
    return display.details.model_dump(mode="json", exclude_none=True)


def read_trace_progress(
    *,
    target_id: str,
    operation_id: str,
    trace_id: str,
    owner_user_uuid: str,
    viewer_is_admin: bool,
    terminal: TraceTerminal | None,
    details: Mapping[str, Mapping[str, Any]] | None = None,
    dropped_spans: int = 0,
    legacy: bool = False,
) -> TraceProgressData:
    """記録の処理の経過（span の表の snapshot）。呼ぶ側は記録の判定を通した後に呼ぶ。

    - `owner_user_uuid` は記録の実行者（span の持ち主）。system_admin（`viewer_is_admin`）以外は
      その利用者の span だけを読む（system_admin が他の利用者の会話に送ったターンも、
      その会話を見られる
      持ち主に同じ span を返す）。
    - 返すのはこの実行（`operation_id`）の span だけ（同じ trace の Agent の span・
    別のジョブは入れない）。
    - trace id の無い記録: `legacy`（span の記録より前の形式の実行）
    なら終端だけを返す（画面は古い形式の
      案内を出す）。まだ実行が始まっていない記録は空。
    """

    if not trace_id:
        events = (
            trace_progress_events([], target_id=target_id, terminal=terminal)
            if legacy and terminal is not None
            else []
        )
        return TraceProgressData(events=parse_chat_progress_events(events))
    spans = []
    store = current_trace_span_store()
    if store is not None:
        try:
            with store.connection_factory() as connection:
                spans = fetch_trace_spans(
                    connection,
                    trace_id,
                    viewer_user_uuid=None
                    if viewer_is_admin
                    else (owner_user_uuid or ANONYMOUS_TRACE_OWNER),
                )
        except Exception as exc:
            logger.warning(
                "nl2sql_trace_spans_read_failed",
                extra={"target_id": target_id, "error_type": type(exc).__name__},
            )
            raise TraceSpanReadError(target_id) from exc
    own = [
        span
        for span in spans
        if span.product == TRACE_PRODUCT and span.operation_id == operation_id
    ]
    events = trace_progress_events(own, target_id=target_id, terminal=terminal, details=details)
    return TraceProgressData(
        events=parse_chat_progress_events(events), dropped_spans=max(0, dropped_spans)
    )


__all__ = [
    "ANONYMOUS_TRACE_OWNER",
    "TRACE_PRODUCT",
    "SpanDetails",
    "TraceSpanReadError",
    "TraceSpanStore",
    "TraceTerminal",
    "combined_listener",
    "current_trace_span_store",
    "flush_trace_spans",
    "install_trace_span_store",
    "read_trace_progress",
    "reset_trace_span_store",
    "span_details_of",
    "start_trace_span_store",
    "stop_trace_span_store",
    "trace_links",
    "trace_span_dropped",
    "trace_span_listener",
]
