"""業務別サンプルの API、対象分離、SQL の関係整合性を検証する。"""

import sqlite3
from unittest.mock import MagicMock

import httpx
import pytest
import sqlglot

from app.features.nl2sql import router as nl2sql_router
from app.features.nl2sql.incremental_store import MemoryIncrementalNl2SqlRepository
from app.features.nl2sql.models import (
    Nl2SqlProfile,
    SampleDataMutationRequest,
    SampleDataset,
    SampleDataStep,
    SchemaCatalog,
    SchemaColumn,
    SchemaTable,
)
from app.features.nl2sql.sample_datasets import SAMPLE_DATA_CONFIRMATION, SAMPLE_DATASETS
from app.features.nl2sql.service import Nl2SqlService, SchemaRefreshMutationSync
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.main import app
from app.security.service import SecurityApiError
from app.settings import get_settings

_LEGACY_CONFIRMATIONS = ("SQL_ASSIST_SAMPLE", "NL2SQL_SALES_SAMPLE", "NL2SQL_INQUIRY_SAMPLE")


def make_service() -> Nl2SqlService:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._catalog = SchemaCatalog(
        current_owner="APP", tables=[], refreshed_at="2026-09-13T00:00:00Z"
    )
    return service


def sample_catalog(
    service: Nl2SqlService, dataset: SampleDataset, *, prefix: str = ""
) -> SchemaCatalog:
    """サンプル定義の DDL と同じ種類・列構成のオブジェクトを持つ Oracle catalog。"""

    objects = [
        *service._sample_tables_from_ddl(dataset),
        *service._sample_views_from_ddl(dataset),
    ]
    return SchemaCatalog(
        current_owner="APP",
        refreshed_at="2026-09-13T00:00:00Z",
        tables=[
            item.model_copy(update={"owner": "APP", "table_name": f"{prefix}{item.table_name}"})
            for item in objects
        ],
    )


@pytest.mark.parametrize("dataset", list(SampleDataset))
async def test_api_dataset_preview_import_and_delete(
    dataset: SampleDataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = make_service()
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)
    definition = SAMPLE_DATASETS[dataset]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        # HR は既存 caller と同様に dataset を省略しても同じ SQL になる。確認語は全種類で共通。
        params = {} if dataset == SampleDataset.HR else {"dataset": dataset.value}
        preview = await client.get("/api/nl2sql/sample-data", params=params)
        assert preview.status_code == 200
        info = preview.json()["data"]
        assert info["dataset"] == dataset.value
        assert info["objects"] == list(definition.objects)
        assert info["confirmation"] == "ADMIN_EXECUTE"
        assert info["imported_objects"] == []
        payload = {**params, "confirmation": SAMPLE_DATA_CONFIRMATION}
        imported = await client.post("/api/nl2sql/sample-data/import", json=payload)
        assert imported.status_code == 200
        assert imported.json()["data"]["executed"] is True
        assert imported.json()["data"]["dataset"] == dataset.value
        info = (await client.get("/api/nl2sql/sample-data", params=params)).json()["data"]
        assert info["imported_objects"] == list(definition.objects)
        deleted = await client.post("/api/nl2sql/sample-data/delete", json=payload)
        assert deleted.status_code == 200
        assert deleted.json()["data"]["executed"] is True
        assert deleted.json()["data"]["dataset"] == dataset.value
        assert service.sample_data_info(dataset).imported_objects == []


@pytest.mark.parametrize("dataset", ["unknown", "../sql_assist_sample", "", "SALES"])
async def test_invalid_dataset_is_rejected_without_mutation(
    dataset: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = make_service()
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/nl2sql/sample-data", params={"dataset": dataset})
        assert response.status_code == 422
        for operation in ("import", "delete"):
            response = await client.post(
                f"/api/nl2sql/sample-data/{operation}",
                json={"dataset": dataset, "confirmation": SAMPLE_DATA_CONFIRMATION},
            )
            assert response.status_code == 422
    assert service.get_catalog().tables == []


@pytest.mark.parametrize("incremental", [False, True])
@pytest.mark.parametrize("dataset", list(SampleDataset))
def test_dataset_and_owner_isolation(dataset: SampleDataset, incremental: bool) -> None:
    service = make_service()
    if incremental:
        service._incremental_repository = MemoryIncrementalNl2SqlRepository()
    for item in SAMPLE_DATASETS:
        service.import_sample_data(
            SampleDataMutationRequest(dataset=item, confirmation=SAMPLE_DATA_CONFIRMATION)
        )
    service._catalog.tables.extend(
        table.model_copy(update={"owner": "OTHER"}) for table in list(service._catalog.tables)
    )
    service._catalog.tables.append(
        SchemaTable(owner="APP", table_name="USER_TABLE", logical_name="利用者の表")
    )
    service._persist_local_catalog()
    before = service.get_catalog().model_copy(deep=True)
    definition = SAMPLE_DATASETS[dataset]
    # 種類ごとの旧確認語は受け付けず、ADMIN_EXECUTE だけで実行できる。
    for wrong_phrase in (*_LEGACY_CONFIRMATIONS, "WRONG", "admin_execute"):
        for operation in (service.import_sample_data, service.delete_sample_data):
            rejected = operation(
                SampleDataMutationRequest(dataset=dataset, confirmation=wrong_phrase)
            )
            assert rejected.executed is False
            assert {item.status for item in rejected.statements} == {"confirmation_required"}
            assert "ADMIN_EXECUTE" in rejected.warnings[0]
            assert service.get_catalog() == before

    result = service.delete_sample_data(
        SampleDataMutationRequest(dataset=dataset, confirmation=SAMPLE_DATA_CONFIRMATION)
    )
    assert result.executed
    after = {(t.owner, t.table_name): t for t in service.get_catalog().tables}
    expected = {
        (t.owner, t.table_name): t
        for t in before.tables
        if not (t.owner == "APP" and t.table_name in definition.objects)
    }
    assert after == expected
    assert service.sample_data_info(dataset).imported_objects == []


@pytest.mark.parametrize("dataset", [SampleDataset.SALES, SampleDataset.INQUIRIES])
def test_new_dataset_keeps_legacy_profile_and_has_independent_steps(dataset: SampleDataset) -> None:
    service = make_service()
    service._profiles["sql_assist_sample"] = Nl2SqlProfile(id="sql_assist_sample", name="人事")
    definition = SAMPLE_DATASETS[dataset]
    for step in (SampleDataStep.DATA, SampleDataStep.VIEWS):
        blocked = service.import_sample_data(
            SampleDataMutationRequest(
                dataset=dataset, step=step, confirmation=SAMPLE_DATA_CONFIRMATION
            )
        )
        assert not blocked.executed
        assert {item.status for item in blocked.statements} == {"missing_sample_tables"}
    for step in (SampleDataStep.TABLES, SampleDataStep.VIEWS, SampleDataStep.DATA):
        result = service.import_sample_data(
            SampleDataMutationRequest(
                dataset=dataset, step=step, confirmation=SAMPLE_DATA_CONFIRMATION
            )
        )
        assert result.executed
        if step == SampleDataStep.TABLES:
            assert all(t.row_count == 0 for t in service.get_catalog().tables)
    assert service.sample_data_info(dataset).imported_objects == list(definition.objects)
    counts = {t.table_name: t.row_count for t in service.get_catalog().tables}
    assert [counts[name] for name in definition.tables] == [3, 3, 6]
    service.delete_sample_data(
        SampleDataMutationRequest(dataset=dataset, confirmation=SAMPLE_DATA_CONFIRMATION)
    )
    assert not service._profiles["sql_assist_sample"].archived


@pytest.mark.parametrize("dataset", [SampleDataset.SALES, SampleDataset.INQUIRIES])
@pytest.mark.parametrize("step", list(SampleDataStep))
def test_oracle_uses_selected_sql_status_scope_and_refresh_targets(
    dataset: SampleDataset, step: SampleDataStep, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = make_service()
    adapter = MagicMock()
    definition = SAMPLE_DATASETS[dataset]
    adapter.fetch_catalog.return_value = sample_catalog(service, dataset)
    adapter.execute_admin_statements.side_effect = lambda statements, **kwargs: [
        {"index": i, "statement_type": sql.split()[0], "status": "success", "sql": sql}
        for i, sql in enumerate(statements, 1)
    ]
    monkeypatch.setattr(service, "_oracle_adapter", adapter)
    monkeypatch.setattr(service, "_use_oracle_runtime", lambda: True)
    sync = MagicMock(return_value=SchemaRefreshMutationSync(job_id="sample-refresh"))
    monkeypatch.setattr(service, "_submit_schema_refresh_after_admin_mutation", sync)
    audit = MagicMock()
    monkeypatch.setattr(service, "_record_admin_audit", audit)
    info = service.sample_data_info(dataset)
    assert info.imported_objects == list(definition.objects)
    adapter.fetch_catalog.assert_called_once_with(
        include_samples=False,
        object_keys={("APP", name) for name in (*definition.objects, *definition.legacy_names)},
    )
    request = SampleDataMutationRequest(
        dataset=dataset, step=step, confirmation=SAMPLE_DATA_CONFIRMATION
    )
    result = service.import_sample_data(request)
    assert result.executed and result.schema_refresh_job_id == "sample-refresh"
    sections = ["tables", "views", "data"] if step == SampleDataStep.ALL else [step.value]
    adapter.execute_admin_statements.assert_called_once_with(
        [sql for section in sections for sql in info.sql[section]],
        atomic=False,
        ignored_error_codes=frozenset({"ORA-00955", "ORA-00001"}),
    )
    targets = sync.call_args.kwargs["target_objects"]
    expected = definition.views if step == SampleDataStep.VIEWS else definition.tables
    if step == SampleDataStep.ALL:
        expected = definition.objects
    assert {t.object_name for t in targets} == set(expected)
    assert all(t.owner == "APP" and t.expected_state == "present" for t in targets)
    assert audit.call_args.kwargs["detail"]["dataset"] == dataset.value
    adapter.execute_admin_statements.reset_mock()
    result = service.delete_sample_data(request)
    assert result.executed
    adapter.execute_admin_statements.assert_called_once_with(
        info.sql["delete"], atomic=False, ignored_error_codes=frozenset({"ORA-00942"})
    )
    targets = sync.call_args.kwargs["target_objects"]
    assert {t.object_name for t in targets} == set(definition.objects)
    assert all(t.expected_state == "absent" for t in targets)
    # 削除（DROP）も取り込みと同じく実行の監査を残す（#948）。
    delete_audit = audit.call_args.kwargs
    assert delete_audit["operation"] == "sample_data_delete"
    assert delete_audit["executed"] is True
    assert delete_audit["detail"]["dataset"] == dataset.value
    assert delete_audit["detail"]["success_count"] == len(info.sql["delete"])
    assert set(definition.objects) <= set(delete_audit["target"].split(","))


@pytest.mark.parametrize("operation", ["import_sample_data", "delete_sample_data"])
def test_domain_checks_current_permission_before_mutation(
    operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = make_service()
    monkeypatch.setattr(get_settings(), "auth_mode", "authenticated")
    security = MagicMock()
    security.principal_for_worker.return_value.has_permission.return_value = False
    monkeypatch.setattr("app.security.service.get_security_service", lambda: security)
    with pytest.raises(SecurityApiError) as caught:
        getattr(service, operation)(
            SampleDataMutationRequest(
                dataset=SampleDataset.SALES, confirmation=SAMPLE_DATA_CONFIRMATION
            )
        )
    assert caught.value.status_code == 403
    assert service.get_catalog().tables == []


@pytest.mark.parametrize("dataset", [SampleDataset.SALES, SampleDataset.INQUIRIES])
def test_sample_sql_supports_relations_and_example_queries(dataset: SampleDataset) -> None:
    # Oracle 方言として解析後、テスト専用 SQLite に変換して FK・データ・JOIN を検証する。
    # 実 Oracle での DDL 実行検証の代替ではなく、固定 fixture の整合性検証。
    sections = make_service().sample_data_info(dataset).sql
    with sqlite3.connect(":memory:") as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for section in ("tables", "views", "data"):
            for statement in sections[section]:
                parsed = sqlglot.parse_one(statement, read="oracle")
                if statement.startswith("COMMENT"):
                    continue
                statement = parsed.sql(dialect="sqlite").replace(
                    "CREATE OR REPLACE VIEW", "CREATE VIEW"
                )
                connection.execute(statement)
        for table in SAMPLE_DATASETS[dataset].tables:
            assert connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] > 0
        view = SAMPLE_DATASETS[dataset].views[0]
        assert connection.execute(f'SELECT COUNT(*) FROM "{view}"').fetchone()[0] == 6
        if dataset == SampleDataset.SALES:
            assert (
                connection.execute(
                    "SELECT SUM(SALES_AMOUNT) FROM V_SALES_DETAIL WHERE STATUS = '出荷済'"
                ).fetchone()[0]
                == 550000
            )
        else:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM V_INQUIRY_DETAIL WHERE RESOLVED_DATE IS NULL"
                ).fetchone()[0]
                == 3
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_sample_object_names_have_no_legacy_prefix() -> None:
    service = make_service()
    for dataset, definition in SAMPLE_DATASETS.items():
        assert not any(name.startswith(("SAMPLE_", "NL2SQL_")) for name in definition.objects)
        sections = service.sample_data_info(dataset).sql
        assert "SAMPLE_NL2SQL_" not in "\n".join(s for items in sections.values() for s in items)


def _oracle_service(
    monkeypatch: pytest.MonkeyPatch, catalog: SchemaCatalog
) -> tuple[Nl2SqlService, MagicMock]:
    service = make_service()
    adapter = MagicMock()
    adapter.fetch_catalog.return_value = catalog
    adapter.execute_admin_statements.side_effect = lambda statements, **kwargs: [
        {"index": i, "statement_type": sql.split()[0], "status": "success", "sql": sql}
        for i, sql in enumerate(statements, 1)
    ]
    monkeypatch.setattr(service, "_oracle_adapter", adapter)
    monkeypatch.setattr(service, "_use_oracle_runtime", lambda: True)
    monkeypatch.setattr(
        service,
        "_submit_schema_refresh_after_admin_mutation",
        MagicMock(return_value=SchemaRefreshMutationSync(job_id="sample-refresh")),
    )
    monkeypatch.setattr(service, "_record_admin_audit", MagicMock())
    return service, adapter


@pytest.mark.parametrize("dataset", list(SampleDataset))
def test_oracle_same_name_user_object_blocks_import_and_delete(
    dataset: SampleDataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 接頭辞がないため利用者の同名表と衝突し得る。構成が異なる表には DROP / INSERT を実行しない。
    definition = SAMPLE_DATASETS[dataset]
    user_table = SchemaTable(
        owner="APP",
        table_name=definition.tables[-1],
        logical_name="利用者の表",
        columns=[SchemaColumn(column_name="ID", logical_name="ID", data_type="NUMBER")],
    )
    service, adapter = _oracle_service(
        monkeypatch,
        SchemaCatalog(
            current_owner="APP", refreshed_at="2026-09-13T00:00:00Z", tables=[user_table]
        ),
    )
    info = service.sample_data_info(dataset)
    assert info.imported_objects == []
    assert info.conflicting_objects == [definition.tables[-1]]
    assert any(definition.tables[-1] in warning for warning in info.warnings)
    request = SampleDataMutationRequest(dataset=dataset, confirmation=SAMPLE_DATA_CONFIRMATION)
    for operation in (service.import_sample_data, service.delete_sample_data):
        result = operation(request)
        assert result.executed is False
        assert {item.status for item in result.statements} == {"blocked"}
        assert definition.tables[-1] in result.warnings[0]
    adapter.execute_admin_statements.assert_not_called()


def test_oracle_view_with_table_columns_is_not_treated_as_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = make_service()
    catalog = sample_catalog(service, SampleDataset.SALES)
    catalog.tables[0] = catalog.tables[0].model_copy(update={"table_type": "view"})
    service, adapter = _oracle_service(monkeypatch, catalog)
    info = service.sample_data_info(SampleDataset.SALES)
    assert info.conflicting_objects == [catalog.tables[0].table_name]
    result = service.delete_sample_data(
        SampleDataMutationRequest(
            dataset=SampleDataset.SALES, confirmation=SAMPLE_DATA_CONFIRMATION
        )
    )
    assert not result.executed
    adapter.execute_admin_statements.assert_not_called()


def test_oracle_metadata_failure_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.features.nl2sql.oracle_adapter import OracleAdapterError

    service, adapter = _oracle_service(monkeypatch, SchemaCatalog(refreshed_at="", tables=[]))
    adapter.fetch_catalog.side_effect = OracleAdapterError("ORA-12541")
    request = SampleDataMutationRequest(
        dataset=SampleDataset.SALES, confirmation=SAMPLE_DATA_CONFIRMATION
    )
    for operation in (service.import_sample_data, service.delete_sample_data):
        result = operation(request)
        assert result.executed is False
        assert {item.status for item in result.statements} == {"error"}
    adapter.execute_admin_statements.assert_not_called()


@pytest.mark.parametrize("dataset", [SampleDataset.SALES, SampleDataset.INQUIRIES])
def test_oracle_legacy_prefixed_objects_are_reported_and_dropped_on_delete(
    dataset: SampleDataset, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = make_service()
    definition = SAMPLE_DATASETS[dataset]
    catalog = sample_catalog(service, dataset, prefix="SAMPLE_NL2SQL_")
    service, adapter = _oracle_service(monkeypatch, catalog)
    info = service.sample_data_info(dataset)
    legacy = list(definition.legacy_names)
    assert info.imported_objects == []
    assert info.legacy_objects == legacy
    assert any("旧名" in warning for warning in info.warnings)
    view, *tables = [name for name in legacy if name.startswith("SAMPLE_NL2SQL_V_")] + [
        name for name in reversed(legacy) if not name.startswith("SAMPLE_NL2SQL_V_")
    ]
    legacy_drops = [
        f"DROP VIEW {view}",
        *(f"DROP TABLE {name} CASCADE CONSTRAINTS PURGE" for name in tables),
    ]
    assert info.sql["delete"][: len(legacy_drops)] == legacy_drops
    # 旧名があっても現行名の取り込みは妨げない。
    assert service.import_sample_data(
        SampleDataMutationRequest(dataset=dataset, confirmation=SAMPLE_DATA_CONFIRMATION)
    ).executed
    adapter.execute_admin_statements.reset_mock()
    result = service.delete_sample_data(
        SampleDataMutationRequest(dataset=dataset, confirmation=SAMPLE_DATA_CONFIRMATION)
    )
    assert result.executed
    adapter.execute_admin_statements.assert_called_once_with(
        info.sql["delete"], atomic=False, ignored_error_codes=frozenset({"ORA-00942"})
    )
    refresh = service._submit_schema_refresh_after_admin_mutation
    assert isinstance(refresh, MagicMock)
    targets = refresh.call_args.kwargs["target_objects"]
    assert {t.object_name for t in targets} == {*definition.objects, *legacy}


def test_deterministic_same_name_user_object_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service()
    user_table = SchemaTable(
        owner="APP",
        table_name="SALES_ORDER",
        logical_name="利用者の受注",
        columns=[SchemaColumn(column_name="ID", logical_name="ID", data_type="NUMBER")],
    )
    service._catalog.tables.append(user_table)
    request = SampleDataMutationRequest(
        dataset=SampleDataset.SALES, confirmation=SAMPLE_DATA_CONFIRMATION
    )
    for operation in (service.import_sample_data, service.delete_sample_data):
        result = operation(request)
        assert result.executed is False
        assert {item.status for item in result.statements} == {"blocked"}
    assert service._catalog.tables == [user_table]
    # 他の種類のサンプルは名前が重ならないため操作できる。
    assert service.import_sample_data(
        SampleDataMutationRequest(
            dataset=SampleDataset.INQUIRIES, confirmation=SAMPLE_DATA_CONFIRMATION
        )
    ).executed
