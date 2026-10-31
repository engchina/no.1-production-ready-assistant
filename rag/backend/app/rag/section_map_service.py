"""章節と chunk の対応の作り直しと、章節の本文の読み取り（#1438）。

- 作り直し（``rebuild_document_section_map``）: 文書の章節の正本（``document_sections``）と、
  有効な chunk_set の chunk の位置から対応を決め（``app.rag.section_chunk_map``）、対応の表
  だけを置き換える。
  chunk・embedding・ベクトル索引は変えない。取込の job の成功・章節の保存（PUT / DELETE）・章節の
  抽出規則の変更・レシピの章節の方式の変更の後に呼ぶ（``refresh_document_section_map``。失敗しても
  呼び出し元の処理は止めず、ログに残す）。
- 読み取り（``read_section``）: 章節の id から、章節の chunk を章節の中の順に本文つきで返す
  （検索と同じ見え方の条件）。MCP の ``rag_read_section`` と、回答のときの章節の拡張
  （#1436 / #1437）が共通で使う。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.clients.oracle import OracleClient
from app.clients.section_map_store import ReadableChunkSet, SectionChunkRow, SectionMapStore
from app.rag.document_sections_service import document_sections_with_source
from app.rag.request_context import system_access_context
from app.rag.section_chunk_map import (
    ROOT_SECTION_ID,
    SectionChunkMap,
    SectionNode,
    build_section_chunk_map,
)
from app.rag.section_rules import extraction_section_anchors

logger = logging.getLogger(__name__)

# 章節の本文を 1 回の SQL で読む chunk の数の単位。
READ_SECTION_BATCH = 50
# 規則の変更の後の全文書の作り直し（同じ process の中で 1 つだけ走らせる。参照を持って
# GC させない）。
_refresh_all_task: asyncio.Task[None] | None = None


async def rebuild_document_section_map(
    document_id: str, *, oracle: OracleClient | None = None
) -> list[SectionChunkMap] | None:
    """文書の章節と chunk の対応を作り直す（文書が無ければ None。対応の表だけを変える）。"""
    client = oracle or OracleClient()
    resolved = await document_sections_with_source(client, document_id, None)
    if resolved is None:
        return None
    data, _removed, source = resolved
    store = SectionMapStore(client)
    detail = await client.get_document(document_id)
    root_title = (detail.file_name if detail is not None else None) or document_id
    anchors = extraction_section_anchors(source.extraction, source.rules_mode)
    maps = [
        build_section_chunk_map(
            item.chunk_set_id,
            data.sections,
            item.chunks,
            # 解析の要素の ID は、章節の元にした解析の結果の chunk_set の chunk にだけ当てる。
            anchors=(
                anchors
                if source.extraction_recipe_id is not None
                and item.extraction_recipe_id == source.extraction_recipe_id
                else {}
            ),
            root_title=root_title,
        )
        for item in await store.document_chunk_sets(document_id)
    ]
    await store.replace_document_section_maps(document_id, maps)
    return maps


async def refresh_document_section_map(
    document_id: str, *, oracle: OracleClient | None = None, reason: str
) -> bool:
    """対応を作り直す。失敗しても呼び出し元を止めず、ログに残して False を返す。"""
    try:
        await rebuild_document_section_map(document_id, oracle=oracle)
    except Exception:  # noqa: BLE001 - 対応は補助の情報。取込・章節の保存の結果を止めない
        logger.warning(
            "section_map_refresh_failed",
            extra={"document_id": document_id, "reason": reason},
            exc_info=True,
        )
        return False
    return True


async def refresh_all_section_maps(*, reason: str, oracle: OracleClient | None = None) -> int:
    """有効な chunk_set を持つすべての文書の対応を作り直す（作り直せた文書の数）。"""
    client = oracle or OracleClient()
    try:
        document_ids = await SectionMapStore(client).indexed_document_ids()
    except Exception:  # noqa: BLE001 - 補助の情報。設定の保存の結果を止めない
        logger.warning("section_map_refresh_all_failed", extra={"reason": reason}, exc_info=True)
        return 0
    refreshed = 0
    for document_id in document_ids:
        if await refresh_document_section_map(document_id, oracle=client, reason=reason):
            refreshed += 1
    return refreshed


def schedule_refresh_all_section_maps(*, reason: str) -> None:
    """章節の抽出規則を変えたとき、すべての文書の対応をバックグラウンドで作り直す。

    走っている作り直しがあれば止めて、今の規則で最初から作り直す。
    """
    global _refresh_all_task
    if _refresh_all_task is not None and not _refresh_all_task.done():
        _refresh_all_task.cancel()
    _refresh_all_task = asyncio.get_running_loop().create_task(_refresh_all(reason))


async def _refresh_all(reason: str) -> None:
    # 全体の規則の変更は全文書に効くので、保存した利用者の対象範囲（task が引き継ぐ要求の
    # context）ではなく、範囲の制限の無い context で全文書を作り直す（#3334）。
    with system_access_context():
        count = await refresh_all_section_maps(reason=reason)
    logger.info("section_map_refresh_all_finished", extra={"reason": reason, "count": count})


# ---- 読み取り ----


class SectionNotFoundError(LookupError):
    """章節が無い（文書が見えない・章節の id が無い・chunk が章節に入っていない）。"""


@dataclass(frozen=True)
class SectionOutline:
    """利用者が読める chunk_set の章節の一覧（根の章節を先頭に）。"""

    chunk_set: ReadableChunkSet
    sections: list[SectionNode]

    def get(self, section_id: str) -> SectionNode | None:
        return next((node for node in self.sections if node.section_id == section_id), None)

    def ancestor(self, section: SectionNode, levels: int) -> SectionNode:
        """``levels`` 段上の章節（根より上には上がらない）。"""
        current = section
        for _ in range(max(0, levels)):
            parent = self.get(current.parent_section_id) if current.parent_section_id else None
            if parent is None:
                break
            current = parent
        return current

    def children(self, section_id: str) -> list[SectionNode]:
        return [node for node in self.sections if node.parent_section_id == section_id]

    def path(self, section: SectionNode) -> list[str]:
        """根の下から章節までの名前の列（根の章節は空）。"""
        titles: list[str] = []
        current: SectionNode | None = section
        while current is not None and current.section_id != ROOT_SECTION_ID:
            titles.append(current.title)
            current = self.get(current.parent_section_id) if current.parent_section_id else None
        return list(reversed(titles))


async def section_outline(
    document_id: str,
    *,
    chunk_set_id: str | None = None,
    oracle: OracleClient | None = None,
) -> SectionOutline | None:
    """利用者が読める文書の章節の一覧（見えなければ None）。

    対応がまだ無い chunk_set（この機能の前に取り込んだ文書など）は、その場で作り直してから読む。
    """
    client = oracle or OracleClient()
    store = SectionMapStore(client)
    readable = await store.readable_chunk_set(document_id, chunk_set_id)
    if readable is None:
        return None
    sections = await store.chunk_set_sections(document_id, readable.chunk_set_id)
    if not sections:
        await rebuild_document_section_map(document_id, oracle=client)
        sections = await store.chunk_set_sections(document_id, readable.chunk_set_id)
    if not sections:
        return None
    return SectionOutline(chunk_set=readable, sections=sections)


@dataclass(frozen=True)
class SectionText:
    """章節の本文（章節の中の順の chunk）。``complete`` は章節の最後の chunk まで読んだか。"""

    outline: SectionOutline
    section: SectionNode
    chunks: list[SectionChunkRow]
    complete: bool


async def read_section(
    document_id: str,
    section_id: str,
    *,
    chunk_set_id: str | None = None,
    from_order: int = 0,
    limit: int | None = None,
    filters: Mapping[str, str] | None = None,
    oracle: OracleClient | None = None,
) -> SectionText:
    """章節の chunk を、章節の中の順に ``from_order`` から本文つきで返す（第 2・3 段階と共通）。

    ``limit`` が無ければ章節の最後まで読む。見えない文書・無い章節は ``SectionNotFoundError``。
    ``filters`` は回答の検索の条件（回答の章節の拡張。#1436）で、その条件に入る chunk だけを読む。
    """
    client = oracle or OracleClient()
    outline = await section_outline(document_id, chunk_set_id=chunk_set_id, oracle=client)
    if outline is None:
        raise SectionNotFoundError(document_id)
    section = outline.get(section_id)
    if section is None:
        raise SectionNotFoundError(section_id)
    store = SectionMapStore(client)
    rows: list[SectionChunkRow] = []
    order = max(0, from_order)
    remaining = limit
    while remaining is None or remaining > 0:
        batch_size = READ_SECTION_BATCH if remaining is None else min(remaining, READ_SECTION_BATCH)
        batch = await store.section_chunks(
            document_id,
            chunk_set_id=outline.chunk_set.chunk_set_id,
            section_id=section.section_id,
            from_order=order,
            limit=batch_size,
            filters=filters,
        )
        rows.extend(batch)
        if remaining is not None:
            remaining -= len(batch)
        if len(batch) < batch_size:
            break
        order = batch[-1].chunk_order + 1
    last_order = rows[-1].chunk_order if rows else order - 1
    return SectionText(
        outline=outline,
        section=section,
        chunks=rows,
        complete=last_order + 1 >= section.chunk_count,
    )


async def chunk_section(
    document_id: str, chunk_id: str, *, oracle: OracleClient | None = None
) -> tuple[str, str] | None:
    """利用者が読める chunk の (chunk_set_id, 所有の章節の id)。対応が無ければ作り直して引く。"""
    client = oracle or OracleClient()
    store = SectionMapStore(client)
    found = await store.readable_owner_section(document_id, chunk_id)
    if found is None and await client.retrievable_chunk(document_id, chunk_id) is not None:
        await rebuild_document_section_map(document_id, oracle=client)
        found = await store.readable_owner_section(document_id, chunk_id)
    return found


async def evidence_sections(
    chunk_ids: Sequence[str], *, oracle: OracleClient | None = None
) -> dict[str, tuple[SectionNode, SectionNode | None]]:
    """根拠の chunk ごとの所有の章節と親の章節（対応の無い chunk は含めない）。"""
    return await SectionMapStore(oracle).owner_sections(chunk_ids)
