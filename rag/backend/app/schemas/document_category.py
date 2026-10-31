"""文書の分類の一覧（大分類 → 中分類 → 小分類）の API の型（#2131）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DocumentCategory(BaseModel):
    """分類の 1 件と、その下の分類（並び順）。"""

    id: str
    name: str
    # 1 = 大分類、2 = 中分類、3 = 小分類。
    level: int
    parent_id: str | None = None
    # この分類（とその下の分類）を使っている文書の数。
    document_count: int = 0
    children: list[DocumentCategory] = Field(default_factory=list)


class DocumentCategoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 親の分類（大分類を作るときは省略）。
    parent_id: str | None = Field(default=None, max_length=64)
    name: str = Field(min_length=1, max_length=400)


class DocumentCategoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=400)


class DocumentCategoryMove(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 同じ親の下で 1 つ前（up）・後（down）へ。
    direction: Literal["up", "down"]


class DocumentCategoryRenamed(BaseModel):
    """名前の変更の結果（分類の値を変えた文書の数）。"""

    updated_document_count: int
