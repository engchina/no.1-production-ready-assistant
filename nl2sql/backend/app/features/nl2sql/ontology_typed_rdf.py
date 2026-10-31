"""型付き定義から RDF/SHACL を生成する。SQL を実行・推測変換しない。"""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter
from rdflib import Literal
from rdflib.namespace import XSD

from .ontology_catalog import SchemaOntology
from .ontology_definitions import BusinessDefinition
from .ontology_graph_semantics import XSD_TYPES, reference_semantics
from .ontology_models import OntologyEdge, OntologyNode
from .ontology_store import stable_ontology_id

DEFINITION: TypeAdapter[BusinessDefinition] = TypeAdapter(BusinessDefinition)


def typed_literal(value: Any, data_type: str) -> str:
    datatype = XSD_TYPES.get(data_type)
    return Literal(value, datatype=XSD[datatype] if datatype else None).n3()


def definition_nodes(ontology: SchemaOntology) -> dict[str, Any]:
    return {
        str(n.metadata["definition"]["api_name"]): n
        for n in ontology.nodes
        if n.review_status == "approved" and isinstance(n.metadata.get("definition"), dict)
    }


def typed_rdf_lines(ontology: SchemaOntology) -> list[str]:
    from .ontology_semantics import _literal, stable_edge_iri, stable_node_iri

    lines: list[str] = []
    node_by_name = definition_nodes(ontology)
    link_by_name = {
        str(e.metadata.get("api_name")): e
        for e in ontology.edges
        if e.kind == "link_type" and e.review_status == "approved"
    }
    items: list[OntologyNode | OntologyEdge] = [*ontology.nodes, *ontology.edges]
    for item in items:
        if item.review_status != "approved":
            continue
        definition = item.metadata.get("definition")
        if not isinstance(definition, dict):
            continue
        subject = (
            stable_edge_iri(item.id)
            if definition["kind"] == "link_type"
            else stable_node_iri(item.id)
        )
        kind = definition["kind"]
        datatype = XSD_TYPES.get(definition.get("data_type", ""))
        if datatype:
            if kind == "value_type":
                lines.append(f"{subject} owl:equivalentClass xsd:{datatype} .")
            elif kind in {"property", "shared_property"} and not definition.get("value_type"):
                lines.append(f"{subject} rdfs:range xsd:{datatype} .")
        for field, predicate in {
            "expression_sql": "expressionSql",
            "filter_sql": "filterSql",
            "predicate_sql": "predicateSql",
            "join_expression_sql": "joinExpressionSql",
            "unit": "unit",
            "currency": "currency",
            "aggregation": "aggregation",
            "null_policy_ja": "nullPolicy",
            "time_policy_ja": "timePolicy",
            "return_type": "returnType",
            "cardinality": "cardinality",
        }.items():
            if definition.get(field):
                lines.append(f"{subject} ont:{predicate} {_literal(definition[field])} .")
        # SQL 条件は宣言として保持する。SHACL で実行可能という主張はしない。
        for evidence in definition.get("evidence", []):
            identity = stable_ontology_id("evidence", item.id, evidence)
            evidence_iri = f"<urn:nl2sql:ontology:evidence:{identity}>"
            lines.extend(
                [
                    f"{subject} prov:wasDerivedFrom {evidence_iri} .",
                    f"{evidence_iri} rdf:type prov:Entity .",
                ]
            )
            for field, predicate in {
                "source_id": "sourceId",
                "locator": "locator",
                "excerpt_ja": "excerpt",
                "source_sha256": "sourceSha256",
                "source_kind": "sourceKind",
                "assertion": "assertion",
                "verified": "verified",
            }.items():
                if field in evidence:
                    lines.append(f"{evidence_iri} ont:{predicate} {_literal(evidence[field])} .")
        for mapping in definition.get("mappings", []):
            identity = stable_ontology_id("physical_mapping", item.id, mapping)
            mapping_iri = f"<urn:nl2sql:ontology:mapping:{identity}>"
            lines.extend(
                [
                    f"{subject} ont:physicalMapping {mapping_iri} .",
                    f"{mapping_iri} rdf:type ont:PhysicalMapping .",
                ]
            )
            for field, predicate in {
                "owner": "owner",
                "object_name": "objectName",
                "column_name": "columnName",
                "expression_sql": "expressionSql",
            }.items():
                if mapping.get(field):
                    lines.append(f"{mapping_iri} ont:{predicate} {_literal(mapping[field])} .")
        # Link Type は辺として描くが、定義への参照は両端ではなく述語 IRI に結ぶ。
        from .ontology_unified_model import definition_references

        parsed = DEFINITION.validate_python(definition)
        for field, ref in definition_references(parsed):
            if ref in link_by_name:
                target_iri = stable_edge_iri(link_by_name[ref].id)
            elif ref in node_by_name:
                target_iri = stable_node_iri(node_by_name[ref].id)
            else:
                continue
            lines.append(f"{subject} {reference_semantics(parsed, field)[1]} {target_iri} .")
    for node in ontology.nodes:
        if node.review_status != "approved" or not node.enum_value_definition:
            continue
        parent = node.metadata.get("derived_from_definition_id")
        if parent:
            subject = stable_node_iri(node.id)
            lines.extend(
                [
                    f"{subject} skos:inScheme {stable_node_iri(str(parent))} .",
                    f"{subject} skos:notation {_literal(node.enum_value_definition.code)} .",
                    f"{subject} skos:prefLabel {_literal(node.business_name_ja)} .",
                ]
            )
    return sorted(set(lines))


def typed_shacl_lines(ontology: SchemaOntology) -> list[str]:
    from .ontology_semantics import stable_edge_iri, stable_node_iri

    by_name = definition_nodes(ontology)
    lines: list[str] = []

    def shape(identity: str, owner: Any, path: str, constraints: list[str]) -> None:
        if not constraints:
            return
        sid = stable_ontology_id("typed_shape", identity)
        lines.append(
            f"ont:{sid} a sh:NodeShape ; sh:targetClass {stable_node_iri(owner.id)} ; "
            f"sh:property [ sh:path {path} ; {' ; '.join(constraints)} ] ."
        )

    for node in by_name.values():
        d = node.metadata["definition"]
        if node.kind == "property":
            owner = by_name.get(d.get("object_type"))
            if owner is None:
                continue
            constraints = []
            dtype = XSD_TYPES.get(d.get("data_type"))
            if dtype:
                constraints.append(f"sh:datatype xsd:{dtype}")
            owner_definition = owner.metadata["definition"]
            if d.get("required") or d["api_name"] in owner_definition.get("primary_key", []):
                constraints.append("sh:minCount 1")
            for source in [
                node,
                by_name.get(d.get("value_type")),
                by_name.get(d.get("shared_property")),
            ]:
                if source is None:
                    continue
                values = source.metadata["definition"].get("allowed_values", [])
                if values:
                    allowed = " ".join(typed_literal(v, d.get("data_type", "")) for v in values)
                    constraints.append(f"sh:in ( {allowed} )")
            # 複数の sh:in は別 property shape にし、各制約をすべて適用する。
            for index, constraint in enumerate(constraints):
                shape(f"{node.id}:{index}", owner, stable_node_iri(node.id), [constraint])
        elif node.kind == "enumeration":
            prop = by_name.get(d.get("property"))
            if prop is None:
                continue
            properties = (
                [prop]
                if prop.kind == "property"
                else [
                    candidate
                    for candidate in by_name.values()
                    if candidate.kind == "property"
                    and candidate.metadata["definition"].get("shared_property") == d["property"]
                ]
            )
            for concrete in properties:
                pd = concrete.metadata["definition"]
                owner = by_name.get(pd.get("object_type"))
                if owner is None or not d.get("values"):
                    continue
                allowed = " ".join(
                    typed_literal(v["code"], pd.get("data_type", "")) for v in d["values"]
                )
                shape(
                    f"{node.id}:{concrete.id}",
                    owner,
                    stable_node_iri(concrete.id),
                    [f"sh:in ( {allowed} )"],
                )
    for edge in ontology.edges:
        if edge.kind != "link_type" or edge.review_status != "approved":
            continue
        d = edge.metadata.get("definition")
        if not isinstance(d, dict):
            continue
        owner, target = by_name.get(d.get("source", "")), by_name.get(d.get("target", ""))
        if owner is None or target is None:
            continue
        constraints = [f"sh:class {stable_node_iri(target.id)}"]
        if not d.get("optional", True):
            constraints.append("sh:minCount 1")
        if d.get("cardinality") in {"one_to_one", "many_to_one"}:
            constraints.append("sh:maxCount 1")
        shape(edge.id, owner, stable_edge_iri(edge.id), constraints)
        if d.get("cardinality") in {"one_to_one", "one_to_many"}:
            shape(
                f"{edge.id}:inverse",
                target,
                f"[ sh:inversePath {stable_edge_iri(edge.id)} ]",
                ["sh:maxCount 1"],
            )
    return lines
