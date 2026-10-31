"""根拠・データのツールを呼ばずに作った回答の差し戻しと拒答（#1835）の決定論テスト。

モデルは SDK の `ScriptedModel`、RAG / NL2SQL は契約どおりの fake の MCP（`mcp_support`）にする。
ツールを呼ばずに答えたら 1 回だけ差し戻し、ツールを呼べば今どおり、呼ばなければ決まった拒答の文に
置き換えることと、確認の質問だけの回答・ツールを持たない Agent・無効の設定は対象外であることを
確かめる。
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from copy import deepcopy
from typing import Any

import anyio
import pytest
from agents import Usage
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from mcp_support import DEFAULT_OUTPUTS, FakeProductMcp, McpToolError, fake_product_mcp
from pytest import MonkeyPatch

from app.features.agent import builtin_runtime, tool_grounding
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.runtime import (
    EVALUATION_DRY_RUN_KEY,
    AgentProfile,
    AgentProfilePatch,
    ApprovalDecisionRequest,
    RunCreateRequest,
    RunState,
    RunStatus,
    RunStep,
    StepStatus,
    run_answer_text,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.tools import (
    ToolCall,
    ToolDefinition,
    ToolPermissionLevel,
    ToolPolicy,
    ToolResult,
    tool_registry,
)
from app.settings import get_settings

SKILL_ID = "test1835-skill"
AGENT_ID = "test1835-agent"
BARE_AGENT_ID = "test1835-bare-agent"
USER_UUID = "11111111-2222-3333-4444-555555555555"
# 承認が要る Control Plane のツール（根拠・データのツールには数えない）。
WRITE = "test1835_write"
SEARCH = "rag__rag_search"
QUERY = "nl2sql__nl2sql_query"
UNGROUNDED = "日本の首都は東京です。"


@pytest.fixture
def mcp(monkeypatch: MonkeyPatch) -> Iterator[FakeProductMcp]:
    """RAG の検索と NL2SQL の問い合わせを持つ業務 Agent と、ツールを持たない業務 Agent。"""
    fake = fake_product_mcp(monkeypatch)
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", False)
    monkeypatch.setattr(get_settings(), "agent_tool_grounding_required", True)
    # ツール権限は保存先の状態（ほかのテストの変更）に依らず、データのツールと登録のツールだけ承認に
    # する（並列・直列のどちらでも同じ結果にする）。
    monkeypatch.setattr(builtin_runtime, "_active_policy", lambda: ToolPolicy(ask={QUERY, WRITE}))
    tool_registry.register(
        ToolDefinition(
            name=WRITE,
            description="登録のテスト用ツール",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            permission_level=ToolPermissionLevel.WRITE,
            side_effects=True,
        ),
        lambda arguments, context: {"registered": True},
    )
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name="経理の調べ物",
            instructions="RAG と NL2SQL で調べて答える。",
            mcp_requirements=[
                SkillMcpRequirement(server_id="rag", tool_names=["rag_search"]),
                SkillMcpRequirement(server_id="nl2sql", tool_names=["nl2sql_query"]),
                SkillMcpRequirement(server_id="control-plane", tool_names=[WRITE]),
            ],
        )
    )
    runtime_repository.create_agent(
        AgentProfile(
            id=AGENT_ID,
            name="経理の業務 Agent",
            description="経費精算と受注の質問に答えます。",
            skill_ids=[SKILL_ID],
        )
    )
    runtime_repository.create_agent(AgentProfile(id=BARE_AGENT_ID, name="雑談の業務 Agent"))
    try:
        yield fake
    finally:
        repository: Any = runtime_repository
        with repository._lock:  # noqa: SLF001 - テストの後始末
            for run_id in [
                run_id
                for run_id, run in repository._runs.items()
                if run.agent_id in {AGENT_ID, BARE_AGENT_ID}
            ]:
                run = repository._runs.pop(run_id)
                for approval in run.approvals:
                    repository._approvals.pop(approval.id, None)
        for agent_id in (AGENT_ID, BARE_AGENT_ID):
            with contextlib.suppress(KeyError, ValueError):
                runtime_repository.delete_agent(agent_id)
        with contextlib.suppress(KeyError, ValueError):
            skill_registry.remove(SKILL_ID)
        tool_registry._definitions.pop(WRITE, None)  # noqa: SLF001 - テスト用の登録を戻す
        tool_registry._handlers.pop(WRITE, None)  # noqa: SLF001


def _script(monkeypatch: MonkeyPatch, *steps: Any) -> ScriptedModel:
    model = ScriptedModel(list(steps))
    monkeypatch.setattr(
        builtin_runtime,
        "resolve_model_target",
        lambda model_id="": ModelTarget(
            model_id=model_id or "test-model",
            endpoint="https://oci.example",
            project_ocid="",
            api_key="k",
        ),
    )
    monkeypatch.setattr(builtin_runtime, "model_factory", lambda _target: model)
    return model


def _run(
    goal: str = "日本の首都は？",
    *,
    agent_id: str = AGENT_ID,
    metadata: dict[str, Any] | None = None,
    thread_id: str | None = None,
) -> RunState:
    created = runtime_repository.create_builtin_run(
        RunCreateRequest(
            goal=goal, agent_id=agent_id, metadata=metadata or {}, thread_id=thread_id
        ),
        created_by_user_uuid=USER_UUID,
    )
    anyio.run(builtin_runtime.execute_run, created.id)
    return runtime_repository.get_run(created.id)


def _answer_content(run: RunState) -> dict[str, Any]:
    [artifact] = [item for item in run.artifacts if item.kind == "answer"]
    return artifact.content


def _grounding_events(run: RunState) -> list[dict[str, Any]]:
    return [
        event.payload["tool_grounding"] for event in run.events if "tool_grounding" in event.payload
    ]


def _usage(requests: int, input_tokens: int, output_tokens: int) -> Usage:
    return Usage(
        requests=requests,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


def _last_user_message(call_input: Any) -> str:
    assert isinstance(call_input, list)
    message = call_input[-1]
    assert message["role"] == "user"
    return str(message["content"])


def test_answer_after_the_retry_with_a_tool_is_kept(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    model = _script(
        monkeypatch,
        ModelStep(output=[assistant_message("締め日は毎月 25 日です。")], usage=_usage(1, 100, 10)),
        ModelStep(
            output=[function_call(SEARCH, {"query": "経費精算 締め日"}, call_id="call-1")],
            usage=_usage(1, 200, 20),
        ),
        ModelStep(output=[assistant_message("締め日は毎月 20 日です。")], usage=_usage(1, 300, 30)),
    )

    run = _run("経費精算の締め日は？")

    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    assert run_answer_text(run) == "締め日は毎月 20 日です。"
    assert len(mcp.calls_of("rag_search")) == 1
    # 差し戻しは利用者のメッセージとして、前の回答の後に足す（同じ会話の続き）。
    retry_input = model.calls[1].input
    # 前の assistant のメッセージは入力に入れず（OCI の gpt-oss は output_text を受け付けない）、
    # 差し戻しのメッセージに元の回答を引用して添える。
    # チャットの Run（方針「確認する」）は ask_user_confirmation を渡すので、本文で聞き返さず
    # 資料のツールか確認のカードで確かめる差し戻しの文にする（#1881）。
    assert _last_user_message(retry_input) == (
        f"{tool_grounding.RETRY_MESSAGE_CONFIRMABLE}\n\n直前の回答（利用者には出していません）:\n"
        "締め日は毎月 25 日です。"
    )
    assert not any(
        isinstance(item, dict) and item.get("role") == "assistant" for item in retry_input
    )
    assert retry_input[0] == {"role": "user", "content": "経費精算の締め日は？"}
    # 元の回答は Run の経過にだけ残す。
    assert _grounding_events(run) == [
        {"action": "retry", "original_answer": "締め日は毎月 25 日です。"}
    ]
    assert run.metadata[tool_grounding.METADATA_KEY] == {"retries": 1}
    # 回答の対応は今どおり（拒答ではない）。
    assert _answer_content(run)["outcome"]["basis"] != tool_grounding.BASIS_NO_TOOL_GROUNDING
    # 差し戻しの分の利用量も積む。
    assert run.usage is not None
    assert (run.usage.requests, run.usage.input_tokens, run.usage.output_tokens) == (3, 600, 60)


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param({}, id="chat"),
        pytest.param(
            {"automation_id": "auto_1835", "automation_trigger": "schedule"}, id="automation"
        ),
        pytest.param({EVALUATION_DRY_RUN_KEY: True}, id="evaluation"),
    ],
)
def test_answer_without_a_tool_after_the_retry_is_refused(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp, metadata: dict[str, Any]
) -> None:
    model = _script(
        monkeypatch,
        ModelStep(output=[assistant_message(UNGROUNDED)], usage=_usage(1, 100, 10)),
        ModelStep(output=[assistant_message("首都は東京です。")], usage=_usage(1, 150, 15)),
    )

    run = _run(metadata=metadata)

    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    assert len(model.calls) == 2
    assert mcp.calls_of("rag_search") == [] and mcp.calls_of("nl2sql_query") == []
    # 差し戻しの文は、確認のツールを渡す Run（チャット）と、利用者に確認しない無人の Run で分ける
    # （#1881・#1959）。
    expected = tool_grounding.retry_message(confirmable=not metadata, assume=bool(metadata))
    assert _last_user_message(model.calls[1].input).startswith(expected)
    text = run_answer_text(run)
    assert text == tool_grounding.refusal_text(
        description="経費精算と受注の質問に答えます。",
        skill_names=["経理の調べ物"],
        tool_names=[SEARCH, QUERY, WRITE],
    )
    assert text.startswith(tool_grounding.REFUSAL_HEAD)
    scope = "この業務 Agent が扱う内容: 経費精算と受注の質問に答えます。（スキル: 経理の調べ物）"
    assert scope in text
    assert "「経費精算規程の締め日は？」「先月の部門別の受注金額は？」" in text
    assert UNGROUNDED not in text
    content = _answer_content(run)
    assert content["outcome"] == {
        "schema_version": 1,
        "value": "insufficient_evidence",
        "basis": "no_tool_grounding",
        "rag_outcome": None,
        "signals": [],
    }
    # 最終の検証の成果物は「拒答した」（資料の根拠を使わずに答えた no_rag_evidence と分ける）。
    [validation] = [item for item in run.artifacts if item.kind == "answer_validation"]
    assert (validation.content["status"], validation.content["reason"]) == (
        "skipped",
        "no_tool_grounding",
    )
    assert _grounding_events(run) == [
        {"action": "retry", "original_answer": UNGROUNDED},
        {"action": "refused", "original_answer": "首都は東京です。"},
    ]
    assert run.metadata[tool_grounding.METADATA_KEY] == {"retries": 1, "refused": True}
    assert run.usage is not None
    assert (run.usage.requests, run.usage.total_tokens) == (2, 275)


def test_text_clarification_without_tools_is_sent_back_once_when_the_tool_is_offered(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """確認のツールを渡した Run で、ツールを呼ばずに本文で聞き返したら 1 回差し戻す（#1881）。

    差し戻しの後も本文の聞き返しなら、拒答にせずそのまま出し、経過に残す。
    """
    question = "確認させてください。\nどの件のことですか？（経費精算／受注）"
    model = _script(
        monkeypatch,
        ModelStep(output=[assistant_message(question)], usage=_usage(1, 100, 10)),
        ModelStep(output=[assistant_message(question)], usage=_usage(1, 150, 15)),
    )

    run = _run("あの件どうなった？")

    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    assert len(model.calls) == 2
    assert mcp.calls_of("rag_search") == []
    retry = _last_user_message(model.calls[1].input)
    assert retry.startswith(tool_grounding.RETRY_MESSAGE_CONFIRMABLE)
    assert run_answer_text(run) == question
    assert _answer_content(run)["outcome"]["value"] == "needs_clarification"
    assert _grounding_events(run) == [
        {"action": "retry", "original_answer": question},
        {"action": "text_clarification", "original_answer": question},
    ]
    assert run.metadata[tool_grounding.METADATA_KEY] == {"retries": 1, "text_clarification": True}


def test_text_clarification_retry_that_searches_keeps_the_answer(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """差し戻しでツールを呼んで答えたら、その回答を出す（#1881）。"""
    model = _script(
        monkeypatch,
        ModelStep(
            output=[assistant_message("どの件のことですか？（経費精算／受注）")],
            usage=_usage(1, 100, 10),
        ),
        ModelStep(
            output=[function_call(SEARCH, {"query": "経費精算 状況"}, call_id="call-1")],
            usage=_usage(1, 200, 20),
        ),
        ModelStep(output=[assistant_message("経費精算は承認待ちです。")], usage=_usage(1, 300, 30)),
    )

    run = _run("あの件どうなった？")

    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    assert len(model.calls) == 3
    assert len(mcp.calls_of("rag_search")) == 1
    assert run_answer_text(run) == "経費精算は承認待ちです。"
    assert [event["action"] for event in _grounding_events(run)] == ["retry"]


def test_text_clarification_after_the_retry_is_kept_and_recorded(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """差し戻しの後も本文で確認の質問だけを返したら、拒答にせず経過に残す（#1881）。"""
    question = "どの件のことですか？（経費精算／受注）"
    model = _script(
        monkeypatch,
        ModelStep(output=[assistant_message(UNGROUNDED)], usage=_usage(1, 100, 10)),
        ModelStep(output=[assistant_message(question)], usage=_usage(1, 150, 15)),
    )

    run = _run("あの件どうなった？")

    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    assert len(model.calls) == 2
    assert mcp.calls_of("rag_search") == []
    assert run_answer_text(run) == question
    assert _answer_content(run)["outcome"]["value"] == "needs_clarification"
    # 確認のツールを渡したのに、カードではなく本文で聞き返したことを改善の手がかりとして残す。
    assert _grounding_events(run) == [
        {"action": "retry", "original_answer": UNGROUNDED},
        {"action": "text_clarification", "original_answer": question},
    ]
    assert run.metadata[tool_grounding.METADATA_KEY] == {"retries": 1, "text_clarification": True}


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param({"automation_id": "auto_1881", "automation_trigger": "schedule"}, id="auto"),
        pytest.param({EVALUATION_DRY_RUN_KEY: True}, id="evaluation"),
    ],
)
def test_unattended_run_sends_back_text_clarification_with_the_assume_message(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp, metadata: dict[str, Any]
) -> None:
    """無人の Run（品質評価・自動実行）の本文の聞き返し（#1959）。

    利用者に聞けないので、確認の質問だけの回答も「前提を示して回答」の文で差し戻す（推薦が null の
    ときも候補・一覧から選ばせる）。差し戻しの後も聞き返しなら、拒答にせず経過に残して出す。
    """
    del mcp
    question = "どの業務プロファイルを使いますか？"
    model = _script(
        monkeypatch,
        ModelStep(output=[assistant_message(question)], usage=_usage(1, 100, 10)),
        ModelStep(output=[assistant_message(question)], usage=_usage(1, 150, 15)),
    )

    run = _run("サンプル工業が注文した商品の名前をすべて挙げてください。", metadata=metadata)

    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    assert len(model.calls) == 2
    retry = _last_user_message(model.calls[1].input)
    assert retry.startswith(tool_grounding.RETRY_MESSAGE_ASSUME)
    assert "推薦の候補" in retry
    assert "返してかまいません" not in retry
    assert run_answer_text(run) == question
    assert _grounding_events(run) == [
        {"action": "retry", "original_answer": question},
        {"action": "text_clarification", "original_answer": question},
    ]


def test_assume_policy_retries_with_the_message_that_answers_with_assumptions(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    runtime_repository.patch_agent(AGENT_ID, AgentProfilePatch(clarification_policy="assume"))
    runtime_repository.publish_agent(AGENT_ID)
    model = _script(
        monkeypatch,
        ModelStep(output=[assistant_message(UNGROUNDED)], usage=_usage(1, 100, 10)),
        ModelStep(
            output=[function_call(SEARCH, {"query": "首都"}, call_id="call-1")],
            usage=_usage(1, 200, 20),
        ),
        ModelStep(output=[assistant_message("資料では東京です。")], usage=_usage(1, 300, 30)),
    )

    run = _run()

    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    assert len(mcp.calls_of("rag_search")) == 1
    retry = _last_user_message(model.calls[1].input)
    # 利用者に確認しない Run（#1903）: 確認の質問をさせず、ツールで調べて前提を示して答えさせる。
    assert retry.startswith(tool_grounding.RETRY_MESSAGE_ASSUME)
    assert "ask_user_confirmation" not in retry
    assert "返してかまいません" not in retry


def test_assume_policy_sends_back_text_clarification_and_keeps_it_after_the_retry(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """方針「前提を示して回答」で、ツールを呼ばずに本文で聞き返した回答（#1903 の追加）。

    確認の質問だけの回答も差し戻し、差し戻しの後も聞き返しなら、拒答にせず経過に残して出す。
    """
    runtime_repository.patch_agent(AGENT_ID, AgentProfilePatch(clarification_policy="assume"))
    runtime_repository.publish_agent(AGENT_ID)
    question = "どのシステムまたはサービスに対してアクセス権限を付与しますか？"
    model = _script(
        monkeypatch,
        ModelStep(output=[assistant_message(question)], usage=_usage(1, 100, 10)),
        ModelStep(output=[assistant_message(question)], usage=_usage(1, 150, 15)),
    )

    run = _run("アクセス権限を付与したい")

    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    assert len(model.calls) == 2
    retry = _last_user_message(model.calls[1].input)
    assert retry.startswith(tool_grounding.RETRY_MESSAGE_ASSUME)
    assert run_answer_text(run) == question
    assert _grounding_events(run) == [
        {"action": "retry", "original_answer": question},
        {"action": "text_clarification", "original_answer": question},
    ]


@pytest.mark.parametrize(
    ("question", "answer"),
    [
        ("こんにちは", "こんにちは。お手伝いできることがありましたら、お知らせください。"),
        ("こんにちは", "こんにちは。ご用件をお知らせください。"),
        # お礼・質問でない入力に用件を聞く返事（#1929。スキルの「入力の種類を分ける」）。
        (
            "ありがとう",
            "どういたしまして。ほかに社内の規程や手順について知りたいことがあれば、"
            "お知らせください。",
        ),
        ("テスト", "社内の規程や手順について、知りたいことをお知らせください。"),
        ("あ", "何についてお調べしましょうか？"),
    ],
    ids=["help", "request", "thanks", "test-input", "short-input"],
)
def test_greeting_that_asks_for_the_request_is_not_sent_back(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp, question: str, answer: str
) -> None:
    """あいさつ・お礼・質問でない入力に用件を聞いて答えた回答（#1879・#1929）は確認の質問として
    扱い、差し戻し・拒答にしない（確認のツールを渡した Run でも）。"""
    del mcp
    model = _script(monkeypatch, [assistant_message(answer)])

    run = _run(question)

    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    assert len(model.calls) == 1
    assert run_answer_text(run) == answer
    assert _answer_content(run)["outcome"]["value"] == "needs_clarification"
    assert _grounding_events(run) == []


def test_answer_with_a_tool_is_not_sent_back(monkeypatch: MonkeyPatch, mcp: FakeProductMcp) -> None:
    model = _script(
        monkeypatch,
        [function_call(SEARCH, {"query": "経費精算 締め日"}, call_id="c1")],
        [assistant_message("締め日は毎月 20 日です。")],
    )

    run = _run("経費精算の締め日は？")

    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    assert len(model.calls) == 2
    assert len(mcp.calls_of("rag_search")) == 1
    assert run_answer_text(run) == "締め日は毎月 20 日です。"
    assert _grounding_events(run) == []
    assert tool_grounding.METADATA_KEY not in run.metadata


def test_rejected_data_tool_counts_as_an_attempt(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """承認を却下したデータのツールも、確かめようとした呼び出しとして数え、モデルへ差し戻さず終了する。"""
    model = _script(
        monkeypatch,
        [function_call(QUERY, {"question": "先月の受注金額", "profile_id": "p1"}, call_id="c1")],
        [assistant_message("受注金額を確かめられなかったため、お答えできません。")],
    )
    created = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="先月の受注金額は？", agent_id=AGENT_ID),
        created_by_user_uuid=USER_UUID,
    )
    anyio.run(builtin_runtime.execute_run, created.id)
    [approval] = runtime_repository.get_run(created.id).approvals
    runtime_repository.decide_approval(approval.id, ApprovalDecisionRequest(approved=False))
    anyio.run(builtin_runtime.resume_run, created.id)

    run = runtime_repository.get_run(created.id)
    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    assert len(model.calls) == 1
    assert mcp.calls_of("nl2sql_query") == []
    answer = run_answer_text(run) or ""
    assert "却下" in answer and "実行していません" in answer
    assert _grounding_events(run) == []


def test_agent_without_grounding_tools_is_not_a_target(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    del mcp
    model = _script(monkeypatch, [assistant_message("こんにちは。")])

    run = _run("こんにちは", agent_id=BARE_AGENT_ID)

    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    assert len(model.calls) == 1
    assert run_answer_text(run) == "こんにちは。"
    assert _grounding_events(run) == []


def test_rephrasing_an_answer_grounded_earlier_in_the_thread_is_not_sent_back(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """同じ会話の前の Run がツールで答えていれば、ツールを呼ばない言い換え・整形を拒答にしない。"""
    model = _script(
        monkeypatch,
        [function_call(SEARCH, {"query": "経費精算 締め日"}, call_id="call-1")],
        [assistant_message("締め日は毎月 20 日です。提出先は経理部です。")],
        [assistant_message("| 項目 | 内容 |\n| --- | --- |\n| 締め日 | 毎月 20 日 |")],
    )
    first = _run("経費精算の締め日と提出先は？")
    assert len(mcp.calls_of("rag_search")) == 1

    second = _run("前の回答を表にして", thread_id=first.thread_id)

    assert second.status == RunStatus.COMPLETED, second.events[-1].message
    assert len(model.calls) == 3
    assert run_answer_text(second) == "| 項目 | 内容 |\n| --- | --- |\n| 締め日 | 毎月 20 日 |"
    assert _grounding_events(second) == []
    assert tool_grounding.METADATA_KEY not in second.metadata


def test_thread_without_grounded_answers_is_still_refused(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """前の Run もツールで答えていない（拒答した）会話では、続きの Run も差し戻し・拒答にする。"""
    del mcp
    model = _script(
        monkeypatch,
        [assistant_message(UNGROUNDED)],
        [assistant_message(UNGROUNDED)],
        [assistant_message("大阪は第二の都市です。")],
        [assistant_message("大阪は第二の都市です。")],
    )
    first = _run()
    assert (run_answer_text(first) or "").startswith(tool_grounding.REFUSAL_HEAD)

    second = _run("大阪は？", thread_id=first.thread_id)

    assert len(model.calls) == 4
    assert (run_answer_text(second) or "").startswith(tool_grounding.REFUSAL_HEAD)
    assert [item["action"] for item in _grounding_events(second)] == ["retry", "refused"]


def test_only_runs_within_the_history_budget_count(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """会話の履歴の上限（PLATFORM_CHAT_HISTORY_MAX_TURNS）より前の Run の根拠は数えない。"""
    del mcp
    monkeypatch.setattr(get_settings(), "chat_history_max_turns", 1)
    model = _script(
        monkeypatch,
        [function_call(SEARCH, {"query": "経費精算 締め日"}, call_id="call-1")],
        [assistant_message("締め日は毎月 20 日です。")],
        [assistant_message("確認させてください。\nどの部署の経費ですか？")],
        [assistant_message("締め日は 20 日です。")],
        [assistant_message("締め日は 20 日です。")],
    )
    first = _run("経費精算の締め日は？")
    # 2 つ目は確認の質問だけ（ツールを呼んでいないが拒答の対象外）。
    second = _run("営業部の場合は？", thread_id=first.thread_id)
    assert _answer_content(second)["outcome"]["value"] == "needs_clarification"

    third = _run("もっと短く", thread_id=first.thread_id)

    # 上限 1 では直前の Run（ツールを呼んでいない）だけを見るので、差し戻し・拒答になる。
    assert len(model.calls) == 5
    assert (run_answer_text(third) or "").startswith(tool_grounding.REFUSAL_HEAD)

    # 上限が 0（履歴を渡さない）なら、前の Run を見ない。
    monkeypatch.setattr(get_settings(), "chat_history_max_turns", 0)
    assert not builtin_runtime._grounded_in_thread(second.id)  # noqa: SLF001
    monkeypatch.setattr(get_settings(), "chat_history_max_turns", 8)
    assert builtin_runtime._grounded_in_thread(third.id)  # noqa: SLF001


def test_guard_can_be_turned_off(monkeypatch: MonkeyPatch, mcp: FakeProductMcp) -> None:
    del mcp
    monkeypatch.setattr(get_settings(), "agent_tool_grounding_required", False)
    model = _script(monkeypatch, [assistant_message(UNGROUNDED)])

    run = _run()

    assert len(model.calls) == 1
    assert run_answer_text(run) == UNGROUNDED
    assert _grounding_events(run) == []


def test_retry_that_waits_for_approval_is_not_sent_back_twice(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """差し戻した後に承認で止まった Run は、再開しても 2 回目を差し戻さない。"""
    del mcp
    model = _script(
        monkeypatch,
        [assistant_message(UNGROUNDED)],
        [function_call(WRITE, {"query": "登録"}, call_id="call-w")],
        [assistant_message("登録しました。首都は東京です。")],
    )
    created = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="日本の首都を登録して", agent_id=AGENT_ID),
        created_by_user_uuid=USER_UUID,
    )

    anyio.run(builtin_runtime.execute_run, created.id)
    waiting = runtime_repository.get_run(created.id)
    assert waiting.status == RunStatus.WAITING_APPROVAL, waiting.events[-1].message
    [approval] = waiting.approvals
    runtime_repository.decide_approval(approval.id, ApprovalDecisionRequest(approved=True))
    anyio.run(builtin_runtime.resume_run, created.id)

    run = runtime_repository.get_run(created.id)
    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    assert len(model.calls) == 3
    assert (run_answer_text(run) or "").startswith(tool_grounding.REFUSAL_HEAD)
    assert [item["action"] for item in _grounding_events(run)] == ["retry", "refused"]


def _step(name: str, trace_id: str = "call-1") -> RunStep:
    return RunStep(
        run_id="run-1",
        status=StepStatus.COMPLETED,
        tool_call=ToolCall(name=name, arguments={}, trace_id=trace_id),
    )


def test_grounding_tools_and_attempts() -> None:
    # MCP 接続のツールが根拠・データのツール。Control Plane のツールと一覧・推薦は数えない。
    assert tool_grounding.is_grounding_tool(SEARCH)
    assert tool_grounding.is_grounding_tool("rag__rag_read_source")
    assert tool_grounding.is_grounding_tool(QUERY)
    assert tool_grounding.is_grounding_tool("erp__lookup_order")
    assert not tool_grounding.is_grounding_tool("skill_reference_read")
    assert not tool_grounding.is_grounding_tool("echo")
    assert not tool_grounding.is_grounding_tool("rag__rag_list_search_answer_profiles")
    assert not tool_grounding.is_grounding_tool("nl2sql__nl2sql_list_profiles")
    assert not tool_grounding.is_grounding_tool("nl2sql__nl2sql_recommend_profile")
    # 検索・回答プロファイルの推薦（#1850）も、資料の根拠を確かめないので数えない（#1881）。
    assert not tool_grounding.is_grounding_tool("rag__rag_recommend_search_answer_profile")
    assert not tool_grounding.grounding_attempted(
        [_step("rag__rag_recommend_search_answer_profile")]
    )
    # モデルの呼び出しは数え、Control Plane が自分で呼んだもの（確認の取り消し・照合）は数えない。
    assert tool_grounding.grounding_attempted([_step(SEARCH)])
    assert not tool_grounding.grounding_attempted([_step("skill_reference_read")])
    assert not tool_grounding.grounding_attempted(
        [_step("nl2sql__nl2sql_query", trace_id="clarification_cancel_run-1_0")]
    )
    assert not tool_grounding.grounding_attempted(
        [_step("rag__rag_lookup_guides", trace_id="guide_check_run-1")]
    )


def test_refusal_text_examples_follow_the_tools() -> None:
    rag_only = tool_grounding.refusal_text(tool_names=[SEARCH])
    assert "「経費精算規程の締め日は？」" in rag_only
    assert "受注金額" not in rag_only
    # 説明もスキルも無ければ、扱う内容の行は出さない。
    assert "扱う内容:" not in rag_only
    data_only = tool_grounding.refusal_text(skill_names=["受注の集計"], tool_names=[QUERY])
    assert "この業務 Agent が扱う内容: 受注の集計" in data_only
    assert "「先月の部門別の受注金額は？」" in data_only
    assert "経費精算規程" not in data_only
    long = tool_grounding.refusal_text(description="あ" * 300, tool_names=[SEARCH])
    assert "あ" * 199 + "…" in long


def test_retry_message_follows_whether_the_confirmation_tool_is_offered() -> None:
    confirmable = tool_grounding.retry_message(confirmable=True)
    plain = tool_grounding.retry_message(confirmable=False)
    # 確認のツールを渡した Run: 先に資料・業務データのツールで調べ、決まらなければ確認のツールで
    # 聞く。本文に確認の質問を書かせない（#1881）。
    assert "まず資料・業務データのツールで調べてください" in confirmable
    assert "ask_user_confirmation で確認してください" in confirmable
    assert "推薦の候補が割れたとき" in confirmable
    assert "本文に確認の質問や選ばせる表を書かないでください" in confirmable
    assert "ツールを呼ばずに" not in confirmable
    # 渡していない Run: 今どおり、確認の質問だけを返してよい。
    assert "対象を確かめる確認の質問だけを返してかまいません" in plain
    assert "ask_user_confirmation" not in plain
    # 利用者に確認しない Run（#1903）: 確認のツールを渡していても、確認させずに前提を示させる。
    assume = tool_grounding.retry_message(confirmable=True, assume=True)
    assert assume == tool_grounding.RETRY_MESSAGE_ASSUME
    assert "確認の質問はせず" in assume
    assert "何を前提にしたかを回答の中で示して答えてください" in assume
    assert "ask_user_confirmation" not in assume
    assert "返してかまいません" not in assume
    items: list[Any] = [{"role": "user", "content": "あの件は？"}]
    retried = tool_grounding.retry_input(items, "答え", confirmable=True)
    assert retried[-1]["content"].startswith(confirmable)


def test_clarified_in_text_needs_the_confirmation_tool_and_no_grounding() -> None:
    question = "どの件のことですか？（経費精算／受注）"
    offered = [SEARCH, "ask_user_confirmation"]
    assert tool_grounding.clarified_in_text(question, steps=[], tool_names=offered)
    # 確認のツールを渡していない・根拠のツールが無い・根拠のツールを呼んだ・確認の質問でない。
    assert not tool_grounding.clarified_in_text(question, steps=[], tool_names=[SEARCH])
    assert not tool_grounding.clarified_in_text(
        question, steps=[], tool_names=["ask_user_confirmation"]
    )
    assert not tool_grounding.clarified_in_text(question, steps=[_step(SEARCH)], tool_names=offered)
    assert not tool_grounding.clarified_in_text(UNGROUNDED, steps=[], tool_names=offered)


def test_text_clarification_is_a_target_unless_it_replies_to_a_greeting() -> None:
    """確認のツールを渡した Run・利用者に確認しない Run の本文の聞き返し（#1881・#1903）。"""
    question = "どの件のことですか？（経費精算／受注）"
    greeting = "こんにちは。ご用件をお知らせください。"
    tools = [SEARCH, "ask_user_confirmation"]
    assert not tool_grounding.needs_grounding(question, steps=[], tool_names=tools)
    assert tool_grounding.needs_grounding(
        question, steps=[], tool_names=tools, clarification_exempt=False
    )
    # あいさつへの返事（#1879）は確認の質問ではない: 回答があいさつを含むか、質問があいさつだけ。
    assert not tool_grounding.needs_grounding(
        greeting, steps=[], tool_names=tools, clarification_exempt=False
    )
    assert not tool_grounding.needs_grounding(
        "ご用件は何ですか？",
        steps=[],
        tool_names=tools,
        clarification_exempt=False,
        question="こんにちは",
    )
    assert tool_grounding.is_greeting_reply("", "こんにちは！")
    assert not tool_grounding.is_greeting_reply(question, "あの件どうなった？")
    # お礼・質問でない入力に用件を聞く返事（#1929）も差し戻さない（対象の聞き返しは今どおり）。
    assert tool_grounding.is_greeting_reply("", "ありがとう")
    assert tool_grounding.is_greeting_reply("どういたしまして。ほかに知りたいことはありますか？")
    assert tool_grounding.is_greeting_reply(
        "社内の規程や手順について、知りたいことをお知らせください。", "テスト"
    )
    assert not tool_grounding.is_greeting_reply("どの申請についてのご質問ですか？", "テスト")
    assert tool_grounding.needs_grounding(
        "よろしくお願いします。どの申請ですか？",
        steps=[],
        tool_names=tools,
        clarification_exempt=False,
        question="申請の期限は？",
    )


def test_retry_input_drops_the_assistant_message_and_its_reasoning() -> None:
    items: list[Any] = [
        {"role": "user", "content": "こんにちは"},
        {"type": "function_call", "call_id": "c1", "name": "skill_ref", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "{}"},
        {"id": "rs_1", "type": "reasoning", "summary": []},
        {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "こんにちは！", "annotations": []}],
        },
    ]

    retried = tool_grounding.retry_input(items, "こんにちは！" + "あ" * 5000)

    # OCI の gpt-oss は入力の output_text を受け付けないので、assistant のメッセージと直前の
    # reasoning を外し、ツールの呼び出しは残す。
    assert retried[:3] == items[:3]
    assert len(retried) == 4
    content = retried[-1]["content"]
    assert content.startswith(tool_grounding.RETRY_MESSAGE)
    assert "直前の回答（利用者には出していません）:\nこんにちは！" in content
    assert content.endswith("…")


# 呼び先（RAG）の回答の pipeline で AI の API が失敗したときのツールのエラー（#2475）。
_API_FAILURE = McpToolError(
    "RAG_ANSWER_AI_API_FAILED",
    "回答の作成中に AI の API の呼び出しに失敗したため、検索を終えられませんでした。",
    status=502,
    details={"kind": "unknown", "error_type": "APIError"},
)
_ABSENCE = "資料には該当する記載が見つかりませんでした。"


def test_failed_search_is_a_run_failure_not_insufficient_evidence(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """検索が実行に失敗したまま答えた Run は、根拠が無いという回答にせず失敗にする（#2475）。"""
    mcp.outputs["rag_search"] = _API_FAILURE
    _script(
        monkeypatch,
        ModelStep(output=[function_call(SEARCH, {"query": "締め日"}, call_id="call-1")]),
        ModelStep(output=[assistant_message(_ABSENCE)]),
    )

    run = _run("経費精算の締め日は？")

    assert run.status == RunStatus.FAILED
    failure = run.events[-1].payload
    assert failure["error_code"] == tool_grounding.GROUNDING_TOOL_FAILED_CODE
    message = run.events[-1].message
    assert "rag_search" in message
    assert "検索を終えられませんでした" in message
    assert "根拠が無いという意味ではありません" in message
    assert run_answer_text(run) != _ABSENCE


def test_search_that_found_no_evidence_is_still_insufficient_evidence(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """検索が成功して根拠が無かった回答は、今どおり根拠の不足として出す（#2475）。"""
    mcp.outputs["rag_search"] = {
        **DEFAULT_OUTPUTS["rag_search"],
        "outcome": "insufficient_evidence",
    }
    _script(
        monkeypatch,
        ModelStep(output=[function_call(SEARCH, {"query": "締め日"}, call_id="call-1")]),
        ModelStep(output=[assistant_message(_ABSENCE)]),
        # 業務データを引かずに資料に無いとした回答は 1 回差し戻す（#1979）。差し戻しの後も同じ回答。
        ModelStep(output=[assistant_message(_ABSENCE)]),
    )

    run = _run("経費精算の締め日は？")

    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    assert run_answer_text(run) == _ABSENCE
    assert _answer_content(run)["outcome"]["value"] == "insufficient_evidence"


def test_search_retried_after_a_failure_is_answered(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """失敗の後に検索をやり直して結果を得た Run は、今どおり回答する（#2475）。"""
    outputs = [_API_FAILURE, deepcopy(DEFAULT_OUTPUTS["rag_search"])]

    def search(_argument: Any) -> Any:
        output = outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output

    mcp.outputs["rag_search"] = search
    _script(
        monkeypatch,
        ModelStep(output=[function_call(SEARCH, {"query": "締め日"}, call_id="call-1")]),
        ModelStep(output=[function_call(SEARCH, {"query": "締め日"}, call_id="call-2")]),
        ModelStep(output=[assistant_message("締め日は毎月 20 日です。")]),
    )

    run = _run("経費精算の締め日は？")

    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    assert run_answer_text(run) == "締め日は毎月 20 日です。"


def _failed_step(error_code: str, **details: Any) -> RunStep:
    return RunStep(
        run_id="run-1",
        status=StepStatus.FAILED,
        tool_call=ToolCall(name=SEARCH, arguments={}, trace_id="call-1"),
        tool_result=ToolResult(
            name=SEARCH, success=False, error="失敗", error_code=error_code, error_details=details
        ),
    )


def test_grounding_failure_counts_only_execution_failures() -> None:
    """呼び先で実行できなかった失敗だけを数える（呼び出し側の誤り・呼ばなかったものは数えない）。"""
    assert tool_grounding.grounding_failure([_failed_step("mcp.tool_error", status=502)])
    assert tool_grounding.grounding_failure([_failed_step("mcp.tool_error")])
    assert tool_grounding.grounding_failure([_failed_step("mcp.rpc_error")])
    assert tool_grounding.grounding_failure([_failed_step("tool.unhandled_error")])
    # 引数・対象の誤り（4xx）は、呼び出しを直せば答えられる。
    assert tool_grounding.grounding_failure([_failed_step("mcp.tool_error", status=404)]) is None
    # 評価の dry-run・予算の超過は、呼び先の失敗ではない。
    assert tool_grounding.grounding_failure([_failed_step("evaluation.dry_run")]) is None
    assert tool_grounding.grounding_failure([_failed_step("budget_exceeded")]) is None
    # 別の根拠のツールで結果を得ていれば、失敗にしない。
    succeeded = _step(QUERY)
    succeeded.tool_result = ToolResult(name=QUERY, success=True, output={})
    assert (
        tool_grounding.grounding_failure([_failed_step("mcp.tool_error", status=502), succeeded])
        is None
    )
