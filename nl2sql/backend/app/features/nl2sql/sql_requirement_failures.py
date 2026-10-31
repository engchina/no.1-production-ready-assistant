"""回答前の確認の LLM の失敗の、利用者向けの理由と「詳細」（#1715）。

失敗は platform の共通の表（`pr_system_settings.enterprise_ai_errors`）の種類に分け、利用者向けの
1 行の理由（何が起きたか）と、画面の「詳細」に畳む値（種類・OCI Enterprise AI の HTTP ステータス・
モデル ID・所要時間・試行回数・OCI の request id・応答の理由）を作る。

「詳細」に出さないもの: API key・Authorization の header・prompt と context の全文・LLM の
出力の本文。応答の理由（provider_message）は platform の `enterprise_ai_provider_message` で
秘密の値を伏せ、短く切る。
"""

from __future__ import annotations

from typing import Any

from pr_system_settings.enterprise_ai_errors import (
    ENTERPRISE_AI_CALL_FAILURE_ERROR_CODES,
    ENTERPRISE_AI_FAILURE_KIND_LABELS_JA,
)

from .enterprise_ai_client import (
    EnterpriseAiDirectError,
    enterprise_ai_configuration_failure_message,
    find_enterprise_ai_error,
)

_SUBJECT = "回答前の確認"
# 次の操作（「もう一度確認」「確認せずに SQL を生成」）は画面が出す（チャットと SQL 生成の画面で
# 選べる操作が違う。MCP は呼び出し元が決める）。ここでは何が起きたか（理由）だけを書く。


def _find_direct_error(exc: BaseException | None) -> EnterpriseAiDirectError | None:
    """例外（原因の連鎖を含む）から Enterprise AI の失敗を探す（HTTP 以外の失敗も含む）。"""

    found = find_enterprise_ai_error(exc)
    if found is not None:
        return found
    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, EnterpriseAiDirectError):
            return current
        current = current.__cause__ or current.__context__
    return None


def _seconds(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:g}"


def requirement_failure_message(
    exc: BaseException, *, model_id: str = ""
) -> tuple[str, dict[str, Any]]:
    """LLM の呼び出しの失敗の `(利用者向けの文, 詳細)`。"""

    error = _find_direct_error(exc)
    model = (error.model_id if error is not None else "") or model_id
    model_label = f"（{model}）" if model else ""
    # 「回答前の確認の LLM（xai.grok-4.3）の応答」「回答前の確認の LLM の応答」
    llm = f" LLM{model_label}" if model else " LLM "
    details: dict[str, Any] = {"model_id": model}
    if error is None:
        details.update(
            {
                "kind": "unknown",
                "kind_label_ja": "不明な失敗",
                "exception_type": type(exc).__name__,
            }
        )
        return (
            f"{_SUBJECT}の{llm}の呼び出しに失敗しました（{type(exc).__name__}）。",
            details,
        )

    kind = error.failure_kind
    status = error.http_failure.status_code if error.http_failure is not None else None
    details.update(
        {
            "kind": kind or "unknown",
            "kind_label_ja": ENTERPRISE_AI_FAILURE_KIND_LABELS_JA.get(kind, "不明な失敗")
            if kind
            else "不明な失敗",
            "error_code": ENTERPRISE_AI_CALL_FAILURE_ERROR_CODES.get(kind, "") if kind else "",
            "http_status": status,
            "endpoint_host": error.endpoint_host,
            "elapsed_ms": error.elapsed_ms,
            "attempts": error.attempts or None,
            "timeout_seconds": error.timeout_seconds,
            "max_output_tokens": error.max_output_tokens,
            "upstream_request_id": error.upstream_request_id,
            "provider_message": error.provider_message,
            "retryable": bool(error.retryable),
        }
    )
    details = {key: value for key, value in details.items() if value not in (None, "")}
    if error.configuration_error:
        # 設定の誤り（401 / 403 / 404 / モデルが無い 400）は「もう一度」と案内しない（#1684）。
        return enterprise_ai_configuration_failure_message(error, f"{_SUBJECT}で "), details
    tried = error.attempts if error.attempts and error.attempts > 1 else 0
    tried_inline = f"、{tried} 回試行" if tried else ""
    tried_note = f"（{tried} 回試行）" if tried else ""
    hint = ""
    if kind == "timeout":
        limit = _seconds(error.timeout_seconds)
        within = f" {limit} 秒以内に" if limit else "規定の時間内に"
        message = f"{_SUBJECT}の{llm}が{within}応答しませんでした{tried_note}。"
    elif kind == "rate_limit":
        message = (
            f"{_SUBJECT}で OCI Enterprise AI の呼び出しの上限に達しました"
            f"（HTTP {status}{tried_inline}）。"
        )
        hint = "少し待ってから、もう一度確認してください。"
    elif kind == "unavailable":
        message = (
            f"{_SUBJECT}で OCI Enterprise AI がサーバーのエラーを返しました"
            f"（HTTP {status}{tried_inline}）。サービス側の一時的な障害の可能性があります。"
        )
    elif kind == "request":
        message = (
            f"{_SUBJECT}の要求を OCI Enterprise AI が受け付けませんでした（HTTP {status}）。"
            f"モデル{model_label}が受け付けない設定の可能性があります。"
        )
    elif kind == "connection":
        message = f"{_SUBJECT}で OCI Enterprise AI に接続できませんでした{tried_note}。"
    elif kind == "incomplete":
        limit = f"（{error.max_output_tokens} token）" if error.max_output_tokens else ""
        if error.incomplete_reason == "max_output_tokens":
            message = (
                f"{_SUBJECT}の{llm}の応答が、出力の上限{limit}に達して"
                "途中で終わりました。推論の量が多いとき・応答の形式が崩れたときに起こります。"
                "続くときは、管理者がシステム設定 › モデル の既定のテキストモデルの"
                "「出力の上限（tokens）」と、回答前の確認の出力の上限"
                "（NL2SQL_REQUIREMENT_MAX_OUTPUT_TOKENS）・モデルの推論の量を見直してください。"
            )
        else:
            message = f"{_SUBJECT}の{llm}の応答が途中で終わりました。"
    elif kind == "response_format":
        message = f"{_SUBJECT}の{llm}の応答が、指定の JSON の形式ではありませんでした。"
    elif kind == "refusal":
        message = f"{_SUBJECT}の{llm}が応答を拒否しました。"
    else:
        message = f"{_SUBJECT}の{llm}の呼び出しに失敗しました。"
    return f"{message}{hint}", details


def requirement_invalid_output_details(
    *, model_id: str, elapsed_ms: int, error_count: int
) -> dict[str, Any]:
    """LLM の応答が要件の形（Pydantic の検査）を満たさないときの「詳細」。"""

    details: dict[str, Any] = {
        "kind": "response_format",
        "kind_label_ja": ENTERPRISE_AI_FAILURE_KIND_LABELS_JA["response_format"],
        "error_code": ENTERPRISE_AI_CALL_FAILURE_ERROR_CODES["response_format"],
        "model_id": model_id,
        "elapsed_ms": elapsed_ms,
        "validation_error_count": error_count,
    }
    return {key: value for key, value in details.items() if value not in (None, "")}


__all__ = ["requirement_failure_message", "requirement_invalid_output_details"]
