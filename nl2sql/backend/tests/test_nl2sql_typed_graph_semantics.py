"""型付き graph の意味、データ制約、互換変換を実際の RDF で検査する。"""

from typing import Any

import pytest
from pyshacl import validate
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD
from test_nl2sql_markdown_unification import all_concepts
from test_nl2sql_ontology_definitions import runtime

from app.features.nl2sql.ontology_catalog import SchemaOntology
from app.features.nl2sql.ontology_definition_validation import validate_definitions
from app.features.nl2sql.ontology_definitions import BusinessDefinition, ProfileOntologyBundle
from app.features.nl2sql.ontology_semantics import (
    serialize_owl_turtle,
    serialize_shacl_turtle,
    stable_edge_iri,
    stable_node_iri,
)
from app.features.nl2sql.ontology_unified_model import (
    DEFINITIONS,
    legacy_definitions,
    merge_definitions,
    project_graph,
)

ONT = Namespace("urn:nl2sql:ontology:")
PROV = Namespace("http://www.w3.org/ns/prov#")


def fixture_graph(*, shared: bool = False) -> tuple[SchemaOntology, list[BusinessDefinition]]:
    raw = [d.model_dump(mode="json") for d in all_concepts()]
    by = {d["api_name"]: d for d in raw}
    by["Order.id"].update(
        value_type="IdType",
        shared_property="SharedId",
        evidence=[
            {
                "source_id": "schema",
                "locator": "APP.ORDERS.ID",
                "source_kind": "database",
                "verified": True,
            }
        ],
    )
    by["Order"].update(implements=[{"interface": "Identified", "property_mapping": []}])
    by["Identified"].update(required_links=["Related"])
    if shared:
        by["Codes"]["property"] = "SharedId"
    definitions, _ = merge_definitions("sales", DEFINITIONS.validate_python(raw))
    rt, _ = runtime()
    return (
        project_graph(
            rt.profile_view("sales")[1], definitions, "ontology_markdown_snapshot_semantics"
        ),
        definitions,
    )


def test_projection_keeps_enum_identity_and_round_trip_without_extra_definitions() -> None:
    graph, definitions = fixture_graph()
    member = next(n for n in graph.nodes if n.kind == "enum_value")
    assert member.enum_value_definition is not None
    assert member.enum_value_definition.physical_literal == 1
    assert len(legacy_definitions(graph)) == 13
    again = project_graph(graph, legacy_definitions(graph))
    assert {n.id for n in again.nodes} == {n.id for n in graph.nodes}
    assert {e.id for e in again.edges} == {e.id for e in graph.edges}
    by = {d.api_name: d.id for d in definitions}
    assert any(
        e.kind == "domain"
        and e.source_node_id == by["Order.id"]
        and e.target_node_id == by["Order"]
        for e in graph.edges
    )
    assert any(
        e.kind == "range"
        and e.source_node_id == by["Order.id"]
        and e.target_node_id == by["IdType"]
        for e in graph.edges
    )


def test_rdf_uses_specific_relations_and_link_definition_iri() -> None:
    graph, definitions = fixture_graph()
    rdf = Graph().parse(data=serialize_owl_turtle(graph), format="turtle")
    by = {d.api_name: URIRef(stable_node_iri(d.id)[1:-1]) for d in definitions}
    link = URIRef(stable_edge_iri(next(d.id for d in definitions if d.kind == "link_type"))[1:-1])
    assert (by["Order.id"], RDFS.domain, by["Order"]) in rdf
    assert (by["Order.id"], RDFS.range, by["IdType"]) in rdf
    assert (by["IdType"], OWL.equivalentClass, XSD.integer) in rdf
    assert (by["Order"], ONT.implementsInterface, by["Identified"]) in rdf
    assert (by["Order"], RDFS.subClassOf, by["Identified"]) not in rdf
    assert (by["Identified"], ONT.requiresLink, link) in rdf
    assert (by["Identified"], ONT.requiresLink, by["Order"]) not in rdf
    assert (by["Calculate"], ONT.dependsOn, by["Order.id"]) in rdf
    assert (by["Valid"], ONT.appliesTo, by["Order"]) in rdf
    assert (by["Approve"], ONT.affectsProperty, by["Order.id"]) in rdf
    member = next(rdf.objects(by["Codes"], SKOS.hasTopConcept))
    assert (member, SKOS.inScheme, by["Codes"]) in rdf
    assert (member, SKOS.notation, Literal("1")) in rdf
    mapping = next(rdf.objects(by["Order.id"], ONT.physicalMapping))
    assert (mapping, ONT.columnName, Literal("ID")) in rdf
    evidence = next(rdf.objects(by["Order.id"], PROV.wasDerivedFrom))
    assert (evidence, RDF.type, PROV.Entity) in rdf
    assert (evidence, ONT.locator, Literal("APP.ORDERS.ID")) in rdf
    assert (evidence, ONT.verified, Literal(True)) in rdf


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize(
    "value,conforms",
    [(Literal(1), True), (Literal(2), False), (Literal("1"), False), (None, False)],
)
def test_shacl_checks_real_instances_including_shared_enums(
    shared: bool, value: Any, conforms: bool
) -> None:
    graph, definitions = fixture_graph(shared=shared)
    shapes = Graph().parse(data=serialize_shacl_turtle(graph), format="turtle")
    by = {d.api_name: URIRef(stable_node_iri(d.id)[1:-1]) for d in definitions}
    data = Graph()
    data.add((ONT.example, RDF.type, by["Order"]))
    if value is not None:
        data.add((ONT.example, by["Order.id"], value))
    actual, _, report = validate(data, shacl_graph=shapes)
    assert bool(actual) == conforms, report


@pytest.mark.parametrize(
    "codes,expected",
    [
        (["oops"], "ALLOWED_VALUE_TYPE_INVALID"),
        (["1", "1"], "ENUM_CODE_DUPLICATE"),
        ([""], "ENUM_MEMBER_INCOMPLETE"),
    ],
)
def test_invalid_enum_values_are_reported_for_the_actual_definition(
    codes: list[str], expected: str
) -> None:
    _, definitions = fixture_graph()
    raw = [d.model_dump(mode="json") for d in definitions]
    enum = next(d for d in raw if d["kind"] == "enumeration")
    enum["values"] = [{"code": code, "label_ja": "値"} for code in codes]
    bundle = ProfileOntologyBundle(
        id="bundle",
        profile_id="sales",
        job_id="job",
        schema_fingerprint="schema",
        profile_fingerprint="profile",
        definitions=DEFINITIONS.validate_python(raw),
    )
    findings = validate_definitions(
        bundle, {"objects": [{"owner": "APP", "object_name": "ORDERS", "columns": ["ID"]}]}
    )
    assert any(
        f.code == expected and f.definition_id == enum["id"] and f.severity == "error"
        for f in findings
    )


def test_narrow_scope_prunes_domain_governance_and_derived_enum_members() -> None:
    from app.features.nl2sql.models import AllowedObjects
    from app.features.nl2sql.ontology_catalog import migrate_profile_ontology_view

    graph, definitions = fixture_graph()
    raw = [d.model_dump(mode="json") for d in definitions] + [
        dict(
            kind="object_type",
            api_name="Customer",
            name_ja="顧客",
            mappings=[dict(owner="APP", object_name="CUSTOMERS")],
        ),
        dict(
            kind="property",
            api_name="Customer.code",
            name_ja="顧客コード",
            data_type="integer",
            object_type="Customer",
        ),
        dict(
            kind="business_rule",
            api_name="CustomerRule",
            name_ja="顧客条件",
            applies_to=["Customer.code"],
        ),
        dict(
            kind="enumeration",
            api_name="CustomerCodes",
            name_ja="顧客分類",
            property="Customer.code",
            values=[dict(code="1", label_ja="通常")],
        ),
    ]
    definitions, _ = merge_definitions("sales", DEFINITIONS.validate_python(raw))
    graph = project_graph(graph, definitions)
    rt, legacy = runtime()
    view = migrate_profile_ontology_view(legacy.profile, graph, strict=False)
    narrow = rt._narrow_profile_view(view, graph, AllowedObjects(table_names=["APP.ORDERS"]))
    kept = {n.technical_name for n in graph.nodes if n.id in narrow.node_ids}
    assert {"Order", "Order.id", "Codes", "Codes:1"} <= kept
    assert not any(name.startswith("Customer") for name in kept)


@pytest.mark.parametrize(
    "cardinality", ["one_to_one", "many_to_one", "one_to_many", "many_to_many"]
)
def test_link_cardinality_checks_both_directions(cardinality: str) -> None:
    graph, definitions = fixture_graph()
    raw = [d.model_dump(mode="json") for d in definitions]
    next(d for d in raw if d["kind"] == "link_type").update(cardinality=cardinality, optional=False)
    graph = project_graph(graph, DEFINITIONS.validate_python(raw))
    shapes = Graph().parse(data=serialize_shacl_turtle(graph), format="turtle")
    obj = URIRef(stable_node_iri(next(d.id for d in definitions if d.kind == "object_type"))[1:-1])
    prop = URIRef(stable_node_iri(next(d.id for d in definitions if d.kind == "property"))[1:-1])
    link = URIRef(stable_edge_iri(next(d.id for d in definitions if d.kind == "link_type"))[1:-1])
    data = Graph()
    for name in ["a", "b", "c"]:
        data.add((ONT[name], RDF.type, obj))
        data.add((ONT[name], prop, Literal(1)))
        data.add((ONT[name], link, ONT[name]))
    assert validate(data, shacl_graph=shapes)[0]
    data.add((ONT.a, link, ONT.b))
    assert bool(validate(data, shacl_graph=shapes)[0]) == (cardinality == "many_to_many")
    data.remove((ONT.a, link, None))
    assert not validate(data, shacl_graph=shapes)[0]  # 非 optional の欠落は常に不適合。


def test_member_search_grounds_its_enumeration_property_and_object() -> None:
    from app.features.nl2sql.ontology_catalog import (
        migrate_profile_ontology_view,
        retrieve_ontology_nodes,
    )

    graph, definitions = fixture_graph()
    _, legacy = runtime()
    view = migrate_profile_ontology_view(legacy.profile, graph, strict=False)
    hits = {hit.node_id for hit in retrieve_ontology_nodes("有効", graph, view)}
    assert next(d.id for d in definitions if d.api_name == "Order") in hits


def test_shared_allowed_values_intersect_enum_values() -> None:
    graph, definitions = fixture_graph()
    raw = [d.model_dump(mode="json") for d in definitions]
    next(d for d in raw if d["kind"] == "shared_property")["allowed_values"] = ["1"]
    next(d for d in raw if d["kind"] == "enumeration")["values"].append(
        dict(code="2", label_ja="別値")
    )
    graph = project_graph(graph, DEFINITIONS.validate_python(raw))
    shapes = Graph().parse(data=serialize_shacl_turtle(graph), format="turtle")
    obj = URIRef(stable_node_iri(next(d.id for d in definitions if d.kind == "object_type"))[1:-1])
    prop = URIRef(stable_node_iri(next(d.id for d in definitions if d.kind == "property"))[1:-1])
    data = Graph()
    data.add((ONT.example, RDF.type, obj))
    data.add((ONT.example, prop, Literal(2)))
    assert not validate(data, shacl_graph=shapes)[0]
    data.set((ONT.example, prop, Literal(1)))
    assert validate(data, shacl_graph=shapes)[0]
