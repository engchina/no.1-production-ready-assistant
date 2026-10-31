"""受付の幂等性・再認可・回復・Oracle の実件数を境界から検証する。"""

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import MagicMock, Mock

import pytest
from fastapi import HTTPException

from app.features.nl2sql import synthetic_service as module
from app.features.nl2sql.oracle_adapter import OracleNl2SqlAdapter
from app.features.nl2sql.synthetic_models import SyntheticRun, SyntheticRunRequest, SyntheticTarget
from app.features.nl2sql.synthetic_oracle import inspect_operation
from app.features.nl2sql.synthetic_service import SyntheticService
from app.features.nl2sql.synthetic_store import SyntheticConflict, SyntheticStore
from app.security.domain import Principal
from app.settings import Settings


def actor(name: Any = "owner") -> Principal:
    return Principal(
        user_uuid=name,
        login_user_id=name,
        display_name=name,
        status="ACTIVE",
        force_password_change=False,
        role_codes=[],
        permissions={"menu.data_management"},
        session_id="s",
        csrf_token_hash="",
    )


def run(**values: Any) -> SyntheticRun:
    return SyntheticRun(
        **{
            "run_id": "one",
            "actor_id": "owner",
            "context_id": "db",
            "idempotency_key": "key-one",
            "request_hash": "hash",
            "targets": [SyntheticTarget(table_name="APP.T", requested_rows=2)],
            **values,
        }
    )


def test_independent_same_table_runs_keep_idempotency_cas_and_context_isolation() -> None:
    store: Any = SyntheticStore()
    first = store.create(run())
    assert store.create(run(run_id="duplicate")).run_id == "one"
    second = store.create(run(run_id="two", idempotency_key="key-two"))
    assert second.status == "pending"
    with pytest.raises(SyntheticConflict):
        store.create(run(request_hash="changed"))
    assert store.create(run(run_id="other-db", context_id="db2")).run_id == "other-db"
    first.status = "unknown"
    assert store.save(first)
    assert not store.save(first)  # stale worker cannot overwrite a newer state
    assert store.create(run(run_id="three", idempotency_key="key-three")).run_id == "three"
    latest = store.get("one")
    latest.status = "no_data"
    assert store.save(latest)
    assert store.get("two").status == "pending"
    assert store.get("three").status == "pending"
    assert store.get("one").targets[0].loaded_rows is None


def test_idempotency_survives_more_than_100_records() -> None:
    store: Any = SyntheticStore()
    store.create(run())
    for i in range(101):
        store.create(
            run(
                run_id=f"r{i}",
                idempotency_key=f"k{i}",
                targets=[SyntheticTarget(table_name=f"APP.T{i}", requested_rows=1)],
            )
        )
    assert store.by_key("owner", "db", "key-one").run_id == "one"
    with pytest.raises(SyntheticConflict):
        store.create(run(run_id="duplicate", request_hash="changed"))


class OracleFixture:
    def __init__(self, operations: Any, chunks: Any) -> None:
        self.operations, self.chunks = operations, chunks
        self.calls: list[tuple[str, Any]] = []

    @contextmanager
    def connection(self) -> Iterator[Any]:
        yield self

    @contextmanager
    def cursor(self) -> Iterator[Any]:
        yield self

    def execute(self, sql: Any, params: Any = None) -> Any:
        self.calls.append((sql, params))

    def fetchone(self) -> Any:
        return None

    def fetchall(self) -> Any:
        return self.operations if len(self.calls) % 2 else self.chunks


@pytest.mark.parametrize(
    "op,chunks,expected,loaded",
    [
        ("COMPLETED", [("COMPLETED", 1), ("COMPLETED", 1)], "completed", 2),
        ("COMPLETED", [("COMPLETED", 0)], "no_data", 0),
        ("FAILED", [("FAILED", 0)], "failed", 0),
        ("FAILED", [("COMPLETED", 1), ("FAILED", 0)], "partial", 1),
        ("FAILED", [("COMPLETED", 2)], "partial", 2),
        ("COMPLETED", [("COMPLETED", None)], "unknown", None),
        ("RUNNING", [("COMPLETED", 2)], "running", 2),
    ],
)
def test_oracle_counts_never_use_requested_or_current_table_total(
    op: Any, chunks: Any, expected: Any, loaded: Any
) -> None:
    adapter: Any = OracleFixture(
        [(42, op, "SYNTHETIC_DATA$42_STATUS")],
        [
            (
                '"APP"."T"',
                status,
                rows,
                1 if status == "FAILED" else None,
                "失敗" if status == "FAILED" else None,
            )
            for status, rows in chunks
        ],
    )
    result = inspect_operation(
        adapter,
        run(
            status="running",
            session={
                "sid": 7,
                "serial": 9,
                "username": "APP",
                "since": datetime.now(UTC).isoformat(),
            },
        ),
    )
    assert result.status == expected
    assert result.targets[0].loaded_rows == loaded
    assert result.operation_ids == [42]
    sql, params = adapter.calls[0]
    assert "SERIAL#=:serial" in sql and params["sid"] == 7 and params["serial"] == 9
    assert "MAX(" not in sql


def test_missing_or_ambiguous_operation_cannot_be_completed() -> None:
    for operations in [
        [],
        [(1, "COMPLETED", "SYNTHETIC_DATA$1_STATUS"), (2, "COMPLETED", "SYNTHETIC_DATA$2_STATUS")],
    ]:
        result = inspect_operation(
            cast(Any, OracleFixture(operations, [])),
            run(
                status="unknown",
                session={
                    "sid": 1,
                    "serial": 2,
                    "username": "APP",
                    "since": datetime.now(UTC).isoformat(),
                },
            ),
        )
        assert result.status == "unknown"
        assert result.targets[0].loaded_rows is None


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> SyntheticService:
    settings = Settings(
        _env_file=None,
        nl2sql_runtime_mode="oracle",
        nl2sql_persistence_mode="oracle",
        oracle_user="APP",
    )
    svc = SyntheticService(settings, store=SyntheticStore(), resolve_actor=lambda _: actor())
    monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(svc, "preflight", Mock(return_value={"APP.T": 1}))
    monkeypatch.setattr(svc.adapter, "generate_synthetic_data", Mock())
    monkeypatch.setattr(
        svc.preview,
        "plan",
        lambda run: {
            t.table_name: {"name": f"APP.NL2SQL_SP_{run.run_id.replace('-', '').upper()}_{i}"}
            for i, t in enumerate(run.targets)
        },
    )
    monkeypatch.setattr(svc.preview, "prepare", Mock())
    return svc


def request(**values: Any) -> SyntheticRunRequest:
    return SyntheticRunRequest(
        **{
            "table_name": "APP.T",
            "profile_name": "P",
            "confirmation": "APP.T",
            "idempotency_key": "1234567890123456",
            **values,
        }
    )


def test_create_rejects_empty_profile_with_field_error(service: Any) -> None:
    """Profile の未選択は、画面の欄と同じ文言で /profile_name を指す 422（#540）。"""
    with pytest.raises(HTTPException) as error:
        service.create(request(profile_name=" "), actor())
    assert error.value.status_code == 422
    detail = error.value.detail
    assert isinstance(detail, dict)
    assert detail["field_errors"] == [
        {
            "pointer": "/profile_name",
            "code": "required",
            "message": "業務プロファイルを選択してください。",
        }
    ]
    service.adapter.generate_synthetic_data.assert_not_called()


def test_create_validates_confirmation_and_actor_even_on_idempotent_replay(service: Any) -> None:
    with pytest.raises(HTTPException) as error:
        service.create(request(confirmation=""), actor())
    assert error.value.status_code == 400
    created = service.create(request(), actor())
    assert created.status == "pending" and created.targets[0].loaded_rows is None
    assert service.create(request(), actor()).run_id == created.run_id
    service.adapter.generate_synthetic_data.assert_not_called()
    with pytest.raises(HTTPException) as error:
        service.create(request(), replace(actor(), permissions=set()))
    assert error.value.status_code == 403
    with pytest.raises(HTTPException) as error:
        service.get(created.run_id, actor("other"))
    assert error.value.status_code == 404
    assert "request" not in created.public() and "session" not in created.public()


@pytest.mark.parametrize("problem", ["revoked", "recreated", "context"])
def test_worker_rechecks_authorization_and_identity_before_any_write(
    service: Any, monkeypatch: pytest.MonkeyPatch, problem: Any
) -> None:
    created = service.create(request(), actor())
    created.status = "running"
    service.store.save(created)
    if problem == "revoked":
        service.resolve_actor = lambda _: replace(actor(), permissions=set())
    elif problem == "recreated":
        service.preflight.return_value = {"APP.T": 99}
    else:
        monkeypatch.setattr(
            module, "get_settings", lambda: Settings(_env_file=None, oracle_dsn="different")
        )
    service.execute(created.run_id)
    result = service.store.get(created.run_id)
    assert result.status == "failed" and result.targets[0].loaded_rows == 0
    service.adapter.generate_synthetic_data.assert_not_called()


def test_persist_session_before_write_and_recover_timeout_without_replay(
    service: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = service.create(request(), actor())
    created.status = "running"
    service.store.save(created)
    session = {"sid": 7, "serial": 8, "username": "APP", "since": datetime.now(UTC).isoformat()}
    monkeypatch.setattr(module, "capture_session", lambda _: session)

    def write(**kwargs: Any) -> Any:
        kwargs["on_connection"](None)
        assert service.store.get(created.run_id).session == session
        raise RuntimeError("応答が途切れました")

    service.adapter.generate_synthetic_data.side_effect = write
    service.execute(created.run_id)
    assert service.store.get(created.run_id).status == "unknown", service.store.get(
        created.run_id
    ).message
    recovered = SyntheticService(service.settings, store=service.store, adapter=service.adapter)
    monkeypatch.setattr(module, "inspect_operation", lambda _, value: value)
    recovered.tick()
    assert service.adapter.generate_synthetic_data.call_count == 1
    assert service.store.get(created.run_id).targets[0].loaded_rows is None


def test_failed_session_persistence_prevents_write(
    service: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = service.create(request(), actor())
    created.status = "running"
    service.store.save(created)
    monkeypatch.setattr(module, "capture_session", Mock(side_effect=RuntimeError("監視権限なし")))
    reached_write = []

    def write(**kwargs: Any) -> Any:
        kwargs["on_connection"](None)
        reached_write.append(True)

    service.adapter.generate_synthetic_data.side_effect = write
    service.execute(created.run_id)
    assert reached_write == []
    assert service.store.get(created.run_id).status == "failed"


@pytest.mark.asyncio
async def test_api_returns_202_without_work_and_scopes_reads(
    service: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx
    from fastapi import FastAPI, Request

    from app.features.nl2sql import synthetic_router

    app = FastAPI()
    current = actor()

    @app.middleware("http")
    async def authentication(req: Request, next_handler: Any) -> Any:
        req.state.principal = current
        return await next_handler(req)

    app.include_router(synthetic_router.router, prefix="/api/nl2sql")
    monkeypatch.setattr(synthetic_router, "get_synthetic_service", lambda: service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/api/nl2sql/synthetic-data/runs", json=request().model_dump())
        assert response.status_code == 202
        body = response.json()["data"]
        assert body["status"] == "pending" and body["targets"][0]["loaded_rows"] is None
        assert "session" not in body and "request" not in body
        replay = await client.post("/api/nl2sql/synthetic-data/runs", json=request().model_dump())
        assert replay.json()["data"]["run_id"] == body["run_id"]
        path = "/api/nl2sql/synthetic-data/runs/" + body["run_id"]
        assert response.headers["Location"] == path
        assert (await client.get(path)).status_code == 200
        next_response = await client.post(
            "/api/nl2sql/synthetic-data/runs",
            json=request(idempotency_key="independent-request-2").model_dump(),
        )
        assert next_response.status_code == 202
        assert next_response.json()["data"]["run_id"] != body["run_id"]
        changed_replay = await client.post(
            "/api/nl2sql/synthetic-data/runs", json=request(row_count=9).model_dump()
        )
        assert changed_replay.status_code == 409
        assert (await client.get(path + "/results?table_name=APP.OTHER")).status_code == 400
        assert (await client.get(path + "/results?table_name=APP.T&limit=0")).status_code == 422
        current = actor("other")
        assert (await client.get(path)).status_code == 404
        assert (await client.get("/api/nl2sql/synthetic-data/runs")).json()["data"] == []
    service.adapter.generate_synthetic_data.assert_not_called()


def test_store_decodes_oracle_json_dict_text_and_lob() -> None:
    value = run()
    for payload in (
        value.model_dump(),
        value.model_dump_json(),
        Mock(read=lambda: value.model_dump_json()),
    ):
        assert SyntheticStore.decode(payload) == value


def test_worker_return_is_only_verifying_and_claim_is_not_replayed(
    service: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    created = service.create(request(), actor())
    entered, release = threading.Event(), threading.Event()
    session = {"sid": 7, "serial": 8, "username": "APP", "since": datetime.now(UTC).isoformat()}
    monkeypatch.setattr(module, "capture_session", lambda _: session)
    monkeypatch.setattr(module, "inspect_operation", lambda _, value: value)

    def write(**kwargs: Any) -> Any:
        kwargs["on_connection"](None)
        entered.set()
        assert release.wait(3)

    service.adapter.generate_synthetic_data.side_effect = write
    service.tick()
    try:
        assert entered.wait(3)
        other_worker = SyntheticService(
            service.settings, store=service.store, adapter=service.adapter
        )
        other_worker.tick()
        assert service.adapter.generate_synthetic_data.call_count == 1
    finally:
        release.set()
        for thread in service._threads.values():
            thread.join(3)
    result = service.store.get(created.run_id)
    assert result.status == "verifying" and result.targets[0].loaded_rows is None


def test_worker_debug_identity_requires_current_explicit_debug_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.security import service as security
    from app.security.dependencies import LOCAL_DEBUG_USER_UUID

    settings = Settings(_env_file=None)
    resolver = security.SecurityService(Mock(), settings)
    monkeypatch.setattr(security, "get_settings", lambda: Mock(local_debug_enabled=True))
    assert resolver.principal_for_worker(LOCAL_DEBUG_USER_UUID).is_system_admin
    monkeypatch.setattr(security, "get_settings", lambda: Mock(local_debug_enabled=False))
    with pytest.raises(security.SecurityApiError):
        resolver.principal_for_worker(LOCAL_DEBUG_USER_UUID)


@pytest.mark.parametrize("prompt", ["", "  \n ", "日本語の部署名を生成してください。"])
@pytest.mark.parametrize("multiple", [False, True])
def test_oracle_synthetic_optional_prompt_contract(
    monkeypatch: pytest.MonkeyPatch, prompt: str, multiple: bool
) -> None:
    adapter = OracleNl2SqlAdapter(Settings(_env_file=None, oracle_user="APP"))
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value

    @contextmanager
    def connection() -> Iterator[Any]:
        yield conn

    monkeypatch.setattr(adapter, "connection", connection)
    adapter.generate_synthetic_data(
        table_name="" if multiple else "APP.T",
        object_list=["APP.T", "APP.U"] if multiple else [],
        row_count=2,
        profile_name="P",
        user_prompt=prompt,
        sample_rows=5,
    )
    sql, binds = cursor.execute.call_args.args
    if multiple:
        assert "object_list => :object_list" in sql
        objects = json.loads(binds["object_list"])
        assert [value["name"] for value in objects] == ["T", "U"]
        for value in objects:
            assert value["owner"] == "APP" and value["record_count"] == 2
            if prompt.strip():
                assert value["user_prompt"] == prompt
            else:
                assert "user_prompt" not in value
    else:
        assert binds["user_prompt"] == (prompt or None)
    assert json.loads(binds["params"]) == {"comments": True, "sample_rows": 5}
    conn.commit.assert_called_once()


PROMPT_REJECTION = (
    "DBMS_CLOUD_AI.GENERATE_SYNTHETIC_DATA に失敗しました: "
    "ORA-20000: Missing value for user_prompt in "
    '{"owner":"APP","name":"T","record_count":2,"user_prompt":null} '
    "in argument object_list\nORA-06512: at line 2"
)


@pytest.mark.parametrize(
    "evidence",
    [
        "rejected",
        "active_session",
        "no_return",
        "other_error",
        "unreadable",
        "observed_operation",
        "observed_rows",
    ],
)
def test_recover_only_proven_prompt_rejection_without_operation_or_session(
    service: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, evidence: str
) -> None:
    caplog.set_level(logging.INFO)
    created = service.create(request(), actor())
    created.status = "unknown"
    created.execution_returned = evidence != "no_return"
    if evidence == "observed_operation":
        created.operation_ids = [42]
    if evidence == "observed_rows":
        created.targets[0].loaded_rows = 1
    created.message = (
        PROMPT_REJECTION if evidence != "other_error" else "ORA-20000: Operation failed"
    )
    created.session = {
        "sid": 7,
        "serial": 9,
        "username": "APP",
        "since": datetime.now(UTC).isoformat(),
    }
    service.store.save(created)
    adapter = OracleFixture([], [])
    if evidence == "active_session":
        monkeypatch.setattr(adapter, "fetchone", lambda: ("ACTIVE",))
    elif evidence == "unreadable":
        monkeypatch.setattr(adapter, "fetchone", Mock(side_effect=RuntimeError("unavailable")))
    service.adapter = adapter
    service.reconcile(service.store.get(created.run_id))
    result = service.store.get(created.run_id)
    assert result.operation_ids == ([42] if evidence == "observed_operation" else [])
    if evidence == "rejected":
        assert result.status == "failed" and result.failure_phase == "validation"
        assert result.targets[0].loaded_rows == 0 and result.targets[0].status == "failed"
        assert result.finished_at and result.checked_at
        service.store.create(created.model_copy(update={"run_id": "new", "idempotency_key": "new"}))
        assert f"run_id={result.run_id} status=failed" in caplog.text
        assert "user_prompt" not in caplog.text
    else:
        assert result.status == "unknown" and result.failure_phase is None
        assert result.targets[0].loaded_rows == (1 if evidence == "observed_rows" else None)
        assert result.finished_at is None
        # 新しい明示的な生成は受理できるが、旧 unknown 自体は再送・成功扱いしない。
        service.store.create(created.model_copy(update={"run_id": "new", "idempotency_key": "new"}))
        assert service.store.get(created.run_id).status == "unknown"


def test_worker_runs_same_table_requests_independently_without_replaying_claims(
    service: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    first = service.create(request(), actor())
    second = service.create(request(idempotency_key="independent-request-2"), actor())
    sessions = [
        {"sid": sid, "serial": 9, "username": "APP", "since": datetime.now(UTC).isoformat()}
        for sid in [1, 2]
    ]
    monkeypatch.setattr(module, "capture_session", Mock(side_effect=sessions))
    monkeypatch.setattr(module, "inspect_operation", lambda _, value: value)
    lock, both_entered, release = threading.Lock(), threading.Event(), threading.Event()
    entered = []

    def write(**kwargs: Any) -> None:
        kwargs["on_connection"](None)
        with lock:
            entered.append(True)
            if len(entered) == 2:
                both_entered.set()
        assert release.wait(5)

    service.adapter.generate_synthetic_data.side_effect = write
    service.tick()
    try:
        assert both_entered.wait(3)
        assert service.store.get(first.run_id).session != service.store.get(second.run_id).session
        other_worker = SyntheticService(
            service.settings, store=service.store, adapter=service.adapter
        )
        other_worker.tick()
        assert service.adapter.generate_synthetic_data.call_count == 2
    finally:
        release.set()
        for thread in service._threads.values():
            thread.join(3)
    assert service.store.get(first.run_id).status == "verifying"
    assert service.store.get(second.run_id).status == "verifying"
    service.update(first.run_id, lambda value: setattr(value, "status", "completed"))
    assert service.store.get(second.run_id).status == "verifying"


@pytest.mark.parametrize("status", ["completed", "partial", "failed", "no_data"])
def test_review_window_boundary_keeps_history_and_isolates_context(status: Any) -> None:
    """生成の記録は期間で消さず（#3235）、確認用データの期間（24 時間）だけを判定する。"""
    from datetime import timedelta

    at = datetime.now(UTC)
    old = (at - timedelta(days=3)).isoformat()
    cutoff = (at - timedelta(hours=24)).isoformat()
    staging = {"APP.T": {"name": "APP.NL2SQL_SP_FIXTURE", "checksum": "digest", "count": 1}}
    store = SyntheticStore()
    values = [
        run(
            run_id="expired",
            idempotency_key="expired",
            status=status,
            created_at=old,
            finished_at=old,
            staging=staging,
        ),
        run(
            run_id="boundary",
            idempotency_key="boundary",
            status=status,
            created_at=old,
            finished_at=cutoff,
        ),
        run(
            run_id="recent-finish",
            idempotency_key="recent",
            status=status,
            created_at=old,
            finished_at=at.isoformat(),
        ),
        run(run_id="legacy", idempotency_key="legacy", status=status, created_at=old),
        run(run_id="other-actor", actor_id="other", status=status, created_at=old),
        run(run_id="other-db", context_id="other", status=status, created_at=old, staging=staging),
        *[
            run(run_id=s, idempotency_key=s, status=s, created_at=old, staging=staging)
            for s in ["pending", "running", "verifying", "unknown"]
        ],
    ]
    for value in values:
        store.create(value)
    for value in values:
        assert store.get(value.run_id) == value
    assert store.by_key("owner", "db", "expired") == values[0]
    assert {item.run_id for item in store.list("db", "owner")} >= {
        "expired",
        "boundary",
        "recent-finish",
        "legacy",
    }
    assert values[0].review_expired(at)
    assert values[3].review_expired(at)
    assert not values[1].review_expired(at)
    assert values[1].review_expired(at + timedelta(microseconds=1))
    assert not values[2].review_expired(at)
    assert not any(value.review_expired(at) for value in values[6:])
    # 後始末の対象は、一時表の管理情報が残る終端の記録だけ（実行中・別の DB は含めない）。
    assert [item.run_id for item in store.list_cleanup_candidates("db")] == ["expired"]


def test_service_keeps_expired_history_after_worker_tick(
    service: SyntheticService,
) -> None:
    from datetime import timedelta

    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    value = run(context_id=service.context, status="completed", created_at=old, finished_at=old)
    service.store.create(value)
    kept = service.get(value.run_id, actor())
    assert kept.public()["review_expired"] is True
    assert [item.run_id for item in service.list(actor())] == [value.run_id]
    service.tick()
    assert service.store.get(value.run_id) == value
    service.adapter.generate_synthetic_data.assert_not_called()  # type: ignore[attr-defined]


def test_worker_loop_logs_failure_cause_and_backs_off(caplog: pytest.LogCaptureFixture) -> None:
    from app.features.nl2sql import synthetic_service

    class StopAfter:
        def __init__(self, iterations: int) -> None:
            self.iterations = iterations
            self.waits: list[float] = []

        def is_set(self) -> bool:
            return len(self.waits) >= self.iterations

        def wait(self, seconds: float) -> None:
            self.waits.append(seconds)

    outcomes: list[Exception | None] = [
        RuntimeError("Oracle 接続に失敗しました: DPY-6005"),
        RuntimeError("Oracle 接続に失敗しました: DPY-6005"),
        None,
        *[RuntimeError("timeout")] * 6,
    ]

    def tick() -> None:
        outcome = outcomes.pop(0)
        if outcome:
            raise outcome

    stop = StopAfter(len(outcomes))
    with caplog.at_level(logging.WARNING, logger=synthetic_service.__name__):
        synthetic_service.worker_loop(cast(Any, stop), tick=tick)

    # 失敗が続くと 4 → 8 秒と伸び、成功で 2 秒に戻り、上限は 60 秒
    assert stop.waits == [4.0, 8.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
    first = caplog.records[0]
    assert first.getMessage() == "synthetic_worker_poll_failed"
    assert cast(Any, first).error_type == "RuntimeError"
    assert cast(Any, first).exception_type == "RuntimeError"
    assert cast(Any, first).error_code == "DPY-6005"
    assert not hasattr(first, "error")  # raw exception text は出力しない
    assert cast(Any, first).exception_frames[-1]["function"] == "tick"
    assert cast(Any, caplog.records[-1]).consecutive_failures == 6


@pytest.mark.parametrize("failure", ["configuration", "connection"])
def test_worker_keeps_pending_on_actor_service_outage_and_executes_once_after_recovery(
    service: Any, failure: str
) -> None:
    from app.features.nl2sql.oracle_adapter import OracleAdapterError
    from app.security.service import SecurityApiError

    created = service.create(request(), actor())
    error = (
        SecurityApiError(503, "構成管理者の認証情報が正しく設定されていません。")
        if failure == "configuration"
        else OracleAdapterError("Oracle 接続に失敗しました: timed out")
    )
    resolver = Mock(side_effect=error)
    service.resolve_actor = resolver
    for _ in range(2):
        with pytest.raises(type(error)):
            service.tick()
        saved = service.store.get(created.run_id)
        assert saved.status == "pending"
        assert saved.started_at is None and saved.finished_at is None
        assert saved.version == created.version
        assert not saved.worker_id
        assert not saved.session and not saved.operation_ids and not saved.staging
        service.adapter.generate_synthetic_data.assert_not_called()
        service.preview.prepare.assert_not_called()
    resolver.side_effect = None
    resolver.return_value = actor()
    service.tick()
    for thread in service._threads.values():
        thread.join(3)
        assert not thread.is_alive()
    completed = service.store.get(created.run_id)
    assert completed.status == "verifying"
    assert completed.worker_id
    assert "worker_id" not in completed.public()
    assert service.adapter.generate_synthetic_data.call_count == 1
    assert service.create(request(), actor()).run_id == created.run_id
    assert service.adapter.generate_synthetic_data.call_count == 1


def test_worker_does_not_retry_revoked_actor_as_configuration_outage(service: Any) -> None:
    from app.security.service import SecurityApiError

    created = service.create(request(), actor())
    service.resolve_actor = Mock(side_effect=SecurityApiError(403, "実行権限がありません。"))
    service.tick()
    for thread in service._threads.values():
        thread.join(3)
        assert not thread.is_alive()
    saved = service.store.get(created.run_id)
    assert saved.status == "failed" and saved.finished_at is not None
    assert saved.message == "実行権限がありません。"
    service.adapter.generate_synthetic_data.assert_not_called()
    service.preview.prepare.assert_not_called()


@pytest.mark.parametrize("case", ["recover", "exhausted", "credentials", "statement", "default"])
def test_synthetic_connection_retries_only_establishment_timeouts(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from app.features.nl2sql import oracle_adapter as oracle

    settings = Settings(
        _env_file=None,
        oracle_user="APP",
        oracle_password="fixture-only",
        oracle_dsn="localhost/FREEPDB1",
        oracle_connection_security="walletless_tls",
    )
    adapter = OracleNl2SqlAdapter(settings, connect_attempts=1 if case == "default" else 3)
    conn = MagicMock()
    timeout = RuntimeError("DPY-6005: cannot connect to database. timed out")
    driver = Mock()
    if case == "recover":
        driver.connect.side_effect = [timeout, timeout, conn]
    elif case == "statement":
        driver.connect.return_value = conn
    elif case == "credentials":
        driver.connect.side_effect = RuntimeError("ORA-01017: invalid credential")
    else:
        driver.connect.side_effect = timeout
    monkeypatch.setattr(adapter, "_load_oracledb", lambda: driver)
    monkeypatch.setattr(adapter, "_init_client", lambda _: None)
    monkeypatch.setattr("app.features.nl2sql.oracle_adapter.time.sleep", Mock())
    if case == "recover":
        with adapter.connection() as result:
            assert result is conn
        assert driver.connect.call_count == 3
        conn.close.assert_called_once()
    elif case == "statement":
        with pytest.raises(RuntimeError, match="DPY-6005"), adapter.connection():
            raise timeout
        assert driver.connect.call_count == 1
        conn.rollback.assert_called_once()
        conn.close.assert_called_once()
    else:
        with pytest.raises(oracle.OracleAdapterError), adapter.connection():
            pytest.fail("connection must not yield on failure")
        assert driver.connect.call_count == (3 if case == "exhausted" else 1)
