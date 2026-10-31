"""analysis preview の挙動を保護するテスト。"""

import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image

from rag_engine.config import get_settings
from rag_engine.knowledge.classification import classification_from_selection
from rag_engine.models.layout import LayoutRecord, PageImage
from rag_engine.parsing.analysis import create_run_id, preview_pdf
from rag_engine.parsing.parse_inputs import file_sha256, save_parse_input


class PreviewPdfTests(unittest.TestCase):
    def test_preview_renders_all_pages_without_records_or_engines(self):
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            pdf_path = temp_path / "manual.pdf"
            pdf_path.write_bytes(b"%PDF-preview")
            settings = replace(get_settings(), output_dir=temp_path / "runs", render_dpi=200)

            def fake_prepare(source_path, page_numbers, output_dir, dpi):
                pages_dir = Path(output_dir) / "pages"
                pages_dir.mkdir(parents=True, exist_ok=True)
                pages = [
                    PageImage(
                        page=page,
                        width=100,
                        height=200,
                        pdf_width=50,
                        pdf_height=100,
                        image_path=str(pages_dir / f"page_{page:04d}.png"),
                    )
                    for page in page_numbers
                ]
                return str(Path(output_dir) / "source.pdf"), pages

            with (
                patch("rag_engine.parsing.analysis.get_source_page_count", return_value=3),
                patch("rag_engine.parsing.analysis.prepare_source_for_analysis", side_effect=fake_prepare) as prepare,
            ):
                run = preview_pdf(pdf_path, settings, dpi=144)

            prepare.assert_called_once()
            self.assertEqual(prepare.call_args.args[1], [1, 2, 3])
            self.assertEqual(prepare.call_args.args[3], 144)
            self.assertEqual([page.page for page in run.pages], [1, 2, 3])
            self.assertEqual(run.records, [])
            self.assertEqual(run.statuses, [])
            self.assertEqual(run.warnings, [])
            self.assertTrue(Path(run.viewer_data_path).exists())
            self.assertEqual(Path(run.jsonl_path).read_text(encoding="utf-8"), "")

    def test_run_id_reuses_precomputed_digest_without_reading_the_file(self):
        run_id = create_run_id(Path("not-read.pdf"), "1", ["docling"], source_sha256="0" * 64)

        self.assertRegex(run_id, r"^[0-9a-f]{16}$")

    def test_preview_warns_about_unparsed_image_frames(self):
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            image_path = temp_path / "scan.tiff"
            frames = [Image.new("RGB", (40, 40), color) for color in ("white", "black")]
            frames[0].save(image_path, save_all=True, append_images=frames[1:])
            settings = replace(get_settings(), output_dir=temp_path / "runs")

            run = preview_pdf(image_path, settings)

            self.assertEqual(len(run.pages), 1)
            self.assertEqual(len(run.warnings), 1)
            self.assertIn("2 フレーム", run.warnings[0])

    def test_preview_restores_matching_engine_without_building_adapters(self):
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            pdf_path = temp_path / "manual.pdf"
            pdf_path.write_bytes(b"%PDF-cached")
            settings = replace(get_settings(), output_dir=temp_path / "runs", render_dpi=200)
            saved_page = PageImage(1, 100, 200, 50, 100, "saved.png")
            saved_record = LayoutRecord(
                id="docling-p1-1",
                engine="docling",
                page=1,
                seq_no=1,
                bbox=[10, 20, 30, 40],
                coord_system="image_top_left",
                page_width=100,
                page_height=200,
                category="Text",
                text="cached",
            )
            save_parse_input(
                output_dir=settings.output_dir,
                source_path=pdf_path,
                source_sha256=file_sha256(pdf_path),
                page_count=1,
                engine_id="docling",
                engine_label="Docling",
                pages=[saved_page],
                records=[saved_record],
                dpi=300,
                min_confidence=0.5,
                use_docling_vision=True,
            )

            def fake_prepare(source_path, page_numbers, output_dir, dpi):
                return str(source_path), [PageImage(1, 200, 100, 50, 100, str(Path(output_dir) / "page.png"))]

            with (
                patch("rag_engine.parsing.analysis.get_source_page_count", return_value=1),
                patch("rag_engine.parsing.analysis.prepare_source_for_analysis", side_effect=fake_prepare),
                patch("rag_engine.parsing.analysis.build_adapters", side_effect=AssertionError("must not parse")),
            ):
                run = preview_pdf(pdf_path, settings, dpi=144)

            self.assertEqual([status.engine for status in run.statuses], ["docling"])
            self.assertEqual(run.records[0].bbox, [20.0, 10.0, 60.0, 20.0])
            self.assertEqual((run.records[0].page_width, run.records[0].page_height), (200, 100))
            self.assertTrue(run.statuses[0].use_docling_vision)
            self.assertEqual(run.warnings, [])
            viewer = json.loads(Path(run.viewer_data_path).read_text(encoding="utf-8"))
            self.assertEqual(viewer["warnings"], [])
            self.assertEqual(viewer["engines"][0]["engine"], "docling")
            self.assertTrue(viewer["engines"][0]["use_docling_vision"])
            self.assertIn("cached", Path(run.jsonl_path).read_text(encoding="utf-8"))

    def test_preview_restores_saved_classification_metadata(self):
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            pdf_path = temp_path / "manual.pdf"
            pdf_path.write_bytes(b"%PDF-cached")
            settings = replace(get_settings(), output_dir=temp_path / "runs", render_dpi=200)
            saved_page = PageImage(1, 100, 200, 50, 100, "saved.png")
            classification = classification_from_selection(
                source_path=pdf_path,
                large_category="在庫管理",
                middle_category="操作説明書",
                small_category="倉庫連携",
            )
            save_parse_input(
                output_dir=settings.output_dir,
                source_path=pdf_path,
                source_sha256=file_sha256(pdf_path),
                page_count=1,
                engine_id="docling",
                engine_label="Docling",
                pages=[saved_page],
                records=[],
                dpi=300,
                min_confidence=0.5,
                use_docling_vision=False,
                classification=classification,
            )

            def fake_prepare(source_path, page_numbers, output_dir, dpi):
                return str(source_path), [PageImage(1, 100, 200, 50, 100, str(Path(output_dir) / "page.png"))]

            with (
                patch("rag_engine.parsing.analysis.get_source_page_count", return_value=1),
                patch("rag_engine.parsing.analysis.prepare_source_for_analysis", side_effect=fake_prepare),
            ):
                run = preview_pdf(pdf_path, settings)

        self.assertEqual(run.classification["large_category"], "在庫管理")
        self.assertEqual(run.classification["middle_category"], "操作説明書")
        self.assertEqual(run.classification["small_category"], "倉庫連携")

    def test_preview_recreates_native_table_crop_for_current_run(self):
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            pdf_path = temp_path / "manual.pdf"
            pdf_path.write_bytes(b"%PDF-table")
            settings = replace(get_settings(), output_dir=temp_path / "runs", render_dpi=200)
            saved_page = PageImage(1, 100, 100, 100, 100, "saved.png")
            saved_record = LayoutRecord(
                id="docling-p1-1",
                engine="docling",
                page=1,
                seq_no=1,
                bbox=[10, 20, 80, 70],
                coord_system="image_top_left",
                page_width=100,
                page_height=100,
                category="Table",
                text="<table><tr><td>value</td></tr></table>",
                raw_type="table",
                raw={"crop_path": "old-run/table.png"},
            )
            save_parse_input(
                output_dir=settings.output_dir,
                source_path=pdf_path,
                source_sha256=file_sha256(pdf_path),
                page_count=1,
                engine_id="docling",
                engine_label="Docling",
                pages=[saved_page],
                records=[saved_record],
                dpi=200,
                min_confidence=0.0,
                use_docling_vision=False,
            )

            def fake_prepare(source_path, page_numbers, output_dir, dpi):
                page_path = Path(output_dir) / "pages" / "page_0001.png"
                page_path.parent.mkdir(parents=True)
                Image.new("RGB", (100, 100), "white").save(page_path)
                return str(source_path), [PageImage(1, 100, 100, 100, 100, str(page_path))]

            with (
                patch("rag_engine.parsing.analysis.get_source_page_count", return_value=1),
                patch("rag_engine.parsing.analysis.prepare_source_for_analysis", side_effect=fake_prepare),
            ):
                run = preview_pdf(pdf_path, settings)

            crop_path = run.records[0].raw["crop_path"]
            self.assertEqual(crop_path, "docling/visuals/docling-p1-1.png")
            self.assertTrue((Path(run.output_dir) / crop_path).is_file())

    def test_preview_exposes_cache_mismatch_warning_without_records(self):
        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            pdf_path = temp_path / "manual.pdf"
            pdf_path.write_bytes(b"old")
            settings = replace(get_settings(), output_dir=temp_path / "runs")
            saved_page = PageImage(1, 100, 200, 50, 100, "saved.png")
            save_parse_input(
                output_dir=settings.output_dir,
                source_path=pdf_path,
                source_sha256=file_sha256(pdf_path),
                page_count=1,
                engine_id="docling",
                engine_label="Docling",
                pages=[saved_page],
                records=[],
                dpi=300,
                min_confidence=0.5,
                use_docling_vision=False,
            )
            pdf_path.write_bytes(b"new")

            def fake_prepare(source_path, page_numbers, output_dir, dpi):
                return str(source_path), [PageImage(1, 100, 200, 50, 100, str(Path(output_dir) / "page.png"))]

            with (
                patch("rag_engine.parsing.analysis.get_source_page_count", return_value=1),
                patch("rag_engine.parsing.analysis.prepare_source_for_analysis", side_effect=fake_prepare),
            ):
                run = preview_pdf(pdf_path, settings)

            self.assertEqual(run.records, [])
            self.assertEqual(run.statuses, [])
            self.assertEqual(len(run.warnings), 1)
            viewer = json.loads(Path(run.viewer_data_path).read_text(encoding="utf-8"))
            self.assertEqual(viewer["warnings"], run.warnings)


if __name__ == "__main__":
    unittest.main()
