"""根拠 1 件の原文のプレビューを、短命・対象に縛った署名つきの URL で返す（#2389）。

- `GET /api/source-previews/{token}/pages/{page_number}?dpi=`: PDF（Office は処理後の
  PDF）の 1 ページの画像（PNG）。
- `GET /api/source-previews/{token}/content?disposition=`: 原文のファイル（画像・本文・
  ページの画像を描けない PDF・ダウンロード）。

URL は Agent の backend が MCP の ``rag_read_source``（``include_preview``）で受け取り、
Agent の画面（RAG の Cookie を持たないブラウザ）が共通のプレビューの部品で開く。Cookie も
サービストークンも使わず、path のトークンだけで読む（権限 manifest では公開 path。認証は
この route が行う）。

読むたびに次を確かめる（どれかが通らなければ返さない。図の URL〔``app.api.routes.figures``〕
と同じ）:

1. トークンの署名・形式・期限（``app.rag.source_preview``。改ざん 403・期限切れ 410）。
2. トークンの利用者（``sub``）の今の状態と権限（無効な利用者・検索の権限が無ければ 403）。
3. その利用者の範囲で、``rag_read_source`` と同じ見え方の条件で根拠の chunk を読み直す
   （見えなければ 404、古い版は 409）。
4. トークンの版（``chunk_set_id``）と今の版が同じか（違えば 409）。

応答は ``Cache-Control: private, no-store``・``Referrer-Policy: no-referrer``・
``X-Content-Type-Options: nosniff``。原文は利用者がアップロードした HTML / SVG でも
スクリプトを動かさない（``Content-Security-Policy: sandbox``。文書の画面のプレビューと同じ
応答）。原文の応答には ``Access-Control-Allow-Origin: *`` を付ける（画面が本文を fetch で
読むため。Cookie を送らない URL で、トークンそのものが読む資格なので、ほかの origin に新しく
読ませる物は無い）。トークンはアクセスログに残さない（uvicorn の access log は伏せる。
``app.logging_config``）。
"""

from __future__ import annotations

import asyncio
import logging
from contextvars import Token
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import Response
from pr_backend_core.mcp import McpToolError

from app.api.routes.documents import document_content_response
from app.api.signed_access import SIGNED_RESPONSE_HEADERS, enter_token_user, signed_error
from app.clients.oracle import OracleClient
from app.config import get_settings
from app.mcp import tools as mcp_tools
from app.rag.document_crop import render_page_png
from app.rag.request_context import reset_audit_request_context
from app.rag.signed_url import SignedTokenError, SignedUrlUnavailableError
from app.rag.source_preview import SourcePreviewClaims, verify_source_preview_token

logger = logging.getLogger(__name__)
router = APIRouter()

_INVALID_MESSAGE = "原文のプレビューの URL が無効です。Agent の画面からもう一度開いてください。"
_EXPIRED_MESSAGE = (
    "原文のプレビューの URL の期限が切れました。Agent の画面からもう一度開いてください。"
)
_FORBIDDEN_MESSAGE = "この文書を見る権限がありません。"
_STALE_MESSAGE = "この根拠は文書の古い版のものです。もう一度質問してください。"
# MCP のエラー（`rag_read_source` と同じ判定）→ HTTP の status。
_TOOL_ERROR_STATUS = {
    mcp_tools.SOURCE_STALE_CODE: 409,
    mcp_tools.SOURCE_NOT_FOUND_CODE: 404,
    mcp_tools.PREVIEW_FILE_MISSING_CODE: 404,
}


def _claims(token: str) -> SourcePreviewClaims:
    try:
        return verify_source_preview_token(get_settings().app_service_token_secret, token)
    except SignedUrlUnavailableError:
        raise signed_error(503, "原文のプレビューの URL の署名の鍵が設定されていません。") from None
    except SignedTokenError as exc:
        if exc.expired:
            raise signed_error(410, _EXPIRED_MESSAGE) from None
        raise signed_error(403, _INVALID_MESSAGE) from None


async def _readable_target(claims: SourcePreviewClaims) -> tuple[Any, Any]:
    """利用者の今の範囲で根拠の chunk を読み直し、版を確かめてから文書と処理後のファイルを返す。"""
    oracle = OracleClient()
    try:
        chunk = await mcp_tools.readable_chunk(oracle, claims.document_id, claims.chunk_id)
        stored = chunk.metadata.get("chunk_set_id")
        current_chunk_set = stored.strip() or None if isinstance(stored, str) else None
        if current_chunk_set != claims.chunk_set_id:
            raise signed_error(409, _STALE_MESSAGE)
        return await mcp_tools.preview_document(oracle, claims.document_id, claims.recipe_id)
    except McpToolError as exc:
        status = _TOOL_ERROR_STATUS.get(exc.code, exc.status or 400)
        raise signed_error(status, _STALE_MESSAGE if status == 409 else exc.message) from None


async def _enter(request: Request, claims: SourcePreviewClaims) -> Token[Any] | None:
    return await enter_token_user(request, claims.subject, forbidden_message=_FORBIDDEN_MESSAGE)


async def _file(claims: SourcePreviewClaims, detail: Any, artifact: Any) -> bytes:
    try:
        return await mcp_tools.preview_file(detail, artifact, claims.variant)
    except McpToolError as exc:
        raise signed_error(404, exc.message) from None


@router.get("/{token}/pages/{page_number}", name="read_source_preview_page")
async def read_source_preview_page(
    token: str,
    page_number: Annotated[int, Path(ge=1, le=10000)],
    request: Request,
    dpi: Annotated[int, Query(ge=48, le=288)] = 144,
) -> Response:
    """署名つきのトークンの文書の 1 ページを PNG で返す。読むたびに今の権限・版を確かめる。"""
    claims = _claims(token)
    context_token = await _enter(request, claims)
    try:
        detail, artifact = await _readable_target(claims)
        data = await _file(claims, detail, artifact)
    finally:
        if context_token is not None:
            reset_audit_request_context(context_token)
    try:
        png = await asyncio.to_thread(render_page_png, data, page_number, dpi)
    except ValueError as exc:
        raise signed_error(422, str(exc)) from None
    # トークンは残さず、どの根拠の文書を誰が開いたかだけを残す。
    logger.info(
        "rag_source_preview_page_read",
        extra={
            "event": "rag_source_preview_page_read",
            "document_id": claims.document_id,
            "chunk_id": claims.chunk_id,
            "page": page_number,
        },
    )
    return Response(content=png, media_type="image/png", headers=SIGNED_RESPONSE_HEADERS)


@router.get("/{token}/content", name="read_source_preview_content")
async def read_source_preview_content(
    token: str,
    request: Request,
    disposition: Annotated[Literal["inline", "attachment"], Query()] = "inline",
) -> Response:
    """署名つきのトークンの文書の原文のファイルを返す。読むたびに今の権限・版を確かめる。"""
    claims = _claims(token)
    context_token = await _enter(request, claims)
    try:
        detail, artifact = await _readable_target(claims)
        try:
            response = await document_content_response(
                detail,
                variant=claims.variant,
                disposition=disposition,
                preprocess_artifact=artifact,
            )
        except HTTPException as exc:
            # ファイルが保存先に無い（404）・参照パスが不正（400）など。cache に残さない
            # header で返す。
            raise signed_error(exc.status_code, str(exc.detail)) from None
    finally:
        if context_token is not None:
            reset_audit_request_context(context_token)
    response.headers.update(SIGNED_RESPONSE_HEADERS)
    response.headers["Access-Control-Allow-Origin"] = "*"
    logger.info(
        "rag_source_preview_content_read",
        extra={
            "event": "rag_source_preview_content_read",
            "document_id": claims.document_id,
            "chunk_id": claims.chunk_id,
        },
    )
    return response
