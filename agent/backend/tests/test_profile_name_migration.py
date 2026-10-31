"""#860 の保存値移行は対象範囲とユーザー本文を保持する。"""

import json

import pytest
from pydantic import ValidationError

from app.features.agent.profile_name_migration import migrate_sdk_checkpoint, migrate_tool_name
from app.features.agent.skills import SkillMcpRequirement
from app.features.agent.tools import ToolCall


def test_saved_call_and_skill_keep_target_and_free_text() -> None:
    call = ToolCall.model_validate(
        {
            "name": "rag__rag_search",
            "arguments": {
                "business_view_id": "bv-42",
                "query": "業務ビュー business_view_id の説明",
            },
        }
    )
    assert call.arguments == {
        "search_answer_profile_id": "bv-42",
        "query": "業務ビュー business_view_id の説明",
    }
    skill = SkillMcpRequirement(
        server_id="rag", tool_names=["rag_list_business_views", "rag_search"]
    )
    assert skill.tool_names == ["rag_list_search_answer_profiles", "rag_search"]
    assert (
        migrate_tool_name("rag__rag_list_business_views") == "rag__rag_list_search_answer_profiles"
    )
    assert migrate_tool_name("nl2sql__list_business_views") == "nl2sql__list_business_views"


def test_conflicting_saved_scope_is_not_silently_changed() -> None:
    with pytest.raises(ValidationError):
        ToolCall.model_validate(
            {
                "name": "rag__rag_search",
                "arguments": {"business_view_id": "bv-1", "search_answer_profile_id": "bv-2"},
            }
        )


def test_checkpoint_changes_function_calls_only_and_is_idempotent() -> None:
    source = {
        "items": [
            {"type": "message", "content": "rag_list_business_views 業務ビュー"},
            {
                "type": "function_call",
                "name": "rag__rag_search",
                "call_id": "existing-id",
                "arguments": json.dumps({"business_view_id": "bv-1", "query": "business_view_id"}),
            },
            {
                "type": "function_call",
                "name": "other__custom",
                "arguments": {"business_view_id": "keep"},
            },
        ]
    }
    migrated = migrate_sdk_checkpoint(json.dumps(source))
    data = json.loads(migrated)
    assert data["items"][0] == source["items"][0]
    assert data["items"][2] == source["items"][2]
    assert data["items"][1]["call_id"] == "existing-id"
    assert json.loads(data["items"][1]["arguments"]) == {
        "search_answer_profile_id": "bv-1",
        "query": "business_view_id",
    }
    assert migrate_sdk_checkpoint(migrated) == migrated


def test_long_connection_policy_name_uses_the_same_sdk_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.features.agent.config import McpConnectionConfig, runtime_config_store
    from app.features.agent.tools import mcp_function_name

    server_id = "company-rag-" + "a" * 45
    config = McpConnectionConfig(server_id=server_id, base_url="http://rag.test/api/mcp")
    monkeypatch.setattr(runtime_config_store, "list_mcp_servers", lambda: [config])
    old = mcp_function_name(server_id, "rag_list_business_views")
    expected = mcp_function_name(server_id, "rag_list_search_answer_profiles")
    assert migrate_tool_name(old) == expected
    assert len(expected) == 64


def test_restored_deny_policy_still_denies_the_renamed_read_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import Any

    from app.features.agent.config import runtime_config_store
    from app.features.agent.control_plane_store import _restore_item
    from app.features.agent.tools import ToolDefinition, ToolPolicy, ToolPolicyDecision

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        runtime_config_store, "patch_tool_policy", lambda **kwargs: captured.update(kwargs)
    )
    _restore_item(
        "tool_policy",
        {
            "default_mode": "approval",
            "allow": [],
            "ask": [],
            "deny": ["rag__rag_list_business_views"],
        },
    )
    policy = ToolPolicy(**captured)
    definition = ToolDefinition(
        name="rag__rag_list_search_answer_profiles",
        description="一覧",
        input_schema={},
        output_schema={},
    )
    assert policy.decide(definition) == ToolPolicyDecision.DENY
