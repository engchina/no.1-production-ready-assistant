"""文書の削除の後始末の記録（#2452）の SQL と、記録の対象の形を確かめる（DB は使わない）。"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import Any

import anyio
import pytest

from app.clients.document_cleanup_store import (
    DocumentCleanupStore,
    DocumentCleanupTargets,
)
from app.rag.request_context import (
    AuditRequestContext,
    reset_audit_request_context,
    set_audit_request_context,
)


class CapturingOracle:
    """``_fetch_all`` と ``_run_transaction`` の SQL を記録する OracleClient の代わり。"""

    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self.rows = rows or []
        self.fetches: list[tuple[str, Mapping[str, object]]] = []
        self.executes: list[tuple[str, Mapping[str, object]]] = []

    async def _fetch_all(
        self, statement: str, binds: Mapping[str, object] | None = None
    ) -> list[dict[str, object]]:
        self.fetches.append((statement, dict(binds or {})))
        return self.rows

    async def _run_transaction(
        self, operation: Callable[[Any], object], *, retry_deadlocks: bool = False
    ) -> object:
        executes = self.executes

        class Cursor:
            rowcount = 1

            def setinputsizes(self, **kwargs: object) -> None:
                _ = kwargs

            def execute(self, statement: str, binds: Mapping[str, object]) -> None:
                executes.append((statement, dict(binds)))

            def close(self) -> None:
                return None

        class Connection:
            def cursor(self) -> Cursor:
                return Cursor()

        return operation(Connection())


@pytest.fixture
def audit_context() -> Iterator[Callable[[AuditRequestContext], None]]:
    tokens: list[Any] = []

    def apply(context: AuditRequestContext) -> None:
        tokens.append(set_audit_request_context(context))

    yield apply
    for token in reversed(tokens):
        reset_audit_request_context(token)


def test_targets_round_trip_through_json() -> None:
    targets = DocumentCleanupTargets(
        source_uri="local://uploaded/a/source.txt",
        artifact_uris=("local://artifacts/x.json",),
        prefixes=("artifacts/extractions/doc-1/",),
    )

    assert DocumentCleanupTargets.from_json(targets.to_json()) == targets
    assert DocumentCleanupTargets.from_json('{"artifact_uris": [1, ""]}').is_empty
    assert DocumentCleanupTargets().is_empty
    # 以前の記録の `include_oci`（OCI の prefix も消す。#11 で削除）は読み捨てる。
    assert DocumentCleanupTargets.from_json({**targets.to_json(), "include_oci": True}) == targets


def test_find_for_document_is_limited_to_the_deleting_user(
    audit_context: Callable[[AuditRequestContext], None],
) -> None:
    """system_admin 以外は、削除した利用者の記録だけを再試行できる。"""
    oracle = CapturingOracle()
    store = DocumentCleanupStore(oracle)  # type: ignore[arg-type]
    audit_context(AuditRequestContext(tenant_id_hash="t" * 64, user_id_hash="u" * 64))

    assert anyio.run(store.find_for_document, "doc-1") is None

    statement, binds = oracle.fetches[-1]
    assert "user_id_hash = :cleanup_user_id_hash" in statement
    assert "tenant_id_hash = :tenant_id_hash" in statement
    assert binds["cleanup_user_id_hash"] == "u" * 64
    assert binds["tenant_id_hash"] == "t" * 64
    assert binds["document_id"] == "doc-1"


def test_find_for_document_lets_system_admin_retry_any_user(
    audit_context: Callable[[AuditRequestContext], None],
) -> None:
    oracle = CapturingOracle(
        rows=[
            {
                "cleanup_id": "c-1",
                "document_id": "doc-1",
                "file_name": "a.txt",
                "targets": '{"source_uri": "local://uploaded/a/a.txt"}',
                "attempt_count": 2,
            }
        ]
    )
    store = DocumentCleanupStore(oracle)  # type: ignore[arg-type]
    audit_context(AuditRequestContext(user_id_hash="a" * 64, all_users_visible=True))

    task = anyio.run(store.find_for_document, "doc-1")

    assert task is not None
    assert task.targets.source_uri == "local://uploaded/a/a.txt"
    assert task.attempt_count == 2
    statement, binds = oracle.fetches[-1]
    assert "user_id_hash" not in statement
    assert "cleanup_user_id_hash" not in binds


def test_save_progress_deletes_finished_record_and_keeps_remaining_targets() -> None:
    oracle = CapturingOracle()
    store = DocumentCleanupStore(oracle)  # type: ignore[arg-type]

    anyio.run(lambda: store.save_progress("c-1", DocumentCleanupTargets(), error_type=None))
    remaining = DocumentCleanupTargets(prefixes=("artifacts/extractions/doc-1/",))
    anyio.run(lambda: store.save_progress("c-2", remaining, error_type="OSError"))

    (delete_sql, delete_binds), (update_sql, update_binds) = oracle.executes
    assert delete_sql.startswith("DELETE FROM rag_document_cleanups")
    assert delete_binds == {"cleanup_id": "c-1"}
    assert "attempt_count = attempt_count + 1" in update_sql
    assert update_binds["last_error_type"] == "OSError"
    assert DocumentCleanupTargets.from_json(update_binds["targets"]) == remaining


def test_referenced_uris_checks_all_documents_without_tenant_scope(
    audit_context: Callable[[AuditRequestContext], None],
) -> None:
    """別の文書（ほかの tenant を含む）が参照する URI は消さないため、範囲で絞らずに確かめる。"""
    oracle = CapturingOracle(rows=[{"uri": "local://b"}])
    store = DocumentCleanupStore(oracle)  # type: ignore[arg-type]
    audit_context(AuditRequestContext(tenant_id_hash="t" * 64, user_id_hash="u" * 64))

    referenced = anyio.run(store.referenced_uris, ["local://a", "local://b", ""])

    assert referenced == {"local://b"}
    statement, binds = oracle.fetches[-1]
    assert "tenant_id_hash" not in statement
    assert sorted(str(value) for value in binds.values()) == ["local://a", "local://b"]
    assert "rag_ingestion_segments" in statement
    assert anyio.run(store.referenced_uris, []) == set()
    assert len(oracle.fetches) == 1
