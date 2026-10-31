"""rendering の挙動を保護するテスト。"""

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from rag_engine.parsing.rendering import prepare_source_for_analysis, source_file_kind, source_frame_warnings


class SourceRenderingTests(unittest.TestCase):
    def test_source_file_kind_accepts_pdf_and_images(self):
        self.assertEqual(source_file_kind("sample.PDF"), "pdf")
        self.assertEqual(source_file_kind("scan.JPG"), "image")
        self.assertEqual(source_file_kind("scan.jfif"), "image")
        self.assertEqual(source_file_kind("diagram.gif"), "image")
        self.assertEqual(source_file_kind("page.webp"), "image")

    def test_source_file_kind_rejects_unsupported_extension(self):
        with self.assertRaises(ValueError):
            source_file_kind("notes.txt")

    def test_prepare_source_for_analysis_converts_image_to_single_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_path = tmp_path / "scan.png"
            Image.new("RGBA", (80, 40), (20, 120, 200, 128)).save(source_path)

            source_pdf_path, pages = prepare_source_for_analysis(source_path, [1], tmp_path / "run", dpi=150)

            self.assertTrue(Path(source_pdf_path).exists())
            self.assertEqual(Path(source_pdf_path).name, "source.pdf")
            self.assertEqual(len(pages), 1)
            self.assertEqual(pages[0].page, 1)
            self.assertEqual(pages[0].width, 80)
            self.assertEqual(pages[0].height, 40)
            self.assertTrue(Path(pages[0].image_path).exists())
            with Image.open(pages[0].image_path) as image:
                self.assertEqual(image.mode, "RGB")
                self.assertEqual(image.size, (80, 40))

    def test_multi_frame_image_reports_unparsed_frames(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            multi = Path(temp_dir) / "scan.tiff"
            frames = [Image.new("RGB", (20, 20), color) for color in ("white", "black", "red")]
            frames[0].save(multi, save_all=True, append_images=frames[1:])
            single = Path(temp_dir) / "photo.png"
            frames[0].save(single)

            self.assertEqual(
                source_frame_warnings(multi),
                ["画像に 3 フレームあります。先頭フレームだけを解析し、残り 2 フレームは対象外です。"],
            )
            self.assertEqual(source_frame_warnings(single), [])
            self.assertEqual(source_frame_warnings(Path(temp_dir) / "missing.pdf"), [])

    def test_prepare_source_for_analysis_rejects_image_page_other_than_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_path = tmp_path / "scan.png"
            Image.new("RGB", (10, 10), "white").save(source_path)

            with self.assertRaises(ValueError):
                prepare_source_for_analysis(source_path, [2], tmp_path / "run", dpi=150)


if __name__ == "__main__":
    unittest.main()
