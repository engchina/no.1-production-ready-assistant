"""回答の LLM の時間の短縮 (#1499) の検証。

用途ごとの推論の量（質問拡張戦略 / 根拠の評価 / 生成・監査）、規則の信号が強いときの CRAG の評価の省略、質問拡張の検索文の上限、
質問拡張の LLM と元の質問の検索の並行を、外部の I/O を置き換えて確かめる。
"""

import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

from test_answer_generation import _answer_context, _chunk_run_stub, _crag_grade, _record

import rag_engine.generation.answering as generation
from rag_engine.config import (
    ENTERPRISE_AI_LLM_PROVIDER,
    ENTERPRISE_AI_VISION_LLM_PROVIDER,
    LLM_PURPOSE_GENERATION,
    LLM_PURPOSE_JUDGE,
    LLM_PURPOSE_STRATEGY,
    get_settings,
    llm_reasoning_effort_note,
    settings_for_llm_purpose,
)
from rag_engine.dependencies import AnswerDependencies, bind_dependencies
from rag_engine.models.llm import QueryExpansionOutput, QueryRoutingOutput


def _settings(*, answer_effort="medium", vision_effort="medium", **overrides):
    """外部の .env を読まない設定。モデルの登録値（送る値）は provider の reasoning_effort。"""
    base = get_settings(
        environ={
            "OCI_ENTERPRISE_AI_ENDPOINT": "https://example.invalid/openai/v1",
            "OCI_ENTERPRISE_AI_API_KEY": "test-key",
            "OCI_ENTERPRISE_AI_DEFAULT_MODEL": "test.model",
            "OCI_ENTERPRISE_AI_VLM_MODEL": "test.vision",
            "OCI_ENTERPRISE_AI_REASONING_EFFORT": answer_effort,
            "OCI_ENTERPRISE_AI_VLM_REASONING_EFFORT": vision_effort,
        },
        dotenv_path=None,
    )
    return replace(base, **overrides)


def _efforts(settings):
    return {key: provider.reasoning_effort for key, provider in settings.llm_providers.items()}


class PurposeReasoningEffortTests(unittest.TestCase):
    """用途ごとの推論の量の解釈（none は送らない・登録値が none のモデルには送らない）。"""

    def test_env_values_are_normalized(self):
        settings = get_settings(
            environ={
                "RAG_ENGINE_STRATEGY_REASONING_EFFORT": "Medium",
                "RAG_ENGINE_JUDGE_REASONING_EFFORT": " LOW ",
                "RAG_ENGINE_GENERATION_REASONING_EFFORT": "model",
                "RAG_ENGINE_CRAG_SKIP_ON_STRONG_SIGNALS": "1",
                "RAG_ENGINE_CRAG_SKIP_MIN_RERANK_SCORE": "1.7",
                "RAG_ENGINE_QUERY_EXPANSION_MAX_QUERIES": "3",
            },
            dotenv_path=None,
        )
        self.assertEqual(settings.judge_reasoning_effort, "low")
        self.assertEqual(settings.strategy_reasoning_effort, "medium")
        # 選択肢に無い値（model など）はモデルの登録値のまま。
        self.assertEqual(settings.generation_reasoning_effort, "")
        self.assertTrue(settings.crag_skip_on_strong_signals)
        self.assertEqual(settings.crag_skip_min_rerank_score, 1.0)
        self.assertEqual(settings.query_expansion_max_queries, 3)
        defaults = get_settings(environ={}, dotenv_path=None)
        self.assertEqual(
            (defaults.strategy_reasoning_effort, defaults.judge_reasoning_effort, defaults.generation_reasoning_effort), ("", "", "")
        )
        self.assertFalse(defaults.crag_skip_on_strong_signals)
        self.assertEqual(defaults.query_expansion_max_queries, generation.MAX_GENERATED_QUERY_COUNT)

    def test_override_replaces_only_sent_efforts(self):
        settings = _settings(vision_effort="", judge_reasoning_effort="low")
        judged = settings_for_llm_purpose(settings, LLM_PURPOSE_JUDGE)
        # 登録値が送らない（none）の Vision モデルには送らない。
        self.assertEqual(_efforts(judged), {ENTERPRISE_AI_LLM_PROVIDER: "low", ENTERPRISE_AI_VISION_LLM_PROVIDER: ""})
        # 元の設定は変えない。上書きの無い用途は同じ設定を返す。
        self.assertEqual(_efforts(settings)[ENTERPRISE_AI_LLM_PROVIDER], "medium")
        self.assertIs(settings_for_llm_purpose(settings, LLM_PURPOSE_GENERATION), settings)
        self.assertIs(settings_for_llm_purpose(settings, LLM_PURPOSE_STRATEGY), settings)
        omitted = settings_for_llm_purpose(replace(settings, generation_reasoning_effort="none"), LLM_PURPOSE_GENERATION)
        self.assertEqual(_efforts(omitted)[ENTERPRISE_AI_LLM_PROVIDER], "")
        # 設定でない stub はそのまま返す（差し替え実装のテストを壊さない）。
        stub = object()
        self.assertIs(settings_for_llm_purpose(stub, LLM_PURPOSE_JUDGE), stub)  # type: ignore[arg-type]

    def test_note_shows_sent_effort_and_its_source(self):
        settings = _settings(judge_reasoning_effort="low")
        self.assertEqual(
            llm_reasoning_effort_note(settings, LLM_PURPOSE_JUDGE), "推論の量: low（根拠の評価の設定。モデルの登録値は medium）"
        )
        self.assertEqual(llm_reasoning_effort_note(settings, LLM_PURPOSE_STRATEGY), "推論の量: medium（モデルの登録値）")
        self.assertEqual(
            llm_reasoning_effort_note(replace(settings, strategy_reasoning_effort="high"), LLM_PURPOSE_STRATEGY),
            "推論の量: high（質問拡張戦略の判定・検索文の生成の設定。モデルの登録値は medium）",
        )
        self.assertEqual(llm_reasoning_effort_note(settings, LLM_PURPOSE_GENERATION), "推論の量: medium（モデルの登録値）")
        self.assertEqual(
            llm_reasoning_effort_note(replace(settings, generation_reasoning_effort="none"), LLM_PURPOSE_GENERATION),
            "推論の量: 送らない（回答文の生成の設定）",
        )
        self.assertEqual(
            llm_reasoning_effort_note(_settings(answer_effort="", judge_reasoning_effort="high"), LLM_PURPOSE_JUDGE),
            "推論の量: 送らない（モデルの登録値が none）",
        )


class PurposeEffortCallSiteTests(unittest.TestCase):
    """各呼び出しが用途の推論の量で LLM を呼ぶ。"""

    def setUp(self):
        self.settings = _settings(strategy_reasoning_effort="none", judge_reasoning_effort="low", generation_reasoning_effort="high")
        self.calls = []

    def record(self, reply):
        def parse(system, prompt, settings, schema, **options):
            self.calls.append((schema, settings.llm_providers[ENTERPRISE_AI_LLM_PROVIDER].reasoning_effort))
            return reply(schema) if callable(reply) else reply

        return parse

    def test_routing_and_query_generation_use_strategy_effort(self):
        def reply(schema):
            if schema is QueryRoutingOutput:
                return QueryRoutingOutput(strategy="rag_fusion", reason="複数の言い回し", queries=[])
            return QueryExpansionOutput(queries=["経費の申請", "経費精算の手順"])

        with patch.object(generation, "parse_text_response", side_effect=self.record(reply)):
            expansion = generation.build_query_expansion("経費精算の申請方法は？", generation.AUTO_ROUTING_STRATEGY, self.settings)
        # 戦略の判定と検索文の生成は根拠の評価（low）と別の設定（ここでは none = 送らない）で呼ぶ。
        self.assertEqual(self.calls, [(QueryRoutingOutput, ""), (QueryExpansionOutput, "")])
        defaults = replace(self.settings, strategy_reasoning_effort="")
        self.calls.clear()
        with patch.object(generation, "parse_text_response", side_effect=self.record(reply)):
            generation.build_query_expansion("経費精算の申請方法は？", generation.AUTO_ROUTING_STRATEGY, defaults)
        # 既定（空）はモデルの登録値のまま（#1499 の A/B で low は戦略の選択がぶれた）。
        self.assertEqual(self.calls, [(QueryRoutingOutput, "medium"), (QueryExpansionOutput, "medium")])
        self.assertEqual(expansion.generated_queries, ("経費の申請", "経費精算の手順"))

    def test_crag_grade_uses_judge_effort(self):
        context = _answer_context([_record("r1", "docling", 1, 1, "経費精算の申請方法")])
        with patch.object(
            generation,
            "parse_text_response",
            side_effect=self.record(_crag_grade(sufficient=True, relevant_chunk_ids=["r1"], support_text=context.records[0].text)),
        ):
            generation._grade_crag_retrieval(1, "経費精算の申請方法は？", "経費精算の申請方法は？", ("経費精算",), context, self.settings)
        self.assertEqual([effort for _, effort in self.calls], ["low"])

    def test_generation_and_audit_use_generation_effort(self):
        from rag_engine.generation import grounded

        seen = []

        def fake_round(question, spans, settings, **options):
            seen.append(settings.llm_providers[ENTERPRISE_AI_LLM_PROVIDER].reasoning_effort)
            raise RuntimeError("stop after the first round")

        context = _answer_context([_record("r1", "docling", 1, 1, "経費精算の申請方法")])
        with patch.object(grounded, "run_round", side_effect=fake_round), self.assertRaises(RuntimeError):
            generation.synthesize_grounded_answer("経費精算の申請方法は？", context, self.settings, image_prompt_mode="text_only")
        self.assertEqual(seen, ["high"])

    def test_query_expansion_max_queries_caps_generated_queries(self):
        output = QueryExpansionOutput(queries=[f"経費精算の検索文{n}" for n in range(1, 6)])
        settings = replace(self.settings, query_expansion_max_queries=2)
        with patch.object(generation, "parse_text_response", side_effect=self.record(output)):
            capped = generation.build_query_expansion("経費精算の申請方法は？", generation.RAG_FUSION_STRATEGY, settings)
            default = generation.build_query_expansion("経費精算の申請方法は？", generation.RAG_FUSION_STRATEGY, self.settings)
        self.assertEqual(capped.generated_queries, ("経費精算の検索文1", "経費精算の検索文2"))
        self.assertEqual(len(default.generated_queries), generation.MAX_GENERATED_QUERY_COUNT)


QUESTION = "経費精算の申請方法を教えてください"
EVIDENCE = "経費精算の申請方法: 経費精算画面で申請内容を入力し、申請ボタンを押します。"


def _scored_context(score):
    record = _record("r1", "docling", 1, 1, EVIDENCE)
    if score is not None:
        record = replace(record, metadata={"rerank": {"rank": 1, "relevance_score": score}})
    return replace(_answer_context([record]), ranked_candidates=(record,))


class CragGradeSkipTests(unittest.TestCase):
    """規則の信号が強いときだけ CRAG の評価（1 回目）を飛ばす。"""

    def setUp(self):
        self.settings = _settings(crag_skip_on_strong_signals=True, crag_skip_min_rerank_score=0.8)
        self.enterContext(patch.object(generation, "load_latest_or_source_chunk_run", return_value=_chunk_run_stub()))

    def build(self, context, settings=None, question=QUESTION):
        grade = _crag_grade(sufficient=True, relevant_chunk_ids=["r1"], support_text=context.records[0].text)
        with (
            patch.object(generation, "build_adb_hybrid_answer_context", return_value=context),
            patch.object(generation, "parse_text_response", return_value=grade) as parse,
        ):
            built, _rewrites, attempts = generation.build_crag_answer_context(question, "abcdef", ["docling"], settings or self.settings)
        return built, attempts, parse.call_count

    def test_strong_signals_skip_the_grade_and_keep_the_context(self):
        context = _scored_context(0.93)
        built, attempts, calls = self.build(context)
        self.assertEqual(calls, 1)
        self.assertEqual([r.id for r in built.records], ["r1"])
        self.assertNotEqual(attempts[0].stop_reason, generation.CRAG_SKIP_STOP_REASON)

    def test_weak_or_disabled_signals_call_the_grade(self):
        cases = {
            "disabled": (_scored_context(0.93), replace(self.settings, crag_skip_on_strong_signals=False), QUESTION),
            "low-score": (_scored_context(0.5), None, QUESTION),
            "no-rerank": (_scored_context(None), None, QUESTION),
            "missing-request": (_scored_context(0.93), None, "経費精算の申請方法と、出張旅費の上限額を教えてください"),
            "error-message": (_scored_context(0.93), None, "「E0123 申請できません」と表示されたときの対処は？"),
            # 列挙・網羅・件数の質問は飛ばさない（#1503。根拠が欠けても語の一致では根拠ありに見える）。
            "enumeration-all": (_scored_context(0.93), None, "経費精算の申請方法をすべて教えてください"),
            "enumeration-each": (_scored_context(0.93), None, "各部署の経費精算の申請方法を教えてください"),
            "enumeration-list": (_scored_context(0.93), None, "経費精算の申請方法の一覧を教えてください"),
            "count": (_scored_context(0.93), None, "経費精算の申請方法は何件ありますか"),
        }
        for name, (context, settings, question) in cases.items():
            with self.subTest(name):
                _built, attempts, calls = self.build(context, settings, question)
                self.assertEqual(calls, 1)
                self.assertNotEqual(attempts[0].stop_reason, generation.CRAG_SKIP_STOP_REASON)

    def test_skipped_step_records_the_reason(self):
        lines = []
        token = generation._execution_lines.set(lines)
        try:
            self.build(_scored_context(0.93))
        finally:
            generation._execution_lines.reset(token)
        display = generation.format_question_display(QUESTION, lines)
        self.assertIn("根拠確認（1回目）", display)
        self.assertNotIn("状態: 省略", display)
        self.assertIn("語の一致による充足性の判定は行わない", display)


class OriginalQueryPrefetchTests(unittest.TestCase):
    """質問拡張の LLM と元の質問の検索を並行に実行する。"""

    def setUp(self):
        from grounded_stub import echo_model

        self.settings = _settings()
        self.enterContext(patch.object(generation, "load_latest_or_source_chunk_run", return_value=_chunk_run_stub()))
        self.enterContext(patch.object(generation, "check_adb_hybrid_search_ready"))
        self.context = _answer_context([_record("r1", "docling", 1, 1, "source evidence")])
        self.search = self.enterContext(patch.object(generation, "build_adb_hybrid_answer_context", return_value=self.context))
        self.enterContext(patch.object(generation, "parse_text_response", side_effect=echo_model))
        self.prefetched = threading.Event()
        self.calls = []

    def dependencies(self, prefetch):
        def unused(*args, **kwargs):
            raise AssertionError("not used")

        return AnswerDependencies(
            search=unused, check_ready=unused, parse_text=unused, parse_images=unused, rerank=unused, prefetch_search=prefetch
        )

    def expansion_waiting_for_prefetch(self, question, strategy, settings, **options):
        # 先読みが終わるまで質問拡張（LLM の応答待ち）を終えない。直列なら待ちきれずに失敗する。
        self.assertTrue(self.prefetched.wait(5), "質問拡張と元の質問の検索が並行に動いていない")
        return generation.QueryExpansionResult(
            original_question=question,
            selected_strategy=generation.RAG_FUSION_STRATEGY,
            effective_strategy=generation.RAG_FUSION_STRATEGY,
            generated_queries=("追加の検索文",),
        )

    def run_answer(self, question, strategy=generation.RAG_FUSION_LABEL):
        return generation.answer_question_result(
            question, "abcdef", ["docling"], self.settings, query_strategy=strategy, answer_flow=generation.STANDARD_ANSWER_FLOW_LABEL
        )

    def test_prefetch_runs_during_query_expansion_with_the_first_search_query(self):
        caller = threading.current_thread()

        def prefetch(**kwargs):
            # 注入した I/O（contextvars）が先読みの thread でも見える。
            from rag_engine.dependencies import injected_prefetch_search

            self.calls.append((kwargs, threading.current_thread() is caller, injected_prefetch_search() is prefetch))
            self.prefetched.set()

        with (
            bind_dependencies(self.dependencies(prefetch)),
            patch.object(generation, "build_query_expansion", side_effect=self.expansion_waiting_for_prefetch),
        ):
            result = self.run_answer("受注の登録方法は？")
        self.assertEqual(len(self.calls), 1)
        kwargs, same_thread, visible = self.calls[0]
        self.assertFalse(same_thread)
        self.assertTrue(visible)
        # 文書検索の先頭の検索文と同じ形（NFKC）・同じ件数で先に読む。
        self.assertEqual(kwargs["query"], "受注の登録方法は?")
        self.assertEqual(kwargs["candidate_limit"], generation._retrieval_candidate_limit(generation.DEFAULT_RETRIEVAL_TOP_K))
        first_query = self.search.call_args.kwargs["retrieval_queries"][0]
        self.assertEqual(first_query, kwargs["query"])
        self.assertIn("元の質問の検索を、検索文の生成と並行に実行しました", result.question_display)

    def test_results_are_the_same_with_and_without_prefetch(self):
        def prefetch(**kwargs):
            self.prefetched.set()

        with patch.object(generation, "build_query_expansion", side_effect=self.expansion_waiting_for_prefetch):
            with bind_dependencies(self.dependencies(prefetch)):
                with_prefetch = self.run_answer("受注の登録方法は？")
            with_prefetch_queries = self.search.call_args.kwargs["retrieval_queries"]
            self.prefetched.set()  # 先読みなしでも質問拡張を待たせない
            without_prefetch = self.run_answer("受注の登録方法は？")
        self.assertEqual(self.search.call_args.kwargs["retrieval_queries"], with_prefetch_queries)
        self.assertEqual(with_prefetch.answer_text, without_prefetch.answer_text)
        self.assertEqual(with_prefetch.evidence_items, without_prefetch.evidence_items)
        self.assertEqual(with_prefetch.retrieval_query_plan, without_prefetch.retrieval_query_plan)

    def test_failed_prefetch_keeps_the_answer(self):
        def prefetch(**kwargs):
            self.prefetched.set()
            raise ConnectionError("db down")

        with (
            bind_dependencies(self.dependencies(prefetch)),
            patch.object(generation, "build_query_expansion", side_effect=self.expansion_waiting_for_prefetch),
        ):
            result = self.run_answer("受注の登録方法は？")
        self.assertIn("元の質問の検索の先読みに失敗しました（ConnectionError）", result.question_display)
        self.assertEqual(self.search.call_count, 1)

    def test_simple_strategy_does_not_prefetch(self):
        with bind_dependencies(self.dependencies(lambda **kwargs: self.calls.append(kwargs))):
            self.run_answer("受注の登録方法は？", strategy=generation.SIMPLE_RETRIEVAL_LABEL)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
