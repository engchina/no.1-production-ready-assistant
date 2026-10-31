"""analysis vision の挙動を保護するテスト。"""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rag_engine.config import get_settings
from rag_engine.models.layout import LayoutRecord, PageImage
from rag_engine.parsing.analysis import analyze_pdf
from rag_engine.parsing.picture_descriptions import VisionDescriptionStats


class AnalysisVisionTests(unittest.TestCase):
    def test_docling_vision_stats_are_reported_without_changing_record_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.pdf"
            source.write_bytes(b"pdf")
            image = root / "page.png"
            image.write_bytes(b"png")
            page = PageImage(page=1, width=100, height=100, pdf_width=100, pdf_height=100, image_path=str(image))
            record = _picture_record()
            settings = replace(get_settings(), output_dir=root / "runs")
            adapter = _Adapter([record])

            with (
                patch("rag_engine.parsing.analysis.ENGINE_ORDER", ["docling"]),
                patch("rag_engine.parsing.analysis.build_adapters", return_value={"docling": adapter}),
                patch("rag_engine.parsing.analysis.get_source_page_count", return_value=1),
                patch("rag_engine.parsing.analysis.prepare_source_for_analysis", return_value=(str(source), [page])),
                patch("rag_engine.parsing.analysis.model_pool.trim_cuda_cache"),
                patch(
                    "rag_engine.parsing.picture_descriptions.describe_docling_pictures",
                    return_value=VisionDescriptionStats(2, 1, 1),
                ) as describe,
            ):
                run = analyze_pdf(source, "1", ["docling"], settings, use_docling_vision=True)

            self.assertEqual(len(run.records), 1)
            self.assertEqual(run.statuses[0].count, 1)
            self.assertTrue(run.statuses[0].use_docling_vision)
            self.assertIn("成功 1/2", run.statuses[0].message)
            self.assertIn("失敗 1", run.statuses[0].message)
            self.assertTrue(describe.called)

    def test_disabled_docling_vision_does_not_call_enrichment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.pdf"
            source.write_bytes(b"pdf")
            image = root / "page.png"
            image.write_bytes(b"png")
            page = PageImage(page=1, width=100, height=100, pdf_width=100, pdf_height=100, image_path=str(image))
            settings = replace(get_settings(), output_dir=root / "runs")

            with (
                patch("rag_engine.parsing.analysis.ENGINE_ORDER", ["docling"]),
                patch("rag_engine.parsing.analysis.build_adapters", return_value={"docling": _Adapter([_picture_record()])}),
                patch("rag_engine.parsing.analysis.get_source_page_count", return_value=1),
                patch("rag_engine.parsing.analysis.prepare_source_for_analysis", return_value=(str(source), [page])),
                patch("rag_engine.parsing.analysis.model_pool.trim_cuda_cache"),
                patch("rag_engine.parsing.picture_descriptions.describe_docling_pictures") as describe,
            ):
                run = analyze_pdf(source, "1", ["docling"], settings, use_docling_vision=False)

            self.assertFalse(describe.called)
            self.assertFalse(run.statuses[0].use_docling_vision)
            self.assertEqual(run.statuses[0].message, "解析が完了しました。")


class _Adapter:
    def __init__(self, records):
        self.records = records

    def availability(self):
        return SimpleNamespace(available=True, message="")

    def analyze(self, context):
        return self.records


def _picture_record() -> LayoutRecord:
    return LayoutRecord(
        id="docling-p1-1",
        engine="docling",
        page=1,
        seq_no=1,
        bbox=[10, 10, 20, 20],
        coord_system="image_top_left",
        page_width=100,
        page_height=100,
        category="Picture",
        raw={},
    )


if __name__ == "__main__":
    unittest.main()
