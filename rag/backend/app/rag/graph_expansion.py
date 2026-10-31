"""関係情報（文書・章節・表 / 図の見出しの entity と chunk の対応）で検索の候補を広げる（#2055）。

「文書構造索引の構築」（``app.rag.graph_index``。取込で LLM を呼ばずに作る）の表（
``rag_graph_entities`` / ``rag_graph_entity_chunks``）を、回答の検索で読む。GraphRAG / LightRAG の
local search（当たった chunk の entity から、その entity の他の chunk へ広げる）の考え方を、構造の
関係情報に当てはめたもの。LLM は呼ばない。

1. **当たった chunk からの広げ**（``neighbor``）: 検索の上位の chunk（``GRAPH_SEED_ANCHORS`` 件）が
   対応する章節・表 / 図の entity のうち、chunk の数が 2〜``GRAPH_NEIGHBOR_MAX_ENTITY_CHUNKS`` の最
   も狭いもの（同じ章節・同じ表）の、ほかの chunk を当たった chunk に近い順に足す。広い章節は回答の
   生成の段の「章節の拡張」（rag_engine の ``section_expansion``。足りないときに章節の全体を読む）
   に任せ、ここでは読まない。
2. **entity の名前との一致**（``name``）: 質問に章節・表 / 図の見出し（``第 2 章`` などの番号を除い
   た ``GRAPH_NAME_MIN_CHARS`` 文字以上の語）が含まれる entity の chunk を、章節の先頭から
   ``GRAPH_NAME_CHUNKS_PER_ENTITY`` 件まで足す（複数の文書にまたがる）。長い見出しから
   ``GRAPH_NAME_MAX_ENTITIES`` 個まで。

足す chunk は検索と同じ見え方の条件（tenant・ナレッジベースの範囲・利用者の権限・有効な chunk_set・
旧版の扱い）の中だけで、1 回の検索で ``rag_graph_expansion_max_chunks`` 件まで（名前の一致と広げで
半分ずつ。片方が余れば他方に回す）。すでに候補・前後の文脈・交差参照・実体の拡張にある chunk は足さ
ない（二重に数えない）。章節の拡張は文脈にある chunk を読み直さない（#1504）ので、ここで足した
chunk を章節の拡張が二重に読むことも無い。関係情報を作っていないナレッジベースでは表が空で、何も足
さない。
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from rag_parser_core.language import current_pack
from rag_parser_core.language.markers import marker_patterns
from rag_parser_core.language.text import numbered, text_patterns

from app.rag.entity_index import entity_key
from app.schemas.search import RetrievedChunk

# 起点にする検索の上位の chunk の数（RRF の上位から。実体の拡張と同じ）。
GRAPH_SEED_ANCHORS = 5
# 当たった chunk から広げる entity の chunk の数の上限（これより広い章節は章節の拡張に任せる）。
GRAPH_NEIGHBOR_MAX_ENTITY_CHUNKS = 8
# 名前の一致で照合する見出しの最短の文字数（「目的」「対象」のような短い見出しは質問の任意の語に
# 一致させない）。
GRAPH_NAME_MIN_CHARS = 4
# 名前の一致で使う entity の数と、1 つの entity から足す chunk の数の上限。
GRAPH_NAME_MAX_ENTITIES = 4
GRAPH_NAME_CHUNKS_PER_ENTITY = 3
# 名前の一致の候補として読む entity の数の上限（1 回の SQL）。
GRAPH_NAME_CANDIDATE_LIMIT = 2000
# 足した chunk の metadata の key（回答の診断に出す）。
GRAPH_EXPANSION_KEY = "graph_expansion"
# 足した理由の種類。
GRAPH_KIND_NEIGHBOR = "neighbor"
GRAPH_KIND_NAME = "name"
# 検索に使う entity の種類（文書全体の entity は chunk を持たないので使わない）。
GRAPH_SECTION_ENTITY_TYPES = ("section", "table_section", "figure_section")


# 見出しが無い chunk をまとめた entity の名前（graph_index の既定）。広すぎるので使わない。
def _fallback_entity_name() -> str:
    """見出しが無い chunk をまとめた entity の名前（言語資源。graph_index と同じ）。"""
    return current_pack().text("labels", "whole_document")


_PATH_SEPARATOR = " > "
_KIND_SUFFIX = re.compile(r"\s*\((?:table|figure)\)$")
_heading_numbers: dict[str, re.Pattern[str]] = {}


def _heading_number() -> re.Pattern[str]:
    """見出しの先頭の番号（「第 2 章」「1.2」「(3)」「２．」など）。

    章・条などの書き方は言語資源(#2181)。
    """
    text = text_patterns()
    markers = marker_patterns()
    pattern = _heading_numbers.get(current_pack().hash)
    if pattern is None:
        units = "|".join(
            getattr(text, key)
            for key in (
                "part_units",
                "chapter_units",
                "section_units",
                "subsection_units",
                "division_units",
                "article_units",
                "paragraph_units",
            )
        )
        pattern = re.compile(
            r"^(?:"
            rf"{numbered(text, f'(?:{units})')}\s*"
            rf"|{markers.number_open}\s*\d+\s*{markers.number_close}\s*"
            rf"|\d+(?:[.．]\d+)*{markers.number_closer}?\s+"
            r")"
        )
        _heading_numbers.clear()
        _heading_numbers[current_pack().hash] = pattern
    return pattern


@dataclass(frozen=True)
class GraphEntityRow:
    """関係情報の entity（``OracleGraphStore`` の 1 行）。"""

    entity_id: str
    canonical_name: str
    entity_type: str
    # entity に対応する chunk の数（広さの目安）。
    chunk_count: int
    # この entity に対応する起点の chunk（名前の一致の候補では None）。
    seed_chunk_id: str | None = None


@dataclass(frozen=True)
class GraphChunkRow:
    """entity に対応する chunk（``OracleGraphStore.graph_entity_chunks`` の 1 行）。"""

    entity_id: str
    chunk: RetrievedChunk


class GraphExpansionStore(Protocol):
    """関係情報の表を読む SQL（``OracleGraphStore`` が実装する。テストは同じ規則の代役を使う）。"""

    async def graph_seed_entities(
        self, filters: dict[str, str], *, seed_chunk_ids: Sequence[str]
    ) -> list[GraphEntityRow]: ...

    async def graph_name_entities(
        self, filters: dict[str, str], *, limit: int
    ) -> list[GraphEntityRow]: ...

    async def graph_entity_chunks(
        self, filters: dict[str, str], *, entity_ids: Sequence[str], per_entity: int
    ) -> list[GraphChunkRow]: ...


@dataclass(frozen=True)
class GraphExpansion:
    """足す chunk と、足した理由（診断に出す）。"""

    chunk: RetrievedChunk
    info: dict[str, object]


def heading_label(canonical_name: str) -> str:
    """entity の名前から、照合に使う見出しの語（最後の段・番号と表 / 図の印を除く）。"""
    name = _KIND_SUFFIX.sub("", canonical_name.strip())
    label = name.rsplit(_PATH_SEPARATOR, 1)[-1].strip()
    return _heading_number().sub("", label).strip()


def _usable_entity(row: GraphEntityRow) -> bool:
    return (
        row.entity_type in GRAPH_SECTION_ENTITY_TYPES
        and _KIND_SUFFIX.sub("", row.canonical_name.strip()) != _fallback_entity_name()
    )


async def plan_graph_expansion(
    store: GraphExpansionStore,
    filters: dict[str, str],
    *,
    question: str,
    seed_chunks: Sequence[RetrievedChunk],
    exclude_chunk_ids: set[str],
    max_chunks: int,
    diagnostics: dict[str, object] | None = None,
) -> list[GraphExpansion]:
    """質問と検索の上位の chunk から、関係情報で足す chunk を順に返す（名前の一致 → 広げ）。

    ``exclude_chunk_ids``（すでに候補・前後の文脈などにある chunk）は足さない。関係情報が無ければ
    空。
    """
    if max_chunks <= 0:
        return []
    question_key = entity_key(question)
    seeds = list(seed_chunks[:GRAPH_SEED_ANCHORS])
    seed_rows = (
        await store.graph_seed_entities(filters, seed_chunk_ids=[chunk.chunk_id for chunk in seeds])
        if seeds
        else []
    )
    available_rows = await store.graph_name_entities(filters, limit=GRAPH_NAME_CANDIDATE_LIMIT)
    name_rows = (
        _name_matches(available_rows, question_key)
        if len(question_key) >= GRAPH_NAME_MIN_CHARS
        else []
    )
    neighbor_rows = _neighbor_entities(seed_rows, [chunk.chunk_id for chunk in seeds])
    entity_ids = list(
        dict.fromkeys([*(row.entity_id for row, _ in name_rows), *neighbor_rows.values()])
    )
    if diagnostics is not None:
        diagnostics.update(
            {
                "status": "completed",
                "graph_available": bool(seed_rows or available_rows),
                "seed_entity_ids": list(set(neighbor_rows.values())),
                "name_entity_ids": list({row.entity_id for row, _ in name_rows}),
                "planned_chunk_ids": [],
                "skipped_existing_chunk_ids": [],
            }
        )
    if not entity_ids:
        return []
    chunk_rows = await store.graph_entity_chunks(
        filters,
        entity_ids=entity_ids,
        per_entity=max(GRAPH_NEIGHBOR_MAX_ENTITY_CHUNKS, GRAPH_NAME_CHUNKS_PER_ENTITY),
    )
    by_entity: dict[str, list[RetrievedChunk]] = {}
    for chunk_row in chunk_rows:
        by_entity.setdefault(chunk_row.entity_id, []).append(chunk_row.chunk)
    names = {row.entity_id: row.canonical_name for row in [*seed_rows, *(r for r, _ in name_rows)]}

    name_items: list[GraphExpansion] = []
    for row, label in name_rows:
        for chunk in by_entity.get(row.entity_id, [])[:GRAPH_NAME_CHUNKS_PER_ENTITY]:
            name_items.append(
                GraphExpansion(
                    chunk=chunk,
                    info={
                        "kind": GRAPH_KIND_NAME,
                        "entity_id": row.entity_id,
                        "entity": row.canonical_name,
                        "match": label,
                    },
                )
            )
    neighbor_items: list[GraphExpansion] = []
    seed_index = {chunk.chunk_id: _chunk_index(chunk) for chunk in seeds}
    for seed_id, entity_id in neighbor_rows.items():
        for chunk in _nearest_first(by_entity.get(entity_id, []), seed_index.get(seed_id)):
            neighbor_items.append(
                GraphExpansion(
                    chunk=chunk,
                    info={
                        "kind": GRAPH_KIND_NEIGHBOR,
                        "entity_id": entity_id,
                        "entity": names.get(entity_id, ""),
                        "from_chunk_id": seed_id,
                    },
                )
            )
    if diagnostics is not None:
        candidates = {item.chunk.chunk_id for item in [*name_items, *neighbor_items]}
        candidates.difference_update(chunk.chunk_id for chunk in seeds)
        diagnostics["planned_chunk_ids"] = sorted(candidates)
        diagnostics["skipped_existing_chunk_ids"] = sorted(candidates & exclude_chunk_ids)
    return _take_balanced(name_items, neighbor_items, exclude=exclude_chunk_ids, limit=max_chunks)


def _name_matches(
    rows: Sequence[GraphEntityRow], question_key: str
) -> list[tuple[GraphEntityRow, str]]:
    """見出しの語が質問に含まれる entity（長い見出しから。同じ見出しは chunk の少ないほうから）。

    見出しの語の数は ``GRAPH_NAME_MAX_ENTITIES`` まで（同じ見出しの entity は文書をまたいで全部）。
    """
    matched: list[tuple[GraphEntityRow, str, str]] = []
    for row in rows:
        if not _usable_entity(row):
            continue
        label = heading_label(row.canonical_name)
        key = entity_key(label)
        if len(key) >= GRAPH_NAME_MIN_CHARS and key in question_key:
            matched.append((row, label, key))
    # より長い一致（「権限の見直しの期限」）の一部だけの見出し（「権限の見直し」）は外す。
    keys = {key for _, _, key in matched}
    shadowed = {key for key in keys for other in keys if key != other and key in other}
    ordered = sorted(
        (item for item in matched if item[2] not in shadowed),
        key=lambda item: (-len(item[2]), item[0].chunk_count, item[0].entity_id),
    )
    result: list[tuple[GraphEntityRow, str]] = []
    labels: dict[str, int] = {}
    for row, label, key in ordered:
        if key not in labels and len(labels) >= GRAPH_NAME_MAX_ENTITIES:
            continue
        labels[key] = labels.get(key, 0) + 1
        result.append((row, label))
    return result


def _neighbor_entities(rows: Sequence[GraphEntityRow], seed_ids: Sequence[str]) -> dict[str, str]:
    """起点の chunk ごとに、広げる entity（chunk が 2 つ以上で上限以下の、最も狭いもの）。"""
    by_seed: dict[str, list[GraphEntityRow]] = {}
    for row in rows:
        if row.seed_chunk_id and _usable_entity(row):
            by_seed.setdefault(row.seed_chunk_id, []).append(row)
    chosen: dict[str, str] = {}
    for seed_id in seed_ids:
        candidates = [
            row
            for row in by_seed.get(seed_id, [])
            if 2 <= row.chunk_count <= GRAPH_NEIGHBOR_MAX_ENTITY_CHUNKS
        ]
        if not candidates:
            continue
        best = min(candidates, key=lambda row: (row.chunk_count, row.entity_id))
        if best.entity_id not in chosen.values():
            chosen[seed_id] = best.entity_id
    return chosen


def _take_balanced(
    first: Sequence[GraphExpansion],
    second: Sequence[GraphExpansion],
    *,
    exclude: set[str],
    limit: int,
) -> list[GraphExpansion]:
    """名前の一致と広げを上限の半分ずつ（片方が余れば他方に回す）、重ならずに取る。"""
    used = set(exclude)

    def fresh(items: Sequence[GraphExpansion]) -> list[GraphExpansion]:
        result: list[GraphExpansion] = []
        for item in items:
            if item.chunk.chunk_id not in used:
                used.add(item.chunk.chunk_id)
                result.append(item)
        return result

    names = fresh(first)
    neighbors = fresh(second)
    half = math.ceil(limit / 2)
    take_names = min(len(names), max(half, limit - len(neighbors)))
    take_neighbors = min(len(neighbors), limit - take_names)
    return [*names[:take_names], *neighbors[:take_neighbors]]


def _nearest_first(chunks: Sequence[RetrievedChunk], origin: int | None) -> list[RetrievedChunk]:
    """起点の chunk に近い順（chunk の順の差。順が分からなければ最後）。"""

    def distance(chunk: RetrievedChunk) -> tuple[float, str]:
        index = _chunk_index(chunk)
        if origin is None or index is None:
            return (math.inf, chunk.chunk_id)
        return (float(abs(index - origin)), chunk.chunk_id)

    return sorted(chunks, key=distance)


def _chunk_index(chunk: RetrievedChunk) -> int | None:
    value = chunk.metadata.get("chunk_index")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def graph_expansion_metadata(chunk: RetrievedChunk, info: Mapping[str, object]) -> RetrievedChunk:
    """足した chunk の metadata に、足した理由（``graph_expansion``）を付ける。"""
    return chunk.model_copy(
        update={
            "metadata": {
                **chunk.metadata,
                GRAPH_EXPANSION_KEY: {
                    key: value
                    for key, value in info.items()
                    if isinstance(value, str | int | float | bool) or value is None
                },
            }
        }
    )


__all__ = [
    "GRAPH_EXPANSION_KEY",
    "GRAPH_KIND_NAME",
    "GRAPH_KIND_NEIGHBOR",
    "GRAPH_SEED_ANCHORS",
    "GraphChunkRow",
    "GraphEntityRow",
    "GraphExpansion",
    "GraphExpansionStore",
    "graph_expansion_metadata",
    "heading_label",
    "plan_graph_expansion",
]
