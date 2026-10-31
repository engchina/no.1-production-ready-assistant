"""登録済み実装へ渡す Profile 制約付き能力。接続・任意 cursor を公開しない。"""

from __future__ import annotations

from typing import Any

from .ontology_definition_data_validation import quote_identifier
from .ontology_service import OntologyVersionConflictError


class ReadOnlyFunctionContext:
    def __init__(self, service: Any, profile_id: str, release_id: str, max_rows: int) -> None:
        self._service, self._profile_id, self._release_id = service, profile_id, release_id
        self.max_rows = max_rows

    def select(self, sql: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
        from .ontology_capabilities import checked_capability_sql

        _, bundle = self._service._release(self._profile_id, self._release_id)
        checked = checked_capability_sql(
            sql,
            bundle,
            self._service._schema(self._profile_id),
            expression=False,
            parameter_names=set(parameters),
        )
        with (
            self._service._adapter().user_data_connection() as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(checked, parameters)
            return list(self._service._rows(cursor, self.max_rows))


class ActionExecutionContext:
    def __init__(
        self,
        cursor: Any,
        plan: dict[str, Any],
        target: dict[str, Any],
        affected_properties: list[str],
    ) -> None:
        self._cursor, self._plan, self._target = cursor, plan, target
        self._allowed = set(affected_properties)

    def update(self, values: dict[str, Any]) -> None:
        from .ontology_capabilities import typed_value

        if (
            not values
            or not set(values) <= self._allowed
            or any(
                name not in self._plan["columns"] or name in self._plan["keys"] for name in values
            )
        ):
            raise ValueError("登録実装の更新範囲が Action の契約外です。")
        for name, value in values.items():
            rule = self._plan["types"][name]
            typed_value(value, rule["kind"], rule["required"])
            if not rule["writable"] or (
                rule["allowed_values"] and value not in rule["allowed_values"]
            ):
                raise ValueError("プロパティは更新不可、または値が列挙範囲外です。")
        setters = ", ".join(
            f"{quote_identifier(self._plan['columns'][name])} = :v{i}"
            for i, name in enumerate(values)
        )
        predicates = " AND ".join(
            f"{quote_identifier(self._plan['columns'][name])} = :k{i}"
            for i, name in enumerate(self._plan["keys"])
        )
        binds = {
            f"v{i}": typed_value(
                value, self._plan["types"][name]["kind"], self._plan["types"][name]["required"]
            )
            for i, (name, value) in enumerate(values.items())
        }
        binds.update(
            {
                f"k{i}": typed_value(self._target[name], self._plan["types"][name]["kind"], True)
                for i, name in enumerate(self._plan["keys"])
            }
        )
        # 固定された Profile mapping の識別子のみ。入力値は全て bind。
        sql = f"UPDATE {self._plan['table']} SET {setters} WHERE {predicates}"  # nosec B608
        self._cursor.execute(sql, binds)
        if self._cursor.rowcount != 1:
            raise OntologyVersionConflictError(
                "OBJECT_VERSION_CHANGED", "更新対象が一意ではありません。"
            )
