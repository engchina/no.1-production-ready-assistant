"""入力の検査に接続障害の対処を足さない。"""

import json

import pytest
from starlette.exceptions import HTTPException
from starlette.requests import Request

from app.main import http_exception_problem_handler


@pytest.mark.asyncio
async def test_validation_error_keeps_input_guidance() -> None:
    request = Request({"type": "http", "method": "POST", "path": "/api/test", "headers": []})
    response = await http_exception_problem_handler(
        request, HTTPException(400, "SELECT の文を1つだけ入力してください。")
    )
    text = json.dumps(json.loads(bytes(response.body)), ensure_ascii=False)
    assert "データベースの状態と実行権限" not in text
    assert "SELECT の文を1つだけ" in text
