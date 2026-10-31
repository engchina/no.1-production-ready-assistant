"""一意な未修飾の物理列の解決と、回答前の確認の回帰（#3396）。"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from test_mcp import _call, _FakeNl2SqlService, _run, _structured, _token, _Users
from test_mcp import fake_service as fake_service
from test_mcp import users as users
from test_nl2sql_guided_clarification import _item, _LegacyService, _output, _ScriptedLlm

from app.features.nl2sql import ontology_router
from app.features.nl2sql.models import (
    AllowedObjects,
    Nl2SqlProfile,
    SchemaCatalog,
    SchemaColumn,
    SchemaTable,
)
from app.features.nl2sql.ontology_models import (
    ClarificationMode,
    RequirementPhase,
    RequirementSection,
    RequirementStatus,
)
from app.features.nl2sql.ontology_router import OntologyApiRuntime, QuerySessionApiCreate
from app.features.nl2sql.ontology_store import InMemoryOntologyStore
from app.features.nl2sql.sql_requirement import (
    LlmRequirementOutput,
    RequirementContext,
    build_draft,
    build_requirement_context,
)
from app.settings import get_settings

QUESTION = "APP.TABLE_A の VALUE_A を CATEGORY ごとに合計し、CATEGORY の昇順で表示する"


def _table(
    name: str = "TABLE_A", *, owner: str = "APP", columns: tuple[str, ...] = ("CATEGORY", "VALUE_A")
) -> SchemaTable:
    return SchemaTable(
        owner=owner,
        table_name=name,
        logical_name=name,
        columns=[
            SchemaColumn(
                column_name=column,
                logical_name=column,
                data_type="NUMBER" if column == "VALUE_A" else "VARCHAR2",
            )
            for column in columns
        ],
    )


def _context(
    tables: list[SchemaTable] | None = None,
    *,
    allowed: AllowedObjects | None = None,
    scope: list[str] | None = None,
) -> RequirementContext:
    return build_requirement_context(
        question=QUESTION,
        profile=Nl2SqlProfile(
            id="profile-a",
            name="プロファイル A",
            allowed_tables=scope if scope is not None else ["APP.TABLE_A"],
        ),
        catalog_tables=tables if tables is not None else [_table()],
        allowed=allowed or AllowedObjects(),
        ontology=None,
        view=None,
        today=date(2026, 10, 30),
    )


def _aggregation_output() -> dict[str, Any]:
    return _output(
        _item(
            "from",
            "from",
            "confirmed",
            value="APP.TABLE_A",
            tables=["APP.TABLE_A"],
            sql_hint="APP.TABLE_A",
        ),
        _item(
            "select",
            "select",
            "confirmed",
            value="区分と合計",
            columns=["APP.TABLE_A.CATEGORY", "APP.TABLE_A.VALUE_A"],
            sql_hint="APP.TABLE_A.CATEGORY, SUM(APP.TABLE_A.VALUE_A) AS TOTAL_VALUE",
        ),
        _item(
            "group",
            "group_by",
            "confirmed",
            value="CATEGORY",
            columns=["CATEGORY"],
            sql_hint="CATEGORY",
        ),
        _item(
            "order",
            "order_by",
            "confirmed",
            value="CATEGORY の昇順",
            columns=["CATEGORY"],
            sql_hint="CATEGORY ASC",
        ),
        clarified=QUESTION,
    )


def test_unique_unqualified_column_resolves_to_physical_column() -> None:
    context = _context()
    assert context.resolve_column("APP.TABLE_A.CATEGORY") == "APP.TABLE_A.CATEGORY"
    assert context.resolve_column("TABLE_A.CATEGORY") == "APP.TABLE_A.CATEGORY"
    assert context.resolve_column("CATEGORY") == "APP.TABLE_A.CATEGORY"


def test_unqualified_group_and_order_columns_are_not_asked_again() -> None:
    draft = build_draft(
        LlmRequirementOutput.model_validate(_aggregation_output()), _context(), question=QUESTION
    )
    for section in (RequirementSection.GROUP_BY, RequirementSection.ORDER_BY):
        item = next(item for item in draft.items if item.section == section)
        assert item.status == RequirementStatus.CONFIRMED
        assert item.columns == ["APP.TABLE_A.CATEGORY"]
        assert item.sql_hint
    assert not any("範囲の外" in warning for warning in draft.warnings_ja)


def test_ambiguous_column_requires_table_or_owner() -> None:
    context = _context([_table(), _table(owner="OTHER")], scope=["APP.TABLE_A", "OTHER.TABLE_A"])
    assert context.resolve_column("CATEGORY") == ""
    assert context.resolve_column("TABLE_A.CATEGORY") == ""
    assert context.resolve_column("APP.TABLE_A.CATEGORY") == "APP.TABLE_A.CATEGORY"
    assert context.resolve_column("OTHER.TABLE_A.CATEGORY") == "OTHER.TABLE_A.CATEGORY"


def test_column_resolution_stays_within_allowed_tables_and_columns() -> None:
    tables = [_table(), _table("TABLE_B", columns=("SECRET_COLUMN", "CATEGORY"))]
    context = _context(tables)
    assert context.resolve_column("CATEGORY") == "APP.TABLE_A.CATEGORY"
    for value in ("SECRET_COLUMN", "TABLE_B.CATEGORY", "OTHER.TABLE_A.CATEGORY", "UNKNOWN"):
        assert context.resolve_column(value) == ""
    restricted = _context(tables, allowed=AllowedObjects(columns={"APP.TABLE_A": ["VALUE_A"]}))
    assert restricted.resolve_column("VALUE_A") == "APP.TABLE_A.VALUE_A"
    assert restricted.resolve_column("CATEGORY") == ""
    assert restricted.resolve_column("APP.TABLE_A.CATEGORY") == ""


def test_quoted_column_names_preserve_case_and_embedded_dots() -> None:
    context = _context([_table(columns=("MixedCase", "Mixed.Name", "CATEGORY"))])
    for value in ('"MixedCase"', 'TABLE_A."MixedCase"', 'APP.TABLE_A."MixedCase"'):
        assert context.resolve_column(value) == 'APP.TABLE_A."MixedCase"'
    for value in ('"Mixed.Name"', 'TABLE_A."Mixed.Name"', 'APP.TABLE_A."Mixed.Name"'):
        assert context.resolve_column(value) == 'APP.TABLE_A."Mixed.Name"'
    for value in ("MixedCase", '"mixedcase"', "Mixed.Name", '"category"'):
        assert context.resolve_column(value) == ""
    assert context.resolve_column("category") == "APP.TABLE_A.CATEGORY"


def test_relevance_filter_does_not_make_duplicate_columns_unique() -> None:
    tables = [_table()]
    tables.extend(_table(f"TABLE_{i}", columns=(f"COLUMN_{i}",)) for i in range(16))
    for table in tables[1:]:
        table.logical_name = "CATEGORY"
        table.comment = "VALUE_A"
    tables.append(_table("TABLE_LAST", columns=("CATEGORY", "HIDDEN_COLUMN")))
    context = _context(tables, scope=[table.qualified_name for table in tables])
    assert len(context.tables) < len(tables)
    assert "APP.TABLE_LAST" not in context.tables
    assert context.resolve_column("CATEGORY") == ""
    assert context.resolve_column("HIDDEN_COLUMN") == ""
    assert context.resolve_column("APP.TABLE_LAST.CATEGORY") == ""
    assert context.resolve_column("APP.TABLE_A.CATEGORY") == "APP.TABLE_A.CATEGORY"


def _runtime(monkeypatch: pytest.MonkeyPatch) -> OntologyApiRuntime:
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_profile_confirmation_required", False)
    legacy = _LegacyService()
    legacy.profile = Nl2SqlProfile(
        id="profile-a", name="プロファイル A", allowed_tables=["APP.TABLE_A"]
    )
    legacy.catalog = SchemaCatalog(
        refreshed_at="2026-10-30T00:00:00Z", schema_fingerprint="columns", tables=[_table()]
    )
    legacy._enterprise_ai_client = _ScriptedLlm(_aggregation_output())
    return OntologyApiRuntime(legacy_service=legacy, store=InMemoryOntologyStore())


def test_guided_session_with_unqualified_columns_is_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(monkeypatch)
    created = runtime.create_session(
        QuerySessionApiCreate(
            question=QUESTION, profile_id="profile-a", clarification_mode=ClarificationMode.GUIDED
        ),
        actor_user_uuid="user-a",
    )
    assert created.requirement_state is not None
    assert created.requirement_state.status == RequirementPhase.READY
    assert created.session.requirement is not None


def test_agent_mcp_starts_job_with_unqualified_group_and_order_columns(
    monkeypatch: pytest.MonkeyPatch, users: _Users, fake_service: _FakeNl2SqlService
) -> None:
    runtime = _runtime(monkeypatch)
    monkeypatch.setattr(ontology_router, "ontology_runtime", runtime)
    fake_service.profiles = [
        Nl2SqlProfile(id="profile-a", name="プロファイル A", allowed_tables=["APP.TABLE_A"])
    ]
    fake_service.finish_after_polls = None
    actor = users.system_admin()
    response = _run(
        _call("nl2sql_query", {"question": QUESTION, "profile_id": "profile-a", "wait_seconds": 0}),
        token=_token(actor.user_uuid, profile_ids=["profile-a"]),
    )
    is_error, content = _structured(response)
    assert is_error is False, content
    assert content["status"] == "pending"
    assert content["job_id"]
    assert content["requirement"]["status"] == "applied"
    assert content["requirement"]["question"] is None
    assert len(fake_service.started) == 1
    request, owner, _ = fake_service.started[0]
    assert owner == actor.user_uuid
    assert request.sql_requirement is not None
    for section in (RequirementSection.GROUP_BY, RequirementSection.ORDER_BY):
        item = next(item for item in request.sql_requirement.items if item.section == section)
        assert item.columns == ["TABLE_A.CATEGORY"]
        assert item.sql_hint


def test_order_alias_keeps_priority_over_a_same_named_physical_column() -> None:
    output = _aggregation_output()
    output["items"][1]["sql_hint"] = "SUM(APP.TABLE_A.VALUE_A) AS CATEGORY"
    output["items"][1]["columns"] = ["APP.TABLE_A.VALUE_A"]
    draft = build_draft(LlmRequirementOutput.model_validate(output), _context(), question=QUESTION)
    order = next(item for item in draft.items if item.section == RequirementSection.ORDER_BY)
    assert order.status == RequirementStatus.CONFIRMED
    assert order.columns == ["APP.TABLE_A.VALUE_A"]
    group = next(item for item in draft.items if item.section == RequirementSection.GROUP_BY)
    assert group.columns == ["APP.TABLE_A.CATEGORY"]


def test_ambiguous_unqualified_group_column_still_requires_clarification() -> None:
    context = _context([_table(), _table("TABLE_B")], scope=["APP.TABLE_A", "APP.TABLE_B"])
    draft = build_draft(
        LlmRequirementOutput.model_validate(_aggregation_output()), context, question=QUESTION
    )
    group = next(item for item in draft.items if item.section == RequirementSection.GROUP_BY)
    assert group.status == RequirementStatus.AMBIGUOUS
    assert group.sql_hint == ""
