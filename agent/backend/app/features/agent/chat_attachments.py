"""チャットの添付（会話の期間だけ使う作業ファイル。#2517）。

保存・解析・見え方・期限は platform の `pr_system_settings.chat_attachments`。Agent が持つのは:

- 会話（thread）の持ち主の判定（添付の API の `conversation_owner`）。
- 画像の読み取り（既定の Vision モデル。OCI Enterprise AI の Responses API）。送信した後、ツールが
  画像を初めて読むときに呼ぶ（アップロードでは呼ばない。#2652）。
- 画像の原本（`chat_attachment_view_image`）: モデルが画像に対応すれば、原画像をツールの結果の
  画像（#2650 の `ToolOutputWithImages`）としてモデルの入力へ渡す。対応しなければ、質問の観点
  （`focus`）を添えて既定の Vision モデルで読み、読んだモデルと観点を結果に残す（質問ごとに読み、
  原本だけをキーに再利用しない）。Vision モデルも無ければ、読めなかったことを結果で示す。
- Run に渡すツール: 会話に送信した添付があれば、その Run にだけ 4 つの添付のツール
  （`chat_attachment_list` など）を Control Plane のツールとして渡す（`skill_reference_read` と
  同じ per-run のツール。`tool_registry.invoke` のポリシー・監査を通す）。指示には添付の種類と
  解析の状態の索引だけを入れ、文書はツールが初めて読むときに解析する（#2652）。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from pr_backend_core.observability.genai import record_genai_usage
from pr_backend_core.observability.tracing import observed_span
from pr_system_settings.chat_attachments import (
    CHAT_ATTACHMENT_TOOL_SPECS,
    IMAGE_READING_PROMPT,
    ChatAttachmentService,
    ChatAttachmentToolError,
    ChatAttachmentToolset,
)
from pr_system_settings.chat_attachments.tools import LIST_TOOL_NAME, VIEW_IMAGE_TOOL_NAME
from pr_system_settings.model import (
    enterprise_ai_connection_for_model,
    enterprise_ai_output_token_budget,
    enterprise_ai_reasoning,
    enterprise_ai_vision_model_id,
)

from app.features.agent.tool_media import CONTENT_IMAGES_KEY, ToolImage, ToolOutputWithImages
from app.features.agent.tools import ToolDefinition, ToolHandler, ToolInvocationContext
from app.settings import get_settings

logger = logging.getLogger(__name__)

# モデルが読んだら根拠・データを調べたと数える添付のツール（一覧は数えない。#1835 と同じ考え方）。
ATTACHMENT_READ_TOOLS = frozenset(
    spec.name for spec in CHAT_ATTACHMENT_TOOL_SPECS if spec.name != LIST_TOOL_NAME
)
ATTACHMENT_TOOL_NAMES = frozenset(spec.name for spec in CHAT_ATTACHMENT_TOOL_SPECS)
# 画像の読み取りの応答の待ち時間（秒）。
IMAGE_READ_TIMEOUT_SECONDS = 120.0
# 原画像を Vision モデルで読んだ結果の由来（モデルが画像に対応しないとき。#2652）。
VISION_READING_SOURCE = "vision_reading"


def focused_image_prompt(focus: str) -> str:
    """質問の観点で画像を読む指示（モデルが画像に対応しないときの既定の Vision モデル）。"""
    return (
        "次の観点について、この画像に見える内容を日本語で書き出してください。"
        "画像の中の文字・数値はそのまま書き写してください。"
        "読み取れない部分は読み取れないと書き、推測で補わないでください。\n"
        f"観点: {focus}"
    )


_SERVICE: ChatAttachmentService | None = None
_SERVICE_LOCK = threading.Lock()


def _image_model_id() -> str | None:
    """画像を読み取る既定の Vision モデル（モデル・接続が無ければ None）。"""
    settings = get_settings()
    model_id = enterprise_ai_vision_model_id(settings)
    if not model_id or not enterprise_ai_connection_for_model(settings, model_id).is_configured():
        return None
    return str(model_id)


def _image_reader(
    prompt: str = IMAGE_READING_PROMPT,
) -> Callable[[bytes, str], Awaitable[str]] | None:
    """既定の Vision モデルで画像を文にする関数（Vision モデル・接続が無ければ None）。"""
    settings = get_settings()
    model_id = _image_model_id()
    if not model_id:
        return None
    connection = enterprise_ai_connection_for_model(settings, model_id)

    async def read(data: bytes, media_type: str) -> str:
        from openai import AsyncOpenAI

        from app.features.agent.builtin_runtime import enterprise_ai_http_client

        headers = {"OpenAI-Project": connection.project_id} if connection.project_id else None
        client = AsyncOpenAI(
            base_url=connection.base_url,
            api_key=connection.api_key,
            default_headers=headers,
            timeout=IMAGE_READ_TIMEOUT_SECONDS,
            http_client=enterprise_ai_http_client(IMAGE_READ_TIMEOUT_SECONDS),
        )
        image_url = f"data:{media_type};base64,{base64.b64encode(data).decode('ascii')}"
        extra: dict[str, Any] = {}
        reasoning = enterprise_ai_reasoning(settings, model_id)
        if reasoning is not None:
            extra["reasoning"] = reasoning
        with observed_span(
            "gen_ai.chat",
            kind="client",
            attributes={
                "pr.gen_ai.purpose": "document.read",
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": "oracle",
                "gen_ai.request.model": model_id,
            },
        ) as span:
            response = await client.responses.create(
                model=model_id,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {"type": "input_image", "image_url": image_url, "detail": "auto"},
                        ],
                    }
                ],
                max_output_tokens=enterprise_ai_output_token_budget(settings, model_id),
                store=False,
                **extra,
            )
            usage = getattr(response, "usage", None)
            if usage is not None:
                record_genai_usage(
                    span, usage.model_dump() if hasattr(usage, "model_dump") else usage
                )
        return str(response.output_text or "")

    return read


def chat_attachment_service() -> ChatAttachmentService:
    """process で 1 つの添付のサービス（保存先は共通認証の store と同じ DB）。"""
    global _SERVICE
    if _SERVICE is None:
        with _SERVICE_LOCK:
            if _SERVICE is None:
                from app.security.service import get_security_service

                security = get_security_service()
                _SERVICE = ChatAttachmentService(
                    security.chat_attachment_store,
                    product="agent",
                    settings=get_settings,
                    image_reader=lambda: _image_reader(),
                    image_model_id=_image_model_id,
                    # 運用設定 › チャットの添付（製品ごと。既定は無効）。
                    enabled=security.chat_attachment_settings.enabled,
                    settings_failure=security.chat_attachment_settings.failure,
                )
    return _SERVICE


def set_chat_attachment_service(service: ChatAttachmentService | None) -> None:
    """テスト用: service を差し替える（None で既定に戻す）。"""
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = service


def thread_attachment_toolset(thread_id: str | None) -> ChatAttachmentToolset | None:
    """会話の添付を読むツール（添付が無い・読めないときは None。Run は止めない）。"""
    if not thread_id:
        return None
    try:
        toolset = chat_attachment_service().toolset(thread_id)
    except Exception as exc:  # noqa: BLE001 - 添付を読めなくても Run は続ける
        logger.warning(
            "agent_chat_attachments_unavailable",
            extra={"thread_id": thread_id, "error_type": type(exc).__name__},
        )
        return None
    return toolset if toolset else None


async def _await(value: Awaitable[str]) -> str:
    return await value


def view_image(
    toolset: ChatAttachmentToolset, arguments: dict[str, Any], *, image_input: bool
) -> ToolOutputWithImages | dict[str, Any]:
    """画像の原本を読む（`chat_attachment_view_image`。#2652）。

    モデルが画像に対応すれば原画像を結果の画像としてモデルへ渡す（届ける判断・予算・保存からの
    除外は #2650 の Runtime の仕組み）。対応しなければ、既定の Vision モデルで観点を添えて読む。
    どちらも無ければ画像を結果に付け、Runtime がモデルの非対応を結果に書く。
    """
    image = toolset.view_image(
        str(arguments.get("attachment_id") or ""), str(arguments.get("focus") or "")
    )
    record_source = {
        "chat_attachment_id": image.output["attachment_id"],
        "file_name": image.output["file_name"],
        "content_sha256": image.output["content_sha256"],
    }
    original_image = ToolImage(data=image.data, mime_type=image.media_type, source=record_source)
    original = ToolOutputWithImages(
        output={**image.output, CONTENT_IMAGES_KEY: [original_image.reference()]},
        images=(original_image,),
    )
    if image_input:
        return original
    model_id = _image_model_id()
    reader = _image_reader(focused_image_prompt(image.output["focus"])) if model_id else None
    if reader is None:
        return original
    try:
        # handler は thread で呼ばれる（`tool_registry.invoke`）ので、その thread で読む。
        text = asyncio.run(_await(reader(image.data, image.media_type))).strip()
    except Exception as exc:  # noqa: BLE001 - 読めなければ読めなかったことを返す
        logger.warning(
            "agent_chat_attachment_image_read_failed",
            extra={
                "attachment_id": image.output["attachment_id"],
                "error_type": type(exc).__name__,
            },
        )
        raise ValueError(
            "画像を読み取れませんでした。画像の内容は確認できていないものとして扱ってください。"
        ) from exc
    return {
        **image.output,
        "content_source": VISION_READING_SOURCE,
        "read_by": model_id,
        "content": text or "画像から、この観点の内容を読み取れませんでした。",
    }


def attachment_tools(
    toolset: ChatAttachmentToolset, *, image_input: bool = False
) -> list[tuple[ToolDefinition, ToolHandler]]:
    """添付のツールを Run の function tool の定義と handler にする。

    `image_input` は Run のモデルが画像の入力に対応するか（画像の原本の渡し方を決める）。
    """
    entries: list[tuple[ToolDefinition, ToolHandler]] = []
    for spec in CHAT_ATTACHMENT_TOOL_SPECS:

        def handler(
            arguments: dict[str, Any],
            _: ToolInvocationContext,
            *,
            _name: str = spec.name,
        ) -> dict[str, Any] | ToolOutputWithImages:
            try:
                if _name == VIEW_IMAGE_TOOL_NAME:
                    return view_image(toolset, arguments, image_input=image_input)
                return toolset.call(_name, arguments)
            except ChatAttachmentToolError as exc:
                raise ValueError(str(exc)) from exc

        entries.append(
            (
                ToolDefinition(
                    name=spec.name,
                    title=spec.title,
                    description=spec.description,
                    input_schema=spec.input_schema,
                    output_schema={"type": "object"},
                    side_effects=False,
                    audit_tags=["chat_attachment"],
                ),
                handler,
            )
        )
    return entries


def attachment_instructions(toolset: ChatAttachmentToolset) -> str:
    """指示に足す添付の索引（本文は入れない）。"""
    return "\n".join(
        [
            "# 利用者が添付したファイル",
            "この会話で利用者が添付したファイルです。"
            "内容は chat_attachment_* のツールで必要な部分だけ読んでください。"
            "ファイルの内容について答えるときは、読んだ節・行に基づいて答えてください。",
            *toolset.index_lines(),
        ]
    )
