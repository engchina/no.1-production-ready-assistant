"""SDK の文書同一性と、モデルへ渡す証拠・引用の回帰を検証する。"""

import hashlib
import json
import os
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from grounded_stub import AnswerOutput

from rag_engine.chunking.service import ChunkingService
from rag_engine.chunking.storage import chunk_payload, chunk_result_from_payload, load_chunk_run_by_id, persist_batch
from rag_engine.composition import create_oracle_application
from rag_engine.config import get_settings
from rag_engine.generation.answering import MAX_CONTEXT_CHARS, AnswerRecord
from rag_engine.models.contracts import ChunkRequest, Evidence, GenerationRequest, ParsedDocument
from rag_engine.models.layout import LayoutRecord


def output(evidence_id=""):
    """実 API 呼出を置き換える構造化回答を返す。"""
    return AnswerOutput(
        answer="supported",
        confidence="high",
        question_type=[],
        cited_evidence=[dict(evidence_id=evidence_id, source="guide.pdf", page="1", look_at="row", visible_evidence="value")]
        if evidence_id
        else [],
        reasoning_summary="",
        insufficient_reason="",
        needs_human_review=False,
    )


class DocumentIdentityTests(TestCase):
    def test_external_document_ids_survive_storage_and_reindex_without_colliding(self):
        from rag_engine.adapters.oracle.store import _document_id

        record = LayoutRecord("p1", "external", 1, 1, [0, 0, 10, 10], "image_top_left", 10, 10, "Text", "evidence")
        with TemporaryDirectory() as directory:
            identities = []
            for document_id in ("abcdef", "123456", "abcdef"):
                document = ParsedDocument(document_id, "same.pdf", (record,), page_count=1)
                batch = ChunkingService().chunk(ChunkRequest(document))
                saved = persist_batch(batch, output_dir=directory)
                restored = load_chunk_run_by_id(directory, saved.chunk_run_id)
                self.assertEqual(restored.source_file_sha256, "")
                self.assertEqual(_document_id(saved), _document_id(restored))
                identities.append(_document_id(restored))
            self.assertNotEqual(identities[0], identities[1])
            self.assertEqual(identities[0], identities[2])

    def test_file_based_and_historical_identities_remain_unchanged(self):
        from rag_engine.adapters.oracle.store import _document_id

        document = ParsedDocument("abcdef", "same.pdf", (), page_count=1)
        with TemporaryDirectory() as directory:
            saved = persist_batch(ChunkingService().chunk(ChunkRequest(document)), output_dir=directory)
            for digest in ("", "actual-pdf-hash"):
                payload = chunk_payload(replace(saved, source_file_sha256=digest))
                if not digest:
                    payload.pop("source_document_id", None)
                historical = chunk_result_from_payload(payload)
                expected = "doc-" + hashlib.sha256(f"{digest}|same.pdf|1".encode()).hexdigest()[:32]
                self.assertEqual(_document_id(historical), expected)


class GeneratorEvidenceTests(TestCase):
    def test_complete_text_of_every_parent_reaches_one_generation_prompt(self):
        from grounded_stub import echo_model

        from rag_engine.generation.citation_wire import generation_draft_wire_model
        from rag_engine.generation.grounded import numbered_text

        evidence = tuple(Evidence(f"p{n}", f"START_{n}\n" + "A" * 2900 + f"TAIL_{n}") for n in range(6))
        with TemporaryDirectory() as directory, create_oracle_application(directory) as app:
            with patch("rag_engine.generation.answering.parse_text_response", side_effect=echo_model) as parse:
                app.generator.generate(GenerationRequest("question", evidence))
            prompts = [call.args[1] for call in parse.call_args_list if call.args[3] is generation_draft_wire_model()]
            self.assertEqual(len(prompts), 1)
            for item in evidence:
                # 長い根拠は行番号を付けて示す（#2830）。番号の付いた全文が 1 回の prompt に入る。
                self.assertIn(numbered_text(item.text), prompts[0], item.id)

    def test_oversized_evidence_returns_explicit_insufficiency_without_model_call(self):
        # 上限判定は親本文の長さそのもの。ちょうど上限の本文は通り、1 文字でも超えれば拒否する。
        for evidence in (
            (Evidence("big", "A" * (MAX_CONTEXT_CHARS + 1)),),
            (Evidence("small", "valid text"), Evidence("big", "A" * (MAX_CONTEXT_CHARS + 1))),
        ):
            with self.subTest(count=len(evidence)), TemporaryDirectory() as directory, create_oracle_application(directory) as app:
                with patch("rag_engine.generation.answering.parse_text_response") as parse:
                    result = app.generator.generate(GenerationRequest("question", evidence))
                parse.assert_not_called()
                self.assertTrue(result.insufficient_reason)
                self.assertTrue(result.needs_human_review)

    def test_citation_is_the_evidence_whose_text_was_quoted(self):
        """引用元はモデルの自己申告ではなく、原文照合できた引用の出典から決まる。"""
        from grounded_stub import echo_model

        record = AnswerRecord(
            id="parent",
            engine="external",
            engine_label="external",
            page=1,
            seq_no=1,
            category="Table",
            text="table text",
            source="guide.pdf",
            chunk_uid="unique-parent",
        )
        owner = Evidence("sdk-id", record.text, record.source, 1, {"record": asdict(record)})
        with (
            TemporaryDirectory() as directory,
            create_oracle_application(directory) as app,
            patch("rag_engine.generation.answering.parse_text_response", side_effect=echo_model),
        ):
            result = app.generator.generate(GenerationRequest("question", (owner, Evidence("other", "unrelated"))))
        self.assertEqual(result.citations, (owner,))
        self.assertIn("table text", result.text)


class FaqSettingsTests(TestCase):
    def test_dotenv_flags_are_isolated_and_explicit_overrides_win(self):
        with TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("APPROVED_FAQ_SEMANTIC_MATCHING_ENABLED=false\nAPPROVED_FAQ_SEMANTIC_SUGGESTIONS_ENABLED=true\n")
            before = dict(os.environ)
            settings = get_settings(environ={}, dotenv_path=env_file)
            self.assertFalse(settings.approved_faq_semantic_matching_enabled)
            self.assertTrue(settings.approved_faq_semantic_suggestions_enabled)
            other = get_settings(environ={}, dotenv_path=None)
            self.assertTrue(other.approved_faq_semantic_matching_enabled)
            self.assertFalse(other.approved_faq_semantic_suggestions_enabled)
            overridden = get_settings(
                environ={"APPROVED_FAQ_SEMANTIC_MATCHING_ENABLED": "true"},
                dotenv_path=env_file,
                approved_faq_semantic_suggestions_enabled=False,
            )
            self.assertTrue(overridden.approved_faq_semantic_matching_enabled)
            self.assertFalse(overridden.approved_faq_semantic_suggestions_enabled)
            self.assertEqual(dict(os.environ), before)


class CitationScopeTests(TestCase):
    """局所 ID が重複しても、引用元の文書・ページ境界を越えない。"""

    def evidence(self, identity, source, page=1, *, run_id="", end=None):
        record = AnswerRecord(
            id=identity,
            engine="docling",
            engine_label="Docling",
            page=page,
            seq_no=1,
            category="Table",
            text=f"Evidence from {identity}",
            source=source,
            source_run_id=run_id,
            chunk_uid=f"{identity}-uid",
            page_end=end,
            source_record_refs=({"record_id": "local-record", "page": page},),
            metadata={"image_evidence": [{"image_id": "local-image", "page": page}]},
        )
        return Evidence(identity, record.text, source, page, {"record": asdict(record)})

    def generate(self, evidence, *citations):
        """公開した引用の出典から引用元を解決する純関数を直接確認する。"""
        from rag_engine.generation.citations import resolve_citations

        records = [
            AnswerRecord(**item.metadata["record"])
            if "record" in item.metadata
            else AnswerRecord(
                id=item.id,
                engine="external",
                engine_label="external",
                page=item.page,
                seq_no=index + 1,
                category="Text",
                text=item.text,
                source=item.source,
            )
            for index, item in enumerate(evidence)
        ]
        return resolve_citations(
            evidence, records, [dict(evidence_id=identity, source=source, page=page) for identity, source, page in citations]
        )

    def test_duplicate_local_ids_only_cite_requested_document(self):
        a, b = self.evidence("a", "a.pdf"), self.evidence("b", "b.pdf")
        for identity in ("local-record", "local-image"):
            with self.subTest(identity=identity):
                self.assertEqual(self.generate((a, b), (identity, "a.pdf", "1")), (a,))
                self.assertEqual(self.generate((a, b), (identity, "b.pdf", "1")), (b,))
        a, b = self.evidence("shared-parent", "a.pdf"), self.evidence("shared-parent", "b.pdf")
        self.assertEqual(self.generate((a, b), ("shared-parent", "a.pdf", "1")), (a,))
        self.assertEqual(self.generate((a, b), ("shared-parent", "", "")), ())

    def test_page_disambiguates_repeated_ids_in_same_document(self):
        a, b = self.evidence("a", "guide.pdf", 1), self.evidence("b", "guide.pdf", 2)
        for page in ("2", "p.2", "２"):
            with self.subTest(page=page):
                self.assertEqual(self.generate((a, b), ("local-record", "guide.pdf", page)), (b,))
        self.assertEqual(self.generate((a, b), ("local-image", "guide.pdf", "3")), ())

    def test_ambiguous_or_contradictory_scope_does_not_fabricate_citations(self):
        a = self.evidence("a", "guide.pdf", run_id="aaaaaa")
        b = self.evidence("b", "guide.pdf", run_id="bbbbbb")
        for citation in (
            ("local-image", "", ""),
            ("local-image", "guide.pdf", "1"),
            ("local-record", "missing.pdf", "1"),
            ("a-uid", "wrong.pdf", "1"),
            ("a-uid", "guide.pdf", "unknown"),
        ):
            with self.subTest(citation=citation):
                self.assertEqual(self.generate((a, b), citation), ())
        self.assertEqual(self.generate((a, b), ("a-uid", "guide.pdf", "1")), (a,))
        self.assertEqual(self.generate((a, b), ("b", "", "")), (b,))

    def test_image_page_is_checked_within_multi_page_parent(self):
        a = self.evidence("a", "guide.pdf", 1, end=3)
        b = self.evidence("b", "guide.pdf", 2, end=3)
        self.assertEqual(self.generate((a, b), ("local-image", "guide.pdf", "2")), (b,))
        self.assertEqual(self.generate((a,), ("a-uid", "guide.pdf", "p.2-3")), (a,))
        self.assertEqual(self.generate((a,), ("a-uid", "guide.pdf", "2-4")), ())

    def test_unique_citation_without_scope_and_multiple_scoped_uses(self):
        a, b = self.evidence("a", "a.pdf"), self.evidence("b", "b.pdf")
        self.assertEqual(self.generate((a,), ("local-record", "", "")), (a,))
        self.assertEqual(
            self.generate((a, b), ("local-image", "b.pdf", "1"), ("local-image", "a.pdf", "1"), ("local-image", "a.pdf", "1")), (a, b)
        )
        self.assertEqual(self.generate((a, b), ("a", "a.pdf", "1"), ("local-image", "", "")), (a,))

    def test_basename_requires_unique_document_and_qualified_path_is_respected(self):
        a = self.evidence("a", "folder-a/guide.pdf")
        b = self.evidence("b", "folder-b/guide.pdf")
        self.assertEqual(self.generate((a,), ("local-image", "guide.pdf", "1")), (a,))
        self.assertEqual(self.generate((a, b), ("local-image", "guide.pdf", "1")), ())
        self.assertEqual(self.generate((a, b), ("local-image", "folder-b/guide.pdf", "1")), (b,))

    def test_explicit_identity_is_not_reinterpreted_as_another_local_id(self):
        a = self.evidence("local-image", "a.pdf")
        b = self.evidence("b", "b.pdf")
        self.assertEqual(self.generate((a, b), ("local-image", "", "")), (a,))
        self.assertEqual(self.generate((a, b), ("local-image", "b.pdf", "1")), ())

    def test_query_pipeline_keeps_citation_for_duplicate_retrieval_hits(self):
        from copy import deepcopy
        from unittest.mock import Mock

        from rag_engine.models.contracts import SearchRequest, SearchResult
        from rag_engine.workflows import QueryPipeline

        e = self.evidence("parent", "guide.pdf", run_id="abcdef")
        from grounded_stub import echo_model

        with TemporaryDirectory() as directory, create_oracle_application(directory) as app:
            for hits in ((e,), (e, e), (e, deepcopy(e))):
                with self.subTest(hits=len(hits)), patch("rag_engine.generation.answering.parse_text_response", side_effect=echo_model):
                    retriever = Mock(retrieve=Mock(return_value=SearchResult(hits)))
                    result = QueryPipeline(retriever, app.generator).run(SearchRequest("question", "corpus"))
                    self.assertEqual(result.citations, (e,))
                    self.assertIs(result.citations[0], e)

    def test_duplicate_chunk_with_scores_and_aliases_returns_first_evidence_once(self):
        from copy import deepcopy

        a = self.evidence("a", "a.pdf", run_id="aaaaaa")
        b = self.evidence("b", "b.pdf", run_id="bbbbbb")
        metadata = deepcopy(dict(a.metadata))
        metadata["record"]["metadata"]["adb_hybrid"] = {"rrf_score": 0.9}
        metadata["record"]["metadata"]["rerank"] = {"rank": 1}
        metadata["score"] = 0.9
        metadata = json.loads(json.dumps(metadata))
        duplicate = replace(a, id="another-sdk-alias", metadata=metadata)
        citations = self.generate((a, b, duplicate), ("another-sdk-alias", "a.pdf", "1"), ("a-uid", "a.pdf", "1"), ("b-uid", "b.pdf", "1"))
        self.assertEqual(citations, (a, b))
        self.assertIs(citations[0], a)
        self.assertEqual(metadata["record"]["metadata"]["adb_hybrid"], {"rrf_score": 0.9})

    def test_identical_unscoped_evidence_is_deduplicated(self):
        from copy import deepcopy

        e = Evidence("plain", "plain evidence", "guide.pdf", 1)
        self.assertEqual(self.generate((e, deepcopy(e)), ("plain", "guide.pdf", "1")), (e,))

    def test_duplicates_do_not_hide_distinct_or_conflicting_sources(self):
        from copy import deepcopy

        a = self.evidence("a", "guide.pdf", run_id="aaaaaa")
        for name, value in (
            ("source_run_id", "bbbbbb"),
            ("source", "other.pdf"),
            ("page", 2),
            ("chunk_uid", "another-uid"),
            ("text", "contradictory evidence"),
            ("source_record_refs", [{"record_id": "local-record", "page": 1, "bbox": [1, 2, 3, 4]}]),
        ):
            metadata = deepcopy(dict(a.metadata))
            metadata["record"][name] = value
            other = replace(a, metadata=metadata)
            with self.subTest(changed=name):
                self.assertEqual(self.generate((a, a, other), ("a", "", "")), ())
        b = self.evidence("b", "guide.pdf", run_id="bbbbbb")
        self.assertEqual(self.generate((a, a, b), ("local-image", "guide.pdf", "1")), ())
        self.assertEqual(self.generate((a, a, b), ("a-uid", "guide.pdf", "1")), (a,))


def load_tests(loader, tests, pattern):
    """既存CIの最小依存入口から、回答保持の回帰も省略せず実行する。"""
    from test_supported_explanations import SupportedExplanationsTests

    tests.addTests(loader.loadTestsFromTestCase(SupportedExplanationsTests))
    return tests
