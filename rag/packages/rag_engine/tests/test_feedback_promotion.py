"""feedback promotion の挙動を保護するテスト。"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from rag_engine.knowledge.feedback_promotion import (
    append_retrieval_eval_cases,
    feedback_to_retrieval_eval_case,
    is_feedback_review_approved,
    reviewed_feedback_records,
)


class FeedbackPromotionTests(unittest.TestCase):
    def test_feedback_to_retrieval_eval_case_requires_review_approval(self):
        pending = _feedback_record(status="review_pending", decision="approved")

        self.assertFalse(is_feedback_review_approved(pending))
        with self.assertRaises(ValueError):
            feedback_to_retrieval_eval_case(pending)

    def test_inactive_lifecycle_statuses_cannot_be_promoted_by_stale_decision(self):
        for status in ("draft", "deprecated", "superseded"):
            with self.subTest(status=status):
                record = _feedback_record(status=status, decision="approved")

                self.assertFalse(is_feedback_review_approved(record))
                with self.assertRaises(ValueError):
                    feedback_to_retrieval_eval_case(record)

    def test_feedback_to_retrieval_eval_case_preserves_source_metadata(self):
        record = _feedback_record(status="approved", decision="approved")

        case = feedback_to_retrieval_eval_case(record)

        self.assertEqual(case["case_id"], "feedback-fb-1")
        self.assertEqual(case["question"], "拠点名を変更する方法は？")
        self.assertEqual(case["classification_filter"], {"large_category": "10_販売管理"})
        self.assertEqual(case["expected_child_ids"], ["chunk-1"])
        self.assertIn("feedback", case["tags"])
        self.assertIn("incorrect_answer", case["tags"])
        self.assertEqual(case["source_feedback"]["feedback_id"], "fb-1")
        self.assertEqual(case["source_feedback"]["answer_id"], "answer-1")
        self.assertIn("倉庫マスタ登録", case["expected_terms"])

    def test_reviewed_feedback_records_and_append_eval_cases_dedupe(self):
        records = [
            _feedback_record(feedback_id="fb-1", status="approved"),
            _feedback_record(feedback_id="fb-2", status="review_pending"),
        ]
        cases = [feedback_to_retrieval_eval_case(record) for record in reviewed_feedback_records(records)]

        with TemporaryDirectory() as tmp:
            dataset_path = Path(tmp) / "retrieval_representative_questions.json"
            dataset_path.write_text(
                json.dumps({"schema_version": 1, "cases": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            first_added = append_retrieval_eval_cases(dataset_path, cases)
            second_added = append_retrieval_eval_cases(dataset_path, cases)
            dataset = json.loads(dataset_path.read_text(encoding="utf-8"))

        self.assertEqual(first_added, 1)
        self.assertEqual(second_added, 0)
        self.assertEqual([case["case_id"] for case in dataset["cases"]], ["feedback-fb-1"])


def _feedback_record(feedback_id="fb-1", status="review_pending", decision="") -> dict:
    return {
        "schema_version": 1,
        "feedback_id": feedback_id,
        "status": status,
        "feedback_type": "incorrect_answer",
        "run_id": "run-1",
        "answer_id": "answer-1",
        "question": "拠点名を変更する方法は？",
        "corrected_answer": "倉庫マスタ登録画面で拠点倉庫名を変更し、実行します。",
        "classification_filter": {"large_category": "10_販売管理"},
        "answer_trace": {
            "question": "拠点名を変更する方法は？",
            "answer_text": "古い回答",
            "answer_payload_path": ".runs/run-1/answers/answer-1.json",
            "evidence_item_ids": ["chunk-1"],
            "classification_filter": {"large_category": "10_販売管理"},
        },
        "review": {
            "decision": decision,
            "promote_to": ["retrieval_eval"],
            "reviewer": "qa",
            "reviewed_at": "2026-09-08T00:00:00Z",
        },
    }


if __name__ == "__main__":
    unittest.main()
