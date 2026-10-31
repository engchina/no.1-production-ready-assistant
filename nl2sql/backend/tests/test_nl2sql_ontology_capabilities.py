"""公開版能力・実行確認・同一 transaction の回帰。Oracle は明示的 fixture。"""

from __future__ import annotations

import copy
import json
import re
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from test_nl2sql_ontology_definitions import runtime
from test_nl2sql_ontology_workspace import model, no_auth  # noqa: F401

from app.features.nl2sql.ontology_capabilities import (
    ACTION_EXECUTE,
    CAPABILITY_MANAGE,
    CapabilityBindingRequest,
    CapabilityCallRequest,
    ProfileOntologyCapabilityService,
    StateRequirement,
    checked_capability_sql,
)
from app.features.nl2sql.ontology_definitions import (
    ActionAssignment,
    ActionTypeDefinition,
    DefinitionMapping,
    FunctionDefinition,
    PropertyDefinition,
    TypedParameter,
)
from app.features.nl2sql.ontology_published_context import published_context
from app.features.nl2sql.ontology_service import (
    OntologyGateBlockedError,
    OntologyVersionConflictError,
)
from app.features.nl2sql.ontology_store import canonical_json
from app.security.domain import Principal


class Variable:
    def __init__(self) -> None:
        self.value: Any = None

    def getvalue(self) -> Any:
        return self.value


class Cursor:
    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.description: list[Any] = []
        self.rows: list[Any] = []
        self.rowcount = 0

    def __enter__(self) -> Cursor:
        return self

    def __exit__(self, *_: Any) -> None:
        pass

    def var(self, _: Any) -> Variable:
        return Variable()

    def callproc(self, name: str, params: list[Any]) -> None:
        store = self.connection.adapter.svc.store
        if name.endswith("LOCK_SCOPE"):
            profile, _, head_etag, binding_id, binding_etag, identity, key_id, existing, key = (
                params
            )
            assert self.connection.adapter.svc.head(profile)["etag"] == head_etag
            assert store.get_artifact(binding_id)["etag"] == binding_etag
            for variable, artifact_id in ((existing, identity), (key, key_id)):
                record = store.get_artifact(artifact_id)
                variable.value = canonical_json(record) if record else None
        else:
            assert name.endswith("SAVE_RECORD")
            self.connection.records.append(json.loads(params[4]))

    def execute(self, sql: str, params: dict[str, Any]) -> None:
        self.connection.adapter.sql.append((sql, params))
        if sql.startswith("SAVEPOINT"):
            self.connection.savepoint = (
                copy.deepcopy(self.connection.row),
                copy.deepcopy(self.connection.records),
            )
        elif sql.startswith("ROLLBACK TO SAVEPOINT"):
            self.connection.row, self.connection.records = copy.deepcopy(self.connection.savepoint)
        elif sql.startswith("UPDATE"):
            columns = re.findall(r'"([^\"]+)" = :v\d+', sql)
            for index, column in enumerate(columns):
                self.connection.row[column] = params[f"v{index}"]
            self.rowcount = 1
        elif "FROM DUAL" in sql:
            self.description = [("RESULT",)]
            self.rows = [(params["amount"] * 2,)]
        else:
            fields = re.findall(r'"([^\"]+)"', sql.split(" FROM ")[0])
            self.description = [(field,) for field in fields]
            self.rows = (
                [
                    tuple(
                        (
                            self.connection.adapter.scn
                            if field == "NL2SQL_ONT_ROW_SCN"
                            else self.connection.row[field]
                        )
                        for field in fields
                    )
                ]
                if params.get("k0") == self.connection.row["ID"]
                else []
            )

    def fetchmany(self, count: int) -> list[Any]:
        return self.rows[:count]


class Connection:
    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter
        self.row = copy.deepcopy(adapter.row)
        self.records: list[Any] = []

    def cursor(self) -> Cursor:
        return Cursor(self)

    def commit(self) -> None:
        if self.adapter.fail_commit:
            raise RuntimeError("fixture commit failure")
        self.adapter.svc.store.save_documents_atomic("artifacts", [(r, None) for r in self.records])
        self.adapter.row = self.row
        self.adapter.scn += 1
        self.adapter.commits += 1
        if self.adapter.fail_ack:
            raise RuntimeError("fixture acknowledgement lost after commit")

    def rollback(self) -> None:
        self.adapter.rollbacks += 1


class Adapter:
    settings = SimpleNamespace(oracle_user="APP")

    def __init__(self, svc: Any) -> None:
        self.svc = svc
        self.scn = 1
        self.row = {"ID": 1, "STATUS": "DRAFT"}
        self.sql: list[Any] = []
        self.commits = self.rollbacks = 0
        self.fail_commit = False
        self.fail_ack = False

    @contextmanager
    def user_data_connection(self) -> Any:
        yield Connection(self)


def ready(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, dict[str, Any], dict[str, str]]:
    rt, legacy = runtime()
    original = rt.prepare_build_schema_context

    def prepare(pid: str) -> Any:
        result = original(pid)
        schema = json.loads(result.schema_context)
        next(obj for obj in schema["objects"] if obj["object_name"] == "ORDERS")["columns"].append(
            {"column": "STATUS", "data_type": "VARCHAR2"}
        )
        return replace(result, schema_context=json.dumps(schema))

    monkeypatch.setattr(rt, "prepare_build_schema_context", prepare)
    svc = ProfileOntologyCapabilityService(rt)
    definitions = model()
    definitions[0].properties.append("Order.status")
    definitions.extend(
        [
            PropertyDefinition(
                api_name="Order.status",
                name_ja="状態",
                object_type="Order",
                data_type="string",
                writable=True,
                required=True,
                mappings=[
                    DefinitionMapping(owner="APP", object_name="ORDERS", column_name="STATUS")
                ],
            ),
            ActionTypeDefinition(
                api_name="approve",
                name_ja="承認",
                object_type="Order",
                parameters=[
                    TypedParameter(api_name="status", name_ja="承認状態", data_type="string")
                ],
                assignments=[ActionAssignment(property="Order.status", parameter="status")],
                affected_properties=["Order.status"],
                preconditions_ja=["下書きのみ"],
                permission_requirement_ja="操作権限",
                failure_policy_ja="全体を rollback",
            ),
            FunctionDefinition(
                api_name="twice",
                name_ja="倍額",
                return_type="number",
                parameters=[TypedParameter(api_name="amount", name_ja="金額", data_type="number")],
                expression_sql=":amount * 2",
            ),
        ]
    )
    bundle = svc.save_build(
        profile_id="sales",
        job_id="one",
        definitions=definitions,
        schema_fingerprint="schema",
        source_revision_id="legacy",
    )
    bundle = svc.validate("sales", bundle.id, bundle.etag, None)
    assert not [f for f in bundle.findings if f.severity == "error"]
    bundle = svc.review("sales", bundle.id, bundle.etag, [d.id for d in bundle.definitions], None)
    release = svc.publish("sales", bundle.id, bundle.etag, "", "publish", None)
    adapter = Adapter(svc)
    monkeypatch.setattr(legacy, "_oracle_adapter", adapter, raising=False)
    ids = {d.api_name: d.id for d in bundle.definitions}
    return svc, adapter, release, ids


def binding(release: dict[str, Any]) -> CapabilityBindingRequest:
    return CapabilityBindingRequest(
        release_id=release["id"],
        kind="property_update",
        state_requirements=[StateRequirement(property="Order.status", value="DRAFT")],
        reviewed_rules_ja="下書きのみ承認、失敗時 rollback",
    )


def request(release: dict[str, Any]) -> CapabilityCallRequest:
    return CapabilityCallRequest(
        release_id=release["id"], parameters={"status": "CONFIRMED"}, target={"Order.id": 1}
    )


def test_build_does_not_enable_capabilities_and_explicit_binding_uses_etag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, adapter, release, ids = ready(monkeypatch)
    assert {c["status"] for c in svc.catalog("sales")["capabilities"]} == {"configuration_required"}
    with pytest.raises(OntologyGateBlockedError):
        svc.preview("sales", ids["approve"], request(release), "preview", None)
    saved = svc.bind("sales", ids["approve"], binding(release), "*", None)
    assert saved["etag"]
    with pytest.raises(OntologyVersionConflictError):
        svc.bind("sales", ids["approve"], binding(release), "*", None)
    assert adapter.sql == []


def test_action_preview_execute_replay_and_audit_are_atomic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, adapter, release, ids = ready(monkeypatch)
    svc.bind("sales", ids["approve"], binding(release), "*", None)
    preview = svc.preview("sales", ids["approve"], request(release), "preview", None)
    assert preview["before"]["Order.status"] == "DRAFT"
    assert preview["after"]["Order.status"] == "CONFIRMED"
    assert adapter.row["STATUS"] == "DRAFT"
    result = svc.execute("sales", ids["approve"], preview["id"], "execute", None)
    assert adapter.row["STATUS"] == "CONFIRMED"
    assert adapter.commits == 1
    assert result["release_id"] == release["id"]
    assert svc.execute("sales", ids["approve"], preview["id"], "retry", None) == result
    assert adapter.commits == 1
    assert svc.document("sales", result["id"], "ontology_action_execution")["content_hash"]


@pytest.mark.parametrize("change", ["target", "binding", "expired", "commit", "aba"])
def test_changed_confirmation_and_failed_transaction_never_apply(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    svc, adapter, release, ids = ready(monkeypatch)
    saved = svc.bind("sales", ids["approve"], binding(release), "*", None)
    preview = svc.preview("sales", ids["approve"], request(release), "preview", None)
    if change == "target":
        adapter.row["STATUS"] = "CANCELLED"
    elif change == "aba":
        adapter.scn += 2  # 更新後に元の値へ戻っても、確認時の行版と一致しない。
    elif change == "binding":
        svc.bind("sales", ids["approve"], binding(release), saved["etag"], None)
    elif change == "expired":
        record = svc.document("sales", preview["id"], "ontology_action_preview")
        preview["expires_at"] = "2000-01-01T00:00:00+00:00"
        svc.store.save_artifact(
            svc.artifact("sales", preview["id"], "ontology_action_preview", preview),
            expected_etag=record["etag"],
        )
    else:
        adapter.fail_commit = True
    if change in {"target", "aba"}:
        result = svc.execute("sales", ids["approve"], preview["id"], "execute", None)
        assert result["status"] == "failed"
        assert result["error_code"] == "OBJECT_VERSION_CHANGED"
        assert adapter.commits == 1  # 確定失敗だけを記録し、対象の更新は行わない。
    else:
        with pytest.raises((OntologyVersionConflictError, RuntimeError)):
            svc.execute("sales", ids["approve"], preview["id"], "execute", None)
        assert adapter.commits == 0
    assert adapter.row["STATUS"] == ("CANCELLED" if change == "target" else "DRAFT")
    assert not [
        r
        for r in svc.store.list_artifacts(svc._session("sales"))
        if r["artifact_type"] == "ontology_action_execution"
        and json.loads(r["content"])["status"] == "succeeded"
    ]


def test_function_typed_binds_and_idempotent_result(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, adapter, release, ids = ready(monkeypatch)
    svc.bind(
        "sales",
        ids["twice"],
        CapabilityBindingRequest(
            release_id=release["id"], kind="expression", expression_sql=":amount * 2"
        ),
        "*",
        None,
    )
    call = CapabilityCallRequest(release_id=release["id"], parameters={"amount": 21})
    result = svc.invoke("sales", ids["twice"], call, "invoke", None)
    assert result["result"] == 42
    assert svc.invoke("sales", ids["twice"], call, "invoke", None) == result
    assert len(adapter.sql) == 1
    assert adapter.sql[0][1] == {"amount": 21}
    with pytest.raises(ValueError):
        svc.invoke(
            "sales",
            ids["twice"],
            call.model_copy(update={"parameters": {"amount": "21 OR 1=1"}}),
            "bad",
            None,
        )


def test_unsafe_sql_and_out_of_scope_bindings_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _, release, ids = ready(monkeypatch)
    bundle, _ = svc._definition("sales", release["id"], ids["twice"])
    for sql in (
        "DELETE FROM APP.ORDERS",
        "SELECT UTL_HTTP.REQUEST('https://example.com') FROM APP.ORDERS",
        "SELECT ID FROM OTHER.ORDERS",
        "SELECT SECRET FROM APP.ORDERS",
        "SELECT * FROM APP.ORDERS",
    ):
        with pytest.raises(ValueError):
            checked_capability_sql(
                sql, bundle, svc._schema("sales"), expression=False, parameter_names=set()
            )


def test_action_permission_and_profile_ownership_cannot_be_bypassed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _, release, ids = ready(monkeypatch)
    actor = Principal(
        user_uuid="reader",
        login_user_id="reader",
        display_name="reader",
        status="ACTIVE",
        force_password_change=False,
        role_codes=[],
        permissions={CAPABILITY_MANAGE},
        data_entitlements=[],
        allowed_profile_ids={"sales"},
        session_id="session",
        csrf_token_hash="hash",
    )
    svc.bind("sales", ids["approve"], binding(release), "*", actor)
    with pytest.raises(HTTPException) as error:
        svc.preview("sales", ids["approve"], request(release), "preview", actor)
    assert error.value.status_code == 403
    assert ACTION_EXECUTE not in actor.permissions


def test_published_context_is_frozen_and_filters_physical_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _, release, _ = ready(monkeypatch)
    context = published_context(svc.runtime, "sales", release["id"], {"APP.ORDERS": ["ID"]})
    # 許可されない列（STATUS）のプロパティと、SQL に使わないアクションは渡さない（#1720）。
    assert context.startswith("# 業務オントロジー\n")
    assert "状態" not in context and "STATUS" not in context
    assert "承認" not in context
    assert published_context(svc.runtime, "sales", "") == ""
    assert svc.release("sales", release["id"])["id"] == release["id"]


def test_two_profiles_share_schema_without_sharing_releases_bindings_or_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _, release, ids = ready(monkeypatch)
    profiles = {
        pid: svc.runtime.legacy_service.profile.model_copy(update={"id": pid})
        for pid in ("sales", "support")
    }
    monkeypatch.setattr(svc.runtime.legacy_service, "get_profile", lambda pid: profiles[pid])
    original = svc.release("sales", release["id"])
    from app.features.nl2sql.ontology_definitions import ProfileOntologyBundle

    definitions = ProfileOntologyBundle.model_validate(original["bundle"]).definitions
    for definition in definitions:
        definition.id = ""
        definition.name_ja = f"サポート {definition.name_ja}"
    bundle = svc.save_build(
        profile_id="support",
        job_id="same-schema",
        definitions=definitions,
        schema_fingerprint="schema",
        source_revision_id="legacy",
    )
    bundle = svc.validate("support", bundle.id, bundle.etag, None)
    bundle = svc.review("support", bundle.id, bundle.etag, [d.id for d in bundle.definitions], None)
    second = svc.publish("support", bundle.id, bundle.etag, "", "publish", None)
    assert second["id"] != release["id"]
    assert svc.head("sales")["release_id"] == release["id"]
    assert "サポート" in published_context(svc.runtime, "support", second["id"])
    assert "サポート" not in published_context(svc.runtime, "sales", release["id"])
    from app.features.nl2sql.ontology_service import OntologyNotFoundError

    with pytest.raises(OntologyNotFoundError):
        svc._definition("support", second["id"], ids["approve"])
    with pytest.raises(OntologyNotFoundError):
        svc.release("support", release["id"])
    svc.bind("sales", ids["approve"], binding(release), "*", None)
    assert all(c["binding"] is None for c in svc.catalog("support")["capabilities"])


def test_registered_handlers_are_explicit_and_state_failure_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.features.nl2sql.ontology_capabilities import FUNCTION_REGISTRY, RegisteredFunction

    svc, adapter, release, ids = ready(monkeypatch)
    backend = CapabilityBindingRequest(
        release_id=release["id"], kind="backend", implementation_key="trusted.double"
    )
    with pytest.raises(OntologyGateBlockedError):
        svc.bind("sales", ids["twice"], backend, "*", None)
    monkeypatch.setitem(
        FUNCTION_REGISTRY,
        "trusted.double",
        RegisteredFunction(lambda params, _context: params["amount"] * 2, "number"),
    )
    svc.bind("sales", ids["twice"], backend, "*", None)
    assert (
        svc.invoke(
            "sales",
            ids["twice"],
            CapabilityCallRequest(release_id=release["id"], parameters={"amount": 7}),
            "backend",
            None,
        )["result"]
        == 14
    )
    svc.bind("sales", ids["approve"], binding(release), "*", None)
    adapter.row["STATUS"] = "CANCELLED"
    with pytest.raises(OntologyGateBlockedError):
        svc.preview("sales", ids["approve"], request(release), "cancelled", None)


def test_action_key_reuse_and_disabled_binding_reject_new_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, adapter, release, ids = ready(monkeypatch)
    saved = svc.bind("sales", ids["approve"], binding(release), "*", None)
    first = svc.preview("sales", ids["approve"], request(release), "first", None)
    second = svc.preview("sales", ids["approve"], request(release), "second", None)
    svc.execute("sales", ids["approve"], first["id"], "key", None)
    with pytest.raises(OntologyVersionConflictError):
        svc.execute("sales", ids["approve"], second["id"], "key", None)
    svc.bind(
        "sales",
        ids["approve"],
        binding(release).model_copy(update={"enabled": False}),
        saved["etag"],
        None,
    )
    with pytest.raises(OntologyGateBlockedError):
        svc.preview("sales", ids["approve"], request(release), "disabled", None)
    assert adapter.commits == 1


def test_registered_action_uses_scoped_transaction_and_records_actual_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.features.nl2sql.ontology_capabilities import ACTION_REGISTRY, RegisteredAction

    svc, adapter, release, ids = ready(monkeypatch)

    def execute(context: Any, before: dict[str, Any], parameters: dict[str, Any]) -> dict[str, Any]:
        with pytest.raises(ValueError):
            context.update({"Order.id": 99})
        context.update({"Order.status": parameters["status"]})
        return {**before, "Order.status": parameters["status"]}

    monkeypatch.setitem(
        ACTION_REGISTRY,
        "trusted.approve",
        RegisteredAction(
            lambda before, params: {**before, "Order.status": params["status"]}, execute
        ),
    )
    svc.bind(
        "sales",
        ids["approve"],
        binding(release).model_copy(
            update={"kind": "backend", "implementation_key": "trusted.approve"}
        ),
        "*",
        None,
    )
    preview = svc.preview("sales", ids["approve"], request(release), "registered", None)
    result = svc.execute("sales", ids["approve"], preview["id"], "registered", None)
    assert result["after"] == {"Order.id": 1, "Order.status": "CONFIRMED"}
    assert adapter.row["ID"] == 1
    assert adapter.commits == 1


def test_business_release_survives_job_worker_snapshot_restore() -> None:
    from app.features.nl2sql.models import JobCreateRequest
    from app.features.nl2sql.service import StoredJob, nl2sql_service

    job = StoredJob(
        job_id="frozen-job",
        request=JobCreateRequest(question="件数", profile_id="sales"),
        business_release_id="release-sales-v1",
    )
    snapshot = nl2sql_service._job_to_snapshot(job)
    assert nl2sql_service._job_from_snapshot(snapshot).business_release_id == "release-sales-v1"
    snapshot.pop("business_release_id")
    assert nl2sql_service._job_from_snapshot(snapshot).business_release_id == ""


def test_lost_commit_acknowledgement_replays_persisted_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, adapter, release, ids = ready(monkeypatch)
    svc.bind("sales", ids["approve"], binding(release), "*", None)
    preview = svc.preview("sales", ids["approve"], request(release), "ack", None)
    adapter.fail_ack = True
    result = svc.execute("sales", ids["approve"], preview["id"], "ack", None)
    assert result["status"] == "succeeded"
    assert adapter.row["STATUS"] == "CONFIRMED"
    assert svc.execute("sales", ids["approve"], preview["id"], "ack", None) == result
    assert adapter.commits == 1
