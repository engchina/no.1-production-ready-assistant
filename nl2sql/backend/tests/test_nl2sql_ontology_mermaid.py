from __future__ import annotations

import re

from app.features.nl2sql.models import (
    Nl2SqlProfile,
    SchemaCatalog,
    SchemaColumn,
    SchemaConstraintDetail,
    SchemaTable,
    SchemaViewDependency,
)
from app.features.nl2sql.ontology_catalog import (
    SchemaOntology,
    build_schema_ontology,
    migrate_profile_ontology_view,
)
from app.features.nl2sql.ontology_mermaid import render_mermaid_er
from app.features.nl2sql.ontology_models import (
    JoinCondition,
    OntologyEdge,
    OntologyEdgeKind,
    OntologyNode,
    OntologyNodeKind,
    OntologyProvenance,
    OntologyReviewStatus,
    OntologySourceKind,
    PhysicalColumnRef,
    PhysicalMapping,
    PhysicalObjectRef,
    RelationshipCardinality,
)


def _assert_no_inline_entity_comments(rendered: str) -> None:
    assert "{ %%" not in rendered
    assert not any(line.strip().startswith('"') and "%%" in line for line in rendered.splitlines())


def _column(name: str, logical_name: str, data_type: str = "NUMBER") -> SchemaColumn:
    return SchemaColumn(column_name=name, logical_name=logical_name, data_type=data_type)


def _catalog() -> SchemaCatalog:
    return SchemaCatalog(
        refreshed_at="2026-07-11T00:00:00+00:00",
        tables=[
            SchemaTable(
                owner="SALES",
                table_name="CUSTOMERS",
                logical_name="顧客",
                columns=[
                    _column("CUSTOMER_ID", "顧客ID"),
                    _column("CUSTOMER_NAME", "顧客名", "VARCHAR2"),
                ],
            ),
            SchemaTable(
                owner="CRM",
                table_name="CUSTOMERS",
                logical_name="CRM顧客",
                columns=[_column("CUSTOMER_ID", "顧客ID")],
            ),
            SchemaTable(
                owner="SALES",
                table_name="ORDERS",
                logical_name="注文",
                columns=[_column("ORDER_ID", "注文ID"), _column("CUSTOMER_ID", "顧客ID")],
                constraint_details=[
                    SchemaConstraintDetail(
                        constraint_name="FK_ORDERS_CUSTOMER",
                        constraint_type="R",
                        owner="SALES",
                        table_name="ORDERS",
                        columns=["CUSTOMER_ID"],
                        referenced_owner="SALES",
                        referenced_table="CUSTOMERS",
                        referenced_columns=["CUSTOMER_ID"],
                    )
                ],
            ),
            SchemaTable(
                owner="SALES",
                table_name="ORDER_SUMMARY",
                logical_name="注文サマリー",
                table_type="view",
                columns=[_column("ORDER_ID", "注文ID")],
            ),
        ],
        view_dependencies=[
            SchemaViewDependency(
                owner="SALES",
                view_name="ORDER_SUMMARY",
                referenced_owner="SALES",
                referenced_name="ORDERS",
                referenced_type="TABLE",
            )
        ],
    )


def _business_entity(ontology: SchemaOntology, status: OntologyReviewStatus) -> OntologyNode:
    return OntologyNode(
        id="business_entity:orders",
        revision_id=ontology.revision.id,
        kind=OntologyNodeKind.BUSINESS_ENTITY,
        business_name_ja="受注",
        physical_mappings=[
            PhysicalMapping(
                object_ref=PhysicalObjectRef(
                    owner="SALES", object_name="ORDERS", object_type="table"
                )
            )
        ],
        provenance=OntologyProvenance(source_kind=OntologySourceKind.MANUAL),
        review_status=status,
    )


def _business_relationship(ontology: SchemaOntology, status: OntologyReviewStatus) -> OntologyEdge:
    orders = next(node for node in ontology.nodes if node.technical_name == "SALES.ORDERS")
    summary = next(node for node in ontology.nodes if node.technical_name == "SALES.ORDER_SUMMARY")
    return OntologyEdge(
        id="business_relationship:orders-summary",
        revision_id=ontology.revision.id,
        kind=OntologyEdgeKind.BUSINESS_RELATIONSHIP,
        source_node_id=orders.id,
        target_node_id=summary.id,
        relationship_name_ja="集計対象",
        cardinality=RelationshipCardinality.ONE_TO_MANY,
        join_conditions=[
            JoinCondition(
                left=PhysicalColumnRef(owner="SALES", object_name="ORDERS", column_name="ORDER_ID"),
                right=PhysicalColumnRef(
                    owner="SALES", object_name="ORDER_SUMMARY", column_name="ORDER_ID"
                ),
            )
        ],
        provenance=OntologyProvenance(source_kind=OntologySourceKind.MANUAL),
        review_status=status,
    )


def test_render_is_deterministic_and_contains_entities_fk_and_attributes() -> None:
    ontology = build_schema_ontology(_catalog())

    first = render_mermaid_er(ontology)
    second = render_mermaid_er(ontology)

    assert first == second
    assert first.startswith("erDiagram")
    assert '"SALES.ORDERS"' in first
    assert '"CRM.CUSTOMERS"' in first
    # FK は many-to-one 記法 + join 条件付きラベル
    assert '"SALES.ORDERS" }o--|| "SALES.CUSTOMERS"' in first
    assert "ORDERS.CUSTOMER_ID = CUSTOMERS.CUSTOMER_ID" in first
    # FK マーカーと論理名コメント
    assert "NUMBER CUSTOMER_ID FK" in first
    assert '"顧客ID"' in first
    assert 'VARCHAR2 CUSTOMER_NAME "顧客名"' in first
    # Entity block のインラインコメントは Mermaid ER parser が受け付けないため出さない。
    _assert_no_inline_entity_comments(first)
    assert "%% 注文 = SALES.ORDERS" in first


def test_render_sanitizes_mermaid_attribute_type_and_name_tokens() -> None:
    catalog = SchemaCatalog(
        refreshed_at="2026-07-11T00:00:00+00:00",
        tables=[
            SchemaTable(
                owner="SALES",
                table_name="ODD_COLUMNS",
                logical_name="特殊列",
                columns=[
                    _column("売上#額$", "売上#額$", "NUMBER(10,2)"),
                    _column("1ST-RANK", "先頭数字列", "VARCHAR2(30 CHAR)"),
                ],
            )
        ],
    )
    rendered = render_mermaid_er(build_schema_ontology(catalog))

    assert "NUMBER_10_2 COLUMN_" in rendered
    assert "VARCHAR2_30_CHAR COLUMN_1ST_RANK" in rendered
    assert "NUMBER(10,2) 売上#額$" not in rendered
    assert '"売上#額$"' in rendered
    attribute_lines = [
        line.strip()
        for line in rendered.splitlines()
        if line.startswith("        ") and not line.strip().startswith("%%")
    ]
    assert attribute_lines
    assert all(
        re.match(r"^[A-Za-z_][A-Za-z0-9_]* [A-Za-z_][A-Za-z0-9_]*(?: FK)?(?: \".*\")?$", line)
        for line in attribute_lines
    )


def test_profile_view_scopes_entities() -> None:
    ontology = build_schema_ontology(_catalog())
    profile = Nl2SqlProfile(
        id="sales",
        name="営業",
        allowed_tables=["SALES.ORDERS", "SALES.CUSTOMERS"],
    )
    view = migrate_profile_ontology_view(profile, ontology)

    rendered = render_mermaid_er(ontology, view)

    assert '"SALES.ORDERS"' in rendered
    assert '"SALES.CUSTOMERS"' in rendered
    assert "CRM.CUSTOMERS" not in rendered
    assert "ORDER_SUMMARY" not in rendered
    assert '"SALES.ORDERS" }o--|| "SALES.CUSTOMERS"' in rendered
    _assert_no_inline_entity_comments(rendered)


def test_only_approved_business_relationships_and_entities_are_rendered() -> None:
    ontology = build_schema_ontology(_catalog())
    approved = ontology.model_copy(
        update={
            "nodes": [*ontology.nodes, _business_entity(ontology, OntologyReviewStatus.APPROVED)],
            "edges": [
                *ontology.edges,
                _business_relationship(ontology, OntologyReviewStatus.APPROVED),
            ],
        },
        deep=True,
    )
    proposed = ontology.model_copy(
        update={
            "nodes": [*ontology.nodes, _business_entity(ontology, OntologyReviewStatus.PROPOSED)],
            "edges": [
                *ontology.edges,
                _business_relationship(ontology, OntologyReviewStatus.PROPOSED),
            ],
        },
        deep=True,
    )

    rendered_approved = render_mermaid_er(approved)
    rendered_proposed = render_mermaid_er(proposed)

    assert '"SALES.ORDERS" ||--o{ "SALES.ORDER_SUMMARY" : "集計対象' in rendered_approved
    assert "%% 受注 = SALES.ORDERS" in rendered_approved
    assert "集計対象" not in rendered_proposed
    assert "%% 受注" not in rendered_proposed


def test_max_entities_and_max_chars_degrade_gracefully() -> None:
    ontology = build_schema_ontology(_catalog())

    limited = render_mermaid_er(ontology, max_entities=1)
    assert "%% omitted: 3 entities" in limited

    compact = render_mermaid_er(ontology, max_chars=600)
    assert len(compact) <= 600 or compact.endswith("%% truncated")
    _assert_no_inline_entity_comments(compact)
    # 属性を落としても関係(構造)は優先して残す
    full = render_mermaid_er(ontology)
    assert "NUMBER CUSTOMER_ID" in full

    truncated = render_mermaid_er(ontology, max_chars=120)
    assert truncated.endswith("%% truncated")
    assert len(truncated) <= 120 + len("\n    %% truncated")


def test_generation_context_prompt_includes_er_diagram_and_hash_ignores_it() -> None:
    from app.features.nl2sql.ontology_models import OntologySqlGenerationContext
    from app.features.nl2sql.service import nl2sql_service

    base_payload = {
        "session_id": "session-1",
        "profile_id": "sales",
        "profile_view_id": "view-1",
        "ontology_revision_id": "revision-1",
        "intent_version": 1,
        "question_effective": "受注件数",
        "context_hash": "hash-1",
    }
    # 旧 payload(mermaid_er 無し)もそのまま validate できる(後方互換)。
    legacy = OntologySqlGenerationContext.model_validate(base_payload)
    assert legacy.mermaid_er == ""
    prompt_without = nl2sql_service._ontology_generation_context_prompt(legacy)
    assert "er_diagram(mermaid):" not in prompt_without

    enriched = OntologySqlGenerationContext.model_validate(
        {**base_payload, "mermaid_er": 'erDiagram\n    "SALES.ORDERS"'}
    )
    assert enriched.context_hash == legacy.context_hash
    prompt_with = nl2sql_service._ontology_generation_context_prompt(enriched)
    assert "er_diagram(mermaid):" in prompt_with
    assert '"SALES.ORDERS"' in prompt_with
    assert prompt_with.index("er_diagram(mermaid):") < prompt_with.index("rules:")
