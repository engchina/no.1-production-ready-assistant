"""回答の最終の検証（#1246・#1277）の決定論テスト。

モデルは SDK の `ScriptedModel`、RAG は契約どおりの fake の MCP（`mcp_support`）にする。
Control Plane が（モデルではなく）`rag_validate_answer` を、その Run の根拠でツールの境界を
通して呼び、結果を成果物に残し、確かめられない点・検証できなかったことを回答の末尾に示すことを
確かめる。
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import anyio
import pytest
from agents import FunctionTool
from agents.testing import ScriptedModel, assistant_message, function_call
from mcp_support import DEFAULT_OUTPUTS, FakeProductMcp, McpToolError, fake_product_mcp
from pr_backend_core.mcp import McpServer, McpTool
from pr_system_settings.answer_notice import DEFAULT_ANSWER_NOTICE_TEXT
from pydantic import BaseModel
from pytest import MonkeyPatch
from test_clarification_preface import CLARIFICATIONS

from app.features.agent import builtin_runtime
from app.features.agent.answer_passages import (
    is_absence_only,
    is_heading,
    is_operation_step,
    label_operation_end,
    list_heading_lines,
    list_item_label,
    non_claim_passages,
    passage_kind,
    passage_spans,
)
from app.features.agent.answer_validation import (
    ALL_REMOVED,
    ANSWER_WARNING_REASONS,
    MAX_CARRIED_EVIDENCE,
    MAX_VALIDATION_EVIDENCE,
    NO_EVIDENCE_NOTICE,
    PASSAGE_REASONS,
    REMOVED_HEADING,
    REMOVED_INTRO,
    REMOVED_REASONS,
    REVIEW_HEADING,
    REVIEW_INTRO,
    UNVERIFIED_NOTICE,
    carried_evidence,
    carried_evidence_groups,
    connection_check_inputs,
    dependent_steps,
    merge_results,
    publish_answer,
    run_evidence_groups,
    withhold_paragraphs,
)
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.config import runtime_config_store
from app.features.agent.runtime import (
    AgentProfile,
    RunCreateRequest,
    RunState,
    RunStatus,
    RunStep,
    StepStatus,
    run_answer_text,
    run_support_task,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.tools import ToolCall, ToolPolicy, ToolResult
from app.settings import Settings, get_settings

SKILL_ID = "test1246-skill"
AGENT_ID = "test1246-agent"
USER_UUID = "11111111-2222-3333-4444-555555555555"
VALIDATOR = "rag__rag_validate_answer"
ANSWER = "契約の更新は 30 日前までに申し出ます。"


@pytest.fixture
def mcp(monkeypatch: MonkeyPatch) -> Iterator[FakeProductMcp]:
    yield from _mcp(monkeypatch)


def _mcp(monkeypatch: MonkeyPatch) -> Iterator[FakeProductMcp]:
    """RAG の rag_search・rag_retrieve_evidence だけを使う業務 Agent（検証のツールは渡さない）。"""
    fake = fake_product_mcp(monkeypatch)
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", True)
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name="検証の Skill",
            instructions="RAG で調べて答える。",
            mcp_requirements=[
                SkillMcpRequirement(
                    server_id="rag", tool_names=["rag_search", "rag_retrieve_evidence"]
                )
            ],
        )
    )
    runtime_repository.create_agent(
        AgentProfile(id=AGENT_ID, name="検証の業務 Agent", skill_ids=[SKILL_ID])
    )
    try:
        yield fake
    finally:
        repository: Any = runtime_repository
        with repository._lock:  # noqa: SLF001 - テストの後始末
            for run_id in [
                run_id for run_id, run in repository._runs.items() if run.agent_id == AGENT_ID
            ]:
                repository._runs.pop(run_id)
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
            endpoint="https://oci.example",
            project_ocid="",
            api_key="k",
        ),
    )
    monkeypatch.setattr(builtin_runtime, "model_factory", lambda _target: model)
    return model


def _searched_run(monkeypatch: MonkeyPatch, answer: str = ANSWER) -> tuple[RunState, ScriptedModel]:
    model = _script(
        monkeypatch,
        [function_call("rag__rag_search", {"query": "契約の更新"}, call_id="call-1")],
        [assistant_message(answer)],
    )
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="契約の更新の期限は？", agent_id=AGENT_ID),
        created_by_user_uuid=USER_UUID,
    )
    anyio.run(builtin_runtime.execute_run, run.id)
    return runtime_repository.get_run(run.id), model


def _validation(run: RunState) -> dict[str, Any]:
    [artifact] = [item for item in run.artifacts if item.kind == "answer_validation"]
    assert artifact.name == "資料との照合"
    return artifact.content


def test_valid_answer_is_kept_and_the_result_is_saved(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    run, model = _searched_run(monkeypatch)

    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    assert run_answer_text(run) == ANSWER
    # モデルには検証のツールを渡さない（Control Plane が呼ぶ）。
    tools = {tool.name for tool in model.calls[0].tools if isinstance(tool, FunctionTool)}
    assert VALIDATOR not in tools
    # その Run の質問・回答・RAG の根拠で、Run の利用者のサービストークンで呼ぶ。
    [call] = mcp.calls_of("rag_validate_answer")
    assert call["arguments"] == {
        "query": "契約の更新の期限は？",
        "answer": ANSWER,
        "evidence": [{"document_id": "doc-1", "chunk_id": "chunk-1"}],
        # rag_search の要求ごとの充足（決定的な検査。#1276）。
        "requests": [{"id": "Q1", "text": "契約条項", "status": "addressed"}],
        # rag_search の回答の監査の判定を再利用する（#1527）。
        "answer_trace_ids": ["rag-trace-1"],
    }
    assert call["claims"]["sub"] == USER_UUID
    assert call["claims"]["run_id"] == run.id
    # ツールの境界（step・監査）を通す。
    step = run.steps[-1]
    assert step.tool_call is not None and step.tool_call.name == VALIDATOR
    assert step.status == "completed"
    assert step.tool_result is not None
    assert step.tool_result.audit_metadata["tool_name"] == VALIDATOR
    content = _validation(run)
    assert content["status"] == "completed"
    assert content["valid"] is True
    assert content["connection"] == "rag"
    assert content["step_id"] == step.id
    assert content["result"]["counts"]["supported"] == 1
    # Control Plane の検証はモデルの予算に数えない。
    state = run_support_task(run)
    assert state is not None
    assert state["budget"]["run"]["tool_calls"] == 1


def test_validation_is_on_by_default() -> None:
    # 既定で on（#1277）。環境変数 AGENT_FINAL_VALIDATION_ENABLED=false で切れる。
    assert Settings.model_fields["agent_final_validation_enabled"].default is True


STEP_OK = "1. 管理画面で「更新」を押します。"
STEP_FEE = "2. 手数料として 5,000 円を支払い" + "、担当者の確認を受け" * 6 + "ます。"
STEP_WRONG = "3. 解約は 60 日前までに申し出ます。"


def test_failed_verdict_keeps_unsupported_steps_and_removes_contradicted_ones(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    mcp.outputs["rag_validate_answer"] = {
        "valid": False,
        "status": "completed",
        "counts": {"supported": 2, "unsupported": 1, "contradicted": 1},
        "claims": [
            {"answer_quote": ANSWER, "status": "supported", "chunk_id": "chunk-1", "reason": "ok"},
            {"answer_quote": STEP_OK, "status": "supported", "chunk_id": "chunk-1", "reason": "ok"},
            {"answer_quote": STEP_FEE, "status": "unsupported", "reason": "根拠に金額が無い。"},
            {
                "answer_quote": STEP_WRONG,
                "status": "contradicted",
                "chunk_id": "chunk-1",
                "reason": "根拠は 30 日前。",
            },
        ],
        "missing_evidence": [],
        "stale_evidence": [{"document_id": "doc-1", "chunk_id": "chunk-1"}],
        "evidence_truncated": False,
    }

    run, _ = _searched_run(monkeypatch, answer=f"{ANSWER}\n\n{STEP_OK}\n{STEP_FEE}\n{STEP_WRONG}")

    answer = run_answer_text(run)
    assert answer is not None
    # 根拠と矛盾した手順だけを外し、根拠で確かめられない手順は本文に残して印を付ける（#2738）。
    body, rest = answer.split(f"\n\n**{REMOVED_HEADING}**\n\n", 1)
    removed, review = rest.split(f"\n\n**{REVIEW_HEADING}**\n\n", 1)
    assert body == f"{ANSWER}\n\n{STEP_OK}\n{STEP_FEE}"
    assert removed.splitlines() == [REMOVED_INTRO, f"- 根拠と矛盾: 「{STEP_WRONG}」"]
    lines = review.splitlines()
    assert lines[0] == REVIEW_INTRO
    assert lines[1].startswith("- 根拠で確かめられない: 「2. 手数料として 5,000 円を支払い")
    quote = lines[1].split("「", 1)[1].split("」", 1)[0]
    assert len(quote) == 80 and quote.endswith("…")
    # 検証のモデルが書いた理由は本文に出さない（#1833。成果物の印の理由に残す）。
    assert lines[1].endswith("…」")
    assert lines[2].startswith("- 根拠の 1 件は文書の古い版です。")
    assert len(lines) == 3
    content = _validation(run)
    assert (content["status"], content["valid"]) == ("completed", False)
    assert content["withheld"] == {
        "removed": 1,
        "unverified": 1,
        "warnings": 0,
        "all": False,
        "listed": 3,
    }
    assert content["passage_marks"] == [
        {
            "text": STEP_FEE,
            "state": "unverified",
            "reasons": [
                {
                    "code": "unsupported",
                    "label": "根拠で確かめられない",
                    "detail": "根拠に金額が無い。",
                }
            ],
        }
    ]
    assert content["removed_passages"] == [
        {
            "text": STEP_WRONG,
            "reasons": [
                {"code": "contradicted", "label": "根拠と矛盾", "detail": "根拠は 30 日前。"}
            ],
        }
    ]
    assert content["notice"] == {
        "items": 3,
        "heading": REMOVED_HEADING,
        "headings": [REMOVED_HEADING, REVIEW_HEADING],
    }


def test_verdict_without_readable_evidence_keeps_the_answer_with_a_warning(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    mcp.outputs["rag_validate_answer"] = {
        "valid": False,
        "status": "no_evidence",
        "counts": {},
        "claims": [],
        "missing_evidence": [{"document_id": "doc-1", "chunk_id": "chunk-1"}],
        "stale_evidence": [],
        "evidence_truncated": False,
    }

    run, _ = _searched_run(monkeypatch)

    # 本文は置き換えず、すべての段落を未確認にして回答全体の警告を出す（#2738）。
    assert run_answer_text(run) == (
        f"{ANSWER}\n\n**{REVIEW_HEADING}**\n\n{REVIEW_INTRO}\n"
        "- 根拠を読めず、すべての段落が未確認\n"
        "- 根拠の 1 件は見つかりません（削除された・参照できない）。"
    )
    content = _validation(run)
    assert [item["code"] for item in content["answer_warnings"]] == ["no_evidence"]
    assert content["passage_marks"] == [
        {
            "text": ANSWER,
            "state": "unverified",
            "reasons": [{"code": "unsupported", "label": "根拠で確かめられない", "detail": ""}],
        }
    ]
    assert content["withheld"] == {
        "removed": 0,
        "unverified": 1,
        "warnings": 0,
        "all": False,
        "listed": 2,
    }


def test_invalid_answer_shows_deterministic_findings(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """RAG の決定的な検査の error（#1276）は回答全体の警告にして「確認が必要な点」に出す（warning は
    除く。#2738）。
    """
    output = deepcopy(DEFAULT_OUTPUTS["rag_validate_answer"])
    output["valid"] = False
    output["checks"] = ["requests", "guide", "guide_steps", "impact"]
    output["findings"] = [
        {
            "check": "requests",
            "code": "request_missing",
            "severity": "error",
            "message": "要求「更新の手数料」に答えていません。不足も示していません。",
            "request_id": "Q2",
        },
        {
            "check": "guide_steps",
            "code": "step_dependency_missing",
            "severity": "warning",
            "message": "手順「申し出る」の前の手順「契約を開く」が回答にありません。",
            "step_id": "s2",
            "related_step_id": "s1",
        },
    ]
    mcp.outputs["rag_validate_answer"] = output

    run, _ = _searched_run(monkeypatch)

    answer = run_answer_text(run)
    assert answer is not None
    body, section = answer.split(f"\n\n**{REVIEW_HEADING}**\n\n", 1)
    assert body == ANSWER
    message = "要求「更新の手数料」に答えていません。不足も示していません。"
    assert section.splitlines() == [REVIEW_INTRO, f"- 質問の要求の一部に答えていない: {message}"]
    content = _validation(run)
    assert content["result"]["findings"][0]["code"] == "request_missing"
    assert content["answer_warnings"] == [
        {"code": "requests", "label": "質問の要求の一部に答えていない", "detail": message}
    ]


def test_validator_failure_keeps_the_answer_with_a_notice(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    mcp.outputs["rag_validate_answer"] = McpToolError("RAG_UNAVAILABLE", "一時的に利用できません。")

    run, _ = _searched_run(monkeypatch)

    assert run.status == RunStatus.COMPLETED
    # 基盤の障害では内容を落とさない（確かめていないことを示す）。
    assert run_answer_text(run) == f"{ANSWER}\n\n{UNVERIFIED_NOTICE}"
    content = _validation(run)
    assert content["status"] == "unvalidated"
    assert content["reason"] == "mcp.tool_error"
    assert content["message"] == "一時的に利用できません。"
    assert run.steps[-1].status == "failed"


def test_validator_timeout_keeps_the_answer_with_a_notice(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    def search(_argument: Any) -> dict[str, Any]:
        # 検索の後の呼び出し（検証）だけ、呼び先に届いた後の読み取りの timeout にする（再試行も）。
        mcp.tool_call_statuses.extend(["timeout"] * 5)
        return deepcopy(DEFAULT_OUTPUTS["rag_search"])

    mcp.outputs["rag_search"] = search

    run, _ = _searched_run(monkeypatch)

    assert run_answer_text(run) == f"{ANSWER}\n\n{UNVERIFIED_NOTICE}"
    assert _validation(run)["status"] == "unvalidated"


def test_unexpected_validation_error_keeps_the_answer_and_records_it(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    def broken(_steps: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(builtin_runtime, "run_evidence_groups", broken)

    run, _ = _searched_run(monkeypatch)

    assert run.status == RunStatus.COMPLETED
    assert run_answer_text(run) == f"{ANSWER}\n\n{UNVERIFIED_NOTICE}"
    content = _validation(run)
    assert content["status"] == "unvalidated"
    assert content["reason"] == "validation_error"


def test_unusable_validator_result_keeps_the_answer_with_a_notice(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    mcp.outputs["rag_validate_answer"] = {
        "valid": False,
        "status": "input_too_large",
        "counts": {},
        "claims": [],
        "missing_evidence": [],
        "stale_evidence": [],
        "evidence_truncated": False,
    }

    run, _ = _searched_run(monkeypatch)

    assert run_answer_text(run) == f"{ANSWER}\n\n{UNVERIFIED_NOTICE}"
    content = _validation(run)
    assert (content["status"], content["reason"]) == ("unvalidated", "unusable_result")


def test_policy_denied_validator_is_not_called(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    monkeypatch.setattr(builtin_runtime, "_active_policy", lambda: ToolPolicy(deny={VALIDATOR}))

    run, _ = _searched_run(monkeypatch)

    assert mcp.calls_of("rag_validate_answer") == []
    assert run_answer_text(run) == f"{ANSWER}\n\n{UNVERIFIED_NOTICE}"
    assert _validation(run)["reason"] == "policy_denied"


def test_disabled_validation_is_not_called(monkeypatch: MonkeyPatch, mcp: FakeProductMcp) -> None:
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", False)

    run, _ = _searched_run(monkeypatch)

    assert mcp.calls_of("rag_validate_answer") == []
    assert run_answer_text(run) == ANSWER
    assert not [item for item in run.artifacts if item.kind == "answer_validation"]


def test_rag_agent_answer_without_evidence_is_unvalidated(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    # 根拠・データのツールの拒答（#1835。test_tool_grounding.py）の外の振る舞いを確かめる。
    monkeypatch.setattr(get_settings(), "agent_tool_grounding_required", False)
    _script(monkeypatch, [assistant_message("資料を調べずに答えました。")])
    created = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="こんにちは", agent_id=AGENT_ID), created_by_user_uuid=USER_UUID
    )

    anyio.run(builtin_runtime.execute_run, created.id)

    run = runtime_repository.get_run(created.id)
    assert mcp.calls_of("rag_validate_answer") == []
    assert run_answer_text(run) == (
        "資料を調べずに答えました。\n\n"
        "この回答は資料の根拠を使っておらず、資料と照らし合わせて確かめていません。"
    )
    content = _validation(run)
    assert (content["status"], content["reason"]) == ("unvalidated", "no_rag_evidence")


def test_agent_without_rag_tools_is_skipped(monkeypatch: MonkeyPatch, mcp: FakeProductMcp) -> None:
    # 根拠・データのツールの拒答（#1835。test_tool_grounding.py）の外の振る舞いを確かめる。
    monkeypatch.setattr(get_settings(), "agent_tool_grounding_required", False)
    del mcp
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=f"{SKILL_ID}-sql",
            name="SQL の Skill",
            instructions="NL2SQL で調べる。",
            mcp_requirements=[SkillMcpRequirement(server_id="nl2sql", tool_names=["nl2sql_query"])],
        )
    )
    runtime_repository.create_agent(
        AgentProfile(id=f"{AGENT_ID}-sql", name="SQL の業務 Agent", skill_ids=[f"{SKILL_ID}-sql"])
    )
    try:
        _script(monkeypatch, [assistant_message("売上は 100 件です。")])
        created = runtime_repository.create_builtin_run(
            RunCreateRequest(goal="売上は？", agent_id=f"{AGENT_ID}-sql"),
            created_by_user_uuid=USER_UUID,
        )

        anyio.run(builtin_runtime.execute_run, created.id)

        run = runtime_repository.get_run(created.id)
        # RAG のツールを持たない Agent は今までどおり（注記を足さない）。
        assert run_answer_text(run) == "売上は 100 件です。"
        content = _validation(run)
        assert (content["status"], content["reason"]) == ("skipped", "no_rag_evidence")
    finally:
        repository: Any = runtime_repository
        with repository._lock:  # noqa: SLF001 - テストの後始末
            for run_id in [
                run_id
                for run_id, run in repository._runs.items()
                if run.agent_id == f"{AGENT_ID}-sql"
            ]:
                repository._runs.pop(run_id)
        runtime_repository.delete_agent(f"{AGENT_ID}-sql")
        skill_registry.remove(f"{SKILL_ID}-sql")


def test_evidence_of_all_rag_calls_is_passed_newest_first(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    retrieved = deepcopy(DEFAULT_OUTPUTS["rag_retrieve_evidence"])
    first = retrieved["evidence"][0]
    retrieved["evidence"] = [
        {**first, "evidence_id": "chunk-2", "chunk_id": "chunk-2"},
        first,
    ]
    mcp.outputs["rag_retrieve_evidence"] = retrieved
    _script(
        monkeypatch,
        [function_call("rag__rag_retrieve_evidence", {"query": "契約"}, call_id="call-1")],
        [function_call("rag__rag_search", {"query": "契約の更新"}, call_id="call-2")],
        [assistant_message(ANSWER)],
    )
    created = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="契約の更新の期限は？", agent_id=AGENT_ID),
        created_by_user_uuid=USER_UUID,
    )

    anyio.run(builtin_runtime.execute_run, created.id)

    [call] = mcp.calls_of("rag_validate_answer")
    # 新しい呼び出し（rag_search）の根拠から、重複を除いて渡す。
    assert call["arguments"]["evidence"] == [
        {"document_id": "doc-1", "chunk_id": "chunk-1"},
        {"document_id": "doc-1", "chunk_id": "chunk-2"},
    ]


def test_chunks_read_in_a_multi_hop_run_are_validation_evidence(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    # 多段の質問（#1345）: 段の事実を rag_read_document で読んで答えたら、読んだ chunk も検証する。
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name="検証の Skill",
            instructions="RAG で段ごとに調べて答える。",
            mcp_requirements=[
                SkillMcpRequirement(
                    server_id="rag",
                    tool_names=["rag_search", "rag_retrieve_evidence", "rag_read_document"],
                )
            ],
        )
    )
    _script(
        monkeypatch,
        [function_call("rag__rag_retrieve_evidence", {"query": "契約"}, call_id="call-1")],
        [
            function_call(
                "rag__rag_read_document",
                {"document_id": "doc-1", "cursor": "cursor-section-5"},
                call_id="call-2",
            )
        ],
        [assistant_message(ANSWER)],
    )
    created = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="契約の更新の期限は？", agent_id=AGENT_ID),
        created_by_user_uuid=USER_UUID,
    )

    anyio.run(builtin_runtime.execute_run, created.id)

    [call] = mcp.calls_of("rag_validate_answer")
    # 読んだ本文の chunk も根拠と合わせて渡す。回答に当たらない読んだ chunk は、検索の根拠の
    # 後ろに置く（#1451。文書をまとめて読んだ chunk が上限を埋めない）。
    assert call["arguments"]["evidence"] == [
        {"document_id": "doc-1", "chunk_id": "chunk-1"},
        {"document_id": "doc-1", "chunk_id": "chunk-3"},
        {"document_id": "doc-1", "chunk_id": "chunk-4"},
    ]


def test_evidence_groups_take_read_source_and_read_document_chunks() -> None:
    steps = [
        _step(
            "rag__rag_retrieve_evidence",
            {"query": "台帳"},
            {"evidence": [{"document_id": "doc-1", "chunk_id": "c-1"}]},
        ),
        _step(
            "rag__rag_read_source",
            {"document_id": "doc-2", "chunk_id": "c-9"},
            {"document_id": "doc-2", "chunk_id": "c-9", "text": "第 4 章を参照"},
        ),
        _step(
            "other__rag_read_document",
            {"document_id": "doc-3"},
            {"document_id": "doc-3", "chunks": [{"chunk_id": "c-a"}, {"chunk_id": "c-b"}]},
        ),
        # 形の違う出力・chunk の無い読み取りは根拠にしない。
        _step("rag__rag_read_document", {"document_id": "doc-4"}, {"chunks": "x"}),
        _step("rag__rag_outline", {"document_id": "doc-1"}, {"document_id": "doc-1"}),
    ]
    failed = _step("rag__rag_read_source", {}, {"document_id": "doc-5", "chunk_id": "c-5"})
    failed.tool_result = ToolResult(name="rag__rag_read_source", success=False, error="x")
    steps.append(failed)

    assert run_evidence_groups(steps) == [
        (
            "other__rag_read_document",
            [
                {"document_id": "doc-3", "chunk_id": "c-a"},
                {"document_id": "doc-3", "chunk_id": "c-b"},
            ],
        ),
        (
            "rag__rag_read_source",
            [
                {"document_id": "doc-2", "chunk_id": "c-9"},
                {"document_id": "doc-1", "chunk_id": "c-1"},
            ],
        ),
    ]


def test_evidence_groups_take_read_section_chunks() -> None:
    """rag_read_section（#1438）で読んだ章節の chunk も、最終の検証の根拠に入れる。"""
    steps = [
        _step(
            "rag__rag_read_section",
            {"document_id": "doc-1", "chunk_id": "c-1"},
            {"document_id": "doc-1", "chunks": [{"chunk_id": "c-1"}, {"chunk_id": "c-2"}]},
        )
    ]

    assert run_evidence_groups(steps) == [
        (
            "rag__rag_read_section",
            [
                {"document_id": "doc-1", "chunk_id": "c-1"},
                {"document_id": "doc-1", "chunk_id": "c-2"},
            ],
        )
    ]


def test_evidence_groups_keep_chunks_the_answer_cited_within_the_limit() -> None:
    """多段の質問の評価の Run の形（#1364）: 3 回の検索で 36 件の根拠を集め、回答が 1 回目の
    検索の根拠（台帳の行）を引用した。新しい順の 30 件で切ると台帳の行が検証に入らず、正しい結論が
    「確かめられない」になっていた。回答が引用した根拠は上限の中に入れる。"""

    def evidence(call: int) -> dict[str, Any]:
        return {
            "evidence": [
                {"document_id": f"doc-{call}", "chunk_id": f"doc-{call}:set:{index}"}
                for index in range(1, 13)
            ]
        }

    steps = [
        _step("rag__rag_retrieve_evidence", {"query": f"段 {call}"}, evidence(call))
        for call in (1, 2, 3)
    ]
    # `doc-1:set:1` は `doc-1:set:12` の先頭と同じだが、回答が書いたのは :12 だけ。
    answer = (
        "台帳の行で担当部署は経理部です【evidence_id:doc-1:set:12】。"
        "経理部の承認者は管理本部長です【evidence_id: doc-2:set:3】。"
    )

    [(tool_name, refs)] = run_evidence_groups(steps, answer)

    assert tool_name == "rag__rag_retrieve_evidence"
    assert len(refs) == 30
    # 回答が引用した根拠を先に（新しい呼び出しから順）、残りは新しい呼び出しから順。
    assert refs[:2] == [
        {"document_id": "doc-2", "chunk_id": "doc-2:set:3"},
        {"document_id": "doc-1", "chunk_id": "doc-1:set:12"},
    ]
    # 残りは新しい呼び出しから順（1 回目の検索は前から 5 件まで）。
    assert refs[2] == {"document_id": "doc-3", "chunk_id": "doc-3:set:1"}
    assert refs[-1] == {"document_id": "doc-1", "chunk_id": "doc-1:set:5"}
    # 回答を渡さなければ今までどおり新しい順の 30 件（1 回目の検索の後ろ 6 件は入らない）。
    [(_name, newest)] = run_evidence_groups(steps)
    assert {"document_id": "doc-1", "chunk_id": "doc-1:set:12"} not in newest


def test_evidence_groups_keep_chunks_cited_with_abbreviated_ids() -> None:
    """回答が根拠の id を省いて書いた（文書の id を落とした・途中を「…」で省いた・区切りを
    横線で書いた）引用も、1 つの根拠に決まれば上限の中に入れる。番号だけ・どの根拠か決まらない
    書き方は数えない。"""

    def ids(call: int) -> tuple[str, str]:
        return f"{call}f1e2d3c4b5a69788796a5b4c3d2e1f0", f"{call}1b2c3d4e5f60718293a4b5c6d7e8f90"

    def evidence(call: int) -> dict[str, Any]:
        doc, chunk_set = ids(call)
        return {
            "evidence": [
                {"document_id": doc, "chunk_id": f"{doc}:{chunk_set}:{index}"}
                for index in range(1, 13)
            ]
        }

    steps = [
        _step("rag__rag_retrieve_evidence", {"query": f"段 {call}"}, evidence(call))
        for call in (1, 2, 3)
    ]
    doc, chunk_set = ids(1)
    answer = (
        f"台帳の行で担当部署は経理部です【evidence_id: {chunk_set}:12】。"
        f"承認者は管理本部長です【証拠 ID {doc[:8]}‑…‑11】。"
        f"期限は 30 日です【{chunk_set[:10]}…:10】。"
        "番号だけの引用は数えません【:9】。"
    )

    [(_name, refs)] = run_evidence_groups(steps, answer)

    assert {ref["chunk_id"] for ref in refs[:3]} == {
        f"{doc}:{chunk_set}:12",
        f"{doc}:{chunk_set}:11",
        f"{doc}:{chunk_set}:10",
    }
    assert {"document_id": doc, "chunk_id": f"{doc}:{chunk_set}:9"} not in refs


def _previous_validation(
    chunk_status: list[tuple[str, str]], *, status: str = "completed", connection: str = "rag"
) -> dict[str, Any]:
    """前のターンの最終の検証の成果物の内容（接続 1 つ。chunk ごとの判定）。"""
    return {
        "status": status,
        "connections": [
            {
                "status": "completed",
                "connection": connection,
                "tool_name": f"{connection}__rag_validate_answer",
                "evidence": [
                    {"document_id": f"doc-{chunk}", "chunk_id": chunk} for chunk, _s in chunk_status
                ],
                "result": {
                    "status": "completed",
                    "claims": [
                        {"answer_quote": f"段落 {chunk}", "status": verdict, "chunk_id": chunk}
                        for chunk, verdict in chunk_status
                    ],
                },
            }
        ],
    }


def test_carried_evidence_takes_supported_chunks_of_previous_turns() -> None:
    """前のターンの検証で回答の段落を裏付けた根拠だけを、新しいターンから順に引き継ぐ（#1837）。"""
    validations = [
        _previous_validation([("c-old", "supported"), ("c-x", "unsupported")]),
        # 判定が出なかったターンは使わない。
        _previous_validation([("c-failed", "supported")], status="unvalidated"),
        _previous_validation([("c-new", "supported"), ("c-old", "supported")]),
        _previous_validation([("c-other", "supported")], connection="rag2"),
    ]

    assert carried_evidence(validations) == {
        "rag2": [{"document_id": "doc-c-other", "chunk_id": "c-other"}],
        "rag": [
            {"document_id": "doc-c-new", "chunk_id": "c-new"},
            {"document_id": "doc-c-old", "chunk_id": "c-old"},
        ],
    }
    many = [_previous_validation([(f"c-{index}", "supported") for index in range(15)])]
    assert len(carried_evidence(many)["rag"]) == MAX_CARRIED_EVIDENCE
    assert carried_evidence([]) == {}


def test_evidence_groups_put_carried_evidence_after_the_answer_matches() -> None:
    """前のターンの根拠は、回答が引用した根拠の後ろ・残りの根拠の前に入れ、上限の中に残す（#1837）。

    その Run で根拠を返さなかった接続には入れない（前のターンの根拠だけで検証を始めない）。
    """
    steps = [
        _step(
            "rag__rag_retrieve_evidence",
            {"query": "段"},
            {
                "evidence": [
                    {"document_id": "doc-now", "chunk_id": f"doc-now:set:{index}"}
                    for index in range(1, 31)
                ]
            },
        )
    ]
    carried = {
        "rag": [
            {"document_id": "doc-prev", "chunk_id": "doc-prev:set:1"},
            # 今の Run の根拠と同じ参照は重ねない（前のターンの根拠として前に出す）。
            {"document_id": "doc-now", "chunk_id": "doc-now:set:30"},
        ],
        "rag2": [{"document_id": "doc-other", "chunk_id": "doc-other:set:1"}],
    }
    answer = "承認者は管理本部長です【evidence_id:doc-now:set:7】。"

    [(tool_name, refs)] = run_evidence_groups(steps, answer, carried=carried)

    assert tool_name == "rag__rag_retrieve_evidence"
    assert len(refs) == MAX_VALIDATION_EVIDENCE
    assert refs[:3] == [
        {"document_id": "doc-now", "chunk_id": "doc-now:set:7"},
        {"document_id": "doc-now", "chunk_id": "doc-now:set:30"},
        {"document_id": "doc-prev", "chunk_id": "doc-prev:set:1"},
    ]
    assert refs[3] == {"document_id": "doc-now", "chunk_id": "doc-now:set:1"}
    # 上限で落ちるのは、回答に当たらない今の Run の根拠の後ろ。
    assert {"document_id": "doc-now", "chunk_id": "doc-now:set:29"} not in refs
    # 前のターンの根拠を渡さなければ今までどおり。
    [(_name, plain)] = run_evidence_groups(steps, answer)
    assert {"document_id": "doc-prev", "chunk_id": "doc-prev:set:1"} not in plain


def test_followup_turn_validates_with_evidence_the_previous_turn_confirmed(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """会話の続き（#1837。#1833 の 2 問目の形）: 前のターンで確かめた事実の根拠も検証に渡す。

    2 問目の検索は別の文書だけを返すが、モデルは 1 問目の事実（締め日）を言い直す。前のターンの
    検証で回答の段落を裏付けた根拠（doc-1 / chunk-1）も入れて検証する。
    """
    first, _ = _searched_run(monkeypatch)
    assert first.thread_id is not None
    other = deepcopy(mcp.outputs["rag_search"])
    assert isinstance(other, dict)
    other["evidence"] = [
        {
            **other["evidence"][0],
            "evidence_id": "chunk-9",
            "document_id": "doc-9",
            "chunk_id": "chunk-9",
        }
    ]
    mcp.outputs["rag_search"] = other
    _script(
        monkeypatch,
        [function_call("rag__rag_search", {"query": "期限を過ぎた場合"}, call_id="call-2")],
        [assistant_message("締め日（毎月 25 日）を過ぎた場合の扱いは資料に記載がありません。")],
    )
    second = runtime_repository.create_builtin_run(
        RunCreateRequest(
            goal="その期限を過ぎたらどうなりますか？", agent_id=AGENT_ID, thread_id=first.thread_id
        ),
        created_by_user_uuid=USER_UUID,
    )
    anyio.run(builtin_runtime.execute_run, second.id)

    first_call, second_call = mcp.calls_of("rag_validate_answer")
    assert first_call["arguments"]["evidence"] == [{"document_id": "doc-1", "chunk_id": "chunk-1"}]
    # 回答に当たらない今の Run の根拠より前（回答が引用・特徴で当たる根拠の後ろ）。
    assert second_call["arguments"]["evidence"] == [
        {"document_id": "doc-1", "chunk_id": "chunk-1"},
        {"document_id": "doc-9", "chunk_id": "chunk-9"},
    ]
    # 会話の履歴を渡さない設定（往復 0）では引き継がない。
    monkeypatch.setattr(get_settings(), "chat_history_max_turns", 0)
    _script(
        monkeypatch,
        [function_call("rag__rag_search", {"query": "期限"}, call_id="call-3")],
        [assistant_message("資料に記載がありません。")],
    )
    third = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="ほかには？", agent_id=AGENT_ID, thread_id=first.thread_id),
        created_by_user_uuid=USER_UUID,
    )
    anyio.run(builtin_runtime.execute_run, third.id)
    assert mcp.calls_of("rag_validate_answer")[2]["arguments"]["evidence"] == [
        {"document_id": "doc-9", "chunk_id": "chunk-9"}
    ]


def test_carried_evidence_groups_name_the_connection_of_the_previous_turn() -> None:
    """前のターンの根拠だけの検証の入力（#1879）: 接続は前のターンの検証の接続から決める。"""
    carried = {
        "rag2": [{"document_id": "doc-b", "chunk_id": "doc-b:1"}],
        "rag": [
            {"document_id": "doc-a", "chunk_id": "doc-a:1"},
            {"document_id": "doc-a", "chunk_id": "doc-a:2"},
        ],
    }

    assert carried_evidence_groups(carried, "| 項目 | 内容 |") == [
        ("rag2__rag_search", [{"document_id": "doc-b", "chunk_id": "doc-b:1"}]),
        (
            "rag__rag_search",
            [
                {"document_id": "doc-a", "chunk_id": "doc-a:1"},
                {"document_id": "doc-a", "chunk_id": "doc-a:2"},
            ],
        ),
    ]
    assert carried_evidence_groups({}) == []
    assert builtin_runtime._validator_connection("rag__rag_search") is not None  # noqa: SLF001


TABLE_ANSWER = "| 項目 | 内容 |\n| --- | --- |\n| 更新の期限 | 30 日前まで |"


def test_reformatting_the_previous_answer_is_validated_with_the_previous_evidence(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """会話の続きで、ツールを呼ばずに前の回答を表にした回答（#1879）: 前のターンの検証で裏付けた
    根拠で検証し、「資料の根拠を使っておらず…」の注記を付けない。"""
    first, _ = _searched_run(monkeypatch)
    assert first.thread_id is not None
    _script(monkeypatch, [assistant_message(TABLE_ANSWER)])
    created = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="前の回答を表にして", agent_id=AGENT_ID, thread_id=first.thread_id),
        created_by_user_uuid=USER_UUID,
    )

    anyio.run(builtin_runtime.execute_run, created.id)

    run = runtime_repository.get_run(created.id)
    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    assert len(mcp.calls_of("rag_search")) == 1
    _first_call, second_call = mcp.calls_of("rag_validate_answer")
    assert second_call["arguments"] == {
        "query": "前の回答を表にして",
        "answer": TABLE_ANSWER,
        "evidence": [{"document_id": "doc-1", "chunk_id": "chunk-1"}],
    }
    assert run_answer_text(run) == TABLE_ANSWER
    content = _validation(run)
    assert (content["status"], content["connection"]) == ("completed", "rag")
    assert content["tool_name"] == VALIDATOR


def test_followup_without_evidence_of_previous_turns_still_has_the_notice(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """前のターンに裏付けた根拠が無ければ（会話の履歴を渡さない設定など）、今までどおり注記する。"""
    first, _ = _searched_run(monkeypatch)
    monkeypatch.setattr(get_settings(), "chat_history_max_turns", 0)
    # 会話の履歴を渡さないと前の Run の根拠も数えない（#1835 の拒答の外の振る舞いを確かめる）。
    monkeypatch.setattr(get_settings(), "agent_tool_grounding_required", False)
    _script(monkeypatch, [assistant_message(TABLE_ANSWER)])
    created = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="前の回答を表にして", agent_id=AGENT_ID, thread_id=first.thread_id),
        created_by_user_uuid=USER_UUID,
    )

    anyio.run(builtin_runtime.execute_run, created.id)

    run = runtime_repository.get_run(created.id)
    assert len(mcp.calls_of("rag_validate_answer")) == 1
    assert run_answer_text(run) == f"{TABLE_ANSWER}\n\n{NO_EVIDENCE_NOTICE}"
    content = _validation(run)
    assert (content["status"], content["reason"]) == ("unvalidated", "no_rag_evidence")


# 根拠が見つからなかった rag_search の後の拒答（#1879。#1863 の実モデルの確認
# run_0c19e37a48ad42a09ea2584abb760b97）。
ABSENCE_ANSWER = (
    "調査しましたが、利用可能な業務資料・データベースの中に「日本の首都」についての根拠は"
    "見つかりませんでした。したがって、資料に基づく回答はできません。ご了承ください。"
)


def _insufficient_search(mcp: FakeProductMcp) -> None:
    search = deepcopy(mcp.outputs["rag_search"])
    assert isinstance(search, dict)
    search.update(
        {
            "answer": "",
            "outcome": "insufficient_evidence",
            "insufficient_reason": "no_evidence",
            "evidence": [],
        }
    )
    mcp.outputs["rag_search"] = search


@pytest.mark.parametrize(
    ("answer", "notice"),
    [
        (ABSENCE_ANSWER, False),
        ("**回答**\n資料からは確認できません。\nお役に立てず申し訳ありません。", False),
        # 資料の主張を含む回答は今までどおり注記する。
        ("日本の首都は東京です。資料には記載がありません。", True),
        ("資料に記載がありませんが、一般には東京です。ご了承ください。", True),
    ],
    ids=["refusal", "heading-and-apology", "claim", "contrast"],
)
def test_refusal_without_evidence_has_no_notice(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp, answer: str, notice: bool
) -> None:
    """根拠の無い回答のうち、資料に無い・答えられないことだけを述べる拒答に「資料の根拠を
    使っておらず…」を足さず、検証を skipped（absence_only）にする（#1879）。"""
    _insufficient_search(mcp)
    _script(
        monkeypatch,
        [function_call("rag__rag_search", {"query": "日本の首都"}, call_id="call-1")],
        [assistant_message(answer)],
    )
    created = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="日本の首都は？", agent_id=AGENT_ID), created_by_user_uuid=USER_UUID
    )

    anyio.run(builtin_runtime.execute_run, created.id)

    run = runtime_repository.get_run(created.id)
    assert mcp.calls_of("rag_validate_answer") == []
    content = _validation(run)
    if notice:
        assert run_answer_text(run) == f"{answer}\n\n{NO_EVIDENCE_NOTICE}"
        assert (content["status"], content["reason"]) == ("unvalidated", "no_rag_evidence")
    else:
        assert run_answer_text(run) == answer
        assert (content["status"], content["reason"]) == ("skipped", "absence_only")
        assert _answer_outcome(run)["value"] == "insufficient_evidence"


RESTATED_ANSWER = f"{ANSWER}期限を過ぎた場合の扱いは資料に記載がありません。"


def test_followup_whose_search_found_nothing_is_validated_with_the_previous_evidence(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """会話の続きで、今のターンの検索が 0 件だった回答（#3314）: 検索が成功しただけでは根拠を
    得たことにせず、前のターンの検証で裏付けた根拠で検証する（ツールを呼ばなかったときと同じ。
    #1879）。前のターンの事実を言い直した段落に「資料の根拠を使っておらず…」の注記を付けない。"""
    first, _ = _searched_run(monkeypatch)
    assert first.thread_id is not None
    _insufficient_search(mcp)
    _script(
        monkeypatch,
        [function_call("rag__rag_search", {"query": "期限を過ぎた場合"}, call_id="call-2")],
        [assistant_message(RESTATED_ANSWER)],
    )
    created = runtime_repository.create_builtin_run(
        RunCreateRequest(
            goal="その期限を過ぎたらどうなりますか？", agent_id=AGENT_ID, thread_id=first.thread_id
        ),
        created_by_user_uuid=USER_UUID,
    )

    anyio.run(builtin_runtime.execute_run, created.id)

    run = runtime_repository.get_run(created.id)
    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    assert len(mcp.calls_of("rag_search")) == 2
    _first_call, second_call = mcp.calls_of("rag_validate_answer")
    # 前のターンで裏付けた根拠だけで検証する（本文は RAG が今の利用者の権限で読み直す）。
    assert second_call["arguments"]["evidence"] == [{"document_id": "doc-1", "chunk_id": "chunk-1"}]
    assert second_call["arguments"]["answer"] == RESTATED_ANSWER
    assert run_answer_text(run) == RESTATED_ANSWER
    content = _validation(run)
    assert (content["status"], content["connection"]) == ("completed", "rag")


def test_first_turn_whose_search_found_nothing_still_has_the_notice(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """前のターンの根拠が無い（会話の最初の）ターンの検索が 0 件なら、資料の主張を含む回答には
    今までどおり注記する（引き継ぐ根拠が無いときの振る舞いは変えない。#3314）。"""
    _insufficient_search(mcp)
    _script(
        monkeypatch,
        [function_call("rag__rag_search", {"query": "契約の更新"}, call_id="call-1")],
        [assistant_message(ANSWER)],
    )
    created = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="契約の更新の期限は？", agent_id=AGENT_ID),
        created_by_user_uuid=USER_UUID,
    )

    anyio.run(builtin_runtime.execute_run, created.id)

    run = runtime_repository.get_run(created.id)
    assert mcp.calls_of("rag_validate_answer") == []
    assert run_answer_text(run) == f"{ANSWER}\n\n{NO_EVIDENCE_NOTICE}"
    content = _validation(run)
    assert (content["status"], content["reason"]) == ("unvalidated", "no_rag_evidence")


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (ABSENCE_ANSWER, True),
        ("資料に記載がありません。申し訳ありません。", True),
        ("- 取得した資料からは、今の設定値は確かめられません。\n何卒ご了承ください。", True),
        # 結びだけ・主張・逆接・数量・操作を含むものは拒答だけの回答にしない。
        ("ご了承ください。", False),
        ("申し訳ありませんが、管理画面で削除してください。", False),
        ("日本の首都は東京です。資料には記載がありません。", False),
        ("ライセンス費用は月額 1,000 円で、資料には記載がありません。", False),
        ("資料に記載がありません。設定画面で「削除」を押してください。", False),
        ("", False),
    ],
    ids=[
        "refusal",
        "apology",
        "bullet-and-closing",
        "closing-only",
        "apology-with-operation",
        "claim",
        "quantity",
        "operation",
        "empty",
    ],
)
def test_absence_only_is_conservative(answer: str, expected: bool) -> None:
    assert is_absence_only(answer) is expected


PARAGRAPH_A = "契約の更新は 30 日前までに申し出ます。"
PARAGRAPH_B = "更新の手数料は無料です。"


def test_evidence_of_each_rag_connection_is_validated_and_merged(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    # 2 つ目の RAG の接続（同じ fake の RAG。サービストークンの aud は rag）。
    connections: Any = runtime_config_store._mcp_servers  # noqa: SLF001 - fake の接続を足す
    connections["rag2"] = connections["rag"].model_copy(
        update={"server_id": "rag2", "label": "RAG 2", "service_audience": "rag"}
    )
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name="検証の Skill",
            instructions="2 つの RAG で調べて答える。",
            mcp_requirements=[
                SkillMcpRequirement(server_id="rag", tool_names=["rag_search"]),
                SkillMcpRequirement(server_id="rag2", tool_names=["rag_search"]),
            ],
        )
    )

    def search(argument: Any) -> dict[str, Any]:
        output: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_search"])
        chunk = "chunk-a" if argument.query == "更新の期限" else "chunk-b"
        output["evidence"] = [{**output["evidence"][0], "evidence_id": chunk, "chunk_id": chunk}]
        return output

    def validate(argument: Any) -> dict[str, Any]:
        # 接続ごとに、自分の根拠で確かめられる段落だけを裏付ける。
        chunks = {item.chunk_id for item in argument.evidence}
        supported = PARAGRAPH_A if "chunk-a" in chunks else PARAGRAPH_B
        claims = [
            {
                "answer_quote": quote,
                "status": "supported" if quote == supported else "unsupported",
                "chunk_id": next(iter(chunks)) if quote == supported else None,
                "reason": "根拠の範囲",
            }
            for quote in (PARAGRAPH_A, PARAGRAPH_B)
        ]
        return {
            "valid": False,
            "status": "completed",
            "counts": {"supported": 1, "unsupported": 1},
            "claims": claims,
            "missing_evidence": [],
            "stale_evidence": [],
            "evidence_truncated": False,
        }

    mcp.outputs["rag_search"] = search
    mcp.outputs["rag_validate_answer"] = validate
    answer = f"{PARAGRAPH_A}\n{PARAGRAPH_B}"
    _script(
        monkeypatch,
        [function_call("rag__rag_search", {"query": "更新の期限"}, call_id="call-1")],
        [function_call("rag2__rag_search", {"query": "更新の手数料"}, call_id="call-2")],
        [assistant_message(answer)],
    )
    created = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="契約の更新の期限と手数料は？", agent_id=AGENT_ID),
        created_by_user_uuid=USER_UUID,
    )

    anyio.run(builtin_runtime.execute_run, created.id)

    run = runtime_repository.get_run(created.id)
    # 接続ごとに、その接続の根拠だけで 1 回ずつ呼ぶ（最も新しく根拠を返した接続から）。
    calls = mcp.calls_of("rag_validate_answer")
    assert [call["arguments"]["evidence"] for call in calls] == [
        [{"document_id": "doc-1", "chunk_id": "chunk-b"}],
        [{"document_id": "doc-1", "chunk_id": "chunk-a"}],
    ]
    names = [step.tool_call.name for step in run.steps if step.tool_call is not None]
    assert names[-2:] == ["rag2__rag_validate_answer", "rag__rag_validate_answer"]
    # 段落ごとにまとめると、どちらの段落もどれかの接続の根拠で裏付けられる。
    assert run_answer_text(run) == answer
    content = _validation(run)
    assert (content["status"], content["valid"]) == ("completed", True)
    assert content["connection"] is None
    assert [item["connection"] for item in content["connections"]] == ["rag2", "rag"]
    assert [item["status"] for item in content["connections"]] == ["completed", "completed"]
    assert content["result"]["counts"] == {"supported": 2}


def test_rag_without_the_validator_is_unvalidated(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    rag = mcp.servers["rag"]
    mcp.servers["rag"] = McpServer(
        name=rag.name,
        version="test",
        tools=[tool for tool in rag.tools.values() if tool.name != "rag_validate_answer"],
    )

    run, _ = _searched_run(monkeypatch)

    assert run_answer_text(run) == f"{ANSWER}\n\n{UNVERIFIED_NOTICE}"
    content = _validation(run)
    assert (content["status"], content["reason"]) == ("unvalidated", "validator_unavailable")


def test_merge_keeps_contradictions_and_strictest_claim_of_a_connection() -> None:
    first = {
        "valid": False,
        "status": "completed",
        "claims": [
            {"answer_quote": "A。", "status": "supported"},
            {"answer_quote": "A。", "status": "unsupported", "reason": "一部だけ"},
            {"answer_quote": "B。", "status": "contradicted", "reason": "矛盾"},
        ],
    }
    second = {
        "valid": True,
        "status": "completed",
        "claims": [
            {"answer_quote": "A。", "status": "supported"},
            {"answer_quote": "B。", "status": "supported"},
        ],
    }

    merged = merge_results([first, second])

    # 接続の中では最も厳しい判定、接続をまたいでは矛盾を残し、裏付けがあれば通す。
    assert [(item["answer_quote"], item["status"]) for item in merged["claims"]] == [
        ("A。", "supported"),
        ("B。", "contradicted"),
    ]
    assert merged["valid"] is False


GUIDE_STEP_ERROR = {
    "check": "guide_steps",
    "code": "step_order",
    "severity": "error",
    "message": "手順「申し出る」を、先に行う手順「契約を開く」より前に書いています。",
    "step_id": "s2",
    "related_step_id": "s1",
}


def test_search_guide_and_requests_are_checked_and_step_errors_keep_the_answer_with_a_warning(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """rag_search の要求・不足・業務ガイドを渡す（#1276・#1277）。手順の error は本文を置き換えず、
    回答全体の警告にする（#2738。回答の対応は人の確認）。
    """
    search = deepcopy(DEFAULT_OUTPUTS["rag_search"])
    search["requests"] = [{"id": "Q1", "text": "更新の期限", "status": "partial"}]
    search["gaps"] = ["更新の手数料"]
    search["guide"] = {
        "guide_id": "guide-1",
        "revision": 3,
        "title": "契約の更新",
        "decision": "answer",
        "known_conditions": [{"id": "kind", "label": "契約の種類", "value": "年間"}],
        "unknown_conditions": [],
    }
    search["provenance"] = {"search_answer_profile_id": "bv-sales"}
    mcp.outputs["rag_search"] = search
    output = deepcopy(DEFAULT_OUTPUTS["rag_validate_answer"])
    output.update(
        valid=False,
        checks=["requests", "guide", "guide_steps", "impact"],
        findings=[GUIDE_STEP_ERROR],
        guide_revision=3,
    )
    mcp.outputs["rag_validate_answer"] = output

    run, _ = _searched_run(monkeypatch)

    [call] = mcp.calls_of("rag_validate_answer")
    assert call["arguments"]["requests"] == [
        {"id": "Q1", "text": "更新の期限", "status": "partial"}
    ]
    assert call["arguments"]["gaps"] == ["更新の手数料"]
    assert call["arguments"]["guide"] == {
        "search_answer_profile_id": "bv-sales",
        "guide_id": "guide-1",
        "revision": 3,
        "conditions": {"kind": "年間"},
    }
    label = "業務ガイドの手順の確認で問題が見つかった"
    assert run_answer_text(run) == (
        f"{ANSWER}\n\n**{REVIEW_HEADING}**\n\n{REVIEW_INTRO}\n"
        f"- {label}: 手順「申し出る」を、先に行う手順「契約を開く」より前に書いています。"
    )
    content = _validation(run)
    assert content["answer_warnings"] == [
        {"code": "guide_check", "label": label, "detail": GUIDE_STEP_ERROR["message"]}
    ]
    assert content["withheld"] == {
        "removed": 0,
        "unverified": 0,
        "warnings": 0,
        "all": False,
        "listed": 1,
    }
    [answer] = [item for item in run.artifacts if item.kind == "answer"]
    assert (answer.content["outcome"]["value"], answer.content["outcome"]["basis"]) == (
        "needs_human",
        "validation_guide_check",
    )


def _step(name: str, arguments: dict[str, Any], output: dict[str, Any]) -> RunStep:
    return RunStep(
        run_id="run-1",
        status=StepStatus.COMPLETED,
        tool_call=ToolCall(name=name, arguments=arguments),
        tool_result=ToolResult(name=name, success=True, output=output),
    )


def test_lookup_guide_is_used_only_when_it_gives_steps() -> None:
    """rag_search が業務ガイドを返さなければ、同じ接続の rag_lookup_guides の最上位を使う。"""
    lookup = deepcopy(DEFAULT_OUTPUTS["rag_lookup_guides"])
    lookup["guides"][0]["decision"] = "branch"
    steps = [
        _step(
            "rag__rag_lookup_guides",
            {"query": "q", "search_answer_profile_id": "bv-sales", "conditions": {"kind": "月額"}},
            lookup,
        ),
        # 別の接続の業務ガイドは使わない。
        _step("rag2__rag_lookup_guides", {"search_answer_profile_id": "other"}, lookup),
        _step("rag__rag_search", {"query": "q"}, deepcopy(DEFAULT_OUTPUTS["rag_search"])),
    ]

    inputs = connection_check_inputs(steps, "rag__rag_search")

    assert inputs == {
        "answer_trace_ids": ["rag-trace-1"],
        "requests": [{"id": "Q1", "text": "契約条項", "status": "addressed"}],
        "guide": {
            "search_answer_profile_id": "bv-sales",
            "guide_id": "guide-1",
            "revision": 1,
            "conditions": {"kind": "月額"},
        },
    }
    # 確かめる質問の判断（手順を書かない回答）では、手順・影響範囲を確かめさせない。
    lookup["guides"][0]["decision"] = "clarify"
    assert "guide" not in connection_check_inputs(steps, "rag__rag_search")
    # プロファイルが分からなければ業務ガイドは渡さない。
    lookup["guides"][0]["decision"] = "answer"
    steps[0].tool_call = ToolCall(name="rag__rag_lookup_guides", arguments={"query": "q"})
    assert "guide" not in connection_check_inputs(steps, "rag__rag_search")


def test_request_errors_keep_the_answer_and_merge_across_connections() -> None:
    claim = {"answer_quote": ANSWER, "status": "supported", "reason": "根拠あり"}
    request_error = {
        "check": "requests",
        "code": "request_missing",
        "severity": "error",
        "message": "要求「更新の手数料」に答えていません。不足も示していません。",
    }
    warning = {**GUIDE_STEP_ERROR, "code": "step_dependency_missing", "severity": "warning"}
    first = {
        "valid": False,
        "status": "completed",
        "claims": [claim],
        "checks": ["requests"],
        "findings": [request_error],
    }
    second = {
        "valid": True,
        "status": "completed",
        "claims": [claim],
        "checks": ["guide_steps", "requests"],
        "findings": [request_error, warning],
    }

    merged = merge_results([first, second])

    # 同じ指摘は 1 つにし、error があれば valid にしない。
    assert merged["findings"] == [request_error, warning]
    assert merged["checks"] == ["guide_steps", "requests"]
    assert merged["valid"] is False
    published = publish_answer(ANSWER, merged)
    # 要求の error は本文を残し、回答全体の警告として示す（warning は出さない）。
    assert published.text == (
        f"{ANSWER}\n\n**{REVIEW_HEADING}**\n\n{REVIEW_INTRO}\n"
        "- 質問の要求の一部に答えていない: "
        "要求「更新の手数料」に答えていません。不足も示していません。"
    )
    assert [item["code"] for item in published.answer_warnings] == ["requests"]
    assert published.withheld == {
        "removed": 0,
        "unverified": 0,
        "warnings": 0,
        "all": False,
        "listed": 1,
    }
    # 確かめる主張が無くても error の指摘があれば示す。
    no_claims = {"valid": False, "status": "no_claims", "claims": [], "findings": [request_error]}
    assert publish_answer(ANSWER, no_claims).text.endswith("不足も示していません。")


def test_withhold_removes_only_the_listed_paragraphs() -> None:
    answer = "会費は無料です。\n無料です。\n手順: 開く。押す。閉じる。"

    # 同じ文を含む別の段落（「会費は無料です。」）は削らない。行の中の段落だけを外す。
    assert (
        withhold_paragraphs(answer, {"無料です。", "押す。"})
        == "会費は無料です。\n手順: 開く。閉じる。"
    )
    # 位置を決められない段落があれば本文を載せない。
    assert withhold_paragraphs(answer, {"どこにも無い。"}) is None


def test_withhold_removes_numbered_headings_of_emptied_sections_and_renumbers() -> None:
    """中身をすべて外した節の見出しは、番号つきでも消し、残った項目の番号を詰める（#1898・#2692）。

    業務 Agent の確認 #1863（run_839d27d75e224dd39e9bb2fdecee1c99）の形。「1. **承認の有無の
    確認**」の下の段落を外すと、本文が「2. **削除手順の要件**」から始まっていた（#1898 は見出しを
    残したが、中身の無い名前が実行の順の 1 つとして読めるので、#2692 で消して番号を詰める）。
    番号の無い見出し（「**補足**」）は今までどおり消す。
    """
    answer = (
        "**手順（部門長の承認がない場合）**\n\n"
        "1. **承認の有無の確認**\n"
        "   - 利用者の回答により、部門長の承認は取得されていません。\n\n"
        "2. **削除手順の要件**\n"
        "   - アカウントを削除するには、所属部署の部門長の承認が必要です。\n\n"
        "**補足**\n"
        "- 削除は 1 日で終わります。"
    )

    kept = withhold_paragraphs(
        answer,
        {"- 利用者の回答により、部門長の承認は取得されていません。", "- 削除は 1 日で終わります。"},
    )

    assert kept == (
        "**手順（部門長の承認がない場合）**\n\n"
        "1. **削除手順の要件**\n"
        "   - アカウントを削除するには、所属部署の部門長の承認が必要です。"
    )


@pytest.mark.parametrize(
    ("answer", "quotes", "expected"),
    [
        # 外した段落の直後の段落の、前の段落を受ける接続の語を外す（同じ行・次の行・見出しを
        # 挟んでも）。
        (
            "締め日は 25 日です。したがって、処置は不明です。",
            {"締め日は 25 日です。"},
            "処置は不明です。",
        ),
        (
            "締め日は 25 日です。\nそのため、処置は不明です。",
            {"締め日は 25 日です。"},
            "処置は不明です。",
        ),
        (
            "締め日は 25 日です。\n\n**結論**\n\nつまり、処置は不明です。",
            {"締め日は 25 日です。"},
            "**結論**\n\n処置は不明です。",
        ),
        # 意味を変える語（ただし・なお）は残す。
        (
            "締め日は 25 日です。ただし、処置は不明です。",
            {"締め日は 25 日です。"},
            "ただし、処置は不明です。",
        ),
        # 直前の段落を残した段落の接続の語は変えない。
        (
            "締め日は 25 日です。延長は無い。したがって、処置は不明です。",
            {"延長は無い。"},
            "締め日は 25 日です。処置は不明です。",
        ),
        (
            "延長は無い。締め日は 25 日です。したがって、処置は不明です。",
            {"延長は無い。"},
            "締め日は 25 日です。したがって、処置は不明です。",
        ),
    ],
    ids=[
        "same-line",
        "next-line",
        "across-heading",
        "keeps-tadashi",
        "after-withheld",
        "after-kept",
    ],
)
def test_withhold_drops_dangling_connectives(answer: str, quotes: set[str], expected: str) -> None:
    """外した段落を受ける接続の語（「したがって、」など）から本文が始まらない（#1833）。"""
    assert withhold_paragraphs(answer, quotes) == expected


# #1833 の実機の会話の 2 問目（run_337c8282fade4f1981d0a28f666d521d）の回答と判定。前のターンの
# 事実を言い直した 1 文目と出典の行が外され、本文が「したがって、」から始まり、理由に内部の語が
# 出ていた。
FOLLOWUP_FIRST = (
    "資料を確認したところ、経費精算の締め日（毎月 25 日）を過ぎた場合の取扱いや"
    "ペナルティ等についての記載は見当たりませんでした。"
)
FOLLOWUP_ABSENCE = "したがって、締め日を過ぎた際の具体的な処置は資料からは確認できません。"
FOLLOWUP_SOURCE = (
    "※根拠：**経費精算マニュアル.md（締め日 25 日の記載）** ただし、期限超過時の対応は"
    "本資料には記載がなく、現時点では不明です。"
)


def test_followup_answer_starts_without_a_dangling_connective_and_hides_internal_reasons() -> None:
    answer = FOLLOWUP_FIRST + FOLLOWUP_ABSENCE + FOLLOWUP_SOURCE
    result = _verdict(
        _claim(
            FOLLOWUP_FIRST,
            "contradicted",
            "提供された証拠項目には締め日超過時の扱いの記載があり、記載が無いという主張と矛盾する。",
        ),
        _claim(
            FOLLOWUP_ABSENCE, "unsupported", "締め日超過時の処置に関する情報は証拠に含まれない。"
        ),
        _claim(
            FOLLOWUP_SOURCE,
            "unsupported",
            "回答が示す根拠「経費精算マニュアル.md」は evidence_items に存在せず、"
            "引用エラーとなる。",
        ),
    )

    published = publish_answer(answer, result)

    # 不足の文（主張ではない）は残し、矛盾で外した 1 文目を受ける「したがって、」は外す。根拠で
    # 確かめられない出典の文は本文に残して印を付ける（#2738）。
    assert published.body == "締め日を過ぎた際の具体的な処置は資料からは確認できません。" + (
        FOLLOWUP_SOURCE
    )
    removed, review = published.sections
    # 理由は判定の種類の短い文だけ（検証のモデルが書いた理由・内部の語を出さない）。
    [line] = removed.splitlines()[3:]
    assert line.startswith("- 根拠と矛盾: 「資料を確認したところ、")
    [listed] = review.splitlines()[3:]
    assert re.fullmatch(r"- 根拠で確かめられない: 「※根拠：[^\n]+」", listed)
    assert not re.search(r"evidence_items|監査|クレーム|証拠項目|引用エラー", published.text)
    # 理由の全文は記録（印の理由の detail）に残す。
    assert "evidence_items" in published.passage_marks[0]["reasons"][0]["detail"]
    assert (published.withheld["removed"], published.withheld["unverified"]) == (1, 1)


def test_location_and_closing_lines_marked_unassessed_keep_the_answer() -> None:
    """#1833 の 1 問目: 場所のラベルの行と締めくくりの定型の文を、RAG が unassessed にしても
    外さない。"""
    case = next(
        item for item in CITATION_CASES["answers"] if item["id"] == "expense-closing-day-location"
    )
    answer = case["answer"]
    closing = "以上、資料に基づく回答です。"
    assert non_claim_passages(answer).get(closing) == "heading"
    result = _verdict(
        *[_claim(text, "supported", "根拠に記載。") for text in case["claim_passages"]],
        _claim(
            case["citation_passages"][1], "unassessed", "本文を見出しとして監査から除外できません。"
        ),
        _claim(closing, "unassessed", "本文を見出しとして監査から除外できません。"),
    )

    published = publish_answer(answer, result)
    assert (published.text, published.passage_marks, published.removed_passages) == (answer, [], [])


# ---- 主張ではない段落を外さない（#1306） ------------------------------------------------
# #1289 の業務支援の評価（D）で、見出し・出典の行・利用者への質問・「資料に記載が無い」の文まで
# 「資料で確認できず除いた内容」に移り、回答が読みにくく、拒答が拒答に見えなくなった。

TRIAL_HEADING = "## 試用アカウントの有効期限"
TRIAL_FACT = "試用アカウントの有効期限は、発行日から最長 30 日です。"
TRIAL_STEPS = "**延長の手順**"
TRIAL_STEP_OK = "1. ポータルの「アカウント管理」を開きます。"
# 根拠に無い手順（外す）。
TRIAL_STEP_UNSUPPORTED = "2. 「延長」を押して、有効期限を 90 日まで延長します。"
TRIAL_CITATION = "【portal-operations-manual.pdf, section 2. アカウント管理, page 1】"
TRIAL_SOURCE = "出典: portal-operations-manual.pdf p.2"
TRIAL_ABSENCE = "延長の上限日数については、資料に記載がありません。"
TRIAL_QUESTION = "所属部署の部門長の承認は得ていますか？（はい／いいえ）"
TRIAL_ANSWER = "\n".join(
    [
        TRIAL_HEADING,
        "",
        TRIAL_FACT,
        "",
        TRIAL_STEPS,
        TRIAL_STEP_OK,
        TRIAL_STEP_UNSUPPORTED,
        TRIAL_CITATION,
        TRIAL_SOURCE,
        "",
        TRIAL_ABSENCE,
        TRIAL_QUESTION,
    ]
)


def _claim(quote: str, status: str, reason: str = "") -> dict[str, Any]:
    claim: dict[str, Any] = {"answer_quote": quote, "status": status, "reason": reason}
    if status in {"supported", "contradicted"}:
        claim["chunk_id"] = "chunk-1"
    return claim


def _verdict(*claims: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for claim in claims:
        counts[claim["status"]] = counts.get(claim["status"], 0) + 1
    output: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_validate_answer"])
    output.update({"valid": False, "status": "completed", "counts": counts, "claims": list(claims)})
    return output


def test_non_claim_passages_are_kept_and_unsupported_steps_are_marked(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    mcp.outputs["rag_validate_answer"] = _verdict(
        _claim(TRIAL_HEADING, "unassessed", "見出しのため監査しない。"),
        _claim(TRIAL_FACT, "supported", "根拠に記載。"),
        _claim(TRIAL_STEPS, "unassessed", "見出し。"),
        _claim(TRIAL_STEP_OK, "supported", "根拠に記載。"),
        _claim(TRIAL_STEP_UNSUPPORTED, "unsupported", "根拠に延長の操作が無い。"),
        _claim(
            TRIAL_CITATION,
            "unassessed",
            "本文を見出しとして監査から除外できません。参照情報のみで、具体的な主張を含まない",
        ),
        _claim(TRIAL_SOURCE, "unassessed", "出典の行。"),
        _claim(TRIAL_ABSENCE, "unsupported", "根拠で確かめられない。"),
        _claim(TRIAL_QUESTION, "unassessed", "This passage is a question to the user."),
    )

    run, _ = _searched_run(monkeypatch, answer=TRIAL_ANSWER)

    answer = run_answer_text(run)
    assert answer is not None
    body, section = answer.split(f"\n\n**{REVIEW_HEADING}**\n\n", 1)
    # 見出し・出典・不足の文・質問は印を付けずに残し、根拠に無い手順も本文に残して印を付ける
    # （#1306・#2738）。
    assert body == TRIAL_ANSWER
    assert section.splitlines() == [
        REVIEW_INTRO,
        f"- 根拠で確かめられない: 「{TRIAL_STEP_UNSUPPORTED}」",
    ]
    content = _validation(run)
    assert content["withheld"] == {
        "removed": 0,
        "unverified": 1,
        "warnings": 0,
        "all": False,
        "listed": 1,
    }
    assert [item["text"] for item in content["passage_marks"]] == [TRIAL_STEP_UNSUPPORTED]
    # RAG の判定は変えず、主張ではない段落に non_claim を付けて残す（画面が同じ判断をする）。
    marked = {item["answer_quote"]: item.get("non_claim") for item in content["result"]["claims"]}
    assert marked == {
        TRIAL_HEADING: "heading",
        TRIAL_FACT: None,
        TRIAL_STEPS: "heading",
        TRIAL_STEP_OK: None,
        TRIAL_STEP_UNSUPPORTED: None,
        TRIAL_CITATION: "citation",
        TRIAL_SOURCE: "citation",
        TRIAL_ABSENCE: "absence",
        TRIAL_QUESTION: "question",
    }
    assert [item["status"] for item in content["result"]["claims"]].count("unassessed") == 5


def test_refusal_that_the_documents_lack_the_answer_is_kept_as_a_refusal(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    # km-license-fee: 資料に答えが無い質問。拒答の文を外すと
    # 「確かめられた内容はありません」だけになる。
    refusal = "\n".join(
        [
            "**ライセンス費用について**",
            "取得した資料には、ライセンス費用の記載は見つかりませんでした。",
            "資料からは、費用の金額や支払い方法は確かめられません。",
            "契約の担当部署に問い合わせてください。",
        ]
    )
    mcp.outputs["rag_validate_answer"] = _verdict(
        _claim("**ライセンス費用について**", "unassessed", "見出し。"),
        _claim(
            "取得した資料には、ライセンス費用の記載は見つかりませんでした。",
            "unsupported",
            "根拠で確かめられない。",
        ),
        _claim(
            "資料からは、費用の金額や支払い方法は確かめられません。",
            "unsupported",
            "根拠で確かめられない。",
        ),
        _claim("契約の担当部署に問い合わせてください。", "data_confirmation", "問い合わせ先。"),
    )

    run, _ = _searched_run(monkeypatch, answer=refusal)

    assert run_answer_text(run) == refusal
    content = _validation(run)
    assert (content["passage_marks"], content["removed_passages"]) == ([], [])


def test_refusal_mixed_with_an_unsupported_fee_still_marks_the_fee() -> None:
    answer = (
        "ライセンス費用は、資料に記載がありません。\n"
        "ライセンス費用は月額 1,000 円で、資料には記載がありません。"
    )
    result = _verdict(
        _claim("ライセンス費用は、資料に記載がありません。", "unsupported", "確かめられない。"),
        _claim(
            "ライセンス費用は月額 1,000 円で、資料には記載がありません。",
            "unsupported",
            "金額が根拠に無い。",
        ),
    )

    published = publish_answer(answer, result)

    # 金額の主張を抱き合わせた文は「記載が無い」の文として扱わず、印を付ける（本文には残す）。
    assert published.body == answer
    assert [item["text"] for item in published.passage_marks] == [
        "ライセンス費用は月額 1,000 円で、資料には記載がありません。"
    ]
    assert "月額 1,000 円" in published.sections[0]
    assert published.withheld["unverified"] == 1


def test_headings_and_citations_left_alone_are_not_published() -> None:
    # 手順をすべて矛盾で外した後に、見出しと出典の行だけを本文として出さない。
    step = "1. 管理画面で「削除」を押します。"
    answer = f"## アカウントの削除の手順\n{step}\n【admin-guide.pdf p.12】"
    result = _verdict(
        _claim("## アカウントの削除の手順", "unassessed", "見出し。"),
        _claim(step, "contradicted", "根拠と矛盾。"),
        _claim("【admin-guide.pdf p.12】", "unassessed", "出典。"),
    )

    published = publish_answer(answer, result)

    assert published.body == ALL_REMOVED
    assert published.headings == [REMOVED_HEADING]
    assert published.withheld["removed"] == 1 and published.withheld["all"] is False
    # 段落を外した行に残った出典、中身をすべて外した節の見出しも消す。
    kept = withhold_paragraphs(
        f"{TRIAL_FACT}\n\n## 削除の手順\n{step}【admin-guide.pdf p.12】"
        "\n\n## 注意\n- 元に戻せません。",
        {step},
    )
    assert kept == f"{TRIAL_FACT}\n\n## 注意\n- 元に戻せません。"


# #1362 の評価の Run の形（#1391）。br-document-portal-retention: 台帳と規程を合わせた結論を、
# 検証のモデルは「根拠で裏付けられる」と判定したが、ID を照合できず `citation_error` になった。
PORTAL_RETENTION_LEDGER = (
    "- システム台帳 《system‑ledger.xlsx》: 「機密区分: 社内限り」【record_codes.values[1]】"
)
PORTAL_RETENTION_ANSWER = "\n".join(
    [
        "ドキュメントポータル（SYS‑105）の機密区分は「社内限り」（システム台帳）であり、"
        "機密区分「社内限り」のデータは **3 年間** 保管することが定められています。",
        "",
        PORTAL_RETENTION_LEDGER,
        "- データ保管規程 《data‑retention‑rules.pdf》: 「社内限りのデータは 3 年間保管します」"
        "【evidence_id af257c62c85d45cd9211a098fdca6fb4:16f11273eede4c4f508dcc4453985fffa…:1】",
        "",
        "したがって、ドキュメントポータルのデータの保管期間は **3 年** です。",
    ]
)
# 「未登録の原文ID。」は RAG が付ける理由の前置き。理由の文は根拠を認めている。
UNRESOLVED_REASON = "未登録の原文ID。台帳と規程で主張は根拠で裏付けられます。"


def _portal_retention_claims(first_status: str = "citation_error") -> list[dict[str, Any]]:
    """評価の Run の判定（台帳の行は通り、ほかの 3 段落が `citation_error`）。

    最初の段落の判定だけ変えられる。
    """
    paragraphs = [line for line in PORTAL_RETENTION_ANSWER.split("\n") if line]
    statuses = [first_status, "supported", "citation_error", "citation_error"]
    return [
        _claim(line, status, UNRESOLVED_REASON)
        for line, status in zip(paragraphs, statuses, strict=True)
    ]


def test_unresolved_citations_are_kept_with_marks() -> None:
    """出典を照合できない段落は本文に残して印を付ける（#1391・#2738。評価では本文が
    「確かめられた内容はありませんでした」だけになった）。"""
    result = _verdict(*_portal_retention_claims())

    published = publish_answer(PORTAL_RETENTION_ANSWER, result)

    assert published.body == PORTAL_RETENTION_ANSWER
    assert [(item["state"], item["reasons"][0]["code"]) for item in published.passage_marks] == [
        ("unverified", "citation_error")
    ] * 3
    assert "- 出典を確かめられない: 「したがって、ドキュメントポータル" in published.sections[0]
    assert published.withheld["unverified"] == 3
    # 確かめが終わらない見出し（次の行で決まる番号つきの見出し）には印を付けない
    # （da-trial-account-setup の手順の見出し）。
    heading = "1. **検証用アカウントを登録**"
    step = "- 管理画面の「利用者」メニューを開き「追加」ボタンを押す。"
    published = publish_answer(
        f"{heading}\n{step}",
        _verdict(_claim(heading, "unassessed", "見出し。"), _claim(step, "citation_error", "")),
    )
    assert published.text.startswith(f"{heading}\n{step}\n\n**{REVIEW_HEADING}**")
    assert [item["text"] for item in published.passage_marks] == [step]


def test_unsupported_paragraphs_are_kept_with_unresolved_citations() -> None:
    """根拠で確かめられない段落（`unsupported`）も外さず、出典を照合できない段落と同じく本文に
    残して印を付ける（#2738。外すのは矛盾だけ）。"""
    claims = _portal_retention_claims("unsupported")
    published = publish_answer(PORTAL_RETENTION_ANSWER, _verdict(*claims))
    assert published.body == PORTAL_RETENTION_ANSWER
    assert [item["reasons"][0]["code"] for item in published.passage_marks] == [
        "unsupported",
        "citation_error",
        "citation_error",
    ]
    assert published.removed_passages == []
    fact = "検証用アカウントの有効期限は最長 30 日です。"
    step = "- 管理画面の「利用者」メニューを開き「追加」ボタンを押す。"
    published = publish_answer(
        f"{fact}\n\n{step}",
        _verdict(_claim(fact, "supported"), _claim(step, "citation_error", "")),
    )
    assert published.text.startswith(f"{fact}\n\n{step}\n\n**{REVIEW_HEADING}**")
    assert published.withheld["unverified"] == 1


# #2622 の合成の事例（資料は「設定Aは無効」、回答は「設定Aは有効」）。
SETTING_ANSWER = "設定Aは有効です。"
SETTING_REASON = "合成資料は設定Aが無効と記載している。"


def _unbound(status: str, audit_status: str, reference_check: str) -> dict[str, Any]:
    """RAG が根拠の ID を照合できなかった判定（出典は無い）。"""
    return {
        "answer_quote": SETTING_ANSWER,
        "status": status,
        "reason": SETTING_REASON,
        "audit_status": audit_status,
        "reference_check": reference_check,
    }


@pytest.mark.parametrize("status", ["contradicted", "citation_error"])
def test_contradiction_with_an_unbound_citation_is_not_published(status: str) -> None:
    """監査が矛盾と判定した段落は、根拠の ID を照合できなくても（判定が citation_error に変わって
    いても）載せない (#2622)。"""
    claim = _unbound(status, "contradicted", "audit_id_invalid")
    published = publish_answer(SETTING_ANSWER, _verdict(claim))
    assert published.body == ALL_REMOVED
    assert published.removed_passages == [
        {
            "text": SETTING_ANSWER,
            "reasons": [{"code": "contradicted", "label": "根拠と矛盾", "detail": SETTING_REASON}],
        }
    ]
    assert f"- 根拠と矛盾: 「{SETTING_ANSWER}」" in published.sections[0]
    assert published.passage_marks == []


def test_supported_claims_with_unbound_audit_ids_still_keep_the_answer() -> None:
    """監査が裏付けありと判定し、ID の照合だけができなかった段落は、本文を残して印を付ける
    (#1391・#2622・#2738)。"""
    claim = _unbound("citation_error", "supported", "audit_id_invalid")
    published = publish_answer(SETTING_ANSWER, _verdict(claim))
    assert published.body == SETTING_ANSWER
    assert published.passage_marks[0]["reasons"][0]["code"] == "citation_error"
    # 引用が根拠に無い（quote_unmatched）・監査の元の判定が根拠に無い段落は、その理由にする。
    unmatched = _unbound("citation_error", "supported", "quote_unmatched")
    marks = publish_answer(SETTING_ANSWER, _verdict(unmatched)).passage_marks
    assert marks[0]["reasons"][0]["code"] == "quote_unmatched"
    refuted = _unbound("citation_error", "unsupported", "audit_id_invalid")
    marks = publish_answer(SETTING_ANSWER, _verdict(refuted)).passage_marks
    assert marks[0]["reasons"][0]["code"] == "unsupported"


def test_contradiction_from_any_connection_wins_even_when_its_citation_is_unbound() -> None:
    """接続をまたいでも、監査の元の判定が矛盾の段落は矛盾として選ぶ（ほかの接続が通しても載せない。#2622）。"""
    unbound = _verdict(_unbound("citation_error", "contradicted", "audit_id_invalid"))
    passed = _verdict(_claim(SETTING_ANSWER, "supported", "根拠に記載。"))
    merged = merge_results([passed, unbound])
    [claim] = merged["claims"]
    assert claim["audit_status"] == "contradicted" and merged["valid"] is False
    published = publish_answer(SETTING_ANSWER, merged)
    assert published.body == ALL_REMOVED
    assert published.withheld["removed"] == 1


# #2604 の合成の事例: 前提の確認 → 後続の操作の番号つきの手順と、手順と無関係な説明。
PREREQUISITE_STEP = "1. 対象が同じものであることを確認する。"
FOLLOWING_STEPS = ["2. 対象の情報を訂正する。", "3. 識別子を統合する。"]
STEPS_ANSWER = "\n".join([PREREQUISITE_STEP, *FOLLOWING_STEPS])
STEP_INTRO = "識別子は対象ごとに 1 つだけ持ちます。"
STEP_OUTRO = "統合した後は、旧い識別子で検索できません。"
STEPS_WITH_EXPLANATION = f"{STEP_INTRO}\n\n{STEPS_ANSWER}\n\n{STEP_OUTRO}"


def _following_mark(quote: str, code: str) -> dict[str, Any]:
    """続く手順の段落の警告の印（#2738）。"""
    label = {
        "after_contradicted_step": "前の手順が根拠と矛盾",
        "after_unverified_step": "前提の手順が未確認",
    }
    return {
        "text": quote,
        "state": "warning",
        "reasons": [{"code": code, "label": label[code], "detail": ""}],
    }


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("citation_error", "citation_error"),
        ("unsupported", "unsupported"),
        ("unassessed", "unassessed"),
    ],
)
def test_unverified_prerequisite_step_marks_the_steps_that_follow(status: str, code: str) -> None:
    """前提の手順が未確認なら、それに続く操作の手順も本文に残して「前提の手順が未確認」の警告を
    付け、手順と無関係な説明には付けない（#2604・#2738）。"""
    result = _verdict(
        _claim(STEP_INTRO, "supported"),
        _claim(PREREQUISITE_STEP, status, "未登録の原文ID。"),
        *(_claim(step, "supported") for step in FOLLOWING_STEPS),
        _claim(STEP_OUTRO, "supported"),
    )

    published = publish_answer(STEPS_WITH_EXPLANATION, result)

    assert published.body == STEPS_WITH_EXPLANATION
    assert published.passage_marks[0]["text"] == PREREQUISITE_STEP
    assert published.passage_marks[0]["reasons"][0]["code"] == code
    assert published.passage_marks[1:] == [
        _following_mark(step, "after_unverified_step") for step in FOLLOWING_STEPS
    ]
    assert published.withheld == {
        "removed": 0,
        "unverified": 1,
        "warnings": 2,
        "all": False,
        "listed": 3,
    }


def test_contradicted_prerequisite_removes_it_and_marks_the_steps_that_follow() -> None:
    """前提の手順が根拠と矛盾したら、その手順だけを外し、続く操作の手順は本文に残して「前の手順が
    根拠と矛盾」の警告を付ける（#2604・#2738。手順の連鎖で外さない）。"""
    result = _verdict(
        _claim(PREREQUISITE_STEP, "contradicted", "根拠と矛盾。"),
        *(_claim(step, "supported") for step in FOLLOWING_STEPS),
    )

    published = publish_answer(STEPS_ANSWER, result)

    # 外した項目の後の番号は詰める（#2692）。
    assert published.body == "1. 対象の情報を訂正する。\n2. 識別子を統合する。"
    assert [item["text"] for item in published.removed_passages] == [PREREQUISITE_STEP]
    assert published.passage_marks == [
        _following_mark(step, "after_contradicted_step") for step in FOLLOWING_STEPS
    ]
    review = published.sections[1].splitlines()
    assert review[3:] == [f"- 前の手順が根拠と矛盾: 「{step}」" for step in FOLLOWING_STEPS]


def test_unsupported_prerequisite_keeps_a_steps_only_answer() -> None:
    """前提が根拠で確かめられなくても、手順だけの回答の本文を残す（#2738。以前は本文を載せな
    かった）。"""
    result = _verdict(
        _claim(PREREQUISITE_STEP, "unsupported", "根拠に無い。"),
        *(_claim(step, "supported") for step in FOLLOWING_STEPS),
    )

    published = publish_answer(STEPS_ANSWER, result)

    assert published.body == STEPS_ANSWER
    assert [item["state"] for item in published.passage_marks] == [
        "unverified",
        "warning",
        "warning",
    ]


def test_steps_before_the_marked_step_and_unordered_lists_are_not_marked() -> None:
    """未確認の手順より前の手順には印を付けない。番号の無い箇条書きは順序を表さないので、一部の
    項目が未確認でもほかの項目に続く手順の印を付けない（#2604・#2738）。"""
    result = _verdict(
        _claim(STEP_INTRO, "supported"),
        _claim(PREREQUISITE_STEP, "supported"),
        _claim(FOLLOWING_STEPS[0], "unsupported", "根拠に無い。"),
        _claim(FOLLOWING_STEPS[1], "supported"),
        _claim(STEP_OUTRO, "supported"),
    )
    published = publish_answer(STEPS_WITH_EXPLANATION, result)
    assert published.body == STEPS_WITH_EXPLANATION
    assert [item["text"] for item in published.passage_marks] == FOLLOWING_STEPS
    assert published.passage_marks[1] == _following_mark(
        FOLLOWING_STEPS[1], "after_unverified_step"
    )

    bullets = ["- 識別子は 12 桁です。", "- 識別子は再利用しません。", "- 識別子は変更できません。"]
    result = _verdict(
        _claim(bullets[0], "unsupported", "根拠に無い。"),
        *(_claim(item, "supported") for item in bullets[1:]),
    )
    published = publish_answer("\n".join(bullets), result)
    assert published.body == "\n".join(bullets)
    assert [item["text"] for item in published.passage_marks] == [bullets[0]]


def test_numbered_facts_after_a_flagged_item_are_not_marked() -> None:
    """番号で事実を並べた一覧は、1 項目が未確認・矛盾でも後の事実の項目に続く手順の印を付けない
    （操作の項目だけが前の項目に依存する。#2604・#2738）。"""
    facts = [
        "1. 識別子は 12 桁です。",
        "2. 識別子は再利用しません。",
        "3. 識別子は変更できません。",
        "4. 発行の記録は 5 年間保存されます。",
    ]
    for status in ("citation_error", "unsupported", "unassessed", "contradicted"):
        result = _verdict(
            _claim(facts[0], status, "未登録の原文ID。"),
            *(_claim(item, "supported") for item in facts[1:]),
        )
        published = publish_answer("\n".join(facts), result)
        flagged = published.removed_passages + published.passage_marks
        assert [item["text"] for item in flagged] == [facts[0]]
        if status == "contradicted":
            # 残った項目の番号は詰める（#2692）。
            assert published.body == "\n".join(
                f"{number}.{item[2:]}" for number, item in enumerate(facts[1:], 1)
            )
        else:
            assert published.body == "\n".join(facts)
    # 項目が事実なら、後の操作の項目も前提を失わない（#2735）。
    request = "5. 変更が必要なときは、管理者に申請してください。"
    answer = "\n".join([*facts, request])
    assert dependent_steps(answer, {facts[0]}) == []
    # 項目が操作なら、後の操作の項目だけを返し、間の事実の項目は返さない。
    check = "1. 対象が同じものであることを確認する。"
    answer = "\n".join([check, *facts[1:], request])
    assert dependent_steps(answer, {check}) == [(4, 0, request)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1. 対象の情報を訂正する。", True),
        ("- 管理者に申請してください。", True),
        ("2. **申請書を提出します**（第 2 章）。", True),
        ("3. 対象を確認しておく。", True),
        ("1. 識別子は 12 桁です。", False),
        ("- 延長は最長 90 日までできます。", False),
        ("2. 処理の中で通知が発生します。", False),
        ("3. 承認者は管理者である。", False),
    ],
)
def test_operation_steps_are_decided_by_the_predicate_form(text: str, expected: bool) -> None:
    """手順の項目かは、操作の指示か文末の指示の形で決める（業務の語は見ない。#2604）。"""
    assert is_operation_step(text) is expected


def test_numbered_step_headings_and_their_contents_follow_the_flagged_step() -> None:
    """見出しの形の番号つきの手順（下の行に中身）は、矛盾・未確認の手順に続く手順の中身に警告の
    印を付け、名前の見出し・出典の行・一覧の外の段落には付けない（#2604・#2738）。"""
    check = "   - 対象が同じものであることを確認する。"
    fix = "   - 対象の情報を訂正する。"
    merge = "識別子を統合する。"
    answer = "\n".join(
        [
            STEP_INTRO,
            "",
            "1. **確認**",
            check,
            "",
            "2. **訂正**",
            fix,
            "出典: manual-a.pdf p.3",
            "",
            "3. **統合**",
            merge,
            "",
            "## 注意",
            STEP_OUTRO,
        ]
    )

    def result(status: str) -> dict[str, Any]:
        return _verdict(
            _claim(STEP_INTRO, "supported"),
            _claim(check.strip(), status, "根拠に無い。"),
            _claim(fix.strip(), "supported"),
            _claim(merge, "supported"),
            _claim(STEP_OUTRO, "supported"),
        )

    published = publish_answer(answer, result("unsupported"))
    assert published.body == answer
    assert [(item["text"], item["state"]) for item in published.passage_marks] == [
        (check.strip(), "unverified"),
        (fix.strip(), "warning"),
        (merge, "warning"),
    ]

    published = publish_answer(answer, result("contradicted"))
    # 中身をすべて外した「1. **確認**」の見出しも残さず、続く項目の番号を詰める（#2692）。
    assert published.body == "\n".join(
        [
            STEP_INTRO,
            "",
            "1. **訂正**",
            fix,
            "出典: manual-a.pdf p.3",
            "",
            "2. **統合**",
            merge,
            "",
            "## 注意",
            STEP_OUTRO,
        ]
    )
    assert published.passage_marks == [
        _following_mark(fix.strip(), "after_contradicted_step"),
        _following_mark(merge, "after_contradicted_step"),
    ]


def test_dependent_steps_are_located_by_position() -> None:
    """同じ文が手順の外にもあれば、手順の外のものは外さない。

    数（「1.5 倍」）は手順の番号にしない。
    """
    record = "訂正した内容を記録する。"
    step = f"{FOLLOWING_STEPS[0]}{record}"
    answer = "\n".join([PREREQUISITE_STEP, step, "", f"1.5 倍の時間がかかります。{record}"])
    dependents = dependent_steps(answer, {PREREQUISITE_STEP})
    assert dependents == [(1, 0, FOLLOWING_STEPS[0]), (1, len(FOLLOWING_STEPS[0]), record)]
    positions = frozenset((index, begin) for index, begin, _text in dependents)
    kept = withhold_paragraphs(answer, {PREREQUISITE_STEP}, positions)
    assert kept == f"1.5 倍の時間がかかります。{record}"
    # 全角の番号・括弧の番号・丸数字も手順の番号。
    for marks in (["１．", "２．"], ["（1）", "（2）"], ["①", "②"]):
        steps = f"{marks[0]}確認する。\n{marks[1]}訂正する。"
        assert dependent_steps(steps, {f"{marks[0]}確認する。"}) == [
            (1, 0, f"{marks[1]}訂正する。")
        ]


# #2735 の合成の事例: 並列の別の方法を、名前の項目の下の入れ子の中身（事実と手順）で並べた一覧。
METHOD_INTRO = "変更の方法は 3 通りあります。"
METHOD_ONE = "1. **方法1 ファイルを直して取り込む**"
METHOD_ONE_STEP = (
    "- 手順概要：終了日の列を空欄にし、開始日の列に新しい年月を入力して保存する【証拠 1】。"
)
METHOD_ONE_RESULT = "- 保存したファイルを取込処理から取り込むと、新しい値が反映される。"
METHOD_TWO = "2. **方法2 画面で直す**"
METHOD_TWO_PAGE = "- 「方法2 画面で直す」への手順が 9 ページに記載されている。"
METHOD_TWO_STEP = "- 手順概要：画面で対象の行を開き、値を上書きして登録する【証拠 2】。"
METHOD_THREE = "3. **方法3 別の取込で直す**"
METHOD_THREE_PAGE = "- 「方法3 別の取込で直す」への手順が 13 ページに記載されている。"
METHODS_ANSWER = "\n".join(
    [
        METHOD_INTRO,
        "",
        METHOD_ONE,
        f"   {METHOD_ONE_STEP}",
        f"   {METHOD_ONE_RESULT}",
        METHOD_TWO,
        f"   {METHOD_TWO_PAGE}",
        f"   {METHOD_TWO_STEP}",
        METHOD_THREE,
        f"   {METHOD_THREE_PAGE}",
    ]
)


def test_flagged_result_in_parallel_methods_does_not_mark_the_other_methods() -> None:
    """並列の方法の 1 つの結果の段落が未確認でも、ほかの方法の項目・頁の参照の文・手順に続く手順の
    印を付けない（#2735・#2738）。"""
    assert dependent_steps(METHODS_ANSWER, {METHOD_ONE_RESULT}) == []
    result = _verdict(
        _claim(METHOD_INTRO, "supported"),
        _claim(METHOD_ONE_STEP, "supported"),
        _claim(METHOD_ONE_RESULT, "unsupported", "根拠に無い。"),
        _claim(METHOD_TWO_PAGE, "supported"),
        _claim(METHOD_TWO_STEP, "supported"),
        _claim(METHOD_THREE_PAGE, "supported"),
    )

    published = publish_answer(METHODS_ANSWER, result)

    assert published.body == METHODS_ANSWER
    assert [item["text"] for item in published.passage_marks] == [METHOD_ONE_RESULT]
    assert published.withheld["warnings"] == 0


def test_withheld_operation_in_a_parallel_method_stays_inside_that_method() -> None:
    """並列の方法の 1 つの操作を外したら、同じ方法の中の後の操作だけを外し、事実の段落とほかの方法は
    残す（各項目は Markdown の見出しで切った節と同じく独立する。#2735）。"""
    # 方法 1 の後の段落は結果を述べる文なので残す。
    assert dependent_steps(METHODS_ANSWER, {METHOD_ONE_STEP}) == []
    # 方法 1 の中に後の操作があれば、それだけを外す。
    register = "- 取り込んだ後に、反映の結果を画面で確認する。"
    answer = METHODS_ANSWER.replace(
        f"   {METHOD_ONE_RESULT}", f"   {METHOD_ONE_RESULT}\n   {register}"
    )
    assert dependent_steps(answer, {METHOD_ONE_STEP}) == [(5, 3, register)]
    # 方法 2 の操作を外しても、方法 3 の項目・事実の文は残す。
    assert dependent_steps(answer, {METHOD_TWO_STEP}) == []


def test_sequential_steps_mark_later_operations_but_not_facts() -> None:
    """項目の行に中身を書いた順の手順は、未確認の操作より後の項目の操作に警告の印を付け、項目の中の
    事実の段落には付けない（#2604・#2735・#2738）。"""
    note = "   - 訂正の記録は 5 年間保存されます。"
    answer = "\n".join([PREREQUISITE_STEP, FOLLOWING_STEPS[0], note, FOLLOWING_STEPS[1]])
    assert dependent_steps(answer, {PREREQUISITE_STEP}) == [
        (1, 0, FOLLOWING_STEPS[0]),
        (3, 0, FOLLOWING_STEPS[1]),
    ]
    result = _verdict(
        _claim(PREREQUISITE_STEP, "unsupported", "根拠に無い。"),
        *(_claim(step, "supported") for step in FOLLOWING_STEPS),
        _claim(note.strip(), "supported"),
    )
    published = publish_answer(answer, result)
    assert published.body == answer
    assert published.passage_marks[1:] == [
        _following_mark(step, "after_unverified_step") for step in FOLLOWING_STEPS
    ]


def test_named_steps_with_one_line_each_are_sequential() -> None:
    """名前の項目の下に中身が 1 行ずつの一覧は順の手順として、後の項目の操作を外す。項目の名前は、
    事実の段落が残れば残し、主張の段落をすべて外すときだけ外す（#2604・#2692・#2735）。"""
    check = "   - 対象が同じものであることを確認する。"
    fix = "   - 対象の情報を訂正する。訂正の記録は 5 年間保存されます。"
    merge = "   - 識別子を統合する。"
    answer = "\n".join(["1. **確認**", check, "2. **訂正**", fix, "3. **統合**", merge])
    dependents = dependent_steps(answer, {check.strip()})
    assert [text for _index, _begin, text in dependents] == [
        "- 対象の情報を訂正する。",
        "3. **統合**",
        merge.strip(),
    ]
    positions = frozenset((index, begin) for index, begin, _text in dependents)
    assert withhold_paragraphs(answer, {check.strip()}, positions) == (
        "1. **訂正**\n   訂正の記録は 5 年間保存されます。"
    )


# #2692 の合成の事例: 操作の名前だけを番号で並べた順序と、後半の操作の文。
ORDER_TITLE = "処理の順序"
ORDER_LABELS = ["1. **対象 A の属性訂正（記録修正）**", "2. **対象 A の同一性確認**"]
ORDER_FIX = "対象 A の属性を先に訂正する。"
ORDER_CHECK = "対象 A が同一であることを確認する。"
ORDER_NOTE = "訂正の記録は 5 年間保存されます。"
ORDER_ANSWER = "\n".join([ORDER_TITLE, *ORDER_LABELS, "", ORDER_FIX, ORDER_CHECK, "", ORDER_NOTE])


def _order_result(fix_status: str) -> dict[str, Any]:
    return _verdict(
        _claim(ORDER_TITLE, "supported"),
        *(_claim(label, "supported") for label in ORDER_LABELS),
        _claim(ORDER_FIX, fix_status, "根拠は同一性の確認を先にしている。"),
        _claim(ORDER_CHECK, "supported"),
        _claim(ORDER_NOTE, "supported"),
    )


def test_contradicted_operation_also_removes_its_numbered_name() -> None:
    """操作の文を矛盾で外したら、同じ操作を名前だけで示す番号つきの項目も外し、残りの番号を詰める
    （#2692。変更前は「1. **対象 A の属性訂正（記録修正）**」が実行の順の最初に残った）。"""
    published = publish_answer(ORDER_ANSWER, _order_result("contradicted"))

    assert published.body == "\n".join(
        [ORDER_TITLE, "1. **対象 A の同一性確認**", "", ORDER_CHECK, "", ORDER_NOTE]
    )
    # 名前の項目は外した内容の節に数えない（外した主張は操作の文の 1 件）。
    assert [item["text"] for item in published.removed_passages] == [ORDER_FIX]
    assert "属性訂正" not in published.text
    # 根拠で確かめられないだけなら、操作の文も名前の項目も本文に残す（#2738）。
    published = publish_answer(ORDER_ANSWER, _order_result("unsupported"))
    assert published.body == ORDER_ANSWER
    assert [item["text"] for item in published.passage_marks] == [ORDER_FIX]


def test_numbered_names_are_unchanged_when_the_operation_is_supported() -> None:
    """操作の文が根拠で確かめられれば、名前の項目も番号もそのまま（#2692）。"""
    published = publish_answer(ORDER_ANSWER, _order_result("supported"))
    assert published.text == ORDER_ANSWER
    assert published.withheld == {
        "removed": 0,
        "unverified": 0,
        "warnings": 0,
        "all": False,
        "listed": 0,
    }

    # ほかの（操作でない）段落だけを矛盾で外しても、名前の項目と番号は変えない。
    result = _verdict(
        _claim(ORDER_TITLE, "supported"),
        *(_claim(label, "supported") for label in ORDER_LABELS),
        _claim(ORDER_FIX, "supported"),
        _claim(ORDER_CHECK, "supported"),
        _claim(ORDER_NOTE, "contradicted", "根拠と矛盾。"),
    )
    body = publish_answer(ORDER_ANSWER, result).body
    assert body == "\n".join([ORDER_TITLE, *ORDER_LABELS, "", ORDER_FIX, ORDER_CHECK])


def test_only_the_main_operation_name_of_a_sentence_is_withheld() -> None:
    """1 つの文が 2 つの操作の名前を含むときは、文末の述語に近い名前（主の操作）だけを外す
    （#2692。前の操作は文の中の条件として出てくるだけ）。"""
    labels = ["1. **対象 A の同一性確認**", "2. **対象 A の属性訂正**"]
    sentence = "対象 A の属性を、同一性確認の前に訂正する。"
    answer = "\n".join([ORDER_TITLE, *labels, "", sentence])
    result = _verdict(
        _claim(ORDER_TITLE, "supported"),
        *(_claim(label, "supported") for label in labels),
        _claim(sentence, "contradicted", "順序が逆。"),
    )
    assert publish_answer(answer, result).body == "\n".join([ORDER_TITLE, labels[0]])


def test_chapter_and_explanatory_names_are_not_withheld_with_an_operation() -> None:
    """章題・説明の名前（外す操作の語と重ならない・短い名前）は外さない（#2692）。"""
    labels = ["1. **概要**", "2. **手続きの流れ**", "3. **記録の保存期間**", "4. **属性訂正**"]
    answer = "\n".join([*labels, "", ORDER_FIX])
    result = _verdict(
        *(_claim(label, "supported") for label in labels),
        _claim(ORDER_FIX, "contradicted", "順序が逆。"),
    )
    # 「属性訂正」は外す操作の名前。ほかの名前は残す。
    assert publish_answer(answer, result).body == "\n".join(labels[:3])
    for label, expected in [
        ("概要", None),
        ("手続きの流れ", None),
        ("訂正", None),
        ("属性訂正", 11),
        ("対象 A の属性訂正", 11),
        ("対象 B の属性訂正", None),
    ]:
        assert label_operation_end(label, ORDER_FIX) == expected, label
    assert list_item_label("1. **対象 A の属性訂正（記録修正）**") == "対象 A の属性訂正"
    assert list_item_label("1. 対象 A の属性を訂正する。") is None
    assert list_item_label("**対象 A の属性訂正**") is None


def test_heading_keeps_its_number_while_correct_content_remains() -> None:
    """見出しの下に正しい内容が残る項目は、見出しも番号も残す（下の内容を消さない。#2692）。
    中身をすべて外した見出しは消し、続く項目の番号を詰める。"""
    answer = "\n".join(
        [
            "1. **属性訂正**",
            "   - 対象 A の属性を先に訂正する。",
            "   - 訂正の記録は 5 年間保存されます。",
            "2. **記録の保存**",
            "   - 保存期間の満了後は記録を消去できます。",
        ]
    )
    quote = "- 対象 A の属性を先に訂正する。"
    assert withhold_paragraphs(answer, {quote}) == "\n".join(
        [
            "1. **属性訂正**",
            "   - 訂正の記録は 5 年間保存されます。",
            "2. **記録の保存**",
            "   - 保存期間の満了後は記録を消去できます。",
        ]
    )
    assert withhold_paragraphs(answer, {quote, "- 訂正の記録は 5 年間保存されます。"}) == (
        "1. **記録の保存**\n   - 保存期間の満了後は記録を消去できます。"
    )


@pytest.mark.parametrize(
    ("marks", "expected"),
    [
        (["１．", "２．", "３．"], ["１．", "２．"]),
        (["（1）", "（2）", "（3）"], ["（1）", "（2）"]),
        (["①", "②", "③"], ["①", "②"]),
    ],
)
def test_renumbering_keeps_the_number_style(marks: list[str], expected: list[str]) -> None:
    """番号を詰めるときは、全角の数字・括弧・丸数字の番号の形を保つ（#2692）。"""
    items = ["事実の 1 つ目です。", "事実の 2 つ目です。", "事実の 3 つ目です。"]
    answer = "\n".join(f"{mark}{item}" for mark, item in zip(marks, items, strict=True))
    kept = withhold_paragraphs(answer, {f"{marks[0]}{items[0]}"})
    assert kept == "\n".join(
        f"{mark}{item}" for mark, item in zip(expected, items[1:], strict=True)
    )


def test_numbers_are_not_renumbered_when_the_body_refers_to_them() -> None:
    """本文が番号で項目を指していれば（「手順 3 の後に」）、番号を詰めない（指す先が変わるため。
    #2692）。"""
    answer = (
        "1. 事実の 1 つ目です。\n2. 事実の 2 つ目です。\n3. 事実の 3 つ目です。\n"
        "手順 3 の後に連絡する。"
    )
    assert withhold_paragraphs(answer, {"1. 事実の 1 つ目です。"}) == (
        "2. 事実の 2 つ目です。\n3. 事実の 3 つ目です。\n手順 3 の後に連絡する。"
    )


def test_clarification_only_answer_without_evidence_has_no_notice(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    # cr-delete-account: 根拠を使わない確認の質問だけの回答に「確かめていません」を足さない。
    question = "\n".join(
        [
            "アカウントの削除の前に、次を確認させてください。",
            "所属部署の部門長の承認は得ていますか？（はい／いいえ）",
            "削除するのはどちらのアカウントですか？",
            "- 自分のアカウント",
            "- 部下のアカウント",
        ]
    )
    # 確認のツールを渡した Run の本文の聞き返しは 1 回差し戻される（#1881）。差し戻しの後も
    # 聞き返しなら、そのまま出す。
    _script(monkeypatch, [assistant_message(question)], [assistant_message(question)])
    created = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="アカウントを削除したい", agent_id=AGENT_ID),
        created_by_user_uuid=USER_UUID,
    )

    anyio.run(builtin_runtime.execute_run, created.id)

    run = runtime_repository.get_run(created.id)
    assert mcp.calls_of("rag_validate_answer") == []
    assert run_answer_text(run) == question
    content = _validation(run)
    assert (content["status"], content["reason"]) == ("skipped", "clarification_only")


@pytest.mark.parametrize("answer", CLARIFICATIONS.values(), ids=CLARIFICATIONS.keys())
def test_clarification_with_preface_and_answer_guidance_has_no_notice(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp, answer: str
) -> None:
    """#1362 の評価の確認の質問（前置きの文・答え方の案内の括弧つき）に「確かめていません」を
    足さず、回答の対応を needs_clarification にする（#1454）。"""
    # 確認のツールを渡した Run の本文の聞き返しは 1 回差し戻される（#1881）。
    _script(monkeypatch, [assistant_message(answer)], [assistant_message(answer)])
    created = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="アクセス権限を付与したい", agent_id=AGENT_ID),
        created_by_user_uuid=USER_UUID,
    )

    anyio.run(builtin_runtime.execute_run, created.id)

    run = runtime_repository.get_run(created.id)
    assert run_answer_text(run) == answer
    content = _validation(run)
    assert (content["status"], content["reason"]) == ("skipped", "clarification_only")
    assert _answer_outcome(run)["value"] == "needs_clarification"


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("## 確認手順", "heading"),
        ("**手順**", "heading"),
        # 「確認が必要な点」は最終の検証が足す節の見出し（#2738。unverified_section）。
        ("注意が必要な点：", "heading"),
        ("【portal-operations-manual.pdf, section 2. アカウント管理, page 1】", "citation"),
        ("- 出典：admin-guide.pdf 第3章", "citation"),
        ("（参照: security-policy.docx p.4）", "citation"),
        ("[運用マニュアル](https://example.com/manual.pdf)", "citation"),
        ("所属部署の部門長の承認は得ていますか？（はい／いいえ）", "question"),
        ("付与先の部署名を教えてください。", "question"),
        ("ライセンス費用については、資料に記載がありません。", "absence"),
        ("取得した資料からは、今の設定値は確かめられません。", "absence"),
        # 主張・操作の手順（外す対象のまま）。
        ("**30 日以内に申請してください**", None),
        ("## 管理画面で「削除」を押します", None),
        ("根拠: 契約書の第 5 条により、30 日前までに申し出る必要があります", None),
        ("削除できますが、よろしいですか？", None),
        ("ライセンス費用は月額 1,000 円で、資料には記載がありません。", None),
        ("資料には記載がありませんが、設定画面で削除できます。", None),
        ("管理画面で「削除」を押してください。", None),
        ("パスワードは 90 日ごとに変更します。", None),
    ],
    ids=[f"case{index}" for index in range(19)],
)
def test_passage_kind_is_conservative(text: str, kind: str | None) -> None:
    assert non_claim_passages(text).get(text) == kind


def test_unverified_section_keeps_points_but_not_operation_steps() -> None:
    answer = "\n".join(
        [
            "**資料で確認できず除いた内容**",
            "- 承認の要否は、資料に記載がありません。",
            "- 復元できる期限は確かめられていません",
            "- 管理画面で「削除」を押してください。",
        ]
    )

    kinds = non_claim_passages(answer)

    assert kinds == {
        "**資料で確認できず除いた内容**": "unverified_section",
        "- 承認の要否は、資料に記載がありません。": "absence",
        "- 復元できる期限は確かめられていません": "unverified_section",
    }
    # 最終の検証が足す 2 つの節（#2738）も同じ。語の一部に当たる見出しは節にしない。
    for heading in (REMOVED_HEADING, REVIEW_HEADING):
        kinds = non_claim_passages(f"**{heading}**\n- 申請の期限は 30 日です。")
        assert kinds == {
            f"**{heading}**": "unverified_section",
            "- 申請の期限は 30 日です。": "unverified_section",
        }
    assert non_claim_passages("**再確認が必要な点の一覧**").get("**再確認が必要な点の一覧**") == (
        "heading"
    )
    # 同じ文が別の場所で主張なら、主張として扱う。
    assert "パスワードは 90 日です。" not in non_claim_passages(
        "**資料で確認できず除いた内容**\nパスワードは 90 日です。\n\nパスワードは 90 日です。"
    )


EVIDENCE_REF = (
    "【証拠 ID: 03ac895f1d744a2b9eab4dd569eb68fc:"
    "b245283c90ea80e5d8aadd3283e6027f148113c0aa42c59bbf12923cc59ae01c:1】"
)


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        # 出典の位置のラベル（#1317。da-trial-account-expiry）。
        (
            "- セクション: 「サンプル業務ポータル 運用手順書 第3版」 → "
            "**2. 検証用アカウントの登録**",
            "citation",
        ),
        ("- ページ: 1  ", "citation"),
        ("- **根拠**：regional-report-guide-v2.pdf、2. 出力の手順（ページ1）", "citation"),
        # 表のセルに分かれた出典（da-trial-account-setup）。
        (f"| 「1. 前提」 （ページ1）{EVIDENCE_REF} |", "citation"),
        (
            "（*portal-operations-manual.pdf*、セクション「3. アクセス権限の付与」）【証拠1】",
            "citation",
        ),
        ("- 「サンプル業務ポータル 運用手順書 第3版」 6. アカウントの削除", "citation"),
        # 表の区切りの行・区切り線。
        ("|------|----------|------|", "table"),
        ("| :--- | ---: |", "table"),
        ("---", "table"),
        # 拒答の文（km-license-fee・km-password-policy）と、検索結果に該当が無かった文。
        ("推測で金額を示すことはできません。", "absence"),
        (
            "そのため、パスワードは何文字以上にすべきかについて根拠のある回答はできません。",
            "absence",
        ),
        (
            "- rag__rag_lookup_guides の検索結果でも、今回の質問に該当する業務ガイドは"
            "返ってきませんでした。",
            "absence",
        ),
        # 主張のまま（外す対象）。
        ("- ページ: 管理画面で「削除」を押してください", None),
        ("- 抜粋: 「…有効期限は最長 30 日です。」", None),
        ("- 「有効期限は最長 30 日です。」", None),
        ("- 「検証用」", None),
        ("| 締め日 | 毎月 10 日 |", None),
        ("この機能では、アカウントを削除することはできません。", None),
        ("利用者を検索してもヒットしません。", None),
        ("年間ライセンス費用は 120,000 円のため、それ以上の金額はお示しできません。", None),
        (f"| 2. 登録 | 1. 「利用者」を開く 2. 「追加」を押す | {EVIDENCE_REF} |", None),
    ],
    ids=[f"case{index}" for index in range(21)],
)
def test_citations_tables_and_refusals_are_not_claims(text: str, kind: str | None) -> None:
    # #1317: #1306 の判定から漏れて unassessed で外れ、回答の対応を誤らせていた段落。
    assert non_claim_passages(text).get(text.strip()) == kind


TABLE_HEADER = "| 手順 | 操作内容 | 根拠 |"
TABLE_SEPARATOR = "|------|----------|------|"
TABLE_ROW = "| 1. 前提確認 | 「ポータル管理者」ロールが要ります | 「1. 前提」（ページ1） |"


def test_table_header_is_structure_only_before_a_separator() -> None:
    answer = f"検証用アカウントの登録の手順です。\n\n{TABLE_HEADER}\n{TABLE_SEPARATOR}\n{TABLE_ROW}"
    kinds = non_claim_passages(answer)
    assert kinds[TABLE_HEADER] == "table"
    assert kinds[TABLE_SEPARATOR] == "table"
    # 表の本文の行は主張のまま。区切りの行が後ろに無い行は見出しにしない。
    assert TABLE_ROW not in kinds
    assert TABLE_HEADER not in non_claim_passages(f"{TABLE_HEADER}\n{TABLE_ROW}")
    # 本文の行をすべて外したら、表の見出しと区切りだけを本文として出さない。
    lead = "検証用アカウントの登録の手順です。"
    assert withhold_paragraphs(answer, {lead, TABLE_ROW}) == ""
    assert (
        withhold_paragraphs(answer, {TABLE_ROW}) == f"{lead}\n\n{TABLE_HEADER}\n{TABLE_SEPARATOR}"
    )


def test_table_and_location_lines_are_not_withheld() -> None:
    # da-trial-account-setup: 表の見出し・区切り・出典のセルが unassessed で外れ、
    # 本文から消えていた。
    citation = f"| 「1. 前提」 （ページ1）{EVIDENCE_REF} |"
    row = "| 1. 前提確認 | 本操作は「ポータル管理者」ロールを持つ利用者のみが実施可能です。"
    answer = "\n".join([TABLE_HEADER, TABLE_SEPARATOR, f"{row} {citation}"])
    result = _verdict(
        _claim(TABLE_HEADER, "unassessed", "テーブルのヘッダー。"),
        _claim(TABLE_SEPARATOR, "unassessed", "テーブルの区切り線。"),
        _claim(row, "supported", "根拠に記載。"),
        _claim(citation, "unassessed", "引用情報の記載。"),
    )

    published = publish_answer(answer, result)

    assert published.text == answer
    assert published.passage_marks == []


# 出典の行の事例（RAG と Agent の両方のテストが読む。#1370）。
CITATION_CASES_PATH = "platform/contracts/answer-passages/citation-lines.json"
CITATION_CASES: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[3] / CITATION_CASES_PATH).read_text("utf-8")
)


@pytest.mark.parametrize(
    "case", CITATION_CASES["citation_passages"], ids=lambda case: str(case["id"])
)
def test_citation_only_passages_are_not_claims(case: dict[str, str]) -> None:
    """定位子・根拠の ID・文書名と場所だけの行は出典の行（#1370）。"""
    text = case["passage"]
    assert passage_kind(text) == "citation"
    assert non_claim_passages(text).get(text) == "citation"


@pytest.mark.parametrize("case", CITATION_CASES["claim_passages"], ids=lambda case: str(case["id"]))
def test_passages_with_body_after_the_location_stay_claims(case: dict[str, str]) -> None:
    """場所・ラベルの後に本文が続く行は主張のまま確かめる（取りこぼさない。#1370）。"""
    text = case["passage"]
    assert passage_kind(text) is None
    assert text not in non_claim_passages(text)


@pytest.mark.parametrize("case", CITATION_CASES["answers"], ids=lambda case: str(case["id"]))
def test_citation_lines_left_unassessed_do_not_withhold_the_answer(case: dict[str, Any]) -> None:
    """#1335 の再評価の回答で、RAG が出典の行を unassessed にしても、回答を外さない（#1370）。"""
    answer = case["answer"]
    kinds = non_claim_passages(answer)
    assert {text: kinds.get(text) for text in case["citation_passages"]} == {
        text: "citation" for text in case["citation_passages"]
    }
    assert not set(case["claim_passages"]) & set(kinds)
    result = _verdict(
        *[_claim(text, "supported", "根拠に記載。") for text in case["claim_passages"]],
        *[_claim(text, "unassessed", "ロケータの提示のみ。") for text in case["citation_passages"]],
    )

    published = publish_answer(answer, result)

    assert published.text == answer
    assert published.passage_marks == []


# 見出しの名詞句・導入の文の事例（RAG と Agent の両方のテストが読む。#1469）。
HEADING_CASES_PATH = "platform/contracts/answer-passages/headings.json"
HEADING_CASES: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[3] / HEADING_CASES_PATH).read_text("utf-8")
)


@pytest.mark.parametrize(
    "case", HEADING_CASES["heading_passages"], ids=lambda case: str(case["id"])
)
def test_heading_phrases_and_lead_ins_are_headings(case: dict[str, str]) -> None:
    """「補足（…）」・「根拠」・「…は次のとおりです。」は見出し（#1469）。"""
    text = case["passage"]
    assert is_heading(text)
    assert passage_kind(text) == "heading"
    assert non_claim_passages(text).get(text) == "heading"


@pytest.mark.parametrize(
    "case", HEADING_CASES["numbered_heading_passages"], ids=lambda case: str(case["id"])
)
def test_numbered_headings_are_decided_by_the_following_line(case: dict[str, str]) -> None:
    """番号つきの見出しの行は、次の行が下の内容のときだけ見出し（#1481 / #1482）。

    終わりの語が一覧にある行（「1. 人事評価システムの重要度」）も、一覧に無い「…の…」の行も、
    単独の行と、番号の行が続く一覧の中の行は主張のまま確かめる。
    """
    text = case["passage"]
    for below in ("   - 資料の記載", "- 資料の記載", "  資料の記載", "→ 資料の記載"):
        answer = f"{text}\n\n{below}"
        assert list_heading_lines(answer) == {text}
        assert non_claim_passages(answer).get(text) == "heading"
    assert not is_heading(text)
    assert passage_kind(text) is None
    assert text not in non_claim_passages(text)
    listed = f"{text}\n9. 次の項目\n   - 資料の記載"
    assert text not in list_heading_lines(listed)
    assert text not in non_claim_passages(listed)
    assert list_heading_lines(f"{text}\n**次の節**") == set()


@pytest.mark.parametrize(
    "passage",
    [
        "- システム台帳（system-ledger.xlsx・シート「システム台帳」）",
        "- **システム台帳**（system-ledger.xlsx、シート「システム台帳」）",
    ],
    ids=["sheet-note", "bold-document"],
)
def test_bullet_document_with_file_note_is_a_citation(passage: str) -> None:
    """資料のファイル名の注記が付いた箇条書きの行は、次の行に関係なく出典の行（#1486）。

    e1467 の run_93a965a4・run_eff5f621 の「根拠」の行。RAG は下に字下げした行が続くときだけ見出しに
    する（`list_heading_lines`）ので、見出しの契約（headings.json）には入れず両方のテストで個別に
    確かめる。
    """
    assert passage_kind(passage) == "citation"
    for following in ("  - SYS-105 正式名: ドキュメントポータル / 重要度: B", "- 組織規程（1 頁）"):
        answer = f"**根拠**\n{passage}\n{following}"
        assert passage not in list_heading_lines(answer)
        assert non_claim_passages(answer).get(passage) == "citation"


def test_numbered_list_items_ending_with_heading_words_are_claims() -> None:
    """番号の行が続く一覧の項目は、終わりの語が見出しの語の一覧にあっても主張（#1482）。

    e1467 の run_ee5468f8 の「申請書に書く 4 項目」。#1469 の段落だけの判定では「1. 変更の内容と
    理由」「3. 切り戻しの手順」が見出しになり、主張として監査されなかった。同じ行でも、下に内容の
    行があれば見出し。
    """
    items = [
        "1. 変更の内容と理由",
        "2. 影響を受けるシステムと利用者",
        "3. 切り戻しの手順",
        "4. 作業の日時",
    ]
    answer = "**申請書に書く 4 項目**\n" + "\n".join(items) + "\n\n**条件によって加わる項目**"
    kinds = non_claim_passages(answer)
    assert list_heading_lines(answer) == set()
    assert kinds.get("**申請書に書く 4 項目**") == "heading"
    for item in items:
        assert not is_heading(item)
        assert passage_kind(item) is None
        assert item not in kinds
    below = "3. 切り戻しの手順\n   1. 旧版に戻す\n   2. 動作を確かめる"
    assert list_heading_lines(below) == {"3. 切り戻しの手順"}
    assert non_claim_passages(below).get("3. 切り戻しの手順") == "heading"
    assert "1. 旧版に戻す" not in non_claim_passages(below)


@pytest.mark.parametrize(
    "case", HEADING_CASES["bullet_heading_passages"], ids=lambda case: str(case["id"])
)
def test_bullet_headings_are_decided_by_the_following_line(case: dict[str, str]) -> None:
    """箇条書きの見出しの行は、次の行が下位の内容（深い字下げ・「→」）のときだけ見出し（#1486）。

    単独の行と、同じ字下げの箇条書きの行が続く一覧の項目（「ご確認いただきたい点」の「- 組織規程
    （ご担当部署の承認者）」）は、終わりの語が一覧にあっても主張のまま確かめる。
    """
    text = case["passage"]
    for below in ("   - 資料の記載", "  資料の記載", "→ 資料の記載"):
        answer = f"{text}\n\n{below}"
        assert list_heading_lines(answer) == {text}
        assert non_claim_passages(answer).get(text) == "heading"
    assert not is_heading(text)
    assert passage_kind(text) is None
    assert text not in non_claim_passages(text)
    for sibling in ("- 次の項目", "・次の項目", "1. 次の項目", "**次の節**"):
        listed = f"{text}\n{sibling}\n   - 資料の記載"
        assert text not in list_heading_lines(listed)
        assert text not in non_claim_passages(listed)


@pytest.mark.parametrize("case", HEADING_CASES["claim_passages"], ids=lambda case: str(case["id"]))
def test_short_statements_are_not_headings(case: dict[str, str]) -> None:
    """数・操作・言い切りを含む短い行と、値の名前だけの行は主張のまま確かめる（#1469）。

    次の行が下の内容でも見出しにしない（#1481）。
    """
    text = case["passage"]
    assert not is_heading(text)
    assert passage_kind(text) is None
    assert text not in non_claim_passages(text)
    below = f"{text}\n   - 資料の記載"
    assert list_heading_lines(below) == set()
    assert text not in non_claim_passages(below)


@pytest.mark.parametrize("case", HEADING_CASES["answers"], ids=lambda case: str(case["id"]))
def test_headings_left_unassessed_do_not_withhold_the_answer(case: dict[str, Any]) -> None:
    """#1362 の評価（e1467）の回答で、RAG が見出しを unassessed にしても回答を外さない（#1469）。"""
    answer = case["answer"]
    kinds = non_claim_passages(answer)
    assert {text: kinds.get(text) for text in case["heading_passages"]} == {
        text: "heading" for text in case["heading_passages"]
    }
    assert not set(case["claim_passages"]) & set(kinds)
    result = _verdict(
        *[_claim(text, "supported", "根拠に記載。") for text in case["claim_passages"]],
        *[
            _claim(text, "unassessed", "本文を見出しとして監査から除外できません。")
            for text in case["heading_passages"]
        ],
    )

    published = publish_answer(answer, result)

    assert published.text == answer
    assert published.passage_marks == []


def _answer_outcome(run: RunState) -> dict[str, Any]:
    [artifact] = [item for item in run.artifacts if item.kind == "answer"]
    outcome = artifact.content["outcome"]
    assert isinstance(outcome, dict)
    return outcome


def test_answer_artifact_records_the_outcome_after_the_validation(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    # 回答の対応（#1305）は、最終の検証の後に Control Plane が決めて成果物 answer に残す。
    run, _ = _searched_run(monkeypatch)

    assert _answer_outcome(run) == {
        "schema_version": 1,
        "value": "answered",
        "basis": "rag_search",
        "rag_outcome": "answered",
        "signals": [],
    }


def test_answer_artifact_records_a_clarification_and_a_refusal(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    # 根拠・データのツールの拒答（#1835。test_tool_grounding.py）の外の振る舞いを確かめる。
    monkeypatch.setattr(get_settings(), "agent_tool_grounding_required", False)
    del mcp
    _script(
        monkeypatch,
        [assistant_message("確認させてください。\n付与先は個別の利用者ですか、グループですか？")],
    )
    clarified = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="権限を付けたい", agent_id=AGENT_ID), created_by_user_uuid=USER_UUID
    )
    anyio.run(builtin_runtime.execute_run, clarified.id)
    outcome = _answer_outcome(runtime_repository.get_run(clarified.id))
    assert (outcome["value"], outcome["basis"]) == ("needs_clarification", "clarification_question")

    # 根拠を使わない回答は、注記を足す前のモデルの回答の段落で決める。
    _script(monkeypatch, [assistant_message("ライセンス費用は資料に記載がありません。")])
    refused = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="ライセンス費用は？", agent_id=AGENT_ID),
        created_by_user_uuid=USER_UUID,
    )
    anyio.run(builtin_runtime.execute_run, refused.id)
    outcome = _answer_outcome(runtime_repository.get_run(refused.id))
    assert (outcome["value"], outcome["basis"]) == ("insufficient_evidence", "answer_passages")


def test_answer_is_saved_without_an_outcome_when_it_cannot_be_decided(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(builtin_runtime, "answer_outcome", broken)
    run, _ = _searched_run(monkeypatch)

    assert run.status == RunStatus.COMPLETED
    [artifact] = [item for item in run.artifacts if item.kind == "answer"]
    # 対応は付けない（回答の注意書きは付ける。#2405）。
    assert artifact.content == {"text": ANSWER, "notice": DEFAULT_ANSWER_NOTICE_TEXT}


def test_outcome_is_recorded_when_the_validation_is_disabled(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", False)
    mcp.outputs["rag_search"] = {
        **deepcopy(DEFAULT_OUTPUTS["rag_search"]),
        "outcome": "needs_environment_data",
    }
    run, _ = _searched_run(monkeypatch)

    assert mcp.calls_of("rag_validate_answer") == []
    outcome = _answer_outcome(run)
    assert (outcome["value"], outcome["rag_outcome"]) == (
        "needs_environment_data",
        "needs_environment_data",
    )


# 段落の分け方の事例（RAG と Agent の両方のテストが読む。#1455）。
SENTENCE_CASES_PATH = "platform/contracts/answer-passages/sentence-splits.json"
SENTENCE_CASES: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[3] / SENTENCE_CASES_PATH).read_text("utf-8")
)


@pytest.mark.parametrize("case", SENTENCE_CASES["cases"], ids=lambda case: str(case["id"]))
def test_passages_are_not_split_inside_brackets(case: dict[str, Any]) -> None:
    """括弧の中の「。」で段落を分けず、太字の印・閉じ括弧だけの断片を作らない（#1455）。"""
    line = case["line"]
    spans = passage_spans(line)
    assert [text for _start, _end, text in spans] == case["passages"]
    # 位置は行の中の原文を指す。
    assert all(line[start:end] == text for start, end, text in spans)


# dev cmp-pms-severity（run_aeda6990aad14955bb88d957deb2b6e2）の回答の台帳の節（#1455）。
_LEDGER_SECTION = "\n".join(
    [
        "- システム台帳（system-ledger.xlsx、シート「システム台帳」）",
        "  - 行1〜3（表頭の説明）: 「重要度は A・B・C の 3 段階です。"
        "A が最も高く、B、C の順に低くなります。」",
    ]
)


def test_withholding_a_quoted_passage_leaves_no_fragment() -> None:
    """括弧の中で分けた前の半分だけを外し、閉じ括弧の残る断片を本文に残さない（#1455）。

    以前は「…3 段階です。」と「A が最も高く、…」」に分かれ、前の半分を外すと
    「A が最も高く、B、C の順に低くなります。」」が本文に残っていた。
    """
    quote = _LEDGER_SECTION.split("\n")[1].strip()
    assert [text for _s, _e, text in passage_spans(_LEDGER_SECTION.split("\n")[1])] == [quote]
    published = withhold_paragraphs(_LEDGER_SECTION, {quote})
    assert published is not None
    assert "A が最も高く" not in published
    assert "」" not in published.replace("「システム台帳」", "")


def test_a_label_with_a_note_in_brackets_is_a_heading() -> None:
    """ラベルの後ろの注記の括弧（「補足（資料の注記。同シート A1:A2）：」）は見出し（#1455）。

    業務支援 da-parameter-default（run_a96b17e4a37e4f1bbd195802a1247112）で、括弧の中の「。」で
    2 つに分かれた断片がどちらも unassessed になり、外した主張は無いのに回答の対応が
    conditional になっていた。
    """
    label = "補足（資料の注記。同シート A1:A2）："
    assert [text for _s, _e, text in passage_spans(label)] == [label]
    assert passage_kind(label) == "heading"
    # 注記が操作・述語の文なら見出しにしない。ラベルの後ろに本文が続く段落も主張のまま。
    assert passage_kind("注意（必ず削除してください。）：") is None
    assert passage_kind("補足（資料の注記。同シート A1:A2）：初期値は 30 日です。") is None
    result = _verdict(
        _claim(label, "unassessed", "本文を見出しとして監査から除外できません。"),
        _claim("- 初期値は 30 日です。", "supported", "根拠に記載。"),
    )
    answer = f"{label}\n- 初期値は 30 日です。"
    published = publish_answer(answer, result)
    assert published.text == answer
    assert published.passage_marks == []


# 「資料で確認できず除いた内容」の節の事例（RAG の品質評価のテストも読む。#1456）。
UNVERIFIED_SECTION_PATH = "platform/contracts/answer-passages/unverified-section.json"
UNVERIFIED_SECTION: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[3] / UNVERIFIED_SECTION_PATH).read_text("utf-8")
)


def test_the_section_headings_are_shared_with_the_rag_evaluation() -> None:
    """最終の検証が足す節の見出しは、RAG の品質評価が faithfulness から除く見出し
    （#1456・#2738）。"""
    assert UNVERIFIED_SECTION["headings"] == [REMOVED_HEADING, REVIEW_HEADING]
    answer = (
        "検証用アカウントの有効期限は最長 30 日です。\n\n延長は 90 日までできます。"
        "\n\n延長の申請は管理者が行います。"
    )
    result = _verdict(
        _claim("検証用アカウントの有効期限は最長 30 日です。", "supported", "根拠に記載。"),
        _claim("延長は 90 日までできます。", "contradicted", "延長は 60 日まで。"),
        _claim("延長の申請は管理者が行います。", "unsupported", "申請者の記載が無い。"),
    )
    published = publish_answer(answer, result).text
    # RAG は太字の見出しの行から次の見出しの前までを除く（節の中に見出しの形の行を足さない）。
    body, rest = published.split(f"\n\n**{REMOVED_HEADING}**\n\n")
    removed, review = rest.split(f"\n\n**{REVIEW_HEADING}**\n\n")
    assert body == "検証用アカウントの有効期限は最長 30 日です。\n\n延長の申請は管理者が行います。"
    heading_line = re.compile(r"(?:#{1,6}\s+.+|\*\*[^*\n]+\*\*)\s*[:：]?")
    for section in (removed, review):
        assert not any(heading_line.fullmatch(line.strip()) for line in section.split("\n"))
    for case in UNVERIFIED_SECTION["examples"]:
        kinds = non_claim_passages(case["answer"])
        for heading in UNVERIFIED_SECTION["headings"]:
            if f"**{heading}**" in case["answer"]:
                assert kinds.get(f"**{heading}**") == "unverified_section"
        # 節を除いた本文は、Agent が組み立てた回答の本文と同じ。
        assert case["answer"].startswith(case["body"])


def test_answer_trace_ids_are_the_searches_of_the_connection() -> None:
    """監査の判定を再利用する回答の記録は、その接続の rag_search の trace_id（#1527）。

    新しい呼び出しから順で、重複は除く。
    """
    first = deepcopy(DEFAULT_OUTPUTS["rag_search"])
    second = {**deepcopy(DEFAULT_OUTPUTS["rag_search"]), "trace_id": "rag-trace-2"}
    other = {**deepcopy(DEFAULT_OUTPUTS["rag_search"]), "trace_id": "other-trace"}
    steps = [
        _step("rag__rag_search", {"query": "q1"}, first),
        _step("rag2__rag_search", {"query": "q"}, other),
        _step("rag__rag_search", {"query": "q2"}, second),
        _step("rag__rag_search", {"query": "q1"}, deepcopy(first)),
    ]
    inputs = connection_check_inputs(steps, "rag__rag_search")
    assert inputs["answer_trace_ids"] == ["rag-trace-1", "rag-trace-2"]


def test_paragraphs_rag_did_not_adopt_as_supported_are_marked_with_the_reason() -> None:
    """RAG が監査の supported を採らなかった段落（別の対象の記載・表の別の行の値・写した原文が根拠に
    無い）は本文に残し、規則の検査は警告、写した原文が無いのは未確認の印にする（#2653・#2659・
    #2738）。"""
    own_step = "処理 A の一覧で対象を選び、取消ボタンを押します。"
    other_step = "エラー E10 が出た場合は、訂正画面で設定値を 5 に変更します。"
    swapped = "区分Ⅱでは選択肢の値 01 を選びます。"
    unquoted = "取消の後は、処理 A をもう一度実行します。"

    def not_adopted(quote: str, check: str, status: str = "unsupported") -> dict[str, Any]:
        claim = _claim(quote, status, "根拠の対象が違う。")
        return {**claim, "audit_status": "supported", "support_check": check}

    result = _verdict(
        _claim(own_step, "supported"),
        not_adopted(other_step, "target_mismatch"),
        not_adopted(swapped, "value_unbound"),
        not_adopted(unquoted, "quote_unmatched", "citation_error"),
    )
    answer = f"{own_step}\n\n{other_step}\n\n{swapped}\n\n{unquoted}"

    published = publish_answer(answer, result)

    assert published.body == answer
    assert [
        (item["text"], item["state"], item["reasons"][0]["code"])
        for item in published.passage_marks
    ] == [
        (other_step, "warning", "target_mismatch"),
        (swapped, "warning", "value_unbound"),
        (unquoted, "unverified", "quote_unmatched"),
    ]
    assert published.sections[0].splitlines()[3:] == [
        f"- 根拠は別の対象についての記載: 「{other_step}」",
        f"- 根拠の表の対応と違う可能性: 「{swapped}」",
        f"- 引用の原文が根拠に見当たらない: 「{unquoted}」",
    ]
    assert published.withheld == {
        "removed": 0,
        "unverified": 1,
        "warnings": 2,
        "all": False,
        "listed": 3,
    }


def test_paragraph_rag_found_against_the_table_is_removed_with_the_right_value() -> None:
    """RAG が根拠の表の対応と取り違えたとした段落（contradicted・value_unbound）は本文から外し、
    表の正しい対応を外した理由に出す（#2998）。"""
    kept = "区分Ⅰでは選択肢の値 01 を選びます。"
    swapped = "区分Ⅱでは選択肢の値 01 を選びます。"
    reason = "根拠の表では『区分Ⅱ』の値は『02』（『01』は別の行の値）です。"
    table_claim = {
        **_claim(swapped, "contradicted", reason),
        "audit_status": "supported",
        "support_check": "value_unbound",
    }
    published = publish_answer(
        f"{kept}\n\n{swapped}", _verdict(_claim(kept, "supported"), table_claim)
    )
    assert published.body == kept
    assert published.removed_passages == [
        {
            "text": swapped,
            "reasons": [{"code": "contradicted", "label": "根拠と矛盾", "detail": reason}],
        }
    ]
    assert published.passage_marks == []


def test_passage_rag_corrected_with_the_table_replaces_the_wrong_passage() -> None:
    """RAG が根拠の表の正しい値に直した段落（revised_text。#3005）は、外さずに直した文に置き換え、
    「根拠の表の値に直した」の印を付ける。"""
    kept = "区分Ⅰでは選択肢の値 01 を選びます。"
    swapped = "区分Ⅱでは選択肢の値 01 を選びます。"
    revised = "区分Ⅱでは選択肢の値 02 を選びます。"
    reason = "根拠の表の値に直しました（『区分Ⅱ』の値は『02』（『01』は別の行の値））。"
    table_claim = {
        **_claim(swapped, "contradicted", reason),
        "audit_status": "supported",
        "support_check": "table_corrected",
        "revised_text": revised,
    }
    published = publish_answer(
        f"{kept}\n\n{swapped}", _verdict(_claim(kept, "supported"), table_claim)
    )
    assert published.body == f"{kept}\n\n{revised}"
    assert published.removed_passages == []
    assert [
        (mark["text"], mark["state"], mark["reasons"][0]["code"])
        for mark in published.passage_marks
    ] == [(revised, "warning", "table_corrected")]
    assert published.passage_marks[0]["reasons"][0]["detail"] == reason


# 段落の印の契約の事例（#2738。RAG・Agent・UI のテストが読む）。
PASSAGE_MARKS: dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "platform/contracts/answer-passages/passage-marks.json"
    ).read_text("utf-8")
)


def _contract_verdict(case: dict[str, Any]) -> dict[str, Any]:
    """契約の事例の判定（印の理由から RAG の判定を作る。印・外した段落の無い段落は裏付けあり）。"""
    marks = {item["text"]: item for item in case["passage_marks"]}
    removed = {item["text"]: item for item in case["removed_passages"]}
    claims: list[dict[str, Any]] = []
    for line in case["answer"].split("\n"):
        for _start, _end, text in passage_spans(line):
            if text in removed:
                claims.append(_claim(text, "contradicted", removed[text]["reasons"][0]["detail"]))
            elif text in marks:
                reason = marks[text]["reasons"][0]
                claim = _claim(text, "unsupported", reason["detail"])
                if reason["code"] in {"target_mismatch", "value_unbound", "quote_unmatched"}:
                    claim.update(audit_status="supported", support_check=reason["code"])
                claims.append(claim)
            else:
                claims.append(_claim(text, "supported", "根拠に記載。"))
    findings = [
        {
            "check": "guide_steps" if item["code"] == "guide_check" else item["code"],
            "code": "step_order",
            "severity": "error",
            "message": item["detail"],
        }
        for item in case["answer_warnings"]
    ]
    return {**_verdict(*claims), "findings": findings}


@pytest.mark.parametrize("case", PASSAGE_MARKS["examples"], ids=lambda case: str(case["id"]))
def test_passage_marks_follow_the_contract(case: dict[str, Any]) -> None:
    """契約の事例（platform/contracts/answer-passages/passage-marks.json）と同じ本文・印・外した
    段落・回答の警告を返す（#2738）。契約の `published` は末尾の節の前の本文。"""
    published = publish_answer(case["answer"], _contract_verdict(case))

    assert published.body == case["published"]
    assert published.passage_marks == case["passage_marks"]
    assert published.removed_passages == case["removed_passages"]
    assert published.answer_warnings == case["answer_warnings"]


def test_reason_labels_and_states_follow_the_contract() -> None:
    """理由の code・状態・利用者に見せる文は契約の語彙と同じ（#2738）。"""
    contract = PASSAGE_MARKS["reason_codes"]
    assert set(PASSAGE_MARKS["states"]) == {"unverified", "warning"}
    for code, (state, label) in PASSAGE_REASONS.items():
        assert contract[code] == {"kind": "passage", "state": state, "label": label}, code
    for code, label in REMOVED_REASONS.items():
        assert contract[code] == {"kind": "removed", "label": label}, code
    for code, label in ANSWER_WARNING_REASONS.items():
        assert contract[code] == {"kind": "answer", "label": label}, code


def test_nested_item_label_does_not_add_review_warning(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    label = "- 照合すべき項目"
    fact = f"- {ANSWER}"
    unsupported = "延長は 90 日までできます。"
    answer = f"{label}\n  {fact}\n{unsupported}"
    mcp.outputs["rag_validate_answer"] = _verdict(
        _claim(label, "unsupported", "見出しだけ。"),
        _claim(fact, "supported", "根拠に記載。"),
        _claim(unsupported, "unsupported", "記載が無い。"),
    )
    run, _ = _searched_run(monkeypatch, answer=answer)
    validation = _validation(run)
    claims = {c["answer_quote"]: c for c in validation["result"]["claims"]}
    assert claims[label]["non_claim"] == "heading"
    assert [mark["text"] for mark in validation["passage_marks"]] == [unsupported]
    published = run_answer_text(run)
    assert published is not None
    section = published.split(f"**{REVIEW_HEADING}**", 1)[1]
    assert label not in section
    assert unsupported in section


def test_bibliography_and_table_intro_do_not_make_answer_conditional(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    source = "1. 「合成の記録」第2章 比較 「対象と条件」‑\u202fページ\u202f4"
    intro = "- 表「合成の集計」では、"
    answer = f"{ANSWER}\n{source}\n{intro}"
    mcp.outputs["rag_validate_answer"] = _verdict(
        _claim(ANSWER, "supported", "根拠に記載。"),
        _claim(source, "unsupported", "出典だけ。"),
        _claim(intro, "unsupported", "導入だけ。"),
    )
    run, _ = _searched_run(monkeypatch, answer=answer)
    result = _validation(run)
    assert result["passage_marks"] == []
    assert result["withheld"]["listed"] == 0
    assert run_answer_text(run) == answer
    assert _answer_outcome(run)["value"] == "answered"


@pytest.mark.parametrize(
    "case", HEADING_CASES["table_header_cases"], ids=lambda case: str(case["id"])
)
def test_table_header_contract_with_empty_cells(case: dict[str, Any]) -> None:
    from app.features.agent.answer_passages import table_header_lines

    assert table_header_lines(case["answer"]) == set(case["headers"])
    kinds = non_claim_passages(case["answer"])
    for header in case["headers"]:
        assert kinds[header] == "table"
    assert "| 値 | 10 | 20 |" not in kinds


@pytest.mark.parametrize(
    "heading", ["**上限は無制限**", "## 上限は無制限。確認してください。", "【上限は無制限】"]
)
def test_assertion_in_heading_markup_ends_the_previous_unverified_section(heading: str) -> None:
    claim = "- 上限は 30 件です。"
    answer = f"**確認が必要な点**\n- 未確認の項目\n\n{heading}\n{claim}"
    kinds = non_claim_passages(answer)
    assert heading not in kinds
    assert claim not in kinds
    assert kinds["- 未確認の項目"] == "unverified_section"


@pytest.fixture
def titled_mcp(monkeypatch: MonkeyPatch) -> Iterator[FakeProductMcp]:
    from dataclasses import replace

    original = FakeProductMcp._tool

    def titled(
        self: FakeProductMcp, name: str, model: type[BaseModel], *, read_only: bool = True
    ) -> McpTool:
        tool = original(self, name, model, read_only=read_only)
        return replace(tool, title="資料との照合") if name == "rag_validate_answer" else tool

    monkeypatch.setattr(FakeProductMcp, "_tool", titled)
    yield from _mcp(monkeypatch)


def test_control_plane_validator_records_its_display_title(
    monkeypatch: MonkeyPatch, titled_mcp: FakeProductMcp
) -> None:
    run, _ = _searched_run(monkeypatch)
    assert run.metadata["tool_titles"][VALIDATOR] == "資料との照合"


@pytest.mark.parametrize("heading", ["**出力**（記録済み）", "**結果** (補足)"])
def test_bold_heading_with_trailing_note_is_not_a_claim(heading: str) -> None:
    from app.features.agent.answer_validation import claim_paragraphs

    answer = heading + "\n\n\u200b\n\ufeff\n\n値は 7 です。"
    assert claim_paragraphs(answer) == ["値は 7 です。"]
    assert non_claim_passages(answer)[heading] == "heading"


@pytest.mark.parametrize(
    "text",
    ["**上限は 7 日**（設定済み）", "**設定**（上限は 7 日）", "**保存してください**（必須）"],
)
def test_trailing_note_does_not_hide_facts(text: str) -> None:
    from app.features.agent.answer_validation import claim_paragraphs

    assert claim_paragraphs(text) == [text]


def test_trailing_note_and_invisible_lines_do_not_receive_warning_marks(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    heading = "**出力**（記録済み）"
    fact = "値は 7 です。"
    mcp.outputs["rag_validate_answer"] = _verdict(
        _claim(heading, "unsupported", "根拠に記載なし。"),
        _claim("\u200b", "unsupported", "空白。"),
        _claim(fact, "unsupported", "値の裏付けなし。"),
    )
    run, _ = _searched_run(monkeypatch, answer=heading + "\n\u200b\n" + fact)
    content = _validation(run)
    assert [item["text"] for item in content["passage_marks"]] == [fact]


@pytest.mark.parametrize("validate", [True, False])
def test_answer_table_preserves_source_identifier_characters(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp, validate: bool
) -> None:
    """モデルが表のハイフンを変えても、保存と検証に渡す値は根拠と同じ文字にする。"""
    from copy import deepcopy

    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", validate)
    search = deepcopy(mcp.outputs["rag_search"])
    assert isinstance(search, dict)
    search["evidence"][0]["excerpt"] = "| id | value |\n|---|---|\n| row-a | 10 |\n| row‑b | 20 |"
    mcp.outputs["rag_search"] = search
    answer = (
        "| id | value |\n|---|---|\n| row‑a | 10 |\n| row‑b | 20 |\n\n"
        "一般の説明では word‑word の表記を保ちます。"
    )
    model = _script(
        monkeypatch,
        [
            function_call(
                "rag__rag_search", {"query": "資料の識別子を確認"}, call_id="c-identifiers"
            )
        ],
        [assistant_message(answer)],
    )
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(
            goal="資料に記載された識別子と値を表で示してください。", agent_id=AGENT_ID
        ),
        created_by_user_uuid=USER_UUID,
    )
    anyio.run(builtin_runtime.execute_run, run.id)
    finished = runtime_repository.get_run(run.id)
    assert finished.status == RunStatus.COMPLETED
    published = run_answer_text(finished) or ""
    assert "| row-a | 10 |" in published
    assert "| row‑b | 20 |" in published
    assert "word‑word" in published
    assert len(model.calls) == 2
    if validate:
        [checked] = mcp.calls_of("rag_validate_answer")
        assert "| row-a | 10 |" in checked["arguments"]["answer"]
        assert "row‑a" not in checked["arguments"]["answer"]


def test_ambiguous_identifier_is_reported_without_guessing(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    from copy import deepcopy

    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", False)
    search = deepcopy(mcp.outputs["rag_search"])
    assert isinstance(search, dict)
    search["evidence"][0]["excerpt"] = "| id |\n|---|\n| row-a |\n| row‑a |"
    mcp.outputs["rag_search"] = search
    _script(
        monkeypatch,
        [function_call("rag__rag_search", {"query": "識別子の確認"}, call_id="ambiguous")],
        [assistant_message("| id |\n|---|\n| row–a |")],
    )
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="資料の識別子を表で示してください。", agent_id=AGENT_ID),
        created_by_user_uuid=USER_UUID,
    )
    anyio.run(builtin_runtime.execute_run, run.id)
    finished = runtime_repository.get_run(run.id)
    assert "row–a" in (run_answer_text(finished) or "")
    validation = _validation(finished)
    assert validation["status"] == "unvalidated" and validation["valid"] is False
    assert validation["reason"] == "identifier_ambiguous"
    assert validation["identifier_check"] == {"corrected": 0, "ambiguous": 1}


@pytest.mark.parametrize("ambiguous", [True, False])
def test_recorded_output_uses_the_same_source_identifier_check(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp, ambiguous: bool
) -> None:
    from copy import deepcopy

    from app.features.agent.output_fields import AgentOutputField
    from app.features.agent.runtime import AgentProfilePatch

    search = deepcopy(mcp.outputs["rag_search"])
    assert isinstance(search, dict)
    search["evidence"][0]["excerpt"] = "row-a row‑a" if ambiguous else "row-a"
    mcp.outputs["rag_search"] = search
    runtime_repository.patch_agent(
        AGENT_ID, AgentProfilePatch(output_fields=[AgentOutputField(key="id", label="識別子")])
    )
    written = "row–a" if ambiguous else "row‑a"
    scripted = [
        [function_call("rag__rag_search", {"query": "識別子の確認"}, call_id="source")],
        [function_call("record_output", {"rows": [{"id": written}]}, call_id="record")],
    ]
    if ambiguous:
        # 候補を推測して保存せず、モデルへ返した誤りに従って原文の表記で呼び直す。
        scripted.append(
            [function_call("record_output", {"rows": [{"id": "row-a"}]}, call_id="record-correct")]
        )
    scripted.append([assistant_message("資料の値を記録しました。")])
    _script(monkeypatch, *scripted)
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="資料の識別子を記録してください。", agent_id=AGENT_ID, draft=True),
        created_by_user_uuid=USER_UUID,
    )
    anyio.run(builtin_runtime.execute_run, run.id)
    finished = runtime_repository.get_run(run.id)
    assert finished.status == RunStatus.COMPLETED
    [output] = [a.content for a in finished.artifacts if a.kind == "structured_output"]
    assert output["rows"] == [{"id": "row-a"}]
    recorded = [s for s in finished.steps if s.tool_call and s.tool_call.name == "record_output"]
    if ambiguous:
        assert recorded[0].tool_result is not None
        assert recorded[0].tool_result.error_code == "output.identifier_ambiguous"
    else:
        assert output["identifier_check"] == {"corrected": 1, "ambiguous": 0}


def test_falling_back_to_an_earlier_answer_preserves_identifiers_too(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    from copy import deepcopy
    from functools import partial

    from app.features.agent import tool_grounding

    search = deepcopy(mcp.outputs["rag_search"])
    assert isinstance(search, dict)
    search["evidence"][0]["excerpt"] = "row-a"
    mcp.outputs["rag_search"] = search
    original = "| id |\n|---|\n| row‑a |"
    run, _ = _searched_run(monkeypatch, answer=original)
    monkeypatch.setattr(tool_grounding, "hop_original_answer", lambda _: original)

    async def validate(run_id: str, answer: str, **_: Any) -> tuple[str, dict[str, Any]]:
        assert "row-a" in answer and "row‑a" not in answer
        return answer, {"status": "completed", "withheld": {"removed": 0, "unverified": 0}}

    monkeypatch.setattr(builtin_runtime, "_validate_final_answer", validate)
    result = anyio.run(
        partial(
            builtin_runtime._answer_before_hop_retry,
            run.id,
            "後の回答",
            {"status": "completed", "withheld": {"removed": 1}},
            rag_tools=True,
            data_scopes=None,
        )
    )
    assert result is not None
    assert result[0] == result[1] == original.replace("row‑a", "row-a")
    assert result[2]["identifier_check"] == {"corrected": 1, "ambiguous": 0}
