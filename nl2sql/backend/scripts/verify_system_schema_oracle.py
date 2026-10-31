"""一次性 Oracle user で NL2SQL system schema lifecycle を検証する。"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.clients.oracle_runtime import get_oracle_pool_manager
from app.features.nl2sql.oracle_adapter import oracle_connect_kwargs
from app.features.settings.system_schema import (
    RECREATE_CONFIRMATION,
    SystemSchemaError,
    SystemSchemaManager,
)
from app.settings import get_settings


def _safe_identifier(value: str) -> str:
    normalized = value.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", normalized):
        raise RuntimeError("Oracle identifier is not safe")
    return normalized


def main() -> int:
    settings = get_settings()
    username = _safe_identifier(f"NL2SQL_IT_{secrets.token_hex(4).upper()}")
    password = f"Tmp-{secrets.token_urlsafe(24)}-9aA"
    # system schema が管理しないアプリの表（全再作成で消さないこと）の見本。
    legacy_table = "NL2SQL_STATE_STORE"
    if username == settings.oracle_user.strip().upper():
        raise RuntimeError("temporary user must differ from the configured application user")

    created = False
    result: dict[str, Any] = {"temporary_user_created": False}
    try:
        with get_oracle_pool_manager().control_connection() as admin:
            with admin.cursor() as cursor:
                cursor.execute(f'CREATE USER "{username}" IDENTIFIED BY "{password}"')
                cursor.execute(
                    f'GRANT CREATE SESSION, CREATE TABLE, CREATE SEQUENCE TO "{username}"'
                )
                cursor.execute(f'GRANT UNLIMITED TABLESPACE TO "{username}"')
            admin.commit()
        created = True
        result["temporary_user_created"] = True

        @contextmanager
        def temporary_connection() -> Iterator[Any]:
            import oracledb

            connection = oracledb.connect(
                **oracle_connect_kwargs(settings, user=username, password=password)
            )
            try:
                yield connection
            finally:
                connection.close()

        manager = SystemSchemaManager(
            temporary_connection,
            ddl_lock_timeout_seconds=1,
        )
        with temporary_connection() as connection, connection.cursor() as cursor:
            cursor.execute("CREATE TABLE NL2SQL_APP_USERS (ID NUMBER PRIMARY KEY)")
            cursor.execute("INSERT INTO NL2SQL_APP_USERS (ID) VALUES (1)")
            cursor.execute("CREATE TABLE USER_BUSINESS_SENTINEL (ID NUMBER PRIMARY KEY)")
            cursor.execute("INSERT INTO USER_BUSINESS_SENTINEL (ID) VALUES (1)")
            cursor.execute(
                f"CREATE TABLE {legacy_table} "  # nosec B608 - validated identifier
                "(STATE_KEY VARCHAR2(32) PRIMARY KEY, STATE_JSON CLOB)"
            )
            cursor.execute(
                f"INSERT INTO {legacy_table} (STATE_KEY, STATE_JSON) "  # nosec B608
                "VALUES ('default', '{}')"
            )
            cursor.execute(
                "CREATE TABLE NL2SQL_FEEDBACK_VECTORS "
                "(ID NUMBER PRIMARY KEY, EMBEDDING VECTOR(1536, FLOAT32))"
            )
            cursor.execute("INSERT INTO NL2SQL_FEEDBACK_VECTORS (ID) VALUES (1)")
            connection.commit()

        initialized = manager.initialize()

        # 旧 migration の対象表がロック中でも、欠損 object の所有 migration (v8)
        # だけを選べば更新できることを二つの session で確認する。
        with temporary_connection() as connection, connection.cursor() as cursor:
            cursor.execute("DROP TABLE NL2SQL_EVALUATION_RESULTS CASCADE CONSTRAINTS PURGE")
            cursor.execute("DROP TABLE NL2SQL_EVALUATION_JOBS CASCADE CONSTRAINTS PURGE")
            cursor.execute("DELETE FROM NL2SQL_SCHEMA_MIGRATIONS WHERE VERSION_NO = 8")
            connection.commit()
        with temporary_connection() as locked_connection:
            with locked_connection.cursor() as cursor:
                cursor.execute("LOCK TABLE NL2SQL_SCHEMA_COLUMNS IN ROW EXCLUSIVE MODE")
            selective_migration = manager.initialize()
            locked_connection.rollback()
        assert selective_migration["status"] == "ready"
        assert selective_migration["applied_versions"] == [0, 1, 2, 3, 5, 6, 7, 8]

        def reset_v7_constraint() -> None:
            with temporary_connection() as connection, connection.cursor() as cursor:
                cursor.execute(
                    "ALTER TABLE NL2SQL_ONTOLOGY_PROFILE_VIEWS "
                    "DROP CONSTRAINT FK_NL2SQL_ONT_VIEW_PROFILE"
                )
                cursor.execute("DELETE FROM NL2SQL_SCHEMA_MIGRATIONS WHERE VERSION_NO = 7")
                connection.commit()

        # timeout まで解放されない実ロックは ORA-00054/409 相当となり、
        # lock 解放後の同一操作が安全に再開できることを確認する。
        reset_v7_constraint()
        with temporary_connection() as locked_connection:
            with locked_connection.cursor() as cursor:
                cursor.execute("LOCK TABLE NL2SQL_ONTOLOGY_PROFILE_VIEWS IN ROW EXCLUSIVE MODE")
            try:
                manager.initialize()
            except SystemSchemaError as exc:
                assert exc.code == "ORA-00054"
                assert exc.status_code == 409
                lock_timeout_observed = True
            else:
                raise AssertionError("ORA-00054 was not raised for the held DDL lock")
            finally:
                locked_connection.rollback()
        retried_after_lock_release = manager.initialize()
        assert retried_after_lock_release["status"] == "ready"

        # timeout 内に別 session が lock を解放した場合は、そのまま成功する。
        reset_v7_constraint()
        lock_ready = threading.Event()
        lock_errors: list[str] = []

        def hold_then_release_lock() -> None:
            try:
                with temporary_connection() as locked_connection:
                    with locked_connection.cursor() as cursor:
                        cursor.execute(
                            "LOCK TABLE NL2SQL_ONTOLOGY_PROFILE_VIEWS IN ROW EXCLUSIVE MODE"
                        )
                    lock_ready.set()
                    time.sleep(0.25)
                    locked_connection.rollback()
            except Exception as exc:  # pragma: no cover - real Oracle boundary
                lock_errors.append(type(exc).__name__)
                lock_ready.set()

        lock_thread = threading.Thread(target=hold_then_release_lock, daemon=True)
        lock_thread.start()
        if not lock_ready.wait(timeout=10):
            raise AssertionError("lock session did not become ready")
        if lock_errors:
            raise AssertionError(f"lock session failed: {lock_errors[0]}")
        released_within_timeout = SystemSchemaManager(
            temporary_connection,
            ddl_lock_timeout_seconds=3,
        ).initialize()
        lock_thread.join(timeout=10)
        if lock_thread.is_alive():
            raise AssertionError("lock session did not finish")
        assert released_within_timeout["status"] == "ready"
        with temporary_connection() as connection, connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO NL2SQL_PROFILES
                    (PROFILE_ID, NAME, CATEGORY, DESCRIPTION, ARCHIVED,
                     ALLOWED_TABLE_COUNT, ALLOWED_VIEW_COUNT, GLOSSARY_COUNT,
                     FEW_SHOT_COUNT, VERSION_NO, ETAG, PAYLOAD_JSON)
                VALUES
                    ('integration-profile', 'Integration', NULL, NULL, 0,
                     0, 0, 0, 0, 1, 'integration-etag', '{}')
                """)
            cursor.execute(
                "UPDATE NL2SQL_SCHEMA_MIGRATIONS SET CHECKSUM = 'stale' WHERE VERSION_NO = 5"
            )
            connection.commit()

        assert manager.status()["status"] == "outdated"
        migrated = manager.initialize()
        with temporary_connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM NL2SQL_PROFILES WHERE PROFILE_ID = 'integration-profile'"
            )
            profile_preserved = int(cursor.fetchone()[0]) == 1
        assert profile_preserved

        recreated = manager.initialize(
            recreate=True,
            confirmation=RECREATE_CONFIRMATION,
        )
        with temporary_connection() as connection, connection.cursor() as cursor:
            preserved_counts: dict[str, int] = {}
            for table_name in (
                "NL2SQL_APP_USERS",
                "USER_BUSINESS_SENTINEL",
                legacy_table,
                "NL2SQL_FEEDBACK_VECTORS",
            ):
                cursor.execute(
                    f"SELECT COUNT(*) FROM {table_name}"  # nosec B608 - validated constants
                )
                preserved_counts[table_name] = int(cursor.fetchone()[0])
            cursor.execute("SELECT COUNT(*) FROM NL2SQL_PROFILES")
            core_profile_count = int(cursor.fetchone()[0])

        assert all(count == 1 for count in preserved_counts.values())
        assert core_profile_count == 0
        assert manager.status()["status"] == "ready"
        result.update(
            initialized_operation=initialized["operation"],
            migrated_operation=migrated["operation"],
            selective_migration_operation=selective_migration["operation"],
            selective_migration_versions=selective_migration["applied_versions"],
            ddl_lock_timeout_observed=lock_timeout_observed,
            retry_after_lock_release_status=retried_after_lock_release["status"],
            release_within_timeout_status=released_within_timeout["status"],
            recreated_operation=recreated["operation"],
            profile_preserved_during_upgrade=profile_preserved,
            core_profile_count_after_recreate=core_profile_count,
            preserved_counts=preserved_counts,
            final_status="ready",
        )
    finally:
        if created:
            with (
                get_oracle_pool_manager().control_connection() as admin,
                admin.cursor() as cursor,
            ):
                cursor.execute(f'DROP USER "{username}" CASCADE')
                admin.commit()
            result["temporary_user_deleted"] = True

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))  # noqa: T201
    return 0


if __name__ == "__main__":  # pragma: no cover - operator boundary
    raise SystemExit(main())
