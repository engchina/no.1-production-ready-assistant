"""公開準備の孤児回復・並行 claim・遅着結果のフェンス。"""

import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from test_nl2sql_markdown_unification import prepared_workspace

from app.features.nl2sql.ontology_markdown_workspace import (
    PREPARATION,
    MarkdownOntologyWorkspace,
    shutdown_markdown_preparations,
)
from app.settings import get_settings


@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "auth_mode", "local")
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")


def queued() -> tuple[Any, MarkdownOntologyWorkspace, Any, dict[str, Any]]:
    rt, svc, parser, previous = prepared_workspace()
    value = svc.prepare("sales", previous["draft_etag"], "next", None)
    parser.calls = 0
    return rt, svc, parser, value


def test_running_job_is_persisted_and_provider_retry_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rt, svc, parser, value = queued()
    original = parser.generate

    def generate(**kwargs: Any) -> str:
        assert 0 < kwargs["timeout_seconds"] <= 600
        assert kwargs["max_retries"] == 0
        job = rt.store.get_job(value["id"])
        assert job and job["status"] == job["payload"]["status"] == "running"
        # 別 workspace に届いた再配送でも同じ LLM 呼び出しを実行しない。
        MarkdownOntologyWorkspace(rt).run_preparation("sales", value["id"])
        return str(original(**kwargs))

    monkeypatch.setattr(parser, "generate", generate)
    svc.run_preparation("sales", value["id"])
    assert parser.calls == 1
    assert svc.preparation("sales", value["id"])["status"] == "ready"
    job = rt.store.get_job(value["id"])
    assert job and job["status"] == "succeeded"


@pytest.mark.parametrize("status", ["queued", "running"])
def test_legacy_orphan_expires_and_repairs_queued_job(
    status: str, caplog: pytest.LogCaptureFixture
) -> None:
    rt, svc, parser, value = queued()
    value.pop("deadline_at")
    value.update(status=status, created_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat())
    svc._write("sales", value["id"], PREPARATION, value)
    with caplog.at_level(logging.WARNING):
        failed = svc.preparation("sales", value["id"])
    assert failed["status"] == "failed"
    assert failed["error_code"] == "PREPARATION_TIMEOUT"
    assert "再度公開前の確認" in failed["error_message_ja"]
    job = rt.store.get_job(value["id"])
    assert job and job["status"] == "failed" and job["payload"] == failed
    assert any(getattr(r, "job_id", None) == value["id"] for r in caplog.records)
    svc.run_preparation("sales", value["id"])
    assert parser.calls == 0


def test_terminal_artifact_repairs_job_after_partial_persistence() -> None:
    rt, svc, _, value = prepared_workspace()
    job = rt.store.get_job(value["id"])
    assert job
    rt.store.save_job({**job, "status": "queued", "payload": {}}, expected_etag=job["etag"])
    svc.run_preparation("sales", value["id"])
    repaired = rt.store.get_job(value["id"])
    assert repaired and repaired["status"] == "succeeded" and repaired["payload"] == value


@pytest.mark.parametrize("interrupt", ["shutdown", "timeout"])
def test_interrupted_worker_cannot_overwrite_failure_with_late_result(
    monkeypatch: pytest.MonkeyPatch,
    interrupt: str,
) -> None:
    rt, svc, parser, previous = prepared_workspace()
    started, release = threading.Event(), threading.Event()
    original = parser.generate
    threads: list[threading.Thread] = []
    real_start = threading.Thread.start

    def start(thread: threading.Thread) -> None:
        threads.append(thread)
        real_start(thread)

    def generate(**kwargs: Any) -> str:
        started.set()
        assert release.wait(5)
        return str(original(**kwargs))

    monkeypatch.setattr(parser, "generate", generate)
    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "inprocess")
    value = svc.prepare("sales", previous["draft_etag"], "blocked", None)
    try:
        assert started.wait(5)
        # 別 runtime の終了はこの処理を中断しない。
        shutdown_markdown_preparations(object())
        assert svc.preparation("sales", value["id"])["status"] == "running"
        if interrupt == "shutdown":
            shutdown_markdown_preparations(rt)
        else:
            # 時計だけを進め、worker の ETag を変更せず期限切れを再現。
            monkeypatch.setattr(
                MarkdownOntologyWorkspace, "_remaining", staticmethod(lambda _: -1.0)
            )
        failed = svc.preparation("sales", value["id"])
        assert failed["status"] == "failed"
        assert failed["error_code"] == (
            "PREPARATION_INTERRUPTED" if interrupt == "shutdown" else "PREPARATION_TIMEOUT"
        )
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
    assert svc.preparation("sales", value["id"])["status"] == "failed"
    record = rt.store.get_artifact(value["id"])
    job = rt.store.get_job(value["id"])
    assert record and job
    assert job["payload"] == json.loads(record["content"])
    assert job["status"] == "failed"


def test_provider_failure_is_actionable_without_logging_source_or_raw_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, svc, parser, value = queued()

    def generate(**kwargs: Any) -> str:
        raise RuntimeError("private-response-content")

    monkeypatch.setattr(parser, "generate", generate)
    with caplog.at_level(logging.INFO):
        svc.run_preparation("sales", value["id"])
    failed = svc.preparation("sales", value["id"])
    assert failed["status"] == "failed"
    assert "接続・応答状況" in failed["error_message_ja"]
    assert "private-response-content" not in caplog.text + failed["error_message_ja"]
    assert any(getattr(r, "error_type", None) == "RuntimeError" for r in caplog.records)
