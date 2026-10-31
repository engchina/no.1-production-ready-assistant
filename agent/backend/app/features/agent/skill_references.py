"""割り当てたスキルの参照文書を、必要な部分だけ読む（`skill_reference_read`。#862 / #1803）。

参照文書は `AGENT_SKILLS_DIR` の SKILL.md と同じディレクトリの `references/` から読み込んだ
テキスト / Markdown（`skills_loader.load_skill_references`）。Run に割り当てたスキルの参照文書
だけを読ませ、読み込み済みの内容のほかは参照しない（ファイル・URL を開かない・実行しない）。
"""

from __future__ import annotations

from typing import Any

from app.features.agent.skills import resolve_skill, skill_registry
from app.features.agent.tools import ToolDefinition, ToolHandler, ToolInvocationContext

REFERENCE_TOOL_NAME = "skill_reference_read"
REFERENCE_CHUNK_CHARACTERS = 8000


def skill_reference_keys(skill_ids: list[str]) -> list[tuple[str, str]]:
    """割り当てた有効なスキルの参照文書の（スキル ID, パス）。"""
    keys: list[tuple[str, str]] = []
    for skill_id in skill_ids:
        skill = resolve_skill(skill_id)
        if skill is None or not skill.enabled:
            continue
        for reference in skill.references:
            key = (skill.id, reference.path)
            if key not in keys:
                keys.append(key)
    return keys


def reference_tool(keys: list[tuple[str, str]]) -> tuple[ToolDefinition, ToolHandler]:
    allowed = frozenset(keys)

    def read(arguments: dict[str, Any], _: ToolInvocationContext) -> dict[str, Any]:
        skill_id = arguments.get("skill_id")
        path = arguments.get("path")
        offset = arguments.get("offset", 0)
        if not isinstance(skill_id, str) or not isinstance(path, str):
            raise ValueError("skill_id と path を指定してください。")
        if (skill_id, path) not in allowed:
            raise ValueError("この実行のスキルに割り当てられていない参照文書です。")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset は 0 以上の整数を指定してください。")
        # 本文は今のスキルから読む（公開の版に固定するのは索引だけ。#1855）。
        skill = skill_registry.get(skill_id)
        reference = next(
            (item for item in (skill.references if skill else []) if item.path == path), None
        )
        if reference is None:
            raise ValueError("参照文書が見つかりません。")
        content = reference.content[offset : offset + REFERENCE_CHUNK_CHARACTERS]
        end = offset + len(content)
        return {
            "skill_id": skill_id,
            "path": path,
            "content": content,
            "next_offset": end if end < len(reference.content) else None,
        }

    return ToolDefinition(
        name=REFERENCE_TOOL_NAME,
        description=(
            f"割り当てたスキルの参照文書を {REFERENCE_CHUNK_CHARACTERS} 文字ずつ読む。"
            "文書を実行しない。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "skill_id": {"type": "string"},
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
            },
            "required": ["skill_id", "path", "offset"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        side_effects=False,
        audit_tags=["skill", "reference"],
    ), read
