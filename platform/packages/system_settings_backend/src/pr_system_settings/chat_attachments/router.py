"""会話の添付の API（3 製品で同じ形。製品は `/api` の下に置く。#2517）。

- `POST /chat-attachments`（multipart `file`・任意 `conversation_id`）→ 添付の要約（原本を保存し、
  解析はしない。会話には送信のときに結び付ける。#2652）。
- `GET /chat-attachments?conversation_id=…` → 会話の添付（無ければ自分が添付して送る前のもの）
  と上限。
- `DELETE /chat-attachments/{attachment_id}` → 外す。

権限は製品の API の manifest が判定する（チャットの権限）。会話の権限（見えるか・持ち主）は
製品の `conversation_owner(principal, conversation_id)` が判定し、見えなければ 404 の例外を投げ、
見える会話は持ち主の UUID（分からなければ空文字）を返す。会話の添付の一覧は閲覧なので見える
会話なら返し、会話への添付は内容を足す操作なので持ち主だけ（`can_add_user_content`。
system_admin でも他の利用者の会話・持ち主の分からない会話は 403。#3244）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from pr_backend_core.schemas import ApiResponse
from starlette.concurrency import run_in_threadpool

from ..auth.visibility import (
    OTHERS_CONTENT_FORBIDDEN_MESSAGE,
    OTHERS_CONTENT_FORBIDDEN_STATUS,
    can_add_user_content,
)
from .limits import file_too_large
from .models import ChatAttachmentData, ChatAttachmentListData
from .service import AttachmentPrincipal, ChatAttachmentService

_READ_CHUNK_BYTES = 1024 * 1024


def build_chat_attachment_router(
    *,
    get_service: Callable[[], ChatAttachmentService],
    current_principal: Callable[..., Any],
    conversation_owner: Callable[[Any, str], str],
    tags: list[str] | None = None,
) -> APIRouter:
    router = APIRouter(tags=list(tags or ["chat-attachments"]))
    principal_dependency = Depends(current_principal)

    async def read_limited(file: UploadFile, max_bytes: int) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while chunk := await file.read(_READ_CHUNK_BYTES):
            size += len(chunk)
            if size > max_bytes:
                raise file_too_large(max_bytes)
            chunks.append(chunk)
        return b"".join(chunks)

    @router.post("/chat-attachments", response_model=ApiResponse[ChatAttachmentData])
    async def upload_chat_attachment(
        request: Request,
        file: Annotated[UploadFile, File()],
        conversation_id: Annotated[str | None, Form(max_length=128)] = None,
        principal: Any = principal_dependency,
    ) -> ApiResponse[ChatAttachmentData]:
        """ファイルを添付する（検査して原本を保存する。解析と会話への結び付けは送信の後）。"""
        service = get_service()
        owner: str | None = None
        if conversation_id:
            owner = await run_in_threadpool(conversation_owner, principal, conversation_id)
            # 会話への添付は持ち主だけ（見える他の利用者の会話は 403。#3244）。
            if not can_add_user_content(principal, owner):
                raise HTTPException(
                    status_code=OTHERS_CONTENT_FORBIDDEN_STATUS,
                    detail=OTHERS_CONTENT_FORBIDDEN_MESSAGE,
                )
        data = await read_limited(file, service.limits().max_bytes)
        attachment = await service.upload(
            _principal(principal),
            file_name=file.filename or "",
            data=data,
            conversation_id=conversation_id or None,
            conversation_owner_user_uuid=owner,
        )
        return ApiResponse(data=attachment)

    @router.get("/chat-attachments", response_model=ApiResponse[ChatAttachmentListData])
    def list_chat_attachments(
        conversation_id: Annotated[str | None, Query(max_length=128)] = None,
        principal: Any = principal_dependency,
    ) -> ApiResponse[ChatAttachmentListData]:
        """会話の添付と上限（`conversation_id` が無ければ、自分が添付して送る前のもの）。

        見える会話なら持ち主でなくても返す（閲覧。#3244）。
        """
        if conversation_id:
            conversation_owner(principal, conversation_id)
        return ApiResponse(
            data=get_service().list_items(
                _principal(principal), conversation_id=conversation_id or None
            )
        )

    @router.delete("/chat-attachments/{attachment_id}", response_model=ApiResponse[dict[str, bool]])
    def delete_chat_attachment(
        attachment_id: str, principal: Any = principal_dependency
    ) -> ApiResponse[dict[str, bool]]:
        """添付を外す（行を消す）。"""
        get_service().delete(_principal(principal), attachment_id)
        return ApiResponse(data={"deleted": True})

    return router


def _principal(value: Any) -> AttachmentPrincipal:
    return value  # type: ignore[no-any-return]
