"""図の根拠の元の画像を、短命・対象に縛った署名つきの URL で返す（#1311）。

`GET /api/figures/{token}`。RAG の画面の「図を開く」（#2428）は、Cookie のセッションで同じ図を
読む `GET /api/figures?document_id=&chunk_id=`（下の ``read_chunk_figure``）を使う。

URL は Agent の backend が MCP の ``rag_read_source``（``include_image_url``）で受け取り、Agent の
画面（RAG の Cookie を持たないブラウザ）が開く。Cookie もサービストークンも使わず、path の
トークンだけで読む（権限 manifest では公開 path。認証はこの route が行う）。

読むたびに次を確かめる（どれかが通らなければ画像を返さない）:

1. トークンの署名・形式・期限（`app.rag.figure_url`。改ざん 403・期限切れ 410）。
2. トークンの利用者（``sub``）の今の状態と権限（無効な利用者・検索の権限が無ければ 403）。
3. その利用者の範囲で、``rag_read_source`` と同じ見え方の条件（tenant・利用できるナレッジ
   ベース・INDEXED・有効な chunk_set）で chunk を読み直す（見えなければ 404、古い版は 409）。
4. トークンの版（``chunk_set_id``）と今の版が同じか（違えば 409）。

応答は ``Cache-Control: private, no-store``・``Referrer-Policy: no-referrer``・
``X-Content-Type-Options: nosniff`` で、ファイル名は RFC 5987（``filename*=UTF-8''…``）。トークンは
アクセスログに残さない（Nginx は route の種類だけ、uvicorn の access log は伏せる。
`app.logging_config`）。
"""

from __future__ import annotations

import logging
import re
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pr_backend_core.mcp import McpToolError

from app.api.signed_access import SIGNED_RESPONSE_HEADERS, enter_token_user, signed_error
from app.clients.oracle import OracleClient
from app.config import get_settings
from app.mcp import tools as mcp_tools
from app.rag.figure_region import figure_region
from app.rag.figure_url import (
    FigureTokenError,
    FigureUrlUnavailableError,
    verify_figure_token,
)
from app.rag.rate_limit import enforce_rate_limit
from app.rag.request_context import reset_audit_request_context

logger = logging.getLogger(__name__)
router = APIRouter()

# 画像の応答とエラーの応答の両方に付ける（URL を共有の cache・Referer に残さない）。
FIGURE_RESPONSE_HEADERS = SIGNED_RESPONSE_HEADERS
# ファイル名に使わない文字（path の区切り・制御文字・引用符など）。
_UNSAFE_FILENAME = re.compile(r'[\x00-\x1f\x7f"\\/:*?<>|;]+')
_FILENAME_STEM_MAX = 80

_INVALID_MESSAGE = "図を開く URL が無効です。Agent の画面からもう一度開いてください。"
_EXPIRED_MESSAGE = "図を開く URL の期限が切れました。Agent の画面からもう一度開いてください。"
_FORBIDDEN_MESSAGE = "この図を開く権限がありません。"
_STALE_MESSAGE = "この図は文書の古い版のものです。もう一度検索してください。"
# MCP のエラー（`rag_read_source` と同じ判定）→ HTTP の status。
_TOOL_ERROR_STATUS = {
    mcp_tools.SOURCE_STALE_CODE: 409,
    mcp_tools.SOURCE_NOT_FOUND_CODE: 404,
    mcp_tools.IMAGE_NOT_AVAILABLE_CODE: 404,
}


def _error(status_code: int, message: str) -> HTTPException:
    return signed_error(status_code, message)


def _content_disposition(file_name: str | None, page: int | None) -> str:
    """inline の Content-Disposition（ASCII の filename と RFC 5987 の filename*）。"""
    stem = (file_name or "").rsplit(".", 1)[0]
    stem = _UNSAFE_FILENAME.sub("_", stem).strip(" ._")[:_FILENAME_STEM_MAX] or "figure"
    suffix = f"_p{page}" if page else ""
    name = f"{stem}{suffix}_figure.png"
    ascii_name = name.encode("ascii", "ignore").decode("ascii").strip(" ._") or "figure.png"
    if not ascii_name.endswith(".png"):
        ascii_name = "figure.png"
    return f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


@router.get("", name="read_chunk_figure", response_class=Response)
async def read_chunk_figure(
    request: Request,
    document_id: Annotated[str, Query(min_length=1, max_length=128)],
    chunk_id: Annotated[str, Query(min_length=1, max_length=512)],
) -> Response:
    """画面の根拠の図の画像（PNG。#2428）。Cookie のセッションの利用者の今の範囲で読む。

    RAG の根拠のカード・サイドパネルの「図を開く」が読む（Agent の画面は署名つきの URL）。
    ``rag_read_source`` と同じ見え方の条件で chunk を読み直し、保存した metadata の図の領域を
    切り出す（利用者が渡す座標は使わない）。見えない根拠・図でない根拠は 404、古い版は 409。
    """
    enforce_rate_limit("search", request)
    oracle = OracleClient()
    try:
        chunk = await mcp_tools.readable_chunk(oracle, document_id, chunk_id)
        metadata = dict(chunk.metadata)
        region = figure_region(metadata)
        crop = await mcp_tools.crop_figure(oracle, chunk, metadata, region)
    except McpToolError as exc:
        status = _TOOL_ERROR_STATUS.get(exc.code, exc.status or 400)
        raise _error(status, _STALE_MESSAGE if status == 409 else exc.message) from None
    logger.info(
        "rag_chunk_figure_read",
        extra={
            "event": "rag_chunk_figure_read",
            "document_id": document_id,
            "chunk_id": chunk_id,
            "byte_size": len(crop.png),
        },
    )
    return Response(
        content=crop.png,
        media_type=mcp_tools.IMAGE_MIME_TYPE,
        headers={
            **FIGURE_RESPONSE_HEADERS,
            "Content-Disposition": _content_disposition(
                chunk.file_name, region.page if region is not None else None
            ),
        },
    )


@router.get("/{token}", name="read_figure", response_class=Response)
async def read_figure(token: str, request: Request) -> Response:
    """署名つきのトークンの図の画像（PNG）を返す。読むたびに今の権限・版を確かめる。"""
    try:
        claims = verify_figure_token(get_settings().app_service_token_secret, token)
    except FigureUrlUnavailableError:
        raise _error(503, "図を開く URL の署名の鍵が設定されていません。") from None
    except FigureTokenError as exc:
        if exc.expired:
            raise _error(410, _EXPIRED_MESSAGE) from None
        raise _error(403, _INVALID_MESSAGE) from None
    context_token = await enter_token_user(
        request, claims.subject, forbidden_message=_FORBIDDEN_MESSAGE
    )
    try:
        # 切り出しは元のファイルを読んで描くので、検索と同じ上限で守る（include_image と同じ）。
        enforce_rate_limit("search", request)
        oracle = OracleClient()
        try:
            chunk = await mcp_tools.readable_chunk(oracle, claims.document_id, claims.chunk_id)
            metadata = dict(chunk.metadata)
            stored = metadata.get("chunk_set_id")
            current_chunk_set = stored.strip() or None if isinstance(stored, str) else None
            if current_chunk_set != claims.chunk_set_id:
                raise _error(409, _STALE_MESSAGE)
            region = figure_region(metadata)
            crop = await mcp_tools.crop_figure(oracle, chunk, metadata, region)
        except McpToolError as exc:
            status = _TOOL_ERROR_STATUS.get(exc.code, exc.status or 400)
            raise _error(status, _STALE_MESSAGE if status == 409 else exc.message) from None
    finally:
        if context_token is not None:
            reset_audit_request_context(context_token)
    # トークンは残さず、どの根拠を誰が開いたかだけを残す。
    logger.info(
        "rag_figure_url_read",
        extra={
            "event": "rag_figure_url_read",
            "document_id": claims.document_id,
            "chunk_id": claims.chunk_id,
            "byte_size": len(crop.png),
        },
    )
    return Response(
        content=crop.png,
        media_type=mcp_tools.IMAGE_MIME_TYPE,
        headers={
            **FIGURE_RESPONSE_HEADERS,
            "Content-Disposition": _content_disposition(
                chunk.file_name, region.page if region is not None else None
            ),
        },
    )
