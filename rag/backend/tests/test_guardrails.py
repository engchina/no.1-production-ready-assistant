"""安全チェックの段は外部の検査を持たず、質問と回答をそのまま通す（#11）。"""

import pytest

from app.config import Settings
from app.rag.guardrails import GuardrailPolicy


@pytest.mark.parametrize("method", ["validate_query", "validate_answer"])
@pytest.mark.parametrize("text", ["合成の質問 synthetic@example.com", ""])
def test_policy_passes_text_through_without_findings(method: str, text: str) -> None:
    result = getattr(GuardrailPolicy(Settings()), method)(text)
    assert result.allowed
    assert result.sanitized_text == text
    assert result.findings == []
    assert result.warnings == []
    assert not result.backend_degraded
    assert result.skipped_reason == "disabled"


def test_policy_ignores_saved_guardrail_settings() -> None:
    # 以前の RAG_GUARDRAILS_* は Settings の項目に無く、読み捨てる（extra=ignore）。
    settings = Settings.model_validate(
        {"rag_guardrails_input_enabled": True, "rag_guardrails_output_enabled": True}
    )
    assert not hasattr(settings, "rag_guardrails_input_enabled")
    assert GuardrailPolicy(settings).validate_query("合成の質問").allowed
