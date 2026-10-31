"""文書の分類（大分類 → 中分類 → 小分類）の一覧（マスタ）の Oracle の保存（#2131）。

分類の値の正本は文書ごとの ``rag_documents.classification``（名前の文字列。検索の絞り込みも
この値）。この表は、どの大分類の下にどの中分類、どの中分類の下にどの小分類があるかを持ち、
文書の詳細・RAG 検索の分類の選択肢と、管理の画面（作成・名前の変更・並べ替え・削除）に使う。

- 名前は保存と同じ正規化（``normalize_category_value``）を通す。同じ親の下で、番号の接頭辞を
  除いた名前（``category_label``。検索の絞り込みと同じ比べ方）が同じ分類は持てない。
- 名前を変えたら、その分類を使っている文書の分類の値も同じ transaction で変える（使っているかは、
  祖先と自分の名前を ``category_label`` で比べる。検索の絞り込みと同じ）。
- 子を持つ分類・文書が使っている分類は削除できない（件数を返す）。
- 分類の一覧は利用者・tenant で分けない（管理者が決める 1 つの一覧）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from uuid import uuid4

from app.clients.oracle import (
    OracleClient,
    OracleConnectionProtocol,
    _category_label_sql,
    _execute,
    _execute_count,
    _fetch_all,
)
from app.schemas.classification import (
    CLASSIFICATION_CATEGORY_KEYS,
    category_label,
    normalize_category_value,
)

DOCUMENT_CATEGORY_TABLE = "rag_document_categories"
# 大分類・中分類・小分類の 3 段。段の番号（1〜3）と、文書の分類の値の項目。
MAX_CATEGORY_LEVEL = len(CLASSIFICATION_CATEGORY_KEYS)
CATEGORY_NAME_MAX_LENGTH = 200


def oracle_document_category_schema_sql(table_name: str = DOCUMENT_CATEGORY_TABLE) -> str:
    """分類の一覧の表の DDL（#2131）。

    ``parent_id`` は親の分類（大分類は NULL）。同じ親の下の同じ名前は一意の索引でも防ぐ（番号の
    接頭辞の違いは API で防ぐ）。親の削除は、子があれば API が止めるので FK は削除を連鎖させない。
    """
    return f"""
CREATE TABLE {table_name} (
    category_id     VARCHAR2(64) PRIMARY KEY,
    parent_id       VARCHAR2(64),
    category_level  NUMBER(1) NOT NULL,
    name            VARCHAR2({CATEGORY_NAME_MAX_LENGTH} CHAR) NOT NULL,
    sort_order      NUMBER(10) DEFAULT 0 NOT NULL,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT {table_name}_level_ck CHECK (category_level BETWEEN 1 AND {MAX_CATEGORY_LEVEL}),
    CONSTRAINT {table_name}_parent_fk FOREIGN KEY (parent_id) REFERENCES {table_name} (category_id)
);

CREATE UNIQUE INDEX {table_name}_name_ux
    ON {table_name} (NVL(parent_id, '-'), name);

CREATE INDEX {table_name}_parent_idx
    ON {table_name} (parent_id, sort_order);
""".strip()


class CategoryNotFoundError(LookupError):
    """分類（または親の分類）が無い。"""


class CategoryConflictError(ValueError):
    """同じ親の下に同じ名前の分類がある。"""


class CategoryDepthError(ValueError):
    """小分類の下には分類を作れない。"""


@dataclass
class CategoryInUseError(ValueError):
    """子を持つ・文書が使っている分類は削除できない。"""

    child_count: int
    document_count: int


@dataclass
class CategoryNode:
    """分類の一覧の 1 件と、その下の分類（並び順）。"""

    id: str
    name: str
    level: int
    parent_id: str | None
    sort_order: int
    # この分類（とその下の分類）を使っている文書の数。
    document_count: int = 0
    children: list[CategoryNode] = field(default_factory=list)


def build_category_tree(
    rows: Sequence[Mapping[str, object]],
    usage: Sequence[tuple[str | None, str | None, str | None, int]] = (),
) -> list[CategoryNode]:
    """表の行を、並び順の木にする。``usage`` は文書の分類の (大, 中, 小, 文書の数)。

    文書の数は、祖先と自分の名前（番号の接頭辞を除いた名前）が文書の値と一致する文書を数える。
    """
    nodes = {
        str(row["category_id"]): CategoryNode(
            id=str(row["category_id"]),
            name=str(row["name"]),
            level=int(row["category_level"]),  # type: ignore[call-overload]
            parent_id=str(row["parent_id"]) if row.get("parent_id") else None,
            sort_order=int(row.get("sort_order") or 0),  # type: ignore[call-overload]
        )
        for row in rows
    }
    roots: list[CategoryNode] = []
    for node in nodes.values():
        parent = nodes.get(node.parent_id) if node.parent_id else None
        (parent.children if parent else roots).append(node)

    def sort(items: list[CategoryNode]) -> None:
        items.sort(key=lambda item: (item.sort_order, item.name))
        for item in items:
            sort(item.children)

    sort(roots)
    labels = [(tuple(category_label(value) for value in values), count) for *values, count in usage]

    def count(items: list[CategoryNode], path: tuple[str, ...]) -> None:
        for item in items:
            own = (*path, category_label(item.name))
            item.document_count = sum(
                number for values, number in labels if values[: len(own)] == own
            )
            count(item.children, own)

    count(roots, ())
    return roots


def _ancestors(connection: OracleConnectionProtocol, category_id: str) -> list[dict[str, object]]:
    """分類と祖先（大分類から自分まで）。無ければ CategoryNotFoundError。"""
    chain: list[dict[str, object]] = []
    current: str | None = category_id
    while current:
        rows = _fetch_all(
            connection,
            f"""
            SELECT category_id, parent_id, category_level, name, sort_order
            FROM {DOCUMENT_CATEGORY_TABLE}
            WHERE category_id = :category_id
            """,
            {"category_id": current},
        )
        if not rows:
            raise CategoryNotFoundError(current)
        chain.insert(0, rows[0])
        current = str(rows[0]["parent_id"]) if rows[0].get("parent_id") else None
    return chain


def _siblings(
    connection: OracleConnectionProtocol, parent_id: str | None
) -> list[dict[str, object]]:
    return _fetch_all(
        connection,
        f"""
        SELECT category_id, name, sort_order
        FROM {DOCUMENT_CATEGORY_TABLE}
        WHERE NVL(parent_id, '-') = NVL(:parent_id, '-')
        ORDER BY sort_order, name
        """,
        {"parent_id": parent_id},
    )


def _ensure_unique(
    siblings: Sequence[Mapping[str, object]], name: str, *, exclude: str | None = None
) -> None:
    label = category_label(name)
    if any(
        category_label(row["name"]) == label and str(row["category_id"]) != exclude
        for row in siblings
    ):
        raise CategoryConflictError(name)


def _documents_predicate(path: Sequence[str]) -> tuple[str, dict[str, object]]:
    """祖先と自分の名前（番号の接頭辞を除いた名前）が一致する文書の条件と bind。"""
    clauses = []
    binds: dict[str, object] = {}
    for index, name in enumerate(path):
        column = f"JSON_VALUE(d.classification, '$.{CLASSIFICATION_CATEGORY_KEYS[index]}')"
        clauses.append(f"{_category_label_sql(column)} = :category_{index}")
        binds[f"category_{index}"] = category_label(name)
    return " AND ".join(clauses), binds


def _clean_name(name: object) -> str:
    cleaned = normalize_category_value(name)
    if not cleaned:
        raise ValueError("分類の名前を入力してください。")
    if len(cleaned) > CATEGORY_NAME_MAX_LENGTH:
        raise ValueError(f"分類の名前は {CATEGORY_NAME_MAX_LENGTH} 文字以内にしてください。")
    return cleaned


class DocumentCategoryStore:
    """分類の一覧の読み書き（OracleClient の接続と transaction を使う）。"""

    def __init__(self, oracle: OracleClient | None = None) -> None:
        self._oracle = oracle or OracleClient()

    async def list_tree(self) -> list[CategoryNode]:
        """分類の一覧を、並び順の木で返す（文書の数つき）。"""
        rows = await self._oracle._fetch_all(
            f"""
            SELECT category_id, parent_id, category_level, name, sort_order
            FROM {DOCUMENT_CATEGORY_TABLE}
            """
        )
        usage_rows = await self._oracle._fetch_all(
            """
            SELECT JSON_VALUE(d.classification, '$.large_category') AS large_category,
                   JSON_VALUE(d.classification, '$.middle_category') AS middle_category,
                   JSON_VALUE(d.classification, '$.small_category') AS small_category,
                   COUNT(*) AS document_count
            FROM rag_documents d
            WHERE d.classification IS NOT NULL
            GROUP BY JSON_VALUE(d.classification, '$.large_category'),
                     JSON_VALUE(d.classification, '$.middle_category'),
                     JSON_VALUE(d.classification, '$.small_category')
            """
        )
        usage = [
            (
                _optional_text(row.get("large_category")),
                _optional_text(row.get("middle_category")),
                _optional_text(row.get("small_category")),
                int(row.get("document_count") or 0),  # type: ignore[call-overload]
            )
            for row in usage_rows
        ]
        return build_category_tree(rows, usage)

    async def create(self, *, parent_id: str | None, name: object) -> str:
        """分類を作り、その ID を返す（同じ親の下の最後に並べる）。"""
        cleaned = _clean_name(name)

        def operation(connection: OracleConnectionProtocol) -> str:
            level = 1
            if parent_id:
                level = len(_ancestors(connection, parent_id)) + 1
                if level > MAX_CATEGORY_LEVEL:
                    raise CategoryDepthError(parent_id)
            siblings = _siblings(connection, parent_id)
            _ensure_unique(siblings, cleaned)
            category_id = uuid4().hex
            _execute(
                connection,
                f"""
                INSERT INTO {DOCUMENT_CATEGORY_TABLE}
                    (category_id, parent_id, category_level, name, sort_order)
                VALUES (:category_id, :parent_id, :category_level, :name, :sort_order)
                """,
                {
                    "category_id": category_id,
                    "parent_id": parent_id,
                    "category_level": level,
                    "name": cleaned,
                    "sort_order": max(
                        (int(row.get("sort_order") or 0) for row in siblings),  # type: ignore[call-overload]
                        default=0,
                    )
                    + 1,
                },
            )
            return category_id

        return await self._oracle._run_transaction(operation)

    async def rename(self, category_id: str, name: object) -> int:
        """名前を変え、その分類を使っている文書の値も変える。変えた文書の数を返す。"""
        cleaned = _clean_name(name)

        def operation(connection: OracleConnectionProtocol) -> int:
            chain = _ancestors(connection, category_id)
            node = chain[-1]
            if str(node["name"]) == cleaned:
                return 0
            parent_id = str(node["parent_id"]) if node.get("parent_id") else None
            _ensure_unique(_siblings(connection, parent_id), cleaned, exclude=category_id)
            _execute(
                connection,
                f"""
                UPDATE {DOCUMENT_CATEGORY_TABLE}
                SET name = :name, updated_at = SYSTIMESTAMP
                WHERE category_id = :category_id
                """,
                {"name": cleaned, "category_id": category_id},
            )
            predicate, binds = _documents_predicate([str(row["name"]) for row in chain])
            key = CLASSIFICATION_CATEGORY_KEYS[len(chain) - 1]
            return _execute_count(
                connection,
                f"""
                UPDATE rag_documents d
                SET classification = JSON_TRANSFORM(d.classification, SET '$.{key}' = :new_name)
                WHERE d.classification IS NOT NULL AND {predicate}
                """,
                {**binds, "new_name": cleaned},
            )

        return await self._oracle._run_transaction(operation)

    async def move(self, category_id: str, direction: int) -> None:
        """同じ親の下で 1 つ前（-1）・後（+1）と入れ替える。端なら何もしない。"""

        def operation(connection: OracleConnectionProtocol) -> None:
            node = _ancestors(connection, category_id)[-1]
            parent_id = str(node["parent_id"]) if node.get("parent_id") else None
            ids = [str(row["category_id"]) for row in _siblings(connection, parent_id)]
            index = ids.index(category_id)
            target = index + direction
            if target < 0 or target >= len(ids):
                return
            ids[index], ids[target] = ids[target], ids[index]
            # 並びを 1 から振り直す（同じ sort_order の行が残らないように）。
            for order, item in enumerate(ids, start=1):
                _execute(
                    connection,
                    f"""
                    UPDATE {DOCUMENT_CATEGORY_TABLE}
                    SET sort_order = :sort_order
                    WHERE category_id = :category_id
                    """,
                    {"sort_order": order, "category_id": item},
                )

        await self._oracle._run_transaction(operation)

    async def delete(self, category_id: str) -> None:
        """分類を削除する。子を持つ・文書が使っているときは CategoryInUseError。"""

        def operation(connection: OracleConnectionProtocol) -> None:
            chain = _ancestors(connection, category_id)
            children = _fetch_all(
                connection,
                f"SELECT COUNT(*) AS count_value FROM {DOCUMENT_CATEGORY_TABLE}"
                " WHERE parent_id = :category_id",
                {"category_id": category_id},
            )
            predicate, binds = _documents_predicate([str(row["name"]) for row in chain])
            documents = _fetch_all(
                connection,
                "SELECT COUNT(*) AS count_value FROM rag_documents d"
                f" WHERE d.classification IS NOT NULL AND {predicate}",
                binds,
            )
            child_count = int(children[0]["count_value"]) if children else 0  # type: ignore[call-overload]
            document_count = int(documents[0]["count_value"]) if documents else 0  # type: ignore[call-overload]
            if child_count or document_count:
                raise CategoryInUseError(child_count, document_count)
            _execute(
                connection,
                f"DELETE FROM {DOCUMENT_CATEGORY_TABLE} WHERE category_id = :category_id",
                {"category_id": category_id},
            )

        await self._oracle._run_transaction(operation)


def _optional_text(value: object) -> str | None:
    return str(value) if value not in (None, "") else None
