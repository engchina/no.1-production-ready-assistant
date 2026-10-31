"""Navigation tree 構築・要約（Knowhere 由来）の単体テスト。"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

import pytest
from rag_engine.chunking.headings import layout_section_paths

from app.clients.section_map_store import map_chunk_from_row
from app.rag.chunking import CHUNKING_STRATEGIES, Chunk, chunk_extraction_with_strategy
from app.rag.document_sections import sections_from_navigation
from app.rag.navigation import (
    build_navigation_tree,
    navigation_section_id,
    navigation_summary_elements,
    summarize_navigation_nodes,
)
from app.rag.section_chunk_map import build_section_chunk_map
from app.schemas.document import DocumentSection
from app.schemas.extraction import (
    DocumentElement,
    DocumentNavigationNode,
    StructuredExtraction,
)


def test_navigation_summary_elements_are_searchable_and_link_to_section() -> None:
    nodes = [
        DocumentNavigationNode(
            section_id="nav-abc",
            title="第1章",
            section_path=["第1章"],
            depth=1,
            page_start=1,
            summary="クラウド利用料の概要。",
        ),
        DocumentNavigationNode(
            section_id="nav-def",
            title="第2章",
            section_path=["第2章"],
            depth=1,
            summary=None,
        ),
    ]
    elements = navigation_summary_elements(nodes, start_order=10)
    # summary を持つ node だけ element 化される。
    assert len(elements) == 1
    element = elements[0]
    assert element.kind == "text"  # 検索可能 kind
    assert element.content_kind == "section_summary"
    assert "クラウド利用料" in element.text
    assert element.metadata.get("section_id") == "nav-abc"
    assert element.metadata.get("nav_summary") is True
    assert element.element_id == "nav-summary-nav-abc"
    assert element.order == 10


def _element(
    *, text: str, element_id: str, section_path: list[str], order: int, page: int | None = None
) -> DocumentElement:
    return DocumentElement(
        kind="text",
        text=text,
        order=order,
        element_id=element_id,
        section_path=section_path,
        page_number=page,
    )


def _extraction(elements: list[DocumentElement]) -> StructuredExtraction:
    return StructuredExtraction(raw_text="", elements=elements)


def test_build_navigation_tree_reconstructs_hierarchy() -> None:
    extraction = _extraction(
        [
            _element(
                text="導入の本文", element_id="e1", section_path=["第1章", "概要"], order=0, page=1
            ),
            _element(
                text="詳細の本文", element_id="e2", section_path=["第1章", "詳細"], order=1, page=2
            ),
            _element(text="結論本文", element_id="e3", section_path=["第2章"], order=2, page=3),
        ]
    )
    nodes = build_navigation_tree(extraction)
    by_path = {tuple(node.section_path): node for node in nodes}

    # 第1章 は明示要素を持たないが prefix として node 化される。
    assert tuple(["第1章"]) in by_path
    chapter1 = by_path[("第1章",)]
    assert chapter1.parent_section_id is None
    assert chapter1.depth == 1
    # 子 node の id が親に link される。
    assert by_path[("第1章", "概要")].section_id in chapter1.child_section_ids
    assert by_path[("第1章", "詳細")].section_id in chapter1.child_section_ids
    # page 範囲は subtree 全体を覆う。
    assert chapter1.page_start == 1
    assert chapter1.page_end == 2
    # element_ids は完全一致 path の node にのみ付く。
    assert by_path[("第1章", "概要")].element_ids == ["e1"]
    assert chapter1.element_ids == []


def test_navigation_sections_keep_body_parents_when_toc_comes_first() -> None:
    """目次の要素が章の見出しを本文より先に持っても、本文の節は本文の章の子になる（#3511）。"""
    extraction = _extraction(
        [
            _element(text="目次", element_id="t0", section_path=["文書", "目次"], order=0, page=1),
            _element(
                text="第1章 総則",
                element_id="t1",
                section_path=["文書", "第1章 総則"],
                order=1,
                page=1,
            ),
            _element(
                text="第2章 手続",
                element_id="t2",
                section_path=["文書", "第2章 手続"],
                order=2,
                page=1,
            ),
            _element(
                text="1.1 の本文",
                element_id="b1",
                section_path=["文書", "第1章 総則", "1.1 目的"],
                order=3,
                page=2,
            ),
            _element(
                text="2.1 の本文",
                element_id="b2",
                section_path=["文書", "第2章 手続", "2.1 受付"],
                order=4,
                page=3,
            ),
            _element(
                text="2.1.1 の本文",
                element_id="b3",
                section_path=["文書", "第2章 手続", "2.1 受付", "2.1.1 確認"],
                order=5,
                page=3,
            ),
        ]
    )

    nodes = build_navigation_tree(extraction)
    sections = sections_from_navigation(nodes)

    assert [section.title for section in sections] == [
        "文書",
        "目次",
        "第1章 総則",
        "1.1 目的",
        "第2章 手続",
        "2.1 受付",
        "2.1.1 確認",
    ]
    assert [section.level for section in sections] == [1, 2, 2, 3, 2, 3, 4]
    # 親子の link は並びと同じ（並びから決まる親と一致する）。
    by_title = {node.title: node for node in nodes}
    assert by_title["1.1 目的"].parent_section_id == by_title["第1章 総則"].section_id
    assert by_title["2.1.1 確認"].parent_section_id == by_title["2.1 受付"].section_id


def test_navigation_section_id_is_stable() -> None:
    assert navigation_section_id(("第1章", "概要")) == navigation_section_id(("第1章", "概要"))
    assert navigation_section_id(("第1章",)) != navigation_section_id(("第2章",))


def test_build_navigation_tree_is_empty_without_sections() -> None:
    extraction = _extraction(
        [_element(text="見出しのない本文", element_id="e1", section_path=[], order=0)]
    )
    assert build_navigation_tree(extraction) == []


@pytest.mark.anyio
async def test_summarize_navigation_nodes_fills_summaries_with_injected_summarizer() -> None:
    extraction = _extraction(
        [
            _element(text="あ" * 300, element_id="e1", section_path=["第1章"], order=0, page=1),
            _element(text="短文", element_id="e2", section_path=["第2章"], order=1, page=2),
        ]
    )
    nodes = build_navigation_tree(extraction)

    calls: list[str] = []

    async def _summarize(text: str) -> str:
        calls.append(text)
        return f"要約({len(text)}文字)"

    summarized = await summarize_navigation_nodes(nodes, extraction, _summarize, min_chars=200)
    by_path = {tuple(node.section_path): node for node in summarized}
    # 長い章は要約され、短い章（min_chars 未満）は要約されない。
    assert by_path[("第1章",)].summary == "要約(300文字)"
    assert by_path[("第2章",)].summary is None
    assert len(calls) == 1


@pytest.mark.anyio
async def test_summarize_navigation_nodes_respects_max_nodes() -> None:
    extraction = _extraction(
        [
            _element(
                text="本文" * 200, element_id=f"e{i}", section_path=[f"章{i}"], order=i, page=1
            )
            for i in range(5)
        ]
    )
    nodes = build_navigation_tree(extraction)

    async def _summarize(text: str) -> str:
        return "要約"

    summarized = await summarize_navigation_nodes(
        nodes, extraction, _summarize, max_nodes=2, min_chars=10
    )
    assert sum(1 for node in summarized if node.summary) == 2


def test_navigation_round_trips_through_document_payload() -> None:
    extraction = _extraction(
        [_element(text="本文", element_id="e1", section_path=["第1章"], order=0, page=1)]
    )
    extraction = extraction.model_copy(update={"navigation": build_navigation_tree(extraction)})
    payload = extraction.to_document_payload()
    assert payload["navigation"]
    restored = StructuredExtraction.model_validate(payload)
    assert restored.navigation == extraction.navigation


# 評価の PDF を Docling で解析した layout record（rag_engine の回帰テストと同じ fixture。#1517）。
_RAG_ROOT = Path(__file__).resolve().parents[2]
_LAYOUT_FIXTURE = (
    _RAG_ROOT / "packages" / "rag_engine" / "tests" / "fixtures" / "evaluation_layout_headings.json"
)
_LAYOUT_DOCUMENTS: dict[str, list[dict[str, Any]]] = {
    name: document["records"]
    for name, document in json.loads(_LAYOUT_FIXTURE.read_text(encoding="utf-8"))[
        "documents"
    ].items()
}
_HEADING_TAG = re.compile(r"<h([1-6])[^>]*>(.*?)</h\1>", re.S)


def _heading_key(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def _source_toc(name: str) -> list[tuple[int, str]]:
    """原稿の HTML の見出し(h1〜h6)の階層と名前。"""
    folder, file_name = name.split("/", 1)
    source = _RAG_ROOT / "evaluation" / folder / "sources" / file_name.replace(".pdf", ".html")
    return [
        (int(level), _heading_key(re.sub(r"<[^>]+>", "", title)))
        for level, title in _HEADING_TAG.findall(source.read_text(encoding="utf-8"))
    ]


@pytest.mark.parametrize("name", sorted(_LAYOUT_DOCUMENTS), ids=sorted(_LAYOUT_DOCUMENTS))
def test_docling_navigation_sections_follow_document_toc(name: str) -> None:
    """Docling の要素から作る章節ナビゲーションの階層が、文書の目次どおりになる(#1517)。

    要素の section_path は parser(``layout_to_extraction``)と同じく、chunk と同じ規則
    (``layout_section_paths``)で作る。「第 3 章 > 3.1」の 3.1 は第 3 章の子(兄弟にしない)。
    """
    records = _LAYOUT_DOCUMENTS[name]
    paths = layout_section_paths(records)
    extraction = _extraction(
        [
            DocumentElement(
                kind="title" if record["category"] in {"Title", "Section-header"} else "text",
                text=record["text"],
                order=order,
                element_id=record["id"],
                page_number=record["page"],
                section_path=paths.get(record["id"], []),
            )
            for order, record in enumerate(records)
        ]
    )

    sections = sections_from_navigation(build_navigation_tree(extraction))

    assert [(section.level, _heading_key(section.title)) for section in sections] == _source_toc(
        name
    )


# ---- 章節の要約の由来を分割・章節の本文で保つ（#2590） ----

# 要約だけに含まれる合成の値（原文の要素には無い）。
_SUMMARY_ONLY_VALUE = "合成値7Q"
_SUMMARY_TITLES = {"numbered-title": "第1章 総則", "plain-title": "概要"}


def _summary_extraction(*, title: str, revalidated: bool) -> StructuredExtraction:
    """原文の要素の後に章節の要約の要素を足した抽出（取込の ``_attach_navigation_tree`` と同じ形）。

    ``revalidated`` は、保存した抽出を読み直す経路・分割のサービスへ送る経路と同じく、抽出を
    もう一度検証したもの（要素の正規化がもう一度走る）。
    """
    base = StructuredExtraction(
        raw_text=f"{title}\n本文の段落。",
        elements=[
            DocumentElement(
                kind="heading",
                text=title,
                order=1,
                element_id="e1",
                page_number=1,
                section_path=[title],
            ),
            DocumentElement(
                kind="text",
                text="本文の段落。",
                order=2,
                element_id="e2",
                page_number=1,
                section_path=[title],
            ),
        ],
    )
    node = DocumentNavigationNode(
        section_id="nav-s1",
        title=title,
        section_path=[title],
        depth=1,
        page_start=1,
        summary=f"{_SUMMARY_ONLY_VALUE} を含む要約。",
    )
    summaries = navigation_summary_elements([node], start_order=3)
    extraction = base.model_copy(update={"elements": [*base.elements, *summaries]})
    if revalidated:
        return StructuredExtraction.model_validate(extraction.model_dump(mode="json"))
    return extraction


def _row(chunk: Chunk) -> dict[str, object]:
    return {
        "chunk_id": f"c{chunk.index}",
        "chunk_index": chunk.index,
        "metadata_json": chunk.metadata,
        "head": chunk.text[:80],
        "chars": len(chunk.text),
    }


@pytest.mark.parametrize("revalidated", [False, True], ids=["in-process", "revalidated"])
@pytest.mark.parametrize("title_key", sorted(_SUMMARY_TITLES), ids=sorted(_SUMMARY_TITLES))
@pytest.mark.parametrize("strategy", CHUNKING_STRATEGIES, ids=CHUNKING_STRATEGIES)
def test_navigation_summary_chunks_keep_derived_marks_in_every_strategy(
    strategy: str, title_key: str, revalidated: bool
) -> None:
    """どの分割方式でも、要約の chunk は生成要約の印を持ち、原文の chunk と混ざらない。"""
    title = _SUMMARY_TITLES[title_key]
    extraction = _summary_extraction(title=title, revalidated=revalidated)

    chunks = chunk_extraction_with_strategy(
        extraction, strategy=strategy, chunk_size=400, overlap=0
    )

    summary_chunks = [chunk for chunk in chunks if _SUMMARY_ONLY_VALUE in chunk.text]
    body_chunks = [chunk for chunk in chunks if _SUMMARY_ONLY_VALUE not in chunk.text]
    assert summary_chunks
    for chunk in summary_chunks:
        assert chunk.text.startswith(title)
        assert chunk.metadata["content_kind"] == "section_summary"
        assert chunk.metadata["nav_summary"] is True
        assert chunk.metadata["section_id"] == "nav-s1"
        assert chunk.metadata["element_ids"] == "nav-summary-nav-s1"
        assert map_chunk_from_row(_row(chunk)) is None
    assert any("本文の段落" in chunk.text for chunk in body_chunks)
    for chunk in body_chunks:
        assert "nav_summary" not in chunk.metadata
        assert map_chunk_from_row(_row(chunk)) is not None


@pytest.mark.parametrize("title_key", sorted(_SUMMARY_TITLES), ids=sorted(_SUMMARY_TITLES))
def test_revalidated_navigation_summary_is_not_read_as_a_heading(title_key: str) -> None:
    """「章節名: 要約」が見出しに見えても、抽出を検証し直したときに見出しにしない。"""
    title = _SUMMARY_TITLES[title_key]
    extraction = _summary_extraction(title=title, revalidated=True)

    summary = next(e for e in extraction.elements if e.element_id == "nav-summary-nav-s1")

    assert summary.kind == "text"
    assert summary.content_kind == "section_summary"
    assert summary.section_path == [title]
    assert "section_level" not in summary.metadata


@pytest.mark.parametrize("strategy", ["structure_aware", "page_level"], ids=str)
def test_summary_only_value_never_reaches_the_section_body(strategy: str) -> None:
    """章節の本文（章節の読み取り・回答の章節の拡張が読む対応）に要約の chunk が入らない。

    同じ章節の原文の chunk は本文として入る。
    """
    title = _SUMMARY_TITLES["numbered-title"]
    extraction = _summary_extraction(title=title, revalidated=True)
    chunks = chunk_extraction_with_strategy(
        extraction, strategy=strategy, chunk_size=400, overlap=0
    )
    mapped = [found for chunk in chunks if (found := map_chunk_from_row(_row(chunk))) is not None]
    section = DocumentSection(
        id="s1", title=title, level=1, page_start=1, page_end=1, source_section_id="nav-s1"
    )

    mapping = build_section_chunk_map("set-1", [section], mapped, root_title="文書 A")

    texts = {f"c{chunk.index}": chunk.text for chunk in chunks}
    members = mapping.chunk_ids("s1")
    assert members
    assert any("本文の段落" in texts[chunk_id] for chunk_id in members)
    assert all(_SUMMARY_ONLY_VALUE not in texts[link.chunk_id] for link in mapping.links)


@pytest.mark.parametrize("enabled", [False, True])
async def test_section_rules_work_independently_of_navigation_summaries(enabled: bool) -> None:
    """要約 OFF でも木を作り、再解析せず規則の章節へ切り替えられる。"""
    from typing import cast

    from app.config import Settings
    from app.rag.ingestion import IngestionPipeline
    from app.rag.section_rules import SectionRulesStore, extraction_sections

    calls: list[str] = []

    class Summarizer:
        async def generate(self, prompt: str, text: str) -> str:
            calls.append(text)
            return "合成の要約"

    extraction = StructuredExtraction(
        raw_text="合成の文書",
        elements=[
            DocumentElement(
                kind="text",
                text=f"{index}. 節 {index}\n" + "合成の本文。" * 50,
                order=index,
                element_id=f"element-{index}",
                page_number=index,
                section_path=[f"解析見出し {index}"],
            )
            for index in (1, 2)
        ],
    )
    pipeline = IngestionPipeline(
        vlm=cast(Any, Summarizer()),
        oracle=cast(Any, object()),
        settings=Settings(rag_navigation_summary_enabled=enabled),
    )
    result = await pipeline._attach_navigation_tree("trace-3011", extraction)
    assert len(result.navigation) == 2
    assert len(calls) == (2 if enabled else 0)
    assert all(bool(node.summary) is enabled for node in result.navigation)
    numbered = extraction_sections(result, "numbered", SectionRulesStore())
    parser = extraction_sections(result, "parser", SectionRulesStore())
    assert [section.title for section in numbered] == ["1. 節 1", "2. 節 2"]
    assert [section.title for section in parser] == ["解析見出し 1", "解析見出し 2"]
    assert all(section.id.startswith("rule-") for section in numbered)
    # 現行の要約は解析見出しに属する。規則の ID に推測で付け替えず、画面で未対応を案内する。
    assert {section.id for section in numbered}.isdisjoint(
        node.section_id for node in result.navigation
    )
    summary_elements = [
        element for element in result.elements if element.content_kind == "section_summary"
    ]
    assert len(summary_elements) == (2 if enabled else 0)


@pytest.mark.parametrize("enabled", [False, True])
async def test_rule_summaries_match_visible_sections_and_keep_parser_tree(
    enabled: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import cast

    from app.config import Settings
    from app.rag import section_rules
    from app.rag.ingestion import IngestionPipeline

    store = section_rules.SectionRulesStore(mode="numbered")
    monkeypatch.setattr(section_rules, "load_section_rules", lambda: store)
    calls: list[str] = []

    class Model:
        async def generate(self, prompt: str, text: str) -> str:
            calls.append(text)
            return "合成の要約"

    extraction = StructuredExtraction(
        elements=[
            DocumentElement(
                kind="text",
                text=f"{title}\n" + "合成の本文。" * 60,
                order=index,
                element_id=f"body-{index}",
                page_number=index + 1,
                section_path=["解析見出し A"],
            )
            for index, title in enumerate(["1. 節 A", "1.1 節 B", "2. 節 C"])
        ]
    )
    pipeline = IngestionPipeline(
        vlm=cast(Any, Model()),
        oracle=cast(Any, object()),
        settings=Settings(rag_navigation_summary_enabled=enabled),
    )
    result = await pipeline._attach_navigation_tree("trace", extraction)
    visible = section_rules.extraction_sections(result, "numbered", store)
    assert [node.section_id for node in result.navigation] == [
        section.source_section_id for section in visible
    ]
    assert [node.title for node in result.navigation] == ["1. 節 A", "1.1 節 B", "2. 節 C"]
    assert result.navigation[1].parent_section_id == result.navigation[0].section_id
    assert result.navigation[0].element_ids == ["body-0"]
    assert result.navigation[1].element_ids == ["body-1"]
    assert len(calls) == (3 if enabled else 0)
    assert all(bool(node.summary) is enabled for node in result.navigation)
    parser = section_rules.extraction_sections(result, "parser", store)
    assert [section.title for section in parser] == ["解析見出し A"]
    assert section_rules.extraction_section_anchors(result, "parser", store)[
        parser[0].id
    ] == frozenset({"body-0", "body-1", "body-2"})
    if enabled:
        again = await pipeline._attach_navigation_tree("trace", result)
        assert len(again.elements) == len(result.elements)
        assert len(again.navigation) == len(result.navigation)


async def test_recipe_section_mode_overrides_global_for_summaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import cast

    from app.config import Settings
    from app.rag import section_rules
    from app.rag.ingestion import IngestionPipeline

    monkeypatch.setattr(
        section_rules, "load_section_rules", lambda: section_rules.SectionRulesStore(mode="parser")
    )

    class Oracle:
        async def get_document_recipe(self, document_id: str, recipe_id: str) -> dict[str, Any]:
            assert (document_id, recipe_id) == ("doc-a", "recipe-a")
            return {"processing_config": {"section_rules_mode": "numbered"}}

    pipeline = IngestionPipeline(
        oracle=cast(Any, Oracle()), settings=Settings(), recipe_id="recipe-a"
    )
    result = await pipeline._attach_navigation_tree(
        "trace",
        StructuredExtraction(
            elements=[
                DocumentElement(
                    kind="text",
                    text="1. 節 A",
                    order=0,
                    element_id="body-a",
                    section_path=["解析見出し A"],
                )
            ]
        ),
        document_id="doc-a",
    )
    assert result.navigation[0].title == "1. 節 A"
