"""有効時だけ OCI Guardrails で質問と回答を検査する。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from app.clients.oci_guardrails import GuardrailInspection, OciGuardrailsClient
from app.config import Settings, get_settings

SENSITIVE_VALUE_MASK = "[機微情報]"


class _OciGuardrailsLike(Protocol):
    def inspect_text(self, text: str) -> GuardrailInspection | None: ...


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
    """OCI の検出に基づき、PII をマスクし攻撃・不適切な内容を停止する。"""

    def __init__(
        self, settings: Settings | None = None, oci_client: _OciGuardrailsLike | None = None
    ) -> None:
        settings = settings or get_settings()
        self._input_enabled = settings.rag_guardrails_input_enabled
        self._output_enabled = settings.rag_guardrails_output_enabled
        self._oci_client = (
            (oci_client if oci_client is not None else OciGuardrailsClient(settings))
            if self._input_enabled or self._output_enabled
            else None
        )

    def validate_query(self, query: str) -> GuardrailResult:
        """有効時だけ質問を検査する。"""
        return self._inspect(query, enabled=self._input_enabled)

    def validate_answer(self, answer: str, context: str | None = None) -> GuardrailResult:
        """有効時だけ回答を検査する。根拠との語の重なりは判定しない。"""
        return self._inspect(answer, enabled=self._output_enabled)

    def _inspect(self, text: str, *, enabled: bool) -> GuardrailResult:
        if not enabled or self._oci_client is None or not text.strip():
            return GuardrailResult(
                allowed=True,
                sanitized_text=text,
                findings=[],
                skipped_reason="disabled"
                if not enabled
                else "empty_text"
                if not text.strip()
                else "client_unavailable",
            )
        try:
            inspection = self._oci_client.inspect_text(text)
        except Exception:  # noqa: BLE001 - SDK の詳細や入力を応答へ出さない
            inspection = None
        if inspection is None:
            return GuardrailResult(
                allowed=False,
                sanitized_text="安全検査を完了できないため、処理を停止しました。",
                findings=[
                    GuardrailFinding(
                        code="guardrail_backend_unavailable",
                        severity="error",
                        message="OCI Guardrails を利用できないため、処理を停止しました。",
                    )
                ],
                backend_degraded=True,
            )
        findings: list[GuardrailFinding] = []
        blocked = False
        if inspection.prompt_injection:
            blocked = True
            findings.append(
                GuardrailFinding(
                    code="oci_prompt_injection",
                    severity="error",
                    message="OCI Guardrails がプロンプト攻撃を検出しました。",
                )
            )
        if inspection.moderation_categories:
            blocked = True
            findings.append(
                GuardrailFinding(
                    code="oci_content_moderation",
                    severity="error",
                    message="OCI Guardrails が不適切な内容を検出しました。",
                )
            )
        sanitized = text
        if inspection.pii_spans:
            sanitized = _mask_spans(text, inspection.pii_spans)
            findings.append(
                GuardrailFinding(
                    code="oci_pii_detected",
                    severity="warning",
                    message="OCI Guardrails が個人情報の可能性を検出し、マスクしました。",
                )
            )
        return GuardrailResult(
            allowed=not blocked,
            sanitized_text="安全チェックにより処理を停止しました。" if blocked else sanitized,
            findings=findings,
        )


def _mask_spans(text: str, spans: tuple[object, ...]) -> str:
    """OCI PII の位置情報を後ろから適用し、原値を保持せずマスクする。"""
    masked = text
    normalized_spans: list[tuple[int, int]] = []
    for span in spans:
        offset = int(getattr(span, "offset", -1))
        length = int(getattr(span, "length", 0))
        if offset < 0 or length <= 0 or offset + length > len(text):
            continue
        normalized_spans.append((offset, offset + length))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(set(normalized_spans)):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    for start, end in reversed(merged):
        masked = f"{masked[:start]}{SENSITIVE_VALUE_MASK}{masked[end:]}"
    return masked
