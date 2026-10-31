"""文書の分類の一覧（大分類 → 中分類 → 小分類）の API（#2131）。

一覧は文書の詳細・RAG 検索の分類の選択肢にも使う（読むのは文書・検索の画面の権限でもよい）。
作成・名前の変更・並べ替え・削除は「文書の分類」の画面の権限だけ。規則は ``DocumentCategoryStore``。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.clients.document_category_store import (
    CategoryConflictError,
    CategoryDepthError,
    CategoryInUseError,
    CategoryNode,
    CategoryNotFoundError,
    DocumentCategoryStore,
)
from app.schemas.common import ApiResponse
from app.schemas.document_category import (
    DocumentCategory,
    DocumentCategoryCreate,
    DocumentCategoryMove,
    DocumentCategoryRenamed,
    DocumentCategoryUpdate,
)

router = APIRouter()

NOT_FOUND_MESSAGE = (
    "分類が見つかりません。ほかの利用者が削除した可能性があります。画面を再読み込みしてください。"
)
CONFLICT_MESSAGE = (
    "同じ分類の下に同じ名前の分類があります"
    "（先頭の番号「10_」などの違いは同じ名前として扱います）。"
)
DEPTH_MESSAGE = "小分類の下には分類を作れません。"


def _category(node: CategoryNode) -> DocumentCategory:
    return DocumentCategory(
        id=node.id,
        name=node.name,
        level=node.level,
        parent_id=node.parent_id,
        document_count=node.document_count,
        children=[_category(child) for child in node.children],
    )


async def _tree() -> list[DocumentCategory]:
    return [_category(node) for node in await DocumentCategoryStore().list_tree()]


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, CategoryNotFoundError):
        return HTTPException(status_code=404, detail=NOT_FOUND_MESSAGE)
    if isinstance(exc, CategoryConflictError):
        return HTTPException(status_code=409, detail=CONFLICT_MESSAGE)
    if isinstance(exc, CategoryDepthError):
        return HTTPException(status_code=422, detail=DEPTH_MESSAGE)
    if isinstance(exc, CategoryInUseError):
        reasons = []
        if exc.child_count:
            reasons.append(f"下に {exc.child_count} 件の分類があります")
        if exc.document_count:
            reasons.append(f"{exc.document_count} 件の文書が使っています")
        return HTTPException(
            status_code=409,
            detail="この分類は削除できません（"
            + "。".join(reasons)
            + "）。先に下の分類を削除するか、"
            "文書の分類を変えてください。",
        )
    return HTTPException(status_code=422, detail=str(exc))


@router.get("", response_model=ApiResponse[list[DocumentCategory]])
async def list_document_categories() -> ApiResponse[list[DocumentCategory]]:
    """分類の一覧を、並び順の木で返す（文書の数つき）。"""
    return ApiResponse(data=await _tree())


@router.post("", status_code=201, response_model=ApiResponse[list[DocumentCategory]])
async def create_document_category(
    request: DocumentCategoryCreate,
) -> ApiResponse[list[DocumentCategory]]:
    """分類を作る（親の下の最後に並べる）。作った後の一覧を返す。"""
    try:
        await DocumentCategoryStore().create(parent_id=request.parent_id, name=request.name)
    except (CategoryNotFoundError, CategoryConflictError, CategoryDepthError, ValueError) as exc:
        raise _http_error(exc) from exc
    return ApiResponse(data=await _tree())


@router.patch("/{category_id}", response_model=ApiResponse[DocumentCategoryRenamed])
async def rename_document_category(
    category_id: str, request: DocumentCategoryUpdate
) -> ApiResponse[DocumentCategoryRenamed]:
    """名前を変え、その分類を使っている文書の分類の値も変える。"""
    try:
        count = await DocumentCategoryStore().rename(category_id, request.name)
    except (CategoryNotFoundError, CategoryConflictError, ValueError) as exc:
        raise _http_error(exc) from exc
    return ApiResponse(data=DocumentCategoryRenamed(updated_document_count=count))


@router.post("/{category_id}/move", response_model=ApiResponse[list[DocumentCategory]])
async def move_document_category(
    category_id: str, request: DocumentCategoryMove
) -> ApiResponse[list[DocumentCategory]]:
    """同じ親の下で 1 つ前・後へ動かす。動かした後の一覧を返す。"""
    try:
        await DocumentCategoryStore().move(category_id, -1 if request.direction == "up" else 1)
    except CategoryNotFoundError as exc:
        raise _http_error(exc) from exc
    return ApiResponse(data=await _tree())


@router.delete("/{category_id}", response_model=ApiResponse[list[DocumentCategory]])
async def delete_document_category(category_id: str) -> ApiResponse[list[DocumentCategory]]:
    """分類を削除する。下に分類がある・文書が使っているときは 409。削除した後の一覧を返す。"""
    try:
        await DocumentCategoryStore().delete(category_id)
    except (CategoryNotFoundError, CategoryInUseError) as exc:
        raise _http_error(exc) from exc
    return ApiResponse(data=await _tree())
