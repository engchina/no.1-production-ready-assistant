"""answer feedback の挙動を保護するテスト。"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from rag_engine.knowledge.answer_feedback import (
    answer_feedback_path,
    append_answer_feedback,
    create_answer_feedback_record,
)


class AnswerFeedbackTests(unittest.TestCase):
    def test_create_and_append_answer_feedback_record_with_trace_summary(self):
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            answers_dir = output_dir / "run123" / "answers"
            answers_dir.mkdir(parents=True)
            (answers_dir / "answer456.json").write_text(
                json.dumps(
                    {
                        "question": "Q?",
                        "answer_text": "A.",
                        "confidence": "high",
                        "needs_human_review": False,
                        "answer_flow": "standard",
                        "retrieval_scope": "knowledge_base",
                        "classification_filter": {"large_category": "10_販売管理"},
                        "evidence_items": [{"id": "chunk-1"}, {"id": "chunk-2"}],
                        "retrieval_query_plan": {"text": ["Q?"]},
                        "runtime_knowledge": {"matched_terms": [{"term": "出庫伝票"}]},
                        "query_understanding": {"intent": "操作確認"},
                        "generated_queries": ["Q?", "Q variation"],
                        "text_search_queries": ["Q?"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            record = create_answer_feedback_record(
                output_dir=output_dir,
                run_id="run123",
                answer_id="answer456",
                feedback_type="wrong_evidence",
                question="Edited UI question?",
                comment="Evidence page is wrong.",
                corrected_answer="Corrected A.",
                classification_filter={"large_category": "20_在庫管理"},
            )
            path = append_answer_feedback(output_dir, record)

            self.assertEqual(path, answer_feedback_path(output_dir))
            saved = json.loads(path.read_text(encoding="utf-8").strip())

        self.assertEqual(saved["schema_version"], 1)
        self.assertEqual(saved["status"], "review_pending")
        self.assertEqual(saved["feedback_type"], "wrong_evidence")
        self.assertEqual(saved["question"], "Q?")
        self.assertEqual(saved["classification_filter"], {"large_category": "10_販売管理"})
        self.assertEqual(saved["answer_trace"]["evidence_item_ids"], ["chunk-1", "chunk-2"])
        self.assertEqual(saved["answer_trace"]["retrieval_query_plan"], {"text": ["Q?"]})
        self.assertEqual(saved["corrected_answer"], "Corrected A.")

    def test_rejects_missing_answer_payload(self):
        with TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            create_answer_feedback_record(
                output_dir=tmp,
                run_id="run123",
                answer_id="answer456",
                feedback_type="incorrect_answer",
            )

    def test_feedback_ids_are_unique_within_the_same_second(self):
        from rag_engine.knowledge.answer_feedback import _create_feedback_id

        ids = {_create_feedback_id(run_id="r", answer_id="a", feedback_type="correct", created_at="2026-01-01T00:00:00Z") for _ in range(5)}
        self.assertEqual(len(ids), 5)

    def test_rejects_invalid_path_parts_and_feedback_types(self):
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            answers_dir = output_dir / "run123" / "answers"
            answers_dir.mkdir(parents=True)
            (answers_dir / "answer456.json").write_text("{}", encoding="utf-8")

            with self.assertRaises(ValueError):
                create_answer_feedback_record(
                    output_dir=tmp,
                    run_id="../run123",
                    answer_id="answer456",
                    feedback_type="incorrect_answer",
                )
            with self.assertRaises(ValueError):
                create_answer_feedback_record(
                    output_dir=tmp,
                    run_id="run123",
                    answer_id="answer456",
                    feedback_type="unexpected",
                )


if __name__ == "__main__":
    unittest.main()
