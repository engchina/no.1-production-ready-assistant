"""実行履歴（チャット・RAG 検索の実行の一覧と詳細。#1890）。

- 記録: チャットと RAG 検索の画面の実行だけを 1 行で残す（安全チェックで止めた質問は保存しない、
  根拠の検索だけ（`retrieval_only`）は残さない、記録の失敗で検索・回答を止めない）。
- 見え方（#1889・#1954）: system_admin は全員の実行。それ以外は自分が実行した実行と、自分の会話で
  他の人（system_admin）が実行した実行。一覧・件数・詳細は Oracle の SQL の条件
  （`r.user_id_hash = … OR r.conversation_id IN (持ち主の会話)`）で絞る。実行者の表示名は
  見る人を問わず返し、「実行者」（すべて / 自分が実行 / 他の人が実行）で絞れる。条件どおりに
  行が絞られることは実 Oracle の結合テスト（`test_run_history_oracle.py`）で確かめる。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from pr_backend_core.observability.model_usage import run_usage_totals
from pytest import MonkeyPatch
from rag_engine.resources.tracing import observe_call

from app.api.routes import history as history_route
from app.api.routes.search import _run_model_id, _search_run_recorder
from app.clients.oracle import OracleClient
from app.clients.oracle_run_history import RunHistoryStore, run_history_list_where
from app.config import EnterpriseAiConfiguredModel, Settings, get_settings
from app.main import app
from app.rag import run_history as run_history_module
from app.rag.guardrails import GuardrailResult
from app.rag.pipeline import RagPipeline
from app.rag.request_context import (
    AuditRequestContext,
    _header_hash,
    reset_audit_request_context,
    set_audit_request_context,
)
from app.rag.run_history import RunRecorder, answer_verification, search_run_conditions
from app.schemas.search import SearchRequest, StorageFailure
from tests.security_support import enable_production_auth, login, login_configured_admin
from tests.span_progress_support import RecordingTrace
from tests.support import AsgiTestClient
from tests.test_answer_engine import FakeGenAi, FakeOracle, _fake_llm
from tests.test_oracle_adapter import FakeOraclePool, _run_inline

client = AsgiTestClient(app)

OWNER_PREDICATE = "r.user_id_hash = :run_owner_user_id_hash"
# 持ち主の会話で他の人（system_admin）が実行した行（#1954）。
OWNER_CONVERSATION_PREDICATE = (
    "r.conversation_id IN (SELECT oc.conversation_id FROM rag_conversations oc"
    " WHERE oc.user_id_hash = :run_owner_user_id_hash"
)
MINE_PREDICATE = "r.user_id_hash = :run_executor_user_id_hash"
OTHERS_PREDICATE = "(r.user_id_hash <> :run_executor_user_id_hash OR r.user_id_hash IS NULL)"


class RecordingStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.records: list[dict[str, Any]] = []
        self.purged: list[int] = []
        self.fail = fail

    async def save_run(self, record: Mapping[str, object]) -> None:
        if self.fail:
            raise RuntimeError("db down")
        self.records.append(dict(record))

    async def purge_runs(self, retention_days: int) -> int:
        self.purged.append(retention_days)
        return 0


@pytest.fixture(autouse=True)
def _reset_purge_clock(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(run_history_module, "_last_purge_at", None)


@contextmanager
def _scope(
    *,
    user_id_hash: str | None = None,
    all_users_visible: bool = False,
    search_answer_profile_ids: set[str] | None = None,
) -> Iterator[None]:
    token = set_audit_request_context(
        AuditRequestContext(
            request_id="run-history-test",
            user_id_hash=user_id_hash,
            all_users_visible=all_users_visible,
            allowed_search_answer_profile_ids=(
                None if search_answer_profile_ids is None else frozenset(search_answer_profile_ids)
            ),
        )
    )
    try:
        yield
    finally:
        reset_audit_request_context(token)


# ---- 回答の検証 ----


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["addressed", "addressed"], "verified"),
        (["addressed", "partial"], "partial"),
        (["addressed", "missing"], "partial"),
        (["addressed", "unknown"], "partial"),
        (["unknown"], "unverified"),
        ([], "unverified"),
    ],
    ids=["all-addressed", "partial", "missing", "unknown-mixed", "all-unknown", "no-requests"],
)
def test_answer_verification_reads_the_audited_requests(statuses: list[str], expected: str) -> None:
    diagnostics = {
        "envelope": {"requests": [{"id": str(i), "status": s} for i, s in enumerate(statuses)]}
    }
    assert answer_verification(diagnostics) == expected


def test_answer_verification_without_diagnostics() -> None:
    assert answer_verification(None) is None
    # 回答の構造が無い回答（承認済み FAQ・古い記録）は確かめていない。
    assert answer_verification({"outcome": "answered"}) == "unverified"


# ---- 記録 ----


async def test_recorder_saves_summary_trace_record_and_purges_once() -> None:
    """実行の要約と、詳細の処理の経過（表を引く trace id と工程の詳細。#3226）を保存する。"""
    store = RecordingStore()
    progress = {"trace_id": "a" * 32, "trace_dropped_spans": 0, "span_details": {}}
    recorder = RunRecorder(
        surface="chat",
        settings=Settings(rag_answer_record_retention_days=30),
        store=store,
        conversation_id="conv-1",
        message_id="msg-1",
        model_id="model-a",
        trace_source=lambda: progress,
    )
    callback = recorder.observe(None)

    class Progress:
        def __init__(self, stage: str, outcome: str) -> None:
            self.stage, self.outcome, self.elapsed_ms = stage, outcome, 0.0

    await callback(Progress("answer", "started"))
    await callback(Progress("answer", "success"))
    await recorder.record(
        run_id="trace-1",
        question="受注の登録方法は？",
        search_answer_profile_id="bv-1",
        answer_generated=True,
        status="success",
        elapsed_ms=1500.0,
        answer_diagnostics={
            "outcome": "answered",
            "confidence": "high",
            "needs_human_review": False,
            "envelope": {"requests": [{"status": "addressed"}]},
            "execution_steps": [{"name": "文書検索", "status": "complete"}],
        },
    )
    await recorder.record(
        run_id="trace-2",
        question=None,
        search_answer_profile_id="bv-1",
        answer_generated=True,
        status="blocked",
        elapsed_ms=10.0,
    )

    first, blocked = store.records
    assert first["surface"] == "chat"
    assert first["conversation_id"] == "conv-1"
    assert first["message_id"] == "msg-1"
    assert first["model_id"] == "model-a"
    assert first["answer_outcome"] == "answered"
    assert first["verification"] == "verified"
    assert first["confidence"] == "high"
    # 回答を作った実行の根拠は回答の記録にあるので、ここには持たない。
    assert first["citations"] is None
    # 処理の経過は表を引く値だけ（段のイベント・trace の要約は持たない）。
    assert first["stages"] == {"trace": progress}
    assert blocked["status"] == "blocked"
    assert blocked["question"] is None
    assert blocked["verification"] is None
    # 期限切れの削除はプロセスごとに間隔を空けて 1 回。
    assert store.purged == [30]


async def test_recorder_keeps_runs_by_default() -> None:
    """既定の保持日数は 0（無期限）で、実行履歴を自動では削除しない（#3235）。"""
    store = RecordingStore()
    recorder = RunRecorder(surface="search", settings=Settings(), store=store)

    await recorder.record(
        run_id="trace-1",
        question="q",
        search_answer_profile_id=None,
        answer_generated=False,
        status="success",
        elapsed_ms=1.0,
    )

    assert len(store.records) == 1
    assert store.purged == []


async def test_recorder_failure_does_not_raise(caplog: pytest.LogCaptureFixture) -> None:
    recorder = RunRecorder(surface="search", settings=Settings(), store=RecordingStore(fail=True))
    await recorder.record(
        run_id="trace-1",
        question="q",
        search_answer_profile_id=None,
        answer_generated=False,
        status="success",
        elapsed_ms=1.0,
    )
    assert "run history record failed" in caplog.text


@pytest.mark.usefixtures("trace_body_enabled")
async def test_pipeline_records_search_answer_and_blocked_runs(monkeypatch: MonkeyPatch) -> None:
    """RAG 検索（回答なし・回答あり）と、安全チェックで止めた質問を記録する。"""
    import rag_engine.adapters.model_api as engine_api

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)

    class AnswerOracle(FakeOracle):
        async def save_answer_record(self, record: Any) -> None:
            return None

        async def purge_answer_records(self, retention_days: int) -> int:
            return 0

    store = RecordingStore()
    traces: list[RecordingTrace] = []

    async def run(request: SearchRequest, **kwargs: Any) -> None:
        # 実行履歴の詳細の処理の経過は、共通の span の表を引く trace id と工程の詳細（#3226）。
        tracker = RecordingTrace("t" * 32)
        traces.append(tracker)
        recorder = RunRecorder(
            surface="search", settings=Settings(), store=store, trace_source=tracker.record
        )
        await RagPipeline(
            genai=FakeGenAi(),  # type: ignore[arg-type]
            oracle=AnswerOracle(),  # type: ignore[arg-type]
            settings=Settings(),
            run_recorder=recorder,
        ).run(request, trace=tracker, **kwargs)

    await run(
        SearchRequest(
            query="受注の登録方法は？", search_answer_profile_id="bv-1", generate_answer=False
        )
    )
    await run(SearchRequest(query="受注の登録方法は？", search_answer_profile_id="bv-1"))
    await run(
        SearchRequest(query="止める質問", search_answer_profile_id="bv-1"),
        query_guardrail_result=GuardrailResult(
            allowed=False, sanitized_text="止める質問", findings=[]
        ),
    )
    # 根拠の検索だけ（ナレッジベースの検索テスト・レシピの検索比較）は残さない。
    await run(SearchRequest(query="受注", search_answer_profile_id="bv-1", retrieval_only=True))

    def span_names(index: int) -> set[str]:
        return {update.operation_name for update in traces[index].updates}

    search_only, answered, blocked = store.records
    # 実行履歴は、表を引く trace id（実行の root の span の trace id）を持つ。
    for index, record in enumerate((search_only, answered, blocked)):
        assert record["stages"]["trace"]["trace_id"] == traces[index].trace_id
    assert search_only["status"] == "success"
    assert search_only["answer_generated"] is False
    assert search_only["question"] == "受注の登録方法は？"
    assert search_only["verification"] is None
    assert search_only["citation_count"] == len(search_only["citations"]) > 0
    assert {"rag.search", "rag.evidence"} <= span_names(0)
    assert answered["answer_generated"] is True
    assert answered["citations"] is None
    assert answered["verification"] in {"verified", "partial", "unverified"}
    assert {"rag.answer", "rag.engine.stage"} <= span_names(1)
    engine_steps = [
        update
        for update in traces[1].updates
        if update.display is not None and update.display.kind == "answer_step"
    ]
    assert engine_steps and all(
        (update.display.params or {}).get("name") for update in engine_steps if update.display
    )
    # 工程の詳細は実行履歴の記録に置く（表には入れない）。
    assert answered["stages"]["trace"]["span_details"]
    assert blocked["status"] == "blocked"
    assert blocked["question"] is None


# ---- 見え方（#1889） ----


def test_list_where_limits_regular_users_to_their_own_runs() -> None:
    with _scope(user_id_hash="u" * 64, search_answer_profile_ids={"bv-1"}):
        where, binds = run_history_list_where(query="受注_%", surface="chat", status="error")
    assert f"({OWNER_PREDICATE} OR {OWNER_CONVERSATION_PREDICATE}" in where
    assert binds["run_owner_user_id_hash"] == "u" * 64
    assert "r.search_answer_profile_id IN (:access_search_answer_profile_id_0)" in where
    assert binds["run_query"] == "%受注\\_\\%%"
    # 検索語は実行 ID の完全一致でも探す（#1955）。
    assert "r.run_id = :run_id_query" in where
    assert binds["run_id_query"] == "受注_%"
    assert binds["run_surface"] == "chat"
    assert binds["run_status"] == "error"
    assert "run_since" not in binds


def test_list_where_filters_by_period() -> None:
    """期間の絞り込み（#1955）は開始日時が `since` 以後の実行だけ。"""
    since = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
    with _scope(user_id_hash="u" * 64, all_users_visible=True):
        where, binds = run_history_list_where(query=None, surface=None, status=None, since=since)
    assert "r.created_at >= :run_since" in where
    assert binds["run_since"] == since


def test_list_where_does_not_limit_system_admin_and_denies_unknown_users() -> None:
    with _scope(user_id_hash="u" * 64, all_users_visible=True):
        where, binds = run_history_list_where(query=None, surface=None, status=None)
    assert OWNER_PREDICATE not in where
    assert OWNER_CONVERSATION_PREDICATE not in where
    assert "run_owner_user_id_hash" not in binds
    with _scope(user_id_hash=None):
        where, _ = run_history_list_where(query=None, surface=None, status=None)
    assert "1 = 0" in where


@pytest.mark.parametrize(
    ("executor", "expected", "absent"),
    [
        ("all", None, (MINE_PREDICATE, OTHERS_PREDICATE)),
        ("mine", MINE_PREDICATE, (OTHERS_PREDICATE,)),
        ("others", OTHERS_PREDICATE, ()),
    ],
    ids=["all", "mine", "others"],
)
@pytest.mark.parametrize("all_users", [False, True], ids=["user", "system-admin"])
def test_list_where_filters_by_executor(
    executor: str, expected: str | None, absent: tuple[str, ...], all_users: bool
) -> None:
    """「実行者」の絞り込み（#1954）は見え方の条件に重ねる（system_admin も自分を基準に絞る）。"""
    with _scope(user_id_hash="u" * 64, all_users_visible=all_users):
        where, binds = run_history_list_where(
            query=None,
            surface=None,
            status=None,
            executor=executor,  # type: ignore[arg-type]
        )
    if expected is None:
        assert "run_executor_user_id_hash" not in binds
    else:
        assert expected in where
        assert binds["run_executor_user_id_hash"] == "u" * 64
    for predicate in absent:
        assert predicate not in where


def test_list_where_executor_filter_without_a_user() -> None:
    """利用者の分からない context に「自分が実行」した行は無く、「他の人」は絞らない。"""
    with _scope(user_id_hash=None, all_users_visible=True):
        mine, _ = run_history_list_where(query=None, surface=None, status=None, executor="mine")
        others, binds = run_history_list_where(
            query=None, surface=None, status=None, executor="others"
        )
    assert "1 = 0" in mine
    assert "1 = 0" not in others
    assert "run_executor_user_id_hash" not in binds


def _row(run_id: str = "run-1", **overrides: Any) -> dict[str, object]:
    return {
        "run_id": run_id,
        "surface": "search",
        "search_answer_profile_id": "bv-1",
        "search_answer_profile_name": "受注",
        "user_id_hash": "x" * 64,
        "user_uuid": "user-uuid-1",
        "question": "受注の登録方法は？",
        "answer_generated": 0,
        "status": "success",
        "answer_outcome": None,
        "verification": None,
        "confidence": None,
        "needs_human_review": None,
        "citation_count": 1,
        "elapsed_ms": 1234.5,
        "error_stage": None,
        "model_id": None,
        "conversation_id": None,
        "message_id": None,
        "created_at": datetime(2026, 10, 15, tzinfo=UTC),
        **overrides,
    }


def _capture(monkeypatch: MonkeyPatch, execute_results: list[list[dict[str, object]]]) -> Any:
    pool = FakeOraclePool(execute_results=execute_results)
    monkeypatch.setattr(
        history_route,
        "OracleClient",
        lambda *_a, **_k: OracleClient(
            settings=Settings.model_construct(), pool=pool, db_call_runner=_run_inline
        ),
    )
    return pool


def test_list_api_shows_owner_and_executor_with_executor_names(
    monkeypatch: MonkeyPatch,
) -> None:
    """持ち主の一覧には、持ち主の会話で system_admin が実行した行も実行者の名前つきで出る。

    #1954。
    """
    auth = enable_production_auth(monkeypatch)
    viewer = auth.user_with_permissions("viewer", ["menu.history"])
    other = auth.user_with_permissions("other", ["menu.history"])
    admin_role = auth.create_role(["menu.search"])
    admin_user = auth.create_user("admin", [admin_role], system_admin=True)
    settings = get_settings()
    viewer_hash = _header_hash(viewer.user_uuid, settings)
    viewer_row = _row("run-viewer", user_id_hash=viewer_hash, user_uuid=viewer.user_uuid)
    # system_admin が viewer の会話で実行した行（SQL の会話の条件で viewer に見える）。
    admin_in_viewer = _row(
        "run-admin",
        surface="chat",
        user_id_hash=_header_hash(admin_user.user_uuid, settings),
        user_uuid=admin_user.user_uuid,
        conversation_id="conv-viewer",
        conversation_owner_hash=viewer_hash,
    )
    other_row = _row("run-other", user_uuid=other.user_uuid)
    # 1 回の一覧で 2 つの SQL（一覧・件数）。
    pool = _capture(
        monkeypatch,
        [
            [admin_in_viewer, viewer_row],
            [{"count_value": 2}],
            [admin_in_viewer],
            [{"count_value": 1}],
            [admin_in_viewer, viewer_row, other_row],
            [{"count_value": 3}],
        ],
    )

    viewer_headers = login(client, "viewer")
    mine = client.get("/api/history/runs?q=受注&status=success", headers=viewer_headers)
    others = client.get("/api/history/runs?executor=others", headers=viewer_headers)
    admin = client.get("/api/history/runs", headers=login(client, "admin"))

    assert mine.status_code == 200, mine.text
    assert others.status_code == 200, others.text
    assert admin.status_code == 200, admin.text
    viewer_calls, others_calls = pool.connection.calls[0:2], pool.connection.calls[2:4]
    admin_calls = pool.connection.calls[4:6]
    for call in [*viewer_calls, *others_calls]:
        assert OWNER_PREDICATE in call.statement
        assert OWNER_CONVERSATION_PREDICATE in call.statement
        assert call.parameters["run_owner_user_id_hash"] == viewer_hash
    for call in viewer_calls:
        assert OTHERS_PREDICATE not in call.statement
    # 件数（total）も一覧と同じ「実行者」の条件で数える。
    for call in others_calls:
        assert OTHERS_PREDICATE in call.statement
        assert call.parameters["run_executor_user_id_hash"] == viewer_hash
    for call in admin_calls:
        assert OWNER_PREDICATE not in call.statement
        assert OWNER_CONVERSATION_PREDICATE not in call.statement
    items = mine.json()["data"]["items"]
    assert [
        (item["run_id"], item["executed_by_me"], item["executor_display_name"]) for item in items
    ] == [("run-admin", False, "admin"), ("run-viewer", True, "viewer")]
    for item in items:
        assert not {"user_uuid", "user_id_hash", "conversation_owner_hash"} & item.keys()
        # 一覧には詳細の項目（会話の持ち主）を返さない。
        assert "owned_by_me" not in item
    assert mine.json()["data"]["total"] == 2
    assert others.json()["data"]["total"] == 1
    admin_items = admin.json()["data"]["items"]
    assert [entry["executor_display_name"] for entry in admin_items] == ["admin", "viewer", "other"]
    assert [entry["executed_by_me"] for entry in admin_items] == [True, False, False]


def test_list_api_passes_the_period_to_the_list_and_the_count(monkeypatch: MonkeyPatch) -> None:
    """`since`（ISO 8601）は一覧と件数の両方の条件になる。タイムゾーンの無い時刻は UTC（#1955）。"""
    auth = enable_production_auth(monkeypatch)
    auth.user_with_permissions("viewer", ["menu.history"])
    pool = _capture(monkeypatch, [[], [{"count_value": 0}], [], [{"count_value": 0}]])
    headers = login(client, "viewer")

    aware = client.get(
        "/api/history/runs", params={"since": "2026-10-14T09:00:00.000Z"}, headers=headers
    )
    naive = client.get(
        "/api/history/runs", params={"since": "2026-10-14T09:00:00"}, headers=headers
    )

    assert aware.status_code == 200, aware.text
    assert naive.status_code == 200, naive.text
    expected = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
    for call in pool.connection.calls:
        assert "r.created_at >= :run_since" in call.statement
        assert call.parameters["run_since"] == expected
    invalid = client.get("/api/history/runs", params={"since": "yesterday"}, headers=headers)
    assert invalid.status_code == 422


def test_list_api_rejects_an_unknown_executor_filter(monkeypatch: MonkeyPatch) -> None:
    auth = enable_production_auth(monkeypatch)
    auth.user_with_permissions("viewer", ["menu.history"])
    _capture(monkeypatch, [])
    response = client.get("/api/history/runs?executor=owner", headers=login(client, "viewer"))
    assert response.status_code == 422


def test_list_api_requires_the_history_menu(monkeypatch: MonkeyPatch) -> None:
    auth = enable_production_auth(monkeypatch)
    auth.user_with_permissions("searcher", ["menu.search"])
    _capture(monkeypatch, [])
    response = client.get("/api/history/runs", headers=login(client, "searcher"))
    assert response.status_code == 403


def test_detail_api_returns_404_outside_the_scope_and_joins_the_answer_record(
    monkeypatch: MonkeyPatch,
) -> None:
    auth = enable_production_auth(monkeypatch)
    auth.user_with_permissions("viewer", ["menu.history"])
    viewer = login(client, "viewer")

    # 他人の実行は見え方の条件で 0 行 → 404。
    pool = _capture(monkeypatch, [[]])
    missing = client.get("/api/history/runs/run-other", headers=viewer)
    assert missing.status_code == 404
    assert OWNER_PREDICATE in pool.connection.calls[0].statement
    assert OWNER_CONVERSATION_PREDICATE in pool.connection.calls[0].statement

    # 回答を作った自分の実行は、回答の記録から回答・根拠・診断を読む。
    stages = {
        # 処理の経過は trace の API（`/runs/{run_id}/trace`）で読む（詳細には入れない。#3226）。
        "trace": {"trace_id": "a" * 32, "trace_dropped_spans": 0, "span_details": {}},
        # 保存できなかった記録（#2342）。
        "storage_failures": [
            {"step": "chat_message_save", "error_type": "DatabaseError", "error_code": "ORA-40441"}
        ],
    }
    answer_record: dict[str, object] = {
        "trace_id": "run-1",
        "search_answer_profile_id": "bv-1",
        "surface": "chat",
        "answer_engine": "grounded",
        "question": "受注の登録方法は？",
        "rewritten_question": None,
        "answer": "受注番号を入力して登録します。",
        "citations_json": [
            {"document_id": "doc-1", "chunk_id": "doc-1:c1", "text": "受注番号", "score": 0.9}
        ],
        "diagnostics_json": {"outcome": "answered", "execution_steps": []},
        "evaluation_input_json": None,
        "evaluation_json": None,
        "created_at": datetime(2026, 10, 15, tzinfo=UTC),
    }
    _capture(
        monkeypatch,
        [
            [
                {
                    **_row(
                        "run-1",
                        surface="chat",
                        answer_generated=1,
                        conversation_id="conv-1",
                        message_id="synthetic-message",
                        verification="verified",
                        answer_outcome="answered",
                    ),
                    "question_full": "受注の登録方法は？（全文）",
                    "error_type": None,
                    "stages_json": stages,
                    "citations_json": None,
                }
            ],
            [answer_record],
            # 保存した根拠の文書を、見る人の今の範囲で確かめ直す（#3252）。
            [{"document_id": "doc-1"}],
            [
                {
                    "feedback_id": "fb-1",
                    "target_type": "answer",
                    "rating": "helpful",
                    "reason": None,
                    "document_id": None,
                    "created_at": datetime(2026, 10, 15, tzinfo=UTC),
                }
            ],
        ],
    )
    detail = client.get("/api/history/runs/run-1", headers=viewer)
    assert detail.status_code == 200, detail.text
    data = detail.json()["data"]
    assert data["question"] == "受注の登録方法は？（全文）"
    assert data["answer"] == "受注番号を入力して登録します。"
    assert data["answer_available"] is True
    assert data["citations"][0]["chunk_id"] == "doc-1:c1"
    assert data["answer_diagnostics"]["outcome"] == "answered"
    # 処理の経過（trace）・段・工程の秒の古い形式の項目は返さない。
    assert not {"stages", "steps", "stage_events", "progress_events", "trace_summary"} & set(data)
    assert data["feedback"][0]["feedback_id"] == "fb-1"
    assert data["verification"] == "verified"
    assert data["error_code"] is None
    assert data["storage_failures"] == [
        {"step": "chat_message_save", "error_type": "DatabaseError", "error_code": "ORA-40441"}
    ]
    # 会話の持ち主の分からない（会話の削除された）実行は、持ち主を出さない。
    assert (data["conversation_owner_display_name"], data["owned_by_me"]) == (None, None)


def test_detail_api_names_the_executor_and_the_conversation_owner(
    monkeypatch: MonkeyPatch,
) -> None:
    """詳細の「実行者」と「会話の持ち主」（#1954）。会話の無い実行は持ち主を出さない。"""
    auth = enable_production_auth(monkeypatch)
    alice = auth.user_with_permissions("alice", ["menu.history"])
    auth.store.users[alice.user_uuid].display_name = "アリス"
    admin_role = auth.create_role(["menu.search"])
    admin_user = auth.create_user("admin", [admin_role], system_admin=True)
    settings = get_settings()
    alice_hash = _header_hash(alice.user_uuid, settings)
    admin_hash = _header_hash(admin_user.user_uuid, settings)

    def detail_row(run_id: str, **overrides: Any) -> dict[str, object]:
        return {
            **_row(run_id, user_id_hash=admin_hash, user_uuid=admin_user.user_uuid, **overrides),
            "question_full": None,
            "error_type": None,
            "stages_json": None,
            "citations_json": None,
        }

    in_conversation = detail_row(
        "run-admin-chat",
        surface="chat",
        conversation_id="conv-alice",
        conversation_owner_hash=alice_hash,
    )
    search_only = detail_row("run-admin-search")
    # 1 件の詳細で 2 つの SQL（実行・フィードバック）。
    pool = _capture(
        monkeypatch,
        [[in_conversation], [], [in_conversation], [], [search_only], []],
    )
    alice_view = client.get("/api/history/runs/run-admin-chat", headers=login(client, "alice"))
    admin_headers = login(client, "admin")
    admin_view = client.get("/api/history/runs/run-admin-chat", headers=admin_headers)
    search_view = client.get("/api/history/runs/run-admin-search", headers=admin_headers)

    assert alice_view.status_code == 200, alice_view.text
    assert OWNER_CONVERSATION_PREDICATE in pool.connection.calls[0].statement
    assert pool.connection.calls[0].parameters["run_owner_user_id_hash"] == alice_hash
    data = alice_view.json()["data"]
    assert (data["executed_by_me"], data["executor_display_name"]) == (False, "admin")
    assert (data["conversation_owner_display_name"], data["owned_by_me"]) == ("アリス", True)
    assert "conversation_owner_hash" not in data
    admin_data = admin_view.json()["data"]
    assert (admin_data["executed_by_me"], admin_data["owned_by_me"]) == (True, False)
    assert admin_data["conversation_owner_display_name"] == "アリス"
    search_data = search_view.json()["data"]
    assert (search_data["conversation_owner_display_name"], search_data["owned_by_me"]) == (
        None,
        None,
    )


def test_configured_admin_sees_every_run(monkeypatch: MonkeyPatch) -> None:
    enable_production_auth(monkeypatch)
    pool = _capture(monkeypatch, [[], [{"count_value": 0}]])
    response = client.get("/api/history/runs", headers=login_configured_admin(client))
    assert response.status_code == 200, response.text
    for call in pool.connection.calls:
        assert OWNER_PREDICATE not in call.statement


async def test_save_run_takes_the_owner_from_the_context() -> None:
    pool = FakeOraclePool(execute_results=[])
    store = RunHistoryStore(
        OracleClient(settings=Settings.model_construct(), pool=pool, db_call_runner=_run_inline)
    )
    token = set_audit_request_context(
        AuditRequestContext(request_id="r", user_id_hash="h" * 64, user_uuid="user-1")
    )
    try:
        await store.save_run(
            {
                "run_id": "run-1",
                "surface": "search",
                "status": "success",
                "question": "q",
                "stages": {},
                "citations": [],
                # 呼び出し側の値は使わない。
                "user_uuid": "spoofed",
            }
        )
    finally:
        reset_audit_request_context(token)
    call = pool.connection.calls[0]
    assert "MERGE INTO rag_run_history" in call.statement
    assert call.parameters["user_uuid"] == "user-1"
    assert call.parameters["user_id_hash"] == "h" * 64


class _DriverError:
    """python-oracledb の例外の args[0]（`full_code` を持つ）。"""

    def __init__(self, code: str) -> None:
        self.full_code = code


class _DatabaseError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(_DriverError(code))


async def test_recorder_keeps_failed_stage_error_code_and_storage_failures() -> None:
    """失敗の止まった段（時間切れ以外も）とエラーコード、後から分かった保存の失敗を残す（#2342）。"""
    store = RecordingStore()
    recorder = RunRecorder(surface="chat", settings=Settings(), store=store)
    callback = recorder.observe(None)

    class Progress:
        def __init__(self, stage: str, outcome: str) -> None:
            self.stage, self.outcome, self.elapsed_ms = stage, outcome, 1.0

    await callback(Progress("answer", "started"))
    await callback(Progress("answer", "success"))
    await callback(Progress("answer_guardrail", "started"))
    await callback(Progress("answer_guardrail", "error"))
    await recorder.record(
        run_id="trace-failed",
        question="q",
        search_answer_profile_id=None,
        answer_generated=True,
        status="error",
        elapsed_ms=2.0,
        error=_DatabaseError("ORA-03146"),
    )
    (failed,) = store.records
    assert failed["error_type"] == "_DatabaseError"
    assert failed["error_stage"] == "answer_guardrail"
    assert failed["stages"]["error_code"] == "ORA-03146"

    store.records.clear()
    await recorder.record(
        run_id="trace-ok",
        question="q",
        search_answer_profile_id=None,
        answer_generated=True,
        status="success",
        elapsed_ms=2.0,
        storage_failures=[
            StorageFailure(
                step="answer_record_save", error_type="DatabaseError", error_code="ORA-1"
            )
        ],
    )
    await recorder.add_storage_failure(
        StorageFailure(step="chat_message_save", error_type="DatabaseError", error_code="ORA-2")
    )
    first, resaved = store.records
    assert first["error_stage"] is None
    assert "error_code" not in first["stages"]
    assert [item["step"] for item in resaved["stages"]["storage_failures"]] == [
        "answer_record_save",
        "chat_message_save",
    ]
    assert resaved["run_id"] == "trace-ok"


async def test_pipeline_returns_answer_when_answer_record_save_fails(
    monkeypatch: MonkeyPatch,
) -> None:
    """回答の記録を保存できなくても回答を返し、工程の失敗と保存の失敗を記録する（#2342）。

    期限切れの記録の削除の失敗は、保存の失敗に含めない。
    """
    import rag_engine.adapters.model_api as engine_api

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)

    class FailingOracle(FakeOracle):
        async def save_answer_record(self, record: Any) -> None:
            raise _DatabaseError("ORA-03146")

    class PurgeFailingOracle(FakeOracle):
        async def save_answer_record(self, record: Any) -> None:
            return None

        async def purge_answer_records(self, retention_days: int) -> int:
            raise _DatabaseError("ORA-00054")

    store = RecordingStore()
    stages: list[tuple[str, str, dict[str, object]]] = []

    async def progress(event: Any) -> None:
        stages.append((event.stage, event.outcome, dict(event.attributes)))

    result = await RagPipeline(
        genai=FakeGenAi(),  # type: ignore[arg-type]
        oracle=FailingOracle(),  # type: ignore[arg-type]
        settings=Settings(),
        run_recorder=RunRecorder(surface="search", settings=Settings(), store=store),
    ).run(
        SearchRequest(query="受注の登録方法は？", search_answer_profile_id="bv-1"),
        progress_callback=progress,
    )

    assert result.answer
    assert [item.model_dump() for item in result.storage_failures] == [
        {"step": "answer_record_save", "error_type": "_DatabaseError", "error_code": "ORA-03146"}
    ]
    saves = [item for item in stages if item[0] == "answer_record_save"]
    assert [item[1] for item in saves] == ["started", "error"]
    assert saves[-1][2]["error_code"] == "ORA-03146"
    (record,) = store.records
    assert record["status"] == "success"
    assert record["stages"]["storage_failures"][0]["error_code"] == "ORA-03146"

    purged = await RagPipeline(
        genai=FakeGenAi(),  # type: ignore[arg-type]
        oracle=PurgeFailingOracle(),  # type: ignore[arg-type]
        settings=Settings(rag_answer_record_retention_days=30),
    ).run(SearchRequest(query="受注の登録方法は？", search_answer_profile_id="bv-1"))
    assert purged.storage_failures == []


@pytest.mark.parametrize(
    ("validation", "expected"),
    [
        ({"status": "completed", "valid": True}, "verified"),
        (
            {"status": "completed", "valid": False, "counts": {"supported": 1, "unsupported": 1}},
            "partial",
        ),
        ({"status": "error", "valid": False}, "unverified"),
    ],
)
def test_final_validation_overrides_request_audit_verification(
    validation: dict[str, object],
    expected: str,
) -> None:
    diagnostics = {
        "envelope": {"requests": [{"status": "addressed"}]},
        "answer_validation": validation,
    }
    assert answer_verification(diagnostics) == expected


async def test_search_recorder_keeps_the_screen_conditions() -> None:
    """RAG 検索の実行は、件数と画面の絞り込みを記録に残す（詳細の「RAG 検索で開く」。#3309）。

    画面が送れない条件（ナレッジベースの ID・ページの範囲など）と空の値は残さない。
    """
    request = SearchRequest(
        query="合成の質問 A",
        top_k=10,
        search_answer_profile_id="profile-a",
        generate_answer=False,
        filters={
            "large_category": "分類 A",
            "small_category": "",
            "as_of": "2026-04-01",
            "extraction_fields": (
                '[{"name":"項目 A","value_type":"string","op":"eq","value":"値 A"}]'
            ),
            "knowledge_base_id": "kb-a",
            "page_ranges": "[]",
        },
    )
    recorder = _search_run_recorder(request)
    assert recorder is not None
    assert recorder.conditions == {
        "top_k": 10,
        "filters": {
            "large_category": "分類 A",
            "as_of": "2026-04-01",
            "extraction_fields": request.filters["extraction_fields"],
        },
    }
    # 根拠の検索だけの実行は記録しない。
    assert _search_run_recorder(request.model_copy(update={"retrieval_only": True})) is None
    # 絞り込みの無い実行は件数だけ。
    assert search_run_conditions(SearchRequest(query="q")) == {"top_k": 50}

    store = RecordingStore()
    recorder.store = store
    await recorder.record(
        run_id="trace-conditions",
        question="合成の質問 A",
        search_answer_profile_id="profile-a",
        answer_generated=False,
        status="success",
        elapsed_ms=1.0,
    )
    assert store.records[0]["stages"]["conditions"] == recorder.conditions


@pytest.mark.parametrize(
    ("surface", "stages", "expected"),
    [
        (
            "search",
            {
                "conditions": {
                    "top_k": 10,
                    "filters": {"as_of": "2026-04-01", "middle_category": ""},
                }
            },
            {"top_k": 10, "filters": {"as_of": "2026-04-01"}},
        ),
        # この機能より前の記録・壊れた記録は、質問とプロファイルだけを戻す（条件は None）。
        ("search", {"trace": {}}, None),
        ("search", {"conditions": "broken"}, None),
        (
            "search",
            {"conditions": {"top_k": True, "filters": ["x"]}},
            {"top_k": None, "filters": {}},
        ),
        # チャットの実行は条件を返さない（「会話を開く」で会話を開く）。
        ("chat", {"conditions": {"top_k": 10}}, None),
    ],
    ids=["search", "legacy", "broken", "wrong-types", "chat"],
)
def test_detail_api_returns_the_search_conditions(
    monkeypatch: MonkeyPatch,
    surface: str,
    stages: dict[str, object],
    expected: dict[str, object] | None,
) -> None:
    auth = enable_production_auth(monkeypatch)
    auth.user_with_permissions("viewer", ["menu.history"])
    viewer = login(client, "viewer")
    _capture(
        monkeypatch,
        [
            [
                {
                    **_row("run-conditions", surface=surface),
                    "question_full": "合成の質問 A",
                    "error_type": None,
                    "stages_json": stages,
                    "citations_json": None,
                }
            ],
            [],
        ],
    )
    detail = client.get("/api/history/runs/run-conditions", headers=viewer)
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["search_conditions"] == expected


# ---- モデルの利用量（#3419） ----


def _fake_llm_with_usage(model: str, *, fail_on: type | None = None) -> Any:
    """回答エンジンの LLM の呼び出しを作る。

    本番の adapter と同じ `gen_ai.chat` の span と合成の usage にする。
    """

    def fake(system: str, prompt: str, settings: Any, schema: type, **options: Any) -> Any:
        with observe_call("chat", model) as observation:
            if fail_on is not None and schema is fail_on:
                raise TimeoutError("合成の時間切れ")
            observation.usage(
                {
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "input_tokens_details": {"cached_tokens": 30},
                }
            )
        return _fake_llm(system, prompt, settings, schema, **options)

    return fake


def _llm_calls(trace: RecordingTrace) -> int:
    return len(
        {
            update.span_id
            for update in trace.updates
            if update.operation_name == "gen_ai.chat" and update.status != "running"
        }
    )


class _AnswerOracle(FakeOracle):
    async def save_answer_record(self, record: Any) -> None:
        return None

    async def purge_answer_records(self, retention_days: int) -> int:
        return 0


async def _record_pipeline_run(
    request: SearchRequest, *, model_id: str, store: RecordingStore
) -> RecordingTrace:
    tracker = RecordingTrace(model_id.encode().hex().ljust(32, "0")[:32])
    # 回答のモデルは登録モデルから接続を引く（登録に無いモデルは失敗にする。#3240）。
    settings = Settings(enterprise_ai_models=[EnterpriseAiConfiguredModel(model_id=model_id)])
    recorder = RunRecorder(
        surface="chat",
        settings=settings,
        store=store,
        model_id=model_id,
        trace_source=tracker.record,
        usage_source=tracker.model_usage,
    )
    await RagPipeline(
        genai=FakeGenAi(),  # type: ignore[arg-type]
        oracle=_AnswerOracle(),  # type: ignore[arg-type]
        settings=settings,
        answer_model_id=model_id,
        run_recorder=recorder,
    ).run(request, trace=tracker)
    return tracker


async def test_pipeline_records_the_model_usage_of_each_run(monkeypatch: MonkeyPatch) -> None:
    """実行の記録に、その実行の LLM の呼び出しの回数・token・モデルを残す。

    工程の span と二重に数えない。
    """
    import rag_engine.adapters.model_api as engine_api

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm_with_usage("model-a"))
    store = RecordingStore()
    tracker = await _record_pipeline_run(
        SearchRequest(query="合成の質問 A", search_answer_profile_id="bv-1"),
        model_id="model-a",
        store=store,
    )

    usage = store.records[0]["stages"]["usage"]
    calls = _llm_calls(tracker)
    assert calls > 0
    assert usage == {
        "models": ["model-a"],
        "requests": calls,
        "input_tokens": 100 * calls,
        "output_tokens": 10 * calls,
        "total_tokens": 110 * calls,
        "cached_input_tokens": 30 * calls,
        "reasoning_output_tokens": None,
        "measurement": "complete",
        "measured_requests": calls,
        "unmeasured_sources": [],
    }
    assert store.records[0]["model_id"] == "model-a"


async def test_compared_columns_keep_their_own_usage(monkeypatch: MonkeyPatch) -> None:
    """比較の列を同時に実行しても、列ごとの実行の利用量は混ざらない。"""
    import rag_engine.adapters.model_api as engine_api

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm_with_usage("model-shared"))
    store = RecordingStore()
    request = SearchRequest(query="合成の質問 A", search_answer_profile_id="bv-1")
    first, second = await asyncio.gather(
        _record_pipeline_run(request, model_id="model-a", store=store),
        _record_pipeline_run(request, model_id="model-b", store=store),
    )

    by_model = {record["model_id"]: record["stages"]["usage"] for record in store.records}
    assert by_model["model-a"]["requests"] == _llm_calls(first) > 0
    assert by_model["model-b"]["requests"] == _llm_calls(second) > 0
    assert by_model["model-a"]["total_tokens"] == 110 * _llm_calls(first)


async def test_failed_run_keeps_the_calls_it_made_as_partial(monkeypatch: MonkeyPatch) -> None:
    """失敗した実行は、失敗までの呼び出しを数え、利用量の無い呼び出しがあれば一部計測にする。"""
    import rag_engine.adapters.model_api as engine_api
    from rag_engine.generation.citation_wire import generation_draft_wire_model

    # 生成専用の実送信schemaで失敗させ、監査の保留とは区別する。
    monkeypatch.setattr(
        engine_api,
        "parse_text_response",
        _fake_llm_with_usage("model-a", fail_on=generation_draft_wire_model()),
    )
    store = RecordingStore()
    tracker = RecordingTrace("f" * 32)
    recorder = RunRecorder(
        surface="search",
        settings=Settings(),
        store=store,
        model_id="model-a",
        trace_source=tracker.record,
        usage_source=tracker.model_usage,
    )
    with pytest.raises(Exception):  # noqa: B017 - 失敗の種類は回答エンジンの扱いによる。
        await RagPipeline(
            genai=FakeGenAi(),  # type: ignore[arg-type]
            oracle=_AnswerOracle(),  # type: ignore[arg-type]
            settings=Settings(),
            run_recorder=recorder,
        ).run(SearchRequest(query="合成の質問 A", search_answer_profile_id="bv-1"), trace=tracker)
    # 経路と同じく、pipeline の外で失敗を記録する（`_record_failed_run`）。
    await recorder.record(
        run_id="trace-failed",
        question=recorder.question,
        search_answer_profile_id="bv-1",
        answer_generated=True,
        status="error",
        elapsed_ms=1.0,
        error=TimeoutError(),
    )

    usage = store.records[-1]["stages"]["usage"]
    assert usage["requests"] == _llm_calls(tracker) > usage["measured_requests"]
    assert usage["measurement"] == "partial"
    assert usage["total_tokens"] == 110 * usage["measured_requests"]


async def test_recorder_without_a_trace_keeps_no_usage() -> None:
    """trace の始まる前に止まった実行・利用量の元が無い実行は、利用量を残さない（画面は未計測）。"""
    store = RecordingStore()
    trace = RecordingTrace("e" * 32)
    for usage_source in (None, trace.model_usage):
        await RunRecorder(
            surface="search", settings=Settings(), store=store, usage_source=usage_source
        ).record(
            run_id="trace-early",
            question=None,
            search_answer_profile_id=None,
            answer_generated=True,
            status="error",
            elapsed_ms=1.0,
            error=RuntimeError(),
        )
    assert len(store.records) == 2
    assert all("usage" not in record["stages"] for record in store.records)


def test_search_records_the_concrete_default_model() -> None:
    """RAG 検索の実行は、未指定でも実行のときの既定のテキストモデルの具体的な ID を残す。"""
    settings = Settings(enterprise_ai_default_text_model="model-default")
    assert _run_model_id(None, settings) == "model-default"
    assert _run_model_id("model-explicit", settings) == "model-explicit"


@pytest.mark.parametrize(
    ("stages", "expected"),
    [
        (
            {
                "usage": {
                    "models": ["model-a", "model-b"],
                    "requests": 3,
                    "input_tokens": 300,
                    "output_tokens": 30,
                    "total_tokens": 330,
                    "measurement": "partial",
                    "measured_requests": 2,
                }
            },
            {"models": ["model-a", "model-b"], "requests": 3, "measurement": "partial"},
        ),
        # LLM を呼ばなかった実行は実測の 0。
        (
            {"usage": {"requests": 0, "measurement": "complete"}},
            {"requests": 0, "total_tokens": 0, "measurement": "complete"},
        ),
        # この機能より前の記録・壊れた記録は未計測（None。0 にしない）。
        ({"trace": {}}, None),
        ({"usage": "broken"}, None),
    ],
    ids=["saved", "no-llm", "legacy", "broken"],
)
def test_detail_api_returns_the_model_usage(
    monkeypatch: MonkeyPatch,
    stages: dict[str, object],
    expected: dict[str, object] | None,
) -> None:
    auth = enable_production_auth(monkeypatch)
    auth.user_with_permissions("viewer", ["menu.history"])
    viewer = login(client, "viewer")
    _capture(
        monkeypatch,
        [
            [
                {
                    **_row("run-usage", model_id="model-a"),
                    "question_full": "合成の質問 A",
                    "error_type": None,
                    "stages_json": stages,
                    "citations_json": None,
                }
            ],
            [],
        ],
    )
    detail = client.get("/api/history/runs/run-usage", headers=viewer)
    assert detail.status_code == 200, detail.text
    data = detail.json()["data"]
    usage = data["usage"]
    if expected is None:
        assert usage is None
        assert (data["total_tokens"], data["usage_measurement"]) == (None, None)
    else:
        assert {key: usage[key] for key in expected} == expected
        # 詳細の一覧の 2 つの値は、読んだ利用量の全体と同じ。
        assert (data["total_tokens"], data["usage_measurement"]) == (
            usage["total_tokens"],
            usage["measurement"],
        )


def test_list_api_returns_the_usage_totals_without_reading_the_stages(
    monkeypatch: MonkeyPatch,
) -> None:
    """一覧の行は合計の token と計測の状態を返す（#3466）。

    SQL は `stages_json` の全体を読まず、`JSON_VALUE` で 2 つの値だけを取り出す。記録の無い
    実行（この機能より前・形の違う記録）は未計測（None。0 にしない）、LLM を呼ばなかった実行は
    実測の 0、一部の呼び出しの利用量が無い実行は partial。
    """
    auth = enable_production_auth(monkeypatch)
    auth.user_with_permissions("viewer", ["menu.history"])
    rows = [
        _row("run-new", usage_total_tokens=12345, usage_measurement="complete"),
        _row("run-partial", usage_total_tokens=330, usage_measurement="partial"),
        _row("run-no-llm", usage_total_tokens=0, usage_measurement="complete"),
        # 計測の状態だけがあって合計の無い記録は 0（`RunModelUsage` の既定と同じ）。
        _row("run-no-total", usage_total_tokens=None, usage_measurement="complete"),
        _row("run-legacy", usage_total_tokens=None, usage_measurement=None),
        _row("run-broken", usage_total_tokens=5, usage_measurement="unknown"),
    ]
    pool = _capture(monkeypatch, [rows, [{"count_value": len(rows)}]])

    response = client.get("/api/history/runs", headers=login(client, "viewer"))

    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    assert [
        (item["run_id"], item["total_tokens"], item["usage_measurement"]) for item in items
    ] == [
        ("run-new", 12345, "complete"),
        ("run-partial", 330, "partial"),
        ("run-no-llm", 0, "complete"),
        ("run-no-total", 0, "complete"),
        ("run-legacy", None, None),
        ("run-broken", None, None),
    ]
    for item in items:
        assert not {"usage_total_tokens", "usage"} & item.keys()
    list_sql = pool.connection.calls[0].statement
    assert "JSON_VALUE(r.stages_json, '$.usage.total_tokens'" in list_sql
    assert "JSON_VALUE(r.stages_json, '$.usage.measurement'" in list_sql
    # 処理の段・trace 全体を読まず、利用量2値と照合実行状態だけを取り出す。
    assert "JSON_VALUE(r.stages_json, '$.validation_execution'" in list_sql
    assert list_sql.count("stages_json") == 3
    # ページ（並べ替えと件数の切り出し）は run_id だけで決め、列はページの行だけで読む。
    assert "WITH run_page AS ( SELECT r.run_id FROM rag_run_history r WHERE" in list_sql
    assert "JOIN run_page pg ON pg.run_id = r.run_id" in list_sql


@pytest.mark.parametrize(
    ("total", "measurement", "expected"),
    [
        (1234, "complete", (1234, "complete")),
        (1234.0, "partial", (1234, "partial")),
        (None, "complete", (0, "complete")),
        (-1, "complete", (0, "complete")),
        (True, "complete", (0, "complete")),
        (10, None, (None, None)),
        (10, "other", (None, None)),
    ],
    ids=["int", "float", "no-total", "negative", "bool", "legacy", "unknown"],
)
def test_run_usage_totals(
    total: object, measurement: object, expected: tuple[object, object]
) -> None:
    assert run_usage_totals(total, measurement) == expected


async def test_wrapped_answer_failure_records_safe_cause_and_keeps_wrapper_identity() -> None:
    from rag_engine.generation.execution_record import AnswerExecutionError

    class SyntheticFailure(RuntimeError):
        code: str

    cause = SyntheticFailure("秘密の本文")
    cause.code = "SYNTHETIC_FAILURE"
    wrapped = AnswerExecutionError("秘密の本文", "非公開の質問")
    wrapped.__cause__ = cause
    store = RecordingStore()
    recorder = RunRecorder(surface="search", settings=Settings(), store=store)
    await recorder.record(
        run_id="trace-error",
        question=None,
        search_answer_profile_id=None,
        answer_generated=False,
        status="error",
        elapsed_ms=1,
        error=wrapped,
    )
    record = store.records[0]
    assert record["error_type"] == "SyntheticFailure"
    assert record["stages"]["error_code"] == "SYNTHETIC_FAILURE"
    assert record["stages"]["error_wrapper_type"] == "AnswerExecutionError"
    assert "秘密" not in json.dumps(record, ensure_ascii=False)
