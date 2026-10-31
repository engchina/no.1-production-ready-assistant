"""HTTP service retry helper の単体テスト。"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from app.clients import http_retry
from app.clients.http_retry import (
    HttpRetryConfig,
    async_request_with_retry,
    request_with_retry,
)


def test_request_with_retry_defaults_to_five_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="warming up")

    monkeypatch.setattr(http_retry.time, "sleep", sleeps.append)
    transport = httpx.MockTransport(handle)

    with httpx.Client(transport=transport) as client:
        response = request_with_retry(
            client,
            "GET",
            "http://svc.example/health",
            retry=HttpRetryConfig(),
            logger=logging.getLogger(__name__),
        )

    assert response.status_code == 503
    assert calls == 5
    assert sleeps == [0.5, 1.0, 2.0, 4.0]


def test_request_with_retry_does_not_retry_non_retryable_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, text="bad request")

    monkeypatch.setattr(http_retry.time, "sleep", lambda _delay: None)
    transport = httpx.MockTransport(handle)

    with httpx.Client(transport=transport) as client:
        response = request_with_retry(
            client,
            "POST",
            "http://svc.example/run",
            retry=HttpRetryConfig(),
            logger=logging.getLogger(__name__),
        )

    assert response.status_code == 400
    assert calls == 1


def test_request_with_retry_retries_request_errors_then_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(http_retry.time, "sleep", lambda _delay: None)
    transport = httpx.MockTransport(handle)

    with httpx.Client(transport=transport) as client, pytest.raises(httpx.ConnectError):
        request_with_retry(
            client,
            "GET",
            "http://svc.example/health",
            retry=HttpRetryConfig(attempts=3),
            logger=logging.getLogger(__name__),
        )

    assert calls == 3


def test_request_with_retry_caps_exponential_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, text="busy")

    monkeypatch.setattr(http_retry.time, "sleep", sleeps.append)
    transport = httpx.MockTransport(handle)

    with httpx.Client(transport=transport) as client:
        response = request_with_retry(
            client,
            "GET",
            "http://svc.example/health",
            retry=HttpRetryConfig(attempts=4, initial_delay_seconds=0.25, max_delay_seconds=0.7),
            logger=logging.getLogger(__name__),
        )

    assert response.status_code == 429
    assert calls == 4
    assert sleeps == [0.25, 0.5, 0.7]


def test_async_request_with_retry_defaults_to_five_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="warming up")

    async def fake_sleep(delay: float, *_args: Any, **_kwargs: Any) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(http_retry.asyncio, "sleep", fake_sleep)
    transport = httpx.MockTransport(handle)

    async def run() -> httpx.Response:
        async with httpx.AsyncClient(transport=transport) as client:
            return await async_request_with_retry(
                client,
                "GET",
                "http://svc.example/health",
                retry=HttpRetryConfig(),
                logger=logging.getLogger(__name__),
            )

    response = http_retry.asyncio.run(run())

    assert response.status_code == 503
    assert calls == 5
    assert sleeps == [0.5, 1.0, 2.0, 4.0]
