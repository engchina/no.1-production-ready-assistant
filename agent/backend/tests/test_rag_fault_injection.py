"""Agent → RAG の境界の障害注入（#64）。

RAG の MCP（契約どおりの fake。`mcp_support`）に、停止・timeout・5xx・不正な応答・認証の失敗・
権限の拒否を注入し、モデル（SDK の `ScriptedModel`）が次の 2 つの台本で動いても、Runtime が
境界を保つことを確かめる。外部への通信はしない。

- 作り話の台本: 検索が失敗したのに、モデルが業務の事実を断定して答える。→ 回答を出さずに Run を
  失敗にし、「根拠が無い」とは言わない（取得の失敗と根拠の不足を分ける。#2475）。
- 危険な次の操作の台本: 検索が失敗した後に、モデルが書き込みのツール
  （`nl2sql_cancel_requirement`）を要求する。→ 承認待ちで止まり、承認なしに呼び先へ送らない。
  無人の実行では送らない。取消の後は承認しても送らない。

どの場合も、RAG への呼び出しは Run の利用者（`sub`）のまま送り、障害を理由に別の主体・広い範囲で
呼び直さない（権限の拡大がない）。
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

import anyio
import pytest
from agents.testing import ScriptedModel, assistant_message, function_call
from mcp_support import FakeProductMcp, McpToolError, fake_product_mcp
from pytest import MonkeyPatch

import app.features.agent.tools as tools_module
from app.features.agent import builtin_runtime
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.runtime import (
    MCP_APPROVAL_SKIPPED_CODE,
    AgentProfile,
    AgentProfilePatch,
    ApprovalDecisionRequest,
    ApprovalStatus,
    RunCreateRequest,
    RunStatus,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.tool_grounding import GROUNDING_TOOL_FAILED_CODE

SKILL_ID = "test64-skill"
AGENT_ID = "test64-agent"
USER_UUID = "11111111-2222-3333-4444-555555550064"
FABRICATED = "就業規則では、有給休暇は年に 20 日です。"
RAG_SEARCH = "rag__rag_search"
# 書き込みのツール（readOnlyHint false。既定で承認）。`nl2sql_query` は既定で自動実行の照会（#1832）
# なので、危険な次の操作には使わない。
NL2SQL_WRITE = "nl2sql__nl2sql_cancel_requirement"

# 注入する障害（tools/call の応答）と、Agent が返す error code。
FAULTS: dict[str, tuple[Any, str]] = {
    "down": ("connect", "mcp.unreachable"),
    "timeout": ("timeout", "mcp.timeout"),
    "http_500": (500, "mcp.http_error"),
    "http_503": (503, "mcp.http_error"),
    "invalid_response": ("invalid_json", "mcp.invalid_json"),
    "auth_401": (401, "mcp.http_error"),
    "forbidden_403": (403, "mcp.http_error"),
}


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(tools_module, "_retry_sleep", lambda _seconds: None)


@pytest.fixture
def agent() -> Iterator[None]:
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name="障害注入の Skill",
            instructions="資料で確かめてから業務データを照会する。",
            mcp_requirements=[
                SkillMcpRequirement(server_id="rag", tool_names=["rag_search"]),
                SkillMcpRequirement(server_id="nl2sql", tool_names=["nl2sql_cancel_requirement"]),
            ],
        )
    )
    runtime_repository.create_agent(
        AgentProfile(id=AGENT_ID, name="障害注入の業務 Agent", skill_ids=[SKILL_ID])
    )
    try:
        yield
    finally:
        repository: Any = runtime_repository
        with repository._lock:  # noqa: SLF001 - テストの後始末
            for run_id in [
                run_id for run_id, run in repository._runs.items() if run.agent_id == AGENT_ID
            ]:
                run = repository._runs.pop(run_id)
                for approval in run.approvals:
                    repository._approvals.pop(approval.id, None)
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent(AGENT_ID)
        with contextlib.suppress(KeyError, ValueError):
            skill_registry.remove(SKILL_ID)


def _script(monkeypatch: MonkeyPatch, *steps: Any) -> ScriptedModel:
    model = ScriptedModel(list(steps))
    monkeypatch.setattr(
        builtin_runtime,
        "resolve_model_target",
        lambda model_id="": ModelTarget(
            model_id=model_id or "test-model",
            endpoint="https://llm.example",
            project_id="",
            api_key="k",
        ),
    )
    monkeypatch.setattr(builtin_runtime, "model_factory", lambda _target: model)
    return model


def _inject(monkeypatch: MonkeyPatch, fault: str) -> FakeProductMcp:
    """RAG の tools/call だけを壊す（ツールの一覧の取得と NL2SQL は正常）。"""
    if fault == "tool_error_forbidden":
        return fake_product_mcp(
            monkeypatch,
            outputs={
                "rag_search": McpToolError(
                    "RAG_SEARCH_ANSWER_PROFILE_FORBIDDEN",
                    "この検索・回答プロファイルは利用できません。",
                )
            },
        )
    mcp = fake_product_mcp(monkeypatch)
    failure = FAULTS[fault][0]
    real = mcp._handle  # noqa: SLF001 - RAG の tools/call だけに障害を入れる

    def handle(request: Any) -> Any:
        if request.url.host == "rag.test" and b'"tools/call"' in request.content:
            mcp.tool_call_statuses[:] = [failure]
        return real(request)

    mcp._handle = handle  # type: ignore[method-assign]  # noqa: SLF001
    return mcp


def _run(**metadata: Any) -> str:
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(
            goal="有給休暇の日数を調べて、部署別の取得状況を照会して", agent_id=AGENT_ID
        ),
        created_by_user_uuid=USER_UUID,
        extra_metadata=metadata or None,
    )
    anyio.run(builtin_runtime.execute_run, run.id)
    return run.id


def _assert_no_widening(mcp: FakeProductMcp) -> None:
    """障害の後も、呼び先へは Run の利用者・呼び先の製品の token だけを送った。"""
    signed = [request for request in mcp.requests if request["claims"] is not None]
    assert signed, "RAG / NL2SQL へ 1 回も署名した呼び出しをしていない"
    for request in mcp.requests:
        claims = request["claims"]
        assert claims is not None, "署名の無い呼び出しを送った"
        assert claims["sub"] == USER_UUID
        assert claims["aud"] == request["product"]


ALL_FAULTS = [*FAULTS, "tool_error_forbidden"]


def _expected_code(fault: str) -> str:
    return "mcp.tool_error" if fault == "tool_error_forbidden" else FAULTS[fault][1]


@pytest.mark.parametrize("fault", ALL_FAULTS)
def test_rag_failure_never_becomes_a_fabricated_or_no_evidence_answer(
    monkeypatch: MonkeyPatch, agent: None, fault: str
) -> None:
    """検索が失敗したのにモデルが断定しても、回答を出さず、根拠の不足とも言わない。"""
    mcp = _inject(monkeypatch, fault)
    _script(
        monkeypatch,
        [function_call(RAG_SEARCH, {"query": "有給休暇の日数"}, call_id="call-rag")],
        [assistant_message(FABRICATED)],
        [assistant_message(FABRICATED)],
    )

    run = runtime_repository.get_run(_run())

    assert run.status == RunStatus.FAILED
    [step] = run.steps
    assert step.tool_result is not None and step.tool_result.success is False
    assert step.tool_result.error_code == _expected_code(fault)
    assert run.events[-1].payload["error_code"] == GROUNDING_TOOL_FAILED_CODE
    assert "根拠が無いという意味ではありません" in run.events[-1].message
    assert not any(FABRICATED in str(artifact.content) for artifact in run.artifacts)
    assert len(mcp.calls_of("rag_search")) >= 1
    assert mcp.calls_of("nl2sql_cancel_requirement") == []
    _assert_no_widening(mcp)


def _dangerous_write(monkeypatch: MonkeyPatch) -> ScriptedModel:
    """検索の失敗の後も、根拠の無いまま書き込みのツールを要求する台本（#2586 の B）。"""
    return _script(
        monkeypatch,
        [function_call(RAG_SEARCH, {"query": "有給休暇の日数"}, call_id="call-rag")],
        [function_call(NL2SQL_WRITE, {"requirement_session_id": "qs-64"}, call_id="call-write")],
        [assistant_message(FABRICATED)],
        [assistant_message(FABRICATED)],
    )


@pytest.mark.parametrize("fault", ALL_FAULTS)
def test_write_requested_after_rag_failure_waits_for_approval_and_is_not_sent(
    monkeypatch: MonkeyPatch, agent: None, fault: str
) -> None:
    """検索の失敗の後に書き込みを要求されても、承認待ちで止まり、呼び先へ送らない。"""
    mcp = _inject(monkeypatch, fault)
    _dangerous_write(monkeypatch)

    run = runtime_repository.get_run(_run())

    assert run.status == RunStatus.WAITING_APPROVAL
    [approval] = run.approvals
    assert approval.status == ApprovalStatus.PENDING
    assert approval.tool_call.name == NL2SQL_WRITE
    assert approval.decided_by is None
    assert mcp.calls_of("nl2sql_cancel_requirement") == []
    _assert_no_widening(mcp)


@pytest.mark.parametrize("fault", ["down", "http_503", "auth_401", "tool_error_forbidden"])
def test_write_requested_after_rag_failure_is_never_sent_in_unattended_runs(
    monkeypatch: MonkeyPatch, agent: None, fault: str
) -> None:
    """無人の実行（外部の MCP クライアント）は承認できないので、書き込みを送らない。"""
    mcp = _inject(monkeypatch, fault)
    _dangerous_write(monkeypatch)

    run = runtime_repository.get_run(_run(source="mcp", api_key_id="0123456789abcdef"))

    assert run.approvals == []
    assert mcp.calls_of("nl2sql_cancel_requirement") == []
    skipped = [step for step in run.steps if step.tool_call and step.tool_call.name == NL2SQL_WRITE]
    assert [step.tool_result.error_code for step in skipped if step.tool_result] == [
        MCP_APPROVAL_SKIPPED_CODE
    ]
    # 根拠のツールが 1 つも結果を返していないので、作り話の回答は出さない。
    assert run.status == RunStatus.FAILED
    assert run.events[-1].payload["error_code"] == GROUNDING_TOOL_FAILED_CODE
    _assert_no_widening(mcp)


def test_cancel_while_waiting_for_approval_after_rag_failure_never_sends_the_write(
    monkeypatch: MonkeyPatch, agent: None
) -> None:
    """承認待ちの Run を取り消すと、承認は取り消され、後から承認・再開しても送らない。"""
    mcp = _inject(monkeypatch, "down")
    _dangerous_write(monkeypatch)
    run_id = _run()
    [approval] = runtime_repository.get_run(run_id).approvals

    runtime_repository.cancel_run(run_id, cancelled_by="operator")

    cancelled = runtime_repository.get_run(run_id)
    assert cancelled.status == RunStatus.CANCELLED
    assert [item.status for item in cancelled.approvals] == [ApprovalStatus.CANCELLED]
    # 遅れて届いた承認は採用しない（取消済みのまま）。
    late = runtime_repository.decide_approval(
        approval.id, ApprovalDecisionRequest(approved=True, decided_by="late-approver")
    )
    assert late.status == RunStatus.CANCELLED
    assert [item.status for item in late.approvals] == [ApprovalStatus.CANCELLED]
    anyio.run(builtin_runtime.resume_run, run_id)
    assert runtime_repository.get_run(run_id).status == RunStatus.CANCELLED
    assert mcp.calls_of("nl2sql_cancel_requirement") == []


def test_disabling_the_agent_stops_new_runs_but_not_accepted_ones(
    monkeypatch: MonkeyPatch, agent: None
) -> None:
    """受付停止（業務 Agent の無効化）は新しい Run だけを止める。受理済みの Run は取消で止める。

    運用の手順（agent/docs/agent-rag-ship-preflight.md の「停止」）の前提を固定する。
    """
    mcp = _inject(monkeypatch, "down")
    _dangerous_write(monkeypatch)
    run_id = _run()
    assert runtime_repository.get_run(run_id).status == RunStatus.WAITING_APPROVAL

    runtime_repository.patch_agent(AGENT_ID, AgentProfilePatch(enabled=False))

    with pytest.raises(ValueError, match="agent disabled"):
        runtime_repository.create_builtin_run(
            RunCreateRequest(goal="新しい依頼", agent_id=AGENT_ID),
            created_by_user_uuid=USER_UUID,
        )
    # 受理済みの承認待ちは残る（自動で承認・取消しない）。止めるのは実行の取消。
    waiting = runtime_repository.get_run(run_id)
    assert waiting.status == RunStatus.WAITING_APPROVAL
    assert [item.status for item in waiting.approvals] == [ApprovalStatus.PENDING]
    runtime_repository.cancel_run(run_id, cancelled_by="operator")
    assert runtime_repository.get_run(run_id).status == RunStatus.CANCELLED
    assert mcp.calls_of("nl2sql_cancel_requirement") == []
