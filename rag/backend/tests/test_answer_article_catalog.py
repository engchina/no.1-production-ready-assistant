"""条の目録から根拠の条を選ぶ段(#2254)の、backend が注入する目録と条の chunk の読み込み。"""

import json
from typing import Any

import pytest
from rag_engine.retrieval.article_catalog import ARTICLE_CATALOG_KEY, SelectedArticle

from app.clients.section_map_store import ReadableChunkSet
from app.config import Settings
from app.rag.answer_engine import AnswerEngine, _SearchState
from app.rag.section_chunk_map import ROOT_SECTION_ID, SectionNode
from app.rag.section_map_service import SectionOutline
from app.schemas.search import RetrievedChunk, SearchRequest
from tests.test_answer_engine import FakeGenAi, FakeOracle, _chunk

_FILE = "架空規程.json"


def _article_chunk(chunk_id: str, label: str, text: str) -> RetrievedChunk:
    chunk = _chunk(chunk_id, text, 1)
    return chunk.model_copy(
        update={
            "document_id": "doc-law",
            "file_name": _FILE,
            "metadata": {
                **chunk.metadata,
                "chunk_group_id": f"{chunk_id}-p",
                "parent_text": text,
                "xml_tree_label_arabic": label,
                "chunk_set_id": "cs-law",
            },
        }
    )


def _node(section_id: str, title: str, level: int, ordinal: int) -> SectionNode:
    return SectionNode(
        section_id=section_id,
        title=title,
        level=level,
        ordinal=ordinal,
        parent_section_id=None if section_id == ROOT_SECTION_ID else ROOT_SECTION_ID,
        page_start=None,
        page_end=None,
    )


OUTLINE = SectionOutline(
    chunk_set=ReadableChunkSet(
        document_id="doc-law", chunk_set_id="cs-law", file_name=_FILE, superseded=False
    ),
    sections=[
        _node(ROOT_SECTION_ID, "架空規程", 0, 0),
        _node("s1", "第一条（目的）", 1, 1),
        _node("s2", "第二条（届出）", 1, 2),
    ],
)


def _engine(oracle: Any) -> AnswerEngine:
    return AnswerEngine(Settings(), oracle=oracle, genai=FakeGenAi())  # type: ignore[arg-type]


async def test_article_catalogs_read_only_documents_with_article_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """候補のうち引用ラベルを持つ文書だけの目録を、順位の順に上限まで作る。

    1 回の回答で 1 回だけ読む。
    """
    from app.rag import section_catalog, section_map_service

    outline_calls: list[str] = []

    async def section_outline(document_id: str, **kwargs: Any) -> SectionOutline:
        outline_calls.append(document_id)
        return OUTLINE

    async def compact(
        oracle: Any, outline: Any, nodes: Any, lead_chars: int, *, max_chars: int
    ) -> tuple[list[str], int]:
        assert [node.section_id for node in nodes] == ["s1", "s2"]  # 根の章節は目録に入れない
        return [node.title for node in nodes], 0

    monkeypatch.setattr(section_map_service, "section_outline", section_outline)
    monkeypatch.setattr(section_catalog, "compact_section_lines", compact)
    engine = _engine(FakeOracle())
    state = _SearchState()
    manual = _chunk("doc-1:c1", "手引きの本文", 1)
    article = _article_chunk("law:c1", "第1条", "第一条 この規程は…")
    state.chunks = {manual.chunk_id: manual, article.chunk_id: article}

    catalogs = await engine._article_catalogs(state, ["doc-1", "doc-law", "doc-other"], 3)
    again = await engine._article_catalogs(state, ["doc-law"], 3)

    assert [(c.document_id, c.file_name, c.chunk_set_id) for c in catalogs] == [
        ("doc-law", _FILE, "cs-law")
    ]
    assert catalogs[0].lines == ("第一条（目的）", "第二条（届出）")
    assert catalogs[0].section_ids == ("s1", "s2")
    assert again == catalogs
    assert outline_calls == ["doc-law"]


async def test_article_catalogs_skip_documents_that_fail_to_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.rag import section_map_service

    async def failing(document_id: str, **kwargs: Any) -> SectionOutline:
        raise RuntimeError("DB")

    monkeypatch.setattr(section_map_service, "section_outline", failing)
    state = _SearchState()
    article = _article_chunk("law:c1", "第1条", "第一条")
    state.chunks = {article.chunk_id: article}

    assert await _engine(FakeOracle())._article_catalogs(state, ["doc-law"], 3) == []


async def test_article_chunks_mark_the_first_child_of_each_selected_article(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """選んだ条の章節の最初の child に印を付け、親と一緒に返す。

    分類・1 ページ目の本文も付け、state に入れる。
    """
    from app.clients import section_map_store

    first = _article_chunk("law:c2", "第2条", "第二条 届出は…")
    requested: list[tuple[str, str, list[str]]] = []

    class Store:
        def __init__(self, oracle: Any) -> None:
            pass

        async def section_first_chunks(
            self, document_id: str, *, chunk_set_id: str, section_ids: list[str]
        ) -> dict[str, RetrievedChunk]:
            requested.append((document_id, chunk_set_id, list(section_ids)))
            return {"s2": first}

    monkeypatch.setattr(section_map_store, "SectionMapStore", Store)
    oracle = FakeOracle()
    oracle.classifications = {"doc-law": {"large_category": "業務A"}}
    engine = _engine(oracle)
    state = _SearchState()
    articles = [
        SelectedArticle("doc-law", _FILE, "cs-law", "s2", "第二条（届出）", 0),
        SelectedArticle("doc-law", _FILE, "cs-law", "s9", "第九条", 1),  # 読めない条は足さない
    ]

    stored = await engine._article_chunks(SearchRequest(query="q"), state, articles)

    assert requested == [("doc-law", "cs-law", ["s2", "s9"])]
    children = [chunk for chunk in stored if chunk.chunk_level == "child"]
    assert [chunk.chunk_uid for chunk in children] == ["law:c2"]
    assert children[0].metadata[ARTICLE_CATALOG_KEY] == {
        "order": 0,
        "document": _FILE,
        "title": "第二条（届出）",
    }
    assert children[0].metadata["document"]["classification"] == {"large_category": "業務A"}
    assert any(chunk.chunk_level == "parent" for chunk in stored)
    assert "law:c2" in state.chunks
    # 読めなかった条も選んだ条として診断に残す(chunk_id は無し)。
    assert list(state.article_selections.values()) == [
        {
            "order": 0,
            "document_id": "doc-law",
            "document": _FILE,
            "title": "第二条（届出）",
            "chunk_id": "law:c2",
        },
        {
            "order": 1,
            "document_id": "doc-law",
            "document": _FILE,
            "title": "第九条",
            "chunk_id": None,
        },
    ]
    assert ARTICLE_CATALOG_KEY not in state.chunks["law:c2"].metadata


async def test_article_leads_return_the_body_lead_without_the_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """条の選択の 2 段目の書き出し: 章節の最初の chunk から名前を除いた本文。読めなければ空。"""
    from app.clients import section_map_store

    first = _article_chunk(
        "law:c5",
        "第5条",
        "第五条（請求の特例） 前条の請求があつたときは、特例の請求があつたものとみなす。",
    )

    class Store:
        def __init__(self, oracle: Any) -> None:
            pass

        async def section_first_chunks(
            self, document_id: str, *, chunk_set_id: str, section_ids: list[str]
        ) -> dict[str, RetrievedChunk]:
            assert (document_id, chunk_set_id, section_ids) == ("doc-law", "cs-law", ["s5", "s6"])
            return {"s5": first}

    monkeypatch.setattr(section_map_store, "SectionMapStore", Store)
    engine = _engine(FakeOracle())

    leads = await engine._article_leads(
        "doc-law", "cs-law", {"s5": "第五条（請求の特例）", "s6": "第六条（委任）"}
    )

    assert list(leads) == ["s5"]
    assert leads["s5"].startswith("前条の請求があつたときは")

    class Broken(Store):
        async def section_first_chunks(
            self, *args: Any, **kwargs: Any
        ) -> dict[str, RetrievedChunk]:
            raise RuntimeError("db")

    monkeypatch.setattr(section_map_store, "SectionMapStore", Broken)
    assert await engine._article_leads("doc-law", "cs-law", {"s5": "第五条"}) == {}


def test_engine_settings_pass_the_article_catalog_options(tmp_path: Any) -> None:
    from app.rag.answer_engine import build_engine_settings

    settings = Settings(
        rag_article_catalog_enabled=False,
        rag_article_catalog_context_records=4,
        rag_article_catalog_output_token_cap=1024,
        rag_article_catalog_call_timeout_seconds=20,
        rag_article_catalog_refine_articles=0,
        rag_article_catalog_review_enabled=True,
    )
    engine_settings = build_engine_settings(
        settings, output_dir=tmp_path, runtime_knowledge_path=None
    )

    assert engine_settings.article_catalog_enabled is False
    assert engine_settings.article_catalog_output_token_cap == 1024
    assert engine_settings.article_catalog_call_timeout_seconds == 20.0
    assert engine_settings.article_catalog_refine_articles == 0
    assert engine_settings.article_catalog_review is True
    assert (
        build_engine_settings(
            Settings(), output_dir=tmp_path, runtime_knowledge_path=None
        ).article_catalog_review
        is False
    )
    assert engine_settings.article_catalog_refine_candidates == 60
    assert engine_settings.article_catalog_context_records == 4
    assert json.loads(json.dumps(engine_settings.article_catalog_max_documents)) == 16
    # テストでは process をまたぐ記録の置き場所を渡さない（conftest。#3507）。
    assert engine_settings.article_catalog_cache_dir is None


def test_engine_settings_pass_the_shared_article_catalog_record(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """条の選択の記録(#3507)の置き場所・有効期間・件数を rag_engine へ渡す。"""
    from app.rag import answer_engine

    settings = Settings(
        rag_article_catalog_cache_ttl_seconds=600,
        rag_article_catalog_cache_max_entries=32,
    )
    monkeypatch.setattr(
        answer_engine, "article_catalog_cache_dir", answer_engine.resolve_article_catalog_cache_dir
    )
    settings.local_storage_dir = str(tmp_path / "storage")
    engine_settings = answer_engine.build_engine_settings(settings, output_dir=tmp_path)

    assert engine_settings.article_catalog_cache_dir == (
        tmp_path / "storage" / "cache" / "rag-article-catalog"
    )
    assert engine_settings.article_catalog_cache_ttl_seconds == 600.0
    assert engine_settings.article_catalog_cache_max_entries == 32

    settings.rag_article_catalog_cache_dir = str(tmp_path / "explicit")
    assert answer_engine.resolve_article_catalog_cache_dir(settings) == tmp_path / "explicit"
    settings.rag_article_catalog_cache_dir = ""
    settings.local_storage_dir = ""
    assert answer_engine.resolve_article_catalog_cache_dir(settings) is None
    defaults = Settings()
    assert (
        defaults.rag_article_catalog_cache_ttl_seconds,
        defaults.rag_article_catalog_cache_max_entries,
    ) == (86400, 4096)


async def test_article_documents_are_read_once_per_scope_and_index_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """範囲の条で構成された文書の一覧は、検索と同じ条件で読み、(条件, 索引の状態) ごとに cache。"""
    monkeypatch.setattr("app.rag.answer_engine._ARTICLE_DOCUMENTS_CACHE", {})
    calls: list[dict[str, str]] = []

    class Oracle(FakeOracle):
        async def retrieval_scope_state(self, filters: dict[str, str]) -> str:
            return "1:1"

        async def retrieval_article_documents(
            self, filters: dict[str, str]
        ) -> list[tuple[str, str]]:
            calls.append(dict(filters))
            return [("doc-law", _FILE)]

    engine = _engine(Oracle())
    request = SearchRequest(query="q", filters={"knowledge_base_id": "kb-1"})

    first = await engine._article_documents(request, _SearchState())
    second = await engine._article_documents(request, _SearchState())

    assert first == second == [("doc-law", _FILE)]
    assert calls == [{"knowledge_base_id": "kb-1"}]
