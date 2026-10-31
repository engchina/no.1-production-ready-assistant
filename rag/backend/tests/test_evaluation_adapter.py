"""Evaluation アダプター(評価の基準 = 閾値のプリセット。#591)のテスト。"""

from app.config import Settings
from app.rag.evaluation_adapter import (
    EVALUATION_SUITE_ORDER,
    evaluation_adapter_runtime_settings,
    normalize_evaluation_suite,
    resolve_evaluation_adapter,
    resolve_evaluation_suite,
)
from app.schemas.evaluation import EVALUATION_METRIC_NAMES


def test_default_suite_is_standard_with_thresholds() -> None:
    """既定は標準。閾値を持ち、評価の結果を合格 / 要改善で判定する。"""
    params = resolve_evaluation_adapter(Settings())
    assert params.suite == "standard"
    assert params.thresholds.required_evidence_recall == 0.9
    assert params.thresholds.response_decision_accuracy == 0.9


def test_presets_cover_every_metric_and_strict_is_stricter() -> None:
    """2 つの基準は 9 つの指標すべてに閾値を持ち、厳格は標準以上。"""
    standard = resolve_evaluation_suite("standard").model_dump()
    strict = resolve_evaluation_suite("strict").model_dump()
    assert set(standard) == set(EVALUATION_METRIC_NAMES)
    for metric in EVALUATION_METRIC_NAMES:
        assert standard[metric] is not None
        assert strict[metric] is not None
        assert strict[metric] >= standard[metric]


def test_runtime_settings_orders_and_marks_selected() -> None:
    runtime = evaluation_adapter_runtime_settings(Settings(rag_evaluation_suite="strict"))
    assert tuple(status.name for status in runtime.suites) == EVALUATION_SUITE_ORDER
    assert EVALUATION_SUITE_ORDER == ("standard", "strict")
    selected = [status.name for status in runtime.suites if status.selected]
    assert selected == ["strict"]


def test_normalize_evaluation_suite_maps_legacy_names() -> None:
    """削除した基準は後継へ、未知の名前は既定(標準)へ寄せる。"""
    assert normalize_evaluation_suite("nope") == "standard"
    assert normalize_evaluation_suite("strict_ci") == "strict"
    assert normalize_evaluation_suite("request_only") == "standard"
    assert normalize_evaluation_suite("ragas_like") == "standard"
    assert normalize_evaluation_suite("STRICT") == "strict"


def test_settings_accept_legacy_suite_value_for_startup() -> None:
    """保存済みの .env の旧値でも起動でき、後継の基準で動く。"""
    assert Settings(rag_evaluation_suite="strict_ci").rag_evaluation_suite == "strict"
    assert Settings(rag_evaluation_suite="balanced").rag_evaluation_suite == "standard"
