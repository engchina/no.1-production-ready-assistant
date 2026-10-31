"""品質評価の判定の校正（人手の判定の記入・裁定・校正の結果）の保存（#3578）。

- ``rag_evaluation_label_sets``: 記入の集合（終わった品質評価の job か、
  取り込んだ LabelSet から作る）。
- ``rag_evaluation_label_items``: 集合の項目（質問・回答・根拠・標準回答・判定する観点。
  合成の対照の除外の印）。本文は記入と校正のために保存する（回答の記録と同じ内容）。
- ``rag_evaluation_labels``: 項目 × 観点 × 記入者の判定（``annotation``）と裁定
  （``adjudication``）。記入者は利用者の hash と、集合の中の匿名の ID（``a1``・``a2``…）
  で持つ（実名・メールは持たない）。
- ``rag_evaluation_calibrations``: 校正の実行と結果（判定のモデル・rubric の版・指示の
  sha256・観点 × 区分の一致）。進捗・取り消しは品質評価の job（``rag_evaluation_jobs``
  の kind ``calibration``）で、行の ID は job の ID と同じ。

集合・結果は tenant で分ける。誰が何を見られるかは route と
``app.rag.evaluation_calibration`` が決める（記入者は自分の判定だけ。全員の判定・裁定・
校正は判定の校正の管理の権限）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from app.clients.oracle import (
    OracleClient,
    OracleConnectionProtocol,
    _execute,
    _execute_count,
    _json_bind,
    _json_input_sizes,
    _json_loads,
    _oracle_tenant_predicate,
    _truncate_text,
    _with_tenant_bind,
)
from app.rag.request_context import current_audit_request_context

LABEL_SET_TABLE = "rag_evaluation_label_sets"
LABEL_ITEM_TABLE = "rag_evaluation_label_items"
LABEL_TABLE = "rag_evaluation_labels"
CALIBRATION_TABLE = "rag_evaluation_calibrations"
LABEL_SET_NAME_MAX_CHARS = 200
ITEM_ID_MAX_CHARS = 200
NOTE_MAX_CHARS = 2000
JUDGE_MODEL_MAX_CHARS = 200

LabelRole = Literal["annotation", "adjudication"]
CalibrationStatus = Literal["RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]


def oracle_evaluation_calibration_schema_sql() -> str:
    """判定の校正の表の DDL（#3578）。項目・判定・結果は集合の削除で消える（FK の CASCADE）。"""
    return f"""
CREATE TABLE {LABEL_SET_TABLE} (
    label_set_id    VARCHAR2(64) PRIMARY KEY,
    tenant_id_hash  VARCHAR2(64),
    name            VARCHAR2({LABEL_SET_NAME_MAX_CHARS} CHAR) NOT NULL,
    origin          VARCHAR2(16) NOT NULL,
    rubric_id       VARCHAR2(80) NOT NULL,
    rubric_version  VARCHAR2(40),
    rubric_sha256   VARCHAR2(64),
    items_sha256    VARCHAR2(64) NOT NULL,
    item_count      NUMBER(10) DEFAULT 0 NOT NULL,
    split_policy    JSON,
    sources         JSON,
    created_by_hash VARCHAR2(64),
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT {LABEL_SET_TABLE}_origin_ck CHECK (origin IN ('jobs', 'import'))
);

CREATE INDEX {LABEL_SET_TABLE}_tenant_created_idx
    ON {LABEL_SET_TABLE} (tenant_id_hash, created_at DESC);

CREATE TABLE {LABEL_ITEM_TABLE} (
    label_set_id    VARCHAR2(64) NOT NULL,
    item_id         VARCHAR2({ITEM_ID_MAX_CHARS} CHAR) NOT NULL,
    item_order      NUMBER(10) NOT NULL,
    split           VARCHAR2(16) NOT NULL,
    origin          VARCHAR2(16) NOT NULL,
    excluded        NUMBER(1) DEFAULT 0 NOT NULL,
    item_json       JSON NOT NULL,
    CONSTRAINT {LABEL_ITEM_TABLE}_pk PRIMARY KEY (label_set_id, item_id),
    CONSTRAINT {LABEL_ITEM_TABLE}_set_fk FOREIGN KEY (label_set_id)
        REFERENCES {LABEL_SET_TABLE} (label_set_id) ON DELETE CASCADE,
    CONSTRAINT {LABEL_ITEM_TABLE}_split_ck CHECK (split IN ('dev', 'holdout')),
    CONSTRAINT {LABEL_ITEM_TABLE}_origin_ck CHECK (origin IN ('human', 'synthetic')),
    CONSTRAINT {LABEL_ITEM_TABLE}_excluded_ck CHECK (excluded IN (0, 1))
);

CREATE TABLE {LABEL_TABLE} (
    label_set_id    VARCHAR2(64) NOT NULL,
    item_id         VARCHAR2({ITEM_ID_MAX_CHARS} CHAR) NOT NULL,
    aspect          VARCHAR2(80) NOT NULL,
    role            VARCHAR2(16) NOT NULL,
    annotator_code  VARCHAR2(40) NOT NULL,
    annotator_hash  VARCHAR2(64),
    label           VARCHAR2(16) NOT NULL,
    error_type      VARCHAR2(40),
    note            VARCHAR2({NOTE_MAX_CHARS} CHAR),
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT {LABEL_TABLE}_pk
        PRIMARY KEY (label_set_id, item_id, aspect, role, annotator_code),
    CONSTRAINT {LABEL_TABLE}_item_fk FOREIGN KEY (label_set_id, item_id)
        REFERENCES {LABEL_ITEM_TABLE} (label_set_id, item_id) ON DELETE CASCADE,
    CONSTRAINT {LABEL_TABLE}_role_ck CHECK (role IN ('annotation', 'adjudication')),
    CONSTRAINT {LABEL_TABLE}_label_ck CHECK (label IN ('pass', 'fail', 'cannot_judge'))
);

CREATE INDEX {LABEL_TABLE}_annotator_idx
    ON {LABEL_TABLE} (label_set_id, annotator_hash);

CREATE TABLE {CALIBRATION_TABLE} (
    calibration_id       VARCHAR2(64) PRIMARY KEY,
    label_set_id         VARCHAR2(64) NOT NULL,
    tenant_id_hash       VARCHAR2(64),
    user_id_hash         VARCHAR2(64),
    status               VARCHAR2(16) NOT NULL,
    repeats              NUMBER(2) DEFAULT 1 NOT NULL,
    judge_model_id       VARCHAR2({JUDGE_MODEL_MAX_CHARS} CHAR),
    judge_rubric_version VARCHAR2(40),
    prompt_sha256        VARCHAR2(64),
    labels_sha256        VARCHAR2(64),
    passed               NUMBER(1),
    error_message        VARCHAR2(2000 CHAR),
    summary_json         JSON,
    report_json          JSON,
    created_at           TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    finished_at          TIMESTAMP WITH TIME ZONE,
    CONSTRAINT {CALIBRATION_TABLE}_set_fk FOREIGN KEY (label_set_id)
        REFERENCES {LABEL_SET_TABLE} (label_set_id) ON DELETE CASCADE,
    CONSTRAINT {CALIBRATION_TABLE}_status_ck
        CHECK (status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')),
    CONSTRAINT {CALIBRATION_TABLE}_passed_ck CHECK (passed IN (0, 1))
);

CREATE INDEX {CALIBRATION_TABLE}_set_created_idx
    ON {CALIBRATION_TABLE} (label_set_id, created_at DESC);

CREATE INDEX {CALIBRATION_TABLE}_judge_idx
    ON {CALIBRATION_TABLE} (tenant_id_hash, judge_model_id, prompt_sha256, status)
""".strip()


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class LabelSetRecord:
    """記入の集合。"""

    label_set_id: str
    name: str
    origin: Literal["jobs", "import"]
    rubric_id: str
    items_sha256: str
    item_count: int
    rubric_version: str | None = None
    rubric_sha256: str | None = None
    split_policy: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, Any] = field(default_factory=dict)
    created_by_hash: str | None = None
    tenant_id_hash: str | None = None
    created_at: datetime = field(default_factory=_now)


@dataclass
class LabelItemRecord:
    """集合の項目（``payload`` は質問・回答・根拠・標準回答・観点など）。"""

    item_id: str
    item_order: int
    split: Literal["dev", "holdout"]
    origin: Literal["human", "synthetic"]
    payload: dict[str, Any]
    excluded: bool = False


@dataclass
class LabelRecord:
    """項目 × 観点の、1 人の記入者の判定か裁定。"""

    item_id: str
    aspect: str
    role: LabelRole
    annotator_code: str
    label: str
    annotator_hash: str | None = None
    error_type: str | None = None
    note: str | None = None
    updated_at: datetime = field(default_factory=_now)


@dataclass
class CalibrationRecord:
    """校正の実行と結果（``report`` は一覧では読まない）。"""

    calibration_id: str
    label_set_id: str
    status: CalibrationStatus
    repeats: int = 1
    tenant_id_hash: str | None = None
    user_id_hash: str | None = None
    judge_model_id: str | None = None
    judge_rubric_version: str | None = None
    prompt_sha256: str | None = None
    labels_sha256: str | None = None
    passed: bool | None = None
    error_message: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    report: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=_now)
    finished_at: datetime | None = None


@dataclass(frozen=True)
class CalibrationFinish:
    """校正の終わりに書く値。"""

    status: CalibrationStatus
    judge_rubric_version: str | None = None
    passed: bool | None = None
    error_message: str | None = None
    summary: Mapping[str, Any] | None = None
    report: Mapping[str, Any] | None = None


class EvaluationCalibrationStore(Protocol):
    """判定の校正の保存先（Oracle / memory）。"""

    async def create_label_set(
        self,
        record: LabelSetRecord,
        items: Sequence[LabelItemRecord],
        labels: Sequence[LabelRecord],
    ) -> None: ...

    async def list_label_sets(self) -> list[LabelSetRecord]: ...

    async def get_label_set(self, label_set_id: str) -> LabelSetRecord | None: ...

    async def delete_label_set(self, label_set_id: str) -> bool: ...

    async def list_items(self, label_set_id: str) -> list[LabelItemRecord]: ...

    async def set_item_excluded(self, label_set_id: str, item_id: str, excluded: bool) -> bool: ...

    async def list_labels(
        self,
        label_set_id: str,
        *,
        role: LabelRole | None = None,
        annotator_hash: str | None = None,
    ) -> list[LabelRecord]: ...

    async def save_label(self, label_set_id: str, label: LabelRecord) -> None: ...

    async def delete_label(
        self, label_set_id: str, *, item_id: str, aspect: str, role: LabelRole, annotator_code: str
    ) -> bool: ...

    async def create_calibration(self, record: CalibrationRecord) -> None: ...

    async def finish_calibration(self, calibration_id: str, finish: CalibrationFinish) -> None: ...

    async def get_calibration(self, calibration_id: str) -> CalibrationRecord | None: ...

    async def list_calibrations(
        self, *, label_set_id: str | None = None, status: CalibrationStatus | None = None
    ) -> list[CalibrationRecord]: ...


def _tenant() -> str | None:
    return current_audit_request_context().tenant_id_hash


class MemoryEvaluationCalibrationStore:
    """Oracle の接続設定が無い開発環境・テストの保存先（プロセスの中だけ）。"""

    def __init__(self) -> None:
        self._sets: dict[str, LabelSetRecord] = dict()
        self._items: dict[str, dict[str, LabelItemRecord]] = dict()
        self._labels: dict[str, dict[tuple[str, str, str, str], LabelRecord]] = dict()
        self._calibrations: dict[str, CalibrationRecord] = dict()

    def _visible(self, record: LabelSetRecord | CalibrationRecord) -> bool:
        tenant = _tenant()
        return tenant is None or record.tenant_id_hash == tenant

    async def create_label_set(
        self,
        record: LabelSetRecord,
        items: Sequence[LabelItemRecord],
        labels: Sequence[LabelRecord],
    ) -> None:
        self._sets[record.label_set_id] = replace(record, tenant_id_hash=_tenant())
        self._items[record.label_set_id] = dict((item.item_id, replace(item)) for item in items)
        self._labels[record.label_set_id] = dict(
            ((label.item_id, label.aspect, label.role, label.annotator_code), replace(label))
            for label in labels
        )

    async def list_label_sets(self) -> list[LabelSetRecord]:
        records = [replace(item) for item in self._sets.values() if self._visible(item)]
        return sorted(records, key=lambda item: item.created_at, reverse=True)

    async def get_label_set(self, label_set_id: str) -> LabelSetRecord | None:
        record = self._sets.get(label_set_id)
        return replace(record) if record is not None and self._visible(record) else None

    async def delete_label_set(self, label_set_id: str) -> bool:
        if await self.get_label_set(label_set_id) is None:
            return False
        del self._sets[label_set_id]
        self._items.pop(label_set_id, None)
        self._labels.pop(label_set_id, None)
        for calibration_id in [
            key for key, item in self._calibrations.items() if item.label_set_id == label_set_id
        ]:
            del self._calibrations[calibration_id]
        return True

    async def list_items(self, label_set_id: str) -> list[LabelItemRecord]:
        items = self._items.get(label_set_id, dict()).values()
        return sorted((replace(item) for item in items), key=lambda item: item.item_order)

    async def set_item_excluded(self, label_set_id: str, item_id: str, excluded: bool) -> bool:
        item = self._items.get(label_set_id, dict()).get(item_id)
        if item is None or item.origin != "synthetic":
            return False
        item.excluded = excluded
        return True

    async def list_labels(
        self,
        label_set_id: str,
        *,
        role: LabelRole | None = None,
        annotator_hash: str | None = None,
    ) -> list[LabelRecord]:
        return [
            replace(label)
            for label in self._labels.get(label_set_id, dict()).values()
            if (role is None or label.role == role)
            and (annotator_hash is None or label.annotator_hash == annotator_hash)
        ]

    async def save_label(self, label_set_id: str, label: LabelRecord) -> None:
        key = (label.item_id, label.aspect, label.role, label.annotator_code)
        self._labels.setdefault(label_set_id, dict())[key] = replace(label, updated_at=_now())

    async def delete_label(
        self, label_set_id: str, *, item_id: str, aspect: str, role: LabelRole, annotator_code: str
    ) -> bool:
        key = (item_id, aspect, role, annotator_code)
        return self._labels.get(label_set_id, dict()).pop(key, None) is not None

    async def create_calibration(self, record: CalibrationRecord) -> None:
        self._calibrations[record.calibration_id] = replace(record, tenant_id_hash=_tenant())

    async def finish_calibration(self, calibration_id: str, finish: CalibrationFinish) -> None:
        record = self._calibrations.get(calibration_id)
        if record is None or record.status != "RUNNING":
            return
        record.status = finish.status
        record.judge_rubric_version = finish.judge_rubric_version
        record.passed = finish.passed
        record.error_message = finish.error_message
        record.summary = dict(finish.summary or dict())
        record.report = dict(finish.report) if finish.report is not None else None
        record.finished_at = _now()

    async def get_calibration(self, calibration_id: str) -> CalibrationRecord | None:
        record = self._calibrations.get(calibration_id)
        return replace(record) if record is not None and self._visible(record) else None

    async def list_calibrations(
        self, *, label_set_id: str | None = None, status: CalibrationStatus | None = None
    ) -> list[CalibrationRecord]:
        records = [
            replace(item, report=None)
            for item in self._calibrations.values()
            if self._visible(item)
            and (label_set_id is None or item.label_set_id == label_set_id)
            and (status is None or item.status == status)
        ]
        return sorted(records, key=lambda item: item.created_at, reverse=True)


def _aware(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return _now()


def _optional_aware(value: object) -> datetime | None:
    return _aware(value) if isinstance(value, datetime) else None


def _str_or_none(value: object) -> str | None:
    return str(value) if value is not None and value != "" else None


def _label_set_from_row(row: Mapping[str, object]) -> LabelSetRecord:
    origin: Literal["jobs", "import"] = "import" if row.get("origin") == "import" else "jobs"
    return LabelSetRecord(
        label_set_id=str(row["label_set_id"]),
        name=str(row.get("name") or ""),
        origin=origin,
        rubric_id=str(row.get("rubric_id") or ""),
        rubric_version=_str_or_none(row.get("rubric_version")),
        rubric_sha256=_str_or_none(row.get("rubric_sha256")),
        items_sha256=str(row.get("items_sha256") or ""),
        item_count=int(str(row.get("item_count") or 0)),
        split_policy=_json_loads(row.get("split_policy")),
        sources=_json_loads(row.get("sources")),
        created_by_hash=_str_or_none(row.get("created_by_hash")),
        tenant_id_hash=_str_or_none(row.get("tenant_id_hash")),
        created_at=_aware(row.get("created_at")),
    )


def _label_from_row(row: Mapping[str, object]) -> LabelRecord:
    role: LabelRole = "adjudication" if row.get("role") == "adjudication" else "annotation"
    return LabelRecord(
        item_id=str(row["item_id"]),
        aspect=str(row["aspect"]),
        role=role,
        annotator_code=str(row["annotator_code"]),
        annotator_hash=_str_or_none(row.get("annotator_hash")),
        label=str(row["label"]),
        error_type=_str_or_none(row.get("error_type")),
        note=_str_or_none(row.get("note")),
        updated_at=_aware(row.get("updated_at")),
    )


_CALIBRATION_STATUSES: dict[str, CalibrationStatus] = dict(
    RUNNING="RUNNING", SUCCEEDED="SUCCEEDED", FAILED="FAILED", CANCELLED="CANCELLED"
)


def _calibration_from_row(row: Mapping[str, object]) -> CalibrationRecord:
    passed = row.get("passed")
    report = row.get("report_json")
    return CalibrationRecord(
        calibration_id=str(row["calibration_id"]),
        label_set_id=str(row["label_set_id"]),
        status=_CALIBRATION_STATUSES.get(str(row.get("status") or ""), "FAILED"),
        repeats=int(str(row.get("repeats") or 1)),
        tenant_id_hash=_str_or_none(row.get("tenant_id_hash")),
        user_id_hash=_str_or_none(row.get("user_id_hash")),
        judge_model_id=_str_or_none(row.get("judge_model_id")),
        judge_rubric_version=_str_or_none(row.get("judge_rubric_version")),
        prompt_sha256=_str_or_none(row.get("prompt_sha256")),
        labels_sha256=_str_or_none(row.get("labels_sha256")),
        passed=None if passed is None else str(passed) == "1",
        error_message=_str_or_none(row.get("error_message")),
        summary=_json_loads(row.get("summary_json")),
        report=_json_loads(report) if report is not None else None,
        created_at=_aware(row.get("created_at")),
        finished_at=_optional_aware(row.get("finished_at")),
    )


# 実行中の校正は、品質評価の job（同じ ID）が終わっていればその状態にする（プロセスが止まった job は
# job の側が失敗にする）。
_CALIBRATION_COLUMNS = """
    c.calibration_id, c.label_set_id, c.tenant_id_hash, c.user_id_hash,
    CASE WHEN c.status = 'RUNNING' AND j.status IS NOT NULL AND j.status <> 'RUNNING'
         THEN j.status ELSE c.status END AS status,
    c.repeats, c.judge_model_id, c.judge_rubric_version, c.prompt_sha256, c.labels_sha256,
    c.passed,
    COALESCE(c.error_message, CASE WHEN c.status = 'RUNNING' THEN j.error_message END)
        AS error_message,
    c.summary_json, c.created_at, c.finished_at
"""
_LABEL_SET_COLUMNS = """
    label_set_id, tenant_id_hash, name, origin, rubric_id, rubric_version, rubric_sha256,
    items_sha256, item_count, split_policy, sources, created_by_hash, created_at
"""


def _label_binds(label_set_id: str, label: LabelRecord) -> dict[str, object]:
    return dict(
        label_set_id=label_set_id,
        item_id=label.item_id,
        aspect=label.aspect,
        role=label.role,
        annotator_code=label.annotator_code,
        annotator_hash=label.annotator_hash,
        label=label.label,
        error_type=label.error_type,
        note=_truncate_text(label.note, NOTE_MAX_CHARS),
    )


def _insert_label(
    connection: OracleConnectionProtocol, label_set_id: str, label: LabelRecord
) -> None:
    _execute(
        connection,
        f"""
        INSERT INTO {LABEL_TABLE} (
            label_set_id, item_id, aspect, role, annotator_code, annotator_hash, label,
            error_type, note
        ) VALUES (
            :label_set_id, :item_id, :aspect, :role, :annotator_code, :annotator_hash, :label,
            :error_type, :note
        )
        """,
        _label_binds(label_set_id, label),
    )


class OracleEvaluationCalibrationStore:
    """判定の校正の表に保存する（OracleClient の接続と transaction を使う）。"""

    def __init__(self, oracle: OracleClient | None = None) -> None:
        self._oracle = oracle or OracleClient()

    async def create_label_set(
        self,
        record: LabelSetRecord,
        items: Sequence[LabelItemRecord],
        labels: Sequence[LabelRecord],
    ) -> None:
        tenant = _tenant()

        def operation(connection: OracleConnectionProtocol) -> None:
            _execute(
                connection,
                f"""
                INSERT INTO {LABEL_SET_TABLE} (
                    label_set_id, tenant_id_hash, name, origin, rubric_id, rubric_version,
                    rubric_sha256, items_sha256, item_count, split_policy, sources, created_by_hash
                ) VALUES (
                    :label_set_id, :tenant_id_hash, :name, :origin, :rubric_id, :rubric_version,
                    :rubric_sha256, :items_sha256, :item_count, :split_policy, :sources,
                    :created_by_hash
                )
                """,
                dict(
                    label_set_id=record.label_set_id,
                    tenant_id_hash=tenant,
                    name=_truncate_text(record.name, LABEL_SET_NAME_MAX_CHARS),
                    origin=record.origin,
                    rubric_id=record.rubric_id,
                    rubric_version=record.rubric_version,
                    rubric_sha256=record.rubric_sha256,
                    items_sha256=record.items_sha256,
                    item_count=record.item_count,
                    split_policy=_json_bind(record.split_policy),
                    sources=_json_bind(record.sources),
                    created_by_hash=record.created_by_hash,
                ),
                input_sizes=_json_input_sizes("split_policy", "sources"),
            )
            for item in items:
                _execute(
                    connection,
                    f"""
                    INSERT INTO {LABEL_ITEM_TABLE} (
                        label_set_id, item_id, item_order, split, origin, excluded, item_json
                    ) VALUES (
                        :label_set_id, :item_id, :item_order, :split, :origin, :excluded,
                        :item_json
                    )
                    """,
                    dict(
                        label_set_id=record.label_set_id,
                        item_id=item.item_id,
                        item_order=item.item_order,
                        split=item.split,
                        origin=item.origin,
                        excluded=1 if item.excluded else 0,
                        item_json=_json_bind(item.payload),
                    ),
                    input_sizes=_json_input_sizes("item_json"),
                )
            for label in labels:
                _insert_label(connection, record.label_set_id, label)

        await self._oracle._run_transaction(operation)

    async def list_label_sets(self) -> list[LabelSetRecord]:
        rows = await self._oracle._fetch_all(
            f"""
            SELECT {_LABEL_SET_COLUMNS}
            FROM {LABEL_SET_TABLE}
            WHERE {_oracle_tenant_predicate()}
            ORDER BY created_at DESC, label_set_id
            FETCH FIRST 200 ROWS ONLY
            """,
            _with_tenant_bind(dict()),
        )
        return [_label_set_from_row(row) for row in rows]

    async def get_label_set(self, label_set_id: str) -> LabelSetRecord | None:
        row = await self._oracle._fetch_one(
            f"""
            SELECT {_LABEL_SET_COLUMNS}
            FROM {LABEL_SET_TABLE}
            WHERE label_set_id = :label_set_id AND {_oracle_tenant_predicate()}
            """,
            _with_tenant_bind(dict(label_set_id=label_set_id)),
        )
        return _label_set_from_row(row) if row is not None else None

    async def delete_label_set(self, label_set_id: str) -> bool:
        binds = _with_tenant_bind(dict(label_set_id=label_set_id))
        statement = (
            f"DELETE FROM {LABEL_SET_TABLE} "
            f"WHERE label_set_id = :label_set_id AND {_oracle_tenant_predicate()}"
        )

        def operation(connection: OracleConnectionProtocol) -> int:
            return _execute_count(connection, statement, binds)

        return await self._oracle._run_transaction(operation) > 0

    async def list_items(self, label_set_id: str) -> list[LabelItemRecord]:
        rows = await self._oracle._fetch_all(
            f"""
            SELECT item_id, item_order, split, origin, excluded, item_json
            FROM {LABEL_ITEM_TABLE}
            WHERE label_set_id = :label_set_id
            ORDER BY item_order
            """,
            dict(label_set_id=label_set_id),
        )
        return [
            LabelItemRecord(
                item_id=str(row["item_id"]),
                item_order=int(str(row.get("item_order") or 0)),
                split="dev" if row.get("split") == "dev" else "holdout",
                origin="synthetic" if row.get("origin") == "synthetic" else "human",
                excluded=str(row.get("excluded") or 0) == "1",
                payload=_json_loads(row.get("item_json")),
            )
            for row in rows
        ]

    async def set_item_excluded(self, label_set_id: str, item_id: str, excluded: bool) -> bool:
        def operation(connection: OracleConnectionProtocol) -> int:
            return _execute_count(
                connection,
                f"""
                UPDATE {LABEL_ITEM_TABLE} SET excluded = :excluded
                WHERE label_set_id = :label_set_id AND item_id = :item_id
                  AND origin = 'synthetic'
                """,
                dict(label_set_id=label_set_id, item_id=item_id, excluded=1 if excluded else 0),
            )

        return await self._oracle._run_transaction(operation) > 0

    async def list_labels(
        self,
        label_set_id: str,
        *,
        role: LabelRole | None = None,
        annotator_hash: str | None = None,
    ) -> list[LabelRecord]:
        clauses = ["label_set_id = :label_set_id"]
        binds: dict[str, object] = dict(label_set_id=label_set_id)
        if role is not None:
            clauses.append("role = :role")
            binds["role"] = role
        if annotator_hash is not None:
            clauses.append("annotator_hash = :annotator_hash")
            binds["annotator_hash"] = annotator_hash
        where = " AND ".join(clauses)
        rows = await self._oracle._fetch_all(
            f"""
            SELECT item_id, aspect, role, annotator_code, annotator_hash, label, error_type,
                   note, updated_at
            FROM {LABEL_TABLE}
            WHERE {where}
            """,
            binds,
        )
        return [_label_from_row(row) for row in rows]

    async def save_label(self, label_set_id: str, label: LabelRecord) -> None:
        def operation(connection: OracleConnectionProtocol) -> None:
            updated = _execute_count(
                connection,
                f"""
                UPDATE {LABEL_TABLE}
                SET label = :label, error_type = :error_type, note = :note,
                    annotator_hash = :annotator_hash, updated_at = SYSTIMESTAMP
                WHERE label_set_id = :label_set_id AND item_id = :item_id
                  AND aspect = :aspect AND role = :role AND annotator_code = :annotator_code
                """,
                _label_binds(label_set_id, label),
            )
            if not updated:
                _insert_label(connection, label_set_id, label)

        await self._oracle._run_transaction(operation, retry_deadlocks=True)

    async def delete_label(
        self, label_set_id: str, *, item_id: str, aspect: str, role: LabelRole, annotator_code: str
    ) -> bool:
        def operation(connection: OracleConnectionProtocol) -> int:
            return _execute_count(
                connection,
                f"""
                DELETE FROM {LABEL_TABLE}
                WHERE label_set_id = :label_set_id AND item_id = :item_id
                  AND aspect = :aspect AND role = :role AND annotator_code = :annotator_code
                """,
                dict(
                    label_set_id=label_set_id,
                    item_id=item_id,
                    aspect=aspect,
                    role=role,
                    annotator_code=annotator_code,
                ),
            )

        return await self._oracle._run_transaction(operation) > 0

    async def create_calibration(self, record: CalibrationRecord) -> None:
        binds = dict(
            calibration_id=record.calibration_id,
            label_set_id=record.label_set_id,
            tenant_id_hash=_tenant(),
            user_id_hash=record.user_id_hash,
            repeats=record.repeats,
            judge_model_id=_truncate_text(record.judge_model_id, JUDGE_MODEL_MAX_CHARS),
            prompt_sha256=record.prompt_sha256,
            labels_sha256=record.labels_sha256,
        )
        statement = f"""
            INSERT INTO {CALIBRATION_TABLE} (
                calibration_id, label_set_id, tenant_id_hash, user_id_hash, status, repeats,
                judge_model_id, prompt_sha256, labels_sha256
            ) VALUES (
                :calibration_id, :label_set_id, :tenant_id_hash, :user_id_hash, 'RUNNING',
                :repeats, :judge_model_id, :prompt_sha256, :labels_sha256
            )
        """
        await self._oracle._run_transaction(
            lambda connection: _execute(connection, statement, binds)
        )

    async def finish_calibration(self, calibration_id: str, finish: CalibrationFinish) -> None:
        binds = dict(
            calibration_id=calibration_id,
            status=finish.status,
            judge_rubric_version=finish.judge_rubric_version,
            passed=None if finish.passed is None else int(finish.passed),
            error_message=_truncate_text(finish.error_message, 2000),
            summary_json=_json_bind(dict(finish.summary or dict())),
            report_json=_json_bind(dict(finish.report)) if finish.report is not None else None,
        )
        statement = f"""
            UPDATE {CALIBRATION_TABLE}
            SET status = :status, judge_rubric_version = :judge_rubric_version,
                passed = :passed, error_message = :error_message,
                summary_json = :summary_json, report_json = :report_json,
                finished_at = SYSTIMESTAMP
            WHERE calibration_id = :calibration_id AND status = 'RUNNING'
        """

        def operation(connection: OracleConnectionProtocol) -> None:
            _execute(
                connection,
                statement,
                binds,
                input_sizes=_json_input_sizes("summary_json", "report_json"),
            )

        await self._oracle._run_transaction(operation)

    async def get_calibration(self, calibration_id: str) -> CalibrationRecord | None:
        row = await self._oracle._fetch_one(
            f"""
            SELECT {_CALIBRATION_COLUMNS}, c.report_json
            FROM {CALIBRATION_TABLE} c
            LEFT JOIN rag_evaluation_jobs j ON j.job_id = c.calibration_id
            WHERE c.calibration_id = :calibration_id
              AND {_oracle_tenant_predicate(alias="c")}
            """,
            _with_tenant_bind(dict(calibration_id=calibration_id)),
        )
        return _calibration_from_row(row) if row is not None else None

    async def list_calibrations(
        self, *, label_set_id: str | None = None, status: CalibrationStatus | None = None
    ) -> list[CalibrationRecord]:
        clauses = [_oracle_tenant_predicate(alias="c")]
        binds: dict[str, object] = dict()
        if label_set_id is not None:
            clauses.append("c.label_set_id = :label_set_id")
            binds["label_set_id"] = label_set_id
        if status is not None:
            clauses.append("c.status = :status")
            binds["status"] = status
        where = " AND ".join(clauses)
        rows = await self._oracle._fetch_all(
            f"""
            SELECT {_CALIBRATION_COLUMNS}
            FROM {CALIBRATION_TABLE} c
            LEFT JOIN rag_evaluation_jobs j ON j.job_id = c.calibration_id
            WHERE {where}
            ORDER BY c.created_at DESC
            FETCH FIRST 100 ROWS ONLY
            """,
            _with_tenant_bind(binds),
        )
        return [_calibration_from_row(row) for row in rows]
