"""文書IDの安定性、未確認値の扱い、検証、旧項目の無視と保存経路の回帰テスト。"""

import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from test_adb_vector_store import _CaptureConnection
from test_analysis_full_file import _Adapter, _analysis_patches

from rag_engine.adapters.oracle.store import _chunk_row, _upsert_document
from rag_engine.chunking import create_chunk_run, load_chunk_run_by_id
from rag_engine.config import get_settings
from rag_engine.knowledge.document_metadata import normalize_document_metadata
from rag_engine.parsing.analysis import analyze_pdf, preview_pdf
from rag_engine.retrieval.context_builder import context_bundle_from_records


class DocumentMetadataTests(unittest.TestCase):
    def test_identity_uses_source_not_revision_or_content(self):
        first = normalize_document_metadata({"effective_from": "2025-04-01"}, source_file_name="manual.pdf")
        revised = normalize_document_metadata({"effective_from": "2026-04-01"}, source_file_name="manual.pdf")
        self.assertEqual(first["source_document_id"], revised["source_document_id"])
        self.assertEqual(first["identity_source"], "local_filename")
        renamed = normalize_document_metadata(first, source_file_name="renamed.pdf")
        self.assertEqual(first["source_document_id"], renamed["source_document_id"])
        uri = {"source_uri": "https://example.org/document?id=12"}
        self.assertEqual(
            normalize_document_metadata(uri, source_file_name="a.pdf")["source_document_id"],
            normalize_document_metadata(uri, source_file_name="b.pdf")["source_document_id"],
        )
        self.assertEqual(
            normalize_document_metadata({"source_uri": uri["source_uri"] + "#page=2"}, source_file_name="a.pdf")["source_document_id"],
            normalize_document_metadata(uri, source_file_name="a.pdf")["source_document_id"],
        )
        self.assertNotEqual(
            first["source_document_id"], normalize_document_metadata(None, source_file_name="other.pdf")["source_document_id"]
        )
        self.assertEqual(
            normalize_document_metadata({"source_document_id": "D-42"}, source_file_name="new.pdf")["identity_source"], "explicit"
        )

    def test_unknown_values_are_absent_and_input_is_not_mutated(self):
        original = {"effective_from": "", "source_uri": None}
        result = normalize_document_metadata(original, source_file_name="manual.pdf")
        self.assertEqual(original, {"effective_from": "", "source_uri": None})
        self.assertEqual(set(result), {"schema_version", "source_document_id", "identity_source"})

    def test_legacy_fields_from_saved_metadata_are_dropped_silently(self):
        # 旧版で保存した title / document_version / published_at / source_updated_at は読めるが保持しない (#893)。
        saved = {
            "source_document_id": "D1",
            "title": "旧タイトル",
            "document_version": "2",
            "published_at": "2026-01-01",
            "source_updated_at": "2026-01-02",
            "effective_from": "2026-04-01",
        }
        result = normalize_document_metadata(saved, source_file_name="manual.pdf")
        self.assertEqual(set(result), {"schema_version", "source_document_id", "identity_source", "effective_from"})

    def test_invalid_dates_urls_and_unknown_contract_are_rejected(self):
        invalid = [
            {"effective_from": "2026-02-30"},
            {"effective_to": "20260910"},
            {"effective_from": "2026-09-10", "effective_to": "2026-09-09"},
            {"effective_from": "2026-09-10", "effective_to": "2026-09-10"},
            {"source_uri": "javascript:alert(1)"},
            {"source_uri": "file:///tmp/source.pdf"},
            {"source_uri": "https://user:password@example.org/doc"},
            {"source_uri": "https://example.org/doc?X-Amz-Signature=synthetic"},
            {"source_uri": "https://example.org:invalid/doc"},
            {"source_document_id": "x\nheader"},
            {"effective_from": 12},
            {"schema_version": 99},
            {"allowed_group_ids": ["a"]},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_document_metadata(value, source_file_name="manual.pdf")

    def test_analysis_restore_chunks_and_adb_preserve_document_metadata(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "manual.pdf"
            source.write_bytes(b"synthetic source")
            settings = replace(get_settings(), output_dir=Path(tmp) / "runs")
            info = {
                "source_document_id": "manual-42",
                "effective_from": "2026-09-01",
                "effective_to": "2027-04-01",
                "source_uri": "https://example.org/manual",
            }
            with _analysis_patches(_Adapter("docling"), page_count=2):
                run = analyze_pdf(source, "all", ["docling"], settings, parse_entire_file=True, document_metadata=info)
                restored = preview_pdf(source, settings)
            self.assertEqual(restored.document_metadata, run.document_metadata)
            self.assertFalse(hasattr(run, "extraction_quality"))
            result = create_chunk_run(output_dir=settings.output_dir, run_id=restored.run_id, preferred_engine_ids=["docling"])
            loaded = load_chunk_run_by_id(settings.output_dir, result.chunk_run_id)
            empty_classification = {"large_category": "", "middle_category": "", "small_category": ""}
            expected_document = {
                **run.document_metadata,
                "classification": empty_classification,
                "first_page_context": {
                    "page": 1,
                    "status": "available",
                    "text": "page 1",
                    "engine": "docling",
                    "record_ids": ["docling-p1-1"],
                    "truncated": False,
                },
            }
            self.assertEqual(loaded.document_metadata, expected_document)
            for chunk in loaded.chunks:
                self.assertEqual(chunk.metadata["document"], expected_document)
                row = _chunk_row(loaded, "doc-id", chunk)
                # 第1ページ背景は chunk 行へ複製せず、文書ごとに1回だけ保存する。
                self.assertEqual(
                    json.loads(row["metadata_json"])["document"], {**run.document_metadata, "classification": empty_classification}
                )
                self.assertEqual(chunk.metadata["document"], expected_document)  # 入力は変更しない
                if chunk.chunk_level == "child":
                    # 有効期間は検索 filter と回答 context だけで使い、embedding 入力には混ぜない (#893)。
                    self.assertNotIn("2026-09-01", chunk.retrieval_text)
                    self.assertNotIn("Document:", chunk.retrieval_text)
                    self.assertNotIn("manual-42", chunk.retrieval_text)
            connection = _CaptureConnection()
            _upsert_document(connection, loaded, "doc-id")
            self.assertEqual(json.loads(connection.cursor_obj.binds["metadata_json"])["document"], expected_document)

    def test_answer_context_keeps_effective_window_and_source_without_technical_id(self):
        record = SimpleNamespace(
            id="e1",
            text="本文",
            citation="p.1",
            engine_label="Docling",
            category="Text",
            metadata={
                "document": {
                    "effective_from": "2026-09-01",
                    "effective_to": "2027-04-01",
                    "source_uri": "https://example.org/manual",
                    "source_document_id": "private-id",
                }
            },
        )
        bundle = context_bundle_from_records([record], max_chars=2000)
        self.assertIn("Effective from: 2026-09-01", bundle.text)
        self.assertIn("Effective until (exclusive): 2027-04-01", bundle.text)
        self.assertIn("https://example.org/manual", bundle.text)
        self.assertNotIn("private-id", bundle.text)
        self.assertIn("本文", bundle.text)

    def test_invalid_input_is_rejected_before_analysis_side_effects(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.pdf"
            source.write_bytes(b"synthetic")
            settings = replace(get_settings(), output_dir=Path(tmp) / "runs")
            with patch("rag_engine.parsing.analysis.build_adapters") as adapters, self.assertRaises(ValueError):
                analyze_pdf(source, "all", ["docling"], settings, document_metadata={"effective_from": "unknown"})
            adapters.assert_not_called()
            self.assertFalse(settings.output_dir.exists())
