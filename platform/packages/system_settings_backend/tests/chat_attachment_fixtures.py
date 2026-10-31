"""会話の添付のテストの合成のファイル（#2517）。業務の内容を含まない。"""

from __future__ import annotations

import io
import zipfile

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _zip(parts: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def _w_paragraph(text: str, style: str | None = None) -> str:
    style_xml = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{style_xml}<w:r><w:t>{text}</w:t></w:r></w:p>"


def docx_bytes(*, macro: bool = False) -> bytes:
    body = "".join(
        [
            _w_paragraph("はじめの段落"),
            _w_paragraph("第一の見出し", "Heading1"),
            _w_paragraph("見出しの下の本文 A"),
            "<w:tbl>"
            "<w:tr><w:tc><w:p><w:r><w:t>項目</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p><w:r><w:t>値</w:t></w:r></w:p></w:tc></w:tr>"
            "<w:tr><w:tc><w:p><w:r><w:t>甲</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p><w:r><w:t>10</w:t></w:r></w:p></w:tc></w:tr>"
            "</w:tbl>",
            _w_paragraph("第二の見出し", "Custom2"),
            _w_paragraph("見出しの下の本文 B"),
        ]
    )
    styles = (
        f'<w:styles xmlns:w="{_W_NS}">'
        '<w:style w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>'
        '<w:style w:styleId="Custom2"><w:name w:val="見出し 2"/></w:style>'
        "</w:styles>"
    )
    parts: dict[str, str | bytes] = {
        "[Content_Types].xml": "<Types/>",
        "word/document.xml": f'<w:document xmlns:w="{_W_NS}"><w:body>{body}</w:body></w:document>',
        "word/styles.xml": styles,
    }
    if macro:
        parts["word/vbaProject.bin"] = b"\x00macro"
    return _zip(parts)


def _slide(title: str, body: str) -> str:
    return (
        f'<p:sld xmlns:p="{_P_NS}" xmlns:a="{_A_NS}"><p:cSld><p:spTree>'
        '<p:sp><p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>'
        f"<p:txBody><a:p><a:r><a:t>{title}</a:t></a:r></a:p></p:txBody></p:sp>"
        "<p:sp><p:nvSpPr><p:nvPr/></p:nvSpPr>"
        f"<p:txBody><a:p><a:r><a:t>{body}</a:t></a:r></a:p></p:txBody></p:sp>"
        '<p:sp><p:nvSpPr><p:nvPr><p:ph type="sldNum"/></p:nvPr></p:nvSpPr>'
        "<p:txBody><a:p><a:r><a:t>99</a:t></a:r></a:p></p:txBody></p:sp>"
        "</p:spTree></p:cSld></p:sld>"
    )


def pptx_bytes() -> bytes:
    presentation = (
        f'<p:presentation xmlns:p="{_P_NS}" xmlns:r="{_R_NS}"><p:sldIdLst>'
        '<p:sldId id="256" r:id="rId2"/><p:sldId id="257" r:id="rId1"/>'
        "</p:sldIdLst></p:presentation>"
    )
    rels = (
        f'<Relationships xmlns="{_PKG_REL_NS}">'
        '<Relationship Id="rId1" Target="slides/slide2.xml"/>'
        '<Relationship Id="rId2" Target="slides/slide1.xml"/>'
        "</Relationships>"
    )
    slide1_rels = (
        f'<Relationships xmlns="{_PKG_REL_NS}">'
        '<Relationship Id="rId1" Target="../notesSlides/notesSlide1.xml"/>'
        "</Relationships>"
    )
    notes = (
        f'<p:notes xmlns:p="{_P_NS}" xmlns:a="{_A_NS}"><p:cSld><p:spTree>'
        "<p:sp><p:nvSpPr><p:nvPr/></p:nvSpPr>"
        "<p:txBody><a:p><a:r><a:t>話す内容のメモ</a:t></a:r></a:p></p:txBody></p:sp>"
        "</p:spTree></p:cSld></p:notes>"
    )
    return _zip(
        {
            "[Content_Types].xml": "<Types/>",
            "ppt/presentation.xml": presentation,
            "ppt/_rels/presentation.xml.rels": rels,
            "ppt/slides/slide1.xml": _slide("最初のスライド", "一枚目の本文"),
            "ppt/slides/_rels/slide1.xml.rels": slide1_rels,
            "ppt/notesSlides/notesSlide1.xml": notes,
            "ppt/slides/slide2.xml": _slide("次のスライド", "二枚目の本文"),
        }
    )


def xlsx_bytes(*, macro: bool = False) -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "一覧"
    sheet.append([None, None, None])
    sheet.append(["コード", "名前", "金額", None])
    sheet.append(["A-1", "品目甲", 1200, None])
    sheet.append([None, None, None, None])
    sheet.append(["A-2", "品目乙", 3400.5, None])
    other = workbook.create_sheet("空")
    other.append([None])
    buffer = io.BytesIO()
    workbook.save(buffer)
    data = buffer.getvalue()
    if not macro:
        return data
    with zipfile.ZipFile(io.BytesIO(data)) as source:
        parts: dict[str, str | bytes] = {name: source.read(name) for name in source.namelist()}
    parts["xl/vbaProject.bin"] = b"\x00macro"
    return _zip(parts)


def pdf_bytes(pages: list[str]) -> bytes:
    """文字の入った最小の PDF（Helvetica。ASCII の文字だけ）。空の文字の頁は文字を持たない。"""
    objects: list[bytes] = []
    kids = " ".join(f"{3 + index * 2} 0 R" for index in range(len(pages)))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    font_id = 3 + len(pages) * 2
    for index, text in enumerate(pages):
        content_id = 4 + index * 2
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>"
            ).encode()
        )
        stream = f"BT /F1 12 Tf 72 712 Td ({text}) Tj ET".encode() if text else b""
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    output = io.BytesIO()
    output.write(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(output.tell())
        output.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = output.tell()
    output.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        output.write(f"{offset:010d} 00000 n \n".encode())
    output.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return output.getvalue()


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
