"""Ontology contract から OWL 2 RL / SHACL Core / LLM 文脈を決定論的に生成する。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .ontology_catalog import SchemaOntology
from .ontology_mermaid import render_mermaid_er
from .ontology_models import (
    BusinessRuleExecutionMode,
    BusinessRuleExpression,
    BusinessRuleSeverity,
    OntologyEdgeKind,
    OntologyNode,
    OntologyNodeKind,
    OntologyReviewStatus,
    ProfileOntologyView,
)

ONTOLOGY_RENDERER_VERSION = "ontology-semantic-renderer/2"
_PREFIXES = """@prefix ont: <urn:nl2sql:ontology:> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix prov: <http://www.w3.org/ns/prov#> .
"""

_NODE_KIND_LABELS_JA: dict[OntologyNodeKind, str] = {
    OntologyNodeKind.BUSINESS_ENTITY: "業務エンティティ",
    OntologyNodeKind.BUSINESS_EVENT: "業務イベント",
    OntologyNodeKind.PROPERTY: "属性",
    OntologyNodeKind.METRIC: "指標",
    OntologyNodeKind.BUSINESS_TERM: "業務用語",
    OntologyNodeKind.BUSINESS_RULE: "業務ルール",
    OntologyNodeKind.ENUM_VALUE: "列挙値",
}

_EDGE_KIND_LABELS_JA: dict[OntologyEdgeKind, str] = {
    OntologyEdgeKind.FOREIGN_KEY: "外部キー",
    OntologyEdgeKind.BUSINESS_RELATIONSHIP: "業務関係",
    OntologyEdgeKind.JOINS: "Join",
    OntologyEdgeKind.LINK_TYPE: "リンク型（Link Type）",
}


def _node_kind_label_ja(kind: OntologyNodeKind) -> str:
    from .ontology_terms import CONCEPT_LABELS

    return CONCEPT_LABELS.get(kind.value, _NODE_KIND_LABELS_JA.get(kind, kind.value))


def _edge_kind_label_ja(kind: OntologyEdgeKind) -> str:
    return _EDGE_KIND_LABELS_JA.get(kind, kind.value)


@dataclass(frozen=True)
class OntologySemanticArtifacts:
    owl_turtle: str
    shacl_turtle: str
    llm_markdown: str
    mermaid: str
    hashes: dict[str, str]


@dataclass(frozen=True)
class ShaclValidationResult:
    conforms: bool
    report_text: str
    report_turtle: str


def stable_node_iri(node_id: str) -> str:
    return f"<urn:nl2sql:ontology:node:{quote(node_id, safe='-_')}>"


def stable_edge_iri(edge_id: str) -> str:
    return f"<urn:nl2sql:ontology:edge:{quote(edge_id, safe='-_')}>"


def revision_graph_names(revision_id: str) -> tuple[str, str]:
    digest = hashlib.sha256(revision_id.encode("utf-8")).hexdigest()[:16].upper()
    return f"ONT_{digest}", f"INF_{digest}"


def _literal(value: Any, data_type: str = "string") -> str:
    if isinstance(value, bool) or data_type == "boolean":
        return f'"{str(bool(value)).lower()}"^^xsd:boolean'
    if isinstance(value, int) or data_type == "integer":
        return f'"{int(value)}"^^xsd:integer'
    if isinstance(value, float) or data_type == "number":
        return f'"{value}"^^xsd:decimal'
    escaped = json.dumps("" if value is None else str(value), ensure_ascii=False)
    if data_type == "date":
        return f"{escaped}^^xsd:date"
    if data_type == "datetime":
        return f"{escaped}^^xsd:dateTime"
    return escaped


def _label_lines(subject: str, node: OntologyNode) -> list[str]:
    lines = [f"{subject} rdfs:label {_literal(node.business_name_ja)} ."]
    if node.description_ja:
        lines.append(f"{subject} rdfs:comment {_literal(node.description_ja)} .")
    for alias in sorted(set(node.aliases)):
        if alias:
            lines.append(f"{subject} skos:altLabel {_literal(alias)} .")
    return lines


def serialize_owl_turtle(ontology: SchemaOntology) -> str:
    from .ontology_graph_semantics import REFERENCE_PREDICATES
    from .ontology_typed_rdf import typed_rdf_lines

    lines = [_PREFIXES.rstrip(), ""]
    nodes = [node for node in ontology.nodes if node.review_status == OntologyReviewStatus.APPROVED]
    node_by_id = {node.id: node for node in nodes}
    for node in sorted(nodes, key=lambda item: item.id):
        subject = stable_node_iri(node.id)
        node_type = {
            OntologyNodeKind.BUSINESS_ENTITY: "owl:Class",
            OntologyNodeKind.OBJECT_TYPE: "owl:Class",
            OntologyNodeKind.INTERFACE: "owl:Class",
            OntologyNodeKind.FUNCTION: "ont:Function",
            OntologyNodeKind.ACTION_TYPE: "ont:ActionType",
            OntologyNodeKind.SHARED_PROPERTY: "owl:DatatypeProperty",
            OntologyNodeKind.VALUE_TYPE: "rdfs:Datatype",
            OntologyNodeKind.ENUMERATION: "skos:ConceptScheme",
            OntologyNodeKind.OBJECT_SET: "ont:ObjectSet",
            OntologyNodeKind.BUSINESS_EVENT: "owl:Class",
            OntologyNodeKind.PROPERTY: "owl:DatatypeProperty",
            OntologyNodeKind.ENUM_VALUE: "owl:NamedIndividual, skos:Concept",
            OntologyNodeKind.METRIC: "ont:Metric",
            OntologyNodeKind.BUSINESS_RULE: "ont:BusinessRule",
            OntologyNodeKind.BUSINESS_TERM: "skos:Concept",
            OntologyNodeKind.SCHEMA: "ont:Schema",
            OntologyNodeKind.TABLE: "ont:Table",
            OntologyNodeKind.VIEW: "ont:View",
            OntologyNodeKind.COLUMN: "ont:Column",
        }.get(node.kind)
        if node_type is None:
            continue
        definition = node.metadata.get("definition")
        definition = definition if isinstance(definition, dict) else {}
        if (
            node.kind in {OntologyNodeKind.PROPERTY, OntologyNodeKind.SHARED_PROPERTY}
            and definition.get("data_type") == "object"
        ):
            node_type = "owl:ObjectProperty"
        if node.kind == OntologyNodeKind.VALUE_TYPE and definition.get("data_type") == "object":
            node_type = "ont:ValueType"
        lines.append(f"{subject} rdf:type {node_type} .")
        lines.extend(_label_lines(subject, node))
        if node.enum_value_definition is not None:
            definition = node.enum_value_definition
            lines.append(f"{subject} ont:code {_literal(definition.code)} .")
            lines.append(
                f"{subject} ont:physicalLiteral "
                f"{_literal(definition.physical_literal, definition.data_type)} ."
            )
            lines.append(
                f"{subject} ont:enumProperty {stable_node_iri(definition.property_node_id)} ."
            )
        lines.append("")

    for edge in sorted(ontology.edges, key=lambda item: item.id):
        if edge.review_status != OntologyReviewStatus.APPROVED:
            continue
        if edge.source_node_id not in node_by_id or edge.target_node_id not in node_by_id:
            continue
        source = stable_node_iri(edge.source_node_id)
        target = stable_node_iri(edge.target_node_id)
        semantic_predicate = edge.metadata.get("semantic_predicate")
        if edge.metadata.get("reference_definition_kind") == "link_type":
            continue  # 辺の定義への参照は typed_rdf_lines が直接出力する。
        if isinstance(semantic_predicate, str) and semantic_predicate in {
            *REFERENCE_PREDICATES.values(),
            "skos:hasTopConcept",
            "rdfs:domain",
        }:
            lines.append(f"{source} {semantic_predicate} {target} .")
        elif edge.kind == OntologyEdgeKind.IS_A:
            lines.append(f"{source} rdfs:subClassOf {target} .")
        elif edge.kind == OntologyEdgeKind.DOMAIN:
            lines.append(f"{source} rdfs:domain {target} .")
        elif edge.kind == OntologyEdgeKind.RANGE:
            lines.append(f"{source} rdfs:range {target} .")
        elif edge.kind == OntologyEdgeKind.INSTANCE_OF:
            lines.append(f"{source} rdf:type {target} .")
        elif edge.kind == OntologyEdgeKind.HAS_VALUE:
            lines.append(f"{source} ont:hasValue {target} .")
        elif edge.kind == OntologyEdgeKind.GOVERNS:
            lines.append(f"{source} ont:governs {target} .")
        elif edge.kind == OntologyEdgeKind.MAPS_TO:
            lines.append(f"{source} ont:mapsTo {target} .")
        elif edge.kind == OntologyEdgeKind.USES:
            lines.append(f"{source} ont:uses {target} .")
        elif edge.kind in {OntologyEdgeKind.BUSINESS_RELATIONSHIP, OntologyEdgeKind.LINK_TYPE}:
            predicate = stable_edge_iri(edge.id)
            lines.append(f"{predicate} rdf:type owl:ObjectProperty .")
            lines.append(f"{predicate} rdfs:label {_literal(edge.relationship_name_ja)} .")
            lines.append(f"{predicate} rdfs:domain {source} .")
            lines.append(f"{predicate} rdfs:range {target} .")
    lines.extend(typed_rdf_lines(ontology))
    return "\n".join(lines).rstrip() + "\n"


def _shape_for_expression(expression: BusinessRuleExpression) -> str:
    """固定 AST を SHACL Core の inline blank shape へ変換する。"""

    property_iri = (
        stable_node_iri(expression.property_node_id) if expression.property_node_id else ""
    )
    if expression.operator in {"all", "any"}:
        predicate = "sh:and" if expression.operator == "all" else "sh:or"
        children = " ".join(_shape_for_expression(child) for child in expression.children)
        return f"[ {predicate} ( {children} ) ]"
    if expression.operator == "not" and expression.children:
        return f"[ sh:not {_shape_for_expression(expression.children[0])} ]"
    constraints = [f"sh:path {property_iri}"]
    if expression.operator == "eq":
        constraints.append(f"sh:hasValue {_literal(expression.value)}")
    elif expression.operator == "in":
        constraints.append(f"sh:in ( {' '.join(_literal(item) for item in expression.values)} )")
    elif expression.operator == "is_null":
        constraints.append("sh:maxCount 0")
    elif expression.operator == "not_null":
        constraints.append("sh:minCount 1")
    elif expression.operator in {"lt", "lte", "gt", "gte"}:
        predicate = {
            "lt": "maxExclusive",
            "lte": "maxInclusive",
            "gt": "minExclusive",
            "gte": "minInclusive",
        }[expression.operator]
        constraints.append(f"sh:{predicate} {_literal(expression.value)}")
    elif expression.operator in {"ne", "not_in"}:
        inner = (
            f"sh:hasValue {_literal(expression.value)}"
            if expression.operator == "ne"
            else f"sh:in ( {' '.join(_literal(item) for item in expression.values)} )"
        )
        constraints.append(f"sh:not [ {inner} ]")
    return f"[ sh:property [ {' ; '.join(constraints)} ] ]"


def serialize_shacl_turtle(ontology: SchemaOntology) -> str:
    from .ontology_typed_rdf import typed_shacl_lines

    lines = [_PREFIXES.rstrip(), ""]
    approved_nodes = {
        node.id: node
        for node in ontology.nodes
        if node.review_status == OntologyReviewStatus.APPROVED
    }
    domain_by_property = {
        edge.source_node_id: edge.target_node_id
        for edge in ontology.edges
        if edge.kind == OntologyEdgeKind.DOMAIN
        and edge.review_status == OntologyReviewStatus.APPROVED
    }
    enum_by_property: dict[str, list[OntologyNode]] = {}
    for node in approved_nodes.values():
        if node.enum_value_definition is not None and not node.metadata.get(
            "derived_from_definition_id"
        ):
            enum_by_property.setdefault(node.enum_value_definition.property_node_id, []).append(
                node
            )
    for property_id, enum_nodes in sorted(enum_by_property.items()):
        domain_id = domain_by_property.get(property_id)
        if not domain_id or domain_id not in approved_nodes:
            continue
        shape = f"ont:EnumShape_{hashlib.sha256(property_id.encode()).hexdigest()[:12]}"
        allowed = " ".join(
            _literal(
                item.enum_value_definition.physical_literal, item.enum_value_definition.data_type
            )
            for item in sorted(enum_nodes, key=lambda value: value.id)
            if item.enum_value_definition is not None
        )
        lines.extend(
            [
                f"{shape} rdf:type sh:NodeShape ;",
                f"    sh:targetClass {stable_node_iri(domain_id)} ;",
                "    sh:property [",
                f"        sh:path {stable_node_iri(property_id)} ;",
                f"        sh:in ( {allowed} )",
                "    ] .",
                "",
            ]
        )
    for node in sorted(approved_nodes.values(), key=lambda item: item.id):
        definition = node.business_rule_definition
        if (
            definition is None
            or definition.execution_mode != BusinessRuleExecutionMode.SHACL
            or definition.expression is None
        ):
            continue
        target_id = definition.applies_to_node_ids[0]
        if target_id not in approved_nodes:
            continue
        shape = f"ont:RuleShape_{hashlib.sha256(node.id.encode()).hexdigest()[:12]}"
        expression_shape = _shape_for_expression(definition.expression)
        severity = {
            BusinessRuleSeverity.VIOLATION: "sh:Violation",
            BusinessRuleSeverity.WARNING: "sh:Warning",
            BusinessRuleSeverity.INFO: "sh:Info",
        }[definition.severity]
        lines.extend(
            [
                f"{shape} rdf:type sh:NodeShape ;",
                f"    sh:targetClass {stable_node_iri(target_id)} ;",
                f"    sh:message {_literal(definition.statement_ja)} ;",
                f"    sh:severity {severity} ;",
                f"    sh:and ( {expression_shape} ) .",
                "",
            ]
        )
    lines.extend(typed_shacl_lines(ontology))
    return "\n".join(lines).rstrip() + "\n"


def render_llm_markdown(ontology: SchemaOntology, view: ProfileOntologyView) -> str:
    node_by_id = {node.id: node for node in ontology.nodes}
    scoped_nodes = [
        node_by_id[node_id] for node_id in sorted(view.node_ids) if node_id in node_by_id
    ]
    scoped_edges = [
        edge
        for edge in sorted(ontology.edges, key=lambda item: item.id)
        if edge.id in set(view.edge_ids) and edge.review_status == OntologyReviewStatus.APPROVED
    ]
    lines = [
        "# NL2SQL オントロジーコンテキスト",
        "",
        f"- プロファイル: `{view.profile_id}`",
        f"- リビジョン: `{ontology.revision.id}`",
        f"- レンダラー: `{ONTOLOGY_RENDERER_VERSION}`",
        "",
        "## 適用場面",
    ]
    if view.activation_scenarios_ja:
        lines.extend(f"- {item}" for item in view.activation_scenarios_ja)
    else:
        lines.append("- 未設定")
    lines.extend(["", "## エンティティ・属性・列挙・ルール"])
    for node in scoped_nodes:
        if node.kind not in {
            OntologyNodeKind.BUSINESS_ENTITY,
            OntologyNodeKind.BUSINESS_EVENT,
            OntologyNodeKind.PROPERTY,
            OntologyNodeKind.BUSINESS_TERM,
            OntologyNodeKind.BUSINESS_RULE,
            OntologyNodeKind.ENUM_VALUE,
        }:
            continue
        aliases = f" (別名: {', '.join(sorted(set(node.aliases)))})" if node.aliases else ""
        lines.append(
            f"- [{_node_kind_label_ja(node.kind)}] {node.business_name_ja}{aliases} `#{node.id}`"
        )
        if node.description_ja:
            lines.append(f"  - {node.description_ja}")
        if node.enum_value_definition is not None:
            enum_definition = node.enum_value_definition
            lines.append(
                f"  - コード=`{enum_definition.code}` / "
                f"物理値=`{enum_definition.physical_literal}` / "
                f"属性=`#{enum_definition.property_node_id}`"
            )
        if node.business_rule_definition is not None:
            rule_definition = node.business_rule_definition
            lines.append(
                f"  - {rule_definition.statement_ja} "
                f"(重大度={rule_definition.severity.value}, "
                f"実行方式={rule_definition.execution_mode.value})"
            )
    lines.extend(["", "## 指標"])
    metric_nodes = [node for node in scoped_nodes if node.kind == OntologyNodeKind.METRIC]
    if not metric_nodes:
        lines.append("- なし")
    for node in metric_nodes:
        lines.append(f"- {node.business_name_ja} `#{node.id}`")
        metric_definition = node.metadata.get("metric_definition")
        if isinstance(metric_definition, dict) and metric_definition.get("expression_sql"):
            lines.append(f"  - 管理 SQL: `{metric_definition['expression_sql']}`")
    lines.extend(["", "## 承認済み関係・Join"])
    for edge in scoped_edges:
        source = node_by_id.get(edge.source_node_id)
        target = node_by_id.get(edge.target_node_id)
        if source is None or target is None:
            continue
        edge_label = _edge_kind_label_ja(edge.kind)
        lines.append(
            f"- {source.business_name_ja} --{edge.relationship_name_ja}/{edge_label}--> "
            f"{target.business_name_ja}"
        )
        for condition in edge.join_conditions:
            lines.append(
                f"  - `{condition.left.owner}.{condition.left.object_name}."
                f"{condition.left.column_name} {condition.operator} "
                f"{condition.right.owner}.{condition.right.object_name}."
                f"{condition.right.column_name}`"
            )
    shacl_rule_count = sum(
        node.business_rule_definition is not None
        and node.business_rule_definition.execution_mode == BusinessRuleExecutionMode.SHACL
        for node in scoped_nodes
    )
    enum_count = sum(node.kind == OntologyNodeKind.ENUM_VALUE for node in scoped_nodes)
    lines.extend(
        [
            "",
            "## SHACL 摘要",
            "- 公開時に SHACL Core 検証済み。",
            f"- 制約ルール: {shacl_rule_count} / 列挙値: {enum_count}",
            "",
            "## Mermaid",
            "```mermaid",
            render_mermaid_er(ontology, view).rstrip(),
            "```",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def build_semantic_artifacts(
    ontology: SchemaOntology,
    view: ProfileOntologyView | None = None,
) -> OntologySemanticArtifacts:
    owl_turtle = serialize_owl_turtle(ontology)
    shacl_turtle = serialize_shacl_turtle(ontology)
    if view is None:
        view = ProfileOntologyView(
            id=f"all:{ontology.revision.id}",
            profile_id="all",
            ontology_revision_id=ontology.revision.id,
            node_ids=[node.id for node in ontology.nodes],
            edge_ids=[edge.id for edge in ontology.edges],
        )
    llm_markdown = render_llm_markdown(ontology, view)
    mermaid = render_mermaid_er(ontology, view)
    values = {
        "owl_turtle": owl_turtle,
        "shacl_turtle": shacl_turtle,
        "llm_markdown": llm_markdown,
        "mermaid": mermaid,
    }
    hashes = {
        key: hashlib.sha256(value.encode("utf-8")).hexdigest() for key, value in values.items()
    }
    return OntologySemanticArtifacts(**values, hashes=hashes)


def materialize_local_owl2rl(asserted_turtle: str) -> str:
    from owlrl import DeductiveClosure, OWLRL_Semantics  # type: ignore[import-untyped]
    from rdflib import Graph
    from rdflib.compare import to_canonical_graph

    graph = Graph()
    graph.parse(data=asserted_turtle, format="turtle")
    DeductiveClosure(OWLRL_Semantics).expand(graph)
    canonical = to_canonical_graph(graph)
    # N-Triples は Turtle の部分集合。canonical BNode と行 sort で artifact hash を安定化する。
    return (
        "\n".join(
            sorted(
                f"{subject.n3()} {predicate.n3()} {object_.n3()} ."
                for subject, predicate, object_ in canonical
            )
        )
        + "\n"
    )


def validate_shacl_core(
    *,
    asserted_turtle: str,
    inferred_turtle: str,
    shapes_turtle: str,
) -> ShaclValidationResult:
    from pyshacl import validate
    from rdflib import Graph

    data_graph = Graph()
    data_graph.parse(data=asserted_turtle, format="turtle")
    data_graph.parse(data=inferred_turtle, format="turtle")
    shapes_graph = Graph()
    shapes_graph.parse(data=shapes_turtle, format="turtle")
    conforms, report_graph, report_text = validate(
        data_graph,
        shacl_graph=shapes_graph,
        inference="none",
        advanced=False,
        js=False,
        meta_shacl=True,
        abort_on_first=False,
        allow_infos=True,
        allow_warnings=True,
    )
    report_turtle = str(report_graph.serialize(format="turtle"))
    return ShaclValidationResult(bool(conforms), str(report_text), report_turtle)
