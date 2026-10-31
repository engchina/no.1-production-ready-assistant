"""approved faq corpus の挙動を保護するテスト。"""

import json
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APPROVED_FAQ_PATH = PROJECT_ROOT / "knowledge_assets" / "approved_faq" / "approved_faq_golden_qa.json"
EXAMPLES_QA_PATH = PROJECT_ROOT / "examples" / "qa.json"


# Approved FAQ は顧客の問い合わせ由来なので repo 外にある。ローカルにある環境だけで照合する。
@unittest.skipUnless(APPROVED_FAQ_PATH.is_file(), "ローカルの Approved FAQ がない")
class ApprovedFaqCorpusTests(unittest.TestCase):
    def test_approved_faq_corpus_is_separate_from_examples_fixture(self):
        self.assertNotEqual(APPROVED_FAQ_PATH, EXAMPLES_QA_PATH)

        data = json.loads(APPROVED_FAQ_PATH.read_text(encoding="utf-8"))

        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["name"], "approved_faq_golden_qa")
        self.assertIsInstance(data["records"], list)
        for record in data["records"]:
            self.assertIsInstance(record, dict)
            self.assertTrue(str(record.get("question") or "").strip())
            self.assertTrue(str(record.get("approved_answer") or "").strip())
            self.assertNotIn("standard_answer", record)
        self.assertIn("examples/qa.json remains a test and debugging fixture only.", data["usage_policy"]["examples_qa_role"])

    def test_approved_faq_record_schema_preserves_governed_metadata(self):
        data = json.loads(APPROVED_FAQ_PATH.read_text(encoding="utf-8"))
        record_schema = data["record_schema"]

        for field in (
            "id",
            "question",
            "approved_answer",
            "status",
            "business",
            "classification",
            "source",
            "review",
            "timestamps",
        ):
            self.assertIn(field, record_schema["required"])

        self.assertIn("approved", record_schema["status_values"])
        self.assertIn("stale_review_needed", record_schema["status_values"])
        self.assertIn("chunk_ids", record_schema["fields"]["source"])
        self.assertIn("next_review_at", record_schema["fields"]["timestamps"])


if __name__ == "__main__":
    unittest.main()
