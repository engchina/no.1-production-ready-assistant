"""同一の型付き版から文書・graph・OWL/SHACL・query context を生成する。"""

from __future__ import annotations

import json

from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, Namespace, URIRef

from .ontology_definition_service import definition_fingerprint
from .ontology_definitions import ProfileOntologyBundle


def render_definition_artifacts(bundle: ProfileOntologyBundle) -> dict[str, str]:
    ns = Namespace(f"urn:nl2sql:profile:{bundle.profile_id}:")
    sh = Namespace("http://www.w3.org/ns/shacl#")
    graph, shapes = Graph(), Graph()
    graph.bind("owl", OWL)
    shapes.bind("sh", sh)
    markdown = [f"# {bundle.profile_id} — Ontology", f"Version: {bundle.id}"]
    mermaid = ["graph LR"]
    by_name = {item.api_name: item for item in bundle.definitions}
    for item in sorted(bundle.definitions, key=lambda value: value.id):
        uri = URIRef(ns[item.id])
        graph.add((uri, RDFS.label, Literal(item.name_ja, lang="ja")))
        graph.add(
            (
                uri,
                ns.definitionJson,
                Literal(
                    json.dumps(item.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
                ),
            )
        )
        if item.kind in {"object_type", "interface"}:
            graph.add((uri, RDF.type, OWL.Class))
            shape = URIRef(ns[f"shape:{item.id}"])
            shapes.add((shape, RDF.type, sh.NodeShape))
            shapes.add((shape, sh.targetClass, uri))
        elif item.kind in {"property", "shared_property", "value_type"}:
            graph.add((uri, RDF.type, OWL.DatatypeProperty))
        else:
            graph.add((uri, RDF.type, ns[item.kind]))
        label = (
            item.name_ja.replace('"', "＂").replace("\n", " ").replace("<", "〈").replace(">", "〉")
        )
        mermaid.append(f'  {item.id}["{label}"]')
        if item.kind == "link_type" and item.source in by_name and item.target in by_name:
            mermaid.append(f"  {by_name[item.source].id} -->|{item.id}| {by_name[item.target].id}")
        if item.kind == "object_type":
            for prop_name in item.properties:
                prop = by_name.get(prop_name)
                if prop is not None and prop.kind == "property":
                    prop_shape = URIRef(ns[f"shape:{item.id}:{prop.id}"])
                    shapes.add((URIRef(ns[f"shape:{item.id}"]), sh.property, prop_shape))
                    shapes.add((prop_shape, sh.path, URIRef(ns[prop.id])))
                    if prop.required:
                        shapes.add((prop_shape, sh.minCount, Literal(1)))
                    datatype = {
                        "string": XSD.string,
                        "integer": XSD.integer,
                        "number": XSD.decimal,
                        "date": XSD.date,
                        "datetime": XSD.dateTime,
                        "boolean": XSD.boolean,
                    }.get(prop.data_type)
                    if datatype:
                        shapes.add((prop_shape, sh.datatype, datatype))
            for implementation in item.implements:
                if implementation.interface in by_name:
                    graph.add(
                        (uri, RDFS.subClassOf, URIRef(ns[by_name[implementation.interface].id]))
                    )
        markdown.extend(
            [
                f"## {item.name_ja} ({item.kind})",
                "```json",
                json.dumps(
                    item.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2
                ),
                "```",
            ]
        )
    # N-Triples are deterministic valid Turtle and avoid randomized blank-node serialization.
    owl = "\n".join(sorted(str(graph.serialize(format="nt")).splitlines()))
    shacl = "\n".join(sorted(str(shapes.serialize(format="nt")).splitlines()))
    context = json.dumps(
        {
            "profile_id": bundle.profile_id,
            "version_id": bundle.id,
            "definitions": [item.model_dump(mode="json") for item in bundle.definitions],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    artifacts = {
        "markdown": "\n\n".join(markdown),
        "mermaid": "\n".join(mermaid),
        "owl_turtle": owl,
        "shacl_turtle": shacl,
        "sql_context": context,
    }
    graph_nodes = [
        {"id": item.id, "name_ja": item.name_ja, "kind": item.kind} for item in bundle.definitions
    ]
    graph_edges = []
    for item in bundle.definitions:
        references = []
        if item.kind == "link_type":
            references.extend([item.source, item.target])
        for field in ("object_type", "shared_property", "value_type"):
            if getattr(item, field, ""):
                references.append(getattr(item, field))
        for field in ("dependencies", "applies_to", "extends"):
            references.extend(getattr(item, field, []))
        if item.kind == "object_type":
            references.extend(item.properties)
            references.extend(implementation.interface for implementation in item.implements)
        for reference in sorted(set(references)):
            if reference in by_name:
                graph_edges.append(
                    {
                        "id": f"{item.id}:{by_name[reference].id}",
                        "source": item.id,
                        "target": by_name[reference].id,
                    }
                )
    artifacts["graph_json"] = json.dumps(
        {"version_id": bundle.id, "nodes": graph_nodes, "edges": graph_edges},
        ensure_ascii=False,
        sort_keys=True,
    )
    artifacts["manifest"] = json.dumps(
        {
            "version_id": bundle.id,
            "profile_id": bundle.profile_id,
            "hashes": {key: definition_fingerprint(value) for key, value in artifacts.items()},
        },
        sort_keys=True,
    )
    return artifacts
