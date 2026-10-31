"""Issue #469: SQL の所属スコープと確定した Action rollback。実 Oracle は fixture。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_nl2sql_ontology_capabilities import binding, ready, request
from test_nl2sql_ontology_definitions import runtime
from test_nl2sql_ontology_workspace import model, no_auth  # noqa: F401

from app.features.nl2sql.ontology_capabilities import ACTION_REGISTRY, RegisteredAction
from app.features.nl2sql.ontology_definition_validation import validate_definitions
from app.features.nl2sql.ontology_definition_workspace import ProfileOntologyWorkspaceService
from app.features.nl2sql.ontology_definitions import MetricDefinitionV2, ObjectSetDefinition
from app.features.nl2sql.ontology_published_context import _expressions_visible
from app.features.nl2sql.ontology_service import OntologyVersionConflictError


def metric(predicate: str) -> MetricDefinitionV2:
    return MetricDefinitionV2(
        api_name="Sales",
        name_ja="売上",
        expression_sql=(
            "SELECT o.ID, SUM(o.AMOUNT) FROM APP.ORDERS o "
            f"WHERE {predicate.replace('APP.ORDERS.', 'o.')} GROUP BY o.ID"
        ),
        filter_sql=predicate,
        aggregation="sum",
        grain=["Order.id"],
        unit="円",
        time_policy_ja="全期間",
        null_policy_ja="除外",
        additivity="additive",
    )


def inspect(definitions: list[Any]) -> tuple[Any, Any, set[str]]:
    rt, _ = runtime()
    svc = ProfileOntologyWorkspaceService(rt)
    b = svc.save_build(
        profile_id="sales",
        job_id="one",
        definitions=definitions,
        schema_fingerprint="schema",
        source_revision_id="legacy",
    )
    schema = json.loads(rt.prepare_build_schema_context("sales").schema_context)
    errors = {f.code for f in validate_definitions(b, schema) if f.severity == "error"}
    return svc, b, errors


@pytest.mark.parametrize("predicate", ["o.AMOUNT > 0", "o.ID > 0", "ID > 0", "APP.ORDERS.ID > 0"])
def test_metric_filter_keeps_select_scope_and_published_context(predicate: str) -> None:
    svc, b, errors = inspect([*model(), metric(predicate)])
    assert not errors
    definitions = {d.api_name: d for d in b.definitions}
    assert _expressions_visible(
        definitions["Sales"],
        {
            "APP.ORDERS": {"ID", "CUSTOMER_ID", "AMOUNT"},
            "APP.CUSTOMERS": {"ID", "NAME"},
        },
        definitions,
    )
    assert not _expressions_visible(
        definitions["Sales"],
        {
            "APP.ORDERS": {"ID"},
            "APP.CUSTOMERS": {"ID", "AMOUNT"},
        },
        definitions,
    )
    b = svc.validate("sales", b.id, b.etag, None)
    b = svc.review("sales", b.id, b.etag, [d.id for d in b.definitions], None)
    assert svc.publish("sales", b.id, b.etag, "", "publish", None)["id"]


@pytest.mark.parametrize("filter_sql", ["c.ID > 0", "o.MISSING > 0", "APP.CUSTOMERS.ID > 0"])
def test_filter_cannot_borrow_an_unselected_table_or_unknown_alias(filter_sql: str) -> None:
    definition = metric("o.ID > 0")
    definition.filter_sql = filter_sql
    _, _, errors = inspect([*model(), definition])
    assert errors


@pytest.mark.parametrize(
    "filter_sql,valid",
    [
        ("ID > 0", True),
        ("ORDERS.ID > 0", True),
        ("APP.CUSTOMERS.ID > 0", False),
        ("MISSING > 0", False),
    ],
)
def test_object_set_uses_its_object_mapping(filter_sql: str, valid: bool) -> None:
    _, b, errors = inspect(
        [
            *model(),
            ObjectSetDefinition(
                api_name="Selected", name_ja="対象受注", object_type="Order", filter_sql=filter_sql
            ),
        ]
    )
    assert (not errors) is valid
    definitions = {d.api_name: d for d in b.definitions}
    assert (
        _expressions_visible(
            definitions["Selected"],
            {
                "APP.ORDERS": {"ID"},
                "APP.CUSTOMERS": {"ID"},
            },
            definitions,
        )
        is valid
    )
    assert not _expressions_visible(
        definitions["Selected"],
        {
            "APP.ORDERS": set(),
            "APP.CUSTOMERS": {"ID"},
        },
        definitions,
    )


@pytest.mark.parametrize("mapping_sql", ["ID", "ORDERS.ID", "APP.ORDERS.ID"])
def test_metric_grain_and_distinct_keys_keep_property_mapping_scope(mapping_sql: str) -> None:
    definitions = model()
    definitions[1].mappings[0].expression_sql = mapping_sql
    total = metric("o.ID > 0")
    total.expression_sql = (
        "SELECT o.ID, COUNT(DISTINCT o.ID) FROM APP.ORDERS o WHERE o.ID > 0 GROUP BY o.ID"
    )
    total.aggregation = "count"
    total.distinct_keys = ["Order.id"]
    _, _, errors = inspect([*definitions, total])
    assert not errors
    total.expression_sql = (
        "SELECT o.CUSTOMER_ID, COUNT(DISTINCT o.CUSTOMER_ID) "
        "FROM APP.ORDERS o WHERE o.ID > 0 GROUP BY o.CUSTOMER_ID"
    )
    _, _, errors = inspect([*definitions, total])
    assert {"METRIC_GRAIN_MISMATCH", "METRIC_DISTINCT_MISMATCH"} <= errors


def test_committed_failure_is_terminal_and_new_preview_can_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, adapter, release, ids = ready(monkeypatch)
    calls = []

    def execute(context: Any, _before: Any, params: Any) -> Any:
        calls.append(params["status"])
        context.update({"Order.status": params["status"]})
        if params["status"] == "CONFIRMED":
            raise ValueError("入力を修正してください。")

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
    preview = svc.preview("sales", ids["approve"], request(release), "preview", None)
    failure = svc.execute("sales", ids["approve"], preview["id"], "execute", None)
    assert failure["status"] == "failed" and failure["changes_applied"] is False
    assert adapter.row == {"ID": 1, "STATUS": "DRAFT"}
    assert adapter.commits == 1
    assert svc.outcome("sales", ids["approve"], preview["id"], None) == {
        "status": "failed",
        "execution": failure,
    }
    for key in ("execute", "retry-other-key"):
        assert svc.execute("sales", ids["approve"], preview["id"], key, None) == failure
    assert calls == ["CONFIRMED"] and adapter.commits == 1
    corrected = request(release).model_copy(update={"parameters": {"status": "APPROVED"}})
    fresh = svc.preview("sales", ids["approve"], corrected, "new-preview", None)
    success = svc.execute("sales", ids["approve"], fresh["id"], "new-execute", None)
    assert success["status"] == "succeeded"
    assert adapter.row["STATUS"] == "APPROVED"
    assert calls == ["CONFIRMED", "APPROVED"]


@pytest.mark.parametrize("lost_ack", [False, True])
def test_failure_record_requires_a_known_commit(
    monkeypatch: pytest.MonkeyPatch, lost_ack: bool
) -> None:
    svc, adapter, release, ids = ready(monkeypatch)

    def fail(context: Any, _before: Any, params: Any) -> Any:
        context.update({"Order.status": params["status"]})
        raise ValueError("業務条件違反")

    monkeypatch.setitem(
        ACTION_REGISTRY,
        "trusted.fail",
        RegisteredAction(lambda before, params: {**before, "Order.status": params["status"]}, fail),
    )
    svc.bind(
        "sales",
        ids["approve"],
        binding(release).model_copy(
            update={"kind": "backend", "implementation_key": "trusted.fail"}
        ),
        "*",
        None,
    )
    preview = svc.preview("sales", ids["approve"], request(release), "preview", None)
    adapter.fail_ack, adapter.fail_commit = lost_ack, not lost_ack
    if lost_ack:
        assert (
            svc.execute("sales", ids["approve"], preview["id"], "execute", None)["status"]
            == "failed"
        )
        assert svc.outcome("sales", ids["approve"], preview["id"], None)["status"] == "failed"
    else:
        with pytest.raises(OntologyVersionConflictError) as exc:
            svc.execute("sales", ids["approve"], preview["id"], "execute", None)
        assert exc.value.code == "ACTION_OUTCOME_UNKNOWN"
        assert svc.outcome("sales", ids["approve"], preview["id"], None) == {"status": "unresolved"}
    assert adapter.row == {"ID": 1, "STATUS": "DRAFT"}
