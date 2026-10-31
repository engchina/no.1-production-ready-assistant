"""権限の拒否・未設定・通信障害を区別する。"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.features.agent.data_scope import DataScopeCandidatesError, list_candidates
from app.features.agent.tools import ExternalToolError


@pytest.mark.parametrize(
    "error,expected",
    [
        (
            ExternalToolError("mcp.tool_error", "権限なし", {"error_code": "MCP_TOOL_FORBIDDEN"}),
            403,
        ),
        (ExternalToolError("mcp.not_configured", "未設定"), 409),
        (ExternalToolError("mcp.transport_error", "通信失敗"), 502),
    ],
)
def test_candidate_error_status(error: ExternalToolError, expected: int) -> None:
    store = SimpleNamespace(get_mcp=lambda _name: SimpleNamespace(label="接続 A"))
    with (
        patch("app.features.agent.config.runtime_config_store", store),
        patch("app.features.agent.data_scope.McpConnectionClient") as client,
    ):
        client.return_value.call_tool.side_effect = error
        with pytest.raises(DataScopeCandidatesError) as caught:
            list_candidates("nl2sql", user_uuid="synthetic")
    assert caught.value.status_code == expected
