"""SDK 直接経路と未観測の実装の診断を区別する。"""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from rag_engine.adapters import oci
from rag_engine.resources.rerank_diagnostics import bind_rerank_context, current_rerank_receipt
from rag_engine.resources.tracing import bind_trace_observer


def test_direct_sdk_retry_keeps_request_and_actual_attempts(monkeypatch):
    requests, recorded, private_requests, private_responses = [], [], [], []

    def rerank_text(request, **kwargs):
        requests.append(request)
        if len(requests) == 1:
            raise TimeoutError("private failure")
        return SimpleNamespace(data=SimpleNamespace(document_ranks=[SimpleNamespace(index=0, relevance_score=0.9)]))

    runtime = SimpleNamespace(
        models=SimpleNamespace(RerankTextDetails=SimpleNamespace),
        compartment_id="synthetic",
        client=SimpleNamespace(rerank_text=rerank_text),
        oci=SimpleNamespace(retry=SimpleNamespace(NoneRetryStrategy=lambda: None)),
    )
    monkeypatch.setattr(oci, "_runtime", lambda *args: runtime)
    monkeypatch.setattr(oci, "_serving_mode", lambda *args: "synthetic")
    monkeypatch.setattr(oci, "_is_retryable_error", lambda exc: True)
    monkeypatch.setattr(oci.time, "sleep", lambda seconds: None)
    settings = SimpleNamespace(
        rerank_model="model-a",
        oci_config_file="synthetic",
        oci_profile="synthetic",
        oci_compartment_id="synthetic",
        oci_rerank_endpoint="synthetic",
        rerank_request_timeout_seconds=10,
        rerank_retries=1,
        rerank_retry_initial_wait_seconds=0,
        rerank_retry_max_wait_seconds=0,
    )

    @contextmanager
    def observer(operation, model):
        yield SimpleNamespace(
            display=lambda **kwargs: recorded.append(kwargs),
            request=lambda value, **kwargs: private_requests.append(value),
            response=lambda value: private_responses.append(value),
        )

    with (
        bind_trace_observer(observer),
        bind_rerank_context(phase="crag_recovery", batch_id="batch-a", query_index=1, query_role="supplement") as receipt,
    ):
        ranks = oci._rerank_text_with_retry("Ａ  B", ["private document"], settings, top_n=1)
    assert private_requests == requests
    assert len(private_responses) == 1 and private_responses[0].document_ranks[0].index == 0
    assert ranks[0].index == 0
    data = receipt.to_dict()
    assert data["candidate_count"] == 1 and data["phase"] == "crag_recovery"
    assert data["sdk_attempt_count"] == 2 and data["http_attempt_count"] is None
    assert data["sdk_attempts"] == [
        {"attempt_number": 1, "retry_number": 0, "status": "error", "error_type": "TimeoutError"},
        {"attempt_number": 2, "retry_number": 1, "status": "completed"},
    ]
    assert data["status"] == "completed" and data["cache_state"] == "disabled"
    snapshots = [json.loads(row["details"]["code"][0]["text"]) for row in recorded]
    assert [row["status"] for row in snapshots] == ["error", "completed", "completed"]
    assert snapshots[0]["status_scope"] == snapshots[1]["status_scope"] == "sdk_attempt"
    assert requests[0].input == "Ａ  B"
    assert "private document" not in json.dumps(recorded) and "private failure" not in json.dumps(recorded)
    assert current_rerank_receipt() is None


def test_known_inputs_do_not_claim_custom_adapter_sdk_observation():
    with bind_rerank_context(phase="other", batch_id="a", query_index=0, query_role="original") as receipt:
        receipt.describe_inputs("question", ["document"], top_n=1)
        data = receipt.to_dict()
        assert data["candidate_count"] == 1 and data["candidate_hash"]
        assert data["sdk_attempt_count"] is None and data["http_attempt_count"] is None


def test_context_failure_does_not_leave_running_receipt():
    with pytest.raises(ValueError), bind_rerank_context(phase="other", batch_id="a", query_index=0, query_role="original") as receipt:
        raise ValueError("private failure")
    assert receipt.to_dict()["status"] == "error"
    assert receipt.to_dict()["error_type"] == "ValueError"
    assert current_rerank_receipt() is None
