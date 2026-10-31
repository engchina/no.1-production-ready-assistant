"""SchemaOntology を mermaid erDiagram へ決定論変換する serializer。

正本は Oracle AI Database 上の JSON(ontology_store)であり、この module は LLM プロンプト注入と
UI プレビュー用の表現を生成するだけ。network・LLM・DB に依存しない。
"""

from __future__ import annotations

import hashlib
import re

from app.features.nl2sql.object_identity import catalog_match_key, object_match_key
from app.features.nl2sql.ontology_catalog import SchemaOntology
from app.features.nl2sql.ontology_models import (
    OntologyEdge,
    OntologyEdgeKind,
    OntologyNode,
    OntologyNodeKind,
    OntologyReviewStatus,
    ProfileOntologyView,
    RelationshipCardinality,
)

_OBJECT_NODE_KINDS = frozenset({OntologyNodeKind.TABLE, OntologyNodeKind.VIEW})
_MERMAID_TOKEN_INVALID_RE = re.compile(r"[^A-Za-z0-9_]+")
_MERMAID_TOKEN_START_RE = re.compile(r"^[A-Za-z_]")

# mermaid ER 記法(source 側, target 側)。未確認は非識別(点線)で描く。
_CARDINALITY_NOTATION: dict[RelationshipCardinality, str] = {
    RelationshipCardinality.ONE_TO_ONE: "||--||",
    RelationshipCardinality.ONE_TO_MANY: "||--o{",
    RelationshipCardinality.MANY_TO_ONE: "}o--||",
    RelationshipCardinality.MANY_TO_MANY: "}o--o{",
    RelationshipCardinality.UNKNOWN: "}o..o{",
}


def _entity_name(node: OntologyNode) -> str:
    owner = str(node.metadata.get("owner", "")).strip()
    object_name = str(node.metadata.get("object_name", "")).strip()
    if owner and object_name:
        return f"{owner}.{object_name}"
    return node.technical_name or node.business_name_ja


def _object_key(node: OntologyNode) -> str:
    """表・ビュー node の照合キー（カタログ上の名前、引用名は大文字小文字を保持）。

    `_entity_name(...).upper()` をキーにすると、`SALES."Mixed_Case"` と大文字の同名表
    `SALES.MIXED_CASE` の列・FK マーカー・業務エンティティが混ざる（#573）。
    """

    owner = str(node.metadata.get("owner", "")).strip()
    object_name = str(node.metadata.get("object_name", "")).strip()
    if owner and object_name:
        return catalog_match_key(owner, object_name)
    return object_match_key(node.technical_name or node.business_name_ja)


def _quoted(value: str) -> str:
    return '"' + value.replace('"', "'") + '"'


def _mermaid_token(value: str, *, fallback: str) -> str:
    raw = value.strip()
    normalized = _MERMAID_TOKEN_INVALID_RE.sub("_", raw)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if not normalized:
        if not raw:
            return fallback
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
        normalized = f"{fallback}_{digest}"
    if _MERMAID_TOKEN_START_RE.match(normalized) is None:
        normalized = f"{fallback}_{normalized}"
    return normalized[:96]


def _attribute_type(node: OntologyNode) -> str:
    data_type = str(node.metadata.get("data_type", "")).strip() or "UNKNOWN"
    return _mermaid_token(data_type, fallback="UNKNOWN")


def _attribute_name(node: OntologyNode) -> str:
    column_name = str(node.metadata.get("column_name", "")).strip() or node.id
    return _mermaid_token(column_name, fallback="COLUMN")


def _label(value: str) -> str:
    return value.replace('"', "'").replace("\n", " ").strip()


def _resolve_object_node(
    node: OntologyNode | None,
    object_node_by_key: dict[str, OntologyNode],
) -> OntologyNode | None:
    """endpoint node(物理 or 業務)を対応する表・ビュー node へ解決する。"""

    if node is None:
        return None
    if node.kind in _OBJECT_NODE_KINDS:
        return node
    for mapping in node.physical_mappings:
        ref = mapping.object_ref
        resolved = object_node_by_key.get(catalog_match_key(ref.owner, ref.object_name))
        if resolved is not None:
            return resolved
    return None


def _join_summary(edge: OntologyEdge) -> str:
    parts = []
    for condition in sorted(edge.join_conditions, key=lambda item: item.ordinal):
        parts.append(
            f"{condition.left.object_name}.{condition.left.column_name} "
            f"{condition.operator} "
            f"{condition.right.object_name}.{condition.right.column_name}"
        )
    return " AND ".join(parts)


def render_mermaid_er(
    ontology: SchemaOntology,
    view: ProfileOntologyView | None = None,
    *,
    max_entities: int = 60,
    max_chars: int = 8000,
) -> str:
    """Profile スコープ(view 指定時)の erDiagram を決定論で生成する。

    - entities: TABLE/VIEW node(ID ソート、max_entities で切り詰め)
    - attributes: COLUMN node(型・FK マーカー・論理名コメント)
    - relationships: FOREIGN_KEY と、承認済みかつ許可 path の BUSINESS_RELATIONSHIP
    - 承認済み業務エンティティ名は %% コメントで併記
    - max_chars 超過時は attributes を落として関係構造を優先する
    """

    scoped_node_ids = set(view.node_ids) if view is not None else None
    scoped_edge_ids = set(view.edge_ids) if view is not None else None
    allowed_path_ids = set(view.allowed_path_ids) if view is not None else None

    def node_in_scope(node: OntologyNode) -> bool:
        return scoped_node_ids is None or node.id in scoped_node_ids

    object_nodes = sorted(
        (
            node
            for node in ontology.nodes
            if node.kind in _OBJECT_NODE_KINDS and node_in_scope(node)
        ),
        key=lambda node: node.id,
    )
    omitted_entities = max(0, len(object_nodes) - max_entities)
    object_nodes = object_nodes[:max_entities]
    object_node_by_key = {_object_key(node): node for node in object_nodes}
    included_node_ids = {node.id for node in object_nodes}

    # 表・ビューごとの列(ordinal 順)。view スコープ内の列だけを載せる。
    columns_by_object: dict[str, list[OntologyNode]] = {}
    for node in ontology.nodes:
        if node.kind != OntologyNodeKind.COLUMN or not node_in_scope(node):
            continue
        owner = str(node.metadata.get("owner", "")).strip()
        object_name = str(node.metadata.get("object_name", "")).strip()
        key = catalog_match_key(owner, object_name)
        if key in object_node_by_key:
            columns_by_object.setdefault(key, []).append(node)
    for columns in columns_by_object.values():
        columns.sort(key=lambda node: (node.metadata.get("ordinal") or 0, node.id))

    # FK 側の列名(FK マーカー用)と関係行。
    node_by_id = {node.id: node for node in ontology.nodes}
    fk_column_keys: set[tuple[str, str]] = set()
    relationship_lines: list[str] = []
    for edge in sorted(ontology.edges, key=lambda edge: edge.id):
        if scoped_edge_ids is not None and edge.id not in scoped_edge_ids:
            continue
        if edge.kind == OntologyEdgeKind.FOREIGN_KEY:
            approved = edge.review_status == OntologyReviewStatus.APPROVED
        elif edge.kind == OntologyEdgeKind.BUSINESS_RELATIONSHIP:
            if edge.review_status != OntologyReviewStatus.APPROVED:
                continue
            if allowed_path_ids is not None and edge.id not in allowed_path_ids:
                continue
            approved = True
        else:
            continue
        source = _resolve_object_node(node_by_id.get(edge.source_node_id), object_node_by_key)
        target = _resolve_object_node(node_by_id.get(edge.target_node_id), object_node_by_key)
        if (
            source is None
            or target is None
            or source.id not in included_node_ids
            or target.id not in included_node_ids
        ):
            continue
        for condition in edge.join_conditions:
            fk_column_keys.add(
                (
                    catalog_match_key(condition.left.owner, condition.left.object_name),
                    catalog_match_key(condition.left.column_name),
                )
            )
        notation = _CARDINALITY_NOTATION[edge.cardinality]
        if not approved:
            notation = notation.replace("--", "..")
        label_parts = [_label(edge.relationship_name_ja)]
        join_summary = _join_summary(edge)
        if join_summary:
            label_parts.append(join_summary)
        relationship_lines.append(
            f"    {_quoted(_entity_name(source))} {notation} "
            f"{_quoted(_entity_name(target))} : {_quoted(' / '.join(label_parts))}"
        )

    # 承認済み業務エンティティ(表・ビューへの mapping を持つもの)は別名コメントで示す。
    business_comments: list[str] = []
    for node in sorted(ontology.nodes, key=lambda node: node.id):
        if node.kind != OntologyNodeKind.BUSINESS_ENTITY or not node_in_scope(node):
            continue
        if node.review_status != OntologyReviewStatus.APPROVED:
            continue
        resolved = _resolve_object_node(node, object_node_by_key)
        if resolved is None or resolved.id not in included_node_ids:
            continue
        business_comments.append(
            f"    %% {_label(node.business_name_ja)} = {_entity_name(resolved)}"
        )

    def entity_block(node: OntologyNode, *, with_attributes: bool) -> list[str]:
        name = _entity_name(node)
        header = f"    {_quoted(name)}"
        logical = _label(node.business_name_ja)
        lines: list[str] = []
        if logical and logical != name.split(".")[-1]:
            lines.append(f"    %% {logical} = {name}")
        columns = columns_by_object.get(_object_key(node), []) if with_attributes else []
        if not columns:
            lines.append(header)
            return lines
        lines.append(f"{header} {{")
        for column in columns:
            raw_column_name = str(column.metadata.get("column_name", "")).strip() or column.id
            column_name = _attribute_name(column)
            owner_key = catalog_match_key(
                str(column.metadata.get("owner", "")), str(column.metadata.get("object_name", ""))
            )
            marker = (
                " FK" if (owner_key, catalog_match_key(raw_column_name)) in fk_column_keys else ""
            )
            logical_name = _label(column.business_name_ja)
            comments: list[str] = []
            if logical_name and logical_name.upper() != raw_column_name.upper():
                comments.append(logical_name)
            if column_name != raw_column_name:
                comments.append(_label(raw_column_name))
            comment_text = " / ".join(dict.fromkeys(item for item in comments if item))
            comment_part = f" {_quoted(comment_text)}" if comment_text else ""
            lines.append(f"        {_attribute_type(column)} {column_name}{marker}{comment_part}")
        lines.append("    }")
        return lines

    def render(*, with_attributes: bool) -> str:
        lines = ["erDiagram"]
        lines.extend(business_comments)
        for node in object_nodes:
            lines.extend(entity_block(node, with_attributes=with_attributes))
        lines.extend(relationship_lines)
        if omitted_entities:
            lines.append(f"    %% omitted: {omitted_entities} entities")
        return "\n".join(lines)

    result = render(with_attributes=True)
    if len(result) > max_chars:
        result = render(with_attributes=False)
    if len(result) > max_chars:
        result = result[:max_chars].rsplit("\n", 1)[0] + "\n    %% truncated"
    return result
