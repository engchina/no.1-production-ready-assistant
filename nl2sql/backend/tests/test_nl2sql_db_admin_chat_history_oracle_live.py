"""実 Oracle AI Database で、DB 管理チャットの会話の履歴が表（migration 024）に残るか（#2319）。

通常 CI では実行しない。`NL2SQL_RUN_ORACLE_INTEGRATION=1` の明示指定時だけ、migration 024 の DDL を
一意な名前に置き換えた一時の表に作り（system schema の台帳・本物の会話の表には触れない）、会話と
ターンを保存して読み戻し、finally で一時の表を消す。LLM は決定論のスタブ（SQL の実行は実 Oracle）。
資格情報は出力しない。

後半は DB 管理チャットの列の検査（#2352）: アプリの接続（管理の接続。
`OracleNl2SqlAdapter.admin_sql_describer`）で、データ辞書のビューと動的性能ビューの列を解析だけで
調べ、存在しない列を `ORA-00904` で見つけることを確かめる（表は作らず、データも読まない）。
CI の Oracle の結合テスト（oracle-integration.yml）が実行する file なのでここに置く。
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values

from app.features.nl2sql.db_admin_chat_columns import check_statement, unknown_column_reason
from app.features.nl2sql.db_admin_chat_store import DbAdminChatStore
from app.features.nl2sql.models import (
    DbAdminChatGenerateRequest,
    DbAdminChatPlan,
    DbAdminChatTurn,
    DbAdminChatTurnExecuteRequest,
    SchemaCatalog,
)
from app.features.nl2sql.oracle_adapter import OracleNl2SqlAdapter
from app.features.nl2sql.service import Nl2SqlService
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.features.settings.system_schema import split_migration_sql
from app.security.request_actor import actor_scope
from app.settings import get_settings

pytestmark = pytest.mark.skipif(
    os.getenv("NL2SQL_RUN_ORACLE_INTEGRATION") != "1",
    reason="NL2SQL_RUN_ORACLE_INTEGRATION=1 の明示指定が必要です。",
)

_MIGRATION = Path(__file__).parents[1] / "migrations" / "024_db_admin_chat_conversations.sql"
_NAME = "NL2SQL_DB_ADMIN_CHAT"


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
        return self.cursor.execute(sql.replace(_NAME, self.prefix), *args)

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
def chat_store(live_settings: Any) -> Iterator[tuple[OracleNl2SqlAdapter, DbAdminChatStore]]:
    """migration 024 の DDL を一意な名前の一時の表に作った store（終わったら消す）。"""

    adapter = OracleNl2SqlAdapter(live_settings)
    prefix = "T2319_" + uuid.uuid4().hex[:8].upper()

    @contextmanager
    def connection() -> Iterator[Any]:
        with adapter.connection() as conn:
            yield _RemapConnection(conn, prefix)

    with adapter.connection() as conn, conn.cursor() as cursor:
        for statement in split_migration_sql(_MIGRATION.read_text(encoding="utf-8")):
            cursor.execute(statement.replace(_NAME, prefix))
    try:
        yield adapter, DbAdminChatStore(connection)
    finally:
        with adapter.connection() as conn, conn.cursor() as cursor:
            for table in (f"{prefix}_TURNS", f"{prefix}S"):
                with contextlib.suppress(Exception):
                    cursor.execute(f"DROP TABLE {table} CASCADE CONSTRAINTS PURGE")  # nosec B608


def _turn(turn_id: str, question: str, sql: str) -> DbAdminChatTurn:
    return DbAdminChatTurn(
        turn_id=turn_id,
        question=question,
        created_at="2026-10-18T00:00:00+00:00",
        generated_at="2026-10-18T00:00:01+00:00",
        plan=DbAdminChatPlan(sql=sql),
    )


def test_store_round_trips_conversations_and_turns_on_oracle(
    chat_store: tuple[OracleNl2SqlAdapter, DbAdminChatStore],
) -> None:
    _adapter, store = chat_store
    first = store.create_conversation("c-1", owner_user_uuid="u-1", title="会話 1")
    store.create_conversation("c-2", owner_user_uuid="u-2", title="会話 2")
    store.create_conversation("c-3", owner_user_uuid="u-1", title="会話 3")
    assert first.actor_user_uuid == "u-1" and first.created_at

    # SQL の全文は 32KB を超えることがあるので CLOB で渡す。
    long_sql = "SELECT 1 FROM DUAL\n-- " + "あ" * 40000
    store.append_turn("c-1", _turn("t-1", "最初の質問", long_sql))
    store.append_turn(
        "c-1",
        _turn("t-2", "続き", "SELECT 2 FROM DUAL").model_copy(update={"sender_user_uuid": "a"}),
    )
    turns = store.list_turns("c-1")
    assert [turn.turn_id for turn in turns] == ["t-1", "t-2"]
    assert turns[0].plan.sql == long_sql
    assert [turn.sender_user_uuid for turn in turns] == [None, "a"]

    store.update_turn("c-1", turns[1].model_copy(update={"revision": 1, "corrected": True}))
    updated = store.get_turn("c-1", "t-2")
    assert updated is not None and updated.revision == 1 and updated.corrected is True
    assert updated.sender_user_uuid == "a"
    with pytest.raises(LookupError):
        store.update_turn("c-1", _turn("missing", "x", "SELECT 3 FROM DUAL"))

    mine, total = store.list_conversations(owner_user_uuid="u-1", offset=0, limit=1)
    assert total == 2 and [item.id for item in mine] == ["c-3"]
    rest, _ = store.list_conversations(owner_user_uuid="u-1", offset=1, limit=1)
    assert [item.id for item in rest] == ["c-1"]
    everyone, total_all = store.list_conversations(owner_user_uuid=None, offset=0, limit=10)
    assert total_all == 3 and [item.id for item in everyone] == ["c-3", "c-2", "c-1"]

    renamed = store.rename_conversation("c-1", "新しい名前")
    assert renamed is not None and renamed.title == "新しい名前"
    # 会話を消すとターンも消える（ON DELETE CASCADE）。
    assert store.delete_conversation("c-1") is True
    assert store.get_conversation("c-1") is None
    assert store.list_turns("c-1") == []
    assert store.delete_conversation("c-1") is False


class _FakeEnterpriseAi:
    def __init__(self, sql: str) -> None:
        self.sql = sql

    def is_configured(self) -> bool:
        return True

    def model_id(self) -> str:
        return "test-text-model"

    def generate(self, **_kwargs: Any) -> str:
        return json.dumps({"sql": self.sql, "explanation": "説明"}, ensure_ascii=False)


class _LiveService(Nl2SqlService):
    def __init__(self, adapter: OracleNl2SqlAdapter, store: DbAdminChatStore) -> None:
        super().__init__(store=MemoryNl2SqlStore())
        self._oracle_adapter = adapter
        self._db_admin_chat_store = store
        self._enterprise_ai_client = _FakeEnterpriseAi(
            "SELECT 1 AS N FROM DUAL;\nSELECT 2 AS M FROM DUAL;"
        )

    def _use_oracle_runtime(self) -> bool:
        return True

    def get_catalog(self, *_args: Any, **_kwargs: Any) -> SchemaCatalog:
        return SchemaCatalog(refreshed_at="2026-10-18T00:00:00+00:00", tables=[])


def test_sent_turn_and_execution_summary_are_saved_on_oracle(
    chat_store: tuple[OracleNl2SqlAdapter, DbAdminChatStore],
) -> None:
    adapter, store = chat_store
    service = _LiveService(adapter, store)
    turn_id = uuid.uuid4()
    with actor_scope("t2319-actor", is_system_admin=True):
        sent = service.send_db_admin_chat_question(
            DbAdminChatGenerateRequest(question="DUAL を読む", client_turn_id=turn_id),
            actor="t2319-actor",
            all_users=True,
        )
        assert sent is not None
        # 1 文ずつの実行と「すべて実行」（#2333）。
        single = service.execute_db_admin_chat_turn(
            DbAdminChatTurnExecuteRequest(
                conversation_id=sent.conversation.id, turn_id=sent.turn.turn_id, statement_index=2
            ),
            actor="t2319-actor",
            all_users=True,
        )
        assert single.executed is True
        result = single.statements[0].result
        assert result is not None and result.execution_context == "admin_control_plane"
        executed = service.execute_db_admin_chat_turn(
            DbAdminChatTurnExecuteRequest(
                conversation_id=sent.conversation.id, turn_id=sent.turn.turn_id, mode="all"
            ),
            actor="t2319-actor",
            all_users=True,
        )
    assert [item.status for item in executed.statements] == ["succeeded", "succeeded"]
    data = service.get_db_admin_chat(sent.conversation.id, actor="t2319-actor", all_users=True)
    [turn] = data.turns
    assert turn.turn_id == str(turn_id)
    assert turn.plan.read_only is True
    assert [(item.index, item.status, item.mode) for item in turn.statement_executions] == [
        (1, "succeeded", "all"),
        (2, "succeeded", "all"),
    ]
    assert all(item.row_count == 1 for item in turn.statement_executions)
    # 結果の行は保存しない。
    assert all(
        item.result is not None and item.result.select_result is None
        for item in turn.statement_executions
    )
    assert turn.last_run_all is not None and turn.last_run_all.batch_id == executed.batch_id


# ---- 列の検査（#2352）----

# 権限によって読めるかが変わる動的性能ビュー（CI の Oracle AI Database Free の APP は
# V$VECTOR_GRAPH_INDEX を grant される。ADB の ADMIN はどれも読める）。
_DYNAMIC_PERFORMANCE_VIEWS = ("V$LOCK", "V$SESSION", "V$VERSION", "V$VECTOR_GRAPH_INDEX")


def test_describes_a_dictionary_view_without_reading_data(live_settings: Any) -> None:
    adapter = OracleNl2SqlAdapter(live_settings)
    with adapter.admin_sql_describer() as describer:
        columns = describer.describe("USER_ROLE_PRIVS")
        assert describer.available is True
        assert columns is not None
        assert {"USERNAME", "GRANTED_ROLE"} <= set(columns)
        assert "GRANTEE" not in columns
        all_columns = describer.describe("SYS.ALL_TAB_COLUMNS")
        assert all_columns is not None
        assert {"OWNER", "TABLE_NAME", "COLUMN_NAME"} <= set(all_columns)
        # 存在しない object は調べられない（例外にしない）。
        assert describer.describe("NO_SUCH_VIEW_2352") is None


def test_describes_a_dynamic_performance_view(live_settings: Any) -> None:
    adapter = OracleNl2SqlAdapter(live_settings)
    with adapter.admin_sql_describer() as describer:
        described = {
            name: columns
            for name in _DYNAMIC_PERFORMANCE_VIEWS
            if (columns := describer.describe(name)) is not None
        }
    assert described, "アプリの接続で読める動的性能ビューがありません。"
    if "V$LOCK" in described:
        assert {"SID", "TYPE", "LMODE", "REQUEST"} <= set(described["V$LOCK"])
        assert "MODE_HELD" not in described["V$LOCK"]


def test_finds_unknown_columns_with_the_oracle_parse(live_settings: Any) -> None:
    adapter = OracleNl2SqlAdapter(live_settings)
    with adapter.admin_sql_describer() as describer:
        invalid = check_statement(
            "SELECT USERNAME, GRANTEE FROM USER_ROLE_PRIVS", describer, select_only=True
        )
        valid = check_statement(
            "SELECT USERNAME, GRANTED_ROLE FROM USER_ROLE_PRIVS FETCH FIRST 1 ROWS ONLY",
            describer,
            select_only=True,
        )
        missing = check_statement("SELECT A FROM NO_SUCH_VIEW_2352", describer, select_only=True)
    assert invalid is not None and invalid.invalid is True
    assert "ORA-00904" in str(invalid.oracle_error)
    assert [item.column for item in invalid.unknown] == ["GRANTEE"]
    assert unknown_column_reason(invalid).startswith(
        "列が存在しません: GRANTEE（USER_ROLE_PRIVS の列: "
    )
    assert valid is not None and valid.invalid is False and valid.oracle_error == ""
    assert missing is not None and missing.invalid is False
    assert missing.undescribed == ["NO_SUCH_VIEW_2352"]
    assert missing.missing_object is True


class _ColumnCheckService(Nl2SqlService):
    def __init__(self, adapter: OracleNl2SqlAdapter) -> None:
        super().__init__(store=MemoryNl2SqlStore())
        self._oracle_adapter = adapter

    def _use_oracle_runtime(self) -> bool:
        return True

    def _current_schema_owner(self) -> str:
        return str(get_settings().oracle_user or "").upper()


def test_plan_blocks_unknown_columns_on_the_app_connection(live_settings: Any) -> None:
    service = _ColumnCheckService(OracleNl2SqlAdapter(live_settings))
    plan = service._db_admin_chat_plan(
        "SELECT USERNAME, GRANTEE FROM USER_ROLE_PRIVS;\nSELECT USERNAME FROM USER_ROLE_PRIVS"
    )
    first, second = plan.statements
    assert first.unknown_columns == ["GRANTEE"]
    assert first.blocked_reason.startswith("列が存在しません: GRANTEE")
    assert second.blocked_reason == "" and second.unknown_columns == []
