"""解析結果プレビュー用の crop API(PDF / 画像の bbox 切り出し)。"""

import io
from types import SimpleNamespace
from typing import Any, cast

import pytest
from PIL import Image

from app.api.routes import documents as documents_route
from app.main import app
from app.rag import document_crop
from tests.fixtures.synthetic_pdf import SyntheticPdf, blank_pdf, encrypted_pdf, png
from tests.support import AsgiTestClient

client = AsgiTestClient(app)


def _pdf_bytes() -> bytes:
    document = SyntheticPdf()
    document.page(600, 800).rect((100, 100, 300, 200), fill=(1, 0, 0))
    return document.to_bytes()


def _png_bytes() -> bytes:
    return png((200, 100), (255, 255, 255))


def _install(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    detail = SimpleNamespace(object_storage_path="docs/doc-1/source", preprocess_artifact=None)

    class FakeOracle:
        async def get_document(self, document_id: str) -> object | None:
            return detail if document_id == "doc-1" else None

    class FakeStorage:
        async def get(self, path: str) -> bytes:
            assert path == "docs/doc-1/source"
            return data

    monkeypatch.setattr(documents_route, "OracleClient", FakeOracle)
    monkeypatch.setattr(document_crop, "FileStorageClient", FakeStorage)


def _size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as image:
        return image.width, image.height


def test_crop_pdf_region_from_page_image_coordinates(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _pdf_bytes())

    # 1200x1600 px のページ画像座標(2 倍)で指定した bbox は PDF の 100..300 x 100..200 pt。
    response = client.get(
        "/api/documents/doc-1/crop",
        params={
            "page": 1,
            "x0": 200,
            "y0": 200,
            "x1": 600,
            "y1": 400,
            "page_width": 1200,
            "page_height": 1600,
            "dpi": 72,
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert _size(response.content) == (200, 100)


def test_crop_image_source_and_invalid_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _png_bytes())
    ok = client.get(
        "/api/documents/doc-1/crop",
        params={
            "page": 1,
            "x0": 0,
            "y0": 0,
            "x1": 100,
            "y1": 50,
            "page_width": 200,
            "page_height": 100,
        },
    )
    assert ok.status_code == 200

    reversed_box = client.get(
        "/api/documents/doc-1/crop",
        params={
            "page": 1,
            "x0": 50,
            "y0": 0,
            "x1": 10,
            "y1": 50,
            "page_width": 200,
            "page_height": 100,
        },
    )
    assert reversed_box.status_code == 422
    beyond_page = client.get(
        "/api/documents/doc-1/crop",
        params={
            "page": 3,
            "x0": 0,
            "y0": 0,
            "x1": 10,
            "y1": 10,
            "page_width": 200,
            "page_height": 100,
        },
    )
    assert beyond_page.status_code == 422
    missing = client.get(
        "/api/documents/other/crop",
        params={
            "page": 1,
            "x0": 0,
            "y0": 0,
            "x1": 10,
            "y1": 10,
            "page_width": 200,
            "page_height": 100,
        },
    )
    assert missing.status_code == 404


def _install_preview(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    """プレビューのページ画像 API 用(原本の読み出しは documents route の FileStorageClient)。"""
    detail = SimpleNamespace(
        object_storage_path="docs/doc-1/source",
        preprocess_artifact=None,
        file_name="source.pdf",
        content_type="application/pdf",
    )

    class FakeOracle:
        async def get_document(self, document_id: str) -> object | None:
            return detail if document_id == "doc-1" else None

    class FakeStorage:
        async def get(self, path: str) -> bytes:
            assert path == "docs/doc-1/source"
            return data

    monkeypatch.setattr(documents_route, "OracleClient", FakeOracle)
    monkeypatch.setattr(documents_route, "FileStorageClient", FakeStorage)


def _rotated_two_page_pdf() -> bytes:
    # 2 頁目は 600x800 pt の頁を 90 度回転（表示は 800x600）。
    return blank_pdf((600, 800), (800, 600), rotations=(0, 90))


def test_preview_pages_lists_page_sizes_with_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    """PDF のページ画像プレビュー: ページ数と、/Rotate を反映した向きの寸法を返す(#349)。"""
    _install_preview(monkeypatch, _rotated_two_page_pdf())

    response = client.get("/api/documents/doc-1/preview-pages")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["page_count"] == 2
    assert data["pages"] == [
        {"page_number": 1, "width": 600.0, "height": 800.0},
        {"page_number": 2, "width": 800.0, "height": 600.0},
    ]


def test_preview_page_image_renders_whole_page_at_requested_dpi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_preview(monkeypatch, _rotated_two_page_pdf())

    first = client.get("/api/documents/doc-1/preview-pages/1", params={"dpi": 72})
    assert first.status_code == 200
    assert first.headers["content-type"] == "image/png"
    assert first.headers["cache-control"] == "private, max-age=300"
    assert _size(first.content) == (600, 800)

    # 回転したページは回転後の向きで描く(解析の bbox と同じ向き)。倍率は dpi / 72。
    rotated = client.get("/api/documents/doc-1/preview-pages/2", params={"dpi": 144})
    assert rotated.status_code == 200
    assert _size(rotated.content) == (1600, 1200)


def test_preview_page_image_rejects_invalid_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_preview(monkeypatch, _pdf_bytes())

    assert client.get("/api/documents/doc-1/preview-pages/2").status_code == 422
    assert client.get("/api/documents/doc-1/preview-pages/0").status_code == 422
    assert (
        client.get("/api/documents/doc-1/preview-pages/1", params={"dpi": 1000}).status_code == 422
    )
    assert client.get("/api/documents/other/preview-pages").status_code == 404
    assert client.get("/api/documents/other/preview-pages/1").status_code == 404


def test_preview_pages_rejects_files_that_are_not_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_preview(monkeypatch, b"\x00\x01not-a-document")

    response = client.get("/api/documents/doc-1/preview-pages")

    assert response.status_code == 422


@pytest.mark.parametrize(
    "data",
    [encrypted_pdf(), b"%PDF-1.7\nbroken"],
    ids=["encrypted", "broken"],
)
def test_preview_and_crop_reject_encrypted_and_broken_pdfs(
    monkeypatch: pytest.MonkeyPatch, data: bytes
) -> None:
    """暗号化・破損した PDF は PDFium で開けず、422（ValueError）になる（#3343）。"""
    _install_preview(monkeypatch, data)
    assert client.get("/api/documents/doc-1/preview-pages").status_code == 422
    assert client.get("/api/documents/doc-1/preview-pages/1").status_code == 422
    with pytest.raises(ValueError, match="切り出し"):
        document_crop.crop_png(data, 1, (0, 0, 10, 10), (100, 100))


def test_render_page_png_caps_large_pages() -> None:
    data = blank_pdf((14400, 14400))

    width, height = _size(document_crop.render_page_png(data, 1, dpi=288))

    assert width * height <= document_crop.MAX_PAGE_PIXELS
    assert abs(width - 4000) <= 1


def test_recipe_preview_pages_use_recipe_prepared_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """レシピのプレビューは、そのレシピのファイル準備後 artifact(変換済み PDF)を描く。"""
    detail = SimpleNamespace(
        object_storage_path="docs/doc-1/source.docx",
        preprocess_artifact=None,
        file_name="source.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    prepared = _pdf_bytes()

    class FakeOracle:
        async def get_document(self, document_id: str) -> object | None:
            return detail if document_id == "doc-1" else None

        async def get_document_recipe(self, document_id: str, recipe_id: str) -> object | None:
            if recipe_id != "recipe-1":
                return None
            return {
                "preprocess_artifact": {
                    "derivation_id": "d-1",
                    "profile": "office_to_pdf",
                    "converted": True,
                    "object_storage_path": "docs/doc-1/recipe-1/prepared.pdf",
                    "content_type": "application/pdf",
                    "file_name": "prepared.pdf",
                }
            }

    class FakeStorage:
        async def get(self, path: str) -> bytes:
            assert path == "docs/doc-1/recipe-1/prepared.pdf"
            return prepared

    monkeypatch.setattr(documents_route, "OracleClient", FakeOracle)
    monkeypatch.setattr(documents_route, "FileStorageClient", FakeStorage)

    pages = client.get(
        "/api/documents/doc-1/recipes/recipe-1/preview-pages", params={"variant": "prepared"}
    )
    assert pages.status_code == 200
    assert pages.json()["data"]["page_count"] == 1
    image = client.get(
        "/api/documents/doc-1/recipes/recipe-1/preview-pages/1",
        params={"variant": "prepared", "dpi": 72},
    )
    assert image.status_code == 200
    assert _size(image.content) == (600, 800)
    missing = client.get("/api/documents/doc-1/recipes/other/preview-pages")
    assert missing.status_code == 404


# ---------------------------------------------------------------------------
# MCP の図の画像（上限つきの切り出しと、処理レシピのファイル。#1282）
# ---------------------------------------------------------------------------


def test_crop_png_bounded_limits_long_edge_and_upscales_small_figures() -> None:
    data = _pdf_bytes()

    # 頁全体(600x800 pt)を長い辺 400 px 以下にする。
    whole = document_crop.crop_png_bounded(
        data, 1, (0, 0, 600, 800), (600, 800), max_edge=400, max_bytes=1_000_000
    )
    assert (whole.width, whole.height) == _size(whole.png)
    assert max(whole.width, whole.height) <= 400
    # 小さな図(200x100 pt)は max_dpi まで拡大する(200 dpi で約 556x278 px)。
    small = document_crop.crop_png_bounded(
        data, 1, (100, 100, 300, 200), (600, 800), max_edge=1568, max_bytes=1_000_000
    )
    assert 540 <= small.width <= 560
    assert 270 <= small.height <= 285


def test_crop_png_bounded_rejects_too_large_and_invalid_regions() -> None:
    data = _pdf_bytes()

    with pytest.raises(document_crop.CropTooLargeError):
        document_crop.crop_png_bounded(
            data, 1, (0, 0, 600, 800), (600, 800), max_edge=1568, max_bytes=10
        )
    with pytest.raises(ValueError, match="切り出し範囲"):
        document_crop.crop_png_bounded(
            data, 1, (0, 0, 700, 800), (600, 800), max_edge=400, max_bytes=1_000_000
        )
    with pytest.raises(ValueError, match="ページ番号"):
        document_crop.crop_png_bounded(
            data, 2, (0, 0, 600, 800), (600, 800), max_edge=400, max_bytes=1_000_000
        )


async def test_load_parsed_source_uses_recipe_prepared_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """図の bbox はレシピが解析したファイルの座標なので、そのレシピの artifact を読む。"""
    detail = SimpleNamespace(object_storage_path="docs/doc-1/source.docx", preprocess_artifact=None)
    read: list[str] = []

    class FakeOracle:
        async def get_document(self, document_id: str) -> object | None:
            return detail if document_id == "doc-1" else None

        async def get_document_recipe(self, document_id: str, recipe_id: str) -> object | None:
            if recipe_id != "recipe-1":
                return None
            return {
                "preprocess_artifact": {
                    "derivation_id": "d-1",
                    "profile": "office_to_pdf",
                    "converted": True,
                    "object_storage_path": "docs/doc-1/recipe-1/prepared.pdf",
                    "content_type": "application/pdf",
                    "file_name": "prepared.pdf",
                }
            }

    class FakeStorage:
        async def get(self, path: str) -> bytes:
            read.append(path)
            return b"pdf"

    monkeypatch.setattr(document_crop, "FileStorageClient", FakeStorage)
    oracle = cast(Any, FakeOracle())

    await document_crop.load_parsed_source(oracle, "doc-1", "recipe-1")
    # レシピが無ければ文書のファイル(ここでは原本)。
    await document_crop.load_parsed_source(oracle, "doc-1", "unknown")
    await document_crop.load_parsed_source(oracle, "doc-1")
    assert read == [
        "docs/doc-1/recipe-1/prepared.pdf",
        "docs/doc-1/source.docx",
        "docs/doc-1/source.docx",
    ]
    with pytest.raises(document_crop.DocumentSourceNotFoundError):
        await document_crop.load_parsed_source(oracle, "doc-2", "recipe-1")
