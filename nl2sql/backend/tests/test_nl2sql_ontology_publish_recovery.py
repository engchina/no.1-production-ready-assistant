"""旧公開 worker の失効・遅着・部分 commit 復旧。実 Oracle/OCI は使用しない。"""

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_nl2sql_ontology_definitions import runtime

from app.features.nl2sql import ontology_reasoning as reasoning
from app.features.nl2sql.ontology_models import OntologyPublishJob, OntologyPublishStatus
from app.features.nl2sql.ontology_reasoning import OntologyPublishService
from app.settings import get_settings


@pytest.fixture(autouse=True)
def external(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_profile_confirmation_required", False)


def required_job(svc: OntologyPublishService, job_id: str) -> OntologyPublishJob:
    job = svc.get(job_id)
    assert job is not None
    return job


def setup_job() -> tuple[Any, OntologyPublishService, Any]:
    rt, _ = runtime()
    base = rt.current_ontology().revision
    draft, _ = rt.create_build_markdown_draft(
        profile_id="sales",
        base_revision_id=base.id,
        payloads=[],
        titles=[],
        markdown="# 保存済みの下書き",
        note="recovery",
    )
    svc = OntologyPublishService(rt)
    job = svc.start(draft.revision.id, etag=draft.revision.etag, idempotency_key="publish")
    return rt, svc, job


def expire(rt: Any, job_id: str) -> None:
    doc = rt.store.get_job(job_id)
    rt.store.save_job(
        {**doc, "deadline_at": "2000-01-01T00:00:00+00:00"}, expected_etag=doc["etag"]
    )


@pytest.mark.parametrize("status", ["queued", "materializing", "validating"])
def test_cold_reader_recovers_legacy_expired_job(status: str) -> None:
    rt, svc, job = setup_job()
    doc = rt.store.get_job(job.id)
    doc.pop("deadline_at")
    doc["status"] = status
    doc["payload"].update(
        status=status, created_at=(datetime.now(UTC) - timedelta(days=1)).isoformat()
    )
    rt.store.save_job(doc, expected_etag=doc["etag"])
    result = required_job(OntologyPublishService(rt), job.id)
    assert result and result.status == OntologyPublishStatus.FAILED
    assert result.error_code == "ONTOLOGY_PUBLISH_TIMEOUT"
    assert rt.ontology_revision(job.revision_id).revision.status.value == "draft"
    # 同じキーの再送で推論を再実行しない。
    assert (
        svc.start(job.revision_id, etag=job.requested_etag, idempotency_key="publish").id == job.id
    )
    assert svc.start(job.revision_id, etag=job.requested_etag, idempotency_key="retry").id != job.id


@pytest.mark.parametrize("status", ["succeeded", "failed"])
def test_store_terminal_state_wins_over_stale_cache(status: str) -> None:
    rt, svc, job = setup_job()
    svc._jobs[job.id] = job.model_copy(update={"status": OntologyPublishStatus.MATERIALIZING})
    doc = rt.store.get_job(job.id)
    doc["status"] = doc["payload"]["status"] = status
    rt.store.save_job(doc, expected_etag=doc["etag"])
    assert required_job(svc, job.id).status.value == status


def test_two_services_cannot_claim_same_publish() -> None:
    rt, svc, job = setup_job()
    assert svc._claim(job.id)
    second = OntologyPublishService(rt)
    assert not second._claim(job.id)
    assert second.run(job.id, etag=job.requested_etag).status.value == "materializing"


@pytest.mark.parametrize("stage", ["materialize", "validate", "publish"])
def test_expired_late_worker_cannot_publish_or_save_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    rt, svc, job = setup_job()
    reached, release = threading.Event(), threading.Event()
    target: Any
    if stage == "materialize":
        target, name = svc._materializer, "materialize"
    elif stage == "validate":
        target, name = reasoning, "validate_shacl_core"
    else:
        target, name = rt, "materialize_profile_views_for_revision"
    original = getattr(target, name)

    def blocked(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        reached.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(target, name, blocked)
    worker = threading.Thread(target=svc._run_safely, args=(job.id, job.requested_etag))
    worker.start()
    try:
        assert reached.wait(5)
        expire(rt, job.id)
        assert required_job(OntologyPublishService(rt), job.id).status.value == "failed"
        artifact_count = len(rt.store.list_artifacts(job.revision_id))
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert required_job(svc, job.id).error_code == "ONTOLOGY_PUBLISH_TIMEOUT"
    assert rt.ontology_revision(job.revision_id).revision.status.value == "draft"
    assert len(rt.store.list_artifacts(job.revision_id)) == artifact_count
    assert not [
        a
        for a in rt.store.list_artifacts(job.revision_id)
        if a.get("artifact_type") == "ontology_markdown_published"
    ]


def test_shutdown_interrupts_owned_worker_only(monkeypatch: pytest.MonkeyPatch) -> None:
    rt, svc, job = setup_job()
    second = OntologyPublishService(rt)
    second.shutdown()
    assert required_job(svc, job.id).status.value == "queued"
    svc._inprocess_jobs.add(job.id)
    svc.shutdown()
    assert required_job(svc, job.id).error_code == "ONTOLOGY_PUBLISH_INTERRUPTED"
    assert not svc._claim(job.id)


def test_restart_after_publication_commit_restores_copy_without_republishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rt, svc, job = setup_job()
    finalize = rt.finalize_semantic_publish
    publications = 0

    class Crash(BaseException):
        pass

    def crash_after_commit(*args: Any, **kwargs: Any) -> Any:
        nonlocal publications
        publications += 1
        finalize(*args, **kwargs)
        raise Crash()

    monkeypatch.setattr(rt, "finalize_semantic_publish", crash_after_commit)
    with pytest.raises(Crash):
        svc.run(job.id, etag=job.requested_etag)
    before = rt.store.get_revision(job.revision_id)
    assert before["payload"]["publish_job_id"] == job.id
    assert not [
        a
        for a in rt.store.list_artifacts(job.revision_id)
        if a.get("artifact_type") == "ontology_markdown_published"
    ]
    expire(rt, job.id)
    reader = OntologyPublishService(rt)
    result = required_job(reader, job.id)
    assert result.status.value == "succeeded"
    assert "再公開はしていません" in result.warnings_ja[-1]
    assert result.shacl_conforms is True
    assert (
        rt.published_markdown_for_revision(job.revision_id, profile_id="sales")
        == "# 保存済みの下書き"
    )
    assert publications == 1
    assert rt.store.get_revision(job.revision_id) == before
    artifacts = rt.store.list_artifacts(job.revision_id)
    assert reader.run_persisted(job.id).status.value == "succeeded"
    assert rt.store.list_artifacts(job.revision_id) == artifacts
    assert publications == 1


def test_duplicate_dispatch_does_not_remove_live_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    rt, svc, job = setup_job()
    reached, release = threading.Event(), threading.Event()
    original = svc._materializer.materialize
    calls = 0

    def blocked(**kwargs: Any) -> str:
        nonlocal calls
        calls += 1
        reached.set()
        assert release.wait(5)
        return original(**kwargs)

    monkeypatch.setattr(svc._materializer, "materialize", blocked)
    thread = threading.Thread(target=svc._run_safely, args=(job.id, job.requested_etag))
    thread.start()
    try:
        assert reached.wait(5)
        token = svc._execution_ids[job.id]
        svc._run_safely(job.id, job.requested_etag)
        assert svc._execution_ids[job.id] == token
    finally:
        release.set()
        thread.join(5)
    assert calls == 1
    assert required_job(svc, job.id).status.value == "succeeded"


def test_copy_failure_reports_committed_revision_and_can_reconcile_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rt, svc, job = setup_job()
    copy = rt.copy_draft_markdown_to_published

    def unavailable(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("temporary artifact storage failure")

    monkeypatch.setattr(rt, "copy_draft_markdown_to_published", unavailable)
    failed = svc.run_persisted(job.id)
    assert failed.error_code == "ONTOLOGY_PUBLISH_RECOVERY_REQUIRED"
    assert "版の公開は完了" in failed.error_message_ja
    revision = rt.store.get_revision(job.revision_id)
    assert revision["payload"]["status"] == "published"
    monkeypatch.setattr(rt, "copy_draft_markdown_to_published", copy)
    assert required_job(OntologyPublishService(rt), job.id).status.value == "succeeded"
    assert rt.store.get_revision(job.revision_id) == revision
    assert (
        rt.published_markdown_for_revision(job.revision_id, profile_id="sales")
        == "# 保存済みの下書き"
    )
