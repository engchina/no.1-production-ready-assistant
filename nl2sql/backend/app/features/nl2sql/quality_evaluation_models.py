"""NL2SQL SQL生成評価の公開契約と永続レコード。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

from pr_backend_core import Page
from pydantic import BaseModel, Field, field_validator

from .models import Nl2SqlEngine


class QualityEvaluationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"
    CANCELLED = "cancelled"


class QualityEvaluationVerdict(StrEnum):
    CORRECT = "correct"
    INCORRECT = "incorrect"
    UNCERTAIN = "uncertain"
    NOT_ANALYZED = "not_analyzed"


class QualityEvaluationCase(BaseModel):
    case_no: int = Field(ge=1)
    case_id: str
    excel_row: int = Field(ge=2)
    question: str
    expected_sql: str


class QualityEvaluationJudge(BaseModel):
    verdict: Literal[
        QualityEvaluationVerdict.CORRECT,
        QualityEvaluationVerdict.INCORRECT,
        QualityEvaluationVerdict.UNCERTAIN,
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    differences: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    correction_suggestion: str = ""

    @field_validator("verdict", mode="before")
    @classmethod
    def _normalize_verdict(cls, value: Any) -> Any:
        if isinstance(value, QualityEvaluationVerdict):
            return value
        normalized = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
        aliases = {
            "correct": QualityEvaluationVerdict.CORRECT,
            "incorrect": QualityEvaluationVerdict.INCORRECT,
            "uncertain": QualityEvaluationVerdict.UNCERTAIN,
        }
        return aliases.get(normalized, value)

    @field_validator("confidence", mode="before")
    @classmethod
    def _normalize_confidence(cls, value: Any) -> Any:
        try:
            if isinstance(value, str):
                text = value.strip()
                percent = text.endswith("%")
                numeric = float(text.removesuffix("%").strip())
                if percent:
                    numeric /= 100
            else:
                numeric = float(value)
        except (TypeError, ValueError):
            return value
        if numeric > 1.0:
            numeric /= 100
        return min(1.0, max(0.0, numeric))


class QualityEvaluationDeterministicAnalysis(BaseModel):
    is_safe: bool = False
    is_select_only: bool = False
    # 安全確認をしたか。Select AI / Select AI Agent の SQL は省略する（#1523）。
    safety_checked: bool = True
    referenced_objects: list[str] = Field(default_factory=list)
    structure_summary: str = ""
    risk_findings: list[str] = Field(default_factory=list)


class QualityEvaluationResult(BaseModel):
    result_id: str
    job_id: str
    case_no: int = Field(ge=1)
    case_id: str
    excel_row: int = Field(ge=2)
    question: str
    expected_sql: str
    engine: Nl2SqlEngine
    repetition_no: int = Field(ge=1, le=10)
    generated_sql: str = ""
    normalized_sql: str = ""
    deterministic_analysis: QualityEvaluationDeterministicAnalysis = Field(
        default_factory=QualityEvaluationDeterministicAnalysis
    )
    generation_elapsed_ms: int = Field(default=0, ge=0)
    judge_elapsed_ms: int = Field(default=0, ge=0)
    total_elapsed_ms: int = Field(default=0, ge=0)
    verdict: QualityEvaluationVerdict = QualityEvaluationVerdict.NOT_ANALYZED
    judge: QualityEvaluationJudge | None = None
    generation_error: str = ""
    judge_error: str = ""
    created_at: str

    @property
    def generation_succeeded(self) -> bool:
        return bool(self.generated_sql) and not self.generation_error


class QualityEvaluationEngineSummary(BaseModel):
    engine: Nl2SqlEngine
    total_attempts: int = 0
    generation_successes: int = 0
    generation_success_rate: float = 0.0
    correct: int = 0
    incorrect: int = 0
    uncertain: int = 0
    not_analyzed: int = 0
    normalized_sql_consistency: float = 0.0
    error_count: int = 0


class QualityEvaluationRunningAttempt(BaseModel):
    """実行中の 1 つの試行（ケース・engine・繰り返しの位置と開始時刻。#2059）。

    試行は上限つきの並行で実行するため、job の記録は実行中の試行を複数持つ。lease の再取得の
    ときは、時間切れになった試行をそれぞれ時間切れの結果として記録する。
    """

    case_no: int = Field(ge=1)
    case_id: str
    engine: Nl2SqlEngine
    repetition_no: int = Field(ge=1, le=10)
    started_at: str

    def key(self) -> tuple[int, str, int]:
        return (self.case_no, self.engine.value, self.repetition_no)


class QualityEvaluationRunningAttemptSummary(QualityEvaluationRunningAttempt):
    """画面に出す実行中の試行（時間切れと判断する時刻を添える）。"""

    deadline_at: str | None = None


class QualityEvaluationJobRecord(BaseModel):
    job_id: str
    profile_id: str
    profile_name: str
    profile_category: str = ""
    engines: list[Nl2SqlEngine]
    repeat_count: int = Field(ge=1, le=10)
    cases: list[QualityEvaluationCase]
    status: QualityEvaluationStatus = QualityEvaluationStatus.PENDING
    total_attempts: int = Field(ge=1)
    completed_attempts: int = Field(default=0, ge=0)
    success_count: int = Field(default=0, ge=0)
    error_count: int = Field(default=0, ge=0)
    # 実行中の試行（開始の順。#2059）と、試行を同時に実行する数の上限。
    running_attempts: list[QualityEvaluationRunningAttempt] = Field(default_factory=list)
    concurrency: int = Field(default=1, ge=1)
    engine_summaries: list[QualityEvaluationEngineSummary] = Field(default_factory=list)
    actor_user_uuid: str = ""
    input_filename: str = ""
    worker_id: str = ""
    heartbeat_at: str | None = None
    lease_expires_at: str | None = None
    attempt_no: int = 0
    attempt_timeout_seconds: float = Field(default=300.0, ge=1.0)
    error_message: str = ""
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str


class QualityEvaluationJobSummary(BaseModel):
    job_id: str
    profile_id: str
    profile_name: str
    profile_category: str = ""
    engines: list[Nl2SqlEngine]
    repeat_count: int
    case_count: int
    total_attempts: int
    completed_attempts: int
    success_count: int
    error_count: int
    status: QualityEvaluationStatus
    # 実行中の試行のうち最も前に始めたもの（時間切れに最も近い試行。#2059）。
    current_case_id: str = ""
    current_engine: Nl2SqlEngine | None = None
    current_repetition: int = 0
    current_attempt_started_at: str | None = None
    current_attempt_deadline_at: str | None = None
    # 実行中の試行（開始の順）と、試行を同時に実行する数の上限（#2059）。
    running_attempts: list[QualityEvaluationRunningAttemptSummary] = Field(default_factory=list)
    concurrency: int = 1
    engine_summaries: list[QualityEvaluationEngineSummary] = Field(default_factory=list)
    error_message: str = ""
    heartbeat_at: str | None = None
    lease_expires_at: str | None = None
    attempt_no: int = 0
    attempt_timeout_seconds: float = 300.0
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str


class QualityEvaluationJobPage(Page[QualityEvaluationJobSummary]):
    """最近の SQL生成評価 job のページ（任意のページへ移る offset 型。#1891）。"""


class QualityEvaluationResultPage(Page[QualityEvaluationResult]):
    """SQL生成評価の結果明細のページ（任意のページへ移る offset 型。#1891）。"""


class QualityEvaluationEngineCapability(BaseModel):
    engine: Nl2SqlEngine
    label: str
    available: bool
    reason: str = ""


class QualityEvaluationJudgeCapability(BaseModel):
    available: bool
    reason: str = ""
    provider: str = "OCI Enterprise AI"


class QualityEvaluationLimits(BaseModel):
    max_file_bytes: int
    max_cases: int
    max_attempts: int
    min_repeat_count: int = 1
    max_repeat_count: int = 10
    attempt_timeout_seconds: float = 300.0


class QualityEvaluationCapabilities(BaseModel):
    engines: list[QualityEvaluationEngineCapability]
    judge: QualityEvaluationJudgeCapability
    limits: QualityEvaluationLimits


def _attempt_deadline_at(job: QualityEvaluationJobRecord, started_at_text: str) -> str | None:
    """試行を時間切れと判断する時刻（開始 + 試行の timeout の 2 倍）。"""
    try:
        started_at = datetime.fromisoformat(started_at_text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    timeout_seconds = max(1.0, float(job.attempt_timeout_seconds))
    return (started_at + timedelta(seconds=2.0 * timeout_seconds)).isoformat()


def _running_attempt_summaries(
    job: QualityEvaluationJobRecord,
) -> list[QualityEvaluationRunningAttemptSummary]:
    if job.status != QualityEvaluationStatus.RUNNING:
        return []
    return [
        QualityEvaluationRunningAttemptSummary(
            **attempt.model_dump(),
            deadline_at=_attempt_deadline_at(job, attempt.started_at),
        )
        for attempt in job.running_attempts
    ]


def job_summary(job: QualityEvaluationJobRecord) -> QualityEvaluationJobSummary:
    running = _running_attempt_summaries(job)
    oldest = min(running, key=lambda attempt: attempt.started_at) if running else None
    return QualityEvaluationJobSummary(
        job_id=job.job_id,
        profile_id=job.profile_id,
        profile_name=job.profile_name,
        profile_category=job.profile_category,
        engines=job.engines,
        repeat_count=job.repeat_count,
        case_count=len(job.cases),
        total_attempts=job.total_attempts,
        completed_attempts=job.completed_attempts,
        success_count=job.success_count,
        error_count=job.error_count,
        status=job.status,
        current_case_id=oldest.case_id if oldest else "",
        current_engine=oldest.engine if oldest else None,
        current_repetition=oldest.repetition_no if oldest else 0,
        current_attempt_started_at=oldest.started_at if oldest else None,
        current_attempt_deadline_at=oldest.deadline_at if oldest else None,
        running_attempts=running,
        concurrency=job.concurrency,
        engine_summaries=job.engine_summaries,
        error_message=job.error_message,
        heartbeat_at=job.heartbeat_at,
        lease_expires_at=job.lease_expires_at,
        attempt_no=job.attempt_no,
        attempt_timeout_seconds=job.attempt_timeout_seconds,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        updated_at=job.updated_at,
    )
