"""実体の層（#1362）のテストの道具: 評価セットの資料の chunk と、実体の表を Python で持つ store。

- ``multi_hop_corpus_chunks``: ``rag/evaluation/multi-hop/sources`` の原稿から、取込と同じ形の
  chunk を作る（台帳は 1 行 = 1 chunk の「列名: 値 / 列名: 値」（#1349）、PDF の原稿は章ごとに 1
  chunk）。
- ``InMemoryEntityStore``: ``app.clients.entity_store.EntityStore`` の 3 つの SQL と同じ規則（join
  の向き・役割・ナレッジベースの範囲・ラベルの別名の扱い（#1393））を Python の表で行う。実
  Oracle の SQL は ``test_entity_layer_oracle.py``。
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.rag.chunking import Chunk
from app.rag.entity_expansion import (
    MIN_QUESTION_ALIAS_CHARS,
    EntityDefinitionRow,
    EntitySeedRow,
)
from app.rag.entity_index import (
    ALIAS_KIND_LABEL,
    EntityAlias,
    EntityIndex,
    EntityIndexOptions,
    EntityRecord,
    build_entity_index,
    entity_chunk_id,
)
from app.schemas.search import RetrievedChunk

MULTI_HOP_DIR = Path(__file__).resolve().parents[2] / "evaluation" / "multi-hop"
BUSINESS_SUPPORT_DIR = Path(__file__).resolve().parents[2] / "evaluation" / "business-support"
CHUNK_SET_ID = "cs-1"


@dataclass
class CorpusDocument:
    document_id: str
    file_name: str
    chunks: list[Chunk]


def _html_chunks(path: Path) -> list[Chunk]:
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"<style.*?</style>", "", text, flags=re.S)
    text = re.sub(r"<title>.*?</title>", "", text, flags=re.S)
    chunks: list[Chunk] = []
    for index, part in enumerate(re.split(r"<h2[^>]*>", text)):
        body = html.unescape(re.sub(r"<[^>]+>", "\n", part))
        body = "\n".join(line.strip() for line in body.splitlines() if line.strip())
        chunks.append(
            Chunk(
                text=body,
                index=index,
                start_offset=0,
                end_offset=len(body),
                metadata={"content_kind": "text", "section_path": body.splitlines()[0]},
            )
        )
    return chunks


def _workbook_chunks(path: Path) -> list[Chunk]:
    sheet = json.loads(path.read_text(encoding="utf-8"))["sheets"][0]
    preamble = "\n".join(sheet["preamble"])
    chunks = [
        Chunk(
            text=preamble,
            index=0,
            start_offset=0,
            end_offset=len(preamble),
            metadata={"content_kind": "text", "section_path": sheet["title"]},
        )
    ]
    for index, row in enumerate(sheet["rows"], start=1):
        text = " / ".join(
            f"{name}: {value}" for name, value in zip(sheet["header"], row, strict=True) if value
        )
        chunks.append(
            Chunk(
                text=text,
                index=index,
                start_offset=0,
                end_offset=len(text),
                metadata={"content_kind": "record", "section_path": sheet["title"]},
            )
        )
    return chunks


def multi_hop_corpus() -> list[CorpusDocument]:
    """評価セットの 15 文書（PDF の原稿は章ごと、xlsx の原稿は 1 行ずつの chunk）。"""
    return _corpus(MULTI_HOP_DIR)


def business_support_corpus() -> list[CorpusDocument]:
    """多段ではない評価セット ``rag/evaluation/business-support`` の 5 文書（#1393）。"""
    return _corpus(BUSINESS_SUPPORT_DIR)


def _corpus(directory: Path) -> list[CorpusDocument]:
    documents: list[CorpusDocument] = []
    for source in sorted((directory / "sources").iterdir()):
        if source.suffix == ".html":
            file_name = f"{source.stem}.pdf"
            chunks = _html_chunks(source)
        elif source.name.endswith(".workbook.json"):
            file_name = source.name.replace(".workbook.json", ".xlsx")
            chunks = _workbook_chunks(source)
        else:
            continue
        documents.append(CorpusDocument(f"file:{file_name}", file_name, chunks))
    return documents


def retrieved(document: CorpusDocument, chunk: Chunk) -> RetrievedChunk:
    chunk_id = entity_chunk_id(document.document_id, chunk, chunk_set_id=CHUNK_SET_ID)
    return RetrievedChunk(
        document_id=document.document_id,
        chunk_id=chunk_id,
        text=chunk.text,
        score=0.0,
        file_name=document.file_name,
        metadata={
            **chunk.metadata,
            "chunk_id": chunk_id,
            "chunk_set_id": CHUNK_SET_ID,
            "chunk_index": chunk.index,
            "chunk_group_id": chunk_id,
            "page_start": 1,
            "page_end": 1,
        },
    )


@dataclass
class InMemoryEntityStore:
    """実体の表（rag_entities / rag_entity_aliases / rag_entity_chunks）と検索の SQL の規則。"""

    entities: dict[str, EntityRecord] = field(default_factory=dict)
    links: list[tuple[str, str, str]] = field(default_factory=list)
    chunks: dict[str, RetrievedChunk] = field(default_factory=dict)
    # 文書 ID → 属するナレッジベース
    memberships: dict[str, set[str]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def add_document(
        self,
        document: CorpusDocument,
        *,
        knowledge_base_ids: Sequence[str] = ("kb-1",),
        options: EntityIndexOptions | None = None,
        index_entities: bool = True,
    ) -> EntityIndex:
        self.memberships[document.document_id] = set(knowledge_base_ids)
        for chunk in document.chunks:
            item = retrieved(document, chunk)
            self.chunks[item.chunk_id] = item
        if not index_entities:
            return EntityIndex()
        index = build_entity_index(
            document_id=document.document_id,
            chunks=document.chunks,
            chunk_set_id=CHUNK_SET_ID,
            document_title=document.chunks[0].text if document.chunks else "",
            options=options,
        )
        for entity in index.entities:
            # chunk_set ごとの実体の ID を、文書ごとに分ける（テストでは chunk_set の ID が同じた
            # め）。
            self.entities[f"{document.document_id}|{entity.entity_id}"] = entity
        self.links.extend(
            (f"{document.document_id}|{link.entity_id}", link.chunk_id, link.chunk_role)
            for link in index.links
        )
        return index

    def chunk_ids(self, document_id: str) -> list[str]:
        return [
            chunk_id for chunk_id, chunk in self.chunks.items() if chunk.document_id == document_id
        ]

    def _in_scope(self, chunk_id: str, filters: dict[str, str]) -> bool:
        chunk = self.chunks.get(chunk_id)
        if chunk is None:
            return False
        wanted = {value for value in filters.get("knowledge_base_id", "").split(",") if value}
        return not wanted or bool(self.memberships.get(chunk.document_id, set()) & wanted)

    def _aliases(self, entity_id: str, filters: dict[str, str] | None = None) -> list[str]:
        """実体の別名。``filters`` を渡すと、拡張に使える別名だけ（``_usable_alias_sql``）。"""
        return [
            alias.alias_key
            for alias in self.entities[entity_id].aliases
            if filters is None or self._usable_alias(entity_id, alias, filters)
        ]

    def _usable_alias(self, entity_id: str, alias: EntityAlias, filters: dict[str, str]) -> bool:
        """ラベルの別名は、別の文書の実体がラベル以外の種類で同じ別名を持つときだけ使う（#1393）。"""
        if alias.alias_kind != ALIAS_KIND_LABEL:
            return True
        document_id = _document_of(entity_id)
        return any(
            _document_of(other_id) != document_id
            and any(
                other.alias_key == alias.alias_key and other.alias_kind != ALIAS_KIND_LABEL
                for other in entity.aliases
            )
            and any(
                link_entity == other_id and self._in_scope(chunk_id, filters)
                for link_entity, chunk_id, _ in self.links
            )
            for other_id, entity in self.entities.items()
        )

    def _definition_rows(
        self, filters: dict[str, str], entity_id: str, match_key: str
    ) -> list[EntityDefinitionRow]:
        entity = self.entities[entity_id]
        return [
            EntityDefinitionRow(
                chunk=self.chunks[chunk_id],
                match_key=match_key,
                entity_id=entity_id,
                display_name=entity.display_name,
                scope_label=entity.scope_label,
            )
            for link_entity, chunk_id, role in self.links
            if link_entity == entity_id
            and role == "definition"
            and self._in_scope(chunk_id, filters)
        ]

    async def entity_seed_aliases(
        self,
        filters: dict[str, str],
        *,
        seed_chunk_ids: Sequence[str],
        question_key: str,
        limit: int,
    ) -> list[EntitySeedRow]:
        self.calls.append("seed")
        seeds = list(dict.fromkeys(seed_chunk_ids))
        ranks: dict[str, int | None] = {}
        for entity_id, chunk_id, role in self.links:
            if (
                chunk_id in seeds
                and role in {"definition", "mention"}
                and self._in_scope(chunk_id, filters)
            ):
                rank = seeds.index(chunk_id)
                current = ranks.get(entity_id)
                ranks[entity_id] = rank if current is None else min(current, rank)
        for entity_id in self.entities:
            if any(
                len(key) >= MIN_QUESTION_ALIAS_CHARS and key in question_key
                for key in self._aliases(entity_id)
            ) and any(
                link_entity == entity_id and self._in_scope(chunk_id, filters)
                for link_entity, chunk_id, _ in self.links
            ):
                ranks.setdefault(entity_id, None)
        rows = [
            EntitySeedRow(
                entity_id=entity_id,
                alias_key=key,
                scope_label=self.entities[entity_id].scope_label,
                in_question=len(key) >= MIN_QUESTION_ALIAS_CHARS and key in question_key,
                seed_rank=rank,
            )
            for entity_id, rank in ranks.items()
            for key in self._aliases(entity_id, filters)
        ]
        return rows[:limit]

    async def entity_definition_chunks(
        self, filters: dict[str, str], *, alias_keys: Sequence[str], limit: int
    ) -> list[EntityDefinitionRow]:
        self.calls.append("definition")
        keys = set(alias_keys)
        rows = [
            row
            for entity_id in self.entities
            for key in self._aliases(entity_id, filters)
            if key in keys
            for row in self._definition_rows(filters, entity_id, key)
        ]
        return sorted(rows, key=lambda row: (row.chunk.document_id, row.chunk.chunk_id))[:limit]

    async def entity_attribute_definition_chunks(
        self, filters: dict[str, str], *, source_chunk_ids: Sequence[str], limit: int
    ) -> list[EntityDefinitionRow]:
        self.calls.append("hop")
        rows: list[EntityDefinitionRow] = []
        for source in dict.fromkeys(source_chunk_ids):
            for src_entity, chunk_id, role in self.links:
                if chunk_id != source or role != "attribute":
                    continue
                for key in self._aliases(src_entity):
                    for entity_id in self.entities:
                        if entity_id == src_entity or key not in self._aliases(entity_id, filters):
                            continue
                        rows.extend(
                            EntityDefinitionRow(
                                chunk=row.chunk,
                                match_key=key,
                                entity_id=row.entity_id,
                                display_name=row.display_name,
                                scope_label=row.scope_label,
                                via_chunk_id=source,
                                via_scope_label=self.entities[src_entity].scope_label,
                            )
                            for row in self._definition_rows(filters, entity_id, key)
                        )
        return rows[:limit]


def _document_of(entity_id: str) -> str:
    """store の実体の ID（``文書 ID|実体の ID``）の文書 ID。"""
    return entity_id.split("|", 1)[0]


def corpus_store(**kwargs: object) -> InMemoryEntityStore:
    store = InMemoryEntityStore()
    for document in multi_hop_corpus():
        store.add_document(document, **kwargs)  # type: ignore[arg-type]
    return store
