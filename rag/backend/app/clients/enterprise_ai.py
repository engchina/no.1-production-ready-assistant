"""OCI Enterprise AI クライアント(共有 core の Settings adapter / re-export shim)。

正本は共有 package `rag_parser_core.enterprise_ai`。backend は Settings から
`EnterpriseAiConfig` を組み立てて駆動する薄い adapter として本モジュールを使い、
従来の import パス(`app.clients.enterprise_ai`)と公開シンボルを維持する。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pr_backend_core.observability.genai import record_genai_usage
from pr_backend_core.observability.tracing import observed_span
from rag_parser_core.enterprise_ai import (
    DEFAULT_MIME_TYPE,
    EnterpriseAiConfig,
    EnterpriseAiHttpTransport,
    EnterpriseAiIncompleteResponseError,
    EnterpriseAiRequestPreview,
    EnterpriseAiTimeoutError,
    EnterpriseAiUnsupportedInputError,
    EnterpriseAiValidationError,
    _raise_for_status_with_body,
)
from rag_parser_core.enterprise_ai import (
    EnterpriseAiClient as _SharedEnterpriseAiClient,
)

from app.config import (
    Settings,
    enterprise_ai_connection_for_model,
    enterprise_ai_default_model_id,
    enterprise_ai_output_token_budget,
    enterprise_ai_reasoning_effort_to_send,
    enterprise_ai_vision_model_id,
    get_settings,
)

__all__ = [
    "DEFAULT_MIME_TYPE",
    "EnterpriseAiHttpTransport",
    "EnterpriseAiIncompleteResponseError",
    "EnterpriseAiRequestPreview",
    "EnterpriseAiTimeoutError",
    "EnterpriseAiUnsupportedInputError",
    "EnterpriseAiValidationError",
    "EnterpriseAiClient",
    "EnterpriseAiConfig",
    "config_from_settings",
    "_raise_for_status_with_body",
]


def config_from_settings(settings: Settings, *, model_id: str | None = None) -> EnterpriseAiConfig:
    """backend Settings から共有 core 用の config を組み立てる。

    vision/default model ID は model catalog 解決(config.py)で従来同等に決める。
    `model_id` を渡すと、テキストの呼び出しをそのモデルに差し替える(マルチモデル比較)。
    接続はモデルごとに引く(#533): テキストはテキストのモデルの接続、Vision は既定の
    Vision モデルの接続(テキストと違うときだけ `vision_enterprise_ai_*` に入れる)。
    推論(思考)の量(#1425)も、それぞれのモデルの送る値を入れる(none は空 = 送らない。
    解釈は platform の `reasoning_effort_to_send`)。
    出力の上限(#1719)も、それぞれのモデルの値(platform の `enterprise_ai_output_token_budget`。
    モデルの出力の上限と入力の上限の小さい方。prompt はここでは見積もらない)を入れる。
    """
    text_model_id = model_id or enterprise_ai_default_model_id(settings)
    vision_model_id = enterprise_ai_vision_model_id(settings)
    text = enterprise_ai_connection_for_model(settings, text_model_id)
    vision = enterprise_ai_connection_for_model(settings, vision_model_id)
    separate_vision = vision.connection_id != text.connection_id
    return EnterpriseAiConfig(
        # Endpoint URL は platform の関数で base URL にして渡す(#1440。共有 core はそのまま使う)。
        enterprise_ai_endpoint=text.base_url,
        enterprise_ai_api_key=text.api_key,
        enterprise_ai_project_id=text.project_id,
        vision_enterprise_ai_endpoint=vision.base_url if separate_vision else None,
        vision_enterprise_ai_api_key=vision.api_key if separate_vision else None,
        vision_enterprise_ai_project_id=vision.project_id if separate_vision else None,
        vision_model_id=vision_model_id,
        default_model_id=text_model_id,
        enterprise_ai_llm_path=settings.enterprise_ai_llm_path,
        enterprise_ai_vlm_path=settings.enterprise_ai_vlm_path,
        enterprise_ai_llm_response_path=settings.enterprise_ai_llm_response_path,
        enterprise_ai_vlm_response_path=settings.enterprise_ai_vlm_response_path,
        enterprise_ai_llm_payload_template=settings.enterprise_ai_llm_payload_template,
        enterprise_ai_vlm_payload_template=settings.enterprise_ai_vlm_payload_template,
        enterprise_ai_vlm_input_mode=getattr(settings, "enterprise_ai_vlm_input_mode", "files_api"),
        enterprise_ai_timeout_seconds=float(settings.enterprise_ai_timeout_seconds),
        enterprise_ai_max_retries=int(settings.enterprise_ai_max_retries),
        text_max_output_tokens=enterprise_ai_output_token_budget(settings, text_model_id),
        vision_max_output_tokens=enterprise_ai_output_token_budget(settings, vision_model_id),
        text_reasoning_effort=enterprise_ai_reasoning_effort_to_send(settings, text_model_id),
        vision_reasoning_effort=enterprise_ai_reasoning_effort_to_send(settings, vision_model_id),
    )


class EnterpriseAiClient(_SharedEnterpriseAiClient):
    """Settings から config を組み立てて共有 client を駆動する backend adapter。"""

    def __init__(
        self,
        settings: Settings | None = None,
        http_transport: EnterpriseAiHttpTransport | None = None,
        *,
        model_id: str | None = None,
    ) -> None:
        # マルチモデル比較では生成モデルだけを差し替える。共有 core は frozen config の
        # default_model_id をペイロードへ焼き込むため、差し替えたモデル(とその接続。#533)の
        # config で駆動する。
        config = config_from_settings(settings or get_settings(), model_id=model_id or None)
        super().__init__(config, http_transport=http_transport)

    async def _post_enterprise_ai(
        self, path: str, payload: Mapping[str, Any], *, vision: bool = False
    ) -> Mapping[str, Any]:
        with observed_span(
            "gen_ai.chat",
            kind="client",
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": "oracle",
                "gen_ai.request.model": str(payload.get("model") or ""),
            },
        ) as span:
            span.set_call_input(payload, restricted=True)
            response = await super()._post_enterprise_ai(path, payload, vision=vision)
            span.set_call_output(response)
            record_genai_usage(span, response.get("usage"))
            return response
