"""engine へ注入した observer が backend に依存せず親子関係を保つ。"""

import asyncio
import importlib
from typing import Any

import pytest
from pr_backend_core.config.tracing import TracingSettings
from pr_backend_core.observability.tracing import (
    configure_tracing,
    operation_trace,
    shutdown_tracing,
)
from rag_engine.generation.execution_record import _execution_step
from rag_engine.resources.tracing import bind_trace_observer, observe_call

from app.clients.oci_genai import OciGenAiClient
from app.config import Settings
from app.rag.pipeline import _observe_engine_call


@pytest.mark.asyncio
async def test_engine_stage_and_client_keep_parent_across_thread() -> None:
    def run_engine() -> None:
        with _execution_step("合成の工程"), observe_call("chat", "model-a") as observation:
            observation.request(
                {"input": "合成の入力", "authorization": "synthetic-secret"},
                schema={"type": "object"},
            )
            observation.response({"output": "合成の結果"})
            observation.usage({"input_tokens": 10, "output_tokens": 0})

    configure_tracing(TracingSettings(), service_name="synthetic")
    try:
        with (
            operation_trace("rag.search", operation_id="synthetic") as summary,
            bind_trace_observer(_observe_engine_call),
        ):
            await asyncio.to_thread(run_engine)
        stage = next(span for span in summary.spans if span.operation_name == "rag.engine.stage")
        llm = next(span for span in summary.spans if span.operation_name == "gen_ai.chat")
        assert llm.display and llm.display.details and llm.display.details.call
        call = llm.display.details.call
        assert call.visibility == "system_admin"
        assert "合成の入力" in call.input.text
        assert "synthetic-secret" not in call.input.text
        assert "合成の結果" in call.output.text
        assert call.schema_.status == "recorded"
        assert "合成の入力" not in str(llm.attributes)
        assert stage.parent_span_id == summary.root_span_id
        assert llm.parent_span_id == stage.span_id
        assert llm.attributes["gen_ai.usage.output_tokens"] == 0
        assert "gen_ai.usage.cache_read.input_tokens" not in llm.attributes
    finally:
        shutdown_tracing()


def test_sdk_snapshot_keeps_rerank_diagnostics_and_omits_image() -> None:
    models = importlib.import_module("oci.generative_ai_inference.models")

    request = models.EmbedTextDetails(
        inputs=["合成の入力"],
        serving_mode=models.OnDemandServingMode(model_id="synthetic-model"),
        embed_contents=[
            models.EmbedImageContent(
                image_url=models.ImageUrl(url="data:image/png;base64,synthetic-image")
            )
        ],
    )
    configure_tracing(TracingSettings(), service_name="synthetic")
    try:
        with (
            operation_trace("rag.search", operation_id="synthetic") as summary,
            _observe_engine_call("rerank", "synthetic-model") as observation,
        ):
            observation.request(request)
            observation.response({"document_ranks": [{"index": 0, "relevance_score": 0.9}]})
            observation.display(
                details={"code": [{"label": "rerank", "text": "{}", "language": "json"}]}
            )
        call_span = next(span for span in summary.spans if span.operation_name == "gen_ai.rerank")
        assert call_span.display and call_span.display.details and call_span.display.details.call
        details = call_span.display.details
        assert details.call and details.call.visibility == "system_admin"
        assert (
            "合成の入力" in details.call.input.text and "synthetic-model" in details.call.input.text
        )
        assert "synthetic-image" not in details.call.input.text
        assert "media omitted" in details.call.input.text
        assert '"relevance_score": 0.9' in details.call.output.text
        assert details.call.schema_.status == "not_recorded"
        assert details.code and details.code[0].label == "rerank"
    finally:
        shutdown_tracing()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["embeddings", "rerank"])
@pytest.mark.parametrize("failed", [False, True])
async def test_backend_sdk_boundary_keeps_request_response_and_failure(
    operation: str, failed: bool
) -> None:

    sent: list[object] = []

    def invoke(url: str, body: dict[str, object]) -> dict[str, object]:
        sent.append(body)
        if failed:
            raise RuntimeError("合成の接続失敗")
        if operation == "embeddings":
            return {"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]}
        return {"results": [{"index": 0, "relevance_score": 0.9}]}

    client = OciGenAiClient(
        settings=Settings(
            oci_enterprise_ai_endpoint="https://synthetic.invalid/compatible-mode/v1",
            oci_enterprise_ai_api_key="sk-synthetic",
        ),
        post=invoke,
    )
    configure_tracing(TracingSettings(), service_name="synthetic")
    try:
        with operation_trace("rag.search", operation_id="synthetic") as summary:
            call = (
                client._embed_with_oci(["合成の入力"], input_type="SEARCH_QUERY")
                if operation == "embeddings"
                else client._rerank_with_oci("合成の入力", ["合成の資料"], 1)
            )
            if failed:
                with pytest.raises(RuntimeError, match="合成の接続失敗"):
                    await call
            else:
                await call
        span = next(span for span in summary.spans if span.operation_name == f"gen_ai.{operation}")
        assert span.display and span.display.details and span.display.details.call
        details = span.display.details.call
        assert details.visibility == "system_admin" and "合成の入力" in details.input.text
        assert (
            details.output.status == "recorded"
            and ("RuntimeError" in details.output.text) == failed
        )
        assert len(sent) == 1
        assert "合成の入力" not in str(span.attributes)
    finally:
        shutdown_tracing()


@pytest.fixture(autouse=True)
def enable_trace_content_for_snapshot_tests() -> Any:
    from pr_backend_core.observability.call_policy import call_content_policy

    with call_content_policy(lambda: True):
        yield
