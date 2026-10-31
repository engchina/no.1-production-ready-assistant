"""実 Oracle AI Database で、DB 管理の監査が表（migration 023）に残るか（#2264）。

通常 CI では実行しない。`NL2SQL_RUN_ORACLE_INTEGRATION=1` の明示指定時だけ、migration 023 の DDL を
一意な名前に置き換えた一時の表に作り（system schema の台帳・本物の監査の表には触れない）、
DB 管理チャットと管理 SQL を実行の実行を残して読み戻し、finally で一時の表と作った表を消す。
LLM は呼ばない（生成は通さず、実行の経路だけを確かめる）。資格情報は出力しない。
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values

from app.features.nl2sql.admin_audit_store import AdminAuditRecord, AdminAuditStore
from app.features.nl2sql.models import (
    DbAdminChatExecuteData,
    DbAdminChatTurn,
    DbAdminChatTurnExecuteRequest,
    DbAdminExecuteRequest,
)
from app.features.nl2sql.oracle_adapter import OracleNl2SqlAdapter
from app.features.nl2sql.service import Nl2SqlService, SchemaRefreshMutationSync
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.features.settings.system_schema import split_migration_sql
from app.security.request_actor import actor_scope
from app.settings import get_settings

pytestmark = pytest.mark.skipif(
    os.getenv("NL2SQL_RUN_ORACLE_INTEGRATION") != "1",
    reason="NL2SQL_RUN_ORACLE_INTEGRATION=1 の明示指定が必要です。",
)

_MIGRATION = Path(__file__).parents[1] / "migrations" / "023_admin_audit_log.sql"


@pytest.fixture
def live_settings() -> Iterator[Any]:
    """conftest が PLATFORM_ORACLE_USER=APP を強制するため、共通 .env の接続ユーザーへ戻す。"""
    settings = get_settings()
    env_user = str(
        dotenv_values(Path(__file__).resolve().parents[3] / "platform" / ".env").get(
            "PLATFORM_ORACLE_USER"
        )
        or os.environ.get("PLATFORM_ORACLE_USER", "")
    )
    original_user = settings.oracle_user
    if env_user:
        settings.oracle_user = env_user
    try:
        yield settings
    finally:
        settings.oracle_user = original_user


class _RemapCursor:
    def __init__(self, cursor: Any, prefix: str) -> None:
        self.cursor = cursor
        self.prefix = prefix

    def __getattr__(self, name: str) -> Any:
        return getattr(self.cursor, name)

    def execute(self, sql: str, *args: Any) -> Any:
        return self.cursor.execute(sql.replace("NL2SQL_ADMIN_AUDIT", self.prefix), *args)

    def __enter__(self) -> _RemapCursor:
        return self

    def __exit__(self, *_: Any) -> None:
        self.cursor.close()


class _RemapConnection:
    def __init__(self, connection: Any, prefix: str) -> None:
        self.connection = connection
        self.prefix = prefix

    def cursor(self) -> _RemapCursor:
        return _RemapCursor(self.connection.cursor(), self.prefix)

    def commit(self) -> None:
        self.connection.commit()


@pytest.fixture
def audit_store(live_settings: Any) -> Iterator[tuple[OracleNl2SqlAdapter, AdminAuditStore]]:
    """migration 023 の DDL を一意な名前の一時の表に作った store（終わったら消す）。"""

    adapter = OracleNl2SqlAdapter(live_settings)
    prefix = "T2264A_" + uuid.uuid4().hex[:8].upper()

    @contextmanager
    def connection() -> Iterator[Any]:
        with adapter.connection() as conn:
            yield _RemapConnection(conn, prefix)

    with adapter.connection() as conn, conn.cursor() as cursor:
        for statement in split_migration_sql(_MIGRATION.read_text(encoding="utf-8")):
            cursor.execute(statement.replace("NL2SQL_ADMIN_AUDIT", prefix))
    try:
        yield adapter, AdminAuditStore(connection)
    finally:
        with (
            adapter.connection() as conn,
            conn.cursor() as cursor,
            contextlib.suppress(Exception),
        ):
            cursor.execute(f"DROP TABLE {prefix}_LOG PURGE")  # nosec B608 - test-local name


def test_store_keeps_full_sql_text_as_clob_on_oracle(
    audit_store: tuple[OracleNl2SqlAdapter, AdminAuditStore],
) -> None:
    _adapter, store = audit_store
    long_sql = "SELECT 1 FROM DUAL\n-- " + "あ" * 40000
    store.append(
        AdminAuditRecord(
            source="db_admin_chat",
            operation="db_admin_chat_execute",
            actor_user_uuid="t2264-store",
            outcome="success",
            executed=True,
            statement_count=1,
            success_count=1,
            statement_types=["SELECT"],
            duration_ms=12,
            sql_text=long_sql,
            detail={"returned_rows": 1},
        )
    )
    [record] = store.list_recent()
    assert record.sql_text == long_sql
    assert record.statement_types == ["SELECT"]
    assert record.detail == {"returned_rows": 1}
    assert record.outcome == "success"


class _LiveService(Nl2SqlService):
    def __init__(self, adapter: OracleNl2SqlAdapter, store: AdminAuditStore) -> None:
        super().__init__(store=MemoryNl2SqlStore())
        self._oracle_adapter = adapter
        self._admin_audit_store = store

    def _use_oracle_runtime(self) -> bool:
        return True


_ACTOR = "t2264-actor"


def _turn(service: _LiveService, sql: str) -> tuple[str, str]:
    """DB 管理チャットのターン（サーバーの分類つき）を memory の会話に置く（生成は通さない）。"""

    conversation_id = str(uuid.uuid4())
    store = service._db_admin_chat_store
    store.create_conversation(conversation_id, owner_user_uuid=_ACTOR, title="質問")
    store.append_turn(
        conversation_id,
        DbAdminChatTurn(
            turn_id=conversation_id,
            question="質問",
            created_at="2026-10-18T00:00:00+00:00",
            generated_at="2026-10-18T00:00:00+00:00",
            plan=service._db_admin_chat_plan(sql),
        ),
    )
    return conversation_id, conversation_id


def _execute(
    service: _LiveService,
    ids: tuple[str, str],
    *,
    index: int | None = 1,
    confirmation: str = "",
) -> DbAdminChatExecuteData:
    return service.execute_db_admin_chat_turn(
        DbAdminChatTurnExecuteRequest(
            conversation_id=ids[0],
            turn_id=ids[1],
            mode="statement" if index is not None else "all",
            statement_index=index,
            confirmation=confirmation,
        ),
        actor=_ACTOR,
        all_users=True,
    )


def test_chat_and_admin_sql_executions_are_audited_in_the_same_table(
    audit_store: tuple[OracleNl2SqlAdapter, AdminAuditStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, store = audit_store
    service = _LiveService(adapter, store)
    monkeypatch.setattr(
        service,
        "_submit_schema_refresh_after_admin_mutation",
        lambda **_kwargs: SchemaRefreshMutationSync(job_id="t2264-refresh"),
    )
    table = f"T2264_{uuid.uuid4().hex[:8].upper()}"
    try:
        with actor_scope(_ACTOR, is_system_admin=True):
            # DB 管理チャットの SELECT は確認語なしで実行し、監査に残す。
            select = _execute(service, _turn(service, "SELECT 1 AS N FROM DUAL"))
            assert select.executed is True
            [outcome] = select.statements
            assert outcome.result is not None
            assert outcome.result.execution_context == "admin_control_plane"
            # SELECT 以外は確認語が無ければ実行しない（監査にも残さない）。
            create = _turn(service, f"CREATE TABLE {table} (ID NUMBER)")
            pending = _execute(service, create)
            assert pending.executed is False
            assert {item.status for item in pending.statements} == {"confirmation_required"}
            created = _execute(service, create, confirmation="ADMIN_EXECUTE")
            assert created.committed is True
            # 方針で止める文は実行せず、止めたことを残す。
            blocked = _execute(
                service,
                _turn(service, "ALTER SYSTEM SET OPEN_CURSORS = 300"),
                confirmation="ADMIN_EXECUTE",
            )
            assert blocked.executed is False
            # 管理 SQL を実行も同じ表に残す（管理 SQL の DROP は ADMIN_EXECUTE）。
            dropped = service._execute_db_admin_sql(
                DbAdminExecuteRequest(sql=f"DROP TABLE {table} PURGE", confirmation="ADMIN_EXECUTE")
            )
            assert dropped.committed is True

        records = sorted(store.list_recent(), key=lambda item: item.created_at)
        assert [(item.source, item.outcome) for item in records] == [
            ("db_admin_chat", "success"),
            ("db_admin_chat", "success"),
            ("db_admin_chat", "blocked"),
            ("admin_sql", "success"),
        ]
        assert all(item.actor_user_uuid == _ACTOR for item in records)
        assert records[0].statement_types == ["SELECT"]
        assert records[0].confirmation_given is False
        assert records[1].confirmation_given is True
        assert records[1].committed is True
        assert records[1].sql_text == f"CREATE TABLE {table} (ID NUMBER)"
        assert records[2].executed is False
        assert records[3].operation == "db_admin_execute"
    finally:
        # 既に消えていれば何もしない。
        with (
            adapter.connection() as conn,
            conn.cursor() as cursor,
            contextlib.suppress(Exception),
        ):
            cursor.execute(f"DROP TABLE {table} PURGE")  # nosec B608 - test-local name


def _count(adapter: OracleNl2SqlAdapter, table: str) -> int:
    with adapter.connection() as conn, conn.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) FROM {table}")  # nosec B608 - test-local name
        row = cursor.fetchone()
    return int(row[0]) if row else 0


def test_run_all_uses_one_transaction_and_stops_at_the_first_error(
    audit_store: tuple[OracleNl2SqlAdapter, AdminAuditStore],
) -> None:
    """「すべて実行」（#2333）: 1 つの接続で上から順に実行し、SELECT は commit 前の変更も読む。

    最初のエラーで止め、DML を rollback する。監査は実行した文ごとに 1 行（同じ batch_id）。
    """

    adapter, store = audit_store
    service = _LiveService(adapter, store)
    table = f"T2333_{uuid.uuid4().hex[:8].upper()}"
    with adapter.connection() as conn, conn.cursor() as cursor:
        cursor.execute(f"CREATE TABLE {table} (ID NUMBER)")  # nosec B608 - test-local name
    try:
        with actor_scope(_ACTOR, is_system_admin=True):
            # SELECT だけなら確認語なしで、文ごとの結果を返す。
            selects = _execute(
                service,
                _turn(service, "SELECT 1 AS A FROM DUAL;\nSELECT 2 AS B FROM DUAL;"),
                index=None,
            )
            assert [item.status for item in selects.statements] == ["succeeded", "succeeded"]
            assert [
                item.result.select_result.rows
                for item in selects.statements
                if item.result and item.result.select_result
            ] == [[{"A": 1}], [{"B": 2}]]
            assert selects.committed is False

            script = _turn(
                service,
                f"INSERT INTO {table} (ID) VALUES (1);\n"
                f"SELECT COUNT(*) AS N FROM {table};\n"
                "SELECT 3 AS C FROM DUAL;",
            )
            pending = _execute(service, script, index=None)
            assert pending.statements[0].status == "confirmation_required"
            done = _execute(service, script, index=None, confirmation="ADMIN_EXECUTE")
            assert [item.status for item in done.statements] == ["succeeded"] * 3
            counted = done.statements[1].result
            # 同じトランザクションの中なので、commit 前の INSERT が見える。
            assert counted is not None and counted.select_result is not None
            assert counted.select_result.rows == [{"N": 1}]
            assert done.committed is True
            assert _count(adapter, table) == 1

            failing = _turn(
                service,
                f"INSERT INTO {table} (ID) VALUES (2);\n"
                f"INSERT INTO {table} (ID) VALUES ('x');\n"
                f"SELECT COUNT(*) AS N FROM {table};",
            )
            stopped = _execute(service, failing, index=None, confirmation="ADMIN_EXECUTE")
            assert [item.status for item in stopped.statements] == [
                "succeeded",
                "failed",
                "not_run",
            ]
            assert stopped.stopped_at == 2
            assert stopped.rolled_back is True and stopped.committed is False
            failed = stopped.statements[1].result
            assert failed is not None and "ORA-01722" in failed.statements[0].error_message
            # 1 文目の INSERT は rollback された。
            assert _count(adapter, table) == 1

        by_batch: dict[str, list[AdminAuditRecord]] = {}
        for record in store.list_recent():
            by_batch.setdefault(str(record.detail.get("batch_id")), []).append(record)
        assert sorted(len(items) for items in by_batch.values()) == [2, 2, 3]
        assert sorted(item.outcome for item in by_batch[stopped.batch_id]) == [
            "failure",
            "success",
        ]
        assert all(
            item.detail["batch_mode"] == "all" for items in by_batch.values() for item in items
        )
    finally:
        with (
            adapter.connection() as conn,
            conn.cursor() as cursor,
            contextlib.suppress(Exception),
        ):
            cursor.execute(f"DROP TABLE {table} PURGE")  # nosec B608 - test-local name
