"""状態の保存先の接続を pool から借りる（#830）。

- 状態の保存先（業務プロファイル・ジョブ・履歴・オントロジー・評価）は、読み書きのたびに新しい接続を
  張らず、`nl2sql-state` の pool から借りる。1 回のジョブで新しい接続を張る回数は、状態の読み書きの
  回数に比例しない
- 業務データ・Select AI の接続は状態の保存先の pool に混ぜない（`connection()` の単発の接続、
  または業務データの読み取り・Select AI の生成の `nl2sql-runtime` の pool。#904）
- stage の境界の実行所有権とキャンセル要求は、直前の stage の保存で読んだ job の文書で確かめる
  （#830 で 1 回の往復にし、#904 で保存と同じ往復にした）
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from app.clients.oracle_runtime import close_oracle_pools
from app.features.nl2sql import oracle_adapter
from app.features.nl2sql.incremental_store import (
    MemoryIncrementalNl2SqlRepository,
    OracleIncrementalNl2SqlRepository,
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
from app.settings import Settings


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "oracle_user": "APP",
        "oracle_password": "AppPass!123",  # nosec B106 - テスト用
        "oracle_dsn": "adb.example.oraclecloud.com:1522/app_high",
        "oracle_connection_security": "walletless_tls",
        "oracle_driver_mode": "thin",
        "nl2sql_oracle_call_timeout_seconds": 7,
        "nl2sql_oracle_state_pool_max": 6,
    }
    values.update(overrides)
    return Settings(**values)


class _Connection:
    def __init__(self) -> None:
        self.call_timeout = 0
        self.closed = 0
        self.rollbacks = 0

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1

    def cursor(self) -> _SessionCursor:
        # 単発の接続は、開くときに result cache を無効にする（`init_oracle_session`）だけ。
        return _SessionCursor()


class _SessionCursor:
    def __enter__(self) -> _SessionCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, binds: object = None) -> None:
        del binds
        assert sql.upper().startswith("ALTER SESSION")


class _Pool:
    def __init__(self, kwargs: dict[str, Any]) -> None:
        self.kwargs = kwargs
        self.acquired: list[_Connection] = []
        self.closed = False

    def acquire(self) -> _Connection:
        connection = _Connection()
        self.acquired.append(connection)
        return connection

    def close(self, force: bool = False) -> None:
        del force
        self.closed = True


class _FakeOracledb:
    """`create_pool` と `connect` の回数を数える python-oracledb の代わり。"""

    POOL_GETMODE_TIMEDWAIT = 3

    def __init__(self) -> None:
        self.pools: list[_Pool] = []
        self.connects = 0
        self._lock = threading.Lock()

    def create_pool(self, **kwargs: Any) -> _Pool:
        with self._lock:
            pool = _Pool(kwargs)
            self.pools.append(pool)
            return pool

    def connect(self, **_kwargs: Any) -> _Connection:
        with self._lock:
            self.connects += 1
        return _Connection()

    @property
    def acquisitions(self) -> int:
        return sum(len(pool.acquired) for pool in self.pools)


@pytest.fixture
def fake_oracledb(monkeypatch: pytest.MonkeyPatch) -> Iterator[_FakeOracledb]:
    fake = _FakeOracledb()
    pool = oracle_adapter._STATE_CONNECTION_POOL  # noqa: SLF001
    pool.close()
    monkeypatch.setattr(pool, "_oracledb_loader", lambda: fake)
    try:
        yield fake
    finally:
        pool.close()


def _adapter(fake: _FakeOracledb, monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> Any:
    adapter = OracleNl2SqlAdapter(_settings(**overrides))
    monkeypatch.setattr(adapter, "_oracledb", fake)
    return adapter


def test_state_connection_borrows_from_one_pool(
    fake_oracledb: _FakeOracledb, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(fake_oracledb, monkeypatch)

    for _ in range(5):
        with adapter.state_connection() as connection:
            assert connection.call_timeout == 7000
    with pytest.raises(RuntimeError), adapter.state_connection():
        raise RuntimeError("boom")

    assert fake_oracledb.connects == 0
    assert len(fake_oracledb.pools) == 1
    pool = fake_oracledb.pools[0]
    assert pool.kwargs["max"] == 6
    assert pool.kwargs["min"] == 1
    # 新しい接続ごとに result cache を無効にする（#333）。
    assert callable(pool.kwargs["session_callback"])
    assert len(pool.acquired) == 6
    # 借りた接続はすべて pool に返し、例外のときは rollback してから返す。
    assert all(connection.closed == 1 for connection in pool.acquired)
    assert pool.acquired[-1].rollbacks == 1
    # DB 設定の保存時・終了時（close_oracle_pools）に閉じる。
    close_oracle_pools()
    assert pool.closed


def test_nested_state_connection_does_not_wait_for_itself(
    fake_oracledb: _FakeOracledb, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _adapter(fake_oracledb, monkeypatch)

    with adapter.state_connection() as outer, adapter.state_connection() as inner:
        assert inner is not outer

    # 入れ子は pool の上限で自分を待たないよう単発の接続にし、抜けたら pool に戻る。
    assert fake_oracledb.connects == 1
    with adapter.state_connection():
        pass
    assert fake_oracledb.connects == 1
    assert fake_oracledb.acquisitions == 2


def test_business_data_connection_stays_single_shot(
    fake_oracledb: _FakeOracledb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Select AI・利用者の SQL の接続は session の状態を持ち得るため pool に混ぜない。"""
    adapter = _adapter(fake_oracledb, monkeypatch)

    with adapter.connection() as connection:
        assert connection.call_timeout == 7000

    assert fake_oracledb.connects == 1
    assert fake_oracledb.pools == []


def test_service_state_stores_use_the_state_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.features.nl2sql.ontology_router as ontology_router_module
    import app.features.nl2sql.quality_evaluation_service as quality_module
    from app.features.nl2sql import service as service_module
    from app.features.nl2sql.ontology_router import OntologyApiRuntime
    from app.features.nl2sql.quality_evaluation_service import QualityEvaluationService

    settings = _settings(nl2sql_persistence_mode="oracle")
    monkeypatch.setattr(service_module, "get_settings", lambda: settings)
    service = Nl2SqlService()
    repository = service._incremental_repository  # noqa: SLF001
    assert isinstance(repository, OracleIncrementalNl2SqlRepository)
    adapter = service._oracle_adapter  # noqa: SLF001
    assert repository._connection_factory == adapter.state_connection  # noqa: SLF001

    monkeypatch.setattr(ontology_router_module, "get_settings", lambda: settings)
    store = OntologyApiRuntime._default_store(service)  # noqa: SLF001
    assert store._connection_factory == adapter.state_connection  # type: ignore[attr-defined]  # noqa: SLF001

    monkeypatch.setattr(quality_module, "get_settings", lambda: settings)
    quality = QualityEvaluationService(service)
    assert quality._repository._connection_factory == adapter.state_connection  # type: ignore[attr-defined]  # noqa: SLF001


class _PooledMemoryRepository(MemoryIncrementalNl2SqlRepository):
    """Oracle の repository と同じく、公開メソッドの呼び出しごとに状態の接続を 1 本借りる。"""

    _METHODS = (
        "check",
        "get_change_token",
        "search_profiles",
        "get_profile",
        "list_profiles",
        "save_profile",
        "get_catalog_head",
        "load_catalog",
        "search_schema_objects",
        "get_schema_object",
        "schema_manifest",
        "claim_document",
        "patch_document_if_current",
        "put_document",
        "patch_document",
        "replace_documents",
        "get_document",
        "get_documents",
        "delete_document",
        "list_documents",
        "list_documents_page",
    )

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        super().__init__(seed_default=False)
        self._connection_factory = connection_factory
        self._inside = threading.local()
        self.calls: Counter[str] = Counter()
        for name in self._METHODS:
            setattr(self, name, self._through_pool(name, getattr(self, name)))

    def _through_pool(self, name: str, method: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            if getattr(self._inside, "value", False):
                return method(*args, **kwargs)
            self._inside.value = True
            try:
                with self._connection_factory():
                    self.calls[name] += 1
                    return method(*args, **kwargs)
            finally:
                self._inside.value = False

        return call


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


def _run_query_job(service: Nl2SqlService) -> Counter[str]:
    """ジョブを作り、同じスレッドで実行して（external の worker と同じ入口）、
    状態の操作を数える。"""
    repository = service._incremental_repository  # noqa: SLF001
    assert isinstance(repository, _PooledMemoryRepository)
    before = Counter(repository.calls)
    created = service.start_job(
        JobCreateRequest(
            question="注文一覧を確認したい",
            engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
            profile_id="orders-profile",
        ),
        actor_user_uuid="user-1",
        actor_is_system_admin=True,
    )
    assert service.run_next_nl2sql_job(worker_id="worker-1", job_id=created.job_id)
    job = service.get_job(created.job_id)
    assert job is not None
    assert job.status == JobStatus.DONE, job.error_message
    return Counter(repository.calls) - before


def test_query_jobs_do_not_open_a_connection_per_state_operation(
    fake_oracledb: _FakeOracledb, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.features.nl2sql import service as service_module
    from app.settings import get_settings

    settings = get_settings().model_copy(update={"nl2sql_job_worker_mode": "external"})
    monkeypatch.setattr(service_module, "get_settings", lambda: settings)
    adapter = _adapter(fake_oracledb, monkeypatch)
    repository = _PooledMemoryRepository(adapter.state_connection)
    catalog = _orders_catalog()
    manifest = {("APP", "ORDERS"): catalog.refreshed_at}
    repository.apply_schema_refresh(
        catalog=catalog, manifest=manifest, changed_keys=set(manifest), deleted_keys=set()
    )
    repository.save_profile(
        Nl2SqlProfile(id="orders-profile", name="注文管理", allowed_tables=["APP.ORDERS"]),
        expected_etag=None,
    )
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._incremental_repository = repository  # noqa: SLF001 - white-box contract test
    service._refresh_job_repository = repository  # noqa: SLF001
    service._persistence_ready = True  # noqa: SLF001
    service._persistence_writable = True  # noqa: SLF001
    service._catalog = repository.load_catalog()  # noqa: SLF001
    service._enterprise_ai_client = _EnterpriseAiClient()  # noqa: SLF001

    first = _run_query_job(service)
    acquisitions_after_first = fake_oracledb.acquisitions
    second = _run_query_job(service)

    # 1 回のジョブで状態を十数回読み書きするが、新しい接続（connect）は張らず、pool は 1 つだけ。
    assert sum(first.values()) >= 10
    assert fake_oracledb.connects == 0
    assert len(fake_oracledb.pools) == 1
    assert fake_oracledb.acquisitions - acquisitions_after_first == sum(second.values())
    # 2 回目のジョブも状態の操作の回数は増えない（キャッシュの分だけ減る）。
    assert sum(second.values()) <= sum(first.values())
    # stage の境界（6 回）の実行所有権とキャンセル要求は、直前の stage の保存（fence 付きの更新。
    # 開始・4 つの stage・結果の 6 回）が読んだ job の文書で確かめ、別に読むのは結果の保存の前の
    # 1 回だけ（#904。#830 では境界ごとに 1 回読んでいた）。保存の前に job を読み直さない。
    assert second["get_documents"] == 1
    assert second["patch_document_if_current"] == 6
    assert second["get_document"] <= 3


class _BatchCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.executed: list[tuple[str, dict[str, Any]]] = []

    def __enter__(self) -> _BatchCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, binds: dict[str, Any] | None = None) -> None:
        self.executed.append((" ".join(sql.split()), dict(binds or {})))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class _BatchConnection:
    def __init__(self, cursor: _BatchCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _BatchCursor:
        return self._cursor


def test_oracle_get_documents_reads_all_keys_in_one_round_trip() -> None:
    cursor = _BatchCursor([("jobs", "job-1", '{"status": "running"}')])
    connections: list[_BatchConnection] = []

    @contextmanager
    def factory() -> Iterator[_BatchConnection]:
        connection = _BatchConnection(cursor)
        connections.append(connection)
        yield connection

    repository = OracleIncrementalNl2SqlRepository(connection_factory=factory)

    documents = repository.get_documents(
        [("jobs", "job-1"), ("job_cancel_requests", "job-1"), ("jobs", "job-1")]
    )

    assert documents == {("jobs", "job-1"): {"status": "running"}}
    assert len(connections) == 1
    assert len(cursor.executed) == 1
    sql, binds = cursor.executed[0]
    assert sql.startswith("SELECT COLLECTION, ENTITY_ID, PAYLOAD_JSON FROM NL2SQL_STATE_DOCUMENTS")
    assert binds == {
        "collection_0": "jobs",
        "entity_id_0": "job-1",
        "collection_1": "job_cancel_requests",
        "entity_id_1": "job-1",
    }
    assert repository.get_documents([]) == {}
    assert len(connections) == 1
