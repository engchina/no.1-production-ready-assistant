"""XML ツリー JSON の取込の経路と分割(#2024)。

- 経路: XML ツリー JSON は使用エンジン(全体の既定・処理レシピ)で選んだときだけ使う(#2042)。
  Docling のまま JSON を取り込むと、ほかの非対応形式と同じく取込前に止める。XML ツリー JSON を
  選んで対応しない形の JSON を渡すと、扱えない旨のエラーで止める。
- 分割: 親子階層(small-to-big)は番号つきの単位 = 親・段落 = 子。構造認識も段落ごとに分け、
  検索の文に引用ラベルの漢数字と算用数字の両方を入れる。
- 出典: 回答の根拠の場所を引用ラベルにし、原典の URL はひな形があるときだけ付ける。
fixture は合成の XML ツリー JSON(rag_parser_core の tests/fixtures/xml_tree_sample.json)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from rag_engine.retrieval.article_catalog import ARTICLE_UNIT_KEY
from rag_parser_core.registry import ParserRegistryResult
from rag_parser_core.xml_tree_json import xml_tree_json_extraction
from rag_pipeline_core.chunking import Chunk, chunk_extraction_with_strategy

from app.config import Settings
from app.rag.answer_engine import _stored_child, _xml_tree_location_labels
from app.rag.chunking_small_to_big import (
    build_parent_child_chunks,
    has_xml_tree_units,
    small_to_big_parent_source,
)
from app.rag.ingestion import IngestionPipeline
from app.rag.parser_source_guard import parser_source_block
from app.rag.source_profile import build_source_profile
from app.rag.variant_keys import extraction_recipe_subset
from app.rag.xml_tree_source_urls import apply_xml_tree_source_urls
from app.schemas.document import SourceProfile
from app.schemas.search import RetrievedChunk

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "rag_parser_core"
    / "tests"
    / "fixtures"
    / "xml_tree_sample.json"
)
TITLE = "サンプル規程"


def _settings(**updates: object) -> Settings:
    base = Settings().model_copy(
        update={
            "rag_parser_adapter_backend": "docling",
            "rag_parser_docling_enabled": True,
            "rag_parser_unstructured_enabled": False,
            "rag_preprocess_profile": "passthrough",
        }
    )
    return base.model_copy(update=updates)


def _profile(data: bytes, *, file_name: str = "sample.json") -> SourceProfile:
    return build_source_profile(
        original_file_name=file_name,
        sanitized_file_name=file_name,
        content_type="application/json",
        file_size_bytes=len(data),
        content_sha256="a" * 64,
        data=data,
    )


def _pipeline(settings: Settings) -> IngestionPipeline:
    return IngestionPipeline(
        vlm=cast(Any, object()),
        genai=cast(Any, object()),
        oracle=cast(Any, object()),
        object_storage=cast(Any, object()),
        settings=settings,
    )


def _patch_runner(
    monkeypatch: pytest.MonkeyPatch, called: list[str], *, supported: bool = True
) -> None:
    def _runner(
        self: object,
        backend: str,
        source_bytes: bytes,
        source_profile: SourceProfile | None,
        content_type: str,
        *,
        fail_fast: bool = False,
    ) -> ParserRegistryResult:
        called.append(backend)
        if not supported:
            return ParserRegistryResult(
                extraction=None,
                parser_backend=backend,
                fallback_used=True,
                template=f"{backend}_fallback",
                warnings=("xml_tree_json_unsupported_tree",),
            )
        return ParserRegistryResult(
            extraction=xml_tree_json_extraction(source_bytes),
            parser_backend=backend,
            parser_version="test",
        )

    monkeypatch.setattr(
        "app.clients.parser_service.ParserServiceClient.runner", _runner, raising=True
    )


def test_guard_blocks_json_with_docling_like_other_formats() -> None:
    """XML ツリー JSON も形式で自動には回さない(#2042)。Docling ではほかの JSON と同じく止める。"""
    settings = _settings()
    for data in (FIXTURE.read_bytes(), b'{"items": [1]}'):
        block = parser_source_block(settings, _profile(data))
        assert block is not None and block.backend == "docling"
        assert block.suggested_backend == "unstructured"


def test_guard_allows_json_with_xml_tree_json_engine() -> None:
    settings = _settings(rag_parser_adapter_backend="xml_tree_json")
    assert parser_source_block(settings, _profile(FIXTURE.read_bytes())) is None
    # 対応しない形の JSON は取込の解析で止まる(拡張子では見分けない)。
    assert parser_source_block(settings, _profile(b'{"items": [1]}')) is None
    pdf = build_source_profile(
        original_file_name="sample.pdf",
        sanitized_file_name="sample.pdf",
        content_type="application/pdf",
        file_size_bytes=10,
        content_sha256="b" * 64,
    )
    block = parser_source_block(settings, pdf)
    assert block is not None and block.backend == "xml_tree_json"
    assert "JSON" in block.message


def test_partition_source_does_not_route_tree_json_from_docling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.clients.parser_service import ParserServiceUnavailableError

    called: list[str] = []
    _patch_runner(monkeypatch, called)
    data = FIXTURE.read_bytes()
    with pytest.raises(ParserServiceUnavailableError) as exc_info:
        _pipeline(_settings())._partition_source(
            parse_bytes=data, source_profile=_profile(data), content_type="application/json"
        )
    assert exc_info.value.warning_code == "docling_adapter_source_unsupported"
    assert called == []


def test_partition_source_uses_xml_tree_service_when_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []
    _patch_runner(monkeypatch, called)
    pipeline = _pipeline(_settings(rag_parser_adapter_backend="xml_tree_json"))
    data = FIXTURE.read_bytes()
    result = pipeline._partition_source(
        parse_bytes=data, source_profile=_profile(data), content_type="application/json"
    )
    assert called == ["xml_tree_json"]
    assert result.parser_backend == "xml_tree_json"
    pipeline._raise_if_selected_parser_was_not_used(result)


def test_unsupported_json_with_xml_tree_engine_stops_with_clear_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.rag.ingestion import IngestionUserError

    called: list[str] = []
    _patch_runner(monkeypatch, called, supported=False)
    pipeline = _pipeline(_settings(rag_parser_adapter_backend="xml_tree_json"))
    data = b'{"items": [1]}'
    result = pipeline._partition_source(
        parse_bytes=data, source_profile=_profile(data), content_type="application/json"
    )
    with pytest.raises(IngestionUserError) as exc_info:
        pipeline._raise_if_selected_parser_was_not_used(result)
    message = str(exc_info.value)
    assert "tag / attr / children" in message
    assert "xml_tree_json_unsupported_tree" in message


def _extraction() -> Any:
    extraction = xml_tree_json_extraction(FIXTURE.read_bytes())
    assert extraction is not None
    return extraction


def test_small_to_big_uses_units_as_parents_and_paragraphs_as_children() -> None:
    extraction = _extraction()
    assert has_xml_tree_units(extraction)
    assert small_to_big_parent_source(extraction) == "xml_tree_units"
    chunks = build_parent_child_chunks(extraction, source_name="sample.json")
    by_label = {chunk.metadata["xml_tree_citation_label_arabic"]: chunk for chunk in chunks}
    first = by_label[f"{TITLE} 第7条第1項"]
    second = by_label[f"{TITLE} 第7条第2項"]
    # 同じ単位の段落は同じ親で、親の本文は両方の段落を持つ。
    assert first.metadata["parent_chunk_id"] == second.metadata["parent_chunk_id"]
    assert "第七条第一項" in str(first.metadata["parent_text"])
    assert "第七条第二項" in str(first.metadata["parent_text"])
    other = by_label[f"{TITLE} 第7条の2"]
    assert other.metadata["parent_chunk_id"] != first.metadata["parent_chunk_id"]
    search_text = str(first.metadata["engine_search_text"])
    assert "第七条第一項" in search_text and "第7条第1項" in search_text
    assert "第7条第1項第2号" in search_text
    assert "第7条第1項" in str(first.metadata["xml_tree_lookup_keys"]).splitlines()


def test_small_to_big_splits_long_paragraphs_at_line_boundaries() -> None:
    from app.rag.chunking_strategy import SmallToBigParams

    extraction = _extraction()
    chunks = build_parent_child_chunks(
        extraction, params=SmallToBigParams(child_target_chars=200, parent_target_chars=6000)
    )
    parts = [
        chunk
        for chunk in chunks
        if chunk.metadata["xml_tree_citation_label_arabic"] == f"{TITLE} 別表第1"
    ]
    assert parts
    # 各部分の先頭の行はラベルを繰り返す。
    assert all(chunk.text.splitlines()[0].startswith(f"{TITLE} 別表第一") for chunk in parts)


def test_structure_aware_keeps_one_chunk_per_paragraph() -> None:
    chunks = chunk_extraction_with_strategy(
        _extraction(), strategy="structure_aware", chunk_size=800, overlap=120, min_chars=50
    )
    labels = [chunk.metadata.get("xml_tree_citation_label_arabic") for chunk in chunks]
    assert f"{TITLE} 第7条第1項" in labels and f"{TITLE} 第7条第2項" in labels
    assert all(chunk.metadata.get("engine_search_text") for chunk in chunks)


def test_location_labels_for_answer_citations() -> None:
    paragraph = {
        "xml_tree_document_title": TITLE,
        "xml_tree_label_arabic": "第7条第1項",
        "xml_tree_parent_label_arabic": f"{TITLE} 第7条",
    }
    assert _xml_tree_location_labels(paragraph) == ("第7条第1項", "第7条")
    assert _xml_tree_location_labels({"section_path": "x"}) is None


def test_article_units_are_marked_for_the_answer_audit() -> None:
    """条で構成された文書の段落の根拠に印を付け、回答がその出典の説明を監査に掛ける(#3526)。"""
    paragraph = RetrievedChunk(
        document_id="doc-1",
        chunk_id="chunk-1",
        text="本文",
        score=0.5,
        file_name="rules.xml",
        metadata={"xml_tree_label_arabic": "第7条第1項", "xml_tree_parent_label_arabic": "第7条"},
    )
    other = paragraph.model_copy(update={"metadata": {"section_path": "x"}})
    assert _stored_child(paragraph, rrf_score=0.1).metadata[ARTICLE_UNIT_KEY] is True
    assert ARTICLE_UNIT_KEY not in _stored_child(other, rrf_score=0.1).metadata


def test_source_url_template_is_applied_only_when_https() -> None:
    chunks = [
        Chunk(
            text="a", index=0, start_offset=0, end_offset=1, metadata={"xml_tree_document_id": "D1"}
        ),
        Chunk(text="b", index=1, start_offset=0, end_offset=1, metadata={}),
    ]
    apply_xml_tree_source_urls(chunks, _settings())
    assert "xml_tree_source_url" not in chunks[0].metadata
    apply_xml_tree_source_urls(
        chunks,
        _settings(rag_xml_tree_json_source_url_template="https://example.test/doc/{document_id}"),
    )
    assert chunks[0].metadata["xml_tree_source_url"] == "https://example.test/doc/D1"
    assert "xml_tree_source_url" not in chunks[1].metadata
    plain = [
        Chunk(
            text="a", index=0, start_offset=0, end_offset=1, metadata={"xml_tree_document_id": "D1"}
        )
    ]
    apply_xml_tree_source_urls(
        plain, _settings(rag_xml_tree_json_source_url_template="http://x/{document_id}")
    )
    assert "xml_tree_source_url" not in plain[0].metadata


def test_extraction_recipe_includes_xml_tree_flag_only_for_its_engine() -> None:
    assert "rag_parser_xml_tree_json_enabled" not in extraction_recipe_subset(_settings())
    subset = extraction_recipe_subset(
        _settings(rag_parser_adapter_backend="xml_tree_json", rag_parser_xml_tree_json_enabled=True)
    )
    assert subset["rag_parser_xml_tree_json_enabled"] is True


def test_enqueue_guard_blocks_json_with_docling() -> None:
    """取込の投入でも、Docling の既定のまま JSON を始めない(原本の中身は読まない。#2042)。"""
    import asyncio

    from fastapi import HTTPException

    from app.api.routes import documents as documents_route
    from app.schemas.document import IngestionJobPhase

    profile = _profile(FIXTURE.read_bytes())
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            documents_route._raise_if_parser_source_blocked(
                _settings(), profile, IngestionJobPhase.PREPROCESS
            )
        )
    assert exc_info.value.status_code == 409
    asyncio.run(
        documents_route._raise_if_parser_source_blocked(
            _settings(rag_parser_adapter_backend="xml_tree_json"),
            profile,
            IngestionJobPhase.PREPROCESS,
        )
    )


def test_citation_hits_go_first_without_duplicates() -> None:
    from app.clients.oracle import _with_citation_hits
    from app.schemas.search import RetrievedChunk

    def hit(chunk_id: str, **metadata: object) -> RetrievedChunk:
        return RetrievedChunk(
            document_id="d1", chunk_id=chunk_id, text=chunk_id, score=0.0, metadata=metadata
        )

    fused = [hit("a", vector_rank=1), hit("b", vector_rank=2), hit("c", vector_rank=3)]
    merged = _with_citation_hits([hit("c", retrieval_mode="citation")], fused, 3)
    assert [item.chunk_id for item in merged] == ["c", "a", "b"]
    assert merged[0].metadata["vector_rank"] == 3
    assert merged[0].metadata["retrieval_mode"] == "citation"
    assert _with_citation_hits([], fused, 3) == fused
