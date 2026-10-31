"""OCI Enterprise AI クライアント。

RAG と同じ `app.clients.oci_enterprise_ai.OciEnterpriseAiClient` import path を提供する。
NL2SQL 側の既存 direct client を薄く包み、settings API からは RAG と同じ async
interface で呼び出す。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from app.features.nl2sql.enterprise_ai_client import OciEnterpriseAiDirectClient
from app.features.nl2sql.structured_outputs import TextOutput, response_format
from app.settings import Settings, get_settings

type SyncCallRunner = Callable[[Callable[[], Any]], Any]


class OciEnterpriseAiClient:
    """Settings から OCI Enterprise AI 呼び出しを行う backend adapter。"""

    def __init__(
        self,
        settings: Settings | None = None,
        sync_call_runner: SyncCallRunner | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._client = OciEnterpriseAiDirectClient(self._settings)
        self._sync_call_runner = sync_call_runner

    async def generate(self, prompt: str, context: str = "") -> str:
        """Enterprise AI の text model から応答テキストを取得する。"""
        raw = await self._run_sync(
            lambda: self._client.generate(
                prompt=prompt,
                context=context,
                system_prompt="日本語で簡潔に回答し、text に格納してください。",
                response_format=response_format(TextOutput),
            )
        )
        return TextOutput.model_validate_json(raw).text

    async def generate_from_image(
        self,
        image_bytes: bytes,
        prompt: str,
        *,
        mime_type: str = "image/jpeg",
    ) -> str:
        """Enterprise AI の vision model から応答テキストを取得する。"""
        raw = await self._run_sync(
            lambda: self._client.generate_from_image(
                image_bytes,
                prompt,
                mime_type=mime_type,
                response_format=response_format(TextOutput),
            )
        )
        return TextOutput.model_validate_json(raw).text

    async def _run_sync(self, operation: Callable[[], str]) -> str:
        if self._sync_call_runner is not None:
            return str(self._sync_call_runner(operation))
        return await asyncio.to_thread(operation)
