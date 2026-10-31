"""機能内の画面根拠と、部分拒否時に残す独立した説明を検証する。"""

import unittest

from grounded_stub import AnswerOutput

from rag_engine.generation.operation_audit import answer_passages


def make_output(text):
    return AnswerOutput(
        answer=text,
        confidence="high",
        question_type=["操作手順"],
        cited_evidence=[],
        reasoning_summary="",
        insufficient_reason="",
        needs_human_review=False,
    )


def make_audit(text, rejected=()):
    return dict(
        complete=False,
        goal_alignment="partial",
        reviews=[
            dict(
                passage_id=p["id"],
                answer_quote=p["text"],
                status="unsupported" if any(word in p["text"] for word in rejected) else "supported",
                applicability="matched",
                evidence_ids=["E1"],
                reason="原文照合",
            )
            for p in answer_passages(text)
        ],
    )


class SupportedExplanationsTests(unittest.TestCase):
    """業務名や帳票名に依存しない根拠分割・最終化の回帰ケース。"""

    def span(self, **overrides):
        return dict(
            dict(
                evidence_id="E1",
                source_id="v1:parent1",
                source="manual.pdf",
                page=2,
                section_path=["共通－（1）警告一覧画面", "A【画面説明】"],
                text="期限超過の場合、警告一覧ボタンを押すと一覧を確認できます。",
            ),
            **overrides,
        )

    def test_documented_print_instruction_is_preserved(self):
        from rag_engine.generation.operation_audit import missing_operation_terms

        self.assertEqual(missing_operation_terms("印刷してください。", [dict(text="印字ボタンを押します。")]), [])
        self.assertEqual(missing_operation_terms("期限超過は更新期限を過ぎた状態です。", [dict(text="期限の説明")]), [])


if __name__ == "__main__":
    unittest.main()
