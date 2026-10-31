"""関係情報(文書と章・節の見出しのつながり)の決定的 index builder。

外部 LLM / graph DB は使わず、構造化抽出と chunk metadata から Oracle 内の entity
(文書全体・章節の見出し・表 / 図のラベル)と relationship(文書全体が見出しを「含む」)を作る。
構築した関係情報はナレッジベースの「文書構造グラフ」の表示に使い、``rag_graph_expansion_enabled``
(既定 OFF)を ON にすると回答の検索の候補を広げるのにも使う(``app.rag.graph_expansion``。#2055)。
claims / community summary は読む経路が無かったため作らない(#621)。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Literal

from rag_parser_core.language import current_pack

from app.rag.chunking import Chunk
from app.schemas.extraction import StructuredExtraction

GRAPH_ENTITY_MAX_PER_KB = 80


@dataclass(frozen=True)
class GraphEntity:
    """Oracle rag_graph_entities に保存する entity。"""

    entity_id: str
    knowledge_base_id: str | None
    canonical_name: str
    entity_type: str
    description: str
    confidence: float
    source_document_ids: list[str]


@dataclass(frozen=True)
class GraphRelationship:
    """Oracle rag_graph_relationships に保存する relationship。"""

    relationship_id: str
    knowledge_base_id: str | None
    source_entity_id: str
    target_entity_id: str
    relationship_type: str
    description: str
    confidence: float
    source_document_ids: list[str]


@dataclass(frozen=True)
class GraphEntityChunkLink:
    """Oracle rag_graph_entity_chunks に保存する entity-chunk link。"""

    entity_id: str
    chunk_id: str
    document_id: str
    relevance_score: float


@dataclass(frozen=True)
class GraphIndex:
    """1 document から生成した関係情報の index。"""

    entities: list[GraphEntity] = field(default_factory=list)
    relationships: list[GraphRelationship] = field(default_factory=list)
    entity_chunk_links: list[GraphEntityChunkLink] = field(default_factory=list)


def build_graph_index(
    *,
    document_id: str,
    knowledge_base_ids: list[str],
    extraction: StructuredExtraction,
    chunks: list[Chunk],
    chunk_set_id: str | None = None,
    document_name: str | None = None,
) -> GraphIndex:
    """構造化抽出と chunk から関係情報(entity / relationship / entity-chunk link)を生成する。

    ``document_name``(文書のファイル名)は文書の節点の名前に使う(#2051。無ければ文書の種類)。
    """
    if not chunks:
        return GraphIndex()
    kb_ids: list[str | None] = list(knowledge_base_ids) if knowledge_base_ids else [None]
    entities: list[GraphEntity] = []
    relationships: list[GraphRelationship] = []
    links: list[GraphEntityChunkLink] = []
    for knowledge_base_id in kb_ids:
        kb_index = _build_graph_index_for_kb(
            document_id=document_id,
            identity_scope=chunk_set_id or document_id,
            chunk_set_id=chunk_set_id,
            knowledge_base_id=knowledge_base_id,
            extraction=extraction,
            chunks=chunks,
            document_name=document_name,
        )
        entities.extend(kb_index.entities)
        relationships.extend(kb_index.relationships)
        links.extend(kb_index.entity_chunk_links)
    return GraphIndex(
        entities=entities,
        relationships=relationships,
        entity_chunk_links=links,
    )


def _build_graph_index_for_kb(
    *,
    document_id: str,
    identity_scope: str,
    chunk_set_id: str | None,
    knowledge_base_id: str | None,
    extraction: StructuredExtraction,
    chunks: list[Chunk],
    document_name: str | None = None,
) -> GraphIndex:
    document_entity = _document_entity(
        document_id=document_id,
        identity_scope=identity_scope,
        knowledge_base_id=knowledge_base_id,
        extraction=extraction,
        document_name=document_name,
    )
    entity_by_name: dict[str, GraphEntity] = {document_entity.canonical_name: document_entity}
    relationships: dict[str, GraphRelationship] = {}
    links: dict[tuple[str, str], GraphEntityChunkLink] = {}

    for chunk in chunks:
        names = _entity_names_for_chunk(chunk)
        for name in names:
            entity = entity_by_name.get(name)
            if entity is None and len(entity_by_name) < GRAPH_ENTITY_MAX_PER_KB:
                entity = _section_entity(
                    document_id=document_id,
                    identity_scope=identity_scope,
                    knowledge_base_id=knowledge_base_id,
                    chunk=chunk,
                    canonical_name=name,
                )
                entity_by_name[name] = entity
                relationship = _contains_relationship(
                    document_id=document_id,
                    identity_scope=identity_scope,
                    knowledge_base_id=knowledge_base_id,
                    source_entity_id=document_entity.entity_id,
                    target_entity_id=entity.entity_id,
                    target_name=name,
                )
                relationships[relationship.relationship_id] = relationship
            if entity is None:
                continue
            chunk_id = _chunk_id(document_id, chunk, chunk_set_id=chunk_set_id)
            links[(entity.entity_id, chunk_id)] = GraphEntityChunkLink(
                entity_id=entity.entity_id,
                chunk_id=chunk_id,
                document_id=document_id,
                relevance_score=_chunk_relevance(chunk),
            )

    return GraphIndex(
        entities=list(entity_by_name.values()),
        relationships=list(relationships.values()),
        entity_chunk_links=list(links.values()),
    )


def _document_entity(
    *,
    document_id: str,
    identity_scope: str,
    knowledge_base_id: str | None,
    extraction: StructuredExtraction,
    document_name: str | None = None,
) -> GraphEntity:
    pack = current_pack()
    document_type = _clean_label(extraction.document_type) or pack.text(
        "labels", "default_document_type"
    )
    # 文書の種類だけでは同じ種類の文書が同じ名前になり、グラフで区別できない(#2051)。
    # 文書全体の名前は言語資源(#2181)。
    whole = pack.text("labels", "whole_document")
    canonical_name = f"{whole}: {_clean_label(document_name or '') or document_type}"
    return GraphEntity(
        entity_id=_graph_id("entity", identity_scope, knowledge_base_id, canonical_name),
        knowledge_base_id=knowledge_base_id,
        canonical_name=canonical_name,
        entity_type="document",
        description=pack.text("labels", "graph_document").format(document_type=document_type),
        confidence=_confidence(extraction.confidence),
        source_document_ids=[document_id],
    )


def _section_entity(
    *,
    document_id: str,
    identity_scope: str,
    knowledge_base_id: str | None,
    chunk: Chunk,
    canonical_name: str,
) -> GraphEntity:
    content_kind = _metadata_str(chunk, "content_kind") or "text"
    page = _metadata_str(chunk, "page_number")
    description_parts = [
        current_pack()
        .text("labels", "graph_section")
        .format(name=canonical_name, kind=content_kind)
    ]
    if page:
        description_parts.append(f"page={page}")
    return GraphEntity(
        entity_id=_graph_id("entity", identity_scope, knowledge_base_id, canonical_name),
        knowledge_base_id=knowledge_base_id,
        canonical_name=canonical_name,
        entity_type=_entity_type_for_content_kind(content_kind),
        description=" ".join(description_parts),
        confidence=_confidence(_metadata_float(chunk, "confidence")),
        source_document_ids=[document_id],
    )


def _contains_relationship(
    *,
    document_id: str,
    identity_scope: str,
    knowledge_base_id: str | None,
    source_entity_id: str,
    target_entity_id: str,
    target_name: str,
) -> GraphRelationship:
    relationship_id = _graph_id(
        "relationship",
        identity_scope,
        knowledge_base_id,
        source_entity_id,
        target_entity_id,
        "contains",
    )
    return GraphRelationship(
        relationship_id=relationship_id,
        knowledge_base_id=knowledge_base_id,
        source_entity_id=source_entity_id,
        target_entity_id=target_entity_id,
        relationship_type="contains",
        description=current_pack()
        .text("labels", "graph_contains")
        .format(whole=current_pack().text("labels", "whole_document"), target=target_name),
        confidence=1.0,
        source_document_ids=[document_id],
    )


def _entity_names_for_chunk(chunk: Chunk) -> list[str]:
    names: list[str] = []
    section_title = _metadata_str(chunk, "section_title")
    section_path = _metadata_str(chunk, "section_path")
    content_kind = _metadata_str(chunk, "content_kind")
    if section_path:
        names.append(section_path)
    if section_title:
        names.append(section_title)
    if content_kind in {"table", "figure"}:
        label = section_title or section_path or current_pack().text("labels", "whole_document")
        names.append(f"{label} ({content_kind})")
    if not names:
        names.append(current_pack().text("labels", "whole_document"))
    return _dedupe_labels(names)


def _chunk_id(
    document_id: str,
    chunk: Chunk,
    *,
    chunk_set_id: str | None,
) -> str:
    if chunk_set_id is not None:
        return f"{document_id}:{chunk_set_id}:{chunk.index}"
    return f"{document_id}:{chunk.index}"


def _chunk_relevance(chunk: Chunk) -> float:
    kind = _metadata_str(chunk, "content_kind")
    if kind == "table":
        return 1.0
    if kind == "figure":
        return 0.9
    return 0.8


def _entity_type_for_content_kind(content_kind: str) -> str:
    if content_kind == "table":
        return "table_section"
    if content_kind == "figure":
        return "figure_section"
    return "section"


def _metadata_str(chunk: Chunk, key: str) -> str | None:
    value = chunk.metadata.get(key)
    if value is None or isinstance(value, bool):
        return None
    cleaned = str(value).strip()
    return cleaned[:512] if cleaned else None


def _metadata_float(chunk: Chunk, key: str) -> float | None:
    value = chunk.metadata.get(key)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _confidence(value: float | None) -> float:
    if value is None:
        return 1.0
    return max(0.0, min(float(value), 1.0))


def _clean_label(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()[:512]


def _dedupe_labels(values: list[str]) -> list[str]:
    seen: set[str] = set()
    labels: list[str] = []
    for value in values:
        label = _clean_label(value)
        if not label or label in seen:
            continue
        seen.add(label)
        labels.append(label)
    return labels[:4]


def _graph_id(
    kind: Literal["entity", "relationship"],
    *parts: object,
) -> str:
    payload = "|".join([kind, *(str(part or "") for part in parts)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
