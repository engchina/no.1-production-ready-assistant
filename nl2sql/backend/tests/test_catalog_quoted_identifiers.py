"""schema catalog・オントロジー・列単位の許可チェックを Oracle の引用規則で識別する (#563)。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi import HTTPException

from app.features.nl2sql.incremental_store import (
    MemoryIncrementalNl2SqlRepository,
    OracleIncrementalNl2SqlRepository,
    _merge_refresh_targets,
)
from app.features.nl2sql.models import (
    AllowedObjects,
    Nl2SqlProfile,
    SchemaCatalog,
    SchemaColumn,
    SchemaRefreshTargetObject,
    SchemaTable,
)
from app.features.nl2sql.object_visibility import system_object_prefix_sql_filters
from app.features.nl2sql.ontology_build import _ScopeResolver, build_schema_context
from app.features.nl2sql.ontology_catalog import (
    build_schema_ontology,
    catalog_schema_fingerprint,
    migrate_profile_ontology_view,
)
from app.features.nl2sql.ontology_models import OntologyNodeKind
from app.features.nl2sql.ontology_router import OntologyApiRuntime
from app.features.nl2sql.ontology_store import (
    physical_identity_part,
    stable_ontology_id,
    stable_physical_id,
)
from app.features.nl2sql.oracle_adapter import OracleNl2SqlAdapter
from app.features.nl2sql.service import Nl2SqlService, _dedupe_schema_refresh_targets
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.settings import get_settings

UPPER = "SALES.MIXED_CASE"
QUOTED = 'SALES."Mixed_Case"'


def _column(name: str) -> SchemaColumn:
    return SchemaColumn(column_name=name, logical_name=f"{name} 列", data_type="NUMBER")


def _table(owner: str, name: str, *columns: str) -> SchemaTable:
    return SchemaTable(
        owner=owner,
        table_name=name,
        logical_name=f"{owner} {name}",
        table_type="table",
        columns=[_column(column) for column in columns],
    )


def _both_catalog() -> SchemaCatalog:
    return SchemaCatalog(
        refreshed_at="2026-09-14T00:00:00+00:00",
        current_owner="SALES",
        tables=[
            _table("SALES", "MIXED_CASE", "AMOUNT", "UPPER_ONLY"),
            _table("SALES", "Mixed_Case", "AMOUNT", "Amount", "QUOTED_ONLY"),
            _table("Sales", "ORDERS", "ID"),
            _table("SALES", "ORDERS", "ID"),
        ],
    )


def _memory_repository(catalog: SchemaCatalog) -> MemoryIncrementalNl2SqlRepository:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    manifest = {(table.owner, table.table_name): "v1" for table in catalog.tables}
    repository.apply_schema_refresh(
        catalog=catalog, manifest=manifest, changed_keys=set(manifest), deleted_keys=set()
    )
    return repository


def _service(catalog: SchemaCatalog | None = None) -> Nl2SqlService:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._catalog = catalog or _both_catalog()  # noqa: SLF001 - unit boundary
    return service


# --- schema catalog: Memory repository ------------------------------------------------


def test_memory_repository_detail_is_case_sensitive() -> None:
    repository = _memory_repository(_both_catalog())

    upper = repository.get_schema_object("SALES", "MIXED_CASE")
    quoted = repository.get_schema_object("SALES", "Mixed_Case")

    assert upper is not None and quoted is not None
    assert [c.column_name for c in upper.table.columns] == ["AMOUNT", "UPPER_ONLY"]
    assert [c.column_name for c in quoted.table.columns] == ["AMOUNT", "Amount", "QUOTED_ONLY"]
    assert repository.get_schema_object("SALES", "mixed_case") is None


def test_memory_repository_search_keeps_same_name_objects_and_paginates_without_skip() -> None:
    repository = _memory_repository(_both_catalog())

    names: list[tuple[str, str]] = []
    cursor: str | None = None
    for _ in range(10):
        page = repository.search_schema_objects(
            cursor=cursor, limit=1, query="", owner="", object_type="", allowed_names=None
        )
        names.extend((item.owner, item.object_name) for item in page.items)
        cursor = page.next_cursor
        if not cursor:
            break

    assert sorted(names) == sorted(
        [("SALES", "MIXED_CASE"), ("SALES", "Mixed_Case"), ("Sales", "ORDERS"), ("SALES", "ORDERS")]
    )
    assert len(names) == len(set(names))


def test_memory_repository_search_filters_by_quoted_profile_names_and_exact_owner() -> None:
    repository = _memory_repository(_both_catalog())

    def search(**kwargs: Any) -> list[tuple[str, str]]:
        params: dict[str, Any] = {
            "cursor": None,
            "limit": 10,
            "query": "",
            "owner": "",
            "object_type": "",
            "allowed_names": None,
        }
        params.update(kwargs)
        page = repository.search_schema_objects(**params)
        return [(item.owner, item.object_name) for item in page.items]

    assert search(allowed_names={QUOTED}) == [("SALES", "Mixed_Case")]
    assert search(allowed_names={UPPER}) == [("SALES", "MIXED_CASE")]
    assert search(owner="Sales") == [("Sales", "ORDERS")]
    assert ("Sales", "ORDERS") not in search(owner="SALES")


def test_service_search_owner_and_detail_cache_follow_quoting_rules() -> None:
    service = _service()
    service.create_profile(Nl2SqlProfile(id="quoted", name="引用名", allowed_tables=[QUOTED]))

    def search(**kwargs: Any) -> list[tuple[str, str]]:
        params: dict[str, Any] = {
            "cursor": None,
            "limit": 10,
            "query": "",
            "owner": "",
            "object_type": "",
            "profile_id": None,
        }
        params.update(kwargs)
        return [(i.owner, i.object_name) for i in service.search_schema_objects(**params).items]

    assert search(profile_id="quoted") == [("SALES", "Mixed_Case")]
    # API の owner は引用規則: `"Sales"` は小文字を含む user、`sales` は `SALES`。
    assert search(owner='"Sales"') == [("Sales", "ORDERS")]
    assert ("Sales", "ORDERS") not in search(owner="sales")

    quoted = service.get_schema_object("SALES", "Mixed_Case")
    upper = service.get_schema_object("SALES", "MIXED_CASE")
    assert quoted is not None and upper is not None
    assert quoted.table.table_name == "Mixed_Case"
    assert upper.table.table_name == "MIXED_CASE"


def test_schema_detail_api_interprets_path_with_quoting_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.features.schema import router as schema_router

    service = _service()
    monkeypatch.setattr(schema_router, "nl2sql_service", service)

    class _Response:
        headers: dict[str, str] = {}

    def detail(owner: str, name: str) -> str:
        data = schema_router.object_detail(owner, name, _Response(), None)  # type: ignore[arg-type]
        return data.data.table.table_name  # type: ignore[union-attr]

    assert detail("SALES", '"Mixed_Case"') == "Mixed_Case"
    assert detail("SALES", "MIXED_CASE") == "MIXED_CASE"
    # 引用なしの小文字は従来どおり大文字として解釈する（API 互換）。
    assert detail("sales", "mixed_case") == "MIXED_CASE"
    with pytest.raises(HTTPException) as exc_info:
        detail("SALES", '"Mixed_Case')
    assert exc_info.value.status_code == 400


def test_refresh_target_keys_do_not_collapse_same_name_objects() -> None:
    targets = [
        SchemaRefreshTargetObject(owner="SALES", object_name="MIXED_CASE", object_type="table"),
        SchemaRefreshTargetObject(owner="SALES", object_name="Mixed_Case", object_type="table"),
    ]

    assert sorted((t.owner, t.object_name) for t in _dedupe_schema_refresh_targets(targets)) == [
        ("SALES", "MIXED_CASE"),
        ("SALES", "Mixed_Case"),
    ]
    assert len(_merge_refresh_targets(targets[:1], targets[1:])) == 2


# --- schema catalog: Oracle repository / adapter ---------------------------------------


class _Cursor:
    def __init__(self, results: list[list[tuple[Any, ...]]]) -> None:
        self.results = results
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, binds: dict[str, Any] | None = None) -> None:
        self.executed.append((sql, dict(binds or {})))
        self._rows = self.results.pop(0) if self.results else []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def __iter__(self) -> Iterator[tuple[Any, ...]]:
        return iter(list(self._rows))

    def setinputsizes(self, **_kwargs: Any) -> None:
        return None


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _Cursor:
        return self._cursor

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _oracle_repository(cursor: _Cursor) -> OracleIncrementalNl2SqlRepository:
    @contextmanager
    def connect() -> Iterator[_Connection]:
        yield _Connection(cursor)

    return OracleIncrementalNl2SqlRepository(connection_factory=connect)


def test_oracle_repository_search_binds_catalog_names_for_owner_cursor_and_profile_scope() -> None:
    cursor = _Cursor([[("SALES", "Mixed_Case", "TABLE", "", "", None, 3, "")]])
    repository = _oracle_repository(cursor)
    first = repository.search_schema_objects(
        cursor=None,
        limit=1,
        query="",
        owner="Sales",
        object_type="",
        allowed_names={QUOTED, "ORDERS"},
        include_counts=False,
    )
    sql, binds = cursor.executed[0]

    assert binds["owner"] == "Sales"
    # 大文字化して比べるのはシステムテーブルの接頭辞の除外（#1710）だけ。
    # 対象の名前は完全一致で比べる。
    for system_filter in system_object_prefix_sql_filters("o.OBJECT_NAME"):
        sql = sql.replace(system_filter, "")
    assert "UPPER(o.OBJECT_NAME)" not in sql
    assert json.loads(binds["allowed_names_json"]) == [
        {"owner": "", "name": "ORDERS"},
        {"owner": "SALES", "name": "Mixed_Case"},
    ]
    assert first.next_cursor is None

    cursor = _Cursor([[("SALES", "MIXED_CASE", "TABLE", "", "", None, 3, "")] * 2])
    repository = _oracle_repository(cursor)
    page = repository.search_schema_objects(
        cursor=None, limit=1, query="", owner="", object_type="", allowed_names=None,
        include_counts=False,
    )  # fmt: skip
    assert page.next_cursor
    cursor = _Cursor([[]])
    _oracle_repository(cursor).search_schema_objects(
        cursor=page.next_cursor, limit=1, query="", owner="", object_type="",
        allowed_names=None, include_counts=False,
    )  # fmt: skip
    assert cursor.executed[0][1]["after_name"] == "MIXED_CASE"


def test_oracle_repository_detail_and_insert_keep_catalog_case() -> None:
    cursor = _Cursor([[]])
    assert _oracle_repository(cursor).get_schema_object("SALES", "Mixed_Case") is None
    assert cursor.executed[0][1] == {"owner": "SALES", "object_name": "Mixed_Case"}

    cursor = _Cursor([])
    _oracle_repository(cursor)._insert_schema_table(  # noqa: SLF001
        cursor, _table("SALES", "Mixed_Case", "Amount"), ""
    )
    assert {(b["owner"], b["object_name"]) for _sql, b in cursor.executed} == {
        ("SALES", "Mixed_Case")
    }
    assert cursor.executed[1][1]["column_name"] == "Amount"


def _adapter(cursor: _Cursor) -> OracleNl2SqlAdapter:
    adapter = OracleNl2SqlAdapter(get_settings().model_copy(update={"oracle_user": "SALES"}))

    @contextmanager
    def connection() -> Iterator[_Connection]:
        yield _Connection(cursor)

    adapter.connection = connection  # type: ignore[method-assign,assignment]
    return adapter


def test_adapter_manifest_and_catalog_fetch_keep_catalog_case() -> None:
    cursor = _Cursor([[("SALES", "MIXED_CASE", "t1"), ("SALES", "Mixed_Case", "t2")]])
    manifest = _adapter(cursor).fetch_schema_manifest({("SALES", "Mixed_Case")})

    assert manifest == {("SALES", "MIXED_CASE"): "t1", ("SALES", "Mixed_Case"): "t2"}
    assert "Mixed_Case" in cursor.executed[0][1].values()

    rows = [
        ("SALES", "MIXED_CASE", "", "AMOUNT", "", "NUMBER", "Y", 1, 0, "TABLE"),
        ("SALES", "Mixed_Case", "", "Amount", "", "NUMBER", "Y", 1, 0, "TABLE"),
    ]
    cursor = _Cursor([rows, [], []])
    catalog = _adapter(cursor).fetch_catalog(
        include_samples=False, object_keys={("SALES", "Mixed_Case")}
    )

    assert "Mixed_Case" in cursor.executed[0][1].values()
    assert "MIXED_CASE" not in cursor.executed[0][1].values()
    assert [(t.table_name, [c.column_name for c in t.columns]) for t in catalog.tables] == [
        ("MIXED_CASE", ["AMOUNT"]),
        ("Mixed_Case", ["Amount"]),
    ]


def test_adapter_detail_samples_use_quoted_catalog_names() -> None:
    cursor = _Cursor([[("AMOUNT",), ("Amount",)], [("10",)]])
    samples, warnings = _adapter(cursor).fetch_metadata_sample_values(
        [{"owner": "SALES", "object_name": '"Mixed_Case"', "columns": ['"Amount"']}], 3
    )

    assert warnings == []
    assert cursor.executed[0][1] == {"owner": "SALES", "object_name": "Mixed_Case"}
    assert 'SELECT DISTINCT "Amount" FROM "SALES"."Mixed_Case"' in cursor.executed[1][0]
    assert samples == {QUOTED: {"Amount": ["10"]}}


# --- オントロジー ----------------------------------------------------------------------


def test_schema_ontology_keeps_existing_ids_for_names_unchanged_by_uppercasing() -> None:
    # origin/main (#565 時点) で生成した値。大文字化で変わらない名前の ID・fingerprint は不変。
    catalog = SchemaCatalog(
        refreshed_at="x",
        tables=[
            SchemaTable(
                owner="SALES",
                table_name="ORDERS",
                logical_name="受注",
                table_type="table",
                columns=[
                    SchemaColumn(column_name="AMOUNT", logical_name="金額", data_type="NUMBER")
                ],
            ),
            SchemaTable(
                owner="SALES",
                table_name="売上",
                logical_name="売上",
                table_type="table",
                columns=[SchemaColumn(column_name="金額", logical_name="金額", data_type="NUMBER")],
            ),
        ],
    )
    ontology = build_schema_ontology(catalog)

    assert catalog_schema_fingerprint(catalog) == (
        "f81114edb9eb0e31b8210b59199103b9be0de23d3c140461935372d33ca07c6b"
    )
    assert ontology.revision.id == "ontology_revision_f29fd3940ab30bae06460048"
    assert sorted((node.id, node.technical_name) for node in ontology.nodes) == [
        ("physical_06f80c9b12bd295fcc34b073", "SALES.ORDERS"),
        ("physical_22105490fd3f065251cc599d", "SALES.売上.金額"),
        ("physical_4316223612cb821aa1907151", "SALES"),
        ("physical_64d4ea8bd7b6fd609271d40a", "SALES.ORDERS.AMOUNT"),
        ("physical_999104f700b4433ee3303231", "SALES.売上"),
    ]


def test_schema_ontology_distinguishes_quoted_same_name_objects_and_columns() -> None:
    ontology = build_schema_ontology(_both_catalog())
    objects = {
        node.technical_name: node
        for node in ontology.nodes
        if node.kind in {OntologyNodeKind.TABLE, OntologyNodeKind.VIEW}
    }
    columns = {n.technical_name for n in ontology.nodes if n.kind == OntologyNodeKind.COLUMN}
    schemas = {n.technical_name for n in ontology.nodes if n.kind == OntologyNodeKind.SCHEMA}

    assert set(objects) == {UPPER, QUOTED, "SALES.ORDERS", '"Sales".ORDERS'}
    assert schemas == {"SALES", '"Sales"'}
    assert objects[UPPER].id == stable_ontology_id("physical", "table", "SALES", "MIXED_CASE")
    assert objects[QUOTED].id == stable_physical_id("table", "SALES", '"Mixed_Case"')
    assert objects[QUOTED].id != objects[UPPER].id
    assert objects[QUOTED].metadata["object_name"] == "Mixed_Case"
    assert {f"{QUOTED}.AMOUNT", f'{QUOTED}."Amount"', f"{UPPER}.AMOUNT"} <= columns
    assert f"{UPPER}.QUOTED_ONLY" not in columns
    assert physical_identity_part("売上") == "売上"
    assert physical_identity_part("Mixed_Case") == '"Mixed_Case"'


def test_profile_view_maps_quoted_profile_object_to_quoted_node() -> None:
    ontology = build_schema_ontology(_both_catalog())

    quoted = migrate_profile_ontology_view(
        Nl2SqlProfile(id="quoted", name="引用名", allowed_tables=[QUOTED]), ontology, strict=True
    )
    upper = migrate_profile_ontology_view(
        Nl2SqlProfile(id="upper", name="大文字", allowed_tables=[UPPER]), ontology, strict=True
    )
    node_by_id = {node.id: node for node in ontology.nodes}

    assert [(i.owner, i.object_name) for i in quoted.physical_objects] == [("SALES", "Mixed_Case")]
    assert [(i.owner, i.object_name) for i in upper.physical_objects] == [("SALES", "MIXED_CASE")]
    quoted_columns = {
        node_by_id[node_id].technical_name
        for node_id in quoted.node_ids
        if node_by_id[node_id].kind == OntologyNodeKind.COLUMN
    }
    assert quoted_columns == {f"{QUOTED}.AMOUNT", f'{QUOTED}."Amount"', f"{QUOTED}.QUOTED_ONLY"}
    assert (
        OntologyApiRuntime._profile_view_unresolved_object_warnings(  # noqa: SLF001
            Nl2SqlProfile(id="quoted", name="引用名", allowed_tables=[QUOTED]), quoted
        )
        == []
    )


def test_narrow_profile_view_selects_only_requested_quoted_object() -> None:
    ontology = build_schema_ontology(_both_catalog())
    base = migrate_profile_ontology_view(
        Nl2SqlProfile(id="both", name="両方", allowed_tables=[UPPER, QUOTED]), ontology
    )

    narrowed = OntologyApiRuntime._narrow_profile_view(  # noqa: SLF001
        base, ontology, AllowedObjects(table_names=['"SALES"."Mixed_Case"'])
    )

    assert [(i.owner, i.object_name) for i in narrowed.physical_objects] == [
        ("SALES", "Mixed_Case")
    ]


def test_ai_build_scope_resolver_includes_case_sensitive_nodes() -> None:
    ontology = build_schema_ontology(_both_catalog())
    view = migrate_profile_ontology_view(
        Nl2SqlProfile(id="both", name="両方", allowed_tables=[UPPER, QUOTED]), ontology
    )

    resolver = _ScopeResolver(ontology, view)
    context = json.loads(build_schema_context(ontology, view))
    upper = resolver.resolve_object(UPPER)
    quoted = resolver.resolve_object(QUOTED)
    column = resolver.resolve_column(f"{UPPER}.AMOUNT")
    quoted_column = resolver.resolve_column(f'{QUOTED}."Amount"')

    # AI 構築も引用規則で照合する（#573）。引用名の node を除外せず、大文字の同名表と区別する。
    assert upper is not None and upper.metadata["object_name"] == "MIXED_CASE"
    assert quoted is not None and quoted.metadata["object_name"] == "Mixed_Case"
    assert column is not None and column.metadata["object_name"] == "MIXED_CASE"
    assert quoted_column is not None and quoted_column.metadata["column_name"] == "Amount"
    assert [item["object"] for item in context["objects"]] == [QUOTED, UPPER]


# --- 列単位の許可チェック ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("columns", "sql", "safe"),
    [
        (['"Amount"'], 'SELECT "Amount" FROM SALES."Mixed_Case"', True),
        (['"Amount"'], 'SELECT m."Amount" FROM SALES."Mixed_Case" m', True),
        (['"Amount"'], 'SELECT "Mixed_Case"."Amount" FROM SALES."Mixed_Case"', True),
        (['"Amount"'], 'SELECT SALES."Mixed_Case"."Amount" FROM SALES."Mixed_Case"', True),
        (['"Amount"'], 'SELECT AMOUNT FROM SALES."Mixed_Case"', False),
        (['"Amount"'], 'SELECT amount FROM SALES."Mixed_Case"', False),
        (["AMOUNT"], 'SELECT "Amount" FROM SALES."Mixed_Case"', False),
        (["amount"], 'SELECT amount, "AMOUNT" FROM SALES."Mixed_Case"', True),
    ],
)
def test_column_scope_distinguishes_quoted_column_names(
    columns: list[str], sql: str, safe: bool
) -> None:
    service = _service()
    allowed = AllowedObjects(
        table_names=[QUOTED], columns={QUOTED: columns}, enforce_table_scope=True
    )

    analyzed = service.analyze_sql(sql, allowed, None)

    assert analyzed.safety.is_safe is safe, analyzed.safety
    if not safe:
        assert "許可されていない列" in analyzed.safety.blocked_reason


def test_column_scope_resolves_table_qualifier_when_same_name_tables_are_joined() -> None:
    service = _service()
    allowed = AllowedObjects(
        table_names=[UPPER, QUOTED],
        columns={QUOTED: ['"Amount"'], UPPER: ["AMOUNT"]},
        enforce_table_scope=True,
    )
    sql = (
        'SELECT "Mixed_Case"."Amount", MIXED_CASE.AMOUNT '
        'FROM SALES."Mixed_Case" JOIN SALES.MIXED_CASE ON 1 = 1'
    )
    blocked = 'SELECT "Mixed_Case".AMOUNT FROM SALES."Mixed_Case" JOIN SALES.MIXED_CASE ON 1 = 1'

    analyzed = service.analyze_sql(sql, allowed, None)

    assert analyzed.safety.is_safe, analyzed.safety
    assert analyzed.safety.referenced_columns == [f'{QUOTED}."Amount"', f"{UPPER}.AMOUNT"]
    assert service.analyze_sql(blocked, allowed, None).safety.is_safe is False


def test_resolved_column_scope_keeps_quoted_tokens() -> None:
    service = _service()
    service.create_profile(Nl2SqlProfile(id="quoted", name="引用名", allowed_tables=[QUOTED]))

    resolved = service.resolve_allowed_objects(
        "quoted",
        AllowedObjects(table_names=[QUOTED], columns={QUOTED: ['"Amount"', "quoted_only"]}),
    )
    direct = service.resolve_direct_sql_allowed_objects(
        AllowedObjects(table_names=[QUOTED], columns={QUOTED: ['"Amount"']})
    )

    assert resolved.columns == {QUOTED: ['"Amount"', "QUOTED_ONLY"]}
    assert direct.columns == {QUOTED: ['"Amount"']}
