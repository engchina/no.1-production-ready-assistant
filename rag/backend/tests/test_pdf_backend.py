"""PDF と画像の頁を開く・描く・文字の行を読む共通の土台（PDFium と Pillow。#3343）。"""

from __future__ import annotations

import io
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from PIL import Image

from app.rag import pdf_backend
from app.rag.pdf_backend import (
    ImageSource,
    PdfSource,
    SourceOpenError,
    assemble_lines,
    open_source,
    pixel_rect,
)
from tests.fixtures.synthetic_pdf import (
    SyntheticPdf,
    blank_pdf,
    encrypted_pdf,
    form_pdf,
    png,
    text_box,
)


def _sample_pdf() -> bytes:
    document = SyntheticPdf()
    page = document.page(595, 842)
    page.text(72, 100, "Synthetic first line")
    page.text(72, 116, "Synthetic second line")
    page.text(72, 200, "Left column")
    page.text(330, 200, "Right column")
    page.text(72, 260, "09:00～17:00", font="japan")
    page.rect((100, 400, 300, 500), fill=(1, 0, 0))
    document.page(600, 800, rotation=90)
    document.page(600, 800, cropbox=(50, 100, 550, 700)).text(100, 150, "Cropped text")
    return document.to_bytes()


def test_pdf_page_sizes_follow_rotation_and_cropbox() -> None:
    with open_source(_sample_pdf()) as source:
        assert isinstance(source, PdfSource)
        assert source.page_count == 3
        assert source.page_size(0) == (595.0, 842.0)
        # 90 度の頁は表示の向き（600x800）、CropBox の頁はその寸法。
        assert source.page_size(1) == (600.0, 800.0)
        assert source.rotation(1) == 90
        assert source.page_size(2) == (500.0, 600.0)
        with pytest.raises(IndexError):
            source.page_size(3)


@pytest.mark.parametrize(
    ("clip", "zoom", "expected"),
    [
        ((0, 0, 595, 842), 200 / 72, (0, 0, 1653, 2339)),
        ((0, 0, 612, 792), 200 / 72, (0, 0, 1700, 2200)),
        ((60.3, 90.7, 300.2, 210.9), 150 / 72, (125, 188, 626, 440)),
        # 端がちょうど画素の境目に来るときは広げない（MuPDF の丸めと同じ）。
        ((36, 36, 72, 72), 2.0, (72, 72, 144, 144)),
    ],
    ids=["a4-200dpi", "letter-200dpi", "clip-150dpi", "exact"],
)
def test_pixel_rect_uses_mupdf_rounding(
    clip: tuple[float, float, float, float], zoom: float, expected: tuple[int, int, int, int]
) -> None:
    assert pixel_rect(clip, zoom) == expected


def test_render_size_matches_zoom_rotation_and_clip() -> None:
    with open_source(_sample_pdf()) as source:
        assert source.render(0, 200 / 72).size == (1653, 2339)
        assert source.render(1, 2.0).size == (1200, 1600)
        clipped = source.render(0, 1.0, (100, 400, 300, 500))
        assert clipped.size == (200, 100)
        assert clipped.mode == "RGB"
        # 赤い矩形を切り出した画像は赤（白い地に描いた内容が出る）。
        assert clipped.getpixel((100, 50)) == (255, 0, 0)
        assert source.render(0, 1.0).getpixel((10, 10)) == (255, 255, 255)


def test_text_lines_are_split_by_line_and_column_with_top_left_boxes() -> None:
    with open_source(_sample_pdf()) as source:
        lines = source.text_lines(0)
        cropped = source.text_lines(2)
    assert [line.text for line in lines] == [
        "Synthetic first line",
        "Synthetic second line",
        "Left column",
        "Right column",
        "09:00～17:00",
    ]
    assert all(line.dir == (1.0, 0.0) for line in lines)
    expected = text_box(72, 100, "Synthetic first line")
    box = lines[0].bbox
    assert box[0] == pytest.approx(expected[0], abs=0.5)
    assert box[2] == pytest.approx(expected[2], abs=0.5)
    # 行の高さは書体の ascent / descent から PDFium が決める（基線を含み、字の大きさ程度）。
    assert box[1] < 100 < box[3]
    assert 8 <= box[3] - box[1] <= 16
    # 文字の箱も同じ座標（左上が原点の pt）。
    assert lines[0].chars[0].c == "S"
    assert lines[0].chars[0].bbox[0] == pytest.approx(72, abs=0.5)
    # CropBox の頁は CropBox の左上が原点。
    assert cropped[0].text == "Cropped text"
    assert cropped[0].bbox[0] == pytest.approx(50, abs=0.5)
    assert 30 < cropped[0].bbox[1] < 60


def test_stacked_glyphs_are_not_read_as_a_horizontal_line() -> None:
    document = SyntheticPdf()
    # 書体を埋め込まない CID の字は OS の書体の有無で読めるかが変わるため、標準の書体の字で積む。
    document.page().vertical_text(500, 300, "ABC", font="Helvetica")
    with open_source(document.to_bytes()) as source:
        lines = source.text_lines(0)
    # 1 字ずつ積んだ字は、横に並んだ 1 行にはならない（縦の行か 1 字ずつの行）。
    assert "".join(line.text for line in lines) == "ABC"
    assert all(len(line.chars) == 1 or line.dir == (0.0, 1.0) for line in lines)


def test_assemble_lines_splits_gaps_baseline_shifts_and_detects_vertical_lines() -> None:
    def glyph(char: str, x: float, y: float, size: float = 10) -> tuple[str, Any]:
        return char, (x, y, x + size * 0.6, y + size)

    lines = assemble_lines(
        [
            glyph("a", 0, 0),
            glyph("b", 6, 0),
            (" ", None),
            glyph("c", 14, 0),
            # 字の大きさの 0.8 倍を超えて離れた字は別の行（表の列・段組み）。
            glyph("d", 40, 0),
            # 基線が大きくずれた字は別の行。
            glyph("e", 46, 20),
            ("\n", None),
            # 上から下へ積んだ字は縦の行。
            glyph("縦", 100, 0),
            glyph("書", 100, 11),
            glyph("き", 100, 22),
            ("\n", None),
            ("\x02", (0, 40, 6, 50)),
        ]
    )
    assert [(line.text, line.dir) for line in lines] == [
        ("abc", (1.0, 0.0)),
        ("d", (1.0, 0.0)),
        ("e", (1.0, 0.0)),
        ("縦書き", (0.0, 1.0)),
        ("\x02", (1.0, 0.0)),
    ]
    assert lines[3].bbox == (100, 0, 106, 32)


def test_images_open_as_one_page_with_resolution_and_orientation() -> None:
    def encoded(fmt: str, **kwargs: Any) -> bytes:
        buffer = io.BytesIO()
        Image.new("RGB", (400, 300), (200, 30, 30)).save(buffer, format=fmt, **kwargs)
        return buffer.getvalue()

    with open_source(encoded("PNG")) as source:
        assert isinstance(source, ImageSource)
        assert source.page_count == 1
        # 解像度の情報が無ければ 96 dpi（400 px = 300 pt）。
        assert source.page_size(0) == (300.0, 225.0)
        assert source.render(0, 2.0).size == (600, 450)
        assert source.text_lines(0) == []
    with open_source(encoded("JPEG", dpi=(200, 200))) as source:
        assert source.page_size(0) == pytest.approx((144.0, 108.0))
        assert source.render(0, 2.0).size == (288, 216)
        assert source.render(0, 1.0, (0, 0, 72, 54)).size == (72, 54)
    with open_source(encoded("WEBP")) as source:
        assert source.page_size(0) == (300.0, 225.0)

    # EXIF の向き（90 度）を反映した向きの寸法。
    exif = Image.Exif()
    exif[0x0112] = 6
    with open_source(encoded("JPEG", dpi=(96, 96), exif=exif.tobytes())) as source:
        assert source.page_size(0) == (225.0, 300.0)

    # 透過は白の地に重ねる。
    buffer = io.BytesIO()
    Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(buffer, format="PNG")
    with open_source(buffer.getvalue()) as source:
        assert source.render(0, 0.75).getpixel((0, 0)) == (255, 255, 255)


@pytest.mark.parametrize(
    "data",
    [encrypted_pdf(), b"%PDF-1.7\nbroken", b"\x00\x01not-a-document", b""],
    ids=["encrypted", "broken-pdf", "unknown", "empty"],
)
def test_unreadable_sources_raise_source_open_error(data: bytes) -> None:
    with pytest.raises(SourceOpenError):
        open_source(data)
    assert issubclass(SourceOpenError, ValueError)


def test_pdf_only_rejects_images() -> None:
    with pytest.raises(SourceOpenError):
        open_source(png(), pdf_only=True)


def test_pdf_backend_shares_the_process_pdfium_lock() -> None:
    from rag_engine.parsing import rendering

    from app.rag import page_labels

    assert pdf_backend.PDFIUM_LOCK is rendering.PDFIUM_LOCK
    assert vars(page_labels)["PDFIUM_LOCK"] is rendering.PDFIUM_LOCK


class _TrackingLock:
    """PDFium の lock の代わり。中に入っているスレッドの数の最大と、持ち主を記録する。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._meta = threading.Lock()
        self._owner: int | None = None
        self._depth = 0
        self.active = 0
        self.max_active = 0

    def __enter__(self) -> _TrackingLock:
        self._lock.acquire()
        self._depth += 1
        if self._depth == 1:
            self._owner = threading.get_ident()
            with self._meta:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
        return self

    def __exit__(self, *_exc: object) -> None:
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            with self._meta:
                self.active -= 1
        self._lock.release()

    def held_by_current_thread(self) -> bool:
        return self._owner == threading.get_ident()


def test_concurrent_rendering_and_text_reading_stay_inside_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """8 スレッドで描画と文字の読み取りを並行させても、PDFium の呼び出しは lock の中だけ。"""
    import pypdfium2  # type: ignore[import-untyped]

    lock = _TrackingLock()
    monkeypatch.setattr(pdf_backend, "PDFIUM_LOCK", lock)
    outside: list[str] = []

    def guarded(name: str, function: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            if not lock.held_by_current_thread():
                outside.append(name)
            return function(*args, **kwargs)

        return call

    document_class = pypdfium2.PdfDocument
    for owner, names in (
        (document_class, ("__getitem__", "__len__", "close")),
        (pypdfium2.PdfPage, ("get_size", "get_rotation", "get_bbox", "get_textpage", "close")),
        (pypdfium2.PdfTextPage, ("count_chars", "get_charbox", "close")),
        (pypdfium2.PdfBitmap, ("fill_rect", "to_pil", "close")),
    ):
        for name in names:
            monkeypatch.setattr(owner, name, guarded(name, getattr(owner, name)))
    monkeypatch.setattr(pypdfium2, "PdfDocument", guarded("open", document_class))
    for name in ("FPDF_RenderPageBitmap", "FPDFText_GetUnicode", "FPDFText_IsGenerated"):
        monkeypatch.setattr(pypdfium2.raw, name, guarded(name, getattr(pypdfium2.raw, name)))

    data = _sample_pdf()

    def work(_index: int) -> tuple[tuple[int, int], list[str], bytes]:
        with open_source(data) as source:
            image = source.render(0, 0.5)
            lines = [line.text for line in source.text_lines(0)]
        return image.size, lines, image.tobytes()

    expected = work(0)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(work, range(8 * 6)))

    assert results == [expected] * (8 * 6)
    assert outside == []
    assert lock.max_active == 1


def test_blank_pdf_has_no_text_lines() -> None:
    with open_source(blank_pdf((200, 100))) as source:
        assert source.text_lines(0) == []
        assert source.render(0, 1.0).size == (200, 100)


def test_render_draws_form_field_values() -> None:
    """フォームの欄の値（widget の外観）も頁の画像に描く（プレビュー・外部の parser の画像）。"""
    with open_source(form_pdf()) as source:
        image = source.render(0, 1.0, (20, 70, 220, 100))
    dark = sum(image.convert("L").histogram()[:128])
    assert dark > 200
