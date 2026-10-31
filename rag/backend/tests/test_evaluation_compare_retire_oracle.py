"""品質評価の設定比較（#2088 で削除）の名残を消す migration のテスト（#2099）。

SQL の形は ``test_oracle_schema_cli.py`` が確かめる。ここでは実 Oracle で、旧版の形
（設定比較の列・索引・kind の CHECK の 'compare'・設定比較の job の行）に戻した表へ migration を
2 回当て、消えること・評価の実行の行が残ること・冪等なことを確かめる（未到達なら
oracle_db fixture が skip する）。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest

from app.rag.oracle_schema import oracle_schema_migration_sections, split_sql_statements
from tests import _oracle_test_db

_MIGRATION = "20261016_001_retire_evaluation_compare"


def _migration_statements() -> list[str]:
    section = next(item for item in oracle_schema_migration_sections() if item.name == _MIGRATION)
    return split_sql_statements(section.sql)


def _count(cursor: Any, sql: str, **binds: object) -> int:
    cursor.execute(sql, binds)
    row = cursor.fetchone()
    return int(row[0])


def _column_exists(cursor: Any, table: str, column: str) -> bool:
    return (
        _count(
            cursor,
            "SELECT COUNT(*) FROM user_tab_columns WHERE table_name = :t AND column_name = :c",
            t=table,
            c=column,
        )
        > 0
    )


def _apply_migration(cursor: Any) -> None:
    for statement in _migration_statements():
        cursor.execute(statement)


@pytest.fixture
def legacy_compare_schema(oracle_db: None) -> Iterator[tuple[Any, str, str]]:
    """評価の表を設定比較の残っていた旧版の形に戻し、後で migration で今の形にそろえ直す。

    実 Oracle のテスト（oracle_db）は xdist でも 1 つの worker で直列に動く（conftest の
    ``xdist_group("oracle")``）ので、表の形を一時的に変えても他のテストと重ならない。
    """
    run_job_id = f"pytest-run-{uuid4().hex}"
    compare_job_id = f"pytest-compare-{uuid4().hex}"
    with _oracle_test_db._schema_connection() as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                "ALTER TABLE rag_evaluation_jobs ADD (current_experiment_id VARCHAR2(80 CHAR))"
            )
            cursor.execute(
                "ALTER TABLE rag_evaluation_jobs DROP CONSTRAINT rag_evaluation_jobs_kind_ck"
            )
            cursor.execute(
                "ALTER TABLE rag_evaluation_jobs ADD CONSTRAINT rag_evaluation_jobs_kind_ck "
                "CHECK (kind IN ('run', 'compare'))"
            )
            cursor.execute("ALTER TABLE rag_evaluation_runs ADD (best_experiment_id VARCHAR2(80))")
            cursor.execute(
                "CREATE INDEX rag_evaluation_runs_best_experiment_idx "
                "ON rag_evaluation_runs (best_experiment_id)"
            )
            for job_id, kind in ((run_job_id, "run"), (compare_job_id, "compare")):
                cursor.execute(
                    "INSERT INTO rag_evaluation_jobs (job_id, kind, status, time_limit_seconds) "
                    "VALUES (:job_id, :kind, 'SUCCEEDED', 60)",
                    {"job_id": job_id, "kind": kind},
                )
            connection.commit()
            yield cursor, run_job_id, compare_job_id
        finally:
            # 失敗しても今の形に戻す（migration は冪等）。テストの行を消す。
            connection.rollback()
            _apply_migration(cursor)
            cursor.execute(
                "DELETE FROM rag_evaluation_jobs WHERE job_id IN (:run_job_id, :compare_job_id)",
                {"run_job_id": run_job_id, "compare_job_id": compare_job_id},
            )
            connection.commit()


def test_retire_evaluation_compare_migration_on_real_oracle(
    legacy_compare_schema: tuple[Any, str, str],
) -> None:
    cursor, run_job_id, compare_job_id = legacy_compare_schema
    oracledb = pytest.importorskip("oracledb")

    for _ in range(2):  # 2 回目は何もしない（冪等）。
        _apply_migration(cursor)

        assert not _column_exists(cursor, "RAG_EVALUATION_JOBS", "CURRENT_EXPERIMENT_ID")
        assert not _column_exists(cursor, "RAG_EVALUATION_RUNS", "BEST_EXPERIMENT_ID")
        assert (
            _count(
                cursor,
                "SELECT COUNT(*) FROM user_indexes WHERE index_name = :name",
                name="RAG_EVALUATION_RUNS_BEST_EXPERIMENT_IDX",
            )
            == 0
        )
        # 設定比較の job の行だけを消し、評価の実行の行は残す。
        assert (
            _count(
                cursor,
                "SELECT COUNT(*) FROM rag_evaluation_jobs WHERE job_id = :id",
                id=compare_job_id,
            )
            == 0
        )
        assert (
            _count(
                cursor, "SELECT COUNT(*) FROM rag_evaluation_jobs WHERE job_id = :id", id=run_job_id
            )
            == 1
        )

    # kind の CHECK は 'run' だけを許す。
    with pytest.raises(oracledb.DatabaseError, match="ORA-02290"):
        cursor.execute(
            "INSERT INTO rag_evaluation_jobs (job_id, kind, status, time_limit_seconds) "
            "VALUES (:job_id, 'compare', 'SUCCEEDED', 60)",
            {"job_id": compare_job_id},
        )
