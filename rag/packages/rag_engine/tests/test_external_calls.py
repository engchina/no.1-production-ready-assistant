"""外部の AI の呼び出しの記録と再生（#59）。合成の応答だけを使い、モデルの API に接続しない。

- 記録した応答（成功・時間切れ・HTTP の失敗）を、再生では実の API を呼ばずに同じ順で返す。
- 記録に無い入力・同じ入力の余分な呼び出しは失敗し、実の API へ fallback しない。使わなかった記録も分かる。
- 記録に API キー・入力の本文（プロンプト）を残さない。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import openai
import pytest

from rag_engine.adapters import model_api
from rag_engine.resources.external_calls import (
    ExternalCallMismatch,
    ExternalCallSession,
    external_call,
    external_call_mode,
    use_external_calls,
)

ENDPOINT = "https://enterprise-ai.example/openai/v1"
MODEL = "model-a"
CLIENT_CLASS = model_api._ModelApiClient


def _response(text: str) -> dict[str, Any]:
    return {
        "id": f"resp-{text}",
        "object": "response",
        "created_at": 0,
        "model": MODEL,
        "status": "completed",
        "output": [
            {
                "id": "msg-1",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
    }


def _client(monkeypatch: pytest.MonkeyPatch, handler: Any) -> Any:
    monkeypatch.setattr(
        model_api,
        "_ModelApiClient",
        lambda **kwargs: CLIENT_CLASS(transport=httpx.MockTransport(handler), **kwargs),
    )
    return model_api._openai_client.__wrapped__("provider-a", ENDPOINT, "sk-test-secret", "", 30, 0)


def _live_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if body["input"] == "slow":
        raise httpx.ReadTimeout("timed out", request=request)
    if body["input"] == "busy":
        return httpx.Response(429, json={"error": {"message": "rate limited"}}, headers={"retry-after": "0"})
    return httpx.Response(200, json=_response(f"answer to {body['input']}"))


def _offline_handler(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"再生で実の API を呼びました: {request.url}")


def _record(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    session = ExternalCallSession("record")
    client = _client(monkeypatch, _live_handler)
    with use_external_calls(session):
        assert external_call_mode() == "record"
        assert client.responses.create(model=MODEL, input="q1").output_text == "answer to q1"
        assert client.responses.create(model=MODEL, input="q1").output_text == "answer to q1"
        with pytest.raises(openai.APITimeoutError):
            client.responses.create(model=MODEL, input="slow")
        with pytest.raises(openai.RateLimitError):
            client.responses.create(model=MODEL, input="busy")
    return session.to_recording({"as_of": "2026-10-31"})


def test_replay_returns_recorded_responses_without_calling_api(monkeypatch: pytest.MonkeyPatch) -> None:
    recording = json.loads(json.dumps(_record(monkeypatch)))
    text = json.dumps(recording, ensure_ascii=False)
    assert "sk-test-secret" not in text
    assert '"q1"' not in text  # 入力の本文は hash だけ。

    session = ExternalCallSession.from_recording(recording)
    client = _client(monkeypatch, _offline_handler)
    with use_external_calls(session):
        assert client.responses.create(model=MODEL, input="q1").output_text == "answer to q1"
        assert client.responses.create(model=MODEL, input="q1").output_text == "answer to q1"
        # 時間切れ・流量の制限も記録どおりに再生する（再試行・時間切れの経路の回帰）。
        with pytest.raises(openai.APITimeoutError):
            client.responses.create(model=MODEL, input="slow")
        with pytest.raises(openai.RateLimitError):
            client.responses.create(model=MODEL, input="busy")

    assert session.mismatches == []
    assert session.unused() == []
    assert external_call_mode() == "live"


def test_replay_fails_on_different_input_and_extra_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    session = ExternalCallSession.from_recording(_record(monkeypatch))
    client = _client(monkeypatch, _offline_handler)
    with use_external_calls(session):
        # SDK は transport の失敗を接続の失敗として返す。実の API には fallback しない。
        with pytest.raises(openai.APIConnectionError):
            client.responses.create(model=MODEL, input="q2")
        for _ in range(2):
            client.responses.create(model=MODEL, input="q1")
        with pytest.raises(openai.APIConnectionError):
            client.responses.create(model=MODEL, input="q1")

    assert "記録に無い入力" in session.mismatches[0]
    assert "同じ入力の余分な呼び出し" in session.mismatches[-1]
    # 使わなかった記録（時間切れ・流量の制限の 2 件）は呼び出しの不足として分かる。
    assert [entry["kind"] for entry in session.unused()] == ["http", "http"]


def test_replayed_stream_body_reads_like_live(monkeypatch: pytest.MonkeyPatch) -> None:
    events = b'event: response.output_text.delta\ndata: {"delta": "a"}\n\nevent: done\ndata: {}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=events, headers={"content-type": "text/event-stream"})

    request = httpx.Request("POST", f"{ENDPOINT}/responses", json={"model": MODEL, "stream": True})
    session = ExternalCallSession("record")
    with use_external_calls(session), model_api._ModelApiClient(transport=httpx.MockTransport(handler)) as live:
        recorded = live.send(request, stream=True)
        assert list(recorded.iter_lines())[1] == 'data: {"delta": "a"}'

    replay = ExternalCallSession.from_recording(session.to_recording({}))
    offline = model_api._ModelApiClient(transport=httpx.MockTransport(_offline_handler))
    with use_external_calls(replay), offline:
        replayed = offline.send(request, stream=True)
        assert replayed.headers["content-type"] == "text/event-stream"
        assert b"".join(replayed.iter_bytes()) == events


def test_rerank_is_recorded_and_replayed(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"output": {"results": [{"index": 1, "relevance_score": 0.9}]}}
    monkeypatch.setattr(model_api, "_send_rerank_request", lambda *args: payload)
    session = ExternalCallSession("record")
    body = {"model": "rerank-a", "query": "q", "documents": ["a", "b"]}
    with use_external_calls(session):
        assert model_api._rerank_request(f"{ENDPOINT}/reranks", body, "sk-test-secret", 30) == payload

    def offline(*args: object) -> dict[str, Any]:
        raise AssertionError("再生で実の API を呼びました")

    monkeypatch.setattr(model_api, "_send_rerank_request", offline)
    replay = ExternalCallSession.from_recording(session.to_recording({}))
    with use_external_calls(replay):
        assert model_api._rerank_request(f"{ENDPOINT}/reranks", body, "", 30) == payload
        with pytest.raises(ExternalCallMismatch):
            model_api._rerank_request(f"{ENDPOINT}/reranks", {**body, "query": "other"}, "", 30)


def test_without_session_calls_live_and_rejects_unknown_format() -> None:
    assert external_call("embeddings", {"x": 1}, lambda: "live") == "live"
    with pytest.raises(ValueError, match="形式"):
        ExternalCallSession.from_recording({"format": "other", "entries": []})
