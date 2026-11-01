"""逆生成の段階単位 retry。入力・応答・credential は診断ログに記録しない。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from pr_backend_core.observability.model_purpose import call_with_model_purpose

from .enterprise_ai_client import EnterpriseAiDirectClient, EnterpriseAiDirectError
from .structured_outputs import QuestionOutput, StructureOutput, response_format

logger = logging.getLogger(__name__)
# 段階 retry の間の待ち。テストは conftest で待たない関数に差し替える（#344）。
_retry_sleep: Callable[[float], None] = time.sleep
STAGE_LABELS = {
    "structure_analysis": "SQL 構造分析",
    "logical_structure": "SQL 論理構造の生成",
    "business_question": "自然言語の質問生成",
}
REASONS = {
    "timeout": "応答が制限時間内に完了しませんでした",
    "deadline": "分析・質問生成全体の制限時間に達しました",
    "connection": "Enterprise AI に接続できませんでした",
    "rate_limit": "Enterprise AI のリクエスト上限に達しました",
    "unavailable": "Enterprise AI が一時的に利用できませんでした",
    "authentication": "Enterprise AI の認証に失敗しました。API key を確認してください",
    # 403 / 404 は API key の認可の失敗（#1684。モデルが無いときは model_not_found）。
    "authorization": (
        "Enterprise AI の呼び出しが認可されませんでした。API key の権限と"
        " Project ID を確認してください"
    ),
    "model_not_found": (
        "既定のテキストモデルがこの接続にありません。モデルの設定を確認してください"
    ),
    "request": "Enterprise AI のモデル・リクエスト設定を確認してください",
    "response_format": "応答が空、または必要な形式を満たしていませんでした",
    "provider_error": "Enterprise AI が生成エラーを返しました",
    "incomplete": "応答が未完了です。出力上限とモデル設定を確認してください",
    "refusal": "Enterprise AI が応答を拒否しました。入力内容を確認してください",
}


class ReverseStageError(ValueError):
    def __init__(self, stage: str, reason: str, attempts: int) -> None:
        self.stage = stage
        self.reason = reason
        self.attempts = attempts
        super().__init__(f"{stage}: {reason} (attempts={attempts})")

    def warning(self) -> str:
        label = STAGE_LABELS[self.stage]
        message = (
            f"{label}を完了できませんでした。{REASONS[self.reason]}（試行 {self.attempts} 回）。"
        )
        if self.stage == "business_question":
            message += "SQL 論理構造は生成済みです。質問候補のみ簡易生成で表示しています。"
        else:
            message += "元 SQL を含む簡易論理構造と簡易生成した質問候補を表示しています。"
        return message + "設定・接続状態を確認し、「SQL 分析・質問生成」で再試行してください。"


def generate_stage[T](
    *,
    client: EnterpriseAiDirectClient,
    stage: str,
    prompt: str,
    context: str,
    system_prompt: str,
    parse: Callable[[str], T],
    timeout_seconds: float,
    max_retries: int,
    deadline: float,
) -> T:
    # HTTP 層の retry は停止し、この段階内だけで最大3回。全三段階で deadline を共有する。
    limit = min(max(max_retries, 0), 2) + 1
    started = time.monotonic()
    for attempt in range(1, limit + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReverseStageError(stage, "deadline", attempt - 1)
        try:
            raw = call_with_model_purpose(
                {
                    "structure_analysis": "sql.structure",
                    "logical_structure": "sql.logical_structure",
                    "business_question": "sql.reverse",
                }.get(stage, ""),
                client.generate,
                response_format=response_format(
                    QuestionOutput if stage == "business_question" else StructureOutput
                ),
                prompt=prompt,
                context=context,
                system_prompt=system_prompt,
                timeout_seconds=min(timeout_seconds, remaining),
                max_retries=0,
            )
            return parse(raw)
        except (EnterpriseAiDirectError, ValueError) as exc:
            reason = exc.code if isinstance(exc, EnterpriseAiDirectError) else "response_format"
            retryable = exc.retryable if isinstance(exc, EnterpriseAiDirectError) else True
            if reason not in REASONS:
                reason = "provider_error"
            logger.warning(
                "reverse_sql_stage_failed",
                extra={
                    "stage": stage,
                    "reason": reason,
                    "attempt": attempt,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                },
            )
            if not retryable or attempt == limit:
                raise ReverseStageError(stage, reason, attempt) from exc
            delay = min(2 ** (attempt - 1), max(0, deadline - time.monotonic()))
            _retry_sleep(delay)
            if reason == "response_format":
                system_prompt += (
                    "\n前回は応答形式を検証できませんでした。"
                    "指定の出力形式を守り、空の応答や説明の前置きを返さないでください。"
                )
    raise AssertionError("unreachable")
