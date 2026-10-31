"""取込の AI の呼び出しを上限つきの並行の実行器に当てる（#2002）。

待ちは差し替えて実時間で待たない。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pr_backend_core.bounded_calls import BoundedCallAborted, ErrorKind

from app.clients import ai_calls
from app.clients.ai_calls import (
    ai_call_policy,
    ingestion_ai_calls,
    make_ai_caller,
    rag_error_kind,
)
from app.clients.oci_genai import OciEmbeddingRequestError, OciGenAiClient
from app.config import Settings
from app.rag.ingestion_progress import IngestionProgressReporter, track_ingestion_progress
from app.rag.navigation import summarize_navigation_nodes
from app.schemas.extraction import DocumentElement, DocumentNavigationNode, StructuredExtraction


class HttpError(Exception):
    """httpx / openai と同じく ``response.status_code`` / ``headers`` を持つ失敗のスタブ。"""

    def __init__(self, status: int, body: str = "", headers: dict[str, str] | None = None) -> None:
        super().__init__(f"returned {status}: {body}")
        self.response = SimpleNamespace(status_code=status, headers=headers or {})


@pytest.fixture(autouse=True)
def no_real_wait(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)
        await asyncio.sleep(0)

    monkeypatch.setattr(ai_calls, "_sleep", fake_sleep)
    monkeypatch.setattr(ai_calls, "_random", lambda: 0.5)
    return waits


def _settings(**values: Any) -> Settings:
    base: dict[str, Any] = {
        "oci_genai_embedding_dim": 3,
        "oci_genai_embedding_model": "cohere.embed-v4.0",
        "rag_embedding_cache_enabled": False,
        "rag_ai_call_final_retry_delay_seconds": 0.0,
    }
    base.update(values)
    return Settings.model_construct(**base)


# --- 設定・分類 -----------------------------------------------------------------------------


def test_policy_uses_stage_concurrency_and_common_settings() -> None:
    settings = Settings.model_construct(
        rag_vision_concurrency=6,
        rag_navigation_summary_concurrency=3,
        rag_embedding_concurrency=2,
        rag_ai_call_backoff_max_seconds=20.0,
        rag_ai_call_final_retry_delay_seconds=5.0,
        rag_ai_call_fail_job_on_item_error=True,
    )
    assert ai_call_policy(settings, "vision").max_concurrency == 6
    assert ai_call_policy(settings, "navigation_summary").max_concurrency == 3
    assert ai_call_policy(settings, "embedding").max_concurrency == 2
    # 文書ごとに 1 回の呼び出し（項目抽出）とリランクは並べない。
    assert ai_call_policy(settings, "field_extraction").max_concurrency == 1
    policy = ai_call_policy(settings, "vision")
    assert policy.backoff_max_seconds == 20.0
    assert policy.final_retry_delay_seconds == 5.0
    assert policy.fail_job_on_item_error is True


def test_settings_defaults_for_ingestion_concurrency() -> None:
    fields = Settings.model_fields
    assert fields["rag_vision_concurrency"].default == 4
    assert fields["rag_navigation_summary_concurrency"].default == 4
    assert fields["rag_embedding_concurrency"].default == 2
    assert fields["rag_ai_call_fail_job_on_item_error"].default is False


def test_rag_error_kind_maps_client_errors() -> None:
    from app.clients.oci_genai import OciGenAiConfigError

    assert rag_error_kind(OciGenAiConfigError("bad")) is ErrorKind.AUTHENTICATION
    assert (
        rag_error_kind(ValueError("OCI Enterprise AI LLM response に回答 text がありません。"))
        is ErrorKind.INVALID_OUTPUT
    )
    assert (
        rag_error_kind(ValueError("embedding の件数が一致しません。")) is ErrorKind.INVALID_OUTPUT
    )
    assert rag_error_kind(RuntimeError("other")) is None


def test_enterprise_ai_errors_are_classified_by_the_shared_rules() -> None:
    from rag_parser_core.oci_enterprise_ai import (
        EnterpriseAiIncompleteResponseError,
        EnterpriseAiTimeoutError,
        EnterpriseAiUnsupportedInputError,
        EnterpriseAiValidationError,
    )

    from app.rag.vision import VisionResponseError

    caller = make_ai_caller(_settings(), "vision")
    classify = caller._classify
    assert classify(EnterpriseAiTimeoutError("x", 1)).kind is ErrorKind.TIMEOUT
    assert classify(EnterpriseAiValidationError("x")).kind is ErrorKind.INVALID_OUTPUT
    assert classify(VisionResponseError("x")).kind is ErrorKind.INVALID_OUTPUT
    assert classify(EnterpriseAiIncompleteResponseError("x")).kind is ErrorKind.TRUNCATED
    assert classify(EnterpriseAiUnsupportedInputError("x")).kind is ErrorKind.NOT_SUPPORTED


def test_sdk_retries_disabled_only_inside_the_context() -> None:
    from rag_parser_core.oci_enterprise_ai import (
        OciEnterpriseAiConfig,
        sdk_max_retries,
        sdk_retries_disabled,
    )

    config = OciEnterpriseAiConfig(oci_enterprise_ai_max_retries=3)
    assert sdk_max_retries(config) == 3
    with sdk_retries_disabled():
        assert sdk_max_retries(config) == 0
    assert sdk_max_retries(config) == 3


# --- 進捗・heartbeat ---------------------------------------------------------------------


async def test_waiting_is_written_to_job_progress_as_heartbeat(no_real_wait: list[float]) -> None:
    written: list[object] = []

    async def writer(progress: object) -> None:
        written.append(progress)

    calls = 0

    async def flaky(_: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise HttpError(429, "slow down", {"retry-after": "25"})
        return "ok"

    snapshots: list[Any] = []
    async with track_ingestion_progress(writer, interval_seconds=60) as progress:
        progress.update_vision(_vision())
        with ingestion_ai_calls(_settings(), "vision") as caller:
            caller.add_listener(lambda event: snapshots.append(progress.snapshot()))
            assert await caller.call(flaky) == "ok"
        final = progress.snapshot()

    waiting = [
        snap.calls
        for snap in snapshots
        if snap is not None and snap.calls is not None and snap.calls.waiting_reason
    ]
    assert waiting and waiting[0].waiting_reason == "rate_limited"
    assert waiting[0].waiting_seconds == 26  # Retry-After 25 秒 + jitter 0.5 秒（切り上げ）
    assert final is not None and final.calls is not None
    assert final.calls.finished is True
    assert final.calls.retries == 1
    assert final.calls.waiting_reason is None
    # 待ちは heartbeat の間隔（既定 10 秒）ごとに区切って待つ。
    assert len(no_real_wait) >= 3


def _vision() -> Any:
    from app.schemas.document import IngestionVisionProgress

    return IngestionVisionProgress(phase="pictures")


def test_reporter_update_calls_changes_updated_at_only_when_state_changes() -> None:
    reporter = IngestionProgressReporter(lambda progress: asyncio.sleep(0))
    reporter.update_vision(_vision())
    caller = make_ai_caller(_settings(), "embedding")
    reporter.update_calls("embedding", caller.status(), total=3)
    first = reporter.snapshot()
    reporter.update_calls("embedding", caller.status(), total=3)
    assert reporter.snapshot() == first
    assert first is not None and first.calls is not None
    assert first.calls.max_concurrency == 2 and first.calls.total == 3


# --- Embedding ------------------------------------------------------------------------------


class _EmbeddingClient(OciGenAiClient):
    def __init__(self, settings: Settings, plan: dict[int, list[BaseException]] | None = None):
        super().__init__(settings=settings)
        self.plan = plan or {}
        self.calls: list[list[str]] = []
        self.active = 0
        self.max_active = 0

    async def _embed_with_oci(self, texts: list[str], *, input_type: str) -> list[list[float]]:
        _ = input_type
        self.calls.append(list(texts))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
            index = int(texts[0].split("-")[0])
            failures = self.plan.get(index)
            if failures:
                raise failures.pop(0)
            return [[float(int(text.split("-")[1])), 0.0, 1.0] for text in texts]
        finally:
            self.active -= 1


def _texts(batches: int, size: int) -> list[str]:
    return [f"{batch}-{batch * size + item}" for batch in range(batches) for item in range(size)]


async def test_embedding_batches_run_concurrently_and_keep_input_order() -> None:
    settings = _settings(rag_embedding_batch_size=2, rag_embedding_concurrency=3)
    client = _EmbeddingClient(settings)
    texts = _texts(6, 2)
    vectors = await client.embed(texts)
    assert [vector[0] for vector in vectors] == [float(i) for i in range(12)]
    assert len(client.calls) == 6
    assert client.max_active == 3


async def test_embedding_rate_limit_and_unavailable_are_retried() -> None:
    settings = _settings(rag_embedding_batch_size=2, rag_embedding_concurrency=2)
    client = _EmbeddingClient(
        settings,
        {1: [HttpError(429, "", {"retry-after": "3"})], 2: [HttpError(503)]},
    )
    vectors = await client.embed(_texts(3, 2))
    assert len(vectors) == 6
    # 429 は同じ要求を送り直し、一時的な不可用(503)のあとはバッチを分けて送る(#2467)。
    assert sorted(len(call) for call in client.calls) == [1, 1, 2, 2, 2, 2]


async def test_embedding_permission_error_stops_without_retry() -> None:
    settings = _settings(rag_embedding_batch_size=1, rag_embedding_concurrency=1)
    client = _EmbeddingClient(settings, {index: [HttpError(403)] for index in range(10)})
    with pytest.raises(BoundedCallAborted, match="IAM policy"):
        await client.embed(_texts(10, 1))
    assert len(client.calls) == 1


async def test_embedding_item_failure_raises_original_error_after_final_retry() -> None:
    settings = _settings(rag_embedding_batch_size=2, rag_embedding_concurrency=1)
    client = _EmbeddingClient(settings, {0: [ValueError("bad request body")] * 2})
    with pytest.raises(ValueError, match="bad request body"):
        await client.embed(_texts(2, 2))


class ServiceError(Exception):
    """OCI SDK の ``ServiceError`` と同じ属性(status / code / message / request_id)のスタブ。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{{'status': {status}, 'message': '{message}'}}")
        self.status = status
        self.code = str(status)
        self.message = message
        self.request_id = "opc-request-id-example"


# OCI Generative AI の Embedding が実際に返した HTTP 400 の文(#2467。合成データで確認)。
_OVERLOADED = "the requested model is currently experiencing high load, please try again later"
_TOO_LONG = "invalid request: inputs[0] is too long. Max tokens per text must be at most 128000"
_EMPTY = "Input text cannot be empty."


def test_rag_error_kind_maps_oci_embedding_rejections() -> None:
    # 高負荷は 400 で返るが一時的なため再試行する。1 件の入力の超過は同じ要求をくり返さない。
    assert rag_error_kind(ServiceError(400, _OVERLOADED)) is ErrorKind.UNAVAILABLE
    assert rag_error_kind(ServiceError(400, _TOO_LONG)) is ErrorKind.TOO_LARGE
    assert rag_error_kind(ServiceError(400, _EMPTY)) is None
    assert rag_error_kind(ServiceError(400, "other")) is None
    assert rag_error_kind(ServiceError(500, _OVERLOADED)) is None


async def test_embedding_overload_400_is_retried_with_split_batches() -> None:
    settings = _settings(rag_embedding_batch_size=4, rag_embedding_concurrency=1)
    client = _EmbeddingClient(settings, {0: [ServiceError(400, _OVERLOADED)]})
    vectors = await client.embed(_texts(1, 4))
    assert [vector[0] for vector in vectors] == [0.0, 1.0, 2.0, 3.0]
    # 失敗した 4 件の要求を、再試行では 2 件ずつの 2 回に分けて送る。
    assert [len(call) for call in client.calls] == [4, 2, 2]


async def test_embedding_rejection_reports_safe_diagnostics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(rag_embedding_batch_size=2, rag_embedding_concurrency=1)
    client = _EmbeddingClient(settings, {1: [ServiceError(400, _OVERLOADED)] * 20})
    texts = _texts(2, 2)
    with caplog.at_level("WARNING"), pytest.raises(OciEmbeddingRequestError) as raised:
        await client.embed(texts)
    error = raised.value
    assert error.safe_for_user is True
    assert str(error).startswith(
        "Embedding の要求が失敗しました（HTTP 400・サービスの高負荷）。バッチ 2/2（2 件・6 文字）。"
    )
    assert error.diagnostics == {
        "reason": "service_overloaded",
        "kind": "unavailable",
        "status": 400,
        "service_code": "400",
        "opc_request_id": "opc-request-id-example",
        "attempts": 1,
        "batch_index": 2,
        "batch_total": 2,
        "batch_inputs": 2,
        "batch_chars": 6,
        "batch_max_input_chars": 3,
        "batch_token_upper_bound": 6,
        "total_inputs": 4,
        "model": "cohere.embed-v4.0",
    }
    records = [record for record in caplog.records if record.message == "embedding_request_failed"]
    assert len(records) == 1 and records[0].__dict__["batch_index"] == 2
    # 入力の本文とサービスの応答の文は、例外の文・ログに残さない。
    logged = " ".join(f"{record.getMessage()} {record.__dict__}" for record in records)
    assert all(text not in logged and text not in str(error) for text in texts)
    assert "high load" not in logged and "high load" not in str(error)


async def test_embedding_too_long_input_is_not_retried_and_names_the_limit() -> None:
    settings = _settings(rag_embedding_batch_size=2, rag_embedding_concurrency=1)
    client = _EmbeddingClient(settings, {0: [ServiceError(400, _TOO_LONG)] * 5})
    with pytest.raises(OciEmbeddingRequestError, match="上限（128,000 トークン）") as raised:
        await client.embed(_texts(1, 2))
    assert raised.value.diagnostics["reason"] == "input_too_long"
    assert len(client.calls) == 1


async def test_embedding_rejects_empty_input_before_calling_the_service() -> None:
    client = _EmbeddingClient(_settings())
    with pytest.raises(OciEmbeddingRequestError, match="空の Chunk が 1 件") as raised:
        await client.embed(["0-0", "", "0-2"])
    assert raised.value.diagnostics["first_empty_index"] == 1
    assert client.calls == []


# --- 章節の要約 ----------------------------------------------------------------


def _nav_inputs(count: int) -> tuple[list[DocumentNavigationNode], StructuredExtraction]:
    nodes = []
    elements = []
    for index in range(count):
        element_id = f"e{index}"
        elements.append(
            DocumentElement(kind="text", text=f"章節 {index} の本文。" * 30, element_id=element_id)
        )
        nodes.append(
            DocumentNavigationNode(
                section_id=f"s{index}",
                title=f"章 {index}",
                depth=1,
                section_path=[f"章 {index}"],
                element_ids=[element_id],
            )
        )
    return nodes, StructuredExtraction(raw_text="x", elements=elements)


async def test_navigation_waves_match_sequential_with_failures() -> None:
    nodes, extraction = _nav_inputs(8)
    failing = {"章節 1 ", "章節 4 "}

    async def summarize(text: str) -> str:
        if any(text.startswith(prefix) for prefix in failing):
            raise RuntimeError("失敗")
        return f"要約:{text[:5]}"

    sequential = await summarize_navigation_nodes(nodes, extraction, summarize, max_nodes=4)

    async def summarize_many(texts: Sequence[str]) -> list[str | None]:
        results: list[str | None] = []
        for text in texts:
            try:
                results.append(await summarize(text))
            except RuntimeError:
                results.append(None)
        return results

    waves = await summarize_navigation_nodes(
        nodes, extraction, None, summarize_many=summarize_many, max_nodes=4
    )
    assert [node.summary for node in waves] == [node.summary for node in sequential]
    assert sum(1 for node in waves if node.summary) == 4


async def test_navigation_summary_runs_concurrently_in_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.rag.ingestion import IngestionPipeline

    active = 0
    max_active = 0

    class Llm:
        async def generate(self, prompt: str, text: str) -> str:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return "要約"

    nodes, extraction = _nav_inputs(9)
    pipeline = IngestionPipeline(
        vlm=Llm(),  # type: ignore[arg-type]
        oracle=SimpleNamespace(),  # type: ignore[arg-type]
        settings=Settings(
            rag_navigation_summary_enabled=True,
            rag_navigation_summary_concurrency=3,
            rag_navigation_summary_max_nodes=6,
        ),
    )
    monkeypatch.setattr("app.rag.ingestion.build_navigation_tree", lambda _: nodes)
    result = await pipeline._attach_navigation_tree("trace", extraction)
    assert sum(1 for node in result.navigation if node.summary) == 6
    assert max_active == 3


async def test_navigation_summary_abort_stops_ingestion(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.rag.ingestion import IngestionPipeline, IngestionUserError

    class Llm:
        calls = 0

        async def generate(self, prompt: str, text: str) -> str:
            Llm.calls += 1
            raise HttpError(404, "model not found")

    nodes, extraction = _nav_inputs(5)
    pipeline = IngestionPipeline(
        vlm=Llm(),  # type: ignore[arg-type]
        oracle=SimpleNamespace(),  # type: ignore[arg-type]
        settings=Settings(
            rag_navigation_summary_enabled=True, rag_navigation_summary_concurrency=1
        ),
    )
    monkeypatch.setattr("app.rag.ingestion.build_navigation_tree", lambda _: nodes)
    with pytest.raises(IngestionUserError, match="モデルの設定"):
        await pipeline._attach_navigation_tree("trace", extraction)
    assert Llm.calls == 1


# --- RAPTOR の階層の要約（#2067） ---


def _raptor_leaves(count: int) -> list[Any]:
    from app.rag.chunking import Chunk

    return [
        Chunk(text=f"本文 {i}", index=i, start_offset=i * 10, end_offset=i * 10 + 9)
        for i in range(count)
    ]


class _RaptorLlm:
    """RAPTOR の要約のスタブ（同時の呼び出しの数・SDK の再試行・最初の何回かの失敗）。"""

    def __init__(self, failures: Sequence[BaseException] | None = None) -> None:
        self.failures = list(failures or [])
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.sdk_retries: list[int] = []

    async def generate(self, prompt: str, text: str, *, system_prompt: str | None = None) -> str:
        from rag_parser_core.oci_enterprise_ai import OciEnterpriseAiConfig, sdk_max_retries

        _ = (prompt, system_prompt)
        self.calls += 1
        self.sdk_retries.append(sdk_max_retries(OciEnterpriseAiConfig()))
        failure = self.failures.pop(0) if self.failures else None
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
            if failure is not None:
                raise failure
            return f"要約<{text[:6]}>"
        finally:
            self.active -= 1


def _raptor_pipeline(llm: _RaptorLlm, **settings: Any) -> Any:
    from app.rag.ingestion import IngestionPipeline

    return IngestionPipeline(
        vlm=llm,  # type: ignore[arg-type]
        oracle=SimpleNamespace(),  # type: ignore[arg-type]
        settings=_settings(
            rag_raptor_enabled=True,
            rag_raptor_cluster_size=2,
            rag_raptor_max_levels=1,
            **settings,
        ),
    )


def _capture_raptor_caller(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """取込が作った RAPTOR の段の実行器と、その event を残す。"""
    from app.rag import ingestion as ingestion_module

    captured: list[Any] = []
    original = ingestion_ai_calls

    @contextlib.contextmanager
    def capture(settings: object, stage: str, **kwargs: Any) -> Any:
        with original(settings, stage, **kwargs) as caller:
            events: list[Any] = []
            caller.add_listener(events.append)
            captured.append((stage, caller, events))
            yield caller

    monkeypatch.setattr(ingestion_module, "ingestion_ai_calls", capture)
    return captured


def test_raptor_stage_uses_its_concurrency_setting() -> None:
    settings = Settings.model_construct(rag_raptor_concurrency=3)
    assert ai_call_policy(settings, "raptor_summary").max_concurrency == 3
    assert Settings.model_fields["rag_raptor_concurrency"].default == 4


async def test_raptor_summaries_run_concurrently_and_back_off_on_rate_limit(
    monkeypatch: pytest.MonkeyPatch, no_real_wait: list[float]
) -> None:
    captured = _capture_raptor_caller(monkeypatch)
    # 最初の 3 回が 429（Retry-After 4 秒）。ほかの段と同じく段の全体で待ち、並行の数を下げる。
    llm = _RaptorLlm([HttpError(429, "slow down", {"retry-after": "4"})] * 3)
    pipeline = _raptor_pipeline(llm, rag_raptor_concurrency=4)
    leaves = _raptor_leaves(16)

    result = await pipeline._augment_with_raptor("trace", leaves, None)

    summaries = [chunk for chunk in result if chunk.metadata.get("raptor_summary")]
    # 8 cluster の要約をすべて得る（429 のために失われない）。順番は cluster の順のまま。
    assert len(summaries) == 8
    assert [chunk.metadata["raptor_child_indices"] for chunk in summaries] == [
        f"{i * 2},{i * 2 + 1}" for i in range(8)
    ]
    assert llm.max_active <= 4
    assert llm.calls == 8 + 3
    # SDK の再試行は切る（実行器だけが再試行する）。
    assert set(llm.sdk_retries) == {0}
    [(stage, caller, events)] = captured
    assert stage == "raptor_summary"
    concurrency = [event.concurrency for event in events if event.type == "concurrency"]
    assert concurrency and min(concurrency) < 4
    assert any(wait >= 4 for wait in no_real_wait)
    assert caller.status().retries == 3


async def test_raptor_permission_error_stops_ingestion(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.rag.ingestion import IngestionUserError

    llm = _RaptorLlm([HttpError(403, "NotAuthorizedOrNotFound")] * 10)
    pipeline = _raptor_pipeline(llm, rag_raptor_concurrency=1)

    with pytest.raises(IngestionUserError, match="RAPTOR の階層の要約を止めました"):
        await pipeline._augment_with_raptor("trace", _raptor_leaves(8), None)
    # 再試行せず、残りの cluster も呼ばない。
    assert llm.calls == 1


async def test_raptor_item_failure_skips_only_that_cluster() -> None:
    # 400（その件の誤り）は再試行せずにその cluster を飛ばし、ほかの要約と leaf は残す。
    llm = _RaptorLlm([HttpError(400, "bad request")])
    pipeline = _raptor_pipeline(llm, rag_raptor_concurrency=1)
    leaves = _raptor_leaves(6)

    result = await pipeline._augment_with_raptor("trace", leaves, None)

    assert result[: len(leaves)] == leaves
    summaries = [chunk for chunk in result if chunk.metadata.get("raptor_summary")]
    assert [chunk.metadata["raptor_child_indices"] for chunk in summaries] == ["2,3", "4,5"]


async def test_raptor_item_failure_can_fail_the_job() -> None:
    from app.rag.ingestion import IngestionUserError

    llm = _RaptorLlm([HttpError(400, "bad request")])
    pipeline = _raptor_pipeline(
        llm, rag_raptor_concurrency=1, rag_ai_call_fail_job_on_item_error=True
    )
    with pytest.raises(IngestionUserError, match="RAPTOR の階層の要約を止めました"):
        await pipeline._augment_with_raptor("trace", _raptor_leaves(6), None)


async def test_raptor_checks_cancellation_between_levels() -> None:
    from app.rag.ingestion import IngestionCancelledError

    llm = _RaptorLlm()
    pipeline = _raptor_pipeline(llm, rag_raptor_concurrency=2)
    pipeline._settings = _settings(
        rag_raptor_enabled=True,
        rag_raptor_cluster_size=2,
        rag_raptor_max_levels=3,
        rag_raptor_concurrency=2,
    )
    checks = 0

    async def cancel_after_first_level() -> bool:
        nonlocal checks
        checks += 1
        # 1 回目は段の開始、2 回目は 1 階層目の前、3 回目は 2 階層目の前。
        return checks >= 3

    with pytest.raises(IngestionCancelledError):
        await pipeline._augment_with_raptor("trace", _raptor_leaves(8), cancel_after_first_level)
    assert llm.calls == 4


# --- Vision -----------------------------------------------------------------------------------


class _SlowReader:
    """応答の遅い Vision のスタブ（同時の呼び出しの数と、最初の何回かの失敗）。"""

    def __init__(
        self, failures: Sequence[BaseException] | None = None, delay: float = 0.02
    ) -> None:
        self.failures = list(failures or [])
        self.delay = delay
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    async def generate_from_images(self, images: Sequence[bytes], prompt: str, **_: Any) -> str:
        with self.lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            failure = self.failures.pop(0) if self.failures else None
        try:
            await asyncio.sleep(self.delay)
            if failure is not None:
                raise failure
            return json.dumps({"retrieval_text": f"説明 {len(images)}"}, ensure_ascii=False)
        finally:
            with self.lock:
                self.active -= 1


def _png(tmp_path: Path, name: str) -> Path:
    from PIL import Image

    path = tmp_path / name
    Image.new("RGB", (40, 30), "white").save(path)
    return path


async def test_vision_describer_submit_respects_the_concurrency_limit(tmp_path: Path) -> None:
    from app.rag.vision import EnterpriseAiPictureDescriber

    reader = _SlowReader()
    image = _png(tmp_path, "a.png")
    with ingestion_ai_calls(_settings(rag_vision_concurrency=3), "vision") as caller:
        describer = EnterpriseAiPictureDescriber(
            reader, asyncio.get_running_loop(), model_id="vision", caller=caller
        )
        assert describer.max_concurrency == 3

        def submit_all() -> list[dict[str, Any]]:
            futures = [
                describer.submit(
                    image,
                    {"page": index, "record_id": f"r{index}"},
                    context_image_paths=[image],
                    target_kind="picture",
                    rendered_prompt="prompt",
                )
                for index in range(8)
            ]
            return [future.result() for future in futures]

        results = await asyncio.to_thread(submit_all)
    assert len(results) == 8
    assert reader.max_active == 3


async def test_vision_describer_retries_429_and_final_retry_after_others(tmp_path: Path) -> None:
    from app.rag.vision import EnterpriseAiPictureDescriber

    reader = _SlowReader(
        [HttpError(429, "", {"retry-after": "2"})] + [HttpError(500)] * 3,
    )
    image = _png(tmp_path, "a.png")
    with ingestion_ai_calls(_settings(rag_vision_concurrency=1), "vision") as caller:
        describer = EnterpriseAiPictureDescriber(
            reader, asyncio.get_running_loop(), model_id="vision", caller=caller
        )
        result = await asyncio.to_thread(
            describer.describe,
            image,
            {"page": 1, "record_id": "r1"},
            context_image_paths=[image],
            target_kind="picture",
            rendered_prompt="prompt",
        )
    # 429 → 再試行、500 × 3（1 回 + 再試行 2 回）→ 最後の再試行で成功。
    assert result["retrieval_text"] == "説明 2"
    assert reader.calls == 5
    assert caller.status().retries == 3


async def test_vision_describer_downscales_images_when_too_large(tmp_path: Path) -> None:
    from app.rag.vision import EnterpriseAiPictureDescriber, downscale_images

    sizes: list[int] = []

    class Reader(_SlowReader):
        async def generate_from_images(self, images: Sequence[bytes], prompt: str, **_: Any) -> str:
            sizes.append(len(images[0]))
            return await super().generate_from_images(images, prompt)

    reader = Reader([HttpError(413)])
    image = _png(tmp_path, "a.png")
    with ingestion_ai_calls(_settings(), "vision") as caller:
        describer = EnterpriseAiPictureDescriber(
            reader, asyncio.get_running_loop(), model_id="vision", caller=caller
        )
        await asyncio.to_thread(
            describer.describe,
            image,
            {"page": 1, "record_id": "r1"},
            context_image_paths=[image],
            target_kind="picture",
            rendered_prompt="prompt",
        )
    assert reader.calls == 2
    from io import BytesIO

    from PIL import Image

    smaller = downscale_images([image.read_bytes()], 1)[0]
    with Image.open(BytesIO(smaller)) as opened:
        assert opened.size == (20, 15)


async def test_ingestion_vision_permission_error_stops_job() -> None:
    from app.rag.ingestion import IngestionPipeline, IngestionUserError
    from tests.test_vision import _figure_extraction, _pdf

    class PromptOracle:
        async def answer_prompt_overrides(self) -> dict[str, str]:
            return {}

    reader = _SlowReader([HttpError(403, "Forbidden")] * 5)
    pipeline = IngestionPipeline(
        vlm=reader,  # type: ignore[arg-type]
        oracle=PromptOracle(),  # type: ignore[arg-type]
        settings=Settings(rag_vision_enabled=True),
    )
    with pytest.raises(IngestionUserError, match="IAM policy"):
        await pipeline._attach_vision(
            "trace",
            _figure_extraction([0.168, 0.119, 0.504, 0.297]),
            source_bytes=_pdf(),
            content_type="application/pdf",
            file_name="manual.pdf",
            parser_backend="unstructured",
            cancel_checker=None,
        )
    assert reader.calls == 1


async def test_ingestion_vision_item_failure_can_fail_the_job() -> None:
    from app.rag.ingestion import IngestionPipeline, IngestionUserError
    from tests.test_vision import _figure_extraction, _pdf

    class PromptOracle:
        async def answer_prompt_overrides(self) -> dict[str, str]:
            return {}

    def pipeline(fail_job: bool) -> IngestionPipeline:
        return IngestionPipeline(
            vlm=_SlowReader([HttpError(400, "bad image")] * 5),  # type: ignore[arg-type]
            oracle=PromptOracle(),  # type: ignore[arg-type]
            settings=Settings(rag_vision_enabled=True, rag_ai_call_fail_job_on_item_error=fail_job),
        )

    kwargs: dict[str, Any] = {
        "source_bytes": _pdf(),
        "content_type": "application/pdf",
        "file_name": "manual.pdf",
        "parser_backend": "unstructured",
        "cancel_checker": None,
    }
    extraction = _figure_extraction([0.168, 0.119, 0.504, 0.297])
    result = await pipeline(False)._attach_vision("trace", extraction, **kwargs)
    summary = result.parser_artifacts["vision"]
    assert isinstance(summary, dict) and summary["failed"] == 1
    with pytest.raises(IngestionUserError, match="RAG_AI_CALL_FAIL_JOB_ON_ITEM_ERROR"):
        await pipeline(True)._attach_vision("trace", extraction, **kwargs)


# --- 項目抽出 ---------------------------------------------------------------------------------


async def test_field_extraction_retries_rate_limit_and_stops_on_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.rag import ingestion as ingestion_module
    from app.rag.extraction_field_adapter import load_field_schema
    from app.rag.ingestion import IngestionPipeline, IngestionUserError

    field_defs = load_field_schema().fields[:1]
    if not field_defs:
        pytest.skip("既定の項目の定義がありません")

    class Oracle:
        async def list_document_extraction_field_sets(self, document_id: str) -> list[Any]:
            return []

    monkeypatch.setattr(ingestion_module, "resolve_field_definitions", lambda *_: field_defs)
    extraction = StructuredExtraction(raw_text="契約日は 2026-10-15 です。", elements=[])

    class Llm:
        def __init__(self, failures: list[BaseException]) -> None:
            self.failures = failures
            self.calls = 0

        async def generate(self, prompt: str, text: str, **_options: object) -> str:
            self.calls += 1
            if self.failures:
                raise self.failures.pop(0)
            return json.dumps([{"name": field_defs[0].name, "value": "x", "value_type": "string"}])

    llm = Llm([HttpError(429, "", {"retry-after": "1"})])
    pipeline = IngestionPipeline(
        vlm=llm,  # type: ignore[arg-type]
        oracle=Oracle(),  # type: ignore[arg-type]
        # 単位の結果を保存しない(2 回目の呼び出しが 1 回目の結果を使い直さないように。#2720)。
        settings=Settings(
            rag_field_extraction_enabled=True, rag_extraction_artifact_cache_enabled=False
        ),
    )
    await pipeline._attach_extraction_fields("trace", extraction, document_id="d1")
    assert llm.calls == 2

    quota = Llm([HttpError(429, "insufficient_quota")] * 3)
    pipeline._vlm = quota  # type: ignore[assignment]
    with pytest.raises(IngestionUserError, match="割り当て"):
        await pipeline._attach_extraction_fields("trace", extraction, document_id="d1")
    assert quota.calls == 1
