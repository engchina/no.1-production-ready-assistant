"""domain keyword candidates の挙動を保護するテスト。"""

import hashlib
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from rag_engine.chunking import (
    CHILD_CHUNK_LEVEL,
    CHUNK_SCHEMA_VERSION,
    CHUNKS_DIRECTORY,
    LATEST_CHUNKS_FILE,
    ChunkingConfig,
    ChunkingResult,
    DocumentChunk,
    chunk_payload,
)
from rag_engine.knowledge.domain_keyword_candidates import (
    DomainKeywordCandidateTokenizationError,
    DomainKeywordSourceText,
    format_domain_keyword_candidates_text,
    load_adb_domain_keyword_source_texts,
    load_adb_domain_keyword_source_texts_with_stats,
    load_latest_corpus_chunk_runs,
    suggest_domain_keyword_candidates,
    suggest_domain_keyword_candidates_from_latest_chunks,
)
from rag_engine.retrieval.text_search_tokenizer import TextSearchTokenizerConfig


class DomainKeywordCandidateTests(unittest.TestCase):
    def test_suggest_candidates_filters_existing_and_noise(self):
        chunks = [
            _source("chunk-1", "契約区分 出庫伝票 ORA-01555 2026-01-01 する"),
            _source("chunk-2", "契約区分 の エラー ORA-01555 を確認"),
        ]

        candidates = suggest_domain_keyword_candidates(
            chunks,
            existing_keywords=["出庫伝票"],
            tokenizer_config=TextSearchTokenizerConfig(),
            limit=10,
        )
        keywords = [candidate.keyword for candidate in candidates]

        self.assertIn("契約区分", keywords)
        self.assertIn("ora-01555", keywords)
        self.assertNotIn("出庫伝票", keywords)
        self.assertFalse(any(keyword.startswith("2026") for keyword in keywords))

    def test_candidates_keep_surface_nouns_codes_and_exclude_predicates_and_layout_fragments(self):
        text = (
            "設定識別番号を変える。画面接続情報を確かめる。合成マスタをまとめて表示する。"
            "サンプルを開く。サンプ\nルを閉じる。e-2045 と e_2045 を受ける。"
        )
        keywords = {item.keyword for item in suggest_domain_keyword_candidates([_source("a", text)])}
        self.assertIn("設定識別番号", keywords)
        self.assertIn("画面接続情報", keywords)
        self.assertIn("e-2045", keywords)
        self.assertIn("e_2045", keywords)
        self.assertIn("マスタ", keywords)
        self.assertIn("サンプル", keywords)
        self.assertFalse(keywords & {"変える", "変え", "確かめる", "確かめ", "受ける", "受け", "纏める", "マスター", "サンプ"})

    def test_candidates_exclude_search_text_context_labels(self):
        """検索文の文脈行は全 chunk 共通の定型なので、そのラベル語を候補にしない (#1035)。"""
        search_text = (
            "Source file: manual.pdf\n"
            "Classification: 申請業務 / 台帳\n"
            "Section path: 基本設定 > 初期設定\n"
            "Parent summary: 概要\n"
            "Child text: 契約区分 出庫伝票番号"
        )
        sources = [_source("chunk-1", search_text), _source("chunk-2", search_text)]

        candidates = suggest_domain_keyword_candidates(
            sources,
            tokenizer_config=TextSearchTokenizerConfig(),
            limit=50,
        )
        keywords = {candidate.keyword for candidate in candidates}

        self.assertIn("契約区分", keywords)
        self.assertIn("出庫伝票番号", keywords)
        self.assertFalse(
            keywords
            & {"source", "sourc", "file", "classification", "classif", "section", "path", "parent", "summary", "summari", "child", "text"}
        )

    def test_ranking_prefers_document_specific_terms_over_corpus_wide_terms(self):
        """全文書へ出る語より、一部文書に偏る用語を上位にする (#1035)。"""
        sources = [
            DomainKeywordSourceText(chunk_id=f"{document}-{index}", document_id=document, text=text)
            for document, chunk_texts in {
                "doc-a": ["システム 棚卸管理番号", "システム 棚卸管理番号", "システム"],
                "doc-b": ["システム", "システム", "システム"],
                "doc-c": ["システム", "システム", "システム"],
            }.items()
            for index, text in enumerate(chunk_texts)
        ]

        candidates = suggest_domain_keyword_candidates(
            sources,
            tokenizer_config=TextSearchTokenizerConfig(),
            limit=10,
        )
        keywords = [candidate.keyword for candidate in candidates]

        self.assertIn("棚卸管理番号", keywords)
        self.assertIn("システム", keywords)
        self.assertLess(keywords.index("棚卸管理番号"), keywords.index("システム"))

    def test_latest_chunk_runs_can_feed_candidate_generation(self):
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            result = _chunk_run(
                output_dir,
                [
                    _chunk("chunk-1", "契約区分 出庫伝票"),
                    _chunk("chunk-2", "契約区分 生年月日"),
                ],
            )
            _write_chunk_run(output_dir, result)

            loaded = load_latest_corpus_chunk_runs(output_dir)
            candidates, chunk_count, chunk_run_count = suggest_domain_keyword_candidates_from_latest_chunks(
                output_dir,
                tokenizer_config=TextSearchTokenizerConfig(),
            )

        self.assertEqual([run.chunk_run_id for run in loaded], [result.chunk_run_id])
        self.assertEqual(chunk_count, 2)
        self.assertEqual(chunk_run_count, 1)
        self.assertIn("契約区分", format_domain_keyword_candidates_text(candidates))

    def test_adb_source_query_reads_latest_active_child_search_text(self):
        connection = _Connection(
            result_sets=[
                [(1, 1)],
                [("chunk-uid-1", _Lob("契約区分 出庫伝票"), "doc-1", "source.pdf", "abcdef")],
            ]
        )

        with (
            patch("rag_engine.knowledge.domain_keyword_candidates.load_adb_settings", return_value=object()),
            patch("rag_engine.knowledge.domain_keyword_candidates.connect_adb_thin", return_value=connection),
        ):
            sources = load_adb_domain_keyword_source_texts(
                preferred_engine_ids=["docling"],
                max_source_chunks=5,
            )

        self.assertEqual(sources[0].chunk_id, "chunk-uid-1")
        self.assertEqual(sources[0].text, "契約区分 出庫伝票")
        self.assertIn("JOIN rag_documents d", connection.cursor_obj.sql)
        self.assertIn("d.latest_chunk_run_id = c.chunk_run_id", connection.cursor_obj.sql)
        self.assertIn("c.chunk_level = :chunk_level", connection.cursor_obj.sql)
        self.assertIn("$.schema_version", connection.cursor_obj.sql)
        self.assertIn("OFFSET 0 ROWS FETCH NEXT", connection.cursor_obj.sql)
        self.assertIn("c.chunk_seq, c.chunk_uid", connection.cursor_obj.sql)
        self.assertEqual(connection.cursor_obj.binds["chunk_level"], CHILD_CHUNK_LEVEL)
        self.assertEqual(connection.cursor_obj.binds["chunk_metadata_schema_version"], 4)
        self.assertEqual(connection.cursor_obj.binds["source_engine_id_0"], "docling")

    def test_candidate_generation_surfaces_tokenizer_errors(self):
        sources = [_source("chunk-1", "契約区分")]

        with (
            patch(
                "rag_engine.knowledge.domain_keyword_candidates.tokenize_domain_keyword_candidates",
                side_effect=RuntimeError("broken tokenizer"),
            ),
            self.assertRaisesRegex(DomainKeywordCandidateTokenizationError, "broken tokenizer"),
        ):
            suggest_domain_keyword_candidates(sources)

    def test_latest_chunk_runs_deduplicate_repeated_analysis_of_same_document(self):
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            older = _chunk_run(
                output_dir,
                [_chunk("older", "契約区分", source_run_id="aaaaaaaaaaaaaaaa")],
                source_run_id="aaaaaaaaaaaaaaaa",
                chunk_run_id="c0ffee0000000001",
                source_file_sha256="same-source-sha",
            )
            newer = _chunk_run(
                output_dir,
                [_chunk("newer", "出庫伝票番号", source_run_id="bbbbbbbbbbbbbbbb")],
                source_run_id="bbbbbbbbbbbbbbbb",
                chunk_run_id="c0ffee0000000002",
                source_file_sha256="same-source-sha",
            )
            _write_chunk_run(output_dir, older)
            _write_chunk_run(output_dir, newer)
            os.utime(older.latest_path, (1, 1))
            os.utime(newer.latest_path, (2, 2))

            loaded = load_latest_corpus_chunk_runs(output_dir)

        self.assertEqual([run.chunk_run_id for run in loaded], [newer.chunk_run_id])

    def test_adb_source_paginates_full_scan_before_balanced_sample(self):
        connection = _Connection(
            result_sets=[
                [(5, 2)],
                [
                    ("a-1", _Lob("契約区分"), "doc-a", "a.pdf", "run-a"),
                    ("a-2", _Lob("契約区分"), "doc-a", "a.pdf", "run-a"),
                ],
                [
                    ("a-3", _Lob("契約区分"), "doc-a", "a.pdf", "run-a"),
                    ("b-1", _Lob("出庫伝票番号"), "doc-b", "b.pdf", "run-b"),
                ],
                [
                    ("b-2", _Lob("出庫伝票番号"), "doc-b", "b.pdf", "run-b"),
                ],
            ]
        )

        with (
            patch("rag_engine.knowledge.domain_keyword_candidates.load_adb_settings", return_value=object()),
            patch("rag_engine.knowledge.domain_keyword_candidates.connect_adb_thin", return_value=connection),
        ):
            result = load_adb_domain_keyword_source_texts_with_stats(
                max_source_chunks=3,
                page_size=2,
            )

        self.assertEqual([source.chunk_id for source in result.sources], ["a-1", "b-1", "a-2"])
        self.assertEqual(result.processed_chunk_count, 3)
        self.assertEqual(result.total_chunk_count, 5)
        self.assertEqual(result.processed_document_count, 2)
        self.assertEqual(result.total_document_count, 2)
        self.assertTrue(result.sampled)
        self.assertEqual(result.page_count, 3)
        self.assertEqual(len(connection.cursor_obj.executions), 4)

    def test_latest_chunk_sampling_balances_documents_when_limited(self):
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            result = _chunk_run(
                output_dir,
                [
                    _chunk("a-1", "契約区分", source_file_name="a.pdf", source_run_id="run-a"),
                    _chunk("a-2", "契約区分", source_file_name="a.pdf", source_run_id="run-a"),
                    _chunk("a-3", "契約区分", source_file_name="a.pdf", source_run_id="run-a"),
                    _chunk("b-1", "出庫伝票番号", source_file_name="b.pdf", source_run_id="run-b"),
                    _chunk("b-2", "出庫伝票番号", source_file_name="b.pdf", source_run_id="run-b"),
                ],
            )
            _write_chunk_run(output_dir, result)

            suggestion = suggest_domain_keyword_candidates_from_latest_chunks(
                output_dir,
                tokenizer_config=TextSearchTokenizerConfig(),
                limit=10,
                max_source_chunks=3,
            )

        keywords = [candidate.keyword for candidate in suggestion.candidates]
        self.assertIn("契約区分", keywords)
        self.assertIn("出庫伝票番号", keywords)
        self.assertEqual(suggestion.processed_chunk_count, 3)
        self.assertEqual(suggestion.total_chunk_count, 5)
        self.assertEqual(suggestion.processed_source_count, 2)
        self.assertEqual(suggestion.total_source_count, 2)
        self.assertTrue(suggestion.sampled)


def _source(chunk_id: str, text: str) -> DomainKeywordSourceText:
    return DomainKeywordSourceText(
        chunk_id=chunk_id,
        document_id="source.pdf",
        text=text,
    )


def _chunk(
    chunk_id: str,
    text: str,
    *,
    source_file_name: str = "source.pdf",
    source_run_id: str = "abcdef",
) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=chunk_id,
        chunk_level=CHILD_CHUNK_LEVEL,
        chunk_seq=1,
        parent_chunk_id="",
        child_chunk_ids=[],
        text=text,
        retrieval_text=text,
        char_count=len(text),
        token_estimate=len(text.split()),
        source_run_id=source_run_id,
        source_file_name=source_file_name,
        source_engine_id="docling",
        source_engine_label="Docling",
        page_start=1,
        page_end=1,
        source_seq_ranges=[],
        source_record_refs=[],
        metadata={
            "schema_version": 4,
            "active": True,
            "atomic": False,
            "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "document": {"classification": {"large_category": "", "middle_category": "", "small_category": ""}},
            "section_path": [],
            "section_path_sources": [],
            "source_categories": ["Text"],
        },
    )


def _chunk_run(
    output_dir: Path,
    chunks: list[DocumentChunk],
    *,
    source_run_id: str = "abcdef",
    chunk_run_id: str = "abcdef1234567890",
    source_file_sha256: str = "sha",
) -> ChunkingResult:
    chunk_dir = output_dir / source_run_id / CHUNKS_DIRECTORY / chunk_run_id
    return ChunkingResult(
        source_run_id=source_run_id,
        chunk_run_id=chunk_run_id,
        source_file_name="source.pdf",
        selected_engine_ids=["docling"],
        config=ChunkingConfig(),
        config_hash="config123",
        created_at_utc="2026-09-08T00:00:00+00:00",
        active=True,
        source_file_sha256=source_file_sha256,
        source_page_count=1,
        chunks=chunks,
        json_path=str(chunk_dir / "chunks.json"),
        jsonl_path=str(chunk_dir / "chunks.jsonl"),
        latest_path=str(chunk_dir.parent / LATEST_CHUNKS_FILE),
        classification={},
    )


def _write_chunk_run(output_dir: Path, result: ChunkingResult) -> None:
    payload_path = Path(result.json_path)
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path.write_text(json.dumps(chunk_payload(result), ensure_ascii=False), encoding="utf-8")
    Path(result.latest_path).write_text(
        json.dumps(
            {
                "schema_version": CHUNK_SCHEMA_VERSION,
                "strategy": "small_to_big_parent_child",
                "source_run_id": result.source_run_id,
                "chunk_run_id": result.chunk_run_id,
                "active": True,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class _Lob:
    def __init__(self, value: str):
        self.value = value

    def read(self) -> str:
        return self.value


class _Cursor:
    def __init__(self, result_sets):
        self.result_sets = list(result_sets)
        self.rows = []
        self.sql = ""
        self.binds = {}
        self.executions = []

    def execute(self, sql, binds):
        self.sql = sql
        self.binds = binds
        self.executions.append((sql, binds))
        self.rows = list(self.result_sets.pop(0)) if self.result_sets else []

    def __iter__(self):
        return iter(self.rows)


class _Connection:
    def __init__(self, rows=None, result_sets=None):
        self.cursor_obj = _Cursor(result_sets if result_sets is not None else [rows or []])

    def cursor(self):
        return self.cursor_obj

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


if __name__ == "__main__":
    unittest.main()
