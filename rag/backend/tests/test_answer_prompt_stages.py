"""回答プロンプトの画面の「各段のプロンプト」とコードのずれを防ぐ(#2054)。

- 一覧の各プロンプトの定数は、実行のコード(一覧の定義の外)から参照されている。
- 実行のコードが定義するプロンプトの定数は、一覧に入っているか、載せない理由がある。
- 段・区分・部分には画面の文言(i18n)がある。
"""

from __future__ import annotations

import ast
import importlib
import re
from collections.abc import Iterator
from functools import cache
from pathlib import Path

import pytest

from app.rag.answer_prompts import (
    PROMPT_PHASES,
    PROMPT_STAGES,
    STAGE_PROMPT_REFS,
    readonly_prompt_stages,
)

BACKEND = Path(__file__).resolve().parents[1]
RAG = BACKEND.parent
# 実行のコード(package 名, 置き場所)。テスト・CLI の置き場所は中に無い。
RUNTIME_PACKAGES = (
    ("app", BACKEND / "app"),
    ("rag_engine", RAG / "packages" / "rag_engine" / "rag_engine"),
)
I18N = RAG / "frontend" / "src" / "lib" / "i18n.ts"
# 一覧を作る module 自身の参照は「使われている」に数えない。
LIST_MODULE = "app.rag.answer_prompts"

# プロンプトの定数とみなす名前(module の直下に置く文字列か dict)。
PROMPT_NAME = re.compile(
    r"^_?[A-Z][A-Z0-9_]*(PROMPT|PROMPT_TEMPLATE|INSTRUCTIONS?|REQUIREMENTS|_RULE)$"
)

# 一覧に載せないプロンプトの定数と、その理由。
EDITABLE_DEFAULT = "編集できるプロンプトの既定値(画面は編集欄で示す)"
RERANK_CONTEXT_EXPERIMENT = "再ランクの文脈の実験 CLI だけ（#101）"
PROBE_ONLY = "Enterprise AI の契約の確認の CLI だけ"
UNUSED_SCREEN_JUDGE = "現行の六指標の品質評価から呼ばれない画面専用の補助判定"
NOT_LISTED = {
    "app.rag.evaluation_screens:SCREEN_JUDGE_SYSTEM_PROMPT": UNUSED_SCREEN_JUDGE,
    "app.rag.evaluation_screens:SCREEN_JUDGE_PROMPT": UNUSED_SCREEN_JUDGE,
    "rag_engine.knowledge.prompt_files:DEFAULT_IMAGE_RETRIEVAL_PROMPT": EDITABLE_DEFAULT,
    "rag_engine.knowledge.prompt_files:DEFAULT_VLM_ANSWER_PROMPT": EDITABLE_DEFAULT,
    # 一覧に載せた定数へ組み込み済みの部品。
    "rag_engine.generation.query_prompts:_EXPANSION_INSTRUCTIONS": (
        "QUERY_ROUTING_PROMPT_TEMPLATE と QUERY_EXPANSION_PROMPT_TEMPLATE に組み込み済み"
    ),
    # 検証・確認の CLI だけが使う(回答・取込・品質評価の経路では使わない)。
    "rag_engine.evaluation.crag_eval:CRAG_JUDGE_SYSTEM_PROMPT": "検証 CLI(answer_verify_cli)だけ",
    "app.rag.enterprise_ai_probe:DEFAULT_LLM_PROMPT": PROBE_ONLY,
    "app.rag.enterprise_ai_probe:DEFAULT_VLM_PROMPT": PROBE_ONLY,
    "app.rag.file_processing_staging:FILE_PROCESSING_STAGING_PROMPT": "staging の確認だけ",
    "app.rag.representation_experiment:CONTEXT_PROMPT": "検索表現の比較の実験 CLI だけ（#75）",
    "app.rag.representation_experiment:QUESTIONS_PROMPT": "検索表現の比較の実験 CLI だけ（#75）",
    "app.rag.rerank_context_experiment:DISTRACTOR_PROMPT": RERANK_CONTEXT_EXPERIMENT,
    "app.rag.rerank_context_experiment:CONTEXT_BATCH_PROMPT": RERANK_CONTEXT_EXPERIMENT,
    "app.rag.rerank_context_experiment:ANSWER_PROMPT": RERANK_CONTEXT_EXPERIMENT,
    # 解析の指示(ExtractionStrategy.prompt)は組み立てるが、Vision の解析を
    # 削除した後(#2042)はどのモデルにも渡していない。
    "app.rag.ingestion_strategy:BASE_EXTRACTION_REQUIREMENTS": "モデルに渡していない",
    "app.rag.ingestion_strategy:PROFILE_INSTRUCTIONS": "モデルに渡していない",
}


def _runtime_files() -> Iterator[tuple[str, Path]]:
    for package, root in RUNTIME_PACKAGES:
        for path in sorted(root.rglob("*.py")):
            parts = path.relative_to(root).with_suffix("").parts
            module = ".".join((package, *parts))
            yield (module.removesuffix(".__init__"), path)


@cache
def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _module_level_names(tree: ast.Module) -> Iterator[str]:
    for node in tree.body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        for target in targets:
            if isinstance(target, ast.Name):
                yield target.id


def _defined_prompt_constants() -> set[str]:
    """実行のコードが module の直下に置くプロンプトの定数(``module:名前``)。"""
    found: set[str] = set()
    for module, path in _runtime_files():
        if module == LIST_MODULE:
            continue
        names = [name for name in _module_level_names(_tree(path)) if PROMPT_NAME.match(name)]
        if not names:
            continue
        loaded = importlib.import_module(module)
        for name in names:
            if isinstance(getattr(loaded, name, None), str | dict):
                found.add(f"{module}:{name}")
    return found


def _listed_refs() -> list[str]:
    return [ref for stage in PROMPT_STAGES for _, ref in stage.parts]


def _loads(tree: ast.AST, name: str) -> int:
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load)
    )


def _imports(tree: ast.AST, module: str, name: str) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == module
        and any(alias.name == name for alias in node.names)
        for node in ast.walk(tree)
    )


def _runtime_uses(ref: str) -> int:
    """定数を実行のコードが参照する回数(定義の module の中の読み取り + 他の module の import)。"""
    module, _, name = ref.partition(":")
    uses = 0
    for other, path in _runtime_files():
        if other == LIST_MODULE:
            continue
        tree = _tree(path)
        if other == module:
            uses += _loads(tree, name)
        elif _imports(tree, module, name) and _loads(tree, name):
            uses += 1
    return uses


@pytest.mark.parametrize("ref", _listed_refs(), ids=_listed_refs())
def test_listed_prompt_is_used_by_runtime_code(ref: str) -> None:
    assert _runtime_uses(ref) > 0, (
        f"{ref} は実行のコードから使われていません。一覧から外してください。"
    )


def test_runtime_prompt_constants_are_listed_or_explained() -> None:
    defined = _defined_prompt_constants()
    listed = set(_listed_refs())
    missing = sorted(defined - listed - set(NOT_LISTED))
    assert missing == [], (
        f"実行のコードのプロンプトが画面の一覧(PROMPT_STAGES)にありません: {missing}"
    )
    # 載せない理由の一覧に、もう無い定数を残さない。
    assert sorted(set(NOT_LISTED) - defined) == []
    assert listed & set(NOT_LISTED) == set()


def test_stages_follow_processing_order_with_content() -> None:
    stages = readonly_prompt_stages()
    ids = [stage["id"] for stage in stages]
    assert len(ids) == len(set(ids))
    order = [PROMPT_PHASES.index(str(stage["phase"])) for stage in stages]
    assert order == sorted(order), "段は処理の順(PROMPT_PHASES)に並べる"
    for stage in stages:
        prompts = stage["prompts"]
        assert isinstance(prompts, list) and prompts
        for prompt in prompts:
            assert str(prompt["content"]).strip(), f"{stage['id']}.{prompt['id']} が空です"


def test_every_stage_phase_and_part_has_screen_copy() -> None:
    keys = set(
        re.findall(
            r'^\s*"(settings\.answerPrompts\.[^"]+)":', I18N.read_text(encoding="utf-8"), re.M
        )
    )
    expected = {f"settings.answerPrompts.phase.{phase}" for phase in PROMPT_PHASES}
    for stage in PROMPT_STAGES:
        expected.add(f"settings.answerPrompts.phase.{stage.phase}.description")
        expected.add(f"settings.answerPrompts.stage.{stage.id}")
        expected.add(f"settings.answerPrompts.stage.{stage.id}.condition")
        expected.update(f"settings.answerPrompts.part.{part_id}" for part_id, _ in stage.parts)
    assert sorted(expected - keys) == []


def test_all_runtime_instructions_use_markdown_sections() -> None:
    for stage in readonly_prompt_stages():
        parts = stage["prompts"]
        assert isinstance(parts, list)
        for part in parts:
            # 監査の理由の規則など、Step に連結する部分は小節のまま表示する。
            assert re.search(r"^#{2,3} \S", str(part["content"]), re.M), (stage["id"], part["id"])


def test_evaluation_catalog_exposes_only_current_evaluation_stages() -> None:
    stages = {stage.id: stage for stage in PROMPT_STAGES if stage.phase == "evaluation"}
    assert set(stages) == {"evaluation"}
    assert (
        next(stage for stage in PROMPT_STAGES if stage.id == "answer_validation").phase == "audit"
    )
    assert {part for part, _ in stages["evaluation"].parts} == {
        "scope",
        "coverage",
        "claim",
        "correctness",
    }


def test_catalog_separates_independent_preparation_and_orders_evaluation_calls() -> None:
    stages = {stage.id: stage for stage in PROMPT_STAGES}
    assert stages["raptor_summary"].phase == "indexing"
    assert stages["support_guide_draft"].phase == "knowledge"
    assert [part for part, _ in stages["evaluation"].parts] == [
        "scope",
        "coverage",
        "claim",
        "correctness",
    ]
    assert [part for part, _ in stages["audit"].parts] == [
        "detailedSystem",
        "factSystem",
        "compactRule",
    ]
    # 取込内の順は設定の概要が参照するレシピの実行順と同じ。
    assert [stage.id for stage in PROMPT_STAGES if stage.phase == "ingestion"] == [
        "picture_description",
        "heading_levels",
        "field_extraction",
        "navigation_summary",
    ]


def test_crag_readonly_prompts_are_the_executed_step_instructions() -> None:
    from rag_engine.generation.answering import CRAG_GRADER_SYSTEM_PROMPT
    from rag_engine.generation.crag_support import CRAG_GRADE_PROMPT_TEMPLATE

    displayed: list[str] = []
    for stage in readonly_prompt_stages():
        parts = stage["prompts"]
        assert isinstance(parts, list)
        displayed.extend(part["content"] for part in parts)
    # 画面用の別文ではなく、実行で参照する定数の本文をそのまま登録する。
    assert CRAG_GRADER_SYSTEM_PROMPT in displayed
    assert CRAG_GRADE_PROMPT_TEMPLATE in displayed


def test_readonly_generation_validation_and_evaluation_are_executed_step_prompts() -> None:
    from rag_engine.evaluation.answer_eval import (
        CLAIM_PROMPT,
        CORRECTNESS_PROMPT,
        COVERAGE_PROMPT,
        SCOPE_PROMPT,
    )
    from rag_engine.evaluation.answer_validation import (
        PREMISE_CONSISTENCY_SYSTEM_PROMPT,
        VALIDATION_SYSTEM_PROMPT,
    )
    from rag_engine.generation.grounded import (
        AUDIT_SYSTEM_PROMPT,
        GENERATE_SYSTEM_PROMPT,
        PLANNED_GENERATE_SYSTEM_PROMPT,
    )
    from rag_engine.generation.table_revision import REVISION_SYSTEM_PROMPT

    displayed: list[str] = []
    for stage in readonly_prompt_stages():
        parts = stage["prompts"]
        assert isinstance(parts, list)
        displayed.extend(part["content"] for part in parts)
    for executed in (
        GENERATE_SYSTEM_PROMPT,
        PLANNED_GENERATE_SYSTEM_PROMPT,
        AUDIT_SYSTEM_PROMPT,
        VALIDATION_SYSTEM_PROMPT,
        PREMISE_CONSISTENCY_SYSTEM_PROMPT,
        REVISION_SYSTEM_PROMPT,
        SCOPE_PROMPT,
        COVERAGE_PROMPT,
        CLAIM_PROMPT,
        CORRECTNESS_PROMPT,
    ):
        assert executed in displayed
        assert executed.startswith("## Step 1:")


def test_query_strategy_display_uses_actual_step_prompts() -> None:
    from rag_engine.generation.answering import (
        QUERY_EXPANSION_SYSTEM_PROMPT,
        QUERY_ROUTING_SYSTEM_PROMPT,
    )
    from rag_engine.generation.query_prompts import (
        QUERY_EXPANSION_PROMPT_TEMPLATE,
        QUERY_ROUTING_PROMPT_TEMPLATE,
    )

    displayed: list[object] = []
    for stage in readonly_prompt_stages():
        parts = stage["prompts"]
        assert isinstance(parts, list)
        displayed.extend(part["content"] for part in parts)
    for prompt in (
        QUERY_ROUTING_SYSTEM_PROMPT,
        QUERY_EXPANSION_SYSTEM_PROMPT,
        QUERY_ROUTING_PROMPT_TEMPLATE,
        QUERY_EXPANSION_PROMPT_TEMPLATE,
    ):
        assert prompt in displayed
        assert prompt.startswith("## Step 1:")


def test_every_editable_stage_has_a_runtime_override_lookup() -> None:
    used = {
        node.args[0].value
        for _module, path in _runtime_files()
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "resolve_prompt"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    assert set(STAGE_PROMPT_REFS) <= used


def test_all_default_stage_instructions_follow_ordered_steps() -> None:
    """追加部品は組立先の Step を継続し、それ以外は Step 1 から始める。"""
    continuations = {
        ("picture_description", "picture"): 6,
        ("picture_description", "table"): 6,
        ("picture_description", "contract"): 7,
        ("generation", "assumptionSystem"): 6,
    }
    for stage in readonly_prompt_stages():
        parts = stage["prompts"]
        assert isinstance(parts, list)
        for part in parts:
            identity = (str(stage["id"]), str(part["id"]))
            content = str(part["content"])
            if identity in {("audit", "compactRule"), ("picture_description", "rules")}:
                # 判定の理由の書き方は監査の最後の Step 内の追加規則。
                assert content.lstrip().startswith("### ")
                assert not re.search(r"^## ", content, re.M)
                continue
            steps = [int(n) for n in re.findall(r"^## Step (\d+): \S", content, re.M)]
            first = continuations.get(identity, 1)
            assert content.lstrip().startswith(f"## Step {first}:"), identity
            assert steps and steps == list(range(first, first + len(steps))), identity
            assert not re.search(r"^## (?!Step \d+:)", content, re.M), identity


def test_every_catalog_default_is_the_executed_instruction_constant() -> None:
    """画面だけの見出し変換を認めず、既定値と実行用の定数を一致させる。"""
    from app.rag.answer_prompts import prompt_constant

    displayed: dict[tuple[str, str], str] = {}
    for displayed_stage in readonly_prompt_stages():
        parts = displayed_stage["prompts"]
        assert isinstance(parts, list)
        for part in parts:
            displayed[str(displayed_stage["id"]), str(part["id"])] = str(part["content"])
    for stage in PROMPT_STAGES:
        for part_id, ref in stage.parts:
            assert displayed[stage.id, part_id] == prompt_constant(ref)


def test_default_prompt_steps_have_one_blank_line_between_sections() -> None:
    """画面とモデルが使う既定指示の Step 境界を読みやすく揃える。"""
    for stage in readonly_prompt_stages():
        parts = stage["prompts"]
        assert isinstance(parts, list)
        for part in parts:
            lines = str(part["content"]).splitlines()
            for index, line in enumerate(lines):
                if index == 0 or not re.match(r"^## Step \d+:", line):
                    continue
                identity = (stage["id"], part["id"], line)
                assert lines[index - 1] == "", identity
                if index >= 2:
                    assert lines[index - 2].strip(), identity
