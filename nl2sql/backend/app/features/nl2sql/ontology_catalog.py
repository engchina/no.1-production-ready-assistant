"""Oracle schema catalog と NL2SQL Ontology を接続する決定論的な変換層。

このモジュールは network や LLM に依存しない。OCI Generative AI の embedding を
利用する場合だけ、呼び出し元が明示的に callback を渡す。callback が無い、または
利用できない場合は、profile view 内の業務名・別名・comment・glossary の一致だけで
候補を絞り、確定できない意味は blocking ambiguity として返す。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import defaultdict, deque
from collections.abc import Sequence
from itertools import combinations
from typing import Literal, Protocol

from pydantic import Field

from app.features.nl2sql.models import Nl2SqlProfile, SchemaCatalog, SchemaTable
from app.features.nl2sql.object_identity import format_object_part, object_name_tokens
from app.features.nl2sql.ontology_models import (
    IntentAmbiguity,
    IntentDimension,
    IntentEntity,
    IntentMetric,
    IntentRelationshipPath,
    IntentTimeRange,
    JoinCondition,
    JoinType,
    OntologyContract,
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
    QuestionIntentGraph,
    RelationshipCardinality,
    RelationshipDirection,
)
from app.features.nl2sql.ontology_store import (
    compute_etag,
    physical_identity_part,
    schema_fingerprint,
    stable_ontology_id,
    stable_physical_id,
)

logger = logging.getLogger(__name__)

_IDENTIFIER_SEPARATOR = re.compile(r"[\s\W_]+", re.UNICODE)
_LIMIT_PATTERN = re.compile(r"(?:上位|最大|先頭)?\s*(\d{1,5})\s*(?:件|行)")
_PHYSICAL_NODE_KINDS = frozenset(
    {
        OntologyNodeKind.SCHEMA,
        OntologyNodeKind.TABLE,
        OntologyNodeKind.VIEW,
        OntologyNodeKind.COLUMN,
    }
)
_BUSINESS_NODE_KINDS = frozenset(
    {
        OntologyNodeKind.OBJECT_TYPE,
        OntologyNodeKind.INTERFACE,
        OntologyNodeKind.FUNCTION,
        OntologyNodeKind.ACTION_TYPE,
        OntologyNodeKind.SHARED_PROPERTY,
        OntologyNodeKind.VALUE_TYPE,
        OntologyNodeKind.ENUMERATION,
        OntologyNodeKind.OBJECT_SET,
        OntologyNodeKind.BUSINESS_ENTITY,
        OntologyNodeKind.BUSINESS_EVENT,
        OntologyNodeKind.PROPERTY,
        OntologyNodeKind.METRIC,
        OntologyNodeKind.BUSINESS_TERM,
        OntologyNodeKind.BUSINESS_RULE,
        OntologyNodeKind.ENUM_VALUE,
    }
)
_PATH_EDGE_KINDS = frozenset(
    {
        OntologyEdgeKind.FOREIGN_KEY,
        OntologyEdgeKind.BUSINESS_RELATIONSHIP,
        OntologyEdgeKind.LINK_TYPE,
    }
)


class SchemaOntology(OntologyContract):
    """一つの schema fingerprint に対応する versioned physical graph。"""

    revision: OntologyRevision
    nodes: list[OntologyNode] = Field(default_factory=list)
    edges: list[OntologyEdge] = Field(default_factory=list)


class OntologyRetrievalHit(OntologyContract):
    """語彙/embedding の根拠を失わない profile-scoped retrieval hit。"""

    node_id: str
    score: float = Field(ge=0.0)
    matched_terms: list[str] = Field(default_factory=list)
    sources: list[
        Literal["business_name", "alias", "technical_name", "comment", "glossary", "embedding"]
    ] = Field(  # noqa: E501
        default_factory=list
    )


class EmbeddingRetrievalCallback(Protocol):
    """OCI embedding/Oracle Vector Search adapter が実装する任意 callback。"""

    def __call__(
        self,
        question: str,
        candidates: Sequence[OntologyNode],
        limit: int,
    ) -> Sequence[tuple[str, float]]: ...


class AmbiguousPhysicalObjectError(ValueError):
    """owner 無しの旧 object 名が複数 schema に一致した場合に送出する。"""


def _oracle_name(value: str) -> str:
    """カタログ上の owner / object / column 名（大文字小文字を保持）。

    #563 より前は大文字化していたため、`MIXED_CASE` と `"Mixed_Case"` が 1 node に潰れていた。
    """

    return unicodedata.normalize("NFC", value.strip())


def _identity_name(value: str) -> str:
    """ID・technical_name・fingerprint に使う名前（`physical_identity_part`）。

    大文字化しても変わらない名前は従来と同じ値、小文字を含む名前だけ `"Mixed_Case"` になる。
    """

    return physical_identity_part(_oracle_name(value))


def _catalog_name_token(value: str) -> str:
    """カタログ上の owner / object 名を、引用規則どおりの照合 token にする。"""

    normalized = unicodedata.normalize("NFC", value.strip())
    try:
        return format_object_part(normalized)
    except ValueError:
        return normalized


def _normalized_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return _IDENTIFIER_SEPARATOR.sub("", normalized)


def _unique(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        stripped = value.strip()
        key = _normalized_text(stripped)
        if stripped and key and key not in seen:
            seen.add(key)
            result.append(stripped)
    return result


def _object_kind(table_type: str) -> OntologyNodeKind:
    return (
        OntologyNodeKind.VIEW if "view" in table_type.strip().casefold() else OntologyNodeKind.TABLE
    )


def _object_type(kind: OntologyNodeKind) -> Literal["table", "view"]:
    return "view" if kind == OntologyNodeKind.VIEW else "table"


def _catalog_semantic_payload(catalog: SchemaCatalog) -> dict[str, object]:
    """refresh timestamp を除き、relation の列順を保持した fingerprint payload。"""

    return {
        "tables": [
            {
                "owner": _identity_name(table.owner),
                "table_name": _identity_name(table.table_name),
                "table_type": table.table_type.casefold(),
                "logical_name": table.logical_name,
                "comment": table.comment,
                "columns": [
                    {
                        "column_name": _identity_name(column.column_name),
                        "logical_name": column.logical_name,
                        "data_type": column.data_type,
                        "nullable": column.nullable,
                        "comment": column.comment,
                    }
                    for column in table.columns
                ],
                "constraints": list(table.constraints),
                "constraint_details": [
                    {
                        "constraint_name": detail.constraint_name,
                        "constraint_type": detail.constraint_type,
                        "owner": _identity_name(detail.owner),
                        "table_name": _identity_name(detail.table_name),
                        # Composite key order is semantically significant.
                        "source_columns": [_identity_name(value) for value in detail.columns],
                        "referenced_owner": _identity_name(detail.referenced_owner or ""),
                        "referenced_table": _identity_name(detail.referenced_table or ""),
                        "target_columns": [
                            _identity_name(value) for value in detail.referenced_columns
                        ],
                        "delete_rule": detail.delete_rule,
                        "status": detail.status,
                        "deferrable": detail.deferrable,
                    }
                    for detail in table.constraint_details
                ],
            }
            for table in catalog.tables
        ],
        "view_dependencies": [
            {
                "owner": _identity_name(dependency.owner),
                "view_name": _identity_name(dependency.view_name),
                "referenced_owner": _identity_name(dependency.referenced_owner),
                "referenced_name": _identity_name(dependency.referenced_name),
                "referenced_type": dependency.referenced_type.casefold(),
            }
            for dependency in catalog.view_dependencies
        ],
    }


def catalog_schema_fingerprint(catalog: SchemaCatalog) -> str:
    """Catalog row order に依存しない Ontology 用 schema fingerprint。"""

    return schema_fingerprint(_catalog_semantic_payload(catalog))


def _revision_etag(
    revision: OntologyRevision,
    nodes: Sequence[OntologyNode],
    edges: Sequence[OntologyEdge],
) -> str:
    return compute_etag(
        {
            "id": revision.id,
            "schema_fingerprint": revision.schema_fingerprint,
            "parent_revision_id": revision.parent_revision_id,
            "node_states": sorted((node.id, node.review_status.value) for node in nodes),
            "edge_states": sorted((edge.id, edge.review_status.value) for edge in edges),
        },
        revision.version,
    )


def build_schema_ontology(
    catalog: SchemaCatalog,
    *,
    version: int = 1,
    parent_revision_id: str | None = None,
    revision_id: str | None = None,
) -> SchemaOntology:
    """SchemaCatalog を stable-ID の SchemaOntology draft に変換する。"""

    fingerprint = catalog_schema_fingerprint(catalog)
    resolved_revision_id = revision_id or stable_ontology_id(
        "ontology_revision", fingerprint, version
    )
    provenance = OntologyProvenance(
        source_kind=OntologySourceKind.INTROSPECTED,
        source_id=fingerprint,
        source_detail="Oracle schema catalog refresh",
    )
    nodes: dict[str, OntologyNode] = {}
    edges: dict[str, OntologyEdge] = {}

    def add_schema(owner: str) -> OntologyNode:
        normalized_owner = _oracle_name(owner or "APP")
        identity_owner = physical_identity_part(normalized_owner)
        node_id = stable_physical_id("schema", identity_owner, identity_owner)
        if node_id not in nodes:
            nodes[node_id] = OntologyNode(
                id=node_id,
                revision_id=resolved_revision_id,
                kind=OntologyNodeKind.SCHEMA,
                technical_name=identity_owner,
                business_name_ja=normalized_owner,
                description_ja="Oracle schema",
                aliases=_unique([normalized_owner, identity_owner]),
                provenance=provenance,
                review_status=OntologyReviewStatus.APPROVED,
                metadata={"owner": normalized_owner},
            )
        return nodes[node_id]

    def add_contains(source: OntologyNode, target: OntologyNode) -> None:
        edge_id = stable_ontology_id("contains", source.id, target.id)
        edges.setdefault(
            edge_id,
            OntologyEdge(
                id=edge_id,
                revision_id=resolved_revision_id,
                kind=OntologyEdgeKind.CONTAINS,
                source_node_id=source.id,
                target_node_id=target.id,
                relationship_name_ja="含む",
                direction=RelationshipDirection.DIRECTED,
                cardinality=RelationshipCardinality.ONE_TO_MANY,
                provenance=provenance,
                review_status=OntologyReviewStatus.APPROVED,
            ),
        )

    def add_object(
        owner: str,
        name: str,
        kind: OntologyNodeKind,
        *,
        logical_name: str = "",
        comment: str = "",
        external: bool = False,
    ) -> OntologyNode:
        normalized_owner = _oracle_name(owner or "APP")
        normalized_name = _oracle_name(name)
        physical_type = _object_type(kind)
        # ID と technical_name は physical_identity_part で作る。小文字を含む引用名
        # （`SALES."Mixed_Case"`）を大文字の同名表と別 node にし、既存名の ID は変えない（#563）。
        identity_owner = physical_identity_part(normalized_owner)
        identity_name = physical_identity_part(normalized_name)
        technical_name = f"{identity_owner}.{identity_name}"
        node_id = stable_physical_id(physical_type, identity_owner, identity_name)
        if node_id not in nodes:
            object_ref = PhysicalObjectRef(
                node_id=node_id,
                owner=normalized_owner,
                object_name=normalized_name,
                object_type=physical_type,
            )
            nodes[node_id] = OntologyNode(
                id=node_id,
                revision_id=resolved_revision_id,
                kind=kind,
                technical_name=technical_name,
                business_name_ja=logical_name.strip() or normalized_name,
                description_ja=comment.strip()
                or ("外部参照として検出された object" if external else ""),
                aliases=_unique([normalized_name, technical_name]),
                physical_mappings=[PhysicalMapping(object_ref=object_ref)],
                provenance=provenance,
                review_status=OntologyReviewStatus.APPROVED,
                metadata={
                    "owner": normalized_owner,
                    "object_name": normalized_name,
                    "object_type": physical_type,
                    "comment": comment,
                    "external": external,
                },
            )
            add_contains(add_schema(normalized_owner), nodes[node_id])
        return nodes[node_id]

    def add_column(
        object_node: OntologyNode,
        column_name: str,
        *,
        ordinal: int | None = None,
        logical_name: str = "",
        data_type: str = "",
        nullable: bool = True,
        comment: str = "",
        external: bool = False,
    ) -> OntologyNode:
        owner = str(object_node.metadata["owner"])
        object_name = str(object_node.metadata["object_name"])
        normalized_column = _oracle_name(column_name)
        identity_prefix = f"{physical_identity_part(owner)}.{physical_identity_part(object_name)}"
        identity_column = physical_identity_part(normalized_column)
        node_id = stable_physical_id(
            "column",
            physical_identity_part(owner),
            physical_identity_part(object_name),
            identity_column,
        )
        if node_id not in nodes:
            object_ref = PhysicalObjectRef(
                node_id=object_node.id,
                owner=owner,
                object_name=object_name,
                object_type=_object_type(object_node.kind),
            )
            column_ref = PhysicalColumnRef(
                node_id=node_id,
                owner=owner,
                object_name=object_name,
                column_name=normalized_column,
                ordinal=ordinal,
            )
            nodes[node_id] = OntologyNode(
                id=node_id,
                revision_id=resolved_revision_id,
                kind=OntologyNodeKind.COLUMN,
                technical_name=f"{identity_prefix}.{identity_column}",
                business_name_ja=logical_name.strip() or normalized_column,
                description_ja=comment.strip(),
                aliases=_unique(
                    [
                        normalized_column,
                        f"{physical_identity_part(object_name)}.{identity_column}",
                        f"{identity_prefix}.{identity_column}",
                    ]
                ),
                physical_mappings=[
                    PhysicalMapping(object_ref=object_ref, column_refs=[column_ref])
                ],
                provenance=provenance,
                review_status=OntologyReviewStatus.APPROVED,
                metadata={
                    "owner": owner,
                    "object_name": object_name,
                    "column_name": normalized_column,
                    "ordinal": ordinal,
                    "data_type": data_type,
                    "nullable": nullable,
                    "comment": comment,
                    "external": external,
                },
            )
            add_contains(object_node, nodes[node_id])
        return nodes[node_id]

    table_by_key: dict[tuple[str, str], SchemaTable] = {}
    for table in catalog.tables:
        owner = _oracle_name(table.owner or "APP")
        table_name = _oracle_name(table.table_name)
        table_by_key[(owner, table_name)] = table
        object_node = add_object(
            owner,
            table_name,
            _object_kind(table.table_type),
            logical_name=table.logical_name,
            comment=table.comment,
        )
        for ordinal, column in enumerate(table.columns, start=1):
            add_column(
                object_node,
                column.column_name,
                ordinal=ordinal,
                logical_name=column.logical_name,
                data_type=column.data_type,
                nullable=column.nullable,
                comment=column.comment,
            )

    # Referenced object/column が catalog 外でも lineage/join mapping は失わない。
    for table in catalog.tables:
        owner = _oracle_name(table.owner or "APP")
        source_name = _oracle_name(table.table_name)
        source_id = stable_physical_id(
            _object_type(_object_kind(table.table_type)),
            physical_identity_part(owner),
            physical_identity_part(source_name),
        )
        source = nodes[source_id]
        unique_column_sets = {
            tuple(_oracle_name(value) for value in detail.columns)
            for detail in table.constraint_details
            if detail.constraint_type in {"P", "U"}
        }
        for detail in table.constraint_details:
            if detail.constraint_type != "R" or not detail.referenced_table:
                continue
            target_owner = _oracle_name(detail.referenced_owner or owner)
            target_name = _oracle_name(detail.referenced_table)
            target_table = table_by_key.get((target_owner, target_name))
            target = add_object(
                target_owner,
                target_name,
                _object_kind(target_table.table_type) if target_table else OntologyNodeKind.TABLE,
                logical_name=target_table.logical_name if target_table else target_name,
                comment=target_table.comment if target_table else "",
                external=target_table is None,
            )
            join_conditions: list[JoinCondition] = []
            for ordinal, (left_name, right_name) in enumerate(
                zip(detail.columns, detail.referenced_columns, strict=False),
                start=1,
            ):
                left = add_column(source, left_name, external=True)
                right = add_column(target, right_name, external=True)
                left_ref = left.physical_mappings[0].column_refs[0]
                right_ref = right.physical_mappings[0].column_refs[0]
                join_conditions.append(
                    JoinCondition(
                        left=left_ref,
                        right=right_ref,
                        ordinal=ordinal,
                    )
                )
            edge_id = stable_physical_id(
                "foreign_key",
                physical_identity_part(owner),
                physical_identity_part(source_name),
                physical_identity_part(detail.constraint_name),
                physical_identity_part(target_owner),
                physical_identity_part(target_name),
            )
            source_columns = tuple(_oracle_name(value) for value in detail.columns)
            mapping_complete = (
                bool(detail.columns)
                and len(detail.columns) == len(detail.referenced_columns)
                and len(join_conditions) == len(detail.columns)
            )
            edge_status = (
                OntologyReviewStatus.APPROVED
                if detail.status.strip().upper() == "ENABLED" and mapping_complete
                else OntologyReviewStatus.PROPOSED
            )
            edges[edge_id] = OntologyEdge(
                id=edge_id,
                revision_id=resolved_revision_id,
                kind=OntologyEdgeKind.FOREIGN_KEY,
                source_node_id=source.id,
                target_node_id=target.id,
                relationship_name_ja=f"{source.business_name_ja} → {target.business_name_ja}",
                description_ja=f"Oracle 外部キー {detail.constraint_name}",
                direction=RelationshipDirection.DIRECTED,
                cardinality=(
                    RelationshipCardinality.ONE_TO_ONE
                    if source_columns in unique_column_sets
                    else RelationshipCardinality.MANY_TO_ONE
                ),
                join_conditions=join_conditions,
                allowed_join_types=[JoinType.INNER, JoinType.LEFT],
                provenance=provenance,
                review_status=edge_status,
                metadata={
                    "constraint_name": detail.constraint_name,
                    "owner": owner,
                    "referenced_owner": target_owner,
                    "delete_rule": detail.delete_rule,
                    "status": detail.status,
                    "deferrable": detail.deferrable,
                },
            )

    for dependency in catalog.view_dependencies:
        owner = _oracle_name(dependency.owner or "APP")
        view_name = _oracle_name(dependency.view_name)
        source_table = table_by_key.get((owner, view_name))
        source = add_object(
            owner,
            view_name,
            OntologyNodeKind.VIEW,
            logical_name=source_table.logical_name if source_table else view_name,
            comment=source_table.comment if source_table else "",
            external=source_table is None,
        )
        target_owner = _oracle_name(dependency.referenced_owner or owner)
        target_name = _oracle_name(dependency.referenced_name)
        target_table = table_by_key.get((target_owner, target_name))
        target_kind = (
            _object_kind(target_table.table_type)
            if target_table
            else (
                OntologyNodeKind.VIEW
                if "view" in dependency.referenced_type.casefold()
                else OntologyNodeKind.TABLE
            )
        )
        target = add_object(
            target_owner,
            target_name,
            target_kind,
            logical_name=target_table.logical_name if target_table else target_name,
            comment=target_table.comment if target_table else "",
            external=target_table is None,
        )
        edge_id = stable_physical_id(
            "lineage",
            physical_identity_part(owner),
            physical_identity_part(view_name),
            physical_identity_part(target_owner),
            physical_identity_part(target_name),
        )
        edges[edge_id] = OntologyEdge(
            id=edge_id,
            revision_id=resolved_revision_id,
            kind=OntologyEdgeKind.LINEAGE,
            source_node_id=source.id,
            target_node_id=target.id,
            relationship_name_ja="参照元",
            description_ja=f"{source.technical_name} は {target.technical_name} を参照",
            direction=RelationshipDirection.DIRECTED,
            cardinality=RelationshipCardinality.UNKNOWN,
            provenance=provenance,
            review_status=OntologyReviewStatus.APPROVED,
            metadata={"referenced_type": dependency.referenced_type},
        )

    sorted_nodes = sorted(nodes.values(), key=lambda node: node.id)
    sorted_edges = sorted(edges.values(), key=lambda edge: edge.id)
    revision = OntologyRevision(
        id=resolved_revision_id,
        version=version,
        status=OntologyRevisionStatus.DRAFT,
        schema_fingerprint=fingerprint,
        parent_revision_id=parent_revision_id,
        note="Oracle schema catalog から生成",
    )
    revision = revision.model_copy(
        update={"etag": _revision_etag(revision, sorted_nodes, sorted_edges)}
    )
    return SchemaOntology(revision=revision, nodes=sorted_nodes, edges=sorted_edges)


def _mapping_missing_ids(
    mapping: PhysicalMapping,
    current_node_ids: set[str],
) -> list[str]:
    missing: list[str] = []
    object_id = mapping.object_ref.node_id or stable_physical_id(
        mapping.object_ref.object_type,
        mapping.object_ref.owner,
        mapping.object_ref.object_name,
    )
    if object_id not in current_node_ids:
        missing.append(object_id)
    for column in mapping.column_refs:
        column_id = column.node_id or stable_physical_id(
            "column",
            column.owner,
            column.object_name,
            column.column_name,
        )
        if column_id not in current_node_ids:
            missing.append(column_id)
    return missing


def evolve_schema_ontology(
    catalog: SchemaCatalog,
    previous: SchemaOntology,
) -> SchemaOntology:
    """Schema drift 時だけ新 draft を作り、業務定義と orphan mapping を保持する。"""

    fingerprint = catalog_schema_fingerprint(catalog)
    if fingerprint == previous.revision.schema_fingerprint:
        return previous

    current = build_schema_ontology(
        catalog,
        version=previous.revision.version + 1,
        parent_revision_id=previous.revision.id,
    )
    current_node_ids = {node.id for node in current.nodes}
    preserved_nodes: list[OntologyNode] = []
    for node in previous.nodes:
        if node.kind not in _BUSINESS_NODE_KINDS:
            continue
        missing_ids = sorted(
            {
                missing_id
                for mapping in node.physical_mappings
                for missing_id in _mapping_missing_ids(mapping, current_node_ids)
            }
        )
        metadata = dict(node.metadata)
        metadata["drift_from_revision_id"] = previous.revision.id
        if missing_ids:
            metadata["orphaned_mapping_node_ids"] = missing_ids
        else:
            metadata.pop("orphaned_mapping_node_ids", None)
        preserved_nodes.append(
            node.model_copy(
                deep=True,
                update={
                    "revision_id": current.revision.id,
                    "review_status": (
                        OntologyReviewStatus.ORPHANED if missing_ids else node.review_status
                    ),
                    "metadata": metadata,
                },
            )
        )

    all_nodes = sorted([*current.nodes, *preserved_nodes], key=lambda node: node.id)
    all_node_ids = {node.id for node in all_nodes}
    missing_endpoint_ids: set[str] = set()
    preserved_edges: list[OntologyEdge] = []
    for edge in previous.edges:
        if edge.kind not in {
            OntologyEdgeKind.BUSINESS_RELATIONSHIP,
            OntologyEdgeKind.MAPS_TO,
        }:
            continue
        missing_endpoints = sorted(
            endpoint
            for endpoint in (edge.source_node_id, edge.target_node_id)
            if endpoint not in all_node_ids
        )
        missing_endpoint_ids.update(missing_endpoints)
        missing_join_ids = sorted(
            {
                ref.node_id
                for condition in edge.join_conditions
                for ref in (condition.left, condition.right)
                if ref.node_id and ref.node_id not in all_node_ids
            }
        )
        metadata = dict(edge.metadata)
        orphaned_ids = sorted({*missing_endpoints, *missing_join_ids})
        if orphaned_ids:
            metadata["orphaned_mapping_node_ids"] = orphaned_ids
        else:
            metadata.pop("orphaned_mapping_node_ids", None)
        preserved_edges.append(
            edge.model_copy(
                deep=True,
                update={
                    "revision_id": current.revision.id,
                    "review_status": (
                        OntologyReviewStatus.ORPHANED if orphaned_ids else edge.review_status
                    ),
                    "metadata": metadata,
                },
            )
        )

    # 参照切れ edge を保持するには端点も必要。旧版の node を履歴として残し、
    # 最新カタログの物理 scope には戻さない。欠落判定の集合は追加前のままにする。
    previous_nodes = {node.id: node for node in previous.nodes}
    for node_id in sorted(missing_endpoint_ids):
        previous_node = previous_nodes.get(node_id)
        if previous_node is not None:
            all_nodes.append(
                previous_node.model_copy(
                    deep=True,
                    update={
                        "revision_id": current.revision.id,
                        "review_status": OntologyReviewStatus.ORPHANED,
                        "metadata": {
                            **previous_node.metadata,
                            "drift_from_revision_id": previous.revision.id,
                        },
                    },
                )
            )
    all_nodes.sort(key=lambda node: node.id)
    all_edges = sorted([*current.edges, *preserved_edges], key=lambda edge: edge.id)
    revision = current.revision.model_copy(
        update={"etag": _revision_etag(current.revision, all_nodes, all_edges)}
    )
    return SchemaOntology(revision=revision, nodes=all_nodes, edges=all_edges)


def _resolve_profile_object(
    raw_name: str,
    kind: OntologyNodeKind,
    candidates: Sequence[OntologyNode],
    *,
    strict: bool,
) -> OntologyNode | None:
    # Profile object は object_identity の canonical 修飾名（引用名は `SALES."Mixed_Case"`）。
    # dot 分割・大文字化すると大文字の同名表にも一致するため、引用規則どおりに照合する（#561）。
    try:
        parts = object_name_tokens(unicodedata.normalize("NFC", raw_name))
    except ValueError:
        parts = []
    owner = parts[-2] if len(parts) >= 2 else ""
    object_name = parts[-1] if parts else ""
    matches = [
        node
        for node in candidates
        if node.kind == kind
        and _catalog_name_token(str(node.metadata.get("object_name", ""))) == object_name
        and (not owner or _catalog_name_token(str(node.metadata.get("owner", ""))) == owner)
    ]
    if len(matches) > 1:
        qualified = ", ".join(sorted(node.technical_name for node in matches))
        raise AmbiguousPhysicalObjectError(
            f"Profile object '{raw_name}' は複数 schema に一致します: {qualified}"
        )
    if not matches:
        if strict:
            raise ValueError(f"Profile object '{raw_name}' は Ontology に存在しません。")
        return None
    return matches[0]


def migrate_profile_ontology_view(
    profile: Nl2SqlProfile,
    ontology: SchemaOntology,
    *,
    strict: bool = False,
) -> ProfileOntologyView:
    """旧 allowed_tables/views を shared Ontology の profile view へ移行する。"""

    object_nodes = [
        node
        for node in ontology.nodes
        if node.kind in {OntologyNodeKind.TABLE, OntologyNodeKind.VIEW}
        and node.review_status != OntologyReviewStatus.ORPHANED
    ]
    selected_objects: list[OntologyNode] = []
    if not profile.allowed_tables and not profile.allowed_views:
        # 旧 default profile の空 list は「catalog 全体」を意味していたため、移行時も
        # 明示的な物理 scope に展開して unrestricted sentinel を残さない。
        selected_objects.extend(object_nodes)
    for table_name in profile.allowed_tables:
        match = _resolve_profile_object(
            table_name, OntologyNodeKind.TABLE, object_nodes, strict=strict
        )
        if match is not None:
            selected_objects.append(match)
    for view_name in profile.allowed_views:
        match = _resolve_profile_object(
            view_name, OntologyNodeKind.VIEW, object_nodes, strict=strict
        )
        if match is not None:
            selected_objects.append(match)

    selected_objects = list({node.id: node for node in selected_objects}.values())

    selected_object_ids = {node.id for node in selected_objects}
    selected_node_ids = set(selected_object_ids)
    selected_owners = {
        _oracle_name(str(node.metadata.get("owner", ""))) for node in selected_objects
    }
    selected_node_ids.update(
        node.id
        for node in ontology.nodes
        if node.kind == OntologyNodeKind.SCHEMA
        and _oracle_name(str(node.metadata.get("owner", ""))) in selected_owners
    )
    selected_node_ids.update(
        node.id
        for node in ontology.nodes
        if node.kind == OntologyNodeKind.COLUMN
        and node.review_status != OntologyReviewStatus.ORPHANED
        and any(
            mapping.object_ref.node_id in selected_object_ids for mapping in node.physical_mappings
        )
    )
    # Published business nodes remain shared; the profile selects only mappings in scope.
    selected_node_ids.update(
        node.id
        for node in ontology.nodes
        if node.kind in _BUSINESS_NODE_KINDS
        and any(
            mapping.object_ref.node_id in selected_object_ids for mapping in node.physical_mappings
        )
    )

    # Markdown snapshot は Profile 所有。物理 mapping のない共有型・関数等も含める。
    if ontology.revision.id.startswith("ontology_markdown_snapshot"):
        selected_node_ids.update(
            node.id for node in ontology.nodes if node.metadata.get("definition")
        )
        selected_node_ids.update(
            node.id
            for node in ontology.nodes
            if node.kind == OntologyNodeKind.ENUM_VALUE
            and node.metadata.get("derived_from_definition_id") in selected_node_ids
        )
    selected_edges = [
        edge
        for edge in ontology.edges
        if edge.source_node_id in selected_node_ids and edge.target_node_id in selected_node_ids
    ]
    selected_edge_ids = {edge.id for edge in selected_edges}
    allowed_path_ids = sorted(
        edge.id
        for edge in selected_edges
        if edge.kind in _PATH_EDGE_KINDS and edge.review_status == OntologyReviewStatus.APPROVED
    )
    physical_objects = [
        PhysicalObjectRef(
            node_id=node.id,
            owner=str(node.metadata.get("owner", "")),
            object_name=str(node.metadata.get("object_name", "")),
            object_type=_object_type(node.kind),
        )
        for node in sorted(selected_objects, key=lambda item: item.id)
    ]
    view_id = stable_ontology_id("profile_ontology_view", profile.id, ontology.revision.id)
    payload = {
        "id": view_id,
        "profile_id": profile.id,
        "ontology_revision_id": ontology.revision.id,
        "node_ids": sorted(selected_node_ids),
        "edge_ids": sorted(selected_edge_ids),
        "allowed_path_ids": allowed_path_ids,
        "archived": profile.archived,
        "source_profile_etag": profile.etag,
        "source_profile_scope_fingerprint": schema_fingerprint(
            {
                "allowed_tables": sorted(profile.allowed_tables),
                "allowed_views": sorted(profile.allowed_views),
            }
        ),
    }
    return ProfileOntologyView(
        **payload,
        etag=compute_etag(payload, 1),
        physical_objects=physical_objects,
        table_usages_ja={
            node.id: node.description_ja or f"{profile.name} で利用" for node in selected_objects
        },
    )


def _node_labels(
    node: OntologyNode,
) -> list[tuple[str, float, Literal["business_name", "alias", "technical_name", "comment"]]]:
    labels: list[
        tuple[str, float, Literal["business_name", "alias", "technical_name", "comment"]]
    ] = [(node.business_name_ja, 1.0, "business_name")]
    labels.extend((alias, 0.9, "alias") for alias in node.aliases)
    if node.technical_name:
        labels.append((node.technical_name, 0.8, "technical_name"))
    definition = node.metadata.get("definition", {})
    if isinstance(definition, dict):
        labels.extend((v, 0.4, "comment") for v in definition.values() if isinstance(v, str) and v)
    for value in (
        node.description_ja,
        str(node.metadata.get("comment", "")),
    ):
        if value:
            labels.append((value, 0.45, "comment"))
    return labels


def retrieve_ontology_nodes(
    question: str,
    ontology: SchemaOntology,
    profile_view: ProfileOntologyView,
    *,
    profile: Nl2SqlProfile | None = None,
    embedding_callback: EmbeddingRetrievalCallback | None = None,
    limit: int = 24,
) -> list[OntologyRetrievalHit]:
    """Profile view 内だけで lexical/optional embedding retrieval を行う。"""

    if not question.strip():
        raise ValueError("質問は空にできません。")
    if profile_view.ontology_revision_id != ontology.revision.id:
        raise ValueError("Profile view と Ontology revision が一致しません。")
    allowed_ids = set(profile_view.node_ids)
    candidates = [
        node
        for node in ontology.nodes
        if node.id in allowed_ids and node.review_status != OntologyReviewStatus.ORPHANED
    ]
    allowed_ids = {node.id for node in candidates}
    question_key = _normalized_text(question)
    scores: dict[str, float] = defaultdict(float)
    terms: dict[str, set[str]] = defaultdict(set)
    sources: dict[str, set[str]] = defaultdict(set)

    labels_by_node: dict[str, list[tuple[str, float, str]]] = {}
    for node in candidates:
        labels = _node_labels(node)
        labels_by_node[node.id] = list(labels)
        for label, weight, source in labels:
            normalized_label = _normalized_text(label)
            if len(normalized_label) < 2 or normalized_label not in question_key:
                continue
            scores[node.id] = max(
                scores[node.id], weight + min(len(normalized_label) / 100.0, 0.12)
            )
            terms[node.id].add(normalized_label)
            sources[node.id].add(source)

    for edge in ontology.edges:
        if edge.kind != OntologyEdgeKind.LINK_TYPE or edge.id not in profile_view.edge_ids:
            continue
        definition = edge.metadata.get("definition", {})
        edge_labels = [
            edge.relationship_name_ja,
            str(edge.metadata.get("api_name", "")),
            *definition.get("aliases", []),
        ]
        matches = {
            _normalized_text(label)
            for label in edge_labels
            if len(label) >= 2 and _normalized_text(label) in question_key
        }
        if matches:
            for endpoint in (edge.source_node_id, edge.target_node_id):
                if endpoint in allowed_ids:
                    scores[endpoint] = max(scores[endpoint], 0.95)
                    terms[endpoint].update(matches)
                    sources[endpoint].add("business_name")

    if profile is not None:
        for term, definition in profile.glossary.items():
            normalized_term = _normalized_text(term)
            normalized_definition = _normalized_text(definition)
            if len(normalized_term) < 2 or normalized_term not in question_key:
                continue
            for node in candidates:
                target_labels = [
                    _normalized_text(label)
                    for label, _weight, _source in labels_by_node[node.id]
                    if len(_normalized_text(label)) >= 2
                ]
                if not any(label in normalized_definition for label in target_labels):
                    continue
                scores[node.id] = max(scores[node.id], 1.08)
                terms[node.id].add(normalized_term)
                sources[node.id].add("glossary")

    if embedding_callback is not None and candidates:
        try:
            embedding_hits = embedding_callback(question, candidates, limit)
        except Exception:  # Embedding outage must not trigger a different provider.
            logger.warning("ontology_embedding_retrieval_failed", exc_info=True)
        else:
            for node_id, raw_score in embedding_hits:
                if node_id not in allowed_ids:
                    continue
                score = max(0.0, min(float(raw_score), 1.0))
                if score <= 0.0:
                    continue
                scores[node_id] = max(scores[node_id], score * 0.75)
                sources[node_id].add("embedding")

    # 型付き概念の明示参照を辿り、関数・集合等の命中も対象 object に接地する。
    # 関係先 object への際限ない伝播はせず、object に到達したら停止する。
    node_by_id = {node.id: node for node in candidates}
    frontier = list(scores)
    visited = set(frontier)
    while frontier:
        source_id = frontier.pop(0)
        source_node = node_by_id[source_id]
        if source_node.kind in {
            OntologyNodeKind.OBJECT_TYPE,
            OntologyNodeKind.TABLE,
            OntologyNodeKind.VIEW,
        }:
            continue
        for edge in ontology.edges:
            # Interface は下位の継承・実装だけを逆引きする。上位へ進んでから
            # 降り直すと、その上位だけを実装する別 object まで誤接地する。
            reverse_interface = (
                source_node.kind == OntologyNodeKind.INTERFACE
                and (
                    edge.kind == OntologyEdgeKind.IS_A
                    or edge.metadata.get("reference_field") == "implements"
                )
                and edge.target_node_id == source_id
            )
            if (
                source_node.kind == OntologyNodeKind.INTERFACE
                and edge.kind == OntologyEdgeKind.IS_A
                and not reverse_interface
            ):
                continue
            reverse_member = (
                source_node.kind == OntologyNodeKind.ENUM_VALUE
                and edge.kind == OntologyEdgeKind.HAS_VALUE
                and edge.target_node_id == source_id
                and edge.source_node_id == source_node.metadata.get("derived_from_definition_id")
            )
            reverse = reverse_interface or reverse_member
            target = edge.source_node_id if reverse else edge.target_node_id
            if (
                edge.id not in profile_view.edge_ids
                or (edge.source_node_id != source_id and not reverse)
                or edge.kind
                not in {
                    OntologyEdgeKind.USES,
                    OntologyEdgeKind.IS_A,
                    OntologyEdgeKind.DOMAIN,
                    OntologyEdgeKind.RANGE,
                    OntologyEdgeKind.GOVERNS,
                    OntologyEdgeKind.HAS_VALUE,
                }
                or target not in allowed_ids
            ):
                continue
            scores[target] = max(scores[target], scores[source_id] * 0.98)
            terms[target].update(terms[source_id])
            sources[target].update(sources[source_id])
            if target not in visited:
                visited.add(target)
                frontier.append(target)

    hits = [
        OntologyRetrievalHit(
            node_id=node_id,
            score=score,
            matched_terms=sorted(terms[node_id]),
            sources=sorted(sources[node_id]),
        )
        for node_id, score in scores.items()
        if score > 0.0
    ]
    return sorted(hits, key=lambda hit: (-hit.score, hit.node_id))[:limit]


def _edge_allowed(edge: OntologyEdge, profile_view: ProfileOntologyView) -> bool:
    allowed_ids = set(profile_view.allowed_path_ids)
    metadata_path_id = str(edge.metadata.get("path_id", ""))
    metadata_path_ids = {str(value) for value in edge.metadata.get("allowed_path_ids", [])}
    return bool(
        edge.id in allowed_ids
        or (metadata_path_id and metadata_path_id in allowed_ids)
        or metadata_path_ids.intersection(allowed_ids)
    )


def find_bounded_shortest_paths(
    ontology: SchemaOntology,
    profile_view: ProfileOntologyView,
    source_node_id: str,
    target_node_id: str,
    *,
    max_hops: int = 3,
    max_paths: int = 8,
) -> list[IntentRelationshipPath]:
    """Approved + profile-whitelisted edge だけを辿る有界最短経路。"""

    if max_hops < 1:
        raise ValueError("max_hops は 1 以上である必要があります。")
    if source_node_id == target_node_id:
        return []
    view_edge_ids = set(profile_view.edge_ids)
    eligible = [
        edge
        for edge in ontology.edges
        if edge.id in view_edge_ids
        and edge.kind in _PATH_EDGE_KINDS
        and edge.review_status == OntologyReviewStatus.APPROVED
        and _edge_allowed(edge, profile_view)
    ]
    adjacency: dict[str, list[tuple[str, OntologyEdge]]] = defaultdict(list)
    for edge in eligible:
        adjacency[edge.source_node_id].append((edge.target_node_id, edge))
        adjacency[edge.target_node_id].append((edge.source_node_id, edge))
    for neighbors in adjacency.values():
        neighbors.sort(key=lambda item: item[1].id)

    queue: deque[tuple[str, list[str], list[str]]] = deque([(source_node_id, [source_node_id], [])])
    shortest_length: int | None = None
    raw_paths: list[tuple[list[str], list[str]]] = []
    while queue and len(raw_paths) < max_paths:
        current, node_path, edge_path = queue.popleft()
        if shortest_length is not None and len(edge_path) >= shortest_length:
            continue
        if len(edge_path) >= max_hops:
            continue
        for neighbor, edge in adjacency.get(current, []):
            if neighbor in node_path:
                continue
            next_nodes = [*node_path, neighbor]
            next_edges = [*edge_path, edge.id]
            if neighbor == target_node_id:
                shortest_length = len(next_edges)
                raw_paths.append((next_nodes, next_edges))
                continue
            queue.append((neighbor, next_nodes, next_edges))

    node_by_id = {node.id: node for node in ontology.nodes}
    paths: list[IntentRelationshipPath] = []
    for node_ids, edge_ids in raw_paths:
        source = node_by_id.get(node_ids[0])
        target = node_by_id.get(node_ids[-1])
        name = (
            f"{source.business_name_ja} → {target.business_name_ja}"
            if source and target
            else "承認済み関係パス"
        )
        paths.append(
            IntentRelationshipPath(
                id=stable_ontology_id("intent_path", *edge_ids),
                name_ja=name,
                edge_ids=edge_ids,
                node_ids=node_ids,
                approved=True,
                explanation_ja=f"承認済み関係を {len(edge_ids)} hop で接続",
            )
        )
    return paths


def _node_object_ids(node: OntologyNode) -> list[str]:
    if node.kind in {OntologyNodeKind.TABLE, OntologyNodeKind.VIEW}:
        return [node.id]
    return _unique(
        [
            mapping.object_ref.node_id
            for mapping in node.physical_mappings
            if mapping.object_ref.node_id
        ]
    )


def _ambiguity(
    code: str,
    message: str,
    options: Sequence[str] = (),
) -> IntentAmbiguity:
    return IntentAmbiguity(
        id=stable_ontology_id("intent_ambiguity", code, message, *options),
        code=code,
        message_ja=message,
        options=list(options),
        blocking=True,
    )


def _select_mentions(
    hits: Sequence[OntologyRetrievalHit],
    node_by_id: dict[str, OntologyNode],
    kinds: frozenset[OntologyNodeKind],
    *,
    scoped_object_ids: set[str] | None = None,
) -> tuple[list[OntologyNode], list[IntentAmbiguity]]:
    relevant = [hit for hit in hits if node_by_id[hit.node_id].kind in kinds]
    if scoped_object_ids:
        scoped = [
            hit
            for hit in relevant
            if set(_node_object_ids(node_by_id[hit.node_id])).intersection(scoped_object_ids)
        ]
        if scoped:
            relevant = scoped

    by_term: dict[str, list[OntologyRetrievalHit]] = defaultdict(list)
    embedding_only: list[OntologyRetrievalHit] = []
    for hit in relevant:
        if hit.matched_terms:
            for term in hit.matched_terms:
                by_term[term].append(hit)
        else:
            embedding_only.append(hit)

    selected: dict[str, OntologyNode] = {}
    ambiguities: list[IntentAmbiguity] = []
    for term, term_hits in sorted(by_term.items()):
        best_score = max(hit.score for hit in term_hits)
        best = [hit for hit in term_hits if best_score - hit.score <= 0.02]
        candidate_ids = sorted({hit.node_id for hit in best})
        if len(candidate_ids) > 1:
            options = [node_by_id[node_id].technical_name for node_id in candidate_ids]
            ambiguities.append(
                _ambiguity(
                    "ontology_term_ambiguous",
                    f"「{term}」が複数の Ontology 要素に一致します。",
                    options,
                )
            )
            continue
        selected[candidate_ids[0]] = node_by_id[candidate_ids[0]]

    if not by_term and embedding_only:
        best_score = embedding_only[0].score
        best = [hit for hit in embedding_only if best_score - hit.score <= 0.02]
        if len(best) == 1:
            selected[best[0].node_id] = node_by_id[best[0].node_id]
        elif best:
            ambiguities.append(
                _ambiguity(
                    "ontology_embedding_ambiguous",
                    "Embedding 検索だけでは業務要素を一意に確定できません。",
                    [node_by_id[hit.node_id].technical_name for hit in best],
                )
            )
    return sorted(selected.values(), key=lambda node: node.id), ambiguities


def interpret_question_deterministically(
    question: str,
    ontology: SchemaOntology,
    profile_view: ProfileOntologyView,
    *,
    profile: Nl2SqlProfile | None = None,
    embedding_callback: EmbeddingRetrievalCallback | None = None,
    max_hops: int = 3,
) -> QuestionIntentGraph:
    """LLM 不使用で QuestionIntentGraph を作り、不確定要素は hard blocker にする。"""

    hits = retrieve_ontology_nodes(
        question,
        ontology,
        profile_view,
        profile=profile,
        embedding_callback=embedding_callback,
    )
    node_by_id = {node.id: node for node in ontology.nodes}
    entity_nodes, ambiguities = _select_mentions(
        hits,
        node_by_id,
        frozenset(
            {
                OntologyNodeKind.BUSINESS_ENTITY,
                OntologyNodeKind.OBJECT_TYPE,
                OntologyNodeKind.BUSINESS_EVENT,
                OntologyNodeKind.TABLE,
                OntologyNodeKind.VIEW,
            }
        ),
    )
    entity_object_ids = {object_id for node in entity_nodes for object_id in _node_object_ids(node)}
    metric_nodes, metric_ambiguities = _select_mentions(
        hits,
        node_by_id,
        frozenset({OntologyNodeKind.METRIC}),
        scoped_object_ids=entity_object_ids,
    )
    dimension_nodes, dimension_ambiguities = _select_mentions(
        hits,
        node_by_id,
        frozenset({OntologyNodeKind.PROPERTY, OntologyNodeKind.COLUMN}),
        scoped_object_ids=entity_object_ids,
    )
    ambiguities.extend(metric_ambiguities)
    ambiguities.extend(dimension_ambiguities)

    # Explicit entity が無くても、一意な property/metric mapping から安全に導出できる。
    if not entity_nodes:
        derived_object_ids = {
            object_id
            for node in [*metric_nodes, *dimension_nodes]
            for object_id in _node_object_ids(node)
        }
        if len(derived_object_ids) == 1:
            derived = node_by_id.get(next(iter(derived_object_ids)))
            if derived is not None:
                entity_nodes = [derived]
                entity_object_ids = {derived.id}

    if not entity_nodes and not any(
        ambiguity.code in {"ontology_term_ambiguous", "ontology_embedding_ambiguous"}
        for ambiguity in ambiguities
    ):
        ambiguities.append(
            _ambiguity(
                "ontology_entity_not_found",
                "質問から profile 内の業務エンティティを確定できません。",
            )
        )

    entities = [
        IntentEntity(
            id=stable_ontology_id("intent_entity", node.id),
            ontology_node_id=node.id,
            name_ja=node.business_name_ja,
            role=(
                "event"
                if node.kind == OntologyNodeKind.BUSINESS_EVENT
                else ("subject" if index == 0 else "related")
            ),
            physical_object_ids=_node_object_ids(node),
        )
        for index, node in enumerate(entity_nodes)
    ]
    metrics = [
        IntentMetric(
            id=stable_ontology_id("intent_metric", node.id),
            ontology_node_id=node.id,
            name_ja=node.business_name_ja,
            aggregation=str(node.metadata.get("aggregation", "")),
            formula_description_ja=str(node.metadata.get("formula_description_ja", "")),
        )
        for node in metric_nodes
    ]
    if not metrics and entities and any(token in question for token in ("件数", "何件", "数")):
        metrics.append(
            IntentMetric(
                id=stable_ontology_id("intent_metric", entities[0].id, "count"),
                name_ja=f"{entities[0].name_ja}件数",
                aggregation="COUNT",
                formula_description_ja="対象エンティティの件数",
            )
        )
    dimensions = [
        IntentDimension(
            id=stable_ontology_id("intent_dimension", node.id),
            ontology_node_id=node.id,
            name_ja=node.business_name_ja,
            granularity=str(node.metadata.get("granularity", "")),
        )
        for node in dimension_nodes
    ]

    candidate_paths: list[IntentRelationshipPath] = []
    entity_path_endpoints = [
        entity.physical_object_ids[0] if entity.physical_object_ids else entity.ontology_node_id
        for entity in entities
    ]
    for source_id, target_id in combinations(entity_path_endpoints, 2):
        if source_id == target_id:
            continue
        paths = find_bounded_shortest_paths(
            ontology,
            profile_view,
            source_id,
            target_id,
            max_hops=max_hops,
        )
        candidate_paths.extend(paths)
        if not paths:
            ambiguities.append(
                _ambiguity(
                    "join_path_not_approved",
                    "対象エンティティ間に profile で許可された承認済み関係パスがありません。",
                    [source_id, target_id],
                )
            )
        elif len(paths) > 1:
            ambiguities.append(
                _ambiguity(
                    "multiple_join_paths",
                    "同じ長さの承認済み Join パスが複数あります。利用する関係を選択してください。",
                    [path.name_ja for path in paths],
                )
            )

    limit_match = _LIMIT_PATTERN.search(question)
    row_limit = min(int(limit_match.group(1)), 5000) if limit_match else None
    relative_time = next(
        (value for value in ("今月", "先月", "今年", "昨年", "今日", "昨日") if value in question),
        "",
    )
    blockers = sum(ambiguity.blocking and not ambiguity.resolved for ambiguity in ambiguities)
    confidence = (
        max(0.1, 0.45 - blockers * 0.1)
        if blockers
        else min(0.95, 0.62 + len(entities) * 0.08 + len(metrics) * 0.05)
    )
    selected_path_id = candidate_paths[0].id if len(candidate_paths) == 1 else None
    return QuestionIntentGraph(
        question_original=question.strip(),
        question_effective=question.strip(),
        profile_view_id=profile_view.id,
        ontology_revision_id=ontology.revision.id,
        entities=entities,
        metrics=metrics,
        dimensions=dimensions,
        time_range=(IntentTimeRange(relative_expression=relative_time) if relative_time else None),
        limit=row_limit,
        candidate_paths=candidate_paths,
        selected_path_id=selected_path_id,
        ambiguities=ambiguities,
        confidence=confidence,
    )
