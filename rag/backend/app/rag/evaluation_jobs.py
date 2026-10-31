"""品質評価の job（非同期の実行・進捗・取り消し。#390）。

golden set の評価（`/run`）は、ケースごとに回答生成（最長
`rag_answer_timeout_seconds`）を行うため、同期の HTTP の待ちに収まらない。job にして、投入 →
状態の取得（進捗: 終わったケースの数 / 全体・今のケース・経過時間）→ 結果の流れにする。

実行と保存:
- job は投入を受けた backend のプロセスの中で `asyncio` の task として動く（取込 job のような
  別の worker には渡さない）。評価は request の文脈（利用者・対象範囲。監査にも使う）で検索する
  ため、投入した request の文脈をそのまま使う。query の本文は保存しない。
- 状態は Oracle の `rag_evaluation_jobs` に保存する（Gunicorn の複数の worker のどれに状態の取得・
  取り消しが届いても同じ結果にするため）。Oracle の接続設定が無い開発環境では、プロセスの中の
  memory に保存する。
- 実行中のプロセスは `EVALUATION_JOB_HEARTBEAT_SECONDS` ごとに heartbeat を DB の時刻で書く。
  プロセスが止まって heartbeat が `EVALUATION_JOB_STALE_SECONDS` を超えて途絶えた job は、状態の
  取得・投入のときに失敗にする（別のプロセスは引き継がない。評価はやり直せばよいため）。
- 取り消しは job の行を CANCELLED にする。実行中のプロセスは、次の heartbeat か進捗の更新で
  RUNNING でなくなったことを知り、評価の task を止める（同じプロセスならすぐに止める）。
- job 全体の時間の上限は、投入時の回答の回数（ケース × 往復）×
  `rag_answer_timeout_seconds` と `rag_evaluation_job_timeout_seconds`（最小値。既定 3600 秒）の
  大きいほうで、`RAG_EVALUATION_JOB_TIMEOUT_MAX_SECONDS`（86400 秒）を超えない（#1430。
  `evaluation_job_time_limit_seconds`）。評価はケースを 1 件ずつ回答するため、固定の上限では
  推論型のモデル（1 回の回答が数分）の評価が問の数に応じて打ち切られる。上限に達したら、
  実行中のケースを打ち切り、残りのケースは実行せずに失敗として記録して結果を返す（#383 と同じ）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import socket
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from pr_backend_core.bounded_calls import BoundedCallAborted

from app.clients.oracle import OracleClient
from app.config import RAG_EVALUATION_JOB_TIMEOUT_MAX_SECONDS, Settings
from app.rag.evaluation import EvaluationProgressCallback
from app.rag.request_context import current_audit_request_context
from app.schemas.evaluation import (
    EvaluationCase,
    EvaluationJob,
    EvaluationJobKind,
    EvaluationJobStatus,
    EvaluationMetrics,
)

logger = logging.getLogger(__name__)

# 実行中のプロセスが heartbeat を書く間隔（秒）。
EVALUATION_JOB_HEARTBEAT_SECONDS = 10.0
# heartbeat がこれを超えて途絶えた RUNNING の job は、プロセスが止まったとみなして失敗にする（秒）。
EVALUATION_JOB_STALE_SECONDS = 120.0
# 同時に実行する品質評価の job の数の上限（全プロセスの合計）。評価は LLM を何度も呼ぶため、
# 画面と nightly が同時に動く程度に抑える。
EVALUATION_JOB_MAX_RUNNING = 2
# 終わった job の保持期間（日）。0 は無期限で、自動では削除しない（実行の記録は残す。#3235）。
EVALUATION_JOB_RETENTION_DAYS = 0
# current_case_id の列（VARCHAR2(200 CHAR)）に収める。
_CASE_ID_MAX_CHARS = 200
# lease_owner の列（VARCHAR2(128)）に収める。
_WORKER_ID_HOST_MAX_CHARS = 80

EVALUATION_JOB_CANCELLED_MESSAGE = "利用者の操作で品質評価を取り消しました。"
EVALUATION_JOB_STALE_MESSAGE = (
    "品質評価を実行していたサービスが応答しなくなったため、評価を中断しました。"
    "もう一度実行してください。"
)
EVALUATION_JOB_SHUTDOWN_MESSAGE = (
    "サービスの停止・再起動で品質評価を中断しました。もう一度実行してください。"
)
EVALUATION_JOB_LIMIT_MESSAGE = (
    "実行中の品質評価が上限の {limit} 件に達しています。"
    "実行中の評価が終わってから、もう一度実行してください。"
)
EVALUATION_JOB_NOT_FOUND_MESSAGE = "品質評価の job が見つかりません。"
EVALUATION_JOB_ALREADY_FINISHED_MESSAGE = "この品質評価は既に終わっているため、取り消せません。"


def evaluation_answer_runs(cases: Iterable[EvaluationCase]) -> int:
    """評価で回答を作る回数。ケースごとに最初の質問と確認の質問への返答（#1284）を 1 回ずつ。"""
    return sum(1 + len(case.turns) for case in cases)


def evaluation_job_time_limit_seconds(settings: Settings, *, answer_runs: int) -> int:
    """評価 job の全体の時間の上限（秒。#1430）。

    回答の回数 × 1 回の回答の上限（`rag_answer_timeout_seconds`）にし、設定の
    `rag_evaluation_job_timeout_seconds` を最小値、`RAG_EVALUATION_JOB_TIMEOUT_MAX_SECONDS` を
    最大値にする。どの回答も上限まで掛かっても、各回答はそれぞれの上限で打ち切られて結果に残り、
    job の上限が先に残りのケースを打ち切らない。
    """
    proportional = math.ceil(max(answer_runs, 0) * settings.rag_answer_timeout_seconds)
    return min(
        RAG_EVALUATION_JOB_TIMEOUT_MAX_SECONDS,
        max(settings.rag_evaluation_job_timeout_seconds, proportional),
    )


def evaluation_job_failed_message(error: BaseException) -> str:
    """評価が例外で止まったときの文言。例外の本文（query などを含みうる）は出さない。

    1 文目は利用者の言葉で何が起きたかと対処にし、例外のクラス名は技術的な詳細として末尾の
    「エラー種別: …」に分ける（画面は「詳細」に畳む。UX 契約 messaging.md §10.3）。
    """
    if isinstance(error, BoundedCallAborted):
        # 権限・認証・モデルの設定の誤り・割り当ての使い切り（#2004）。直す所の案内を出し、
        # 例外の本文（「詳細」）は出さない。
        guidance = str(error).split(" 詳細: ", 1)[0]
        return f"品質評価を中断しました。{guidance} エラー種別: {error.kind.value}"
    return (
        "品質評価の実行中に予期しないエラーが発生したため、評価を中断しました。"
        "サーバーのログを確認して、もう一度実行してください。"
        f" エラー種別: {type(error).__name__}"
    )


class EvaluationJobNotFoundError(LookupError):
    """job が無いか、投入した利用者ではない。"""

    safe_for_user = True


class EvaluationJobStateError(RuntimeError):
    """終わった job を取り消そうとした。"""

    safe_for_user = True


class EvaluationJobLimitError(RuntimeError):
    """実行中の job の数が上限に達している。"""

    safe_for_user = True


@dataclass(frozen=True)
class EvaluationJobOutcome:
    """評価の結果（`EvaluationMetrics` の JSON）と artifact の id。

    artifact を保存できなかったときは id が None。
    """

    result: Mapping[str, Any]
    evaluation_run_id: str | None = None


EvaluationJobExecutor = Callable[[EvaluationProgressCallback], Awaitable[EvaluationJobOutcome]]


@dataclass
class EvaluationJobRecord:
    """保存する job の状態（query の本文は持たない）。"""

    job_id: str
    kind: EvaluationJobKind
    status: EvaluationJobStatus
    total_cases: int
    time_limit_seconds: int
    created_at: datetime
    tenant_id_hash: str | None = None
    user_id_hash: str | None = None
    completed_cases: int = 0
    current_case_id: str | None = None
    current_case_started_at: datetime | None = None
    lease_owner: str | None = None
    heartbeat_at: datetime | None = None
    error_message: str | None = None
    result: dict[str, Any] | None = field(default=None, repr=False)
    evaluation_run_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class EvaluationJobStore(Protocol):
    """job の状態の保存先（Oracle / memory）。"""

    async def create(self, record: EvaluationJobRecord) -> None: ...

    async def get(
        self, job_id: str, *, tenant_id_hash: str | None, user_id_hash: str | None
    ) -> EvaluationJobRecord | None: ...

    async def update_progress(
        self,
        job_id: str,
        *,
        lease_owner: str,
        completed_cases: int,
        current_case_id: str | None,
    ) -> bool: ...

    async def heartbeat(self, job_id: str, *, lease_owner: str) -> bool: ...

    async def finish(
        self,
        job_id: str,
        *,
        lease_owner: str,
        status: EvaluationJobStatus,
        result: Mapping[str, Any] | None,
        error_message: str | None,
        evaluation_run_id: str | None,
    ) -> bool: ...

    async def cancel(
        self,
        job_id: str,
        *,
        tenant_id_hash: str | None,
        user_id_hash: str | None,
        error_message: str,
    ) -> bool: ...

    async def fail_stale(
        self, *, stale_seconds: float, error_message: str, lease_owner: str | None = None
    ) -> int: ...

    async def count_running(self) -> int: ...

    async def purge(self, *, retention_days: int) -> int: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _owned(
    record: EvaluationJobRecord, tenant_id_hash: str | None, user_id_hash: str | None
) -> bool:
    return record.tenant_id_hash == tenant_id_hash and record.user_id_hash == user_id_hash


class MemoryEvaluationJobStore:
    """Oracle の接続設定が無い開発環境・テストの保存先（プロセスの中だけ）。"""

    def __init__(self, clock: Callable[[], datetime] = _utc_now) -> None:
        self._clock = clock
        self._jobs: dict[str, EvaluationJobRecord] = {}

    async def create(self, record: EvaluationJobRecord) -> None:
        now = self._clock()
        self._jobs[record.job_id] = replace(
            record, status="RUNNING", created_at=now, started_at=now, heartbeat_at=now
        )

    async def get(
        self, job_id: str, *, tenant_id_hash: str | None, user_id_hash: str | None
    ) -> EvaluationJobRecord | None:
        record = self._jobs.get(job_id)
        if record is None or not _owned(record, tenant_id_hash, user_id_hash):
            return None
        return replace(record)

    def _leased(self, job_id: str, lease_owner: str) -> EvaluationJobRecord | None:
        record = self._jobs.get(job_id)
        if record is None or record.status != "RUNNING" or record.lease_owner != lease_owner:
            return None
        return record

    async def update_progress(
        self,
        job_id: str,
        *,
        lease_owner: str,
        completed_cases: int,
        current_case_id: str | None,
    ) -> bool:
        record = self._leased(job_id, lease_owner)
        if record is None:
            return False
        now = self._clock()
        record.completed_cases = completed_cases
        record.current_case_id = current_case_id
        record.current_case_started_at = now if current_case_id is not None else None
        record.heartbeat_at = now
        return True

    async def heartbeat(self, job_id: str, *, lease_owner: str) -> bool:
        record = self._leased(job_id, lease_owner)
        if record is None:
            return False
        record.heartbeat_at = self._clock()
        return True

    def _close(
        self, record: EvaluationJobRecord, status: EvaluationJobStatus, message: str | None
    ) -> None:
        record.status = status
        record.error_message = message
        record.current_case_id = None
        record.current_case_started_at = None
        record.lease_owner = None
        record.finished_at = self._clock()

    async def finish(
        self,
        job_id: str,
        *,
        lease_owner: str,
        status: EvaluationJobStatus,
        result: Mapping[str, Any] | None,
        error_message: str | None,
        evaluation_run_id: str | None,
    ) -> bool:
        record = self._leased(job_id, lease_owner)
        if record is None:
            return False
        self._close(record, status, error_message)
        record.result = dict(result) if result is not None else None
        record.evaluation_run_id = evaluation_run_id
        if status == "SUCCEEDED":
            record.completed_cases = record.total_cases
        return True

    async def cancel(
        self,
        job_id: str,
        *,
        tenant_id_hash: str | None,
        user_id_hash: str | None,
        error_message: str,
    ) -> bool:
        record = self._jobs.get(job_id)
        if (
            record is None
            or record.status != "RUNNING"
            or not _owned(record, tenant_id_hash, user_id_hash)
        ):
            return False
        self._close(record, "CANCELLED", error_message)
        return True

    async def fail_stale(
        self, *, stale_seconds: float, error_message: str, lease_owner: str | None = None
    ) -> int:
        threshold = self._clock() - timedelta(seconds=stale_seconds)
        count = 0
        for record in self._jobs.values():
            if record.status != "RUNNING":
                continue
            if lease_owner is not None:
                stale = record.lease_owner == lease_owner
            else:
                stale = record.heartbeat_at is None or record.heartbeat_at < threshold
            if stale:
                self._close(record, "FAILED", error_message)
                count += 1
        return count

    async def count_running(self) -> int:
        return sum(1 for record in self._jobs.values() if record.status == "RUNNING")

    async def purge(self, *, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        threshold = self._clock() - timedelta(days=retention_days)
        expired = [
            job_id
            for job_id, record in self._jobs.items()
            if record.status != "RUNNING" and record.created_at < threshold
        ]
        for job_id in expired:
            del self._jobs[job_id]
        return len(expired)


class OracleEvaluationJobStore:
    """`rag_evaluation_jobs` に保存する（heartbeat・stale の判定は DB の時計）。"""

    def __init__(self, client: OracleClient | None = None) -> None:
        self._client = client or OracleClient()

    async def create(self, record: EvaluationJobRecord) -> None:
        await self._client.create_evaluation_job(
            {
                "job_id": record.job_id,
                "kind": record.kind,
                "tenant_id_hash": record.tenant_id_hash,
                "user_id_hash": record.user_id_hash,
                "total_cases": record.total_cases,
                "lease_owner": record.lease_owner,
                "time_limit_seconds": record.time_limit_seconds,
            }
        )

    async def get(
        self, job_id: str, *, tenant_id_hash: str | None, user_id_hash: str | None
    ) -> EvaluationJobRecord | None:
        row = await self._client.get_evaluation_job(
            job_id, tenant_id_hash=tenant_id_hash, user_id_hash=user_id_hash
        )
        if row is None:
            return None
        return _record_from_row(row, tenant_id_hash=tenant_id_hash, user_id_hash=user_id_hash)

    async def update_progress(
        self,
        job_id: str,
        *,
        lease_owner: str,
        completed_cases: int,
        current_case_id: str | None,
    ) -> bool:
        return await self._client.update_evaluation_job_progress(
            job_id,
            lease_owner=lease_owner,
            completed_cases=completed_cases,
            current_case_id=current_case_id,
        )

    async def heartbeat(self, job_id: str, *, lease_owner: str) -> bool:
        return await self._client.heartbeat_evaluation_job(job_id, lease_owner=lease_owner)

    async def finish(
        self,
        job_id: str,
        *,
        lease_owner: str,
        status: EvaluationJobStatus,
        result: Mapping[str, Any] | None,
        error_message: str | None,
        evaluation_run_id: str | None,
    ) -> bool:
        return await self._client.finish_evaluation_job(
            job_id,
            lease_owner=lease_owner,
            status=status,
            result=result,
            error_message=error_message,
            evaluation_run_id=evaluation_run_id,
        )

    async def cancel(
        self,
        job_id: str,
        *,
        tenant_id_hash: str | None,
        user_id_hash: str | None,
        error_message: str,
    ) -> bool:
        return await self._client.cancel_evaluation_job(
            job_id,
            tenant_id_hash=tenant_id_hash,
            user_id_hash=user_id_hash,
            error_message=error_message,
        )

    async def fail_stale(
        self, *, stale_seconds: float, error_message: str, lease_owner: str | None = None
    ) -> int:
        return await self._client.fail_stale_evaluation_jobs(
            stale_seconds=stale_seconds, error_message=error_message, lease_owner=lease_owner
        )

    async def count_running(self) -> int:
        return await self._client.count_running_evaluation_jobs()

    async def purge(self, *, retention_days: int) -> int:
        return await self._client.purge_evaluation_jobs(retention_days=retention_days)


def _optional_str(value: object) -> str | None:
    return str(value) if value is not None and value != "" else None


def _optional_datetime(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


_STATUSES: dict[str, EvaluationJobStatus] = {
    "RUNNING": "RUNNING",
    "SUCCEEDED": "SUCCEEDED",
    "FAILED": "FAILED",
    "CANCELLED": "CANCELLED",
}


def _record_from_row(
    row: Mapping[str, object], *, tenant_id_hash: str | None, user_id_hash: str | None
) -> EvaluationJobRecord:
    result = row.get("result_json")
    kind: EvaluationJobKind = "calibration" if row.get("kind") == "calibration" else "run"
    return EvaluationJobRecord(
        job_id=str(row["job_id"]),
        kind=kind,
        status=_STATUSES.get(str(row.get("status") or ""), "FAILED"),
        total_cases=int(str(row.get("total_cases") or 0)),
        completed_cases=int(str(row.get("completed_cases") or 0)),
        time_limit_seconds=int(str(row.get("time_limit_seconds") or 0)),
        created_at=_optional_datetime(row.get("created_at")) or _utc_now(),
        tenant_id_hash=tenant_id_hash,
        user_id_hash=user_id_hash,
        current_case_id=_optional_str(row.get("current_case_id")),
        current_case_started_at=_optional_datetime(row.get("current_case_started_at")),
        heartbeat_at=_optional_datetime(row.get("heartbeat_at")),
        error_message=_optional_str(row.get("error_message")),
        result=dict(result) if isinstance(result, Mapping) else None,
        evaluation_run_id=_optional_str(row.get("evaluation_run_id")),
        started_at=_optional_datetime(row.get("started_at")),
        finished_at=_optional_datetime(row.get("finished_at")),
    )


def evaluation_job_response(record: EvaluationJobRecord) -> EvaluationJob:
    """API の応答に変換する。結果は成功のときだけ返す。"""
    run_result: EvaluationMetrics | None = None
    if record.kind == "run" and record.status == "SUCCEEDED" and record.result is not None:
        run_result = EvaluationMetrics.model_validate(record.result)
    return EvaluationJob(
        job_id=record.job_id,
        kind=record.kind,
        status=record.status,
        total_cases=record.total_cases,
        completed_cases=record.completed_cases,
        current_case_id=record.current_case_id,
        current_case_started_at=record.current_case_started_at,
        time_limit_seconds=record.time_limit_seconds,
        error_message=record.error_message,
        created_at=record.created_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
        heartbeat_at=record.heartbeat_at,
        run_result=run_result,
    )


def new_evaluation_worker_id() -> str:
    """lease_owner に使う、backend のプロセスごとに一意な識別子（host:pid:乱数）。"""
    host = (socket.gethostname() or "backend")[:_WORKER_ID_HOST_MAX_CHARS]
    return f"{host}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


def _truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value if len(value) <= limit else value[:limit]


class EvaluationJobService:
    """品質評価の job の投入・状態の取得・取り消し・実行（プロセスの中の task）。"""

    def __init__(
        self,
        store: EvaluationJobStore | None = None,
        *,
        heartbeat_seconds: float = EVALUATION_JOB_HEARTBEAT_SECONDS,
        stale_seconds: float = EVALUATION_JOB_STALE_SECONDS,
        max_running: int = EVALUATION_JOB_MAX_RUNNING,
        worker_id: str | None = None,
    ) -> None:
        self._store = store
        self._memory_store: MemoryEvaluationJobStore | None = None
        self._heartbeat_seconds = heartbeat_seconds
        self._stale_seconds = stale_seconds
        self._max_running = max_running
        self._fixed_worker_id = worker_id
        self._worker_id: str | None = None
        self._worker_pid: int | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def worker_id(self) -> str:
        """このプロセスの lease_owner。

        fork（Gunicorn の preload など）した後は、プロセスごとに作り直す。
        """
        if self._fixed_worker_id is not None:
            return self._fixed_worker_id
        if self._worker_id is None or self._worker_pid != os.getpid():
            self._worker_id = new_evaluation_worker_id()
            self._worker_pid = os.getpid()
        return self._worker_id

    def _resolve_store(self) -> EvaluationJobStore:
        if self._store is not None:
            return self._store
        client = OracleClient()
        if client.is_connection_configured():
            return OracleEvaluationJobStore(client)
        if self._memory_store is None:
            self._memory_store = MemoryEvaluationJobStore()
        return self._memory_store

    @staticmethod
    def _owner() -> tuple[str | None, str | None]:
        context = current_audit_request_context()
        return context.tenant_id_hash, context.user_id_hash

    async def submit(
        self,
        *,
        kind: EvaluationJobKind,
        total_cases: int,
        time_limit_seconds: int,
        executor: EvaluationJobExecutor,
        job_id: str | None = None,
    ) -> EvaluationJob:
        """job を作り、このプロセスの task で実行を始める。

        task は呼び出し元（request）の文脈（利用者・対象範囲）を引き継ぐ。``job_id`` は、job の
        結果を別の表に書く呼び出し（判定の校正。#3578）が先に決めた ID。
        """
        store = self._resolve_store()
        await store.fail_stale(
            stale_seconds=self._stale_seconds, error_message=EVALUATION_JOB_STALE_MESSAGE
        )
        if EVALUATION_JOB_RETENTION_DAYS > 0:
            with contextlib.suppress(Exception):
                # 保持期間を過ぎた job の削除は補助。失敗しても投入は止めない。
                await store.purge(retention_days=EVALUATION_JOB_RETENTION_DAYS)
        if await store.count_running() >= self._max_running:
            raise EvaluationJobLimitError(
                EVALUATION_JOB_LIMIT_MESSAGE.format(limit=self._max_running)
            )
        tenant_id_hash, user_id_hash = self._owner()
        record = EvaluationJobRecord(
            job_id=job_id or uuid.uuid4().hex,
            kind=kind,
            status="RUNNING",
            total_cases=total_cases,
            time_limit_seconds=time_limit_seconds,
            created_at=_utc_now(),
            tenant_id_hash=tenant_id_hash,
            user_id_hash=user_id_hash,
            lease_owner=self.worker_id,
        )
        await store.create(record)
        logger.info(
            "evaluation_job_submitted",
            extra={"job_id": record.job_id, "kind": kind, "total_cases": total_cases},
        )
        task = asyncio.create_task(
            self._run(record.job_id, store, executor), name=f"rag-evaluation-{record.job_id[:8]}"
        )
        self._tasks[record.job_id] = task
        return await self.get(record.job_id)

    async def get(self, job_id: str) -> EvaluationJob:
        """投入した利用者の job を返す。heartbeat が途絶えた job は失敗にしてから返す。"""
        store = self._resolve_store()
        tenant_id_hash, user_id_hash = self._owner()
        record = await store.get(job_id, tenant_id_hash=tenant_id_hash, user_id_hash=user_id_hash)
        if record is None:
            raise EvaluationJobNotFoundError(EVALUATION_JOB_NOT_FOUND_MESSAGE)
        # 別のプロセスが実行している job は、そのプロセスが止まっていれば失敗にする。
        if (
            record.status == "RUNNING"
            and job_id not in self._tasks
            and await store.fail_stale(
                stale_seconds=self._stale_seconds, error_message=EVALUATION_JOB_STALE_MESSAGE
            )
        ):
            refreshed = await store.get(
                job_id, tenant_id_hash=tenant_id_hash, user_id_hash=user_id_hash
            )
            record = refreshed or record
        return evaluation_job_response(record)

    async def cancel(self, job_id: str) -> EvaluationJob:
        """投入した利用者の実行中の job を取り消す。取り消し済みならそのまま返す。"""
        store = self._resolve_store()
        tenant_id_hash, user_id_hash = self._owner()
        cancelled = await store.cancel(
            job_id,
            tenant_id_hash=tenant_id_hash,
            user_id_hash=user_id_hash,
            error_message=EVALUATION_JOB_CANCELLED_MESSAGE,
        )
        record = await store.get(job_id, tenant_id_hash=tenant_id_hash, user_id_hash=user_id_hash)
        if record is None:
            raise EvaluationJobNotFoundError(EVALUATION_JOB_NOT_FOUND_MESSAGE)
        if not cancelled and record.status != "CANCELLED":
            raise EvaluationJobStateError(EVALUATION_JOB_ALREADY_FINISHED_MESSAGE)
        task = self._tasks.get(job_id)
        if cancelled and task is not None:
            # 同じプロセスで実行中なら、すぐに止める（別のプロセスは heartbeat で止まる）。
            task.cancel()
        if cancelled:
            logger.info("evaluation_job_cancelled", extra={"job_id": job_id})
        return evaluation_job_response(record)

    async def shutdown(self) -> None:
        """backend の停止で、このプロセスの実行中の job を打ち切り、失敗にする。"""
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            with contextlib.suppress(Exception):
                await self._resolve_store().fail_stale(
                    stale_seconds=0.0,
                    error_message=EVALUATION_JOB_SHUTDOWN_MESSAGE,
                    lease_owner=self.worker_id,
                )

    async def wait_for(self, job_id: str) -> None:
        """テスト用: このプロセスの job の task が終わるまで待つ。"""
        task = self._tasks.get(job_id)
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(
        self,
        job_id: str,
        store: EvaluationJobStore,
        executor: EvaluationJobExecutor,
    ) -> None:
        run_task = asyncio.current_task()
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(job_id, store, run_task))

        async def progress(
            *,
            completed_cases: int,
            current_case_id: str | None,
        ) -> None:
            try:
                updated = await store.update_progress(
                    job_id,
                    lease_owner=self.worker_id,
                    completed_cases=completed_cases,
                    current_case_id=_truncate(current_case_id, _CASE_ID_MAX_CHARS),
                )
            except Exception as exc:  # noqa: BLE001 - 進捗の保存は補助。評価は続ける。
                logger.warning(
                    "evaluation_job_progress_failed",
                    extra={"job_id": job_id, "error_type": type(exc).__name__},
                )
                return
            if not updated:
                # 取り消し・中断で RUNNING でなくなった。評価を止める。
                raise asyncio.CancelledError

        try:
            outcome = await executor(progress)
        except asyncio.CancelledError:
            # 取り消し（行は CANCELLED 済み）か backend の停止（`shutdown` が失敗にする）。
            logger.info("evaluation_job_stopped", extra={"job_id": job_id})
        except Exception as exc:  # noqa: BLE001 - job の失敗として記録する
            logger.warning(
                "evaluation_job_failed",
                extra={"job_id": job_id, "error_type": type(exc).__name__},
                exc_info=True,
            )
            await self._finish(
                store,
                job_id,
                status="FAILED",
                result=None,
                error_message=evaluation_job_failed_message(exc),
                evaluation_run_id=None,
            )
        else:
            await self._finish(
                store,
                job_id,
                status="SUCCEEDED",
                result=outcome.result,
                error_message=None,
                evaluation_run_id=outcome.evaluation_run_id,
            )
        finally:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
            self._tasks.pop(job_id, None)

    async def _finish(
        self,
        store: EvaluationJobStore,
        job_id: str,
        *,
        status: EvaluationJobStatus,
        result: Mapping[str, Any] | None,
        error_message: str | None,
        evaluation_run_id: str | None,
    ) -> None:
        try:
            written = await store.finish(
                job_id,
                lease_owner=self.worker_id,
                status=status,
                result=result,
                error_message=error_message,
                evaluation_run_id=evaluation_run_id,
            )
        except Exception as exc:  # noqa: BLE001 - 書けなければ heartbeat の途絶で失敗になる
            logger.warning(
                "evaluation_job_finish_failed",
                extra={"job_id": job_id, "error_type": type(exc).__name__},
            )
            return
        logger.info(
            "evaluation_job_finished",
            extra={"job_id": job_id, "status": status, "written": written},
        )

    async def _heartbeat_loop(
        self,
        job_id: str,
        store: EvaluationJobStore,
        run_task: asyncio.Task[Any] | None,
    ) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            try:
                alive = await store.heartbeat(job_id, lease_owner=self.worker_id)
            except Exception as exc:  # noqa: BLE001 - 一時的な DB の失敗では止めない
                logger.warning(
                    "evaluation_job_heartbeat_failed",
                    extra={"job_id": job_id, "error_type": type(exc).__name__},
                )
                continue
            if not alive:
                # 取り消された（別のプロセスへの取り消しを含む）か、中断として失敗にされた。
                if run_task is not None:
                    run_task.cancel()
                return


_SERVICE = EvaluationJobService()


def get_evaluation_job_service() -> EvaluationJobService:
    """このプロセスの品質評価の job の service を返す。"""
    return _SERVICE


def set_evaluation_job_service(service: EvaluationJobService | None) -> EvaluationJobService:
    """テスト用に service を差し替える（None なら新しい service にする）。"""
    global _SERVICE
    _SERVICE = service or EvaluationJobService()
    return _SERVICE
