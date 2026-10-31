"""検索・回答プロファイルの参照 KB の状態と、KB 一覧の ID 絞り込みの回帰テスト（#302）。

- 検索・回答プロファイルの詳細・一覧で、参照先のアーカイブ済み・存在しない KB を見分けられる。
- KB 一覧は `knowledge_base_ids` でその ID に絞れる（選択済みの名前・状態の解決用）。
"""

import json
from datetime import UTC, datetime

import pytest

from app.clients.oracle import OracleClient, _oracle_knowledge_base_where
from app.schemas.knowledge_base import KnowledgeBaseStatus
from tests.test_oracle_adapter import FakeOraclePool, _oci_settings, _run_inline


def _search_answer_profile_row(
    search_answer_profile_id: str, knowledge_base_ids: list[str]
) -> dict[str, object]:
    return {
        "search_answer_profile_id": search_answer_profile_id,
        "tenant_id_hash": None,
        "name": f"検索・回答プロファイル {search_answer_profile_id}",
        "description": None,
        "status": "ACTIVE",
        "profile_config": json.dumps({"version": 1, "knowledge_base_ids": knowledge_base_ids}),
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 2, tzinfo=UTC),
        "archived_at": None,
    }


def _ref_row(knowledge_base_id: str, name: str, status: str) -> dict[str, object]:
    return {"knowledge_base_id": knowledge_base_id, "name": name, "status": status}


async def test_search_answer_profile_detail_marks_archived_and_missing_knowledge_bases() -> None:
    """詳細はアーカイブ済みの KB を status 付きで返し、存在しない KB の ID を分けて返す。"""
    pool = FakeOraclePool(
        execute_results=[
            [_search_answer_profile_row("bv-1", ["kb-active", "kb-archived", "kb-missing"])],
            [
                _ref_row("kb-archived", "旧規程", "ARCHIVED"),
                _ref_row("kb-active", "社内規程", "ACTIVE"),
            ],
        ]
    )
    client = OracleClient(settings=_oci_settings(), pool=pool, db_call_runner=_run_inline)

    detail = await client.get_search_answer_profile("bv-1")

    assert detail is not None
    assert [(ref.id, ref.name, ref.status) for ref in detail.knowledge_bases] == [
        ("kb-active", "社内規程", KnowledgeBaseStatus.ACTIVE),
        ("kb-archived", "旧規程", KnowledgeBaseStatus.ARCHIVED),
    ]
    assert detail.missing_knowledge_base_ids == ["kb-missing"]
    assert detail.knowledge_base_count == 3
    assert detail.archived_knowledge_base_count == 1
    assert detail.missing_knowledge_base_count == 1
    lookup = pool.connection.calls[-1]
    assert "kb.status" in lookup.statement
    # アーカイブ済みも名前を出すため、状態では絞らない。
    assert "kb.status =" not in lookup.statement


async def test_profile_list_counts_archived_and_missing_references_in_one_lookup() -> None:
    """一覧はページ内の参照 KB をまとめて 1 回で
    解決し、検索・回答プロファイルごとに件数を数える。"""
    pool = FakeOraclePool(
        execute_results=[
            [
                _search_answer_profile_row("bv-1", ["kb-active", "kb-archived"]),
                _search_answer_profile_row("bv-2", ["kb-active", "kb-missing"]),
                _search_answer_profile_row("bv-3", ["kb-active"]),
            ],
            [
                _ref_row("kb-active", "社内規程", "ACTIVE"),
                _ref_row("kb-archived", "旧規程", "ARCHIVED"),
            ],
        ]
    )
    client = OracleClient(settings=_oci_settings(), pool=pool, db_call_runner=_run_inline)

    views = await client.list_search_answer_profiles(limit=20)

    counts = {
        view.id: (
            view.knowledge_base_count,
            view.archived_knowledge_base_count,
            view.missing_knowledge_base_count,
        )
        for view in views
    }
    assert counts == {"bv-1": (2, 1, 0), "bv-2": (2, 0, 1), "bv-3": (1, 0, 0)}
    lookups = [
        call for call in pool.connection.calls if "FROM rag_knowledge_bases kb" in call.statement
    ]
    assert len(lookups) == 1
    assert sorted(
        str(value) for key, value in lookups[0].parameters.items() if key.startswith("ref_kb_id")
    ) == ["kb-active", "kb-archived", "kb-missing"]


async def test_search_answer_profile_list_without_references_skips_lookup() -> None:
    """参照 KB がなければ KB の解決をしない。"""
    pool = FakeOraclePool(execute_results=[[_search_answer_profile_row("bv-1", [])]])
    client = OracleClient(settings=_oci_settings(), pool=pool, db_call_runner=_run_inline)

    views = await client.list_search_answer_profiles(limit=20)

    assert views[0].missing_knowledge_base_count == 0
    assert len(pool.connection.calls) == 1
    statement = pool.connection.calls[0].statement
    order = statement[statement.index("ORDER BY") :]
    assert "updated_at DESC" not in order
    assert "CASE WHEN UPPER(bv.name) = 'DEFAULT' THEN 0 ELSE 1 END" in order
    assert "JAPANESE_M_CI" in order
    assert order.index("bv.search_answer_profile_id ASC") < order.index("OFFSET")


def test_knowledge_base_where_filters_by_ids() -> None:
    """ID の指定は IN 条件になり、空の指定はどれにも一致しない。"""
    where_sql, binds = _oracle_knowledge_base_where(knowledge_base_ids=["kb-1", "kb-2", "kb-1"])

    assert "kb.knowledge_base_id IN (:filter_kb_id_0, :filter_kb_id_1)" in where_sql
    assert binds["filter_kb_id_0"] == "kb-1"
    assert binds["filter_kb_id_1"] == "kb-2"

    empty_sql, _ = _oracle_knowledge_base_where(knowledge_base_ids=[])
    assert "1 = 0" in empty_sql

    unfiltered_sql, _ = _oracle_knowledge_base_where()
    assert "knowledge_base_id IN" not in unfiltered_sql


async def test_update_archived_search_answer_profile_is_rejected() -> None:
    """アーカイブ済みの検索・回答プロファイルは更新しない（画面の読み取り専用と同じ。#961）。"""
    row = _search_answer_profile_row("bv-old", ["kb-1"]) | {"status": "ARCHIVED"}
    pool = FakeOraclePool(execute_results=[[], [row]])
    client = OracleClient(settings=_oci_settings(), pool=pool, db_call_runner=_run_inline)

    with pytest.raises(ValueError, match="アーカイブ済みの検索・回答プロファイルは変更できません"):
        await client.update_search_answer_profile(
            "bv-old", description="新しい説明", update_fields={"description"}
        )

    assert not any(
        "UPDATE rag_search_answer_profiles" in call.statement for call in pool.connection.calls
    )
    assert pool.connection.commits == 0
