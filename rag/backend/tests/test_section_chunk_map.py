"""章節と chunk の対応（#1438）。

章節ナビゲーションの章節を正本にし、章節を直しても chunk・embedding は作り直さず、対応だけを
決め直す。対応の決め方（要素の ID → 見出しの名前とページ → ページ）と、作り直しの呼び出し（取込の
後・章節の保存の後・章節の抽出規則の変更の後）と、章節の本文の読み取り（第 2・3 段階と共通の関数）
を確かめる。
"""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from rag_parser_core.extraction import DocumentElement, DocumentNavigationNode, StructuredExtraction

from app.api.routes import documents as documents_route
from app.api.routes import settings as settings_route
from app.clients.section_map_store import (
    ChunkSetChunks,
    ReadableChunkSet,
    SectionChunkRow,
    map_chunk_from_row,
)
from app.main import app
from app.rag import section_map_service
from app.rag.section_chunk_map import (
    ROOT_SECTION_ID,
    MapChunk,
    SectionChunkMap,
    SectionNode,
    build_section_chunk_map,
    section_parents,
)
from app.rag.section_rules import extraction_section_anchors
from app.schemas.document import DocumentSection
from app.schemas.search import RetrievedChunk
from tests.support import AsgiTestClient
from tests.test_document_sections import URL, FakeSectionsOracle

client = AsgiTestClient(app)


def _section(
    section_id: str,
    title: str,
    level: int,
    pages: tuple[int, int] | None,
    **extra: Any,
) -> DocumentSection:
    return DocumentSection(
        id=section_id,
        title=title,
        level=level,
        page_start=pages[0] if pages else None,
        page_end=pages[1] if pages else None,
        **extra,
    )


def _chunk(
    index: int,
    page: int | None,
    head: str = "本文",
    *,
    elements: Sequence[str] = (),
    path: Sequence[str] = (),
    chars: int = 10,
) -> MapChunk:
    return MapChunk(
        chunk_id=f"c{index}",
        chunk_index=index,
        page_start=page,
        page_end=page,
        element_ids=tuple(elements),
        section_path=tuple(path),
        head=head,
        chars=chars,
    )


def _owners(mapping: SectionChunkMap) -> dict[str, str]:
    return {link.chunk_id: link.section_id for link in mapping.links if link.is_owner}


def _members(mapping: SectionChunkMap) -> dict[str, list[str]]:
    return {node.section_id: mapping.chunk_ids(node.section_id) for node in mapping.sections}


# ---- 対応の決め方（DB を読まない決定論の関数） ----


def test_element_anchors_decide_where_each_section_starts() -> None:
    """同じページに 2 つの章節が始まっても、解析の要素の ID で境目を決める。"""
    sections = [
        _section("s1", "第1章 総則", 1, (1, 2), source_section_id="nav-1"),
        _section("s2", "第2章 申請", 1, (2, 4), source_section_id="nav-2"),
        _section("s21", "2.1 申請書", 2, (2, 3), source_section_id="nav-21"),
        _section("s22", "2.2 添付", 2, (4, 4), source_section_id="nav-22"),
    ]
    chunks = [
        _chunk(0, 1, elements=["e1"]),
        _chunk(1, 2, elements=["e2"]),
        # p.2 の途中から第 2 章（要素 e3）。名前は本文に無い。
        _chunk(2, 2, elements=["e3", "e4"]),
        _chunk(3, 3, elements=["e5"]),
        _chunk(4, 4, elements=["e6"]),
    ]
    anchors = {
        "nav-1": frozenset({"e1", "e2"}),
        "nav-2": frozenset({"e3"}),
        "nav-21": frozenset({"e4", "e5"}),
        "nav-22": frozenset({"e6"}),
    }

    mapping = build_section_chunk_map("cs-1", sections, chunks, anchors=anchors, root_title="規程")

    assert _owners(mapping) == {"c0": "s1", "c1": "s1", "c2": "s21", "c3": "s21", "c4": "s22"}
    members = _members(mapping)
    assert members[ROOT_SECTION_ID] == ["c0", "c1", "c2", "c3", "c4"]
    assert members["s2"] == ["c2", "c3", "c4"]
    assert members["s21"] == ["c2", "c3"]
    nodes = {node.section_id: node for node in mapping.sections}
    assert (nodes["s2"].chunk_count, nodes["s2"].chars) == (3, 30)
    assert (nodes["s2"].first_chunk_index, nodes["s2"].last_chunk_index) == (2, 4)
    assert (nodes["s22"].parent_section_id, nodes["s2"].parent_section_id) == ("s2", "document")
    assert (nodes[ROOT_SECTION_ID].title, nodes[ROOT_SECTION_ID].level) == ("規程", 0)
    assert (nodes[ROOT_SECTION_ID].page_start, nodes[ROOT_SECTION_ID].page_end) == (1, 4)
    # 章節の中の順は読み順（chunk_index）。
    order = [link.chunk_order for link in mapping.links if link.section_id == "s2"]
    assert order == [0, 1, 2]


def test_title_and_page_decide_starts_without_elements() -> None:
    """人が足した章節（要素の ID が無い）は、開始のページの見出しの名前、無ければページで決める。

    本文の途中の言及（「この手順書は」の「手順」）は見出しとみなさない。
    """
    sections = [
        _section("m1", "はじめに", 1, (1, 1)),
        _section("m2", "手順", 1, (1, 3)),
        _section("m3", "付録", 1, (4, 4)),
    ]
    chunks = [
        _chunk(0, 1, "はじめに この手順書は"),
        # 全角・空白の違いは無視して名前を照合する。
        _chunk(1, 1, "手 順\n1. 申請する"),
        _chunk(2, 2, "2. 承認を待つ"),
        _chunk(3, 3, "3. 作業する"),
        # 付録の名前は本文に無い（ページで決める）。
        _chunk(4, 4, "別表"),
    ]

    mapping = build_section_chunk_map("cs-1", sections, chunks)

    assert _owners(mapping) == {"c0": "m1", "c1": "m2", "c2": "m2", "c3": "m2", "c4": "m3"}
    nodes = {node.section_id: node for node in mapping.sections}
    assert (nodes["m2"].page_start, nodes["m2"].page_end, nodes["m2"].chunk_count) == (1, 3, 3)


def test_section_path_of_the_chunk_also_matches_the_title() -> None:
    sections = [_section("a", "概要", 1, (1, 2)), _section("b", "詳細", 1, (2, 2))]
    chunks = [
        _chunk(0, 1, "本文 A"),
        _chunk(1, 2, "本文 B", path=["規程", "概要"]),
        _chunk(2, 2, "本文 C", path=["規程", "詳細"]),
    ]

    mapping = build_section_chunk_map("cs-1", sections, chunks)

    assert _owners(mapping) == {"c0": "a", "c1": "a", "c2": "b"}


def test_edited_pages_win_over_element_anchors() -> None:
    """人がページを直した章節は、ページの範囲の外の要素の ID を使わない（章節が正本）。"""
    sections = [
        _section("s1", "第1章", 1, (1, 1), source_section_id="nav-1"),
        # 抽出では p.2 から始まる章節を、人が p.3 からに直した。
        _section("s2", "第2章", 1, (3, 3), source_section_id="nav-2", edited=True),
    ]
    chunks = [_chunk(0, 1, elements=["e1"]), _chunk(1, 2, elements=["e2"]), _chunk(2, 3)]
    anchors = {"nav-1": frozenset({"e1"}), "nav-2": frozenset({"e2"})}

    mapping = build_section_chunk_map("cs-1", sections, chunks, anchors=anchors)

    # p.2 の chunk は第 1 章の終わりのページより後ろ、第 2 章の始まりより前なので、文書全体に
    # だけ入る。
    assert _owners(mapping) == {"c0": "s1", "c1": ROOT_SECTION_ID, "c2": "s2"}


def test_chunks_after_the_end_page_move_to_the_ancestor_or_the_document() -> None:
    sections = [
        _section("s1", "第1章", 1, (1, 4)),
        _section("s11", "1.1 目的", 2, (1, 2)),
        _section("s2", "第2章", 1, (6, 6)),
    ]
    chunks = [
        _chunk(0, 1, "第1章"),
        _chunk(1, 1, "1.1 目的"),
        _chunk(2, 3, "続き"),
        _chunk(3, 5, "どの章節でもない"),
        _chunk(4, 6, "第2章"),
    ]

    mapping = build_section_chunk_map("cs-1", sections, chunks)

    assert _owners(mapping) == {
        "c0": "s1",
        "c1": "s11",
        "c2": "s1",
        "c3": ROOT_SECTION_ID,
        "c4": "s2",
    }
    assert _members(mapping)["s1"] == ["c0", "c1", "c2"]


def test_document_without_sections_is_one_section() -> None:
    chunks = [_chunk(0, 1), _chunk(1, 2), _chunk(2, None)]

    mapping = build_section_chunk_map("cs-1", [], chunks, root_title="メモ.docx")

    assert [node.section_id for node in mapping.sections] == [ROOT_SECTION_ID]
    assert _members(mapping)[ROOT_SECTION_ID] == ["c0", "c1", "c2"]
    assert all(link.is_owner for link in mapping.links)


def test_sections_without_pages_or_matching_titles_are_empty() -> None:
    sections = [_section("a", "第1章", 1, (1, 2)), _section("x", "存在しない見出し", 1, None)]
    chunks = [_chunk(0, 1, "第1章"), _chunk(1, 2, "続き")]

    mapping = build_section_chunk_map("cs-1", sections, chunks)

    nodes = {node.section_id: node for node in mapping.sections}
    assert (nodes["x"].chunk_count, nodes["x"].first_chunk_index) == (0, None)
    assert _owners(mapping) == {"c0": "a", "c1": "a"}


def test_map_is_deterministic_and_ignores_the_input_order() -> None:
    sections = [_section("a", "第1章", 1, (1, 1)), _section("b", "第2章", 1, (2, 2))]
    chunks = [_chunk(0, 1, "第1章"), _chunk(1, 2, "第2章"), _chunk(2, 2, "続き")]

    first = build_section_chunk_map("cs-1", sections, chunks)
    second = build_section_chunk_map("cs-1", sections, list(reversed(chunks)))

    assert first == second


def test_section_parents_follow_order_and_levels() -> None:
    sections = [
        _section("a", "A", 1, None),
        _section("a1", "A1", 2, None),
        _section("a11", "A11", 3, None),
        _section("a2", "A2", 2, None),
        _section("b", "B", 1, None),
    ]

    assert section_parents(sections) == ["document", "a", "a1", "a", "document"]


def test_extraction_anchors_use_navigation_elements_or_rule_headings() -> None:
    extraction = StructuredExtraction(
        navigation=[
            DocumentNavigationNode(section_id="nav-1", title="第1章", element_ids=["e1", "e2"]),
            DocumentNavigationNode(section_id="nav-2", title="第2章"),
        ],
        elements=[
            DocumentElement(kind="heading", text="第1章 総則", order=0, element_id="e1"),
            DocumentElement(kind="text", text="本文。", order=1, element_id="e2"),
            DocumentElement(kind="heading", text="第2章 申請", order=2, element_id="e3"),
        ],
    )

    parser = extraction_section_anchors(extraction, "parser")
    assert parser == {"nav-1": frozenset({"e1", "e2"})}
    legal = extraction_section_anchors(extraction, "legal")
    assert sorted(next(iter(ids)) for ids in legal.values()) == ["e1", "e3"]
    assert extraction_section_anchors(None, "parser") == {}


def test_map_chunk_skips_summaries_and_reads_pages_and_elements() -> None:
    row: dict[str, object] = {
        "chunk_id": "c1",
        "chunk_index": 3,
        "metadata_json": {"page_number": 4, "element_ids": "e1,e2", "section_path": "規程 > 第1章"},
        "head": "第1章",
        "chars": 120,
    }
    chunk = map_chunk_from_row(row)
    assert chunk == MapChunk("c1", 3, 4, 4, ("e1", "e2"), ("規程", "第1章"), "第1章", 120)
    assert map_chunk_from_row({**row, "metadata_json": {"raptor_summary": True}}) is None
    assert map_chunk_from_row({**row, "metadata_json": {"nav_summary": True}}) is None


@pytest.mark.parametrize(
    "metadata",
    [
        # 印の無い分割（#2590 より前に保存した chunk）でも、要約・項目の値の種類で除く。
        {"content_kind": "section_summary", "element_ids": "nav-summary-nav-1"},
        {"content_kind": "field", "element_ids": "field-3-amount"},
        {"content_kind": "text", "extracted_field": True},
    ],
    ids=["section-summary-without-mark", "field-without-mark", "extracted-field-mark"],
)
def test_map_chunk_skips_derived_chunks_without_summary_marks(metadata: dict[str, object]) -> None:
    row: dict[str, object] = {"chunk_id": "c1", "chunk_index": 0, "metadata_json": metadata}
    assert map_chunk_from_row(row) is None


@pytest.mark.parametrize("content_kind", ["text", "list", "table", "figure", "record"])
def test_map_chunk_keeps_source_chunks_of_every_body_kind(content_kind: str) -> None:
    row: dict[str, object] = {
        "chunk_id": "c1",
        "chunk_index": 0,
        "metadata_json": {"content_kind": content_kind, "page_number": 2},
    }
    chunk = map_chunk_from_row(row)
    assert chunk is not None
    assert chunk.page_start == 2


# ---- 作り直し（章節の正本から、対応の表だけを置き換える） ----


class _MapOracle(FakeSectionsOracle):
    """章節の fake に文書名を足したもの。chunk・embedding を書く method は持たない。"""

    async def get_document(self, document_id: str) -> Any:
        return SimpleNamespace(file_name="規程.pdf") if document_id == "doc-1" else None


class _RecordingStore:
    chunk_sets: list[ChunkSetChunks] = []
    replaced: list[tuple[str, list[SectionChunkMap]]] = []

    def __init__(self, oracle: object | None = None) -> None:
        del oracle

    async def document_chunk_sets(self, document_id: str) -> list[ChunkSetChunks]:
        return list(self.chunk_sets)

    async def replace_document_section_maps(
        self, document_id: str, maps: Sequence[SectionChunkMap]
    ) -> None:
        self.replaced.append((document_id, list(maps)))


@pytest.fixture
def recording_store(monkeypatch: pytest.MonkeyPatch) -> type[_RecordingStore]:
    _RecordingStore.chunk_sets = []
    _RecordingStore.replaced = []
    monkeypatch.setattr(section_map_service, "SectionMapStore", _RecordingStore)
    return _RecordingStore


async def test_rebuild_maps_every_active_chunk_set_from_the_resolved_sections(
    recording_store: type[_RecordingStore],
) -> None:
    """要素の ID は章節の元にした解析の結果（ext-1）の chunk_set にだけ当てる。"""
    oracle = _MapOracle()
    oracle.navigation[0].element_ids = ["e1"]
    oracle.navigation[1].element_ids = ["e2"]
    oracle.navigation[2].element_ids = ["e3"]
    recording_store.chunk_sets = [
        ChunkSetChunks(
            "cs-1",
            "ext-1",
            [
                _chunk(0, 1, elements=["e1"]),
                _chunk(1, 3, elements=["e2"]),
                _chunk(2, 5, elements=["e3"]),
            ],
        ),
        # 別の解析の結果の chunk_set（要素の ID は使わず、名前とページで決める）。
        ChunkSetChunks(
            "cs-2", "ext-2", [_chunk(0, 1, "第1章 総則"), _chunk(1, 3), _chunk(2, 5, "第2章 申請")]
        ),
    ]

    maps = await section_map_service.rebuild_document_section_map("doc-1", oracle=oracle)  # type: ignore[arg-type]

    assert maps is not None
    assert [item.chunk_set_id for item in maps] == ["cs-1", "cs-2"]
    assert recording_store.replaced == [("doc-1", maps)]
    expected = {"c0": "nav-1", "c1": "nav-2", "c2": "nav-3"}
    assert _owners(maps[0]) == expected
    assert _owners(maps[1]) == expected
    assert maps[0].sections[0].title == "規程.pdf"


async def test_rebuild_follows_the_edited_sections(recording_store: type[_RecordingStore]) -> None:
    oracle = _MapOracle()
    oracle.stored = {
        "sections": {
            "sections": [
                _section("m1", "前半", 1, (1, 4)).model_dump(),
                _section("m2", "後半", 1, (5, 6)).model_dump(),
            ],
            "removed_source_ids": ["nav-1", "nav-2", "nav-3"],
        },
        "revision": 2,
    }
    recording_store.chunk_sets = [ChunkSetChunks("cs-1", "ext-1", [_chunk(0, 1), _chunk(1, 5)])]

    maps = await section_map_service.rebuild_document_section_map("doc-1", oracle=oracle)  # type: ignore[arg-type]

    assert maps is not None
    assert _owners(maps[0]) == {"c0": "m1", "c1": "m2"}


async def test_rebuild_of_a_missing_document_does_nothing(
    recording_store: type[_RecordingStore],
) -> None:
    result = await section_map_service.rebuild_document_section_map("doc-9", oracle=_MapOracle())  # type: ignore[arg-type]

    assert result is None
    assert recording_store.replaced == []


async def test_refresh_logs_and_continues_when_the_rebuild_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(document_id: str, *, oracle: object | None = None) -> None:
        raise RuntimeError("ORA-00942")

    monkeypatch.setattr(section_map_service, "rebuild_document_section_map", broken)

    assert await section_map_service.refresh_document_section_map("doc-1", reason="test") is False
    assert "section_map_refresh_failed" in caplog.text


# ---- 作り直しを呼ぶ所（取込の後・章節の保存の後・規則の変更の後） ----


@pytest.fixture
def refreshes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    async def record(document_id: str, *, oracle: object | None = None, reason: str) -> bool:
        calls.append((document_id, reason))
        return True

    monkeypatch.setattr(documents_route, "refresh_document_section_map", record)
    return calls


def test_saving_or_resetting_sections_rebuilds_only_the_map(
    monkeypatch: pytest.MonkeyPatch, refreshes: list[tuple[str, str]]
) -> None:
    """章節の保存・取り消しは対応だけを作り直す。

    chunk・embedding を書く method を持たない fake で通る（再分割・再 embedding をしない）。
    """
    fake = _MapOracle()
    monkeypatch.setattr(documents_route, "OracleClient", lambda: fake)
    sections = client.get(URL).json()["data"]["sections"]

    assert client.put(URL, json={"sections": sections, "base_revision": None}).status_code == 200
    assert client.delete(URL).status_code == 200
    assert refreshes == [("doc-1", "sections_saved"), ("doc-1", "sections_reset")]

    # 保存できなかった（版の衝突）ときは作り直さない。
    stale = client.put(URL, json={"sections": sections, "base_revision": 99})
    assert stale.status_code == 409
    assert len(refreshes) == 2


def test_section_rules_changes_rebuild_maps_of_all_documents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setenv("RAG_SECTION_RULES_FILE", str(tmp_path / "section-rules.json"))
    reasons: list[str] = []
    monkeypatch.setattr(
        settings_route,
        "schedule_refresh_all_section_maps",
        lambda *, reason: reasons.append(reason),
    )

    saved = client.patch("/api/settings/section-rules", json={"mode": "legal", "rules": []})
    assert saved.status_code == 200, saved.text
    assert client.delete("/api/settings/section-rules").status_code == 200
    assert reasons == ["section_rules_saved", "section_rules_reset"]


async def test_refresh_all_rebuilds_every_indexed_document(monkeypatch: pytest.MonkeyPatch) -> None:
    rebuilt: list[str] = []

    class _Store:
        def __init__(self, oracle: object | None = None) -> None:
            del oracle

        async def indexed_document_ids(self) -> list[str]:
            return ["d1", "d2"]

    async def record(document_id: str, *, oracle: object | None = None) -> None:
        rebuilt.append(document_id)

    monkeypatch.setattr(section_map_service, "SectionMapStore", _Store)
    monkeypatch.setattr(section_map_service, "rebuild_document_section_map", record)

    count = await section_map_service.refresh_all_section_maps(reason="test", oracle=object())  # type: ignore[arg-type]

    assert (count, rebuilt) == (2, ["d1", "d2"])


# ---- 章節の本文の読み取り（MCP の rag_read_section と第 2・3 段階が共通で使う） ----


def _retrieved(chunk_id: str, text: str) -> RetrievedChunk:
    return RetrievedChunk(document_id="d1", chunk_id=chunk_id, text=text, score=0.0)


class _ReadStore:
    sections: list[SectionNode] = []
    rows: dict[str, list[SectionChunkRow]] = {}
    calls: list[tuple[int, int]] = []

    def __init__(self, oracle: object | None = None) -> None:
        del oracle

    async def readable_chunk_set(
        self, document_id: str, chunk_set_id: str | None = None
    ) -> ReadableChunkSet | None:
        return ReadableChunkSet("d1", "cs-1", "手順書.pdf", False) if document_id == "d1" else None

    async def chunk_set_sections(self, document_id: str, chunk_set_id: str) -> list[SectionNode]:
        return list(self.sections)

    async def section_chunks(
        self,
        document_id: str,
        *,
        chunk_set_id: str,
        section_id: str,
        from_order: int,
        limit: int,
        filters: object = None,
    ) -> list[SectionChunkRow]:
        self.calls.append((from_order, limit))
        rows = [row for row in self.rows.get(section_id, []) if row.chunk_order >= from_order]
        return rows[:limit]


@pytest.fixture
def read_store(monkeypatch: pytest.MonkeyPatch) -> type[_ReadStore]:
    mapping = build_section_chunk_map(
        "cs-1",
        [_section("s1", "手順", 1, (1, 3)), _section("s2", "付録", 1, (4, 4))],
        [_chunk(0, 1, "手順"), _chunk(1, 2), _chunk(2, 3), _chunk(3, 4, "付録")],
        root_title="手順書.pdf",
    )
    _ReadStore.sections = mapping.sections
    _ReadStore.rows = {
        node.section_id: [
            SectionChunkRow(order, _retrieved(chunk_id, f"本文 {chunk_id}"))
            for order, chunk_id in enumerate(mapping.chunk_ids(node.section_id))
        ]
        for node in mapping.sections
    }
    _ReadStore.calls = []
    monkeypatch.setattr(section_map_service, "SectionMapStore", _ReadStore)
    monkeypatch.setattr(section_map_service, "READ_SECTION_BATCH", 2)
    return _ReadStore


async def test_read_section_returns_the_chunks_of_the_section_in_order(
    read_store: type[_ReadStore],
) -> None:
    text = await section_map_service.read_section("d1", "s1", oracle=object())  # type: ignore[arg-type]

    assert [row.chunk.chunk_id for row in text.chunks] == ["c0", "c1", "c2"]
    assert (text.section.page_start, text.section.page_end, text.complete) == (1, 3, True)
    # 章節の中を batch の単位で順に読み、次の章節の chunk は読まない。
    assert read_store.calls == [(0, 2), (2, 2)]
    assert text.outline.path(text.section) == ["手順"]
    assert text.outline.ancestor(text.section, 3).section_id == ROOT_SECTION_ID


async def test_read_section_with_limit_reports_that_it_is_not_complete(
    read_store: type[_ReadStore],
) -> None:
    first = await section_map_service.read_section("d1", "s1", limit=2, oracle=object())  # type: ignore[arg-type]
    rest = await section_map_service.read_section(
        "d1",
        "s1",
        from_order=first.chunks[-1].chunk_order + 1,
        oracle=object(),  # type: ignore[arg-type]
    )

    assert ([row.chunk.chunk_id for row in first.chunks], first.complete) == (["c0", "c1"], False)
    assert ([row.chunk.chunk_id for row in rest.chunks], rest.complete) == (["c2"], True)


async def test_read_section_errors_for_hidden_documents_and_unknown_sections(
    read_store: type[_ReadStore],
) -> None:
    del read_store
    with pytest.raises(section_map_service.SectionNotFoundError):
        await section_map_service.read_section("d9", "s1", oracle=object())  # type: ignore[arg-type]
    with pytest.raises(section_map_service.SectionNotFoundError):
        await section_map_service.read_section("d1", "s9", oracle=object())  # type: ignore[arg-type]


async def test_outline_rebuilds_the_map_when_it_is_missing(
    read_store: type[_ReadStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    """この機能の前に取り込んだ文書は、章節を読むときに対応を作ってから読む。"""
    sections = list(read_store.sections)
    read_store.sections = []
    rebuilt: list[str] = []

    async def rebuild(document_id: str, *, oracle: object | None = None) -> None:
        rebuilt.append(document_id)
        read_store.sections = sections

    monkeypatch.setattr(section_map_service, "rebuild_document_section_map", rebuild)

    outline = await section_map_service.section_outline("d1", oracle=object())  # type: ignore[arg-type]

    assert rebuilt == ["d1"]
    assert outline is not None and [node.section_id for node in outline.sections] == [
        ROOT_SECTION_ID,
        "s1",
        "s2",
    ]
