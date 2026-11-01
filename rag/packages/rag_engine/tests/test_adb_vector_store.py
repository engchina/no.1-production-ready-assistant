"""adb vector store の挙動を保護するテスト。"""

import hashlib
import json
import re
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import rag_engine.adapters.oracle.store as adb_vector_store
from rag_engine.adapters.oracle.store import (
    EMBEDDING_INPUT_TYPE_SEARCH_DOCUMENT,
    RETRIEVAL_SCOPE_KNOWLEDGE_BASE,
    StoredChunk,
    _chunk_uid,
    _classification_filter_sql,
    _combine_hybrid_scores,
    _hybrid_chunk_sort_key,
    _metadata_filter_sql,
    _retrieval_text_hash,
    format_embedding_save_result,
    format_embedding_status_result,
    load_chunk_embedding_status,
    oracle_text_query,
    save_chunk_run_embeddings,
    search_adb_hybrid_chunks,
)
from rag_engine.chunking import (
    CHILD_CHUNK_LEVEL,
    CHUNK_METADATA_SCHEMA_VERSION,
    PARENT_CHUNK_LEVEL,
    ChunkingConfig,
    ChunkingResult,
    DocumentChunk,
)
from rag_engine.config import DEFAULT_TIMEZONE, get_settings
from rag_engine.knowledge.classification import business_today, classification_filter_from_values
from rag_engine.retrieval.inquiry_conditions import FILE_DATA_PROFILE, InquiryMetadataFilter, parse_inquiry_conditions
from rag_engine.retrieval.text_search_tokenizer import TextSearchTokenizerConfig


class AdbVectorStoreTests(unittest.TestCase):
    def setUp(self):
        from rag_engine.adapters.oracle import store

        store._pool_cache.clear()
        self.addCleanup(store._pool_cache.clear)

    def test_original_question_keeps_its_vote_against_many_derived_queries(self):
        """派生検索文が何本あっても合計の票は原質問と同じ。無関係な拡張の多数決で別機能へ寄らない。"""
        from rag_engine.adapters.oracle.store import derived_query_weights

        original = [("uid-target", 0.1), ("uid-other", 0.3)]
        derived = [("uid-other", 0.1), ("uid-target", 0.4)]
        rankings = [original, derived, derived, derived, [("uid-third", 0.2)]]
        unweighted = _combine_hybrid_scores(rankings, [])
        weighted = _combine_hybrid_scores(rankings, [], query_weights=derived_query_weights(len(rankings)))
        self.assertEqual(unweighted[0].chunk_uid, "uid-other")
        self.assertEqual(weighted[0].chunk_uid, "uid-target")
        self.assertEqual(derived_query_weights(5), [1.0, 0.25, 0.25, 0.25, 0.25])
        self.assertEqual(derived_query_weights(3, enabled=False), [1.0, 1.0, 1.0])
        self.assertEqual(derived_query_weights(1), [1.0])

    def test_each_query_keeps_its_top_candidates_after_fusion(self):
        """1 つの派生質問にしか一致しない節が RRF 融合で候補上限から落ちても、その質問の上位として残す (#669)。"""
        from rag_engine.adapters.oracle.store import _reserve_query_top_candidates

        common = [(f"uid-common-{i}", 0.1 + i * 0.01) for i in range(6)]
        only_q3 = [("uid-q3-a", 0.05), ("uid-parent", 0.06), ("uid-q3-b", 0.07), ("uid-q3-c", 0.08), ("uid-q3-d", 0.09)]
        rankings = [common, common, only_q3 + common]
        scores = _combine_hybrid_scores(rankings, [])
        fused_top = [s.chunk_uid for s in scores[:6]]
        self.assertNotIn("uid-q3-a", fused_top)
        candidates = _reserve_query_top_candidates(scores, rankings, [], is_candidate=lambda uid: uid != "uid-parent", limit=6)
        self.assertEqual([s.chunk_uid for s in candidates[:6]], fused_top)  # 融合順位は変えない
        self.assertEqual([s.chunk_uid for s in candidates[6:]], ["uid-q3-a", "uid-q3-b", "uid-q3-c"])  # 親を数えず child 上位 3 件
        self.assertEqual(candidates[6].reserved_by, ["vector:q3"])
        self.assertFalse(candidates[0].reserved_by)

    def test_search_result_includes_reserved_candidates_with_their_reason(self):
        settings = replace(get_settings(), embedding_output_dimensions=1024, image_embedding_enabled=False)
        pool = [_stored_chunk(f"uid-{i}", f"chunk-{i}", page_start=i, chunk_seq=i) for i in range(1, 8)]
        common = [(f"uid-{i}", 0.1 + i * 0.01) for i in range(1, 6)]
        rankings = iter([common, common, [("uid-7", 0.05), ("uid-6", 0.06)] + common])
        with (
            patch("rag_engine.adapters.oracle.store.load_adb_settings", return_value="adb"),
            patch("rag_engine.adapters.oracle.store.connect_adb_thin", side_effect=lambda *_: _ConnectionContext(object())),
            patch("rag_engine.adapters.oracle.store.load_domain_keywords", return_value=[]),
            patch("rag_engine.adapters.oracle.store._available_embedding_count", return_value=1),
            patch("rag_engine.adapters.oracle.store._vector_search", side_effect=lambda *a, **k: next(rankings)),
            patch("rag_engine.adapters.oracle.store._text_search", return_value=[]),
            patch("rag_engine.adapters.oracle.store._load_chunk_run_chunks", return_value=pool),
        ):
            result = search_adb_hybrid_chunks(
                chunk_run_id="run-1",
                retrieval_queries=["原質問", "派生1", "派生2"],
                settings=settings,
                query_embedder=lambda *_: [0.1] * 1024,
                candidate_limit=5,
            )
        ids = {c.chunk_id: c for c in result.child_chunks}
        self.assertIn("chunk-7", ids)
        self.assertEqual(ids["chunk-7"].metadata["adb_hybrid"]["reserved_by"], ["vector:q3"])
        self.assertEqual(ids["chunk-1"].metadata["adb_hybrid"]["reserved_by"], [])

    def test_vector_only_queries_skip_text_search_but_keep_vector_search(self):
        # HyDE の仮説文は embedding だけに使い、Oracle Text の CONTAINS には使わない (#911)。
        settings = replace(get_settings(), embedding_output_dimensions=1024, image_embedding_enabled=False)
        pool = [_stored_chunk("uid-1", "chunk-1", page_start=1, chunk_seq=1)]
        with (
            patch("rag_engine.adapters.oracle.store.load_adb_settings", return_value="adb"),
            patch("rag_engine.adapters.oracle.store.connect_adb_thin", side_effect=lambda *_: _ConnectionContext(object())),
            patch("rag_engine.adapters.oracle.store.load_domain_keywords", return_value=[]),
            patch("rag_engine.adapters.oracle.store._available_embedding_count", return_value=1),
            patch("rag_engine.adapters.oracle.store._vector_search", return_value=[("uid-1", 0.1)]) as vector,
            patch("rag_engine.adapters.oracle.store._text_search", return_value=[("uid-1", 0.2)]) as text,
            patch("rag_engine.adapters.oracle.store._load_chunk_run_chunks", return_value=pool),
        ):
            search_adb_hybrid_chunks(
                chunk_run_id="run-1",
                retrieval_queries=["原質問 出力", "仮説文 出力 条件", "派生 出力"],
                vector_only_queries=["仮説文 出力 条件"],
                settings=settings,
                query_embedder=lambda *_: [0.1] * 1024,
                candidate_limit=5,
            )
        self.assertEqual(vector.call_count, 3)
        self.assertEqual(text.call_count, 2)
        self.assertFalse(any("仮説" in call.args[2] for call in text.call_args_list))

    def test_db_searches_are_stage_spans_for_the_progress_timeline(self):
        # 文書の検索の中の DB の検索（ベクトル検索・全文検索・検索対象の読み込み）は工程の span にする（#3436）。
        # 処理の経過で、埋め込みの後の時間の大半を占める DB の検索を末端の段として出す。
        from contextlib import contextmanager

        from rag_engine.resources.tracing import bind_trace_observer

        observed: list[tuple[str, str, str]] = []

        @contextmanager
        def observer(operation: str, name: str):
            observed.append(("start", operation, name))
            yield object()
            observed.append(("end", operation, name))

        settings = replace(get_settings(), embedding_output_dimensions=1024, image_embedding_enabled=False)
        pool = [_stored_chunk("uid-1", "chunk-1", page_start=1, chunk_seq=1)]

        def search(**kwargs):
            return search_adb_hybrid_chunks(
                chunk_run_id="run-1",
                settings=settings,
                query_embedder=lambda *_: [0.1] * 1024,
                candidate_limit=5,
                **kwargs,
            )

        with (
            patch("rag_engine.adapters.oracle.store.load_adb_settings", return_value="adb"),
            patch("rag_engine.adapters.oracle.store.connect_adb_thin", side_effect=lambda *_: _ConnectionContext(object())),
            patch("rag_engine.adapters.oracle.store.load_domain_keywords", return_value=[]),
            patch("rag_engine.adapters.oracle.store._available_embedding_count", return_value=1),
            patch("rag_engine.adapters.oracle.store._vector_search", return_value=[("uid-1", 0.1)]),
            patch("rag_engine.adapters.oracle.store._text_search", return_value=[("uid-1", 0.2)]) as text,
            patch("rag_engine.adapters.oracle.store._load_chunk_run_chunks", return_value=pool) as load,
            bind_trace_observer(observer),
        ):
            search(retrieval_queries=["原質問 出力", "派生 出力"])
            self.assertEqual(
                observed,
                [
                    ("start", "stage", adb_vector_store.VECTOR_SEARCH_STAGE),
                    ("end", "stage", adb_vector_store.VECTOR_SEARCH_STAGE),
                    ("start", "stage", adb_vector_store.TEXT_SEARCH_STAGE),
                    ("end", "stage", adb_vector_store.TEXT_SEARCH_STAGE),
                    ("start", "stage", adb_vector_store.CHUNK_POOL_LOAD_STAGE),
                    ("end", "stage", adb_vector_store.CHUNK_POOL_LOAD_STAGE),
                ],
            )
            self.assertEqual(text.call_count, 2)
            self.assertEqual(load.call_count, 1)
            # 読み込み済み（cache）の検索対象は読み込みの工程を出さない。全文検索に使う検索文が無ければ全文検索の工程も出さない。
            observed.clear()
            search(retrieval_queries=["仮説文 出力"], vector_only_queries=["仮説文 出力"])
            self.assertEqual(
                [entry[2] for entry in observed if entry[0] == "start"],
                [adb_vector_store.VECTOR_SEARCH_STAGE],
            )
            self.assertEqual(load.call_count, 1)

    def test_text_search_limit_counts_only_text_candidates(self):
        # 上限は vector 専用の検索文を除いてから数える (#921)。
        settings = replace(
            get_settings(), embedding_output_dimensions=1024, image_embedding_enabled=False, text_search_query_variant_limit=2
        )
        pool = [_stored_chunk("uid-1", "chunk-1", page_start=1, chunk_seq=1)]
        with (
            patch("rag_engine.adapters.oracle.store.load_adb_settings", return_value="adb"),
            patch("rag_engine.adapters.oracle.store.connect_adb_thin", side_effect=lambda *_: _ConnectionContext(object())),
            patch("rag_engine.adapters.oracle.store.load_domain_keywords", return_value=[]),
            patch("rag_engine.adapters.oracle.store._available_embedding_count", return_value=1),
            patch("rag_engine.adapters.oracle.store._vector_search", return_value=[("uid-1", 0.1)]),
            patch("rag_engine.adapters.oracle.store._text_search", return_value=[("uid-1", 0.2)]) as text,
            patch("rag_engine.adapters.oracle.store._load_chunk_run_chunks", return_value=pool),
        ):
            search_adb_hybrid_chunks(
                chunk_run_id="run-1",
                retrieval_queries=["原質問 出力", "仮説文 出力 条件", "派生 出力 手順"],
                vector_only_queries=["仮説文 出力 条件"],
                settings=settings,
                query_embedder=lambda *_: [0.1] * 1024,
                candidate_limit=5,
            )
        self.assertEqual(text.call_count, 2)
        self.assertTrue(any("派生" in call.args[2] for call in text.call_args_list))

    def test_repeated_search_in_same_scope_loads_chunks_and_checks_embeddings_once(self):
        settings = replace(get_settings(), embedding_output_dimensions=1024, image_embedding_enabled=False)
        pool = [_stored_chunk("uid-1", "chunk-1", page_start=1, chunk_seq=1)]
        with (
            patch("rag_engine.adapters.oracle.store.load_adb_settings", return_value="adb"),
            patch("rag_engine.adapters.oracle.store.connect_adb_thin", side_effect=lambda *_: _ConnectionContext(object())),
            patch("rag_engine.adapters.oracle.store.load_domain_keywords", return_value=[]),
            patch("rag_engine.adapters.oracle.store._available_embedding_count", return_value=1) as count,
            patch("rag_engine.adapters.oracle.store._vector_search", return_value=[("uid-1", 0.1)]),
            patch("rag_engine.adapters.oracle.store._text_search", return_value=[]),
            patch("rag_engine.adapters.oracle.store._load_chunk_run_chunks", return_value=pool) as load,
        ):
            for query in ("質問", "書き換えた質問"):
                result = search_adb_hybrid_chunks(
                    chunk_run_id="run-1", retrieval_queries=[query], settings=settings, query_embedder=lambda *_: [0.1] * 1024
                )
                self.assertEqual([c.chunk_id for c in result.child_chunks], ["chunk-1"])
            search_adb_hybrid_chunks(
                chunk_run_id="run-2", retrieval_queries=["質問"], settings=settings, query_embedder=lambda *_: [0.1] * 1024
            )
        self.assertEqual((load.call_count, count.call_count), (2, 2))  # run-1 で1回、run-2 で1回

    def test_oracle_text_query_sanitizes_reserved_words_and_symbols(self):
        query = oracle_text_query(
            "AND 契約区分 (登録) foo*bar\x00 OR 生年月日",
            tokenizer_config=TextSearchTokenizerConfig(),
        )

        # 語は ACCUM で結び、補う語（文字種の区切り・漢字の部分語）は軽くする (#588)。
        self.assertEqual(
            query,
            "{契約} ACCUM {区分} ACCUM {登録} ACCUM {foo} ACCUM {bar} ACCUM {生年月日} ACCUM {年月日}"
            " ACCUM {契約区分}*0.5 ACCUM {生年}*0.5 ACCUM {月日}*0.5",
        )
        self.assertNotIn("*", re.sub(r"\}\*[0-9.]+", "}", query))  # 重み以外に * が残らない
        self.assertNotIn("\x00", query)
        self.assertLessEqual(len(query), adb_vector_store.MAX_ORACLE_TEXT_QUERY_CHARS)

    def test_oracle_text_query_prioritizes_matched_domain_keywords(self):
        query = oracle_text_query(
            "登録 出庫伝票 契約区分",
            domain_keywords=["契約区分"],
            tokenizer_config=TextSearchTokenizerConfig(),
        )

        self.assertEqual(query, "{契約区分}*2 ACCUM {契約} ACCUM {区分} ACCUM {登録} ACCUM {出庫伝票} ACCUM {出庫} ACCUM {伝票}")

    def test_combine_hybrid_scores_uses_rrf_and_text_vector_tiebreakers(self):
        scores = _combine_hybrid_scores(
            [[("page9", 0.1), ("page1", 0.2)]],
            [[("page1", 10.0), ("page9", 5.0)]],
        )

        self.assertEqual([score.chunk_uid for score in scores], ["page1", "page9"])
        self.assertGreater(scores[0].rrf_score, 0)
        self.assertEqual(scores[0].text_score, 10.0)
        self.assertIn("vector:q1", scores[0].channels)
        self.assertIn("keyword:oracle_text", scores[0].channels)

    def test_combine_hybrid_scores_uses_weighted_profile_channels(self):
        scores = _combine_hybrid_scores(
            [[("vector-only", 0.1)]],
            [],
            [(f"profile:{FILE_DATA_PROFILE}", 2.0, [("profile-hit", 3.5)])],
        )

        self.assertEqual(scores[0].chunk_uid, "profile-hit")
        self.assertEqual(scores[0].profile_rank, 1)
        self.assertEqual(scores[0].profile_score, 3.5)
        self.assertIn(f"profile:{FILE_DATA_PROFILE}", scores[0].channels)
        self.assertEqual(scores[0].channel_scores[f"profile:{FILE_DATA_PROFILE}"], 3.5)

    def test_hybrid_chunk_sort_key_uses_page_and_seq_after_rank_ties(self):
        score_a = adb_vector_store._HybridScore(
            chunk_uid="a",
            rrf_score=1.0,
            text_score=2.0,
            vector_distance=0.3,
        )
        score_b = adb_vector_store._HybridScore(
            chunk_uid="b",
            rrf_score=1.0,
            text_score=2.0,
            vector_distance=0.3,
        )
        chunk_a = _stored_chunk("a", "chunk-a", page_start=9, chunk_seq=3)
        chunk_b = _stored_chunk("b", "chunk-b", page_start=1, chunk_seq=2)

        ordered = sorted([(score_a, chunk_a), (score_b, chunk_b)], key=_hybrid_chunk_sort_key)

        self.assertEqual([chunk.chunk_uid for _, chunk in ordered], ["b", "a"])

    def test_hybrid_search_uses_all_queries_for_vector_and_text_with_limit(self):
        settings = replace(
            get_settings(),
            embedding_output_dimensions=1024,
            image_embedding_enabled=False,
            text_search_tokenizer_latin_stemmer="none",
            text_search_query_variant_limit=2,
        )
        connection = object()
        embedded_queries = []

        def query_embedder(query, settings_value):
            embedded_queries.append(query)
            return [0.1] * 1024

        with (
            patch("rag_engine.adapters.oracle.store.load_adb_settings", return_value=object()),
            patch("rag_engine.adapters.oracle.store.connect_adb_thin", return_value=_ConnectionContext(connection)),
            patch("rag_engine.adapters.oracle.store.load_domain_keywords", return_value=["question"]) as load_keywords,
            patch("rag_engine.adapters.oracle.store._available_embedding_count", return_value=2),
            patch(
                "rag_engine.adapters.oracle.store._vector_search",
                side_effect=[[("uid-original", 0.1)], [("uid-expanded", 0.2)]],
            ) as vector_search,
            patch(
                "rag_engine.adapters.oracle.store._text_search",
                side_effect=[[("uid-original", 12.0)], [("uid-expanded", 8.0)]],
            ) as text_search,
            patch(
                "rag_engine.adapters.oracle.store._load_chunk_run_chunks",
                return_value=[
                    _stored_chunk("uid-original", "chunk-original", page_start=1, chunk_seq=1),
                    _stored_chunk("uid-expanded", "chunk-expanded", page_start=1, chunk_seq=2),
                ],
            ),
        ):
            result = search_adb_hybrid_chunks(
                chunk_run_id="abcdef-1234567890ab",
                retrieval_queries=["original question", "expanded synonym"],
                settings=settings,
                candidate_limit=5,
                query_embedder=query_embedder,
            )

        self.assertEqual(embedded_queries, ["original question", "expanded synonym"])
        self.assertEqual(vector_search.call_count, 2)
        load_keywords.assert_called_once_with(settings.output_dir)
        self.assertEqual(text_search.call_count, 2)
        self.assertEqual(text_search.call_args_list[0].args[2], "{question}*2 ACCUM {クエスチョン} ACCUM {オリジナル} ACCUM {original}")
        self.assertEqual(text_search.call_args_list[1].args[2], "{エキスパンデッド} ACCUM {expanded} ACCUM {シノニム} ACCUM {synonym}")
        self.assertEqual([chunk.chunk_id for chunk in result.child_chunks], ["chunk-original", "chunk-expanded"])
        self.assertIn("keyword:oracle_text:q2", result.child_chunks[1].metadata["adb_hybrid"]["retrieval_channels"])

    def test_hybrid_search_runs_extra_text_queries_as_additional_keyword_channels(self):
        """組み立て済みの Oracle Text 式（対象語の部分語）は質問文の変種とは別に検索し、派生と同じ重みで融合する (#730)。"""
        settings = replace(
            get_settings(),
            embedding_output_dimensions=1024,
            image_embedding_enabled=False,
            text_search_tokenizer_latin_stemmer="none",
        )
        connection = object()
        with (
            patch("rag_engine.adapters.oracle.store.load_adb_settings", return_value=object()),
            patch("rag_engine.adapters.oracle.store.connect_adb_thin", return_value=_ConnectionContext(connection)),
            patch("rag_engine.adapters.oracle.store.load_domain_keywords", return_value=[]),
            patch("rag_engine.adapters.oracle.store._available_embedding_count", return_value=2),
            patch("rag_engine.adapters.oracle.store._vector_search", return_value=[("uid-original", 0.1)]),
            patch(
                "rag_engine.adapters.oracle.store._text_search", side_effect=[[("uid-original", 12.0)], [("uid-note", 45.0)]]
            ) as text_search,
            patch(
                "rag_engine.adapters.oracle.store._load_chunk_run_chunks",
                return_value=[
                    _stored_chunk("uid-original", "chunk-original", page_start=1, chunk_seq=1),
                    _stored_chunk("uid-note", "chunk-note", page_start=13, chunk_seq=2),
                ],
            ),
        ):
            result = search_adb_hybrid_chunks(
                chunk_run_id="abcdef-1234567890ab",
                retrieval_queries=["original question"],
                settings=settings,
                candidate_limit=5,
                query_embedder=lambda query, settings_value: [0.1] * 1024,
                extra_text_queries=("{基準月}", "{基準月}"),
            )

        self.assertEqual(text_search.call_count, 2)  # 重複した式は 1 回だけ
        self.assertEqual(text_search.call_args_list[1].args[2], "{基準月}")
        self.assertIn("chunk-note", [chunk.chunk_id for chunk in result.child_chunks])  # フレーズだけに一致した chunk が候補に入る

    def test_hybrid_search_adds_optional_image_vector_channel(self):
        settings = replace(
            get_settings(),
            embedding_output_dimensions=1024,
            image_embedding_enabled=True,
            image_embedding_rrf_weight=0.5,
            text_search_tokenizer_latin_stemmer="none",
        )
        connection = object()

        def query_embedder(query, settings_value):
            return [0.1] * 1024

        with (
            patch("rag_engine.adapters.oracle.store.load_adb_settings", return_value=object()),
            patch("rag_engine.adapters.oracle.store.connect_adb_thin", return_value=_ConnectionContext(connection)),
            patch("rag_engine.adapters.oracle.store.load_domain_keywords", return_value=[]),
            patch("rag_engine.adapters.oracle.store._available_embedding_count", return_value=2),
            patch(
                "rag_engine.adapters.oracle.store._vector_search",
                side_effect=[[("uid-text", 0.1)], [("uid-image", 0.05)]],
            ) as vector_search,
            patch("rag_engine.adapters.oracle.store._text_search", return_value=[]),
            patch(
                "rag_engine.adapters.oracle.store._load_chunk_run_chunks",
                return_value=[
                    _stored_chunk("uid-text", "chunk-text", page_start=1, chunk_seq=1),
                    _stored_chunk("uid-image", "chunk-image", page_start=1, chunk_seq=2),
                ],
            ),
        ):
            result = search_adb_hybrid_chunks(
                chunk_run_id="abcdef-1234567890ab",
                retrieval_queries=["画像ボタン"],
                settings=settings,
                candidate_limit=5,
                query_embedder=query_embedder,
            )

        self.assertEqual(vector_search.call_count, 2)
        self.assertEqual(
            vector_search.call_args_list[1].kwargs["embedding_modality"],
            adb_vector_store.EMBEDDING_MODALITY_IMAGE,
        )
        by_chunk_id = {chunk.chunk_id: chunk for chunk in result.child_chunks}
        self.assertIn("image_vector:q1", by_chunk_id["chunk-image"].metadata["adb_hybrid"]["retrieval_channels"])

    def test_metadata_filter_sql_is_applied_before_vector_row_limit(self):
        settings = replace(
            get_settings(),
            embedding_output_dimensions=1024,
        )
        conditions = parse_inquiry_conditions("販売管理操作説明書.pdf の拠点名を変更する方法を教えてください。")
        connection = _CaptureConnection()

        sql, binds = _metadata_filter_sql("c", conditions.metadata_filter)

        self.assertIn("LOWER(c.source_file_name)", sql)
        self.assertNotIn("JSON_EXISTS(c.metadata_json", sql)  # 業務語は WHERE でなく加点 (#848)
        self.assertIn("metadata_source_file_0", binds)
        self.assertNotIn("metadata_json_term_0", binds)

        adb_vector_store._vector_search(
            connection,
            "abcdef-1234567890ab",
            [0.1] * 1024,
            settings,
            ["docling"],
            5,
            conditions.metadata_filter,
        )

        self.assertIn("LOWER(c.source_file_name)", connection.cursor_obj.sql)
        self.assertLess(
            connection.cursor_obj.sql.index("LOWER(c.source_file_name)"),
            connection.cursor_obj.sql.index("FETCH FIRST"),
        )
        self.assertNotIn("metadata_json_term_0", connection.cursor_obj.binds)

    def test_business_filter_does_not_match_keys_diagnostics_or_partial_values(self):
        cases = [
            ({"販売管理": "unrelated"}, False),
            ({"parser": {"engine_label": "販売管理"}}, False),
            ({"retrieval_profile": {"page_facts": {"screen_terms": ["販売管理"]}}}, False),
            ({"retrieval_profile": {"business_domains": ["販売管理"]}}, True),
            ({"retrieval_profile": {"business_domains": ["販売管理以外"]}}, False),
            ({"retrieval_profile": {"business_domains": {"販売管理": True}}}, False),
            ({"document": {"classification": {"large_category": "10_販売管理"}}}, True),
            ({"document": {"classification": {"large_category": "販売管理"}}}, True),
            ({"document": {"classification": {"large_category": None}}}, False),
            ({}, False),
        ]
        for metadata, expected in cases:
            with self.subTest(metadata=metadata):
                chunk = _stored_chunk("uid", "chunk", page_start=1, chunk_seq=1, metadata=metadata)
                result = adb_vector_store._chunks_matching_metadata_filter(
                    [chunk],
                    InquiryMetadataFilter(metadata_terms=("販売管理",)),
                )
                self.assertEqual(bool(result), expected)

    def test_local_metadata_filter_excludes_legacy_schema_even_without_business_filter(self):
        current = _stored_chunk("uid-v3", "chunk-v3", page_start=1, chunk_seq=1)
        legacy = _stored_chunk("uid-v2", "chunk-v2", page_start=1, chunk_seq=2, metadata={"schema_version": 2, "active": True})

        self.assertEqual(adb_vector_store._chunks_matching_metadata_filter([legacy, current], None), [current])

    def test_business_terms_are_not_sql_conditions_and_match_only_business_fields(self):
        term = "a\"b\\c' OR 1=1 --"
        sql, binds = _metadata_filter_sql("c", InquiryMetadataFilter(metadata_terms=(term,)))
        self.assertEqual((sql, binds), ("", {}))  # 業務語だけでは WHERE を作らない (#848)
        chunk = _stored_chunk(
            "uid", "chunk", page_start=1, chunk_seq=1, metadata={"document": {"classification": {"small_category": term}}}
        )
        self.assertEqual(adb_vector_store._chunks_matching_metadata_filter([chunk], InquiryMetadataFilter(metadata_terms=(term,))), [chunk])

    def test_page_filter_sql_and_local_filter_use_page_range(self):
        conditions = InquiryMetadataFilter(source_file_terms=("manual.pdf",), page_numbers=(2, 2, 0))
        sql, binds = _metadata_filter_sql("c", conditions)
        self.assertIn("(c.page_start <= :metadata_page_0 AND c.page_end >= :metadata_page_0)", sql)
        self.assertEqual(binds["metadata_page_0"], 2)
        self.assertNotIn("metadata_page_1", binds)
        on_page = _stored_chunk("uid-2", "chunk-2", page_start=2, chunk_seq=1)
        off_page = _stored_chunk("uid-3", "chunk-3", page_start=3, chunk_seq=2)
        self.assertEqual(adb_vector_store._chunks_matching_metadata_filter([off_page, on_page], conditions), [on_page])

    def test_business_filter_local_limit_is_twelve_terms(self):
        terms = tuple(f"term{i}" for i in range(13))
        chunk = _stored_chunk("uid", "chunk", page_start=1, chunk_seq=1, metadata={"retrieval_profile": {"business_domains": [terms[-1]]}})
        conditions = InquiryMetadataFilter(metadata_terms=terms)
        self.assertEqual(adb_vector_store._chunks_matching_metadata_filter([chunk], conditions), [])

    def test_business_term_match_boosts_ranking_without_excluding_other_chunks(self):
        """業務語に一致する chunk は channel の加点で上位に寄り、一致しない chunk も候補に残る (#848)。"""
        from rag_engine.retrieval.inquiry_conditions import InquiryConditionParse

        settings = replace(get_settings(), embedding_output_dimensions=1024, image_embedding_enabled=False)
        plain = _stored_chunk("uid-1", "chunk-1", page_start=1, chunk_seq=1)
        matched = _stored_chunk(
            "uid-2", "chunk-2", page_start=2, chunk_seq=2, metadata={"retrieval_profile": {"business_domains": ["販売管理"]}}
        )
        conditions = InquiryConditionParse(
            original_question="販売管理の伝票登録手順は？",
            business_domains=("販売管理",),
            metadata_filter=InquiryMetadataFilter(metadata_terms=("販売管理",)),
        )
        with (
            patch("rag_engine.adapters.oracle.store.load_adb_settings", return_value="adb"),
            patch("rag_engine.adapters.oracle.store.connect_adb_thin", side_effect=lambda *_: _ConnectionContext(object())),
            patch("rag_engine.adapters.oracle.store.load_domain_keywords", return_value=[]),
            patch("rag_engine.adapters.oracle.store._available_embedding_count", return_value=1),
            patch("rag_engine.adapters.oracle.store._vector_search", return_value=[("uid-1", 0.10), ("uid-2", 0.11)]) as vector,
            patch("rag_engine.adapters.oracle.store._text_search", return_value=[]),
            patch("rag_engine.adapters.oracle.store._load_chunk_run_chunks", return_value=[plain, matched]),
        ):
            result = search_adb_hybrid_chunks(
                chunk_run_id="run-1",
                retrieval_queries=["質問"],
                settings=settings,
                query_embedder=lambda *_: [0.1] * 1024,
                inquiry_conditions=conditions,
            )
        self.assertEqual([c.chunk_id for c in result.child_chunks], ["chunk-2", "chunk-1"])
        self.assertIn("business_match", result.child_chunks[0].metadata["adb_hybrid"]["channel_ranks"])
        self.assertNotIn("business_match", result.child_chunks[1].metadata["adb_hybrid"]["channel_ranks"])
        self.assertIs(vector.call_args.args[6], conditions.metadata_filter)  # ファイル名・ページ条件は従来どおり渡す

    def test_embedding_hash_uses_actual_input_despite_stale_metadata(self):
        chunk = _chunk_run().chunks[0]
        chunk.retrieval_text = "new embedding input"
        chunk.metadata["content_hash"] = "c" * 64
        chunk.metadata["retrieval"] = {"search_text_hash": "a" * 64, "retrieval_text_hash": "b" * 64}
        expected = hashlib.sha256(chunk.retrieval_text.encode("utf-8")).hexdigest()
        expected_content = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
        self.assertEqual(_retrieval_text_hash(chunk), expected)
        row = adb_vector_store._chunk_row(_chunk_run(), "document", chunk)
        self.assertEqual(row["search_text_hash"], expected)
        self.assertEqual(row["content_hash"], expected_content)

    def test_chunk_row_derives_flags_from_compact_context_and_atomic_value(self):
        chunk_run = _chunk_run()
        chunk = chunk_run.chunks[0]
        chunk.metadata.update(
            {
                "source_categories": ["Text"],
                "image_evidence": [{"image_id": "picture-1"}],
                "table_context": [{"table_id": "table-1"}],
                "atomic": True,
            }
        )

        row = adb_vector_store._chunk_row(chunk_run, "document", chunk)

        self.assertEqual(row["contains_picture"], "Y")
        self.assertEqual(row["contains_table"], "Y")
        self.assertEqual(row["atomic"], "Y")

    def test_classification_filter_sql_uses_json_value(self):
        classification_filter = classification_filter_from_values(
            large_category="在庫管理",
            middle_category="操作説明書",
            small_category="倉庫連携",
        )

        sql, binds = _classification_filter_sql("c", classification_filter)

        self.assertIn("JSON_VALUE(c.metadata_json, '$.document.classification.large_category')", sql)
        self.assertIn("JSON_VALUE(c.metadata_json, '$.document.classification.middle_category')", sql)
        self.assertIn("JSON_VALUE(c.metadata_json, '$.document.classification.small_category')", sql)
        self.assertNotIn(" IN ", sql)
        self.assertEqual(binds["classification_large_category"], "在庫管理")
        self.assertEqual(binds["classification_middle_category"], "操作説明書")
        self.assertEqual(binds["classification_small_category"], "倉庫連携")

    def test_filter_sql_always_limits_to_effective_window_as_of(self):
        # 日付未設定の chunk は除外せず、as_of は空なら既定の時間帯の今日 (#3249)、指定日は文字列比較で終了日を排他にする (#893)。
        sql, binds = _classification_filter_sql("c", classification_filter_from_values(as_of="2026-04-01"))
        self.assertIn("COALESCE(JSON_VALUE(c.metadata_json, '$.document.effective_from'), :as_of) <= :as_of", sql)
        self.assertIn("COALESCE(JSON_VALUE(c.metadata_json, '$.document.effective_to'), :as_of_open_end) > :as_of", sql)
        self.assertNotIn("classification", sql)
        self.assertEqual(binds, {"as_of": "2026-04-01", "as_of_open_end": "9999-12-31"})
        self.assertEqual(_classification_filter_sql("c", None)[1]["as_of"], business_today(DEFAULT_TIMEZONE).isoformat())
        with self.assertRaises(ValueError):
            classification_filter_from_values(as_of="2026/04/01")

    def test_knowledge_base_scope_searches_latest_active_documents_with_classification_filter(self):
        settings = replace(
            get_settings(),
            embedding_output_dimensions=1024,
        )
        connection = _CaptureConnection()
        classification_filter = classification_filter_from_values(large_category="販売管理")

        adb_vector_store._vector_search(
            connection,
            "",
            [0.1] * 1024,
            settings,
            ["docling"],
            5,
            retrieval_scope=RETRIEVAL_SCOPE_KNOWLEDGE_BASE,
            classification_filter=classification_filter,
        )

        self.assertIn("JOIN rag_documents d ON d.document_id = c.document_id", connection.cursor_obj.sql)
        self.assertIn("d.latest_chunk_run_id = c.chunk_run_id", connection.cursor_obj.sql)
        self.assertNotIn("c.chunk_run_id = :chunk_run_id", connection.cursor_obj.sql)
        self.assertEqual(connection.cursor_obj.binds["classification_large_category"], "販売管理")

    def test_image_vector_search_filters_by_embedding_modality(self):
        settings = replace(
            get_settings(),
            embedding_output_dimensions=1024,
        )
        connection = _CaptureConnection()

        adb_vector_store._vector_search(
            connection,
            "abcdef-1234567890ab",
            [0.1] * 1024,
            settings,
            ["docling"],
            5,
            embedding_modality=adb_vector_store.EMBEDDING_MODALITY_IMAGE,
        )

        self.assertIn("e.embedding_modality = :embedding_modality", connection.cursor_obj.sql)
        self.assertNotIn("e.embedding_text_hash = c.search_text_hash", connection.cursor_obj.sql)
        self.assertEqual(connection.cursor_obj.binds["embedding_modality"], adb_vector_store.EMBEDDING_MODALITY_IMAGE)

    def test_knowledge_base_scope_does_not_bind_chunk_run_id_without_placeholder(self):
        settings = replace(
            get_settings(),
            embedding_output_dimensions=1024,
        )
        classification_filter = classification_filter_from_values(large_category="販売管理")

        count_connection = _CaptureConnection(rows=[(1,)])
        adb_vector_store._available_embedding_count(
            count_connection,
            "",
            settings.embedding_model,
            1024,
            ["docling"],
            retrieval_scope=RETRIEVAL_SCOPE_KNOWLEDGE_BASE,
            classification_filter=classification_filter,
        )
        vector_connection = _CaptureConnection()
        adb_vector_store._vector_search(
            vector_connection,
            "",
            [0.1] * 1024,
            settings,
            ["docling"],
            5,
            retrieval_scope=RETRIEVAL_SCOPE_KNOWLEDGE_BASE,
            classification_filter=classification_filter,
        )
        text_connection = _CaptureConnection()
        adb_vector_store._text_search(
            text_connection,
            "",
            "{販売管理}",
            settings,
            ["docling"],
            5,
            retrieval_scope=RETRIEVAL_SCOPE_KNOWLEDGE_BASE,
            classification_filter=classification_filter,
        )
        chunks_connection = _CaptureConnection()
        adb_vector_store._load_chunk_run_chunks(
            chunks_connection,
            "",
            ["docling"],
            retrieval_scope=RETRIEVAL_SCOPE_KNOWLEDGE_BASE,
            classification_filter=classification_filter,
        )

        for cursor in (
            count_connection.cursor_obj,
            vector_connection.cursor_obj,
            text_connection.cursor_obj,
            chunks_connection.cursor_obj,
        ):
            self.assertIn("JOIN rag_documents d ON d.document_id = c.document_id", cursor.sql)
            self.assertIn("$.schema_version", cursor.sql)
            self.assertEqual(cursor.binds["chunk_metadata_schema_version"], 4)
            self.assertNotIn(":chunk_run_id", cursor.sql)
            self.assertNotIn("chunk_run_id", cursor.binds)

    def test_metadata_filter_matching_accepts_classification_without_metadata_filter(self):
        classification_filter = classification_filter_from_values(large_category="在庫管理")
        chunks = [
            _stored_chunk(
                "uid-match",
                "chunk-match",
                page_start=1,
                chunk_seq=1,
                metadata={"document": {"classification": {"large_category": "20_在庫管理"}}},
            ),
            _stored_chunk(
                "uid-legacy",
                "chunk-legacy",
                page_start=1,
                chunk_seq=2,
                metadata={"document": {"classification": {"large_category": "在庫管理"}}},
            ),
            _stored_chunk(
                "uid-miss",
                "chunk-miss",
                page_start=1,
                chunk_seq=3,
                metadata={"document": {"classification": {"large_category": "販売管理"}}},
            ),
        ]

        selected = adb_vector_store._chunks_matching_metadata_filter(
            chunks,
            None,
            classification_filter,
        )

        self.assertEqual([chunk.chunk_uid for chunk in selected], ["uid-legacy"])

    def test_save_chunk_run_embeddings_upserts_chunks_skips_existing_and_batches(self):
        chunk_run = _chunk_run()
        existing_hash = _retrieval_text_hash(chunk_run.chunks[0])
        connection = _FakeConnection(
            existing_hashes={
                _chunk_uid(chunk_run.chunk_run_id, chunk_run.chunks[0].chunk_id): existing_hash,
            }
        )
        settings = replace(
            get_settings(),
            embedding_model="cohere.embed-v4.0",
            embedding_output_dimensions=1024,
            embedding_batch_size=1,
        )
        embed_calls = []

        def embedder(texts, settings_value, *, input_type):
            embed_calls.append((list(texts), settings_value, input_type))
            return [[float(len(embed_calls))] * 1024 for _ in texts]

        result = save_chunk_run_embeddings(
            chunk_run,
            settings,
            preferred_engine_ids=["docling"],
            embedder=embedder,
            connection=connection,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.child_count, 2)
        self.assertEqual(result.parent_count, 1)
        self.assertEqual(result.created_count, 1)
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(result.error_count, 0)
        self.assertEqual(embed_calls[0][0], ["new retrieval"])
        self.assertEqual(embed_calls[0][2], EMBEDDING_INPUT_TYPE_SEARCH_DOCUMENT)
        self.assertEqual(connection.commit_count, 2)  # embedding batch 1回 + latest run の公開
        self.assertEqual(connection.rollback_count, 0)
        self.assertEqual([binds["publish"] for binds in connection.document_binds], [0, 1])
        # 新規文書: 公開前の UPDATE は 0 行なので INSERT、Embedding 保存後に公開の UPDATE (#615)。
        self.assertEqual(connection.document_statements, ["UPDATE", "INSERT", "UPDATE"])
        # VARCHAR2 bind は CLOB 列と型が合わず ORA-00932 になる (#533)。
        import oracledb

        self.assertEqual(
            [sizes.get("metadata_json") for sizes in connection.document_input_sizes],
            [oracledb.DB_TYPE_CLOB, oracledb.DB_TYPE_CLOB],
        )
        self.assertEqual(len(connection.chunk_rows), 3)
        self.assertEqual(len(connection.embedding_rows), 1)
        self.assertEqual(connection.embedding_rows[0]["embedding_dimensions"], 1024)

    def test_default_query_embedder_batches_all_queries_in_one_call(self):
        # 既定の embed_query のままなら検索文をまとめて 1 回で embedding する (#876)。
        from rag_engine.adapters.oracle.store import _query_vectors, embed_query

        settings = get_settings()
        calls = []

        def fake_embed_texts(texts, _settings, *, input_type):
            calls.append((list(texts), input_type))
            return [[float(i)] for i in range(len(texts))]

        with patch("rag_engine.adapters.oracle.store.embed_texts", side_effect=fake_embed_texts):
            vectors = _query_vectors(["原質問", "派生1", "派生2"], settings, embed_query)
        self.assertEqual(vectors, [[0.0], [1.0], [2.0]])
        self.assertEqual(calls, [(["原質問", "派生1", "派生2"], "SEARCH_QUERY")])
        # 差し替えた embedder は 1 件ずつ
        self.assertEqual(_query_vectors(["a", "b"], settings, lambda q, _s: [len(q)]), [[1], [1]])

    def test_upsert_document_splits_update_and_insert_without_merge(self):
        # MERGE の CASE で目標表の CLOB を自己参照すると ORA-30926 になる環境がある (#615)。
        from rag_engine.adapters.oracle.store import _upsert_document

        chunk_run = _chunk_run()
        # 既存文書・公開前: latest と metadata_json に触れない UPDATE だけ。
        connection = _FakeConnection()
        connection.documents.add("doc-1")
        _upsert_document(connection, chunk_run, "doc-1", publish=False)
        self.assertEqual(connection.document_statements, ["UPDATE"])
        self.assertNotIn("chunk_run_id", connection.document_binds[0])
        self.assertNotIn("metadata_json", connection.document_binds[0])
        # 既存文書・公開: latest と metadata_json を含む UPDATE だけ。
        _upsert_document(connection, chunk_run, "doc-1", publish=True)
        self.assertEqual(connection.document_statements, ["UPDATE", "UPDATE"])
        self.assertEqual(connection.document_binds[1]["chunk_run_id"], chunk_run.chunk_run_id)
        self.assertIn("metadata_json", connection.document_binds[1])
        # 新規文書: UPDATE が 0 行なら INSERT。公開前は latest と metadata_json を NULL にする (#875)。
        _upsert_document(connection, chunk_run, "doc-2", publish=False)
        self.assertEqual(connection.document_statements[2:], ["UPDATE", "INSERT"])
        self.assertIn("doc-2", connection.documents)
        self.assertIsNone(connection.document_binds[2]["chunk_run_id"])  # binds は呼び出しごとに 1 件
        self.assertIsNone(connection.document_binds[2]["metadata_json"])
        # 新規文書・公開: INSERT が latest と metadata_json を持つ。
        _upsert_document(connection, chunk_run, "doc-4", publish=True)
        self.assertEqual(connection.document_statements[4:], ["UPDATE", "INSERT"])
        self.assertEqual(connection.document_binds[3]["chunk_run_id"], chunk_run.chunk_run_id)
        # 並行登録: INSERT が ORA-00001 なら UPDATE をやり直す。
        racing = _FakeConnection()
        original_execute = _FakeCursor.execute

        def execute_with_race(cursor, sql, binds=None):
            if sql.split()[0] == "INSERT":
                racing.documents.add(binds["document_id"])  # 別 session が先に INSERT した
            return original_execute(cursor, sql, binds)

        _FakeCursor.execute = execute_with_race
        try:
            _upsert_document(racing, chunk_run, "doc-3", publish=False)
        finally:
            _FakeCursor.execute = original_execute
        self.assertEqual(racing.document_statements, ["UPDATE", "INSERT", "UPDATE"])
        self.assertFalse(any("MERGE" in name for name, _ in connection.statements + racing.statements))

    def test_save_chunk_run_embeddings_rejects_engine_without_active_children(self):
        connection = _FakeConnection()
        settings = replace(get_settings(), embedding_output_dimensions=1024)

        result = save_chunk_run_embeddings(
            _chunk_run(),
            settings,
            preferred_engine_ids=["archived_parser"],
            connection=connection,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.child_count, 0)
        self.assertEqual(result.error_count, 1)
        self.assertIn("active child chunk", result.errors[0])
        self.assertEqual(connection.commit_count, 0)

    def test_save_chunk_run_embeddings_optionally_writes_image_embedding_rows(self):
        chunk_run = _chunk_run()
        chunk_run.chunks[1].metadata = {
            "schema_version": 4,
            "active": True,
            "image_evidence": [
                {
                    "image_id": "docling-p1-2",
                    "source_run_id": "abcdef",
                    "page": 1,
                    "crop_path": "docling/vision/docling-p1-2.png",
                }
            ],
        }
        connection = _FakeConnection()
        image_calls = []

        with TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "abcdef" / "docling" / "vision" / "docling-p1-2.png"
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"image-bytes")
            settings = replace(
                get_settings(),
                output_dir=Path(tmp),
                embedding_model="cohere.embed-v4.0",
                embedding_output_dimensions=1024,
                embedding_batch_size=4,
                image_embedding_enabled=True,
            )

            def embedder(texts, settings_value, *, input_type):
                return [[1.0] * 1024 for _ in texts]

            def image_embedder(paths, settings_value, *, texts, input_type):
                image_calls.append((list(paths), list(texts), input_type))
                return [[2.0] * 1024 for _ in paths]

            result = save_chunk_run_embeddings(
                chunk_run,
                settings,
                embedder=embedder,
                image_embedder=image_embedder,
                connection=connection,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.created_count, 3)
        self.assertEqual(result.image_candidate_count, 1)
        self.assertEqual(result.image_created_count, 1)
        self.assertEqual(result.image_missing_asset_count, 0)
        self.assertEqual(len(connection.embedding_rows), 3)
        image_rows = [
            row for row in connection.embedding_rows if row.get("embedding_modality") == adb_vector_store.EMBEDDING_MODALITY_IMAGE
        ]
        self.assertEqual(len(image_rows), 1)
        self.assertEqual(image_rows[0]["chunk_uid"], _chunk_uid(chunk_run.chunk_run_id, chunk_run.chunks[1].chunk_id))
        self.assertEqual(image_calls[0][0][0].name, "docling-p1-2.png")
        self.assertEqual(image_calls[0][1], ["new retrieval"])
        self.assertEqual(image_calls[0][2], EMBEDDING_INPUT_TYPE_SEARCH_DOCUMENT)

    def test_image_embedding_candidates_include_native_table_crop(self):
        chunk_run = _chunk_run()
        chunk = chunk_run.chunks[1]
        chunk.metadata = {
            "schema_version": 4,
            "active": True,
            "table_context": [
                {
                    "table_id": "docling-p1-1",
                    "visual_evidence": [
                        {
                            "image_id": "docling-p1-1",
                            "record_id": "docling-p1-1",
                            "source_run_id": "abcdef",
                            "page": 1,
                            "raw_type": "table",
                            "relationship": "source_table_crop",
                            "crop_path": "docling/visuals/docling-p1-1.png",
                        }
                    ],
                }
            ],
        }

        with TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "abcdef" / "docling" / "visuals" / "docling-p1-1.png"
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"table-image")

            candidates, missing, unresolved = adb_vector_store._image_embedding_candidates(
                [chunk],
                output_dir=tmp,
            )

        self.assertEqual(missing, 0)
        self.assertEqual(unresolved, set())
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].image_id, "docling-p1-1")
        self.assertEqual(candidates[0].image_path.name, "docling-p1-1.png")

    def test_save_chunk_run_embeddings_rolls_back_and_counts_errors(self):
        chunk_run = _chunk_run()
        connection = _FakeConnection()
        settings = replace(
            get_settings(),
            embedding_model="cohere.embed-v4.0",
            embedding_output_dimensions=1024,
        )

        def failing_embedder(texts, settings_value, *, input_type):
            raise RuntimeError("embedding down")

        result = save_chunk_run_embeddings(
            chunk_run,
            settings,
            embedder=failing_embedder,
            connection=connection,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.created_count, 0)
        self.assertEqual(result.skipped_count, 0)
        self.assertEqual(result.error_count, 2)
        self.assertEqual(connection.commit_count, 0)
        self.assertEqual(connection.rollback_count, 1)
        self.assertIn("embedding down", result.errors[-1])

    def test_save_keeps_committed_batches_when_a_later_batch_fails_and_resumes(self):
        # 全 batch を1 transaction にすると、終盤の一時障害で取得済みの embedding を全て失う (#458)。
        chunk_run = _chunk_run()
        connection = _FakeConnection()
        settings = replace(
            get_settings(),
            embedding_model="cohere.embed-v4.0",
            embedding_output_dimensions=1024,
            embedding_batch_size=1,
        )
        embedded = []

        def flaky_embedder(texts, settings_value, *, input_type):
            if "new retrieval" in texts:
                raise TimeoutError("embedding timeout")
            embedded.extend(texts)
            return [[0.1] * 1024 for _ in texts]

        result = save_chunk_run_embeddings(chunk_run, settings, embedder=flaky_embedder, connection=connection)

        self.assertFalse(result.ok)
        self.assertEqual(result.created_count, 1)
        self.assertEqual(result.error_count, 1)
        self.assertIn("embedding timeout", result.errors[-1])
        self.assertEqual(connection.commit_count, 1)
        self.assertEqual(connection.rollback_count, 1)
        self.assertEqual(len(connection.embedding_rows), 1)
        # 途中で失敗した run は既存文書の latest として公開しない。
        self.assertEqual([binds["publish"] for binds in connection.document_binds], [0])

        resumed = _FakeConnection(
            existing_hashes={
                _chunk_uid(chunk_run.chunk_run_id, chunk_run.chunks[0].chunk_id): _retrieval_text_hash(chunk_run.chunks[0]),
            }
        )
        calls = []

        def embedder(texts, settings_value, *, input_type):
            calls.append(list(texts))
            return [[0.2] * 1024 for _ in texts]

        result = save_chunk_run_embeddings(chunk_run, settings, embedder=embedder, connection=resumed)

        self.assertTrue(result.ok)
        self.assertEqual(calls, [["new retrieval"]])
        self.assertEqual((result.created_count, result.skipped_count), (1, 1))
        self.assertEqual(resumed.document_binds[-1]["publish"], 1)

    def test_save_isolates_chunks_rejected_by_the_embedder_and_blank_text(self):
        chunk_run = _chunk_run()
        blank = replace(chunk_run.chunks[0], chunk_id="chunk-docling-c000009", text="", retrieval_text=" ")
        chunk_run = replace(chunk_run, chunks=[*chunk_run.chunks, blank])
        connection = _FakeConnection()
        settings = replace(
            get_settings(),
            embedding_model="cohere.embed-v4.0",
            embedding_output_dimensions=1024,
            embedding_batch_size=8,
        )

        def embedder(texts, settings_value, *, input_type):
            if "new retrieval" in texts:
                raise ValueError("input is too long")
            return [[0.1] * 1024 for _ in texts]

        result = save_chunk_run_embeddings(chunk_run, settings, embedder=embedder, connection=connection)

        self.assertEqual(result.created_count, 1)
        self.assertEqual(result.error_count, 2)
        self.assertTrue(any("chunk-docling-c000009" in error for error in result.errors))
        self.assertTrue(any("chunk-docling-c000002: input is too long" in error for error in result.errors))
        self.assertNotIn("chunk-docling-c000009", [row["chunk_id"] for row in connection.chunk_rows])
        self.assertEqual(
            [row["chunk_uid"] for row in connection.embedding_rows], [_chunk_uid(chunk_run.chunk_run_id, "chunk-docling-c000001")]
        )
        # 入力起因で除外した chunk があっても、保存できた範囲を latest として公開する。
        self.assertEqual(connection.document_binds[-1]["publish"], 1)
        self.assertEqual(connection.rollback_count, 0)

    def test_save_deactivates_unselected_chunks_and_prunes_stale_text_vectors(self):
        # MERGE だけでは、外した engine の chunk が active のまま検索に残り、古い hash の vector も消えない (#462)。
        chunk_run = _chunk_run()
        connection = _FakeConnection()
        settings = replace(get_settings(), embedding_model="cohere.embed-v4.0", embedding_output_dimensions=1024)

        def embedder(texts, settings_value, *, input_type):
            return [[0.1] * 1024 for _ in texts]

        result = save_chunk_run_embeddings(chunk_run, settings, preferred_engine_ids=["docling"], embedder=embedder, connection=connection)

        self.assertTrue(result.ok)
        order = [name for name, _ in connection.statements]
        deactivate = order.index("UPDATE rag_chunks SET")
        # 全 chunk を無効化してから対象だけを MERGE で有効に戻す。commit 前なので途中状態は見えない。
        self.assertLess(deactivate, order.index("MERGE INTO rag_chunks"))
        self.assertEqual(connection.statements[deactivate][1], {"chunk_run_id": chunk_run.chunk_run_id})
        self.assertTrue(all(row["active"] == "Y" for row in connection.chunk_rows))
        text_prunes = [row for row in connection.deleted_embedding_rows if row["embedding_modality"] == "text"]
        self.assertEqual(
            {(row["chunk_uid"], row["embedding_text_hash"]) for row in text_prunes},
            {
                (_chunk_uid(chunk_run.chunk_run_id, chunk.chunk_id), _retrieval_text_hash(chunk))
                for chunk in chunk_run.chunks
                if chunk.chunk_level == CHILD_CHUNK_LEVEL
            },
        )

    def test_save_clears_chunk_cache_after_commit_and_after_partial_failure(self):
        # 冒頭の clear だけでは、commit までに走った検索が保存前の chunk 一覧を 120 秒 cache し直す (#465)。
        chunk_run = _chunk_run()
        settings = replace(get_settings(), embedding_model="cohere.embed-v4.0", embedding_output_dimensions=1024)

        def searching_embedder(texts, settings_value, *, input_type):
            adb_vector_store._pool_cache["concurrent-search"] = (0.0, [])
            return [[0.1] * 1024 for _ in texts]

        def failing_embedder(texts, settings_value, *, input_type):
            adb_vector_store._pool_cache["concurrent-search"] = (0.0, [])
            raise TimeoutError("embedding timeout")

        for embedder in (searching_embedder, failing_embedder):
            with self.subTest(embedder.__name__):
                save_chunk_run_embeddings(chunk_run, settings, embedder=embedder, connection=_FakeConnection())
                self.assertEqual(adb_vector_store._pool_cache, {})

    def test_delete_source_documents_removes_the_document_rows_and_clears_the_pool_cache(self):
        # rag_documents を消せば ON DELETE CASCADE で chunk run・chunk・embedding も消える (#555)。
        connection = _FakeConnection()
        adb_vector_store._pool_cache["stale"] = (0.0, [])
        adb_vector_store.delete_source_documents(
            get_settings(),
            source_file_sha256="a" * 64,
            source_file_name="manual.pdf",
            document_ids=["doc-1", "doc-1", "doc-2"],
            connection=connection,
        )
        name, binds = connection.statements[-1]
        self.assertEqual(name, "DELETE FROM rag_documents")
        self.assertEqual(
            binds, {"source_file_sha256": "a" * 64, "source_file_name": "manual.pdf", "document_id_0": "doc-1", "document_id_1": "doc-2"}
        )
        self.assertEqual(connection.commit_count, 1)
        self.assertEqual(adb_vector_store._pool_cache, {})

    def test_text_embedding_lookups_are_limited_to_text_rows_when_the_schema_has_modality(self):
        # image 行を text の既存 hash として数えると、status が missing を stale と表示する (#465)。
        chunk_run = _chunk_run()
        # text 行だけを検証する。ローカル .env の IMAGE_EMBEDDING_ENABLED=true を拾うと image の prune が混ざる。
        settings = replace(
            get_settings(),
            embedding_model="cohere.embed-v4.0",
            embedding_output_dimensions=1024,
            image_embedding_enabled=False,
        )

        def embedder(texts, settings_value, *, input_type):
            return [[0.1] * 1024 for _ in texts]

        for has_column, expected in ((True, "text"), (False, None)):
            with self.subTest(has_modality_column=has_column):
                connection = _FakeConnection()
                connection.has_modality_column = has_column
                result = save_chunk_run_embeddings(chunk_run, settings, embedder=embedder, connection=connection)
                load_chunk_embedding_status(chunk_run, settings, connection=connection)
                self.assertTrue(result.ok)
                lookups = [binds for name, binds in connection.statements if name == "SELECT chunk_uid, embedding_text_hash"]
                self.assertEqual([binds.get("embedding_modality") for binds in lookups], [expected, expected])
                # modality 列のない旧 schema でも text vector の prune が ORA-00904 にならない。
                self.assertEqual({row.get("embedding_modality") for row in connection.deleted_embedding_rows}, {expected})

    def test_image_embedding_hash_does_not_depend_on_the_output_directory(self):
        chunk = _chunk_run().chunks[0]
        image = {"image_id": "abcdef-picture-1"}
        hashes = set()
        for _ in range(2):
            with TemporaryDirectory() as tmp:
                path = Path(tmp) / "picture.png"
                path.write_bytes(b"same-image")
                hashes.add(adb_vector_store._image_embedding_hash(chunk, image, path))
        self.assertEqual(len(hashes), 1)

    def test_resaving_an_older_chunk_run_keeps_the_newer_published_run(self):
        # 保存のたびに latest を上書きすると、古い run の再保存で検索対象が黙って巻き戻る (#460)。
        chunk_run = _chunk_run()
        settings = replace(get_settings(), embedding_model="cohere.embed-v4.0", embedding_output_dimensions=1024)

        def embedder(texts, settings_value, *, input_type):
            return [[0.1] * 1024 for _ in texts]

        cases = {
            "newer run stays published": (("newer-run", "9999-01-01T00:00:00+00:00"), "newer-run", [0]),
            "older run is replaced": (("older-run", "2000-01-01T00:00:00+00:00"), "", [0, 1]),
            "same run is republished": ((chunk_run.chunk_run_id, "9999-01-01T00:00:00+00:00"), "", [0, 1]),
            "legacy row without timestamp is replaced": (("legacy-run", None), "", [0, 1]),
            "new document": (None, "", [0, 1]),
        }
        for name, (published_run, kept, publish_flags) in cases.items():
            with self.subTest(name):
                connection = _FakeConnection()
                connection.published_run = published_run
                result = save_chunk_run_embeddings(chunk_run, settings, embedder=embedder, connection=connection)
                self.assertTrue(result.ok)
                self.assertEqual(result.kept_latest_chunk_run_id, kept)
                self.assertEqual([binds["publish"] for binds in connection.document_binds], publish_flags)
                self.assertEqual(len(connection.embedding_rows), 2)
                self.assertEqual("newer-run" in format_embedding_save_result(result), bool(kept))
        self.assertEqual(
            json.loads(connection.document_binds[-1]["metadata_json"])["chunk_run_created_at_utc"],
            chunk_run.created_at_utc,
        )

    def test_save_deactivates_the_documents_other_chunk_runs(self):
        # 公開中 run 以外が active のまま残ると、active が公開状態を表さなくなる (#1062)。
        chunk_run = _chunk_run()
        settings = replace(get_settings(), embedding_model="cohere.embed-v4.0", embedding_output_dimensions=1024)

        def embedder(texts, settings_value, *, input_type):
            return [[0.1] * 1024 for _ in texts]

        cases = {
            "保存した run を公開": (None, chunk_run.chunk_run_id),
            "より新しい run が公開中なら、そちらを残す": (("newer-run", "9999-01-01T00:00:00+00:00"), "newer-run"),
        }
        for name, (published_run, expected_kept) in cases.items():
            with self.subTest(name):
                connection = _FakeConnection()
                connection.published_run = published_run
                result = save_chunk_run_embeddings(chunk_run, settings, embedder=embedder, connection=connection)
                self.assertTrue(result.ok)
                for table in ("rag_chunks", "rag_chunk_runs"):
                    binds = [b for head, b in connection.statements if head == f"UPDATE {table} SET" and "document_id" in b]
                    self.assertEqual(
                        binds,
                        [{"document_id": result.document_id, "chunk_run_id": expected_kept}],
                        table,
                    )

    def test_save_preserves_existing_image_embedding_when_local_asset_is_missing(self):
        chunk_run = _chunk_run()
        target = chunk_run.chunks[1]
        target.metadata = {
            "schema_version": 4,
            "active": True,
            "image_evidence": [
                {
                    "image_id": "docling-p1-2",
                    "source_run_id": "abcdef",
                    "page": 1,
                    "crop_path": "docling/vision/missing.png",
                }
            ],
        }
        target_uid = _chunk_uid(chunk_run.chunk_run_id, target.chunk_id)
        connection = _FakeConnection(
            existing_hashes={
                _chunk_uid(chunk_run.chunk_run_id, chunk_run.chunks[0].chunk_id): _retrieval_text_hash(chunk_run.chunks[0]),
                target_uid: _retrieval_text_hash(target),
                (adb_vector_store.EMBEDDING_MODALITY_IMAGE, target_uid): "existing-image-hash",
            }
        )

        with TemporaryDirectory() as tmp:
            settings = replace(
                get_settings(),
                output_dir=Path(tmp),
                embedding_output_dimensions=1024,
                image_embedding_enabled=True,
            )
            result = save_chunk_run_embeddings(
                chunk_run,
                settings,
                connection=connection,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.image_missing_asset_count, 1)
        self.assertNotIn(
            target_uid, {row["chunk_uid"] for row in connection.deleted_embedding_rows if row["embedding_modality"] == "image"}
        )
        self.assertEqual(connection.commit_count, 1)

    def test_save_chunk_run_embeddings_rejects_non_schema_dimension(self):
        chunk_run = _chunk_run()
        connection = _FakeConnection()
        settings = replace(
            get_settings(),
            embedding_model="cohere.embed-v4.0",
            # Oracle の列（1024）と違う次元。#46 より前の既定の 1536 で設定したまま。
            embedding_output_dimensions=1536,
        )
        embed_calls = []

        def embedder(texts, settings_value, *, input_type):
            embed_calls.append(texts)
            return [[0.0] * 1536 for _ in texts]

        result = save_chunk_run_embeddings(
            chunk_run,
            settings,
            embedder=embedder,
            connection=connection,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.created_count, 0)
        self.assertEqual(result.error_count, 2)
        self.assertEqual(embed_calls, [])
        self.assertEqual(connection.rollback_count, 1)
        self.assertIn("expects 1024 dimensions", result.errors[0])

    def test_format_embedding_save_result_lists_created_skipped_and_errors(self):
        result = adb_vector_store.EmbeddingSaveResult(
            source_run_id="abcdef",
            chunk_run_id="chunk-run",
            document_id="doc",
            child_count=3,
            parent_count=1,
            created_count=2,
            skipped_count=1,
            error_count=0,
        )

        text = format_embedding_save_result(result)

        self.assertIn("作成・更新した embedding（created/updated）: 2", text)
        self.assertIn("再利用した embedding（skipped）: 1", text)
        self.assertIn("エラー（errors）: 0", text)
        self.assertNotIn("child chunks", text)  # 直上のチャンキング結果と重複するため出さない (#816)
        self.assertIn("knowledge base 公開（published）: エラーで中断したため公開していません", text)

    def test_format_embedding_save_result_reports_model_and_publication(self):
        # 既存データと混在できるかの判断にモデル・次元を、次の操作の判断に公開状態を出す (#816)。
        published = adb_vector_store.EmbeddingSaveResult(
            source_run_id="abcdef",
            chunk_run_id="chunk-run",
            document_id="doc",
            child_count=3,
            parent_count=1,
            created_count=3,
            skipped_count=0,
            error_count=0,
            published=True,
            embedding_model="cohere.embed-v4.0",
            embedding_dimensions=1024,
        )
        text = format_embedding_save_result(published)
        self.assertIn("embedding モデル（model）: cohere.embed-v4.0 / 1024 次元（dimensions）", text)
        self.assertIn("knowledge base 公開（published）: この run を検索対象として公開しました", text)
        kept = adb_vector_store.EmbeddingSaveResult(
            source_run_id="abcdef",
            chunk_run_id="chunk-run",
            document_id="doc",
            child_count=3,
            parent_count=1,
            created_count=3,
            skipped_count=0,
            error_count=0,
            kept_latest_chunk_run_id="newer-run",
        )
        self.assertIn("より新しい run `newer-run` を公開中のため", format_embedding_save_result(kept))

    def test_load_chunk_embedding_status_counts_saved_missing_and_stale_embeddings(self):
        chunk_run = _chunk_run()
        first_uid = _chunk_uid(chunk_run.chunk_run_id, chunk_run.chunks[0].chunk_id)
        second_uid = _chunk_uid(chunk_run.chunk_run_id, chunk_run.chunks[1].chunk_id)
        connection = _FakeConnection(
            existing_hashes={
                first_uid: _retrieval_text_hash(chunk_run.chunks[0]),
                second_uid: "stale-hash",
            }
        )
        settings = replace(
            get_settings(),
            embedding_model="cohere.embed-v4.0",
            embedding_output_dimensions=1024,
        )

        status = load_chunk_embedding_status(
            chunk_run,
            settings,
            preferred_engine_ids=["docling"],
            connection=connection,
        )
        text = format_embedding_status_result(status)

        self.assertFalse(status.ready)
        self.assertEqual(status.child_count, 2)
        self.assertEqual(status.parent_count, 1)
        self.assertEqual(status.embedded_count, 1)
        self.assertEqual(status.missing_count, 0)
        self.assertEqual(status.stale_count, 1)
        self.assertIn("本文 embedding 保存済み（saved text embeddings）: 1 / 2", text)
        self.assertIn("本文 embedding 再作成待ち（stale）: 1", text)
        self.assertIn(f"embedding モデル（model）: {settings.embedding_model} / {settings.embedding_output_dimensions} 次元", text)
        self.assertNotIn("child chunks", text)

    def test_load_chunk_embedding_status_reports_legacy_schema_rows(self):
        # v4 移行後、旧 schema のまま残る ADB 行は検索対象外。件数と案内を出し ready にしない (#819)。
        chunk_run = _chunk_run()
        settings = replace(get_settings(), embedding_output_dimensions=1024, image_embedding_enabled=False)
        uid = _chunk_uid(chunk_run.chunk_run_id, chunk_run.chunks[1].chunk_id)
        connection = _FakeConnection(existing_hashes={uid: _retrieval_text_hash(chunk_run.chunks[1])})
        connection.legacy_rows = 2
        status = load_chunk_embedding_status(chunk_run, settings, preferred_engine_ids=["docling"], connection=connection)
        text = format_embedding_status_result(status)
        self.assertEqual(status.legacy_row_count, 2)
        self.assertFalse(status.ready)
        self.assertIn("旧 schema の chunk 行（legacy rows）: 2 件", text)
        self.assertIn("状態（status）: 旧 schema（legacy）", text)
        connection.legacy_rows = 0
        self.assertNotIn(
            "legacy",
            format_embedding_status_result(
                load_chunk_embedding_status(chunk_run, settings, preferred_engine_ids=["docling"], connection=connection)
            ),
        )

    def test_load_chunk_embedding_status_counts_missing_image_embeddings(self):
        chunk_run = _chunk_run()
        chunk_run.chunks[1].metadata = {
            "schema_version": 4,
            "active": True,
            "image_evidence": [
                {
                    "image_id": "docling-p1-2",
                    "source_run_id": "abcdef",
                    "page": 1,
                    "crop_path": "docling/vision/docling-p1-2.png",
                }
            ],
        }
        first_uid = _chunk_uid(chunk_run.chunk_run_id, chunk_run.chunks[0].chunk_id)
        second_uid = _chunk_uid(chunk_run.chunk_run_id, chunk_run.chunks[1].chunk_id)
        connection = _FakeConnection(
            existing_hashes={
                first_uid: _retrieval_text_hash(chunk_run.chunks[0]),
                second_uid: _retrieval_text_hash(chunk_run.chunks[1]),
            }
        )

        with TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "abcdef" / "docling" / "vision" / "docling-p1-2.png"
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"image-bytes")
            settings = replace(
                get_settings(),
                output_dir=Path(tmp),
                embedding_model="cohere.embed-v4.0",
                embedding_output_dimensions=1024,
                image_embedding_enabled=True,
            )

            status = load_chunk_embedding_status(
                chunk_run,
                settings,
                preferred_engine_ids=["docling"],
                connection=connection,
            )

        text = format_embedding_status_result(status)

        self.assertFalse(status.ready)
        self.assertEqual(status.embedded_count, 2)
        self.assertEqual(status.missing_count, 0)
        self.assertEqual(status.stale_count, 0)
        self.assertTrue(status.image_embedding_enabled)
        self.assertEqual(status.image_candidate_count, 1)
        self.assertEqual(status.image_embedded_count, 0)
        self.assertEqual(status.image_missing_count, 1)
        self.assertEqual(status.image_stale_count, 0)
        self.assertEqual(status.image_missing_asset_count, 0)
        self.assertIn("本文 embedding 保存済み（saved text embeddings）: 2 / 2", text)
        self.assertIn("画像 embedding（image embeddings）: 有効（enabled）", text)
        self.assertIn("画像 embedding 候補（candidates）: 1", text)
        self.assertIn("画像 embedding 保存済み（saved）: 0 / 1", text)
        self.assertIn("画像 embedding 未保存（missing）: 1", text)
        self.assertIn("状態（status）: 一部のみ（partial）", text)

    def test_load_chunk_embedding_status_counts_stale_image_embeddings(self):
        chunk_run = _chunk_run()
        chunk_run.chunks[1].metadata = {
            "schema_version": 4,
            "active": True,
            "image_evidence": [
                {
                    "image_id": "docling-p1-2",
                    "source_run_id": "abcdef",
                    "page": 1,
                    "crop_path": "docling/vision/docling-p1-2.png",
                }
            ],
        }
        first_uid = _chunk_uid(chunk_run.chunk_run_id, chunk_run.chunks[0].chunk_id)
        second_uid = _chunk_uid(chunk_run.chunk_run_id, chunk_run.chunks[1].chunk_id)
        connection = _FakeConnection(
            existing_hashes={
                first_uid: _retrieval_text_hash(chunk_run.chunks[0]),
                second_uid: _retrieval_text_hash(chunk_run.chunks[1]),
                (adb_vector_store.EMBEDDING_MODALITY_IMAGE, second_uid): "stale-image-hash",
            }
        )

        with TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "abcdef" / "docling" / "vision" / "docling-p1-2.png"
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"image-bytes")
            settings = replace(
                get_settings(),
                output_dir=Path(tmp),
                embedding_model="cohere.embed-v4.0",
                embedding_output_dimensions=1024,
                image_embedding_enabled=True,
            )

            status = load_chunk_embedding_status(
                chunk_run,
                settings,
                preferred_engine_ids=["docling"],
                connection=connection,
            )

        text = format_embedding_status_result(status)

        self.assertFalse(status.ready)
        self.assertEqual(status.image_candidate_count, 1)
        self.assertEqual(status.image_embedded_count, 0)
        self.assertEqual(status.image_missing_count, 0)
        self.assertEqual(status.image_stale_count, 1)
        self.assertIn("画像 embedding 再作成待ち（stale）: 1", text)

    def test_adb_search_only_uses_embeddings_matching_current_search_text_hash(self):
        source = Path(adb_vector_store.__file__).read_text(encoding="utf-8")

        self.assertGreaterEqual(source.count("e.embedding_text_hash = c.search_text_hash"), 3)

    def test_image_embedding_candidates_deduplicate_shared_form_page(self):
        chunk_run = _chunk_run()
        image = {
            "image_id": "abcdef-form-page-1",
            "source_run_id": "abcdef",
            "page": 1,
            "raw_type": "form_page",
            "asset_kind": "page",
            "visual_kind": "form",
        }
        chunks = [
            replace(chunk_run.chunks[0], metadata={"image_evidence": [image]}),
            replace(chunk_run.chunks[1], metadata={"image_evidence": [image]}),
        ]

        with TemporaryDirectory() as tmp:
            page_path = Path(tmp) / "abcdef" / "pages" / "page_0001.png"
            page_path.parent.mkdir(parents=True)
            page_path.write_bytes(b"page-image")
            candidates, missing_count, unresolved = adb_vector_store._image_embedding_candidates(
                chunks,
                output_dir=tmp,
            )

        self.assertEqual(missing_count, 0)
        self.assertEqual(unresolved, set())
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].image_id, "abcdef-form-page-1")
        self.assertEqual(candidates[0].chunk.chunk_id, chunks[0].chunk_id)


class _FakeConnection:
    def __init__(self, existing_hashes=None):
        self.existing_hashes = existing_hashes or {}
        self.commit_count = 0
        self.rollback_count = 0
        self.chunk_rows = []
        self.embedding_rows = []
        self.deleted_embedding_rows = []
        self.document_binds = []
        self.document_input_sizes = []
        self.documents = set()  # rag_documents に存在する document_id。UPDATE の rowcount と INSERT の ORA-00001 に使う
        self.document_statements = []  # rag_documents への文の先頭語（UPDATE / INSERT）
        self.statements = []  # 実行順の検証用: (SQL の先頭語句, binds)
        self.legacy_rows = 0  # metadata_json.schema_version が現行版でない active 行の件数 (#819)
        self.published_run = None  # (latest_chunk_run_id, chunk_run_created_at_utc)
        self.has_modality_column = True  # False: migrate_image_embedding_modality.sql 未適用の旧 schema

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1


class _FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []
        self.input_sizes = {}
        self.rowcount = 0

    def setinputsizes(self, **kwargs):
        self.input_sizes.update(kwargs)

    def execute(self, sql, binds=None):
        binds = binds or {}
        self.connection.statements.append((" ".join(sql.split()[:3]), binds))
        if "UPDATE rag_documents" in sql or "INSERT INTO rag_documents" in sql:
            kind = sql.split()[0]
            self.connection.document_statements.append(kind)
            # 1 回の _upsert_document を document_binds の 1 件にまとめる。0 行の UPDATE に続く INSERT は
            # 同じ呼び出しなので直前の記録を置き換える。publish は latest を書く UPDATE だけ 1。
            recorded = dict(binds, publish=1 if kind == "UPDATE" and "chunk_run_id" in binds else 0)
            if kind == "INSERT":
                self.connection.document_binds[-1] = recorded
                self.connection.document_input_sizes[-1] = dict(self.input_sizes)
                if binds["document_id"] in self.connection.documents:
                    raise RuntimeError("ORA-00001: unique constraint violated")
                self.connection.documents.add(binds["document_id"])
            else:
                self.connection.document_binds.append(recorded)
                self.connection.document_input_sizes.append(dict(self.input_sizes))
                self.rowcount = 1 if binds["document_id"] in self.connection.documents else 0
        if "FROM user_tab_cols" in sql:
            self.rows = [(1 if self.connection.has_modality_column else 0,)]
        elif "SELECT COUNT(*)" in sql and "$.schema_version" in sql:
            self.rows = [(self.connection.legacy_rows,)]
        elif "FROM rag_documents" in sql and "SELECT latest_chunk_run_id" in sql:
            self.rows = [self.connection.published_run] if self.connection.published_run else []
        elif "SELECT chunk_uid, embedding_text_hash" in sql:
            chunk_uids = [value for key, value in binds.items() if key.startswith("chunk_uid_")]
            modality = binds.get("embedding_modality")
            self.rows = []
            for chunk_uid in chunk_uids:
                lookup_key = (modality, chunk_uid) if modality else chunk_uid
                if lookup_key in self.connection.existing_hashes:
                    hashes = self.connection.existing_hashes[lookup_key]
                elif modality and modality != "text":
                    continue  # chunk_uid だけの key は text 行を表す
                elif chunk_uid in self.connection.existing_hashes:
                    hashes = self.connection.existing_hashes[chunk_uid]
                else:
                    continue
                if isinstance(hashes, (list, tuple, set)):
                    self.rows.extend((chunk_uid, text_hash) for text_hash in hashes)
                else:
                    self.rows.append((chunk_uid, hashes))
        else:
            self.rows = []
        return self

    def executemany(self, sql, rows):
        rows = list(rows)
        self.connection.statements.append((" ".join(sql.split()[:3]), rows))
        if "MERGE INTO rag_chunks" in sql:
            self.connection.chunk_rows.extend(rows)
        elif "MERGE INTO rag_chunk_embeddings" in sql:
            self.connection.embedding_rows.extend(rows)
        elif "DELETE FROM rag_chunk_embeddings" in sql:
            self.connection.deleted_embedding_rows.extend(rows)
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class _ConnectionContext:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, tb):
        return False


class _CaptureConnection:
    def __init__(self, rows=None):
        self.cursor_obj = _CaptureCursor(rows=rows)

    def cursor(self):
        return self.cursor_obj


class _CaptureCursor:
    def __init__(self, rows=None):
        self.sql = ""
        self.binds = {}
        self.rows = list(rows or [])
        self.input_sizes = {}

    def setinputsizes(self, **kwargs):
        self.input_sizes.update(kwargs)

    def execute(self, sql, binds=None):
        self.sql = sql
        self.binds = binds or {}
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


def _chunk_run() -> ChunkingResult:
    chunks = [
        DocumentChunk(
            chunk_id="chunk-docling-c000001",
            chunk_level=CHILD_CHUNK_LEVEL,
            chunk_seq=1,
            parent_chunk_id="chunk-docling-p000001",
            child_chunk_ids=[],
            text="existing text",
            retrieval_text="existing retrieval",
            char_count=17,
            token_estimate=5,
            source_run_id="abcdef",
            source_file_name="manual.pdf",
            source_engine_id="docling",
            source_engine_label="Docling",
            page_start=1,
            page_end=1,
            source_seq_ranges=[{"page": 1, "seq_start": 1, "seq_end": 1}],
            source_record_refs=[{"record_id": "r1", "page": 1, "seq_no": 1, "category": "Text"}],
            metadata={"schema_version": 4, "active": True},
        ),
        DocumentChunk(
            chunk_id="chunk-docling-c000002",
            chunk_level=CHILD_CHUNK_LEVEL,
            chunk_seq=2,
            parent_chunk_id="chunk-docling-p000001",
            child_chunk_ids=[],
            text="new text",
            retrieval_text="new retrieval",
            char_count=13,
            token_estimate=4,
            source_run_id="abcdef",
            source_file_name="manual.pdf",
            source_engine_id="docling",
            source_engine_label="Docling",
            page_start=1,
            page_end=1,
            source_seq_ranges=[{"page": 1, "seq_start": 2, "seq_end": 2}],
            source_record_refs=[{"record_id": "r2", "page": 1, "seq_no": 2, "category": "Text"}],
            metadata={"schema_version": 4, "active": True},
        ),
        DocumentChunk(
            chunk_id="chunk-docling-p000001",
            chunk_level=PARENT_CHUNK_LEVEL,
            chunk_seq=1,
            parent_chunk_id="",
            child_chunk_ids=["chunk-docling-c000001", "chunk-docling-c000002"],
            text="parent text",
            retrieval_text="parent retrieval",
            char_count=16,
            token_estimate=5,
            source_run_id="abcdef",
            source_file_name="manual.pdf",
            source_engine_id="docling",
            source_engine_label="Docling",
            page_start=1,
            page_end=1,
            source_seq_ranges=[{"page": 1, "seq_start": 1, "seq_end": 2}],
            source_record_refs=[{"record_id": "r1", "page": 1, "seq_no": 1, "category": "Text"}],
            metadata={"schema_version": 4, "active": True},
        ),
    ]
    return ChunkingResult(
        source_run_id="abcdef",
        chunk_run_id="abcdef-1234567890ab",
        source_file_name="manual.pdf",
        selected_engine_ids=["docling"],
        config=ChunkingConfig(),
        config_hash="1234567890ab",
        created_at_utc="2026-09-03T00:00:00+00:00",
        active=True,
        source_file_sha256="0" * 64,
        source_page_count=1,
        chunks=chunks,
        json_path="",
        jsonl_path="",
        latest_path="",
    )


def _stored_chunk(
    chunk_uid: str,
    chunk_id: str,
    *,
    page_start: int,
    chunk_seq: int,
    metadata=None,
) -> StoredChunk:
    stored_metadata = dict(metadata or {"active": True})
    stored_metadata.setdefault("schema_version", 4)
    return StoredChunk(
        chunk_uid=chunk_uid,
        chunk_id=chunk_id,
        chunk_level=CHILD_CHUNK_LEVEL,
        chunk_seq=chunk_seq,
        parent_chunk_uid="parent",
        parent_chunk_id="parent",
        child_chunk_ids=(),
        text="text",
        retrieval_text="retrieval",
        source_run_id="abcdef",
        source_file_name="manual.pdf",
        source_engine_id="docling",
        source_engine_label="Docling",
        page_start=page_start,
        page_end=page_start,
        source_seq_ranges=(),
        source_record_refs=(),
        metadata=stored_metadata,
    )


if __name__ == "__main__":
    unittest.main()


def test_display_regions_are_stored_once_and_restored_on_load():
    """表示領域は専用列だけに保存し、読込時に metadata へ戻す。旧形式の行もそのまま読める。"""
    import json
    from types import SimpleNamespace

    from rag_engine.adapters.oracle.store import _chunk_row, _stored_chunk_from_row

    regions = [{"page": 1, "boxes": [{"bbox": [1.0, 2.0, 3.0, 4.0]}]}]
    chunk = SimpleNamespace(
        chunk_id="c1",
        chunk_level="child",
        chunk_seq=1,
        parent_chunk_id="",
        child_chunk_ids=[],
        source_run_id="run",
        source_file_name="a.pdf",
        source_engine_id="docling",
        source_engine_label="Docling",
        page_start=1,
        page_end=1,
        source_seq_ranges=[],
        source_record_refs=[],
        text="本文",
        retrieval_text="本文",
        char_count=2,
        token_estimate=1,
        metadata={"active": True, "atomic": False, "section_path": ["章"], "layout": {"display_regions": regions}},
    )
    row = _chunk_row(SimpleNamespace(chunk_run_id="cr"), "doc", chunk)
    assert "layout" not in json.loads(row["metadata_json"])  # display_regions だけの layout は行へ残さない
    assert json.loads(row["display_regions_json"]) == regions
    assert chunk.metadata["layout"]["display_regions"] == regions  # 入力は変更しない
    loaded = _stored_chunk_from_row(
        [
            "cr:c1",
            "c1",
            "child",
            1,
            "",
            "",
            "[]",
            "本文",
            "本文",
            "run",
            "a.pdf",
            "docling",
            "Docling",
            1,
            1,
            "[]",
            "[]",
            row["display_regions_json"],
            row["metadata_json"],
        ]
    )
    assert loaded.metadata["layout"]["display_regions"] == regions and loaded.metadata["section_path"] == ["章"]


def test_first_page_context_is_stored_once_per_document_and_restored_on_load():
    """同じ第1ページ本文を全 chunk 行へ複製しない。旧形式の行が持つ値は上書きしない。"""
    import json
    from types import SimpleNamespace

    from rag_engine.adapters.oracle.store import StoredChunk, _first_page_contexts, _restore_first_page_contexts, _row_metadata

    context = {"page": 1, "status": "available", "text": "表紙の本文", "engine": "docling"}
    metadata = {"active": True, "document": {"title": "資料", "first_page_context": context}, "layout": {"display_regions": [{"page": 1}]}}
    row = _row_metadata(metadata)
    assert row == {"active": True, "document": {"title": "資料"}}
    assert metadata["document"]["first_page_context"] is context  # 入力は変更しない
    chunks = [
        SimpleNamespace(metadata=metadata, source_engine_id="docling"),
        SimpleNamespace(metadata=metadata, source_engine_id="docling"),
    ]
    assert _first_page_contexts(chunks) == {"docling": context}

    def stored(uid, document):
        return StoredChunk(
            chunk_uid=uid,
            chunk_id=uid,
            chunk_level="child",
            chunk_seq=1,
            parent_chunk_uid="",
            parent_chunk_id="",
            child_chunk_ids=(),
            text="t",
            retrieval_text="t",
            source_run_id="r",
            source_file_name="a.pdf",
            source_engine_id="docling",
            source_engine_label="Docling",
            page_start=2,
            page_end=2,
            source_seq_ranges=(),
            source_record_refs=(),
            metadata={"document": document},
        )

    new, legacy, unknown = stored("n", {"title": "資料"}), stored("l", {"first_page_context": {"text": "旧"}}), stored("u", {})
    queries = []

    class Cursor:
        def execute(self, sql, binds):
            queries.append(sorted(binds.values()))

        def __iter__(self):
            return iter([("doc-1", json.dumps({"first_page_contexts": {"docling": context}}))])

    _restore_first_page_contexts(SimpleNamespace(cursor=Cursor), [new, legacy, unknown], ["doc-1", "doc-1", "doc-2"])
    assert queries == [["doc-1", "doc-2"]]  # 文書ごとに1回だけ問い合わせる
    assert new.metadata["document"] == {"title": "資料", "first_page_context": context}
    assert legacy.metadata["document"]["first_page_context"] == {"text": "旧"}
    assert "first_page_context" not in unknown.metadata["document"]


def test_profile_channel_can_be_switched_off_for_ablation():
    from dataclasses import replace as _replace

    from rag_engine.adapters.oracle import store

    store._pool_cache.clear()
    settings = _replace(get_settings(), embedding_output_dimensions=1024, image_embedding_enabled=False, profile_channel_enabled=False)
    with (
        patch("rag_engine.adapters.oracle.store.load_adb_settings", return_value="adb-profile"),
        patch("rag_engine.adapters.oracle.store.connect_adb_thin", side_effect=lambda *_: _ConnectionContext(object())),
        patch("rag_engine.adapters.oracle.store.load_domain_keywords", return_value=[]),
        patch("rag_engine.adapters.oracle.store._available_embedding_count", return_value=1),
        patch("rag_engine.adapters.oracle.store._vector_search", return_value=[("uid-1", 0.1)]),
        patch("rag_engine.adapters.oracle.store._text_search", return_value=[]),
        patch(
            "rag_engine.adapters.oracle.store._load_chunk_run_chunks",
            return_value=[_stored_chunk("uid-1", "chunk-1", page_start=1, chunk_seq=1)],
        ),
        patch("rag_engine.adapters.oracle.store.profile_channel_rankings") as profile,
    ):
        result = search_adb_hybrid_chunks(
            chunk_run_id="run-p", retrieval_queries=["質問"], settings=settings, query_embedder=lambda *_: [0.1] * 1024
        )
    profile.assert_not_called()
    assert [c.chunk_id for c in result.child_chunks] == ["chunk-1"]
    store._pool_cache.clear()


class LegacySchemaDocumentCountTests(unittest.TestCase):
    """旧 schema の文書数は検索と同じ範囲条件で数え、検索前検査の戻り値で呼出側へ渡す (#948)。"""

    def test_knowledge_base_scope_counts_documents_on_their_latest_chunk_run(self):
        connection = _CaptureConnection(rows=[(3,)])
        count = adb_vector_store._legacy_schema_document_count(connection, "", retrieval_scope=RETRIEVAL_SCOPE_KNOWLEDGE_BASE)
        self.assertEqual(count, 3)
        sql, binds = connection.cursor_obj.sql, connection.cursor_obj.binds
        self.assertIn("COUNT(DISTINCT c.document_id)", sql)
        self.assertIn("d.latest_chunk_run_id = c.chunk_run_id", sql)
        self.assertIn("<> :chunk_metadata_schema_version", sql)
        self.assertNotIn(":chunk_run_id", sql)
        self.assertEqual(binds["chunk_metadata_schema_version"], CHUNK_METADATA_SCHEMA_VERSION)

    def test_current_run_scope_binds_the_chunk_run_id(self):
        connection = _CaptureConnection(rows=[(0,)])
        count = adb_vector_store._legacy_schema_document_count(connection, "run-1")
        self.assertEqual(count, 0)
        self.assertIn("c.chunk_run_id = :chunk_run_id", connection.cursor_obj.sql)
        self.assertEqual(connection.cursor_obj.binds["chunk_run_id"], "run-1")

    def test_ready_check_returns_the_legacy_document_count_without_raising(self):
        settings = replace(get_settings(), embedding_output_dimensions=1024)
        with (
            patch("rag_engine.adapters.oracle.store.load_adb_settings", return_value="adb"),
            patch("rag_engine.adapters.oracle.store.connect_adb_thin", side_effect=lambda *_: _ConnectionContext(object())),
            patch("rag_engine.adapters.oracle.store._available_embedding_count", return_value=1),
            patch("rag_engine.adapters.oracle.store._legacy_schema_document_count", return_value=2),
        ):
            readiness = adb_vector_store.check_adb_hybrid_search_ready(settings=settings, retrieval_scope=RETRIEVAL_SCOPE_KNOWLEDGE_BASE)
        self.assertEqual(readiness, adb_vector_store.AdbSearchReadiness(legacy_document_count=2))
