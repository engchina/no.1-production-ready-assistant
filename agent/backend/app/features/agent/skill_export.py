"""スキルを Agent Skills の形（`<name>/SKILL.md` ＋ `references/`）で書き出す（#1874）。

画面で作ったスキルを、Claude Code・Codex などの外部の Agent に入れられるようにする。仕様は
agentskills.io（frontmatter は `name`・`description`・`compatibility`・`metadata`。`name` は
小文字・数字・ハイフンの 64 文字以内でフォルダ名と同じ）。検証は `skills-ref`（テスト）。

- 組み込みのスキル（`skills/<name>/`）と `AGENT_SKILLS_DIR` のスキルは、元の SKILL.md と
  `references/` のテキストをそのまま書き出す（`skills/` と同じ内容）。
- 画面・API・JSON の宣言のスキルは、frontmatter を作る。本プロダクトの拡張は `metadata` の
  `production-ready-display-name`・`production-ready-mcp-tools`（`<接続>:<ツール>`）・
  `production-ready-tags`・`production-ready-evidence-planning`（読み込みの `skills_loader` と
  同じ key）。
- `scripts/` などの実行するファイルは書き出さない（読み込みと同じく読まない）。
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.features.agent.skills import (
    BUILTIN_SKILLS_DIR,
    EVIDENCE_PLANNING_RAG,
    AgentSkillDefinition,
)
from app.features.agent.skills_loader import (
    METADATA_DISPLAY_NAME,
    METADATA_EVIDENCE_PLANNING,
    METADATA_MCP_TOOLS,
    METADATA_TAGS,
    SKILL_NAME_PATTERN,
    load_skill_references,
)
from app.settings import get_settings

# Agent Skills の上限（skills-ref の validator と同じ）。
_MAX_NAME_CHARS = 64
_MAX_DESCRIPTION_CHARS = 1024
_MAX_COMPATIBILITY_CHARS = 500
_INVALID_NAME_CHARS = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class ExportedFile:
    # スキルのフォルダからの相対パス（`SKILL.md`・`references/guide.md`）。
    path: str
    content: str


@dataclass(frozen=True, slots=True)
class ExportedSkill:
    # フォルダ名と frontmatter の `name`。
    name: str
    files: tuple[ExportedFile, ...]

    def zip_bytes(self) -> bytes:
        """`<name>/SKILL.md` と `<name>/references/…` の zip。"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for item in self.files:
                archive.writestr(f"{self.name}/{item.path}", item.content)
        return buffer.getvalue()


def skill_folder_name(skill_id: str) -> str:
    """スキルの ID から、仕様の `name`（小文字・数字・ハイフン、64 文字以内）を作る。"""
    if SKILL_NAME_PATTERN.fullmatch(skill_id):
        return skill_id
    slug = _INVALID_NAME_CHARS.sub("-", skill_id.lower()).strip("-")
    slug = slug[:_MAX_NAME_CHARS].strip("-")
    return slug or "skill"


def _source_dir(skill: AgentSkillDefinition) -> Path | None:
    """元の SKILL.md のあるフォルダ（組み込み・`AGENT_SKILLS_DIR` のスキル）。"""
    if skill.source == "builtin":
        base: Path | None = BUILTIN_SKILLS_DIR
    elif skill.source == "project":
        configured = str(get_settings().agent_skills_dir or "").strip()
        base = Path(configured) if configured else None
    else:
        base = None
    if base is None:
        return None
    folder = base / skill.id
    skill_md = folder / "SKILL.md"
    if folder.is_symlink() or skill_md.is_symlink() or not skill_md.is_file():
        return None
    return folder


def _generated_skill_md(skill: AgentSkillDefinition, name: str) -> str:
    description = " ".join(skill.description.split()) or f"{skill.name or name}のスキル。"
    metadata: dict[str, str] = {METADATA_DISPLAY_NAME: skill.name or name}
    tools = " ".join(
        f"{requirement.server_id}:{tool}" if tool else requirement.server_id
        for requirement in skill.mcp_requirements
        for tool in (requirement.tool_names or [""])
    )
    if tools:
        metadata[METADATA_MCP_TOOLS] = tools
    planning = " ".join(
        f"{requirement.server_id}:{requirement.evidence_planning}"
        for requirement in skill.mcp_requirements
        if requirement.evidence_planning != EVIDENCE_PLANNING_RAG
    )
    if planning:
        metadata[METADATA_EVIDENCE_PLANNING] = planning
    if skill.tags:
        metadata[METADATA_TAGS] = " ".join(skill.tags)
    frontmatter: dict[str, object] = {
        "name": name,
        "description": description[:_MAX_DESCRIPTION_CHARS],
    }
    servers = list(dict.fromkeys(item.server_id for item in skill.mcp_requirements))
    if servers:
        compatibility = f"MCP サーバー {'・'.join(servers)} に接続していること。"
        frontmatter["compatibility"] = compatibility[:_MAX_COMPATIBILITY_CHARS]
    frontmatter["metadata"] = metadata
    header = yaml.safe_dump(
        frontmatter, allow_unicode=True, sort_keys=False, default_flow_style=False, width=1000
    )
    body = skill.instructions.strip()
    return f"---\n{header}---\n{body}\n"


def export_skill(skill: AgentSkillDefinition) -> ExportedSkill:
    """スキルを Agent Skills の形のファイルの一覧にする。"""
    folder = _source_dir(skill)
    if folder is not None:
        # 元のフォルダ（組み込みは skills/ と同じ内容）。参照文書は読み込みと同じ規則で読む。
        files = [ExportedFile("SKILL.md", (folder / "SKILL.md").read_text(encoding="utf-8"))]
        files.extend(
            ExportedFile(item.path, item.content) for item in load_skill_references(folder)
        )
        return ExportedSkill(name=folder.name, files=tuple(files))
    name = skill_folder_name(skill.id)
    files = [ExportedFile("SKILL.md", _generated_skill_md(skill, name))]
    files.extend(
        ExportedFile(item.path, item.content)
        for item in skill.references
        if item.content and item.path.startswith("references/") and ".." not in item.path
    )
    return ExportedSkill(name=name, files=tuple(files))
