"""rag_engine の数・単位・元号・文末の書き方は言語資源から作る(#2181 段 1b-2)。"""

import unittest

from rag_parser_core.language import bind_pack, builtin_pack, resolve

from rag_engine.chunking.headings import _heading_patterns
from rag_engine.generation.value_grounding import _date
from rag_engine.retrieval.enumeration import enumeration_request
from rag_engine.retrieval.inquiry_conditions import _date_pattern


def _custom(**sections):
    return resolve(builtin_pack(), sections)


class LanguagePackEngineTest(unittest.TestCase):
    def test_acquisition_notice_uses_display_pack_without_changing_ingest(self):
        from rag_engine.generation.answer_labels import answer_labels

        original = answer_labels().guide_acquisition_incomplete_template
        custom = _custom(answer_labels={"guide_acquisition_incomplete_template": "合成の未完了: {steps}"})
        with bind_pack(custom):
            self.assertEqual(answer_labels().guide_acquisition_incomplete_template.format(steps="s1"), "合成の未完了: s1")
            self.assertEqual(custom.ingest_hash, builtin_pack().ingest_hash)
        self.assertEqual(answer_labels().guide_acquisition_incomplete_template, original)

    def test_heading_levels_follow_units(self):
        def levels(text):
            return [level for pattern, level in _heading_patterns().levels if pattern.match(text)]

        self.assertEqual(levels("第2部 概要"), [1])
        self.assertEqual(levels("第2巻 概要"), [])
        with bind_pack(_custom(numbers={"part_units": ["編", "部", "巻"]})):
            self.assertEqual(levels("第2巻 概要"), [1])

    def test_sentence_like_heading_follows_endings(self):
        self.assertTrue(_heading_patterns().sentence_heading.search("入力します。"))
        self.assertFalse(_heading_patterns().sentence_heading.search("〜とは？"))
        with bind_pack(_custom(symbols={"sentence_like_endings": ["。", "ます", "とは？"]})):
            self.assertTrue(_heading_patterns().sentence_heading.search("〜とは？"))

    def test_enumeration_chapter_range_follows_connectors(self):
        question = "第2章〜第4章の手順をすべて挙げてください"
        self.assertEqual(enumeration_request(question).chapters, (2, 3, 4))
        question = "第2章 to 第4章の手順をすべて挙げてください"
        self.assertNotEqual(enumeration_request(question).chapters, (2, 3, 4))
        with bind_pack(_custom(numbers={"range_connectors": ["から", "to"]})):
            self.assertEqual(enumeration_request(question).chapters, (2, 3, 4))

    def test_dates_use_era_words_not_single_characters(self):
        self.assertEqual([m.group(0) for m in _date_pattern().finditer("令和6年4月の申請")], ["令和6年4月"])
        # 以前は元号を字の集合で書いていて、元号の 1 字だけでも日付にしていた。
        self.assertEqual([m.group(0) for m in _date_pattern().finditer("和6年4月の申請")], [])
        self.assertEqual(_date().sub("", "R6年度の上限は100"), "の上限は100")
        with bind_pack(_custom(numbers={"era_letters": ["K=令和"]})):
            self.assertEqual(_date().sub("", "K6年度の上限は100"), "の上限は100")

    def test_content_labels_follow_the_pack(self):
        """表・図の内容のラベルは書く側と読む側が同じ言語資源から作る(#2181 段 2c)。"""
        from rag_engine.generation.grounded import _without_screen_examples
        from rag_engine.parsing.content_labels import content_labels
        from rag_engine.parsing.picture_descriptions import vision_description_text
        from rag_engine.parsing.table_structure import _fixed_group_pieces
        from rag_engine.parsing.vision_prompt_rules import is_vision_enumeration_line

        payload = {"retrieval_text": "画面の説明", "visible_buttons": ["保存"], "visible_values": ["ID=A1"]}
        structure = {"caption": "一覧", "column_headers": [{"column_index": 0, "text": "名前"}]}
        self.assertIn("■ 要点\n回答用本文: 画面の説明", vision_description_text(payload))
        self.assertEqual(_fixed_group_pieces(structure, ""), ["表タイトル: 一覧", "列見出し: 名前"])
        custom = _custom(
            content_labels={
                "table_title": "表の名前",
                "vision_group_prefix": "◆",
                "vision_groups": ["概要=summary", "見えるもの=visible"],
                "vision_fields": ["説明=retrieval_text", "押すもの=visible_buttons", "例の値=visible_values"],
            }
        )
        with bind_pack(custom):
            text = vision_description_text(payload)
            self.assertIn("◆ 概要\n説明: 画面の説明", text)
            self.assertIn("押すもの: 保存", text)
            self.assertEqual(_fixed_group_pieces(structure, "")[0], "表の名前: 一覧")
            self.assertTrue(is_vision_enumeration_line("押すもの: 保存"))
            self.assertTrue(content_labels().generated.search(text))
            self.assertNotIn("A1", _without_screen_examples(text))

    def test_search_terms_follow_the_pack(self):
        """全文検索の質問の語（止め語・1 字の語・語の端の助詞）は言語資源から作る(#2181 段 3)。"""
        from rag_engine.retrieval.text_search_tokenizer import (
            TextSearchTokenizerConfig,
            _keep_token,
            _trim_japanese_particle_edges,
            tokenize_text_search_query,
            tokenizer_fingerprint,
        )

        self.assertFalse(_keep_token("こと", frozenset({"こと"})))
        self.assertTrue(_keep_token("図", frozenset()))
        self.assertEqual(_trim_japanese_particle_edges("申請を"), "申請")
        before = tokenizer_fingerprint(TextSearchTokenizerConfig())
        custom = _custom(
            search_terms={
                "excluded_terms": ["申請"],
                "particle_edge_chars": ["が"],
            }
        )
        with bind_pack(custom):
            self.assertFalse(_keep_token("申請", frozenset()))
            self.assertEqual(_trim_japanese_particle_edges("申請を"), "申請を")
            self.assertNotIn("申請", tokenize_text_search_query("申請の期限"))
            self.assertNotEqual(tokenizer_fingerprint(TextSearchTokenizerConfig()), before)

    def test_question_cues_follow_the_pack(self):
        """質問の理解の手がかり（原因・総称の対象・要求の語）は言語資源から作る(#2181 段 4)。"""
        from rag_engine.retrieval.task_contract import action_targets, asks_cause, task_contract

        self.assertTrue(asks_cause("なぜ止まるのか"))
        self.assertFalse(asks_cause("どの理屈で止まるのか"))
        self.assertEqual(action_targets("帳票を削除したい"), ["帳票"])
        with bind_pack(_custom(question_cues={"cause_cues": ["理屈"], "generic_target_words": ["帳票"]})):
            self.assertFalse(asks_cause("なぜ止まるのか"))
            self.assertTrue(asks_cause("どの理屈で止まるのか"))
            self.assertEqual(action_targets("帳票を削除したい"), [])
            self.assertEqual(task_contract("帳票を削除したい")["action_targets"], [])

    def test_inquiry_enumeration_and_definition_cues_follow_the_pack(self):
        """問い合わせ・列挙・定義の手がかりは言語資源から作る(#2181 段 4)。"""
        from rag_engine.retrieval.definition_evidence import definition_labels
        from rag_engine.retrieval.inquiry_conditions import parse_inquiry_conditions

        self.assertIsNotNone(enumeration_request("該当する条件をすべて挙げてください"))
        self.assertIsNone(enumeration_request("該当する条件を残さず挙げてください"))
        self.assertEqual(definition_labels("「区分」の意味は"), ["区分"])
        self.assertEqual(definition_labels("〈区分〉の意味は"), [])
        with bind_pack(
            _custom(
                enumeration_cues={"exhaustive_words": ["残さず"]},
                definition_cues={"definition_label_brackets": ["〈〉"]},
                inquiry_cues={"column_words": ["カラム"]},
            )
        ):
            self.assertIsNotNone(enumeration_request("該当する条件を残さず挙げてください"))
            self.assertEqual(definition_labels("〈区分〉の意味は"), ["区分"])
            self.assertEqual(parse_inquiry_conditions("B カラムの値は").column_terms, ("Bカラム",))

    def test_evidence_cues_follow_the_pack(self):
        """根拠の選び方の手がかり（集計値・機能をつなぐ語・機能番号の線）は言語資源から作る(#2181 段 4)。"""
        from types import SimpleNamespace

        from rag_engine.retrieval.evidence_scope import evidence_scope_error
        from rag_engine.retrieval.operation_context import alternative_operation_queries
        from rag_engine.retrieval.result_granularity import aggregate_only_evidence

        spans = [{"text": "件数の合計を表示する"}]
        self.assertTrue(aggregate_only_evidence(spans))
        records = [
            SimpleNamespace(metadata={"section_path": ["業務~(1)登録画面"]}),
            SimpleNamespace(metadata={"section_path": ["業務~(2)削除画面"]}),
        ]
        self.assertEqual(alternative_operation_queries("登録したい", records), ())
        linked = [
            {"text": "機能Aから機能Bへ渡る", "section_path": ["X-(1)機能A"]},
            {"text": "機能Aから機能Bへ渡る", "section_path": ["X-(2)機能B"]},
        ]
        self.assertEqual(evidence_scope_error(linked), "別機能の操作を接続する明示根拠がない")
        with bind_pack(
            _custom(
                evidence_cues={
                    "aggregate_value_words": ["総数"],
                    "link_verbs": ["渡る"],
                    "function_number_dashes": ["-", "~"],
                }
            )
        ):
            self.assertFalse(aggregate_only_evidence(spans))
            self.assertEqual(evidence_scope_error(linked), "")
            self.assertEqual(len(alternative_operation_queries("登録したい", records)), 2)

    def test_answer_labels_follow_the_pack(self):
        """回答の表示（見出し・出典・条件の型）は言語資源から作り、検査も同じ語で読む(#2181 段 5)。"""
        from rag_engine.generation.answer_labels import answer_labels
        from rag_engine.generation.grounded import _condition_phrase, is_structural_line, known_gap_labels

        self.assertTrue(is_structural_line("資料により確認できた事項"))
        self.assertTrue(is_structural_line("根拠：a.pdf p.3"))
        self.assertEqual(_condition_phrase("有効期限を過ぎた"), "有効期限を過ぎた場合")
        self.assertEqual(_condition_phrase("管理者"), "管理者の場合")
        custom = _custom(
            answer_labels={
                "rules_section": "確かめた事項",
                "citation_prefix": "出典: ",
                "page_location": "{page} 頁",
                "condition_suffix": "とき",
                "aspect_labels": ["手順=procedure"],
            }
        )
        with bind_pack(custom):
            self.assertTrue(is_structural_line("確かめた事項"))
            self.assertFalse(is_structural_line("資料により確認できた事項"))
            self.assertEqual(answer_labels().page(3), "3 頁")
            self.assertEqual(_condition_phrase("管理者"), "管理者のとき")
            self.assertEqual(known_gap_labels(["procedure"]), ["手順"])

    def test_answer_check_cues_follow_the_pack(self):
        """回答の検査の手がかり（限定・推測・版の廃止）は言語資源から作る(#2181 段 5)。"""
        from rag_engine.generation.grounded import _tag_constraint, superseded_evidence_ids

        span = {"text": "管理者に限定する。", "evidence_id": "E1"}
        self.assertEqual(_tag_constraint("", span), set())
        abolished = {"text": "第1版は改訂により廃棄されました。", "evidence_id": "E2", "source": "改訂の案内", "section_path": []}
        old = {"text": "手順", "evidence_id": "E1", "source": "第1版 手引き", "section_path": []}
        self.assertEqual(superseded_evidence_ids("手順は", [old, abolished]), set())
        with bind_pack(_custom(answer_check_cues={"constraint_words": ["に限定"], "abolish_words": ["廃棄"]})):
            self.assertEqual(_tag_constraint("", span), {"constraint"})
            self.assertIn("E1", superseded_evidence_ids("手順は", [old, abolished]))

    def test_operation_audit_cues_follow_the_pack(self):
        """操作の指示・操作の語の族・削除の対象は言語資源から作る(#2181 段 5)。"""
        from rag_engine.generation.operation_audit import deletion_targets, is_operation_instruction, missing_operation_terms

        self.assertFalse(is_operation_instruction("申請書を提出します"))
        self.assertEqual(missing_operation_terms("再交付してください", [{"text": "再発行の手順"}]), [])
        self.assertEqual(deletion_targets("帳票を破棄したい"), [])
        with bind_pack(
            _custom(
                operation_audit_cues={"instruction_phrases": ["を提出"], "operation_families": ["再交付=再交付"]},
                answer_check_cues={"deletion_words": ["破棄"]},
            )
        ):
            self.assertTrue(is_operation_instruction("申請書を提出します"))
            self.assertEqual(deletion_targets("帳票を破棄したい"), ["帳票"])

    def test_grounding_cues_follow_the_pack(self):
        """回答の値の裏付け・算式の単位は言語資源から作る(#2181 段 5)。"""
        from rag_engine.generation.applications import arithmetic_mismatch
        from rag_engine.generation.value_grounding import unsupported_document_value_claim

        claim = "入力の限度は 8 です"
        self.assertEqual(unsupported_document_value_claim(claim, [{"text": "画面の例"}], ""), "")
        self.assertEqual(arithmetic_mismatch("2兆円+3兆円=5兆円"), "")
        with bind_pack(
            _custom(
                grounding_cues={
                    "numeric_rule_words": ["限度"],
                    "amount_units": ["兆円", "円"],
                    "amount_scale_units": ["兆=9"],
                }
            )
        ):
            self.assertTrue(unsupported_document_value_claim(claim, [{"text": "画面の例"}], ""))
            self.assertEqual(arithmetic_mismatch("2兆円+3兆円=5兆円"), "")
            self.assertTrue(arithmetic_mismatch("2兆円+3兆円=5円"))

    def test_remaining_cues_follow_the_pack(self):
        """エラーの質問・根拠の ID のラベル・機能番号の線は言語資源から作る(#2181 段 5)。"""
        from rag_engine.generation.answering import _error_question

        self.assertTrue(_error_question().search("エラーが出る"))
        self.assertFalse(_error_question().search("障害が出る"))
        with bind_pack(_custom(question_cues={"error_words": ["障害"]})):
            self.assertTrue(_error_question().search("障害が出る"))


if __name__ == "__main__":
    unittest.main()
