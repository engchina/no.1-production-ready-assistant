"""AI オントロジー構築(スキーマ・Q/A・資料から13種類の関連概念を統合抽出)。

モデルの API の入力 schema は Profile + DB schema catalog から直接作る。
出力は Pydantic(:class:`OntologyBuildExtraction`)で検証し、profile スコープ外の
owner/object/column を参照する候補は Markdown 下書きへ入れず採用外候補として記録する。
生成物は承認済み draft revision と Markdown 下書き artifact として保存され、
publish で Published Markdown へコピーされるまで SQL 生成には使われない。

job と実行入力は Oracle store に永続化する。local は thread、production は独立 worker が
同じ処理を実行し、成果物は Markdown 下書きの確認ゲートを通る。
"""

from __future__ import annotations

import contextvars
import hashlib
import inspect
import json
import logging
import re
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast, get_args
from uuid import uuid4

from pr_backend_core.bounded_calls import BoundedCallAborted, SyncBoundedCaller
from pr_backend_core.observability.model_purpose import call_with_model_purpose
from sqlglot import parse_one

from app.features.nl2sql.ai_calls import (
    BoundedEnterpriseAiClient,
    image_call,
    stage_concurrency,
    sync_ai_caller,
)
from app.features.nl2sql.models import Nl2SqlProfile, SchemaCatalog, SchemaTable
from app.features.nl2sql.object_identity import (
    catalog_match_key,
    format_object_part,
    is_unquoted_object_part,
    object_match_key,
    object_name_tokens,
    split_match_key,
    sql_identifier_token,
)
from app.features.nl2sql.ontology_catalog import SchemaOntology
from app.features.nl2sql.ontology_definition_service import definition_fingerprint
from app.features.nl2sql.ontology_definitions import DefinitionPhase, DefinitionSource
from app.features.nl2sql.ontology_models import (
    JoinCondition,
    MetricDefinition,
    OntologyBuildEvent,
    OntologyBuildExtraction,
    OntologyBuildJob,
    OntologyBuildPhase,
    OntologyBuildStatus,
    OntologyBuildStep,
    OntologyBuildStepName,
    OntologyBuildStepStatus,
    OntologyConceptExtraction,
    OntologyEdge,
    OntologyEdgeKind,
    OntologyEvidence,
    OntologyEvidenceLocatorKind,
    OntologyMetricCandidate,
    OntologyNode,
    OntologyNodeKind,
    OntologyProposalKind,
    OntologyProposalPayload,
    OntologyProvenance,
    OntologyRelationshipCandidate,
    OntologyReviewStatus,
    OntologySourceDocument,
    OntologySourceKind,
    OntologySourceProgress,
    OntologySourceStatus,
    ProfileOntologyView,
    QaPair,
    RelationshipCardinality,
    utc_now,
)
from app.features.nl2sql.ontology_observability import record_job, record_source_extraction
from app.features.nl2sql.ontology_service import (
    OntologyNotFoundError,
    OntologyStateConflictError,
    OntologyVersionConflictError,
)
from app.features.nl2sql.ontology_sources import (
    ExtractedSourceChunk,
    OntologySourceStorage,
    extract_ontology_source,
)
from app.features.nl2sql.ontology_store import (
    OntologyVersionConflict,
    canonical_json,
    physical_identity_part,
    stable_ontology_id,
)
from app.features.nl2sql.sql_semantics import parse_oracle_sql
from app.features.nl2sql.structured_outputs import TextOutput, response_format
from app.settings import get_settings

logger = logging.getLogger(__name__)

_DANGEROUS_EXPRESSION_TOKENS = (";", "--", "/*")
_ONTOLOGY_BUILD_LLM_CONTEXT_MAX_CHARS = 100_000
_LLM_CONTEXT_HEADROOM_CHARS = 512
_ORACLE_PSEUDO_COLUMNS = {"LEVEL", "ORA_ROWSCN", "ROWID", "ROWNUM"}
_QA_SQL_EXAMPLE_SECTION_TITLE = "## Q/A SQL 例"
_QA_SQL_RULE_SECTION_TITLE = "## Q/A 由来 SQL 生成ルール"
_QA_SQL_PATTERN_SECTION_TITLE = "## Q/A SQL 構造パターン"
_QA_SQL_EXAMPLE_BLOCK_RE = re.compile(
    rf"^{re.escape(_QA_SQL_EXAMPLE_SECTION_TITLE)}\s*\n```jsonl?\s*\n(?P<body>.*?)\n```",
    flags=re.MULTILINE | re.DOTALL,
)
_QA_SQL_PATTERN_BLOCK_RE = re.compile(
    rf"^{re.escape(_QA_SQL_PATTERN_SECTION_TITLE)}\s*\n```jsonl?\s*\n(?P<body>.*?)\n```",
    flags=re.MULTILINE | re.DOTALL,
)
_QA_SQL_EXAMPLE_MAX_PROMPT_COUNT = 3
_QA_SQL_PATTERN_MAX_PROMPT_COUNT = 3


# --- DB schema scope / profile view スコープの解決 -------------------------------------------


@dataclass(frozen=True)
class OntologyBuildSchemaContext:
    schema_context: str
    object_count: int
    column_count: int
    schema_fingerprint: str
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _context_name(value: str) -> str:
    """カタログ上の owner / object / column 名を、AI 構築の schema context に載せる表記にする。

    ontology node の technical_name と同じ `physical_identity_part`。大文字化しても変わらない名前
    （`ORDERS`、`売上`）はそのまま、小文字を含む名前だけ `"Mixed_Case"` と SQL の引用表記にする。
    LLM 出力と Q/A SQL の参照は `object_match_key`（引用規則）で照合するため、大文字の同名表
    `SALES.MIXED_CASE` と取り違えない（#573）。引用が不要な名前の表記は従来と同じ値になる。
    """

    return physical_identity_part(value)


def _sql_token(name: str, *, quoted: bool) -> str:
    """SQL parser が返した識別子 1 部分の照合 token。不正な値は例外にせずそのまま返す。"""

    if not str(name or "").strip():
        return ""
    try:
        return sql_identifier_token(name, quoted=quoted)
    except ValueError:
        return str(name).strip()


def _alias_tokens(alias: str) -> set[str]:
    """SQL の表別名・CTE 名の照合 token（引用の有無は保持されないため両方の解釈を登録する）。"""

    raw = str(alias or "").strip()
    if not raw:
        return set()
    return {object_match_key(raw), catalog_match_key(raw)}


def _schema_object_kind(table: SchemaTable) -> str:
    return "view" if "view" in table.table_type.casefold() else "table"


def _schema_object_label(table: SchemaTable) -> str:
    owner = _context_name(table.owner or "APP")
    name = _context_name(table.table_name)
    return f"{owner}.{name}" if owner else name


def _catalog_object_part(value: str) -> str:
    """カタログ上の名前を Profile object と照合する token にする（引用名の大文字小文字を保つ）。"""

    try:
        return format_object_part(value)
    except ValueError:
        return value


def _resolve_catalog_object(
    raw_name: str,
    *,
    object_kind: str,
    candidates: list[SchemaTable],
) -> tuple[SchemaTable | None, str | None]:
    # Profile object は object_identity の canonical 修飾名。引用符を外して大文字化すると
    # `SALES."Mixed_Case"` が大文字の同名表 `SALES.MIXED_CASE` にも一致する（#561）。
    try:
        parts = object_name_tokens(raw_name)
    except ValueError:
        parts = []
    owner = parts[-2] if len(parts) >= 2 else ""
    object_name = parts[-1] if parts else ""
    matches = [
        table
        for table in candidates
        if _schema_object_kind(table) == object_kind
        and _catalog_object_part(table.table_name) == object_name
        and (not owner or _catalog_object_part(table.owner or "APP") == owner)
    ]
    if len(matches) > 1:
        qualified = ", ".join(sorted(_schema_object_label(table) for table in matches))
        return (
            None,
            f"「{raw_name}」は DB schema catalog 内で複数 object に一致します: {qualified}。"
            "owner 付きの object 名を Profile に設定してください。",
        )
    if not matches:
        return (
            None,
            f"「{raw_name}」を DB schema catalog の {object_kind} として解決できません。"
            "DB 構造を再取得するか、Profile の対象 object 名(owner 付き)を確認してください。",
        )
    return matches[0], None


def _selected_schema_objects(
    profile: Nl2SqlProfile,
    catalog: SchemaCatalog,
) -> tuple[list[SchemaTable], list[str], list[str]]:
    if not profile.allowed_tables and not profile.allowed_views:
        return sorted(catalog.tables, key=_schema_object_label), [], []

    selected: dict[tuple[str, str, str], SchemaTable] = {}
    warnings: list[str] = []
    errors: list[str] = []
    for raw_name, object_kind in [
        *((name, "table") for name in profile.allowed_tables),
        *((name, "view") for name in profile.allowed_views),
    ]:
        table, message = _resolve_catalog_object(
            raw_name,
            object_kind=object_kind,
            candidates=catalog.tables,
        )
        if message:
            if "複数 object" in message:
                errors.append(message)
            else:
                warnings.append(message)
            continue
        if table is None:
            continue
        key = (
            _catalog_object_part(table.owner or "APP"),
            _catalog_object_part(table.table_name),
            _schema_object_kind(table),
        )
        selected[key] = table
    return sorted(selected.values(), key=_schema_object_label), warnings, errors


def _constraint_cardinality(detail: Any, source_table: SchemaTable) -> str:
    # 制約の列はカタログ上の名前どうしで比べる（`"Amount"` と `AMOUNT` を同じ列にしない）。
    source_columns = tuple(detail.columns)
    unique_column_sets = {
        tuple(constraint.columns)
        for constraint in source_table.constraint_details
        if constraint.constraint_type in {"P", "U"}
    }
    return "one_to_one" if source_columns in unique_column_sets else "many_to_one"


def build_schema_context_from_catalog(
    profile: Nl2SqlProfile,
    catalog: SchemaCatalog,
) -> OntologyBuildSchemaContext:
    """AI 構築 input 用に、Profile scope の DB schema 情報を catalog から直接作る。"""

    selected_objects, warnings, errors = _selected_schema_objects(profile, catalog)
    selected_keys = {
        catalog_match_key(table.owner or "APP", table.table_name) for table in selected_objects
    }
    objects: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    for table in selected_objects:
        catalog_owner = table.owner or "APP"
        owner = _context_name(catalog_owner)
        object_name = _context_name(table.table_name)
        qualified_name = f"{owner}.{object_name}" if owner else object_name
        objects.append(
            {
                "object": qualified_name,
                "owner": owner,
                "object_name": object_name,
                "object_type": _schema_object_kind(table),
                "logical_name": table.logical_name,
                "comment": table.comment,
                "row_count": table.row_count,
                "columns": [
                    {
                        "column": _context_name(column.column_name),
                        "qualified_column": (
                            f"{qualified_name}.{_context_name(column.column_name)}"
                        ),
                        "data_type": column.data_type,
                        "nullable": column.nullable,
                        "ordinal": ordinal,
                        "logical_name": column.logical_name,
                        "comment": column.comment,
                    }
                    for ordinal, column in enumerate(table.columns, start=1)
                ],
                "constraints": [
                    {
                        "constraint_name": detail.constraint_name,
                        "constraint_type": detail.constraint_type,
                        "columns": [_context_name(column) for column in detail.columns],
                    }
                    for detail in table.constraint_details
                    if detail.constraint_type in {"P", "U", "C"}
                ],
            }
        )
        for detail in table.constraint_details:
            if detail.constraint_type != "R" or not detail.referenced_table:
                continue
            catalog_target_owner = detail.referenced_owner or catalog_owner
            if catalog_match_key(catalog_target_owner, detail.referenced_table) not in (
                selected_keys
            ):
                continue
            target = (
                f"{_context_name(catalog_target_owner)}.{_context_name(detail.referenced_table)}"
            )
            relationships.append(
                {
                    "id": detail.constraint_name,
                    "kind": "foreign_key",
                    "source_object": qualified_name,
                    "target_object": target,
                    "relationship_name_ja": f"{qualified_name} → {target}",
                    "description_ja": f"Oracle 外部キー {detail.constraint_name}",
                    "cardinality": _constraint_cardinality(detail, table),
                    "review_status": "approved" if detail.status == "ENABLED" else "proposed",
                    "allowed_path": True,
                    "join_conditions": [
                        {
                            "left": f"{qualified_name}.{_context_name(left)}",
                            "right": f"{target}.{_context_name(right)}",
                            "operator": "=",
                            "ordinal": ordinal,
                            "expression": (
                                f"{qualified_name}.{_context_name(left)} = "
                                f"{target}.{_context_name(right)}"
                            ),
                        }
                        for ordinal, (left, right) in enumerate(
                            zip(detail.columns, detail.referenced_columns, strict=False),
                            start=1,
                        )
                    ],
                }
            )

    for dependency in catalog.view_dependencies:
        catalog_source_owner = dependency.owner or "APP"
        catalog_target_owner = dependency.referenced_owner or catalog_source_owner
        if catalog_match_key(catalog_source_owner, dependency.view_name) not in selected_keys:
            continue
        if catalog_match_key(catalog_target_owner, dependency.referenced_name) not in selected_keys:
            continue
        source = f"{_context_name(catalog_source_owner)}.{_context_name(dependency.view_name)}"
        target = (
            f"{_context_name(catalog_target_owner)}.{_context_name(dependency.referenced_name)}"
        )
        relationships.append(
            {
                "id": f"view_dependency:{source}:{target}",
                "kind": "view_dependency",
                "source_object": source,
                "target_object": target,
                "relationship_name_ja": "参照",
                "description_ja": "Oracle ビュー依存関係",
                "cardinality": "unknown",
                "review_status": "approved",
                "allowed_path": True,
                "join_conditions": [],
            }
        )

    schema_context = json.dumps(
        {
            "profile": {"id": profile.id, "name": profile.name},
            "objects": sorted(objects, key=lambda item: str(item["object"])),
            "relationships": sorted(
                relationships,
                key=lambda item: (
                    str(item["kind"]),
                    str(item["source_object"]),
                    str(item["target_object"]),
                    str(item["id"]),
                ),
            ),
            "warnings": warnings,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return OntologyBuildSchemaContext(
        schema_context=schema_context,
        object_count=len(objects),
        column_count=sum(len(item["columns"]) for item in objects),
        schema_fingerprint=hashlib.sha256(
            json.dumps(
                {"objects": objects, "relationships": relationships},
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
        warnings=warnings,
        errors=errors,
    )


class _ScopeResolver:
    """profile view 内の物理 object/column に限定した参照解決。

    キーは `object_identity` の照合キー（引用なしは大文字、引用名は大文字小文字を保持）。
    LLM 出力・Q/A SQL の参照は SQL と同じ表記として `object_match_key` で解釈する。大文字化して
    照合すると `SALES."Mixed_Case"` と大文字の同名表 `SALES.MIXED_CASE` を区別できない（#573）。
    """

    def __init__(self, ontology: SchemaOntology, view: ProfileOntologyView) -> None:
        scoped = set(view.node_ids)
        self.objects: dict[str, OntologyNode] = {}
        self.objects_by_name: dict[str, list[OntologyNode]] = {}
        self.columns: dict[str, OntologyNode] = {}
        self.columns_by_name: dict[str, list[OntologyNode]] = {}
        self.sql_aliases: dict[str, set[str]] = {}
        for node in ontology.nodes:
            if node.id not in scoped:
                continue
            owner = str(node.metadata.get("owner", ""))
            name = str(node.metadata.get("object_name", ""))
            if node.kind in {OntologyNodeKind.TABLE, OntologyNodeKind.VIEW}:
                self.objects[catalog_match_key(owner, name)] = node
                self.objects_by_name.setdefault(catalog_match_key(name), []).append(node)
            elif node.kind == OntologyNodeKind.COLUMN:
                column = str(node.metadata.get("column_name", ""))
                self.columns[catalog_match_key(owner, name, column)] = node
                self.columns_by_name.setdefault(catalog_match_key(column), []).append(node)

    @staticmethod
    def _object_node_key(node: OntologyNode) -> str:
        return catalog_match_key(
            str(node.metadata.get("owner", "")), str(node.metadata.get("object_name", ""))
        )

    def _resolve_object_key(self, reference: str) -> str | None:
        key = object_match_key(reference)
        if not key:
            return None
        if key in self.objects:
            return key
        alias_targets = self.sql_aliases.get(key, set())
        if len(alias_targets) == 1:
            return next(iter(alias_targets))
        parts = split_match_key(key)
        lookup_name = parts[-1] if parts else key
        candidates = self.objects_by_name.get(lookup_name, [])
        if len(candidates) == 1:
            return self._object_node_key(candidates[0])
        return None

    def resolve_object(self, reference: str) -> OntologyNode | None:
        key = self._resolve_object_key(reference)
        return self.objects.get(key) if key is not None else None

    def register_sql_aliases(self, sql_texts: Sequence[str]) -> None:
        """Q/A SQL 内の table alias を profile scope の物理 object に結び直す。"""

        for sql in sql_texts:
            graph = parse_oracle_sql(sql).graph
            if graph is None:
                continue
            for table in graph.tables:
                if table.is_cte:
                    continue
                owner = _sql_token(table.owner, quoted=table.owner_quoted)
                name = _sql_token(table.name, quoted=table.name_quoted)
                object_key = self._resolve_object_key(f"{owner}.{name}" if owner else name)
                if object_key is None:
                    continue
                for token in {name, *_alias_tokens(table.alias)}:
                    if token:
                        self.sql_aliases.setdefault(token, set()).add(object_key)

    def resolve_column(self, reference: str) -> OntologyNode | None:
        parts = split_match_key(object_match_key(reference))
        if len(parts) >= 3:
            exact = self.columns.get(".".join(parts[-3:]))
            if exact is not None:
                return exact
            matches = [
                node
                for node_key, node in self.columns.items()
                if split_match_key(node_key)[-2:] == parts[-2:]
            ]
            return matches[0] if len(matches) == 1 else None
        if len(parts) == 2:
            alias_targets = self.sql_aliases.get(parts[0], set())
            alias_matches = [
                node
                for object_key in sorted(alias_targets)
                if (node := self.columns.get(f"{object_key}.{parts[1]}")) is not None
            ]
            if len(alias_matches) == 1:
                return alias_matches[0]
            # OBJECT.COLUMN 形式は owner が一意に決まる場合だけ解決する
            matches = [
                node
                for node_key, node in self.columns.items()
                if split_match_key(node_key)[-2:] == parts
            ]
            return matches[0] if len(matches) == 1 else None
        if len(parts) == 1:
            matches = self.columns_by_name.get(parts[0], [])
            return matches[0] if len(matches) == 1 else None
        return None


def _physical_object_label(node: OntologyNode) -> str:
    if node.physical_mappings:
        ref = node.physical_mappings[0].object_ref
        object_name = _context_name(ref.object_name)
        return f"{_context_name(ref.owner)}.{object_name}" if ref.owner else object_name
    owner = str(node.metadata.get("owner", "")).strip()
    object_name = str(node.metadata.get("object_name", "")).strip()
    if object_name:
        return (
            f"{_context_name(owner)}.{_context_name(object_name)}"
            if owner
            else _context_name(object_name)
        )
    return node.technical_name or node.id


def _column_ref_label(ref: Any) -> str:
    # カタログ上の名前を schema context と同じ表記（小文字を含む名前だけ `"..."`）で示す。
    owner = _context_name(str(getattr(ref, "owner", "") or ""))
    object_name = _context_name(str(getattr(ref, "object_name", "") or ""))
    column_name = _context_name(str(getattr(ref, "column_name", "") or ""))
    if owner and object_name and column_name:
        return f"{owner}.{object_name}.{column_name}"
    if object_name and column_name:
        return f"{object_name}.{column_name}"
    return column_name or object_name or owner


def _join_condition_label(condition: JoinCondition) -> str:
    return (
        f"{_column_ref_label(condition.left)} {condition.operator} "
        f"{_column_ref_label(condition.right)}"
    )


def build_schema_context(ontology: SchemaOntology, view: ProfileOntologyView) -> str:
    """LLM に渡す profile スコープの schema 情報(JSON 文字列、決定論)。"""

    scoped = set(view.node_ids)
    scoped_edges = set(view.edge_ids)
    objects: dict[str, dict[str, Any]] = {}
    for node in sorted(ontology.nodes, key=lambda item: item.id):
        if node.id not in scoped:
            continue
        if node.kind in {OntologyNodeKind.TABLE, OntologyNodeKind.VIEW}:
            objects[_ScopeResolver._object_node_key(node)] = {
                "object": node.technical_name,
                "object_type": node.kind.value,
                "logical_name": node.business_name_ja,
                "comment": node.description_ja,
                "table_usage_ja": view.table_usages_ja.get(node.id, ""),
                "columns": [],
            }
    for node in sorted(ontology.nodes, key=lambda item: item.id):
        if node.id not in scoped or node.kind != OntologyNodeKind.COLUMN:
            continue
        entry = objects.get(_ScopeResolver._object_node_key(node))
        if entry is None:
            continue
        entry["columns"].append(
            {
                "column": _context_name(str(node.metadata.get("column_name", ""))),
                "data_type": str(node.metadata.get("data_type", "")),
                "nullable": bool(node.metadata.get("nullable", True)),
                "ordinal": node.metadata.get("ordinal"),
                "logical_name": node.business_name_ja,
                "comment": node.description_ja,
            }
        )
    node_by_id = {node.id: node for node in ontology.nodes}
    relationships: list[dict[str, Any]] = []
    for edge in sorted(ontology.edges, key=lambda item: item.id):
        if edge.id not in scoped_edges:
            continue
        if edge.kind not in {
            OntologyEdgeKind.FOREIGN_KEY,
            OntologyEdgeKind.BUSINESS_RELATIONSHIP,
            OntologyEdgeKind.JOINS,
        }:
            continue
        source = node_by_id.get(edge.source_node_id)
        target = node_by_id.get(edge.target_node_id)
        if source is None or target is None:
            continue
        relationships.append(
            {
                "id": edge.id,
                "kind": edge.kind.value,
                "source_object": _physical_object_label(source),
                "target_object": _physical_object_label(target),
                "relationship_name_ja": edge.relationship_name_ja,
                "description_ja": edge.description_ja,
                "cardinality": edge.cardinality.value,
                "review_status": edge.review_status.value,
                "allowed_path": edge.id in set(view.allowed_path_ids),
                "join_conditions": [
                    {
                        "left": _column_ref_label(condition.left),
                        "right": _column_ref_label(condition.right),
                        "operator": condition.operator,
                        "ordinal": condition.ordinal,
                        "expression": _join_condition_label(condition),
                    }
                    for condition in sorted(edge.join_conditions, key=lambda item: item.ordinal)
                ],
            }
        )
    return json.dumps(
        {
            "objects": sorted(objects.values(), key=lambda item: str(item["object"])),
            "relationships": relationships,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


# --- 候補 → proposal 変換 ---------------------------------------------------------------------


@dataclass
class ProposalDraft:
    kind: OntologyProposalKind
    title_ja: str
    description_ja: str
    payload: OntologyProposalPayload


@dataclass
class _ConversionResult:
    drafts: list[ProposalDraft] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _provenance(
    job_id: str,
    inferred_by: str,
    evidence_ja: str,
    source_evidence: list[Any] | None = None,
) -> OntologyProvenance:
    return OntologyProvenance(
        source_kind=OntologySourceKind.INFERRED,
        source_id=f"ontology_build:{job_id}",
        source_detail=evidence_ja,
        inferred_by=inferred_by,
        evidence=list(source_evidence or []),
    )


def _business_entity_node(
    object_node: OntologyNode,
    *,
    revision_id: str,
    business_name_ja: str,
    description_ja: str,
    aliases: list[str],
    confidence: float,
    provenance: OntologyProvenance,
    synthetic: bool,
) -> OntologyNode:
    owner = str(object_node.metadata.get("owner", ""))
    object_name = str(object_node.metadata.get("object_name", ""))
    metadata: dict[str, Any] = {"owner": owner, "object_name": object_name}
    if synthetic:
        # 関係提案の endpoint 用に自動生成した最小ノード。accept 時に既存ノードがあれば
        # 上書きせずスキップされる。
        metadata["synthetic_endpoint"] = True
    return OntologyNode(
        id=stable_ontology_id("business_entity", owner, object_name),
        revision_id=revision_id,
        kind=OntologyNodeKind.BUSINESS_ENTITY,
        technical_name=object_node.technical_name,
        business_name_ja=business_name_ja,
        description_ja=description_ja,
        aliases=aliases,
        # 検証(BUSINESS_OBJECT_MAPPING_SPOOFED)を通すため、物理ノードの安定参照を複製する
        physical_mappings=[object_node.physical_mappings[0].model_copy(deep=True)],
        provenance=provenance,
        confidence=confidence,
        review_status=OntologyReviewStatus.PROPOSED,
        metadata=metadata,
    )


def _maps_to_edge(
    business_node: OntologyNode,
    physical_node: OntologyNode,
    *,
    revision_id: str,
    provenance: OntologyProvenance,
) -> OntologyEdge:
    return OntologyEdge(
        id=stable_ontology_id("maps_to", business_node.id, physical_node.id),
        revision_id=revision_id,
        kind=OntologyEdgeKind.MAPS_TO,
        source_node_id=business_node.id,
        target_node_id=physical_node.id,
        relationship_name_ja="物理マッピング",
        provenance=provenance,
        review_status=OntologyReviewStatus.PROPOSED,
    )


def _upserts_payload(
    kind: OntologyProposalKind,
    nodes: list[OntologyNode],
    edges: list[OntologyEdge],
) -> OntologyProposalPayload:
    return OntologyProposalPayload(
        kind=kind,
        values={
            "node_upserts": [node.model_dump(mode="json") for node in nodes],
            "edge_upserts": [edge.model_dump(mode="json") for edge in edges],
        },
    )


_NON_ACTIONABLE_PROPOSAL_WARNING_FRAGMENTS = (
    "profile 範囲外のため提案化しません",
    "profile 範囲内に解決できません",
)
_INTERNAL_DIAGNOSTIC_WARNING_PATTERNS = (
    re.compile(r"(?:^|[^A-Za-z0-9_])schema_resolved_[A-Za-z0-9_]*"),
)


def _unique_non_empty_messages(messages: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(message.strip() for message in messages if message.strip()))


def _is_non_actionable_proposal_rejection(message: str) -> bool:
    if "Q/A の SQL に" in message:
        return "現れないため提案化しません" in message
    return any(fragment in message for fragment in _NON_ACTIONABLE_PROPOSAL_WARNING_FRAGMENTS)


def _is_internal_diagnostic_warning(message: str) -> bool:
    return any(pattern.search(message) for pattern in _INTERNAL_DIAGNOSTIC_WARNING_PATTERNS)


def _is_content_note(message: str) -> bool:
    """資料の不足は下書きの補足に保持し、実行上の警告とは分ける。"""
    return (
        message.endswith("証拠の資料・位置・原文を照合できません。")
        or message.endswith("根拠を確認できない推論です。業務担当者の確認が必要です。")
        or (message.startswith("business_text_chunks") and "正の業務記述なし" in message)
    )


def _split_ontology_build_warnings(messages: Iterable[str]) -> tuple[list[str], list[str]]:
    actionable: list[str] = []
    proposal_rejections: list[str] = []
    for message in _unique_non_empty_messages(messages):
        if _is_internal_diagnostic_warning(message) or _is_content_note(message):
            continue
        if _is_non_actionable_proposal_rejection(message):
            proposal_rejections.append(message)
        else:
            actionable.append(message)
    return actionable, proposal_rejections


def _build_result_counts_ja(
    proposal_count: int,
    *,
    warning_count: int,
    rejected_count: int,
) -> str:
    parts = [f"候補 {proposal_count} 件"]
    if warning_count:
        parts.append(f"警告 {warning_count} 件")
    if rejected_count:
        parts.append(f"採用外 {rejected_count} 件")
    return "、".join(parts)


_QUOTED_SQL_IDENTIFIER_RE = re.compile(r'"(?:[^"]|"")*"')


@dataclass(frozen=True)
class _QaSqlColumns:
    """Q/A SQL が参照する列（Join 候補の裏付け確認用）。"""

    sql: str
    # SQL parser が解決した列名の照合 token（引用なしは大文字、引用名は保持）。
    tokens: frozenset[str]

    @classmethod
    def from_sql(cls, sql: str) -> _QaSqlColumns:
        graph = parse_oracle_sql(sql).graph
        columns = graph.columns if graph is not None else []
        tokens = frozenset(_sql_token(column.name, quoted=column.name_quoted) for column in columns)
        return cls(sql=sql, tokens=tokens)

    def mentions(self, column_node: OntologyNode) -> bool:
        name = str(column_node.metadata.get("column_name", ""))
        if catalog_match_key(name) in self.tokens:
            return True
        # USING 句や解析できない SQL の列は文字列で確認する。引用が不要な名前は引用識別子を
        # 除いた本文を大文字で探し（`"Amount"` を `AMOUNT` の出現と数えない）、引用名は
        # `"Amount"` の表記そのものを探す（#573）。
        if not name:
            return False
        if f'"{name}"' in self.sql:
            return True
        return (
            is_unquoted_object_part(name)
            and name in _QUOTED_SQL_IDENTIFIER_RE.sub(" ", self.sql).upper()
        )


def _convert_relationship(
    candidate: OntologyRelationshipCandidate,
    resolver: _ScopeResolver,
    *,
    revision_id: str,
    provenance: OntologyProvenance,
    qa_sql_texts: list[_QaSqlColumns] | None,
    result: _ConversionResult,
) -> None:
    source = resolver.resolve_object(candidate.source_object)
    target = resolver.resolve_object(candidate.target_object)
    if source is None or target is None:
        result.warnings.append(
            f"関係候補 {candidate.source_object} → {candidate.target_object} は "
            "profile 範囲外のため提案化しません。"
        )
        return
    if not candidate.join_conditions:
        result.warnings.append(
            f"関係候補 {candidate.relationship_name_ja} に Join 条件がないため提案化しません。"
        )
        return
    if candidate.cardinality is RelationshipCardinality.UNKNOWN:
        result.warnings.append(
            f"関係候補 {candidate.relationship_name_ja} の cardinality が未確定(unknown)です。"
            "承認前に確認してください。"
        )
    join_conditions: list[JoinCondition] = []
    for ordinal, item in enumerate(candidate.join_conditions, start=1):
        left = resolver.resolve_column(item.left)
        right = resolver.resolve_column(item.right)
        if left is None or right is None:
            result.warnings.append(
                f"関係候補 {candidate.relationship_name_ja} の Join 列 "
                f"({item.left} {item.operator} {item.right}) を profile 範囲内に解決できません。"
            )
            return
        if qa_sql_texts is not None and not any(
            sql.mentions(left) and sql.mentions(right) for sql in qa_sql_texts
        ):
            result.warnings.append(
                f"関係候補 {candidate.relationship_name_ja} の Join 列が Q/A の SQL に "
                "現れないため提案化しません。"
            )
            return
        join_conditions.append(
            JoinCondition(
                # 検証(BUSINESS_COLUMN_MAPPING_SPOOFED)を通すため列の安定参照を複製する
                left=left.physical_mappings[0].column_refs[0].model_copy(deep=True),
                right=right.physical_mappings[0].column_refs[0].model_copy(deep=True),
                operator=item.operator,
                ordinal=ordinal,
            )
        )
    nodes: list[OntologyNode] = []
    edges: list[OntologyEdge] = []
    endpoints: list[OntologyNode] = []
    for object_node in (source, target):
        business = _business_entity_node(
            object_node,
            revision_id=revision_id,
            business_name_ja=object_node.business_name_ja,
            description_ja=object_node.description_ja,
            aliases=list(object_node.aliases),
            confidence=candidate.confidence,
            provenance=provenance,
            synthetic=True,
        )
        endpoints.append(business)
        nodes.append(business)
        edges.append(
            _maps_to_edge(business, object_node, revision_id=revision_id, provenance=provenance)
        )
    edges.append(
        OntologyEdge(
            id=stable_ontology_id(
                "business_relationship",
                endpoints[0].id,
                endpoints[1].id,
                [f"{item.left}={item.right}" for item in candidate.join_conditions],
            ),
            revision_id=revision_id,
            kind=OntologyEdgeKind.BUSINESS_RELATIONSHIP,
            source_node_id=endpoints[0].id,
            target_node_id=endpoints[1].id,
            relationship_name_ja=candidate.relationship_name_ja,
            description_ja=candidate.evidence_ja,
            cardinality=candidate.cardinality,
            join_conditions=join_conditions,
            provenance=provenance,
            confidence=candidate.confidence,
            review_status=OntologyReviewStatus.PROPOSED,
        )
    )
    result.drafts.append(
        ProposalDraft(
            kind=OntologyProposalKind.RELATIONSHIP,
            title_ja=f"業務関係の提案: {candidate.relationship_name_ja}",
            description_ja=candidate.evidence_ja
            or f"{source.technical_name} と {target.technical_name} の関係候補",
            payload=_upserts_payload(OntologyProposalKind.RELATIONSHIP, nodes, edges),
        )
    )


def _convert_metric(
    candidate: OntologyMetricCandidate,
    resolver: _ScopeResolver,
    *,
    revision_id: str,
    provenance: OntologyProvenance,
    result: _ConversionResult,
) -> None:
    expression_upper = f" {candidate.expression_sql.upper()} "
    if any(token in expression_upper for token in _DANGEROUS_EXPRESSION_TOKENS):
        result.warnings.append(
            f"指標候補 {candidate.metric_name_ja} の式に危険な token が含まれるため提案化しません。"
        )
        return
    column_nodes: list[OntologyNode] = []
    for reference in candidate.base_columns:
        column = resolver.resolve_column(reference)
        if column is None:
            result.warnings.append(
                f"指標候補 {candidate.metric_name_ja} の列 {reference} を "
                "profile 範囲内に解決できません。"
            )
            return
        column_nodes.append(column)
    if not column_nodes:
        result.warnings.append(
            f"指標候補 {candidate.metric_name_ja} に基準列がないため提案化しません。"
        )
        return
    metric_node_id = stable_ontology_id("metric", candidate.metric_name_ja)
    definition = MetricDefinition(
        id=stable_ontology_id("metric_definition", metric_node_id),
        metric_node_id=metric_node_id,
        expression_sql=candidate.expression_sql,
        aggregation=candidate.aggregation,
        base_column_node_ids=[column.id for column in column_nodes],
        unit=candidate.unit,
        description_ja=candidate.description_ja,
    )
    node = OntologyNode(
        id=metric_node_id,
        revision_id=revision_id,
        kind=OntologyNodeKind.METRIC,
        technical_name=candidate.metric_name_ja,
        business_name_ja=candidate.metric_name_ja,
        description_ja=candidate.description_ja,
        physical_mappings=[
            column.physical_mappings[0].model_copy(deep=True) for column in column_nodes
        ],
        provenance=provenance,
        confidence=candidate.confidence,
        review_status=OntologyReviewStatus.PROPOSED,
        metadata={"metric_definition": definition.model_dump(mode="json")},
    )
    result.drafts.append(
        ProposalDraft(
            kind=OntologyProposalKind.METRIC_DEFINITION,
            title_ja=f"指標定義の提案: {candidate.metric_name_ja}",
            description_ja=candidate.evidence_ja or candidate.description_ja,
            payload=_upserts_payload(OntologyProposalKind.METRIC_DEFINITION, [node], []),
        )
    )


def convert_extraction_to_proposals(
    extraction: OntologyBuildExtraction,
    *,
    ontology: SchemaOntology,
    view: ProfileOntologyView,
    job_id: str,
    inferred_by: str,
    qa_sql_texts: list[str] | None = None,
    source_evidence: list[Any] | None = None,
) -> tuple[list[ProposalDraft], list[str]]:
    """検証済み LLM 出力を承認フロー用の proposal 下書きへ決定論変換する。"""

    resolver = _ScopeResolver(ontology, view)
    if qa_sql_texts is not None:
        resolver.register_sql_aliases(qa_sql_texts)
    revision_id = ontology.revision.id
    result = _ConversionResult(warnings=list(extraction.warnings_ja))
    normalized_qa = (
        [_QaSqlColumns.from_sql(sql) for sql in qa_sql_texts] if qa_sql_texts is not None else None
    )

    # 同義語は entity 候補の aliases に合流させる(対象 object が同じもの)。
    alias_by_object: dict[str, list[str]] = {}
    for synonym in extraction.synonyms:
        target_object = resolver.resolve_object(synonym.target)
        if target_object is not None:
            alias_by_object.setdefault(target_object.id, []).extend(synonym.aliases)
            continue
        target_column = resolver.resolve_column(synonym.target)
        if target_column is None:
            result.warnings.append(
                f"同義語候補 {synonym.target} を profile 範囲内に解決できません。"
            )
            continue
        term_node = OntologyNode(
            id=stable_ontology_id("business_term", target_column.id),
            revision_id=revision_id,
            kind=OntologyNodeKind.BUSINESS_TERM,
            technical_name=target_column.technical_name,
            business_name_ja=synonym.aliases[0],
            description_ja=synonym.evidence_ja,
            aliases=synonym.aliases,
            physical_mappings=[target_column.physical_mappings[0].model_copy(deep=True)],
            provenance=_provenance(job_id, inferred_by, synonym.evidence_ja, source_evidence),
            review_status=OntologyReviewStatus.PROPOSED,
        )
        term_edge = _maps_to_edge(
            term_node,
            target_column,
            revision_id=revision_id,
            provenance=_provenance(job_id, inferred_by, synonym.evidence_ja, source_evidence),
        )
        result.drafts.append(
            ProposalDraft(
                kind=OntologyProposalKind.ALIAS,
                title_ja=f"同義語の提案: {synonym.target}",
                description_ja=synonym.evidence_ja or "、".join(synonym.aliases),
                payload=_upserts_payload(OntologyProposalKind.ALIAS, [term_node], [term_edge]),
            )
        )

    for candidate in extraction.entities:
        object_node = resolver.resolve_object(candidate.object_name)
        if object_node is None:
            result.warnings.append(
                f"命名候補 {candidate.object_name} を profile 範囲内に解決できません。"
            )
            continue
        provenance = _provenance(job_id, inferred_by, candidate.description_ja, source_evidence)
        aliases = [*dict.fromkeys([*candidate.aliases, *alias_by_object.pop(object_node.id, [])])]
        business = _business_entity_node(
            object_node,
            revision_id=revision_id,
            business_name_ja=candidate.business_name_ja,
            description_ja=candidate.description_ja,
            aliases=aliases,
            confidence=candidate.confidence,
            provenance=provenance,
            synthetic=False,
        )
        edge = _maps_to_edge(business, object_node, revision_id=revision_id, provenance=provenance)
        result.drafts.append(
            ProposalDraft(
                kind=OntologyProposalKind.MAPPING,
                title_ja=f"業務エンティティ命名: {candidate.business_name_ja}",
                description_ja=candidate.description_ja
                or f"{object_node.technical_name} の業務名候補",
                payload=_upserts_payload(OntologyProposalKind.MAPPING, [business], [edge]),
            )
        )

    # entity 候補に合流できなかった同義語(object 対象)は alias 専用の提案にする。
    for object_id, aliases in alias_by_object.items():
        object_node = next(
            (node for node in resolver.objects.values() if node.id == object_id), None
        )
        if object_node is None:
            continue
        provenance = _provenance(job_id, inferred_by, "同義語の提案", source_evidence)
        business = _business_entity_node(
            object_node,
            revision_id=revision_id,
            business_name_ja=object_node.business_name_ja,
            description_ja=object_node.description_ja,
            aliases=[*dict.fromkeys(aliases)],
            confidence=0.6,
            provenance=provenance,
            synthetic=False,
        )
        edge = _maps_to_edge(business, object_node, revision_id=revision_id, provenance=provenance)
        result.drafts.append(
            ProposalDraft(
                kind=OntologyProposalKind.ALIAS,
                title_ja=f"同義語の提案: {object_node.technical_name}",
                description_ja="、".join(dict.fromkeys(aliases)),
                payload=_upserts_payload(OntologyProposalKind.ALIAS, [business], [edge]),
            )
        )

    for relationship in extraction.relationships:
        _convert_relationship(
            relationship,
            resolver,
            revision_id=revision_id,
            provenance=_provenance(job_id, inferred_by, relationship.evidence_ja, source_evidence),
            qa_sql_texts=normalized_qa,
            result=result,
        )

    for metric in extraction.metrics:
        _convert_metric(
            metric,
            resolver,
            revision_id=revision_id,
            provenance=_provenance(job_id, inferred_by, metric.evidence_ja, source_evidence),
            result=result,
        )

    return result.drafts, result.warnings


# --- Markdown output --------------------------------------------------------------------------


def _md_text(value: Any, fallback: str = "未設定") -> str:
    text = " ".join(str(value or "").split())
    return text or fallback


def _md_code(value: Any, fallback: str = "未設定") -> str:
    text = _md_text(value, fallback).replace("`", "'")
    return f"`{text}`"


def _markdown_json_payloads(markdown: str, block_pattern: re.Pattern[str]) -> list[object]:
    payloads: list[object] = []
    for match in block_pattern.finditer(markdown):
        body = match.group("body").strip()
        if not body:
            continue
        if body.startswith("["):
            try:
                loaded = json.loads(body)
            except json.JSONDecodeError:
                continue
            if isinstance(loaded, list):
                payloads.extend(loaded)
            continue
        for line in body.splitlines():
            cleaned = line.strip()
            if not cleaned:
                continue
            try:
                payloads.append(json.loads(cleaned))
            except json.JSONDecodeError:
                continue
    return payloads


def qa_markdown_sections(markdown: str) -> str:
    """Q/A 由来の SQL 生成ルール・構造パターンの節のうち、中身があるものだけを返す（#1720）。"""

    sections: list[str] = []
    for title in (_QA_SQL_RULE_SECTION_TITLE, _QA_SQL_PATTERN_SECTION_TITLE):
        match = re.search(
            rf"^{re.escape(title)}[ \t]*\n(?P<body>.*?)(?=^#{{1,2}} |\Z)",
            markdown,
            flags=re.MULTILINE | re.DOTALL,
        )
        body = match.group("body").strip() if match else ""
        if body and body != "- なし":
            sections.append(f"{title}\n{body}")
    return "\n\n".join(sections)


def _qa_pair_from_markdown_payload(payload: object) -> QaPair | None:
    if not isinstance(payload, Mapping):
        return None
    question = str(payload.get("question") or "").strip()
    sql = str(payload.get("sql") or "").strip()
    note_ja = str(payload.get("note_ja") or "").strip()
    if not question or not sql:
        return None
    if sql.split(None, 1)[0].upper() not in {"SELECT", "WITH"}:
        return None
    return QaPair(question=question, sql=sql, note_ja=note_ja)


def _qa_sql_example_markdown_lines(qa_pairs: Sequence[QaPair]) -> list[str]:
    deduped: list[QaPair] = []
    seen: set[tuple[str, str]] = set()
    for pair in qa_pairs:
        question = pair.question.strip()
        sql = pair.sql.strip()
        if not question or not sql:
            continue
        key = (question, sql)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(QaPair(question=question, sql=sql, note_ja=pair.note_ja.strip()))
    if not deduped:
        return ["- なし"]
    lines = ["```jsonl"]
    for pair in deduped:
        lines.append(
            json.dumps(
                pair.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    lines.append("```")
    return lines


def qa_sql_examples_from_markdown(markdown: str) -> list[QaPair]:
    """Published Markdown に保存された Q/A SQL 例を、指示ではなくデータとして復元する。"""

    examples: list[QaPair] = []
    seen: set[tuple[str, str]] = set()
    for payload in _markdown_json_payloads(markdown, _QA_SQL_EXAMPLE_BLOCK_RE):
        pair = _qa_pair_from_markdown_payload(payload)
        if pair is None:
            continue
        key = (pair.question, pair.sql)
        if key in seen:
            continue
        seen.add(key)
        examples.append(pair)
    return examples


def _normalize_question_for_example_match(value: str) -> str:
    return "".join(character.lower() for character in value if not character.isspace())


def _question_ngrams(value: str) -> set[str]:
    normalized = _normalize_question_for_example_match(value)
    if not normalized:
        return set()
    if len(normalized) == 1:
        return {normalized}
    return {normalized[index : index + 2] for index in range(len(normalized) - 1)}


def _question_example_score(question: str, example_question: str) -> float:
    left = _normalize_question_for_example_match(question)
    right = _normalize_question_for_example_match(example_question)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return 0.92
    left_terms = _question_ngrams(left)
    right_terms = _question_ngrams(right)
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / len(left_terms | right_terms)


def select_qa_sql_examples_from_markdown(
    markdown: str,
    question: str,
    *,
    limit: int = _QA_SQL_EXAMPLE_MAX_PROMPT_COUNT,
    min_score: float = 0.2,
) -> list[QaPair]:
    """現在の質問に近い Q/A SQL 例だけを SQL 生成 context 用に選ぶ。"""

    ranked: list[tuple[float, int, QaPair]] = []
    for index, pair in enumerate(qa_sql_examples_from_markdown(markdown)):
        score = _question_example_score(question, pair.question)
        if score >= min_score:
            ranked.append((score, index, pair))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [pair for _score, _index, pair in ranked[: max(0, limit)]]


def _unique_values(values: Sequence[str], *, limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = " ".join(str(value or "").split())
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
        if len(result) >= limit:
            break
    return result


def _qa_sql_pattern_id(pattern: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in pattern.items() if key != "pattern_id"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return f"qa_pattern_{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:12]}"


def _format_pattern_values(values: object, *, limit: int = 5) -> str:
    if not isinstance(values, Sequence) or isinstance(values, str):
        return ""
    cleaned = _unique_values([str(value) for value in values], limit=limit)
    return ", ".join(cleaned)


def _qa_sql_generation_rules_from_pattern(pattern: Mapping[str, Any]) -> list[str]:
    rules: list[str] = []
    tables = _format_pattern_values(pattern.get("physical_tables"))
    if tables:
        rules.append(f"使用表候補は {tables} を優先して確認する。")
    joins = _format_pattern_values(pattern.get("join_conditions"), limit=8)
    if joins:
        rules.append(f"JOIN 条件候補は {joins} を再利用候補として確認する。")
    filters = _format_pattern_values(pattern.get("filters"), limit=8)
    if filters:
        rules.append(f"WHERE/HAVING 条件候補は {filters} を業務制約として確認する。")
    aggregates = _format_pattern_values(pattern.get("aggregates"))
    if aggregates:
        rules.append(f"集計式候補は {aggregates} を同じ業務意図の算出式として確認する。")
    group_by = _format_pattern_values(pattern.get("group_by"))
    if group_by:
        rules.append(f"集計粒度候補は {group_by} を優先して確認する。")
    order_by = _format_pattern_values(pattern.get("order_by"))
    if order_by:
        rules.append(f"並び順候補は {order_by} を必要に応じて再利用する。")
    ctes = _format_pattern_values(pattern.get("cte_names"))
    if ctes:
        rules.append(f"CTE 構造を使う場合は {ctes} を中間集合の役割候補として扱う。")
    set_operations = _format_pattern_values(pattern.get("set_operations"))
    if set_operations:
        rules.append(f"集合演算候補は {set_operations} を同種の比較・統合要件で確認する。")
    if not rules and pattern.get("parse_complete") is False:
        rules.append("Q/A 由来 SQL の構造解析が不完全なため、公開済みオントロジーを優先する。")
    return rules


def _qa_order_expression(expression: str, direction: str) -> str:
    """ORDER BY の向きと NULLS 指定を AST から一度だけ組み立てる。"""
    parsed = parse_one(f"SELECT 1 ORDER BY {expression}", read="oracle")
    ordered = parsed.args["order"].expressions[0]
    text = f"{ordered.this.sql(dialect='oracle')} {direction}"
    if "nulls_first" in ordered.args:
        # Oracle の既定と違う NULLS 指定だけ残す。
        first = ordered.args["nulls_first"]
        if first != (direction == "desc"):
            text += " NULLS FIRST" if first else " NULLS LAST"
    return text


def _qa_sql_pattern_from_pair(pair: QaPair) -> dict[str, Any]:
    try:
        analysis = parse_oracle_sql(
            pair.sql,
            intent_version=1,
            ontology_revision_id="qa_sql_pattern",
        )
    except Exception as exc:
        pattern = {
            "source_role": "qa_sql_structure",
            "parse_complete": False,
            "warnings_ja": [f"SQL 構造を解析できませんでした: {type(exc).__name__}"],
        }
        pattern["generation_rules_ja"] = _qa_sql_generation_rules_from_pattern(pattern)
        pattern["pattern_id"] = _qa_sql_pattern_id(pattern)
        return pattern
    graph = analysis.graph
    if graph is None:
        pattern = {
            "source_role": "qa_sql_structure",
            "parse_complete": False,
            "warnings_ja": [finding.message_ja for finding in analysis.validation.findings[:5]],
        }
        pattern["generation_rules_ja"] = _qa_sql_generation_rules_from_pattern(pattern)
        pattern["pattern_id"] = _qa_sql_pattern_id(pattern)
        return pattern
    pattern = {
        "source_role": "qa_sql_structure",
        "parse_complete": True,
        "statement_type": graph.statement_type,
        "physical_tables": _unique_values(
            [
                table.qualified_name or table.name
                for table in graph.tables
                if not table.is_cte and (table.qualified_name or table.name)
            ],
            limit=20,
        ),
        "cte_names": _unique_values([cte.name for cte in graph.ctes], limit=20),
        "set_operations": _unique_values(
            [operation.operator for operation in graph.set_operations],
            limit=20,
        ),
        "join_conditions": _unique_values(
            [join.condition_sql for join in graph.joins if join.condition_sql],
            limit=40,
        ),
        "filters": _unique_values(
            [predicate.expression_sql for predicate in [*graph.filters, *graph.having]],
            limit=40,
        ),
        "projections": _unique_values(
            [projection.expression_sql for projection in graph.projections],
            limit=40,
        ),
        "aggregates": _unique_values(
            [aggregate.expression_sql for aggregate in graph.aggregates],
            limit=20,
        ),
        "group_by": _unique_values([group.expression_sql for group in graph.groups], limit=20),
        "order_by": _unique_values(
            [_qa_order_expression(order.expression_sql, order.direction) for order in graph.orders],
            limit=20,
        ),
    }
    pattern["generation_rules_ja"] = _qa_sql_generation_rules_from_pattern(pattern)
    pattern["pattern_id"] = _qa_sql_pattern_id(pattern)
    return pattern


def qa_sql_patterns_from_pairs(
    pairs: Sequence[QaPair],
    *,
    limit: int = _QA_SQL_PATTERN_MAX_PROMPT_COUNT,
) -> list[dict[str, Any]]:
    max_patterns = max(0, limit)
    if max_patterns == 0:
        return []
    patterns: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pair in pairs:
        pattern = _qa_sql_pattern_from_pair(pair)
        pattern_id = str(pattern.get("pattern_id") or "")
        if pattern_id and pattern_id in seen:
            continue
        if pattern_id:
            seen.add(pattern_id)
        patterns.append(pattern)
        if len(patterns) >= max_patterns:
            break
    return patterns


def _qa_sql_pattern_from_markdown_payload(payload: object) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    pattern: dict[str, Any] = {}
    for key in (
        "pattern_id",
        "source_role",
        "parse_complete",
        "statement_type",
    ):
        if key in payload:
            pattern[key] = payload[key]
    for key in (
        "physical_tables",
        "cte_names",
        "set_operations",
        "join_conditions",
        "filters",
        "projections",
        "aggregates",
        "group_by",
        "order_by",
        "generation_rules_ja",
        "warnings_ja",
    ):
        values = payload.get(key)
        if not isinstance(values, Sequence) or isinstance(values, str):
            continue
        cleaned = _unique_values([str(value) for value in values], limit=40)
        if cleaned:
            pattern[key] = cleaned
    useful_keys = set(pattern) - {"pattern_id", "source_role", "parse_complete", "statement_type"}
    if not useful_keys:
        return None
    pattern.pop("question", None)
    if "generation_rules_ja" not in pattern:
        pattern["generation_rules_ja"] = _qa_sql_generation_rules_from_pattern(pattern)
    if "pattern_id" not in pattern:
        pattern["pattern_id"] = _qa_sql_pattern_id(pattern)
    return pattern


def qa_sql_patterns_from_markdown(
    markdown: str,
    *,
    limit: int = _QA_SQL_PATTERN_MAX_PROMPT_COUNT,
) -> list[dict[str, Any]]:
    max_patterns = max(0, limit)
    if max_patterns == 0:
        return []
    patterns: list[dict[str, Any]] = []
    seen: set[str] = set()
    for payload in _markdown_json_payloads(markdown, _QA_SQL_PATTERN_BLOCK_RE):
        pattern = _qa_sql_pattern_from_markdown_payload(payload)
        if pattern is None:
            continue
        pattern_id = str(pattern.get("pattern_id") or "")
        if pattern_id and pattern_id in seen:
            continue
        if pattern_id:
            seen.add(pattern_id)
        patterns.append(pattern)
        if len(patterns) >= max_patterns:
            break
    return patterns


def _qa_sql_rule_markdown_lines(qa_pairs: Sequence[QaPair]) -> list[str]:
    rules: list[str] = []
    for pattern in qa_sql_patterns_from_pairs(qa_pairs, limit=20):
        rules.extend(_qa_sql_generation_rules_from_pattern(pattern))
    unique_rules = _unique_values(rules, limit=40)
    if not unique_rules:
        return ["- なし"]
    return [f"- {rule}" for rule in unique_rules]


def _qa_sql_pattern_markdown_lines(qa_pairs: Sequence[QaPair]) -> list[str]:
    patterns = qa_sql_patterns_from_pairs(qa_pairs, limit=20)
    if not patterns:
        return ["- なし"]
    lines = ["```jsonl"]
    for pattern in patterns:
        lines.append(json.dumps(pattern, ensure_ascii=False, separators=(",", ":")))
    lines.append("```")
    return lines


def _draft_nodes(draft: ProposalDraft) -> list[OntologyNode]:
    nodes: list[OntologyNode] = []
    for value in draft.payload.values.get("node_upserts") or []:
        try:
            nodes.append(OntologyNode.model_validate(value))
        except Exception:  # nosec B112
            continue
    return nodes


def _draft_edges(draft: ProposalDraft) -> list[OntologyEdge]:
    edges: list[OntologyEdge] = []
    for value in draft.payload.values.get("edge_upserts") or []:
        try:
            edges.append(OntologyEdge.model_validate(value))
        except Exception:  # nosec B112
            continue
    return edges


def _physical_mapping_label(node: OntologyNode) -> str:
    if not node.physical_mappings:
        return node.technical_name or node.id
    mapping = node.physical_mappings[0]
    if mapping.column_refs:
        return _column_ref_label(mapping.column_refs[0])
    ref = mapping.object_ref
    return f"{ref.owner}.{ref.object_name}" if ref.owner else ref.object_name


def _profile_node_override_map(view: ProfileOntologyView | None) -> dict[str, dict[str, Any]]:
    if view is None:
        return {}
    return {
        str(item.get("node_id") or ""): dict(item)
        for item in view.draft_node_overrides
        if str(item.get("node_id") or "")
    }


def _profile_edge_override_map(view: ProfileOntologyView | None) -> dict[str, dict[str, Any]]:
    if view is None:
        return {}
    return {
        str(item.get("edge_id") or ""): dict(item)
        for item in view.draft_edge_overrides
        if str(item.get("edge_id") or "")
    }


def _effective_business_name(node: OntologyNode, view: ProfileOntologyView | None) -> str:
    override = _profile_node_override_map(view).get(node.id, {})
    return _md_text(override.get("business_name_ja") or node.business_name_ja, node.id)


def _effective_table_usage(node: OntologyNode, view: ProfileOntologyView | None) -> str:
    if view is None:
        return ""
    override = _profile_node_override_map(view).get(node.id, {})
    usage = str(override.get("table_usage") or view.table_usages_ja.get(node.id, "") or "")
    return usage.strip()


def _effective_edge_cardinality(edge: OntologyEdge, view: ProfileOntologyView | None) -> str:
    override = _profile_edge_override_map(view).get(edge.id, {})
    cardinality = override.get("cardinality") or edge.cardinality.value
    return str(cardinality or "unknown")


def _effective_edge_allowed(edge: OntologyEdge, view: ProfileOntologyView | None) -> bool:
    # OntologyEdge に enabled 属性は存在しない(extra=forbid のため参照すると
    # AttributeError)。view 無し = 共有 Ontology 全体の export なので常に許可、
    # view 有りは override > allowed_path_ids の順で判定する。
    if view is None:
        return True
    override = _profile_edge_override_map(view).get(edge.id, {})
    if "allowed_path" in override:
        return bool(override.get("allowed_path"))
    return edge.id in set(view.allowed_path_ids)


def _node_markdown_lines(node: OntologyNode, view: ProfileOntologyView | None) -> list[str]:
    aliases = ", ".join(sorted(set(node.aliases))) or "なし"
    usage = _effective_table_usage(node, view)
    lines = [
        f"- {_effective_business_name(node, view)} ({_md_code(_physical_mapping_label(node))})",
        f"  - 種別: {node.kind.value}",
        f"  - 説明: {_md_text(node.description_ja)}",
        f"  - 別名: {aliases}",
    ]
    if usage:
        lines.append(f"  - 用途: {_md_text(usage)}")
    if node.provenance.evidence:
        lines.append("  - 証拠:")
        for evidence in node.provenance.evidence[:5]:
            # OntologyEvidence の実フィールドは excerpt_ja / locator / source_document_id。
            # (旧 label/location/source_id は存在せず AttributeError になっていた)
            label = str(
                evidence.excerpt_ja or evidence.locator or evidence.source_document_id or ""
            ).strip()
            if label:
                lines.append(f"    - {_md_text(label)}")
    return lines


def _edge_markdown_lines(
    edge: OntologyEdge,
    *,
    node_by_id: dict[str, OntologyNode],
    view: ProfileOntologyView | None,
) -> list[str]:
    source = node_by_id.get(edge.source_node_id)
    target = node_by_id.get(edge.target_node_id)
    source_label = (
        f"{_effective_business_name(source, view)} ({_md_code(_physical_mapping_label(source))})"
        if source is not None
        else _md_code(edge.source_node_id)
    )
    target_label = (
        f"{_effective_business_name(target, view)} ({_md_code(_physical_mapping_label(target))})"
        if target is not None
        else _md_code(edge.target_node_id)
    )
    lines = [
        f"- {source_label} → {target_label}: {_md_text(edge.relationship_name_ja)}",
        f"  - 種別: {edge.kind.value}",
        f"  - 多重度: {_effective_edge_cardinality(edge, view)}",
        f"  - 検索利用: {'利用可' if _effective_edge_allowed(edge, view) else '利用不可'}",
        f"  - 証拠: {_md_text(edge.description_ja)}",
    ]
    if edge.join_conditions:
        lines.append("  - Join 条件:")
        lines.extend(
            f"    - {_md_code(_join_condition_label(condition))}"
            for condition in sorted(edge.join_conditions, key=lambda item: item.ordinal)
        )
    return lines


def _physical_object_lines(
    *,
    schema_objects: list[Any],
    ontology: SchemaOntology | None,
    view: ProfileOntologyView | None,
) -> list[str]:
    if ontology is not None and view is not None and view.physical_objects:
        node_by_id = {node.id: node for node in ontology.nodes}
        lines: list[str] = []
        for ref in sorted(
            view.physical_objects,
            key=lambda item: (item.owner, item.object_name, item.object_type),
        ):
            node = node_by_id.get(ref.node_id)
            business_name = _effective_business_name(node, view) if node else ref.object_name
            description = _md_text(node.description_ja if node else "")
            usage = _effective_table_usage(node, view) if node else ""
            lines.extend(
                [
                    f"- {_md_code(f'{ref.owner}.{ref.object_name}')} ({ref.object_type})",
                    f"  - 業務名: {_md_text(business_name)}",
                    f"  - 説明: {description}",
                    f"  - 用途: {_md_text(usage, '未設定')}",
                ]
            )
        return lines

    lines = []
    for item in schema_objects:
        if not isinstance(item, dict):
            continue
        object_name = str(item.get("object") or item.get("object_name") or "")
        object_type = str(item.get("object_type") or "")
        logical_name = str(item.get("logical_name") or "")
        comment = str(item.get("comment") or "")
        raw_columns = item.get("columns")
        columns: list[Any] = raw_columns if isinstance(raw_columns, list) else []
        lines.extend(
            [
                f"- {_md_code(object_name)} ({_md_text(object_type)})",
                f"  - 業務名: {_md_text(logical_name)}",
                f"  - 説明: {_md_text(comment)}",
                f"  - 列数: {len(columns)}",
            ]
        )
    return lines


def _profile_entity_lines(
    ontology: SchemaOntology | None,
    view: ProfileOntologyView | None,
) -> list[str]:
    if ontology is None or view is None:
        return []
    scoped = set(view.node_ids)
    return [
        line
        for node in sorted(ontology.nodes, key=lambda item: (item.kind.value, item.id))
        if node.id in scoped
        and node.kind
        in {
            OntologyNodeKind.TABLE,
            OntologyNodeKind.VIEW,
            OntologyNodeKind.BUSINESS_ENTITY,
            OntologyNodeKind.BUSINESS_TERM,
        }
        for line in _node_markdown_lines(node, view)
    ]


def _profile_relationship_lines(
    ontology: SchemaOntology | None,
    view: ProfileOntologyView | None,
) -> list[str]:
    if ontology is None or view is None:
        return []
    scoped = set(view.edge_ids)
    node_by_id = {node.id: node for node in ontology.nodes}
    return [
        line
        for edge in sorted(ontology.edges, key=lambda item: (item.kind.value, item.id))
        if edge.id in scoped
        and edge.kind
        in {
            OntologyEdgeKind.FOREIGN_KEY,
            OntologyEdgeKind.BUSINESS_RELATIONSHIP,
            OntologyEdgeKind.JOINS,
        }
        for line in _edge_markdown_lines(edge, node_by_id=node_by_id, view=view)
    ]


def _profile_metric_lines(
    ontology: SchemaOntology | None,
    view: ProfileOntologyView | None,
) -> list[str]:
    if ontology is None or view is None:
        return []
    scoped = set(view.node_ids)
    lines: list[str] = []
    for node in sorted(ontology.nodes, key=lambda item: (item.kind.value, item.id)):
        if node.id not in scoped:
            continue
        if node.kind == OntologyNodeKind.METRIC:
            lines.extend(_node_markdown_lines(node, view))
            definition = node.metadata.get("metric_definition")
            if isinstance(definition, dict):
                lines.append(f"  - 定義 SQL: {_md_code(definition.get('expression_sql'))}")
                lines.append(f"  - 集計: {_md_text(definition.get('aggregation'))}")
                lines.append(f"  - 単位: {_md_text(definition.get('unit'), 'なし')}")
    return lines


def _profile_rule_enum_lines(
    ontology: SchemaOntology | None,
    view: ProfileOntologyView | None,
) -> list[str]:
    if ontology is None or view is None:
        return []
    scoped = set(view.node_ids)
    node_by_id = {node.id: node for node in ontology.nodes}
    lines: list[str] = []
    for node in sorted(ontology.nodes, key=lambda item: (item.kind.value, item.id)):
        if node.id not in scoped:
            continue
        if node.business_rule_definition is not None:
            rule = node.business_rule_definition
            applies_to = ", ".join(
                _effective_business_name(target, view)
                for target_id in rule.applies_to_node_ids
                if (target := node_by_id.get(target_id)) is not None
            )
            lines.extend(
                [
                    f"- {_effective_business_name(node, view)}",
                    "  - 種別: 業務ルール",
                    f"  - ルール文: {_md_text(rule.statement_ja)}",
                    f"  - 適用対象: {_md_text(applies_to, '未設定')}",
                    f"  - 重大度: {rule.severity.value}",
                    f"  - 実行方式: {rule.execution_mode.value}",
                ]
            )
        elif node.enum_value_definition is not None:
            enum_value = node.enum_value_definition
            property_node = node_by_id.get(enum_value.property_node_id)
            property_label = (
                _effective_business_name(property_node, view)
                if property_node is not None
                else enum_value.property_node_id
            )
            lines.extend(
                [
                    f"- {_effective_business_name(node, view)}",
                    "  - 種別: 列挙値",
                    f"  - コード: {_md_text(enum_value.code)}",
                    f"  - 物理値: {_md_code(enum_value.physical_literal)}",
                    f"  - 表示名: {_md_text(enum_value.label_ja)}",
                    f"  - 属性: {_md_text(property_label)}",
                    f"  - 別名: {', '.join(enum_value.aliases) or 'なし'}",
                ]
            )
    return lines


def render_ontology_build_markdown(
    *,
    profile_id: str,
    schema_context: str,
    drafts: list[ProposalDraft],
    warnings: list[str],
    proposal_rejections: list[str] | None = None,
    source_count: int,
    qa_pair_count: int,
    business_text_present: bool,
    qa_pairs: list[QaPair] | None = None,
    ontology: SchemaOntology | None = None,
    profile_view: ProfileOntologyView | None = None,
) -> str:
    """AI 構築結果から、確認・編集用 Markdown 下書きを決定論生成する。"""

    try:
        schema = json.loads(schema_context)
    except Exception:
        schema = {}
    objects = schema.get("objects") if isinstance(schema, dict) else []
    relationships = schema.get("relationships") if isinstance(schema, dict) else []
    object_count = len(objects) if isinstance(objects, list) else 0
    column_count = (
        sum(len(item.get("columns") or []) for item in objects if isinstance(item, dict))
        if isinstance(objects, list)
        else 0
    )
    relationship_count = len(relationships) if isinstance(relationships, list) else 0
    physical_lines = _physical_object_lines(
        schema_objects=objects if isinstance(objects, list) else [],
        ontology=ontology,
        view=profile_view,
    )
    profile_entity_lines = _profile_entity_lines(ontology, profile_view)
    profile_relationship_lines = _profile_relationship_lines(ontology, profile_view)
    profile_metric_lines = _profile_metric_lines(ontology, profile_view)
    profile_rule_enum_lines = _profile_rule_enum_lines(ontology, profile_view)

    lines = [
        "# オントロジー下書き",
        "",
        "## 入力サマリー",
        f"- プロファイル: {_md_code(profile_id)}",
        f"- 業務説明: {'あり' if business_text_present else 'なし'}",
        f"- Q/A 件数: {qa_pair_count}",
        f"- 構築資料: {source_count}",
        f"- DB スキーマオブジェクト: {object_count}",
        f"- DB スキーマ列: {column_count}",
        f"- 既存スキーマ関係: {relationship_count}",
        "",
        _QA_SQL_RULE_SECTION_TITLE,
    ]
    lines.extend(_qa_sql_rule_markdown_lines(qa_pairs or []))
    lines.extend(["", _QA_SQL_PATTERN_SECTION_TITLE])
    lines.extend(_qa_sql_pattern_markdown_lines(qa_pairs or []))
    lines.extend(["", "## 物理オブジェクト"])
    lines.extend(physical_lines or ["- なし"])
    lines.extend(
        [
            "",
            "## 業務エンティティ",
        ]
    )

    entity_lines: list[str] = []
    relationship_lines: list[str] = []
    metric_lines: list[str] = []
    synonym_lines: list[str] = []

    for draft in drafts:
        nodes = _draft_nodes(draft)
        edges = _draft_edges(draft)
        if draft.kind == OntologyProposalKind.MAPPING:
            for node in nodes:
                if node.kind != OntologyNodeKind.BUSINESS_ENTITY:
                    continue
                aliases = ", ".join(sorted(set(node.aliases))) or "なし"
                entity_lines.extend(
                    [
                        f"- {node.business_name_ja} ({_md_code(_physical_mapping_label(node))})",
                        f"  - 説明: {_md_text(node.description_ja)}",
                        f"  - 別名: {aliases}",
                        f"  - 信頼度: {node.confidence:.2f}",
                    ]
                )
                if node.aliases:
                    synonym_lines.extend(
                        [
                            f"- 対象: {_md_code(_physical_mapping_label(node))}",
                            f"  - 別名: {aliases}",
                            f"  - 証拠: {_md_text(node.description_ja)}",
                        ]
                    )
        elif draft.kind == OntologyProposalKind.RELATIONSHIP:
            node_by_id = {node.id: node for node in nodes}
            for edge in edges:
                if edge.kind != OntologyEdgeKind.BUSINESS_RELATIONSHIP:
                    continue
                source = node_by_id.get(edge.source_node_id)
                target = node_by_id.get(edge.target_node_id)
                source_label = (
                    f"{source.business_name_ja} ({_md_code(_physical_mapping_label(source))})"
                    if source is not None
                    else _md_code(edge.source_node_id)
                )
                target_label = (
                    f"{target.business_name_ja} ({_md_code(_physical_mapping_label(target))})"
                    if target is not None
                    else _md_code(edge.target_node_id)
                )
                relationship_lines.extend(
                    [
                        f"- {source_label} → {target_label}: {edge.relationship_name_ja}",
                        f"  - 多重度: {edge.cardinality.value}",
                        f"  - 証拠: {_md_text(edge.description_ja)}",
                        f"  - 信頼度: {edge.confidence:.2f}",
                    ]
                )
                if edge.join_conditions:
                    relationship_lines.append("  - Join 条件:")
                    relationship_lines.extend(
                        f"    - {_md_code(_join_condition_label(condition))}"
                        for condition in sorted(edge.join_conditions, key=lambda item: item.ordinal)
                    )
        elif draft.kind == OntologyProposalKind.METRIC_DEFINITION:
            for node in nodes:
                if node.kind != OntologyNodeKind.METRIC:
                    continue
                metric_definition = node.metadata.get("metric_definition")
                expression = ""
                aggregation = ""
                unit = ""
                if isinstance(metric_definition, dict):
                    expression = str(metric_definition.get("expression_sql") or "")
                    aggregation = str(metric_definition.get("aggregation") or "")
                    unit = str(metric_definition.get("unit") or "")
                base_columns = ", ".join(
                    _md_code(_column_ref_label(column_ref))
                    for mapping in node.physical_mappings
                    for column_ref in mapping.column_refs
                )
                metric_lines.extend(
                    [
                        f"- {node.business_name_ja}",
                        f"  - 定義 SQL: {_md_code(expression)}",
                        f"  - 元列: {base_columns or 'なし'}",
                        f"  - 集計: {_md_text(aggregation)}",
                        f"  - 単位: {_md_text(unit, 'なし')}",
                        f"  - 説明: {_md_text(node.description_ja)}",
                        f"  - 信頼度: {node.confidence:.2f}",
                    ]
                )
        elif draft.kind == OntologyProposalKind.ALIAS:
            for node in nodes:
                aliases = ", ".join(sorted(set(node.aliases))) or "なし"
                synonym_lines.extend(
                    [
                        f"- 対象: {_md_code(_physical_mapping_label(node))}",
                        f"  - 別名: {aliases}",
                        f"  - 証拠: {_md_text(node.description_ja)}",
                    ]
                )

    merged_entity_lines = [*profile_entity_lines, *entity_lines]
    merged_relationship_lines = [*profile_relationship_lines, *relationship_lines]
    lines.extend(merged_entity_lines or ["- なし"])
    lines.extend(["", "## 関係 / Join"])
    lines.extend(merged_relationship_lines or ["- なし"])
    lines.extend(["", "## 指標"])
    lines.extend([*profile_metric_lines, *metric_lines] or ["- なし"])
    lines.extend(["", "## 業務ルール / 列挙値"])
    lines.extend(profile_rule_enum_lines or ["- なし"])
    lines.extend(["", "## 同義語"])
    lines.extend(synonym_lines or ["- なし"])
    lines.extend(["", "## 証拠 / 確認事項"])
    unique_warnings = _unique_non_empty_messages(warnings)
    lines.extend(f"- 警告: {warning}" for warning in unique_warnings)
    if not unique_warnings:
        lines.append("- なし")
    unique_proposal_rejections = _unique_non_empty_messages(proposal_rejections or [])
    if unique_proposal_rejections:
        lines.extend(["", "## 採用外候補"])
        lines.extend(f"- {message}" for message in unique_proposal_rejections)
    return "\n".join(lines).rstrip() + "\n"


# --- LLM 呼び出し -----------------------------------------------------------------------------

_EXTRACTION_SYSTEM_PROMPT = (
    "あなたは NL2SQL 用オントロジーの構築支援器です。"
    "応答 JSON Schema に従った JSON だけを返してください。"
    "新規生成は definitions を唯一の正本とし、entities/relationships/metrics/synonyms の旧形式へ"
    "同じ候補を二重出力しないでください。"
    "各入力バッチから主要6種類（Object Type/Property/Link Type/"
    "Interface/Function/Action Type）と"
    "補助7種類（Shared Property/Value Type/Enumeration/Metric/"
    "Business Rule/Business Event/Object Set）を"
    "まとめて抽出してください。根拠のない種類は創作せず coverage に未生成の理由を記録してください。"
    "同一概念の重複定義を作らず、同じ種類と api_name または"
    "既存 ID が一致する概念を補完してください。"
    "既存定義にない別の概念は資料・スキーマに根拠があれば追加してください。"
    "api_name は安定した英語識別子、参照は definitions の api_name とします。"
    "schema_context に存在しない owner/object/column を参照しないでください。"
    '引用された物理名は引用符と大文字小文字を保持してください（例: SALES."Mixed_Case"."Amount"）。'
    "Q/A の schema_resolved_columns/schema_resolved_join_conditions を正とし、"
    "SQL の alias を物理名にしない。"
    "名詞をオブジェクト、動詞・述語を関係の手がかりとし、物理表と業務オブジェクトを一対一と決めつけない。"
    "主識別子 primary_key/properties は所属する Property の api_name を参照し、"
    "grain_ja は業務上の粒度、mappings は根拠のある物理表・列を設定する。"
    "JSON Schema の required はキーの出力要件であり、すべての値が業務上必須という意味ではない。"
    "title_property は任意で、根拠のある同じオブジェクトの表示用 Property がなければ空文字にする。"
    "title_property の未設定を warnings_ja や missing_information_ja に記録しない。"
    "必須項目の不足は対象定義の missing_information_ja に具体的なフィールド名と理由を記録し、"
    "キーが required という理由だけで不足を推測しない。必須判定の正本は型と後段の機械的検証である。"
    "資料にない任意の概念・契約を生成しなかったことや既存 ID の再利用などの"
    "正常な処理方針を warnings_ja に重複記録しない。"
    "warnings_ja は根拠の矛盾や抽出失敗など利用者の確認・修正が必要な問題に限定する。"
    "業務説明が空、検証メッセージのみ、または根拠資料が不足していること自体は警告にしない。"
    "記述できる定義を schema_context や資料から生成し、不明な情報は創作せず"
    "missing_information_ja または coverage の理由に記録する。検証メッセージを業務定義にしない。"
    "関係の cardinality は one_to_one / one_to_many / many_to_one / many_to_many を"
    "主キー・一意制約と資料から判断し、不明なら unknown と理由を記録する。"
    "指標は expression_sql/filter_sql/aggregation/grain/distinct_keys/"
    "time_property/time_policy_ja/"
    "unit/currency/null_policy_ja/additivity を資料に合わせ、"
    "実装キー・権限・アルゴリズムは創作しない。"
    "インターフェースは必須プロパティ・関係・アクションと実装オブジェクトの対応を定義する。"
    "業務名・説明・同義語は日本語とし、現場の別名・略称を aliases に記録する。"
    "evidence は入力の source_id と locator、原文の引用を記録し、assertion に対象フィールドを書く。"
    "DB は source_id=schema、locator=OWNER.OBJECT または OWNER.OBJECT.COLUMN とする。"
    # graph を作れる範囲を最大にし、足りない項目は推測で埋めない（#1720）。
    "best effort で、schema_context から作れるオブジェクト・プロパティ・関係はすべて作る。"
    "根拠の無い種類は空の定義を作らず、coverage に理由を書く。"
    "新しい定義の id は空文字にし、既存定義を補完するときだけ既存の id を使う。"
    "情報が足りない任意の項目（識別子・粒度・単位・基数など）は推測で埋めず空のままにする。"
    "空の項目を missing_information_ja に書き写さない（「説明なし」「主キー不明」など）。"
    "missing_information_ja は、資料どうしの矛盾など利用者が判断すべき点だけにする。"
    "プロパティは 1 つのオブジェクトに属し、object_type はそのオブジェクトの api_name、"
    "mappings はそのオブジェクトの表・ビューの列にする。"
    "同じ列名が複数の表・ビューにあるとき（例: 2 つのビューの DEPARTMENT_NAME）は、"
    "オブジェクトごとに別の api_name のプロパティを作る"
    "（例: v_emp_dept_department_name）。他のオブジェクトのプロパティを properties に入れない。"
    "オブジェクトの mappings の column_name に * を書かない。表・ビュー全体の対応は"
    "column_name を空文字にする。"
    "主キー・一意制約が無い表・ビュー（ビューは制約を持たない）の primary_key は、"
    "資料で確かめられなければ空にする。"
    "外部キー制約が無くても、同じ名前・型の ID 列（例: EMPLOYEE.DEPARTMENT_ID と"
    " DEPARTMENT.DEPARTMENT_ID）があれば、その列で結合する関係を link_type として作り、"
    "join_expression_sql に OWNER.OBJECT.COLUMN = OWNER.OBJECT.COLUMN の形で書く。"
    # 一意でない列での結合は多対多になりうる（#1741）。
    "join_expression_sql は外部キー・主キー・一意制約の列で結合し、"
    "DEPARTMENT_NAME など一意でない列で結合しない。"
    # 業界の標準のモデルに沿う（#1729。profile-ontology-capabilities.md「オントロジーの標準」）。
    "オントロジーは Palantir Foundry の Ontology のモデルに沿う。"
    "オブジェクト型（object_type）は表示名 name_ja・api_name（snake_case）・説明 description_ja・"
    "主キー primary_key・タイトルのプロパティ title_property・対応する表 mappings を持つ。"
    "プロパティ（property）は業務の型 data_type（string / integer / number / boolean / date / "
    "datetime のいずれか）と、精度・桁を含む物理の型 physical_type（例: NUMBER(12,2)・"
    "VARCHAR2(50)）・必須 required・単位 unit・説明・所属 object_type を持つ。"
    "複数のオブジェクト型で同じ意味のものは共有プロパティ（shared_property）にする。"
    "リンク型（link_type）は両端 source / target・多重度 cardinality・結合のキー "
    "join_expression_sql・それぞれの向きの名前 name_ja / inverse_name_ja を持つ。"
    "用語は SKOS の考え方で、name_ja を優先ラベル、aliases を別ラベル、"
    "description_ja を定義とする。"
    "指標（metric）は semantic layer の書き方で、expression_sql を測定値、aggregation を集計方法、"
    "grain をディメンション、time_property を時間のディメンション、"
    "filter_sql を絞り込みの条件とする。"
    "api_name に種類の名前（object_type・property・link_type など）を使わない。"
    "name_ja・説明・別名は日本語で書き、表・列・api_name・SQL は英語のまま書く。"
)


def _proposal_payload_key(kind_value: str, values: dict[str, Any]) -> str:
    """提案の同一性判定キー(kind + 安定 node/edge ID)。実行を跨いだ dedup に使う。"""

    return json.dumps(
        {
            "kind": kind_value,
            "nodes": sorted(str(node["id"]) for node in values.get("node_upserts") or []),
            "edges": sorted(str(edge["id"]) for edge in values.get("edge_upserts") or []),
        },
        sort_keys=True,
    )


def merge_build_extractions(
    base: OntologyBuildExtraction,
    addition: OntologyBuildExtraction,
) -> tuple[OntologyBuildExtraction, int]:
    """gleaning パスの追加抽出を key ベースで重複排除しつつ結合する。

    戻り値は (結合結果, 新規に追加された候補数)。
    """

    added = 0
    entities = list(base.entities)
    # 参照は SQL と同じ表記の照合キーで重複排除する（`SALES."Mixed_Case"` と
    # `SALES.MIXED_CASE` を同じ候補にしない、#573）。
    entity_keys = {object_match_key(candidate.object_name) for candidate in entities}
    for candidate in addition.entities:
        key = object_match_key(candidate.object_name)
        if key in entity_keys:
            continue
        entity_keys.add(key)
        entities.append(candidate)
        added += 1

    relationships = list(base.relationships)
    relationship_keys = {
        (
            object_match_key(relationship.source_object),
            object_match_key(relationship.target_object),
            relationship.relationship_name_ja.strip(),
        )
        for relationship in relationships
    }
    for relationship in addition.relationships:
        relationship_key = (
            object_match_key(relationship.source_object),
            object_match_key(relationship.target_object),
            relationship.relationship_name_ja.strip(),
        )
        if relationship_key in relationship_keys:
            continue
        relationship_keys.add(relationship_key)
        relationships.append(relationship)
        added += 1

    metrics = list(base.metrics)
    metric_keys = {metric.metric_name_ja.strip() for metric in metrics}
    for metric in addition.metrics:
        metric_key = metric.metric_name_ja.strip()
        if metric_key in metric_keys:
            continue
        metric_keys.add(metric_key)
        metrics.append(metric)
        added += 1

    synonyms = list(base.synonyms)
    synonym_keys = {object_match_key(synonym.target) for synonym in synonyms}
    for synonym in addition.synonyms:
        synonym_key = object_match_key(synonym.target)
        if synonym_key in synonym_keys:
            continue
        synonym_keys.add(synonym_key)
        synonyms.append(synonym)
        added += 1

    # 完全に同じ再出力だけを除去する。差分は最終の意味的統合へ渡し競合を保持する。
    definitions = list(base.definitions)
    definition_values = {canonical_json(d.model_dump(mode="json")) for d in definitions}
    for definition in addition.definitions:
        value = canonical_json(definition.model_dump(mode="json"))
        if value not in definition_values:
            definitions.append(definition)
            definition_values.add(value)
            added += 1
    warnings_ja = [*base.warnings_ja]
    for warning in addition.warnings_ja:
        if warning not in warnings_ja:
            warnings_ja.append(warning)

    return (
        OntologyBuildExtraction(
            definitions=definitions,
            coverage=[*base.coverage, *addition.coverage],
            entities=entities,
            relationships=relationships,
            metrics=metrics,
            synonyms=synonyms,
            warnings_ja=warnings_ja,
        ),
        added,
    )


def parse_extraction(raw: str) -> OntologyBuildExtraction:
    """Structured Outputs の JSON 全文を契約 schema で検証する。"""

    cleaned = str(raw).strip()
    return OntologyBuildExtraction.model_validate(json.loads(cleaned))


class OntologyBuildContextBudgetError(ValueError):
    """LLM 入力を省略せずに分割しても context 予算へ収まらない。"""


@dataclass(frozen=True)
class _BuildTextUnit:
    source_document: OntologySourceDocument | None
    source_label: str
    locator_kind: OntologyEvidenceLocatorKind
    locator: str
    text: str

    def context_payload(self) -> dict[str, str]:
        return {
            "source": self.source_label,
            "source_id": self.source_document.id if self.source_document else "manual",
            "locator_kind": self.locator_kind.value,
            "locator": self.locator,
            "text": self.text,
        }

    def evidence(self) -> OntologyEvidence | None:
        if self.source_document is None:
            return None
        return ExtractedSourceChunk(
            self.text,
            self.locator_kind,
            self.locator,
        ).evidence(self.source_document)


@dataclass(frozen=True)
class _OntologyBuildLlmTask:
    name: OntologyBuildStepName
    prompt: str
    context: str
    progress_ja: str
    cross_check_sql: list[str] | None = None
    source_evidence: list[OntologyEvidence] = field(default_factory=list)
    # 二分割リトライ用の元 batch(QA/TEXT のみ)。schema_naming は分割不可。
    qa_batch: list[QaPair] | None = None
    text_batch: list[_BuildTextUnit] | None = None
    schema_payload: dict[str, Any] | None = None
    definition_phase: str = "concepts"
    # 前の定義の要約と前の確認の直すべき点（`_with_prior_context`）。二分割した子タスクにも
    # 付け直す（#1736）。`prompt` / `context` には付けた後の値が入る。
    prior: dict[str, Any] | None = None

    def with_prior(self, prior: dict[str, Any] | None) -> _OntologyBuildLlmTask:
        """前の定義の要約を付けたタスク（上限を超えるときは要約から削る）。"""

        from .ontology_build_prior import PRIOR_PROMPT

        if not prior:
            return self
        prompt, context = _with_prior_context(
            self.prompt.removesuffix(PRIOR_PROMPT), self.context, prior
        )
        return replace(self, prompt=prompt, context=context, prior=prior)

    def child(self, context: str, **changes: Any) -> _OntologyBuildLlmTask:
        """context を差し替えた子タスク。前の定義の要約は付け直す（#1736）。"""

        from .ontology_build_prior import PRIOR_PROMPT

        base = replace(
            self, prompt=self.prompt.removesuffix(PRIOR_PROMPT), context=context, **changes
        )
        return base.with_prior(self.prior)

    def split(self) -> list[_OntologyBuildLlmTask] | None:
        """batch を二分割した子タスクを返す(分割不能・要素 1 件以下は None)。"""

        if self.schema_payload is None:
            return None
        if self.qa_batch is not None and len(self.qa_batch) > 1:
            middle = len(self.qa_batch) // 2
            qa_halves = [self.qa_batch[:middle], self.qa_batch[middle:]]
            return [
                self.child(
                    _dump_qa_context(self.schema_payload, half),
                    cross_check_sql=[pair.sql for pair in half],
                    qa_batch=half,
                )
                for half in qa_halves
            ]
        if self.text_batch is not None and len(self.text_batch) > 1:
            middle = len(self.text_batch) // 2
            text_halves = [self.text_batch[:middle], self.text_batch[middle:]]
            return [
                self.child(
                    _dump_text_context(self.schema_payload, text_half),
                    source_evidence=[
                        evidence for unit in text_half if (evidence := unit.evidence()) is not None
                    ],
                    text_batch=text_half,
                )
                for text_half in text_halves
            ]
        return None


@dataclass(frozen=True)
class _ValidatedBuildExtraction:
    name: OntologyBuildStepName
    label_ja: str
    extraction: OntologyBuildExtraction
    cross_check_sql: list[str] | None
    source_evidence: list[OntologyEvidence]


def _llm_call_chars(prompt: str, context: str) -> int:
    return len(_EXTRACTION_SYSTEM_PROMPT) + len(prompt) + len(context) + _LLM_CONTEXT_HEADROOM_CHARS


def _ensure_llm_call_fits(prompt: str, context: str) -> None:
    if _llm_call_chars(prompt, context) <= _ONTOLOGY_BUILD_LLM_CONTEXT_MAX_CHARS:
        return
    raise OntologyBuildContextBudgetError(
        "LLM 入力が上限を超えています。業務プロファイルの対象 object を減らすか、"
        "単一行・単一ページ・単一セルの内容を分割して再実行してください。"
    )


def _owner_label(names: dict[str, str], owner: str) -> str:
    """構築の注記の「『オブジェクト型』の」（所属が分からなければ空）。"""

    label = names.get(owner, "")
    return f"『{label}』の" if label else ""


def _with_prior_context(prompt: str, context: str, prior: dict[str, Any] | None) -> tuple[str, str]:
    """LLM の入力に、前の定義の要約と前の確認の直すべき点を足す（#1734）。

    上限（`_ONTOLOGY_BUILD_LLM_CONTEXT_MAX_CHARS`）を超えるときは、前の定義の要約から削り、
    診断（直すべき点）を優先して残す。足せなければ元の入力のまま。
    """

    if not prior:
        return prompt, context
    from .ontology_build_prior import PRIOR_PROMPT, shrink_prior_summary

    try:
        payload = json.loads(context)
    except ValueError:
        return prompt, context
    if not isinstance(payload, dict):
        return prompt, context
    with_prior = prompt + PRIOR_PROMPT
    findings = list(prior.get("previous_check_findings") or [])
    summary = prior.get("previous_ontology")
    summaries: list[dict[str, Any] | None] = [
        *(shrink_prior_summary(summary) if summary else []),
        None,
    ]
    while True:
        for candidate in summaries:
            extra: dict[str, Any] = {}
            if findings:
                extra["previous_check_findings"] = findings
            if candidate:
                extra["previous_ontology"] = candidate
            if not extra:
                return prompt, context
            text = json.dumps({**payload, **extra}, ensure_ascii=False, sort_keys=True)
            if _llm_call_chars(with_prior, text) <= _ONTOLOGY_BUILD_LLM_CONTEXT_MAX_CHARS:
                return with_prior, text
        if not findings:
            return prompt, context
        findings = findings[: len(findings) // 2]


def _schema_context_payload(schema_context: str) -> dict[str, Any]:
    try:
        loaded = json.loads(schema_context)
    except Exception as exc:
        raise OntologyBuildContextBudgetError("schema_context を解析できません。") from exc
    if not isinstance(loaded, dict):
        raise OntologyBuildContextBudgetError("schema_context が不正です。")
    return loaded


def _dedupe(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


@dataclass(frozen=True)
class _SchemaContextLookup:
    """schema context の object / 列を、SQL の参照から引く索引。

    キーは `object_identity` の照合キー（引用なしは大文字、引用名は大文字小文字を保持）、値は
    schema context に載せた表記（`SALES."Mixed_Case"`）。引用符を外して大文字化すると、Q/A SQL の
    `SALES."Mixed_Case"` を大文字の同名表 `SALES.MIXED_CASE` に解決する（#573）。
    """

    objects: dict[str, str]
    objects_by_name: dict[str, tuple[str, ...]]
    columns: dict[str, str]
    columns_by_name: dict[str, tuple[str, ...]]

    @classmethod
    def from_payload(cls, schema_context: Mapping[str, Any]) -> _SchemaContextLookup:
        objects: dict[str, str] = {}
        objects_by_name: dict[str, list[str]] = {}
        columns: dict[str, str] = {}
        columns_by_name: dict[str, list[str]] = {}
        for item in schema_context.get("objects") or []:
            if not isinstance(item, Mapping):
                continue
            object_key, object_label = _schema_context_object_key(item)
            if not object_key:
                continue
            objects[object_key] = object_label
            object_name = split_match_key(object_key)[-1]
            objects_by_name.setdefault(object_name, []).append(object_key)
            for raw_column in item.get("columns") or []:
                if isinstance(raw_column, Mapping):
                    column_label = str(raw_column.get("column") or "").strip()
                    qualified_label = str(raw_column.get("qualified_column") or "").strip()
                else:
                    column_label = str(raw_column).strip()
                    qualified_label = ""
                column_token = object_match_key(column_label)
                if not column_token:
                    continue
                column_key = f"{object_key}.{column_token}"
                columns[column_key] = qualified_label or f"{object_label}.{column_label}"
                columns_by_name.setdefault(column_token, []).append(column_key)
        return cls(
            objects=objects,
            objects_by_name={
                key: tuple(sorted(set(value))) for key, value in objects_by_name.items()
            },
            columns=columns,
            columns_by_name={
                key: tuple(sorted(set(value))) for key, value in columns_by_name.items()
            },
        )

    def resolve_object(self, owner: str, object_name: str) -> str | None:
        """owner / object の照合 token から object の照合キーを返す。"""

        object_token = object_match_key(object_name)
        if not object_token:
            return None
        owner_token = object_match_key(owner)
        if owner_token:
            key = f"{owner_token}.{object_token}"
            if key in self.objects:
                return key
        candidates = self.objects_by_name.get(object_token, ())
        return candidates[0] if len(candidates) == 1 else None

    def resolve_column(
        self,
        column_name: str,
        *,
        object_keys: Sequence[str] = (),
    ) -> str | None:
        """列の照合 token から列の照合キーを返す。"""

        column_token = object_match_key(column_name)
        if not column_token:
            return None
        if object_keys:
            matches = sorted(
                {
                    column_key
                    for object_key in object_keys
                    if (column_key := f"{object_key}.{column_token}") in self.columns
                }
            )
            if len(matches) == 1:
                return matches[0]
        candidates = self.columns_by_name.get(column_token, ())
        return candidates[0] if len(candidates) == 1 else None

    def resolve_qualified_column(
        self,
        reference: str,
        *,
        aliases: Mapping[str, set[str]],
        statement_objects: Sequence[str],
    ) -> str | None:
        """SQL の列参照（`m."Amount"` 等、各部は SQL の表記）から列の照合キーを返す。"""

        parts = split_match_key(object_match_key(reference))
        if not parts:
            return None
        column_name = parts[-1]
        if len(parts) >= 3:
            object_key = self.resolve_object(parts[-3], parts[-2])
            return self.resolve_column(column_name, object_keys=[object_key] if object_key else [])
        if len(parts) == 2:
            qualifier = parts[0]
            alias_targets = aliases.get(qualifier, set())
            if alias_targets:
                return self.resolve_column(column_name, object_keys=sorted(alias_targets))
            object_key = self.resolve_object("", qualifier)
            return self.resolve_column(column_name, object_keys=[object_key] if object_key else [])
        return self.resolve_column(column_name, object_keys=statement_objects)


def _schema_context_object_key(item: Mapping[str, Any]) -> tuple[str, str]:
    """schema context の object から（照合キー, 表記）を返す。"""

    owner = str(item.get("owner") or "").strip()
    object_name = str(item.get("object_name") or "").strip()
    if owner and object_name:
        return f"{object_match_key(owner)}.{object_match_key(object_name)}", (
            f"{owner}.{object_name}"
        )
    raw_object = str(item.get("object") or "").strip()
    parts = split_match_key(raw_object)
    if len(parts) >= 2:
        return (
            f"{object_match_key(parts[-2])}.{object_match_key(parts[-1])}",
            ".".join(parts[-2:]),
        )
    if parts:
        return object_match_key(parts[0]), parts[0]
    return object_match_key(object_name), object_name


def _explicit_projection_aliases(graph: Any) -> set[str]:
    # SELECT の出力別名の判定（物理 object の識別ではない）。別名は大文字で比べる。
    aliases: set[str] = set()
    for projection in graph.projections:
        output_name = str(projection.output_name).replace('"', "").strip().upper()
        if not output_name:
            continue
        expression = str(projection.expression_sql).replace('"', "").strip().upper()
        if f" AS {output_name}" in f" {expression} ":
            aliases.add(output_name)
    return aliases


def _derived_sql_qualifiers(graph: Any) -> set[str]:
    qualifiers = {token for cte in graph.ctes for token in _alias_tokens(str(cte.name))}
    qualifiers.update(
        token for subquery in graph.subqueries for token in _alias_tokens(str(subquery.alias))
    )
    return qualifiers


def _ignore_sql_column_reference(
    column: Any,
    *,
    projection_aliases: set[str],
    derived_qualifiers: set[str],
) -> bool:
    name = _sql_token(str(column.name), quoted=bool(column.name_quoted))
    qualifier = _sql_token(str(column.table), quoted=bool(column.table_quoted))
    if name in _ORACLE_PSEUDO_COLUMNS:
        return True
    if not qualifier and str(column.name).strip().upper() in projection_aliases:
        return True
    return bool(qualifier and qualifier in derived_qualifiers)


def _qa_pair_context_payload(schema_context: Mapping[str, Any], pair: QaPair) -> dict[str, Any]:
    payload = pair.model_dump(mode="json")
    payload["source_id"] = "qa:" + definition_fingerprint(payload)[:16]
    payload["locator"] = "question_sql"
    lookup = _SchemaContextLookup.from_payload(schema_context)
    analysis = parse_oracle_sql(pair.sql)
    graph = analysis.graph
    if graph is None:
        first_finding = analysis.validation.findings[0] if analysis.validation.findings else None
        if first_finding is not None:
            payload["sql_parse_warning_ja"] = first_finding.message_ja
        return payload

    aliases: dict[str, set[str]] = {}
    alias_rows: list[dict[str, str]] = []
    resolved_objects: list[dict[str, str]] = []
    resolved_object_keys: list[str] = []
    unresolved: list[str] = []
    for table in graph.tables:
        if table.is_cte:
            continue
        owner_token = _sql_token(str(table.owner), quoted=bool(table.owner_quoted))
        name_token = _sql_token(str(table.name), quoted=bool(table.name_quoted))
        object_key = lookup.resolve_object(owner_token, name_token)
        sql_table = str(table.source_sql or table.qualified_name or table.name)
        if object_key is None:
            unresolved.append(sql_table)
            continue
        object_label = lookup.objects[object_key]
        resolved_object_keys.append(object_key)
        resolved_objects.append(
            {
                "sql_table": sql_table,
                "object": object_label,
                "alias": str(table.alias or ""),
            }
        )
        statement_tokens = {
            name_token,
            f"{owner_token}.{name_token}" if owner_token else "",
        }
        for token in statement_tokens:
            if token:
                aliases.setdefault(token, set()).add(object_key)
        alias_tokens = _alias_tokens(str(table.alias or ""))
        for alias in alias_tokens:
            aliases.setdefault(alias, set()).add(object_key)
        if alias_tokens:
            alias_rows.append({"alias": object_match_key(str(table.alias)), "object": object_label})

    statement_objects = _dedupe(resolved_object_keys)
    projection_aliases = _explicit_projection_aliases(graph)
    derived_qualifiers = _derived_sql_qualifiers(graph)
    resolved_columns: list[dict[str, str]] = []
    ignored_aliases: list[str] = []
    for column in graph.columns:
        if str(column.name).strip() == "*":
            # `別名.*` / `owner.表.*` は列名 `*` の列ではなく、表の全列（#1787）。表は
            # schema_resolved_objects で解決し、列の未解決として LLM に渡さない。
            continue
        if _ignore_sql_column_reference(
            column,
            projection_aliases=projection_aliases,
            derived_qualifiers=derived_qualifiers,
        ):
            ignored_aliases.append(str(column.expression_sql))
            continue
        # expression_sql は SQL の表記（引用符を保持）なので、引用規則で解釈できる。
        resolved = lookup.resolve_qualified_column(
            str(column.expression_sql),
            aliases=aliases,
            statement_objects=statement_objects,
        )
        if resolved is None:
            unresolved.append(str(column.expression_sql))
            continue
        resolved_columns.append(
            {
                "sql": str(column.expression_sql),
                "column": lookup.columns[resolved],
                "clause": str(column.clause),
            }
        )

    resolved_join_conditions: list[dict[str, Any]] = []
    for join in graph.joins:
        # referenced_columns は SQL の表記（引用符を保持）なので、引用規則で解釈できる。
        columns = [
            lookup.columns[resolved]
            for reference in join.referenced_columns
            if (
                resolved := lookup.resolve_qualified_column(
                    reference,
                    aliases=aliases,
                    statement_objects=statement_objects,
                )
            )
            is not None
        ]
        if columns:
            resolved_join_conditions.append(
                {
                    "condition_sql": join.condition_sql,
                    "resolved_columns": sorted(set(columns)),
                }
            )

    payload["schema_resolved_objects"] = resolved_objects
    payload["schema_sql_aliases"] = alias_rows
    payload["schema_projection_aliases"] = sorted(projection_aliases)
    payload["schema_ignored_sql_aliases"] = _dedupe(ignored_aliases)
    payload["schema_resolved_columns"] = [
        item for index, item in enumerate(resolved_columns) if item not in resolved_columns[:index]
    ]
    payload["schema_resolved_join_conditions"] = resolved_join_conditions
    payload["schema_unresolved_references"] = sorted(set(unresolved))
    return payload


def _dump_text_context(
    schema_context: dict[str, Any],
    units: list[_BuildTextUnit],
) -> str:
    return json.dumps(
        {
            "schema_context": schema_context,
            "business_text_chunks": [unit.context_payload() for unit in units],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _dump_qa_context(schema_context: dict[str, Any], pairs: list[QaPair]) -> str:
    return json.dumps(
        {
            "schema_context": schema_context,
            "qa_pairs": [_qa_pair_context_payload(schema_context, pair) for pair in pairs],
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _extraction_batch_max_chars() -> int:
    """1 回の抽出呼び出しに載せる資料本文の上限。チャンクが大きいほど抽出漏れが
    増える(GraphRAG の知見)ため、入力上限とは別に本文量を絞る。"""

    return max(1000, int(get_settings().nl2sql_ontology_extraction_batch_max_chars))


def _split_text_unit_for_budget(
    schema_context: dict[str, Any],
    unit: _BuildTextUnit,
    prompt: str,
) -> list[_BuildTextUnit]:
    empty_unit = _BuildTextUnit(
        source_document=unit.source_document,
        source_label=unit.source_label,
        locator_kind=unit.locator_kind,
        locator=unit.locator,
        text="",
    )
    empty_context = _dump_text_context(schema_context, [empty_unit])
    available = (
        _ONTOLOGY_BUILD_LLM_CONTEXT_MAX_CHARS
        - _llm_call_chars(prompt, empty_context)
        - _LLM_CONTEXT_HEADROOM_CHARS
    )
    if available <= 0:
        raise OntologyBuildContextBudgetError(
            "schema_context が大きすぎるため、資料本文を 1 chunk も LLM 入力へ追加できません。"
            "業務プロファイルの対象 object を減らして再実行してください。"
        )
    available = min(available, _extraction_batch_max_chars())
    if len(unit.text) <= available:
        return [unit]
    parts: list[_BuildTextUnit] = []
    total_parts = (len(unit.text) + available - 1) // available
    for index, start in enumerate(range(0, len(unit.text), available), start=1):
        part_text = unit.text[start : start + available]
        parts.append(
            _BuildTextUnit(
                source_document=unit.source_document,
                source_label=unit.source_label,
                locator_kind=unit.locator_kind,
                locator=f"{unit.locator};part:{index}/{total_parts}",
                text=part_text,
            )
        )
    return parts


def _batch_text_units(
    schema_context: dict[str, Any],
    units: list[_BuildTextUnit],
    prompt: str,
) -> list[list[_BuildTextUnit]]:
    expanded: list[_BuildTextUnit] = []
    for unit in units:
        if unit.text.strip():
            expanded.extend(_split_text_unit_for_budget(schema_context, unit, prompt))
    batch_max_chars = _extraction_batch_max_chars()
    batches: list[list[_BuildTextUnit]] = []
    current: list[_BuildTextUnit] = []
    for unit in expanded:
        candidate = [*current, unit]
        candidate_context = _dump_text_context(schema_context, candidate)
        candidate_content_chars = sum(len(item.text) for item in candidate)
        if (
            _llm_call_chars(prompt, candidate_context) <= _ONTOLOGY_BUILD_LLM_CONTEXT_MAX_CHARS
            and candidate_content_chars <= batch_max_chars
        ):
            current = candidate
            continue
        if not current:
            _ensure_llm_call_fits(prompt, candidate_context)
        batches.append(current)
        current = [unit]
        _ensure_llm_call_fits(prompt, _dump_text_context(schema_context, current))
    if current:
        batches.append(current)
    return batches


def _batch_qa_pairs(
    schema_context: dict[str, Any],
    pairs: list[QaPair],
    prompt: str,
) -> list[list[QaPair]]:
    batch_max_chars = _extraction_batch_max_chars()
    batches: list[list[QaPair]] = []
    current: list[QaPair] = []
    for pair in pairs:
        candidate = [*current, pair]
        candidate_context = _dump_qa_context(schema_context, candidate)
        candidate_content_chars = sum(len(item.question) + len(item.sql) for item in candidate)
        if _llm_call_chars(prompt, candidate_context) <= _ONTOLOGY_BUILD_LLM_CONTEXT_MAX_CHARS and (
            len(candidate) == 1 or candidate_content_chars <= batch_max_chars
        ):
            current = candidate
            continue
        if not current:
            raise OntologyBuildContextBudgetError(
                "1 件の Q/A が LLM 入力上限を超えています。"
                "質問または SQL を分割して再実行してください。"
            )
        batches.append(current)
        current = [pair]
        _ensure_llm_call_fits(prompt, _dump_qa_context(schema_context, current))
    if current:
        batches.append(current)
    return batches


# --- job service ------------------------------------------------------------------------------

_STEP_LABELS_JA: dict[OntologyBuildStepName, str] = {
    OntologyBuildStepName.SOURCE_EXTRACTION: "資料の抽出",
    OntologyBuildStepName.SCHEMA_CONTEXT: "スキーマ情報の準備",
    OntologyBuildStepName.SCHEMA_NAMING: "スキーマからの概念抽出",
    OntologyBuildStepName.QA_EXTRACTION: "Q/A からの抽出",
    OntologyBuildStepName.TEXT_EXTRACTION: "業務説明からの抽出",
    OntologyBuildStepName.PROPOSAL_REGISTRATION: "Markdown 下書き生成",
}
_MAX_JOB_EVENTS = 100
# 定義のフェーズのうち、アクティビティのイベントに付けられるもの(名前 → OntologyBuildPhase)。
# cast を使わずに Literal へ写す(mypy の版で `in` による絞り込みの有無が違っても通るように。#1661)。
_EVENT_PHASE_BY_NAME: dict[str, OntologyBuildPhase] = {
    name: name for name in get_args(OntologyBuildPhase)
}


def _running_event_phase(phases: Sequence[DefinitionPhase]) -> OntologyBuildPhase | None:
    """最後に実行中になった定義のフェーズを、イベントに付けられるときだけ返す。"""

    active = next((p for p in reversed(phases) if p.status == "running"), None)
    return _EVENT_PHASE_BY_NAME.get(active.name) if active else None


# 完了(succeeded/failed/cancelled)job の in-memory 保持上限。超過分は start 時に古い順へ破棄する
_MAX_FINISHED_JOBS = 20
_TERMINAL_STATUSES = {
    OntologyBuildStatus.SUCCEEDED,
    OntologyBuildStatus.SUCCEEDED_WITH_WARNINGS,
    OntologyBuildStatus.FAILED,
    OntologyBuildStatus.CANCELLED,
}
_PROFILE_ACTIVE_LOCK_OPERATION = "build_ontology_profile_active"
_PROFILE_ACTIVE_LOCK_STALE_SECONDS = 300.0
_TERMINAL_STEP_STATUSES = {
    OntologyBuildStepStatus.SUCCEEDED,
    OntologyBuildStepStatus.FAILED,
    OntologyBuildStepStatus.SKIPPED,
}


class OntologyBuildExecutionLost(RuntimeError):
    """失効した実行が新しい状態・成果物を更新するのを止める。"""


class OntologySourceDocumentGone(RuntimeError):
    """構築 job が参照する保存済み資料が、実行中に削除されていた。"""

    def __init__(self, source_document_id: str) -> None:
        super().__init__("保存済みファイルが削除されたため、この資料は利用しません。")
        self.source_document_id = source_document_id


def inprocess_worker_thread_name(job_id: str) -> str:
    """local（inprocess）の構築を実行する thread の名前。"""

    return f"{job_id}-worker"


class OntologyBuildService:
    """永続 job。local は thread、production は独立 worker から同じ run を呼ぶ。"""

    def __init__(
        self,
        runtime: Any,
        *,
        source_storage: OntologySourceStorage | None = None,
    ) -> None:
        self._runtime = runtime
        self._source_storage = source_storage or OntologySourceStorage()
        self._jobs: dict[str, OntologyBuildJob] = {}
        self._inputs: dict[str, dict[str, Any]] = {}
        # store から復元した job と、このプロセスが実行している job を区別する。
        self._inprocess_jobs: set[str] = set()
        self._execution_ids: dict[str, str] = {}
        # _persist_job の read-modify-write を直列化する(worker thread と API thread の競合防止)
        self._persist_lock = threading.RLock()
        self._lock = threading.Lock()
        # 実行中の job の LLM の呼び出しの実行器（#2004。job の中で上限・待ちを共有する）。
        self._ai_bridges: dict[str, SyncBoundedCaller] = {}

    def start(
        self,
        profile_id: str,
        *,
        business_text: str = "",
        qa_pairs: list[QaPair] | None = None,
        run_schema_naming: bool = True,
        run_qa_extraction: bool = True,
        run_text_extraction: bool = True,
        initial_warnings: list[str] | None = None,
        source_documents: list[OntologySourceDocument] | None = None,
        idempotency_key: str | None = None,
        checkpoint_job_ids: Sequence[str] = (),
    ) -> OntologyBuildJob:
        # 未知 profile を非同期 error に隠さない。重いオントロジー同期は worker 側の
        # 「スキーマ情報の準備」ステップで行い、POST は即時に job を返す。
        self._runtime.ensure_profile(profile_id)
        pairs = qa_pairs or []
        sources = source_documents or []
        request_hash = hashlib.sha256(
            canonical_json(
                {
                    "profile_id": profile_id,
                    "business_text": business_text,
                    "qa_pairs": pairs,
                    "source_sha256": [source.sha256 for source in sources],
                    "source_roles": [source.source_role.value for source in sources],
                    "run_schema_naming": run_schema_naming,
                    "run_qa_extraction": run_qa_extraction,
                    "run_text_extraction": run_text_extraction,
                }
            ).encode("utf-8")
        ).hexdigest()
        if idempotency_key:
            existing = self._runtime.store.get_idempotency("build_ontology", idempotency_key)
            if existing is not None:
                if existing.get("request_hash") != request_hash:
                    raise OntologyVersionConflictError(
                        "IDEMPOTENCY_KEY_REUSED",
                        "同じ Idempotency-Key が別の構築リクエストに使用されています。",
                    )
                restored = self.get(str(existing.get("resource_id") or ""))
                if restored is not None:
                    return restored
        steps: list[OntologyBuildStep] = []
        if sources:
            steps.append(OntologyBuildStep(name=OntologyBuildStepName.SOURCE_EXTRACTION))
        steps.append(OntologyBuildStep(name=OntologyBuildStepName.SCHEMA_CONTEXT))
        if run_schema_naming:
            steps.append(OntologyBuildStep(name=OntologyBuildStepName.SCHEMA_NAMING))
        if run_qa_extraction and (pairs or sources):
            steps.append(OntologyBuildStep(name=OntologyBuildStepName.QA_EXTRACTION))
        if run_text_extraction and (business_text.strip() or sources):
            steps.append(OntologyBuildStep(name=OntologyBuildStepName.TEXT_EXTRACTION))
        steps.append(OntologyBuildStep(name=OntologyBuildStepName.PROPOSAL_REGISTRATION))
        job = OntologyBuildJob(
            id=f"ontology_build_{uuid4().hex}",
            profile_id=profile_id,
            steps=steps,
            definition_phases=[
                DefinitionPhase(name=name)
                for name in (
                    "freeze",
                    "evidence",
                    "concepts",
                    "validation",
                    "markdown",
                    "save",
                )
            ],
            profile_fingerprint=definition_fingerprint(
                self._runtime._strict_profile(profile_id).model_dump(mode="json")
            ),
            # POST 応答に最初のフィードバックを含める(worker 開始を待たない)
            events=[
                OntologyBuildEvent(message_ja="構築リクエストを受け付けました。処理を開始します。")
            ],
            warnings_ja=list(initial_warnings or []),
            source_document_ids=[source.id for source in sources],
            sources=[
                OntologySourceProgress(
                    source_document_id=source.id,
                    filename=source.filename,
                    status=source.status,
                )
                for source in sources
            ],
        )
        self._acquire_profile_job_lock(profile_id, job.id)
        try:
            for source in sources:
                self._save_source_document(source)
            with self._lock:
                self._prune_finished_jobs_locked()
                self._jobs[job.id] = job
                self._inputs[job.id] = {
                    "business_text": business_text,
                    "qa_pairs": [pair.model_dump(mode="json") for pair in pairs],
                    # retry がステップ構成を忠実に再現できるようトグルも永続化する
                    "run_schema_naming": run_schema_naming,
                    "run_qa_extraction": run_qa_extraction,
                    "run_text_extraction": run_text_extraction,
                    "checkpoint_job_ids": list(checkpoint_job_ids),
                }
            self._persist_job(job)
            if idempotency_key:
                self._runtime.store.save_idempotency(
                    {
                        "operation": "build_ontology",
                        "idempotency_key": idempotency_key,
                        "request_hash": request_hash,
                        "resource_id": job.id,
                        "status": "accepted",
                    }
                )
            if get_settings().nl2sql_ontology_worker_mode == "inprocess":
                with self._lock:
                    self._inprocess_jobs.add(job.id)
                # 名前で実行 thread を特定できるようにする（heartbeat と同じ形。テストは
                # この thread の終了を待って結果を読む。#1914）。
                thread = threading.Thread(
                    target=self._run_inprocess,
                    args=(job.id, business_text, pairs),
                    daemon=True,
                    name=inprocess_worker_thread_name(job.id),
                )
                thread.start()
            return job.model_copy(deep=True)
        except Exception:
            with self._lock:
                self._inprocess_jobs.discard(job.id)
            self._release_profile_job_lock(profile_id, job.id)
            raise

    def _run_inprocess(self, job_id: str, business_text: str, qa_pairs: list[QaPair]) -> None:
        try:
            self._run_safely(job_id, business_text, qa_pairs)
        finally:
            with self._lock:
                self._inprocess_jobs.discard(job_id)

    def _recover_document(self, document: dict[str, Any] | None) -> dict[str, Any] | None:
        """heartbeat が消えた実行を読み取り時に終了させる。LLM は再送しない。"""
        for attempt in range(3):
            job = self._job_from_document(document)
            if not document or not job or job.status in _TERMINAL_STATUSES:
                return document
            settings = get_settings()
            deadline = document.get("deadline_at")
            expires = (
                datetime.fromisoformat(str(deadline))
                if deadline
                else job.created_at
                + timedelta(seconds=settings.nl2sql_ontology_build_timeout_seconds)
            )
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            last_seen = max(
                value if value.tzinfo else value.replace(tzinfo=UTC)
                for value in (
                    job.created_at,
                    job.started_at or job.created_at,
                    *(event.at for event in job.events),
                )
            )
            heartbeat = document.get("heartbeat_at")
            if heartbeat:
                last_seen = datetime.fromisoformat(str(heartbeat))
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=UTC)
            timed_out = utc_now() >= expires
            lost = (
                job.status != OntologyBuildStatus.QUEUED
                or document.get("dispatch_mode", settings.nl2sql_ontology_worker_mode)
                == "inprocess"
            ) and (
                (utc_now() - last_seen).total_seconds()
                >= settings.nl2sql_ontology_build_lease_seconds
            )
            if not timed_out and not lost:
                return document
            code = "ONTOLOGY_BUILD_TIMEOUT" if timed_out else "ONTOLOGY_BUILD_WORKER_LOST"
            message = (
                "構築の実行期限を超えました。"
                if timed_out
                else "構築処理の実行プロセスとの接続が失われました。"
            ) + "保存済み入力と抽出結果を使って再実行してください。"
            try:
                self._save_cancelled(document, job, message, code=code)
                logger.warning(
                    message,
                    extra={"job_id": job.id, "profile_id": job.profile_id, "error_code": code},
                )
                return cast(dict[str, Any] | None, self._runtime.store.get_job(job.id))
            except OntologyVersionConflict:
                document = self._runtime.store.get_job(job.id)
                if attempt == 2:
                    raise
        return document

    def _claim_execution(self, job_id: str) -> str | None:
        document = self._recover_document(self._runtime.store.get_job(job_id))
        job = self._job_from_document(document)
        if not document or not job or job.status != OntologyBuildStatus.QUEUED:
            return None
        token = uuid4().hex
        job.status = OntologyBuildStatus.RUNNING
        job.started_at = utc_now()
        try:
            self._runtime.store.save_job(
                {
                    **document,
                    "status": "running",
                    "payload": job.model_dump(mode="json"),
                    "execution_id": token,
                    "heartbeat_at": utc_now().isoformat(),
                    "claimed_by": token,
                    "claimed_at": time.time(),
                },
                expected_etag=document["etag"],
            )
        except OntologyVersionConflict:
            return None
        with self._lock:
            self._execution_ids[job_id] = token
            self._jobs[job_id] = job
        return token

    def _heartbeat(self, job_id: str, token: str) -> bool:
        for _ in range(3):
            document = self._recover_document(self._runtime.store.get_job(job_id))
            if (
                not document
                or document.get("execution_id") != token
                or document["status"] != "running"
            ):
                return False
            try:
                self._runtime.store.save_job(
                    {**document, "heartbeat_at": utc_now().isoformat(), "claimed_at": time.time()},
                    expected_etag=document["etag"],
                )
                return True
            except OntologyVersionConflict:
                continue
        return True

    def _assert_execution(self, job_id: str) -> None:
        document = self._recover_document(self._runtime.store.get_job(job_id))
        with self._lock:
            token = self._execution_ids.get(job_id)
        if (
            not document
            or document["status"] == "cancelled"
            or (document.get("execution_id") and document.get("execution_id") != token)
        ):
            raise OntologyBuildExecutionLost(job_id)

    def shutdown(self) -> None:
        """再ロードで消える自プロセスの daemon job を永続的に中断する。"""

        with self._lock:
            job_ids = tuple(self._inprocess_jobs)
        for job_id in job_ids:
            for attempt in range(3):
                try:
                    document = self._runtime.store.get_document("jobs", {"job_id": job_id})
                    job = self._job_from_document(document)
                    if document is None or job is None or job.status in _TERMINAL_STATUSES:
                        break
                    self._save_cancelled(
                        document,
                        job,
                        "バックエンドの停止・再起動により構築を中断しました。再実行してください。",
                        code="ONTOLOGY_BUILD_PROCESS_STOPPED",
                    )
                    logger.warning(
                        "バックエンドの停止・再起動によりオントロジー構築を中断しました。"
                        "保存済み入力から再実行してください。",
                        extra={"event": "ontology_build_process_stopped", "job_id": job_id},
                    )
                    break
                except OntologyVersionConflict:
                    if attempt < 2:
                        continue
                    logger.exception(
                        "ontology_build_shutdown_state_conflict", extra={"job_id": job_id}
                    )
                except Exception:
                    logger.exception("ontology_build_shutdown_failed", extra={"job_id": job_id})
                    break

    def peek(self, job_id: str) -> OntologyBuildJob | None:
        """scope 認可前は復旧の書き込みを行わない。"""
        return self._job_from_document(self._runtime.store.get_job(job_id))

    def get(self, job_id: str) -> OntologyBuildJob | None:
        document = self._recover_document(self._runtime.store.get_job(job_id))
        stored = self._job_from_document(document)
        if stored and stored.status in _TERMINAL_STATUSES:
            with self._lock:
                self._jobs[job_id] = stored
            return self._normalize_job_for_response(stored)
        # 自プロセスの実行 thread だけは保存前の進捗を返す。再ロード後に復元した
        # job と external job は store が正(独立 worker の復旧結果も反映する)。
        with self._lock:
            owns_execution = job_id in self._inprocess_jobs
        if not owns_execution or get_settings().nl2sql_ontology_worker_mode == "external":
            restored = self._restore_from_store(job_id)
            if restored is not None:
                return self._normalize_job_for_response(restored)
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                # 終端は永続化後に返す。未保存の failed を見て直ちに retry する競合を防ぐ。
                if job.status in _TERMINAL_STATUSES and stored is not None:
                    return self._normalize_job_for_response(stored)
                return self._normalize_job_for_response(job)
        restored = self._restore_from_store(job_id)
        return self._normalize_job_for_response(restored) if restored is not None else None

    def _restore_from_store(self, job_id: str) -> OntologyBuildJob | None:
        """store の job を in-memory キャッシュへ再水和して返す(未登録は None)。"""

        document = self._runtime.store.get_document("jobs", {"job_id": job_id})
        if document is None or document.get("job_type") != "build":
            return None
        restored = OntologyBuildJob.model_validate(document["payload"])
        with self._lock:
            self._jobs[job_id] = restored
            input_payload = document.get("input_payload")
            if isinstance(input_payload, dict):
                self._inputs[job_id] = dict(input_payload)
        return restored

    def run_persisted(self, job_id: str) -> OntologyBuildJob:
        """独立 worker から、Oracle に保存した入力だけで job を再開する。"""

        job = self.get(job_id)
        if job is None:
            raise RuntimeError("Ontology build job が見つかりません。")
        with self._lock:
            payload = dict(self._inputs.get(job_id, {}))
        if not payload:
            document = self._runtime.store.get_document("jobs", {"job_id": job_id})
            raw_payload = document.get("input_payload") if document is not None else None
            payload = dict(raw_payload) if isinstance(raw_payload, dict) else {}
        business_text = str(payload.get("business_text") or "")
        qa_pairs = [QaPair.model_validate(item) for item in payload.get("qa_pairs", [])]
        self._run_safely(job_id, business_text, qa_pairs)
        result = self.get(job_id)
        if result is None:
            raise RuntimeError("Ontology build job の実行結果を取得できません。")
        return result

    def purge_profile_source_documents(self, profile_id: str) -> int:
        """Profile 削除時に source_documents 行とアップロード実体を削除する(残置リーク防止)。"""

        documents = self._runtime.store.list_documents(
            "source_documents", {"profile_id": profile_id}
        )
        for document in documents:
            try:
                source = OntologySourceDocument.model_validate(document["payload"])
                self._source_storage.delete(source)
            except Exception:
                # 実体削除の失敗で行削除・Profile 削除全体を止めない(cleanup は best-effort)
                logger.warning(
                    "ontology_source_blob_cleanup_failed",
                    exc_info=True,
                    extra={"profile_id": profile_id},
                )
        return int(
            self._runtime.store.delete_documents("source_documents", {"profile_id": profile_id})
        )

    def discard_source_documents(self, sources: Sequence[OntologySourceDocument]) -> int:
        """Job 投入失敗時に、今回保存した source_documents だけを best-effort で破棄する。"""

        deleted_rows = 0
        for source in sources:
            try:
                self._source_storage.delete(source)
            except Exception:
                logger.warning(
                    "ontology_source_blob_cleanup_failed",
                    exc_info=True,
                    extra={
                        "profile_id": source.profile_id,
                        "source_document_id": source.id,
                    },
                )
            try:
                deleted_rows += int(
                    self._runtime.store.delete_documents(
                        "source_documents",
                        {"source_document_id": source.id},
                    )
                )
            except Exception:
                logger.warning(
                    "ontology_source_row_cleanup_failed",
                    exc_info=True,
                    extra={
                        "profile_id": source.profile_id,
                        "source_document_id": source.id,
                    },
                )
        return deleted_rows

    def delete_profile_source_document(
        self,
        profile_id: str,
        source_document_id: str,
    ) -> OntologySourceDocument:
        """利用者操作で profile 配下の保存済み構築資料を 1 件削除する。"""

        document = self._runtime.store.get_document(
            "source_documents", {"source_document_id": source_document_id}
        )
        if document is None:
            raise OntologyNotFoundError(
                "ONTOLOGY_SOURCE_DOCUMENT_NOT_FOUND",
                "保存済みファイルが見つかりません。表示を更新して対象を確認してください。",
            )
        try:
            source = OntologySourceDocument.model_validate(document["payload"])
        except Exception as exc:
            raise OntologyStateConflictError(
                "ONTOLOGY_SOURCE_DOCUMENT_INVALID",
                "保存済みファイルの保存データを読み取れません。表示を更新して対象を確認してください。",
            ) from exc
        if source.profile_id != profile_id or document.get("profile_id") != profile_id:
            raise OntologyNotFoundError(
                "ONTOLOGY_SOURCE_DOCUMENT_NOT_FOUND",
                "保存済みファイルが見つかりません。表示を更新して対象を確認してください。",
            )

        try:
            self._source_storage.delete(source)
        except Exception:
            logger.warning(
                "ontology_source_blob_cleanup_failed",
                exc_info=True,
                extra={
                    "profile_id": profile_id,
                    "source_document_id": source_document_id,
                },
            )
        deleted_rows = self._runtime.store.delete_documents(
            "source_documents",
            {
                "source_document_id": source_document_id,
                "profile_id": profile_id,
            },
        )
        if deleted_rows < 1:
            raise OntologyNotFoundError(
                "ONTOLOGY_SOURCE_DOCUMENT_NOT_FOUND",
                "保存済みファイルが見つかりません。表示を更新して対象を確認してください。",
            )
        return source

    def cancel_profile_jobs(self, profile_id: str) -> int:
        """削除対象 Profile の queued/running build を永続的に取消す。"""

        cancelled = 0
        documents = self._runtime.store.list_documents("jobs", {"profile_id": profile_id})
        for listed in documents:
            if listed.get("job_type") != "build":
                continue
            job_id = str(listed.get("job_id") or listed["payload"].get("id") or "")
            document: dict[str, Any] | None = listed
            # 実行中 job の進捗書込と ETag が競合しても、cancel() と同様に再読して再判定する
            for attempt in range(3):
                if document is None:
                    break
                job = OntologyBuildJob.model_validate(document["payload"])
                if job.status in _TERMINAL_STATUSES:
                    break
                try:
                    self._save_cancelled(
                        document, job, "業務 Profile が削除されたため構築を中止しました。"
                    )
                    cancelled += 1
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    logger.info("ontology_build_profile_cancel_conflict job_id=%s", job_id)
                    document = self._runtime.store.get_document("jobs", {"job_id": job_id})
        return cancelled

    def _save_cancelled(
        self,
        document: dict[str, Any],
        job: OntologyBuildJob,
        message_ja: str,
        *,
        code: str = "",
    ) -> None:
        """job を CANCELLED として ETag 付きで保存し、in-memory も同期する。"""

        now = utc_now()
        job.status = OntologyBuildStatus.CANCELLED
        job.error_message_ja = message_ja
        job.finished_at = now
        if code:
            job.error_code = code
        for phase in job.definition_phases:
            if phase.status in {"pending", "running"}:
                phase.status = "skipped"
                phase.finished_at = now.isoformat()
        for step in job.steps:
            if step.status in {
                OntologyBuildStepStatus.PENDING,
                OntologyBuildStepStatus.RUNNING,
            }:
                step.status = OntologyBuildStepStatus.SKIPPED
                step.finished_at = now
        job.events.append(OntologyBuildEvent(at=now, message_ja=message_ja, code=code))
        del job.events[:-_MAX_JOB_EVENTS]
        self._runtime.store.save_document(
            "jobs",
            {
                **{
                    key: value
                    for key, value in document.items()
                    if key not in {"etag", "created_at", "updated_at"}
                },
                "status": OntologyBuildStatus.CANCELLED.value,
                "payload": job.model_dump(mode="json"),
            },
            expected_etag=str(document["etag"]),
        )
        self._release_profile_job_lock(job.profile_id, job.id)
        with self._lock:
            self._jobs[job.id] = job.model_copy(deep=True)

    def list_profile_jobs(self, profile_id: str, *, limit: int = 5) -> list[OntologyBuildJob]:
        """profile の build job を新しい順に返す(store が正、リロード復旧/履歴用)。"""

        documents = self._runtime.store.list_documents("jobs", {"profile_id": profile_id})
        jobs = [
            job
            for document in documents
            if document.get("job_type") == "build"
            if (job := self._job_from_document(self._recover_document(document))) is not None
        ]
        jobs.sort(key=lambda job: (job.created_at, job.id), reverse=True)
        return [self._normalize_job_for_response(job) for job in jobs[: max(1, limit)]]

    def list_profile_source_documents(
        self, profile_id: str, *, limit: int = 20
    ) -> list[OntologySourceDocument]:
        """profile に保存済みの Ontology 構築資料を新しい順に返す。"""

        documents = self._runtime.store.list_documents(
            "source_documents", {"profile_id": profile_id}
        )
        sources: list[OntologySourceDocument] = []
        for document in documents:
            try:
                source = OntologySourceDocument.model_validate(document["payload"])
                if source.profile_id != profile_id:
                    logger.warning(
                        "ontology_source_document_profile_mismatch",
                        extra={
                            "profile_id": profile_id,
                            "source_document_id": source.id,
                            "source_profile_id": source.profile_id,
                        },
                    )
                    continue
                sources.append(source)
            except Exception:
                logger.warning(
                    "ontology_source_document_decode_failed",
                    exc_info=True,
                    extra={"profile_id": profile_id},
                )
        sources.sort(key=lambda source: (source.created_at, source.id), reverse=True)
        return sources[: max(1, limit)]

    def cancel(self, job_id: str) -> OntologyBuildJob:
        """ユーザー起点の単一 job キャンセル。

        queued/running → CANCELLED(実行ループは既存の cancelled チェックで停止)。
        CANCELLED → no-op 成功。SUCCEEDED/FAILED → 409。
        """

        for _attempt in range(3):
            document = self._runtime.store.get_document("jobs", {"job_id": job_id})
            if document is None or document.get("job_type") != "build":
                raise OntologyNotFoundError(
                    "ONTOLOGY_BUILD_JOB_NOT_FOUND",
                    "AI オントロジー構築 job が見つかりません。",
                )
            job = OntologyBuildJob.model_validate(document["payload"])
            if job.status == OntologyBuildStatus.CANCELLED:
                return job.model_copy(deep=True)
            if job.status in _TERMINAL_STATUSES:
                raise OntologyStateConflictError(
                    "ONTOLOGY_BUILD_JOB_FINISHED",
                    "この構築 job は既に完了しているため中止できません。",
                )
            try:
                self._save_cancelled(document, job, "利用者の操作で構築を中止しました。")
                record_job(job_type="build", status="cancelled", error_code="user_cancelled")
                return job.model_copy(deep=True)
            except Exception:  # 完了/進捗書込と競合(ETag 不一致)→ 再読して再判定
                logger.info("ontology_build_cancel_conflict job_id=%s", job_id)
                continue
        raise OntologyStateConflictError(
            "ONTOLOGY_BUILD_CANCEL_CONFLICT",
            "構築 job の状態が変化し続けているため中止できませんでした。再試行してください。",
        )

    def retry(self, job_id: str) -> OntologyBuildJob:
        """failed/cancelled job を保存済み入力から新規 job として再実行する。"""

        document = self._recover_document(self._runtime.store.get_job(job_id))
        if document is None or document.get("job_type") != "build":
            raise OntologyNotFoundError(
                "ONTOLOGY_BUILD_JOB_NOT_FOUND",
                "AI オントロジー構築 job が見つかりません。",
            )
        job = OntologyBuildJob.model_validate(document["payload"])
        if job.status not in {OntologyBuildStatus.FAILED, OntologyBuildStatus.CANCELLED}:
            raise OntologyStateConflictError(
                "ONTOLOGY_BUILD_JOB_NOT_RETRYABLE",
                "失敗または中止された構築 job だけを再実行できます。",
            )
        raw_input = document.get("input_payload")
        payload = dict(raw_input) if isinstance(raw_input, dict) else {}
        step_names = {step.name for step in job.steps}

        def toggle(key: str, step_name: OntologyBuildStepName) -> bool:
            if key in payload:
                return bool(payload[key])
            # 旧 job(トグル未永続化)はステップ構成から復元する
            return step_name in step_names

        sources: list[OntologySourceDocument] = []
        for source_id in job.source_document_ids:
            try:
                source = self._get_source_document(source_id, profile_id=job.profile_id)
            except Exception as exc:
                raise OntologyStateConflictError(
                    "ONTOLOGY_BUILD_SOURCE_MISSING",
                    "元の構築に使った資料が見つからないため再実行できません。"
                    "資料を選び直して新規に実行してください。",
                ) from exc
            sources.append(
                source.model_copy(
                    update={"status": OntologySourceStatus.STORED, "updated_at": utc_now()},
                    deep=True,
                )
            )
        return self.start(
            job.profile_id,
            business_text=str(payload.get("business_text") or ""),
            qa_pairs=[QaPair.model_validate(item) for item in payload.get("qa_pairs", [])],
            run_schema_naming=toggle("run_schema_naming", OntologyBuildStepName.SCHEMA_NAMING),
            run_qa_extraction=toggle("run_qa_extraction", OntologyBuildStepName.QA_EXTRACTION),
            run_text_extraction=toggle(
                "run_text_extraction", OntologyBuildStepName.TEXT_EXTRACTION
            ),
            source_documents=sources,
            idempotency_key=f"retry:{job_id}:{uuid4().hex}",
            checkpoint_job_ids=[job_id, *payload.get("checkpoint_job_ids", [])],
        )

    def _acquire_profile_job_lock(self, profile_id: str, job_id: str) -> None:
        active = self._find_active_profile_job(profile_id)
        if active is not None:
            self._raise_active_profile_job_conflict(active)
        existing = self._runtime.store.get_idempotency(
            _PROFILE_ACTIVE_LOCK_OPERATION,
            profile_id,
        )
        if existing is not None:
            active = self._active_job_from_lock(existing)
            if active is not None:
                self._raise_active_profile_job_conflict(active)
            self._delete_profile_job_lock(profile_id, str(existing.get("resource_id") or ""))
        try:
            self._runtime.store.save_idempotency(
                {
                    "operation": _PROFILE_ACTIVE_LOCK_OPERATION,
                    "idempotency_key": profile_id,
                    "request_hash": hashlib.sha256(profile_id.encode("utf-8")).hexdigest(),
                    "resource_id": job_id,
                    "status": "active",
                    "payload": {
                        "operation": _PROFILE_ACTIVE_LOCK_OPERATION,
                        "profile_id": profile_id,
                        "job_id": job_id,
                        "created_at_epoch": time.time(),
                    },
                },
                expected_etag=None,
            )
        except OntologyVersionConflict as exc:
            lock = self._runtime.store.get_idempotency(
                _PROFILE_ACTIVE_LOCK_OPERATION,
                profile_id,
            )
            active = self._active_job_from_lock(lock) if lock is not None else None
            if active is None:
                active = self._find_active_profile_job(profile_id)
            if active is not None:
                self._raise_active_profile_job_conflict(active)
            raise OntologyStateConflictError(
                "ONTOLOGY_BUILD_START_CONFLICT",
                "構築 job の開始状態が競合しました。再試行してください。",
            ) from exc

    def _release_profile_job_lock(self, profile_id: str, job_id: str) -> None:
        try:
            self._delete_profile_job_lock(profile_id, job_id)
        except Exception:
            logger.warning(
                "ontology_build_profile_lock_release_failed",
                exc_info=True,
                extra={"profile_id": profile_id, "job_id": job_id},
            )

    def _delete_profile_job_lock(self, profile_id: str, job_id: str) -> None:
        existing = self._runtime.store.get_idempotency(
            _PROFILE_ACTIVE_LOCK_OPERATION,
            profile_id,
        )
        if existing is None or str(existing.get("resource_id") or "") != job_id:
            return
        self._runtime.store.delete_documents(
            "idempotency",
            {
                "operation": _PROFILE_ACTIVE_LOCK_OPERATION,
                "idempotency_key": profile_id,
                "resource_id": job_id,
            },
        )

    def _active_job_from_lock(self, lock: Mapping[str, Any]) -> OntologyBuildJob | None:
        job_id = str(lock.get("resource_id") or "")
        if not job_id:
            return None
        document = self._recover_document(self._runtime.store.get_job(job_id))
        job = self._job_from_document(document)
        if job is None:
            payload = lock.get("payload")
            created_at_epoch = (
                payload.get("created_at_epoch") if isinstance(payload, Mapping) else 0
            )
            lock_age = _PROFILE_ACTIVE_LOCK_STALE_SECONDS + 1
            if isinstance(created_at_epoch, int | float | str):
                try:
                    lock_age = time.time() - float(created_at_epoch)
                except ValueError:
                    lock_age = _PROFILE_ACTIVE_LOCK_STALE_SECONDS + 1
            if 0 <= lock_age <= _PROFILE_ACTIVE_LOCK_STALE_SECONDS:
                profile_id = (
                    str(payload.get("profile_id") or "")
                    if isinstance(payload, Mapping)
                    else str(lock.get("idempotency_key") or "")
                )
                return OntologyBuildJob(
                    id=job_id,
                    profile_id=profile_id,
                    status=OntologyBuildStatus.QUEUED,
                )
            return None
        return job if job.status not in _TERMINAL_STATUSES else None

    def _find_active_profile_job(self, profile_id: str) -> OntologyBuildJob | None:
        jobs = [
            job
            for document in self._runtime.store.list_documents("jobs", {"profile_id": profile_id})
            if (job := self._job_from_document(self._recover_document(document))) is not None
            and job.status not in _TERMINAL_STATUSES
        ]
        jobs.sort(key=lambda job: (job.created_at, job.id))
        return jobs[0] if jobs else None

    @staticmethod
    def _job_from_document(document: Mapping[str, Any] | None) -> OntologyBuildJob | None:
        if document is None or document.get("job_type") != "build":
            return None
        try:
            return OntologyBuildJob.model_validate(document["payload"])
        except Exception:
            logger.warning("ontology_build_job_decode_failed", exc_info=True)
            return None

    @staticmethod
    def _raise_active_profile_job_conflict(job: OntologyBuildJob) -> None:
        raise OntologyStateConflictError(
            "ONTOLOGY_BUILD_JOB_ALREADY_RUNNING",
            f"この業務プロファイルでは構築 job {job.id} が実行中です。完了後に再実行してください。",
        )

    # --- internal ---------------------------------------------------------------------------

    def _prune_finished_jobs_locked(self) -> None:
        """lock 保持中に呼ぶ。完了 job が上限を超えたら古い順に破棄する(queued/running は保護)。"""
        finished = [job for job in self._jobs.values() if job.status in _TERMINAL_STATUSES]
        overflow = len(finished) - _MAX_FINISHED_JOBS
        if overflow <= 0:
            return
        finished.sort(key=lambda job: (job.finished_at or job.created_at, job.id))
        for job in finished[:overflow]:
            del self._jobs[job.id]
            # business_text / Q/A 全文を保持する入力もあわせて破棄する(メモリリーク防止)
            self._inputs.pop(job.id, None)

    def _update(self, job_id: str, mutate: Any) -> None:
        persisted = self._runtime.store.get_document("jobs", {"job_id": job_id})
        if persisted is not None and persisted.get("status") == OntologyBuildStatus.CANCELLED.value:
            cancelled = OntologyBuildJob.model_validate(persisted["payload"])
            with self._lock:
                self._jobs[job_id] = cancelled
            return
        updated: OntologyBuildJob | None = None
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                mutate(job)
                updated = job.model_copy(deep=True)
        if updated is not None:
            self._persist_job(updated)

    def _update_persisted_copy(self, job_id: str, mutate: Any) -> None:
        persisted = self._runtime.store.get_document("jobs", {"job_id": job_id})
        if persisted is not None and persisted.get("status") == OntologyBuildStatus.CANCELLED.value:
            cancelled = OntologyBuildJob.model_validate(persisted["payload"])
            with self._lock:
                self._jobs[job_id] = cancelled
            return
        updated: OntologyBuildJob | None = None
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                updated = job.model_copy(deep=True)
        if updated is None:
            return
        mutate(updated)
        self._persist_job(updated)
        with self._lock:
            self._jobs[job_id] = updated

    def _normalize_job_for_response(self, job: OntologyBuildJob) -> OntologyBuildJob:
        normalized = job.model_copy(deep=True)
        if normalized.status not in {
            OntologyBuildStatus.SUCCEEDED,
            OntologyBuildStatus.SUCCEEDED_WITH_WARNINGS,
        } or not (
            normalized.draft_revision_id or normalized.draft_etag or normalized.markdown_output
        ):
            return normalized
        non_terminal = [
            step for step in normalized.steps if step.status not in _TERMINAL_STEP_STATUSES
        ]
        if (
            len(non_terminal) != 1
            or non_terminal[0].name != OntologyBuildStepName.PROPOSAL_REGISTRATION
        ):
            return normalized
        step = non_terminal[0]
        step.status = OntologyBuildStepStatus.SUCCEEDED
        # code=FINALIZING は「完了状態の保存中」を表す機械可読コード。文言一致は旧データ互換。
        legacy_finalizing = "構築 job の完了状態" in step.detail_ja
        if not step.detail_ja or step.code == "FINALIZING" or legacy_finalizing:
            step.detail_ja = "Markdown 下書きを生成しました。"
            step.code = ""
        step.finished_at = normalized.finished_at or step.finished_at or step.started_at
        return normalized

    def _persist_job(self, job: OntologyBuildJob) -> None:
        # read(etag) → save の間に別 thread(worker の進捗更新 vs API の cancel)が
        # 書き込むと etag conflict で状態が分岐する。専用 lock で直列化し、conflict 時は
        # etag を読み直して再試行する。persisted が CANCELLED になっていたら cancel を優先する。
        with self._persist_lock:
            for attempt in range(3):
                current = self._recover_document(self._runtime.store.get_job(job.id))
                if (
                    current is not None
                    and current.get("status") == OntologyBuildStatus.CANCELLED.value
                    and job.status != OntologyBuildStatus.CANCELLED
                ):
                    cancelled = OntologyBuildJob.model_validate(current["payload"])
                    with self._lock:
                        self._jobs[job.id] = cancelled
                    return
                with self._lock:
                    token = self._execution_ids.get(job.id)
                    input_payload = self._inputs.get(job.id)
                if current and current.get("execution_id") and current["execution_id"] != token:
                    raise OntologyBuildExecutionLost(job.id)
                if input_payload is None and current is not None:
                    current_input = current.get("input_payload")
                    input_payload = dict(current_input) if isinstance(current_input, dict) else {}
                try:
                    self._runtime.store.save_document(
                        "jobs",
                        {
                            **(current or {}),
                            "deadline_at": (current or {}).get("deadline_at")
                            or (
                                job.created_at
                                + timedelta(
                                    seconds=get_settings().nl2sql_ontology_build_timeout_seconds
                                )
                            ).isoformat(),
                            "dispatch_mode": (current or {}).get(
                                "dispatch_mode", get_settings().nl2sql_ontology_worker_mode
                            ),
                            "job_id": job.id,
                            "job_type": "build",
                            "profile_id": job.profile_id,
                            "status": job.status.value,
                            "payload": job.model_dump(mode="json"),
                            "input_payload": input_payload or {},
                            **(
                                {
                                    "claimed_by": current.get("claimed_by"),
                                    "claimed_at": time.time(),
                                }
                                if current is not None and current.get("claimed_by")
                                else {}
                            ),
                        },
                        expected_etag=str(current["etag"]) if current is not None else None,
                    )
                    if job.status in _TERMINAL_STATUSES:
                        self._release_profile_job_lock(job.profile_id, job.id)
                    return
                except OntologyVersionConflict:
                    if attempt == 2:
                        raise

    def _save_source_document(
        self,
        source: OntologySourceDocument,
        *,
        create: bool = True,
    ) -> None:
        current = self._runtime.store.get_document(
            "source_documents", {"source_document_id": source.id}
        )
        if current is None and not create:
            # 実行中に利用者が資料を削除した場合、進捗更新で行を復活させない
            raise OntologySourceDocumentGone(source.id)
        self._runtime.store.save_document(
            "source_documents",
            {
                "source_document_id": source.id,
                "profile_id": source.profile_id,
                "status": source.status.value,
                "sha256": source.sha256,
                "payload": source.model_dump(mode="json"),
            },
            expected_etag=str(current["etag"]) if current is not None else None,
        )

    def _get_source_document(
        self,
        source_id: str,
        *,
        profile_id: str = "",
    ) -> OntologySourceDocument:
        document = self._runtime.store.get_document(
            "source_documents", {"source_document_id": source_id}
        )
        if document is None:
            raise OntologySourceDocumentGone(source_id)
        source = OntologySourceDocument.model_validate(document["payload"])
        if profile_id and source.profile_id != profile_id:
            raise RuntimeError(
                f"Ontology source document の業務プロファイルが構築 job と一致しません: {source_id}"
            )
        return source

    def _update_source_document(
        self,
        source: OntologySourceDocument,
        **updates: Any,
    ) -> OntologySourceDocument:
        updated = source.model_copy(update={**updates, "updated_at": utc_now()}, deep=True)
        # job 実行中の状態更新は既存行だけを対象にする(削除済み資料を復活させない)
        self._save_source_document(updated, create=False)
        return updated

    def _update_source_progress(
        self,
        job_id: str,
        source_id: str,
        **updates: Any,
    ) -> None:
        def mutate(job: OntologyBuildJob) -> None:
            for index, progress in enumerate(job.sources):
                if progress.source_document_id == source_id:
                    job.sources[index] = progress.model_copy(update=updates, deep=True)
                    break

        self._update(job_id, mutate)

    def _set_definition_phase(self, job_id: str, name: str, status: str, detail: str = "") -> None:
        def mutate(job: OntologyBuildJob) -> None:
            if not any(p.name == name for p in job.definition_phases):
                job.definition_phases.append(DefinitionPhase(name=name))
            for phase in job.definition_phases:
                if phase.name == name:
                    phase.status = status  # type: ignore[assignment]
                    phase.detail_ja = detail
                    if status == "running" and not phase.started_at:
                        phase.started_at = utc_now().isoformat()
                    if status in {"succeeded", "failed", "skipped"}:
                        phase.finished_at = utc_now().isoformat()

        self._update(job_id, mutate)

    def _set_step(
        self,
        job_id: str,
        name: OntologyBuildStepName,
        status: OntologyBuildStepStatus,
        detail_ja: str = "",
        *,
        code: str = "",
    ) -> None:
        now = utc_now()

        def mutate(job: OntologyBuildJob) -> None:
            for step in job.steps:
                if step.name == name:
                    step.status = status
                    if detail_ja:
                        step.detail_ja = detail_ja
                    if code:
                        step.code = code
                    if status == OntologyBuildStepStatus.RUNNING and step.started_at is None:
                        step.started_at = now
                    if status in {
                        OntologyBuildStepStatus.SUCCEEDED,
                        OntologyBuildStepStatus.FAILED,
                        OntologyBuildStepStatus.SKIPPED,
                    }:
                        step.finished_at = now

        self._update(job_id, mutate)

    def _emit(
        self,
        job_id: str,
        message_ja: str,
        *,
        code: str = "",
        step: OntologyBuildStepName | None = None,
        phase: OntologyBuildPhase | None = None,
    ) -> None:
        """アクティビティタイムラインへ 1 行追記する(上限超過は古い順に間引く)。"""

        event = OntologyBuildEvent(message_ja=message_ja, code=code, step=step, phase=phase)

        def mutate(job: OntologyBuildJob) -> None:
            if event.phase is None:
                event.phase = _running_event_phase(job.definition_phases)
            # 時刻は lock の中で追記の直前に付ける。並行の工程（#2004）が作った順と追記の順は
            # 入れ替わることがあり、作った時刻のままだとタイムラインが時系列にならない（#2286）。
            event.at = utc_now()
            job.events.append(event)
            if len(job.events) > _MAX_JOB_EVENTS:
                del job.events[: len(job.events) - _MAX_JOB_EVENTS]

        self._update(job_id, mutate)

    def _add_warnings(self, job_id: str, warnings: list[str]) -> None:
        if not warnings:
            return

        def mutate(job: OntologyBuildJob) -> None:
            job.warnings_ja = [*job.warnings_ja, *warnings]

        self._update(job_id, mutate)

    def _generate_extraction(self, client: Any, task: _OntologyBuildLlmTask) -> str:
        """抽出系呼び出し。出力トークン予算に対応するクライアントには予算を渡す。"""

        budget = int(get_settings().nl2sql_ontology_extraction_max_output_tokens)
        try:
            supports_budget = "max_output_tokens" in inspect.signature(client.generate).parameters
        except (TypeError, ValueError):
            supports_budget = False
        if supports_budget and budget > 0:
            return str(
                call_with_model_purpose(
                    "ontology.build",
                    client.generate,
                    prompt=task.prompt,
                    context=task.context,
                    system_prompt=_EXTRACTION_SYSTEM_PROMPT,
                    response_format=response_format(OntologyConceptExtraction),
                    max_output_tokens=budget,
                )
            )
        return str(
            call_with_model_purpose(
                "ontology.build",
                client.generate,
                prompt=task.prompt,
                context=task.context,
                system_prompt=_EXTRACTION_SYSTEM_PROMPT,
                response_format=response_format(OntologyConceptExtraction),
            )
        )

    def _glean_extraction(
        self,
        job_id: str,
        client: Any,
        task: _OntologyBuildLlmTask,
        extraction: OntologyBuildExtraction,
        *,
        label: str,
    ) -> OntologyBuildExtraction:
        """GraphRAG 流 gleaning: 抽出済み候補の一覧を提示し、見逃した候補だけを
        追加パスで回収する。失敗・追加なし・入力上限超過は静かに打ち切る。"""

        passes = max(0, int(get_settings().nl2sql_ontology_extraction_gleaning_passes))
        if passes == 0 or task.name not in {
            OntologyBuildStepName.SCHEMA_NAMING,
            OntologyBuildStepName.QA_EXTRACTION,
            OntologyBuildStepName.TEXT_EXTRACTION,
        }:
            return extraction
        current = extraction
        for pass_index in range(1, passes + 1):
            if self._is_cancelled(job_id):
                return current
            seen_summary = json.dumps(
                {
                    "抽出済みエンティティ": [item.object_name for item in current.entities],
                    "抽出済み関係": [
                        f"{item.source_object}->{item.target_object}"
                        for item in current.relationships
                    ],
                    "抽出済み定義": [item.model_dump(mode="json") for item in current.definitions],
                    "抽出済み指標": [item.metric_name_ja for item in current.metrics],
                    "抽出済み同義語": [item.target for item in current.synonyms],
                },
                ensure_ascii=False,
            )
            glean_prompt = (
                f"{task.prompt}\n\n【追加パス {pass_index}】前回までに抽出済みの候補は"
                f"次のとおりです: {seen_summary} 。多くの候補が見逃されている可能性が"
                "あります。未抽出候補と、根拠を確認できた不足フィールドの補完だけを返してください。"
                "補完対象だけを同じ id/api_name と既存の値を保って返し、"
                "全定義を再生成しないでください。"
                "追加・補完が無ければ配列がすべて空の JSON を返してください。"
            )
            if _llm_call_chars(glean_prompt, task.context) > _ONTOLOGY_BUILD_LLM_CONTEXT_MAX_CHARS:
                return current
            glean_task = _OntologyBuildLlmTask(
                name=task.name,
                prompt=glean_prompt,
                context=task.context,
                progress_ja=task.progress_ja,
            )
            try:
                addition = parse_extraction(self._generate_extraction(client, glean_task))
            except BoundedCallAborted:
                raise
            except Exception:
                logger.warning(
                    "ontology_build_gleaning_failed step=%s pass=%s",
                    task.name.value,
                    pass_index,
                    exc_info=True,
                )
                return current
            merged, added = merge_build_extractions(current, addition)
            if added == 0:
                return merged
            current = merged
            self._emit(
                job_id,
                f"{label}: 追加パスで候補 {added} 件を回収しました。",
                code="EXTRACTION_GLEANING",
                step=task.name,
            )
        return current

    def _execute_llm_task(
        self,
        job_id: str,
        client: Any,
        task: _OntologyBuildLlmTask,
        *,
        label: str,
        depth: int = 0,
    ) -> tuple[list[_ValidatedBuildExtraction], list[str]]:
        """1 タスクを実行する。失敗時は 1 回再試行し、なお失敗する batch は二分割して
        再帰する(最小 1 件まで)。最終的に失敗した分は warnings として返し、
        ジョブ全体は止めない。戻り値は (検証済み抽出, 警告)。"""

        if self._is_cancelled(job_id):
            return [], []
        # 補充用の参照一覧も入力予算に含め、送信する前に再分割する。
        if _llm_call_chars(task.prompt, task.context) > _ONTOLOGY_BUILD_LLM_CONTEXT_MAX_CHARS:
            children = task.split()
            if task.text_batch is not None and task.schema_payload is not None:
                children = [
                    task.child(
                        _dump_text_context(task.schema_payload, batch),
                        text_batch=batch,
                        source_evidence=[e for unit in batch if (e := unit.evidence())],
                    )
                    for batch in _batch_text_units(
                        task.schema_payload, task.text_batch, task.prompt
                    )
                ]
            if not children or depth >= 6:
                return [], [
                    f"{label}: 入力上限を超えました。資料または対象範囲を分割してください。"
                ]
            results: list[_ValidatedBuildExtraction] = []
            messages: list[str] = []
            for child in children:
                extracted, warnings = self._execute_llm_task(
                    job_id, client, child, label=label, depth=depth + 1
                )
                results.extend(extracted)
                messages.extend(warnings)
            return results, messages
        job = self.get(job_id)
        checkpoint_id = stable_ontology_id(
            "ontology_batch", job_id, task.name.value, task.prompt, task.context
        )
        checkpoint = self._runtime.store.get_artifact(checkpoint_id)
        if checkpoint is None and job is not None:
            with self._lock:
                prior_ids = list(self._inputs.get(job_id, {}).get("checkpoint_job_ids", []))
            for prior_id in prior_ids:
                prior = self._job_from_document(self._runtime.store.get_job(prior_id))
                if (
                    not prior
                    or prior.profile_id != job.profile_id
                    or prior.profile_fingerprint != job.profile_fingerprint
                ):
                    continue
                previous_id = stable_ontology_id(
                    "ontology_batch", prior_id, task.name.value, task.prompt, task.context
                )
                checkpoint = self._runtime.store.get_artifact(previous_id)
                if checkpoint is not None:
                    break
        if (
            checkpoint is not None
            and job is not None
            and checkpoint.get("profile_id") == job.profile_id
        ):
            extraction = OntologyBuildExtraction.model_validate_json(checkpoint["content"])
            self._emit(
                job_id,
                f"{label}: 保存済み抽出を再利用しました。",
                code="BATCH_RESTORED",
                step=task.name,
            )
            return [
                _ValidatedBuildExtraction(
                    task.name, label, extraction, task.cross_check_sql, task.source_evidence
                )
            ], []
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                raw = self._generate_extraction(client, task)
                extraction = parse_extraction(raw)
                if depth == 0 and task.definition_phase == "concepts":
                    # 統合抽出の取りこぼし回収。再試行の子バッチでは重複実行しない。
                    extraction = self._glean_extraction(
                        job_id, client, task, extraction, label=label
                    )
                if self._is_cancelled(job_id):
                    return [], []
                # 業務プロファイルの削除と競合したら、checkpoint を残さない（削除の後に孤立の
                # 成果物を作らない。#1743）。
                if job is not None and self._profile_exists(job.profile_id):
                    self._runtime.store.save_artifact(
                        {
                            "artifact_id": checkpoint_id,
                            "session_id": f"profile-ontology:{job.profile_id}",
                            "profile_id": job.profile_id,
                            "artifact_type": "ontology_build_checkpoint",
                            "content": extraction.model_dump_json(),
                            "content_hash": definition_fingerprint(
                                extraction.model_dump(mode="json")
                            ),
                        }
                    )
                return (
                    [
                        _ValidatedBuildExtraction(
                            name=task.name,
                            label_ja=label,
                            extraction=extraction,
                            cross_check_sql=task.cross_check_sql,
                            source_evidence=task.source_evidence,
                        )
                    ],
                    [],
                )
            except BoundedCallAborted:
                raise
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "ontology_build_task_attempt_failed step=%s attempt=%s depth=%s",
                    task.name.value,
                    attempt + 1,
                    depth,
                    exc_info=True,
                )
                if attempt == 0:
                    self._emit(
                        job_id,
                        f"{label}: 抽出に失敗したため再試行します。",
                        code="EXTRACTION_RETRY",
                        step=task.name,
                    )
        halves = task.split()
        if halves and depth < 6:
            self._emit(
                job_id,
                f"{label}: batch を分割して再実行します。",
                code="EXTRACTION_SPLIT",
                step=task.name,
            )
            extractions: list[_ValidatedBuildExtraction] = []
            half_warnings: list[str] = []
            for half in halves:
                half_extractions, half_warns = self._execute_llm_task(
                    job_id, client, half, label=label, depth=depth + 1
                )
                extractions.extend(half_extractions)
                half_warnings.extend(half_warns)
            return extractions, half_warnings
        return [], [f"{task.name.value} の抽出に失敗しました: {last_error}"]

    def _run_safely(self, job_id: str, business_text: str, qa_pairs: list[QaPair]) -> None:
        token = self._claim_execution(job_id)
        if token is None:
            return
        stop = threading.Event()

        def renew() -> None:
            interval = min(30.0, get_settings().nl2sql_ontology_build_lease_seconds / 3)
            while not stop.wait(interval):
                try:
                    if not self._heartbeat(job_id, token):
                        return
                except Exception as exc:
                    logger.warning(
                        "構築 heartbeat の保存に失敗しました。期限失効後は結果を破棄します。",
                        extra={"job_id": job_id, "error_type": type(exc).__name__},
                    )

        heartbeat = threading.Thread(target=renew, daemon=True, name=f"{job_id}-heartbeat")
        try:
            heartbeat.start()
            # job の中の LLM の呼び出しは、上限つきの並行の実行器を共有する（#2004）。
            with sync_ai_caller(get_settings(), "ontology_build") as bridge:
                with self._lock:
                    self._ai_bridges[job_id] = bridge
                try:
                    self._run(job_id, business_text, qa_pairs)
                finally:
                    with self._lock:
                        self._ai_bridges.pop(job_id, None)
        except OntologyBuildExecutionLost:
            logger.info("失効した構築処理を停止しました。", extra={"job_id": job_id})
        except BoundedCallAborted as exc:
            # 権限・認証・モデルの設定の誤り・割り当ての使い切り。同じ失敗をくり返さないよう
            # 残りの呼び出しをせずに止める（#2004）。
            logger.warning(
                "ontology_build_ai_calls_aborted",
                extra={"job_id": job_id, "error_kind": exc.kind.value},
            )
            current = self.get(job_id)
            if current is not None and current.status == OntologyBuildStatus.CANCELLED:
                return
            self._fail(job_id, str(exc), error_code="LLM_CALL_ABORTED")
        except Exception as exc:  # pragma: no cover - 予期しない障害の最終防壁
            logger.warning("ontology_build_job_failed", exc_info=True)
            current = self.get(job_id)
            if current is not None and current.status == OntologyBuildStatus.CANCELLED:
                return
            message = f"オントロジー構築に失敗しました: {exc}"
            now = utc_now()

            def mutate(job: OntologyBuildJob) -> None:
                # _fail と同じく running/pending の step・phase も終端化する
                self._mark_failed(job, message, now)

            self._update(job_id, mutate)
            record_job(job_type="build", status="failed", error_code="unexpected")
        finally:
            stop.set()
            with self._lock:
                if self._execution_ids.get(job_id) == token:
                    self._execution_ids.pop(job_id, None)

    def _fail(
        self,
        job_id: str,
        message_ja: str,
        *,
        skip_pending_steps: bool = True,
        failed_step: OntologyBuildStepName | None = None,
        failed_step_detail_ja: str = "",
        error_code: str = "",
    ) -> None:
        now = utc_now()

        def mutate(job: OntologyBuildJob) -> None:
            self._mark_failed(
                job,
                message_ja,
                now,
                skip_pending_steps=skip_pending_steps,
                failed_step=failed_step,
                failed_step_detail_ja=failed_step_detail_ja,
                error_code=error_code,
            )

        self._update(job_id, mutate)
        self._emit(job_id, message_ja)
        record_job(job_type="build", status="failed", error_code="build_failed")

    @staticmethod
    def _mark_failed(
        job: OntologyBuildJob,
        message_ja: str,
        now: datetime,
        *,
        skip_pending_steps: bool = True,
        failed_step: OntologyBuildStepName | None = None,
        failed_step_detail_ja: str = "",
        error_code: str = "",
    ) -> None:
        """job を FAILED に終端化し、running/pending の phase・step も閉じる。"""

        job.status = OntologyBuildStatus.FAILED
        job.error_message_ja = message_ja
        for phase in job.definition_phases:
            if phase.status == "running":
                phase.status = "failed"
                phase.detail_ja = message_ja
            elif phase.status == "pending":
                phase.status = "skipped"
        if error_code:
            job.error_code = error_code
        job.finished_at = now
        for step in job.steps:
            if failed_step is not None and step.name == failed_step:
                step.status = OntologyBuildStepStatus.FAILED
                if failed_step_detail_ja:
                    step.detail_ja = failed_step_detail_ja
                if error_code:
                    step.code = error_code
                if step.started_at is None:
                    step.started_at = now
                step.finished_at = now
                continue
            if skip_pending_steps and step.status in {
                OntologyBuildStepStatus.PENDING,
                OntologyBuildStepStatus.RUNNING,
            }:
                step.status = OntologyBuildStepStatus.SKIPPED
                step.finished_at = now

    def _profile_exists(self, profile_id: str) -> bool:
        """業務プロファイルがまだあるか（削除済みなら False。読めないときは True のまま続ける）。"""

        try:
            self._runtime.ensure_profile(profile_id)
        except OntologyNotFoundError:
            return False
        return True

    def _record_build_removals(self, profile_id: str, job_id: str, removals: list[str]) -> None:
        """今回の構築で外したものを、次の構築の直すべき点に残す（残せなくても構築は続ける。#1742）。"""

        from .ontology_markdown_workspace import MarkdownOntologyWorkspace

        try:
            MarkdownOntologyWorkspace(self._runtime).record_build_removals(
                profile_id, job_id, removals
            )
        except Exception as exc:
            logger.warning(
                "構築で外したものを次の構築に残せませんでした。",
                extra={"profile_id": profile_id, "error_type": type(exc).__name__},
            )

    def _prior_build_context(self, profile_id: str) -> dict[str, Any] | None:
        """前の定義の要約と前の確認の直すべき点（読めなければ無しで構築を続ける）。"""

        from .ontology_markdown_workspace import MarkdownOntologyWorkspace

        try:
            return MarkdownOntologyWorkspace(self._runtime).prior_build_context(profile_id)
        except Exception as exc:
            logger.warning(
                "前の定義を読めなかったため、前の定義なしで AI 構築を続けます。",
                extra={"profile_id": profile_id, "error_type": type(exc).__name__},
            )
            return None

    def _run(self, job_id: str, business_text: str, qa_pairs: list[QaPair]) -> None:
        if self._is_cancelled(job_id):
            return

        def start_job(job: OntologyBuildJob) -> None:
            job.status = OntologyBuildStatus.RUNNING
            job.started_at = utc_now()

        self._update(job_id, start_job)
        job = self.get(job_id)
        if job is None:
            return
        self._emit(job_id, "AI オントロジー構築を開始しました。")
        self._set_definition_phase(job_id, "freeze", "running")
        if job.profile_fingerprint and job.profile_fingerprint != definition_fingerprint(
            self._runtime._strict_profile(job.profile_id).model_dump(mode="json")
        ):
            self._set_definition_phase(
                job_id, "freeze", "failed", "Profile が受付時から変更されました。"
            )
            self._fail(
                job_id,
                "業務プロファイルの範囲が変更されたため再構築してください。",
                error_code="PROFILE_SCOPE_CHANGED",
            )
            return
        self._set_definition_phase(job_id, "evidence", "running")

        client = getattr(self._runtime.legacy_service, "_enterprise_ai_client", None)
        with self._lock:
            bridge = self._ai_bridges.get(job_id)
        if client is not None and bridge is not None:
            client = BoundedEnterpriseAiClient(client, bridge)
        configured = getattr(client, "is_configured", None)
        text_units: list[_BuildTextUnit] = []
        if business_text.strip():
            text_units.append(
                _BuildTextUnit(
                    source_document=None,
                    source_label="業務説明(入力フォーム)",
                    locator_kind=OntologyEvidenceLocatorKind.LINE,
                    locator="manual:1",
                    text=business_text.strip(),
                )
            )
        source_evidence_count = 0
        # 部分成功の対象ステップ(資料抽出・LLM 抽出)。finish で succeeded_with_warnings に反映する
        partial_failed_steps: set[OntologyBuildStepName] = set()
        failed_source_names: list[str] = []
        failed_source_errors: list[str] = []
        if job.source_document_ids:
            self._set_step(
                job_id,
                OntologyBuildStepName.SOURCE_EXTRACTION,
                OntologyBuildStepStatus.RUNNING,
                f"資料 {len(job.source_document_ids)} 件を抽出中…",
            )
            extracted_pairs: list[QaPair] = []
            seen_hashes: set[str] = set()
            source_filenames = {
                progress.source_document_id: progress.filename for progress in job.sources
            }
            for source_id in job.source_document_ids:
                # 資料の取得・状態更新も含めて 1 資料単位で失敗を閉じ込める
                # (実行中に資料が削除されてもジョブ全体は止めない)
                source: OntologySourceDocument | None = None
                try:
                    source = self._get_source_document(source_id, profile_id=job.profile_id)
                    if source.sha256 in seen_hashes:
                        warning = f"{source.filename}: 同一内容の資料は 1 回だけ利用します。"
                        self._update_source_progress(
                            job_id,
                            source.id,
                            status=OntologySourceStatus.EXTRACTED,
                            warnings_ja=[warning],
                        )
                        record_source_extraction(
                            file_format=Path(source.filename).suffix, status="duplicate"
                        )
                        continue
                    seen_hashes.add(source.sha256)
                    self._update_source_progress(
                        job_id, source.id, status=OntologySourceStatus.EXTRACTING
                    )
                    source = self._update_source_document(
                        source, status=OntologySourceStatus.EXTRACTING
                    )
                    image_runner = None
                    generate_image = getattr(client, "generate_from_image", None)
                    if callable(generate_image) and bridge is not None:
                        generate_image = image_call(generate_image, bridge)
                    if callable(configured) and configured() and callable(generate_image):

                        def image_runner(
                            image: bytes,
                            page: int,
                            _generate_image: Any = generate_image,
                        ) -> str:
                            return TextOutput.model_validate_json(
                                call_with_model_purpose(
                                    "document.read",
                                    _generate_image,
                                    image,
                                    f"この資料の {page} ページ目を日本語で正確に"
                                    "文字起こししてください。",
                                    mime_type="image/jpeg",
                                    response_format=response_format(TextOutput),
                                )
                            ).text

                    extracted = extract_ontology_source(
                        source,
                        self._source_storage.load(source),
                        vlm_page_runner=image_runner,
                        vlm_concurrency=stage_concurrency(get_settings(), "ontology_build"),
                    )
                    if not extracted.chunks and not extracted.qa_pairs:
                        raise RuntimeError(
                            f"{source.filename}: 抽出可能なテキストまたは Q/A がありません。"
                            + (
                                f"({' / '.join(extracted.warnings_ja)})"
                                if extracted.warnings_ja
                                else ""
                            )
                        )
                    # 先に資料の状態を保存する(抽出中に削除されていたらここで失敗扱いにし、
                    # 削除済み資料の証拠を構築へ混ぜない)
                    extracting_source = source
                    source = self._update_source_document(
                        source,
                        status=OntologySourceStatus.EXTRACTED,
                        extracted_chunk_count=len(extracted.chunks),
                        warnings_ja=extracted.warnings_ja,
                    )
                    if extracted.warnings_ja:
                        # ページ単位のスキップ等は警告として残し、資料全体は成功扱いにする
                        self._add_warnings(
                            job_id,
                            [f"{source.filename}: {warning}" for warning in extracted.warnings_ja],
                        )
                    for chunk in extracted.chunks:
                        if not chunk.text.strip():
                            continue
                        text_units.append(
                            _BuildTextUnit(
                                source_document=extracting_source,
                                source_label=source.filename,
                                locator_kind=chunk.locator_kind,
                                locator=chunk.locator,
                                text=chunk.text,
                            )
                        )
                    source_evidence_count += len(extracted.chunks)
                    extracted_pairs.extend(extracted.qa_pairs)
                    self._update_source_progress(
                        job_id,
                        source.id,
                        status=OntologySourceStatus.EXTRACTED,
                        extracted_chunk_count=len(extracted.chunks),
                        warnings_ja=extracted.warnings_ja,
                    )
                    record_source_extraction(
                        file_format=Path(source.filename).suffix, status="extracted"
                    )
                except (OntologyBuildExecutionLost, BoundedCallAborted):
                    raise
                except Exception as exc:
                    source_name = (
                        source.filename
                        if source is not None
                        else source_filenames.get(source_id) or source_id
                    )
                    error_message = f"{source_name}: {exc}"
                    if source is not None and not isinstance(exc, OntologySourceDocumentGone):
                        try:
                            source = self._update_source_document(
                                source,
                                status=OntologySourceStatus.FAILED,
                                error_message_ja=error_message,
                            )
                        except Exception:
                            # 失敗記録の直前に削除された資料は行を復活させない。
                            # 記録自体の失敗でも残りの資料の処理は継続する
                            logger.warning(
                                "ontology_source_failure_record_failed",
                                exc_info=True,
                                extra={"job_id": job_id, "source_document_id": source_id},
                            )
                    self._update_source_progress(
                        job_id,
                        source_id,
                        status=OntologySourceStatus.FAILED,
                        error_message_ja=error_message,
                    )
                    record_source_extraction(file_format=Path(source_name).suffix, status="failed")
                    # 1 資料の失敗でジョブ全体を止めず、警告として残りの入力で継続する
                    failed_source_names.append(source_name)
                    failed_source_errors.append(error_message)
                    self._add_warnings(job_id, [error_message])
                    self._emit(
                        job_id,
                        f"{source_name}: 抽出に失敗しました(他の入力で処理を継続します)。",
                        code="SOURCE_EXTRACTION_FAILED",
                        step=OntologyBuildStepName.SOURCE_EXTRACTION,
                    )
                    continue
            qa_by_key = {(item.question, item.sql): item for item in [*qa_pairs, *extracted_pairs]}
            qa_pairs = list(qa_by_key.values())
            extracted_source_count = len(job.source_document_ids) - len(failed_source_names)
            if failed_source_names:
                partial_failed_steps.add(OntologyBuildStepName.SOURCE_EXTRACTION)
            self._set_step(
                job_id,
                OntologyBuildStepName.SOURCE_EXTRACTION,
                (
                    OntologyBuildStepStatus.FAILED
                    if extracted_source_count == 0
                    else OntologyBuildStepStatus.SUCCEEDED
                ),
                f"資料 {extracted_source_count}/{len(job.source_document_ids)} 件、"
                f"証拠 {source_evidence_count} 件、Q/A {len(extracted_pairs)} 件を抽出しました。"
                + (f" 失敗 {len(failed_source_names)} 件。" if failed_source_names else ""),
                code="SOURCE_PARTIAL_FAILED" if failed_source_names else "",
            )
            self._emit(job_id, "資料の抽出と証拠位置の記録が完了しました。")
            if extracted_source_count == 0 and failed_source_names:
                # 全資料が失敗し、他に入力(業務説明・Q/A・schema 命名)が無ければ
                # 継続しても成果が出ないためジョブを失敗させる
                has_other_inputs = (
                    bool(business_text.strip())
                    or bool(qa_pairs)
                    or OntologyBuildStepName.SCHEMA_NAMING in {step.name for step in job.steps}
                )
                if not has_other_inputs:
                    self._fail(
                        job_id,
                        "すべての資料の抽出に失敗しました。 " + " / ".join(failed_source_errors),
                        failed_step=OntologyBuildStepName.SOURCE_EXTRACTION,
                        failed_step_detail_ja="資料の抽出に失敗しました。",
                        error_code="SOURCE_EXTRACTION_FAILED",
                    )
                    return
        if client is None or not callable(configured) or not configured():
            self._fail(
                job_id,
                "モデルの API が未設定のため、AI オントロジー構築を実行できません。",
                error_code="LLM_UNCONFIGURED",
            )
            return

        # --- スキーマ情報の準備(DB catalog 直読みによる AI input 作成) ---
        self._set_step(
            job_id,
            OntologyBuildStepName.SCHEMA_CONTEXT,
            OntologyBuildStepStatus.RUNNING,
            "DB から profile 範囲のスキーマ情報を取得中…",
        )
        self._emit(job_id, "DB から業務プロファイル範囲のスキーマ情報を取得しています。")
        prepared_schema = self._runtime.prepare_build_schema_context(job.profile_id)
        schema_warnings = list(prepared_schema.warnings)
        schema_errors = list(prepared_schema.errors)
        object_count = int(prepared_schema.object_count)
        column_count = int(prepared_schema.column_count)
        for warning in schema_warnings:
            self._emit(job_id, warning)
        for error in schema_errors:
            self._emit(job_id, error)
        if object_count == 0 or schema_errors:
            # LLM を無駄撃ちせず、原因(schema 情報未解決)を明確に返す
            scope_code = "SCHEMA_SCOPE_AMBIGUOUS" if schema_errors else "SCHEMA_SCOPE_EMPTY"
            self._set_step(
                job_id,
                OntologyBuildStepName.SCHEMA_CONTEXT,
                OntologyBuildStepStatus.FAILED,
                (
                    "profile 範囲の DB object が曖昧です。"
                    if schema_errors
                    else "profile 範囲に DB 表・ビューがありません。"
                ),
                code=scope_code,
            )
            self._add_warnings(job_id, [*schema_warnings, *schema_errors])
            unresolved_hint = (
                f" 詳細: {' / '.join([*schema_errors, *schema_warnings])}"
                if schema_errors or schema_warnings
                else ""
            )
            self._fail(
                job_id,
                "業務プロファイルの対象オブジェクトを DB schema catalog に解決できません。"
                "DB 構造を再取得するか、業務プロファイルの対象 object を確認してから"
                "再実行してください。"
                f"{unresolved_hint}",
                error_code="SCHEMA_SCOPE_UNRESOLVED",
            )
            return

        schema_context = str(prepared_schema.schema_context)
        context_fingerprint = definition_fingerprint(schema_context)
        if job.schema_context_fingerprint and job.schema_context_fingerprint != context_fingerprint:
            self._set_definition_phase(
                job_id, "freeze", "failed", "Schema が構築中に変更されました。"
            )
            self._fail(
                job_id,
                "Schema が変更されたため再構築してください。",
                error_code="SCHEMA_SCOPE_CHANGED",
            )
            return
        self._update(
            job_id, lambda value: setattr(value, "schema_context_fingerprint", context_fingerprint)
        )
        self._set_definition_phase(job_id, "freeze", "succeeded")
        self._set_definition_phase(job_id, "evidence", "succeeded")
        self._set_step(
            job_id,
            OntologyBuildStepName.SCHEMA_CONTEXT,
            OntologyBuildStepStatus.SUCCEEDED,
            f"表・ビュー {object_count} 件、列 {column_count} 件",
        )
        self._emit(
            job_id,
            f"スキーマ情報を準備しました(表・ビュー {object_count} 件、列 {column_count} 件)。",
            phase="evidence",
        )
        self._set_definition_phase(job_id, "concepts", "running")
        inferred_by = str(getattr(client, "model_id", lambda: "enterprise-ai")())
        qa_pairs = list({(item.question, item.sql): item for item in qa_pairs}.values())
        try:
            schema_payload = _schema_context_payload(schema_context)
        except OntologyBuildContextBudgetError as exc:
            self._fail(
                job_id,
                str(exc),
                failed_step=OntologyBuildStepName.SCHEMA_CONTEXT,
                failed_step_detail_ja="LLM 入力用 schema_context の準備に失敗しました。",
            )
            return

        drafts: list[ProposalDraft] = []
        warnings: list[str] = [*schema_warnings]
        step_names = {step.name for step in job.steps}
        llm_tasks: list[_OntologyBuildLlmTask] = []
        validated_extractions: list[_ValidatedBuildExtraction] = []
        ontology: SchemaOntology | None = None
        if OntologyBuildStepName.SCHEMA_NAMING in step_names:
            prompt = (
                "schema_context を根拠に主要6種類・補助7種類の関連概念を"
                " definitions に抽出してください。"
                "各表・ビューの日本語の業務名・説明・同義語、オブジェクトごとの Property、"
                "制約で分かる主識別子、制約または同じ名前・型の ID 列で分かる関係を含め、"
                "根拠のない業務契約は創作しないでください。"
                "分からない項目は空のままにし、作れる範囲で graph を作ってください。"
            )
            try:
                _ensure_llm_call_fits(prompt, schema_context)
            except OntologyBuildContextBudgetError as exc:
                self._fail(
                    job_id,
                    str(exc),
                    failed_step=OntologyBuildStepName.SCHEMA_NAMING,
                    failed_step_detail_ja="スキーマからの概念抽出の LLM 入力が上限を超えています。",
                )
                return
            llm_tasks.append(
                _OntologyBuildLlmTask(
                    name=OntologyBuildStepName.SCHEMA_NAMING,
                    prompt=prompt,
                    context=schema_context,
                    progress_ja="スキーマから関連概念を構築中です。",
                )
            )
        if OntologyBuildStepName.QA_EXTRACTION in step_names:
            if not qa_pairs:
                self._set_step(
                    job_id,
                    OntologyBuildStepName.QA_EXTRACTION,
                    OntologyBuildStepStatus.SKIPPED,
                    "有効な Q/A 行がないためスキップしました。",
                )
            else:
                prompt = (
                    "入力 JSON の qa_pairs にある質問と正解 SQL から、実際に使われた "
                    "JOIN パスを Link Type に、業務指標を Metric にし、"
                    "根拠のある主要6種類・補助7種類を"
                    " definitions にまとめて抽出してください。"
                    "qa_pairs[].schema_resolved_columns と "
                    "qa_pairs[].schema_resolved_join_conditions にある "
                    "OWNER.OBJECT.COLUMN 形式の参照を正としてください。"
                    "SQL 内の table alias、CTE/inline view alias、出力 alias は "
                    "schema_context の object/column 名ではないため、"
                    "schema_unresolved_references に含まれない限り不存在警告にしないでください。"
                    "SQL に現れない関係を推測しないでください。"
                    "cardinality フィールドは schema_context の constraints"
                    "(主キー P / 一意 U)から判断してください"
                    "(JOIN 先の列が主キー・一意キーなら many_to_one など)。"
                )
                try:
                    qa_batches = _batch_qa_pairs(schema_payload, qa_pairs, prompt)
                except OntologyBuildContextBudgetError as exc:
                    self._fail(
                        job_id,
                        str(exc),
                        failed_step=OntologyBuildStepName.QA_EXTRACTION,
                        failed_step_detail_ja="Q/A の LLM 入力が上限を超えています。",
                    )
                    return
                processed = 0
                for batch_index, batch in enumerate(qa_batches, start=1):
                    processed += len(batch)
                    llm_tasks.append(
                        _OntologyBuildLlmTask(
                            name=OntologyBuildStepName.QA_EXTRACTION,
                            prompt=prompt,
                            context=_dump_qa_context(schema_payload, batch),
                            progress_ja=(
                                f"Q/A batch {batch_index}/{len(qa_batches)} を処理中"
                                f"({processed}/{len(qa_pairs)} 件)。"
                            ),
                            cross_check_sql=[pair.sql for pair in batch],
                            qa_batch=list(batch),
                            schema_payload=schema_payload,
                        )
                    )
        if OntologyBuildStepName.TEXT_EXTRACTION in step_names:
            if not text_units:
                self._set_step(
                    job_id,
                    OntologyBuildStepName.TEXT_EXTRACTION,
                    OntologyBuildStepStatus.SKIPPED,
                    "抽出できる業務説明がないためスキップしました。",
                )
            else:
                prompt = (
                    "入力 JSON の business_text_chunks をすべて読み、"
                    "根拠のある主要6種類・補助7種類を"
                    " definitions にまとめて抽出してください。同義語は各定義の aliases に含め、"
                    "名詞をエンティティ、"
                    "動詞・述語を関係の手がかりとして読み取り、schema_context に対応づかない"
                    "内容は coverage の理由または対象定義の missing_information_ja に"
                    "残してください。"
                )
                try:
                    text_batches = _batch_text_units(schema_payload, text_units, prompt)
                except OntologyBuildContextBudgetError as exc:
                    self._fail(
                        job_id,
                        str(exc),
                        failed_step=OntologyBuildStepName.TEXT_EXTRACTION,
                        failed_step_detail_ja="業務説明の LLM 入力が上限を超えています。",
                    )
                    return
                processed = 0
                total_units = sum(len(text_batch) for text_batch in text_batches)
                # 上の Q/A ループの batch(QaPair)と変数を共有しない(型の取り違え防止)。
                for text_batch_index, text_batch in enumerate(text_batches, start=1):
                    processed += len(text_batch)
                    llm_tasks.append(
                        _OntologyBuildLlmTask(
                            name=OntologyBuildStepName.TEXT_EXTRACTION,
                            prompt=prompt,
                            context=_dump_text_context(schema_payload, text_batch),
                            progress_ja=(
                                f"業務説明 chunk batch {text_batch_index}"
                                f"/{len(text_batches)} を処理中"
                                f"({processed}/{total_units} chunk)。"
                            ),
                            source_evidence=[
                                evidence
                                for unit in text_batch
                                if (evidence := unit.evidence()) is not None
                            ],
                            text_batch=list(text_batch),
                            schema_payload=schema_payload,
                        )
                    )

        definition_sources = [
            DefinitionSource(
                source_id=unit.source_document.id if unit.source_document else "manual",
                locator=unit.locator,
                kind="document" if unit.source_document else "manual",
                sha256=(
                    unit.source_document.sha256
                    if unit.source_document
                    else definition_fingerprint(unit.text)
                ),
                text=unit.text,
            )
            for task in llm_tasks
            for unit in task.text_batch or []
        ]
        for pair in qa_pairs:
            payload = _qa_pair_context_payload(schema_payload, pair)
            definition_sources.append(
                DefinitionSource(
                    source_id=str(payload["source_id"]),
                    locator="question_sql",
                    kind="qa",
                    sha256=definition_fingerprint(pair.model_dump(mode="json")),
                    text=pair.question + "\n" + pair.sql,
                )
            )
        for obj in schema_payload.get("objects", []):
            definition_sources.append(
                DefinitionSource(
                    source_id="schema",
                    locator=f"{obj.get('owner', '')}.{obj.get('object_name', '')}",
                    kind="database",
                    sha256=context_fingerprint,
                    text=canonical_json(obj),
                )
            )

        # 前の定義（下書き → 公開版 → 無し）の名前・物理の対応と、前の確認の直すべき点を
        # LLM の入力に入れる（名付けの揺れを抑え、前の問題を狙って直す。#1734）。
        prior = self._prior_build_context(job.profile_id)
        if prior:
            llm_tasks = [task.with_prior(prior) for task in llm_tasks]
            from .ontology_build_prior import REMOVAL_PREFIX

            findings = list(prior.get("previous_check_findings") or [])
            removed = sum(1 for item in findings if str(item).startswith(REMOVAL_PREFIX))
            self._emit(
                job_id,
                "前の定義の名前と、直すべき点（前回の構築で外したもの "
                f"{removed} 件・前回の公開前の確認 {len(findings) - removed} 件）"
                "を AI に渡します。",
                phase="concepts",
            )

        # 各タスクは 1 回再試行 → なお失敗なら batch を二分割して再帰する。
        # 一部 batch の失敗はジョブ全体を止めず warning として継続し、
        # 全タスク失敗のときだけジョブを FAILED にする(部分成功)。
        # partial_failed_steps は資料抽出ステップと共有(冒頭で初期化済み)。
        # タスクは上限つきの並行（NL2SQL_ONTOLOGY_BUILD_CONCURRENCY。#2004）で始め、結果は
        # タスクの順に取り込む（並行でも統合の順番は変わらない）。LLM の呼び出しは job の実行器を
        # 共有し、429 は全体で待って並行の数を下げる。
        def run_task(
            index: int, task: _OntologyBuildLlmTask
        ) -> tuple[list[_ValidatedBuildExtraction], list[str]] | None:
            if self._is_cancelled(job_id):
                return None
            label = _STEP_LABELS_JA[task.name]
            self._set_step(
                job_id,
                task.name,
                OntologyBuildStepStatus.RUNNING,
                task.progress_ja,
            )
            self._emit(
                job_id,
                f"Enterprise AI に問い合わせています({index}/{len(llm_tasks)}: {label})。"
                f"{task.progress_ja}",
                step=task.name,
            )
            return self._execute_llm_task(job_id, client, task, label=label)

        workers = max(1, min(stage_concurrency(get_settings(), "ontology_build"), len(llm_tasks)))
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ontology-build")
        futures: list[Future[tuple[list[_ValidatedBuildExtraction], list[str]] | None]] = [
            pool.submit(contextvars.copy_context().run, run_task, index, task)
            for index, task in enumerate(llm_tasks, start=1)
        ]
        try:
            for task, future in zip(llm_tasks, futures, strict=True):
                outcome = future.result()
                if outcome is None:
                    return
                label = _STEP_LABELS_JA[task.name]
                task_extractions, task_warnings = outcome
                validated_extractions.extend(task_extractions)
                if task_warnings:
                    partial_failed_steps.add(task.name)
                    warnings.extend(task_warnings)
                    self._add_warnings(job_id, task_warnings)
                    self._set_step(
                        job_id,
                        task.name,
                        (
                            OntologyBuildStepStatus.FAILED
                            if not task_extractions
                            else OntologyBuildStepStatus.RUNNING
                        ),
                        "一部の LLM 抽出に失敗しました(成功分で継続します)。",
                        code="BATCH_EXTRACTION_FAILED",
                    )
                    self._emit(
                        job_id,
                        f"{label}: 一部の抽出に失敗しました。成功分で処理を継続します。",
                        code="BATCH_EXTRACTION_FAILED",
                        step=task.name,
                    )
                if task_extractions:
                    self._set_step(
                        job_id,
                        task.name,
                        OntologyBuildStepStatus.SUCCEEDED,
                        "応答を検証しました。",
                    )
                    self._emit(job_id, f"{label}: 抽出候補を検証しました。", step=task.name)
        finally:
            # 取り消し・失敗で抜けたら、まだ始めていないタスクを呼ばない。
            for future in futures:
                future.cancel()
            pool.shutdown(wait=True)

        # 全13種類を入力バッチごとに一度生成する。旧形式の変換は保存時の互換経路のみ。
        self._set_definition_phase(
            job_id, "concepts", "succeeded" if validated_extractions else "failed"
        )
        self._set_definition_phase(job_id, "validation", "running")

        if llm_tasks and not validated_extractions:
            self._fail(
                job_id,
                "すべての LLM 抽出に失敗しました。"
                "時間をおいて再実行するか、資料を分割してください。",
                error_code="LLM_EXTRACTION_FAILED",
            )
            return

        if self._is_cancelled(job_id):
            return
        if validated_extractions:
            self._set_step(
                job_id,
                OntologyBuildStepName.PROPOSAL_REGISTRATION,
                OntologyBuildStepStatus.RUNNING,
                "DB schema revision を準備中…",
            )
            try:
                view, ontology = self._runtime.build_proposal_scope(
                    job.profile_id,
                    schema_fingerprint=str(prepared_schema.schema_fingerprint),
                )
            except (OntologyNotFoundError, OntologyStateConflictError, ValueError) as exc:
                message = getattr(exc, "message_ja", str(exc))
                self._set_step(
                    job_id,
                    OntologyBuildStepName.PROPOSAL_REGISTRATION,
                    OntologyBuildStepStatus.FAILED,
                    "DB schema revision の準備に失敗しました。",
                )
                self._fail(job_id, message)
                return
            from .ontology_build_grounding import ground_qa_links

            for validated in validated_extractions:
                # checkpoint の旧候補を読み込んだ場合も、型付き関係を統合前に検証する。
                grounded = ground_qa_links(
                    validated.extraction, validated.cross_check_sql, schema_payload
                )
                validated.extraction.definitions = grounded.definitions
                validated.extraction.warnings_ja = grounded.warnings_ja
                validated.extraction.coverage = grounded.coverage
                step_drafts, step_warnings = convert_extraction_to_proposals(
                    validated.extraction,
                    ontology=ontology,
                    view=view,
                    job_id=job_id,
                    inferred_by=inferred_by,
                    qa_sql_texts=validated.cross_check_sql,
                    source_evidence=validated.source_evidence,
                )
                drafts.extend(step_drafts)
                warnings.extend(step_warnings)
                actionable_step_warnings, step_rejections = _split_ontology_build_warnings(
                    step_warnings
                )
                step_result_counts = _build_result_counts_ja(
                    len(validated.extraction.definitions) or len(step_drafts),
                    warning_count=len(actionable_step_warnings),
                    rejected_count=len(step_rejections),
                )
                self._set_step(
                    job_id,
                    validated.name,
                    OntologyBuildStepStatus.SUCCEEDED,
                    step_result_counts,
                )
                self._emit(
                    job_id,
                    f"{validated.label_ja}: {step_result_counts}を整理しました。",
                    step=validated.name,
                    phase="concepts",
                )
        if ontology is None:
            self._set_step(
                job_id,
                OntologyBuildStepName.PROPOSAL_REGISTRATION,
                OntologyBuildStepStatus.RUNNING,
                "Markdown 下書き生成用の DB schema revision を準備中…",
            )
            try:
                view, ontology = self._runtime.build_proposal_scope(
                    job.profile_id,
                    schema_fingerprint=str(prepared_schema.schema_fingerprint),
                )
            except (OntologyNotFoundError, OntologyStateConflictError, ValueError) as exc:
                message = getattr(exc, "message_ja", str(exc))
                self._set_step(
                    job_id,
                    OntologyBuildStepName.PROPOSAL_REGISTRATION,
                    OntologyBuildStepStatus.FAILED,
                    "Markdown 下書き生成用の DB schema revision 準備に失敗しました。",
                )
                self._fail(job_id, message)
                return

        # Profile 削除と競合した場合に draft を生成しないため、書込直前に再確認する。
        self._runtime.ensure_profile(job.profile_id)
        self._set_step(
            job_id,
            OntologyBuildStepName.PROPOSAL_REGISTRATION,
            OntologyBuildStepStatus.RUNNING,
            f"候補 {len(drafts)} 件から Markdown 下書きを生成中…",
        )
        draft_inputs: list[ProposalDraft] = []
        # 同一 run 内で複数ステップが同じ候補を出した場合の dedup。provenance の
        # timestamp は揺れるため、安定 ID(node/edge)と kind で同一性を判定する。
        seen_payload_keys: set[str] = set()
        for draft in drafts:
            if self._is_cancelled(job_id):
                return
            payload_key = _proposal_payload_key(draft.kind.value, dict(draft.payload.values))
            if payload_key in seen_payload_keys:
                continue
            seen_payload_keys.add(payload_key)
            draft_inputs.append(draft)
            self._set_step(
                job_id,
                OntologyBuildStepName.PROPOSAL_REGISTRATION,
                OntologyBuildStepStatus.RUNNING,
                f"{len(draft_inputs)} 件を整理済み(候補 {len(drafts)} 件)",
            )
        self._set_step(
            job_id,
            OntologyBuildStepName.PROPOSAL_REGISTRATION,
            OntologyBuildStepStatus.RUNNING,
            "Markdown 下書きをレンダリング中…",
        )
        actionable_warnings, proposal_rejections = _split_ontology_build_warnings(warnings)
        content_notes = [
            message
            for message in _unique_non_empty_messages([*job.warnings_ja, *warnings])
            if _is_content_note(message)
        ]
        from .ontology_build_prior import plain_build_removals
        from .ontology_unified_model import (
            legacy_definitions,
            merge_definitions,
            render_concepts,
            repair_definitions_report,
            schema_resolved_conflicts,
            settle_grain_conflicts,
            settle_link_cardinalities,
        )

        candidate_request = self._runtime._proposal_payloads_upsert_draft_request(
            [draft.payload for draft in draft_inputs],
            ontology,
        )
        candidate_nodes = {n.id: n for n in ontology.nodes}
        candidate_edges = {e.id: e for e in ontology.edges}
        candidate_nodes.update({n.id: n for n in candidate_request.node_upserts})
        candidate_edges.update({e.id: e for e in candidate_request.edge_upserts})
        candidates = SchemaOntology(
            revision=ontology.revision,
            nodes=list(candidate_nodes.values()),
            edges=list(candidate_edges.values()),
        )
        # 前の定義（下書き・公開版の業務層）の名前。同じ列のプロパティ・同じ物の共有プロパティを
        # まとめるとき、AI の名付けの揺れで前の名前を上書きしない（#1734）。
        previous_definitions = legacy_definitions(ontology)
        previous_names = {d.api_name for d in previous_definitions}
        previous_ids = {d.id for d in previous_definitions if d.id}
        unified_definitions, concept_conflicts = merge_definitions(
            job.profile_id,
            [
                # 今回の AI の候補（前の定義に無いもの）の作った人は AI（#1734）。
                *[
                    d
                    if d.id in previous_ids or d.api_name in previous_names
                    else d.model_copy(update={"origin": "ai"})
                    for d in legacy_definitions(candidates)
                ],
                # AI の出力の「作った人」は常に AI（LLM が書いた origin は使わない。#1734）。
                *[
                    d.model_copy(update={"origin": "ai"})
                    for result in validated_extractions
                    for d in result.extraction.definitions
                ],
            ],
            # 前の定義の説明・更新可能を正にする（再構築で説明を連結しない。#1736）。
            previous=previous_names,
        )
        # 一意でない列でつなぐリンク型は、業務説明に追記された改善案の結合のキーにする（#1760）。
        # 修復の前に直し、同じ両端・結合のキーの AI のリンク型と 1 つにまとめる（残すのは前の定義の
        # 名前）。
        from .ontology_improvement_suggestion import apply_business_text_links

        apply_business_text_links(unified_definitions, business_text, json.loads(schema_context))
        # AI の出力を graph にできる形へ直す（共有した列のプロパティの分割など。#1720）。
        # 同じ表を指すオブジェクト型の統合と、外部キー・同じ名前と型の ID 列の関係も作る（#1729）。
        repair = repair_definitions_report(
            job.profile_id,
            unified_definitions,
            json.loads(schema_context),
            ai_output=True,
            previous=previous_names,
        )
        unified_definitions = repair.definitions
        # 業務説明に追記された改善案の文の粒度・主キーは、AI が読み落としても入れる（#1729）。
        # AI の出力の粒度・主キーより業務説明を正にし、前の定義は変えない（#1759）。
        from .ontology_improvement_suggestion import (
            apply_business_text_facts,
            business_text_cardinalities,
            business_text_grain_objects,
        )

        apply_business_text_facts(unified_definitions, business_text, previous=previous_names)
        # DB の列の型・NOT NULL で値が決まった競合は利用者に出さない（#1729）。粒度は業務説明と
        # DB の主キー・一意キーで決まるものを除く（#1759）。
        concept_conflicts = settle_grain_conflicts(
            schema_resolved_conflicts(
                concept_conflicts, unified_definitions, json.loads(schema_context), repair.remap
            ),
            unified_definitions,
            json.loads(schema_context),
            remap=repair.remap,
            decided=business_text_grain_objects(business_text),
        )
        # AI のリンク型の多重度は、業務説明の文 → 結合のキーと両端の主キー・一意キーで決め、決まった
        # 競合は出さない。前の定義の多重度は変えない（#1765）。
        concept_conflicts = settle_link_cardinalities(
            concept_conflicts,
            unified_definitions,
            json.loads(schema_context),
            remap=repair.remap,
            previous=previous_names,
            decided=business_text_cardinalities(unified_definitions, business_text),
        )
        merge_conflicts = list(concept_conflicts)
        from .ontology_definition_validation import validate_definitions
        from .ontology_definitions import ProfileOntologyBundle

        provisional = ProfileOntologyBundle(
            id=job_id,
            profile_id=job.profile_id,
            job_id=job_id,
            source_revision_id=ontology.revision.id,
            schema_fingerprint=ontology.revision.schema_fingerprint,
            profile_fingerprint=job.profile_fingerprint,
            definitions=unified_definitions,
            sources=definition_sources,
        )
        build_findings = validate_definitions(provisional, json.loads(schema_context))
        # AI が作った式（業務ルール・指標など）の SQL が確かめられなければ、下書きに入れない
        # （graph には要らず、公開を止めない。best effort。#1729）。
        invalid_sql = {
            f.definition_id
            for f in build_findings
            if f.code == "SQL_EXPRESSION_INVALID" and f.severity == "error"
        }
        dropped_sql = [
            d
            for d in unified_definitions
            if (d.id in invalid_sql or d.api_name in invalid_sql)
            and d.kind in {"business_rule", "metric", "function", "object_set", "action_type"}
        ]
        if dropped_sql:
            unified_definitions = [
                d for d in unified_definitions if all(d is not x for x in dropped_sql)
            ]
            content_notes.extend(
                f"『{d.name_ja}』の SQL の式を確かめられなかったため、下書きに含めませんでした。"
                "必要なら Markdown に正しい式で書き足してください。"
                for d in dropped_sql
            )
            provisional = provisional.model_copy(update={"definitions": unified_definitions})
            build_findings = validate_definitions(provisional, json.loads(schema_context))
        # 利用者に見せる文には内部の ID・項目名を出さず、業務の名前を使う（#1729）。
        finding_names = {
            key: d.name_ja for d in unified_definitions for key in (d.id, d.api_name) if key
        }
        concept_conflicts.extend(
            f"『{finding_names.get(f.definition_id, f.definition_id)}』: {f.message_ja}"
            for f in build_findings
            if f.severity == "error"
        )
        content_notes.extend(
            f"『{finding_names.get(f.definition_id, f.definition_id)}』: {f.message_ja}"
            for f in build_findings
            if f.severity == "warning"
        )
        content_notes.extend(
            f"『{d.name_ja}』は対応する表・ビューが分からないため、下書きに含めませんでした。"
            "含める場合は、業務プロファイルに表・ビューを足して構築し直してください。"
            for d in repair.dropped
            if d.kind == "object_type"
        )
        content_notes.extend(
            f"共有プロパティ『{d.name_ja}』はどのプロパティからも使われていないため、"
            "下書きから外しました。必要なら Markdown に書き足してください。"
            for d in repair.unused_shared
        )
        # 対応する列の決まらない AI のプロパティと、ビューの依存だけで結合のキーの無い AI の
        # リンク型は、graph に置けないため下書きから外す（公開を止めない。#1736）。
        owner_names = {d.api_name: d.name_ja for d in unified_definitions}
        content_notes.extend(
            f"{_owner_label(owner_names, getattr(d, 'object_type', ''))}『{d.name_ja}』は"
            "対応する列が分からないため、下書きから外しました。"
            "必要なら Markdown のプロパティに「対応する表・列」を書き足してください。"
            for d in repair.unmapped_properties
        )
        content_notes.extend(
            f"『{owner_names.get(getattr(d, 'source', ''), getattr(d, 'source', ''))}』と"
            f"『{owner_names.get(getattr(d, 'target', ''), getattr(d, 'target', ''))}』の"
            "リンク型は、結合のキー（つなぐ列）が分からないため下書きから外しました。"
            "つなぐ列があれば、Markdown にリンク型と「結合のキー」を書き足してください。"
            for d in repair.keyless_links
        )
        # 主キー・一意キー・外部キーでない列でしかつなげない AI のリンク型（#1741）。
        content_notes.extend(
            f"『{owner_names.get(getattr(d, 'source', ''), getattr(d, 'source', ''))}』と"
            f"『{owner_names.get(getattr(d, 'target', ''), getattr(d, 'target', ''))}』の"
            "リンク型は、主キー・一意キーではない列でつないでいたため、下書きから外しました。"
            "主キー・一意キー・外部キーの列でつなげるときは、Markdown にリンク型と"
            "「結合のキー」を書き足してください。"
            for d in repair.nonkey_links
        )
        self._set_definition_phase(
            job_id,
            "validation",
            "succeeded",
            f"定義 {len(unified_definitions)} 件、要確認 {len(concept_conflicts)} 件。",
        )
        self._set_definition_phase(job_id, "markdown", "running")
        self._emit(job_id, "Markdown 下書きをレンダリングしています。")
        try:
            markdown_output = render_ontology_build_markdown(
                profile_id=job.profile_id,
                schema_context=schema_context,
                drafts=draft_inputs,
                warnings=actionable_warnings,
                proposal_rejections=proposal_rejections,
                source_count=len(job.source_document_ids),
                qa_pair_count=len(qa_pairs),
                business_text_present=bool(text_units),
                qa_pairs=qa_pairs,
                ontology=ontology,
                profile_view=view,
            )
            # 中身の無い節・入力の要約・物理オブジェクトの一覧は書かない（#1720）。
            # Q/A 由来のルール・構造パターンは中身があるときだけ残す。
            markdown_output = (
                "\n\n".join(
                    part.strip()
                    for part in (
                        qa_markdown_sections(markdown_output),
                        render_concepts(
                            unified_definitions,
                            concept_conflicts,
                            [
                                c
                                for result in validated_extractions
                                for c in result.extraction.coverage
                            ],
                        ),
                    )
                    if part.strip()
                )
                + "\n"
            )
            self._set_definition_phase(job_id, "markdown", "succeeded")
            self._set_definition_phase(job_id, "save", "running")
        except Exception as exc:
            logger.warning("ontology_build_markdown_render_failed", exc_info=True)
            self._set_step(
                job_id,
                OntologyBuildStepName.PROPOSAL_REGISTRATION,
                OntologyBuildStepStatus.FAILED,
                "Markdown 下書きのレンダリングに失敗しました。",
            )
            self._fail(job_id, f"Markdown 下書きのレンダリングに失敗しました: {exc}")
            return
        self._set_step(
            job_id,
            OntologyBuildStepName.PROPOSAL_REGISTRATION,
            OntologyBuildStepStatus.RUNNING,
            f"Markdown 下書きをレンダリングしました({len(markdown_output)} 文字)。",
        )
        self._emit(job_id, f"Markdown 下書きをレンダリングしました({len(markdown_output)} 文字)。")

        def save_progress(message_ja: str) -> None:
            self._assert_execution(job_id)
            # 保存完了イベントには機械可読コードを付け、frontend は文言正規表現ではなく
            # このコードで Markdown 下書きの再取得を判断できるようにする。
            code = "MARKDOWN_DRAFT_UPDATED" if "保存しました" in message_ja else ""
            self._set_step(
                job_id,
                OntologyBuildStepName.PROPOSAL_REGISTRATION,
                OntologyBuildStepStatus.RUNNING,
                message_ja,
                code=code,
            )
            self._emit(
                job_id,
                message_ja,
                code=code,
                step=OntologyBuildStepName.PROPOSAL_REGISTRATION,
            )

        try:
            draft_ontology, markdown_artifact = self._runtime.create_build_markdown_draft(
                profile_id=job.profile_id,
                base_revision_id=ontology.revision.id,
                payloads=[draft.payload for draft in draft_inputs],
                titles=[draft.title_ja for draft in draft_inputs],
                markdown=markdown_output,
                unified_definitions=unified_definitions,
                definition_conflicts=merge_conflicts,
                definition_diagnostics=[
                    *[f.model_dump(mode="json") for f in build_findings],
                    *[
                        {"message_ja": message}
                        for message in _unique_non_empty_messages(
                            [*content_notes, *warnings, *proposal_rejections]
                        )
                    ],
                ],
                note=f"AI 構築 Markdown 下書き: {len(draft_inputs)} 件",
                prepared_base=ontology,
                on_progress=save_progress,
            )
        except Exception as exc:
            logger.warning("ontology_build_markdown_save_failed", exc_info=True)
            message = getattr(exc, "message_ja", str(exc))
            self._set_step(
                job_id,
                OntologyBuildStepName.PROPOSAL_REGISTRATION,
                OntologyBuildStepStatus.FAILED,
                "Markdown 下書きの保存に失敗しました。",
            )
            self._fail(job_id, message)
            return
        result_counts = _build_result_counts_ja(
            len(unified_definitions),
            warning_count=len(actionable_warnings),
            rejected_count=len(proposal_rejections),
        )
        registered_note = (
            f"Markdown 下書き v{draft_ontology.revision.version} を生成しました({result_counts})。"
        )
        self._set_step(
            job_id,
            OntologyBuildStepName.PROPOSAL_REGISTRATION,
            OntologyBuildStepStatus.RUNNING,
            "構築 job の完了状態を保存しています…",
            code="FINALIZING",
        )
        self._emit(job_id, "構築 job の完了状態を保存しています。", code="FINALIZING")

        from .ontology_definition_service import ProfileOntologyDefinitionService

        self._assert_execution(job_id)
        bundle = ProfileOntologyDefinitionService(self._runtime).save_build(
            profile_id=job.profile_id,
            job_id=job_id,
            definitions=unified_definitions,
            unified=True,
            coverage=[
                item for result in validated_extractions for item in result.extraction.coverage
            ],
            schema_fingerprint=ontology.revision.schema_fingerprint,
            source_revision_id=draft_ontology.revision.id,
            sources=definition_sources,
            schema_context_fingerprint=context_fingerprint,
            profile_fingerprint=job.profile_fingerprint,
            requires_revalidation=(
                job.profile_fingerprint
                != definition_fingerprint(
                    self._runtime._strict_profile(job.profile_id).model_dump(mode="json")
                )
                or context_fingerprint
                != definition_fingerprint(
                    str(self._runtime.prepare_build_schema_context(job.profile_id).schema_context)
                )
            ),
        )
        self._set_definition_phase(
            job_id,
            "save",
            "succeeded",
            f"検査事項 {len(bundle.findings)} 件、競合 {len(bundle.conflicts)} 件。",
        )
        self._record_build_removals(
            job.profile_id,
            job_id,
            plain_build_removals(
                dropped_objects=[d for d in repair.dropped if d.kind == "object_type"],
                dropped_sql=dropped_sql,
                unused_shared=repair.unused_shared,
                unmapped_properties=repair.unmapped_properties,
                keyless_links=repair.keyless_links,
                nonkey_links=repair.nonkey_links,
                nonkey_columns=repair.nonkey_columns,
                names=owner_names,
            ),
        )

        def finish(job: OntologyBuildJob) -> None:
            finished_at = utc_now()
            for step in job.steps:
                if step.name == OntologyBuildStepName.PROPOSAL_REGISTRATION:
                    step.status = OntologyBuildStepStatus.SUCCEEDED
                    step.detail_ja = registered_note
                    step.code = ""
                    if step.started_at is None:
                        step.started_at = finished_at
                    step.finished_at = finished_at
                    break
            if any(step.status == OntologyBuildStepStatus.SUCCEEDED for step in job.steps):
                # 一部の LLM batch が失敗しても成功分で Draft を生成した場合は
                # 部分成功として区別する(全 discard しない)。
                job.status = (
                    OntologyBuildStatus.SUCCEEDED_WITH_WARNINGS
                    if partial_failed_steps
                    else OntologyBuildStatus.SUCCEEDED
                )
            else:
                job.status = OntologyBuildStatus.FAILED
            job.proposal_ids = []
            job.result_bundle_id = bundle.id
            job.concept_coverage = bundle.coverage
            job.draft_revision_id = draft_ontology.revision.id
            job.draft_etag = str(markdown_artifact.get("etag") or "")
            job.markdown_output = markdown_output
            job_warnings, _job_rejections = _split_ontology_build_warnings(
                [*job.warnings_ja, *warnings]
            )
            job.warnings_ja = job_warnings
            job.finished_at = finished_at
            job.events.append(
                OntologyBuildEvent(
                    message_ja=registered_note,
                    code="MARKDOWN_DRAFT_UPDATED",
                    step=OntologyBuildStepName.PROPOSAL_REGISTRATION,
                    phase="save",
                )
            )
            job.events.append(
                OntologyBuildEvent(
                    message_ja=(
                        f"構築が完了しました(Markdown 下書き v{draft_ontology.revision.version}"
                        + (f"、警告 {len(actionable_warnings)} 件" if actionable_warnings else "")
                        + (f"、採用外 {len(proposal_rejections)} 件" if proposal_rejections else "")
                        + ")。"
                    )
                )
            )
            if len(job.events) > _MAX_JOB_EVENTS:
                del job.events[: len(job.events) - _MAX_JOB_EVENTS]

        try:
            self._update_persisted_copy(job_id, finish)
        except Exception as exc:
            logger.warning("ontology_build_job_finalize_failed", exc_info=True)
            message = getattr(exc, "message_ja", str(exc))
            self._fail(
                job_id,
                f"構築 job の完了状態の保存に失敗しました: {message}",
                failed_step=OntologyBuildStepName.PROPOSAL_REGISTRATION,
                failed_step_detail_ja="構築 job の完了状態の保存に失敗しました。",
            )
            return
        finished_job = self.get(job_id)
        record_job(
            job_type="build",
            status=(
                "succeeded"
                if finished_job is not None
                and finished_job.status
                in {OntologyBuildStatus.SUCCEEDED, OntologyBuildStatus.SUCCEEDED_WITH_WARNINGS}
                else "failed"
            ),
            error_code="none" if finished_job is not None else "result_missing",
        )

    def _is_cancelled(self, job_id: str) -> bool:
        current = self.get(job_id)
        return current is not None and current.status == OntologyBuildStatus.CANCELLED
