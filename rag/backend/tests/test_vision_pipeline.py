"""Vision を PDF の段ごとの解析と並べる（流水線）・段ごとの再開・進捗（#2002）。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable, Sequence
from types import SimpleNamespace
from typing import Any, cast

import pytest
from rag_parser_core.result import ParserRegistryResult

from app.clients import ai_calls
from app.config import Settings
from app.rag.ingestion import IngestionPipeline, IngestionUserError
from app.schemas.document import IngestionVisionProgress
from app.schemas.extraction import StructuredExtraction
from tests.test_ingestion_pdf_segments import (
    DOCUMENT_ID,
    _docling_like_extraction,
    _original_pages,
    _pdf,
)
from tests.test_ingestion_strategy import (
    FakeEmbeddingClient,
    FakeObjectStorage,
    FakeOracle,
    _pdf_source_profile,
)


@pytest.fixture(autouse=True)
def no_real_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(seconds: float) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(ai_calls, "_sleep", fake_sleep)
    monkeypatch.setattr(ai_calls, "_random", lambda: 0.5)


class Storage(FakeObjectStorage):
    """実の client と同じく、保存のキーでも取得できる fake。"""

    async def get(self, path: str) -> bytes:
        self.gets.append(path)
        if path in self.objects:
            return self.objects[path]
        return self.objects[f"local://{path}"]


def _with_pictures(extraction: StructuredExtraction) -> StructuredExtraction:
    """各ページの本文の下に 1 つの図（本文の空の Picture）を足す。"""
    layout = dict(cast(dict[str, Any], extraction.parser_artifacts["layout_records"]))
    records = [dict(record) for record in layout["records"]]
    pages = sorted({record["page"] for record in records})
    for page in pages:
        records.append(
            {
                "id": f"docling-p{page}-9",
                "engine": "docling",
                "page": page,
                "seq_no": 9,
                "bbox": [100.0, 250.0, 500.0, 650.0],
                "coord_system": "image_top_left",
                "page_width": 600.0,
                "page_height": 800.0,
                "category": "Picture",
                "text": "",
                "raw_type": "picture",
                "raw": {"prov": [{"page_no": page}]},
            }
        )
    layout["records"] = records
    # PDF の寸法（point）を渡すと、Docling と同じ解像度で描く（小さい画像で速い）。
    layout["pages"] = [
        {**page, "pdf_width": 612.0, "pdf_height": 792.0} for page in layout["pages"]
    ]
    return extraction.model_copy(
        update={"parser_artifacts": {**extraction.parser_artifacts, "layout_records": layout}}
    )


class Runner:
    """解析サービスの代わり（段ごとに ``delay`` 秒かかる。``fail_on_call`` 回目は失敗）。

    ``hold`` は、何回目の呼び出しを終えてよいかを返す（偽の間は終えずに待つ。上限 ``HOLD_TIMEOUT``
    秒）。重なり・先読みを、実行の速さ（CI の負荷）によらず確かめるために、時間ではなく相手の進み
    で待つ。
    """

    HOLD_TIMEOUT = 30.0

    def __init__(
        self,
        delay: float = 0.0,
        fail_on_call: int | None = None,
        fail_delay: float = 0.0,
        hold: Callable[[int], bool] | None = None,
    ) -> None:
        self.delay = delay
        self.fail_on_call = fail_on_call
        self.fail_delay = fail_delay
        self.hold = hold
        self.calls: list[tuple[float, float, list[int]]] = []

    def __call__(
        self, backend: str, source_bytes: bytes, *_: Any, **__: Any
    ) -> ParserRegistryResult:
        from app.clients.parser_service import ParserServiceUnavailableError

        started = time.monotonic()
        number = len(self.calls) + 1
        failing = self.fail_on_call is not None and number == self.fail_on_call
        time.sleep(self.fail_delay if failing else self.delay)
        deadline = started + self.HOLD_TIMEOUT
        while self.hold is not None and not self.hold(number) and time.monotonic() < deadline:
            time.sleep(0.01)
        pages = _original_pages(source_bytes)
        self.calls.append((started, time.monotonic(), pages))
        if failing:
            raise ParserServiceUnavailableError(backend, "connect_error")
        return ParserRegistryResult(
            extraction=_with_pictures(_docling_like_extraction(source_bytes)),
            parser_backend=backend,
            parser_version="2.132.0",
            template="docling_layout",
        )


class Reader:
    """Vision のスタブ。説明は prompt の hash から作る（入力が同じなら同じ説明）。"""

    def __init__(self, delay: float = 0.01, failure: BaseException | None = None) -> None:
        self.delay = delay
        self.failure = failure
        self.calls: list[float] = []

    async def generate_from_images(self, images: Sequence[bytes], prompt: str, **_: Any) -> str:
        self.calls.append(time.monotonic())
        await asyncio.sleep(self.delay)
        if self.failure is not None:
            raise self.failure
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:10]
        return json.dumps({"retrieval_text": f"図の説明 {digest}"}, ensure_ascii=False)

    async def generate(self, prompt: str, context: str, **_: Any) -> str:
        return "[]"


class PromptOracle(FakeOracle):
    async def answer_prompt_overrides(self) -> dict[str, str]:
        return {}


def _install(monkeypatch: pytest.MonkeyPatch, runner: Runner) -> None:
    def run(_client: object, *args: Any, **kwargs: Any) -> ParserRegistryResult:
        return runner(*args, **kwargs)

    monkeypatch.setattr("app.clients.parser_service.ParserServiceClient.runner", run)


def _pipeline(reader: Reader, oracle: FakeOracle, storage: FakeObjectStorage) -> IngestionPipeline:
    return IngestionPipeline(
        genai=FakeEmbeddingClient(),
        vlm=cast(Any, reader),
        oracle=cast(Any, oracle),
        object_storage=cast(Any, storage),
        settings=Settings(
            rag_parser_adapter_backend="docling",
            rag_review_gate_enabled=False,
            rag_auto_parse_after_preprocess_enabled=True,
            rag_vision_enabled=True,
            rag_field_extraction_enabled=False,
            rag_pdf_max_pages_per_segment=10,
            rag_vision_concurrency=4,
            rag_ai_call_final_retry_delay_seconds=0.0,
        ),
    )


async def _ingest(pipeline: IngestionPipeline, pdf: bytes) -> Any:
    return await pipeline.ingest(
        DOCUMENT_ID,
        pdf,
        "本文を抽出してください。",
        content_type="application/pdf",
        source_profile=_pdf_source_profile(file_size_bytes=len(pdf)),
    )


def _figure_texts(oracle: FakeOracle) -> list[tuple[str, str]]:
    extraction = _saved_extraction(oracle)
    layout = cast(dict[str, Any], extraction.parser_artifacts["layout_records"])
    return [
        (record["id"], record["text"])
        for record in layout["records"]
        if record["category"] == "Picture"
    ]


def _saved_extraction(oracle: FakeOracle) -> StructuredExtraction:
    assert oracle.saved_extraction is not None, "抽出が保存されていません"
    return oracle.saved_extraction


async def test_vision_starts_per_segment_while_next_segments_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """段の解析が終わるとその段の図の Vision を始め、次の段の解析と重ねる。

    全体の読み取りは呼び直さない。
    """
    reader = Reader()
    # 最後の段の解析は、先に解析した段の図の Vision が始まるまで終えない（重なりを確かめる）。
    runner = Runner(hold=lambda number: number < 3 or bool(reader.calls))
    _install(monkeypatch, runner)
    oracle = PromptOracle()
    await _ingest(_pipeline(reader, oracle, Storage()), _pdf(23))

    assert [pages[0] for _, _, pages in runner.calls] == [1, 11, 21]
    # 23 ページ × 1 図。全体の読み取りは段ごとに先に読んだ応答を使い、VLM を呼び直さない。
    assert len(reader.calls) == 23
    last_parse_ended = runner.calls[-1][1]
    assert min(reader.calls) < last_parse_ended  # 最後の段の解析の間に Vision が始まった
    texts = dict(_figure_texts(oracle))
    assert len(texts) == 23 and all("図の説明" in text for text in texts.values())


async def test_pipelined_vision_matches_vision_after_all_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """流水線で読んでも、すべての段の後に読んだときと同じ説明（同じ入力・同じ prompt）になる。"""
    runner = Runner()
    _install(monkeypatch, runner)
    pipelined_oracle = PromptOracle()
    await _ingest(_pipeline(Reader(), pipelined_oracle, Storage()), _pdf(23))

    async def no_prefetch(self: IngestionPipeline, **_: Any) -> None:
        return None

    monkeypatch.setattr(IngestionPipeline, "_start_vision_prefetch", no_prefetch)
    sequential_oracle = PromptOracle()
    sequential_reader = Reader()
    await _ingest(_pipeline(sequential_reader, sequential_oracle, Storage()), _pdf(23))

    assert len(sequential_reader.calls) == 23
    assert _figure_texts(pipelined_oracle) == _figure_texts(sequential_oracle)
    assert [e.text for e in _saved_extraction(pipelined_oracle).elements] == [
        e.text for e in _saved_extraction(sequential_oracle).elements
    ]


async def test_resume_reuses_vision_of_finished_segments(monkeypatch: pytest.MonkeyPatch) -> None:
    """解析の失敗の後の再実行は、前の試行で段ごとに読んだ図の VLM を呼び直さない。"""
    oracle = PromptOracle()
    storage = Storage()
    # 同じ原本（再試行は同じ文書のバイト列。応答の鍵は原本の hash を含む）。
    pdf = _pdf(23)

    # 段 3 の解析は、段 1・2 の先読みの応答を段の artifact に残してから失敗する。
    def vision_saved() -> int:
        return sum(1 for key, _, _ in storage.puts if "/vision/" in key)

    failing = Runner(fail_on_call=3, hold=lambda number: number != 3 or vision_saved() >= 2)
    _install(monkeypatch, failing)
    first = Reader()
    with pytest.raises(IngestionUserError):
        await _ingest(_pipeline(first, oracle, storage), pdf)
    # 先読みの終わった段（段 1・2。10 図ずつ）の応答を段の artifact に残す。
    saved = sorted(key.rsplit("/", 1)[-1] for key, _, _ in storage.puts if "/vision/" in key)
    assert saved and set(saved) <= {"doc-seg_p1-10.json", "doc-seg_p11-20.json"}
    assert len(first.calls) >= 10 * len(saved)

    _install(monkeypatch, Runner())
    second = Reader()
    await _ingest(_pipeline(second, oracle, storage), pdf)
    # 残した段の図は呼び直さない（呼ぶのは残していない段の図だけ）。
    assert len(second.calls) == 23 - 10 * len(saved)
    assert len(dict(_figure_texts(oracle))) == 23


async def test_segment_vision_progress_is_aggregated_while_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """先に読んでいる段の Vision の件数を足して出し、解析の途中は解析の段のままにする。"""
    from app.rag.ingestion_progress import IngestionProgressReporter, track_ingestion_progress

    seen: list[tuple[IngestionVisionProgress, str | None]] = []

    def vision_seen_while_parsing() -> bool:
        return any(v.segments_total and stage == "parse" for v, stage in seen)

    # 最後の段の解析は、先に解析した段の Vision の進捗が解析の段のうちに届くまで終えない（#3390。
    # 実時間の待ちに頼らず、CI の負荷によらず解析の途中の更新を確かめる）。
    _install(monkeypatch, Runner(hold=lambda number: number < 3 or vision_seen_while_parsing()))
    original = IngestionProgressReporter.update_vision

    def record(self: IngestionProgressReporter, vision: IngestionVisionProgress) -> None:
        original(self, vision)
        snapshot = self.snapshot()
        seen.append((vision, snapshot.stage if snapshot else None))

    monkeypatch.setattr(IngestionProgressReporter, "update_vision", record)

    async def writer(_: object) -> None:
        return None

    async with track_ingestion_progress(writer, interval_seconds=60):
        await _ingest(_pipeline(Reader(), PromptOracle(), Storage()), _pdf(23))

    segment_updates = [(v, stage) for v, stage in seen if v.segments_total]
    assert segment_updates
    assert {v.segments_total for v, _ in segment_updates} == {3}
    assert any(v.segment_page_start == 1 and v.segment_page_end == 10 for v, _ in segment_updates)
    assert max(v.segments_completed for v, _ in segment_updates) == 3
    assert "parse" in {stage for _, stage in segment_updates}
    final = seen[-1][0]
    assert final.finished and final.succeeded == 23


async def test_permission_error_in_segment_vision_stops_ingestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, Runner())
    error = Exception("returned 403: Forbidden")
    error.response = SimpleNamespace(status_code=403, headers={})  # type: ignore[attr-defined]
    reader = Reader(failure=error)
    with pytest.raises(IngestionUserError, match="権限がありません"):
        await _ingest(_pipeline(reader, PromptOracle(), Storage()), _pdf(23))
    assert len(reader.calls) <= 4  # 並行の上限の分だけで止める（残りの図を呼ばない）
