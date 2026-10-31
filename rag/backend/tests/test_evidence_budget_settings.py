"""生成に渡す根拠の文字予算(#3776)の全体設定: 既定・範囲・.env.example・engine への受け渡し。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from rag_engine.config import DEFAULT_EVIDENCE_BUDGET_CHARS

from app import config as config_module
from app.config import PROFILE_ONLY_SETTING_FIELDS, Settings
from app.rag.answer_engine import build_engine_settings
from app.rag.diagnostics import rag_config_fingerprint


def test_default_matches_the_engine_default() -> None:
    """既定は rag_engine の既定と同じ(既定の挙動を変えない)。"""
    assert Settings().rag_evidence_budget_chars == DEFAULT_EVIDENCE_BUDGET_CHARS == 80000
    assert "rag_evidence_budget_chars" not in PROFILE_ONLY_SETTING_FIELDS


def test_env_example_lists_the_default() -> None:
    example = (config_module.BACKEND_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "RAG_EVIDENCE_BUDGET_CHARS=80000" in example.splitlines()


def test_env_value_is_read_and_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAG_EVIDENCE_BUDGET_CHARS", "80000")
    assert Settings().rag_evidence_budget_chars == 80000
    for value in (999, 500001):
        with pytest.raises(ValidationError):
            Settings(rag_evidence_budget_chars=value)


def test_engine_receives_the_configured_budget(tmp_path: Path) -> None:
    """製品の .env の値が engine の evidence_budget_chars になる(エンジン用のキーは不要)。"""
    assert (
        build_engine_settings(Settings(), output_dir=tmp_path).evidence_budget_chars
        == DEFAULT_EVIDENCE_BUDGET_CHARS
    )
    engine = build_engine_settings(Settings(rag_evidence_budget_chars=80000), output_dir=tmp_path)
    assert engine.evidence_budget_chars == 80000


def test_engine_does_not_read_the_engine_key_from_the_process_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """build_engine_settings は env を読まない。環境のエンジン用のキーより製品の設定を使う。"""
    monkeypatch.setenv("RAG_ENGINE_EVIDENCE_BUDGET_CHARS", "12000")
    engine = build_engine_settings(Settings(rag_evidence_budget_chars=64000), output_dir=tmp_path)
    assert engine.evidence_budget_chars == 64000


def test_budget_changes_the_config_fingerprint() -> None:
    """回答の結果に効く設定なので、設定の fingerprint に入れる。"""
    assert rag_config_fingerprint(Settings()) != rag_config_fingerprint(
        Settings(rag_evidence_budget_chars=72000)
    )
