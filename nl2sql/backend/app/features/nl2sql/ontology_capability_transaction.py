"""DATA USER の transaction で物理更新・監査・幂等記録を確定する。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .ontology_definition_data_validation import quote_identifier
from .ontology_service import OntologyVersionConflictError
from .ontology_store import canonical_json, next_versioned_document


class OracleActionTransaction:
    def __init__(
        self,
        service: Any,
        profile_id: str,
        cursor: Any,
        package: str,
        identity: str,
        key_id: str,
        preview: dict[str, Any],
    ) -> None:
        self.service, self.profile_id, self.cursor, self.package = (
            service,
            profile_id,
            cursor,
            package,
        )
        self.identity, self.key_id, self.preview = identity, key_id, preview
        self.existing: dict[str, Any] | None = None

    def save(self, result: dict[str, Any]) -> None:
        for identity, kind, value in (
            (self.identity, "ontology_action_execution", result),
            (
                self.key_id,
                "ontology_action_key",
                {"preview_id": self.preview["id"], "actor": self.preview["actor"]},
            ),
        ):
            record = next_versioned_document(
                self.service.artifact(self.profile_id, identity, kind, value),
                current=None,
                expected_etag=None,
            )
            self.cursor.callproc(
                f"{self.package}.SAVE_RECORD",
                [identity, record["content_hash"], record["etag"], kind, canonical_json(record)],
            )


def _read_clob(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    result: dict[str, Any] = json.loads(value.read() if hasattr(value, "read") else value)
    return result


@contextmanager
def action_transaction(
    service: Any,
    profile_id: str,
    binding: dict[str, Any],
    preview: dict[str, Any],
    identity: str,
    key_id: str,
    actor: Any,
) -> Iterator[Any]:
    # test/runtime adapter は明示的に注入する。Oracle 不達を InMemory 実行へ縮退しない。
    factory = getattr(service.runtime, "capability_transaction_factory", None)
    if factory is not None:
        with factory(service, profile_id, binding, preview, identity, key_id, actor) as transaction:
            yield transaction
        return
    import oracledb

    adapter = service._adapter()
    package = f"{quote_identifier(adapter.settings.oracle_user)}.NL2SQL_ONT_ACTION_TX"
    with adapter.user_data_connection() as connection, connection.cursor() as cursor:
        transaction = OracleActionTransaction(
            service, profile_id, cursor, package, identity, key_id, preview
        )
        existing, key = cursor.var(oracledb.DB_TYPE_CLOB), cursor.var(oracledb.DB_TYPE_CLOB)
        try:
            cursor.callproc(
                f"{package}.LOCK_SCOPE",
                [
                    profile_id,
                    preview["actor"],
                    preview["head_etag"],
                    binding["artifact_id"],
                    binding["etag"],
                    identity,
                    key_id,
                    existing,
                    key,
                ],
            )
            key_record = _read_clob(key.getvalue())
            if key_record and json.loads(key_record["content"])["preview_id"] != preview["id"]:
                raise OntologyVersionConflictError(
                    "IDEMPOTENCY_KEY_REUSED", "このキーは別の確認に使用されています。"
                )
            record = _read_clob(existing.getvalue())
            if record:
                transaction.existing = json.loads(record["content"])
                if (
                    transaction.existing["actor"] != preview["actor"]
                    or transaction.existing["preview_id"] != preview["id"]
                ):
                    raise OntologyVersionConflictError(
                        "ACTION_SCOPE_CHANGED", "実行記録の確認対象が一致しません。"
                    )
            yield transaction
            try:
                connection.commit()
            except Exception as exc:
                raise OntologyVersionConflictError(
                    "ACTION_OUTCOME_UNKNOWN",
                    "コミット結果を確認できません。"
                    "同じプレビュー ID で実行記録を再確認してください。",
                ) from exc
        except Exception as exc:
            connection.rollback()
            if "ORA-20041" in str(exc):
                raise OntologyVersionConflictError(
                    "ACTION_PREVIEW_STALE",
                    "公開版または binding が更新されました。再プレビューしてください。",
                ) from exc
            raise
