"""jsonl の挙動を保護するテスト。"""

import json
import unittest

from rag_engine.models.layout import LayoutRecord, PageImage
from rag_engine.resources.jsonl import dumps_jsonl


class JsonlTests(unittest.TestCase):
    def test_jsonl_contains_legacy_fields(self):
        page = PageImage(page=1, width=200, height=200, pdf_width=100, pdf_height=100, image_path="page.png")
        record = LayoutRecord(
            id="r1",
            engine="docling",
            page=1,
            seq_no=1,
            bbox=[20, 40, 120, 160],
            coord_system="image_top_left",
            page_width=200,
            page_height=200,
            category="Text",
            text="本文",
            confidence=0.91,
            raw_type="text",
        )
        line = dumps_jsonl([record], [page]).strip()
        payload = json.loads(line)
        self.assertEqual(payload["sentence"], "本文")
        self.assertEqual(payload["detected_type"], "Text")
        self.assertEqual(payload["text_location"]["location"][0], [10.0, 20.0, 60.0, 80.0])


if __name__ == "__main__":
    unittest.main()
