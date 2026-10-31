"""章節と chunk の対応を決める（#1438）。

章節の正本は文書の詳細の「章節ナビゲーション」（人の修正 ``rag_document_sections`` と、
修正が無いときの抽出の章節。``app.rag.document_sections_service.document_sections``）で、
100% 正しいものとして扱う。
章節は何度でも直されるが、chunk・embedding・ベクトル索引は作り直さない。代わりに、章節を直すたびに
「どの chunk がどの章節に入るか」の対応だけを決め直す（この module は DB を読まない決定論の関数）。

決め方:

1. 章節を並び順に見て、それぞれの**始まりの chunk**（読み順 = ``chunk_index``）を決める。
   始まりは前の章節の始まりより前にしない。
   - 解析の要素の ID（``anchors``。同じ解析の結果の chunk_set のときだけ）を含む最初の chunk。
   - 無ければ、章節の開始のページの中で、本文の最初の行が見出しの名前で始まる（終わる）か、
     chunk の見出しの列に名前を持つ最初の chunk（同じページに始まる章節を分ける）。
   - 無ければ、章節の開始のページ以降で始まる最初の chunk。
   - ページも名前も無い章節は chunk を持たない（空の章節）。
   要素・名前・ページの順に信頼し、要素や名前で決めた chunk でもページの範囲の外なら使わない
   （人がページを直した章節）。
2. 各 chunk の**所有の章節**は、始まりがその chunk 以前の最後の章節（最も深い・最も後の章節）。
   所有の章節の終了のページより後ろの chunk は、終了のページがその chunk を含む祖先へ、無ければ文書
   全体へ移す。
3. chunk は所有の章節と、その祖先の章節と、文書全体（仮想の根の章節 ``ROOT_SECTION_ID``）に入る。
   章節の無い文書は、文書全体が 1 つの章節になる。

親の章節は並びから決まる（直前の、自分より浅い階層の章節。``DocumentSection`` と同じ）。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.schemas.document import DocumentSection

# 文書全体を表す仮想の根の章節の id（章節の id は文書の中でだけ一意）。
ROOT_SECTION_ID = "document"
ROOT_SECTION_LEVEL = 0
_SPACE = re.compile(r"\s+")
# Markdown の見出しの印（本文の最初の行の照合の前に外す）。
_HEADING_MARK = re.compile(r"^\s*#{1,6}\s+")


@dataclass(frozen=True)
class MapChunk:
    """対応を決めるための chunk の位置（本文は先頭だけ）。"""

    chunk_id: str
    chunk_index: int
    page_start: int | None = None
    page_end: int | None = None
    element_ids: tuple[str, ...] = ()
    section_path: tuple[str, ...] = ()
    head: str = ""
    chars: int = 0


@dataclass(frozen=True)
class SectionNode:
    """対応の中の 1 つの章節（根の章節を含む）。``ordinal`` は文書の中の並び（根は 0）。"""

    section_id: str
    title: str
    level: int
    ordinal: int
    parent_section_id: str | None
    page_start: int | None
    page_end: int | None
    chunk_count: int = 0
    chars: int = 0
    first_chunk_index: int | None = None
    last_chunk_index: int | None = None


@dataclass(frozen=True)
class SectionChunkLink:
    """章節に入る chunk（``chunk_order`` は章節の中の順。0 始まり）。"""

    section_id: str
    chunk_id: str
    chunk_index: int
    chunk_order: int
    is_owner: bool


@dataclass(frozen=True)
class SectionChunkMap:
    """1 つの chunk_set の章節と chunk の対応。"""

    chunk_set_id: str
    sections: list[SectionNode] = field(default_factory=list)
    links: list[SectionChunkLink] = field(default_factory=list)

    def owner_of(self, chunk_id: str) -> str | None:
        return next(
            (link.section_id for link in self.links if link.chunk_id == chunk_id and link.is_owner),
            None,
        )

    def chunk_ids(self, section_id: str) -> list[str]:
        """章節の chunk の id（章節の中の順）。"""
        members = [link for link in self.links if link.section_id == section_id]
        return [link.chunk_id for link in sorted(members, key=lambda link: link.chunk_order)]


def normalize_title(text: str) -> str:
    """見出しの照合に使う形（NFKC・小文字・空白なし）。"""
    return _SPACE.sub("", unicodedata.normalize("NFKC", text)).lower()


def section_parents(sections: Sequence[DocumentSection]) -> list[str]:
    """並び順 + 階層の一覧の、各章節の親の id（直前の、自分より浅い階層の章節。無ければ根）。"""
    parents: list[str] = []
    stack: list[DocumentSection] = []
    for section in sections:
        while stack and stack[-1].level >= section.level:
            stack.pop()
        parents.append(stack[-1].id if stack else ROOT_SECTION_ID)
        stack.append(section)
    return parents


def _page_of(chunk: MapChunk) -> int | None:
    return chunk.page_start if chunk.page_start is not None else chunk.page_end


def _within_pages(chunk: MapChunk, section: DocumentSection) -> bool:
    """chunk が章節のページの範囲に入るか（ページの分からない側は入るとみなす）。"""
    first = _page_of(chunk)
    last = chunk.page_end if chunk.page_end is not None else first
    if section.page_start is None or first is None or last is None:
        return True
    if last < section.page_start:
        return False
    return section.page_end is None or first <= section.page_end


def _on_start_page(chunk: MapChunk, section: DocumentSection) -> bool:
    """chunk が章節の開始のページにあるか（ページの分からない側はあるとみなす）。"""
    first = _page_of(chunk)
    last = chunk.page_end if chunk.page_end is not None else first
    if section.page_start is None or first is None or last is None:
        return True
    return first <= section.page_start <= last


def _first_line(text: str) -> str:
    for line in text.splitlines():
        cleaned = normalize_title(_HEADING_MARK.sub("", line))
        if cleaned:
            return cleaned
    return ""


def _title_matches(chunk: MapChunk, title: str) -> bool:
    """chunk が見出しの名前で始まるか（見出しの列か、本文の最初の行の始まり・終わりが名前）。

    本文の途中の言及（「この手順書は」の「手順」）は見出しとみなさない。最初の行の終わりで照合
    するのは、番号を見出しの名前に含めない章節（名前「総則」・行「第1章 総則」）のため。
    """
    if not title:
        return False
    if any(normalize_title(part) == title for part in chunk.section_path):
        return True
    line = _first_line(chunk.head)
    return line.startswith(title) or line.endswith(title)


def _start_position(
    section: DocumentSection,
    chunks: Sequence[MapChunk],
    anchors: Mapping[str, frozenset[str]],
    floor: int,
) -> int | None:
    """章節の始まりの chunk の位置（``chunks`` の添字。決まらなければ None）。"""
    candidates = range(floor, len(chunks))
    anchor_ids = anchors.get(section.id) or (
        anchors.get(section.source_section_id) if section.source_section_id else None
    )
    if anchor_ids:
        for position in candidates:
            chunk = chunks[position]
            if anchor_ids.intersection(chunk.element_ids) and _within_pages(chunk, section):
                return position
    title = normalize_title(section.title)
    if title:
        for position in candidates:
            chunk = chunks[position]
            if _on_start_page(chunk, section) and _title_matches(chunk, title):
                return position
    if section.page_start is not None:
        for position in candidates:
            page = _page_of(chunks[position])
            if page is not None and page >= section.page_start:
                return position
    return None


def build_section_chunk_map(
    chunk_set_id: str,
    sections: Sequence[DocumentSection],
    chunks: Sequence[MapChunk],
    *,
    anchors: Mapping[str, frozenset[str]] | None = None,
    root_title: str = "",
) -> SectionChunkMap:
    """章節（並び順 + 階層の一覧）と chunk の対応を決める（決定論。DB を読まない）。"""
    ordered = sorted(chunks, key=lambda chunk: (chunk.chunk_index, chunk.chunk_id))
    anchor_map = anchors or {}
    parents = section_parents(sections)
    parent_index: list[int | None] = []
    index_by_id = {section.id: index for index, section in enumerate(sections)}
    for parent in parents:
        parent_index.append(index_by_id.get(parent) if parent != ROOT_SECTION_ID else None)

    starts: list[int | None] = []
    floor = 0
    for section in sections:
        start = _start_position(section, ordered, anchor_map, floor)
        starts.append(start)
        if start is not None:
            floor = start

    # 所有の章節: 始まりがその chunk 以前の最後の章節。
    owners: list[int | None] = []
    cursor: int | None = None
    pointer = 0
    started = [(start, index) for index, start in enumerate(starts) if start is not None]
    for position, chunk in enumerate(ordered):
        while pointer < len(started) and started[pointer][0] <= position:
            cursor = started[pointer][1]
            pointer += 1
        owner = cursor
        page = _page_of(chunk)
        while owner is not None and page is not None:
            end = sections[owner].page_end
            if end is None or page <= end:
                break
            owner = parent_index[owner]
        owners.append(owner)

    members: dict[str, list[tuple[MapChunk, bool]]] = {ROOT_SECTION_ID: []}
    for section in sections:
        members.setdefault(section.id, [])
    for chunk, owner in zip(ordered, owners, strict=True):
        members[ROOT_SECTION_ID].append((chunk, owner is None))
        current = owner
        first = True
        while current is not None:
            members[sections[current].id].append((chunk, first))
            first = False
            current = parent_index[current]

    links: list[SectionChunkLink] = []
    nodes: list[SectionNode] = []
    pages = [page for chunk in ordered for page in (chunk.page_start, chunk.page_end) if page]

    def node(
        section_id: str,
        title: str,
        level: int,
        ordinal: int,
        parent: str | None,
        page_start: int | None,
        page_end: int | None,
    ) -> SectionNode:
        items = members.get(section_id, [])
        for order, (chunk, is_owner) in enumerate(items):
            links.append(
                SectionChunkLink(
                    section_id=section_id,
                    chunk_id=chunk.chunk_id,
                    chunk_index=chunk.chunk_index,
                    chunk_order=order,
                    is_owner=is_owner,
                )
            )
        return SectionNode(
            section_id=section_id,
            title=title,
            level=level,
            ordinal=ordinal,
            parent_section_id=parent,
            page_start=page_start,
            page_end=page_end,
            chunk_count=len(items),
            chars=sum(chunk.chars for chunk, _owner in items),
            first_chunk_index=items[0][0].chunk_index if items else None,
            last_chunk_index=items[-1][0].chunk_index if items else None,
        )

    nodes.append(
        node(
            ROOT_SECTION_ID,
            root_title,
            ROOT_SECTION_LEVEL,
            0,
            None,
            min(pages) if pages else None,
            max(pages) if pages else None,
        )
    )
    for ordinal, (section, parent) in enumerate(zip(sections, parents, strict=True), start=1):
        nodes.append(
            node(
                section.id,
                section.title,
                section.level,
                ordinal,
                parent,
                section.page_start,
                section.page_end if section.page_end is not None else section.page_start,
            )
        )
    return SectionChunkMap(chunk_set_id=chunk_set_id, sections=nodes, links=links)
