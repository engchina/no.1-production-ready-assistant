"""回答の注意書き（設定は製品ごと。#2405・#2449）を、NL2SQL のチャット・DB 管理チャット・MCP で
確かめる。

文は backend が回答の確定の後に、回答とは別の項目（`notice`）として付ける。モデルに渡す会話の
履歴（前の質問と SQL）には入れない。MCP は API キーで呼んだ外部のクライアントにだけ返す。
LLM・Oracle は決定論のスタブ（memory の保存先）。
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import Request
from pr_system_settings.answer_notice import DEFAULT_ANSWER_NOTICE_TEXT, AnswerNoticeRecord
from test_nl2sql_db_admin_chat_history import ALICE, _answer, _send
from test_nl2sql_db_admin_chat_history import setup as setup  # noqa: F401 - fixture
from test_sql_chat import ChatFixture, request, run
from test_sql_chat import chat as chat  # noqa: F401 - fixture

from app.features.mcp import tools as mcp_tools
from app.features.nl2sql.models import JobStatus
from app.security.service import get_security_service, reset_security_service


@pytest.fixture(autouse=True)
def _fresh_security_service() -> Iterator[None]:
    # 注意書きの設定はプロセスの中の共通認証の service が持つ（テストごとに初期化する）。
    reset_security_service()
    yield
    reset_security_service()


def test_chat_answer_gets_notice_and_history_for_the_model_does_not(chat: ChatFixture) -> None:
    service, _, client = chat
    first = run(service, request())
    assert first.status == JobStatus.DONE
    assert first.result is not None
    assert first.result.notice == DEFAULT_ANSWER_NOTICE_TEXT
    assert DEFAULT_ANSWER_NOTICE_TEXT not in first.result.explanation

    second = run(service, request("多い順にして", first.job_id))
    assert second.status == JobStatus.DONE
    # 続きの質問でモデルに渡す文（前の質問と SQL）に注意書きを入れない。
    assert "注文一覧" in client.prompts[-1]
    assert DEFAULT_ANSWER_NOTICE_TEXT.splitlines()[0] not in client.prompts[-1]


def test_chat_uses_saved_text_and_omits_when_disabled(chat: ChatFixture) -> None:
    service, _, _ = chat
    notice = get_security_service().answer_notice
    notice.save(AnswerNoticeRecord(enabled=True, text="独自の注意書き"))
    job = run(service, request())
    assert job.result is not None
    assert (
        job.result.notice
        == "独自の注意書き\n解決しない場合は、サポート窓口へお問い合わせください。"
    )

    notice.save(AnswerNoticeRecord(enabled=False, text="独自の注意書き"))
    job = run(service, request("顧客の一覧"))
    assert job.result is not None and job.result.notice is None


def test_blocked_sql_does_not_get_notice(
    chat: ChatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _, client = chat
    client.text = '{"sql":"DELETE FROM APP.ORDERS","explanation":"削除"}'
    monkeypatch.setattr(
        service, "execute_sql", lambda *args, **kwargs: pytest.fail("危険 SQL を実行")
    )
    job = run(service, request())
    assert job.status == JobStatus.ERROR
    assert job.result is not None and job.result.notice is None


def test_db_admin_chat_turn_gets_notice_and_follow_up_context_does_not(
    setup: Any,  # noqa: F811
) -> None:
    _service, _adapter, ai = setup
    ai.responses.append(_answer("SELECT QTY FROM APP.ITEM_STOCK", "在庫の数量です"))
    first = _send(ALICE, "ITEM_STOCK の数量")
    assert first.turn.notice == DEFAULT_ANSWER_NOTICE_TEXT
    assert DEFAULT_ANSWER_NOTICE_TEXT not in first.turn.explanation

    ai.responses.append(_answer("SELECT COUNT(*) FROM APP.ITEM_STOCK"))
    _send(ALICE, "件数は？", conversation_id=first.conversation.id)
    context = ai.calls[-1]["context"]
    assert "ITEM_STOCK の数量" in context
    assert DEFAULT_ANSWER_NOTICE_TEXT.splitlines()[0] not in context


def _job(status: JobStatus, notice: str | None) -> Any:
    result = SimpleNamespace(
        safety=SimpleNamespace(blocked_reason="", warnings=[], is_safe=True, safety_checked=True),
        generated_sql="SELECT 1 FROM DUAL",
        executable_sql="SELECT 1 FROM DUAL",
        explanation="説明",
        results=SimpleNamespace(
            columns=[], rows=[], returned_count=0, total=0, has_more=False, truncated=False
        ),
        history_id="h-1",
        notice=notice,
    )
    return SimpleNamespace(
        job_id="job-1",
        status=status,
        profile_id="p-1",
        error_code=None,
        error_message=None,
        error_detail=None,
        result=result,
    )


def _request_with(**state: Any) -> Request:
    return cast(Request, SimpleNamespace(state=SimpleNamespace(**state)))


def test_mcp_returns_notice_only_to_api_key_callers() -> None:
    assert mcp_tools.called_with_api_key(_request_with(api_key=object())) is True
    # Agent のサービストークン・local（認証なし）は API キーではない。
    assert mcp_tools.called_with_api_key(_request_with(service_token_claims={"sub": "u"})) is False
    assert mcp_tools.called_with_api_key(_request_with()) is False

    done = _job(JobStatus.DONE, "注意書き")
    assert mcp_tools._job_result(done, include_notice=True).notice == "注意書き"
    assert mcp_tools._job_result(done).notice is None
    # 答えていない job（実行中・失敗）には付けない。
    assert (
        mcp_tools._job_result(_job(JobStatus.RUNNING, "注意書き"), include_notice=True).notice
        is None
    )
    assert (
        mcp_tools._job_result(_job(JobStatus.ERROR, "注意書き"), include_notice=True).notice is None
    )
