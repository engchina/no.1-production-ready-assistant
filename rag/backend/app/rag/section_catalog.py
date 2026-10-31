"""章節の目録の行（章節を 1 行ずつ、階層で字下げ。見出しの語の無い章節は本文の書き出しを添える）。

MCP の ``rag_outline`` の compact（#2252）と、回答のときに条の目録から条を選ぶ段（#2254）が
同じ行を使う。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from app.clients.oracle import OracleClient
from app.clients.section_map_store import SectionMapStore
from app.rag.section_chunk_map import SectionNode
from app.rag.section_map_service import SectionOutline
from app.schemas.search import RetrievedChunk

# 見出しの語の無い章節の名前（「第十一条の二」「第3条」「附則」だけ。#2252）。番号の語を除くと
# 空になる。
_NUMBERING_ONLY = re.compile(
    r"[第条章節款編項号目のノ附則〇一二三四五六七八九十百千0-9０-９\s.．・-]+"
)
_LEAD_CAPTION = re.compile(r"^\s*[（(][^）)]{0,60}[）)]\s*")
# 本文の書き出しの先頭の項の番号（「第一項」。条の書き出しには要らない）。
_LEAD_PARAGRAPH = re.compile(r"^第[〇一二三四五六七八九十0-9０-９]+項\s*")


def numbering_only(title: str) -> bool:
    return bool(title.strip()) and _NUMBERING_ONLY.fullmatch(title.strip()) is not None


def section_lead(title: str, text: str, limit: int) -> str:
    """章節の最初の chunk の本文の書き出し（章節の名前と見出しの括弧を除く）。"""
    body = " ".join(unicodedata.normalize("NFKC", text or "").split())
    name = " ".join(unicodedata.normalize("NFKC", title).split())
    position = body.find(name)
    if 0 <= position <= 80:
        body = body[position + len(name) :]
    body = _LEAD_PARAGRAPH.sub("", _LEAD_CAPTION.sub("", body).strip()).strip()
    return body[:limit]


async def compact_section_lines(
    oracle: OracleClient,
    outline: SectionOutline,
    nodes: Sequence[SectionNode],
    lead_chars: int,
    *,
    max_chars: int,
) -> tuple[list[str], int]:
    """目録の行（文字数の上限まで。``nodes`` の先頭から順に対応）と、省いた章節の数（#2252）。"""
    leads: dict[str, RetrievedChunk] = {}
    bare = [node.section_id for node in nodes if lead_chars and numbering_only(node.title)]
    if bare:
        leads = await SectionMapStore(oracle).section_first_chunks(
            outline.chunk_set.document_id,
            chunk_set_id=outline.chunk_set.chunk_set_id,
            section_ids=bare,
        )
    base = min((node.level for node in nodes), default=1)
    lines: list[str] = []
    used = 0
    for node in nodes:
        line = "  " * max(0, node.level - base) + node.title
        lead = leads.get(node.section_id)
        if lead is not None:
            text = section_lead(node.title, lead.text, lead_chars)
            if text:
                line = f"{line}｜{text}"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return lines, len(nodes) - len(lines)
