"""登録語の補完制御、実作用 trace、対照評価の回帰テスト。"""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rag_engine.config import get_settings
from rag_engine.evaluation.domain_keyword_eval import run_keyword_comparison, score_context, summarize_pairs
from rag_engine.evaluation.rag_eval import RagEvalCase
from rag_engine.generation.answering import (
    AnswerContext,
    build_adb_hybrid_answer_context,
    build_question_text_search_info,
)
from rag_engine.knowledge.domain_keywords import save_domain_keywords
from rag_engine.retrieval.text_search_tokenizer import TextSearchTokenizerConfig, tokenize_text_search_query_with_trace


class DomainKeywordImpactTests(unittest.TestCase):
    def settings(self, **kwargs):
        return replace(get_settings(), **kwargs)

    def test_registered_keywords_are_never_added_by_partial_match(self):
        # 質問の語と部分的に重なるだけの登録キーワードは検索文に加えない。この機能と設定は廃止した (#557)。
        info = build_question_text_search_info("登録語の前半", self.settings(domain_keywords_override=("登録語の前半と後半",)))
        self.assertNotIn("登録語の前半と後半", str(info.tokenization_traces))
        settings = get_settings(environ={"DOMAIN_KEYWORD_EXPANSION_ENABLED": "true"}, dotenv_path=None)
        self.assertFalse(hasattr(settings, "domain_keyword_expansion_enabled"))

    def test_matched_keywords_report_retained_and_truncated(self):
        result = tokenize_text_search_query_with_trace(
            "契約区分 出庫伝票",
            domain_keywords=["契約区分", "出庫伝票", "未出現"],
            config=TextSearchTokenizerConfig(),
            max_tokens=1,
        )
        self.assertEqual(result.matched_domain_keywords, ("出庫伝票", "契約区分"))
        self.assertEqual(result.selected_domain_keywords, ("出庫伝票",))
        self.assertEqual(result.truncated_domain_keywords, ("契約区分",))

    def test_empty_override_never_reads_live_dictionary(self):
        with patch("rag_engine.generation.answering.load_domain_keywords", side_effect=AssertionError("live read")):
            info = build_question_text_search_info("出庫伝票", self.settings(domain_keywords_override=()))
        self.assertFalse(info.error)
        self.assertEqual(info.tokenization_traces[0]["matched_domain_keywords"], [])

    def test_query_trace_records_actual_expression_and_protected_omissions(self):
        question = " ".join(f"ID-{index:03}" for index in range(30))
        info = build_question_text_search_info(question, self.settings(domain_keywords_override=()))
        trace = info.tokenization_traces[0]
        self.assertEqual(trace["oracle_text_query"], info.oracle_text_query)
        self.assertEqual(trace["oracle_text_query_chars"], len(info.oracle_text_query))
        self.assertTrue(trace["omitted_protected_tokens"])
        self.assertEqual(trace["tokens"], list(info.tokens))
        self.assertLessEqual(len(info.tokens), 24)

    def test_metrics_use_actual_anchor_rank_and_ignore_neighbors(self):
        children = [
            SimpleNamespace(role="retrieved_anchor", retrieval_rank=3, record=SimpleNamespace(chunk_id="hit")),
            SimpleNamespace(role="neighbor", retrieval_rank=None, record=SimpleNamespace(chunk_id="neighbor")),
            SimpleNamespace(role="retrieved_anchor", retrieval_rank=1, record=SimpleNamespace(chunk_id="miss")),
        ]
        context = AnswerContext(records=[], text="", evidence_tree=(SimpleNamespace(children=children),))
        case = RagEvalCase("case", "question", expected_child_ids=("hit", "neighbor"))
        scores = score_context(case, context, 3)
        self.assertEqual(scores["recall_at_k"], 0.5)
        self.assertEqual(scores["reciprocal_rank"], 1 / 3)
        self.assertEqual(score_context(case, context, 2)["recall_at_k"], 0)
        self.assertIsNone(score_context(RagEvalCase("x", "q"), context, 3)["recall_at_k"])

    def test_unjudged_is_not_zero_and_regressions_are_reported(self):
        results = [
            {
                "case_id": "x",
                "without_keywords": {"recall_at_k": 1, "answer_correct": True},
                "with_keywords": {"recall_at_k": 0, "answer_correct": None},
            }
        ]
        summary = summarize_pairs(results)
        self.assertEqual(summary["recall_at_k"]["regressions"], ["x"])
        self.assertIsNone(summary["answer_correct"]["delta"])
        results[0]["with_keywords"]["answer_correct"] = False
        self.assertEqual(summarize_pairs(results)["answer_correct"]["delta"], -1)
        results[0]["with_keywords"]["answer_correct"] = "false"
        with self.assertRaises(ValueError):
            summarize_pairs(results)

    def test_comparison_pins_dictionary_and_run_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            save_domain_keywords(root, ["出庫伝票番号"])
            before = (root / "domain_keywords.json").read_bytes()
            dataset = root / "cases.json"
            dataset.write_text(json.dumps({"cases": [{"case_id": "x", "question": "出庫伝票", "reference_answer": "参照"}]}))
            observed = []

            def retrieve(question, run, engines, settings, **kwargs):
                observed.append((settings.domain_keywords_override, kwargs["pinned_chunk_run_id"]))
                return AnswerContext(records=[], text="", status="insufficient")

            with (
                patch("rag_engine.evaluation.domain_keyword_eval.get_settings", return_value=self.settings(output_dir=root)),
                patch(
                    "rag_engine.evaluation.domain_keyword_eval.load_latest_or_source_chunk_run",
                    return_value=SimpleNamespace(chunk_run_id="abcdef"),
                ),
                patch("rag_engine.evaluation.domain_keyword_eval.build_adb_hybrid_answer_context", side_effect=retrieve),
            ):
                report = run_keyword_comparison(dataset, "source")
            json.dumps(report, ensure_ascii=False)
            self.assertEqual(observed, [((), "abcdef"), (("出庫伝票番号",), "abcdef")])
            self.assertEqual(report["results"][0]["reference_answer"], "参照")
            self.assertEqual((root / "domain_keywords.json").read_bytes(), before)
            self.assertIsNone(report["summary"]["answer_correct"]["with_keywords"])

    def test_pinned_context_never_resolves_latest(self):
        from rag_engine.adapters.oracle.store import AdbHybridSearchUnavailable

        with (
            patch("rag_engine.generation.answering.load_chunk_run_by_id", return_value=None) as direct,
            patch("rag_engine.generation.answering.load_latest_or_source_chunk_run", side_effect=AssertionError("latest read")),
            self.assertRaises(AdbHybridSearchUnavailable),
        ):
            build_adb_hybrid_answer_context("q", "source", ("docling",), self.settings(), pinned_chunk_run_id="abcdef")
        direct.assert_called_once()
