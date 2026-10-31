"""判定の校正: LLM による判定を人手の判定（正解のラベル）と照らす（#3418・#2577）。

RAG の検索の評価器の校正（`rag_engine.evaluation.crag_grader_eval`。検索の候補の関連・根拠の足りる
かのラベル）とは対象が違うので分ける。ここは回答の品質の観点（rubric の観点）ごとのラベルを使い、
一致の集計と校正の合否の閾値は platform の `pr_evaluation_scoring.agreement` を共有する（RAG の回答
の評価器の校正も同じ形・同じ閾値を使う）。

人手の判定の形（`LabelSet`。JSON。`suite-evaluation label-import` が記入用のブックから書く）:

    {"rubric_id": "answer_quality",
     "items": [{"id": "c01", "split": "holdout",
                "question": "…", "answer": "…", "standard_answer": "…",
                "evidence": [{"id": "e1", "text": "…"}],
                "labels": {"requirement_coverage": "pass", "answer_correctness": "fail",
                           "claim_support": "pass"},
                "labeler": "reviewer-1", "note": "…"}]}

- `split`: dev は rubric の調整に使ってよい項目、holdout は調整に使わず合否の判定だけに使う項目。
- `labels` は観点ごとの pass / fail（その観点を人が判定できない項目は書かない）。
- `origin`: human は人手の判定、synthetic は故意に壊した回答（`perturbation`）にコードが付けた
  fail。synthetic は holdout の合否に入れず、`synthetic` の区分として別に集計する（参考）。
- `annotator_labels`: 記入者ごとの元の判定（裁定の前。cannot_judge を含む）。

校正の合否（`CalibrationThresholds`）は holdout の人手の項目で、観点ごとに件数・判定率・Cohen の κ・
誤合格の率を閾値と比べる。合格した観点だけ、同じ rubric（sha256）・同じモデルの判定を合否に使える。
同じ項目を複数回判定したとき（`--repeats`）は、判定の分散（観点ごとに回の間でラベルが変わった項目
の率）も記録する（合否には最初の回を使う）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

from pr_evaluation_scoring import (
    Agreement,
    CalibrationThresholds,
    LabelPair,
    agreement,
    calibration_failures,
    label_variance,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from pr_suite_evaluation.dataset import canonical_sha256
from pr_suite_evaluation.judge.client import JudgeClient
from pr_suite_evaluation.judge.judging import (
    JudgeInput,
    Judgement,
    JudgeSettings,
    judge_item,
    prompt_sha256,
)
from pr_suite_evaluation.judge.rubric import FAIL, PASS, Rubric

__all__ = [
    "CalibrationThresholds",
    "CalibrationReport",
    "LabelSet",
    "LabeledItem",
    "calibrate",
    "judge_labels",
    "judge_variance",
    "load_labels",
]

CALIBRATION_SCHEMA_VERSION = 1
HUMAN_LABELS = (PASS, FAIL)
SPLITS = ("dev", "holdout")
# 合成の対照（故意に壊した回答）の集計の区分（dev / holdout と別に、合否に入れない）。
SYNTHETIC_SPLIT = "synthetic"

AnnotatorLabel = Literal["pass", "fail", "cannot_judge"]


class LabeledEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=200)
    text: str


class PerturbationInfo(BaseModel):
    """合成の項目の元の項目と壊し方。"""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(..., min_length=1, max_length=80)
    base_item_id: str = Field(..., min_length=1, max_length=200)
    description: str = ""


class LabeledItem(BaseModel):
    """人手の判定の 1 件。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=200)
    split: Literal["dev", "holdout"]
    question: str = Field(..., min_length=1)
    answer: str
    standard_answer: str | None = None
    evidence: list[LabeledEvidence] = Field(default_factory=list)
    labels: dict[str, Literal["pass", "fail"]] = Field(..., min_length=1)
    labeler: str | None = None
    note: str | None = None
    origin: Literal["human", "synthetic"] = "human"
    perturbation: PerturbationInfo | None = None
    # 記入者が付けた誤りの型・対照（#2577 の型。件数の目標を確かめるため）。
    error_types: list[str] = Field(default_factory=list)
    annotator_labels: dict[str, dict[str, AnnotatorLabel]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _origin(self) -> Self:
        if (self.origin == "synthetic") != (self.perturbation is not None):
            raise ValueError(f"合成の項目には perturbation を、人手の項目には付けない: {self.id}")
        return self

    def judge_input(self) -> JudgeInput:
        return JudgeInput(
            item_id=self.id,
            question=self.question,
            answer=self.answer,
            standard_answer=self.standard_answer,
            evidence=tuple((item.id, item.text) for item in self.evidence),
        )


class LabelSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rubric_id: str
    description: str | None = None
    # 記入用のブック・記入者・裁定・記入者の間の一致（label-import が書く）。
    provenance: dict[str, Any] = Field(default_factory=dict)
    items: list[LabeledItem] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        ids = [item.id for item in self.items]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            raise ValueError(f"項目の id が重複しています: {', '.join(duplicates)}")
        return self

    def check_rubric(self, rubric: Rubric) -> None:
        if self.rubric_id != rubric.id:
            raise ValueError(
                f"人手の判定の rubric（{self.rubric_id}）と rubric（{rubric.id}）が違います。"
            )
        unknown = sorted(
            {key for item in self.items for key in item.labels} - set(rubric.aspect_ids)
        )
        if unknown:
            raise ValueError(f"rubric に無い観点のラベルです: {', '.join(unknown)}")

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


def load_labels(path: Path) -> LabelSet:
    return LabelSet.model_validate(json.loads(path.read_text(encoding="utf-8")))


class AgreementSummary(BaseModel):
    total: int
    judged: int
    unjudged: int
    coverage: float | None
    accuracy: float | None
    kappa: float | None
    false_pass_rate: float | None
    false_fail_rate: float | None
    confusion: dict[str, dict[str, int]]
    # 人手と判定が違った項目（ラベルを見直すときに読む）。
    disagreements: list[str] = Field(default_factory=list)


class AspectCalibration(BaseModel):
    # dev・holdout（人手）と synthetic（合成の対照。合否に入れない）の集計。
    splits: dict[str, AgreementSummary] = Field(default_factory=dict)
    passed: bool
    failures: list[str] = Field(default_factory=list)


class JudgeVariance(BaseModel):
    """同じ項目を複数回判定したときの、観点ごとの回の間の揺れ。"""

    runs: int
    items: int
    unstable: int
    unstable_rate: float | None
    unstable_items: list[str] = Field(default_factory=list)


class CalibrationReport(BaseModel):
    """校正の結果（判定の結果も持ち、人手の判定を直したら `--judgements` で集計だけやり直せる）。"""

    schema_version: int = CALIBRATION_SCHEMA_VERSION
    created_at: str
    provenance: dict[str, Any] = Field(default_factory=dict)
    judgements: list[Judgement] = Field(default_factory=list)
    # 2 回目以降の判定（`--repeats`。判定の分散を測る。合否には最初の回を使う）。
    repeat_judgements: list[list[Judgement]] = Field(default_factory=list)
    variance: dict[str, JudgeVariance] = Field(default_factory=dict)
    aspects: dict[str, AspectCalibration] = Field(default_factory=dict)
    passed: bool


def _summary(pairs: Sequence[LabelPair]) -> tuple[AgreementSummary, Agreement]:
    result = agreement(pairs, HUMAN_LABELS, positive=PASS, negative=FAIL)
    summary = AgreementSummary(
        total=result.total,
        judged=result.judged,
        unjudged=result.unjudged,
        coverage=result.coverage,
        accuracy=result.accuracy,
        kappa=result.kappa,
        false_pass_rate=result.false_pass_rate,
        false_fail_rate=result.false_fail_rate,
        confusion=result.confusion,
        disagreements=[item.item_id for item in pairs if item.judge != item.human],
    )
    return summary, result


def _bucket(item: LabeledItem) -> str:
    return SYNTHETIC_SPLIT if item.origin == "synthetic" else item.split


def _label_of(judgement: Judgement | None, aspect_id: str) -> str | None:
    result = judgement.aspects.get(aspect_id) if judgement is not None else None
    return result.label if result is not None and result.status == "scored" else None


def judge_variance(runs: Sequence[Sequence[Judgement]], rubric: Rubric) -> dict[str, JudgeVariance]:
    """観点ごとに、回の間で判定（ラベルか評価不能）が変わった項目を数える（2 回以上のとき）。"""
    if len(runs) < 2:
        return {}
    variance: dict[str, JudgeVariance] = {}
    for aspect_id in rubric.aspect_ids:
        outcomes: list[dict[str, str]] = []
        for run in runs:
            by_item: dict[str, str] = {}
            for judgement in run:
                result = judgement.aspects.get(aspect_id)
                if result is None or result.status == "not_applicable":
                    continue
                by_item[judgement.item_id] = (
                    result.label if result.status == "scored" and result.label else "-"
                )
            outcomes.append(by_item)
        measured = label_variance(outcomes)
        variance[aspect_id] = JudgeVariance(
            runs=measured.runs,
            items=measured.items,
            unstable=measured.unstable,
            unstable_rate=measured.unstable_rate,
            unstable_items=measured.unstable_items,
        )
    return variance


def calibrate(
    labels: LabelSet,
    rubric: Rubric,
    *,
    judgements: Sequence[Judgement],
    judge_provenance: Mapping[str, Any],
    settings: JudgeSettings,
    thresholds: CalibrationThresholds,
    repeat_judgements: Sequence[Sequence[Judgement]] = (),
) -> CalibrationReport:
    """判定の結果と人手の判定の一致を観点ごと・区分ごとに集計し、holdout で合否を決める。"""
    labels.check_rubric(rubric)
    by_id = {item.item_id: item for item in judgements}
    missing = [item.id for item in labels.items if item.id not in by_id]
    if missing:
        raise ValueError(f"判定の無い項目があります: {', '.join(missing[:10])}")
    aspects: dict[str, AspectCalibration] = {}
    for aspect_id in rubric.aspect_ids:
        splits: dict[str, AgreementSummary] = {}
        holdout: Agreement | None = None
        for split in (*SPLITS, SYNTHETIC_SPLIT):
            pairs = [
                LabelPair(
                    item_id=item.id,
                    human=human,
                    judge=_label_of(by_id[item.id], aspect_id),
                )
                for item in labels.items
                if _bucket(item) == split and (human := item.labels.get(aspect_id)) is not None
            ]
            if pairs:
                splits[split], result = _summary(pairs)
                if split == "holdout":
                    holdout = result
        failures = calibration_failures(holdout, thresholds)
        aspects[aspect_id] = AspectCalibration(
            splits=splits, passed=not failures, failures=failures
        )
    origins = {
        origin: sum(1 for item in labels.items if item.origin == origin)
        for origin in ("human", "synthetic")
    }
    return CalibrationReport(
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        provenance={
            "rubric": rubric.provenance(),
            "prompt_sha256": prompt_sha256(rubric),
            "labels": {
                "sha256": labels.sha256,
                "item_count": len(labels.items),
                "origins": origins,
            },
            "judge": dict(judge_provenance),
            "settings": settings.provenance(),
            "thresholds": thresholds.provenance(),
            "repeats": 1 + len(repeat_judgements),
        },
        judgements=list(judgements),
        repeat_judgements=[list(run) for run in repeat_judgements],
        variance=judge_variance([list(judgements), *repeat_judgements], rubric),
        aspects=aspects,
        passed=all(item.passed for item in aspects.values()),
    )


def judge_labels(
    labels: LabelSet,
    rubric: Rubric,
    client: JudgeClient,
    settings: JudgeSettings,
) -> list[Judgement]:
    """人手の判定の項目を判定する（モデルを呼ぶ）。"""
    labels.check_rubric(rubric)
    return [judge_item(item.judge_input(), rubric, client, settings) for item in labels.items]
