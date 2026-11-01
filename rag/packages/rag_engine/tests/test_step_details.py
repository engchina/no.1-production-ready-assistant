"""処理の経過の段を開いて見る、工程ごとの詳細（#2341）。

回答フローの各工程が、共有の詳細の形（見出しと値 ``fields`` と行 ``lines``）で、意味のある有限の詳細を
``execution_steps[].details`` に残すことを、合成の質問・根拠で確かめる（外部のサービスには接続しない）。
"""

import unittest
from unittest.mock import patch

from grounded_stub import echo_model
from test_answer_generation import (
    _answer_context,
    _chunk_record,
    _chunk_run_stub,
    _crag_grade,
    _hybrid_result,
    _rerank_ready_settings,
    _stored_child,
)

from rag_engine.adapters.model_api import RerankTextRank
from rag_engine.config import get_settings
from rag_engine.generation import step_details
from rag_engine.generation.answer_models import AnswerRecord
from rag_engine.generation.answering import (
    CRAG_ANSWER_FLOW_LABEL,
    SIMPLE_RETRIEVAL_LABEL,
    STANDARD_ANSWER_FLOW_LABEL,
    CragRetrievalGradeOutput,
    answer_question_result,
)
from rag_engine.knowledge.classification import ClassificationFilter


def _fields(step: dict) -> dict[str, str]:
    return {field["label"]: field["value"] for field in step.get("details", {}).get("fields", [])}


def _lines(step: dict) -> list[str]:
    return list(step.get("details", {}).get("lines", []))


def _by_name(result) -> dict[str, dict]:
    return {step["name"]: step for step in result.execution_steps}


def _hybrid_metadata(channels: list[str], heading: str) -> dict:
    return {
        "active": True,
        "section_path": ["第 1 章 総則", heading],
        "adb_hybrid": {"retrieval_channels": channels, "rrf_score": 0.03},
    }


class StandardFlowStepDetailsTest(unittest.TestCase):
    """通常の回答フロー: 検索の実装と並べ替え・回答の生成だけを差し替えて、工程ごとの詳細を確かめる。"""

    def run_flow(self, settings=None, **options):
        search_result = _hybrid_result(
            _stored_child(
                "chunk-docling-c000001",
                1,
                "合成の手順 A の本文",
                metadata=_hybrid_metadata(["keyword:oracle_text", "vector:q1"], "1.1 申請の手順"),
            ),
            _stored_child("chunk-docling-c000002", 2, "合成の手順 B の本文", metadata=_hybrid_metadata(["vector:q1"], "1.2 申請の期限")),
        )
        with (
            patch("rag_engine.generation.answering.load_latest_or_source_chunk_run", return_value=_chunk_run_stub()),
            patch("rag_engine.generation.answering.check_adb_hybrid_search_ready"),
            patch("rag_engine.generation.answering.search_adb_hybrid_chunks", return_value=search_result),
            patch(
                "rag_engine.generation.answering.rerank_text_with_scores",
                return_value=[RerankTextRank(1, 0.94), RerankTextRank(0, 0.73)],
            ),
            patch("rag_engine.generation.answering.parse_text_response", side_effect=echo_model),
        ):
            return answer_question_result(
                "合成の申請の期限は？",
                "abcdef",
                ["docling"],
                settings or _rerank_ready_settings(),
                query_strategy=SIMPLE_RETRIEVAL_LABEL,
                rerank_enabled=True,
                answer_flow=STANDARD_ANSWER_FLOW_LABEL,
                **options,
            )

    def test_every_step_records_bounded_details(self):
        result = self.run_flow()
        self.assertTrue(result.execution_steps)
        for step in result.execution_steps:
            with self.subTest(step=step["name"]):
                details = step.get("details")
                self.assertIsInstance(details, dict)
                self.assertTrue(details.get("fields") or details.get("lines"))
                self.assertLessEqual(len(details.get("fields", [])), 16)
                self.assertLessEqual(len(details.get("lines", [])), 21)

    def test_question_and_preparation_steps(self):
        steps = _by_name(self.run_flow())
        understanding = _fields(steps["質問の理解"])
        self.assertIn("目的", understanding)
        self.assertEqual(understanding["答える要求"], "1 件")
        self.assertIn("結果", understanding)
        self.assertIn("結果", _fields(steps["用語・同義語の確認"]))
        self.assertIn("結果", _fields(steps["回答ルールの確認"]))
        # 検索の前提の確認は工程にしない（#3495）。普段どおりの検索範囲・準備の状態は示さない。
        self.assertNotIn("検索の準備確認", steps)
        self.assertNotIn("分類フィルタ", _fields(steps["文書検索"]))
        self.assertEqual(_fields(steps["質問拡張戦略"])["設定"], SIMPLE_RETRIEVAL_LABEL)
        queries = steps["検索文と検索語の確定"]
        self.assertRegex(_fields(queries)["結果"], r"^検索文 \d+ 本$")
        self.assertIn("全文検索のキーワード", _fields(queries))
        self.assertTrue(any(line.startswith("1. [原質問]") for line in _lines(queries)))
        self.assertEqual(_fields(steps["回答生成フロー"])["設定"], STANDARD_ANSWER_FLOW_LABEL)

    def test_classification_filter_is_shown_on_search_step_only_when_set(self):
        """分類フィルタを指定したときだけ、文書検索の工程に示す（#3495）。"""
        steps = _by_name(self.run_flow(classification_filter=ClassificationFilter(large_category="分類 A")))
        self.assertNotIn("検索の準備確認", steps)
        self.assertEqual(_fields(steps["文書検索"])["分類フィルタ"], "大分類=分類 A")

    def test_search_and_rerank_steps_show_routes_scores_and_top_documents(self):
        steps = _by_name(self.run_flow())
        search = _fields(steps["文書検索"])
        # 検索に使った検索文の数は、確定した検索文の数と同じ。
        self.assertEqual(f"検索文 {search['検索文']}", _fields(steps["検索文と検索語の確定"])["結果"])
        self.assertEqual(search["検索の候補"], "2 件（1 文書）")
        self.assertEqual(search["候補の経路"], "ベクトルと全文の両方 1・ベクトルだけ 1")
        self.assertTrue(search["結果"].startswith("回答に渡す根拠 "))
        self.assertIn("回答に渡す根拠:", _lines(steps["文書検索"]))
        rerank = steps["Rerank"]
        self.assertEqual(_fields(rerank)["並べ替える候補"], "2 件")
        self.assertEqual(_fields(rerank)["最上位の関連度"], "0.940")
        # 上位は関連度の順に、文書名 › 見出し・頁で示す（本文は入れない）。
        self.assertIn("1. 0.940 manual.pdf › 第 1 章 総則 › 1.2 申請の期限（p.1）", _lines(rerank))
        self.assertFalse(any("本文" in line for line in _lines(rerank)))

    def test_hidden_steps_still_explain_why_nothing_happened(self):
        steps = _by_name(self.run_flow())
        # 生成前に語の一致で根拠の充足を判定した工程は表示しない。
        self.assertNotIn("要求ごとの根拠の確認", steps)
        self.assertEqual("根拠に画像がありません", _fields(steps["回答に添付する画像"])["結果"])
        # 記録の文（question_display）からは従来どおり省く。
        result = self.run_flow()
        self.assertNotIn("根拠に画像がありません", result.question_display)

    def test_answer_round_records_audit_counts(self):
        steps = _by_name(self.run_flow())
        answer = _fields(steps["回答文の生成と根拠確認（1回目）"])
        self.assertIn("目的", answer)
        self.assertIn("結果", answer)
        self.assertIn("監査した説明", answer)
        self.assertIn("要求の充足（監査）", answer)
        self.assertNotIn("採否", answer)  # 1 組目は比べる相手が無い

    def test_answer_round_without_audit_does_not_describe_an_audit(self):
        """監査の型が none なら、目的・推論の量・要求の充足に、行っていない監査を書かない（#2966）。

        条を出典にした説明にも監査を実行したとは表示しない。
        """
        from dataclasses import replace

        steps = _by_name(self.run_flow(replace(_rerank_ready_settings(), answer_audit_mode="none")))
        step = steps["回答文の生成と根拠確認（1回目）"]
        answer = _fields(step)
        self.assertIn("生成後の LLM 監査は実行しません", answer["目的"])
        self.assertNotIn("条を出典にした説明だけに掛けます", answer["目的"])
        self.assertNotIn("別のLLM呼出で適用性を監査", answer["目的"])
        self.assertNotIn("監査した説明", answer)
        self.assertNotIn("要求の充足（監査）", answer)
        self.assertIn("要求の充足（原文との照合）", answer)
        self.assertFalse(any(line.startswith("監査の推論の量") for line in _lines(step)))


class CragFlowStepDetailsTest(unittest.TestCase):
    """補正の回答フロー: 根拠の確認の判定・理由・次の対処、探し直しの検索文を詳細に残す。"""

    def test_grade_and_rewrite_steps(self):
        grades = [
            _crag_grade(sufficient=False, confidence=0.1, reason="根拠が話題違い", rewritten_query="合成の探し直しの検索文"),
            _crag_grade(
                sufficient=True,
                confidence=0.92,
                relevant_chunk_ids=["chunk-good"],
                reason="根拠が直接答えている",
                support_text="設定画面を開いて保存ボタンを押します。",
            ),
        ]

        def model(system, prompt, settings, schema, **options):
            return grades.pop(0) if schema is CragRetrievalGradeOutput else echo_model(system, prompt, settings, schema, **options)

        initial = _answer_context([_chunk_record("chunk-bad", "child", 1, "", "合成の別の話題")])
        corrected = _answer_context([_chunk_record("chunk-good", "child", 2, "", "設定画面を開いて保存ボタンを押します。")])
        with (
            patch("rag_engine.generation.answering.load_latest_or_source_chunk_run", return_value=_chunk_run_stub()),
            patch("rag_engine.generation.answering.check_adb_hybrid_search_ready"),
            patch("rag_engine.generation.answering.build_adb_hybrid_answer_context", side_effect=[initial, corrected]),
            patch("rag_engine.generation.answering.parse_text_response", side_effect=model),
        ):
            result = answer_question_result(
                "合成の質問",
                "abcdef",
                ["docling"],
                get_settings(),
                query_strategy=SIMPLE_RETRIEVAL_LABEL,
                answer_flow=CRAG_ANSWER_FLOW_LABEL,
            )
        steps = _by_name(result)
        first = _fields(steps["根拠確認（1回目）"])
        self.assertTrue(first["結果"].startswith("必要な説明が不足"))
        self.assertEqual(first["判定した根拠"], "1 件")
        self.assertEqual(first["関係あり / なし"], "0 件 / 0 件")
        self.assertEqual(first["次の対処"], "検索文を書き換えて探し直す")
        self.assertEqual(first["書き換えた検索文"], "合成の探し直しの検索文")
        self.assertIn("理由: 根拠が話題違い", _lines(steps["根拠確認（1回目）"]))
        rewrite = _fields(steps["補正検索の準備"])
        self.assertEqual(rewrite["結果"], "追加する検索文: 合成の探し直しの検索文")
        self.assertTrue(rewrite["探し直しの回数"].startswith("1 回目"))
        second_search = steps["文書検索（2回目）"]
        self.assertIn("回答に渡す根拠", _fields(second_search)["結果"])
        self.assertTrue(any("今回追加した 1 本" in line for line in _lines(second_search)))
        second = _fields(steps["根拠確認（2回目）"])
        self.assertTrue(second["結果"].startswith("生成に利用可能"))
        self.assertEqual(second["関係あり / なし"], "1 件 / 0 件")
        self.assertNotIn("次の対処", second)
        self.assertIn("検索文", _fields(steps["検索語の再確定（書き換え後）"]))


class StepDetailsHelpersTest(unittest.TestCase):
    def test_record_label_uses_names_and_pages_only(self):
        record = AnswerRecord(
            id="r1",
            engine="docling",
            engine_label="Docling",
            page=3,
            seq_no=1,
            category="Text",
            text="本文は出さない",
            source="folder/manual-a.pdf",
            page_end=4,
            metadata={"section_path": ["第 1 章", "1.1 節", "1.1.1 項"]},
        )
        self.assertEqual(step_details.record_label(record), "manual-a.pdf › 1.1 節 › 1.1.1 項（p.3–4）")

    def test_channel_line_and_numbered_limits(self):
        def rec(channels):
            return AnswerRecord(
                id="x",
                engine="e",
                engine_label="e",
                page=1,
                seq_no=1,
                category="Text",
                text="",
                metadata={"adb_hybrid": {"retrieval_channels": channels}},
            )

        # 経路は検索の実装と backend の検索が書く形（`vector:q1` / `keyword:oracle_text:q2` など。#3443）。
        records = [
            rec(["keyword:oracle_text:q2", "vector:q1"]),
            rec(["keyword:oracle_text"]),
            rec(["image_vector:q1"]),
            rec(["profile:file_data_confirmation"]),
        ]
        self.assertEqual(
            step_details.channel_line(records),
            "ベクトルと全文の両方 1・ベクトルだけ 1・全文だけ 1・そのほか（目録・画面などで加えた候補） 1",
        )
        self.assertEqual(step_details.numbered(["a", "b", "c"], limit=2), ["1. a", "2. b", "ほか 1 件"])
        self.assertEqual(step_details.join_values(["a", "", "b"], limit=1), "a ほか 1 件")
        self.assertEqual(step_details.aspect_labels(["procedure", "unknown_aspect"]), "操作の手順、unknown_aspect")


if __name__ == "__main__":
    unittest.main()
