"""業務 Agent の出力の項目と `record_output`（#2549）。

モデルは SDK の `ScriptedModel` に差し替え、OCI へは接続しない。
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from typing import Any

import anyio
import pytest
from agents.testing import ScriptedModel, assistant_message, function_call
from pytest import MonkeyPatch
from security_support import client

from app.features.agent import builtin_runtime
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.output_fields import (
    OUTPUT_KIND,
    RECORD_OUTPUT_TOOL,
    AgentOutputField,
    output_input_schema,
    output_retried,
    validate_rows,
)
from app.features.agent.runtime import (
    AgentProfile,
    RunCreateRequest,
    RunStatus,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.tools import ExternalToolError, ToolDefinition, tool_registry

LOOKUP = "test2549_lookup"
SKILL_ID = "test2549-skill"
AGENT_ID = "test2549-agent"
PLAIN_AGENT_ID = "test2549-plain"
USER_UUID = "00000000-0000-0000-0000-000000002549"

FIELDS = [
    AgentOutputField(key="item", label="項目", required=True),
    AgentOutputField(key="amount", label="金額", type="number"),
    AgentOutputField(key="due", label="期日", type="date"),
    AgentOutputField(key="matched", label="一致", type="boolean", required=True),
]


def test_validate_rows_reports_each_field_error() -> None:
    with pytest.raises(ExternalToolError) as raised:
        validate_rows(
            FIELDS,
            {
                "rows": [
                    {"item": "A", "amount": "十", "due": "2026/10/01", "matched": "yes"},
                    {"amount": 3, "extra": 1},
                ]
            },
        )
    errors = {
        ".".join(str(part) for part in item["loc"]): item["message"]
        for item in raised.value.details["details"]["errors"]
    }
    assert errors == {
        "rows.0.amount": "数値で書いてください。",
        "rows.0.due": "日付は YYYY-MM-DD の形で書いてください。",
        "rows.0.matched": "true か false で書いてください。",
        "rows.1.extra": "出力の項目に無い key です。",
        "rows.1.item": "必須の項目です。",
        "rows.1.matched": "必須の項目です。",
    }
    rows = validate_rows(
        FIELDS, {"rows": [{"item": " A ", "amount": 10, "due": "2026-10-01", "matched": True}]}
    )
    assert rows == [{"item": "A", "amount": 10, "due": "2026-10-01", "matched": True}]
    with pytest.raises(ExternalToolError):
        validate_rows(FIELDS, {"rows": []})


def test_input_schema_follows_fields() -> None:
    schema = output_input_schema(FIELDS)
    items = schema["properties"]["rows"]["items"]
    assert items["required"] == ["item", "matched"]
    assert items["properties"]["amount"]["type"] == ["number", "null"]
    assert items["properties"]["matched"]["type"] == "boolean"
    assert "YYYY-MM-DD" in items["properties"]["due"]["description"]


def test_fields_are_validated_on_save() -> None:
    duplicated = client.post(
        "/api/agents",
        json={
            "id": "test2549-dup",
            "name": "Agent D",
            "output_fields": [{"key": "a", "label": "A"}, {"key": "a", "label": "B"}],
        },
    )
    bad_key = client.post(
        "/api/agents",
        json={
            "id": "test2549-bad",
            "name": "Agent E",
            "output_fields": [{"key": "A-1", "label": "A"}],
        },
    )
    assert duplicated.status_code == 422
    assert bad_key.status_code == 422


@pytest.fixture
def agents() -> Iterator[None]:
    tool_registry.register(
        ToolDefinition(
            name=LOOKUP,
            description="照合のテスト用ツール",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
        ),
        lambda _arguments, _context: {"value": 10},
    )
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name="照合のテストの Skill",
            instructions="値は test2549_lookup で調べる。",
            mcp_requirements=[SkillMcpRequirement(server_id="control-plane", tool_names=[LOOKUP])],
        )
    )
    runtime_repository.create_agent(
        AgentProfile(
            id=AGENT_ID,
            name="照合の業務 Agent",
            instructions="書類を照合する。",
            skill_ids=[SKILL_ID],
            output_fields=FIELDS,
        )
    )
    runtime_repository.create_agent(
        AgentProfile(id=PLAIN_AGENT_ID, name="項目の無い業務 Agent", skill_ids=[SKILL_ID])
    )
    try:
        yield
    finally:
        repository: Any = runtime_repository
        with repository._lock:  # noqa: SLF001 - テストの後始末
            for run_id in [
                run_id
                for run_id, run in repository._runs.items()
                if run.agent_id in {AGENT_ID, PLAIN_AGENT_ID}
            ]:
                repository._runs.pop(run_id)
        for agent_id in (AGENT_ID, PLAIN_AGENT_ID):
            with contextlib.suppress(KeyError, ValueError):
                runtime_repository.delete_agent(agent_id)
        with contextlib.suppress(KeyError, ValueError):
            skill_registry.remove(SKILL_ID)
        tool_registry._definitions.pop(LOOKUP, None)  # noqa: SLF001 - テスト用の登録を戻す
        tool_registry._handlers.pop(LOOKUP, None)  # noqa: SLF001


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


def _run(agent_id: str) -> str:
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="書類を照合して", agent_id=agent_id), created_by_user_uuid=USER_UUID
    )
    anyio.run(builtin_runtime.execute_run, run.id)
    return run.id


def _tool_names(model: ScriptedModel) -> set[str]:
    return {tool.name for tool in model.calls[0].tools}


ROW = {"item": "A", "amount": 10, "due": "2026-10-01", "matched": True}


@pytest.mark.parametrize("heading", ["**記録（record_output の結果）**", "**出力**（記録済み）"])
def test_record_output_is_validated_and_saved(
    monkeypatch: MonkeyPatch, agents: None, heading: str
) -> None:
    model = _script(
        monkeypatch,
        [function_call(LOOKUP, {"query": "A"}, call_id="call-l")],
        # 1 回目は必須の項目が欠けている（誤りがモデルに返り、直して呼び直す）。
        [function_call(RECORD_OUTPUT_TOOL, {"rows": [{"item": "A"}]}, call_id="call-r1")],
        [function_call(RECORD_OUTPUT_TOOL, {"rows": [ROW]}, call_id="call-r2")],
        [
            assistant_message(
                f"A は業務データと一致しました。\n\n{heading}\n\n| item | amount |\n| A | 10 |"
            )
        ],
    )

    run_id = _run(AGENT_ID)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    assert RECORD_OUTPUT_TOOL in _tool_names(model)
    [output] = [item for item in run.artifacts if item.kind == OUTPUT_KIND]
    assert output.content == {
        "columns": [
            {"name": "item", "label": "項目", "type": "text", "match": "exact"},
            {"name": "amount", "label": "金額", "type": "number", "match": "exact"},
            {"name": "due", "label": "期日", "type": "date", "match": "exact"},
            {"name": "matched", "label": "一致", "type": "boolean", "match": "exact"},
        ],
        "rows": [ROW],
    }
    # 1 回目の誤りは、項目ごとの違反としてモデルに返している。
    retry_input = json.dumps(model.calls[2].input, ensure_ascii=False)
    assert "argument_errors" in retry_input and "rows.0.matched" in retry_input
    assert not output_retried(run.metadata)
    assert run.metadata["tool_titles"][RECORD_OUTPUT_TOOL] == "出力の項目の記録"
    answer = next(a.content["text"] for a in run.artifacts if a.kind == "answer")
    assert heading not in answer
    assert "record_output" not in answer
    assert "| item |" not in answer


def test_agents_without_fields_get_no_record_tool(monkeypatch: MonkeyPatch, agents: None) -> None:
    model = _script(
        monkeypatch,
        [function_call(LOOKUP, {"query": "A"}, call_id="call-l")],
        [assistant_message("A は 10 です。")],
    )

    run_id = _run(PLAIN_AGENT_ID)

    assert runtime_repository.get_run(run_id).status == RunStatus.COMPLETED
    assert RECORD_OUTPUT_TOOL not in _tool_names(model)


def test_answer_without_record_is_sent_back_once(monkeypatch: MonkeyPatch, agents: None) -> None:
    model = _script(
        monkeypatch,
        [function_call(LOOKUP, {"query": "A"}, call_id="call-l")],
        [assistant_message("A は一致しました。")],
        # 差し戻しの後に記録して答え直す。
        [function_call(RECORD_OUTPUT_TOOL, {"rows": [ROW]}, call_id="call-r")],
        [assistant_message("A は業務データと一致しました。")],
    )

    run_id = _run(AGENT_ID)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    assert output_retried(run.metadata)
    assert [item for item in run.artifacts if item.kind == OUTPUT_KIND]
    resent = json.dumps(model.calls[2].input, ensure_ascii=False)
    assert RECORD_OUTPUT_TOOL in resent and "出力の項目" in resent


def test_send_back_happens_only_once(monkeypatch: MonkeyPatch, agents: None) -> None:
    model = _script(
        monkeypatch,
        [function_call(LOOKUP, {"query": "A"}, call_id="call-l")],
        [assistant_message("A は一致しました。")],
        [assistant_message("A は一致しました（記録しない）。")],
    )

    run_id = _run(AGENT_ID)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    assert output_retried(run.metadata)
    assert not [item for item in run.artifacts if item.kind == OUTPUT_KIND]
    assert len(model.calls) == 3


def test_fields_saved_by_patch_work_in_draft_runs(monkeypatch: MonkeyPatch, agents: None) -> None:
    """PATCH で保存した出力の項目でも、下書きの Run に record_output を渡せる（#2559）。"""
    patched = client.patch(
        f"/api/agents/{PLAIN_AGENT_ID}",
        json={"output_fields": [{"key": "item", "label": "項目", "required": True}]},
    )
    assert patched.status_code == 200, patched.text
    agent = next(item for item in runtime_repository.list_agents() if item.id == PLAIN_AGENT_ID)
    assert isinstance(agent.output_fields[0], AgentOutputField)
    model = _script(
        monkeypatch,
        [function_call(LOOKUP, {"query": "A"}, call_id="call-l")],
        [function_call(RECORD_OUTPUT_TOOL, {"rows": [{"item": "A"}]}, call_id="call-r")],
        [assistant_message("A を記録しました。")],
    )
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="記録して", agent_id=PLAIN_AGENT_ID, draft=True),
        created_by_user_uuid=USER_UUID,
    )
    anyio.run(builtin_runtime.execute_run, run.id)

    done = runtime_repository.get_run(run.id)
    assert done.status == RunStatus.COMPLETED
    assert RECORD_OUTPUT_TOOL in _tool_names(model)
    assert [item for item in done.artifacts if item.kind == OUTPUT_KIND]


@pytest.mark.parametrize(
    "heading", ["## 記録（record_output の結果）", "**記録（record_output）**"]
)
def test_internal_record_section_is_removed_without_hiding_next_section(heading: str) -> None:
    from app.features.agent.output_fields import without_output_record_sections

    text = "回答です。\n\n" + heading + "\n\n- 値の記録\n\n## 補足\n次の操作。"
    assert without_output_record_sections(text) == "回答です。\n\n## 補足\n次の操作。"


@pytest.mark.parametrize(
    "heading", ["## 記録された項目", "**記録**", "### 記録した項目：", "**記録内容**"]
)
def test_record_report_section_uses_configured_titles(heading: str) -> None:
    from app.features.agent.output_fields import without_output_record_sections
    from app.settings import Settings

    text = (
        "回答です。\n\n" + heading + "\n\n| 項目 | 値 |\n| --- | --- |\n| A | 7 |\n"
        "\n（record_output に記録済みです）\n\n## 補足\n次の操作。"
    )
    assert (
        without_output_record_sections(text, Settings().agent_output_record_section_titles)
        == "回答です。\n\n## 補足\n次の操作。"
    )


def test_report_title_is_not_removed_inside_a_code_fence() -> None:
    from app.features.agent.output_fields import without_output_record_sections

    text = "回答です。\n\n```md\n## 記録\n記録の例。\n```\n\n## 別の節\n残す。"
    assert without_output_record_sections(text, ["記録"]) == text


def test_unconfigured_heading_remains() -> None:
    from app.features.agent.output_fields import without_output_record_sections

    text = "回答です。\n\n## 記録\n文書で定めた記録の規則。"
    assert without_output_record_sections(text) == text


def test_record_report_titles_can_be_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.features.agent.output_fields import without_output_record_sections
    from app.settings import Settings

    monkeypatch.setenv("AGENT_OUTPUT_RECORD_SECTION_TITLES", '["記録報告"]')
    settings = Settings(_env_file=None)
    assert (
        without_output_record_sections(
            "回答です。\n\n## 記録報告\n記録済みの値。", settings.agent_output_record_section_titles
        )
        == "回答です。"
    )


@pytest.mark.parametrize("heading", ["**記録内容**（記録済み）", "## 記録内容（記録済み）"])
def test_record_report_note_does_not_reach_answer_validation(heading: str) -> None:
    from app.features.agent.output_fields import without_output_record_sections

    text = "回答です。\n\n" + heading + "\n- 項目 A: 7\n\n## 補足\n値は 8 です。"
    assert (
        without_output_record_sections(text, ["記録内容（記録済み）"])
        == "回答です。\n\n## 補足\n値は 8 です。"
    )


def test_recorded_output_title_uses_the_shared_section_filter() -> None:
    from app.features.agent.output_fields import without_output_record_sections
    from app.settings import Settings

    answer = "本文。\n\n**出力**（記録済み）\n- 項目 A: 7\n\n## 補足\n値は 8 です。"
    assert (
        without_output_record_sections(answer, Settings().agent_output_record_section_titles)
        == "本文。\n\n## 補足\n値は 8 です。"
    )
