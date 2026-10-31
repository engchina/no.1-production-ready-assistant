"""チャットの回答の作成（HTTP の要求から切り離したタスク。#1175）。

回答の作成は、SSE の接続（`POST .../messages/stream`）の寿命に結び付けない。接続が切れても
作成を最後まで続け、利用者が再接続・再読込で回答を受け取れるようにする（ChatGPT・Claude の
再接続、Vercel AI SDK の resumable streams、OpenAI の background mode と同じ考え方）。

実行と保存（品質評価の job（#390。`app/rag/evaluation_jobs.py`）と同じ形）:
- 作成は送信を受けた backend のプロセスの中で `asyncio` の task として動く。task は送信の要求の
  文脈（利用者・対象範囲。監査にも使う）を引き継ぐ。取込の worker（別プロセスの batch）は
  使わない（チャットは低遅延と段階の配信が要るため）。
- 状態は会話のメッセージ（`rag_messages`）に保存する。`start` の前に ASSISTANT のメッセージ
  を `STREAMING` で作り、処理の経過の記録（`progress_json`。共通の span の表の trace id と工
  程の詳細。`app.rag.trace_store.TraceRecord`。#3226）・実行中のプロセス（`lease_owner`）・
  heartbeat（`heartbeat_at`）を書く。最終の回答・失敗・停止は同じメッセージを更新して保存す
  る。処理の経過の終端はメッセージの状態（正本）で、span は共通の表にある。
- SSE は task の event の記録（`ChatAnswerRun.events`）を購読するだけ。event には連番を付け、
  再購読（`Last-Event-ID`）では続きから送る。購読が切れても task は止めない。終わった run の
  記録は `CHAT_ANSWER_RETENTION_SECONDS` だけ残す（作成が終わった直後の再購読に応える）。
- 実行中のプロセスは `CHAT_ANSWER_HEARTBEAT_SECONDS` ごとに heartbeat を DB の時刻で書く。
  プロセスが止まって heartbeat が `CHAT_ANSWER_STALE_SECONDS` を超えて途絶えた `STREAMING`
  のメッセージは、会話の取得のときに失敗（中断）にする（別のプロセスは引き継がない）。
- 停止は明示の取消（`cancel`）だけ。同じプロセスなら task をすぐ止めてメッセージを
  `CANCELLED` にする。別のプロセスの task は、メッセージが `STREAMING` でなくなったことを
  次の heartbeat で知って止まる。接続の切断は取消ではない。
- 同じ利用者が同時に作成できる回答の数は `rag_chat_max_active_answers_per_user`（プロセスごと）。
  task 全体の時間は回答の上限（`rag_answer_timeout_seconds`）に余裕を足したもので打ち切る。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# 実行中のプロセスが heartbeat を書く間隔（秒）。
CHAT_ANSWER_HEARTBEAT_SECONDS = 15.0
# heartbeat がこれを超えて途絶えた `STREAMING` のメッセージは、プロセスが止まったとみなす（秒）。
CHAT_ANSWER_STALE_SECONDS = 60.0
# 終わった run の event の記録を残す時間（秒）。作成が終わった直後の再購読に応える。
CHAT_ANSWER_RETENTION_SECONDS = 180.0
# 開始の前に届いた取消を覚えておく時間（秒）。送信の直後（`start` の前）の停止に使う。
CHAT_ANSWER_EARLY_CANCEL_SECONDS = 300.0
# task 全体の時間の上限に足す余裕（秒）。回答の生成は `rag_answer_timeout_seconds` で打ち切る。
CHAT_ANSWER_TIMEOUT_MARGIN_SECONDS = 60.0
# lease_owner の列（VARCHAR2(128)）に収める。
_WORKER_ID_HOST_MAX_CHARS = 80

CHAT_ANSWER_CANCELLED_MESSAGE = "回答の作成を停止しました。"
CHAT_ANSWER_INTERRUPTED_MESSAGE = (
    "回答を作成していたサービスが停止・再起動したため、回答の作成を中断しました。"
    "もう一度送信してください。"
)
CHAT_ANSWER_LIMIT_MESSAGE = (
    "回答を作成中の質問が上限の {limit} 件に達しています。"
    "作成中の回答が終わってから、もう一度送信してください。"
)


class ChatAnswerProgress(Protocol):
    """作成中のメッセージの処理の経過の記録（`app.rag.trace_store.AnswerTrace`）。"""

    def record(self) -> dict[str, Any]: ...


class ChatAnswerStore(Protocol):
    """作成中の回答（`STREAMING` の ASSISTANT のメッセージ）の保存先（`OracleClient`）。"""

    async def update_chat_message_progress(
        self, message_id: str, *, lease_owner: str, progress: dict[str, Any]
    ) -> bool: ...

    async def heartbeat_chat_message(self, message_id: str, *, lease_owner: str) -> bool: ...

    async def close_streaming_chat_message(
        self,
        message_id: str,
        *,
        status: str,
        content: str,
        lease_owner: str | None = None,
        stale_seconds: float | None = None,
        scoped: bool = True,
        progress: dict[str, Any] | None = None,
    ) -> bool: ...


class ChatAnswerLimitError(RuntimeError):
    """同じ利用者の作成中の回答が上限に達した。"""


@dataclass(frozen=True, slots=True)
class ChatAnswerEvent:
    """SSE の 1 event（連番は `id:` になり、再購読の `Last-Event-ID` で続きを指す）。"""

    seq: int
    name: str
    payload: dict[str, Any]


@dataclass
class ChatAnswerRun:
    """1 つの質問（USER のメッセージ）に対する回答の作成（モデルごとに 1 つのメッセージ）。"""

    run_id: str
    conversation_id: str
    tenant_id_hash: str | None
    user_id_hash: str | None
    store: ChatAnswerStore
    # model_id → 作成中の ASSISTANT のメッセージの id。
    message_ids: dict[str, str] = field(default_factory=dict)
    # 作成中のメッセージの id → 処理の経過の記録（閉じるときに保存する。#3226）。
    progress: dict[str, ChatAnswerProgress] = field(default_factory=dict)
    events: list[ChatAnswerEvent] = field(default_factory=list)
    done: bool = False
    finished_at: float | None = None
    cancel_requested: bool = False
    # 最終の状態を保存した（または保存を試みた）メッセージの id。
    closed_message_ids: set[str] = field(default_factory=set)
    task: asyncio.Task[None] | None = None
    _wake: asyncio.Event = field(default_factory=asyncio.Event)

    def publish(self, name: str, payload: dict[str, Any]) -> ChatAnswerEvent:
        """event を記録し、購読している接続を起こす。"""
        event = ChatAnswerEvent(seq=len(self.events) + 1, name=name, payload=payload)
        self.events.append(event)
        self._notify()
        return event

    def mark_closed(self, message_id: str) -> None:
        self.closed_message_ids.add(message_id)

    def open_message_ids(self) -> dict[str, str]:
        """まだ最終の状態を保存していないメッセージ（model_id → message_id）。"""
        return {
            model_id: message_id
            for model_id, message_id in self.message_ids.items()
            if message_id not in self.closed_message_ids
        }

    def finish(self) -> None:
        self.done = True
        self.finished_at = time.monotonic()
        self._notify()

    def _notify(self) -> None:
        wake = self._wake
        self._wake = asyncio.Event()
        wake.set()

    async def subscribe(
        self, *, after: int = 0, heartbeat_seconds: float
    ) -> AsyncIterator[ChatAnswerEvent | None]:
        """`after` より後の event を順に返し、作成が終わるまで待つ。

        event の無い間は `heartbeat_seconds` ごとに None を返す（SSE のコメントにする）。
        購読をやめても（接続が切れても）作成は止めない。
        """
        index = max(0, after)
        while True:
            while index < len(self.events):
                event = self.events[index]
                index += 1
                yield event
            if self.done:
                return
            wake = self._wake
            try:
                await asyncio.wait_for(wake.wait(), timeout=heartbeat_seconds)
            except TimeoutError:
                yield None


type ChatAnswerExecutor = Callable[[ChatAnswerRun], Awaitable[None]]


def new_chat_answer_worker_id() -> str:
    """lease_owner に使う、backend のプロセスごとに一意な識別子（host:pid:乱数）。"""
    host = (socket.gethostname() or "backend")[:_WORKER_ID_HOST_MAX_CHARS]
    return f"{host}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


class ChatAnswerRunService:
    """回答の作成の task の開始・購読・取消・停止（プロセスの中）。"""

    def __init__(
        self,
        *,
        heartbeat_seconds: float = CHAT_ANSWER_HEARTBEAT_SECONDS,
        stale_seconds: float = CHAT_ANSWER_STALE_SECONDS,
        retention_seconds: float = CHAT_ANSWER_RETENTION_SECONDS,
        worker_id: str | None = None,
    ) -> None:
        self.heartbeat_seconds = heartbeat_seconds
        self.stale_seconds = stale_seconds
        self.retention_seconds = retention_seconds
        self._fixed_worker_id = worker_id
        self._worker_id: str | None = None
        self._worker_pid: int | None = None
        self._runs: dict[str, ChatAnswerRun] = {}
        # 開始の前に届いた取消（(run_id, 会話) → 届いた時刻）。会話に結び付け、別の会話の
        # 質問の ID を混ぜた取消をその会話の回答に当てない（#3302）。
        self._early_cancels: dict[tuple[str, str], float] = {}

    @property
    def worker_id(self) -> str:
        """このプロセスの lease_owner。fork した後はプロセスごとに作り直す。"""
        if self._fixed_worker_id is not None:
            return self._fixed_worker_id
        if self._worker_id is None or self._worker_pid != os.getpid():
            self._worker_id = new_chat_answer_worker_id()
            self._worker_pid = os.getpid()
        return self._worker_id

    def _purge(self) -> None:
        now = time.monotonic()
        for run_id, run in list(self._runs.items()):
            if (
                run.done
                and run.finished_at is not None
                and now - run.finished_at > self.retention_seconds
            ):
                self._runs.pop(run_id, None)
        for key, at in list(self._early_cancels.items()):
            if now - at > CHAT_ANSWER_EARLY_CANCEL_SECONDS:
                self._early_cancels.pop(key, None)

    def get(self, run_id: str | None) -> ChatAnswerRun | None:
        """このプロセスの run（作成中か、終わって間もないもの）。"""
        self._purge()
        if not run_id:
            return None
        return self._runs.get(run_id)

    def is_running(self, run_id: str | None) -> bool:
        run = self.get(run_id)
        return run is not None and not run.done

    def active_count(self, *, tenant_id_hash: str | None, user_id_hash: str | None) -> int:
        self._purge()
        return sum(
            1
            for run in self._runs.values()
            if not run.done
            and run.tenant_id_hash == tenant_id_hash
            and run.user_id_hash == user_id_hash
        )

    def ensure_capacity(
        self, *, tenant_id_hash: str | None, user_id_hash: str | None, limit: int
    ) -> None:
        """同じ利用者の作成中の回答が上限なら `ChatAnswerLimitError`。"""
        if self.active_count(tenant_id_hash=tenant_id_hash, user_id_hash=user_id_hash) >= limit:
            raise ChatAnswerLimitError(CHAT_ANSWER_LIMIT_MESSAGE.format(limit=limit))

    def register(
        self,
        *,
        run_id: str,
        conversation_id: str,
        tenant_id_hash: str | None,
        user_id_hash: str | None,
        store: ChatAnswerStore,
    ) -> ChatAnswerRun:
        """run を作って登録する（作成中のメッセージを保存する前に呼ぶ）。

        先に登録するのは、会話の取得（中断の判定）が、保存したばかりの `STREAMING` の
        メッセージを「このプロセスで動いていない」と誤って中断にしないため。
        """
        self._purge()
        run = ChatAnswerRun(
            run_id=run_id,
            conversation_id=conversation_id,
            tenant_id_hash=tenant_id_hash,
            user_id_hash=user_id_hash,
            store=store,
        )
        # 開始の前に届いた取消（送信の直後の停止）は、作成を始めずに停止として保存する。
        if self._early_cancels.pop((run_id, conversation_id), None) is not None:
            run.cancel_requested = True
        self._runs[run_id] = run
        return run

    def discard(self, run: ChatAnswerRun) -> None:
        """開始できなかった run を外す（作成中のメッセージの保存に失敗したときなど）。"""
        if self._runs.get(run.run_id) is run:
            self._runs.pop(run.run_id, None)

    def start(
        self, run: ChatAnswerRun, executor: ChatAnswerExecutor, *, timeout_seconds: float
    ) -> None:
        """作成の task を始める。task は呼び出し元（送信の要求）の文脈を引き継ぐ。"""
        run.task = asyncio.create_task(
            self._run(run, executor, timeout_seconds), name=f"rag-chat-answer-{run.run_id[:8]}"
        )

    def remember_early_cancel(self, run_id: str, conversation_id: str) -> None:
        """まだ始まっていない run への取消を覚えておく（同じ会話の run が始まったら停止にする）。

        取消を受けた会話に結び付ける（会話を開ける利用者が、別の会話の質問の ID を混ぜても、
        その会話の回答は止まらない。#3302）。
        """
        self._purge()
        self._early_cancels[(run_id, conversation_id)] = time.monotonic()

    async def cancel(self, run: ChatAnswerRun, *, wait_seconds: float = 10.0) -> None:
        """このプロセスの run を止め、作成中のメッセージを `CANCELLED` にするまで待つ。"""
        if run.done:
            return
        run.cancel_requested = True
        task = run.task
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError, Exception):
            await asyncio.wait_for(asyncio.shield(task), timeout=wait_seconds)

    async def shutdown(self) -> None:
        """backend の停止で、このプロセスの作成を打ち切り、中断として保存する。"""
        tasks = [run.task for run in self._runs.values() if run.task is not None and not run.done]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def wait_for(self, run_id: str) -> None:
        """テスト用: このプロセスの run の task が終わるまで待つ。"""
        run = self._runs.get(run_id)
        if run is not None and run.task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await run.task

    async def _run(
        self, run: ChatAnswerRun, executor: ChatAnswerExecutor, timeout_seconds: float
    ) -> None:
        run_task = asyncio.current_task()
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(run, run_task))
        cancelled = False
        try:
            if not run.cancel_requested:
                async with asyncio.timeout(timeout_seconds):
                    await executor(run)
        except asyncio.CancelledError:
            # 利用者の取消（`cancel`）・別のプロセスの取消（heartbeat で知る）・backend の停止。
            cancelled = True
            logger.info(
                "chat_answer_stopped",
                extra={"run_id": run.run_id, "cancel_requested": run.cancel_requested},
            )
        except Exception as exc:  # noqa: BLE001 - 作成中のメッセージを中断として保存する
            logger.warning(
                "chat_answer_failed",
                extra={"run_id": run.run_id, "error_type": type(exc).__name__},
                exc_info=True,
            )
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
            # 最終の状態を保存していないメッセージ（取消・停止・時間切れ・予期しない失敗）を閉じる。
            stopped = run.cancel_requested
            message = CHAT_ANSWER_CANCELLED_MESSAGE if stopped else CHAT_ANSWER_INTERRUPTED_MESSAGE
            for model_id, message_id in run.open_message_ids().items():
                run.mark_closed(message_id)
                # 処理の経過の記録（trace id・工程の詳細・書けなかった数）も一緒に保存する（
                # #3226）。
                progress = run.progress.get(message_id)
                with contextlib.suppress(Exception):
                    await asyncio.shield(
                        run.store.close_streaming_chat_message(
                            message_id,
                            status="CANCELLED" if stopped else "ERROR",
                            content=message,
                            lease_owner=self.worker_id,
                            scoped=False,
                            progress=progress.record() if progress is not None else None,
                        )
                    )
                run.publish(
                    "error",
                    {
                        "model_id": model_id,
                        "message_id": message_id,
                        "message": message,
                        "error_type": "Cancelled" if stopped else "Interrupted",
                        "cancelled": stopped,
                        "stage": None,
                    },
                )
            run.publish("all_done", {"conversation_id": run.conversation_id})
            run.finish()
        if cancelled and not run.cancel_requested and run_task is not None:
            # backend の停止など、外からの cancel は呼び出し元へ伝える。
            raise asyncio.CancelledError

    async def _heartbeat_loop(self, run: ChatAnswerRun, run_task: asyncio.Task[Any] | None) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            open_ids = list(run.open_message_ids().values())
            if not open_ids:
                return
            alive = False
            failed = False
            for message_id in open_ids:
                try:
                    alive = (
                        await run.store.heartbeat_chat_message(
                            message_id, lease_owner=self.worker_id
                        )
                        or alive
                    )
                except Exception as exc:  # noqa: BLE001 - 一時的な DB の失敗では止めない
                    failed = True
                    logger.warning(
                        "chat_answer_heartbeat_failed",
                        extra={"run_id": run.run_id, "error_type": type(exc).__name__},
                    )
            if not alive and not failed:
                # 別のプロセスへの取消か、中断として失敗にされた。作成を止める（メッセージの
                # 状態は書き換えない。閉じる更新は `STREAMING` の行だけに当たる）。
                run.cancel_requested = True
                if run_task is not None:
                    run_task.cancel()
                return


_SERVICE = ChatAnswerRunService()


def get_chat_answer_run_service() -> ChatAnswerRunService:
    """プロセスで 1 つの回答の作成の service。"""
    return _SERVICE
