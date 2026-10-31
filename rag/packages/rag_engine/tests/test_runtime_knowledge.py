"""runtime knowledge の挙動を保護するテスト。"""

import json
import os
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from grounded_stub import AnswerOutput

from rag_engine.config import get_settings
from rag_engine.generation.answering import (
    QUESTION_DISPLAY_METADATA_SEPARATOR,
    RUNTIME_KNOWLEDGE_SEPARATOR,
    SIMPLE_RETRIEVAL_LABEL,
    STANDARD_ANSWER_FLOW_LABEL,
    AnswerContext,
    AnswerRecord,
    answer_question_result,
    answer_result_payload,
    extract_original_question,
)
from rag_engine.knowledge.runtime_knowledge import (
    RUNTIME_KNOWLEDGE_FILE_NAME,
    build_runtime_knowledge_context,
    load_runtime_knowledge,
    runtime_retrieval_queries,
    runtime_rules_status,
    runtime_terms_status,
)
from rag_engine.retrieval.text_search_tokenizer import TextSearchTokenizationResult


class RuntimeKnowledgeTests(unittest.TestCase):
    def test_load_runtime_knowledge_matches_terms_rules_and_expands_question(self):
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            _write_runtime_knowledge(output_dir)

            knowledge = load_runtime_knowledge(output_dir)
            context = build_runtime_knowledge_context("出荷業務で再発行できない条件は？", output_dir)

        self.assertTrue(knowledge.loaded)
        self.assertEqual([term.term for term in knowledge.terms], ["納品書"])
        self.assertEqual([rule.rule_id for rule in knowledge.rules], ["reissue"])
        self.assertIn("納品書", context.expanded_question)
        self.assertIn("出荷業務", context.expanded_question)
        self.assertIn("再発行条件", context.expanded_question)
        self.assertTrue(context.has_matches)
        self.assertIn("[Runtime Glossary / Rules]", context.prompt_context())

    def test_runtime_retrieval_queries_dedupes_original_and_expanded_queries(self):
        queries = runtime_retrieval_queries("質問", "質問\n別名 正式名称", ("質問", "追加検索"))

        self.assertEqual(queries, ("質問", "質問 別名 正式名称", "追加検索"))

    def test_context_is_not_display_active_without_matches(self):
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            _write_runtime_knowledge(output_dir)

            context = build_runtime_knowledge_context("概要を教えてください。", output_dir)

        self.assertFalse(context.has_matches)
        self.assertFalse(context.is_active)
        self.assertEqual(context.query_source, "原質問のみ")

    def test_load_runtime_knowledge_reports_bad_json_without_raising(self):
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            (output_dir / RUNTIME_KNOWLEDGE_FILE_NAME).write_text("{bad json", encoding="utf-8")

            knowledge = load_runtime_knowledge(output_dir)
            context = build_runtime_knowledge_context("再発行", output_dir)

        self.assertFalse(knowledge.loaded)
        self.assertTrue(knowledge.error)
        self.assertEqual(context.expanded_question, "再発行")
        self.assertTrue(context.error)

    def test_runtime_knowledge_lifecycle_filters_inactive_items(self):
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            payload = {
                "schema_version": 1,
                "terms": [
                    {"term": "承認済み", "status": "approved", "reviewed_at": "2026-09-08T00:00:00Z"},
                    {"term": "古い用語", "status": "deprecated"},
                ],
                "rules": [
                    {
                        "id": "stale-rule",
                        "title": "要再確認ルール",
                        "triggers": ["再確認"],
                        "content": "レビュー期限が近いが現行ルールとして使う",
                        "status": "stale_review_needed",
                        "next_review_at": "2026-10-01T00:00:00Z",
                    },
                    {
                        "id": "draft-rule",
                        "title": "下書きルール",
                        "triggers": ["下書き"],
                        "content": "未承認",
                        "status": "draft",
                    },
                ],
            }
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / RUNTIME_KNOWLEDGE_FILE_NAME).write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )

            context = build_runtime_knowledge_context("承認済みと古い用語、再確認、下書きについて", output_dir)
            trace = context.to_payload()

        self.assertEqual([term.term for term in context.matched_terms], ["承認済み"])
        self.assertEqual([rule.rule_id for rule in context.matched_rules], ["stale-rule"])
        self.assertEqual(trace["matched_terms"][0]["reviewed_at"], "2026-09-08T00:00:00Z")
        self.assertEqual(trace["matched_rules"][0]["next_review_at"], "2026-10-01T00:00:00Z")

    def test_status_guides_to_the_profile_knowledge_screen(self):
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            missing = build_runtime_knowledge_context("概要は？", output_dir)
            (output_dir / RUNTIME_KNOWLEDGE_FILE_NAME).write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "terms": [{"term": "古い用語", "status": "deprecated"}],
                        "rules": [{"id": "r1", "title": "下書き", "triggers": ["古い用語"], "content": "x", "status": "draft"}],
                    }
                ),
                encoding="utf-8",
            )
            inactive = build_runtime_knowledge_context("古い用語は？", output_dir)

        # 案内の行き先は、今の画面にある検索・回答プロファイルの「プロファイルの知識」の各タブ（#3071 / #3451）。
        for status_of, tab in ((runtime_terms_status, "用語・同義語"), (runtime_rules_status, "回答ルール")):
            with self.subTest(tab=tab):
                self.assertTrue(status_of(missing).startswith("未登録："))
                self.assertTrue(status_of(inactive).startswith("有効な登録なし："))
                for status in (status_of(missing), status_of(inactive)):
                    self.assertIn(f"検索・回答プロファイルの「プロファイルの知識」の「{tab}」", status)
                    self.assertNotIn("用語・ルール管理", status)
        self.assertEqual((inactive.registered_term_count, inactive.active_term_count), (1, 0))
        self.assertEqual((inactive.registered_rule_count, inactive.active_rule_count), (1, 0))
        self.assertEqual((inactive.registered_count, inactive.active_count), (2, 0))

    def test_settings_reads_runtime_knowledge_path(self):
        with patch.dict(os.environ, {"RUNTIME_KNOWLEDGE_PATH": "~/runtime-rules.json"}, clear=True), patch("rag_engine.config.load_dotenv"):
            settings = get_settings(dotenv_path=None)

        self.assertEqual(settings.runtime_knowledge_path, Path("~/runtime-rules.json").expanduser())

    def test_answer_flow_uses_runtime_knowledge_for_search_prompt_display_and_payload(self):
        from grounded_stub import stub_generation

        stub_generation(self)
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            _write_runtime_knowledge(output_dir)
            settings = replace(get_settings(dotenv_path=None), output_dir=output_dir, runtime_knowledge_path=None)
            records = [
                AnswerRecord(
                    id="chunk-docling-c000001",
                    engine="docling",
                    engine_label="Docling",
                    page=1,
                    seq_no=1,
                    category="ChildChunk",
                    text="納品書の再発行条件は伝票番号と発行日を確認することです。",
                    chunk_id="chunk-docling-c000001",
                    chunk_level="child",
                    chunk_seq=1,
                    metadata={"active": True},
                )
            ]
            context = AnswerContext(records=records, text="[chunk] 納品書 再発行条件")

            with (
                patch(
                    "rag_engine.generation.answering.load_latest_or_source_chunk_run",
                    return_value=SimpleNamespace(chunk_run_id="abcdef-1234567890ab"),
                ),
                patch("rag_engine.generation.answering.check_adb_hybrid_search_ready"),
                patch("rag_engine.generation.answering.build_adb_hybrid_answer_context", return_value=context) as build_context,
                patch("rag_engine.generation.answering.load_domain_keywords", return_value=[]),
                patch(
                    "rag_engine.generation.answering.tokenize_text_search_query_with_trace",
                    return_value=TextSearchTokenizationResult(
                        tokens=("納品書", "再発行条件"),
                        candidate_tokens=("納品書", "再発行条件"),
                        candidate_count=2,
                        max_tokens=24,
                    ),
                ) as tokenize,
                patch("rag_engine.generation.answering.build_oracle_text_query", return_value="{納品書} OR {再発行条件}"),
                patch("rag_engine.generation.answering.tokenizer_fingerprint", return_value="tokfp"),
                patch(
                    "rag_engine.generation.answering.parse_text_response",
                    return_value=AnswerOutput(
                        answer="再発行条件を確認してください。",
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
                    "出荷業務で再発行できない条件は？",
                    "abcdef",
                    ["docling"],
                    settings,
                    query_strategy=SIMPLE_RETRIEVAL_LABEL,
                    answer_flow=STANDARD_ANSWER_FLOW_LABEL,
                )

        tokenized_questions = [call.args[0] for call in tokenize.call_args_list]
        retrieval_queries = build_context.call_args.kwargs["retrieval_queries"]
        prompt = parse.call_args.args[1]
        payload = answer_result_payload(result, answer_id="answer123", run_id="abcdef")

        self.assertTrue(any("納品書" in question for question in tokenized_questions))
        self.assertIn("納品書", retrieval_queries[1])
        self.assertIn("再発行条件", retrieval_queries[1])
        self.assertIn("[Runtime Glossary / Rules]", prompt)
        self.assertIn("再発行条件", prompt)
        self.assertIn(QUESTION_DISPLAY_METADATA_SEPARATOR, result.question_display)
        self.assertIn("用語・同義語の確認", result.question_display)
        self.assertIn("結果: 一致: 納品書", result.question_display)
        self.assertIn("影響: 一致した用語の別名を補助の検索文に加えます。", result.question_display)
        self.assertIn("回答ルールの確認", result.question_display)
        self.assertIn("結果: 一致: 再発行条件", result.question_display)
        self.assertIn("影響: 回答生成に使う指示 1 件を渡します。", result.question_display)
        # 検索文の出どころは、原質問に語を足したもの（用語・同義語と回答ルール）と、用語と別名だけの検索文を分ける（#3490）。
        self.assertIn("[原質問 + 用語・同義語 + 確認・回答ルール] ", result.question_display)
        self.assertIn("[用語・同義語だけ] 納品書 出荷業務 nouhinsho", result.question_display)
        self.assertNotIn("用語・ルール", result.question_display)
        self.assertEqual(payload["runtime_knowledge"]["query_source"], "原質問 + 用語・同義語 + 確認・回答ルール")
        self.assertTrue(payload["text_search_query_source"].startswith("原質問 + 用語・同義語 + 確認・回答ルール"))
        self.assertNotIn(RUNTIME_KNOWLEDGE_SEPARATOR, result.question_display)
        self.assertNotIn("パス:", result.question_display)
        self.assertIn("text_search_queries", payload)
        self.assertEqual(payload["runtime_knowledge"]["matched_terms"][0]["term"], "納品書")
        self.assertEqual(payload["runtime_knowledge"]["matched_rules"][0]["id"], "reissue")

    def test_extract_original_question_strips_runtime_knowledge_metadata(self):
        displayed = "\n".join(["元の質問", "", RUNTIME_KNOWLEDGE_SEPARATOR, "用語: 納品書"])

        self.assertEqual(extract_original_question(displayed), "元の質問")


def _write_runtime_knowledge(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "terms": [
            {
                "term": "納品書",
                "aliases": ["出荷業務", "nouhinsho"],
                "description": "納品書発行に関する画面群",
                "source": "運用ルール",
            }
        ],
        "rules": [
            {
                "id": "reissue",
                "title": "再発行条件",
                "triggers": ["再発行", "納品書"],
                "content": "再発行は伝票番号と発行日の確認後に行う",
                "source": "運用ルール",
            }
        ],
    }
    (output_dir / RUNTIME_KNOWLEDGE_FILE_NAME).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
