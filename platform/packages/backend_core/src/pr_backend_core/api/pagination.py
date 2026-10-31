"""ページングユーティリティ（サービス横断で共通。#1266）。

- offset 型: `offset_params()` の依存で `limit` / `offset` を受け取り（上限は `Query(le=)` で
  422）、`paginate()` / `paginate_slice()` / `empty_page()` で `Page[T]` を組み立てる。
  `has_next` の式はここだけ。
- カーソル型: `cursor_params()` の依存で `cursor` / `limit` を受け取り、`CursorPage[T]` を返す。
  カーソルの文字列は `encode_cursor()` / `decode_cursor()`（base64 の JSON）で作る。壊れたカーソルは
  `InvalidCursorError`（製品の router で 422 にする）。
- Oracle の `OFFSET … FETCH NEXT` は `offset_fetch_clause()`（bind 名を `:offset` / `:limit` に
  そろえる）。
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import Query

from ..schemas import CursorPage, Page

#: 一覧の既定の 1 ページの件数（frontend の `DEFAULT_PAGE_SIZE` と同じ 25 件。#1952）。
DEFAULT_PAGE_LIMIT = 25
#: 一覧の `limit` の既定の上限。
DEFAULT_PAGE_LIMIT_MAX = 200
#: 権限管理の「利用できる対象」の一覧（`ListPicker` の候補）の `limit` の上限（3 製品で同じ）。
ACCESS_TARGET_PAGE_LIMIT_MAX = 100
#: カーソルの文字列の上限（`Query(max_length=)`）。
CURSOR_MAX_LENGTH = 512


@dataclass(frozen=True, slots=True)
class OffsetParams:
    """offset 型のページングの query（`limit` / `offset`）。"""

    limit: int
    offset: int

    @property
    def end(self) -> int:
        """このページの終端（exclusive）。list の slice に使う。"""
        return self.offset + self.limit


@dataclass(frozen=True, slots=True)
class CursorParams:
    """カーソル型のページングの query（`cursor` / `limit`）。`cursor` は最初のページで None。"""

    cursor: str | None
    limit: int


def offset_params(
    *, default: int = DEFAULT_PAGE_LIMIT, max_limit: int = DEFAULT_PAGE_LIMIT_MAX
) -> Callable[..., OffsetParams]:
    """`limit` / `offset` を受け取る FastAPI の依存を作る。

    使い方: `paging: Annotated[OffsetParams, Depends(offset_params(default=50, max_limit=200))]`。
    `limit` は 1〜`max_limit`、`offset` は 0 以上で、外れた値は FastAPI が 422 にする
    （黙って clamp しない）。
    """
    if not 1 <= default <= max_limit:
        raise ValueError(f"default は 1..max_limit（{max_limit}）の範囲で指定する: {default}")

    def dependency(
        limit: int = Query(default, ge=1, le=max_limit, description="1 ページの件数"),
        offset: int = Query(0, ge=0, description="先頭から飛ばす件数"),
    ) -> OffsetParams:
        return OffsetParams(limit=limit, offset=offset)

    return dependency


def cursor_params(
    *, default: int = DEFAULT_PAGE_LIMIT, max_limit: int = DEFAULT_PAGE_LIMIT_MAX
) -> Callable[..., CursorParams]:
    """`cursor` / `limit` を受け取る FastAPI の依存を作る（カーソル型の一覧）。"""
    if not 1 <= default <= max_limit:
        raise ValueError(f"default は 1..max_limit（{max_limit}）の範囲で指定する: {default}")

    def dependency(
        cursor: str | None = Query(
            None, min_length=1, max_length=CURSOR_MAX_LENGTH, description="前の応答の next_cursor"
        ),
        limit: int = Query(default, ge=1, le=max_limit, description="1 ページの件数"),
    ) -> CursorParams:
        return CursorParams(cursor=cursor, limit=limit)

    return dependency


def has_next_page(*, offset: int, count: int, total: int) -> bool:
    """次のページがあるか（offset + 当該ページの件数が total 未満）。`Page.has_next` の唯一の式。"""
    return offset + count < total


def paginate[T](items: Sequence[T], *, total: int, limit: int, offset: int) -> Page[T]:
    """items（当該ページ分）と総件数から Page を組み立てる。"""
    page_items = list(items)
    return Page[T](
        items=page_items,
        total=total,
        limit=limit,
        offset=offset,
        has_next=has_next_page(offset=offset, count=len(page_items), total=total),
    )


def paginate_slice[T](items: Sequence[T], params: OffsetParams) -> Page[T]:
    """メモリ上の全件から当該ページを切り出して Page にする（件数の少ない一覧・絞り込んだ後）。"""
    return paginate(
        items[params.offset : params.end],
        total=len(items),
        limit=params.limit,
        offset=params.offset,
    )


def empty_page[T](
    params: OffsetParams | None = None, *, limit: int | None = None, offset: int = 0
) -> Page[T]:
    """0 件の Page（DB の縮退などで一覧を返せないときの fallback）。"""
    if params is not None:
        limit, offset = params.limit, params.offset
    return Page[T](
        items=[], total=0, limit=limit or DEFAULT_PAGE_LIMIT, offset=offset, has_next=False
    )


def cursor_page[T](
    items: Sequence[T], *, next_cursor: str | None, total: int | None = None
) -> CursorPage[T]:
    """カーソル型の応答。`next_cursor` は続きが無ければ None（空文字は使わない）。"""
    return CursorPage[T](items=list(items), next_cursor=next_cursor or None, total=total)


class InvalidCursorError(ValueError):
    """壊れたカーソル（base64 / JSON として読めない、形が違う）。router で 422 にする。"""


def encode_cursor(payload: dict[str, Any]) -> str:
    """カーソルの文字列を作る（JSON → base64url、padding 無し）。値は JSON にできるものだけ。"""
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None, *, required: Sequence[str] = ()) -> dict[str, Any] | None:
    """カーソルの文字列を dict に戻す。None / 空なら None（最初のページ）。

    `required` のキーが無い・JSON の object でない・base64 として読めないときは
    `InvalidCursorError`。
    """
    if not cursor:
        return None
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeError, ValueError) as exc:
        raise InvalidCursorError("cursor が読めません") from exc
    if not isinstance(payload, dict):
        raise InvalidCursorError("cursor の形が違います")
    missing = [key for key in required if key not in payload]
    if missing:
        raise InvalidCursorError(f"cursor に {', '.join(missing)} がありません")
    return payload


def encode_offset_cursor(offset: int) -> str:
    """offset をカーソルにする（任意のページへ移らない一覧で、offset を続きの印にするとき）。"""
    return encode_cursor({"offset": int(offset)})


def decode_offset_cursor(cursor: str | None) -> int:
    """`encode_offset_cursor` の逆。None / 空は 0。負の値や整数でない値は `InvalidCursorError`。"""
    payload = decode_cursor(cursor, required=("offset",))
    if payload is None:
        return 0
    offset = payload["offset"]
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise InvalidCursorError("cursor の offset が不正です")
    return offset


def next_offset_cursor(*, offset: int, count: int, total: int) -> str | None:
    """offset 型のカーソルの `next_cursor`（続きが無ければ None）。"""
    return (
        encode_offset_cursor(offset + count)
        if has_next_page(offset=offset, count=count, total=total)
        else None
    )


def offset_fetch_clause(
    *, offset_bind: str = "offset", limit_bind: str = "limit", with_limit: bool = True
) -> str:
    """Oracle の `OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY`（limit が無ければ OFFSET だけ）。

    SQL の末尾に空白を挟んで連結し、bind は `offset_fetch_binds()` で渡す。
    """
    clause = f"OFFSET :{offset_bind} ROWS"
    if with_limit:
        clause += f" FETCH NEXT :{limit_bind} ROWS ONLY"
    return clause


def offset_fetch_binds(
    params: OffsetParams | None = None,
    *,
    offset: int | None = None,
    limit: int | None = None,
    offset_bind: str = "offset",
    limit_bind: str = "limit",
) -> dict[str, int]:
    """`offset_fetch_clause()` の bind 値。`limit` が無ければ offset だけ。"""
    if params is not None:
        offset, limit = params.offset, params.limit
    binds = {offset_bind: int(offset or 0)}
    if limit is not None:
        binds[limit_bind] = int(limit)
    return binds
