"""全 API の problem response 契約を検証する。"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest
from fastapi import Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from app.api.problems import api_problem_response, validation_field_problems
from app.features.nl2sql.service import DbAdminOperationFailed
from app.main import (
    db_admin_operation_failed_handler,
    http_exception_problem_handler,
    unhandled_problem_handler,
)


def _request(request_id: str = "problem-test") -> Request:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/test",
            "raw_path": b"/api/test",
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("test", 80),
            "client": ("127.0.0.1", 1234),
            "root_path": "",
        }
    )
    request.state.request_id = request_id
    return request


def _body(response: JSONResponse) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(bytes(response.body)))


@pytest.mark.parametrize("status_code", [429, 503])
def test_retryable_service_problem_keeps_legacy_envelope(status_code: int) -> None:
    response = api_problem_response(
        _request(),
        status_code=status_code,
        detail="データベースへ接続できません。時間をおいて再試行してください。",
        code="NL2SQL_PERSISTENCE_UNAVAILABLE",
    )
    body = _body(response)

    assert response.status_code == status_code
    assert body["data"] is None
    assert body["error_messages"] == [body["problem"]["detail"]]
    assert body["problem"]["retryable"] is True
    assert body["problem"]["request_id"] == "problem-test"


def test_unhandled_problem_does_not_expose_exception_detail() -> None:
    secret = "ORA-01017 password=DoNotExpose"
    response = asyncio.run(unhandled_problem_handler(_request(), RuntimeError(secret)))
    serialized = bytes(response.body).decode("utf-8")

    assert response.status_code == 500
    assert secret not in serialized
    assert "ORA-01017" not in serialized
    assert "problem-test" in serialized


def test_db_admin_problem_keeps_raw_oracle_detail_out_of_response() -> None:
    response = asyncio.run(
        db_admin_operation_failed_handler(
            _request(),
            DbAdminOperationFailed(
                error_code="ORA-01031",
                summary="この操作を実行する権限がありません。",
                cause="実行権限が不足しています。",
                actions=["管理者に権限を確認してください。"],
                operation="execute",
                raw_message="ORA-01031 password=DoNotExpose",
            ),
        )
    )
    serialized = bytes(response.body).decode("utf-8")

    assert response.status_code == 500
    assert "ORA-01031 password" not in serialized
    assert "raw_message" not in serialized
    assert "この操作を実行する権限がありません。" in serialized


def test_http_exception_problem_keeps_structured_field_errors() -> None:
    response = asyncio.run(
        http_exception_problem_handler(
            _request(),
            StarletteHTTPException(
                status_code=422,
                detail=cast(
                    Any,
                    {
                        "code": "NL2SQL_PROFILE_NAME_CONFLICT",
                        "message_ja": "業務プロファイル名「SALES_PROFILE」は既に使用されています。",
                        "field_errors": [
                            {
                                "pointer": "/name",
                                "code": "profile_name_conflict",
                                "message": "同じ名称の業務プロファイルが既に存在します。",
                            }
                        ],
                    },
                ),
            ),
        )
    )
    body = _body(response)

    assert response.status_code == 422
    assert body["error_code"] == "NL2SQL_PROFILE_NAME_CONFLICT"
    assert body["problem"]["field_errors"] == [
        {
            "pointer": "/name",
            "code": "profile_name_conflict",
            "message": "同じ名称の業務プロファイルが既に存在します。",
        }
    ]


def test_validation_error_uses_json_pointer_for_nested_array() -> None:
    problems = validation_field_problems(
        [
            {
                "loc": ("body", "items", 1, "name"),
                "type": "missing",
                "msg": "Field required",
            }
        ]
    )

    assert problems[0].pointer == "/items/1/name"
    assert problems[0].message == "必須の項目です。入力してください。"
    assert problems[0].location == "items[1].name"
    assert problems[0].raw_location == "body.items.1.name"
    assert problems[0].raw_message == "Field required"
