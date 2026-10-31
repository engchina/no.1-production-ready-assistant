"""DB 管理の操作の監査の保存先（#2264）。DDL は migration 023（`NL2SQL_ADMIN_AUDIT_LOG`）。

DB 管理チャット・管理 SQL を実行・テーブル / ビュー / データの管理などの実行を 1 行ずつ残す。
以前は状態の文書（`NL2SQL_STATE_DOCUMENTS`）に直近 200 件だけを持っていたが、
件数の上限なく表に残す。
Oracle の永続化を使わない構成（テスト・ローカルの memory）は、同じ契約の memory に残す。
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

AdminAuditOutcome = Literal["success", "partial", "failure", "blocked"]

# 列の長さ（migration 023）。超える値は切り詰めて保存する（SQL の全文は CLOB なので切らない）。
_TARGET_MAX = 512
_STATEMENT_TYPES_MAX = 4000
_REASON_MAX = 2000
_ERROR_MESSAGE_MAX = 4000
_MEMORY_MAX_RECORDS = 10_000


class AdminAuditRecord(BaseModel):
    """監査の 1 行。"""

    audit_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # 実行した画面・経路（`db_admin_chat` / `admin_sql` / `data_preparation`）。
    source: str
    operation: str
    target: str = ""
    actor_user_uuid: str = ""
    actor_is_system_admin: bool = False
    outcome: AdminAuditOutcome
    executed: bool = False
    confirmation_given: bool = False
    committed: bool = False
    rolled_back: bool = False
    statement_count: int = 0
    success_count: int = 0
    statement_types: list[str] = Field(default_factory=list)
    affected_rows: int | None = None
    duration_ms: int | None = None
    reason: str = ""
    error_message: str = ""
    sql_text: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)


def _clip(value: str, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


class AdminAuditStore:
    """Oracle の表（`NL2SQL_ADMIN_AUDIT_LOG`）か memory に監査を残す。"""

    def __init__(self, connection: Callable[[], AbstractContextManager[Any]] | None = None):
        self.connection = connection
        self._lock = threading.RLock()
        self._records: list[AdminAuditRecord] = []

    def append(self, record: AdminAuditRecord) -> None:
        if self.connection is None:
            with self._lock:
                self._records.append(record.model_copy(deep=True))
                # memory は上限を持つ（プロセスの中だけの記録。永続の監査は Oracle の表）。
                if len(self._records) > _MEMORY_MAX_RECORDS:
                    self._records = self._records[-_MEMORY_MAX_RECORDS:]
            return
        # 時刻は DB の SYSTIMESTAMP（列の既定）で残す（python-oracledb は datetime を秒までの
        # DATE で渡すので、同じ秒の行の並びが崩れる）。
        with self.connection() as conn, conn.cursor() as cursor:
            if hasattr(cursor, "setinputsizes"):
                # SQL の全文は 32KB を超えることがあるので CLOB で渡す。
                clob = _clob_type()
                cursor.setinputsizes(sql_text=clob, detail_json=clob)
            cursor.execute(
                "INSERT INTO NL2SQL_ADMIN_AUDIT_LOG ("
                "AUDIT_ID, SOURCE, OPERATION, TARGET, ACTOR_USER_UUID, "
                "ACTOR_IS_SYSTEM_ADMIN, OUTCOME, EXECUTED, CONFIRMATION_GIVEN, COMMITTED, "
                "ROLLED_BACK, STATEMENT_COUNT, SUCCESS_COUNT, STATEMENT_TYPES, AFFECTED_ROWS, "
                "DURATION_MS, REASON, ERROR_MESSAGE, SQL_TEXT, DETAIL_JSON) VALUES ("
                ":audit_id, :source, :operation, :target, :actor_user_uuid, "
                ":actor_is_system_admin, :outcome, :executed, :confirmation_given, :committed, "
                ":rolled_back, :statement_count, :success_count, :statement_types, "
                ":affected_rows, :duration_ms, :reason, :error_message, :sql_text, :detail_json)",
                self._binds(record),
            )
            conn.commit()

    @staticmethod
    def _binds(record: AdminAuditRecord) -> dict[str, Any]:
        return {
            "audit_id": record.audit_id,
            "source": _clip(record.source, 40),
            "operation": _clip(record.operation, 80),
            "target": _clip(record.target, _TARGET_MAX),
            "actor_user_uuid": _clip(record.actor_user_uuid, 64),
            "actor_is_system_admin": int(record.actor_is_system_admin),
            "outcome": record.outcome,
            "executed": int(record.executed),
            "confirmation_given": int(record.confirmation_given),
            "committed": int(record.committed),
            "rolled_back": int(record.rolled_back),
            "statement_count": record.statement_count,
            "success_count": record.success_count,
            "statement_types": _clip(",".join(record.statement_types), _STATEMENT_TYPES_MAX),
            "affected_rows": record.affected_rows,
            "duration_ms": record.duration_ms,
            "reason": _clip(record.reason, _REASON_MAX),
            "error_message": _clip(record.error_message, _ERROR_MESSAGE_MAX),
            "sql_text": record.sql_text,
            "detail_json": json.dumps(record.detail, ensure_ascii=False, default=str),
        }

    def list_recent(self, limit: int = 100) -> list[AdminAuditRecord]:
        """新しい順に返す（テスト・調査用。画面には出さない）。"""

        bounded = max(1, min(int(limit), 1000))
        if self.connection is None:
            with self._lock:
                return [item.model_copy(deep=True) for item in reversed(self._records)][:bounded]
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT AUDIT_ID, CREATED_AT, SOURCE, OPERATION, TARGET, ACTOR_USER_UUID, "
                "ACTOR_IS_SYSTEM_ADMIN, OUTCOME, EXECUTED, CONFIRMATION_GIVEN, COMMITTED, "
                "ROLLED_BACK, STATEMENT_COUNT, SUCCESS_COUNT, STATEMENT_TYPES, AFFECTED_ROWS, "
                "DURATION_MS, REASON, ERROR_MESSAGE, SQL_TEXT, DETAIL_JSON "
                "FROM NL2SQL_ADMIN_AUDIT_LOG ORDER BY CREATED_AT DESC, AUDIT_ID DESC "
                "FETCH FIRST :limit ROWS ONLY",
                {"limit": bounded},
            )
            return [self._decode(row) for row in cursor.fetchall()]

    @staticmethod
    def _decode(row: Any) -> AdminAuditRecord:
        def text(value: Any) -> str:
            if value is None:
                return ""
            return str(value.read() if hasattr(value, "read") else value)

        detail_text = text(row[20])
        detail = json.loads(detail_text) if detail_text else {}
        types = text(row[14])
        return AdminAuditRecord(
            audit_id=text(row[0]),
            created_at=row[1],
            source=text(row[2]),
            operation=text(row[3]),
            target=text(row[4]),
            actor_user_uuid=text(row[5]),
            actor_is_system_admin=bool(row[6]),
            outcome=text(row[7]),
            executed=bool(row[8]),
            confirmation_given=bool(row[9]),
            committed=bool(row[10]),
            rolled_back=bool(row[11]),
            statement_count=int(row[12] or 0),
            success_count=int(row[13] or 0),
            statement_types=[item for item in types.split(",") if item],
            affected_rows=None if row[15] is None else int(row[15]),
            duration_ms=None if row[16] is None else int(row[16]),
            reason=text(row[17]),
            error_message=text(row[18]),
            sql_text=text(row[19]),
            detail=detail if isinstance(detail, dict) else {},
        )


def _clob_type() -> Any:
    """CLOB の bind の型（python-oracledb の `DB_TYPE_CLOB`）。"""

    import oracledb

    return oracledb.DB_TYPE_CLOB
