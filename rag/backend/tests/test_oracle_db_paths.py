"""DB アクセスの経路の回帰テスト(#341)。

- GET で書き込まない(レシピ 1 は登録・ジョブ投入などの書き込みの経路で作る)。
- 存在確認・状態確認で rag_documents の大きな JSON 列を読まない。
- transaction の rollback の失敗で元の例外を隠さない。
- 一時的な接続断は新しい接続で 1 回だけ再試行する。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from app.clients.oracle import (
    OracleClient,
    is_transient_oracle_error,
    oracle_error_log_fields,
)
from app.schemas.document import (
    DocumentProcessingConfig,
    FileStatus,
    IngestionJob,
    IngestionJobStatus,
)
from tests.test_oracle_adapter import (
    FakeOraclePool,
    _llm_settings,
    _oracle_document_row,
    _oracle_knowledge_base_row,
    _run_inline,
)

_JSON_COLUMNS = ("extraction", "classification", "preprocess_artifact")
_WRITE_PREFIXES = ("MERGE", "INSERT", "UPDATE", "DELETE")


def _default_recipe_id(document_id: str = "doc-1") -> str:
    return hashlib.sha256(f"{document_id}:recipe:1".encode()).hexdigest()


def _recipe_row(**overrides: object) -> dict[str, object]:
    now = datetime(2026, 1, 3, tzinfo=UTC)
    row: dict[str, object] = {
        "recipe_id": _default_recipe_id(),
        "document_id": "doc-1",
        "slot_no": 1,
        "processing_config": None,
        "status": "INDEXED",
        "failed_phase": None,
        "preprocess_artifact": None,
        "active_extraction_recipe_id": None,
        "config_revision": 1,
        "materialized_revision": 1,
        "error_message": None,
        "started_at": None,
        "finished_at": None,
        "created_at": now,
        "updated_at": now,
        "active_chunk_set_id": None,
        "chunk_count": 0,
        "vector_count": 0,
        "chunk_set_status": None,
    }
    row.update(overrides)
    return row


def _document_state_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "document_id": "doc-1",
        "file_name": "policy.txt",
        "status": "UPLOADED",
        "tenant_id_hash": None,
        "category_name": None,
        "object_storage_path": "local://policy.txt",
        "content_type": "text/plain",
        "file_size_bytes": 12,
        "content_sha256": "a" * 64,
        "duplicate_of_document_id": None,
        "error_message": None,
        "uploaded_at": datetime(2026, 1, 1, tzinfo=UTC),
        "indexed_at": None,
    }
    row.update(overrides)
    return row


def _statements(pool: FakeOraclePool) -> list[str]:
    return [call.statement for call in pool.connection.calls]


def _write_statements(pool: FakeOraclePool) -> list[str]:
    return [
        statement
        for statement in _statements(pool)
        if statement.lstrip().upper().startswith(_WRITE_PREFIXES)
    ] + [call.statement for call in pool.connection.many_calls]


def _document_selects(pool: FakeOraclePool) -> list[str]:
    """rag_documents を FROM に持つ SELECT(JOIN を含む)だけを返す。"""
    return [
        statement
        for statement in _statements(pool)
        if statement.lstrip().upper().startswith(("SELECT", "WITH"))
        and "FROM rag_documents" in statement
    ]


def _assert_no_json_columns(statements: list[str]) -> None:
    for statement in statements:
        select_list = statement.split("FROM rag_documents", 1)[0]
        for column in _JSON_COLUMNS:
            assert column not in select_list, f"{column} を SELECT している: {statement}"


# ---------------------------------------------------------------------------
# 1. GET /recipes で書き込まない
# ---------------------------------------------------------------------------


async def test_list_document_recipes_does_not_write_when_recipe_rows_exist() -> None:
    """レシピ行があれば読むだけ(MERGE も commit もしない)。"""
    pool = FakeOraclePool(execute_results=[[_recipe_row()]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    rows = await client.list_document_recipes("doc-1")

    assert [row["recipe_id"] for row in rows] == [_default_recipe_id()]
    assert _write_statements(pool) == []
    assert pool.connection.commits == 0
    _assert_no_json_columns(_document_selects(pool))


async def test_list_document_recipes_returns_virtual_default_recipe_without_write() -> None:
    """レシピ行が無い既存の文書は、書き込まずに文書の状態からレシピ 1 を仮に返す。"""
    document_row: dict[str, object] = {
        "document_id": "doc-1",
        "tenant_id_hash": None,
        "status": "REVIEW",
        "processing_config": '{"parser_backend":"local"}',
        "preprocess_artifact": None,
        "error_message": None,
        "uploaded_at": datetime(2026, 1, 1, tzinfo=UTC),
        "indexed_at": None,
        "recipe_count": 0,
    }
    pool = FakeOraclePool(execute_results=[[], [document_row]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    rows = await client.list_document_recipes("doc-1")

    assert len(rows) == 1
    assert rows[0]["recipe_id"] == _default_recipe_id()
    assert rows[0]["slot_no"] == 1
    assert rows[0]["status"] == "REVIEW"
    assert rows[0]["config_revision"] == 1
    assert rows[0]["processing_config"] == {"parser_backend": "local"}
    assert _write_statements(pool) == []
    assert pool.connection.commits == 0
    for statement in _document_selects(pool):
        select_list = statement.split("FROM rag_documents", 1)[0]
        assert "extraction" not in select_list
        assert "classification" not in select_list


async def test_list_document_recipes_raises_key_error_for_missing_document() -> None:
    """文書が無い(または見えない)ときは KeyError(API は 404)。"""
    pool = FakeOraclePool(execute_results=[[], []])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    with pytest.raises(KeyError):
        await client.list_document_recipes("doc-1")

    assert _write_statements(pool) == []


async def test_list_document_recipes_does_not_resurrect_deleted_recipe_one() -> None:
    """レシピ 1 を削除して他のレシピだけがある文書に、レシピ 1 を戻さない。"""
    pool = FakeOraclePool(
        execute_results=[[_recipe_row(recipe_id="recipe-2", slot_no=2)]],
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    rows = await client.list_document_recipes("doc-1")

    assert [row["recipe_id"] for row in rows] == ["recipe-2"]
    assert _write_statements(pool) == []


async def test_get_document_recipe_returns_virtual_default_recipe_without_write() -> None:
    """レシピ行の無い文書のレシピ 1 は、読み取りでは仮の行を返す(書き込まない)。"""
    document_row: dict[str, object] = {
        "document_id": "doc-1",
        "tenant_id_hash": None,
        "status": "UPLOADED",
        "processing_config": None,
        "preprocess_artifact": None,
        "error_message": None,
        "uploaded_at": datetime(2026, 1, 1, tzinfo=UTC),
        "indexed_at": None,
        "recipe_count": 0,
    }
    pool = FakeOraclePool(execute_results=[[], [document_row]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    row = await client.get_document_recipe("doc-1", _default_recipe_id())

    assert row is not None
    assert row["recipe_id"] == _default_recipe_id()
    assert _write_statements(pool) == []


async def test_get_document_recipe_does_not_return_virtual_row_for_other_recipe_ids() -> None:
    """レシピ 1 以外の ID は仮の行を作らない。"""
    pool = FakeOraclePool(execute_results=[[]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    assert await client.get_document_recipe("doc-1", "recipe-2") is None
    assert len(pool.connection.calls) == 1


async def test_ensure_default_document_recipe_skips_merge_when_recipe_exists() -> None:
    """投入の経路でも、レシピ 1 があれば MERGE せず JSON 列も読まない。"""
    pool = FakeOraclePool(execute_results=[[_recipe_row()]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    row = await client.ensure_default_document_recipe("doc-1")

    assert row["recipe_id"] == _default_recipe_id()
    assert _write_statements(pool) == []
    assert _document_selects(pool) == []


async def test_ensure_default_document_recipe_creates_missing_recipe_one() -> None:
    """レシピ 1 が無ければ(書き込みの経路で)文書の状態から作る。"""
    document_row: dict[str, object] = {
        "document_id": "doc-1",
        "tenant_id_hash": "t" * 64,
        "status": "INDEXED",
        "processing_config": None,
        "preprocess_artifact": None,
        "error_message": None,
        "uploaded_at": datetime(2026, 1, 1, tzinfo=UTC),
        "indexed_at": datetime(2026, 1, 2, tzinfo=UTC),
        "recipe_count": 0,
    }
    pool = FakeOraclePool(execute_results=[[], [document_row], [_recipe_row()]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    row = await client.ensure_default_document_recipe("doc-1")

    assert row["recipe_id"] == _default_recipe_id()
    merge = next(
        call
        for call in pool.connection.calls
        if "MERGE INTO rag_document_recipes" in call.statement
    )
    assert merge.parameters["recipe_id"] == _default_recipe_id()
    assert merge.parameters["status"] == "INDEXED"
    assert merge.parameters["tenant_id_hash"] == "t" * 64
    assert merge.parameters["materialized_revision"] == 1
    # レシピ 1 の元にする preprocess_artifact / processing_config は読むが、抽出結果は読まない。
    for statement in _document_selects(pool):
        select_list = statement.split("FROM rag_documents", 1)[0]
        assert "extraction" not in select_list
        assert "classification" not in select_list


async def test_create_document_creates_recipe_one_in_same_transaction() -> None:
    """文書の登録と同じ transaction でレシピ 1 を作る(GET で作らない前提)。"""
    pool = FakeOraclePool(execute_results=[[_oracle_knowledge_base_row()]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    detail = await client.create_document(
        file_name="policy.txt",
        object_storage_path="local://policy.txt",
        content_type="text/plain",
        knowledge_base_ids=["kb-1"],
    )

    recipe_insert = next(
        call
        for call in pool.connection.calls
        if "rag_document_recipes" in call.statement
        and call.statement.lstrip().upper().startswith(("INSERT", "MERGE"))
    )
    assert recipe_insert.parameters["recipe_id"] == _default_recipe_id(detail.id)
    assert recipe_insert.parameters["document_id"] == detail.id
    assert recipe_insert.parameters["status"] == FileStatus.UPLOADED.value
    assert pool.connection.commits == 1


# ---------------------------------------------------------------------------
# 2. 存在確認・状態確認で JSON 列を読まない
# ---------------------------------------------------------------------------


async def test_document_exists_and_summary_do_not_select_json_columns() -> None:
    """ポーリングの API の存在確認・状態確認は JSON 列を読まない。"""
    pool = FakeOraclePool(
        execute_results=[[{"document_id": "doc-1"}], [_document_state_row()], [], []]
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    assert await client.document_exists("doc-1") is True
    summary = await client.get_document_summary("doc-1")
    assert await client.document_exists("doc-missing") is False
    assert await client.get_document_summary("doc-missing") is None

    assert summary is not None
    assert summary.id == "doc-1"
    assert summary.status == FileStatus.UPLOADED
    assert summary.content_sha256 == "a" * 64
    assert not hasattr(summary, "extraction")
    selects = _document_selects(pool)
    assert len(selects) == 4
    _assert_no_json_columns(selects)


async def test_list_documents_does_not_select_json_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    """取込中にポーリングされる文書一覧は、要約に使わない JSON 列を読まない。"""
    pool = FakeOraclePool(execute_results=[[_document_state_row()], []])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)
    # 公開版の取得は独立に検証し、このテストでは元の文書 SQL の応答だけを供給する。
    monkeypatch.setattr(client, "document_language_pack_uses", AsyncMock(return_value={}))

    documents = await client.list_documents(limit=10)

    assert [document.id for document in documents] == ["doc-1"]
    selects = _document_selects(pool)
    assert selects
    _assert_no_json_columns(selects)


async def test_update_document_processing_config_existence_check_skips_json_columns() -> None:
    pool = FakeOraclePool(execute_results=[[_document_state_row()]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    await client.update_document_processing_config("doc-1", DocumentProcessingConfig())

    _assert_no_json_columns(_document_selects(pool))


async def test_create_document_ingestion_job_existence_check_skips_json_columns() -> None:
    pool = FakeOraclePool(execute_results=[[_document_state_row()]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)
    job = IngestionJob(
        id="job-1",
        document_id="doc-1",
        status=IngestionJobStatus.QUEUED,
        parser_profile="local_text_structure",
        queued_at=datetime(2026, 1, 2, tzinfo=UTC),
    )

    await client.create_ingestion_job(job)

    assert _document_selects(pool)
    _assert_no_json_columns(_document_selects(pool))


async def test_replace_ingestion_segments_existence_check_skips_json_columns() -> None:
    pool = FakeOraclePool(execute_results=[[_document_state_row()]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    await client.replace_ingestion_segments("doc-1", [])

    assert _document_selects(pool)
    _assert_no_json_columns(_document_selects(pool))


async def test_delete_document_existence_check_skips_json_columns() -> None:
    pool = FakeOraclePool(execute_results=[[_document_state_row()]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    await client.delete_document("doc-1")

    assert _document_selects(pool)
    _assert_no_json_columns(_document_selects(pool))


async def test_update_document_status_existence_check_skips_json_columns() -> None:
    """状態の更新の前の存在確認は JSON 列を読まない(戻り値の詳細は従来どおり)。"""
    pool = FakeOraclePool(execute_results=[[_document_state_row()], [_oracle_document_row()], []])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    await client.update_document_status("doc-1", FileStatus.PREPROCESSING)

    first_select = _document_selects(pool)[0]
    _assert_no_json_columns([first_select])


# ---------------------------------------------------------------------------
# 3. transaction の rollback と一時的な接続断の再試行
# ---------------------------------------------------------------------------


def _dead_session_error() -> RuntimeError:
    return RuntimeError(
        SimpleNamespace(
            is_session_dead=True,
            isrecoverable=True,
            full_code="DPY-4011",
            code=0,
            message="DPY-4011: the database or network closed the connection",
        )
    )


class _Connection:
    def __init__(
        self,
        *,
        rollback_error: Exception | None = None,
        commit_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.rollback_error = rollback_error
        self.commit_error = commit_error
        self.close_error = close_error
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def cursor(self) -> Any:  # pragma: no cover - operation は cursor を使わない
        raise AssertionError("cursor は使わない")

    def commit(self) -> None:
        if self.commit_error is not None:
            raise self.commit_error
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1
        if self.rollback_error is not None:
            raise self.rollback_error

    def close(self) -> None:
        self.closes += 1
        if self.close_error is not None:
            raise self.close_error


class _Pool:
    def __init__(self, connections: list[_Connection]) -> None:
        self.connections = connections
        self.acquired: list[_Connection] = []

    def acquire(self) -> _Connection:
        connection = self.connections[len(self.acquired)]
        self.acquired.append(connection)
        return connection

    def close(self, force: bool = False) -> None:
        _ = force


def _failing_operation(errors: list[Exception]) -> Callable[[Any], str]:
    calls: list[Any] = []

    def operation(connection: Any) -> str:
        calls.append(connection)
        if errors:
            raise errors.pop(0)
        return "ok"

    return operation


async def test_transaction_rollback_failure_keeps_original_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """rollback が失敗(DPY-1001)しても、元の例外を raise してログに両方を残す。"""
    rollback_error = RuntimeError(
        SimpleNamespace(is_session_dead=False, isrecoverable=False, full_code="DPY-1001", code=0)
    )
    connection = _Connection(rollback_error=rollback_error, close_error=RuntimeError("closed"))
    pool = _Pool([connection])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)
    original = ValueError("元の例外")

    with (
        caplog.at_level(logging.WARNING, logger="app.clients.oracle"),
        pytest.raises(ValueError, match="元の例外"),
    ):
        await client._run_transaction(_failing_operation([original]))

    assert connection.rollbacks == 1
    assert connection.closes == 1
    record = next(item for item in caplog.records if item.message == "oracle_rollback_failed")
    assert record.__dict__["full_code"] == "DPY-1001"
    assert "oracle_error_code" in record.__dict__


async def test_transaction_retries_once_on_dead_session_with_new_connection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """commit 前の DPY-4011(is_session_dead)は、新しい接続で 1 回だけ再試行する。"""
    first = _Connection(rollback_error=RuntimeError("DPY-1001: not connected"))
    second = _Connection()
    pool = _Pool([first, second])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    with caplog.at_level(logging.WARNING, logger="app.clients.oracle"):
        result = await client._run_transaction(_failing_operation([_dead_session_error()]))

    assert result == "ok"
    assert pool.acquired == [first, second]
    assert first.commits == 0
    assert second.commits == 1
    record = next(item for item in caplog.records if item.message == "oracle_transaction_retry")
    assert record.__dict__["full_code"] == "DPY-4011"
    assert record.__dict__["oracle_error_code"] == 0


async def test_transaction_does_not_retry_more_than_once() -> None:
    pool = _Pool([_Connection(), _Connection(), _Connection()])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    with pytest.raises(RuntimeError):
        await client._run_transaction(
            _failing_operation([_dead_session_error(), _dead_session_error()])
        )

    assert len(pool.acquired) == 2


async def test_transaction_does_not_retry_non_transient_errors() -> None:
    pool = _Pool([_Connection(), _Connection()])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    with pytest.raises(ValueError):
        await client._run_transaction(_failing_operation([ValueError("制約違反")]))

    assert len(pool.acquired) == 1


async def test_transaction_does_not_retry_when_commit_fails() -> None:
    """commit で切断したら反映済みか分からないため、再試行しない。"""
    first = _Connection(commit_error=_dead_session_error())
    pool = _Pool([first, _Connection()])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    with pytest.raises(RuntimeError):
        await client._run_transaction(_failing_operation([]))

    assert len(pool.acquired) == 1


def test_transient_oracle_error_classification_and_log_fields() -> None:
    dead = _dead_session_error()
    assert is_transient_oracle_error(dead) is True
    assert is_transient_oracle_error(ValueError("x")) is False
    assert (
        is_transient_oracle_error(
            RuntimeError(
                SimpleNamespace(isrecoverable=False, is_session_dead=False, full_code="ORA-00001")
            )
        )
        is False
    )
    fields = oracle_error_log_fields(dead)
    assert fields["oracle_error_code"] == 0
    assert fields["full_code"] == "DPY-4011"
    plain = oracle_error_log_fields(ValueError("x"))
    assert plain["oracle_error_code"] is None
    assert plain["full_code"] is None


def test_real_oracledb_dead_session_error_is_transient() -> None:
    """python-oracledb の DPY-4011 の実オブジェクトでも一時的な接続断と判定する。"""
    oracledb = pytest.importorskip("oracledb")
    errors = pytest.importorskip("oracledb.errors")
    error = errors._create_err(errors.ERR_CONNECTION_CLOSED)
    exc = oracledb.DatabaseError(error)
    assert is_transient_oracle_error(exc) is True
    assert oracle_error_log_fields(exc)["full_code"] == "DPY-4011"


@pytest.mark.parametrize("retry", [False, True])
async def test_deadlock_retries_only_when_explicitly_enabled(retry: bool) -> None:
    first, second = _Connection(), _Connection()
    pool = _Pool([first, second])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)
    operation = _failing_operation([RuntimeError("ORA-00060: deadlock detected")])
    if retry:
        assert await client._run_transaction(operation, retry_deadlocks=True) == "ok"
        assert first.rollbacks == 1 and first.commits == 0 and second.commits == 1
        assert pool.acquired == [first, second]
    else:
        with pytest.raises(RuntimeError, match="ORA-00060"):
            await client._run_transaction(operation)
        assert pool.acquired == [first]


async def test_deadlock_is_not_retried_after_rollback_failure() -> None:
    connection = _Connection(rollback_error=RuntimeError("rollback failed"))
    pool = _Pool([connection, _Connection()])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)
    with pytest.raises(RuntimeError, match="ORA-00060"):
        await client._run_transaction(
            _failing_operation([RuntimeError("ORA-00060: deadlock")]), retry_deadlocks=True
        )
    assert pool.acquired == [connection]


async def test_deadlock_retry_is_bounded() -> None:
    pool = _Pool([_Connection(), _Connection(), _Connection()])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)
    with pytest.raises(RuntimeError, match="ORA-00060"):
        await client._run_transaction(
            _failing_operation([RuntimeError("ORA-00060"), RuntimeError("ORA-00060")]),
            retry_deadlocks=True,
        )
    assert len(pool.acquired) == 2


def _text_commit_error() -> RuntimeError:
    return RuntimeError(
        SimpleNamespace(
            code=30600,
            full_code="ORA-30600",
            message=(
                "ORA-30600: private-detail\nDRG-50857: private-detail\nORA-20000: private-detail"
            ),
            is_session_dead=False,
            isrecoverable=False,
        )
    )


@pytest.mark.parametrize("phase", ["operation", "commit"])
async def test_transaction_failure_logs_phase_and_lower_codes_without_details(
    caplog: pytest.LogCaptureFixture,
    phase: str,
) -> None:
    """失敗段階と下位コードを記録し、原文を出さず、commit 失敗は再送しない。"""
    error = _text_commit_error()
    first = _Connection(commit_error=error if phase == "commit" else None)
    pool = _Pool([first, _Connection()])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)
    with pytest.raises(RuntimeError) as caught:
        await client._run_transaction(_failing_operation([error] if phase == "operation" else []))
    assert caught.value is error
    assert len(pool.acquired) == 1
    assert first.rollbacks == first.closes == 1
    record = next(r for r in caplog.records if r.message == "oracle_transaction_failed")
    assert record.__dict__["transaction_phase"] == phase
    assert record.__dict__["transaction_attempt"] == 1
    assert record.__dict__["oracle_error_codes"] == ["ORA-30600", "DRG-50857", "ORA-20000"]
    assert record.__dict__["full_code"] == "ORA-30600"
    assert record.exc_info is None
    assert "private-detail" not in caplog.text
    assert "private-detail" not in json.dumps(
        {key: value for key, value in record.__dict__.items() if key not in {"created", "msecs"}},
        default=str,
    )


def test_invalid_full_code_does_not_leak_error_details() -> None:
    error = RuntimeError(SimpleNamespace(full_code="ORA-30600 private-detail"))
    fields = oracle_error_log_fields(error)
    assert fields["full_code"] is None
    assert fields["oracle_error_codes"] == ["ORA-30600"]
    assert "private-detail" not in json.dumps(fields)


async def test_embedding_save_commit_failure_keeps_original_and_does_not_return_success(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """保存件数が一致しても commit 失敗なら保存成功を返さず、派生索引へ進まない。"""
    from app.rag.chunking import Chunk
    from app.rag.ingestion import IngestionPipeline
    from app.schemas.extraction import StructuredExtraction

    pool = FakeOraclePool(execute_results=[[{"cnt": 1}]])
    error = _text_commit_error()

    def fail_commit() -> None:
        raise error

    monkeypatch.setattr(pool.connection, "commit", fail_commit)
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)
    entity_save = AsyncMock()
    pipeline = IngestionPipeline(
        vlm=cast(Any, object()),
        genai=cast(Any, object()),
        oracle=client,
        object_storage=cast(Any, object()),
        settings=_llm_settings(),
    )
    monkeypatch.setattr(pipeline, "_save_entity_index_if_enabled", entity_save)
    chunk = Chunk(
        text="合成の本文",
        index=0,
        start_offset=0,
        end_offset=5,
        metadata={"document_id": "doc-a", "chunk_id": "chunk-a"},
    )
    with pytest.raises(RuntimeError) as caught:
        await pipeline._save_embeddings_for_chunk_set(
            "trace-a",
            "doc-a",
            StructuredExtraction(raw_text="合成の本文"),
            [chunk],
            [[0.1, 0.2, 0.3]],
            chunk_set_id="set-a",
        )
    assert caught.value is error
    assert pool.acquire_calls == 1
    assert pool.connection.rollbacks == pool.connection.closes == 1
    entity_save.assert_not_awaited()
    assert pool.connection.calls[-1].statement.startswith("SELECT COUNT(*)")
    assert "oracle_transaction_failed" in caplog.text
