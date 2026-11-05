"""段ごとの保存値の反映・分離・差し込み口の契約。"""

import asyncio

import pytest
from rag_engine.resources.runtime import (
    bind_prompt_overrides,
    resolve_prompt,
    saved_prompt_overrides,
)

from app.api.routes.settings import _answer_prompts_data
from app.rag.answer_prompts import STAGE_PROMPT_REFS, default_prompt, validate_prompt
from app.rag.rerank_context_experiment import judge_prompt_sha256


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
