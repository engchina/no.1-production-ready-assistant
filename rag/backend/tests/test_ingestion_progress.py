"""取込 job の抽出の段(解析・Vision)の進捗の記録のテスト(#1999)。

通しジョブ内で実行する抽出は、解析サービスが原本全体を 1 回で解析し、その後に図・表の説明を
行う。画面が「抽出: 0 / N ページ」のまま止まって見えないよう、worker が段の進捗を job の行へ
書く。ここでは記録・解析サービスの進捗の読み取り・segment の状態を確かめる(実際の Docling・
VLM は使わない)。
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from app.clients.parser_service import ParserServiceClient, parse_progress_id
from app.config import Settings
from app.rag import ingestion as ingestion_module
from app.rag.ingestion import IngestionPipeline
from app.rag.ingestion_progress import (
    IngestionProgressReporter,
    active_ingestion_progress,
    parse_progress_from_service,
    track_ingestion_progress,
    vision_progress_from_ledger,
)
from app.schemas.document import (
    IngestionJobProgress,
    IngestionParseProgress,
    IngestionSegment,
    IngestionVisionProgress,
)
from tests.fixtures.synthetic_pdf import text_pdf


class _Writer:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[IngestionJobProgress | None] = []
        self.fail = fail

    async def __call__(self, progress: IngestionJobProgress | None) -> None:
        if self.fail:
            raise RuntimeError("db down")
        self.calls.append(progress)


async def test_reporter_writes_only_changed_progress() -> None:
    writer = _Writer()
    reporter = IngestionProgressReporter(writer)

    await reporter.flush()
    reporter.update_parse(IngestionParseProgress(engine="docling", completed=10, total=82))
    await reporter.flush()
    reporter.update_parse(IngestionParseProgress(engine="docling", completed=10, total=82))
    await reporter.flush()

    assert len(writer.calls) == 1
    written = writer.calls[0]
    assert written is not None
    assert written.stage == "parse"
    assert written.parse == IngestionParseProgress(engine="docling", completed=10, total=82)


async def test_reporter_retries_after_write_failure() -> None:
    writer = _Writer(fail=True)
    reporter = IngestionProgressReporter(writer)
    reporter.update_parse(IngestionParseProgress(total=5))

    await reporter.flush()
    writer.fail = False
    await reporter.flush()

    assert len(writer.calls) == 1


def test_finish_parse_fills_total_from_result_and_keeps_vision_stage() -> None:
    reporter = IngestionProgressReporter(_Writer())
    reporter.update_parse(IngestionParseProgress())

    reporter.finish_parse(engine="docling", page_count=82)
    parsed = reporter.snapshot()
    reporter.update_vision(IngestionVisionProgress(phase="pictures", total_known=4))
    # Vision の段に進んだ後に解析の進捗が届いても、段は戻さない。
    reporter.update_parse(IngestionParseProgress(engine="docling", completed=82, total=82))
    reporter.finish_vision()
    final = reporter.snapshot()

    assert parsed is not None
    assert parsed.parse == IngestionParseProgress(
        engine="docling", completed=82, total=82, finished=True
    )
    assert final is not None
    assert final.stage == "vision"
    assert final.vision is not None
    assert final.vision.finished is True
    assert final.vision.total_known == 4


def test_finish_parse_keeps_failed_pages() -> None:
    reporter = IngestionProgressReporter(_Writer())
    reporter.update_parse(IngestionParseProgress(completed=80, failed=2, total=82))

    reporter.finish_parse(engine="docling", page_count=82)
    snapshot = reporter.snapshot()

    assert snapshot is not None
    assert snapshot.parse == IngestionParseProgress(
        engine="docling", completed=80, failed=2, total=82, finished=True
    )


async def test_track_resets_previous_progress_and_flushes_periodically() -> None:
    writer = _Writer()

    async with track_ingestion_progress(writer, interval_seconds=0.01) as reporter:
        assert active_ingestion_progress() is reporter
        reporter.update_parse(IngestionParseProgress(engine="docling", completed=3, total=9))
        for _ in range(200):
            if len(writer.calls) >= 2:
                break
            await asyncio.sleep(0.01)
        # thread(解析・Vision の段)からも同じ記録が見える。
        seen = await asyncio.to_thread(active_ingestion_progress)

    assert seen is reporter
    assert writer.calls[0] is None
    assert writer.calls[1] is not None
    assert writer.calls[1].parse == IngestionParseProgress(engine="docling", completed=3, total=9)
    assert active_ingestion_progress() is None


async def test_track_keeps_ingestion_running_when_reset_fails() -> None:
    async with track_ingestion_progress(_Writer(fail=True), interval_seconds=60) as reporter:
        reporter.update_parse(IngestionParseProgress())

    assert active_ingestion_progress() is None


def test_parse_progress_from_docling_payload() -> None:
    running = parse_progress_from_service(
        {
            "engine": "docling",
            "stage": "parsing_pages",
            "total_pages": 82,
            "completed_pages": 40,
            "failed_pages": 1,
        }
    )
    completed = parse_progress_from_service(
        {
            "engine": "docling",
            "stage": "docling_completed",
            "total_pages": 82,
            "completed_pages": 82,
        }
    )

    assert running == IngestionParseProgress(engine="docling", completed=40, failed=1, total=82)
    assert completed is not None
    assert completed.finished is True
    # ページの描画中(progress.json がまだ無い)は進捗にしない。
    assert parse_progress_from_service({"engine": "docling", "stage": "preparing_pages"}) is None
    assert parse_progress_from_service({"total_pages": "82", "completed_pages": -1}) is None


def test_vision_progress_from_ledger_payload() -> None:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "phase": "pictures",
        "total_known": 12,
        "total_final": False,
        "counts": {"pending": 7, "running": 1, "succeeded": 3, "failed": 1, "skipped": 0},
        "current": {"record_id": "docling-p5-2", "page": 5, "kind": "Picture"},
    }

    vision = vision_progress_from_ledger(payload)
    completed = vision_progress_from_ledger(
        {**payload, "phase": "completed_with_errors", "total_final": True, "current": None}
    )

    assert vision == IngestionVisionProgress(
        phase="pictures",
        succeeded=3,
        failed=1,
        running=1,
        pending=7,
        total_known=12,
        current_page=5,
        current_kind="picture",
    )
    assert completed.finished is True
    assert completed.total_final is True
    assert completed.current_page is None


class _ProgressParserService:
    def __init__(self, payloads: list[dict[str, Any] | None]) -> None:
        self.payloads = payloads
        self.ids: list[str] = []

    def fetch_progress(self, progress_id: str) -> dict[str, Any] | None:
        self.ids.append(progress_id)
        return self.payloads.pop(0) if self.payloads else None


async def test_poll_parse_progress_copies_service_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ingestion_module, "_PARSE_PROGRESS_POLL_SECONDS", 0.01)
    pipeline = IngestionPipeline(settings=Settings())
    service = _ProgressParserService(
        [
            None,
            {"engine": "docling", "stage": "preparing_pages"},
            {
                "engine": "docling",
                "stage": "parsing_pages",
                "total_pages": 82,
                "completed_pages": 9,
            },
        ]
    )
    pipeline._parser_service = service  # type: ignore[assignment]
    reporter = IngestionProgressReporter(_Writer())

    # 原本全体の解析(``_partition_whole_source``)は、呼び出しの前に解析の段の始まりを記録する。
    reporter.update_parse(IngestionParseProgress())
    task = asyncio.create_task(pipeline._poll_parse_progress("a" * 32, reporter.update_parse))
    started = None
    for _ in range(300):
        snapshot = reporter.snapshot()
        started = started or snapshot
        if snapshot is not None and snapshot.parse is not None and snapshot.parse.completed == 9:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # 解析サービスが進捗を返す前から、解析の段(ページ数は未確定)を出す。
    assert started is not None
    assert started.stage == "parse"
    final = reporter.snapshot()
    assert final is not None
    assert final.parse == IngestionParseProgress(engine="docling", completed=9, total=82)
    assert set(service.ids) == {"a" * 32}


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler: httpx.MockTransport) -> None:
    original = httpx.Client

    def factory(*args: object, **kwargs: object) -> httpx.Client:
        return original(transport=handler)

    monkeypatch.setattr(httpx, "Client", factory)


def test_parser_service_sends_progress_id_and_reads_progress_while_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """解析の呼び出しの間だけ、同じ id で解析サービスの進捗を読める(#1999)。"""
    client = ParserServiceClient(Settings(rag_parser_docling_service_url="http://127.0.0.1:18020"))
    seen: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/parse/progress/"):
            seen["progress_path"] = request.url.path
            return httpx.Response(200, json={"engine": "docling", "total_pages": 3})
        seen["form"] = request.content
        # 解析の途中で進捗を読む(worker の polling と同じ)。
        seen["during"] = client.fetch_progress("progress-0001")
        return httpx.Response(
            200,
            json={
                "extraction": {"raw_text": "本文"},
                "parser_backend": "docling",
                "parser_version": "docling",
                "template": "docling_layout",
            },
        )

    _install_transport(monkeypatch, httpx.MockTransport(handle))
    with parse_progress_id("progress-0001"):
        result = client.runner("docling", b"%PDF", None, "application/pdf")

    assert result.extraction is not None
    form = seen["form"]
    assert isinstance(form, bytes)
    assert b'name="progress_id"' in form
    assert seen["during"] == {"engine": "docling", "total_pages": 3}
    assert seen["progress_path"] == "/parse/progress/progress-0001"
    # 解析の後は読まない(id を忘れる)。
    assert client.fetch_progress("progress-0001") is None


def test_parser_service_progress_ignores_missing_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """進捗を返さない旧版の解析サービス(404)・接続の失敗は None にして解析を続ける。"""
    client = ParserServiceClient(Settings(rag_parser_docling_service_url="http://127.0.0.1:18020"))
    client._progress_urls["p-404"] = "http://127.0.0.1:18020"
    client._progress_urls["p-down"] = "http://127.0.0.1:18020"

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/p-down"):
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(404, json={"detail": "progress not found"})

    _install_transport(monkeypatch, httpx.MockTransport(handle))

    assert client.fetch_progress("p-404") is None
    assert client.fetch_progress("p-down") is None
    assert client.fetch_progress("unknown") is None


class _SegmentOracle:
    def __init__(self) -> None:
        self.replaced: list[IngestionSegment] = []
        self.updates: list[dict[str, object]] = []
        self.existing: list[IngestionSegment] = []

    async def list_ingestion_segments(self, document_id: str) -> list[IngestionSegment]:
        return list(self.existing)

    async def replace_ingestion_segments(
        self, document_id: str, segments: list[IngestionSegment]
    ) -> list[IngestionSegment]:
        self.replaced = list(segments)
        return self.replaced

    async def update_ingestion_segment(self, segment_id: str, **kwargs: object) -> None:
        self.updates.append({"segment_id": segment_id, **kwargs})


def _pdf(pages: int) -> bytes:
    return text_pdf([f"page {index + 1}" for index in range(pages)])


async def test_segments_created_after_whole_document_parse_are_running() -> None:
    """segment は解析の後に作るため、待機中ではなく処理中で作る(#1999)。"""
    oracle = _SegmentOracle()
    pipeline = IngestionPipeline(
        oracle=oracle,  # type: ignore[arg-type]
        settings=Settings(rag_pdf_max_pages_per_segment=10),
    )

    segments = await pipeline._prepare_segment_checkpoints(
        document_id="doc-1",
        source_bytes=_pdf(23),
        content_type="application/pdf",
        source_profile=None,
        parser_backend="docling",
        parser_profile="docling",
    )

    assert len(segments) == 3
    assert {segment.status for segment in segments} == {"RUNNING"}
    assert {segment.status for segment in oracle.replaced} == {"RUNNING"}


async def test_retried_failed_segments_are_running() -> None:
    oracle = _SegmentOracle()
    oracle.existing = [
        IngestionSegment(segment_id="doc-1:p1-10", document_id="doc-1", status="SUCCEEDED"),
        IngestionSegment(segment_id="doc-1:p11-20", document_id="doc-1", status="FAILED"),
    ]
    pipeline = IngestionPipeline(oracle=oracle, settings=Settings())  # type: ignore[arg-type]

    segments = await pipeline._prepare_segment_checkpoints(
        document_id="doc-1",
        source_bytes=_pdf(20),
        content_type="application/pdf",
        source_profile=None,
        parser_backend="docling",
        parser_profile="docling",
    )

    assert [segment.status for segment in segments] == ["SUCCEEDED", "RUNNING"]
    assert oracle.updates[0]["status"] == "RUNNING"
