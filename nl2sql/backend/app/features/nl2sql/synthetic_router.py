"""合成生成の受理・状態・結果。全 mutation は domain service で再認可する。"""

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pr_backend_core import ApiResponse

from .oracle_adapter import SelectAiCredentialMissingError
from .select_ai_readiness import (
    assert_select_ai_credential_ready,
    select_ai_credential_missing_detail,
)
from .synthetic_models import SyntheticApplyRequest, SyntheticRunRequest
from .synthetic_service import get_synthetic_service

router = APIRouter(prefix="/synthetic-data", tags=["synthetic-data"])


@router.post("/runs", status_code=202, response_model=ApiResponse[dict[str, Any]])
def create_run(
    req: SyntheticRunRequest, request: Request, response: Response
) -> ApiResponse[dict[str, Any]]:
    service = get_synthetic_service()
    try:
        # 合成データは業務プロファイルの DBMS_CLOUD_AI Profile（Select AI の Credential を
        # 参照する）で作る。Credential が無ければ run を作る前に止める（#1688）。
        assert_select_ai_credential_ready(service.adapter)
    except SelectAiCredentialMissingError as exc:
        raise HTTPException(
            status_code=409, detail=select_ai_credential_missing_detail(exc)
        ) from exc
    run = service.create(req, getattr(request.state, "principal", None))
    response.headers["Location"] = f"/api/nl2sql/synthetic-data/runs/{run.run_id}"
    return ApiResponse(data=run.public())


@router.get("/runs", response_model=ApiResponse[list[dict[str, Any]]])
def list_runs(request: Request) -> ApiResponse[list[dict[str, Any]]]:
    runs = get_synthetic_service().list(getattr(request.state, "principal", None))
    return ApiResponse(data=[r.public() for r in runs])


@router.get("/runs/{run_id}", response_model=ApiResponse[dict[str, Any]])
def get_run(run_id: str, request: Request) -> ApiResponse[dict[str, Any]]:
    run = get_synthetic_service().get(run_id, getattr(request.state, "principal", None))
    return ApiResponse(data=run.public())


@router.post("/runs/{run_id}/apply", response_model=ApiResponse[dict[str, Any]])
def apply_run(
    run_id: str, req: SyntheticApplyRequest, request: Request
) -> ApiResponse[dict[str, Any]]:
    run = get_synthetic_service().review(
        run_id,
        getattr(request.state, "principal", None),
        confirmation=req.confirmation,
        previews=req.previews,
    )
    return ApiResponse(data=run.public())


@router.post("/runs/{run_id}/discard", response_model=ApiResponse[dict[str, Any]])
def discard_run(run_id: str, request: Request) -> ApiResponse[dict[str, Any]]:
    run = get_synthetic_service().review(
        run_id, getattr(request.state, "principal", None), discard=True
    )
    return ApiResponse(data=run.public())


@router.get("/runs/{run_id}/results", response_model=ApiResponse[dict[str, Any]])
def get_results(
    run_id: str, request: Request, table_name: str, limit: int = Query(default=100, ge=1, le=100000)
) -> ApiResponse[dict[str, Any]]:
    service = get_synthetic_service()
    run = service.get(run_id, getattr(request.state, "principal", None))
    target = next((t for t in run.targets if t.table_name == table_name), None)
    if target is None:
        raise HTTPException(400, "今回の生成対象に含まれないテーブルです。")
    if run.preview:
        principal = getattr(request.state, "principal", None)
        if run.request.get("profile_id") and (
            not principal or not principal.can_use_profile(run.request["profile_id"])
        ):
            raise HTTPException(403, "業務プロファイルの利用権限が変更されています。")
        return ApiResponse(data=service.preview.results(run, table_name, limit))
    identity = service.adapter._db_admin_identity(table_name)
    with service.adapter.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT OBJECT_ID FROM ALL_OBJECTS WHERE OWNER=:owner "
            "AND OBJECT_NAME=:name AND OBJECT_TYPE='TABLE'",
            {"owner": identity.owner, "name": identity.object_name},
        )
        row = cur.fetchone()
    if not row or int(row[0]) != run.request.get("_object_ids", {}).get(table_name):
        raise HTTPException(
            409, "生成対象が削除・再作成されています。元の生成結果を参照できません。"
        )
    quoted_owner = identity.owner.replace('"', '""')
    quoted_name = identity.object_name.replace('"', '""')
    result = service.adapter.execute_select(
        f'SELECT * FROM "{quoted_owner}"."{quoted_name}"',  # nosec B608
        limit,
    )  # both identifiers escape embedded double quotes
    return ApiResponse(
        data={
            "table_name": table_name,
            "runtime": "oracle",
            "results": result.model_dump(),
            "warnings": [],
            "generation": target.model_dump(),
            "run_id": run.run_id,
            "generation_status": run.status,
            "generated_at": run.finished_at,
        }
    )
