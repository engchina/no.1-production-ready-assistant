"""近似の vector 検索の再現率の測定(#74)の決定論の部分と、検索の SQL の形を確かめる。

実 Oracle での索引の利用・再現率は ``test_vector_prefilter_oracle.py`` と
``python -m app.rag.vector_recall_cli`` が確かめる。
"""

from __future__ import annotations

import pytest

from app.clients.oracle import (
    _ACTIVE_CHUNK_PREFILTER_SQL,
    _oracle_retrieval_where,
    vector_search_statement,
)
from app.config import Settings
from app.rag.vector_recall_cli import (
    FORCE_VECTOR_INDEX_HINT,
    Hit,
    compare_hits,
    force_vector_index_hint,
    main,
    percentile,
    vector_plan_operations,
)


def _hits(*pairs: tuple[str, float]) -> list[Hit]:
    return [Hit(chunk_id, distance) for chunk_id, distance in pairs]


def test_recall_is_one_when_approx_matches_exact() -> None:
    exact = _hits(("a", 0.1), ("b", 0.2), ("c", 0.3))
    result = compare_hits(exact, exact, top_k=3)
    assert result.recall == 1.0
    assert result.strict_overlap == 1.0


def test_recall_counts_missing_neighbours() -> None:
    exact = _hits(("a", 0.1), ("b", 0.2), ("c", 0.3), ("d", 0.4))
    approx = _hits(("a", 0.1), ("c", 0.3), ("x", 0.5), ("y", 0.6))
    result = compare_hits(approx, exact, top_k=4)
    assert result.recall == 0.5
    assert result.strict_overlap == 0.5


def test_tie_swaps_at_the_boundary_are_not_misses() -> None:
    """exact の k 番目と同じ距離の別の行を返しても漏れと数えない(ID の一致は下がる)。"""
    exact = _hits(("a", 0.1), ("b", 0.5), ("c", 0.5))
    approx = _hits(("a", 0.1), ("b", 0.5), ("z", 0.5))
    result = compare_hits(approx, exact, top_k=3)
    assert result.recall == 1.0
    assert result.strict_overlap == pytest.approx(2 / 3)


def test_fewer_candidates_than_k_use_the_candidate_count() -> None:
    exact = _hits(("a", 0.1), ("b", 0.2))
    assert compare_hits(exact, exact, top_k=10).recall == 1.0
    assert compare_hits(exact[:1], exact, top_k=10).recall == 0.5


def test_returning_fewer_rows_than_k_is_a_miss() -> None:
    """近似の検索が k 件に満たない(索引の後で行が捨てられた)のは漏れ。"""
    exact = _hits(*[(f"c{n}", n / 100) for n in range(10)])
    result = compare_hits(exact[:3], exact, top_k=10)
    assert result.recall == 0.3
    assert result.approx_count == 3


def test_empty_allowed_set_has_no_recall() -> None:
    result = compare_hits([], [], top_k=10)
    assert result.recall is None
    assert result.strict_overlap is None
    assert result.exact_count == 0


def test_duplicate_rows_are_counted_once() -> None:
    exact = _hits(("a", 0.1), ("b", 0.2))
    approx = _hits(("a", 0.1), ("a", 0.1))
    result = compare_hits(approx, exact, top_k=2)
    assert result.recall == 0.5
    assert result.approx_duplicates == 1


def test_percentile_interpolates() -> None:
    assert percentile([], 0.5) is None
    assert percentile([5.0], 0.95) == 5.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5
    assert percentile([float(n) for n in range(1, 101)], 0.95) == pytest.approx(95.05)


def test_vector_plan_operations_reads_display_cursor_rows() -> None:
    plan = [
        "| Id  | Operation                         | Name                          |",
        "|   0 | SELECT STATEMENT                  |                               |",
        "|*  6 |       FILTER                      |                               |",
        "|   7 |        VECTOR INDEX HNSW SCAN PRE-FILTER | RAG_CHUNKS_EMBEDDING_HNSW_IDX |",
        "|*  8 |         VECTOR INDEX HNSW SCAN PRE-FILTER | RAG_CHUNKS_EMBEDDING_HNSW_IDX |",
        "|  21 |                TABLE ACCESS FULL  | RAG_CHUNKS                    |",
    ]
    assert vector_plan_operations(plan) == ["VECTOR INDEX HNSW SCAN PRE-FILTER"]
    assert vector_plan_operations(["NOTE: cannot fetch plan"]) == []


def _statement(*, exact: bool = False, filters: dict[str, str] | None = None) -> str:
    statement, _binds = vector_search_statement(
        Settings(), None, top_k=10, filters=filters or {}, exact=exact
    )
    return statement


@pytest.mark.usefixtures("system_access")
def test_vector_search_keeps_the_active_chunk_condition_in_the_prefilter_form() -> None:
    """vector の検索は active な chunk_set の条件を OR の無い IN にする(#74)。

    相関の EXISTS を OR で結ぶと、索引が k 件を返した後で行を捨てる計画になる。
    """
    statement = _statement()
    assert _ACTIVE_CHUNK_PREFILTER_SQL.strip() in statement
    assert "WHERE active_cs.chunk_set_id = c.chunk_set_id" not in statement
    assert "FETCH APPROX FIRST 10 ROWS ONLY WITH TARGET ACCURACY" in statement


@pytest.mark.usefixtures("system_access")
def test_other_retrieval_reads_keep_the_exists_form() -> None:
    """vector 以外(keyword・ID で読む入口)の条件は変えない。"""
    where_sql, _binds = _oracle_retrieval_where({})
    assert "WHERE active_cs.chunk_set_id = c.chunk_set_id" in where_sql
    assert _ACTIVE_CHUNK_PREFILTER_SQL.strip() not in where_sql


@pytest.mark.usefixtures("system_access")
def test_explicit_chunk_set_filter_skips_the_active_condition() -> None:
    statement = _statement(filters={"chunk_set_id": "cs-1"})
    assert _ACTIVE_CHUNK_PREFILTER_SQL.strip() not in statement
    assert "c.chunk_set_id = :filter_chunk_set_id" in statement


@pytest.mark.usefixtures("system_access")
def test_exact_statement_differs_only_in_the_fetch_clause() -> None:
    approx = _statement()
    exact = _statement(exact=True)
    assert "FETCH EXACT FIRST 10 ROWS ONLY" in exact
    assert "APPROX" not in exact
    assert (
        approx.replace(
            "FETCH APPROX FIRST 10 ROWS ONLY WITH TARGET ACCURACY 98",
            "FETCH EXACT FIRST 10 ROWS ONLY",
        )
        == exact
    )


@pytest.mark.usefixtures("system_access")
def test_force_vector_index_hint_targets_the_inner_query() -> None:
    hinted = force_vector_index_hint(_statement())
    assert hinted.lstrip().startswith(f"SELECT * FROM ( SELECT {FORCE_VECTOR_INDEX_HINT}")
    with pytest.raises(ValueError):
        force_vector_index_hint("SELECT 1 FROM dual")


def test_main_refuses_without_test_schema_confirmation(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "--yes-test-schema" in capsys.readouterr().err
