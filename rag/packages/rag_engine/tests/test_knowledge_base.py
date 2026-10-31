"""knowledge base の挙動を保護するテスト。"""

import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from rag_engine.adapters.oracle.store import EmbeddingSaveResult
from rag_engine.chunking import ChunkingConfig, ChunkingResult
from rag_engine.config import get_settings
from rag_engine.knowledge.classification import classification_from_selection
from rag_engine.workflows.knowledge_base import format_knowledge_registration_result, register_source_file_for_rag


class KnowledgeBaseRegistrationTests(unittest.TestCase):
    def test_register_source_file_runs_parse_chunk_and_embedding_pipeline(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "share" / "20260101_20_在庫管理" / "20_在庫管理" / "20_操作説明書" / "6_倉庫連携_41.pdf"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"%PDF-1.4\n")
            settings = replace(get_settings(), output_dir=Path(tmp) / "runs")
            classification = classification_from_selection(source_path=source, source_root=Path(tmp) / "share")
            chunk_result = ChunkingResult(
                source_run_id="run123",
                chunk_run_id="chunk123",
                source_file_name=source.name,
                selected_engine_ids=["docling"],
                config=ChunkingConfig(),
                config_hash="hash",
                created_at_utc="2026-09-04T00:00:00+00:00",
                active=True,
                source_file_sha256="0" * 64,
                source_page_count=29,
                chunks=[],
                json_path="",
                jsonl_path="",
                latest_path="",
                classification=classification.to_metadata(),
            )
            embedding_result = EmbeddingSaveResult(
                source_run_id="run123",
                chunk_run_id="chunk123",
                document_id="doc",
                child_count=1,
                parent_count=1,
                created_count=1,
                skipped_count=0,
                error_count=0,
            )

            with (
                patch(
                    "rag_engine.workflows.knowledge_base.analyze_pdf",
                    return_value=_run(
                        [SimpleNamespace(raw={"vision_status": "failed"}), SimpleNamespace(raw={"vision_status": "succeeded"})],
                    ),
                ) as analyze,
                patch("rag_engine.workflows.knowledge_base.create_chunk_run", return_value=chunk_result) as chunk,
                patch("rag_engine.workflows.knowledge_base.save_chunk_run_embeddings", return_value=embedding_result) as embed,
            ):
                result = register_source_file_for_rag(
                    source_path=source,
                    engine_ids=["docling"],
                    settings=settings,
                    classification=classification,
                    chunking_config=ChunkingConfig(child_target_chars=900),
                    min_confidence=0.5,
                    dpi=200,
                    use_docling_vision=True,
                )

        self.assertTrue(result.ok)
        self.assertEqual(result.classification["small_category"], "倉庫連携")
        self.assertTrue(analyze.call_args.kwargs["parse_entire_file"])
        self.assertEqual(analyze.call_args.kwargs["page_range"], "all")
        self.assertEqual(analyze.call_args.kwargs["classification"], classification)
        self.assertEqual(chunk.call_args.kwargs["classification"], classification)
        self.assertEqual(embed.call_args.args[0], chunk_result)
        self.assertIn("Knowledge Base 登録結果", format_knowledge_registration_result(result))
        self.assertEqual(result.vision_failed_count, 1)
        self.assertIn("Vision 説明の失敗: 1 件", format_knowledge_registration_result(result))

    def test_engine_failure_reports_parser_error_before_chunking(self):
        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "broken.pdf"
            source.write_bytes(b"%PDF-1.4\n")
            failed = _run([], available=False, message="解析に失敗しました: Docling boom")
            with (
                patch("rag_engine.workflows.knowledge_base.analyze_pdf", return_value=failed),
                patch("rag_engine.workflows.knowledge_base.create_chunk_run") as chunk,
                self.assertRaisesRegex(RuntimeError, "Docling boom"),
            ):
                register_source_file_for_rag(
                    source_path=source,
                    engine_ids=["docling"],
                    settings=replace(get_settings(), output_dir=Path(tmp) / "runs"),
                )
        chunk.assert_not_called()


def _run(records, *, available=True, message="解析が完了しました。"):
    status = SimpleNamespace(label="Docling", available=available, message=message)
    return SimpleNamespace(run_id="run123", statuses=[status], records=records)


if __name__ == "__main__":
    unittest.main()
