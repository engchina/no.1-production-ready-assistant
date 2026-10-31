"""1 回の「SQL を生成して実行」・チャットの 1 ターンの DB の新しい接続と往復を数える（#904）。

遅延の大きいネットワーク（開発機から ADB: 新しい接続 約 3 秒、1 往復 約 0.4 秒）では、所要時間は
「新しい接続の数 × 3 秒 + 往復の数 × 0.4 秒」でほぼ決まる。ここでは python-oracledb の代わりの
fake で、次を数える。

- 新しい接続: `oracledb.connect()` と、pool が新しく張る接続（借りるだけなら数えない）
- 往復: execute / executemany / callproc / commit / rollback / LOB の read / prefetch 0 の
  fetch（`autocommit` の execute は commit を同じ往復に載せる。CLOB を文字列で fetch すると
  LOB の read は起きない。python-oracledb Thin と同じ数え方）

状態の保存先（`NL2SQL_*` の表）は、結果をメモリの repository で作り、往復は Oracle の repository の
SQL を同じ引数で fake 接続へ流して数える（`_ShadowStateRepository`）。業務データの SQL・Select AI は
`OracleNl2SqlAdapter` をそのまま fake 接続で動かす。投入（`start_job`）と worker の処理を
分けて数え、pool がすでにある 2 回目のジョブ（定常）を比べる。

あわせて、system_admin / 非 system_admin で業務データの SQL が使う接続（pool）を確かめる
（Issue #904 の必須の要件）。

内訳の表示: `uv run pytest tests/test_nl2sql_db_roundtrips.py -s -k round_trips`
（HTTP の層（共通認証の session の確認など）とオントロジーの保存先の往復は含まない）
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import oracledb
import pytest

from app.clients import oracle_runtime
from app.features.nl2sql import oracle_adapter
from app.features.nl2sql import service as service_module
from app.features.nl2sql.incremental_store import (
    MemoryIncrementalNl2SqlRepository,
    OracleIncrementalNl2SqlRepository,
    _memory_document_payload,
    _profile_payload,
)
from app.features.nl2sql.models import (
    JobCreateRequest,
    JobStatus,
    Nl2SqlEngine,
    Nl2SqlProfile,
    SchemaCatalog,
    SchemaColumn,
    SchemaTable,
)
from app.features.nl2sql.oracle_adapter import OracleNl2SqlAdapter
from app.features.nl2sql.service import Nl2SqlService
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.settings import Settings, get_settings

# 往復に数える操作。
_ROUND_TRIP_OPS = frozenset(
    {"execute", "executemany", "callproc", "commit", "rollback", "lob_read", "fetch"}
)


@dataclass(frozen=True)
class DbCall:
    plane: str
    op: str
    detail: str = ""
    connection_id: int = 0


@dataclass
class DbRecorder:
    calls: list[DbCall] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, plane: str, op: str, detail: str = "", *, connection_id: int = 0) -> None:
        with self.lock:
            self.calls.append(
                DbCall(plane, op, " ".join(detail.split())[:120], connection_id=connection_id)
            )

    def mark(self) -> int:
        with self.lock:
            return len(self.calls)

    def window(self, start: int, end: int | None = None) -> list[DbCall]:
        with self.lock:
            return list(self.calls[start:end])


@dataclass(frozen=True)
class DbCost:
    """ある区間の新しい接続・往復の数（plane ごとの内訳つき）。"""

    new_connections: Counter[str]
    round_trips: Counter[str]
    acquires: Counter[str]

    @classmethod
    def of(cls, calls: list[DbCall]) -> DbCost:
        return cls(
            new_connections=Counter(c.plane for c in calls if c.op == "new_connection"),
            round_trips=Counter(c.plane for c in calls if c.op in _ROUND_TRIP_OPS),
            acquires=Counter(c.plane for c in calls if c.op == "acquire"),
        )

    @property
    def total_new_connections(self) -> int:
        return sum(self.new_connections.values())

    @property
    def total_round_trips(self) -> int:
        return sum(self.round_trips.values())

    def estimated_seconds(self, *, connect: float = 3.0, round_trip: float = 0.4) -> float:
        return self.total_new_connections * connect + self.total_round_trips * round_trip

    def describe(self) -> str:
        return (
            f"新しい接続 {self.total_new_connections} {dict(self.new_connections)} / "
            f"往復 {self.total_round_trips} {dict(self.round_trips)} / "
            f"推定 {self.estimated_seconds():.1f} 秒"
        )


class Clob:
    """fake の DB が返す CLOB 列の値（fetch の仕方で LOB か文字列になる）。"""

    def __init__(self, text: str) -> None:
        self.text = text


class FakeLob:
    def __init__(self, text: str, recorder: DbRecorder, plane: str) -> None:
        self._text = text
        self._recorder = recorder
        self._plane = plane

    def read(self, *_args: Any) -> str:
        self._recorder.record(self._plane, "lob_read")
        return self._text


class _ClobMetadata:
    type_code = oracledb.DB_TYPE_CLOB


Responder = Callable[[str, dict[str, Any]], list[tuple[Any, ...]]]


def _no_rows(_sql: str, _binds: dict[str, Any]) -> list[tuple[Any, ...]]:
    return []


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.description: list[tuple[str, ...]] | None = None
        self.prefetchrows = 2
        self.arraysize = 100
        self.outputtypehandler: Any = None
        self.rowcount = 0
        self._rows: list[tuple[Any, ...]] = []
        self._fetched = False

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def _record(self, op: str, detail: str = "") -> None:
        self.connection.recorder.record(
            self.connection.plane, op, detail, connection_id=id(self.connection)
        )

    def execute(self, sql: str, binds: Any = None, **kwargs: Any) -> None:
        params = dict(binds or {})
        params.update(kwargs)
        text = " ".join(sql.split())
        self._record("execute", text)
        self.connection.executed.append((text, params))
        rows = self.connection.responder(text, params)
        self.description = [(f"C{index}",) for index in range(len(rows[0]))] if rows else None
        self._rows = [tuple(self._value(value) for value in row) for row in rows]
        self.rowcount = len(rows)
        self._fetched = False

    def executemany(self, sql: str, rows: Any, **_kwargs: Any) -> None:
        self._record("executemany", sql)
        self.rowcount = len(list(rows))

    def callproc(self, name: str, params: Any = None) -> None:
        self._record("callproc", name)
        self.connection.executed.append((name, {"params": list(params or [])}))
        self.connection.on_callproc(name, list(params or []))
        if name in self.connection.fail_procs:
            raise RuntimeError(f"ORA-00000: {name} failed (test)")

    def _value(self, value: Any) -> Any:
        if not isinstance(value, Clob):
            return value
        handler = self.outputtypehandler or getattr(self.connection, "outputtypehandler", None)
        if handler is not None and handler(self, _ClobMetadata()) is not None:
            # CLOB を文字列で fetch する（LOB の read の往復が無い）。
            return value.text
        return FakeLob(value.text, self.connection.recorder, self.connection.plane)

    def _fetch(self) -> None:
        if self.prefetchrows == 0 and not self._fetched:
            # prefetch 0 の query は、行を fetch するときに往復する。
            self._record("fetch")
        self._fetched = True

    def fetchone(self) -> Any:
        self._fetch()
        return self._rows.pop(0) if self._rows else None

    def fetchmany(self, size: int = 100) -> list[Any]:
        self._fetch()
        rows, self._rows = self._rows[:size], self._rows[size:]
        return rows

    def fetchall(self) -> list[Any]:
        self._fetch()
        rows, self._rows = self._rows, []
        return rows

    def setinputsizes(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def var(self, *_args: Any, **_kwargs: Any) -> object:
        return object()


class FakeConnection:
    def __init__(self, recorder: DbRecorder, plane: str) -> None:
        self.recorder = recorder
        self.plane = plane
        self.call_timeout = 0
        self.autocommit = False
        self.transaction_in_progress = False
        self.outputtypehandler: Any = None
        self.fail_procs: set[str] = set()
        self.dropped = False
        self.responder: Responder = _no_rows
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.callprocs: list[tuple[str, list[Any]]] = []
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def on_callproc(self, name: str, params: list[Any]) -> None:
        self.callprocs.append((name, params))

    def commit(self) -> None:
        self.recorder.record(self.plane, "commit")
        self.transaction_in_progress = False

    def rollback(self) -> None:
        self.recorder.record(self.plane, "rollback")
        self.transaction_in_progress = False

    def close(self) -> None:
        self.closed = True
        self.recorder.record(self.plane, "close")


class FakePooledConnection(FakeConnection):
    def __init__(self, recorder: DbRecorder, plane: str, pool: FakePool) -> None:
        super().__init__(recorder, plane)
        self.pool = pool

    def close(self) -> None:
        if self.dropped:
            return
        self.recorder.record(self.plane, "release")
        self.pool.idle.append(self)


class FakePool:
    def __init__(self, oracledb_fake: FakeOracledb, kwargs: dict[str, Any]) -> None:
        self.fake = oracledb_fake
        self.kwargs = kwargs
        self.plane = oracledb_fake.plane_for(kwargs)
        self.idle: list[FakePooledConnection] = []
        self.created: list[FakePooledConnection] = []
        self.dropped: list[FakeConnection] = []
        self.closed = False

    def acquire(self) -> FakePooledConnection:
        recorder = self.fake.recorder
        recorder.record(self.plane, "acquire")
        if self.idle:
            return self.idle.pop()
        recorder.record(self.plane, "new_connection")
        connection = FakePooledConnection(recorder, self.plane, self)
        connection.responder = self.fake.responder
        connection.fail_procs = self.fake.fail_procs
        self.created.append(connection)
        callback = self.kwargs.get("session_callback")
        if callable(callback):
            callback(connection, None)
        return connection

    def drop(self, connection: FakeConnection) -> None:
        self.fake.recorder.record(self.plane, "drop")
        connection.dropped = True
        self.dropped.append(connection)

    def close(self, force: bool = False) -> None:
        del force
        self.closed = True


class FakeOracledb:
    """python-oracledb の代わり。`plane` で、どの接続（pool）の往復かを分けて記録する。"""

    POOL_GETMODE_TIMEDWAIT = 3
    DB_TYPE_CLOB = oracledb.DB_TYPE_CLOB
    DB_TYPE_LONG = oracledb.DB_TYPE_LONG

    def __init__(
        self,
        recorder: DbRecorder,
        plane: str,
        *,
        responder: Responder = _no_rows,
        plane_for: Callable[[dict[str, Any]], str] | None = None,
    ) -> None:
        self.recorder = recorder
        self.plane = plane
        self.responder = responder
        self._plane_for = plane_for
        self.pools: list[FakePool] = []
        self.connections: list[FakeConnection] = []
        self.fail_procs: set[str] = set()

    def created_connections(self) -> list[FakeConnection]:
        """pool が張った接続と `connect()` の接続（作った順）。"""
        return [*self.connections, *(c for pool in self.pools for c in pool.created)]

    def plane_for(self, kwargs: dict[str, Any]) -> str:
        return self._plane_for(kwargs) if self._plane_for else self.plane

    def connect(self, **_kwargs: Any) -> FakeConnection:
        self.recorder.record(self.plane, "new_connection")
        connection = FakeConnection(self.recorder, self.plane)
        connection.responder = self.responder
        self.connections.append(connection)
        return connection

    def create_pool(self, **kwargs: Any) -> FakePool:
        pool = FakePool(self, kwargs)
        self.pools.append(pool)
        return pool

    def is_thin_mode(self) -> bool:
        return True


# --- 状態の保存先: 結果はメモリ、往復は Oracle の repository の SQL で数える ---------------------


class _ShadowStateRepository(MemoryIncrementalNl2SqlRepository):
    """公開メソッドの呼び出しごとに、同じ引数で Oracle の repository を fake 接続に流す。

    fake 接続の SELECT には、呼び出し前のメモリの状態から行を返す（Oracle の実装と同じ分岐を通す）。
    結果はメモリの repository の値を返す。未対応の SQL は失敗させる（数え漏れを出さない）。
    """

    _METHODS = (
        "get_change_token",
        "get_profile",
        "get_catalog_head",
        "get_schema_object",
        "claim_document",
        "patch_document_if_current",
        "put_document",
        "patch_document",
        "get_document",
        "get_documents",
        "delete_document",
        "list_documents",
        "list_documents_page",
    )

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        super().__init__(seed_default=False)
        self._state_connection_factory = connection_factory
        self._inside = threading.local()
        self.calls: Counter[str] = Counter()
        self._oracle = OracleIncrementalNl2SqlRepository(connection_factory=self._shadow_connection)
        for name in self._METHODS:
            setattr(self, name, self._shadowed(name))

    @contextmanager
    def _shadow_connection(self) -> Iterator[Any]:
        with self._state_connection_factory() as connection:
            previous = connection.responder
            connection.responder = self._respond
            try:
                yield connection
            finally:
                connection.responder = previous

    def _shadowed(self, name: str) -> Callable[..., Any]:
        memory_method = getattr(MemoryIncrementalNl2SqlRepository, name)
        oracle_method = getattr(OracleIncrementalNl2SqlRepository, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            if getattr(self._inside, "call", None) is not None:
                return memory_method(self, *args, **kwargs)
            self._inside.call = (name, args, kwargs)
            try:
                self.calls[name] += 1
                oracle_method(self._oracle, *args, **kwargs)
                return memory_method(self, *args, **kwargs)
            finally:
                self._inside.call = None

        return call

    def _respond(self, sql: str, binds: dict[str, Any]) -> list[tuple[Any, ...]]:
        text = sql.upper()
        if text.startswith(("MERGE ", "UPDATE ", "DELETE ", "INSERT ", "LOCK TABLE")):
            return []
        if text.startswith("SELECT CHANGE_SEQ FROM NL2SQL_CHANGE_TOKENS"):
            return [(MemoryIncrementalNl2SqlRepository.get_change_token(self, binds["namespace"]),)]
        if "FROM NL2SQL_PROFILES WHERE PROFILE_ID = :PROFILE_ID" in text:
            profile = MemoryIncrementalNl2SqlRepository.get_profile(self, binds["profile_id"])
            if profile is None:
                return []
            payload = json.dumps(_profile_payload(profile), ensure_ascii=False)
            return [(Clob(payload), profile.version, profile.etag, profile.updated_at)]
        if text.startswith("SELECT H.CATALOG_VERSION"):
            head = MemoryIncrementalNl2SqlRepository.get_catalog_head(self)
            return [
                (
                    head.catalog_version,
                    head.schema_fingerprint,
                    head.refreshed_at,
                    head.object_count,
                    head.column_count,
                    head.etag,
                    head.change_token,
                )
            ]
        if "FROM NL2SQL_SCHEMA_" in text:
            return self._respond_schema_subset(text, binds)
        if text.startswith("SELECT PAYLOAD_JSON FROM NL2SQL_STATE_DOCUMENTS") and (
            "FETCH FIRST :LIMIT ROWS ONLY" in text
        ):
            name, args, kwargs = self._inside.call
            assert name == "list_documents", name
            documents = MemoryIncrementalNl2SqlRepository.list_documents(self, *args, **kwargs)
            return [(Clob(json.dumps(document, ensure_ascii=False)),) for document in documents]
        if text.startswith(
            "SELECT PAYLOAD_JSON FROM NL2SQL_STATE_DOCUMENTS "
            "WHERE COLLECTION = :COLLECTION AND ENTITY_ID"
        ):
            document = self._documents.get((binds["collection"], binds["entity_id"]))
            return [(_clob(document),)] if document is not None else []
        if text.startswith(
            "SELECT COLLECTION, ENTITY_ID, PAYLOAD_JSON FROM NL2SQL_STATE_DOCUMENTS"
        ):
            rows: list[tuple[Any, ...]] = []
            index = 0
            while f"collection_{index}" in binds:
                key = (binds[f"collection_{index}"], binds[f"entity_id_{index}"])
                if (document := self._documents.get(key)) is not None:
                    rows.append((key[0], key[1], _clob(document)))
                index += 1
            return rows
        if text.startswith("SELECT PAYLOAD_JSON, PROFILE_ID, STATUS FROM NL2SQL_STATE_DOCUMENTS"):
            document = self._documents.get((binds["collection"], binds["entity_id"]))
            if document is None:
                return []
            return [
                (
                    _clob(document),
                    str(document.get("_profile_id") or ""),
                    str(document.get("_status") or ""),
                )
            ]
        if text.startswith(
            "SELECT ENTITY_ID, PAYLOAD_JSON, PROFILE_ID FROM NL2SQL_STATE_DOCUMENTS"
        ):
            candidates = sorted(
                (
                    (entity_id, document)
                    for (collection, entity_id), document in self._documents.items()
                    if collection == binds["collection"]
                    and document.get("_status") in {"pending", "running"}
                    and binds.get("entity_id", entity_id) == entity_id
                ),
                key=lambda item: (str(item[1].get("created_at") or ""), item[0]),
            )
            return [
                (entity_id, _clob(document), str(document.get("_profile_id") or ""))
                for entity_id, document in candidates
            ]
        if text.startswith("SELECT COUNT(*) FROM NL2SQL_STATE_DOCUMENTS"):
            _items, _next, total = self._current_page()
            return [(total,)]
        if text.startswith("SELECT PAYLOAD_JSON,") and "AS SORT_KEY" in text:
            items, next_cursor, _total = self._current_page()
            page_rows: list[tuple[Any, ...]] = [
                (Clob(json.dumps(item)), str(index), str(index)) for index, item in enumerate(items)
            ]
            if next_cursor and page_rows:
                page_rows.append(page_rows[-1])
            return page_rows
        raise AssertionError(f"状態の保存先の未対応の SQL です: {sql}")

    def _current_page(self) -> tuple[list[dict[str, Any]], str | None, int]:
        name, args, kwargs = self._inside.call
        assert name == "list_documents_page", name
        return MemoryIncrementalNl2SqlRepository.list_documents_page(self, *args, **kwargs)

    def _respond_schema_subset(self, text: str, binds: dict[str, Any]) -> list[tuple[Any, ...]]:
        detail = MemoryIncrementalNl2SqlRepository.get_schema_object(
            self, binds["owner"], binds["object_name"]
        )
        if detail is None:
            return []
        table = detail.table
        if "FROM NL2SQL_SCHEMA_OBJECTS" in text:
            return [
                (
                    table.owner,
                    table.table_name,
                    table.table_type.upper(),
                    table.logical_name,
                    table.comment,
                    table.row_count,
                )
            ]
        if "FROM NL2SQL_SCHEMA_COLUMNS" in text:
            return [
                (
                    column.column_name,
                    column.logical_name,
                    column.data_type,
                    1 if column.nullable else 0,
                    column.comment,
                    Clob(json.dumps(column.sample_values)) if column.sample_values else None,
                )
                for column in table.columns
            ]
        if "FROM NL2SQL_SCHEMA_CONSTRAINTS" in text:
            return [
                (constraint, Clob(json.dumps(detail_item.model_dump(mode="json"))))
                for constraint, detail_item in zip(
                    table.constraints, table.constraint_details, strict=False
                )
            ]
        if "FROM NL2SQL_SCHEMA_DEPENDENCIES" in text:
            return [
                (
                    dependency.owner,
                    dependency.view_name,
                    dependency.referenced_owner,
                    dependency.referenced_name,
                    dependency.referenced_type,
                )
                for dependency in detail.dependencies
            ]
        raise AssertionError(f"DB 構造の未対応の SQL です: {text}")


def _clob(document: dict[str, Any]) -> Clob:
    return Clob(json.dumps(_memory_document_payload(document), ensure_ascii=False))


# --- 業務データの SQL・Select AI の応答 ----------------------------------------------------------


def _runtime_responder(sql: str, _binds: dict[str, Any]) -> list[tuple[Any, ...]]:
    text = sql.upper()
    if "DBMS_CLOUD_AI.GENERATE" in text:
        # DBMS_CLOUD_AI.GENERATE は CLOB を返す。
        return [(Clob("SELECT ID FROM APP.ORDERS"),)]
    if text.startswith("SELECT ID FROM APP.ORDERS"):
        return [(1,), (2,)]
    if text.startswith("ALTER SESSION"):
        return []
    raise AssertionError(f"業務データ・Select AI の未対応の SQL です: {sql}")


# --- シナリオ -------------------------------------------------------------------------------


class _EnterpriseAiClient:
    def is_configured(self) -> bool:
        return True

    def model_id(self) -> str:
        return "enterprise-nl2sql-model"

    def generate(self, **_kwargs: Any) -> str:
        return '{"sql":"SELECT ID FROM APP.ORDERS","explanation":"注文 ID を取得します。"}'


def _orders_catalog() -> SchemaCatalog:
    return SchemaCatalog(
        refreshed_at="2026-08-14T00:00:00+00:00",
        schema_fingerprint="incremental-schema-v1",
        current_owner="APP",
        tables=[
            SchemaTable(
                owner="APP",
                table_name="ORDERS",
                logical_name="注文",
                comment="注文",
                columns=[
                    SchemaColumn(
                        column_name="ID", logical_name="ID", data_type="NUMBER", nullable=False
                    )
                ],
            )
        ],
    )


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "oracle_user": "APP",
        "oracle_password": "AppPass!123",  # nosec B106 - テスト用
        "oracle_dsn": "adb.example.oraclecloud.com:1522/app_high",
        "oracle_connection_security": "walletless_tls",
        "oracle_driver_mode": "thin",
        "nl2sql_runtime_mode": "oracle",
        "nl2sql_persistence_mode": "oracle",
        "nl2sql_job_worker_mode": "external",
    }
    values.update(overrides)
    return get_settings().model_copy(update=values)


@dataclass
class Harness:
    service: Nl2SqlService
    settings: Settings
    recorder: DbRecorder
    repository: _ShadowStateRepository
    state: FakeOracledb
    runtime_admin: FakeOracledb
    runtime_user: FakeOracledb
    control: FakeOracledb

    def submit(self, request: JobCreateRequest, *, actor: str, is_system_admin: bool) -> Any:
        return self.service.start_job(
            request, actor_user_uuid=actor, actor_is_system_admin=is_system_admin
        )

    def measure(
        self, request: JobCreateRequest, *, actor: str = "user-1", is_system_admin: bool = True
    ) -> tuple[DbCost, DbCost, Any]:
        start = self.recorder.mark()
        created = self.submit(request, actor=actor, is_system_admin=is_system_admin)
        submitted = self.recorder.mark()
        assert self.service.run_next_nl2sql_job(worker_id="worker-1", job_id=created.job_id)
        finished = self.recorder.mark()
        job = self.service.get_job(created.job_id)
        assert job is not None
        assert job.status == JobStatus.DONE, job.error_message
        return (
            DbCost.of(self.recorder.window(start, submitted)),
            DbCost.of(self.recorder.window(submitted, finished)),
            job,
        )


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Harness]]:
    from app.features.nl2sql import ontology_router
    from app.features.nl2sql.ontology_store import InMemoryOntologyStore

    created_pools: list[Any] = []

    def build(**overrides: Any) -> Harness:
        settings = _settings(**overrides)
        monkeypatch.setattr(service_module, "get_settings", lambda: settings)
        recorder = DbRecorder()
        state = FakeOracledb(recorder, "state-pool")
        app = FakeOracledb(recorder, "app-connect", responder=_runtime_responder)
        runtime_admin = FakeOracledb(recorder, "runtime-admin", responder=_runtime_responder)
        runtime_user = FakeOracledb(recorder, "runtime-user", responder=_runtime_responder)
        control = FakeOracledb(recorder, "control", responder=_runtime_responder)
        for pool, fake in (
            (oracle_adapter._RUNTIME_ADMIN_CONNECTION_POOL, runtime_admin),  # noqa: SLF001
            (oracle_adapter._RUNTIME_USER_CONNECTION_POOL, runtime_user),  # noqa: SLF001
        ):
            pool.close()
            created_pools.append(pool)
            monkeypatch.setattr(pool, "_oracledb_loader", lambda fake=fake: fake)
        state_pool = oracle_adapter._STATE_CONNECTION_POOL  # noqa: SLF001
        state_pool.close()
        monkeypatch.setattr(state_pool, "_oracledb_loader", lambda: state)
        manager = oracle_runtime.OraclePoolManager(settings)
        manager._oracledb = control  # noqa: SLF001
        monkeypatch.setattr(oracle_runtime, "get_oracle_pool_manager", lambda: manager)

        service = Nl2SqlService(store=MemoryNl2SqlStore())
        adapter = OracleNl2SqlAdapter(settings)
        adapter._oracledb = app  # noqa: SLF001
        repository = _ShadowStateRepository(adapter.state_connection)
        catalog = _orders_catalog()
        manifest = {("APP", "ORDERS"): catalog.refreshed_at}
        MemoryIncrementalNl2SqlRepository.apply_schema_refresh(
            repository,
            catalog=catalog,
            manifest=manifest,
            changed_keys=set(manifest),
            deleted_keys=set(),
        )
        MemoryIncrementalNl2SqlRepository.save_profile(
            repository,
            Nl2SqlProfile(id="orders-profile", name="注文管理", allowed_tables=["APP.ORDERS"]),
            expected_etag=None,
        )
        service._oracle_adapter = adapter  # noqa: SLF001
        service._incremental_repository = repository  # noqa: SLF001
        service._refresh_job_repository = repository  # noqa: SLF001
        service._persistence_ready = True  # noqa: SLF001
        service._persistence_writable = True  # noqa: SLF001
        service._catalog = MemoryIncrementalNl2SqlRepository.load_catalog(repository)  # noqa: SLF001
        service._enterprise_ai_client = _EnterpriseAiClient()  # noqa: SLF001
        monkeypatch.setattr(
            ontology_router,
            "ontology_runtime",
            ontology_router.OntologyApiRuntime(
                legacy_service=service, store=InMemoryOntologyStore()
            ),
        )
        return Harness(
            service=service,
            settings=settings,
            recorder=recorder,
            repository=repository,
            state=state,
            runtime_admin=runtime_admin,
            runtime_user=runtime_user,
            control=control,
        )

    try:
        yield build
    finally:
        oracle_adapter._STATE_CONNECTION_POOL.close()  # noqa: SLF001
        for pool in created_pools:
            pool.close()


def _sql_job(engine: Nl2SqlEngine) -> JobCreateRequest:
    return JobCreateRequest(
        question="注文一覧を確認したい", engine=engine, profile_id="orders-profile"
    )


def _chat_turn(engine: Nl2SqlEngine, previous_job_id: str | None) -> JobCreateRequest:
    return JobCreateRequest(
        question="先月の注文に絞って",
        engine=engine,
        profile_id="orders-profile",
        generation_only=True,
        previous_job_id=previous_job_id,
    )


# 定常（pool があり、キャッシュが温まった 2 回目以降）の 1 ジョブの上限（#904 の修正後の値）。
# 修正前（main）: SQL 生成の worker は Select AI で新しい接続 2・往復 54、Enterprise AI で
# 新しい接続 1・往復 60。チャットの投入は 2 ターン目で往復 14 から 1 ターンごとに 5 増えていた。
# 業務データの SELECT は、session の時間帯を設定の時間帯にする・UTC に戻すの 2 往復を含む
# （#3265。設定が UTC なら 0）。
_SQL_JOB_LIMITS = {
    # engine: (投入の往復, worker の往復)
    Nl2SqlEngine.SELECT_AI: (2, 28),
    Nl2SqlEngine.ENTERPRISE_AI_DIRECT: (2, 36),
}
_CHAT_TURN_LIMITS = {
    Nl2SqlEngine.SELECT_AI: (4, 28),
    Nl2SqlEngine.ENTERPRISE_AI_DIRECT: (4, 36),
}
_ENGINE_IDS = ["select_ai", "enterprise_ai_direct"]


@pytest.mark.parametrize("engine", list(_SQL_JOB_LIMITS), ids=_ENGINE_IDS)
def test_sql_generation_job_db_round_trips(
    harness: Callable[..., Harness], engine: Nl2SqlEngine
) -> None:
    h = harness()
    # 1 回目は pool の最初の接続（状態の保存先・業務データ）とオントロジーの同期を含む。
    cold_submit, cold_run, _ = h.measure(_sql_job(engine))
    submit, run, _ = h.measure(_sql_job(engine))
    print(f"\n[{engine.value}] SQL 生成 1 回目 投入: {cold_submit.describe()}")
    print(f"[{engine.value}] SQL 生成 1 回目 worker: {cold_run.describe()}")
    print(f"[{engine.value}] SQL 生成 定常 投入: {submit.describe()}")
    print(f"[{engine.value}] SQL 生成 定常 worker: {run.describe()}")

    submit_limit, run_limit = _SQL_JOB_LIMITS[engine]
    # 定常では新しい接続を張らない（業務データ・Select AI も pool から借りる）。
    assert submit.total_new_connections == 0
    assert run.total_new_connections == 0
    assert submit.total_round_trips <= submit_limit
    assert run.total_round_trips <= run_limit
    # 1 回目でも、業務データ・Select AI の新しい接続は pool の 1 本だけ。
    assert cold_run.new_connections.get("app-connect", 0) == 0
    assert cold_run.new_connections.get("runtime-admin", 0) <= 1
    calls = h.recorder.window(0)
    # 状態の保存先の JSON CLOB は文字列で受け取る（LOB の read の往復が無い）。
    assert not [c for c in calls if c.op == "lob_read"]
    # commit は最後の文の実行に載せ、pool に返す接続の autocommit は元に戻す。
    assert not [c for c in calls if c.plane == "state-pool" and c.op == "commit"]
    assert all(not connection.autocommit for connection in h.state.created_connections())


def test_trace_span_writes_do_not_add_job_round_trips(harness: Callable[..., Harness]) -> None:
    """span の表の書き込み（#3226 の案 A）は書き手の別の接続で後から行い、ジョブの往復を増やさない。

    ジョブの文書の保存は段の切り替え・完了だけのまま（段の中の span で保存しない。#904）。
    """
    from trace_span_fake import FakeTraceSpanDb

    from app.features.nl2sql.trace_spans import install_trace_span_store, reset_trace_span_store

    h = harness()
    request = _sql_job(Nl2SqlEngine.ENTERPRISE_AI_DIRECT)
    h.measure(request)
    _, baseline, _ = h.measure(request)
    db = FakeTraceSpanDb()
    store = install_trace_span_store(db.connect, start=False)
    try:
        _, traced, job = h.measure(request)
        # 主の処理は queue に積むだけで、まだ表には書いていない（DB を待たない）。
        assert db.writes == 0
        store.writer.flush()
    finally:
        reset_trace_span_store()
    assert traced.total_round_trips == baseline.total_round_trips
    assert traced.total_new_connections == 0
    assert db.writes >= 1
    assert {row["operation_id"] for row in db.rows.values()} == {job.job_id}


@pytest.mark.parametrize("engine", list(_CHAT_TURN_LIMITS), ids=_ENGINE_IDS)
def test_chat_turn_db_round_trips_do_not_grow_with_turns(
    harness: Callable[..., Harness], engine: Nl2SqlEngine
) -> None:
    h = harness()
    h.measure(_sql_job(engine))
    _submit, _run, first = h.measure(_chat_turn(engine, None))
    previous = first.job_id
    submits: list[int] = []
    runs: list[int] = []
    for turn in range(2, 7):
        submit, run, job = h.measure(_chat_turn(engine, previous))
        previous = job.job_id
        print(f"\n[{engine.value}] チャット {turn} ターン目 投入: {submit.describe()}")
        print(f"[{engine.value}] チャット {turn} ターン目 worker: {run.describe()}")
        assert submit.total_new_connections == 0
        assert run.total_new_connections == 0
        submits.append(submit.total_round_trips)
        runs.append(run.total_round_trips)

    submit_limit, run_limit = _CHAT_TURN_LIMITS[engine]
    # 会話の前のターンを 1 件ずつ読み直さない（ターンが増えても往復は増えない）。
    assert len(set(submits)) == 1, submits
    assert len(set(runs)) == 1, runs
    assert submits[0] <= submit_limit
    assert runs[0] <= run_limit
    # 会話のジョブは 1 回の問い合わせでまとめて読む。投入と worker で 1 回ずつ（2〜6 ターン目）。
    assert h.repository.calls["list_documents"] == 2 * len(submits)


# --- 接続の使い分け（Issue #904 の必須の要件） ---------------------------------------------------

_BUSINESS_SQL = "SELECT ID FROM APP.ORDERS"


@pytest.mark.parametrize(
    ("is_system_admin", "business_plane"),
    [(True, "runtime-admin"), (False, "runtime-user")],
    ids=["system_admin", "user"],
)
@pytest.mark.parametrize("engine", list(_SQL_JOB_LIMITS), ids=_ENGINE_IDS)
# チャットのターンも SQL 生成のジョブと同じくジョブの中で実行する（#1176）。接続の分離も同じ。
@pytest.mark.parametrize("chat", [False, True], ids=["sql_job", "chat_turn"])
def test_business_sql_connection_follows_job_actor(
    harness: Callable[..., Harness],
    engine: Nl2SqlEngine,
    is_system_admin: bool,
    business_plane: str,
    chat: bool,
) -> None:
    """業務データの SQL の接続は、ジョブの actor（system_admin か）で決まる。

    - system_admin: アプリの接続の system_admin 用の pool（`nl2sql-runtime-admin`）
    - 非 system_admin: アプリの接続の非 system_admin 用の pool（`nl2sql-runtime-user`）。
      資格情報は同じでも、system_admin と pool・接続を共有しない
    - 状態の保存先の SQL は状態の pool だけ。業務データの SQL・Select AI と混ぜない
    """
    h = harness()
    app_plane = "runtime-admin" if is_system_admin else "runtime-user"
    connections_by_kind: dict[bool, set[int]] = {True: set(), False: set()}
    job = _sql_job(engine).model_copy(update={"chat": True}) if chat else _sql_job(engine)
    for actor in ("user-a", "user-b"):  # 同じ worker が 2 人の利用者のジョブを続けて処理する
        start = h.recorder.mark()
        h.measure(job, actor=actor, is_system_admin=is_system_admin)
        calls = h.recorder.window(start)

        business = [c for c in calls if c.op == "execute" and c.detail == _BUSINESS_SQL]
        assert [c.plane for c in business] == [business_plane]
        connections_by_kind[is_system_admin].update(c.connection_id for c in business)
        if engine == Nl2SqlEngine.SELECT_AI:
            # Select AI の生成は今までどおりアプリの接続（利用者の context を使わない）。actor の
            # 種類ごとに別の pool。
            generate = [c for c in calls if "DBMS_CLOUD_AI.GENERATE" in c.detail]
            assert [c.plane for c in generate] == [app_plane]
        state_sql = [c for c in calls if c.op == "execute" and "NL2SQL_" in c.detail]
        assert state_sql
        assert {c.plane for c in state_sql} == {"state-pool"}
        assert not [c for c in calls if c.plane == "state-pool" and c.detail == _BUSINESS_SQL]

        # 業務データの SQL は control pool（システムの表の DDL 用）では実行しない。
        assert not [c for c in calls if c.plane == "control"]

    # 同じ worker が、続けてもう一方の種類の actor のジョブを処理しても、業務データの SQL の接続は
    # 交わらない（system_admin 用と非 system_admin 用で同じ接続を使い回さない）。
    start = h.recorder.mark()
    h.measure(job, actor="user-c", is_system_admin=not is_system_admin)
    other = [c for c in h.recorder.window(start) if c.op == "execute" and c.detail == _BUSINESS_SQL]
    assert len(other) == 1
    connections_by_kind[not is_system_admin].update(c.connection_id for c in other)
    assert connections_by_kind[True]
    assert connections_by_kind[False]
    assert connections_by_kind[True].isdisjoint(connections_by_kind[False])
    admin_ids = {id(c) for c in h.runtime_admin.created_connections()}
    user_ids = {id(c) for c in h.runtime_user.created_connections()}
    assert admin_ids.isdisjoint(user_ids)
    assert connections_by_kind[True] <= admin_ids
    # アプリの接続の pool では session の context を一度も設定しない。
    assert all(
        not connection.callprocs
        for connection in [
            *h.runtime_admin.created_connections(),
            *h.runtime_user.created_connections(),
        ]
    )


@pytest.mark.parametrize(
    ("is_system_admin", "business_plane"),
    [(True, "runtime-admin"), (False, "runtime-user")],
    ids=["system_admin", "user"],
)
def test_chat_execution_connection_follows_requesting_actor(
    harness: Callable[..., Harness],
    is_system_admin: bool,
    business_plane: str,
) -> None:
    """チャットのターンの SQL の実行（#1154）も、SQL 生成のジョブと同じ接続の使い分けにする。

    実行は要求の利用者（system_admin か）で接続を選ぶ。system_admin はアプリの接続の
    system_admin 用の pool、非 system_admin は非 system_admin 用の pool。状態の保存
    （要約・実行履歴）は状態の pool だけ。
    """
    h = harness()
    _submit, _run, turn = h.measure(
        _chat_turn(Nl2SqlEngine.ENTERPRISE_AI_DIRECT, None),
        actor="user-a",
        is_system_admin=is_system_admin,
    )
    start = h.recorder.mark()
    data = h.service.execute_chat_turn(
        turn.job_id, actor_user_uuid="user-a", actor_is_system_admin=is_system_admin
    )
    calls = h.recorder.window(start)

    assert data.status == "done", data.error_message
    assert [list(row.values()) for row in data.results.rows] == [[1], [2]]
    business = [c for c in calls if c.op == "execute" and c.detail == _BUSINESS_SQL]
    assert [c.plane for c in business] == [business_plane]
    state_sql = [c for c in calls if c.op == "execute" and "NL2SQL_" in c.detail]
    assert state_sql
    assert {c.plane for c in state_sql} == {"state-pool"}
    # 業務データの SELECT の間だけ、その接続の session を設定の時間帯にし、終わると UTC に戻す
    # （#3265）。状態の保存先の接続は UTC のまま（時間帯を変えない）。
    time_zone = [
        (c.plane, c.detail)
        for c in calls
        if c.op == "execute" and c.detail.startswith("ALTER SESSION SET TIME_ZONE")
    ]
    assert time_zone == [
        (business_plane, "ALTER SESSION SET TIME_ZONE = '+09:00'"),
        (business_plane, "ALTER SESSION SET TIME_ZONE = '+00:00'"),
    ]
    business_window = [c.detail for c in calls if c.plane == business_plane and c.op == "execute"]
    start_zone = business_window.index("ALTER SESSION SET TIME_ZONE = '+09:00'")
    assert business_window[start_zone : start_zone + 3] == [
        "ALTER SESSION SET TIME_ZONE = '+09:00'",
        _BUSINESS_SQL,
        "ALTER SESSION SET TIME_ZONE = '+00:00'",
    ]
    # 業務データの SQL は control pool では実行せず、アプリの接続の pool では session の
    # context を設定しない。
    assert not [c for c in calls if c.plane == "control"]
    assert all(
        not connection.callprocs
        for connection in [
            *h.runtime_admin.created_connections(),
            *h.runtime_user.created_connections(),
        ]
    )


@pytest.mark.parametrize(
    ("is_system_admin", "plane"),
    [(True, "runtime-admin"), (False, "runtime-user")],
    ids=["system_admin", "user"],
)
def test_runtime_connection_rolls_back_open_transaction_before_returning(
    harness: Callable[..., Harness], is_system_admin: bool, plane: str
) -> None:
    from app.security.request_actor import actor_scope

    h = harness()
    adapter = h.service._oracle_adapter  # noqa: SLF001

    with actor_scope("user-a", is_system_admin=is_system_admin):
        with adapter.runtime_connection(call_timeout_seconds=300) as connection:
            assert connection.call_timeout == 300_000
            connection.transaction_in_progress = True
        with adapter.runtime_connection() as again:
            assert again is connection
            assert again.call_timeout == int(h.settings.nl2sql_oracle_call_timeout_seconds * 1000)

    ops = [(c.plane, c.op) for c in h.recorder.window(0)]
    assert ops.count((plane, "new_connection")) == 1
    assert (plane, "rollback") in ops
    # もう一方の種類の pool は使わない。
    assert not [op for op in ops if op[0].startswith("runtime-") and op[0] != plane]


def test_runtime_connection_without_actor_uses_single_connection(
    harness: Callable[..., Harness],
) -> None:
    """actor の無い処理（システムの処理・認証が無効のとき）は、どちらの pool にも入れない。"""
    h = harness()
    adapter = h.service._oracle_adapter  # noqa: SLF001

    rows = adapter.execute_select(_BUSINESS_SQL, 10)

    assert rows.total == 2
    ops = [(c.plane, c.op) for c in h.recorder.window(0)]
    assert ("app-connect", "new_connection") in ops
    assert not [op for op in ops if op[0].startswith("runtime-")]
    assert h.runtime_admin.pools == []
    assert h.runtime_user.pools == []


def test_close_oracle_pools_closes_both_runtime_pools(
    harness: Callable[..., Harness], monkeypatch: pytest.MonkeyPatch
) -> None:
    from functools import lru_cache

    from app.clients.oracle_runtime import close_oracle_pools
    from app.security.request_actor import actor_scope

    h = harness()
    # close_oracle_pools は lru_cache の get_oracle_pool_manager を前提にする。
    manager = oracle_runtime.get_oracle_pool_manager()
    monkeypatch.setattr(oracle_runtime, "get_oracle_pool_manager", lru_cache(lambda: manager))
    adapter = h.service._oracle_adapter  # noqa: SLF001
    for is_system_admin in (True, False):
        with actor_scope("user-a", is_system_admin=is_system_admin):
            adapter.execute_select(_BUSINESS_SQL, 10)
    assert len(h.runtime_admin.pools) == 1
    assert len(h.runtime_user.pools) == 1

    close_oracle_pools()

    assert all(pool.closed for pool in [*h.runtime_admin.pools, *h.runtime_user.pools])
