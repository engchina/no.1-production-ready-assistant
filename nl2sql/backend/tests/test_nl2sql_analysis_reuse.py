"""SQL 解析(analyze_sql)の重複実行を防ぐ回帰テスト。

1 回の job で同じ SQL を safety_check と execute_sql で二重に sqlglot 解析していた
(Issue: analyze_sql 二重実行 / _optimization_hints 二重呼び出し)。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from app.features.nl2sql.incremental_store import MemoryIncrementalNl2SqlRepository
from app.features.nl2sql.models import (
    AllowedObjects,
    JobCreateRequest,
    JobStatus,
    Nl2SqlEngine,
    Nl2SqlProfile,
    SafetyReport,
    SchemaCatalog,
    SchemaColumn,
    SchemaTable,
)
from app.features.nl2sql.service import Nl2SqlService
from app.features.nl2sql.store import MemoryNl2SqlStore

_PROFILE_ID = "orders-profile"
_SQL = "SELECT ID FROM APP.ORDERS"


class _FakeEnterpriseAiClient:
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
        del prompt, context, system_prompt, timeout_seconds, max_output_tokens, max_retries
        return '{"sql":"SELECT ID FROM APP.ORDERS","explanation":"注文 ID を取得します。"}'


def _service() -> Nl2SqlService:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    catalog = SchemaCatalog(
        refreshed_at="2026-09-01T00:00:00+00:00",
        schema_fingerprint="analysis-reuse-v1",
        current_owner="APP",
        tables=[
            SchemaTable(
                owner="APP",
                table_name="ORDERS",
                logical_name="注文",
                columns=[
                    SchemaColumn(
                        column_name="ID", logical_name="ID", data_type="NUMBER", nullable=False
                    ),
                    SchemaColumn(
                        column_name="ORDER_NAME", logical_name="注文名", data_type="VARCHAR2"
                    ),
                    SchemaColumn(column_name="AMOUNT", logical_name="金額", data_type="NUMBER"),
                    SchemaColumn(column_name="CREATED_AT", logical_name="作成日", data_type="DATE"),
                ],
            )
        ],
    )
    manifest = {
        (table.owner.upper(), table.table_name.upper()): catalog.refreshed_at
        for table in catalog.tables
    }
    repository.apply_schema_refresh(
        catalog=catalog,
        manifest=manifest,
        changed_keys=set(manifest),
        deleted_keys=set(),
    )
    repository.save_profile(
        Nl2SqlProfile(id=_PROFILE_ID, name="注文管理", allowed_tables=["APP.ORDERS"]),
        expected_etag=None,
    )
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._incremental_repository = repository  # noqa: SLF001 - white-box contract test
    service._refresh_job_repository = repository  # noqa: SLF001
    service._persistence_ready = True  # noqa: SLF001
    service._persistence_writable = True  # noqa: SLF001
    service._cache_token_poll_seconds = 0.0  # noqa: SLF001
    service._catalog = repository.load_catalog()  # noqa: SLF001
    service._enterprise_ai_client = _FakeEnterpriseAiClient()  # noqa: SLF001
    return service


def _spy(monkeypatch: pytest.MonkeyPatch, target: Any, name: str) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []
    original = getattr(target, name)

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, wrapper)
    return calls


def test_job_analyzes_generated_sql_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    analyze_calls = _spy(monkeypatch, service, "analyze_sql")

    created = service.start_job(
        JobCreateRequest(
            question="注文一覧を確認したい",
            engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
            profile_id=_PROFILE_ID,
        ),
        actor_user_uuid="user-1",
        actor_is_system_admin=True,
    )
    job = None
    for _ in range(200):
        job = service.get_job(created.job_id)
        if job is not None and job.status in {JobStatus.DONE, JobStatus.ERROR}:
            break
        time.sleep(0.01)

    assert job is not None
    assert job.status == JobStatus.DONE, job.error_message
    assert job.result is not None
    assert job.result.results.total > 0
    # 旧実装は safety_check と execute_sql で 2 回解析していた。
    assert len(analyze_calls) == 1


def test_execute_sql_reuses_provided_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    allowed = AllowedObjects(table_names=["APP.ORDERS"], enforce_table_scope=True)
    precomputed = service.analyze_sql(_SQL, allowed, 10)
    analyze_calls = _spy(monkeypatch, service, "analyze_sql")

    safety, executable, results = service.execute_sql(_SQL, allowed, 10, analysis=precomputed)

    assert analyze_calls == []
    assert safety is precomputed.safety
    assert executable == _SQL
    assert results.total > 0


def test_execute_sql_honours_blocked_analysis_without_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service()
    allowed = AllowedObjects(table_names=["APP.ORDERS"], enforce_table_scope=True)
    blocked = service.analyze_sql(_SQL, allowed, 10).model_copy(
        update={
            "safety": SafetyReport(
                is_safe=False,
                is_select_only=True,
                row_limit_applied=10,
                blocked_reason="テスト用の拒否理由",
            )
        }
    )
    execute_calls = _spy(monkeypatch, service, "_mock_execute")

    safety, _, results = service.execute_sql(_SQL, allowed, 10, analysis=blocked)

    assert safety.is_safe is False
    assert safety.blocked_reason == "テスト用の拒否理由"
    assert results.total == 0
    assert execute_calls == []


def test_execute_sql_without_analysis_still_analyzes(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    allowed = AllowedObjects(table_names=["APP.ORDERS"], enforce_table_scope=True)
    analyze_calls = _spy(monkeypatch, service, "analyze_sql")

    safety, _, _ = service.execute_sql(_SQL, allowed, 10)

    assert safety.is_safe is True
    assert len(analyze_calls) == 1


def test_analyze_sql_computes_optimization_hints_once(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    hint_calls = _spy(monkeypatch, service, "_optimization_hints")

    analysis = service.analyze_sql(_SQL, AllowedObjects(), 10)

    assert len(hint_calls) == 1
    assert set(analysis.optimization_hints) <= set(analysis.risk_findings)
