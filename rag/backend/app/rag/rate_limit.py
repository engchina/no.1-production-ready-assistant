"""高コスト API を保護する軽量 rate limit。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from math import ceil
from threading import Lock
from time import perf_counter
from typing import Literal

from fastapi import HTTPException, Request

from app.config import Settings, get_settings
from app.rag.observability import record_rate_limit_decision
from app.rag.request_context import current_audit_request_context

RateLimitScope = Literal["search", "evaluation", "upload", "ingest"]

RATE_LIMIT_MESSAGE = "リクエスト数が上限を超えました。しばらく待ってから再度お試しください。"


@dataclass(frozen=True)
class RateLimitDecision:
    """rate limit 判定結果。"""

    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


@dataclass
class _WindowCounter:
    count: int
    reset_at: float


class FixedWindowRateLimiter:
    """process 内で動く fixed-window limiter。

    本番では API Gateway / Ingress / Redis などの共有 limiter と併用または置換する。
    local/CI では外部依存なしで高コスト API の契約を検証できる。
    """

    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str], _WindowCounter] = {}
        self._lock = Lock()

    def check(
        self,
        *,
        scope: RateLimitScope,
        subject: str,
        limit: int,
        window_seconds: float,
    ) -> RateLimitDecision:
        """scope + subject 単位の使用量を加算し、許可可否を返す。"""
        now = perf_counter()
        key = (scope, subject)
        with self._lock:
            self._prune_expired(now)
            counter = self._buckets.get(key)
            if counter is None or now >= counter.reset_at:
                counter = _WindowCounter(count=0, reset_at=now + window_seconds)
                self._buckets[key] = counter

            retry_after = max(1, ceil(counter.reset_at - now))
            if counter.count >= limit:
                return RateLimitDecision(
                    allowed=False,
                    limit=limit,
                    remaining=0,
                    retry_after_seconds=retry_after,
                )

            counter.count += 1
            return RateLimitDecision(
                allowed=True,
                limit=limit,
                remaining=max(0, limit - counter.count),
                retry_after_seconds=retry_after,
            )

    def reset(self) -> None:
        """テスト用に全 bucket を削除する。"""
        with self._lock:
            self._buckets.clear()

    def _prune_expired(self, now: float) -> None:
        if len(self._buckets) < 10000:
            return
        expired = [key for key, counter in self._buckets.items() if now >= counter.reset_at]
        for key in expired:
            del self._buckets[key]


_LIMITER = FixedWindowRateLimiter()


def enforce_rate_limit(
    scope: RateLimitScope,
    request: Request,
    *,
    settings: Settings | None = None,
) -> None:
    """設定に基づき高コスト API の rate limit を適用する。"""
    resolved_settings = settings or get_settings()
    if not resolved_settings.rate_limit_enabled:
        return

    limit = _limit_for_scope(scope, resolved_settings)
    decision = _LIMITER.check(
        scope=scope,
        subject=_subject_fingerprint(request, resolved_settings),
        limit=limit,
        window_seconds=resolved_settings.rate_limit_window_seconds,
    )
    record_rate_limit_decision(scope, "allowed" if decision.allowed else "blocked")
    if decision.allowed:
        return

    raise HTTPException(
        status_code=429,
        detail=RATE_LIMIT_MESSAGE,
        headers={
            "Retry-After": str(decision.retry_after_seconds),
            "X-RateLimit-Limit": str(decision.limit),
            "X-RateLimit-Remaining": str(decision.remaining),
            "X-RateLimit-Reset-After": str(decision.retry_after_seconds),
        },
    )


def reset_rate_limiter() -> None:
    """テスト用に limiter 状態を初期化する。"""
    _LIMITER.reset()


def _limit_for_scope(scope: RateLimitScope, settings: Settings) -> int:
    match scope:
        case "search":
            return settings.rate_limit_search_requests
        case "evaluation":
            return settings.rate_limit_evaluation_runs
        case "upload":
            return settings.rate_limit_uploads
        case "ingest":
            return settings.rate_limit_ingest_requests


def _subject_fingerprint(request: Request, settings: Settings) -> str:
    context = current_audit_request_context()
    if context.tenant_id_hash and context.user_id_hash:
        return f"tenant-user:{context.tenant_id_hash}:{context.user_id_hash}"
    if context.tenant_id_hash:
        return f"tenant:{context.tenant_id_hash}"
    if context.user_id_hash:
        return f"user:{context.user_id_hash}"

    client_host = request.client.host if request.client is not None else "anonymous"
    salt = settings.audit_context_hash_salt
    payload = f"{salt}\0{client_host}" if salt else client_host
    return f"client:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"
