"""判定の校正（#158 で削除）を消す migration のテスト。

SQL の形は ``test_oracle_schema_cli.py`` が確かめる。ここでは実 Oracle で、旧版の形（校正の 4 表・
kind の CHECK の 'calibration'・校正の job の行・権限の行）に戻した DB へ migration を 2 回当て、
消えること・評価の実行の行とほかの権限が残ること・冪等なことを確かめる（未到達なら oracle_db
fixture が skip する）。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest

from app.rag.oracle_schema import oracle_schema_migration_sections, split_sql_statements
from tests import _oracle_test_db

_RETIRE = "20261105_001_retire_evaluation_calibration"
_LEGACY = ("20261026_001_evaluation_calibration", "20261026_002_evaluation_job_calibration_kind")
_TABLES = (
    "RAG_EVALUATION_LABEL_SETS",
    "RAG_EVALUATION_LABEL_ITEMS",
    "RAG_EVALUATION_LABELS",
    "RAG_EVALUATION_CALIBRATIONS",
)
_RETIRED_PERMISSION = "rag.evaluation_calibration.manage"
_KEPT_PERMISSION = "menu.evaluation"


def _apply(cursor: Any, name: str) -> None:
    section = next(item for item in oracle_schema_migration_sections() if item.name == name)
    for statement in split_sql_statements(section.sql):
        cursor.execute(statement)


def _count(cursor: Any, sql: str, **binds: object) -> int:
    cursor.execute(sql, binds)
    return int(cursor.fetchone()[0])


@pytest.fixture
def legacy_calibration_schema(oracle_db: None) -> Iterator[tuple[Any, str, str, str]]:
    """校正の残っていた旧版の形に戻し、後で migration で今の形にそろえ直す。

    実 Oracle のテスト（oracle_db）は xdist でも 1 つの worker で直列に動く（conftest の
    ``xdist_group("oracle")``）ので、表の形を一時的に変えても他のテストと重ならない。
    """
    run_job_id = f"pytest-run-{uuid4().hex}"
    calibration_job_id = f"pytest-cal-{uuid4().hex}"
    role_id = uuid4().hex
    with _oracle_test_db._schema_connection() as connection:
        cursor = connection.cursor()
        try:
            for name in _LEGACY:
                _apply(cursor, name)
            for job_id, kind in ((run_job_id, "run"), (calibration_job_id, "calibration")):
                cursor.execute(
                    "INSERT INTO rag_evaluation_jobs (job_id, kind, status, time_limit_seconds) "
                    "VALUES (:job_id, :kind, 'SUCCEEDED', 60)",
                    {"job_id": job_id, "kind": kind},
                )
            cursor.execute(
                "INSERT INTO rag_evaluation_label_sets "
                "(label_set_id, name, origin, rubric_id, items_sha256) "
                "VALUES (:id, 'pytest', 'jobs', 'pytest', 'pytest')",
                {"id": run_job_id[:64]},
            )
            cursor.execute(
                "INSERT INTO platform_roles (role_id, role_code, display_name) "
                "VALUES (:role_id, :role_code, 'pytest')",
                {"role_id": role_id, "role_code": f"PYTEST_{role_id}"[:64]},
            )
            for code in (_RETIRED_PERMISSION, _KEPT_PERMISSION):
                cursor.execute(
                    "INSERT INTO rag_role_permissions (role_id, permission_code) "
                    "VALUES (:role_id, :code)",
                    {"role_id": role_id, "code": code},
                )
            connection.commit()
            yield cursor, run_job_id, calibration_job_id, role_id
        finally:
            # 失敗しても今の形に戻す（migration は冪等）。テストの行を消す。
            connection.rollback()
            _apply(cursor, _RETIRE)
            cursor.execute(
                "DELETE FROM rag_evaluation_jobs WHERE job_id IN (:run_id, :calibration_id)",
                {"run_id": run_job_id, "calibration_id": calibration_job_id},
            )
            cursor.execute("DELETE FROM platform_roles WHERE role_id = :id", {"id": role_id})
            connection.commit()


def test_retire_evaluation_calibration_migration_on_real_oracle(
    legacy_calibration_schema: tuple[Any, str, str, str],
) -> None:
    cursor, run_job_id, calibration_job_id, role_id = legacy_calibration_schema
    oracledb = pytest.importorskip("oracledb")

    for _ in range(2):  # 2 回目は何もしない（冪等）。
        _apply(cursor, _RETIRE)

        for table in _TABLES:
            assert (
                _count(cursor, "SELECT COUNT(*) FROM user_tables WHERE table_name = :t", t=table)
                == 0
            )
        # 校正の job と権限の行だけを消し、評価の実行の行とほかの権限は残す。
        job_count = "SELECT COUNT(*) FROM rag_evaluation_jobs WHERE job_id = :id"
        assert _count(cursor, job_count, id=calibration_job_id) == 0
        assert _count(cursor, job_count, id=run_job_id) == 1
        permission_count = (
            "SELECT COUNT(*) FROM rag_role_permissions "
            "WHERE role_id = :role_id AND permission_code = :code"
        )
        assert _count(cursor, permission_count, role_id=role_id, code=_RETIRED_PERMISSION) == 0
        assert _count(cursor, permission_count, role_id=role_id, code=_KEPT_PERMISSION) == 1

    # kind の CHECK は 'run' だけを許す。
    with pytest.raises(oracledb.DatabaseError, match="ORA-02290"):
        cursor.execute(
            "INSERT INTO rag_evaluation_jobs (job_id, kind, status, time_limit_seconds) "
            "VALUES (:job_id, 'calibration', 'SUCCEEDED', 60)",
            {"job_id": calibration_job_id},
        )
