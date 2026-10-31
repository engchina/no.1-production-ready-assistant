"""検索・回答プロファイル(Search Answer Profile)API。作成・一覧・詳細・更新・アーカイブ。

KB が「文書をどう加工して索引するか」を司るのに対し、検索・回答プロファイルは「どの KB 群を
どんな検索/生成方針・persona で束ねて回答するか」を司る利用者視点のエンティティ。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pr_backend_core.api import OffsetParams, empty_page, offset_params, paginate

from app.clients.oracle import OracleClient
from app.config import get_settings
from app.db_degradation import load_or_degrade
from app.rag.search_answer_profile_config import validate_profile_answer_model
from app.schemas.common import ApiResponse, Page
from app.schemas.search_answer_profile import (
    EntityIndexCoverageData,
    EntityIndexCoverageRequest,
    InheritedAnswerContactData,
    SearchAnswerProfileCreateRequest,
    SearchAnswerProfileDetail,
    SearchAnswerProfileStatus,
    SearchAnswerProfileSummary,
    SearchAnswerProfileUpdateRequest,
)

router = APIRouter()


@router.get("", response_model=ApiResponse[Page[SearchAnswerProfileSummary]])
async def list_search_answer_profiles(
    paging: Annotated[OffsetParams, Depends(offset_params(default=50, max_limit=200))],
    status: SearchAnswerProfileStatus | None = None,
    q: str | None = Query(default=None, min_length=1, max_length=200),
) -> ApiResponse[Page[SearchAnswerProfileSummary]]:
    """検索・回答プロファイル一覧を返す。DB 停止時は空一覧 + warning で縮退する。"""
    oracle = OracleClient()
    settings = get_settings()

    async def _load() -> Page[SearchAnswerProfileSummary]:
        await oracle.ensure_default_search_answer_profile()
        items = await oracle.list_search_answer_profiles(
            status=status, query=q, limit=paging.limit, offset=paging.offset
        )
        total = await oracle.count_search_answer_profiles(status=status, query=q)
        return paginate(items, total=total, limit=paging.limit, offset=paging.offset)

    fallback: Page[SearchAnswerProfileSummary] = empty_page(paging)
    page, degraded = await load_or_degrade(
        _load,
        timeout_seconds=settings.db_read_timeout_seconds,
        fallback=fallback,
        log_label="search_answer_profiles_list",
    )
    return ApiResponse(
        data=page,
        warning_messages=[degraded.message] if degraded else [],
    )


@router.post("", response_model=ApiResponse[SearchAnswerProfileDetail])
async def create_search_answer_profile(
    request: SearchAnswerProfileCreateRequest,
) -> ApiResponse[SearchAnswerProfileDetail]:
    """検索・回答プロファイルを作成する。"""
    validate_profile_answer_model(get_settings(), request.config)
    oracle = OracleClient()
    created = await oracle.create_search_answer_profile(
        name=request.name,
        description=request.description,
        config=request.config,
    )
    # 参照 KB 名を解決した詳細を返す。
    detail = await oracle.get_search_answer_profile(created.id)
    return ApiResponse(data=detail or created)


@router.get("/answer-models", response_model=ApiResponse[list[dict[str, str]]])
async def profile_answer_models() -> ApiResponse[list[dict[str, str]]]:
    """回答設定の権限で読める、接続情報を含まない登録済みモデルの候補。"""
    from app.api.routes.search import registered_answer_model_choices

    return ApiResponse(data=registered_answer_model_choices(get_settings()))


@router.post("/entity-index-coverage", response_model=ApiResponse[EntityIndexCoverageData])
async def entity_index_coverage(
    request: EntityIndexCoverageRequest,
) -> ApiResponse[EntityIndexCoverageData]:
    """参照するナレッジベースで、実体の索引を持つ文書の数を返す（#1388）。

    実体の 1 段の拡張（プロファイルの ``query.entity_expansion_enabled``）は、文書レシピで実体の
    索引を選んだ文書にだけ効く。画面は保存前の選択（下書き）でも案内できるよう、ナレッジベースを
    request で受け取る。見え方は回答の検索と同じ（利用者が見られない文書・ナレッジベースは
    数えない）。
    """
    from app.clients.entity_store import EntityStore

    count = await EntityStore(OracleClient()).count_entity_documents(request.knowledge_base_ids)
    return ApiResponse(data=EntityIndexCoverageData(document_count=count))


@router.get("/inherited-contact", response_model=ApiResponse[InheritedAnswerContactData])
async def inherited_answer_contact() -> ApiResponse[InheritedAnswerContactData]:
    """プロファイルの窓口の上書きが空のときに継承する、システム設定の窓口（読み取りだけ。#2683）。

    回答の注意書きの設定 API（``/settings/answer-notice``）は運用設定の権限なので、プロファイルの
    画面の権限で窓口だけを読む。値は回答に使うものと同じ（``current_answer_notice``）。
    ``/{search_answer_profile_id}`` より先に登録する（id と取り違えない）。
    """
    from app.rag.answer_notice import inherited_answer_notice_contact

    enabled, contact = inherited_answer_notice_contact()
    return ApiResponse(data=InheritedAnswerContactData(enabled=enabled, contact=contact))


@router.get("/{search_answer_profile_id}", response_model=ApiResponse[SearchAnswerProfileDetail])
async def get_search_answer_profile(
    search_answer_profile_id: str,
) -> ApiResponse[SearchAnswerProfileDetail]:
    """検索・回答プロファイル詳細を返す。"""
    detail = await OracleClient().get_search_answer_profile(search_answer_profile_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="検索・回答プロファイルが見つかりません。")
    return ApiResponse(data=detail)


@router.patch("/{search_answer_profile_id}", response_model=ApiResponse[SearchAnswerProfileDetail])
async def update_search_answer_profile(
    search_answer_profile_id: str,
    request: SearchAnswerProfileUpdateRequest,
) -> ApiResponse[SearchAnswerProfileDetail]:
    """検索・回答プロファイルを更新する。"""
    if request.config is not None:
        validate_profile_answer_model(get_settings(), request.config)
    update_fields = set(request.model_fields_set)
    oracle = OracleClient()
    try:
        await oracle.update_search_answer_profile(
            search_answer_profile_id,
            name=request.name,
            description=request.description,
            config=request.config,
            update_fields=update_fields,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="検索・回答プロファイルが見つかりません。"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    detail = await oracle.get_search_answer_profile(search_answer_profile_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="検索・回答プロファイルが見つかりません。")
    return ApiResponse(data=detail)


@router.post(
    "/{search_answer_profile_id}/archive", response_model=ApiResponse[SearchAnswerProfileDetail]
)
async def archive_search_answer_profile(
    search_answer_profile_id: str,
) -> ApiResponse[SearchAnswerProfileDetail]:
    """検索・回答プロファイルをアーカイブする。参照 KB・文書は変更しない。"""
    try:
        await OracleClient().archive_search_answer_profile(search_answer_profile_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="検索・回答プロファイルが見つかりません。"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    detail = await OracleClient().get_search_answer_profile(search_answer_profile_id)
    return ApiResponse(data=detail)
