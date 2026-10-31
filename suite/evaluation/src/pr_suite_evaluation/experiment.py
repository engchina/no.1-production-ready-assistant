"""Experiment: 評価セットを Target に流して観測を保存し、保存した観測を採点する（#3240）。

結果のファイル（`ExperimentRecord`）は、評価セット・対象構成・採点の版と hash（provenance）、
ケースごとの観測（公開の契約の出力そのもの）、採点の結果（report）と Target の診断を持つ。
`rescore` は保存した観測だけから採点し直す（モデル・製品を呼ばない）。

結果のファイルは回答の本文と根拠の本文を含む。repo に commit せず、手元（`./tmp/` など）に置く。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import metadata
from typing import Any, Protocol

from pydantic import BaseModel, Field

from pr_suite_evaluation.dataset import Case, LoadedDataset, canonical_sha256
from pr_suite_evaluation.evaluators import EVALUATORS_VERSION, CaseScore, score_case
from pr_suite_evaluation.judge.judging import JudgeRun
from pr_suite_evaluation.observation import CaseObservation
from pr_suite_evaluation.report import Report, build_report
from pr_suite_evaluation.targets import agent_mcp, rag_mcp
from pr_suite_evaluation.views import CaseView

RECORD_SCHEMA_VERSION = 1
PACKAGE_NAME = "production-ready-suite-evaluation"


class Target(Protocol):
    """評価の対象（製品の公開の契約を呼び、1 ケースの観測を返す）。"""

    kind: str

    def run_case(self, case: Case) -> CaseObservation: ...


@dataclass(frozen=True)
class Adapter:
    """Target の種類ごとの、観測 → 採点の入力の変換と Target の診断（同じ Evaluator にかける）。"""

    rules_version: str
    view: Callable[..., CaseView]
    summary: Callable[..., dict[str, Any]]


ADAPTERS: dict[str, Adapter] = {
    agent_mcp.TARGET_KIND: Adapter(
        rules_version=agent_mcp.RULES_VERSION,
        view=agent_mcp.agent_case_view,
        summary=agent_mcp.target_summary,
    ),
    rag_mcp.TARGET_KIND: Adapter(
        rules_version=rag_mcp.RULES_VERSION,
        view=rag_mcp.rag_case_view,
        summary=rag_mcp.target_summary,
    ),
}


@dataclass(frozen=True)
class ScorerConfig:
    """採点の設定（版と hash を結果に記録する）。"""

    target_kind: str = agent_mcp.TARGET_KIND
    # outcome の無い完了の Run を、回答の文から推定する（旧形式の互換。既定は評価不能）。
    infer_missing_outcome: bool = False
    run_timeout_seconds: float = agent_mcp.DEFAULT_RUN_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.target_kind not in ADAPTERS:
            raise ValueError(f"採点できない Target の種類です: {self.target_kind}")

    @property
    def metrics_version(self) -> str:
        version = f"{EVALUATORS_VERSION}+{ADAPTERS[self.target_kind].rules_version}"
        if self.infer_missing_outcome and self.target_kind == agent_mcp.TARGET_KIND:
            version = f"{version}+{agent_mcp.LEGACY_OUTCOME_RULES}"
        return version

    def view(self, observation: CaseObservation) -> CaseView:
        return ADAPTERS[self.target_kind].view(
            observation,
            infer_missing_outcome=self.infer_missing_outcome,
            run_timeout_seconds=self.run_timeout_seconds,
        )

    def provenance(self, thresholds: Mapping[str, float]) -> dict[str, Any]:
        config = {
            "target_kind": self.target_kind,
            "infer_missing_outcome": self.infer_missing_outcome,
            "thresholds": dict(sorted(thresholds.items())),
        }
        return {
            "metrics_version": self.metrics_version,
            "package_version": _package_version(),
            "config": config,
            "config_sha256": canonical_sha256(config),
        }


class ExperimentRecord(BaseModel):
    """評価の結果のファイル。"""

    schema_version: int = RECORD_SCHEMA_VERSION
    created_at: str
    provenance: dict[str, Any] = Field(default_factory=dict)
    observations: list[CaseObservation] = Field(default_factory=list)
    report: Report
    target: dict[str, Any] = Field(default_factory=dict)
    # LLM による判定（決定的な採点 report と分けて置く。`suite-evaluation judge` が足す。#3418）。
    judge: JudgeRun | None = None


def _package_version() -> str:
    try:
        return metadata.version(PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return "unknown"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def final_status(observation: CaseObservation, score: CaseScore) -> str:
    """ケースの最後の往復の状態（分母の内訳）。

    承認の要るツールを飛ばした完了は approval_skipped にする。
    """
    final = observation.final
    if final is None:
        return "target_error"
    if score.error_type == "AgentRunApprovalSkipped":
        return agent_mcp.APPROVAL_SKIPPED
    return final.status


def score_observations(
    dataset: LoadedDataset, observations: Sequence[CaseObservation], scorer: ScorerConfig
) -> tuple[Report, dict[str, Any]]:
    """保存した観測を採点する（観測の無いケースは採点しない）。"""
    by_id = {case.id: case for case in dataset.dataset.cases}
    unknown = [item.case_id for item in observations if item.case_id not in by_id]
    if unknown:
        raise ValueError(f"評価セットに無いケースの観測です: {', '.join(unknown[:10])}")
    scores: list[CaseScore] = []
    statuses: dict[str, int] = {}
    for observation in observations:
        if observation.target_kind != scorer.target_kind:
            raise ValueError(f"採点できない Target の観測です: {observation.target_kind}")
        score = score_case(by_id[observation.case_id], scorer.view(observation))
        scores.append(score)
        status = final_status(observation, score)
        statuses[status] = statuses.get(status, 0) + 1
    report = build_report(
        scores,
        metrics_version=scorer.metrics_version,
        thresholds=dataset.dataset.thresholds,
        final_statuses=statuses,
    )
    cases = [by_id[item.case_id] for item in observations]
    target = ADAPTERS[scorer.target_kind].summary(
        cases, observations, infer_missing_outcome=scorer.infer_missing_outcome
    )
    return report, target


def run_experiment(
    dataset: LoadedDataset,
    target: Target,
    *,
    target_config: Mapping[str, Any],
    scorer: ScorerConfig,
    log: Callable[[str], None] = print,
) -> ExperimentRecord:
    """評価セットのケースを順に Target に流し、観測を保存して採点する。"""
    if target.kind != scorer.target_kind:
        raise ValueError(
            f"採点の設定（{scorer.target_kind}）と Target（{target.kind}）が違います。"
        )
    observations: list[CaseObservation] = []
    total = len(dataset.dataset.cases)
    for index, case in enumerate(dataset.dataset.cases, start=1):
        observation = target.run_case(case)
        observations.append(observation)
        final = observation.final
        log(
            f"[{index}/{total}] {case.id}: "
            f"{final.status if final else 'no-turn'} turns={len(observation.turns)}"
        )
    report, summary = score_observations(dataset, observations, scorer)
    return ExperimentRecord(
        created_at=_now(),
        provenance={
            "dataset": dataset.provenance(),
            "target": {**target_config, "config_sha256": canonical_sha256(dict(target_config))},
            "scorer": scorer.provenance(dataset.dataset.thresholds),
        },
        observations=observations,
        report=report,
        target=summary,
    )


def rescore(
    record: ExperimentRecord, dataset: LoadedDataset, scorer: ScorerConfig
) -> ExperimentRecord:
    """保存した観測だけから採点し直す（Target を呼ばない）。

    観測を取ったときの評価セットの hash は `observed_dataset` に残し、採点に使った評価セットを
    `dataset` にする（期待を直した評価セットで採点し直せる。hash が違えば両方が残る）。
    """
    report, summary = score_observations(dataset, record.observations, scorer)
    provenance = dict(record.provenance)
    observed = provenance.get("observed_dataset") or provenance.get("dataset")
    provenance["observed_dataset"] = observed
    provenance["dataset"] = dataset.provenance()
    provenance["scorer"] = scorer.provenance(dataset.dataset.thresholds)
    provenance["rescored_at"] = _now()
    return record.model_copy(update={"provenance": provenance, "report": report, "target": summary})
