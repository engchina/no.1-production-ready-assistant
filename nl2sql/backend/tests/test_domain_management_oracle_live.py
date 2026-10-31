"""実 Oracle AI Database でドメイン管理(作成 → 把握 → 更新 → 再作成 → 削除)を確認する (#669)。

通常 CI では実行しない。`NL2SQL_RUN_ORACLE_INTEGRATION=1` の明示指定時だけ、現在の schema に
一意な接頭辞の検証表 2 つを作成し、生成 SQL を `domain_sql` policy で実行して dictionary
(ALL_TAB_COLS / ALL_DOMAINS / ALL_DOMAIN_COLS / ALL_ANNOTATIONS_USAGE)で確認し、finally で
必ず削除する。LLM は呼ばない(deterministic 生成だけを使う)。資格情報は出力しない。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values

from app.features.nl2sql.models import (
    DbAdminExecuteData,
    DbAdminObjectDetail,
    DbAdminStatementsRequest,
    DomainInventoryRequest,
    MetadataSqlGenerateRequest,
)
from app.features.nl2sql.oracle_adapter import OracleNl2SqlAdapter
from app.features.nl2sql.service import Nl2SqlService, SchemaRefreshMutationSync
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.settings import get_settings

pytestmark = pytest.mark.skipif(
    os.getenv("NL2SQL_RUN_ORACLE_INTEGRATION") != "1",
    reason="NL2SQL_RUN_ORACLE_INTEGRATION=1 の明示指定が必要です。",
)


class _NoEnterpriseAi:
    def is_configured(self) -> bool:
        return False


class _LiveService(Nl2SqlService):
    """実 adapter を使い、Schema job 投入と LLM 呼び出しだけを外した service。"""

    def __init__(self, adapter: OracleNl2SqlAdapter) -> None:
        super().__init__(store=MemoryNl2SqlStore())
        self._oracle_adapter = adapter
        self._enterprise_ai_client = _NoEnterpriseAi()  # type: ignore[assignment]

    def _use_oracle_runtime(self) -> bool:
        return True

    def _submit_schema_refresh_after_admin_mutation(
        self, *, target_objects: Any, source: str
    ) -> SchemaRefreshMutationSync:
        del target_objects, source
        return SchemaRefreshMutationSync()


def _structure_text(details: Sequence[DbAdminObjectDetail]) -> str:
    """frontend の buildMetadataInputTexts と同じ構造テキスト(DOMAIN= は COMMENT= の前)。"""
    blocks: list[str] = []
    for detail in details:
        lines = [
            f"OBJECT: {detail.qualified_name}",
            f"TYPE: {detail.object_type}",
            f"COMMENT: {detail.comment or '-'}",
            "COLUMNS:",
        ]
        for column in detail.columns:
            domain = f"DOMAIN={column.domain_name} " if column.domain_name else ""
            lines.append(
                f"- {column.column_name}: {column.data_type} "
                f"NULLABLE={'Y' if column.nullable else 'N'} "
                f"{domain}COMMENT={column.comment or column.logical_name or '-'}"
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _details(
    adapter: OracleNl2SqlAdapter, owner: str, names: Sequence[str]
) -> list[DbAdminObjectDetail]:
    return [
        DbAdminObjectDetail.model_validate(
            adapter.get_db_admin_object_detail(
                object_name=name, owner=owner, object_type="table", include_ddl=False
            )
        )
        for name in names
    ]


def _execute(service: Nl2SqlService, sql: str) -> DbAdminExecuteData:
    result = service._execute_db_admin_statements(  # noqa: SLF001
        DbAdminStatementsRequest(sql=sql, policy="domain_sql", confirmation="ADMIN_EXECUTE")
    )
    failures = [
        f"{item.status}: {item.sql} -> {item.error_message}"
        for item in result.statements
        if item.status != "success"
    ]
    assert not failures, "\n".join(failures)
    assert result.executed is True
    return result


def _annotation_rows(cursor: Any, object_name: str, column_name: str) -> list[tuple[str, str, str]]:
    cursor.execute(
        """
        SELECT annotation_name, annotation_value, domain_name
        FROM all_annotations_usage
        WHERE object_name = :object_name AND column_name = :column_name
        ORDER BY annotation_name
        """,
        {"object_name": object_name, "column_name": column_name},
    )
    return [
        (
            str(row[0]),
            str(row[1].read() if hasattr(row[1], "read") else row[1] or ""),
            str(row[2] or ""),
        )
        for row in cursor.fetchall()
    ]


@pytest.fixture
def live_settings() -> Iterator[Any]:
    """conftest が PLATFORM_ORACLE_USER=APP を強制するため、共通 .env の接続ユーザーへ戻す。"""
    settings = get_settings()
    env_user = str(
        dotenv_values(Path(__file__).resolve().parents[3] / "platform" / ".env").get(
            "PLATFORM_ORACLE_USER"
        )
        or ""
    )
    original_user = settings.oracle_user
    if env_user:
        settings.oracle_user = env_user
    try:
        yield settings
    finally:
        settings.oracle_user = original_user


def test_domain_management_lifecycle_on_oracle(live_settings: Any) -> None:
    adapter = OracleNl2SqlAdapter(live_settings)
    with adapter.connection() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT version_full FROM product_component_version WHERE product LIKE 'Oracle%'"
        )
        version = str(cursor.fetchone()[0])
        cursor.execute("SELECT SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') FROM DUAL")
        owner = str(cursor.fetchone()[0])
    if int(version.split(".")[0]) < 23:
        pytest.skip(f"SQL ドメインに対応していないバージョンです(接続先: {version})")
    print(f"\nOracle {version} / schema {owner}")

    suffix = uuid.uuid4().hex[:6].upper()
    cust = f"ZZ_DM_{suffix}_CUST"
    orders = f"ZZ_DM_{suffix}_ORD"
    status_domain = f"ZZ_DM_{suffix}_STATUS_D"
    service = _LiveService(adapter)
    targets = [
        {"owner": owner, "object_name": cust, "object_type": "table"},
        {"owner": owner, "object_name": orders, "object_type": "table"},
    ]
    domain_names = ["CUST_ID_D", "STAT_CD_D", status_domain]
    try:
        with adapter.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                f"CREATE TABLE {cust} (CUST_ID NUMBER(10) NOT NULL, "
                "CUST_NM VARCHAR2(100), STAT_CD CHAR(1) NOT NULL)"
            )
            cursor.execute(
                f"CREATE TABLE {orders} (ORD_ID NUMBER(10) NOT NULL, CUST_ID NUMBER(10) NOT NULL, "
                "AMT NUMBER(12,2), STAT_CD CHAR(1) NOT NULL)"
            )
            cursor.execute(f"COMMENT ON COLUMN {cust}.CUST_ID IS '顧客ID'")
            cursor.execute(f"COMMENT ON COLUMN {orders}.CUST_ID IS '顧客ID'")
            cursor.execute(f"COMMENT ON COLUMN {cust}.STAT_CD IS '顧客状態'")
            cursor.execute(f"INSERT INTO {cust} VALUES (1, 'A社', 'A')")
            cursor.execute(f"INSERT INTO {orders} VALUES (10, 1, 1000, 'C')")
            conn.commit()

        # 0) ドメイン無し: 詳細の domain_name は空、inventory も空で dictionary warning は出ない
        details = _details(adapter, owner, [cust, orders])
        assert all(not column.domain_name for detail in details for column in detail.columns)
        empty = service.get_domain_inventory(DomainInventoryRequest(targets=targets))
        assert empty.runtime == "oracle"
        assert empty.domains == []
        assert empty.warnings == []

        # 1) 作成: 2 表で共通する CUST_ID(NUMBER(10)) と STAT_CD(CHAR(1)) がドメイン候補になる
        create = service.generate_domain_sql(
            MetadataSqlGenerateRequest(
                targets=targets, structure_text=_structure_text(details), operation="create"
            )
        )
        print("\n[create]\n" + create.sql)
        assert create.source == "deterministic"
        assert f'CREATE DOMAIN IF NOT EXISTS "{owner}"."CUST_ID_D" AS NUMBER(10)' in create.sql
        assert f'CREATE DOMAIN IF NOT EXISTS "{owner}"."STAT_CD_D" AS CHAR(1)' in create.sql
        _execute(service, create.sql)

        # 2) 把握: 列の domain_name と inventory(定義・全関連付け列)が dictionary から取れる
        details = _details(adapter, owner, [cust, orders])
        by_column = {
            (detail.name, column.column_name): column.domain_name
            for detail in details
            for column in detail.columns
        }
        assert by_column[(cust, "CUST_ID")] == f"{owner}.CUST_ID_D"
        assert by_column[(orders, "CUST_ID")] == f"{owner}.CUST_ID_D"
        assert by_column[(orders, "STAT_CD")] == f"{owner}.STAT_CD_D"
        assert by_column[(orders, "AMT")] == ""
        inventory = service.get_domain_inventory(DomainInventoryRequest(targets=targets))
        print("\n[inventory]\n" + inventory.domain_text)
        assert inventory.warnings == []
        domains = {domain.name: domain for domain in inventory.domains}
        assert set(domains) == {"CUST_ID_D", "STAT_CD_D"}
        cust_id_d = domains["CUST_ID_D"]
        assert cust_id_d.domain_type == "single"
        assert cust_id_d.data_type == "NUMBER(10)"
        assert cust_id_d.strict is False
        assert sorted((ref.table_name, ref.column_name) for ref in cust_id_d.columns) == [
            (cust, "CUST_ID"),
            (orders, "CUST_ID"),
        ]
        assert [(item.name, item.value) for item in cust_id_d.annotations] == [
            ("DESCRIPTION", "顧客ID")
        ]
        with adapter.connection() as conn, conn.cursor() as cursor:
            inherited = _annotation_rows(cursor, cust, "CUST_ID")
        print(f"\n[inherited annotations on {cust}.CUST_ID] {inherited}")
        # 単一列ドメインの ANNOTATIONS は列へ継承され、DOMAIN_NAME で由来が分かる
        assert ("DESCRIPTION", "顧客ID", "CUST_ID_D") in inherited

        # 3) 更新: ALTER DOMAIN のオブジェクトレベル ANNOTATIONS だけを生成し、実行できる
        update = service.generate_domain_sql(
            MetadataSqlGenerateRequest(
                targets=targets,
                structure_text=_structure_text(details),
                operation="update",
                domains=inventory.domains,
                domain_text=inventory.domain_text,
            )
        )
        print("\n[update]\n" + update.sql)
        assert update.sql.count("ALTER DOMAIN") == 2
        assert "ALTER TABLE" not in update.sql
        _execute(service, update.sql)
        with adapter.connection() as conn, conn.cursor() as cursor:
            after_update = _annotation_rows(cursor, cust, "CUST_ID")
        print(f"\n[after ALTER DOMAIN] {after_update}")

        # 4) 再作成: DROP FORCE → CREATE(定義引き継ぎ)→ 全関連付け列へ ADD DOMAIN が実行でき、
        #    関連付けと継承 annotation が戻る
        rebuild = service.generate_domain_sql(
            MetadataSqlGenerateRequest(
                targets=[targets[0]],
                structure_text=_structure_text(details[:1]),
                operation="rebuild",
                domains=inventory.domains,
                domain_text=inventory.domain_text,
            )
        )
        print("\n[rebuild]\n" + rebuild.sql)
        assert rebuild.sql.count("DROP DOMAIN") == 2
        assert rebuild.sql.count("CREATE DOMAIN") == 2
        assert rebuild.sql.count("ADD DOMAIN") == 4
        assert any(f"{owner}.{orders}.CUST_ID" in warning for warning in rebuild.warnings)
        _execute(service, rebuild.sql)
        rebuilt = service.get_domain_inventory(DomainInventoryRequest(targets=targets))
        rebuilt_domains = {domain.name: domain for domain in rebuilt.domains}
        assert set(rebuilt_domains) == {"CUST_ID_D", "STAT_CD_D"}
        assert len(rebuilt_domains["CUST_ID_D"].columns) == 2
        assert len(rebuilt_domains["STAT_CD_D"].columns) == 2
        with adapter.connection() as conn, conn.cursor() as cursor:
            after_rebuild = _annotation_rows(cursor, orders, "CUST_ID")
        assert ("DESCRIPTION", "顧客ID", "CUST_ID_D") in after_rebuild

        # 5) 記事どおりの CHECK 付きドメインへの付け替え(解除 → 作成 → 関連付け)が policy を通り、
        #    既存データの検証と新規行の拒否が DB 側で効く
        swap = "\n".join(
            [
                f'ALTER TABLE "{owner}"."{cust}" MODIFY ("STAT_CD") DROP DOMAIN;',
                f'CREATE DOMAIN "{owner}"."{status_domain}" AS CHAR(1) STRICT '
                "CONSTRAINT CHECK (VALUE IN ('A', 'I')) "
                "ANNOTATIONS (\"DESCRIPTION\" 'Customer status code.', "
                "\"ALIASES\" 'customer status, 顧客状態, 有効顧客', "
                "\"VALUES\" 'A = active (有効); I = inactive (休眠).');",
                f'ALTER TABLE "{owner}"."{cust}" MODIFY ("STAT_CD") ADD DOMAIN '
                f'"{owner}"."{status_domain}";',
            ]
        )
        _execute(service, swap)
        with adapter.connection() as conn, conn.cursor() as cursor:
            status_rows = _annotation_rows(cursor, cust, "STAT_CD")
            try:
                cursor.execute(f"INSERT INTO {cust} VALUES (2, 'B社', 'X')")
                violated = ""
            except Exception as exc:
                violated = str(exc)
            conn.rollback()
        print(f"\n[status domain annotations] {status_rows}")
        assert ("VALUES", "A = active (有効); I = inactive (休眠).", status_domain) in status_rows
        assert "ORA-11534" in violated, violated
        swapped = service.get_domain_inventory(DomainInventoryRequest(targets=targets))
        swapped_status = next(d for d in swapped.domains if d.name == status_domain)
        assert swapped_status.strict is True
        assert swapped_status.constraints == ["VALUE IN ('A', 'I')"]

        # 6) 削除: 片方の表だけ選ぶと解除を生成し、他表で使用中のドメイン(CUST_ID_D)は DROP しない。
        #    STAT_CD_D は 5) で cust 側を付け替えており、orders の解除だけで DROP される
        details = _details(adapter, owner, [cust, orders])
        partial = service.generate_domain_sql(
            MetadataSqlGenerateRequest(
                targets=[targets[1]],
                structure_text=_structure_text(details[1:]),
                operation="delete",
                domains=swapped.domains,
                domain_text=swapped.domain_text,
            )
        )
        print("\n[delete: orders only]\n" + partial.sql)
        assert partial.sql.count("DROP DOMAIN;") == 2
        assert f'DROP DOMAIN "{owner}"."STAT_CD_D";' in partial.sql
        assert f'DROP DOMAIN "{owner}"."CUST_ID_D";' not in partial.sql
        assert any(
            "CUST_ID_D" in w and "使用中のため DROP DOMAIN は生成しません" in w
            for w in partial.warnings
        )
        _execute(service, partial.sql)
        remaining = service.get_domain_inventory(DomainInventoryRequest(targets=targets))
        remaining_domains = {domain.name: domain for domain in remaining.domains}
        assert set(remaining_domains) == {"CUST_ID_D", status_domain}
        assert [
            (ref.table_name, ref.column_name) for ref in remaining_domains["CUST_ID_D"].columns
        ] == [(cust, "CUST_ID")]

        # 7) 削除: 全ての関連付け先を選ぶと DROP DOMAIN まで生成し、dictionary から消える
        details = _details(adapter, owner, [cust, orders])
        full = service.generate_domain_sql(
            MetadataSqlGenerateRequest(
                targets=targets,
                structure_text=_structure_text(details),
                operation="delete",
                domains=remaining.domains,
                domain_text=remaining.domain_text,
            )
        )
        print("\n[delete: all]\n" + full.sql)
        assert full.sql.count("DROP DOMAIN;") == 2
        assert f'DROP DOMAIN "{owner}"."CUST_ID_D";' in full.sql
        assert f'DROP DOMAIN "{owner}"."{status_domain}";' in full.sql
        _execute(service, full.sql)
        with adapter.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM all_domains WHERE owner = :owner "
                "AND name IN ('CUST_ID_D', 'STAT_CD_D', :status_domain)",
                {"owner": owner, "status_domain": status_domain},
            )
            assert int(cursor.fetchone()[0]) == 0
        gone = service.get_domain_inventory(DomainInventoryRequest(targets=targets))
        assert gone.domains == []
    finally:
        # ドメインは FORCE で関連付けごと外してから表を消す。未作成/削除済みは無視する。
        with adapter.connection() as conn, conn.cursor() as cursor:
            for statement in (
                *(f'DROP DOMAIN "{owner}"."{name}" FORCE' for name in domain_names),
                f"DROP TABLE {cust} PURGE",
                f"DROP TABLE {orders} PURGE",
            ):
                try:
                    cursor.execute(statement)
                except Exception as exc:
                    if not any(
                        code in str(exc) for code in ("ORA-00942", "ORA-11501", "ORA-11504")
                    ):
                        raise
