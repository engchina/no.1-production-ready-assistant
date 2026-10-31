"""リンク型（Link Type）の結合のキーが、主キー・一意キー・外部キーに当たるか（#1741）。

一意でない列（部門名 `DEPARTMENT_NAME` など）でつなぐと多対多になりうるため、Select AI / LLM が
誤った JOIN を作り、多重度も決められない。結合のキーは次のどれかに当たるときだけ「キーでつなぐ」
とみなす（根拠は DB の制約と物理の列）:

- 外部キーの制約の列の組（`schema_context.relationships` の `foreign_key`）
- どちらかの端の表の主キー・一意キーの制約の列（の組）を含む
- 制約の無い表・ビュー: オブジェクト型の主キーに対応する列、または制約の無い表の「表名_ID」の列
  （`ontology_unified_model._infer_links` の同じ名前・型の ID 列と同じ規則）

式が単純な列の等号（`A.X = B.Y AND …`）でない、両端の表が決まらない、列が表に無いときは判断しない
（`None`。式・対応の誤りは別の検査が知らせる）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .object_identity import object_match_key
from .ontology_definitions import BusinessDefinition, ObjectTypeDefinition


def schema_tables(schema: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """schema context の表・ビュー（`OWNER.OBJECT` の照合キー → 列・主キー・一意キー）。"""

    tables: dict[str, dict[str, Any]] = {}
    for obj in (schema or {}).get("objects", []):
        owner, name = str(obj.get("owner", "")), str(obj.get("object_name", ""))
        key = f"{object_match_key(owner)}.{object_match_key(name)}"
        columns = {}
        for column in obj.get("columns", []):
            if isinstance(column, dict):
                text = str(column.get("column_name", column.get("column", column.get("name", ""))))
                columns[object_match_key(text)] = {**column, "column": text}
        primary = [
            [object_match_key(str(c)) for c in constraint.get("columns", [])]
            for constraint in obj.get("constraints", [])
            if constraint.get("constraint_type") == "P"
        ]
        unique = [
            frozenset(object_match_key(str(c)) for c in constraint.get("columns", []))
            for constraint in obj.get("constraints", [])
            if constraint.get("constraint_type") in {"P", "U"} and constraint.get("columns")
        ]
        tables[key] = {
            "qualified": str(obj.get("object") or f"{owner}.{name}"),
            "object_name": name,
            "kind": str(obj.get("object_type", "")).lower(),
            "columns": columns,
            "primary_key": primary[0] if primary else [],
            # 主キー・一意キーの列の組（多重度を決める根拠。#1736）。
            "unique_keys": unique,
            "has_constraints": bool(obj.get("constraints")),
        }
    return tables


@dataclass(frozen=True)
class LinkJoinCheck:
    """リンク型の結合のキーの判定。`keyed` が `None` なら判断しない。"""

    keyed: bool | None
    # 結合のキーの列（物理の名前。`OBJECT.COLUMN`）。診断の文に使う。
    columns: tuple[str, ...] = ()


def _object_table(d: BusinessDefinition | None) -> str:
    if not isinstance(d, ObjectTypeDefinition):
        return ""
    tables = {
        f"{object_match_key(m.owner)}.{object_match_key(m.object_name)}"
        for m in d.mappings
        if m.object_name
    }
    return next(iter(tables)) if len(tables) == 1 else ""


def _resolve_table(part: str, candidates: list[str]) -> str:
    """結合の式の表の部分（`OWNER.OBJECT` か `OBJECT`）を、両端の表の照合キーに当てる。"""

    if part in candidates:
        return part
    named = [key for key in candidates if key.rsplit(".", 1)[-1] == part.rsplit(".", 1)[-1]]
    return named[0] if len(set(named)) == 1 else ""


def _join_pairs(sql: str, candidates: list[str]) -> list[tuple[tuple[str, str], tuple[str, str]]]:
    """`A.X = B.Y AND …` を ((表, 列), (表, 列)) の組にする（単純な等号でなければ空）。"""

    import sqlglot
    from sqlglot import exp

    try:
        tree = sqlglot.parse_one(sql, read="oracle")
    except sqlglot.errors.SqlglotError:
        return []
    parts = list(tree.flatten()) if isinstance(tree, exp.And) else [tree]
    pairs: list[tuple[tuple[str, str], tuple[str, str]]] = []
    for part in parts:
        if not (
            isinstance(part, exp.EQ)
            and isinstance(part.this, exp.Column)
            and isinstance(part.expression, exp.Column)
        ):
            return []
        sides: list[tuple[str, str]] = []
        for column in (part.this, part.expression):
            key = object_match_key(column.sql(dialect="oracle"))
            table_part, _, name = key.rpartition(".")
            table = _resolve_table(table_part, candidates) if table_part else ""
            if not table or not name:
                return []
            sides.append((table, name))
        pairs.append((sides[0], sides[1]))
    return pairs


def _foreign_keys(schema: dict[str, Any] | None) -> set[frozenset[tuple[str, str]]]:
    """外部キーの制約の列の組（(表, 列) の集合）。"""

    result: set[frozenset[tuple[str, str]]] = set()
    for relationship in (schema or {}).get("relationships", []):
        if relationship.get("kind") != "foreign_key":
            continue
        source = object_match_key(str(relationship.get("source_object", "")))
        target = object_match_key(str(relationship.get("target_object", "")))
        columns: set[tuple[str, str]] = set()
        for condition in relationship.get("join_conditions", []):
            left = object_match_key(str(condition.get("left", ""))).rsplit(".", 1)[-1]
            right = object_match_key(str(condition.get("right", ""))).rsplit(".", 1)[-1]
            columns.update({(source, left), (target, right)})
        if columns:
            result.add(frozenset(columns))
    return result


def _unique_keys(
    table_key: str,
    tables: Mapping[str, dict[str, Any]],
    owner: ObjectTypeDefinition | None,
    by_name: Mapping[str, BusinessDefinition],
) -> list[frozenset[str]]:
    table = tables[table_key]
    keys = list(table["unique_keys"])
    if table["has_constraints"]:
        return keys
    # 制約の無い表・ビュー: オブジェクト型の主キーに対応する列の組と、表の「表名_ID」の列。
    if owner is not None and owner.primary_key:
        columns: set[str] = set()
        for name in owner.primary_key:
            prop = by_name.get(name)
            mapped = [
                m
                for m in getattr(prop, "mappings", [])
                if m.column_name.strip() not in {"", "*"}
                and not m.expression_sql
                and f"{object_match_key(m.owner)}.{object_match_key(m.object_name)}" == table_key
            ]
            if len(mapped) != 1:
                columns = set()
                break
            columns.add(object_match_key(mapped[0].column_name))
        if columns:
            keys.append(frozenset(columns))
    guess = object_match_key(f"{table['object_name']}_ID")
    if table["kind"] != "view" and guess in table["columns"]:
        keys.append(frozenset({guess}))
    return keys


def link_join_check(
    link: BusinessDefinition,
    by_name: Mapping[str, BusinessDefinition],
    schema: dict[str, Any] | None,
    tables: Mapping[str, dict[str, Any]] | None = None,
) -> LinkJoinCheck:
    """リンク型の結合のキーが、主キー・一意キー・外部キーに当たるか。"""

    sql = str(getattr(link, "join_expression_sql", "") or "").strip()
    if link.kind != "link_type" or not sql:
        return LinkJoinCheck(None)
    tables = tables if tables is not None else schema_tables(schema)
    ends = {
        name: _object_table(by_name.get(str(getattr(link, name, "") or "")))
        for name in ("source", "target")
    }
    candidates = [key for key in dict.fromkeys(ends.values()) if key in tables]
    if len(candidates) != len({k for k in ends.values() if k}) or not candidates:
        return LinkJoinCheck(None)
    pairs = _join_pairs(sql, candidates)
    if not pairs:
        return LinkJoinCheck(None)
    used = [side for pair in pairs for side in pair]
    if any(column not in tables[table]["columns"] for table, column in used):
        return LinkJoinCheck(None)
    shown = tuple(
        dict.fromkeys(
            f"{tables[table]['object_name']}.{tables[table]['columns'][column]['column']}"
            for table, column in used
        )
    )
    if frozenset(used) in _foreign_keys(schema):
        return LinkJoinCheck(True, shown)
    owners = {
        key: by_name.get(str(getattr(link, name, "") or "")) for name, key in ends.items() if key
    }
    for table in candidates:
        side = frozenset(column for t, column in used if t == table)
        owner = owners.get(table)
        if any(
            key <= side
            for key in _unique_keys(
                table,
                tables,
                owner if isinstance(owner, ObjectTypeDefinition) else None,
                by_name,
            )
        ):
            return LinkJoinCheck(True, shown)
    return LinkJoinCheck(False, shown)


@dataclass(frozen=True)
class SuggestedJoin:
    """一意でない列でつなぐリンク型の付け替え先（同じ両端の、キーでつなぐ結合のキー。#1760）。"""

    # 結合のキーの SQL（リンク型の向き。`OWNER.OBJECT.COLUMN = OWNER.OBJECT.COLUMN`）。
    join_expression_sql: str
    # 結合のキーの列の名前（診断・改善案の文に使う。`DEPARTMENT_ID`）。
    columns: tuple[str, ...]
    cardinality: str


_REVERSED_CARDINALITY = {"many_to_one": "one_to_many", "one_to_many": "many_to_one"}


def suggest_link_join(
    link: BusinessDefinition,
    by_name: Mapping[str, BusinessDefinition],
    schema: dict[str, Any] | None,
    tables: Mapping[str, dict[str, Any]] | None = None,
) -> SuggestedJoin | None:
    """一意でない列でつなぐリンク型の、同じ両端の付け替え先を返す（無ければ `None`。#1760）。

    根拠は AI 構築の関係の推定（`ontology_unified_model._infer_links`）と同じ: 両端の表の外部キーの
    制約、または同じ名前・型の列が片方の端の主キー（制約。制約の無い表は「表名_ID」、ビューは
    オブジェクト型の主キー）であるもの。作った結合のキーが `link_join_check` でキーでつなぐと
    判定できるときだけ返す（推測で埋めない）。
    """

    from .ontology_unified_model import base_data_type

    tables = tables if tables is not None else schema_tables(schema)
    source = by_name.get(str(getattr(link, "source", "") or ""))
    target = by_name.get(str(getattr(link, "target", "") or ""))
    source_key, target_key = _object_table(source), _object_table(target)
    if (
        link.kind != "link_type"
        or not source_key
        or not target_key
        or source_key == target_key
        or source_key not in tables
        or target_key not in tables
    ):
        return None
    left, right = tables[source_key], tables[target_key]

    def build(pairs: list[tuple[str, str]], cardinality: str) -> SuggestedJoin | None:
        join = " AND ".join(
            f"{left['qualified']}.{left['columns'][a]['column']} = "
            f"{right['qualified']}.{right['columns'][b]['column']}"
            for a, b in pairs
        )
        candidate = link.model_copy(update={"join_expression_sql": join})
        if link_join_check(candidate, by_name, schema, tables).keyed is not True:
            return None
        columns = tuple(dict.fromkeys(left["columns"][a]["column"] for a, _ in pairs))
        return SuggestedJoin(join, columns, cardinality)

    # 1. 外部キーの制約（リンク型の向きにそろえる）。
    for relationship in (schema or {}).get("relationships", []):
        if relationship.get("kind") != "foreign_key":
            continue
        ends = (
            object_match_key(str(relationship.get("source_object", ""))),
            object_match_key(str(relationship.get("target_object", ""))),
        )
        if set(ends) != {source_key, target_key}:
            continue
        forward = ends[0] == source_key
        pairs: list[tuple[str, str]] = []
        for condition in relationship.get("join_conditions", []):
            a = object_match_key(str(condition.get("left", ""))).rsplit(".", 1)[-1]
            b = object_match_key(str(condition.get("right", ""))).rsplit(".", 1)[-1]
            pairs.append((a, b) if forward else (b, a))
        if not pairs or any(
            a not in left["columns"] or b not in right["columns"] for a, b in pairs
        ):
            continue
        cardinality = str(relationship.get("cardinality") or "many_to_one")
        if cardinality not in {"one_to_one", "one_to_many", "many_to_one", "many_to_many"}:
            cardinality = "many_to_one"
        found = build(
            pairs, cardinality if forward else _REVERSED_CARDINALITY.get(cardinality, cardinality)
        )
        if found is not None:
            return found

    # 2. 同じ名前・型の列が、片方の端の主キー（1 列）であるもの。
    owners = {source_key: source, target_key: target}

    def key_column(table_key: str) -> str:
        owner = owners[table_key]
        keys = [
            key
            for key in _unique_keys(
                table_key,
                tables,
                owner if isinstance(owner, ObjectTypeDefinition) else None,
                by_name,
            )
            if len(key) == 1
        ]
        table = tables[table_key]
        if table["primary_key"] and len(table["primary_key"]) == 1:
            return str(table["primary_key"][0])
        return next(iter(keys[0])) if len(keys) == 1 else ""

    def same_type(column: str) -> bool:
        families = {
            "number"
            if (base := base_data_type(str(t["columns"][column].get("data_type", "")))) == "integer"
            else base
            for t in (left, right)
        } - {""}
        return len(families) <= 1

    def one_side_unique(table_key: str, column: str) -> bool:
        owner = owners[table_key]
        return frozenset({column}) in _unique_keys(
            table_key,
            tables,
            owner if isinstance(owner, ObjectTypeDefinition) else None,
            by_name,
        )

    for keyed_side, other_side in ((target_key, source_key), (source_key, target_key)):
        column = key_column(keyed_side)
        if (
            not column
            or column == "ID"
            or column not in tables[other_side]["columns"]
            or not same_type(column)
        ):
            continue
        if keyed_side == target_key:
            cardinality = "one_to_one" if one_side_unique(source_key, column) else "many_to_one"
        else:
            cardinality = "one_to_one" if one_side_unique(target_key, column) else "one_to_many"
        found = build([(column, column)], cardinality)
        if found is not None:
            return found
    return None


def link_cardinality(
    link: BusinessDefinition,
    by_name: Mapping[str, BusinessDefinition],
    schema: dict[str, Any] | None,
    tables: Mapping[str, dict[str, Any]] | None = None,
) -> str | None:
    """結合のキーと両端のキーから決まるリンク型の多重度（決まらなければ `None`。#1765）。

    リンク型の向き（起点 → 終点）で、結合のキーの起点の列の組が起点の主キー・一意キー（制約。制約の
    無い表・ビューはオブジェクト型の主キーか「表名_ID」。`link_join_check` と同じ規則）を含めば
    起点の 1 行は相手の 1 行以下、終点も同じ。両方なら 1 対 1、終点だけなら多対 1、起点だけなら
    1 対多。どちらでもない（一意でない列でつなぐ）・式が単純でない・表が決まらないときは決めない。
    外部キーの列の NULL 可は任意の関係（Optional）で表し、多重度には入れない。
    """

    sql = str(getattr(link, "join_expression_sql", "") or "").strip()
    if link.kind != "link_type" or not sql:
        return None
    tables = tables if tables is not None else schema_tables(schema)
    owners = {
        name: by_name.get(str(getattr(link, name, "") or "")) for name in ("source", "target")
    }
    ends = {name: _object_table(owner) for name, owner in owners.items()}
    if not all(ends.values()) or ends["source"] == ends["target"]:
        return None
    if any(key not in tables for key in ends.values()):
        return None
    pairs = _join_pairs(sql, list(ends.values()))
    if not pairs:
        return None
    used = [side for pair in pairs for side in pair]
    if any(column not in tables[table]["columns"] for table, column in used):
        return None

    def unique(end: str) -> bool:
        table = ends[end]
        side = frozenset(column for t, column in used if t == table)
        owner = owners[end]
        return bool(side) and any(
            key <= side
            for key in _unique_keys(
                table,
                tables,
                owner if isinstance(owner, ObjectTypeDefinition) else None,
                by_name,
            )
        )

    source_unique, target_unique = unique("source"), unique("target")
    if source_unique and target_unique:
        return "one_to_one"
    if target_unique:
        return "many_to_one"
    if source_unique:
        return "one_to_many"
    return None
