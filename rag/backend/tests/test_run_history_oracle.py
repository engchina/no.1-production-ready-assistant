"""実行履歴（#1890）の実 Oracle AI Database の結合テスト（保存・見え方・詳細・期限切れの削除）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pr_system_settings.auth.visibility import RunExecutorFilter

from app.clients.oracle import OracleClient
from app.clients.oracle_run_history import RunHistoryStore
from app.rag.request_context import (
    AuditRequestContext,
    reset_audit_request_context,
    set_audit_request_context,
)


@pytest.mark.usefixtures("oracle_db")
async def test_run_history_round_trip_on_real_oracle() -> None:
    """実 Oracle AI Database で、実行の保存・見え方の絞り込み・詳細・期限切れの削除ができる。"""
    from app.clients.oracle import _execute_count

    oracle = OracleClient()
    store = RunHistoryStore(oracle)
    run_ids = ("pytest-run-history-a", "pytest-run-history-b")

    async def cleanup() -> None:
        # テスト用 DB の後始末(cleanup_to_baseline)は文書と KB だけなので、自分で消す。
        await oracle._run_transaction(  # noqa: SLF001
            lambda connection: _execute_count(
                connection,
                "DELETE FROM rag_run_history WHERE run_id IN (:run_a, :run_b)",
                {"run_a": run_ids[0], "run_b": run_ids[1]},
            )
        )

    def context(user: str, *, all_users: bool = False) -> AuditRequestContext:
        return AuditRequestContext(
            request_id="pytest-run-history",
            user_id_hash=user * 64,
            user_uuid=f"uuid-{user}",
            all_users_visible=all_users,
        )

    usage = {
        "models": ["model-a"],
        "requests": 3,
        "input_tokens": 12000,
        "output_tokens": 345,
        "total_tokens": 12345,
        "measurement": "partial",
        "measured_requests": 2,
    }

    await cleanup()
    try:
        for run_id, user in zip(run_ids, ("a", "b"), strict=True):
            token = set_audit_request_context(context(user))
            try:
                await store.save_run(
                    {
                        "run_id": run_id,
                        "surface": "chat" if user == "a" else "search",
                        "search_answer_profile_id": None,
                        "question": f"受注の登録方法は？ {user}" + "あ" * 400,
                        "answer_generated": user == "a",
                        "status": "success",
                        "verification": "verified" if user == "a" else None,
                        "citation_count": 2,
                        "elapsed_ms": 1234.5,
                        "stages": {
                            "stages": [{"stage": "answer", "outcome": "success"}],
                            # 利用量（#3419）は a の実行だけ。b は記録の無い実行（未計測）。
                            **(
                                {"usage": usage, "validation_execution": "timed_out"}
                                if user == "a"
                                else {}
                            ),
                        },
                        "citations": None if user == "a" else [{"chunk_id": "c1"}],
                    }
                )
            finally:
                reset_audit_request_context(token)

        token = set_audit_request_context(context("a"))
        try:
            rows, total = await store.list_runs(
                query="受注の登録", surface=None, status=None, limit=10, offset=0
            )
            own_ids = [row["run_id"] for row in rows if row["run_id"] in run_ids]
            hidden = await store.get_run(run_ids[1])
            own = await store.get_run(run_ids[0])
        finally:
            reset_audit_request_context(token)
        token = set_audit_request_context(context("z", all_users=True))
        try:
            admin_rows, _ = await store.list_runs(
                query="受注の登録", surface="search", status="success", limit=10, offset=0
            )
            # 期間（開始日時が since 以後）と、実行 ID の完全一致の検索（#1955）。
            now = datetime.now(UTC)
            recent_rows, _ = await store.list_runs(
                query=run_ids[1],
                surface=None,
                status=None,
                since=now - timedelta(hours=1),
                limit=10,
                offset=0,
            )
            future_rows, future_total = await store.list_runs(
                query=run_ids[1],
                surface=None,
                status=None,
                since=now + timedelta(hours=1),
                limit=10,
                offset=0,
            )
            other = await store.get_run(run_ids[1])
            purged = await store.purge_runs(3650)
        finally:
            reset_audit_request_context(token)

        assert own_ids == [run_ids[0]]
        assert total >= 1
        assert len(str(rows[0]["question"])) == 300
        assert hidden is None
        assert own is not None
        assert own["validation_execution"] == "timed_out"
        saved_stages = own["stages_json"]
        assert isinstance(saved_stages, dict)
        assert saved_stages["validation_execution"] == "timed_out"
        assert (
            next(row for row in rows if row["run_id"] == run_ids[0])["validation_execution"]
            == "timed_out"
        )
        assert other is not None and other.get("validation_execution") is None
        assert own["user_uuid"] == "uuid-a"
        assert str(own["question"]).endswith("あ")
        assert len(str(own["question"])) > 300
        assert own["stages_json"] == {
            "stages": [{"stage": "answer", "outcome": "success"}],
            "usage": usage,
            "validation_execution": "timed_out",
        }
        # 一覧は利用量の合計と計測の状態だけを JSON_VALUE で読む（#3466）。記録の無い実行は NULL。
        own_row = next(row for row in rows if row["run_id"] == run_ids[0])
        assert (own_row["usage_total_tokens"], own_row["usage_measurement"]) == (12345, "partial")
        admin_row = next(row for row in admin_rows if row["run_id"] == run_ids[1])
        assert (admin_row["usage_total_tokens"], admin_row["usage_measurement"]) == (None, None)
        assert [row["run_id"] for row in admin_rows if row["run_id"] in run_ids] == [run_ids[1]]
        assert [row["run_id"] for row in recent_rows] == [run_ids[1]]
        assert (future_rows, future_total) == ([], 0)
        assert other is not None
        assert other["citations_json"] == [{"chunk_id": "c1"}]
        # 作ったばかりの実行は保持期間の内側なので消えない。
        assert purged >= 0
    finally:
        await cleanup()


@pytest.mark.usefixtures("oracle_db")
async def test_run_history_shows_runs_to_the_executor_and_the_conversation_owner() -> None:
    """持ち主の会話で system_admin が実行した行は、持ち主にも見える（#1954）。

    一般の利用者（alice）は、自分の実行と、自分の会話で system_admin が実行した実行を見る。
    会話の無い実行（RAG 検索）は実行した人だけ。別の利用者（bob）には見えない。「実行者」の
    絞り込みと件数も同じ条件で数える。
    """
    from app.clients.oracle import _execute_count

    oracle = OracleClient()
    store = RunHistoryStore(oracle)
    question = "pytest 実行者の見え方 #1954"
    admin_in_alice = "pytest-run-executor-admin-in-alice"
    admin_search = "pytest-run-executor-admin-search"
    alice_own = "pytest-run-executor-alice-own"

    def context(user: str, *, all_users: bool = False) -> AuditRequestContext:
        return AuditRequestContext(
            request_id="pytest-run-history-executor",
            user_id_hash=user * 64,
            user_uuid=f"uuid-{user}",
            all_users_visible=all_users,
        )

    async def save(run_id: str, *, conversation_id: str | None) -> None:
        await store.save_run(
            {
                "run_id": run_id,
                "surface": "chat" if conversation_id else "search",
                "search_answer_profile_id": None,
                "question": question,
                "status": "success",
                "conversation_id": conversation_id,
                "stages": {"stages": []},
            }
        )

    async def cleanup() -> None:
        await oracle._run_transaction(  # noqa: SLF001
            lambda connection: _execute_count(
                connection,
                "DELETE FROM rag_run_history WHERE run_id IN (:run_a, :run_b, :run_c)",
                {"run_a": admin_in_alice, "run_b": admin_search, "run_c": alice_own},
            )
        )

    async def visible(user: str, executor: RunExecutorFilter = "all") -> tuple[list[str], int]:
        token = set_audit_request_context(context(user))
        try:
            rows, total = await store.list_runs(
                query=question,
                surface=None,
                status=None,
                limit=10,
                offset=0,
                executor=executor,
            )
        finally:
            reset_audit_request_context(token)
        return sorted(str(row["run_id"]) for row in rows), total

    async def detail(user: str, run_id: str) -> dict[str, object] | None:
        token = set_audit_request_context(context(user))
        try:
            return await store.get_run(run_id)
        finally:
            reset_audit_request_context(token)

    await cleanup()
    profile = await oracle.ensure_default_search_answer_profile()
    token = set_audit_request_context(context("a"))
    try:
        conversation = await oracle.create_conversation(search_answer_profile_id=profile.id)
        await save(alice_own, conversation_id=None)
    finally:
        reset_audit_request_context(token)
    try:
        token = set_audit_request_context(context("z", all_users=True))
        try:
            await save(admin_in_alice, conversation_id=conversation.id)
            await save(admin_search, conversation_id=None)
        finally:
            reset_audit_request_context(token)

        alice_all = await visible("a")
        alice_mine = await visible("a", "mine")
        alice_others = await visible("a", "others")
        bob_all = await visible("b")
        owner_detail = await detail("a", admin_in_alice)
        hidden_search = await detail("a", admin_search)
        bob_detail = await detail("b", admin_in_alice)
    finally:
        await cleanup()
        token = set_audit_request_context(context("a"))
        try:
            await oracle.delete_conversation(conversation.id)
        finally:
            reset_audit_request_context(token)

    assert alice_all == (sorted([admin_in_alice, alice_own]), 2)
    assert alice_mine == ([alice_own], 1)
    assert alice_others == ([admin_in_alice], 1)
    assert bob_all == ([], 0)
    assert owner_detail is not None
    assert owner_detail["user_uuid"] == "uuid-z"
    assert owner_detail["conversation_owner_hash"] == "a" * 64
    assert hidden_search is None
    assert bob_detail is None
