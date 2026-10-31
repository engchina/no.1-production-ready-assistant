"""選択文書の実在レシピを確認し、指定した工程から再処理する。"""

import logging

from fastapi import APIRouter, HTTPException, Request

from app.api.routes.documents import _enqueue_ingestion_job_for_recipe
from app.clients.oracle import OracleClient
from app.rag.language_pack_releases import index_relation
from app.rag.published_language import initialized_release_store
from app.rag.rate_limit import enforce_rate_limit
from app.schemas.common import ApiResponse
from app.schemas.language_pack_release import LanguagePackRelease
from app.schemas.language_pack_reprocessing import (
    ReprocessingJobResult,
    ReprocessingJobsRequest,
    ReprocessingJobsResult,
    ReprocessingSelection,
    ReprocessingTarget,
    ReprocessingTargets,
)

router = APIRouter()
logger = logging.getLogger(__name__)


async def _current(expected: str) -> LanguagePackRelease:
    current = await (await initialized_release_store()).current()
    if current.id != expected:
        raise HTTPException(
            status_code=409, detail="言語資源の公開版が変わりました。対象を確認し直してください。"
        )
    return current


@router.post("/language-pack/reprocessing-targets", response_model=ApiResponse[ReprocessingTargets])
async def reprocessing_targets(payload: ReprocessingSelection) -> ApiResponse[ReprocessingTargets]:
    current = await _current(payload.expected_release_id)
    ids = list(dict.fromkeys(payload.document_ids))
    rows = await OracleClient().list_language_reprocessing_targets(ids)
    targets = [ReprocessingTarget.model_validate(row) for row in rows]
    for target in targets:
        target.relation = index_relation(
            used_release_id=target.used_release_id,
            used_ingest_hash=target.used_ingest_hash,
            current=current,
        )
    visible = {item.document_id for item in targets}
    return ApiResponse(
        data=ReprocessingTargets(
            current_release=current,
            targets=targets,
            unavailable_count=sum(document_id not in visible for document_id in ids),
        )
    )


@router.post("/language-pack/reprocessing-jobs", response_model=ApiResponse[ReprocessingJobsResult])
async def reprocessing_jobs(
    payload: ReprocessingJobsRequest, request: Request
) -> ApiResponse[ReprocessingJobsResult]:
    enforce_rate_limit("ingest", request)
    await _current(payload.expected_release_id)
    keys = [(item.document_id, item.recipe_id) for item in payload.targets]
    if len(set(keys)) != len(keys):
        raise HTTPException(status_code=422, detail="同じ文書レシピを重複して指定できません。")
    results: list[ReprocessingJobResult] = []
    for item in payload.targets:
        result = ReprocessingJobResult(
            document_id=item.document_id, recipe_id=item.recipe_id, outcome="rejected"
        )
        try:
            job = await _enqueue_ingestion_job_for_recipe(
                item.document_id,
                item.recipe_id,
                phase=item.phase,
                expected_language_pack_release_id=payload.expected_release_id,
                expected_config_revision=item.expected_config_revision,
            )
            result.outcome = "accepted"
            result.job_id = job.id
        except KeyError:
            result.reason = "対象の文書レシピを利用できません。"
        except ValueError:
            result.reason = (
                "公開版・レシピの改訂・処理状態が変わりました。対象を確認し直してください。"
            )
        except HTTPException:
            result.reason = (
                "指定した工程から処理できません。レシピの設定と成果物を確認してください。"
            )
        except Exception:  # noqa: BLE001 - 受付済みの job と失敗を区別して返す。
            logger.exception("language_pack_reprocessing_enqueue_failed")
            result.reason = "再処理を受け付けられませんでした。処理状態を確認してください。"
        results.append(result)
    accepted = sum(item.outcome == "accepted" for item in results)
    return ApiResponse(
        data=ReprocessingJobsResult(
            items=results,
            accepted_count=accepted,
            rejected_count=len(results) - accepted,
        )
    )
