"""PDF の段ごとの解析・段の状態・再開・まとめ・文書全体の整理(#2001)。"""

from __future__ import annotations

from io import BytesIO
from typing import Any, cast

import pytest
from pypdf import PdfReader, PdfWriter
from rag_engine.chunking.headings import layout_section_paths
from rag_parser_core.result import ParserRegistryResult

from app.clients.parser_service import ParserServiceUnavailableError
from app.config import Settings
from app.rag.ingestion import IngestionPipeline, IngestionUserError
from app.rag.ingestion_progress import IngestionProgressReporter
from app.rag.pdf_segment_merge import (
    CONSOLIDATION_ARTIFACT,
    consolidate_document_extraction,
    extraction_with_page_offset,
    merge_segment_extractions,
    pdf_outline_entries,
)
from app.schemas.document import FileStatus, IngestionJobProgress, IngestionSegment
from app.schemas.extraction import (
    DocumentElement,
    ExtractionPage,
    ExtractionTable,
    ExtractionTableCell,
    StructuredExtraction,
)
from tests.fixtures.synthetic_pdf import text_pdf
from tests.test_ingestion_strategy import (
    FakeEmbeddingClient,
    FakeObjectStorage,
    FakeOracle,
    _pdf_source_profile,
)

DOCUMENT_ID = "doc-seg"
# 見出しのあるページ(原本のページ → 見出し)。段の境目(10/11)をまたいで 1 章が続く。
HEADINGS = {1: "1. 概要", 15: "2. 手順", 16: "2.1 準備"}
# 文が終わらずに次のページへ続く本文のページ。
CONTINUED_PAGES = {10}


def _pdf(pages: int) -> bytes:
    return text_pdf([f"page {index + 1}" for index in range(pages)])


def _original_pages(pdf_bytes: bytes) -> list[int]:
    reader = PdfReader(BytesIO(pdf_bytes))
    return [int(page.extract_text().split()[-1]) for page in reader.pages]


def _body(original: int) -> str:
    return f"本文 p{original}" + ("" if original in CONTINUED_PAGES else "。")


def _docling_like_extraction(pdf_bytes: bytes) -> StructuredExtraction:
    """Docling サービスと同じ形の結果(段の中のページ番号・page を含む ID・段の中の見出しの階層)。"""
    records: list[dict[str, Any]] = []
    for local_page, original in enumerate(_original_pages(pdf_bytes), start=1):
        rows = []
        if original in HEADINGS:
            rows.append(("Section-header", HEADINGS[original]))
        rows.append(("Text", _body(original)))
        for seq_no, (category, text) in enumerate(rows, start=1):
            records.append(
                {
                    "id": f"docling-p{local_page}-{seq_no}",
                    "engine": "docling",
                    "page": local_page,
                    "seq_no": seq_no,
                    "bbox": [10.0, 40.0 + seq_no * 20, 200.0, 50.0 + seq_no * 20],
                    "coord_system": "image_top_left",
                    "page_width": 600.0,
                    "page_height": 800.0,
                    "category": category,
                    "text": text,
                    "raw_type": category.lower(),
                    "raw": {"prov": [{"page_no": local_page}]},
                }
            )
    paths = layout_section_paths(records)
    elements = [
        DocumentElement(
            kind="title" if record["category"] == "Section-header" else "text",
            text=record["text"],
            order=order,
            element_id=record["id"],
            source_parser="docling_layout",
            page_number=record["page"],
            bbox=record["bbox"],
            section_path=paths.get(record["id"], []),
            metadata={"seq_no": record["seq_no"]},
        )
        for order, record in enumerate(records)
    ]
    page_numbers = sorted({record["page"] for record in records})
    return StructuredExtraction(
        raw_text="\n\n".join(element.text for element in elements),
        confidence=0.9,
        elements=elements,
        pages=[
            ExtractionPage(
                page_number=page,
                width=600.0,
                height=800.0,
                element_ids=[r["id"] for r in records if r["page"] == page],
            )
            for page in page_numbers
        ],
        parser_artifacts={
            "external_adapter": "docling",
            "source_page_count": len(page_numbers),
            "table_count": 0,
            "picture_count": 0,
            "layout_records": {
                "version": 1,
                "pages": [{"page": page, "width": 600, "height": 800} for page in page_numbers],
                "records": records,
            },
        },
    )


class _Runner:
    """``ParserServiceClient.runner`` の代わり。呼ばれた段(原本のページ)を記録する。"""

    def __init__(self, *, fail_on_call: int | None = None) -> None:
        self.calls: list[list[int]] = []
        self.fail_on_call = fail_on_call

    def __call__(
        self,
        backend: str,
        source_bytes: bytes,
        _source_profile: object,
        _content_type: str,
        *,
        fail_fast: bool = False,
    ) -> ParserRegistryResult:
        _ = fail_fast
        self.calls.append(_original_pages(source_bytes))
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            raise ParserServiceUnavailableError(backend, "connect_error")
        return ParserRegistryResult(
            extraction=_docling_like_extraction(source_bytes),
            parser_backend=backend,
            parser_version="2.132.0",
            template="docling_layout",
        )


class _SegmentHistoryOracle(FakeOracle):
    """段の状態の書き換えの順番を記録する。"""

    def __init__(self) -> None:
        super().__init__()
        self.history: list[tuple[str, str]] = []

    async def replace_ingestion_segments(
        self, document_id: str, segments: list[IngestionSegment]
    ) -> list[IngestionSegment]:
        for segment in segments:
            self.history.append((segment.segment_id, segment.status))
        return await super().replace_ingestion_segments(document_id, segments)

    async def update_ingestion_segment(
        self, segment_id: str, **updates: object
    ) -> IngestionSegment | None:
        status = updates.get("status")
        if isinstance(status, str):
            self.history.append((segment_id, status))
        return await super().update_ingestion_segment(segment_id, **updates)


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "rag_parser_adapter_backend": "docling",
        "rag_review_gate_enabled": False,
        "rag_auto_parse_after_preprocess_enabled": True,
        "rag_vision_enabled": False,
        # 項目抽出は対象外(VLM の stub が無いため、有効なら取込が止まる。#2481)。
        "rag_field_extraction_enabled": False,
        "rag_pdf_max_pages_per_segment": 10,
        **overrides,
    }
    return Settings(**values)  # type: ignore[arg-type]


def _pipeline(
    oracle: FakeOracle, storage: FakeObjectStorage, **settings: object
) -> IngestionPipeline:
    return IngestionPipeline(
        genai=FakeEmbeddingClient(),
        oracle=cast(Any, oracle),
        object_storage=cast(Any, storage),
        settings=_settings(**settings),
    )


async def _ingest(pipeline: IngestionPipeline, pdf: bytes) -> object:
    return await pipeline.ingest(
        DOCUMENT_ID,
        pdf,
        "本文を抽出してください。",
        content_type="application/pdf",
        source_profile=_pdf_source_profile(file_size_bytes=len(pdf)),
    )


def _install(monkeypatch: pytest.MonkeyPatch, fake: _Runner) -> None:
    def runner(_client: object, *args: Any, **kwargs: Any) -> ParserRegistryResult:
        return fake(*args, **kwargs)

    monkeypatch.setattr("app.clients.parser_service.ParserServiceClient.runner", runner)


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> _Runner:
    fake = _Runner()
    _install(monkeypatch, fake)
    return fake


def _segment_ids() -> list[str]:
    return [f"{DOCUMENT_ID}:p1-10", f"{DOCUMENT_ID}:p11-20", f"{DOCUMENT_ID}:p21-23"]


async def test_ingest_parses_pdf_segment_by_segment(runner: _Runner) -> None:
    """23 ページの PDF は 3 つの段に分けて 1 段ずつ解析し、段ごとに 待機中→処理中→完了 にする。"""
    oracle = _SegmentHistoryOracle()
    storage = FakeObjectStorage()

    detail = await _ingest(_pipeline(oracle, storage), _pdf(23))

    assert cast(Any, detail).status == FileStatus.INDEXED
    assert runner.calls == [list(range(1, 11)), list(range(11, 21)), list(range(21, 24))]
    first, second, third = _segment_ids()
    # 段は解析の前に待機中で作り、1 段ずつ処理中 → 完了にする(次の段は前の段の完了の後に始まる)。
    assert oracle.history[:3] == [(first, "QUEUED"), (second, "QUEUED"), (third, "QUEUED")]
    assert oracle.history[3:9] == [
        (first, "RUNNING"),
        (first, "SUCCEEDED"),
        (second, "RUNNING"),
        (second, "SUCCEEDED"),
        (third, "RUNNING"),
        (third, "SUCCEEDED"),
    ]
    segments = [oracle.segments[segment_id] for segment_id in _segment_ids()]
    assert [segment.status for segment in segments] == ["SUCCEEDED"] * 3
    # 段の抽出結果は段ごとの artifact(再開に使う)。
    paths = [segment.artifact_path or "" for segment in segments]
    assert len(set(paths)) == 3
    assert all("/segments/" in path for path in paths)

    saved = oracle.saved_extraction
    assert saved is not None
    assert [page.page_number for page in saved.pages] == list(range(1, 24))
    by_text = {element.text: element for element in saved.elements}
    # 原本のページ番号・ID にそろえる。
    assert by_text[_body(23)].page_number == 23
    assert by_text[_body(23)].element_id == "docling-p23-1"
    # 段の境目の後(p.11)の本文も、前の段の見出しの下に入る(原本全体の解析と同じ)。
    assert by_text[_body(11)].section_path == ["1. 概要"]
    assert by_text["2.1 準備"].section_path == ["2. 手順", "2.1 準備"]
    # ページの境目で切れた段落(p.10 → p.11)をつなぐ。
    assert by_text[_body(11)].metadata["continues_element_id"] == "docling-p10-1"
    report = cast(dict[str, Any], saved.parser_artifacts[CONSOLIDATION_ARTIFACT])
    assert report["continued_paragraphs"] == 1
    assert "extraction_artifact_segment_id" not in saved.parser_artifacts


async def test_resume_reuses_succeeded_segments_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """段が失敗したら、始めていない段は取消にする。再実行は成功した段を解析し直さない。"""
    oracle = _SegmentHistoryOracle()
    storage = FakeObjectStorage()
    failing = _Runner(fail_on_call=2)
    _install(monkeypatch, failing)

    with pytest.raises(IngestionUserError):
        await _ingest(_pipeline(oracle, storage), _pdf(23))

    first, second, third = _segment_ids()
    assert [oracle.segments[segment_id].status for segment_id in _segment_ids()] == [
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
    ]
    assert oracle.segments[third].error_code == "segment_not_started"

    retry = _Runner()
    _install(monkeypatch, retry)
    oracle.history.clear()

    detail = await _ingest(_pipeline(oracle, storage), _pdf(23))

    assert cast(Any, detail).status == FileStatus.INDEXED
    assert retry.calls == [list(range(11, 21)), list(range(21, 24))]
    # 成功した段はそのまま、ほかの段は待機中から始める。
    assert (first, "QUEUED") not in oracle.history
    assert oracle.history[:2] == [(second, "QUEUED"), (third, "QUEUED")]
    assert [oracle.segments[segment_id].status for segment_id in _segment_ids()] == [
        "SUCCEEDED"
    ] * 3
    saved = oracle.saved_extraction
    assert saved is not None
    assert [page.page_number for page in saved.pages] == list(range(1, 24))


async def test_stale_segments_from_interrupted_job_restart_from_queued(runner: _Runner) -> None:
    """中断・再起動で待機中・処理中のまま残った段の記録は待機中から始める。

    artifact を読み戻せない成功の記録は解析し直す。
    """
    oracle = _SegmentHistoryOracle()
    first, second, third = _segment_ids()
    stale = {first: "SUCCEEDED", second: "RUNNING", third: "QUEUED"}
    pages = {first: (1, 10), second: (11, 20), third: (21, 23)}
    oracle.segments = {
        segment_id: IngestionSegment(
            segment_id=segment_id,
            document_id=DOCUMENT_ID,
            status=status,
            parser_backend="docling",
            parser_profile="docling",
            page_start=pages[segment_id][0],
            page_end=pages[segment_id][1],
            progress_unit="page",
            # 前の試行の成功の記録はあるが、artifact を読み戻せない(保存先が消えた)。
            artifact_path="oci://namespace/bucket/missing.json" if status == "SUCCEEDED" else None,
        )
        for segment_id, status in stale.items()
    }

    await _ingest(_pipeline(oracle, FakeObjectStorage()), _pdf(23))

    assert runner.calls == [list(range(1, 11)), list(range(11, 21)), list(range(21, 24))]
    assert (second, "QUEUED") in oracle.history and (third, "QUEUED") in oracle.history
    assert oracle.history.index((second, "QUEUED")) < oracle.history.index((second, "RUNNING"))
    assert [oracle.segments[segment_id].status for segment_id in _segment_ids()] == [
        "SUCCEEDED"
    ] * 3


async def test_segment_artifact_is_reused_only_for_same_plan_and_settings(runner: _Runner) -> None:
    """同じ段の分け方・設定なら成功した段を使い回し、段の分け方を変えたら作り直す。"""
    oracle = _SegmentHistoryOracle()
    storage = FakeObjectStorage()
    await _ingest(_pipeline(oracle, storage), _pdf(23))
    runner.calls.clear()

    await _ingest(_pipeline(oracle, storage), _pdf(23))
    reused_calls = list(runner.calls)
    await _ingest(_pipeline(oracle, storage, rag_pdf_max_pages_per_segment=5), _pdf(23))

    assert reused_calls == []
    assert len(runner.calls) == 5


def test_segment_parse_key_changes_with_parser_settings() -> None:
    """段の artifact の鍵は、段の PDF と解析・前処理の設定から作る(設定が変われば使い回さない)。"""
    segment = _pdf(3)
    base = IngestionPipeline(settings=_settings())._segment_parse_key(segment)

    assert IngestionPipeline(settings=_settings())._segment_parse_key(segment) == base
    assert IngestionPipeline(settings=_settings())._segment_parse_key(_pdf(4)) != base
    changed = IngestionPipeline(settings=_settings(rag_parser_docling_enabled=False))
    assert changed._segment_parse_key(segment) != base


async def test_short_pdf_is_parsed_as_one_call(runner: _Runner) -> None:
    """段が 1 つ(10 ページ以下)の PDF は原本全体を 1 回で解析する。"""
    oracle = _SegmentHistoryOracle()

    await _ingest(_pipeline(oracle, FakeObjectStorage()), _pdf(6))

    assert runner.calls == [list(range(1, 7))]


async def test_segment_progress_is_published_with_segment_states(runner: _Runner) -> None:
    """job の進捗に段ごとの状態を載せる(段の帯・「抽出: N / M ページ」・bar を同じ記録から作る)。"""
    snapshots: list[IngestionJobProgress] = []

    async def writer(progress: IngestionJobProgress | None) -> None:
        if progress is not None:
            snapshots.append(progress)

    reporter = IngestionProgressReporter(writer)
    published: list[IngestionJobProgress] = []
    original_update = reporter.update_parse

    def record_update(parse: Any) -> None:
        original_update(parse)
        snapshot = reporter.snapshot()
        if snapshot is not None:
            published.append(snapshot)

    reporter.update_parse = record_update  # type: ignore[method-assign]
    from app.rag import ingestion_progress

    token = ingestion_progress._ACTIVE.set(reporter)
    try:
        await _ingest(_pipeline(_SegmentHistoryOracle(), FakeObjectStorage()), _pdf(23))
    finally:
        ingestion_progress._ACTIVE.reset(token)

    states = [
        tuple(segment.status for segment in item.parse.segments)
        for item in published
        if item.parse is not None
    ]
    assert states[0] == ("QUEUED", "QUEUED", "QUEUED")
    assert ("SUCCEEDED", "RUNNING", "QUEUED") in states
    final = published[-1].parse
    assert final is not None and final.finished
    assert (final.completed, final.total) == (23, 23)
    assert [segment.status for segment in final.segments] == ["SUCCEEDED"] * 3
    assert [(s.page_start, s.page_end) for s in final.segments] == [(1, 10), (11, 20), (21, 23)]


def test_merged_segments_match_whole_document_parse() -> None:
    """段の結果をまとめると、原本全体を 1 回で解析した結果と同じ record・ID・見出しの階層になる。"""
    pdf = _pdf(23)
    whole = _docling_like_extraction(pdf)
    from app.rag.pdf_segments import split_pdf_page_segments

    merged = merge_segment_extractions(
        [
            (
                segment.page_start,
                extraction_with_page_offset(
                    _docling_like_extraction(segment.content),
                    page_start=segment.page_start,
                    page_end=segment.page_end,
                ),
            )
            for segment in split_pdf_page_segments(pdf, max_pages_per_segment=10)
        ]
    )

    def records(extraction: StructuredExtraction) -> list[tuple[Any, ...]]:
        layout = cast(dict[str, Any], extraction.parser_artifacts["layout_records"])
        return [(r["id"], r["page"], r["category"], r["text"]) for r in layout["records"]]

    assert records(merged) == records(whole)
    assert [(e.element_id, e.page_number, e.section_path) for e in merged.elements] == [
        (e.element_id, e.page_number, e.section_path) for e in whole.elements
    ]
    assert merged.parser_artifacts["source_page_count"] == 23
    assert merged.raw_text == whole.raw_text


def test_page_offset_prefixes_identifiers_without_page() -> None:
    """ページを含まない ID は段の先頭のページを付け、段の間で重ならないようにする。"""
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(text="x", element_id="el-0001", parent_id="el-0000", page_number=2)
        ],
        pages=[ExtractionPage(page_number=2, element_ids=["el-0001"])],
        tables=[ExtractionTable(table_id="t-1", element_id="el-0001", page_number=2)],
    )

    shifted = extraction_with_page_offset(extraction, page_start=11, page_end=20)

    element = shifted.elements[0]
    assert (element.element_id, element.parent_id, element.page_number) == (
        "p11-el-0001",
        "p11-el-0000",
        12,
    )
    assert shifted.pages[0].page_number == 12
    assert shifted.pages[0].element_ids == ["p11-el-0001"]
    assert (shifted.tables[0].table_id, shifted.tables[0].page_number) == ("p11-t-1", 12)


def test_merge_inherits_section_path_for_parsers_without_layout_records() -> None:
    """layout_records の無い解析エンジンは、段の先頭の本文に前の段の最後の見出しを引き継ぐ。"""
    first = StructuredExtraction(
        elements=[
            DocumentElement(kind="title", text="1. 概要", page_number=1, section_path=["1. 概要"]),
            DocumentElement(kind="text", text="本文", page_number=1, section_path=["1. 概要"]),
        ]
    )
    second = StructuredExtraction(
        elements=[
            DocumentElement(kind="text", text="続き", page_number=11),
            DocumentElement(kind="title", text="2. 手順", page_number=11, section_path=["2. 手順"]),
        ]
    )

    merged = merge_segment_extractions([(11, second), (1, first)])

    assert [element.section_path for element in merged.elements] == [
        ["1. 概要"],
        ["1. 概要"],
        ["1. 概要"],
        ["2. 手順"],
    ]
    assert [element.order for element in merged.elements] == [0, 1, 2, 3]


def _pdf_with_outline(pages: int, outline: list[tuple[int, str, int]]) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=600, height=800)
    parents: dict[int, Any] = {}
    for level, title, page in outline:
        parents[level] = writer.add_outline_item(title, page - 1, parent=parents.get(level - 1))
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def _layout_extraction(records: list[dict[str, Any]]) -> StructuredExtraction:
    for seq, record in enumerate(records, start=1):
        record.setdefault("engine", "docling")
        record.setdefault("seq_no", seq)
        record.setdefault("id", f"docling-p{record['page']}-{seq}")
        record.setdefault("bbox", [10.0, 100.0, 200.0, 120.0])
        record.setdefault("coord_system", "image_top_left")
        record.setdefault("page_width", 600.0)
        record.setdefault("page_height", 800.0)
        record.setdefault("raw", {})
    return StructuredExtraction(
        elements=[
            DocumentElement(
                kind="title" if r["category"] == "Section-header" else "text",
                text=r["text"],
                element_id=r["id"],
                page_number=r["page"],
            )
            for r in records
        ],
        tables=[
            ExtractionTable(table_id=f"table-{r['id']}", element_id=r["id"], page_number=r["page"])
            for r in records
            if r["category"] == "Table"
        ],
        parser_artifacts={"layout_records": {"version": 1, "pages": [], "records": records}},
    )


def test_outline_levels_set_heading_hierarchy() -> None:
    """しおりの深さを見出しの深さにする(番号の無い見出しの階層をしおりから決める)。"""
    pdf = _pdf_with_outline(3, [(1, "はじめに", 1), (2, "背景", 2), (1, "まとめ", 3)])
    extraction = _layout_extraction(
        [
            {"page": 1, "category": "Section-header", "text": "はじめに"},
            {"page": 2, "category": "Section-header", "text": "背景"},
            {"page": 2, "category": "Text", "text": "背景の本文。"},
            {"page": 3, "category": "Section-header", "text": "まとめ"},
        ]
    )

    assert pdf_outline_entries(pdf) == [(1, "はじめに", 1), (2, "背景", 2), (1, "まとめ", 3)]
    result = consolidate_document_extraction(extraction, source_bytes=pdf)

    paths = {element.text: element.section_path for element in result.elements}
    assert paths["背景"] == ["はじめに", "背景"]
    assert paths["背景の本文。"] == ["はじめに", "背景"]
    assert paths["まとめ"] == ["まとめ"]
    report = cast(dict[str, Any], result.parser_artifacts[CONSOLIDATION_ARTIFACT])
    assert report["outline_headings"] == 3


def test_outline_is_ignored_when_most_entries_do_not_match_headings() -> None:
    """しおりの多くが本文の見出しに当たらない(スライドの区分など)なら、しおりを使わない。"""
    pdf = _pdf_with_outline(
        3, [(1, "Default Section", 1), (2, "スライド 1", 1), (2, "スライド 2", 2), (1, "まとめ", 3)]
    )
    extraction = _layout_extraction(
        [
            {"page": 1, "category": "Section-header", "text": "概要"},
            {"page": 3, "category": "Section-header", "text": "まとめ"},
        ]
    )

    result = consolidate_document_extraction(extraction, source_bytes=pdf)

    report = cast(dict[str, Any], result.parser_artifacts[CONSOLIDATION_ARTIFACT])
    assert report["outline_headings"] == 0


def test_table_continued_on_next_page_carries_column_headers() -> None:
    """次のページへ続く表(同じ列の数・続きに列見出しが無い)に、前の表の列見出しの行を付ける。"""
    first = (
        "<table><tbody><tr><th>項目</th><th>値</th></tr>"
        "<tr><td>A</td><td>1</td></tr></tbody></table>"
    )
    second = "<table><tbody><tr><td>B</td><td>2</td></tr></tbody></table>"
    other = "<table><tbody><tr><td>x</td><td>y</td><td>z</td></tr></tbody></table>"
    extraction = _layout_extraction(
        [
            {"page": 1, "category": "Table", "text": first},
            {"page": 1, "category": "Page-footer", "text": "1"},
            {"page": 2, "category": "Page-header", "text": "柱"},
            {"page": 2, "category": "Table", "text": second},
            {"page": 3, "category": "Table", "text": other},
        ]
    )

    result = consolidate_document_extraction(extraction, source_bytes=b"")

    layout = cast(dict[str, Any], result.parser_artifacts["layout_records"])
    by_page = {r["page"]: r for r in layout["records"] if r["category"] == "Table"}
    assert by_page[2]["raw"]["continues_record_id"] == by_page[1]["id"]
    assert "<th>項目</th>" in by_page[2]["raw"]["table_enhanced_html"]
    # 列の数が違う表は続きにしない。
    assert "continues_record_id" not in by_page[3]["raw"]
    table = next(t for t in result.tables if t.element_id == by_page[2]["id"])
    assert table.metadata["continued_from_table_id"] == f"table-{by_page[1]['id']}"
    report = cast(dict[str, Any], result.parser_artifacts[CONSOLIDATION_ARTIFACT])
    assert (report["continued_tables"], report["table_headers_carried"]) == (1, 1)


@pytest.mark.parametrize(
    ("previous", "current", "continues"),
    [
        ("文が次のページへ", "続いています。", True),
        ("文はここで終わります。", "新しい段落です。", False),
        ("手順の説明", "・次の項目", False),
        ("手順の説明", "(2) 次の手順", False),
    ],
    ids=["unfinished", "finished", "bullet", "numbered"],
)
def test_paragraph_continuation_across_page(previous: str, current: str, continues: bool) -> None:
    extraction = _layout_extraction(
        [
            {"page": 1, "category": "Text", "text": previous},
            {"page": 2, "category": "Text", "text": current},
        ]
    )

    result = consolidate_document_extraction(extraction, source_bytes=b"")

    second = result.elements[1]
    assert ("continues_element_id" in second.metadata) is continues


def test_consolidation_is_idempotent_and_skips_non_docling() -> None:
    extraction = _layout_extraction(
        [
            {"page": 1, "category": "Text", "text": "文が次のページへ"},
            {"page": 2, "category": "Text", "text": "続いています。"},
        ]
    )
    once = consolidate_document_extraction(extraction, source_bytes=b"")
    twice = consolidate_document_extraction(once, source_bytes=b"")
    assert twice.parser_artifacts == once.parser_artifacts

    plain = StructuredExtraction(
        elements=[DocumentElement(text="本文", page_number=1)],
        tables=[ExtractionTable(table_id="t", cells=[ExtractionTableCell(row=0, col=0, text="a")])],
    )
    assert consolidate_document_extraction(plain, source_bytes=b"") == plain
