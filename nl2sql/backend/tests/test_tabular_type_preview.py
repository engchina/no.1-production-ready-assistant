"""取込の推定・型の補正・実行しないプレビューの契約。"""

import base64
from datetime import datetime
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.features.nl2sql.models import CsvImportColumn, DbAdminImportTabularRequest
from app.features.nl2sql.oracle_adapter import OracleNl2SqlAdapter
from app.features.nl2sql.service import Nl2SqlService
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.settings import get_settings


@pytest.mark.parametrize(
    "values,expected",
    [
        (["2026-01-02", "2026-02-03"], "DATE"),
        (["2026-01-02T12:30:00"], "TIMESTAMP"),
        (["2026-01-02T12:30:00+09:00"], "TIMESTAMP WITH TIME ZONE"),
        (["2026-02-30"], "VARCHAR2(255 CHAR)"),
        (["00123"], "VARCHAR2(255 CHAR)"),
        (["a" * 300], "VARCHAR2(600 CHAR)"),
        (["a" * 3000], "VARCHAR2(4000 CHAR)"),
    ],
)
def test_inference_has_capacity_and_parses_only_valid_dates(
    values: list[str], expected: str
) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    assert service._infer_csv_data_type(values) == expected


def test_text_capacity_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_csv_import_text_min_chars", 20)
    monkeypatch.setattr(get_settings(), "nl2sql_csv_import_text_growth_factor", 3.0)
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    assert service._infer_csv_data_type(["a" * 10]) == "VARCHAR2(30 CHAR)"


def test_preview_never_executes_even_with_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    adapter = Mock()
    monkeypatch.setattr(service, "_oracle_adapter", adapter)
    monkeypatch.setattr(service, "_use_oracle_runtime", lambda: True)
    monkeypatch.setattr(service, "_current_schema_owner", lambda: "APP")
    result = service.import_db_admin_tabular(
        DbAdminImportTabularRequest(
            table_name="TABLE_A",
            content_base64=base64.b64encode(b"ID,VALUE\n1,abc\n").decode(),
            preview=True,
            confirmation="ADMIN_EXECUTE",
            column_types={"VALUE": "VARCHAR2(1000 CHAR)"},
        )
    )
    assert result.preview
    assert not result.executed
    assert "VARCHAR2(1000 CHAR)" in result.ddl
    assert result.sample_rows == [{"ID": "1", "VALUE": "abc"}]
    adapter.import_tabular_table.assert_not_called()
    assert not result.schema_refresh_job_id
    assert not any("confirmation" in warning for warning in result.warnings)


@pytest.mark.parametrize(
    "kind", ["VARCHAR2(5000 CHAR)", "NUMBER); DROP TABLE TABLE_A", "VARCHAR2(10 BYTE)"]
)
def test_override_rejects_unsupported_types(kind: str) -> None:
    with pytest.raises(ValidationError):
        DbAdminImportTabularRequest(
            table_name="TABLE_A", content_base64="YQ==", column_types={"VALUE": kind}
        )


def test_date_bind_is_typed_and_independent_of_nls() -> None:
    adapter = OracleNl2SqlAdapter(get_settings())
    column = CsvImportColumn(source_name="DAY", column_name="DAY", data_type="DATE")
    assert adapter._coerce_csv_value("2026-01-02", column) == datetime(2026, 1, 2)
