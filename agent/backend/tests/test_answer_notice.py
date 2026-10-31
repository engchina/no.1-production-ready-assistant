"""回答の注意書き（設定は製品ごと。#2405・#2449）を、Agent の Run と MCP で確かめる。

文は最終の検証の後に、回答の成果物の `notice` として付ける（本文の `text` とは別）。最終の検証の
入力・続きの質問でモデルに渡す会話の履歴には入れない。MCP は API キーで呼んだ外部のクライアントに
だけ返す。モデルは `ScriptedModel`、RAG は fake の MCP（`tests/test_answer_validation.py` と同じ）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from fastapi import Request
from mcp_support import FakeProductMcp
from pr_system_settings.answer_notice import DEFAULT_ANSWER_NOTICE_TEXT, AnswerNoticeRecord
from pytest import MonkeyPatch
from test_answer_validation import AGENT_ID, ANSWER, USER_UUID, _searched_run
from test_answer_validation import mcp as mcp  # noqa: F401 - fixture

from app.features.agent import mcp_server
from app.features.agent.answer_notice import answer_takes_notice
from app.features.agent.runtime import (
    RunCreateRequest,
    RunStatus,
    run_answer_notice,
    run_answer_text,
    runtime_repository,
)
from app.security.service import get_security_service


def _answer_content(run: Any) -> dict[str, Any]:
    [artifact] = [item for item in run.artifacts if item.kind == "answer"]
    return cast(dict[str, Any], artifact.content)


def test_answer_gets_notice_after_final_validation(
    monkeypatch: MonkeyPatch,
    mcp: FakeProductMcp,  # noqa: F811
) -> None:
    run, _model = _searched_run(monkeypatch)

    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    content = _answer_content(run)
    assert content["notice"] == DEFAULT_ANSWER_NOTICE_TEXT
    assert run_answer_notice(run) == DEFAULT_ANSWER_NOTICE_TEXT
    # 本文と最終の検証の入力には入れない。
    assert run_answer_text(run) == ANSWER
    [call] = mcp.calls_of("rag_validate_answer")
    assert DEFAULT_ANSWER_NOTICE_TEXT not in call["arguments"]["answer"]

    # 続きの質問でモデルに渡す会話の履歴（前の回答の本文）にも入れない。
    follow_up = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="続きの質問", agent_id=AGENT_ID, thread_id=run.thread_id),
        created_by_user_uuid=USER_UUID,
    )
    history = runtime_repository.thread_history(follow_up.id)
    assert history
    assert all(DEFAULT_ANSWER_NOTICE_TEXT not in str(turn) for turn in history)


def test_disabled_notice_is_not_attached(
    monkeypatch: MonkeyPatch,
    mcp: FakeProductMcp,  # noqa: F811
) -> None:
    get_security_service().answer_notice.save(AnswerNoticeRecord(enabled=False))
    run, _model = _searched_run(monkeypatch)
    assert run.status == RunStatus.COMPLETED
    assert "notice" not in _answer_content(run)
    assert run_answer_notice(run) is None


def test_confirmation_cards_and_clarification_questions_do_not_get_notice() -> None:
    assert answer_takes_notice({"value": "answered"}, confirmations=False) is True
    assert answer_takes_notice({"value": "insufficient_evidence"}, confirmations=False) is True
    assert answer_takes_notice(None, confirmations=False) is True
    # ツールの結果の回答前の確認（カード）と、確認の質問だけの回答には付けない。
    assert answer_takes_notice({"value": "answered"}, confirmations=True) is False
    assert answer_takes_notice({"value": "needs_clarification"}, confirmations=False) is False


def _request_with(**state: Any) -> Request:
    return cast(Request, SimpleNamespace(state=SimpleNamespace(**state)))


def test_mcp_returns_notice_only_to_api_key_callers(
    monkeypatch: MonkeyPatch,
    mcp: FakeProductMcp,  # noqa: F811
) -> None:
    assert mcp_server.called_with_api_key(_request_with(api_key=object())) is True
    assert mcp_server.called_with_api_key(_request_with()) is False

    run, _model = _searched_run(monkeypatch)
    assert mcp_server.run_output(run, include_notice=True).notice == DEFAULT_ANSWER_NOTICE_TEXT
    assert mcp_server.run_output(run).notice is None
    # 完了していない Run には付けない。
    running = run.model_copy(update={"status": RunStatus.RUNNING})
    assert mcp_server.run_output(running, include_notice=True).notice is None
