"""Skill 外部定義のローダ。

Agent Skills の仕様（agentskills.io。ディレクトリ + `SKILL.md`（YAML frontmatter + 本文）、
progressive disclosure）を、本プロダクトの Skill（instructions + MCP ツールの許可リスト）へ中立に
再マッピングする。`.claude` / `.codex` のような vendor 固有名は使わず、中立な
`AGENT_SKILLS_DIR`(`<dir>/<name>/SKILL.md`)と `AGENT_SKILLS_DEFINITIONS_JSON` を読む。
組み込みのスキル（repo 直下の `skills/`）も同じ読み方をする（#1816）。不正定義は warning で
スキップし、安全側へ縮退する。

SKILL.md の frontmatter は仕様のフィールドだけを使う（#1816）:

- `name`: スキルの ID（小文字・数字・ハイフン、64 文字以内、フォルダ名と同じ）。
- `description`: 何をするか＋いつ使うか。
- `metadata`（文字列 → 文字列）の本プロダクトの拡張:
  - `production-ready-display-name`: 画面の名前（無ければ `name`）。
  - `production-ready-mcp-tools`: 使う MCP ツール。`<接続>:<ツール>` を空白で区切る
    （`<接続>` だけなら接続のすべてのツール）。`mcp_requirements` になる。
  - `production-ready-tags`: タグ（空白区切り）。
  - `production-ready-evidence-planning`: 根拠の取得の検索文を作る主体。`<接続>:caller` を空白で
    区切る（その接続の `mcp_requirements` の `evidence_planning`。#2651）。無ければ rag。

SKILL.md と同じディレクトリの `references/` の下のテキスト / Markdown は、スキルの参照文書として
読む（#1803）。symlink は辿らず、ディレクトリの外は読まない。件数・大きさに上限を置き、超えた分は
読まずに warning を出す。`scripts/` などの実行するファイルは読まない・実行しない。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.features.agent.skills import (
    EVIDENCE_PLANNING_CALLER,
    EVIDENCE_PLANNING_RAG,
    AgentSkillDefinition,
    SkillMcpRequirement,
    SkillReference,
)

logger = logging.getLogger(__name__)

JsonObject = dict[str, Any]

# 参照文書（`references/`）。読むのはテキスト / Markdown だけ。
REFERENCES_DIR = "references"
REFERENCE_SUFFIXES = frozenset({".md", ".markdown", ".txt"})
REFERENCE_MAX_FILES = 100
REFERENCE_MAX_FILE_BYTES = 1024 * 1024
REFERENCE_MAX_TOTAL_BYTES = 4 * 1024 * 1024

# Agent Skills の `name` の制約（1〜64 文字の小文字・数字・ハイフン。先頭・末尾・連続の
# ハイフンは不可）。
SKILL_NAME_PATTERN = re.compile(r"^(?=.{1,64}$)[a-z0-9]+(?:-[a-z0-9]+)*$")
METADATA_DISPLAY_NAME = "production-ready-display-name"
METADATA_MCP_TOOLS = "production-ready-mcp-tools"
METADATA_TAGS = "production-ready-tags"
METADATA_EVIDENCE_PLANNING = "production-ready-evidence-planning"


def _split_frontmatter(text: str) -> tuple[JsonObject, str]:
    """`---` 区切りの YAML frontmatter と本文を分離する。"""
    if not text.startswith("---"):
        return {}, text.strip()
    lines = text.splitlines()
    closing: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            closing = index
            break
    if closing is None:
        return {}, text.strip()
    frontmatter_text = "\n".join(lines[1:closing])
    body = "\n".join(lines[closing + 1 :]).strip()
    try:
        data = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError:
        return {}, body
    return (data if isinstance(data, dict) else {}), body


def _skill_from_mapping(
    data: JsonObject,
    body: str,
    *,
    fallback_id: str,
    source: str,
) -> AgentSkillDefinition | None:
    skill_id = str(data.get("id") or fallback_id or "").strip()
    if not skill_id:
        return None
    instructions = body.strip() or str(data.get("instructions") or "")
    payload: JsonObject = {
        "id": skill_id,
        "name": str(data.get("name") or skill_id),
        "description": str(data.get("description") or ""),
        "instructions": instructions,
        "mcp_requirements": data.get("mcp_requirements") or [],
        "enabled": bool(data.get("enabled", True)),
        "tags": list(data.get("tags") or []),
        "source": source,
    }
    return AgentSkillDefinition.model_validate(payload)


def mcp_requirements_from_metadata(value: str) -> list[SkillMcpRequirement]:
    """`rag:rag_search rag:rag_read_source nl2sql` → 接続ごとの許可リスト（順序を保つ）。

    `<接続>` だけの語は、その接続のすべてのツール（`tool_names` が空）。
    """
    tools: dict[str, list[str] | None] = {}
    for token in value.split():
        server, _, tool = token.partition(":")
        server = server.strip()
        if not server:
            continue
        if not tool:
            tools[server] = None
            continue
        current = tools.setdefault(server, [])
        if current is not None and tool not in current:
            current.append(tool)
    return [
        SkillMcpRequirement(server_id=server, tool_names=list(names or []))
        for server, names in tools.items()
    ]


def with_evidence_planning(
    requirements: list[SkillMcpRequirement], value: str
) -> list[SkillMcpRequirement]:
    """`rag:caller` → その接続の要件の `evidence_planning`（知らない値・接続は無視する）。"""
    planning: dict[str, str] = {}
    for token in value.split():
        server, _, mode = token.partition(":")
        if server.strip() and mode in {EVIDENCE_PLANNING_RAG, EVIDENCE_PLANNING_CALLER}:
            planning[server.strip()] = mode
        else:
            logger.warning("%s の値を読めないので無視します: %s", METADATA_EVIDENCE_PLANNING, token)
    return [
        item.model_copy(update={"evidence_planning": planning[item.server_id]})
        if item.server_id in planning
        else item
        for item in requirements
    ]


def _skill_from_skill_md(
    data: JsonObject, body: str, *, folder: str, source: str
) -> AgentSkillDefinition | None:
    """仕様の frontmatter（`name`・`description`・`metadata`）から Skill を作る。

    `name` が仕様の制約を満たさない・フォルダ名と違うものは読まない（外部の Agent でも同じ ID で
    使えるようにする）。
    """
    name = data.get("name")
    if not isinstance(name, str) or not SKILL_NAME_PATTERN.fullmatch(name) or name != folder:
        logger.warning(
            "SKILL.md の name がフォルダ名と同じ小文字・数字・ハイフンでないので読みません: %s",
            folder,
        )
        return None
    raw_metadata = data.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    display_name = str(metadata.get(METADATA_DISPLAY_NAME) or name).strip() or name
    return AgentSkillDefinition.model_validate(
        {
            "id": name,
            "name": display_name,
            "description": str(data.get("description") or "").strip(),
            "instructions": body.strip(),
            "mcp_requirements": with_evidence_planning(
                mcp_requirements_from_metadata(str(metadata.get(METADATA_MCP_TOOLS) or "")),
                str(metadata.get(METADATA_EVIDENCE_PLANNING) or ""),
            ),
            "tags": str(metadata.get(METADATA_TAGS) or "").split(),
            "source": source,
        }
    )


def load_skill_references(skill_dir: Path) -> list[SkillReference]:
    """`<skill_dir>/references/**` のテキスト / Markdown を参照文書として読む。

    symlink（ファイル・ディレクトリ）は辿らず、解決したパスが `references/` の外なら読まない。
    件数・1 ファイル・合計の上限を超えた分は読まない。
    """
    root = skill_dir / REFERENCES_DIR
    if root.is_symlink() or not root.is_dir():
        return []
    resolved_root = root.resolve()
    references: list[SkillReference] = []
    total = 0
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        # symlink のディレクトリは降りない（os.walk は followlinks=False でも一覧には出す）。
        dirnames[:] = sorted(name for name in dirnames if not (current_path / name).is_symlink())
        for name in sorted(filenames):
            file_path = current_path / name
            relative = file_path.relative_to(skill_dir).as_posix()
            if file_path.is_symlink() or file_path.suffix.lower() not in REFERENCE_SUFFIXES:
                continue
            if not file_path.resolve().is_relative_to(resolved_root):
                logger.warning("参照文書がディレクトリの外を指すので読みません: %s", relative)
                continue
            if len(references) >= REFERENCE_MAX_FILES:
                logger.warning("参照文書の件数の上限を超えたので読みません: %s", relative)
                continue
            size = file_path.stat().st_size
            if size > REFERENCE_MAX_FILE_BYTES or total + size > REFERENCE_MAX_TOTAL_BYTES:
                logger.warning("参照文書の大きさの上限を超えたので読みません: %s", relative)
                continue
            try:
                content = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                logger.warning("参照文書を読めませんでした: %s", relative)
                continue
            total += size
            references.append(
                SkillReference(path=relative, characters=len(content), content=content)
            )
    return references


def load_skills_from_dir(
    path: str | None, *, source: str = "project"
) -> list[AgentSkillDefinition]:
    """`<path>/<name>/SKILL.md` を走査して skill を読む（既定は project 層）。"""
    if not path or not str(path).strip():
        return []
    base = Path(path)
    if not base.is_dir():
        return []
    skills: list[AgentSkillDefinition] = []
    seen: set[str] = set()
    for skill_md in sorted(base.glob("*/SKILL.md")):
        try:
            text = skill_md.read_text(encoding="utf-8")
        except OSError:
            logger.warning("SKILL.md を読めませんでした: %s", skill_md)
            continue
        data, body = _split_frontmatter(text)
        try:
            skill = _skill_from_skill_md(data, body, folder=skill_md.parent.name, source=source)
        except ValidationError as exc:
            logger.warning("不正な SKILL.md をスキップ: %s (%s)", skill_md, exc)
            continue
        if skill is None or skill.id in seen:
            continue
        seen.add(skill.id)
        references = load_skill_references(skill_md.parent)
        skills.append(skill.model_copy(update={"references": references}) if references else skill)
    return skills


def load_skills_from_json(raw: str | None) -> list[AgentSkillDefinition]:
    """`AGENT_SKILLS_DEFINITIONS_JSON` を env 層の skill 宣言として読む。"""
    if not raw or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("AGENT_SKILLS_DEFINITIONS_JSON が不正な JSON です")
        return []
    items: object
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("skills")
    else:
        items = None
    if not isinstance(items, list):
        return []
    skills: list[AgentSkillDefinition] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            skill = _skill_from_mapping(
                item, "", fallback_id=str(item.get("id") or ""), source="env"
            )
        except ValidationError as exc:
            logger.warning("JSON 宣言の不正 skill をスキップ: %s", exc)
            continue
        if skill is None or skill.id in seen:
            continue
        seen.add(skill.id)
        skills.append(skill)
    return skills
