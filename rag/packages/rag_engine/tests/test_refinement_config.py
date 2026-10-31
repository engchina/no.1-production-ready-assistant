"""根拠選別の既定と不正値の回復を確認する。"""

import pytest

from rag_engine.config import get_settings


@pytest.mark.parametrize("value", [None, "", "unknown", "legacy", "cited_only", "cited_required"])
def test_refinement_mode_preserves_default_or_explicit_mode(value: str | None) -> None:
    env = {} if value is None else {"RAG_ENGINE_ANSWER_EVIDENCE_REFINEMENT_MODE": value}
    settings = get_settings(environ=env, dotenv_path=None)
    assert settings.answer_evidence_refinement_mode == (value if value in {"legacy", "cited_only", "cited_required"} else "legacy")
    assert settings.answer_audit_mode == "detailed"
