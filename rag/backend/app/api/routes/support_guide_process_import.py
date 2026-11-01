"""回答の過程の表から業務ガイドの下書きを作る API（#2397）。

1. ``POST .../support-guides/import/process/workbook``: Excel のシートと列を返す。列を選べば、
   選んだ列で行を読む（モデルは呼ばない）。
2. ``POST .../support-guides/import/process/preview``: 行（Excel の行か、貼り付けたテキスト）ごとに
   モデルの API で業務ガイドの下書きを作り、資料名の解決と検証の結果を返す（保存しない）。

選んだ下書きは、既存の取り込み（``POST .../support-guides/import``）で下書きとして作る。ルールの
候補は、用語・同義語と回答ルールの編集（``POST .../runtime-knowledge/edit``）で無効のルールとして
足す。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from contextlib import nullcontext
from pathlib import PurePath
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pr_backend_core.bounded_calls import Attempt, BoundedCallAborted
from rag_parser_core.enterprise_ai import sdk_retries_disabled
from rag_parser_core.language import bind_pack, current_pack, resolve

from app.clients.ai_calls import make_ai_caller
from app.clients.enterprise_ai import EnterpriseAiClient
from app.clients.oracle import OracleClient
from app.config import Settings, get_settings
from app.rag.rate_limit import enforce_rate_limit
from app.rag.search_answer_profile_config import resolve_search_answer_profile_settings
from app.rag.support_guide_process_import import (
    PROCESS_WORKBOOK_MAX_BYTES,
    GuideDraftLlm,
    draft_guides,
    list_knowledge_documents,
    process_headings,
    read_process_workbook,
)
from app.schemas.common import ApiResponse
from app.schemas.search_answer_profile import SearchAnswerProfileDetail
from app.schemas.support_guide import (
    SupportGuideProcessPreviewData,
    SupportGuideProcessPreviewRequest,
    SupportGuideProcessRow,
    SupportGuideProcessWorkbookData,
)

router = APIRouter()
_BASE = "/{search_answer_profile_id}/support-guides/import/process"


def guide_draft_llm(settings: Settings) -> GuideDraftLlm:
    """下書きを作る LLM（テストで差し替える）。"""
    return EnterpriseAiClient(settings=settings)


async def _profile(
    oracle: OracleClient, search_answer_profile_id: str
) -> SearchAnswerProfileDetail:
    view = await oracle.get_search_answer_profile(search_answer_profile_id)
    if view is None:
        raise HTTPException(status_code=404, detail="検索・回答プロファイルが見つかりません。")
    return view


@router.post(_BASE + "/workbook", response_model=ApiResponse[SupportGuideProcessWorkbookData])
async def read_support_guide_process_workbook(
    search_answer_profile_id: str,
    file: Annotated[UploadFile, File()],
    sheet: Annotated[str | None, Form()] = None,
    question_column: Annotated[int | None, Form(ge=0)] = None,
    process_column: Annotated[int | None, Form(ge=0)] = None,
    answer_column: Annotated[int | None, Form(ge=0)] = None,
    start_row: Annotated[int, Form(ge=2)] = 2,
) -> ApiResponse[SupportGuideProcessWorkbookData]:
    """Excel のシートと列。質問と回答の過程の列を選べば、``start_row`` から行を読む。"""
    oracle = OracleClient()
    profile = await _profile(oracle, search_answer_profile_id)
    settings, _ = resolve_search_answer_profile_settings(get_settings(), profile.config)
    file_name = PurePath(file.filename or "").name or "process.xlsx"
    if not file_name.lower().endswith(".xlsx"):
        raise HTTPException(status_code=422, detail="Excel（.xlsx）のファイルを選んでください。")
    data = await file.read(PROCESS_WORKBOOK_MAX_BYTES + 1)
    try:
        result = read_process_workbook(
            data,
            file_name,
            max_rows=settings.rag_support_guide_import_max_rows,
            sheet=sheet,
            question_column=question_column,
            process_column=process_column,
            answer_column=answer_column,
            start_row=start_row,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ApiResponse(data=result)


@router.post(_BASE + "/preview", response_model=ApiResponse[SupportGuideProcessPreviewData])
async def preview_support_guide_process_import(
    http_request: Request,
    search_answer_profile_id: str,
    body: SupportGuideProcessPreviewRequest,
) -> ApiResponse[SupportGuideProcessPreviewData]:
    """行ごとに業務ガイドの下書きを作り、資料名の解決と検証の結果を返す（保存しない）。

    1 行でモデルの API を 1 回呼ぶ（上限つき並行。``RAG_SUPPORT_GUIDE_IMPORT_CONCURRENCY``）。
    行の数の上限は ``RAG_SUPPORT_GUIDE_IMPORT_MAX_ROWS``。見出しの語は言語資源
    （``guide_import_cues.process_headings``。プロファイルの上書きを重ねる）。
    """
    enforce_rate_limit("search", http_request)
    oracle = OracleClient()
    profile = await _profile(oracle, search_answer_profile_id)
    settings, _ = resolve_search_answer_profile_settings(get_settings(), profile.config)
    max_rows = settings.rag_support_guide_import_max_rows
    if len(body.rows) > max_rows:
        raise HTTPException(
            status_code=422,
            detail=f"1 回で下書きにできるのは {max_rows} 行までです。行を分けてください。",
        )
    documents = await list_knowledge_documents(
        oracle, profile.config.normalized_knowledge_base_ids()
    )
    overrides = profile.config.language_pack
    llm = guide_draft_llm(settings)
    caller = make_ai_caller(settings, "guide_import")

    async def run(
        rows: Sequence[SupportGuideProcessRow],
        fn: Callable[[SupportGuideProcessRow, Attempt], Awaitable[str]],
    ) -> list[tuple[str | None, str | None]]:
        async def call(row: SupportGuideProcessRow, attempt: Attempt) -> str:
            with sdk_retries_disabled():
                return await fn(row, attempt)

        outcomes = await caller.map(rows, call, labels=[f"row-{row.row}" for row in rows])
        return [
            (outcome.value, None)
            if outcome.ok
            else (None, f"下書きを作れませんでした（{outcome.error}）。")
            for outcome in outcomes
        ]

    with bind_pack(resolve(current_pack(), overrides)) if overrides else nullcontext():
        headings = process_headings(current_pack())
        try:
            items = await draft_guides(body.rows, documents, llm=llm, headings=headings, run=run)
        except BoundedCallAborted as exc:
            raise HTTPException(
                status_code=503, detail=f"業務ガイドの下書きを作れませんでした。{exc}"
            ) from exc
    return ApiResponse(
        data=SupportGuideProcessPreviewData(
            items=items, importable_count=sum(item.valid for item in items)
        )
    )
