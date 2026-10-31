"""入力の検証エラー（422）の利用者向けの日本語の文のテスト（#1065）。"""

from typing import Annotated, Any, Literal

import pytest
from fastapi import APIRouter, Query
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

from pr_backend_core import create_app
from pr_backend_core.api.validation import (
    VALIDATION_ERROR_CODE,
    validation_error_message,
    validation_error_messages,
    validation_field_errors,
    validation_tool_errors,
)


class _Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    query: str = Field(min_length=1, max_length=10)
    tags: list[str] = Field(default_factory=list)


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cases: list[_Case] = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=50)
    suite: Literal["standard", "strict"] = "standard"
    note: str | int | None = None

    @field_validator("cases")
    @classmethod
    def _unique_ids(cls, value: list[_Case]) -> list[_Case]:
        ids = [case.id for case in value]
        if len(ids) != len(set(ids)):
            raise ValueError("ケースの id が重複しています。")
        if any(case.id == "english" for case in value):
            raise ValueError("english only message")
        if any(case.id == "custom" for case in value):
            raise PydanticCustomError("case_reserved", "予約された id は使えません。")
        return value


def _client() -> TestClient:
    router = APIRouter()

    @router.post("/items")
    async def create_item(payload: _Payload) -> dict[str, str]:
        return {"ok": "yes"}

    @router.get("/items")
    async def list_items(
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
    ) -> dict[str, int]:
        return {"limit": limit}

    app = create_app(
        service_name="test-service",
        version="1.0.0",
        cors_origins=[],
        api_router=router,
        enable_metrics=False,
    )
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"cases": [{"id": "a"}]}, "cases[0].query: 必須の項目です。入力してください。"),
        ({"cases": [{"id": "a", "query": ""}]}, "cases[0].query: 1 文字以上で入力してください。"),
        (
            {"cases": [{"id": "a", "query": "x" * 11}]},
            "cases[0].query: 10 文字以内で入力してください。",
        ),
        (
            {"cases": [{"id": "a", "query": "q", "tags": "x"}]},
            "cases[0].tags: 配列（[ ]）で指定してください。",
        ),
        ({"cases": [{"id": "a", "query": "q"}], "top_k": "x"}, "top_k: 整数で入力してください。"),
        (
            {"cases": [{"id": "a", "query": "q"}], "top_k": 0},
            "top_k: 1 以上の値を入力してください。",
        ),
        (
            {"cases": [{"id": "a", "query": "q"}], "top_k": 51},
            "top_k: 50 以下の値を入力してください。",
        ),
        (
            {"cases": [{"id": "a", "query": "q", "foo": 1}]},
            "cases[0].foo: この項目は指定できません。",
        ),
        (
            {"cases": [{"id": "a", "query": "q"}], "suite": "loose"},
            "suite: 指定できる値ではありません（指定できる値: 'standard'、'strict'）。",
        ),
        ({"cases": []}, "cases: 1 件以上指定してください。"),
        ({"cases": "x"}, "cases: 配列（[ ]）で指定してください。"),
        # union の候補の型の名前（str / int）は位置に出さない。
        ({"cases": [{"id": "a", "query": "q"}], "note": [1]}, "note: 文字列で入力してください。"),
    ],
    ids=[
        "missing",
        "too-short",
        "too-long",
        "list-type",
        "int-parsing",
        "greater-equal",
        "less-equal",
        "extra",
        "literal",
        "too-short-list",
        "list-type-root",
        "union",
    ],
)
def test_builtin_errors_are_japanese_with_user_location(
    body: dict[str, Any], expected: str
) -> None:
    response = _client().post("/api/items", json=body)

    assert response.status_code == 422
    assert expected in response.json()["error_messages"]


def test_custom_value_error_is_returned_as_written() -> None:
    """自前の検証の日本語の文は、`Value error, ` の接頭辞と位置を付けずにそのまま返す。"""
    response = _client().post(
        "/api/items",
        json={"cases": [{"id": "a", "query": "q"}, {"id": "a", "query": "r"}]},
        headers={"X-Request-ID": "req-422"},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["error_messages"] == ["ケースの id が重複しています。"]
    assert body["error_code"] == VALIDATION_ERROR_CODE
    assert response.headers["x-request-id"] == "req-422"
    problem = body["problem"]
    assert problem["status"] == 422
    assert problem["code"] == VALIDATION_ERROR_CODE
    assert problem["detail"] == "ケースの id が重複しています。"
    assert problem["request_id"] == "req-422"
    assert problem["retryable"] is False
    # 技術的な原文（loc・type・msg）は field_errors に残す。入力値は含めない。
    assert problem["field_errors"] == [
        {
            "pointer": "/cases",
            "code": "value_error",
            "message": "ケースの id が重複しています。",
            "location": "cases",
            "raw_location": "body.cases",
            "raw_message": "Value error, ケースの id が重複しています。",
        }
    ]


def test_english_custom_message_and_pydantic_custom_error() -> None:
    client = _client()
    english = client.post("/api/items", json={"cases": [{"id": "english", "query": "q"}]})
    assert english.json()["error_messages"] == ["cases: 入力内容を確認してください。"]
    assert english.json()["problem"]["field_errors"][0]["raw_message"] == (
        "Value error, english only message"
    )

    custom = client.post("/api/items", json={"cases": [{"id": "custom", "query": "q"}]})
    assert custom.json()["error_messages"] == ["予約された id は使えません。"]
    assert custom.json()["problem"]["field_errors"][0]["code"] == "case_reserved"


def test_query_parameter_and_broken_json() -> None:
    client = _client()
    query = client.get("/api/items?limit=1000")
    assert query.status_code == 422
    assert query.json()["error_messages"] == ["limit: 100 以下の値を入力してください。"]
    assert query.json()["problem"]["field_errors"][0]["raw_location"] == "query.limit"

    broken = client.post(
        "/api/items", content=b'{"cases": [', headers={"Content-Type": "application/json"}
    )
    assert broken.status_code == 422
    assert broken.json()["error_messages"] == ["JSON の形式が正しくありません。"]
    assert broken.json()["problem"]["field_errors"][0]["pointer"] == "/"


def test_messages_are_deduplicated_and_unknown_types_fall_back() -> None:
    assert validation_error_messages(
        [
            {"type": "missing", "loc": ("body", "a"), "msg": "Field required"},
            {"type": "missing", "loc": ("body", "a"), "msg": "Field required"},
        ]
    ) == ["a: 必須の項目です。入力してください。"]
    assert (
        validation_error_message({"type": "is_instance_of", "loc": ("body", "x"), "msg": "Bad"})
        == "x: 入力内容を確認してください。"
    )
    # ctx が欠けていても例外にしない。
    assert (
        validation_error_message({"type": "string_too_short", "loc": ("body",), "msg": "short"})
        == "入力内容を確認してください。"
    )
    assert (
        validation_field_errors([{"type": "missing", "loc": ("body", "a/b", 0)}])[0]["pointer"]
        == "/a~1b/0"
    )


def test_tool_errors_use_user_location_and_japanese() -> None:
    assert validation_tool_errors(
        [{"type": "missing", "loc": ("cases", 0, "query"), "msg": "Field required"}]
    ) == [
        {
            "loc": "cases[0].query",
            "message": "必須の項目です。入力してください。",
            "type": "missing",
            "raw_message": "Field required",
        }
    ]


class _ToolInput(BaseModel):
    """MCP のツールの入力（引数の名前が要求の出どころと同じ語）。"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    body: int = 1
    path: list[str] = Field(default_factory=list)


def test_tool_errors_keep_arguments_named_like_request_sources() -> None:
    """ツールの入力の loc の先頭は引数の名前で、`query` などを出どころとして除かない（#2505）。"""
    with pytest.raises(Exception) as caught:
        _ToolInput.model_validate({"body": "x", "path": "y", "Query": "z"})
    errors = validation_tool_errors(caught.value.errors())  # type: ignore[attr-defined]

    assert {(item["loc"], item["type"]) for item in errors} == {
        ("query", "missing"),
        ("body", "int_parsing"),
        ("path", "list_type"),
        ("Query", "extra_forbidden"),
    }
    # HTTP の 422（要求の出どころを持つ loc）は従来どおり出どころを除く。
    assert (
        validation_field_errors([{"type": "missing", "loc": ("query", "limit")}])[0]["location"]
        == "limit"
    )
    assert (
        validation_field_errors([{"type": "missing", "loc": ("body", "query")}])[0]["location"]
        == "query"
    )
