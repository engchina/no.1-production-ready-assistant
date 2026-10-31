"""品質評価の判定の校正（人手の判定の記入・一致・裁定・校正）の API の型（#3578）。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.evaluation import EvaluationJob

CalibrationAspect = Literal["requirement_coverage", "answer_correctness", "claim_support"]
HumanLabel = Literal["pass", "fail", "cannot_judge"]
RowStatus = Literal["pending", "agreed", "disagreed", "adjudicated"]
LABEL_SET_NAME_MAX_CHARS = 200
LABEL_NOTE_MAX_CHARS = 2000
LABEL_SET_MAX_JOBS = 20
CALIBRATION_MAX_REPEATS = 3


class CalibrationSourceJob(BaseModel):
    """記入の集合を作れる、終わった品質評価の job。"""

    job_id: str
    created_at: datetime
    finished_at: datetime | None = None
    case_count: int = 0
    compared: bool = False


class CalibrationSourcesData(BaseModel):
    jobs: list[CalibrationSourceJob] = Field(default_factory=list)


class LabelSetCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=LABEL_SET_NAME_MAX_CHARS)
    job_ids: list[str] = Field(..., min_length=1, max_length=LABEL_SET_MAX_JOBS)
    include_synthetic: bool = False
    holdout_ratio: float = Field(default=0.7, gt=0, le=1)


class AspectCriterion(BaseModel):
    """観点と、記入者が読む基準の文（rubric の観点の行）。"""

    aspect: CalibrationAspect
    metric: str
    criteria: str
    requires_standard_answer: bool


class ErrorTypeInfo(BaseModel):
    id: str
    group: str
    title: str
    aspects: str
    holdout_target: int


class AspectProgress(BaseModel):
    """観点ごとの記入の進み具合（holdout の pass / fail の数と目標）。"""

    aspect: CalibrationAspect
    rows: int
    labeled: int
    cannot_judge: int
    holdout_rows: int
    holdout_pass: int
    holdout_fail: int


class ErrorTypeProgress(BaseModel):
    id: str
    holdout_count: int


class LabelProgress(BaseModel):
    aspects: list[AspectProgress] = Field(default_factory=list)
    error_types: list[ErrorTypeProgress] = Field(default_factory=list)
    holdout_target: int
    label_target: int


class CalibrationSplitResult(BaseModel):
    split: Literal["dev", "holdout", "synthetic"]
    total: int
    judged: int
    coverage: float | None = None
    accuracy: float | None = None
    kappa: float | None = None
    false_pass_rate: float | None = None
    false_fail_rate: float | None = None


class CalibrationVariance(BaseModel):
    runs: int
    items: int
    unstable: int
    unstable_rate: float | None = None


class CalibrationAspectResult(BaseModel):
    aspect: CalibrationAspect
    passed: bool
    failures: list[str] = Field(default_factory=list)
    splits: list[CalibrationSplitResult] = Field(default_factory=list)
    variance: CalibrationVariance | None = None


class CalibrationSummary(BaseModel):
    """校正の実行と結果（観点 × 区分の一致と合否、判定のモデル・rubric の版・指示の sha256）。"""

    calibration_id: str
    label_set_id: str
    status: Literal["RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]
    created_at: datetime
    finished_at: datetime | None = None
    repeats: int = 1
    judge_model_id: str | None = None
    judge_rubric_version: str | None = None
    prompt_sha256: str | None = None
    labels_sha256: str | None = None
    passed: bool | None = None
    error_message: str | None = None
    aspects: list[CalibrationAspectResult] = Field(default_factory=list)
    # 今の判定のモデル・指示と同じか（違えば「校正済み」の表示に使わない）。
    current: bool = False


class LabelSetSummary(BaseModel):
    label_set_id: str
    name: str
    origin: Literal["jobs", "import"]
    created_at: datetime
    human_item_count: int
    synthetic_item_count: int
    row_count: int
    annotator_count: int
    my_labeled: int
    resolved_rows: int
    created_by_me: bool = False
    latest_calibration: CalibrationSummary | None = None


class LabelSetListData(BaseModel):
    items: list[LabelSetSummary] = Field(default_factory=list)
    can_manage: bool = False


class LabelSetDetail(BaseModel):
    summary: LabelSetSummary
    rubric_id: str
    rubric_version: str | None = None
    aspects: list[AspectCriterion] = Field(default_factory=list)
    error_types: list[ErrorTypeInfo] = Field(default_factory=list)
    my_code: str | None = None
    my_progress: LabelProgress
    # 最終の判定（裁定・全員一致）の進み具合。判定の校正の管理の権限のときだけ。
    final_progress: LabelProgress | None = None
    can_manage: bool = False
    skipped: dict[str, int] = Field(default_factory=dict)
    holdout_ratio: float | None = None


class LabelEvidence(BaseModel):
    id: str
    text: str


class AnnotationRow(BaseModel):
    """記入の 1 行（項目 × 観点）。自分の判定だけを持つ（他の記入者・評価器の判定は持たない）。"""

    item_id: str
    aspect: CalibrationAspect
    split: Literal["dev", "holdout"]
    question: str
    answer: str
    standard_answer: str | None = None
    evidence: list[LabelEvidence] = Field(default_factory=list)
    label: HumanLabel | None = None
    error_type: str | None = None
    note: str | None = None


class AnnotationRowsData(BaseModel):
    items: list[AnnotationRow] = Field(default_factory=list)
    total: int
    offset: int
    limit: int
    unlabeled: int


class LabelInput(BaseModel):
    """判定の保存（``label`` が None なら消す）。"""

    item_id: str = Field(..., min_length=1, max_length=200)
    aspect: CalibrationAspect
    label: HumanLabel | None = None
    error_type: str | None = Field(default=None, max_length=40)
    note: str | None = Field(default=None, max_length=LABEL_NOTE_MAX_CHARS)


class AnnotatorLabel(BaseModel):
    code: str
    label: HumanLabel
    error_type: str | None = None
    note: str | None = None


class AgreementPair(BaseModel):
    annotators: list[str]
    compared: int
    agreement: float | None = None
    kappa: float | None = None


class AspectAgreement(BaseModel):
    aspect: CalibrationAspect
    rows: int
    agreed: int
    disagreed: int
    adjudicated: int
    pending: int
    pairs: list[AgreementPair] = Field(default_factory=list)


class AgreementRow(BaseModel):
    item_id: str
    aspect: CalibrationAspect
    split: Literal["dev", "holdout"]
    status: RowStatus
    question: str
    answer: str
    standard_answer: str | None = None
    evidence: list[LabelEvidence] = Field(default_factory=list)
    annotations: list[AnnotatorLabel] = Field(default_factory=list)
    final_label: HumanLabel | None = None
    adjudication: AnnotatorLabel | None = None


class AgreementData(BaseModel):
    annotators: list[str] = Field(default_factory=list)
    aspects: list[AspectAgreement] = Field(default_factory=list)
    rows: list[AgreementRow] = Field(default_factory=list)
    total: int = 0
    offset: int = 0
    limit: int = 25


class SyntheticItem(BaseModel):
    item_id: str
    base_item_id: str
    kind: str
    description: str
    fail_aspects: list[str] = Field(default_factory=list)
    answer: str
    excluded: bool = False


class SyntheticItemsData(BaseModel):
    items: list[SyntheticItem] = Field(default_factory=list)


class SyntheticExcludeRequest(BaseModel):
    excluded: bool


class CalibrationRequest(BaseModel):
    repeats: int = Field(default=1, ge=1, le=CALIBRATION_MAX_REPEATS)


class CalibrationStartData(BaseModel):
    job: EvaluationJob
    calibration: CalibrationSummary


class CalibrationListData(BaseModel):
    items: list[CalibrationSummary] = Field(default_factory=list)


class AspectCalibrationStatus(BaseModel):
    """品質評価の結果に出す、観点の判定の校正の状態。"""

    aspect: CalibrationAspect
    metric: str
    calibrated: bool
    kappa: float | None = None
    calibration_id: str | None = None
    calibrated_at: datetime | None = None
    judge_rubric_version: str | None = None


class JudgeCalibrationStatus(BaseModel):
    judge_model_id: str | None = None
    prompt_sha256: str
    aspects: list[AspectCalibrationStatus] = Field(default_factory=list)


class LabelSetImportData(BaseModel):
    label_set: LabelSetSummary
    imported_items: int
    imported_labels: int
