"""rerank の論理要求と実際の SDK 呼び出しを区別する、本文を持たない回収票。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from rag_engine.retrieval.query_identity import normalized_query_key


@dataclass
class RerankCallReceipt:
    phase: str = "other"
    batch_id: str = ""
    query_index: int = 0
    query_role: str = "original"
    backend_observed: bool = False
    candidate_count: int = 0
    candidate_hash: str = ""
    normalized_query_hash: str = ""
    model: str = ""
    top_n: int = 0
    cache_state: str = "unknown"
    status: str = "running"
    error_type: str = ""
    sdk_attempts: list[dict[str, Any]] = field(default_factory=list)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def request(self, query: str, documents: Sequence[str], *, model: str, top_n: int) -> None:
        self.describe_inputs(query, documents, model=model, top_n=top_n)
        with self._lock:
            self.backend_observed = True

    def describe_inputs(self, query: str, documents: Sequence[str], *, top_n: int, model: str = "") -> None:
        with self._lock:
            self.candidate_count = len(documents)
            self.candidate_hash = hashlib.sha256(json.dumps(list(documents), ensure_ascii=False).encode()).hexdigest()
            self.normalized_query_hash = hashlib.sha256(normalized_query_key(query).encode()).hexdigest()
            self.model, self.top_n = model, top_n

    def begin_sdk(self, number: int) -> dict[str, Any]:
        attempt: dict[str, Any] = {"attempt_number": number, "retry_number": max(0, number - 1), "status": "running"}
        with self._lock:
            self.sdk_attempts.append(attempt)
        return attempt

    def finish_sdk(self, attempt: dict[str, Any], status: str, error: BaseException | None = None) -> None:
        with self._lock:
            attempt["status"] = status
            if error is not None:
                attempt["error_type"] = type(error).__name__

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "phase": self.phase,
                "batch_id": self.batch_id,
                "question_index": self.query_index,
                "query_role": self.query_role,
                "candidate_count": self.candidate_count,
                "candidate_hash": self.candidate_hash,
                "normalized_query_hash": self.normalized_query_hash,
                "model": self.model,
                "top_n": self.top_n,
                "cache_state": self.cache_state,
                "status": self.status,
                "backend_observed": self.backend_observed,
                "sdk_attempt_count": len(self.sdk_attempts) if self.backend_observed else None,
                # SDK の開始は数えられるが、HTTP送信の完了やサーバー到着は推測しない。
                "http_attempt_count": None,
                "sdk_attempts": [dict(attempt) for attempt in self.sdk_attempts],
                **({"error_type": self.error_type} if self.error_type else {}),
            }

    def attributes(self) -> dict[str, object]:
        return {"rerank." + key: value for key, value in self.to_dict().items() if isinstance(value, (str, int, bool))}


_CURRENT: ContextVar[RerankCallReceipt | None] = ContextVar("rerank_call_receipt", default=None)
_ATTEMPT: ContextVar[int] = ContextVar("rerank_sdk_attempt", default=1)


def current_rerank_receipt() -> RerankCallReceipt | None:
    return _CURRENT.get()


def current_rerank_attempt() -> int:
    return _ATTEMPT.get()


@contextmanager
def use_rerank_receipt(receipt: RerankCallReceipt) -> Iterator[None]:
    token = _CURRENT.set(receipt)
    try:
        yield
    finally:
        _CURRENT.reset(token)


@contextmanager
def bind_rerank_context(*, phase: str, batch_id: str, query_index: int, query_role: str) -> Iterator[RerankCallReceipt]:
    receipt = RerankCallReceipt(phase=phase, batch_id=batch_id, query_index=query_index, query_role=query_role)
    token = _CURRENT.set(receipt)
    try:
        yield receipt
    except BaseException as exc:
        if receipt.status == "running":
            receipt.status = "cancelled" if type(exc).__name__ == "CancelledError" else "error"
            receipt.error_type = type(exc).__name__
        raise
    else:
        if receipt.status == "running":
            receipt.status = "completed"
    finally:
        _CURRENT.reset(token)


@contextmanager
def bind_rerank_attempt(number: int) -> Iterator[None]:
    token = _ATTEMPT.set(number)
    try:
        yield
    finally:
        _ATTEMPT.reset(token)
