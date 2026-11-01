"""モデルの API（OpenAI 互換の API）を呼ぶ URL の決め方（#1440）。

#3418 で pr_system_settings から移した。
"""

from __future__ import annotations

from typing import Final

DEFAULT_ENTERPRISE_AI_API_PATH: Final = "/responses"


def enterprise_ai_base_url(endpoint: str) -> str:
    """接続の Endpoint URL から OpenAI 互換 API の base URL を決める（#1440）。

    3 製品と suite の呼び出し・接続テストの URL はすべてこれ（と `enterprise_ai_request_url`）で
    決める。
    入力どおり使い、前後の空白と末尾の `/` だけを除く。path は補わない（OpenAI 互換 API は
    `https://api.openai.com/v1`・`https://dashscope.aliyuncs.com/compatible-mode/v1` のように
    利用者が API の base URL まで書く）。以前の RAG は path の無い URL に `/openai/v1` を足して
    いたため、OpenAI 互換 API（例 `https://api.deepseek.com`）で 404 になっていた。
    """
    return endpoint.strip().rstrip("/")


def enterprise_ai_request_url(endpoint: str, api_path: str = DEFAULT_ENTERPRISE_AI_API_PATH) -> str:
    """接続の Endpoint URL と API path から、呼ぶ URL を決める（#1440）。

    `api_path` が空なら `/responses`。`http://` / `https://` で始まる `api_path` はそのまま使う。
    """
    path = (api_path or "").strip() or DEFAULT_ENTERPRISE_AI_API_PATH
    if path.startswith(("http://", "https://")):
        return path
    return f"{enterprise_ai_base_url(endpoint)}/{path.lstrip('/')}"
