"""用户可见的 Oracle schema object 规则。"""

from __future__ import annotations

import re

from .models import SchemaCatalog, SchemaObjectPage

# 名前に含むと Oracle の内部 object とみなす文字（例: Oracle Text の索引の内部表 `DR$…$B`、
# vector 索引の内部表 `VECTOR$…$HNSW_CHECKPOINTS`。owner 名にも使う）。
_SYSTEM_OBJECT_NAME_MARKERS = ("$", "#")
# 3 製品は同じ schema を共有する（#212）。共通基盤（PLATFORM_: 認証テーブルなど）と他製品の
# テーブル（RAG_ / AGENT_）も業務データではないため、業務ユーザーの対象一覧に出さない。
# 一覧を SQL で絞る呼び出し側（Deep Data Security の対象の一覧など）も同じ接頭辞を使う（#1708）。
SYSTEM_OBJECT_NAME_PREFIXES = ("NL2SQL_", "PLATFORM_", "RAG_", "AGENT_")
# 上の判定で拒否したときの文言。NL2SQL の表だけでなく共通基盤・他製品の表と Oracle の内部 object も
# 対象なので、「NL2SQL のシステム object」とだけ書かない（#933）。
SYSTEM_OBJECT_BLOCKED_MESSAGE = (
    "NL2SQL_・PLATFORM_・RAG_・AGENT_ で始まる表/VIEW（NL2SQL・共通基盤・他製品の表）と、"
    "名前に $ / # を含む Oracle の内部 object は業務データではないため、ここでは扱えません。"
    "NL2SQL_ の表はシステムテーブル管理から管理します。"
)


# SQL の断片に埋め込む列の式（`o.object_name`・`OWNER_NAME` など）。固定の式だけを受ける。
_SQL_COLUMN_EXPRESSION = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)?")


def _sql_column(column_sql: str) -> str:
    if not _SQL_COLUMN_EXPRESSION.fullmatch(column_sql):
        raise ValueError(f"SQL の列の式ではありません: {column_sql!r}")
    return column_sql


def system_object_prefix_sql_filters(column_sql: str) -> tuple[str, ...]:
    """`SYSTEM_OBJECT_NAME_PREFIXES` の object を SQL で除く条件（AND でつなぐ断片）を返す。

    一覧を SQL でページング・件数取得するときは、取った後に Python で落とさず SQL で除く。
    後から落とすと 1 ページが数件になり、件数（total）もずれる（#1708）。
    `column_sql` は固定の列の式（例: ``o.object_name``）だけを渡す（利用者の入力を渡さない）。
    大文字小文字は `is_user_visible_object_name` と同じく区別しない。
    """

    column = _sql_column(column_sql)
    return tuple(
        f"UPPER({column}) NOT LIKE '{prefix.replace('_', chr(92) + '_')}%' ESCAPE '\\'"
        for prefix in SYSTEM_OBJECT_NAME_PREFIXES
    )


def system_object_marker_sql_filters(column_sql: str) -> tuple[str, ...]:
    """名前に `$` / `#` を含む Oracle の内部 object を SQL で除く条件を返す。

    LIKE の wildcard を避けて INSTR で比べる（`_` は LIKE の 1 文字の wildcard）。
    """

    column = _sql_column(column_sql)
    return tuple(f"INSTR({column}, '{marker}') = 0" for marker in _SYSTEM_OBJECT_NAME_MARKERS)


def user_visible_object_sql_filters(
    *,
    object_column: str,
    owner_column: str | None = None,
) -> tuple[str, ...]:
    """`is_user_visible_schema_object` と同じ規則で、システムの object を SQL で除く条件を返す。

    業務の DB object を一覧・検索・件数・ページング・候補で出す SQL は、この条件を WHERE に入れて
    システムテーブル（`NL2SQL_` / `PLATFORM_` / `RAG_` / `AGENT_`）と Oracle の内部 object
    （名前に `$` / `#`）を最初から除く（#1710）。取った後に Python で落とすと、1 ページが埋まらず
    件数もずれる。Python の判定は、名前・ID を直接指定する操作を拒む安全策として残す。
    owner 名は `$` / `#` だけを見る（`NL2SQL_APP` などの schema は業務の schema として見せる）。
    """

    filters: list[str] = []
    if owner_column is not None:
        filters.extend(system_object_marker_sql_filters(owner_column))
    filters.extend(system_object_marker_sql_filters(object_column))
    filters.extend(system_object_prefix_sql_filters(object_column))
    return tuple(filters)


def user_visible_object_sql_predicate(
    *,
    object_column: str,
    owner_column: str | None = None,
) -> str:
    """`user_visible_object_sql_filters` を AND でつないだ 1 つの条件。"""

    return " AND ".join(
        user_visible_object_sql_filters(object_column=object_column, owner_column=owner_column)
    )


def _split_identifier_parts(value: str) -> list[str]:
    """Owner-qualified name を、二重引用符内の dot を保って分割する。"""

    parts: list[str] = []
    buffer: list[str] = []
    in_double = False
    for char in str(value or "").strip():
        if char == '"':
            in_double = not in_double
            buffer.append(char)
            continue
        if char == "." and not in_double:
            part = "".join(buffer).strip()
            if part:
                parts.append(part)
            buffer = []
            continue
        buffer.append(char)
    tail = "".join(buffer).strip()
    if tail:
        parts.append(tail)
    return parts


def _normalize_identifier_part(value: str) -> str:
    normalized = str(value or "").strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] == '"':
        normalized = normalized[1:-1].replace('""', '"')
    return normalized.upper()


def _is_user_visible_owner_name(owner_name: str) -> bool:
    normalized = _normalize_identifier_part(owner_name)
    return bool(normalized) and not any(
        marker in normalized for marker in _SYSTEM_OBJECT_NAME_MARKERS
    )


def _is_user_visible_object_part(object_name: str) -> bool:
    normalized = _normalize_identifier_part(object_name)
    return (
        bool(normalized)
        and not any(marker in normalized for marker in _SYSTEM_OBJECT_NAME_MARKERS)
        and not normalized.startswith(SYSTEM_OBJECT_NAME_PREFIXES)
    )


def is_user_visible_object_name(object_name: str) -> bool:
    """系统生成对象不进入业务用户使用的对象目录。"""

    parts = _split_identifier_parts(object_name)
    if not parts:
        return False
    return all(_is_user_visible_owner_name(part) for part in parts[:-1]) and (
        _is_user_visible_object_part(parts[-1])
    )


def is_user_visible_owner_name(owner_name: str) -> bool:
    """schema owner 名は Oracle system marker だけを隠し、NL2SQL_ prefix は許可する。"""

    return _is_user_visible_owner_name(owner_name)


def is_user_visible_schema_object(owner: str, object_name: str) -> bool:
    """owner/object が分離した schema metadata 用の可視性判定。"""

    normalized_owner = owner.strip()
    owner_visible = not normalized_owner or _is_user_visible_owner_name(normalized_owner)
    return owner_visible and is_user_visible_object_name(object_name)


def filter_user_visible_catalog(catalog: SchemaCatalog) -> SchemaCatalog:
    """过滤旧 snapshot/cache 中残留的系统对象及其依赖。"""

    tables = [
        table
        for table in catalog.tables
        if is_user_visible_schema_object(table.owner, table.table_name)
    ]
    dependencies = [
        dependency
        for dependency in catalog.view_dependencies
        if is_user_visible_schema_object(dependency.owner, dependency.view_name)
        and is_user_visible_schema_object(
            dependency.referenced_owner,
            dependency.referenced_name,
        )
    ]
    if len(tables) == len(catalog.tables) and len(dependencies) == len(catalog.view_dependencies):
        return catalog
    return catalog.model_copy(
        deep=True,
        update={"tables": tables, "view_dependencies": dependencies},
    )


def filter_user_visible_object_page(page: SchemaObjectPage) -> SchemaObjectPage:
    """自定义 repository 的异常响应也不会泄露系统对象。"""

    items = [
        item for item in page.items if is_user_visible_schema_object(item.owner, item.object_name)
    ]
    hidden_items = [item for item in page.items if item not in items]
    hidden_tables = sum(
        item.object_type.upper() not in {"VIEW", "MATERIALIZED VIEW"} for item in hidden_items
    )
    hidden_views = len(hidden_items) - hidden_tables
    return page.model_copy(
        update={
            "items": items,
            "total": (max(0, page.total - len(hidden_items)) if page.total is not None else None),
            "table_count": max(0, page.table_count - hidden_tables),
            "view_count": max(0, page.view_count - hidden_views),
        }
    )
