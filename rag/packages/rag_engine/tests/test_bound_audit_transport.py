"""実SDKの送信strict schemaとraw JSON隔離をMockTransportで確認する。外部呼出し0。"""

import json
from typing import Any

import httpx
import pytest
from openai import OpenAI
from test_bound_audit_wire import deref, sample_wire

from rag_engine.adapters import oci
from rag_engine.config import get_settings
from rag_engine.models.audit_wire import AuditWireResult


def response_bytes(raw: str, streamed: bool) -> tuple[bytes, dict[str, str]]:
    item = {
        "id": "msg_synthetic",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": raw, "annotations": []}],
    }
    response = {
        "id": "resp_synthetic",
        "object": "response",
        "created_at": 0,
        "model": "synthetic",
        "status": "completed",
        "output": [item],
        "parallel_tool_calls": False,
        "tools": [],
        "tool_choice": "auto",
        "temperature": 0,
        "top_p": 1,
    }
    if not streamed:
        return json.dumps(response).encode(), {"content-type": "application/json"}
    events = [
        {"type": "response.created", "sequence_number": 0, "response": {**response, "output": [], "status": "in_progress"}},
        {
            "type": "response.output_item.added",
            "sequence_number": 1,
            "output_index": 0,
            "item": {**item, "content": [], "status": "in_progress"},
        },
        {
            "type": "response.content_part.added",
            "sequence_number": 2,
            "item_id": item["id"],
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
        {
            "type": "response.output_text.delta",
            "sequence_number": 3,
            "item_id": item["id"],
            "output_index": 0,
            "content_index": 0,
            "delta": raw,
        },
        {
            "type": "response.output_text.done",
            "sequence_number": 4,
            "item_id": item["id"],
            "output_index": 0,
            "content_index": 0,
            "text": raw,
        },
        {
            "type": "response.content_part.done",
            "sequence_number": 5,
            "item_id": item["id"],
            "output_index": 0,
            "content_index": 0,
            "part": item["content"][0],
        },
        {"type": "response.output_item.done", "sequence_number": 6, "output_index": 0, "item": item},
        {"type": "response.completed", "sequence_number": 7, "response": response},
    ]
    body = "".join("event: " + str(value["type"]) + "\ndata: " + json.dumps(value) + "\n\n" for value in events)
    return body.encode(), {"content-type": "text/event-stream"}


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("fault", ["none", "missing", "duplicate_request", "duplicate_requirement"])
def test_actual_sdk_request_and_partial_decode_preserve_raw_contract(streamed: bool, fault: str, monkeypatch: pytest.MonkeyPatch) -> None:
    schema, payload = sample_wire()
    if fault == "missing":
        payload["guide_requirement_reviews"].pop("step:0")
    raw = json.dumps(payload)
    if fault.startswith("duplicate"):
        key = "Q1" if fault == "duplicate_request" else "step:0"
        field = "request_reviews" if key == "Q1" else "guide_requirement_reviews"
        encoded = json.dumps(payload[field][key])
        token = json.dumps(key) + ": " + encoded
        raw = raw.replace(token, token + ", " + token, 1)
    sent: list[dict[str, Any]] = []

    def transport(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        assert body.get("stream", False) is streamed
        data, headers = response_bytes(raw, streamed)
        return httpx.Response(200, content=data, headers=headers)

    client = OpenAI(
        api_key="mock-only",
        base_url="https://mock.invalid/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    monkeypatch.setattr(oci, "_openai_client_for_provider", lambda *_args: client)
    monkeypatch.setattr(oci, "_stall_detection_enabled", lambda _settings: streamed)
    settings = get_settings(
        environ={
            "OCI_ENTERPRISE_AI_DEFAULT_MODEL": "synthetic",
            "OCI_ENTERPRISE_AI_ENDPOINT": "https://mock.invalid/v1",
            "OCI_ENTERPRISE_AI_API_KEY": "mock-only",
        },
        dotenv_path=None,
        llm_stall_resends=0,
    )
    try:
        result = oci._parse_text_once(
            "## Step 1: 検査する", "合成入力", settings, schema, settings.llm_providers[settings.default_answer_llm]
        )
    finally:
        client.close()
    assert isinstance(result, AuditWireResult)
    assert len(sent) == 1
    actual = sent[0]["text"]["format"]
    assert actual["strict"] is True and actual["type"] == "json_schema"
    for field, keys in [("request_reviews", {"Q1", "Q2"}), ("guide_requirement_reviews", {"step:0", "step:1"})]:
        node = deref(actual["schema"], actual["schema"]["properties"][field])
        assert set(node["required"]) == keys and node["additionalProperties"] is False
    assert result.raw_text == raw
    assert len(result.audit.reviews) == 1
    if fault == "none":
        assert result.validation["strict_wire_validation"] == "passed"
    elif fault == "missing":
        assert result.validation["requirements"]["step:0"] == "missing"
        assert len(result.audit.request_reviews) == 2
    elif fault == "duplicate_request":
        assert result.validation["requests"]["Q1"] == "duplicate"
        assert result.validation["request_binding"]["raw_key_sequence"].count("Q1") == 2
        assert [row.request_id for row in result.audit.request_reviews] == ["Q2"]
    else:
        assert result.validation["requirements"]["step:0"] == "duplicate"
        assert len(result.audit.guide_requirement_reviews) == 1 and len(result.audit.request_reviews) == 2
