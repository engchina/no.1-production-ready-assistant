"""pdfium rendering の挙動を保護するテスト。"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rag_engine.parsing.rendering import get_pdf_page_count, pdf_text_lines_in_bbox, render_pdf_pages


class _FakeImage:
    size = (200, 100)

    def convert(self, mode):
        if mode != "RGB":
            raise AssertionError(mode)
        return self

    def save(self, path):
        Path(path).write_bytes(b"png")


class _FakeBitmap:
    def __init__(self):
        self.closed = False

    def to_pil(self):
        return _FakeImage()

    def close(self):
        self.closed = True


class _FakeTextPage:
    def __init__(self, text="行1\n行2\n行3"):
        self.closed = False
        self.text = text

    def get_text_range(self):
        return self.text

    def get_text_bounded(self, *, left, bottom, right, top):
        if bottom > top:
            raise AssertionError((bottom, top))
        return "行2\n行3の途中"

    def close(self):
        self.closed = True


class _FakePage:
    def __init__(self):
        self.closed = False
        self.bitmap = None
        self.textpage = None

    def get_height(self):
        return 50.0

    def get_textpage(self):
        self.textpage = _FakeTextPage()
        return self.textpage

    def get_size(self):
        return (100.0, 50.0)

    def render(self, *, scale, rotation):
        if scale != 2.0 or rotation != 0:
            raise AssertionError((scale, rotation))
        self.bitmap = _FakeBitmap()
        return self.bitmap

    def close(self):
        self.closed = True


class _FakeDocument:
    instances = []

    def __init__(self, path):
        self.path = path
        self.closed = False
        self.pages = [_FakePage(), _FakePage()]
        self.instances.append(self)

    def __len__(self):
        return len(self.pages)

    def __getitem__(self, index):
        return self.pages[index]

    def close(self):
        self.closed = True


class PdfiumRenderingTests(unittest.TestCase):
    def setUp(self):
        _FakeDocument.instances.clear()
        self.pdfium = SimpleNamespace(PdfDocument=_FakeDocument)

    def test_get_pdf_page_count_closes_document(self):
        with patch("rag_engine.parsing.rendering._import_pdfium", return_value=self.pdfium):
            self.assertEqual(get_pdf_page_count("sample.pdf"), 2)

        self.assertTrue(_FakeDocument.instances[0].closed)

    def test_render_pdf_pages_uses_pdfium_dimensions_and_closes_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("rag_engine.parsing.rendering._import_pdfium", return_value=self.pdfium):
                pages = render_pdf_pages("sample.pdf", [1], tmp, dpi=144)

            self.assertTrue(Path(pages[0].image_path).exists())

        document = _FakeDocument.instances[0]
        page = document.pages[0]
        self.assertEqual((pages[0].width, pages[0].height), (200, 100))
        self.assertEqual((pages[0].pdf_width, pages[0].pdf_height), (100.0, 50.0))
        self.assertTrue(page.bitmap.closed)
        self.assertTrue(page.closed)
        self.assertTrue(document.closed)


if __name__ == "__main__":
    unittest.main()

    def test_pdf_text_lines_in_bbox_closes_page_and_textpage(self):
        # render_pdf_pages と同じく page / textpage を明示的に閉じる (#791)。
        with patch("rag_engine.parsing.rendering._import_pdfium", return_value=self.pdfium):
            lines = pdf_text_lines_in_bbox("source.pdf", 2, {"l": 1, "t": 40, "r": 90, "b": 10, "coord_origin": "TOPLEFT"})

        self.assertEqual(lines, ["行2"])
        document = _FakeDocument.instances[-1]
        page = document.pages[1]
        self.assertTrue(page.textpage.closed)
        self.assertTrue(page.closed)
        self.assertTrue(document.closed)

    def test_pdf_text_lines_in_bbox_closes_resources_on_error(self):
        with patch("rag_engine.parsing.rendering._import_pdfium", return_value=self.pdfium), self.assertRaises(KeyError):
            pdf_text_lines_in_bbox("source.pdf", 1, {"l": 1, "t": 40})  # r / b が無い

        page = _FakeDocument.instances[-1].pages[0]
        self.assertTrue(page.closed)
        self.assertTrue(_FakeDocument.instances[-1].closed)
