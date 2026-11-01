"""回答の注意書き（設定は製品ごと。#2405・#2449）を、RAG の回答の経路ごとに確かめる。

文は backend が回答の確定の後に、本文とは別の項目（`notice`）として付ける。本文・会話の履歴には
入れず、回答の記録・会話の回答に保存する。MCP は API キーで呼んだ外部のクライアントにだけ返す。
外部 I/O はスタブ（`tests.test_answer_surfaces` と同じ）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pr_system_settings.answer_notice import (
    DEFAULT_ANSWER_NOTICE_TEXT,
    AnswerNoticeParts,
    AnswerNoticeRecord,
)
from pytest import MonkeyPatch

import app.rag.pipeline as pipeline_module
from app.api.routes import chat as chat_route
from app.api.routes import search as search_route
from app.clients.oracle import StoredMessage
from app.config import Settings, get_settings
from app.main import app
from app.rag.answer_notice import answer_outcome_takes_notice
from app.rag.guardrails import GuardrailPolicy
from app.rag.pipeline import RagPipeline
from app.schemas.search import SearchRequest
from app.security.service import SecurityService, set_security_service
from app.security.store import InMemorySecurityStore
from tests.security_support import enable_production_auth, login
from tests.support import AsgiTestClient
from tests.test_answer_engine import FakeGenAi, SavingOracle, _fake_llm
from tests.test_search_search_answer_profile import FakeViewOracle

client = AsgiTestClient(app)
QUESTION = "受注の登録方法は？"


@pytest.fixture(autouse=True)
def _stub_llm(monkeypatch: MonkeyPatch) -> None:
    import rag_engine.adapters.model_api as engine_api

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)


class _Oracle(FakeViewOracle, SavingOracle):
    """検索・回答プロファイルの解決と回答フローの検索・回答の記録の保存を受けるスタブ。"""

    def __init__(self) -> None:
        FakeViewOracle.__init__(self, {})
        SavingOracle.__init__(self)


def _install(monkeypatch: MonkeyPatch) -> _Oracle:
    oracle = _Oracle()
    monkeypatch.setattr(search_route, "OracleClient", lambda *_args, **_kwargs: oracle)
    monkeypatch.setattr(pipeline_module, "OracleClient", lambda *_args, **_kwargs: oracle)
    monkeypatch.setattr(pipeline_module, "GenAiClient", lambda *_args, **_kwargs: FakeGenAi())
    return oracle


def _local_service(record: AnswerNoticeRecord | None = None) -> SecurityService:
    service = SecurityService(InMemorySecurityStore(), get_settings())
    if record is not None:
        service.answer_notice.save(record)
    set_security_service(service)
    return service


def test_search_api_attaches_notice_after_the_answer_and_saves_it(monkeypatch: MonkeyPatch) -> None:
    oracle = _install(monkeypatch)
    _local_service()

    response = client.post("/api/search", json={"query": QUESTION, "knowledge_base_ids": ["kb-1"]})

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["notice"] == DEFAULT_ANSWER_NOTICE_TEXT
    # 本文には入れない（モデルに書かせない・回答の検証の対象にしない）。
    assert DEFAULT_ANSWER_NOTICE_TEXT not in data["answer"]
    assert oracle.saved[-1]["answer_notice"] == DEFAULT_ANSWER_NOTICE_TEXT
    assert DEFAULT_ANSWER_NOTICE_TEXT not in oracle.saved[-1]["answer"]


def test_search_api_uses_the_saved_text_and_omits_when_disabled(monkeypatch: MonkeyPatch) -> None:
    oracle = _install(monkeypatch)
    service = _local_service(
        AnswerNoticeRecord(enabled=True, text="独自の注意書き", contact="窓口 A")
    )

    data = client.post(
        "/api/search", json={"query": QUESTION, "knowledge_base_ids": ["kb-1"]}
    ).json()["data"]
    assert data["notice"] == "独自の注意書き\n解決しない場合は、窓口 Aへお問い合わせください。"

    service.answer_notice.save(AnswerNoticeRecord(enabled=False, text="独自の注意書き"))
    data = client.post(
        "/api/search", json={"query": QUESTION, "knowledge_base_ids": ["kb-1"]}
    ).json()["data"]
    assert data["notice"] is None
    assert oracle.saved[-1]["answer_notice"] is None


async def test_pipeline_without_notice_source_does_not_attach() -> None:
    """品質評価・確かめのスクリプト（注意書きの文を渡さない経路）には付けない。"""
    pipeline = RagPipeline(
        settings=Settings(),
        oracle=SavingOracle(),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    )
    response = await pipeline.run(SearchRequest(query=QUESTION, knowledge_base_ids=["kb-1"]))
    assert response.answer
    assert response.notice is None


async def test_pipeline_does_not_attach_when_search_only() -> None:
    """回答を作らない RAG 検索（根拠の一覧だけ）には付けない。"""
    pipeline = RagPipeline(
        settings=Settings(),
        oracle=SavingOracle(),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
        answer_notice=lambda: AnswerNoticeParts(body="注意書き", contact="窓口 A"),
    )
    response = await pipeline.run(
        SearchRequest(query=QUESTION, knowledge_base_ids=["kb-1"], generate_answer=False)
    )
    assert response.notice is None


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("answered", True),
        ("conditional", True),
        ("insufficient_evidence", True),
        ("needs_human", True),
        ("needs_environment_data", True),
        ("needs_clarification", False),
    ],
    ids=lambda value: str(value),
)
def test_notice_goes_to_answers_and_refusals_but_not_clarifications(
    outcome: str, expected: bool
) -> None:
    assert answer_outcome_takes_notice({"outcome": outcome}) is expected


def _message(**overrides: Any) -> StoredMessage:
    values: dict[str, Any] = {
        "id": "m-2",
        "conversation_id": "c-1",
        "role": "ASSISTANT",
        "content": "回答の本文",
        "created_at": datetime(2026, 10, 18, tzinfo=UTC),
        "reply_to_message_id": "m-1",
        "answer_notice": "注意書きの文",
    }
    values.update(overrides)
    return StoredMessage(**values)


def test_chat_message_returns_notice_only_for_completed_answers() -> None:
    assert chat_route._to_chat_message(_message()).notice == "注意書きの文"
    for status in ("ERROR", "CANCELLED", "STREAMING"):
        assert chat_route._to_chat_message(_message(status=status)).notice is None


async def test_chat_history_for_the_model_does_not_include_the_notice() -> None:
    user = StoredMessage(
        id="m-1",
        conversation_id="c-1",
        role="USER",
        content="最初の質問",
        created_at=datetime(2026, 10, 18, tzinfo=UTC),
    )
    turns = await chat_route._build_safe_history(
        [user, _message()], GuardrailPolicy(get_settings())
    )
    assert [turn.content for turn in turns] == ["最初の質問", "回答の本文"]
    assert all("注意書きの文" not in turn.content for turn in turns)


def _rag_search_call(headers: dict[str, str] | None = None) -> dict[str, Any]:
    response = client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "rag_search",
                "arguments": {"query": QUESTION, "knowledge_base_ids": ["kb-1"]},
            },
        },
        headers=headers or {},
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()["result"]
    assert result["isError"] is False, result
    structured: dict[str, Any] = result["structuredContent"]
    return structured


def test_mcp_returns_notice_only_to_api_key_callers(monkeypatch: MonkeyPatch) -> None:
    _install(monkeypatch)
    # local（認証なし。API キーでもない）の呼び出しには返さない。
    _local_service()
    assert _rag_search_call()["notice"] is None

    auth = enable_production_auth(monkeypatch)
    role = auth.create_role(["menu.search"], knowledge_base_ids=["kb-1"])
    auth.create_user("mcp_user", [role])
    session = login(client, "mcp_user")
    created = client.post(
        "/api/account/api-keys",
        json={"name": "外部のクライアント", "products": ["rag"], "expires_in_days": 30},
        headers=session,
    ).json()["data"]
    structured = _rag_search_call({"Authorization": f"Bearer {created['token']}"})
    assert structured["notice"] == DEFAULT_ANSWER_NOTICE_TEXT
    assert DEFAULT_ANSWER_NOTICE_TEXT not in structured["answer"]
