"""inquiry conditions の挙動を保護するテスト。"""

import unittest

from rag_engine.adapters.oracle.store import StoredChunk
from rag_engine.chunking import CHILD_CHUNK_LEVEL
from rag_engine.retrieval.inquiry_conditions import (
    CONDITION_PROFILE,
    FILE_DATA_PROFILE,
    OPERATION_PROFILE,
    build_inquiry_chunk_metadata,
    inquiry_profile_contract_hash,
    parse_inquiry_conditions,
    profile_channel_rankings,
)


class InquiryConditionsTests(unittest.TestCase):
    def test_only_injected_business_domains_are_used(self):
        """業務名は検索範囲の分類を照合した結果だけを使う。"""
        from rag_engine.dependencies import AnswerDependencies, bind_dependencies

        def unused(*args, **kwargs):
            raise AssertionError("I/O は呼ばない")

        def bound(matcher):
            return bind_dependencies(
                AnswerDependencies(
                    search=unused,
                    check_ready=unused,
                    parse_text=unused,
                    parse_images=unused,
                    rerank=unused,
                    business_domains=matcher,
                )
            )

        question = "販管の業務Xで拠点名を変更する方法は？"
        self.assertEqual(parse_inquiry_conditions(question).business_domains, ())
        with bound(lambda text: ["10_業務X", "10_業務X"] if "業務X" in text else []):
            parsed = parse_inquiry_conditions(question)
        self.assertEqual(parsed.business_domains, ("10_業務X",))
        self.assertEqual(parsed.metadata_filter.metadata_terms, ("10_業務X",))
        with bound(lambda text: []):
            self.assertEqual(parse_inquiry_conditions(question).business_domains, ())

    def test_parse_rename_question_keeps_operation_without_external_aliases(self):
        parsed = parse_inquiry_conditions("倉庫を移転したので、拠点名を変更する方法を教えてほしい。")

        self.assertIn("procedure", parsed.decision_types)
        self.assertIn(OPERATION_PROFILE, parsed.active_profiles)
        self.assertNotIn("拠点倉庫名", parsed.search_terms)
        self.assertNotIn("倉庫マスタ登録", parsed.search_terms)

    def test_parse_file_data_question_extracts_file_code_column_and_external_context(self):
        parsed = parse_inquiry_conditions("外部システムから取込した result.csv で W1114 が出た。EC列を確認する必要がありますか。")

        self.assertTrue(parsed.requires_file_data_confirmation)
        self.assertTrue(parsed.requires_external_context)
        self.assertTrue(parsed.requires_condition_answer)
        self.assertIn(FILE_DATA_PROFILE, parsed.active_profiles)
        self.assertIn(CONDITION_PROFILE, parsed.active_profiles)
        self.assertEqual(parsed.file_terms, ("result.csv",))
        self.assertIn("W1114", parsed.codes_and_errors)
        self.assertIn("EC列", parsed.column_terms)
        self.assertTrue(parsed.metadata_filter.active)
        self.assertEqual(parsed.metadata_filter.source_file_terms, ())

    def test_error_codes_are_uppercase_only(self):
        # xlsx2023 / csv2024 / ver100 はファイル種別・版表記で、エラーコードではない (#826)。
        parsed = parse_inquiry_conditions("xlsx2023 形式と csv2024 を ver100 で取り込むと E1234 と ORA-00904 が出る。")
        self.assertEqual(parsed.codes_and_errors, ("E1234", "ORA-00904"))

    def test_page_numbers_require_source_file_name(self):
        # ファイル名 + ページ指定は検索条件になり、ファイル名のないページ数の記述は条件にしない（#601）
        parsed = parse_inquiry_conditions("info_pdf_list.pdf の２ページ目と p.4 には何が書かれていますか？")
        self.assertEqual(parsed.metadata_filter.source_file_terms, ("info_pdf_list.pdf",))
        self.assertEqual(parsed.metadata_filter.page_numbers, (2, 4))
        self.assertIn("ページ: p.2, p.4", parsed.display_lines())
        self.assertEqual(parsed.metadata_filter.to_payload()["page_numbers"], [2, 4])

        parsed = parse_inquiry_conditions("申込書は2ページありますか。P2P の記載も教えてください。")
        self.assertEqual(parsed.metadata_filter.page_numbers, ())

    def test_display_lines_use_formal_query_understanding_labels(self):
        parsed = parse_inquiry_conditions("外部システムから取込した result.csv で W1114 が出た。EC列を確認する必要がありますか。")

        display = "\n".join(parsed.display_lines())

        self.assertIn("処理: Fine-grained Query Understanding", display)
        self.assertIn("NLUタスク: Intent Classification / Slot Filling", display)
        self.assertIn("文書種別:", display)
        self.assertIn("Intent: 要否", display)
        self.assertIn("ファイル:", display)
        self.assertIn("コード/エラー:", display)
        self.assertIn("検索語:", display)
        self.assertIn("プロファイル: 条件・例外, ファイル/データ確認", display)
        self.assertIn("外部コンテキスト: 可能性あり", display)
        self.assertNotIn("necessity", display)
        self.assertNotIn("conditions_and_exceptions", display)
        self.assertNotIn("file_data_confirmation", display)

    def test_parse_parameter_question_keeps_name_value_pair_and_aliases(self):
        parsed = parse_inquiry_conditions(
            "送料無料キャンペーンの受注だけ数量割引額が計算されないのはなぜか。DiscQtyRule,03 は関係しますか。"
        )

        self.assertIn({"name": "DiscQtyRule", "value": "03"}, parsed.parameter_refs)
        self.assertNotIn("ボリュームディスカウント", parsed.search_terms)
        self.assertIn("DiscQtyRule 03", parsed.search_terms)
        self.assertTrue(parsed.requires_condition_answer)

    def test_generic_processing_does_not_invent_external_aliases(self):
        parsed = parse_inquiry_conditions("項目Aの設定を変更する方法を教えてください。")
        self.assertIn("procedure", parsed.decision_types)
        self.assertNotIn("外部ファイルだけの別名", parsed.search_terms)

    def test_chunk_metadata_records_only_compact_filter_and_profile_signals(self):
        metadata = build_inquiry_chunk_metadata(
            text="条件と結果: 拠点倉庫名欄が空白の場合は設定不要です。",
            retrieval_text="販売管理 操作説明書 拠点倉庫名 条件 不要",
            source_file_name="販売管理操作説明書.pdf",
            source_categories=["Text"],
            page_start=4,
            page_end=4,
        )

        self.assertNotIn("business_domains", metadata)
        self.assertIn("操作説明書", metadata["document_kinds"])
        self.assertIn(CONDITION_PROFILE, metadata["active_profiles"])
        self.assertEqual(
            set(metadata),
            {"document_kinds", "active_profiles"},
        )
        self.assertRegex(inquiry_profile_contract_hash(), r"^[0-9a-f]{64}$")

    def test_profile_channel_rankings_weight_typed_child_hits(self):
        parsed = parse_inquiry_conditions("拠点名を変更する方法を教えてください。")
        chunk = StoredChunk(
            chunk_uid="run:chunk-docling-c000001",
            chunk_id="chunk-docling-c000001",
            chunk_level=CHILD_CHUNK_LEVEL,
            chunk_seq=1,
            parent_chunk_uid="",
            parent_chunk_id="",
            child_chunk_ids=(),
            text="倉庫マスタ登録画面で拠点倉庫名を変更し、実行します。",
            retrieval_text="倉庫マスタ登録画面 拠点倉庫名 変更 実行",
            source_run_id="abcdef",
            source_file_name="manual.pdf",
            source_engine_id="docling",
            source_engine_label="Docling",
            page_start=1,
            page_end=1,
            source_seq_ranges=(),
            source_record_refs=(),
            metadata={
                "retrieval_profile": {
                    "active_profiles": [OPERATION_PROFILE],
                }
            },
        )

        rankings = profile_channel_rankings([chunk], parsed, limit=5)

        self.assertEqual(rankings[0][0], f"profile:{OPERATION_PROFILE}")
        self.assertEqual(rankings[0][2][0][0], "run:chunk-docling-c000001")
        self.assertGreater(rankings[0][2][0][1], 0)


if __name__ == "__main__":
    unittest.main()


class ActiveChunkProfileTests(unittest.TestCase):
    def test_operation_profile_depends_only_on_trigger_terms(self):
        # Picture / Section-header の有無では変わらない（旧分岐は恒等式だった #885）。
        from rag_engine.retrieval.inquiry_conditions import _PROFILE_BY_ID, _active_chunk_profiles, _trigger_terms

        term = _trigger_terms(_PROFILE_BY_ID[OPERATION_PROFILE])[0]
        self.assertEqual(_active_chunk_profiles("説明図です。", ["Picture", "Section-header"]), [])
        self.assertIn(OPERATION_PROFILE, _active_chunk_profiles(f"{term}を確認します。", ["Text"]))
