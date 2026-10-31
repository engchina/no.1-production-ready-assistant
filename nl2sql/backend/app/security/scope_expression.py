"""DeepSec の有界条件ツリー。SQL の生成と metadata 検証は同じ木を辿る。"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import Any, cast

from pydantic import ValidationError

from .domain import DataEntitlementRecord
from .schemas import ScopeCondition, ScopeExpression, ScopeGroup, ScopeRelatedExists
from .scope_relations import DependencyPlan
from .service import SecurityApiError


def parse_expression(value: object) -> ScopeExpression:
    try:
        return ScopeExpression.model_validate(value)
    except ValidationError as exc:
        raise SecurityApiError(400, f"条件ツリーが不正です: {exc.errors()[0]['msg']}") from exc


def related_nodes(expression: ScopeExpression) -> Iterator[ScopeRelatedExists]:
    def walk(group: ScopeGroup) -> Iterator[ScopeRelatedExists]:
        for node in group.children:
            if isinstance(node, ScopeGroup):
                yield from walk(node)
            elif isinstance(node, ScopeRelatedExists):
                yield node

    yield from walk(expression.root)


def expression_uses_login(value: object) -> bool:
    expression = parse_expression(value)

    def walk(group: ScopeGroup) -> bool:
        return any(
            (
                node.filter.value_source == "LOGIN_USER_ID"
                if isinstance(node, ScopeCondition)
                else walk(node if isinstance(node, ScopeGroup) else node.condition)
            )
            for node in group.children
        )

    return walk(expression.root)


def compile_expression(
    entitlement: DataEntitlementRecord,
    column_types: Mapping[str, str],
    validate_relation: Callable[[ScopeRelatedExists], Mapping[str, str]] | None = None,
) -> str:
    from .deepsec import _identifier_token, _qualified, _scope_filter_predicate, _scope_value_type

    expression = parse_expression(entitlement.scope_expression)
    target = _qualified(entitlement.target_owner, entitlement.target_object)
    alias_number = 0

    def group_sql(group: ScopeGroup, qualifier: str, types: Mapping[str, str]) -> str:
        nonlocal alias_number
        parts: list[str] = []
        for node in group.children:
            if isinstance(node, ScopeGroup):
                parts.append(group_sql(node, qualifier, types))
            elif isinstance(node, ScopeCondition):
                field = node.filter.to_record()
                data_type = types.get(field.column_name)
                if types and (
                    data_type is None or _scope_value_type(data_type) != field.value_type
                ):
                    raise SecurityApiError(
                        400, f"{qualifier}.{field.column_name}: 列が存在しないか型が一致しません。"
                    )
                parts.append(
                    _scope_filter_predicate(
                        qualifier, field, data_type=data_type or field.value_type
                    )
                )
            else:
                related_target = _qualified(node.target_owner, node.target_object)
                if related_target == target:
                    raise SecurityApiError(400, "自分自身のテーブルは関連条件に指定できません。")
                related_types = (
                    validate_relation(node)
                    if validate_relation
                    else {
                        key.removeprefix(related_target + "."): value
                        for key, value in column_types.items()
                        if key.startswith(related_target + ".")
                    }
                )
                alias_number += 1
                alias = f"dsr{alias_number}"
                keys = [
                    f"{target}.{_identifier_token(key.source_column)} = "
                    f"{alias}.{_identifier_token(key.target_column)}"
                    for key in node.join_keys
                ]
                parts.append(
                    f"EXISTS (SELECT 1 FROM {related_target} {alias} "  # nosec B608 — validated AST
                    f"WHERE {' AND '.join(keys)} AND "
                    f"{group_sql(node.condition, alias, related_types)})"
                )
        return "(" + f" {group.operator} ".join(parts) + ")"

    return group_sql(expression.root, target, column_types)


def validate_expression_metadata(
    service: Any,
    cursor: Any,
    entitlement: DataEntitlementRecord,
    columns: dict[str, str],
    dependency_plan: DependencyPlan | None = None,
) -> None:
    from .deepsec import _qualified, _scope_value_type
    from .scope_relations import relation_catalog, validate_relation_dependency

    target = _qualified(entitlement.target_owner, entitlement.target_object)

    def validate(node: ScopeRelatedExists) -> Mapping[str, str]:
        catalog = relation_catalog(service, node.profile_id, target, cursor=cursor)
        if node.object_scope_version != catalog["object_scope_version"]:
            raise SecurityApiError(
                409, "業務プロファイルの対象範囲が変更されました。関連条件を再選択してください。"
            )
        related_target = _qualified(node.target_owner, node.target_object)
        if related_target not in catalog["objects"]:
            raise SecurityApiError(
                400, "関連テーブルは同じ業務プロファイル内から選択してください。"
            )
        if node.relation_source != "MANUAL":
            matches = [
                item
                for item in catalog["relations"]
                if item["id"] == node.relation_id and item["source"] == node.relation_source
            ]
            if (
                not matches
                or matches[0]["version"] != node.relation_version
                or matches[0]["target"] != related_target
                or matches[0]["join_keys"] != [key.model_dump() for key in node.join_keys]
            ):
                raise SecurityApiError(
                    409, "関連定義が変更されました。関連条件を再選択してください。"
                )
        validate_relation_dependency(cursor, target, related_target, dependency_plan)
        related_entitlement = DataEntitlementRecord(
            entitlement_id="",
            role_id="",
            resource_code=related_target,
            scope_code="*",
            capability="SELECT",
            target_owner=node.target_owner,
            target_object=node.target_object,
            target_type=node.target_type,
            column_names=[key.target_column for key in node.join_keys],
        )
        related_types = service._validate_data_entitlement(cursor, related_entitlement)
        for key in node.join_keys:
            left = columns.get(key.source_column)
            right = related_types.get(key.target_column)
            if (
                not left
                or not right
                or not _scope_value_type(left)
                or _scope_value_type(left) != _scope_value_type(right)
            ):
                raise SecurityApiError(
                    400, f"関連キー {key.source_column} / {key.target_column} の型が一致しません。"
                )
        # control connection は Data Grant owner。ゼロ行 SELECT で実際の参照権限を確認する。
        try:
            cursor.execute(
                f"SELECT 1 FROM {related_target} WHERE 1 = 0"  # nosec B608 — validated identifiers
            )
        except Exception as exc:
            raise SecurityApiError(
                400, f"Data Grant owner に {related_target} の SELECT 権限が必要です。"
            ) from exc
        columns.update({f"{related_target}.{key}": value for key, value in related_types.items()})
        return cast(Mapping[str, str], related_types)

    compile_expression(entitlement, columns, validate)
