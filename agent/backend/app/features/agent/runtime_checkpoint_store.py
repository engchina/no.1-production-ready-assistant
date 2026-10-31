"""Run・業務 Agent の保存先（`AGENT_RUNTIME_CHECKPOINTS` の head と `AGENT_RUNTIME_ENTITIES`）。

#1939 から、Run・業務 Agent を 1 件 = 1 行（`AGENT_RUNTIME_ENTITIES`）で持ち、変わったものだけを
書く（以前は全 Run・業務 Agent の snapshot を 1 行の JSON で書き戻し、書き込みの時間が Run の総数に
比例して伸びた）。checkpoint の行（`AGENT_RUNTIME_CHECKPOINTS`）は保存先の head で、次を持つ。

- `VERSION_NO`: 書くたびに 1 つ増やす版。書くプロセスは head の行を lock（`SELECT ... FOR UPDATE`）
  してから書くので、同じ保存先に書くプロセス（worktree の 2 つ目の backend・runtime-dispatcher・
  CLI。#1916）の書き込みは版の順に並ぶ。
- `WRITER_ID`: 最後に書いたプロセス。
- `SNAPSHOT_JSON`: 小さな header（形式 `storage = "entities"`・読み込みで退避した記録
  `quarantined`）。Run・業務 Agent は持たない。

Run・業務 Agent の行の `CHANGE_VERSION` は、その行を書いたときの head の版。書くプロセスは head を
lock した後に、前回の同期の版より新しい行（別のプロセスが書いた Run・業務 Agent）を読み、自分が
変えていないものは保存先の値にそろえ、変えたものは自分の値で書く（両方が同じ 1 件を変えていたときは
このプロセスの値が勝ち、ログに残す。#1916 と同じ規則）。1 件の削除は行を消さずに `ENTITY_JSON` を
NULL にする（別のプロセスが削除を知る）。

migration 20261015_016 が、以前の 1 行の snapshot を行に移す（元の snapshot は
`ENTITY_KIND = 'legacy_snapshot'` の行に残す）。移す前の形の checkpoint は読まない
（`CheckpointMigrationRequired`。業務の API は 503 でシステムテーブルの更新へ案内する）。
"""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib import import_module
from typing import Any
from uuid import uuid4

# このプロセスを表す ID（`WRITER_ID`。ホスト・pid・起動ごとの乱数）。
WRITER_ID = f"{socket.gethostname()[:120]}:{os.getpid()}:{uuid4().hex[:8]}"

ENTITY_TABLE = "AGENT_RUNTIME_ENTITIES"
# header の形式（`SNAPSHOT_JSON` の `storage`）。
STORAGE_ENTITIES = "entities"
SNAPSHOT_VERSION = "agent-control-plane.snapshot.v2"
RUN = "run"
AGENT = "agent"
ENTITY_KINDS: tuple[str, ...] = (RUN, AGENT)
# 移す前の形の snapshot の版（`AgentRuntimeSnapshot.version`）。
LEGACY_SNAPSHOT_VERSIONS = frozenset({"agent-runtime.snapshot.v1", SNAPSHOT_VERSION})
# migration 016 が残す、移す前の snapshot（読み込みでは読まない）。
LEGACY_SNAPSHOT_KIND = "legacy_snapshot"

EntityKey = tuple[str, str]


class CheckpointMigrationRequired(Exception):  # noqa: N818 - 例外ではなく「移す前の形」という状態
    """保存先が Run ごとの行に移す前の形（migration 015 / 016 を適用していない）。"""


@dataclass(frozen=True, slots=True)
class CheckpointHead:
    """head の行（header の JSON・版・最後に書いたプロセス）。"""

    header: dict[str, Any]
    version: int
    writer_id: str | None = None


@dataclass(frozen=True, slots=True)
class EntityRow:
    kind: str
    entity_id: str
    # None は削除の印（tombstone）。
    entity_json: str | None
    change_version: int

    @property
    def key(self) -> EntityKey:
        return (self.kind, self.entity_id)


def is_missing_column_error(exc: Exception) -> bool:
    text = str(exc)
    return "ORA-00904" in text or "invalid identifier" in text


def is_missing_table_error(exc: Exception) -> bool:
    text = str(exc)
    return "ORA-00942" in text or "table or view does not exist" in text


def is_unique_violation(exc: Exception) -> bool:
    return "ORA-00001" in str(exc)


def header_json(*, quarantined: list[Any]) -> str:
    return json.dumps(
        {
            "version": SNAPSHOT_VERSION,
            "storage": STORAGE_ENTITIES,
            "quarantined": quarantined,
        },
        ensure_ascii=False,
    )


def read_head(
    cursor: Any, table: str, checkpoint_key: str, *, for_update: bool = False
) -> CheckpointHead | None:
    """head の行（無ければ None）。

    移す前の形（全体の snapshot。`version` はあるが `storage` が無い）なら
    `CheckpointMigrationRequired`、JSON として読めない・形が違うなら ValueError。移す前の形の行は
    全 Run の snapshot（大きな CLOB）なので、形式を先に確かめて本文は読まない。
    """
    lock = " FOR UPDATE" if for_update else ""
    query = (
        "SELECT CASE WHEN JSON_VALUE(snapshot_json, '$.storage') = 'entities' "
        "THEN snapshot_json END, JSON_VALUE(snapshot_json, '$.storage'), version_no, writer_id, "
        "JSON_VALUE(snapshot_json, '$.version') "
        f"FROM {table} WHERE checkpoint_key = :checkpoint_key{lock}"  # nosec B608 - 固定の表名
    )
    try:
        cursor.execute(query, checkpoint_key=checkpoint_key)
    except Exception as exc:
        if is_missing_column_error(exc):
            raise CheckpointMigrationRequired(str(exc)) from exc
        raise
    row = cursor.fetchone()
    if row is None:
        return None
    if row[1] != STORAGE_ENTITIES or row[0] is None:
        if row[1] is None and row[4] in LEGACY_SNAPSHOT_VERSIONS:
            raise CheckpointMigrationRequired(f"checkpoint の形式: snapshot {row[4]}")
        raise ValueError(f"checkpoint の形式が読めません（storage: {row[1]!r}）")
    # CLOB は接続を閉じる前に読む（閉じた後に読むと DPY-1001。#765）。
    text = lob_to_text(row[0])
    try:
        header = json.loads(text)
    except ValueError as exc:
        raise ValueError("checkpoint の header が JSON として読めません") from exc
    if not isinstance(header, dict):
        raise ValueError("checkpoint の header が JSON の object ではありません")
    return CheckpointHead(
        header=header,
        version=int(row[2]) if row[2] is not None else 0,
        writer_id=str(row[3]) if row[3] is not None else None,
    )


def insert_head(cursor: Any, table: str, checkpoint_key: str, text: str) -> bool:
    """head の行を版 0 で作る（別のプロセスが先に作っていたら False）。"""
    cursor.setinputsizes(snapshot_json=_clob_type())
    try:
        cursor.execute(
            f"""
            INSERT INTO {table} (
                checkpoint_key, snapshot_json, version_no, writer_id, updated_at
            ) VALUES (
                :checkpoint_key, :snapshot_json, 0, :writer_id, SYSTIMESTAMP
            )
            """,  # nosec B608 - 固定の表名
            checkpoint_key=checkpoint_key,
            snapshot_json=text,
            writer_id=WRITER_ID,
        )
    except Exception as exc:
        if is_unique_violation(exc):
            return False
        raise
    return True


def update_head(
    cursor: Any, table: str, checkpoint_key: str, *, version: int, text: str | None
) -> None:
    """lock した head の版を進める（`text` があれば header も書き換える）。"""
    if text is None:
        cursor.execute(
            f"""
            UPDATE {table}
            SET version_no = :version_no, writer_id = :writer_id, updated_at = SYSTIMESTAMP
            WHERE checkpoint_key = :checkpoint_key
            """,  # nosec B608 - 固定の表名
            checkpoint_key=checkpoint_key,
            version_no=version,
            writer_id=WRITER_ID,
        )
        return
    cursor.setinputsizes(snapshot_json=_clob_type())
    cursor.execute(
        f"""
        UPDATE {table}
        SET snapshot_json = :snapshot_json, version_no = :version_no,
            writer_id = :writer_id, updated_at = SYSTIMESTAMP
        WHERE checkpoint_key = :checkpoint_key
        """,  # nosec B608 - 固定の表名
        checkpoint_key=checkpoint_key,
        snapshot_json=text,
        version_no=version,
        writer_id=WRITER_ID,
    )


def read_entities(
    cursor: Any, checkpoint_key: str, *, after_version: int | None = None
) -> list[EntityRow]:
    """Run・業務 Agent の行。

    `after_version` が None なら今ある行の全部（削除の印は除く）、あればその版より後に書かれた行
    （削除の印を含む。別のプロセスの変更）。
    """
    kinds = ", ".join(f"'{kind}'" for kind in ENTITY_KINDS)
    if after_version is None:
        cursor.execute(
            "SELECT entity_kind, entity_id, entity_json, change_version "  # nosec B608 - 固定値
            f"FROM {ENTITY_TABLE} WHERE checkpoint_key = :checkpoint_key "
            f"AND entity_kind IN ({kinds}) AND entity_json IS NOT NULL "
            "ORDER BY entity_kind, entity_id",
            checkpoint_key=checkpoint_key,
        )
    else:
        cursor.execute(
            "SELECT entity_kind, entity_id, entity_json, change_version "  # nosec B608 - 固定値
            f"FROM {ENTITY_TABLE} WHERE checkpoint_key = :checkpoint_key "
            f"AND entity_kind IN ({kinds}) AND change_version > :after_version "
            "ORDER BY entity_kind, entity_id",
            checkpoint_key=checkpoint_key,
            after_version=after_version,
        )
    # CLOB は接続を閉じる前に読む（#765）。
    return [
        EntityRow(
            kind=str(row[0]),
            entity_id=str(row[1]),
            entity_json=lob_to_text(row[2]) if row[2] is not None else None,
            change_version=int(row[3]),
        )
        for row in cursor.fetchall()
    ]


def read_entity(cursor: Any, checkpoint_key: str, kind: str, entity_id: str) -> EntityRow | None:
    """1 件の Run・業務 Agent の今の行（無い・削除の印なら None。#2609 の取消の判定）。"""
    cursor.execute(
        "SELECT entity_kind, entity_id, entity_json, change_version "  # nosec B608 - 固定値
        f"FROM {ENTITY_TABLE} WHERE checkpoint_key = :checkpoint_key "
        "AND entity_kind = :entity_kind AND entity_id = :entity_id AND entity_json IS NOT NULL",
        checkpoint_key=checkpoint_key,
        entity_kind=kind,
        entity_id=entity_id,
    )
    for row in cursor.fetchall():
        return EntityRow(
            kind=str(row[0]),
            entity_id=str(row[1]),
            entity_json=lob_to_text(row[2]) if row[2] is not None else None,
            change_version=int(row[3]),
        )
    return None


def write_entities(
    cursor: Any,
    checkpoint_key: str,
    rows: Mapping[EntityKey, str | None],
    *,
    version: int,
) -> None:
    """変わった Run・業務 Agent の行を書く（None は削除の印）。"""
    if not rows:
        return
    cursor.setinputsizes(entity_json=_clob_type())
    cursor.executemany(
        f"""
        MERGE INTO {ENTITY_TABLE} target
        USING (
            SELECT :checkpoint_key AS checkpoint_key, :entity_kind AS entity_kind,
                   :entity_id AS entity_id, :entity_json AS entity_json,
                   :change_version AS change_version, :writer_id AS writer_id
            FROM dual
        ) source
        ON (
            target.checkpoint_key = source.checkpoint_key
            AND target.entity_kind = source.entity_kind
            AND target.entity_id = source.entity_id
        )
        WHEN MATCHED THEN UPDATE SET
            target.entity_json = source.entity_json,
            target.change_version = source.change_version,
            target.writer_id = source.writer_id,
            target.updated_at = SYSTIMESTAMP
        WHEN NOT MATCHED THEN INSERT (
            checkpoint_key, entity_kind, entity_id, entity_json, change_version, writer_id,
            updated_at
        ) VALUES (
            source.checkpoint_key, source.entity_kind, source.entity_id, source.entity_json,
            source.change_version, source.writer_id, SYSTIMESTAMP
        )
        """,  # nosec B608 - 固定の表名
        [
            {
                "checkpoint_key": checkpoint_key,
                "entity_kind": kind,
                "entity_id": entity_id,
                "entity_json": text,
                "change_version": version,
                "writer_id": WRITER_ID,
            }
            for (kind, entity_id), text in rows.items()
        ],
    )


def entity_keys(kind: str, ids: Iterable[str]) -> list[EntityKey]:
    return [(kind, entity_id) for entity_id in ids]


def _clob_type() -> Any:
    # Run の JSON は 4,000 / 32,767 byte を超える。型を指定しないと bind が VARCHAR2 になり
    # ORA-01461（#841）。
    return import_module("oracledb").DB_TYPE_CLOB


def lob_to_text(value: object) -> str:
    read = getattr(value, "read", None)
    if callable(read):
        return str(read())
    return str(value)


__all__ = [
    "AGENT",
    "ENTITY_TABLE",
    "LEGACY_SNAPSHOT_KIND",
    "RUN",
    "STORAGE_ENTITIES",
    "WRITER_ID",
    "CheckpointHead",
    "CheckpointMigrationRequired",
    "EntityKey",
    "EntityRow",
    "header_json",
    "insert_head",
    "read_entities",
    "read_head",
    "update_head",
    "write_entities",
]
