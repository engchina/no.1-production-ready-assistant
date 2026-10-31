"""業務ガイドの手順を段として引く（#2396）の決定論テスト（合成の fixture。RAG はスタブ）。

`rag_lookup_guides` の手順（検索の手がかり・参照の資料と頁）から、手順ごとの根拠を決定的に判定し、
根拠の無い手順があれば答える前に 1 回だけ戻すこと、手順を段として引く次の手（予算が足りなければ
まとめて引く案内）、根拠が別の文書を参照するときの手がかりを確かめる。
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
from app.features.agent.guide_steps import (
    UNRESOLVED_GUIDE_STEPS_HINT,
    guide_step_states,
    guide_steps_next_step,
    plan_from_lookup,
    step_evidence,
    unresolved_guide_steps,
    unresolved_guide_steps_message,
)
from app.features.agent.runtime import (
    AgentProfile,
    RunCreateRequest,
    RunState,
    RunStep,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.support_task import (
    DOCUMENT_REFERENCE_READ_HINT,
    build_support_task,
    guide_check_note,
    text_references,
)
from app.features.agent.tool_grounding import METADATA_KEY
from app.features.agent.tools import ToolCall, ToolResult
from app.settings import get_settings

SKILL_ID = "test2396-skill"
AGENT_ID = "test2396-agent"
USER_UUID = "11111111-2222-3333-4444-555555555555"
PROFILE = "sap-1"
GOAL = "設定 A を変えたいです。どうすればよいですか？"

STEP_A: dict[str, Any] = {
    "id": "s1",
    "title": "画面 A で設定を変える",
    "purpose": "",
    "depends_on": [],
    "allowed_tools": [],
    "retrieval_hints": ["画面 A"],
    "references": [],
    "evidence_requirements": [],
    "done_when": "",
}
STEP_B: dict[str, Any] = {
    **STEP_A,
    "id": "s2",
    "title": "画面 B で反映する",
    "depends_on": ["s1"],
    "retrieval_hints": ["画面 B 反映"],
    "references": [
        {
            "document_id": "doc-b",
            "title": "文書 B",
            "section_path": [],
            "page_ranges": [{"page_start": 4, "page_end": 6}],
        }
    ],
}


def _guide(decision: str = "answer", steps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    guide: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_lookup_guides"]["guides"][0])
    guide.update(
        guide_id="g-1",
        title="設定の変更",
        decision=decision,
        unknown_conditions=[],
        clarifications=[],
        steps=deepcopy(steps if steps is not None else [STEP_A, STEP_B]),
        branches=[],
        references=[],
        completion=[],
    )
    return guide


def _lookup(decision: str = "answer", steps: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    output: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_lookup_guides"])
    output["guides"] = [_guide(decision, steps)]
    return output


def _evidence(document_id: str, excerpt: str, *, page: int = 1) -> dict[str, Any]:
    item: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_retrieve_evidence"]["evidence"][0])
    item.update(document_id=document_id, chunk_id=f"{document_id}:{page}", excerpt=excerpt)
    item["locator"] = {**item["locator"], "page_start": page, "page_end": page}
    return item


def _step(name: str, arguments: dict[str, Any], output: dict[str, Any]) -> RunStep:
    return RunStep(
        run_id="run-2396",
        title=name,
        status="completed",
        tool_call=ToolCall(name=f"rag__{name}", arguments=arguments),
        tool_result=ToolResult(name=f"rag__{name}", success=True, output=output),
    )


def _retrieve_step(query: str, *items: dict[str, Any]) -> RunStep:
    return _step("rag_retrieve_evidence", {"query": query}, {"evidence": list(items)})


# ---- 手順と根拠の紐づけ（決定的） --------------------------------------------------------------


def test_plan_is_only_for_answering_guides() -> None:
    plan = plan_from_lookup(_lookup())
    assert plan is not None
    assert [step["id"] for step in plan["steps"]] == ["s1", "s2"]
    assert plan["steps"][1]["references"][0]["page_ranges"] == [{"page_start": 4, "page_end": 6}]
    assert plan_from_lookup(_lookup("clarify")) is None
    assert plan_from_lookup(_lookup(steps=[])) is None
    assert plan_from_lookup({"guides": []}) is None


def test_step_evidence_links_by_query_reference_page_and_text() -> None:
    plan = plan_from_lookup(_lookup())
    assert plan is not None
    steps = [
        _step("rag_lookup_guides", {"query": GOAL}, _lookup()),
        # 検索文に手順 A の手がかりが出る。
        _retrieve_step("設定 A 画面 A", _evidence("doc-a", "設定 A を変える。")),
        # 手順 B の参照の文書でも、頁の範囲の外は紐づけない。
        _retrieve_step("設定 A 反映", _evidence("doc-b", "別の説明", page=9)),
    ]
    assert step_evidence(plan, steps) == {"s1": ["doc-a:1"], "s2": []}
    # 参照の頁が重なる根拠と、本文に手がかりが出る根拠は手順 B に紐づく。
    on_page = [*steps, _retrieve_step("設定", _evidence("doc-b", "項目の説明", page=5))]
    assert step_evidence(plan, on_page)["s2"] == ["doc-b:5"]
    in_text = [*steps, _retrieve_step("設定", _evidence("doc-c", "最後に画面 B 反映を押す。"))]
    assert step_evidence(plan, in_text)["s2"] == ["doc-c:1"]


def test_unresolved_guide_steps_and_message() -> None:
    lookup = _step("rag_lookup_guides", {"query": GOAL}, _lookup())
    steps = [lookup, _retrieve_step("設定 A 画面 A", _evidence("doc-a", "設定 A を変える。"))]

    pending = unresolved_guide_steps(steps)

    assert [step["id"] for step in pending] == ["s2"]
    message = unresolved_guide_steps_message(pending)
    assert message.startswith(UNRESOLVED_GUIDE_STEPS_HINT)
    assert "手順 s2「画面 B で反映する」の根拠が未取得。" in message
    assert "検索の手がかり: 画面 B 反映。" in message
    assert "文書 B（document_id: doc-b、p.4-6）" in message
    # 根拠のツールを呼んでいない・業務ガイドを照合していない・確認の判断なら判定しない。
    assert unresolved_guide_steps([lookup]) == []
    assert unresolved_guide_steps(steps[1:]) == []
    clarify = _step("rag_lookup_guides", {"query": GOAL}, _lookup("clarify"))
    assert unresolved_guide_steps([clarify, steps[1]]) == []
    # 題名だけの手順は根拠との紐づけを決められないので数えない。
    title_only = {**STEP_A, "id": "s3", "retrieval_hints": []}
    plain = _step("rag_lookup_guides", {"query": GOAL}, _lookup(steps=[title_only]))
    assert unresolved_guide_steps([plain, steps[1]]) == []
    assert guide_step_states([plain, steps[1]])[0]["evidence"] is None


def test_guide_check_note_adds_steps_as_hops_and_combines_under_budget() -> None:
    note = guide_check_note(_lookup(), evidence_gathered=False, rag_calls_remaining=4)
    assert note is not None
    next_step = note["next_step"]
    assert next_step["action"] == "answer_with_guide"
    assert next_step["steps"] == [
        {"id": "s1", "title": "画面 A で設定を変える", "query": "画面 A"},
        {
            "id": "s2",
            "title": "画面 B で反映する",
            "query": "画面 B 反映",
            "references": [
                {
                    "document_id": "doc-b",
                    "title": "文書 B",
                    "page_ranges": [{"page_start": 4, "page_end": 6}],
                }
            ],
        },
    ]
    assert "業務ガイドの手順を段として順に引いてください" in next_step["instruction"]
    # 残る検索の回数が手順の数より少なければ、手順の語をまとめて 1 回で引く。
    tight = guide_check_note(_lookup(), rag_calls_remaining=1)
    assert tight is not None
    assert "手順の query をまとめて" in tight["next_step"]["instruction"]
    # 確認の判断には手順を足さない。
    clarify = guide_check_note(_lookup("clarify"))
    assert clarify is not None and "steps" not in clarify["next_step"]
    assert guide_steps_next_step(_lookup("handoff"), None) is None


def test_text_references_add_other_document_references() -> None:
    item = _evidence("doc-a", "詳しくは文書 B を参照。")
    item["references"] = [
        {"label": "第 2 章", "document_title": "文書 B", "section_path": [], "resolved": False},
        {"label": "第 3 章", "document_title": None, "section_path": ["3"], "resolved": True},
    ]

    references = text_references("rag_retrieve_evidence", {"evidence": [item]})

    assert references == [
        {
            "document_id": "doc-a",
            "file_name": item.get("file_name"),
            "reference": "第 2 章",
            "document_title": "文書 B",
            "how_to_read": DOCUMENT_REFERENCE_READ_HINT,
        }
    ]
    assert text_references("rag_search", {"evidence": [item]})[0]["document_title"] == "文書 B"


def test_support_task_state_keeps_step_evidence() -> None:
    steps = [
        _step(
            "rag_lookup_guides",
            {"query": GOAL, "search_answer_profile_id": PROFILE},
            _lookup(),
        ),
        _retrieve_step("設定 A 画面 A", _evidence("doc-a", "設定 A を変える。")),
    ]

    state = build_support_task(
        None,
        steps=steps,
        run_id="run-2396",
        thread_id=None,
        owner_user_uuid=USER_UUID,
        goal=GOAL,
        max_rag_calls_per_run=6,
        max_tool_calls_per_task=40,
    )

    assert state["guide"]["guide_id"] == "g-1"
    assert [(item["id"], item["evidence"]) for item in state["guide"]["steps"]] == [
        ("s1", True),
        ("s2", False),
    ]


# ---- 答える前に 1 回だけ戻す（組み込み Runtime） ----------------------------------------------


@pytest.fixture
def mcp(monkeypatch: MonkeyPatch) -> Iterator[FakeProductMcp]:
    def retrieve(argument: Any) -> dict[str, Any]:
        output: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_retrieve_evidence"])
        if "画面 B" in argument.query:
            output["evidence"] = [_evidence("doc-b", "画面 B で反映ボタンを押す。", page=5)]
        else:
            output["evidence"] = [_evidence("doc-a", "設定 A は画面 A で変える。")]
        return output

    fake = fake_product_mcp(
        monkeypatch,
        outputs={
            "rag_lookup_guides": lambda _argument: _lookup(),
            "rag_retrieve_evidence": retrieve,
        },
    )
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", False)
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name=SKILL_ID,
            instructions="手順は rag_retrieve_evidence で調べる。",
            mcp_requirements=[
                SkillMcpRequirement(
                    server_id="rag",
                    tool_names=["rag_search", "rag_retrieve_evidence", "rag_lookup_guides"],
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


def _run(goal: str = GOAL) -> RunState:
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal=goal, agent_id=AGENT_ID), created_by_user_uuid=USER_UUID
    )
    anyio.run(builtin_runtime.execute_run, run.id)
    return runtime_repository.get_run(run.id)


def _tool_output(model: ScriptedModel, call_index: int, call_id: str) -> dict[str, Any]:
    for item in model.calls[call_index].input:
        if isinstance(item, dict) and item.get("call_id") == call_id and "output" in item:
            loaded: dict[str, Any] = json.loads(str(item["output"]))
            return loaded
    raise AssertionError(f"{call_id} の結果がモデルの入力にありません")


def _retrieve(query: str, call_id: str) -> list[Any]:
    arguments = {"query": query, "search_answer_profile_id": PROFILE}
    return [function_call("rag__rag_retrieve_evidence", arguments, call_id=call_id)]


def test_answer_without_a_guide_step_evidence_is_sent_back_once(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    first = "画面 A で設定 A を変えます。"
    model = _script(
        monkeypatch,
        _retrieve("設定 A 画面 A", "c1"),
        [assistant_message(first)],
        _retrieve("設定 A 画面 B 反映", "c2"),
        [assistant_message("画面 A で設定 A を変え、画面 B で反映します。")],
    )

    run = _run()

    # Control Plane の照合の結果に、手順を段として引く次の手が付く。
    guide_check = _tool_output(model, 1, "c1")["guide_check"]
    assert [step["id"] for step in guide_check["next_step"]["steps"]] == ["s1", "s2"]
    assert len(model.calls) == 4
    users = [item for item in model.calls[2].input if isinstance(item, dict)]
    retry = [item for item in users if item.get("role") == "user"][-1]["content"]
    assert retry.startswith(UNRESOLVED_GUIDE_STEPS_HINT)
    assert "手順 s2「画面 B で反映する」" in retry
    assert first in retry
    assert run.metadata[METADATA_KEY] == {"hop_retries": 1, "guide_step_retry": True}
    answer = [item for item in run.artifacts if item.kind == "answer"][-1].content["text"]
    assert answer == "画面 A で設定 A を変え、画面 B で反映します。"


def test_guide_step_retry_is_not_repeated(monkeypatch: MonkeyPatch, mcp: FakeProductMcp) -> None:
    model = _script(
        monkeypatch,
        _retrieve("設定 A 画面 A", "c1"),
        [assistant_message("画面 A で設定 A を変えます。")],
        [assistant_message("画面 A で設定 A を変えます（画面 B は確かめられませんでした）。")],
    )

    run = _run()

    assert len(model.calls) == 3
    assert run.metadata[METADATA_KEY] == {"hop_retries": 1, "guide_step_retry": True}
