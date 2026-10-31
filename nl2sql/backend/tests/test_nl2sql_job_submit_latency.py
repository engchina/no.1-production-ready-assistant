"""ジョブの投入（`POST /api/nl2sql/jobs` → `start_job`）が重い処理・共有の lock を待たないこと。

投入は job ID を返してすぐ戻り、オントロジーの公開版の確定（オントロジーの runtime の lock と
DB の読み取りを伴う）は worker の `prepare_context` の段階で行う。lock を持ったまま DB を読む
別の処理（オントロジーの同期・前のジョブの結果の整形など）が長くかかっても、投入は塞がれない。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import pytest
from test_nl2sql_job_runtime import _repository, _request, _worker

from app.features.nl2sql import ontology_router
from app.features.nl2sql.incremental_store import MemoryIncrementalNl2SqlRepository
from app.features.nl2sql.models import JobCreateRequest, JobStatus, QueryResults
from app.features.nl2sql.ontology_markdown_workspace import MarkdownOntologyWorkspace
from app.features.nl2sql.ontology_router import OntologyApiRuntime
from app.features.nl2sql.ontology_store import InMemoryOntologyStore
from app.features.nl2sql.service import _NL2SQL_JOB_STAGES, Nl2SqlService
from app.settings import get_settings

_SUBMIT_DEADLINE_SECONDS = 5.0


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> Nl2SqlService:
    # in-process の worker を起こさず、テストが worker を明示的に動かす。
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    return _worker(_repository())


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, service: Nl2SqlService) -> OntologyApiRuntime:
    """投入する service に結び付いたオントロジーの runtime（本番と同じ legacy_service is self）。"""

    bound = OntologyApiRuntime(legacy_service=service, store=InMemoryOntologyStore())
    monkeypatch.setattr(ontology_router, "ontology_runtime", bound)
    return bound


def _chat_request() -> JobCreateRequest:
    return _request().model_copy(update={"generation_only": True, "use_ontology_context": True})


def _hold_lock(lock: Any) -> tuple[threading.Event, threading.Event, threading.Thread]:
    """別のスレッドが lock を持ち続ける（DB I/O を lock の中で待っている処理の代わり）。"""

    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with lock:
            held.set()
            release.wait(30)

    holder = threading.Thread(target=hold, name="ontology-lock-holder", daemon=True)
    holder.start()
    assert held.wait(5)
    return held, release, holder


def _submit_in_thread(
    service: Nl2SqlService, request: JobCreateRequest
) -> tuple[threading.Event, dict[str, Any]]:
    done = threading.Event()
    outcome: dict[str, Any] = {}

    def submit() -> None:
        try:
            outcome["created"] = service.start_job(
                request, actor_user_uuid="user-1", actor_is_system_admin=False
            )
        except BaseException as exc:  # pragma: no cover - 失敗はテストの assert で出す
            outcome["error"] = exc
        finally:
            done.set()

    threading.Thread(target=submit, name="submit-job", daemon=True).start()
    return done, outcome


def test_submit_returns_while_ontology_lock_is_held(
    service: Nl2SqlService, runtime: OntologyApiRuntime
) -> None:
    """オントロジーの lock を別のスレッドが持っていても、投入は待たずに job ID を返す。"""

    _, release, holder = _hold_lock(runtime._lock)  # noqa: SLF001 - lock の競合の再現
    try:
        started = time.monotonic()
        done, outcome = _submit_in_thread(service, _chat_request())
        finished = done.wait(_SUBMIT_DEADLINE_SECONDS)
        elapsed = time.monotonic() - started
    finally:
        release.set()
        holder.join(5)
    assert done.wait(10), "lock を放した後も投入が戻らない"
    assert finished, f"投入がオントロジーの lock を {elapsed:.1f} 秒以上待った"
    assert "error" not in outcome, outcome.get("error")
    created = outcome["created"]
    assert created.status == JobStatus.PENDING


def test_submit_does_not_read_the_ontology_and_worker_pins_the_release(
    monkeypatch: pytest.MonkeyPatch,
    service: Nl2SqlService,
    runtime: OntologyApiRuntime,
) -> None:
    """公開版の確定は worker の prepare_context で行い、結果・履歴に同じ版を残す。"""

    calls: list[str] = []

    def snapshot_id(_self: MarkdownOntologyWorkspace, profile_id: str) -> str:
        calls.append(threading.current_thread().name)
        return ""

    monkeypatch.setattr(MarkdownOntologyWorkspace, "published_snapshot_id", snapshot_id)
    published: list[str] = []
    original_release = runtime.published_release_id

    def release_id(profile_id: str) -> str:
        published.append(profile_id)
        return original_release(profile_id)

    monkeypatch.setattr(runtime, "published_release_id", release_id)

    created = service.start_job(_chat_request(), actor_user_uuid="user-1")
    assert calls == [], "投入の要求の中でオントロジーの公開版を読んだ"
    assert published == []

    assert service.run_next_nl2sql_job(worker_id="latency-test", job_id=created.job_id)
    job = service.get_job(created.job_id, actor_user_uuid="user-1")
    assert job is not None
    assert job.status == JobStatus.DONE
    assert calls, "worker が公開版を確定していない"
    assert job.business_release_id == ""
    assert job.result is not None
    assert job.result.business_release_id == ""


def test_worker_uses_release_published_before_processing(
    monkeypatch: pytest.MonkeyPatch,
    service: Nl2SqlService,
    runtime: OntologyApiRuntime,
) -> None:
    """worker が確定した公開版を、生成の文脈・job・結果に使う。"""

    monkeypatch.setattr(
        MarkdownOntologyWorkspace,
        "published_snapshot_id",
        lambda _self, _profile_id: "ontology_markdown_snapshot_v1",
    )
    seen: list[str | None] = []

    def published_markdown(**kwargs: Any) -> str | None:
        seen.append(kwargs.get("business_release_id"))
        return None

    monkeypatch.setattr(service, "_job_published_ontology_markdown", published_markdown)

    created = service.start_job(_chat_request(), actor_user_uuid="user-1")
    assert service.run_next_nl2sql_job(worker_id="latency-test", job_id=created.job_id)
    job = service.get_job(created.job_id, actor_user_uuid="user-1")
    assert job is not None
    assert seen == ["ontology_markdown_snapshot_v1"]
    assert job.business_release_id == "ontology_markdown_snapshot_v1"
    assert job.result is not None
    assert job.result.business_release_id == "ontology_markdown_snapshot_v1"


def test_job_snapshot_keeps_unresolved_release_until_worker_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """投入の直後の snapshot は「未確定」を持ち、別の worker が読んでも確定前と分かる。"""

    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    repository: MemoryIncrementalNl2SqlRepository = _repository()
    service = _worker(repository)
    created = service.start_job(_chat_request(), actor_user_uuid="user-1")
    document = repository.get_document("jobs", created.job_id)
    assert document is not None
    assert document["business_release_pending"] is True
    restored = service._job_from_snapshot(document)  # noqa: SLF001
    assert restored.business_release_pending is True
    # 旧版の snapshot（印が無い）は確定済みとして扱う（投入時に確定していた）。
    document.pop("business_release_pending")
    assert service._job_from_snapshot(document).business_release_pending is False  # noqa: SLF001


def test_job_stage_logs_cover_every_stage(
    caplog: pytest.LogCaptureFixture, service: Nl2SqlService
) -> None:
    """投入の受付と、各段階の開始・終了（段階・所要時間・job_id）をログで追える。"""

    caplog.set_level(logging.INFO, logger="app.features.nl2sql.service")
    created = service.start_job(_request(), actor_user_uuid="user-1")
    assert service.run_next_nl2sql_job(worker_id="latency-test", job_id=created.job_id)

    def records(message: str) -> list[logging.LogRecord]:
        return [record for record in caplog.records if record.getMessage() == message]

    accepted = records("nl2sql_job_accepted")
    assert [getattr(record, "job_id", None) for record in accepted] == [created.job_id]
    assert getattr(accepted[0], "elapsed_ms", None) is not None
    started = records("nl2sql_job_stage_started")
    finished = records("nl2sql_job_stage_finished")
    assert [getattr(record, "stage", None) for record in started] == list(_NL2SQL_JOB_STAGES)
    assert [getattr(record, "stage", None) for record in finished] == list(_NL2SQL_JOB_STAGES)
    for record in [*started, *finished]:
        assert getattr(record, "job_id", None) == created.job_id
        assert getattr(record, "attempt", None) == 1
    for record in finished:
        assert isinstance(getattr(record, "elapsed_ms", None), int)
        assert getattr(record, "status", None) in {"done", "skipped", "error"}
    completed = records("nl2sql_job_finished")
    assert len(completed) == 1
    assert getattr(completed[0], "status", None) == "done"
    # SQL の本文や質問の本文はログに出さない。
    question = _request().question
    assert all(question not in record.getMessage() for record in caplog.records)
    assert all(
        question not in str(value)
        for record in [*accepted, *started, *finished, *completed]
        for value in record.__dict__.values()
    )


def test_ontology_history_reads_profile_outside_service_lock(
    monkeypatch: pytest.MonkeyPatch, service: Nl2SqlService
) -> None:
    """Query Session の履歴の記録は、業務プロファイルの読み取り（DB）を lock の外で行う。"""

    original_get_profile = service.get_profile
    held_during_read: list[bool] = []

    def get_profile(profile_id: str | None, **kwargs: Any) -> Any:
        held_during_read.append(bool(service._lock._is_owned()))  # type: ignore[attr-defined]  # noqa: SLF001
        return original_get_profile(profile_id, **kwargs)

    monkeypatch.setattr(service, "get_profile", get_profile)
    item = service.record_ontology_history(
        session_id="session-lock",
        question="注文一覧",
        rewritten_question="注文一覧",
        engine=_request().engine,
        generated_sql="SELECT ID FROM APP.ORDERS",
        executable_sql="SELECT ID FROM APP.ORDERS",
        profile_id=_request().profile_id or "default",
        result=QueryResults(columns=["ID"], rows=[], total=0),
        ontology_trace_summary={},
    )
    assert item.session_id == "session-lock"
    assert held_during_read == [False]


def test_chat_detail_does_not_reread_each_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """会話の取得（生成中は 1.5 秒ごと）は、ターンの数だけ job を読み直さない。"""

    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    repository = _repository()
    service = _worker(repository)
    chat = _request().model_copy(update={"generation_only": True})
    first = service.start_job(chat, actor_user_uuid="user-1")
    assert service.run_next_nl2sql_job(worker_id="w", job_id=first.job_id)
    previous = first.job_id
    for turn in range(3):
        created = service.start_job(
            chat.model_copy(update={"question": f"続き {turn}", "previous_job_id": previous}),
            actor_user_uuid="user-1",
        )
        assert service.run_next_nl2sql_job(worker_id="w", job_id=created.job_id)
        previous = created.job_id

    observer = _worker(repository)
    reads: list[str] = []
    original_get_document = repository.get_document

    def get_document(collection: str, entity_id: str) -> Any:
        reads.append(f"{collection}:{entity_id}")
        return original_get_document(collection, entity_id)

    monkeypatch.setattr(repository, "get_document", get_document)
    data = observer.get_sql_chat(first.job_id, actor="user-1")
    assert data is not None
    assert len(data.turns) == 4
    assert all(turn.status == JobStatus.DONE for turn in data.turns)
    assert [turn.steps[-1].status.value for turn in data.turns] == ["done"] * 4
    # 会話の root（所有者の確認）の 1 回だけ。
    assert reads == [f"jobs:{first.job_id}"]


def test_job_steps_carry_start_and_finish_times_and_engine(service: Nl2SqlService) -> None:
    """チャットの段階の表示（#1145）が使う、段階の開始・終了の時刻と生成方法を返す。"""

    request = _request().model_copy(update={"generation_only": True})
    created = service.start_job(request, actor_user_uuid="user-1")
    pending = service.get_job(created.job_id, actor_user_uuid="user-1")
    assert pending is not None
    assert pending.engine == request.engine
    assert all(step.started_at is None and step.finished_at is None for step in pending.steps)

    assert service.run_next_nl2sql_job(worker_id="latency-test", job_id=created.job_id)
    job = service.get_job(created.job_id, actor_user_uuid="user-1")
    assert job is not None
    assert job.status == JobStatus.DONE
    assert job.engine == request.engine
    by_stage = {step.stage: step for step in job.steps}
    for stage in ("prepare_context", "generate_sql", "safety_check"):
        step = by_stage[stage]
        assert step.status.value == "done"
        started, finished = step.started_at, step.finished_at
        assert started is not None and finished is not None
        assert started <= finished
    prepared_started = by_stage["prepare_context"].started_at
    prepared_finished = by_stage["prepare_context"].finished_at
    generated_started = by_stage["generate_sql"].started_at
    assert job.started_at is not None
    assert prepared_started is not None and prepared_finished is not None
    assert generated_started is not None
    assert prepared_started >= job.started_at
    assert generated_started >= prepared_finished


def test_failed_step_records_finish_time(
    monkeypatch: pytest.MonkeyPatch, service: Nl2SqlService
) -> None:
    """失敗した段階にも終了の時刻を入れる（チャットの段階の表示で所要時間を出す）。"""

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("生成に失敗")

    monkeypatch.setattr(service, "_generate_selected_engine", fail)
    created = service.start_job(_request(), actor_user_uuid="user-1")
    assert service.run_next_nl2sql_job(worker_id="latency-test", job_id=created.job_id)
    job = service.get_job(created.job_id, actor_user_uuid="user-1")
    assert job is not None
    assert job.status == JobStatus.ERROR
    failed = next(step for step in job.steps if step.status.value == "error")
    assert failed.stage == "generate_sql"
    assert failed.started_at is not None
    assert failed.finished_at == job.finished_at
