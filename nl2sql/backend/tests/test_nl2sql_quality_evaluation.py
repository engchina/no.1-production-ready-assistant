from __future__ import annotations

import io
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from openpyxl import Workbook, load_workbook  # type: ignore[import-untyped]

import app.features.nl2sql.router as nl2sql_router_module
from app.features.nl2sql.models import (
    Nl2SqlEngine,
    Nl2SqlProfile,
    SampleDataMutationRequest,
    SampleDataStep,
)
from app.features.nl2sql.quality_evaluation_models import (
    QualityEvaluationCase,
    QualityEvaluationJobRecord,
    QualityEvaluationJudge,
    QualityEvaluationResult,
    QualityEvaluationRunningAttempt,
    QualityEvaluationStatus,
    QualityEvaluationVerdict,
    job_summary,
)
from app.features.nl2sql.quality_evaluation_service import (
    QualityEvaluationJobStateError,
    QualityEvaluationService,
    QualityEvaluationValidationError,
)
from app.features.nl2sql.quality_evaluation_store import (
    MemoryQualityEvaluationRepository,
    OracleQualityEvaluationRepository,
)
from app.features.nl2sql.service import Nl2SqlService
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.main import app
from app.settings import get_settings


def _xlsx(
    rows: list[list[object]],
    *,
    headers: list[str] | None = None,
    sheet_name: str = "cases",
) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    sheet.append(headers or ["ケースID", "質問", "期待SQL"])
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _service(**kwargs: object) -> QualityEvaluationService:
    return QualityEvaluationService(
        Nl2SqlService(store=MemoryNl2SqlStore()),
        repository=MemoryQualityEvaluationRepository(),
        **kwargs,  # type: ignore[arg-type]
    )


def _create_sample_profile(service: Nl2SqlService) -> Nl2SqlProfile:
    return service.create_profile(
        Nl2SqlProfile(
            id="sql_assist_sample",
            name="SQL_ASSIST_SAMPLE",
            category="SAMPLE",
            description="SQL Assist sample objects test profile.",
            allowed_tables=["DEPARTMENT", "EMPLOYEE", "PROJECT"],
            allowed_views=["V_EMP_DEPT", "V_DEPT_PROJECT"],
        )
    )


def _judge(*_args: object) -> QualityEvaluationJudge:
    return QualityEvaluationJudge(
        verdict=QualityEvaluationVerdict.CORRECT,
        confidence=0.92,
        summary="質問の意味と一致します。",
    )


def _evaluation_job(
    job_id: str,
    *,
    status: QualityEvaluationStatus = QualityEvaluationStatus.PENDING,
    created_at: datetime | None = None,
    lease_expires_at: str | None = None,
    profile_id: str = "default",
    current_attempt_started_at: str | None = None,
    attempt_timeout_seconds: float = 300.0,
) -> QualityEvaluationJobRecord:
    now = created_at or datetime.now(UTC)
    finished_at = (
        now.isoformat()
        if status
        in {
            QualityEvaluationStatus.COMPLETED,
            QualityEvaluationStatus.COMPLETED_WITH_ERRORS,
            QualityEvaluationStatus.FAILED,
            QualityEvaluationStatus.CANCELLED,
        }
        else None
    )
    return QualityEvaluationJobRecord(
        job_id=job_id,
        profile_id=profile_id,
        profile_name=profile_id,
        engines=[Nl2SqlEngine.ENTERPRISE_AI_DIRECT],
        repeat_count=1,
        cases=[
            QualityEvaluationCase(
                case_no=1,
                case_id="A",
                excel_row=2,
                question="q",
                expected_sql="SELECT 1 FROM dual",
            )
        ],
        status=status,
        total_attempts=1,
        running_attempts=(
            [
                QualityEvaluationRunningAttempt(
                    case_no=1,
                    case_id="A",
                    engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
                    repetition_no=1,
                    started_at=current_attempt_started_at,
                )
            ]
            if current_attempt_started_at
            else []
        ),
        attempt_timeout_seconds=attempt_timeout_seconds,
        lease_expires_at=lease_expires_at,
        created_at=now.isoformat(),
        finished_at=finished_at,
        updated_at=now.isoformat(),
    )


def _evaluation_result(job_id: str) -> QualityEvaluationResult:
    now = datetime.now(UTC).isoformat()
    return QualityEvaluationResult(
        result_id=f"{job_id}-result-1",
        job_id=job_id,
        case_no=1,
        case_id="A",
        excel_row=2,
        question="q",
        expected_sql="SELECT 1 FROM dual",
        engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
        repetition_no=1,
        generated_sql="SELECT 1 FROM dual",
        normalized_sql="SELECT 1 FROM DUAL",
        verdict=QualityEvaluationVerdict.CORRECT,
        created_at=now,
    )


class _FakeEnterpriseAiClient:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    def is_configured(self) -> bool:
        return True

    def model_id(self) -> str:
        return "enterprise-nl2sql-model"

    def generate(
        self,
        *,
        prompt: str,
        context: str,
        system_prompt: str,
        timeout_seconds: float | None = None,
        max_output_tokens: int | None = None,
        max_retries: int | None = None,
        response_format: object | None = None,
    ) -> str:
        self.calls.append(
            {
                "prompt": prompt,
                "context": context,
                "system_prompt": system_prompt,
                "timeout_seconds": timeout_seconds,
                "max_retries": max_retries,
            }
        )
        return self.text


def _configure_strict_engine_runtime(
    service: Nl2SqlService, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_enterprise_ai_direct_enabled", True)
    monkeypatch.setattr(service, "_use_oracle_runtime", lambda: True)
    monkeypatch.setattr(service._oracle_adapter, "is_configured", lambda: True)
    monkeypatch.setattr(service._enterprise_ai_client, "is_configured", lambda: True)


class _LobPayload:
    def __init__(self, value: str) -> None:
        self.value = value


class _ConnectionBoundLob:
    def __init__(self, connection: _QualityEvaluationOracleConnection, value: str) -> None:
        self._connection = connection
        self._value = value

    def read(self) -> str:
        if self._connection.closed:
            raise RuntimeError("LOB locator is no longer valid")
        return self._value


_ResultSet = list[tuple[object, ...]]


class _QualityEvaluationOracleCursor:
    def __init__(
        self,
        connection: _QualityEvaluationOracleConnection,
        result_sets: list[_ResultSet],
    ) -> None:
        self._connection = connection
        self._result_sets = result_sets
        self._current: _ResultSet = []
        self.prefetchrows: int | None = None
        self.arraysize: int | None = None
        self.rowcount = 0
        self.executed: list[tuple[str, Any]] = []

    def __enter__(self) -> _QualityEvaluationOracleCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, binds: Any = None) -> None:
        self.executed.append((sql, binds))
        self.rowcount = 0
        if not sql.lstrip().upper().startswith("SELECT"):
            self._current = []
            self.rowcount = 1
            return
        if not self._result_sets:
            raise AssertionError("scripted result set is missing")
        self._current = [self._materialize_row(row) for row in self._result_sets.pop(0)]

    def fetchone(self) -> tuple[object, ...] | None:
        return self._current[0] if self._current else None

    def fetchall(self) -> _ResultSet:
        return list(self._current)

    def _materialize_row(self, row: tuple[object, ...]) -> tuple[object, ...]:
        return tuple(
            (
                _ConnectionBoundLob(self._connection, value.value)
                if isinstance(value, _LobPayload)
                else value
            )
            for value in row
        )


class _QualityEvaluationOracleConnection:
    def __init__(self, result_sets: list[_ResultSet]) -> None:
        self._result_sets = result_sets
        self.closed = False
        self.cursors: list[_QualityEvaluationOracleCursor] = []
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self) -> _QualityEvaluationOracleConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        self.closed = True

    def cursor(self) -> _QualityEvaluationOracleCursor:
        cursor = _QualityEvaluationOracleCursor(self, self._result_sets)
        self.cursors.append(cursor)
        return cursor

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class _QualityEvaluationOracleConnectionFactory:
    def __init__(self, calls: list[list[_ResultSet]]) -> None:
        self._calls = calls
        self.connections: list[_QualityEvaluationOracleConnection] = []

    def __call__(self) -> _QualityEvaluationOracleConnection:
        if not self._calls:
            raise AssertionError("scripted connection is missing")
        connection = _QualityEvaluationOracleConnection(self._calls.pop(0))
        self.connections.append(connection)
        return connection


def test_parse_cases_accepts_japanese_and_english_headers_and_active_sheet() -> None:
    service = _service()
    japanese = service.parse_cases(
        _xlsx([["JP-1", "一覧を取得", "SELECT * FROM orders"]]), "cases.xlsx"
    )
    english = service.parse_cases(
        _xlsx(
            [["EN-1", "count rows", "WITH q AS (SELECT 1 x FROM dual) SELECT * FROM q"]],
            headers=["CASE_ID", "QUESTION", "EXPECTED_SQL"],
            sheet_name="Sheet1",
        ),
        "cases.xlsx",
    )
    assert japanese[0].case_id == "JP-1"
    assert english[0].excel_row == 2


def test_parse_cases_ignores_empty_rows_and_assigns_case_id() -> None:
    cases = _service().parse_cases(
        _xlsx([[None, None, None], [None, "件数", "SELECT COUNT(*) FROM orders"]]),
        "cases.xlsx",
    )
    assert len(cases) == 1
    assert cases[0].case_id == "CASE-0001"
    assert cases[0].excel_row == 3


def test_parse_cases_auto_case_ids_skip_explicit_case_ids() -> None:
    cases = _service().parse_cases(
        _xlsx(
            [
                ["CASE-0003", "明示 ID", "SELECT 1 FROM dual"],
                [None, "自動 ID 1", "SELECT 2 FROM dual"],
                [None, "自動 ID 2", "SELECT 3 FROM dual"],
            ]
        ),
        "cases.xlsx",
    )

    assert [case.case_id for case in cases] == ["CASE-0003", "CASE-0001", "CASE-0002"]


def test_parse_cases_reads_limits_without_engine_readiness_probe() -> None:
    def fail_readiness(_profile_id: str | None) -> dict[Nl2SqlEngine, tuple[bool, str]]:
        raise AssertionError("parse_cases must not probe engine readiness")

    cases = _service(readiness_provider=fail_readiness).parse_cases(
        _xlsx([["A", "質問", "SELECT 1 FROM dual"]]),
        "cases.xlsx",
    )

    assert cases[0].case_id == "A"


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([["A", "質問", "=A1"]], "数式セル"),
        (
            [["A", "質問1", "SELECT 1 FROM dual"], ["A", "質問2", "SELECT 2 FROM dual"]],
            "重複",
        ),
        ([["A", "質問", "DELETE FROM orders"]], "SELECT/WITH"),
        ([["A", "", "SELECT 1 FROM dual"]], "質問は必須"),
    ],
)
def test_parse_cases_rejects_entire_workbook_with_excel_row_errors(
    rows: list[list[object]], expected: str
) -> None:
    with pytest.raises(QualityEvaluationValidationError) as caught:
        _service().parse_cases(_xlsx(rows), "cases.xlsx")
    assert expected in str(caught.value)
    assert "行 " in str(caught.value)


def test_parse_cases_rejects_extension_and_file_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_max_file_bytes", 32)
    with pytest.raises(QualityEvaluationValidationError) as caught:
        _service().parse_cases(b"x" * 33, "cases.xls")
    assert ".xlsx" in str(caught.value)
    assert "ファイルサイズ" in str(caught.value)


def test_parse_cases_enforces_case_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_max_cases", 1)
    with pytest.raises(QualityEvaluationValidationError, match="ケース数が上限 1"):
        _service().parse_cases(
            _xlsx(
                [
                    ["A", "質問1", "SELECT 1 FROM dual"],
                    ["B", "質問2", "SELECT 2 FROM dual"],
                ]
            ),
            "cases.xlsx",
        )


def test_submit_validates_engines_repeat_profile_and_attempt_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(engine_runner=lambda *_args: "SELECT 1 FROM dual", judge_runner=_judge)
    workbook = _xlsx([["A", "質問", "SELECT 1 FROM dual"]])
    with pytest.raises(QualityEvaluationValidationError, match="1つ以上"):
        service.submit(
            profile_id="default",
            engines=[],
            repeat_count=1,
            content=workbook,
            filename="cases.xlsx",
        )
    with pytest.raises(QualityEvaluationValidationError, match="1〜10"):
        service.submit(
            profile_id="default",
            engines=[Nl2SqlEngine.ENTERPRISE_AI_DIRECT],
            repeat_count=11,
            content=workbook,
            filename="cases.xlsx",
        )
    with pytest.raises(QualityEvaluationValidationError, match="業務プロファイル"):
        service.submit(
            profile_id="missing",
            engines=[Nl2SqlEngine.ENTERPRISE_AI_DIRECT],
            repeat_count=1,
            content=workbook,
            filename="cases.xlsx",
        )
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_max_attempts", 1)
    with pytest.raises(QualityEvaluationValidationError, match="総試行回数"):
        service.submit(
            profile_id="default",
            engines=[Nl2SqlEngine.ENTERPRISE_AI_DIRECT],
            repeat_count=2,
            content=workbook,
            filename="cases.xlsx",
        )


def test_submit_accepts_repeat_boundaries_and_rejects_unavailable_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_worker_mode", "external")
    workbook = _xlsx([["A", "質問", "SELECT 1 FROM dual"]])
    service = _service(engine_runner=lambda *_args: "SELECT 1 FROM dual", judge_runner=_judge)
    assert (
        service.submit(
            profile_id="default",
            engines=[Nl2SqlEngine.ENTERPRISE_AI_DIRECT],
            repeat_count=1,
            content=workbook,
            filename="cases.xlsx",
        ).total_attempts
        == 1
    )
    assert (
        service.submit(
            profile_id="default",
            engines=[Nl2SqlEngine.ENTERPRISE_AI_DIRECT],
            repeat_count=10,
            content=workbook,
            filename="cases.xlsx",
        ).total_attempts
        == 10
    )

    unavailable = _service(
        engine_runner=lambda *_args: "SELECT 1 FROM dual",
        judge_runner=_judge,
        readiness_provider=lambda _profile_id: {
            Nl2SqlEngine.ENTERPRISE_AI_DIRECT: (False, "モデルの API が未構成です。")
        },
    )
    with pytest.raises(QualityEvaluationValidationError, match="未構成"):
        unavailable.submit(
            profile_id="default",
            engines=[Nl2SqlEngine.ENTERPRISE_AI_DIRECT],
            repeat_count=1,
            content=workbook,
            filename="cases.xlsx",
        )


def test_quality_evaluation_get_wakes_orphan_pending_job_inprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_worker_mode", "inprocess")
    repository = MemoryQualityEvaluationRepository()
    repository.save_job(_evaluation_job("pending-job"))
    service = QualityEvaluationService(
        Nl2SqlService(store=MemoryNl2SqlStore()), repository=repository
    )
    dispatched: list[str] = []
    monkeypatch.setattr(service, "_dispatch", lambda job_id: dispatched.append(job_id))

    fetched = service.get_job("pending-job")

    assert fetched.status == QualityEvaluationStatus.PENDING
    assert dispatched == ["pending-job"]


def test_quality_evaluation_get_wakes_expired_running_job_only_inprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_worker_mode", "inprocess")
    now = datetime.now(UTC)
    repository = MemoryQualityEvaluationRepository()
    repository.save_job(
        _evaluation_job(
            "expired-running",
            status=QualityEvaluationStatus.RUNNING,
            lease_expires_at=(now - timedelta(seconds=1)).isoformat(),
        )
    )
    repository.save_job(
        _evaluation_job(
            "active-running",
            status=QualityEvaluationStatus.RUNNING,
            lease_expires_at=(now + timedelta(minutes=5)).isoformat(),
        )
    )
    repository.save_job(_evaluation_job("completed", status=QualityEvaluationStatus.COMPLETED))
    service = QualityEvaluationService(
        Nl2SqlService(store=MemoryNl2SqlStore()), repository=repository
    )
    dispatched: list[str] = []
    monkeypatch.setattr(service, "_dispatch", lambda job_id: dispatched.append(job_id))

    service.get_job("expired-running")
    service.get_job("active-running")
    service.get_job("completed")

    assert dispatched == ["expired-running"]


def test_quality_evaluation_get_does_not_wake_external_worker_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_worker_mode", "external")
    repository = MemoryQualityEvaluationRepository()
    repository.save_job(_evaluation_job("pending-job"))
    service = QualityEvaluationService(
        Nl2SqlService(store=MemoryNl2SqlStore()), repository=repository
    )
    dispatched: list[str] = []
    monkeypatch.setattr(service, "_dispatch", lambda job_id: dispatched.append(job_id))

    fetched = service.get_job("pending-job")

    assert fetched.status == QualityEvaluationStatus.PENDING
    assert dispatched == []


def test_quality_evaluation_list_wakes_visible_orphan_jobs_inprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_worker_mode", "inprocess")
    now = datetime.now(UTC)
    repository = MemoryQualityEvaluationRepository()
    repository.save_job(_evaluation_job("older-pending", created_at=now - timedelta(minutes=2)))
    repository.save_job(_evaluation_job("newer-pending", created_at=now - timedelta(minutes=1)))
    repository.save_job(_evaluation_job("completed", status=QualityEvaluationStatus.COMPLETED))
    service = QualityEvaluationService(
        Nl2SqlService(store=MemoryNl2SqlStore()), repository=repository
    )
    dispatched: list[str] = []
    monkeypatch.setattr(service, "_dispatch", lambda job_id: dispatched.append(job_id))

    page = service.list_jobs(limit=10)

    assert [item.job_id for item in page.items] == [
        "completed",
        "newer-pending",
        "older-pending",
    ]
    assert dispatched == ["newer-pending", "older-pending"]


def test_quality_evaluation_list_filters_profile_before_paging() -> None:
    repository = MemoryQualityEvaluationRepository()
    base = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)
    for index in range(12):
        repository.save_job(
            _evaluation_job(
                f"foreign-{index:02d}",
                profile_id="foreign",
                created_at=base + timedelta(minutes=20 - index),
            )
        )
    for index in range(3):
        repository.save_job(
            _evaluation_job(
                f"allowed-{index:02d}",
                profile_id="allowed",
                created_at=base - timedelta(minutes=index),
            )
        )
    service = QualityEvaluationService(
        Nl2SqlService(store=MemoryNl2SqlStore()), repository=repository
    )

    first = service.list_jobs(
        offset=0,
        limit=2,
        allowed_profile_ids={"allowed"},
    )
    second = service.list_jobs(
        offset=2,
        limit=2,
        allowed_profile_ids={"allowed"},
    )
    beyond = service.list_jobs(
        offset=6,
        limit=2,
        allowed_profile_ids={"allowed"},
    )

    assert first.total == 3
    assert [item.job_id for item in first.items] == ["allowed-00", "allowed-01"]
    assert (first.limit, first.offset, first.has_next) == (2, 0, True)
    assert second.total == 3
    assert [item.job_id for item in second.items] == ["allowed-02"]
    assert second.has_next is False
    assert (beyond.items, beyond.total, beyond.has_next) == ([], 3, False)


def test_quality_evaluation_summary_exposes_current_attempt_deadline() -> None:
    summary = job_summary(
        _evaluation_job(
            "running-job",
            status=QualityEvaluationStatus.RUNNING,
            current_attempt_started_at="2026-07-22T08:00:00Z",
            attempt_timeout_seconds=300,
        )
    )
    completed = job_summary(
        _evaluation_job("completed-job", status=QualityEvaluationStatus.COMPLETED)
    )

    assert summary.current_attempt_deadline_at == "2026-07-22T08:10:00+00:00"
    assert summary.current_case_id == "A"
    assert summary.current_engine == Nl2SqlEngine.ENTERPRISE_AI_DIRECT
    assert summary.current_repetition == 1
    assert [item.deadline_at for item in summary.running_attempts] == ["2026-07-22T08:10:00+00:00"]
    assert completed.current_attempt_deadline_at is None
    assert completed.running_attempts == []


def test_quality_evaluation_summary_reports_oldest_of_running_attempts() -> None:
    """並行の試行のうち最も前に始めた試行を「現在の試行」として出す（#2059）。"""
    job = _evaluation_job(
        "running-job",
        status=QualityEvaluationStatus.RUNNING,
        attempt_timeout_seconds=300,
    ).model_copy(
        update={
            "concurrency": 2,
            "running_attempts": [
                QualityEvaluationRunningAttempt(
                    case_no=1,
                    case_id="B",
                    engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
                    repetition_no=2,
                    started_at="2026-07-22T08:01:00+00:00",
                ),
                QualityEvaluationRunningAttempt(
                    case_no=1,
                    case_id="A",
                    engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
                    repetition_no=1,
                    started_at="2026-07-22T08:00:00+00:00",
                ),
            ],
        }
    )

    summary = job_summary(job)

    assert (summary.current_case_id, summary.current_repetition) == ("A", 1)
    assert summary.current_attempt_deadline_at == "2026-07-22T08:10:00+00:00"
    assert len(summary.running_attempts) == 2
    assert summary.concurrency == 2


def test_quality_evaluation_dispatch_drops_finished_active_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = QualityEvaluationService(
        Nl2SqlService(store=MemoryNl2SqlStore()),
        repository=MemoryQualityEvaluationRepository(),
    )
    finished = threading.Event()

    def run_job(*, job_id: str, worker_id: str | None = None) -> None:
        del worker_id
        assert job_id == "finished-job"
        finished.set()

    monkeypatch.setattr(service, "run_job", run_job)

    service._dispatch("finished-job")  # noqa: SLF001

    assert finished.wait(timeout=5)
    for _ in range(50):
        if not service._active_threads:  # noqa: SLF001
            break
        time.sleep(0.02)
    assert service._active_threads == {}  # noqa: SLF001


def test_quality_evaluation_delete_terminal_job_removes_job_and_results() -> None:
    repository = MemoryQualityEvaluationRepository()
    repository.save_job(_evaluation_job("completed-job", status=QualityEvaluationStatus.COMPLETED))
    repository.save_result(_evaluation_result("completed-job"))
    service = QualityEvaluationService(
        Nl2SqlService(store=MemoryNl2SqlStore()), repository=repository
    )

    deleted = service.delete_job("completed-job")

    assert deleted.job_id == "completed-job"
    assert repository.get_job("completed-job") is None
    results, total = repository.list_results(job_id="completed-job", offset=0, limit=10)
    assert results == []
    assert total == 0


def test_quality_evaluation_delete_missing_job_raises_not_found() -> None:
    service = _service()

    with pytest.raises(ValueError, match="見つかりません"):
        service.delete_job("missing-job")


@pytest.mark.parametrize(
    "status",
    [QualityEvaluationStatus.PENDING, QualityEvaluationStatus.RUNNING],
)
def test_quality_evaluation_delete_rejects_active_jobs(status: QualityEvaluationStatus) -> None:
    repository = MemoryQualityEvaluationRepository()
    repository.save_job(_evaluation_job("active-job", status=status))
    repository.save_result(_evaluation_result("active-job"))
    service = QualityEvaluationService(
        Nl2SqlService(store=MemoryNl2SqlStore()), repository=repository
    )

    with pytest.raises(QualityEvaluationJobStateError, match="削除できません"):
        service.delete_job("active-job")

    assert repository.get_job("active-job") is not None
    results, total = repository.list_results(job_id="active-job", offset=0, limit=10)
    assert [item.result_id for item in results] == ["active-job-result-1"]
    assert total == 1


def test_quality_evaluation_capabilities_exposes_attempt_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_attempt_timeout_seconds", 300.0)
    service = _service(engine_runner=lambda *_args: "SELECT 1 FROM dual", judge_runner=_judge)

    capabilities = service.capabilities(profile_id="default")

    assert capabilities.limits.attempt_timeout_seconds == 300.0


def test_quality_evaluation_worker_lease_uses_settings_and_retry_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    service = _service(engine_runner=lambda *_args: "SELECT 1 FROM dual", judge_runner=_judge)

    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_attempt_timeout_seconds", 300.0)
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_lease_seconds", 900.0)
    monkeypatch.setattr(settings, "enterprise_ai_max_retries", 0)
    assert service._attempt_lease_seconds() == 900.0  # noqa: SLF001

    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_lease_seconds", 60.0)
    monkeypatch.setattr(settings, "enterprise_ai_max_retries", 3)
    assert service._attempt_lease_seconds() == 2400.0  # noqa: SLF001


def test_quality_evaluation_cancel_is_idempotent_and_rejects_completed_jobs() -> None:
    repository = MemoryQualityEvaluationRepository()
    repository.save_job(_evaluation_job("pending-job"))
    repository.save_job(_evaluation_job("finished-job", status=QualityEvaluationStatus.COMPLETED))
    service = QualityEvaluationService(
        Nl2SqlService(store=MemoryNl2SqlStore()), repository=repository
    )

    cancelled = service.cancel_job("pending-job")
    cancelled_again = service.cancel_job("pending-job")

    assert cancelled.status == QualityEvaluationStatus.CANCELLED
    assert cancelled_again.status == QualityEvaluationStatus.CANCELLED
    assert cancelled_again.finished_at is not None
    with pytest.raises(QualityEvaluationJobStateError, match="中止できません"):
        service.cancel_job("finished-job")
    with pytest.raises(ValueError, match="見つかりません"):
        service.cancel_job("missing-job")


def test_worker_records_engine_timeout_as_error_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_worker_mode", "external")
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_attempt_timeout_seconds", 300.0)

    def engine(_question: str, selected: Nl2SqlEngine, _profile: str) -> str:
        assert selected == Nl2SqlEngine.ENTERPRISE_AI_DIRECT
        raise RuntimeError("DPI-1067: call timeout of 300000 ms exceeded")

    service = _service(engine_runner=engine, judge_runner=_judge)
    submitted = service.submit(
        profile_id="default",
        engines=[Nl2SqlEngine.ENTERPRISE_AI_DIRECT],
        repeat_count=1,
        content=_xlsx([["A", "質問", "SELECT 1 FROM dual"]]),
        filename="cases.xlsx",
    )

    service.run_job(job_id=submitted.job_id)

    result = service.list_results(job_id=submitted.job_id, limit=1).items[0]
    assert result.verdict == QualityEvaluationVerdict.NOT_ANALYZED
    assert "Enterprise AI Direct" in result.generation_error
    assert "300 秒" in result.generation_error
    assert "タイムアウト" in result.generation_error
    assert service.get_job(submitted.job_id).status == QualityEvaluationStatus.COMPLETED_WITH_ERRORS


def test_evaluation_worker_discards_result_after_expired_lease_is_reclaimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_worker_mode", "external")
    repository = MemoryQualityEvaluationRepository()
    service = QualityEvaluationService(
        Nl2SqlService(store=MemoryNl2SqlStore()),
        repository=repository,
        engine_runner=lambda *_args: "SELECT 1 FROM dual",
        judge_runner=_judge,
    )
    submitted = service.submit(
        profile_id="default",
        engines=[Nl2SqlEngine.ENTERPRISE_AI_DIRECT],
        repeat_count=1,
        content=_xlsx([["A", "質問", "SELECT 1 FROM dual"]]),
        filename="cases.xlsx",
    )

    def engine(_question: str, _selected: Nl2SqlEngine, _profile: str) -> str:
        current = repository.get_job(submitted.job_id)
        assert current is not None
        expired = current.model_copy(
            update={"lease_expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()},
            deep=True,
        )
        repository.save_job(expired)
        reclaimed = repository.claim_job(
            worker_id="new-worker",
            lease_seconds=60,
            job_id=submitted.job_id,
        )
        assert reclaimed is not None
        assert reclaimed.worker_id == "new-worker"
        assert reclaimed.attempt_no == 2
        return "SELECT 1 FROM dual"

    service._engine_runner = engine  # noqa: SLF001

    service.run_job(job_id=submitted.job_id, worker_id="old-worker")

    job = repository.get_job(submitted.job_id)
    results, total = repository.list_results(job_id=submitted.job_id, offset=0, limit=10)
    assert job is not None
    assert job.status == QualityEvaluationStatus.RUNNING
    assert job.worker_id == "new-worker"
    assert job.attempt_no == 2
    assert results == []
    assert total == 0


def test_expired_lease_reclaim_synthesizes_timeout_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_worker_mode", "external")
    now = datetime.now(UTC)
    repository = MemoryQualityEvaluationRepository()
    repository.save_job(
        QualityEvaluationJobRecord(
            job_id="expired-attempt",
            profile_id="default",
            profile_name="default",
            engines=[Nl2SqlEngine.ENTERPRISE_AI_DIRECT],
            repeat_count=1,
            cases=[
                QualityEvaluationCase(
                    case_no=1,
                    case_id="A",
                    excel_row=2,
                    question="q",
                    expected_sql="SELECT 1 FROM dual",
                )
            ],
            status=QualityEvaluationStatus.RUNNING,
            total_attempts=1,
            running_attempts=[
                QualityEvaluationRunningAttempt(
                    case_no=1,
                    case_id="A",
                    engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
                    repetition_no=1,
                    started_at=(now - timedelta(seconds=360)).isoformat(),
                )
            ],
            heartbeat_at=(now - timedelta(seconds=360)).isoformat(),
            lease_expires_at=(now - timedelta(seconds=1)).isoformat(),
            attempt_timeout_seconds=300.0,
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        )
    )

    def engine(*_args: object) -> str:
        raise AssertionError("expired attempt should not be executed again")

    service = QualityEvaluationService(
        Nl2SqlService(store=MemoryNl2SqlStore()),
        repository=repository,
        engine_runner=engine,
        judge_runner=_judge,
    )

    service.run_job(job_id="expired-attempt", worker_id="reclaimer")

    result = service.list_results(job_id="expired-attempt", limit=1).items[0]
    job = service.get_job("expired-attempt")
    assert "worker lease" in result.generation_error
    assert "タイムアウト" in result.generation_error
    assert result.verdict == QualityEvaluationVerdict.NOT_ANALYZED
    assert job.status == QualityEvaluationStatus.COMPLETED_WITH_ERRORS
    assert job.completed_attempts == 1


def test_expired_lease_reclaim_times_out_each_running_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lease の再取得のとき、実行中だった試行をそれぞれ時間切れにする（#2059）。

    試行の timeout を過ぎた試行は時間切れの結果を記録し、過ぎていない試行はもう一度実行する。
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_worker_mode", "external")
    now = datetime.now(UTC)
    cases = [
        QualityEvaluationCase(
            case_no=index,
            case_id=case_id,
            excel_row=index + 1,
            question=f"q-{case_id}",
            expected_sql="SELECT 1 FROM dual",
        )
        for index, case_id in enumerate(["A", "B", "C"], start=1)
    ]

    def running(case_no: int, case_id: str, seconds_ago: int) -> QualityEvaluationRunningAttempt:
        return QualityEvaluationRunningAttempt(
            case_no=case_no,
            case_id=case_id,
            engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
            repetition_no=1,
            started_at=(now - timedelta(seconds=seconds_ago)).isoformat(),
        )

    repository = MemoryQualityEvaluationRepository()
    repository.save_job(
        QualityEvaluationJobRecord(
            job_id="expired-attempts",
            profile_id="default",
            profile_name="default",
            engines=[Nl2SqlEngine.ENTERPRISE_AI_DIRECT],
            repeat_count=1,
            cases=cases,
            status=QualityEvaluationStatus.RUNNING,
            total_attempts=3,
            running_attempts=[running(1, "A", 360), running(2, "B", 420), running(3, "C", 10)],
            concurrency=3,
            heartbeat_at=(now - timedelta(seconds=10)).isoformat(),
            lease_expires_at=(now - timedelta(seconds=1)).isoformat(),
            attempt_timeout_seconds=300.0,
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        )
    )
    executed: list[str] = []

    def engine(question: str, _selected: Nl2SqlEngine, _profile: str) -> str:
        executed.append(question)
        return "SELECT 1 FROM dual"

    service = QualityEvaluationService(
        Nl2SqlService(store=MemoryNl2SqlStore()),
        repository=repository,
        engine_runner=engine,
        judge_runner=_judge,
    )

    service.run_job(job_id="expired-attempts", worker_id="reclaimer")

    results = {
        item.case_id: item
        for item in service.list_results(job_id="expired-attempts", limit=10).items
    }
    job = service.get_job("expired-attempts")
    # 時間切れの 2 つは実行し直さず、それぞれ時間切れの結果になる。
    assert executed == ["q-C"]
    for case_id in ("A", "B"):
        assert "worker lease" in results[case_id].generation_error
        assert results[case_id].verdict == QualityEvaluationVerdict.NOT_ANALYZED
    assert results["C"].generation_error == ""
    assert results["C"].verdict == QualityEvaluationVerdict.CORRECT
    assert job.status == QualityEvaluationStatus.COMPLETED_WITH_ERRORS
    assert job.completed_attempts == 3
    assert job.running_attempts == []


class _HttpFailure(Exception):
    """openai / httpx と同じく ``status_code`` と ``headers`` を持つ失敗のスタブ。"""

    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        super().__init__(f"HTTP {status}")
        self.status_code = status
        self.headers = headers or {}


class _SlowEngine:
    """遅延のある SQL 生成のスタブ（同時の呼び出しの数と、最初の何回かの失敗）。"""

    def __init__(self, *, delay: float = 0.02, failures: list[BaseException] | None = None) -> None:
        self.delay = delay
        self.failures = list(failures or [])
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def __call__(self, question: str, _engine: Nl2SqlEngine, _profile: str) -> str:
        with self.lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            failure = self.failures.pop(0) if self.failures else None
        try:
            time.sleep(self.delay)
            if failure is not None:
                raise failure
            return f"SELECT '{question}' FROM dual"
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture
def no_real_ai_wait(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """実行器の待ちを実時間で待たない（待った秒数を残す）。"""
    import asyncio

    from app.features.nl2sql import ai_calls

    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)
        await asyncio.sleep(0)

    monkeypatch.setattr(ai_calls, "_sleep", fake_sleep)
    monkeypatch.setattr(ai_calls, "_random", lambda: 0.5)
    return waits


def _capture_evaluation_caller(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """評価が作った実行器の event を残す。"""
    from app.features.nl2sql import quality_evaluation_service as module
    from app.features.nl2sql.ai_calls import sync_ai_caller

    events: list[Any] = []
    original = sync_ai_caller

    def capture(settings: Any, stage: Any) -> Any:
        bridge = original(settings, stage)
        bridge.caller.add_listener(events.append)
        return bridge

    monkeypatch.setattr(module, "sync_ai_caller", capture)
    return events


def _submit_cases(service: QualityEvaluationService, *, cases: int, repeat: int = 4) -> str:
    return service.submit(
        profile_id="default",
        engines=[Nl2SqlEngine.ENTERPRISE_AI_DIRECT],
        repeat_count=repeat,
        content=_xlsx(
            [[f"C{index}", f"質問{index}", "SELECT 1 FROM dual"] for index in range(cases)]
        ),
        filename="cases.xlsx",
    ).job_id


def test_attempts_run_concurrently_and_results_keep_their_positions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_worker_mode", "external")
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_concurrency", 3)
    engine = _SlowEngine()
    service = _service(engine_runner=engine, judge_runner=_judge)
    job_id = _submit_cases(service, cases=3)

    service.run_job(job_id=job_id)

    job = service.get_job(job_id)
    items = service.list_results(job_id=job_id, limit=100).items
    assert job.status == QualityEvaluationStatus.COMPLETED
    assert job.completed_attempts == 12 and job.running_attempts == []
    assert job.concurrency == 3
    assert engine.max_active == 3
    # 結果はケース・engine・繰り返しの位置に書く（終わる順に関わらない）。
    assert [(item.case_id, item.engine, item.repetition_no) for item in items] == [
        (f"C{case}", Nl2SqlEngine.ENTERPRISE_AI_DIRECT, repetition)
        for case in range(3)
        for repetition in (1, 2, 3, 4)
    ]
    assert all(item.generated_sql == f"SELECT '{item.question}' FROM dual" for item in items)


def test_rate_limited_attempts_wait_together_and_finish_without_losing_results(
    monkeypatch: pytest.MonkeyPatch, no_real_ai_wait: list[float]
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_worker_mode", "external")
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_concurrency", 4)
    events = _capture_evaluation_caller(monkeypatch)
    engine = _SlowEngine(failures=[_HttpFailure(429, {"retry-after": "5"}) for _ in range(3)])
    service = _service(engine_runner=engine, judge_runner=_judge)
    job_id = _submit_cases(service, cases=2)

    service.run_job(job_id=job_id)

    job = service.get_job(job_id)
    items = service.list_results(job_id=job_id, limit=100).items
    assert job.status == QualityEvaluationStatus.COMPLETED
    assert len(items) == 8 and all(not item.generation_error for item in items)
    # 429 の 3 回は送り直し、並行の上限を超えない。
    assert engine.calls == 8 + 3
    assert engine.max_active <= 4
    # 全体で待ち（Retry-After 5 秒 + jitter）、並行の数を下げる。
    assert any(wait >= 5 for wait in no_real_ai_wait)
    concurrency = [event.concurrency for event in events if event.type == "concurrency"]
    assert concurrency and min(concurrency) < 4
    assert sum(1 for event in events if event.type == "retry") == 3


@pytest.mark.parametrize("stage", ["generation", "judge"], ids=["generation", "judge"])
def test_permission_error_stops_without_starting_remaining_attempts(
    monkeypatch: pytest.MonkeyPatch, no_real_ai_wait: list[float], stage: str
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_worker_mode", "external")
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_concurrency", 2)
    forbidden: list[BaseException] = [_HttpFailure(403) for _ in range(20)]
    engine = _SlowEngine(failures=forbidden if stage == "generation" else None)
    judge_calls = 0
    judge_lock = threading.Lock()

    def judge(*_args: object) -> QualityEvaluationJudge:
        nonlocal judge_calls
        with judge_lock:
            judge_calls += 1
        if stage == "judge":
            raise _HttpFailure(403)
        return _judge()

    service = _service(engine_runner=engine, judge_runner=judge)
    job_id = _submit_cases(service, cases=3)

    service.run_job(job_id=job_id)

    job = service.get_job(job_id)
    assert job.status == QualityEvaluationStatus.FAILED
    assert "403" in job.error_message
    assert job.running_attempts == []
    # 再試行せず、実行中だった試行（並行の上限まで）のほかは始めない（12 試行のうち）。
    calls = engine.calls if stage == "generation" else judge_calls
    assert 1 <= calls <= 2
    assert engine.calls <= 2
    assert service.list_results(job_id=job_id, limit=100).total == 0


def test_cancelled_job_is_not_overwritten_by_late_worker_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_worker_mode", "external")
    service = _service(judge_runner=_judge)
    submitted_job_id = ""

    def engine(_question: str, _selected: Nl2SqlEngine, _profile: str) -> str:
        service.cancel_job(submitted_job_id)
        return "SELECT 1 FROM dual"

    service._engine_runner = engine  # noqa: SLF001
    submitted = service.submit(
        profile_id="default",
        engines=[Nl2SqlEngine.ENTERPRISE_AI_DIRECT],
        repeat_count=1,
        content=_xlsx([["A", "質問", "SELECT 1 FROM dual"]]),
        filename="cases.xlsx",
    )
    submitted_job_id = submitted.job_id

    service.run_job(job_id=submitted.job_id)

    job = service.get_job(submitted.job_id)
    results = service.list_results(job_id=submitted.job_id, limit=10)
    assert job.status == QualityEvaluationStatus.CANCELLED
    assert job.completed_attempts == 0
    assert results.items == []


def test_default_quality_evaluation_enterprise_ai_calls_disable_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_worker_mode", "external")
    nl2sql = Nl2SqlService(store=MemoryNl2SqlStore())
    nl2sql.import_sample_data(
        SampleDataMutationRequest(step=SampleDataStep.ALL, confirmation="ADMIN_EXECUTE")
    )
    _create_sample_profile(nl2sql)
    fake_client = _FakeEnterpriseAiClient(
        '{"sql":"SELECT EMPLOYEE_ID FROM EMPLOYEE","explanation":"社員IDを取得します。"}'
    )
    nl2sql._enterprise_ai_client = fake_client  # noqa: SLF001
    monkeypatch.setattr(
        nl2sql,
        "quality_evaluation_engine_readiness",
        lambda profile_id=None: {Nl2SqlEngine.ENTERPRISE_AI_DIRECT: (True, "")},
    )
    monkeypatch.setattr(nl2sql, "_use_oracle_runtime", lambda: False)
    service = QualityEvaluationService(
        nl2sql,
        repository=MemoryQualityEvaluationRepository(),
        judge_runner=_judge,
    )
    submitted = service.submit(
        profile_id="sql_assist_sample",
        engines=[Nl2SqlEngine.ENTERPRISE_AI_DIRECT],
        repeat_count=1,
        content=_xlsx([["A", "社員IDを取得してください", "SELECT EMPLOYEE_ID FROM EMPLOYEE"]]),
        filename="cases.xlsx",
    )

    service.run_job(job_id=submitted.job_id)

    assert fake_client.calls[0]["timeout_seconds"] == 300.0
    assert fake_client.calls[0]["max_retries"] == 0


def test_worker_runs_every_attempt_and_isolates_generation_and_judge_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[tuple[str, Nl2SqlEngine], int] = {}
    # 試行は並行に実行する（#2059）。数えるところは lock で直列にする。
    calls_lock = threading.Lock()

    def engine(question: str, selected: Nl2SqlEngine, _profile: str) -> str:
        key = (question, selected)
        with calls_lock:
            calls[key] = calls.get(key, 0) + 1
            count = calls[key]
        if count == 1:
            raise RuntimeError("strict engine failure")
        return "SELECT 1 FROM dual"

    def judge(
        question: str,
        _expected: str,
        _generated: str,
        _profile: str,
        _analysis: object,
    ) -> QualityEvaluationJudge:
        if question == "judge failure":
            raise RuntimeError("invalid judge response")
        return _judge()

    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_worker_mode", "external")
    service = _service(engine_runner=engine, judge_runner=judge)
    submitted = service.submit(
        profile_id="default",
        engines=[Nl2SqlEngine.ENTERPRISE_AI_DIRECT],
        repeat_count=4,
        content=_xlsx(
            [
                ["A", "ok", "SELECT 1 FROM dual"],
                ["B", "judge failure", "SELECT 1 FROM dual"],
            ]
        ),
        filename="cases.xlsx",
    )
    service.run_job(job_id=submitted.job_id, worker_id="test-worker")
    job = service.get_job(submitted.job_id)
    page = service.list_results(job_id=submitted.job_id, limit=100)
    assert job.status == QualityEvaluationStatus.COMPLETED_WITH_ERRORS
    assert job.completed_attempts == 8
    assert len(page.items) == 8
    assert sum(bool(item.generation_error) for item in page.items) == 2
    assert sum(bool(item.judge_error) for item in page.items) == 3
    assert sum(item.verdict == QualityEvaluationVerdict.CORRECT for item in page.items) == 3


def test_worker_aggregates_verdict_distribution_and_normalized_consistency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_worker_mode", "external")
    call_count = 0
    count_lock = threading.Lock()

    def engine(*_args: object) -> str:
        nonlocal call_count
        with count_lock:
            call_count += 1
            count = call_count
        return "SELECT 1 FROM dual" if count == 1 else "SELECT 2 FROM dual"

    def judge(
        _question: str,
        _expected: str,
        generated: str,
        _profile: str,
        _analysis: object,
    ) -> QualityEvaluationJudge:
        return QualityEvaluationJudge(
            verdict=(
                QualityEvaluationVerdict.CORRECT
                if "SELECT 1" in generated
                else QualityEvaluationVerdict.INCORRECT
            ),
            confidence=0.8,
            summary="比較しました。",
        )

    service = _service(engine_runner=engine, judge_runner=judge)
    submitted = service.submit(
        profile_id="default",
        engines=[Nl2SqlEngine.ENTERPRISE_AI_DIRECT],
        repeat_count=2,
        content=_xlsx([["A", "質問", "SELECT 1 FROM dual"]]),
        filename="cases.xlsx",
    )
    service.run_job(job_id=submitted.job_id)
    summary = service.get_job(submitted.job_id).engine_summaries[0]
    assert summary.generation_success_rate == 1.0
    assert summary.correct == 1
    assert summary.incorrect == 1
    assert summary.normalized_sql_consistency == 0.5


def test_normalized_sql_consistency_preserves_literal_case_and_ignores_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_worker_mode", "external")
    call_count = 0
    count_lock = threading.Lock()

    def engine(*_args: object) -> str:
        nonlocal call_count
        with count_lock:
            call_count += 1
            count = call_count
        if count == 1:
            return "SELECT 1 FROM dual WHERE 'active' = 'active' -- generated comment"
        return "SELECT 1 FROM dual WHERE 'ACTIVE' = 'ACTIVE'"

    service = _service(engine_runner=engine, judge_runner=_judge)
    submitted = service.submit(
        profile_id="default",
        engines=[Nl2SqlEngine.ENTERPRISE_AI_DIRECT],
        repeat_count=2,
        content=_xlsx([["A", "質問", "SELECT 1 FROM dual"]]),
        filename="cases.xlsx",
    )
    service.run_job(job_id=submitted.job_id)

    results = service.list_results(job_id=submitted.job_id, limit=10).items
    normalized_values = [item.normalized_sql for item in results]
    assert "COMMENT" not in normalized_values[0]
    assert normalized_values[0] != normalized_values[1]
    assert service.get_job(submitted.job_id).engine_summaries[0].normalized_sql_consistency == 0.5


def test_worker_records_empty_engine_output_as_generation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_worker_mode", "external")
    service = _service(engine_runner=lambda *_args: "   ", judge_runner=_judge)
    submitted = service.submit(
        profile_id="default",
        engines=[Nl2SqlEngine.ENTERPRISE_AI_DIRECT],
        repeat_count=1,
        content=_xlsx([["A", "質問", "SELECT 1 FROM dual"]]),
        filename="cases.xlsx",
    )
    service.run_job(job_id=submitted.job_id)
    result = service.list_results(job_id=submitted.job_id, limit=1).items[0]
    assert result.generated_sql == ""
    assert "SQL を返しませんでした" in result.generation_error
    assert result.verdict == QualityEvaluationVerdict.NOT_ANALYZED
    assert service.get_job(submitted.job_id).status == QualityEvaluationStatus.COMPLETED_WITH_ERRORS


def test_default_judge_uses_profile_schema_catalog_without_argument_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_worker_mode", "external")
    nl2sql = Nl2SqlService(store=MemoryNl2SqlStore())
    nl2sql.import_sample_data(
        SampleDataMutationRequest(step=SampleDataStep.ALL, confirmation="ADMIN_EXECUTE")
    )
    _create_sample_profile(nl2sql)
    fake_client = _FakeEnterpriseAiClient(
        '{"verdict":"correct","confidence":0.91,"summary":"意味が一致します。",'
        '"differences":[],"risks":[],"correction_suggestion":""}'
    )
    nl2sql._enterprise_ai_client = fake_client  # noqa: SLF001
    service = QualityEvaluationService(
        nl2sql,
        repository=MemoryQualityEvaluationRepository(),
        engine_runner=lambda *_args: "SELECT EMPLOYEE_ID FROM EMPLOYEE",
    )

    submitted = service.submit(
        profile_id="sql_assist_sample",
        engines=[Nl2SqlEngine.ENTERPRISE_AI_DIRECT],
        repeat_count=1,
        content=_xlsx([["A", "社員IDを取得してください", "SELECT EMPLOYEE_ID FROM EMPLOYEE"]]),
        filename="cases.xlsx",
    )
    service.run_job(job_id=submitted.job_id)

    result = service.list_results(job_id=submitted.job_id, limit=1).items[0]
    assert result.judge_error == ""
    assert "_enterprise_ai_schema_context" not in result.judge_error
    assert result.verdict == QualityEvaluationVerdict.CORRECT
    assert fake_client.calls
    assert fake_client.calls[0]["timeout_seconds"] == 300.0
    assert fake_client.calls[0]["max_retries"] == 0
    assert "schema:" in fake_client.calls[0]["context"]
    assert "EMPLOYEE" in fake_client.calls[0]["context"]


def test_judge_accepts_case_variants_and_percent_confidence() -> None:
    judge = QualityEvaluationJudge.model_validate(
        {
            "verdict": "Correct",
            "confidence": 85,
            "summary": "一致しています。",
        }
    )

    assert judge.verdict == QualityEvaluationVerdict.CORRECT
    assert judge.confidence == 0.85


def test_strict_enterprise_ai_direct_generation_passes_timeout_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service.import_sample_data(
        SampleDataMutationRequest(step=SampleDataStep.ALL, confirmation="ADMIN_EXECUTE")
    )
    _create_sample_profile(service)
    fake_client = _FakeEnterpriseAiClient(
        '{"sql":"SELECT EMPLOYEE_ID FROM EMPLOYEE","explanation":"社員IDを取得します。"}'
    )
    service._enterprise_ai_client = fake_client  # noqa: SLF001
    monkeypatch.setattr(
        service,
        "quality_evaluation_engine_readiness",
        lambda profile_id=None: {Nl2SqlEngine.ENTERPRISE_AI_DIRECT: (True, "")},
    )
    monkeypatch.setattr(service, "_use_oracle_runtime", lambda: False)

    generated = service.generate_sql_strict_for_quality_evaluation(
        question="社員IDを取得してください",
        engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
        profile_id="sql_assist_sample",
        timeout_seconds=123.0,
    )

    assert generated.generated_sql == "SELECT EMPLOYEE_ID FROM EMPLOYEE"
    assert fake_client.calls[0]["timeout_seconds"] == 123.0


@pytest.mark.parametrize(
    "engine",
    [Nl2SqlEngine.ENTERPRISE_AI_DIRECT],
)
def test_strict_generation_never_uses_deterministic_fallback(
    engine: Nl2SqlEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service.import_sample_data(
        SampleDataMutationRequest(step=SampleDataStep.ALL, confirmation="ADMIN_EXECUTE")
    )
    monkeypatch.setattr(
        service,
        "quality_evaluation_engine_readiness",
        lambda profile_id=None: {engine: (True, "")},
    )
    monkeypatch.setattr(service, "_use_oracle_runtime", lambda: False)
    monkeypatch.setattr(service._enterprise_ai_client, "is_configured", lambda: False)
    with pytest.raises(RuntimeError):
        service.generate_sql_strict_for_quality_evaluation(
            question="社員一覧",
            engine=engine,
            profile_id="default",
        )


def test_memory_repository_reclaims_expired_lease_and_preserves_result_idempotency() -> None:
    repository = MemoryQualityEvaluationRepository()
    now = datetime.now(UTC)
    job = QualityEvaluationJobRecord(
        job_id="job-1",
        profile_id="default",
        profile_name="default",
        engines=[Nl2SqlEngine.ENTERPRISE_AI_DIRECT],
        repeat_count=1,
        cases=[
            QualityEvaluationCase(
                case_no=1,
                case_id="A",
                excel_row=2,
                question="q",
                expected_sql="SELECT 1 FROM dual",
            )
        ],
        status=QualityEvaluationStatus.RUNNING,
        total_attempts=1,
        lease_expires_at=(now - timedelta(seconds=1)).isoformat(),
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )
    repository.save_job(job)
    reclaimed = repository.claim_job(worker_id="new-worker", lease_seconds=60)
    assert reclaimed is not None
    assert reclaimed.worker_id == "new-worker"
    assert reclaimed.attempt_no == 1


def test_oracle_repository_materializes_quality_evaluation_lobs_before_connection_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_cursors: list[_QualityEvaluationOracleCursor] = []

    def configure_clob_fetch(cursor: _QualityEvaluationOracleCursor) -> None:
        configured_cursors.append(cursor)

    monkeypatch.setattr(
        "app.features.nl2sql.quality_evaluation_store.configure_clob_fetch_as_text",
        configure_clob_fetch,
    )
    now = datetime.now(UTC).isoformat()
    job = QualityEvaluationJobRecord(
        job_id="job-1",
        profile_id="default",
        profile_name="default",
        engines=[Nl2SqlEngine.ENTERPRISE_AI_DIRECT],
        repeat_count=1,
        cases=[
            QualityEvaluationCase(
                case_no=1,
                case_id="A",
                excel_row=2,
                question="q",
                expected_sql="SELECT 1 FROM dual",
            )
        ],
        total_attempts=1,
        created_at=now,
        updated_at=now,
    )
    result = QualityEvaluationResult(
        result_id="result-1",
        job_id=job.job_id,
        case_no=1,
        case_id="A",
        excel_row=2,
        question="q",
        expected_sql="SELECT 1 FROM dual",
        engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
        repetition_no=1,
        generated_sql="SELECT 1 FROM dual",
        normalized_sql="SELECT 1 FROM DUAL",
        verdict=QualityEvaluationVerdict.CORRECT,
        created_at=now,
    )
    job_payload = job.model_dump_json()
    result_payload = result.model_dump_json()
    factory = _QualityEvaluationOracleConnectionFactory(
        [
            [[(_LobPayload(job_payload),)]],
            [[(1,)], [(_LobPayload(job_payload),)]],
            [[(1,)], [(_LobPayload(result_payload),)]],
            [[(_LobPayload(result_payload),)]],
            [[("job-1", _LobPayload(job_payload))]],
        ]
    )
    repository = OracleQualityEvaluationRepository(connection_factory=factory)

    fetched = repository.get_job("job-1")
    jobs, total_jobs = repository.list_jobs(offset=0, limit=10)
    results, total_results = repository.list_results(job_id="job-1", offset=0, limit=10)
    all_results = repository.all_results("job-1")
    claimed = repository.claim_job(worker_id="worker-1", lease_seconds=60, job_id="job-1")

    assert fetched is not None
    assert fetched.job_id == "job-1"
    assert total_jobs == 1
    assert [item.job_id for item in jobs] == ["job-1"]
    assert total_results == 1
    assert [item.result_id for item in results] == ["result-1"]
    assert [item.result_id for item in all_results] == ["result-1"]
    assert claimed is not None
    assert claimed.worker_id == "worker-1"
    assert claimed.status == QualityEvaluationStatus.RUNNING
    assert len(configured_cursors) == 5
    claim_cursor = factory.connections[-1].cursors[0]
    claim_sql = claim_cursor.executed[0][0].upper()
    assert "FOR UPDATE SKIP LOCKED" in claim_sql
    assert "FETCH FIRST" not in claim_sql
    assert claim_cursor.prefetchrows == 0
    assert claim_cursor.arraysize == 1
    assert all(connection.closed for connection in factory.connections)


def test_oracle_repository_list_jobs_filters_profile_ids_before_paging() -> None:
    job_payload = _evaluation_job("job-allowed", profile_id="allowed").model_dump_json()
    factory = _QualityEvaluationOracleConnectionFactory(
        [
            [[(1,)], [(_LobPayload(job_payload),)]],
        ]
    )
    repository = OracleQualityEvaluationRepository(connection_factory=factory)

    jobs, total_jobs = repository.list_jobs(
        offset=10,
        limit=5,
        profile_ids={"allowed", "default"},
    )

    cursor = factory.connections[0].cursors[0]
    count_sql, count_binds = cursor.executed[0]
    page_sql, page_binds = cursor.executed[1]
    expected_predicate = (
        "COALESCE(NULLIF(PROFILE_ID, ''), 'default') IN (:profile_id_0, :profile_id_1)"
    )
    assert total_jobs == 1
    assert [item.job_id for item in jobs] == ["job-allowed"]
    assert expected_predicate in count_sql
    assert expected_predicate in page_sql
    assert count_binds == {"profile_id_0": "allowed", "profile_id_1": "default"}
    assert page_binds == {
        "profile_id_0": "allowed",
        "profile_id_1": "default",
        "offset": 10,
        "limit": 5,
    }


def test_oracle_repository_delete_job_uses_job_delete_and_transaction() -> None:
    job = _evaluation_job("job-1", status=QualityEvaluationStatus.COMPLETED)
    factory = _QualityEvaluationOracleConnectionFactory(
        [
            [[(_LobPayload(job.model_dump_json()),)]],
        ]
    )
    repository = OracleQualityEvaluationRepository(connection_factory=factory)

    deleted = repository.delete_job("job-1")

    assert deleted is not None
    assert deleted.job_id == "job-1"
    connection = factory.connections[0]
    assert connection.commits == 1
    assert connection.rollbacks == 0
    executed_sql = [sql for cursor in connection.cursors for sql, _binds in cursor.executed]
    assert any(
        "DELETE FROM NL2SQL_EVALUATION_JOBS WHERE JOB_ID = :job_id" in sql for sql in executed_sql
    )


def test_oracle_repository_delete_job_rolls_back_on_delete_error() -> None:
    class _FailingDeleteCursor(_QualityEvaluationOracleCursor):
        def execute(self, sql: str, binds: Any = None) -> None:
            if sql.lstrip().upper().startswith("DELETE"):
                self.executed.append((sql, binds))
                raise RuntimeError("delete failed")
            super().execute(sql, binds)

    class _FailingDeleteConnection(_QualityEvaluationOracleConnection):
        def cursor(self) -> _QualityEvaluationOracleCursor:
            cursor = _FailingDeleteCursor(self, self._result_sets)
            self.cursors.append(cursor)
            return cursor

    class _FailingDeleteConnectionFactory(_QualityEvaluationOracleConnectionFactory):
        def __call__(self) -> _QualityEvaluationOracleConnection:
            if not self._calls:
                raise AssertionError("scripted connection is missing")
            connection = _FailingDeleteConnection(self._calls.pop(0))
            self.connections.append(connection)
            return connection

    job = _evaluation_job("job-1", status=QualityEvaluationStatus.COMPLETED)
    factory = _FailingDeleteConnectionFactory(
        [
            [[(_LobPayload(job.model_dump_json()),)]],
        ]
    )
    repository = OracleQualityEvaluationRepository(connection_factory=factory)

    with pytest.raises(RuntimeError, match="delete failed"):
        repository.delete_job("job-1")

    connection = factory.connections[0]
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_results_workbook_preserves_sql_text_and_strips_illegal_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_worker_mode", "external")

    def judge(*_args: object) -> QualityEvaluationJudge:
        return QualityEvaluationJudge(
            verdict="Correct",
            confidence=85,
            summary="-judge\x0bsummary",
            correction_suggestion="=1+1",
        )

    service = _service(
        engine_runner=lambda *_args: "-- generated\x0b comment\nSELECT 1 FROM dual",
        judge_runner=judge,
    )
    submitted = service.submit(
        profile_id="default",
        engines=[Nl2SqlEngine.ENTERPRISE_AI_DIRECT],
        repeat_count=1,
        content=_xlsx([["+CASE", "+question", "-- expected comment\nSELECT 1 FROM dual"]]),
        filename="cases.xlsx",
    )
    service.run_job(job_id=submitted.job_id)
    filename, content = service.results_workbook(submitted.job_id)
    workbook = load_workbook(io.BytesIO(content))
    details = workbook["評価結果"]
    headers = [cell.value for cell in details[1]]
    assert filename.startswith("nl2sql_quality_evaluation_")
    assert workbook.sheetnames == ["概要", "評価結果"]
    assert headers[-2:] == ["人手判定", "人手コメント"]
    assert details.freeze_panes == "A2"
    assert details.auto_filter.ref is not None
    assert details.cell(2, 2).value == "+CASE"
    assert details.cell(2, 4).value == "+question"
    assert details.cell(2, 5).value == "-- expected comment\nSELECT 1 FROM dual"
    assert details.cell(2, 8).value == "-- generated comment\nSELECT 1 FROM dual"
    assert details.cell(2, 20).value == "-judgesummary"
    assert details.cell(2, 23).value == "=1+1"
    assert details.cell(2, 23).data_type == "s"


@pytest.mark.asyncio
async def test_quality_evaluation_http_statuses_for_missing_workbook_and_bad_paging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = MemoryQualityEvaluationRepository()
    repository.save_job(_evaluation_job("completed-job", status=QualityEvaluationStatus.COMPLETED))
    service = QualityEvaluationService(
        Nl2SqlService(store=MemoryNl2SqlStore()),
        repository=repository,
        engine_runner=lambda *_args: "SELECT 1 FROM dual",
        judge_runner=_judge,
    )
    monkeypatch.setattr(get_settings(), "auth_mode", "local")
    monkeypatch.setattr(nl2sql_router_module, "quality_evaluation_service", service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing_workbook = await client.get(
            "/api/nl2sql/quality-evaluations/missing-job/results.xlsx"
        )
        bad_paging = await client.get(
            "/api/nl2sql/quality-evaluations/completed-job/results",
            params={"limit": "101"},
        )
        paged = await client.get(
            "/api/nl2sql/quality-evaluations/completed-job/results",
            params={"limit": "100", "offset": "3"},
        )
        completed_workbook = await client.get(
            "/api/nl2sql/quality-evaluations/completed-job/results.xlsx"
        )

    assert missing_workbook.status_code == 404
    assert bad_paging.status_code == 422
    assert paged.status_code == 200
    assert {key: paged.json()["data"][key] for key in ("limit", "offset", "has_next")} == {
        "limit": 100,
        "offset": 3,
        "has_next": False,
    }
    assert completed_workbook.status_code == 200


def test_result_and_job_pagination_use_offsets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_quality_evaluation_worker_mode", "external")
    service = _service(
        engine_runner=lambda *_args: "SELECT 1 FROM dual",
        judge_runner=_judge,
    )
    first = service.submit(
        profile_id="default",
        engines=[Nl2SqlEngine.ENTERPRISE_AI_DIRECT],
        repeat_count=2,
        content=_xlsx([["A", "質問", "SELECT 1 FROM dual"]]),
        filename="cases.xlsx",
    )
    service.submit(
        profile_id="default",
        engines=[Nl2SqlEngine.ENTERPRISE_AI_DIRECT],
        repeat_count=1,
        content=_xlsx([["B", "質問", "SELECT 1 FROM dual"]]),
        filename="cases.xlsx",
    )
    service.run_job(job_id=first.job_id)

    jobs_page = service.list_jobs(limit=1)
    results_page = service.list_results(job_id=first.job_id, limit=1)
    assert len(jobs_page.items) == 1
    assert (jobs_page.total, jobs_page.has_next) == (2, True)
    last_jobs = service.list_jobs(offset=1, limit=1)
    assert len(last_jobs.items) == 1
    assert last_jobs.has_next is False
    assert last_jobs.items[0].job_id != jobs_page.items[0].job_id
    assert len(results_page.items) == 1
    assert (results_page.total, results_page.has_next) == (2, True)
    last_results = service.list_results(job_id=first.job_id, offset=1, limit=1)
    assert len(last_results.items) == 1
    assert last_results.has_next is False


def test_openapi_exposes_new_quality_flow_and_removes_legacy_evaluation_routes() -> None:
    paths = app.openapi()["paths"]
    expected = {
        "/api/nl2sql/quality-evaluations/capabilities",
        "/api/nl2sql/quality-evaluations/template.xlsx",
        "/api/nl2sql/quality-evaluations",
        "/api/nl2sql/quality-evaluations/{job_id}",
        "/api/nl2sql/quality-evaluations/{job_id}/cancel",
        "/api/nl2sql/quality-evaluations/{job_id}/results",
        "/api/nl2sql/quality-evaluations/{job_id}/results.xlsx",
    }
    assert expected <= set(paths)
    assert "post" in paths["/api/nl2sql/quality-evaluations/{job_id}/cancel"]
    assert "delete" in paths["/api/nl2sql/quality-evaluations/{job_id}"]
    assert "/api/nl2sql/analyze" in paths
    assert "/api/nl2sql/repair" not in paths
    assert "/api/nl2sql/reverse" in paths
    removed = {
        # 生成方式の選択・Select AI の資産・合成データは削除した（#7）。
        "/api/nl2sql/engines",
        "/api/nl2sql/synthetic-data/generate",
        "/api/nl2sql/synthetic-data/runs",
        "/api/nl2sql/oracle-sync-jobs/{job_id}",
        "/api/nl2sql/evaluate",
        "/api/nl2sql/evaluation-sets",
        "/api/nl2sql/evaluation-runs",
        "/api/nl2sql/compare",
        "/api/nl2sql/synthetic-cases",
        "/api/nl2sql/synthetic-data/operations/{operation_id}",
    }
    assert removed.isdisjoint(paths)


def test_capabilities_expose_only_enterprise_ai_direct() -> None:
    capabilities = _service().capabilities()

    assert [item.engine for item in capabilities.engines] == [Nl2SqlEngine.ENTERPRISE_AI_DIRECT]


def test_stored_records_with_removed_engine_values_are_read_as_enterprise_ai_direct() -> None:
    """削除した生成方式（#7）で保存した評価の記録も、唯一の方式として読み戻せる。"""

    job = _evaluation_job("legacy-job").model_dump(mode="json")
    job["engines"] = ["select_ai", "select_ai_agent"]
    result = _evaluation_result("legacy-job").model_dump(mode="json")
    result["engine"] = "select_ai_agent"

    restored_job = QualityEvaluationJobRecord.model_validate(job)
    restored_result = QualityEvaluationResult.model_validate(result)

    assert restored_job.engines == [
        Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
        Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
    ]
    assert restored_result.engine == Nl2SqlEngine.ENTERPRISE_AI_DIRECT
