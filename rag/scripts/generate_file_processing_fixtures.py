"""Generate synthetic file-processing golden fixtures.

The fixtures are intentionally small and dependency-free. They are designed to
exercise parser routing, structure extraction, chunk lineage, and checkpoint
contracts in local CI. Staging should replace or supplement them with real
customer-like scans before using quality thresholds as a production gate.
"""

from __future__ import annotations

import zlib
from html import escape
from io import BytesIO
from pathlib import Path
from struct import pack
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "evaluation" / "file-processing-fixtures"
PACKAGE_RELATIONSHIPS_CONTENT_TYPE = (
    "application/vnd.openxmlformats-package.relationships+xml"
)
OFFICE_DOCUMENT_RELATIONSHIP = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
)

_FONT_5X7 = {
    " ": ["00000", "00000", "00000", "00000", "00000", "00000", "00000"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
    ":": ["00000", "01100", "01100", "00000", "01100", "01100", "00000"],
    "/": ["00001", "00010", "00100", "01000", "10000", "00000", "00000"],
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "10000", "11110", "00001", "00001", "11110"],
    "6": ["01110", "10000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00001", "01110"],
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01110", "10001", "10000", "10000", "10000", "10001", "01110"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01110", "10001", "10000", "10111", "10001", "10001", "01110"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["01110", "00100", "00100", "00100", "00100", "00100", "01110"],
    "J": ["00111", "00010", "00010", "00010", "10010", "10010", "01100"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "Q": ["01110", "10001", "10001", "10001", "10101", "10010", "01101"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "10101", "10101", "01010"],
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "Y": ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
    "Z": ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
}


def main() -> int:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    policy_pdf = _text_pdf(
        [
            "POLICY APPROVAL FLOW",
            "DEPARTMENT MANAGER APPROVES EXPENSES",
            "TOTAL LIMIT 1000 JPY",
        ]
    )
    _write("policy-ja.pdf", policy_pdf)
    _write("policy-ja-duplicate.pdf", policy_pdf)
    _write(
        "scanned-contract-ja.pdf",
        _image_pdf(
            [
                "SCANNED CONTRACT",
                "APPROVAL FLOW",
                "TOTAL 1000 JPY",
                "PAGE 1",
            ]
        ),
    )
    _write(
        "two-column-report-ja.pdf",
        _text_pdf(
            [
                "LEFT COLUMN READING ORDER",
                "STEP 1 INGESTION",
                "STEP 2 CHUNKING",
                "RIGHT COLUMN CITATION",
                "PAGE 2 HAS ANSWER",
            ],
            two_column=True,
        ),
    )
    _write_docx(
        "policy-ja.docx",
        [
            "1. 経費ポリシー",
            "部門長は1000円以上の申請を確認します。",
            "承認後に原本をObject Storageへ保管します。",
        ],
    )
    _write_pptx(
        "product-brief-ja.pptx",
        [
            "製品概要: RAG取込",
            "検索品質: 引用とページ番号を保持",
        ],
    )
    _write_xlsx("long-table-expenses.xlsx", long_table=True)
    _write_tsv("long-table-expenses.tsv", long_table=True)
    _write_xlsx("budget-ja.xlsx", long_table=False)
    _write_broken_xlsx("broken.xlsx")
    _write_text(
        "manual.html",
        """<!doctype html>
<html lang="ja">
<head><meta charset="utf-8"><title>運用マニュアル</title></head>
<body>
  <main>
    <h1>検索運用マニュアル</h1>
    <section>
      <h2>インデックス確認</h2>
      <p>検索結果が出ない場合はINDEXED状態と再実行履歴を確認します。</p>
    </section>
    <section>
      <h2>引用確認</h2>
      <p>回答の根拠はページ番号、bbox、element_idで追跡します。</p>
      <figure>構造化 block citation 対応<figcaption>図1: block と citation の対応</figcaption></figure>
    </section>
  </main>
</body>
</html>
""",
    )
    _write_text(
        "approval-thread.eml",
        """Subject: 経費申請の承認
From: requester@example.com
To: manager@example.com
Cc: accounting@example.com
Date: Tue, 16 Jun 2026 09:00:00 +0900
Content-Type: text/plain; charset=utf-8

部門長へ

交通費1000円の申請について承認をお願いします。
添付: receipt-ja.png

承認します。Object Storageへ原本を保管してください。
""",
    )
    _write_png("receipt-ja.png", ["RECEIPT", "TOTAL 1000 JPY", "PAGE 1"])
    return 0


def _write(name: str, data: bytes) -> None:
    (FIXTURE_DIR / name).write_bytes(data)


def _write_text(name: str, text: str) -> None:
    (FIXTURE_DIR / name).write_text(text, encoding="utf-8")


def _write_docx(name: str, paragraphs: list[str]) -> None:
    body = "".join(
        f"<w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>" for text in paragraphs
    )
    _write_zip(
        name,
        {
            "[Content_Types].xml": (
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                f'<Default Extension="rels" ContentType="{PACKAGE_RELATIONSHIPS_CONTENT_TYPE}"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/word/document.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                "</Types>"
            ),
            "_rels/.rels": (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                f'Type="{OFFICE_DOCUMENT_RELATIONSHIP}" '
                'Target="word/document.xml"/>'
                "</Relationships>"
            ),
            "word/document.xml": (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body>{body}</w:body></w:document>"
            ),
        },
    )


def _write_pptx(name: str, slides: list[str]) -> None:
    slide_overrides = "".join(
        f'<Override PartName="/ppt/slides/slide{index}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        for index in range(1, len(slides) + 1)
    )
    slide_ids = "".join(
        f'<p:sldId id="{255 + index}" r:id="rId{index}"/>'
        for index in range(1, len(slides) + 1)
    )
    slide_relationships = "".join(
        f'<Relationship Id="rId{index}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" '
        f'Target="slides/slide{index}.xml"/>'
        for index in range(1, len(slides) + 1)
    )
    files = {
        "[Content_Types].xml": (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            f'<Default Extension="rels" ContentType="{PACKAGE_RELATIONSHIPS_CONTENT_TYPE}"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/ppt/presentation.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
            f"{slide_overrides}"
            "</Types>"
        ),
        "_rels/.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            f'Type="{OFFICE_DOCUMENT_RELATIONSHIP}" '
            'Target="ppt/presentation.xml"/>'
            "</Relationships>"
        ),
        "ppt/_rels/presentation.xml.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{slide_relationships}</Relationships>"
        ),
        "ppt/presentation.xml": (
            '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<p:sldIdLst>{slide_ids}</p:sldIdLst>"
            '<p:sldSz cx="9144000" cy="5143500" type="screen16x9"/>'
            '<p:notesSz cx="6858000" cy="9144000"/>'
            "</p:presentation>"
        ),
    }
    for index, text in enumerate(slides, start=1):
        files[f"ppt/slides/slide{index}.xml"] = (
            '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            "<p:cSld><p:spTree>"
            '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
            "<p:grpSpPr/>"
            '<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title"/>'
            '<p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
            "<p:spPr/>"
            f"<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>{escape(text)}</a:t>"
            "</a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
        )
    _write_zip(name, files)


def _write_xlsx(name: str, *, long_table: bool) -> None:
    rows = _expense_rows(long_table=long_table)
    shared = _shared_strings(rows)
    _write_zip(
        name,
        {
            "[Content_Types].xml": (
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                f'<Default Extension="rels" ContentType="{PACKAGE_RELATIONSHIPS_CONTENT_TYPE}"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/xl/workbook.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/worksheets/sheet1.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                '<Override PartName="/xl/sharedStrings.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
                '<Override PartName="/xl/styles.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                "</Types>"
            ),
            "_rels/.rels": (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                f'Type="{OFFICE_DOCUMENT_RELATIONSHIP}" '
                'Target="xl/workbook.xml"/>'
                "</Relationships>"
            ),
            "xl/_rels/workbook.xml.rels": (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                'Target="worksheets/sheet1.xml"/>'
                '<Relationship Id="rId2" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" '
                'Target="sharedStrings.xml"/>'
                '<Relationship Id="rId3" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
                'Target="styles.xml"/>'
                "</Relationships>"
            ),
            "xl/workbook.xml": (
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="経費" sheetId="1" r:id="rId1"/></sheets>'
                "</workbook>"
            ),
            "xl/styles.xml": (
                '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
                '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
                '<borders count="1"><border/></borders>'
                '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
                "</styleSheet>"
            ),
            "xl/sharedStrings.xml": _shared_strings_xml(shared),
            "xl/worksheets/sheet1.xml": _sheet_xml(rows, shared),
        },
    )


def _write_tsv(name: str, *, long_table: bool) -> None:
    rows = _expense_rows(long_table=long_table)
    _write_text(name, "\n".join("\t".join(row) for row in rows) + "\n")


def _expense_rows(*, long_table: bool) -> list[list[str]]:
    return [["日付", "部門", "品目", "金額"]] + [
        [
            f"2026-06-{day:02d}",
            "営業",
            "交通費",
            "1000円" if day == 3 else f"{day * 100}円",
        ]
        for day in range(1, 31 if long_table else 5)
    ]


def _write_broken_xlsx(name: str) -> None:
    rows = [["日付", "部門", "金額"], ["2026-06-03", "営業", "1000円"]]
    shared = _shared_strings(rows)
    _write_zip(
        name,
        {
            "xl/sharedStrings.xml": _shared_strings_xml(shared),
            "xl/worksheets/sheet1.xml": _sheet_xml(rows, shared),
            "xl/worksheets/sheet2.xml": "<worksheet><sheetData><row><c><v>broken",
        },
    )


def _shared_strings(rows: list[list[str]]) -> dict[str, int]:
    values: dict[str, int] = {}
    for row in rows:
        for cell in row:
            values.setdefault(cell, len(values))
    return values


def _shared_strings_xml(shared: dict[str, int]) -> str:
    ordered = sorted(shared, key=shared.get)
    values = "".join(f"<si><t>{escape(value)}</t></si>" for value in ordered)
    return (
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"{values}</sst>"
    )


def _sheet_xml(rows: list[list[str]], shared: dict[str, int]) -> str:
    row_xml = []
    for row_number, row in enumerate(rows, start=1):
        cells = "".join(
            f'<c r="{chr(65 + col)}{row_number}" t="s"><v>{shared[cell]}</v></c>'
            for col, cell in enumerate(row)
        )
        row_xml.append(f'<row r="{row_number}">{cells}</row>')
    return (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(row_xml)}</sheetData></worksheet>"
    )


def _write_zip(name: str, files: dict[str, str]) -> None:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    _write(name, output.getvalue())


def _text_pdf(lines: list[str], *, two_column: bool = False) -> bytes:
    if not two_column:
        commands = ["BT /F1 12 Tf 72 760 Td 16 TL"]
        for index, line in enumerate(lines):
            if index:
                commands.append("T*")
            commands.append(f"({_pdf_escape(line)}) Tj")
        commands.append("ET")
    else:
        left = lines[:3]
        right = lines[3:]
        commands = ["BT /F1 12 Tf 54 760 Td 16 TL"]
        for index, line in enumerate(left):
            if index:
                commands.append("T*")
            commands.append(f"({_pdf_escape(line)}) Tj")
        commands.append("ET")
        commands.append("BT /F1 12 Tf 306 760 Td 16 TL")
        for index, line in enumerate(right):
            if index:
                commands.append("T*")
            commands.append(f"({_pdf_escape(line)}) Tj")
        commands.append("ET")
    stream = "\n".join(commands).encode("ascii")
    return _pdf_with_text_stream(stream)


def _image_pdf(lines: list[str]) -> bytes:
    width, height, pixels = _bitmap_text(lines, scale=4, padding=18)
    image = zlib.compress(pixels)
    content = (
        b"q\n" + f"{width} 0 0 {height} 72 520 cm\n".encode("ascii") + b"/Im1 Do\nQ\n"
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /XObject << /Im1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        (
            f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
            "/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
            f"/Length {len(image)} >>\nstream\n".encode("ascii")
            + image
            + b"\nendstream"
        ),
        _stream_object(content),
    ]
    return _pdf_document(objects)


def _pdf_with_text_stream(stream: bytes) -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        _stream_object(stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    return _pdf_document(objects)


def _stream_object(stream: bytes) -> bytes:
    return (
        f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
        + stream
        + b"\nendstream"
    )


def _pdf_document(objects: list[bytes]) -> bytes:
    body = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body.extend(f"{index} 0 obj\n".encode("ascii"))
        body.extend(obj)
        body.extend(b"\nendobj\n")
    xref_start = len(body)
    body.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    body.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        body.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    body.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_start}\n%%EOF\n".encode("ascii")
    )
    return bytes(body)


def _pdf_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _write_png(name: str, lines: list[str]) -> None:
    width, height, pixels = _bitmap_text(lines, scale=4, padding=12)
    raw = bytearray()
    row_length = width * 3
    for row in range(height):
        raw.append(0)
        start = row * row_length
        raw.extend(pixels[start : start + row_length])
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw)))
        + _png_chunk(b"IEND", b"")
    )
    _write(name, png)


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
    return pack(">I", len(data)) + kind + data + pack(">I", checksum)


def _bitmap_text(
    lines: list[str], *, scale: int, padding: int
) -> tuple[int, int, bytes]:
    normalized = [line.upper() for line in lines]
    width = max(len(line) for line in normalized) * 6 * scale + padding * 2
    height = len(normalized) * 9 * scale + padding * 2
    pixels = bytearray([255] * width * height * 3)
    for line_index, line in enumerate(normalized):
        y = padding + line_index * 9 * scale
        x = padding
        for char in line:
            glyph = _FONT_5X7.get(char, _FONT_5X7[" "])
            _draw_glyph(pixels, width=width, x=x, y=y, glyph=glyph, scale=scale)
            x += 6 * scale
    return width, height, bytes(pixels)


def _draw_glyph(
    pixels: bytearray,
    *,
    width: int,
    x: int,
    y: int,
    glyph: list[str],
    scale: int,
) -> None:
    for row, pattern in enumerate(glyph):
        for col, value in enumerate(pattern):
            if value != "1":
                continue
            for dy in range(scale):
                for dx in range(scale):
                    index = ((y + row * scale + dy) * width + x + col * scale + dx) * 3
                    pixels[index : index + 3] = b"\x00\x00\x00"


if __name__ == "__main__":
    raise SystemExit(main())
