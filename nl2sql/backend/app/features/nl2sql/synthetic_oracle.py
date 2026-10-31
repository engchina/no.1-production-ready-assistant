"""生成専用 session と Oracle operation を対応付ける。全体 MAX(id) は使わない。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from .object_identity import parse_object_identity
from .oracle_adapter import OracleNl2SqlAdapter
from .synthetic_models import SyntheticRun, SyntheticTarget


def capture_session(conn: Any) -> dict[str, Any]:
    with conn.cursor() as cur:
        # 監視権限の欠落は、業務表へ書く前に検出する。
        cur.execute("SELECT ID FROM DBA_LOAD_OPERATIONS WHERE 1=0")
        cur.execute(
            "SELECT SID, SERIAL#, USERNAME, SYSTIMESTAMP FROM V$SESSION "
            "WHERE SID=TO_NUMBER(SYS_CONTEXT('USERENV','SID'))"
        )
        row = cur.fetchone()
        if not row:
            raise RuntimeError("Oracle 実行セッションを確認できません。")
        return {
            "sid": int(row[0]),
            "serial": int(row[1]),
            "username": str(row[2]),
            "since": row[3].isoformat(),
        }


def is_prompt_validation_rejection(message: str) -> bool:
    """今回確認した JSON null の引数拒否だけを識別。一般の ORA-20000 は含めない。"""
    match = re.search(
        r"ORA-20000: Missing value for user_prompt in (\{[^\n]+\}) in argument object_list(?:\n|$)",
        message,
    )
    if not match:
        return False
    try:
        argument = json.loads(match.group(1))
    except (ValueError, TypeError):
        return False
    return (
        isinstance(argument, dict) and "user_prompt" in argument and argument["user_prompt"] is None
    )


def inspect_operation(adapter: OracleNl2SqlAdapter, run: SyntheticRun) -> SyntheticRun:
    """別接続から対象 session の operation/chunk を読み、実数だけを反映する。"""
    if not run.session:
        return run
    with adapter.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT ID, STATUS, STATUS_TABLE FROM DBA_LOAD_OPERATIONS "
            "WHERE TYPE='SYNTHETIC_DATA' AND SID=:sid AND SERIAL#=:serial "
            "AND USERNAME=:username AND START_TIME>=:since ORDER BY ID",
            {**run.session, "since": datetime.fromisoformat(run.session["since"])},
        )
        operations = cur.fetchall()
        if not operations:
            # 引数拒否で戻り、対応 operation がなく元 session も終了したものだけを確定する。
            # timeout / 切断 / worker 消失 / 一般的な Oracle エラーの lock は解除しない。
            if (
                run.execution_returned
                and not run.operation_ids
                and all(target.loaded_rows is None for target in run.targets)
                and is_prompt_validation_rejection(run.message)
            ):
                cur.execute(
                    "SELECT STATUS FROM V$SESSION WHERE SID=:sid AND SERIAL#=:serial",
                    {"sid": run.session["sid"], "serial": run.session["serial"]},
                )
                if cur.fetchone() is None:
                    run.status = "failed"
                    run.failure_phase = "validation"
                    for target in run.targets:
                        target.status = "failed"
                        target.loaded_rows = 0
                        target.error = "Oracle に生成要求が受理されませんでした。"
            return run
        # 単一呼出しに対応しない曖昧な記録を流用しない。
        if len(operations) != 1:
            run.status = "unknown"
            run.message = (
                "Oracle 実行記録を一意に特定できません。再生成せず管理者へ確認してください。"
            )
            return run
        operation_id, operation_status, status_table = operations[0]
        if not re.fullmatch(r"SYNTHETIC_DATA\$[0-9]+_STATUS", str(status_table or "")):
            return run
        owner = str(run.session["username"])
        owner = owner.replace('"', '""')
        cur.execute(
            "SELECT NAME, STATUS, ROWS_LOADED, ERROR_CODE, ERROR_MESSAGE "  # nosec B608
            f'FROM "{owner}"."{status_table}"'
        )  # owner is quoted, status table is restricted to Oracle-generated names
        chunks = cur.fetchall()
    run.operation_ids = [int(operation_id)]
    by_table: dict[str, list[Any]] = {}
    for chunk in chunks:
        try:
            name = parse_object_identity(
                str(chunk[0]), default_owner=run.session["username"]
            ).qualified_name
        except ValueError:
            continue
        by_table.setdefault(name, []).append(chunk)
    targets: list[SyntheticTarget] = []
    for target in run.targets:
        name = run.staging.get(target.table_name, {}).get("name", target.table_name)
        rows = by_table.get(name, [])
        target = target.model_copy(deep=True)
        if rows:
            target.loaded_rows = (
                sum(int(row[2] or 0) for row in rows)
                if all(row[2] is not None for row in rows)
                else None
            )
            statuses = {str(row[1]).upper() for row in rows}
            target.status = (
                "running"
                if statuses - {"COMPLETED", "FAILED", "SKIPPED"}
                else (
                    "failed"
                    if "FAILED" in statuses
                    else "skipped"
                    if "SKIPPED" in statuses
                    else "completed"
                )
            )
            target.error = " ".join(
                str(row[4] or f"Oracle error {row[3]}")[:1500] for row in rows if row[3] or row[4]
            )
        targets.append(target)
    run.targets = targets
    done = str(operation_status).upper() in {"COMPLETED", "FAILED"}
    all_finished = all(t.status in {"completed", "failed", "skipped"} for t in targets)
    # 呼出し中にも rows_loaded は見える。全体終端と戻り/回復確認の両方を待つ。
    if done and all_finished and all(t.loaded_rows is not None for t in targets):
        loaded = sum(t.loaded_rows or 0 for t in targets)
        if str(operation_status).upper() == "COMPLETED" and all(
            t.status == "completed" and t.loaded_rows == t.requested_rows for t in targets
        ):
            run.status = "completed"
        elif loaded:
            run.status = "partial"
        elif (
            any(t.status == "failed" for t in targets) or str(operation_status).upper() == "FAILED"
        ):
            run.status = "failed"
        else:
            run.status = "no_data"
        run.message = ""
    elif done:
        run.status = "unknown"
        run.message = "Oracle は処理を終了しましたが、対象表の書込み件数を確認できません。"
    else:
        run.status = "running"
    return run
