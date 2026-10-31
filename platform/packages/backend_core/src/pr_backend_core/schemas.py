"""共通スキーマ。3 サービス共通の API レスポンス契約。

RAG で実証済みの envelope（data / error_messages / warning_messages）を標準として採用する。
フロントの共通エラーハンドリング（TanStack Query）はこの形を前提にできる。
"""

from typing import Literal

from pydantic import BaseModel, Field

type JsonValue = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


class ApiResponse[T](BaseModel):
    """API 共通レスポンス形。"""

    data: T | None = None
    error_messages: list[str] = Field(default_factory=list)
    warning_messages: list[str] = Field(default_factory=list)


class Page[T](BaseModel):
    """ページング済みレスポンス。"""

    items: list[T]
    total: int
    limit: int
    offset: int
    has_next: bool


class CursorPage[T](BaseModel):
    """カーソル型のページング済みレスポンス（任意のページへ移らない一覧。#1266）。

    `next_cursor` は続きが無ければ None（空文字は使わない）。`total` は数えるのが重い一覧では None。
    製品固有の付加情報（件数の内訳・version など）は、この class を継承して足す。
    """

    items: list[T]
    next_cursor: str | None = None
    total: int | None = None


class HealthData(BaseModel):
    """ヘルスチェック結果。"""

    status: str
    version: str
    message: str | None = None
    checks: dict[str, str] = Field(default_factory=dict)


# readiness check の生ステータス語彙（サービス横断で共通化）。
ReadinessStatus = Literal[
    "ok",
    "missing",
    "invalid",
    "missing_credentials",
    "unreachable",
    "error",
]
