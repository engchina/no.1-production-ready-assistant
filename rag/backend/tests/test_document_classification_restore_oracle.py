"""取り消した migration を当てた DB の文書の分類の列を戻す migration の実 Oracle のテスト（#2450）。

SQL の形は ``test_oracle_schema_cli.py`` が確かめる。ここでは実 Oracle で、取り消した migration
``20261016_002_document_effective_period``（#2127。#2131 で取り消し）を当てた DB を再現し、
システムテーブルの状態が欠けた列を出すこと・更新で列が戻り分類の一覧の API が 200 になること・
有効期間だけが写ること・冪等なことを確かめる（未到達なら oracle_db fixture が skip し、
CI は ``RAG_TEST_REQUIRE_ORACLE=1`` で失敗にする）。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx

from app.clients.oracle import OracleClient
from app.main import app
from app.rag.oracle_schema import oracle_schema_migration_sections, split_sql_statements
from app.rag.system_schema import MIGRATIONS, SystemSchemaManager
from app.schemas.document import DocumentClassification
from tests import _oracle_test_db
from tests.support import TEST_REQUEST_HEADERS

_RESTORE = "20261019_001_restore_document_classification"
_RETRACTED = "20261016_002_document_effective_period"

# 取り消した migration の SQL（#2127 の版のまま）。今のコードには無いので、再現のためにここに置く。
_RETRACTED_SQL = """DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_DOCUMENTS' AND column_name = 'CLASSIFICATION';
    IF v_count > 0 THEN
        EXECUTE IMMEDIATE
            'ALTER TABLE rag_documents RENAME COLUMN classification TO effective_period';
    END IF;
    SELECT COUNT(*) INTO v_count
    FROM user_tab_columns
    WHERE table_name = 'RAG_DOCUMENTS' AND column_name = 'EFFECTIVE_PERIOD';
    IF v_count > 0 THEN
        EXECUTE IMMEDIATE
            'UPDATE rag_documents SET effective_period = CASE '
            || 'WHEN JSON_VALUE(effective_period, ''$.effective_from'') IS NULL '
            || 'AND JSON_VALUE(effective_period, ''$.effective_to'') IS NULL THEN NULL '
            || 'ELSE JSON_OBJECT('
            || '''effective_from'' VALUE JSON_VALUE(effective_period, ''$.effective_from''), '
            || '''effective_to'' VALUE JSON_VALUE(effective_period, ''$.effective_to'') '
            || 'ABSENT ON NULL RETURNING JSON) END '
            || 'WHERE effective_period IS NOT NULL';
    END IF;
END;"""


def _restore_statements() -> list[str]:
    section = next(item for item in oracle_schema_migration_sections() if item.name == _RESTORE)
    return split_sql_statements(section.sql)


def _column_exists(cursor: Any, table: str, column: str) -> bool:
    cursor.execute(
        "SELECT COUNT(*) FROM user_tab_columns WHERE table_name = :t AND column_name = :c",
        {"t": table, "c": column},
    )
    return int(cursor.fetchone()[0]) > 0


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "read"):
        value = value.read()
    return json.loads(value) if isinstance(value, (str, bytes)) else value


async def _list_categories() -> httpx.Response:
    """分類の一覧の API（``GET /api/document-categories``）を、テストの event loop の中で呼ぶ。"""
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get("/api/document-categories", headers=TEST_REQUEST_HEADERS)


def _manager() -> SystemSchemaManager:
    return SystemSchemaManager(_oracle_test_db._schema_connection)


async def _seed_documents(oracle: OracleClient) -> dict[str, str]:
    """分類と有効期間を持つ文書（取り消した migration を当てる前の値）。"""
    documents: dict[str, str] = {}
    values = {
        # 分類と有効期間: 有効期間だけが残る。
        "both": DocumentClassification(
            large_category="分類 A",
            middle_category="分類 B",
            effective_from=date(2026, 1, 1),
            effective_to=date(2026, 12, 31),
        ),
        # 分類だけ: 取り消した migration が値を消す。
        "categories": DocumentClassification(large_category="分類 A"),
        # 終了日だけ。
        "to_only": DocumentClassification(effective_to=date(2027, 3, 31)),
        # 下で形の合わない値に書き換える。
        "invalid": DocumentClassification(effective_from=date(2026, 4, 1)),
        "inverted": DocumentClassification(effective_from=date(2026, 4, 1)),
    }
    for key, classification in values.items():
        document = await oracle.create_document(
            file_name=f"classification-{key}.pdf",
            object_storage_path=f"local://classification-{key}.pdf",
            content_type="application/pdf",
        )
        await oracle.save_document_classification(document.id, classification)
        documents[key] = document.id
    return documents


async def test_restore_classification_after_retracted_migration_on_real_oracle(
    oracle_schema_locked: None,
) -> None:
    oracle = OracleClient()
    documents = await _seed_documents(oracle)
    with _oracle_test_db._schema_connection() as connection:
        cursor = connection.cursor()
        restore = next(migration for migration in MIGRATIONS if migration.name == _RESTORE)
        manager = _manager()
        try:
            # 取り消した migration を当てた DB（台帳に記録あり・戻す migration は未適用）。
            cursor.execute(_RETRACTED_SQL)
            with connection.cursor() as ledger:
                manager._merge_migration(
                    ledger, key=_RETRACTED, description="rag_documents", checksum="retracted"
                )
            cursor.execute(
                "DELETE FROM rag_schema_migrations WHERE migration_name = :name", {"name": _RESTORE}
            )
            # 取り消した migration の後に入った、形の合わない有効期間。
            cursor.execute(
                "UPDATE rag_documents SET effective_period = JSON(:value) WHERE document_id = :id",
                {"value": '{"effective_from":"2026-13-01"}', "id": documents["invalid"]},
            )
            cursor.execute(
                "UPDATE rag_documents SET effective_period = JSON(:value) WHERE document_id = :id",
                {
                    "value": '{"effective_from":"2026-12-31","effective_to":"2026-01-01"}',
                    "id": documents["inverted"],
                },
            )
            connection.commit()
            assert not _column_exists(cursor, "RAG_DOCUMENTS", "CLASSIFICATION")

            # Issue の症状: 分類の一覧の API が ORA-00904 で 500 になる。
            assert (await _list_categories()).status_code == 500

            # 状態は欠けた列を出し、ready にしない。
            before = manager.status()
            assert before["status"] == "outdated"
            assert _RESTORE in before["pending_versions"]
            assert {
                "table_name": "RAG_DOCUMENTS",
                "column_name": "CLASSIFICATION",
            } in before["missing_columns"]
            # データを消す migration ではないので、承認は要らない。
            assert _RESTORE not in {
                item["name"] for item in before["pending_destructive_migrations"]
            }

            # 「作成・更新」と同じ経路で戻す。
            manager.initialize()
            after = manager.status()
            assert after["missing_columns"] == []
            assert _RESTORE in after["applied_versions"]
            assert _column_exists(cursor, "RAG_DOCUMENTS", "CLASSIFICATION")

            # 利用者が保存し直した分類は、もう一度当てても上書きしない（冪等）。
            await oracle.save_document_classification(
                documents["to_only"],
                DocumentClassification(large_category="分類 C", effective_to=date(2027, 3, 31)),
            )
            for _ in range(2):
                for statement in _restore_statements():
                    cursor.execute(statement)
            connection.commit()

            cursor.execute(
                "SELECT document_id, classification, effective_period FROM rag_documents "
                f"WHERE document_id IN ({', '.join(f':d{i}' for i in range(len(documents)))})",
                {f"d{i}": value for i, value in enumerate(documents.values())},
            )
            rows = {row[0]: (_json_value(row[1]), _json_value(row[2])) for row in cursor.fetchall()}
            by_key = {key: rows[document_id] for key, document_id in documents.items()}
            # 有効期間だけが戻る（取り消した migration が消した分類は戻らない）。
            assert by_key["both"][0] == {
                "effective_from": "2026-01-01",
                "effective_to": "2026-12-31",
            }
            assert by_key["categories"][0] is None
            assert by_key["to_only"][0] == {
                "large_category": "分類 C",
                "effective_to": "2027-03-31",
            }
            # 形の合わない値は写さず、effective_period に残す。
            assert by_key["invalid"] == (None, {"effective_from": "2026-13-01"})
            assert by_key["inverted"][0] is None
            assert by_key["inverted"][1] == {
                "effective_from": "2026-12-31",
                "effective_to": "2026-01-01",
            }

            saved = await oracle.get_document(documents["both"])
            assert saved is not None and saved.classification is not None
            assert saved.classification.effective_from == date(2026, 1, 1)
            assert saved.classification.large_category is None

            # 分類の一覧の API が 200 を返す。
            listed = await _list_categories()
            assert listed.status_code == 200, listed.text
            assert isinstance(listed.json()["data"], list)
        finally:
            connection.rollback()
            for statement in _restore_statements():
                cursor.execute(statement)
            if _column_exists(cursor, "RAG_DOCUMENTS", "EFFECTIVE_PERIOD"):
                cursor.execute("ALTER TABLE rag_documents DROP COLUMN effective_period")
            cursor.execute(
                "DELETE FROM rag_schema_migrations WHERE migration_name = :name",
                {"name": _RETRACTED},
            )
            connection.commit()
            manager._record_migration(connection, restore)


async def test_restore_classification_is_no_op_on_current_schema_on_real_oracle(
    oracle_schema_locked: None,
) -> None:
    """取り消した migration を当てていない DB では、何も変えない（新しい DB と同じ）。"""
    oracle = OracleClient()
    document = await oracle.create_document(
        file_name="classification-current.pdf",
        object_storage_path="local://classification-current.pdf",
        content_type="application/pdf",
    )
    classification = DocumentClassification(large_category="分類 A", effective_to=date(2027, 1, 1))
    await oracle.save_document_classification(document.id, classification)
    with _oracle_test_db._schema_connection() as connection:
        cursor = connection.cursor()
        for _ in range(2):
            for statement in _restore_statements():
                cursor.execute(statement)
        connection.commit()
        assert not _column_exists(cursor, "RAG_DOCUMENTS", "EFFECTIVE_PERIOD")
    saved = await oracle.get_document(document.id)
    assert saved is not None and saved.classification == classification
    assert _manager().status()["missing_columns"] == []
