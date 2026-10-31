"""DeepSec の対象識別子を object_identity の引用規則で保存・照合する (#560)。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import pytest
from pydantic import ValidationError

from app.clients.oracle_runtime import OraclePoolManager
from app.features.nl2sql.object_identity import canonical_object_part, qualified_object_name
from app.security.deepsec import (
    DeepSecService,
    _data_grant_name,
    _disable_data_grants_only_statement,
    build_data_entitlement_statements,
    build_v001_reset_statements,
)
from app.security.domain import (
    DataEntitlementRecord,
    DataEntitlementScopeFilter,
    Principal,
    RoleRecord,
    scope_filters_scope_code,
)
from app.security.schemas import DataEntitlementInput
from app.security.service import SecurityApiError, SecurityService
from app.security.store import InMemorySecurityStore
from app.settings import Settings


def _settings() -> Settings:
    return Settings.model_construct(
        oracle_user="APP_OWNER",
        oracle_password="ControlPass!123",
        app_admin_login_user_id="system_admin",
        app_admin_login_user_password="AppAdminPass123",
        oracle_dsn="test",
        oracle_driver_mode="thin",
        oracle_connection_security="walletless_tls",
        oracle_deepsec_enabled=True,
        oracle_deepsec_data_user="DEEPSEC_DATA_USER",
        oracle_deepsec_data_user_password="DeepSecret!123",
        nl2sql_persistence_mode="memory",
        nl2sql_schema_owner_allowlist=[],
        app_auth_password_min_length=12,
        app_auth_password_max_length=128,
    )


def _service(pools: object = None) -> DeepSecService:
    settings = _settings()
    return DeepSecService(
        settings,
        SecurityService(InMemorySecurityStore(), settings),
        cast(OraclePoolManager, pools),
    )


# --- 規則そのもの -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("orders", "ORDERS"),
        ("ORDERS", "ORDERS"),
        ('"ORDERS"', "ORDERS"),
        ("Mixed_Case", "MIXED_CASE"),
        ('"Mixed_Case"', '"Mixed_Case"'),
        ('"my table"', '"my table"'),
        ('"O\'Brien"', '"O\'Brien"'),
        ('"売上"', '"売上"'),
        ("A" * 128, "A" * 128),
    ],
)
def test_canonical_object_part_follows_oracle_quoting(raw: str, expected: str) -> None:
    assert canonical_object_part(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "DEPARTMENTS@REMOTE",
        "my table",
        '"a""b"',
        '" padded "',
        '"tab\there"',
        '"unterminated',
        '"' + "a" * 127 + '"',
    ],
)
def test_canonical_object_part_rejects_ambiguous_or_unsafe_names(raw: str) -> None:
    with pytest.raises(ValueError):
        canonical_object_part(raw)


# --- 入力チェックと保存キー -----------------------------------------------------


def test_input_keeps_quoted_target_and_derives_resource_code() -> None:
    entitlement = DataEntitlementInput.model_validate(
        {
            "capability": "SELECT",
            "resource_code": 'SALES."Mixed_Case"',
            "target_owner": "SALES",
            "target_object": '"Mixed_Case"',
            "column_names": ['"Amount"', "order_id", '"Amount"'],
            "scope_mode": "COLUMN_EQUALS",
            "scope_column": '"Region"',
            "scope_code": "EAST",
        }
    ).to_record("role")

    assert entitlement.target_owner == "SALES"
    assert entitlement.target_object == '"Mixed_Case"'
    assert entitlement.resource_code == 'SALES."Mixed_Case"'
    assert entitlement.column_names == ['"Amount"', "ORDER_ID"]
    assert entitlement.scope_column == '"Region"'


def test_input_keeps_legacy_keys_for_names_that_need_no_quotes() -> None:
    entitlement = DataEntitlementInput.model_validate(
        {
            "capability": "SELECT",
            "resource_code": "sales.orders",
            "target_owner": "sales",
            "target_object": '"ORDERS"',
            "column_names": ["order_id"],
            "scope_mode": "FILTERS",
            "scope_filters": [
                {"column_name": "region", "operator": "EQ", "value_type": "TEXT", "value": "E"}
            ],
        }
    ).to_record("role")

    # 以前の保存キー（大文字の単純連結）と同じ。
    assert (entitlement.target_owner, entitlement.target_object) == ("SALES", "ORDERS")
    assert entitlement.resource_code == "SALES.ORDERS"
    assert entitlement.column_names == ["ORDER_ID"]
    assert entitlement.scope_filters[0].column_name == "REGION"
    legacy_filter = DataEntitlementScopeFilter(
        column_name="REGION", operator="EQ", value_type="TEXT", value="E"
    )
    assert entitlement.scope_code == scope_filters_scope_code([legacy_filter])
    legacy = DataEntitlementRecord(
        entitlement_id="id",
        role_id="role",
        resource_code="SALES.ORDERS",
        scope_code="*",
        capability="SELECT",
        target_owner="SALES",
        target_object="ORDERS",
    )
    assert _data_grant_name(legacy).startswith("NL2SQL_DG_")
    assert qualified_object_name("SALES", "ORDERS") == "SALES.ORDERS"
    # 旧 resource code（対象なし）も従来どおり受け付ける。
    probe = DataEntitlementInput(capability="SELECT", resource_code="nl2sql_deepsec_probe")
    assert probe.resource_code == "NL2SQL_DEEPSEC_PROBE"


def test_input_does_not_fold_quoted_name_into_uppercase_table() -> None:
    quoted = DataEntitlementInput(
        capability="SELECT", target_owner="SALES", target_object='"Mixed_Case"'
    )
    upper = DataEntitlementInput(
        capability="SELECT", target_owner="SALES", target_object="Mixed_Case"
    )

    assert quoted.resource_code == 'SALES."Mixed_Case"'
    assert upper.resource_code == "SALES.MIXED_CASE"
    assert quoted.resource_code != upper.resource_code


@pytest.mark.parametrize(
    "target_object",
    ["DEPARTMENTS@REMOTE", '"a""b"', "EMP; DROP TABLE X", '"' + "a" * 127 + '"'],
)
def test_input_rejects_unsafe_target_with_guidance(target_object: str) -> None:
    with pytest.raises(ValidationError, match="二重引用符で囲みます"):
        DataEntitlementInput(capability="SELECT", target_owner="SALES", target_object=target_object)


def test_policy_signature_distinguishes_case_of_quoted_names() -> None:
    def signature(target_object: str) -> tuple[object, ...]:
        record = DataEntitlementInput(
            capability="SELECT", target_owner="SALES", target_object=target_object
        ).to_record("role")
        return SecurityService._data_entitlement_policy_signature(record)  # noqa: SLF001

    assert signature('"Mixed"') != signature('"mixed"')
    assert signature("orders") == signature('"ORDERS"')


def test_role_store_round_trip_keeps_quoted_and_uppercase_targets_apart() -> None:
    settings = _settings()
    store = InMemorySecurityStore()
    security = SecurityService(store, settings)
    security.bootstrap()
    store.roles["role-sales"] = RoleRecord(
        role_id="role-sales",
        role_code="SALES_ANALYST",
        display_name="営業分析",
        description="",
        is_built_in=False,
        archived=False,
        version=1,
        permissions=set(),
        entitlements=[],
    )
    drafts = [
        DataEntitlementInput(
            capability="SELECT",
            target_owner="SALES",
            target_object='"Mixed_Case"',
            column_names=['"Amount"'],
        ).to_record("role-sales"),
        DataEntitlementInput(
            capability="SELECT",
            target_owner="SALES",
            target_object="MIXED_CASE",
            column_names=["AMOUNT"],
        ).to_record("role-sales"),
    ]

    security.update_role_data_entitlements(
        "role-sales",
        expected_version=1,
        entitlements=list(drafts),
        actor=cast(Principal, None),
    )
    stored = store.get_role("role-sales")

    assert stored is not None
    assert [
        (item.resource_code, item.target_object, item.column_names) for item in stored.entitlements
    ] == [
        ('SALES."Mixed_Case"', '"Mixed_Case"', ['"Amount"']),
        ("SALES.MIXED_CASE", "MIXED_CASE", ["AMOUNT"]),
    ]
    grant_names = {_data_grant_name(item) for item in stored.entitlements}
    assert len(grant_names) == 2


# --- SQL / PL/SQL の識別子 ---------------------------------------------------------


def _quoted_record(**changes: Any) -> DataEntitlementRecord:
    values: dict[str, Any] = {
        "entitlement_id": "entitlement-quoted",
        "role_id": "role-sales",
        "resource_code": 'SALES."Mixed_Case"',
        "scope_code": "*",
        "capability": "SELECT",
        "target_owner": "SALES",
        "target_object": '"Mixed_Case"',
        "target_type": "TABLE",
        "column_names": ['"Amount"', "ORDER_ID"],
        "scope_mode": "ALL",
    }
    values.update(changes)
    return DataEntitlementRecord(**values)


def test_data_grant_sql_quotes_only_identifiers_that_need_quotes() -> None:
    filters = [
        DataEntitlementScopeFilter(
            column_name='"Region"', operator="EQ", value_type="TEXT", value="EAST"
        ),
        DataEntitlementScopeFilter(
            column_name="ORDER_ID", operator="GT", value_type="NUMBER", value="10"
        ),
    ]
    entitlement = _quoted_record(
        scope_mode="FILTERS",
        scope_filters=filters,
        scope_code=scope_filters_scope_code(filters),
    )

    statements = build_data_entitlement_statements(
        _settings(),
        entitlement,
        column_types={'"Region"': "VARCHAR2", "ORDER_ID": "NUMBER", '"Amount"': "NUMBER"},
    )
    sql = "\n".join(statements)

    # #1022: DATA GRANTS ONLY を先に有効にし、DB role の SELECT は最後に付ける（fail-closed）。
    assert statements[0] == 'SET USE DATA GRANTS ONLY ON SALES."Mixed_Case" ENABLED'
    assert 'AS SELECT ("Amount", ORDER_ID)' in sql
    assert 'ON SALES."Mixed_Case"' in sql
    assert 'SALES."Mixed_Case"."Region" = \'EAST\'' in sql
    assert 'SALES."Mixed_Case".ORDER_ID > 10' in sql
    assert "MIXED_CASE" not in sql
    assert statements[-1] == 'GRANT SELECT ON SALES."Mixed_Case" TO NL2SQL_APP_DB_ROLE'


def test_data_grant_sql_rejects_unvalidated_identifier() -> None:
    with pytest.raises(SecurityApiError):
        build_data_entitlement_statements(
            _settings(), _quoted_record(target_object="ORDERS X, SYS.USER$")
        )
    with pytest.raises(SecurityApiError):
        build_data_entitlement_statements(_settings(), _quoted_record(column_names=['"a""b"']))


def test_disable_statement_escapes_quoted_name_inside_plsql_literals() -> None:
    statement = _disable_data_grants_only_statement("SALES", '"O\'Brien"')

    assert "WHERE OWNER = 'SALES'" in statement
    assert "AND OBJECT_NAME = 'O''Brien'" in statement
    assert (
        "EXECUTE IMMEDIATE 'SET USE DATA GRANTS ONLY ON SALES.\"O''Brien\" DISABLED';" in statement
    )


def test_disable_statement_for_simple_name_is_unchanged() -> None:
    statement = _disable_data_grants_only_statement("SALES", "ORDERS")

    assert "WHERE OWNER = 'SALES'" in statement
    assert "AND OBJECT_NAME = 'ORDERS'" in statement
    assert "EXECUTE IMMEDIATE 'SET USE DATA GRANTS ONLY ON SALES.ORDERS DISABLED';" in statement


def test_reset_statements_disable_quoted_and_uppercase_tables_separately() -> None:
    statements = build_v001_reset_statements(
        _settings(),
        [
            _quoted_record(),
            _quoted_record(
                entitlement_id="entitlement-upper",
                resource_code="SALES.MIXED_CASE",
                target_object="MIXED_CASE",
                column_names=["AMOUNT"],
            ),
        ],
    )
    sql = "\n".join(statements)

    assert "OBJECT_NAME = 'Mixed_Case'" in sql
    assert 'SET USE DATA GRANTS ONLY ON SALES."Mixed_Case" DISABLED' in sql
    assert "OBJECT_NAME = 'MIXED_CASE'" in sql
    assert "SET USE DATA GRANTS ONLY ON SALES.MIXED_CASE DISABLED" in sql


# --- 辞書ビューとの照合 -----------------------------------------------------------


class _CatalogCursor:
    """大文字の同名表と引用名の表が並存するカタログ。bind は大文字小文字を区別して比べる。"""

    objects = [
        ("SALES", "MIXED_CASE", "TABLE", 3, "upper"),
        ("SALES", "Mixed_Case", "TABLE", 5, "quoted"),
    ]
    columns = {
        ("SALES", "MIXED_CASE"): [("AMOUNT", "NUMBER", "Y", "")],
        ("SALES", "Mixed_Case"): [
            ("Amount", "NUMBER", "Y", "quoted amount"),
            ("AMOUNT", "NUMBER", "Y", "upper amount"),
            ("Region", "VARCHAR2(10)", "Y", ""),
        ],
    }

    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, object]]] = []
        self.rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> _CatalogCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: dict[str, object] | None = None) -> None:
        binds = dict(params or {})
        self.executed.append((sql, binds))
        key = (str(binds.get("owner", "")), str(binds.get("object_name", "")))
        if "ALL_TAB_COLUMNS" in sql:
            rows = self.columns.get(key, [])
            if "COLUMN_NAME, DATA_TYPE" in sql:
                self.rows = [(name, data_type) for name, data_type, *_rest in rows]
            else:
                self.rows = list(rows)
        elif "SELECT OBJECT_TYPE" in sql:
            self.rows = [(row[2],) for row in self.objects if (row[0], row[1]) == key]
        elif "o.owner = :owner" in sql:
            self.rows = [row for row in self.objects if (row[0], row[1]) == key]
        else:
            self.rows = list(self.objects)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.rows)


class _CatalogPools:
    def __init__(self) -> None:
        self.cursor = _CatalogCursor()

    @contextmanager
    def control_connection(self) -> Iterator[Any]:
        cursor = self.cursor

        class _Connection:
            def cursor(self) -> _CatalogCursor:
                return cursor

        yield _Connection()


def test_target_objects_list_quoted_and_uppercase_tables_as_distinct_items() -> None:
    service = _service(_CatalogPools())

    page = service.target_objects()
    items = cast(list[dict[str, object]], page["items"])

    assert [(item["owner"], item["name"], item["qualified_name"]) for item in items] == [
        ("SALES", "MIXED_CASE", "SALES.MIXED_CASE"),
        ("SALES", "Mixed_Case", 'SALES."Mixed_Case"'),
    ]


def test_target_object_detail_resolves_quoted_token_without_uppercasing() -> None:
    pools = _CatalogPools()
    service = _service(pools)

    detail = service.target_object_detail(owner="SALES", object_name='"Mixed_Case"')

    assert detail["name"] == "Mixed_Case"
    assert detail["qualified_name"] == 'SALES."Mixed_Case"'
    assert detail["comment"] == "quoted"
    columns = cast(list[dict[str, object]], detail["columns"])
    assert [column["column_name"] for column in columns] == ['"Amount"', "AMOUNT", '"Region"']
    assert all(
        binds.get("object_name") == "Mixed_Case"
        for _sql, binds in pools.cursor.executed
        if "object_name" in binds
    )


def test_validation_binds_exact_name_and_does_not_mix_up_uppercase_table() -> None:
    service = _service()
    cursor = _CatalogCursor()

    columns = service._validate_data_entitlement(  # noqa: SLF001
        cursor,
        _quoted_record(column_names=['"Amount"', '"Region"']),
    )

    assert set(columns) == {'"Amount"', "AMOUNT", '"Region"'}
    assert {binds["object_name"] for _sql, binds in cursor.executed} == {"Mixed_Case"}


def test_validation_rejects_column_that_exists_only_on_uppercase_table() -> None:
    service = _service()

    with pytest.raises(SecurityApiError, match="存在しない列"):
        service._validate_data_entitlement(  # noqa: SLF001
            _CatalogCursor(),
            _quoted_record(target_object='"Mixed_Case"', column_names=['"Region"', "REGION"]),
        )


def test_validation_of_uppercase_target_never_reads_quoted_table() -> None:
    service = _service()
    cursor = _CatalogCursor()

    with pytest.raises(SecurityApiError, match="存在しない列"):
        service._validate_data_entitlement(  # noqa: SLF001
            cursor,
            _quoted_record(
                resource_code="SALES.MIXED_CASE",
                target_object="MIXED_CASE",
                column_names=['"Amount"'],
            ),
        )
    assert {binds["object_name"] for _sql, binds in cursor.executed} == {"MIXED_CASE"}


# --- scope relations -------------------------------------------------------------


class _RelationCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, object]]] = []
        self.rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: dict[str, object] | None = None) -> None:
        binds = dict(params or {})
        self.executed.append((sql, binds))
        if "FROM ALL_CONSTRAINTS" in sql:
            self.rows = [
                ("SALES", "FK_Region", "Mixed_Case", "SALES", "Regions", "Region", "Id", 1),
                ("SALES", "FK_UPPER", "MIXED_CASE", "SALES", "REGIONS", "REGION", "ID", 1),
            ]
        elif "FROM ALL_DEPENDENCIES" in sql:
            self.rows = []
        elif "FROM DBA_DATA_GRANTS" in sql:
            self.rows = [("SALES", "EXISTING", 'EXISTS (SELECT 1 FROM SALES."Mixed_Case" m)')]
        else:
            self.rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.rows)


def test_relation_catalog_keeps_quoted_relation_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.security import scope_relations

    monkeypatch.setattr(
        scope_relations,
        "scope_profiles",
        lambda: [
            {
                "id": "p",
                "object_scope_version": 1,
                "objects": ['SALES."Mixed_Case"', 'SALES."Regions"', "SALES.REGIONS"],
            }
        ],
    )
    monkeypatch.setattr(scope_relations, "_ontology_relations", lambda *args: [])
    cursor = _RelationCursor()

    catalog = scope_relations.relation_catalog(_service(), "p", 'SALES."Mixed_Case"', cursor=cursor)

    assert cursor.executed[0][1] == {"owner": "SALES", "object_name": "Mixed_Case"}
    assert catalog["objects"] == ['SALES."Regions"', "SALES.REGIONS"]
    assert [
        (relation["id"], relation["target"], relation["join_keys"])
        for relation in catalog["relations"]
    ] == [
        (
            'SALES."FK_Region"',
            'SALES."Regions"',
            [{"source_column": '"Region"', "target_column": '"Id"'}],
        )
    ]


def test_relation_dependency_detects_cycle_through_quoted_predicate_table() -> None:
    from app.security.scope_relations import validate_relation_dependency

    cursor = _RelationCursor()

    with pytest.raises(SecurityApiError, match="循環参照"):
        validate_relation_dependency(cursor, 'SALES."Mixed_Case"', 'SALES."Regions"')
    assert cursor.executed[0][1] == {"owner": "SALES", "object_name": "Regions"}
