"""画面の必須・数値の検証と backend の検証をそろえる（#540 / #541）。

画面の検証は先回りで、正本は backend。API を直接呼んでも、画面と同じ規則・日本語の文言で拒否する。
どのテストも拒否だけを確かめ、設定を書き換えない（並列でも直列でも通る）。
"""

from __future__ import annotations

import httpx
import pytest

from app.features.agent.runtime import RunCreateRequest
from app.main import app


async def _request(method: str, path: str, payload: dict[str, object]) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.request(method, path, json=payload)


@pytest.mark.parametrize("goal", ["", "   "], ids=["empty", "blank"])
def test_run_goal_is_required(goal: str) -> None:
    with pytest.raises(ValueError, match="ゴールを入力してください。"):
        RunCreateRequest(goal=goal)
    assert RunCreateRequest(goal=" 要点を整理する ").goal == " 要点を整理する "


@pytest.mark.asyncio
async def test_run_with_empty_goal_is_rejected_with_field_message() -> None:
    response = await _request("POST", "/api/runs", {"goal": " "})
    assert response.status_code == 422
    # 自前の検証の文は位置と `Value error, ` の接頭辞を付けずにそのまま返す（#1065）。
    body = response.json()
    assert body["error_messages"] == ["ゴールを入力してください。"]
    assert body["error_code"] == "REQUEST_VALIDATION_FAILED"
    (field_error,) = body["problem"]["field_errors"]
    assert field_error["pointer"] == "/goal"
    assert field_error["raw_message"] == "Value error, ゴールを入力してください。"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("POST", "/api/settings/mcp-connections", {"server_id": "t0", "timeout_seconds": 0}),
        ("PATCH", "/api/settings/mcp-connections/rag", {"timeout_seconds": -1}),
        ("PATCH", "/api/settings/mcp-connections/nl2sql", {"timeout_seconds": 901}),
    ],
    ids=["mcp-create-zero", "mcp-patch-negative", "mcp-patch-over"],
)
async def test_mcp_timeout_is_never_saved_as_zero(
    method: str, path: str, payload: dict[str, object]
) -> None:
    response = await _request(method, path, payload)
    assert response.status_code == 422
    (message,) = response.json()["error_messages"]
    assert message.endswith("タイムアウト秒は 0 より大きく 900 以下の数値を入力してください。")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"server_id": "a__b"}, "接続 ID は英数字で始まる"),
        ({"server_id": "control-plane"}, "接続 ID は英数字で始まる"),
        ({"server_id": "erp", "base_url": "ftp://erp"}, "MCP の URL は http:// または https://"),
    ],
    ids=["double-underscore", "reserved", "url-scheme"],
)
async def test_mcp_connection_id_and_url_are_validated(
    payload: dict[str, object], message: str
) -> None:
    response = await _request("POST", "/api/settings/mcp-connections", payload)
    assert response.status_code == 422
    (error,) = response.json()["error_messages"]
    assert message in error
