"""処理の経過の読み出しの API と、MCP の trace のテスト（共通の span の表。#3226 の案 A）。

- 読み出し: チャットの回答・実行履歴・回答の記録・RAG 検索の実行を、今の判定（会話を開ける・実行
  履歴の見え方・回答の記録の持ち主）で開き、その記録の trace id で表を読む。system_admin は全員の
  span、それ以外は自分の span だけ。終端は記録の状態（正本）で、表で実行中の span は終端に合わせて
  閉じる。工程の詳細は記録から足し、書けなかった span の数は応答に入れる。
- trace の親: サービストークン（Agent）の MCP の要求だけ Agent の traceparent を親にし、API キーと
  画面の要求は新しい trace（span link だけ）にする。

実 DB は使わない（`tests.trace_store_support` の fake の表と、OracleClient の fake）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from opentelemetry import trace as otel_trace
from pr_backend_core.config.tracing import TracingSettings
from pr_backend_core.observability.tracing import (
    SpanDisplay,
    configure_tracing,
    observed_span,
    operation_trace,
    shutdown_tracing,
)
from pytest import MonkeyPatch

from app.api.routes import chat as chat_route
from app.api.routes import history as history_route
from app.api.routes import mcp as mcp_route
from app.api.routes import search as search_route
from app.clients.oracle import StoredMessage
from app.config import Settings
from app.main import app
from app.rag import trace_store
from app.rag.request_context import (
    AuditRequestContext,
    reset_audit_request_context,
    set_audit_request_context,
)
from app.rag.trace_store import AnswerTrace, mcp_request_trace, operation_trace_parent
from app.security.permissions import (
    MENU_CHAT,
    MENU_HISTORY,
    MENU_KNOWLEDGE_BASES,
    MENU_SEARCH,
    permission_for_route,
)
from tests.security_support import enable_production_auth, login
from tests.support import TEST_USER_ID, AsgiTestClient
from tests.test_chat_api import FakeChatOracle, _chat_conversation
from tests.trace_store_support import FakeTraceDb

client = AsgiTestClient(app)

TRACE_ID = "d" * 32
ROOT = "1" * 16
STEP = "2" * 16
ANSWER_ID = "e" * 32
AT = datetime(2026, 10, 9, tzinfo=UTC).replace(tzinfo=None)


@pytest.fixture
def tracing() -> Iterator[None]:
    configure_tracing(TracingSettings(), service_name="rag-test")
    yield
    shutdown_tracing()


def _span(
    db: FakeTraceDb,
    span_id: str,
    *,
    owner: str,
    operation_id: str = ANSWER_ID,
    status: str = "done",
    parent: str | None = None,
    root: bool = False,
    name: str = "rag.engine.stage",
    trace_id: str = TRACE_ID,
) -> None:
    db.rows[(trace_id, span_id)] = {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent,
        "product": "rag",
        "operation_id": operation_id,
        "attempt": 0,
        "is_root": 1 if root else 0,
        "owner_user_uuid": owner,
        "name": name,
        "span_kind": "internal",
        "status": status,
        "started_at": AT,
        "finished_at": None if status == "running" else AT,
        "display_kind": None if root else "answer_step",
        "display_params_json": None if root else '{"name":"文書検索"}',
        "attributes_json": None,
    }


def _tree(db: FakeTraceDb, owner: str, *, status: str = "running", **kwargs: Any) -> None:
    _span(db, ROOT, owner=owner, status=status, root=True, name="rag.answer", **kwargs)
    _span(db, STEP, owner=owner, status=status, parent=ROOT, **kwargs)


def _record(*, dropped: int = 0) -> dict[str, Any]:
    return {
        "trace_id": TRACE_ID,
        "trace_dropped_spans": dropped,
        "span_details": {STEP: {"fields": [{"label": "検索文", "value": "合成の検索文"}]}},
    }


@pytest.fixture
def chat_fake(monkeypatch: MonkeyPatch) -> FakeChatOracle:
    fake = FakeChatOracle()
    _chat_conversation(fake, "conv-t")
    monkeypatch.setattr(chat_route, "OracleClient", lambda *a, **k: fake)
    return fake


def _answer(fake: FakeChatOracle, *, status: str, progress: object) -> None:
    now = datetime.now(UTC)
    fake.messages["conv-t"] = [
        StoredMessage(id="u-1", conversation_id="conv-t", role="USER", content="q", created_at=now),
        StoredMessage(
            id=ANSWER_ID,
            conversation_id="conv-t",
            role="ASSISTANT",
            content="",
            status=status,
            reply_to_message_id="u-1",
            lease_owner="pytest-other:1:x",
            heartbeat_at=now,
            created_at=now,
            progress=progress,  # type: ignore[arg-type]
        ),
    ]


CHAT_TRACE = f"/api/chat/conversations/conv-t/messages/{ANSWER_ID}/trace"


# --------------------------------------------------------------------------- #
# チャットの回答
# --------------------------------------------------------------------------- #


def test_chat_trace_returns_running_spans_with_details(
    chat_fake: FakeChatOracle, trace_db: FakeTraceDb
) -> None:
    """作成中の回答は表の span をそのまま返し（終端なし）、開いた段の詳細を記録から足す。"""
    _tree(trace_db, TEST_USER_ID)
    _answer(chat_fake, status="STREAMING", progress=_record())

    data = client.get(CHAT_TRACE).json()["data"]
    events = data["events"]
    assert [event["type"] for event in events] == ["step", "step"]
    assert {event["status"] for event in events} == {"running"}
    assert {event["target_id"] for event in events} == {ANSWER_ID}
    step = next(event for event in events if event["step_id"] == STEP)
    assert step["parent_id"] == ROOT
    assert step["params"] == {"name": "文書検索"}
    assert step["details"]["fields"][0]["value"] == "合成の検索文"
    assert data["dropped_spans"] is None
    # 表は記録の trace id で引く（画面から trace id は受け取らない）。
    assert trace_db.queries[-1][1]["trace_id"] == TRACE_ID


@pytest.mark.parametrize(
    ("status", "terminal", "closed"),
    [
        ("COMPLETE", "done", "done"),
        ("ERROR", "failed", "failed"),
        ("CANCELLED", "cancelled", "skipped"),
    ],
    ids=["complete", "error", "cancelled"],
)
def test_chat_trace_closes_running_spans_with_message_status(
    chat_fake: FakeChatOracle, trace_db: FakeTraceDb, status: str, terminal: str, closed: str
) -> None:
    """終わった回答は回答の状態（正本）を終端にし、表で実行中のまま残った span を閉じる。"""
    _tree(trace_db, TEST_USER_ID)
    _answer(chat_fake, status=status, progress=_record(dropped=2))

    data = client.get(CHAT_TRACE).json()["data"]
    events = data["events"]
    assert events[-1] == {**events[-1], "type": "terminal", "status": terminal}
    assert {event["status"] for event in events if event["type"] == "step"} == {closed}
    # 書けなかった span があったら応答に入れる（画面は「一部の処理だけを記録しています。」）。
    assert data["dropped_spans"] == 2


def test_chat_trace_of_legacy_record_returns_only_terminal(
    chat_fake: FakeChatOracle, trace_db: FakeTraceDb
) -> None:
    """古い形式の記録（段のイベントの一覧）は表を読まず、終端だけを返す（画面は古い形式の案内）。"""
    _answer(chat_fake, status="COMPLETE", progress=[{"type": "step", "step_id": "x"}])

    events = client.get(CHAT_TRACE).json()["data"]["events"]
    assert [(event["type"], event["status"]) for event in events] == [("terminal", "done")]
    assert trace_db.queries == []


def test_chat_trace_before_root_span_is_empty(
    chat_fake: FakeChatOracle, trace_db: FakeTraceDb
) -> None:
    """root の span が始まる前（trace id がまだ無い）作成中の回答は空（古い形式にしない）。"""
    _answer(
        chat_fake,
        status="STREAMING",
        progress={"trace_id": None, "trace_dropped_spans": 0, "span_details": {}},
    )
    assert client.get(CHAT_TRACE).json()["data"] == {"events": [], "dropped_spans": None}


def test_chat_trace_is_404_outside_the_conversation(
    chat_fake: FakeChatOracle, trace_db: FakeTraceDb
) -> None:
    """会話が無い・回答が無い・質問（USER）の id は 404（会話の取得と同じ判定）。"""
    _answer(chat_fake, status="COMPLETE", progress=_record())
    assert client.get(f"/api/chat/conversations/none/messages/{ANSWER_ID}/trace").status_code == 404
    assert client.get("/api/chat/conversations/conv-t/messages/unknown/trace").status_code == 404
    assert client.get("/api/chat/conversations/conv-t/messages/u-1/trace").status_code == 404


def test_non_admin_sees_only_own_spans_and_admin_sees_all(
    chat_fake: FakeChatOracle, trace_db: FakeTraceDb, monkeypatch: MonkeyPatch
) -> None:
    """system_admin 以外は自分が持ち主の span だけ、system_admin は全員の span を読む。"""
    auth = enable_production_auth(monkeypatch)
    chatter = auth.user_with_permissions("chatter", [MENU_CHAT])
    admin_role = auth.create_role([MENU_CHAT])
    auth.create_user("admin", [admin_role], system_admin=True)
    _tree(trace_db, chatter.user_uuid, status="done")
    _span(trace_db, "3" * 16, owner="someone-else", parent=ROOT)
    _answer(chat_fake, status="COMPLETE", progress=_record())

    own = client.get(CHAT_TRACE, headers=login(client, "chatter")).json()["data"]["events"]
    assert {event["step_id"] for event in own if event["type"] == "step"} == {ROOT, STEP}
    assert trace_db.queries[-1][1]["viewer"] == chatter.user_uuid
    every = client.get(CHAT_TRACE, headers=login(client, "admin")).json()["data"]["events"]
    assert {event["step_id"] for event in every if event["type"] == "step"} == {
        ROOT,
        STEP,
        "3" * 16,
    }
    assert "viewer" not in trace_db.queries[-1][1]


# --------------------------------------------------------------------------- #
# 実行履歴・回答の記録・RAG 検索の実行
# --------------------------------------------------------------------------- #


class _RunStore:
    rows: dict[str, dict[str, Any]] = {}

    def __init__(self, _oracle: object) -> None:
        pass

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self.rows.get(run_id)


class _Oracle:
    def __init__(self, message: StoredMessage | None = None, answer: dict[str, Any] | None = None):
        self.message = message
        self.answer = answer

    async def get_chat_message(
        self, _conversation_id: str, _message_id: str
    ) -> StoredMessage | None:
        return self.message

    async def get_answer_record(self, _trace_id: str) -> dict[str, Any] | None:
        return self.answer


def _run_row(run_id: str, **overrides: Any) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "status": "success",
        "conversation_id": None,
        "message_id": None,
        "stages_json": {"trace": _record()},
        **overrides,
    }


def test_run_history_trace_reads_the_run_record(
    trace_db: FakeTraceDb, monkeypatch: MonkeyPatch
) -> None:
    """実行履歴の見え方で実行を開き（範囲の外は 404）、終端は実行の状態。"""
    monkeypatch.setattr(_RunStore, "rows", {"run-1": _run_row("run-1", status="error")})
    monkeypatch.setattr(history_route, "RunHistoryStore", _RunStore)
    monkeypatch.setattr(history_route, "OracleClient", lambda *a, **k: _Oracle())
    _tree(trace_db, TEST_USER_ID)

    events = client.get("/api/history/runs/run-1/trace").json()["data"]["events"]
    assert events[-1] == {
        **events[-1],
        "type": "terminal",
        "status": "failed",
        "target_id": "run-1",
    }
    assert {event["status"] for event in events if event["type"] == "step"} == {"failed"}
    assert client.get("/api/history/runs/run-other/trace").status_code == 404


def test_chat_run_prefers_the_message_record(
    trace_db: FakeTraceDb, monkeypatch: MonkeyPatch
) -> None:
    """チャットの実行は、会話の回答の記録（同じ trace。後の工程の詳細まで持つ）を優先する。"""
    run_record = {**_record(), "span_details": {}}
    row = _run_row(
        "run-chat",
        conversation_id="conv-t",
        message_id=ANSWER_ID,
        stages_json={"trace": run_record},
    )
    monkeypatch.setattr(_RunStore, "rows", {"run-chat": row})
    monkeypatch.setattr(history_route, "RunHistoryStore", _RunStore)
    message = StoredMessage(
        id=ANSWER_ID,
        conversation_id="conv-t",
        role="ASSISTANT",
        content="",
        created_at=datetime.now(UTC),
        progress=_record(),
    )
    monkeypatch.setattr(history_route, "OracleClient", lambda *a, **k: _Oracle(message))
    _tree(trace_db, TEST_USER_ID, status="done")

    events = client.get("/api/history/runs/run-chat/trace").json()["data"]["events"]
    step = next(event for event in events if event.get("step_id") == STEP)
    assert step["details"]["fields"][0]["value"] == "合成の検索文"


def test_answer_record_trace_reads_diagnostics_record(
    trace_db: FakeTraceDb, monkeypatch: MonkeyPatch
) -> None:
    """保存した回答は回答の記録の判定で開き、記録の trace で読む（完了の実行）。"""
    answer = {"trace_id": "rec-1", "diagnostics_json": {"outcome": "answered", "trace": _record()}}
    oracle = _Oracle(answer=answer)
    monkeypatch.setattr(search_route, "OracleClient", lambda *a, **k: oracle)
    _tree(trace_db, TEST_USER_ID, status="done")

    events = client.get("/api/search/answers/rec-1/trace").json()["data"]["events"]
    assert events[-1] == {**events[-1], "type": "terminal", "status": "done"}
    oracle.answer = None
    assert client.get("/api/search/answers/rec-1/trace").status_code == 404


def test_search_run_trace_looks_up_running_run_by_operation_id(
    trace_db: FakeTraceDb, monkeypatch: MonkeyPatch
) -> None:
    """記録の前（実行中）の RAG 検索は、表を実行の id で引き、記録があれば記録で読む。"""
    monkeypatch.setattr(_RunStore, "rows", {})
    monkeypatch.setattr(search_route, "RunHistoryStore", _RunStore)
    monkeypatch.setattr(search_route, "OracleClient", lambda *a, **k: _Oracle())
    _tree(trace_db, TEST_USER_ID, operation_id="run-s")

    running = client.get("/api/search/runs/run-s/trace").json()["data"]["events"]
    assert {event["status"] for event in running} == {"running"}
    lookup = next(query for query in trace_db.queries if query[0].startswith("SELECT TRACE_ID"))
    assert lookup[1]["operation_id"] == "run-s"
    assert client.get("/api/search/runs/unknown/trace").json()["data"]["events"] == []

    monkeypatch.setattr(_RunStore, "rows", {"run-s": _run_row("run-s")})
    finished = client.get("/api/search/runs/run-s/trace").json()["data"]["events"]
    assert finished[-1] == {**finished[-1], "type": "terminal", "status": "done"}


def test_trace_endpoints_are_registered_with_the_record_permissions() -> None:
    """trace の API は、その記録の画面と同じ権限（manifest の登録）。"""
    assert permission_for_route(
        "GET", "/chat/conversations/{conversation_id}/messages/{assistant_message_id}/trace"
    ) == frozenset({MENU_CHAT})
    assert permission_for_route("GET", "/history/runs/{run_id}/trace") == frozenset({MENU_HISTORY})
    assert permission_for_route("GET", "/search/runs/{run_id}/trace") == frozenset(
        {MENU_SEARCH, MENU_KNOWLEDGE_BASES}
    )
    assert permission_for_route("GET", "/search/answers/{trace_id}/trace") == permission_for_route(
        "GET", "/search/answers/{trace_id}"
    )


# --------------------------------------------------------------------------- #
# trace の親（MCP・画面）
# --------------------------------------------------------------------------- #


def _request(*, trusted: bool, traceparent: str | None = None) -> Any:
    return SimpleNamespace(
        state=SimpleNamespace(
            request_id="req-mcp-1",
            service_token_claims={"run_id": "run-1"} if trusted else None,
        ),
        headers={"traceparent": traceparent} if traceparent else {},
    )


def _agent_traceparent() -> tuple[str, str, str]:
    """Agent の `mcp.request` の span（trace id・span id・traceparent）。"""
    with observed_span("mcp.request", kind="client") as span:
        context = span.span.get_span_context()
    trace_id = f"{context.trace_id:032x}"
    span_id = f"{context.span_id:016x}"
    return trace_id, span_id, f"00-{trace_id}-{span_id}-01"


@pytest.fixture
def actor() -> Iterator[None]:
    token = set_audit_request_context(AuditRequestContext(user_uuid="user-sub"))
    yield
    reset_audit_request_context(token)


async def test_service_token_mcp_call_continues_the_agent_trace(
    tracing: None, trace_db: FakeTraceDb, actor: None
) -> None:
    """サービストークンの要求は `params._meta.traceparent` を親にし、中の検索も同じ trace へ。"""
    agent_trace, agent_span, traceparent = _agent_traceparent()
    message = {"params": {"name": "rag_search", "_meta": {"traceparent": traceparent}}}
    with mcp_request_trace(_request(trusted=True), message):
        search = AnswerTrace("rag-run-1", owner_user_uuid="user-sub")
        with operation_trace_parent() as links:
            assert list(links) == []
            with operation_trace(
                "rag.search", operation_id="rag-run-1", listener=search.listener(), links=links
            ):
                pass
    rows = trace_db.spans()
    server = next(row for row in rows if row["name"] == "mcp.server")
    inner = next(row for row in rows if row["name"] == "rag.search")
    assert {row["trace_id"] for row in rows} == {agent_trace}
    assert server["parent_span_id"] == agent_span
    assert server["span_kind"] == "server"
    assert (server["operation_id"], server["owner_user_uuid"]) == ("req-mcp-1", "user-sub")
    # OpenTelemetry の MCP の semantic conventions の method と呼んだツール（#3272）。
    attributes = json.loads(server["attributes_json"])
    assert (attributes["mcp.method.name"], attributes["gen_ai.tool.name"]) == (
        "tools/call",
        "rag_search",
    )
    assert inner["parent_span_id"] == server["span_id"]
    assert inner["operation_id"] == "rag-run-1"


async def test_rag_spans_carry_the_rag_step_names_for_other_products(
    tracing: None, trace_db: FakeTraceDb, actor: None
) -> None:
    """RAG の工程の span は RAG の画面と同じ工程の名前（名詞）を `display_name` に持つ（#3272）。

    別の製品の画面（Agent の実行の詳細）は RAG の工程の定義を持たず、この名前を出す。技術の span
    には付けない（画面が共通の規則で名前を付ける）。
    """
    search = AnswerTrace("rag-run-names", owner_user_uuid="user-sub")
    with (
        operation_trace("rag.search", operation_id="rag-run-names", listener=search.listener()),
        observed_span("rag.retrieval", attributes={"pr.stage": "retrieval"}),
        observed_span(
            "rag.engine.stage",
            attributes={"pr.stage": "文書検索（2回目）"},
            display=SpanDisplay(kind="answer_step", params={"name": "文書検索（2回目）"}),
        ) as stage,
    ):
        stage.set_display(params={"llm_calls": 1})
        with observed_span("gen_ai.embeddings", kind="client"):
            pass
    rows = {row["name"]: row for row in trace_db.spans()}
    names = {
        name: json.loads(row["display_params_json"] or "{}").get("display_name")
        for name, row in rows.items()
    }
    assert names == {
        "rag.search": None,
        "rag.retrieval": "文書から根拠の候補を検索する",
        "rag.engine.stage": "検索文から根拠の候補を集める（2 回目）",
        "gen_ai.embeddings": None,
    }
    assert {row["product"] for row in rows.values()} == {"rag"}
    # 工程の定義の値（工程名・LLM の回数）はそのまま残す。
    assert json.loads(rows["rag.engine.stage"]["display_params_json"]) == {
        "display_name": "検索文から根拠の候補を集める（2 回目）",
        "name": "文書検索（2回目）",
        "llm_calls": 1,
    }


async def test_api_key_mcp_call_starts_a_new_trace(
    tracing: None, trace_db: FakeTraceDb, actor: None
) -> None:
    """API キー（外部の MCP クライアント）の traceparent は表の親にしない（新しい trace）。"""
    agent_trace, _span_id, traceparent = _agent_traceparent()
    with mcp_request_trace(_request(trusted=False, traceparent=traceparent), {"params": {}}):
        search = AnswerTrace("rag-run-2", owner_user_uuid="user-sub")
        with (
            operation_trace_parent() as links,
            operation_trace(
                "rag.search", operation_id="rag-run-2", listener=search.listener(), links=links
            ),
        ):
            pass
    rows = trace_db.spans()
    server = next(row for row in rows if row["name"] == "mcp.server")
    assert server["trace_id"] != agent_trace
    assert server["parent_span_id"] is None
    # 信頼しない要求の中の検索も、来た trace に入れない（新しい trace を始める）。
    inner = next(row for row in rows if row["name"] == "rag.search")
    assert inner["trace_id"] not in {agent_trace, server["trace_id"]}
    assert inner["parent_span_id"] is None


async def test_screen_request_trace_is_new_and_linked(tracing: None) -> None:
    """画面の要求（ブラウザの traceparent を親にした HTTP の span）の中の実行は新しい trace に置き、
    HTTP の span は span link にだけ残す。"""
    with observed_span("http.request", kind="server") as http_span:
        http_context = http_span.span.get_span_context()
        with operation_trace_parent() as links:
            assert [link.context.span_id for link in links] == [http_context.span_id]
            current = otel_trace.get_current_span().get_span_context()
            assert not current.is_valid


async def test_traced_mcp_server_wraps_only_tool_calls(monkeypatch: MonkeyPatch) -> None:
    """server の span は `tools/call` だけに置く（`initialize` / `tools/list` は置かない）。"""
    from contextlib import contextmanager

    from pr_backend_core.mcp import McpServer

    traced: list[object] = []

    @contextmanager
    def fake_trace(_request: object, message: object) -> Iterator[None]:
        traced.append(message)
        yield

    monkeypatch.setattr(mcp_route, "mcp_request_trace", fake_trace)
    server = mcp_route.TracedMcpServer(
        McpServer(name="rag", version="1", tools=[]), _request(trusted=True)
    )
    allow = lambda _permissions: True  # noqa: E731
    await server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, has_any_permission=allow
    )
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "none"}}
    await server.handle(call, has_any_permission=allow)
    assert traced == [call]


def test_writer_is_started_only_when_the_database_is_configured(monkeypatch: MonkeyPatch) -> None:
    """DB の接続の設定が無ければ書き手を始めない（hermetic の e2e・テスト）。"""
    from app.config import Settings

    previous = trace_store.set_trace_span_writer(None)
    try:
        assert trace_store.start_trace_span_writer(Settings.model_construct()) is None
        assert trace_store.trace_span_writer() is None
    finally:
        trace_store.set_trace_span_writer(previous)


async def test_mcp_validate_answer_records_its_steps_and_model_calls(
    tracing: None, trace_db: FakeTraceDb, actor: None, monkeypatch: MonkeyPatch
) -> None:
    """MCP の `rag_validate_answer`（資料との照合）の中の工程とモデルの呼び出しを span にする。

    #3470。検索のツールと同じく Agent の `mcp.request` の下につながり、「資料との照合」→
    根拠の本文の
    読み込み・回答の段落の監査 → モデルの呼び出し（監査の thread の中）の木になる。
    """
    import rag_engine.evaluation.answer_validation as engine_validation
    from rag_engine.resources.tracing import observe_call

    from app.rag.answer_engine import ANSWER_VALIDATION_STEP, _answer_step_span
    from app.rag.answer_validation import (
        CLAIM_AUDIT_STAGE,
        EVIDENCE_LOAD_STAGE,
        EvidenceRef,
        validate_answer,
    )
    from app.schemas.search import RetrievedChunk

    class _Oracle:
        async def retrievable_chunk(self, document_id: str, chunk_id: str) -> Any:
            return RetrievedChunk(
                document_id=document_id, chunk_id=chunk_id, text="合成の本文。", score=0.5
            )

    def fake_claims(question: str, answer: str, items: Any, settings: Any, **_: Any) -> Any:
        # 監査の thread の中のモデルの呼び出し（rag_engine の OCI の呼び出しと同じ観測）。
        with observe_call("chat", "synthetic-model"):
            pass
        return {"status": "completed", "counts": {"supported": 1}, "claim_checks": []}

    monkeypatch.setattr(engine_validation, "validate_answer_claims", fake_claims)
    _agent_trace, _agent_span, traceparent = _agent_traceparent()
    message = {"params": {"name": "rag_validate_answer", "_meta": {"traceparent": traceparent}}}
    with (
        mcp_request_trace(_request(trusted=True), message),
        _answer_step_span(ANSWER_VALIDATION_STEP),
    ):
        await validate_answer(
            "合成の質問",
            "合成の回答。",
            [EvidenceRef("d1", "c1")],
            Settings(),
            oracle=cast(Any, _Oracle()),
        )
    rows = trace_db.spans()
    by_id = {row["span_id"]: row for row in rows}

    def name(row: dict[str, Any]) -> str:
        params = json.loads(row["display_params_json"] or "{}")
        return str(params.get("display_name") or row["name"])

    def parent(row: dict[str, Any]) -> str:
        return name(by_id[row["parent_span_id"]])

    server = next(row for row in rows if row["name"] == "mcp.server")
    step = next(row for row in rows if name(row) == ANSWER_VALIDATION_STEP)
    assert step["parent_span_id"] == server["span_id"]
    assert parent(next(row for row in rows if name(row) == EVIDENCE_LOAD_STAGE)) == (
        ANSWER_VALIDATION_STEP
    )
    assert parent(next(row for row in rows if name(row) == CLAIM_AUDIT_STAGE)) == (
        ANSWER_VALIDATION_STEP
    )
    model = next(row for row in rows if row["name"] == "gen_ai.chat")
    assert parent(model) == CLAIM_AUDIT_STAGE
