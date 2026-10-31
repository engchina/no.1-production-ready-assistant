"""質問の理解が検索文を作らないことの回帰テスト（#3496）。

質問の理解（`inquiry_conditions`）は条件を読み取り、検索の絞り込み・候補の加点・回答に渡す条件にだけ使う。
検索文を足すのは質問の拡張などの別の機能で、それらの検索文は検索と rerank に引き続き届く。
外部のサービスは呼ばず、検索・rerank・LLM は決定論のスタブにする。
"""

import unittest
from unittest.mock import patch

from grounded_stub import stub_generation
from test_answer_generation import (
    _answer_output,
    _chunk_run_stub,
    _hybrid_result,
    _rerank_ready_settings,
    _stored_child,
)

from rag_engine.generation.answer_payload import answer_result_payload
from rag_engine.generation.answering import (
    RAG_FUSION_LABEL,
    SIMPLE_RETRIEVAL_LABEL,
    STANDARD_ANSWER_FLOW_LABEL,
    answer_question_result,
)
from rag_engine.models.llm import QueryExpansionOutput, RerankTextRank
from rag_engine.retrieval.inquiry_conditions import parse_inquiry_conditions

# 業務の内容を含まない合成の質問。ファイル名・ページ・エラーコード・列・2 つの要求を含む。
QUESTION = "sample_guide.pdf の 2ページ目にある E1234 のとき、AB列は確認が必要ですか。また再実行の手順も教えてください。"
GENERATED_QUERY = "言い換えた検索文"


def _rerank_stub(calls: list[str]):
    def rerank(query, documents, settings, top_n=None):
        calls.append(query)
        return [RerankTextRank(index, 1.0 - index * 0.1) for index in range(len(documents))]

    return rerank


class InquiryQueryRemovalTests(unittest.TestCase):
    def setUp(self):
        stub_generation(self)

    def run_answer(self, *, query_strategy, parse_side_effect):
        search_result = _hybrid_result(
            _stored_child("chunk-a", 1, "合成の根拠 A"),
            _stored_child("chunk-b", 2, "合成の根拠 B"),
        )
        rerank_calls: list[str] = []
        with (
            patch("rag_engine.generation.answering.load_latest_or_source_chunk_run", return_value=_chunk_run_stub()),
            patch("rag_engine.generation.answering.check_adb_hybrid_search_ready"),
            patch("rag_engine.generation.answering.search_adb_hybrid_chunks", return_value=search_result) as search,
            patch("rag_engine.generation.answering.rerank_text_with_scores", side_effect=_rerank_stub(rerank_calls)),
            patch("rag_engine.generation.answering.parse_text_response", side_effect=parse_side_effect),
        ):
            result = answer_question_result(
                QUESTION,
                "abcdef",
                ["docling"],
                _rerank_ready_settings(),
                query_strategy=query_strategy,
                rerank_enabled=True,
                answer_flow=STANDARD_ANSWER_FLOW_LABEL,
            )
        return result, search, rerank_calls

    def test_conditions_are_read_but_no_auxiliary_query_is_generated(self):
        parsed = parse_inquiry_conditions(QUESTION)
        # 前提: 条件は読み取れている（以前はこの語から補助の検索文を作っていた）。
        self.assertTrue(parsed.has_signals)
        self.assertTrue(parsed.search_terms)
        self.assertIn("E1234", parsed.codes_and_errors)
        self.assertIn("AB列", parsed.column_terms)
        self.assertEqual(parsed.metadata_filter.source_file_terms, ("sample_guide.pdf",))
        self.assertEqual(parsed.metadata_filter.page_numbers, (2,))
        self.assertFalse(hasattr(parsed, "retrieval_queries"))
        payload = parsed.to_payload()
        self.assertNotIn("retrieval_queries", payload)
        self.assertEqual(payload["schema_version"], 2)
        self.assertIn("search_terms", payload)

    def test_without_other_expansions_only_the_original_question_is_searched_and_reranked(self):
        result, search, rerank_calls = self.run_answer(
            query_strategy=SIMPLE_RETRIEVAL_LABEL,
            parse_side_effect=[_answer_output(answer="合成の回答", cited_evidence_id="")],
        )

        # embed・Oracle Text・融合は検索の実装（search_adb_hybrid_chunks）が受け取った検索文だけを使う。
        kwargs = search.call_args.kwargs
        self.assertEqual(kwargs["retrieval_queries"], (QUESTION,))
        plan = result.retrieval_query_plan
        self.assertEqual(plan["vector_queries"], [QUESTION])
        self.assertEqual(plan["text_query_variants"], [QUESTION])
        self.assertEqual(len(plan["oracle_text_queries"]), 1)
        self.assertEqual(plan["lexical_queries"], [])
        self.assertNotIn("inquiry_queries", plan)
        # 追加の rerank は無く、原質問だけで並べ替える。
        self.assertEqual(rerank_calls, [QUESTION])

        # 条件・絞り込み・要求の分割は残る。
        conditions = kwargs["inquiry_conditions"]
        self.assertIn("E1234", conditions.codes_and_errors)
        self.assertIn("AB列", conditions.column_terms)
        self.assertEqual(plan["metadata_filter"]["inquiry"]["source_file_terms"], ["sample_guide.pdf"])
        self.assertEqual(plan["metadata_filter"]["inquiry"]["page_numbers"], [2])
        display = result.question_display
        self.assertIn("答える要求は 2 件", display)
        self.assertIn("読み取ったコード・エラー: E1234", display)
        self.assertIn("1. [原質問] ", display)
        self.assertNotIn("補助の検索文", display)
        self.assertNotIn("[質問理解]", display)

        payload = answer_result_payload(result, answer_id="a1", run_id="abcdef")
        self.assertEqual(payload["text_search_query_source"], "原質問")
        self.assertNotIn("retrieval_queries", payload["inquiry_conditions"])
        self.assertIn("E1234", payload["inquiry_conditions"]["codes_and_errors"])

    def test_rewritten_queries_still_reach_search_and_rerank(self):
        result, search, rerank_calls = self.run_answer(
            query_strategy=RAG_FUSION_LABEL,
            parse_side_effect=[
                QueryExpansionOutput(queries=[GENERATED_QUERY]),
                _answer_output(answer="合成の回答", cited_evidence_id=""),
            ],
        )

        self.assertEqual(search.call_args.kwargs["retrieval_queries"], (QUESTION, GENERATED_QUERY))
        self.assertEqual(result.retrieval_query_plan["vector_queries"], [QUESTION, GENERATED_QUERY])
        self.assertEqual(result.retrieval_query_plan["llm_expansion_queries"], [GENERATED_QUERY])
        # 言い換えた検索文でも関連度を測る（追加の rerank は検索に使った検索文だけ）。
        self.assertEqual(rerank_calls, [QUESTION, GENERATED_QUERY])
        self.assertIn(f"2. [質問拡張] {GENERATED_QUERY}", result.question_display)
        self.assertNotIn("[質問理解]", result.question_display)


if __name__ == "__main__":
    unittest.main()
