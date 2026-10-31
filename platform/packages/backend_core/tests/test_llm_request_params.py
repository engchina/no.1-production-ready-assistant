"""モデルが受け付けない引数を外して送り直す部品のテスト（#3539。合成の 400 の本文）。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from pr_backend_core.llm_request_params import (
    RejectedParameter,
    collect_dropped_parameters,
    droppable_parameter,
    endpoint_key,
    rejected_parameter,
    remember_dropped_parameter,
    remembered_parameters,
    reset_dropped_parameters,
    without_parameters,
)
from pr_backend_core.llm_request_params_httpx import (
    AsyncParameterDroppingClient,
    ParameterDroppingClient,
    asend_dropping_parameters,
    send_dropping_parameters,
)

URL = "https://gateway.example.test/v1/responses"


def _error(message: str, *, param: str | None = None, code: str | None = None) -> str:
    return json.dumps(
        {
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "param": param,
                "code": code,
            }
        }
    )


UNSUPPORTED_TEMPERATURE = _error(
    "Unsupported parameter: 'temperature' is not supported with this model.",
    param="temperature",
)


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    reset_dropped_parameters()
    yield
    reset_dropped_parameters()


# --------------------------------------------------------------------------- 400 の本文の読み方


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (UNSUPPORTED_TEMPERATURE, RejectedParameter("temperature", "unsupported_parameter")),
        # param が無く message だけ
        (
            _error("Unsupported parameter: 'top_p' is not supported with this model."),
            RejectedParameter("top_p", "unsupported_parameter"),
        ),
        (_error("Unknown parameter: 'seed'."), RejectedParameter("seed", "unsupported_parameter")),
        (
            _error("Unrecognized request argument supplied: temperature"),
            RejectedParameter("temperature", "unsupported_parameter"),
        ),
        (
            _error("'top_logprobs' is not supported with this model."),
            RejectedParameter("top_logprobs", "unsupported_parameter"),
        ),
        # 値を拒む（推論モデルの temperature 0）
        (
            _error(
                "Unsupported value: 'temperature' does not support 0 with this model.",
                param="temperature",
                code="unsupported_value",
            ),
            RejectedParameter("temperature", "unsupported_value"),
        ),
        # 入れ子の名前は top-level にする
        (
            _error(
                "Unsupported parameter: 'reasoning.effort' is not supported with this model.",
                param="reasoning.effort",
            ),
            RejectedParameter("reasoning", "unsupported_parameter"),
        ),
        # 受け付けない引数・値を示さない 400（型の誤りなど）は対象にしない
        (_error("Invalid type for 'temperature': expected a number.", param="temperature"), None),
        ("not json", None),
        ("", None),
    ],
    ids=[
        "param-field",
        "message-only-unsupported",
        "message-only-unknown",
        "azure-unrecognized",
        "name-first",
        "unsupported-value",
        "nested-name",
        "invalid-type",
        "not-json",
        "empty",
    ],
)
def test_rejected_parameter(body: str, expected: RejectedParameter | None) -> None:
    assert rejected_parameter(400, body) == expected


def test_rejected_parameter_only_for_400() -> None:
    assert rejected_parameter(422, UNSUPPORTED_TEMPERATURE) is None
    assert rejected_parameter(500, UNSUPPORTED_TEMPERATURE) is None
    assert rejected_parameter(400, UNSUPPORTED_TEMPERATURE.encode()) is not None


@pytest.mark.parametrize(
    ("rejection", "expected"),
    [
        (RejectedParameter("temperature", "unsupported_parameter"), "temperature"),
        (RejectedParameter("temperature", "unsupported_value"), "temperature"),
        (RejectedParameter("reasoning", "unsupported_parameter"), "reasoning"),
        # 推論の量の「値」の誤りは利用者の設定の誤りなので外さない
        (RejectedParameter("reasoning", "unsupported_value"), None),
        # 許可の一覧に無い引数は外さない
        (RejectedParameter("model", "unsupported_parameter"), None),
        (RejectedParameter("input", "unsupported_parameter"), None),
        (RejectedParameter("tools", "unsupported_parameter"), None),
        (RejectedParameter("text", "unsupported_parameter"), None),
        (RejectedParameter("max_output_tokens", "unsupported_parameter"), None),
        # 要求に無い引数は外せない
        (RejectedParameter("top_p", "unsupported_parameter"), None),
        (None, None),
    ],
    ids=[
        "sampling",
        "sampling-value",
        "reasoning-parameter",
        "reasoning-value",
        "model",
        "input",
        "tools",
        "text-format",
        "max-output-tokens",
        "absent",
        "none",
    ],
)
def test_droppable_parameter(rejection: RejectedParameter | None, expected: str | None) -> None:
    payload = {
        "model": "m",
        "input": [],
        "tools": [],
        "text": {},
        "max_output_tokens": 10,
        "temperature": 0,
        "reasoning": {"effort": "high"},
    }
    assert droppable_parameter(rejection, payload) == expected


def test_remember_logs_once_per_model(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="pr_backend_core.llm_request_params"):
        assert remember_dropped_parameter(URL, "model-a", "temperature") is True
        assert remember_dropped_parameter(URL + "/", "model-a", "temperature") is False
    assert len(caplog.records) == 1
    assert "parameter=temperature" in caplog.records[0].getMessage()
    assert remembered_parameters(URL, "model-a") == {"temperature"}
    # 接続かモデルが違えば覚えていない
    assert remembered_parameters(URL, "model-b") == frozenset()
    assert remembered_parameters("https://other.example.test/v1/responses", "model-a") == set()


def test_endpoint_key_and_without_parameters() -> None:
    assert endpoint_key("HTTPS://Gateway.Example.Test/v1/responses/?x=1") == URL
    filtered, removed = without_parameters(
        {"model": "m", "temperature": 0, "top_p": 1}, {"temperature", "top_k"}
    )
    assert filtered == {"model": "m", "top_p": 1}
    assert removed == ("temperature",)


# --------------------------------------------------------------------------- httpx の client


class _Gateway:
    """受け付けない引数を 400 で拒む合成の gateway（受けた本文を記録する）。"""

    def __init__(self, rejected: dict[str, str]) -> None:
        self.rejected = rejected  # 引数 → 400 の本文
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert int(request.headers["content-length"]) == len(request.content)
        self.bodies.append(body)
        for name, error in self.rejected.items():
            if name in body and body.get("model") == "reasoning-model":
                return httpx.Response(400, text=error)
        return httpx.Response(200, json={"output_text": "ok"})


def _payload(model: str = "reasoning-model", **extra: Any) -> dict[str, Any]:
    return {"model": model, "input": "q", "temperature": 0, "max_output_tokens": 10, **extra}


def test_client_drops_and_retries_once_then_remembers() -> None:
    gateway = _Gateway({"temperature": UNSUPPORTED_TEMPERATURE})
    with (
        collect_dropped_parameters() as dropped,
        ParameterDroppingClient(transport=httpx.MockTransport(gateway)) as client,
    ):
        first = client.post(URL, json=_payload())
        assert first.status_code == 200
        assert [("temperature" in body) for body in gateway.bodies] == [True, False]
        # 2 回目は送り直さずに最初から外す
        second = client.post(URL, json=_payload())
        assert second.status_code == 200
        assert len(gateway.bodies) == 3
        assert "temperature" not in gateway.bodies[2]
        assert gateway.bodies[2]["max_output_tokens"] == 10
    assert dropped == {"temperature"}


def test_client_keeps_parameter_for_models_that_accept_it() -> None:
    gateway = _Gateway({"temperature": UNSUPPORTED_TEMPERATURE})
    with ParameterDroppingClient(transport=httpx.MockTransport(gateway)) as client:
        client.post(URL, json=_payload())
        response = client.post(URL, json=_payload(model="sampling-model"))
    assert response.status_code == 200
    assert gateway.bodies[-1]["temperature"] == 0


def test_client_surfaces_400_for_non_allow_listed_parameter() -> None:
    error = _error("Unsupported parameter: 'tools' is not supported.", param="tools")
    gateway = _Gateway({"tools": error})
    with ParameterDroppingClient(transport=httpx.MockTransport(gateway)) as client:
        response = client.post(URL, json=_payload(tools=[]))
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "tools"
    assert len(gateway.bodies) == 1
    assert remembered_parameters(URL, "reasoning-model") == frozenset()


def test_client_retries_only_once_but_remembers_second_parameter() -> None:
    gateway = _Gateway(
        {
            "temperature": UNSUPPORTED_TEMPERATURE,
            "top_p": _error("Unsupported parameter: 'top_p'.", param="top_p"),
        }
    )
    with ParameterDroppingClient(transport=httpx.MockTransport(gateway)) as client:
        failed = client.post(URL, json=_payload(top_p=1))
        assert failed.status_code == 400
        assert len(gateway.bodies) == 2
        assert remembered_parameters(URL, "reasoning-model") == {"temperature", "top_p"}
        ok = client.post(URL, json=_payload(top_p=1))
    assert ok.status_code == 200
    assert len(gateway.bodies) == 3
    assert {"temperature", "top_p"}.isdisjoint(gateway.bodies[2])


def test_client_ignores_other_failures_and_non_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=UNSUPPORTED_TEMPERATURE)

    calls: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    with ParameterDroppingClient(transport=httpx.MockTransport(recording)) as client:
        # JSON でない本文・GET は対象にしない
        assert client.post(URL, content=b"temperature=0").status_code == 400
        assert client.get(URL).status_code == 400
    assert len(calls) == 2


def test_client_streaming_request() -> None:
    gateway = _Gateway({"temperature": UNSUPPORTED_TEMPERATURE})
    with (
        ParameterDroppingClient(transport=httpx.MockTransport(gateway)) as client,
        client.stream("POST", URL, json=_payload()) as response,
    ):
        response.read()
    assert response.status_code == 200
    assert len(gateway.bodies) == 2


def test_async_client_drops_and_remembers() -> None:
    gateway = _Gateway({"temperature": UNSUPPORTED_TEMPERATURE})

    async def run() -> tuple[int, int, set[str]]:
        with collect_dropped_parameters() as dropped:
            async with AsyncParameterDroppingClient(
                transport=httpx.MockTransport(gateway)
            ) as client:
                first = await client.post(URL, json=_payload())
                second = await client.post(URL, json=_payload())
        return first.status_code, second.status_code, set(dropped)

    assert asyncio.run(run()) == (200, 200, {"temperature"})
    assert [("temperature" in body) for body in gateway.bodies] == [True, False, False]


def test_send_helpers_with_plain_clients() -> None:
    """既存の httpx の client のままでも ``send`` を渡せば同じに働く。"""
    gateway = _Gateway({"temperature": UNSUPPORTED_TEMPERATURE})
    with httpx.Client(transport=httpx.MockTransport(gateway)) as client:
        response = send_dropping_parameters(
            client.send, client.build_request("POST", URL, json=_payload())
        )
    assert response.status_code == 200
    assert len(gateway.bodies) == 2

    async def run() -> int:
        async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
            result = await asend_dropping_parameters(
                client.send, client.build_request("POST", URL, json=_payload())
            )
        return result.status_code

    assert asyncio.run(run()) == 200
    # 覚えているので送り直さない
    assert len(gateway.bodies) == 3
    assert "temperature" not in gateway.bodies[2]
