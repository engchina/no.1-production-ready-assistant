"""明示 opt-in: 一時メタデータ表のみで Oracle CAS/rollback/CLOB 契約を検証する。

保存の往復は Oracle AI Database Free でも動くので、CI の Oracle integration で実行する。
ADB だけにある DBA_LOAD_OPERATIONS を読む `capture_session` は、別の opt-in
（NL2SQL_SYNTHETIC_ADB_LIVE_TEST）で確かめる。
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.features.nl2sql.oracle_adapter import OracleNl2SqlAdapter
from app.features.nl2sql.synthetic_models import SyntheticRun, SyntheticTarget
from app.features.nl2sql.synthetic_oracle import capture_session
from app.features.nl2sql.synthetic_store import SyntheticConflict, SyntheticStore
from app.features.settings.system_schema import split_migration_sql
from app.settings import Settings


def _live_adapter() -> OracleNl2SqlAdapter:
    # conftest の APP 上書きを使わず、ローカル接続設定を明示的に読込む。
    from dotenv import dotenv_values

    # 接続設定は共通 .env（platform/.env の PLATFORM_ORACLE_*。#211）
    env = dotenv_values(Path(__file__).parents[3] / "platform" / ".env")
    connection_settings: dict[str, Any] = {
        key.removeprefix("PLATFORM_").lower(): value
        for key, value in env.items()
        if key.startswith("PLATFORM_ORACLE_")
        and key.removeprefix("PLATFORM_").lower() in Settings.model_fields
        and value is not None
    }
    return OracleNl2SqlAdapter(Settings(_env_file=None, **connection_settings))


@pytest.mark.skipif(
    os.environ.get("NL2SQL_SYNTHETIC_ADB_LIVE_TEST") != "1", reason="isolated ADB opt-in"
)
def test_capture_session_reads_adb_load_operations() -> None:
    with _live_adapter().connection() as conn:
        assert capture_session(conn)["sid"] > 0


@pytest.mark.skipif(
    os.environ.get("NL2SQL_SYNTHETIC_LIVE_TEST") != "1", reason="isolated Oracle opt-in"
)
def test_oracle_persistence_roundtrip_and_conflict_rollback() -> None:
    adapter = _live_adapter()
    prefix = "NL2SQL_T" + uuid4().hex[:8].upper()

    def remap(sql: Any) -> Any:
        return sql.replace("NL2SQL_SYNTHETIC", prefix)

    class Cursor:
        def __init__(self, cursor: Any) -> None:
            self.cursor = cursor

        def __getattr__(self, name: Any) -> Any:
            return getattr(self.cursor, name)

        def execute(self, sql: Any, *args: Any) -> Any:
            return self.cursor.execute(remap(sql), *args)

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *_: Any) -> Any:
            self.cursor.close()

    class Connection:
        def __init__(self, connection: Any) -> None:
            self.connection = connection

        def cursor(self) -> Any:
            return Cursor(self.connection.cursor())

        def commit(self) -> Any:
            self.connection.commit()

    @contextmanager
    def connection() -> Iterator[Any]:
        with adapter.connection() as conn:
            yield Connection(conn)

    ddl = Path(__file__).parents[1] / "migrations/019_synthetic_runs.sql"
    created = []
    try:
        with adapter.connection() as conn, conn.cursor() as cur:
            for sql in split_migration_sql(ddl.read_text()):
                cur.execute(remap(sql))
                if sql.startswith("CREATE TABLE"):
                    created.append(remap(sql.split()[2]))
        store: Any = SyntheticStore(connection)
        value = SyntheticRun(
            run_id=str(uuid4()),
            actor_id="test",
            context_id="test",
            idempotency_key="key",
            request_hash="hash",
            request={"prompt": "日" * 50000},
            targets=[SyntheticTarget(table_name="APP.FIXTURE_ONLY", requested_rows=1)],
        )
        store.create(value)
        assert store.get(value.run_id).request == value.request
        assert store.by_key("test", "test", "key").run_id == value.run_id
        # 旧版の表 lock が残っていても新しい受付を阻害しない。
        with connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO NL2SQL_SYNTHETIC_LOCKS (CONTEXT_ID,TARGET_NAME,RUN_ID) "
                "VALUES (:ctx,:target,:id)",
                {"ctx": "test", "target": "APP.FIXTURE_ONLY", "id": value.run_id},
            )
            conn.commit()
        independent = value.model_copy(
            update={"run_id": str(uuid4()), "idempotency_key": "another"}
        )
        assert store.create(independent).run_id == independent.run_id
        replay = value.model_copy(update={"run_id": str(uuid4())})
        assert store.create(replay).run_id == value.run_id
        assert store.get(replay.run_id) is None
        conflict = value.model_copy(update={"run_id": str(uuid4()), "request_hash": "changed"})
        with pytest.raises(SyntheticConflict):
            store.create(conflict)
        assert store.get(conflict.run_id) is None
        value.status = "completed"
        assert store.save(value)
        assert not store.save(value)  # compare-and-swap fencing
        assert store.get(independent.run_id).status == "pending"
        assert len(store.list("test")) == 2
        with connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM NL2SQL_SYNTHETIC_LOCKS")
            assert cur.fetchone()[0] == 0
        at = datetime.now(UTC)
        old = (at - timedelta(days=3)).isoformat()
        cutoff = (at - timedelta(hours=24)).isoformat()
        staging = {"APP.T": {"name": "APP.NL2SQL_SP_FIXTURE", "checksum": "digest", "count": 1}}
        for name, status, finished, context, stage in [
            ("expired", "completed", old, "test", staging),
            ("boundary", "failed", cutoff, "test", {}),
            ("recent-finish", "partial", at.isoformat(), "test", {}),
            ("unknown", "unknown", None, "test", staging),
            ("other", "no_data", old, "other", staging),
        ]:
            store.create(
                value.model_copy(
                    update={
                        "run_id": name,
                        "idempotency_key": name,
                        "status": status,
                        "created_at": old,
                        "finished_at": finished,
                        "context_id": context,
                        "staging": stage,
                    }
                )
            )
        # 生成の記録は期間で消さない（#3235）。後始末の対象は一時表の管理情報が残る終端の記録だけ。
        for name in ["expired", "boundary", "recent-finish", "unknown", "other"]:
            assert store.get(name) is not None
        assert store.by_key("test", "test", "expired") is not None
        assert [run.run_id for run in store.list_cleanup_candidates("test")] == ["expired"]
        assert store.get("expired").review_expired(at)
        assert not store.get("boundary").review_expired(at)
    finally:
        with adapter.connection() as conn, conn.cursor() as cur:
            for name in reversed(created):
                cur.execute(f"DROP TABLE {name} CASCADE CONSTRAINTS PURGE")  # nosec B608 - generated name
