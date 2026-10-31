"""Application auth/RBAC security tables を idempotent に作成する。

3 製品で共有する `PLATFORM_*`（platform の DDL）を作ってから、NL2SQL 固有の
`NL2SQL_APP_ROLE_PERMISSIONS` / `NL2SQL_APP_ROLE_PROFILES` / `NL2SQL_APP_DATA_ENTITLEMENTS` /
`NL2SQL_DEEPSEC_MIGRATIONS`（migration 004）を作る。既存の表は変えない（ORA-00955 を読み飛ばす）。

旧名・旧形からの移行（`RAG_APP_*` → `NL2SQL_APP_*` → `PLATFORM_*` の改名、旧監査ログの表の
削除、権限の行の削除・追加、列の追加など）は、初めての release の前に削除した（#1648。後方互換を
持たない）。既存の ADB の同じ schema に旧名の表（旧版の単独の製品など）があっても、改名・削除・
変更はせず、名前を出して残す。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from pr_backend_core.logging import configure_cli_logging
from pr_backend_core.oracle_dsn import enable_cli_connect_mode
from pr_system_settings.auth.migrations import PLATFORM_AUTH_DDL, PLATFORM_AUTH_DDL_IGNORED_ERRORS
from pr_system_settings.auth.store import PLATFORM_AUTH_TABLES

from app.clients.oracle_runtime import get_oracle_pool_manager
from app.clients.oracle_statement_executor import oracle_statement_executor

SECURITY_MIGRATION_FILE = "004_app_security_rbac.sql"

# NL2SQL 固有のセキュリティの表（migration 004 が作る）。
NL2SQL_SECURITY_TABLES: tuple[str, ...] = (
    "NL2SQL_APP_ROLE_PERMISSIONS",
    "NL2SQL_APP_DATA_ENTITLEMENTS",
    "NL2SQL_DEEPSEC_MIGRATIONS",
    "NL2SQL_APP_ROLE_PROFILES",
)
# `--apply` の後にそろっているべき表（配備の確認 `nl2sql_system_schema --check` が使う。#1648）。
SECURITY_TABLES: tuple[str, ...] = (*PLATFORM_AUTH_TABLES, *NL2SQL_SECURITY_TABLES)

# 3 製品の統合（#212）より前の名前の表。改名・削除・変更はせず、あれば名前を出して残す（#1648）。
LEGACY_SECURITY_TABLES: tuple[str, ...] = (
    "RAG_APP_USERS",
    "RAG_APP_ROLES",
    "RAG_APP_USER_ROLES",
    "RAG_APP_ROLE_PERMISSIONS",
    "RAG_APP_DATA_ENTITLEMENTS",
    "RAG_AUTH_SESSIONS",
    "RAG_AUTH_AUDIT_LOG",
    "RAG_DEEPSEC_MIGRATIONS",
    "NL2SQL_APP_USERS",
    "NL2SQL_APP_ROLES",
    "NL2SQL_APP_USER_ROLES",
    "NL2SQL_AUTH_SESSIONS",
    "NL2SQL_AUTH_AUDIT_LOG",
)


def split_ddl(sql: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    for line in sql.splitlines():
        if line.strip().startswith("--"):
            continue
        current.append(line)
        if line.rstrip().endswith(";"):
            statement = "\n".join(current).strip().removesuffix(";").strip()
            if statement:
                statements.append(statement)
            current = []
    trailing = "\n".join(current).strip()
    if trailing:
        statements.append(trailing)
    return statements


def security_statements() -> list[str]:
    """NL2SQL 固有のセキュリティの表の DDL（migration 004）。"""

    migration = Path(__file__).resolve().parents[2] / "migrations" / SECURITY_MIGRATION_FILE
    return split_ddl(migration.read_text(encoding="utf-8"))


def _existing_tables(connection: Any, names: tuple[str, ...]) -> set[str]:
    binds = {f"n{index}": name for index, name in enumerate(names)}
    placeholders = ", ".join(f":{key}" for key in binds)
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT TABLE_NAME FROM USER_TABLES WHERE TABLE_NAME IN ({placeholders})",  # nosec B608 - 固定の bind
            binds,
        )
        return {str(row[0]).upper() for row in cursor.fetchall()}


def legacy_security_tables(connection: Any) -> list[str]:
    """schema に残る旧名の表（改名・削除しない。#1648）。"""

    existing = _existing_tables(connection, LEGACY_SECURITY_TABLES)
    return [name for name in LEGACY_SECURITY_TABLES if name in existing]


def missing_security_tables(connection: Any) -> list[str]:
    """`--apply` で作る表のうち、無いもの（配備の確認。#1648）。"""

    existing = _existing_tables(connection, SECURITY_TABLES)
    return [name for name in SECURITY_TABLES if name not in existing]


def _with_migration_label(migration: str, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for result in results:
        result["migration"] = migration
    return results


def apply_security_migrations() -> dict[str, Any]:
    """`PLATFORM_*` と NL2SQL 固有のセキュリティの表を冪等に作る（ユーザーは作らない）。

    旧名の表は改名・削除・変更しない。戻り値の `legacy_tables` に、schema に残る旧名の表を返す。
    """

    statements = security_statements()
    with get_oracle_pool_manager().control_connection() as connection:
        legacy_tables = legacy_security_tables(connection)
        platform_results = _with_migration_label(
            "platform-auth",
            oracle_statement_executor.execute(
                connection,
                list(PLATFORM_AUTH_DDL),
                atomic=False,
                include_sql=False,
                ignored_error_codes=PLATFORM_AUTH_DDL_IGNORED_ERRORS,
            ),
        )
        results = _with_migration_label(
            "004",
            oracle_statement_executor.execute(
                connection,
                statements,
                atomic=False,
                include_sql=False,
                ignored_error_codes=frozenset({"ORA-00955", "ORA-00001"}),
            ),
        )
    errors = [result for result in (*platform_results, *results) if result["status"] == "error"]
    if errors:
        first_error = errors[0]
        location = (
            f"migration={first_error.get('migration', '-')}"
            f" statement={first_error.get('index', '-')}"
        )
        raise RuntimeError(
            f"{location}: {first_error.get('error_message') or 'security migration failed'}"
        )
    return {
        "platform_statements": len(PLATFORM_AUTH_DDL),
        "statements": len(statements),
        "legacy_tables": legacy_tables,
    }


def main() -> int:
    configure_cli_logging("nl2sql")
    # 配備（init_script.sh）が retry する CLI。DB の停止中に TNS の再試行で長く待たない（#1779）。
    enable_cli_connect_mode()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Oracle へ migration を適用する")
    parser.add_argument(
        "--skip-bootstrap",
        action="store_true",
        help="初回 SYSTEM_ADMIN の作成を行わない",
    )
    args = parser.parse_args()
    statements = security_statements()
    if not args.apply:
        print(
            f"migration=platform-auth statements={len(PLATFORM_AUTH_DDL)} mode=preview "
            f"migration=004 statements={len(statements)}"
        )
        return 0

    result = apply_security_migrations()
    bootstrapped = False
    if not args.skip_bootstrap:
        from app.security.service import get_security_service

        bootstrapped = get_security_service().bootstrap()
    if result["legacy_tables"]:
        print(
            "旧名の表（"
            + ", ".join(result["legacy_tables"])
            + "）は改名・削除・変更せずに残します。NL2SQL はこれらの表を読みません。"
            "不要なら書き出してから手動で削除してください（#1648）。",
            file=sys.stderr,
        )
    print(
        f"migration=platform-auth statements={result['platform_statements']} mode=applied "
        f"migration=004 statements={result['statements']} "
        f"legacy_tables={len(result['legacy_tables'])} "
        f"bootstrap_created={str(bootstrapped).lower()}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI boundary
    raise SystemExit(main())
