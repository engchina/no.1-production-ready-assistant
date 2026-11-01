"""文書の削除の後始末（原本・成果物・成果物の prefix の削除）の記録の Oracle の保存（#2452）。

文書の行を消すと、原本・成果物の保存先の参照も消える。保存先の削除が一時的な障害で失敗しても
後から消せるよう、文書の行の削除と同じ transaction で、消す対象（原本の URI・成果物の URI・
成果物の prefix）を ``rag_document_cleanups`` に 1 行で残す。

- 行があることが「後始末が残っている」こと。すべて消せたら行を消す。一部が残れば、残った対象
  だけに書き換え、試した回数と最後の失敗の種類（例外の型の名前）を残す。
- 行には保存先の参照だけを持ち、原本の内容・認証情報は持たない。
- 文書の削除の再試行（同じ文書の DELETE）で後始末をやり直すときは、削除した利用者
  （system_admin は全員の分）の、同じ tenant の行だけを扱う。取込ワーカーの定期の後始末は
  tenant・利用者で絞らない。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import uuid4

from app.clients.oracle import (
    OracleClient,
    OracleConnectionProtocol,
    _execute,
    _json_bind,
    _json_input_sizes,
    _json_loads,
    _oracle_tenant_predicate,
    _with_tenant_bind,
    delete_document_rows,
)
from app.rag.request_context import current_audit_request_context

DOCUMENT_CLEANUP_TABLE = "rag_document_cleanups"


def oracle_document_cleanup_schema_sql(table_name: str = DOCUMENT_CLEANUP_TABLE) -> str:
    """文書の削除の後始末の記録の表の DDL（#2452）。

    文書の行は消えた後なので、``document_id`` に FK は付けない。``targets`` は残っている対象
    （``DocumentCleanupTargets``）。
    """
    return f"""
CREATE TABLE {table_name} (
    cleanup_id      VARCHAR2(64) PRIMARY KEY,
    document_id     VARCHAR2(64) NOT NULL,
    file_name       VARCHAR2(512) NOT NULL,
    tenant_id_hash  CHAR(64),
    user_id_hash    CHAR(64),
    targets         JSON NOT NULL,
    attempt_count   NUMBER(10) DEFAULT 0 NOT NULL,
    last_error_type VARCHAR2(128),
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE INDEX {table_name}_document_idx
    ON {table_name} (document_id);

CREATE INDEX {table_name}_updated_idx
    ON {table_name} (updated_at);
""".strip()


@dataclass(frozen=True, slots=True)
class DocumentCleanupTargets:
    """後始末で消す保存先の対象。"""

    source_uri: str | None = None
    artifact_uris: tuple[str, ...] = ()
    prefixes: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.source_uri and not self.artifact_uris and not self.prefixes

    def to_json(self) -> dict[str, object]:
        return {
            "source_uri": self.source_uri,
            "artifact_uris": list(self.artifact_uris),
            "prefixes": list(self.prefixes),
        }

    @classmethod
    def from_json(cls, value: object) -> DocumentCleanupTargets:
        payload = _json_loads(value)
        source_uri = payload.get("source_uri")
        return cls(
            source_uri=source_uri if isinstance(source_uri, str) and source_uri else None,
            artifact_uris=_str_tuple(payload.get("artifact_uris")),
            prefixes=_str_tuple(payload.get("prefixes")),
        )


@dataclass(frozen=True, slots=True)
class DocumentCleanupTask:
    """後始末の記録の 1 行。"""

    cleanup_id: str
    document_id: str
    file_name: str
    targets: DocumentCleanupTargets
    attempt_count: int = 0


def _str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _task_from_row(row: Mapping[str, object]) -> DocumentCleanupTask:
    return DocumentCleanupTask(
        cleanup_id=str(row["cleanup_id"]),
        document_id=str(row["document_id"]),
        file_name=str(row.get("file_name") or ""),
        targets=DocumentCleanupTargets.from_json(row.get("targets")),
        attempt_count=int(row.get("attempt_count") or 0),  # type: ignore[call-overload]
    )


def _placeholders(prefix: str, values: Sequence[str]) -> tuple[str, dict[str, object]]:
    names = [f"{prefix}_{index}" for index in range(len(values))]
    return ", ".join(f":{name}" for name in names), dict(zip(names, values, strict=True))


class DocumentCleanupStore:
    """後始末の記録の読み書き（OracleClient の接続と transaction を使う）。"""

    def __init__(self, oracle: OracleClient | None = None) -> None:
        self._oracle = oracle or OracleClient()

    async def delete_document_with_cleanup(
        self, document_id: str, file_name: str, targets: DocumentCleanupTargets
    ) -> DocumentCleanupTask | None:
        """文書の行を消し、同じ transaction で後始末の記録を残す。文書が無ければ None。

        実行中の取込 job があれば ``DocumentDeleteBlockedByRunningIngestionError``（何も消さない）。
        """
        context = current_audit_request_context()
        task = DocumentCleanupTask(
            cleanup_id=uuid4().hex,
            document_id=document_id,
            file_name=file_name,
            targets=targets,
        )

        def operation(connection: OracleConnectionProtocol) -> DocumentCleanupTask | None:
            if not delete_document_rows(connection, document_id):
                return None
            if targets.is_empty:
                return task
            _execute(
                connection,
                f"""
                INSERT INTO {DOCUMENT_CLEANUP_TABLE} (
                    cleanup_id, document_id, file_name, tenant_id_hash, user_id_hash, targets
                ) VALUES (
                    :cleanup_id, :document_id, :file_name, :tenant_id_hash, :user_id_hash,
                    :targets
                )
                """,
                {
                    "cleanup_id": task.cleanup_id,
                    "document_id": document_id,
                    "file_name": file_name,
                    "tenant_id_hash": context.tenant_id_hash,
                    "user_id_hash": context.user_id_hash,
                    "targets": _json_bind(targets.to_json()),
                },
                input_sizes=_json_input_sizes("targets"),
            )
            return task

        return await self._oracle._run_transaction(operation, retry_deadlocks=True)

    async def find_for_document(self, document_id: str) -> DocumentCleanupTask | None:
        """削除の再試行で、今の利用者が扱える文書の後始末の記録を返す。

        同じ tenant で、削除した利用者の行だけ（system_admin は全員の分）。
        """
        context = current_audit_request_context()
        predicates = [_oracle_tenant_predicate()]
        binds: dict[str, object] = {"document_id": document_id}
        if not context.all_users_visible:
            if context.user_id_hash is None:
                predicates.append("user_id_hash IS NULL")
            else:
                predicates.append("user_id_hash = :cleanup_user_id_hash")
                binds["cleanup_user_id_hash"] = context.user_id_hash
        rows = await self._oracle._fetch_all(
            f"""
            SELECT cleanup_id, document_id, file_name, targets, attempt_count
            FROM {DOCUMENT_CLEANUP_TABLE}
            WHERE document_id = :document_id
              AND {" AND ".join(predicates)}
            ORDER BY created_at ASC
            FETCH FIRST 1 ROWS ONLY
            """,
            _with_tenant_bind(binds),
        )
        return _task_from_row(rows[0]) if rows else None

    async def list_due(self, *, limit: int, min_age_seconds: float) -> list[DocumentCleanupTask]:
        """取込ワーカーの定期の後始末の対象（最後に試してから ``min_age_seconds`` 経った行）。

        削除の要求の中で後始末をしている最中の行と重ならないよう、新しい行は取らない。
        """
        rows = await self._oracle._fetch_all(
            f"""
            SELECT cleanup_id, document_id, file_name, targets, attempt_count
            FROM {DOCUMENT_CLEANUP_TABLE}
            WHERE updated_at <= SYSTIMESTAMP - NUMTODSINTERVAL(:min_age_seconds, 'SECOND')
            ORDER BY updated_at ASC
            FETCH FIRST :limit ROWS ONLY
            """,
            {"min_age_seconds": float(min_age_seconds), "limit": int(limit)},
        )
        return [_task_from_row(row) for row in rows]

    async def save_progress(
        self,
        cleanup_id: str,
        remaining: DocumentCleanupTargets,
        *,
        error_type: str | None,
    ) -> None:
        """後始末を 1 回試した結果を残す。残りが無ければ行を消す。"""

        def operation(connection: OracleConnectionProtocol) -> None:
            if remaining.is_empty:
                _execute(
                    connection,
                    f"DELETE FROM {DOCUMENT_CLEANUP_TABLE} WHERE cleanup_id = :cleanup_id",
                    {"cleanup_id": cleanup_id},
                )
                return
            _execute(
                connection,
                f"""
                UPDATE {DOCUMENT_CLEANUP_TABLE}
                SET targets = :targets,
                    attempt_count = attempt_count + 1,
                    last_error_type = :last_error_type,
                    updated_at = SYSTIMESTAMP
                WHERE cleanup_id = :cleanup_id
                """,
                {
                    "cleanup_id": cleanup_id,
                    "targets": _json_bind(remaining.to_json()),
                    "last_error_type": (error_type or None) and error_type[:128],
                },
                input_sizes=_json_input_sizes("targets"),
            )

        await self._oracle._run_transaction(operation)

    async def referenced_uris(self, uris: Sequence[str]) -> set[str]:
        """残っている文書（tenant を問わない）が参照している保存先の URI を返す。

        後始末で別の文書の原本・成果物を消さないための確認に使う。
        """
        values = sorted({uri for uri in uris if uri})
        if not values:
            return set()
        in_list, binds = _placeholders("cleanup_uri", values)
        rows = await self._oracle._fetch_all(
            f"""
            SELECT object_storage_path AS uri
            FROM rag_documents
            WHERE object_storage_path IN ({in_list})
            UNION
            SELECT JSON_VALUE(preprocess_artifact, '$.object_storage_path') AS uri
            FROM rag_documents
            WHERE JSON_VALUE(preprocess_artifact, '$.object_storage_path') IN ({in_list})
            UNION
            SELECT artifact_path AS uri
            FROM rag_ingestion_segments
            WHERE artifact_path IN ({in_list})
            """,
            binds,
        )
        return {str(row["uri"]) for row in rows if row.get("uri")}

    async def document_exists(self, document_id: str) -> bool:
        """同じ ID の文書の行が（tenant を問わず）あるか。あれば文書の prefix は消さない。"""
        rows = await self._oracle._fetch_all(
            "SELECT 1 AS found FROM rag_documents WHERE document_id = :document_id",
            {"document_id": document_id},
        )
        return bool(rows)
