"""approved faq の挙動を保護するテスト。"""

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from rag_engine.knowledge.approved_faq import (
    APPROVED_FAQ_IMPORT_MODE_DELETE_THEN_INSERT,
    APPROVED_FAQ_SCOPE_MODE_STRICT_WHEN_AVAILABLE,
    DEFAULT_APPROVED_FAQ_DIRECT_MATCH_MIN_SCORE,
    ApprovedFaqImportRow,
    ApprovedFaqRecord,
    add_approved_faq_record,
    apply_approved_faq_import_rows,
    approved_faq_feedback_skip_reason,
    approved_faq_import_row_from_answer_feedback,
    approved_faq_records_signature,
    approved_faq_semantic_cache_path,
    approved_faq_suggestion_choices,
    approved_faq_suggestion_labels,
    approved_faq_table_rows,
    build_approved_faq_semantic_index,
    delete_approved_faq_records,
    find_direct_approved_faq_answer,
    load_approved_faq_excel_rows,
    load_approved_faq_records,
    load_approved_faq_semantic_index,
    promote_answer_feedback_to_approved_faq,
    save_approved_faq_payload,
    select_approved_faq_suggestion,
    suggest_approved_faq_questions,
)


class ApprovedFaqTests(unittest.TestCase):
    def test_loads_records_from_dedicated_corpus(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "approved_faq_golden_qa.json"
            _write_corpus(path)

            records = load_approved_faq_records(path)

        self.assertEqual([record.id for record in records], ["faq-1", "faq-2", "faq-3"])
        self.assertEqual(records[0].alternate_questions, ("拠点名の変更手順は？",))
        self.assertEqual(records[0].classification["major"], "10_販売管理")

    def test_suggests_similar_approved_questions_with_classification_filter(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "approved_faq_golden_qa.json"
            _write_corpus(path)
            records = load_approved_faq_records(path)

        suggestions = suggest_approved_faq_questions(
            "拠点名を変更するにはどうする？",
            records,
            classification_filter={"large_category": "10_販売管理"},
        )

        self.assertEqual([suggestion.record.id for suggestion in suggestions], ["faq-1"])
        self.assertGreaterEqual(suggestions[0].score, 0.1)
        self.assertEqual(approved_faq_suggestion_choices(suggestions), [["faq-1"]])
        self.assertEqual(approved_faq_suggestion_labels(suggestions), ["拠点名を変更する方法を教えてください。 / 販売管理"])

    def test_selecting_suggestion_returns_question_and_reference(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "approved_faq_golden_qa.json"
            _write_corpus(path)
            records = load_approved_faq_records(path)

        question, reference = select_approved_faq_suggestion([["faq-1"]], records)

        self.assertEqual(question, "拠点名を変更する方法を教えてください。")
        self.assertIn("マスタ管理から変更してください。", reference)
        self.assertIn("Source: mayor.pdf p.1", reference)
        self.assertIn("Status: approved", reference)

    def test_ignores_unapproved_records_by_default(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "approved_faq_golden_qa.json"
            _write_corpus(path)
            records = load_approved_faq_records(path)

        suggestions = suggest_approved_faq_questions("未承認の質問", records)

        self.assertEqual(suggestions, [])

    def test_unclassified_approved_records_match_any_classification_filter(self):
        records = [
            ApprovedFaqRecord(
                id="faq-global",
                question="全体FAQの質問",
                approved_answer="全体FAQの回答",
                status="approved",
                classification={"major": "", "middle": "", "minor": ""},
            )
        ]

        suggestions = suggest_approved_faq_questions(
            "全体FAQの質問",
            records,
            classification_filter={"large_category": "10_販売管理"},
        )

        self.assertEqual([suggestion.record.id for suggestion in suggestions], ["faq-global"])

    def test_direct_approved_faq_answer_requires_high_similarity(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "approved_faq_golden_qa.json"
            _write_corpus(path)
            records = load_approved_faq_records(path)

        direct = find_direct_approved_faq_answer(
            "拠点名の変更手順は？",
            records,
            classification_filter={"large_category": "10_販売管理"},
        )
        unrelated = find_direct_approved_faq_answer(
            "出庫伝票の倉庫連携結果はどこで確認しますか？",
            records,
            classification_filter={"large_category": "10_販売管理"},
        )

        self.assertIsNotNone(direct)
        assert direct is not None
        self.assertEqual(direct.record.id, "faq-1")
        self.assertGreaterEqual(direct.score, DEFAULT_APPROVED_FAQ_DIRECT_MATCH_MIN_SCORE)
        self.assertIsNone(unrelated)

    def test_semantic_index_can_match_lexically_different_question(self):
        records = [
            ApprovedFaqRecord(
                id="faq-text",
                question="mayor name maintenance",
                approved_answer="Open code maintenance.",
                status="approved",
            ),
            ApprovedFaqRecord(
                id="faq-semantic",
                question="benefit payment transfer schedule",
                approved_answer="Payment is transferred on the configured benefit date.",
                status="approved",
            ),
        ]
        with TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "semantic_index.json"

            index = build_approved_faq_semantic_index(
                cache_path,
                records,
                model="test-embedding",
                dimensions=3,
                embedder=lambda texts, settings: [[0.0, 1.0, 0.0] if "mayor" in text else [1.0, 0.0, 0.0] for text in texts],
                settings=None,
            )
            suggestions = suggest_approved_faq_questions(
                "when is the allowance deposited",
                records,
                semantic_index=index,
                semantic_query_embedding=[1.0, 0.0, 0.0],
                min_score=0.92,
            )

        self.assertEqual([suggestion.record.id for suggestion in suggestions], ["faq-semantic"])
        self.assertEqual(suggestions[0].matched_question, "benefit payment transfer schedule")
        self.assertEqual(suggestions[0].match_method, "semantic")
        self.assertEqual(suggestions[0].semantic_score, 1.0)
        self.assertGreaterEqual(suggestions[0].score, DEFAULT_APPROVED_FAQ_DIRECT_MATCH_MIN_SCORE)

    def test_direct_match_requires_nearly_the_same_question_not_mere_containment(self):
        """包含を一律 0.92 にすると直答の閾値と同値になり、短い語だけで選択待ちになって RAG が止まる (#549)。"""
        faq_question = "納品書の発行手順を教えてください。"
        records = [ApprovedFaqRecord(id="faq-1", question=faq_question, approved_answer="手順です。", status="approved")]

        def direct(question):
            return [s.record.id for s in suggest_approved_faq_questions(question, records, min_score=0.92)]

        for question in ("の", "納品書", "納品書の発行手順", faq_question + "ただし再発行の場合です"):
            with self.subTest(question):
                self.assertEqual(direct(question), [])
                # 入力中の候補提示（低い閾値）には引き続き出る。
                self.assertEqual([s.record.id for s in suggest_approved_faq_questions(question, records, min_score=0.1)], ["faq-1"])
        for question in (
            faq_question,
            "納品書の発行手順を教えてください？",
            "納品書の発行手順を教えてください",
            " 納品書の発行手順を 教えてください。",
        ):
            with self.subTest(question):
                self.assertEqual(direct(question), ["faq-1"])

    def test_strict_scope_filters_to_current_source_when_available(self):
        records = [
            ApprovedFaqRecord(
                id="faq-other-file",
                question="拠点名を変更する方法を教えてください。",
                approved_answer="別ファイルの回答です。",
                status="approved",
                source={"file_name": "other.pdf"},
            ),
            ApprovedFaqRecord(
                id="faq-current-file",
                question="拠点名を変更する方法を教えてください。",
                approved_answer="現在ファイルの回答です。",
                status="approved",
                source={"file_name": "mayor.pdf"},
            ),
        ]

        suggestions = suggest_approved_faq_questions(
            "拠点名を変更する方法を教えてください。",
            records,
            scope={"source_file_name": "mayor.pdf"},
            scope_mode=APPROVED_FAQ_SCOPE_MODE_STRICT_WHEN_AVAILABLE,
            min_score=0.92,
        )

        self.assertEqual([suggestion.record.id for suggestion in suggestions], ["faq-current-file"])
        self.assertEqual(suggestions[0].scope_matches, ("source_file_name",))

    def test_semantic_index_cache_roundtrips_with_record_signature(self):
        records = [
            ApprovedFaqRecord(
                id="faq-1",
                question="QUESTION one",
                approved_answer="ANSWER one",
                alternate_questions=("QUESTION alias",),
                status="approved",
            )
        ]
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "runs"
            cache_path = approved_faq_semantic_cache_path(output_dir, faq_path=Path(tmp) / "faq.json")
            index = build_approved_faq_semantic_index(
                cache_path,
                records,
                model="test-embedding",
                dimensions=3,
                embedder=lambda texts, settings: [[1.0, 0.0, 0.0] for _ in texts],
                settings=None,
            )
            loaded = load_approved_faq_semantic_index(
                cache_path,
                model="test-embedding",
                dimensions=3,
                records_signature=approved_faq_records_signature(records),
            )
            wrong_model = load_approved_faq_semantic_index(
                cache_path,
                model="other-model",
                dimensions=3,
                records_signature=approved_faq_records_signature(records),
            )

            self.assertTrue(cache_path.exists())
            self.assertEqual(index.records_signature, approved_faq_records_signature(records))
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(len(loaded.items), 2)
            self.assertIsNone(wrong_model)

    def test_adds_approved_faq_record_and_skips_duplicate_question(self):
        now = datetime(2026, 9, 8, 9, 0, tzinfo=UTC)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "approved_faq_golden_qa.json"
            _write_corpus(path)

            result = add_approved_faq_record(
                path,
                question=" 新しいFAQを追加できますか？ ",
                approved_answer="追加できます。",
                now=now,
            )
            duplicate = add_approved_faq_record(
                path,
                question="新しいFAQを追加できますか？",
                approved_answer="上書きしません。",
                now=now,
            )
            records = load_approved_faq_records(path)
            table_rows = approved_faq_table_rows(path)

        self.assertEqual(result.inserted_count, 1)
        self.assertEqual(result.deleted_count, 0)
        self.assertIsNotNone(result.backup_path)
        self.assertEqual(duplicate.inserted_count, 0)
        self.assertEqual(duplicate.skipped_count, 1)
        self.assertEqual(records[-1].question, "新しいFAQを追加できますか？")
        self.assertEqual(records[-1].approved_answer, "追加できます。")
        self.assertEqual(records[-1].status, "approved")
        self.assertEqual(records[-1].source["source_file"], "manual")
        self.assertEqual(len(table_rows[-1]), 4)
        self.assertEqual(table_rows[-1][1], "新しいFAQを追加できますか？")
        self.assertNotIn(records[0].business, table_rows[0])

    def test_deletes_approved_faq_records_by_id(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "approved_faq_golden_qa.json"
            _write_corpus(path)

            result = delete_approved_faq_records(
                path,
                " faq-1\nfaq-2,faq-1 ",
                now=datetime(2026, 9, 8, 9, 5, tzinfo=UTC),
            )
            records = load_approved_faq_records(path)

        self.assertEqual(result.deleted_count, 2)
        self.assertEqual([record.id for record in records], ["faq-3"])

    def test_delete_ignores_questions_unknown_ids_and_empty_input(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "approved_faq_golden_qa.json"
            _write_corpus(path)
            original = path.read_bytes()
            for selectors in ("拠点名を変更する方法を教えてください。", "unknown-id", " "):
                with self.subTest(selectors=selectors):
                    result = delete_approved_faq_records(path, selectors)
                    self.assertEqual(result.deleted_count, 0)
                    self.assertIsNone(result.backup_path)
                    self.assertEqual(path.read_bytes(), original)

    def test_delete_does_not_match_another_records_question_to_selected_id(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "approved_faq_golden_qa.json"
            _write_corpus(path)
            payload = json.loads(path.read_text())
            payload["records"][1]["question"] = "faq-1"
            path.write_text(json.dumps(payload))

            result = delete_approved_faq_records(path, ["faq-1"])

            self.assertEqual(result.deleted_count, 1)
            self.assertEqual([record.id for record in load_approved_faq_records(path)], ["faq-2", "faq-3"])
            self.assertIsNotNone(result.backup_path)

    def test_delete_then_insert_replaces_matching_question_from_import_rows(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "approved_faq_golden_qa.json"
            _write_corpus(path)

            result = apply_approved_faq_import_rows(
                path,
                [
                    ApprovedFaqImportRow(
                        question="拠点名を変更する方法を教えてください。",
                        approved_answer="新しい回答です。",
                        source_file="faq.xlsx",
                        sheet="FAQ",
                        row=2,
                    )
                ],
                mode=APPROVED_FAQ_IMPORT_MODE_DELETE_THEN_INSERT,
                now=datetime(2026, 9, 8, 9, 10, tzinfo=UTC),
            )
            records = load_approved_faq_records(path)

        self.assertEqual(result.deleted_count, 1)
        self.assertEqual(result.inserted_count, 1)
        replaced = next(record for record in records if record.question == "拠点名を変更する方法を教えてください。")
        self.assertEqual(replaced.approved_answer, "新しい回答です。")
        self.assertEqual(replaced.source["source_file"], "faq.xlsx")
        self.assertEqual(replaced.source["sheet"], "FAQ")
        self.assertEqual(replaced.source["row"], 2)

    def test_loads_question_answer_rows_from_excel(self):
        from openpyxl import Workbook

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "faq.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "FAQ"
            sheet.append(["QUESTION", "ANSWER", "NOTE"])
            sheet.append(["質問1", "回答1", "memo"])
            sheet.append(["", "", "empty"])
            sheet.append(["質問2", "回答2", "memo"])
            workbook.save(path)

            rows = load_approved_faq_excel_rows(path)

        self.assertEqual([row.question for row in rows], ["質問1", "質問2"])
        self.assertEqual([row.approved_answer for row in rows], ["回答1", "回答2"])
        self.assertEqual(rows[0].source_file, "faq.xlsx")
        self.assertEqual(rows[0].sheet, "FAQ")
        self.assertEqual(rows[0].row, 2)

    def test_promotes_corrected_answer_feedback_to_approved_faq(self):
        now = datetime(2026, 9, 8, 9, 20, tzinfo=UTC)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "approved_faq_golden_qa.json"
            _write_corpus(path)
            feedback = {
                "feedback_id": "feedback-1",
                "feedback_type": "incorrect_answer",
                "run_id": "run123",
                "answer_id": "answer456",
                "question": "拠点名を変更する方法を教えてください。",
                "comment": "更新済みの手順に直す。",
                "corrected_answer": "拠点倉庫名欄を更新し、実行ボタンを押してください。",
                "classification_filter": {
                    "large_category": "10_販売管理",
                    "middle_category": "40_Q&A",
                    "small_category": "拠点名",
                },
                "answer_trace": {
                    "answer_text": "古い回答です。",
                    "confidence": "low",
                    "answer_payload_path": "/tmp/run123/answers/answer456.json",
                    "evidence_item_ids": ["chunk-1", "chunk-2"],
                },
            }

            result = promote_answer_feedback_to_approved_faq(path, feedback, now=now)
            records = load_approved_faq_records(path)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.deleted_count, 1)
        self.assertEqual(result.inserted_count, 1)
        promoted = next(record for record in records if record.question == "拠点名を変更する方法を教えてください。")
        self.assertEqual(promoted.approved_answer, "拠点倉庫名欄を更新し、実行ボタンを押してください。")
        self.assertEqual(promoted.classification["major"], "10_販売管理")
        self.assertEqual(promoted.classification["middle"], "40_Q&A")
        self.assertEqual(promoted.classification["minor"], "拠点名")
        self.assertEqual(promoted.source["source_file"], "answer_feedback")
        self.assertEqual(promoted.source["feedback_id"], "feedback-1")
        self.assertEqual(promoted.source["evidence_item_ids"], ["chunk-1", "chunk-2"])
        self.assertIn("更新済みの手順に直す。", promoted.review["review_notes"])
        self.assertIn("knowledge_return", promoted.tags)

    def test_concurrent_additions_are_all_saved(self):
        import threading

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "approved_faq.json"
            barrier = threading.Barrier(8)
            errors = []

            def add(index):
                barrier.wait()
                try:
                    add_approved_faq_record(path, question=f"質問{index}", approved_answer=f"回答{index}")
                except Exception as exc:  # スレッド内の失敗を主スレッドの assert へ渡す。
                    errors.append(exc)

            threads = [threading.Thread(target=add, args=(index,)) for index in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            questions = {record.question for record in load_approved_faq_records(path)}
            leftovers = [item.name for item in Path(tmp).iterdir() if item.name.endswith(".tmp")]
        self.assertEqual(errors, [])
        # ロックがないと、同じ内容を読み込んだ保存同士が互いの追加を上書きして消す。
        self.assertEqual(questions, {f"質問{index}" for index in range(8)})
        self.assertEqual(leftovers, [])

    def test_feedback_replacement_inherits_curated_fields_of_the_existing_record(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "approved_faq.json"
            save_approved_faq_payload(
                {
                    "records": [
                        {
                            "id": "approved-faq-curated",
                            "question": "申請期限は？",
                            "alternate_questions": ["締切はいつ？"],
                            "approved_answer": "旧回答",
                            "status": "approved",
                            "business": "経理",
                            "classification": {"major": "規程"},
                            "tags": ["curated", "correct"],
                            "source": {"source_run_id": "run-real", "source_file_name": "rule.pdf"},
                            "timestamps": {"created_at": "2026-01-01T00:00:00+00:00"},
                        }
                    ]
                },
                path=path,
            )
            promote_answer_feedback_to_approved_faq(
                path,
                {
                    "feedback_id": "fb-fix",
                    "feedback_type": "incorrect_answer",
                    "run_id": "run-x",
                    "answer_id": "a1",
                    "question": "申請期限は？",
                    "corrected_answer": "月末です。",
                    "answer_trace": {},
                },
            )
            (record,) = load_approved_faq_records(path)
        self.assertEqual(record.approved_answer, "月末です。")
        self.assertEqual(record.alternate_questions, ("締切はいつ？",))
        self.assertEqual(record.business, "経理")
        self.assertEqual(record.classification["major"], "規程")
        self.assertEqual(record.source["source_run_id"], "run-real")
        self.assertEqual(record.source["source_file_name"], "rule.pdf")
        self.assertEqual(record.source["feedback_id"], "fb-fix")
        # 旧 feedback 種別のタグは残さず、今回の種別に置き換える。
        self.assertEqual(record.tags, ("curated", "knowledge_return", "answer_feedback", "incorrect_answer"))

    def test_correct_feedback_skips_faq_direct_and_unanswerable_answers(self):
        base = {"feedback_id": "fb", "run_id": "run-x", "answer_id": "a1", "question": "質問"}
        faq_direct = {
            **base,
            "feedback_type": "correct",
            "answer_trace": {"answer_text": "承認済み回答", "retrieval_scope": "approved_faq"},
        }
        unanswerable = {
            **base,
            "feedback_type": "correct",
            "answer_trace": {"answer_text": "回答できません。", "confidence": "low", "insufficient_reason": "根拠不足"},
        }
        partial = {
            **base,
            "feedback_type": "correct",
            "answer_trace": {"answer_text": "一部の回答", "confidence": "medium", "insufficient_reason": "残りは不明"},
        }
        self.assertIn("再登録は不要", approved_faq_feedback_skip_reason(faq_direct))
        self.assertIn("根拠不足", approved_faq_feedback_skip_reason(unanswerable))
        self.assertEqual(approved_faq_feedback_skip_reason(partial), "")
        self.assertIsNone(approved_faq_import_row_from_answer_feedback(faq_direct))
        self.assertIsNone(approved_faq_import_row_from_answer_feedback(unanswerable))
        # FAQ 直接回答を修正する場合、approved-faq-* の保存先を出典 run として記録しない。
        corrected = approved_faq_import_row_from_answer_feedback(
            {**faq_direct, "run_id": "approved-faq-0123", "feedback_type": "incorrect_answer", "corrected_answer": "修正"}
        )
        self.assertNotIn("run_id", corrected.source)
        self.assertEqual(corrected.source["answer_run_id"], "approved-faq-0123")

    def test_knowledge_base_feedback_is_promoted_to_the_evidence_document_not_the_open_file(self):
        """Knowledge Base 検索では、回答の保存先（UI で開いていたファイル）と根拠文書が異なる (#545)。"""

        def feedback(scope):
            return {
                "feedback_id": "feedback-kb",
                "feedback_type": "correct",
                "run_id": "run-open-file-x",
                "answer_id": "answer-kb",
                "question": "拠点名を変更する方法を教えてください。",
                "answer_trace": {"answer_text": "文書 Y の手順です。", "retrieval_scope": scope, "primary_source_run_id": "run-evidence-y"},
            }

        row = approved_faq_import_row_from_answer_feedback(feedback("knowledge_base"))
        self.assertEqual(row.source["source_run_id"], "run-evidence-y")
        self.assertEqual(row.source["answer_run_id"], "run-open-file-x")
        self.assertNotIn("run_id", row.source)  # run_id は出典として照合されるため、保存先を入れない

        promoted = ApprovedFaqRecord(
            id="faq-from-kb", question=row.question, approved_answer=row.approved_answer, status="approved", source=row.source
        )
        matched = {}
        for open_run in ("run-open-file-x", "run-evidence-y"):
            suggestions = suggest_approved_faq_questions(
                row.question,
                [promoted],
                scope={"source_run_id": open_run},
                scope_mode=APPROVED_FAQ_SCOPE_MODE_STRICT_WHEN_AVAILABLE,
                min_score=0.92,
            )
            matched[open_run] = [suggestion.scope_matches for suggestion in suggestions]
        self.assertEqual(matched, {"run-open-file-x": [()], "run-evidence-y": [("source_run_id",)]})

        # 現在のファイルを検索した回答は、保存先の run がそのまま出典になる。
        current = approved_faq_import_row_from_answer_feedback(feedback("current_chunk_run"))
        self.assertEqual(current.source["run_id"], "run-open-file-x")
        self.assertNotIn("source_run_id", current.source)

    def test_promotes_correct_feedback_with_generated_answer_and_skips_empty_wrong_feedback(self):
        now = datetime(2026, 9, 8, 9, 25, tzinfo=UTC)
        correct_feedback = {
            "feedback_id": "feedback-2",
            "feedback_type": "correct",
            "run_id": "run123",
            "answer_id": "answer456",
            "question": "新しい質問ですか？",
            "corrected_answer": "前の回答の修正値（正しい評価では使わない）",
            "answer_trace": {
                "answer_text": "生成回答をそのまま FAQ にします。",
                "confidence": "high",
            },
        }
        wrong_without_answer = {
            "feedback_id": "feedback-3",
            "feedback_type": "incorrect_answer",
            "question": "回答が空ですか？",
            "corrected_answer": "",
            "answer_trace": {"answer_text": "使わない回答です。"},
        }

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "approved_faq_golden_qa.json"
            _write_corpus(path)
            result = promote_answer_feedback_to_approved_faq(path, correct_feedback, now=now)
            skipped_row = approved_faq_import_row_from_answer_feedback(wrong_without_answer)
            skipped_result = promote_answer_feedback_to_approved_faq(path, wrong_without_answer, now=now)
            records = load_approved_faq_records(path)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.inserted_count, 1)
        promoted = next(record for record in records if record.question == "新しい質問ですか？")
        self.assertEqual(promoted.approved_answer, "生成回答をそのまま FAQ にします。")
        self.assertEqual(promoted.review["confidence"], "high")
        self.assertIsNone(skipped_row)
        self.assertIsNone(skipped_result)


def _write_corpus(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "records": [
                    {
                        "id": "faq-1",
                        "question": "拠点名を変更する方法を教えてください。",
                        "alternate_questions": ["拠点名の変更手順は？"],
                        "approved_answer": "マスタ管理から変更してください。",
                        "status": "approved",
                        "business": "販売管理",
                        "classification": {
                            "major": "10_販売管理",
                            "middle": "40_Q&A",
                            "minor": "拠点名",
                        },
                        "source": {
                            "file_name": "mayor.pdf",
                            "page": 1,
                        },
                        "timestamps": {
                            "updated_at": "2026-09-08T00:00:00Z",
                        },
                    },
                    {
                        "id": "faq-2",
                        "question": "会員証の出力方法を教えてください。",
                        "approved_answer": "帳票出力から実行してください。",
                        "status": "approved",
                        "business": "在庫管理",
                        "classification": {
                            "major": "20_在庫管理",
                            "middle": "40_Q&A",
                            "minor": "会員証",
                        },
                    },
                    {
                        "id": "faq-3",
                        "question": "未承認の質問",
                        "approved_answer": "まだ使わない回答です。",
                        "status": "draft",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()


def test_semantic_index_cache_is_written_atomically(tmp_path):
    """cache は一時ファイル → replace で書き、途中の状態や一時ファイルを残さない (#828)。"""
    from rag_engine.knowledge.approved_faq import (
        ApprovedFaqSemanticIndex,
        ApprovedFaqSemanticItem,
        load_approved_faq_semantic_index,
        save_approved_faq_semantic_index,
    )

    index = ApprovedFaqSemanticIndex(
        model="m", dimensions=2, records_signature="sig", items=(ApprovedFaqSemanticItem("r1", "質問", "h1", (0.1, 0.2)),)
    )
    cache = tmp_path / "cache" / "semantic.json"
    save_approved_faq_semantic_index(cache, index)
    assert [p.name for p in cache.parent.iterdir()] == ["semantic.json"]
    loaded = load_approved_faq_semantic_index(cache, model="m", dimensions=2, records_signature="sig")
    assert loaded is not None and loaded.items[0].record_id == "r1"
