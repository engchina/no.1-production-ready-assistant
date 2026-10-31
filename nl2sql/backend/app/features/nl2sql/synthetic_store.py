"""Oracle と memory の同一 CAS/幂等受理契約。DDL は migration 019。"""

from __future__ import annotations

import builtins
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from .synthetic_models import TERMINAL, SyntheticRun


class SyntheticConflict(RuntimeError):
    pass


class SyntheticStore:
    def __init__(self, connection: Callable[[], AbstractContextManager[Any]] | None = None):
        self.connection = connection
        self._lock = threading.RLock()
        self._runs: dict[str, SyntheticRun] = {}

    @staticmethod
    def decode(value: Any) -> SyntheticRun:
        if isinstance(value, dict):
            return SyntheticRun.model_validate(value)
        return SyntheticRun.model_validate_json(value.read() if hasattr(value, "read") else value)

    def list(
        self,
        context: str,
        actor: str | None = None,
        *,
        active: bool = False,
        all_records: bool = False,
    ) -> list[SyntheticRun]:
        if self.connection:
            with self.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT PAYLOAD FROM NL2SQL_SYNTHETIC_RUNS WHERE CONTEXT_ID=:ctx "  # nosec B608
                    "AND (:actor IS NULL OR ACTOR_ID=:actor) "
                    "AND (:active=0 OR STATUS IN ('pending','running','verifying','unknown')) "
                    "ORDER BY CREATED_AT DESC"
                    + ("" if active or all_records else " FETCH FIRST 100 ROWS ONLY"),
                    {"ctx": context, "actor": actor, "active": int(active)},
                )
                return [self.decode(row[0]) for row in cur.fetchall()]
        with self._lock:
            return sorted(
                [
                    r.model_copy(deep=True)
                    for r in self._runs.values()
                    if r.context_id == context
                    and (actor is None or r.actor_id == actor)
                    and (not active or r.status not in TERMINAL)
                ],
                key=lambda r: r.created_at,
                reverse=True,
            )[: None if active or all_records else 100]

    def get(self, run_id: str) -> SyntheticRun | None:
        if self.connection:
            with self.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT PAYLOAD FROM NL2SQL_SYNTHETIC_RUNS WHERE RUN_ID=:id", {"id": run_id}
                )
                row = cur.fetchone()
                return self.decode(row[0]) if row else None
        with self._lock:
            r = self._runs.get(run_id)
            return r.model_copy(deep=True) if r else None

    def list_cleanup_candidates(self, context: str) -> builtins.list[SyntheticRun]:
        """確認用データ（一時表）が残っている終端の記録（worker の後始末の対象）。

        生成の記録は消さない（#3235）ので、全件ではなく一時表の管理情報が残る記録だけを読む。
        """
        if self.connection:
            with self.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT PAYLOAD FROM NL2SQL_SYNTHETIC_RUNS WHERE CONTEXT_ID=:ctx "
                    "AND STATUS IN ('completed','partial','failed','no_data') "
                    "AND JSON_EXISTS(PAYLOAD, '$.staging.*') ORDER BY CREATED_AT",
                    {"ctx": context},
                )
                return [self.decode(row[0]) for row in cur.fetchall()]
        with self._lock:
            return sorted(
                [
                    r.model_copy(deep=True)
                    for r in self._runs.values()
                    if r.context_id == context and r.status in TERMINAL and r.staging
                ],
                key=lambda r: r.created_at,
            )

    def by_key(self, actor: str, context: str, key: str) -> SyntheticRun | None:
        if self.connection:
            with self.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT PAYLOAD FROM NL2SQL_SYNTHETIC_RUNS WHERE ACTOR_ID=:actor "
                    "AND CONTEXT_ID=:ctx AND IDEMPOTENCY_KEY=:key",
                    {"actor": actor, "ctx": context, "key": key},
                )
                row = cur.fetchone()
                return self.decode(row[0]) if row else None
        with self._lock:
            return next(
                (
                    r.model_copy(deep=True)
                    for r in self._runs.values()
                    if r.context_id == context and r.actor_id == actor and r.idempotency_key == key
                ),
                None,
            )

    def create(self, run: SyntheticRun) -> SyntheticRun:
        if self.connection:
            try:
                with self.connection() as conn, conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO NL2SQL_SYNTHETIC_RUNS "
                        "(RUN_ID,ACTOR_ID,CONTEXT_ID,IDEMPOTENCY_KEY,STATUS,"
                        "VERSION_NO,CREATED_AT,PAYLOAD) "
                        "VALUES (:id,:actor,:ctx,:key,:status,0,:created,:payload)",
                        {
                            "id": run.run_id,
                            "actor": run.actor_id,
                            "ctx": run.context_id,
                            "key": run.idempotency_key,
                            "status": run.status,
                            "created": run.created_at,
                            "payload": run.model_dump_json(),
                        },
                    )
                    conn.commit()
                    return run
            except Exception as exc:
                if "ORA-00001" not in str(exc):
                    raise
                existing = self.by_key(run.actor_id, run.context_id, run.idempotency_key)
                if existing and existing.request_hash == run.request_hash:
                    return existing
                raise SyntheticConflict("同じ生成番号または受付キーの条件が異なります。") from exc
        with self._lock:
            existing = self.by_key(run.actor_id, run.context_id, run.idempotency_key)
            if existing:
                if existing.request_hash != run.request_hash:
                    raise SyntheticConflict("同じ受付キーの条件が異なります。")
                return existing
            if run.run_id in self._runs:
                raise SyntheticConflict("同じ生成番号の記録が既に存在します。")
            self._runs[run.run_id] = run.model_copy(deep=True)
            return run

    def save(self, run: SyntheticRun) -> bool:
        updated = run.model_copy(update={"version": run.version + 1}, deep=True)
        if self.connection:
            with self.connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE NL2SQL_SYNTHETIC_RUNS SET PAYLOAD=:payload, STATUS=:status, "
                    "VERSION_NO=:next WHERE RUN_ID=:id AND VERSION_NO=:version",
                    {
                        "payload": updated.model_dump_json(),
                        "status": updated.status,
                        "next": updated.version,
                        "id": run.run_id,
                        "version": run.version,
                    },
                )
                saved = bool(cur.rowcount == 1)
                # 旧版が作成した表ロックは終端時に片付ける。新規受理では使用しない。
                if saved and updated.status in TERMINAL:
                    cur.execute(
                        "DELETE FROM NL2SQL_SYNTHETIC_LOCKS WHERE RUN_ID=:id", {"id": run.run_id}
                    )
                conn.commit()
                return saved
        with self._lock:
            current = self._runs.get(run.run_id)
            if not current or current.version != run.version:
                return False
            self._runs[run.run_id] = updated
            return True

    def transact(self, run_id: str, action: Callable[[Any, SyntheticRun], None]) -> SyntheticRun:
        """Serialize review mutations; target INSERTs and applied receipt commit atomically."""
        if self.connection:
            with self.connection() as conn, conn.cursor() as cur:
                try:
                    cur.execute(
                        "SELECT PAYLOAD FROM NL2SQL_SYNTHETIC_RUNS "
                        "WHERE RUN_ID=:id FOR UPDATE WAIT 5",
                        {"id": run_id},
                    )
                    row = cur.fetchone()
                    if not row:
                        raise SyntheticConflict("生成記録が見つかりません。")
                    run = self.decode(row[0])
                    action(conn, run)
                    run.version += 1
                    cur.execute(
                        "UPDATE NL2SQL_SYNTHETIC_RUNS SET PAYLOAD=:payload, VERSION_NO=:version "
                        "WHERE RUN_ID=:id",
                        {"payload": run.model_dump_json(), "version": run.version, "id": run_id},
                    )
                    conn.commit()
                    return run
                except Exception:
                    conn.rollback()
                    raise
        with self._lock:
            found = self.get(run_id)
            if found is None:
                raise SyntheticConflict("生成記録が見つかりません。")
            run = found
            action(None, run)
            run.version += 1
            self._runs[run_id] = run.model_copy(deep=True)
            return run
