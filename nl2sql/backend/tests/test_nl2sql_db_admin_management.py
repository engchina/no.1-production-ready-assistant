"""テーブル/ビュー/データ管理(SQL Assist 移植)API のテスト。"""

from __future__ import annotations

import base64
import csv
import importlib
import io
import json
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.features.nl2sql.enterprise_ai_client import EnterpriseAiDirectError
from app.features.nl2sql.models import (
    AllowedObjects,
    AnnotationApplyRequest,
    CommentApplyRequest,
    CsvImportColumn,
    DbAdminAiAnalysisRequest,
    DbAdminCsvUploadRequest,
    DbAdminDataPreviewRequest,
    DbAdminDropTableRequest,
    DbAdminDropViewRequest,
    DbAdminExecuteRequest,
    DbAdminImportTabularRequest,
    DbAdminJoinWhereRequest,
    DbAdminStatementsRequest,
    DbAdminTruncateTableRequest,
    DomainDefinition,
    DomainInventoryRequest,
    MetadataSqlGenerateRequest,
    MetadataSqlSampleRequest,
    QueryResults,
    SampleDataMutationRequest,
    SampleDataStep,
    SchemaCatalog,
    SchemaColumn,
    SchemaTable,
)
from app.features.nl2sql.oracle_adapter import (
    SELECT_FETCH_BATCH_SIZE,
    OracleAdapterError,
    OracleNl2SqlAdapter,
    TabularImportValidationError,
    _domain_column_type,
    _flexible_date_value,
)
from app.features.nl2sql.router import db_admin_import_tabular, db_admin_upload_csv
from app.features.nl2sql.service import (
    DbAdminOperationFailed,
    Nl2SqlPersistenceUnavailable,
    Nl2SqlService,
    SchemaRefreshMutationSync,
    _admin_statement_type,
    _db_admin_error,
    _normalize_oracle_sql_text,
    _schema_refresh_targets_for_statements,
    _split_sql_statements,
    _statements_change_schema,
)
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.security.request_actor import actor_scope
from app.settings import get_settings


class FakeEnterpriseAiClient:
    def __init__(self, *responses: str | Exception, configured: bool = True) -> None:
        self.responses = list(responses)
        self.configured = configured
        self.calls: list[dict[str, Any]] = []

    def is_configured(self) -> bool:
        return self.configured

    def model_id(self) -> str:
        return "fake-enterprise-ai"

    def generate(
        self,
        *,
        prompt: str,
        context: str,
        system_prompt: str,
        timeout_seconds: float | None = None,
        max_output_tokens: int | None = None,
        max_retries: int | None = None,
        response_format: Mapping[str, Any] | None = None,
    ) -> str:
        del timeout_seconds, max_retries
        self.calls.append({"prompt": prompt, "context": context, "system_prompt": system_prompt})
        if not self.responses:
            return ""
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        assert response_format is not None
        assert response_format["strict"] is True
        name = response_format["name"]
        field = {
            "SqlOutput": "sql",
            "AnalysisOutput": "analysis",
            "StructureOutput": "logical_structure",
        }[name]
        payload = {field: response}
        if field == "sql":
            payload["explanation"] = ""
        return json.dumps(payload, ensure_ascii=False)


class _FakeStatementsAdapter:
    """execute_admin_statements の結果を固定で返す fake adapter。"""

    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.results = results
        self.calls: list[tuple[list[str], bool]] = []

    def execute_admin_statements(
        self,
        statements: list[str],
        *,
        atomic: bool = True,
        ignored_error_codes: frozenset[str] = frozenset(),
    ) -> list[dict[str, Any]]:
        _ = ignored_error_codes
        self.calls.append((statements, atomic))
        return self.results

    def fetch_catalog(self, *, include_samples: bool = True) -> SchemaCatalog:
        del include_samples
        return SchemaCatalog(refreshed_at="2026-07-10T00:00:00+00:00", tables=[])


class _FakeAdminSqlAdapter(_FakeStatementsAdapter):
    """Admin SQL の SELECT/data plane と statement/control plane を分けて記録する。"""

    def __init__(
        self,
        *,
        select_result: QueryResults | None = None,
        select_error: OracleAdapterError | None = None,
        statement_results: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(statement_results or [])
        self.select_result = select_result or QueryResults(
            columns=["ID"],
            rows=[{"ID": 1}],
            total=1,
            execution_context="oracle_data_plane",
        )
        self.select_error = select_error
        self.select_calls: list[tuple[str, int | None]] = []

    def execute_select(self, sql: str, max_rows: int | None) -> QueryResults:
        self.select_calls.append((sql, max_rows))
        if self.select_error is not None:
            raise self.select_error
        return self.select_result


class _FakeObjectTypeAdapter(_FakeStatementsAdapter):
    def __init__(self, object_type: str | None) -> None:
        super().__init__([])
        self.object_type = object_type
        self.object_type_calls: list[str] = []

    def find_db_admin_object_type(self, object_name: str, owner: str = "") -> str | None:
        self.object_type_calls.append(f"{owner}.{object_name}" if owner else object_name)
        return self.object_type


class _FakeMetadataSamplesAdapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[list[dict[str, Any]], int]] = []

    def fetch_metadata_sample_values(
        self, targets: list[dict[str, Any]], sample_limit: int
    ) -> tuple[dict[str, dict[str, list[str]]], list[str]]:
        self.calls.append((targets, sample_limit))
        if self.fail:
            raise OracleAdapterError("接続できません")
        return {"EMPLOYEE": {"EMPLOYEE_NAME": ["山田", "佐藤"]}}, []


def _issue71_create_table_sql() -> str:
    return (
        'CREATE TABLE "ABC"\n'
        '\u00a0\u00a0\u00a0(\t"EMPLID" VARCHAR2(11) COLLATE "USING_NLS_COMP" NOT NULL ENABLE,\n'
        '\t"EMPL_RCD" NUMBER(3,0) NOT NULL ENABLE,\n'
        '\t"EFFDT" DATE NOT NULL ENABLE,\n'
        '\t"EFFSEQ" NUMBER(3,0) NOT NULL ENABLE,\n'
        '\t"COMP_RATECD" VARCHAR2(20) COLLATE "USING_NLS_COMP" NOT NULL ENABLE,\n'
        '\t"COMP_RATE" NUMBER(12,2),\n'
        '\t"COMP_PERCENT" NUMBER(7,4),\n'
        '\t"SAL_ADMIN_PLAN" VARCHAR2(20) COLLATE "USING_NLS_COMP" NOT NULL ENABLE,\n'
        '\t"GRADE" VARCHAR2(10) COLLATE "USING_NLS_COMP" NOT NULL ENABLE,\n'
        '\t"CURRENCY_CD" VARCHAR2(3) COLLATE "USING_NLS_COMP" NOT NULL ENABLE,\n'
        '\t PRIMARY KEY ("EMPLID", "EMPL_RCD", "EFFDT", "EFFSEQ", "COMP_RATECD")\n'
        "\u00a0\u00a0\u00a0)\u00a0 "
    )


class _OracleRuntimeService(Nl2SqlService):
    def __init__(self, adapter: Any) -> None:
        super().__init__(store=MemoryNl2SqlStore())
        self._oracle_adapter = adapter

    def _use_oracle_runtime(self) -> bool:
        return True


class _FakeCsvUploadAdapter:
    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def upload_csv_to_existing_table(
        self,
        *,
        table_name: str,
        owner: str = "",
        columns: list[CsvImportColumn],
        rows: list[dict[str, str | None]],
        truncate: bool,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "table_name": table_name,
                "owner": owner,
                "columns": columns,
                "rows": rows,
                "truncate": truncate,
            }
        )
        return self.result


def _import_sample(service: Nl2SqlService) -> None:
    service.import_sample_data(
        SampleDataMutationRequest(
            step=SampleDataStep.ALL,
            confirmation="ADMIN_EXECUTE",
        )
    )


def test_statement_policy_table_ddl_accepts_and_blocks() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    allowed = service.execute_db_admin_statements(
        DbAdminStatementsRequest(
            sql=(
                "CREATE TABLE T1 (ID NUMBER);\n"
                "CREATE GLOBAL TEMPORARY TABLE T2 (ID NUMBER);\n"
                "COMMENT ON TABLE T1 IS 'テスト';\n"
                "COMMENT ON COLUMN T1.ID IS 'ID';\n"
                "DROP TABLE T1"
            ),
            policy="table_ddl",
        )
    )
    assert allowed.executed is False
    assert [item.status for item in allowed.statements] == ["confirmation_required"] * 5

    blocked = service.execute_db_admin_statements(
        DbAdminStatementsRequest(
            sql="CREATE TABLE T1 (ID NUMBER); GRANT SELECT ON T1 TO PUBLIC",
            policy="table_ddl",
        )
    )
    assert blocked.executed is False
    assert blocked.statements[0].status == "blocked"
    assert blocked.statements[1].status == "blocked"
    assert "禁止された操作" in blocked.statements[1].error_message
    assert any("禁止された操作" in warning for warning in blocked.warnings)


def test_admin_mutation_submits_schema_job_only_for_schema_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _FakeStatementsAdapter(
        [
            {
                "index": 1,
                "statement_type": "DML",
                "status": "success",
                "sql": "",
            }
        ]
    )
    service = _OracleRuntimeService(adapter)
    submitted = 0
    submitted_targets: list[list[tuple[str, str, str]]] = []

    def submit(**kwargs: Any) -> SchemaRefreshMutationSync:
        nonlocal submitted
        submitted += 1
        submitted_targets.append(
            [
                (target.owner, target.object_name, target.expected_state)
                for target in kwargs["target_objects"]
            ]
        )
        return SchemaRefreshMutationSync(job_id="refresh-job-1")

    monkeypatch.setattr(service, "_submit_schema_refresh_after_admin_mutation", submit)

    dml = service.execute_db_admin_statements(
        DbAdminStatementsRequest(
            sql="UPDATE ORDERS SET STATUS = 'DONE'",
            policy="data_dml",
            confirmation="ADMIN_EXECUTE",
        )
    )
    ddl = service.execute_db_admin_statements(
        DbAdminStatementsRequest(
            sql="CREATE TABLE ORDERS_ARCHIVE (ID NUMBER)",
            policy="table_ddl",
            confirmation="ADMIN_EXECUTE",
        )
    )

    assert dml.executed is True
    assert dml.schema_refresh_job_id == ""
    assert ddl.executed is True
    assert ddl.schema_refresh_job_id == "refresh-job-1"
    assert submitted == 1
    assert submitted_targets == [[("APP", "ORDERS_ARCHIVE", "present")]]


def test_admin_plsql_dynamic_sql_is_blocked_before_oracle() -> None:
    adapter = _FakeStatementsAdapter(
        [
            {
                "index": 1,
                "statement_type": "PLSQL",
                "status": "success",
                "sql": "BEGIN EXECUTE IMMEDIATE 'CREATE TABLE X (ID NUMBER)'; END;",
            }
        ]
    )
    service = _OracleRuntimeService(adapter)

    result = service.execute_db_admin_sql(
        DbAdminExecuteRequest(
            sql="BEGIN EXECUTE IMMEDIATE 'CREATE TABLE X (ID NUMBER)'; END;",
            confirmation="ADMIN_EXECUTE",
        )
    )

    assert result.executed is False
    assert adapter.calls == []
    assert result.statements[0].status == "blocked"
    assert "PL/SQL の動的 SQL" in result.statements[0].error_message
    assert result.schema_refresh_job_id == ""
    assert result.schema_refresh_required is False
    assert result.schema_refresh_reason_code == ""


def test_admin_plsql_dynamic_sql_concatenated_system_object_is_blocked() -> None:
    adapter = _FakeAdminSqlAdapter()
    service = _OracleRuntimeService(adapter)

    result = service.execute_db_admin_sql(
        DbAdminExecuteRequest(
            sql="BEGIN EXECUTE IMMEDIATE 'DROP TABLE NL2' || 'SQL_APP_USERS'; END;",
            confirmation="ADMIN_EXECUTE",
        )
    )

    assert result.executed is False
    assert result.runtime == "oracle"
    assert result.execution_context == "admin_control_plane"
    assert result.statements[0].statement_type == "PLSQL"
    assert result.statements[0].status == "blocked"
    assert "PL/SQL の動的 SQL" in result.statements[0].error_message
    assert adapter.select_calls == []
    assert adapter.calls == []


def test_admin_statements_dynamic_sql_is_blocked_before_policy_and_oracle() -> None:
    adapter = _FakeStatementsAdapter([])
    service = _OracleRuntimeService(adapter)

    result = service.execute_db_admin_statements(
        DbAdminStatementsRequest(
            sql="BEGIN DBMS_SQL.PARSE(v_cursor, 'DROP TABLE NL2' || 'SQL_APP_USERS', 1); END;",
            policy="table_ddl",
            confirmation="ADMIN_EXECUTE",
        )
    )

    assert result.executed is False
    assert result.statements[0].statement_type == "PLSQL"
    assert result.statements[0].status == "blocked"
    assert "PL/SQL の動的 SQL" in result.statements[0].error_message
    assert adapter.calls == []


def test_statement_policy_view_ddl_and_data_dml() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    view_ok = service.execute_db_admin_statements(
        DbAdminStatementsRequest(
            sql=(
                "CREATE OR REPLACE FORCE EDITIONABLE VIEW V1 AS SELECT 1 AS C FROM DUAL;\n"
                "COMMENT ON TABLE V1 IS 'ビュー';\n"
                "DROP VIEW V1"
            ),
            policy="view_ddl",
        )
    )
    assert [item.status for item in view_ok.statements] == ["confirmation_required"] * 3

    view_ng = service.execute_db_admin_statements(
        DbAdminStatementsRequest(sql="CREATE TABLE T1 (ID NUMBER)", policy="view_ddl")
    )
    assert view_ng.statements[0].status == "blocked"

    dml_ok = service.execute_db_admin_statements(
        DbAdminStatementsRequest(
            sql=(
                "INSERT INTO T1 VALUES (1);\nUPDATE T1 SET ID = 2;\n"
                "DELETE FROM T1;\nMERGE INTO T1 USING DUAL ON (1=1) "
                "WHEN MATCHED THEN UPDATE SET ID = 3;\nTRUNCATE TABLE T1"
            ),
            policy="data_dml",
        )
    )
    assert [item.status for item in dml_ok.statements] == ["confirmation_required"] * 5

    select_ng = service.execute_db_admin_statements(
        DbAdminStatementsRequest(sql="SELECT * FROM T1", policy="data_dml")
    )
    assert select_ng.statements[0].status == "blocked"


def test_db_admin_statements_block_nl2sql_system_objects_before_oracle() -> None:
    adapter = _FakeStatementsAdapter(
        [
            {
                "index": 1,
                "statement_type": "DDL",
                "status": "success",
                "sql": "",
            }
        ]
    )
    service = _OracleRuntimeService(adapter)

    create_table = service.execute_db_admin_statements(
        DbAdminStatementsRequest(
            sql="CREATE TABLE NL2SQL_SCHEMA_OBJECTS (ID NUMBER)",
            policy="table_ddl",
            confirmation="ADMIN_EXECUTE",
        )
    )
    create_view_ref = service.execute_db_admin_statements(
        DbAdminStatementsRequest(
            sql="CREATE OR REPLACE VIEW V_SYSTEM AS SELECT * FROM NL2SQL_SCHEMA_OBJECTS",
            policy="view_ddl",
            confirmation="ADMIN_EXECUTE",
        )
    )
    dml = service.execute_db_admin_statements(
        DbAdminStatementsRequest(
            sql="UPDATE NL2SQL_SCHEMA_OBJECTS SET ID = 1",
            policy="data_dml",
            confirmation="ADMIN_EXECUTE",
        )
    )

    assert create_table.statements[0].status == "blocked"
    assert create_view_ref.statements[0].status == "blocked"
    assert dml.statements[0].status == "blocked"
    assert all(
        "システムテーブル管理" in item.error_message
        for item in [
            create_table.statements[0],
            create_view_ref.statements[0],
            dml.statements[0],
        ]
    )
    assert adapter.calls == []


def test_db_admin_execute_blocks_nl2sql_select_dml_and_plsql_before_oracle() -> None:
    adapter = _FakeAdminSqlAdapter()
    service = _OracleRuntimeService(adapter)

    select_result = service.execute_db_admin_sql(
        DbAdminExecuteRequest(sql="SELECT * FROM APP.NL2SQL_SCHEMA_OBJECTS", row_limit=10)
    )
    dml_result = service.execute_db_admin_sql(
        DbAdminExecuteRequest(
            sql="DELETE FROM NL2SQL_SCHEMA_OBJECTS",
            confirmation="ADMIN_EXECUTE",
        )
    )
    plsql_result = service.execute_db_admin_sql(
        DbAdminExecuteRequest(
            sql="BEGIN EXECUTE IMMEDIATE 'DROP TABLE NL2SQL_SCHEMA_OBJECTS'; END;",
            confirmation="ADMIN_EXECUTE",
        )
    )
    grant_result = service.execute_db_admin_sql(
        DbAdminExecuteRequest(
            sql="GRANT SELECT ON PLATFORM_USERS TO PUBLIC",
            confirmation="ADMIN_EXECUTE",
        )
    )
    revoke_result = service.execute_db_admin_sql(
        DbAdminExecuteRequest(
            sql='REVOKE SELECT ON APP."PLATFORM_AUTH_SESSIONS" FROM PUBLIC',
            confirmation="ADMIN_EXECUTE",
        )
    )

    assert [item.status for item in select_result.statements] == ["blocked"]
    assert [item.status for item in dml_result.statements] == ["blocked"]
    assert [item.status for item in plsql_result.statements] == ["blocked"]
    assert [item.status for item in grant_result.statements] == ["blocked"]
    assert [item.status for item in revoke_result.statements] == ["blocked"]
    assert "システムテーブル管理" in select_result.statements[0].error_message
    assert "システムテーブル管理" in grant_result.statements[0].error_message
    assert "システムテーブル管理" in revoke_result.statements[0].error_message
    assert adapter.select_calls == []
    assert adapter.calls == []


def test_statement_policy_comment_and_annotation_sql() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    comment_ok = service.execute_db_admin_statements(
        DbAdminStatementsRequest(
            sql=(
                "COMMENT ON TABLE T1 IS 'テーブル';\n"
                "COMMENT ON COLUMN T1.ID IS 'ID';\n"
                "COMMENT ON TABLE V1 IS 'ビュー';\n"
                "COMMENT ON MATERIALIZED VIEW MV1 IS 'MV'"
            ),
            policy="comment_sql",
        )
    )
    assert [item.status for item in comment_ok.statements] == ["confirmation_required"] * 4

    comment_view_ng = service.execute_db_admin_statements(
        DbAdminStatementsRequest(sql="COMMENT ON VIEW V1 IS 'ビュー'", policy="comment_sql")
    )
    assert comment_view_ng.statements[0].status == "blocked"
    assert comment_view_ng.statements[0].error_code == "DB_ADMIN_COMMENT_SQL_POLICY_VIOLATION"

    comment_ng = service.execute_db_admin_statements(
        DbAdminStatementsRequest(sql="CREATE TABLE T1 (ID NUMBER)", policy="comment_sql")
    )
    assert comment_ng.statements[0].status == "blocked"

    annotation_ok = service.execute_db_admin_statements(
        DbAdminStatementsRequest(
            sql=(
                "ALTER TABLE T1 ANNOTATIONS (UI_Display 'T1');\n"
                "ALTER TABLE T1 MODIFY (ID ANNOTATIONS (UI_Display 'ID'));\n"
                "ALTER TABLE T1 MODIFY ID ANNOTATIONS (UI_Display 'ID');\n"
                "ALTER VIEW V1 ANNOTATIONS (UI_Display 'V1');\n"
                "ALTER VIEW V1 MODIFY (ID ANNOTATIONS (\"DESCRIPTION\" 'ID'));\n"
                "ALTER TABLE T1 ANNOTATIONS (Business_Label '業務名');\n"
                "ALTER TABLE T1 ANNOTATIONS (ADD IF NOT EXISTS \"COMMENT\" '説明');\n"
                "ALTER MATERIALIZED VIEW MV1 ANNOTATIONS (UI_Display 'MV1')"
            ),
            policy="annotation_sql",
        )
    )
    assert [item.status for item in annotation_ok.statements] == ["confirmation_required"] * 8

    annotation_ng = service.execute_db_admin_statements(
        DbAdminStatementsRequest(sql="ALTER TABLE T1 ADD C1 NUMBER", policy="annotation_sql")
    )
    assert annotation_ng.statements[0].status == "blocked"
    assert annotation_ng.statements[0].error_code == "DB_ADMIN_ANNOTATION_SQL_POLICY_VIOLATION"


def test_annotation_policy_allows_only_annotation_clauses() -> None:
    """アノテーションの policy は、列の型・削除などを同じ文に含む ALTER を通さない（#940）。"""
    adapter = _FakeStatementsAdapter([])
    service = _OracleRuntimeService(adapter)
    allowed = service.execute_db_admin_statements(
        DbAdminStatementsRequest(
            sql=(
                'ALTER TABLE "APP"."T1" ANNOTATIONS (ADD OR REPLACE "DESCRIPTION" \'a (b), c\');\n'
                "ALTER TABLE APP.T1 MODIFY (ID ANNOTATIONS (ADD \"ALIASES\" 'id'), "
                "NAME ANNOTATIONS (ADD \"DESCRIPTION\" 'name'));\n"
                '/* 生成 */ ALTER VIEW V1 MODIFY ("Amt" ANNOTATIONS (ADD "UNITS" \'JPY\'))'
            ),
            policy="annotation_sql",
        )
    )
    assert [item.status for item in allowed.statements] == ["confirmation_required"] * 3

    for sql in (
        "ALTER TABLE T MODIFY (SALARY VARCHAR2(1) ANNOTATIONS (ADD \"X\" 'y'))",
        "ALTER TABLE T MODIFY (C NUMBER) DROP COLUMN D MODIFY (E ANNOTATIONS (ADD \"X\" 'y'))",
        "ALTER TABLE T ANNOTATIONS (ADD \"A\" 'x') DROP (SALARY)",
        "ALTER TABLE T MODIFY (E ANNOTATIONS (ADD \"X\" 'y')) DROP COLUMN D",
        "ALTER TABLE T MODIFY (E NOT NULL ANNOTATIONS (ADD \"X\" 'y'))",
        "ALTER VIEW V MODIFY E ANNOTATIONS (ADD \"X\" 'y')",
        "ALTER MATERIALIZED VIEW MV MODIFY (E ANNOTATIONS (ADD \"X\" 'y'))",
        'ALTER TABLE T ANNOTATIONS (ADD "X" \'unterminated)',
    ):
        blocked = service.execute_db_admin_statements(
            DbAdminStatementsRequest(sql=sql, policy="annotation_sql", confirmation="ADMIN_EXECUTE")
        )
        assert blocked.executed is False, sql
        assert blocked.statements[0].status == "blocked", sql
        assert blocked.statements[0].error_code == "DB_ADMIN_ANNOTATION_SQL_POLICY_VIOLATION", sql
    assert adapter.calls == []


@pytest.mark.parametrize("oracle_runtime", [False, True])
def test_comment_policy_error_code_survives_api_without_executing_batch(
    monkeypatch: pytest.MonkeyPatch, oracle_runtime: bool
) -> None:
    adapter = _FakeStatementsAdapter([])
    service = (
        _OracleRuntimeService(adapter)
        if oracle_runtime
        else Nl2SqlService(store=MemoryNl2SqlStore())
    )
    router = importlib.import_module("app.features.nl2sql.router")
    monkeypatch.setattr(router, "nl2sql_service", service)
    response = router.db_admin_statements(
        DbAdminStatementsRequest(
            sql="COMMENT ON TABLE V1 IS '説明'; COMMENT ON VIEW V2 IS '説明'",
            policy="comment_sql",
            confirmation="ADMIN_EXECUTE",
        )
    ).model_dump(mode="json")["data"]

    assert response["executed"] is False
    assert response["committed"] is False
    assert [item["status"] for item in response["statements"]] == ["blocked", "blocked"]
    assert [item["error_code"] for item in response["statements"]] == [
        "",
        "DB_ADMIN_COMMENT_SQL_POLICY_VIOLATION",
    ]
    assert response["statements"][0]["error_message"] == ""
    assert "COMMENT ON TABLE/COLUMN/MATERIALIZED VIEW" in response["statements"][1]["error_message"]
    assert adapter.calls == []

    corrected = service.execute_db_admin_statements(
        DbAdminStatementsRequest(
            sql="COMMENT ON TABLE V2 IS '説明'",
            policy="comment_sql",
        )
    )
    assert corrected.executed is False
    assert corrected.statements[0].status == "confirmation_required"
    assert corrected.statements[0].error_code == ""
    assert adapter.calls == []


def test_annotation_comment_name_is_blocked_before_oracle_execution() -> None:
    adapter = _FakeStatementsAdapter([])
    service = _OracleRuntimeService(adapter)
    invalid_sql = (
        "ALTER TABLE DEPARTMENT ANNOTATIONS "
        "(ADD IF NOT EXISTS COMMENT '部署情報を管理するテーブル');\n"
        "ALTER TABLE DEPARTMENT MODIFY (DEPARTMENT_ID ANNOTATIONS "
        "(ADD IF NOT EXISTS COMMENT '部署ID。主キー。'));\n"
        "ALTER TABLE DEPARTMENT MODIFY (DEPARTMENT_NAME ANNOTATIONS "
        "(ADD IF NOT EXISTS COMMENT '部署名。'));\n"
        "ALTER TABLE DEPARTMENT MODIFY (LOCATION ANNOTATIONS "
        "(ADD IF NOT EXISTS COMMENT '所在地。'));\n"
        "ALTER TABLE DEPARTMENT MODIFY (CREATED_AT ANNOTATIONS "
        "(ADD IF NOT EXISTS COMMENT 'レコード作成日時。'));"
    )

    result = service.execute_db_admin_statements(
        DbAdminStatementsRequest(
            sql=invalid_sql,
            policy="annotation_sql",
            confirmation="ADMIN_EXECUTE",
        )
    )

    assert result.executed is False
    assert adapter.calls == []
    assert {item.status for item in result.statements} == {"blocked"}
    assert all("ORA-11548" in item.error_message for item in result.statements)
    assert all(item.error_code == "ORA-11548" for item in result.statements)
    assert all('"DESCRIPTION"' in item.error_message for item in result.statements)


def test_admin_sql_splitter_handles_comments_literals_and_plsql_blocks() -> None:
    assert _split_sql_statements("BEGIN NULL; END;") == ["BEGIN NULL; END;"]
    assert _split_sql_statements(
        "UPDATE T SET X = 1 WHERE period_begin = 1; UPDATE T SET X = 2;"
    ) == [
        "UPDATE T SET X = 1 WHERE period_begin = 1",
        "UPDATE T SET X = 2",
    ]

    commented = _split_sql_statements(
        "CREATE TABLE T1 (MEMO VARCHAR2(100)); -- ok; done\n"
        "INSERT INTO T1 VALUES ('a;b', q'[c;d]');"
    )
    assert len(commented) == 2
    assert commented[0] == "CREATE TABLE T1 (MEMO VARCHAR2(100))"
    assert "-- ok; done" in commented[1]
    assert "INSERT INTO T1 VALUES ('a;b', q'[c;d]')" in commented[1]

    procedure_and_table = _split_sql_statements(
        "CREATE OR REPLACE PROCEDURE P1 IS\n"
        "  X NUMBER;\n"
        "BEGIN\n"
        "  NULL;\n"
        "END;\n"
        "/\n"
        "CREATE TABLE T2 (ID NUMBER);"
    )
    assert procedure_and_table == [
        "CREATE OR REPLACE PROCEDURE P1 IS\n  X NUMBER;\nBEGIN\n  NULL;\nEND;",
        "CREATE TABLE T2 (ID NUMBER)",
    ]

    assert _split_sql_statements("BEGIN X := CASE WHEN 1 = 1 THEN 'END;' ELSE 'N' END; END;") == [
        "BEGIN X := CASE WHEN 1 = 1 THEN 'END;' ELSE 'N' END; END;"
    ]


def test_admin_sql_normalizes_issue71_pasted_oracle_ddl_whitespace() -> None:
    sql = _issue71_create_table_sql()

    normalized = _normalize_oracle_sql_text(sql)
    statements = _split_sql_statements(sql)

    assert "\u00a0" not in normalized
    assert 'COLLATE "USING_NLS_COMP"' in normalized
    assert "NUMBER(3,0)" in normalized
    assert statements == [normalized.strip()]
    assert _admin_statement_type(statements[0]) == "CREATE"
    assert _statements_change_schema(statements) is True
    assert _normalize_oracle_sql_text("COMMENT ON TABLE T IS 'A\u00a0B'").endswith("'A\u00a0B'")


def test_issue71_create_table_runs_through_admin_sql_and_table_management(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = _issue71_create_table_sql()
    normalized = _split_sql_statements(sql)[0]
    submit_sources: list[str] = []
    submit_targets: list[list[tuple[str, str, str]]] = []

    def submit(**kwargs: Any) -> SchemaRefreshMutationSync:
        submit_sources.append(str(kwargs["source"]))
        submit_targets.append(
            [
                (target.owner, target.object_name, target.expected_state)
                for target in kwargs["target_objects"]
            ]
        )
        return SchemaRefreshMutationSync(job_id=f"schema-job-{len(submit_sources)}")

    admin_adapter = _FakeAdminSqlAdapter(
        statement_results=[
            {
                "index": 1,
                "statement_type": "CREATE",
                "status": "success",
                "sql": normalized,
            }
        ]
    )
    admin_service = _OracleRuntimeService(admin_adapter)
    monkeypatch.setattr(admin_service, "_submit_schema_refresh_after_admin_mutation", submit)

    admin_result = admin_service.execute_db_admin_sql(
        DbAdminExecuteRequest(
            sql=sql,
            confirmation="ADMIN_EXECUTE",
            reason="admin-sql-admin",
        )
    )

    table_adapter = _FakeStatementsAdapter(
        [
            {
                "index": 1,
                "statement_type": "CREATE",
                "status": "success",
                "sql": normalized,
            }
        ]
    )
    table_service = _OracleRuntimeService(table_adapter)
    monkeypatch.setattr(table_service, "_submit_schema_refresh_after_admin_mutation", submit)

    table_result = table_service.execute_db_admin_statements(
        DbAdminStatementsRequest(
            sql=sql,
            policy="table_ddl",
            confirmation="ADMIN_EXECUTE",
            reason="ui-db-admin-table_ddl",
        )
    )

    assert admin_result.executed is True
    assert table_result.executed is True
    assert admin_adapter.calls == [([normalized], True)]
    assert table_adapter.calls == [([normalized], False)]
    assert submit_sources == ["db_admin_execute", "db_admin_statements_table_ddl"]
    assert submit_targets == [[("APP", "ABC", "present")], [("APP", "ABC", "present")]]


@pytest.mark.parametrize(
    ("sql", "statement_type"),
    [
        ("RENAME OLD_TABLE TO NEW_TABLE", "RENAME"),
        ("FLASHBACK TABLE OLD_TABLE TO BEFORE DROP", "FLASHBACK"),
        ("PURGE TABLE OLD_TABLE", "PURGE"),
    ],
)
def test_admin_statement_type_marks_schema_mutation_for_recovery_sql(
    sql: str,
    statement_type: str,
) -> None:
    assert _admin_statement_type(sql) == statement_type
    assert _statements_change_schema([sql]) is True
    adapter = object.__new__(OracleNl2SqlAdapter)
    assert adapter._admin_statement_type(sql) == statement_type


def test_statements_execute_requires_confirmation_and_oracle_runtime() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    missing_confirmation = service.execute_db_admin_statements(
        DbAdminStatementsRequest(
            sql="CREATE TABLE T1 (ID NUMBER)",
            policy="table_ddl",
        )
    )
    assert missing_confirmation.statements[0].status == "confirmation_required"
    assert missing_confirmation.execution_context == "admin_control_plane"

    requires_oracle = service.execute_db_admin_statements(
        DbAdminStatementsRequest(
            sql="CREATE TABLE T1 (ID NUMBER)",
            policy="table_ddl",
            confirmation="ADMIN_EXECUTE",
        )
    )
    assert requires_oracle.statements[0].status == "requires_oracle"
    assert requires_oracle.execution_context == "admin_control_plane"
    assert any("NL2SQL_RUNTIME_MODE=oracle" in warning for warning in requires_oracle.warnings)


def test_admin_sql_single_select_uses_data_plane_without_statement_executor() -> None:
    adapter = _FakeAdminSqlAdapter()
    service = _OracleRuntimeService(adapter)

    result = service.execute_db_admin_sql(
        DbAdminExecuteRequest(
            sql="SELECT ID FROM T1",
            row_limit=50,
        )
    )

    assert len(adapter.select_calls) == 1
    assert adapter.select_calls[0][1] == 50
    assert adapter.calls == []
    assert result.executed is True
    assert result.committed is False
    assert result.runtime == "oracle"
    assert result.execution_context == "oracle_data_plane"
    assert result.statements[0].status == "executed"


def test_admin_sql_mixed_select_and_dml_stays_blocked_control_plane() -> None:
    adapter = _FakeAdminSqlAdapter()
    service = _OracleRuntimeService(adapter)

    result = service.execute_db_admin_sql(
        DbAdminExecuteRequest(
            sql="SELECT ID FROM T1; UPDATE T1 SET ID = 2",
            confirmation="ADMIN_EXECUTE",
        )
    )

    assert adapter.select_calls == []
    assert adapter.calls == []
    assert result.executed is False
    assert result.execution_context == "admin_control_plane"
    assert {item.status for item in result.statements} == {"blocked"}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM T1 WHERE",
        # sqlglot が解析できない Oracle の構文（MODEL 句）。
        "SELECT * FROM T1 MODEL DIMENSION BY (ID) MEASURES (V) RULES (V[1] = 1)",
        "WITH A AS (SELECT ID FROM T1) SELECT * FROM A WHERE",
    ],
)
def test_admin_sql_unverifiable_select_without_confirmation_reports_reason(sql: str) -> None:
    """画面は確認語の欄を出さないので、確認語待ちにせず理由を返す（#933）。"""

    adapter = _FakeAdminSqlAdapter()
    service = _OracleRuntimeService(adapter)

    with actor_scope("business-user-id", is_system_admin=False):
        result = service.execute_db_admin_sql(DbAdminExecuteRequest(sql=sql, row_limit=10))

    # data plane にも control plane にも流さない。
    assert adapter.select_calls == []
    assert adapter.calls == []
    assert result.executed is False
    assert result.committed is False
    assert len(result.statements) == 1
    statement = result.statements[0]
    assert statement.status == "error"
    assert statement.statement_type == "SELECT"
    assert statement.error_message.startswith(
        "SELECT 文を読み取り専用として確認できないため実行しませんでした。構文を確認してください。"
    )
    assert "confirmation=ADMIN_EXECUTE" not in statement.error_message
    assert result.warnings == [statement.error_message]


def test_admin_sql_flashback_query_runs_as_read_only_select() -> None:
    """Oracle の flashback query（AS OF）は読み取り専用の SELECT として data plane で実行する。

    sqlglot 30.23 から解析でき、表の版の指定（`Table.version`）になる。以前は解析できず、
    読み取り専用と確かめられない SELECT として理由を返していた（#933）。
    """

    adapter = _FakeAdminSqlAdapter()
    service = _OracleRuntimeService(adapter)
    sql = "SELECT * FROM T1 AS OF TIMESTAMP SYSTIMESTAMP"

    with actor_scope("business-user-id", is_system_admin=False):
        result = service.execute_db_admin_sql(DbAdminExecuteRequest(sql=sql, row_limit=10))

    assert adapter.select_calls == [(sql, 10)]
    assert adapter.calls == []
    assert result.committed is False
    assert [item.statement_type for item in result.statements] == ["SELECT"]


def test_admin_sql_select_into_without_confirmation_reports_side_effect_reason() -> None:
    adapter = _FakeAdminSqlAdapter()
    service = _OracleRuntimeService(adapter)

    result = service.execute_db_admin_sql(
        DbAdminExecuteRequest(sql="SELECT ID INTO V_ID FROM T1", row_limit=10)
    )

    assert adapter.select_calls == []
    assert adapter.calls == []
    assert result.statements[0].status == "error"
    assert "SELECT INTO または FOR UPDATE/SHARE" in result.statements[0].error_message


def test_admin_sql_confirmed_select_for_update_keeps_control_plane_path() -> None:
    """画面が更新系として確認語を求める FOR UPDATE は、確認語付きなら従来どおり実行する。"""

    adapter = _FakeAdminSqlAdapter(
        statement_results=[
            {
                "index": 1,
                "statement_type": "SELECT",
                "status": "success",
                "sql": "SELECT ID FROM T1 FOR UPDATE",
            }
        ]
    )
    service = _OracleRuntimeService(adapter)

    result = service.execute_db_admin_sql(
        DbAdminExecuteRequest(sql="SELECT ID FROM T1 FOR UPDATE", confirmation="ADMIN_EXECUTE")
    )

    assert adapter.select_calls == []
    assert len(adapter.calls) == 1
    assert result.statements[0].status == "success"
    assert result.execution_context == "admin_control_plane"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT ID FROM T1 FOR UPDATE",
        "WITH A AS (SELECT ID FROM T1) UPDATE T1 SET NAME = 'X' WHERE ID IN (SELECT ID FROM A)",
    ],
)
def test_admin_sql_select_head_with_mutating_token_still_requires_confirmation(sql: str) -> None:
    """画面が更新系の語で確認語の欄を出す文は、確認語なしならエラーにせず確認語を求める（#933）。"""

    adapter = _FakeAdminSqlAdapter()
    service = _OracleRuntimeService(adapter)

    result = service.execute_db_admin_sql(DbAdminExecuteRequest(sql=sql, row_limit=10))

    assert adapter.select_calls == []
    assert adapter.calls == []
    assert result.executed is False
    assert result.statements[0].status == "confirmation_required"
    assert any("ADMIN_EXECUTE" in warning for warning in result.warnings)


@pytest.mark.parametrize("table_name", ["PLATFORM_USERS", "RAG_DOCUMENTS", "AGENT_RUNS"])
def test_admin_sql_blocks_other_product_tables_with_accurate_message(table_name: str) -> None:
    adapter = _FakeAdminSqlAdapter()
    service = _OracleRuntimeService(adapter)

    result = service.execute_db_admin_sql(
        DbAdminExecuteRequest(sql=f"SELECT * FROM {table_name}", row_limit=10)
    )

    assert adapter.select_calls == []
    assert result.statements[0].status == "blocked"
    message = result.statements[0].error_message
    assert message.startswith(f"APP.{table_name}: ")
    assert "NL2SQL システム object" not in message
    assert "PLATFORM_・RAG_・AGENT_" in message
    assert "業務データではない" in message


def test_statements_partial_success_commits_and_records_audit() -> None:
    adapter = _FakeStatementsAdapter(
        [
            {
                "index": 1,
                "statement_type": "CREATE",
                "status": "success",
                "sql": "CREATE TABLE T1 (ID NUMBER)",
                "message": "実行しました。",
            },
            {
                "index": 2,
                "statement_type": "COMMENT",
                "status": "error",
                "sql": "COMMENT ON TABLE MISSING IS 'x'",
                "error_message": "ORA-00942: table or view does not exist",
            },
        ]
    )
    service = _OracleRuntimeService(adapter)
    sql = "CREATE TABLE T1 (ID NUMBER); COMMENT ON TABLE MISSING IS 'x'"

    with actor_scope("audit-user-uuid", is_system_admin=True):
        result = service.execute_db_admin_statements(
            DbAdminStatementsRequest(
                sql=sql,
                policy="table_ddl",
                confirmation="ADMIN_EXECUTE",
            )
        )

    assert adapter.calls and adapter.calls[0][1] is False  # atomic=False
    assert result.executed is True
    assert result.committed is True
    assert result.rolled_back is False
    assert result.execution_context == "admin_control_plane"
    assert any("部分的に成功" in warning for warning in result.warnings)
    audit = next(
        item
        for item in service._admin_audit_store.list_recent()
        if item.operation == "db_admin_statements_table_ddl"
    )
    assert audit.actor_user_uuid == "audit-user-uuid"
    assert audit.actor_is_system_admin is True
    assert audit.source == "data_preparation"
    assert audit.outcome == "partial"
    assert audit.sql_text == sql
    assert audit.statement_count == 2
    assert audit.success_count == 1
    assert len(audit.detail["sql_sha256"]) == 64
    assert audit.detail["sql_preview"] == sql
    assert len(audit.detail["statement_hashes"]) == 2
    assert audit.detail["statement_previews"] == [
        "CREATE TABLE T1 (ID NUMBER)",
        "COMMENT ON TABLE MISSING IS 'x'",
    ]


def test_admin_sql_execute_audit_records_actor_and_sql_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = "CREATE TABLE AUDIT_DIRECT (ID NUMBER)"
    adapter = _FakeStatementsAdapter(
        [
            {
                "index": 1,
                "statement_type": "CREATE",
                "status": "success",
                "sql": sql,
                "message": "OK",
            }
        ]
    )
    service = _OracleRuntimeService(adapter)
    monkeypatch.setattr(
        service,
        "_submit_schema_refresh_after_admin_mutation",
        lambda **_kwargs: SchemaRefreshMutationSync(job_id="refresh-job-audit"),
    )

    with actor_scope("direct-audit-user", is_system_admin=False):
        result = service.execute_db_admin_sql(
            DbAdminExecuteRequest(
                sql=sql,
                confirmation="ADMIN_EXECUTE",
                reason="admin-sql-admin",
            )
        )

    assert result.executed is True
    audit = service._admin_audit_store.list_recent(1)[0]
    assert audit.operation == "db_admin_execute"
    assert audit.source == "admin_sql"
    assert audit.outcome == "success"
    assert audit.confirmation_given is True
    assert audit.committed is True
    assert audit.sql_text == sql
    assert audit.actor_user_uuid == "direct-audit-user"
    assert audit.actor_is_system_admin is False
    assert audit.statement_count == 1
    assert audit.success_count == 1
    assert audit.statement_types == ["CREATE"]
    assert audit.duration_ms is not None
    assert len(audit.detail["sql_sha256"]) == 64
    assert audit.detail["sql_preview"] == sql
    assert audit.detail["statement_previews"] == [sql]


def test_admin_sql_delegates_data_dml_batch_to_partial_commit_policy() -> None:
    adapter = _FakeStatementsAdapter(
        [
            {
                "index": 1,
                "statement_type": "INSERT",
                "status": "success",
                "sql": "INSERT INTO T1 VALUES (1)",
                "message": "RowsAffected=1",
            },
            {
                "index": 2,
                "statement_type": "UPDATE",
                "status": "error",
                "sql": "UPDATE MISSING_TABLE SET ID = 2",
                "error_message": "ORA-00942: table or view does not exist",
            },
        ]
    )
    service = _OracleRuntimeService(adapter)

    result = service.execute_db_admin_sql(
        DbAdminExecuteRequest(
            sql="INSERT INTO T1 VALUES (1); UPDATE MISSING_TABLE SET ID = 2",
            confirmation="ADMIN_EXECUTE",
            reason="admin-sql-admin",
        )
    )

    assert adapter.calls == [
        (
            ["INSERT INTO T1 VALUES (1)", "UPDATE MISSING_TABLE SET ID = 2"],
            False,
        )
    ]
    assert result.executed is True
    assert result.committed is True
    assert result.rolled_back is False
    assert result.execution_context == "admin_control_plane"
    assert any("部分的に成功しました(1/2 件)" in warning for warning in result.warnings)
    # 管理 SQL を実行からの委譲は、管理 SQL の 1 行だけを残す（二重に残さない。#2264）。
    audits = service._admin_audit_store.list_recent()
    assert [item.operation for item in audits] == ["db_admin_execute"]
    assert audits[0].source == "admin_sql"
    assert audits[0].outcome == "partial"


def test_admin_sql_commits_when_all_data_dml_statements_succeed() -> None:
    adapter = _FakeStatementsAdapter(
        [
            {
                "index": 1,
                "statement_type": "INSERT",
                "status": "success",
                "sql": "INSERT INTO T1 VALUES (1)",
                "message": "RowsAffected=1",
            },
            {
                "index": 2,
                "statement_type": "DELETE",
                "status": "success",
                "sql": "DELETE FROM T1 WHERE ID = 2",
                "message": "RowsAffected=1",
            },
        ]
    )
    service = _OracleRuntimeService(adapter)

    result = service.execute_db_admin_sql(
        DbAdminExecuteRequest(
            sql="INSERT INTO T1 VALUES (1); DELETE FROM T1 WHERE ID = 2",
            confirmation="ADMIN_EXECUTE",
        )
    )

    assert adapter.calls[0][1] is False
    assert result.executed is True
    assert result.committed is True
    assert result.rolled_back is False
    assert result.execution_context == "admin_control_plane"
    assert result.warnings == []


def test_admin_sql_rolls_back_when_all_data_dml_statements_fail() -> None:
    adapter = _FakeStatementsAdapter(
        [
            {
                "index": 1,
                "statement_type": "DELETE",
                "status": "error",
                "sql": "DELETE FROM MISSING_TABLE",
                "error_message": "ORA-00942: table or view does not exist",
            },
            {
                "index": 2,
                "statement_type": "UPDATE",
                "status": "error",
                "sql": "UPDATE MISSING_TABLE SET ID = 2",
                "error_message": "ORA-00942: table or view does not exist",
            },
        ]
    )
    service = _OracleRuntimeService(adapter)

    result = service.execute_db_admin_sql(
        DbAdminExecuteRequest(
            sql="DELETE FROM MISSING_TABLE; UPDATE MISSING_TABLE SET ID = 2",
            confirmation="ADMIN_EXECUTE",
        )
    )

    assert adapter.calls[0][1] is False
    assert result.executed is False
    assert result.committed is False
    assert result.rolled_back is True
    assert result.execution_context == "admin_control_plane"


def test_admin_sql_reports_implicit_ddl_commit_when_atomic_batch_partially_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _FakeStatementsAdapter(
        [
            {
                "index": 1,
                "statement_type": "CREATE",
                "status": "success",
                "sql": "CREATE TABLE T1 (ID NUMBER)",
                "message": "実行しました。",
            },
            {
                "index": 2,
                "statement_type": "UPDATE",
                "status": "error",
                "sql": "UPDATE MISSING_TABLE SET ID = 2",
                "error_message": "ORA-00942: table or view does not exist",
            },
        ]
    )
    service = _OracleRuntimeService(adapter)
    submitted_targets: list[list[tuple[str, str, str]]] = []

    def submit(**kwargs: Any) -> SchemaRefreshMutationSync:
        submitted_targets.append(
            [
                (target.owner, target.object_name, target.expected_state)
                for target in kwargs["target_objects"]
            ]
        )
        return SchemaRefreshMutationSync(job_id="refresh-job-34")

    monkeypatch.setattr(service, "_submit_schema_refresh_after_admin_mutation", submit)

    result = service.execute_db_admin_sql(
        DbAdminExecuteRequest(
            sql="CREATE TABLE T1 (ID NUMBER); UPDATE MISSING_TABLE SET ID = 2",
            confirmation="ADMIN_EXECUTE",
        )
    )

    assert adapter.calls[0][1] is True
    assert result.executed is True
    assert result.committed is True
    assert result.rolled_back is False
    assert result.execution_context == "admin_control_plane"
    assert result.schema_refresh_job_id == "refresh-job-34"
    assert submitted_targets == [[("APP", "T1", "present")]]
    assert any("暗黙 commit" in warning for warning in result.warnings)
    assert not any("部分的に成功" in warning for warning in result.warnings)


def test_admin_sql_rename_requires_manual_schema_refresh() -> None:
    adapter = _FakeStatementsAdapter(
        [
            {
                "index": 1,
                "statement_type": "RENAME",
                "status": "success",
                "sql": "RENAME OLD_TABLE TO NEW_TABLE",
                "message": "OK",
            }
        ]
    )
    service = _OracleRuntimeService(adapter)

    result = service.execute_db_admin_sql(
        DbAdminExecuteRequest(
            sql="RENAME OLD_TABLE TO NEW_TABLE",
            confirmation="ADMIN_EXECUTE",
        )
    )

    assert result.executed is True
    assert result.committed is True
    assert result.schema_refresh_job_id == ""
    assert result.schema_refresh_required is True
    assert result.schema_refresh_reason_code == "schema_refresh_target_unresolved"
    # 再取得の案内は構造化した項目だけで返し、同じ文を warnings に重ねない（#2031）。
    assert not any("DB 構造を再取得" in warning for warning in result.warnings)


def test_admin_sql_returns_result_when_audit_persistence_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _FakeStatementsAdapter(
        [
            {
                "index": 1,
                "statement_type": "CREATE",
                "status": "success",
                "sql": "CREATE TABLE T1 (ID NUMBER)",
                "message": "OK",
            }
        ]
    )
    service = _OracleRuntimeService(adapter)

    def fail_audit(**_kwargs: Any) -> None:
        raise Nl2SqlPersistenceUnavailable()

    monkeypatch.setattr(service, "_record_admin_audit", fail_audit)
    monkeypatch.setattr(
        service,
        "_submit_schema_refresh_after_admin_mutation",
        lambda **_kwargs: SchemaRefreshMutationSync(job_id="refresh-job-34"),
    )

    result = service.execute_db_admin_sql(
        DbAdminExecuteRequest(
            sql="CREATE TABLE T1 (ID NUMBER)",
            confirmation="ADMIN_EXECUTE",
        )
    )

    assert result.executed is True
    assert result.committed is True
    assert result.schema_refresh_job_id == "refresh-job-34"
    assert any("監査保存に失敗" in warning for warning in result.warnings)


def test_admin_sql_keeps_plsql_and_with_dml_atomic() -> None:
    plsql_adapter = _FakeStatementsAdapter(
        [
            {
                "index": 1,
                "statement_type": "PLSQL",
                "status": "success",
                "sql": "BEGIN NULL; END",
                "message": "実行しました。",
            }
        ]
    )
    plsql_service = _OracleRuntimeService(plsql_adapter)

    plsql_result = plsql_service.execute_db_admin_sql(
        DbAdminExecuteRequest(
            sql="BEGIN NULL; END;",
            confirmation="ADMIN_EXECUTE",
        )
    )

    assert plsql_adapter.calls[0][1] is True
    assert plsql_result.executed is True
    assert plsql_result.committed is True
    assert plsql_result.execution_context == "admin_control_plane"

    with_dml_adapter = _FakeStatementsAdapter(
        [
            {
                "index": 1,
                "statement_type": "SELECT",
                "status": "success",
                "sql": (
                    "WITH TARGET AS (SELECT ID FROM T1) "
                    "UPDATE T1 SET ID = 2 WHERE ID IN (SELECT ID FROM TARGET)"
                ),
                "message": "RowsAffected=1",
            }
        ]
    )
    with_dml_service = _OracleRuntimeService(with_dml_adapter)
    with_dml_sql = (
        "WITH TARGET AS (SELECT ID FROM T1) "
        "UPDATE T1 SET ID = 2 WHERE ID IN (SELECT ID FROM TARGET)"
    )

    with_dml_result = with_dml_service.execute_db_admin_sql(
        DbAdminExecuteRequest(
            sql=with_dml_sql,
            confirmation="ADMIN_EXECUTE",
        )
    )

    assert with_dml_adapter.calls == [([with_dml_sql], True)]
    assert with_dml_result.executed is True
    assert with_dml_result.committed is True
    assert with_dml_result.execution_context == "admin_control_plane"


def test_metadata_sql_generation_fallback_and_fence_cleanup() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    cast(Any, service)._enterprise_ai_client = FakeEnterpriseAiClient(configured=False)
    fallback = service.generate_comment_sql(
        MetadataSqlGenerateRequest(
            targets=[{"object_name": "EMPLOYEE", "object_type": "table"}],
            structure_text="OBJECT: EMPLOYEE",
        )
    )
    assert "COMMENT ON TABLE" in fallback.sql
    assert fallback.source == "deterministic"
    assert any("モデルの API" in warning for warning in fallback.warnings)

    service._enterprise_ai_client = FakeEnterpriseAiClient(
        "```sql\nCOMMENT ON VIEW V_EMP IS '社員ビュー';\n```"
    )
    comment_ai = service.generate_comment_sql(
        MetadataSqlGenerateRequest(
            targets=[{"object_name": "V_EMP", "object_type": "view"}],
            structure_text="OBJECT: V_EMP",
        )
    )
    assert comment_ai.source == "enterprise_ai"
    assert comment_ai.sql == "COMMENT ON TABLE V_EMP IS '社員ビュー';"

    service._enterprise_ai_client = FakeEnterpriseAiClient(
        "```sql\n"
        "ALTER TABLE EMPLOYEE MODIFY (EMPLOYEE_NAME ANNOTATIONS (UI_Display '社員名'));\n"
        "```"
    )
    annotation_ai = service.generate_annotation_sql(
        MetadataSqlGenerateRequest(
            targets=[{"object_name": "EMPLOYEE", "object_type": "table"}],
            structure_text="OBJECT: EMPLOYEE",
        )
    )
    assert annotation_ai.source == "enterprise_ai"
    assert annotation_ai.sql == (
        "ALTER TABLE EMPLOYEE MODIFY "
        "(EMPLOYEE_NAME ANNOTATIONS (ADD IF NOT EXISTS UI_Display '社員名'));"
    )


def test_annotation_generation_ports_reference_prompt_and_filters_sample_annotations() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    enterprise_ai = FakeEnterpriseAiClient(
        "```sql\n"
        "ALTER TABLE T1 ANNOTATIONS "
        "(UI_Display 'T, One', sample_header 'ID,NAME', sample_data '1,A');\n"
        "```"
    )
    service._enterprise_ai_client = enterprise_ai

    result = service.generate_annotation_sql(
        MetadataSqlGenerateRequest(
            targets=[{"object_name": "T1", "object_type": "table"}],
            structure_text="OBJECT: T1\nTYPE: table\nCOMMENT: T One",
            sample_text="",
        )
    )

    assert result.source == "enterprise_ai"
    assert result.sql == "ALTER TABLE T1 ANNOTATIONS (ADD IF NOT EXISTS UI_Display 'T, One');"
    assert "COMMENT: は入力メタデータ" in enterprise_ai.calls[0]["prompt"]
    assert (
        "sample_header / sample_data は SQL の生成が DDL から得るため生成しない"
        in (enterprise_ai.calls[0]["prompt"])
    )
    assert '"JOIN COLUMN": 外部キー情報から結合先' in enterprise_ai.calls[0]["prompt"]
    assert "サンプルが無いためコメントに根拠がある場合だけ付け" in enterprise_ai.calls[0]["prompt"]
    assert "未引用の COMMENT は禁止" in enterprise_ai.calls[0]["system_prompt"]
    assert "ビュー列: ALTER VIEW <ビュー> MODIFY" in enterprise_ai.calls[0]["system_prompt"]

    with_samples_ai = FakeEnterpriseAiClient(
        "ALTER TABLE T1 ANNOTATIONS "
        "(UI_Display 'T One', sample_header 'ID,NAME', sample_data '1,A');"
    )
    service._enterprise_ai_client = with_samples_ai
    with_samples = service.generate_annotation_sql(
        MetadataSqlGenerateRequest(
            targets=[{"object_name": "T1", "object_type": "table"}],
            structure_text="OBJECT: T1\nTYPE: table\nCOMMENT: T One",
            sample_text="OBJECT: T1\nID: 1\nNAME: A",
        )
    )

    # サンプルがあっても sample_header / sample_data は SQL の生成に不要なため常に除外する。
    assert "sample_header" not in with_samples.sql
    assert "sample_data" not in with_samples.sql
    assert with_samples.sql == "ALTER TABLE T1 ANNOTATIONS (ADD IF NOT EXISTS UI_Display 'T One');"
    assert "サンプル・コメントに根拠がある場合だけ付け" in with_samples_ai.calls[0]["prompt"]


def test_invalid_ai_comment_annotation_falls_back_to_idempotent_ui_display_sql() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._enterprise_ai_client = FakeEnterpriseAiClient(
        "ALTER TABLE DEPARTMENT ANNOTATIONS "
        "(ADD IF NOT EXISTS COMMENT '部署情報を管理するテーブル');"
    )

    result = service.generate_annotation_sql(
        MetadataSqlGenerateRequest(
            targets=[{"object_name": "DEPARTMENT", "object_type": "table"}],
            structure_text=("OBJECT: DEPARTMENT\nTYPE: table\nCOMMENT: 部署情報を管理するテーブル"),
        )
    )

    assert result.source == "deterministic"
    assert 'ADD OR REPLACE "DESCRIPTION"' in result.sql
    assert "ADD IF NOT EXISTS COMMENT" not in result.sql
    # AI の失敗は warnings ではなく fallback の理由・詳細に出す（#1719）。
    assert result.fallback is True
    assert "ORA-11548" in result.fallback_detail


def test_multi_annotation_generation_preserves_explicit_add_and_normalizes_omitted_items() -> None:
    # 操作句省略だけを ADD IF NOT EXISTS に補い、明示 ADD は no-op 化しない。
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._enterprise_ai_client = FakeEnterpriseAiClient(
        "ALTER TABLE EMPLOYEE MODIFY (EMPLOYEE_ID ANNOTATIONS "
        "(ADD UI_Display '従業員ID', data_type 'NUMBER', nullable 'N'));"
    )

    result = service.generate_annotation_sql(
        MetadataSqlGenerateRequest(
            targets=[{"object_name": "EMPLOYEE", "object_type": "table"}],
            structure_text=(
                "OBJECT: EMPLOYEE\nTYPE: table\nCOMMENT: 従業員\n"
                "- EMPLOYEE_ID: NUMBER NULLABLE=N COMMENT=従業員ID"
            ),
        )
    )

    assert "ADD UI_Display '従業員ID'" in result.sql
    assert result.sql.count("ADD IF NOT EXISTS") == 2
    assert "data_type 'NUMBER'" in result.sql
    assert "ADD IF NOT EXISTS data_type 'NUMBER'" in result.sql
    assert "ADD IF NOT EXISTS nullable 'N'" in result.sql


def test_deterministic_annotation_sql_sorts_objects_and_escapes_values() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._enterprise_ai_client = FakeEnterpriseAiClient(configured=False)

    result = service.generate_annotation_sql(
        MetadataSqlGenerateRequest(
            targets=[
                {"object_name": "B_TABLE", "object_type": "table"},
                {"object_name": "A_TABLE", "object_type": "table"},
                {"object_name": "V_TABLE", "object_type": "view"},
            ],
            structure_text=(
                "OBJECT: B_TABLE\nTYPE: table\nCOMMENT: B\n"
                "- B_ID: NUMBER NULLABLE=N COMMENT=B's ID\n\n"
                "OBJECT: A_TABLE\nTYPE: table\nCOMMENT: O'Brien\n"
                "- A_ID: NUMBER NULLABLE=N COMMENT=A ID\n\n"
                "OBJECT: V_TABLE\nTYPE: view\nCOMMENT: View\n"
                "- V_ID: NUMBER NULLABLE=Y COMMENT=View ID"
            ),
        )
    )

    assert result.sql.index('ALTER TABLE "APP"."A_TABLE"') < result.sql.index(
        'ALTER TABLE "APP"."B_TABLE"'
    )
    assert "O''Brien" in result.sql
    assert 'ADD OR REPLACE "DESCRIPTION"' in result.sql
    # ビュー列は ALTER VIEW ... MODIFY で annotation する(記事パターン 5)。
    assert (
        'ALTER VIEW "APP"."V_TABLE" MODIFY ("V_ID" ANNOTATIONS '
        "(ADD OR REPLACE \"DESCRIPTION\" 'View ID'));"
    ) in result.sql


def test_metadata_sql_uses_oracle_view_and_materialized_view_syntax() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._catalog = SchemaCatalog(
        refreshed_at="2026-07-11T00:00:00+00:00",
        tables=[
            SchemaTable(
                table_name="SALES_V",
                logical_name="売上ビュー",
                table_type="view",
                comment="売上ビュー",
                columns=[
                    SchemaColumn(
                        column_name="SALES_ID",
                        logical_name="売上ID",
                        data_type="NUMBER",
                    )
                ],
            ),
            SchemaTable(
                table_name="SALES_MV",
                logical_name="売上MV",
                table_type="materialized view",
                comment="売上MV",
                columns=[
                    SchemaColumn(
                        column_name="SALES_ID",
                        logical_name="売上ID",
                        data_type="NUMBER",
                    )
                ],
            ),
        ],
    )
    service._enterprise_ai_client = FakeEnterpriseAiClient(configured=False)

    comments = service.generate_comment_sql(
        MetadataSqlGenerateRequest(
            targets=[
                {"object_name": "SALES_V", "object_type": "view"},
                {"object_name": "SALES_MV", "object_type": "view"},
            ],
            structure_text=(
                "OBJECT: SALES_V\nTYPE: view\n\nOBJECT: SALES_MV\nTYPE: materialized view"
            ),
        )
    )
    annotations = service.generate_annotation_sql(
        MetadataSqlGenerateRequest(
            targets=[
                {"object_name": "SALES_V", "object_type": "view"},
                {"object_name": "SALES_MV", "object_type": "view"},
            ],
            structure_text=(
                "OBJECT: SALES_V\nTYPE: view\n\nOBJECT: SALES_MV\nTYPE: materialized view"
            ),
        )
    )

    assert 'COMMENT ON TABLE "APP"."SALES_V"' in comments.sql
    assert 'COMMENT ON MATERIALIZED VIEW "APP"."SALES_MV"' in comments.sql
    assert "COMMENT ON VIEW" not in comments.sql
    assert 'ALTER VIEW "APP"."SALES_V"' in annotations.sql
    assert 'ALTER MATERIALIZED VIEW "APP"."SALES_MV"' in annotations.sql
    assert 'ADD OR REPLACE "DESCRIPTION"' in annotations.sql
    # ビュー列は MODIFY で annotation し、MV 列は生成しない。
    assert 'ALTER MATERIALIZED VIEW "APP"."SALES_MV" MODIFY' not in annotations.sql


def test_comment_apply_reports_partial_oracle_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _FakeStatementsAdapter(
        [
            {"index": 1, "status": "success", "message": "RowsAffected=0"},
            {"index": 2, "status": "error", "error_message": "ORA-00942: missing object"},
        ]
    )
    service = _OracleRuntimeService(adapter)
    service._catalog = SchemaCatalog(
        refreshed_at="2026-07-11T00:00:00+00:00",
        tables=[
            SchemaTable(table_name="EMPLOYEE", logical_name="社員"),
            SchemaTable(table_name="DEPARTMENT", logical_name="部署"),
        ],
    )
    submitted: list[tuple[list[Any] | None, str]] = []

    def submit(
        *,
        target_objects: list[Any] | None,
        source: str,
    ) -> SchemaRefreshMutationSync:
        submitted.append((target_objects, source))
        return SchemaRefreshMutationSync(job_id="refresh-comment")

    monkeypatch.setattr(service, "_submit_schema_refresh_after_admin_mutation", submit)

    result = service.apply_comments(
        CommentApplyRequest(
            items=[
                {
                    "object_name": "EMPLOYEE",
                    "object_type": "table",
                    "comment": "社員",
                },
                {
                    "object_name": "DEPARTMENT",
                    "object_type": "table",
                    "comment": "部署",
                },
            ],
            confirmation="ADMIN_EXECUTE",
        )
    )

    assert adapter.calls == [
        (
            [
                'COMMENT ON TABLE "APP"."EMPLOYEE" IS \'社員\';',
                'COMMENT ON TABLE "APP"."DEPARTMENT" IS \'部署\';',
            ],
            False,
        )
    ]
    assert result.executed is True
    assert [statement.status for statement in result.statements] == ["applied", "error"]
    assert result.statements[1].error_message == "ORA-00942: missing object"
    assert result.schema_refresh_job_id == "refresh-comment"
    assert submitted[0][1] == "comments_apply"
    assert submitted[0][0] is not None
    assert [target.object_name for target in submitted[0][0]] == ["EMPLOYEE"]
    assert any("部分的に成功" in warning for warning in result.warnings)


def test_annotation_apply_reports_partial_oracle_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = _FakeStatementsAdapter(
        [
            {"index": 1, "status": "success", "message": "RowsAffected=0"},
            {"index": 2, "status": "error", "error_message": "ORA-11560: annotation exists"},
        ]
    )
    service = _OracleRuntimeService(adapter)
    service._catalog = SchemaCatalog(
        refreshed_at="2026-07-11T00:00:00+00:00",
        tables=[
            SchemaTable(table_name="EMPLOYEE", logical_name="社員"),
            SchemaTable(table_name="DEPARTMENT", logical_name="部署"),
        ],
    )

    def submit(
        *,
        target_objects: list[Any] | None,
        source: str,
    ) -> SchemaRefreshMutationSync:
        assert source == "annotations_apply"
        assert target_objects is not None
        assert [target.object_name for target in target_objects] == ["EMPLOYEE"]
        return SchemaRefreshMutationSync(job_id="refresh-annotation")

    monkeypatch.setattr(service, "_submit_schema_refresh_after_admin_mutation", submit)

    result = service.apply_annotations(
        AnnotationApplyRequest(
            items=[
                {
                    "object_name": "EMPLOYEE",
                    "object_type": "table",
                    "annotation_name": "Display",
                    "annotation_value": "社員",
                },
                {
                    "object_name": "DEPARTMENT",
                    "object_type": "table",
                    "annotation_name": "Display",
                    "annotation_value": "部署",
                },
            ],
            confirmation="ADMIN_EXECUTE",
        )
    )

    assert adapter.calls == [
        (
            [
                'ALTER TABLE "APP"."EMPLOYEE" ANNOTATIONS (ADD OR REPLACE DISPLAY \'社員\');',
                'ALTER TABLE "APP"."DEPARTMENT" ANNOTATIONS (ADD OR REPLACE DISPLAY \'部署\');',
            ],
            False,
        )
    ]
    assert result.executed is True
    assert [statement.status for statement in result.statements] == ["applied", "error"]
    assert result.statements[1].error_message == "ORA-11560: annotation exists"
    assert result.schema_refresh_job_id == "refresh-annotation"
    assert any("部分的に成功" in warning for warning in result.warnings)


def test_annotation_candidates_normalize_mv_and_skip_view_columns() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._catalog = SchemaCatalog(
        refreshed_at="2026-07-11T00:00:00+00:00",
        tables=[
            SchemaTable(
                table_name="SALES_V",
                logical_name="売上ビュー",
                table_type="view",
                columns=[
                    SchemaColumn(
                        column_name="SALES_ID",
                        logical_name="売上ID",
                        data_type="NUMBER",
                    )
                ],
            ),
            SchemaTable(
                table_name="SALES_MV",
                logical_name="売上MV",
                table_type="materialized view",
                columns=[
                    SchemaColumn(
                        column_name="SALES_ID",
                        logical_name="売上ID",
                        data_type="NUMBER",
                    )
                ],
            ),
        ],
    )

    suggestions = service.suggest_annotations().suggestions
    assert [(item.object_name, item.object_type) for item in suggestions] == [
        ("SALES_V", "view"),
        ("SALES_V.SALES_ID", "column"),
        ("SALES_MV", "materialized_view"),
    ]
    assert {item.annotation_name for item in suggestions} == {"DESCRIPTION"}

    view_column = service.apply_annotations(
        AnnotationApplyRequest(
            items=[
                {
                    "object_name": "SALES_V.SALES_ID",
                    "object_type": "column",
                    "annotation_value": "売上ID",
                }
            ],
            confirmation="ADMIN_EXECUTE",
        )
    )
    assert view_column.statements[0].sql == (
        'ALTER VIEW "APP"."SALES_V" MODIFY ("SALES_ID" '
        "ANNOTATIONS (ADD OR REPLACE DESCRIPTION '売上ID'));"
    )
    mv_column = service.apply_annotations(
        AnnotationApplyRequest(
            items=[
                {
                    "object_name": "SALES_MV.SALES_ID",
                    "object_type": "column",
                    "annotation_value": "売上ID",
                }
            ],
            confirmation="ADMIN_EXECUTE",
        )
    )
    assert mv_column.statements == []
    assert any("MV の列 annotation" in warning for warning in mv_column.warnings)

    mv_apply = service.apply_annotations(
        AnnotationApplyRequest(
            items=[
                {
                    "object_name": "SALES_MV",
                    "object_type": "materialized view",
                    "annotation_name": "Display",
                    "annotation_value": "売上MV",
                }
            ],
            confirmation="ADMIN_EXECUTE",
        )
    )
    assert mv_apply.statements[0].object_type == "materialized_view"
    assert 'ALTER MATERIALIZED VIEW "APP"."SALES_MV"' in mv_apply.statements[0].sql


def test_annotation_statement_prefers_catalog_object_type_over_request() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._catalog = SchemaCatalog(
        refreshed_at="2026-07-11T00:00:00+00:00",
        tables=[SchemaTable(table_name="EMPLOYEE", logical_name="社員", table_type="table")],
    )

    result = service.apply_annotations(
        AnnotationApplyRequest(
            items=[
                {
                    "object_name": "EMPLOYEE",
                    "object_type": "view",
                    "annotation_name": "Display",
                    "annotation_value": "社員",
                }
            ],
            confirmation="ADMIN_EXECUTE",
        )
    )

    assert result.statements[0].object_type == "table"
    assert 'ALTER TABLE "APP"."EMPLOYEE"' in result.statements[0].sql


def test_comment_apply_rejects_over_4000_byte_comment() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())

    result = service.apply_comments(
        CommentApplyRequest(
            items=[
                {
                    "object_name": "EMPLOYEE",
                    "object_type": "table",
                    "comment": "あ" * 1334,
                }
            ],
            confirmation="ADMIN_EXECUTE",
        )
    )

    assert result.statements == []
    assert any("4000 バイト" in warning for warning in result.warnings)


def test_metadata_sql_target_preservation_uses_object_boundaries() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._enterprise_ai_client = FakeEnterpriseAiClient(
        "COMMENT ON TABLE APP.EMPLOYEE IS '社員';"
    )

    result = service.generate_comment_sql(
        MetadataSqlGenerateRequest(
            targets=[{"owner": "APP", "object_name": "EMP", "object_type": "table"}],
            structure_text="OBJECT: APP.EMP\nTYPE: table\nCOMMENT: EMP",
        )
    )

    assert result.source == "deterministic"
    assert 'COMMENT ON TABLE "APP"."EMP"' in result.sql
    assert result.fallback is True
    assert "owner 修飾を保持しません" in result.fallback_detail


def test_metadata_sql_router_returns_400_for_invalid_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.features.nl2sql import router as nl2sql_router

    class _FailingMetadataSqlService:
        def generate_comment_sql(self, _request: MetadataSqlGenerateRequest) -> None:
            raise ValueError("識別子が不正です。")

        def get_metadata_samples(self, _request: MetadataSqlSampleRequest) -> None:
            raise ValueError("識別子が不正です。")

        def generate_annotation_sql(self, _request: MetadataSqlGenerateRequest) -> None:
            raise ValueError("識別子が不正です。")

    monkeypatch.setattr(nl2sql_router, "nl2sql_service", _FailingMetadataSqlService())

    with pytest.raises(HTTPException) as exc_info:
        nl2sql_router.generate_comment_sql(
            MetadataSqlGenerateRequest(targets=[{"object_name": "EMP", "object_type": "table"}])
        )
    assert exc_info.value.status_code == 400

    with pytest.raises(HTTPException) as samples_exc_info:
        nl2sql_router.metadata_samples(
            MetadataSqlSampleRequest(targets=[{"object_name": "EMP", "object_type": "table"}])
        )
    assert samples_exc_info.value.status_code == 400

    with pytest.raises(HTTPException) as annotation_exc_info:
        nl2sql_router.generate_annotation_sql(
            MetadataSqlGenerateRequest(targets=[{"object_name": "EMP", "object_type": "table"}])
        )
    assert annotation_exc_info.value.status_code == 400


def test_metadata_samples_use_requested_limit_and_generation_context() -> None:
    adapter = _FakeMetadataSamplesAdapter()
    service = _OracleRuntimeService(adapter)
    request = MetadataSqlSampleRequest(
        targets=[
            {
                "object_name": "EMPLOYEE",
                "object_type": "table",
                "columns": ["EMPLOYEE_NAME"],
            },
            {
                "object_name": "V_EMPLOYEE",
                "object_type": "view",
                "columns": ["EMPLOYEE_NAME"],
            },
        ],
        sample_limit=10,
    )

    samples = service.get_metadata_samples(request)

    assert adapter.calls[0][1] == 10
    assert adapter.calls[0][0][1]["object_type"] == "view"
    assert samples.sample_count == 2
    assert "EMPLOYEE_NAME: 山田, 佐藤" in samples.sample_text

    enterprise_ai = FakeEnterpriseAiClient("COMMENT ON TABLE EMPLOYEE IS '社員';")
    service._enterprise_ai_client = enterprise_ai
    service.generate_comment_sql(
        MetadataSqlGenerateRequest(
            targets=[{"object_name": "EMPLOYEE", "object_type": "table"}],
            structure_text="OBJECT: EMPLOYEE",
            sample_text=samples.sample_text,
        )
    )
    assert "<サンプル>" in enterprise_ai.calls[0]["context"]
    assert "EMPLOYEE_NAME: 山田, 佐藤" in enterprise_ai.calls[0]["context"]

    empty = service.get_metadata_samples(request.model_copy(update={"sample_limit": 0}))
    assert empty.sample_text == ""
    assert empty.sample_count == 0
    assert len(adapter.calls) == 1


@pytest.mark.parametrize(
    "object_name", ["PLATFORM_USERS", "RAG_DOCUMENTS", "AGENT_RUNS", "NL2SQL_JOBS", "V$SESSION"]
)
def test_metadata_samples_and_domain_inventory_reject_hidden_targets_before_oracle(
    monkeypatch: pytest.MonkeyPatch, object_name: str
) -> None:
    """システム・共通基盤・他製品の表は、Oracle に問い合わせる前に 400 にする（#943）。"""
    samples_adapter = _FakeMetadataSamplesAdapter()
    samples_service = _OracleRuntimeService(samples_adapter)
    with pytest.raises(ValueError, match=re.escape(object_name)):
        samples_service.get_metadata_samples(
            MetadataSqlSampleRequest(
                targets=[
                    {"object_name": "EMPLOYEE", "object_type": "table", "columns": []},
                    {"object_name": object_name, "object_type": "table", "columns": ["X"]},
                ],
                sample_limit=5,
            )
        )
    assert samples_adapter.calls == []

    domain_adapter = _FakeDomainInventoryAdapter(([], []))
    domain_service = _OracleRuntimeService(domain_adapter)
    with pytest.raises(ValueError, match=re.escape(object_name)):
        domain_service.get_domain_inventory(
            DomainInventoryRequest(targets=[{"object_name": object_name, "object_type": "table"}])
        )
    assert domain_adapter.calls == []

    router = importlib.import_module("app.features.nl2sql.router")
    monkeypatch.setattr(router, "nl2sql_service", domain_service)
    with pytest.raises(HTTPException) as exc_info:
        router.domain_inventory(
            DomainInventoryRequest(targets=[{"object_name": object_name, "object_type": "table"}])
        )
    assert exc_info.value.status_code == 400
    assert domain_adapter.calls == []


def test_metadata_samples_fall_back_to_catalog_when_oracle_fails() -> None:
    service = _OracleRuntimeService(_FakeMetadataSamplesAdapter(fail=True))
    service._catalog = SchemaCatalog(
        refreshed_at="2026-07-11T00:00:00+00:00",
        tables=[
            SchemaTable(
                table_name="EMPLOYEE",
                logical_name="社員",
                columns=[
                    SchemaColumn(
                        column_name="EMPLOYEE_NAME",
                        logical_name="社員名",
                        data_type="VARCHAR2(100)",
                        sample_values=["山田", "佐藤", "鈴木"],
                    )
                ],
            )
        ],
    )

    samples = service.get_metadata_samples(
        MetadataSqlSampleRequest(
            targets=[
                {
                    "object_name": "EMPLOYEE",
                    "object_type": "table",
                    "columns": ["EMPLOYEE_NAME"],
                }
            ],
            sample_limit=2,
        )
    )

    assert samples.sample_text == "OBJECT: EMPLOYEE\nEMPLOYEE_NAME: 山田, 佐藤"
    assert samples.sample_count == 2
    assert any("既存値" in warning for warning in samples.warnings)


def test_drop_view_confirmation_and_requires_oracle() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())

    missing = service.drop_db_admin_view(DbAdminDropViewRequest(view_name="V_EMP_DEPT"))
    assert missing.executed is False
    assert missing.statements[0].status == "confirmation_required"
    assert 'DROP VIEW "APP"."V_EMP_DEPT"' in missing.statements[0].sql

    requires_oracle = service.drop_db_admin_view(
        DbAdminDropViewRequest(view_name="V_EMP_DEPT", confirmation="V_EMP_DEPT")
    )
    assert requires_oracle.statements[0].status == "requires_oracle"


def test_truncate_table_requires_target_confirmation_and_oracle() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())

    missing = service.truncate_db_admin_table(DbAdminTruncateTableRequest(table_name="INVOICES"))
    assert missing.executed is False
    assert missing.statements[0].status == "confirmation_required"
    assert missing.statements[0].statement_type == "TRUNCATE"
    assert 'TRUNCATE TABLE "APP"."INVOICES"' in missing.statements[0].sql

    admin_execute = service.truncate_db_admin_table(
        DbAdminTruncateTableRequest(table_name="INVOICES", confirmation="ADMIN_EXECUTE")
    )
    assert admin_execute.executed is False
    assert admin_execute.statements[0].status == "confirmation_required"
    assert any("ADMIN_EXECUTE では代替できません" in warning for warning in admin_execute.warnings)

    requires_oracle = service.truncate_db_admin_table(
        DbAdminTruncateTableRequest(table_name="INVOICES", confirmation="INVOICES")
    )
    assert requires_oracle.executed is False
    assert requires_oracle.statements[0].status == "requires_oracle"
    assert requires_oracle.statements[0].sql == 'TRUNCATE TABLE "APP"."INVOICES"'


def test_db_admin_direct_object_operations_block_nl2sql_namespace() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    csv_text = "ID\n1\n"
    encoded = base64.b64encode(csv_text.encode()).decode()

    with pytest.raises(ValueError, match="システムテーブル管理"):
        service.get_db_admin_object("NL2SQL_SCHEMA_OBJECTS", "table")
    with pytest.raises(ValueError, match="システムテーブル管理"):
        service.preview_db_admin_data(
            DbAdminDataPreviewRequest(object_name="NL2SQL_SCHEMA_OBJECTS")
        )
    with pytest.raises(ValueError, match="システムテーブル管理"):
        service.drop_db_admin_table(
            DbAdminDropTableRequest(
                table_name="NL2SQL_SCHEMA_OBJECTS",
                confirmation="NL2SQL_SCHEMA_OBJECTS",
            )
        )
    with pytest.raises(ValueError, match="システムテーブル管理"):
        service.truncate_db_admin_table(
            DbAdminTruncateTableRequest(
                table_name="NL2SQL_SCHEMA_OBJECTS",
                confirmation="NL2SQL_SCHEMA_OBJECTS",
            )
        )
    with pytest.raises(ValueError, match="システムテーブル管理"):
        service.upload_db_admin_csv(
            DbAdminCsvUploadRequest(
                table_name="NL2SQL_SCHEMA_OBJECTS",
                content_base64=encoded,
                filename="upload.csv",
                confirmation="NL2SQL_SCHEMA_OBJECTS",
            )
        )
    with pytest.raises(ValueError, match="システムテーブル管理"):
        service.import_db_admin_tabular(
            DbAdminImportTabularRequest(
                table_name="NL2SQL_IMPORTED",
                content_base64=encoded,
                filename="import.csv",
                confirmation="ADMIN_EXECUTE",
            )
        )


def test_nl2sql_execute_safety_blocks_system_tables() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._catalog = SchemaCatalog(  # noqa: SLF001
        refreshed_at="2026-07-11T00:00:00+00:00",
        tables=[
            SchemaTable(
                owner="APP",
                table_name="NL2SQL_SCHEMA_OBJECTS",
                logical_name="system",
                columns=[
                    SchemaColumn(
                        column_name="ID",
                        logical_name="ID",
                        data_type="NUMBER",
                    )
                ],
            )
        ],
    )

    safety, _sql, results = service.execute_sql(
        "SELECT * FROM NL2SQL_SCHEMA_OBJECTS",
        AllowedObjects(enforce_table_scope=False),
        10,
    )

    assert safety.is_safe is False
    assert "システムテーブル管理" in safety.blocked_reason
    assert results.total == 0


def test_truncate_table_executes_quoted_statement_in_oracle_runtime() -> None:
    adapter = _FakeStatementsAdapter(
        [
            {
                "index": 1,
                "statement_type": "TRUNCATE",
                "status": "success",
                "sql": 'TRUNCATE TABLE "INVOICES"',
                "row_count": 0,
                "message": "OK",
                "elapsed_ms": 1,
            }
        ]
    )
    service = _OracleRuntimeService(adapter)

    result = service.truncate_db_admin_table(
        DbAdminTruncateTableRequest(
            table_name="INVOICES",
            confirmation="INVOICES",
            reason="ui-data-management-truncate",
        )
    )

    assert result.executed is True
    assert result.committed is True
    assert adapter.calls == [(['TRUNCATE TABLE "APP"."INVOICES"'], False)]


def test_truncate_table_blocks_catalog_views_and_invalid_names() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._catalog = SchemaCatalog(
        refreshed_at="2026-07-11T00:00:00+00:00",
        tables=[
            SchemaTable(
                table_name="V_EMP_DEPT",
                logical_name="社員部署ビュー",
                table_type="view",
            )
        ],
    )

    view = service.truncate_db_admin_table(
        DbAdminTruncateTableRequest(table_name="V_EMP_DEPT", confirmation="V_EMP_DEPT")
    )
    assert view.executed is False
    assert view.statements[0].status == "blocked"
    assert any("ビューは TRUNCATE できません" in warning for warning in view.warnings)

    adapter = _FakeObjectTypeAdapter("VIEW")
    oracle_service = _OracleRuntimeService(adapter)
    oracle_service._catalog = SchemaCatalog(
        refreshed_at="2026-07-11T00:00:00+00:00",
        tables=[],
    )
    oracle_view = oracle_service.truncate_db_admin_table(
        DbAdminTruncateTableRequest(table_name="V_EMP_DEPT", confirmation="V_EMP_DEPT")
    )
    assert oracle_view.executed is False
    assert oracle_view.statements[0].status == "blocked"
    assert adapter.object_type_calls == ["APP.V_EMP_DEPT"]
    assert adapter.calls == []

    with pytest.raises(ValueError):
        service.truncate_db_admin_table(
            DbAdminTruncateTableRequest(table_name='INVOICES"; DROP TABLE USERS --')
        )


def test_preview_data_builds_guarded_select() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())

    plain = service.preview_db_admin_data(DbAdminDataPreviewRequest(object_name="INVOICES"))
    assert plain.runtime == "deterministic"
    # 行数上限は SQL へ書き足さず、取得時の fetch 上限だけで効かせる。
    assert plain.sql == 'SELECT * FROM "APP"."INVOICES"'
    assert plain.results.columns

    filtered = service.preview_db_admin_data(
        DbAdminDataPreviewRequest(
            object_name="INVOICES",
            limit=10,
            where_clause="STATUS = 'A' AND TOTAL_AMOUNT > 100",
        )
    )
    assert "WHERE STATUS = 'A' AND TOTAL_AMOUNT > 100" in filtered.sql
    assert "FETCH FIRST" not in filtered.sql

    with pytest.raises(ValueError, match="複数 statement"):
        service.preview_db_admin_data(
            DbAdminDataPreviewRequest(
                object_name="INVOICES",
                where_clause="1=1; DROP TABLE INVOICES",
            )
        )

    # 先頭の WHERE キーワードは重複しないよう正規化される
    normalized = service.preview_db_admin_data(
        DbAdminDataPreviewRequest(object_name="INVOICES", where_clause="WHERE STATUS = 'X'")
    )
    assert "WHERE STATUS = 'X'" in normalized.sql
    assert "WHERE WHERE" not in normalized.sql

    # システム object は import 用 sanitizer で別名化せず、preview 前に止める。
    with pytest.raises(ValueError, match="システムテーブル管理"):
        service.preview_db_admin_data(
            DbAdminDataPreviewRequest(object_name="DBTOOLS$EXECUTION_HISTORY")
        )

    with pytest.raises(ValueError, match="システムテーブル管理"):
        service.preview_db_admin_data(
            DbAdminDataPreviewRequest(
                object_name="INVOICES",
                where_clause="1=0 UNION ALL SELECT STATE_KEY FROM NL2SQL_STATE",
            )
        )

    with pytest.raises(ValueError, match="Oracle 識別子"):
        service.preview_db_admin_data(DbAdminDataPreviewRequest(object_name='ADMIN"."SECRET'))


def test_preview_data_max_limit_reaches_preview_and_export() -> None:
    adapter = _FakeAdminSqlAdapter()
    service = _OracleRuntimeService(adapter)

    preview = service.preview_db_admin_data(
        DbAdminDataPreviewRequest(object_name="INVOICES", limit=100000)
    )

    assert preview.runtime == "oracle"
    assert adapter.select_calls[-1][1] == 100000

    filename, content = service.export_db_admin_preview_xlsx(
        DbAdminDataPreviewRequest(object_name="INVOICES", limit=100000)
    )

    assert filename == "app_invoices_preview.xlsx"
    assert content
    assert adapter.select_calls[-1][1] == 100000


def test_oracle_adapter_execute_select_normalizes_driver_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingCursor:
        def __enter__(self) -> _FailingCursor:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, _sql: str) -> None:
            raise RuntimeError("ORA-00942: table or view does not exist")

    class _FailingConnection:
        def cursor(self) -> _FailingCursor:
            return _FailingCursor()

    @contextmanager
    def failing_connection(**_kwargs: object) -> Iterator[_FailingConnection]:
        yield _FailingConnection()

    adapter = OracleNl2SqlAdapter(get_settings())
    monkeypatch.setattr(adapter, "user_data_connection", failing_connection)

    with pytest.raises(OracleAdapterError, match="SELECT の実行に失敗しました.*ORA-00942"):
        adapter.execute_select('SELECT * FROM "MISSING_TABLE"', 100)


def test_oracle_adapter_lists_quoted_catalog_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Cursor:
        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, _sql: str, _binds: dict[str, object]) -> None:
            return None

        def fetchall(self) -> list[tuple[str, str, int | None, str]]:
            return [
                ("lower", "APP", 2, "小文字テーブル"),
                ("売上", "APP", None, "売上ビュー"),
            ]

    class _Connection:
        def cursor(self) -> _Cursor:
            return _Cursor()

    @contextmanager
    def fake_connection() -> Iterator[_Connection]:
        yield _Connection()

    adapter = OracleNl2SqlAdapter(get_settings())
    monkeypatch.setattr(adapter, "connection", fake_connection)

    objects = adapter.list_db_admin_objects("table")

    assert [item["qualified_name"] for item in objects] == ['APP."lower"', 'APP."売上"']


@pytest.mark.parametrize(
    ("actual_type", "expected_comment_object_type", "expected_ddl_type"),
    [
        ("VIEW", "TABLE", "VIEW"),
        ("MATERIALIZED VIEW", "MATERIALIZED VIEW", "MATERIALIZED_VIEW"),
    ],
)
def test_oracle_adapter_uses_oracle_comment_syntax_for_view_ddl(
    monkeypatch: pytest.MonkeyPatch,
    actual_type: str,
    expected_comment_object_type: str,
    expected_ddl_type: str,
) -> None:
    class _Cursor:
        def __init__(self) -> None:
            self.rows: list[tuple[Any, ...]] = []
            self.ddl_binds: dict[str, object] = {}

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, sql: str, binds: dict[str, object] | None = None) -> None:
            if "FROM all_tab_columns" in sql:
                self.rows = [("ID", "NUMBER", "N", "識別子")]
            elif "FROM all_tab_comments" in sql:
                self.rows = [("ビューコメント",)]
            elif "FROM all_objects" in sql:
                self.rows = [(actual_type,)]
            elif "DBMS_METADATA.GET_DDL" in sql:
                self.ddl_binds = dict(binds or {})
                self.rows = [('CREATE OR REPLACE VIEW "APP"."V1" AS SELECT 1 ID FROM DUAL',)]
            else:
                self.rows = []

        def __iter__(self) -> Iterator[tuple[Any, ...]]:
            return iter(self.rows)

        def fetchone(self) -> tuple[Any, ...] | None:
            return self.rows[0] if self.rows else None

    class _Connection:
        def __init__(self, cursor: _Cursor) -> None:
            self._cursor = cursor

        def cursor(self) -> _Cursor:
            return self._cursor

    cursor = _Cursor()

    @contextmanager
    def fake_connection() -> Iterator[_Connection]:
        yield _Connection(cursor)

    adapter = OracleNl2SqlAdapter(get_settings())
    monkeypatch.setattr(adapter, "connection", fake_connection)

    detail = adapter.get_db_admin_object_detail(
        object_name="V1",
        owner="APP",
        object_type="view",
        include_ddl=True,
    )

    assert cursor.ddl_binds["object_type"] == expected_ddl_type
    assert f"COMMENT ON {expected_comment_object_type}" in detail["ddl"]
    assert "COMMENT ON VIEW" not in detail["ddl"]


def test_oracle_adapter_system_admin_select_uses_normal_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Cursor:
        description = [("ID",)]

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, _sql: str) -> None:
            return None

        def fetchmany(self, _max_rows: int) -> list[tuple[int]]:
            return [(1,)]

    class _Connection:
        def cursor(self) -> _Cursor:
            return _Cursor()

    @contextmanager
    def normal_connection() -> Iterator[_Connection]:
        yield _Connection()

    settings = get_settings()
    adapter = OracleNl2SqlAdapter(settings)
    monkeypatch.setattr(adapter, "runtime_connection", normal_connection)

    with actor_scope("system-admin", is_system_admin=True):
        result = adapter.execute_select("SELECT ID FROM T1", 100)

    assert result.rows == [{"ID": 1}]
    assert result.execution_context == "oracle_data_plane"


def test_oracle_adapter_unbounded_select_fetches_all_rows_in_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Cursor:
        description = [("ID",)]

        def __init__(self) -> None:
            self.remaining = [(index,) for index in range(125)]
            self.fetchmany_calls: list[int] = []

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, _sql: str) -> None:
            return None

        def fetchmany(self, max_rows: int) -> list[tuple[int]]:
            self.fetchmany_calls.append(max_rows)
            batch = self.remaining[:50]
            self.remaining = self.remaining[50:]
            return batch

        def fetchall(self) -> list[tuple[int]]:
            raise AssertionError("row_limit 未指定でも fetchall は使わない")

    class _Connection:
        def __init__(self, cursor: _Cursor) -> None:
            self._cursor = cursor

        def cursor(self) -> _Cursor:
            return self._cursor

    cursor = _Cursor()

    @contextmanager
    def normal_connection() -> Iterator[_Connection]:
        yield _Connection(cursor)

    adapter = OracleNl2SqlAdapter(get_settings())
    monkeypatch.setattr(adapter, "runtime_connection", normal_connection)

    with actor_scope("system-admin", is_system_admin=True):
        result = adapter.execute_select("SELECT ID FROM T1", None)

    assert result.total == 125
    assert result.returned_count == 125
    assert result.has_more is False
    assert result.truncated is False
    assert cursor.fetchmany_calls == [
        SELECT_FETCH_BATCH_SIZE,
        SELECT_FETCH_BATCH_SIZE,
        SELECT_FETCH_BATCH_SIZE,
        SELECT_FETCH_BATCH_SIZE,
    ]


def test_oracle_adapter_explicit_select_limit_reports_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Cursor:
        description = [("ID",)]

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, _sql: str) -> None:
            return None

        def fetchmany(self, max_rows: int) -> list[tuple[int]]:
            assert max_rows == 4
            return [(1,), (2,), (3,), (4,)]

    class _Connection:
        def cursor(self) -> _Cursor:
            return _Cursor()

    @contextmanager
    def normal_connection() -> Iterator[_Connection]:
        yield _Connection()

    adapter = OracleNl2SqlAdapter(get_settings())
    monkeypatch.setattr(adapter, "runtime_connection", normal_connection)

    with actor_scope("system-admin", is_system_admin=True):
        result = adapter.execute_select("SELECT ID FROM T1", 3)

    assert result.rows == [{"ID": 1}, {"ID": 2}, {"ID": 3}]
    assert result.total == 3
    assert result.returned_count == 3
    assert result.has_more is True
    assert result.truncated is True


def test_oracle_adapter_non_admin_select_uses_runtime_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _Cursor:
        description = [("ID",)]

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, sql: str) -> None:
            calls.append(sql)

        def fetchmany(self, _max_rows: int) -> list[tuple[int]]:
            return [(3,)]

    class _Connection:
        def cursor(self) -> _Cursor:
            return _Cursor()

    @contextmanager
    def normal_connection() -> Iterator[_Connection]:
        yield _Connection()

    settings = get_settings()
    adapter = OracleNl2SqlAdapter(settings)
    monkeypatch.setattr(adapter, "runtime_connection", normal_connection)

    with actor_scope("business-user", is_system_admin=False):
        result = adapter.execute_select("SELECT ID FROM T1", 100)

    # 業務データの SELECT の間だけ、session の時間帯を共通の設定の時間帯にする（#3265）。
    assert calls == [
        "ALTER SESSION SET TIME_ZONE = '+09:00'",
        "SELECT ID FROM T1",
        "ALTER SESSION SET TIME_ZONE = '+00:00'",
    ]
    assert result.rows == [{"ID": 3}]
    assert result.execution_context == "oracle_data_plane"


def test_oracle_adapter_explain_uses_normal_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _Cursor:
        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, sql: str, *_args: object) -> None:
            calls.append(sql)

        def __iter__(self) -> Iterator[tuple[object, ...]]:
            return iter(())

    class _Connection:
        def cursor(self) -> _Cursor:
            return _Cursor()

        def commit(self) -> None:
            calls.append("COMMIT")

    @contextmanager
    def normal_connection() -> Iterator[_Connection]:
        yield _Connection()

    def forbidden_data_connection() -> Iterator[_Connection]:
        raise AssertionError("EXPLAIN must not use the business data connection")

    settings = get_settings()
    adapter = OracleNl2SqlAdapter(settings)
    monkeypatch.setattr(adapter, "connection", normal_connection)
    monkeypatch.setattr(adapter, "user_data_connection", forbidden_data_connection)

    with actor_scope("business-user", is_system_admin=False):
        plan = adapter.explain_select("SELECT ID FROM T1")

    assert plan.available is False
    assert any("EXPLAIN PLAN" in sql for sql in calls)
    assert calls[-1] == "COMMIT"


def test_oracle_adapter_generation_samples_use_normal_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _Cursor:
        def __init__(self) -> None:
            self.rows: list[tuple[str]] = []

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, sql: str, *_args: object) -> None:
            calls.append(sql)
            if "SELECT column_name FROM all_tab_columns" in sql:
                self.rows = [("ID",)]
            else:
                self.rows = [("sample-value",)]

        def __iter__(self) -> Iterator[tuple[str]]:
            return iter(self.rows)

    class _Connection:
        def __init__(self) -> None:
            self.cursor_instance = _Cursor()

        def cursor(self) -> _Cursor:
            return self.cursor_instance

    @contextmanager
    def normal_connection() -> Iterator[_Connection]:
        yield _Connection()

    def forbidden_data_connection() -> Iterator[_Connection]:
        raise AssertionError("generation samples must not use the business data connection")

    settings = get_settings()
    adapter = OracleNl2SqlAdapter(settings)
    monkeypatch.setattr(adapter, "connection", normal_connection)
    monkeypatch.setattr(adapter, "user_data_connection", forbidden_data_connection)

    with actor_scope("business-user", is_system_admin=False):
        samples, warnings = adapter.fetch_metadata_sample_values(
            [{"owner": "APP", "object_name": "T1", "columns": ["ID"]}],
            1,
        )

    assert samples == {"APP.T1": {"ID": ["sample-value"]}}
    assert warnings == []
    assert any("all_tab_columns" in sql for sql in calls)


def test_preview_data_exports_xlsx() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())

    filename, content = service.export_db_admin_preview_xlsx(
        DbAdminDataPreviewRequest(object_name="INVOICES", limit=10, where_clause="STATUS = 'A'")
    )

    openpyxl = importlib.import_module("openpyxl")
    workbook = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    assert filename == "app_invoices_preview.xlsx"
    assert workbook.sheetnames == ["data", "query"]
    assert workbook["data"].max_row >= 1
    assert workbook["query"]["A2"].value == 'SELECT * FROM "APP"."INVOICES" WHERE STATUS = \'A\''


def test_preview_data_normalizes_special_values_for_api_and_xlsx() -> None:
    adapter = _FakeAdminSqlAdapter(
        select_result=QueryResults(
            columns=["RAW_VALUE", "META", "FORMULA", "CREATED_AT", "CONTROL_TEXT"],
            rows=[
                {
                    "RAW_VALUE": b"\x0f\xff",
                    "META": {"status": "ok"},
                    "FORMULA": "=1+1",
                    "CREATED_AT": datetime(2026, 1, 31, 9, 30, tzinfo=UTC),
                    "CONTROL_TEXT": "A\x01B",
                }
            ],
            total=1,
            execution_context="oracle_data_plane",
        )
    )
    service = _OracleRuntimeService(adapter)
    request = DbAdminDataPreviewRequest(object_name="INVOICES", limit=10)

    preview = service.preview_db_admin_data(request)

    assert preview.results.rows[0] == {
        "RAW_VALUE": "0FFF",
        "META": '{"status": "ok"}',
        "FORMULA": "=1+1",
        "CREATED_AT": "2026-01-31T09:30:00+00:00",
        "CONTROL_TEXT": "AB",
    }

    _filename, content = service.export_db_admin_preview_xlsx(request)
    openpyxl = importlib.import_module("openpyxl")
    workbook = openpyxl.load_workbook(io.BytesIO(content), data_only=False)

    assert workbook["data"]["C2"].value == "=1+1"
    assert workbook["data"]["C2"].data_type == "s"


def test_cross_schema_management_resolves_duplicate_object_names() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._catalog = SchemaCatalog(
        refreshed_at="2026-07-10T00:00:00+00:00",
        current_owner="APP",
        tables=[
            SchemaTable(
                table_name="ORDERS",
                logical_name="自社注文",
                owner="APP",
                row_count=2,
                columns=[
                    SchemaColumn(
                        column_name="ORDER_ID",
                        logical_name="注文ID",
                        data_type="NUMBER",
                    ),
                    SchemaColumn(
                        column_name="ORDER_NAME",
                        logical_name="注文名",
                        data_type="VARCHAR2(100)",
                    ),
                    SchemaColumn(
                        column_name="AMOUNT",
                        logical_name="金額",
                        data_type="NUMBER",
                    ),
                    SchemaColumn(
                        column_name="ORDER_DATE",
                        logical_name="注文日",
                        data_type="DATE",
                    ),
                ],
            ),
            SchemaTable(
                table_name="ORDERS",
                logical_name="共有注文",
                owner="SH",
                row_count=8,
                columns=[
                    SchemaColumn(
                        column_name="ORDER_ID",
                        logical_name="注文ID",
                        data_type="NUMBER",
                    ),
                    SchemaColumn(
                        column_name="ORDER_NAME",
                        logical_name="注文名",
                        data_type="VARCHAR2(100)",
                    ),
                    SchemaColumn(
                        column_name="AMOUNT",
                        logical_name="金額",
                        data_type="NUMBER",
                    ),
                    SchemaColumn(
                        column_name="ORDER_DATE",
                        logical_name="注文日",
                        data_type="DATE",
                    ),
                ],
            ),
        ],
    )

    page = service.list_db_admin_objects_page(
        cursor=None,
        limit=10,
        query="orders",
        object_type="table",
        row_state="all",
    )
    assert {item.qualified_name for item in page.items} == {"APP.ORDERS", "SH.ORDERS"}

    detail = service.get_db_admin_object("ORDERS", "table", owner="SH", include_ddl=False)
    assert detail.owner == "SH"
    assert detail.qualified_name == "SH.ORDERS"
    assert detail.row_count == 8

    preview = service.preview_db_admin_data(
        DbAdminDataPreviewRequest(object_name="ORDERS", owner="SH", limit=5)
    )
    assert preview.sql == 'SELECT * FROM "SH"."ORDERS"'


def test_db_admin_quoted_catalog_objects_remain_actionable() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._catalog = SchemaCatalog(
        refreshed_at="2026-07-10T00:00:00+00:00",
        current_owner="APP",
        tables=[
            SchemaTable(
                table_name="lower",
                logical_name="小文字テーブル",
                owner="APP",
                row_count=2,
                columns=[
                    SchemaColumn(
                        column_name="ID",
                        logical_name="ID",
                        data_type="NUMBER",
                    )
                ],
            ),
            SchemaTable(
                table_name="売上",
                logical_name="売上",
                owner="APP",
                row_count=3,
                columns=[
                    SchemaColumn(
                        column_name="ID",
                        logical_name="ID",
                        data_type="NUMBER",
                    )
                ],
            ),
        ],
    )

    tables = service.list_db_admin_tables()
    assert [item.qualified_name for item in tables.items] == ['APP."lower"', 'APP."売上"']

    detail = service.get_db_admin_object('APP."lower"', "table")
    assert detail.name == "lower"
    assert detail.owner == "APP"
    assert detail.qualified_name == 'APP."lower"'
    assert 'CREATE TABLE "APP"."lower"' in detail.ddl

    preview = service.preview_db_admin_data(
        DbAdminDataPreviewRequest(object_name='APP."売上"', limit=5)
    )
    assert preview.sql == 'SELECT * FROM "APP"."売上"'

    dropped = service.drop_db_admin_table(
        DbAdminDropTableRequest(table_name='APP."lower"', confirmation='APP."lower"')
    )
    assert dropped.executed is False
    assert dropped.statements[0].status == "requires_oracle"
    assert dropped.statements[0].sql == 'DROP TABLE "APP"."lower" PURGE'


def test_cross_schema_mutations_and_metadata_sql_use_qualified_targets() -> None:
    adapter = _FakeStatementsAdapter(
        [
            {
                "index": 1,
                "statement_type": "TRUNCATE",
                "status": "success",
                "sql": 'TRUNCATE TABLE "SH"."ORDERS"',
                "row_count": 0,
                "message": "OK",
                "elapsed_ms": 1,
            }
        ]
    )
    service = _OracleRuntimeService(adapter)
    service._catalog = SchemaCatalog(
        refreshed_at="2026-07-10T00:00:00+00:00",
        current_owner="APP",
        tables=[
            SchemaTable(
                table_name="ORDERS",
                logical_name="共有注文",
                owner="SH",
                row_count=8,
                comment="共有注文",
                columns=[
                    SchemaColumn(
                        column_name="ORDER_ID",
                        logical_name="注文ID",
                        data_type="NUMBER",
                    )
                ],
            )
        ],
    )
    service._enterprise_ai_client = FakeEnterpriseAiClient(configured=False)

    result = service.truncate_db_admin_table(
        DbAdminTruncateTableRequest(
            table_name="ORDERS",
            owner="SH",
            confirmation="SH.ORDERS",
            reason="cross-schema-test",
        )
    )
    assert result.executed is True
    assert adapter.calls == [(['TRUNCATE TABLE "SH"."ORDERS"'], False)]

    comment = service.generate_comment_sql(
        MetadataSqlGenerateRequest(
            targets=[{"owner": "SH", "object_name": "ORDERS", "object_type": "TABLE"}],
            structure_text="OBJECT: SH.ORDERS",
        )
    )
    assert 'COMMENT ON TABLE "SH"."ORDERS"' in comment.sql

    annotation = service.generate_annotation_sql(
        MetadataSqlGenerateRequest(
            targets=[{"owner": "SH", "object_name": "ORDERS", "object_type": "table"}],
            structure_text="OBJECT: SH.ORDERS",
        )
    )
    assert 'ALTER TABLE "SH"."ORDERS"' in annotation.sql


def test_table_export_xlsx_contains_column_information_only() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._catalog = SchemaCatalog(
        refreshed_at="2026-07-10T00:00:00+00:00",
        tables=[
            SchemaTable(
                table_name="INVOICES",
                logical_name="請求",
                owner="APP",
                row_count=2,
                comment="請求情報",
                columns=[
                    SchemaColumn(
                        column_name="CUSTOMER_NAME",
                        logical_name="取引先名",
                        data_type="VARCHAR2(120)",
                        nullable=False,
                        comment="取引先名",
                        sample_values=["架空商事"],
                    ),
                    SchemaColumn(
                        column_name="TOTAL_AMOUNT",
                        logical_name="請求金額",
                        data_type="NUMBER",
                        nullable=False,
                        comment="税込請求金額",
                        sample_values=["1200000"],
                    ),
                ],
            )
        ],
    )

    filename, content = service.export_db_admin_table_xlsx("INVOICES")

    openpyxl = importlib.import_module("openpyxl")
    workbook = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    assert filename == "app_invoices_columns.xlsx"
    assert workbook.sheetnames == ["columns"]
    sheet = workbook["columns"]
    assert [sheet.cell(row=1, column=index).value for index in range(1, 7)] == [
        "物理名",
        "論理名",
        "コメント",
        "型",
        "NULL 可",
        "サンプル",
    ]
    # 論理名はオントロジー業務名のみを正とするため、未構築時は空(表示は "-")
    assert [sheet.cell(row=2, column=index).value for index in range(1, 7)] == [
        "CUSTOMER_NAME",
        "-",
        "取引先名",
        "VARCHAR2(120)",
        "NO",
        "架空商事",
    ]


def test_view_export_xlsx_contains_column_information_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    # 論理名はオントロジー業務名だけを正とする。共有 ontology_runtime(memory store)に
    # 他テストが公開した revision が残っていても影響を受けないよう lookup を固定する。
    monkeypatch.setattr(service, "_ontology_business_names", lambda **_kwargs: {})
    service._catalog = SchemaCatalog(
        refreshed_at="2026-07-10T00:00:00+00:00",
        tables=[
            SchemaTable(
                table_name="V_EMP_DEPT",
                logical_name="社員部署ビュー",
                owner="APP",
                table_type="view",
                row_count=None,
                comment="社員と部署",
                columns=[
                    SchemaColumn(
                        column_name="EMPLOYEE_NAME",
                        logical_name="社員名",
                        data_type="VARCHAR2(120)",
                        nullable=False,
                        comment="社員名",
                        sample_values=["山田太郎"],
                    ),
                    SchemaColumn(
                        column_name="DEPARTMENT_NAME",
                        logical_name="部署名",
                        data_type="VARCHAR2(80)",
                        nullable=True,
                        comment="部署名",
                        sample_values=["営業部"],
                    ),
                ],
            )
        ],
    )

    filename, content = service.export_db_admin_view_xlsx("V_EMP_DEPT")

    openpyxl = importlib.import_module("openpyxl")
    workbook = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    assert filename == "app_v_emp_dept_columns.xlsx"
    assert workbook.sheetnames == ["columns"]
    sheet = workbook["columns"]
    assert [sheet.cell(row=1, column=index).value for index in range(1, 7)] == [
        "物理名",
        "論理名",
        "コメント",
        "型",
        "NULL 可",
        "サンプル",
    ]
    assert [sheet.cell(row=2, column=index).value for index in range(1, 7)] == [
        "EMPLOYEE_NAME",
        "-",
        "社員名",
        "VARCHAR2(120)",
        "NO",
        "山田太郎",
    ]


def _invoices_catalog(*, column_logical: str, column_comment: str) -> SchemaCatalog:
    return SchemaCatalog(
        refreshed_at="2026-07-10T00:00:00+00:00",
        tables=[
            SchemaTable(
                table_name="INVOICES",
                logical_name="請求",
                owner="APP",
                columns=[
                    SchemaColumn(
                        column_name="CUSTOMER_NAME",
                        logical_name=column_logical,
                        data_type="VARCHAR2(120)",
                        nullable=False,
                        comment=column_comment,
                    ),
                ],
            )
        ],
    )


def test_get_db_admin_object_uses_ontology_business_name_for_logical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """論理名はオントロジー業務名、コメントは生カラムコメント(別ソース)になる。"""
    from app.features.nl2sql import ontology_router

    calls: list[dict[str, str]] = []

    class _StubRuntime:
        def column_business_names(self, **kwargs: str) -> dict[str, str]:
            calls.append(kwargs)
            return {"CUSTOMER_NAME": "得意先名称"}

        def current_ontology(self) -> Any:
            raise AssertionError("table detail must not load the full ontology")

    monkeypatch.setattr(ontology_router, "ontology_runtime", _StubRuntime())

    service = Nl2SqlService(store=MemoryNl2SqlStore())
    # detail 側の logical_name は生コメント由来(サービスがオントロジー業務名で上書き)
    service._catalog = _invoices_catalog(column_logical="取引先名", column_comment="取引先名")

    detail = service.get_db_admin_object("INVOICES", "table")
    column = detail.columns[0]
    assert column.logical_name == "得意先名称"  # オントロジー業務名で上書き
    assert column.comment == "取引先名"  # 生カラムコメントは保持
    assert calls == [{"owner": "APP", "object_name": "INVOICES", "object_type": "table"}]


def test_get_db_admin_object_blanks_logical_when_ontology_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """オントロジー取得に失敗しても例外を出さず、論理名は空にする(コメントを流用しない)。"""
    from app.features.nl2sql import ontology_router

    class _BrokenRuntime:
        def column_business_names(self, **_kwargs: str) -> dict[str, str]:
            raise RuntimeError("ontology unavailable")

    monkeypatch.setattr(ontology_router, "ontology_runtime", _BrokenRuntime())

    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._catalog = _invoices_catalog(column_logical="取引先名", column_comment="取引先名")

    detail = service.get_db_admin_object("INVOICES", "table")
    column = detail.columns[0]
    assert column.logical_name == ""  # 業務名未設定なら論理名は空
    assert column.comment == "取引先名"  # 生カラムコメントは保持


def test_get_db_admin_object_blanks_logical_when_no_ontology_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """オントロジーは在るが該当カラムに業務名が無い場合も論理名は空にする。"""
    from app.features.nl2sql import ontology_router

    class _EmptyRuntime:
        def column_business_names(self, **_kwargs: str) -> dict[str, str]:
            return {}

    monkeypatch.setattr(ontology_router, "ontology_runtime", _EmptyRuntime())

    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._catalog = _invoices_catalog(column_logical="取引先名", column_comment="取引先名")

    detail = service.get_db_admin_object("INVOICES", "table")
    column = detail.columns[0]
    assert column.logical_name == ""
    assert column.comment == "取引先名"


def test_get_db_admin_object_skips_ddl_when_include_ddl_false() -> None:
    """include_ddl=False で重い DDL 生成を省略し ddl="" を返す(列・行数は保持)。"""
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._catalog = _invoices_catalog(column_logical="取引先名", column_comment="取引先名")

    with_ddl = service.get_db_admin_object("INVOICES", "table")
    assert with_ddl.ddl != ""  # 既定は DDL 込み(後方互換)

    without_ddl = service.get_db_admin_object("INVOICES", "table", include_ddl=False)
    assert without_ddl.ddl == ""  # DDL 省略
    # 列・行数など DDL 以外は従来どおり
    assert [c.column_name for c in without_ddl.columns] == [c.column_name for c in with_ddl.columns]
    assert without_ddl.row_count == with_ddl.row_count


def _catalog_with_samples() -> SchemaCatalog:
    return SchemaCatalog(
        refreshed_at="2026-07-11T09:30:00+00:00",
        tables=[
            SchemaTable(
                table_name="ORDERS",
                logical_name="注文",
                owner="APP",
                row_count=4200,
                columns=[
                    SchemaColumn(
                        column_name="STATUS",
                        logical_name="状態",
                        data_type="VARCHAR2(20)",
                        nullable=False,
                        comment="注文状態",
                        sample_values=["NEW", "PAID", "SHIPPED"],
                    ),
                ],
            )
        ],
    )


def test_get_db_admin_object_merges_sample_values_and_num_rows() -> None:
    """詳細は catalog から該当テーブルの sample_values を補完し、行数は num_rows 統計を使う。"""
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._catalog = _catalog_with_samples()

    detail = service.get_db_admin_object("ORDERS", "table")
    assert detail.row_count == 4200  # num_rows 統計(COUNT(*) を撃たない)
    assert detail.columns[0].sample_values == ["NEW", "PAID", "SHIPPED"]


def test_list_db_admin_tables_includes_refreshed_at() -> None:
    """一覧応答に catalog の refreshed_at を載せる(ヘッダ用途を catalog 全取得なしで賄う)。"""
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._catalog = _catalog_with_samples()

    listed = service.list_db_admin_tables()
    assert listed.refreshed_at == "2026-07-11T09:30:00+00:00"


def test_upload_csv_validates_confirmation_and_matches_catalog_columns() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    _import_sample(service)
    table = service._catalog.tables[0]
    known_column = table.columns[0].column_name
    csv_text = f"{known_column},UNKNOWN_COLUMN\n1,foo\n2,bar\n"
    import base64

    result = service.upload_db_admin_csv(
        DbAdminCsvUploadRequest(
            table_name=table.table_name,
            content_base64=base64.b64encode(csv_text.encode()).decode(),
            filename="upload.csv",
        )
    )
    assert result.executed is False
    assert any("confirmation=" in warning for warning in result.warnings)
    assert known_column.upper() in result.matched_columns
    assert "UNKNOWN_COLUMN" in result.unmatched_csv_columns
    assert result.row_count == 2

    requires_oracle = service.upload_db_admin_csv(
        DbAdminCsvUploadRequest(
            table_name=table.table_name,
            content_base64=base64.b64encode(csv_text.encode()).decode(),
            confirmation=result.table_name,
        )
    )
    assert requires_oracle.executed is False
    assert any("NL2SQL_RUNTIME_MODE=oracle" in warning for warning in requires_oracle.warnings)


def test_upload_csv_oracle_empty_unmatched_overrides_stale_catalog() -> None:
    """Oracle 実行結果の空配列を正とし、事前 catalog 照合の古い不一致列へ戻さない。"""

    adapter = _FakeCsvUploadAdapter(
        {
            "matched_columns": ["ID", "NAME"],
            "unmatched_csv_columns": [],
            "success_count": 2,
            "error_count": 0,
            "row_errors": [],
            "hint": "",
        }
    )
    service = _OracleRuntimeService(adapter)
    service._catalog = SchemaCatalog(refreshed_at="2026-07-11T09:30:00+00:00", tables=[])
    csv_text = "ID,NAME\n123,456\n666,777\n"

    result = service.upload_db_admin_csv(
        DbAdminCsvUploadRequest(
            table_name="TEST_TABLE",
            content_base64=base64.b64encode(csv_text.encode()).decode(),
            filename="Book2.csv",
            confirmation="TEST_TABLE",
        )
    )

    assert result.executed is True
    assert result.matched_columns == ["ID", "NAME"]
    assert result.unmatched_csv_columns == []
    assert result.success_count == 2
    assert result.error_count == 0
    assert adapter.calls[0]["table_name"] == "APP.TEST_TABLE"
    assert adapter.calls[0]["owner"] == "APP"
    assert [column.column_name for column in adapter.calls[0]["columns"]] == ["ID", "NAME"]


def test_upload_csv_oracle_keeps_actual_unmatched_file_columns() -> None:
    """Oracle 実行結果が返した追加ファイル列だけを不一致として表示用 response に残す。"""

    adapter = _FakeCsvUploadAdapter(
        {
            "matched_columns": ["ID", "NAME"],
            "unmatched_csv_columns": ["UNKNOWN_COLUMN"],
            "success_count": 1,
            "error_count": 0,
            "row_errors": [],
            "hint": "",
        }
    )
    service = _OracleRuntimeService(adapter)
    service._catalog = SchemaCatalog(refreshed_at="2026-07-11T09:30:00+00:00", tables=[])
    csv_text = "ID,NAME,UNKNOWN_COLUMN\n123,456,extra\n"

    result = service.upload_db_admin_csv(
        DbAdminCsvUploadRequest(
            table_name="TEST_TABLE",
            content_base64=base64.b64encode(csv_text.encode()).decode(),
            filename="Book2.csv",
            confirmation="TEST_TABLE",
        )
    )

    assert result.executed is True
    assert result.matched_columns == ["ID", "NAME"]
    assert result.unmatched_csv_columns == ["UNKNOWN_COLUMN"]
    assert result.sample_rows == [{"ID": "123", "NAME": "456", "UNKNOWN_COLUMN": "extra"}]


def test_upload_csv_accepts_cr_newlines_and_preserves_quoted_cr() -> None:
    """CR 単独改行を行区切りにし、quote 内の CR は cell 値として保持する。"""
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    import base64

    csv_text = 'ORDER_ID,ORDER_NAME,NOTE\r1,架空商事,"第1行\r第2行"\r2,見本物産,通常行\r'
    result = service.upload_db_admin_csv(
        DbAdminCsvUploadRequest(
            table_name="ORDERS",
            content_base64=base64.b64encode(csv_text.encode()).decode(),
            filename="cr-only.csv",
        )
    )

    assert result.row_count == 2
    assert result.sample_rows[0]["ORDER_NAME"] == "架空商事"
    assert result.sample_rows[0]["NOTE"] == "第1行\r第2行"
    assert result.sample_rows[1]["ORDER_NAME"] == "見本物産"


def test_upload_csv_decodes_cp932_and_reports_encoding_warning() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    content = "商品名,単価\n鉛筆,100\n".encode("cp932")

    text, sheet_name, warnings = service._tabular_content_to_csv_text(  # noqa: SLF001
        filename="products.csv",
        content=content,
    )

    assert sheet_name == ""
    assert "商品名,単価" in text
    assert "鉛筆" in text
    assert any("CP932" in warning for warning in warnings)


def test_upload_csv_forces_doublequote_escaping_after_delimiter_sniff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BadDialect(csv.excel):
        delimiter = ","
        quotechar = "'"
        doublequote = False
        skipinitialspace = False

    service = Nl2SqlService(store=MemoryNl2SqlStore())

    def fake_sniff(
        _self: csv.Sniffer,
        _sample: str,
        delimiters: str | None = None,
    ) -> type[csv.Dialect]:
        del delimiters
        return _BadDialect

    monkeypatch.setattr(csv.Sniffer, "sniff", fake_sniff)

    columns, rows, warnings = service._parse_csv_sample(  # noqa: SLF001
        table_name="ORDERS",
        csv_text='ID,NOTE\n1,"He said ""hi"", ok"\n',
        max_rows=10,
        max_columns=10,
    )

    assert warnings == []
    assert [column.column_name for column in columns] == ["ID", "NOTE"]
    assert rows == [{"ID": "1", "NOTE": 'He said "hi", ok'}]


def test_upload_csv_allows_long_text_for_existing_table_upload() -> None:
    long_text = "あ" * 4001
    adapter = _FakeCsvUploadAdapter(
        {
            "matched_columns": ["NOTE"],
            "unmatched_csv_columns": [],
            "success_count": 1,
            "error_count": 0,
            "row_errors": [],
            "hint": "",
        }
    )
    service = _OracleRuntimeService(adapter)

    result = service.upload_db_admin_csv(
        DbAdminCsvUploadRequest(
            table_name="TEST_TABLE",
            content_base64=base64.b64encode(f"NOTE\n{long_text}\n".encode()).decode(),
            filename="long.csv",
            confirmation="TEST_TABLE",
        )
    )

    assert result.executed is True
    assert adapter.calls[0]["columns"][0].data_type == "CLOB"
    assert adapter.calls[0]["rows"] == [{"NOTE": long_text}]


def test_import_tabular_append_allows_long_text_for_existing_clob() -> None:
    class _ImportAdapter:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def import_tabular_table(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            return {}

    long_text = "あ" * 4001
    adapter = _ImportAdapter()
    service = _OracleRuntimeService(adapter)

    result = service.import_db_admin_tabular(
        DbAdminImportTabularRequest(
            table_name="TEST_TABLE",
            content_base64=base64.b64encode(f"NOTE\n{long_text}\n".encode()).decode(),
            filename="long.csv",
            mode="append",
            confirmation="ADMIN_EXECUTE",
        )
    )

    assert result.executed is True
    assert adapter.calls[0]["columns"][0].data_type == "CLOB"
    assert adapter.calls[0]["rows"] == [{"NOTE": long_text}]


def test_import_tabular_replace_requires_target_confirmation() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    result = service.import_db_admin_tabular(
        DbAdminImportTabularRequest(
            table_name="IMPORTED_ORDERS",
            content_base64=base64.b64encode(b"ID\n1\n").decode(),
            filename="orders.csv",
            mode="replace",
            confirmation="ADMIN_EXECUTE",
        )
    )

    assert result.executed is False
    assert any("confirmation=IMPORTED_ORDERS" in warning for warning in result.warnings)


def test_import_tabular_keeps_leading_zero_codes_as_text() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())

    columns, rows, warnings = service._parse_csv_sample(  # noqa: SLF001
        table_name="ORDERS",
        csv_text="CODE\n00123\n",
        max_rows=10,
        max_columns=10,
    )

    assert warnings == []
    assert columns[0].data_type == "VARCHAR2(255 CHAR)"
    assert rows == [{"CODE": "00123"}]


def test_import_tabular_dedupe_csv_column_names_avoids_suffix_collision() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())

    assert service._dedupe_csv_column_names(["A", "A", "A_2"]) == [  # noqa: SLF001
        "A",
        "A_2",
        "A_2_2",
    ]


def test_upload_csv_parse_error_returns_http_400() -> None:
    """csv.Error を API 境界で利用者向け 400 応答へ正規化する。"""
    import base64

    oversized_cell = "x" * (csv.field_size_limit() + 1)
    request = DbAdminCsvUploadRequest(
        table_name="ORDERS",
        content_base64=base64.b64encode(f"NOTE\n{oversized_cell}\n".encode()).decode(),
        filename="oversized.csv",
    )

    with pytest.raises(HTTPException) as exc_info:
        db_admin_upload_csv(request)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == (
        "CSV の解析に失敗しました。改行形式・引用符・セルの長さを確認してください。"
    )
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert isinstance(exc_info.value.__cause__.__cause__, csv.Error)


def test_upload_csv_validation_error_returns_http_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.features.nl2sql import router as nl2sql_router

    class _FailingService:
        def upload_db_admin_csv(self, _request: DbAdminCsvUploadRequest) -> None:
            raise TabularImportValidationError("CSV の文字エンコーディングを判定できません。")

    monkeypatch.setattr(nl2sql_router, "nl2sql_service", _FailingService())
    request = DbAdminCsvUploadRequest(
        table_name="ORDERS",
        content_base64="YQ==",
        confirmation="ORDERS",
    )

    with pytest.raises(HTTPException) as exc_info:
        db_admin_upload_csv(request)

    assert exc_info.value.status_code == 422
    assert "文字エンコーディング" in str(exc_info.value.detail)
    assert isinstance(exc_info.value.__cause__, TabularImportValidationError)


def test_import_tabular_execute_requires_admin_execute_confirmation() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    import base64

    content_base64 = base64.b64encode("ORDER_ID,ORDER_NAME\n1,架空商事\n".encode()).decode()
    table_confirmation = service.import_db_admin_tabular(
        DbAdminImportTabularRequest(
            table_name="IMPORTED_ORDERS",
            content_base64=content_base64,
            filename="orders.csv",
            confirmation="IMPORTED_ORDERS",
        )
    )
    assert table_confirmation.executed is False
    assert any("confirmation=ADMIN_EXECUTE" in warning for warning in table_confirmation.warnings)

    admin_confirmation = service.import_db_admin_tabular(
        DbAdminImportTabularRequest(
            table_name="IMPORTED_ORDERS",
            content_base64=content_base64,
            filename="orders.csv",
            confirmation="ADMIN_EXECUTE",
        )
    )
    assert admin_confirmation.executed is False
    assert any("NL2SQL_RUNTIME_MODE=oracle" in warning for warning in admin_confirmation.warnings)


def test_import_tabular_create_submits_targeted_schema_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ImportAdapter:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def import_tabular_table(self, **kwargs: Any) -> None:
            self.calls.append(kwargs)

    adapter = ImportAdapter()
    service = _OracleRuntimeService(adapter)
    submitted_targets: list[list[tuple[str, str, str]]] = []

    def submit(**kwargs: Any) -> SchemaRefreshMutationSync:
        submitted_targets.append(
            [
                (target.owner, target.object_name, target.expected_state)
                for target in kwargs["target_objects"]
            ]
        )
        return SchemaRefreshMutationSync(job_id="targeted-import-refresh")

    monkeypatch.setattr(service, "_submit_schema_refresh_after_admin_mutation", submit)

    content_base64 = base64.b64encode("ORDER_ID,ORDER_NAME\n1,架空商事\n".encode()).decode()
    result = service.import_db_admin_tabular(
        DbAdminImportTabularRequest(
            table_name="IMPORTED_ORDERS",
            content_base64=content_base64,
            filename="orders.csv",
            confirmation="ADMIN_EXECUTE",
        )
    )

    assert result.executed is True
    assert result.schema_refresh_job_id == "targeted-import-refresh"
    assert result.schema_refresh_required is False
    assert submitted_targets == [[("APP", "IMPORTED_ORDERS", "present")]]
    assert adapter.calls[0]["table_name"] == "IMPORTED_ORDERS"


def test_import_tabular_infers_explicit_char_semantics_for_japanese() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())

    columns, rows, warnings = service._parse_csv_sample(
        table_name="TEST_TABLE",
        csv_text="ID,NAME\n1,株式会社架空\n",
        max_rows=100,
        max_columns=10,
    )

    assert warnings == []
    assert rows == [{"ID": "1", "NAME": "株式会社架空"}]
    assert [column.data_type for column in columns] == ["NUMBER", "VARCHAR2(255 CHAR)"]
    assert service._csv_import_ddl("TEST_TABLE", columns) == (
        'CREATE TABLE "TEST_TABLE" ("ID" NUMBER, "NAME" VARCHAR2(255 CHAR))'
    )


def test_import_tabular_rejects_text_over_varchar2_char_limit() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())

    with pytest.raises(ValueError, match="4000 文字を超えるセル"):
        service._infer_csv_data_type(["あ" * 4001])


def test_import_tabular_rejects_oversized_existing_byte_column_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Cursor:
        def __init__(self) -> None:
            self.executed: list[tuple[str, dict[str, object]]] = []
            self.executemany_called = False
            self._metadata = [
                ("ID", "NUMBER", 22, 0, None),
                ("NAME", "VARCHAR2", 6, 6, "B"),
            ]
            self._violation: tuple[object, ...] | None = None

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, sql: str, binds: dict[str, object]) -> None:
            self.executed.append((sql, binds))
            if "JSON_TABLE" in sql:
                assert "c0 VARCHAR2(4000 BYTE) TRUNCATE" in sql
                assert "LENGTHB(c0)" in sql
                self._violation = (2, "NAME", 16, 6, "バイト")

        def fetchall(self) -> list[tuple[object, ...]]:
            return self._metadata  # type: ignore[return-value]

        def fetchone(self) -> tuple[object, ...] | None:
            return self._violation

        def setinputsizes(self, **_kwargs: object) -> None:
            return None

        def executemany(
            self,
            _sql: str,
            _rows: list[dict[str, object]],
            *,
            batcherrors: bool,
        ) -> None:
            assert batcherrors is True
            self.executemany_called = True

        def getbatcherrors(self) -> list[Exception]:
            return []

    class _Connection:
        def __init__(self, cursor: _Cursor) -> None:
            self._cursor = cursor
            self.committed = False

        def cursor(self) -> _Cursor:
            return self._cursor

        def commit(self) -> None:
            self.committed = True

    cursor = _Cursor()
    connection = _Connection(cursor)

    @contextmanager
    def fake_connection() -> Iterator[_Connection]:
        yield connection

    adapter = OracleNl2SqlAdapter(get_settings())
    monkeypatch.setattr(adapter, "connection", fake_connection)
    import_columns = [
        CsvImportColumn(
            source_name="ID",
            column_name="ID",
            data_type="NUMBER",
            nullable=False,
        ),
        CsvImportColumn(
            source_name="NAME",
            column_name="NAME",
            data_type="VARCHAR2(6 CHAR)",
            nullable=False,
        ),
    ]

    with pytest.raises(TabularImportValidationError, match="16バイト.*上限6バイト"):
        adapter.import_tabular_table(
            table_name="TEST_TABLE",
            columns=import_columns,
            rows=[{"ID": "1", "NAME": "株式会社架空"}],
            mode="append",
        )

    assert cursor.executemany_called is False
    assert connection.committed is False
    assert not any(
        "DELETE FROM" in sql or "TRUNCATE TABLE" in sql for sql, _binds in cursor.executed
    )


def test_import_tabular_truncate_mode_uses_transactional_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Cursor:
        def __init__(self) -> None:
            self.executed: list[str] = []
            self.batch_rows: list[dict[str, object]] = []
            self._metadata = [
                ("ID", "NUMBER", 22, 0, None),
                ("NAME", "VARCHAR2", 20, 20, "C"),
            ]

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, sql: str, _binds: dict[str, object]) -> None:
            self.executed.append(sql)
            if "JSON_TABLE" in sql:
                assert "c0 CLOB" in sql
                assert "LENGTH(c0)" in sql
                assert "LENGTHB(c0)" not in sql

        def fetchall(self) -> list[tuple[object, ...]]:
            return self._metadata  # type: ignore[return-value]

        def fetchone(self) -> None:
            return None

        def setinputsizes(self, **_kwargs: object) -> None:
            return None

        def executemany(
            self,
            _sql: str,
            rows: list[dict[str, object]],
            *,
            batcherrors: bool,
        ) -> None:
            assert batcherrors is True
            self.batch_rows = rows

        def getbatcherrors(self) -> list[Exception]:
            return []

    class _Connection:
        def __init__(self, cursor: _Cursor) -> None:
            self._cursor = cursor
            self.committed = False

        def cursor(self) -> _Cursor:
            return self._cursor

        def commit(self) -> None:
            self.committed = True

    cursor = _Cursor()
    connection = _Connection(cursor)

    @contextmanager
    def fake_connection() -> Iterator[_Connection]:
        yield connection

    adapter = OracleNl2SqlAdapter(get_settings())
    monkeypatch.setattr(adapter, "connection", fake_connection)
    result = adapter.import_tabular_table(
        table_name="TEST_TABLE",
        columns=[
            CsvImportColumn(
                source_name="ID",
                column_name="ID",
                data_type="NUMBER",
                nullable=False,
            ),
            CsvImportColumn(
                source_name="NAME",
                column_name="NAME",
                data_type="VARCHAR2(6 CHAR)",
                nullable=False,
            ),
        ],
        rows=[{"ID": "1", "NAME": "株式会社架空"}],
        mode="truncate",
    )

    assert result["row_count"] == 1
    assert any('DELETE FROM "TEST_TABLE"' in sql for sql in cursor.executed)
    assert not any("TRUNCATE TABLE" in sql for sql in cursor.executed)
    assert cursor.batch_rows == [{"c0": 1, "c1": "株式会社架空"}]
    assert connection.committed is True


def test_import_tabular_create_drops_new_table_when_insert_batch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BatchError(Exception):
        offset = 0

        def __str__(self) -> str:
            return (
                'ORA-12899: value too large for column "APP"."TEST_TABLE"."NAME" '
                "(actual: 16, maximum: 6)"
            )

    class _Cursor:
        def __init__(self) -> None:
            self.executed: list[str] = []

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, sql: str, _binds: dict[str, object]) -> None:
            self.executed.append(sql)

        def executemany(
            self,
            _sql: str,
            _rows: list[dict[str, object]],
            *,
            batcherrors: bool,
        ) -> None:
            assert batcherrors is True

        def getbatcherrors(self) -> list[Exception]:
            return [_BatchError()]

    class _Connection:
        def __init__(self, cursor: _Cursor) -> None:
            self._cursor = cursor
            self.committed = False
            self.rolled_back = False

        def cursor(self) -> _Cursor:
            return self._cursor

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            self.rolled_back = True

    cursor = _Cursor()
    connection = _Connection(cursor)

    @contextmanager
    def fake_connection() -> Iterator[_Connection]:
        yield connection

    adapter = OracleNl2SqlAdapter(get_settings())
    monkeypatch.setattr(adapter, "connection", fake_connection)

    with pytest.raises(TabularImportValidationError, match="ファイル2行目"):
        adapter.import_tabular_table(
            table_name="TEST_TABLE",
            columns=[
                CsvImportColumn(
                    source_name="NAME",
                    column_name="NAME",
                    data_type="VARCHAR2(6 CHAR)",
                    nullable=False,
                )
            ],
            rows=[{"NAME": "株式会社架空"}],
            mode="create",
        )

    assert any('CREATE TABLE "TEST_TABLE"' in sql for sql in cursor.executed)
    assert any('DROP TABLE "TEST_TABLE" PURGE' in sql for sql in cursor.executed)
    assert connection.rolled_back is True
    assert connection.committed is False


def test_upload_csv_truncate_insert_uses_delete_and_rolls_back_on_insert_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CsvRow(dict[str, str | None]):
        def __init__(self, value: str, *, file_row_number: int) -> None:
            super().__init__({"ID": value})
            self.file_row_number = file_row_number

    class _Cursor:
        def __init__(self) -> None:
            self.executed: list[str] = []
            self._metadata = [("ID", "VARCHAR2")]

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, sql: str, binds: dict[str, object]) -> None:
            self.executed.append(sql)
            if sql.strip().upper().startswith("INSERT") and binds.get("c0") == "bad":
                raise Exception("ORA-00001: unique constraint violated")

        def fetchall(self) -> list[tuple[object, ...]]:
            return self._metadata  # type: ignore[return-value]

    class _Connection:
        def __init__(self, cursor: _Cursor) -> None:
            self._cursor = cursor
            self.committed = False
            self.rolled_back = False

        def cursor(self) -> _Cursor:
            return self._cursor

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            self.rolled_back = True

    cursor = _Cursor()
    connection = _Connection(cursor)

    @contextmanager
    def fake_connection() -> Iterator[_Connection]:
        yield connection

    adapter = OracleNl2SqlAdapter(get_settings())
    monkeypatch.setattr(adapter, "connection", fake_connection)

    result = adapter.upload_csv_to_existing_table(
        table_name="TEST_TABLE",
        columns=[
            CsvImportColumn(
                source_name="ID",
                column_name="ID",
                data_type="VARCHAR2(10 CHAR)",
                nullable=False,
            )
        ],
        rows=[_CsvRow("ok", file_row_number=2), _CsvRow("bad", file_row_number=5)],
        truncate=True,
    )

    assert any('DELETE FROM "APP"."TEST_TABLE"' in sql for sql in cursor.executed)
    assert not any("TRUNCATE TABLE" in sql for sql in cursor.executed)
    assert result["success_count"] == 0
    assert result["error_count"] == 2
    assert "既存データは保持されています" in result["row_errors"][0]
    assert any("行5" in row_error for row_error in result["row_errors"])
    assert connection.rolled_back is True
    assert connection.committed is False


def test_import_tabular_validation_error_returns_http_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.features.nl2sql import router as nl2sql_router

    class _FailingService:
        def import_db_admin_tabular(self, _request: DbAdminImportTabularRequest) -> None:
            raise TabularImportValidationError(
                "TEST_TABLE.NAME: ファイル2行目は16バイトで、"
                "取込先列の上限6バイトを超えています。"
                "値を短くするか列定義を拡張して再試行してください。"
            )

    monkeypatch.setattr(nl2sql_router, "nl2sql_service", _FailingService())
    request = DbAdminImportTabularRequest(
        table_name="TEST_TABLE",
        content_base64="YQ==",
        confirmation="ADMIN_EXECUTE",
    )

    with pytest.raises(HTTPException) as exc_info:
        db_admin_import_tabular(request)

    assert exc_info.value.status_code == 422
    assert "16バイト" in str(exc_info.value.detail)
    assert isinstance(exc_info.value.__cause__, TabularImportValidationError)


@pytest.mark.parametrize(
    ("message", "code", "expected"),
    [
        ("ORA-01031: insufficient privileges", "ORA-01031", "権限が不足"),
        ("ORA-01722: invalid number", "ORA-01722", "数値形式"),
        ("ORA-00001: unique constraint violated", "ORA-00001", "一意制約"),
        ("ORA-00054: resource busy", "ORA-00054", "使用中"),
    ],
)
def test_db_admin_error_maps_oracle_codes_to_actionable_japanese(
    message: str, code: str, expected: str
) -> None:
    error = _db_admin_error(OracleAdapterError(message), target_name="TEST_TABLE")
    assert error.error_code == code
    assert expected in error.summary
    assert error.actions


def test_db_admin_duplicate_error_identifies_target_and_safe_recovery() -> None:
    error = _db_admin_error(
        OracleAdapterError("ORA-00955: name is already used by an existing object"),
        target_name="TEST_TABLE",
        target_type="TABLE",
        operation="tabular_import",
    )
    assert isinstance(error, DbAdminOperationFailed)
    assert error.error_code == "ORA-00955"
    assert "TEST_TABLE" in error.summary
    assert "既存のTABLE" in error.cause
    assert all("削除" not in action for action in error.actions)


def test_db_admin_unknown_oracle_error_keeps_code_and_safe_fallback() -> None:
    error = _db_admin_error(OracleAdapterError("ORA-29999: unexpected"))
    assert error.error_code == "ORA-29999"
    assert "ORA-29999" in error.summary
    assert "SQL" in error.actions[0]


def test_flexible_date_value_parses_common_formats() -> None:
    assert _flexible_date_value("2026-01-31") == datetime(2026, 1, 31)
    assert _flexible_date_value("2026/01/31") == datetime(2026, 1, 31)
    assert _flexible_date_value("20260131") == datetime(2026, 1, 31)
    assert _flexible_date_value("2026-01-31 12:34:56") == datetime(2026, 1, 31, 12, 34, 56)
    assert _flexible_date_value("2026-01-31T12:34:56") == datetime(2026, 1, 31, 12, 34, 56)
    assert _flexible_date_value("2026-01-31 12:34:56.789") == datetime(2026, 1, 31, 12, 34, 56)
    assert _flexible_date_value("2026年01月31日") == datetime(2026, 1, 31)
    # Excel シリアル日付(1899-12-30 起点): 45658 = 2025-01-01
    assert _flexible_date_value("45658") == datetime(2025, 1, 1)
    assert _flexible_date_value("not-a-date") is None
    assert _flexible_date_value("") is None


class _FakeLob:
    """oracledb LOB を模した read() 対応の値。"""

    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


def test_analyze_error_uses_enterprise_ai_and_falls_back() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    cast(Any, service)._enterprise_ai_client = FakeEnterpriseAiClient(
        "1) エラー原因: 表が既に存在します。\n2) 解決方法: DROP してください。\n3) 結論: 再実行。"
    )
    analyzed = service.analyze_db_admin_failure(
        DbAdminAiAnalysisRequest(
            sql="CREATE TABLE T1 (ID NUMBER)",
            result_text="ORA-00955: name is already used by an existing object",
        )
    )
    assert analyzed.source == "enterprise_ai"
    assert "エラー原因" in analyzed.analysis

    cast(Any, service)._enterprise_ai_client = FakeEnterpriseAiClient(
        EnterpriseAiDirectError("boom")
    )
    fallback = service.analyze_db_admin_failure(
        DbAdminAiAnalysisRequest(
            sql="CREATE TABLE T1 (ID NUMBER)",
            result_text="ORA-00955: name is already used by an existing object",
        )
    )
    assert fallback.source == "deterministic"
    assert "ORA-00955" in fallback.analysis
    assert fallback.warnings

    cast(Any, service)._enterprise_ai_client = FakeEnterpriseAiClient(configured=False)
    unconfigured = service.analyze_db_admin_failure(
        DbAdminAiAnalysisRequest(sql="INSERT INTO T1 VALUES (1)", result_text="成功", target="data")
    )
    assert unconfigured.source == "deterministic"
    assert unconfigured.warnings


def test_extract_join_where_defaults_to_sql_structure_and_falls_back() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    fake_client = FakeEnterpriseAiClient(
        "## SQL構造分析\n\n"
        "### JOIN句\n"
        "- **JOIN**: EMPLOYEE(e) JOIN DEPARTMENT(d)\n"
        "  - ON: EMPLOYEE(e).DEPT_ID = DEPARTMENT(d).DEPT_ID\n\n"
        "### WHERE句\n"
        "- EMPLOYEE(e).STATUS = 'A'\n"
    )
    cast(Any, service)._enterprise_ai_client = fake_client
    ddl = (
        'CREATE OR REPLACE VIEW "V_EMP_DEPT" AS\n'
        "SELECT e.EMP_ID, d.DEPT_NAME FROM EMPLOYEE e "
        "JOIN DEPARTMENT d ON e.DEPT_ID = d.DEPT_ID WHERE e.STATUS = 'A'"
    )
    extracted = service.extract_db_admin_join_where(DbAdminJoinWhereRequest(ddl=ddl))
    assert extracted.source == "enterprise_ai"
    assert extracted.prompt_profile == "sql_structure"
    assert "EMPLOYEE" in extracted.join_text
    assert extracted.where_text == "EMPLOYEE(e).STATUS = 'A'"
    assert "SQL構造分析" in extracted.structure_markdown
    assert "Analyze the SQL query" in fake_client.calls[0]["prompt"]

    cast(Any, service)._enterprise_ai_client = FakeEnterpriseAiClient("整形されていない応答")
    fallback = service.extract_db_admin_join_where(DbAdminJoinWhereRequest(ddl=ddl))
    assert fallback.source == "deterministic"
    assert fallback.prompt_profile == "sql_structure"
    assert fallback.warnings
    assert "JOIN" in fallback.join_text.upper() or fallback.join_text == "None"
    assert fallback.where_text != ""


def test_extract_join_where_limits_view_ddl_to_first_statement_and_uses_semantic_joins() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    cast(Any, service)._enterprise_ai_client = FakeEnterpriseAiClient(configured=False)
    ddl = (
        "CREATE OR REPLACE VIEW V_ORDER_CUSTOMER AS "
        "SELECT o.ORDER_ID, c.CUSTOMER_NAME FROM ORDERS o "
        "JOIN CUSTOMERS c ON c.CUSTOMER_ID = o.CUSTOMER_ID "
        "WHERE c.STATUS = 'ACTIVE';\n"
        "COMMENT ON VIEW V_ORDER_CUSTOMER IS '受注明細';\n"
        "COMMENT ON COLUMN V_ORDER_CUSTOMER.CUSTOMER_NAME IS '顧客名';"
    )

    extracted = service.extract_db_admin_join_where(DbAdminJoinWhereRequest(ddl=ddl))

    assert extracted.source == "deterministic"
    assert "CUSTOMERS" in extracted.join_text.upper()
    assert " ON " in extracted.join_text.upper()
    assert "C.CUSTOMER_ID = O.CUSTOMER_ID" in extracted.join_text.upper()
    assert "C.STATUS = 'ACTIVE'" in extracted.where_text.upper()
    assert "COMMENT ON" not in extracted.where_text.upper()


def test_extract_join_where_uses_sql_structure_prompt_profile() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    fake_client = FakeEnterpriseAiClient(
        "## SQL構造分析\n\n"
        "### SELECT句\n"
        "- EMPLOYEE(e).EMP_ID\n\n"
        "### JOIN句\n"
        "- **JOIN**: EMPLOYEE(e) JOIN DEPARTMENT(d)\n"
        "  - ON: EMPLOYEE(e).DEPT_ID = DEPARTMENT(d).DEPT_ID\n\n"
        "### WHERE句\n"
        "- EMPLOYEE(e).STATUS = 'A'\n"
    )
    cast(Any, service)._enterprise_ai_client = fake_client
    ddl = (
        "CREATE OR REPLACE VIEW V_EMP_DEPT AS "
        "SELECT e.EMP_ID, d.DEPT_NAME FROM EMPLOYEE e "
        "JOIN DEPARTMENT d ON e.DEPT_ID = d.DEPT_ID WHERE e.STATUS = 'A'"
    )

    extracted = service.extract_db_admin_join_where(
        DbAdminJoinWhereRequest(ddl=ddl, prompt_profile="sql_structure")
    )

    assert extracted.source == "enterprise_ai"
    assert extracted.prompt_profile == "sql_structure"
    assert "EMPLOYEE(e) JOIN DEPARTMENT(d)" in extracted.join_text
    assert "ON: EMPLOYEE(e).DEPT_ID = DEPARTMENT(d).DEPT_ID" in extracted.join_text
    assert extracted.where_text == "EMPLOYEE(e).STATUS = 'A'"
    assert "SQL構造分析" in extracted.structure_markdown
    assert "Analyze the SQL query" in fake_client.calls[0]["prompt"]


def test_extract_join_where_rejects_legacy_strict_prompt_profile() -> None:
    with pytest.raises(ValidationError):
        DbAdminJoinWhereRequest(
            ddl="CREATE OR REPLACE VIEW V1 AS SELECT * FROM EMPLOYEE",
            prompt_profile="join_where_strict",
        )


def test_extract_join_where_unconfigured_keeps_selected_prompt_profile() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    cast(Any, service)._enterprise_ai_client = FakeEnterpriseAiClient(configured=False)
    result = service.extract_db_admin_join_where(
        DbAdminJoinWhereRequest(
            ddl="CREATE OR REPLACE VIEW V1 AS SELECT * FROM EMPLOYEE",
            prompt_profile="sql_structure",
        )
    )

    assert result.source == "deterministic"
    assert result.prompt_profile == "sql_structure"
    assert result.warnings


def test_named_target_confirmation_rejects_admin_execute_master_word() -> None:
    """対象名確認を要求する操作は ADMIN_EXECUTE では迂回できない。"""
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    _import_sample(service)
    table = service._catalog.tables[0]

    dropped = service.drop_db_admin_table(
        DbAdminDropTableRequest(table_name=table.table_name, confirmation="ADMIN_EXECUTE")
    )
    assert dropped.executed is False
    assert dropped.statements[0].status == "confirmation_required"
    assert any("ADMIN_EXECUTE では代替できません" in warning for warning in dropped.warnings)

    view_dropped = service.drop_db_admin_view(
        DbAdminDropViewRequest(view_name="V_EMP_DEPT", confirmation="ADMIN_EXECUTE")
    )
    assert view_dropped.executed is False
    assert view_dropped.statements[0].status == "confirmation_required"

    import base64

    csv_text = f"{table.columns[0].column_name}\n1\n"
    uploaded = service.upload_db_admin_csv(
        DbAdminCsvUploadRequest(
            table_name=table.table_name,
            content_base64=base64.b64encode(csv_text.encode()).decode(),
            confirmation="ADMIN_EXECUTE",
        )
    )
    assert uploaded.executed is False
    assert any("ADMIN_EXECUTE では代替できません" in warning for warning in uploaded.warnings)


_DOMAIN_STRUCTURE_TEXT = (
    "OBJECT: APP.SALES\nTYPE: table\nCOMMENT: 売上\n"
    "COLUMNS:\n"
    "- SALES_ID: NUMBER NULLABLE=N COMMENT=売上ID\n"
    "- CUSTOMER_ID: NUMBER NULLABLE=N COMMENT=顧客ID\n"
    "- STATUS: VARCHAR2(10) NULLABLE=Y COMMENT=売上状態\n"
    "- REGION: VARCHAR2(20 CHAR) NULLABLE=Y COMMENT=O'Brien 地域\n\n"
    "OBJECT: APP.CUSTOMERS\nTYPE: table\nCOMMENT: 顧客\n"
    "COLUMNS:\n"
    "- CUSTOMER_ID: NUMBER NULLABLE=N COMMENT=顧客ID\n"
    "- STATUS: VARCHAR2(20) NULLABLE=Y COMMENT=顧客状態\n"
    "- REGION: VARCHAR2(20 CHAR) NULLABLE=Y COMMENT=地域\n\n"
    "OBJECT: APP.SALES_V\nTYPE: view\nCOMMENT: 売上ビュー\n"
    "COLUMNS:\n"
    "- CUSTOMER_ID: NUMBER NULLABLE=Y COMMENT=顧客ID\n"
)


def _domain_request(**overrides: Any) -> MetadataSqlGenerateRequest:
    payload: dict[str, Any] = {
        "targets": [
            {"object_name": "SALES", "object_type": "table"},
            {"object_name": "CUSTOMERS", "object_type": "table"},
            {"object_name": "SALES_V", "object_type": "view"},
        ],
        "structure_text": _DOMAIN_STRUCTURE_TEXT,
    }
    payload.update(overrides)
    return MetadataSqlGenerateRequest.model_validate(payload)


def test_statement_policy_domain_sql() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    allowed = service.execute_db_admin_statements(
        DbAdminStatementsRequest(
            sql=(
                "CREATE DOMAIN IF NOT EXISTS APP.CUSTOMER_ID_DOM AS NUMBER "
                "ANNOTATIONS (UI_Display '顧客ID');\n"
                "CREATE DOMAIN ORDER_STATUS_DOM AS VARCHAR2(10) "
                "CONSTRAINT CHECK (ORDER_STATUS_DOM IN ('NEW', 'CLOSED'));\n"
                "ALTER TABLE APP.SALES MODIFY (CUSTOMER_ID DOMAIN APP.CUSTOMER_ID_DOM);\n"
                "ALTER TABLE SALES MODIFY CUSTOMER_ID DOMAIN CUSTOMER_ID_DOM;\n"
                "ALTER TABLE SALES MODIFY (CUSTOMER_ID) DROP DOMAIN PRESERVE CONSTRAINTS;\n"
                "ALTER TABLE ADDRESSES MODIFY (CITY, STATE, ZIP) ADD DOMAIN US_CITY;\n"
                "ALTER DOMAIN CUSTOMER_ID_DOM ANNOTATIONS (Description '顧客番号');\n"
                "DROP DOMAIN CUSTOMER_ID_DOM FORCE PRESERVE;\n"
                # 生成 SQL は引用識別子なので引用付きも通す(実 DB で検出、#673)。
                'ALTER TABLE "APP"."SALES" MODIFY ("CUSTOMER_ID") '
                'ADD DOMAIN "APP"."CUSTOMER_ID_D";\n'
                'ALTER TABLE "APP"."SALES" MODIFY ("CUSTOMER_ID") DROP DOMAIN;\n'
                'DROP DOMAIN "APP"."CUSTOMER_ID_D" FORCE'
            ),
            policy="domain_sql",
        )
    )
    assert [item.status for item in allowed.statements] == ["confirmation_required"] * 11

    for sql in (
        "ALTER TABLE SALES MODIFY (STATUS VARCHAR2(40))",
        # リテラル内の domain では通さない(列型変更の抜け道を塞ぐ)。
        "ALTER TABLE SALES MODIFY (STATUS VARCHAR2(40) DEFAULT 'domain')",
        'ALTER TABLE "APP"."SALES" MODIFY ("STATUS" VARCHAR2(40) DEFAULT \'domain\')',
        'ALTER TABLE "APP"."SALES" MODIFY ("DOMAIN" VARCHAR2(40))',
        "ALTER TABLE SALES ADD (NOTE VARCHAR2(10) DOMAIN NOTE_DOM)",
        "ALTER TABLE SALES DROP COLUMN STATUS",
        # DOMAIN の語があっても、同じ文の列の型の変更・列の削除は通さない（#940）。
        "ALTER TABLE SALES MODIFY (STATUS VARCHAR2(1) DOMAIN STATUS_DOM)",
        "ALTER TABLE SALES MODIFY (CUSTOMER_ID DOMAIN CUSTOMER_ID_DOM) DROP COLUMN STATUS",
        "ALTER TABLE SALES MODIFY (CUSTOMER_ID) ADD DOMAIN CUSTOMER_ID_DOM DROP COLUMN STATUS",
        "ALTER TABLE SALES MODIFY (CUSTOMER_ID) DROP DOMAIN DROP COLUMN STATUS",
        "ALTER TABLE SALES DROP COLUMN STATUS MODIFY (CUSTOMER_ID) ADD DOMAIN CUSTOMER_ID_DOM",
        "ALTER TABLE SALES MODIFY CUSTOMER_ID NUMBER DOMAIN CUSTOMER_ID_DOM",
        "CREATE TABLE SALES (ID NUMBER)",
        "DROP TABLE SALES",
    ):
        blocked = service.execute_db_admin_statements(
            DbAdminStatementsRequest(sql=sql, policy="domain_sql")
        )
        assert blocked.statements[0].status == "blocked", sql
        assert blocked.statements[0].error_code == "DB_ADMIN_DOMAIN_SQL_POLICY_VIOLATION", sql
        assert "CREATE DOMAIN" in blocked.statements[0].error_message


def test_domain_ddl_skips_schema_refresh_but_column_association_targets_table() -> None:
    assert not _statements_change_schema(
        [
            "CREATE DOMAIN X_DOM AS NUMBER",
            "ALTER DOMAIN X_DOM DROP DISPLAY",
            "DROP DOMAIN X_DOM FORCE",
        ]
    )
    statements = [
        "CREATE DOMAIN IF NOT EXISTS APP.X_DOM AS NUMBER",
        "ALTER TABLE APP.SALES MODIFY (X DOMAIN APP.X_DOM)",
    ]
    assert _statements_change_schema(statements)
    targets = _schema_refresh_targets_for_statements(statements, current_owner="APP")
    assert targets is not None
    assert [(target.owner, target.object_name) for target in targets] == [("APP", "SALES")]


def test_deterministic_domain_sql_groups_shared_columns_across_tables() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._enterprise_ai_client = FakeEnterpriseAiClient(configured=False)

    result = service.generate_domain_sql(_domain_request())

    assert result.source == "deterministic"
    assert result.sql.splitlines() == [
        'CREATE DOMAIN IF NOT EXISTS "APP"."CUSTOMER_ID_D" AS NUMBER '
        "ANNOTATIONS (\"DESCRIPTION\" '顧客ID');",
        'ALTER TABLE "APP"."CUSTOMERS" MODIFY ("CUSTOMER_ID") ADD DOMAIN "APP"."CUSTOMER_ID_D";',
        'ALTER TABLE "APP"."SALES" MODIFY ("CUSTOMER_ID") ADD DOMAIN "APP"."CUSTOMER_ID_D";',
        'CREATE DOMAIN IF NOT EXISTS "APP"."REGION_D" AS VARCHAR2(20 CHAR) '
        "ANNOTATIONS (\"DESCRIPTION\" 'O''Brien 地域');",
        'ALTER TABLE "APP"."CUSTOMERS" MODIFY ("REGION") ADD DOMAIN "APP"."REGION_D";',
        'ALTER TABLE "APP"."SALES" MODIFY ("REGION") ADD DOMAIN "APP"."REGION_D";',
    ]
    # 型が違う同名列は共有せず、単一表だけの列とビュー列は候補にしない。
    assert "STATUS" not in result.sql
    assert "SALES_ID" not in result.sql
    assert "SALES_V" not in result.sql
    assert any("モデルの API が未設定" in warning for warning in result.warnings)

    single = service.generate_domain_sql(
        _domain_request(targets=[{"object_name": "SALES", "object_type": "table"}])
    )
    assert single.sql == ""
    assert any("複数テーブルで共通する列" in warning for warning in single.warnings)

    # 長さ無しの文字型(catalog の生の型)はドメイン定義に使えないため候補から外す。
    service._catalog = SchemaCatalog(
        refreshed_at="2026-09-22T00:00:00+00:00",
        tables=[
            SchemaTable(
                table_name="A_TABLE",
                logical_name="A",
                columns=[
                    SchemaColumn(column_name="CODE", logical_name="コード", data_type="VARCHAR2"),
                    SchemaColumn(column_name="QTY", logical_name="数量", data_type="NUMBER"),
                ],
            ),
            SchemaTable(
                table_name="B_TABLE",
                logical_name="B",
                columns=[
                    SchemaColumn(column_name="CODE", logical_name="コード", data_type="VARCHAR2"),
                    SchemaColumn(column_name="QTY", logical_name="数量", data_type="NUMBER"),
                ],
            ),
        ],
    )
    from_catalog = service.generate_domain_sql(
        _domain_request(
            targets=[
                {"object_name": "A_TABLE", "object_type": "table"},
                {"object_name": "B_TABLE", "object_type": "table"},
            ],
            structure_text="",
        )
    )
    assert 'CREATE DOMAIN IF NOT EXISTS "APP"."QTY_D" AS NUMBER' in from_catalog.sql
    assert "CODE_D" not in from_catalog.sql


def test_domain_generation_uses_enterprise_ai_and_falls_back_on_violations() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    request = _domain_request(sample_text="OBJECT: APP.SALES\nSTATUS: NEW, CLOSED")
    enterprise_ai = FakeEnterpriseAiClient(
        "```sql\n"
        "CREATE DOMAIN IF NOT EXISTS APP.CUSTOMER_ID_DOM AS NUMBER "
        "ANNOTATIONS (UI_Display '顧客ID', Description '顧客番号');\n"
        "ALTER TABLE APP.CUSTOMERS MODIFY (CUSTOMER_ID DOMAIN APP.CUSTOMER_ID_DOM);\n"
        "ALTER TABLE APP.SALES MODIFY (CUSTOMER_ID DOMAIN APP.CUSTOMER_ID_DOM)\n"
        "```"
    )
    service._enterprise_ai_client = enterprise_ai

    result = service.generate_domain_sql(request)

    assert result.source == "enterprise_ai"
    assert result.sql == (
        "CREATE DOMAIN IF NOT EXISTS APP.CUSTOMER_ID_DOM AS NUMBER "
        "ANNOTATIONS (UI_Display '顧客ID', Description '顧客番号');\n"
        "ALTER TABLE APP.CUSTOMERS MODIFY (CUSTOMER_ID DOMAIN APP.CUSTOMER_ID_DOM);\n"
        "ALTER TABLE APP.SALES MODIFY (CUSTOMER_ID DOMAIN APP.CUSTOMER_ID_DOM);"
    )
    assert "CHECK 制約を付け" in enterprise_ai.calls[0]["prompt"]
    assert "ALTER TABLE APP.ORD_TXN MODIFY (CUST_ID) ADD DOMAIN" in enterprise_ai.calls[0]["prompt"]
    assert "COLUMNS 以外の ALTER TABLE は出力しない" in enterprise_ai.calls[0]["system_prompt"]

    no_samples = FakeEnterpriseAiClient("CREATE DOMAIN APP.X_DOM AS NUMBER;")
    service._enterprise_ai_client = no_samples
    service.generate_domain_sql(_domain_request())
    assert "サンプルが無いため CHECK 制約は付けない" in no_samples.calls[0]["prompt"]

    service._enterprise_ai_client = FakeEnterpriseAiClient(
        "ALTER TABLE APP.SALES MODIFY (STATUS VARCHAR2(40));"
    )
    policy_fallback = service.generate_domain_sql(request)
    assert policy_fallback.source == "deterministic"
    assert 'CREATE DOMAIN IF NOT EXISTS "APP"."CUSTOMER_ID_D"' in policy_fallback.sql
    assert policy_fallback.fallback is True
    assert policy_fallback.fallback_reason

    service._enterprise_ai_client = FakeEnterpriseAiClient(
        "CREATE DOMAIN IF NOT EXISTS APP.CUSTOMER_ID_DOM AS NUMBER;\n"
        "ALTER TABLE APP.ORDERS MODIFY (CUSTOMER_ID DOMAIN APP.CUSTOMER_ID_DOM);"
    )
    outside = service.generate_domain_sql(request)
    assert outside.source == "deterministic"
    assert outside.fallback is True
    assert "選択した表以外" in outside.fallback_detail


_DOMAIN_INVENTORY: dict[str, Any] = {
    "owner": "APP",
    "name": "CUSTOMER_ID_D",
    "domain_type": "single",
    "data_type": "NUMBER(10)",
    "strict": True,
    "nullable": False,
    "constraints": ["VALUE > 0"],
    "display": "",
    "order": "",
    "annotations": [
        {"name": "DESCRIPTION", "value": "Unique identifier for a customer."},
        {"name": "ALIASES", "value": "customer id, 顧客ID"},
    ],
    "columns": [
        {"owner": "APP", "table_name": "CUSTOMERS", "column_name": "CUSTOMER_ID"},
        {"owner": "APP", "table_name": "ORDERS", "column_name": "CUSTOMER_ID"},
        {"owner": "APP", "table_name": "SALES", "column_name": "CUSTOMER_ID"},
    ],
}

_DOMAIN_STRUCTURE_WITH_DOMAIN = _DOMAIN_STRUCTURE_TEXT.replace(
    "- CUSTOMER_ID: NUMBER NULLABLE=N COMMENT=顧客ID",
    "- CUSTOMER_ID: NUMBER NULLABLE=N DOMAIN=APP.CUSTOMER_ID_D COMMENT=顧客ID",
)


class _FakeDomainInventoryAdapter:
    def __init__(self, result: tuple[list[dict[str, Any]], list[str]] | Exception) -> None:
        self.result = result
        self.calls: list[list[dict[str, Any]]] = []

    def fetch_domain_inventory(
        self, targets: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        self.calls.append(targets)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_metadata_input_objects_parse_domain_before_comment() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    objects = service._metadata_input_objects(
        MetadataSqlGenerateRequest(
            structure_text=(
                "OBJECT: APP.SALES\nTYPE: table\nCOMMENT: 売上\nCOLUMNS:\n"
                "- STAT_CD: CHAR(1) NULLABLE=N DOMAIN=APP.SALES_STATUS_D COMMENT=状態 DOMAIN=偽\n"
                "- AMT: NUMBER(12,2) NULLABLE=Y COMMENT=金額 DOMAIN=APP.NOT_A_DOMAIN"
            )
        )
    )
    assert objects[0]["columns"] == [
        {
            "name": "STAT_CD",
            "data_type": "CHAR(1)",
            "comment": "状態 DOMAIN=偽",
            "domain_name": "APP.SALES_STATUS_D",
        },
        {
            "name": "AMT",
            "data_type": "NUMBER(12,2)",
            "comment": "金額 DOMAIN=APP.NOT_A_DOMAIN",
            "domain_name": "",
        },
    ]


def test_domain_inventory_uses_oracle_adapter_and_degrades_without_oracle() -> None:
    adapter = _FakeDomainInventoryAdapter(([_DOMAIN_INVENTORY], ["dictionary warning"]))
    service = _OracleRuntimeService(adapter)
    request = DomainInventoryRequest(
        targets=[
            {"object_name": "SALES", "object_type": "table"},
            {"object_name": "SALES_V", "object_type": "view"},
        ]
    )

    inventory = service.get_domain_inventory(request)

    assert adapter.calls == [[{"owner": "", "object_name": "SALES", "object_type": "table"}]]
    assert inventory.runtime == "oracle"
    assert inventory.warnings == ["dictionary warning"]
    assert [domain.qualified_name for domain in inventory.domains] == ["APP.CUSTOMER_ID_D"]
    assert inventory.domain_text.splitlines() == [
        "DOMAIN: APP.CUSTOMER_ID_D",
        "TYPE: NUMBER(10) STRICT NOT NULL",
        "CHECK: VALUE > 0",
        "ANNOTATIONS: \"DESCRIPTION\"='Unique identifier for a customer.'; "
        "\"ALIASES\"='customer id, 顧客ID'",
        "COLUMNS: APP.CUSTOMERS.CUSTOMER_ID, APP.ORDERS.CUSTOMER_ID, APP.SALES.CUSTOMER_ID",
    ]

    failing = _OracleRuntimeService(_FakeDomainInventoryAdapter(OracleAdapterError("ORA-12541")))
    degraded = failing.get_domain_inventory(request)
    assert degraded.domains == []
    assert any("ORA-12541" in warning for warning in degraded.warnings)

    memory = Nl2SqlService(store=MemoryNl2SqlStore()).get_domain_inventory(request)
    assert memory.runtime == "deterministic"
    assert memory.domains == []
    assert any("deterministic runtime" in warning for warning in memory.warnings)

    views_only = service.get_domain_inventory(
        DomainInventoryRequest(targets=[{"object_name": "SALES_V", "object_type": "view"}])
    )
    assert views_only.domains == [] and views_only.warnings == []


def test_deterministic_domain_create_skips_columns_with_existing_domain() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._enterprise_ai_client = FakeEnterpriseAiClient(configured=False)

    result = service.generate_domain_sql(
        _domain_request(structure_text=_DOMAIN_STRUCTURE_WITH_DOMAIN)
    )

    assert "CUSTOMER_ID_D" not in result.sql
    assert 'CREATE DOMAIN IF NOT EXISTS "APP"."REGION_D"' in result.sql
    assert any(
        "既にドメインが付いた列" in warning and "APP.SALES.CUSTOMER_ID" in warning
        for warning in result.warnings
    )


def test_deterministic_domain_update_rebuild_and_delete() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._enterprise_ai_client = FakeEnterpriseAiClient(configured=False)
    inventory = DomainDefinition.model_validate(_DOMAIN_INVENTORY)

    update = service.generate_domain_sql(
        _domain_request(
            structure_text=_DOMAIN_STRUCTURE_WITH_DOMAIN,
            operation="update",
            domains=[inventory],
        )
    )
    assert update.sql == (
        'ALTER DOMAIN "APP"."CUSTOMER_ID_D" ANNOTATIONS (ADD OR REPLACE "DESCRIPTION" \'顧客ID\');'
    )
    assert any("再作成を使ってください" in warning for warning in update.warnings)

    rebuild = service.generate_domain_sql(
        _domain_request(
            structure_text=_DOMAIN_STRUCTURE_WITH_DOMAIN,
            operation="rebuild",
            domains=[inventory],
        )
    )
    assert rebuild.sql.splitlines() == [
        'DROP DOMAIN "APP"."CUSTOMER_ID_D" FORCE;',
        'CREATE DOMAIN "APP"."CUSTOMER_ID_D" AS NUMBER(10) STRICT NOT NULL '
        "CONSTRAINT CHECK (VALUE > 0) "
        "ANNOTATIONS (\"DESCRIPTION\" 'Unique identifier for a customer.', "
        "\"ALIASES\" 'customer id, 顧客ID');",
        'ALTER TABLE "APP"."CUSTOMERS" MODIFY ("CUSTOMER_ID") ADD DOMAIN "APP"."CUSTOMER_ID_D";',
        'ALTER TABLE "APP"."ORDERS" MODIFY ("CUSTOMER_ID") ADD DOMAIN "APP"."CUSTOMER_ID_D";',
        'ALTER TABLE "APP"."SALES" MODIFY ("CUSTOMER_ID") ADD DOMAIN "APP"."CUSTOMER_ID_D";',
    ]
    assert rebuild.warnings[0].startswith("再作成は DROP DOMAIN FORCE")
    assert any("選択外の列 APP.ORDERS.CUSTOMER_ID" in warning for warning in rebuild.warnings)

    enumerated = inventory.model_copy(
        update={"name": "STATUS_D", "domain_type": "enumerated", "data_type": ""}
    )
    skipped = service.generate_domain_sql(
        _domain_request(
            structure_text=_DOMAIN_STRUCTURE_WITH_DOMAIN.replace(
                "APP.CUSTOMER_ID_D", "APP.STATUS_D"
            ),
            operation="rebuild",
            domains=[enumerated],
        )
    )
    assert skipped.sql == ""
    assert any("enumerated ドメインは自動で再作成できません" in w for w in skipped.warnings)

    delete = service.generate_domain_sql(
        _domain_request(
            structure_text=_DOMAIN_STRUCTURE_WITH_DOMAIN,
            operation="delete",
            domains=[inventory],
        )
    )
    assert delete.sql.splitlines() == [
        'ALTER TABLE "APP"."CUSTOMERS" MODIFY ("CUSTOMER_ID") DROP DOMAIN;',
        'ALTER TABLE "APP"."SALES" MODIFY ("CUSTOMER_ID") DROP DOMAIN;',
    ]
    assert any("他の列(APP.ORDERS.CUSTOMER_ID)で使用中" in warning for warning in delete.warnings)

    only_selected = inventory.model_copy(update={"columns": inventory.columns[::2]})
    dropped = service.generate_domain_sql(
        _domain_request(
            structure_text=_DOMAIN_STRUCTURE_WITH_DOMAIN,
            operation="delete",
            domains=[only_selected],
        )
    )
    assert dropped.sql.splitlines()[-1] == 'DROP DOMAIN "APP"."CUSTOMER_ID_D";'

    # inventory が無く構造情報の DOMAIN= だけの場合は、関連付け解除だけを生成する。
    blind = service.generate_domain_sql(
        _domain_request(structure_text=_DOMAIN_STRUCTURE_WITH_DOMAIN, operation="delete")
    )
    assert "DROP DOMAIN;" in blind.sql and 'DROP DOMAIN "APP"' not in blind.sql
    assert any("関連付け先を取得できない" in warning for warning in blind.warnings)


def test_domain_generation_prompt_carries_operation_and_allows_inventory_tables() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    inventory = DomainDefinition.model_validate(_DOMAIN_INVENTORY)
    request = _domain_request(
        structure_text=_DOMAIN_STRUCTURE_WITH_DOMAIN,
        operation="rebuild",
        domains=[inventory],
        domain_text="DOMAIN: APP.CUSTOMER_ID_D\nCOLUMNS: APP.ORDERS.CUSTOMER_ID",
    )
    enterprise_ai = FakeEnterpriseAiClient(
        "DROP DOMAIN APP.CUSTOMER_ID_D FORCE;\n"
        "CREATE DOMAIN APP.CUSTOMER_ID_D AS NUMBER(10) "
        "ANNOTATIONS (\"DESCRIPTION\" 'Unique identifier for a customer.');\n"
        "ALTER TABLE APP.ORDERS MODIFY (CUSTOMER_ID) ADD DOMAIN APP.CUSTOMER_ID_D;\n"
        "ALTER TABLE APP.SALES MODIFY (CUSTOMER_ID) ADD DOMAIN APP.CUSTOMER_ID_D;"
    )
    service._enterprise_ai_client = enterprise_ai

    result = service.generate_domain_sql(request)

    assert result.source == "enterprise_ai"
    assert result.sql.startswith("DROP DOMAIN APP.CUSTOMER_ID_D FORCE;")
    assert "ALTER TABLE APP.ORDERS MODIFY (CUSTOMER_ID) ADD DOMAIN APP.CUSTOMER_ID_D;" in result.sql
    assert result.warnings == [
        "再作成は DROP DOMAIN FORCE で関連付けと制約を一旦外してから作り直します。"
        "途中で失敗した場合は残りの文を確認して再実行してください。"
        "DEFAULT / COLLATE は復元しません。"
    ]
    assert "操作: 再作成" in enterprise_ai.calls[0]["prompt"]
    assert "<既存ドメイン>\nDOMAIN: APP.CUSTOMER_ID_D" in enterprise_ai.calls[0]["context"]

    service._enterprise_ai_client = FakeEnterpriseAiClient(
        "ALTER TABLE APP.OTHER MODIFY (CUSTOMER_ID) DROP DOMAIN;"
    )
    outside = service.generate_domain_sql(request.model_copy(update={"operation": "delete"}))
    assert outside.source == "deterministic"
    assert outside.fallback is True
    assert "選択した表以外" in outside.fallback_detail


def test_deterministic_annotation_skips_domain_columns_and_comment_prompt_guides_codes() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._enterprise_ai_client = FakeEnterpriseAiClient(configured=False)

    result = service.generate_annotation_sql(
        MetadataSqlGenerateRequest(
            targets=[{"object_name": "SALES", "object_type": "table"}],
            structure_text=(
                "OBJECT: APP.SALES\nTYPE: table\nCOMMENT: 売上\nCOLUMNS:\n"
                "- CUST_ID: NUMBER(10) NULLABLE=N DOMAIN=APP.CUSTOMER_ID_D COMMENT=顧客ID\n"
                "- AMT: NUMBER(12,2) NULLABLE=N COMMENT=売上金額"
            ),
        )
    )
    assert result.sql.splitlines() == [
        'ALTER TABLE "APP"."SALES" ANNOTATIONS (ADD OR REPLACE "DESCRIPTION" \'売上\');',
        'ALTER TABLE "APP"."SALES" MODIFY ("AMT" ANNOTATIONS '
        "(ADD OR REPLACE \"DESCRIPTION\" '売上金額'));",
    ]
    assert any("APP.SALES.CUST_ID" in warning for warning in result.warnings)

    enterprise_ai = FakeEnterpriseAiClient("COMMENT ON TABLE APP.SALES IS 'Sales orders.';")
    service._enterprise_ai_client = enterprise_ai
    comments = service.generate_comment_sql(
        MetadataSqlGenerateRequest(
            targets=[{"object_name": "SALES", "object_type": "table"}],
            structure_text="OBJECT: APP.SALES\nTYPE: table\nCOMMENT: 売上",
        )
    )
    assert comments.source == "enterprise_ai"
    assert "コード値の意味" in enterprise_ai.calls[0]["prompt"]
    assert "Join with ORD_TXN.CUST_ID." in enterprise_ai.calls[0]["prompt"]
    assert "推測しない" in enterprise_ai.calls[0]["prompt"]


def test_domain_column_type_keeps_char_length_semantics() -> None:
    """ALL_DOMAIN_COLS は CHAR_USED を持たないため、文字長とバイト長の差で CHAR 長を復元する。"""
    # row = (data_type, data_length, char_length, data_precision, data_scale)
    assert _domain_column_type(("VARCHAR2", 80, 20, None, None)) == "VARCHAR2(20 CHAR)"
    assert _domain_column_type(("VARCHAR2", 20, 20, None, None)) == "VARCHAR2(20)"
    assert _domain_column_type(("CHAR", 12, 3, None, None)) == "CHAR(3 CHAR)"
    assert _domain_column_type(("NUMBER", 22, 0, 10, 2)) == "NUMBER(10,2)"


def test_oracle_adapter_unbounded_select_stops_at_safety_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """件数未指定でも安全上限を超えて読まず、超過は truncated で示す。"""

    class _Cursor:
        description = [("ID",)]

        def __init__(self) -> None:
            self.remaining = [(index,) for index in range(1000)]
            self.fetched = 0

        def __enter__(self) -> _Cursor:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def execute(self, _sql: str) -> None:
            return None

        def fetchmany(self, _max_rows: int) -> list[tuple[int]]:
            batch = self.remaining[:50]
            self.remaining = self.remaining[50:]
            self.fetched += len(batch)
            return batch

    class _Connection:
        def __init__(self, cursor: _Cursor) -> None:
            self._cursor = cursor

        def cursor(self) -> _Cursor:
            return self._cursor

    cursor = _Cursor()

    @contextmanager
    def normal_connection() -> Iterator[_Connection]:
        yield _Connection(cursor)

    adapter = OracleNl2SqlAdapter(get_settings())
    monkeypatch.setattr(adapter, "runtime_connection", normal_connection)
    monkeypatch.setattr(adapter.settings, "nl2sql_max_result_rows", 60)

    with actor_scope("system-admin", is_system_admin=True):
        result = adapter.execute_select("SELECT ID FROM T1", None)

    assert result.returned_count == 60
    assert result.has_more is True
    assert result.truncated is True
    assert cursor.fetched == 100  # 上限を 1 行でも超えた時点で読むのをやめる
