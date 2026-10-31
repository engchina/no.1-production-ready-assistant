"""関係情報の表を回答の検索で読む SQL（#2055）。

表（``rag_graph_entities`` / ``rag_graph_entity_chunks``。#621・#2051）は
取込の「文書構造索引の構築」（``app.rag.graph_index``）が作る。
entity の ``knowledge_base_id`` は取込時の所属なので、検索の範囲には使わない。
どの SQL も entity に対応する chunk と文書を、検索と同じ
見え方の条件（``_oracle_retrieval_where``。tenant・ナレッジベースの範囲・利用者の権限・有効な
chunk_set・旧版の扱い）で join し、範囲の外の chunk を返さない（名前の一致の候補も、範囲の中に
chunk を持つ entity だけ）。entity と chunk の対応は FK でつながっていない（chunk_set の GC で
chunk が消えても対応は残る）が、chunk との join で消えた chunk は返らない。

規則（足す順・上限）は ``app.rag.graph_expansion`` にある。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.clients.oracle import (
    OracleClient,
    _oracle_in_predicate,
    _oracle_retrieval_where,
    _render_sql,
    _retrieved_chunk_from_row,
)
from app.rag.graph_expansion import (
    GRAPH_SECTION_ENTITY_TYPES,
    GraphChunkRow,
    GraphEntityRow,
)

# 1 回の SQL の IN の値の数の上限（Oracle の IN の上限 1000 より小さく）。
_MAX_IN_VALUES = 500
_ENTITY_TYPES_SQL = ", ".join(f"'{value}'" for value in GRAPH_SECTION_ENTITY_TYPES)
_CHUNK_COUNT_SQL = """(
                    SELECT COUNT(*) FROM rag_graph_entity_chunks cnt_ec
                    WHERE cnt_ec.entity_id = e.entity_id
                )"""


class OracleGraphStore:
    """関係情報の表の読み取り（``OracleClient`` の接続を使う）。"""

    def __init__(self, oracle: OracleClient | None = None) -> None:
        self._oracle = oracle or OracleClient()

    async def graph_seed_entities(
        self, filters: dict[str, str], *, seed_chunk_ids: Sequence[str]
    ) -> list[GraphEntityRow]:
        """起点の chunk が対応する章節・表 / 図の entity（起点の chunk ごと）。"""
        seeds = list(dict.fromkeys(seed_chunk_ids))[:_MAX_IN_VALUES]
        if not seeds:
            return []
        where_sql, binds = _oracle_retrieval_where(filters)
        seed_in_sql, seed_binds = _oracle_in_predicate("src.chunk_id", "graph_seed", seeds)
        binds.update(seed_binds)
        rows = await self._oracle._fetch_all(
            _render_sql(
                f"""
            SELECT
                e.entity_id,
                e.canonical_name,
                e.entity_type,
                src.chunk_id AS seed_chunk_id,
                {_CHUNK_COUNT_SQL} AS chunk_count
            FROM rag_graph_entity_chunks src
            JOIN rag_graph_entities e ON e.entity_id = src.entity_id
            JOIN rag_chunks c ON c.chunk_id = src.chunk_id
            JOIN rag_documents d ON d.document_id = c.document_id
            WHERE {{where_sql}}
              AND {seed_in_sql}
              AND e.entity_type IN ({_ENTITY_TYPES_SQL})
            ORDER BY src.chunk_id, e.entity_id
            """,
                where_sql=where_sql,
            ),
            binds,
        )
        return [_entity_row(row) for row in rows]

    async def graph_name_entities(
        self, filters: dict[str, str], *, limit: int
    ) -> list[GraphEntityRow]:
        """範囲の中に chunk を持つ章節・表 / 図の entity（名前の一致の候補。照合は Python）。"""
        where_sql, binds = _oracle_retrieval_where(filters)
        binds["graph_name_limit"] = max(1, int(limit))
        rows = await self._oracle._fetch_all(
            _render_sql(
                f"""
            SELECT * FROM (
                SELECT
                    e.entity_id,
                    e.canonical_name,
                    e.entity_type,
                    {_CHUNK_COUNT_SQL} AS chunk_count
                FROM rag_graph_entities e
                WHERE e.entity_type IN ({_ENTITY_TYPES_SQL})
                  AND EXISTS (
                      SELECT 1
                      FROM rag_graph_entity_chunks ec
                      JOIN rag_chunks c ON c.chunk_id = ec.chunk_id
                      JOIN rag_documents d ON d.document_id = c.document_id
                      WHERE ec.entity_id = e.entity_id
                        AND {{where_sql}}
                  )
                ORDER BY e.entity_id
            ) WHERE ROWNUM <= :graph_name_limit
            """,
                where_sql=where_sql,
            ),
            binds,
        )
        return [_entity_row(row) for row in rows]

    async def graph_entity_chunks(
        self, filters: dict[str, str], *, entity_ids: Sequence[str], per_entity: int
    ) -> list[GraphChunkRow]:
        """entity に対応する chunk（entity ごとに章節の中の順で ``per_entity`` 件まで）。"""
        ids = list(dict.fromkeys(entity_ids))[:_MAX_IN_VALUES]
        if not ids:
            return []
        where_sql, binds = _oracle_retrieval_where(filters)
        entity_in_sql, entity_binds = _oracle_in_predicate("ec.entity_id", "graph_entity", ids)
        binds.update(entity_binds)
        binds["graph_per_entity"] = max(1, int(per_entity))
        rows = await self._oracle._fetch_all(
            _render_sql(
                f"""
            SELECT * FROM (
                SELECT
                    c.document_id,
                    c.chunk_id,
                    c.chunk_text,
                    c.metadata_json,
                    c.chunk_index,
                    c.chunk_set_id,
                    d.file_name,
                    d.category_name,
                    d.superseded_by_document_id,
                    0 AS score,
                    ec.entity_id AS graph_entity_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY ec.entity_id ORDER BY c.chunk_index, c.chunk_id
                    ) AS graph_rank
                FROM rag_graph_entity_chunks ec
                JOIN rag_chunks c ON c.chunk_id = ec.chunk_id
                JOIN rag_documents d ON d.document_id = c.document_id
                WHERE {{where_sql}}
                  AND {entity_in_sql}
            ) WHERE graph_rank <= :graph_per_entity
            ORDER BY graph_entity_id, graph_rank
            """,
                where_sql=where_sql,
            ),
            binds,
        )
        return [
            GraphChunkRow(
                entity_id=str(row["graph_entity_id"]), chunk=_retrieved_chunk_from_row(row)
            )
            for row in rows
        ]


def _entity_row(row: Mapping[str, object]) -> GraphEntityRow:
    seed = row.get("seed_chunk_id")
    return GraphEntityRow(
        entity_id=str(row["entity_id"]),
        canonical_name=str(row.get("canonical_name") or ""),
        entity_type=str(row.get("entity_type") or ""),
        chunk_count=_int(row.get("chunk_count")),
        seed_chunk_id=str(seed) if seed else None,
    )


def _int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


__all__ = ["OracleGraphStore"]
