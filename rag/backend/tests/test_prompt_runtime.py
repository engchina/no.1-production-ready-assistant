"""段ごとの保存値の反映・分離・差し込み口の契約。"""

import asyncio

import pytest
from rag_engine.resources.runtime import (
    bind_prompt_overrides,
    resolve_prompt,
    saved_prompt_overrides,
)

from app.api.routes.settings import _answer_prompts_data
from app.rag.answer_judge_calibration import judge_prompt_sha256
from app.rag.answer_prompts import STAGE_PROMPT_REFS, default_prompt, validate_prompt


def test_stage_editor_displays_saved_instruction_and_builtin_default() -> None:
    key = "audit.factSystem"
    custom = "## Step 1: 根拠を確認する\n合成の確認指示。"
    data = _answer_prompts_data({key: {"content": custom, "updated_at": None}})
    editor = next(prompt for prompt in data.prompts if prompt.key == key)
    stage = next(stage for stage in data.stages if stage.id == "audit")
    part = next(part for part in stage.prompts if part.id == "factSystem")
    assert editor.content == part.content == custom
    assert part.prompt_key == key
    assert editor.default_content == default_prompt(key)
    assert editor.customized


@pytest.mark.asyncio
async def test_saved_instructions_are_isolated_between_tasks_and_threads() -> None:
    ready = asyncio.Event()
    count = 0

    async def request(value: str) -> tuple[str, str]:
        nonlocal count
        with bind_prompt_overrides({"evaluation.coverage": value}):
            count += 1
            if count == 2:
                ready.set()
            await ready.wait()
            direct = resolve_prompt("evaluation.coverage", "builtin")
            threaded = await asyncio.to_thread(resolve_prompt, "evaluation.coverage", "builtin")
            return direct, threaded

    assert list(await asyncio.gather(request("first"), request("second"))) == [
        ("first", "first"),
        ("second", "second"),
    ]
    assert resolve_prompt("evaluation.coverage", "builtin") == "builtin"
    assert saved_prompt_overrides() is None


def test_prompt_reset_restores_builtin_and_changes_evaluation_identity() -> None:
    before = judge_prompt_sha256()
    with bind_prompt_overrides({"evaluation.coverage": "## Step 1: 合成の判定"}):
        assert judge_prompt_sha256() != before
    assert judge_prompt_sha256() == before


@pytest.mark.parametrize("key", STAGE_PROMPT_REFS)
def test_builtin_stage_prompts_pass_placeholder_validation(key: str) -> None:
    validate_prompt(key, default_prompt(key))


def test_stage_template_requires_original_input_tokens() -> None:
    key = "expansion.template"
    default = default_prompt(key)
    validate_prompt(key, default)
    with pytest.raises(ValueError, match="placeholder"):
        validate_prompt(key, "## Step 1: 入力の差し込み口を削除した指示")


def test_only_confirmed_branch_notes_are_rendered_from_the_guide() -> None:
    from rag_engine.generation.grounded import guide_notice_lines
    from rag_engine.retrieval.guide_steps import GuidePlan

    from app.rag.support_guide_runtime import guide_notices, match_guide
    from app.schemas.support_guide import SupportGuideContent

    content = SupportGuideContent.model_validate(
        {
            "title": "操作 A の進め方",
            "goal": {"match_terms": ["操作 A"], "expected_result": "操作を完了する"},
            "conditions": [
                {
                    "id": "mode",
                    "label": "区分",
                    "allowed_values": ["区分甲", "区分乙"],
                    "required": True,
                    "question": "区分は A と B のどちらですか。",
                }
            ],
            "steps": [{"id": "a", "title": "設定を開く"}, {"id": "b", "title": "担当者と確認する"}],
            "branches": [
                {
                    "id": "a",
                    "when": {"condition_id": "mode", "operator": "equals", "values": ["区分甲"]},
                    "goto_step": "a",
                    "note": "操作を実施してください。",
                },
                {
                    "id": "b",
                    "when": {"condition_id": "mode", "operator": "equals", "values": ["区分乙"]},
                    "goto_step": "b",
                    "note": "担当者に確認を依頼してください。",
                },
            ],
        }
    )
    for conditions, expected in [
        ({}, []),
        ({"mode": "区分甲"}, ["操作を実施してください。"]),
        ({"mode": "区分乙"}, ["担当者に確認を依頼してください。"]),
    ]:
        match = match_guide([("g", 1, content)], "操作 A を行いたい", conditions)
        assert match is not None
        notices = guide_notices(match)
        assert notices["branch_notes"] == expected
        plan = GuidePlan.from_payload("g", 1, [{"id": "a", "title": "設定を開く"}], notices=notices)
        assert plan is not None
        assert guide_notice_lines("", [], branch_notes=plan.branch_notes) == expected


def test_saved_search_template_is_used_by_runtime_prompt_builder() -> None:
    from rag_engine.generation.query_prompts import _build_query_routing_prompt

    original = _build_query_routing_prompt("合成の質問")
    with bind_prompt_overrides(
        {"routing.template": "## Step 1: 保存した指示\n{{question}}\n{{task_contract}}"}
    ):
        actual = _build_query_routing_prompt("合成の質問")
        assert "保存した指示" in actual
        assert "合成の質問" in actual
        assert "{{question}}" not in actual
    assert _build_query_routing_prompt("合成の質問") == original
