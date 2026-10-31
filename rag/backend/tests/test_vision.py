"""解析後の図・画像の読み取り(Vision。#497)の共通の段のテスト。

VLM は決定論スタブ(``FakeDescriber``)に置き換え、切り出した画像の寸法で bbox の単位の
正規化(解析エンジンごと)を確かめる。PDF / 画像は reportlab / Pillow でテストの中で作る。
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image
from rag_engine.models.layout import PageImage
from rag_pipeline_core.chunking import chunk_extraction_with_strategy

from app.rag.chunking_small_to_big import build_parent_child_chunks
from app.rag.vision import (
    WARNING_BBOX_UNIT_UNKNOWN,
    WARNING_PARTIAL_FAILURE,
    EnterpriseAiPictureDescriber,
    VisionResponseError,
    parse_picture_description,
    read_figures_with_vision,
    vision_failure,
)
from app.rag.vision import _page_px_bbox as page_px_bbox
from app.schemas.extraction import (
    DocumentElement,
    ExtractionAsset,
    ExtractionPage,
    StructuredExtraction,
)
from tests.fixtures.synthetic_pdf import SyntheticPdf

PAGE_PT = (595.0, 842.0)
# 図の位置(PDF の pt)。テストの PDF はここに画像を置く。
FIGURE_PT = (100.0, 100.0, 300.0, 250.0)
TEMPLATE = "図を説明してください。\n{{image_metadata}}\n{{image}}"
RENDER_DPI = 300


def _png(size: tuple[int, int], color: str = "navy") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _pdf(*, table_image: bool = False) -> bytes:
    document = SyntheticPdf()
    page = document.page(*PAGE_PT)
    page.text(100, 80, "受注入力の手順", font="japan", size=14)
    page.image(FIGURE_PT, _png((400, 300)))
    if table_image:
        page.image((120, 520, 220, 600), _png((200, 160), "green"))
    return document.to_bytes()


def _artifact(extraction: StructuredExtraction, key: str) -> Any:
    return extraction.parser_artifacts[key]


def _px(points: Sequence[float], dpi: float) -> list[float]:
    return [value * dpi / 72 for value in points]


def _ratio(points: Sequence[float]) -> list[float]:
    """ページに対する割合(0-1)の bbox。解析エンジンに依らず換算できる単位。"""
    return [
        points[0] / PAGE_PT[0],
        points[1] / PAGE_PT[1],
        points[2] / PAGE_PT[0],
        points[3] / PAGE_PT[1],
    ]


class FakeDescriber:
    """rag_engine の PictureDescriber の決定論スタブ(切り出しの寸法と呼び出しを記録する)。"""

    api_mode = "test"
    max_tokens = 1

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

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
        with Image.open(crop_path) as image:
            size = image.size
            center = image.convert("RGB").getpixel((size[0] // 2, size[1] // 2))
        self.calls.append(
            {
                "size": size,
                "center": center,
                "kind": target_kind,
                "contexts": len(context_image_paths),
                "metadata": metadata,
                "prompt": rendered_prompt,
            }
        )
        if self.error is not None:
            raise self.error
        return {
            "retrieval_text": f"受注画面の登録ボタン{len(self.calls)}",
            "main_topic": "受注入力",
            "visible_buttons": ["登録"],
        }


def _read(
    extraction: StructuredExtraction,
    source: bytes,
    *,
    backend: str,
    content_type: str = "application/pdf",
    describer: FakeDescriber | None = None,
) -> tuple[StructuredExtraction, FakeDescriber]:
    describer = describer or FakeDescriber()
    result = read_figures_with_vision(
        extraction,
        source_bytes=source,
        content_type=content_type,
        file_name="manual.pdf" if content_type == "application/pdf" else "screen.png",
        parser_backend=backend,
        describer=describer,
        prompt_template=TEMPLATE,
    )
    return result, describer


def _expected_crop_width(points: Sequence[float]) -> float:
    # rag_engine の crop は bbox の外側に 8px の余白を付ける。
    return (points[2] - points[0]) * RENDER_DPI / 72 + 16


def _expected_crop_height(points: Sequence[float]) -> float:
    return (points[3] - points[1]) * RENDER_DPI / 72 + 16


def _figure_extraction(
    bbox: list[float],
    *,
    text: str = "OCR: 登録",
    element_metadata: dict[str, Any] | None = None,
    pages: list[ExtractionPage] | None = None,
) -> StructuredExtraction:
    return StructuredExtraction(
        raw_text=f"受注入力の手順\n\n{text}",
        elements=[
            DocumentElement(kind="title", text="受注入力の手順", element_id="el-0", page_number=1),
            DocumentElement(
                kind="figure",
                text=text,
                element_id="el-1",
                page_number=1,
                bbox=bbox,
                metadata=element_metadata or {},
            ),
        ],
        assets=[
            ExtractionAsset(
                asset_id="asset-1",
                kind="figure",
                page_number=1,
                bbox=bbox,
                alt_text=text,
                metadata={"element_id": "el-1"},
            )
        ],
        pages=pages or [ExtractionPage(page_number=1)],
    )


# 350 は実際の Unstructured(0.27.8、hi_res)の PDF の PixelSpace(A4 で 2893x4094。#502)。
@pytest.mark.parametrize("dpi", [200, 350], ids=["dpi200", "hi_res_350dpi"])
def test_unstructured_bbox_uses_coordinate_system_size(dpi: int) -> None:
    """Unstructured は coordinates の座標系(layout_width / height)の px を基準に切り出す。"""
    width, height = _px(PAGE_PT, dpi)
    extraction = _figure_extraction(
        _px(FIGURE_PT, dpi),
        element_metadata={"page_width": width, "page_height": height},
    )

    result, describer = _read(extraction, _pdf(), backend="unstructured")

    assert len(describer.calls) == 1
    call = describer.calls[0]
    assert call["contexts"] == 1  # 対象の切り出し + ページ全体の文脈の 2 枚
    assert abs(call["size"][0] - _expected_crop_width(FIGURE_PT)) <= 4
    assert "OCR: 登録" in json.dumps(call["metadata"], ensure_ascii=False)
    figure = result.elements[1]
    assert "受注画面の登録ボタン1" in figure.text
    assert figure.metadata["vision_status"] == "succeeded"
    assert figure.metadata["vision_source_text"] == "OCR: 登録"
    assert figure.metadata["vision_retrieval_text"] == "受注画面の登録ボタン1"
    assert result.assets[0].summary == "受注画面の登録ボタン1"
    assert "受注画面の登録ボタン1" in result.raw_text
    assert "OCR: 登録" not in result.raw_text
    assert _artifact(result, "vision") == {
        "enabled": True,
        "engine": "unstructured",
        "targets": 1,
        "succeeded": 1,
        "failed": 0,
        "discovery_failed": False,
    }
    # 検索の chunk(構造認識)に説明文が入る。
    chunks = chunk_extraction_with_strategy(result, strategy="structure_aware")
    assert any("受注画面の登録ボタン1" in chunk.text for chunk in chunks)


def test_unstructured_without_coordinate_system_is_not_cropped() -> None:
    """座標系の寸法が分からない px の bbox は切り出さず、理由と warning を残す。"""
    extraction = _figure_extraction(_px(FIGURE_PT, 200))

    result, describer = _read(extraction, _pdf(), backend="unstructured")

    assert describer.calls == []
    figure = result.elements[1]
    assert figure.text == "OCR: 登録"
    assert figure.metadata["vision_status"] == "skipped"
    assert figure.metadata["vision_skip_reason"] == "bbox_unit_unknown"
    assert WARNING_BBOX_UNIT_UNKNOWN in result.warnings


# ai-foundations-lab(engchina/ai-foundations-lab、commit 572e9fa の 20260819/)が、ページ画像に
# 重ねて確かめた換算の例(#512)。lab の tests/test_unstructured_adapter.py の座標と期待値だけを
# 使う(文書の本文は使わない)。
LAB_A4_PAGE = PageImage(
    page=1, width=2480, height=3500, pdf_width=595.0, pdf_height=841.0, image_path=""
)
# 350 dpi 相当の PixelSpace(2894x1930)の Unstructured を、300 dpi のページ画像(2481x1654)へ。
LAB_A5_LANDSCAPE_PAGE = PageImage(
    page=1, width=2481, height=1654, pdf_width=595.28, pdf_height=396.85, image_path=""
)


def test_unstructured_bbox_matches_ai_foundations_lab() -> None:
    """Unstructured の points(PixelSpace)を座標系の寸法で割り、ページ内へ収める(lab と同じ)。"""
    from rag_parser_core.registry import remap_external_ocr_output

    element = {
        "type": "Header",
        "text": "見出し",
        "metadata": {
            "page_number": 1,
            "coordinates": {
                "points": [[2484, 180], [2484, 233], [2894, 233], [2894, 180]],
                "system": "PixelSpace",
                "layout_width": 2894,
                "layout_height": 1930,
            },
        },
    }
    result = remap_external_ocr_output("unstructured", [element], source_profile=None)
    assert result.extraction is not None
    remapped = result.extraction.elements[0]
    assert remapped.bbox == [2484.0, 180.0, 2894.0, 233.0]

    mapped = page_px_bbox(
        remapped.bbox,
        parser_backend="unstructured",
        metadata=remapped.metadata,
        extraction_page=None,
        page=LAB_A5_LANDSCAPE_PAGE,
        source_is_image=False,
    )

    assert mapped == pytest.approx(
        [2484 * 2481 / 2894, 180 * 1654 / 1930, 2481.0, 233 * 1654 / 1930]
    )


def test_figure_ocr_record_names_its_picture() -> None:
    """図の中の文字の record は、図と同じ bbox に加えて持ち主の図の id を持つ(#3455)。"""
    from app.rag.vision import _adapter_records

    layout = _adapter_records(
        _figure_extraction([0.166, 0.3, 0.82, 0.6]),
        engine="unstructured",
        parser_backend="unstructured",
        pages=[LAB_A4_PAGE],
        source_is_image=False,
    )

    picture, ocr = (record for record in layout.records if record.category == "Picture")
    assert picture.raw_type == "picture"
    assert ocr.raw_type == "picture_ocr_text"
    assert ocr.bbox == picture.bbox
    assert ocr.raw == {"source_picture_ref": picture.id}


@pytest.mark.parametrize(
    ("system", "bbox", "size"),
    [
        ("PointSpace", [100.0, 592.0, 300.0, 742.0], (595.0, 842.0)),
        (
            "RelativeCoordinateSystem",
            [100 / 595, 592 / 842, 300 / 595, 742 / 842],
            (1.0, 1.0),
        ),
    ],
    ids=["point_space", "relative"],
)
def test_bottom_left_coordinate_system_is_flipped(
    system: str, bbox: list[float], size: tuple[float, float]
) -> None:
    """左下原点の座標系(Unstructured の CARTESIAN)は y を反転してページ画像の px にする。

    PDF の [100, 592, 300, 742](左下原点の pt)は、左上原点で FIGURE_PT の
    [100, 100, 300, 250]。lab の pdf_bottom_left_to_image_top_left と同じ換算(#512)。
    """
    page = PageImage(
        page=1,
        width=2480,
        height=3508,
        pdf_width=PAGE_PT[0],
        pdf_height=PAGE_PT[1],
        image_path="",
    )

    mapped = page_px_bbox(
        bbox,
        parser_backend="unstructured",
        metadata={
            "page_width": size[0],
            "page_height": size[1],
            "bbox_coordinate_system": system,
        },
        extraction_page=None,
        page=page,
        source_is_image=False,
    )

    assert mapped == pytest.approx(
        [
            FIGURE_PT[0] * 2480 / PAGE_PT[0],
            FIGURE_PT[1] * 3508 / PAGE_PT[1],
            FIGURE_PT[2] * 2480 / PAGE_PT[0],
            FIGURE_PT[3] * 3508 / PAGE_PT[1],
        ]
    )


def test_unstructured_point_space_figure_is_cropped_at_top_left_position() -> None:
    """PointSpace の図は、反転した位置(左上原点で FIGURE_PT)を切り出す。"""
    extraction = _figure_extraction(
        [FIGURE_PT[0], PAGE_PT[1] - FIGURE_PT[3], FIGURE_PT[2], PAGE_PT[1] - FIGURE_PT[1]],
        element_metadata={
            "page_width": PAGE_PT[0],
            "page_height": PAGE_PT[1],
            "bbox_coordinate_system": "PointSpace",
        },
    )

    result, describer = _read(extraction, _pdf(), backend="unstructured")

    assert len(describer.calls) == 1
    assert abs(describer.calls[0]["size"][0] - _expected_crop_width(FIGURE_PT)) <= 4
    assert abs(describer.calls[0]["size"][1] - _expected_crop_height(FIGURE_PT)) <= 4
    assert describer.calls[0]["center"] == (0, 0, 128)  # 図(navy)の中央を切り出した
    assert result.elements[1].metadata["vision_status"] == "succeeded"


def test_picture_asset_becomes_searchable_figure_element() -> None:
    """本文なしの Picture の asset(ページに対する割合)にも、説明を図の要素として足す。

    asset の kind は registry の変換の実際の値(picture)。#502 までは読み取りの対象外だった。
    """
    dpi = 200
    width, height = _px(PAGE_PT, dpi)
    extraction = StructuredExtraction(
        raw_text="受注入力の手順\n\n登録後に確認します。",
        elements=[
            DocumentElement(
                kind="title",
                text="受注入力の手順",
                element_id="el-0",
                page_number=1,
                bbox=_ratio((100, 60, 300, 85)),
            ),
            DocumentElement(
                kind="text",
                text="登録後に確認します。",
                element_id="el-2",
                page_number=1,
                bbox=_ratio((100, 300, 400, 320)),
            ),
        ],
        assets=[
            ExtractionAsset(
                asset_id="dots-picture-1",
                kind="picture",
                page_number=1,
                bbox=_ratio(FIGURE_PT),
                metadata={"parser_backend": "unstructured"},
            )
        ],
        pages=[ExtractionPage(page_number=1, width=width, height=height)],
    )

    result, describer = _read(extraction, _pdf(), backend="unstructured")

    assert len(describer.calls) == 1
    assert abs(describer.calls[0]["size"][0] - _expected_crop_width(FIGURE_PT)) <= 4
    kinds = [element.kind for element in result.elements]
    assert kinds == ["title", "figure", "text"]
    figure = result.elements[1]
    assert figure.element_id == "vision-dots-picture-1"
    assert figure.metadata["asset_id"] == "dots-picture-1"
    assert figure.metadata["vision_status"] == "succeeded"
    assert [element.order for element in result.elements] == [0, 1, 2]
    assert result.assets[0].summary == "受注画面の登録ボタン1"
    assert result.raw_text.index("受注画面の登録ボタン1") < result.raw_text.index("登録後に確認")


def test_parser_bbox_ratio() -> None:
    bbox = [
        FIGURE_PT[0] / PAGE_PT[0],
        FIGURE_PT[1] / PAGE_PT[1],
        FIGURE_PT[2] / PAGE_PT[0],
        FIGURE_PT[3] / PAGE_PT[1],
    ]
    result, describer = _read(_figure_extraction(bbox), _pdf(), backend="unstructured")

    assert len(describer.calls) == 1
    assert abs(describer.calls[0]["size"][0] - _expected_crop_width(FIGURE_PT)) <= 4
    assert result.elements[1].metadata["vision_status"] == "succeeded"


def test_source_image_asset_reads_whole_image() -> None:
    """bbox のない OCR の画像ファイル(source_image の asset)は画像全体を 1 枚の図として読む。"""
    image = _png((640, 480), "white")
    extraction = StructuredExtraction(
        raw_text="画面の文字",
        elements=[DocumentElement(kind="text", text="画面の文字", page_number=1)],
        assets=[
            ExtractionAsset(
                asset_id="source-image-0000",
                kind="source_image",
                page_number=1,
                bbox=[0.0, 0.0, 1.0, 1.0],
                metadata={"bbox_unit": "ratio", "bbox_scope": "source_image_full_frame"},
            )
        ],
    )

    result, describer = _read(extraction, image, backend="other_engine", content_type="image/png")

    assert len(describer.calls) == 1
    assert describer.calls[0]["size"] == (640, 480)
    assert [element.kind for element in result.elements] == ["figure", "text"]
    assert result.assets[0].summary == "受注画面の登録ボタン1"


def test_failure_keeps_original_text_and_warns() -> None:
    """1 件ごとの失敗は元の本文を保ち、vision_status=failed と warning を残す。"""
    extraction = _figure_extraction(
        [0.168, 0.119, 0.504, 0.297], element_metadata={"bbox_unit": "ratio"}
    )

    result, describer = _read(
        extraction,
        _pdf(),
        backend="unstructured",
        describer=FakeDescriber(error=RuntimeError("vlm down")),
    )

    assert len(describer.calls) == 1
    figure = result.elements[1]
    assert figure.text == "OCR: 登録"
    assert figure.metadata["vision_status"] == "failed"
    assert "vlm down" in str(figure.metadata["vision_error"])
    assert "vision_source_text" not in figure.metadata
    assert result.assets[0].summary is None
    assert WARNING_PARTIAL_FAILURE in result.warnings
    assert _artifact(result, "vision")["failed"] == 1


def test_already_read_figures_are_not_read_again() -> None:
    """再開時など、読み取り済み(succeeded)の図は読み直さない。"""
    extraction = _figure_extraction(
        [0.168, 0.119, 0.504, 0.297],
        text="読み取り済みの説明",
        element_metadata={"vision_status": "succeeded"},
    )

    result, describer = _read(extraction, _pdf(), backend="unstructured")

    assert describer.calls == []
    assert result.elements[1].text == "読み取り済みの説明"


def test_adapter_table_with_uncovered_image_is_read() -> None:
    """Picture で覆われていない画像を含む表も読み取り、表の本文に補足として足す。"""
    dpi = 200
    width, height = _px(PAGE_PT, dpi)
    table_pt = (100.0, 500.0, 500.0, 620.0)
    extraction = StructuredExtraction(
        raw_text="| 品目 | 画像 |\n| --- | --- |\n| 部品A | |",
        elements=[
            DocumentElement(
                kind="table",
                text="| 品目 | 画像 |\n| --- | --- |\n| 部品A | |",
                element_id="tbl-1",
                page_number=1,
                bbox=_px(table_pt, dpi),
                metadata={"page_width": width, "page_height": height},
            )
        ],
    )

    result, describer = _read(extraction, _pdf(table_image=True), backend="unstructured")

    assert [call["kind"] for call in describer.calls] == ["table"]
    table = result.elements[0]
    assert table.metadata["table_vision_text"]
    assert table.metadata["table_image_detection_status"] == "detected"
    assert "表内画像の補足" in table.text
    assert "受注画面の登録ボタン1" in result.raw_text


def _docling_extraction(*, decorative: bool = False) -> StructuredExtraction:
    width, height = round(PAGE_PT[0] * RENDER_DPI / 72), round(PAGE_PT[1] * RENDER_DPI / 72)
    picture_bbox = _px((20, 10, 50, 40), RENDER_DPI) if decorative else _px(FIGURE_PT, RENDER_DPI)

    def record(
        record_id: str, seq: int, category: str, text: str, bbox: list[float], raw_type: str
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
            "raw_type": raw_type,
            "raw": {},
        }

    records = [
        record("docling-p1-1", 1, "Title", "受注入力の手順", _px((100, 60, 300, 85), 300), "title"),
        record("docling-p1-2", 2, "Picture", "", picture_bbox, "picture"),
        record(
            "docling-p1-3",
            3,
            "Text",
            "登録後に確認します。",
            _px((100, 300, 400, 320), 300),
            "text",
        ),
    ]
    return StructuredExtraction(
        raw_text="受注入力の手順\n\n登録後に確認します。",
        elements=[
            DocumentElement(
                kind="title",
                text="受注入力の手順",
                element_id="docling-p1-1",
                page_number=1,
                source_parser="docling_layout",
            ),
            DocumentElement(
                kind="text",
                text="登録後に確認します。",
                element_id="docling-p1-3",
                page_number=1,
                source_parser="docling_layout",
            ),
        ],
        assets=[
            ExtractionAsset(
                asset_id="asset-docling-p1-2",
                kind="figure",
                page_number=1,
                bbox=picture_bbox,
                metadata={"element_id": "docling-p1-2", "vision_status": "", "visual_role": ""},
            )
        ],
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


def test_docling_vision_updates_layout_records_elements_and_assets() -> None:
    """Docling は layout_records の record を入力にし、結果を record と要素・asset へ書き戻す。"""
    result, describer = _read(_docling_extraction(), _pdf(), backend="docling")

    assert len(describer.calls) == 1
    assert abs(describer.calls[0]["size"][0] - _expected_crop_width(FIGURE_PT)) <= 4
    layout = _artifact(result, "layout_records")
    assert isinstance(layout, dict)
    picture = layout["records"][1]
    assert picture["raw"]["vision_status"] == "succeeded"
    assert picture["raw"]["vision_description"]["retrieval_text"] == "受注画面の登録ボタン1"
    assert "受注画面の登録ボタン1" in picture["text"]
    # 読み取る前は本文が空で要素がなかった図が、record の順の位置に要素として入る。
    assert [element.element_id for element in result.elements] == [
        "docling-p1-1",
        "docling-p1-2",
        "docling-p1-3",
    ]
    figure = result.elements[1]
    assert figure.kind == "figure"
    assert figure.metadata["vision_retrieval_text"] == "受注画面の登録ボタン1"
    assert figure.metadata["bbox_unit"] == "absolute"
    assert result.assets[0].summary == "受注画面の登録ボタン1"
    assert result.assets[0].metadata["vision_status"] == "succeeded"
    assert _artifact(result, "vision")["engine"] == "docling"
    # 親子階層（small-to-big）の分割は layout_records の record を読むので、説明文が chunk に入る。
    chunks = build_parent_child_chunks(result, source_name="manual.pdf")
    assert any("受注画面の登録ボタン1" in chunk.text for chunk in chunks)


def test_docling_decorative_picture_is_skipped() -> None:
    """ページの端の小さな画像(ロゴなど)は読み取らない(装飾画像の skip)。"""
    result, describer = _read(_docling_extraction(decorative=True), _pdf(), backend="docling")

    assert describer.calls == []
    layout = _artifact(result, "layout_records")
    assert isinstance(layout, dict)
    assert layout["records"][1]["raw"]["vision_status"] == "skipped"
    assert [element.element_id for element in result.elements] == [
        "docling-p1-1",
        "docling-p1-3",
    ]


def test_unsupported_source_is_reported() -> None:
    """PDF / 画像でない解析対象(切り出せない)は読み取らず warning を残す。"""
    extraction = _figure_extraction([0.1, 0.1, 0.5, 0.5])

    result, describer = _read(
        extraction,
        b"PK\x03\x04",
        backend="unstructured",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    assert describer.calls == []
    assert "vision_source_unsupported" in result.warnings


def test_vision_failure_keeps_extraction_and_warns() -> None:
    extraction = _figure_extraction([0.1, 0.1, 0.5, 0.5])

    result = vision_failure(extraction, "other_engine")

    assert result.elements == extraction.elements
    assert "vision_failed" in result.warnings


def test_parse_picture_description_fills_missing_keys_and_rejects_non_json() -> None:
    parsed = parse_picture_description(
        '```json\n{"retrieval_text": "説明", "main_topic": "図"}\n```'
    )

    assert parsed["retrieval_text"] == "説明"
    assert parsed["visible_buttons"] == []
    assert parsed["diagram_title"] == ""
    with pytest.raises(VisionResponseError):
        parse_picture_description("説明できません")
    with pytest.raises(VisionResponseError):
        parse_picture_description('{"retrieval_text": 1, "unknown": "x"}')


class FakeReader:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate_from_images(
        self,
        images: Sequence[bytes],
        prompt: str,
        *,
        system_prompt: str = "",
        response_schema: Any = None,
        response_schema_name: str = "image_description",
        mime_type: str = "image/png",
    ) -> str:
        self.calls.append(
            {
                "images": len(images),
                "prompt": prompt,
                "system_prompt": system_prompt,
                "schema": response_schema,
            }
        )
        return json.dumps({"retrieval_text": "既定の Vision モデルの説明"}, ensure_ascii=False)


async def test_enterprise_ai_describer_sends_target_and_context_images() -> None:
    """backend の VLM client へ対象と周辺文脈の 2 枚・system prompt・出力 schema を渡す。"""
    reader = FakeReader()
    describer = EnterpriseAiPictureDescriber(
        reader, asyncio.get_running_loop(), model_id="vendor.vision"
    )
    extraction = _figure_extraction(
        [0.168, 0.119, 0.504, 0.297], element_metadata={"bbox_unit": "ratio"}
    )

    result = await asyncio.to_thread(
        read_figures_with_vision,
        extraction,
        source_bytes=_pdf(),
        content_type="application/pdf",
        file_name="manual.pdf",
        parser_backend="unstructured",
        describer=describer,
        prompt_template=TEMPLATE,
    )

    assert len(reader.calls) == 1
    assert reader.calls[0]["images"] == 2
    assert "JSON" in reader.calls[0]["system_prompt"]
    assert "retrieval_text" in json.dumps(reader.calls[0]["schema"])
    assert describer.provider().model == "vendor.vision"
    assert "既定の Vision モデルの説明" in result.elements[1].text


class PromptOracle:
    def __init__(self, overrides: dict[str, str]) -> None:
        self.overrides = overrides

    async def answer_prompt_overrides(self) -> dict[str, str]:
        return self.overrides


def _pipeline(*, enabled: bool, reader: FakeReader | None = None) -> Any:
    from app.config import Settings
    from app.rag.ingestion import IngestionPipeline

    return IngestionPipeline(
        vlm=reader or FakeReader(),  # type: ignore[arg-type]
        oracle=PromptOracle({}),  # type: ignore[arg-type]
        settings=Settings(rag_vision_enabled=enabled),
    )


async def _attach(pipeline: Any, extraction: StructuredExtraction, backend: str) -> Any:
    return await pipeline._attach_vision(
        "trace",
        extraction,
        source_bytes=_pdf(),
        content_type="application/pdf",
        file_name="manual.pdf",
        parser_backend=backend,
        cancel_checker=None,
    )


async def test_ingestion_vision_disabled_keeps_extraction() -> None:
    reader = FakeReader()
    extraction = _figure_extraction([0.168, 0.119, 0.504, 0.297])

    result = await _attach(_pipeline(enabled=False, reader=reader), extraction, "other_engine")

    assert result is extraction
    assert reader.calls == []


async def test_ingestion_vision_uses_saved_image_retrieval_prompt() -> None:
    """文書解析の画面で編集した画像の読み取りの指示(image_retrieval)を全ての解析エンジンで使う。"""
    from rag_engine.knowledge.prompt_files import IMAGE_RETRIEVAL_PROMPT_KEY

    reader = FakeReader()
    pipeline = _pipeline(enabled=True, reader=reader)
    pipeline._oracle = PromptOracle(
        {IMAGE_RETRIEVAL_PROMPT_KEY: "独自の読み取り方針\n{{image_metadata}}"}
    )
    extraction = _figure_extraction([0.168, 0.119, 0.504, 0.297])

    result = await _attach(pipeline, extraction, "unstructured")

    assert len(reader.calls) == 1
    assert "独自の読み取り方針" in reader.calls[0]["prompt"]
    assert "既定の Vision モデルの説明" in result.elements[1].text


async def test_ingestion_vision_stage_failure_keeps_extraction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """段の全体の失敗(描画できない等)も取込は止めず、元の解析結果と warning を残す。"""
    from app.rag import ingestion as ingestion_module

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("render failed")

    monkeypatch.setattr(ingestion_module, "read_figures_with_vision", broken)
    extraction = _figure_extraction([0.168, 0.119, 0.504, 0.297])

    result = await _attach(_pipeline(enabled=True), extraction, "other_engine")

    assert result.elements == extraction.elements
    assert "vision_failed" in result.warnings


async def test_ingestion_vision_reports_item_progress_to_job() -> None:
    """取込の job の中では、図・表の説明の段の件数を job の進捗へ写す(#1999)。"""
    from app.rag.ingestion_progress import track_ingestion_progress

    written: list[object] = []

    async def writer(progress: object) -> None:
        written.append(progress)

    reader = FakeReader()
    async with track_ingestion_progress(writer, interval_seconds=60) as progress:
        await _attach(
            _pipeline(enabled=True, reader=reader),
            _figure_extraction([0.168, 0.119, 0.504, 0.297]),
            "unstructured",
        )
        snapshot = progress.snapshot()

    assert len(reader.calls) == 1
    assert written == [None]  # 始めに前の試行の進捗を消す。間隔の前は書かない。
    assert snapshot is not None
    assert snapshot.stage == "vision"
    assert snapshot.vision is not None
    assert snapshot.vision.finished is True
    assert snapshot.vision.succeeded == 1
    assert snapshot.vision.total_known == 1
    assert snapshot.vision.total_final is True
    assert snapshot.vision.running == 0


async def test_ingestion_vision_outside_job_does_not_track_progress() -> None:
    """job の外(同期の取込・評価)では進捗を記録しない。"""
    from app.rag.ingestion_progress import active_ingestion_progress

    await _attach(
        _pipeline(enabled=True), _figure_extraction([0.168, 0.119, 0.504, 0.297]), "other_engine"
    )

    assert active_ingestion_progress() is None


async def test_ingestion_vision_cancel_stops_reading() -> None:
    from app.rag.ingestion import IngestionCancelledError

    reader = FakeReader()

    async def cancelled() -> bool:
        return True

    with pytest.raises(IngestionCancelledError):
        await _pipeline(enabled=True, reader=reader)._attach_vision(
            "trace",
            _figure_extraction([0.168, 0.119, 0.504, 0.297]),
            source_bytes=_pdf(),
            content_type="application/pdf",
            file_name="manual.pdf",
            parser_backend="unstructured",
            cancel_checker=cancelled,
        )
    assert reader.calls == []


class _PixelSpace:
    def __init__(self, width: float, height: float) -> None:
        self.width = width
        self.height = height


def _unstructured_element(category: str, text: str, points: list[tuple[float, float]]) -> Any:
    coordinates = SimpleNamespace(points=points, system=_PixelSpace(1654.0, 2339.0))
    metadata = SimpleNamespace(coordinates=coordinates, page_number=1)
    return SimpleNamespace(category=category, text=text, metadata=metadata)


def test_unstructured_remap_keeps_coordinate_system_size_for_vision() -> None:
    """Unstructured の coordinates の座標系の寸法を、要素と図だけの asset の metadata に残す。"""
    from rag_parser_core.registry import remap_external_ocr_output

    figure_points = [(277.0, 277.0), (277.0, 694.0), (833.0, 694.0), (833.0, 277.0)]
    result = remap_external_ocr_output(
        "unstructured",
        [
            _unstructured_element("Title", "受注入力の手順", [(270, 150), (270, 200), (900, 200)]),
            _unstructured_element("Image", "", figure_points),
            _unstructured_element("Image", "登録ボタン", figure_points),
        ],
        source_profile=None,
    )

    assert result.extraction is not None
    title = result.extraction.elements[0]
    assert title.metadata["page_width"] == 1654.0
    assert title.metadata["page_height"] == 2339.0
    assert title.metadata["bbox_coordinate_system"] == "_PixelSpace"
    figure_only = result.extraction.assets[0]
    assert figure_only.metadata["page_width"] == 1654.0
    assert figure_only.metadata["page_height"] == 2339.0
    assert figure_only.bbox == [277.0, 277.0, 833.0, 694.0]


def test_unstructured_remap_reads_dict_coordinates() -> None:
    """dict 化した coordinates(layout_width / layout_height / system)も同じく読む。"""
    from rag_parser_core.registry import remap_external_ocr_output

    element = {
        "type": "NarrativeText",
        "text": "登録後に確認します。",
        "metadata": {
            "page_number": 1,
            "coordinates": {
                "points": [[100, 100], [100, 140], [600, 140], [600, 100]],
                "system": "PixelSpace",
                "layout_width": 1700,
                "layout_height": 2200,
            },
        },
    }

    result = remap_external_ocr_output("unstructured", [element], source_profile=None)

    assert result.extraction is not None
    metadata = result.extraction.elements[0].metadata
    assert (metadata["page_width"], metadata["page_height"]) == (1700.0, 2200.0)
    assert metadata["bbox_coordinate_system"] == "PixelSpace"
