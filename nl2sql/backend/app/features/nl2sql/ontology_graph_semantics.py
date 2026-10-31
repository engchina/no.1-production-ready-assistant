"""型付き概念の graph 投影契約。表示用の辺種別と RDF の述語を分離する。"""

from __future__ import annotations

from typing import Any

from .ontology_definitions import BusinessDefinition
from .ontology_models import EnumValueDefinition, OntologyEdge, OntologyNode, OntologyProvenance
from .ontology_store import stable_ontology_id

XSD_TYPES = {
    "string": "string",
    "integer": "integer",
    "number": "decimal",
    "boolean": "boolean",
    "date": "date",
    "datetime": "dateTime",
}
REFERENCE_PREDICATES = {
    "object_type": "ont:objectType",
    "property": "ont:constrainsProperty",
    "shared_property": "ont:usesSharedProperty",
    "value_type": "rdfs:range",
    "object_value_type": "ont:valueType",
    "time_property": "ont:timeProperty",
    "timestamp_property": "ont:timestampProperty",
    "title_property": "ont:titleProperty",
    "intermediary_object": "ont:intermediaryObject",
    "dependencies": "ont:dependsOn",
    "applies_to": "ont:appliesTo",
    "extends": "rdfs:subClassOf",
    "implements": "ont:implementsInterface",
    "required_links": "ont:requiresLink",
    "required_actions": "ont:requiresAction",
    "grain": "ont:hasGrain",
    "distinct_keys": "ont:distinctKey",
    "affected_properties": "ont:affectsProperty",
    "primary_key": "ont:primaryKey",
    "properties": "ont:hasProperty",
}


def reference_semantics(definition: BusinessDefinition, field: str) -> tuple[str, str]:
    if definition.kind == "property" and field == "object_type":
        return "domain", "rdfs:domain"
    if field == "value_type":
        if getattr(definition, "data_type", "") == "object":
            return "uses", "ont:valueType"
        return "range", "rdfs:range"
    if field == "extends":
        return "is_a", "rdfs:subClassOf"
    if field == "applies_to":
        return "governs", "ont:appliesTo"
    # 実装契約は、クラスの包含を保証する OWL 継承とは区別する。
    return "uses", REFERENCE_PREDICATES.get(field, "ont:uses")


def enum_graph_members(
    definitions: list[BusinessDefinition], revision_id: str, provenance: OntologyProvenance
) -> tuple[list[OntologyNode], list[OntologyEdge]]:
    by_name = {d.api_name: d for d in definitions}
    nodes: dict[str, OntologyNode] = {}
    edges: list[OntologyEdge] = []
    for definition in definitions:
        if definition.kind != "enumeration":
            continue
        prop = by_name.get(definition.property)
        if prop is None or prop.kind not in {"property", "shared_property"}:
            continue  # 未解決参照は定義検証で報告し、dangling edge を作らない。
        for member in definition.values:
            if not member.code or not member.label_ja:
                continue  # 不完全なメンバーは公開前の定義検証で報告する。
            identity = stable_ontology_id("enum_member", definition.id, member.code)
            # コードの字面は SKOS notation に残し、物理値は属性の型へ変換する。
            data_type = getattr(prop, "data_type", "string")
            literal: Any = member.code
            try:
                if data_type == "integer":
                    literal = int(member.code)
                elif data_type == "boolean":
                    if member.code.lower() not in {"true", "false", "1", "0"}:
                        raise ValueError("invalid boolean")
                    literal = member.code.lower() in {"true", "1"}
                elif data_type not in XSD_TYPES:
                    data_type = "string"
            except ValueError:
                data_type = "string"  # 不完全な下書きを保存可能にする。公開前に別途検査。
            nodes[identity] = OntologyNode(
                id=identity,
                revision_id=revision_id,
                kind="enum_value",
                technical_name=f"{definition.api_name}:{member.code}",
                business_name_ja=member.label_ja,
                provenance=provenance,
                review_status="approved",
                enum_value_definition=EnumValueDefinition.model_validate(
                    dict(
                        code=member.code,
                        label_ja=member.label_ja,
                        physical_literal=literal,
                        data_type=data_type,
                        property_node_id=prop.id,
                    )
                ),
                metadata={
                    "derived_from_definition_id": definition.id,
                    "enumeration_api_name": definition.api_name,
                },
            )
            edges.append(
                OntologyEdge(
                    id=stable_ontology_id("enum_member_edge", definition.id, identity),
                    revision_id=revision_id,
                    kind="has_value",
                    source_node_id=definition.id,
                    target_node_id=identity,
                    relationship_name_ja="列挙メンバー",
                    provenance=provenance,
                    review_status="approved",
                    metadata={"semantic_predicate": "skos:hasTopConcept"},
                )
            )
    return list(nodes.values()), list({edge.id: edge for edge in edges}.values())
