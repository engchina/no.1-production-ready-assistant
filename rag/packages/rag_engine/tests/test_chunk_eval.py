"""本文監査とチャンク比較の根拠指標を検証する。"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from rag_engine.adapters.oci import RerankTextRank
from rag_engine.config import get_settings
from rag_engine.evaluation.chunk_eval import audit_variants, evaluate_cohere_variants, evidence_metrics, load_token_counter, main


def payload():
    return {
        "run_id": "abcdef",
        "pdf_name": "manual.pdf",
        "records": [
            {
                "id": f"r{i}",
                "engine": "docling",
                "page": 1,
                "seq_no": i,
                "category": "Text",
                "text": "手順の説明。" * 100 + f"例外条件{i}",
                "bbox": [0, 0, 100, 100],
            }
            for i in range(1, 4)
        ],
    }


class ChunkEvalTests(TestCase):
    def test_audit_keeps_defaults_and_reports_missing_tokenizer_honestly(self):
        report, variants = audit_variants([payload()], engines=["docling"])
        self.assertEqual(list(variants), [700, 900, 1200])
        for row in report["variants"].values():
            self.assertEqual(row["body_truncated_count"], 0)
            self.assertEqual(row["config"]["parent_target_chars"], 6000)
            self.assertEqual(row["config"]["parent_max_pages"], 3)
            self.assertEqual(row["config"]["parent_max_children"], 12)
            self.assertIsNone(row["embed_text_tokens"])

    def test_evidence_metrics_require_source_and_page_and_count_unique_evidence(self):
        _, variants = audit_variants([payload()], engines=["docling"], sizes=[700])
        children = variants[700]
        expected = [{"run_id": "abcdef", "page": 1, "text": "例外条件1"}, {"run_id": "abcdef", "page": 1, "text": "例外条件3"}]
        result = evidence_metrics([children[1], children[0], children[0]], expected)
        self.assertEqual(result, {"evidence_recall": 0.5, "reciprocal_rank": 0.5})
        self.assertEqual(evidence_metrics(children, [{**expected[0], "run_id": "other"}])["evidence_recall"], 0)
        self.assertEqual(evidence_metrics(children, [{**expected[0], "page": 2}])["evidence_recall"], 0)
        self.assertIsNone(evidence_metrics(children, [])["evidence_recall"])

    def test_live_metrics_measure_actual_rerank_order_and_do_not_claim_answer_accuracy(self):
        _, variants = audit_variants([payload()], engines=["docling"], sizes=[700])
        cases = [{"case_id": "tail", "question": "条件3は？", "expected_evidence": [{"run_id": "abcdef", "page": 1, "text": "例外条件3"}]}]
        with (
            patch("rag_engine.evaluation.chunk_eval.embed_query", return_value=[1.0, 0.0]),
            patch("rag_engine.evaluation.chunk_eval.embed_texts", return_value=[[1, 0], [0.9, 0.1], [0, 1]]) as embed,
            patch("rag_engine.evaluation.chunk_eval.rerank_text_with_scores", return_value=[RerankTextRank(2, 0.9)]),
        ):
            # 本番の保存と同じく、retrieval_text が空の chunk は本文を embedding 入力にする。
            variants[700][0].retrieval_text = ""
            report = evaluate_cohere_variants(variants, cases, get_settings(), candidate_k=3, top_k=1)
        self.assertEqual(embed.call_args.args[0][0], variants[700][0].text)
        self.assertTrue(all(embed.call_args.args[0]))
        row = report["variants"]["700"]
        self.assertEqual(row["cases"][0]["vector_top_k"]["evidence_recall"], 0)
        self.assertEqual(row["rerank_recall_at_k"], 1)
        self.assertEqual(row["rerank_mrr_at_k"], 1)
        self.assertIsNone(row["answer_accuracy"])

    def test_unannotated_cases_fail_before_network_calls(self):
        _, variants = audit_variants([payload()], engines=["docling"], sizes=[700])
        with (
            patch("rag_engine.evaluation.chunk_eval.embed_query") as query,
            self.assertRaisesRegex(ValueError, "reviewed expected_evidence"),
        ):
            evaluate_cohere_variants(variants, [{"case_id": "x", "question": "q"}], get_settings())
        query.assert_not_called()

    def test_local_token_counter_disables_tokenizer_truncation_and_padding(self):
        try:
            from tokenizers import Tokenizer, models, pre_tokenizers
        except ImportError:
            self.skipTest("tokenizers optional dependency is not installed")
        tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0, "word": 1}, unk_token="[UNK]"))
        tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
        tokenizer.enable_truncation(max_length=2)
        tokenizer.enable_padding(length=10)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "tokenizer.json"
            tokenizer.save(str(path))
            counter, fingerprint = load_token_counter(path)
            self.assertEqual(counter("word word word"), 3)
            self.assertEqual(len(fingerprint), 64)

    def test_cli_leaves_active_chunks_unchanged(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "abcdef"
            source.mkdir()
            (source / "viewer-data.json").write_text(json.dumps(payload()))
            report_path = root / "report.json"
            with patch("rag_engine.evaluation.chunk_eval.embed_texts") as embed:
                self.assertEqual(main(["--output-dir", tmp, "--run-id", "abcdef", "--json-out", str(report_path)]), 0)
            embed.assert_not_called()
            self.assertFalse((source / "chunks").exists())
            self.assertEqual(json.loads(report_path.read_text())["evaluation"]["mode"], "not_run")
