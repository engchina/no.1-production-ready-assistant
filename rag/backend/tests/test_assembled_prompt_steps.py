"""組み立てた後にモデルへ渡す指示の Step の連番を確かめる(#3756)。

部品ごとの既定値の検査(tests/test_answer_prompt_stages.py)とは別に、実際の経路で組み立てた
system / user の指示を取り出し、既定値と保存済みの上書き(合成の内容)の両方で次を確かめる。

- `## Step N:` の見出しが 1 から欠けず重ならずに続く。
- 保存した上書きの文が組み立てた指示に入る(続きの部品は Step の番号だけが振り直される)。
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from rag_engine.adapters.oci import _render_prompt
from rag_engine.generation.citation_wire import generation_draft_wire_model
from rag_engine.generation.grounded import audit_system_prompt
from rag_engine.knowledge.prompt_files import IMAGE_RETRIEVAL_PROMPT_KEY
from rag_engine.models.llm import GroundedDraft
from rag_engine.parsing.vision_prompt_rules import refine_image_retrieval_prompt
from rag_engine.resources.runtime import bind_prompt_overrides

from app.config import Settings
from app.rag.answer_engine import AnswerEngine
from app.rag.answer_prompts import default_prompt, prompt_overrides, validate_prompt
from app.schemas.search import SearchRequest
from tests.test_answer_assumptions import QUESTION, prepared
from tests.test_answer_engine import FakeGenAi, FakeOracle, _fake_llm
from tests.test_vision import FakeReader, PromptOracle, _attach, _figure_extraction, _pipeline

STEP_HEADING = re.compile(r"^## Step (\d+):", re.M)


def _steps(text: str) -> list[int]:
    return [int(number) for number in STEP_HEADING.findall(text)]


def assert_continuous_steps(text: str) -> None:
    steps = _steps(text)
    assert steps, "Step の見出しがありません"
    assert steps == list(range(1, len(steps) + 1)), steps


def _without_step_numbers(text: str) -> str:
    return STEP_HEADING.sub("## Step N:", text)


def assert_saved_text_is_sent(saved: dict[str, str], assembled: str) -> None:
    """保存した文が入る。続きの部品は番号だけ振り直すので、番号を除いて比べる。"""
    for key, content in saved.items():
        assert _without_step_numbers(content.strip()) in _without_step_numbers(assembled), key


def _three_steps(name: str) -> str:
    return (
        f"## Step 1: 合成の{name}の役割を確認する\n"
        f"合成の{name}の入力を読む。\n\n"
        f"## Step 2: 合成の{name}を判断する\n"
        "根拠のある内容だけを使う。\n\n"
        f"## Step 3: 合成の{name}を返す\n"
        "決められた形で返す。\n"
    )


def _seven_steps(name: str) -> str:
    return "\n".join(
        f"## Step {number}: 合成の{name}の手順 {number}\n合成の{name}の内容 {number}。\n"
        for number in range(1, 8)
    )


GENERATION_SYSTEM_3 = _three_steps("回答")
GENERATION_SYSTEM_7 = _seven_steps("回答")
ASSUMPTION_SAVED = (
    "\n## Step 6: 合成の前提を明記する\n"
    "- 未確定の条件は前提として示す。\n"
    "- 選択肢を事実として選ばない。\n"
)


def _validated(saved: dict[str, str]) -> dict[str, str]:
    """画面の保存と同じ検証を通る内容だけを上書きに使う。"""
    for key, content in saved.items():
        validate_prompt(key, content)
    return saved


@pytest.mark.parametrize(
    "saved",
    [
        {},
        {"generation.assumptionSystem": ASSUMPTION_SAVED},
        {"generation.system": GENERATION_SYSTEM_3},
        {"generation.system": GENERATION_SYSTEM_7},
        {"generation.system": GENERATION_SYSTEM_3, "generation.assumptionSystem": ASSUMPTION_SAVED},
    ],
    ids=["defaults", "assumption", "system-3-steps", "system-7-steps", "both"],
)
async def test_generation_system_with_assumptions_keeps_continuous_steps(
    monkeypatch: pytest.MonkeyPatch, saved: dict[str, str]
) -> None:
    """生成の system と未確定の前提の追加部品を、回答の経路で組み立てた後に確かめる。"""
    import rag_engine.adapters.oci as engine_oci

    systems: list[str] = []

    def llm(system: str, prompt: str, settings: Any, schema: type, **options: Any) -> Any:
        if schema in (GroundedDraft, generation_draft_wire_model()):
            systems.append(system)
        return _fake_llm(system, prompt, settings, schema, **options)

    monkeypatch.setattr(engine_oci, "parse_text_response", llm)
    oracle = FakeOracle()
    oracle.prompt_overrides = _validated(saved)
    await AnswerEngine(
        Settings(rag_answer_flow="standard_rag"),
        oracle=oracle,  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
        runtime_knowledge_payload=prepared("assume"),
    ).run(SearchRequest(query=QUESTION))
    assert systems
    for system in systems:
        assert_continuous_steps(system)
        assert_saved_text_is_sent(saved, system)


AUDIT_SAVED_3 = _three_steps("監査")
COMPACT_RULE_SAVED = "\n### 合成の理由の書き方\n\n- 通らなかった判定だけ理由を書く。\n"


@pytest.mark.parametrize("detailed", [True, False], ids=["detailed", "fact"])
@pytest.mark.parametrize("compact", [True, False], ids=["compact", "full"])
@pytest.mark.parametrize(
    "saved",
    [
        {},
        {"audit.detailedSystem": AUDIT_SAVED_3, "audit.factSystem": AUDIT_SAVED_3},
        {"audit.compactRule": COMPACT_RULE_SAVED},
        {
            "audit.detailedSystem": AUDIT_SAVED_3,
            "audit.factSystem": AUDIT_SAVED_3,
            "audit.compactRule": COMPACT_RULE_SAVED,
        },
    ],
    ids=["defaults", "system-3-steps", "compact-rule", "both"],
)
def test_audit_system_keeps_continuous_steps(
    saved: dict[str, str], compact: bool, detailed: bool
) -> None:
    """監査の本体と理由の規則の追加部品を、回答の経路と同じ上書きのスコープで組み立てる。"""
    with prompt_overrides(_validated(saved)):
        system = audit_system_prompt(compact_reasons=compact, detailed=detailed)
    assert_continuous_steps(system)
    used = {
        key: content
        for key, content in saved.items()
        if key == ("audit.detailedSystem" if detailed else "audit.factSystem")
        or (key == "audit.compactRule" and compact)
    }
    assert_saved_text_is_sent(used, system)


IMAGE_TEMPLATE_3 = (
    "## Step 1: 合成の画像を確認する\n合成の画像の目的を読む。\n\n"
    "## Step 2: 合成の入力を読む\n{{image_metadata}}\n\n"
    "## Step 3: 合成の説明を組み立てる\n見えている内容だけを書く。\n"
)
PICTURE_SAVED = "## Step 6: 合成の図を読む\n独立した図の文字を読む。"
TABLE_SAVED = "## Step 6: 合成の表を読む\n同じ行の画像を読む。"
RULES_SAVED = "### 合成の読取方針\nラベルの原文を保持する。"
SYSTEM_SAVED = (
    "## Step 1: 合成の画像説明を作る\n合成の役割。\n\n"
    "## Step 2: 合成の結果を返す\nJSON だけを返す。\n"
)

VISION_CASES = [
    {},
    {"picture_description.picture": PICTURE_SAVED, "picture_description.table": TABLE_SAVED},
    {"picture_description.rules": RULES_SAVED, "picture_description.system": SYSTEM_SAVED},
    {IMAGE_RETRIEVAL_PROMPT_KEY: IMAGE_TEMPLATE_3},
    {
        IMAGE_RETRIEVAL_PROMPT_KEY: IMAGE_TEMPLATE_3,
        "picture_description.picture": PICTURE_SAVED,
        "picture_description.table": TABLE_SAVED,
        "picture_description.rules": RULES_SAVED,
    },
]
VISION_IDS = ["defaults", "target", "rules-and-system", "template-3-steps", "all"]


@pytest.mark.parametrize("saved", VISION_CASES, ids=VISION_IDS)
async def test_ingestion_picture_prompt_keeps_continuous_steps(saved: dict[str, str]) -> None:
    """取込の経路(保存値の読み込み → Vision の指示の組み立て → 読み取り)で送る図の指示。"""
    reader = FakeReader()
    pipeline = _pipeline(enabled=True, reader=reader)
    pipeline._oracle = PromptOracle(_validated(dict(saved)))

    await _attach(pipeline, _figure_extraction([0.168, 0.119, 0.504, 0.297]), "mineru")

    assert len(reader.calls) == 1
    prompt = str(reader.calls[0]["prompt"])
    system = str(reader.calls[0]["system_prompt"])
    assert_continuous_steps(prompt)
    assert_continuous_steps(system)
    in_user = {
        key: content
        for key, content in saved.items()
        if key not in {"picture_description.system", "picture_description.table"}
    }
    # テンプレートの差し込み口は値に置き換わるので、差し込み口の行は除いて比べる。
    if IMAGE_RETRIEVAL_PROMPT_KEY in in_user:
        in_user[IMAGE_RETRIEVAL_PROMPT_KEY] = in_user[IMAGE_RETRIEVAL_PROMPT_KEY].split(
            "{{image_metadata}}"
        )[1]
    assert_saved_text_is_sent(in_user, prompt)
    if "picture_description.system" in saved:
        assert system == saved["picture_description.system"]


@pytest.mark.parametrize("saved", VISION_CASES, ids=VISION_IDS)
def test_table_prompt_keeps_continuous_steps(saved: dict[str, str]) -> None:
    """表の対象の指示を、取込と同じテンプレートの合成と上書きのスコープで組み立てる。"""
    _validated(dict(saved))
    vision = {key: value for key, value in saved.items() if key.startswith("picture_description.")}
    template = refine_image_retrieval_prompt(
        saved.get(IMAGE_RETRIEVAL_PROMPT_KEY) or default_prompt(IMAGE_RETRIEVAL_PROMPT_KEY),
        rules=vision.get("picture_description.rules"),
    ).strip()
    with bind_prompt_overrides(vision):
        prompt = _render_prompt({}, target_kind="table", template=template)
    assert_continuous_steps(prompt)
    in_user = {
        key: content
        for key, content in vision.items()
        if key in {"picture_description.table", "picture_description.rules"}
    }
    assert_saved_text_is_sent(in_user, prompt)
