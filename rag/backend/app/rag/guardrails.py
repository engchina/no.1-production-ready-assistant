"""質問と回答の安全チェックの段（外部の検査を持たない通過）。

OCI Guardrails は #11 で削除した。呼び出し側（pipeline・chat・業務ガイドの照合・会話の履歴の
整形）と応答の形（`guardrail_warnings` 等）は残し、この段は常に通す（findings なし）。
段そのものを pipeline・API・MCP の契約から外すのは別の Issue で行う。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.config import Settings


@dataclass(frozen=True)
class GuardrailFinding:
    """ガードレール検出結果。"""

    code: str
    severity: str
    message: str


@dataclass(frozen=True)
class GuardrailResult:
    """ガードレール適用後の結果。"""

    allowed: bool
    sanitized_text: str
    findings: list[GuardrailFinding]
    backend_degraded: bool = False
    skipped_reason: Literal["disabled", "empty_text", "client_unavailable"] | None = None

    @property
    def warnings(self) -> list[str]:
        """API レスポンス向けの警告メッセージ。"""
        return [finding.message for finding in self.findings]


class GuardrailPolicy:
    """外部の検査を持たない安全チェック。質問と回答をそのまま通す。"""

    def __init__(self, settings: Settings | None = None) -> None:
        # 呼び出し側の契約（設定を渡して作る）を保つ。検査の設定は持たない。
        del settings

    def validate_query(self, query: str) -> GuardrailResult:
        """質問をそのまま通す。"""
        return _passthrough(query)

    def validate_answer(self, answer: str, context: str | None = None) -> GuardrailResult:
        """回答をそのまま通す。"""
        del context
        return _passthrough(answer)


def _passthrough(text: str) -> GuardrailResult:
    return GuardrailResult(
        allowed=True, sanitized_text=text, findings=[], skipped_reason="disabled"
    )
