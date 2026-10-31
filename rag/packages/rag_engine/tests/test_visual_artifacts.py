"""意味を持つ visual block の crop 保存を保護するテスト。"""

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from rag_engine.models.layout import LayoutRecord, PageImage
from rag_engine.parsing.visual_artifacts import persist_semantic_visual_crops


class VisualArtifactTests(unittest.TestCase):
    def test_persists_table_and_content_picture_crops_but_skips_nonsemantic_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            page_path = run_dir / "pages" / "page_0001.png"
            page_path.parent.mkdir(parents=True)
            Image.new("RGB", (100, 100), "white").save(page_path)
            page = PageImage(1, 100, 100, 100, 100, str(page_path))
            table = _record("docling-p1-1", "Table", "table", [10, 20, 70, 80])
            flowchart = _record("docling-p1-2", "Picture", "flowchart", [20, 20, 60, 60])
            ocr_aggregate = _record("docling-p1-3", "Picture", "picture_ocr_text", [20, 20, 60, 60])
            footer_logo = _record("docling-p1-4", "Picture", "logo", [92, 94, 99, 99])
            form = _record("docling-p1-5", "Text", "form", [5, 5, 90, 90])
            background = _record("docling-p1-6", "Picture", "background", [0, 0, 100, 100])

            stats = persist_semantic_visual_crops(
                [table, flowchart, ocr_aggregate, footer_logo, form, background],
                [page],
                run_dir=run_dir,
            )

            self.assertEqual((stats.targets, stats.saved, stats.failed), (3, 3, 0))
            self.assertEqual(table.raw["crop_path"], "docling/visuals/docling-p1-1.png")
            self.assertEqual(table.raw["visual_artifact_type"], "table")
            self.assertEqual(flowchart.raw["visual_artifact_type"], "flowchart")
            self.assertTrue((run_dir / table.raw["crop_path"]).is_file())
            self.assertTrue((run_dir / flowchart.raw["crop_path"]).is_file())
            self.assertEqual(form.raw["visual_artifact_type"], "form")
            self.assertTrue((run_dir / form.raw["crop_path"]).is_file())
            self.assertNotIn("crop_path", ocr_aggregate.raw)
            self.assertNotIn("crop_path", footer_logo.raw)
            self.assertNotIn("crop_path", background.raw)
            self.assertEqual(footer_logo.raw["visual_role"], "decorative")
            self.assertTrue(footer_logo.raw["rag_excluded"])
            self.assertEqual(background.raw["visual_role"], "decorative")
            self.assertTrue(background.raw["rag_excluded"])

    def test_replaces_stale_crop_path_when_recreating_preview_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            page_path = run_dir / "page.png"
            Image.new("RGB", (80, 60), "white").save(page_path)
            page = PageImage(1, 80, 60, 80, 60, str(page_path))
            table = _record("docling-p1-1", "Table", "table", [5, 5, 40, 30])
            table.raw["crop_path"] = "old-run/table.png"

            stats = persist_semantic_visual_crops([table], [page], run_dir=run_dir)

            self.assertEqual((stats.saved, stats.failed), (1, 0))
            self.assertEqual(table.raw["crop_path"], "docling/visuals/docling-p1-1.png")
            self.assertTrue((run_dir / table.raw["crop_path"]).is_file())


def _record(record_id: str, category: str, raw_type: str, bbox: list[float]) -> LayoutRecord:
    return LayoutRecord(
        id=record_id,
        engine="docling",
        page=1,
        seq_no=int(record_id.rsplit("-", 1)[-1]),
        bbox=bbox,
        coord_system="image_top_left",
        page_width=100,
        page_height=100,
        category=category,
        text="<table><tr><td>value</td></tr></table>" if category == "Table" else "",
        raw_type=raw_type,
        raw={},
    )


if __name__ == "__main__":
    unittest.main()
