"""ページングの共通部品（#1266）。"""

from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from pr_backend_core import CursorPage, Page
from pr_backend_core.api import (
    DEFAULT_PAGE_LIMIT,
    CursorParams,
    InvalidCursorError,
    OffsetParams,
    cursor_page,
    cursor_params,
    decode_cursor,
    decode_offset_cursor,
    empty_page,
    encode_cursor,
    encode_offset_cursor,
    has_next_page,
    next_offset_cursor,
    offset_fetch_binds,
    offset_fetch_clause,
    offset_params,
    paginate,
    paginate_slice,
)


def test_paginate_has_next_is_the_single_formula() -> None:
    assert has_next_page(offset=0, count=3, total=10) is True
    assert has_next_page(offset=9, count=1, total=10) is False
    page: Page[int] = paginate([1, 2, 3], total=10, limit=3, offset=0)
    assert page.has_next is True
    assert paginate([10], total=10, limit=3, offset=9).has_next is False
    # 最後のページが limit より短い（count で判定するので has_next は False）。
    assert paginate([9, 10], total=10, limit=3, offset=8).has_next is False


def test_paginate_slice_and_empty_page() -> None:
    params = OffsetParams(limit=2, offset=2)
    page = paginate_slice(["a", "b", "c", "d", "e"], params)
    assert page.model_dump() == {
        "items": ["c", "d"],
        "total": 5,
        "limit": 2,
        "offset": 2,
        "has_next": True,
    }
    assert paginate_slice([], params).items == []
    empty: Page[str] = empty_page(params)
    assert empty.model_dump() == {
        "items": [],
        "total": 0,
        "limit": 2,
        "offset": 2,
        "has_next": False,
    }
    assert empty_page(limit=7).limit == 7
    assert empty_page().limit == DEFAULT_PAGE_LIMIT


def test_default_page_limit_is_25() -> None:
    """一覧の既定の 1 ページは 25 件（frontend の `DEFAULT_PAGE_SIZE` と同じ。#1952）。"""
    assert DEFAULT_PAGE_LIMIT == 25
    app = FastAPI()

    @app.get("/items")
    def items(paging: Annotated[OffsetParams, Depends(offset_params())]) -> dict[str, int]:
        return {"limit": paging.limit, "offset": paging.offset}

    assert TestClient(app).get("/items").json() == {"limit": 25, "offset": 0}


def test_offset_params_dependency_validates_range() -> None:
    app = FastAPI()

    @app.get("/items")
    def items(
        paging: Annotated[OffsetParams, Depends(offset_params(default=20, max_limit=100))],
    ) -> dict[str, int]:
        return {"limit": paging.limit, "offset": paging.offset, "end": paging.end}

    client = TestClient(app)
    assert client.get("/items").json() == {"limit": 20, "offset": 0, "end": 20}
    assert client.get("/items", params={"limit": 100, "offset": 5}).json() == {
        "limit": 100,
        "offset": 5,
        "end": 105,
    }
    assert client.get("/items", params={"limit": 101}).status_code == 422
    assert client.get("/items", params={"limit": 0}).status_code == 422
    assert client.get("/items", params={"offset": -1}).status_code == 422
    with pytest.raises(ValueError):
        offset_params(default=500, max_limit=100)


def test_cursor_params_dependency_and_cursor_page() -> None:
    app = FastAPI()

    @app.get("/items")
    def items(
        paging: Annotated[CursorParams, Depends(cursor_params(default=10, max_limit=50))],
    ) -> dict[str, object]:
        return {"cursor": paging.cursor, "limit": paging.limit}

    client = TestClient(app)
    assert client.get("/items").json() == {"cursor": None, "limit": 10}
    assert client.get("/items", params={"cursor": "abc", "limit": 50}).json() == {
        "cursor": "abc",
        "limit": 50,
    }
    assert client.get("/items", params={"cursor": "x" * 513}).status_code == 422
    assert client.get("/items", params={"limit": 51}).status_code == 422

    page: CursorPage[int] = cursor_page([1, 2], next_cursor="", total=None)
    assert page.model_dump() == {"items": [1, 2], "next_cursor": None, "total": None}
    assert cursor_page([1], next_cursor="c2", total=3).next_cursor == "c2"


def test_cursor_codec_round_trip_and_errors() -> None:
    cursor = encode_cursor({"id": "abc", "at": "2026-10-08T00:00:00Z"})
    assert "=" not in cursor
    assert decode_cursor(cursor, required=("id", "at")) == {
        "at": "2026-10-08T00:00:00Z",
        "id": "abc",
    }
    assert decode_cursor(None) is None
    assert decode_cursor("") is None
    with pytest.raises(InvalidCursorError):
        decode_cursor("not base64 ***")
    with pytest.raises(InvalidCursorError):
        decode_cursor(encode_cursor({"id": "abc"}), required=("id", "at"))
    with pytest.raises(InvalidCursorError):
        decode_cursor(cursor[:-3] + "zz")


def test_offset_cursor_round_trip() -> None:
    assert decode_offset_cursor(None) == 0
    assert decode_offset_cursor(encode_offset_cursor(40)) == 40
    assert next_offset_cursor(offset=0, count=20, total=45) == encode_offset_cursor(20)
    assert next_offset_cursor(offset=40, count=5, total=45) is None
    with pytest.raises(InvalidCursorError):
        decode_offset_cursor(encode_cursor({"offset": -1}))
    with pytest.raises(InvalidCursorError):
        decode_offset_cursor(encode_cursor({"offset": "1"}))


def test_offset_fetch_clause_and_binds() -> None:
    assert offset_fetch_clause() == "OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY"
    assert offset_fetch_clause(with_limit=False) == "OFFSET :offset ROWS"
    assert offset_fetch_clause(offset_bind="page_offset", limit_bind="page_limit") == (
        "OFFSET :page_offset ROWS FETCH NEXT :page_limit ROWS ONLY"
    )
    assert offset_fetch_binds(OffsetParams(limit=10, offset=30)) == {"offset": 30, "limit": 10}
    assert offset_fetch_binds(offset=5) == {"offset": 5}
    assert offset_fetch_binds(offset=5, limit=2, offset_bind="o", limit_bind="l") == {
        "o": 5,
        "l": 2,
    }
