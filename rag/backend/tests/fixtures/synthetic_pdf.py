"""テストの合成の PDF を決定論で作る（reportlab。dev の依存だけ。#3343）。

座標は頁の左上が原点の pt（解析の bbox・``app.rag.pdf_backend`` と同じ向き）。文字は基線の左端を
指定し、返す箱は書体の ascent / descent と字幅から求める（PDF を読み直さない）。同じ入力からは
同じバイト列を作る（``invariant``）。
"""

from __future__ import annotations

import io
from collections.abc import Callable
from dataclasses import dataclass, field

from reportlab.lib.utils import ImageReader  # type: ignore[import-untyped]
from reportlab.pdfbase import pdfmetrics  # type: ignore[import-untyped]
from reportlab.pdfbase.cidfonts import UnicodeCIDFont  # type: ignore[import-untyped]
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

Rect = tuple[float, float, float, float]

# 埋め込まない日本語の CID 書体（Adobe-Japan1。閲覧側の代わりの書体で描く）。
JAPANESE_FONT = "HeiseiKakuGo-W5"
LATIN_FONT = "Helvetica"
A4 = (595.0, 842.0)
LETTER = (612.0, 792.0)

_registered: set[str] = set()


def _font(name: str) -> str:
    if name == "japan":
        name = JAPANESE_FONT
    if name == JAPANESE_FONT and name not in _registered:
        pdfmetrics.registerFont(UnicodeCIDFont(JAPANESE_FONT))
        _registered.add(name)
    return name


def text_box(
    x: float, baseline: float, text: str, *, font: str = LATIN_FONT, size: float = 11
) -> Rect:
    """``SyntheticPage.text`` が書く文字列の箱（左上が原点の pt）。"""
    name = _font(font)
    width = pdfmetrics.stringWidth(text, name, size)
    ascent, descent = pdfmetrics.getAscentDescent(name, size)
    return (x, baseline - ascent, x + width, baseline - descent)


@dataclass
class SyntheticPage:
    width: float
    height: float
    rotation: int = 0
    cropbox: Rect | None = None
    _ops: list[Callable[[canvas.Canvas], None]] = field(default_factory=list)

    @property
    def media_height(self) -> float:
        """内容の座標（回転の前）の高さ。90 / 270 度の頁は表示の幅。"""
        return self.width if self.rotation % 180 == 90 else self.height

    def text(
        self,
        x: float,
        baseline: float,
        text: str,
        *,
        font: str = LATIN_FONT,
        size: float = 11,
        char_space: float = 0.0,
    ) -> Rect:
        """基線の左端 (x, baseline) から文字列を書き、その箱を返す。"""
        name = _font(font)
        height = self.media_height

        def draw(pdf: canvas.Canvas) -> None:
            obj = pdf.beginText(x, height - baseline)
            obj.setFont(name, size)
            if char_space:
                obj.setCharSpace(char_space)
            obj.textOut(text)
            pdf.drawText(obj)

        self._ops.append(draw)
        return text_box(x, baseline, text, font=name, size=size)

    def vertical_text(
        self, x: float, top: float, text: str, *, font: str = "japan", size: float = 11
    ) -> None:
        """1 字ずつ上から下へ積んで書く（縦書きを横書きの書体で書いた PDF の形）。"""
        name = _font(font)
        ascent, _descent = pdfmetrics.getAscentDescent(name, size)
        for index, char in enumerate(text):
            self.text(x, top + ascent + index * size * 1.1, char, font=name, size=size)

    def rect(self, rect: Rect, fill: tuple[float, float, float] = (0, 0, 0)) -> None:
        x0, y0, x1, y1 = rect
        height = self.media_height

        def draw(pdf: canvas.Canvas) -> None:
            pdf.setFillColorRGB(*fill)
            pdf.setStrokeColorRGB(*fill)
            pdf.rect(x0, height - y1, x1 - x0, y1 - y0, stroke=1, fill=1)

        self._ops.append(draw)

    def image(self, rect: Rect, data: bytes) -> None:
        x0, y0, x1, y1 = rect
        height = self.media_height

        def draw(pdf: canvas.Canvas) -> None:
            pdf.drawImage(ImageReader(io.BytesIO(data)), x0, height - y1, x1 - x0, y1 - y0)

        self._ops.append(draw)


@dataclass
class SyntheticPdf:
    pages: list[SyntheticPage] = field(default_factory=list)

    def page(
        self,
        width: float = A4[0],
        height: float = A4[1],
        *,
        rotation: int = 0,
        cropbox: Rect | None = None,
    ) -> SyntheticPage:
        """頁を足す。``cropbox`` は PDF の座標（左下が原点）の (x0, y0, x1, y1)。"""
        page = SyntheticPage(width, height, rotation, cropbox)
        self.pages.append(page)
        return page

    def to_bytes(self) -> bytes:
        buffer = io.BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4, invariant=1, pageCompression=1)
        pages = self.pages or [SyntheticPage(*A4)]
        for page in pages:
            pdf.setPageSize((page.width, page.height))
            # 頁ごとに設定し直す（reportlab は前の頁の値を引き継ぐ）。
            # 90 / 270 度の頁は、reportlab が MediaBox の幅と高さを入れ替える（表示の寸法が
            # width × height になる）。
            pdf.setPageRotation(page.rotation)
            media = (
                (page.height, page.width)
                if page.rotation % 180 == 90
                else (page.width, page.height)
            )
            pdf.setCropBox(page.cropbox or (0, 0, *media))
            for op in page._ops:
                op(pdf)
            pdf.showPage()
        pdf.save()
        return buffer.getvalue()


def text_pdf(
    texts: list[str], *, size: tuple[float, float] = A4, at: tuple[float, float] = (72, 72)
) -> bytes:
    """1 頁に 1 つずつ文字列を書いた PDF。"""
    document = SyntheticPdf()
    for value in texts:
        document.page(*size).text(at[0], at[1], value)
    return document.to_bytes()


def blank_pdf(*sizes: tuple[float, float], rotations: tuple[int, ...] = ()) -> bytes:
    """白紙の頁の PDF（``rotations`` で頁ごとの /Rotate）。"""
    document = SyntheticPdf()
    for index, size in enumerate(sizes or (A4,)):
        rotation = rotations[index] if index < len(rotations) else 0
        document.page(*size, rotation=rotation)
    return document.to_bytes()


def encrypted_pdf(password: str = "synthetic") -> bytes:
    """利用者のパスワードで暗号化した 1 頁の PDF。"""
    from reportlab.lib import pdfencrypt  # type: ignore[import-untyped]

    buffer = io.BytesIO()
    pdf = canvas.Canvas(
        buffer,
        pagesize=A4,
        invariant=1,
        encrypt=pdfencrypt.StandardEncryption(password, canPrint=0),
    )
    pdf.drawString(72, 770, "Synthetic encrypted page")
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def form_pdf(value: str = "FILLEDVALUE") -> bytes:
    """値を入れた文字の欄（AcroForm）が 1 つの 300x200 pt の PDF（欄は左上から 20,70-220,100）。"""
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(300, 200), invariant=1)
    pdf.acroForm.textfield(
        name="field",
        value=value,
        x=20,
        y=100,
        width=200,
        height=30,
        fontSize=16,
        borderWidth=0,
        fillColor=None,
    )
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def png(size: tuple[int, int] = (200, 100), color: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


# 多段の表頭と結合セルを含む合成の表（#3429）。表頭は 2 段（「Group」「Item」は 2 段を結合、「H1」
# 「H2」は 2 列を結合）、本文の「Group」は 2 行ずつ結合する。値は合成の数。
MERGED_HEADER_TABLE_ROWS: tuple[tuple[str, ...], ...] = (
    ("Group", "Item", "H1", "", "H2", ""),
    ("", "", "Plan", "Actual", "Plan", "Actual"),
    ("G1", "Alpha", "110", "105", "120", "118"),
    ("", "Beta", "210", "198", "220", "231"),
    ("G2", "Gamma", "310", "322", "330", "301"),
    ("", "Delta", "410", "407", "420", "415"),
)
# reportlab の SPAN の (列, 行) の範囲。
MERGED_HEADER_TABLE_SPANS: tuple[tuple[tuple[int, int], tuple[int, int]], ...] = (
    ((0, 0), (0, 1)),
    ((1, 0), (1, 1)),
    ((2, 0), (3, 0)),
    ((4, 0), (5, 0)),
    ((0, 2), (0, 3)),
    ((0, 4), (0, 5)),
)


def merged_header_table_pdf() -> bytes:
    """``MERGED_HEADER_TABLE_ROWS`` の表を罫線つきで 1 頁に描いた PDF。

    同じ入力からは同じバイト列を作る（``invariant``）。
    """
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet  # type: ignore[import-untyped]
    from reportlab.platypus import (  # type: ignore[import-untyped]
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    buffer = io.BytesIO()
    document = SimpleDocTemplate(buffer, pagesize=A4, invariant=1, title="Synthetic table")
    style = TableStyle(
        [
            ("GRID", (0, 0), (-1, -1), 0.8, colors.black),
            ("FONTNAME", (0, 0), (-1, 1), "Helvetica-Bold"),
            ("FONTNAME", (0, 2), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 11),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            *(("SPAN", start, end) for start, end in MERGED_HEADER_TABLE_SPANS),
        ]
    )
    table = Table([list(row) for row in MERGED_HEADER_TABLE_ROWS], colWidths=72, rowHeights=24)
    table.setStyle(style)
    styles = getSampleStyleSheet()
    document.build(
        [
            Paragraph("Synthetic quarterly table", styles["Heading2"]),
            Spacer(1, 12),
            table,
            Spacer(1, 12),
            Paragraph("The values in this table are synthetic.", styles["BodyText"]),
        ]
    )
    return buffer.getvalue()
