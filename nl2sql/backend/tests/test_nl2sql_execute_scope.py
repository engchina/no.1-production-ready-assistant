"""POST /nl2sql/execute の実行スコープと row_limit 契約の回帰テスト。

- 非 system admin の principal は、許可された業務プロファイル群の許可オブジェクトの
  和集合を越えて SELECT できない(Issue: /execute がプロファイルスコープを強制しない)
- `row_limit: null` / omitted は未指定のまま保持し、fetchall 回避は adapter batch fetch で担う
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.features.nl2sql import router as nl2sql_router
from app.features.nl2sql.incremental_store import MemoryIncrementalNl2SqlRepository
from app.features.nl2sql.models import (
    AllowedObjects,
    AnalyzeRequest,
    ExecuteRequest,
    Nl2SqlProfile,
    QueryResults,
    SchemaCatalog,
    SchemaColumn,
    SchemaTable,
)
from app.features.nl2sql.service import Nl2SqlService
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.security.domain import SYSTEM_ADMIN_ROLE_CODE, Principal
from app.security.permissions import PROFILE_MANAGE_PERMISSION


def _table(name: str) -> SchemaTable:
    # deterministic runtime の mock 実行は 4 列以上を前提にしているため列を揃える。
    return SchemaTable(
        owner="APP",
        table_name=name,
        logical_name=name,
        columns=[
            SchemaColumn(column_name="ID", logical_name="ID", data_type="NUMBER", nullable=False),
            SchemaColumn(
                column_name="NAME", logical_name="名称", data_type="VARCHAR2", nullable=True
            ),
            SchemaColumn(column_name="AMOUNT", logical_name="金額", data_type="NUMBER"),
            SchemaColumn(column_name="CREATED_AT", logical_name="作成日", data_type="DATE"),
        ],
    )


def _repository() -> MemoryIncrementalNl2SqlRepository:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    catalog = SchemaCatalog(
        refreshed_at="2026-09-01T00:00:00+00:00",
        schema_fingerprint="execute-scope-v1",
        current_owner="APP",
        tables=[_table("ORDERS"), _table("INVOICES"), _table("SALARY")],
    )
    manifest = {
        (table.owner.upper(), table.table_name.upper()): catalog.refreshed_at
        for table in catalog.tables
    }
    repository.apply_schema_refresh(
        catalog=catalog,
        manifest=manifest,
        changed_keys=set(manifest),
        deleted_keys=set(),
    )
    repository.save_profile(
        Nl2SqlProfile(id="sales", name="販売", allowed_tables=["APP.ORDERS"]),
        expected_etag=None,
    )
    repository.save_profile(
        Nl2SqlProfile(id="finance", name="経理", allowed_tables=["APP.INVOICES"]),
        expected_etag=None,
    )
    return repository


def _service(repository: MemoryIncrementalNl2SqlRepository) -> Nl2SqlService:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._incremental_repository = repository  # noqa: SLF001 - white-box contract test
    service._refresh_job_repository = repository  # noqa: SLF001
    service._persistence_ready = True  # noqa: SLF001
    service._persistence_writable = True  # noqa: SLF001
    service._cache_token_poll_seconds = 0.0  # noqa: SLF001
    service._catalog = repository.load_catalog()  # noqa: SLF001
    return service


def _principal(
    allowed_profile_ids: set[str],
    *,
    admin: bool = False,
    permissions: set[str] | None = None,
) -> Principal:
    return Principal(
        user_uuid="user-1",
        login_user_id="user1",
        display_name="利用者",
        status="ACTIVE",
        force_password_change=False,
        role_codes=[SYSTEM_ADMIN_ROLE_CODE] if admin else ["ANALYST"],
        permissions={"menu.query", "nl2sql.sql.execute", *(permissions or set())},
        data_entitlements=[],
        allowed_profile_ids=set(allowed_profile_ids),
        session_id="session-1",
        csrf_token_hash="csrf",
    )


def _request(principal: Principal | None) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(principal=principal))


def test_execute_request_missing_or_null_row_limit_stays_unspecified() -> None:
    assert (
        ExecuteRequest.model_validate({"sql": "SELECT 1 FROM DUAL", "row_limit": None}).row_limit
        is None
    )
    assert ExecuteRequest(sql="SELECT 1 FROM DUAL").row_limit is None
    assert ExecuteRequest(sql="SELECT 1 FROM DUAL", row_limit=5000).row_limit == 5000


@pytest.mark.parametrize("row_limit", [0, -1, 100001])
def test_execute_request_rejects_out_of_range_row_limit(row_limit: int) -> None:
    with pytest.raises(ValidationError):
        ExecuteRequest(sql="SELECT 1 FROM DUAL", row_limit=row_limit)


def test_direct_sql_scope_without_profile_restriction_keeps_request_scope() -> None:
    service = _service(_repository())

    allowed = service.resolve_direct_sql_allowed_objects(AllowedObjects())

    assert allowed.table_names == []
    assert allowed.enforce_table_scope is False


def test_direct_sql_scope_is_limited_to_allowed_profiles() -> None:
    service = _service(_repository())

    sales_only = service.resolve_direct_sql_allowed_objects(AllowedObjects(), profile_ids={"sales"})
    assert sales_only.table_names == ["APP.ORDERS"]
    assert sales_only.enforce_table_scope is True

    both = service.resolve_direct_sql_allowed_objects(
        AllowedObjects(), profile_ids={"sales", "finance"}
    )
    assert both.table_names == ["APP.INVOICES", "APP.ORDERS"]

    intersected = service.resolve_direct_sql_allowed_objects(
        AllowedObjects(table_names=["APP.ORDERS", "APP.INVOICES", "APP.SALARY"]),
        profile_ids={"sales"},
    )
    assert intersected.table_names == ["APP.ORDERS"]

    outside = service.resolve_direct_sql_allowed_objects(
        AllowedObjects(table_names=["APP.INVOICES"]), profile_ids={"sales"}
    )
    assert outside.table_names == []
    assert outside.enforce_table_scope is True

    unknown_only = service.resolve_direct_sql_allowed_objects(
        AllowedObjects(), profile_ids={"missing-profile"}
    )
    assert unknown_only.table_names == []
    assert unknown_only.enforce_table_scope is True

    empty = service.resolve_direct_sql_allowed_objects(AllowedObjects(), profile_ids=set())
    assert empty.table_names == []
    assert empty.enforce_table_scope is True


def test_execute_sql_blocks_tables_outside_profile_scope() -> None:
    service = _service(_repository())
    allowed = service.resolve_direct_sql_allowed_objects(AllowedObjects(), profile_ids={"sales"})

    blocked, _, blocked_results = service.execute_sql("SELECT ID FROM APP.INVOICES", allowed, 10)
    assert blocked.is_safe is False
    assert "許可されていない表" in blocked.blocked_reason
    assert blocked_results.total == 0

    permitted, _, _ = service.execute_sql("SELECT ID FROM APP.ORDERS", allowed, 10)
    assert permitted.is_safe is True


def test_execute_route_scopes_non_admin_principal_to_allowed_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(_repository())
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)

    with pytest.raises(HTTPException) as denied:
        nl2sql_router.execute(
            ExecuteRequest(sql="SELECT ID FROM APP.INVOICES"),
            _request(_principal({"sales"})),  # type: ignore[arg-type]
        )
    assert denied.value.status_code == 400
    assert "許可されていない表" in str(denied.value.detail)

    with pytest.raises(HTTPException) as no_profiles:
        nl2sql_router.execute(
            ExecuteRequest(sql="SELECT ID FROM APP.ORDERS"),
            _request(_principal(set())),  # type: ignore[arg-type]
        )
    assert no_profiles.value.status_code == 400

    permitted = nl2sql_router.execute(
        ExecuteRequest(sql="SELECT ID FROM APP.ORDERS"),
        _request(_principal({"sales"})),  # type: ignore[arg-type]
    )
    assert permitted.data is not None and permitted.data.columns


def test_execute_route_scopes_profile_manager_to_all_active_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(_repository())
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)
    manager = _principal(set(), permissions={PROFILE_MANAGE_PERMISSION})

    with pytest.raises(HTTPException) as denied:
        nl2sql_router.execute(
            ExecuteRequest(sql="SELECT ID FROM APP.SALARY"),
            _request(manager),  # type: ignore[arg-type]
        )
    assert denied.value.status_code == 400
    assert "許可されていない表" in str(denied.value.detail)

    permitted = nl2sql_router.execute(
        ExecuteRequest(sql="SELECT ID FROM APP.INVOICES"),
        _request(manager),  # type: ignore[arg-type]
    )
    assert permitted.data is not None and permitted.data.columns


def test_analyze_route_scopes_non_admin_principal_to_allowed_profiles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(_repository())
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)

    blocked = nl2sql_router.analyze(
        AnalyzeRequest(
            sql="SELECT ID FROM APP.SALARY",
            allowed_objects=AllowedObjects(table_names=["APP.SALARY"]),
        ),
        _request(_principal({"sales"})),  # type: ignore[arg-type]
    )
    assert blocked.data is not None
    assert blocked.data.safety.is_safe is False
    assert "許可されていない表" in blocked.data.safety.blocked_reason

    permitted = nl2sql_router.analyze(
        AnalyzeRequest(sql="SELECT ID FROM APP.ORDERS"),
        _request(_principal({"sales"})),  # type: ignore[arg-type]
    )
    assert permitted.data is not None
    assert permitted.data.safety.is_safe is True


def test_execute_route_keeps_request_scope_for_admin_and_unauthenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(_repository())
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)

    admin = nl2sql_router.execute(
        ExecuteRequest(sql="SELECT ID FROM APP.SALARY"),
        _request(_principal(set(), admin=True)),  # type: ignore[arg-type]
    )
    assert admin.data is not None and admin.data.columns

    unauthenticated = nl2sql_router.execute(
        ExecuteRequest(sql="SELECT ID FROM APP.SALARY"),
        _request(None),  # type: ignore[arg-type]
    )
    assert unauthenticated.data is not None and unauthenticated.data.columns


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT ID FROM SALARY WHERE EXISTS "
        "(WITH SALARY AS (SELECT ID FROM APP.ORDERS) SELECT 1 FROM SALARY)",
        "SELECT ID FROM PLATFORM_USERS WHERE EXISTS "
        "(WITH PLATFORM_USERS AS (SELECT ID FROM APP.ORDERS) "
        "SELECT 1 FROM PLATFORM_USERS)",
        'WITH "salary" AS (SELECT ID FROM APP.ORDERS) SELECT ID FROM SALARY',
    ],
)
def test_execute_does_not_hide_physical_tables_behind_cte_names(
    monkeypatch: pytest.MonkeyPatch, sql: str
) -> None:
    service = _service(_repository())
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)
    monkeypatch.setattr(service, "_use_oracle_runtime", lambda: True)

    def forbidden_execute(*_args: object) -> None:
        pytest.fail("拒否対象の SQL が Oracle adapter に到達した")

    monkeypatch.setattr(service._oracle_adapter, "execute_select", forbidden_execute)
    with pytest.raises(HTTPException) as denied:
        nl2sql_router.execute(
            ExecuteRequest(sql=sql, row_limit=100),
            _request(_principal({"sales"})),  # type: ignore[arg-type]
        )
    assert denied.value.status_code == 400


@pytest.mark.parametrize(
    "sql",
    [
        "WITH sales AS (SELECT ID FROM APP.ORDERS) SELECT ID FROM SALES",
        "SELECT ID FROM (WITH sales AS (SELECT ID FROM APP.ORDERS) SELECT ID FROM SALES)",
        "WITH x AS (SELECT ID FROM APP.ORDERS), y AS (SELECT ID FROM x) SELECT ID FROM y",
        "WITH x (id) AS (SELECT ID FROM APP.ORDERS UNION ALL "
        "SELECT id + 1 FROM x WHERE id < 3) SELECT id FROM x",
    ],
)
def test_direct_sql_keeps_valid_cte_scopes(sql: str) -> None:
    service = _service(_repository())
    allowed = service.resolve_direct_sql_allowed_objects(AllowedObjects(), profile_ids={"sales"})
    analysis = service.analyze_sql(sql, allowed, 100)
    assert analysis.safety.is_safe, analysis.safety.blocked_reason
    assert analysis.safety.referenced_tables == ["APP.ORDERS"]


@pytest.mark.parametrize("alias", ["x", "p", '"x"'])
@pytest.mark.parametrize(
    "operation",
    ["PIVOT (COUNT(*) FOR ID IN (1))", "UNPIVOT (v FOR k IN (ID, AMOUNT))"],
)
def test_execute_accepts_pivot_cte_with_result_alias(
    monkeypatch: pytest.MonkeyPatch, alias: str, operation: str
) -> None:
    service = _service(_repository())
    sql = f"WITH x AS (SELECT ID, AMOUNT FROM APP.ORDERS) SELECT * FROM x {operation} {alias}"
    allowed = service.resolve_direct_sql_allowed_objects(AllowedObjects(), profile_ids={"sales"})
    analysis = service.analyze_sql(sql, allowed, 100)
    assert analysis.safety.is_safe, analysis.safety.blocked_reason
    assert analysis.safety.referenced_tables == ["APP.ORDERS"]
    execute = Mock(return_value=QueryResults(columns=[], rows=[], total=0))
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)
    monkeypatch.setattr(service, "_use_oracle_runtime", lambda: True)
    monkeypatch.setattr(service._oracle_adapter, "execute_select", execute)
    nl2sql_router.execute(
        ExecuteRequest(sql=sql, row_limit=100),
        _request(_principal({"sales"})),  # type: ignore[arg-type]
    )
    execute.assert_called_once_with(sql, 100)


@pytest.mark.parametrize(
    "sql",
    [
        "WITH salary AS (SELECT ID FROM APP.ORDERS) "
        "SELECT * FROM APP.SALARY PIVOT (COUNT(*) FOR ID IN (1)) salary",
        "WITH x AS (SELECT ID FROM APP.ORDERS) "
        "SELECT * FROM SALARY PIVOT (COUNT(*) FOR ID IN (1)) x",
        "SELECT * FROM SALARY PIVOT (COUNT(*) FOR ID IN (1)) salary "
        "WHERE EXISTS (WITH salary AS (SELECT ID FROM APP.ORDERS) SELECT 1 FROM salary)",
    ],
)
def test_pivot_alias_does_not_hide_unauthorized_physical_table(
    monkeypatch: pytest.MonkeyPatch, sql: str
) -> None:
    service = _service(_repository())
    execute = Mock()
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)
    monkeypatch.setattr(service, "_use_oracle_runtime", lambda: True)
    monkeypatch.setattr(service._oracle_adapter, "execute_select", execute)
    with pytest.raises(HTTPException) as denied:
        nl2sql_router.execute(
            ExecuteRequest(sql=sql, row_limit=100),
            _request(_principal({"sales"})),  # type: ignore[arg-type]
        )
    assert denied.value.status_code == 400
    execute.assert_not_called()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT DBURITYPE('/APP/SALARY').getclob() FROM APP.ORDERS",
        "SELECT SYS.DBURITYPE('/APP/SALARY').getclob() x FROM APP.ORDERS",
        "SELECT XDBURITYPE('/APP/SALARY').getclob() FROM APP.ORDERS",
        "SELECT sys.xdburitype('/APP/SALARY').getClob() FROM APP.ORDERS",
        "SELECT URIFACTORY.GETURI('/APP/SALARY').getclob() FROM APP.ORDERS",
        "SELECT DBMS_CLOUD_AI.GENERATE(prompt => 'list salary', "
        "profile_name => 'NL2SQL_FINANCE', action => 'runsql') FROM APP.ORDERS",
        "SELECT DBMS_CLOUD.SEND_REQUEST('c', 'http://x', 'GET') FROM APP.ORDERS",
        "SELECT C##X.DBMS_CLOUD_PIPELINE.GET_PIPELINE_STATUS('p') FROM APP.ORDERS",
        "SELECT APEX_WEB_SERVICE.MAKE_REST_REQUEST(p_url => 'http://x', "
        "p_http_method => 'GET') FROM APP.ORDERS",
        "SELECT UTL_MAIL.SEND('a', 'b') FROM APP.ORDERS",
        "SELECT * FROM XMLTABLE('/a' PASSING XMLTYPE(DBURITYPE('/APP/SALARY').getclob()))",
        "SELECT * FROM JSON_TABLE(UTL_HTTP.REQUEST('http://x'), '$' COLUMNS (a PATH '$'))",
    ],
)
def test_dangerous_functions_cannot_bypass_profile_table_scope(sql: str) -> None:
    # 関数の中で読む表は SQL の表参照に現れないため、denylist で拒否する。
    service = _service(_repository())
    allowed = service.resolve_direct_sql_allowed_objects(AllowedObjects(), profile_ids={"sales"})

    analysis = service.analyze_sql(sql, allowed, 10)

    assert analysis.safety.is_safe is False
    assert "危険な Oracle 関数" in analysis.safety.blocked_reason


@pytest.mark.parametrize(
    ("sql", "referenced"),
    [
        (
            "SELECT j.a FROM APP.ORDERS o, JSON_TABLE(o.NAME, '$' COLUMNS (a PATH '$.a')) j",
            ["APP.ORDERS"],
        ),
        ("SELECT COLUMN_VALUE FROM TABLE(SYS.ODCINUMBERLIST(1, 2, 3))", []),
        (
            "SELECT x.v FROM APP.ORDERS o, "
            "XMLTABLE('/r' PASSING XMLTYPE('<r/>') COLUMNS v VARCHAR2(10) PATH 'v') x",
            ["APP.ORDERS"],
        ),
    ],
)
def test_table_functions_are_not_scope_checked_as_named_tables(
    sql: str, referenced: list[str]
) -> None:
    # 名前の無い表参照（表関数）で識別子エラーにせず、名前のある表だけをスコープ判定する。
    service = _service(_repository())
    allowed = service.resolve_direct_sql_allowed_objects(AllowedObjects(), profile_ids={"sales"})

    analysis = service.analyze_sql(sql, allowed, 10)

    assert analysis.safety.is_safe, analysis.safety.blocked_reason
    assert analysis.safety.referenced_tables == referenced


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM TABLE(SELECT ID FROM APP.SALARY)",
        "SELECT * FROM TABLE(CAST(MULTISET(SELECT ID FROM APP.SALARY) AS SYS.ODCINUMBERLIST))",
        "SELECT j.a FROM APP.SALARY s, JSON_TABLE(s.NAME, '$' COLUMNS (a PATH '$.a')) j",
    ],
)
def test_tables_inside_table_functions_are_still_scope_checked(sql: str) -> None:
    service = _service(_repository())
    allowed = service.resolve_direct_sql_allowed_objects(AllowedObjects(), profile_ids={"sales"})

    analysis = service.analyze_sql(sql, allowed, 10)

    assert analysis.safety.is_safe is False
    assert "許可されていない表" in analysis.safety.blocked_reason
    assert "APP.SALARY" in analysis.safety.referenced_tables


def test_execute_route_accepts_json_table_within_profile_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(_repository())
    sql = "SELECT j.a FROM APP.ORDERS o, JSON_TABLE(o.NAME, '$' COLUMNS (a PATH '$.a')) j"
    execute = Mock(return_value=QueryResults(columns=[], rows=[], total=0))
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)
    monkeypatch.setattr(service, "_use_oracle_runtime", lambda: True)
    monkeypatch.setattr(service._oracle_adapter, "execute_select", execute)

    nl2sql_router.execute(
        ExecuteRequest(sql=sql, row_limit=100),
        _request(_principal({"sales"})),  # type: ignore[arg-type]
    )

    execute.assert_called_once_with(sql, 100)


def test_analyze_route_returns_400_for_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(_repository())
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)

    def invalid_identifier(*_args: object, **_kwargs: object) -> None:
        raise ValueError(": Oracle 識別子が不正です。")

    monkeypatch.setattr(service, "analyze_sql", invalid_identifier)
    with pytest.raises(HTTPException) as invalid:
        nl2sql_router.analyze(
            AnalyzeRequest(sql="SELECT ID FROM APP.ORDERS"),
            _request(_principal({"sales"})),  # type: ignore[arg-type]
        )
    assert invalid.value.status_code == 400
    assert "Oracle 識別子が不正" in str(invalid.value.detail)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) FROM APP.ORDERS",
        "SELECT ID, AMOUNT * 1.1 FROM APP.ORDERS",
        "SELECT ID FROM APP.ORDERS WHERE AMOUNT * 2 > 10",
    ],
)
def test_column_scope_does_not_treat_asterisk_expressions_as_select_star(sql: str) -> None:
    service = _service(_repository())
    allowed = AllowedObjects(
        table_names=["APP.ORDERS"],
        columns={"APP.ORDERS": ["ID", "AMOUNT"]},
        enforce_table_scope=True,
    )

    analysis = service.analyze_sql(sql, allowed, 10)

    assert analysis.safety.is_safe, analysis.safety.blocked_reason
    assert not any("SELECT *" in warning for warning in analysis.safety.warnings)


@pytest.mark.parametrize("sql", ["SELECT * FROM APP.ORDERS", "SELECT o.* FROM APP.ORDERS o"])
def test_column_scope_still_blocks_select_star(sql: str) -> None:
    service = _service(_repository())
    allowed = AllowedObjects(
        table_names=["APP.ORDERS"],
        columns={"APP.ORDERS": ["ID", "AMOUNT"]},
        enforce_table_scope=True,
    )

    analysis = service.analyze_sql(sql, allowed, 10)

    assert analysis.safety.is_safe is False
    assert "許可されていない列" in analysis.safety.blocked_reason


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SYSDATE FROM DUAL",
        "SELECT 1 FROM SYS.DUAL",
        "select sysdate from dual",
        "SELECT o.ID FROM APP.ORDERS o CROSS JOIN DUAL",
    ],
)
def test_dual_is_always_allowed_under_profile_scope(sql: str) -> None:
    service = _service(_repository())
    allowed = service.resolve_direct_sql_allowed_objects(AllowedObjects(), profile_ids={"sales"})

    analysis = service.analyze_sql(sql, allowed, 10)

    assert analysis.safety.is_safe, analysis.safety.blocked_reason
    assert "APP.DUAL" not in analysis.safety.referenced_tables


def test_dual_in_other_schema_is_still_scope_checked() -> None:
    service = _service(_repository())
    allowed = service.resolve_direct_sql_allowed_objects(AllowedObjects(), profile_ids={"sales"})

    analysis = service.analyze_sql("SELECT 1 FROM APP.DUAL", allowed, 10)

    assert analysis.safety.is_safe is False
    assert "許可されていない表" in analysis.safety.blocked_reason


@pytest.mark.parametrize(
    ("sql", "name"),
    [
        ("SELECT ID FROM APP.ORDERS WHERE NAME = :name", ":name"),
        ("SELECT ID FROM APP.ORDERS WHERE ID = :ID AND AMOUNT > :min_amount", ":min_amount"),
    ],
)
def test_bind_variables_are_rejected_before_execution(sql: str, name: str) -> None:
    service = _service(_repository())
    allowed = service.resolve_direct_sql_allowed_objects(AllowedObjects(), profile_ids={"sales"})

    analysis = service.analyze_sql(sql, allowed, 10)

    assert analysis.safety.is_safe is False
    assert "バインド変数" in analysis.safety.blocked_reason
    assert name in analysis.safety.blocked_reason


def test_colon_inside_literal_is_not_a_bind_variable() -> None:
    service = _service(_repository())
    allowed = service.resolve_direct_sql_allowed_objects(AllowedObjects(), profile_ids={"sales"})

    analysis = service.analyze_sql(
        "SELECT TO_CHAR(CREATED_AT, 'HH24:MI') FROM APP.ORDERS WHERE NAME = ':name'",
        allowed,
        10,
    )

    assert analysis.safety.is_safe, analysis.safety.blocked_reason


@pytest.mark.parametrize(
    ("sql", "name"),
    [
        ("SELECT APP.FN(ID) FROM APP.ORDERS", "APP.FN"),
        ("SELECT FN(ID) FROM APP.ORDERS", "FN"),
        ("SELECT * FROM TABLE(APP.READ_SALARY())", "APP.READ_SALARY"),
        ("SELECT o.NAME.getclobval() FROM APP.ORDERS o", "O.NAME.GETCLOBVAL"),
        (
            "SELECT x.v FROM APP.ORDERS o, XMLTABLE("
            "'for $i in fn:collection(\"oradb:/APP/SALARY\") return $i' "
            "COLUMNS v VARCHAR2(10) PATH 'v') x",
            "XMLTABLE",
        ),
        ("SELECT XMLQUERY('1') FROM APP.ORDERS", "XMLQUERY"),
    ],
)
def test_unregistered_user_functions_are_rejected(sql: str, name: str) -> None:
    # 利用者定義・package の関数や XQuery の collection() は表スコープ判定を経ずにデータを読める。
    service = _service(_repository())
    allowed = service.resolve_direct_sql_allowed_objects(AllowedObjects(), profile_ids={"sales"})

    analysis = service.analyze_sql(sql, allowed, 10)

    assert analysis.safety.is_safe is False
    assert analysis.safety.blocked_reason.startswith(f"{name}: 許可されていない関数です。")


def test_oracle_builtin_functions_stay_allowed() -> None:
    service = _service(_repository())
    allowed = service.resolve_direct_sql_allowed_objects(AllowedObjects(), profile_ids={"sales"})
    sql = (
        "SELECT LISTAGG(NAME, ',') WITHIN GROUP (ORDER BY NAME), REGEXP_SUBSTR(NAME, 'a'), "
        "ADD_MONTHS(CREATED_AT, 1), NVL2(NAME, 1, 2), DECODE(ID, 1, 'a', 'b'), "
        "TO_CHAR(CREATED_AT, 'YYYY'), SYS_CONTEXT('USERENV', 'LANG'), TRUNC(CREATED_AT), "
        "NUMTODSINTERVAL(1, 'DAY'), MONTHS_BETWEEN(CREATED_AT, SYSDATE), COUNT(*) "
        "FROM APP.ORDERS GROUP BY NAME, CREATED_AT, ID"
    )

    analysis = service.analyze_sql(sql, allowed, 10)

    assert analysis.safety.is_safe, analysis.safety.blocked_reason


def test_registered_user_function_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.settings import get_settings

    monkeypatch.setattr(get_settings(), "nl2sql_allowed_user_functions", " app.fn , UTIL.LABEL")
    service = _service(_repository())
    allowed = service.resolve_direct_sql_allowed_objects(AllowedObjects(), profile_ids={"sales"})

    assert service.analyze_sql("SELECT APP.FN(ID) FROM APP.ORDERS", allowed, 10).safety.is_safe
    blocked = service.analyze_sql("SELECT APP.OTHER(ID) FROM APP.ORDERS", allowed, 10)
    assert blocked.safety.blocked_reason.startswith("APP.OTHER: 許可されていない関数です。")
