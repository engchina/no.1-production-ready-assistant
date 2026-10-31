"""ナレッジベース API。作成・一覧・詳細・membership 管理。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pr_backend_core.api import OffsetParams, empty_page, offset_params, paginate

from app.clients.oracle import OracleClient
from app.config import get_settings
from app.db_degradation import load_or_degrade
from app.rag.extraction_field_adapter import (
    FieldDefinition,
    load_field_schema,
    validate_field_schema,
)
from app.schemas.common import ApiResponse, Page
from app.schemas.knowledge_base import (
    KnowledgeBaseCreateRequest,
    KnowledgeBaseDetail,
    KnowledgeBaseDocumentAssignmentRequest,
    KnowledgeBaseGraphData,
    KnowledgeBaseGraphEdge,
    KnowledgeBaseGraphNode,
    KnowledgeBaseStatus,
    KnowledgeBaseSummary,
    KnowledgeBaseUpdateRequest,
)
from app.schemas.settings import (
    FieldDefinitionData,
    KnowledgeBaseExtractionFieldsData,
    KnowledgeBaseExtractionFieldsUpdate,
)

router = APIRouter()

# KB は所属(スコープ)だけを持つ。構築設定の書き込みは受け付けない(rag/AGENTS.md「RAG 設定責務」)。
ADAPTER_CONFIG_REJECTED_MESSAGE = (
    "adapter_config は指定できません。ナレッジベースは文書の所属だけを持ちます。"
    "文書の処理は文書のレシピ、検索・回答の設定は検索・回答プロファイルで指定してください。"
)
# ID で絞るときの上限(検索・回答プロファイル・評価の参照 KB の上限と同じ)。
MAX_KNOWLEDGE_BASE_ID_FILTER = 200


def _reject_adapter_config(fields_set: set[str]) -> None:
    """`adapter_config` を含む作成・更新を 422 で拒否する(#302。旧 API との互換を変更)。"""
    if "adapter_config" in fields_set:
        raise HTTPException(status_code=422, detail=ADAPTER_CONFIG_REJECTED_MESSAGE)


async def _refreshed_detail_response(
    oracle: OracleClient,
    detail: KnowledgeBaseDetail,
) -> ApiResponse[KnowledgeBaseDetail]:
    """変更後の詳細を集計列(文書数・索引済み数など)込みで取り直して返す。

    変更系の Oracle 操作は KB の行だけを読むため、集計列が 0 のまま返る。取り直せない
    (範囲外になった等)ときは変更結果をそのまま返す(#282)。
    """
    refreshed = await oracle.get_knowledge_base(detail.id)
    return ApiResponse(data=refreshed or detail)


@router.get("", response_model=ApiResponse[Page[KnowledgeBaseSummary]])
async def list_knowledge_bases(
    paging: Annotated[OffsetParams, Depends(offset_params(default=50, max_limit=200))],
    status: KnowledgeBaseStatus | None = None,
    q: str | None = Query(default=None, min_length=1, max_length=200),
    ids: Annotated[
        list[str] | None,
        Query(
            max_length=MAX_KNOWLEDGE_BASE_ID_FILTER,
            description=(
                "指定した ID の KB だけを返す(選択済みの名前・状態の解決用。#302)。"
                "status を省くとアーカイブ済みも返す。"
            ),
        ),
    ] = None,
) -> ApiResponse[Page[KnowledgeBaseSummary]]:
    """ナレッジベース一覧を返す。DB 停止時は空一覧 + warning で縮退する。"""
    oracle = OracleClient()
    settings = get_settings()

    async def _load() -> Page[KnowledgeBaseSummary]:
        items = await oracle.list_knowledge_bases(
            status=status,
            query=q,
            limit=paging.limit,
            offset=paging.offset,
            knowledge_base_ids=ids,
        )
        total = await oracle.count_knowledge_bases(status=status, query=q, knowledge_base_ids=ids)
        return paginate(items, total=total, limit=paging.limit, offset=paging.offset)

    fallback: Page[KnowledgeBaseSummary] = empty_page(paging)
    page, degraded = await load_or_degrade(
        _load,
        timeout_seconds=settings.db_read_timeout_seconds,
        fallback=fallback,
        log_label="knowledge_bases_list",
    )
    return ApiResponse(
        data=page,
        warning_messages=[degraded.message] if degraded else [],
    )


@router.post("", response_model=ApiResponse[KnowledgeBaseDetail])
async def create_knowledge_base(
    request: KnowledgeBaseCreateRequest,
) -> ApiResponse[KnowledgeBaseDetail]:
    """ナレッジベースを作成する。`adapter_config` は 422 で拒否する。"""
    _reject_adapter_config(request.model_fields_set)
    try:
        detail = await OracleClient().create_knowledge_base(
            name=request.name,
            description=request.description,
            default_search_mode=request.default_search_mode,
            retrieval_config=request.retrieval_config,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ApiResponse(data=detail)


@router.get("/{knowledge_base_id}", response_model=ApiResponse[KnowledgeBaseDetail])
async def get_knowledge_base(
    knowledge_base_id: str,
) -> ApiResponse[KnowledgeBaseDetail]:
    """ナレッジベース詳細を返す。"""
    detail = await OracleClient().get_knowledge_base(knowledge_base_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="ナレッジベースが見つかりません。")
    return ApiResponse(data=detail)


@router.get(
    "/{knowledge_base_id}/graph",
    response_model=ApiResponse[KnowledgeBaseGraphData],
)
async def get_knowledge_base_graph(
    knowledge_base_id: str,
    limit: int = Query(default=80, ge=1, le=300),
) -> ApiResponse[KnowledgeBaseGraphData]:
    """KB の関係情報(GraphRAG)を可視化用 subgraph で返す。DB 停止時は空で縮退する。"""
    oracle = OracleClient()
    settings = get_settings()
    if await oracle.get_knowledge_base(knowledge_base_id) is None:
        raise HTTPException(status_code=404, detail="ナレッジベースが見つかりません。")

    async def _load() -> KnowledgeBaseGraphData:
        nodes, edges = await oracle.fetch_knowledge_base_subgraph(knowledge_base_id, limit=limit)
        return KnowledgeBaseGraphData(
            status="ok" if nodes else "empty",
            nodes=[KnowledgeBaseGraphNode(**node) for node in nodes],
            edges=[KnowledgeBaseGraphEdge(**edge) for edge in edges],
            truncated=len(nodes) >= limit,
        )

    data, degraded = await load_or_degrade(
        _load,
        timeout_seconds=settings.db_read_timeout_seconds,
        fallback=KnowledgeBaseGraphData(),
        log_label="knowledge_base_graph",
    )
    return ApiResponse(data=data, warning_messages=[degraded.message] if degraded else [])


@router.patch("/{knowledge_base_id}", response_model=ApiResponse[KnowledgeBaseDetail])
async def update_knowledge_base(
    knowledge_base_id: str,
    request: KnowledgeBaseUpdateRequest,
) -> ApiResponse[KnowledgeBaseDetail]:
    """ナレッジベースを更新する。`adapter_config` は 422 で拒否する。"""
    _reject_adapter_config(request.model_fields_set)
    update_fields = set(request.model_fields_set)
    oracle = OracleClient()
    try:
        detail = await oracle.update_knowledge_base(
            knowledge_base_id,
            name=request.name,
            description=request.description,
            default_search_mode=request.default_search_mode,
            retrieval_config=request.retrieval_config,
            update_fields=update_fields,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="ナレッジベースが見つかりません。") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await _refreshed_detail_response(oracle, detail)


def _extraction_fields_data(
    fields: list[FieldDefinition] | None,
) -> ApiResponse[KnowledgeBaseExtractionFieldsData]:
    """KB の定義(None なら全体の既定)を API 形にする。"""
    effective = load_field_schema().fields if fields is None else fields
    return ApiResponse(
        data=KnowledgeBaseExtractionFieldsData(
            inherits_default=fields is None,
            fields=[FieldDefinitionData.model_validate(field.model_dump()) for field in effective],
        )
    )


@router.get(
    "/{knowledge_base_id}/extraction-fields",
    response_model=ApiResponse[KnowledgeBaseExtractionFieldsData],
)
async def get_knowledge_base_extraction_fields(
    knowledge_base_id: str,
) -> ApiResponse[KnowledgeBaseExtractionFieldsData]:
    """ナレッジベースの項目抽出の定義を返す。KB に定義が無ければ全体の既定を返す(#548)。"""
    oracle = OracleClient()
    if await oracle.get_knowledge_base(knowledge_base_id) is None:
        raise HTTPException(status_code=404, detail="ナレッジベースが見つかりません。")
    return _extraction_fields_data(
        await oracle.get_knowledge_base_extraction_fields(knowledge_base_id)
    )


@router.put(
    "/{knowledge_base_id}/extraction-fields",
    response_model=ApiResponse[KnowledgeBaseExtractionFieldsData],
)
async def update_knowledge_base_extraction_fields(
    knowledge_base_id: str,
    request: KnowledgeBaseExtractionFieldsUpdate,
) -> ApiResponse[KnowledgeBaseExtractionFieldsData]:
    """ナレッジベースの項目抽出の定義を保存する。`fields` が null なら全体の既定に戻す(#548)。

    name の重複(大文字小文字を区別しない)は 422、アーカイブ済みの KB は 409。保存した定義は
    次の取込から効く(既存の文書の抽出値は変えない)。
    """
    oracle = OracleClient()
    detail = await oracle.get_knowledge_base(knowledge_base_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="ナレッジベースが見つかりません。")
    if detail.status != KnowledgeBaseStatus.ACTIVE:
        raise HTTPException(
            status_code=409, detail="アーカイブ済みのナレッジベースは変更できません。"
        )
    fields: list[FieldDefinition] | None = None
    if request.fields is not None:
        try:
            fields = validate_field_schema(
                [FieldDefinition.model_validate(field.model_dump()) for field in request.fields]
            ).fields
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    await oracle.update_knowledge_base_extraction_fields(knowledge_base_id, fields)
    return _extraction_fields_data(fields)


@router.post("/{knowledge_base_id}/archive", response_model=ApiResponse[KnowledgeBaseDetail])
async def archive_knowledge_base(
    knowledge_base_id: str,
) -> ApiResponse[KnowledgeBaseDetail]:
    """ナレッジベースをアーカイブする。"""
    oracle = OracleClient()
    try:
        detail = await oracle.archive_knowledge_base(knowledge_base_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="ナレッジベースが見つかりません。") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await _refreshed_detail_response(oracle, detail)


@router.post("/{knowledge_base_id}/documents", response_model=ApiResponse[KnowledgeBaseDetail])
async def assign_documents_to_knowledge_base(
    knowledge_base_id: str,
    request: KnowledgeBaseDocumentAssignmentRequest,
) -> ApiResponse[KnowledgeBaseDetail]:
    """既存文書をナレッジベースへ追加する。"""
    oracle = OracleClient()
    try:
        detail = await oracle.assign_documents_to_knowledge_base(
            knowledge_base_id,
            request.document_ids,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="ナレッジベースまたは文書が見つかりません。",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await _refreshed_detail_response(oracle, detail)


@router.delete(
    "/{knowledge_base_id}/documents/{document_id}",
    response_model=ApiResponse[KnowledgeBaseDetail],
)
async def remove_document_from_knowledge_base(
    knowledge_base_id: str,
    document_id: str,
) -> ApiResponse[KnowledgeBaseDetail]:
    """文書をナレッジベースから外す。文書自体は削除しない。

    最後の所属を外すときは DEFAULT へ移す(未所属の文書を作らない)。DEFAULT にだけ
    所属する文書は外せない(409)。
    """
    oracle = OracleClient()
    try:
        detail = await oracle.remove_document_from_knowledge_base(
            knowledge_base_id,
            document_id,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="ナレッジベースまたは文書が見つかりません。",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await _refreshed_detail_response(oracle, detail)
