"""回答の最終の検証の時間の上限・組の並行・専用の executor（#2700）。監査（モデル）はスタブ。"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import pytest
from pytest import MonkeyPatch

from app.config import Settings
from app.rag import answer_threads
from app.rag import answer_validation as validation_module
from app.rag.answer_validation import EvidenceRef, validate_answer
from app.schemas.search import RetrievedChunk


class _Oracle:
    async def retrievable_chunk(self, document_id: str, chunk_id: str) -> Any:
        return RetrievedChunk(
            document_id=document_id,
            chunk_id=chunk_id,
            text="有効期限は最長 30 日です。",
            score=0.5,
            file_name="manual.pdf",
            metadata={"page_start": 1, "page_end": 1},
        )

    async def accessible_chunk_exists(self, document_id: str, chunk_id: str) -> bool:
        return False


def _engine(monkeypatch: MonkeyPatch, fake: Any) -> None:
    import rag_engine.evaluation.answer_validation as engine_validation

    monkeypatch.setattr(engine_validation, "validate_answer_claims", fake)


def _supported(answer: str) -> dict[str, Any]:
    return {
        "status": "completed",
        "counts": {"supported": 1},
        "claim_checks": [
            {
                "answer_quote": answer,
                "status": "supported",
                "source_id": "c1",
                "reason": "根拠に記載",
            }
        ],
        "evidence_truncated": False,
    }


async def test_settings_reach_the_engine_with_a_deadline(monkeypatch: MonkeyPatch) -> None:
    """組の段落の数・並行の数を渡し、監査の thread には上限の残りの期限を置く。"""
    from rag_engine.resources.deadline import current_deadline

    captured: dict[str, Any] = {}

    def fake(question: str, answer: str, items: Any, settings: Any, **options: Any) -> Any:
        captured.update(options)
        deadline = current_deadline()
        captured["remaining"] = deadline.remaining() if deadline is not None else None
        captured["thread"] = threading.current_thread().name
        return _supported(answer)

    _engine(monkeypatch, fake)
    settings = Settings(
        rag_answer_validation_timeout_seconds=120,
        rag_answer_validation_batch_passages=3,
        rag_answer_validation_concurrency=2,
    )
    result = await validate_answer(
        "q",
        "有効期限は 30 日です。",
        [EvidenceRef("d1", "c1")],
        settings,
        oracle=cast(Any, _Oracle()),
    )
    assert result.status == "completed" and result.valid is True
    assert (captured["batch_passages"], captured["max_concurrency"]) == (3, 2)
    # thread の期限は上限（と猶予）以下。
    assert 0 < captured["remaining"] <= 120 + answer_threads.ANSWER_DEADLINE_GRACE_SECONDS
    assert captured["thread"].startswith("rag-validate")


async def test_validation_does_not_wait_for_a_busy_answer_executor(
    monkeypatch: MonkeyPatch,
) -> None:
    """回答フロー（品質評価・多数の検索の回答）で回答の executor が埋まっていても、
    検証は待たない。"""
    release = threading.Event()
    busy = ThreadPoolExecutor(max_workers=1, thread_name_prefix="busy-answer")
    busy.submit(release.wait, 30)
    monkeypatch.setattr(answer_threads, "_executor", busy)
    _engine(monkeypatch, lambda q, answer, items, settings, **_: _supported(answer))
    try:
        result = await validate_answer(
            "q",
            "有効期限は 30 日です。",
            [EvidenceRef("d1", "c1")],
            Settings(),
            oracle=cast(Any, _Oracle()),
        )
    finally:
        release.set()
        busy.shutdown(wait=True)
    assert result.status == "completed"


async def test_partial_results_keep_the_judged_claims(monkeypatch: MonkeyPatch) -> None:
    """上限までに監査し終わらなかった段落は数え、確かめ終わった段落の判定は返す
    （valid にはしない）。"""

    def fake(question: str, answer: str, items: Any, settings: Any, **options: Any) -> Any:
        return {
            "status": "partial",
            "counts": {"supported": 1, "unassessed": 1},
            "claim_checks": [
                {
                    "answer_quote": "A",
                    "status": "supported",
                    "source_id": "c1",
                    "reason": "根拠に記載",
                },
                {
                    "answer_quote": "B",
                    "status": "unassessed",
                    "source_id": "",
                    "reason": "時間切れ",
                },
            ],
            "timed_out_passage_ids": ["p2"],
        }

    _engine(monkeypatch, fake)
    result = await validate_answer(
        "q", "A\n\nB", [EvidenceRef("d1", "c1")], Settings(), oracle=cast(Any, _Oracle())
    )
    assert (result.status, result.valid, result.timed_out_claims) == ("partial", False, 1)
    assert [claim["status"] for claim in result.claim_checks] == ["supported", "unassessed"]


async def test_validation_returns_timed_out_by_the_limit(
    monkeypatch: MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """監査が上限までに返らなければ、上限で timed_out を返し（呼び出し側の待ち時間より
    前）、thread の期限を切る。"""
    from rag_engine.resources.deadline import current_deadline

    seen: dict[str, Any] = {}
    stop = threading.Event()

    def fake(question: str, answer: str, items: Any, settings: Any, **options: Any) -> Any:
        deadline = current_deadline()
        seen["deadline"] = deadline
        # 期限が切れる（待つ側が抜けて取り消す）まで返らない呼び出し。
        while deadline is not None and not deadline.cancelled and not stop.is_set():
            stop.wait(0.01)
        return _supported(answer)

    _engine(monkeypatch, fake)
    monkeypatch.setattr(validation_module, "ANSWER_DEADLINE_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(validation_module, "VALIDATION_FINALIZE_SECONDS", 0.0)
    caplog.set_level(logging.INFO, logger="app.rag.answer_validation")
    try:
        result = await validate_answer(
            "q",
            "有効期限は 30 日です。",
            [EvidenceRef("d1", "c1")],
            Settings(rag_answer_validation_timeout_seconds=0.05),
            oracle=cast(Any, _Oracle()),
        )
    finally:
        stop.set()
    assert (result.status, result.valid) == ("timed_out", False)
    # 待つ側が抜けたら thread の期限を取り消す（以後 LLM を呼ばない）。
    assert seen["deadline"].cancelled
    record = next(item for item in caplog.records if item.getMessage() == "rag_answer_validation")
    assert cast(Any, record).validation_status == "timed_out"
    assert cast(Any, record).validation_limit_seconds == 0.05


def test_validation_timeout_is_shorter_than_the_agent_wait() -> None:
    """既定の上限は、Agent の接続 rag の既定の待ち時間（630 秒）より短い。"""
    assert Settings().rag_answer_validation_timeout_seconds < 630
