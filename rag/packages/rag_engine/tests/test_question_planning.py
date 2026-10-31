"""question planning の挙動を保護するテスト。"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from grounded_stub import AnswerOutput

from rag_engine.config import get_settings
from rag_engine.generation.answering import (
    QUESTION_DISPLAY_METADATA_SEPARATOR,
    QUESTION_PLAN_SEPARATOR,
    SIMPLE_RETRIEVAL_LABEL,
    STANDARD_ANSWER_FLOW_LABEL,
    AnswerContext,
    AnswerRecord,
    answer_question_result,
    answer_result_payload,
    extract_original_question,
)
from rag_engine.retrieval.question_planning import plan_question


class QuestionPlanningTests(unittest.TestCase):
    def test_plan_keeps_only_what_later_steps_use(self):
        """後続が使うのは質問理解だけ。画像の添付は質問ではなく回答のモデルで決める（#3444）。"""
        plain = plan_question("再発行できない条件を教えてください。")
        self.assertIsNotNone(plain.inquiry_conditions)
        self.assertEqual(set(plain.to_payload()), {"original_question"})

    def test_answer_result_includes_question_plan_in_display_prompt_and_payload(self):
        from grounded_stub import stub_generation

        stub_generation(self)
        settings = get_settings()
        records = [
            AnswerRecord(
                id="chunk-docling-c000001",
                engine="docling",
                engine_label="Docling",
                page=1,
                seq_no=1,
                category="ChildChunk",
                text="再発行できない条件は設定資料に記載されています。",
                chunk_id="chunk-docling-c000001",
                chunk_level="child",
                chunk_seq=1,
                metadata={"active": True},
            )
        ]
        context = AnswerContext(records=records, text="[chunk] 再発行できない条件")

        with (
            patch(
                "rag_engine.generation.answering.load_latest_or_source_chunk_run",
                return_value=SimpleNamespace(chunk_run_id="abcdef-1234567890ab"),
            ),
            patch("rag_engine.generation.answering.check_adb_hybrid_search_ready"),
            patch("rag_engine.generation.answering.build_adb_hybrid_answer_context", return_value=context),
            patch(
                "rag_engine.generation.answering.parse_text_response",
                return_value=AnswerOutput(
                    answer="設定資料を確認してください。",
                    confidence="high",
                    question_type=[],
                    cited_evidence=[],
                    reasoning_summary="",
                    insufficient_reason="",
                    needs_human_review=False,
                ),
            ) as parse,
        ):
            result = answer_question_result(
                "再発行できない条件と必要な設定を教えてください。",
                "abcdef",
                ["docling"],
                settings,
                query_strategy=SIMPLE_RETRIEVAL_LABEL,
                answer_flow=STANDARD_ANSWER_FLOW_LABEL,
            )

        prompt = parse.call_args.args[1]
        payload = answer_result_payload(result, answer_id="answer123", run_id="abcdef")
        display = result.question_display
        self.assertIn(QUESTION_DISPLAY_METADATA_SEPARATOR, display)
        # 工程ごとに、目的・結果と、後続を変える場合だけ影響を示す。表示専用だった方針ラベルは出さない。
        self.assertIn("\n1. 質問の理解\n", display)
        self.assertIn("目的: 質問の文から、文書名・画面名・エラーコード・日付などの条件を規則で読み取ります（LLM は使いません）", display)
        self.assertIn("結果: 答える要求は ", display)
        self.assertNotIn("質問の目的", display)
        for removed in ("回答方針を決定", "判定方法", "方針の用途", "rule_lookup", " 開始", " 終了"):
            self.assertNotIn(removed, display)
        self.assertNotIn(QUESTION_PLAN_SEPARATOR, display)
        self.assertNotIn("[Question Plan]", prompt)
        self.assertEqual(payload["question_plan"], {"original_question": result.question_plan.original_question})

    def test_extract_original_question_strips_question_plan_metadata(self):
        displayed = "\n".join(["元の質問", "", QUESTION_PLAN_SEPARATOR, "実行処理: 画面・画像確認"])

        self.assertEqual(extract_original_question(displayed), "元の質問")


if __name__ == "__main__":
    unittest.main()
