"""一括の LLM・Embedding の呼び出しの上限つきの並行（#2004）。

HTTP・OCI SDK はスタブにし、再試行の待ちは実時間で待たない（``ai_calls._sleep`` を差し替える）。
429 を返しても上限を超えずに結果を失わずに終わること、権限の誤り（403）で再試行せずに止まる
ことを確かめる。
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx
import pytest
from pr_backend_core.bounded_calls import ErrorKind
from test_enterprise_ai_output_budget import (
    _mock_http,
    _ReasoningModel,
    _request,
    _service,
    _settings,
)
from test_nl2sql_embedding_client import _FakeEmbedModels, _FakeEmbedResponse
from test_nl2sql_embedding_client import _settings as _embedding_settings

from app.features.nl2sql import ai_calls
from app.features.nl2sql.ai_calls import nl2sql_error_kind
from app.features.nl2sql.embedding_client import EmbeddingClientError, OciGenAiEmbeddingClient
from app.features.nl2sql.enterprise_ai_client import EnterpriseAiDirectError
from app.settings import get_settings


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(ai_calls, "_sleep", no_wait)


class _Peak:
    """thread をまたいで、同時に動いた数の最大を数える。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.now = 0
        self.peak = 0

    def __enter__(self) -> None:
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)

    def __exit__(self, *_: object) -> None:
        with self.lock:
            self.now -= 1


class _ThrottledModel(_ReasoningModel):
    """最初の ``throttled`` 回は 429（または ``status``）を返し、少し待ってから応答する。"""

    def __init__(self, *, throttled: int = 0, status: int = 429, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.throttled = throttled
        self.status = status
        self.failed = 0
        self.peak = _Peak()
        self.lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self.peak:
            time.sleep(0.02)
            with self.lock:
                fail = self.failed < self.throttled
                if fail:
                    self.failed += 1
            if fail:
                return httpx.Response(
                    self.status,
                    headers={"retry-after": "0"},
                    json={"error": {"message": "Too many requests"}},
                )
            return super().__call__(request)


def test_metadata_sql_429_waits_globally_and_keeps_every_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """組を上限まで同時に呼び、429 は実行器が再試行して、規則ベースに戻さずに終わる。"""
    monkeypatch.setattr(get_settings(), "nl2sql_metadata_sql_concurrency", 2)
    model = _ThrottledModel(throttled=2, tokens_per_target=280)
    _mock_http(monkeypatch, model)
    service = _service(_settings(max_output_tokens=4_096))

    result = service.generate_comment_sql(_request(service, 25))

    assert result.source == "oci_enterprise_ai"
    assert result.fallback is False
    assert result.sql.count("COMMENT ON TABLE") == 25
    # 3 組（10・10・5 表）と、429 の 2 回の再試行。client の再試行は切っている。
    assert len(model.payloads) == 3
    assert model.failed == 2
    assert model.peak.peak <= 2


def test_metadata_sql_permission_error_falls_back_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """権限の誤り（403）は再試行せず、規則ベースに戻して設定の誤りの理由を出す。"""
    monkeypatch.setattr(get_settings(), "nl2sql_metadata_sql_concurrency", 1)
    model = _ThrottledModel(throttled=99, status=403)
    _mock_http(monkeypatch, model)
    service = _service(_settings(max_output_tokens=4_096))

    result = service.generate_comment_sql(_request(service, 12))

    assert result.fallback is True
    # 最初の組の 1 回で実行器が止まり、次の組は呼ばずに同じ失敗になる。
    assert model.failed == 1


def test_nl2sql_error_kind_maps_non_http_codes() -> None:
    assert nl2sql_error_kind(EnterpriseAiDirectError("x", code="response_format")) is (
        ErrorKind.INVALID_OUTPUT
    )
    assert nl2sql_error_kind(EnterpriseAiDirectError("x", code="incomplete")) is (
        ErrorKind.TRUNCATED
    )
    assert nl2sql_error_kind(RuntimeError("x")) is None


class _OciError(Exception):
    """OCI SDK の ServiceError と同じく ``status`` と ``headers`` を持つスタブ。"""

    def __init__(self, status: int) -> None:
        super().__init__(f"status {status}")
        self.status = status
        self.headers: dict[str, str] = {}


class _ThrottledEmbedClient:
    def __init__(self, *, errors: list[Exception]) -> None:
        self.errors = errors
        self.lock = threading.Lock()
        self.calls: list[list[str]] = []
        self.peak = _Peak()

    def embed_text(self, details: Any) -> _FakeEmbedResponse:
        with self.peak:
            time.sleep(0.02)
            with self.lock:
                self.calls.append(list(details.inputs))
                error = self.errors.pop(0) if self.errors else None
            if error is not None:
                raise error
            return _FakeEmbedResponse(
                [[float(int(text.rsplit("-", 1)[1])), *([0.0] * 1535)] for text in details.inputs]
            )


def test_embedding_429_keeps_order_and_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OciGenAiEmbeddingClient(_embedding_settings())
    fake = _ThrottledEmbedClient(errors=[_OciError(429)])
    monkeypatch.setattr(client, "_get_client", lambda: fake)
    monkeypatch.setattr(client, "_load_models", lambda: _FakeEmbedModels)

    vectors = client.embed_texts([f"text-{index}" for index in range(400)])

    assert [vector[0] for vector in vectors] == [float(index) for index in range(400)]
    # 5 組（96 件ずつ）と 429 の再試行 1 回。上限（既定 2）を超えない。
    assert len(fake.calls) == 6
    assert fake.peak.peak <= client.settings.nl2sql_embedding_concurrency == 2


def test_embedding_permission_error_raises_client_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OciGenAiEmbeddingClient(_embedding_settings())
    fake = _ThrottledEmbedClient(errors=[_OciError(403)] * 10)
    monkeypatch.setattr(client, "_get_client", lambda: fake)
    monkeypatch.setattr(client, "_load_models", lambda: _FakeEmbedModels)
    client.settings.nl2sql_embedding_concurrency = 1

    with pytest.raises(EmbeddingClientError) as exc_info:
        client.embed_texts([f"text-{index}" for index in range(200)])

    assert "権限" in str(exc_info.value)
    assert len(fake.calls) == 1


def test_quality_evaluation_stage_retries_only_rate_limits_and_stops_on_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQL生成評価の段（#2059）: 429 だけを送り直し、権限・設定の誤りは評価を止める。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_concurrency", 3)
    policy = ai_calls.make_ai_caller(settings, "quality_evaluation").policy
    assert policy.max_concurrency == 3
    assert type(settings).model_fields["nl2sql_quality_evaluation_concurrency"].default == 2
    rate = policy.rule(ErrorKind.RATE_LIMITED)
    assert (rate.retries, rate.throttle) == (3, True)
    for kind in (ErrorKind.TIMEOUT, ErrorKind.SERVER_ERROR, ErrorKind.UNAVAILABLE):
        assert policy.rule(kind).retries == 0
        assert policy.rule(kind).disposition.value == "item"
    for kind in (ErrorKind.PERMISSION, ErrorKind.AUTHENTICATION, ErrorKind.QUOTA_EXHAUSTED):
        assert policy.rule(kind).disposition.value == "job"


def test_quality_evaluation_classifies_wrapped_enterprise_ai_rate_limit() -> None:
    """厳密な SQL 生成が包んだ Enterprise AI の 429 も、原因の連鎖から流量の制限と分かる。"""
    from pr_system_settings.enterprise_ai_errors import classify_enterprise_ai_http_failure

    caller = ai_calls.make_ai_caller(get_settings(), "quality_evaluation")
    cause = EnterpriseAiDirectError(
        "OCI Enterprise AI HTTP 429: slow down",
        http_failure=classify_enterprise_ai_http_failure(429, "slow down"),
        headers={"retry-after": "7"},
    )
    wrapped = RuntimeError(str(cause))
    wrapped.__cause__ = cause
    info = caller._classify(wrapped)  # noqa: SLF001
    assert info.kind is ErrorKind.RATE_LIMITED
    assert info.retry_after == 7.0
