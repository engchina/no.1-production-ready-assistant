"""構築対象の外の DDL では、構築中の文脈を無効にしない。"""

from app.features.nl2sql.models import Nl2SqlProfile, SchemaCatalog, SchemaColumn, SchemaTable
from app.features.nl2sql.ontology_build import build_schema_context_from_catalog


def test_build_fingerprint_is_scoped_to_profile() -> None:
    table = SchemaTable(
        table_name="TABLE_A",
        logical_name="表 A",
        owner="APP",
        columns=[SchemaColumn(column_name="VALUE", logical_name="値", data_type="NUMBER")],
    )
    profile = Nl2SqlProfile(id="profile-a", name="プロファイル A", allowed_tables=["APP.TABLE_A"])
    catalog = SchemaCatalog(tables=[table], refreshed_at="2026-01-01T00:00:00Z")
    before = build_schema_context_from_catalog(profile, catalog).schema_fingerprint
    unrelated = table.model_copy(update={"table_name": "TABLE_B"})
    changed = catalog.model_copy(update={"tables": [table, unrelated], "catalog_version": 99})
    assert build_schema_context_from_catalog(profile, changed).schema_fingerprint == before
    edited = table.model_copy(
        update={"columns": [table.columns[0].model_copy(update={"data_type": "VARCHAR2(30 CHAR)"})]}
    )
    changed = catalog.model_copy(update={"tables": [edited, unrelated]})
    assert build_schema_context_from_catalog(profile, changed).schema_fingerprint != before
