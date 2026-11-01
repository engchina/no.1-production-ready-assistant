"""上限つきの並行の実行器（#2002）。時計と sleep は仮想の時間で、実時間では待たない。"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import heapq
import json
import ssl
import threading
import time
from collections.abc import Awaitable, Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import pytest

from pr_backend_core.bounded_calls import (
    DEFAULT_KIND_RULES,
    Attempt,
    BoundedCallAborted,
    BoundedCaller,
    BoundedCallFailed,
    BoundedCallPolicy,
    CallEvent,
    ClassifiedError,
    Disposition,
    ErrorKind,
    SyncBoundedCaller,
    chain_classifiers,
    classify_error,
    parse_retry_after,
    policy_with_concurrency,
)

# --- 仮想の時間 ---------------------------------------------------------------------------


class VirtualTime:
    """``sleep`` を仮想の時計で進める。すべての task が待ちに入ったら、次の時刻へ進める。"""

    def __init__(self) -> None:
        self.now = 0.0
        self._timers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._seq = 0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._seq += 1
        heapq.heappush(self._timers, (self.now + max(0.0, seconds), self._seq, future))
        await future

    async def run[T](self, coroutine: Coroutine[Any, Any, T]) -> T:
        task = asyncio.ensure_future(coroutine)
        idle = 0
        while not task.done():
            for _ in range(30):
                await asyncio.sleep(0)
                if task.done():
                    break
            if task.done():
                break
            if not self._timers:
                idle += 1
                assert idle < 1000, "仮想の時間で待つものが無いのに終わらない"
                continue
            when, _, future = heapq.heappop(self._timers)
            self.now = max(self.now, when)
            if not future.done():
                future.set_result(None)
        return task.result()


class StubError(Exception):
    """HTTP の失敗のスタブ（httpx / openai と同じく ``response.status_code`` / ``headers``）。"""

    def __init__(self, status: int, body: str = "", headers: dict[str, str] | None = None) -> None:
        super().__init__(f"returned {status}: {body}")
        self.response = type("Resp", (), {"status_code": status, "headers": headers or {}})()


class UpstreamServiceError(Exception):
    """SDK の HTTP の例外の形（status・code・message・headers の属性）。"""

    def __init__(
        self, status: int, code: str, message: str = "", headers: dict[str, str] | None = None
    ):
        super().__init__(f"{status} {code} {message}")
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


class OpenAiStatusError(Exception):
    def __init__(self, status_code: int, message: str, body: object = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class OpenAiStreamError(Exception):
    """openai SDK の ``APIError``（stream の error event）と同じ属性。HTTP status を持たない。"""

    def __init__(self, message: str, body: object = None) -> None:
        super().__init__(message)
        self.message = message
        self.body = body
        self.code = body.get("code") if isinstance(body, dict) else None


class EnterpriseAiIncompleteResponseError(ValueError):
    pass


class PictureValidationError(ValueError):
    pass


class APIConnectionError(Exception):
    pass


def make_caller(
    vt: VirtualTime,
    *,
    events: list[CallEvent] | None = None,
    random_value: float = 0.5,
    refresh: Callable[[], Awaitable[None] | None] | None = None,
    **policy: Any,
) -> BoundedCaller:
    defaults: dict[str, Any] = {
        "max_concurrency": 4,
        "final_retry_delay_seconds": 30.0,
        "heartbeat_interval_seconds": 5.0,
    }
    defaults.update(policy)
    return BoundedCaller(
        "test",
        BoundedCallPolicy(**defaults),
        label="テストの段",
        clock=vt.clock,
        sleep=vt.sleep,
        random=lambda: random_value,
        on_event=events.append if events is not None else None,
        refresh_credentials=refresh,
    )


class Service:
    """遅延と、呼び出しごとの失敗を返すスタブの API（同時の呼び出しの数を数える）。"""

    def __init__(self, vt: VirtualTime, latency: float = 1.0) -> None:
        self.vt = vt
        self.latency = latency
        self.in_flight = 0
        self.max_in_flight = 0
        self.starts: list[tuple[float, object]] = []
        self.plan: dict[object, list[BaseException | None]] = {}
        self.attempts: list[Attempt] = []

    async def __call__(self, item: object, attempt: Attempt) -> str:
        self.starts.append((self.vt.now, item))
        self.attempts.append(attempt)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await self.vt.sleep(self.latency)
            failures = self.plan.get(item)
            if failures:
                error = failures.pop(0)
                if error is not None:
                    raise error
            return f"ok:{item}"
        finally:
            self.in_flight -= 1


def calls_of(service: Service, item: object) -> int:
    return sum(1 for _, started in service.starts if started == item)


# --- 分類 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (StubError(429, "Too many requests"), ErrorKind.RATE_LIMITED),
        (StubError(429, '{"error": {"code": "insufficient_quota"}}'), ErrorKind.QUOTA_EXHAUSTED),
        (StubError(429, "token quota per hour"), ErrorKind.QUOTA_EXHAUSTED),
        (UpstreamServiceError(429, "TooManyRequests"), ErrorKind.RATE_LIMITED),
        (StubError(500, "internal"), ErrorKind.SERVER_ERROR),
        (StubError(502), ErrorKind.UNAVAILABLE),
        (StubError(503), ErrorKind.UNAVAILABLE),
        (StubError(504), ErrorKind.UNAVAILABLE),
        (StubError(501), ErrorKind.NOT_SUPPORTED),
        (StubError(505), ErrorKind.NOT_SUPPORTED),
        (ConnectionResetError("reset by peer"), ErrorKind.NETWORK),
        (APIConnectionError("Connection error."), ErrorKind.NETWORK),
        (TimeoutError("read timeout"), ErrorKind.TIMEOUT),
        (StubError(408), ErrorKind.TIMEOUT),
        (StubError(401), ErrorKind.AUTHENTICATION),
        (UpstreamServiceError(401, "NotAuthenticated"), ErrorKind.AUTHENTICATION),
        (StubError(403), ErrorKind.PERMISSION),
        (
            UpstreamServiceError(404, "NotAuthorizedOrNotFound", "Authorization failed"),
            ErrorKind.PERMISSION,
        ),
        (StubError(404, "model not found"), ErrorKind.NOT_FOUND),
        (StubError(400, "invalid parameter"), ErrorKind.BAD_REQUEST),
        (StubError(413), ErrorKind.TOO_LARGE),
        (
            OpenAiStatusError(400, "context_length_exceeded: maximum context length"),
            ErrorKind.TOO_LARGE,
        ),
        (
            # vLLM 系のモデルの入力の長さの超過（#2720。合成の入力での実測の形）。
            StubError(
                400,
                '{"error":{"message":"The engine prompt length 132224 exceeds the max_model_len'
                ' 131072. Please reduce prompt.","type":"invalid_request_error","param":"input",'
                '"code":400}}',
            ),
            ErrorKind.TOO_LARGE,
        ),
        (StubError(400, "The response was filtered due to content_filter"), ErrorKind.SAFETY),
        (
            ValueError("Model API response status=incomplete: content_filter"),
            ErrorKind.SAFETY,
        ),
        (PictureValidationError("schema mismatch"), ErrorKind.INVALID_OUTPUT),
        (EnterpriseAiIncompleteResponseError("出力の上限"), ErrorKind.TRUNCATED),
        (ValueError("status=incomplete reason=max_output_tokens"), ErrorKind.TRUNCATED),
        (RuntimeError("bug"), ErrorKind.UNKNOWN),
    ],
    ids=lambda value: value.value if isinstance(value, ErrorKind) else type(value).__name__,
)
def test_classify_error_kinds(error: BaseException, kind: ErrorKind) -> None:
    assert classify_error(error).kind is kind


def test_classify_error_finds_tls_and_status_in_cause_chain() -> None:
    try:
        try:
            raise ssl.SSLCertVerificationError("certificate verify failed")
        except ssl.SSLError as inner:
            raise APIConnectionError("Connection error.") from inner
    except APIConnectionError as outer:
        assert classify_error(outer).kind is ErrorKind.TLS

    try:
        try:
            raise StubError(503, "busy", {"Retry-After": "7"})
        except StubError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        info = classify_error(outer)
    assert (info.kind, info.status, info.retry_after) == (ErrorKind.UNAVAILABLE, 503, 7.0)


def _nested(inner: dict[str, object]) -> OpenAiStreamError:
    """呼び先のエラーを JSON の文字列にして ``body.message`` に入れた、status の無い例外。"""
    text = json.dumps({"error": inner})
    return OpenAiStreamError(text, body={"code": None, "message": text})


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        # 割り当て（token の期間の上限）の使い切り（#2667）。
        (
            _nested({"code": 429, "status": "resource-exhausted", "message": "token quota"}),
            ErrorKind.QUOTA_EXHAUSTED,
        ),
        (_nested({"status": "RESOURCE_EXHAUSTED", "message": "x"}), ErrorKind.RATE_LIMITED),
        (OpenAiStreamError("x", body={"code": "rate_limit_exceeded"}), ErrorKind.RATE_LIMITED),
        (OpenAiStreamError("x", body={"code": "insufficient_quota"}), ErrorKind.QUOTA_EXHAUSTED),
        (_nested({"code": "401", "message": "x"}), ErrorKind.AUTHENTICATION),
        (_nested({"status": "PERMISSION_DENIED"}), ErrorKind.PERMISSION),
        (OpenAiStreamError("x", body={"code": "model_not_found"}), ErrorKind.NOT_FOUND),
        (_nested({"status": "INVALID_ARGUMENT", "message": "x"}), ErrorKind.BAD_REQUEST),
        (
            OpenAiStreamError("x", body={"type": "invalid_request_error", "code": None}),
            ErrorKind.BAD_REQUEST,
        ),
        (_nested({"status": "UNAVAILABLE"}), ErrorKind.UNAVAILABLE),
        (_nested({"status": "DEADLINE_EXCEEDED"}), ErrorKind.TIMEOUT),
        # 外側が汎用の誤りでも、入れ子の内側（呼び先の元のエラー）の code を使う。
        (
            OpenAiStreamError(
                "x",
                body={
                    "code": "server_error",
                    "message": json.dumps({"error": {"code": 429, "message": "quota"}}),
                },
            ),
            ErrorKind.QUOTA_EXHAUSTED,
        ),
        # 分類できる code の無い本文・読めない JSON は分類しない。
        (OpenAiStreamError("x", body={"code": None, "message": "boom"}), ErrorKind.UNKNOWN),
        (OpenAiStreamError("x", body={"message": '{"error": '}), ErrorKind.UNKNOWN),
        # 例外の文（str）の JSON は読まない（出力の検証の失敗の文に入ったモデルの出力など）。
        (RuntimeError('{"status": "not_found"}'), ErrorKind.UNKNOWN),
    ],
    ids=lambda value: value.value if isinstance(value, ErrorKind) else type(value).__name__,
)
def test_classify_error_reads_structured_error_without_status(
    error: BaseException, kind: ErrorKind
) -> None:
    info = classify_error(error)
    assert info.kind is kind
    # HTTP status は推定なので残さない。
    assert info.status is None


def test_parse_retry_after_seconds_ms_and_http_date() -> None:
    assert parse_retry_after({"retry-after": "12"}) == 12.0
    assert parse_retry_after({"retry-after-ms": "1500", "retry-after": "9"}) == 1.5
    now = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
    date = format_datetime(now + timedelta(seconds=40), usegmt=True)
    assert parse_retry_after({"Retry-After": date}, now=lambda: now) == pytest.approx(40.0)
    assert parse_retry_after({"retry-after": "soon"}) is None
    assert parse_retry_after({}) is None
    assert parse_retry_after(None) is None


def test_chain_classifiers_lets_caller_override_kind() -> None:
    class EmptyResponse(ValueError):
        pass

    classify = chain_classifiers(
        lambda exc: ErrorKind.INVALID_OUTPUT if isinstance(exc, EmptyResponse) else None
    )
    assert classify(EmptyResponse("")).kind is ErrorKind.INVALID_OUTPUT
    assert classify(StubError(503)).kind is ErrorKind.UNAVAILABLE
    custom = chain_classifiers(lambda exc: ClassifiedError(ErrorKind.SAFETY, detail="x"))
    assert custom(RuntimeError()).kind is ErrorKind.SAFETY


def test_default_rules_follow_the_table() -> None:
    rules = DEFAULT_KIND_RULES
    assert rules[ErrorKind.RATE_LIMITED].throttle and rules[ErrorKind.RATE_LIMITED].retries > 0
    for kind in (
        ErrorKind.QUOTA_EXHAUSTED,
        ErrorKind.NOT_SUPPORTED,
        ErrorKind.TLS,
        ErrorKind.PERMISSION,
        ErrorKind.NOT_FOUND,
    ):
        assert rules[kind].retries == 0 and rules[kind].disposition is Disposition.JOB
    assert rules[ErrorKind.SERVER_ERROR].retries == 2
    assert rules[ErrorKind.INVALID_OUTPUT].retries == 2
    assert rules[ErrorKind.AUTHENTICATION].retries == 1
    assert rules[ErrorKind.AUTHENTICATION].refresh_credentials
    for kind in (ErrorKind.BAD_REQUEST, ErrorKind.SAFETY, ErrorKind.UNKNOWN):
        assert rules[kind].retries == 0 and rules[kind].disposition is Disposition.ITEM
    assert rules[ErrorKind.TOO_LARGE].adaptive_only and rules[ErrorKind.TRUNCATED].adaptive_only


def test_policy_validation() -> None:
    with pytest.raises(ValueError):
        BoundedCallPolicy(max_concurrency=0)
    with pytest.raises(ValueError):
        BoundedCallPolicy(max_concurrency=2, min_concurrency=3)
    assert policy_with_concurrency(BoundedCallPolicy(), 0).max_concurrency == 1


# --- 並行・順番 ---------------------------------------------------------------------------


async def test_map_keeps_input_order_and_never_exceeds_the_limit() -> None:
    vt = VirtualTime()
    service = Service(vt)
    caller = make_caller(vt, max_concurrency=3)
    items = list(range(10))
    outcomes = await vt.run(caller.map(items, service))
    assert [outcome.value for outcome in outcomes] == [f"ok:{item}" for item in items]
    assert service.max_in_flight == 3
    # 10 件・1 件 1 秒・3 並行 → 4 秒（順番なら 10 秒）。
    assert vt.now == pytest.approx(4.0)


async def test_sequential_limit_is_one_call_at_a_time() -> None:
    vt = VirtualTime()
    service = Service(vt)
    outcomes = await vt.run(make_caller(vt, max_concurrency=1).map([1, 2, 3], service))
    assert all(outcome.ok for outcome in outcomes)
    assert service.max_in_flight == 1
    assert vt.now == pytest.approx(3.0)


# --- 429: 全体の待ち・AIMD -----------------------------------------------------------------


async def test_rate_limit_with_retry_after_pauses_all_calls_and_halves_concurrency() -> None:
    vt = VirtualTime()
    events: list[CallEvent] = []
    service = Service(vt)
    service.plan[0] = [StubError(429, "slow down", {"retry-after": "20"})]
    caller = make_caller(vt, events=events, max_concurrency=4, increase_after_successes=2)
    outcomes = await vt.run(caller.map(list(range(12)), service))
    assert all(outcome.ok for outcome in outcomes)
    # 0〜3 を 0 秒に始め、1 秒で 0 が 429。4〜6 は 1 秒に始まっている（すでに枠を取った）。
    # 429 の後は Retry-After の 20 秒（+ jitter 0.5 秒）まで誰も始めない。
    resumed = [started for started, _ in service.starts if started > 1.0]
    assert resumed and min(resumed) >= 21.0
    assert not [started for started, _ in service.starts if 1.0 < started < 21.0]
    concurrency = [event.concurrency for event in events if event.type == "concurrency"]
    assert concurrency[0] == 2  # 半分にした
    assert concurrency[-1] == 4  # 成功が続いて上限まで戻した
    assert max(concurrency) <= 4
    assert calls_of(service, 0) == 2


async def test_rate_limit_without_retry_after_uses_full_jitter_backoff() -> None:
    vt = VirtualTime()
    events: list[CallEvent] = []
    service = Service(vt)
    service.plan["x"] = [StubError(429), StubError(429), StubError(429)]
    caller = make_caller(vt, events=events, random_value=0.5, backoff_base_seconds=2.0)
    outcome = await vt.run(caller.map(["x"], service))
    assert outcome[0].ok
    delays = [event.delay_seconds for event in events if event.type == "retry"]
    # full jitter: 0.5 × min(60, 2 × 2^(n-1)) = 1, 2, 4
    assert delays == [1.0, 2.0, 4.0]


async def test_rate_limit_retry_after_beats_own_backoff_and_is_capped() -> None:
    vt = VirtualTime()
    events: list[CallEvent] = []
    service = Service(vt)
    service.plan["x"] = [StubError(429, "", {"retry-after": "900"})]
    caller = make_caller(vt, events=events, random_value=0.0, retry_after_max_seconds=120.0)
    await vt.run(caller.map(["x"], service))
    assert [event.delay_seconds for event in events if event.type == "retry"] == [120.0]


async def test_waiting_emits_heartbeat_events_with_remaining_seconds() -> None:
    vt = VirtualTime()
    events: list[CallEvent] = []
    service = Service(vt)
    service.plan["x"] = [StubError(429, "", {"retry-after": "30"})]
    caller = make_caller(vt, events=events, random_value=0.0, heartbeat_interval_seconds=10.0)
    await vt.run(caller.map(["x"], service))
    waits = [event for event in events if event.type == "wait"]
    assert [round(event.remaining_seconds or 0) for event in waits] == [30, 20, 10]
    assert {event.reason for event in waits} == {"rate_limited"}
    assert any(event.type == "wait_end" for event in events)


async def test_status_reports_waiting_reason_during_pause() -> None:
    vt = VirtualTime()
    seen: list[tuple[str | None, float | None]] = []
    service = Service(vt)
    service.plan["x"] = [StubError(429, "", {"retry-after": "10"})]
    caller = make_caller(vt, random_value=0.0, heartbeat_interval_seconds=5.0)
    caller.add_listener(
        lambda event: (
            seen.append((caller.status().waiting_reason, caller.status().waiting_seconds))
            if event.type == "wait"
            else None
        )
    )
    await vt.run(caller.map(["x"], service))
    assert seen[0] == ("rate_limited", pytest.approx(10.0))
    assert caller.status().waiting_reason is None


# --- 再試行する・しない --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (StubError(403, "forbidden"), ErrorKind.PERMISSION),
        (UpstreamServiceError(404, "NotAuthorizedOrNotFound"), ErrorKind.PERMISSION),
        (StubError(404, "no such model"), ErrorKind.NOT_FOUND),
        (StubError(501), ErrorKind.NOT_SUPPORTED),
        (StubError(429, "insufficient_quota"), ErrorKind.QUOTA_EXHAUSTED),
    ],
    ids=["403", "permission-404", "404", "501", "quota"],
)
async def test_configuration_errors_stop_the_job_without_retrying(
    error: BaseException, kind: ErrorKind
) -> None:
    vt = VirtualTime()
    service = Service(vt)
    service.plan = {item: [error] for item in range(50)}
    caller = make_caller(vt, max_concurrency=2)
    with pytest.raises(BoundedCallAborted) as raised:
        await vt.run(caller.map(list(range(50)), service))
    assert raised.value.kind is kind
    assert "テストの段" in str(raised.value)
    # 同じ失敗を 50 件分くり返さない（並行の 2 件だけで止める）。
    assert len(service.starts) <= 2


async def test_tls_error_stops_immediately() -> None:
    vt = VirtualTime()
    service = Service(vt)

    def tls_error() -> BaseException:
        try:
            raise ssl.SSLCertVerificationError("certificate verify failed")
        except ssl.SSLError as inner:
            error = APIConnectionError("Connection error.")
            error.__cause__ = inner
            return error

    service.plan["x"] = [tls_error()]
    with pytest.raises(BoundedCallAborted) as raised:
        await vt.run(make_caller(vt).map(["x"], service))
    assert raised.value.kind is ErrorKind.TLS
    assert "証明書" in str(raised.value)


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (StubError(400, "bad"), ErrorKind.BAD_REQUEST),
        (StubError(413), ErrorKind.TOO_LARGE),
        (StubError(400, "content_filter"), ErrorKind.SAFETY),
    ],
    ids=["400", "413", "safety"],
)
async def test_item_errors_are_recorded_without_retry_and_others_continue(
    error: BaseException, kind: ErrorKind
) -> None:
    vt = VirtualTime()
    service = Service(vt)
    service.plan["bad"] = [error]
    outcomes = await vt.run(make_caller(vt).map(["a", "bad", "c"], service))
    assert [outcome.ok for outcome in outcomes] == [True, False, True]
    assert outcomes[1].error is not None and outcomes[1].error.kind is kind
    assert calls_of(service, "bad") == 1


async def test_too_large_and_truncated_retry_only_when_caller_can_shrink() -> None:
    vt = VirtualTime()
    service = Service(vt)
    service.plan["big"] = [StubError(413)]
    service.plan["cut"] = [EnterpriseAiIncompleteResponseError("max_output_tokens")]
    outcomes = await vt.run(make_caller(vt).map(["big", "cut"], service, adaptive=True))
    assert all(outcome.ok for outcome in outcomes)
    retried = [attempt for attempt in service.attempts if attempt.number == 2]
    assert {attempt.adaptation for attempt in retried} == {1}
    assert {attempt.last_error for attempt in retried} == {ErrorKind.TOO_LARGE, ErrorKind.TRUNCATED}


async def test_invalid_output_retries_at_most_twice() -> None:
    vt = VirtualTime()
    service = Service(vt)
    service.plan["x"] = [PictureValidationError("schema")] * 5
    outcomes = await vt.run(make_caller(vt).map(["x"], service))
    assert outcomes[0].error is not None and outcomes[0].error.kind is ErrorKind.INVALID_OUTPUT
    assert calls_of(service, "x") == 3


async def test_server_error_retries_twice_then_final_retry_succeeds_after_all_items() -> None:
    vt = VirtualTime()
    events: list[CallEvent] = []
    service = Service(vt)
    service.plan["x"] = [StubError(500)] * 3
    caller = make_caller(vt, events=events, final_retry_delay_seconds=30.0)
    outcomes = await vt.run(caller.map(["x", "a", "b", "c", "d", "e"], service))
    assert all(outcome.ok for outcome in outcomes)
    starts = [started for started, item in service.starts if item == "x"]
    assert len(starts) == 4  # 1 回 + 再試行 2 回 + 最後の再試行
    others_done = max(started for started, item in service.starts if item != "x") + 1.0
    assert starts[-1] >= others_done + 30.0 - 1e-9
    assert any(event.type == "final_retry" for event in events)


async def test_item_still_failing_after_final_retry_is_recorded_or_fails_the_job() -> None:
    vt = VirtualTime()
    service = Service(vt)
    service.plan["x"] = [StubError(500)] * 10
    outcomes = await vt.run(make_caller(vt).map(["x", "a"], service))
    assert outcomes[0].error is not None and outcomes[0].error.kind is ErrorKind.SERVER_ERROR
    assert outcomes[1].ok

    vt2 = VirtualTime()
    service2 = Service(vt2)
    service2.plan["x"] = [StubError(500)] * 10
    with pytest.raises(BoundedCallFailed):
        await vt2.run(make_caller(vt2, fail_job_on_item_error=True).map(["x", "a"], service2))


async def test_unavailable_network_and_timeout_retry_with_backoff() -> None:
    vt = VirtualTime()
    service = Service(vt)
    service.plan["502"] = [StubError(502)]
    service.plan["503"] = [StubError(503)]
    service.plan["504"] = [StubError(504)]
    service.plan["net"] = [ConnectionResetError("reset")]
    service.plan["timeout"] = [TimeoutError("read")]
    items = ["502", "503", "504", "net", "timeout"]
    outcomes = await vt.run(make_caller(vt, max_concurrency=5).map(items, service))
    assert all(outcome.ok for outcome in outcomes)
    assert all(calls_of(service, item) == 2 for item in items)


async def test_attempt_timeout_is_a_retryable_timeout() -> None:
    vt = VirtualTime()
    service = Service(vt, latency=10.0)
    caller = make_caller(vt, attempt_timeout_seconds=0.001)

    # wait_for は実時間で判定するため、仮想の時間ではなく実の待ちで確かめる（1 ミリ秒）。
    async def slow(_: Attempt) -> str:
        await asyncio.sleep(1)
        return "never"

    real = BoundedCaller(
        "t",
        BoundedCallPolicy(attempt_timeout_seconds=0.001, final_retry_delay_seconds=0.0),
        sleep=lambda _: asyncio.sleep(0),
    )
    with pytest.raises(BoundedCallFailed) as raised:
        await real.call(slow)
    assert raised.value.kind is ErrorKind.TIMEOUT
    assert raised.value.attempts == 1 + DEFAULT_KIND_RULES[ErrorKind.TIMEOUT].retries
    del caller, service


async def test_authentication_refreshes_credentials_once_then_stops() -> None:
    vt = VirtualTime()
    refreshed: list[float] = []

    def refresh() -> None:
        refreshed.append(vt.now)

    service = Service(vt)
    service.plan["ok-after-refresh"] = [StubError(401)]
    outcomes = await vt.run(make_caller(vt, refresh=refresh).map(["ok-after-refresh"], service))
    assert outcomes[0].ok and len(refreshed) == 1

    vt2 = VirtualTime()
    service2 = Service(vt2)
    service2.plan["x"] = [StubError(401), StubError(401)]
    with pytest.raises(BoundedCallAborted) as raised:
        await vt2.run(make_caller(vt2, refresh=lambda: None).map(["x"], service2))
    assert raised.value.kind is ErrorKind.AUTHENTICATION
    assert calls_of(service2, "x") == 2


async def test_non_idempotent_calls_are_not_retried() -> None:
    vt = VirtualTime()
    calls: list[int] = []

    async def write(_: Attempt) -> None:
        calls.append(1)
        raise StubError(503)

    with pytest.raises(BoundedCallFailed):
        await vt.run(make_caller(vt).call(write, idempotent=False))
    assert len(calls) == 1


# --- circuit breaker・予算・期限 -----------------------------------------------------------


async def test_circuit_breaker_opens_after_consecutive_failures_and_stops_job() -> None:
    vt = VirtualTime()
    events: list[CallEvent] = []
    service = Service(vt)
    service.plan = {item: [StubError(503)] * 100 for item in range(40)}
    caller = make_caller(
        vt,
        events=events,
        max_concurrency=2,
        breaker_threshold=10,
        breaker_cooldown_seconds=30.0,
        breaker_probe_attempts=2,
        retry_budget_initial=1000.0,
    )
    with pytest.raises(BoundedCallAborted) as raised:
        await vt.run(caller.map(list(range(40)), service))
    assert raised.value.kind is ErrorKind.UNAVAILABLE
    assert "回復しませんでした" in str(raised.value)
    breaker = [event.reason for event in events if event.type == "breaker"]
    assert breaker[0] == "consecutive_failures"
    assert "half_open" in breaker and "probe_failed" in breaker
    # 40 件 × 5 回（= 200 回）呼ばず、breaker の後は試しの 1 件だけ。
    assert len(service.starts) < 30


async def test_circuit_breaker_closes_when_probe_succeeds() -> None:
    vt = VirtualTime()
    events: list[CallEvent] = []
    service = Service(vt)
    for item in range(6):
        service.plan[item] = [StubError(503)]
    caller = make_caller(
        vt,
        events=events,
        max_concurrency=3,
        breaker_threshold=4,
        breaker_cooldown_seconds=30.0,
        retry_budget_initial=1000.0,
    )
    outcomes = await vt.run(caller.map(list(range(6)), service))
    assert all(outcome.ok for outcome in outcomes)
    breaker = [event.reason for event in events if event.type == "breaker"]
    assert breaker[0] == "consecutive_failures" and "closed" in breaker
    assert caller.status().breaker == "closed"


async def test_retry_budget_limits_total_retries_and_opens_the_breaker() -> None:
    vt = VirtualTime()
    events: list[CallEvent] = []
    service = Service(vt)
    # 最初の 10 件（並行の 1 回目）だけが失敗する短い障害。
    service.plan = {item: [StubError(502)] for item in range(10)}
    caller = make_caller(
        vt,
        events=events,
        max_concurrency=10,
        retry_budget_initial=3.0,
        retry_budget_ratio=0.0,
        breaker_threshold=1000,
    )
    outcomes = await vt.run(caller.map(list(range(30)), service))
    # 予算の 3 件だけすぐ再試行し、残りは最後の再試行で救う（件を失わない）。
    assert all(outcome.ok for outcome in outcomes)
    assert caller.retries == 3
    assert "retry_budget" in [event.reason for event in events if event.type == "breaker"]


async def test_deadline_stops_further_retries() -> None:
    vt = VirtualTime()
    service = Service(vt, latency=50.0)
    service.plan["x"] = [StubError(503)] * 10
    caller = make_caller(vt, deadline_seconds=60.0)
    outcomes = await vt.run(caller.map(["x"], service))
    assert outcomes[0].error is not None
    assert calls_of(service, "x") <= 3


async def test_abort_cancels_remaining_calls_in_map() -> None:
    vt = VirtualTime()
    service = Service(vt)
    service.plan[3] = [StubError(403)]
    caller = make_caller(vt, max_concurrency=1)
    with pytest.raises(BoundedCallAborted):
        await vt.run(caller.map(list(range(10)), service))
    assert [item for _, item in service.starts] == [0, 1, 2, 3]
    with pytest.raises(BoundedCallAborted):
        await vt.run(caller.call(lambda attempt: service("late", attempt)))


async def test_listener_failure_does_not_stop_calls() -> None:
    vt = VirtualTime()
    service = Service(vt)
    service.plan["x"] = [StubError(503)]

    def broken(_: CallEvent) -> None:
        raise RuntimeError("listener")

    caller = make_caller(vt)
    caller.add_listener(broken)
    outcomes = await vt.run(caller.map(["x"], service))
    assert outcomes[0].ok


# --- 同期のコードからの橋（#2004） --------------------------------------------------------


async def _no_wait(_seconds: float) -> None:
    await asyncio.sleep(0)


class SyncService:
    """thread から呼ぶ同期のスタブ。同時に動いた数と、件ごとの失敗の予定を持つ。"""

    def __init__(self, plan: dict[Any, list[Exception]] | None = None) -> None:
        self.plan = plan or {}
        self.lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0
        self.calls: list[Any] = []

    def __call__(self, item: Any, _attempt: Attempt) -> str:
        with self.lock:
            self.calls.append(item)
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            errors = self.plan.get(item)
            error = errors.pop(0) if errors else None
        try:
            time.sleep(0.01)
            if error is not None:
                raise error
            return f"{item}:{REQUEST_ID.get()}"
        finally:
            with self.lock:
                self.in_flight -= 1


REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def _sync_caller(**policy: Any) -> BoundedCaller:
    return BoundedCaller("sync", BoundedCallPolicy(**policy), sleep=_no_wait, random=lambda: 0.5)


def test_sync_caller_map_keeps_order_limit_and_context() -> None:
    service = SyncService()
    REQUEST_ID.set("req-1")
    with SyncBoundedCaller(_sync_caller(max_concurrency=3)) as bridge:
        outcomes = bridge.map(list(range(8)), service)
    assert [outcome.value for outcome in outcomes] == [f"{item}:req-1" for item in range(8)]
    assert service.peak == 3


def test_sync_caller_shares_limit_and_rate_limit_across_threads() -> None:
    """thread ごとの入れ子の呼び出しも 1 つの実行器を通り、429 で全体の並行の数が下がる。"""
    service = SyncService({0: [StubError(429)], 1: [StubError(429)]})
    caller = _sync_caller(max_concurrency=4)
    with SyncBoundedCaller(caller) as bridge, ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(bridge.call, functools.partial(service, item), label=str(item))
            for item in range(8)
        ]
        values = [future.result() for future in futures]
    assert values == [f"{item}:-" for item in range(8)]
    assert service.peak <= 4
    assert caller.retries == 2
    assert caller.status().concurrency < 4


def test_sync_caller_permission_error_aborts_every_thread() -> None:
    service = SyncService({0: [StubError(403)]})
    with SyncBoundedCaller(_sync_caller(max_concurrency=1)) as bridge:
        with pytest.raises(BoundedCallAborted):
            bridge.map(list(range(4)), service)
        with pytest.raises(BoundedCallAborted):
            bridge.call(functools.partial(service, "late"))
    assert service.calls == [0]


def test_sync_caller_requires_with() -> None:
    bridge = SyncBoundedCaller(_sync_caller())
    with pytest.raises(RuntimeError):
        bridge.call(lambda _attempt: 1)
