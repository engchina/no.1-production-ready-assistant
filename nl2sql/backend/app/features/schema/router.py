"""Oracle schema catalog router."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pr_backend_core import ApiResponse
from pr_backend_core.api import CursorParams, InvalidCursorError, cursor_params

from app.api.problems import invalid_cursor_exception
from app.features.nl2sql.models import (
    SchemaCatalog,
    SchemaCatalogHead,
    SchemaObjectDetail,
    SchemaObjectPage,
    SchemaOwnersData,
    SchemaRefreshActiveJobData,
    SchemaRefreshJob,
)
from app.features.nl2sql.object_identity import normalize_object_part
from app.features.nl2sql.service import nl2sql_service

router = APIRouter(prefix="/schema", tags=["schema"])


@router.get("/owners", response_model=ApiResponse[SchemaOwnersData])
def owners() -> ApiResponse[SchemaOwnersData]:
    """現在の接続ユーザーが参照可能な業務 schema 一覧を返す。"""

    return ApiResponse(data=nl2sql_service.get_schema_owners())


@router.get("/catalog", response_model=ApiResponse[SchemaCatalog])
def catalog(response: Response) -> ApiResponse[SchemaCatalog]:
    """NL2SQL の表/列選択 UI が利用する schema catalog を返す。"""
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = "Wed, 30 Sep 2026 00:00:00 GMT"
    response.headers["Link"] = '</api/schema/objects>; rel="successor-version"'
    return ApiResponse(data=nl2sql_service.get_catalog())


@router.get("/catalog/head", response_model=ApiResponse[SchemaCatalogHead])
def catalog_head(
    response: Response,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> ApiResponse[SchemaCatalogHead] | Response:
    """Catalog 全体を decode せず active metadata だけを返す。"""
    head = nl2sql_service.get_catalog_head()
    quoted_etag = f'"{head.etag}"'
    if head.etag and if_none_match == quoted_etag:
        return Response(status_code=304, headers={"ETag": quoted_etag})
    if head.etag:
        response.headers["ETag"] = quoted_etag
    return ApiResponse(data=head)


@router.get("/objects", response_model=ApiResponse[SchemaObjectPage])
def search_objects(
    response: Response,
    paging: Annotated[CursorParams, Depends(cursor_params(default=50, max_limit=100))],
    q: str = "",
    owner: str = "",
    type: str = "",  # noqa: A002 - public query parameter name
    row_state: str = "",
    profile_id: str | None = None,
    include_counts: bool = True,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> ApiResponse[SchemaObjectPage] | Response:
    """Schema picker 用 keyset page。"""
    if row_state not in {"", "all", "with_rows", "empty_rows", "unknown_rows"}:
        raise HTTPException(status_code=422, detail="row_state が不正です。")
    try:
        page = nl2sql_service.search_schema_objects(
            cursor=paging.cursor,
            limit=paging.limit,
            query=q,
            owner=owner,
            object_type=type,
            profile_id=profile_id,
            row_state=row_state,
            include_counts=include_counts,
        )
    except InvalidCursorError as exc:
        raise invalid_cursor_exception(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    quoted_etag = f'"schema-{page.catalog_version}"'
    if if_none_match == quoted_etag:
        return Response(status_code=304, headers={"ETag": quoted_etag})
    response.headers["ETag"] = quoted_etag
    return ApiResponse(data=page)


@router.get("/objects/{owner}/{object_name}", response_model=ApiResponse[SchemaObjectDetail])
def object_detail(
    owner: str,
    object_name: str,
    response: Response,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> ApiResponse[SchemaObjectDetail] | Response:
    """選択 object の columns/constraints/dependency だけを返す。

    `owner` / `object_name` は Oracle の引用規則で解釈する（引用なしは大文字、
    `"Mixed_Case"` は大文字小文字を保持）。大文字の同名表と取り違えない（#563）。
    """
    try:
        catalog_owner = normalize_object_part(owner)
        catalog_object_name = normalize_object_part(object_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    detail = nl2sql_service.get_schema_object(catalog_owner, catalog_object_name)
    if detail is None:
        raise HTTPException(status_code=404, detail="Schema object が見つかりません。")
    quoted_etag = f'"{detail.etag}"'
    if detail.etag and if_none_match == quoted_etag:
        return Response(status_code=304, headers={"ETag": quoted_etag})
    if detail.etag:
        response.headers["ETag"] = quoted_etag
    return ApiResponse(data=detail)


@router.post(
    "/refresh-jobs",
    response_model=ApiResponse[SchemaRefreshJob],
    status_code=status.HTTP_202_ACCEPTED,
)
def start_refresh_job() -> ApiResponse[SchemaRefreshJob]:
    """Schema refresh を永続 job として投入し即時に返す。"""
    return ApiResponse(data=nl2sql_service.start_schema_refresh_job())


@router.get(
    "/refresh-jobs/active",
    response_model=ApiResponse[SchemaRefreshActiveJobData],
)
def active_refresh_job() -> ApiResponse[SchemaRefreshActiveJobData]:
    """画面移動・再読込後も追跡できる実行中 job を返す。"""

    return ApiResponse(
        data=SchemaRefreshActiveJobData(active_job=nl2sql_service.get_active_schema_refresh_job())
    )


@router.get("/refresh-jobs/{job_id}", response_model=ApiResponse[SchemaRefreshJob])
def refresh_job(job_id: str) -> ApiResponse[SchemaRefreshJob]:
    job = nl2sql_service.get_schema_refresh_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Schema refresh job が見つかりません。")
    return ApiResponse(data=job)
