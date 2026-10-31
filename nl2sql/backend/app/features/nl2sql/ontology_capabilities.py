"""型付き能力の明示的な binding と、確認に固定された実行。AI コードは実行しない。"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, NoReturn
from uuid import uuid4

from pydantic import Field
from sqlglot import exp

from app.security.domain import Principal
from app.security.permissions import SQL_EXECUTE_PERMISSION
from app.security.request_actor import actor_scope

from .object_identity import object_match_key, object_part_name
from .ontology_definition_data_validation import quote_identifier
from .ontology_definition_service import definition_fingerprint
from .ontology_definition_validation import (
    checked_expression,
    mapping_column_key,
    mapping_object_key,
    schema_objects,
)
from .ontology_definition_workspace import (
    ProfileOntologyWorkspaceService,
    authorize_definition_operation,
)
from .ontology_definitions import (
    ActionTypeDefinition,
    DefinitionContract,
    FunctionDefinition,
    ProfileOntologyBundle,
)
from .ontology_published_context import require_current_scope
from .ontology_service import (
    OntologyGateBlockedError,
    OntologyNotFoundError,
    OntologyVersionConflictError,
)
from .ontology_sql_validation import identifier_token
from .ontology_store import stable_ontology_id

CAPABILITY_MANAGE = "nl2sql.ontology.capabilities.manage"
ACTION_EXECUTE = "nl2sql.ontology.actions.execute"


def refreshed_actor(actor: Principal | None) -> Principal | None:
    from app.settings import get_settings

    settings = get_settings()
    if settings.app_auth_enabled and not settings.local_debug_enabled:
        if actor is None:
            from fastapi import HTTPException

            raise HTTPException(403, "認証が必要です。")
        from app.security.service import get_security_service

        return get_security_service().principal_for_worker(actor.user_uuid)
    return actor


def actor_is_admin(actor: Principal | None) -> bool:
    from app.settings import get_settings

    settings = get_settings()
    return (
        actor.is_system_admin
        if actor
        else (not settings.app_auth_enabled or settings.local_debug_enabled)
    )


class StateRequirement(DefinitionContract):
    property: str
    value: str | int | float | bool | None


class CapabilityBindingRequest(DefinitionContract):
    release_id: str
    kind: Literal["sql", "expression", "backend", "property_update"]
    expression_sql: str = ""
    implementation_key: str = ""
    max_rows: int = Field(default=100, ge=1, le=500)
    state_requirements: list[StateRequirement] = Field(default_factory=list)
    reviewed_rules_ja: str = ""
    enabled: bool = True


class CapabilityCallRequest(DefinitionContract):
    release_id: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    target: dict[str, Any] = Field(default_factory=dict)


class CapabilityExecuteRequest(DefinitionContract):
    preview_id: str
    confirmed: Literal[True]


@dataclass(frozen=True)
class RegisteredFunction:
    # 登録はサーバー code でのみ行う。DB 権限は呼出 actor のまま。
    invoke: Callable[[dict[str, Any], Any], Any]
    return_type: str


@dataclass(frozen=True)
class RegisteredAction:
    # 受け取った transaction 内だけを更新する。外部副作用の登録は許可しない。
    preview: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    execute: Callable[[Any, dict[str, Any], dict[str, Any]], dict[str, Any]]
    failure_policy: Literal["rollback"] = "rollback"


FUNCTION_REGISTRY: dict[str, RegisteredFunction] = {}
ACTION_REGISTRY: dict[str, RegisteredAction] = {}


def register_function(key: str, implementation: RegisteredFunction) -> None:
    if not key or key in FUNCTION_REGISTRY:
        raise ValueError("登録済みまたは空の実装 ID です。")
    FUNCTION_REGISTRY[key] = implementation


def register_action(key: str, implementation: RegisteredAction) -> None:
    if not key or key in ACTION_REGISTRY or implementation.failure_policy != "rollback":
        raise ValueError("transaction 内の rollback 対応実装を登録してください。")
    ACTION_REGISTRY[key] = implementation


def blocked(message: str) -> NoReturn:
    raise OntologyGateBlockedError("CAPABILITY_CONFIGURATION_REQUIRED", message)


def typed_value(value: Any, kind: str, required: bool) -> Any:
    if value is None:
        if required:
            raise ValueError("必須パラメーターがありません。")
        return None
    valid = {
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "number": type(value) in (int, float) and math.isfinite(value),
        "boolean": type(value) is bool,
        "object": isinstance(value, dict),
    }
    if kind == "date" and type(value) is date:
        return value
    if kind == "datetime" and isinstance(value, datetime):
        return value
    if kind in ("date", "datetime"):
        if not isinstance(value, str):
            raise ValueError("日時は ISO 形式で指定してください。")
        return date.fromisoformat(value) if kind == "date" else datetime.fromisoformat(value)
    if not valid.get(kind, False):
        raise ValueError(f"パラメーターの型が {kind} と一致しません。")
    return value


def parameters_for(
    definition: FunctionDefinition | ActionTypeDefinition, values: dict[str, Any]
) -> dict[str, Any]:
    if set(values) - {p.api_name for p in definition.parameters}:
        raise ValueError("未定義のパラメーターがあります。")
    return {
        p.api_name: typed_value(values.get(p.api_name), p.data_type, p.required)
        for p in definition.parameters
    }


def checked_capability_sql(
    sql: str,
    bundle: ProfileOntologyBundle,
    schema: dict[str, set[str]],
    *,
    expression: bool,
    parameter_names: set[str],
) -> str:
    tree = checked_expression(sql, query=not expression)
    if any(not isinstance(star.parent, exp.Count) for star in tree.find_all(exp.Star)) or tree.find(
        exp.Hint
    ):
        raise ValueError("列名を明示し、実行 hint を含めないでください。")
    if expression and (tree.find(exp.Select) or tree.find(exp.Table) or tree.find(exp.Column)):
        raise ValueError("宣言式はパラメーターと組込演算だけで定義してください。")
    if {p.name for p in tree.find_all(exp.Placeholder)} - parameter_names:
        raise ValueError("未定義の SQL bind パラメーターがあります。")
    tables = list(tree.find_all(exp.Table))

    def table_key(table: exp.Table) -> str:
        # 引用規則の照合キー。大文字化すると `SALES."Mixed_Case"` だけの Profile で大文字の
        # 同名表 `SALES.MIXED_CASE` も範囲内になる（#573）。
        return f"{identifier_token(table.args.get('db'))}.{identifier_token(table.this)}"

    for table in tables:
        if table.catalog or table_key(table) not in schema:
            raise ValueError("SQL の物理参照が業務プロファイルの範囲外です。")
    # 曖昧な列を実行へ渡さない。サブクエリ由来列は初版では明示的 backend 実装へ。
    aliases = {
        identifier_token(t.args["alias"].this if t.args.get("alias") else t.this): schema[
            table_key(t)
        ]
        for t in tables
    }
    for column in tree.find_all(exp.Column):
        qualifier = identifier_token(column.args.get("table"))
        name = identifier_token(column.this)
        candidates = [
            cols for alias, cols in aliases.items() if not qualifier or alias == qualifier
        ]
        if sum(name in cols for cols in candidates) != 1:
            raise ValueError("未許可または曖昧な列参照があります。")
    rendered = tree.sql(dialect="oracle")
    return (
        exp.select(tree.as_("RESULT")).from_("DUAL").sql(dialect="oracle")
        if expression
        else rendered
    )


class ProfileOntologyCapabilityService(ProfileOntologyWorkspaceService):
    def _release(
        self, profile_id: str, release_id: str
    ) -> tuple[dict[str, Any], ProfileOntologyBundle]:
        release = self.release(profile_id, release_id)
        if not release or self.head(profile_id)["release_id"] != release_id:
            raise OntologyVersionConflictError(
                "CAPABILITY_RELEASE_CHANGED", "公開版が変わりました。最新の版で再確認してください。"
            )
        return release, require_current_scope(self.runtime, profile_id, release)

    def _definition(
        self, profile_id: str, release_id: str, definition_id: str
    ) -> tuple[ProfileOntologyBundle, FunctionDefinition | ActionTypeDefinition]:
        _, bundle = self._release(profile_id, release_id)
        definition = next((d for d in bundle.definitions if d.id == definition_id), None)
        if not isinstance(definition, (FunctionDefinition, ActionTypeDefinition)):
            raise OntologyNotFoundError(
                "CAPABILITY_NOT_FOUND", "この業務プロファイルの能力定義が見つかりません。"
            )
        return bundle, definition

    def binding(self, profile_id: str, definition_id: str) -> dict[str, Any] | None:
        self._session(profile_id)
        identity = stable_ontology_id("ontology_binding", profile_id, definition_id)
        record = self.store.get_artifact(identity)
        if record is None:
            return None
        record = self.document(profile_id, identity, "ontology_capability_binding")
        return {**json.loads(record["content"]), "etag": record["etag"], "artifact_id": identity}

    def _schema(self, profile_id: str) -> dict[str, set[str]]:
        prepared = self.runtime.prepare_build_schema_context(profile_id)
        if prepared.errors:
            blocked("Schema を取得できません。")
        return schema_objects(json.loads(str(prepared.schema_context)))

    def _plan(
        self,
        bundle: ProfileOntologyBundle,
        definition: ActionTypeDefinition,
        binding: CapabilityBindingRequest,
    ) -> dict[str, Any]:
        items = {d.api_name: d for d in bundle.definitions}
        obj = items.get(definition.object_type)
        if (
            obj is None
            or obj.kind != "object_type"
            or len(obj.mappings) != 1
            or not obj.primary_key
        ):
            blocked("更新対象には単一ソースと明示的な主キーが必要です。")
        mapping = obj.mappings[0]
        if mapping.expression_sql:
            blocked("複合・計算ソースの直接更新には対応していません。")
        schema = self._schema(bundle.profile_id)
        table = mapping_object_key(mapping)
        source_objects = json.loads(
            str(self.runtime.prepare_build_schema_context(bundle.profile_id).schema_context)
        )["objects"]
        if not any(
            f"{object_match_key(str(obj.get('owner', '')))}."
            f"{object_match_key(str(obj.get('object_name', '')))}"
            == table
            and obj.get("object_type") == "table"
            for obj in source_objects
        ):
            blocked("初期の属性更新は物理テーブルの行バージョンを必要とします。")
        columns: dict[str, str] = {}
        for name in obj.properties:
            prop = items.get(name)
            if prop and prop.kind == "property" and len(prop.mappings) == 1:
                m = prop.mappings[0]
                if (
                    mapping_object_key(m) == table
                    and mapping_column_key(m) in schema.get(table, set())
                    and not m.expression_sql
                ):
                    # 実行 SQL では `"..."` で囲むため、カタログ上の名前（`"Amount"` → Amount、
                    # `amount` → AMOUNT）にする。
                    columns[name] = object_part_name(m.column_name)
        if any(key not in columns for key in obj.primary_key):
            blocked("主キーの物理列 binding が必要です。")
        updates: dict[str, str] = {}
        for assignment in definition.assignments:
            prop = items.get(assignment.property)
            param = next(
                (p for p in definition.parameters if p.api_name == assignment.parameter), None
            )
            if (
                not prop
                or prop.kind != "property"
                or not prop.writable
                or assignment.property in obj.primary_key
                or assignment.property not in columns
                or param is None
                or param.data_type != prop.data_type
            ):
                blocked("更新可能なプロパティと同じ型のパラメーターを明示的に対応付けてください。")
            if prop.required and not param.required:
                blocked("必須プロパティには必須パラメーターが必要です。")
            updates[assignment.property] = assignment.parameter
        if binding.kind == "property_update" and (
            not updates or set(updates) != set(definition.affected_properties)
        ):
            blocked("影響範囲と更新プロパティの対応を確定してください。")
        if any(rule.property not in columns for rule in binding.state_requirements):
            blocked("状態制約に未定義のプロパティがあります。")
        if (
            not binding.reviewed_rules_ja.strip()
            or not definition.permission_requirement_ja
            or not definition.failure_policy_ja
        ):
            blocked("操作権限・失敗処理・業務条件の確認が必要です。")
        if (
            definition.preconditions_ja
            and not binding.state_requirements
            and binding.kind != "backend"
        ):
            blocked("業務の前提条件を状態制約へ binding してください。")
        return {
            "table": (
                f"{quote_identifier(object_part_name(mapping.owner))}."
                f"{quote_identifier(object_part_name(mapping.object_name))}"
            ),
            "columns": columns,
            "keys": obj.primary_key,
            "updates": updates,
            "object_type": obj.api_name,
            "types": {
                name: {
                    "kind": getattr(items[name], "data_type", "string"),
                    "required": getattr(items[name], "required", False),
                    "writable": getattr(items[name], "writable", False),
                    "allowed_values": getattr(items[name], "allowed_values", []),
                }
                for name in columns
            },
        }

    def check_binding(
        self,
        bundle: ProfileOntologyBundle,
        definition: FunctionDefinition | ActionTypeDefinition,
        binding: CapabilityBindingRequest,
    ) -> dict[str, Any]:
        if definition.missing_information_ja or definition.review_status != "reviewed":
            blocked("未確定事項の解消と定義のレビューが必要です。")
        if isinstance(definition, FunctionDefinition):
            if not definition.return_type:
                blocked("関数の戻り値型を定義してください。")
            if binding.kind in ("sql", "expression"):
                return {
                    "sql": checked_capability_sql(
                        binding.expression_sql,
                        bundle,
                        self._schema(bundle.profile_id),
                        expression=binding.kind == "expression",
                        parameter_names={p.api_name for p in definition.parameters},
                    )
                }
            if binding.kind != "backend" or binding.implementation_key not in FUNCTION_REGISTRY:
                blocked("登録済みの関数実装が必要です。")
            if FUNCTION_REGISTRY[binding.implementation_key].return_type != definition.return_type:
                blocked("登録実装の戻り値型が一致しません。")
            return {}
        if binding.kind not in ("property_update", "backend"):
            blocked("操作には属性更新または登録済み業務処理を binding してください。")
        if binding.kind == "backend" and binding.implementation_key not in ACTION_REGISTRY:
            blocked("transaction と失敗処理を持つ登録済み操作実装が必要です。")
        return self._plan(bundle, definition, binding)

    def bind(
        self,
        profile_id: str,
        definition_id: str,
        request: CapabilityBindingRequest,
        etag: str,
        actor: Principal | None,
    ) -> dict[str, Any]:
        actor = refreshed_actor(actor)
        who = authorize_definition_operation(profile_id, actor, CAPABILITY_MANAGE)
        bundle, definition = self._definition(profile_id, request.release_id, definition_id)
        if request.enabled:
            self.check_binding(bundle, definition, request)
            self._adapter()
        current = self.binding(profile_id, definition_id)
        if etag.strip('"') != (current["etag"] if current else "*"):
            raise OntologyVersionConflictError(
                "BINDING_CHANGED", "binding が更新されました。最新情報を取得してください。"
            )
        head = self.head(profile_id)
        identity = stable_ontology_id("ontology_binding", profile_id, definition_id)
        value = {
            **request.model_dump(mode="json"),
            "definition_id": definition_id,
            "actor": who,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        # head と binding を同一 transaction で CAS。公開変更との競合を避ける。
        head_record = self.document(
            profile_id, stable_ontology_id("ontology_head", profile_id), "ontology_published_head"
        )
        self.store.save_documents_atomic(
            "artifacts",
            [
                (head_record, head["etag"]),
                (
                    self.artifact(profile_id, identity, "ontology_capability_binding", value),
                    current["etag"] if current else None,
                ),
                (
                    self.artifact(
                        profile_id,
                        f"ontology_binding_audit_{uuid4().hex}",
                        "ontology_capability_audit",
                        value,
                    ),
                    None,
                ),
            ],
        )
        result = self.binding(profile_id, definition_id)
        if result is None:
            raise RuntimeError("binding の保存を確認できません。")
        return result

    def catalog(self, profile_id: str) -> dict[str, Any]:
        release = self.release(profile_id)
        if not release:
            return {
                "release_id": "",
                "capabilities": [],
                "implementations": {
                    "functions": sorted(FUNCTION_REGISTRY),
                    "actions": sorted(ACTION_REGISTRY),
                },
            }
        result = []
        for definition in ProfileOntologyBundle.model_validate(release["bundle"]).definitions:
            if not isinstance(definition, (FunctionDefinition, ActionTypeDefinition)):
                continue
            binding = self.binding(profile_id, definition.id)
            reason = "実装 binding を設定してください。"
            status = "configuration_required"
            try:
                bundle, _ = self._definition(profile_id, release["id"], definition.id)
                if binding and binding["release_id"] == release["id"] and binding["enabled"]:
                    self.check_binding(bundle, definition, self._binding_request(binding))
                    self._adapter()
                    status, reason = "available", ""
            except (OntologyGateBlockedError, OntologyVersionConflictError, ValueError) as exc:
                reason = str(exc)
            result.append(
                {
                    "definition": definition.model_dump(mode="json"),
                    "target_parameters": [
                        {
                            "api_name": prop.api_name,
                            "name_ja": prop.name_ja,
                            "data_type": prop.data_type,
                            "required": True,
                        }
                        for obj in ProfileOntologyBundle.model_validate(
                            release["bundle"]
                        ).definitions
                        if isinstance(definition, ActionTypeDefinition)
                        and obj.kind == "object_type"
                        and obj.api_name == definition.object_type
                        for prop in ProfileOntologyBundle.model_validate(
                            release["bundle"]
                        ).definitions
                        if prop.kind == "property" and prop.api_name in obj.primary_key
                    ],
                    "binding": binding,
                    "status": status,
                    "reason_ja": reason,
                }
            )
        return {
            "release_id": release["id"],
            "capabilities": result,
            "implementations": {
                "functions": sorted(FUNCTION_REGISTRY),
                "actions": sorted(ACTION_REGISTRY),
            },
        }

    @staticmethod
    def _binding_request(binding: dict[str, Any]) -> CapabilityBindingRequest:
        return CapabilityBindingRequest.model_validate(
            {k: v for k, v in binding.items() if k in CapabilityBindingRequest.model_fields}
        )

    def _ready(
        self,
        profile_id: str,
        definition_id: str,
        request: CapabilityCallRequest,
        actor: Principal | None,
    ) -> tuple[Any, Any, Any, str]:
        bundle, definition = self._definition(profile_id, request.release_id, definition_id)
        who = authorize_definition_operation(
            profile_id,
            actor,
            (
                SQL_EXECUTE_PERMISSION
                if isinstance(definition, FunctionDefinition)
                else ACTION_EXECUTE
            ),
        )
        binding = self.binding(profile_id, definition_id)
        if not binding or not binding["enabled"] or binding["release_id"] != request.release_id:
            blocked("現在の公開版の実行 binding が必要です。")
        plan = self.check_binding(bundle, definition, self._binding_request(binding))
        return definition, binding, plan, who

    def _adapter(self) -> Any:
        adapter = getattr(self.runtime.legacy_service, "_oracle_adapter", None)
        if adapter is None or (
            callable(getattr(adapter, "is_configured", None)) and not adapter.is_configured()
        ):
            blocked("Oracle 接続が設定されていません。")
        return adapter

    @staticmethod
    def _rows(cursor: Any, max_rows: int) -> list[dict[str, Any]]:
        from fastapi.encoders import jsonable_encoder

        columns = [str(c[0]) for c in cursor.description]
        return [
            jsonable_encoder(dict(zip(columns, row, strict=True)))
            for row in cursor.fetchmany(max_rows)
        ]

    def invoke(
        self,
        profile_id: str,
        definition_id: str,
        request: CapabilityCallRequest,
        key: str,
        actor: Principal | None,
    ) -> dict[str, Any]:
        actor = refreshed_actor(actor)
        definition, binding, plan, who = self._ready(profile_id, definition_id, request, actor)
        if not isinstance(definition, FunctionDefinition) or request.target:
            blocked("関数呼出の入力が不正です。")
        params = parameters_for(definition, request.parameters)
        if not key.strip():
            raise ValueError("Idempotency-Key が必要です。")
        identity = stable_ontology_id("ontology_function_call", profile_id, who, key)
        request_hash = definition_fingerprint(
            {
                "request": request.model_dump(mode="json"),
                "definition_id": definition_id,
                "binding_etag": binding["etag"],
            }
        )
        # DB 変更のない関数。競合時も保存された最初の結果だけを返す。
        existing = self.store.get_artifact(identity)
        if existing:
            return self._replay(profile_id, identity, "ontology_function_call", who, request_hash)
        with actor_scope(who, is_system_admin=actor_is_admin(actor)):
            if binding["kind"] == "backend":
                from .ontology_capability_context import ReadOnlyFunctionContext

                value = FUNCTION_REGISTRY[binding["implementation_key"]].invoke(
                    params,
                    ReadOnlyFunctionContext(
                        self, profile_id, request.release_id, binding["max_rows"]
                    ),
                )
            else:
                with (
                    self._adapter().user_data_connection() as connection,
                    connection.cursor() as cursor,
                ):
                    used = {
                        p.name
                        for p in checked_expression(plan["sql"], query=True).find_all(
                            exp.Placeholder
                        )
                    }
                    cursor.execute(
                        plan["sql"], {name: value for name, value in params.items() if name in used}
                    )
                    value = self._rows(cursor, binding["max_rows"])
                    if binding["kind"] == "expression":
                        value = value[0]["RESULT"] if value else None
                    elif definition.return_type in (
                        "string",
                        "integer",
                        "number",
                        "boolean",
                        "date",
                        "datetime",
                    ):
                        if len(value) != 1 or len(value[0]) != 1:
                            raise ValueError("スカラー戻り値には単一行・単一列が必要です。")
                        value = next(iter(value[0].values()))
        if definition.return_type in (
            "string",
            "integer",
            "number",
            "boolean",
            "date",
            "datetime",
            "object",
        ):
            typed_value(value, definition.return_type, True)
        result = {
            "id": identity,
            "profile_id": profile_id,
            "definition_id": definition_id,
            "release_id": request.release_id,
            "binding_etag": binding["etag"],
            "actor": who,
            "request_hash": request_hash,
            "parameters": request.parameters,
            "result": value,
            "status": "succeeded",
            "at": datetime.now(UTC).isoformat(),
        }
        self._ready(profile_id, definition_id, request, actor)
        try:
            self.store.save_artifact(
                self.artifact(profile_id, identity, "ontology_function_call", result)
            )
        except Exception:
            if self.store.get_artifact(identity):
                return self._replay(
                    profile_id, identity, "ontology_function_call", who, request_hash
                )
            raise
        return result

    def _replay(
        self, profile_id: str, identity: str, kind: str, who: str, request_hash: str
    ) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(self.document(profile_id, identity, kind)["content"])
        if value.get("actor") != who or value.get("request_hash") != request_hash:
            raise OntologyVersionConflictError(
                "IDEMPOTENCY_KEY_REUSED", "同じキーが別の操作に使用されています。"
            )
        return value

    def _read_target(
        self,
        cursor: Any,
        plan: dict[str, Any],
        target: dict[str, Any],
        *,
        lock: bool = False,
        version: bool = False,
    ) -> dict[str, Any]:
        if set(target) != set(plan["keys"]) or any(value is None for value in target.values()):
            raise ValueError("対象オブジェクトの主キーをすべて指定してください。")
        target = {
            name: typed_value(value, plan["types"][name]["kind"], True)
            for name, value in target.items()
        }
        names = list(plan["columns"])
        fields = ", ".join(quote_identifier(plan["columns"][name]) for name in names)
        scn_alias = "NL2SQL_ONT_ROW_SCN"
        while scn_alias in plan["columns"].values():
            scn_alias += "_"
        if version:
            fields += f", ORA_ROWSCN AS {quote_identifier(scn_alias)}"
        predicates = " AND ".join(
            f"{quote_identifier(plan['columns'][name])} = :k{i}"
            for i, name in enumerate(plan["keys"])
        )
        # Profile の検証済み識別子を quote し、対象値は bind に分離する。
        sql = f"SELECT {fields} FROM {plan['table']} WHERE {predicates}"  # nosec B608
        cursor.execute(
            sql + (" FOR UPDATE WAIT 5" if lock else ""),
            {f"k{i}": target[name] for i, name in enumerate(plan["keys"])},
        )
        rows = self._rows(cursor, 2)
        if len(rows) != 1:
            blocked("対象が存在しない、アクセスできない、または主キーが一意ではありません。")
        result = {name: rows[0][plan["columns"][name]] for name in names}
        for name in names:
            if plan["types"][name]["kind"] == "date" and isinstance(result[name], str):
                result[name] = result[name].split("T")[0]
        if version:
            if rows[0].get(scn_alias) is None:
                blocked("対象行のバージョンを確認できません。")
            result["__row_scn"] = rows[0][scn_alias]
        return result

    @staticmethod
    def _state(before: dict[str, Any], binding: dict[str, Any]) -> None:
        if any(before[r["property"]] != r["value"] for r in binding["state_requirements"]):
            blocked("対象の現在状態ではこの操作を実行できません。")

    def preview(
        self,
        profile_id: str,
        definition_id: str,
        request: CapabilityCallRequest,
        key: str,
        actor: Principal | None,
    ) -> dict[str, Any]:
        actor = refreshed_actor(actor)
        definition, binding, plan, who = self._ready(profile_id, definition_id, request, actor)
        if not isinstance(definition, ActionTypeDefinition):
            blocked("操作定義を選択してください。")
        params = parameters_for(definition, request.parameters)
        if not key.strip():
            raise ValueError("Idempotency-Key が必要です。")
        identity = stable_ontology_id("ontology_action_preview", profile_id, who, key)
        request_hash = definition_fingerprint(
            {
                "request": request.model_dump(mode="json"),
                "definition_id": definition_id,
                "binding_etag": binding["etag"],
            }
        )
        if self.store.get_artifact(identity):
            return self._replay(profile_id, identity, "ontology_action_preview", who, request_hash)
        with (
            actor_scope(who, is_system_admin=actor_is_admin(actor)),
            self._adapter().user_data_connection() as connection,
            connection.cursor() as cursor,
        ):
            before = self._read_target(cursor, plan, request.target, version=True)
        row_scn = before.pop("__row_scn")
        self._state(before, binding)
        for name, parameter in plan["updates"].items():
            rule = plan["types"][name]
            value = request.parameters.get(parameter)
            typed_value(value, rule["kind"], rule["required"])
            if rule["allowed_values"] and value not in rule["allowed_values"]:
                raise ValueError("更新値が許可された列挙値に含まれていません。")
        after = {
            **before,
            **{name: request.parameters.get(param) for name, param in plan["updates"].items()},
        }
        if binding["kind"] == "backend":
            after = ACTION_REGISTRY[binding["implementation_key"]].preview(before, params)
        if set(after) != set(before) or any(
            after[name] != before[name] and name not in definition.affected_properties
            for name in before
        ):
            blocked("実装の影響範囲が定義と一致しません。")
        result = {
            "id": identity,
            "profile_id": profile_id,
            "definition_id": definition_id,
            "release_id": request.release_id,
            "binding_etag": binding["etag"],
            "head_etag": self.head(profile_id)["etag"],
            "actor": who,
            "request_hash": request_hash,
            "request": request.model_dump(mode="json"),
            "before": before,
            "after": after,
            "object_version": definition_fingerprint({"values": before, "row_scn": row_scn}),
            "row_scn": row_scn,
            "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
            "created_at": datetime.now(UTC).isoformat(),
        }
        self.store.save_artifact(
            self.artifact(profile_id, identity, "ontology_action_preview", result)
        )
        return result

    def outcome(
        self, profile_id: str, definition_id: str, preview_id: str, actor: Principal | None
    ) -> dict[str, Any]:
        """応答喪失後の読み取り専用照会。公開版変更後も元の実行結果を参照する。"""
        who = authorize_definition_operation(profile_id, refreshed_actor(actor), ACTION_EXECUTE)
        preview = json.loads(
            self.document(profile_id, preview_id, "ontology_action_preview")["content"]
        )
        if preview["actor"] != who or preview["definition_id"] != definition_id:
            blocked("この操作の確認は現在のユーザーに属していません。")
        identity = stable_ontology_id("ontology_action_execution", profile_id, preview_id)
        if self.store.get_artifact(identity):
            result = self._replay(
                profile_id,
                identity,
                "ontology_action_execution",
                who,
                definition_fingerprint({"preview_id": preview_id, "actor": who}),
            )
            return {"status": result["status"], "execution": result}
        # 未記録は未実行の証明ではない。再試行時にも同じ preview を使用する。
        return {"status": "unresolved"}

    def execute(
        self,
        profile_id: str,
        definition_id: str,
        preview_id: str,
        key: str,
        actor: Principal | None,
    ) -> dict[str, Any]:
        actor = refreshed_actor(actor)
        who = authorize_definition_operation(profile_id, actor, ACTION_EXECUTE)
        preview = json.loads(
            self.document(profile_id, preview_id, "ontology_action_preview")["content"]
        )
        if preview["actor"] != who or preview["definition_id"] != definition_id:
            blocked("この操作の確認は現在のユーザーに属していません。")
        if not key.strip():
            raise ValueError("Idempotency-Key が必要です。")
        identity = stable_ontology_id("ontology_action_execution", profile_id, preview_id)
        # プレビューごとに一度だけ。別キーでも同じ操作の二重適用をしない。
        request_hash = definition_fingerprint({"preview_id": preview_id, "actor": who})
        key_id = stable_ontology_id("ontology_action_key", profile_id, who, key)
        previous_key = self.store.get_artifact(key_id)
        if previous_key and json.loads(previous_key["content"])["preview_id"] != preview_id:
            raise OntologyVersionConflictError(
                "IDEMPOTENCY_KEY_REUSED", "このキーは別の確認に使用されています。"
            )
        if self.store.get_artifact(identity):
            return self._replay(
                profile_id, identity, "ontology_action_execution", who, request_hash
            )
        request = CapabilityCallRequest.model_validate(preview["request"])
        definition, binding, plan, _ = self._ready(profile_id, definition_id, request, actor)
        if (
            preview["binding_etag"] != binding["etag"]
            or preview["head_etag"] != self.head(profile_id)["etag"]
            or datetime.now(UTC) >= datetime.fromisoformat(preview["expires_at"])
        ):
            raise OntologyVersionConflictError(
                "ACTION_PREVIEW_STALE",
                "確認対象・binding・公開版が変わったか有効期限が切れました。再プレビューしてください。",
            )
        params = parameters_for(definition, request.parameters)
        from .ontology_capability_transaction import action_transaction

        try:
            with (
                actor_scope(who, is_system_admin=actor_is_admin(actor)),
                action_transaction(
                    self, profile_id, binding, preview, identity, key_id, actor
                ) as transaction,
            ):
                if transaction.existing:
                    return dict(transaction.existing)
                cursor = transaction.cursor
                # LOCK_SCOPE のロックを保持し、業務更新だけを取り消せる境界を固定する。
                cursor.execute("SAVEPOINT NL2SQL_ONT_ACTION_BODY", {})
                try:
                    before = self._read_target(
                        cursor, plan, request.target, lock=True, version=True
                    )
                    row_scn = before.pop("__row_scn")
                    if (
                        definition_fingerprint({"values": before, "row_scn": row_scn})
                        != preview["object_version"]
                    ):
                        raise OntologyVersionConflictError(
                            "OBJECT_VERSION_CHANGED",
                            "対象が更新されました。再プレビューしてください。",
                        )
                    self._state(before, binding)
                    authorize_definition_operation(profile_id, actor, ACTION_EXECUTE)
                    from .ontology_capability_context import ActionExecutionContext

                    context = ActionExecutionContext(
                        cursor, plan, request.target, definition.affected_properties
                    )
                    if binding["kind"] == "backend":
                        ACTION_REGISTRY[binding["implementation_key"]].execute(
                            context, before, params
                        )
                    else:
                        context.update(
                            {name: params[param] for name, param in plan["updates"].items()}
                        )
                    after = self._read_target(cursor, plan, request.target)
                    if after != preview["after"]:
                        raise OntologyVersionConflictError(
                            "ACTION_EFFECT_CHANGED",
                            "実際の変更がプレビューと一致しません。transaction を取り消しました。",
                        )
                    self._release(profile_id, request.release_id)
                    result = {
                        "id": identity,
                        "profile_id": profile_id,
                        "definition_id": definition_id,
                        "release_id": request.release_id,
                        "preview_id": preview_id,
                        "binding_etag": binding["etag"],
                        "actor": who,
                        "request_hash": request_hash,
                        "parameters": request.parameters,
                        "target": request.target,
                        "before": before,
                        "after": after,
                        "status": "succeeded",
                        "at": datetime.now(UTC).isoformat(),
                    }
                except Exception as failure:
                    # rollback/失敗記録の保存が失敗した場合は確定扱いにしない。
                    cursor.execute("ROLLBACK TO SAVEPOINT NL2SQL_ONT_ACTION_BODY", {})
                    result = {
                        "id": identity,
                        "profile_id": profile_id,
                        "definition_id": definition_id,
                        "release_id": request.release_id,
                        "preview_id": preview_id,
                        "binding_etag": binding["etag"],
                        "actor": who,
                        "request_hash": request_hash,
                        "parameters": request.parameters,
                        "target": request.target,
                        "status": "failed",
                        "changes_applied": False,
                        "error_code": getattr(failure, "code", "ACTION_EXECUTION_FAILED"),
                        "message_ja": (
                            "操作は失敗し、変更を取り消しました。"
                            "入力を確認して再プレビューしてください。"
                        ),
                        "at": datetime.now(UTC).isoformat(),
                    }
                transaction.save(result)
            return result
        except Exception as exc:
            uncertain = getattr(exc, "code", "") == "ACTION_OUTCOME_UNKNOWN"
            if uncertain:
                try:
                    if self.store.get_artifact(identity):
                        return self._replay(
                            profile_id, identity, "ontology_action_execution", who, request_hash
                        )
                except Exception:
                    logging.getLogger(__name__).warning(
                        "ontology_action_outcome_check_failed",
                        extra={"execution_id": identity, "profile_id": profile_id},
                    )
            # 失敗監査は rollback 後の別 record。成功 record と同一扱いにしない。
            self.store.save_artifact(
                self.artifact(
                    profile_id,
                    f"ontology_action_failure_{uuid4().hex}",
                    "ontology_capability_audit",
                    {
                        "actor": who,
                        "preview_id": preview_id,
                        "definition_id": definition_id,
                        "status": "unknown" if uncertain else "failed",
                        "error_type": type(exc).__name__,
                        "at": datetime.now(UTC).isoformat(),
                    },
                )
            )
            raise
