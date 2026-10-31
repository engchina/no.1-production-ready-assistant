"""#860 の改名 migration。旧名は更新境界に限定し、値・ID・自由本文を保つ。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

TABLE_RENAMES = {
    "RAG_BUSINESS_VIEWS": "RAG_SEARCH_ANSWER_PROFILES",
    "RAG_BUSINESS_VIEW_KNOWLEDGE": "RAG_SEARCH_ANSWER_PROFILE_KNOWLEDGE",
    "RAG_ROLE_BUSINESS_VIEWS": "RAG_ROLE_SEARCH_ANSWER_PROFILES",
}
COLUMN_RENAMES = {
    "RAG_SEARCH_ANSWER_PROFILES": {
        "BUSINESS_VIEW_ID": "SEARCH_ANSWER_PROFILE_ID",
        "VIEW_CONFIG": "PROFILE_CONFIG",
    },
    "RAG_ANSWER_RECORDS": {"BUSINESS_VIEW_ID": "SEARCH_ANSWER_PROFILE_ID"},
    "RAG_QUERY_HISTORY": {"BUSINESS_VIEW_ID": "SEARCH_ANSWER_PROFILE_ID"},
    "RAG_SEARCH_ANSWER_PROFILE_KNOWLEDGE": {"BUSINESS_VIEW_ID": "SEARCH_ANSWER_PROFILE_ID"},
    "RAG_CONVERSATIONS": {"BUSINESS_VIEW_ID": "SEARCH_ANSWER_PROFILE_ID"},
    "RAG_CITATION_FEEDBACK": {"BUSINESS_VIEW_ID": "SEARCH_ANSWER_PROFILE_ID"},
    "RAG_ROLE_SEARCH_ANSWER_PROFILES": {"BUSINESS_VIEW_ID": "SEARCH_ANSWER_PROFILE_ID"},
}
INDEX_RENAMES = {
    "RAG_BUSINESS_VIEWS_TENANT_NAME_UIDX": "RAG_SEARCH_ANSWER_PROFILES_TENANT_NAME_UIDX",
    "RAG_BUSINESS_VIEWS_TENANT_STATUS_IDX": "RAG_SEARCH_ANSWER_PROFILES_TENANT_STATUS_IDX",
    "RAG_ANSWER_RECORDS_VIEW_IDX": "RAG_ANSWER_RECORDS_PROFILE_IDX",
    "RAG_QUERY_HISTORY_VIEW_IDX": "RAG_QUERY_HISTORY_PROFILE_IDX",
    "RAG_CONVERSATIONS_TENANT_VIEW_UPDATED_IDX": "RAG_CONVERSATIONS_TENANT_PROFILE_UPDATED_IDX",
    "RAG_CONVERSATIONS_BUSINESS_VIEW_IDX": "RAG_CONVERSATIONS_SEARCH_ANSWER_PROFILE_IDX",
    "RAG_ROLE_BUSINESS_VIEWS_VIEW_IDX": "RAG_ROLE_SEARCH_ANSWER_PROFILES_VIEW_IDX",
}
CONSTRAINT_RENAMES = {
    "RAG_BUSINESS_VIEWS_STATUS_CK": "RAG_SEARCH_ANSWER_PROFILES_STATUS_CK",
    "RAG_BUSINESS_VIEW_KNOWLEDGE_PK": "RAG_SEARCH_ANSWER_PROFILE_KNOWLEDGE_PK",
    "RAG_BUSINESS_VIEW_KNOWLEDGE_KIND_CK": "RAG_SEARCH_ANSWER_PROFILE_KNOWLEDGE_KIND_CK",
    "RAG_CONVERSATIONS_BUSINESS_VIEW_FK": "RAG_CONVERSATIONS_SEARCH_ANSWER_PROFILE_FK",
    "RAG_ROLE_BUSINESS_VIEWS_PK": "RAG_ROLE_SEARCH_ANSWER_PROFILES_PK",
    "RAG_ROLE_BUSINESS_VIEWS_ROLE_FK": "RAG_ROLE_SEARCH_ANSWER_PROFILES_ROLE_FK",
    "RAG_ROLE_BUSINESS_VIEWS_VIEW_FK": "RAG_ROLE_SEARCH_ANSWER_PROFILES_VIEW_FK",
}

RENAME_MIGRATION = "20261003_001_search_answer_profiles"
PERMISSION_RENAMES = {
    "menu.business_views": "menu.search_answer_profiles",
    "rag.business_views.manage": "rag.search_answer_profiles.manage",
}


def historical_sections() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(
            (Path(__file__).parent / "schema_migrations/pre_profile_rename.json").read_text()
        ),
    )


def current_schema_sql(sql: str) -> str:
    """改名後に過去の DDL を補修する場合の対応。ledger checksum の原文は変えない。"""
    for old, new in (
        *TABLE_RENAMES.items(),
        *INDEX_RENAMES.items(),
        *CONSTRAINT_RENAMES.items(),
        *PERMISSION_RENAMES.items(),
    ):
        sql = sql.replace(old, new).replace(old.lower(), new.lower())
    for mapping in COLUMN_RENAMES.values():
        for old, new in mapping.items():
            sql = sql.replace(old, new).replace(old.lower(), new.lower())
    return sql


def legacy_tables_exist(connection: Any) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT TABLE_NAME FROM USER_TABLES WHERE TABLE_NAME IN (:n0, :n1, :n2)",
            {f"n{i}": name for i, name in enumerate(TABLE_RENAMES)},
        )
        return bool(cursor.fetchall())


def rename_sql() -> str:
    """Oracle dictionary を確認して改名。新旧併存は上書きせず停止し、途中から再実行できる。"""
    calls: list[str] = []
    for old, new in TABLE_RENAMES.items():
        calls.append(f"    rename_table('{old}', '{new}');")
    for table, mapping in COLUMN_RENAMES.items():
        for old, new in mapping.items():
            calls.append(f"    rename_column('{table}', '{old}', '{new}');")
    for old, new in INDEX_RENAMES.items():
        calls.append(f"    rename_index('{old}', '{new}');")
    for old, new in CONSTRAINT_RENAMES.items():
        calls.append(f"    rename_constraint('{old}', '{new}');")
    for old, new in PERMISSION_RENAMES.items():
        # UNION のみで重複 grant を統合。実効権限を保持し、旧行を消す操作は destructive と区別する。
        calls.append(f"    rename_permission('{old}', '{new}');")
    return (
        """DECLARE
    old_count NUMBER;
    new_count NUMBER;
    table_value VARCHAR2(128);
    PROCEDURE rename_table(old_name VARCHAR2, new_name VARCHAR2) IS
    BEGIN
        SELECT COUNT(*) INTO old_count FROM user_tables WHERE table_name = old_name;
        SELECT COUNT(*) INTO new_count FROM user_tables WHERE table_name = new_name;
        IF old_count > 0 AND new_count > 0 THEN
            RAISE_APPLICATION_ERROR(-20060, 'PROFILE_RENAME_CONFLICT');
        ELSIF old_count > 0 THEN
            EXECUTE IMMEDIATE 'ALTER TABLE ' || old_name || ' RENAME TO ' || new_name;
        END IF;
    END;
    PROCEDURE rename_column(tab VARCHAR2, old_name VARCHAR2, new_name VARCHAR2) IS
    BEGIN
        SELECT COUNT(*) INTO old_count
            FROM user_tab_columns WHERE table_name = tab
            AND column_name = old_name;
        SELECT COUNT(*) INTO new_count
            FROM user_tab_columns WHERE table_name = tab
            AND column_name = new_name;
        IF old_count > 0 AND new_count > 0 THEN
            RAISE_APPLICATION_ERROR(-20060, 'PROFILE_COLUMN_RENAME_CONFLICT');
        ELSIF old_count > 0 THEN
            EXECUTE IMMEDIATE 'ALTER TABLE ' || tab || ' RENAME COLUMN '
                || old_name || ' TO ' || new_name;
        END IF;
    END;
    PROCEDURE rename_index(old_name VARCHAR2, new_name VARCHAR2) IS
    BEGIN
        SELECT COUNT(*) INTO old_count FROM user_indexes WHERE index_name = old_name;
        SELECT COUNT(*) INTO new_count FROM user_indexes WHERE index_name = new_name;
        IF old_count > 0 AND new_count > 0 THEN
            RAISE_APPLICATION_ERROR(-20060, 'PROFILE_INDEX_RENAME_CONFLICT');
        ELSIF old_count > 0 THEN
            EXECUTE IMMEDIATE 'ALTER INDEX ' || old_name || ' RENAME TO ' || new_name;
        END IF;
    END;
    PROCEDURE rename_constraint(old_name VARCHAR2, new_name VARCHAR2) IS
    BEGIN
        SELECT COUNT(*) INTO old_count
                FROM user_constraints WHERE constraint_name = old_name;
        SELECT COUNT(*) INTO new_count FROM user_constraints WHERE constraint_name = new_name;
        IF old_count > 0 AND new_count > 0 THEN
            RAISE_APPLICATION_ERROR(-20060, 'PROFILE_CONSTRAINT_RENAME_CONFLICT');
        ELSIF old_count > 0 THEN
            SELECT table_name INTO table_value
                FROM user_constraints WHERE constraint_name = old_name;
            EXECUTE IMMEDIATE 'ALTER TABLE ' || table_value || ' RENAME CONSTRAINT '
                || old_name || ' TO ' || new_name;
        END IF;
    END;
    PROCEDURE rename_permission(old_code VARCHAR2, new_code VARCHAR2) IS
    BEGIN
        SELECT COUNT(*) INTO old_count FROM user_tables WHERE table_name = 'RAG_ROLE_PERMISSIONS';
        IF old_count > 0 THEN
            EXECUTE IMMEDIATE 'MERGE INTO rag_role_permissions dst USING '
                || '(SELECT role_id FROM rag_role_permissions WHERE permission_code = :1) src '
                || 'ON (dst.role_id = src.role_id AND dst.permission_code = :2) '
                || 'WHEN NOT MATCHED THEN INSERT (role_id, permission_code) '
                || 'VALUES (src.role_id, :3)'
                USING old_code, new_code, new_code;
            EXECUTE IMMEDIATE 'DELETE FROM rag_role_permissions WHERE permission_code = :1'
                USING old_code;
        END IF;
    END;
    PROCEDURE rename_diagnostics IS
    BEGIN
        SELECT COUNT(*) INTO old_count FROM user_tab_columns
            WHERE table_name = 'RAG_ANSWER_RECORDS' AND column_name = 'DIAGNOSTICS_JSON';
        IF old_count > 0 THEN
            EXECUTE IMMEDIATE q'[SELECT COUNT(*) FROM rag_answer_records
                WHERE JSON_EXISTS(diagnostics_json, '$.business_view_applied')
                AND JSON_EXISTS(diagnostics_json, '$.search_answer_profile_applied')]'
                INTO new_count;
            IF new_count > 0 THEN
                RAISE_APPLICATION_ERROR(-20060, 'PROFILE_DIAGNOSTICS_RENAME_CONFLICT');
            END IF;
            EXECUTE IMMEDIATE q'[UPDATE rag_answer_records
                SET diagnostics_json = JSON_TRANSFORM(diagnostics_json,
                    RENAME '$.business_view_applied' = 'search_answer_profile_applied')
                WHERE JSON_EXISTS(diagnostics_json, '$.business_view_applied')]';
        END IF;
    END;
BEGIN
    rename_diagnostics;
"""
        + "\n".join(calls)
        + "\nEND;"
    )
