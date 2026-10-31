"""照合の実行状態だけを履歴へ保存し、旧verificationと権限境界を維持する。"""

from typing import Any

import pytest
from pytest import MonkeyPatch

from app.config import Settings
from app.rag.run_history import RunRecorder, answer_validation_execution
from tests.security_support import enable_production_auth, login
from tests.test_run_history import (
    OWNER_CONVERSATION_PREDICATE,
    OWNER_PREDICATE,
    RecordingStore,
    _capture,
    _row,
    client,
)


@pytest.mark.parametrize(
    ("validation", "expected"),
    [
        (None, None),
        ({}, "unknown"),
        ({"status": "disabled", "valid": True}, "disabled"),
        ({"status": "not_run", "reason": "audit_disabled", "valid": True}, "disabled"),
        ({"status": "not_run", "reason": "empty_answer", "valid": False}, "not_run"),
        ({"status": "not_run", "valid": True}, "not_run"),
        ({"status": "error", "valid": True}, "error"),
        ({"status": "timed_out", "valid": True}, "timed_out"),
        ({"status": "timeout", "valid": False}, "timed_out"),
        ({"status": "completed", "valid": True}, "passed"),
        ({"status": "completed", "valid": False}, "failed"),
        ({"status": "completed"}, "unknown"),
        ({"status": "completed", "valid": "true"}, "unknown"),
        ({"status": "ok", "valid": True}, "passed"),
        ({"status": "validated", "valid": False}, "failed"),
        ({"status": "partial", "valid": True}, "partial"),
        ({"status": "input_too_large", "valid": False}, "unknown"),
        ({"status": "no_evidence", "valid": False}, "unknown"),
        ({"status": "no_claims", "valid": True}, "not_run"),
        ({"status": "new_status", "valid": True}, "unknown"),
    ],
)
def test_validation_execution_uses_execution_before_validity(
    validation: Any, expected: str | None
) -> None:
    diagnostics = {"answer_validation": validation} if validation is not None else {}
    assert answer_validation_execution(diagnostics) == expected
    assert answer_validation_execution(None) is None


@pytest.mark.parametrize("status", ["not_run", "error", "timed_out", "partial", "completed"])
async def test_recorder_persists_scalar_inside_existing_stages(status: str) -> None:
    store = RecordingStore()
    recorder = RunRecorder(surface="chat", settings=Settings(), store=store)
    await recorder.record(
        run_id="synthetic-validation",
        question="対象Aの設定方法は？",
        search_answer_profile_id=None,
        answer_generated=True,
        status="success",
        elapsed_ms=1,
        answer_diagnostics={"answer_validation": {"status": status, "valid": False}},
    )
    assert store.records[0]["stages"]["validation_execution"] == (
        "failed" if status == "completed" else status
    )
    assert "validation_execution" not in {key for key in store.records[0] if key != "stages"}


@pytest.mark.parametrize(
    "execution",
    ["disabled", "not_run", "error", "timed_out", "passed", "failed", "partial", "unknown", None],
)
def test_list_returns_validation_execution_without_changing_legacy_bucket(
    monkeypatch: MonkeyPatch, execution: str | None
) -> None:
    auth = enable_production_auth(monkeypatch)
    auth.user_with_permissions("viewer", ["menu.history"])
    pool = _capture(
        monkeypatch,
        [
            [
                _row(
                    question="対象Aの設定方法は？",
                    search_answer_profile_name="設定A",
                    answer_generated=1,
                    verification="verified",
                    validation_execution=execution,
                )
            ],
            [{"count_value": 1}],
        ],
    )
    response = client.get("/api/history/runs", headers=login(client, "viewer"))
    assert response.status_code == 200, response.text
    row = response.json()["data"]["items"][0]
    assert row["validation_execution"] == execution
    assert row["verification"] == "verified"
    sql = pool.connection.calls[0].statement
    assert "JSON_VALUE(r.stages_json, '$.validation_execution'" in sql
    assert OWNER_PREDICATE in sql and OWNER_CONVERSATION_PREDICATE in sql
    assert "stages_json" not in row


@pytest.mark.parametrize("execution", ["error", None])
def test_detail_reads_stages_scalar_and_legacy_absence(
    monkeypatch: MonkeyPatch, execution: str | None
) -> None:
    auth = enable_production_auth(monkeypatch)
    auth.user_with_permissions("viewer", ["menu.history"])
    _capture(
        monkeypatch,
        [
            [
                _row(
                    question="対象Aの設定方法は？",
                    stages_json={"validation_execution": execution} if execution else {},
                    citations_json=[],
                )
            ],
            [],
        ],
    )
    response = client.get("/api/history/runs/run-1", headers=login(client, "viewer"))
    assert response.status_code == 200, response.text
    assert response.json()["data"]["validation_execution"] == execution


def test_detail_does_not_reveal_state_outside_existing_visibility_scope(
    monkeypatch: MonkeyPatch,
) -> None:
    auth = enable_production_auth(monkeypatch)
    auth.user_with_permissions("viewer", ["menu.history"])
    pool = _capture(monkeypatch, [[]])
    response = client.get("/api/history/runs/hidden-run", headers=login(client, "viewer"))
    assert response.status_code == 404
    assert "validation_execution" not in response.json()
    assert OWNER_PREDICATE in pool.connection.calls[0].statement


@pytest.mark.parametrize(
    ("stored", "validation", "expected"),
    [
        (None, {"status": "not_run", "reason": "audit_disabled", "valid": False}, "disabled"),
        (None, {"status": "completed", "valid": False}, "failed"),
        ("error", {"status": "completed", "valid": True}, "error"),
    ],
)
def test_detail_uses_saved_diagnostics_only_when_stages_status_is_absent(
    monkeypatch: MonkeyPatch, stored: str | None, validation: dict[str, Any], expected: str
) -> None:
    auth = enable_production_auth(monkeypatch)
    auth.user_with_permissions("viewer", ["menu.history"])
    _capture(
        monkeypatch,
        [
            [
                _row(
                    question="対象Aの設定方法は？",
                    answer_generated=1,
                    stages_json={"validation_execution": stored} if stored else {},
                    citations_json=[],
                )
            ],
            [
                {
                    "answer": "対象Aの設定を開きます。",
                    "citations_json": [],
                    "diagnostics_json": {"answer_validation": validation},
                }
            ],
            [],
        ],
    )
    response = client.get("/api/history/runs/run-1", headers=login(client, "viewer"))
    assert response.status_code == 200, response.text
    assert response.json()["data"]["validation_execution"] == expected
