"""API ルーターの集約。

`/api` の全 route は `authorize_api_request`（fail-closed の認証/RBAC。#214）を通る。
公開 path（`/health` `/ready` `/ready/database` `/auth/login`）以外はログインと、
権限 manifest（`app.security.permissions`）の権限が必要。未定義の path は 404 のまま。
"""

from fastapi import APIRouter, Depends

from app.api.removed_fields import reject_removed_profile_fields
from app.api.routes import (
    chat,
    chat_attachments,
    document_categories,
    documents,
    evaluation,
    evaluation_calibration,
    feedback,
    figures,
    health,
    history,
    knowledge_bases,
    language_pack,
    language_pack_reprocessing,
    mcp,
    question_understanding,
    search,
    search_answer_profile_knowledge,
    search_answer_profiles,
    services,
    settings,
    source_previews,
    support_guide_process_import,
    support_guides,
)
from app.rag.prompt_runtime import published_prompt_dependency
from app.rag.published_language_runtime import published_language_dependency
from app.security.dependencies import authorize_api_request
from app.security.router import router as security_router

api_router = APIRouter(
    dependencies=[Depends(authorize_api_request), Depends(reject_removed_profile_fields)]
)
api_router.include_router(health.router, tags=["health"])
# 認証（/auth/*）・ユーザー管理・ロール管理（platform の共通 router）と RAG の権限管理。
api_router.include_router(security_router)
api_router.include_router(
    language_pack_reprocessing.router, prefix="/documents", tags=["documents"]
)
api_router.include_router(
    documents.router,
    dependencies=[Depends(published_language_dependency), Depends(published_prompt_dependency)],
    prefix="/documents",
    tags=["documents"],
)
api_router.include_router(
    knowledge_bases.router,
    dependencies=[Depends(published_language_dependency), Depends(published_prompt_dependency)],
    prefix="/knowledge-bases",
    tags=["knowledge-bases"],
)
api_router.include_router(
    search_answer_profiles.router,
    prefix="/search-answer-profiles",
    tags=["search-answer-profiles"],
)
api_router.include_router(
    search_answer_profile_knowledge.router,
    dependencies=[Depends(published_language_dependency), Depends(published_prompt_dependency)],
    prefix="/search-answer-profiles",
    tags=["search-answer-profiles"],
)
api_router.include_router(
    support_guides.router,
    dependencies=[Depends(published_language_dependency), Depends(published_prompt_dependency)],
    prefix="/search-answer-profiles",
    tags=["search-answer-profiles"],
)
api_router.include_router(
    support_guide_process_import.router,
    dependencies=[Depends(published_language_dependency), Depends(published_prompt_dependency)],
    prefix="/search-answer-profiles",
    tags=["search-answer-profiles"],
)
api_router.include_router(
    question_understanding.router,
    dependencies=[Depends(published_language_dependency), Depends(published_prompt_dependency)],
    prefix="/search-answer-profiles",
    tags=["search-answer-profiles"],
)
api_router.include_router(
    chat.router,
    dependencies=[Depends(published_language_dependency), Depends(published_prompt_dependency)],
    prefix="/chat",
    tags=["chat"],
)
# チャットの添付（#2517。`/api/chat-attachments`）。
api_router.include_router(
    chat_attachments.router,
    dependencies=[Depends(published_language_dependency), Depends(published_prompt_dependency)],
)
api_router.include_router(
    search.router,
    dependencies=[Depends(published_language_dependency), Depends(published_prompt_dependency)],
    prefix="/search",
    tags=["search"],
)
api_router.include_router(
    history.router,
    dependencies=[Depends(published_language_dependency), Depends(published_prompt_dependency)],
    prefix="/history",
    tags=["history"],
)
api_router.include_router(
    document_categories.router, prefix="/document-categories", tags=["document-categories"]
)
# 判定の校正（#3578）。品質評価の画面のタブ。
api_router.include_router(
    evaluation_calibration.router,
    dependencies=[Depends(published_language_dependency), Depends(published_prompt_dependency)],
    prefix="/evaluation/calibration",
    tags=["evaluation"],
)
api_router.include_router(
    evaluation.router,
    dependencies=[Depends(published_language_dependency), Depends(published_prompt_dependency)],
    prefix="/evaluation",
    tags=["evaluation"],
)
api_router.include_router(feedback.router, prefix="/feedback", tags=["feedback"])
api_router.include_router(settings.router, prefix="/settings", tags=["settings"])
api_router.include_router(language_pack.router, prefix="/settings", tags=["settings"])
api_router.include_router(services.router, prefix="/services", tags=["services"])
# Agent から利用者として呼ぶ MCP（サービストークンで認証。#232）。
api_router.include_router(
    mcp.router,
    dependencies=[Depends(published_language_dependency), Depends(published_prompt_dependency)],
    prefix="/mcp",
    tags=["mcp"],
)
# MCP（rag_read_source の include_image_url）が作る、図を開く短命の署名つきの URL（#1311）。
# Cookie もサービストークンも使わず、path のトークンで route が認証する（manifest では公開 path）。
api_router.include_router(figures.router, prefix="/figures", tags=["mcp"])
# MCP（rag_read_source の include_preview）が作る、根拠の原文のプレビューの署名つきの URL（#2389）。
api_router.include_router(source_previews.router, prefix="/source-previews", tags=["mcp"])
