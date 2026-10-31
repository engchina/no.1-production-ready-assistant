"""構築 worker の強制終了・heartbeat・遅着結果・明示 retry の回帰。"""

import json
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_nl2sql_ontology_build import _FakeEnterpriseAiClient
from test_nl2sql_ontology_definitions import runtime
from test_nl2sql_ontology_workspace import model

from app.features.nl2sql.ontology_build import OntologyBuildService
from app.features.nl2sql.ontology_models import OntologyBuildStatus
from app.settings import get_settings


@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    monkeypatch.setattr(get_settings(), "auth_mode", "local")
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_extraction_gleaning_passes", 0)


@pytest.mark.parametrize("mode", ["inprocess", "external"])
@pytest.mark.parametrize("condition", ["deadline", "heartbeat"])
def test_cold_reader_expires_orphan_and_releases_profile_lock(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    condition: str,
) -> None:
    rt, _ = runtime()
    svc = OntologyBuildService(rt)
    job = svc.start("sales", business_text="保存した入力")
    doc = rt.store.get_job(job.id)
    assert doc
    old = datetime.now(UTC) - timedelta(days=1)
    payload = {
        **doc["payload"],
        "status": "running",
        "started_at": old.isoformat(),
        "created_at": old.isoformat(),
        "events": [],
    }
    doc.update(
        status="running",
        payload=payload,
        heartbeat_at=old.isoformat(),
        deadline_at=(
            old if condition == "deadline" else datetime.now(UTC) + timedelta(hours=2)
        ).isoformat(),
    )
    if condition == "deadline":
        doc.pop("deadline_at")  # 旧形式にも適用する。
    rt.store.save_job(doc, expected_etag=doc["etag"])
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", mode)
    reader = OntologyBuildService(rt)
    expired = reader.get(job.id)
    assert expired and expired.status == OntologyBuildStatus.CANCELLED
    assert expired.error_code == (
        "ONTOLOGY_BUILD_TIMEOUT" if condition == "deadline" else "ONTOLOGY_BUILD_WORKER_LOST"
    )
    assert rt.store.get_idempotency("build_ontology_profile_active", "sales") is None
    assert reader.list_profile_jobs("sales")[0].status == OntologyBuildStatus.CANCELLED
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    retry = reader.retry(job.id)
    saved = rt.store.get_job(retry.id)
    assert saved and saved["input_payload"]["business_text"] == "保存した入力"
    assert saved["input_payload"]["checkpoint_job_ids"] == [job.id]


def test_new_start_recovers_orphan_without_prior_get() -> None:
    rt, _ = runtime()
    svc = OntologyBuildService(rt)
    job = svc.start("sales")
    record = rt.store.get_job(job.id)
    assert record
    rt.store.save_job(
        {**record, "deadline_at": "2000-01-01T00:00:00+00:00"}, expected_etag=record["etag"]
    )
    fresh = OntologyBuildService(rt).start("sales")
    assert fresh.id != job.id
    lock = rt.store.get_idempotency("build_ontology_profile_active", "sales")
    assert lock and lock["resource_id"] == fresh.id
    # 遅れた旧実行の解放でも、新しい job の lock を削除しない。
    svc._release_profile_job_lock("sales", job.id)
    assert rt.store.get_idempotency("build_ontology_profile_active", "sales") == lock


def test_long_running_build_with_fresh_heartbeat_stays_live() -> None:
    rt, _ = runtime()
    svc = OntologyBuildService(rt)
    job = svc.start("sales")
    token = svc._claim_execution(job.id)
    assert token
    record = rt.store.get_job(job.id)
    assert record
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    rt.store.save_job(
        {**record, "payload": {**record["payload"], "created_at": old, "started_at": old}},
        expected_etag=record["etag"],
    )
    assert OntologyBuildService(rt).get(job.id).status == OntologyBuildStatus.RUNNING  # type: ignore[union-attr]
    assert svc._heartbeat(job.id, "other-owner") is False
    assert svc._heartbeat(job.id, token) is True
    assert svc._claim_execution(job.id) is None
    assert OntologyBuildService(rt).get(job.id).status == OntologyBuildStatus.RUNNING  # type: ignore[union-attr]


def test_lock_delete_is_fenced_when_replaced_between_read_and_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rt, _ = runtime()
    svc = OntologyBuildService(rt)
    job = svc.start("sales")
    original = rt.store.delete_documents

    def delete(collection: Any, filters: Any) -> int:
        if collection == "idempotency":
            lock = rt.store.get_idempotency("build_ontology_profile_active", "sales")
            assert lock
            rt.store.save_idempotency(
                {**lock, "resource_id": "new-worker-job"}, expected_etag=lock["etag"]
            )
        return original(collection, filters)

    monkeypatch.setattr(rt.store, "delete_documents", delete)
    svc._delete_profile_job_lock("sales", job.id)
    lock = rt.store.get_idempotency("build_ontology_profile_active", "sales")
    assert lock and lock["resource_id"] == "new-worker-job"


def test_expired_worker_does_not_save_draft_and_retry_reuses_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rt, legacy = runtime()
    client = _FakeEnterpriseAiClient(
        json.dumps({"definitions": [d.model_dump(mode="json") for d in model()]})
    )
    legacy._enterprise_ai_client = client
    svc = OntologyBuildService(rt)
    job = svc.start("sales", business_text="受注を受注番号で識別する。")
    entered, release = threading.Event(), threading.Event()
    original_save = rt.create_build_markdown_draft

    def blocked_save(**kwargs: Any) -> Any:
        entered.set()
        assert release.wait(10)
        return original_save(**kwargs)

    monkeypatch.setattr(rt, "create_build_markdown_draft", blocked_save)
    worker = threading.Thread(target=svc.run_persisted, args=(job.id,))
    worker.start()
    try:
        assert entered.wait(10)
        record = rt.store.get_job(job.id)
        assert record
        rt.store.save_job(
            {**record, "heartbeat_at": "2000-01-01T00:00:00+00:00"}, expected_etag=record["etag"]
        )
        expired = OntologyBuildService(rt).get(job.id)
        assert expired and expired.error_code == "ONTOLOGY_BUILD_WORKER_LOST"
    finally:
        release.set()
        worker.join(10)
        assert not worker.is_alive()
    assert rt.ontology_markdown_state("sales").draft_revision is None
    before = len(client.calls)
    assert before > 0
    monkeypatch.setattr(rt, "create_build_markdown_draft", original_save)
    retry = svc.retry(job.id)
    result = svc.run_persisted(retry.id)
    assert result.status in {
        OntologyBuildStatus.SUCCEEDED,
        OntologyBuildStatus.SUCCEEDED_WITH_WARNINGS,
    }, result.error_message_ja
    assert len(client.calls) == before
    assert any(event.code == "BATCH_RESTORED" for event in result.events)
    assert rt.ontology_markdown_state("sales").draft_markdown


def test_reader_does_not_expose_terminal_cache_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rt, _ = runtime()
    svc = OntologyBuildService(rt)
    job = svc.start("sales")
    assert svc._claim_execution(job.id)
    svc._inprocess_jobs.add(job.id)
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "inprocess")
    svc._jobs[job.id].status = OntologyBuildStatus.FAILED
    current = svc.get(job.id)
    assert current and current.status == OntologyBuildStatus.RUNNING
    svc._persist_job(svc._jobs[job.id])
    committed = svc.get(job.id)
    assert committed and committed.status == OntologyBuildStatus.FAILED
