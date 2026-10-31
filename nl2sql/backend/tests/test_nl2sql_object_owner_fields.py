"""表名を表示する API が所有者と所有者付きの名前を返すことを検証する（#556）。"""

import base64

from app.features.nl2sql.models import (
    DbAdminImportTabularRequest,
    SampleDataset,
    SchemaCatalog,
)
from app.features.nl2sql.sample_datasets import SAMPLE_DATASETS
from app.features.nl2sql.service import (
    Nl2SqlService,
    _display_qualified_name,
    _graph_with_resolved_table_owners,
)
from app.features.nl2sql.sql_semantics import parse_oracle_sql
from app.features.nl2sql.store import MemoryNl2SqlStore


def _service(current_owner: str = "APP") -> Nl2SqlService:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._catalog = SchemaCatalog(
        current_owner=current_owner, tables=[], refreshed_at="2026-09-14T00:00:00Z"
    )
    return service


def test_display_qualified_name_uses_catalog_quoting_and_never_guesses_owner() -> None:
    assert _display_qualified_name("APP", "ORDERS") == "APP.ORDERS"
    assert _display_qualified_name("APP", "orders") == 'APP."orders"'
    assert _display_qualified_name("", "ORDERS") == ""
    assert _display_qualified_name("APP", "") == ""


def test_sample_data_info_returns_owner_and_qualified_object_refs() -> None:
    service = _service("SALES_APP")

    info = service.sample_data_info(SampleDataset.HR)

    objects = list(SAMPLE_DATASETS[SampleDataset.HR].objects)
    assert info.objects == objects  # 既存 field は名前だけのまま（後方互換）
    assert info.owner == "SALES_APP"
    assert [ref.name for ref in info.object_refs] == objects
    assert all(ref.owner == "SALES_APP" for ref in info.object_refs)
    assert [ref.qualified_name for ref in info.object_refs] == [
        f"SALES_APP.{name}" for name in objects
    ]


def test_tabular_import_returns_owner_and_qualified_table_name() -> None:
    service = _service("APP")
    content = base64.b64encode("ID,NAME\n1,架空商事\n".encode()).decode()

    imported = service.import_db_admin_tabular(
        DbAdminImportTabularRequest(
            table_name="customer_import",
            content_base64=content,
            filename="customers.csv",
        )
    )

    assert imported.table_name == "CUSTOMER_IMPORT"
    assert imported.owner == "APP"
    assert imported.qualified_name == "APP.CUSTOMER_IMPORT"


def test_sql_grounding_tables_resolve_missing_owner_with_current_schema() -> None:
    semantic = parse_oracle_sql(
        "WITH recent AS (SELECT * FROM invoices) "
        "SELECT r.id FROM recent r JOIN hr.employees e ON e.id = r.employee_id"
    )

    graph = _graph_with_resolved_table_owners(semantic.graph, "app")

    assert graph is not None
    by_name = {table.name.upper(): table for table in graph.tables}
    invoices = by_name["INVOICES"]
    # SQL に書かれた owner / qualified_name は変えず、表示用の field だけを補う
    assert invoices.owner == ""
    assert invoices.resolved_owner == "APP"
    assert invoices.resolved_qualified_name == "APP.INVOICES"
    employees = by_name["EMPLOYEES"]
    assert employees.resolved_owner == "HR"
    assert employees.resolved_qualified_name == "HR.EMPLOYEES"
    recent = by_name["RECENT"]
    assert recent.is_cte is True
    assert recent.resolved_owner == ""
    assert recent.resolved_qualified_name == ""
    assert _graph_with_resolved_table_owners(None, "APP") is None
