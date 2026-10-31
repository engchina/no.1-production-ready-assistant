from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from rdflib import Graph, URIRef
from rdflib.namespace import RDF

from app.features.nl2sql.ontology_catalog import SchemaOntology
from app.features.nl2sql.ontology_models import (
    BusinessRuleDefinition,
    BusinessRuleExecutionMode,
    BusinessRuleExpression,
    BusinessRuleKind,
    BusinessRuleSeverity,
    EnumValueDefinition,
    OntologyEdge,
    OntologyEdgeKind,
    OntologyNode,
    OntologyNodeKind,
    OntologyProvenance,
    OntologyReviewStatus,
    OntologyRevision,
    OntologyRevisionStatus,
    OntologySourceKind,
    ProfileOntologyView,
)
from app.features.nl2sql.ontology_reasoning import (
    LocalOwl2RlMaterializer,
    OntologyPublishService,
)
from app.features.nl2sql.ontology_semantics import (
    build_semantic_artifacts,
    materialize_local_owl2rl,
    revision_graph_names,
    stable_edge_iri,
    stable_node_iri,
    validate_shacl_core,
)
from app.settings import get_settings


def _provenance() -> OntologyProvenance:
    return OntologyProvenance(source_kind=OntologySourceKind.MANUAL)


def _node(node_id: str, kind: OntologyNodeKind, name: str, **values: object) -> OntologyNode:
    return OntologyNode(
        id=node_id,
        revision_id="rev-semantic-1",
        kind=kind,
        business_name_ja=name,
        provenance=_provenance(),
        review_status=OntologyReviewStatus.APPROVED,
        **values,
    )


def _edge(edge_id: str, kind: OntologyEdgeKind, source: str, target: str) -> OntologyEdge:
    return OntologyEdge(
        id=edge_id,
        revision_id="rev-semantic-1",
        kind=kind,
        source_node_id=source,
        target_node_id=target,
        relationship_name_ja=kind.value,
        provenance=_provenance(),
        review_status=OntologyReviewStatus.APPROVED,
    )


def _semantic_ontology(
    *, severity: BusinessRuleSeverity = BusinessRuleSeverity.VIOLATION
) -> tuple[SchemaOntology, ProfileOntologyView]:
    nodes = [
        _node("entity-parent", OntologyNodeKind.BUSINESS_ENTITY, "取引"),
        _node("entity-order", OntologyNodeKind.BUSINESS_ENTITY, "受注"),
        _node("property-status", OntologyNodeKind.PROPERTY, "受注状態"),
        _node(
            "enum-open",
            OntologyNodeKind.ENUM_VALUE,
            "受付中",
            enum_value_definition=EnumValueDefinition(
                code="OPEN",
                label_ja="受付中",
                physical_literal="OPEN",
                property_node_id="property-status",
            ),
        ),
        _node(
            "enum-closed",
            OntologyNodeKind.ENUM_VALUE,
            "完了",
            enum_value_definition=EnumValueDefinition(
                code="CLOSED",
                label_ja="完了",
                physical_literal="CLOSED",
                property_node_id="property-status",
            ),
        ),
        _node(
            "rule-status",
            OntologyNodeKind.BUSINESS_RULE,
            "状態必須ルール",
            business_rule_definition=BusinessRuleDefinition(
                rule_kind=BusinessRuleKind.VALIDATION,
                statement_ja="受注状態は必須です。",
                applies_to_node_ids=["entity-order"],
                severity=severity,
                execution_mode=BusinessRuleExecutionMode.SHACL,
                expression=BusinessRuleExpression(
                    operator="not_null", property_node_id="property-status"
                ),
            ),
        ),
    ]
    edges = [
        _edge("edge-is-a", OntologyEdgeKind.IS_A, "entity-order", "entity-parent"),
        _edge("edge-domain", OntologyEdgeKind.DOMAIN, "property-status", "entity-order"),
        _edge("edge-instance", OntologyEdgeKind.INSTANCE_OF, "enum-open", "entity-order"),
    ]
    ontology = SchemaOntology(
        revision=OntologyRevision(
            id="rev-semantic-1",
            version=1,
            status=OntologyRevisionStatus.DRAFT,
            etag="etag-1",
        ),
        nodes=nodes,
        edges=edges,
    )
    view = ProfileOntologyView(
        id="view-sales",
        profile_id="sales",
        ontology_revision_id=ontology.revision.id,
        node_ids=[node.id for node in nodes],
        edge_ids=[edge.id for edge in edges],
        activation_scenarios_ja=["受注状況を分析する"],
    )
    return ontology, view


def test_semantic_artifacts_are_deterministic_and_use_stable_iris() -> None:
    ontology, view = _semantic_ontology()
    first = build_semantic_artifacts(ontology, view)
    second = build_semantic_artifacts(ontology, view)

    assert first == second
    assert stable_node_iri("entity-order") in first.owl_turtle
    assert "rdfs:subClassOf" in first.owl_turtle
    assert 'sh:in ( "CLOSED" "OPEN" )' in first.shacl_turtle
    assert "sh:minCount 1" in first.shacl_turtle
    assert "sh:sparql" not in first.shacl_turtle.lower()
    assert "swrl" not in first.owl_turtle.lower()
    assert "## エンティティ・属性・列挙・ルール" in first.llm_markdown
    assert "## 指標" in first.llm_markdown
    assert "## SHACL 摘要" in first.llm_markdown
    assert "## Mermaid" in first.llm_markdown
    assert "# NL2SQL オントロジーコンテキスト" in first.llm_markdown
    assert (
        first.hashes["owl_turtle"] == hashlib.sha256(first.owl_turtle.encode("utf-8")).hexdigest()
    )
    assert revision_graph_names(ontology.revision.id) == (
        "ONT_1735E05224FDA9C1",
        "INF_1735E05224FDA9C1",
    )


def test_owl2rl_materializes_class_and_domain_range_inference() -> None:
    ontology, view = _semantic_ontology()
    artifacts = build_semantic_artifacts(ontology, view)
    asserted = (
        artifacts.owl_turtle
        + f"<urn:test:order-1> rdf:type {stable_node_iri('entity-order')} .\n"
        + f"<urn:test:order-1> {stable_edge_iri('edge-rel')} <urn:test:item-1> .\n"
        + f"{stable_edge_iri('edge-rel')} rdf:type owl:ObjectProperty ; "
        + f"rdfs:domain {stable_node_iri('entity-order')} ; "
        + f"rdfs:range {stable_node_iri('entity-parent')} .\n"
    )
    first_closure = materialize_local_owl2rl(asserted)
    assert first_closure == materialize_local_owl2rl(asserted)
    closure = Graph().parse(data=first_closure, format="turtle")

    assert (
        URIRef("urn:test:order-1"),
        RDF.type,
        URIRef("urn:nl2sql:ontology:node:entity-parent"),
    ) in closure
    assert (
        URIRef("urn:test:item-1"),
        RDF.type,
        URIRef("urn:nl2sql:ontology:node:entity-parent"),
    ) in closure


def test_shacl_violation_blocks_but_warning_is_non_blocking() -> None:
    ontology, view = _semantic_ontology()
    artifacts = build_semantic_artifacts(ontology, view)
    instance = f"<urn:test:order-1> rdf:type {stable_node_iri('entity-order')} .\n"
    asserted = artifacts.owl_turtle + instance
    inferred = materialize_local_owl2rl(asserted)

    violation = validate_shacl_core(
        asserted_turtle=asserted,
        inferred_turtle=inferred,
        shapes_turtle=artifacts.shacl_turtle,
    )
    assert not violation.conforms
    assert "Violation" in violation.report_text

    warning_ontology, warning_view = _semantic_ontology(severity=BusinessRuleSeverity.WARNING)
    warning_artifacts = build_semantic_artifacts(warning_ontology, warning_view)
    warning = validate_shacl_core(
        asserted_turtle=warning_artifacts.owl_turtle + instance,
        inferred_turtle=materialize_local_owl2rl(warning_artifacts.owl_turtle + instance),
        shapes_turtle=warning_artifacts.shacl_turtle,
    )
    assert warning.conforms
    assert "Warning" in warning.report_text


def test_rule_contract_rejects_arbitrary_or_misclassified_execution() -> None:
    with pytest.raises(ValidationError):
        BusinessRuleExpression.model_validate(
            {"operator": "sparql", "property_node_id": "property-status"}
        )
    with pytest.raises(ValidationError):
        BusinessRuleDefinition(
            rule_kind=BusinessRuleKind.CALCULATION,
            statement_ja="任意計算",
            applies_to_node_ids=["entity-order"],
            execution_mode=BusinessRuleExecutionMode.SHACL,
            expression=BusinessRuleExpression(
                operator="eq", property_node_id="property-status", value="OPEN"
            ),
        )
    with pytest.raises(ValidationError):
        BusinessRuleDefinition.model_validate(
            {
                "rule_kind": "validation",
                "statement_ja": "任意規則",
                "applies_to_node_ids": ["entity-order"],
                "execution_mode": "shacl",
                "expression": {
                    "operator": "eq",
                    "property_node_id": "property-status",
                    "value": "OPEN",
                    "sparql": "ASK {}",
                },
            }
        )


def test_publish_service_rejects_non_owl2rl_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_reasoning_profile", "user_rules")

    with pytest.raises(RuntimeError, match="OWL 2 RL"):
        OntologyPublishService(SimpleNamespace(store=SimpleNamespace()))


def test_oracle_store_uses_local_materializer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_ontology_reasoning_profile", "owl2rl")
    runtime = SimpleNamespace(
        store=SimpleNamespace(mode="oracle"),
        legacy_service=SimpleNamespace(),
    )

    publisher = OntologyPublishService(runtime)

    assert isinstance(publisher._materializer, LocalOwl2RlMaterializer)
