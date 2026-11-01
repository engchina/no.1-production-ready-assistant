"""論理要求・SDK開始・キャッシュを混同しない診断の回帰。"""

import asyncio
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest
from rag_engine.resources.rerank_diagnostics import bind_rerank_context, current_rerank_receipt

from app.clients.genai import GenAiClient
from app.config import Settings


class Sdk:
    """OpenAI 互換の API の fake（#8。HTTP は送らない）。"""

    def __init__(self) -> None:
        self.calls = 0
        self.fail_once = False

    def __call__(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        assert url.endswith("/reranks"), "embedding は呼び出さない"
        self.calls += 1
        if self.fail_once and self.calls == 1:
            raise TimeoutError("private error body")
        return {"results": [{"index": 0, "relevance_score": 0.9}]}


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []

    @contextmanager
    def observe(name: str, **kwargs: Any) -> Iterator[Any]:
        data = {"name": name, **kwargs}
        spans.append(data)
        yield SimpleNamespace(
            set_call_input=lambda *args, **kwargs: None,
            set_call_output=lambda *args, **kwargs: None,
            set_attributes=lambda values: data["attributes"].update(values),
            set_status=lambda value: data.update(status=value),
            set_display=lambda **values: data.update(values),
        )

    monkeypatch.setattr("app.clients.genai.observed_span", observe)

    async def no_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr("app.clients.ai_calls._sleep", no_sleep)
    return spans


def client(sdk: Any, *, enabled: bool = True, runner: Any = None) -> GenAiClient:
    async def inline(operation: Callable[[], Any]) -> Any:
        return operation()

    return GenAiClient(
        settings=Settings.model_construct(
            genai_rerank_model="synthetic-rerank",
            enterprise_ai_endpoint="https://synthetic.invalid/compatible-mode/v1",
            enterprise_ai_api_key="sk-synthetic",
            enterprise_ai_timeout_seconds=60,
            rag_rerank_cache_enabled=enabled,
        ),
        post=sdk,
        sdk_call_runner=runner or inline,
    )


def receipt(spans: list[dict[str, Any]], index: int = -1) -> dict[str, Any]:
    logical = [span for span in spans if span["name"] == "rerank.request"][index]
    return cast(dict[str, Any], json.loads(logical["details"].code[0].text))


async def test_direct_client_cache_hit_is_visible_without_sdk_attempt(
    captured: list[dict[str, Any]],
) -> None:
    sdk = Sdk()
    adapter = client(sdk)
    await adapter.rerank("Ａ  B", ["private document"], 1)
    await adapter.rerank("A B", ["private document"], 1)
    first, second = receipt(captured, 0), receipt(captured)
    assert sdk.calls == 1
    assert first["cache_state"] == "miss" and first["sdk_attempt_count"] == 1
    assert first["sdk_attempts"] == [
        {"attempt_number": 1, "retry_number": 0, "status": "completed"}
    ]
    assert second["cache_state"] == "hit" and second["sdk_attempt_count"] == 0
    assert first["normalized_query_hash"] == second["normalized_query_hash"]
    assert second["status"] == "completed" and second["http_attempt_count"] is None
    assert "private document" not in json.dumps(captured, default=str)
    assert current_rerank_receipt() is None


async def test_disabled_cache_and_ordered_candidate_identity(
    captured: list[dict[str, Any]],
) -> None:
    adapter = client(Sdk(), enabled=False)
    with bind_rerank_context(
        phase="guide_step", batch_id="batch-1", query_index=2, query_role="supplement"
    ) as context:
        await adapter.rerank("synthetic question", ["first", "second"], 1)
    await adapter.rerank("synthetic question", ["second", "first"], 1)
    first, second = receipt(captured, 0), receipt(captured)
    assert first["candidate_hash"] != second["candidate_hash"]
    assert first["question_index"] == 2 and first["query_role"] == "supplement"
    assert first["phase"] == "guide_step" and first["batch_id"] == "batch-1"
    assert first["cache_state"] == second["cache_state"] == "disabled"
    assert context.to_dict()["sdk_attempt_count"] == second["sdk_attempt_count"] == 1


async def test_actual_sdk_retry_records_bounded_attempt_number(
    captured: list[dict[str, Any]],
) -> None:
    sdk = Sdk()
    sdk.fail_once = True
    await client(sdk).rerank("question", ["document"], 1)
    data = receipt(captured)
    assert sdk.calls == data["sdk_attempt_count"] == 2
    assert data["sdk_attempts"] == [
        {"attempt_number": 1, "retry_number": 0, "status": "error", "error_type": "TimeoutError"},
        {"attempt_number": 2, "retry_number": 1, "status": "completed"},
    ]
    assert data["status"] == "completed" and "private error body" not in json.dumps(data)


async def test_cancelled_before_sdk_is_not_a_transport_attempt(
    captured: list[dict[str, Any]],
) -> None:
    async def cancelled(operation: Callable[[], Any]) -> Any:
        raise asyncio.CancelledError

    sdk = Sdk()
    with pytest.raises(asyncio.CancelledError):
        await client(sdk, runner=cancelled).rerank("question", ["document"], 1)
    assert sdk.calls == receipt(captured)["sdk_attempt_count"] == 0
    assert receipt(captured)["status"] == "cancelled"


async def test_context_isolation_and_unobserved_adapter_count(
    captured: list[dict[str, Any]],
) -> None:
    async def run(index: int) -> dict[str, Any]:
        with bind_rerank_context(
            phase="main_search", batch_id=str(index), query_index=index, query_role="original"
        ) as context:
            assert context.to_dict()["sdk_attempt_count"] is None
            await asyncio.sleep(0)
            await client(Sdk()).rerank("question", ["document"], 1)
            assert current_rerank_receipt() is context
            return context.to_dict()

    results = await asyncio.gather(run(0), run(1))
    assert [row["question_index"] for row in results] == [0, 1]
    assert all(row["sdk_attempt_count"] == 1 for row in results)
    assert current_rerank_receipt() is None


async def test_client_initialization_error_is_not_sdk_start(
    captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = client(Sdk())

    def fail_base_url(*, rerank: bool) -> str:
        raise ValueError("private configuration")

    monkeypatch.setattr(adapter, "_base_url", fail_base_url)
    with pytest.raises(ValueError):
        await adapter.rerank("question", ["document"], 1)
    data = receipt(captured)
    assert data["sdk_attempt_count"] == 0 and data["status"] == "error"
    assert data["error_type"] == "ValueError"
    assert "private configuration" not in json.dumps(data)


async def test_sdk_success_with_invalid_rank_is_logical_error(
    captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = monkeypatch

    def invalid_rank(url: str, body: dict[str, Any]) -> dict[str, Any]:
        return {"results": [{"index": 99, "relevance_score": 0.9}]}

    with pytest.raises(ValueError):
        await client(invalid_rank).rerank("question", ["document"], 1)
    data = receipt(captured)
    assert data["status"] == "error" and data["sdk_attempt_count"] == 1
    assert data["sdk_attempts"][0]["status"] == "completed"
    sdk_span = next(span for span in captured if span["name"] == "gen_ai.rerank")
    assert sdk_span["attributes"]["rerank.status"] == "completed"
    assert sdk_span["attributes"]["rerank.status_scope"] == "sdk_attempt"


async def test_sdk_cancellation_records_started_attempt(
    captured: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = monkeypatch

    def cancel(url: str, body: dict[str, Any]) -> dict[str, Any]:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await client(cancel).rerank("question", ["document"], 1)
    data = receipt(captured)
    assert data["status"] == "cancelled" and data["sdk_attempt_count"] == 1
    assert data["sdk_attempts"][0]["status"] == "cancelled"


def test_engine_sdk_observation_exposes_safe_receipt_in_existing_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.rag import pipeline

    displayed: dict[str, Any] = {}

    @contextmanager
    def observe(*args: Any, **kwargs: Any) -> Iterator[Any]:
        yield SimpleNamespace(
            call_content_enabled=True,
            display=None,
            set_display=lambda **values: displayed.update(values),
        )

    monkeypatch.setattr(pipeline, "observed_span", observe)
    with pipeline._observe_engine_call("rerank", "synthetic-model") as observation:
        observation.display(
            details={
                "code": [
                    {
                        "label": "rerank",
                        "language": "json",
                        "text": '{"phase":"main_search","sdk_attempt_count":1}',
                    }
                ]
            }
        )
    assert displayed["details"].code[0].label == "rerank"
    assert '"sdk_attempt_count":1' in displayed["details"].code[0].text
