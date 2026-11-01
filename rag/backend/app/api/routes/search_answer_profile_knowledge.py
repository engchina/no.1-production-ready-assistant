"""検索・回答プロファイル単位の知識 API(ドメインキーワード)。

rag_poc の「ドメインキーワード管理」を検索・回答プロファイル層へ移植したもの。
KB・文書レシピには持たせず、検索時は検索・回答プロファイルのキーワードだけを使う。
"""

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from rag_engine.knowledge.approved_faq import ApprovedFaqImportRow, ApprovedFaqRecord

from app.api.routes.search import (
    _published_guides,
    profile_scope_filters,
    support_guide_context,
)
from app.clients.genai import GenAiClient
from app.clients.oracle import OracleClient
from app.config import get_settings
from app.rag.chat_question_token import with_rule_selection_answers
from app.rag.domain_keyword_import import (
    PREVIEW_ROWS,
    KeywordImportMode,
    import_keywords,
    read_keyword_excel,
)
from app.rag.question_understanding import profile_language_scope
from app.rag.rule_selection import recognize_rule_selections
from app.rag.search_answer_profile_config import resolve_search_answer_profile_settings
from app.rag.search_answer_profile_knowledge import (
    APPROVED_FAQ_PREVIEW_ROWS,
    ApprovedFaqMutation,
    add_approved_faq,
    delete_approved_faq,
    edit_runtime_knowledge,
    import_approved_faq,
    is_direct_faq_match,
    load_approved_faq,
    load_approved_faq_enabled,
    load_domain_keywords,
    load_runtime_knowledge_payload,
    preview_runtime_knowledge,
    read_approved_faq_excel,
    save_approved_faq_enabled,
    save_domain_keywords,
    save_rule_clarification,
    suggest_approved_faq,
    suggest_clarification,
    suggest_domain_keywords,
)
from app.rag.support_guide_conditions import resolve_text_conditions
from app.rag.support_guide_query import build_guide_query
from app.rag.support_guide_runtime import (
    GuideChoice,
    guide_choice_clarification,
    guide_clarification,
    select_guide,
)
from app.schemas.common import ApiResponse
from app.schemas.search_answer_profile import SearchAnswerProfileDetail
from app.schemas.search_answer_profile_knowledge import (
    ApprovedFaqAddRequest,
    ApprovedFaqDeleteRequest,
    ApprovedFaqImportPreviewData,
    ApprovedFaqImportRowData,
    ApprovedFaqListData,
    ApprovedFaqMutationData,
    ApprovedFaqRecordData,
    ApprovedFaqSettingsRequest,
    ApprovedFaqSuggestionData,
    ApprovedFaqSuggestionsData,
    ApprovedFaqSuggestRequest,
    ClarificationAnswer,
    ClarificationSuggestionData,
    ClarificationSuggestionsData,
    ClarificationSuggestRequest,
    DomainKeywordCandidateData,
    DomainKeywordImportData,
    DomainKeywordImportPreviewData,
    DomainKeywordImportRowData,
    DomainKeywordsData,
    DomainKeywordSuggestionData,
    DomainKeywordsUpdate,
    RuleClarificationRequest,
    RuntimeKnowledgeData,
    RuntimeKnowledgeEditRequest,
    RuntimeKnowledgePreviewData,
    RuntimeKnowledgePreviewRequest,
)

# チャットで提示する類似問の上限(rag_poc の候補の上限は 5。チャットは 3 ＋「どれでもない」。#684)。
CHAT_APPROVED_FAQ_LIMIT = 3

router = APIRouter()
MAX_FAQ_EXCEL_BYTES = 10 * 1024 * 1024


async def _require_search_answer_profile(
    oracle: OracleClient, search_answer_profile_id: str
) -> SearchAnswerProfileDetail:
    view = await oracle.get_search_answer_profile(search_answer_profile_id)
    if view is None:
        raise HTTPException(status_code=404, detail="検索・回答プロファイルが見つかりません。")
    return view


@router.get(
    "/{search_answer_profile_id}/domain-keywords",
    response_model=ApiResponse[DomainKeywordsData],
)
async def get_domain_keywords(search_answer_profile_id: str) -> ApiResponse[DomainKeywordsData]:
    """検索・回答プロファイルのドメインキーワードを返す。"""
    oracle = OracleClient()
    await _require_search_answer_profile(oracle, search_answer_profile_id)
    keywords = await load_domain_keywords(oracle, search_answer_profile_id)
    return ApiResponse(
        data=DomainKeywordsData(
            search_answer_profile_id=search_answer_profile_id, keywords=keywords
        )
    )


@router.put(
    "/{search_answer_profile_id}/domain-keywords",
    response_model=ApiResponse[DomainKeywordsData],
)
async def put_domain_keywords(
    search_answer_profile_id: str,
    request: DomainKeywordsUpdate,
) -> ApiResponse[DomainKeywordsData]:
    """ドメインキーワードを全置換で保存する。"""
    oracle = OracleClient()
    await _require_search_answer_profile(oracle, search_answer_profile_id)
    try:
        keywords = await save_domain_keywords(oracle, search_answer_profile_id, request.keywords)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ApiResponse(
        data=DomainKeywordsData(
            search_answer_profile_id=search_answer_profile_id, keywords=keywords
        )
    )


async def _keyword_excel_rows(file: UploadFile) -> list[tuple[int, str]]:
    content = await file.read(MAX_FAQ_EXCEL_BYTES + 1)
    if len(content) > MAX_FAQ_EXCEL_BYTES:
        raise HTTPException(status_code=413, detail="Excel ファイルが大きすぎます(上限 10MB)。")
    try:
        return await asyncio.to_thread(read_keyword_excel, content, file.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/{search_answer_profile_id}/domain-keywords/import/preview",
    response_model=ApiResponse[DomainKeywordImportPreviewData],
)
async def preview_domain_keyword_import(
    search_answer_profile_id: str, file: Annotated[UploadFile, File()]
) -> ApiResponse[DomainKeywordImportPreviewData]:
    await _require_search_answer_profile(OracleClient(), search_answer_profile_id)
    rows = await _keyword_excel_rows(file)
    return ApiResponse(
        data=DomainKeywordImportPreviewData(
            total=len(rows),
            rows=[
                DomainKeywordImportRowData(row=number, keyword=word)
                for number, word in rows[:PREVIEW_ROWS]
            ],
        )
    )


@router.post(
    "/{search_answer_profile_id}/domain-keywords/import",
    response_model=ApiResponse[DomainKeywordImportData],
)
async def import_domain_keyword_excel(
    search_answer_profile_id: str,
    file: Annotated[UploadFile, File()],
    mode: Annotated[KeywordImportMode, Form()] = "INSERT",
) -> ApiResponse[DomainKeywordImportData]:
    oracle = OracleClient()
    await _require_search_answer_profile(oracle, search_answer_profile_id)
    rows = await _keyword_excel_rows(file)
    try:
        keywords, inserted = await import_keywords(
            oracle, search_answer_profile_id, [word for _, word in rows], mode=mode
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ApiResponse(
        data=DomainKeywordImportData(
            search_answer_profile_id=search_answer_profile_id,
            keywords=keywords,
            inserted_count=inserted,
        )
    )


@router.post(
    "/{search_answer_profile_id}/domain-keywords/suggest",
    response_model=ApiResponse[DomainKeywordSuggestionData],
)
async def suggest_search_answer_profile_domain_keywords(
    search_answer_profile_id: str,
    limit: int = Query(default=50, ge=1, le=200),
) -> ApiResponse[DomainKeywordSuggestionData]:
    """参照 KB の配信中チャンクからキーワード候補を返す(保存はしない)。"""
    oracle = OracleClient()
    view = await _require_search_answer_profile(oracle, search_answer_profile_id)
    result = await suggest_domain_keywords(
        oracle,
        search_answer_profile_id,
        view.config.normalized_knowledge_base_ids(),
        limit=limit,
    )
    return ApiResponse(
        data=DomainKeywordSuggestionData(
            candidates=[
                DomainKeywordCandidateData(
                    keyword=item.keyword,
                    score=round(item.score, 6),
                    frequency=item.frequency,
                    chunk_count=item.chunk_count,
                    document_count=item.document_count,
                )
                for item in result.candidates
            ],
            processed_chunk_count=result.processed_chunk_count,
        )
    )


# --- Approved FAQ(類似問)-----------------------------------------------------


def _faq_record_data(record: ApprovedFaqRecord) -> ApprovedFaqRecordData:
    return ApprovedFaqRecordData(
        id=record.id,
        question=record.question,
        answer=record.approved_answer,
        alternate_questions=list(record.alternate_questions),
        status=record.status,
    )


def _faq_mutation_data(
    search_answer_profile_id: str, mutation: ApprovedFaqMutation
) -> ApprovedFaqMutationData:
    return ApprovedFaqMutationData(
        search_answer_profile_id=search_answer_profile_id,
        records=[_faq_record_data(record) for record in mutation.records],
        inserted_count=mutation.inserted_count,
        deleted_count=mutation.deleted_count,
    )


@router.get(
    "/{search_answer_profile_id}/approved-faq",
    response_model=ApiResponse[ApprovedFaqListData],
)
async def get_approved_faq(search_answer_profile_id: str) -> ApiResponse[ApprovedFaqListData]:
    """検索・回答プロファイルの承認済み FAQ 一覧を返す。"""
    oracle = OracleClient()
    await _require_search_answer_profile(oracle, search_answer_profile_id)
    records = await load_approved_faq(oracle, search_answer_profile_id)
    return ApiResponse(
        data=ApprovedFaqListData(
            search_answer_profile_id=search_answer_profile_id,
            records=[_faq_record_data(record) for record in records],
            enabled=await load_approved_faq_enabled(oracle, search_answer_profile_id),
        )
    )


@router.put(
    "/{search_answer_profile_id}/approved-faq/settings",
    response_model=ApiResponse[ApprovedFaqListData],
)
async def put_approved_faq_settings(
    search_answer_profile_id: str, request: ApprovedFaqSettingsRequest
) -> ApiResponse[ApprovedFaqListData]:
    """回答の前に類似問を提示するかを保存する(検索・回答プロファイルごと。#684)。"""
    oracle = OracleClient()
    await _require_search_answer_profile(oracle, search_answer_profile_id)
    await save_approved_faq_enabled(oracle, search_answer_profile_id, request.enabled)
    return await get_approved_faq(search_answer_profile_id)


@router.post(
    "/{search_answer_profile_id}/approved-faq",
    response_model=ApiResponse[ApprovedFaqMutationData],
)
async def post_approved_faq(
    search_answer_profile_id: str, request: ApprovedFaqAddRequest
) -> ApiResponse[ApprovedFaqMutationData]:
    """FAQ を 1 件追加する(同じ質問は置き換え)。"""
    oracle = OracleClient()
    await _require_search_answer_profile(oracle, search_answer_profile_id)
    try:
        mutation = await add_approved_faq(
            oracle, search_answer_profile_id, question=request.question, answer=request.answer
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ApiResponse(data=_faq_mutation_data(search_answer_profile_id, mutation))


@router.post(
    "/{search_answer_profile_id}/approved-faq/delete",
    response_model=ApiResponse[ApprovedFaqMutationData],
)
async def delete_search_answer_profile_approved_faq(
    search_answer_profile_id: str, request: ApprovedFaqDeleteRequest
) -> ApiResponse[ApprovedFaqMutationData]:
    """ID を指定して FAQ を削除する。"""
    oracle = OracleClient()
    await _require_search_answer_profile(oracle, search_answer_profile_id)
    try:
        mutation = await delete_approved_faq(oracle, search_answer_profile_id, request.ids)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ApiResponse(data=_faq_mutation_data(search_answer_profile_id, mutation))


async def _excel_rows(file: UploadFile) -> list[ApprovedFaqImportRow]:
    content = await file.read()
    if len(content) > MAX_FAQ_EXCEL_BYTES:
        raise HTTPException(status_code=413, detail="Excel ファイルが大きすぎます(上限 10MB)。")
    try:
        return await asyncio.to_thread(read_approved_faq_excel, content, file.filename or "")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/{search_answer_profile_id}/approved-faq/import/preview",
    response_model=ApiResponse[ApprovedFaqImportPreviewData],
)
async def preview_approved_faq_import(
    search_answer_profile_id: str, file: Annotated[UploadFile, File()]
) -> ApiResponse[ApprovedFaqImportPreviewData]:
    """Excel(QUESTION / ANSWER 列)の取込内容を先頭 10 件だけ返す(保存しない)。"""
    await _require_search_answer_profile(OracleClient(), search_answer_profile_id)
    rows = await _excel_rows(file)
    return ApiResponse(
        data=ApprovedFaqImportPreviewData(
            total=len(rows),
            rows=[
                ApprovedFaqImportRowData(
                    question=row.question, answer=row.approved_answer, row=row.row
                )
                for row in rows[:APPROVED_FAQ_PREVIEW_ROWS]
            ],
        )
    )


@router.post(
    "/{search_answer_profile_id}/approved-faq/import",
    response_model=ApiResponse[ApprovedFaqMutationData],
)
async def import_search_answer_profile_approved_faq(
    search_answer_profile_id: str,
    file: Annotated[UploadFile, File()],
    mode: Annotated[str, Form()] = "INSERT",
) -> ApiResponse[ApprovedFaqMutationData]:
    """Excel から FAQ を取り込む。INSERT は同じ質問をスキップ、DELETE_THEN_INSERT は置き換え。"""
    oracle = OracleClient()
    await _require_search_answer_profile(oracle, search_answer_profile_id)
    rows = await _excel_rows(file)
    try:
        mutation = await import_approved_faq(oracle, search_answer_profile_id, rows, mode=mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ApiResponse(data=_faq_mutation_data(search_answer_profile_id, mutation))


@router.post(
    "/{search_answer_profile_id}/approved-faq/suggest",
    response_model=ApiResponse[ApprovedFaqSuggestionsData],
)
async def suggest_search_answer_profile_approved_faq(
    search_answer_profile_id: str, request: ApprovedFaqSuggestRequest
) -> ApiResponse[ApprovedFaqSuggestionsData]:
    """質問に近い承認済み FAQ(類似問)を返す。回答前の提示に使う。"""
    oracle = OracleClient()
    await _require_search_answer_profile(oracle, search_answer_profile_id)
    settings = get_settings()
    genai = GenAiClient(settings=settings) if settings.rag_approved_faq_semantic_enabled else None
    suggestions = await suggest_approved_faq(
        oracle,
        search_answer_profile_id,
        request.query,
        # チャットは最大 3 件 ＋「どれでもない」を出し、どれかを選ぶまで回答しない(#684)。
        limit=min(request.limit, CHAT_APPROVED_FAQ_LIMIT)
        if request.purpose == "chat"
        else request.limit,
        min_score=settings.rag_approved_faq_suggestion_min_score,
        embed=(
            (lambda texts, input_type: genai.embed(texts, input_type=input_type))
            if genai is not None
            else None
        ),
        embedding_model=settings.genai_embedding_model,
        embedding_dimensions=settings.genai_embedding_dim,
    )
    if suggestions:
        # 1 位とほぼ同じくらい近い候補だけを出す(#709)。
        top = max(item.score for item in suggestions)
        suggestions = [
            item
            for item in suggestions
            if item.score >= top - settings.rag_approved_faq_suggestion_max_gap
        ]
    return ApiResponse(
        data=ApprovedFaqSuggestionsData(
            suggestions=[
                ApprovedFaqSuggestionData(
                    id=item.record.id,
                    question=item.record.question,
                    matched_question=item.matched_question,
                    answer=item.record.approved_answer,
                    score=round(item.score, 4),
                    direct=is_direct_faq_match(item),
                )
                for item in suggestions
            ]
        )
    )


# --- 用語・同義語と回答ルール(runtime knowledge)-------------------------------------------


def _runtime_knowledge_data(
    search_answer_profile_id: str, payload: dict[str, object]
) -> RuntimeKnowledgeData:
    return RuntimeKnowledgeData.model_validate(
        {
            "search_answer_profile_id": search_answer_profile_id,
            "terms": payload.get("terms") or [],
            "rules": payload.get("rules") or [],
        }
    )


@router.get(
    "/{search_answer_profile_id}/runtime-knowledge",
    response_model=ApiResponse[RuntimeKnowledgeData],
)
async def get_runtime_knowledge(search_answer_profile_id: str) -> ApiResponse[RuntimeKnowledgeData]:
    """検索・回答プロファイルの用語・同義語と回答ルールを返す。"""
    oracle = OracleClient()
    await _require_search_answer_profile(oracle, search_answer_profile_id)
    payload = await load_runtime_knowledge_payload(oracle, search_answer_profile_id)
    return ApiResponse(data=_runtime_knowledge_data(search_answer_profile_id, payload))


@router.post(
    "/{search_answer_profile_id}/runtime-knowledge/edit",
    response_model=ApiResponse[RuntimeKnowledgeData],
)
async def post_runtime_knowledge_edit(
    search_answer_profile_id: str, request: RuntimeKnowledgeEditRequest
) -> ApiResponse[RuntimeKnowledgeData]:
    """用語またはルールを 1 行追加・更新・削除する。"""
    oracle = OracleClient()
    await _require_search_answer_profile(oracle, search_answer_profile_id)
    try:
        payload = await edit_runtime_knowledge(
            oracle,
            search_answer_profile_id,
            kind=request.kind,
            selected=request.selected,
            name=request.name,
            title=request.title,
            labels=request.labels,
            content=request.content,
            source=request.source,
            purpose=request.purpose,
            instruction_kind=request.instruction_kind,
            exclusive_group=request.exclusive_group,
            priority=request.priority,
            enabled=request.enabled,
            delete=request.delete,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ApiResponse(data=_runtime_knowledge_data(search_answer_profile_id, payload))


@router.post(
    "/{search_answer_profile_id}/runtime-knowledge/preview",
    response_model=ApiResponse[RuntimeKnowledgePreviewData],
)
async def post_runtime_knowledge_preview(
    search_answer_profile_id: str, request: RuntimeKnowledgePreviewRequest
) -> ApiResponse[RuntimeKnowledgePreviewData]:
    """照合テスト: 質問に一致する用語・同義語と回答ルールと拡張後の検索文を返す(保存しない)。"""
    oracle = OracleClient()
    await _require_search_answer_profile(oracle, search_answer_profile_id)
    payload = await load_runtime_knowledge_payload(oracle, search_answer_profile_id)
    context = await asyncio.to_thread(preview_runtime_knowledge, payload, request.question)
    return ApiResponse(
        data=RuntimeKnowledgePreviewData(
            expanded_question=context.expanded_question,
            matched_terms=[term.term for term in context.matched_terms],
            matched_rules=[rule.title or rule.rule_id for rule in context.matched_rules],
            conflicting_rules=sorted(
                {
                    str(item["title"])
                    for item in context.rule_applications
                    if item["reason"] == "unresolved_exclusive_conflict"
                }
            ),
        )
    )


@router.put(
    "/{search_answer_profile_id}/runtime-knowledge/rules/{rule_id}/clarification",
    response_model=ApiResponse[RuntimeKnowledgeData],
)
async def put_rule_clarification(
    search_answer_profile_id: str, rule_id: str, request: RuleClarificationRequest
) -> ApiResponse[RuntimeKnowledgeData]:
    """ルールの確認の質問と選択肢を保存する(None は外す。#717)。"""
    oracle = OracleClient()
    await _require_search_answer_profile(oracle, search_answer_profile_id)
    try:
        payload = await save_rule_clarification(
            oracle, search_answer_profile_id, rule_id, request.clarification
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="ルールが見つかりません。") from exc
    return ApiResponse(data=_runtime_knowledge_data(search_answer_profile_id, payload))


@router.post(
    "/{search_answer_profile_id}/clarifications/suggest",
    response_model=ApiResponse[ClarificationSuggestionsData],
)
async def post_clarification_suggest(
    search_answer_profile_id: str, request: ClarificationSuggestRequest
) -> ApiResponse[ClarificationSuggestionsData]:
    """質問に出す確認(一致したルールのうち確認を持つ最初の 1 件。#717)。"""
    oracle = OracleClient()
    view = await _require_search_answer_profile(oracle, search_answer_profile_id)
    from app.api.routes.chat import _guide_user_context, prepare_chat_question

    settings, _ = resolve_search_answer_profile_settings(get_settings(), view.config)
    with profile_language_scope(settings):
        query, context_token = await prepare_chat_question(
            oracle,
            request.conversation_id,
            search_answer_profile_id,
            request.query,
            settings,
            force_token=True,
        )

        recognized_answers: tuple[ClarificationAnswer, ...] = ()
        recognition_usage: dict[str, float] = {}

        def response(
            data: ClarificationSuggestionsData,
        ) -> ApiResponse[ClarificationSuggestionsData]:
            token = context_token
            if token and (recognized_answers or recognition_usage):
                token = with_rule_selection_answers(
                    settings.app_service_token_secret,
                    token,
                    [answer.model_dump() for answer in recognized_answers],
                    elapsed_seconds=recognition_usage.get("elapsed_seconds", 0.0),
                )
            data.question_context_token = token
            return ApiResponse(data=data)

        # 方針が「前提を示して回答する」なら確認を出さない（#1839）。
        if view.config.clarification_policy == "assume":
            return response(ClarificationSuggestionsData())
        payload = await load_runtime_knowledge_payload(oracle, search_answer_profile_id)
        source_text = "\n".join(
            [
                *(
                    _guide_user_context(await oracle.list_messages(request.conversation_id))
                    if request.conversation_id
                    else ()
                ),
                request.query,
            ]
        )
        recognized_answers = await recognize_rule_selections(
            payload, query, settings, source_text=source_text, usage=recognition_usage
        )
        found = await asyncio.to_thread(
            suggest_clarification,
            payload,
            query,
            answers=recognized_answers if context_token else (),
        )
        if found is None:
            # 業務ガイドの不明・矛盾の条件（選択肢付き）を、ルールの確認と同じ形で聞く（#1238）。
            # 適用範囲は検索と同じ手がかりで確かめる（#1278）。
            settings, _ = resolve_search_answer_profile_settings(get_settings(), view.config)
            if not settings.rag_support_guide_enabled:
                return response(ClarificationSuggestionsData())
            guides = await _published_guides(oracle, search_answer_profile_id)
            # 文面の補完を OFF にしても、ガイドの既存の利用者履歴の照合は維持する。
            if request.conversation_id and not settings.rag_history_rewrite_enabled:
                query = "\n".join(
                    [
                        *_guide_user_context(await oracle.list_messages(request.conversation_id)),
                        query,
                    ]
                )
            # 検索と同じく、用語の別名で展開し、ON なら意味の照合を使う（#2398）。
            guide_query = await build_guide_query(
                oracle, settings, search_answer_profile_id, guides, query, payload
            )
            selected = select_guide(
                guides,
                guide_query,
                {},
                context=await support_guide_context(
                    oracle, guides, query, profile_scope_filters(view)
                ),
                close_margin=(
                    settings.rag_support_guide_close_match_margin
                    if settings.rag_support_guide_close_match_clarification_enabled
                    else None
                ),
            )
            selected = await resolve_text_conditions(selected, query, settings)
            guide: tuple[str, str, Any] | None = None
            if isinstance(selected, GuideChoice):
                guide = guide_choice_clarification(selected)
            elif selected is not None:
                guide = guide_clarification(selected)
            if guide is None:
                return response(ClarificationSuggestionsData())
            rule_id, title, guide_question = guide
            return response(
                ClarificationSuggestionsData(
                    suggestion=ClarificationSuggestionData(
                        rule_id=rule_id, rule_title=title, clarification=guide_question
                    )
                )
            )
        rule, clarification = found
        return response(
            ClarificationSuggestionsData(
                suggestion=ClarificationSuggestionData(
                    rule_id=str(rule.get("id", "")),
                    rule_title=str(rule.get("title", "") or rule.get("id", "")),
                    clarification=clarification,
                )
            )
        )
