"""MinerU の Middle JSON から作る LayoutRecord と、親子階層（small-to-big）・Vision（#1334）。

MinerU のサーバーは呼ばず、V1 API を ``httpx.MockTransport`` の決定論スタブにする。Middle JSON は
MinerU 4.0 の公開の契約（docvortex.middle 2.0・``extensions.docvortex_layout``）の形の固定の例。
"""

from __future__ import annotations

import io
import json
from typing import Any

import httpx
import pytest
from PIL import Image
from rag_pipeline_core.chunking import Chunk

from app.clients.external_parser import ExternalParserClient
from app.clients.mineru_layout import (
    MINERU_LAYOUT_CATEGORIES,
    mineru_layout_category,
    mineru_layout_records,
)
from app.rag.chunking_small_to_big import (
    PARENT_SOURCE_LAYOUT,
    PARENT_SOURCE_STRUCTURE,
    build_parent_child_chunks,
    has_layout_records,
    layout_source_parser,
    small_to_big_parent_source,
)
from app.rag.vision import _page_px_bbox as page_px_bbox
from app.rag.vision import read_figures_with_vision
from app.schemas.extraction import StructuredExtraction
from tests.fixtures.synthetic_pdf import SyntheticPdf
from tests.test_external_parser_client import (
    _install_transport,
    _mineru_handler,
    _mineru_settings,
    _profile,
)
from tests.test_vision import LAB_A4_PAGE, TEMPLATE, FakeDescriber

# A4 の PDF（pt）。300 dpi のページ画像は 2479x3508 px。
PAGE_PT = (595.0, 842.0)
PAGE_PX = (round(595.0 * 300 / 72), round(842.0 * 300 / 72))
# 2 頁目の図の位置（pt）。テストの PDF はここに画像を置く。
FIGURE_PT = (100.0, 300.0, 300.0, 450.0)


def _spans(text: str) -> list[dict[str, object]]:
    return [{"type": "text", "content": text}]


def _norm(points: tuple[float, float, float, float]) -> list[float]:
    return [
        points[0] / PAGE_PT[0],
        points[1] / PAGE_PT[1],
        points[2] / PAGE_PT[0],
        points[3] / PAGE_PT[1],
    ]


def _middle_json(*, geometry: bool = True) -> dict[str, object]:
    """見出し・本文・表と caption・図と caption・数式・コード・ヘッダー・フッター・頁またぎの例。"""
    document: dict[str, object] = {
        "schema": "docvortex.middle",
        "schema_version": "2.0",
        "is_full_document": True,
        "metadata": {"file_suffix": "pdf", "producer": {"name": "mineru", "version": "4.0.11"}},
        "extensions": {"mineru": {"tier": "basic", "parse_mode": "ocr"}},
        "pages": [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "header",
                        "index": 0,
                        "bbox": [0.1, 0.01, 0.9, 0.03],
                        "content": _spans("社内資料"),
                    },
                    {
                        "type": "doc_title",
                        "index": 1,
                        "level": 1,
                        "bbox": [0.1, 0.05, 0.9, 0.08],
                        "content": _spans("受注登録マニュアル"),
                    },
                    {
                        "type": "paragraph_title",
                        "index": 2,
                        "level": 2,
                        "bbox": [0.1, 0.1, 0.5, 0.12],
                        "content": _spans("1. 受注入力画面"),
                    },
                    {
                        "type": "text",
                        "index": 3,
                        "bbox": [0.1, 0.13, 0.9, 0.2],
                        "content": _spans("受注番号を入力し、登録ボタンを押します。"),
                    },
                    {
                        "type": "table",
                        "index": 4,
                        "bbox": [0.1, 0.25, 0.9, 0.4],
                        "content": [
                            {
                                "type": "table_caption",
                                "bbox": [0.1, 0.22, 0.5, 0.24],
                                "content": _spans("表1 入力項目"),
                            },
                            {
                                "type": "table_body",
                                "index": 4,
                                "bbox": [0.1, 0.25, 0.9, 0.4],
                                "content": (
                                    "<table><tr><th>項目</th><th>説明</th></tr>"
                                    "<tr><td>受注番号</td><td>必須</td></tr></table>"
                                ),
                            },
                            {"type": "table_footnote", "content": _spans("※ 半角で入力する。")},
                        ],
                    },
                    {
                        "type": "equation",
                        "index": 5,
                        "bbox": [0.1, 0.42, 0.5, 0.45],
                        "content": "a^2 + b^2 = c^2",
                    },
                    {
                        "type": "text",
                        "index": 6,
                        "bbox": [0.1, 0.8, 0.9, 0.9],
                        "content": _spans("受注の締め日は毎月末日で、締め日を過ぎた受注は"),
                    },
                    {
                        "type": "page_number",
                        "index": 7,
                        "bbox": [0.45, 0.96, 0.55, 0.98],
                        "content": _spans("1"),
                    },
                ],
            },
            {
                "page_idx": 1,
                "blocks": [
                    {
                        "type": "header",
                        "index": 7,
                        "bbox": [0.1, 0.01, 0.9, 0.03],
                        "content": _spans("社内資料"),
                    },
                    {
                        "type": "text",
                        "index": 0,
                        "continues_prev": True,
                        "bbox": [0.1, 0.05, 0.9, 0.1],
                        "content": _spans("翌月の受注として扱う。"),
                    },
                    {
                        "type": "paragraph_title",
                        "index": 1,
                        "level": 2,
                        "bbox": [0.1, 0.15, 0.5, 0.17],
                        "content": _spans("2. 登録後の確認"),
                    },
                    {
                        "type": "text",
                        "index": 2,
                        "bbox": [0.1, 0.2, 0.9, 0.3],
                        "content": _spans("登録後は一覧画面で状態が受付済みになったか確かめる。"),
                    },
                    {
                        "type": "image",
                        "index": 3,
                        "bbox": _norm(FIGURE_PT),
                        "content": [
                            {
                                "type": "image_body",
                                "index": 3,
                                "bbox": _norm(FIGURE_PT),
                                "content": "受注一覧\n受注番号 状態\nA-001 受付済み",
                            },
                            {
                                "type": "image_caption",
                                "bbox": [0.17, 0.54, 0.5, 0.56],
                                "content": _spans("図1 受注一覧画面"),
                            },
                        ],
                    },
                    {
                        "type": "code",
                        "index": 4,
                        "sub_type": "code",
                        "guess_lang": "python",
                        "bbox": [0.1, 0.6, 0.9, 0.65],
                        "content": [
                            {
                                "type": "code_body",
                                "index": 4,
                                "bbox": [0.1, 0.6, 0.9, 0.65],
                                "content": "print('ok')",
                            }
                        ],
                    },
                    {
                        "type": "callout",
                        "index": 5,
                        "bbox": [0.1, 0.7, 0.9, 0.75],
                        "content": _spans("注意: 取り消しは管理者だけが行える。"),
                    },
                    {
                        "type": "footer",
                        "index": 6,
                        "bbox": [0.1, 0.95, 0.9, 0.97],
                        "content": _spans("Copyright 2026 Example"),
                    },
                ],
            },
        ],
    }
    if geometry:
        document["extensions"] = {
            **document["extensions"],  # type: ignore[dict-item]
            "docvortex_layout": {
                "version": 1,
                "pages": [
                    {"page_idx": 0, "width_pt": PAGE_PT[0], "height_pt": PAGE_PT[1]},
                    {"page_idx": 1, "width_pt": PAGE_PT[0], "height_pt": PAGE_PT[1]},
                ],
            },
        }
    return document


def _pdf() -> bytes:
    """2 頁の A4 の PDF。2 頁目の ``FIGURE_PT`` に画像を置く（Vision の切り出しを確かめる）。

    PyMuPDF（AGPL）は使わず、合成の PDF（reportlab）で作る（#3343）。
    """
    buffer = io.BytesIO()
    Image.new("RGB", (400, 300), "navy").save(buffer, format="PNG")
    document = SyntheticPdf()
    document.page(*PAGE_PT)
    document.page(*PAGE_PT).image(FIGURE_PT, buffer.getvalue())
    return document.to_bytes()


def _parse(
    monkeypatch: pytest.MonkeyPatch, middle_json: dict[str, object], source: bytes
) -> StructuredExtraction:
    def download(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/files/file-mj/content":
            return httpx.Response(200, json=middle_json)
        return httpx.Response(404, json={"detail": request.url.path})

    _install_transport(monkeypatch, _mineru_handler([], download=download))
    result = ExternalParserClient(_mineru_settings()).parse(
        "mineru", source, _profile(), "application/pdf"
    )
    assert result.extraction is not None
    return result.extraction


def _records(extraction: StructuredExtraction) -> list[dict[str, Any]]:
    layout: Any = extraction.parser_artifacts["layout_records"]
    assert isinstance(layout, dict)
    return list(layout["records"])


def _text(chunk: Chunk, key: str) -> str:
    value = chunk.metadata[key]
    assert isinstance(value, str)
    return value


def test_mineru_parse_keeps_layout_records_like_docling(monkeypatch: pytest.MonkeyPatch) -> None:
    extraction = _parse(monkeypatch, _middle_json(), _pdf())

    layout = extraction.parser_artifacts["layout_records"]
    assert isinstance(layout, dict)
    assert layout["version"] == 1
    # 頁の寸法は Docling の layout_records.pages と同じ項目（300 dpi の画像の px と PDF の pt）。
    page_size = {"width": PAGE_PX[0], "height": PAGE_PX[1], "pdf_width": 595.0, "pdf_height": 842.0}
    assert layout["pages"] == [{"page": 1, **page_size}, {"page": 2, **page_size}]
    records = _records(extraction)
    assert [(r["id"], r["page"], r["seq_no"], r["category"], r["raw_type"]) for r in records] == [
        ("mineru-p1-b0", 1, 1, "Page-header", "page_header"),
        ("mineru-p1-b1", 1, 2, "Title", "title"),
        ("mineru-p1-b2", 1, 3, "Section-header", "section_header"),
        ("mineru-p1-b3", 1, 4, "Text", "text"),
        ("mineru-p1-b4-caption1", 1, 5, "Caption", "caption"),
        ("mineru-p1-b4", 1, 6, "Table", "table"),
        ("mineru-p1-b4-footnote1", 1, 7, "Footnote", "footnote"),
        ("mineru-p1-b5", 1, 8, "Formula", "formula"),
        ("mineru-p1-b6", 1, 9, "Text", "text"),
        ("mineru-p1-b7", 1, 10, "Page-footer", "page_footer"),
        ("mineru-p2-b7", 2, 1, "Page-header", "page_header"),
        ("mineru-p2-b0", 2, 2, "Text", "text"),
        ("mineru-p2-b1", 2, 3, "Section-header", "section_header"),
        ("mineru-p2-b2", 2, 4, "Text", "text"),
        ("mineru-p2-b3", 2, 5, "Picture", "picture"),
        ("mineru-p2-b3-ocr", 2, 6, "Picture", "picture_ocr_text"),
        ("mineru-p2-b3-caption1", 2, 7, "Caption", "caption"),
        ("mineru-p2-b4", 2, 8, "Text", "code"),
        ("mineru-p2-b5", 2, 9, "Text", "callout"),
        ("mineru-p2-b6", 2, 10, "Page-footer", "page_footer"),
    ]
    by_id = {record["id"]: record for record in records}
    assert all(record["engine"] == "mineru" for record in records)
    assert all(record["coord_system"] == "image_top_left" for record in records)
    # bbox は Middle JSON の 0〜1 にページ画像の px を掛けた値（左上原点）。
    assert by_id["mineru-p1-b3"]["bbox"] == pytest.approx(
        [0.1 * PAGE_PX[0], 0.13 * PAGE_PX[1], 0.9 * PAGE_PX[0], 0.2 * PAGE_PX[1]]
    )
    # 表の本文は HTML、caption は子の bbox、bbox の無い footnote は表の bbox。
    assert by_id["mineru-p1-b4"]["text"].startswith("<table>")
    assert by_id["mineru-p1-b4-caption1"]["bbox"][1] == pytest.approx(0.22 * PAGE_PX[1])
    assert by_id["mineru-p1-b4-footnote1"]["bbox"] == by_id["mineru-p1-b4"]["bbox"]
    # 図は本文を空にして Vision の対象にし、図の中の文字は同じ bbox の picture_ocr_text にする。
    assert by_id["mineru-p2-b3"]["text"] == ""
    assert by_id["mineru-p2-b3-ocr"]["bbox"] == by_id["mineru-p2-b3"]["bbox"]
    assert "A-001 受付済み" in by_id["mineru-p2-b3-ocr"]["text"]
    assert by_id["mineru-p1-b5"]["text"] == "a^2 + b^2 = c^2"
    assert by_id["mineru-p2-b4"]["text"] == "print('ok')"
    # raw に MinerU の元の block の番号・頁・種類と、続きの印・見出しの深さ・コードの印を残す。
    assert by_id["mineru-p2-b0"]["raw"] == {
        "mineru_type": "text",
        "mineru_page_idx": 1,
        "mineru_block_index": 0,
        "continues_prev": True,
    }
    assert by_id["mineru-p1-b2"]["raw"]["mineru_level"] == 2
    assert by_id["mineru-p2-b4"]["raw"]["code"] is True
    assert by_id["mineru-p2-b4"]["raw"]["guess_lang"] == "python"
    assert by_id["mineru-p2-b3-caption1"]["raw"]["mineru_parent_record_id"] == "mineru-p2-b3"
    # 要素（本文）は今までどおり。ヘッダー・フッター・頁番号は要素に入れない。
    element_ids = [element.element_id for element in extraction.elements]
    assert "mineru-p1-b0" not in element_ids
    assert "mineru-p1-b7" not in element_ids
    assert "mineru-p1-b3" in element_ids
    # record の id は要素の element_id と同じ（chunk の element_ids・#1330 の定位子）。
    assert {element_id for element_id in element_ids if element_id} <= set(by_id)


def test_unknown_mineru_block_type_maps_to_text_with_raw_type() -> None:
    assert mineru_layout_category("callout") == ("Text", "callout")
    assert mineru_layout_category("doc_title") == ("Title", "title")
    assert mineru_layout_category("paragraph_title") == ("Section-header", "section_header")
    assert mineru_layout_category("image") == ("Picture", "picture")
    assert mineru_layout_category("chart") == ("Picture", "picture")
    assert mineru_layout_category("page_number") == ("Page-footer", "page_footer")
    assert mineru_layout_category("page_footnote") == ("Footnote", "footnote")
    # 分割が分岐する Docling の category だけを使う。
    assert {category for category, _ in MINERU_LAYOUT_CATEGORIES.values()} <= {
        "Text",
        "Title",
        "Section-header",
        "List-item",
        "Table",
        "Picture",
        "Caption",
        "Footnote",
        "Formula",
        "Page-header",
        "Page-footer",
    }


def test_small_to_big_uses_mineru_layout_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    extraction = _parse(monkeypatch, _middle_json(), _pdf())

    assert has_layout_records(extraction)
    # MinerU のレイアウトの記録から親子を作る(Docling と同じ入力。#1334・#3377)。
    assert small_to_big_parent_source(extraction) == PARENT_SOURCE_LAYOUT
    chunks = build_parent_child_chunks(extraction, source_name="manual.pdf")

    assert chunks
    assert all("chunk_strategy_fallback_reason" not in chunk.metadata for chunk in chunks)
    assert {chunk.metadata["chunk_strategy"] for chunk in chunks} == {"small_to_big"}
    assert {chunk.metadata["source_parser"] for chunk in chunks} == {"mineru_layout"}
    text = "\n".join(chunk.text for chunk in chunks)
    # 頁の定型（毎頁のヘッダー・著作権のフッター・頁番号）は chunk の本文に入れない。
    assert "社内資料" not in text
    assert "Copyright" not in text
    body = next(chunk for chunk in chunks if "受注番号を入力" in chunk.text)
    assert body.metadata["section_path"] == "受注登録マニュアル > 1. 受注入力画面"
    assert body.metadata["page_number"] == 1
    assert body.metadata["page_width"] == PAGE_PX[0]
    assert body.metadata["page_height"] == PAGE_PX[1]
    assert body.metadata["bbox_unit"] == "absolute"
    # bbox は子の record（見出しと本文）を包むページ画像の px。
    x0, y0, x1, y1 = json.loads(_text(body, "bbox"))
    assert (x0, x1) == pytest.approx((0.1 * PAGE_PX[0], 0.9 * PAGE_PX[0]))
    assert y0 <= 0.13 * PAGE_PX[1] and y1 == pytest.approx(0.2 * PAGE_PX[1])
    assert "mineru-p1-b3" in _text(body, "element_ids").split(",")
    # 親の本文（節）を子の metadata に持つ。
    assert "受注番号を入力" in _text(body, "parent_text")
    # 表は caption・footnote と同じ子にまとまる。
    table = next(chunk for chunk in chunks if chunk.metadata["content_kind"] == "table")
    assert "表1 入力項目" in table.text
    assert "受注番号" in table.text
    # 2 頁目の見出しの下の本文は、2 頁目の節に入る。
    confirm = next(chunk for chunk in chunks if "受付済み" in chunk.text)
    assert _text(confirm, "section_path").endswith("2. 登録後の確認")
    assert confirm.metadata["page_number"] == 2
    # 図は caption・図の中の文字と 1 つの子になる（Vision を使わない文書）。
    figure = next(chunk for chunk in chunks if chunk.metadata["content_kind"] == "figure")
    assert "図1 受注一覧画面" in figure.text
    assert "A-001 受付済み" in figure.text
    assert figure.metadata["figure_text_source"] == "ocr"
    # 対応表に無い種類の block も本文として残る。
    assert "取り消しは管理者だけ" in text


def test_mineru_without_page_geometry_uses_extraction_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """頁の寸法が分からない Middle JSON は record を作らず、抽出の構造から親子を作る(#3377)。"""
    extraction = _parse(monkeypatch, _middle_json(geometry=False), _pdf())

    assert "layout_records" not in extraction.parser_artifacts
    assert small_to_big_parent_source(extraction) == PARENT_SOURCE_STRUCTURE


def test_mineru_layout_records_need_bbox_for_every_block() -> None:
    middle_json = _middle_json()
    pages = middle_json["pages"]
    assert isinstance(pages, list)
    del pages[0]["blocks"][3]["bbox"]

    assert (
        mineru_layout_records(middle_json, source_bytes=b"", content_type="application/pdf") is None
    )


def test_mineru_office_document_has_no_layout_records() -> None:
    assert (
        mineru_layout_records(
            _middle_json(),
            source_bytes=b"PK",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        is None
    )


def test_mineru_image_uses_source_image_pixels() -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (1200, 800), "white").save(buffer, format="PNG")
    middle_json = _middle_json()
    pages = middle_json["pages"]
    assert isinstance(pages, list)
    middle_json["pages"] = pages[:1]

    layout = mineru_layout_records(
        middle_json, source_bytes=buffer.getvalue(), content_type="image/png"
    )

    assert layout is not None
    assert layout["pages"] == [
        {"page": 1, "width": 1200, "height": 800, "pdf_width": 1200.0, "pdf_height": 800.0}
    ]
    text = next(record for record in layout["records"] if record["id"] == "mineru-p1-b3")
    assert text["bbox"] == pytest.approx([120.0, 104.0, 1080.0, 160.0])


@pytest.mark.parametrize(
    ("bbox", "expected"),
    [
        ([166, 810, 820, 829], [411.68, 2835.0, 2033.6, 2901.5]),
        ([277, 554, 705, 574], [686.96, 1939.0, 1748.4, 2009.0]),
    ],
    ids=["content_list", "content_list_v2"],
)
def test_mineru_record_bbox_matches_ai_foundations_lab(
    bbox: list[int], expected: list[float]
) -> None:
    """record の bbox は、#512 で確かめた要素の bbox の換算（``_page_px_bbox``）と一致する。"""
    # lab の A4 のページ画像（2480x3500 px）= 300 dpi の 595.2x840 pt。
    middle_json: dict[str, object] = {
        "schema": "docvortex.middle",
        "schema_version": "2.0",
        "extensions": {
            "docvortex_layout": {
                "version": 1,
                "pages": [{"page_idx": 0, "width_pt": 595.2, "height_pt": 840.0}],
            }
        },
        "pages": [
            {
                "page_idx": 0,
                "blocks": [
                    {
                        "type": "text",
                        "index": 0,
                        "bbox": [value / 1000 for value in bbox],
                        "content": _spans("本文"),
                    }
                ],
            }
        ],
    }

    layout = mineru_layout_records(middle_json, source_bytes=b"", content_type="application/pdf")

    assert layout is not None
    assert (layout["pages"][0]["width"], layout["pages"][0]["height"]) == (
        LAB_A4_PAGE.width,
        LAB_A4_PAGE.height,
    )
    record_bbox = layout["records"][0]["bbox"]
    assert record_bbox == pytest.approx(expected)
    element_bbox = page_px_bbox(
        bbox,
        parser_backend="mineru",
        metadata={},
        extraction_page=None,
        page=LAB_A4_PAGE,
        source_is_image=False,
    )
    assert element_bbox == pytest.approx(record_bbox)


def test_mineru_vision_writes_back_to_records_elements_and_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _pdf()
    extraction = _parse(monkeypatch, _middle_json(), source)
    describer = FakeDescriber()

    result = read_figures_with_vision(
        extraction,
        source_bytes=source,
        content_type="application/pdf",
        file_name="manual.pdf",
        parser_backend="mineru",
        describer=describer,
        prompt_template=TEMPLATE,
    )

    # 図は 1 回だけ読み、切り出しは図の位置（300 dpi の px）。
    assert len(describer.calls) == 1
    width, height = describer.calls[0]["size"]
    # rag_engine の切り出しは bbox の外側に 8px の余白を付ける（Docling と同じ）。
    assert width == pytest.approx((FIGURE_PT[2] - FIGURE_PT[0]) * 300 / 72 + 16, abs=4)
    assert height == pytest.approx((FIGURE_PT[3] - FIGURE_PT[1]) * 300 / 72 + 16, abs=4)
    assert describer.calls[0]["center"] == (0, 0, 128)
    summary: Any = result.parser_artifacts["vision"]
    assert summary["engine"] == "mineru"
    assert summary["succeeded"] == 1
    # record に読み取りの結果が入る（親子階層の分割と画面の「Vision の読み取り内容」）。
    picture = next(record for record in _records(result) if record["id"] == "mineru-p2-b3")
    assert picture["text"]
    assert picture["raw"]["vision_description"]["retrieval_text"] == "受注画面の登録ボタン1"
    assert picture["raw"]["mineru_block_index"] == 3
    # 要素は Docling 以外の解析エンジンと同じ形で書き戻す（構造認識などの分割方式で使う）。
    figure = next(element for element in result.elements if element.element_id == "mineru-p2-b3")
    assert figure.text == picture["text"]
    assert figure.metadata["vision_status"] == "succeeded"
    assert figure.metadata["vision_source_text"] == "図1 受注一覧画面"
    # 親子階層の図の子は Vision の説明を本文にする。
    chunks = build_parent_child_chunks(result, source_name="manual.pdf")
    figure_chunk = next(chunk for chunk in chunks if chunk.metadata["content_kind"] == "figure")
    assert "受注画面の登録ボタン1" in _text(figure_chunk, "engine_search_text")
    assert figure_chunk.metadata["figure_text_source"] == "vision"


def test_mineru_vision_reads_asset_only_figure_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """caption の無い図（要素が無く asset だけの図）も 1 回だけ読み、説明を要素として入れる。"""
    middle_json = _middle_json()
    pages = middle_json["pages"]
    assert isinstance(pages, list)
    image = pages[1]["blocks"][4]
    assert image["type"] == "image"
    image["content"] = [image["content"][0] | {"content": ""}]
    source = _pdf()
    extraction = _parse(monkeypatch, middle_json, source)
    assert "mineru-p2-b3" not in {element.element_id for element in extraction.elements}

    describer = FakeDescriber()
    result = read_figures_with_vision(
        extraction,
        source_bytes=source,
        content_type="application/pdf",
        file_name="manual.pdf",
        parser_backend="mineru",
        describer=describer,
        prompt_template=TEMPLATE,
    )

    assert len(describer.calls) == 1
    figures = [element for element in result.elements if element.kind == "figure"]
    assert [element.text for element in figures] == [
        next(r["text"] for r in _records(result) if r["id"] == "mineru-p2-b3")
    ]
    # 図は前の要素（2 頁目の本文）の後に入る。
    index = result.elements.index(figures[0])
    assert "受付済み" in result.elements[index - 1].text
    asset = next(a for a in result.assets if a.metadata.get("element_id") == "mineru-p2-b3")
    assert asset.summary == "受注画面の登録ボタン1"


def test_mineru_page_continuation_joins_child_chunk_after_consolidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MinerU の ``continues_prev`` は文書全体の整理で続きの元に写り、同じ子に入る(#2266)。"""
    from app.rag.chunking_strategy import SmallToBigParams
    from app.rag.pdf_segment_merge import consolidate_document_extraction

    middle = _middle_json()
    pages: Any = middle["pages"]
    # 印が無ければ頁の境目で子が分かれる長さにする(子の目標は 300 文字)。
    first = next(block for block in pages[0]["blocks"] if block.get("index") == 6)
    first["content"] = _spans("前の頁の本文" * 30)
    second = next(block for block in pages[1]["blocks"] if block.get("index") == 0)
    second["content"] = _spans("続きの本文" * 30 + "。")
    parsed = _parse(monkeypatch, middle, _pdf())
    extraction = consolidate_document_extraction(parsed, source_bytes=_pdf())

    by_id = {record["id"]: record for record in _records(extraction)}
    assert by_id["mineru-p2-b0"]["raw"]["continues_record_id"] == "mineru-p1-b6"
    params = SmallToBigParams(child_target_chars=300)

    def child_with_continuation(source: StructuredExtraction) -> Chunk:
        chunks = build_parent_child_chunks(source, source_name="manual.pdf", params=params)
        return next(chunk for chunk in chunks if "続きの本文続きの本文" in chunk.text)

    assert "前の頁の本文" not in child_with_continuation(parsed).text
    assert "前の頁の本文" in child_with_continuation(extraction).text


def test_mineru_table_cell_merge_is_kept_in_raw() -> None:
    middle = _middle_json()
    pages: Any = middle["pages"]
    table = next(block for block in pages[0]["blocks"] if block["type"] == "table")
    table["cell_merge"] = [0, 1]

    layout = mineru_layout_records(middle, source_bytes=_pdf(), content_type="application/pdf")

    assert layout is not None
    record = next(r for r in layout["records"] if r["id"] == f"mineru-p1-b{table['index']}")
    assert record["raw"]["cell_merge"] == [0, 1]


def test_layout_source_parser_names_the_engine() -> None:
    assert layout_source_parser("docling") == "docling_layout"
    assert layout_source_parser("") == "docling_layout"
    assert layout_source_parser("mineru") == "mineru_layout"
