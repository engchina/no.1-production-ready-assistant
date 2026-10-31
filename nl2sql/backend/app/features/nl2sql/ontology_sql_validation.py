"""Oracle 式を Profile の表・列と SELECT の名前解決スコープで検証する。

表・列は `object_identity` の照合 token で識別する。引用なしの識別子は大文字、`"Mixed_Case"` は
大文字小文字を保つ（Oracle の引用規則）。大文字化して照合すると、`SALES."Mixed_Case"` と大文字の
同名表 `SALES.MIXED_CASE` を同じ表として扱う（#573）。
"""

from __future__ import annotations

from typing import Any, cast

from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope, traverse_scope

from .object_identity import (
    is_unquoted_object_part,
    object_part_name,
    split_match_key,
    sql_identifier_token,
)


def identifier_token(identifier: Any) -> str:
    """sqlglot の識別子 1 部分を照合 token にする（引用なしは大文字、引用ありは保持）。"""

    if identifier is None:
        return ""
    if isinstance(identifier, exp.Identifier):
        name, quoted = identifier.name, bool(identifier.args.get("quoted"))
    else:
        name, quoted = str(getattr(identifier, "name", identifier) or ""), False
    if not name:
        return ""
    try:
        return sql_identifier_token(name, quoted=quoted)
    except ValueError:
        return name


def token_identifier(value: str) -> exp.Identifier:
    """照合 token または入力 1 部分（`"Amount"` / `amount`）を SQL の識別子にする。"""

    name = object_part_name(value)
    return exp.to_identifier(name, quoted=not is_unquoted_object_part(name))


def _table_tokens(table: exp.Table) -> tuple[str, str]:
    return identifier_token(table.args.get("db")), identifier_token(table.this)


def physical_table(table: exp.Table, physical: dict[str, set[str]]) -> str:
    owner, name = _table_tokens(table)
    matches = [
        key
        for key in physical
        if split_match_key(key)[-1:] == [name] and (not owner or key == f"{owner}.{name}")
    ]
    if table.catalog or len(matches) != 1:
        raise ValueError(f"SQL 表 {table.sql()} を業務プロファイル内で一意に解決できません。")
    return matches[0]


def validated_sql(tree: exp.Expression, physical: dict[str, set[str]]) -> exp.Expression:
    if isinstance(tree, exp.Query):
        schema: dict[str, Any] = {}
        for key, columns in physical.items():
            parts = split_match_key(key)
            owner, table = (parts[0], parts[-1]) if len(parts) >= 2 else ("", parts[-1])
            # sqlglot は schema の名前を Oracle の識別子として解釈する。照合 token
            # （`"Mixed_Case"`）は引用名、`ORDERS` は引用なしの名前になる。
            schema.setdefault(owner, {})[table] = dict.fromkeys(columns, "UNKNOWN")
        # CTE は物理表ではない。各スコープの実ソースだけを Profile と照合する。
        for scope in traverse_scope(tree):
            for _, source in scope.selected_sources.values():
                if isinstance(source, exp.Table):
                    physical_table(source, physical)
        return qualify(
            tree.copy(),
            dialect="oracle",
            schema=schema,
            identify=False,
            # CTE 列リスト等から生成された小文字 alias も再解析時に意味を保持する。
            quote_identifiers=True,
        )
    for column in tree.find_all(exp.Column):
        physical_column(column, physical)
    if any(tree.find_all(exp.Query)) or any(tree.find_all(exp.Table)):
        raise ValueError("副問い合わせを含む式は SELECT 全体として検証してください。")
    return tree


def expression_in_query(
    expression: exp.Expression, query: exp.Expression, physical: dict[str, set[str]]
) -> exp.Expression:
    """SELECT のソース・別名・CTE で独立したフィルタを解決する。SQL は実行しない。"""
    if not isinstance(query, exp.Select):
        raise ValueError("フィルタの所属 SELECT を一意に指定してください。")
    query = cast(exp.Select, validated_sql(query, physical))
    scope = build_scope(query)
    if scope is None:
        raise ValueError("フィルタの所属 SELECT を解決できません。")

    def bind_physical_reference(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column) and node.db:
            target = physical_column(node, physical)
            candidates = []
            for alias, (_, source) in scope.selected_sources.items():
                if isinstance(source, exp.Table):
                    columns = [exp.Column(this=node.this.copy())]
                elif isinstance(source, Scope) and isinstance(source.expression, exp.Select):
                    # 公開された投影だけを辿る。未投影列や計算結果を元の列と同一視しない。
                    columns = [
                        exp.column(p.alias_or_name, quoted=True) for p in source.expression.selects
                    ]
                else:
                    continue
                for column in columns:
                    column.set("table", exp.to_identifier(alias, quoted=True))
                    try:
                        resolved = physical_column(column, physical, scope)
                    except ValueError:
                        continue
                    if resolved == target:
                        candidates.append(column)
            if len(candidates) != 1:
                raise ValueError("フィルタの物理参照を SELECT 内で一意に解決できません。")
            return candidates[0]
        return node

    expression = expression.copy().transform(bind_physical_reference)
    name = "NL2SQL_ONTOLOGY_PREDICATE"
    # 生成する列別名の衝突回避（物理 object の識別ではない）。
    while name in {p.alias_or_name.upper() for p in query.selects}:
        name += "_"
    augmented = query.select(exp.alias_(expression.copy(), name, quoted=True), append=True)
    qualified = cast(exp.Select, validated_sql(augmented, physical))
    return cast(exp.Expression, qualified.selects[-1].unalias())


def physical_column(
    column: exp.Column, physical: dict[str, set[str]], scope: Scope | None = None
) -> str:
    """列参照を Profile の `OWNER.OBJECT.COLUMN` 照合キー（各部は token）に解決する。"""

    name = identifier_token(column.this)
    if scope is not None and column.table and not column.db:
        selected = scope.selected_sources.get(column.table)
        if selected is None:
            # SELECT 内の表別名（`o`）は物理 object ではない。引用なしの別名は大文字で引く。
            selected = scope.selected_sources.get(column.table.upper())
        if selected:
            source = selected[1]
            if isinstance(source, Scope):
                if not isinstance(source.expression, exp.Select):
                    raise ValueError("集合演算の粒度には明示的な mapping が必要です。")
                projections = [
                    p
                    for p in source.expression.selects
                    # qualify 済みの識別子は通常名が正規化され、引用名の大小文字は保持される。
                    if p.alias_or_name == column.name
                ]
                if len(projections) != 1:
                    raise ValueError("派生列の物理参照を一意に解決できません。")
                value = projections[0].unalias()
                if not isinstance(value, exp.Column):
                    raise ValueError("派生式の集約粒度には明示的な mapping が必要です。")
                return physical_column(value, physical, source)
            if isinstance(source, exp.Table):
                key = physical_table(source, physical)
                if name in physical[key]:
                    return f"{key}.{name}"
        raise ValueError(f"SQL 列 {column.sql()} の参照元を解決できません。")
    owner = identifier_token(column.args.get("db"))
    table = identifier_token(column.args.get("table"))
    matches = [
        key
        for key, columns in physical.items()
        if name in columns
        and (not table or split_match_key(key)[-1:] == [table])
        and (not owner or key == f"{owner}.{table}")
    ]
    if column.catalog or len(matches) != 1:
        raise ValueError(f"SQL 列 {column.sql()} を業務プロファイル内で一意に解決できません。")
    return f"{matches[0]}.{name}"


def canonical_expression(
    expression: exp.Expression, physical: dict[str, set[str]], scope: Scope | None = None
) -> str:
    def resolve(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column):
            owner, table, column = split_match_key(physical_column(node, physical, scope))[-3:]
            return exp.column(
                token_identifier(column), table=token_identifier(table), db=token_identifier(owner)
            )
        return node

    return expression.copy().transform(resolve).sql(dialect="oracle")
