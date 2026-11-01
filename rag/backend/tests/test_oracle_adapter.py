"""Oracle adapter 境界のテスト。"""

import json
import logging
from array import array
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from pr_backend_core.oracle_session import init_oracle_session

import app.clients.oracle as oracle_module
from app.clients.oracle import (
    DocumentDeleteBlockedByRunningIngestionError,
    OracleClient,
    OracleWalletPasswordRequiredError,
    _datetime_value,
    _test_oracle_connection_sync,
    oracle_document_schema_sql,
    oracle_evaluation_artifact_schema_sql,
    oracle_feedback_details_schema_sql,
    oracle_feedback_schema_sql,
    oracle_ingestion_audit_schema_sql,
    oracle_ingestion_job_schema_sql,
    oracle_ingestion_segment_schema_sql,
    oracle_knowledge_base_schema_sql,
    oracle_knowledge_graph_schema_sql,
    oracle_search_audit_schema_sql,
    oracle_text_terms,
    oracle_vector_schema_sql,
)
from app.config import Settings
from app.rag.chunking import Chunk
from app.rag.graph_index import (
    GraphEntity,
    GraphEntityChunkLink,
    GraphIndex,
    GraphRelationship,
)
from app.rag.request_context import (
    AuditRequestContext,
    reset_audit_request_context,
    set_audit_request_context,
)
from app.rag.search_answer_profile_config import (
    SearchAnswerProfileConfig,
    dump_search_answer_profile_config,
)
from app.schemas.document import (
    DocumentPreprocessArtifact,
    DocumentProcessingConfig,
    FileStatus,
    IngestionJob,
    IngestionJobPhase,
    IngestionJobStatus,
    IngestionSegment,
)
from app.schemas.extraction import StructuredExtraction
from app.schemas.knowledge_base import DEFAULT_KNOWLEDGE_BASE_DESCRIPTION, KnowledgeBaseStatus
from app.schemas.search import RetrievedChunk, SearchMode
from app.schemas.search_answer_profile import (
    DEFAULT_SEARCH_ANSWER_PROFILE_DESCRIPTION,
    SearchAnswerProfileStatus,
)


def test_close_oracle_pool_force_closes_busy_shared_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """設定保存時の pool 切り替えは busy connection があっても 500 にしない。"""
    pool = ForceCloseOnlyPool()
    monkeypatch.setattr(oracle_module, "_SHARED_ORACLE_POOL", pool)

    oracle_module.close_oracle_pool()

    assert pool.close_forces == [True]
    assert oracle_module._SHARED_ORACLE_POOL is None


class _RecoveringDb:
    """停止中に作った pool が起動後も接続を返さない状態を再現する fake の oracledb（#820）。"""

    POOL_GETMODE_TIMEDWAIT = 3

    def __init__(self) -> None:
        self.up = False
        self.pools: list[_RecoveringPool] = []
        self.kwargs: list[dict[str, object]] = []

    def create_pool(self, **kwargs: object) -> "_RecoveringPool":
        self.kwargs.append(kwargs)
        pool = _RecoveringPool(self, broken=not self.up)
        self.pools.append(pool)
        return pool


class _RecoveringPool:
    def __init__(self, db: _RecoveringDb, *, broken: bool) -> None:
        self._db = db
        self.broken = broken
        self.busy = 0
        self.max = 4
        self.closed = 0

    def acquire(self) -> object:
        if self.broken:
            raise RuntimeError("DPY-4005: timed out waiting for the connection pool")
        if not self._db.up:
            raise RuntimeError("DPY-6005: cannot connect to database")
        return SimpleNamespace(close=lambda: None)

    def close(self, force: bool = False) -> None:
        self.closed += 1


def test_shared_pool_created_while_db_down_recovers_after_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB の停止中に作った共有 pool を、起動後はプロセスを再起動せずに作り直して使う（#820）。"""
    db = _RecoveringDb()
    monkeypatch.setattr(oracle_module, "_SHARED_ORACLE_POOL", None)
    monkeypatch.setattr(oracle_module, "_ORACLE_POOL_CLOSER", lambda task: task())
    monkeypatch.setattr("app.clients.oracle.importlib.import_module", lambda name: db)
    settings = Settings.model_construct(
        oracle_user="rag_app",
        oracle_password="db-secret",
        oracle_dsn="ragdb_high",
        oracle_client_lib_dir="",
        oracle_wallet_dir="",
        oracle_wallet_password="",
    )
    client = OracleClient(settings=settings)
    client.connection_pool()  # 停止中に作られた pool

    db.up = True
    connection = client.acquire_connection()

    assert connection is not None
    assert len(db.pools) == 2
    assert db.pools[0].closed == 1
    # 無期限に待たない（壊れた pool を作り直せるように）。
    assert db.kwargs[0]["getmode"] == db.POOL_GETMODE_TIMEDWAIT
    assert db.kwargs[0]["wait_timeout"] == 30_000

    oracle_module.close_oracle_pool()


def test_shared_pool_is_recreated_when_database_settings_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """別の製品が保存したデータベースの設定を読み直したら、共有 pool を作り直す（#2102）。

    古い pool は貸し出し中の接続を止めずに閉じる（`close()` を force なしで呼ぶ）。
    """
    db = _RecoveringDb()
    db.up = True
    monkeypatch.setattr(oracle_module, "_SHARED_ORACLE_POOL", None)
    monkeypatch.setattr(oracle_module, "_ORACLE_POOL_CLOSER", lambda task: task())
    monkeypatch.setattr("app.clients.oracle.importlib.import_module", lambda name: db)
    settings = Settings.model_construct(
        oracle_user="rag_app",
        oracle_password="db-secret",
        oracle_dsn="ragdb_high",
        oracle_client_lib_dir="",
        oracle_wallet_dir="",
        oracle_wallet_password="",
    )
    client = OracleClient(settings=settings)
    first = client.connection_pool()
    assert client.connection_pool() is first

    # `get_settings()` が `.env` を読み直すと、同じ Settings の値が書き換わる。
    settings.oracle_dsn = "otherdb_high"
    second = client.connection_pool()

    assert second is not first
    assert [kwargs["dsn"] for kwargs in db.kwargs] == ["ragdb_high", "otherdb_high"]
    assert db.pools[0].closed == 1
    assert client.connection_pool() is second

    oracle_module.close_oracle_pool()


def test_shared_pool_injected_without_settings_key_is_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ここで作っていない pool（テストが差し込んだもの）は、設定の指紋で作り直さない。"""
    pool = object()
    monkeypatch.setattr(oracle_module, "_SHARED_ORACLE_POOL", pool)
    monkeypatch.setattr(oracle_module, "_SHARED_ORACLE_POOL_KEY", (0, "other"))
    client = OracleClient(settings=Settings.model_construct(oracle_user="u", oracle_dsn="d"))

    assert client.connection_pool() is pool


def test_datetime_value_attaches_utc_to_naive_database_values() -> None:
    """Oracle driver が naive datetime を返しても API JSON の基準時刻を失わない。"""
    value = _datetime_value(datetime(2026, 6, 23, 0, 34, 0))

    assert value.tzinfo is UTC
    assert value.isoformat() == "2026-06-23T00:34:00+00:00"


async def test_vector_index_build_params_reads_actual_index() -> None:
    """実際の索引の NEIGHBORS / EFCONSTRUCTION を v$vector_graph_index から読む(#562)。"""
    pool = FakeOraclePool(execute_results=[[{"num_neighbors": 48, "ef_construction": 800}]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    assert await client.get_vector_index_build_params() == (48, 800)
    call = pool.connection.calls[0]
    assert "FROM v$vector_graph_index" in call.statement
    assert call.parameters == {"index_name": "RAG_CHUNKS_EMBEDDING_HNSW_IDX"}


@pytest.mark.parametrize(
    ("rows", "errors"),
    [
        ([[]], []),
        ([[]], [RuntimeError("ORA-00942: table or view does not exist")]),
    ],
    ids=["no_index", "no_privilege"],
)
async def test_vector_index_build_params_unknown_when_unreadable(
    rows: list[list[dict[str, object]]], errors: list[Exception]
) -> None:
    """索引がない・V$ を読めないときは None(確認できない)で、例外にしない。"""
    pool = FakeOraclePool(execute_results=rows, fetch_errors=errors)
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    assert await client.get_vector_index_build_params() is None


async def test_vector_index_build_params_unknown_without_connection_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """接続設定がなければ DB に接続せず None を返す。"""
    monkeypatch.setattr(oracle_module, "_SHARED_ORACLE_POOL", None)
    client = OracleClient(
        settings=Settings.model_construct(oracle_user="", oracle_dsn=""),
        db_call_runner=_run_inline,
    )

    assert await client.get_vector_index_build_params() is None


@pytest.mark.anyio
async def test_ensure_default_search_answer_profile_preserves_settings_and_fixes_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既存 DEFAULT の検索設定を保持し、DEFAULT KB と有効状態だけを正規化する。"""
    client = OracleClient(settings=Settings.model_construct())
    connection = FakeOracleConnection([])
    now = datetime.now(UTC)
    knowledge_base = oracle_module.StoredKnowledgeBase(
        id="kb-default",
        name="DEFAULT",
        status=KnowledgeBaseStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    existing = oracle_module.StoredSearchAnswerProfile(
        id="bv-default",
        name="DEFAULT",
        status=SearchAnswerProfileStatus.ARCHIVED,
        created_at=now,
        updated_at=now,
        profile_config=dump_search_answer_profile_config(
            SearchAnswerProfileConfig(
                knowledge_base_ids=["kb-old"],
                query={"max_anchors_per_document": 3},
            )
        ),
        archived_at=now,
    )

    async def run_transaction(operation: Callable[[object], object]) -> object:
        return operation(connection)

    monkeypatch.setattr(client, "_run_transaction", run_transaction)
    monkeypatch.setattr(
        oracle_module,
        "_ensure_default_knowledge_base",
        lambda *_: knowledge_base,
    )
    monkeypatch.setattr(
        oracle_module,
        "_select_search_answer_profile_by_name",
        lambda *_: existing,
    )

    detail = await client.ensure_default_search_answer_profile()

    assert detail.status == SearchAnswerProfileStatus.ACTIVE
    assert detail.archived_at is None
    assert detail.config.knowledge_base_ids == ["kb-default"]
    assert detail.config.query.max_anchors_per_document == 3
    # 説明が空の既存 DEFAULT には既定の説明を補う（#521）。
    assert detail.description == DEFAULT_SEARCH_ANSWER_PROFILE_DESCRIPTION
    assert len(connection.calls) == 1
    assert "UPDATE rag_search_answer_profiles" in connection.calls[0].statement
    assert "description = :description" in connection.calls[0].statement
    assert (
        connection.calls[0].parameters["description"] == DEFAULT_SEARCH_ANSWER_PROFILE_DESCRIPTION
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("description", "expected_calls"),
    [(None, 1), ("   ", 1), ("全社の既定ビュー", 0)],
    ids=["null", "blank", "entered"],
)
async def test_ensure_default_search_answer_profile_fills_only_empty_description(
    monkeypatch: pytest.MonkeyPatch,
    description: str | None,
    expected_calls: int,
) -> None:
    """DEFAULT 検索・回答プロファイルの説明は空のとき
    だけ既定の説明で補う（利用者の説明は残す。#521）。"""
    client = OracleClient(settings=Settings.model_construct())
    connection = FakeOracleConnection([])
    now = datetime.now(UTC)
    knowledge_base = oracle_module.StoredKnowledgeBase(
        id="kb-default",
        name="DEFAULT",
        status=KnowledgeBaseStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    existing = oracle_module.StoredSearchAnswerProfile(
        id="bv-default",
        name="DEFAULT",
        description=description,
        status=SearchAnswerProfileStatus.ACTIVE,
        created_at=now,
        updated_at=now,
        profile_config=dump_search_answer_profile_config(
            SearchAnswerProfileConfig(knowledge_base_ids=["kb-default"])
        ),
    )

    async def run_transaction(operation: Callable[[object], object]) -> object:
        return operation(connection)

    monkeypatch.setattr(client, "_run_transaction", run_transaction)
    monkeypatch.setattr(oracle_module, "_ensure_default_knowledge_base", lambda *_: knowledge_base)
    monkeypatch.setattr(oracle_module, "_select_search_answer_profile_by_name", lambda *_: existing)

    detail = await client.ensure_default_search_answer_profile()

    assert len(connection.calls) == expected_calls
    if expected_calls:
        assert detail.description == DEFAULT_SEARCH_ANSWER_PROFILE_DESCRIPTION
    else:
        assert detail.description == "全社の既定ビュー"


@pytest.mark.parametrize(
    ("description", "expected_calls"),
    [(None, 1), ("", 1), ("既存の説明", 0)],
    ids=["null", "empty", "entered"],
)
def test_ensure_default_knowledge_base_fills_only_empty_description(
    monkeypatch: pytest.MonkeyPatch,
    description: str | None,
    expected_calls: int,
) -> None:
    """既存の DEFAULT ナレッジベースの説明は空のときだけ既定の説明で補う（#521）。"""
    connection = FakeOracleConnection([])
    now = datetime.now(UTC)
    existing = oracle_module.StoredKnowledgeBase(
        id="kb-default",
        name="DEFAULT",
        description=description,
        status=KnowledgeBaseStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    monkeypatch.setattr(oracle_module, "_select_knowledge_base_by_name", lambda *_: existing)

    knowledge_base = oracle_module._ensure_default_knowledge_base(cast(Any, connection), "DEFAULT")

    assert len(connection.calls) == expected_calls
    if expected_calls:
        assert knowledge_base.description == DEFAULT_KNOWLEDGE_BASE_DESCRIPTION
        assert "UPDATE rag_knowledge_bases" in connection.calls[0].statement
        assert "name =" not in connection.calls[0].statement
        assert connection.calls[0].parameters["description"] == DEFAULT_KNOWLEDGE_BASE_DESCRIPTION
    else:
        assert knowledge_base.description == "既存の説明"


def test_ensure_default_knowledge_base_creates_with_default_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEFAULT ナレッジベースを新しく作るときは既定の説明を入れる（#521）。"""
    connection = FakeOracleConnection([])
    monkeypatch.setattr(oracle_module, "_select_knowledge_base_by_name", lambda *_: None)

    knowledge_base = oracle_module._ensure_default_knowledge_base(cast(Any, connection), "DEFAULT")

    assert knowledge_base.description == DEFAULT_KNOWLEDGE_BASE_DESCRIPTION
    assert "INSERT INTO rag_knowledge_bases" in connection.calls[0].statement
    assert connection.calls[0].parameters["description"] == DEFAULT_KNOWLEDGE_BASE_DESCRIPTION


@pytest.mark.anyio
async def test_append_message_binds_clarification_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """USER のメッセージの「回答前に確認した内容」を clarification_json に保存する（#1877）。"""
    client = OracleClient(settings=Settings.model_construct())
    connection = FakeOracleConnection([])

    async def run_transaction(operation: Callable[[object], object]) -> object:
        return operation(connection)

    monkeypatch.setattr(client, "_run_transaction", run_transaction)
    monkeypatch.setattr(oracle_module, "_select_conversation", lambda *_: object())
    clarification = {
        "outcome": "answered",
        "entries": [{"prompt": "権限の付与先は？", "selected": ["グループ"], "other_text": ""}],
    }

    stored = await client.append_message(
        oracle_module.StoredMessage(
            id="m-user",
            conversation_id="c-1",
            role="USER",
            content="アクセス権限を付与したい",
            created_at=datetime.now(UTC),
            clarification=clarification,
        )
    )

    assert stored.clarification == clarification
    insert = next(call for call in connection.calls if "INSERT INTO rag_messages" in call.statement)
    assert json.loads(cast(str, insert.parameters["clarification_json"])) == clarification


def test_oracle_connection_refuses_password_wallet_without_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """パスワード必須 Wallet は SDK の対話入力へ進む前に止める。"""
    wallet_dir = tmp_path / "wallet"
    wallet_dir.mkdir()
    (wallet_dir / "tnsnames.ora").write_text("ragdb_high = ...", encoding="utf-8")
    (wallet_dir / "sqlnet.ora").write_text("WALLET_LOCATION = ...", encoding="utf-8")
    (wallet_dir / "ewallet.p12").write_bytes(b"encrypted-wallet")
    called = False

    def fake_connect(**kwargs: object) -> object:
        nonlocal called
        called = True
        return FakeOracleConnection([[{"ok": 1}]])

    monkeypatch.setattr(
        "app.clients.oracle.importlib.import_module",
        lambda name: SimpleNamespace(connect=fake_connect),
    )
    settings = Settings.model_construct(
        oracle_user="rag_app",
        oracle_password="",
        oracle_dsn="ragdb_high",
        oracle_client_lib_dir="",
        oracle_wallet_dir=str(wallet_dir),
        oracle_wallet_password="",
    )

    with pytest.raises(OracleWalletPasswordRequiredError, match="Wallet パスワード"):
        _test_oracle_connection_sync(settings)

    assert called is False


def test_oracle_connection_uses_auto_login_wallet_without_wallet_password(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """cwallet.sso がある Wallet は Wallet パスワードなしで接続 kwargs を作る。"""
    wallet_dir = tmp_path / "wallet"
    wallet_dir.mkdir()
    (wallet_dir / "tnsnames.ora").write_text("ragdb_high = ...", encoding="utf-8")
    (wallet_dir / "sqlnet.ora").write_text("WALLET_LOCATION = ...", encoding="utf-8")
    (wallet_dir / "cwallet.sso").write_bytes(b"auto-login-wallet")
    captured: dict[str, object] = {}

    def fake_connect(**kwargs: object) -> object:
        captured.update(kwargs)
        return FakeOracleConnection([[{"ok": 1}]])

    monkeypatch.setattr(
        "app.clients.oracle.importlib.import_module",
        lambda name: SimpleNamespace(connect=fake_connect),
    )
    settings = Settings.model_construct(
        oracle_user="rag_app",
        oracle_password="",
        oracle_dsn="ragdb_high",
        oracle_client_lib_dir="",
        oracle_wallet_dir=str(wallet_dir),
        oracle_wallet_password="",
    )

    _test_oracle_connection_sync(settings)

    assert captured["config_dir"] == str(wallet_dir)
    assert captured["wallet_location"] == str(wallet_dir)
    assert captured["tcp_connect_timeout"] == 10.0
    assert "wallet_password" not in captured


def test_oracle_connection_initializes_instant_client_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """RAG も nl2sql と同じく PLATFORM_ORACLE_CLIENT_LIB_DIR があれば thick client を使う。"""
    wallet_dir = tmp_path / "instantclient_23_26" / "network" / "admin"
    wallet_dir.mkdir(parents=True)
    (wallet_dir / "tnsnames.ora").write_text("ragdb_high = ...", encoding="utf-8")
    (wallet_dir / "sqlnet.ora").write_text("WALLET_LOCATION = ...", encoding="utf-8")
    (wallet_dir / "cwallet.sso").write_bytes(b"auto-login-wallet")
    calls: list[tuple[str, object]] = []

    def fake_init_oracle_client(*, lib_dir: str) -> None:
        calls.append(("init", lib_dir))

    def fake_connect(**kwargs: object) -> object:
        calls.append(("connect", kwargs["dsn"]))
        return FakeOracleConnection([[{"ok": 1}]])

    monkeypatch.setattr(oracle_module, "_ORACLE_CLIENT_INITIALIZED_LIB_DIR", None)
    monkeypatch.setattr(
        "app.clients.oracle.importlib.import_module",
        lambda name: SimpleNamespace(
            connect=fake_connect,
            init_oracle_client=fake_init_oracle_client,
        ),
    )
    settings = Settings.model_construct(
        oracle_user="rag_app",
        oracle_password="db-secret",
        oracle_dsn="ragdb_high",
        oracle_client_lib_dir=str(tmp_path / "instantclient_23_26"),
        oracle_wallet_dir="",
        oracle_wallet_password="",
    )

    _test_oracle_connection_sync(settings)

    assert calls[0] == ("init", str(tmp_path / "instantclient_23_26"))
    assert calls[1] == ("connect", "ragdb_high")


_WALLET_HIGH = (
    "(description= (retry_count=20)(retry_delay=3)(address=(protocol=tcps)(port=1522)"
    "(host=adb.example.com))(connect_data=(service_name=x_ragdb_high.adb.oraclecloud.com))"
    "(security=(ssl_server_dn_match=yes)))"
)


@pytest.mark.parametrize("cli", [False, True], ids=["server", "cli"])
def test_shared_pool_keeps_tns_retry_only_for_the_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cli: bool
) -> None:
    """常駐の server の共有 pool は Wallet の別名の DSN のまま（TNS の再試行で一時の切断に強い）。

    CLI（`enable_cli_connect_mode`）だけ、TNS の再試行を外した記述子・`retry_count=0`・短い
    `tcp_connect_timeout` で作る（DB の停止中に 1〜2 分待たない。#1779）。
    """
    from pr_backend_core.oracle_dsn import CLI_TCP_CONNECT_TIMEOUT_SECONDS, enable_cli_connect_mode

    (tmp_path / "tnsnames.ora").write_text(f"ragdb_high = {_WALLET_HIGH}\n", encoding="utf-8")
    created: list[dict[str, object]] = []
    pool = FakeOraclePool()

    def fake_create_pool(**kwargs: object) -> object:
        created.append(kwargs)
        return pool

    monkeypatch.setattr(oracle_module, "_SHARED_ORACLE_POOL", None)
    monkeypatch.setattr(
        "app.clients.oracle.importlib.import_module",
        lambda name: SimpleNamespace(create_pool=fake_create_pool, POOL_GETMODE_TIMEDWAIT=3),
    )
    settings = Settings.model_construct(
        oracle_user="rag_app",
        oracle_password="db-secret",
        oracle_dsn="ragdb_high",
        oracle_client_lib_dir="",
        oracle_wallet_dir=str(tmp_path),
        oracle_wallet_password="",
        oracle_tcp_connect_timeout_seconds=10.0,
        oracle_pool_max=4,
    )
    if cli:
        enable_cli_connect_mode()

    assert OracleClient(settings=settings).connection_pool() is pool

    kwargs = created[0]
    assert kwargs["config_dir"] == str(tmp_path) and kwargs["getmode"] == 3
    if cli:
        assert kwargs["dsn"] == _WALLET_HIGH.replace("(retry_count=20)(retry_delay=3)", "")
        assert kwargs["retry_count"] == 0 and kwargs["retry_delay"] == 0
        assert kwargs["tcp_connect_timeout"] == CLI_TCP_CONNECT_TIMEOUT_SECONDS
    else:
        assert kwargs["dsn"] == "ragdb_high"
        assert "retry_count" not in kwargs and "retry_delay" not in kwargs
        assert kwargs["tcp_connect_timeout"] == 10.0
    # 診断の直接の接続も、その pool と同じ接続先を使う（#1744）。
    assert oracle_module.shared_pool_connect_kwargs(settings)["dsn"] == kwargs["dsn"]


def test_oracle_pool_initializes_instant_client_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """共有 pool 作成も thin ではなく nl2sql と同じ thick client に寄せる。"""
    calls: list[tuple[str, object]] = []
    pool = FakeOraclePool()

    def fake_init_oracle_client(*, lib_dir: str) -> None:
        calls.append(("init", lib_dir))

    def fake_create_pool(**kwargs: object) -> object:
        calls.append(("create_pool", kwargs["dsn"]))
        calls.append(("session_callback", kwargs.get("session_callback")))
        return pool

    monkeypatch.setattr(oracle_module, "_SHARED_ORACLE_POOL", None)
    monkeypatch.setattr(oracle_module, "_ORACLE_CLIENT_INITIALIZED_LIB_DIR", None)
    monkeypatch.setattr(
        "app.clients.oracle.importlib.import_module",
        lambda name: SimpleNamespace(
            create_pool=fake_create_pool,
            init_oracle_client=fake_init_oracle_client,
            POOL_GETMODE_TIMEDWAIT=3,
        ),
    )
    settings = Settings.model_construct(
        oracle_user="rag_app",
        oracle_password="db-secret",
        oracle_dsn="ragdb_high",
        oracle_client_lib_dir=str(tmp_path / "instantclient_23_26"),
        oracle_wallet_dir="",
        oracle_wallet_password="",
    )

    client = OracleClient(settings=settings)

    assert client.connection_pool() is pool
    assert calls[0] == ("init", str(tmp_path / "instantclient_23_26"))
    assert calls[1] == ("create_pool", "ragdb_high")
    # 新しい接続ごとに result cache を無効にする（ADB の内部エラーを避ける。#333）。
    assert calls[2] == ("session_callback", init_oracle_session)


def test_oracle_connection_uses_database_password_as_wallet_password(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Wallet パスワード未入力時は DB password を wallet_password に使う。"""
    wallet_dir = tmp_path / "wallet"
    wallet_dir.mkdir()
    (wallet_dir / "tnsnames.ora").write_text("ragdb_high = ...", encoding="utf-8")
    (wallet_dir / "sqlnet.ora").write_text("WALLET_LOCATION = ...", encoding="utf-8")
    (wallet_dir / "cwallet.sso").write_bytes(b"auto-login-wallet")
    (wallet_dir / "ewallet.pem").write_text(
        "-----BEGIN ENCRYPTED PRIVATE KEY-----\nabc\n-----END ENCRYPTED PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_connect(**kwargs: object) -> object:
        captured.update(kwargs)
        return FakeOracleConnection([[{"ok": 1}]])

    monkeypatch.setattr(
        "app.clients.oracle.importlib.import_module",
        lambda name: SimpleNamespace(connect=fake_connect),
    )
    settings = Settings.model_construct(
        oracle_user="rag_app",
        oracle_password="db-secret",
        oracle_dsn="ragdb_high",
        oracle_client_lib_dir="",
        oracle_wallet_dir=str(wallet_dir),
        oracle_wallet_password="",
    )

    _test_oracle_connection_sync(settings)

    assert captured["wallet_password"] == "db-secret"
    assert captured["tcp_connect_timeout"] == 10.0


def test_oracle_connection_test_strips_wallet_retry_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """接続テストは ADB Wallet alias の長い retry 設定を外して即時診断しやすくする。"""
    wallet_dir = tmp_path / "wallet"
    wallet_dir.mkdir()
    (wallet_dir / "tnsnames.ora").write_text(
        "ragdb_high = (description=(retry_count=20)(retry_delay=3)"
        "(address=(protocol=tcps)(port=1522)(host=adb.example.com))"
        "(connect_data=(service_name=ragdb_high.adb.oraclecloud.com)))",
        encoding="utf-8",
    )
    (wallet_dir / "sqlnet.ora").write_text("WALLET_LOCATION = ...", encoding="utf-8")
    (wallet_dir / "cwallet.sso").write_bytes(b"auto-login-wallet")
    captured: dict[str, object] = {}

    def fake_connect(**kwargs: object) -> object:
        captured.update(kwargs)
        return FakeOracleConnection([[{"ok": 1}]])

    monkeypatch.setattr(
        "app.clients.oracle.importlib.import_module",
        lambda name: SimpleNamespace(connect=fake_connect),
    )
    settings = Settings.model_construct(
        oracle_user="rag_app",
        oracle_password="db-secret",
        oracle_dsn="ragdb_high",
        oracle_client_lib_dir="",
        oracle_wallet_dir=str(wallet_dir),
        oracle_wallet_password="",
    )

    _test_oracle_connection_sync(settings)

    assert "(retry_count=" not in str(captured["dsn"]).lower()
    assert "(retry_delay=" not in str(captured["dsn"]).lower()
    assert captured["retry_count"] == 0
    assert captured["retry_delay"] == 0


async def test_oracle_client_persists_documents_through_pool() -> None:
    """document persistence は Oracle pool 経由で実行される。"""
    pool = FakeOraclePool(execute_results=[[]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    detail = await client.create_document(
        file_name="policy.txt",
        object_storage_path="oci://bucket/policy.txt",
        content_type="text/plain",
    )

    assert detail.file_name == "policy.txt"
    assert detail.knowledge_bases[0].name == "DEFAULT"
    assert pool.connection.commits == 1
    statements = [call.statement for call in pool.connection.calls]
    assert any("INSERT INTO rag_knowledge_bases" in statement for statement in statements)
    assert any("INSERT INTO rag_documents" in statement for statement in statements)
    assert pool.connection.many_calls
    assert "INSERT INTO rag_document_knowledge_bases" in pool.connection.many_calls[0].statement


async def test_oracle_client_get_document_restores_preprocess_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """文書詳細は保存済みファイル準備 artifact を欠落させず返す。"""
    row = _oracle_document_row(status="PREPROCESSED")
    row["preprocess_artifact"] = json.dumps(
        {
            "derivation_id": "derivation-1",
            "profile": "office_to_pdf",
            "converted": True,
            "object_storage_path": "oci://namespace/bucket/artifacts/canonical/doc-1.pdf",
            "content_type": "application/pdf",
            "sha256": "b" * 64,
            "file_name": "policy.pdf",
        }
    )
    pool = FakeOraclePool(
        execute_results=[
            [row],
            [{"document_id": "doc-1", "knowledge_base_id": "kb-1", "name": "社内規程"}],
        ]
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)
    # 公開版の取得は独立に検証し、このテストでは元の文書 SQL の応答だけを供給する。
    monkeypatch.setattr(client, "document_language_pack_uses", AsyncMock(return_value={}))

    detail = await client.get_document("doc-1")

    assert "preprocess_artifact" in pool.connection.calls[0].statement
    assert detail is not None
    assert detail.preprocess_artifact is not None
    assert (
        detail.preprocess_artifact.object_storage_path
        == "oci://namespace/bucket/artifacts/canonical/doc-1.pdf"
    )
    assert detail.preprocess_artifact.content_type == "application/pdf"
    assert detail.preprocess_artifact.sha256 == "b" * 64


async def test_oracle_read_retries_one_recoverable_disconnect(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """recoverable な読取切断は新しい接続で1回だけ再試行する。"""
    disconnect = RuntimeError(SimpleNamespace(isrecoverable=True, full_code="DPY-4011"))
    pool = FakeOraclePool(
        execute_results=[[], [{"ok": 1}]],
        fetch_errors=[disconnect],
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    with caplog.at_level(logging.WARNING, logger="app.clients.oracle"):
        rows = await client._fetch_all("SELECT 1 AS ok FROM DUAL")

    assert rows == [{"ok": 1}]
    assert pool.acquire_calls == 2
    record = next(item for item in caplog.records if item.message == "oracle_read_retry")
    assert record.__dict__["error_type"] == "RuntimeError"
    assert record.__dict__["full_code"] == "DPY-4011"
    assert "oracle_error_code" in record.__dict__


@pytest.mark.parametrize(("recoverable", "expected_attempts"), [(True, 2), (False, 1)])
async def test_oracle_read_does_not_retry_more_than_once_or_nonrecoverable_errors(
    recoverable: bool,
    expected_attempts: int,
) -> None:
    """再切断は2回で止め、非recoverable例外は即時に返す。"""
    errors = [
        RuntimeError(SimpleNamespace(isrecoverable=recoverable, full_code="DPY-4011"))
        for _ in range(expected_attempts)
    ]
    pool = FakeOraclePool(
        execute_results=[[] for _ in range(expected_attempts)],
        fetch_errors=errors,
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    with pytest.raises(RuntimeError):
        await client._fetch_all("SELECT 1 AS ok FROM DUAL")

    assert pool.acquire_calls == expected_attempts


def test_fetch_all_preserves_fetch_error_when_cursor_close_also_fails() -> None:
    """切断後の cursor.close 失敗で元の fetch 例外を上書きしない。"""

    def fail(message: str) -> None:
        raise RuntimeError(message)

    cursor = SimpleNamespace(
        description=None,
        execute=lambda *_args: None,
        fetchall=lambda: fail("fetch failed"),
        close=lambda: fail("close failed"),
    )
    connection: Any = SimpleNamespace(cursor=lambda: cursor)

    with pytest.raises(RuntimeError, match="fetch failed"):
        oracle_module._fetch_all(connection, "SELECT 1 FROM DUAL", {})


async def test_oracle_client_assigns_uploaded_document_to_selected_knowledge_base() -> None:
    """upload 時の KB 指定は既定 KB ではなく指定先 membership を作る。"""
    pool = FakeOraclePool(execute_results=[[_oracle_knowledge_base_row()]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    detail = await client.create_document(
        file_name="policy.txt",
        object_storage_path="oci://bucket/policy.txt",
        content_type="text/plain",
        knowledge_base_ids=["kb-1"],
    )

    assert len(detail.knowledge_bases) == 1
    assert detail.knowledge_bases[0].id == "kb-1"
    statements = [call.statement for call in pool.connection.calls]
    assert not any("INSERT INTO rag_knowledge_bases" in statement for statement in statements)
    assert pool.connection.many_calls[0].rows[0]["knowledge_base_id"] == "kb-1"


async def test_oracle_client_persists_ingestion_job() -> None:
    """取込 job は Oracle queue table へ保存する。"""
    pool = FakeOraclePool(execute_results=[[_oracle_document_row()]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)
    job = IngestionJob(
        id="job-1",
        document_id="doc-1",
        status=IngestionJobStatus.QUEUED,
        parser_profile="enterprise_ai_pdf_layout",
        quality_warnings=["table_structure_review"],
        queued_at=datetime(2026, 1, 2, tzinfo=UTC),
    )

    created = await client.create_ingestion_job(job)

    assert created == job
    statements = [call.statement for call in pool.connection.calls]
    assert any("INSERT INTO rag_ingestion_jobs" in statement for statement in statements)
    insert_call = next(
        call for call in pool.connection.calls if "INSERT INTO rag_ingestion_jobs" in call.statement
    )
    assert insert_call.parameters["job_id"] == "job-1"
    assert insert_call.parameters["parser_profile"] == "enterprise_ai_pdf_layout"
    assert insert_call.parameters["quality_warnings"] == '["table_structure_review"]'
    assert insert_call.parameters["attempt_count"] == 0
    assert insert_call.parameters["max_attempts"] == 3


async def test_oracle_client_creates_recipe_job_after_locked_revision_check() -> None:
    """recipe job はアクセス条件付き row lock と active/revision 検証後に作る。"""
    pool = FakeOraclePool(execute_results=[[{"config_revision": 3}], []])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)
    job = IngestionJob(
        id="job-recipe",
        document_id="doc-1",
        recipe_id="recipe-1",
        recipe_revision=3,
        status=IngestionJobStatus.QUEUED,
        phase=IngestionJobPhase.EXTRACT,
        parser_profile="enterprise_ai_pdf_layout",
        queued_at=datetime(2026, 1, 2, tzinfo=UTC),
    )

    await client.create_ingestion_job(job)

    lock_call = pool.connection.calls[0]
    assert "JOIN rag_documents d" in lock_call.statement
    assert "r.document_id = :document_id" in lock_call.statement
    assert "r.recipe_id = :recipe_id" in lock_call.statement
    assert "FOR UPDATE OF r.recipe_id" in lock_call.statement
    assert any("INSERT INTO rag_ingestion_jobs" in call.statement for call in pool.connection.calls)


@pytest.mark.parametrize(
    ("execute_results", "message"),
    [
        ([], "レシピが見つかりません"),
        ([{"config_revision": 4}], "レシピ設定が更新されました"),
        ([{"config_revision": 3}], "処理中または待機中"),
    ],
)
async def test_oracle_client_rejects_recipe_job_before_insert(
    execute_results: list[dict[str, object]],
    message: str,
) -> None:
    """scope/revision/active 競合時は同一 transaction で INSERT 前に拒否する。"""
    results = [execute_results]
    if message == "処理中または待機中":
        results.append([{"job_id": "active-job"}])
    pool = FakeOraclePool(execute_results=results)
    client = OracleClient(
        settings=_llm_settings(),
        pool=pool,
        db_call_runner=_run_inline,
    )
    job = IngestionJob(
        id="job-recipe",
        document_id="doc-1",
        recipe_id="recipe-1",
        recipe_revision=3,
        status=IngestionJobStatus.QUEUED,
        phase=IngestionJobPhase.EXTRACT,
        parser_profile="enterprise_ai_pdf_layout",
        queued_at=datetime(2026, 1, 2, tzinfo=UTC),
    )

    with pytest.raises((KeyError, ValueError), match=message):
        await client.create_ingestion_job(job)

    assert not any(
        "INSERT INTO rag_ingestion_jobs" in call.statement for call in pool.connection.calls
    )


async def test_oracle_client_rejects_recipe_update_before_dml_when_scope_mismatches() -> None:
    """document/recipe scope が一致しなければ設定 UPDATE を行わない。"""
    pool = FakeOraclePool(execute_results=[[]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    with pytest.raises(KeyError, match="レシピが見つかりません"):
        await client.update_document_recipe_config(
            "doc-1", "recipe-other-document", DocumentProcessingConfig()
        )

    assert not any(
        "UPDATE rag_document_recipes" in call.statement for call in pool.connection.calls
    )


async def test_oracle_client_updates_recipe_status_without_null_timestamp_binds() -> None:
    """日時は DB 側で設定し、Oracle が NULL bind を CHAR と推論するのを避ける。"""
    pool = FakeOraclePool(execute_results=[])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    await client.update_document_recipe_status(
        recipe_id="recipe-1",
        status=FileStatus.PREPROCESSED,
    )

    call = pool.connection.calls[0]
    assert "COALESCE(:started_at" not in call.statement
    assert "THEN SYSTIMESTAMP ELSE started_at" in call.statement
    assert "WHEN :status IN ('INDEXED', 'ERROR') THEN SYSTIMESTAMP" in call.statement
    assert "started_at" not in call.parameters
    assert "finished_at" not in call.parameters


async def test_oracle_client_rejects_recipe_delete_before_dml_when_scope_mismatches() -> None:
    """document は見えても recipe が別文書なら削除 DML を行わない。"""
    pool = FakeOraclePool(execute_results=[[{"document_id": "doc-1"}], []])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    with pytest.raises(KeyError, match="レシピが見つかりません"):
        await client.delete_document_recipe("doc-1", "recipe-other-document")

    assert not any("DELETE FROM" in call.statement for call in pool.connection.calls)


async def test_oracle_client_persists_ingestion_job_without_max_attempts_column() -> None:
    """旧 queue table では max_attempts 列を省いて取込 job を保存する。"""
    pool = FakeOraclePool(
        execute_results=[[_oracle_document_row()]],
        missing_ingestion_job_max_attempts=True,
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)
    job = IngestionJob(
        id="job-1",
        document_id="doc-1",
        status=IngestionJobStatus.QUEUED,
        parser_profile="enterprise_ai_pdf_layout",
        queued_at=datetime(2026, 1, 2, tzinfo=UTC),
    )

    created = await client.create_ingestion_job(job)

    assert created == job
    insert_calls = [
        call for call in pool.connection.calls if "INSERT INTO rag_ingestion_jobs" in call.statement
    ]
    assert len(insert_calls) == 2
    assert "max_attempts" in insert_calls[0].statement
    assert "max_attempts" not in insert_calls[1].statement


async def test_oracle_client_lists_and_counts_ingestion_jobs() -> None:
    """取込 job 一覧は document access predicate 付きで取得する。"""
    pool = FakeOraclePool(
        execute_results=[
            [_oracle_ingestion_job_row()],
            [{"count_value": 1}],
        ]
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    jobs = await client.list_ingestion_jobs(
        status=IngestionJobStatus.QUEUED,
        limit=10,
        offset=0,
    )
    count = await client.count_ingestion_jobs(status=IngestionJobStatus.QUEUED)

    assert count == 1
    assert len(jobs) == 1
    assert jobs[0].id == "job-1"
    assert jobs[0].status == IngestionJobStatus.QUEUED
    assert jobs[0].quality_warnings == ["table_structure_review"]
    list_call = pool.connection.calls[0]
    assert "FROM rag_ingestion_jobs j" in list_call.statement
    assert "JOIN rag_documents d" in list_call.statement
    assert "j.status = :ingestion_job_status" in list_call.statement
    assert "FETCH NEXT :limit ROWS ONLY" in list_call.statement
    assert list_call.parameters["ingestion_job_status"] == "QUEUED"
    assert list_call.parameters["limit"] == 10
    count_call = pool.connection.calls[1]
    assert "j.status = :ingestion_job_status" in count_call.statement
    assert count_call.parameters["ingestion_job_status"] == "QUEUED"


async def test_oracle_client_lists_document_ingestion_jobs() -> None:
    """指定 document の取込 job 一覧は document_id と状態で絞り込める。"""
    pool = FakeOraclePool(execute_results=[[_oracle_ingestion_job_row(status="RUNNING")]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    jobs = await client.list_document_ingestion_jobs(
        "doc-1",
        status=IngestionJobStatus.RUNNING,
    )

    assert len(jobs) == 1
    assert jobs[0].document_id == "doc-1"
    assert jobs[0].status == IngestionJobStatus.RUNNING
    call = pool.connection.calls[0]
    assert "FROM rag_ingestion_jobs j" in call.statement
    assert "JOIN rag_documents d" in call.statement
    assert "j.document_id = :document_id" in call.statement
    assert "j.status = :ingestion_job_status" in call.statement
    assert call.parameters["document_id"] == "doc-1"
    assert call.parameters["ingestion_job_status"] == "RUNNING"


async def test_oracle_client_ingestion_jobs_carry_document_file_name() -> None:
    """取込 job の一覧・取得は文書のファイル名を返す（一覧で文書 ID ではなく名前を出す。#306）。"""
    row = {**_oracle_ingestion_job_row(), "document_file_name": "経費規程.pdf"}
    pool = FakeOraclePool(execute_results=[[row], [row], [row]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    listed = await client.list_ingestion_jobs(limit=5, offset=0)
    fetched = await client.get_ingestion_job("job-1")
    by_document = await client.list_document_ingestion_jobs("doc-1")

    assert listed[0].document_file_name == "経費規程.pdf"
    assert fetched is not None
    assert fetched.document_file_name == "経費規程.pdf"
    assert by_document[0].document_file_name == "経費規程.pdf"
    for call in pool.connection.calls:
        assert "d.file_name AS document_file_name" in call.statement


async def test_oracle_client_ingestion_job_without_document_file_name_is_none() -> None:
    """ファイル名の列がない行（作成直後の job 等）は None のまま扱う。"""
    pool = FakeOraclePool(execute_results=[[_oracle_ingestion_job_row()]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    jobs = await client.list_ingestion_jobs(limit=5, offset=0)

    assert jobs[0].document_file_name is None


async def test_oracle_client_transition_ingestion_job_uses_status_condition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """状態遷移は遷移元の status を WHERE に入れた 1 回の UPDATE で行う(#305)。"""
    finished_at = datetime(2026, 1, 2, 0, 5, tzinfo=UTC)
    pool = FakeOraclePool(
        execute_results=[[_oracle_ingestion_job_row(status="CANCELLED")]],
    )
    monkeypatch.setattr(FakeOracleCursor, "rowcount", 1, raising=False)
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    updated = await client.transition_ingestion_job(
        "job-1",
        from_statuses=(IngestionJobStatus.QUEUED, IngestionJobStatus.RUNNING),
        to_status=IngestionJobStatus.CANCELLED,
        error_message="利用者によりキャンセルされました。",
        finished_at=finished_at,
    )

    assert updated is not None
    assert updated.status == IngestionJobStatus.CANCELLED
    update_call = pool.connection.calls[0]
    assert "UPDATE rag_ingestion_jobs" in update_call.statement
    assert "status IN (:from_status_0, :from_status_1)" in update_call.statement
    assert "EXISTS (" in update_call.statement
    assert update_call.parameters["from_status_0"] == "QUEUED"
    assert update_call.parameters["from_status_1"] == "RUNNING"
    assert update_call.parameters["to_status"] == "CANCELLED"
    assert update_call.parameters["finished_at"] == finished_at
    assert pool.connection.commits == 1


async def test_oracle_client_transition_ingestion_job_returns_none_when_no_row_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """遷移元の status でなければ 0 行更新になり、None を返して他者の状態を上書きしない。"""
    pool = FakeOraclePool(execute_results=[])
    monkeypatch.setattr(FakeOracleCursor, "rowcount", 0, raising=False)
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    updated = await client.transition_ingestion_job(
        "job-1",
        from_statuses=(IngestionJobStatus.RUNNING,),
        to_status=IngestionJobStatus.SUCCEEDED,
        finished_at=datetime(2026, 1, 2, 0, 5, tzinfo=UTC),
    )

    assert updated is None
    assert len(pool.connection.calls) == 1
    assert "status IN (:from_status_0)" in pool.connection.calls[0].statement
    # lease を渡さない遷移(利用者の取り消し・lease を持たない実行)は lease を条件にしない。
    assert "lease_owner" not in pool.connection.calls[0].statement
    assert "lease_owner" not in pool.connection.calls[0].parameters


async def test_oracle_client_transition_ingestion_job_with_lease_requires_own_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lease を渡した遷移は、自分の lease の行だけを書く(再 claim 後の上書きを防ぐ。#359)。"""
    pool = FakeOraclePool(execute_results=[])
    monkeypatch.setattr(FakeOracleCursor, "rowcount", 0, raising=False)
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    updated = await client.transition_ingestion_job(
        "job-1",
        from_statuses=(IngestionJobStatus.RUNNING,),
        to_status=IngestionJobStatus.SUCCEEDED,
        finished_at=datetime(2026, 1, 2, 0, 5, tzinfo=UTC),
        lease_owner="host:1:worker",
    )

    assert updated is None
    [update_call] = pool.connection.calls
    assert "UPDATE rag_ingestion_jobs" in update_call.statement
    assert "status IN (:from_status_0)" in update_call.statement
    assert "AND lease_owner = :lease_owner" in update_call.statement
    assert update_call.parameters["lease_owner"] == "host:1:worker"
    # 0 行なら最新行を読み直さない(古い実行の結果として捨てる)。
    assert pool.connection.commits == 1


async def test_oracle_client_reads_ingestion_job_lease() -> None:
    """job の状態と lease の持ち主を、job の取得と同じ範囲の条件で読む(#359)。"""
    pool = FakeOraclePool(
        execute_results=[[{"status": "RUNNING", "lease_owner": "host:2:other"}], []]
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    lease = await client.get_ingestion_job_lease("job-1")
    missing = await client.get_ingestion_job_lease("job-missing")

    assert lease is not None
    assert lease.status == IngestionJobStatus.RUNNING
    assert lease.lease_owner == "host:2:other"
    assert missing is None
    call = pool.connection.calls[0]
    assert "SELECT j.status, j.lease_owner" in call.statement
    assert "JOIN rag_documents d" in call.statement
    assert call.parameters["job_id"] == "job-1"


async def test_oracle_client_recovers_stale_ingestion_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stale RUNNING job は試行回数に応じて再キューまたは失敗へ戻す。"""
    monkeypatch.setattr(FakeOracleCursor, "rowcount", 1, raising=False)
    stale_at = datetime(2026, 1, 2, 1, 0, tzinfo=UTC)
    pool = FakeOraclePool(
        execute_results=[
            [
                _oracle_ingestion_job_row(
                    status="RUNNING",
                    attempt_count=1,
                    max_attempts=3,
                    started_at=datetime(2026, 1, 2, 0, 0, tzinfo=UTC),
                ),
                _oracle_ingestion_job_row(
                    job_id="job-maxed",
                    status="RUNNING",
                    attempt_count=3,
                    max_attempts=3,
                    started_at=datetime(2026, 1, 2, 0, 0, tzinfo=UTC),
                ),
            ],
        ]
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    recovered = await client.recover_stale_ingestion_jobs(stale_before=stale_at, limit=10)

    assert [job.id for job in recovered] == ["job-1", "job-maxed"]
    assert pool.connection.commits == 1
    select_call = pool.connection.calls[0]
    assert "j.status = 'RUNNING'" in select_call.statement
    assert "COALESCE(j.started_at, j.queued_at) < :stale_before" in select_call.statement
    # heartbeat のある job は lease の TTL で判定する(#357)。heartbeat と同じ DB の時計で比べ、
    # worker の時計に依存しない(#359)。省略時は設定の TTL を使う。
    assert "j.heartbeat_at IS NULL" in select_call.statement
    assert (
        "j.heartbeat_at < SYSTIMESTAMP - NUMTODSINTERVAL(:lease_ttl_seconds, 'SECOND')"
        in select_call.statement
    )
    assert "heartbeat_stale_before" not in select_call.statement
    assert "exclude_lease_owner" not in select_call.statement
    assert select_call.parameters["stale_before"] == stale_at
    assert (
        select_call.parameters["lease_ttl_seconds"]
        == _llm_settings().ingestion_queue_lease_ttl_seconds
    )
    assert select_call.parameters["limit"] == 10
    update_calls = [
        call for call in pool.connection.calls[1:] if "UPDATE rag_ingestion_jobs" in call.statement
    ]
    requeue_call = next(call for call in update_calls if "SET status = 'QUEUED'" in call.statement)
    assert any("SET status = 'FAILED'" in call.statement for call in update_calls)
    # 戻す UPDATE も RUNNING かつ stale のときだけ書き、lease を外す。
    for call in update_calls:
        assert "AND status = 'RUNNING'" in call.statement
        assert (
            "heartbeat_at < SYSTIMESTAMP - NUMTODSINTERVAL(:lease_ttl_seconds, 'SECOND')"
            in call.statement
        )
    assert "lease_owner = NULL" in requeue_call.statement
    assert "heartbeat_at = NULL" in requeue_call.statement
    # 固着防止: 再キュー/失敗時は文書状態も復旧する。
    document_updates = [
        call for call in pool.connection.calls if "UPDATE rag_documents" in call.statement
    ]
    document_statuses = {call.parameters.get("status") for call in document_updates}
    assert "UPLOADED" in document_statuses  # 再キューした EXTRACT job の文書
    assert "ERROR" in document_statuses  # 試行上限超過 job の文書
    assert not any("DELETE FROM rag_chunks" in call.statement for call in pool.connection.calls)


async def test_oracle_client_recovers_stale_recipe_jobs_without_touching_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """recipe job の再試行/上限超過は対象 recipe の状態だけを復旧する。"""
    monkeypatch.setattr(FakeOracleCursor, "rowcount", 1, raising=False)
    stale_at = datetime(2026, 1, 2, 1, 0, tzinfo=UTC)
    retry_job = {
        **_oracle_ingestion_job_row(
            status="RUNNING",
            attempt_count=1,
            max_attempts=3,
            started_at=datetime(2026, 1, 2, 0, 0, tzinfo=UTC),
        ),
        "recipe_id": "recipe-retry",
        "recipe_revision": 2,
        "phase": "EXTRACT",
    }
    maxed_job = {
        **_oracle_ingestion_job_row(
            job_id="job-maxed",
            status="RUNNING",
            attempt_count=3,
            max_attempts=3,
            started_at=datetime(2026, 1, 2, 0, 0, tzinfo=UTC),
        ),
        "recipe_id": "recipe-maxed",
        "recipe_revision": 4,
        "phase": "INDEX",
    }
    pool = FakeOraclePool(execute_results=[[retry_job, maxed_job], [], []])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    await client.recover_stale_ingestion_jobs(stale_before=stale_at, limit=10)

    recipe_updates = [
        call for call in pool.connection.calls if "UPDATE rag_document_recipes r" in call.statement
    ]
    assert {call.parameters["recipe_id"] for call in recipe_updates} == {
        "recipe-retry",
        "recipe-maxed",
    }
    retry_update = next(
        call for call in recipe_updates if call.parameters["recipe_id"] == "recipe-retry"
    )
    assert retry_update.parameters["status"] == "PREPROCESSED"
    maxed_update = next(
        call for call in recipe_updates if call.parameters["recipe_id"] == "recipe-maxed"
    )
    assert maxed_update.parameters["status"] == "ERROR"
    assert maxed_update.parameters["phase"] == "INDEX"
    assert not any("UPDATE rag_documents" in call.statement for call in pool.connection.calls)


async def test_oracle_client_recovers_orphaned_recipe_only() -> None:
    """active job の無い processing recipe を ERROR にして文書集約は維持する。"""
    pool = FakeOraclePool(
        execute_results=[[], [], [{"recipe_id": "recipe-stuck", "status": "CHUNKING"}]]
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    await client.recover_stale_ingestion_jobs(
        stale_before=datetime(2026, 1, 2, 1, 0, tzinfo=UTC), limit=10
    )

    recipe_update = next(
        call for call in pool.connection.calls if "UPDATE rag_document_recipes r" in call.statement
    )
    assert recipe_update.parameters["recipe_id"] == "recipe-stuck"
    assert recipe_update.parameters["status"] == "ERROR"
    assert recipe_update.parameters["phase"] == "CHUNK"
    assert not any("UPDATE rag_documents" in call.statement for call in pool.connection.calls)


async def test_oracle_client_recovers_orphaned_ingesting_document() -> None:
    """active な job が無いのに INGESTING で取り残された文書を ERROR へ復旧する。"""
    stale_at = datetime(2026, 1, 2, 1, 0, tzinfo=UTC)
    pool = FakeOraclePool(
        execute_results=[
            [],  # stale RUNNING job は無い
            [{"document_id": "doc-stuck"}],  # orphan 文書
        ]
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    recovered = await client.recover_stale_ingestion_jobs(stale_before=stale_at, limit=10)

    assert recovered == []
    orphan_select = next(
        call
        for call in pool.connection.calls
        if "FROM rag_documents d" in call.statement
        and "NOT EXISTS" in call.statement
        and call.statement.strip().startswith("SELECT")
    )
    assert (
        "d.status IN ('PREPROCESSING', 'INGESTING', 'CHUNKING', 'INDEXING')"
        in orphan_select.statement
    )
    assert not any("DELETE FROM rag_chunks" in call.statement for call in pool.connection.calls)
    document_update = next(
        call
        for call in pool.connection.calls
        if "UPDATE rag_documents" in call.statement
        and call.parameters.get("document_id") == "doc-stuck"
    )
    assert document_update.parameters["status"] == "ERROR"
    assert (
        "status IN ('PREPROCESSING', 'INGESTING', 'CHUNKING', 'INDEXING')"
        in document_update.statement
    )
    assert pool.connection.commits == 1


async def test_oracle_client_recovers_stale_ingestion_jobs_without_max_attempts_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧 queue table では既定 max_attempts を補って stale job を回復する。"""
    monkeypatch.setattr(FakeOracleCursor, "rowcount", 1, raising=False)
    stale_at = datetime(2026, 1, 2, 1, 0, tzinfo=UTC)
    pool = FakeOraclePool(
        execute_results=[
            [
                {
                    key: value
                    for key, value in _oracle_ingestion_job_row(
                        status="RUNNING",
                        attempt_count=1,
                        started_at=datetime(2026, 1, 2, 0, 0, tzinfo=UTC),
                    ).items()
                    if key != "max_attempts"
                }
            ],
        ],
        missing_ingestion_job_max_attempts=True,
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    recovered = await client.recover_stale_ingestion_jobs(stale_before=stale_at, limit=10)

    assert [job.id for job in recovered] == ["job-1"]
    assert recovered[0].max_attempts == 3
    select_calls = [
        call for call in pool.connection.calls if "FROM rag_ingestion_jobs j" in call.statement
    ]
    assert "j.max_attempts" in select_calls[0].statement
    assert ":default_max_attempts AS max_attempts" in select_calls[1].statement
    assert select_calls[1].parameters["default_max_attempts"] == 3
    update_statements = [call.statement for call in pool.connection.calls]
    assert any("SET status = 'QUEUED'" in statement for statement in update_statements)


async def test_oracle_client_recovery_skips_job_heartbeated_after_select() -> None:
    """SELECT の後に heartbeat が打たれた(0 行更新)job は、文書も戻さず回復の結果に含めない。"""
    stale_at = datetime(2026, 1, 2, 1, 0, tzinfo=UTC)
    pool = FakeOraclePool(
        execute_results=[
            [
                _oracle_ingestion_job_row(
                    status="RUNNING",
                    attempt_count=1,
                    max_attempts=3,
                    started_at=datetime(2026, 1, 2, 0, 0, tzinfo=UTC),
                )
            ],
        ]
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    recovered = await client.recover_stale_ingestion_jobs(
        stale_before=stale_at,
        lease_ttl_seconds=90.0,
        limit=10,
        exclude_lease_owner="host:1:worker",
    )

    assert recovered == []
    select_call = pool.connection.calls[0]
    assert "j.lease_owner <> :exclude_lease_owner" in select_call.statement
    assert select_call.parameters["exclude_lease_owner"] == "host:1:worker"
    assert select_call.parameters["lease_ttl_seconds"] == 90.0
    requeue_call = next(
        call for call in pool.connection.calls if "SET status = 'QUEUED'" in call.statement
    )
    assert "lease_owner <> :exclude_lease_owner" in requeue_call.statement
    assert requeue_call.parameters["exclude_lease_owner"] == "host:1:worker"
    assert not any("UPDATE rag_documents" in call.statement for call in pool.connection.calls)


async def test_oracle_client_claims_ingestion_job_with_lease() -> None:
    """lease_owner を渡した claim は lease を取り、heartbeat_at を DB の時刻にする(#357 / #359)。"""
    started_at = datetime(2026, 1, 2, 0, 2, tzinfo=UTC)
    pool = FakeOraclePool(
        execute_results=[
            [{"document_id": "doc-1"}],
            [{"document_id": "doc-1"}],
            [_oracle_ingestion_job_row()],
        ]
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    claimed = await client.claim_ingestion_job(
        "job-1", started_at=started_at, lease_owner="host:1:worker"
    )

    assert claimed is not None
    update_call = pool.connection.calls[3]
    assert "lease_owner = :lease_owner" in update_call.statement
    assert "heartbeat_at = SYSTIMESTAMP" in update_call.statement
    assert update_call.parameters["lease_owner"] == "host:1:worker"
    assert "heartbeat_at" not in update_call.parameters


async def test_oracle_client_heartbeats_own_running_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """heartbeat は自分の lease の RUNNING job だけを、DB の時刻で更新する(#357 / #359)。"""
    monkeypatch.setattr(FakeOracleCursor, "rowcount", 2, raising=False)
    pool = FakeOraclePool()
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    updated = await client.heartbeat_ingestion_jobs(lease_owner="host:1:worker")

    assert updated == 2
    [call] = pool.connection.calls
    assert "SET heartbeat_at = SYSTIMESTAMP" in call.statement
    assert "WHERE lease_owner = :lease_owner" in call.statement
    assert "AND status = 'RUNNING'" in call.statement
    assert call.parameters == {"lease_owner": "host:1:worker"}
    assert pool.connection.commits == 1


async def test_oracle_client_writes_job_progress_only_for_own_running_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """抽出の段の進捗は自分の lease の RUNNING job だけに書き、None で消す(#1999)。"""
    from app.schemas.document import IngestionJobProgress, IngestionParseProgress

    monkeypatch.setattr(FakeOracleCursor, "rowcount", 1, raising=False)
    pool = FakeOraclePool()
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)
    progress = IngestionJobProgress(
        stage="parse",
        parse=IngestionParseProgress(engine="docling", completed=40, total=82),
        updated_at=datetime(2026, 10, 15, 8, 30, tzinfo=UTC),
    )

    written = await client.update_ingestion_job_progress(
        "job-1", progress, lease_owner="host:1:worker"
    )
    cleared = await client.update_ingestion_job_progress("job-1", None)

    assert written is True
    assert cleared is True
    write_call, clear_call = pool.connection.calls
    assert "SET progress_json = :progress_json" in write_call.statement
    assert "AND status = 'RUNNING'" in write_call.statement
    assert "AND lease_owner = :lease_owner" in write_call.statement
    assert write_call.parameters["lease_owner"] == "host:1:worker"
    saved = json.loads(str(write_call.parameters["progress_json"]))
    assert saved["parse"] == {
        "engine": "docling",
        "unit": "page",
        "completed": 40,
        "failed": 0,
        "total": 82,
        "finished": False,
        # 段ごとの解析の段の状態(#2001)。段に分けない解析は空。
        "segments": [],
    }
    assert "lease_owner" not in clear_call.statement
    assert clear_call.parameters == {"job_id": "job-1", "progress_json": None}


def test_oracle_ingestion_job_row_reads_progress_and_ignores_broken_values() -> None:
    """job の行の進捗(CLOB の JSON)を応答の progress にし、読めない値は None にする(#1999)。"""
    progress = {
        "stage": "vision",
        "parse": {"engine": "docling", "completed": 82, "total": 82, "finished": True},
        "vision": {"succeeded": 3, "total_known": 12, "total_final": False},
        "updated_at": "2026-10-15T08:37:27+00:00",
    }
    job = oracle_module._ingestion_job_from_row(
        {**_oracle_ingestion_job_row(status="RUNNING"), "progress_json": json.dumps(progress)}
    )
    broken = oracle_module._ingestion_job_from_row(
        {**_oracle_ingestion_job_row(status="RUNNING"), "progress_json": '{"stage": "other"}'}
    )
    missing = oracle_module._ingestion_job_from_row(_oracle_ingestion_job_row())

    assert job.progress is not None
    assert job.progress.stage == "vision"
    assert job.progress.vision is not None
    assert job.progress.vision.total_known == 12
    assert broken.progress is None
    assert missing.progress is None


async def test_oracle_client_requeues_own_leased_jobs_without_consuming_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """停止時は自分の lease の RUNNING job を QUEUED に戻し、claim で増やした attempt を戻す。"""
    monkeypatch.setattr(FakeOracleCursor, "rowcount", 1, raising=False)
    recipe_job = {
        **_oracle_ingestion_job_row(
            job_id="job-recipe",
            status="RUNNING",
            attempt_count=1,
            started_at=datetime(2026, 1, 2, 0, 0, tzinfo=UTC),
        ),
        "recipe_id": "recipe-2",
        "recipe_revision": 1,
        "phase": "EXTRACT",
    }
    pool = FakeOraclePool(
        execute_results=[
            [
                _oracle_ingestion_job_row(
                    status="RUNNING",
                    attempt_count=2,
                    started_at=datetime(2026, 1, 2, 0, 0, tzinfo=UTC),
                ),
                recipe_job,
            ]
        ]
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    requeued = await client.requeue_leased_ingestion_jobs(lease_owner="host:1:worker")

    assert [(job.id, job.status, job.attempt_count) for job in requeued] == [
        ("job-1", IngestionJobStatus.QUEUED, 1),
        ("job-recipe", IngestionJobStatus.QUEUED, 0),
    ]
    select_call = pool.connection.calls[0]
    assert "j.status = 'RUNNING'" in select_call.statement
    assert "j.lease_owner = :lease_owner" in select_call.statement
    job_updates = [
        call for call in pool.connection.calls if "UPDATE rag_ingestion_jobs" in call.statement
    ]
    assert len(job_updates) == 2
    for call in job_updates:
        assert "SET status = 'QUEUED'" in call.statement
        assert "WHEN attempt_count > 0 THEN attempt_count - 1" in call.statement
        assert "AND status = 'RUNNING'" in call.statement
        assert "AND lease_owner = :lease_owner" in call.statement
        assert "lease_owner = NULL" in call.statement
        assert call.parameters["lease_owner"] == "host:1:worker"
    # 文書単位の job は文書を、レシピの job はレシピ行だけを工程の前へ戻す(#305)。
    document_update = next(
        call for call in pool.connection.calls if "UPDATE rag_documents" in call.statement
    )
    assert document_update.parameters["document_id"] == "doc-1"
    assert document_update.parameters["status"] == "UPLOADED"
    recipe_update = next(
        call for call in pool.connection.calls if "UPDATE rag_document_recipes r" in call.statement
    )
    assert recipe_update.parameters["recipe_id"] == "recipe-2"
    assert recipe_update.parameters["status"] == "PREPROCESSED"
    assert pool.connection.commits == 1


async def test_oracle_client_requeue_skips_job_finished_after_select() -> None:
    """SELECT の後に完了・取り消し・回復された(0 行更新)job は戻さず、文書にも触れない。"""
    pool = FakeOraclePool(
        execute_results=[
            [
                _oracle_ingestion_job_row(
                    status="RUNNING",
                    attempt_count=1,
                    started_at=datetime(2026, 1, 2, 0, 0, tzinfo=UTC),
                )
            ]
        ]
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    requeued = await client.requeue_leased_ingestion_jobs(lease_owner="host:1:worker")

    assert requeued == []
    assert not any("UPDATE rag_documents" in call.statement for call in pool.connection.calls)


async def test_oracle_client_lists_dispatchable_ingestion_jobs() -> None:
    """dispatch は同じ文書の job が RUNNING の QUEUED job と、自分が実行中の job を除く。"""
    pool = FakeOraclePool(execute_results=[[_oracle_ingestion_job_row(job_id="job-2")]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    jobs = await client.list_dispatchable_ingestion_jobs(
        limit=2, exclude_job_ids=["job-1", "job-3", "job-1"]
    )

    assert [job.id for job in jobs] == ["job-2"]
    [call] = pool.connection.calls
    assert "j.status = 'QUEUED'" in call.statement
    assert "active_job.status = 'RUNNING'" in call.statement
    assert "NOT (j.job_id IN (:exclude_job_id_0, :exclude_job_id_1))" in call.statement
    # 取る順番は文書の取込の最初の段の投入の古い順(続きの段を先に。#2027)。
    assert "SELECT MAX(start_job.queued_at)" in call.statement
    assert "END DESC, j.queued_at ASC, j.job_id ASC FETCH FIRST :limit ROWS ONLY" in call.statement
    assert call.parameters["dispatch_priority_window_seconds"] == 3600
    assert "exclude_phase" not in call.statement
    assert call.parameters["exclude_job_id_0"] == "job-1"
    assert call.parameters["exclude_job_id_1"] == "job-3"
    assert call.parameters["limit"] == 2


async def test_oracle_client_lists_dispatchable_jobs_without_exclusions() -> None:
    """実行中の job が無ければ除外の条件を付けない。"""
    pool = FakeOraclePool(execute_results=[[]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    assert await client.list_dispatchable_ingestion_jobs(limit=1) == []
    assert "exclude_job_id" not in pool.connection.calls[0].statement


async def test_oracle_client_claims_ingestion_job_with_row_lock() -> None:
    """取込 job 実行前に QUEUED 行を row lock 付きで claim する。"""
    started_at = datetime(2026, 1, 2, 0, 2, tzinfo=UTC)
    pool = FakeOraclePool(
        execute_results=[
            [{"document_id": "doc-1"}],
            [{"document_id": "doc-1"}],
            [_oracle_ingestion_job_row()],
        ]
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    claimed = await client.claim_ingestion_job("job-1", started_at=started_at)

    assert claimed is not None
    assert claimed.status == IngestionJobStatus.RUNNING
    assert claimed.attempt_count == 1
    assert claimed.started_at == started_at
    parent_lock_call = pool.connection.calls[1]
    select_call = pool.connection.calls[2]
    update_call = pool.connection.calls[3]
    assert "FROM rag_documents" in parent_lock_call.statement
    assert "FOR UPDATE" in parent_lock_call.statement
    assert "FOR UPDATE SKIP LOCKED" in select_call.statement
    assert "j.status = 'QUEUED'" in select_call.statement
    assert "SET status = 'RUNNING'" in update_call.statement
    assert update_call.parameters["attempt_count"] == 1
    assert update_call.parameters["started_at"] == started_at
    # lease を持たない実行は heartbeat を NULL にし、従来の経過時間で stale を判定する(#357)。
    assert update_call.parameters["lease_owner"] is None
    assert "heartbeat_at = NULL" in update_call.statement


async def test_oracle_client_replaces_and_lists_ingestion_segments() -> None:
    """segment checkpoint は document scope で置換・取得できる。"""
    pool = FakeOraclePool(
        execute_results=[
            [_oracle_document_row()],
            [_oracle_ingestion_segment_row()],
        ]
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)
    segment = IngestionSegment(
        segment_id="doc-1:p1-3",
        document_id="doc-1",
        status="QUEUED",
        parser_backend="enterprise_ai",
        parser_profile="enterprise_ai_pdf_layout",
        page_start=1,
        page_end=3,
    )

    replaced = await client.replace_ingestion_segments("doc-1", [segment])
    listed = await client.list_ingestion_segments("doc-1")

    assert replaced == [segment]
    assert listed[0].segment_id == "doc-1:p1-3"
    assert listed[0].page_start == 1
    statements = [call.statement for call in pool.connection.calls]
    assert any("DELETE FROM rag_ingestion_segments" in statement for statement in statements)
    assert pool.connection.many_calls
    assert "INSERT INTO rag_ingestion_segments" in pool.connection.many_calls[0].statement
    assert pool.connection.many_calls[0].rows[0]["segment_id"] == "doc-1:p1-3"
    list_call = pool.connection.calls[-1]
    assert "FROM rag_ingestion_segments s" in list_call.statement
    assert "JOIN rag_documents d" in list_call.statement
    assert list_call.parameters["document_id"] == "doc-1"


async def test_oracle_client_updates_ingestion_segment_status() -> None:
    """segment checkpoint 更新は document access predicate 付きで行う。"""
    pool = FakeOraclePool(execute_results=[[_oracle_ingestion_segment_row(status="RUNNING")]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    updated = await client.update_ingestion_segment(
        "doc-1:p1-3",
        status="RUNNING",
        attempt_count=2,
        artifact_path="oci://namespace/bucket/artifacts/extractions/doc-1/trace.json",
    )

    assert updated is not None
    assert updated.status == "RUNNING"
    update_call = pool.connection.calls[0]
    assert "UPDATE rag_ingestion_segments" in update_call.statement
    assert "updated_at = SYSTIMESTAMP" in update_call.statement
    assert update_call.parameters["segment_id"] == "doc-1:p1-3"
    assert update_call.parameters["status"] == "RUNNING"
    assert update_call.parameters["attempt_count"] == 2


async def test_oracle_client_persists_knowledge_bases_through_pool() -> None:
    """knowledge base persistence は Oracle pool 経由で実行される。"""
    pool = FakeOraclePool()
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    detail = await client.create_knowledge_base(
        name="社内規程",
        description="規程文書",
        default_search_mode=SearchMode.HYBRID,
        retrieval_config={"top_k": 20},
    )

    assert detail.name == "社内規程"
    assert detail.status == KnowledgeBaseStatus.ACTIVE
    assert detail.retrieval_config == {"top_k": 20}
    assert pool.connection.commits == 1
    # 作成前に tenant 内の同名 KB を確かめてから INSERT する（#282）。
    assert "LOWER(name) = :knowledge_base_name" in pool.connection.calls[0].statement
    call = pool.connection.calls[-1]
    assert "INSERT INTO rag_knowledge_bases" in call.statement
    assert call.parameters["name"] == "社内規程"
    assert call.parameters["default_search_mode"] == "hybrid"


async def test_oracle_client_lists_knowledge_bases_with_counts() -> None:
    """knowledge base 一覧は集計列を含む SQL で取得する。"""
    pool = FakeOraclePool(execute_results=[[_oracle_knowledge_base_row(document_count=3)]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    items = await client.list_knowledge_bases(status=KnowledgeBaseStatus.ACTIVE, query="規程")

    assert len(items) == 1
    assert items[0].id == "kb-1"
    assert items[0].document_count == 3
    call = pool.connection.calls[0]
    assert "FROM rag_knowledge_bases kb" in call.statement
    assert "LEFT JOIN rag_document_knowledge_bases dkb" in call.statement
    assert "CASE WHEN UPPER(kb.name) = 'DEFAULT' THEN 0 ELSE 1 END" in call.statement
    order = call.statement.split("ORDER BY", 1)[1]
    assert "kb.updated_at DESC" not in order
    assert "REGEXP_SUBSTR(TRANSLATE(kb.name" in order
    assert "kb.knowledge_base_id ASC" in order
    assert "kb.status = :knowledge_base_status" in call.statement
    assert call.parameters["knowledge_base_status"] == "ACTIVE"
    assert call.parameters["knowledge_base_query"] == "%規程%"


async def test_oracle_client_updates_knowledge_base_metadata() -> None:
    """knowledge base 更新は既存行を確認してから UPDATE する。"""
    pool = FakeOraclePool(execute_results=[[_oracle_knowledge_base_row()]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    updated = await client.update_knowledge_base(
        "kb-1",
        name="更新済み",
        description=None,
        update_fields={"name", "description"},
    )

    assert updated.name == "更新済み"
    assert updated.description is None
    statements = [call.statement for call in pool.connection.calls]
    assert any(
        "SELECT" in statement and "rag_knowledge_bases" in statement for statement in statements
    )
    assert any("UPDATE rag_knowledge_bases" in statement for statement in statements)


async def test_oracle_client_protects_default_knowledge_base() -> None:
    """DEFAULT は改名もアーカイブもできない。"""
    update_pool = FakeOraclePool(execute_results=[[_oracle_knowledge_base_row(name="DEFAULT")]])
    update_client = OracleClient(
        settings=_llm_settings(), pool=update_pool, db_call_runner=_run_inline
    )

    with pytest.raises(ValueError, match="名前は変更できません"):
        await update_client.update_knowledge_base(
            "kb-1",
            name="別名",
            update_fields={"name"},
        )

    archive_pool = FakeOraclePool(execute_results=[[_oracle_knowledge_base_row(name="DEFAULT")]])
    archive_client = OracleClient(
        settings=_llm_settings(), pool=archive_pool, db_call_runner=_run_inline
    )

    with pytest.raises(ValueError, match="アーカイブできません"):
        await archive_client.archive_knowledge_base("kb-1")


async def test_oracle_client_assigns_documents_to_knowledge_base() -> None:
    """membership 追加は active knowledge base と document を確認して MERGE する。"""
    pool = FakeOraclePool(
        execute_results=[
            [_oracle_knowledge_base_row()],
            [_oracle_document_row()],
        ]
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    detail = await client.assign_documents_to_knowledge_base("kb-1", ["doc-1"])

    assert detail.id == "kb-1"
    assert pool.connection.many_calls
    many_call = pool.connection.many_calls[0]
    assert "MERGE INTO rag_document_knowledge_bases" in many_call.statement
    assert many_call.rows[0]["knowledge_base_id"] == "kb-1"
    assert many_call.rows[0]["document_id"] == "doc-1"


async def test_oracle_client_rejects_assignment_to_archived_knowledge_base() -> None:
    """アーカイブ済み knowledge base へ文書は追加できない。"""
    pool = FakeOraclePool(
        execute_results=[
            [_oracle_knowledge_base_row(status="ARCHIVED")],
        ]
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    with pytest.raises(ValueError, match="アーカイブ済み"):
        await client.assign_documents_to_knowledge_base("kb-1", ["doc-1"])

    assert not pool.connection.many_calls


async def test_oci_vector_search_uses_ai_vector_search_sql() -> None:
    """OCI mode の vector search は Oracle AI Vector Search に bind 付きで問い合わせる。"""
    pool = FakeOraclePool(
        execute_results=[
            [
                {
                    "document_id": "doc-1",
                    "chunk_id": "doc-1:0",
                    "chunk_text": "社内規程 クラウド利用料",
                    "metadata_json": '{"chunk_index":0}',
                    "chunk_set_id": "set-a",
                    "recipe_id": "recipe-a",
                    "recipe_slot_no": 1,
                    "file_name": "policy.txt",
                    "category_name": "社内規程",
                    "score": 0.91,
                }
            ]
        ]
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    hits = await client.vector_search(
        [1.0, 0.0, 0.0],
        top_k=3,
        filters={"document_id": "doc-1"},
    )

    assert len(hits) == 1
    assert hits[0].metadata["document_id"] == "doc-1"
    assert hits[0].metadata["chunk_id"] == "doc-1:0"
    assert hits[0].metadata["chunk_set_id"] == "set-a"
    assert hits[0].metadata["recipe_id"] == "recipe-a"
    assert hits[0].metadata["retrieval_mode"] == "vector"
    assert hits[0].metadata["vector_rank"] == 1
    assert hits[0].metadata["vector_score"] == 0.91
    call = pool.connection.calls[0]
    assert "VECTOR_DISTANCE" in call.statement
    assert "d.status = 'INDEXED'" in call.statement
    assert "FETCH APPROX FIRST 3 ROWS ONLY WITH TARGET ACCURACY 90" in call.statement
    assert call.parameters["embedding"] == array("f", [1.0, 0.0, 0.0])
    assert "top_k" not in call.parameters
    assert call.parameters["filter_document_id"] == "doc-1"


async def test_oci_vector_search_applies_chunk_metadata_filters() -> None:
    """OCI retrieval SQL は chunk metadata filter も bind 付きで適用する。"""
    pool = FakeOraclePool(
        execute_results=[
            [
                {
                    "document_id": "doc-1",
                    "chunk_id": "doc-1:2",
                    "chunk_text": "料金表 クラウド利用料",
                    "metadata_json": (
                        '{"chunk_index":2,"content_kind":"table",'
                        '"section_title":"料金表","section_path":"経費申請 > 料金表"}'
                    ),
                    "file_name": "policy.txt",
                    "category_name": "社内規程",
                    "score": 0.93,
                }
            ]
        ]
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    hits = await client.vector_search(
        [1.0, 0.0, 0.0],
        top_k=3,
        filters={
            "content_kind": "TABLE",
            "section_title": "料金",
            "section_path": "経費申請",
        },
    )

    assert len(hits) == 1
    assert hits[0].metadata["content_kind"] == "table"
    call = pool.connection.calls[0]
    assert "JSON_VALUE(c.metadata_json, '$.content_kind')" in call.statement
    assert "JSON_VALUE(c.metadata_json, '$.section_title')" in call.statement
    assert "JSON_VALUE(c.metadata_json, '$.section_path')" in call.statement
    assert call.parameters["filter_content_kind"] == "table"
    assert call.parameters["filter_section_title"] == "%料金%"
    assert call.parameters["filter_section_path"] == "%経費申請%"


async def test_oracle_knowledge_base_subgraph_requires_current_kb_membership() -> None:
    """KB のグラフ表示も、KB から外した文書の entity / relationship を出さない(#274)。"""
    pool = FakeOraclePool(
        execute_results=[
            [{"entity_id": "ent-1", "canonical_name": "承認条件", "confidence": 0.9}],
            [],
        ]
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    nodes, edges = await client.fetch_knowledge_base_subgraph("kb-1", limit=10)

    assert [node["id"] for node in nodes] == ["ent-1"]
    assert edges == []
    node_sql, edge_sql = (" ".join(call.statement.split()) for call in pool.connection.calls[:2])
    assert "{membership_sql}" not in node_sql
    assert "member_dkb.knowledge_base_id = e.knowledge_base_id" in node_sql
    assert "JSON_EXISTS(e.source_document_ids," in node_sql
    assert "member_dkb.knowledge_base_id = r.knowledge_base_id" in edge_sql
    assert "JSON_EXISTS(r.source_document_ids," in edge_sql


async def test_oracle_knowledge_base_subgraph_requires_active_chunk_sets() -> None:
    """成功前の処理結果を node / edge のどちらにも表示しない。"""
    pool = FakeOraclePool(
        execute_results=[[{"entity_id": "entity-active", "canonical_name": "section"}], []]
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    await client.fetch_knowledge_base_subgraph("kb-active", limit=10)

    for call, alias in zip(pool.connection.calls, ("e", "r"), strict=True):
        sql = " ".join(call.statement.split())
        assert f"{alias}.chunk_set_id IS NULL OR EXISTS" in sql
        assert f"graph_cs.chunk_set_id = {alias}.chunk_set_id" in sql
        assert "graph_cs.is_active = 1" in sql


async def test_upsert_extraction_artifact_preserves_existing_payload_when_omitted() -> None:
    """後段 status 更新では大きい抽出 JSON を再 bind せず、既存 payload を保持する。"""
    pool = FakeOraclePool()
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    await client.upsert_document_extraction_artifact(
        document_id="doc-1",
        extraction_recipe_id="ex-1",
        source_sha256="a" * 64,
        recipe_subset={"rag_parser_adapter_backend": "unstructured"},
        extraction=None,
        status="materialized",
    )

    call = pool.connection.calls[0]
    matched_update = call.statement.split("WHEN MATCHED THEN UPDATE SET", 1)[1].split(
        "WHEN NOT MATCHED",
        1,
    )[0]
    assert "COALESCE(:extraction_json, t.extraction_json)" not in call.statement
    assert "t.extraction_json" not in matched_update
    assert call.parameters["extraction_json"] is None
    assert json.loads(cast(str, call.parameters["recipe_subset"])) == {
        "rag_parser_adapter_backend": "unstructured"
    }
    assert (
        call.input_sizes["extraction_json"]
        == oracle_module._json_input_sizes("extraction_json")["extraction_json"]
    )


async def test_upsert_chunk_set_binds_recipe_subset_as_json() -> None:
    """recipe は JSON の文字列を CLOB で渡し、COALESCE は JSON(:x) で列と型をそろえる(#1363)。"""
    pool = FakeOraclePool()
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    await client.upsert_chunk_set(
        chunk_set_id="cs-1",
        document_id="doc-1",
        recipe_subset={"chunking_strategy": "structure_aware"},
    )

    call = pool.connection.calls[0]
    assert json.loads(cast(str, call.parameters["recipe_subset"])) == {
        "chunking_strategy": "structure_aware"
    }
    assert "COALESCE(JSON(:recipe_subset), t.recipe_subset)" in call.statement
    assert (
        call.input_sizes["recipe_subset"]
        == oracle_module._json_input_sizes("recipe_subset")["recipe_subset"]
    )


async def test_upsert_extraction_artifact_updates_payload_when_provided() -> None:
    """抽出 payload を渡す経路では JSON 列を明示更新する。"""
    pool = FakeOraclePool()
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    await client.upsert_document_extraction_artifact(
        document_id="doc-1",
        extraction_recipe_id="ex-1",
        source_sha256="a" * 64,
        extraction={"elements": [{"text": "本文"}]},
        status="materialized",
    )

    call = pool.connection.calls[0]
    matched_update = call.statement.split("WHEN MATCHED THEN UPDATE SET", 1)[1].split(
        "WHEN NOT MATCHED",
        1,
    )[0]
    assert "t.extraction_json = :extraction_json" in matched_update
    assert json.loads(cast(str, call.parameters["extraction_json"])) == {
        "elements": [{"text": "本文"}]
    }
    assert (
        call.input_sizes["extraction_json"]
        == oracle_module._json_input_sizes("extraction_json")["extraction_json"]
    )


async def test_upsert_extraction_artifact_binds_large_payload_as_json() -> None:
    """大きい抽出 payload も VARCHAR2 ではなく CLOB の bind で渡す(OSON は使わない。#1363)。"""
    pool = FakeOraclePool()
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)
    large_text = "x" * 40000

    await client.upsert_document_extraction_artifact(
        document_id="doc-1",
        extraction_recipe_id="ex-1",
        source_sha256="a" * 64,
        extraction={"elements": [{"text": large_text}]},
        status="materialized",
    )

    call = pool.connection.calls[0]
    assert len(cast(str, call.parameters["extraction_json"])) > 32767
    assert json.loads(cast(str, call.parameters["extraction_json"])) == {
        "elements": [{"text": large_text}]
    }
    assert (
        call.input_sizes["extraction_json"]
        == oracle_module._json_input_sizes("extraction_json")["extraction_json"]
    )


def test_json_bind_passes_json_text_with_clob_bind_not_oson() -> None:
    """JSON 列へは JSON の文字列を CLOB で渡し、OSON(DB_TYPE_JSON)を使わない(#1363)。

    thin の OSON の符号化は数百 KB を超える値で極端に遅く、GIL を持ったまま backend 全体を止める。
    """
    oracledb = pytest.importorskip("oracledb")

    assert oracle_module._json_bind(None) is None
    bound = oracle_module._json_bind(
        {"score": Decimal("0.5"), "count": Decimal("3"), "at": datetime(2026, 10, 9, tzinfo=UTC)}
    )
    assert isinstance(bound, str)
    assert json.loads(bound) == {"score": 0.5, "count": 3, "at": "2026-10-09T00:00:00+00:00"}
    assert oracle_module._json_bind({"text": "本文"}) == '{"text":"本文"}'
    sizes = oracle_module._json_input_sizes("a", "b")
    assert sizes == {"a": oracledb.DB_TYPE_CLOB, "b": oracledb.DB_TYPE_CLOB}
    assert oracledb.DB_TYPE_JSON not in sizes.values()


async def test_save_answer_record_binds_large_citations_as_clob_json_text() -> None:
    """1MB 程度の citations も JSON の文字列を CLOB で bind して保存する(#1363)。"""
    pool = FakeOraclePool(execute_results=[[]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)
    citations = [
        {"chunk_id": f"c-{index}", "text": "受注管理規程の本文" * 100, "score": 0.5}
        for index in range(400)
    ]

    await client.save_answer_record(
        {
            "trace_id": "trace-large",
            "surface": "search",
            "answer_engine": "grounded",
            "question": "質問",
            "answer": "回答",
            "citations": citations,
            "diagnostics": {"elapsed_ms": 10},
        }
    )

    (call,) = pool.connection.calls
    citations_text = call.parameters["citations_json"]
    assert isinstance(citations_text, str)
    assert len(citations_text.encode()) > 1_000_000
    assert json.loads(citations_text) == citations
    assert json.loads(cast(str, call.parameters["diagnostics_json"])) == {"elapsed_ms": 10}
    assert call.parameters["evaluation_input_json"] is None
    assert call.input_sizes == oracle_module._json_input_sizes(
        "citations_json", "diagnostics_json", "evaluation_input_json"
    )


async def test_update_recipe_status_coalesces_preprocess_artifact_as_json() -> None:
    """CLOB の bind は JSON 型の列と COALESCE できないため、JSON(:x) で型をそろえる(#1363)。"""
    pool = FakeOraclePool(execute_results=[])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)
    artifact = DocumentPreprocessArtifact(
        derivation_id="der-1",
        profile="passthrough",
        file_name="policy.pdf",
        object_storage_path="local://prepared/policy.pdf",
        content_type="application/pdf",
    )

    await client.update_document_recipe_status(
        recipe_id="recipe-1",
        status=FileStatus.PREPROCESSED,
        preprocess_artifact=artifact,
    )

    call = pool.connection.calls[0]
    assert "COALESCE(JSON(:preprocess_artifact), preprocess_artifact)" in call.statement
    assert json.loads(cast(str, call.parameters["preprocess_artifact"])) == artifact.model_dump(
        mode="json"
    )
    assert call.input_sizes == oracle_module._json_input_sizes("preprocess_artifact")


async def test_oracle_replace_document_graph_index_replaces_document_scope() -> None:
    """GraphRAG-lite index 保存は対象 document の旧 KG rows を置換する。"""
    graph_index = GraphIndex(
        entities=[
            GraphEntity(
                entity_id="ent-doc",
                knowledge_base_id="kb-1",
                canonical_name="文書全体: 社内規程",
                entity_type="document",
                description="社内規程全体",
                confidence=0.95,
                source_document_ids=["doc-1"],
            ),
            GraphEntity(
                entity_id="ent-section",
                knowledge_base_id="kb-1",
                canonical_name="承認条件",
                entity_type="section",
                description="承認条件の章節",
                confidence=0.9,
                source_document_ids=["doc-1"],
            ),
        ],
        relationships=[
            GraphRelationship(
                relationship_id="rel-doc-section",
                knowledge_base_id="kb-1",
                source_entity_id="ent-doc",
                target_entity_id="ent-section",
                relationship_type="contains",
                description="文書全体は承認条件を含みます。",
                confidence=1.0,
                source_document_ids=["doc-1"],
            )
        ],
        entity_chunk_links=[
            GraphEntityChunkLink(
                entity_id="ent-section",
                chunk_id="doc-1:0",
                document_id="doc-1",
                relevance_score=0.8,
            )
        ],
    )
    pool = FakeOraclePool(
        execute_results=[
            [_oracle_document_row()],
            [{"entity_id": "ent-doc"}, {"entity_id": "ent-old"}, {"entity_id": "ent-stale"}],
        ]
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    await client.replace_document_graph_index("doc-1", graph_index)

    assert pool.connection.commits == 1
    statements = [call.statement for call in pool.connection.calls]
    assert any("DELETE FROM rag_graph_relationships" in statement for statement in statements)
    assert any(
        "FROM rag_graph_entities" in statement and "source_document_ids" in statement
        for statement in statements
    )
    assert any("source_entity_id IN" in statement for statement in statements)
    assert any("target_entity_id IN" in statement for statement in statements)
    assert any("DELETE FROM rag_graph_entity_chunks" in statement for statement in statements)
    assert any("DELETE FROM rag_graph_entities" in statement for statement in statements)
    # claims / community summary は #621 で廃止し、表は migration 007 で消した。
    assert not any("rag_graph_claims" in statement for statement in statements)
    assert not any("FROM user_tables" in statement for statement in statements)
    assert not any("rag_graph_community_summaries" in statement for statement in statements)
    many_statements = [call.statement for call in pool.connection.many_calls]
    assert any("INSERT INTO rag_graph_entities" in statement for statement in many_statements)
    assert any("INSERT INTO rag_graph_relationships" in statement for statement in many_statements)
    assert not any("rag_graph_claims" in statement for statement in many_statements)
    assert not any("rag_graph_community_summaries" in statement for statement in many_statements)
    assert any("INSERT INTO rag_graph_entity_chunks" in statement for statement in many_statements)
    entity_insert = next(
        call
        for call in pool.connection.many_calls
        if "INSERT INTO rag_graph_entities" in call.statement
    )
    assert json.loads(str(entity_insert.rows[0]["source_document_ids"])) == ["doc-1"]


async def test_oracle_save_evaluation_artifact_redacts_query_text() -> None:
    """評価 artifact は query 原文ではなく summary/hash だけを保存する。"""
    pool = FakeOraclePool()
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    evaluation_run_id = await client.save_evaluation_artifact(
        {
            "evaluation_run_id": "eval-1",
            "knowledge_base_ids": ["kb-1"],
            "request_summary": {
                "kind": "run",
                "cases": [{"id": "case-1", "query_hash": "a" * 64, "query_chars": 12}],
            },
            "result_summary": {"case_count": 1, "passed": True},
            "passed": True,
        }
    )

    assert evaluation_run_id == "eval-1"
    assert pool.connection.commits == 1
    call = pool.connection.calls[0]
    assert "INSERT INTO rag_evaluation_runs" in call.statement
    assert call.parameters["evaluation_run_id"] == "eval-1"
    assert call.parameters["passed"] == 1
    assert len(str(call.parameters["result_sha256"])) == 64
    assert "query_text" not in str(call.parameters)


async def test_oci_save_index_replaces_existing_chunks_and_binds_vectors() -> None:
    """OCI mode の索引保存は既存 chunk を消し、metadata・検索用テキスト・VECTOR を挿入する。"""
    pool = FakeOraclePool(execute_results=[[_oracle_document_row()]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    saved = await client.save_index(
        "doc-1",
        StructuredExtraction(raw_text="社内規程", confidence=0.9),
        [
            Chunk(
                index=0,
                text="社内規程",
                start_offset=0,
                end_offset=3,
                metadata={
                    "section_path": "経費申請 > 承認",
                    "content_kind": "text",
                    "context_header": "社内規程.pdf > 経費申請 > 承認",
                },
            )
        ],
        [[1.0, 0.0, 0.0]],
    )

    assert saved[0].chunk_id == "doc-1:0"
    assert saved[0].metadata["document_id"] == "doc-1"
    assert saved[0].metadata["chunk_id"] == "doc-1:0"
    assert saved[0].metadata["chunk_index"] == 0
    assert saved[0].metadata["section_path"] == "経費申請 > 承認"
    assert pool.connection.commits == 1
    statements = [call.statement for call in pool.connection.calls]
    assert any("DELETE FROM rag_chunks" in statement for statement in statements)
    assert pool.connection.many_calls
    inserted = pool.connection.many_calls[0].rows[0]
    assert inserted["chunk_id"] == "doc-1:0"
    assert inserted["chunk_text"] == "社内規程"
    assert inserted["search_text"] == "社内規程.pdf > 経費申請 > 承認\n社内規程"
    assert inserted["embedding"] == array("f", [1.0, 0.0, 0.0])
    metadata = json.loads(str(inserted["metadata_json"]))
    assert metadata["document_id"] == "doc-1"
    assert metadata["chunk_id"] == "doc-1:0"
    assert metadata["chunk_index"] == 0
    assert metadata["section_path"] == "経費申請 > 承認"
    assert metadata["content_kind"] == "text"


async def test_oci_save_index_persists_extraction_and_chunks_atomically() -> None:
    """index 保存は extraction と chunk/vector を 1 transaction で置換する。"""
    pool = FakeOraclePool(execute_results=[[_oracle_document_row()]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    saved = await client.save_index(
        "doc-1",
        StructuredExtraction(raw_text="新しい抽出本文", confidence=0.98),
        [
            Chunk(
                index=0,
                text="新しい抽出本文",
                start_offset=0,
                end_offset=7,
                metadata={"section_path": "本文", "content_kind": "text"},
            )
        ],
        [[0.1, 0.2, 0.3]],
    )

    assert saved[0].chunk_id == "doc-1:0"
    assert saved[0].metadata["content_kind"] == "text"
    assert pool.connection.commits == 1
    statements = [call.statement for call in pool.connection.calls]
    update_index = next(
        index for index, statement in enumerate(statements) if "UPDATE rag_documents" in statement
    )
    delete_index = next(
        index for index, statement in enumerate(statements) if "DELETE FROM rag_chunks" in statement
    )
    assert update_index < delete_index
    update_call = pool.connection.calls[update_index]
    extraction_payload = json.loads(str(update_call.parameters["extraction"]))
    assert extraction_payload["raw_text"] == "新しい抽出本文"
    inserted = pool.connection.many_calls[0].rows[0]
    assert inserted["chunk_id"] == "doc-1:0"
    assert inserted["embedding"] == array("f", [0.1, 0.2, 0.3])


async def test_oci_save_index_stores_first_page_context_once_per_chunk_set() -> None:
    """1 ページ目の本文は chunk の行に入れず、chunk set に 1 つだけ保存する(#557)。

    chunk set の行は chunk の保存の後に upsert_chunk_set が作るため、無ければ MERGE で作る。
    chunk set の無い保存(未タグ)では書かない。
    """
    from app.rag.chunking_small_to_big import FIRST_PAGE_CONTEXT_KEY

    first_page = {
        "page": 1,
        "status": "available",
        "text": "受注管理規程 第3版",
        "engine": "docling",
        "record_ids": ["docling-p1-1"],
        "truncated": False,
    }
    chunks = [
        Chunk(
            index=index,
            text=f"本文{index}",
            start_offset=0,
            end_offset=3,
            metadata={
                "engine_search_text": f"Child text: 本文{index}",
                **(
                    {FIRST_PAGE_CONTEXT_KEY: json.dumps(first_page, ensure_ascii=False)}
                    if index == 0
                    else {}
                ),
            },
        )
        for index in range(2)
    ]

    for chunk_set_id in ("cs-1", None):
        pool = FakeOraclePool(execute_results=[[_oracle_document_row()]])
        client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

        await client.save_index(
            "doc-1",
            StructuredExtraction(raw_text="本文", confidence=0.9),
            chunks,
            [[1.0, 0.0, 0.0]] * 2,
            chunk_set_id=chunk_set_id,
        )

        rows = pool.connection.many_calls[0].rows
        assert all(FIRST_PAGE_CONTEXT_KEY not in str(row["metadata_json"]) for row in rows)
        assert all("受注管理規程" not in str(row["search_text"]) for row in rows)
        merges = [
            call for call in pool.connection.calls if "MERGE INTO rag_chunk_sets" in call.statement
        ]
        if chunk_set_id is None:
            assert merges == []
            continue
        assert len(merges) == 1
        assert merges[0].parameters["chunk_set_id"] == "cs-1"
        assert merges[0].parameters["document_id"] == "doc-1"
        assert json.loads(cast(str, merges[0].parameters["first_page_context"])) == first_page
        assert "first_page_context" in merges[0].input_sizes
        assert pool.connection.commits == 1


async def test_oci_list_document_chunks_accepts_json_element_ids_and_row_group_metadata() -> None:
    """chunk view は JSON array element_ids と table row-group metadata を保持する。"""
    pool = FakeOraclePool(
        execute_results=[
            [
                {
                    "document_id": "doc-1",
                    "chunk_id": "doc-1:7",
                    "chunk_index": 7,
                    "chunk_text": "|項目|金額|\n|---|---|\n|交通費|1000円|",
                    "metadata_json": json.dumps(
                        {
                            "content_kind": "table",
                            "element_ids": ["tbl-main", "row-group-1"],
                            "page_start": 2,
                            "page_end": 2,
                            "bbox": [10, 20, 80, 40],
                            "chunk_group_id": "grp-table",
                            "source_parser": "local_office_structure",
                            "bbox_coordinate_mode": "xywh",
                            "bbox_unit": "percent",
                            "dependency_edges": [
                                {"parent_id": "tbl-main", "child_id": "row-group-1"}
                            ],
                            "table_data_row_start": 1,
                            "table_data_row_end": 12,
                            "table_header_repeated": True,
                        }
                    ),
                }
            ]
        ]
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    chunks = await client.list_document_chunks("doc-1")

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.chunk_id == "doc-1:7"
    assert chunk.element_ids == ["tbl-main", "row-group-1"]
    assert chunk.bbox == [10.0, 20.0, 80.0, 40.0]
    assert chunk.content_kind == "table"
    assert chunk.chunk_group_id == "grp-table"
    assert chunk.metadata["element_ids"] == ["tbl-main", "row-group-1"]
    assert chunk.metadata["bbox"] == [10, 20, 80, 40]
    assert chunk.metadata["dependency_edges"] == [
        {"parent_id": "tbl-main", "child_id": "row-group-1"}
    ]
    assert chunk.metadata["bbox_coordinate_mode"] == "xywh"
    assert chunk.metadata["bbox_unit"] == "percent"
    assert chunk.metadata["table_data_row_start"] == 1
    assert chunk.metadata["table_data_row_end"] == 12
    assert chunk.metadata["table_header_repeated"] is True


@pytest.mark.parametrize("strategy", ["small_to_big", "fixed_length"])
async def test_oci_context_group_siblings_uses_chunk_group_sql(strategy: str) -> None:
    """OCI mode の同一 group context は、起点をまとめた 1 回の SQL で chunk_group_id から取得する。

    起点ごとの条件(文書・chunk_set・group・自分を除く)・並び・件数は起点の表(``a``)の値で
    1 件ずつ読むときと同じにする(#1490)。行は sibling の chunk ごと(#1498)。
    """
    pool = FakeOraclePool(
        execute_results=[
            [
                {
                    "anchor_positions": "0:1",
                    "document_id": "doc-1",
                    "chunk_id": "doc-1:1",
                    "chunk_text": "表ヘッダー: 項目 / 条件。",
                    "metadata_json": json.dumps({"chunk_group_id": "grp-table"}),
                    "chunk_index": 1,
                    "file_name": "policy.txt",
                    "category_name": "社内規程",
                    "score": 0,
                },
                {
                    "anchor_positions": "0:2",
                    "document_id": "doc-1",
                    "chunk_id": "doc-1:3",
                    "chunk_text": "表注記: 証憑添付が必要。",
                    "metadata_json": json.dumps({"chunk_group_id": "grp-table"}),
                    "chunk_index": 3,
                    "file_name": "policy.txt",
                    "category_name": "社内規程",
                    "score": 0,
                },
            ]
        ]
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)
    anchor = RetrievedChunk(
        document_id="doc-1",
        chunk_id="doc-1:2",
        text="表行: 承認条件 / 120000 円以上。",
        score=0.91,
        file_name="policy.txt",
        metadata={
            "chunk_index": 2,
            "chunk_group_id": "grp-table",
            "chunk_set_id": "set-a",
            "recipe_id": "recipe-a",
            "chunk_strategy": strategy,
        },
    )

    siblings = await client.context_group_siblings(
        [anchor], max_chunks_per_group=2, neighbor_child_count=3
    )

    assert [chunk.chunk_id for chunk in siblings] == ["doc-1:1", "doc-1:3"]
    assert [chunk.metadata["chunk_index"] for chunk in siblings] == [1, 3]
    assert [chunk.metadata["context_group_distance"] for chunk in siblings] == [-1, 1]
    assert all(chunk.score == 0.91 for chunk in siblings)
    assert all(
        chunk.metadata["context_group_expanded"] is (strategy != "small_to_big")
        for chunk in siblings
    )
    assert all(
        chunk.metadata["context_group_purpose"]
        == ("parent_source_mapping" if strategy == "small_to_big" else "neighbor_context")
        for chunk in siblings
    )
    assert len(pool.connection.calls) == 1
    call = pool.connection.calls[0]
    # chunk group の id は起点の文書・chunk_set の chunk ごとに 1 回だけ読み(結合の外の view)、
    # 起点とはその値で結合する(起点 × chunk の組ごとに CLOB の JSON を解かない。#3461)。
    assert "SELECT /*+ NO_MERGE */" in call.statement
    assert "JSON_VALUE(g.metadata_json, '$.chunk_group_id') AS chunk_group_id" in call.statement
    assert "grp.chunk_group_id = a.chunk_group_id" in call.statement
    assert "JSON_VALUE(c.metadata_json" not in call.statement
    assert "c.chunk_id <> a.anchor_chunk_id" in call.statement
    assert "PARTITION BY a.anchor_ord" in call.statement
    assert "ABS(c.chunk_index - a.anchor_index) ASC, c.chunk_index ASC, c.chunk_id ASC" in (
        call.statement
    )
    assert "context_rank <= context_limit" in call.statement
    assert "THEN :neighbor_child_count" in call.statement
    # 行は sibling の chunk ごとに 1 行(本文は 1 回だけ読む。#1498)。
    assert "GROUP BY chunk_id" in call.statement
    assert "WITHIN GROUP (ORDER BY anchor_ord, context_rank) AS anchor_positions" in (
        call.statement
    )
    assert "d.status = 'INDEXED'" in call.statement
    assert "d.document_id = a.document_id" in call.statement
    assert "c.chunk_set_id = a.chunk_set_id" in call.statement
    assert ":filter_document_id" not in call.statement
    assert ":filter_chunk_set_id" not in call.statement
    assert call.parameters["ca_ord_0"] == 0
    assert call.parameters["ca_doc_0"] == "doc-1"
    assert call.parameters["ca_set_0"] == "set-a"
    assert call.parameters["ca_group_0"] == ("grp-table" if strategy == "small_to_big" else "")
    assert call.parameters["ca_index_0"] == 2
    assert call.parameters["ca_chunk_0"] == "doc-1:2"
    assert call.parameters["max_chunks_per_group"] == 2
    assert call.parameters["neighbor_child_count"] == 3
    # 足りない起点は空(どの chunk にも一致しない)で埋め、SQL の文を同じにする。
    assert call.parameters["ca_doc_1"] is None
    assert call.parameters["ca_ord_49"] is None


async def test_oci_update_error_status_preserves_chunks_and_extraction() -> None:
    """ERROR への状態遷移では段階レビュー用の chunk と抽出 JSON を保持する。"""
    extraction = {"document_type": "text", "raw_text": "抽出済み"}
    errored = _oracle_document_row(status="ERROR", extraction=extraction)
    pool = FakeOraclePool(execute_results=[[_oracle_document_row()], [errored]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    detail = await client.update_document_status(
        "doc-1",
        FileStatus.ERROR,
        "再分析に失敗しました。",
    )

    assert detail.status == FileStatus.ERROR
    assert detail.extraction == extraction
    statements = [call.statement for call in pool.connection.calls]
    assert not any("DELETE FROM rag_chunks" in statement for statement in statements)
    assert not any("extraction = NULL" in statement for statement in statements)
    assert pool.connection.commits == 1


async def test_oci_delete_document_removes_chunks_and_document_with_access_scope() -> None:
    """OCI mode の削除は access scope を維持して document と chunk を同一 transaction で消す。"""
    pool = FakeOraclePool(execute_results=[[_oracle_document_row()]])
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)
    token = set_audit_request_context(
        AuditRequestContext(
            request_id="request-1",
            allowed_document_ids=frozenset({"doc-1"}),
        )
    )
    try:
        deleted = await client.delete_document("doc-1")
    finally:
        reset_audit_request_context(token)

    assert deleted is True
    statements = [call.statement for call in pool.connection.calls]
    assert statements[0].startswith("SELECT")
    assert "document_id IN (:access_document_id_0)" in statements[0]
    assert "ORDER BY document_id" in statements[0]
    assert "FOR UPDATE" in statements[0]
    assert "duplicate_of_document_id = :document_id" in statements[0]
    assert any("FROM rag_graph_entity_chunks" in statement for statement in statements)
    assert any("DELETE FROM rag_graph_entity_chunks" in statement for statement in statements)
    # 関係情報の entity が無い文書は、#621 で廃止した表を見に行かない。
    assert not any("rag_graph_claims" in statement for statement in statements)
    assert not any("rag_graph_community_summaries" in statement for statement in statements)
    duplicate_clear = next(
        call
        for call in pool.connection.calls
        if call.statement.startswith("UPDATE rag_documents")
        and "duplicate_of_document_id = NULL" in call.statement
    )
    assert "duplicate_of_document_id = :document_id" in duplicate_clear.statement
    assert duplicate_clear.parameters["document_id"] == "doc-1"
    segment_delete_index = next(
        index
        for index, statement in enumerate(statements)
        if "DELETE FROM rag_ingestion_segments" in statement
    )
    job_delete_index = next(
        index
        for index, statement in enumerate(statements)
        if "DELETE FROM rag_ingestion_jobs" in statement
    )
    chunk_delete_index = next(
        index for index, statement in enumerate(statements) if "DELETE FROM rag_chunks" in statement
    )
    document_delete = next(
        call
        for call in pool.connection.calls
        if call.statement.startswith("DELETE FROM rag_documents")
    )
    document_delete_index = statements.index(document_delete.statement)
    assert segment_delete_index < document_delete_index
    assert job_delete_index < document_delete_index
    assert chunk_delete_index < document_delete_index
    assert "d.document_id IN (:access_document_id_0)" in statements[segment_delete_index]
    assert "d.document_id IN (:access_document_id_0)" in statements[job_delete_index]
    assert "document_id = :document_id" in document_delete.statement
    assert "document_id IN (:access_document_id_0)" in document_delete.statement
    assert document_delete.parameters["document_id"] == "doc-1"
    assert document_delete.parameters["access_document_id_0"] == "doc-1"
    assert pool.connection.commits == 1


async def test_oci_delete_document_rolls_back_when_running_job_appears() -> None:
    """削除 transaction 中に RUNNING job が見えたら document 物理削除へ進まない。"""
    pool = FakeOraclePool(
        execute_results=[
            [_oracle_document_row()],
            [],
            [{"job_id": "job-running"}],
        ]
    )
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    with pytest.raises(DocumentDeleteBlockedByRunningIngestionError):
        await client.delete_document("doc-1")

    statements = [call.statement for call in pool.connection.calls]
    assert any("DELETE FROM rag_ingestion_jobs" in statement for statement in statements)
    assert any("j.status = 'RUNNING'" in statement for statement in statements)
    assert not any(statement.startswith("DELETE FROM rag_documents") for statement in statements)
    assert pool.connection.commits == 0
    assert pool.connection.rollbacks == 1


async def test_oci_retrieval_applies_request_access_scope_predicates() -> None:
    """OCI retrieval SQL は request context の認可済み scope を必ず含める。"""
    pool = FakeOraclePool()
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)
    token = set_audit_request_context(
        AuditRequestContext(
            request_id="request-1",
            allowed_document_ids=frozenset({"doc-allowed"}),
            allowed_category_names=frozenset({"社内規程".casefold()}),
            allowed_knowledge_base_ids=frozenset({"kb-allowed"}),
        )
    )
    try:
        hits = await client.keyword_search("社内規程", top_k=3)
    finally:
        reset_audit_request_context(token)

    assert hits == []
    call = pool.connection.calls[0]
    assert "d.document_id IN (:access_document_id_0)" in call.statement
    assert "LOWER(d.category_name) IN (:access_category_name_0)" in call.statement
    assert "rag_document_knowledge_bases dkb" in call.statement
    assert "kb.knowledge_base_id IN (:access_knowledge_base_id_0)" in call.statement
    assert call.parameters["access_document_id_0"] == "doc-allowed"
    assert call.parameters["access_category_name_0"] == "社内規程".casefold()
    assert call.parameters["access_knowledge_base_id_0"] == "kb-allowed"


async def test_oci_retrieval_applies_multiple_knowledge_base_filters() -> None:
    """OCI retrieval SQL は複数 KB filter を bind 付き IN 条件で適用する。"""
    pool = FakeOraclePool()
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    hits = await client.keyword_search(
        "社内規程",
        top_k=3,
        filters={"knowledge_base_id": "kb-1,kb-2"},
    )

    assert hits == []
    call = pool.connection.calls[0]
    assert "rag_document_knowledge_bases dkb" in call.statement
    assert (
        "dkb.knowledge_base_id IN (:filter_knowledge_base_id_0, :filter_knowledge_base_id_1)"
    ) in call.statement
    assert call.parameters["filter_knowledge_base_id_0"] == "kb-1"
    assert call.parameters["filter_knowledge_base_id_1"] == "kb-2"


def test_oracle_text_terms_extracts_safe_display_keywords() -> None:
    """表示用 keyword terms は検索と同じ分割(#588)で、自然文から安全な語だけを返す。"""
    terms = oracle_text_terms("社内規程の申請フローは？")

    assert terms == ["社内規程", "社内", "規程", "申請フロー", "申請", "フロー"]
    assert "？" not in "".join(terms)
    assert not {"の申", "請フ", "ーは"} & set(terms)
    assert len(terms) <= 24


def test_oracle_text_terms_filters_japanese_particles_and_english_stopwords() -> None:
    """日本語助詞と英語 stopword は UI/Oracle Text query に出さない。"""
    assert oracle_text_terms("私の上司の興味はなんですか") == ["上司", "興味"]
    assert oracle_text_terms("申請へ承認") == ["申請", "承認"]
    english = oracle_text_terms("what is the expense policy and who approves it")
    assert {"expense", "policy", "approves"} <= set(english)
    assert not {"what", "is", "the", "and", "who"} & set(english)
    assert {"it", "policy", "hr"} <= set(oracle_text_terms("IT policy for HR"))


async def test_oci_keyword_search_normalizes_natural_language_query() -> None:
    """自然文 keyword query は Oracle Text 用の安全な term expression に変換する。"""
    pool = FakeOraclePool()
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)
    raw_query = "社内規程の申請フローは？"

    hits = await client.keyword_search(raw_query, top_k=3)

    assert hits == []
    call = pool.connection.calls[0]
    text_query = call.parameters["query"]
    assert isinstance(text_query, str)
    assert text_query != raw_query
    assert "？" not in text_query
    assert "{社内規程}" in text_query
    assert "{社内}" in text_query
    assert "{規程}" in text_query
    assert "{フロー}" in text_query
    assert " ACCUM " in text_query
    assert "CONTAINS(hit.search_text, :query, 1) > 0" in call.statement


async def test_oci_keyword_search_escapes_oracle_text_special_syntax() -> None:
    """Oracle Text の演算子/記号は literal term 化して query syntax にしない。"""
    pool = FakeOraclePool()
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    await client.keyword_search('policy - NEAR(oracle?) OR "x"', top_k=3)

    text_query = pool.connection.calls[0].parameters["query"]
    assert isinstance(text_query, str)
    assert "{policy}" in text_query
    assert "{oracle}" in text_query
    assert "{near}" not in text_query
    assert "{or}" not in text_query
    assert all(char not in text_query for char in '-?()"')


async def test_oci_keyword_search_empty_normalized_query_skips_database() -> None:
    """検索語にできない入力は DB に投げず空結果にする。"""
    pool = FakeOraclePool()
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    hits = await client.keyword_search("？！--", top_k=3)

    assert hits == []
    assert pool.acquire_calls == 0
    assert pool.connection.calls == []

    hits = await client.keyword_search("の んで です what is the", top_k=3)

    assert hits == []
    assert pool.acquire_calls == 0
    assert pool.connection.calls == []


async def test_oci_hybrid_search_uses_normalized_keyword_query() -> None:
    """hybrid 内の keyword branch も同じ Oracle Text query normalization を通る。"""
    pool = FakeOraclePool()
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)
    raw_query = "社内規程の申請フローは？"

    hits = await client.hybrid_search(
        query=raw_query,
        embedding=[1.0, 0.0, 0.0],
        top_k=3,
        mode=SearchMode.HYBRID,
    )

    assert hits == []
    keyword_call = next(call for call in pool.connection.calls if "CONTAINS" in call.statement)
    text_query = keyword_call.parameters["query"]
    assert isinstance(text_query, str)
    assert text_query != raw_query
    assert "{社内規程}" in text_query
    assert "{社内}" in text_query
    assert "？" not in text_query


def test_oracle_document_schema_includes_ingestion_metadata_columns() -> None:
    """Oracle document DDL 例は ingestion 監査用メタデータ列を含む。"""
    ddl = oracle_document_schema_sql()

    assert "tenant_id_hash           CHAR(64)" in ddl
    assert "content_sha256           CHAR(64)" in ddl
    assert "file_size_bytes          NUMBER(19)" in ddl
    assert "duplicate_of_document_id VARCHAR2(64)" in ddl
    assert "rag_documents_content_sha256_idx" in ddl
    assert "rag_documents_tenant_status_uploaded_idx" in ddl
    assert "'UPLOADED', 'PREPROCESSING', 'PREPROCESSED', 'INGESTING', 'REVIEW'," in ddl
    assert "'INDEXING', 'INDEXED', 'ERROR'" in ddl


def test_oracle_knowledge_base_schema_includes_membership_tables() -> None:
    """Oracle knowledge base DDL 例は KB と membership table を含む。"""
    ddl = oracle_knowledge_base_schema_sql()

    assert "CREATE TABLE rag_knowledge_bases" in ddl
    assert "CREATE TABLE rag_document_knowledge_bases" in ddl
    assert "knowledge_base_id     VARCHAR2(64) PRIMARY KEY" in ddl
    assert "default_search_mode   VARCHAR2(16) DEFAULT 'hybrid' NOT NULL" in ddl
    assert "rag_knowledge_bases_tenant_name_uidx" in ddl
    assert "FOREIGN KEY (document_id)" in ddl
    assert "REFERENCES rag_documents (document_id)" in ddl


def test_oracle_ingestion_job_schema_includes_queue_table() -> None:
    """Oracle ingestion job DDL 例は永続 queue table を含む。"""
    ddl = oracle_ingestion_job_schema_sql()

    assert "CREATE TABLE rag_ingestion_jobs" in ddl
    assert "job_id           VARCHAR2(64) PRIMARY KEY" in ddl
    assert "document_id      VARCHAR2(64) NOT NULL" in ddl
    assert "quality_warnings JSON" in ddl
    assert "attempt_count    NUMBER(5) DEFAULT 0 NOT NULL" in ddl
    assert "max_attempts     NUMBER(5) DEFAULT 3 NOT NULL" in ddl
    assert "phase            VARCHAR2(16) DEFAULT 'PREPROCESS' NOT NULL" in ddl
    assert (
        "CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED', 'CANCELLED'))"
        in ddl
    )
    assert "CHECK (phase IN ('PREPROCESS', 'EXTRACT', 'CHUNK', 'INDEX'))" in ddl
    assert "CHECK (attempt_count >= 0 AND max_attempts >= 1)" in ddl
    assert "REFERENCES rag_documents (document_id)" in ddl
    assert "ON rag_ingestion_jobs (tenant_id_hash, status, queued_at DESC)" in ddl


def test_oracle_ingestion_segment_schema_includes_checkpoint_table() -> None:
    """segment checkpoint は document 単位で page range / artifact path を永続化する。"""
    ddl = oracle_ingestion_segment_schema_sql()

    assert "CREATE TABLE rag_ingestion_segments" in ddl
    assert "segment_id     VARCHAR2(128) PRIMARY KEY" in ddl
    assert "artifact_path  VARCHAR2(1024)" in ddl
    assert "REFERENCES rag_documents (document_id)" in ddl
    assert "rag_ingestion_segments_document_status_idx" in ddl
    assert "CHECK (page_start IS NULL OR page_end IS NULL OR page_start <= page_end)" in ddl


def test_oracle_vector_schema_includes_tenant_filter_columns() -> None:
    """chunk/vector DDL 例は tenant filter 用の列と索引を含む。"""
    ddl = oracle_vector_schema_sql()

    assert "tenant_id_hash  CHAR(64)" in ddl
    assert "search_text     CLOB NOT NULL" in ddl
    assert "embedding       VECTOR(1024, FLOAT32)," in ddl
    assert "embedding       VECTOR(1024, FLOAT32) NOT NULL" not in ddl
    assert "CREATE VECTOR INDEX rag_chunks_embedding_hnsw_idx" in ddl
    assert "ORGANIZATION INMEMORY NEIGHBOR GRAPH" in ddl
    assert "DISTANCE COSINE" in ddl
    # 既定の検索精度「高精度」の推奨ビルド(#1990)。
    assert "WITH TARGET ACCURACY 98" in ddl
    assert "TYPE HNSW" in ddl
    assert "NEIGHBORS 48" in ddl
    assert "EFCONSTRUCTION 800" in ddl
    assert "CTX_DDL.CREATE_PREFERENCE('RAG_TEXT_WORLD_LEXER', 'WORLD_LEXER')" in ddl
    assert "CTX_DDL.CREATE_STOPLIST('RAG_TEXT_STOPLIST', 'BASIC_STOPLIST')" in ddl
    assert "ADD_STOPWORD('RAG_TEXT_STOPLIST', p_word)" in ddl
    assert "add_stopword('の')" in ddl
    assert "add_stopword('へ')" in ddl
    assert (
        "PARAMETERS ('LEXER RAG_TEXT_WORLD_LEXER STOPLIST RAG_TEXT_STOPLIST SYNC (ON COMMIT)')"
        in ddl
    )
    assert "rag_chunks_tenant_document_idx" in ddl
    assert "ON rag_chunks (tenant_id_hash, document_id, chunk_index)" in ddl
    assert "INDEXTYPE IS CTXSYS.CONTEXT" in ddl
    assert "ON rag_chunks (search_text)" in ddl


def test_oracle_search_audit_schema_redacts_query_body() -> None:
    """検索監査 DDL は query 原文ではなく hash と集計値だけを保存する。"""
    ddl = oracle_search_audit_schema_sql()
    normalized = ddl.lower()

    assert "create table rag_search_audit" in normalized
    assert "trace_id              varchar2(64) not null" in normalized
    assert "request_id            varchar2(128)" in normalized
    assert "tenant_id_hash        char(64)" in normalized
    assert "user_id_hash          char(64)" in normalized
    assert "search_mode           varchar2(16) not null" in normalized
    assert "query_hash            char(64) not null" in normalized
    assert "top_k                 number(10)" in normalized
    assert "rerank_top_n          number(10)" in normalized
    assert "query_variant_count   number(10) default 1 not null" in normalized
    assert "guardrail_codes       json" in normalized
    assert "reranked_count        number(10) default 0 not null" in normalized
    assert "deduplicated_count    number(10) default 0 not null" in normalized
    assert "context_diversified_count number(10) default 0 not null" in normalized
    assert "context_group_expanded_count number(10) default 0 not null" in normalized
    assert "context_expanded_count number(10) default 0 not null" in normalized
    assert "context_adaptive_expanded_count number(10) default 0 not null" in normalized
    assert "context_dependency_promoted_count number(10) default 0 not null" in normalized
    assert "context_compressed_count number(10) default 0 not null" in normalized
    assert "context_compression_saved_chars number(10) default 0 not null" in normalized
    assert "agent_memory_retrieved_count number(10) default 0 not null" in normalized
    assert "agent_memory_writeback_count number(10) default 0 not null" in normalized
    assert "agent_memory_writeback_status varchar2(32) default 'skipped' not null" in normalized
    assert "context_chars         number(10) default 0 not null" in normalized
    assert "config_fingerprint    char(64)" in normalized
    assert "document_ids          json" in normalized
    assert "knowledge_base_ids    json" in normalized
    assert "check (outcome in ('success', 'blocked', 'no_results', 'error'))" in normalized
    assert "check (search_mode in ('hybrid', 'vector', 'keyword'))" in normalized
    assert "check (agent_memory_writeback_status in ('skipped', 'saved', 'failed'))" in normalized
    assert "rag_search_audit_created_outcome_idx" in ddl
    assert "rag_search_audit_tenant_created_idx" in ddl
    assert "rag_search_audit_config_idx" in ddl
    assert "query_text" not in normalized
    assert "prompt" not in normalized
    assert " mode " not in normalized


def test_oracle_ingestion_audit_schema_redacts_ocr_body() -> None:
    """取込監査 DDL は原本 hash と件数を保存し、OCR 原文列を持たない。"""
    ddl = oracle_ingestion_audit_schema_sql()
    normalized = ddl.lower()

    assert "create table rag_ingestion_audit" in normalized
    assert "request_id             varchar2(128)" in normalized
    assert "tenant_id_hash         char(64)" in normalized
    assert "user_id_hash           char(64)" in normalized
    assert "document_id            varchar2(64) not null" in normalized
    assert "source_sha256          char(64) not null" in normalized
    assert "source_bytes           number(19) not null" in normalized
    assert "parser_backend         varchar2(80)" in normalized
    assert "parser_profile         varchar2(80)" in normalized
    assert "segment_count          number(10) default 0 not null" in normalized
    assert "fallback_count         number(10) default 0 not null" in normalized
    assert "failed_segment_count   number(10) default 0 not null" in normalized
    assert "chunk_count            number(10) default 0 not null" in normalized
    assert "vector_count           number(10) default 0 not null" in normalized
    assert "check (outcome in ('success', 'error'))" in normalized
    assert "rag_ingestion_audit_tenant_created_idx" in ddl
    assert "rag_ingestion_audit_document_created_idx" in ddl
    assert "rag_ingestion_audit_parser_created_idx" in ddl
    assert "raw_text" not in normalized
    assert "ocr_text" not in normalized


async def test_oci_ingestion_audit_persists_file_processing_metrics() -> None:
    """Oracle 取込監査は parser / segment の低機密集計を保存する。"""
    pool = FakeOraclePool()
    client = OracleClient(settings=_llm_settings(), pool=pool, db_call_runner=_run_inline)

    await client.save_ingestion_audit_event(
        {
            "event_type": "rag.ingestion",
            "trace_id": "trace-ingest",
            "request_id": "request-1",
            "tenant_id_hash": "a" * 64,
            "user_id_hash": "b" * 64,
            "document_id": "doc-1",
            "outcome": "success",
            "source_sha256": "c" * 64,
            "source_bytes": 1024,
            "document_type": "PDF",
            "extraction_confidence": 0.95,
            "parser_backend": "docling",
            "parser_profile": "enterprise_ai_pdf_layout",
            "segment_count": 4,
            "fallback_count": 1,
            "failed_segment_count": 2,
            "chunk_count": 30,
            "vector_count": 30,
            "elapsed_ms": 1234.5,
        }
    )

    call = pool.connection.calls[0]
    assert "INSERT INTO rag_ingestion_audit" in call.statement
    assert "parser_backend" in call.statement
    assert "parser_profile" in call.statement
    assert "segment_count" in call.statement
    assert "fallback_count" in call.statement
    assert "failed_segment_count" in call.statement
    assert call.parameters["parser_backend"] == "docling"
    assert call.parameters["parser_profile"] == "enterprise_ai_pdf_layout"
    assert call.parameters["segment_count"] == 4
    assert call.parameters["fallback_count"] == 1
    assert call.parameters["failed_segment_count"] == 2
    assert "raw_text" not in call.parameters
    assert "ocr_text" not in call.parameters
    assert pool.connection.commits == 1


def test_oracle_graph_feedback_and_eval_artifact_schema_use_oracle_tables() -> None:
    """GraphRAG-lite / feedback / eval artifact は Oracle table と JSON で表現する。"""
    graph_ddl = oracle_knowledge_graph_schema_sql()
    feedback_ddl = oracle_feedback_schema_sql()
    feedback_details_ddl = oracle_feedback_details_schema_sql()
    artifact_ddl = oracle_evaluation_artifact_schema_sql()

    assert "CREATE TABLE rag_graph_entities" in graph_ddl
    assert "CREATE TABLE rag_graph_relationships" in graph_ddl
    # claims / community summary は #621 で廃止した(読む経路が無かった)。
    assert "rag_graph_claims" not in graph_ddl
    assert "rag_graph_community_summaries" not in graph_ddl
    assert "CREATE TABLE rag_graph_entity_chunks" in graph_ddl
    assert "CREATE TABLE rag_citation_feedback" in feedback_ddl
    assert "target_type       VARCHAR2(16)" in feedback_ddl
    assert "source_surface    VARCHAR2(16)" in feedback_ddl
    assert "search_answer_profile_id  VARCHAR2(64)" in feedback_ddl
    assert "RAG_FEEDBACK_USER_TRACE_IDX" in feedback_ddl.upper()
    assert "rating = 'helpful' AND reason IS NULL" in feedback_ddl
    assert "target_type = 'answer' AND reason IN" in feedback_ddl
    assert "target_type = 'citation' AND reason IN" in feedback_ddl
    assert "comment_hash      CHAR(64)" in feedback_ddl
    assert "comment_text" not in feedback_ddl.lower()
    assert "comment_body" not in feedback_ddl.lower()
    assert "CREATE TABLE rag_feedback_details" in feedback_details_ddl
    assert "question_text     CLOB" in feedback_details_ddl
    assert "answer_text       CLOB" in feedback_details_ddl
    assert "comment_text      VARCHAR2(1000 CHAR)" in feedback_details_ddl
    assert "INDEXTYPE IS CTXSYS.CONTEXT" in feedback_details_ddl
    assert "SYNC (ON COMMIT)" in feedback_details_ddl
    assert "CREATE TABLE rag_evaluation_runs" in artifact_ddl
    assert "request_json      JSON NOT NULL" in artifact_ddl
    assert "result_json       JSON NOT NULL" in artifact_ddl
    assert "result_sha256     CHAR(64) NOT NULL" in artifact_ddl
    assert "rag_evaluation_runs_result_hash_idx" in artifact_ddl
    assert "NEO4J" not in (graph_ddl + feedback_ddl + artifact_ddl).upper()


@pytest.mark.anyio
async def test_feedback_write_and_current_read_use_append_only_latest_vote() -> None:
    """投票は INSERT し、current は利用者・対象ごとの最新行だけを返す。"""
    created_at = datetime(2026, 7, 1, tzinfo=UTC)
    pool = FakeOraclePool(
        execute_results=[
            [
                {
                    "feedback_id": "feedback-2",
                    "trace_id": "trace-1",
                    "search_answer_profile_id": "bv-1",
                    "target_type": "answer",
                    "source_surface": "chat",
                    "document_id": None,
                    "chunk_id": None,
                    "rating": "not_helpful",
                    "reason": "incorrect",
                    "comment": "古い回答です。",
                    "created_at": created_at,
                    "feedback_rank": 1,
                }
            ]
        ]
    )
    client = OracleClient(
        settings=Settings.model_construct(), pool=pool, db_call_runner=_run_inline
    )
    token = set_audit_request_context(
        AuditRequestContext(tenant_id_hash="a" * 64, user_id_hash="b" * 64)
    )
    try:
        feedback_id = await client.save_feedback(
            {
                "feedback_id": "feedback-1",
                "trace_id": "trace-1",
                "search_answer_profile_id": "bv-1",
                "target_type": "answer",
                "source_surface": "chat",
                "rating": "helpful",
            }
        )
        current = await client.list_current_feedback("trace-1")
    finally:
        reset_audit_request_context(token)

    insert = pool.connection.calls[0]
    select = pool.connection.calls[1]
    assert feedback_id == "feedback-1"
    assert "INSERT INTO rag_citation_feedback" in insert.statement
    assert insert.parameters["target_type"] == "answer"
    assert insert.parameters["search_answer_profile_id"] == "bv-1"
    assert insert.parameters["document_id"] is None
    assert insert.parameters["tenant_id_hash"] == "a" * 64
    assert insert.parameters["user_id_hash"] == "b" * 64
    assert "ROW_NUMBER() OVER" in select.statement
    assert "f.user_id_hash = :user_id_hash" in select.statement
    assert 'fd.comment_text AS "comment"' in select.statement
    assert current[0]["feedback_id"] == "feedback-2"
    assert current[0]["comment"] == "古い回答です。"
    assert "feedback_rank" not in current[0]


@pytest.mark.anyio
async def test_feedback_detail_uses_oracle_safe_comment_alias() -> None:
    """詳細取得でも予約語 COMMENT を引用し、API キーは comment のまま返す。"""
    pool = FakeOraclePool(
        execute_results=[
            [
                {
                    "feedback_id": "feedback-1",
                    "comment": "古い回答です。",
                    "citations_json": "[]",
                    "guardrail_codes": "[]",
                }
            ]
        ]
    )
    client = OracleClient(
        settings=Settings.model_construct(), pool=pool, db_call_runner=_run_inline
    )

    detail = await client.get_feedback_detail("feedback-1")

    assert 'fd.comment_text AS "comment"' in pool.connection.calls[0].statement
    assert detail is not None
    assert detail["comment"] == "古い回答です。"


@pytest.mark.anyio
async def test_feedback_details_are_committed_with_the_vote() -> None:
    """本文 snapshot は feedback event と同一 transaction で保存する。"""
    pool = FakeOraclePool()
    client = OracleClient(
        settings=Settings.model_construct(), pool=pool, db_call_runner=_run_inline
    )
    token = set_audit_request_context(AuditRequestContext(tenant_id_hash="a" * 64))
    try:
        await client.save_feedback(
            {
                "feedback_id": "feedback-1",
                "trace_id": "trace-1",
                "search_answer_profile_id": "bv-1",
                "target_type": "answer",
                "source_surface": "search",
                "rating": "not_helpful",
                "reason": "incorrect",
            },
            details={
                "content_source": "search_snapshot",
                "question_text": "質問",
                "answer_text": "回答",
                "comment_text": "古い回答です。",
                "citations": [],
            },
        )
    finally:
        reset_audit_request_context(token)

    assert len(pool.connection.calls) == 2
    assert "INSERT INTO rag_citation_feedback" in pool.connection.calls[0].statement
    assert "INSERT INTO rag_feedback_details" in pool.connection.calls[1].statement
    assert pool.connection.calls[1].parameters["search_text"] == "質問\n回答\n古い回答です。"
    assert pool.connection.commits == 1
    assert pool.connection.rollbacks == 0


@pytest.mark.anyio
async def test_feedback_dashboard_filters_tenant_and_aggregates_latest_votes() -> None:
    """一覧・件数・集計は同じ latest CTE と tenant/filter を使う。"""
    pool = FakeOraclePool(execute_results=[[], [{"total": 2}], [], []])
    client = OracleClient(
        settings=Settings.model_construct(), pool=pool, db_call_runner=_run_inline
    )
    token = set_audit_request_context(AuditRequestContext(tenant_id_hash="a" * 64))
    try:
        rows, total, groups, previous_groups = await client.list_feedback_dashboard_rows(
            search_answer_profile_id="bv-1",
            target_type="citation",
            rating="not_helpful",
            reason="not_relevant",
            period_days=30,
            search_query="規程",
            sort_order="newest",
            limit=20,
            offset=0,
        )
    finally:
        reset_audit_request_context(token)

    assert rows == []
    assert total == 2
    assert groups == []
    assert previous_groups == []
    assert len(pool.connection.calls) == 4
    for call in pool.connection.calls:
        assert "latest_feedback" in call.statement
        assert "f.tenant_id_hash = :tenant_id_hash" in call.statement
        assert call.parameters["tenant_id_hash"] == "a" * 64
        assert call.parameters["search_answer_profile_id"] == "bv-1"
        assert call.parameters["period_days"] == 30
        assert call.parameters["feedback_query"] == "{規程}"


async def test_vector_search_rejects_wrong_embedding_dimension() -> None:
    """検索 embedding が Oracle VECTOR 幅と違う場合は明示的に拒否する。"""
    client = OracleClient(settings=Settings.model_construct(genai_embedding_dim=3))

    with pytest.raises(ValueError, match="query embedding の次元数が不正です"):
        await client.vector_search([1.0, 0.0], top_k=1)


@dataclass
class SqlCall:
    """Fake Oracle connection が記録する単発 SQL 呼び出し。"""

    statement: str
    parameters: dict[str, object]
    input_sizes: dict[str, object]


@dataclass
class SqlManyCall:
    """Fake Oracle connection が記録する executemany 呼び出し。"""

    statement: str
    rows: list[dict[str, object]]


class FakeOraclePool:
    """python-oracledb pool の fake。"""

    def __init__(
        self,
        execute_results: list[list[dict[str, object]]] | None = None,
        missing_ingestion_job_max_attempts: bool = False,
        fetch_errors: Sequence[Exception] | None = None,
    ) -> None:
        self.connection = FakeOracleConnection(
            execute_results or [],
            missing_ingestion_job_max_attempts=missing_ingestion_job_max_attempts,
            fetch_errors=fetch_errors,
        )
        self.acquire_calls = 0
        self.close_calls = 0

    def acquire(self) -> "FakeOracleConnection":
        self.acquire_calls += 1
        return self.connection

    def close(self, force: bool = False) -> None:
        self.close_calls += 1


class ForceCloseOnlyPool:
    """busy pool の fake。force=True でしか閉じられない。"""

    def __init__(self) -> None:
        self.close_forces: list[bool] = []

    def acquire(self) -> "FakeOracleConnection":
        raise AssertionError("close_oracle_pool は acquire しない")

    def close(self, force: bool = False) -> None:
        self.close_forces.append(force)
        if not force:
            raise RuntimeError(
                "DPY-1005: connection pool cannot be closed because connections are busy"
            )


class FakeOracleConnection:
    """python-oracledb connection の fake。"""

    def __init__(
        self,
        execute_results: list[list[dict[str, object]]],
        *,
        missing_ingestion_job_max_attempts: bool = False,
        fetch_errors: Sequence[Exception] | None = None,
    ) -> None:
        self._execute_results = execute_results
        self.missing_ingestion_job_max_attempts = missing_ingestion_job_max_attempts
        self.fetch_errors = list(fetch_errors or ())
        self.calls: list[SqlCall] = []
        self.many_calls: list[SqlManyCall] = []
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def cursor(self) -> "FakeOracleCursor":
        return FakeOracleCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closes += 1

    def next_rows(self) -> list[dict[str, object]]:
        if not self._execute_results:
            return []
        return self._execute_results.pop(0)


class FakeOracleCursor:
    """python-oracledb cursor の fake。"""

    description: Sequence[Sequence[Any]] | None = None

    def __init__(self, connection: FakeOracleConnection) -> None:
        self._connection = connection
        self._input_sizes: dict[str, object] = {}
        self._rows: list[dict[str, object]] = []
        self.closed = False

    def setinputsizes(self, **kwargs: object) -> None:
        self._input_sizes.update(kwargs)

    def execute(self, statement: str, parameters: Mapping[str, object] | None = None) -> None:
        self._connection.calls.append(
            SqlCall(statement, dict(parameters or {}), dict(self._input_sizes))
        )
        if self._connection.missing_ingestion_job_max_attempts and (
            "j.max_attempts" in statement
            or ("INSERT INTO rag_ingestion_jobs" in statement and "max_attempts" in statement)
            or ("UPDATE rag_ingestion_jobs" in statement and "max_attempts" in statement)
        ):
            raise RuntimeError('ORA-00904: "J"."MAX_ATTEMPTS": invalid identifier')
        self._rows = (
            self._connection.next_rows() if statement.startswith(("SELECT", "WITH")) else []
        )

    def executemany(
        self,
        statement: str,
        parameters: Sequence[Mapping[str, object]],
    ) -> None:
        self._connection.many_calls.append(
            SqlManyCall(statement, [dict(row) for row in parameters])
        )

    def fetchone(self) -> dict[str, object] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, object]]:
        if self._connection.fetch_errors:
            raise self._connection.fetch_errors.pop(0)
        return self._rows

    def close(self) -> None:
        self.closed = True


def _llm_settings() -> Settings:
    return Settings.model_construct(
        genai_embedding_dim=3,
        rag_min_similarity=0.05,
        oracle_vector_target_accuracy=90,
        # 設定の target accuracy をそのまま使う balanced に固定する（既定は高精度。#272）。
        rag_vector_index_profile="balanced",
        oracle_user="rag_app",
        oracle_password="oracle-password",
        oracle_dsn="adb.example.com/rag",
    )


def _oracle_document_row(
    *,
    status: str = "INDEXED",
    extraction: object = '{"raw_text":"社内規程本文","document_type":"社内規程"}',
) -> dict[str, object]:
    return {
        "document_id": "doc-1",
        "file_name": "policy.txt",
        "status": status,
        "tenant_id_hash": None,
        "category_name": "社内規程",
        "object_storage_path": "oci://namespace/bucket/policy.txt",
        "content_type": "text/plain",
        "file_size_bytes": 12,
        "content_sha256": "a" * 64,
        "duplicate_of_document_id": None,
        "extraction": extraction,
        "error_message": None,
        "uploaded_at": datetime(2026, 1, 1, tzinfo=UTC),
        "indexed_at": None,
    }


def _oracle_knowledge_base_row(
    *,
    name: str = "社内規程",
    status: str = "ACTIVE",
    document_count: int = 0,
) -> dict[str, object]:
    return {
        "knowledge_base_id": "kb-1",
        "tenant_id_hash": None,
        "name": name,
        "description": "規程文書",
        "status": status,
        "default_search_mode": "hybrid",
        "retrieval_config": '{"top_k":20}',
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 2, tzinfo=UTC),
        "archived_at": None,
        "document_count": document_count,
        "indexed_document_count": 1 if document_count else 0,
        "error_document_count": 0,
        "searchable_chunk_count": 5 if document_count else 0,
    }


def _oracle_ingestion_job_row(
    *,
    job_id: str = "job-1",
    status: str = "QUEUED",
    attempt_count: int = 0,
    max_attempts: int = 3,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "job_id": job_id,
        "document_id": "doc-1",
        "status": status,
        "parser_profile": "enterprise_ai_pdf_layout",
        "quality_warnings": '["table_structure_review"]',
        "skip_reason": None,
        "error_message": None,
        "attempt_count": attempt_count,
        "max_attempts": max_attempts,
        "queued_at": datetime(2026, 1, 2, tzinfo=UTC),
        "started_at": started_at,
        "finished_at": finished_at,
    }


def _oracle_ingestion_segment_row(
    *,
    segment_id: str = "doc-1:p1-3",
    status: str = "SUCCEEDED",
) -> dict[str, object]:
    return {
        "segment_id": segment_id,
        "document_id": "doc-1",
        "status": status,
        "parser_backend": "enterprise_ai",
        "parser_profile": "enterprise_ai_pdf_layout",
        "page_start": 1,
        "page_end": 3,
        "attempt_count": 1,
        "artifact_path": "oci://namespace/bucket/artifacts/extractions/doc-1/trace.json",
        "error_code": None,
        "error_message": None,
    }


async def _run_inline(operation: Callable[[], Any]) -> Any:
    """テストでは同期 fake を同一 thread で実行する。"""
    return operation()
