"""関係情報（章節・表の見出しの entity と chunk の対応）で回答の検索の候補を足す（#2055）。

関係情報は取込と同じ ``build_graph_index`` で合成の文書（架空の手順書）から作り、Python の表で
``OracleGraphStore`` と同じ規則（範囲の中の chunk だけ・entity ごとに章節の順）を持つ代役で読む。実
Oracle の SQL は ``test_graph_expansion_oracle.py``。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import pytest

from app.config import Settings
from app.rag.chunking import Chunk
from app.rag.graph_expansion import (
    GRAPH_EXPANSION_KEY,
    GRAPH_KIND_NAME,
    GRAPH_KIND_NEIGHBOR,
    GraphChunkRow,
    GraphEntityRow,
    heading_label,
    plan_graph_expansion,
)
from app.rag.graph_index import GraphIndex, build_graph_index
from app.schemas.extraction import StructuredExtraction
from app.schemas.search import RetrievedChunk

CHUNK_SET_ID = "cs-1"
KB = {"knowledge_base_id": "kb-1"}

# 合成の文書（架空の機器の手順書）。章ごとの chunk の数を変え、狭い章節と広い章節を作る。
MANUAL_SECTIONS = [
    ("手順書 A > 第 1 章 目的", 1),
    ("手順書 A > 第 2 章 作業の準備", 3),
    ("手順書 A > 第 3 章 設定の変更", 12),
    ("手順書 A > 第 4 章 作業の記録", 2),
]
GUIDE_SECTIONS = [
    ("手順書 B > 1. 概要", 1),
    ("手順書 B > 2. 作業の記録の保管", 2),
]


def _chunks(sections: Sequence[tuple[str, int]], *, table_at: int | None = None) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path, count in sections:
        for part in range(count):
            index = len(chunks)
            metadata: dict[str, str | int | float | bool | None] = {"section_path": path}
            if table_at == index:
                metadata["content_kind"] = "table"
            text = f"{path} の本文 {part + 1}"
            chunks.append(
                Chunk(
                    text=text,
                    index=index,
                    start_offset=0,
                    end_offset=len(text),
                    metadata=metadata,
                )
            )
    return chunks


class InMemoryGraphStore:
    """``OracleGraphStore`` と同じ規則を Python の表で行う代役。"""

    def __init__(self, documents: dict[str, tuple[list[Chunk], set[str]]]) -> None:
        self.index = GraphIndex()
        self.chunks: dict[str, RetrievedChunk] = {}
        self.kbs: dict[str, set[str]] = {}
        self.calls: list[str] = []
        for document_id, (chunks, kbs) in documents.items():
            built = build_graph_index(
                document_id=document_id,
                knowledge_base_ids=sorted(kbs),
                extraction=StructuredExtraction(raw_text="本文", document_type="手順書"),
                chunks=chunks,
                chunk_set_id=CHUNK_SET_ID,
                document_name=f"{document_id}.pdf",
            )
            self.index.entities.extend(built.entities)
            self.index.entity_chunk_links.extend(built.entity_chunk_links)
            self.kbs[document_id] = kbs
            for chunk in chunks:
                chunk_id = f"{document_id}:{CHUNK_SET_ID}:{chunk.index}"
                self.chunks[chunk_id] = RetrievedChunk(
                    document_id=document_id,
                    chunk_id=chunk_id,
                    text=chunk.text,
                    score=0.0,
                    metadata={**chunk.metadata, "chunk_index": chunk.index},
                )

    def chunk_ids(self, document_id: str) -> list[str]:
        return [key for key in self.chunks if key.startswith(f"{document_id}:")]

    def _visible(self, filters: dict[str, str], chunk_id: str) -> bool:
        chunk = self.chunks.get(chunk_id)
        if chunk is None:
            return False
        kb = filters.get("knowledge_base_id")
        return kb is None or kb in self.kbs.get(chunk.document_id, set())

    def _count(self, entity_id: str) -> int:
        return sum(1 for link in self.index.entity_chunk_links if link.entity_id == entity_id)

    async def graph_seed_entities(
        self, filters: dict[str, str], *, seed_chunk_ids: Sequence[str]
    ) -> list[GraphEntityRow]:
        self.calls.append("seed")
        entities = {entity.entity_id: entity for entity in self.index.entities}
        return [
            GraphEntityRow(
                entity_id=link.entity_id,
                canonical_name=entities[link.entity_id].canonical_name,
                entity_type=entities[link.entity_id].entity_type,
                chunk_count=self._count(link.entity_id),
                seed_chunk_id=link.chunk_id,
            )
            for link in self.index.entity_chunk_links
            if link.chunk_id in seed_chunk_ids and self._visible(filters, link.chunk_id)
        ]

    async def graph_name_entities(
        self, filters: dict[str, str], *, limit: int
    ) -> list[GraphEntityRow]:
        self.calls.append("name")
        visible = {
            link.entity_id
            for link in self.index.entity_chunk_links
            if self._visible(filters, link.chunk_id)
        }
        return [
            GraphEntityRow(
                entity_id=entity.entity_id,
                canonical_name=entity.canonical_name,
                entity_type=entity.entity_type,
                chunk_count=self._count(entity.entity_id),
            )
            for entity in self.index.entities
            if entity.entity_id in visible and entity.entity_type != "document"
        ][:limit]

    async def graph_entity_chunks(
        self, filters: dict[str, str], *, entity_ids: Sequence[str], per_entity: int
    ) -> list[GraphChunkRow]:
        self.calls.append("chunks")
        rows: list[GraphChunkRow] = []
        for entity_id in entity_ids:
            links = sorted(
                (
                    link
                    for link in self.index.entity_chunk_links
                    if link.entity_id == entity_id and self._visible(filters, link.chunk_id)
                ),
                key=lambda link: int(str(self.chunks[link.chunk_id].metadata["chunk_index"])),
            )
            rows.extend(
                GraphChunkRow(entity_id=entity_id, chunk=self.chunks[link.chunk_id])
                for link in links[:per_entity]
            )
        return rows


def _store() -> InMemoryGraphStore:
    return InMemoryGraphStore(
        {
            "doc-a": (_chunks(MANUAL_SECTIONS, table_at=5), {"kb-1"}),
            "doc-b": (_chunks(GUIDE_SECTIONS), {"kb-1"}),
            "doc-other": (_chunks(GUIDE_SECTIONS), {"kb-2"}),
        }
    )


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("手順書 A > 第 2 章 作業の準備", "作業の準備"),
        ("手順書 A > 第 3 章 設定の変更 (table)", "設定の変更"),
        ("手順書 B > 2. 作業の記録の保管", "作業の記録の保管"),
        ("手順書 B > （3） 連絡先", "連絡先"),
        ("手順書 A", "手順書 A"),
    ],
    ids=["chapter", "table", "dotted", "paren", "top"],
)
def test_heading_label_drops_path_numbers_and_kind(name: str, label: str) -> None:
    assert heading_label(name) == label


async def test_neighbor_adds_other_chunks_of_the_narrow_section_nearest_first() -> None:
    store = _store()
    ids = store.chunk_ids("doc-a")
    # 第 2 章（chunk 1〜3）の真ん中の chunk が当たった。
    seed = store.chunks[ids[2]]

    plan = await plan_graph_expansion(
        store,
        dict(KB),
        question="質問",
        seed_chunks=[seed],
        exclude_chunk_ids={ids[2]},
        max_chunks=6,
    )

    assert [item.chunk.chunk_id for item in plan] == [ids[1], ids[3]]
    assert {item.info["kind"] for item in plan} == {GRAPH_KIND_NEIGHBOR}
    assert plan[0].info["entity"] == "手順書 A > 第 2 章 作業の準備"
    assert plan[0].info["from_chunk_id"] == ids[2]
    # 短い質問でも関係情報の有無を記録するが、名前の一致では根拠を追加しない。
    assert store.calls == ["seed", "name", "chunks"]


async def test_neighbor_leaves_wide_sections_to_the_section_expansion() -> None:
    store = _store()
    ids = store.chunk_ids("doc-a")
    # 第 3 章（12 chunk）は広いので広げない。表の chunk（index 5）の表の entity は chunk が 1 つ。
    plan = await plan_graph_expansion(
        store,
        dict(KB),
        question="質問",
        seed_chunks=[store.chunks[ids[8]], store.chunks[ids[5]]],
        exclude_chunk_ids=set(),
        max_chunks=6,
    )

    assert plan == []


async def test_name_match_adds_sections_across_documents_inside_the_scope() -> None:
    store = _store()
    a = store.chunk_ids("doc-a")
    b = store.chunk_ids("doc-b")

    plan = await plan_graph_expansion(
        store,
        dict(KB),
        question="作業の記録の保管は何年ですか？作業の準備も知りたい",
        seed_chunks=[],
        exclude_chunk_ids=set(),
        max_chunks=6,
    )

    chunk_ids = [item.chunk.chunk_id for item in plan]
    # 長い見出し（作業の記録の保管）から。「作業の記録」は長い一致の一部なので使わない。
    # 範囲の外（kb-2）の同じ見出しの文書は足さない。「目的」（2 文字）は照合しない。
    assert chunk_ids == [b[1], b[2], a[1], a[2], a[3]]
    assert all(item.info["kind"] == GRAPH_KIND_NAME for item in plan)
    assert plan[0].info["match"] == "作業の記録の保管"
    assert not any(chunk_id.startswith("doc-other") for chunk_id in chunk_ids)


async def test_budget_is_split_between_name_matches_and_neighbors() -> None:
    store = _store()
    a = store.chunk_ids("doc-a")
    b = store.chunk_ids("doc-b")

    plan = await plan_graph_expansion(
        store,
        dict(KB),
        question="作業の記録の保管と、作業の準備",
        # 第 4 章の 1 つ目が当たった（広げると 2 つ目）。
        seed_chunks=[store.chunks[a[16]]],
        exclude_chunk_ids={a[16], b[2]},
        max_chunks=3,
    )

    # 上限 3 の半分（2）を名前の一致に、残りを広げに。すでに候補にある chunk（b[2]）は足さない。
    assert [item.chunk.chunk_id for item in plan] == [b[1], a[1], a[17]]
    assert [item.info["kind"] for item in plan] == [GRAPH_KIND_NAME, GRAPH_KIND_NAME, "neighbor"]


async def test_no_graph_tables_add_nothing() -> None:
    store = InMemoryGraphStore({})

    plan = await plan_graph_expansion(
        store,
        {},
        question="作業の記録の保管",
        seed_chunks=[RetrievedChunk(document_id="d", chunk_id="d:1", text="本文", score=1.0)],
        exclude_chunk_ids=set(),
        max_chunks=6,
    )

    assert plan == []
    # entity が無ければ chunk を読まない。
    assert store.calls == ["seed", "name"]


def test_settings_default_off_and_query_overlay_can_turn_on() -> None:
    """重ねる仕組み(compose_query_settings)の確かめ。

    検索・回答プロファイルは ON / OFF も上限も重ねない(#3523)。
    """
    from app.rag.kb_adapter_config import KnowledgeBaseQueryConfig, compose_query_settings

    settings = Settings()
    assert settings.rag_graph_expansion_enabled is False
    assert settings.rag_graph_expansion_max_chunks == 6
    composed, changed = compose_query_settings(
        settings,
        [KnowledgeBaseQueryConfig(graph_expansion_enabled=True, graph_expansion_max_chunks=3)],
    )
    assert changed is True
    assert composed.rag_graph_expansion_enabled is True
    assert composed.rag_graph_expansion_max_chunks == 3


# ---- 回答の検索（AnswerEngine）----


def _engine(store: InMemoryGraphStore, hits: list[RetrievedChunk], **settings: Any) -> Any:
    from app.rag.answer_engine import AnswerEngine
    from tests.test_cross_references import FakeGenAi, ReferenceOracle

    return AnswerEngine(
        Settings(
            **{
                "rag_graph_expansion_enabled": True,
                "rag_entity_expansion_enabled": False,
                **settings,
            }
        ),
        oracle=cast(Any, ReferenceOracle(hits, [])),
        genai=cast(Any, FakeGenAi()),
        graph_store=store,
    )


QUESTION = "作業の準備では何をしますか？"


async def test_search_adds_graph_chunks_after_the_top_candidates() -> None:
    from app.rag.answer_engine import _graph_expansion_diagnostics, _SearchState
    from app.schemas.search import SearchRequest

    store = _store()
    a = store.chunk_ids("doc-a")
    hits = [store.chunks[a[16]], store.chunks[a[8]]]
    state = _SearchState()

    result = await _engine(store, hits)._search(
        SearchRequest(query=QUESTION, filters=dict(KB)), state, retrieval_queries=[QUESTION]
    )

    order = [child.chunk_uid for child in result.child_chunks]
    # 上位の候補の後ろに、名前の一致（第 2 章）と広げ（第 4 章の 2 つ目）を足す。
    assert order[:2] == [hit.chunk_id for hit in hits]
    assert order[2:] == [a[1], a[2], a[3], a[17]]
    marker = result.child_chunks[2].metadata[GRAPH_EXPANSION_KEY]
    assert marker["kind"] == GRAPH_KIND_NAME and marker["match"] == "作業の準備"
    diagnostics = _graph_expansion_diagnostics(state.graph_expansions, max_chunks=6)
    assert diagnostics["added_count"] == 4
    assert diagnostics["entity_count"] == 2
    assert [entity["added_count"] for entity in diagnostics["entities"]] == [3, 1]
    assert diagnostics["entities"][0]["entity"] == "手順書 A > 第 2 章 作業の準備"


async def test_search_caches_the_plan_and_skips_when_disabled_or_search_only() -> None:
    from app.rag.answer_engine import _SearchState
    from app.schemas.search import SearchRequest

    store = _store()
    a = store.chunk_ids("doc-a")
    hits = [store.chunks[a[16]]]
    request = SearchRequest(query=QUESTION, filters=dict(KB))
    engine = _engine(store, hits)
    state = _SearchState()

    await engine._search(request, state, retrieval_queries=[QUESTION])
    await engine._search(request, state, retrieval_queries=[QUESTION, "言い換え"])
    # 同じ起点なら、CRAG の各回で関係情報の表を読み直さない。
    assert store.calls == ["seed", "name", "chunks"]

    # 既定（OFF）なら表を読まず、候補は今までと同じ。
    off_store = _store()
    disabled = await _engine(off_store, hits, rag_graph_expansion_enabled=False)._search(
        request, _SearchState(), retrieval_queries=[QUESTION]
    )
    assert [child.chunk_uid for child in disabled.child_chunks] == [a[16]]
    assert off_store.calls == []
    # 検索だけ（KB の検索テストなど）は上位 top_k 件の候補なので足さない。
    retrieved_only = await engine.retrieve(request.model_copy(update={"top_k": 1}))
    assert [chunk.chunk_id for chunk in retrieved_only] == [a[16]]


async def test_search_continues_when_the_graph_tables_cannot_be_read() -> None:
    from app.rag.answer_engine import _SearchState
    from app.schemas.search import SearchRequest

    class Broken(InMemoryGraphStore):
        async def graph_seed_entities(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("ORA-00942")

    store = _store()
    a = store.chunk_ids("doc-a")
    hits = [store.chunks[a[16]]]
    state = _SearchState()

    result = await _engine(cast(Any, Broken({})), hits)._search(
        SearchRequest(query=QUESTION), state, retrieval_queries=[QUESTION]
    )

    assert [child.chunk_uid for child in result.child_chunks] == [a[16]]
    assert state.graph_expansions == {}


async def test_graph_store_queries_use_the_search_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """3 つの SQL は検索と同じ見え方の条件（KB の範囲・権限・有効な chunk_set）で chunk を join
    する。
    """
    from app.clients.oracle import OracleClient
    from app.clients.oracle_graph_store import OracleGraphStore

    client = OracleClient(settings=Settings())
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_fetch_all(
        statement: str, binds: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        calls.append((" ".join(statement.split()), dict(binds or {})))
        return []

    monkeypatch.setattr(client, "_fetch_all", fake_fetch_all)
    store = OracleGraphStore(client)

    assert await store.graph_seed_entities(dict(KB), seed_chunk_ids=["c1", "c2"]) == []
    assert await store.graph_name_entities(dict(KB), limit=9) == []
    assert await store.graph_entity_chunks(dict(KB), entity_ids=["e1"], per_entity=3) == []
    # 起点・entity が無ければ SQL を読まない。
    assert await store.graph_seed_entities(dict(KB), seed_chunk_ids=[]) == []
    assert await store.graph_entity_chunks(dict(KB), entity_ids=[], per_entity=3) == []
    assert len(calls) == 3

    for sql, binds in calls:
        assert "kb-1" in binds.values()
        assert "rag_chunk_sets active_cs" in sql
        assert "JOIN rag_chunks c ON c.chunk_id = " in sql
        assert "e.knowledge_base_id" not in sql
    seed_sql, seed_binds = calls[0]
    assert seed_binds["graph_seed_0"] == "c1"
    assert "e.entity_type IN ('section', 'table_section', 'figure_section')" in seed_sql
    assert calls[1][1]["graph_name_limit"] == 9
    chunk_sql, chunk_binds = calls[2]
    assert chunk_binds["graph_entity_0"] == "e1" and chunk_binds["graph_per_entity"] == 3
    assert "PARTITION BY ec.entity_id ORDER BY c.chunk_index" in chunk_sql


async def test_graph_zero_additions_records_existing_candidates() -> None:
    from app.rag.answer_engine import _graph_expansion_diagnostics

    store = _store()
    ids = store.chunk_ids("doc-a")
    attempt: dict[str, object] = {}
    plan = await plan_graph_expansion(
        store,
        dict(KB),
        question="質問",
        seed_chunks=[store.chunks[ids[2]]],
        exclude_chunk_ids=set(ids),
        max_chunks=6,
        diagnostics=attempt,
    )
    assert plan == []
    diagnostic = _graph_expansion_diagnostics({}, max_chunks=6, attempts=[attempt])
    assert diagnostic["status"] == "completed"
    assert diagnostic["graph_available"] is True
    assert diagnostic["added_count"] == 0
    assert diagnostic["skipped_existing_count"] > 0


def test_graph_diagnostics_distinguishes_absent_graph_from_not_run_and_failure() -> None:
    from app.rag.answer_engine import _graph_expansion_diagnostics

    assert _graph_expansion_diagnostics({}, max_chunks=6)["status"] == "not_run"
    result = _graph_expansion_diagnostics(
        {}, max_chunks=6, attempts=[{"status": "completed", "graph_available": False}]
    )
    assert result["status"] == "completed" and result["graph_available"] is False
    assert (
        _graph_expansion_diagnostics({}, max_chunks=6, attempts=[{"status": "error"}])["status"]
        == "error"
    )
