"""Evaluation アダプター(評価の基準 = 閾値のプリセットの手動選択。#591)。

基準→CI gate 用閾値の解決は共有パッケージ ``rag_pipeline_core.evaluation`` を単一ソースに
in-process で行う(決定論の name→閾値 lookup)。表示も実 gate も同一の in-process 経路を使う。
閾値 dict は backend で `EvaluationThresholds` へ写す。基準は standard(標準。既定)と
strict(厳格)の 2 つ。外部評価 SaaS は導入しない。
"""

from __future__ import annotations

from dataclasses import dataclass

from rag_pipeline_core.evaluation import (
    EVALUATION_SPECS,
    EVALUATION_SUITES,
    resolve_evaluation,
)
from rag_pipeline_core.evaluation import (
    normalize_evaluation_suite as _core_normalize,
)

from app.config import EvaluationSuite, Settings
from app.schemas.evaluation import EvaluationThresholds

EvaluationSuiteName = EvaluationSuite
DEFAULT_EVALUATION_SUITE: EvaluationSuiteName = "standard"
EVALUATION_SUITE_ORDER: tuple[EvaluationSuiteName, ...] = EVALUATION_SUITES  # type: ignore[assignment]


@dataclass(frozen=True)
class EvaluationAdapterParams:
    """評価へ渡す解決済みパラメータ。"""

    suite: EvaluationSuiteName
    thresholds: EvaluationThresholds


@dataclass(frozen=True)
class EvaluationSuiteStatus:
    """1 評価スイートの選択状態と閾値。"""

    name: EvaluationSuiteName
    origin: str
    recommended_for: tuple[str, ...]
    selected: bool
    thresholds: EvaluationThresholds


@dataclass(frozen=True)
class EvaluationAdapterRuntimeSettings:
    """Evaluation アダプターの非機密 runtime snapshot。"""

    suite: EvaluationSuiteName
    thresholds: EvaluationThresholds
    suites: tuple[EvaluationSuiteStatus, ...]


def normalize_evaluation_suite(value: object) -> EvaluationSuiteName:
    """旧プリセットは後継へ、未知の名前は既定(標準)へ寄せる。"""
    return _core_normalize(value)  # type: ignore[return-value]


def _thresholds_from_dict(thresholds: dict[str, float]) -> EvaluationThresholds:
    return EvaluationThresholds(**thresholds)


def resolve_evaluation_suite(value: object) -> EvaluationThresholds:
    """基準の名前から CI gate 用閾値を解決する。in-process(name→閾値)。"""
    return _thresholds_from_dict(resolve_evaluation(value).thresholds)


def resolve_evaluation_adapter(settings: Settings) -> EvaluationAdapterParams:
    """Settings から Evaluation アダプターの解決済みパラメータを作る(in-process)。"""
    suite = normalize_evaluation_suite(
        getattr(settings, "rag_evaluation_suite", DEFAULT_EVALUATION_SUITE)
    )
    return EvaluationAdapterParams(
        suite=suite,
        thresholds=_thresholds_from_dict(resolve_evaluation(suite).thresholds),
    )


def evaluation_adapter_runtime_settings(settings: Settings) -> EvaluationAdapterRuntimeSettings:
    """Settings から Evaluation アダプター readiness snapshot を作る。"""
    params = resolve_evaluation_adapter(settings)
    statuses = tuple(
        EvaluationSuiteStatus(
            name=spec.name,  # type: ignore[arg-type]
            origin=spec.origin,
            recommended_for=spec.recommended_for,
            selected=spec.name == params.suite,
            thresholds=_thresholds_from_dict(spec.thresholds),
        )
        for spec in (EVALUATION_SPECS[name] for name in EVALUATION_SUITES)
    )
    return EvaluationAdapterRuntimeSettings(
        suite=params.suite,
        thresholds=params.thresholds,
        suites=statuses,
    )
