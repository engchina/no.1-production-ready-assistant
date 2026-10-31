"""HTTP サービス呼び出しの指数退避 retry 共通処理。"""

from __future__ import annotations

import asyncio as asyncio
import logging
import time as time
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class HttpRetryConfig:
    """HTTP service retry の最小設定。"""

    attempts: int = 5
    initial_delay_seconds: float = 0.5
    max_delay_seconds: float = 4.0


def retry_config_from_settings(settings: object) -> HttpRetryConfig:
    """Settings から全 HTTP service 共通 retry 設定を作る。"""
    return HttpRetryConfig(
        attempts=max(1, int(getattr(settings, "rag_http_service_retry_attempts", 5))),
        initial_delay_seconds=max(
            0.0,
            float(getattr(settings, "rag_http_service_retry_initial_delay_seconds", 0.5)),
        ),
        max_delay_seconds=max(
            0.0,
            float(getattr(settings, "rag_http_service_retry_max_delay_seconds", 4.0)),
        ),
    )


def request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    retry: HttpRetryConfig,
    logger: logging.Logger,
    log_extra: dict[str, object] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """同期 httpx request を retryable failure だけ指数退避で再試行する。"""
    attempts = max(1, retry.attempts)
    for attempt in range(1, attempts + 1):
        try:
            response = client.request(method, url, **kwargs)
        except httpx.InvalidURL:
            raise
        except httpx.TimeoutException as exc:
            if attempt < attempts:
                _sleep_before_retry(
                    retry,
                    logger=logger,
                    log_extra=log_extra,
                    attempt=attempt,
                    error=str(exc) or exc.__class__.__name__,
                )
                continue
            raise
        except httpx.HTTPError as exc:
            if attempt < attempts:
                _sleep_before_retry(
                    retry,
                    logger=logger,
                    log_extra=log_extra,
                    attempt=attempt,
                    error=str(exc) or exc.__class__.__name__,
                )
                continue
            raise
        if _is_retryable_status(response.status_code) and attempt < attempts:
            _sleep_before_retry(
                retry,
                logger=logger,
                log_extra=log_extra,
                attempt=attempt,
                error=f"HTTP {response.status_code}",
            )
            continue
        return response
    raise RuntimeError("unreachable HTTP retry loop")


async def async_request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    retry: HttpRetryConfig,
    logger: logging.Logger,
    log_extra: dict[str, object] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """非同期 httpx request を retryable failure だけ指数退避で再試行する。"""
    attempts = max(1, retry.attempts)
    for attempt in range(1, attempts + 1):
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.InvalidURL:
            raise
        except httpx.TimeoutException as exc:
            if attempt < attempts:
                await _async_sleep_before_retry(
                    retry,
                    logger=logger,
                    log_extra=log_extra,
                    attempt=attempt,
                    error=str(exc) or exc.__class__.__name__,
                )
                continue
            raise
        except httpx.HTTPError as exc:
            if attempt < attempts:
                await _async_sleep_before_retry(
                    retry,
                    logger=logger,
                    log_extra=log_extra,
                    attempt=attempt,
                    error=str(exc) or exc.__class__.__name__,
                )
                continue
            raise
        if _is_retryable_status(response.status_code) and attempt < attempts:
            await _async_sleep_before_retry(
                retry,
                logger=logger,
                log_extra=log_extra,
                attempt=attempt,
                error=f"HTTP {response.status_code}",
            )
            continue
        return response
    raise RuntimeError("unreachable async HTTP retry loop")


def retry_delay(attempt: int, *, initial_delay: float, max_delay: float) -> float:
    """1 始まり attempt 番号から指数退避の待機秒数を返す。"""
    if initial_delay <= 0 or max_delay <= 0:
        return 0.0
    exponent = max(0, attempt - 1)
    return min(max_delay, initial_delay * float(2**exponent))


def _is_retryable_status(status_code: int) -> bool:
    return status_code in {408, 409, 425, 429} or 500 <= status_code <= 599


def _sleep_before_retry(
    retry: HttpRetryConfig,
    *,
    logger: logging.Logger,
    log_extra: dict[str, object] | None,
    attempt: int,
    error: str,
) -> None:
    delay = retry_delay(
        attempt,
        initial_delay=retry.initial_delay_seconds,
        max_delay=retry.max_delay_seconds,
    )
    _log_retry(
        logger,
        retry=retry,
        log_extra=log_extra,
        attempt=attempt,
        delay=delay,
        error=error,
    )
    if delay > 0:
        time.sleep(delay)


async def _async_sleep_before_retry(
    retry: HttpRetryConfig,
    *,
    logger: logging.Logger,
    log_extra: dict[str, object] | None,
    attempt: int,
    error: str,
) -> None:
    delay = retry_delay(
        attempt,
        initial_delay=retry.initial_delay_seconds,
        max_delay=retry.max_delay_seconds,
    )
    _log_retry(
        logger,
        retry=retry,
        log_extra=log_extra,
        attempt=attempt,
        delay=delay,
        error=error,
    )
    if delay > 0:
        await asyncio.sleep(delay)


def _log_retry(
    logger: logging.Logger,
    *,
    retry: HttpRetryConfig,
    log_extra: dict[str, object] | None,
    attempt: int,
    delay: float,
    error: str,
) -> None:
    extra = {
        **(log_extra or {}),
        "attempt": attempt,
        "max_attempts": retry.attempts,
        "delay_seconds": delay,
        "error": error,
    }
    logger.info("HTTP service call retrying", extra=extra)
