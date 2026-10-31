"""DB 管理チャット（#2264）: 分類・方針で止める文・確認語・実行・監査・生成・分析。

LLM（OCI Enterprise AI）と Oracle は決定論のスタブで置き換える。表・列の名前は合成のもの。
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

import pytest
from pr_backend_core.chat_progress import fold_chat_progress_events
from trace_span_fake import TraceTable, trace_table  # noqa: F401 - fixture

from app.features.nl2sql.db_admin_chat import (
    forbidden_statement_error,
    select_context_tables,
    single_object_confirmation_target,
)
from app.features.nl2sql.models import (
    DbAdminChatAnalyzeRequest,
    DbAdminChatExecuteData,
    DbAdminChatGenerateRequest,
    DbAdminChatHistoryTurn,
    DbAdminChatStatementLastExecution,
    DbAdminChatTurn,
    DbAdminChatTurnExecuteRequest,
    QueryResults,
    SchemaCatalog,
    SchemaColumn,
    SchemaTable,
)
from app.features.nl2sql.service import (
    DbAdminChatGenerationError,
    Nl2SqlService,
    SchemaRefreshMutationSync,
    _db_admin_chat_join_statements,
    _mask_sql_literals_and_comments,
    _similarity_tokens,
    _split_sql_statements,
)
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.features.nl2sql.trace_spans import read_trace_progress
from app.security.request_actor import actor_scope


class _FakeDescriber:
    """列の検査（#2352）の合成の object の列。`objects` に無い object は調べられない。

    `oracle_parse=True` なら Oracle の解析の代わりに、静的に分かる存在しない列で ORA-00904 を返す
    （`parse_errors` に文ごとの応答を置ける）。`available=False` は接続できない構成。
    """

    def __init__(
        self,
        objects: dict[str, tuple[str, ...]] | None = None,
        *,
        available: bool = True,
        parse_errors: dict[str, str] | None = None,
    ) -> None:
        self.objects = objects or {}
        self._available = available
        self.parse_errors = parse_errors or {}
        self.described: list[str] = []
        self.parsed: list[str] = []

    @property
    def available(self) -> bool:
        return self._available

    def describe(self, reference_sql: str) -> tuple[str, ...] | None:
        self.described.append(reference_sql)
        if not self._available:
            return None
        return self.objects.get(reference_sql)

    def parse_error(self, sql: str) -> str | None:
        self.parsed.append(sql)
        if not self._available:
            return None
        return self.parse_errors.get(sql)


class _FakeAdapter:
    """管理の実行（更新系）と SELECT の呼び出しを記録する。"""

    def __init__(
        self,
        statement_results: list[dict[str, Any]] | None = None,
        *,
        script_fail_at: int | None = None,
        describer: _FakeDescriber | None = None,
    ) -> None:
        self.statement_results = statement_results or []
        self.statement_calls: list[tuple[list[str], bool]] = []
        self.select_calls: list[tuple[str, int | None, bool]] = []
        self.script_fail_at = script_fail_at
        self.script_calls: list[tuple[list[str], frozenset[int], int | None]] = []
        # 既定は接続できない describer（列を確かめない。列の検査のテストは object を渡す）。
        self.describer = describer or _FakeDescriber(available=False)

    @contextmanager
    def admin_sql_describer(self) -> Iterator[_FakeDescriber]:
        yield self.describer

    def execute_admin_script(
        self, statements: list[str], *, query_indexes: frozenset[int], max_rows: int | None
    ) -> list[dict[str, Any]]:
        """「すべて実行」: `script_fail_at` の文で失敗して止める（後ろの文は返さない）。"""
        self.script_calls.append((statements, query_indexes, max_rows))
        results: list[dict[str, Any]] = []
        for index, sql in enumerate(statements, start=1):
            kind = sql.split()[0].upper()
            if index == self.script_fail_at:
                results.append(
                    {
                        "index": index,
                        "statement_type": kind,
                        "status": "error",
                        "sql": sql,
                        "error_message": "ORA-01722: invalid number",
                    }
                )
                break
            item: dict[str, Any] = {
                "index": index,
                "statement_type": kind,
                "status": "success",
                "sql": sql,
                "row_count": 1,
            }
            if index in query_indexes:
                item["select_result"] = QueryResults(
                    columns=["N"], rows=[{"N": index}], total=1, returned_count=1
                )
            results.append(item)
        return results

    def execute_admin_statements(
        self,
        statements: list[str],
        *,
        atomic: bool = True,
        ignored_error_codes: frozenset[str] = frozenset(),
    ) -> list[dict[str, Any]]:
        del ignored_error_codes
        self.statement_calls.append((statements, atomic))
        return self.statement_results

    def execute_select(
        self, sql: str, max_rows: int | None, *, admin_control_plane: bool = False
    ) -> QueryResults:
        self.select_calls.append((sql, max_rows, admin_control_plane))
        return QueryResults(
            columns=["N"],
            rows=[{"N": 1}],
            total=1,
            execution_context="admin_control_plane",
        )


class _FakeEnterpriseAi:
    def __init__(self, responses: list[dict[str, Any]], *, configured: bool = True) -> None:
        self.responses = responses
        self.configured = configured
        self.calls: list[dict[str, Any]] = []

    def is_configured(self) -> bool:
        return self.configured

    def model_id(self) -> str:
        return "test-text-model"

    def generate(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return json.dumps(self.responses.pop(0), ensure_ascii=False)


class _Service(Nl2SqlService):
    def __init__(self, adapter: _FakeAdapter, *, oracle: bool = True) -> None:
        super().__init__(store=MemoryNl2SqlStore())
        self._oracle_adapter = adapter  # type: ignore[assignment]
        self._oracle = oracle

    def _use_oracle_runtime(self) -> bool:
        return self._oracle

    def _current_schema_owner(self) -> str:
        return "APP"


def _masked(sql: str) -> str:
    return _mask_sql_literals_and_comments(sql.strip())


# ---- 方針で止める文 ----


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER SYSTEM SET OPEN_CURSORS = 300",
        "alter system kill session '1,2'",
        "ALTER DATABASE OPEN",
        "ALTER PLUGGABLE DATABASE CLOSE",
        "CREATE PLUGGABLE DATABASE P1 ADMIN USER A IDENTIFIED BY X",
        "DROP PLUGGABLE DATABASE P1",
        "SHUTDOWN IMMEDIATE",
        "STARTUP",
        "DROP USER U1 CASCADE",
        "CREATE USER U1 IDENTIFIED BY X",
        "ALTER USER U1 IDENTIFIED BY X",
        "DROP TABLESPACE TS1",
        "NOAUDIT ALL",
        "ALTER SESSION SET CURRENT_SCHEMA = OTHER",
        "COMMIT",
        "ROLLBACK",
        "GRANT DBA TO U1",
        "GRANT SYSDBA TO U1",
        "GRANT ALL PRIVILEGES TO U1",
        "BEGIN EXECUTE IMMEDIATE 'DROP TABLE T1'; END;",
        "DECLARE c NUMBER; BEGIN c := DBMS_SQL.OPEN_CURSOR; END;",
        "SELECT UTL_HTTP.REQUEST('http://example.invalid') FROM DUAL",
        "BEGIN DBMS_SCHEDULER.CREATE_JOB(job_name => 'J1'); END;",
    ],
    ids=lambda sql: sql[:40],
)
def test_forbidden_statements_are_blocked(sql: str) -> None:
    assert forbidden_statement_error(_masked(sql))


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT BANNER_FULL FROM V$VERSION",
        "SELECT 'ALTER SYSTEM' AS LABEL FROM DUAL",
        "SELECT * FROM T1 WHERE NOTE = 'DROP USER'",
        "CREATE INDEX IX_T1 ON T1 (C1)",
        "ALTER TABLE T1 ADD (C2 NUMBER)",
        "ALTER DATABASE LINK L1 CONNECT TO U IDENTIFIED BY X",
        "GRANT SELECT ON T1 TO R1",
        "INSERT INTO T1 (C1) VALUES ('COMMIT')",
    ],
    ids=lambda sql: sql[:40],
)
def test_ordinary_statements_and_literals_are_not_blocked(sql: str) -> None:
    assert forbidden_statement_error(_masked(sql)) == ""


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("DROP TABLE app.orders_tmp PURGE", "APP.ORDERS_TMP"),
        ('DROP VIEW "MixedCase"', "MixedCase"),
        ("TRUNCATE TABLE T1", "T1"),
        ("DROP INDEX IX_T1", "IX_T1"),
        ("DELETE FROM T1", ""),
        ("CREATE TABLE T1 (ID NUMBER)", ""),
    ],
)
def test_single_object_confirmation_target(sql: str, expected: str) -> None:
    assert single_object_confirmation_target(sql, _masked(sql)) == expected


def test_context_tables_rank_names_over_columns() -> None:
    tables = [
        SchemaTable(
            table_name="ITEM_STOCK",
            logical_name="",
            owner="APP",
            columns=[SchemaColumn(column_name="QTY", logical_name="", data_type="NUMBER")],
        ),
        SchemaTable(
            table_name="OTHER",
            logical_name="",
            owner="APP",
            columns=[SchemaColumn(column_name="STOCK_NOTE", logical_name="", data_type="CHAR")],
        ),
        SchemaTable(table_name="UNRELATED", logical_name="", owner="APP"),
    ]
    picked = select_context_tables(tables, _similarity_tokens("STOCK の件数"), _similarity_tokens)
    assert [table.table_name for table in picked] == ["ITEM_STOCK"]
    assert select_context_tables(tables, set(), _similarity_tokens) == []


# ---- 分類（文ごと。#2333）----


def test_plan_select_is_read_only_and_allows_dynamic_performance_views() -> None:
    service = _Service(_FakeAdapter())
    plan = service._db_admin_chat_plan("SELECT BANNER_FULL FROM V$VERSION")
    assert plan.read_only is True
    assert plan.requires_confirmation is False
    assert plan.blocked is False
    joined = service._db_admin_chat_plan(
        "SELECT s.SID FROM V$SESSION s JOIN V$LOCKED_OBJECT l ON s.SID = l.SESSION_ID"
    )
    assert joined.blocked is False


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM NL2SQL_PROFILES",
        "SELECT * FROM PLATFORM_USERS",
        "SELECT * FROM SYS.OBJ$",
        "DELETE FROM V$SESSION",
        "DROP TABLE AGENT_RUNS",
        "ALTER SYSTEM FLUSH SHARED_POOL",
    ],
)
def test_plan_blocks_product_objects_internal_objects_and_policy(sql: str) -> None:
    plan = _Service(_FakeAdapter())._db_admin_chat_plan(sql)
    assert plan.blocked is True
    assert plan.blocked_reasons
    assert plan.statements[0].blocked_reason
    assert plan.statements[0].requires_confirmation is False


def test_plan_classifies_each_statement_by_risk() -> None:
    service = _Service(_FakeAdapter())
    selects = service._db_admin_chat_plan(
        "SELECT BANNER_FULL FROM V$VERSION;\nSELECT TABLESPACE_NAME FROM DBA_TABLESPACES;"
    )
    # 複数の SELECT も止めない（文ごとに確認語なしで実行でき、「すべて実行」も確認語なし）。
    assert selects.blocked is False
    assert selects.read_only is True
    assert selects.requires_confirmation is False
    assert [item.index for item in selects.statements] == [1, 2]
    assert all(item.select_only and not item.requires_confirmation for item in selects.statements)

    mixed = service._db_admin_chat_plan("SELECT 1 FROM DUAL; DELETE FROM T1; SELECT 2 FROM DUAL")
    assert mixed.blocked is False
    assert [item.requires_confirmation for item in mixed.statements] == [False, True, False]
    assert mixed.statements[1].confirmation_target == "ADMIN_EXECUTE"
    assert mixed.read_only is False
    assert mixed.requires_confirmation is True
    assert mixed.confirmation_target == "ADMIN_EXECUTE"

    # 「すべて実行」の確認語: 変える文が 1 つならその文の確認語、2 つ以上なら ADMIN_EXECUTE。
    drop = service._db_admin_chat_plan("SELECT 1 FROM DUAL; DROP TABLE T1 PURGE")
    assert drop.statements[1].confirmation_target == "T1"
    assert drop.confirmation_target == "T1"
    assert drop.implicit_commit is True
    script = service._db_admin_chat_plan("CREATE TABLE T1 (ID NUMBER);\nINSERT INTO T1 VALUES (1);")
    assert [item.statement_type for item in script.statements] == ["CREATE", "INSERT"]
    assert script.confirmation_target == "ADMIN_EXECUTE"
    assert script.implicit_commit is True


def test_plan_keeps_a_plsql_block_as_one_statement() -> None:
    plan = _Service(_FakeAdapter())._db_admin_chat_plan(
        "BEGIN\n  UPDATE T1 SET C1 = 0;\n  DELETE FROM T2;\nEND;\n/\nSELECT 1 FROM DUAL"
    )
    assert [item.statement_type for item in plan.statements] == ["PLSQL", "SELECT"]
    assert plan.statements[0].requires_confirmation is True


def test_plan_isolates_a_forbidden_statement() -> None:
    plan = _Service(_FakeAdapter())._db_admin_chat_plan(
        "SELECT 1 FROM DUAL; ALTER SYSTEM SET OPEN_CURSORS = 300; SELECT 2 FROM DUAL"
    )
    # 止めるのはその文だけ。ほかの文は実行できる。「すべて実行」はできない。
    assert [bool(item.blocked_reason) for item in plan.statements] == [False, True, False]
    assert plan.statements[0].select_only and plan.statements[2].select_only
    assert plan.blocked is True
    assert plan.requires_confirmation is False


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 FROM DUAL; SELECT 2 FROM DUAL",
        "BEGIN NULL; END;\n/\nSELECT 1 FROM DUAL -- 注\n;\nDELETE FROM T1",
        "CREATE OR REPLACE PROCEDURE P1 AS BEGIN NULL; END;\n/\nINSERT INTO T1 VALUES ('a;b')",
    ],
    ids=["selects", "plsql-and-comment", "procedure"],
)
def test_joined_statements_split_back_to_the_same_statements(sql: str) -> None:
    statements = _split_sql_statements(sql)
    assert _split_sql_statements(_db_admin_chat_join_statements(statements)) == statements


# ---- 実行（文ごと・すべて実行）と監査 ----

_TURN_CREATED_AT = "2026-10-18T00:00:00+00:00"


def _store_turn(service: _Service, sql: str, *, owner: str = "chat-user") -> tuple[str, str]:
    """会話とターン（サーバーの分類つき）を memory の保存先に置き、会話とターンの ID を返す。"""

    conversation_id = str(uuid.uuid4())
    store = service._db_admin_chat_store
    store.create_conversation(conversation_id, owner_user_uuid=owner, title="質問")
    store.append_turn(
        conversation_id,
        DbAdminChatTurn(
            turn_id=conversation_id,
            question="質問",
            created_at=_TURN_CREATED_AT,
            generated_at=_TURN_CREATED_AT,
            plan=service._db_admin_chat_plan(sql),
        ),
    )
    return conversation_id, conversation_id


def _execute(
    service: _Service,
    ids: tuple[str, str],
    *,
    index: int | None = 1,
    mode: Literal["statement", "all"] = "statement",
    confirmation: str = "",
    actor: str = "chat-user",
) -> DbAdminChatExecuteData:
    with actor_scope(actor, is_system_admin=False):
        return service.execute_db_admin_chat_turn(
            DbAdminChatTurnExecuteRequest(
                conversation_id=ids[0],
                turn_id=ids[1],
                mode=mode,
                statement_index=index if mode == "statement" else None,
                confirmation=confirmation,
                row_limit=50,
            ),
            actor=actor,
            all_users=False,
        )


def _saved(service: _Service, ids: tuple[str, str]) -> DbAdminChatTurn:
    turn = service._db_admin_chat_store.get_turn(*ids)
    assert turn is not None
    return turn


def test_execute_request_requires_a_target_for_each_mode() -> None:
    with pytest.raises(ValueError, match="statement_index"):
        DbAdminChatTurnExecuteRequest(conversation_id="c", turn_id="t")
    with pytest.raises(ValueError, match="statement_index"):
        DbAdminChatTurnExecuteRequest(
            conversation_id="c", turn_id="t", mode="all", statement_index=1
        )


def test_select_statement_executes_on_admin_connection_without_confirmation() -> None:
    adapter = _FakeAdapter()
    service = _Service(adapter)
    ids = _store_turn(service, "SELECT 1 AS N FROM DUAL; SELECT 2 AS N FROM DUAL")
    data = _execute(service, ids, index=2)
    assert data.mode == "statement"
    [outcome] = data.statements
    assert (outcome.index, outcome.status) == (2, "succeeded")
    assert outcome.result is not None
    assert outcome.result.execution_context == "admin_control_plane"
    assert outcome.result.select_result is not None
    # 結果の文の番号はターンの中の番号。
    assert outcome.result.statements[0].index == 2
    assert adapter.select_calls == [("SELECT 2 AS N FROM DUAL", 50, True)]
    [audit] = service._admin_audit_store.list_recent()
    assert audit.source == "db_admin_chat"
    assert audit.operation == "db_admin_chat_execute"
    assert audit.outcome == "success"
    assert audit.statement_types == ["SELECT"]
    assert audit.confirmation_given is False
    assert audit.actor_user_uuid == "chat-user"
    assert audit.sql_text == "SELECT 2 AS N FROM DUAL"
    assert audit.detail["returned_rows"] == 1
    assert audit.detail["batch_id"] == data.batch_id
    assert audit.detail["batch_mode"] == "statement"
    assert audit.detail["statement_index"] == 2
    assert audit.detail["turn_id"] == ids[1]
    # 文ごとの最後の実行の要約（行は残さない）。
    [saved] = _saved(service, ids).statement_executions
    assert (saved.index, saved.status, saved.row_count, saved.outcome) == (
        2,
        "succeeded",
        1,
        "成功（1 行）",
    )
    assert saved.result is not None and saved.result.select_result is None


def test_changing_statement_requires_its_confirmation_phrase() -> None:
    adapter = _FakeAdapter([{"index": 1, "statement_type": "DELETE", "status": "success"}])
    service = _Service(adapter)
    ids = _store_turn(service, "SELECT 1 FROM DUAL; DELETE FROM T1 WHERE C1 = 0")
    pending = _execute(service, ids, index=2)
    assert [item.status for item in pending.statements] == ["confirmation_required"]
    assert pending.executed is False
    assert "confirmation=ADMIN_EXECUTE" in pending.warnings[0]
    assert adapter.statement_calls == []
    # 確認語の待ちは監査にも要約にも残さない。
    assert service._admin_audit_store.list_recent() == []
    assert _saved(service, ids).statement_executions == []

    done = _execute(service, ids, index=2, confirmation="ADMIN_EXECUTE")
    assert done.committed is True
    assert done.statements[0].status == "succeeded"
    assert adapter.statement_calls == [(["DELETE FROM T1 WHERE C1 = 0"], True)]
    [audit] = service._admin_audit_store.list_recent()
    assert audit.confirmation_given is True
    assert audit.statement_types == ["DELETE"]
    [saved] = _saved(service, ids).statement_executions
    assert saved.confirmation_given is True and saved.status == "succeeded"


def test_single_object_drop_requires_the_object_name(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _FakeAdapter([{"index": 1, "statement_type": "DROP", "status": "success"}])
    service = _Service(adapter)
    monkeypatch.setattr(
        service,
        "_submit_schema_refresh_after_admin_mutation",
        lambda **_kwargs: SchemaRefreshMutationSync(job_id="refresh-1"),
    )
    ids = _store_turn(service, "DROP TABLE T1 PURGE")
    rejected = _execute(service, ids, confirmation="ADMIN_EXECUTE")
    assert rejected.executed is False
    assert "confirmation=T1" in rejected.warnings[0]
    assert adapter.statement_calls == []
    accepted = _execute(service, ids, confirmation="T1")
    assert accepted.committed is True
    assert accepted.schema_refresh_job_id == "refresh-1"


@pytest.mark.parametrize(
    "sql",
    ["ALTER SYSTEM SET OPEN_CURSORS = 300", "SELECT * FROM NL2SQL_PROFILES", "DROP USER U1"],
)
def test_blocked_statement_is_not_executed_and_is_audited(sql: str) -> None:
    adapter = _FakeAdapter([{"index": 1, "statement_type": "ALTER", "status": "success"}])
    service = _Service(adapter)
    ids = _store_turn(service, sql)
    data = _execute(service, ids, confirmation="ADMIN_EXECUTE")
    assert data.executed is False
    assert [item.status for item in data.statements] == ["blocked"]
    assert adapter.statement_calls == [] and adapter.select_calls == []
    [audit] = service._admin_audit_store.list_recent()
    assert audit.outcome == "blocked"
    assert audit.executed is False


def test_forbidden_statement_does_not_stop_the_other_statements() -> None:
    adapter = _FakeAdapter()
    service = _Service(adapter)
    ids = _store_turn(
        service, "SELECT 1 AS N FROM DUAL; ALTER SYSTEM SET OPEN_CURSORS = 300; SELECT 2 FROM DUAL"
    )
    assert _execute(service, ids, index=1).statements[0].status == "succeeded"
    assert _execute(service, ids, index=3).statements[0].status == "succeeded"
    assert _execute(service, ids, index=2).statements[0].status == "blocked"
    # 「すべて実行」は止める文を黙って飛ばさず、始めない。
    with pytest.raises(ValueError, match="#2"):
        _execute(service, ids, mode="all")
    assert adapter.script_calls == []
    assert [item.status for item in _saved(service, ids).statement_executions] == [
        "succeeded",
        "blocked",
        "succeeded",
    ]


def test_statement_index_out_of_range_is_rejected() -> None:
    service = _Service(_FakeAdapter())
    ids = _store_turn(service, "SELECT 1 FROM DUAL")
    with pytest.raises(ValueError, match="文 2 はありません"):
        _execute(service, ids, index=2)


def test_run_all_selects_without_confirmation_in_one_script() -> None:
    adapter = _FakeAdapter()
    service = _Service(adapter)
    ids = _store_turn(service, "SELECT 1 AS N FROM DUAL;\nSELECT 2 AS N FROM DUAL;")
    data = _execute(service, ids, mode="all")
    assert data.mode == "all"
    assert [item.status for item in data.statements] == ["succeeded", "succeeded"]
    assert all(item.result and item.result.select_result for item in data.statements)
    assert data.committed is False and data.rolled_back is False
    assert data.stopped_at is None
    # 1 つの接続で上から順に（SELECT は同じ cursor から読む）。
    assert adapter.script_calls == [
        (["SELECT 1 AS N FROM DUAL", "SELECT 2 AS N FROM DUAL"], frozenset({1, 2}), 50)
    ]
    audits = service._admin_audit_store.list_recent()
    assert len(audits) == 2
    assert {audit.detail["batch_id"] for audit in audits} == {data.batch_id}
    assert sorted(audit.detail["statement_index"] for audit in audits) == [1, 2]
    assert all(audit.confirmation_given is False for audit in audits)
    turn = _saved(service, ids)
    assert [item.status for item in turn.statement_executions] == ["succeeded", "succeeded"]
    assert turn.last_run_all is not None
    assert turn.last_run_all.succeeded is True
    assert (turn.last_run_all.statement_count, turn.last_run_all.succeeded_count) == (2, 2)


def test_run_all_with_a_change_needs_the_confirmation_once() -> None:
    adapter = _FakeAdapter()
    service = _Service(adapter)
    ids = _store_turn(
        service, "SELECT 1 FROM DUAL; INSERT INTO T1 VALUES (1); SELECT COUNT(*) FROM T1"
    )
    pending = _execute(service, ids, mode="all")
    assert {item.status for item in pending.statements} == {"confirmation_required"}
    assert adapter.script_calls == []
    assert _saved(service, ids).last_run_all is None

    data = _execute(service, ids, mode="all", confirmation="ADMIN_EXECUTE")
    assert [item.status for item in data.statements] == ["succeeded"] * 3
    assert data.committed is True and data.rolled_back is False
    assert [item.result.committed for item in data.statements if item.result] == [
        False,
        True,
        False,
    ]
    audits = sorted(
        service._admin_audit_store.list_recent(), key=lambda audit: audit.detail["statement_index"]
    )
    assert [audit.statement_types for audit in audits] == [["SELECT"], ["INSERT"], ["SELECT"]]
    assert [audit.confirmation_given for audit in audits] == [False, True, False]
    assert all(audit.detail["batch_committed"] is True for audit in audits)


def test_run_all_stops_at_the_first_error_and_rolls_back() -> None:
    adapter = _FakeAdapter(script_fail_at=2)
    service = _Service(adapter)
    ids = _store_turn(
        service, "INSERT INTO T1 VALUES (1); INSERT INTO T1 VALUES ('x'); SELECT COUNT(*) FROM T1"
    )
    data = _execute(service, ids, mode="all", confirmation="ADMIN_EXECUTE")
    assert [item.status for item in data.statements] == ["succeeded", "failed", "not_run"]
    assert data.stopped_at == 2
    assert data.committed is False and data.rolled_back is True
    assert data.statements[0].result is not None
    assert data.statements[0].result.rolled_back is True
    assert data.statements[2].result is None
    # 実行しなかった文は監査に残さない。
    audits = service._admin_audit_store.list_recent()
    assert sorted(audit.outcome for audit in audits) == ["failure", "success"]
    turn = _saved(service, ids)
    assert [item.status for item in turn.statement_executions] == [
        "succeeded",
        "failed",
        "not_run",
    ]
    assert turn.statement_executions[1].outcome.startswith("失敗: INSERT: ORA-01722")
    assert turn.last_run_all is not None
    assert turn.last_run_all.stopped_at == 2 and turn.last_run_all.rolled_back is True


def test_run_all_marks_ddl_before_the_failure_as_committed(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _FakeAdapter(script_fail_at=3)
    service = _Service(adapter)
    monkeypatch.setattr(
        service,
        "_submit_schema_refresh_after_admin_mutation",
        lambda **_kwargs: SchemaRefreshMutationSync(job_id="refresh-2"),
    )
    ids = _store_turn(
        service, "INSERT INTO T0 VALUES (1); CREATE TABLE T1 (ID NUMBER); INSERT INTO T1 VALUES (1)"
    )
    data = _execute(service, ids, mode="all", confirmation="ADMIN_EXECUTE")
    # DDL の暗黙の commit は、その前の成功した DML も commit する。
    assert [item.result.committed for item in data.statements if item.result] == [
        True,
        True,
        False,
    ]
    assert data.committed is True and data.rolled_back is False
    assert any("暗黙 commit" in warning for warning in data.warnings)
    assert data.schema_refresh_job_id == "refresh-2"


def test_run_all_selects_without_oracle_uses_the_deterministic_runtime() -> None:
    adapter = _FakeAdapter()
    service = _Service(adapter, oracle=False)
    ids = _store_turn(service, "SELECT 1 FROM DUAL; SELECT 2 FROM DUAL")
    data = _execute(service, ids, mode="all")
    assert [item.status for item in data.statements] == ["succeeded", "succeeded"]
    assert adapter.script_calls == []


def test_corrected_statement_replaces_only_that_statement() -> None:
    adapter = _FakeAdapter(script_fail_at=2)
    service = _Service(adapter)
    ids = _store_turn(service, "SELECT 1 FROM DUAL; SELECT * FROM T9; SELECT 3 FROM DUAL")
    _execute(service, ids, mode="all")
    turn = service.replace_db_admin_chat_turn_sql(
        ids[0], ids[1], "SELECT * FROM T8", actor="chat-user", all_users=False, statement_index=2
    )
    assert [item.sql for item in turn.plan.statements] == [
        "SELECT 1 FROM DUAL",
        "SELECT * FROM T8",
        "SELECT 3 FROM DUAL",
    ]
    assert turn.revision == 1 and turn.corrected is True
    # 変わらない文の要約は残し、変わった文と「すべて実行」の要約は消す。
    assert [(item.index, item.status) for item in turn.statement_executions] == [
        (1, "succeeded"),
        (3, "not_run"),
    ]
    assert turn.last_run_all is None
    with pytest.raises(ValueError, match="文 4 はありません"):
        service.replace_db_admin_chat_turn_sql(
            ids[0],
            ids[1],
            "SELECT 4 FROM DUAL",
            actor="chat-user",
            all_users=False,
            statement_index=4,
        )


# ---- 実行の部品（1 つの接続で SELECT を含む文を順に実行）----


class _ScriptCursor:
    def __init__(self, fail_on: str = "") -> None:
        self.fail_on = fail_on
        self.executed: list[str] = []
        self.description: list[tuple[str]] = []
        self.rowcount = 0

    def __enter__(self) -> _ScriptCursor:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def callproc(self, *_: Any) -> None:
        return None

    def execute(self, sql: str) -> None:
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("ORA-00942: table or view does not exist")
        self.executed.append(sql)
        self.description = [("N",)] if sql.startswith("SELECT") else []
        self.rowcount = 1


class _ScriptConnection:
    def __init__(self, cursor: _ScriptCursor) -> None:
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _ScriptCursor:
        return self._cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_statement_executor_reads_queries_and_stops_at_the_first_error() -> None:
    from app.clients.oracle_statement_executor import OracleStatementExecutor

    reads: list[str] = []

    def reader(cursor: Any) -> tuple[int, str]:
        reads.append(cursor.executed[-1])
        return 1, "rows"

    ok_connection = _ScriptConnection(_ScriptCursor())
    results = OracleStatementExecutor().execute(
        ok_connection,
        ["INSERT INTO T1 VALUES (1)", "SELECT N FROM T1"],
        stop_on_error=True,
        query_indexes=frozenset({2}),
        query_reader=reader,
    )
    assert [item["status"] for item in results] == ["success", "success"]
    assert results[1]["select_result"] == "rows" and results[1]["row_count"] == 1
    assert reads == ["SELECT N FROM T1"]
    assert (ok_connection.commits, ok_connection.rollbacks) == (1, 0)

    failing = _ScriptConnection(_ScriptCursor(fail_on="T9"))
    results = OracleStatementExecutor().execute(
        failing,
        ["INSERT INTO T1 VALUES (1)", "SELECT N FROM T9", "SELECT N FROM T1"],
        stop_on_error=True,
        query_indexes=frozenset({2, 3}),
        query_reader=reader,
    )
    assert [item["status"] for item in results] == ["success", "error"]
    assert (failing.commits, failing.rollbacks) == (0, 1)


# ---- 生成と分析 ----


def _catalog_service(adapter: _FakeAdapter, ai: _FakeEnterpriseAi) -> _Service:
    service = _Service(adapter)
    service._enterprise_ai_client = ai
    service._catalog = SchemaCatalog(
        refreshed_at="2026-10-18T00:00:00+00:00",
        current_owner="APP",
        tables=[
            SchemaTable(
                table_name="ITEM_STOCK",
                logical_name="",
                owner="APP",
                columns=[SchemaColumn(column_name="QTY", logical_name="", data_type="NUMBER")],
            ),
            SchemaTable(table_name="OTHER_TABLE", logical_name="", owner="APP"),
        ],
    )
    return service


def test_generate_returns_plan_without_executing() -> None:
    adapter = _FakeAdapter()
    ai = _FakeEnterpriseAi(
        [{"sql": "```sql\nDELETE FROM APP.ITEM_STOCK WHERE QTY = 0\n```", "explanation": "説明"}]
    )
    service = _catalog_service(adapter, ai)
    data = service.generate_db_admin_chat_sql(
        "ITEM_STOCK の QTY が 0 の行を消す",
        [DbAdminChatHistoryTurn(question="前の質問", sql="SELECT 1 FROM DUAL")],
    )
    assert data.plan.sql == "DELETE FROM APP.ITEM_STOCK WHERE QTY = 0"
    assert data.plan.requires_confirmation is True
    assert data.plan.confirmation_target == "ADMIN_EXECUTE"
    assert data.context_tables == ["APP.ITEM_STOCK"]
    assert data.model == "test-text-model"
    assert adapter.statement_calls == [] and adapter.select_calls == []
    [call] = ai.calls
    assert "V$VERSION" in call["system_prompt"]
    assert "DBA_TABLESPACE_USAGE_METRICS" in call["system_prompt"]
    assert "table APP.ITEM_STOCK" in call["context"]
    assert "APP.OTHER_TABLE" in call["context"]
    assert "前の質問" in call["context"]


def _progress_steps(data: Any, table: TraceTable, *, turn_id: str) -> dict[str, Any]:
    """生成の処理の経過（span の表から読む。工程の詳細は生成の結果から足す）。"""
    table.flush()
    events = read_trace_progress(
        target_id=turn_id,
        operation_id=turn_id,
        trace_id=data.trace_id,
        owner_user_uuid="",
        viewer_is_admin=True,
        terminal="done",
        details=data.span_details,
    ).events
    fold = fold_chat_progress_events(events)
    return {step.kind: step for step in fold.step_list() if not (step.span or {}).get("root")}


@pytest.mark.usefixtures("trace_body_enabled")
def test_generate_records_the_generation_and_check_as_span_tree(
    trace_table: TraceTable,  # noqa: F811
) -> None:
    """処理の経過は生成と SQL の確認の span の木（#3226 の案 A）。

    ターンの ID の span として表に書く。
    """
    ai = _FakeEnterpriseAi(
        [{"sql": "SELECT QTY FROM APP.ITEM_STOCK; DROP TABLESPACE TS1", "explanation": ""}]
    )
    service = _catalog_service(_FakeAdapter(), ai)
    data = service.generate_db_admin_chat_sql("合成の質問", operation_id="turn-1")
    assert data.trace_id and data.trace_dropped_spans == 0
    # 同期の要求なので、応答の前に表へ書く（画面は応答の直後にターンの処理の経過を読む）。
    assert {row["name"] for row in trace_table.db.spans(trace_id=data.trace_id)} >= {
        "nl2sql.db_admin_chat",
        "nl2sql.generate_sql",
        "nl2sql.safety_check",
    }
    steps = _progress_steps(data, trace_table, turn_id="turn-1")
    rows = trace_table.db.spans(trace_id=data.trace_id)
    assert {row["operation_id"] for row in rows} == {"turn-1"}
    assert {row["product"] for row in rows} == {"nl2sql"}
    # 工程の詳細（生成した SQL）は表に入れない（ターンに置く）。
    assert "ITEM_STOCK" not in str(rows)
    assert list(steps) == ["generate_sql", "db_admin_check"]
    generate, check = steps["generate_sql"], steps["db_admin_check"]
    assert generate.status == "done" and generate.params == {"engine": "enterprise_ai_direct"}
    assert generate.span is not None and generate.span["name"] == "nl2sql.generate_sql"
    assert generate.details is not None and generate.details.code[0].language == "sql"
    # SQL の確認の補足は文の分類ごとの数（止める文があっても、ほかの文は実行できるので完了）。
    assert check.status == "done"
    assert check.params == {"statements": 2, "read_only": 1, "change": 0, "blocked": 1}
    assert check.details is not None
    fields = {field.label: field.value for field in check.details.fields}
    assert fields["文の数"] == "2" and fields["実行できない文"] == "#2"
    # 2 つの段は同じ実行の全体（root）の子。
    [root] = [row for row in rows if row["is_root"]]
    assert {step.span["parent_id"] for step in steps.values() if step.span} == {root["span_id"]}


def test_generate_fails_the_check_when_every_statement_is_blocked(
    trace_table: TraceTable,  # noqa: F811
) -> None:
    ai = _FakeEnterpriseAi([{"sql": "DROP TABLESPACE TS1", "explanation": ""}])
    service = _catalog_service(_FakeAdapter(), ai)
    data = service.generate_db_admin_chat_sql("合成の質問", operation_id="turn-2")
    check = _progress_steps(data, trace_table, turn_id="turn-2")["db_admin_check"]
    assert check.status == "failed"
    assert check.params == {"statements": 1, "read_only": 0, "change": 0, "blocked": 1}


def test_generate_requires_enterprise_ai() -> None:
    service = _catalog_service(_FakeAdapter(), _FakeEnterpriseAi([], configured=False))
    with pytest.raises(DbAdminChatGenerationError) as error:
        service.generate_db_admin_chat_sql("版")
    assert error.value.status_code == 409


def test_prose_only_answer_is_regenerated_as_sql() -> None:
    """SQL の無い応答（文章だけの答え）は、理由を添えて 1 回だけ作り直させる。"""
    ai = _FakeEnterpriseAi(
        [
            {"sql": "", "explanation": "**ITEM_STOCK** は在庫の表です。"},
            {"sql": "SELECT TABLE_NAME, COMMENTS FROM ALL_TAB_COMMENTS", "explanation": "表の説明"},
        ]
    )
    service = _catalog_service(_FakeAdapter(), ai)
    data = service.generate_db_admin_chat_sql("これらの表は何？")
    assert [item.sql for item in data.plan.statements] == [
        "SELECT TABLE_NAME, COMMENTS FROM ALL_TAB_COMMENTS"
    ]
    assert data.explanation == "表の説明"
    assert len(ai.calls) == 2
    assert ai.calls[0]["prompt"] == "これらの表は何？"
    # 作り直しの依頼は、何が違反でどう直すかを書く（質問はそのまま渡す）。
    assert ai.calls[1]["prompt"].startswith("これらの表は何？")
    assert "SQL がありませんでした" in ai.calls[1]["prompt"]
    assert "必ず SQL で答えます" in ai.calls[0]["system_prompt"]


@pytest.mark.parametrize(
    "second",
    [
        {"sql": "", "explanation": "表の一覧です。"},
        # 文章を sql に入れた応答も SQL ではない（文の種類が分からない文だけ）。
        {"sql": "ITEM_STOCK は在庫の表、OTHER_TABLE は補助の表です。", "explanation": ""},
    ],
    ids=["empty", "prose-in-sql"],
)
def test_answer_without_sql_twice_is_a_generation_failure(second: dict[str, Any]) -> None:
    ai = _FakeEnterpriseAi([{"sql": "", "explanation": "表の一覧です。"}, second])
    service = _catalog_service(_FakeAdapter(), ai)
    with pytest.raises(DbAdminChatGenerationError) as error:
        service.generate_db_admin_chat_sql("これらの表は何？")
    # 文章を回答として出さず、生成の失敗（画面は質問を残して「再送信」）。
    assert error.value.status_code == 502
    assert str(error.value).startswith("SQL を生成できませんでした")
    assert len(ai.calls) == 2


def test_prose_only_answer_is_not_saved_as_a_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    ai = _FakeEnterpriseAi(
        [{"sql": "", "explanation": "表の一覧です。"}, {"sql": "", "explanation": "表の一覧です。"}]
    )
    service = _catalog_service(_FakeAdapter(), ai)
    with pytest.raises(DbAdminChatGenerationError):
        service.send_db_admin_chat_question(
            DbAdminChatGenerateRequest(question="これらの表は何？", client_turn_id=uuid.uuid4()),
            actor="chat-user",
            all_users=False,
        )
    items, total = service._db_admin_chat_store.list_conversations(
        owner_user_uuid=None, offset=0, limit=10
    )
    assert (items, total) == ([], 0)


def _saved_analysis_turn(
    service: Nl2SqlService, sql: str, failure: str = "ORA-00942: synthetic failure"
) -> tuple[str, str]:
    conversation_id, turn_id = str(uuid.uuid4()), str(uuid.uuid4())
    service._db_admin_chat_store.create_conversation(
        conversation_id, owner_user_uuid="chat-user", title="合成の質問"
    )
    service._db_admin_chat_store.append_turn(
        conversation_id,
        DbAdminChatTurn(
            turn_id=turn_id,
            question="合成の質問",
            created_at="2026-10-22",
            generated_at="2026-10-22",
            plan=service._db_admin_chat_plan(sql),
            statement_executions=[
                DbAdminChatStatementLastExecution(
                    index=1,
                    status="failed",
                    outcome=failure,
                    executed_at="2026-10-22",
                )
            ],
        ),
    )
    return conversation_id, turn_id


def test_analyze_returns_corrected_plan() -> None:
    ai = _FakeEnterpriseAi(
        [
            {
                "cause": "1) 原因 2) 直し方 3) 結論",
                "fix": "修正方法です。",
                "conclusion": "確認して実行します。",
                "corrected_sql": "DROP TABLE T2 PURGE",
            }
        ]
    )
    service = _catalog_service(_FakeAdapter(), ai)
    ids = _saved_analysis_turn(service, "DROP TABLE T1")
    data = service.analyze_db_admin_chat_failure(
        *ids,
        DbAdminChatAnalyzeRequest(statement_index=1),
        actor="chat-user",
        all_users=False,
    )
    assert data.source == "oci_enterprise_ai"
    assert data.cause and data.fix and data.conclusion
    assert "analysis" not in data.model_dump()
    assert data.corrected_plan is not None
    assert data.corrected_plan.confirmation_target == "T2"


def test_analyze_falls_back_without_enterprise_ai() -> None:
    service = _catalog_service(_FakeAdapter(), _FakeEnterpriseAi([], configured=False))
    ids = _saved_analysis_turn(service, "SELECT * FROM T9")
    data = service.analyze_db_admin_chat_failure(
        *ids,
        DbAdminChatAnalyzeRequest(statement_index=1),
        actor="chat-user",
        all_users=False,
    )
    assert "ORA-00942" in data.cause
    assert data.corrected_plan is None
    assert data.warnings


@pytest.mark.parametrize(
    "question",
    [
        "データベースのバージョンと表領域ごとの使用率を確認したい",
        "累計の待機時間が長い待機イベントを上位100件確認したい",
        "NL2SQL_TEST を TRUNCATE して",
    ],
)
def test_context_tables_ignore_common_comment_tokens(question: str) -> None:
    tables = [
        SchemaTable(
            table_name=f"SAMPLE_{i}",
            logical_name="",
            owner="APP",
            comment="使用中の表を確認します。",
            columns=[
                SchemaColumn(
                    column_name="NOTE",
                    logical_name="",
                    data_type="CHAR",
                    comment="確認に使用する記録の説明です。",
                )
            ],
        )
        for i in range(4)
    ]
    assert select_context_tables(tables, _similarity_tokens(question), _similarity_tokens) == []


def test_context_tables_keep_distinct_names_comments_and_multiple_columns() -> None:
    tables = [
        SchemaTable(table_name="SAMPLE_A", logical_name="", owner="APP", comment="星雲の記録"),
        SchemaTable(
            table_name="SAMPLE_B",
            logical_name="",
            owner="APP",
            columns=[
                SchemaColumn(column_name="ALPHA", logical_name="", data_type="NUMBER"),
                SchemaColumn(column_name="BETA", logical_name="", data_type="NUMBER"),
            ],
        ),
        SchemaTable(table_name="SAMPLE_C", logical_name="", owner="APP"),
    ]
    for question, expected in [
        ("SAMPLE_A", "SAMPLE_A"),
        ("星雲", "SAMPLE_A"),
        ("ALPHA と BETA", "SAMPLE_B"),
    ]:
        assert [
            t.table_name
            for t in select_context_tables(tables, _similarity_tokens(question), _similarity_tokens)
        ] == [expected]


@pytest.mark.parametrize(
    "payload",
    [
        {"analysis": "1) 原因 2) 直し方 3) 結論", "corrected_sql": "SELECT 1 FROM DUAL"},
        {"cause": "原因だけ", "corrected_sql": "SELECT 1 FROM DUAL"},
    ],
)
def test_analyze_invalid_structure_uses_complete_fallback(payload: dict[str, str]) -> None:
    service = _catalog_service(_FakeAdapter(), _FakeEnterpriseAi([payload]))
    ids = _saved_analysis_turn(service, "SELECT * FROM T9")
    data = service.analyze_db_admin_chat_failure(
        *ids, DbAdminChatAnalyzeRequest(statement_index=1), actor="chat-user", all_users=False
    )
    assert data.source == "deterministic"
    assert "ORA-00942" in data.cause
    assert data.fix and data.conclusion and data.warnings
    assert data.corrected_plan is None


@pytest.fixture
def trace_body_enabled() -> Iterator[None]:
    from pr_backend_core.observability.call_policy import call_content_policy

    with call_content_policy(lambda: True):
        yield
