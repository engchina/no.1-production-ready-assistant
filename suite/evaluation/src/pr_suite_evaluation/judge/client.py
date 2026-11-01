"""判定のモデルの呼び出し（モデルの API〔OpenAI 互換〕の Responses API。#3418）。

URL の決め方（`{Endpoint URL}/responses`。path は補わない）と HTTP の失敗の分類は、3 製品と同じ
platform の `pr_enterprise_ai` の規則を使う。接続とモデルは suite の環境変数で決め、既定のモデル
を持たない（指定が無ければ失敗にする。別のモデル・接続へ黙って切り替えない）。

- `SUITE_EVALUATION_JUDGE_ENDPOINT`: モデルの API の Endpoint URL（例
  `https://api.deepseek.com/v1`）
- `SUITE_EVALUATION_JUDGE_API_KEY`: API キー（結果・ログに出さない）
- `SUITE_EVALUATION_JUDGE_PROJECT_ID`: Project ID（入れたときだけ `OpenAI-Project` ヘッダーを
  送る）
- `SUITE_EVALUATION_JUDGE_MODEL`: 判定のモデルの ID
- `SUITE_EVALUATION_JUDGE_MAX_OUTPUT_TOKENS`（任意）・
  `SUITE_EVALUATION_JUDGE_TIMEOUT_SECONDS`（任意。既定 600）

テストは `JudgeClient` の台本（録画した応答）で差し替え、実サービスを呼ばない。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from pr_enterprise_ai.errors import (
    classify_enterprise_ai_http_failure,
    enterprise_ai_provider_message,
)
from pr_enterprise_ai.urls import enterprise_ai_request_url

ENV_PREFIX = "SUITE_EVALUATION_JUDGE_"
DEFAULT_TIMEOUT_SECONDS = 600.0


class JudgeCallError(RuntimeError):
    """判定のモデルを呼べない・応答を読めない（その判定は評価不能にする）。"""

    def __init__(self, message: str, *, error_code: str = "judge_call_failed") -> None:
        super().__init__(message)
        self.error_code = error_code


class JudgeConfigError(ValueError):
    """判定の接続・モデルの設定が足りない（利用者へ返す失敗）。"""


@dataclass(frozen=True)
class JudgeReply:
    """判定のモデルの応答（出力の文と、使った分）。"""

    text: str
    model: str
    usage: Mapping[str, Any] = field(default_factory=dict)


class JudgeClient(Protocol):
    """判定のモデル（構造化出力で JSON を返す）。"""

    @property
    def model(self) -> str: ...

    def complete(
        self, *, instructions: str, prompt: str, schema: Mapping[str, Any], schema_name: str
    ) -> JudgeReply: ...


@dataclass(frozen=True)
class JudgeModelConfig:
    """判定の接続とモデル（API キーは repr・記録に出さない）。"""

    endpoint: str
    model: str
    api_key: str = field(repr=False)
    project_id: str = ""
    max_output_tokens: int | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> JudgeModelConfig:
        env = os.environ if environ is None else environ

        def value(name: str) -> str:
            return str(env.get(ENV_PREFIX + name, "")).strip()

        missing = [name for name in ("ENDPOINT", "API_KEY", "MODEL") if not value(name)]
        if missing:
            raise JudgeConfigError(
                "判定のモデルの設定が足りません: "
                + ", ".join(ENV_PREFIX + name for name in missing)
            )

        def optional_int(name: str) -> int | None:
            text = value(name)
            if not text:
                return None
            try:
                return int(text)
            except ValueError as error:
                raise JudgeConfigError(f"{ENV_PREFIX}{name} は整数にしてください。") from error

        timeout = value("TIMEOUT_SECONDS")
        return cls(
            endpoint=value("ENDPOINT"),
            model=value("MODEL"),
            api_key=value("API_KEY"),
            project_id=value("PROJECT_ID"),
            max_output_tokens=optional_int("MAX_OUTPUT_TOKENS"),
            timeout_seconds=float(timeout) if timeout else DEFAULT_TIMEOUT_SECONDS,
        )

    def provenance(self) -> dict[str, Any]:
        """記録する設定（Endpoint はホストだけ。API キー・Project ID は記録しない）。"""
        return {
            "provider": "enterprise_ai",
            "endpoint_host": urlsplit(self.endpoint).hostname or "",
            "model": self.model,
            "max_output_tokens": self.max_output_tokens,
        }


def response_text(payload: Mapping[str, Any]) -> str:
    """Responses API の応答の出力の文（`output_text`、無ければ `output[].content[]` の文）。"""
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct:
        return direct
    parts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, Mapping):
            continue
        for content in item.get("content") or []:
            if isinstance(content, Mapping) and content.get("type") in {"output_text", "text"}:
                text = content.get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "".join(parts)


class EnterpriseAiJudgeClient:
    """モデルの API の Responses API を構造化出力（JSON schema）で呼ぶ。"""

    def __init__(self, client: httpx.Client, config: JudgeModelConfig) -> None:
        self._client = client
        self._config = config

    @property
    def model(self) -> str:
        return self._config.model

    def complete(
        self, *, instructions: str, prompt: str, schema: Mapping[str, Any], schema_name: str
    ) -> JudgeReply:
        body: dict[str, Any] = {
            "model": self._config.model,
            "instructions": instructions,
            "input": prompt,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": dict(schema),
                    "strict": True,
                }
            },
        }
        if self._config.max_output_tokens is not None:
            body["max_output_tokens"] = self._config.max_output_tokens
        headers = {"Authorization": f"Bearer {self._config.api_key}"}
        if self._config.project_id:
            headers["OpenAI-Project"] = self._config.project_id
        try:
            response = self._client.post(
                enterprise_ai_request_url(self._config.endpoint),
                json=body,
                headers=headers,
                timeout=self._config.timeout_seconds,
            )
        except httpx.HTTPError as error:
            raise JudgeCallError(f"判定のモデルを呼べません: {type(error).__name__}") from error
        if response.status_code >= 400:
            failure = classify_enterprise_ai_http_failure(response.status_code, response.text)
            raise JudgeCallError(
                f"判定のモデルが失敗しました（HTTP {response.status_code}・{failure.error_code}）: "
                f"{enterprise_ai_provider_message(response.text)}",
                error_code=f"judge_{failure.kind}",
            )
        try:
            payload = response.json()
        except json.JSONDecodeError as error:
            raise JudgeCallError("判定のモデルの応答が JSON ではありません。") from error
        if not isinstance(payload, Mapping):
            raise JudgeCallError("判定のモデルの応答の形が読めません。")
        usage = payload.get("usage")
        return JudgeReply(
            text=response_text(payload),
            model=str(payload.get("model") or self._config.model),
            usage=dict(usage) if isinstance(usage, Mapping) else {},
        )
