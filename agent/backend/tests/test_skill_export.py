"""スキルを Agent Skills の形（`<name>/SKILL.md` ＋ `references/`）で書き出す（#1874）。

書き出した zip を展開し、公式の `skills-ref` の `validate` と、本プロダクトの読み込み
（`skills_loader`）で読み戻せることを確かめる。
"""

from __future__ import annotations

import contextlib
import io
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
import skills_ref  # type: ignore[import-untyped]  # 型の情報（py.typed）が無い
from security_support import client

from app.features.agent.runtime import AgentProfile, runtime_repository
from app.features.agent.skill_export import export_skill, skill_folder_name
from app.features.agent.skills import (
    BUILTIN_SKILL_IDS,
    BUILTIN_SKILLS_DIR,
    AgentSkillDefinition,
    SkillMcpRequirement,
    SkillReference,
    skill_registry,
)
from app.features.agent.skills_loader import load_skills_from_dir

SKILL_ID = "Sales_Report 1874"
AGENT_ID = "skill1874-agent"


@pytest.fixture
def runtime_skill() -> Iterator[AgentSkillDefinition]:
    skill = skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name="売上の報告",
            description="売上の数値を NL2SQL で照会し、資料の根拠を RAG で添えて報告する。",
            instructions="売上は nl2sql_query で照会し、規程は rag_search で確かめる。",
            mcp_requirements=[
                SkillMcpRequirement(server_id="nl2sql", tool_names=["nl2sql_query"]),
                SkillMcpRequirement(server_id="rag", tool_names=["rag_search", "rag_read_source"]),
            ],
            tags=["sales", "report"],
        )
    )
    try:
        yield skill
    finally:
        with contextlib.suppress(KeyError, ValueError):
            skill_registry.remove(SKILL_ID)


def _extract(data: bytes, target: Path) -> Path:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        archive.extractall(target)
    roots = {name.split("/", 1)[0] for name in names}
    assert len(roots) == 1
    return target / roots.pop()


def test_folder_name_follows_the_spec() -> None:
    assert skill_folder_name("business-rag-research") == "business-rag-research"
    assert skill_folder_name("Sales_Report 1874") == "sales-report-1874"
    assert skill_folder_name("--x--") == "x"
    assert skill_folder_name("経費") == "skill"
    assert len(skill_folder_name("a" * 100)) == 64


def test_runtime_skill_export_passes_the_reference_validator(
    runtime_skill: AgentSkillDefinition, tmp_path: Path
) -> None:
    exported = export_skill(runtime_skill)
    folder = _extract(exported.zip_bytes(), tmp_path)
    assert folder.name == "sales-report-1874"
    assert skills_ref.validate(folder) == []
    # 本プロダクトの読み込みで同じスキルに戻る（名前・説明・指示・MCP のツール・タグ）。
    [loaded] = load_skills_from_dir(str(tmp_path))
    assert (loaded.name, loaded.description, loaded.instructions) == (
        runtime_skill.name,
        runtime_skill.description,
        runtime_skill.instructions,
    )
    assert [(item.server_id, item.tool_names) for item in loaded.mcp_requirements] == [
        ("nl2sql", ["nl2sql_query"]),
        ("rag", ["rag_search", "rag_read_source"]),
    ]
    assert loaded.tags == ["sales", "report"]
    text = (folder / "SKILL.md").read_text(encoding="utf-8")
    assert "compatibility: MCP サーバー nl2sql・rag に接続していること。" in text


def test_references_are_exported_and_unsafe_paths_are_not(tmp_path: Path) -> None:
    skill = AgentSkillDefinition(
        id="with-refs",
        name="参照つき",
        description="",
        instructions="[手順](references/guide.md) を読む。",
        references=[
            SkillReference(path="references/guide.md", content="# 手順\n"),
            SkillReference(path="references/../secret.txt", content="x"),
            SkillReference(path="scripts/run.sh", content="rm -rf /"),
        ],
    )
    exported = export_skill(skill)
    assert [item.path for item in exported.files] == ["SKILL.md", "references/guide.md"]
    folder = _extract(exported.zip_bytes(), tmp_path)
    # 説明が空でも、仕様の必須の description を埋める。
    assert skills_ref.validate(folder) == []


@pytest.mark.parametrize("skill_id", BUILTIN_SKILL_IDS, ids=list(BUILTIN_SKILL_IDS))
def test_builtin_skill_export_is_the_same_as_the_skills_folder(
    skill_id: str, tmp_path: Path
) -> None:
    skill = skill_registry.get(skill_id)
    assert skill is not None
    folder = _extract(export_skill(skill).zip_bytes(), tmp_path)
    source = BUILTIN_SKILLS_DIR / skill_id
    assert folder.name == skill_id
    assert (folder / "SKILL.md").read_bytes() == (source / "SKILL.md").read_bytes()
    exported = sorted(path.relative_to(folder) for path in folder.rglob("*") if path.is_file())
    expected = sorted(
        path.relative_to(source)
        for path in source.rglob("*")
        if path.is_file() and (path.name == "SKILL.md" or path.parts[-2:-1] == ("references",))
    )
    assert exported == expected
    assert skills_ref.validate(folder) == []


def test_export_api_returns_a_zip(runtime_skill: AgentSkillDefinition, tmp_path: Path) -> None:
    response = client.get(f"/api/skills/{SKILL_ID}/export")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert 'filename="sales-report-1874.zip"' in response.headers["content-disposition"]
    assert skills_ref.validate(_extract(response.content, tmp_path)) == []
    assert client.get("/api/skills/no-such-skill/export").status_code == 404


def test_mcp_skill_export_returns_only_skills_of_usable_agents(
    runtime_skill: AgentSkillDefinition,
) -> None:
    runtime_repository.create_agent(
        AgentProfile(id=AGENT_ID, name="売上", instructions="答える。", skill_ids=[SKILL_ID])
    )
    try:

        def call(skill_id: str) -> dict[str, object]:
            response = client.post(
                "/api/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "agent_skill_export", "arguments": {"skill_id": skill_id}},
                },
            )
            assert response.status_code == 200, response.text
            result: dict[str, object] = response.json()["result"]
            return result

        exported = call(SKILL_ID)["structuredContent"]
        assert isinstance(exported, dict)
        assert exported["name"] == "sales-report-1874"
        assert [item["path"] for item in exported["files"]] == ["SKILL.md"]
        # 使える業務 Agent が使っていないスキルは出さない。
        missing = call("no-such-skill")["structuredContent"]
        assert isinstance(missing, dict) and missing["error_code"] == "SKILL_NOT_FOUND"
    finally:
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent(AGENT_ID)
