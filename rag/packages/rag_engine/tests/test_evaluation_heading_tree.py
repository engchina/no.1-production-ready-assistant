"""評価の PDF の見出しの木が、文書の目次（原稿の HTML の h1〜h6）どおりになる（#1517）。

fixture（``fixtures/evaluation_layout_headings.json``）は、評価の PDF（``rag/evaluation/multi-hop``・
``business-support``）を Docling で解析した layout record を縮めたもの。Docling の見出しの level は PDF では
すべて 1 のため、深さは番号の体系（「第 N 章」「N.N」など）から決まる。章節ナビゲーションの元になる要素の
section_path（``layout_section_paths``）と、chunk の section_path の両方を確かめる。
"""

from __future__ import annotations

import json
import re
import unicodedata
from html.parser import HTMLParser
from pathlib import Path

import pytest

from rag_engine.chunking import (
    CHILD_CHUNK_LEVEL,
    SECTION_CATEGORIES,
    ChunkingConfig,
    build_small_to_big_chunks,
    layout_section_paths,
)

RAG_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = Path(__file__).parent / "fixtures" / "evaluation_layout_headings.json"
EVALUATION_SETS = ("multi-hop", "business-support")
DOCUMENTS: dict[str, list[dict]] = {
    name: document["records"] for name, document in json.loads(FIXTURE.read_text(encoding="utf-8"))["documents"].items()
}


def _key(text: str) -> str:
    """見出しの照合の鍵。PDF の文字の間の空白（「2023 年度」）と全角 / 半角の差を無視する。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


class _HeadingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.headings: list[tuple[int, str]] = []
        self._level: int | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if re.fullmatch(r"h[1-6]", tag):
            self._level, self._text = int(tag[1]), []

    def handle_data(self, data: str) -> None:
        if self._level is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._level is not None and tag == f"h{self._level}":
            self.headings.append((self._level, "".join(self._text).strip()))
            self._level = None


def _expected_paths(name: str) -> list[tuple[str, ...]]:
    """原稿の HTML の見出し（h1〜h6）から、各見出しの path（表題からの並び）を作る。"""
    folder, file_name = name.split("/", 1)
    source = RAG_ROOT / "evaluation" / folder / "sources" / file_name.replace(".pdf", ".html")
    parser = _HeadingParser()
    parser.feed(source.read_text(encoding="utf-8"))
    stack: list[tuple[int, str]] = []
    paths: list[tuple[str, ...]] = []
    for level, title in parser.headings:
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, _key(title)))
        paths.append(tuple(item for _level, item in stack))
    return paths


def _navigation_paths(records: list[dict]) -> list[tuple[str, ...]]:
    """要素の section_path から章節ナビゲーションと同じく、全 prefix を初出の順に並べる。"""
    section_paths = layout_section_paths(records)
    paths: list[tuple[str, ...]] = []
    for record in records:
        path = tuple(_key(item) for item in section_paths.get(record["id"], []))
        for depth in range(1, len(path) + 1):
            if path[:depth] not in paths:
                paths.append(path[:depth])
    return paths


def test_fixture_covers_every_evaluation_pdf() -> None:
    pdfs = {f"{folder}/{path.name}" for folder in EVALUATION_SETS for path in (RAG_ROOT / "evaluation" / folder).glob("*.pdf")}
    assert set(DOCUMENTS) == pdfs


def test_docling_heading_levels_carry_no_depth() -> None:
    """Docling の見出しの level は PDF ではすべて 1（深さの根拠にならない）。番号を優先する理由。"""
    levels = {
        record["raw"].get("level") for records in DOCUMENTS.values() for record in records if record["category"] in SECTION_CATEGORIES
    }
    assert levels == {1}


@pytest.mark.parametrize("name", sorted(DOCUMENTS), ids=sorted(DOCUMENTS))
def test_navigation_heading_tree_follows_document_toc(name: str) -> None:
    assert _navigation_paths(DOCUMENTS[name]) == _expected_paths(name)


@pytest.mark.parametrize("name", sorted(DOCUMENTS), ids=sorted(DOCUMENTS))
def test_chunk_section_paths_follow_document_toc(name: str) -> None:
    payload = {
        "run_id": "eval1517",
        "pdf_name": name.split("/", 1)[1],
        "records": DOCUMENTS[name],
        "engines": [{"engine": "docling", "label": "Docling"}],
    }
    chunks = build_small_to_big_chunks(payload, source_run_id="eval1517", selected_engine_ids=["docling"], config=ChunkingConfig())
    expected = set(_expected_paths(name))
    children = [chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]
    assert children
    for chunk in children:
        path = tuple(_key(item) for item in chunk.metadata.get("section_path") or [])
        assert path in expected, (chunk.chunk_id, chunk.metadata.get("section_path"))


def test_server_room_chapters_after_subsections_return_to_chapter_level() -> None:
    """「3.3 消火設備」の後の「第 4 章」は第 3 章の兄弟に戻る（3.3 の下に入らない）。"""
    records = DOCUMENTS["multi-hop/server-room-inspection.pdf"]
    section_paths = layout_section_paths(records)
    by_text = {record["text"]: section_paths[record["id"]] for record in records if record["category"] in SECTION_CATEGORIES}
    assert by_text["3.3 消火設備"] == ["サーバー室の定期点検要領", "第 3 章 設備ごとの点検", "3.3 消火設備"]
    assert by_text["第 4 章 点検を中止する条件"] == ["サーバー室の定期点検要領", "第 4 章 点検を中止する条件"]
