"""system table 設定 API の契約テスト。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from pytest import MonkeyPatch

from app.main import app
from app.rag.system_schema import SystemSchemaError, system_schema_manager
from tests.security_support import enable_production_auth, login
from tests.support import AsgiTestClient

client = AsgiTestClient(app)


async def _run_inline(operation: Any, *args: Any, **kwargs: Any) -> Any:
    return operation(*args, **kwargs)


def _status_payload() -> dict[str, Any]:
    return {
        "status": "ready",
        "schema_version": "2",
        "schema_head": "20260703_002_feedback_details",
        "applied_versions": [],
        "pending_versions": [],
        "expected_object_count": 97,
        "existing_object_count": 97,
        "expected_table_count": 28,
        "existing_table_count": 28,
        "missing_objects": [],
        "retired_objects": [],
        "tables": [],
        "operation_state": {
            "status": "idle",
            "operation_kind": None,
            "lease_expires_at": None,
            "last_error_code": None,
            "schema_epoch": 1,
            "updated_at": None,
        },
    }


def test_system_tables_status_is_read_only(monkeypatch: MonkeyPatch) -> None:
    calls = 0

    def status() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return _status_payload()

    monkeypatch.setattr(system_schema_manager, "status", status)
    monkeypatch.setattr(asyncio, "to_thread", _run_inline)

    response = client.get("/api/settings/database/system-tables")

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "ready"
    assert calls == 1
    # 外部キーの差分が無い応答（旧い manager の形）でも空の一覧になる（#505）。
    assert response.json()["data"]["missing_foreign_keys"] == []
    assert response.json()["data"]["orphaned_foreign_keys"] == []


def test_system_tables_status_returns_foreign_key_drift(monkeypatch: MonkeyPatch) -> None:
    foreign_key = {
        "name": "RAG_CHUNK_SETS_DOCUMENT_FK",
        "table_name": "RAG_CHUNK_SETS",
        "columns": ["DOCUMENT_ID"],
        "referenced_table_name": "RAG_DOCUMENTS",
        "referenced_columns": ["DOCUMENT_ID"],
        "delete_rule": "CASCADE",
        "orphan_rows": 240,
    }
    monkeypatch.setattr(
        system_schema_manager,
        "status",
        lambda: {
            **_status_payload(),
            "status": "outdated",
            "missing_foreign_keys": [foreign_key],
            "orphaned_foreign_keys": [{**foreign_key, "name": "RAG_DOC_EXT_DOCUMENT_FK"}],
        },
    )
    monkeypatch.setattr(asyncio, "to_thread", _run_inline)

    response = client.get("/api/settings/database/system-tables")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "outdated"
    # 削除規則の違い（#511）の項目は、それ以外の FK では null。
    assert data["missing_foreign_keys"] == [
        {**foreign_key, "current_name": None, "current_delete_rule": None}
    ]
    assert data["orphaned_foreign_keys"][0]["name"] == "RAG_DOC_EXT_DOCUMENT_FK"
    assert data["orphaned_foreign_keys"][0]["orphan_rows"] == 240


def test_initialize_system_tables_returns_typed_operation(monkeypatch: MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def initialize(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            **_status_payload(),
            "operation": "initialized",
            "dropped_object_count": 0,
            "created_object_count": 96,
        }

    monkeypatch.setattr(system_schema_manager, "initialize", initialize)
    monkeypatch.setattr(asyncio, "to_thread", _run_inline)

    response = client.post(
        "/api/settings/database/system-tables/initialize",
        json={"recreate": False},
    )

    assert response.status_code == 200
    assert response.json()["data"]["operation"] == "initialized"
    assert captured == {"recreate": False, "confirmation": None, "allow_destructive": False}


def test_initialize_system_tables_preserves_retryable_error(monkeypatch: MonkeyPatch) -> None:
    def initialize(**_kwargs: Any) -> dict[str, Any]:
        raise SystemSchemaError(
            "ORA-00054",
            "ロックが解放されませんでした。",
            status_code=409,
        )

    monkeypatch.setattr(system_schema_manager, "initialize", initialize)
    monkeypatch.setattr(asyncio, "to_thread", _run_inline)

    response = client.post(
        "/api/settings/database/system-tables/initialize",
        json={"recreate": False},
    )

    assert response.status_code == 409
    assert response.headers["retry-after"] == "5"
    assert response.json()["error_code"] == "ORA-00054"


def test_initialize_system_tables_requires_system_tables_manage(
    monkeypatch: MonkeyPatch,
) -> None:
    """初期化・再作成は rag.system_tables.manage が必要（旧 ADMIN 判定の置き換え）。

    状態の参照はシステムテーブルのメニュー権限だけでよい（#658）。
    """
    monkeypatch.setattr(system_schema_manager, "status", _status_payload)
    monkeypatch.setattr(
        system_schema_manager,
        "initialize",
        lambda **_kwargs: {
            **_status_payload(),
            "operation": "no_op",
            "dropped_object_count": 0,
            "created_object_count": 0,
        },
    )
    monkeypatch.setattr(asyncio, "to_thread", _run_inline)
    auth = enable_production_auth(monkeypatch)
    auth.user_with_permissions("db-viewer", ["menu.settings_system_tables"])
    auth.user_with_permissions("table-manager", ["rag.system_tables.manage"])

    viewer = login(client, "db-viewer")
    assert client.get("/api/settings/database/system-tables", headers=viewer).status_code == 200
    forbidden = client.post(
        "/api/settings/database/system-tables/initialize",
        json={"recreate": False},
        headers=viewer,
    )
    assert forbidden.status_code == 403

    manager = login(client, "table-manager")
    allowed = client.post(
        "/api/settings/database/system-tables/initialize",
        json={"recreate": False},
        headers=manager,
    )
    assert allowed.status_code == 200
    assert allowed.json()["data"]["operation"] == "no_op"


_ORPHANED_FOREIGN_KEY = {
    "name": "RAG_CHUNK_SETS_DOCUMENT_FK",
    "table_name": "RAG_CHUNK_SETS",
    "columns": ["DOCUMENT_ID"],
    "referenced_table_name": "RAG_DOCUMENTS",
    "referenced_columns": ["DOCUMENT_ID"],
    "delete_rule": "CASCADE",
    "orphan_rows": 0,
}


def test_status_returns_delete_rule_mismatch_and_disabled_foreign_keys(
    monkeypatch: MonkeyPatch,
) -> None:
    mismatched = {
        **_ORPHANED_FOREIGN_KEY,
        "orphan_rows": 3,
        "current_name": "RAG_CHUNK_SETS_DOC_FK_OLD",
        "current_delete_rule": "NO ACTION",
    }
    monkeypatch.setattr(
        system_schema_manager,
        "status",
        lambda: {
            **_status_payload(),
            "status": "outdated",
            "mismatched_foreign_keys": [mismatched],
            "disabled_foreign_keys": [{**_ORPHANED_FOREIGN_KEY, "name": "RAG_DOC_EXT_DOCUMENT_FK"}],
        },
    )
    monkeypatch.setattr(asyncio, "to_thread", _run_inline)

    data = client.get("/api/settings/database/system-tables").json()["data"]

    assert data["mismatched_foreign_keys"] == [mismatched]
    assert data["disabled_foreign_keys"][0]["name"] == "RAG_DOC_EXT_DOCUMENT_FK"
    assert data["disabled_foreign_keys"][0]["current_delete_rule"] is None


def test_delete_orphaned_rows_returns_result_and_requires_system_tables_manage(
    monkeypatch: MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def delete_orphaned_rows(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            **_status_payload(),
            "operation": "orphans_deleted",
            "deleted_row_count": 240,
            "foreign_key": _ORPHANED_FOREIGN_KEY,
        }

    monkeypatch.setattr(system_schema_manager, "delete_orphaned_rows", delete_orphaned_rows)
    monkeypatch.setattr(asyncio, "to_thread", _run_inline)
    auth = enable_production_auth(monkeypatch)
    auth.user_with_permissions("db-viewer", ["menu.settings_system_tables"])
    auth.user_with_permissions("table-manager", ["rag.system_tables.manage"])
    body = {"constraint_name": "RAG_CHUNK_SETS_DOCUMENT_FK", "expected_orphan_rows": 240}

    viewer = login(client, "db-viewer")
    forbidden = client.post(
        "/api/settings/database/system-tables/orphaned-rows/delete", json=body, headers=viewer
    )
    assert forbidden.status_code == 403
    assert captured == {}

    manager = login(client, "table-manager")
    allowed = client.post(
        "/api/settings/database/system-tables/orphaned-rows/delete", json=body, headers=manager
    )
    assert allowed.status_code == 200
    data = allowed.json()["data"]
    assert (data["operation"], data["deleted_row_count"]) == ("orphans_deleted", 240)
    assert data["foreign_key"]["name"] == "RAG_CHUNK_SETS_DOCUMENT_FK"
    assert captured == {
        "constraint_name": "RAG_CHUNK_SETS_DOCUMENT_FK",
        "expected_orphan_rows": 240,
    }


def test_delete_orphaned_rows_validates_body_and_maps_business_errors(
    monkeypatch: MonkeyPatch,
) -> None:
    def delete_orphaned_rows(**_kwargs: Any) -> dict[str, Any]:
        raise SystemSchemaError(
            "SCHEMA_ORPHAN_ROWS_CHANGED",
            "参照先のない行の件数が確認したときより増えています。",
            status_code=409,
        )

    monkeypatch.setattr(system_schema_manager, "delete_orphaned_rows", delete_orphaned_rows)
    monkeypatch.setattr(asyncio, "to_thread", _run_inline)

    invalid = client.post(
        "/api/settings/database/system-tables/orphaned-rows/delete",
        json={"constraint_name": "RAG_CHUNK_SETS_DOCUMENT_FK", "expected_orphan_rows": -1},
    )
    assert invalid.status_code == 422

    conflict = client.post(
        "/api/settings/database/system-tables/orphaned-rows/delete",
        json={"constraint_name": "RAG_CHUNK_SETS_DOCUMENT_FK", "expected_orphan_rows": 1},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "SCHEMA_ORPHAN_ROWS_CHANGED"
    assert "retry-after" not in conflict.headers


_DESTRUCTIVE_MIGRATION = {
    "name": "20260930_005_retire_standard_engine_objects",
    "description": "rag_agent_memories などを削除します。",
}


def test_system_tables_status_returns_pending_destructive_migrations(
    monkeypatch: MonkeyPatch,
) -> None:
    """データを消す未適用の migration を状態に出す（#619）。旧い形の応答では空の一覧。"""
    monkeypatch.setattr(
        system_schema_manager,
        "status",
        lambda: {
            **_status_payload(),
            "status": "outdated",
            "pending_versions": [_DESTRUCTIVE_MIGRATION["name"]],
            "pending_destructive_migrations": [_DESTRUCTIVE_MIGRATION],
        },
    )
    monkeypatch.setattr(asyncio, "to_thread", _run_inline)

    response = client.get("/api/settings/database/system-tables")

    assert response.status_code == 200
    assert response.json()["data"]["pending_destructive_migrations"] == [_DESTRUCTIVE_MIGRATION]

    monkeypatch.setattr(system_schema_manager, "status", _status_payload)
    response = client.get("/api/settings/database/system-tables")
    assert response.json()["data"]["pending_destructive_migrations"] == []


@contextmanager
def _null_connection() -> Iterator[None]:
    yield None


def test_initialize_system_tables_requires_destructive_approval(monkeypatch: MonkeyPatch) -> None:
    """承認（allow_destructive）の無い作成・更新は 409 で止まり、承認があれば進む（#619）。"""
    calls: list[dict[str, Any]] = []
    status = {
        **_status_payload(),
        "status": "outdated",
        "pending_versions": [_DESTRUCTIVE_MIGRATION["name"]],
        "pending_destructive_migrations": [_DESTRUCTIVE_MIGRATION],
    }
    monkeypatch.setattr(system_schema_manager, "status", lambda: status)

    def initialize_on(_connection: Any, _owner: str, *, recreate: bool) -> dict[str, Any]:
        calls.append({"recreate": recreate})
        return {
            **_status_payload(),
            "operation": "migrated",
            "dropped_object_count": 3,
            "created_object_count": 0,
        }

    # lease・DB 接続は fake にし、manager の承認の判定だけを実物で通す。
    monkeypatch.setattr(system_schema_manager, "_ensure_control_schema", lambda: None)
    monkeypatch.setattr(system_schema_manager, "_claim_lease", lambda *_args: None)
    monkeypatch.setattr(system_schema_manager, "_initialize_on", initialize_on)
    monkeypatch.setattr(system_schema_manager, "_connection_factory", _null_connection)
    monkeypatch.setattr(asyncio, "to_thread", _run_inline)

    refused = client.post(
        "/api/settings/database/system-tables/initialize",
        json={"recreate": False},
    )

    assert refused.status_code == 409
    assert refused.json()["error_code"] == "SCHEMA_DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED"
    assert _DESTRUCTIVE_MIGRATION["name"] in refused.json()["error_messages"][0]
    assert calls == []

    approved = client.post(
        "/api/settings/database/system-tables/initialize",
        json={"recreate": False, "allow_destructive": True},
    )

    assert approved.status_code == 200
    assert approved.json()["data"]["operation"] == "migrated"
    assert calls == [{"recreate": False}]
