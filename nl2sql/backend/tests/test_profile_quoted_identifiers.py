"""業務プロファイルの対象表を object_identity の引用規則で保存・照合する (#561)。"""

from __future__ import annotations

import pytest

from app.features.nl2sql.models import (
    AllowedObjects,
    Nl2SqlProfile,
    SchemaCatalog,
    SchemaTable,
)
from app.features.nl2sql.object_identity import sql_identifier_token
from app.features.nl2sql.ontology_build import _selected_schema_objects
from app.features.nl2sql.ontology_catalog import (
    build_schema_ontology,
    migrate_profile_ontology_view,
)
from app.features.nl2sql.ontology_models import PhysicalObjectRef, ProfileOntologyView
from app.features.nl2sql.ontology_router import OntologyApiRuntime
from app.features.nl2sql.service import Nl2SqlService, _graph_with_resolved_table_owners
from app.features.nl2sql.sql_semantics import parse_oracle_sql
from app.features.nl2sql.store import MemoryNl2SqlStore

UPPER = "SALES.MIXED_CASE"
QUOTED = 'SALES."Mixed_Case"'


def _table(owner: str, name: str, table_type: str = "TABLE") -> SchemaTable:
    return SchemaTable(
        owner=owner,
        table_name=name,
        logical_name=f"{owner} {name}",
        table_type=table_type,
    )


def _service(*tables: SchemaTable, current_owner: str = "SALES") -> Nl2SqlService:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._catalog = SchemaCatalog(  # noqa: SLF001 - scope resolver unit boundary
        refreshed_at="2026-09-14T00:00:00+00:00",
        current_owner=current_owner,
        tables=list(tables),
    )
    return service


def _both_tables_service() -> Nl2SqlService:
    return _service(_table("SALES", "MIXED_CASE"), _table("SALES", "Mixed_Case"))


# --- 保存（Profile の対象表の正規化） -------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (QUOTED, QUOTED),
        ('"SALES"."Mixed_Case"', QUOTED),
        ('"Mixed_Case"', QUOTED),
        ("mixed_case", UPPER),
        ("Mixed_Case", UPPER),
        ("sales.mixed_case", UPPER),
        ('"SALES"."MIXED_CASE"', UPPER),
        (UPPER, UPPER),
        ('SALES."売上"', 'SALES."売上"'),
    ],
)
def test_resolve_profile_object_name_follows_oracle_quoting(raw: str, expected: str) -> None:
    service = _both_tables_service()

    assert service._resolve_profile_object_name(raw) == expected  # noqa: SLF001


@pytest.mark.parametrize("raw", ['SALES."Mixed_Case', "SALES.my table", "SALES.DEPT@REMOTE", ""])
def test_resolve_profile_object_name_rejects_invalid_identifiers(raw: str) -> None:
    service = _both_tables_service()

    with pytest.raises(ValueError):
        service._resolve_profile_object_name(raw)  # noqa: SLF001


def test_profile_keeps_quoted_and_upper_same_name_tables_distinct() -> None:
    service = _both_tables_service()

    quoted_only = service.create_profile(
        Nl2SqlProfile(id="quoted", name="引用名", allowed_tables=[QUOTED])
    )
    both = service.create_profile(
        Nl2SqlProfile(
            id="both", name="両方", allowed_tables=[UPPER, QUOTED, '"SALES"."MIXED_CASE"']
        )
    )

    assert quoted_only.allowed_tables == [QUOTED]
    assert service.get_profile("quoted").allowed_tables == [QUOTED]
    assert service.profile_allowed_object_names(quoted_only) == [QUOTED]
    assert both.allowed_tables == [UPPER, QUOTED]


def test_existing_unquoted_profile_values_are_unchanged() -> None:
    service = _service(_table("APP", "ORDERS"), _table("SH", "ORDERS"), current_owner="APP")

    profile = service.create_profile(
        Nl2SqlProfile(
            id="orders",
            name="受注",
            allowed_tables=["ORDERS", "SH.ORDERS", "app.orders", '"APP"."ORDERS"'],
        )
    )

    assert profile.allowed_tables == ["APP.ORDERS", "SH.ORDERS"]


def test_bare_unquoted_name_does_not_match_quoted_catalog_table() -> None:
    quoted_only = _service(_table("SH", "Mixed_Case"), current_owner="APP")

    # 引用なしの `mixed_case` は Oracle では `MIXED_CASE`。引用名の表 SH."Mixed_Case" に解決しない。
    assert quoted_only._resolve_profile_object_name("mixed_case") == "APP.MIXED_CASE"  # noqa: SLF001
    assert quoted_only._resolve_profile_object_name('"Mixed_Case"') == 'SH."Mixed_Case"'  # noqa: SLF001


# --- 許可表チェック（SQL 安全性検査） --------------------------------------------


@pytest.mark.parametrize(
    ("allowed_name", "sql", "safe", "referenced"),
    [
        (UPPER, 'SELECT * FROM SALES."Mixed_Case"', False, QUOTED),
        (UPPER, 'SELECT * FROM "Mixed_Case"', False, QUOTED),
        (UPPER, "SELECT * FROM sales.mixed_case", True, UPPER),
        (UPPER, 'SELECT * FROM "SALES"."MIXED_CASE"', True, UPPER),
        (QUOTED, 'SELECT * FROM SALES."Mixed_Case"', True, QUOTED),
        (QUOTED, 'SELECT m.* FROM "Mixed_Case" m', True, QUOTED),
        (QUOTED, "SELECT * FROM SALES.MIXED_CASE", False, UPPER),
        (QUOTED, "SELECT * FROM sales.Mixed_Case", False, UPPER),
    ],
)
def test_sql_safety_distinguishes_quoted_and_upper_same_name_tables(
    allowed_name: str, sql: str, safe: bool, referenced: str
) -> None:
    service = _both_tables_service()
    allowed = AllowedObjects(table_names=[allowed_name], enforce_table_scope=True)

    analyzed = service.analyze_sql(sql, allowed, None)

    assert analyzed.safety.referenced_tables == [referenced]
    assert analyzed.safety.is_safe is safe
    if not safe:
        assert "許可されていない表" in (analyzed.safety.blocked_reason or "")


def test_profile_scope_resolution_does_not_widen_quoted_profile_to_upper_table() -> None:
    service = _both_tables_service()
    service.create_profile(Nl2SqlProfile(id="quoted", name="引用名", allowed_tables=[QUOTED]))

    requested_upper = service.resolve_allowed_objects(
        "quoted", AllowedObjects(table_names=[UPPER], enforce_table_scope=True)
    )
    requested_quoted = service.resolve_allowed_objects(
        "quoted", AllowedObjects(table_names=['"SALES"."Mixed_Case"'], enforce_table_scope=True)
    )
    direct_sql = service.resolve_direct_sql_allowed_objects(
        AllowedObjects(table_names=[UPPER, QUOTED], enforce_table_scope=True),
        profile_ids={"quoted"},
    )

    assert requested_upper.table_names == []
    assert requested_quoted.table_names == [QUOTED]
    assert direct_sql.table_names == [QUOTED]


def test_sql_semantic_graph_records_identifier_quoting() -> None:
    graph = parse_oracle_sql(
        'SELECT * FROM sales."Mixed_Case" m JOIN "SALES".orders o ON 1 = 1'
    ).graph

    assert graph is not None
    tables = [(t.owner, t.owner_quoted, t.name, t.name_quoted) for t in graph.tables]
    assert tables == [("sales", False, "Mixed_Case", True), ("SALES", True, "orders", False)]
    assert sql_identifier_token("Mixed_Case", quoted=True) == '"Mixed_Case"'
    assert sql_identifier_token("mixed_case", quoted=False) == "MIXED_CASE"


def test_resolved_table_owner_keeps_quoted_name_for_display() -> None:
    graph = _graph_with_resolved_table_owners(
        parse_oracle_sql('SELECT * FROM "Mixed_Case" JOIN sales.mixed_case ON 1 = 1').graph,
        "SALES",
    )

    assert graph is not None
    assert [(t.resolved_owner, t.resolved_qualified_name) for t in graph.tables] == [
        ("SALES", QUOTED),
        ("SALES", UPPER),
    ]


def test_generation_catalog_does_not_use_upper_table_detail_for_quoted_profile_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.features.nl2sql.incremental_store import MemoryIncrementalNl2SqlRepository
    from app.features.nl2sql.models import SchemaColumn

    upper = _table("SALES", "MIXED_CASE")
    upper.columns = [
        SchemaColumn(column_name="UPPER_ONLY", logical_name="大文字", data_type="NUMBER")
    ]
    quoted = _table("SALES", "Mixed_Case")
    quoted.columns = [SchemaColumn(column_name="Amount", logical_name="引用名", data_type="NUMBER")]
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    catalog = SchemaCatalog(refreshed_at="2026-09-14T00:00:00+00:00", tables=[upper, quoted])
    manifest = {(t.owner, t.table_name): "v1" for t in catalog.tables}
    repository.apply_schema_refresh(
        catalog=catalog, manifest=manifest, changed_keys=set(manifest), deleted_keys=set()
    )

    service = _both_tables_service()
    profile = service.create_profile(
        Nl2SqlProfile(id="quoted", name="引用名", allowed_tables=[QUOTED])
    )
    service._incremental_repository = repository  # noqa: SLF001
    monkeypatch.setattr(service, "_refresh_cache_token", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "_profile_scope_for_read", lambda value, **_kwargs: value)

    # #563 から schema catalog の詳細取得は大文字小文字を区別する。引用名の Profile には
    # 引用名の表の定義だけを使い、大文字の同名表の定義を使わない。
    generated = service._generation_schema_catalog(  # noqa: SLF001
        profile, AllowedObjects(table_names=[QUOTED], enforce_table_scope=True)
    )

    assert [(t.owner, t.table_name) for t in generated.tables] == [("SALES", "Mixed_Case")]
    assert [c.column_name for c in generated.tables[0].columns] == ["Amount"]


# --- オントロジー --------------------------------------------------------------


def test_ontology_build_selects_exact_catalog_object_for_upper_and_quoted_profile_objects() -> None:
    catalog = SchemaCatalog(
        refreshed_at="2026-09-14T00:00:00+00:00",
        current_owner="SALES",
        tables=[_table("SALES", "MIXED_CASE"), _table("SALES", "Mixed_Case")],
    )

    upper_selected, upper_warnings, upper_errors = _selected_schema_objects(
        Nl2SqlProfile(id="upper", name="大文字", allowed_tables=[UPPER]), catalog
    )
    quoted_selected, quoted_warnings, quoted_errors = _selected_schema_objects(
        Nl2SqlProfile(id="quoted", name="引用名", allowed_tables=[QUOTED]), catalog
    )

    assert [(t.owner, t.table_name) for t in upper_selected] == [("SALES", "MIXED_CASE")]
    assert upper_warnings == [] and upper_errors == []
    # AI 構築も引用規則で照合するため、引用名の表を大文字の同名表と取り違えずに対象にする（#573）。
    assert [(t.owner, t.table_name) for t in quoted_selected] == [("SALES", "Mixed_Case")]
    assert quoted_warnings == [] and quoted_errors == []


def test_profile_ontology_view_does_not_map_quoted_profile_object_to_upper_node() -> None:
    ontology = build_schema_ontology(
        SchemaCatalog(
            refreshed_at="2026-09-14T00:00:00+00:00",
            current_owner="SALES",
            tables=[_table("SALES", "MIXED_CASE"), _table("SALES", "売上")],
        )
    )

    quoted = migrate_profile_ontology_view(
        Nl2SqlProfile(id="quoted", name="引用名", allowed_tables=[QUOTED]), ontology
    )
    upper = migrate_profile_ontology_view(
        Nl2SqlProfile(id="upper", name="大文字", allowed_tables=[UPPER, 'SALES."売上"']), ontology
    )

    assert quoted.physical_objects == []
    assert sorted(item.object_name for item in upper.physical_objects) == ["MIXED_CASE", "売上"]
    with pytest.raises(ValueError, match="Ontology に存在しません"):
        migrate_profile_ontology_view(
            Nl2SqlProfile(id="quoted", name="引用名", allowed_tables=[QUOTED]),
            ontology,
            strict=True,
        )


def test_profile_view_warning_reports_unresolved_quoted_profile_object() -> None:
    view = ProfileOntologyView(
        id="view",
        profile_id="quoted",
        ontology_revision_id="rev",
        physical_objects=[PhysicalObjectRef(owner="SALES", object_name="MIXED_CASE")],
    )

    warnings = OntologyApiRuntime._profile_view_unresolved_object_warnings(  # noqa: SLF001
        Nl2SqlProfile(id="quoted", name="引用名", allowed_tables=[QUOTED, UPPER]), view
    )

    assert len(warnings) == 1
    assert QUOTED in warnings[0]
