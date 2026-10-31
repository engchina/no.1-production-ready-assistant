"""スキルの更新の取り込みの流れ（#1867）。

業務 Agent の公開の版に固定したスキル（#1855）と今のスキルの差（指示の差分・説明・ツール・参照文書）
を作り、まとめての再公開（スキルの更新だけを取り込む新しい版）と、その取り消し（公開中の版を前の版に
戻す）の入出力を持つ。業界の形（Anthropic の Agent Skills の version 固定 / latest、LangSmith の
本番は commit hash に固定、Langfuse・MLflow の環境のラベルの昇格・ロールバック、Dependabot の
「何が変わったか → 確かめる → まとめて取り込む → 戻せる」）にそろえる。
"""

from __future__ import annotations

import difflib
from typing import Literal

from pydantic import BaseModel, Field

from app.features.agent.runtime import AgentProfile
from app.features.agent.skills import AgentSkillDefinition, PinnedSkill, skill_registry

# 差分の行の上限（長い指示でも応答を大きくしない。超えた分は `truncated`）。
DIFF_MAX_LINES = 400
# まとめての再公開・取り消しで 1 回に扱う業務 Agent の上限。
BULK_MAX_AGENTS = 200

SkillChange = Literal["changed", "removed"]
DiffKind = Literal["hunk", "context", "added", "removed"]


class DiffLine(BaseModel):
    kind: DiffKind
    text: str


class SkillSnapshot(BaseModel):
    """比べる片方（固定した中身か今のスキル）。指示の本文は差分の行で渡す。"""

    name: str
    description: str
    enabled: bool
    tools: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)


class SkillUpdateDetail(BaseModel):
    skill_id: str
    change: SkillChange
    pinned: SkillSnapshot
    current: SkillSnapshot | None = None
    # 指示の差分（unified diff の行。固定 → 今）。
    instructions_diff: list[DiffLine] = Field(default_factory=list)
    truncated: bool = False
    tools_added: list[str] = Field(default_factory=list)
    tools_removed: list[str] = Field(default_factory=list)
    references_added: list[str] = Field(default_factory=list)
    references_removed: list[str] = Field(default_factory=list)
    # 本文だけが変わった参照文書（パスは同じ）。
    references_changed: list[str] = Field(default_factory=list)


class AgentSkillUpdates(BaseModel):
    agent_id: str
    published_version: int | None
    items: list[SkillUpdateDetail] = Field(default_factory=list)


class SkillUpdatePublishRequest(BaseModel):
    agent_ids: list[str] = Field(min_length=1, max_length=BULK_MAX_AGENTS)
    note: str = Field(default="", max_length=500)


class SkillUpdatePublishResult(BaseModel):
    agent_id: str
    name: str = ""
    status: Literal["published", "skipped"]
    # 公開した版と、その前の公開中の版（取り消しに使う）。
    version: int | None = None
    previous_version: int | None = None
    reason: str | None = None


class SkillUpdatePublishData(BaseModel):
    results: list[SkillUpdatePublishResult] = Field(default_factory=list)


class PublishedVersionTarget(BaseModel):
    agent_id: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=1)


class PublishedVersionRollbackRequest(BaseModel):
    items: list[PublishedVersionTarget] = Field(min_length=1, max_length=BULK_MAX_AGENTS)


def _tool_labels(skill: AgentSkillDefinition | PinnedSkill) -> list[str]:
    labels: list[str] = []
    for requirement in skill.mcp_requirements:
        if requirement.tool_names:
            labels.extend(f"{requirement.server_id}:{tool}" for tool in requirement.tool_names)
        else:
            labels.append(f"{requirement.server_id}:*")
    return labels


def _snapshot(skill: AgentSkillDefinition | PinnedSkill) -> SkillSnapshot:
    return SkillSnapshot(
        name=skill.name,
        description=skill.description,
        enabled=skill.enabled,
        tools=_tool_labels(skill),
        references=[item.path for item in skill.references],
    )


def instructions_diff(before: str, after: str) -> tuple[list[DiffLine], bool]:
    """指示の差分（unified diff。前後 2 行の文脈）。行が多いときは先頭から上限まで。"""
    lines: list[DiffLine] = []
    for raw in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=2):
        if raw.startswith(("---", "+++")):
            continue
        if raw.startswith("@@"):
            lines.append(DiffLine(kind="hunk", text=raw))
        elif raw.startswith("+"):
            lines.append(DiffLine(kind="added", text=raw[1:]))
        elif raw.startswith("-"):
            lines.append(DiffLine(kind="removed", text=raw[1:]))
        else:
            lines.append(DiffLine(kind="context", text=raw[1:] if raw.startswith(" ") else raw))
    return lines[:DIFF_MAX_LINES], len(lines) > DIFF_MAX_LINES


def skill_update_detail(pin: PinnedSkill) -> SkillUpdateDetail:
    """固定したスキルと今のスキルの差（今のスキルが無ければ removed）。"""
    current = skill_registry.get(pin.id)
    if current is None:
        diff, truncated = instructions_diff(pin.instructions, "")
        return SkillUpdateDetail(
            skill_id=pin.id,
            change="removed",
            pinned=_snapshot(pin),
            instructions_diff=diff,
            truncated=truncated,
            tools_removed=_tool_labels(pin),
            references_removed=[item.path for item in pin.references],
        )
    from app.features.agent.skills import pin_skill

    now = pin_skill(current)
    before_tools, after_tools = _tool_labels(pin), _tool_labels(now)
    before_refs = {item.path: item.sha256 for item in pin.references}
    after_refs = {item.path: item.sha256 for item in now.references}
    diff, truncated = instructions_diff(pin.instructions, now.instructions)
    return SkillUpdateDetail(
        skill_id=pin.id,
        change="changed",
        pinned=_snapshot(pin),
        current=_snapshot(now),
        instructions_diff=diff,
        truncated=truncated,
        tools_added=[tool for tool in after_tools if tool not in before_tools],
        tools_removed=[tool for tool in before_tools if tool not in after_tools],
        references_added=[path for path in after_refs if path not in before_refs],
        references_removed=[path for path in before_refs if path not in after_refs],
        references_changed=[
            path
            for path, digest in after_refs.items()
            if path in before_refs and before_refs[path] != digest
        ],
    )


def agent_skill_updates(agent: AgentProfile) -> AgentSkillUpdates:
    """業務 Agent の公開中の版に固定したスキルのうち、変わったものの差（#1867）。"""
    published = agent.published()
    pins = {item.id: item for item in (published.skills or [])} if published else {}
    return AgentSkillUpdates(
        agent_id=agent.id,
        published_version=agent.published_version,
        items=[skill_update_detail(pins[skill_id]) for skill_id in agent.skill_updates],
    )
