"""PDF と画像の頁を開く・描く・文字の行を読む共通の土台（PDFium = pypdfium2 と Pillow。#3343）。

RAG backend の process の中で PDF の頁を扱う処理（プレビューの頁の画像・bbox の切り出し・外部の
parser へ渡す頁の画像・頁の境目の判定の行の位置・原文の文字の照合）は、すべてこの module を通す。

- PDFium はスレッド安全ではない（pypdfium2 の Known limitations「Incompatibility with
  Threading」）。PDFium の呼び出し（開く・描く・読む・閉じる）は、rag_engine と共有する process
  全体の ``PDFIUM_LOCK`` の中で行う。lock の中では PDFium の処理だけを行い、PNG の符号化などは
  lock の外で行う。
- 座標は頁の表示の向き（/Rotate を反映）の左上を原点にした pt。頁の寸法は CropBox（MediaBox との
  交わり）。
- 画像（PNG・JPEG・TIFF・BMP・GIF・WebP など）は Pillow で 1 頁として開く。頁の寸法は画素数 ×
  72 / 解像度（解像度の情報が無ければ 96 dpi）で、EXIF の向きを反映する。
- 描いた画像の画素数は、clip と倍率から MuPDF と同じ丸め（端を 0.001 だけ内側にして floor / ceil）
  で決める。
- 開けないファイル（暗号化・破損・対応しない形式）は ``SourceOpenError``（``ValueError``）にする。
"""

from __future__ import annotations

import io
import math
import zlib
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rag_engine.parsing.rendering import PDFIUM_LOCK

if TYPE_CHECKING:
    from PIL.Image import Image

Rect = tuple[float, float, float, float]

# 画像の解像度の情報が無いときの値（MuPDF と同じ）。
DEFAULT_IMAGE_DPI = 96.0
# MuPDF の画素の丸め（fz_round_rect）の内側への幅。
_ROUND_EPSILON = 0.001
# 文字の行を分ける閾値（MuPDF の stext と同じく、字の大きさに対する比）。
_LINE_GAP_RATIO = 0.8
_BASELINE_SHIFT_RATIO = 0.8
# PNG の zlib の水準と strategy。Pillow の既定（Z_FILTERED）は、文字の多い頁の画像で
# Z_DEFAULT_STRATEGY より大きくなるため、既定の strategy にする（合成の PDF で測った値。#3343）。
PNG_COMPRESS_LEVEL = 6
PNG_COMPRESS_TYPE = zlib.Z_DEFAULT_STRATEGY
# PDFium が行の終わりの「-」（語の途中の改行）に返す符号。
_PDFIUM_HYPHEN = "\x02"


class SourceOpenError(ValueError):
    """PDF / 画像として開けない（暗号化・破損・対応しない形式）。"""


@dataclass(frozen=True, slots=True)
class TextChar:
    """1 文字（頁の左上が原点の pt の箱）。"""

    c: str
    bbox: Rect


@dataclass(frozen=True, slots=True)
class TextLine:
    """文字の層の 1 行。``dir`` は行の進む向き（横書きは (1, 0)、縦書きは (0, 1)）。"""

    bbox: Rect
    dir: tuple[float, float]
    chars: tuple[TextChar, ...]

    @property
    def text(self) -> str:
        return "".join(char.c for char in self.chars)


def is_pdf(data: bytes) -> bool:
    """PDF の header（先頭の 1024 バイトの中の ``%PDF``）があるか。"""
    return b"%PDF" in data[:1024]


def pixel_rect(clip: Rect, zoom: float) -> tuple[int, int, int, int]:
    """clip（pt）を倍率で画素にした整数の矩形（MuPDF の fz_round_rect と同じ丸め）。"""
    x0, y0, x1, y1 = (value * zoom for value in clip)
    return (
        math.floor(x0 + _ROUND_EPSILON),
        math.floor(y0 + _ROUND_EPSILON),
        math.ceil(x1 - _ROUND_EPSILON),
        math.ceil(y1 - _ROUND_EPSILON),
    )


def png_bytes(image: Image) -> bytes:
    buffer = io.BytesIO()
    image.save(
        buffer, format="PNG", compress_level=PNG_COMPRESS_LEVEL, compress_type=PNG_COMPRESS_TYPE
    )
    return buffer.getvalue()


def intersect(a: Rect, b: Rect) -> Rect | None:
    """2 つの矩形の交わり（空なら None）。"""
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


class PageSource:
    """PDF / 画像の頁の集まり。``open_source`` で開き、``with`` で閉じる。"""

    page_count: int

    def page_size(self, index: int) -> tuple[float, float]:
        """頁（0 始まり）の表示の寸法（pt。/Rotate を反映した向き）。"""
        raise NotImplementedError

    def rotation(self, index: int) -> int:
        """頁の /Rotate（0 / 90 / 180 / 270）。画像は 0。"""
        return 0

    def render(self, index: int, zoom: float, clip: Rect | None = None) -> Image:
        """頁（clip を渡せばその範囲）を倍率 ``zoom``（1 = 72 dpi）で描いた RGB の画像。"""
        raise NotImplementedError

    def text_lines(self, index: int) -> list[TextLine]:
        """頁の文字の層の行（PDF の内容の順）。文字の層が無ければ空。"""
        return []

    def close(self) -> None:
        return None

    def __enter__(self) -> PageSource:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def open_source(data: bytes, *, pdf_only: bool = False) -> PageSource:
    """PDF なら PDFium、それ以外は Pillow の画像として開く。開けなければ ``SourceOpenError``。"""
    if is_pdf(data):
        return PdfSource(data)
    if pdf_only:
        raise SourceOpenError("PDF ではありません。")
    return ImageSource(data)


class PdfSource(PageSource):
    """PDFium の PDF。PDFium の呼び出しは毎回 ``PDFIUM_LOCK`` の中で行う。"""

    def __init__(self, data: bytes) -> None:
        import pypdfium2 as pdfium  # type: ignore[import-untyped]

        self._pdfium = pdfium
        with PDFIUM_LOCK:
            try:
                self._document: Any = pdfium.PdfDocument(data)
            except Exception as exc:  # noqa: BLE001 - 暗号化・破損は開けないファイルとして扱う
                raise SourceOpenError("PDF を開けません。") from exc
            # フォームの欄の値（widget の外観）も描くよう、頁を開く前に form の環境を作る
            # （フォームの無い PDF では何もしない。作れなければフォームを描かずに続ける）。
            with suppress(Exception):
                self._document.init_forms()
            try:
                self.page_count = len(self._document)
            except Exception as exc:  # noqa: BLE001
                self._document.close()
                raise SourceOpenError("PDF を開けません。") from exc

    def close(self) -> None:
        document = getattr(self, "_document", None)
        if document is not None:
            with PDFIUM_LOCK:
                document.close()
            self._document = None

    @contextmanager
    def _page(self, index: int) -> Iterator[Any]:
        """lock を持ったまま頁を開いて閉じる（呼ぶ側が lock の中で使う）。"""
        if not 0 <= index < self.page_count or self._document is None:
            raise IndexError(index)
        page = self._document[index]
        try:
            yield page
        finally:
            page.close()

    def page_size(self, index: int) -> tuple[float, float]:
        with PDFIUM_LOCK, self._page(index) as page:
            width, height = page.get_size()
            return float(width), float(height)

    def rotation(self, index: int) -> int:
        with PDFIUM_LOCK, self._page(index) as page:
            return int(page.get_rotation()) % 360

    def render(self, index: int, zoom: float, clip: Rect | None = None) -> Image:
        pdfium = self._pdfium
        raw = pdfium.raw
        with PDFIUM_LOCK, self._page(index) as page:
            width, height = (float(value) for value in page.get_size())
            area = (0.0, 0.0, width, height) if clip is None else clip
            x0, y0, x1, y1 = pixel_rect(area, zoom)
            bitmap_width, bitmap_height = max(1, x1 - x0), max(1, y1 - y0)
            bitmap = pdfium.PdfBitmap.new_native(bitmap_width, bitmap_height, raw.FPDFBitmap_BGR)
            try:
                bitmap.fill_rect((255, 255, 255, 255), 0, 0, bitmap_width, bitmap_height)
                # 頁の全体を倍率どおりの大きさで描き、clip の左上を bitmap の原点に合わせる。
                position = (-x0, -y0, max(1, round(width * zoom)), max(1, round(height * zoom)), 0)
                raw.FPDF_RenderPageBitmap(bitmap, page, *position, raw.FPDF_ANNOT)
                if page.formenv:
                    raw.FPDF_FFLDraw(page.formenv, bitmap, page, *position, raw.FPDF_ANNOT)
                # to_pil は BGR を RGB に写した別の画像を返す（bitmap の buffer を共有しない）。
                return bitmap.to_pil()  # type: ignore[no-any-return]
            finally:
                bitmap.close()

    def text_lines(self, index: int) -> list[TextLine]:
        raw = self._pdfium.raw
        chars: list[tuple[str, Rect | None]] = []
        with PDFIUM_LOCK, self._page(index) as page:
            left, bottom, right, top = (float(value) for value in page.get_bbox())
            textpage = page.get_textpage()
            try:
                for char_index in range(textpage.count_chars()):
                    code = raw.FPDFText_GetUnicode(textpage, char_index)
                    char = chr(code) if code else ""
                    if char == _PDFIUM_HYPHEN:
                        char = "-"
                    if char in {"\r", "\n"}:
                        chars.append(("\n", None))
                        continue
                    if raw.FPDFText_IsGenerated(textpage, char_index) == 1:
                        # PDFium が足した空白は、文字の間の区切りとしてだけ使う。
                        chars.append((" ", None))
                        continue
                    if not char:
                        continue
                    box_left, box_bottom, box_right, box_top = textpage.get_charbox(
                        char_index, loose=True
                    )
                    chars.append(
                        (
                            char,
                            (
                                float(box_left) - left,
                                top - float(box_top),
                                float(box_right) - left,
                                top - float(box_bottom),
                            ),
                        )
                    )
            finally:
                textpage.close()
        return assemble_lines(chars)


def assemble_lines(chars: list[tuple[str, Rect | None]]) -> list[TextLine]:
    """PDFium の文字の並び（改行は "\\n"、足した空白は箱が None）を行にまとめる。

    PDFium の改行で分け、さらに同じ行の中で、字の大きさの 0.8 倍を超えて離れた文字（段組み・表の
    列）・戻った文字・基線が字の大きさの 0.8 倍を超えてずれた文字で分ける（MuPDF の stext と同じ
    閾値）。上から下へ並んだ文字は縦書きの行（dir = (0, 1)）にする。
    """
    lines: list[TextLine] = []
    current: list[TextChar] = []
    direction: str | None = None

    def flush() -> None:
        nonlocal current, direction
        if any(char.c.strip() for char in current):
            lines.append(_line(current, vertical=direction == "vertical"))
        current = []
        direction = None

    for char, box in chars:
        if char == "\n":
            flush()
            continue
        if box is None:
            continue
        if current and char.strip():
            step = _progression(current[-1].bbox, box, direction)
            if step is None:
                flush()
            else:
                direction = step
        current.append(TextChar(char, box))
    flush()
    return lines


def _progression(previous: Rect, box: Rect, direction: str | None) -> str | None:
    """前の字から次の字へ同じ行で進むなら "horizontal" / "vertical"、行が変わるなら None。"""
    size = max(previous[3] - previous[1], box[3] - box[1], 1e-6)
    width = max(previous[2] - previous[0], box[2] - box[0], 1e-6)
    shift_x = (box[0] + box[2]) / 2 - (previous[0] + previous[2]) / 2
    shift_y = (box[1] + box[3]) / 2 - (previous[1] + previous[3]) / 2
    stacked = abs(shift_x) < width * 0.5 and shift_y > size * 0.5
    if direction == "vertical" or (direction is None and stacked):
        # 縦書き: 左右がそろい、上から下へ進む。
        if abs(shift_x) > width * _BASELINE_SHIFT_RATIO or shift_y <= 0:
            return None
        if box[1] - previous[3] > width * _LINE_GAP_RATIO:
            return None
        return "vertical"
    if abs(shift_y) > size * _BASELINE_SHIFT_RATIO:
        return None
    if box[0] - previous[2] > size * _LINE_GAP_RATIO or box[2] < previous[0] - size:
        return None
    return "horizontal"


def _line(chars: list[TextChar], vertical: bool) -> TextLine:
    while chars and not chars[-1].c.strip():
        chars = chars[:-1]
    while chars and not chars[0].c.strip():
        chars = chars[1:]
    boxes = [char.bbox for char in chars]
    bbox = (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )
    direction = (0.0, 1.0) if vertical else (1.0, 0.0)
    return TextLine(bbox=bbox, dir=direction, chars=tuple(chars))


class ImageSource(PageSource):
    """Pillow の画像を 1 頁として扱う（先頭のフレーム）。"""

    def __init__(self, data: bytes) -> None:
        from PIL import Image, ImageOps

        try:
            with Image.open(io.BytesIO(data)) as opened:
                resolution = opened.info.get("dpi")
                image = ImageOps.exif_transpose(opened)
                image.load()
                if image.mode in {"RGBA", "LA"} or (
                    image.mode == "P" and "transparency" in image.info
                ):
                    rgba = image.convert("RGBA")
                    flattened = Image.new("RGB", rgba.size, (255, 255, 255))
                    flattened.paste(rgba, mask=rgba.getchannel("A"))
                    image = flattened
                else:
                    image = image.convert("RGB")
        except Exception as exc:  # noqa: BLE001 - 画像として読めないファイル
            raise SourceOpenError("画像を開けません。") from exc
        if image.width < 1 or image.height < 1:
            raise SourceOpenError("画像を開けません。")
        self._image = image
        self._dpi = _image_dpi(resolution)
        self.page_count = 1

    def page_size(self, index: int) -> tuple[float, float]:
        if index != 0:
            raise IndexError(index)
        return (
            self._image.width * 72.0 / self._dpi[0],
            self._image.height * 72.0 / self._dpi[1],
        )

    def render(self, index: int, zoom: float, clip: Rect | None = None) -> Image:
        from PIL import Image as PILImage

        width, height = self.page_size(index)
        area = (0.0, 0.0, width, height) if clip is None else clip
        x0, y0, x1, y1 = pixel_rect(area, zoom)
        target = (max(1, x1 - x0), max(1, y1 - y0))
        # 出力の画素の範囲を、元の画像の画素の範囲に戻す。
        scale_x = zoom * 72.0 / self._dpi[0]
        scale_y = zoom * 72.0 / self._dpi[1]
        # 画素の丸めで画像の端を少し越えた分は、画像の中に収める。
        box = (
            max(0.0, x0 / scale_x),
            max(0.0, y0 / scale_y),
            min(float(self._image.width), x1 / scale_x),
            min(float(self._image.height), y1 / scale_y),
        )
        if target == self._image.size and box == (0.0, 0.0, *self._image.size):
            return self._image.copy()
        shrinking = scale_x < 1 or scale_y < 1
        resample = PILImage.Resampling.LANCZOS if shrinking else PILImage.Resampling.BILINEAR
        return self._image.resize(target, resample=resample, box=box)


def _image_dpi(resolution: object) -> tuple[float, float]:
    values: list[float] = []
    if isinstance(resolution, tuple | list) and len(resolution) >= 2:
        for value in resolution[:2]:
            try:
                number = float(value)
            except (TypeError, ValueError):
                number = 0.0
            values.append(number if math.isfinite(number) and number >= 1 else DEFAULT_IMAGE_DPI)
    if len(values) != 2:
        return (DEFAULT_IMAGE_DPI, DEFAULT_IMAGE_DPI)
    return (values[0], values[1])


__all__ = [
    "PDFIUM_LOCK",
    "ImageSource",
    "PageSource",
    "PdfSource",
    "SourceOpenError",
    "TextChar",
    "TextLine",
    "assemble_lines",
    "intersect",
    "is_pdf",
    "open_source",
    "pixel_rect",
    "png_bytes",
]
