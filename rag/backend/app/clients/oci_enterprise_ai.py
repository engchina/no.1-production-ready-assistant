"""OCI Enterprise AI クライアント(共有 core の Settings adapter / re-export shim)。

正本は共有 package `rag_parser_core.oci_enterprise_ai`。backend は Settings から
`OciEnterpriseAiConfig` を組み立てて駆動する薄い adapter として本モジュールを使い、
従来の import パス(`app.clients.oci_enterprise_ai`)と公開シンボルを維持する。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pr_backend_core.observability.genai import record_genai_usage
from pr_backend_core.observability.tracing import observed_span
from rag_parser_core.oci_enterprise_ai import (
    DEFAULT_MIME_TYPE,
    EnterpriseAiHttpTransport,
    EnterpriseAiIncompleteResponseError,
    EnterpriseAiRequestPreview,
    EnterpriseAiTimeoutError,
    EnterpriseAiUnsupportedInputError,
    EnterpriseAiValidationError,
    OciEnterpriseAiConfig,
    _raise_for_status_with_body,
)
from rag_parser_core.oci_enterprise_ai import (
    OciEnterpriseAiClient as _SharedOciEnterpriseAiClient,
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
    "OciEnterpriseAiClient",
    "OciEnterpriseAiConfig",
    "config_from_settings",
    "_raise_for_status_with_body",
]


def config_from_settings(
    settings: Settings, *, model_id: str | None = None
) -> OciEnterpriseAiConfig:
    """backend Settings から共有 core 用の config を組み立てる。

    vision/default model ID は model catalog 解決(config.py)で従来同等に決める。
    `model_id` を渡すと、テキストの呼び出しをそのモデルに差し替える(マルチモデル比較)。
    接続はモデルごとに引く(#533): テキストはテキストのモデルの接続、Vision は既定の
    Vision モデルの接続(テキストと違うときだけ `vision_oci_enterprise_ai_*` に入れる)。
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
    return OciEnterpriseAiConfig(
        # Endpoint URL は platform の関数で base URL にして渡す(#1440。共有 core はそのまま使う)。
        oci_enterprise_ai_endpoint=text.base_url,
        oci_enterprise_ai_api_key=text.api_key,
        oci_enterprise_ai_project_ocid=text.project_ocid,
        vision_oci_enterprise_ai_endpoint=vision.base_url if separate_vision else None,
        vision_oci_enterprise_ai_api_key=vision.api_key if separate_vision else None,
        vision_oci_enterprise_ai_project_ocid=vision.project_ocid if separate_vision else None,
        vision_model_id=vision_model_id,
        default_model_id=text_model_id,
        oci_enterprise_ai_llm_path=settings.oci_enterprise_ai_llm_path,
        oci_enterprise_ai_vlm_path=settings.oci_enterprise_ai_vlm_path,
        oci_enterprise_ai_llm_response_path=settings.oci_enterprise_ai_llm_response_path,
        oci_enterprise_ai_vlm_response_path=settings.oci_enterprise_ai_vlm_response_path,
        oci_enterprise_ai_llm_payload_template=settings.oci_enterprise_ai_llm_payload_template,
        oci_enterprise_ai_vlm_payload_template=settings.oci_enterprise_ai_vlm_payload_template,
        oci_enterprise_ai_vlm_input_mode=getattr(
            settings, "oci_enterprise_ai_vlm_input_mode", "files_api"
        ),
        oci_enterprise_ai_timeout_seconds=float(settings.oci_enterprise_ai_timeout_seconds),
        oci_enterprise_ai_max_retries=int(settings.oci_enterprise_ai_max_retries),
        text_max_output_tokens=enterprise_ai_output_token_budget(settings, text_model_id),
        vision_max_output_tokens=enterprise_ai_output_token_budget(settings, vision_model_id),
        text_reasoning_effort=enterprise_ai_reasoning_effort_to_send(settings, text_model_id),
        vision_reasoning_effort=enterprise_ai_reasoning_effort_to_send(settings, vision_model_id),
    )


class OciEnterpriseAiClient(_SharedOciEnterpriseAiClient):
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
