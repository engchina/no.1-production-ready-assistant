"""Profile 業務定義の静的検査。Oracle SQL AST と契約を検査し、SQL は実行しない。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import sqlglot
from rdflib import Literal
from rdflib.namespace import XSD
from sqlglot import exp
from sqlglot.optimizer.scope import build_scope

from .object_identity import object_match_key
from .ontology_definition_quality import inspect_definition_quality
from .ontology_definitions import (
    ActionTypeDefinition,
    BusinessDefinition,
    BusinessEventDefinition,
    DefinitionFinding,
    DefinitionMapping,
    InterfaceDefinition,
    ObjectSetDefinition,
    ObjectTypeDefinition,
    ProfileOntologyBundle,
    PropertyDefinition,
)
from .ontology_graph_semantics import XSD_TYPES
from .ontology_link_keys import link_join_check, schema_tables, suggest_link_join
from .ontology_sql_validation import (
    canonical_expression,
    expression_in_query,
    token_identifier,
    validated_sql,
)
from .ontology_terms import VALUE_LABELS


def mapping_object_key(mapping: DefinitionMapping) -> str:
    """物理マッピングの `OWNER.OBJECT` 照合キー。

    mapping は SQL と同じ表記（引用なしは大文字、`"Mixed_Case"` は大文字小文字を保持）で解釈する。
    大文字化すると `SALES."Mixed_Case"` の定義が大文字の同名表 `SALES.MIXED_CASE` にも一致する
    （#573）。引用が不要な名前は従来のキー（大文字）と同じ値になる。
    """

    return f"{object_match_key(mapping.owner)}.{object_match_key(mapping.object_name)}"


def mapping_column_key(mapping: DefinitionMapping) -> str:
    """物理マッピングの列の照合 token（`mapping_object_key` と同じ規則）。"""

    return object_match_key(mapping.column_name)


def is_whole_object_column(column_name: str) -> bool:
    """列なし（表・ビュー全体）の対応。AI が `*` と書いた列も列なしとして扱う（#1720）。"""

    return column_name.strip() in {"", "*"}


def schema_objects(payload: dict[str, Any]) -> dict[str, set[str]]:
    """AI 構築 schema context の object → 列を、`object_identity` の照合キーで返す。"""

    return {
        f"{object_match_key(str(obj.get('owner', '')))}."
        f"{object_match_key(str(obj.get('object_name', '')))}": {
            object_match_key(
                str(
                    col.get("column_name", col.get("column", col.get("name", "")))
                    if isinstance(col, dict)
                    else col
                )
            )
            for col in obj.get("columns", [])
        }
        for obj in payload.get("objects", [])
    }


def checked_expression(sql: str, *, query: bool = False) -> exp.Expression:
    statements = sqlglot.parse(sql, read="oracle")
    if len(statements) != 1 or statements[0] is None:
        raise ValueError("単一の Oracle SQL 式が必要です。")
    tree = statements[0]
    if query and not isinstance(tree, exp.Select):
        raise ValueError("読み取り専用 SELECT が必要です。")
    if any(
        isinstance(node, (exp.DDL, exp.DML, exp.Command, exp.Into, exp.Lock))
        for node in tree.walk()
    ):
        raise ValueError("更新・DDL・ロックは許可されません。")
    # UDF/package/network functions require registered implementations, never AI SQL execution.
    if any(isinstance(node, exp.Anonymous) for node in tree.walk()):
        raise ValueError("未登録の SQL 関数は使用できません。")
    return cast(exp.Expression, tree)


def definition_expressions(
    item: BusinessDefinition,
    definitions: dict[str, BusinessDefinition],
    physical: dict[str, set[str]],
) -> Iterator[tuple[str, str, dict[str, set[str]]]]:
    owner = definitions.get(getattr(item, "object_type", ""), item)
    mappings = item.mappings or owner.mappings
    keys = {mapping_object_key(m) for m in mappings}
    local = {key: physical[key] for key in keys if key in physical} if mappings else physical
    for field in ("expression_sql", "filter_sql", "predicate_sql", "join_expression_sql"):
        yield field, getattr(item, field, ""), local
    for index, mapping in enumerate(item.mappings):
        key = mapping_object_key(mapping)
        yield (
            f"mappings.{index}.expression_sql",
            mapping.expression_sql,
            ({key: physical[key]} if key in physical else {}),
        )


def validated_definition_expression(
    item: BusinessDefinition,
    field: str,
    sql: str,
    local: dict[str, set[str]],
    physical: dict[str, set[str]],
) -> exp.Expression:
    tree = checked_expression(sql)
    if isinstance(tree, exp.Query):
        return validated_sql(tree, physical)
    if item.kind == "metric" and field == "filter_sql" and item.expression_sql:
        query = checked_expression(item.expression_sql)
        if isinstance(query, exp.Query):
            return expression_in_query(tree, query, physical)
    return validated_sql(tree, local)


def _type_family(value: str) -> str:
    """業務の型の比較のキー（整数は数値の一部。型名の大文字小文字は区別しない）。"""

    text = value.strip().casefold()
    return "number" if text in {"integer", "number"} else text


def _label(definitions: dict[str, BusinessDefinition], name: str) -> str:
    """参照先の表示名（無ければ API 名）。"""

    target = definitions.get(name)
    return target.name_ja if target is not None else name


def interface_contracts(
    definitions: dict[str, BusinessDefinition], name: str
) -> list[InterfaceDefinition]:
    pending, seen, result = [name], set(), []
    while pending:
        current = pending.pop()
        definition = definitions.get(current)
        if current in seen or not isinstance(definition, InterfaceDefinition):
            continue
        seen.add(current)
        result.append(definition)
        pending.extend(definition.extends)
    return result


def validate_definitions(
    bundle: ProfileOntologyBundle, schema: dict[str, Any]
) -> list[DefinitionFinding]:
    definitions = {item.api_name: item for item in bundle.definitions}
    physical = schema_objects(schema)
    tables = schema_tables(schema)
    findings = inspect_definition_quality(bundle.definitions, bundle.sources, physical)
    for item in bundle.definitions:

        def error(code: str, field: str, message: str, definition_id: str = item.id) -> None:
            findings.append(
                DefinitionFinding(
                    code=code,
                    field=field,
                    message_ja=message,
                    severity="error",
                    definition_id=definition_id,
                )
            )

        if sum(d.api_name == item.api_name for d in bundle.definitions) > 1:
            error(
                "DUPLICATE_NAME",
                "api_name",
                f"同じ API 名（{item.api_name}）の定義が 2 つあります。"
                "Markdown で片方の見出しの API 名を変えるか、1 つにまとめてください。",
            )
        if not item.id or sum(d.id == item.id for d in bundle.definitions) > 1:
            error(
                "DUPLICATE_DEFINITION_ID",
                "id",
                "定義を見分ける内部の番号が重なりました。"
                "もう一度「オントロジーを公開」を押して確認し直してください。",
            )
        if isinstance(item, PropertyDefinition):
            for field in ("value_type", "shared_property"):
                target = definitions.get(getattr(item, field, ""))
                if (
                    isinstance(target, PropertyDefinition)
                    and target.data_type
                    and item.data_type
                    and _type_family(target.data_type) != _type_family(item.data_type)
                ):
                    error(
                        "PROPERTY_DATA_TYPE_MISMATCH",
                        field,
                        f"『{target.name_ja}』と業務の型が違います。"
                        "どちらかの「業務の型」を直してそろえてください。",
                    )
            values = item.allowed_values
            data_type = item.data_type
            value_field = "allowed_values"
        elif item.kind == "enumeration":
            values = [member.code for member in item.values]
            prop = definitions.get(item.property)
            data_type = getattr(prop, "data_type", "")
            value_field = "values"
            if len(set(values)) != len(values):
                error(
                    "ENUM_CODE_DUPLICATE",
                    "values",
                    "列挙値に同じ値が 2 回あります。重なった値を 1 つ削除してください。",
                )
            if any(not member.code or not member.label_ja for member in item.values):
                error(
                    "ENUM_MEMBER_INCOMPLETE",
                    "values",
                    "列挙値に値か表示名の無いものがあります。値と表示名の両方を書いてください。",
                )
        else:
            values, data_type, value_field = [], "", ""
        if data_type in XSD_TYPES:
            for value in values:
                if Literal(value, datatype=XSD[XSD_TYPES[data_type]]).ill_typed:
                    error(
                        "ALLOWED_VALUE_TYPE_INVALID",
                        value_field,
                        f"許可する値 {value!r} が業務の型"
                        f"（{VALUE_LABELS.get(data_type, data_type)}）に合いません。"
                        "値か業務の型を直してください。",
                    )
        refs: list[tuple[str, str, set[str]]] = []
        if isinstance(item, ObjectTypeDefinition):
            refs.extend(
                ("properties", name, {"property"}) for name in {*item.properties, *item.primary_key}
            )
            if item.title_property:
                refs.append(("title_property", item.title_property, {"property"}))
            if not item.mappings:
                error(
                    "OBJECT_MAPPING_REQUIRED",
                    "mappings",
                    "対応する表・ビューが決まっていません。業務プロファイルに表・ビューを足すか、"
                    "Markdown のこのオブジェクト型に「対応する表・列」を書いてください。",
                )
            for name in {
                *item.properties,
                *item.primary_key,
                *([item.title_property] if item.title_property else []),
            }:
                prop = definitions.get(name)
                if isinstance(prop, PropertyDefinition) and prop.object_type != item.api_name:
                    error(
                        "PROPERTY_OWNER_MISMATCH",
                        "title_property" if name == item.title_property else "properties",
                        f"『{prop.name_ja}』は"
                        f"『{_label(definitions, prop.object_type) or '別のオブジェクト型'}』の"
                        "プロパティで、このオブジェクト型の列ではありません。"
                        "このオブジェクト型でも使うなら、このオブジェクト型のプロパティを別に書くか、"
                        "共有プロパティ（Shared Property）にしてください。",
                    )
            for implementation in item.implements:
                refs.append(("implements", implementation.interface, {"interface"}))
                interface = definitions.get(implementation.interface)
                if interface is None or interface.kind != "interface":
                    continue
                bindings = {
                    binding.interface_property: binding.property
                    for binding in implementation.property_mapping
                }
                contracts = interface_contracts(definitions, interface.api_name)
                for requirement in [p for contract in contracts for p in contract.properties]:
                    prop = definitions.get(bindings.get(requirement.api_name, ""))
                    if (
                        not isinstance(prop, PropertyDefinition)
                        or prop.object_type != item.api_name
                        or prop.data_type != requirement.data_type
                        or (requirement.required and not prop.required)
                    ):
                        error(
                            "INTERFACE_PROPERTY_MISMATCH",
                            "implements",
                            f"インターフェース『{interface.name_ja}』が求めるプロパティ"
                            f"『{requirement.name_ja}』を満たしていません。"
                            "対応するプロパティの業務の型・必須を合わせてください。",
                        )
                for name in {name for contract in contracts for name in contract.required_links}:
                    link = definitions.get(name)
                    if (
                        link is None
                        or link.kind != "link_type"
                        or item.api_name not in {link.source, link.target}
                    ):
                        error(
                            "INTERFACE_LINK_MISMATCH",
                            "implements",
                            f"インターフェースが求めるリンク型『{_label(definitions, name)}』が"
                            "ありません。そのリンク型を書くか、インターフェースの指定を外してください。",
                        )
                for name in {name for contract in contracts for name in contract.required_actions}:
                    action = definitions.get(name)
                    if (
                        action is None
                        or action.kind != "action_type"
                        or action.object_type != item.api_name
                    ):
                        error(
                            "INTERFACE_ACTION_MISMATCH",
                            "implements",
                            f"インターフェースが求めるアクション型『{_label(definitions, name)}』が"
                            "ありません。そのアクション型を書くか、インターフェースの指定を外してください。",
                        )
        elif isinstance(item, PropertyDefinition):
            if item.kind == "property":
                refs.append(("object_type", item.object_type, {"object_type"}))
                if not item.mappings:
                    error(
                        "PROPERTY_MAPPING_REQUIRED",
                        "mappings",
                        "対応する列が決まっていません。"
                        "Markdown のこのプロパティに「対応する表・列」を書いてください。",
                    )
            if item.shared_property:
                refs.append(("shared_property", item.shared_property, {"shared_property"}))
            if item.value_type:
                refs.append(("value_type", item.value_type, {"value_type"}))
        elif item.kind == "link_type":
            refs.extend(
                (field, getattr(item, field), {"object_type", "interface"})
                for field in ("source", "target")
            )
            # 一意でない列でつなぐと多対多になりうる。AI 構築は付け替えか外すが、利用者と前の
            # 定義のリンク型は外さず、警告で直してもらう（#1741）。
            check = link_join_check(item, definitions, schema, tables)
            if check.keyed is False:
                # 同じ両端にキーでつなぐ結合のキーがあれば、その列を「どうすればよいか」に書く
                # （改善案にも同じ結合のキーを出す。#1760）。
                fix = suggest_link_join(item, definitions, schema, tables)
                how = (
                    f"行が重なって件数が増えることがあるため、「結合のキー」を "
                    f"{'・'.join(fix.columns)} でつなぐ形（{fix.join_expression_sql}）に"
                    "直してください。"
                    if fix is not None
                    else "行が重なって件数が増えることがあるため、両端の表・ビューが主キー・"
                    "一意キー・外部キーの列で一意につながるかを確かめ、つながる列があれば"
                    "「結合のキー」をその列に直し、無ければこのリンク型を削除してください。"
                )
                findings.append(
                    DefinitionFinding(
                        code="LINK_JOIN_NOT_UNIQUE",
                        field="join_expression_sql",
                        message_ja=(
                            "主キー・一意キーではない列"
                            f"（{'、'.join(check.columns)}）でつないでいます。{how}"
                        ),
                        severity="warning",
                        definition_id=item.id,
                    )
                )
        elif item.kind == "interface":
            refs.extend(("extends", name, {"interface"}) for name in item.extends)
            refs.extend(("required_links", name, {"link_type"}) for name in item.required_links)
            refs.extend(
                ("required_actions", name, {"action_type"}) for name in item.required_actions
            )
            pending, visited = list(item.extends), {item.api_name}
            while pending:
                name = pending.pop()
                if name == item.api_name:
                    error(
                        "INTERFACE_CYCLE",
                        "extends",
                        "インターフェースの継承が循環しています。どこかの継承元を外してください。",
                    )
                    break
                parent = definitions.get(name)
                if name not in visited and parent is not None and parent.kind == "interface":
                    visited.add(name)
                    pending.extend(parent.extends)
        elif isinstance(item, (ActionTypeDefinition, BusinessEventDefinition, ObjectSetDefinition)):
            refs.append(("object_type", str(item.object_type), {"object_type"}))
        elif item.kind == "enumeration":
            refs.append(("property", item.property, {"property", "shared_property"}))
        for field in (
            "dependencies",
            "applies_to",
            "grain",
            "distinct_keys",
            "affected_properties",
        ):
            refs.extend((field, name, set()) for name in getattr(item, field, []))
        for field, name, kinds in refs:
            target = definitions.get(name)
            if target is None or (kinds and target.kind not in kinds):
                error(
                    "REFERENCE_INVALID",
                    field,
                    (
                        f"参照している『{_label(definitions, name)}』が見つかりません。"
                        if name
                        else "参照先が書かれていません。"
                    )
                    + "Markdown にその定義を書くか、参照を削除してください。",
                )
        for field in ("time_property", "timestamp_property"):
            name = getattr(item, field, "")
            if name and (name not in definitions or definitions[name].kind != "property"):
                error(
                    "REFERENCE_INVALID",
                    field,
                    f"時間の項目に指定した『{name}』がプロパティとして見つかりません。"
                    "プロパティの API 名を直してください。",
                )
        for mapping in item.mappings:
            key = mapping_object_key(mapping)
            if key not in physical:
                error(
                    "MAPPING_OUTSIDE_PROFILE",
                    "mappings",
                    f"表・ビュー {key} は業務プロファイルの対象に入っていません。"
                    "業務プロファイルの対象に追加するか、Markdown からこの対応を削除してください。",
                )
            elif not is_whole_object_column(mapping.column_name) and (
                mapping_column_key(mapping) not in physical[key]
            ):
                error(
                    "MAPPING_OUTSIDE_PROFILE",
                    "mappings",
                    f"列 {key}.{mapping.column_name} は表・ビューに無いか、"
                    "業務プロファイルで使えない列です。列名を直すか、"
                    "Markdown からこの対応を削除してください。",
                )
        for field, sql, expression_scope in definition_expressions(item, definitions, physical):
            if not sql:
                continue
            try:
                tree = validated_definition_expression(item, field, sql, expression_scope, physical)
                if item.kind == "metric" and field == "expression_sql":
                    aggregates = list(tree.find_all(exp.AggFunc))
                    if item.aggregation and not any(
                        node.key.lower() == item.aggregation.lower() for node in aggregates
                    ):
                        error(
                            "METRIC_AGGREGATION_MISMATCH",
                            field,
                            "集計方法と式の集計関数が違います。どちらかを直してそろえてください。",
                        )
                    scope = build_scope(tree) if isinstance(tree, exp.Query) else None
                    if item.filter_sql:
                        predicate = checked_expression(item.filter_sql)
                        try:
                            if isinstance(tree, exp.Query):
                                predicate = expression_in_query(predicate, tree, physical)
                            expected_filter = canonical_expression(predicate, physical, scope)
                        except ValueError:
                            expected_filter = ""
                        # SELECT に表示するだけ、または OR の片側だけでは必須フィルタにならない。
                        clauses = [tree.args.get(name) for name in ("where", "having")]
                        pending_conditions: list[exp.Expression] = [
                            clause.this for clause in clauses if clause is not None
                        ]
                        guaranteed = set()
                        while pending_conditions:
                            condition = cast(exp.Expression, pending_conditions.pop().unnest())
                            guaranteed.add(canonical_expression(condition, physical, scope))
                            if isinstance(condition, exp.And):
                                pending_conditions.extend([condition.this, condition.expression])
                        if expected_filter not in guaranteed:
                            error(
                                "METRIC_FILTER_MISMATCH",
                                "filter_sql",
                                "絞り込みの条件が式の WHERE に入っていません。"
                                "式に条件を入れるか、絞り込みの条件を直してください。",
                            )

                    def declared_keys(names: list[str]) -> set[str]:
                        result = set()
                        for name in names:
                            prop = definitions.get(name)
                            if not isinstance(prop, PropertyDefinition) or len(prop.mappings) != 1:
                                raise ValueError(
                                    f"『{_label(definitions, name)}』の対応する列が 1 つに"
                                    "決まりません。"
                                )
                            mapping = prop.mappings[0]
                            value = (
                                checked_expression(mapping.expression_sql)
                                if mapping.expression_sql
                                else exp.column(
                                    token_identifier(mapping.column_name),
                                    table=token_identifier(mapping.object_name),
                                    db=token_identifier(mapping.owner),
                                )
                            )
                            key = mapping_object_key(mapping)
                            local = {key: physical[key]} if key in physical else {}
                            result.add(canonical_expression(value, local))
                        return result

                    if item.distinct_keys:
                        expected = declared_keys(item.distinct_keys)
                        distincts = [
                            node.this
                            for node in aggregates
                            if isinstance(node.this, exp.Distinct)
                            and (
                                not item.aggregation or node.key.lower() == item.aggregation.lower()
                            )
                        ]
                        if not distincts or any(
                            {canonical_expression(e, physical, scope) for e in distinct.expressions}
                            != expected
                            for distinct in distincts
                        ):
                            error(
                                "METRIC_DISTINCT_MISMATCH",
                                "distinct_keys",
                                "式の重複排除（DISTINCT）の列と重複排除のキーが違います。"
                                "どちらかを直してそろえてください。",
                            )
                    if item.grain and isinstance(tree, exp.Query):
                        group = tree.args.get("group")
                        actual = (
                            {canonical_expression(e, physical, scope) for e in group.expressions}
                            if group
                            else set()
                        )
                        if actual != declared_keys(item.grain):
                            error(
                                "METRIC_GRAIN_MISMATCH",
                                "grain",
                                "式の GROUP BY とディメンションが違います。"
                                "どちらかを直してそろえてください。",
                            )
                    if any(True for _ in tree.find_all(exp.Join)) and aggregates:
                        error(
                            "JOIN_GRAIN_REVIEW_REQUIRED",
                            "expression_sql",
                            "結合してから集計する式は、行が重なって数が増えることがあります。"
                            "結合の前に集計するか、ディメンションで重ならないことを確かめてください。",
                        )
            except (ValueError, sqlglot.errors.SqlglotError) as exc:
                error(
                    "SQL_EXPRESSION_INVALID",
                    field,
                    f"SQL の式を確かめられませんでした（{str(exc)[:160]}）。"
                    "Markdown の式を Oracle の SQL に直してください。",
                )
    return findings
