from __future__ import annotations

import sys
import time
from types import SimpleNamespace
from typing import Any

import pytest

from app.features.nl2sql import ontology_worker as ontology_worker_module
from app.features.nl2sql.ontology_worker import OntologyWorker
from app.settings import get_settings


class _JobStore:
    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self.documents = {str(item["job_id"]): dict(item) for item in documents}

    def list_documents(self, _collection: str, filters: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            dict(document)
            for document in self.documents.values()
            if all(document.get(key) == value for key, value in filters.items())
        ]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        document = self.documents.get(job_id)
        return dict(document) if document is not None else None

    def save_job(
        self,
        document: dict[str, Any],
        *,
        expected_etag: str | None = None,
    ) -> dict[str, Any]:
        current = self.documents[str(document["job_id"])]
        if expected_etag != current["etag"]:
            raise RuntimeError("etag conflict")
        saved = {**document, "etag": f"{current['etag']}-next"}
        self.documents[str(document["job_id"])] = saved
        return dict(saved)


def _worker(documents: list[dict[str, Any]]) -> OntologyWorker:
    runtime = SimpleNamespace(store=_JobStore(documents))
    return OntologyWorker(runtime, SimpleNamespace(), SimpleNamespace())


def test_worker_reclaims_expired_claim(monkeypatch: Any) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_claim_timeout_seconds", 60.0)
    worker = _worker(
        [
            {
                "job_id": "stale",
                "job_type": "build",
                "status": "claimed",
                "claimed_at": time.time() - 61,
                "created_at": "2026-01-01T00:00:00Z",
                "etag": "v1",
            }
        ]
    )

    claimed = worker.claim_next()

    assert claimed is not None
    assert claimed["job_id"] == "stale"
    assert claimed["status"] == "claimed"
    assert claimed["claimed_by"] == worker.worker_id


def test_worker_does_not_steal_live_claim(monkeypatch: Any) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_claim_timeout_seconds", 60.0)
    worker = _worker(
        [
            {
                "job_id": "live",
                "job_type": "publish",
                "status": "claimed",
                "claimed_at": time.time(),
                "created_at": "2026-01-01T00:00:00Z",
                "etag": "v1",
            }
        ]
    )

    assert worker.claim_next() is None


def test_worker_reclaims_expired_in_flight_publish(monkeypatch: Any) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_claim_timeout_seconds", 60.0)
    worker = _worker(
        [
            {
                "job_id": "materializing",
                "job_type": "publish",
                "status": "materializing",
                "claimed_by": "stopped-worker",
                "claimed_at": time.time() - 61,
                "created_at": "2026-01-01T00:00:00Z",
                "etag": "v1",
            }
        ]
    )

    claimed = worker.claim_next()

    assert claimed is not None
    assert claimed["job_id"] == "materializing"
    assert claimed["claimed_by"] == worker.worker_id


def _queued_build() -> dict[str, Any]:
    return {
        "job_id": "build-1",
        "job_type": "build",
        "status": "queued",
        "created_at": "2026-01-01T00:00:00Z",
        "etag": "v1",
    }


def test_worker_releases_claim_when_job_fails_before_start() -> None:
    """実行側が状態を進める前に例外になった job を claim のまま残さない。"""

    worker = _worker([_queued_build()])

    def failing_run(_job_id: str) -> None:
        raise ConnectionError("ORA-03113 transient")

    worker.build_service = SimpleNamespace(run_persisted=failing_run)

    with pytest.raises(ConnectionError):
        worker.process_one()

    released = worker.runtime.store.documents["build-1"]
    assert released["status"] == "queued"
    assert released["claimed_by"] == ""
    assert released["claim_failures"] == 1
    # 次の poll で再び claim できる(claim 期限の約 65 分を待たない)
    assert worker.claim_next() is not None


def test_worker_keeps_claim_after_repeated_failures() -> None:
    """同じ job で失敗が続く場合は即時再 claim を止め、claim 期限切れの回収に任せる。"""

    worker = _worker([_queued_build()])

    def failing_run(_job_id: str) -> None:
        raise RuntimeError("poison")

    worker.build_service = SimpleNamespace(run_persisted=failing_run)
    for _ in range(ontology_worker_module._MAX_IMMEDIATE_RECLAIMS):
        with pytest.raises(RuntimeError):
            worker.process_one()

    kept = worker.runtime.store.documents["build-1"]
    assert kept["status"] == "claimed"
    assert kept["claimed_by"] == worker.worker_id
    assert worker.claim_next() is None


def test_worker_does_not_revert_job_already_advanced_by_runner() -> None:
    """実行側が running へ進めた後の例外は、各 job type の回復処理に任せる。"""

    worker = _worker([_queued_build()])
    store = worker.runtime.store

    def advancing_then_failing(job_id: str) -> None:
        current = store.documents[job_id]
        store.save_job({**current, "status": "running"}, expected_etag=current["etag"])
        raise RuntimeError("after start")

    worker.build_service = SimpleNamespace(run_persisted=advancing_then_failing)
    with pytest.raises(RuntimeError):
        worker.process_one()

    assert store.documents["build-1"]["status"] == "running"


def test_worker_main_loop_survives_iteration_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """1 回の process_one の例外で worker プロセスを終了させない。"""

    calls: list[str] = []

    class _StopLoop(BaseException):
        pass

    def process_one(_self: OntologyWorker) -> bool:
        calls.append("process")
        if len(calls) == 1:
            raise ConnectionError("ORA-03113 transient")
        raise _StopLoop

    monkeypatch.setattr(sys, "argv", ["ontology_worker"])
    monkeypatch.setattr(OntologyWorker, "process_one", process_one)
    monkeypatch.setattr("app.features.nl2sql.ontology_worker.time.sleep", lambda _seconds: None)

    with pytest.raises(_StopLoop):
        ontology_worker_module.main()

    assert calls == ["process", "process"]
