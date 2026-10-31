"""業務 Agent を外部の MCP クライアントに公開する（`POST /api/mcp`。#1874）。

業務 Agent のモデルは SDK の `ScriptedModel` に差し替え、OCI へは接続しない。API キーは共通の
`pr_system_settings.auth.api_keys`（InMemory の store）。
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from typing import Any

import pytest
from agents.testing import ScriptedModel, assistant_message, function_call
from pr_backend_core.token_estimate import estimate_input_tokens
from pr_system_settings.auth.api_keys import api_key_rate_limiter
from pytest import MonkeyPatch
from security_support import ProductionAuth, client, enable_production_auth, login

from app.features.agent import builtin_runtime, mcp_server
from app.features.agent.answer_validation import combine_validations, validation_content
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.confirmation import (
    CONFIRMATION_TOOL,
    ConfirmationRequest,
    item_from_arguments,
)
from app.features.agent.runtime import (
    AgentProfile,
    Artifact,
    RunCreateRequest,
    RunEventType,
    RunState,
    RunStatus,
    RunStep,
    StepStatus,
    run_cancel_record,
    runtime_repository,
)
from app.features.agent.tools import ToolCall, ToolResult
from app.security.permissions import RUNS_OPERATE, RUNS_VIEW
from app.security.service import set_security_service
from app.settings import get_settings

AGENT_ID = "mcp1874-agent"
OTHER_AGENT_ID = "mcp1874-other"
TOOLS = {
    "agent_list",
    "agent_chat",
    "agent_get_run",
    "agent_answer_confirmation",
    "agent_cancel_run",
    "agent_get_run_observation",
    "agent_skill_export",
}
ASK_ARGUMENTS = {
    "question": "どの期間の経費を見ますか？",
    "reason": "期間が質問から決められません。",
    "options": [
        {"label": "今月", "description": ""},
        {"label": "今年度", "description": "4 月から"},
    ],
    "multiple": False,
}


@pytest.fixture
def agents(monkeypatch: MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(mcp_server, "wait_poll_seconds", 0.01)
    settings = get_settings()
    # 台本のモデルはツールを呼ばずに答えるので、最終の検証と拒答のガードは外す。
    monkeypatch.setattr(settings, "agent_final_validation_enabled", False)
    monkeypatch.setattr(settings, "agent_tool_grounding_required", False)
    for agent_id, name in ((AGENT_ID, "経理の Agent"), (OTHER_AGENT_ID, "人事の Agent")):
        runtime_repository.create_agent(
            AgentProfile(
                id=agent_id, name=name, description=f"{name}の説明", instructions="答える。"
            )
        )
    api_key_rate_limiter.reset()
    try:
        yield
    finally:
        repository: Any = runtime_repository
        with repository._lock:  # noqa: SLF001 - テストの後始末
            for run_id in [
                run_id
                for run_id, run in repository._runs.items()
                if run.agent_id in {AGENT_ID, OTHER_AGENT_ID}
            ]:
                repository._runs.pop(run_id)
        for agent_id in (AGENT_ID, OTHER_AGENT_ID):
            with contextlib.suppress(KeyError, ValueError):
                runtime_repository.delete_agent(agent_id)


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> Iterator[ProductionAuth]:
    yield enable_production_auth(monkeypatch)
    set_security_service(None)


def _script(monkeypatch: MonkeyPatch, *steps: Any) -> ScriptedModel:
    model = ScriptedModel(list(steps))
    monkeypatch.setattr(
        builtin_runtime,
        "resolve_model_target",
        lambda model_id="": ModelTarget(
            model_id=model_id or "m", endpoint="https://oci.example", project_ocid="", api_key="k"
        ),
    )
    monkeypatch.setattr(builtin_runtime, "model_factory", lambda _target: model)
    return model


def _rpc(
    method: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None
) -> Any:
    return client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers=headers or {},
    )


def _call(
    name: str, arguments: dict[str, Any], headers: dict[str, str] | None = None
) -> dict[str, Any]:
    response = _rpc("tools/call", {"name": name, "arguments": arguments}, headers)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()["result"]
    return result


def _bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def _issue_key(headers: dict[str, str], **body: Any) -> dict[str, Any]:
    payload = {"name": "Claude Code", "products": ["agent"], **body}
    response = client.post("/api/account/api-keys", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


# ---- local（ログインなし）--------------------------------------------------------------


def test_tools_list_and_chat_answers_with_the_agent(monkeypatch: MonkeyPatch, agents: None) -> None:
    del agents
    _script(monkeypatch, [assistant_message("今月の経費は 50 万円です。")])

    initialized = _rpc("initialize", {"protocolVersion": "2025-06-18"}).json()["result"]
    assert initialized["serverInfo"]["name"] == "production-ready-agent"
    assert "agent_chat" in initialized["instructions"]
    tools = {tool["name"]: tool for tool in _rpc("tools/list").json()["result"]["tools"]}
    assert set(tools) == TOOLS
    assert tools["agent_list"]["annotations"]["readOnlyHint"] is True
    assert tools["agent_get_run"]["annotations"]["readOnlyHint"] is True
    assert tools["agent_skill_export"]["annotations"]["readOnlyHint"] is True
    assert tools["agent_chat"]["annotations"]["readOnlyHint"] is False
    assert tools["agent_answer_confirmation"]["annotations"]["readOnlyHint"] is False
    # 取消は書き込み・破壊的・冪等（#3240）。ほかのツールは readOnlyHint だけ。
    assert tools["agent_cancel_run"]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    assert tools["agent_chat"]["annotations"] == {"readOnlyHint": False}
    assert all("outputSchema" in tool for tool in tools.values())

    listed = _call("agent_list", {})["structuredContent"]["agents"]
    mine = next(item for item in listed if item["id"] == AGENT_ID)
    assert (mine["name"], mine["description"]) == ("経理の Agent", "経理の Agentの説明")

    answered = _call("agent_chat", {"agent_id": AGENT_ID, "question": "今月の経費は？"})
    output = answered["structuredContent"]
    assert answered["isError"] is False
    assert (output["status"], output["answer"]) == ("completed", "今月の経費は 50 万円です。")
    assert output["in_progress"] is False and output["confirmation"] is None
    assert output["thread_id"].startswith("thread_")
    run = runtime_repository.get_run(output["run_id"])
    # MCP の Run の印（承認の扱い・監査）。local はキーが無いのでキーの ID は付かない。
    assert run.metadata["source"] == "mcp"
    assert "api_key_id" not in run.metadata
    again = _call("agent_get_run", {"run_id": output["run_id"], "wait_seconds": 0})
    assert again["structuredContent"]["answer"] == "今月の経費は 50 万円です。"


def test_chat_returns_the_run_when_the_answer_is_not_ready(agents: None) -> None:
    del agents
    output = _call("agent_chat", {"agent_id": AGENT_ID, "question": "q", "wait_seconds": 0})
    structured = output["structuredContent"]
    assert structured["status"] in {"queued", "running"}
    assert structured["in_progress"] is True
    assert "agent_get_run" in structured["message"]
    assert "agent_cancel_run" in structured["message"]

    missing = _call("agent_chat", {"agent_id": "no-such-agent", "question": "q"})
    assert missing["isError"] is True
    assert missing["structuredContent"]["error_code"] == "AGENT_NOT_FOUND"
    invalid = _call("agent_chat", {"agent_id": AGENT_ID, "question": "q", "wait_seconds": 999})
    assert invalid["structuredContent"]["error_code"] == "MCP_TOOL_ARGUMENTS_INVALID"
    unknown_run = _call("agent_get_run", {"run_id": "run_missing", "wait_seconds": 0})
    assert unknown_run["structuredContent"]["error_code"] == "RUN_NOT_FOUND"


def test_confirmation_is_returned_and_answered_through_mcp(
    monkeypatch: MonkeyPatch, agents: None
) -> None:
    """回答前の確認（#1839）を構造化して返し、agent_answer_confirmation の答えで続ける。"""
    del agents
    model = _script(
        monkeypatch,
        [function_call(CONFIRMATION_TOOL, ASK_ARGUMENTS, call_id="call-ask")],
        [assistant_message("今年度の経費は 1,200 万円です。")],
    )
    asked = _call("agent_chat", {"agent_id": AGENT_ID, "question": "経費を教えて"})
    output = asked["structuredContent"]
    assert output["status"] == "waiting_confirmation"
    confirmation = output["confirmation"]
    assert confirmation["source"] == "agent"
    [item] = confirmation["items"]
    assert item["prompt"] == "どの期間の経費を見ますか？"
    assert [option["label"] for option in item["options"]] == ["今月", "今年度"]
    assert "agent_answer_confirmation" in output["message"]

    wrong = _call(
        "agent_answer_confirmation",
        {
            "run_id": output["run_id"],
            "confirmation_id": confirmation["id"],
            "answers": [{"item_id": item["id"], "option_ids": ["option_x"]}],
        },
    )
    assert wrong["structuredContent"]["error_code"] == "CONFIRMATION_ANSWER_INVALID"

    answered = _call(
        "agent_answer_confirmation",
        {
            "run_id": output["run_id"],
            "confirmation_id": confirmation["id"],
            "answers": [{"item_id": item["id"], "option_ids": [item["options"][1]["id"]]}],
        },
    )["structuredContent"]
    assert (answered["run_id"], answered["status"]) == (output["run_id"], "completed")
    assert answered["answer"] == "今年度の経費は 1,200 万円です。"
    assert answered["confirmation"] is None
    assert "今年度" in json.dumps(model.calls[1].input, ensure_ascii=False, default=str)
    # 答え済みの確認には答えられない。
    closed = _call(
        "agent_answer_confirmation",
        {"run_id": output["run_id"], "confirmation_id": confirmation["id"], "action": "skip"},
    )
    assert closed["structuredContent"]["error_code"] == "CONFIRMATION_CLOSED"


def test_chat_continues_the_conversation_with_thread_id(
    monkeypatch: MonkeyPatch, agents: None
) -> None:
    del agents
    model = _script(
        monkeypatch,
        [assistant_message("今月の経費は 50 万円です。")],
        [assistant_message("最も多いのは交通費です。")],
    )
    first = _call("agent_chat", {"agent_id": AGENT_ID, "question": "今月の経費は？"})
    thread_id = first["structuredContent"]["thread_id"]
    second = _call(
        "agent_chat",
        {"agent_id": AGENT_ID, "question": "その中で一番多いのは？", "thread_id": thread_id},
    )["structuredContent"]
    assert (second["thread_id"], second["answer"]) == (thread_id, "最も多いのは交通費です。")
    last_input = json.dumps(model.calls[-1].input, ensure_ascii=False, default=str)
    assert "今月の経費は？" in last_input and "今月の経費は 50 万円です。" in last_input
    # 別の業務 Agent・存在しない会話は続けられない（存在を漏らさない）。
    for agent_id, other_thread in ((OTHER_AGENT_ID, thread_id), (AGENT_ID, f"thread_{'0' * 32}")):
        refused = _call(
            "agent_chat", {"agent_id": agent_id, "question": "続き", "thread_id": other_thread}
        )
        assert refused["structuredContent"]["error_code"] == "THREAD_NOT_FOUND"


def test_unpublished_agents_are_not_listed_or_asked(agents: None) -> None:
    del agents
    draft_id = "mcp1874-draft"
    created = client.post(
        "/api/agents", json={"id": draft_id, "name": "下書きの Agent", "instructions": "答える。"}
    )
    assert created.status_code == 200, created.text
    try:
        assert created.json()["data"]["published_version"] is None
        listed = _call("agent_list", {})["structuredContent"]["agents"]
        assert draft_id not in {item["id"] for item in listed}
        refused = _call("agent_chat", {"agent_id": draft_id, "question": "q"})
        assert refused["structuredContent"]["error_code"] == "AGENT_NOT_AVAILABLE"
    finally:
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent(draft_id)


# ---- production（API キー）----------------------------------------------------------------


def test_api_key_acts_as_its_owner_and_is_limited_to_the_key_scope(
    monkeypatch: MonkeyPatch, auth: ProductionAuth, agents: None
) -> None:
    del agents
    _script(monkeypatch, [assistant_message("経費は 50 万円です。")])
    auth.user_with_permissions("mcp1874-op", [RUNS_OPERATE], agent_ids=[AGENT_ID, OTHER_AGENT_ID])
    session = login("mcp1874-op")
    scoped = _issue_key(session, name="経理の連携", resource_scopes={"agent": [AGENT_ID]})
    assert scoped["key"]["resource_scopes"] == {"agent": [AGENT_ID]}
    headers = _bearer(scoped["token"])

    # キーなし・Cookie なしは 401。キーは MCP の入口だけで使える（画面の API は 401）。
    assert _rpc("tools/list").status_code == 401
    assert client.get("/api/agents", headers=headers).status_code == 401
    # キーの範囲の業務 Agent だけが見え、範囲の外は 403。
    listed = _call("agent_list", {}, headers)["structuredContent"]["agents"]
    assert [item["id"] for item in listed] == [AGENT_ID]
    outside = _call("agent_chat", {"agent_id": OTHER_AGENT_ID, "question": "q"}, headers)
    assert outside["structuredContent"]["error_code"] == "AGENT_OUT_OF_KEY_SCOPE"
    assert outside["structuredContent"]["status"] == 403

    answered = _call("agent_chat", {"agent_id": AGENT_ID, "question": "経費は？"}, headers)
    output = answered["structuredContent"]
    assert output["answer"] == "経費は 50 万円です。"
    run = runtime_repository.get_run(output["run_id"])
    # キーを発行した利用者の Run。監査に「MCP（キーの名前）」を出すため、キーの ID と名前を残す。
    owner = auth.store.get_user_by_login_user_id("mcp1874-op")
    assert owner is not None and run.created_by_user_uuid == owner.user_uuid
    assert (run.metadata["source"], run.metadata["api_key_name"]) == ("mcp", "経理の連携")
    assert run.metadata["api_key_id"] == scoped["key"]["id"]

    # 範囲なしのキーは利用者が使えるすべての業務 Agent。
    unscoped = _issue_key(session, name="すべて")
    listed = _call("agent_list", {}, _bearer(unscoped["token"]))["structuredContent"]["agents"]
    assert {item["id"] for item in listed} >= {AGENT_ID, OTHER_AGENT_ID}
    # キーで範囲の外の業務 Agent の Run を読むのも 403。
    other_run = _call(
        "agent_chat",
        {"agent_id": OTHER_AGENT_ID, "question": "q", "wait_seconds": 0},
        _bearer(unscoped["token"]),
    )["structuredContent"]
    denied = _call("agent_get_run", {"run_id": other_run["run_id"], "wait_seconds": 0}, headers)
    assert denied["structuredContent"]["error_code"] == "AGENT_OUT_OF_KEY_SCOPE"


def test_api_key_needs_the_agent_product_and_the_owner_permissions(
    auth: ProductionAuth, agents: None
) -> None:
    del agents
    auth.user_with_permissions("mcp1874-viewer", [RUNS_VIEW], agent_ids=[AGENT_ID])
    session = login("mcp1874-viewer")
    rag_only = _issue_key(session, products=["rag"])
    refused = _rpc("tools/list", headers=_bearer(rag_only["token"]))
    assert refused.status_code == 403
    assert refused.json()["error_code"] == "SECURITY_API_KEY_PRODUCT_FORBIDDEN"

    viewer = _issue_key(session)
    tools = _rpc("tools/list", headers=_bearer(viewer["token"])).json()["result"]["tools"]
    # 閲覧だけの利用者には質問・答えのツールを出さず、呼んでも権限なし。
    assert {tool["name"] for tool in tools} == {
        "agent_list",
        "agent_get_run",
        "agent_get_run_observation",
        "agent_skill_export",
    }
    denied = _call("agent_chat", {"agent_id": AGENT_ID, "question": "q"}, _bearer(viewer["token"]))
    assert denied["structuredContent"]["error_code"] == "MCP_TOOL_FORBIDDEN"

    # ほかの利用者の Run は読めない（存在を漏らさない）。
    auth.user_with_permissions("mcp1874-op2", [RUNS_OPERATE], agent_ids=[AGENT_ID])
    operator = _issue_key(login("mcp1874-op2"))
    asked = _call(
        "agent_chat",
        {"agent_id": AGENT_ID, "question": "q", "wait_seconds": 0},
        _bearer(operator["token"]),
    )["structuredContent"]
    other = _call(
        "agent_get_run", {"run_id": asked["run_id"], "wait_seconds": 0}, _bearer(viewer["token"])
    )
    assert other["structuredContent"]["error_code"] == "RUN_NOT_FOUND"

    # 削除したキーは使えない。
    deleted = client.delete(f"/api/account/api-keys/{viewer['key']['id']}", headers=session)
    assert deleted.status_code == 200
    assert _rpc("tools/list", headers=_bearer(viewer["token"])).status_code == 401


def test_api_key_limits_runs_per_day_and_calls_per_minute(
    monkeypatch: MonkeyPatch, auth: ProductionAuth, agents: None
) -> None:
    del agents
    settings = get_settings()
    monkeypatch.setattr(settings, "app_api_key_runs_per_day", 1)
    auth.user_with_permissions("mcp1874-limit", [RUNS_OPERATE], agent_ids=[AGENT_ID])
    headers = _bearer(_issue_key(login("mcp1874-limit"))["token"])
    chat = {"agent_id": AGENT_ID, "question": "q", "wait_seconds": 0}
    assert _call("agent_chat", chat, headers)["isError"] is False
    limited = _call("agent_chat", chat, headers)["structuredContent"]
    assert (limited["error_code"], limited["status"]) == ("API_KEY_RUN_LIMIT", 429)
    assert limited["details"]["retry_after_seconds"] > 0
    assert limited["details"]["limit"] == 1

    # 1 分あたりの呼び出しの回数は共通の認証が数え、超えたら 429 と Retry-After。
    monkeypatch.setattr(settings, "app_api_key_calls_per_minute", 1)
    api_key_rate_limiter.reset()
    assert _rpc("tools/list", headers=headers).status_code == 200
    throttled = _rpc("tools/list", headers=headers)
    assert throttled.status_code == 429
    assert int(throttled.headers["retry-after"]) >= 1
    assert throttled.json()["error_code"] == "SECURITY_API_KEY_RATE_LIMITED"


def test_run_output_sources_use_the_rag_tool_results_checked_by_the_validation() -> None:
    """根拠は RAG のツールの結果の evidence のうち、回答で引用した chunk（最終の検証が段落を
    裏付けた chunk・回答が書いた ID。画面と同じ判定。#2328）で、無ければ全件。"""
    evidence = [
        {
            "evidence_id": "e1",
            "document_id": "d1",
            "chunk_id": "c1",
            "file_name": "経費精算規程.pdf",
            "excerpt": "翌月10日までに申請する。",
            "locator": {"page_start": 1, "section_path": ["第2条（精算の期限）"]},
        },
        {
            "evidence_id": "e2",
            "document_id": "d2",
            "chunk_id": "c2",
            "file_name": "在宅勤務規程.pdf",
            "excerpt": "x",
        },
    ]
    run = RunState(
        id="run_sources",
        goal="期限は？",
        agent_id=AGENT_ID,
        status=RunStatus.COMPLETED,
        steps=[
            RunStep(
                run_id="run_sources",
                status=StepStatus.COMPLETED,
                tool_call=ToolCall(name="rag__rag_retrieve_evidence", arguments={}),
                tool_result=ToolResult(
                    name="rag__rag_retrieve_evidence", success=True, output={"evidence": evidence}
                ),
            )
        ],
        artifacts=[
            Artifact(name="回答", kind="answer", content={"text": "翌月10日までです。"}),
            Artifact(
                name="資料との照合",
                kind="answer_validation",
                content={
                    "status": "completed",
                    "evidence": [{"chunk_id": "c1"}, {"chunk_id": "c2"}],
                    "result": {"claims": [{"status": "supported", "chunk_id": "c1"}]},
                },
            ),
        ],
    )
    output = mcp_server.run_output(run)
    assert [(item.title, item.location) for item in output.sources] == [
        ("経費精算規程.pdf", "p.1 · 第2条（精算の期限）")
    ]
    assert output.tools_used == ["rag__rag_retrieve_evidence"]
    unvalidated = run.model_copy(update={"artifacts": run.artifacts[:1]})
    assert len(mcp_server.run_output(unvalidated).sources) == 2


def test_run_output_sources_list_one_document_at_a_time() -> None:
    """根拠は資料ごとにまとめ、別の資料の根拠を交互に並べない（資料は初めて現れた順）。"""
    evidence = [
        {
            "evidence_id": f"e{index}",
            "document_id": document,
            "chunk_id": f"c{index}",
            "file_name": f"{document}.pdf",
        }
        for index, document in enumerate(("架空A", "架空B", "架空A"))
    ]
    run = RunState(
        id="run_grouped_sources",
        goal="q",
        agent_id=AGENT_ID,
        status=RunStatus.COMPLETED,
        steps=[
            RunStep(
                run_id="run_grouped_sources",
                status=StepStatus.COMPLETED,
                tool_call=ToolCall(name="rag__rag_retrieve_evidence", arguments={}),
                tool_result=ToolResult(
                    name="rag__rag_retrieve_evidence", success=True, output={"evidence": evidence}
                ),
            )
        ],
        artifacts=[Artifact(name="回答", kind="answer", content={"text": "a"})],
    )
    assert [item.title for item in mcp_server.run_output(run).sources] == [
        "架空A.pdf",
        "架空A.pdf",
        "架空B.pdf",
    ]


def test_audit_shows_mcp_runs_with_the_key_name(agents: None) -> None:
    """監査のツールの呼び出しに、Run のきっかけ「MCP（キーの名前）」を出す（#1874）。"""
    del agents
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="登録して", agent_id=AGENT_ID),
        extra_metadata={"source": "mcp", "api_key_id": "0123456789abcdef", "api_key_name": "経理"},
    )
    runtime_repository.record_builtin_unattended_skips(
        run.id, [ToolCall(name="nl2sql__nl2sql_cancel_requirement", arguments={})]
    )
    audit = client.get(f"/api/audit/tool-calls?run_id={run.id}")
    assert audit.status_code == 200, audit.text
    [record] = audit.json()["data"]["items"]
    assert (record["run_source"], record["api_key_name"]) == ("mcp", "経理")
    assert record["error_code"] == "mcp.approval_required"
    exported = client.get(f"/api/audit/tool-calls.csv?run_id={run.id}").text
    assert "run_source,api_key_name" in exported
    assert ",mcp,経理" in exported


def test_run_output_returns_the_passage_marks_of_the_validation() -> None:
    """agent_chat・agent_get_run は、最終の検証の段落の印・外した段落・回答の警告を返す（#2738）。

    本文（answer）には同じ内容の節が付く（本文だけを読む利用者のため）。
    """
    kept = "申請は翌月10日までです。"
    unverified = "申請後は自動で通知されます。"
    removed = "申請は郵送でもできます。"
    answer = f"{kept}\n\n{unverified}\n\n{removed}"
    result = {
        "valid": False,
        "status": "completed",
        "claims": [
            {"answer_quote": kept, "status": "supported", "chunk_id": "c1", "reason": "記載"},
            {"answer_quote": unverified, "status": "unsupported", "reason": "記載が無い"},
            {"answer_quote": removed, "status": "contradicted", "reason": "窓口だけ"},
        ],
        "findings": [
            {
                "check": "requests",
                "code": "request_missing",
                "severity": "error",
                "message": "要求「費用」に答えていません。",
            }
        ],
        "missing_evidence": [],
        "stale_evidence": [],
    }
    content, published = combine_validations(
        answer, [validation_content("completed", connection="rag", result=result)]
    )
    run = RunState(
        id="run_marks",
        goal="申請の期限は？",
        agent_id=AGENT_ID,
        status=RunStatus.COMPLETED,
        artifacts=[
            Artifact(name="回答", kind="answer", content={"text": published}),
            Artifact(name="資料との照合", kind="answer_validation", content=content),
        ],
    )

    output = mcp_server.run_output(run).model_dump()

    assert output["schema_version"] == mcp_server.RUN_OUTPUT_SCHEMA_VERSION
    assert output["answer"].startswith(f"{kept}\n\n{unverified}\n\n**根拠と矛盾し除いた内容**")
    assert output["passage_marks"] == [
        {
            "text": unverified,
            "state": "unverified",
            "reasons": [
                {"code": "unsupported", "label": "根拠で確かめられない", "detail": "記載が無い"}
            ],
        }
    ]
    assert output["removed_passages"] == [
        {
            "text": removed,
            "reasons": [{"code": "contradicted", "label": "根拠と矛盾", "detail": "窓口だけ"}],
        }
    ]
    assert output["answer_warnings"] == [
        {
            "code": "requests",
            "label": "質問の要求の一部に答えていない",
            "detail": "要求「費用」に答えていません。",
        }
    ]
    # 完了していない Run には返さない。
    running = run.model_copy(update={"status": RunStatus.RUNNING})
    assert mcp_server.run_output(running).passage_marks == []


def _evidence_run(items: list[dict[str, Any]], *, outcome: object = None) -> RunState:
    """成果物「回答の根拠」を保存した完了の Run（合成の値）。"""
    answer: dict[str, Any] = {"text": "合成の回答"}
    if outcome is not None:
        answer["outcome"] = outcome
    return RunState(
        id="run_v3",
        goal="合成の質問",
        agent_id=AGENT_ID,
        status=RunStatus.COMPLETED,
        artifacts=[
            Artifact(name="回答", kind="answer", content=answer),
            Artifact(name="回答の根拠", kind="answer_evidence", content={"items": items}),
        ],
    )


def _item(document_id: str, chunk_id: str, *, cited: bool) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "chunk_id": chunk_id,
        "file_name": f"{document_id}.pdf",
        "excerpt": "合成の抜粋",
        "cited": cited,
    }


def test_run_output_v3_returns_the_outcome_and_the_rag_ids_of_the_sources() -> None:
    """schema_version 3: 回答の対応（RAG と同じ語彙）・根拠の RAG の ID・引用の印（#3240）。"""
    run = _evidence_run(
        [
            _item("doc-x", "doc-x:c1", cited=True),
            _item("doc-y", "doc-y:c1", cited=False),
            _item("attachment:att-1", "attachment:att-1:s1", cited=True),
        ],
        outcome={"schema_version": 1, "value": "conditional", "basis": "basis-canary"},
    )
    output = mcp_server.run_output(run).model_dump()
    assert output["schema_version"] == mcp_server.RUN_OUTPUT_SCHEMA_VERSION == 4
    assert (output["outcome"], output["withheld_evidence_count"]) == ("conditional", 0)
    # 引用した根拠だけ。会話の添付の根拠は RAG の文書ではないので ID を出さない。
    assert [
        (item["document_id"], item["chunk_id"], item["cited"]) for item in output["sources"]
    ] == [("doc-x", "doc-x:c1", True), (None, None, True)]
    # 判断の道筋（basis）は出さない。
    assert "basis-canary" not in json.dumps(output, ensure_ascii=False)
    assert output["sources"][0]["excerpt"] == "合成の抜粋"

    # 引用の無い回答は、AI に渡した根拠をすべて cited=false で出す。
    uncited = _evidence_run([_item("doc-x", "doc-x:c1", cited=False)])
    [source] = mcp_server.run_output(uncited).sources
    assert (source.document_id, source.chunk_id, source.cited) == ("doc-x", "doc-x:c1", False)


def _text_item(chunk_id: str, text: str, **fields: Any) -> dict[str, Any]:
    return {**_item(f"doc-{chunk_id}", chunk_id, cited=True), "excerpt": text, **fields}


def test_run_output_sources_return_full_texts_within_the_response_budget() -> None:
    """根拠は全文を根拠の順に応答の予算へ入れ、入らない根拠は本文を省く（RAG と同じ。#3321）。"""
    texts = ["合成の本文。" * 500 if index != 6 else "短い合成の本文。" for index in range(10)]
    run = _evidence_run([_text_item(f"c{index}", text) for index, text in enumerate(texts)])
    output = mcp_server.run_output(run).model_dump()
    sources = output["sources"]
    assert [source["chunk_id"] for source in sources] == [f"c{index}" for index in range(10)]
    omitted = [source["text_omitted"] for source in sources]
    first = omitted.index(True)
    # 先頭の根拠から入れ、入らない根拠の後ろでも小さな根拠は入れる（400 文字で切らない）。
    assert first >= 1 and not any(omitted[:first]) and omitted[6] is False
    for source, text in zip(sources, texts, strict=True):
        assert source["text_length"] == len(text) and source["truncated"] is False
        if source["text_omitted"]:
            assert (source["excerpt"], source["excerpt_range"]) == ("", None)
        else:
            assert source["excerpt"] == text
            assert source["excerpt_range"] == {"start": 0, "end": len(text)}
    assert output["sources_text_omitted"] == sum(omitted)
    payload = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
    assert estimate_input_tokens(payload) <= mcp_server.SOURCE_TEXT_BUDGET_TOKENS


def test_run_output_sources_keep_what_rag_omitted_or_truncated() -> None:
    """RAG が本文を省いた・切った根拠は、その印と chunk の中の位置をそのまま返す。"""
    run = _evidence_run(
        [
            _text_item("c-omitted", "", text_omitted=True, text_length=9000),
            _text_item(
                "c-cut",
                "合成の一部。",
                truncated=True,
                text_length=9000,
                excerpt_range={"start": 4000, "end": 4006},
                next_offset=4006,
            ),
        ]
    )
    omitted, cut = mcp_server.run_output(run).sources
    assert (omitted.text_omitted, omitted.excerpt, omitted.text_length) == (True, "", 9000)
    assert (omitted.document_id, omitted.chunk_id) == ("doc-c-omitted", "c-omitted")
    assert (cut.truncated, cut.excerpt, cut.text_length, cut.next_offset) == (
        True,
        "合成の一部。",
        9000,
        4006,
    )
    assert cut.excerpt_range is not None and (cut.excerpt_range.start, cut.excerpt_range.end) == (
        4000,
        4006,
    )


def test_run_output_cuts_an_oversize_table_source_at_row_boundaries() -> None:
    """1 件の上限を超える表の根拠は、質問に当たる行を中心に見出しの行を残して行の境界で切る。"""
    header = "<table><tr><th>列A</th><th>列B</th></tr>"
    rows = "".join(
        f"<tr><td>行{index}</td><td>{'目印の値' if index == 900 else '合成の値'}</td></tr>"
        for index in range(1200)
    )
    table = header + rows + "</table>"
    run = _evidence_run([_text_item("t1", table, evidence_type="table")]).model_copy(
        update={"goal": "目印の値は？"}
    )
    [source] = mcp_server.run_output(run).sources
    assert source.truncated and not source.text_omitted
    assert estimate_input_tokens(source.excerpt) <= mcp_server.SOURCE_TEXT_ITEM_MAX_TOKENS
    assert source.excerpt.startswith(header)
    body = source.excerpt[len(header) :]
    assert body.startswith("<tr>") and body.endswith("</tr>") and "目印の値" in body
    assert source.header_range is not None and source.header_range.end == len(header)
    assert source.excerpt_range is not None and source.next_offset == source.excerpt_range.end
    assert table[source.excerpt_range.start : source.excerpt_range.end] == body


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (RunStatus.COMPLETED, None),
        (RunStatus.COMPLETED, {"value": "unknown-value"}),
        (RunStatus.RUNNING, {"value": "answered"}),
        (RunStatus.FAILED, {"value": "answered"}),
        (RunStatus.CANCELLED, {"value": "answered"}),
    ],
    ids=["completed-without-outcome", "unknown-value", "running", "failed", "cancelled"],
)
def test_run_output_outcome_is_null_unless_completed_with_a_known_value(
    status: RunStatus, outcome: object
) -> None:
    run = _evidence_run([], outcome=outcome).model_copy(update={"status": status})
    assert mcp_server.run_output(run).outcome is None


def test_chat_returns_the_outcome_of_the_completed_run(
    monkeypatch: MonkeyPatch, agents: None
) -> None:
    """agent_chat の完了の応答に、Runtime が決めた回答の対応を返す（実行中は null）。"""
    del agents
    _script(monkeypatch, [assistant_message("合成の回答です。")])
    output = _call("agent_chat", {"agent_id": AGENT_ID, "question": "合成の質問"})[
        "structuredContent"
    ]
    assert output["status"] == "completed"
    stored = runtime_repository.get_run(output["run_id"])
    [answer] = [item for item in stored.artifacts if item.kind == "answer"]
    assert output["outcome"] == answer.content["outcome"]["value"]
    assert output["outcome"] in {"answered", "conditional", "insufficient_evidence"}


# ---- 取消（agent_cancel_run。#3240）-----------------------------------------------------


def _queued() -> RunState:
    return runtime_repository.create_builtin_run(
        RunCreateRequest(goal="合成の質問", agent_id=AGENT_ID)
    )


def _running() -> RunState:
    run = _queued()
    assert runtime_repository.begin_builtin_run(run.id) is not None
    return runtime_repository.get_run(run.id)


def _waiting_approval() -> RunState:
    run = _running()
    return runtime_repository.request_builtin_approvals(
        run.id, [ToolCall(name="nl2sql__nl2sql_query", arguments={}, trace_id="c1")], state="{}"
    )


def _waiting_confirmation() -> RunState:
    run = _running()
    request = ConfirmationRequest(
        source="agent", call_id="call-ask", items=[item_from_arguments(ASK_ARGUMENTS)]
    )
    return runtime_repository.request_builtin_approvals(
        run.id, [], state="{}", confirmations=[request]
    )


def _cancel_event(run_id: str) -> dict[str, Any]:
    [event] = [
        item
        for item in runtime_repository.get_run(run_id).events
        if item.type == RunEventType.RUN_CANCELLED
    ]
    return event.payload


@pytest.mark.parametrize(
    "make",
    [_queued, _running, _waiting_confirmation, _waiting_approval],
    ids=["queued", "running", "waiting_confirmation", "waiting_approval"],
)
def test_cancel_run_cancels_an_unfinished_run_once(agents: None, make: Any) -> None:
    del agents
    run = make()
    first = _call("agent_cancel_run", {"run_id": run.id})
    assert first["isError"] is False
    assert first["structuredContent"] == {
        "schema_version": 1,
        "run_id": run.id,
        "status": "cancelled",
        "result": "cancelled",
        "message": "実行を取り消しました。",
    }
    stored = runtime_repository.get_run(run.id)
    # 承認・回答前の確認の待ちも閉じる。
    assert all(item.status == "cancelled" for item in stored.approvals)
    assert all(item.status == "cancelled" for item in stored.confirmations)
    # 入口（MCP）を残す。local はキーが無いのでキーの ID は付かない。
    payload = _cancel_event(run.id)
    assert payload["via"] == "mcp" and "api_key_id" not in payload
    # 冪等: 2 回目はエラーにせず already_cancelled（イベントも増やさない）。
    again = _call("agent_cancel_run", {"run_id": run.id})
    assert again["isError"] is False
    assert (again["structuredContent"]["result"], again["structuredContent"]["status"]) == (
        "already_cancelled",
        "cancelled",
    )
    assert _cancel_event(run.id) == payload


@pytest.mark.parametrize("status", [RunStatus.COMPLETED, RunStatus.FAILED], ids=str)
def test_cancel_run_leaves_a_finished_run_as_it_is(agents: None, status: RunStatus) -> None:
    del agents
    run = _running()
    if status == RunStatus.COMPLETED:
        runtime_repository.complete_builtin_run(run.id, "合成の回答")
    else:
        runtime_repository.fail_builtin_run(run.id, code="runtime.failed", detail="合成の失敗")
    output = _call("agent_cancel_run", {"run_id": run.id})
    assert output["isError"] is False
    assert (output["structuredContent"]["result"], output["structuredContent"]["status"]) == (
        "already_finished",
        status.value,
    )
    assert "agent_get_run" in output["structuredContent"]["message"]
    assert runtime_repository.get_run(run.id).status == status
    # 画面の REST は今どおり 409。
    rest = client.post(f"/api/runs/{run.id}/cancel")
    assert rest.status_code == 409, rest.text


def test_cancel_run_keeps_the_result_when_the_run_completes_first(
    agents: None, monkeypatch: MonkeyPatch
) -> None:
    """取消と完了が重なり完了が先に決まったら、完了を残して already_finished（#911）。"""
    del agents
    run = _running()
    original = runtime_repository.cancel_run

    def complete_then_cancel(run_id: str, **kwargs: Any) -> RunState:
        runtime_repository.complete_builtin_run(run_id, "合成の回答")
        return original(run_id, **kwargs)

    monkeypatch.setattr(runtime_repository, "cancel_run", complete_then_cancel)
    output = _call("agent_cancel_run", {"run_id": run.id})["structuredContent"]
    assert (output["result"], output["status"]) == ("already_finished", "completed")
    assert runtime_repository.get_run(run.id).status == RunStatus.COMPLETED


def test_cancel_run_with_an_api_key_records_the_key_and_ignores_the_run_budget(
    monkeypatch: MonkeyPatch, auth: ProductionAuth, agents: None
) -> None:
    """取消は Run を作らないので Run の数の上限に数えない。イベントに操作した人とキーを残す。"""
    del agents
    monkeypatch.setattr(get_settings(), "app_api_key_runs_per_day", 1)
    auth.user_with_permissions("mcp3240-cancel", [RUNS_OPERATE], agent_ids=[AGENT_ID])
    key = _issue_key(login("mcp3240-cancel"))
    headers = _bearer(key["token"])
    chat = {"agent_id": AGENT_ID, "question": "q", "wait_seconds": 0}
    created = _call("agent_chat", chat, headers)["structuredContent"]
    limited = _call("agent_chat", chat, headers)["structuredContent"]
    assert limited["error_code"] == "API_KEY_RUN_LIMIT"
    output = _call("agent_cancel_run", {"run_id": created["run_id"]}, headers)
    assert output["structuredContent"]["result"] == "cancelled", output
    payload = _cancel_event(created["run_id"])
    assert (payload["cancelled_by"], payload["via"], payload["api_key_id"]) == (
        "mcp3240-cancel",
        "mcp",
        key["key"]["id"],
    )

    # 閲覧だけの利用者のキーには出さず、呼んでも権限なし。
    auth.user_with_permissions("mcp3240-viewer", [RUNS_VIEW], agent_ids=[AGENT_ID])
    viewer = _bearer(_issue_key(login("mcp3240-viewer"))["token"])
    denied = _call("agent_cancel_run", {"run_id": created["run_id"]}, viewer)
    assert denied["structuredContent"]["error_code"] == "MCP_TOOL_FORBIDDEN"
    # キーの業務 Agent の範囲の外の Run は 403。
    scoped = _bearer(
        _issue_key(login("mcp3240-cancel"), resource_scopes={"agent": [OTHER_AGENT_ID]})["token"]
    )
    outside = _call("agent_cancel_run", {"run_id": created["run_id"]}, scoped)
    assert outside["structuredContent"]["error_code"] == "AGENT_OUT_OF_KEY_SCOPE"
    missing = _call("agent_cancel_run", {"run_id": "run_missing"}, headers)
    assert missing["structuredContent"]["error_code"] == "RUN_NOT_FOUND"


def _cancellation(run_id: str, headers: dict[str, str]) -> dict[str, Any] | None:
    response = client.get(f"/api/runs/{run_id}", headers=headers)
    assert response.status_code == 200, response.text
    value: dict[str, Any] | None = response.json()["data"]["cancellation"]
    return value


def test_run_detail_shows_who_cancelled_and_the_key_visible_to_the_viewer(
    auth: ProductionAuth, agents: None
) -> None:
    """実行の詳細の「取消」: 取り消した人・入口と、見ている人が見られるキーの名前だけ（#3348）。"""
    del agents
    auth.user_with_permissions("mcp3348-a", [RUNS_OPERATE], agent_ids=[AGENT_ID])
    auth.user_with_permissions("mcp3348-b", [RUNS_OPERATE], agent_ids=[AGENT_ID])
    auth.create_user("mcp3348-admin", system_admin=True)
    user_a, user_b, admin = login("mcp3348-a"), login("mcp3348-b"), login("mcp3348-admin")
    key_a = _bearer(_issue_key(user_a, name="合成のキー A")["token"])
    key_admin = _bearer(_issue_key(admin, name="合成のキー 管理")["token"])
    chat = {"agent_id": AGENT_ID, "question": "q", "wait_seconds": 0}

    # 本人が自分のキーで取り消した Run: 本人も system_admin もキーの名前が見える。
    mine = _call("agent_chat", chat, key_a)["structuredContent"]["run_id"]
    assert _cancellation(mine, user_a) is None
    _call("agent_cancel_run", {"run_id": mine}, key_a)
    expected = {"via": "mcp", "actor_display_name": "mcp3348-a", "api_key_name": "合成のキー A"}
    assert _cancellation(mine, user_a) == {**expected, "by_me": True}
    assert _cancellation(mine, admin) == {**expected, "by_me": False}

    # system_admin が自分のキーで他の利用者の Run を取り消した: 持ち主にはキーの名前を出さない。
    others = _call("agent_chat", chat, _bearer(_issue_key(user_b)["token"]))["structuredContent"]
    _call("agent_cancel_run", {"run_id": others["run_id"]}, key_admin)
    assert _cancellation(others["run_id"], user_b) == {
        "via": "mcp",
        "actor_display_name": "mcp3348-admin",
        "by_me": False,
        "api_key_name": "",
    }
    assert _cancellation(others["run_id"], admin) == {
        "via": "mcp",
        "actor_display_name": "mcp3348-admin",
        "by_me": True,
        "api_key_name": "合成のキー 管理",
    }

    # 画面の取消は入口が screen（キーの名前は無い）。
    screen = _call("agent_chat", chat, key_a)["structuredContent"]["run_id"]
    assert client.post(f"/api/runs/{screen}/cancel", headers=user_a).status_code == 200
    assert _cancellation(screen, user_a) == {
        "via": "screen",
        "actor_display_name": "mcp3348-a",
        "by_me": True,
        "api_key_name": "",
    }


def test_run_cancel_record_without_an_actor_is_a_system_cancel(agents: None) -> None:
    """操作した人の記録の無い取消（品質評価の後始末など）は system（#3348）。"""
    del agents
    run = _running()
    runtime_repository.cancel_run(run.id)
    record = run_cancel_record(runtime_repository.get_run(run.id))
    assert record is not None
    assert (record.via, record.cancelled_by, record.api_key_id) == ("system", None, None)
    assert run_cancel_record(_running()) is None
