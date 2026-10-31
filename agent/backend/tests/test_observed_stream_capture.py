"""Agent の取消で、SDK から受信済みの本文・思考を失わない。"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from pr_backend_core.observability.call_policy import call_content_policy
from pr_backend_core.observability.tracing import observed_span, operation_trace

from app.features.agent.builtin_runtime import _ObservedStream


@pytest.mark.asyncio
async def test_cancelled_stream_preserves_received_reasoning_and_input() -> None:
    class Stream:
        count = 0

        async def __anext__(self) -> SimpleNamespace:
            self.count += 1
            if self.count == 1:
                return SimpleNamespace(
                    type="response.reasoning_text.delta", delta="合成の思考", output_index=0
                )
            raise asyncio.CancelledError()

        async def close(self) -> None:
            pass

    with (
        call_content_policy(lambda: True),
        operation_trace("run", operation_id="synthetic") as result,
        observed_span("gen_ai.chat") as span,
    ):
        span.set_call_input({"input": "合成の質問"})
        stream = _ObservedStream(Stream(), span)
        await stream.__anext__()
        with pytest.raises(asyncio.CancelledError):
            await stream.__anext__()
    record = next(item for item in result.spans if item.operation_name == "gen_ai.chat")
    assert record.status == "cancelled"
    assert record.display and record.display.details and record.display.details.call
    call = record.display.details.call
    assert "合成の質問" in call.input.text
    assert json.loads(call.output.text)["output"][0]["content"][0]["text"] == "合成の思考"
