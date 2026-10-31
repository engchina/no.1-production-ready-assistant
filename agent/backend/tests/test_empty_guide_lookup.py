"""業務ガイドの照合が 0 件のとき（#2504）の決定論テスト（合成の fixture。RAG はスタブ）。

当たる業務ガイドが無い照合は資料の根拠を調べた呼び出しに数えず、資料を検索せずに「資料に無い」と
答えた回答を差し戻すこと、照合の結果に資料の根拠を集める次の手が付くこと、当たる業務ガイドを
返した照合（引き継ぎ・確認の判断を含む）の扱いは変わらないことを確かめる。
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from copy import deepcopy
from typing import Any

import anyio
import pytest
from agents.testing import ScriptedModel, assistant_message, function_call
from mcp_support import DEFAULT_OUTPUTS, FakeProductMcp, fake_product_mcp
from pytest import MonkeyPatch

from app.features.agent import builtin_runtime
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.runtime import (
    AgentProfile,
    RunCreateRequest,
    RunState,
    RunStep,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.support_task import NO_GUIDE_HINT, guide_check_note
from app.features.agent.tool_grounding import (
    METADATA_KEY,
    REFUSAL_HEAD,
    grounding_attempted,
    grounding_used,
)
from app.features.agent.tools import ToolCall, ToolResult
from app.settings import get_settings

SKILL_ID = "test2504-skill"
AGENT_ID = "test2504-agent"
USER_UUID = "11111111-2222-3333-4444-555555552504"
PROFILE = "profile-2504"
GOAL = "設定 Y を構成する手順を教えてください。"
ABSENCE = "業務ガイドが見つからないため、資料上で確認できる手順はありません。"


def _empty_lookup() -> dict[str, Any]:
    output: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_lookup_guides"])
    output["guides"] = []
    return output


def _lookup_step(output: dict[str, Any], *, success: bool = True) -> RunStep:
    return RunStep(
        run_id="run-2504",
        title="rag_lookup_guides",
        status="completed" if success else "failed",
        tool_call=ToolCall(name="rag__rag_lookup_guides", arguments={"query": "設定 Y"}),
        tool_result=ToolResult(
            name="rag__rag_lookup_guides",
            success=success,
            output=output if success else None,
            error=None if success else "失敗",
            error_code=None if success else "mcp.tool_error",
        ),
    )


def test_empty_guide_lookup_does_not_count_as_grounding() -> None:
    empty = _lookup_step(_empty_lookup())
    assert not grounding_attempted([empty])
    assert not grounding_used([empty])
    found = _lookup_step(deepcopy(DEFAULT_OUTPUTS["rag_lookup_guides"]))
    assert grounding_attempted([found])
    assert grounding_used([found])
    # 呼び出しの失敗は従来どおり「呼ぼうとした」に数える（結果を得たには数えない）。
    failed = _lookup_step({}, success=False)
    assert grounding_attempted([failed])
    assert not grounding_used([failed])


def test_empty_guide_lookup_by_the_model_gets_the_next_step() -> None:
    note = guide_check_note(_empty_lookup(), evidence_gathered=False)
    assert note is not None
    assert note["next_step"]["instruction"] == NO_GUIDE_HINT
    assert note["next_step"]["reason"] == "no_guide"
    # Control Plane の照合（根拠を集めた後）では足さない。
    assert guide_check_note(_empty_lookup()) is None


# ---- 組み込み Runtime ---------------------------------------------------------------------------


@pytest.fixture
def mcp(monkeypatch: MonkeyPatch) -> Iterator[FakeProductMcp]:
    fake = fake_product_mcp(monkeypatch, outputs={"rag_lookup_guides": _empty_lookup()})
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", False)
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name=SKILL_ID,
            instructions="手順は rag_lookup_guides と rag_retrieve_evidence で調べる。",
            mcp_requirements=[
                SkillMcpRequirement(
                    server_id="rag",
                    tool_names=["rag_lookup_guides", "rag_retrieve_evidence"],
                )
            ],
        )
    )
    runtime_repository.create_agent(AgentProfile(id=AGENT_ID, name=AGENT_ID, skill_ids=[SKILL_ID]))
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


def _run() -> RunState:
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal=GOAL, agent_id=AGENT_ID), created_by_user_uuid=USER_UUID
    )
    anyio.run(builtin_runtime.execute_run, run.id)
    return runtime_repository.get_run(run.id)


def _lookup(call_id: str) -> list[Any]:
    arguments = {"query": "設定 Y 手順", "search_answer_profile_id": PROFILE}
    return [function_call("rag__rag_lookup_guides", arguments, call_id=call_id)]


def _retrieve(call_id: str) -> list[Any]:
    arguments = {"query": "設定 Y 手順", "search_answer_profile_id": PROFILE}
    return [function_call("rag__rag_retrieve_evidence", arguments, call_id=call_id)]


def _tool_output(model: ScriptedModel, call_index: int, call_id: str) -> dict[str, Any]:
    for item in model.calls[call_index].input:
        if isinstance(item, dict) and item.get("call_id") == call_id and "output" in item:
            loaded: dict[str, Any] = json.loads(str(item["output"]))
            return loaded
    raise AssertionError(f"{call_id} の結果がモデルの入力にありません")


def _answer(run: RunState) -> str:
    return str([item for item in run.artifacts if item.kind == "answer"][-1].content["text"])


def test_absence_answer_after_an_empty_guide_lookup_is_sent_back_to_search(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    final = "根拠付き回答"
    model = _script(
        monkeypatch,
        _lookup("c1"),
        [assistant_message(ABSENCE)],
        _retrieve("c2"),
        [assistant_message(final)],
    )

    run = _run()

    # 照合の結果に、資料の根拠を集める次の手が付く。
    assert _tool_output(model, 1, "c1")["next_step"]["instruction"] == NO_GUIDE_HINT
    assert len(model.calls) == 4
    assert run.metadata[METADATA_KEY]["retries"] == 1
    assert mcp.calls_of("rag_retrieve_evidence")
    assert _answer(run) == final


def test_absence_answer_without_searching_after_the_retry_is_refused(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    _script(
        monkeypatch,
        _lookup("c1"),
        [assistant_message(ABSENCE)],
        [assistant_message(ABSENCE)],
    )

    run = _run()

    assert run.metadata[METADATA_KEY]["refused"] is True
    assert _answer(run).startswith(REFUSAL_HEAD)


def test_handoff_guide_answer_is_not_sent_back(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    lookup: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_lookup_guides"])
    lookup["guides"][0]["decision"] = "handoff"
    mcp.outputs["rag_lookup_guides"] = lookup
    handoff = "この手続きは担当の窓口へお問い合わせください。"
    model = _script(monkeypatch, _lookup("c1"), [assistant_message(handoff)])

    run = _run()

    assert len(model.calls) == 2
    assert METADATA_KEY not in run.metadata
    assert _answer(run) == handoff
