"""NL2SQL Ontology core の AST、version、確認 gate の単体テスト。"""

from __future__ import annotations

import pytest

from app.features.nl2sql.ontology_models import (
    ColumnQueryPolicy,
    GraphPatch,
    GraphPatchOperation,
    IntentAmbiguity,
    IntentDimension,
    IntentEntity,
    IntentFilter,
    IntentMetric,
    IntentRelationshipPath,
    IntentSort,
    IntentTimeRange,
    JoinCondition,
    JoinType,
    OntologyEdge,
    OntologyEdgeKind,
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
    QuerySession,
    QuerySessionCreate,
    QuestionIntentGraph,
    RelationshipCardinality,
    RelationshipDirection,
    SqlConfirmationRequest,
)
from app.features.nl2sql.ontology_service import (
    OntologyGateBlockedError,
    OntologyIntegrityError,
    OntologyQuerySessionService,
    OntologyVersionConflictError,
)
from app.features.nl2sql.sql_semantics import parse_oracle_sql


def _registered_service(
    *,
    column_policies: dict[str, ColumnQueryPolicy] | None = None,
) -> tuple[OntologyQuerySessionService, ProfileOntologyView]:
    service = OntologyQuerySessionService()
    provenance = OntologyProvenance(
        source_kind=OntologySourceKind.MANUAL,
        source_id="test",
    )
    physical = PhysicalObjectRef(
        node_id="node_orders_table",
        owner="APP",
        object_name="ORDERS",
        object_type="table",
    )
    nodes = [
        OntologyNode(
            id="node_orders_table",
            revision_id="revision_1",
            kind=OntologyNodeKind.TABLE,
            technical_name="APP.ORDERS",
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
            aliases=["注文"],
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
            physical_mappings=[
                PhysicalMapping(
                    object_ref=physical,
                    column_refs=[
                        PhysicalColumnRef(
                            node_id="node_amount_column",
                            owner="APP",
                            object_name="ORDERS",
                            column_name="AMOUNT",
                        )
                    ],
                )
            ],
            provenance=provenance,
            review_status=OntologyReviewStatus.APPROVED,
        ),
        OntologyNode(
            id="node_customer_dimension",
            revision_id="revision_1",
            kind=OntologyNodeKind.PROPERTY,
            technical_name="customer_id",
            business_name_ja="顧客",
            physical_mappings=[
                PhysicalMapping(
                    object_ref=physical,
                    column_refs=[
                        PhysicalColumnRef(
                            node_id="node_customer_id_column",
                            owner="APP",
                            object_name="ORDERS",
                            column_name="CUSTOMER_ID",
                        )
                    ],
                )
            ],
            provenance=provenance,
            review_status=OntologyReviewStatus.APPROVED,
        ),
        OntologyNode(
            id="node_status_property",
            revision_id="revision_1",
            kind=OntologyNodeKind.PROPERTY,
            technical_name="status",
            business_name_ja="受注状態",
            physical_mappings=[
                PhysicalMapping(
                    object_ref=physical,
                    column_refs=[
                        PhysicalColumnRef(
                            node_id="node_status_column",
                            owner="APP",
                            object_name="ORDERS",
                            column_name="STATUS",
                        )
                    ],
                )
            ],
            provenance=provenance,
            review_status=OntologyReviewStatus.APPROVED,
        ),
        OntologyNode(
            id="node_sold_at_property",
            revision_id="revision_1",
            kind=OntologyNodeKind.PROPERTY,
            technical_name="sold_at",
            business_name_ja="受注日",
            physical_mappings=[
                PhysicalMapping(
                    object_ref=physical,
                    column_refs=[
                        PhysicalColumnRef(
                            node_id="node_sold_at_column",
                            owner="APP",
                            object_name="ORDERS",
                            column_name="SOLD_AT",
                        )
                    ],
                )
            ],
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
    view = ProfileOntologyView(
        id="view_sales",
        profile_id="profile_sales",
        ontology_revision_id="revision_1",
        node_ids=[node.id for node in nodes],
        physical_objects=[physical],
        column_policies=column_policies or {},
    )
    service.register_profile_view(view)
    return service, view


def _registered_composite_join_service() -> tuple[
    OntologyQuerySessionService,
    QuestionIntentGraph,
]:
    service = OntologyQuerySessionService()
    provenance = OntologyProvenance(
        source_kind=OntologySourceKind.MANUAL,
        source_id="test",
    )
    order_ref = PhysicalObjectRef(
        node_id="node_orders_table",
        owner="APP",
        object_name="ORDERS",
        object_type="table",
    )
    line_ref = PhysicalObjectRef(
        node_id="node_lines_table",
        owner="APP",
        object_name="ORDER_LINES",
        object_type="table",
    )
    nodes = [
        OntologyNode(
            id="node_orders_table",
            revision_id="revision_join",
            kind=OntologyNodeKind.TABLE,
            technical_name="APP.ORDERS",
            business_name_ja="受注テーブル",
            provenance=provenance,
            review_status=OntologyReviewStatus.APPROVED,
        ),
        OntologyNode(
            id="node_lines_table",
            revision_id="revision_join",
            kind=OntologyNodeKind.TABLE,
            technical_name="APP.ORDER_LINES",
            business_name_ja="受注明細テーブル",
            provenance=provenance,
            review_status=OntologyReviewStatus.APPROVED,
        ),
        OntologyNode(
            id="node_orders_entity",
            revision_id="revision_join",
            kind=OntologyNodeKind.BUSINESS_ENTITY,
            technical_name="orders",
            business_name_ja="受注",
            physical_mappings=[PhysicalMapping(object_ref=order_ref)],
            provenance=provenance,
            review_status=OntologyReviewStatus.APPROVED,
        ),
        OntologyNode(
            id="node_lines_entity",
            revision_id="revision_join",
            kind=OntologyNodeKind.BUSINESS_ENTITY,
            technical_name="order_lines",
            business_name_ja="受注明細",
            physical_mappings=[PhysicalMapping(object_ref=line_ref)],
            provenance=provenance,
            review_status=OntologyReviewStatus.APPROVED,
        ),
    ]
    edge = OntologyEdge(
        id="edge_orders_lines",
        revision_id="revision_join",
        kind=OntologyEdgeKind.FOREIGN_KEY,
        source_node_id="node_orders_entity",
        target_node_id="node_lines_entity",
        relationship_name_ja="受注と明細",
        direction=RelationshipDirection.DIRECTED,
        cardinality=RelationshipCardinality.ONE_TO_MANY,
        join_conditions=[
            JoinCondition(
                left=PhysicalColumnRef(
                    owner="APP",
                    object_name="ORDERS",
                    column_name="TENANT_ID",
                ),
                right=PhysicalColumnRef(
                    owner="APP",
                    object_name="ORDER_LINES",
                    column_name="TENANT_ID",
                ),
                operator="=",
                ordinal=1,
            ),
            JoinCondition(
                left=PhysicalColumnRef(
                    owner="APP",
                    object_name="ORDERS",
                    column_name="ORDER_ID",
                ),
                right=PhysicalColumnRef(
                    owner="APP",
                    object_name="ORDER_LINES",
                    column_name="ORDER_ID",
                ),
                operator="=",
                ordinal=2,
            ),
        ],
        allowed_join_types=[JoinType.INNER],
        provenance=provenance,
        review_status=OntologyReviewStatus.APPROVED,
    )
    service.register_revision(
        OntologyRevision(
            id="revision_join",
            version=1,
            status=OntologyRevisionStatus.PUBLISHED,
        ),
        nodes=nodes,
        edges=[edge],
    )
    view = ProfileOntologyView(
        id="view_join",
        profile_id="profile_join",
        ontology_revision_id="revision_join",
        node_ids=[node.id for node in nodes],
        edge_ids=[edge.id],
        physical_objects=[order_ref, line_ref],
        allowed_path_ids=[edge.id],
    )
    service.register_profile_view(view)
    intent = QuestionIntentGraph(
        question_original="受注と明細を確認",
        question_effective="受注と明細を確認",
        profile_view_id=view.id,
        ontology_revision_id="revision_join",
        entities=[
            IntentEntity(
                id="intent_orders",
                ontology_node_id="node_orders_entity",
                name_ja="受注",
                physical_object_ids=[order_ref.node_id],
            ),
            IntentEntity(
                id="intent_lines",
                ontology_node_id="node_lines_entity",
                name_ja="受注明細",
                physical_object_ids=[line_ref.node_id],
            ),
        ],
        candidate_paths=[
            IntentRelationshipPath(
                id="path_orders_lines",
                name_ja="受注と明細",
                edge_ids=[edge.id],
                node_ids=["node_orders_entity", "node_lines_entity"],
                approved=True,
            )
        ],
        selected_path_id="path_orders_lines",
        limit=20,
        confidence=1.0,
    )
    return service, intent


def _confirmed_intent() -> QuestionIntentGraph:
    return QuestionIntentGraph(
        question_original="顧客別の受注金額合計を表示",
        question_effective="顧客別の受注金額合計を表示",
        profile_view_id="view_sales",
        ontology_revision_id="revision_1",
        entities=[
            IntentEntity(
                id="intent_orders",
                ontology_node_id="node_orders_entity",
                name_ja="受注",
                physical_object_ids=["node_orders_table"],
            )
        ],
        metrics=[
            IntentMetric(
                id="intent_total_amount",
                ontology_node_id="node_amount_metric",
                name_ja="受注金額合計",
                aggregation="SUM",
            )
        ],
        dimensions=[
            IntentDimension(
                id="intent_customer",
                ontology_node_id="node_customer_dimension",
                name_ja="顧客",
            )
        ],
        limit=100,
        confidence=1.0,
    )


def _session_ready_for_sql(service: OntologyQuerySessionService) -> str:
    session = service.create_session(
        QuerySessionCreate(
            question="顧客別の受注金額合計を表示",
            profile_id="profile_sales",
            profile_view_id="view_sales",
            ontology_revision_id="revision_1",
            intent=_confirmed_intent(),
        )
    )
    service.confirm_intent(session.id, intent_version=1)
    return session.id


def _confirmation_for_latest(session: QuerySession) -> SqlConfirmationRequest:
    artifact = session.sql_artifacts[-1]
    return SqlConfirmationRequest(
        artifact_id=artifact.id,
        ontology_revision_id=artifact.ontology_revision_id,
        intent_version=artifact.intent_version,
        sql_hash=artifact.sql_hash,
        validation_hash=artifact.validation_report.validation_hash,
        generation_context_hash=artifact.generation_context_hash,
    )


def test_sql_semantic_graph_covers_cte_subquery_join_window_and_all_clauses() -> None:
    sql = """
        WITH paid_orders AS (
            SELECT customer_id, amount, sold_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY customer_id ORDER BY sold_at DESC
                   ) AS rn
            FROM APP.ORDERS
            WHERE status = 'PAID'
        )
        SELECT c.name, SUM(p.amount) AS total_amount
        FROM APP.CUSTOMERS c
        JOIN paid_orders p ON p.customer_id = c.id
        WHERE EXISTS (
            SELECT 1 FROM APP.CUSTOMER_FLAGS f
            WHERE f.customer_id = c.id AND f.active = 1
        )
        GROUP BY c.name
        HAVING SUM(p.amount) > 1000
        ORDER BY total_amount DESC
        FETCH FIRST 20 ROWS ONLY
    """

    analysis = parse_oracle_sql(sql, ontology_revision_id="revision_1")

    assert analysis.validation.is_valid is True
    assert analysis.graph is not None
    graph = analysis.graph
    assert [cte.name.upper() for cte in graph.ctes] == ["PAID_ORDERS"]
    assert any(table.is_cte and table.name.upper() == "PAID_ORDERS" for table in graph.tables)
    assert len(graph.subqueries) == 1
    assert len(graph.windows) == 1
    assert graph.joins and graph.joins[0].condition_sql
    assert graph.joins[0].is_cartesian is False
    assert graph.filters and graph.having and graph.groups and graph.orders
    assert graph.aggregates and graph.limit == 20
    assert {column.clause for column in graph.columns} >= {
        "select",
        "join",
        "where",
        "group",
        "having",
        "order",
        "window",
    }
    assert graph.lineage


def test_sql_semantic_graph_binds_star_join_endpoints_from_on_condition() -> None:
    """star join では ON 句が結ぶ表を左端点にする(直前の join の右表ではない)。"""
    star = parse_oracle_sql(
        'SELECT "D"."DEPARTMENT_ID", "E"."EMPLOYEE_ID", "P"."PROJECT_ID" '
        'FROM "ADMIN"."DEPARTMENT" "D" '
        'JOIN "ADMIN"."EMPLOYEE" "E" ON "E"."DEPARTMENT_ID" = "D"."DEPARTMENT_ID" '
        'JOIN "ADMIN"."PROJECT" "P" ON "P"."DEPARTMENT_ID" = "D"."DEPARTMENT_ID"',
        ontology_revision_id="revision_1",
    )
    assert star.graph is not None
    assert [(join.left_source, join.right_source) for join in star.graph.joins] == [
        ("ADMIN.DEPARTMENT D", "ADMIN.EMPLOYEE E"),
        ("ADMIN.DEPARTMENT D", "ADMIN.PROJECT P"),
    ]

    # 別名なしの star join でも表名 qualifier から解決する。
    unaliased = parse_oracle_sql(
        "SELECT 1 FROM ADMIN.DEPARTMENT "
        "JOIN ADMIN.EMPLOYEE ON EMPLOYEE.DEPARTMENT_ID = DEPARTMENT.DEPARTMENT_ID "
        "JOIN ADMIN.PROJECT ON PROJECT.DEPARTMENT_ID = DEPARTMENT.DEPARTMENT_ID",
        ontology_revision_id="revision_1",
    )
    assert unaliased.graph is not None
    assert [join.left_source for join in unaliased.graph.joins] == [
        "ADMIN.DEPARTMENT",
        "ADMIN.DEPARTMENT",
    ]

    # 左深 chain は従来どおり。
    chain = parse_oracle_sql(
        "SELECT 1 FROM APP.T1 X JOIN APP.T2 Y ON Y.ID = X.ID JOIN APP.T3 Z ON Z.ID = Y.ID",
        ontology_revision_id="revision_1",
    )
    assert chain.graph is not None
    assert [join.left_source for join in chain.graph.joins] == ["APP.T1 X", "APP.T2 Y"]


def test_sql_semantic_graph_keeps_positional_join_endpoints_when_unresolvable() -> None:
    """USING / CROSS / 複数表を跨ぐ ON は解決できないため位置ベースへ戻す。"""
    using = parse_oracle_sql(
        "SELECT 1 FROM APP.T1 X JOIN APP.T2 Y USING (ID) JOIN APP.T3 Z USING (ID)",
        ontology_revision_id="revision_1",
    )
    assert using.graph is not None
    assert [join.left_source for join in using.graph.joins] == ["APP.T1 X", "APP.T2 Y"]

    crossed = parse_oracle_sql(
        "SELECT 1 FROM APP.T1 X CROSS JOIN APP.T2 Y",
        ontology_revision_id="revision_1",
    )
    assert crossed.graph is not None
    assert [join.left_source for join in crossed.graph.joins] == ["APP.T1 X"]

    multi = parse_oracle_sql(
        "SELECT 1 FROM APP.T1 X JOIN APP.T2 Y ON Y.ID = X.ID "
        "JOIN APP.T3 Z ON Z.ID = Y.ID AND Z.CODE = X.CODE",
        ontology_revision_id="revision_1",
    )
    assert multi.graph is not None
    assert [join.left_source for join in multi.graph.joins] == ["APP.T1 X", "APP.T2 Y"]


def test_sql_semantic_graph_covers_union_and_rejects_incomplete_or_mutating_sql() -> None:
    union = parse_oracle_sql(
        "SELECT id FROM APP.CUSTOMERS UNION ALL SELECT customer_id FROM APP.ORDERS",
        ontology_revision_id="revision_1",
    )
    assert union.graph is not None
    assert [operation.operator for operation in union.graph.set_operations] == ["union_all"]

    malformed = parse_oracle_sql("SELECT FROM", ontology_revision_id="revision_1")
    assert malformed.graph is None
    assert malformed.validation.blocker_count == 1
    assert malformed.validation.findings[0].code == "SQL_PARSE_FAILED"

    mutation = parse_oracle_sql("DELETE FROM APP.ORDERS", ontology_revision_id="revision_1")
    assert mutation.graph is None
    assert mutation.validation.findings[0].code == "SQL_NOT_READ_ONLY_QUERY"


def test_intent_patch_uses_optimistic_version_and_invalidates_confirmation() -> None:
    service, _ = _registered_service()
    session = service.create_session(
        QuerySessionCreate(
            question="顧客別の受注金額合計を表示",
            profile_id="profile_sales",
            profile_view_id="view_sales",
            ontology_revision_id="revision_1",
            intent=_confirmed_intent(),
        )
    )
    patch = GraphPatch(
        base_version=1,
        operations=[
            GraphPatchOperation(
                op="replace",
                path="/question_effective",
                value="顧客別の受注金額合計を上位 50 件表示",
            ),
            GraphPatchOperation(op="replace", path="/limit", value=50),
        ],
    )

    updated = service.apply_intent_patch(session.id, patch)

    assert updated.current_intent_version == 2
    assert updated.intents[-1].limit == 50
    assert updated.intent_confirmed_version is None
    with pytest.raises(OntologyVersionConflictError) as exc_info:
        service.apply_intent_patch(session.id, patch)
    assert exc_info.value.code == "INTENT_VERSION_CONFLICT"


def test_intent_confirmation_rejects_patched_physical_mapping_outside_profile() -> None:
    service, _ = _registered_service()
    session = service.create_session(
        QuerySessionCreate(
            question="顧客別の受注金額合計を表示",
            profile_id="profile_sales",
            profile_view_id="view_sales",
            ontology_revision_id="revision_1",
            intent=_confirmed_intent(),
        )
    )
    updated = service.apply_intent_patch(
        session.id,
        GraphPatch(
            base_version=1,
            operations=[
                GraphPatchOperation(
                    op="replace",
                    path="/entities/0/physical_object_ids",
                    value=[],
                )
            ],
        ),
    )

    with pytest.raises(OntologyGateBlockedError) as exc_info:
        service.confirm_intent(session.id, intent_version=updated.current_intent_version)

    assert exc_info.value.code == "INTENT_SCOPE_INVALID"
    assert exc_info.value.finding_codes == ["INTENT_ENTITY_MAPPING_MISSING"]


def test_unresolved_ambiguity_is_a_hard_intent_gate() -> None:
    service, _ = _registered_service()
    intent = _confirmed_intent().model_copy(deep=True)
    intent.ambiguities = [
        IntentAmbiguity(
            id="ambiguity_metric",
            code="METRIC_DEFINITION_AMBIGUOUS",
            message_ja="売上を税抜と税込のどちらで集計するか不明です。",
            options=["税抜", "税込"],
        )
    ]
    session = service.create_session(
        QuerySessionCreate(
            question="顧客別の売上を表示",
            profile_id="profile_sales",
            profile_view_id="view_sales",
            ontology_revision_id="revision_1",
            intent=intent,
        )
    )

    with pytest.raises(OntologyGateBlockedError) as exc_info:
        service.confirm_intent(session.id, intent_version=1)
    assert exc_info.value.code == "INTENT_AMBIGUITY_UNRESOLVED"
    assert exc_info.value.finding_codes == ["METRIC_DEFINITION_AMBIGUOUS"]


def test_deterministic_fallback_marks_unknown_business_meaning_as_blocking() -> None:
    service, _ = _registered_service()
    session = service.create_session(
        QuerySessionCreate(
            question="定義されていない概念を集計して",
            profile_id="profile_sales",
            profile_view_id="view_sales",
            ontology_revision_id="revision_1",
        )
    )

    assert session.intents[-1].ambiguities[0].code == "BUSINESS_MEANING_NOT_IDENTIFIED"
    with pytest.raises(OntologyGateBlockedError):
        service.confirm_intent(session.id, intent_version=1)


def test_sql_confirmation_and_execution_detect_hash_tampering() -> None:
    service, _ = _registered_service()
    session_id = _session_ready_for_sql(service)
    generated = service.register_generated_sql(
        session_id,
        """
        SELECT customer_id, SUM(amount) AS total_amount
        FROM APP.ORDERS
        GROUP BY customer_id
        FETCH FIRST 100 ROWS ONLY
        """,
    )
    artifact = generated.sql_artifacts[-1]
    assert artifact.validation_report.is_valid is True
    request = _confirmation_for_latest(generated)

    tampered_request = request.model_copy(update={"sql_hash": "0" * 64})
    with pytest.raises(OntologyIntegrityError) as exc_info:
        service.confirm_sql(session_id, tampered_request)
    assert exc_info.value.code == "CONFIRMATION_BINDING_MISMATCH"

    service.confirm_sql(session_id, request)
    with pytest.raises(OntologyIntegrityError) as exc_info:
        service.authorize_execution(
            session_id,
            request,
            sql=artifact.sql + " -- replaced after confirmation",
        )
    assert exc_info.value.code == "SQL_HASH_MISMATCH"

    binding = service.authorize_execution(session_id, request, sql=artifact.sql)
    assert binding.sql_hash == artifact.sql_hash
    done = service.complete_execution(session_id, row_count=3, result_ref="result:test")
    assert done.status.value == "done"
    assert done.execution is not None and done.execution.row_count == 3


def test_three_way_gate_does_not_accept_same_table_name_from_another_owner() -> None:
    service, _ = _registered_service()
    session_id = _session_ready_for_sql(service)

    generated = service.register_generated_sql(
        session_id,
        """
        SELECT customer_id, SUM(amount) AS total_amount
        FROM OTHER.ORDERS
        GROUP BY customer_id
        FETCH FIRST 100 ROWS ONLY
        """,
    )

    report = generated.sql_artifacts[-1].validation_report
    assert report.is_valid is False
    assert "SQL_OBJECT_OUTSIDE_PROFILE" in {finding.code for finding in report.findings}


def test_three_way_gate_blocks_select_wildcard_when_column_scope_is_unknown() -> None:
    service, _ = _registered_service()
    session_id = _session_ready_for_sql(service)

    generated = service.register_generated_sql(
        session_id,
        "SELECT * FROM APP.ORDERS FETCH FIRST 100 ROWS ONLY",
    )

    report = generated.sql_artifacts[-1].validation_report
    assert report.is_valid is False
    assert "SQL_WILDCARD_COLUMN_SCOPE_UNKNOWN" in {finding.code for finding in report.findings}


@pytest.mark.parametrize(
    "join_condition",
    [
        "o.tenant_id = l.order_id AND o.order_id = l.tenant_id",
        "o.order_id = l.order_id AND o.tenant_id = l.tenant_id",
        "o.tenant_id = l.tenant_id AND o.order_id >= l.order_id",
    ],
    ids=["swapped-pairs", "wrong-ordinal", "wrong-operator"],
)
def test_composite_join_requires_exact_pairs_order_and_operator(
    join_condition: str,
) -> None:
    service, intent = _registered_composite_join_service()
    session = service.create_session(
        QuerySessionCreate(
            question=intent.question_original,
            profile_id="profile_join",
            profile_view_id="view_join",
            ontology_revision_id="revision_join",
            intent=intent,
        )
    )
    service.confirm_intent(session.id, intent_version=1)

    generated = service.register_generated_sql(
        session.id,
        f"""
        SELECT o.order_id
        FROM APP.ORDERS o
        JOIN APP.ORDER_LINES l ON {join_condition}
        FETCH FIRST 20 ROWS ONLY
        """,
    )

    codes = {item.code for item in generated.sql_artifacts[-1].validation_report.findings}
    assert "SQL_JOIN_CONDITION_NOT_APPROVED" in codes


def test_composite_join_accepts_the_approved_ordered_column_pairs() -> None:
    service, intent = _registered_composite_join_service()
    session = service.create_session(
        QuerySessionCreate(
            question=intent.question_original,
            profile_id="profile_join",
            profile_view_id="view_join",
            ontology_revision_id="revision_join",
            intent=intent,
        )
    )
    service.confirm_intent(session.id, intent_version=1)

    generated = service.register_generated_sql(
        session.id,
        """
        SELECT o.order_id
        FROM APP.ORDERS o
        JOIN APP.ORDER_LINES l
          ON o.tenant_id = l.tenant_id AND o.order_id = l.order_id
        FETCH FIRST 20 ROWS ONLY
        """,
    )

    report = generated.sql_artifacts[-1].validation_report
    assert report.is_valid is True
    assert "SQL_JOIN_CONDITION_NOT_APPROVED" not in {item.code for item in report.findings}


@pytest.mark.parametrize(
    ("where_clause", "expected_code"),
    [
        ("", "SQL_INTENT_FILTER_MISSING"),
        (
            "WHERE status = 'PAID' AND amount > 0",
            "SQL_UNREQUESTED_FILTER_ADDED",
        ),
        ("WHERE status = 'CANCELLED'", "SQL_INTENT_FILTER_MISMATCH"),
    ],
    ids=["missing", "extra", "wrong-value"],
)
def test_intent_filters_match_target_column_operator_and_value(
    where_clause: str,
    expected_code: str,
) -> None:
    service, _ = _registered_service()
    intent = _confirmed_intent().model_copy(deep=True)
    intent.filters = [
        IntentFilter(
            id="intent_paid",
            property_node_id="node_status_property",
            label_ja="支払済み",
            operator="=",
            value="PAID",
            value_type="string",
        )
    ]
    session = service.create_session(
        QuerySessionCreate(
            question=intent.question_original,
            profile_id="profile_sales",
            profile_view_id="view_sales",
            ontology_revision_id="revision_1",
            intent=intent,
        )
    )
    service.confirm_intent(session.id, intent_version=1)

    generated = service.register_generated_sql(
        session.id,
        f"""
        SELECT customer_id, SUM(amount) AS total_amount
        FROM APP.ORDERS
        {where_clause}
        GROUP BY customer_id
        FETCH FIRST 100 ROWS ONLY
        """,
    )

    codes = {item.code for item in generated.sql_artifacts[-1].validation_report.findings}
    assert expected_code in codes


def test_metric_function_and_sort_direction_are_compared_per_target() -> None:
    service, _ = _registered_service()
    intent = _confirmed_intent().model_copy(deep=True)
    intent.sorts = [IntentSort(target_id="intent_total_amount", direction="desc")]
    session = service.create_session(
        QuerySessionCreate(
            question=intent.question_original,
            profile_id="profile_sales",
            profile_view_id="view_sales",
            ontology_revision_id="revision_1",
            intent=intent,
        )
    )
    service.confirm_intent(session.id, intent_version=1)

    generated = service.register_generated_sql(
        session.id,
        """
        SELECT customer_id, AVG(amount) AS total_amount
        FROM APP.ORDERS
        GROUP BY customer_id
        ORDER BY total_amount ASC
        FETCH FIRST 100 ROWS ONLY
        """,
    )

    codes = {item.code for item in generated.sql_artifacts[-1].validation_report.findings}
    assert "SQL_METRIC_AGGREGATION_MISMATCH" in codes
    assert "SQL_INTENT_SORT_MISMATCH" in codes


def test_dimension_group_is_compared_to_the_intended_target_column() -> None:
    service, _ = _registered_service()
    session_id = _session_ready_for_sql(service)

    generated = service.register_generated_sql(
        session_id,
        """
        SELECT status, SUM(amount) AS total_amount
        FROM APP.ORDERS
        GROUP BY status
        FETCH FIRST 100 ROWS ONLY
        """,
    )

    codes = {item.code for item in generated.sql_artifacts[-1].validation_report.findings}
    assert "SQL_DIMENSION_GRAIN_MISSING" in codes
    assert "SQL_UNREQUESTED_GROUP_ADDED" in codes


def test_time_range_compares_target_bound_operators_and_values() -> None:
    service, _ = _registered_service()
    intent = _confirmed_intent().model_copy(deep=True)
    intent.time_range = IntentTimeRange(
        property_node_id="node_sold_at_property",
        start="2026-01-01",
        end="2026-01-31",
    )
    session = service.create_session(
        QuerySessionCreate(
            question=intent.question_original,
            profile_id="profile_sales",
            profile_view_id="view_sales",
            ontology_revision_id="revision_1",
            intent=intent,
        )
    )
    service.confirm_intent(session.id, intent_version=1)

    generated = service.register_generated_sql(
        session.id,
        """
        SELECT customer_id, SUM(amount) AS total_amount
        FROM APP.ORDERS
        WHERE sold_at >= '2026-01-01' AND sold_at <= '2026-01-30'
        GROUP BY customer_id
        FETCH FIRST 100 ROWS ONLY
        """,
    )

    codes = {item.code for item in generated.sql_artifacts[-1].validation_report.findings}
    assert "SQL_TIME_FILTER_MISMATCH" in codes


@pytest.mark.parametrize(
    ("policy_key", "policy", "expected_code"),
    [
        (
            "node_amount_metric",
            ColumnQueryPolicy(queryable=False, aggregatable=True),
            "SQL_COLUMN_NOT_QUERYABLE",
        ),
        (
            "total_amount",
            ColumnQueryPolicy(aggregatable=False),
            "SQL_COLUMN_NOT_AGGREGATABLE",
        ),
        (
            "node_customer_dimension",
            ColumnQueryPolicy(groupable=False),
            "SQL_COLUMN_NOT_GROUPABLE",
        ),
        (
            "node_status_property",
            ColumnQueryPolicy(required_filter=True),
            "SQL_REQUIRED_FILTER_MISSING",
        ),
    ],
    ids=["queryable-node-id", "aggregatable-technical-name", "groupable", "required"],
)
def test_column_policies_are_enforced_by_clause(
    policy_key: str,
    policy: ColumnQueryPolicy,
    expected_code: str,
) -> None:
    service, _ = _registered_service(column_policies={policy_key: policy})
    session_id = _session_ready_for_sql(service)

    generated = service.register_generated_sql(
        session_id,
        """
        SELECT customer_id, SUM(amount) AS total_amount
        FROM APP.ORDERS
        GROUP BY customer_id
        FETCH FIRST 100 ROWS ONLY
        """,
    )

    codes = {item.code for item in generated.sql_artifacts[-1].validation_report.findings}
    assert expected_code in codes


def test_filterable_policy_blocks_filter_on_exact_target_column() -> None:
    service, _ = _registered_service(
        column_policies={"status": ColumnQueryPolicy(filterable=False)}
    )
    intent = _confirmed_intent().model_copy(deep=True)
    intent.filters = [
        IntentFilter(
            id="intent_paid",
            property_node_id="node_status_property",
            label_ja="支払済み",
            value="PAID",
        )
    ]
    session = service.create_session(
        QuerySessionCreate(
            question=intent.question_original,
            profile_id="profile_sales",
            profile_view_id="view_sales",
            ontology_revision_id="revision_1",
            intent=intent,
        )
    )
    service.confirm_intent(session.id, intent_version=1)

    generated = service.register_generated_sql(
        session.id,
        """
        SELECT customer_id, SUM(amount) AS total_amount
        FROM APP.ORDERS
        WHERE status = 'PAID'
        GROUP BY customer_id
        FETCH FIRST 100 ROWS ONLY
        """,
    )

    codes = {item.code for item in generated.sql_artifacts[-1].validation_report.findings}
    assert "SQL_COLUMN_NOT_FILTERABLE" in codes
