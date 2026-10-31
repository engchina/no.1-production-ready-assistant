"""表の中の画像を Vision で読み取った説明文が、全ての分割の方法で検索の本文に入る(#513)。

解析(Docling / Docling 以外)→ Vision の共通の段(``read_figures_with_vision``)→ 分割 →
索引の本文(Oracle Text の ``search_text`` と embedding の入力)の順に、決定論スタブで確かめる。
VLM は ``FakeDescriber`` に置き換え、表の中の画像は reportlab で作った PDF に置く。

分割の方法ごとの結果(修正前 → 修正後):

- Docling(表の説明文は要素の metadata と ``layout_records`` の record にだけある)
  - small_to_big: 入る → 入る(record の ``table_vision_text`` を chunk に足す)
  - structure_aware / page_level: 入らない → 入る(表の要素の本文の後ろに補足を足す)
  - recursive_character / markdown_heading / fixed_size / fixed_delimiter: 入らない → 入る
    (Docling の raw_text は表を含まないため、表の位置に補足だけを入れる)
- Docling 以外(Vision の段が表の本文と raw_text に補足を足す): 全て入る → 入る(二重に足さない)。
  small_to_big は layout_records がないので構造認識へ縮退し、同じく入る。
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image
from rag_pipeline_core.chunking import (
    CHUNKING_STRATEGIES,
    TABLE_VISION_SUPPLEMENT_LABEL,
    Chunk,
    chunk_extraction_with_strategy,
)

from app.clients.oracle import _chunk_search_text
from app.rag.chunking_small_to_big import (
    SMALL_TO_BIG_STRATEGY,
    build_parent_child_chunks,
    engine_search_text,
)
from app.rag.vision import read_figures_with_vision
from app.schemas.extraction import DocumentElement, ExtractionPage, StructuredExtraction
from tests.fixtures.synthetic_pdf import SyntheticPdf

PAGE_PT = (595.0, 842.0)
TABLE_PT = (100.0, 500.0, 500.0, 620.0)
TABLE_IMAGE_PT = (120.0, 520.0, 220.0, 600.0)
RENDER_DPI = 300
TEMPLATE = "図を説明してください。\n{{image_metadata}}\n{{image}}"
VISION_TEXT = "受注画面の登録ボタン"
TABLE_HTML = "<table><tr><th>品目</th><th>画像</th></tr><tr><td>部品A</td><td></td></tr></table>"
TABLE_MARKDOWN = "| 品目 | 画像 |\n| --- | --- |\n| 部品A | |"
ALL_STRATEGIES = (*CHUNKING_STRATEGIES, SMALL_TO_BIG_STRATEGY)


class FakeDescriber:
    """rag_engine の PictureDescriber の決定論スタブ。"""

    api_mode = "test"
    max_tokens = 1

    def __init__(self) -> None:
        self.kinds: list[str] = []

    def provider(self) -> Any:
        return SimpleNamespace(
            provider_id="fake", model="fake-vision", region="", base_url="", project_id=""
        )

    def describe(
        self,
        crop_path: Path,
        metadata: dict[str, Any],
        *,
        context_image_paths: Sequence[Path],
        target_kind: str,
        rendered_prompt: str,
    ) -> dict[str, Any]:
        self.kinds.append(target_kind)
        return {"retrieval_text": VISION_TEXT, "main_topic": "受注入力"}


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (200, 160), "green").save(buffer, format="PNG")
    return buffer.getvalue()


def _pdf() -> bytes:
    document = SyntheticPdf()
    page = document.page(*PAGE_PT)
    page.text(100, 80, "受注入力の手順", font="japan", size=14)
    page.image(TABLE_IMAGE_PT, _png())
    return document.to_bytes()


def _px(points: Sequence[float], dpi: float) -> list[float]:
    return [value * dpi / 72 for value in points]


def _docling_extraction() -> StructuredExtraction:
    """Docling サービスの変換(``layout_to_extraction``)と同じ形。raw_text は表を含まない。"""
    width, height = round(PAGE_PT[0] * RENDER_DPI / 72), round(PAGE_PT[1] * RENDER_DPI / 72)

    def record(
        record_id: str, seq: int, category: str, text: str, bbox: list[float]
    ) -> dict[str, Any]:
        return {
            "id": record_id,
            "engine": "docling",
            "page": 1,
            "seq_no": seq,
            "bbox": bbox,
            "coord_system": "image_top_left",
            "page_width": width,
            "page_height": height,
            "category": category,
            "text": text,
            "confidence": None,
            "raw_type": category.lower(),
            "raw": {},
        }

    title_bbox = _px((100, 60, 300, 85), RENDER_DPI)
    text_bbox = _px((100, 300, 400, 320), RENDER_DPI)
    after_bbox = _px((100, 650, 400, 670), RENDER_DPI)
    table_bbox = _px(TABLE_PT, RENDER_DPI)
    records = [
        record("docling-p1-1", 1, "Title", "受注入力の手順", title_bbox),
        record("docling-p1-2", 2, "Text", "部品の一覧です。", text_bbox),
        record("docling-p1-3", 3, "Table", TABLE_HTML, table_bbox),
        record("docling-p1-4", 4, "Text", "登録後に確認します。", after_bbox),
    ]
    kinds = {"Title": "title", "Text": "text", "Table": "table"}
    elements = [
        DocumentElement(
            kind=kinds[str(item["category"])],
            text=str(item["text"]),
            element_id=str(item["id"]),
            page_number=1,
            bbox=list(item["bbox"]),
            section_path=["受注入力の手順"],
            source_parser="docling_layout",
            metadata={"category": str(item["category"])},
        )
        for item in records
    ]
    return StructuredExtraction(
        raw_text="受注入力の手順\n\n部品の一覧です。\n\n登録後に確認します。",
        elements=elements,
        pages=[ExtractionPage(page_number=1, width=width, height=height)],
        parser_artifacts={
            "external_adapter": "docling",
            "layout_records": {
                "version": 1,
                "pages": [
                    {
                        "page": 1,
                        "width": width,
                        "height": height,
                        "pdf_width": PAGE_PT[0],
                        "pdf_height": PAGE_PT[1],
                    }
                ],
                "records": records,
            },
        },
    )


def _adapter_extraction() -> StructuredExtraction:
    """Docling 以外(Unstructured)。表は Markdown で raw_text にも入る。"""
    dpi = 200
    width, height = _px(PAGE_PT, dpi)
    return StructuredExtraction(
        raw_text=f"受注入力の手順\n\n部品の一覧です。\n\n{TABLE_MARKDOWN}\n\n登録後に確認します。",
        elements=[
            DocumentElement(kind="title", text="受注入力の手順", element_id="el-0", page_number=1),
            DocumentElement(kind="text", text="部品の一覧です。", element_id="el-1", page_number=1),
            DocumentElement(
                kind="table",
                text=TABLE_MARKDOWN,
                element_id="tbl-1",
                page_number=1,
                bbox=_px(TABLE_PT, dpi),
                metadata={"page_width": width, "page_height": height},
            ),
            DocumentElement(
                kind="text", text="登録後に確認します。", element_id="el-3", page_number=1
            ),
        ],
        pages=[ExtractionPage(page_number=1)],
    )


def _read(extraction: StructuredExtraction, backend: str) -> StructuredExtraction:
    describer = FakeDescriber()
    result = read_figures_with_vision(
        extraction,
        source_bytes=_pdf(),
        content_type="application/pdf",
        file_name="manual.pdf",
        parser_backend=backend,
        describer=describer,
        prompt_template=TEMPLATE,
    )
    assert describer.kinds == ["table"]
    return result


def _chunks(extraction: StructuredExtraction, strategy: str) -> list[Chunk]:
    # 取込(``IngestionPipeline``)と同じく、親子階層（small-to-big）はどの解析の結果でも親子に
    # 分割する(#3377)。
    if strategy == SMALL_TO_BIG_STRATEGY:
        return build_parent_child_chunks(extraction, source_name="manual.pdf")
    return chunk_extraction_with_strategy(
        extraction, strategy=strategy, chunk_size=800, overlap=120, min_chars=0
    )


def _embedding_input(chunk: Chunk) -> str:
    # ``IngestionPipeline._chunk_embedding_inputs`` と同じ選び方(文脈ヘッダは無効のとき)。
    return engine_search_text(chunk.metadata) or chunk.text


@pytest.fixture(scope="module")
def docling_result() -> StructuredExtraction:
    return _read(_docling_extraction(), "docling")


@pytest.fixture(scope="module")
def adapter_result() -> StructuredExtraction:
    return _read(_adapter_extraction(), "unstructured")


def test_docling_keeps_table_vision_text_out_of_the_element_body(
    docling_result: StructuredExtraction,
) -> None:
    """Docling の表の要素と record の本文(表の HTML)は変えず、説明文は metadata にある。"""
    table = next(element for element in docling_result.elements if element.kind == "table")
    assert table.text == TABLE_HTML
    assert table.metadata["table_vision_text"]
    assert VISION_TEXT in str(table.metadata["vision_retrieval_text"])
    assert VISION_TEXT not in docling_result.raw_text
    layout: Any = docling_result.parser_artifacts["layout_records"]
    record = layout["records"][2]
    assert record["text"] == TABLE_HTML
    assert VISION_TEXT in record["raw"]["table_vision_text"]


@pytest.mark.parametrize("strategy", ALL_STRATEGIES)
def test_docling_table_vision_text_reaches_search_text(
    docling_result: StructuredExtraction, strategy: str
) -> None:
    chunks = _chunks(docling_result, strategy)

    assert any(VISION_TEXT in _chunk_search_text(chunk) for chunk in chunks)
    assert any(VISION_TEXT in _embedding_input(chunk) for chunk in chunks)
    # 補足は 1 回だけ入る。
    assert sum(chunk.text.count(TABLE_VISION_SUPPLEMENT_LABEL) for chunk in chunks) == 1


@pytest.mark.parametrize("strategy", ALL_STRATEGIES)
def test_adapter_table_vision_text_reaches_search_text_once(
    adapter_result: StructuredExtraction, strategy: str
) -> None:
    """Docling 以外は Vision の段が表の本文に補足を足している。分割で二重に足さない。"""
    table = next(element for element in adapter_result.elements if element.kind == "table")
    assert table.text.startswith(TABLE_MARKDOWN)
    assert TABLE_VISION_SUPPLEMENT_LABEL in table.text

    chunks = _chunks(adapter_result, strategy)

    assert any(VISION_TEXT in _chunk_search_text(chunk) for chunk in chunks)
    assert any(VISION_TEXT in _embedding_input(chunk) for chunk in chunks)
    assert sum(chunk.text.count(TABLE_VISION_SUPPLEMENT_LABEL) for chunk in chunks) == 1


def test_docling_structure_aware_keeps_table_body_and_offsets(
    docling_result: StructuredExtraction,
) -> None:
    """構造認識の表の chunk は、元の表の本文の後ろに区切った補足を足し、位置は変えない。"""
    before = chunk_extraction_with_strategy(
        _docling_extraction(), strategy="structure_aware", chunk_size=800, overlap=120
    )
    after = chunk_extraction_with_strategy(
        docling_result, strategy="structure_aware", chunk_size=800, overlap=120
    )

    table_chunk = next(chunk for chunk in after if chunk.metadata.get("content_kind") == "table")
    assert table_chunk.text.startswith(f"{TABLE_HTML}\n{TABLE_VISION_SUPPLEMENT_LABEL}\n")
    assert [(chunk.start_offset, chunk.end_offset) for chunk in after] == [
        (chunk.start_offset, chunk.end_offset) for chunk in before
    ]
