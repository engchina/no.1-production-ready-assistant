"""Select AI は専用の永続一時表へ書く。確認後の INSERT と run 更新は同一 transaction。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import HTTPException

from .object_identity import OracleObjectIdentity, parse_object_identity
from .oracle_adapter import OracleNl2SqlAdapter, _coerce_result_value
from .synthetic_models import SyntheticRun


def quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def qualified(value: str) -> str:
    identity = parse_object_identity(value)
    return f"{quote(identity.owner)}.{quote(identity.object_name)}"


def metadata(cur: Any, name: str) -> dict[str, Any]:
    identity = parse_object_identity(name)
    binds = {"owner": identity.owner, "name": identity.object_name}
    cur.execute(
        "SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, DATA_PRECISION, DATA_SCALE, "
        "NULLABLE, VIRTUAL_COLUMN, IDENTITY_COLUMN, DATA_DEFAULT "
        "FROM ALL_TAB_COLS WHERE OWNER=:owner AND TABLE_NAME=:name "
        "AND HIDDEN_COLUMN='NO' ORDER BY COLUMN_ID",
        binds,
    )
    columns = [list(row) for row in cur.fetchall()]
    if not columns or any(row[6] == "YES" or row[7] == "YES" for row in columns):
        raise HTTPException(409, f"{name}: 仮想列・IDENTITY 列を含む表の事前確認は未対応です。")
    cur.execute(
        "SELECT COUNT(*) FROM ALL_TRIGGERS WHERE TABLE_OWNER=:owner "
        "AND TABLE_NAME=:name AND STATUS='ENABLED'",
        binds,
    )
    if cur.fetchone()[0]:
        raise HTTPException(
            409, f"{name}: 有効なトリガーがあるため、確認内容と適用内容を保証できません。"
        )
    cur.execute(
        "SELECT CONSTRAINT_NAME, CONSTRAINT_TYPE, R_OWNER, R_CONSTRAINT_NAME, "
        "SEARCH_CONDITION FROM ALL_CONSTRAINTS "
        "WHERE OWNER=:owner AND TABLE_NAME=:name AND STATUS='ENABLED' "
        "AND CONSTRAINT_TYPE IN ('P','U','R','C') ORDER BY CONSTRAINT_NAME",
        binds,
    )
    constraints = []
    for cname, kind, ref_owner, ref_constraint, condition in cur.fetchall():
        cur.execute(
            "SELECT COLUMN_NAME FROM ALL_CONS_COLUMNS WHERE OWNER=:owner "
            "AND CONSTRAINT_NAME=:name ORDER BY POSITION",
            {"owner": identity.owner, "name": cname},
        )
        item = {
            "name": cname,
            "kind": kind,
            "columns": [r[0] for r in cur.fetchall()],
            "condition": condition,
        }
        if kind == "R":
            cur.execute(
                "SELECT TABLE_NAME, COLUMN_NAME FROM ALL_CONS_COLUMNS WHERE OWNER=:owner "
                "AND CONSTRAINT_NAME=:name ORDER BY POSITION",
                {"owner": ref_owner, "name": ref_constraint},
            )
            refs = cur.fetchall()
            if not refs:
                raise HTTPException(409, f"{name}: 外部キーの参照先を確認できません。")
            item.update(
                reference=OracleObjectIdentity(ref_owner, refs[0][0]).qualified_name,
                ref_columns=[r[1] for r in refs],
            )
        constraints.append(item)
    return {"columns": columns, "constraints": constraints}


def ordered_tables(staging: dict[str, dict[str, Any]]) -> list[str]:
    remaining = set(staging)
    ordered: list[str] = []
    while remaining:
        ready = sorted(
            name
            for name in remaining
            if not any(
                c.get("reference") in remaining
                for c in staging[name]["metadata"]["constraints"]
                if c["kind"] == "R"
            )
        )
        if not ready:
            raise HTTPException(409, "循環・自己参照の外部キーを持つ表の事前確認は未対応です。")
        ordered.extend(ready)
        remaining.difference_update(ready)
    return ordered


class SyntheticPreview:
    def __init__(self, adapter: OracleNl2SqlAdapter):
        self.adapter = adapter

    def plan(self, run: SyntheticRun) -> dict[str, dict[str, Any]]:
        prefix = f"{self.adapter.settings.oracle_user.upper()}.NL2SQL_SP_"
        prefix += run.run_id.replace("-", "").upper()
        with self.adapter.connection() as conn, conn.cursor() as cur:
            plan = {
                target.table_name: {
                    "name": f"{prefix}_{i}",
                    "metadata": metadata(cur, target.table_name),
                    "seeds": [],
                }
                for i, target in enumerate(run.targets)
            }
            ordered_tables(plan)
            return plan

    def prepare(self, run: SyntheticRun, persist: Any) -> None:
        # Persist names before DDL (implicit commit), so cleanup can find every table.
        with self.adapter.connection() as conn, conn.cursor() as cur:
            for name, stage in run.staging.items():
                table = qualified(stage["name"])
                cur.execute(
                    f"CREATE TABLE {table} AS SELECT * FROM {qualified(name)} WHERE 1=0"  # nosec B608
                )  # nosec B608
                identity = parse_object_identity(stage["name"])
                cur.execute(
                    "SELECT OBJECT_ID FROM ALL_OBJECTS WHERE OWNER=:owner "
                    "AND OBJECT_NAME=:name AND OBJECT_TYPE='TABLE'",
                    {"owner": identity.owner, "name": identity.object_name},
                )
                stage["object_id"] = int(cur.fetchone()[0])
                persist(run.staging)
                samples = int(run.request.get("sample_rows", 0))
                if samples:
                    cur.execute(
                        f"INSERT INTO {table} SELECT * FROM {qualified(name)} WHERE ROWNUM<=:n",  # nosec B608
                        {"n": samples},
                    )  # nosec B608
                    cur.execute(f"SELECT ROWIDTOCHAR(ROWID) FROM {table}")  # nosec B608
                    stage["seeds"] = [r[0] for r in cur.fetchall()]
                # Preserve comments and the logical target name for Select AI.
                source = parse_object_identity(name)
                cur.execute(
                    "SELECT COMMENTS FROM ALL_TAB_COMMENTS WHERE OWNER=:owner AND TABLE_NAME=:name",
                    {"owner": source.owner, "name": source.object_name},
                )
                comment_row = cur.fetchone()
                comment = f"{name}: {comment_row[0] or ''}" if comment_row else name
                cur.execute(f"COMMENT ON TABLE {table} IS '" + comment.replace("'", "''") + "'")  # nosec B608
                cur.execute(
                    "SELECT COLUMN_NAME, COMMENTS FROM ALL_COL_COMMENTS WHERE OWNER=:owner "
                    "AND TABLE_NAME=:name AND COMMENTS IS NOT NULL",
                    {"owner": source.owner, "name": source.object_name},
                )
                for column, comment in cur.fetchall():
                    cur.execute(
                        f"COMMENT ON COLUMN {table}.{quote(column)} IS '"
                        + comment.replace("'", "''")
                        + "'"
                    )  # nosec B608
                conn.commit()
                persist(run.staging)
            # NOVALIDATE allows existing seed rows whose parents are outside the sample.
            for kind in ("P", "U", "C", "R"):
                for name in ordered_tables(run.staging):
                    stage = run.staging[name]
                    for c in stage["metadata"]["constraints"]:
                        if c["kind"] != kind:
                            continue
                        cols = ",".join(quote(col) for col in c["columns"])
                        if kind in ("P", "U"):
                            clause = f"{'PRIMARY KEY' if kind == 'P' else 'UNIQUE'} ({cols})"
                        elif kind == "C":
                            clause = f"CHECK ({c['condition']})"
                        else:
                            ref = run.staging.get(c["reference"], {}).get("name", c["reference"])
                            ref_cols = ",".join(quote(col) for col in c["ref_columns"])
                            clause = (
                                f"FOREIGN KEY ({cols}) REFERENCES {qualified(ref)} ({ref_cols})"
                            )
                        cur.execute(
                            f"ALTER TABLE {qualified(stage['name'])} ADD {clause} ENABLE NOVALIDATE"
                        )  # nosec B608

    @staticmethod
    def select_sql(stage: dict[str, Any]) -> tuple[str, dict[str, str]]:
        # Seed rows guide the LLM but never appear in preview or get copied to the target.
        binds = {f"s{i}": value for i, value in enumerate(stage["seeds"])}
        where = (
            " WHERE ROWIDTOCHAR(ROWID) NOT IN (" + ",".join(":" + k for k in binds) + ")"
            if binds
            else ""
        )
        columns = ",".join(quote(c[0]) for c in stage["metadata"]["columns"])
        return f"SELECT {columns} FROM {qualified(stage['name'])}{where}", binds  # nosec B608

    @staticmethod
    def check_identity(cur: Any, name: str, object_id: int) -> None:
        identity = parse_object_identity(name)
        cur.execute(
            "SELECT OBJECT_ID FROM ALL_OBJECTS WHERE OWNER=:owner "
            "AND OBJECT_NAME=:name AND OBJECT_TYPE='TABLE'",
            {"owner": identity.owner, "name": identity.object_name},
        )
        row = cur.fetchone()
        if not row or int(row[0]) != object_id:
            raise HTTPException(
                409, "対象表または確認用データが削除・再作成されています。生成し直してください。"
            )

    def snapshot(
        self, cur: Any, stage: dict[str, Any], limit: int = 0
    ) -> tuple[str, int, list[Any]]:
        self.check_identity(cur, stage["name"], stage["object_id"])
        sql, binds = self.select_sql(stage)
        cur.execute(sql + " ORDER BY ROWID", binds)
        checksum = hashlib.sha256()
        count = 0
        preview: list[Any] = []
        while rows := cur.fetchmany(500):
            for row in rows:
                checksum.update(json.dumps(list(row), ensure_ascii=False, default=str).encode())
                checksum.update(b"\n")
                count += 1
                if len(preview) < limit:
                    preview.append([_coerce_result_value(value) for value in row])
        return checksum.hexdigest(), count, preview

    def seal(self, run: SyntheticRun) -> None:
        with self.adapter.connection() as conn, conn.cursor() as cur:
            for target in run.targets:
                stage = run.staging[target.table_name]
                checksum, count, _ = self.snapshot(cur, stage)
                if count != target.loaded_rows:
                    raise HTTPException(409, "生成記録と確認用データの件数が一致しません。")
                stage["checksum"], stage["count"] = checksum, count
        run.review_status = "ready"

    def results(self, run: SyntheticRun, name: str, limit: int) -> dict[str, Any]:
        if run.review_status not in {"ready", "applied"} or name not in run.staging:
            raise HTTPException(409, "確認用データの準備が完了していないか、既に破棄されています。")
        stage = run.staging[name]
        with self.adapter.connection() as conn, conn.cursor() as cur:
            checksum, count, rows = self.snapshot(cur, stage, limit)
        if checksum != stage["checksum"] or count != stage["count"]:
            raise HTTPException(409, "確認用データが変更されています。生成し直してください。")
        columns = [c[0] for c in stage["metadata"]["columns"]]
        return {
            "table_name": name,
            "run_id": run.run_id,
            "runtime": "oracle",
            "preview_digest": checksum,
            "results": {
                "columns": columns,
                "rows": [dict(zip(columns, row, strict=True)) for row in rows],
                "total": count,
                "returned_count": len(rows),
                "has_more": count > len(rows),
            },
            "warnings": [],
            "generated_at": run.finished_at,
        }

    def apply(self, conn: Any, run: SyntheticRun) -> None:
        with conn.cursor() as cur:
            # Lock all targets and stages in a stable order before checking or inserting anything.
            for name in sorted(run.staging):
                cur.execute(f"LOCK TABLE {qualified(name)} IN SHARE ROW EXCLUSIVE MODE NOWAIT")  # nosec B608
                stage = run.staging[name]
                cur.execute(f"LOCK TABLE {qualified(stage['name'])} IN SHARE MODE NOWAIT")  # nosec B608
                self.check_identity(cur, name, run.request["_object_ids"][name])
                if metadata(cur, name) != stage["metadata"]:
                    raise HTTPException(
                        409, f"{name}: 表定義が変更されています。生成し直してください。"
                    )
                checksum, count, _ = self.snapshot(cur, stage)
                if checksum != stage["checksum"] or count != stage["count"]:
                    raise HTTPException(
                        409, "確認用データが変更されています。生成し直してください。"
                    )
            for name in ordered_tables(run.staging):
                stage = run.staging[name]
                sql, binds = self.select_sql(stage)
                columns = ",".join(quote(c[0]) for c in stage["metadata"]["columns"])
                cur.execute(f"INSERT INTO {qualified(name)} ({columns}) {sql}", binds)  # nosec B608
                if cur.rowcount != stage["count"]:
                    raise HTTPException(409, "適用件数を確認できません。変更を取り消します。")

    def cleanup(self, run: SyntheticRun) -> None:
        prefix = f"{self.adapter.settings.oracle_user.upper()}.NL2SQL_SP_"
        prefix += run.run_id.replace("-", "").upper()
        allowed = {f"{prefix}_{i}" for i in range(len(run.targets))}
        with self.adapter.connection() as conn, conn.cursor() as cur:
            for stage in run.staging.values():
                if stage["name"] not in allowed:
                    raise HTTPException(409, "確認用データの所有関係を確認できません。")
                identity = parse_object_identity(stage["name"])
                cur.execute(
                    "SELECT OBJECT_ID FROM ALL_OBJECTS WHERE OWNER=:owner "
                    "AND OBJECT_NAME=:name AND OBJECT_TYPE='TABLE'",
                    {"owner": identity.owner, "name": identity.object_name},
                )
                row = cur.fetchone()
                if not row:
                    continue
                if not stage.get("object_id") or int(row[0]) != stage["object_id"]:
                    raise HTTPException(
                        409, "確認用テーブルの識別情報が変わっているため削除できません。"
                    )
                try:
                    # Table names are reserved, random and persisted before creation; no user names.
                    cur.execute(f"DROP TABLE {qualified(stage['name'])} CASCADE CONSTRAINTS PURGE")  # nosec B608
                except Exception as exc:
                    if "ORA-00942" not in str(exc):
                        raise

    def execution_stopped(self, run: SyntheticRun) -> bool:
        """Recover a lost worker only after Oracle confirms the original session is gone."""
        if not run.session:
            return False
        with self.adapter.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM V$SESSION WHERE SID=:sid AND SERIAL#=:serial",
                {"sid": run.session["sid"], "serial": run.session["serial"]},
            )
            return cur.fetchone() is None
