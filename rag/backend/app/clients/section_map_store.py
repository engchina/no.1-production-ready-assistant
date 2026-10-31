"""章節と chunk の対応（#1438）の Oracle の表と SQL。

- ``rag_chunk_set_sections``: chunk_set ごとの章節（文書の章節の正本の写しと、根の章節
  「文書全体」）。階層・親・ページの範囲・chunk の数・文字数を持ち、MCP の ``rag_outline`` が
  そのまま返す。文書の削除
  で消える（``ON DELETE CASCADE``）。chunk_set には FK を張らない（実体の層と同じ。取込は chunk を
  保存した後で chunk_set の行を作る経路がある）。chunk_set の GC で残った行は、次の作り直し（同じ
  文書）で消す。読む側は有効な chunk_set だけを見る。
- ``rag_section_chunks``: 章節に入る chunk（章節の中の順・所有の章節か）。chunk の削除と章節の行の
  削除で消える。

対応は文書ごとに作り直す（``replace_document_section_maps``）。chunk・embedding・ベクトル索引は変え
ない。本文を読む SQL は chunk と文書を検索と同じ見え方の条件（``_oracle_retrieval_where``）で join
し、範囲の外の chunk を返さない。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from rag_parser_core.extraction import is_derived_content, strip_formatting_marks

from app.clients.oracle import (
    OracleClient,
    OracleConnectionProtocol,
    _execute,
    _executemany,
    _fetch_one,
    _int_value,
    _metadata_from_json,
    _optional_int,
    _optional_str,
    _oracle_in_predicate,
    _oracle_retrieval_where,
    _render_sql,
    _retrieved_chunk_from_row,
)
from app.rag.element_locator import chunk_element_ids
from app.rag.section_chunk_map import MapChunk, SectionChunkMap, SectionNode
from app.schemas.search import INCLUDE_SUPERSEDED_FILTER_KEY, RetrievedChunk

CHUNK_SET_SECTIONS_TABLE = "rag_chunk_set_sections"
SECTION_CHUNKS_TABLE = "rag_section_chunks"
# 章節の名前の列の長さ（章節の名前は 200 文字まで。根の章節は文書名）。
_TITLE_MAX_CHARS = 400
# 見出しの照合に読む本文の先頭の文字数。
_HEAD_CHARS = 300
_MAX_IN_VALUES = 500

# 目録の書き出しで 1 回に読む章節の数（bind の数の上限より十分小さく。#2252）。
_SECTION_FIRST_CHUNK_BATCH = 500
_CHUNK_COLUMNS = """
                c.document_id,
                c.chunk_id,
                c.chunk_text,
                c.metadata_json,
                c.chunk_index,
                c.chunk_set_id,
                d.file_name,
                d.category_name,
                d.superseded_by_document_id,
                0 AS score"""

_SECTION_COLUMN_NAMES = (
    "chunk_set_id",
    "section_id",
    "title",
    "section_level",
    "section_ordinal",
    "parent_section_id",
    "page_start",
    "page_end",
    "chunk_count",
    "char_count",
    "first_chunk_index",
    "last_chunk_index",
)
_SECTION_COLUMNS = ",\n".join(f"                s.{column}" for column in _SECTION_COLUMN_NAMES)


def oracle_section_map_schema_sql() -> str:
    """章節と chunk の対応の表の DDL（#1438）。"""
    return f"""
CREATE TABLE {CHUNK_SET_SECTIONS_TABLE} (
    chunk_set_id       VARCHAR2(64) NOT NULL,
    section_id         VARCHAR2(64) NOT NULL,
    document_id        VARCHAR2(64) NOT NULL,
    section_ordinal    NUMBER(10) NOT NULL,
    section_level      NUMBER(2) NOT NULL,
    parent_section_id  VARCHAR2(64),
    title              VARCHAR2({_TITLE_MAX_CHARS} CHAR) NOT NULL,
    page_start         NUMBER(10),
    page_end           NUMBER(10),
    chunk_count        NUMBER(10) DEFAULT 0 NOT NULL,
    char_count         NUMBER(19) DEFAULT 0 NOT NULL,
    first_chunk_index  NUMBER(10),
    last_chunk_index   NUMBER(10),
    created_at         TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT {CHUNK_SET_SECTIONS_TABLE}_pk PRIMARY KEY (chunk_set_id, section_id),
    CONSTRAINT {CHUNK_SET_SECTIONS_TABLE}_document_fk
        FOREIGN KEY (document_id) REFERENCES rag_documents (document_id) ON DELETE CASCADE
);

CREATE INDEX {CHUNK_SET_SECTIONS_TABLE}_document_idx
    ON {CHUNK_SET_SECTIONS_TABLE} (document_id);

CREATE TABLE {SECTION_CHUNKS_TABLE} (
    chunk_set_id  VARCHAR2(64) NOT NULL,
    section_id    VARCHAR2(64) NOT NULL,
    chunk_id      VARCHAR2(128) NOT NULL,
    document_id   VARCHAR2(64) NOT NULL,
    chunk_order   NUMBER(10) NOT NULL,
    chunk_index   NUMBER(10) NOT NULL,
    is_owner      NUMBER(1) DEFAULT 0 NOT NULL,
    CONSTRAINT {SECTION_CHUNKS_TABLE}_pk PRIMARY KEY (chunk_set_id, section_id, chunk_id),
    CONSTRAINT {SECTION_CHUNKS_TABLE}_section_fk
        FOREIGN KEY (chunk_set_id, section_id)
        REFERENCES {CHUNK_SET_SECTIONS_TABLE} (chunk_set_id, section_id) ON DELETE CASCADE,
    CONSTRAINT {SECTION_CHUNKS_TABLE}_chunk_fk
        FOREIGN KEY (chunk_id) REFERENCES rag_chunks (chunk_id) ON DELETE CASCADE,
    CONSTRAINT {SECTION_CHUNKS_TABLE}_owner_ck CHECK (is_owner IN (0, 1))
);

CREATE INDEX {SECTION_CHUNKS_TABLE}_chunk_idx
    ON {SECTION_CHUNKS_TABLE} (chunk_id, is_owner);

CREATE INDEX {SECTION_CHUNKS_TABLE}_document_idx
    ON {SECTION_CHUNKS_TABLE} (document_id);
""".strip()


@dataclass(frozen=True)
class ChunkSetChunks:
    """対応を作る chunk_set の chunk（解析の結果の ID は要素の照合に使う）。"""

    chunk_set_id: str
    extraction_recipe_id: str | None
    chunks: list[MapChunk]


@dataclass(frozen=True)
class ReadableChunkSet:
    """利用者が読める文書の chunk_set（検索と同じ見え方の条件で確かめたもの）。"""

    document_id: str
    chunk_set_id: str
    file_name: str | None
    superseded: bool


@dataclass(frozen=True)
class SectionChunkRow:
    """章節の中の 1 つの chunk（章節の中の順と本文）。"""

    chunk_order: int
    chunk: RetrievedChunk


def _section_node(row: dict[str, object]) -> SectionNode:
    return SectionNode(
        section_id=str(row["section_id"]),
        # 線を除く前に作った対応の表の名前も、作り直しを待たずに線を除いて読む（#3434）。
        title=strip_formatting_marks(str(row.get("title") or "")),
        level=_int_value(row.get("section_level")),
        ordinal=_int_value(row.get("section_ordinal")),
        parent_section_id=_optional_str(row.get("parent_section_id")),
        page_start=_optional_int(row.get("page_start")),
        page_end=_optional_int(row.get("page_end")),
        chunk_count=_int_value(row.get("chunk_count")),
        chars=_int_value(row.get("char_count")),
        first_chunk_index=_optional_int(row.get("first_chunk_index")),
        last_chunk_index=_optional_int(row.get("last_chunk_index")),
    )


def _page(metadata: Mapping[str, object], key: str) -> int | None:
    value = metadata.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _section_path(value: object) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(" > ") if part.strip())
    return ()


def map_chunk_from_row(row: dict[str, object]) -> MapChunk | None:
    """対応を作る chunk の行（本文でない派生の chunk は None）。"""
    metadata = _metadata_from_json(row.get("metadata_json"))
    # 派生の chunk（RAPTOR・章節の要約、項目抽出の値）は引用できる原文ではないので、章節の本文に
    # 入れない。印の無い #2590 より前の chunk も content_kind で除く。
    if is_derived_content(metadata.get("content_kind"), metadata):
        return None
    page_start = _page(metadata, "page_start") or _page(metadata, "page_number")
    page_end = _page(metadata, "page_end") or page_start
    return MapChunk(
        chunk_id=str(row["chunk_id"]),
        chunk_index=_int_value(row.get("chunk_index")),
        page_start=page_start,
        page_end=page_end,
        element_ids=tuple(chunk_element_ids(metadata)),
        section_path=_section_path(metadata.get("section_path")),
        head=str(row.get("head") or ""),
        chars=_int_value(row.get("chars")),
    )


class SectionMapStore:
    """章節と chunk の対応の読み書き（``OracleClient`` の接続と transaction を使う）。"""

    def __init__(self, oracle: OracleClient | None = None) -> None:
        self._oracle = oracle or OracleClient()

    # ---- 作り直し（システムの処理。利用者の見え方の条件は使わない） ----

    async def document_chunk_sets(self, document_id: str) -> list[ChunkSetChunks]:
        """文書の有効な chunk_set（INDEXED）ごとの chunk（読み順）。"""
        rows = await self._oracle._fetch_all(
            """
            SELECT
                c.chunk_set_id,
                cs.extraction_recipe_id,
                c.chunk_id,
                c.chunk_index,
                c.metadata_json,
                DBMS_LOB.SUBSTR(c.chunk_text, :head_chars, 1) AS head,
                DBMS_LOB.GETLENGTH(c.chunk_text) AS chars
            FROM rag_chunks c
            JOIN rag_chunk_sets cs ON cs.chunk_set_id = c.chunk_set_id
            WHERE c.document_id = :document_id
              AND cs.document_id = :document_id
              AND cs.is_active = 1
              AND cs.status = 'INDEXED'
            ORDER BY c.chunk_set_id, c.chunk_index, c.chunk_id
            """,
            {"document_id": document_id, "head_chars": _HEAD_CHARS},
        )
        grouped: dict[str, ChunkSetChunks] = {}
        for row in rows:
            chunk_set_id = str(row["chunk_set_id"])
            entry = grouped.get(chunk_set_id)
            if entry is None:
                entry = ChunkSetChunks(
                    chunk_set_id=chunk_set_id,
                    extraction_recipe_id=_optional_str(row.get("extraction_recipe_id")),
                    chunks=[],
                )
                grouped[chunk_set_id] = entry
            chunk = map_chunk_from_row(row)
            if chunk is not None:
                entry.chunks.append(chunk)
        return list(grouped.values())

    async def replace_document_section_maps(
        self, document_id: str, maps: Sequence[SectionChunkMap]
    ) -> None:
        """文書の対応を置き換える（前の対応は消してから入れる。chunk は変えない）。"""

        def operation(connection: OracleConnectionProtocol) -> None:
            # 同じ文書の作り直し（章節の保存と規則の変更の後の作り直しなど）を直列にする。
            if (
                _fetch_one(
                    connection,
                    "SELECT document_id FROM rag_documents WHERE document_id = :document_id "
                    "FOR UPDATE",
                    {"document_id": document_id},
                )
                is None
            ):
                return
            _execute(
                connection,
                f"DELETE FROM {SECTION_CHUNKS_TABLE} WHERE document_id = :document_id",
                {"document_id": document_id},
            )
            _execute(
                connection,
                f"DELETE FROM {CHUNK_SET_SECTIONS_TABLE} WHERE document_id = :document_id",
                {"document_id": document_id},
            )
            sections = [
                {
                    "chunk_set_id": item.chunk_set_id,
                    "section_id": node.section_id,
                    "document_id": document_id,
                    "section_ordinal": node.ordinal,
                    "section_level": node.level,
                    "parent_section_id": node.parent_section_id,
                    "title": (node.title or "")[:_TITLE_MAX_CHARS] or " ",
                    "page_start": node.page_start,
                    "page_end": node.page_end,
                    "chunk_count": node.chunk_count,
                    "char_count": node.chars,
                    "first_chunk_index": node.first_chunk_index,
                    "last_chunk_index": node.last_chunk_index,
                }
                for item in maps
                for node in item.sections
            ]
            if sections:
                _executemany(
                    connection,
                    f"""
                    INSERT INTO {CHUNK_SET_SECTIONS_TABLE} (
                        chunk_set_id, section_id, document_id, section_ordinal, section_level,
                        parent_section_id, title, page_start, page_end, chunk_count, char_count,
                        first_chunk_index, last_chunk_index
                    ) VALUES (
                        :chunk_set_id, :section_id, :document_id, :section_ordinal,
                        :section_level, :parent_section_id, :title, :page_start, :page_end,
                        :chunk_count, :char_count, :first_chunk_index, :last_chunk_index
                    )
                    """,
                    sections,
                )
            links = [
                {
                    "chunk_set_id": item.chunk_set_id,
                    "section_id": link.section_id,
                    "chunk_id": link.chunk_id,
                    "document_id": document_id,
                    "chunk_order": link.chunk_order,
                    "chunk_index": link.chunk_index,
                    "is_owner": 1 if link.is_owner else 0,
                }
                for item in maps
                for link in item.links
            ]
            if links:
                _executemany(
                    connection,
                    f"""
                    INSERT INTO {SECTION_CHUNKS_TABLE} (
                        chunk_set_id, section_id, chunk_id, document_id, chunk_order,
                        chunk_index, is_owner
                    ) VALUES (
                        :chunk_set_id, :section_id, :chunk_id, :document_id, :chunk_order,
                        :chunk_index, :is_owner
                    )
                    """,
                    links,
                )

        await self._oracle._run_transaction(operation)

    async def indexed_document_ids(self) -> list[str]:
        """有効な chunk_set を持つ文書の id（章節の抽出規則を変えたときの作り直しの対象）。"""
        rows = await self._oracle._fetch_all(
            """
            SELECT DISTINCT cs.document_id
            FROM rag_chunk_sets cs
            WHERE cs.is_active = 1 AND cs.status = 'INDEXED'
            ORDER BY cs.document_id
            """,
            {},
        )
        return [str(row["document_id"]) for row in rows if row.get("document_id")]

    # ---- 読み取り（利用者の見え方の条件で確かめる） ----

    async def chunk_set_ids_with_prefix(self, document_id: str, prefix: str) -> list[str]:
        """文書の chunk_set のうち、id が ``prefix`` で始まるもの（多くて 2 件。#2499）。

        続きの位置（next_cursor）は chunk_set の id の先頭だけを持つ。読めるかは
        ``readable_chunk_set`` が決める。
        """
        rows = await self._oracle._fetch_all(
            """
            SELECT chunk_set_id
            FROM rag_chunk_sets
            WHERE document_id = :document_id
              AND SUBSTR(chunk_set_id, 1, :prefix_chars) = :prefix
            ORDER BY chunk_set_id
            FETCH FIRST 2 ROWS ONLY
            """,
            {"document_id": document_id, "prefix": prefix, "prefix_chars": len(prefix)},
        )
        return [str(row["chunk_set_id"]) for row in rows]

    async def readable_chunk_set(
        self, document_id: str, chunk_set_id: str | None = None
    ) -> ReadableChunkSet | None:
        """利用者が読める文書の chunk_set（``chunk_set_id`` が無ければ、有効な chunk_set のうち
        レシピの番号が最も小さいもの）。読めなければ None。"""
        # ID で読むので有効期間では絞らない（検索で選んだ根拠を今日の日付で読めなくしない。#3268）。
        where_sql, binds = _oracle_retrieval_where(
            {INCLUDE_SUPERSEDED_FILTER_KEY: "true"}, effective_period=False
        )
        chunk_set_sql = (
            ":read_chunk_set_id"
            if chunk_set_id is not None
            else """(
                SELECT primary_cs.chunk_set_id
                FROM rag_chunk_sets primary_cs
                JOIN rag_document_recipes primary_r ON primary_r.recipe_id = primary_cs.recipe_id
                WHERE primary_cs.document_id = :read_document_id
                  AND primary_cs.is_active = 1
                  AND primary_cs.status = 'INDEXED'
                ORDER BY primary_r.slot_no, primary_cs.chunk_set_id
                FETCH FIRST 1 ROWS ONLY
            )"""
        )
        query_binds: dict[str, object] = {**binds, "read_document_id": document_id}
        if chunk_set_id is not None:
            query_binds["read_chunk_set_id"] = chunk_set_id
        row = await self._oracle._fetch_one(
            _render_sql(
                """
            SELECT c.chunk_set_id, d.file_name, d.superseded_by_document_id
            FROM rag_chunks c
            JOIN rag_documents d ON d.document_id = c.document_id
            WHERE {where_sql}
              AND c.document_id = :read_document_id
              AND c.chunk_set_id = {chunk_set_sql}
            FETCH FIRST 1 ROWS ONLY
            """,
                where_sql=where_sql,
                chunk_set_sql=chunk_set_sql,
            ),
            query_binds,
        )
        if row is None or not row.get("chunk_set_id"):
            return None
        return ReadableChunkSet(
            document_id=document_id,
            chunk_set_id=str(row["chunk_set_id"]),
            file_name=_optional_str(row.get("file_name")),
            superseded=_optional_str(row.get("superseded_by_document_id")) is not None,
        )

    async def chunk_set_sections(self, document_id: str, chunk_set_id: str) -> list[SectionNode]:
        """chunk_set の章節（根の章節を先頭に、文書の中の並び順）。

        読める chunk_set かは呼び出し側が ``readable_chunk_set`` で確かめる。
        """
        rows = await self._oracle._fetch_all(
            _render_sql(
                f"""
            SELECT {{columns}}
            FROM {CHUNK_SET_SECTIONS_TABLE} s
            WHERE s.chunk_set_id = :chunk_set_id
              AND s.document_id = :document_id
            ORDER BY s.section_ordinal
            """,
                columns=_SECTION_COLUMNS,
            ),
            {"chunk_set_id": chunk_set_id, "document_id": document_id},
        )
        return [_section_node(row) for row in rows]

    async def readable_owner_section(
        self, document_id: str, chunk_id: str
    ) -> tuple[str, str] | None:
        """利用者が読める chunk の (chunk_set_id, 所有の章節の id)。無ければ None。"""
        # ID で読むので有効期間では絞らない（検索で選んだ根拠を今日の日付で読めなくしない。#3268）。
        where_sql, binds = _oracle_retrieval_where(
            {INCLUDE_SUPERSEDED_FILTER_KEY: "true"}, effective_period=False
        )
        row = await self._oracle._fetch_one(
            _render_sql(
                f"""
            SELECT sc.chunk_set_id, sc.section_id
            FROM {SECTION_CHUNKS_TABLE} sc
            JOIN rag_chunks c ON c.chunk_id = sc.chunk_id
            JOIN rag_documents d ON d.document_id = c.document_id
            WHERE {{where_sql}}
              AND sc.chunk_id = :read_chunk_id
              AND sc.document_id = :read_document_id
              AND c.document_id = :read_document_id
              AND sc.is_owner = 1
            FETCH FIRST 1 ROWS ONLY
            """,
                where_sql=where_sql,
            ),
            {**binds, "read_chunk_id": chunk_id, "read_document_id": document_id},
        )
        if row is None:
            return None
        return str(row["chunk_set_id"]), str(row["section_id"])

    async def section_chunks(
        self,
        document_id: str,
        *,
        chunk_set_id: str,
        section_id: str,
        from_order: int,
        limit: int,
        filters: Mapping[str, str] | None = None,
    ) -> list[SectionChunkRow]:
        """章節の chunk を、章節の中の順に ``from_order`` から読む（検索と同じ見え方の条件）。

        ``filters`` は回答の検索の条件（ナレッジベース・ページの範囲など。#1436）。渡すと、章節の
        chunk のうちその条件に入るものだけを読む（文書は起点の chunk の文書なので、旧版の扱いは
        問わない）。有効期間は ``filters`` を渡したときだけ、その基準日で絞る（MCP の
        ``rag_read_section`` のように ``filters`` の無い ID の読み取りは絞らない。#3268）。
        """
        where_sql, binds = _oracle_retrieval_where(
            {**(filters or {}), INCLUDE_SUPERSEDED_FILTER_KEY: "true"},
            effective_period=filters is not None,
        )
        rows = await self._oracle._fetch_all(
            _render_sql(
                f"""
            SELECT sc.chunk_order, {{chunk_columns}}
            FROM {SECTION_CHUNKS_TABLE} sc
            JOIN rag_chunks c ON c.chunk_id = sc.chunk_id
            JOIN rag_documents d ON d.document_id = c.document_id
            WHERE {{where_sql}}
              AND sc.chunk_set_id = :read_chunk_set_id
              AND sc.section_id = :read_section_id
              AND sc.document_id = :read_document_id
              AND c.document_id = :read_document_id
              AND sc.chunk_order >= :read_from_order
            ORDER BY sc.chunk_order
            FETCH FIRST :read_limit ROWS ONLY
            """,
                where_sql=where_sql,
                chunk_columns=_CHUNK_COLUMNS,
            ),
            {
                **binds,
                "read_chunk_set_id": chunk_set_id,
                "read_section_id": section_id,
                "read_document_id": document_id,
                "read_from_order": max(0, int(from_order)),
                "read_limit": max(1, int(limit)),
            },
        )
        return [
            SectionChunkRow(
                chunk_order=_int_value(row.get("chunk_order")),
                chunk=_retrieved_chunk_from_row(row),
            )
            for row in rows
        ]

    async def section_first_chunks(
        self, document_id: str, *, chunk_set_id: str, section_ids: Sequence[str]
    ) -> dict[str, RetrievedChunk]:
        """章節ごとの最初の chunk（章節の中の順 0。検索と同じ見え方の条件。#2252）。

        目録の本文の書き出しに使う。見えない chunk の章節は含めない。
        """
        found: dict[str, RetrievedChunk] = {}
        ids = list(dict.fromkeys(section_ids))
        # ID で読むので有効期間では絞らない（検索で選んだ根拠を今日の日付で読めなくしない。#3268）。
        where_sql, binds = _oracle_retrieval_where(
            {INCLUDE_SUPERSEDED_FILTER_KEY: "true"}, effective_period=False
        )
        for start in range(0, len(ids), _SECTION_FIRST_CHUNK_BATCH):
            batch = ids[start : start + _SECTION_FIRST_CHUNK_BATCH]
            names = {f"lead_section_{index}": section_id for index, section_id in enumerate(batch)}
            rows = await self._oracle._fetch_all(
                _render_sql(
                    f"""
                SELECT sc.section_id AS lead_section_id, {{chunk_columns}}
                FROM {SECTION_CHUNKS_TABLE} sc
                JOIN rag_chunks c ON c.chunk_id = sc.chunk_id
                JOIN rag_documents d ON d.document_id = c.document_id
                WHERE {{where_sql}}
                  AND sc.chunk_set_id = :lead_chunk_set_id
                  AND sc.document_id = :lead_document_id
                  AND c.document_id = :lead_document_id
                  AND sc.chunk_order = 0
                  AND sc.section_id IN ({{section_binds}})
                """,
                    where_sql=where_sql,
                    chunk_columns=_CHUNK_COLUMNS,
                    section_binds=", ".join(f":{name}" for name in names),
                ),
                {
                    **binds,
                    **names,
                    "lead_chunk_set_id": chunk_set_id,
                    "lead_document_id": document_id,
                },
            )
            for row in rows:
                section_id = str(row.get("lead_section_id") or "")
                if section_id and section_id not in found:
                    found[section_id] = _retrieved_chunk_from_row(row)
        return found

    async def owner_sections(
        self, chunk_ids: Sequence[str]
    ) -> dict[str, tuple[SectionNode, SectionNode | None]]:
        """chunk ごとの所有の章節と、その親の章節（根拠の章節を示すため。無ければ含めない）。

        根拠は検索と同じ見え方の条件で読んだ chunk なので、ここでは見え方を確かめ直さない（章節の
        名前・ページだけを返し、本文は返さない）。
        """
        ids = list(dict.fromkeys(item for item in chunk_ids if item))[:_MAX_IN_VALUES]
        if not ids:
            return {}
        in_sql, binds = _oracle_in_predicate("sc.chunk_id", "owner_chunk", ids)
        parent_select = ",\n".join(
            f"                p.{column} AS p_{column}" for column in _SECTION_COLUMN_NAMES
        )
        rows = await self._oracle._fetch_all(
            _render_sql(
                f"""
            SELECT sc.chunk_id, {{columns}}, {{parent_select}}
            FROM {SECTION_CHUNKS_TABLE} sc
            JOIN {CHUNK_SET_SECTIONS_TABLE} s
              ON s.chunk_set_id = sc.chunk_set_id AND s.section_id = sc.section_id
            LEFT JOIN {CHUNK_SET_SECTIONS_TABLE} p
              ON p.chunk_set_id = s.chunk_set_id AND p.section_id = s.parent_section_id
            WHERE {{in_sql}}
              AND sc.is_owner = 1
            """,
                columns=_SECTION_COLUMNS,
                parent_select=parent_select,
                in_sql=in_sql,
            ),
            binds,
        )
        result: dict[str, tuple[SectionNode, SectionNode | None]] = {}
        for row in rows:
            parent_row = {column: row.get(f"p_{column}") for column in _SECTION_COLUMN_NAMES}
            parent = _section_node(parent_row) if parent_row.get("section_id") else None
            result[str(row["chunk_id"])] = (_section_node(row), parent)
        return result
