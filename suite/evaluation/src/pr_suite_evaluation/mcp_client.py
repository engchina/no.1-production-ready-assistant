"""製品の公開の MCP（`POST /api/mcp`。Streamable HTTP の JSON-RPC）を API キーで呼ぶ（#3240）。

画面の API（Cookie / CSRF）は呼ばない。キーは `Authorization: Bearer prak_…` で送り、ログ・結果・
例外の文に出さない。HTTP の client は差し替えられる（テストは `httpx.MockTransport` の台本）。
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from typing import Any

import httpx

MCP_PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "suite-evaluation"
CLIENT_VERSION = "1"
# 例外の文に入れる応答の本文の上限（文字）。
ERROR_TEXT_MAX_CHARS = 300


class TargetError(RuntimeError):
    """Target を呼べない・出力を読めない（ケースは評価不能ではなく失敗として記録する）。"""

    error_type = "TargetError"


class McpClient:
    """製品の公開の MCP の `tools/call`。最初の呼び出しの前に 1 回だけ `initialize` する。"""

    def __init__(
        self,
        client: httpx.Client,
        mcp_url: str,
        *,
        api_key: str,
        label: str,
        error_class: type[TargetError] = TargetError,
    ) -> None:
        self._client = client
        self._url = mcp_url
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        self._label = label
        self._error = error_class
        self._ids = itertools.count(1)
        self._initialized = False

    def _post(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        response = self._client.post(
            self._url,
            json={"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params},
            headers=self._headers,
        )
        if response.status_code >= 400:
            raise self._error(
                f"{self._label} の MCP（{method}）が失敗しました（HTTP {response.status_code}）: "
                f"{response.text[:ERROR_TEXT_MAX_CHARS]}"
            )
        try:
            message = response.json()
        except ValueError as error:
            raise self._error(
                f"{self._label} の MCP（{method}）の応答が JSON ではありません。"
            ) from error
        if not isinstance(message, Mapping):
            raise self._error(f"{self._label} の MCP（{method}）の応答の形が読めません。")
        failure = message.get("error")
        if isinstance(failure, Mapping):
            raise self._error(
                f"{self._label} の MCP（{method}）がエラーを返しました: "
                f"{str(failure.get('message'))[:ERROR_TEXT_MAX_CHARS]}"
            )
        result = message.get("result")
        if not isinstance(result, Mapping):
            raise self._error(f"{self._label} の MCP（{method}）の応答に result がありません。")
        return result

    def _initialize(self) -> None:
        if self._initialized:
            return
        self._post(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
        )
        self._initialized = True

    def call(
        self, tool: str, arguments: Mapping[str, Any], *, schema_version: int
    ) -> dict[str, Any]:
        """ツールを呼び、構造化の出力（`structuredContent`）を返す。

        ツールのエラー（`isError`）と、知らない出力の版（`schema_version`）は失敗にする（知らない形の
        出力を推測で読まない）。
        """
        self._initialize()
        result = self._post("tools/call", {"name": tool, "arguments": dict(arguments)})
        content = result.get("structuredContent")
        if result.get("isError") is True:
            code = content.get("error_code") if isinstance(content, Mapping) else None
            message = content.get("message") if isinstance(content, Mapping) else None
            raise self._error(
                f"{self._label} の MCP のツール {tool} がエラーを返しました"
                f"（{code or 'error'}）: {str(message or '')[:ERROR_TEXT_MAX_CHARS]}"
            )
        if not isinstance(content, Mapping):
            raise self._error(
                f"{self._label} の MCP のツール {tool} の出力に structuredContent がありません。"
            )
        version = content.get("schema_version")
        if version != schema_version:
            raise self._error(
                f"{self._label} の MCP のツール {tool} の出力の版（schema_version={version}）に"
                f"対応していません（対応は {schema_version}）。"
            )
        return dict(content)
