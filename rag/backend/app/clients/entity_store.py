"""実体の層（#1362）の Oracle の表と SQL。

- ``rag_entities``: 実体（chunk_set ごと。正規化した名前・表示名・種類・会社の名前）。文書の削除
  で消える（``ON DELETE CASCADE``）。chunk_set には FK を張らない（取込は chunk を保存した後で
  chunk_set の行を作る経路がある）。chunk_set の GC では chunk の削除で関連が消え、関連の無い実体は
  次の実体の保存（同じ文書）で消す。
- ``rag_entity_aliases``: 実体の別名（正規化した形。名寄せの join の key）。実体の削除で消える。
- ``rag_entity_chunks``: 実体と chunk の関連（定義する行 / 属性 / 言及）。実体と chunk の削除で消
  える（再索引で chunk を作り直すと、その chunk の関連も消える）。

既存の関係情報（``rag_graph_*``。#621）は文書と章・節の見出しのつながりを表示するための表で、ナレ
ッジベースの ID を取込の時点で持ち（ナレッジベースの出し入れで変わらない）、chunk の削除と FK でつ
ながっていない。実体の層は回答の検索の join に使うため、ナレッジベースの範囲を検索のときに chunk
と文書の条件（``_oracle_retrieval_where``）で決め、削除を FK で連動させる別の表にした。

検索の SQL は ``app.rag.entity_expansion.EntityExpansionStore`` の 3 つ（起点の別名・名寄せ・1 段）
。どれも chunk と文書を検索と同じ見え方の条件で join し、範囲の外の chunk を返さない（途中の chunk
も同じ）。

本文の見出し・列挙のラベル（別名の種類 ``label``。「原因 A」「手順 1」）は、別の文書が同じ別名をラ
ベル以外の種類（表の値・定義の形・ID や名前）で持ち、その文書の chunk が同じ見え方の条件の中にある
ときだけ使う（``_usable_alias_sql``。#1393）。1 つの文書の中だけで使う記号は、起点にも名寄せ・1 段の
経路にもしない。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.clients.oracle import (
    OracleClient,
    OracleConnectionProtocol,
    _current_tenant_id_hash,
    _execute,
    _executemany,
    _oracle_in_predicate,
    _oracle_retrieval_where,
    _render_sql,
    _retrieved_chunk_from_row,
)
from app.rag.entity_expansion import (
    MIN_QUESTION_ALIAS_CHARS,
    EntityDefinitionRow,
    EntitySeedRow,
)
from app.rag.entity_index import ALIAS_KIND_LABEL, EntityIndex

ENTITIES_TABLE = "rag_entities"
ENTITY_ALIASES_TABLE = "rag_entity_aliases"
ENTITY_CHUNKS_TABLE = "rag_entity_chunks"
# 質問の照合に渡す正規化した質問の長さの上限（bind の VARCHAR2 に収める）。
_QUESTION_KEY_MAX_CHARS = 1000
# 1 回の SQL の IN の値の数の上限（Oracle の IN の上限 1000 より小さく）。
_MAX_IN_VALUES = 500

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


def _usable_alias_sql(alias: str, entity: str, where_sql: str) -> str:
    """拡張に使える別名の条件（#1393）。

    ラベル（``label``。「原因 A」）の別名は、別の文書の実体が同じ別名をラベル以外の種類で持ち、その
    実体の chunk が検索と同じ見え方の条件（``where_sql``）の中にあるときだけ使う。``where_sql`` は
    ``c`` / ``d`` の別名を使うため、内側の副問い合わせでも同じ別名で chunk と文書を join する
    （外側の ``c`` / ``d`` は内側の別名で隠れる）。
    """
    return f"""(
                    {alias}.alias_kind <> '{ALIAS_KIND_LABEL}'
                    OR EXISTS (
                        SELECT 1
                        FROM {ENTITY_ALIASES_TABLE} label_a
                        JOIN {ENTITIES_TABLE} label_e ON label_e.entity_id = label_a.entity_id
                        JOIN {ENTITY_CHUNKS_TABLE} label_ec
                          ON label_ec.entity_id = label_e.entity_id
                        JOIN rag_chunks c ON c.chunk_id = label_ec.chunk_id
                        JOIN rag_documents d ON d.document_id = c.document_id
                        WHERE label_a.alias_key = {alias}.alias_key
                          AND label_a.alias_kind <> '{ALIAS_KIND_LABEL}'
                          AND label_e.document_id <> {entity}.document_id
                          AND {where_sql}
                    )
                )"""


def oracle_entity_schema_sql() -> str:
    """実体・別名・実体と chunk の関連の表の DDL（#1362）。"""
    return f"""
CREATE TABLE {ENTITIES_TABLE} (
    entity_id       VARCHAR2(64) PRIMARY KEY,
    chunk_set_id    VARCHAR2(64) NOT NULL,
    document_id     VARCHAR2(64) NOT NULL,
    tenant_id_hash  CHAR(64),
    entity_key      VARCHAR2(400 CHAR) NOT NULL,
    display_name    VARCHAR2(400 CHAR) NOT NULL,
    entity_type     VARCHAR2(16) NOT NULL,
    scope_label     VARCHAR2(200 CHAR),
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT {ENTITIES_TABLE}_document_fk
        FOREIGN KEY (document_id) REFERENCES rag_documents (document_id) ON DELETE CASCADE,
    CONSTRAINT {ENTITIES_TABLE}_type_ck
        CHECK (entity_type IN ('record', 'defined', 'value', 'term'))
);

CREATE INDEX {ENTITIES_TABLE}_chunk_set_idx
    ON {ENTITIES_TABLE} (chunk_set_id);

CREATE INDEX {ENTITIES_TABLE}_document_idx
    ON {ENTITIES_TABLE} (document_id);

CREATE TABLE {ENTITY_ALIASES_TABLE} (
    entity_id   VARCHAR2(64) NOT NULL,
    alias_key   VARCHAR2(400 CHAR) NOT NULL,
    alias_text  VARCHAR2(400 CHAR) NOT NULL,
    alias_kind  VARCHAR2(16) NOT NULL,
    CONSTRAINT {ENTITY_ALIASES_TABLE}_pk PRIMARY KEY (entity_id, alias_key),
    CONSTRAINT {ENTITY_ALIASES_TABLE}_entity_fk
        FOREIGN KEY (entity_id) REFERENCES {ENTITIES_TABLE} (entity_id) ON DELETE CASCADE
);

CREATE INDEX {ENTITY_ALIASES_TABLE}_key_idx
    ON {ENTITY_ALIASES_TABLE} (alias_key, entity_id);

CREATE TABLE {ENTITY_CHUNKS_TABLE} (
    entity_id       VARCHAR2(64) NOT NULL,
    chunk_id        VARCHAR2(128) NOT NULL,
    chunk_role      VARCHAR2(16) NOT NULL,
    attribute_name  VARCHAR2(200 CHAR),
    CONSTRAINT {ENTITY_CHUNKS_TABLE}_pk PRIMARY KEY (entity_id, chunk_id),
    CONSTRAINT {ENTITY_CHUNKS_TABLE}_entity_fk
        FOREIGN KEY (entity_id) REFERENCES {ENTITIES_TABLE} (entity_id) ON DELETE CASCADE,
    CONSTRAINT {ENTITY_CHUNKS_TABLE}_chunk_fk
        FOREIGN KEY (chunk_id) REFERENCES rag_chunks (chunk_id) ON DELETE CASCADE,
    CONSTRAINT {ENTITY_CHUNKS_TABLE}_role_ck
        CHECK (chunk_role IN ('definition', 'attribute', 'mention'))
);

CREATE INDEX {ENTITY_CHUNKS_TABLE}_chunk_idx
    ON {ENTITY_CHUNKS_TABLE} (chunk_id, chunk_role);
""".strip()


class EntityStore:
    """実体の層の読み書き（``OracleClient`` の接続と transaction を使う）。"""

    def __init__(self, oracle: OracleClient | None = None) -> None:
        self._oracle = oracle or OracleClient()

    async def replace_chunk_set_entity_index(
        self, document_id: str, chunk_set_id: str, index: EntityIndex
    ) -> None:
        """chunk_set の実体を置き換える（前の実体・別名・関連は消してから入れる）。"""

        def operation(connection: OracleConnectionProtocol) -> None:
            _delete_chunk_set_entities(connection, chunk_set_id)
            _delete_unlinked_document_entities(connection, document_id)
            if not index.entities:
                return
            tenant_id_hash = _current_tenant_id_hash()
            _executemany(
                connection,
                f"""
                INSERT INTO {ENTITIES_TABLE} (
                    entity_id, chunk_set_id, document_id, tenant_id_hash,
                    entity_key, display_name, entity_type, scope_label
                ) VALUES (
                    :entity_id, :chunk_set_id, :document_id, :tenant_id_hash,
                    :entity_key, :display_name, :entity_type, :scope_label
                )
                """,
                [
                    {
                        "entity_id": entity.entity_id,
                        "chunk_set_id": chunk_set_id,
                        "document_id": document_id,
                        "tenant_id_hash": tenant_id_hash,
                        "entity_key": entity.entity_key,
                        "display_name": entity.display_name,
                        "entity_type": entity.entity_type,
                        "scope_label": entity.scope_label,
                    }
                    for entity in index.entities
                ],
            )
            aliases = [
                {
                    "entity_id": entity.entity_id,
                    "alias_key": alias.alias_key,
                    "alias_text": alias.alias_text,
                    "alias_kind": alias.alias_kind,
                }
                for entity in index.entities
                for alias in entity.aliases
            ]
            if aliases:
                _executemany(
                    connection,
                    f"""
                    INSERT INTO {ENTITY_ALIASES_TABLE} (entity_id, alias_key, alias_text,
                    alias_kind)
                    VALUES (:entity_id, :alias_key, :alias_text, :alias_kind)
                    """,
                    aliases,
                )
            if index.links:
                _executemany(
                    connection,
                    f"""
                    INSERT INTO {ENTITY_CHUNKS_TABLE} (
                        entity_id, chunk_id, chunk_role, attribute_name
                    ) VALUES (:entity_id, :chunk_id, :chunk_role, :attribute_name)
                    """,
                    [
                        {
                            "entity_id": link.entity_id,
                            "chunk_id": link.chunk_id,
                            "chunk_role": link.chunk_role,
                            "attribute_name": link.attribute_name,
                        }
                        for link in index.links
                    ],
                )

        await self._oracle._run_transaction(operation)

    async def delete_chunk_set_entity_index(self, chunk_set_id: str) -> None:
        """chunk_set の実体を消す（レシピで実体の抽出をやめたとき）。"""
        await self._oracle._run_transaction(
            lambda connection: _delete_chunk_set_entities(connection, chunk_set_id)
        )

    async def count_entity_documents(self, knowledge_base_ids: Sequence[str]) -> int:
        """ナレッジベースの中で実体の索引を持つ文書の数（#1388）。

        検索・回答プロファイルの画面が、実体の 1 段の拡張の開閉の横に「効く文書が無い」ことを案内
        するために使う。見え方は回答の検索と同じ条件（tenant・権限・有効な chunk_set・ナレッジ
        ベース）で数える。ナレッジベースを渡さなければ 0（範囲の外を数えない）。
        """
        ids = [value for value in dict.fromkeys(knowledge_base_ids) if value.strip()]
        if not ids:
            return 0
        where_sql, binds = _oracle_retrieval_where(
            {"knowledge_base_id": ",".join(ids[:_MAX_IN_VALUES])}
        )
        rows = await self._oracle._fetch_all(
            _render_sql(
                f"""
            SELECT COUNT(DISTINCT c.document_id) AS document_count
            FROM {ENTITY_CHUNKS_TABLE} ec
            JOIN rag_chunks c ON c.chunk_id = ec.chunk_id
            JOIN rag_documents d ON d.document_id = c.document_id
            WHERE {{where_sql}}
            """,
                where_sql=where_sql,
            ),
            binds,
        )
        return _int(rows[0].get("document_count")) if rows else 0

    async def entity_seed_aliases(
        self,
        filters: dict[str, str],
        *,
        seed_chunk_ids: Sequence[str],
        question_key: str,
        limit: int,
    ) -> list[EntitySeedRow]:
        """起点の実体（上位の chunk に関連づけた実体と、質問に別名が含まれる実体）のすべての別名。

        上位の chunk からは「定義する行」と「言及」の実体を起点にする（属性の実体は 1 段の拡張でた
        どる）。質問の照合は 2 文字以上の別名だけ（1 文字の略号は質問の任意の 1 文字に一致させない）
        。どちらも検索と同じ見え方の条件の chunk を持つ実体に限る。
        """
        where_sql, binds = _oracle_retrieval_where(filters)
        seeds = list(dict.fromkeys(seed_chunk_ids))[:_MAX_IN_VALUES]
        branches: list[str] = []
        if seeds:
            seed_in_sql, seed_binds = _oracle_in_predicate("ec.chunk_id", "entity_seed", seeds)
            binds.update(seed_binds)
            rank_case = " ".join(
                f"WHEN :entity_seed_{index} THEN {index}" for index in range(len(seeds))
            )
            branches.append(
                f"""
                SELECT ec.entity_id, CASE ec.chunk_id {rank_case} END AS seed_rank
                FROM {ENTITY_CHUNKS_TABLE} ec
                JOIN rag_chunks c ON c.chunk_id = ec.chunk_id
                JOIN rag_documents d ON d.document_id = c.document_id
                WHERE {where_sql}
                  AND {seed_in_sql}
                  AND ec.chunk_role IN ('definition', 'mention')
                """
            )
        question = question_key[:_QUESTION_KEY_MAX_CHARS]
        if len(question) >= MIN_QUESTION_ALIAS_CHARS:
            binds["entity_question_key"] = question
            binds["entity_min_alias_chars"] = MIN_QUESTION_ALIAS_CHARS
            branches.append(
                f"""
                SELECT qa.entity_id, NULL AS seed_rank
                FROM {ENTITY_ALIASES_TABLE} qa
                WHERE LENGTH(qa.alias_key) >= :entity_min_alias_chars
                  AND INSTR(:entity_question_key, qa.alias_key) > 0
                  AND EXISTS (
                      SELECT 1
                      FROM {ENTITY_CHUNKS_TABLE} ec
                      JOIN rag_chunks c ON c.chunk_id = ec.chunk_id
                      JOIN rag_documents d ON d.document_id = c.document_id
                      WHERE ec.entity_id = qa.entity_id
                        AND {where_sql}
                  )
                """
            )
        if not branches:
            return []
        binds.setdefault("entity_question_key", "")
        binds["entity_seed_limit"] = max(1, int(limit))
        rows = await self._oracle._fetch_all(
            f"""
            SELECT * FROM (
                SELECT
                    seed.entity_id,
                    alias.alias_key,
                    e.scope_label,
                    MAX(
                        CASE
                            WHEN LENGTH(alias.alias_key) >= {MIN_QUESTION_ALIAS_CHARS}
                             AND INSTR(:entity_question_key, alias.alias_key) > 0
                            THEN 1 ELSE 0
                        END
                    ) AS in_question,
                    MIN(seed.seed_rank) AS seed_rank
                FROM ({" UNION ALL ".join(branches)}) seed
                JOIN {ENTITIES_TABLE} e ON e.entity_id = seed.entity_id
                JOIN {ENTITY_ALIASES_TABLE} alias ON alias.entity_id = seed.entity_id
                WHERE {_usable_alias_sql("alias", "e", where_sql)}
                GROUP BY seed.entity_id, alias.alias_key, e.scope_label
                ORDER BY MIN(seed.seed_rank) NULLS FIRST, seed.entity_id, alias.alias_key
            ) WHERE ROWNUM <= :entity_seed_limit
            """,
            binds,
        )
        return [
            EntitySeedRow(
                entity_id=str(row["entity_id"]),
                alias_key=str(row["alias_key"]),
                scope_label=_optional_text(row.get("scope_label")),
                in_question=_int(row.get("in_question")) > 0,
                seed_rank=(
                    _int(row.get("seed_rank")) if row.get("seed_rank") is not None else None
                ),
            )
            for row in rows
        ]

    async def entity_definition_chunks(
        self, filters: dict[str, str], *, alias_keys: Sequence[str], limit: int
    ) -> list[EntityDefinitionRow]:
        """別名のどれかを「定義する行」に持つ chunk（名寄せ）。"""
        keys = list(dict.fromkeys(key for key in alias_keys if key))[:_MAX_IN_VALUES]
        if not keys:
            return []
        where_sql, binds = _oracle_retrieval_where(filters)
        alias_in_sql, alias_binds = _oracle_in_predicate("a.alias_key", "entity_alias", keys)
        binds.update(alias_binds)
        binds["entity_limit"] = max(1, int(limit))
        rows = await self._oracle._fetch_all(
            _render_sql(
                f"""
            SELECT * FROM (
                SELECT {_CHUNK_COLUMNS},
                    a.alias_key AS entity_match_key,
                    e.entity_id AS entity_id,
                    e.display_name AS entity_display_name,
                    e.scope_label AS entity_scope_label
                FROM {ENTITY_ALIASES_TABLE} a
                JOIN {ENTITIES_TABLE} e ON e.entity_id = a.entity_id
                JOIN {ENTITY_CHUNKS_TABLE} ec
                  ON ec.entity_id = a.entity_id AND ec.chunk_role = 'definition'
                JOIN rag_chunks c ON c.chunk_id = ec.chunk_id
                JOIN rag_documents d ON d.document_id = c.document_id
                WHERE {{where_sql}}
                  AND {alias_in_sql}
                  AND {_usable_alias_sql("a", "e", where_sql)}
                ORDER BY c.document_id, c.chunk_index, c.chunk_id, a.alias_key
            ) WHERE ROWNUM <= :entity_limit
            """,
                where_sql=where_sql,
            ),
            binds,
        )
        return [_definition_row(row) for row in rows]

    async def entity_attribute_definition_chunks(
        self, filters: dict[str, str], *, source_chunk_ids: Sequence[str], limit: int
    ) -> list[EntityDefinitionRow]:
        """chunk の属性の実体（台帳の行の「担当部署: 経」）の別名を「定義する行」に持つ chunk（1
        段）。"""
        sources = list(dict.fromkeys(source_chunk_ids))[:_MAX_IN_VALUES]
        if not sources:
            return []
        where_sql, binds = _oracle_retrieval_where(filters)
        source_in_sql, source_binds = _oracle_in_predicate("src.chunk_id", "entity_source", sources)
        binds.update(source_binds)
        binds["entity_limit"] = max(1, int(limit))
        rows = await self._oracle._fetch_all(
            _render_sql(
                f"""
            SELECT * FROM (
                SELECT {_CHUNK_COLUMNS},
                    a.alias_key AS entity_match_key,
                    e.entity_id AS entity_id,
                    e.display_name AS entity_display_name,
                    e.scope_label AS entity_scope_label,
                    src.chunk_id AS entity_via_chunk_id,
                    src_e.scope_label AS entity_via_scope_label
                FROM {ENTITY_CHUNKS_TABLE} src
                JOIN {ENTITIES_TABLE} src_e ON src_e.entity_id = src.entity_id
                JOIN {ENTITY_ALIASES_TABLE} src_a ON src_a.entity_id = src.entity_id
                JOIN {ENTITY_ALIASES_TABLE} a
                  ON a.alias_key = src_a.alias_key AND a.entity_id <> src.entity_id
                JOIN {ENTITIES_TABLE} e ON e.entity_id = a.entity_id
                JOIN {ENTITY_CHUNKS_TABLE} ec
                  ON ec.entity_id = a.entity_id AND ec.chunk_role = 'definition'
                JOIN rag_chunks c ON c.chunk_id = ec.chunk_id
                JOIN rag_documents d ON d.document_id = c.document_id
                WHERE src.chunk_role = 'attribute'
                  AND {source_in_sql}
                  AND {{where_sql}}
                  AND {_usable_alias_sql("a", "e", where_sql)}
                ORDER BY src.chunk_id, c.document_id, c.chunk_index, c.chunk_id, a.alias_key
            ) WHERE ROWNUM <= :entity_limit
            """,
                where_sql=where_sql,
            ),
            binds,
        )
        return [_definition_row(row) for row in rows]


def _delete_chunk_set_entities(connection: OracleConnectionProtocol, chunk_set_id: str) -> None:
    # 別名と関連は FK の ON DELETE CASCADE で一緒に消える。
    _execute(
        connection,
        f"DELETE FROM {ENTITIES_TABLE} WHERE chunk_set_id = :chunk_set_id",
        {"chunk_set_id": chunk_set_id},
    )


def _delete_unlinked_document_entities(
    connection: OracleConnectionProtocol, document_id: str
) -> None:
    # 文書の古い chunk_set（GC で chunk が消えた版）の、関連の無い実体を消す（検索に使われない行）。
    _execute(
        connection,
        f"""
        DELETE FROM {ENTITIES_TABLE} e
        WHERE e.document_id = :document_id
          AND NOT EXISTS (
              SELECT 1 FROM {ENTITY_CHUNKS_TABLE} ec WHERE ec.entity_id = e.entity_id
          )
        """,
        {"document_id": document_id},
    )


def _definition_row(row: Mapping[str, object]) -> EntityDefinitionRow:
    return EntityDefinitionRow(
        chunk=_retrieved_chunk_from_row(row),
        match_key=str(row["entity_match_key"]),
        entity_id=str(row["entity_id"]),
        display_name=str(row.get("entity_display_name") or ""),
        scope_label=_optional_text(row.get("entity_scope_label")),
        via_chunk_id=_optional_text(row.get("entity_via_chunk_id")),
        via_scope_label=_optional_text(row.get("entity_via_scope_label")),
    )


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


__all__ = [
    "ENTITIES_TABLE",
    "ENTITY_ALIASES_TABLE",
    "ENTITY_CHUNKS_TABLE",
    "EntityStore",
    "oracle_entity_schema_sql",
]
