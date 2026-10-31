"""オントロジーの AI 構築・業務定義・SQL 接地を Oracle の引用規則で照合する (#573)。

`SALES."Mixed_Case"` と大文字の同名表 `SALES.MIXED_CASE`（列 `"Amount"` と `AMOUNT`）が並存する
catalog で、名前を大文字化して同じものとして扱わないことを固定する。引用が不要な名前の表記・
照合キーは従来と同じ値のまま（互換）であることも併せて確認する。
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from sqlglot import exp
from test_nl2sql_ontology_core import _session_ready_for_sql

from app.features.nl2sql.models import (
    AllowedObjects,
    MetadataSqlSampleRequest,
    MetadataSqlSampleTarget,
    Nl2SqlProfile,
    SchemaCatalog,
    SchemaColumn,
    SchemaConstraintDetail,
    SchemaTable,
)
from app.features.nl2sql.object_identity import object_part_name, split_match_key
from app.features.nl2sql.ontology_build import (
    _context_name,
    _qa_pair_context_payload,
    _QaSqlColumns,
    _ScopeResolver,
    _selected_schema_objects,
    build_schema_context,
    build_schema_context_from_catalog,
    convert_extraction_to_proposals,
    merge_build_extractions,
)
from app.features.nl2sql.ontology_capabilities import checked_capability_sql
from app.features.nl2sql.ontology_catalog import (
    build_schema_ontology,
    migrate_profile_ontology_view,
)
from app.features.nl2sql.ontology_definition_validation import (
    schema_objects,
    validate_definitions,
)
from app.features.nl2sql.ontology_definitions import (
    DefinitionMapping,
    MetricDefinitionV2,
    ObjectTypeDefinition,
    ProfileOntologyBundle,
    PropertyDefinition,
)
from app.features.nl2sql.ontology_mermaid import render_mermaid_er
from app.features.nl2sql.ontology_models import (
    OntologyBuildExtraction,
    OntologyEntityNamingCandidate,
    OntologyNode,
    OntologyNodeKind,
    OntologyProvenance,
    OntologyReviewStatus,
    OntologyRevision,
    OntologyRevisionStatus,
    OntologySourceKind,
    PhysicalColumnRef,
    PhysicalMapping,
    PhysicalObjectRef,
    ProfileOntologyView,
    QaPair,
)
from app.features.nl2sql.ontology_published_context import _allowed_scope, _mappings_visible
from app.features.nl2sql.ontology_service import (
    OntologyQuerySessionService,
    _sql_alias_map,
    _sql_column_variants,
)
from app.features.nl2sql.ontology_sql_validation import physical_column, physical_table
from app.features.nl2sql.ontology_unified_model import physical_node_index, project_graph
from app.features.nl2sql.service import Nl2SqlService, _extract_referenced_columns
from app.features.nl2sql.sql_semantics import parse_oracle_sql
from app.features.nl2sql.store import MemoryNl2SqlStore

UPPER = "SALES.MIXED_CASE"
QUOTED = 'SALES."Mixed_Case"'


def _column(name: str, logical_name: str = "") -> SchemaColumn:
    return SchemaColumn(
        column_name=name,
        logical_name=logical_name or f"{name} 列",
        data_type="NUMBER",
        sample_values=[f"{name}-sample"],
    )


def _catalog() -> SchemaCatalog:
    upper = SchemaTable(
        owner="SALES",
        table_name="MIXED_CASE",
        logical_name="大文字の表",
        table_type="table",
        columns=[_column("ID"), _column("AMOUNT", "大文字の金額"), _column("UPPER_ONLY")],
    )
    quoted = SchemaTable(
        owner="SALES",
        table_name="Mixed_Case",
        logical_name="引用名の表",
        table_type="table",
        columns=[
            _column("ID"),
            _column("AMOUNT", "引用名の表の大文字列"),
            _column("Amount", "引用名の金額"),
            _column("PARENT_ID"),
        ],
        constraint_details=[
            SchemaConstraintDetail(
                constraint_name="MIXED_FK",
                constraint_type="R",
                owner="SALES",
                table_name="Mixed_Case",
                columns=["PARENT_ID"],
                referenced_owner="SALES",
                referenced_table="MIXED_CASE",
                referenced_columns=["ID"],
                status="ENABLED",
            )
        ],
    )
    return SchemaCatalog(
        refreshed_at="2026-09-14T00:00:00+00:00",
        current_owner="SALES",
        tables=[upper, quoted],
    )


def _profile(*tables: str) -> Nl2SqlProfile:
    return Nl2SqlProfile(id="p573", name="引用名", allowed_tables=list(tables))


def _schema_payload(*tables: str) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(build_schema_context_from_catalog(_profile(*tables), _catalog()).schema_context),
    )


# --- 互換: 引用が不要な名前は従来の表記・照合キーのまま ---------------------------------


def test_context_name_keeps_names_that_do_not_need_case_preserving_quotes() -> None:
    assert _context_name("ORDERS") == "ORDERS"
    assert _context_name("売上") == "売上"
    assert _context_name("MY TABLE") == "MY TABLE"
    assert _context_name("Mixed_Case") == '"Mixed_Case"'


def test_schema_objects_keys_are_unchanged_for_unquoted_names() -> None:
    physical = schema_objects(
        {"objects": [{"owner": "APP", "object_name": "ORDERS", "columns": [{"column": "ID"}]}]}
    )

    assert physical == {"APP.ORDERS": {"ID"}}


# --- AI 構築の schema context ---------------------------------------------------------


def test_quoted_profile_object_is_included_in_ai_build_context() -> None:
    selected, warnings, errors = _selected_schema_objects(_profile(QUOTED), _catalog())
    payload = _schema_payload(UPPER, QUOTED)

    assert [(t.owner, t.table_name) for t in selected] == [("SALES", "Mixed_Case")]
    assert warnings == [] and errors == []
    objects = {item["object"]: item for item in payload["objects"]}
    assert set(objects) == {QUOTED, UPPER}
    assert [column["column"] for column in objects[QUOTED]["columns"]] == [
        "ID",
        "AMOUNT",
        '"Amount"',
        "PARENT_ID",
    ]
    assert objects[QUOTED]["columns"][2]["qualified_column"] == f'{QUOTED}."Amount"'
    [relationship] = payload["relationships"]
    assert relationship["source_object"] == QUOTED
    assert relationship["target_object"] == UPPER
    assert relationship["join_conditions"][0]["left"] == f"{QUOTED}.PARENT_ID"


def test_foreign_key_to_unselected_same_name_table_is_not_included() -> None:
    payload = _schema_payload(QUOTED)

    assert payload["relationships"] == []


def test_ontology_schema_context_and_scope_resolver_distinguish_same_name_tables() -> None:
    ontology = build_schema_ontology(_catalog())
    view = migrate_profile_ontology_view(_profile(UPPER, QUOTED), ontology)
    resolver = _ScopeResolver(ontology, view)
    context = json.loads(build_schema_context(ontology, view))

    quoted = resolver.resolve_object(QUOTED)
    upper = resolver.resolve_object(UPPER)
    unquoted_mixed = resolver.resolve_object("SALES.Mixed_Case")
    quoted_amount = resolver.resolve_column(f'{QUOTED}."Amount"')
    quoted_upper_amount = resolver.resolve_column(f"{QUOTED}.AMOUNT")
    upper_amount = resolver.resolve_column(f"{UPPER}.AMOUNT")

    assert quoted is not None and quoted.metadata["object_name"] == "Mixed_Case"
    assert upper is not None and upper.metadata["object_name"] == "MIXED_CASE"
    # 引用しない `Mixed_Case` は Oracle と同じく大文字の表を指す。
    assert unquoted_mixed is upper
    assert quoted_amount is not None
    assert (quoted_amount.metadata["object_name"], quoted_amount.metadata["column_name"]) == (
        "Mixed_Case",
        "Amount",
    )
    assert quoted_upper_amount is not None
    assert quoted_upper_amount.metadata["column_name"] == "AMOUNT"
    assert quoted_upper_amount.metadata["object_name"] == "Mixed_Case"
    assert upper_amount is not None and upper_amount.metadata["object_name"] == "MIXED_CASE"
    # 列名だけ・表名だけでは同名が複数あるため解決しない。
    assert resolver.resolve_column("AMOUNT") is None
    assert resolver.resolve_column('"Amount"') is quoted_amount
    objects = {item["object"]: item for item in context["objects"]}
    assert set(objects) == {QUOTED, UPPER}
    assert {column["column"] for column in objects[QUOTED]["columns"]} == {
        "ID",
        "AMOUNT",
        '"Amount"',
        "PARENT_ID",
    }


def test_llm_candidates_are_converted_to_the_exact_quoted_object() -> None:
    ontology = build_schema_ontology(_catalog())
    view = migrate_profile_ontology_view(_profile(UPPER, QUOTED), ontology)
    extraction = OntologyBuildExtraction(
        entities=[
            OntologyEntityNamingCandidate(object_name=QUOTED, business_name_ja="引用名の業務"),
            OntologyEntityNamingCandidate(object_name=UPPER, business_name_ja="大文字の業務"),
        ]
    )

    drafts, warnings = convert_extraction_to_proposals(
        extraction,
        ontology=ontology,
        view=view,
        job_id="job573",
        inferred_by="test",
    )

    node_by_id = {node.id: node for node in ontology.nodes}
    mapped = {
        draft.payload.values["node_upserts"][0]["business_name_ja"]: node_by_id[
            draft.payload.values["edge_upserts"][0]["target_node_id"]
        ].metadata["object_name"]
        for draft in drafts
    }
    assert warnings == []
    assert mapped == {"引用名の業務": "Mixed_Case", "大文字の業務": "MIXED_CASE"}


def test_merge_build_extractions_keeps_same_name_candidates_separate() -> None:
    base = OntologyBuildExtraction(
        entities=[OntologyEntityNamingCandidate(object_name=QUOTED, business_name_ja="引用名")]
    )
    addition = OntologyBuildExtraction(
        entities=[
            OntologyEntityNamingCandidate(object_name=UPPER, business_name_ja="大文字"),
            OntologyEntityNamingCandidate(
                object_name='"SALES"."Mixed_Case"', business_name_ja="重複"
            ),
        ]
    )

    merged, added = merge_build_extractions(base, addition)

    assert added == 1
    assert [item.business_name_ja for item in merged.entities] == ["引用名", "大文字"]


def test_qa_context_resolves_quoted_sql_references_to_exact_objects_and_columns() -> None:
    payload = _qa_pair_context_payload(
        _schema_payload(UPPER, QUOTED),
        QaPair(
            question="金額",
            sql=(
                'SELECT m."Amount", u.AMOUNT FROM SALES."Mixed_Case" m '
                "JOIN SALES.MIXED_CASE u ON m.PARENT_ID = u.ID"
            ),
        ),
    )

    assert payload["schema_resolved_objects"] == [
        {"sql_table": 'SALES."Mixed_Case" m', "object": QUOTED, "alias": "m"},
        {"sql_table": "SALES.MIXED_CASE u", "object": UPPER, "alias": "u"},
    ]
    assert payload["schema_sql_aliases"] == [
        {"alias": "M", "object": QUOTED},
        {"alias": "U", "object": UPPER},
    ]
    columns = {item["sql"]: item["column"] for item in payload["schema_resolved_columns"]}
    assert columns['m."Amount"'] == f'{QUOTED}."Amount"'
    assert columns["u.AMOUNT"] == f"{UPPER}.AMOUNT"
    assert payload["schema_resolved_join_conditions"] == [
        {
            "condition_sql": "m.PARENT_ID = u.ID",
            "resolved_columns": [f"{QUOTED}.PARENT_ID", f"{UPPER}.ID"],
        }
    ]
    assert payload["schema_unresolved_references"] == []


def test_qa_sql_join_evidence_distinguishes_quoted_column() -> None:
    ontology = build_schema_ontology(_catalog())
    columns = {
        (node.metadata["object_name"], node.metadata["column_name"]): node
        for node in ontology.nodes
        if node.kind == OntologyNodeKind.COLUMN
    }
    quoted_only = _QaSqlColumns.from_sql('SELECT m."Amount" FROM SALES."Mixed_Case" m')
    unparsable = _QaSqlColumns(sql='SELECT "Amount" FROM', tokens=frozenset())

    assert quoted_only.mentions(columns[("Mixed_Case", "Amount")])
    assert not quoted_only.mentions(columns[("MIXED_CASE", "AMOUNT")])
    assert unparsable.mentions(columns[("Mixed_Case", "Amount")])
    assert not unparsable.mentions(columns[("MIXED_CASE", "AMOUNT")])


# --- 業務定義の検証・公開版の scope ---------------------------------------------------


def _bundle(*definitions: object) -> ProfileOntologyBundle:
    return ProfileOntologyBundle.model_validate(
        {
            "id": "bundle573",
            "profile_id": "p573",
            "job_id": "job573",
            "schema_fingerprint": "schema",
            "profile_fingerprint": "profile",
            "definitions": [
                definition.model_dump(mode="json")  # type: ignore[attr-defined]
                for definition in definitions
            ],
        }
    )


def _quoted_amount_definitions(
    owner: str, object_name: str, column_name: str
) -> tuple[ObjectTypeDefinition, PropertyDefinition]:
    return (
        ObjectTypeDefinition(
            api_name="Mixed",
            name_ja="引用名",
            properties=["Mixed.amount"],
            primary_key=["Mixed.amount"],
            mappings=[DefinitionMapping(owner=owner, object_name=object_name)],
        ),
        PropertyDefinition(
            api_name="Mixed.amount",
            name_ja="金額",
            object_type="Mixed",
            data_type="number",
            required=True,
            mappings=[
                DefinitionMapping(owner=owner, object_name=object_name, column_name=column_name)
            ],
        ),
    )


def _error_codes(bundle: ProfileOntologyBundle, schema: dict[str, Any]) -> set[str]:
    return {
        finding.code
        for finding in validate_definitions(bundle, schema)
        if finding.severity == "error"
    }


def test_schema_objects_keys_distinguish_quoted_objects_and_columns() -> None:
    physical = schema_objects(_schema_payload(UPPER, QUOTED))

    assert physical[QUOTED] == {"ID", "AMOUNT", '"Amount"', "PARENT_ID"}
    assert physical[UPPER] == {"ID", "AMOUNT", "UPPER_ONLY"}


@pytest.mark.parametrize(
    ("object_name", "column_name", "inside"),
    [
        ('"Mixed_Case"', '"Amount"', True),
        ('"Mixed_Case"', "amount", True),
        ("Mixed_Case", '"Amount"', False),
        ("MIXED_CASE", "AMOUNT", False),
    ],
)
def test_definition_mapping_scope_uses_quoting_rules(
    object_name: str, column_name: str, inside: bool
) -> None:
    bundle = _bundle(*_quoted_amount_definitions("SALES", object_name, column_name))

    codes = _error_codes(bundle, _schema_payload(QUOTED))

    assert ("MAPPING_OUTSIDE_PROFILE" not in codes) is inside, codes


@pytest.mark.parametrize(
    ("expression_sql", "valid"),
    [
        ('SELECT SUM(m."Amount") FROM SALES."Mixed_Case" m', True),
        ('SELECT SUM("Amount") FROM SALES."Mixed_Case"', True),
        ("SELECT SUM(AMOUNT) FROM SALES.MIXED_CASE", False),
        ('SELECT SUM(m."Amount") FROM SALES.MIXED_CASE m', False),
        ('SELECT SUM(m.UPPER_ONLY) FROM SALES."Mixed_Case" m', False),
    ],
)
def test_metric_sql_is_validated_against_exact_quoted_objects(
    expression_sql: str, valid: bool
) -> None:
    metric = MetricDefinitionV2(
        api_name="MixedTotal",
        name_ja="引用名の金額合計",
        expression_sql=expression_sql,
        aggregation="sum",
    )

    codes = _error_codes(_bundle(metric), _schema_payload(QUOTED))

    assert ("SQL_EXPRESSION_INVALID" not in codes) is valid, codes


def test_metric_grain_compares_quoted_physical_columns() -> None:
    objects = _quoted_amount_definitions("SALES", '"Mixed_Case"', '"Amount"')
    metric = MetricDefinitionV2(
        api_name="MixedTotal",
        name_ja="金額別件数",
        expression_sql=(
            'SELECT m."Amount", COUNT(*) FROM SALES."Mixed_Case" m GROUP BY m."Amount"'
        ),
        aggregation="count",
        grain=["Mixed.amount"],
    )
    wrong = metric.model_copy(
        update={
            "api_name": "MixedWrong",
            "expression_sql": (
                'SELECT m.AMOUNT, COUNT(*) FROM SALES."Mixed_Case" m GROUP BY m.AMOUNT'
            ),
        }
    )

    codes = _error_codes(_bundle(*objects, metric), _schema_payload(QUOTED))
    wrong_codes = _error_codes(_bundle(*objects, wrong), _schema_payload(QUOTED))

    assert "METRIC_GRAIN_MISMATCH" not in codes, codes
    assert "METRIC_GRAIN_MISMATCH" in wrong_codes


def test_sql_validation_resolves_same_name_tables_and_columns() -> None:
    physical = schema_objects(_schema_payload(UPPER, QUOTED))
    quoted_table = exp.to_table('SALES."Mixed_Case"', dialect="oracle")
    upper_table = exp.to_table("sales.mixed_case", dialect="oracle")
    quoted_column = exp.column(
        exp.to_identifier("Amount", quoted=True),
        table=exp.to_identifier("Mixed_Case", quoted=True),
        db=exp.to_identifier("SALES"),
    )

    assert physical_table(quoted_table, physical) == QUOTED
    assert physical_table(upper_table, physical) == UPPER
    assert physical_column(quoted_column, physical) == f'{QUOTED}."Amount"'
    assert split_match_key(physical_column(quoted_column, physical)) == [
        "SALES",
        '"Mixed_Case"',
        '"Amount"',
    ]


def test_published_scope_filters_definitions_by_quoted_allowed_columns() -> None:
    quoted_object, quoted_property = _quoted_amount_definitions("SALES", '"Mixed_Case"', '"Amount"')
    _, upper_property = _quoted_amount_definitions("SALES", "MIXED_CASE", "AMOUNT")
    allowed = _allowed_scope({QUOTED: ['"Amount"']})

    assert allowed == {QUOTED: {'"Amount"'}}
    assert _mappings_visible(quoted_object, allowed)
    assert _mappings_visible(quoted_property, allowed)
    assert not _mappings_visible(upper_property, allowed)
    assert _allowed_scope({"sales.mixed_case": ["amount"]}) == {UPPER: {"AMOUNT"}}


def test_job_published_context_scope_does_not_uppercase_allowed_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.features.nl2sql.ontology_published_context as published
    import app.features.nl2sql.ontology_router as router

    captured: dict[str, list[str]] = {}

    class _Prepared:
        schema_context = json.dumps(_schema_payload(UPPER, QUOTED))

    monkeypatch.setattr(
        router.ontology_runtime, "prepare_build_schema_context", lambda _profile_id: _Prepared()
    )
    monkeypatch.setattr(
        published,
        "published_context",
        lambda _runtime, _profile_id, _release_id, columns: captured.update(columns) or "md",
    )
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    request = type("Request", (), {"use_ontology_context": True})()

    result = service._job_published_ontology_markdown(  # noqa: SLF001 - unit boundary
        request=request,
        profile=_profile(UPPER, QUOTED),
        business_release_id="release573",
        allowed=AllowedObjects(table_names=[QUOTED], columns={QUOTED: ['"Amount"', "id"]}),
    )

    assert result == "md"
    assert {name: sorted(columns) for name, columns in captured.items()} == {
        QUOTED: ['"Amount"', "ID"]
    }


def test_capability_sql_scope_distinguishes_quoted_objects() -> None:
    physical = {QUOTED: {"ID", '"Amount"'}}
    bundle = _bundle()

    checked = checked_capability_sql(
        'SELECT m."Amount" FROM SALES."Mixed_Case" m WHERE m.ID = :id',
        bundle,
        physical,
        expression=False,
        parameter_names={"id"},
    )

    assert '"Amount"' in checked
    for sql in (
        "SELECT AMOUNT FROM SALES.MIXED_CASE",
        'SELECT m.AMOUNT FROM SALES."Mixed_Case" m',
    ):
        with pytest.raises(ValueError):
            checked_capability_sql(sql, bundle, physical, expression=False, parameter_names=set())


# --- 業務定義から作る接地 graph・Mermaid ------------------------------------------------


def test_project_graph_maps_definitions_to_exact_quoted_physical_nodes() -> None:
    ontology = build_schema_ontology(_catalog())
    quoted_object, quoted_property = _quoted_amount_definitions("SALES", '"Mixed_Case"', '"Amount"')
    quoted_object.id = "def_mixed"
    quoted_property.id = "def_mixed_amount"
    index = physical_node_index(ontology.nodes)

    graph = project_graph(ontology, [quoted_object, quoted_property])

    node_by_id = {node.id: node for node in graph.nodes}
    column = index[f'{QUOTED}."Amount"']
    assert (column.metadata["object_name"], column.metadata["column_name"]) == (
        "Mixed_Case",
        "Amount",
    )
    assert index[f"{UPPER}.AMOUNT"].metadata["object_name"] == "MIXED_CASE"
    mapping = node_by_id["def_mixed_amount"].physical_mappings[0]
    assert (mapping.object_ref.owner, mapping.object_ref.object_name) == ("SALES", "Mixed_Case")
    assert mapping.column_refs[0].node_id == column.id


def test_mermaid_groups_columns_and_fk_markers_per_exact_object() -> None:
    ontology = build_schema_ontology(_catalog())

    rendered = render_mermaid_er(ontology)

    quoted_block = rendered.split('"SALES.Mixed_Case" {', 1)[1].split("}", 1)[0]
    upper_block = rendered.split('"SALES.MIXED_CASE" {', 1)[1].split("}", 1)[0]
    assert "Amount" in quoted_block and "PARENT_ID FK" in quoted_block
    assert "UPPER_ONLY" in upper_block and "Amount" not in upper_block
    assert " FK" not in upper_block


# --- SQL 接地（3 者照合）・逆生成ラベル・代表値 --------------------------------------------


def test_sql_semantic_referenced_columns_keep_quotes() -> None:
    graph = parse_oracle_sql(
        'SELECT m."Amount" FROM SALES."Mixed_Case" m JOIN SALES.MIXED_CASE u ON m.ID = u.id'
    ).graph

    assert graph is not None
    assert graph.projections[0].referenced_columns == ['m."Amount"']
    assert graph.joins[0].referenced_columns == ["m.ID", "u.id"]


def test_ontology_sql_grounding_variants_distinguish_quoted_names() -> None:
    graph = parse_oracle_sql(
        'SELECT m."Amount", u.AMOUNT FROM SALES."Mixed_Case" m JOIN SALES.MIXED_CASE u ON 1 = 1'
    ).graph
    assert graph is not None
    aliases = _sql_alias_map(graph)

    quoted = _sql_column_variants('m."Amount"', aliases)
    upper = _sql_column_variants("u.AMOUNT", aliases)

    assert f'{QUOTED}."Amount"' in quoted and f"{UPPER}.AMOUNT" not in quoted
    assert f"{UPPER}.AMOUNT" in upper and f'{QUOTED}."Amount"' not in upper


def test_reverse_labels_use_exact_quoted_table_and_column() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    catalog = _catalog()
    sql = 'SELECT "Amount" FROM SALES."Mixed_Case"'

    columns, wildcard = _extract_referenced_columns(sql, [QUOTED])
    table = service._reverse_table_for_ref(QUOTED, catalog)  # noqa: SLF001
    column = service._reverse_column_for_ref(columns[0], [QUOTED], catalog)  # noqa: SLF001
    upper_column = service._reverse_column_for_ref("AMOUNT", [UPPER], catalog)  # noqa: SLF001

    assert (columns, wildcard) == ([f'{QUOTED}."Amount"'], False)
    assert table is not None and table.table_name == "Mixed_Case"
    assert column is not None and column.logical_name == "引用名の金額"
    assert upper_column is not None and upper_column.logical_name == "大文字の金額"


def test_metadata_samples_use_catalog_column_names() -> None:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._catalog = _catalog()  # noqa: SLF001 - unit boundary
    request = MetadataSqlSampleRequest(
        targets=[
            MetadataSqlSampleTarget(
                owner="SALES", object_name='"Mixed_Case"', columns=['"Amount"', "amount"]
            )
        ],
        sample_limit=1,
    )

    samples = service._metadata_samples_from_catalog(request)  # noqa: SLF001
    text, count = service._format_metadata_samples(request, samples)  # noqa: SLF001

    assert samples == {QUOTED: {"Amount": ["Amount-sample"], "AMOUNT": ["AMOUNT-sample"]}}
    assert text == (f'OBJECT: {QUOTED}\n"Amount": Amount-sample\namount: AMOUNT-sample')
    assert count == 2
    assert object_part_name('"Amount"') == "Amount"


def _quoted_query_session_service() -> OntologyQuerySessionService:
    """`APP."Orders"."Amount"` を物理対応に持つ 3 者照合 service（node ID は core test と共通）。"""

    service = OntologyQuerySessionService()
    provenance = OntologyProvenance(source_kind=OntologySourceKind.MANUAL, source_id="test")
    physical = PhysicalObjectRef(
        node_id="node_orders_table", owner="APP", object_name="Orders", object_type="table"
    )

    def column_mapping(node_id: str, column_name: str) -> PhysicalMapping:
        return PhysicalMapping(
            object_ref=physical,
            column_refs=[
                PhysicalColumnRef(
                    node_id=node_id, owner="APP", object_name="Orders", column_name=column_name
                )
            ],
        )

    nodes = [
        OntologyNode(
            id="node_orders_table",
            revision_id="revision_1",
            kind=OntologyNodeKind.TABLE,
            technical_name='APP."Orders"',
            business_name_ja="受注テーブル",
            provenance=provenance,
            review_status=OntologyReviewStatus.APPROVED,
        ),
        OntologyNode(
            id="node_orders_entity",
            revision_id="revision_1",
            kind=OntologyNodeKind.BUSINESS_ENTITY,
            technical_name="orders",
            business_name_ja="受注",
            physical_mappings=[PhysicalMapping(object_ref=physical)],
            provenance=provenance,
            review_status=OntologyReviewStatus.APPROVED,
        ),
        OntologyNode(
            id="node_amount_metric",
            revision_id="revision_1",
            kind=OntologyNodeKind.METRIC,
            technical_name="total_amount",
            business_name_ja="受注金額合計",
            physical_mappings=[column_mapping("node_amount_column", "Amount")],
            provenance=provenance,
            review_status=OntologyReviewStatus.APPROVED,
        ),
        OntologyNode(
            id="node_customer_dimension",
            revision_id="revision_1",
            kind=OntologyNodeKind.PROPERTY,
            technical_name="customer_id",
            business_name_ja="顧客",
            physical_mappings=[column_mapping("node_customer_id_column", "CUSTOMER_ID")],
            provenance=provenance,
            review_status=OntologyReviewStatus.APPROVED,
        ),
    ]
    service.register_revision(
        OntologyRevision(
            id="revision_1",
            version=1,
            status=OntologyRevisionStatus.PUBLISHED,
            schema_fingerprint="schema-fingerprint",
        ),
        nodes=nodes,
    )
    service.register_profile_view(
        ProfileOntologyView(
            id="view_sales",
            profile_id="profile_sales",
            ontology_revision_id="revision_1",
            node_ids=[node.id for node in nodes],
            physical_objects=[physical],
        )
    )
    return service


@pytest.mark.parametrize(
    ("sql", "expected_codes", "valid"),
    [
        (
            'SELECT o.CUSTOMER_ID, SUM(o."Amount") AS TOTAL_AMOUNT FROM APP."Orders" o '
            "GROUP BY o.CUSTOMER_ID FETCH FIRST 100 ROWS ONLY",
            set(),
            True,
        ),
        (
            "SELECT o.CUSTOMER_ID, SUM(o.AMOUNT) AS TOTAL_AMOUNT FROM APP.ORDERS o "
            "GROUP BY o.CUSTOMER_ID FETCH FIRST 100 ROWS ONLY",
            {"SQL_OBJECT_OUTSIDE_PROFILE"},
            False,
        ),
        (
            'SELECT o.CUSTOMER_ID, SUM(o.AMOUNT) AS TOTAL_AMOUNT FROM APP."Orders" o '
            "GROUP BY o.CUSTOMER_ID FETCH FIRST 100 ROWS ONLY",
            {"SQL_METRIC_AGGREGATION_MISSING"},
            False,
        ),
    ],
)
def test_three_way_gate_distinguishes_quoted_objects_and_columns(
    sql: str, expected_codes: set[str], valid: bool
) -> None:
    service = _quoted_query_session_service()
    session_id = _session_ready_for_sql(service)

    report = service.register_generated_sql(session_id, sql).sql_artifacts[-1].validation_report

    assert report.is_valid is valid, report.findings
    assert expected_codes <= {finding.code for finding in report.findings}
