"""検索の起点数・context の parent 数・生成の原文予算を環境変数で変えられる (#1054)。既定値は据え置き。"""

import unittest
from types import SimpleNamespace

from rag_engine.chunking.constants import DEFAULT_RETRIEVAL_TOP_K
from rag_engine.config import DEFAULT_EVIDENCE_BUDGET_CHARS, DEFAULT_MAX_CONTEXT_RECORDS, DEFAULT_RETRIEVAL_TOP_K_SETTING, get_settings
from rag_engine.generation import answering
from rag_engine.generation.answer_records import MAX_CONTEXT_RECORDS
from rag_engine.retrieval.evidence_selection import EVIDENCE_BUDGET_CHARS


class RetrievalBudgetSettingsTest(unittest.TestCase):
    def test_defaults_match_the_code_constants(self):
        settings = get_settings(environ={}, dotenv_path=None)
        self.assertEqual(
            (settings.retrieval_top_k, settings.max_context_records, settings.evidence_budget_chars),
            (DEFAULT_RETRIEVAL_TOP_K, MAX_CONTEXT_RECORDS, EVIDENCE_BUDGET_CHARS),
        )
        # config.py は循環 import を避けて値を写しているので、元の常量とのずれをここで止める
        self.assertEqual(
            (DEFAULT_RETRIEVAL_TOP_K_SETTING, DEFAULT_MAX_CONTEXT_RECORDS, DEFAULT_EVIDENCE_BUDGET_CHARS),
            (DEFAULT_RETRIEVAL_TOP_K, MAX_CONTEXT_RECORDS, EVIDENCE_BUDGET_CHARS),
        )

    def test_environment_overrides_each_value(self):
        settings = get_settings(
            environ={
                "RAG_ENGINE_RETRIEVAL_TOP_K": "30",
                "RAG_ENGINE_MAX_CONTEXT_RECORDS": "16",
                "RAG_ENGINE_EVIDENCE_BUDGET_CHARS": "72000",
            },
            dotenv_path=None,
        )
        self.assertEqual((settings.retrieval_top_k, settings.max_context_records, settings.evidence_budget_chars), (30, 16, 72000))
        self.assertEqual(get_settings(environ={"RAG_ENGINE_MAX_CONTEXT_RECORDS": "0"}, dotenv_path=None).max_context_records, 1)

    def test_context_record_limit_falls_back_without_settings(self):
        self.assertEqual(answering._max_context_records(None), MAX_CONTEXT_RECORDS)
        self.assertEqual(answering._max_context_records(SimpleNamespace(max_context_records=16)), 16)

    def test_grounded_spans_pass_the_budget_to_evidence_selection(self):
        seen = {}

        def fake_spans(question, records, **kwargs):
            seen.update(kwargs)
            return []

        original = answering.evidence_spans
        answering.evidence_spans = fake_spans
        try:
            context = SimpleNamespace(records=[], evidence_tree=[], preferred_child_ids=())
            answering._grounded_spans("質問", context, [], None, budget=72000)
            self.assertEqual(seen.get("budget"), 72000)
            seen.clear()
            answering._grounded_spans("質問", context, [], None)
            self.assertNotIn("budget", seen)
        finally:
            answering.evidence_spans = original


class EvidenceBudgetTraceTest(unittest.TestCase):
    """生成に渡した根拠の文字予算を診断に残す (#3776)。設定値・実際に使った値・送った文字数を分ける。"""

    def _trace(self, *, evidence_budget_chars=None, **overrides):
        from dataclasses import replace

        from test_grounded_answer import LIST_TEXT, FakeModel, audit, context, draft, ids, item, record

        from rag_engine.dependencies import AnswerDependencies, bind_dependencies
        from rag_engine.generation.answering import synthesize_grounded_answer

        def unused(*a, **k):
            return (_ for _ in ()).throw(AssertionError("unexpected I/O"))

        ctx = context(record(0, LIST_TEXT, "機能A"))
        eid = ids(ctx)[LIST_TEXT]
        text = "一覧表を選択し、処理を選択します。"
        model = FakeModel([draft(item(eid, text, text))], [audit((0, "supported", "matched", ""))])
        settings = replace(get_settings(environ={}, dotenv_path=None), **overrides)
        with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
            response = synthesize_grounded_answer(
                "一覧表の処理は？", ctx, settings, image_prompt_mode="text_only", evidence_budget_chars=evidence_budget_chars
            ).response
        return response.generation_trace["evidence_budget"]

    def test_configured_budget_and_sent_chars_are_recorded(self):
        trace = self._trace()
        self.assertEqual(trace["configured_chars"], EVIDENCE_BUDGET_CHARS)
        self.assertEqual(trace["effective_chars"], EVIDENCE_BUDGET_CHARS)
        self.assertEqual(trace["source"], "configured")
        self.assertFalse(trace["effective_exceeds_configured"])
        self.assertGreaterEqual(trace["span_count"], 1)
        # 根拠ブロックは本文に出典情報・書式を足したもの。token の見積もりは文字数を超えない。
        self.assertGreater(trace["evidence_text_chars"], 0)
        self.assertGreater(trace["evidence_block_chars"], trace["evidence_text_chars"])
        self.assertLessEqual(trace["evidence_block_tokens_estimate"], trace["evidence_block_chars"])
        # 数値と区分だけを残す（本文・プロンプトを診断に入れない）。
        self.assertTrue(all(isinstance(value, (int, bool, str)) for value in trace.values()))

    def test_section_expansion_budget_is_distinguished_from_the_setting(self):
        trace = self._trace(evidence_budget_chars=60000)
        self.assertEqual((trace["configured_chars"], trace["effective_chars"]), (EVIDENCE_BUDGET_CHARS, 60000))
        self.assertEqual(trace["source"], "section_expansion")
        self.assertFalse(trace["effective_exceeds_configured"])

    def test_configured_value_from_settings_is_used(self):
        from dataclasses import replace

        settings = replace(get_settings(environ={}, dotenv_path=None), evidence_budget_chars=72000)
        trace = answering._evidence_budget_trace(settings, None, [])
        self.assertEqual((trace["configured_chars"], trace["effective_chars"], trace["span_count"]), (72000, 72000, 0))
        self.assertEqual(trace["evidence_block_chars"], 0)
