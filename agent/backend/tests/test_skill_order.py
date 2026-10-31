"""スキルの並びの順（#1866）。

組み込みのスキルを「業務 RAG 調査 → 構造化データ照会 → 資料とデータの横断調査」の順で先頭に
固定し、その後に組み込みでないスキルを表示名の順で並べる。画面はこの順を保つ（並べ替えない）。
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.features.agent.skills import (
    BUILTIN_SKILL_IDS,
    AgentSkillDefinition,
    SkillRegistry,
    skill_registry,
)
from app.main import app

client = TestClient(app)

# ID の文字の順では組み込みのスキルの間に入る ID（`business-…` と `rag-…` の間）。
RUNTIME_SKILLS = {
    "github_repo_research_1866": "GitHub リポジトリ調査（テスト）",
    "zz_accounting_1866": "あいさつの手順",
    "aa_inventory_1866": "ワークフローの手順",
}


@pytest.fixture
def runtime_skills() -> Iterator[None]:
    for skill_id, name in RUNTIME_SKILLS.items():
        created = client.post("/api/skills", json={"id": skill_id, "name": name})
        assert created.status_code == 200
    yield
    for skill_id in RUNTIME_SKILLS:
        with contextlib.suppress(KeyError, ValueError):
            skill_registry.remove(skill_id)


def test_builtin_skill_ids_are_in_display_order() -> None:
    assert BUILTIN_SKILL_IDS == (
        "business-rag-research",
        "structured-data-query",
        "rag-and-structured-data",
        "article-catalog-lookup",
    )


def test_list_skills_puts_builtin_first_then_by_name(runtime_skills: None) -> None:
    response = client.get("/api/skills")
    assert response.status_code == 200
    skills = response.json()["data"]["skills"]
    ids = [skill["id"] for skill in skills]

    # 組み込みは決まった順で先頭（ID の文字の順ではない）。
    assert ids[: len(BUILTIN_SKILL_IDS)] == list(BUILTIN_SKILL_IDS)
    assert all(skill["source"] != "builtin" for skill in skills[len(BUILTIN_SKILL_IDS) :])
    # 組み込みでないスキルは表示名の順（英字 → ひらがな → カタカナは五十音に寄せる）。
    added = [skill_id for skill_id in ids if skill_id in RUNTIME_SKILLS]
    assert added == ["github_repo_research_1866", "zz_accounting_1866", "aa_inventory_1866"]


def test_registry_orders_builtin_by_registration_and_others_by_name() -> None:
    registry = SkillRegistry()
    for skill_id, name in [("c-skill", "C の組み込み"), ("a-skill", "A の組み込み")]:
        registry.register(AgentSkillDefinition(id=skill_id, name=name))
    registry.set_declared(
        "project",
        [
            AgentSkillDefinition(id="p1", name="ファイルの手順"),
            AgentSkillDefinition(id="p0", name="Alpha"),
        ],
    )
    registry.set_declared("env", [AgentSkillDefinition(id="e1", name="えんの手順")])
    registry.upsert_custom(AgentSkillDefinition(id="r1", name="あの手順"))
    registry.upsert_custom(AgentSkillDefinition(id="r0", name="あの手順"))

    assert [skill.id for skill in registry.list()] == [
        "c-skill",
        "a-skill",
        "p0",
        "r0",
        "r1",
        "e1",
        "p1",
    ]
    # 由来で絞り込んでも同じ順。
    assert [skill.id for skill in registry.export("builtin")] == ["c-skill", "a-skill"]
    assert [skill.id for skill in registry.export("runtime")] == ["r0", "r1"]


def test_name_order_folds_width_case_and_kana() -> None:
    registry = SkillRegistry()
    registry.upsert_custom(AgentSkillDefinition(id="k", name="カ行の手順"))
    registry.upsert_custom(AgentSkillDefinition(id="a", name="あ行の手順"))
    registry.upsert_custom(AgentSkillDefinition(id="w", name="ｂｅｔａ"))
    registry.upsert_custom(AgentSkillDefinition(id="x", name="Alpha"))

    assert [skill.id for skill in registry.list()] == ["x", "w", "a", "k"]
