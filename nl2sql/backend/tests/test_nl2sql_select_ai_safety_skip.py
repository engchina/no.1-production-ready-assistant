"""生成の方式ごとの SQL の安全確認（#1523）。

- Select AI / Select AI Agent が生成した SQL は既定で SELECT 文なので、安全確認を一切しない
  （SELECT だけか・危険な関数・bind の placeholder・未承認の関数・他製品のシステムの表・
  業務プロファイルの表と列の範囲・LLM の解析の補強）。ジョブの工程 `safety_check` は「省略」
  （`skipped`。補足に `safety_check_skipped`）として記録する。
- LLM（enterprise_ai_direct）と、生成の方式の無い経路（利用者の SQL・SQL の解析の画面）は、
  従来どおりすべてを確かめる。
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from openpyxl import Workbook  # type: ignore[import-untyped]
from pr_backend_core.chat_progress import fold_chat_progress_events
from test_nl2sql_job_runtime import _FakeEnterpriseAiClient, _repository, _request, _worker
from trace_span_fake import TraceTable, trace_table  # noqa: F401 - fixture

from app.features.mcp.tools import _job_result
from app.features.nl2sql.models import (
    AllowedObjects,
    JobCreateRequest,
    JobData,
    JobStatus,
    JobStepStatus,
    Nl2SqlEngine,
)
from app.features.nl2sql.quality_evaluation_models import (
    QualityEvaluationDeterministicAnalysis,
    QualityEvaluationJudge,
    QualityEvaluationVerdict,
)
from app.features.nl2sql.quality_evaluation_service import (
    QualityEvaluationService,
    _select_only_label,
)
from app.features.nl2sql.quality_evaluation_store import MemoryQualityEvaluationRepository
from app.features.nl2sql.service import (
    SELECT_AI_SAFETY_CHECK_SKIP_REASON,
    GeneratedSql,
    Nl2SqlService,
    engine_skips_safety_check,
)
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.settings import get_settings

_SELECT_AI_ENGINES = [Nl2SqlEngine.SELECT_AI, Nl2SqlEngine.SELECT_AI_AGENT]
_CHECKED_ENGINES = [Nl2SqlEngine.ENTERPRISE_AI_DIRECT, None]
_ORDERS = AllowedObjects(table_names=["APP.ORDERS", "DUAL"])
_DANGEROUS_SQL = "SELECT DBMS_XMLGEN.GETXML('SELECT * FROM APP.SALARY') FROM APP.ORDERS"
_BIND_SQL = "SELECT ID FROM APP.ORDERS WHERE ID = :order_id"
_UNAPPROVED_FUNCTION_SQL = "SELECT APP.FMT_CODE(ID) FROM APP.ORDERS"


def _engine_id(engine: Nl2SqlEngine | None) -> str:
    return engine.value if engine is not None else "no-engine"


@pytest.fixture
def service() -> Nl2SqlService:
    return Nl2SqlService(store=MemoryNl2SqlStore())


def test_only_select_ai_engines_skip_the_safety_check() -> None:
    assert [engine_skips_safety_check(engine) for engine in Nl2SqlEngine] == [
        engine in _SELECT_AI_ENGINES for engine in Nl2SqlEngine
    ]
    assert engine_skips_safety_check(None) is False


# --- analyze_sql: SQL の形の確認 ----------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [_DANGEROUS_SQL, _BIND_SQL, _UNAPPROVED_FUNCTION_SQL],
    ids=["dangerous-function", "bind-placeholder", "unapproved-function"],
)
@pytest.mark.parametrize("engine", _SELECT_AI_ENGINES, ids=_engine_id)
def test_select_ai_sql_skips_the_safety_check(
    service: Nl2SqlService, engine: Nl2SqlEngine, sql: str
) -> None:
    analysis = service.analyze_sql(sql, _ORDERS, 100, engine=engine)

    assert analysis.safety.is_safe is True, analysis.safety.blocked_reason
    assert analysis.safety.safety_checked is False
    assert analysis.safety.safety_check_skip_reason == SELECT_AI_SAFETY_CHECK_SKIP_REASON
    # Select AI の SQL は SELECT 文として扱い、実行できる SQL を返す。
    assert analysis.safety.is_select_only is True
    assert analysis.executable_sql == sql


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        (_DANGEROUS_SQL, "危険な Oracle 関数"),
        (_BIND_SQL, ":order_id"),
        (_UNAPPROVED_FUNCTION_SQL, "APP.FMT_CODE"),
    ],
    ids=["dangerous-function", "bind-placeholder", "unapproved-function"],
)
@pytest.mark.parametrize("engine", _CHECKED_ENGINES, ids=_engine_id)
def test_llm_and_user_sql_keep_the_safety_check(
    service: Nl2SqlService, engine: Nl2SqlEngine | None, sql: str, reason: str
) -> None:
    analysis = service.analyze_sql(sql, _ORDERS, 100, engine=engine)

    assert analysis.safety.is_safe is False
    assert reason in analysis.safety.blocked_reason
    assert analysis.safety.safety_checked is True
    assert analysis.safety.safety_check_skip_reason == ""


@pytest.mark.parametrize("engine", _CHECKED_ENGINES, ids=_engine_id)
def test_llm_and_user_sql_block_non_select_statements(
    service: Nl2SqlService, engine: Nl2SqlEngine | None
) -> None:
    analysis = service.analyze_sql("DELETE FROM APP.ORDERS", _ORDERS, 100, engine=engine)

    assert analysis.safety.is_safe is False
    assert analysis.safety.is_select_only is False
    assert analysis.executable_sql == ""


@pytest.mark.parametrize("engine", _SELECT_AI_ENGINES, ids=_engine_id)
def test_select_ai_sql_does_not_call_the_llm_analysis(
    service: Nl2SqlService, engine: Nl2SqlEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Select AI の SQL に LLM の解析の補強を掛けない")

    monkeypatch.setattr(service, "_enhance_sql_analysis_with_llm", fail)

    analysis = service.analyze_sql(
        "SELECT ID FROM APP.ORDERS", _ORDERS, 100, use_llm=True, engine=engine
    )

    assert analysis.safety.is_safe is True


# --- analyze_sql: 他製品のシステムの表・業務プロファイルの表と列の範囲 ------------------


@pytest.mark.parametrize(
    ("sql", "allowed", "reason"),
    [
        (
            "SELECT PASSWORD_HASH FROM PLATFORM_USERS",
            AllowedObjects(),
            "PLATFORM_USERS",
        ),
        (
            "SELECT ID FROM APP.SALARY",
            AllowedObjects(table_names=["APP.ORDERS"], enforce_table_scope=True),
            "許可されていない表",
        ),
        (
            "SELECT AMOUNT FROM APP.ORDERS",
            AllowedObjects(
                table_names=["APP.ORDERS"],
                columns={"APP.ORDERS": ["ID"]},
                enforce_table_scope=True,
            ),
            "許可されていない列",
        ),
    ],
    ids=["system-table", "table-scope", "column-scope"],
)
@pytest.mark.parametrize("engine", _CHECKED_ENGINES, ids=_engine_id)
def test_llm_and_user_sql_keep_the_access_check(
    service: Nl2SqlService,
    engine: Nl2SqlEngine | None,
    sql: str,
    allowed: AllowedObjects,
    reason: str,
) -> None:
    analysis = service.analyze_sql(sql, allowed, 100, engine=engine)

    assert analysis.safety.is_safe is False
    assert reason in analysis.safety.blocked_reason


@pytest.mark.parametrize(
    ("sql", "allowed"),
    [
        ("SELECT PASSWORD_HASH FROM PLATFORM_USERS", AllowedObjects()),
        (
            "SELECT ID FROM APP.SALARY",
            AllowedObjects(table_names=["APP.ORDERS"], enforce_table_scope=True),
        ),
        (
            "SELECT AMOUNT FROM APP.ORDERS",
            AllowedObjects(
                table_names=["APP.ORDERS"],
                columns={"APP.ORDERS": ["ID"]},
                enforce_table_scope=True,
            ),
        ),
    ],
    ids=["system-table", "table-scope", "column-scope"],
)
@pytest.mark.parametrize("engine", _SELECT_AI_ENGINES, ids=_engine_id)
def test_select_ai_sql_skips_the_access_check(
    service: Nl2SqlService, engine: Nl2SqlEngine, sql: str, allowed: AllowedObjects
) -> None:
    # ユーザーの決定（#1523）: Select AI / Agent では表・列の範囲も照合しない（業務プロファイルの
    # 列の制限は、Select AI の profile の object_list が表の単位のため効かない）。
    analysis = service.analyze_sql(sql, allowed, 100, engine=engine)

    assert analysis.safety.is_safe is True
    assert analysis.safety.safety_checked is False
    # 確かめなかった SQL に修正案を出さない。
    assert analysis.repaired_sql == ""


@pytest.mark.parametrize("engine", _SELECT_AI_ENGINES, ids=_engine_id)
def test_select_ai_sql_is_not_blocked_even_if_it_cannot_be_parsed(
    service: Nl2SqlService, engine: Nl2SqlEngine
) -> None:
    analysis = service.analyze_sql("SELECT FROM WHERE", _ORDERS, 100, engine=engine)

    assert analysis.safety.is_safe is True
    assert analysis.safety.blocked_reason == ""


# --- ジョブの工程の記録 ----------------------------------------------------------------------


@pytest.fixture
def worker_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")


def _run(service: Nl2SqlService, request: JobCreateRequest) -> JobData:
    created = service.start_job(request, actor_user_uuid="user-1", actor_is_system_admin=True)
    assert service.run_next_nl2sql_job(worker_id="safety-skip-test", job_id=created.job_id)
    job = service.get_job(created.job_id, actor_user_uuid="user-1")
    assert job is not None
    return job


def _generated_by(
    service: Nl2SqlService, monkeypatch: pytest.MonkeyPatch, engine: Nl2SqlEngine, sql: str
) -> None:
    def generate(*_args: object, **_kwargs: object) -> GeneratedSql:
        return GeneratedSql(engine=engine, generated_sql=sql, explanation="", engine_meta={})

    monkeypatch.setattr(service, "_generate_selected_engine", generate)


def _step_status(job: JobData, stage: str) -> JobStepStatus:
    return next(step.status for step in job.steps if step.stage == stage)


def _progress(
    service: Nl2SqlService, table: TraceTable, job: JobData, kind: str
) -> tuple[str, dict[str, Any]]:
    """処理の経過の工程の span（`kind` が段階の id。#3226 の案 A）の状態と補足の値。"""

    table.flush()
    progress = service.get_job_trace(job.job_id, actor_user_uuid="user-1")
    assert progress is not None
    fold = fold_chat_progress_events(progress.trace.events)
    [step] = [item for item in fold.step_list() if item.kind == kind]
    return step.status, dict(step.params or {})


@pytest.mark.usefixtures("worker_mode")
@pytest.mark.parametrize("engine", _SELECT_AI_ENGINES, ids=_engine_id)
@pytest.mark.parametrize("chat", [False, True], ids=["sql-generation", "chat"])
def test_select_ai_job_records_the_safety_check_as_skipped(
    monkeypatch: pytest.MonkeyPatch,
    engine: Nl2SqlEngine,
    chat: bool,
    trace_table: TraceTable,  # noqa: F811
) -> None:
    service = _worker(_repository())
    _generated_by(service, monkeypatch, engine, _DANGEROUS_SQL)

    job = _run(service, _request().model_copy(update={"engine": engine, "chat": chat}))

    assert job.status == JobStatus.DONE, job.error_message
    assert _step_status(job, "safety_check") == JobStepStatus.SKIPPED
    assert _step_status(job, "execute_sql") == JobStepStatus.DONE
    assert job.result is not None
    assert job.result.safety.is_safe is True
    assert job.result.safety.safety_checked is False
    # 処理の段階のイベント: 省略の印と参照した表（画面が「省略」と表を出す）。
    assert _progress(service, trace_table, job, "safety_check") == (
        "skipped",
        {"tables": "APP.ORDERS", "table_count": 1, "safety_check_skipped": True},
    )
    # MCP（nl2sql_query）の応答にも省略を出す。
    output = _job_result(job)
    assert output.is_safe is True
    assert output.safety_check == "skipped"


@pytest.mark.usefixtures("worker_mode")
def test_select_ai_job_without_generation_stub_skips_the_safety_check() -> None:
    # deterministic runtime の Select AI（生成の差し替え無し）でも、生成の方式で省略する。
    service = _worker(_repository())

    job = _run(service, _request().model_copy(update={"engine": Nl2SqlEngine.SELECT_AI}))

    assert job.status == JobStatus.DONE, job.error_message
    assert _step_status(job, "safety_check") == JobStepStatus.SKIPPED


@pytest.mark.usefixtures("worker_mode")
@pytest.mark.parametrize("engine", _SELECT_AI_ENGINES, ids=_engine_id)
def test_select_ai_job_does_not_check_the_table_scope(
    monkeypatch: pytest.MonkeyPatch, engine: Nl2SqlEngine
) -> None:
    service = _worker(_repository())
    _generated_by(service, monkeypatch, engine, "SELECT ID FROM APP.SALARY")

    job = _run(service, _request().model_copy(update={"engine": engine}))

    # 安全確認は省略し、実行まで進む（実行の結果は deterministic runtime の擬似の実行）。
    assert _step_status(job, "safety_check") == JobStepStatus.SKIPPED
    assert job.result is not None
    assert job.result.safety.is_safe is True
    assert job.result.safety.blocked_reason == ""


@pytest.mark.usefixtures("worker_mode")
def test_enterprise_ai_direct_job_still_blocks_the_table_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _worker(_repository())
    _generated_by(
        service, monkeypatch, Nl2SqlEngine.ENTERPRISE_AI_DIRECT, "SELECT ID FROM APP.SALARY"
    )

    job = _run(service, _request())

    assert job.status == JobStatus.ERROR
    assert _step_status(job, "safety_check") == JobStepStatus.ERROR
    assert _step_status(job, "execute_sql") == JobStepStatus.SKIPPED
    assert job.result is not None
    assert "許可されていない表" in job.result.safety.blocked_reason


@pytest.mark.usefixtures("worker_mode")
def test_enterprise_ai_direct_job_still_blocks_dangerous_sql() -> None:
    service = _worker(
        _repository(),
        _FakeEnterpriseAiClient(
            '{"sql":"SELECT DBMS_XMLGEN.GETXML(\'SELECT 1 FROM DUAL\') FROM APP.ORDERS",'
            '"explanation":"取得します。"}'
        ),
    )

    job = _run(service, _request())

    assert job.status == JobStatus.ERROR
    assert _step_status(job, "safety_check") == JobStepStatus.ERROR
    assert _step_status(job, "execute_sql") == JobStepStatus.SKIPPED
    assert job.result is not None
    assert "危険な Oracle 関数" in job.result.safety.blocked_reason
    assert job.result.safety.safety_checked is True
    assert _job_result(job).safety_check == "checked"


@pytest.mark.usefixtures("worker_mode")
def test_enterprise_ai_direct_job_records_the_safety_check_as_done(
    trace_table: TraceTable,  # noqa: F811
) -> None:
    service = _worker(_repository())

    job = _run(service, _request())

    assert job.status == JobStatus.DONE, job.error_message
    assert _step_status(job, "safety_check") == JobStepStatus.DONE
    assert _progress(service, trace_table, job, "safety_check") == (
        "done",
        {"tables": "APP.ORDERS", "table_count": 1},
    )


# --- 品質評価の Excel の欄 -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("analysis", "label"),
    [
        (QualityEvaluationDeterministicAnalysis(is_select_only=True), "OK"),
        (QualityEvaluationDeterministicAnalysis(is_select_only=False), "NG"),
        (
            QualityEvaluationDeterministicAnalysis(is_select_only=True, safety_checked=False),
            "省略",
        ),
    ],
    ids=["checked-ok", "checked-ng", "skipped"],
)
def test_quality_evaluation_select_only_label(
    analysis: QualityEvaluationDeterministicAnalysis, label: str
) -> None:
    assert _select_only_label(analysis) == label


def _cases_xlsx() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["ケースID", "質問", "期待SQL"])
    sheet.append(["A", "q", "SELECT 1 FROM dual"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_quality_evaluation_analysis_follows_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """採点に渡す決定論の解析は生成の方式ごと。Select AI は安全確認をしない。"""

    def engine(_question: str, _selected: Nl2SqlEngine, _profile: str) -> str:
        return "SELECT DBMS_XMLGEN.GETXML('SELECT 1 FROM DUAL') FROM DUAL"

    def judge(*_args: object) -> QualityEvaluationJudge:
        return QualityEvaluationJudge(
            verdict=QualityEvaluationVerdict.CORRECT, confidence=0.9, summary="一致します。"
        )

    monkeypatch.setattr(get_settings(), "nl2sql_quality_evaluation_worker_mode", "external")
    service = QualityEvaluationService(
        Nl2SqlService(store=MemoryNl2SqlStore()),
        repository=MemoryQualityEvaluationRepository(),
        engine_runner=engine,
        judge_runner=judge,
    )
    submitted = service.submit(
        profile_id="default",
        engines=[Nl2SqlEngine.SELECT_AI, Nl2SqlEngine.ENTERPRISE_AI_DIRECT],
        repeat_count=1,
        content=_cases_xlsx(),
        filename="cases.xlsx",
    )
    service.run_job(job_id=submitted.job_id, worker_id="safety-skip-test")

    page = service.list_results(job_id=submitted.job_id, limit=100)
    by_engine = {item.engine: item.deterministic_analysis for item in page.items}
    assert by_engine[Nl2SqlEngine.SELECT_AI].safety_checked is False
    assert by_engine[Nl2SqlEngine.SELECT_AI].is_safe is True
    assert by_engine[Nl2SqlEngine.ENTERPRISE_AI_DIRECT].safety_checked is True
    assert by_engine[Nl2SqlEngine.ENTERPRISE_AI_DIRECT].is_safe is False
