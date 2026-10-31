"""ジョブの処理の経過（チャット・SQL 生成・実行履歴の `ChatProgress`。#1359・#3226 の案 A）。

- 処理の経過は実行の trace（span の木）の投影: 実行の全体（root）の下に、開始待ち（`queue`）と
  5 段階の span が並ぶ。段の span は表示用のデータの `kind` に段階の id、補足の値（生成方法・
  参照した表・行数）は `params`、開いて見る詳細は `details`。
- span は共通の span の表に書く（worker の書き手。ジョブの保存を待たずに、段の中の span も実行中から
  出る）。ジョブには trace id・書けなかった span の数・工程の詳細（表には入れない内容）だけを置く。
- 読み出し: `GET /jobs/{job_id}/trace`（span の表の snapshot。権限は `GET /jobs/{job_id}` と同じ。
  最終の状態はジョブの状態で決める）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pr_backend_core.chat_progress import (
    ChatProgressEvent,
    ChatProgressStepState,
    ChatProgressTerminalEvent,
    fold_chat_progress_events,
)
from pr_backend_core.observability.trace_store import TraceSpanOwner
from pr_backend_core.observability.tracing import SpanDisplay, SpanUpdate
from test_nl2sql_job_runtime import (
    _FakeEnterpriseAiClient,
    _put_in_flight_snapshot,
    _repository,
    _request,
    _worker,
)
from test_nl2sql_operation_profile_access import _principal
from trace_span_fake import TraceTable, trace_table  # noqa: F401 - fixture

from app.features.nl2sql import router as nl2sql_router
from app.features.nl2sql.incremental_store import MemoryIncrementalNl2SqlRepository
from app.features.nl2sql.models import JobCreateRequest, JobData, JobStatus
from app.features.nl2sql.service import (
    JOB_CANCELLED_ERROR_CODE,
    Nl2SqlService,
)
from app.features.nl2sql.trace_spans import install_trace_span_store, reset_trace_span_store
from app.security.permissions import QUERY_GENERATE_PERMISSION, SQL_EXECUTE_PERMISSION
from app.settings import get_settings

_PROFILE_ID = "orders-profile"
_STEPS = [
    "queue",
    "prepare_context",
    "generate_sql",
    "safety_check",
    "execute_sql",
    "format_results",
]

type Fixture = tuple[Nl2SqlService, MemoryIncrementalNl2SqlRepository]


@pytest.fixture
def jobs(monkeypatch: pytest.MonkeyPatch, trace_table: TraceTable) -> Fixture:  # noqa: F811
    # worker は明示して動かす（記録の位置を 1 つずつ確かめる）。
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    repository = _repository()
    return _worker(repository), repository


def chat_turn(**update: Any) -> JobCreateRequest:
    return _request().model_copy(update={"chat": True, **update})


def start(service: Nl2SqlService, req: JobCreateRequest | None = None) -> str:
    created = service.start_job(
        req or chat_turn(), actor_user_uuid="user-1", actor_is_system_admin=True
    )
    return created.job_id


def run(service: Nl2SqlService, job_id: str) -> JobData:
    assert service.run_next_nl2sql_job(worker_id="progress-test", job_id=job_id)
    job = service.get_job(job_id, actor_user_uuid="user-1")
    assert job is not None
    return job


def events_of(service: Nl2SqlService, table: TraceTable, job_id: str) -> list[ChatProgressEvent]:
    """書き手の queue を書いてから、画面と同じ読み出し（system_admin 以外の利用者）で読む。"""

    table.flush()
    data = service.get_job_trace(job_id, actor_user_uuid="user-1")
    assert data is not None
    return data.trace.events


def stages(events: list[ChatProgressEvent]) -> dict[str, ChatProgressStepState]:
    """工程の span（`kind` が段階の id）。"""

    fold = fold_chat_progress_events(events)
    return {step.kind: step for step in fold.step_list() if step.kind in _STEPS}


def states(events: list[ChatProgressEvent]) -> list[str]:
    return [f"{kind}:{step.status}" for kind, step in stages(events).items()]


def params(events: list[ChatProgressEvent], kind: str) -> dict[str, Any]:
    return dict(stages(events)[kind].params or {})


def root(events: list[ChatProgressEvent]) -> ChatProgressStepState:
    fold = fold_chat_progress_events(events)
    [found] = [step for step in fold.step_list() if (step.span or {}).get("root")]
    return found


# --- 書き込み -------------------------------------------------------------------------------


def test_created_job_has_no_trace_until_the_worker_starts(
    jobs: Fixture,
    trace_table: TraceTable,  # noqa: F811
) -> None:
    """worker が受け取るまでは span が無い（画面は開始待ちの仮の表示を出す。#3226）。"""

    service, repository = jobs
    created = service.start_job(chat_turn(), actor_user_uuid="user-1", actor_is_system_admin=True)
    job = service.get_job(created.job_id, actor_user_uuid="user-1")
    assert job is not None and job.status == JobStatus.PENDING and job.trace_id == ""
    document = repository.get_document("jobs", created.job_id)
    assert document is not None
    assert "progress_events" not in document and document["trace_id"] == ""
    assert events_of(service, trace_table, created.job_id) == []
    assert trace_table.db.rows == {}


def test_done_chat_turn_writes_the_span_tree_to_the_table(
    jobs: Fixture,
    trace_table: TraceTable,  # noqa: F811
) -> None:
    service, repository = jobs
    job = run(service, start(service))
    assert job.status == JobStatus.DONE, job.error_message
    trace_table.flush()
    # span は共通の span の表に、ジョブ（`OPERATION_ID`）・実行した利用者・試行で入る。
    rows = trace_table.db.spans(operation_id=job.job_id)
    assert {row["product"] for row in rows} == {"nl2sql"}
    assert {row["owner_user_uuid"] for row in rows} == {"user-1"}
    assert {row["attempt"] for row in rows} == {0}
    assert {row["trace_id"] for row in rows} == {job.trace_id}
    [root_row] = [row for row in rows if row["is_root"]]
    assert root_row["name"] == "nl2sql.execute" and root_row["status"] == "done"
    # 工程の詳細（生成した SQL など）は表に入れない（ジョブの記録に置く）。
    assert job.result is not None
    assert job.result.generated_sql not in json.dumps(rows, default=str)
    document = repository.get_document("jobs", job.job_id)
    assert document is not None
    assert "progress_events" not in document and "trace_summary" not in document
    assert document["trace_id"] == job.trace_id

    events = events_of(service, trace_table, job.job_id)
    # snapshot: span ごとに 1 件と終端。番号はこの一覧の中の番号。
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert {event.target_id for event in events} == {job.job_id}
    assert states(events) == [f"{step}:done" for step in _STEPS]
    fold = fold_chat_progress_events(events)
    assert fold.terminal == "done"
    top = root(events)
    assert top.span is not None and top.span["name"] == "nl2sql.execute"
    for kind, step in stages(events).items():
        assert step.span is not None
        assert step.span["name"] == f"nl2sql.{kind}"
        assert step.span["parent_id"] == top.step_id
        assert step.span["trace_id"] == job.trace_id
    # 補足の値: 生成方法・参照した表・取得した行数（名前と文言は画面が付ける）。
    assert params(events, "generate_sql") == {"engine": "enterprise_ai_direct"}
    assert params(events, "safety_check") == {"tables": "APP.ORDERS", "table_count": 1}
    assert job.last_execution is not None
    assert params(events, "execute_sql") == {"rows": job.last_execution.row_count}
    # 段を開いて見る詳細（#2294）はジョブの記録から足す。
    found = stages(events)
    generate = found["generate_sql"].details
    assert generate is not None and generate.code[0].language == "sql"
    assert generate.code[0].text == job.result.generated_sql
    safety = found["safety_check"].details
    assert safety is not None
    assert {field.label: field.value for field in safety.fields}["結果"] == "安全"
    execute = found["execute_sql"].details
    assert execute is not None and execute.fields[0].label == "取得した行数"
    assert found["queue"].details is None
    # 開始待ちはジョブの作成から worker の開始まで。
    queue = found["queue"]
    assert queue.started_at == datetime.fromisoformat(job.created_at)
    assert queue.finished_at == datetime.fromisoformat(job.started_at or "")
    assert isinstance(events[-1], ChatProgressTerminalEvent)

    # 実行履歴の行も同じ実行（ジョブ）の span を指す。
    [history] = repository.list_documents("history", limit=100)
    assert history["job_id"] == job.job_id and history["trace_id"] == job.trace_id
    assert set(history["span_details"]) == set(document["span_details"])
    # 別の worker（プロセス）が読んでも同じ。
    observer = _worker(repository)
    seen = observer.get_job_trace(job.job_id, actor_user_uuid="user-1")
    assert seen is not None
    assert [e.model_dump(exclude={"emitted_at"}) for e in seen.trace.events] == [
        e.model_dump(exclude={"emitted_at"}) for e in events
    ]


def test_inner_spans_reach_the_table_before_the_job_is_saved(
    jobs: Fixture,
    trace_table: TraceTable,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """段の中の span は段の切り替えの保存を待たずに表に入る（実行中の画面に出る。#3226 の案 A）。"""

    service, _ = jobs
    job_id = start(service)
    seen: dict[str, Any] = {}
    generate = service._generate_selected_engine  # noqa: SLF001

    def observe(*args: Any, **kwargs: Any) -> Any:
        trace_table.flush()
        progress = service.get_job_trace(job_id, actor_user_uuid="user-1")
        assert progress is not None
        seen["states"] = states(progress.trace.events)
        return generate(*args, **kwargs)

    monkeypatch.setattr(service, "_generate_selected_engine", observe)
    run(service, job_id)
    assert seen["states"] == [
        "queue:done",
        "prepare_context:done",
        "generate_sql:running",
    ]


def test_generation_only_turn_skips_execution(
    jobs: Fixture,
    trace_table: TraceTable,  # noqa: F811
) -> None:
    service, _ = jobs
    job = run(service, start(service, chat_turn(generation_only=True)))
    assert job.status == JobStatus.DONE
    events = events_of(service, trace_table, job.job_id)
    assert states(events) == [
        "queue:done",
        "prepare_context:done",
        "generate_sql:done",
        "safety_check:done",
        "execute_sql:skipped",
        "format_results:done",
    ]
    execute = stages(events)["execute_sql"]
    assert execute.started_at is not None and execute.started_at == execute.finished_at


def test_blocked_sql_fails_safety_check_and_ends_failed(
    monkeypatch: pytest.MonkeyPatch,
    trace_table: TraceTable,  # noqa: F811
) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    service = _worker(
        _repository(),
        _FakeEnterpriseAiClient('{"sql":"DELETE FROM APP.ORDERS","explanation":"削除します。"}'),
    )
    job = run(service, start(service))
    assert job.status == JobStatus.ERROR
    events = events_of(service, trace_table, job.job_id)
    assert states(events) == [
        "queue:done",
        "prepare_context:done",
        "generate_sql:done",
        "safety_check:failed",
        "execute_sql:skipped",
        "format_results:done",
    ]
    assert fold_chat_progress_events(events).terminal == "failed"
    assert params(events, "safety_check") == {}


def test_failed_stage_fails_its_span_and_the_rest_do_not_start(
    jobs: Fixture,
    trace_table: TraceTable,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = jobs

    def failing_generate(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("想定外の失敗")

    monkeypatch.setattr(service, "_generate_selected_engine", failing_generate)
    job = run(service, start(service))
    assert job.status == JobStatus.ERROR
    events = events_of(service, trace_table, job.job_id)
    assert states(events) == ["queue:done", "prepare_context:done", "generate_sql:failed"]
    assert fold_chat_progress_events(events).terminal == "failed"
    # 例外の文は表にもイベントにも入れない（span の属性は例外の種類だけ）。
    assert "想定外" not in json.dumps([e.model_dump(mode="json") for e in events])
    assert "想定外" not in json.dumps(list(trace_table.db.rows.values()), default=str)


def test_cancelled_job_ends_cancelled(jobs: Fixture, trace_table: TraceTable) -> None:  # noqa: F811
    service, _ = jobs
    job_id = start(service)
    assert service.request_job_cancel(job_id, actor_user_uuid="user-1") is not None
    job = run(service, job_id)
    assert job.error_code == JOB_CANCELLED_ERROR_CODE
    events = events_of(service, trace_table, job_id)
    assert states(events) == ["queue:done", "prepare_context:skipped"]
    prepare = stages(events)["prepare_context"]
    assert prepare.span is not None and prepare.span["cancelled"] is True
    assert fold_chat_progress_events(events).terminal == "cancelled"
    assert root(events).status == "skipped"


def test_span_left_running_in_the_table_closes_with_the_job(
    jobs: Fixture,
    trace_table: TraceTable,  # noqa: F811
) -> None:
    """表で実行中のままの span（書けなかった終了・落ちた process）は、ジョブの終端で閉じる。"""

    service, _ = jobs
    job = run(service, start(service))
    trace_table.flush()
    [row] = [
        row
        for row in trace_table.db.spans(operation_id=job.job_id)
        if row["name"] == "nl2sql.format_results"
    ]
    row.update(status="running", finished_at=None)
    events = events_of(service, trace_table, job.job_id)
    assert stages(events)["format_results"].status == "done"


def test_reclaimed_job_shows_only_the_latest_attempt(
    jobs: Fixture,
    trace_table: TraceTable,  # noqa: F811
) -> None:
    """lease の切れたジョブを引き継いだ実行は、試行を増やして span の木を作り直す（#1358）。"""

    service, repository = jobs
    two_hours_ago = datetime.now(UTC) - timedelta(hours=2)
    _put_in_flight_snapshot(
        service, repository, job_id="job-orphan", updated_at=two_hours_ago.isoformat()
    )
    # 前の実行（最初の試行）が準備の途中まで表に書いていた。
    previous = TraceSpanOwner(product="nl2sql", operation_id="job-orphan", owner_user_uuid="user-1")
    for span_id, kind in (("a" * 16, None), ("c" * 16, "prepare_context")):
        trace_table.store.writer.submit(
            SpanUpdate(
                span_id=span_id,
                parent_span_id=None if kind is None else "a" * 16,
                trace_id="b" * 32,
                operation_name=f"nl2sql.{kind or 'execute'}",
                status="running",
                started_at=two_hours_ago,
                root=kind is None,
                display=SpanDisplay(kind=kind) if kind else None,
            ),
            previous,
        )

    assert service.run_next_nl2sql_job(worker_id="worker-retry") is True
    job = service.get_job("job-orphan")
    assert job is not None and job.status == JobStatus.DONE and job.attempt == 2
    trace_table.flush()
    assert {row["attempt"] for row in trace_table.db.spans(trace_id=job.trace_id)} == {1}
    events = events_of(service, trace_table, "job-orphan")
    assert "c" * 16 not in fold_chat_progress_events(events).steps
    assert states(events) == [f"{step}:done" for step in _STEPS]


def test_finished_job_without_trace_id_reads_as_legacy(jobs: Fixture) -> None:
    """span の記録より前の形式の実行（trace id が無い）は終端だけ（画面は古い形式の案内）。"""

    service, repository = jobs
    job_id = start(service)
    document = repository.get_document("jobs", job_id)
    assert document is not None
    document.update(status="done", started_at=document["created_at"], trace_id="")
    document["progress_events"] = [{"stage": "prepare_context", "status": "done"}]
    repository.put_document("jobs", job_id, document, status="done")
    progress = Nl2SqlService.get_job_trace(_worker(repository), job_id, actor_user_uuid="user-1")
    assert progress is not None
    events = progress.trace.events
    assert len(events) == 1 and isinstance(events[0], ChatProgressTerminalEvent)


def test_dropped_spans_are_saved_with_the_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """書き手の queue が満杯で捨てた span の数を、確定の保存に入れる。

    画面は一部だけの記録の案内を出す。
    """

    from trace_span_fake import FakeTraceSpanDb

    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    monkeypatch.setattr(get_settings(), "trace_store_queue_size", 1)
    db = FakeTraceSpanDb()
    store = install_trace_span_store(db.connect, start=False)
    try:
        repository = _repository()
        service = _worker(repository)
        job = run(service, start(service))
        assert job.trace_dropped_spans > 0
        [history] = repository.list_documents("history", limit=100)
        assert history["trace_dropped_spans"] == job.trace_dropped_spans
        store.writer.flush()
        progress = service.get_job_trace(job.job_id, actor_user_uuid="user-1")
        assert progress is not None and progress.trace.dropped_spans == job.trace_dropped_spans
    finally:
        reset_trace_span_store()


# --- 読み出し（API） ------------------------------------------------------------------------

_PERMISSIONS = {QUERY_GENERATE_PERMISSION, SQL_EXECUTE_PERMISSION}


class _Viewer:
    """要求の利用者（テストが差し替える）。"""

    def __init__(self) -> None:
        self.user_uuid = "user-1"
        self.profiles = {_PROFILE_ID}
        self.roles = ["GENERAL"]


@pytest.fixture
def api(jobs: Fixture, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, _Viewer]]:
    service, _ = jobs
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)
    viewer = _Viewer()
    app = FastAPI()

    @app.middleware("http")
    async def principal(request: Request, call_next: Any) -> Any:
        current = _principal(viewer.profiles, permissions=_PERMISSIONS)
        request.state.principal = replace(
            current, user_uuid=viewer.user_uuid, role_codes=list(viewer.roles)
        )
        return await call_next(request)

    app.include_router(nl2sql_router.router, prefix="/api")
    app.dependency_overrides[nl2sql_router._require_persistence] = lambda: None  # noqa: SLF001
    with TestClient(app) as client:
        yield client, viewer


def test_trace_route_returns_the_snapshot(
    jobs: Fixture,
    trace_table: TraceTable,  # noqa: F811
    api: tuple[TestClient, _Viewer],
) -> None:
    service, _ = jobs
    client, _ = api
    job = run(service, start(service))
    trace_table.flush()
    body = client.get(f"/api/nl2sql/jobs/{job.job_id}/trace").json()["data"]
    assert body["dropped_spans"] == 0
    assert body["events"][-1] == {**body["events"][-1], "type": "terminal", "status": "done"}
    assert {item["target_id"] for item in body["events"]} == {job.job_id}
    # system_admin 以外は、その実行の利用者の span だけを読む。
    assert trace_table.db.selects[-1]["viewer"] == "user-1"


def test_trace_route_checks_access_like_get_job(
    jobs: Fixture,
    trace_table: TraceTable,  # noqa: F811
    api: tuple[TestClient, _Viewer],
) -> None:
    service, _ = jobs
    client, viewer = api
    job = run(service, start(service))
    trace_table.flush()
    assert client.get("/api/nl2sql/jobs/missing/trace").status_code == 404
    # 他の利用者のジョブは 404（存在を漏らさない。#1889）。
    viewer.user_uuid = "other"
    assert client.get(f"/api/nl2sql/jobs/{job.job_id}/trace").status_code == 404
    # system_admin は全員のジョブと、全員の span を読む。
    viewer.roles = ["SYSTEM_ADMIN"]
    response = client.get(f"/api/nl2sql/jobs/{job.job_id}/trace")
    assert response.status_code == 200 and response.json()["data"]["events"]
    assert "viewer" not in trace_table.db.selects[-1]
    viewer.user_uuid, viewer.roles = "user-1", ["GENERAL"]
    # 業務プロファイルの利用権限が外れた。
    viewer.profiles = set()
    assert client.get(f"/api/nl2sql/jobs/{job.job_id}/trace").status_code == 403


def test_trace_route_reports_a_read_failure(
    jobs: Fixture,
    trace_table: TraceTable,  # noqa: F811
    api: tuple[TestClient, _Viewer],
) -> None:
    service, _ = jobs
    client, _ = api
    job = run(service, start(service))
    trace_table.db.fail_reads = True
    response = client.get(f"/api/nl2sql/jobs/{job.job_id}/trace")
    assert response.status_code == 503


@pytest.mark.parametrize("generation_only", [False, True], ids=["execute", "generation-only"])
def test_history_trace_reads_the_same_spans_as_the_job(
    jobs: Fixture,
    trace_table: TraceTable,  # noqa: F811
    api: tuple[TestClient, _Viewer],
    generation_only: bool,
) -> None:
    service, repository = jobs
    client, viewer = api
    request = _request().model_copy(update={"chat": False, "generation_only": generation_only})
    job = run(service, start(service, request))
    [history] = repository.list_documents("history", limit=100)
    job_events = events_of(service, trace_table, job.job_id)
    body = client.get(f"/api/nl2sql/history/{history['id']}/trace").json()["data"]
    assert {item["target_id"] for item in body["events"]} == {history["id"]}
    assert [item.get("step_id") for item in body["events"]] == [
        getattr(event, "step_id", None) for event in job_events
    ]
    execute = stages(job_events)["execute_sql"]
    assert execute.status == ("skipped" if generation_only else "done")
    # 一覧・詳細の応答には工程の詳細を入れない（処理の経過の API だけが返す）。
    detail = client.get(f"/api/nl2sql/history/{history['id']}").json()["data"]
    assert detail["span_details"] == {} and detail["trace_id"] == job.trace_id
    viewer.user_uuid = "other"
    assert client.get(f"/api/nl2sql/history/{history['id']}/trace").status_code == 404


@pytest.fixture(autouse=True)
def enable_trace_content_for_existing_detail_tests() -> Any:
    from pr_backend_core.observability.call_policy import call_content_policy

    with call_content_policy(lambda: True):
        yield


def test_body_off_preserves_business_result_and_progress_metadata(
    jobs: Fixture,
    trace_table: TraceTable,  # noqa: F811 - fixture
) -> None:
    from pr_backend_core.observability.call_policy import call_content_policy

    service, repository = jobs
    with call_content_policy(lambda: False):
        job = run(service, start(service))
    assert job.status == JobStatus.DONE and job.result and job.result.generated_sql
    trace_table.flush()
    events = events_of(service, trace_table, job.job_id)
    assert stages(events)
    assert all(not step.details or not step.details.code for step in stages(events).values())
    assert repository.get_document("jobs", job.job_id) is not None
