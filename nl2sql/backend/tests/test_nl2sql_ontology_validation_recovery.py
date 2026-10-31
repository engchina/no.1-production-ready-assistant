"""旧実データ検証の lease・遅着結果・レポート commit 復旧。"""

import re
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from test_nl2sql_ontology_definitions import runtime
from test_nl2sql_ontology_workspace import model

from app.features.nl2sql import ontology_definition_data_validation as validation
from app.features.nl2sql.models import QueryResults
from app.features.nl2sql.ontology_definition_workspace import ProfileOntologyWorkspaceService
from app.features.nl2sql.ontology_definitions import DefinitionDataValidationRequest
from app.features.nl2sql.ontology_worker import OntologyWorker
from app.settings import get_settings


@pytest.fixture(autouse=True)
def external(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    monkeypatch.setattr(get_settings(), "auth_mode", "local")


def setup_job() -> tuple[Any, Any, Any, dict[str, Any], list[str]]:
    rt, legacy = runtime()
    svc = ProfileOntologyWorkspaceService(rt)
    bundle = svc.save_build(
        profile_id="sales",
        job_id="build",
        definitions=model(),
        schema_fingerprint="schema",
        source_revision_id="legacy",
    )
    bundle = svc.validate("sales", bundle.id, bundle.etag, None)
    queries: list[str] = []

    def execute_select(sql: str, row_limit: int) -> QueryResults:
        queries.append(sql)
        names = re.findall(r'AS "([^"]+)"', sql)
        assert row_limit == 50
        return QueryResults(columns=names, rows=[{name: 1 for name in names}], total=1)

    cast(Any, legacy)._oracle_adapter = SimpleNamespace(execute_select=execute_select)
    job = validation.start_validation_job(
        rt,
        "sales",
        bundle.id,
        bundle.etag,
        DefinitionDataValidationRequest(confirmed=True),
        "request",
        None,
    )
    return rt, legacy, bundle, job, queries


def expire(rt: Any, job_id: str) -> None:
    current = rt.store.get_job(job_id)
    rt.store.save_job(
        {**current, "deadline_at": "2000-01-01T00:00:00+00:00"}, expected_etag=current["etag"]
    )


@pytest.mark.parametrize("status", ["queued", "running", "claimed"])
def test_legacy_expired_validation_recovers_without_sql(status: str) -> None:
    rt, _legacy, bundle, job, queries = setup_job()
    current = rt.store.get_job(job["job_id"])
    current.pop("deadline_at")
    current.update(status=status, created_at=(datetime.now(UTC) - timedelta(days=1)).isoformat())
    rt.store.save_job(current, expected_etag=current["etag"])
    result = validation.read_validation_job(rt, "sales", job["job_id"])
    assert result["status"] == "failed"
    assert result["error_code"] == "ONTOLOGY_VALIDATION_TIMEOUT"
    validation.run_validation_job(rt, job["job_id"])
    assert not queries
    assert ProfileOntologyWorkspaceService(rt).get("sales", bundle.id).etag == bundle.etag


@pytest.mark.parametrize("late_failure", [False, True])
def test_duplicate_and_expired_late_query_cannot_write_report(
    monkeypatch: pytest.MonkeyPatch,
    late_failure: bool,
) -> None:
    rt, legacy, bundle, job, queries = setup_job()
    entered, release = threading.Event(), threading.Event()
    original = legacy._oracle_adapter.execute_select

    def delayed(sql: str, limit: int) -> QueryResults:
        result = original(sql, limit)
        entered.set()
        assert release.wait(5)
        if late_failure:
            raise OSError("late query failure")
        return cast(QueryResults, result)

    monkeypatch.setattr(legacy._oracle_adapter, "execute_select", delayed)
    worker = threading.Thread(target=validation.run_validation_job, args=(rt, job["job_id"]))
    worker.start()
    try:
        assert entered.wait(5)
        validation.run_validation_job(rt, job["job_id"])
        assert len(queries) == 1
        expire(rt, job["job_id"])
        assert validation.read_validation_job(rt, "sales", job["job_id"])["status"] == "failed"
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    result = validation.read_validation_job(rt, "sales", job["job_id"])
    assert result["error_code"] == "ONTOLOGY_VALIDATION_TIMEOUT"
    assert "report" not in result
    assert rt.store.get_artifact(validation._receipt_id(job["job_id"])) is None
    assert ProfileOntologyWorkspaceService(rt).get("sales", bundle.id).etag == bundle.etag


def test_report_receipt_recovers_after_job_completion_write_is_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rt, _legacy, bundle, job, queries = setup_job()
    save = rt.store.save_job

    class Crash(BaseException):
        pass

    def fail_job(document: Any, **kwargs: Any) -> Any:
        if document["status"] == "succeeded":
            raise Crash()
        return save(document, **kwargs)

    monkeypatch.setattr(rt.store, "save_job", fail_job)
    with pytest.raises(Crash):
        validation.run_validation_job(rt, job["job_id"])
    committed = rt.store.get_artifact(bundle.id)
    assert committed is not None
    assert rt.store.get_artifact(validation._receipt_id(job["job_id"])) is not None
    monkeypatch.setattr(rt.store, "save_job", save)
    expire(rt, job["job_id"])
    recovered = validation.read_validation_job(rt, "sales", job["job_id"])
    assert recovered["status"] == "succeeded"
    assert recovered["recovered_from_receipt"] is True
    assert recovered["report"]["instance_count"] == 1
    assert recovered["report"]["entire_database_validated"] is False
    # 同じキーを元の etag で再送しても、既存完了結果を返し SQL を再実行しない。
    duplicate = validation.start_validation_job(
        rt,
        "sales",
        bundle.id,
        bundle.etag,
        DefinitionDataValidationRequest(confirmed=True),
        "request",
        None,
    )
    assert duplicate["status"] == "succeeded"
    validation.run_validation_job(rt, job["job_id"])
    assert len(queries) == 1
    assert rt.store.get_artifact(bundle.id) == committed


def test_commit_guard_stops_expired_report_and_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    rt, _legacy, bundle, job, _queries = setup_job()
    commit = ProfileOntologyWorkspaceService._commit

    def delayed(self: Any, *args: Any, **kwargs: Any) -> Any:
        expire(rt, job["job_id"])
        return commit(self, *args, **kwargs)

    monkeypatch.setattr(ProfileOntologyWorkspaceService, "_commit", delayed)
    validation.run_validation_job(rt, job["job_id"])
    assert validation.read_validation_job(rt, "sales", job["job_id"])["status"] == "failed"
    assert rt.store.get_artifact(validation._receipt_id(job["job_id"])) is None
    assert ProfileOntologyWorkspaceService(rt).get("sales", bundle.id).etag == bundle.etag


def test_external_worker_claim_keeps_single_validation_execution() -> None:
    rt, _legacy, _bundle, job, queries = setup_job()
    worker = OntologyWorker(rt, None, None)
    assert worker.process_one()
    assert not worker.process_one()
    assert validation.read_validation_job(rt, "sales", job["job_id"])["status"] == "succeeded"
    assert len(queries) == 1


def test_shutdown_only_interrupts_owned_validation_jobs() -> None:
    rt, _legacy, _bundle, job, queries = setup_job()
    validation.shutdown_validation_jobs(rt)
    assert validation.read_validation_job(rt, "sales", job["job_id"])["status"] == "queued"
    validation._inprocess_jobs.add((id(rt), job["job_id"]))
    try:
        validation.shutdown_validation_jobs(rt)
        assert (
            validation.read_validation_job(rt, "sales", job["job_id"])["error_code"]
            == "ONTOLOGY_VALIDATION_INTERRUPTED"
        )
        validation.run_validation_job(rt, job["job_id"])
        assert not queries
    finally:
        validation._inprocess_jobs.discard((id(rt), job["job_id"]))


def test_reclaimed_legacy_running_job_is_not_replayed() -> None:
    rt, _legacy, _bundle, job, queries = setup_job()
    current = rt.store.get_job(job["job_id"])
    rt.store.save_job(
        {**current, "status": "claimed", "claimed_from_status": "running"},
        expected_etag=current["etag"],
    )
    validation.run_validation_job(rt, job["job_id"])
    assert not queries
    expire(rt, job["job_id"])
    assert validation.read_validation_job(rt, "sales", job["job_id"])["status"] == "failed"


def test_report_commit_conflict_finishes_job_without_partial_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.features.nl2sql.ontology_store import OntologyVersionConflict

    rt, _legacy, bundle, job, _queries = setup_job()
    original = rt.store.save_documents_atomic

    def conflict(collection: Any, documents: Any) -> Any:
        if any(doc.get("artifact_type") == validation._RECEIPT_TYPE for doc, _etag in documents):
            raise OntologyVersionConflict("stale bundle", current_etag="fresh")
        return original(collection, documents)

    monkeypatch.setattr(rt.store, "save_documents_atomic", conflict)
    validation.run_validation_job(rt, job["job_id"])
    result = validation.read_validation_job(rt, "sales", job["job_id"])
    assert result["status"] == "failed"
    assert rt.store.get_artifact(validation._receipt_id(job["job_id"])) is None
    assert ProfileOntologyWorkspaceService(rt).get("sales", bundle.id).etag == bundle.etag
