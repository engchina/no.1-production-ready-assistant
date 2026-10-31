from __future__ import annotations

from collections.abc import Sequence

import pytest

from app.features.nl2sql.models import (
    Nl2SqlProfile,
    SchemaCatalog,
    SchemaColumn,
    SchemaConstraintDetail,
    SchemaTable,
    SchemaViewDependency,
)
from app.features.nl2sql.ontology_catalog import (
    AmbiguousPhysicalObjectError,
    SchemaOntology,
    build_schema_ontology,
    evolve_schema_ontology,
    find_bounded_shortest_paths,
    interpret_question_deterministically,
    migrate_profile_ontology_view,
    retrieve_ontology_nodes,
)
from app.features.nl2sql.ontology_models import (
    OntologyEdge,
    OntologyEdgeKind,
    OntologyNode,
    OntologyNodeKind,
    OntologyProvenance,
    OntologyReviewStatus,
    OntologyRevisionStatus,
    OntologySourceKind,
    PhysicalColumnRef,
    PhysicalMapping,
    PhysicalObjectRef,
)
from app.features.nl2sql.ontology_service import OntologyQuerySessionService
from app.features.nl2sql.ontology_store import stable_ontology_id, stable_physical_id


def _column(name: str, logical_name: str, data_type: str = "NUMBER") -> SchemaColumn:
    return SchemaColumn(
        column_name=name,
        logical_name=logical_name,
        data_type=data_type,
    )


def _catalog() -> SchemaCatalog:
    return SchemaCatalog(
        refreshed_at="2026-07-11T00:00:00+00:00",
        tables=[
            SchemaTable(
                owner="SALES",
                table_name="CUSTOMERS",
                logical_name="顧客",
                comment="販売先の顧客マスタ",
                columns=[
                    _column("TENANT_ID", "テナントID"),
                    _column("CUSTOMER_ID", "顧客ID"),
                    _column("CUSTOMER_NAME", "顧客名", "VARCHAR2"),
                ],
            ),
            SchemaTable(
                owner="CRM",
                table_name="CUSTOMERS",
                logical_name="顧客",
                comment="CRM の顧客マスタ",
                columns=[_column("CUSTOMER_ID", "顧客ID")],
            ),
            SchemaTable(
                owner="SALES",
                table_name="ORDERS",
                logical_name="注文",
                columns=[
                    _column("TENANT_ID", "テナントID"),
                    _column("ORDER_ID", "注文ID"),
                    _column("CUSTOMER_ID", "顧客ID"),
                ],
                constraint_details=[
                    SchemaConstraintDetail(
                        constraint_name="PK_ORDERS",
                        constraint_type="P",
                        owner="SALES",
                        table_name="ORDERS",
                        columns=["TENANT_ID", "ORDER_ID"],
                    ),
                    SchemaConstraintDetail(
                        constraint_name="FK_ORDERS_CUSTOMER",
                        constraint_type="R",
                        owner="SALES",
                        table_name="ORDERS",
                        columns=["CUSTOMER_ID"],
                        referenced_owner="SALES",
                        referenced_table="CUSTOMERS",
                        referenced_columns=["CUSTOMER_ID"],
                    ),
                ],
            ),
            SchemaTable(
                owner="SALES",
                table_name="ORDER_LINES",
                logical_name="注文明細",
                columns=[
                    _column("TENANT_ID", "テナントID"),
                    _column("ORDER_ID", "注文ID"),
                    _column("LINE_NO", "明細番号"),
                ],
                constraint_details=[
                    SchemaConstraintDetail(
                        constraint_name="FK_LINES_ORDER",
                        constraint_type="R",
                        owner="SALES",
                        table_name="ORDER_LINES",
                        columns=["TENANT_ID", "ORDER_ID"],
                        referenced_owner="SALES",
                        referenced_table="ORDERS",
                        referenced_columns=["TENANT_ID", "ORDER_ID"],
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


def _node(ontology: SchemaOntology, technical_name: str) -> OntologyNode:
    return next(node for node in ontology.nodes if node.technical_name == technical_name)


def test_builder_keeps_cross_schema_identity_composite_fk_and_view_lineage() -> None:
    ontology = build_schema_ontology(_catalog())

    sales_customers = _node(ontology, "SALES.CUSTOMERS")
    crm_customers = _node(ontology, "CRM.CUSTOMERS")
    assert sales_customers.id != crm_customers.id
    assert sales_customers.id == stable_physical_id("table", "SALES", "CUSTOMERS")
    assert ontology.revision.status == OntologyRevisionStatus.DRAFT
    assert ontology.revision.schema_fingerprint
    assert ontology.revision.etag

    composite = next(
        edge for edge in ontology.edges if edge.metadata.get("constraint_name") == "FK_LINES_ORDER"
    )
    assert composite.kind == OntologyEdgeKind.FOREIGN_KEY
    assert composite.review_status == OntologyReviewStatus.APPROVED
    assert [condition.ordinal for condition in composite.join_conditions] == [1, 2]
    assert [condition.left.column_name for condition in composite.join_conditions] == [
        "TENANT_ID",
        "ORDER_ID",
    ]
    assert [condition.right.column_name for condition in composite.join_conditions] == [
        "TENANT_ID",
        "ORDER_ID",
    ]

    lineage = next(edge for edge in ontology.edges if edge.kind == OntologyEdgeKind.LINEAGE)
    assert lineage.source_node_id == _node(ontology, "SALES.ORDER_SUMMARY").id
    assert lineage.target_node_id == _node(ontology, "SALES.ORDERS").id


def test_legacy_profile_migration_requires_owner_for_cross_schema_names() -> None:
    ontology = build_schema_ontology(_catalog())
    ambiguous = Nl2SqlProfile(
        id="ambiguous",
        name="曖昧",
        allowed_tables=["CUSTOMERS"],
    )

    with pytest.raises(AmbiguousPhysicalObjectError, match="複数 schema"):
        migrate_profile_ontology_view(ambiguous, ontology)

    profile = ambiguous.model_copy(
        update={"id": "sales", "allowed_tables": ["SALES.CUSTOMERS", "SALES.ORDERS"]}
    )
    view = migrate_profile_ontology_view(profile, ontology)

    assert {item.owner for item in view.physical_objects} == {"SALES"}
    assert {item.object_name for item in view.physical_objects} == {"CUSTOMERS", "ORDERS"}
    fk_edge = next(
        edge
        for edge in ontology.edges
        if edge.metadata.get("constraint_name") == "FK_ORDERS_CUSTOMER"
    )
    assert fk_edge.id in view.edge_ids
    assert fk_edge.id in view.allowed_path_ids


def test_schema_drift_preserves_business_definition_and_marks_column_mapping_orphaned() -> None:
    initial_catalog = SchemaCatalog(
        refreshed_at="2026-07-11T00:00:00+00:00",
        tables=[
            SchemaTable(
                owner="SALES",
                table_name="CUSTOMERS",
                logical_name="顧客",
                columns=[_column("CUSTOMER_ID", "顧客ID")],
            )
        ],
    )
    previous = build_schema_ontology(initial_catalog)
    object_id = stable_physical_id("table", "SALES", "CUSTOMERS")
    column_id = stable_physical_id("column", "SALES", "CUSTOMERS", "CUSTOMER_ID")
    business_id = stable_ontology_id("business_entity", "顧客")
    business_node = OntologyNode(
        id=business_id,
        revision_id=previous.revision.id,
        kind=OntologyNodeKind.BUSINESS_ENTITY,
        technical_name="customer",
        business_name_ja="顧客",
        physical_mappings=[
            PhysicalMapping(
                object_ref=PhysicalObjectRef(
                    node_id=object_id,
                    owner="SALES",
                    object_name="CUSTOMERS",
                ),
                column_refs=[
                    PhysicalColumnRef(
                        node_id=column_id,
                        owner="SALES",
                        object_name="CUSTOMERS",
                        column_name="CUSTOMER_ID",
                    )
                ],
            )
        ],
        provenance=OntologyProvenance(source_kind=OntologySourceKind.MANUAL),
        review_status=OntologyReviewStatus.APPROVED,
    )
    previous = previous.model_copy(update={"nodes": [*previous.nodes, business_node]})
    drifted_catalog = SchemaCatalog(
        refreshed_at="2026-07-12T00:00:00+00:00",
        tables=[
            SchemaTable(
                owner="SALES",
                table_name="CUSTOMERS",
                logical_name="顧客",
                columns=[_column("CUSTOMER_CODE", "顧客コード")],
            )
        ],
    )

    current = evolve_schema_ontology(drifted_catalog, previous)

    assert current.revision.version == previous.revision.version + 1
    assert current.revision.parent_revision_id == previous.revision.id
    assert current.revision.status == OntologyRevisionStatus.DRAFT
    preserved = next(node for node in current.nodes if node.id == business_id)
    assert preserved.business_name_ja == "顧客"
    assert preserved.review_status == OntologyReviewStatus.ORPHANED
    assert preserved.metadata["orphaned_mapping_node_ids"] == [column_id]


@pytest.mark.parametrize("removed", ["column", "table"])
@pytest.mark.parametrize("kind", [OntologyEdgeKind.MAPS_TO, OntologyEdgeKind.BUSINESS_RELATIONSHIP])
def test_schema_drift_preserves_missing_edge_endpoints_without_enabling_them(
    removed: str, kind: OntologyEdgeKind
) -> None:
    """参照切れの辺も登録でき、欠落した物理 node は利用範囲に戻らない。"""
    catalog = SchemaCatalog(
        refreshed_at="2026-10-03T00:00:00+00:00",
        tables=[
            SchemaTable(
                owner="APP", table_name="ORDERS", logical_name="受注", columns=[_column("ID", "ID")]
            )
        ],
    )
    previous = build_schema_ontology(catalog)
    object_node = _node(previous, "APP.ORDERS")
    target = _node(previous, "APP.ORDERS.ID") if removed == "column" else object_node
    business = OntologyNode(
        id="business_orders",
        revision_id=previous.revision.id,
        kind=OntologyNodeKind.BUSINESS_ENTITY,
        technical_name="orders",
        business_name_ja="受注",
        physical_mappings=target.physical_mappings,
        provenance=OntologyProvenance(source_kind=OntologySourceKind.MANUAL),
        review_status=OntologyReviewStatus.APPROVED,
    )
    edges = [
        OntologyEdge(
            id=f"mapping_{index}",
            revision_id=previous.revision.id,
            kind=kind,
            source_node_id=business.id if index == 0 else target.id,
            target_node_id=target.id if index == 0 else business.id,
            relationship_name_ja="物理対応",
            provenance=business.provenance,
            review_status=OntologyReviewStatus.APPROVED,
        )
        for index in range(2)
    ]
    previous = previous.model_copy(update={"nodes": [*previous.nodes, business], "edges": edges})
    drifted_catalog = catalog.model_copy(
        update={
            "tables": [catalog.tables[0].model_copy(update={"columns": []})]
            if removed == "column"
            else []
        }
    )
    current = evolve_schema_ontology(drifted_catalog, previous)
    sessions = OntologyQuerySessionService()
    sessions.register_revision(current.revision, nodes=current.nodes, edges=current.edges)

    retained = [node for node in current.nodes if node.id == target.id]
    assert len(retained) == 1
    assert retained[0].review_status == OntologyReviewStatus.ORPHANED
    assert retained[0].revision_id == current.revision.id
    assert retained[0].metadata["drift_from_revision_id"] == previous.revision.id
    for edge in current.edges:
        if edge.id in {item.id for item in edges}:
            assert edge.review_status == OntologyReviewStatus.ORPHANED
            assert edge.metadata["orphaned_mapping_node_ids"] == [target.id]
    assert target.review_status == OntologyReviewStatus.APPROVED

    profile = Nl2SqlProfile(id="sales", name="販売")
    view = migrate_profile_ontology_view(profile, current)
    assert target.id not in view.node_ids
    assert target.id not in {item.node_id for item in view.physical_objects}
    assert not view.allowed_path_ids
    assert target.id not in {hit.node_id for hit in retrieve_ontology_nodes("受注", current, view)}
    assert business.id not in {
        hit.node_id for hit in retrieve_ontology_nodes("受注", current, view)
    }
    # さらに drift しても履歴 node で参照切れを解消した扱いにしない。
    next_catalog = drifted_catalog.model_copy(
        update={
            "tables": [
                *drifted_catalog.tables,
                SchemaTable(owner="APP", table_name="OTHER", logical_name="別の表", columns=[]),
            ]
        }
    )
    next_ontology = evolve_schema_ontology(next_catalog, current)
    sessions.register_revision(
        next_ontology.revision, nodes=next_ontology.nodes, edges=next_ontology.edges
    )
    assert all(
        edge.metadata["orphaned_mapping_node_ids"] == [target.id]
        for edge in next_ontology.edges
        if edge.id in {item.id for item in edges}
    )


def test_shortest_path_and_interpreter_use_only_approved_profile_whitelist() -> None:
    ontology = build_schema_ontology(_catalog())
    profile = Nl2SqlProfile(
        id="sales",
        name="営業",
        allowed_tables=["SALES.CUSTOMERS", "SALES.ORDERS"],
    )
    view = migrate_profile_ontology_view(profile, ontology)
    customers = _node(ontology, "SALES.CUSTOMERS")
    orders = _node(ontology, "SALES.ORDERS")

    paths = find_bounded_shortest_paths(ontology, view, customers.id, orders.id)
    assert len(paths) == 1
    assert paths[0].approved is True

    intent = interpret_question_deterministically(
        "顧客と注文の件数を上位10件",
        ontology,
        view,
        profile=profile,
    )
    assert {entity.name_ja for entity in intent.entities} == {"顧客", "注文"}
    assert intent.metrics[0].aggregation == "COUNT"
    assert intent.limit == 10
    assert len(intent.candidate_paths) == 1
    assert intent.selected_path_id == intent.candidate_paths[0].id
    assert not intent.ambiguities

    forbidden = view.model_copy(update={"allowed_path_ids": []})
    blocked = interpret_question_deterministically(
        "顧客と注文の件数",
        ontology,
        forbidden,
        profile=profile,
    )
    assert not blocked.candidate_paths
    assert "join_path_not_approved" in {item.code for item in blocked.ambiguities}


def test_same_business_name_is_blocking_ambiguity_in_cross_schema_profile() -> None:
    ontology = build_schema_ontology(_catalog())
    profile = Nl2SqlProfile(
        id="all-customer",
        name="全顧客",
        allowed_tables=["SALES.CUSTOMERS", "CRM.CUSTOMERS"],
    )
    view = migrate_profile_ontology_view(profile, ontology)

    intent = interpret_question_deterministically(
        "顧客の件数",
        ontology,
        view,
        profile=profile,
    )

    assert not intent.entities
    ambiguity = next(item for item in intent.ambiguities if item.code == "ontology_term_ambiguous")
    assert ambiguity.blocking is True
    assert ambiguity.options == ["CRM.CUSTOMERS", "SALES.CUSTOMERS"]


def test_retrieval_uses_glossary_comment_and_optional_embedding_callback() -> None:
    catalog = SchemaCatalog(
        refreshed_at="2026-07-11T00:00:00+00:00",
        tables=[
            SchemaTable(
                owner="SALES",
                table_name="CUSTOMERS",
                logical_name="顧客マスタ",
                comment="契約先台帳",
                columns=[_column("CUSTOMER_ID", "顧客ID")],
            )
        ],
    )
    ontology = build_schema_ontology(catalog)
    profile = Nl2SqlProfile(
        id="sales",
        name="営業",
        allowed_tables=["SALES.CUSTOMERS"],
        glossary={"取引先": "顧客マスタ"},
    )
    view = migrate_profile_ontology_view(profile, ontology)
    customer = _node(ontology, "SALES.CUSTOMERS")

    glossary_hits = retrieve_ontology_nodes(
        "取引先の件数",
        ontology,
        view,
        profile=profile,
    )
    glossary_hit = next(hit for hit in glossary_hits if hit.node_id == customer.id)
    assert "glossary" in glossary_hit.sources

    comment_hits = retrieve_ontology_nodes("契約先台帳", ontology, view)
    assert customer.id in {hit.node_id for hit in comment_hits}

    callback_calls: list[tuple[str, int, int]] = []

    def embedding_callback(
        question: str,
        candidates: Sequence[OntologyNode],
        limit: int,
    ) -> Sequence[tuple[str, float]]:
        callback_calls.append((question, len(candidates), limit))
        return [(customer.id, 0.9)]

    embedding_hits = retrieve_ontology_nodes(
        "一致しない質問",
        ontology,
        view,
        embedding_callback=embedding_callback,
    )
    assert callback_calls
    assert embedding_hits[0].node_id == customer.id
    assert embedding_hits[0].sources == ["embedding"]


def test_catalog_edges_always_carry_machine_readable_kind() -> None:
    """frontend の graphView はエッジ分類を edge.kind で行う。ラベル文字列一致は
    旧データ互換のフォールバックであり、catalog 生成エッジは必ず kind を持つこと。"""

    ontology = build_schema_ontology(_catalog())
    assert ontology.edges, "catalog から少なくとも 1 本のエッジが生成されること"
    for edge in ontology.edges:
        assert str(edge.kind or "").strip(), f"edge {edge.id} に kind がありません"
