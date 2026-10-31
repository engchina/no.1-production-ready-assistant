"""テストの偽の Oracle の cursor が使う、Run・業務 Agent の保存先の SQL（#1916・#1939）。

`runtime_checkpoint_store` の読み書きを、head（`AGENT_RUNTIME_CHECKPOINTS`。header の JSON・版・
最後に書いたプロセス）と、Run・業務 Agent の行（`AGENT_RUNTIME_ENTITIES`）の dict で再現する。

- 移す前の形（migration 016 の前）の head は `set_legacy`（全体の snapshot の JSON）。
- 版の列が無い DB（migration 015 の前）は `versioned=False`（head の SELECT が ORA-00904）。
- 行の表が無い DB は `entities_table=False`（行の SELECT が ORA-00942）。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.features.agent.runtime import (
    AgentProfile,
    AgentRuntimeRepository,
    AgentRuntimeSnapshot,
    QuarantinedSnapshotRecord,
    RunState,
)

KEY = "default"


@dataclass
class CheckpointResult:
    row: tuple[Any, ...] | None = None
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    rowcount: int = 0


@dataclass
class HeadRow:
    text: str
    version: int = 0
    writer_id: str | None = None


@dataclass
class EntityRow:
    text: str | None
    change_version: int
    writer_id: str | None = None


@dataclass
class CheckpointTable:
    heads: dict[str, HeadRow] = field(default_factory=dict)
    # (CHECKPOINT_KEY, ENTITY_KIND, ENTITY_ID) → 行。
    entities: dict[tuple[str, str, str], EntityRow] = field(default_factory=dict)
    versioned: bool = True
    entities_table: bool = True
    # 実行した SQL の種類（書き込みの量の確認。"head_update" / "entity_merge" など）。
    calls: list[str] = field(default_factory=list)

    # ---- テストの準備と確認 ----

    def seed(
        self,
        runs: Iterable[RunState] = (),
        agents: Iterable[AgentProfile] = (),
        *,
        key: str = KEY,
        quarantined: list[QuarantinedSnapshotRecord] | None = None,
        version: int = 0,
    ) -> None:
        """保存先に Run・業務 Agent を入れる（版 `version` で書いたことにする）。"""
        self.heads[key] = HeadRow(
            text=_header([item.model_dump(mode="json") for item in quarantined or []]),
            version=version,
        )
        for run in runs:
            self.entities[(key, "run", run.id)] = EntityRow(run.model_dump_json(), version)
        for agent in agents:
            self.entities[(key, "agent", agent.id)] = EntityRow(agent.model_dump_json(), version)

    def set_legacy(self, snapshot_json: str, *, key: str = KEY, version: int = 0) -> None:
        """移す前の形（全体の snapshot を 1 行で持つ）の head にする。"""
        self.heads[key] = HeadRow(text=snapshot_json, version=version)

    def snapshot(self, key: str = KEY) -> AgentRuntimeSnapshot:
        """保存先の今の Run・業務 Agent（削除の印は除く）と退避の記録。"""
        runs: list[Any] = []
        agents: list[Any] = []
        for (row_key, kind, _entity_id), row in sorted(self.entities.items()):
            if row_key != key or row.text is None:
                continue
            (runs if kind == "run" else agents).append(json.loads(row.text))
        header = json.loads(self.heads[key].text) if key in self.heads else {}
        return AgentRuntimeSnapshot.model_validate(
            {"runs": runs, "agents": agents, "quarantined": header.get("quarantined") or []}
        )

    def version(self, key: str = KEY) -> int:
        return self.heads[key].version

    def writer(self, key: str = KEY) -> str | None:
        return self.heads[key].writer_id

    def header(self, key: str = KEY) -> dict[str, Any]:
        value = json.loads(self.heads[key].text)
        assert isinstance(value, dict)
        return value

    # ---- SQL ----

    def execute(self, statement: str, params: dict[str, Any]) -> CheckpointResult | None:
        """保存先の SQL なら実行した結果、それ以外は None。"""
        normalized = " ".join(statement.upper().split())
        key = str(params.get("checkpoint_key"))
        if normalized.startswith("SELECT CASE WHEN JSON_VALUE(SNAPSHOT_JSON"):
            self.calls.append("head_select")
            if not self.versioned:
                raise RuntimeError('ORA-00904: "VERSION_NO": invalid identifier')
            head = self.heads.get(key)
            if head is None:
                return CheckpointResult()
            storage, snapshot_version = _json_values(head.text)
            text = head.text if storage == "entities" else None
            return CheckpointResult(
                row=(text, storage, head.version, head.writer_id, snapshot_version), rowcount=1
            )
        if normalized.startswith("INSERT INTO AGENT_RUNTIME_CHECKPOINTS"):
            self.calls.append("head_insert")
            if key in self.heads:
                raise RuntimeError("ORA-00001: unique constraint violated")
            self.heads[key] = HeadRow(
                text=str(params["snapshot_json"]), version=0, writer_id=params.get("writer_id")
            )
            return CheckpointResult(rowcount=1)
        if normalized.startswith("UPDATE AGENT_RUNTIME_CHECKPOINTS"):
            self.calls.append("head_update")
            head = self.heads[key]
            head.version = int(params["version_no"])
            head.writer_id = params.get("writer_id")
            if "snapshot_json" in params:
                head.text = str(params["snapshot_json"])
            return CheckpointResult(rowcount=1)
        if "FROM AGENT_RUNTIME_ENTITIES" in normalized and normalized.startswith("SELECT"):
            self.calls.append("entity_select")
            if not self.entities_table:
                raise RuntimeError("ORA-00942: table or view does not exist")
            after = params.get("after_version")
            rows = []
            for (row_key, kind, entity_id), row in sorted(self.entities.items()):
                if row_key != key or kind not in {"run", "agent"}:
                    continue
                # 1 件の読み込み（`read_entity`）。
                if "entity_kind" in params and kind != params["entity_kind"]:
                    continue
                if "entity_id" in params and entity_id != params["entity_id"]:
                    continue
                if after is None and row.text is None:
                    continue
                if after is not None and row.change_version <= int(after):
                    continue
                rows.append((kind, entity_id, row.text, row.change_version))
            return CheckpointResult(rows=rows, rowcount=len(rows))
        if normalized.startswith("MERGE INTO AGENT_RUNTIME_ENTITIES"):
            self.calls.append("entity_merge")
            self.entities[(key, str(params["entity_kind"]), str(params["entity_id"]))] = EntityRow(
                params["entity_json"],
                int(params["change_version"]),
                params.get("writer_id"),
            )
            return CheckpointResult(rowcount=1)
        return None

    def connect(self) -> CheckpointConnection:
        """`connect_platform_oracle` の代わり（保存先の SQL だけを受け付ける）。"""
        return CheckpointConnection(self)


def _header(quarantined: list[Any]) -> str:
    return json.dumps(
        {
            "version": "agent-control-plane.snapshot.v2",
            "storage": "entities",
            "quarantined": quarantined,
        }
    )


def _json_values(text: str) -> tuple[str | None, str | None]:
    """`JSON_VALUE(..., '$.storage')` と `'$.version'`（JSON でなければ NULL）。"""
    try:
        value = json.loads(text)
    except ValueError:
        return None, None
    if not isinstance(value, dict):
        return None, None
    storage, version = value.get("storage"), value.get("version")
    return (
        storage if isinstance(storage, str) else None,
        version if isinstance(version, str) else None,
    )


class CheckpointCursor:
    def __init__(self, table: CheckpointTable) -> None:
        self._table = table
        self._row: tuple[Any, ...] | None = None
        self._rows: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def __enter__(self) -> CheckpointCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def setinputsizes(self, **_sizes: Any) -> None:
        return None

    def execute(self, statement: str, **params: Any) -> None:
        result = self._table.execute(statement, params)
        if result is None:  # pragma: no cover - 想定外の SQL を見逃さない
            raise AssertionError(statement)
        self._row, self._rows, self.rowcount = result.row, result.rows, result.rowcount

    def executemany(self, statement: str, rows: list[dict[str, Any]]) -> None:
        for params in rows:
            self.execute(statement, **params)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class CheckpointConnection:
    def __init__(self, table: CheckpointTable) -> None:
        self._table = table

    def __enter__(self) -> CheckpointConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> CheckpointCursor:
        return CheckpointCursor(self._table)

    def commit(self) -> None:
        return None


def load_state(repository: AgentRuntimeRepository, snapshot: AgentRuntimeSnapshot) -> None:
    """テストの状態を repository に入れる（保存先の読み込みと同じ移行を通す。保存はしない）。"""
    with repository._lock:
        repository._replace_state_locked(snapshot)


def export_state(repository: AgentRuntimeRepository) -> AgentRuntimeSnapshot:
    """repository の今の状態（保存先に書く形と同じ）。"""
    with repository._lock:
        return repository._export_snapshot_locked()
