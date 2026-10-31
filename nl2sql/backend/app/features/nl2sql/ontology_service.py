"""Versioned NL2SQL Ontology と query session の in-memory reference service。

Oracle 永続化 adapter を接続する前でも同じ契約と gate を検証できるよう、状態遷移、
optimistic concurrency、二段階確認、hash binding はこの domain service に集約する。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from threading import RLock
from typing import Any, NoReturn
from uuid import uuid4

from pr_backend_core.observability.model_usage import RunModelUsage, add_run_usage
from pydantic import BaseModel, ValidationError

from .object_identity import (
    catalog_match_key,
    object_match_key,
    split_match_key,
    sql_identifier_token,
)
from .ontology_models import (
    ClarificationMode,
    GraphPatch,
    GraphPatchOperation,
    IntentAmbiguity,
    IntentDimension,
    IntentEntity,
    IntentMetric,
    IntentRelationshipPath,
    OntologyEdge,
    OntologyNode,
    OntologyNodeKind,
    OntologyProposal,
    OntologyProposalKind,
    OntologyProposalPayload,
    OntologyProposalStatus,
    OntologyReviewStatus,
    OntologyRevision,
    OntologyRevisionStatus,
    OntologyValidationFinding,
    OntologyValidationReport,
    ProfileOntologyView,
    QueryExecutionRecord,
    QuerySession,
    QuerySessionCreate,
    QuerySessionStatus,
    QuestionIntentGraph,
    RelationshipCardinality,
    RequirementAnswerRecord,
    SqlArtifact,
    SqlConfirmationBinding,
    SqlConfirmationRequest,
    SqlRequirementDraft,
    SqlSemanticGraph,
    ValidationSeverity,
    utc_now,
)
from .sql_semantics import parse_oracle_sql, sql_sha256


class OntologyServiceError(RuntimeError):
    """HTTP 層へ安全に写像できる domain error。"""

    def __init__(self, code: str, message_ja: str) -> None:
        super().__init__(message_ja)
        self.code = code
        self.message_ja = message_ja


class OntologyNotFoundError(OntologyServiceError):
    pass


class OntologyVersionConflictError(OntologyServiceError):
    pass


class OntologyGateBlockedError(OntologyServiceError):
    def __init__(
        self,
        code: str,
        message_ja: str,
        *,
        finding_codes: Sequence[str] = (),
    ) -> None:
        super().__init__(code, message_ja)
        self.finding_codes = list(finding_codes)


class OntologyIntegrityError(OntologyServiceError):
    pass


class OntologyStateConflictError(OntologyServiceError):
    pass


# 公開の後に業務プロファイルか DB の構造（スキーマ）が変わり、公開版の業務定義をそのまま使えない
# （公開版の指紋が今の業務プロファイル・Schema と合わない）。時間をおいても直らず、管理者が
# 「オントロジー構築」で Markdown を確認し直して公開するまで続く（#1827）。
BUSINESS_SCOPE_CHANGED_CODE = "BUSINESS_SCOPE_CHANGED"
BUSINESS_SCOPE_CHANGED_MESSAGE_JA = (
    "公開の後に業務プロファイルか DB の構造（スキーマ）が変わったため、"
    "公開版の業務定義を使えません。「オントロジー構築」で Markdown を確認し直して公開してください。"
)


# 操作が途絶えて中止した query session の error_code と、利用者への案内（#1274）。
QUERY_SESSION_EXPIRED_CODE = "QUERY_SESSION_EXPIRED"
QUERY_SESSION_EXPIRED_MESSAGE_JA = (
    "一定時間操作がなかったため、この確認は終了しました。閉じてから、もう一度始めてください。"
)


def _canonical_hash(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stable_physical_node_id(
    owner: str,
    object_type: str,
    object_name: str,
    column_name: str | None = None,
) -> str:
    """schema refresh を跨いで安定する物理 node ID。"""

    identity = "\x1f".join(
        part.strip().upper() for part in (owner, object_type, object_name, column_name or "")
    )
    prefix = "column" if column_name else object_type.strip().lower() or "object"
    return f"physical_{prefix}_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"


def compute_schema_fingerprint(objects: Iterable[Any]) -> str:
    """順序に依存しない schema fingerprint を生成する。"""

    normalized: list[Any] = []
    for item in objects:
        if hasattr(item, "model_dump"):
            normalized.append(item.model_dump(mode="json"))
        elif isinstance(item, Mapping):
            normalized.append(dict(item))
        else:
            normalized.append(str(item))
    normalized.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
    return _canonical_hash(normalized)


def _copy_model[ModelT: BaseModel](value: ModelT) -> ModelT:
    return value.model_copy(deep=True)


def _sql_part_token(name: str, *, quoted: bool) -> str:
    """SQL parser が返した識別子 1 部分の照合 token。不正な値は例外にせずそのまま返す。"""

    if not str(name or "").strip():
        return ""
    try:
        return sql_identifier_token(name, quoted=quoted)
    except ValueError:
        return str(name).strip()


def _variants_from_tokens(owner: str, object_name: str, column_name: str) -> set[str]:
    """照合 token の owner / object / column から、修飾の有無に依存しない比較値を作る。"""

    if not column_name:
        return set()
    values = {column_name}
    if object_name:
        values.add(f"{object_name}.{column_name}")
    if owner and object_name:
        values.add(f"{owner}.{object_name}.{column_name}")
    return values


def _column_variants(owner: str, object_name: str, column_name: str) -> set[str]:
    """カタログ上の owner / object / column 名（大文字小文字を保持）の比較値。

    `object_identity.catalog_match_key` の token で比べる。引用符を外して大文字化すると、
    `SALES."Mixed_Case"."Amount"` と大文字の同名表・同名列を同じ列として扱う（#573）。
    """

    return _variants_from_tokens(
        catalog_match_key(owner) if owner.strip() else "",
        catalog_match_key(object_name) if object_name.strip() else "",
        catalog_match_key(column_name) if column_name.strip() else "",
    )


def _reference_tokens(value: str) -> list[str]:
    """SQL の表記の参照（`SALES."Mixed_Case"."Amount"`、各部は引用可）を照合 token に分ける。"""

    return split_match_key(object_match_key(value))


def _sql_table_key(table: Any) -> str:
    """SQL の表参照（catalog / owner / 表名）の照合キー。"""

    parts = [
        object_match_key(table.catalog) if table.catalog else "",
        _sql_part_token(table.owner, quoted=table.owner_quoted),
        _sql_part_token(table.name, quoted=table.name_quoted),
    ]
    return ".".join(part for part in parts if part)


def _raw_column_variants(value: str) -> set[str]:
    parts = _reference_tokens(value)
    if not parts:
        return set()
    if len(parts) >= 3:
        return _variants_from_tokens(parts[-3], parts[-2], parts[-1])
    if len(parts) == 2:
        return _variants_from_tokens("", parts[0], parts[1])
    return {parts[0]}


def _sql_alias_map(graph: SqlSemanticGraph) -> dict[str, tuple[str, str]]:
    aliases: dict[str, tuple[str, str]] = {}
    for table in graph.tables:
        if table.is_cte:
            continue
        name = _sql_part_token(table.name, quoted=table.name_quoted)
        target = (_sql_part_token(table.owner, quoted=table.owner_quoted), name)
        aliases[name] = target
        alias = table.alias.strip()
        if alias:
            # 別名の引用の有無は保持されないため、引用なし・引用ありの両方の解釈で引けるようにする。
            aliases[object_match_key(alias)] = target
            aliases[catalog_match_key(alias)] = target
    return aliases


def _sql_column_variants(
    value: str,
    aliases: Mapping[str, tuple[str, str]],
) -> set[str]:
    parts = _reference_tokens(value)
    if not parts:
        return set()
    if len(parts) >= 3:
        return _variants_from_tokens(parts[-3], parts[-2], parts[-1])
    if len(parts) == 2:
        qualifier, column_name = parts
        if qualifier in aliases:
            owner, object_name = aliases[qualifier]
            return _variants_from_tokens(owner, object_name, column_name)
        return _variants_from_tokens("", qualifier, column_name)
    return {parts[0]}


def _physical_column_variants(reference: Any) -> set[str]:
    return _column_variants(
        str(getattr(reference, "owner", "")),
        str(getattr(reference, "object_name", "")),
        str(getattr(reference, "column_name", "")),
    )


def _node_column_variants(node: OntologyNode | None) -> set[str]:
    if node is None:
        return set()
    values: set[str] = set()
    mapped_objects: list[tuple[str, str]] = []
    for mapping in node.physical_mappings:
        mapped_objects.append((mapping.object_ref.owner, mapping.object_ref.object_name))
        for reference in mapping.column_refs:
            values.update(_physical_column_variants(reference))

    technical = node.technical_name.strip()
    if technical and node.kind in {
        OntologyNodeKind.COLUMN,
        OntologyNodeKind.PROPERTY,
        OntologyNodeKind.METRIC,
    }:
        values.update(_raw_column_variants(technical))
        if "." not in technical:
            for owner, object_name in mapped_objects:
                values.update(
                    _variants_from_tokens(
                        catalog_match_key(owner) if owner.strip() else "",
                        catalog_match_key(object_name) if object_name.strip() else "",
                        object_match_key(technical),
                    )
                )
    return values


def _columns_match(expected: set[str], actual: set[str]) -> bool:
    return bool(expected and actual and expected.intersection(actual))


def _normalize_operator(value: str) -> str:
    normalized = " ".join(value.strip().upper().split())
    return {
        "==": "=",
        "EQ": "=",
        "EQUALS": "=",
        "<>": "!=",
        "NE": "!=",
        "NOT_EQUALS": "!=",
        "ISNULL": "IS NULL",
        "ISNOTNULL": "IS NOT NULL",
    }.get(normalized, normalized)


def _reverse_operator(value: str) -> str:
    return {"<": ">", "<=": ">=", ">": "<", ">=": "<="}.get(value, value)


def _canonical_number(value: str) -> str | None:
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    normalized = format(number.normalize(), "f")
    return "0" if normalized in {"-0", ""} else normalized


def _canonical_sql_literal(expression: Any) -> tuple[bool, Any]:
    try:
        from sqlglot import exp
    except ImportError:  # pragma: no cover - sql parser dependency gate が先に防ぐ
        return False, None

    while isinstance(expression, exp.Paren):
        expression = expression.this
    if isinstance(expression, exp.Literal):
        raw = str(expression.this)
        if bool(expression.is_string):
            return True, ("string", raw)
        number = _canonical_number(raw)
        return (True, ("number", number)) if number is not None else (False, None)
    if isinstance(expression, exp.Neg):
        known, value = _canonical_sql_literal(expression.this)
        if known and isinstance(value, tuple) and value[0] == "number":
            number = _canonical_number(f"-{value[1]}")
            return (True, ("number", number)) if number is not None else (False, None)
        return False, None
    if isinstance(expression, exp.Null):
        return True, ("null", None)
    if isinstance(expression, exp.Boolean):
        raw_boolean = expression.this
        if isinstance(raw_boolean, bool):
            return True, ("boolean", raw_boolean)
        return True, ("boolean", str(raw_boolean).strip().upper() == "TRUE")
    if isinstance(expression, exp.Cast):
        return _canonical_sql_literal(expression.this)
    return False, None


def _canonical_intent_value(value: Any, value_type: str) -> tuple[bool, Any]:
    if isinstance(value, (list, tuple)):
        normalized_items: list[Any] = []
        for item in value:
            known, normalized = _canonical_intent_value(item, value_type)
            if not known:
                return False, None
            normalized_items.append(normalized)
        normalized_items.sort(key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
        return True, ("list", tuple(normalized_items))
    if value is None:
        return True, ("null", None)
    kind = value_type.strip().lower()
    if kind in {"boolean", "bool"} or isinstance(value, bool):
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered not in {"true", "false"}:
                return False, None
            value = lowered == "true"
        return True, ("boolean", bool(value))
    if kind in {"number", "numeric", "integer", "float", "decimal"} or isinstance(
        value, (int, float, Decimal)
    ):
        number = _canonical_number(str(value))
        return (True, ("number", number)) if number is not None else (False, None)
    if isinstance(value, str):
        return True, ("string", value)
    return False, None


@dataclass(frozen=True)
class _PredicateAtom:
    element_id: str
    columns: frozenset[str]
    operator: str
    value_known: bool
    value: Any
    expression_sql: str


@dataclass(frozen=True)
class _JoinPair:
    left_columns: frozenset[str]
    right_columns: frozenset[str]
    operator: str


def _direct_column_variants(
    expression: Any,
    aliases: Mapping[str, tuple[str, str]],
) -> set[str]:
    try:
        from sqlglot import exp
    except ImportError:  # pragma: no cover
        return set()
    while isinstance(expression, exp.Paren):
        expression = expression.this
    if not isinstance(expression, exp.Column):
        return set()
    return _sql_column_variants(str(expression.sql(dialect="oracle")), aliases)


def _flatten_and(expression: Any) -> list[Any] | None:
    try:
        from sqlglot import exp
    except ImportError:  # pragma: no cover
        return None
    if isinstance(expression, exp.And):
        left = _flatten_and(expression.this)
        right = _flatten_and(expression.expression)
        if left is None or right is None:
            return None
        return [*left, *right]
    if isinstance(expression, exp.Or):
        return None
    return [expression]


def _parse_expression(expression_sql: str) -> Any | None:
    try:
        import sqlglot

        return sqlglot.parse_one(expression_sql, read="oracle")
    except Exception:
        return None


def _comparison_parts(expression: Any) -> tuple[Any, Any, str] | None:
    try:
        from sqlglot import exp
    except ImportError:  # pragma: no cover
        return None
    comparison_types: list[tuple[type[Any], str]] = [
        (exp.EQ, "="),
        (exp.NEQ, "!="),
        (exp.GT, ">"),
        (exp.GTE, ">="),
        (exp.LT, "<"),
        (exp.LTE, "<="),
        (exp.Like, "LIKE"),
    ]
    ilike = getattr(exp, "ILike", None)
    if ilike is not None:
        comparison_types.append((ilike, "ILIKE"))
    for expression_type, operator in comparison_types:
        if isinstance(expression, expression_type):
            return expression.this, expression.expression, operator
    return None


def _predicate_atoms(
    expression_sql: str,
    *,
    element_id: str,
    aliases: Mapping[str, tuple[str, str]],
) -> tuple[list[_PredicateAtom], bool]:
    expression = _parse_expression(expression_sql)
    if expression is None:
        return [], False
    expressions = _flatten_and(expression)
    if expressions is None:
        return [], False
    try:
        from sqlglot import exp
    except ImportError:  # pragma: no cover
        return [], False

    atoms: list[_PredicateAtom] = []
    for index, item in enumerate(expressions):
        atom_id = f"{element_id}:{index + 1}"
        comparison = _comparison_parts(item)
        if comparison is not None:
            left, right, operator = comparison
            left_columns = _direct_column_variants(left, aliases)
            right_columns = _direct_column_variants(right, aliases)
            if bool(left_columns) == bool(right_columns):
                return [], False
            if right_columns:
                left, right = right, left
                left_columns = right_columns
                operator = _reverse_operator(operator)
            known, value = _canonical_sql_literal(right)
            atoms.append(
                _PredicateAtom(
                    element_id=atom_id,
                    columns=frozenset(left_columns),
                    operator=operator,
                    value_known=known,
                    value=value,
                    expression_sql=str(item.sql(dialect="oracle")),
                )
            )
            continue
        if isinstance(item, exp.In):
            columns = _direct_column_variants(item.this, aliases)
            if not columns or item.args.get("query") is not None:
                return [], False
            values: list[Any] = []
            for value_expression in item.expressions:
                known, value = _canonical_sql_literal(value_expression)
                if not known:
                    return [], False
                values.append(value)
            values.sort(key=lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True))
            atoms.append(
                _PredicateAtom(
                    element_id=atom_id,
                    columns=frozenset(columns),
                    operator="IN",
                    value_known=True,
                    value=("list", tuple(values)),
                    expression_sql=str(item.sql(dialect="oracle")),
                )
            )
            continue
        if isinstance(item, exp.Between):
            columns = _direct_column_variants(item.this, aliases)
            low_known, low = _canonical_sql_literal(item.args.get("low"))
            high_known, high = _canonical_sql_literal(item.args.get("high"))
            if not columns:
                return [], False
            atoms.append(
                _PredicateAtom(
                    element_id=atom_id,
                    columns=frozenset(columns),
                    operator="BETWEEN",
                    value_known=low_known and high_known,
                    value=("range", low, high),
                    expression_sql=str(item.sql(dialect="oracle")),
                )
            )
            continue
        if isinstance(item, exp.Is):
            columns = _direct_column_variants(item.this, aliases)
            known, value = _canonical_sql_literal(item.expression)
            if not columns or not known:
                return [], False
            atoms.append(
                _PredicateAtom(
                    element_id=atom_id,
                    columns=frozenset(columns),
                    operator="IS NULL" if value == ("null", None) else "IS",
                    value_known=True,
                    value=value,
                    expression_sql=str(item.sql(dialect="oracle")),
                )
            )
            continue
        return [], False
    return atoms, True


def _join_pairs(
    expression_sql: str,
    aliases: Mapping[str, tuple[str, str]],
) -> list[_JoinPair] | None:
    expression = _parse_expression(expression_sql)
    if expression is None:
        return None
    expressions = _flatten_and(expression)
    if expressions is None:
        return None
    pairs: list[_JoinPair] = []
    for item in expressions:
        comparison = _comparison_parts(item)
        if comparison is None:
            return None
        left, right, operator = comparison
        left_columns = _direct_column_variants(left, aliases)
        right_columns = _direct_column_variants(right, aliases)
        if not left_columns or not right_columns:
            return None
        pairs.append(
            _JoinPair(
                left_columns=frozenset(left_columns),
                right_columns=frozenset(right_columns),
                operator=operator,
            )
        )
    return pairs


def _validation_hash(report: OntologyValidationReport) -> str:
    payload = report.model_dump(mode="json", exclude={"validation_hash"})
    return _canonical_hash(payload)


def _make_report(
    *,
    intent_version: int,
    sql_hash: str,
    ontology_revision_id: str,
    findings: list[OntologyValidationFinding],
    coverage: float,
) -> OntologyValidationReport:
    passed = sum(item.severity == ValidationSeverity.PASS for item in findings)
    warnings = sum(item.severity == ValidationSeverity.WARNING for item in findings)
    blockers = sum(item.severity == ValidationSeverity.BLOCKER for item in findings)
    report = OntologyValidationReport(
        id=f"validation_{uuid4().hex}",
        intent_version=intent_version,
        sql_hash=sql_hash,
        ontology_revision_id=ontology_revision_id,
        is_valid=blockers == 0,
        intent_coverage=coverage,
        findings=findings,
        passed_count=passed,
        warning_count=warnings,
        blocker_count=blockers,
    )
    report.validation_hash = _validation_hash(report)
    return report


class OntologyQuerySessionService:
    """Ontology revisions と query sessions の thread-safe reference implementation。"""

    _PATCHABLE_ROOTS = {
        "question_effective",
        "entities",
        "metrics",
        "dimensions",
        "filters",
        "time_range",
        "granularity",
        "sorts",
        "limit",
        "candidate_paths",
        "selected_path_id",
        "ambiguities",
        "confidence",
    }

    def __init__(self) -> None:
        self._lock = RLock()
        self._revisions: dict[str, OntologyRevision] = {}
        self._nodes: dict[str, dict[str, OntologyNode]] = {}
        self._edges: dict[str, dict[str, OntologyEdge]] = {}
        self._profile_views: dict[str, ProfileOntologyView] = {}
        self._sessions: dict[str, QuerySession] = {}
        self._proposals: dict[str, OntologyProposal] = {}

    # --- Ontology revision / profile view -------------------------------------------------

    def register_revision(
        self,
        revision: OntologyRevision,
        *,
        nodes: Sequence[OntologyNode] = (),
        edges: Sequence[OntologyEdge] = (),
    ) -> OntologyRevision:
        with self._lock:
            node_map = {node.id: _copy_model(node) for node in nodes}
            if len(node_map) != len(nodes):
                raise OntologyIntegrityError(
                    "DUPLICATE_NODE_ID", "Ontology node ID が重複しています。"
                )
            edge_map = {edge.id: _copy_model(edge) for edge in edges}
            if len(edge_map) != len(edges):
                raise OntologyIntegrityError(
                    "DUPLICATE_EDGE_ID", "Ontology edge ID が重複しています。"
                )
            if any(node.revision_id != revision.id for node in node_map.values()):
                raise OntologyIntegrityError(
                    "NODE_REVISION_MISMATCH", "node と revision の ID が一致していません。"
                )
            if any(edge.revision_id != revision.id for edge in edge_map.values()):
                raise OntologyIntegrityError(
                    "EDGE_REVISION_MISMATCH", "edge と revision の ID が一致していません。"
                )
            for edge in edge_map.values():
                if edge.source_node_id not in node_map or edge.target_node_id not in node_map:
                    raise OntologyIntegrityError(
                        "EDGE_ENDPOINT_NOT_FOUND", "edge の始点または終点 node が存在しません。"
                    )

            existing = self._revisions.get(revision.id)
            if existing is not None:
                # 再構成の観測日時は内容の変更ではない。ノード・辺の順にも依存しない。
                def content(value: Any) -> Any:
                    if isinstance(value, OntologyRevision):
                        return value.model_dump(mode="json", exclude={"created_at", "etag"})
                    if isinstance(value, (OntologyNode, OntologyEdge)):
                        data = value.model_dump(mode="json")
                        data["provenance"].pop("observed_at", None)
                        return data
                    if isinstance(value, dict):
                        return {key: content(item) for key, item in value.items()}
                    return value

                same_header = content(existing) == content(revision)
                same_nodes = content(self._nodes[revision.id]) == content(node_map)
                same_edges = content(self._edges[revision.id]) == content(edge_map)
                if same_header and same_nodes and same_edges:
                    return _copy_model(existing)
                raise OntologyVersionConflictError(
                    "REVISION_ALREADY_EXISTS",
                    "同じ版の識別子に異なる内容は登録できません。新しい版を作成してください。",
                )

            stored = _copy_model(revision)
            if not stored.etag:
                stored.etag = _canonical_hash(
                    {
                        "revision": stored.model_dump(mode="json", exclude={"etag"}),
                        "nodes": [node.model_dump(mode="json") for node in node_map.values()],
                        "edges": [edge.model_dump(mode="json") for edge in edge_map.values()],
                    }
                )
            self._revisions[stored.id] = stored
            self._nodes[stored.id] = node_map
            self._edges[stored.id] = edge_map
            return _copy_model(stored)

    def publish_revision(
        self,
        revision_id: str,
        *,
        etag: str,
        updates: Mapping[str, Any] | None = None,
    ) -> OntologyRevision:
        with self._lock:
            revision = self._require_revision(revision_id)
            if revision.etag != etag:
                raise OntologyVersionConflictError(
                    "REVISION_ETAG_MISMATCH",
                    "Ontology revision が更新されています。再読込してください。",
                )
            if revision.status != OntologyRevisionStatus.DRAFT:
                raise OntologyStateConflictError(
                    "REVISION_NOT_DRAFT", "draft の Ontology revision だけを公開できます。"
                )
            published = revision.model_copy(
                update={
                    **dict(updates or {}),
                    "status": OntologyRevisionStatus.PUBLISHED,
                    "published_at": utc_now(),
                },
                deep=True,
            )
            published.etag = _canonical_hash(
                {
                    "revision": published.model_dump(mode="json", exclude={"etag"}),
                    "nodes": [
                        node.model_dump(mode="json") for node in self._nodes[revision_id].values()
                    ],
                    "edges": [
                        edge.model_dump(mode="json") for edge in self._edges[revision_id].values()
                    ],
                }
            )
            self._revisions[revision_id] = published
            return _copy_model(published)

    def archive_published_revisions_except(self, active_revision_id: str) -> list[OntologyRevision]:
        """Active published revision を 1 つに保つため、古い published を archived にする。"""

        with self._lock:
            archived: list[OntologyRevision] = []
            for revision_id, revision in list(self._revisions.items()):
                if revision_id == active_revision_id:
                    continue
                if revision.status != OntologyRevisionStatus.PUBLISHED:
                    continue
                updated = revision.model_copy(
                    update={"status": OntologyRevisionStatus.ARCHIVED},
                    deep=True,
                )
                updated.etag = _canonical_hash(updated.model_dump(mode="json", exclude={"etag"}))
                self._revisions[revision_id] = updated
                archived.append(_copy_model(updated))
            return archived

    def restore_revision_headers(self, revisions: Sequence[OntologyRevision]) -> None:
        """永続 transaction 失敗時に in-memory header を元へ戻す。graph は不変。"""

        with self._lock:
            for revision in revisions:
                if revision.id not in self._revisions:
                    raise OntologyNotFoundError(
                        "ONTOLOGY_REVISION_NOT_FOUND",
                        "復元する Ontology revision が見つかりません。",
                    )
                self._revisions[revision.id] = _copy_model(revision)

    def evict_revision(self, revision_id: str) -> list[str]:
        """Immutable graph cache と、それに依存する再読込可能な session を破棄する。"""

        with self._lock:
            self._revisions.pop(revision_id, None)
            self._nodes.pop(revision_id, None)
            self._edges.pop(revision_id, None)
            stale_view_ids = [
                view_id
                for view_id, view in self._profile_views.items()
                if view.ontology_revision_id == revision_id
            ]
            for view_id in stale_view_ids:
                self._profile_views.pop(view_id, None)
            stale_session_ids = [
                session_id
                for session_id, session in self._sessions.items()
                if session.ontology_revision_id == revision_id
            ]
            for session_id in stale_session_ids:
                self._sessions.pop(session_id, None)
            return stale_session_ids

    def evict_profile_views(self, profile_id: str) -> list[str]:
        """削除済み Profile の live view cache だけを破棄し、監査 session は保持する。"""

        with self._lock:
            stale_view_ids = [
                view_id
                for view_id, view in self._profile_views.items()
                if view.profile_id == profile_id
            ]
            for view_id in stale_view_ids:
                self._profile_views.pop(view_id, None)
            return stale_view_ids

    def register_profile_view(self, view: ProfileOntologyView) -> ProfileOntologyView:
        with self._lock:
            revision = self._require_revision(view.ontology_revision_id)
            nodes = self._nodes[revision.id]
            edges = self._edges[revision.id]
            unknown_nodes = sorted(set(view.node_ids) - set(nodes))
            unknown_edges = sorted(set(view.edge_ids) - set(edges))
            if unknown_nodes or unknown_edges:
                raise OntologyIntegrityError(
                    "PROFILE_VIEW_SCOPE_UNKNOWN",
                    "プロファイル範囲に revision 内に存在しない node/edge があります。",
                )
            if any(path_id not in view.edge_ids for path_id in view.allowed_path_ids):
                raise OntologyIntegrityError(
                    "PROFILE_PATH_OUTSIDE_SCOPE",
                    "許可した関係 path がプロファイル範囲外です。",
                )
            stored = _copy_model(view)
            if not stored.etag:
                stored.etag = _canonical_hash(stored.model_dump(mode="json", exclude={"etag"}))
            self._profile_views[stored.id] = stored
            return _copy_model(stored)

    def get_revision(self, revision_id: str) -> OntologyRevision:
        with self._lock:
            return _copy_model(self._require_revision(revision_id))

    def get_profile_view(self, view_id: str) -> ProfileOntologyView:
        with self._lock:
            return _copy_model(self._require_profile_view(view_id))

    # --- Query intent / session ------------------------------------------------------------

    def create_session(self, request: QuerySessionCreate) -> QuerySession:
        with self._lock:
            view = self._require_profile_view(request.profile_view_id)
            if view.archived:
                raise OntologyGateBlockedError(
                    "PROFILE_VIEW_ARCHIVED",
                    "アーカイブ済みのプロファイル範囲は利用できません。",
                )
            if request.profile_id != view.profile_id:
                raise OntologyIntegrityError(
                    "PROFILE_VIEW_MISMATCH",
                    "指定した業務プロファイルとオントロジー範囲が一致しません。",
                )
            if request.ontology_revision_id != view.ontology_revision_id:
                raise OntologyIntegrityError(
                    "ONTOLOGY_REVISION_MISMATCH",
                    "指定した Ontology revision とプロファイル範囲が一致しません。",
                )
            self._require_revision(request.ontology_revision_id)

            intent = (
                _copy_model(request.intent)
                if request.intent
                else self._deterministic_intent(request, view)
            )
            if intent.profile_view_id != request.profile_view_id:
                raise OntologyIntegrityError(
                    "INTENT_PROFILE_VIEW_MISMATCH",
                    "質問の解釈とプロファイル範囲が一致しません。",
                )
            if intent.ontology_revision_id != request.ontology_revision_id:
                raise OntologyIntegrityError(
                    "INTENT_REVISION_MISMATCH", "質問の解釈と Ontology revision が一致しません。"
                )
            intent.version = 1
            intent.question_original = request.question
            session = QuerySession(
                id=f"query_session_{uuid4().hex}",
                profile_id=request.profile_id,
                profile_view_id=request.profile_view_id,
                ontology_revision_id=request.ontology_revision_id,
                business_release_id=request.business_release_id,
                status=QuerySessionStatus.AWAITING_INTENT_CONFIRMATION,
                original_question=request.question,
                current_intent_version=1,
                intents=[intent],
                clarification_mode=request.clarification_mode,
                actor_user_uuid=request.actor_user_uuid,
                actor_is_system_admin=request.actor_is_system_admin,
            )
            self._sessions[session.id] = session
            return _copy_model(session)

    def get_session(self, session_id: str) -> QuerySession:
        with self._lock:
            return _copy_model(self._require_session(session_id))

    def restore_session(self, session: QuerySession) -> QuerySession:
        """永続化済み session を replay せず、その version 履歴ごと復元する。"""

        with self._lock:
            if session.id in self._sessions:
                raise OntologyVersionConflictError(
                    "QUERY_SESSION_ALREADY_EXISTS",
                    "同じ query session ID は復元できません。",
                )
            return self.replace_session(session)

    def replace_session(self, session: QuerySession) -> QuerySession:
        """永続 store の最新 snapshot で in-memory session cache を置き換える。"""

        with self._lock:
            revision = self._require_revision(session.ontology_revision_id)
            view = self._require_profile_view(session.profile_view_id)
            if view.profile_id != session.profile_id:
                raise OntologyIntegrityError(
                    "RESTORED_SESSION_PROFILE_MISMATCH",
                    "永続化 session とプロファイル範囲が一致しません。",
                )
            if view.ontology_revision_id != revision.id:
                raise OntologyIntegrityError(
                    "RESTORED_SESSION_REVISION_MISMATCH",
                    "永続化 session と Ontology revision が一致しません。",
                )
            if session.intents:
                self._current_intent(session)
            if session.current_sql_artifact_id is not None:
                self._current_artifact(session)
            self._sessions[session.id] = _copy_model(session)
            return _copy_model(session)

    def apply_intent_patch(self, session_id: str, patch: GraphPatch) -> QuerySession:
        with self._lock:
            session = self._require_session(session_id)
            self._assert_session_mutable(session)
            if patch.base_version != session.current_intent_version:
                raise OntologyVersionConflictError(
                    "INTENT_VERSION_CONFLICT",
                    "質問の解釈が別の操作で更新されています。最新版を再読込してください。",
                )
            current = self._current_intent(session)
            payload = current.model_dump(mode="python")
            for operation in patch.operations:
                self._apply_patch_operation(payload, operation)
            payload.update(
                {
                    "version": current.version + 1,
                    "question_original": session.original_question,
                    "profile_view_id": session.profile_view_id,
                    "ontology_revision_id": session.ontology_revision_id,
                    "created_at": utc_now(),
                }
            )
            try:
                updated = type(current).model_validate(payload)
            except ValidationError as exc:
                raise OntologyIntegrityError(
                    "INTENT_PATCH_INVALID", f"質問の解釈を更新できません: {exc.errors()[0]['msg']}"
                ) from exc

            session.intents.append(updated)
            session.current_intent_version = updated.version
            session.intent_confirmed_version = None
            session.current_sql_artifact_id = None
            session.sql_confirmation = None
            session.execution = None
            session.status = QuerySessionStatus.AWAITING_INTENT_CONFIRMATION
            session.updated_at = utc_now()
            return _copy_model(session)

    def create_requirement_session(
        self,
        request: QuerySessionCreate,
        *,
        draft: SqlRequirementDraft,
        usage: RunModelUsage | None = None,
    ) -> QuerySession:
        """回答前の確認（guided）の session を作る（質問の解釈の intent は持たない。#1439）。

        `usage` は下書きの LLM の呼び出しの利用量（#3552）。
        """

        with self._lock:
            view = self._require_profile_view(request.profile_view_id)
            if view.archived:
                raise OntologyGateBlockedError(
                    "PROFILE_VIEW_ARCHIVED",
                    "アーカイブ済みのプロファイル範囲は利用できません。",
                )
            if request.profile_id != view.profile_id:
                raise OntologyIntegrityError(
                    "PROFILE_VIEW_MISMATCH",
                    "指定した業務プロファイルとオントロジー範囲が一致しません。",
                )
            if request.ontology_revision_id != view.ontology_revision_id:
                raise OntologyIntegrityError(
                    "ONTOLOGY_REVISION_MISMATCH",
                    "指定した Ontology revision とプロファイル範囲が一致しません。",
                )
            self._require_revision(request.ontology_revision_id)
            session = QuerySession(
                id=f"query_session_{uuid4().hex}",
                profile_id=request.profile_id,
                profile_view_id=request.profile_view_id,
                ontology_revision_id=request.ontology_revision_id,
                business_release_id=request.business_release_id,
                status=QuerySessionStatus.AWAITING_INTENT_CONFIRMATION,
                original_question=request.question,
                current_intent_version=1,
                intents=[],
                clarification_mode=ClarificationMode.GUIDED,
                requirement=_copy_model(draft),
                requirement_usage=usage,
                actor_user_uuid=request.actor_user_uuid,
                actor_is_system_admin=request.actor_is_system_admin,
                chat=request.chat,
                chat_previous_job_id=request.chat_previous_job_id,
                conversation_history=[_copy_model(turn) for turn in request.conversation_history],
            )
            self._sessions[session.id] = session
            return _copy_model(session)

    def update_requirement(
        self,
        session_id: str,
        *,
        base_version: int,
        draft: SqlRequirementDraft,
        answer: RequirementAnswerRecord,
        review_round: int,
        usage: RunModelUsage | None = None,
    ) -> QuerySession:
        """回答（と LLM の再検査）で更新した下書きを、新しい版として保存する。

        `usage` は再検査の LLM の呼び出しの利用量で、session の利用量に足す（#3552）。
        """

        with self._lock:
            session = self._require_session(session_id)
            self._assert_session_mutable(session)
            if session.clarification_mode != ClarificationMode.GUIDED:
                raise OntologyStateConflictError(
                    "GUIDED_CLARIFICATION_NOT_ENABLED",
                    "この query session では AI 要件確認を開始していません。",
                )
            if base_version != session.current_intent_version:
                raise OntologyVersionConflictError(
                    "INTENT_VERSION_CONFLICT",
                    "確認の内容が別の操作で更新されています。最新版を再読込してください。",
                )
            session.requirement = _copy_model(draft)
            session.requirement_answers.append(_copy_model(answer))
            session.requirement_review_round = review_round
            session.requirement_usage = add_run_usage(session.requirement_usage, usage)
            session.current_intent_version = base_version + 1
            session.updated_at = utc_now()
            return _copy_model(session)

    def apply_requirement(self, session_id: str, *, base_version: int) -> QuerySession:
        """確認した要件を SQL の生成に反映し、session を閉じる（以後は回答できない）。"""

        with self._lock:
            session = self._require_session(session_id)
            self._assert_session_mutable(session)
            if (
                session.clarification_mode != ClarificationMode.GUIDED
                or session.requirement is None
            ):
                raise OntologyStateConflictError(
                    "GUIDED_CLARIFICATION_NOT_ENABLED",
                    "この query session では AI 要件確認を開始していません。",
                )
            if base_version != session.current_intent_version:
                raise OntologyVersionConflictError(
                    "INTENT_VERSION_CONFLICT",
                    "確認の内容が別の操作で更新されています。最新版を再読込してください。",
                )
            now = utc_now()
            session.status = QuerySessionStatus.DONE
            session.requirement_applied_at = now
            session.updated_at = now
            return _copy_model(session)

    def cancel_session(self, session_id: str) -> QuerySession:
        """利用者が終了した未実行 session を明示的な終端状態へ遷移させる。"""

        with self._lock:
            session = self._require_session(session_id)
            if session.status == QuerySessionStatus.CANCELLED:
                return _copy_model(session)
            if session.status in {QuerySessionStatus.EXECUTING, QuerySessionStatus.DONE}:
                raise OntologyStateConflictError(
                    "SESSION_CANNOT_BE_CANCELLED",
                    "実行中または完了済みの query session は終了できません。",
                )
            session.status = QuerySessionStatus.CANCELLED
            session.cancelled_at = utc_now()
            session.updated_at = utc_now()
            return _copy_model(session)

    def confirm_intent(self, session_id: str, *, intent_version: int) -> QuerySession:
        with self._lock:
            session = self._require_session(session_id)
            self._assert_session_mutable(session)
            if intent_version != session.current_intent_version:
                raise OntologyVersionConflictError(
                    "INTENT_VERSION_CONFLICT", "最新版ではない質問の解釈は確認できません。"
                )
            intent = self._current_intent(session)
            view = self._require_profile_view(session.profile_view_id)
            scope_violations = self._intent_scope_violations(intent, view)
            if scope_violations:
                raise OntologyGateBlockedError(
                    "INTENT_SCOPE_INVALID",
                    "質問の解釈にプロファイル範囲外、未承認、または"
                    "物理 mapping のない要素があります。",
                    finding_codes=scope_violations,
                )
            unresolved = [
                item for item in intent.ambiguities if item.blocking and not item.resolved
            ]
            if unresolved:
                raise OntologyGateBlockedError(
                    "INTENT_AMBIGUITY_UNRESOLVED",
                    "未解決の曖昧さがあります。質問の解釈を修正してから確認してください。",
                    finding_codes=[item.code for item in unresolved],
                )
            if len(intent.entities) > 1:
                selected = next(
                    (path for path in intent.candidate_paths if path.id == intent.selected_path_id),
                    None,
                )
                if selected is None or not selected.approved:
                    raise OntologyGateBlockedError(
                        "INTENT_PATH_NOT_APPROVED",
                        "複数の業務対象を結ぶ、承認済みの関係 path を選択してください。",
                    )
            session.intent_confirmed_version = intent_version
            session.status = QuerySessionStatus.GENERATING_SQL
            session.updated_at = utc_now()
            return _copy_model(session)

    # --- SQL semantic graph / validation / confirmation ----------------------------------

    def register_generated_sql(
        self,
        session_id: str,
        sql: str,
        *,
        generation_context_hash: str = "legacy_context",
        additional_validation: OntologyValidationReport | None = None,
    ) -> QuerySession:
        with self._lock:
            session = self._require_session(session_id)
            self._assert_session_mutable(session)
            if session.intent_confirmed_version != session.current_intent_version:
                raise OntologyGateBlockedError(
                    "INTENT_CONFIRMATION_REQUIRED",
                    "SQL 生成前に最新版の質問解釈を確認してください。",
                )
            intent = self._current_intent(session)
            view = self._require_profile_view(session.profile_view_id)
            analysis = parse_oracle_sql(
                sql,
                intent_version=intent.version,
                ontology_revision_id=session.ontology_revision_id,
            )
            findings = list(analysis.validation.findings)
            coverage = 0.0
            if analysis.graph is not None:
                domain_findings, coverage = self._validate_three_way(intent, analysis.graph, view)
                findings.extend(domain_findings)
            if additional_validation is not None:
                self._verify_additional_validation(session, sql, additional_validation)
                findings.extend(additional_validation.findings)
                coverage = max(coverage, additional_validation.intent_coverage)
            report = _make_report(
                intent_version=intent.version,
                sql_hash=sql_sha256(sql),
                ontology_revision_id=session.ontology_revision_id,
                findings=self._deduplicate_findings(findings),
                coverage=coverage,
            )
            artifact = SqlArtifact(
                id=f"sql_artifact_{uuid4().hex}",
                intent_version=intent.version,
                ontology_revision_id=session.ontology_revision_id,
                sql=sql,
                sql_hash=sql_sha256(sql),
                generation_context_hash=generation_context_hash,
                semantic_graph=analysis.graph,
                validation_report=report,
            )
            session.sql_artifacts.append(artifact)
            session.current_sql_artifact_id = artifact.id
            session.sql_confirmation = None
            session.execution = None
            session.status = QuerySessionStatus.AWAITING_SQL_CONFIRMATION
            session.updated_at = utc_now()
            return _copy_model(session)

    def confirm_sql(
        self,
        session_id: str,
        request: SqlConfirmationRequest,
    ) -> QuerySession:
        with self._lock:
            session = self._require_session(session_id)
            if session.status != QuerySessionStatus.AWAITING_SQL_CONFIRMATION:
                raise OntologyStateConflictError(
                    "SQL_NOT_AWAITING_CONFIRMATION", "現在の状態では SQL を確認できません。"
                )
            artifact = self._current_artifact(session)
            self._verify_confirmation_request(session, artifact, request)
            blockers = [
                finding.code
                for finding in artifact.validation_report.findings
                if finding.severity == ValidationSeverity.BLOCKER
            ]
            if blockers or not artifact.validation_report.is_valid:
                raise OntologyGateBlockedError(
                    "SQL_VALIDATION_BLOCKED",
                    "SQL の意味に未解決の阻断項目があります。修正または再生成してください。",
                    finding_codes=blockers,
                )
            session.sql_confirmation = SqlConfirmationBinding(**request.model_dump())
            session.updated_at = utc_now()
            return _copy_model(session)

    def authorize_execution(
        self,
        session_id: str,
        request: SqlConfirmationRequest,
        *,
        sql: str | None = None,
    ) -> SqlConfirmationBinding:
        """確認 binding を再検証し、実行状態へ原子的に遷移する。"""

        with self._lock:
            session = self._require_session(session_id)
            if session.status != QuerySessionStatus.AWAITING_SQL_CONFIRMATION:
                raise OntologyStateConflictError(
                    "SQL_NOT_READY_FOR_EXECUTION", "確認済み SQL を実行できる状態ではありません。"
                )
            artifact = self._current_artifact(session)
            self._verify_confirmation_request(session, artifact, request)
            binding = session.sql_confirmation
            if binding is None:
                raise OntologyGateBlockedError(
                    "SQL_CONFIRMATION_REQUIRED",
                    "SQL の業務上の意味を確認してから実行してください。",
                )
            if binding.model_dump(mode="json", exclude={"confirmed_at"}) != request.model_dump(
                mode="json"
            ):
                raise OntologyIntegrityError(
                    "CONFIRMATION_BINDING_CHANGED", "確認後に SQL binding が変更されています。"
                )
            if sql is not None and sql_sha256(sql) != binding.sql_hash:
                raise OntologyIntegrityError(
                    "SQL_HASH_MISMATCH", "確認後に SQL が変更されたため実行を中止しました。"
                )
            if _validation_hash(artifact.validation_report) != binding.validation_hash:
                raise OntologyIntegrityError(
                    "VALIDATION_HASH_MISMATCH",
                    "確認後に検証結果が変更されたため実行を中止しました。",
                )
            session.status = QuerySessionStatus.EXECUTING
            session.execution = QueryExecutionRecord(binding=_copy_model(binding))
            session.updated_at = utc_now()
            return _copy_model(binding)

    def complete_execution(
        self,
        session_id: str,
        *,
        row_count: int,
        result_ref: str = "",
    ) -> QuerySession:
        with self._lock:
            session = self._require_session(session_id)
            if session.status != QuerySessionStatus.EXECUTING or session.execution is None:
                raise OntologyStateConflictError(
                    "SESSION_NOT_EXECUTING", "実行中ではない query session を完了できません。"
                )
            session.execution.row_count = row_count
            session.execution.result_ref = result_ref
            session.execution.finished_at = utc_now()
            session.status = QuerySessionStatus.DONE
            session.updated_at = utc_now()
            return _copy_model(session)

    def fail_session(self, session_id: str, *, code: str, message_ja: str) -> QuerySession:
        with self._lock:
            session = self._require_session(session_id)
            if session.status in {QuerySessionStatus.DONE, QuerySessionStatus.CANCELLED}:
                raise OntologyStateConflictError(
                    "SESSION_ALREADY_DONE", "完了済み query session は error に変更できません。"
                )
            session.status = QuerySessionStatus.ERROR
            session.error_code = code
            session.error_message_ja = message_ja
            session.updated_at = utc_now()
            return _copy_model(session)

    # --- Improvement proposal --------------------------------------------------------------

    def create_improvement_proposal(
        self,
        session_id: str,
        *,
        title_ja: str,
        description_ja: str,
        patch: GraphPatch | None,
        kind: OntologyProposalKind = OntologyProposalKind.QUERY_EXAMPLE,
        proposal_payload: OntologyProposalPayload | None = None,
    ) -> OntologyProposal:
        with self._lock:
            session = self._require_session(session_id)
            proposal = OntologyProposal(
                id=f"ontology_proposal_{uuid4().hex}",
                session_id=session.id,
                profile_id=session.profile_id,
                base_revision_id=session.ontology_revision_id,
                title_ja=title_ja,
                description_ja=description_ja,
                kind=kind,
                proposal_payload=proposal_payload or OntologyProposalPayload(kind=kind),
                patch=patch,
            )
            self._proposals[proposal.id] = proposal
            session.proposal_ids.append(proposal.id)
            session.updated_at = utc_now()
            return _copy_model(proposal)

    def create_build_proposal(
        self,
        *,
        session_id: str,
        profile_id: str,
        base_revision_id: str,
        title_ja: str,
        description_ja: str,
        kind: OntologyProposalKind,
        proposal_payload: OntologyProposalPayload | None = None,
    ) -> OntologyProposal:
        """AI 構築 job 由来の proposal。query session に紐づかない
        (session_id は ``ontology_build:{job_id}`` の予約プレフィクス)。"""

        with self._lock:
            proposal = OntologyProposal(
                id=f"ontology_proposal_{uuid4().hex}",
                session_id=session_id,
                profile_id=profile_id,
                base_revision_id=base_revision_id,
                title_ja=title_ja,
                description_ja=description_ja,
                kind=kind,
                proposal_payload=proposal_payload or OntologyProposalPayload(kind=kind),
            )
            self._proposals[proposal.id] = proposal
            return _copy_model(proposal)

    def get_proposal(self, proposal_id: str) -> OntologyProposal:
        with self._lock:
            proposal = self._proposals.get(proposal_id)
            if proposal is None:
                raise OntologyNotFoundError(
                    "ONTOLOGY_PROPOSAL_NOT_FOUND", "Ontology 改善提案が見つかりません。"
                )
            return _copy_model(proposal)

    def list_proposals_by_profile(self, profile_id: str) -> list[OntologyProposal]:
        with self._lock:
            return sorted(
                (
                    _copy_model(proposal)
                    for proposal in self._proposals.values()
                    if proposal.profile_id == profile_id
                    and proposal.status != OntologyProposalStatus.SUPERSEDED
                ),
                key=lambda proposal: (proposal.created_at, proposal.id),
                reverse=True,
            )

    def supersede_proposals_by_profile(self, profile_id: str) -> list[OntologyProposal]:
        """当該 profile の全提案を SUPERSEDED にし、変更分のコピーを返す。

        新規 AI 構築のたびにレビュー一覧をリセットするための一括処理。永続化は
        呼び出し側(runtime)が返却した各提案を upsert して durable 化する。
        """

        with self._lock:
            changed: list[OntologyProposal] = []
            for proposal in self._proposals.values():
                if (
                    proposal.profile_id == profile_id
                    and proposal.status != OntologyProposalStatus.SUPERSEDED
                ):
                    proposal.status = OntologyProposalStatus.SUPERSEDED
                    changed.append(_copy_model(proposal))
            return changed

    def update_proposal(self, proposal: OntologyProposal) -> OntologyProposal:
        with self._lock:
            if proposal.id not in self._proposals:
                raise OntologyNotFoundError(
                    "ONTOLOGY_PROPOSAL_NOT_FOUND", "Ontology 改善提案が見つかりません。"
                )
            self._proposals[proposal.id] = _copy_model(proposal)
            return _copy_model(proposal)

    def restore_proposal(self, proposal: OntologyProposal) -> OntologyProposal:
        """監査用 proposal と session の参照を永続化 store から復元する。"""

        with self._lock:
            if proposal.id in self._proposals:
                raise OntologyVersionConflictError(
                    "ONTOLOGY_PROPOSAL_ALREADY_EXISTS",
                    "同じ Ontology 改善提案 ID は復元できません。",
                )
            return self.replace_proposal(proposal)

    def replace_proposal(self, proposal: OntologyProposal) -> OntologyProposal:
        """永続 store の最新 snapshot で in-memory proposal cache を置き換える。"""

        with self._lock:
            # AI 構築 job 由来の proposal は query session に紐づかないため
            # session binding 検証をスキップして復元する。
            if proposal.session_id.startswith("ontology_build:"):
                self._proposals[proposal.id] = _copy_model(proposal)
                return _copy_model(proposal)
            session = self._require_session(proposal.session_id)
            if (
                proposal.profile_id != session.profile_id
                or proposal.base_revision_id != session.ontology_revision_id
            ):
                raise OntologyIntegrityError(
                    "RESTORED_PROPOSAL_BINDING_MISMATCH",
                    "永続化された改善提案と query session の binding が一致しません。",
                )
            self._proposals[proposal.id] = _copy_model(proposal)
            if proposal.id not in session.proposal_ids:
                session.proposal_ids.append(proposal.id)
            return _copy_model(proposal)

    # --- Internal --------------------------------------------------------------------------

    def _deterministic_intent(
        self,
        request: QuerySessionCreate,
        view: ProfileOntologyView,
    ) -> Any:

        question_key = request.question.casefold()
        visible_nodes = [
            self._nodes[request.ontology_revision_id][node_id]
            for node_id in view.node_ids
            if node_id in self._nodes[request.ontology_revision_id]
        ]
        matched: list[OntologyNode] = []
        for node in visible_nodes:
            terms = [node.business_name_ja, node.technical_name, *node.aliases]
            if any(term and term.casefold() in question_key for term in terms):
                matched.append(node)

        entities = [
            IntentEntity(
                id=f"intent_entity_{index}",
                ontology_node_id=node.id,
                name_ja=node.business_name_ja,
                role="event" if node.kind == OntologyNodeKind.BUSINESS_EVENT else "subject",
                physical_object_ids=[
                    mapping.object_ref.node_id for mapping in node.physical_mappings
                ],
            )
            for index, node in enumerate(
                [
                    item
                    for item in matched
                    if item.kind
                    in {
                        OntologyNodeKind.BUSINESS_ENTITY,
                        OntologyNodeKind.OBJECT_TYPE,
                        OntologyNodeKind.BUSINESS_EVENT,
                    }
                ],
                start=1,
            )
        ]
        metrics = [
            IntentMetric(
                id=f"intent_metric_{index}",
                ontology_node_id=node.id,
                name_ja=node.business_name_ja,
            )
            for index, node in enumerate(
                [item for item in matched if item.kind == OntologyNodeKind.METRIC], start=1
            )
        ]
        dimensions = [
            IntentDimension(
                id=f"intent_dimension_{index}",
                ontology_node_id=node.id,
                name_ja=node.business_name_ja,
            )
            for index, node in enumerate(
                [item for item in matched if item.kind == OntologyNodeKind.PROPERTY], start=1
            )
        ]
        edges = self._edges[request.ontology_revision_id]
        candidate_paths = [
            IntentRelationshipPath(
                id=edge.id,
                name_ja=edge.relationship_name_ja,
                edge_ids=[edge.id],
                node_ids=[edge.source_node_id, edge.target_node_id],
                approved=(
                    edge.review_status == OntologyReviewStatus.APPROVED
                    and edge.id in view.allowed_path_ids
                ),
                explanation_ja=edge.description_ja,
            )
            for edge_id in view.edge_ids
            if (edge := edges.get(edge_id)) is not None
        ]
        ambiguities: list[IntentAmbiguity] = []
        if not entities and not metrics:
            ambiguities.append(
                IntentAmbiguity(
                    id="ambiguity_business_meaning",
                    code="BUSINESS_MEANING_NOT_IDENTIFIED",
                    message_ja="質問に対応する承認済みの業務対象または指標を特定できませんでした。",
                    options=[node.business_name_ja for node in visible_nodes[:10]],
                )
            )
        if len(entities) > 1 and not any(path.approved for path in candidate_paths):
            ambiguities.append(
                IntentAmbiguity(
                    id="ambiguity_relationship_path",
                    code="RELATIONSHIP_PATH_NOT_IDENTIFIED",
                    message_ja="複数の業務対象を結ぶ承認済み path を特定できませんでした。",
                    options=[path.name_ja for path in candidate_paths],
                )
            )
        approved_paths = [path for path in candidate_paths if path.approved]
        return QuestionIntentGraph(
            question_original=request.question,
            question_effective=request.question,
            profile_view_id=request.profile_view_id,
            ontology_revision_id=request.ontology_revision_id,
            entities=entities,
            metrics=metrics,
            dimensions=dimensions,
            candidate_paths=candidate_paths,
            selected_path_id=approved_paths[0].id if len(approved_paths) == 1 else None,
            ambiguities=ambiguities,
            confidence=0.7 if matched and not ambiguities else 0.25,
        )

    def _validate_three_way(
        self,
        intent: Any,
        graph: SqlSemanticGraph,
        view: ProfileOntologyView,
    ) -> tuple[list[OntologyValidationFinding], float]:
        findings: list[OntologyValidationFinding] = []

        def add(
            code: str,
            severity: ValidationSeverity,
            message_ja: str,
            *,
            sql_ids: Sequence[str] = (),
            intent_ids: Sequence[str] = (),
            ontology_ids: Sequence[str] = (),
            action: str = "",
        ) -> None:
            findings.append(
                OntologyValidationFinding(
                    id=f"finding_{hashlib.sha256(f'{code}:{len(findings)}'.encode()).hexdigest()[:16]}",
                    code=code,
                    severity=severity,
                    message_ja=message_ja,
                    sql_element_ids=list(sql_ids),
                    intent_element_ids=list(intent_ids),
                    ontology_node_ids=list(ontology_ids),
                    suggested_action_ja=action,
                )
            )

        allowed_full: set[str] = set()
        allowed_short: set[str] = set()
        allowed_node_ids: set[str] = set()
        for physical in view.physical_objects:
            # 物理 object はカタログ上の名前、SQL の表は引用規則の token で照合する（#573）。
            short = catalog_match_key(physical.object_name)
            full = (
                catalog_match_key(physical.owner, physical.object_name) if physical.owner else short
            )
            allowed_short.add(short)
            allowed_full.add(full)
            if physical.node_id:
                allowed_node_ids.add(physical.node_id)
        unauthorized = []
        for table in graph.tables:
            if table.is_cte:
                continue
            full = _sql_table_key(table)
            short = _sql_part_token(table.name, quoted=table.name_quoted)
            explicitly_qualified = bool(table.catalog or table.owner)
            if (
                explicitly_qualified
                and full not in allowed_full
                or not explicitly_qualified
                and short not in allowed_short
            ):
                unauthorized.append(table)
        if unauthorized:
            add(
                "SQL_OBJECT_OUTSIDE_PROFILE",
                ValidationSeverity.BLOCKER,
                "プロファイル範囲外の表または view が SQL に含まれています。",
                sql_ids=[item.id for item in unauthorized],
                action="業務プロファイルの対象範囲と生成 SQL を確認してください。",
            )

        intended_physical_ids = {
            object_id for entity in intent.entities for object_id in entity.physical_object_ids
        }
        intended_objects = [
            physical
            for physical in view.physical_objects
            if physical.node_id in intended_physical_ids
        ]
        intended_short_names = {
            catalog_match_key(physical.object_name) for physical in intended_objects
        }
        intended_full_names = {
            (
                catalog_match_key(physical.owner, physical.object_name)
                if physical.owner
                else catalog_match_key(physical.object_name)
            )
            for physical in intended_objects
        }
        extra_tables = [
            table
            for table in graph.tables
            if not table.is_cte
            and intended_objects
            and (
                bool(table.catalog or table.owner)
                and _sql_table_key(table) not in intended_full_names
                or not bool(table.catalog or table.owner)
                and _sql_part_token(table.name, quoted=table.name_quoted)
                not in intended_short_names
            )
        ]
        if extra_tables:
            add(
                "SQL_OBJECT_NOT_IN_INTENT",
                ValidationSeverity.BLOCKER,
                "質問の解釈で確認していない表または view が SQL に追加されています。",
                sql_ids=[item.id for item in extra_tables],
                action="関連エンティティを質問の解釈へ追加するか、SQL から対象を削除してください。",
            )

        wildcard_projections = [
            projection for projection in graph.projections if projection.contains_wildcard
        ]
        if wildcard_projections:
            add(
                "SQL_WILDCARD_COLUMN_SCOPE_UNKNOWN",
                ValidationSeverity.BLOCKER,
                "SELECT * では利用列を Profile の列 policy と照合できません。",
                sql_ids=[item.id for item in wildcard_projections],
                action="必要な列を明示してください。",
            )

        cartesian = [join for join in graph.joins if join.is_cartesian]
        if cartesian:
            add(
                "SQL_CARTESIAN_JOIN",
                ValidationSeverity.BLOCKER,
                "Join 条件がないため、直積になる関係があります。",
                sql_ids=[item.id for item in cartesian],
                action="承認済みの関係 path と Join 条件を選択してください。",
            )

        unresolved = [item for item in intent.ambiguities if item.blocking and not item.resolved]
        if unresolved:
            add(
                "INTENT_AMBIGUITY_UNRESOLVED",
                ValidationSeverity.BLOCKER,
                "質問の解釈に未解決の曖昧さが残っています。",
                intent_ids=[item.id for item in unresolved],
            )

        selected_path = next(
            (item for item in intent.candidate_paths if item.id == intent.selected_path_id), None
        )
        if graph.joins and (
            selected_path is None
            or not selected_path.approved
            or any(edge_id not in view.allowed_path_ids for edge_id in selected_path.edge_ids)
        ):
            add(
                "SQL_JOIN_PATH_NOT_APPROVED",
                ValidationSeverity.BLOCKER,
                "SQL の Join に対応する承認済みの関係 path が選択されていません。",
                sql_ids=[item.id for item in graph.joins],
            )
        if selected_path is not None and selected_path.edge_ids and not graph.joins:
            add(
                "SQL_JOIN_REQUIRED_BY_INTENT",
                ValidationSeverity.BLOCKER,
                "質問で確認した関係 path に必要な Join が SQL にありません。",
                intent_ids=[selected_path.id],
                ontology_ids=selected_path.edge_ids,
            )

        matched_relationship_count = 0
        if selected_path is not None:
            revision_edges = self._edges.get(view.ontology_revision_id, {})
            selected_edges = [
                revision_edges[edge_id]
                for edge_id in selected_path.edge_ids
                if edge_id in revision_edges
            ]
            aliases = _sql_alias_map(graph)
            actual_join_pairs = [
                _join_pairs(join.condition_sql, aliases) if join.condition_sql else None
                for join in graph.joins
            ]
            invalid_relationships: list[OntologyEdge] = []
            invalid_join_types: list[OntologyEdge] = []
            matched_join_indexes: set[int] = set()
            for edge in selected_edges:
                expected_conditions = sorted(
                    edge.join_conditions,
                    key=lambda condition: condition.ordinal,
                )
                matching_index: int | None = None
                for index, pairs in enumerate(actual_join_pairs):
                    if index in matched_join_indexes or pairs is None:
                        continue
                    if len(pairs) != len(expected_conditions) or not expected_conditions:
                        continue
                    condition_matches: list[bool] = []
                    for expected, actual in zip(expected_conditions, pairs, strict=True):
                        expected_left = _physical_column_variants(expected.left)
                        expected_right = _physical_column_variants(expected.right)
                        expected_operator = _normalize_operator(expected.operator)
                        direct = (
                            _columns_match(expected_left, set(actual.left_columns))
                            and _columns_match(expected_right, set(actual.right_columns))
                            and actual.operator == expected_operator
                        )
                        reversed_direction = (
                            _columns_match(expected_left, set(actual.right_columns))
                            and _columns_match(expected_right, set(actual.left_columns))
                            and _reverse_operator(actual.operator) == expected_operator
                        )
                        condition_matches.append(direct or reversed_direction)
                    if all(condition_matches):
                        matching_index = index
                        break
                if matching_index is None:
                    invalid_relationships.append(edge)
                    continue
                matched_join_indexes.add(matching_index)
                matched_relationship_count += 1
                allowed_types = {item.value for item in edge.allowed_join_types}
                actual_join_type = graph.joins[matching_index].join_type
                if not any(
                    (allowed == "inner" and actual_join_type == "inner")
                    or actual_join_type.startswith(allowed)
                    for allowed in allowed_types
                ):
                    invalid_join_types.append(edge)
            if len(matched_join_indexes) != len(graph.joins):
                invalid_relationships.extend(
                    edge for edge in selected_edges if edge not in invalid_relationships
                )
            if invalid_relationships:
                add(
                    "SQL_JOIN_CONDITION_NOT_APPROVED",
                    ValidationSeverity.BLOCKER,
                    "SQL の Join 条件が、選択した Ontology 関係の"
                    "複合 Join mapping と一致しません。",
                    sql_ids=[item.id for item in graph.joins],
                    ontology_ids=[item.id for item in invalid_relationships],
                    action="起点・終点の列対、複合キー順序、比較演算子を確認してください。",
                )
            if invalid_join_types:
                add(
                    "SQL_JOIN_TYPE_NOT_ALLOWED",
                    ValidationSeverity.BLOCKER,
                    "SQL の Join type が Ontology 関係で許可されていません。",
                    sql_ids=[item.id for item in graph.joins],
                    ontology_ids=[item.id for item in invalid_join_types],
                )
            critical = [
                edge
                for edge in selected_edges
                if edge.cardinality == RelationshipCardinality.MANY_TO_MANY
            ]
            possible = [
                edge
                for edge in selected_edges
                if edge.cardinality == RelationshipCardinality.ONE_TO_MANY
            ]
            if critical:
                add(
                    "SQL_CRITICAL_FAN_OUT_RISK",
                    ValidationSeverity.BLOCKER,
                    "多対多の関係により集計値が重複する重大な fan-out リスクがあります。",
                    ontology_ids=[item.id for item in critical],
                )
            elif possible and graph.aggregates:
                add(
                    "SQL_FAN_OUT_REVIEW_REQUIRED",
                    ValidationSeverity.WARNING,
                    "一対多の関係を跨いだ集計です。粒度と重複排除を確認してください。",
                    ontology_ids=[item.id for item in possible],
                )

        aliases = _sql_alias_map(graph)
        revision_nodes = self._nodes.get(view.ontology_revision_id, {})

        physical_tables = [table for table in graph.tables if not table.is_cte]
        unqualified_columns = [
            column
            for column in graph.columns
            if len(physical_tables) > 1 and "." not in column.expression_sql.replace('"', "")
        ]
        if unqualified_columns:
            add(
                "SQL_UNQUALIFIED_COLUMN_AMBIGUOUS",
                ValidationSeverity.BLOCKER,
                "複数表の SQL に修飾されていない列があり、対象表を決定できません。",
                sql_ids=[item.id for item in unqualified_columns],
                action="owner、表 alias、列名を明示してください。",
            )

        def referenced_variants(values: Sequence[str]) -> set[str]:
            result: set[str] = set()
            for value in values:
                result.update(_sql_column_variants(value, aliases))
            return result

        sql_column_targets = [
            _sql_column_variants(column.expression_sql, aliases) for column in graph.columns
        ]
        predicate_targets = [
            referenced_variants(predicate.referenced_columns)
            for predicate in [*graph.filters, *graph.having]
        ]
        aggregate_targets = [
            referenced_variants(aggregate.referenced_columns) for aggregate in graph.aggregates
        ]
        group_targets = [referenced_variants(group.referenced_columns) for group in graph.groups]
        order_targets = [referenced_variants(order.referenced_columns) for order in graph.orders]

        predicate_atoms: list[_PredicateAtom] = []
        unverifiable_predicates: list[str] = []
        for predicate in [*graph.filters, *graph.having]:
            atoms, complete = _predicate_atoms(
                predicate.expression_sql,
                element_id=predicate.id,
                aliases=aliases,
            )
            if not complete:
                unverifiable_predicates.append(predicate.id)
            predicate_atoms.extend(atoms)
        if unverifiable_predicates:
            add(
                "SQL_FILTER_EXPRESSION_UNVERIFIABLE",
                ValidationSeverity.BLOCKER,
                "絞り込み式を列・演算子・値へ安全に分解できません。",
                sql_ids=unverifiable_predicates,
                action="AND で結んだ単純比較、IN、BETWEEN、IS NULL に分解してください。",
            )

        def resolve_policy_target(column_key: str) -> tuple[set[str], str | None]:
            node = revision_nodes.get(column_key)
            if node is None:
                normalized_key = object_match_key(column_key)
                node = next(
                    (
                        candidate
                        for candidate in revision_nodes.values()
                        if object_match_key(candidate.technical_name) == normalized_key
                    ),
                    None,
                )
            if node is not None:
                return _node_column_variants(node), node.id
            if column_key.startswith(("node_", "physical_")):
                return set(), None
            return _raw_column_variants(column_key), None

        policy_targets: list[tuple[str, Any, set[str], str | None]] = []
        unresolved_policies: list[str] = []
        for column_key, policy in view.column_policies.items():
            targets, node_id = resolve_policy_target(column_key)
            if not targets:
                unresolved_policies.append(column_key)
            policy_targets.append((column_key, policy, targets, node_id))
        if unresolved_policies:
            add(
                "PROFILE_COLUMN_POLICY_UNRESOLVED",
                ValidationSeverity.BLOCKER,
                "Profile の列 policy を物理列へ解決できません。",
                action="有効な column node ID または technical_name を指定してください。",
            )

        non_queryable: list[str] = []
        non_filterable: list[str] = []
        non_groupable: list[str] = []
        non_aggregatable: list[str] = []
        required_filter_targets: list[tuple[str, set[str]]] = []
        for column_key, policy, targets, _node_id in policy_targets:
            if not targets:
                continue
            if not policy.queryable and any(
                _columns_match(targets, actual) for actual in sql_column_targets
            ):
                non_queryable.append(column_key)
            if not policy.filterable and any(
                _columns_match(targets, actual) for actual in predicate_targets
            ):
                non_filterable.append(column_key)
            if not policy.groupable and any(
                _columns_match(targets, actual) for actual in group_targets
            ):
                non_groupable.append(column_key)
            if not policy.aggregatable and any(
                _columns_match(targets, actual) for actual in aggregate_targets
            ):
                non_aggregatable.append(column_key)
            if policy.required_filter:
                required_filter_targets.append((column_key, targets))
        if non_queryable:
            add(
                "SQL_COLUMN_NOT_QUERYABLE",
                ValidationSeverity.BLOCKER,
                "Profile の列 policy で利用を許可されていない列が含まれています。",
                action="列を削除するか、管理者へ利用範囲の見直しを依頼してください。",
            )
        if non_filterable:
            add(
                "SQL_COLUMN_NOT_FILTERABLE",
                ValidationSeverity.BLOCKER,
                "Profile の列 policy で filter 利用を許可されていない列があります。",
            )
        if non_groupable:
            add(
                "SQL_COLUMN_NOT_GROUPABLE",
                ValidationSeverity.BLOCKER,
                "Profile の列 policy で GROUP BY 利用を許可されていない列があります。",
            )
        if non_aggregatable:
            add(
                "SQL_COLUMN_NOT_AGGREGATABLE",
                ValidationSeverity.BLOCKER,
                "Profile の列 policy で集計利用を許可されていない列があります。",
            )

        used_predicate_atoms: set[str] = set()
        missing_filter_ids: list[str] = []
        mismatched_filter_ids: list[str] = []
        unverifiable_filter_ids: list[str] = []
        matched_filter_count = 0
        for intent_filter in intent.filters:
            targets = _node_column_variants(revision_nodes.get(intent_filter.property_node_id))
            if not targets:
                unverifiable_filter_ids.append(intent_filter.id)
                continue
            expected_operator = _normalize_operator(intent_filter.operator)
            value_known, expected_value = _canonical_intent_value(
                intent_filter.value,
                intent_filter.value_type,
            )
            if expected_operator == "BETWEEN" and value_known:
                if (
                    isinstance(expected_value, tuple)
                    and expected_value[0] == "list"
                    and len(expected_value[1]) == 2
                ):
                    expected_value = (
                        "range",
                        expected_value[1][0],
                        expected_value[1][1],
                    )
                else:
                    value_known = False
            filter_candidates = [
                atom
                for atom in predicate_atoms
                if atom.element_id not in used_predicate_atoms
                and _columns_match(targets, set(atom.columns))
            ]
            if not filter_candidates:
                missing_filter_ids.append(intent_filter.id)
                continue
            matching_filter_atom = next(
                (
                    atom
                    for atom in filter_candidates
                    if atom.operator == expected_operator
                    and atom.value_known
                    and value_known
                    and atom.value == expected_value
                ),
                None,
            )
            if matching_filter_atom is None:
                if not value_known or any(not atom.value_known for atom in filter_candidates):
                    unverifiable_filter_ids.append(intent_filter.id)
                else:
                    mismatched_filter_ids.append(intent_filter.id)
                used_predicate_atoms.update(atom.element_id for atom in filter_candidates)
                continue
            used_predicate_atoms.add(matching_filter_atom.element_id)
            matched_filter_count += 1
        if missing_filter_ids:
            add(
                "SQL_INTENT_FILTER_MISSING",
                ValidationSeverity.BLOCKER,
                "質問で確認した対象列の絞り込み条件が SQL にありません。",
                intent_ids=missing_filter_ids,
            )
        if mismatched_filter_ids:
            add(
                "SQL_INTENT_FILTER_MISMATCH",
                ValidationSeverity.BLOCKER,
                "質問で確認した filter の演算子または値と SQL が一致しません。",
                intent_ids=mismatched_filter_ids,
            )
        if unverifiable_filter_ids:
            add(
                "SQL_INTENT_FILTER_UNVERIFIABLE",
                ValidationSeverity.BLOCKER,
                "質問の filter を対象列・演算子・値として安全に照合できません。",
                intent_ids=unverifiable_filter_ids,
            )

        matched_time_count = 0
        time_range = intent.time_range
        if time_range is not None:
            time_targets = _node_column_variants(revision_nodes.get(time_range.property_node_id))
            if not time_targets or time_range.relative_expression:
                add(
                    "SQL_TIME_FILTER_UNVERIFIABLE",
                    ValidationSeverity.BLOCKER,
                    "相対期間または期間対象列を決定的に照合できません。",
                )
            elif time_range.start is None and time_range.end is None:
                add(
                    "SQL_TIME_FILTER_UNVERIFIABLE",
                    ValidationSeverity.BLOCKER,
                    "期間の開始・終了が指定されていないため SQL と照合できません。",
                )
            else:
                start_value = (
                    _canonical_intent_value(time_range.start, "string")[1]
                    if time_range.start is not None
                    else None
                )
                end_value = (
                    _canonical_intent_value(time_range.end, "string")[1]
                    if time_range.end is not None
                    else None
                )
                time_candidates = [
                    atom
                    for atom in predicate_atoms
                    if atom.element_id not in used_predicate_atoms
                    and _columns_match(time_targets, set(atom.columns))
                ]
                between = next(
                    (
                        atom
                        for atom in time_candidates
                        if atom.operator == "BETWEEN"
                        and atom.value_known
                        and atom.value == ("range", start_value, end_value)
                        and start_value is not None
                        and end_value is not None
                        and time_range.start_inclusive
                        and time_range.end_inclusive
                    ),
                    None,
                )
                matched_bounds = 0
                if between is not None:
                    used_predicate_atoms.add(between.element_id)
                    matched_bounds = 2
                else:
                    expected_bounds = [
                        (
                            (
                                ">=" if time_range.start_inclusive else ">",
                                start_value,
                            )
                            if start_value is not None
                            else None
                        ),
                        (
                            (
                                "<=" if time_range.end_inclusive else "<",
                                end_value,
                            )
                            if end_value is not None
                            else None
                        ),
                    ]
                    for expected_bound in expected_bounds:
                        if expected_bound is None:
                            continue
                        match = next(
                            (
                                atom
                                for atom in time_candidates
                                if atom.element_id not in used_predicate_atoms
                                and atom.operator == expected_bound[0]
                                and atom.value_known
                                and atom.value == expected_bound[1]
                            ),
                            None,
                        )
                        if match is not None:
                            used_predicate_atoms.add(match.element_id)
                            matched_bounds += 1
                expected_bound_count = int(start_value is not None) + int(end_value is not None)
                if matched_bounds == expected_bound_count:
                    matched_time_count = 1
                elif time_candidates:
                    add(
                        "SQL_TIME_FILTER_MISMATCH",
                        ValidationSeverity.BLOCKER,
                        "期間の対象列、境界演算子、または境界値が質問の解釈と一致しません。",
                    )
                    used_predicate_atoms.update(atom.element_id for atom in time_candidates)
                else:
                    add(
                        "SQL_TIME_FILTER_MISSING",
                        ValidationSeverity.BLOCKER,
                        "質問で確認した期間条件が SQL にありません。",
                    )

        missing_required_filters: list[str] = []
        for column_key, targets in required_filter_targets:
            matches = [
                atom for atom in predicate_atoms if _columns_match(targets, set(atom.columns))
            ]
            if not matches:
                missing_required_filters.append(column_key)
                continue
            if not any(atom.element_id in used_predicate_atoms for atom in matches):
                used_predicate_atoms.add(matches[0].element_id)
        if missing_required_filters:
            add(
                "SQL_REQUIRED_FILTER_MISSING",
                ValidationSeverity.BLOCKER,
                "Profile で必須とされた対象列の絞り込み条件が SQL にありません。",
                action="必須 filter を質問の解釈へ追加してください。",
            )

        extra_predicate_atoms = [
            atom for atom in predicate_atoms if atom.element_id not in used_predicate_atoms
        ]
        if extra_predicate_atoms:
            add(
                "SQL_UNREQUESTED_FILTER_ADDED",
                ValidationSeverity.BLOCKER,
                "質問の解釈または必須 policy にない対象列・値の filter が追加されています。",
                sql_ids=[
                    atom.element_id.split(":", maxsplit=1)[0] for atom in extra_predicate_atoms
                ],
            )

        matched_metric_count = 0
        used_aggregate_indexes: set[int] = set()
        for metric in intent.metrics:
            targets = _node_column_variants(revision_nodes.get(metric.ontology_node_id))
            expected_function = _normalize_operator(metric.aggregation).replace(" ", "_")
            if not expected_function:
                add(
                    "SQL_METRIC_AGGREGATION_UNVERIFIABLE",
                    ValidationSeverity.BLOCKER,
                    "指標の集計関数が未指定のため SQL と照合できません。",
                    intent_ids=[metric.id],
                )
                continue
            metric_candidate_indexes = [
                index
                for index, actual_targets in enumerate(aggregate_targets)
                if index not in used_aggregate_indexes
                and (
                    _columns_match(targets, actual_targets)
                    or (
                        not targets
                        and expected_function in {"COUNT", "COUNT_DISTINCT"}
                        and "*" in graph.aggregates[index].expression_sql
                    )
                )
            ]
            if not metric_candidate_indexes:
                add(
                    "SQL_METRIC_AGGREGATION_MISSING",
                    ValidationSeverity.BLOCKER,
                    "質問で確認した指標の対象列が集計されていません。",
                    intent_ids=[metric.id],
                )
                continue

            def aggregate_matches(
                index: int,
                expected: str = expected_function,
            ) -> bool:
                aggregate = graph.aggregates[index]
                if expected == "COUNT_DISTINCT":
                    return (
                        aggregate.function_name == "COUNT"
                        and "DISTINCT" in aggregate.expression_sql.upper()
                    )
                return aggregate.function_name == expected

            matching_aggregate_index = next(
                (index for index in metric_candidate_indexes if aggregate_matches(index)),
                None,
            )
            if matching_aggregate_index is None:
                add(
                    "SQL_METRIC_AGGREGATION_MISMATCH",
                    ValidationSeverity.BLOCKER,
                    "指標の対象列は存在しますが、集計関数が質問の解釈と一致しません。",
                    intent_ids=[metric.id],
                    sql_ids=[graph.aggregates[index].id for index in metric_candidate_indexes],
                )
                used_aggregate_indexes.update(metric_candidate_indexes)
                continue
            used_aggregate_indexes.add(matching_aggregate_index)
            matched_metric_count += 1
        extra_aggregates = [
            aggregate
            for index, aggregate in enumerate(graph.aggregates)
            if index not in used_aggregate_indexes
        ]
        if extra_aggregates:
            add(
                "SQL_UNREQUESTED_AGGREGATION_ADDED",
                ValidationSeverity.BLOCKER,
                "質問の解釈にない対象列または集計関数が SQL に追加されています。",
                sql_ids=[item.id for item in extra_aggregates],
            )

        matched_dimension_count = 0
        used_group_indexes: set[int] = set()
        granularity_tokens = {
            "day": {"DAY", "DD"},
            "daily": {"DAY", "DD"},
            "month": {"MONTH", "MM"},
            "monthly": {"MONTH", "MM"},
            "year": {"YEAR", "YYYY", "YY"},
            "yearly": {"YEAR", "YYYY", "YY"},
            "hour": {"HOUR", "HH"},
        }
        for dimension in intent.dimensions:
            targets = _node_column_variants(revision_nodes.get(dimension.ontology_node_id))
            group_candidate_indexes = [
                index
                for index, actual_targets in enumerate(group_targets)
                if index not in used_group_indexes and _columns_match(targets, actual_targets)
            ]
            if not group_candidate_indexes:
                add(
                    "SQL_DIMENSION_GRAIN_MISSING",
                    ValidationSeverity.BLOCKER,
                    "質問で確認した dimension の対象列が GROUP BY にありません。",
                    intent_ids=[dimension.id],
                )
                continue
            matching_group_index = group_candidate_indexes[0]
            if dimension.granularity:
                normalized_granularity = dimension.granularity.strip().lower()
                tokens = granularity_tokens.get(
                    normalized_granularity,
                    {dimension.granularity.replace('"', "").strip().upper()},
                )
                expression = graph.groups[matching_group_index].expression_sql.upper()
                if not any(token in expression for token in tokens):
                    add(
                        "SQL_DIMENSION_GRANULARITY_MISMATCH",
                        ValidationSeverity.BLOCKER,
                        "dimension の時間粒度を SQL の GROUP BY 式で確認できません。",
                        intent_ids=[dimension.id],
                        sql_ids=[graph.groups[matching_group_index].id],
                    )
                    used_group_indexes.update(group_candidate_indexes)
                    continue
            used_group_indexes.add(matching_group_index)
            matched_dimension_count += 1
        extra_groups = [
            group for index, group in enumerate(graph.groups) if index not in used_group_indexes
        ]
        if extra_groups:
            add(
                "SQL_UNREQUESTED_GROUP_ADDED",
                ValidationSeverity.BLOCKER,
                "質問の解釈にない GROUP BY 対象が SQL に追加されています。",
                sql_ids=[item.id for item in extra_groups],
            )

        intent_targets_by_id: dict[str, set[str]] = {}
        for element in [*intent.entities, *intent.metrics, *intent.dimensions]:
            intent_targets_by_id[element.id] = _node_column_variants(
                revision_nodes.get(element.ontology_node_id)
            )
        matched_sort_count = 0
        used_order_indexes: set[int] = set()
        for intent_sort in intent.sorts:
            targets = intent_targets_by_id.get(intent_sort.target_id, set())
            order_candidate_indexes = [
                index
                for index, actual_targets in enumerate(order_targets)
                if index not in used_order_indexes and _columns_match(targets, actual_targets)
            ]
            if not targets or not order_candidate_indexes:
                add(
                    "SQL_INTENT_SORT_MISSING",
                    ValidationSeverity.BLOCKER,
                    "質問で確認した sort 対象を ORDER BY で特定できません。",
                    intent_ids=[intent_sort.target_id],
                )
                continue
            matching_order_index = next(
                (
                    index
                    for index in order_candidate_indexes
                    if graph.orders[index].direction == intent_sort.direction
                ),
                None,
            )
            if matching_order_index is None:
                add(
                    "SQL_INTENT_SORT_MISMATCH",
                    ValidationSeverity.BLOCKER,
                    "ORDER BY の対象列または昇降順が質問の解釈と一致しません。",
                    intent_ids=[intent_sort.target_id],
                    sql_ids=[graph.orders[index].id for index in order_candidate_indexes],
                )
                used_order_indexes.update(order_candidate_indexes)
                continue
            used_order_indexes.add(matching_order_index)
            matched_sort_count += 1
        extra_orders = [
            order for index, order in enumerate(graph.orders) if index not in used_order_indexes
        ]
        if extra_orders:
            add(
                "SQL_UNREQUESTED_SORT_ADDED",
                ValidationSeverity.BLOCKER,
                "質問の解釈にない ORDER BY が SQL に追加されています。",
                sql_ids=[item.id for item in extra_orders],
            )
        if intent.limit is not None and (graph.limit is None or graph.limit > intent.limit):
            add(
                "SQL_LIMIT_EXCEEDS_INTENT",
                ValidationSeverity.BLOCKER,
                "SQL の取得件数上限が質問で確認した上限を超えています。",
            )

        total_elements = (
            len(intent.entities)
            + len(intent.metrics)
            + len(intent.dimensions)
            + len(intent.filters)
            + (1 if intent.selected_path_id else 0)
            + (1 if intent.time_range is not None else 0)
            + len(intent.sorts)
            + (1 if intent.limit is not None else 0)
        )
        covered = 0
        actual_physical_ids = {
            physical.node_id
            for physical in view.physical_objects
            for table in graph.tables
            if not table.is_cte
            and (
                bool(table.catalog or table.owner)
                and _sql_table_key(table)
                == (
                    catalog_match_key(physical.owner, physical.object_name)
                    if physical.owner
                    else catalog_match_key(physical.object_name)
                )
                or not bool(table.catalog or table.owner)
                and _sql_part_token(table.name, quoted=table.name_quoted)
                == catalog_match_key(physical.object_name)
            )
        }
        for entity in intent.entities:
            if set(entity.physical_object_ids) & actual_physical_ids & allowed_node_ids:
                covered += 1
        covered += matched_metric_count
        covered += matched_dimension_count
        covered += matched_filter_count
        covered += matched_time_count
        covered += matched_sort_count
        if (
            selected_path is not None
            and selected_path.edge_ids
            and matched_relationship_count == len(selected_path.edge_ids)
        ):
            covered += 1
        if intent.limit is not None and graph.limit is not None and graph.limit <= intent.limit:
            covered += 1
        coverage = covered / total_elements if total_elements else 1.0

        if not findings:
            add(
                "ONTOLOGY_THREE_WAY_VALIDATED",
                ValidationSeverity.PASS,
                "質問の解釈、SQL の意味、プロファイル範囲が一致しています。",
            )
        elif not any(item.severity == ValidationSeverity.BLOCKER for item in findings):
            add(
                "ONTOLOGY_THREE_WAY_VALIDATED_WITH_WARNINGS",
                ValidationSeverity.PASS,
                "三方 validation を完了しました。警告内容を確認してください。",
            )
        return findings, coverage

    def _intent_scope_violations(
        self,
        intent: Any,
        view: ProfileOntologyView,
    ) -> list[str]:
        """GraphPatch 後の intent が承認済み profile scope を越えていないか検証する。"""

        nodes = self._nodes.get(view.ontology_revision_id, {})
        edges = self._edges.get(view.ontology_revision_id, {})
        view_node_ids = set(view.node_ids)
        view_edge_ids = set(view.edge_ids)
        allowed_path_ids = set(view.allowed_path_ids)
        physical_ids = {item.node_id for item in view.physical_objects}
        violations: set[str] = set()

        referenced_node_ids = [
            item.ontology_node_id
            for item in [*intent.entities, *intent.metrics, *intent.dimensions]
            if item.ontology_node_id
        ]
        referenced_node_ids.extend(
            item.property_node_id for item in intent.filters if item.property_node_id
        )
        if intent.time_range is not None and intent.time_range.property_node_id:
            referenced_node_ids.append(intent.time_range.property_node_id)
        for node_id in referenced_node_ids:
            node = nodes.get(node_id)
            if node is None or node_id not in view_node_ids:
                violations.add("INTENT_NODE_OUTSIDE_PROFILE")
            elif node.review_status != OntologyReviewStatus.APPROVED:
                violations.add("INTENT_NODE_NOT_APPROVED")

        for entity in intent.entities:
            requested_ids = set(entity.physical_object_ids)
            if not requested_ids:
                violations.add("INTENT_ENTITY_MAPPING_MISSING")
                continue
            if not requested_ids.issubset(physical_ids):
                violations.add("INTENT_PHYSICAL_OBJECT_OUTSIDE_PROFILE")
            node = nodes.get(entity.ontology_node_id) if entity.ontology_node_id else None
            if node is not None:
                declared_ids = {
                    mapping.object_ref.node_id
                    for mapping in node.physical_mappings
                    if mapping.object_ref.node_id
                }
                if node.kind in {OntologyNodeKind.TABLE, OntologyNodeKind.VIEW}:
                    declared_ids.add(node.id)
                if not requested_ids.issubset(declared_ids):
                    violations.add("INTENT_ENTITY_MAPPING_MISMATCH")

        if any(not item.property_node_id for item in intent.filters):
            violations.add("INTENT_FILTER_PROPERTY_MISSING")
        if intent.time_range is not None and not intent.time_range.property_node_id:
            violations.add("INTENT_TIME_PROPERTY_MISSING")

        valid_sort_targets = {
            item.id for item in [*intent.entities, *intent.metrics, *intent.dimensions]
        } | view_node_ids
        if any(item.target_id not in valid_sort_targets for item in intent.sorts):
            violations.add("INTENT_SORT_TARGET_INVALID")

        for path in intent.candidate_paths:
            if not set(path.node_ids).issubset(view_node_ids):
                violations.add("INTENT_PATH_NODE_OUTSIDE_PROFILE")
            path_edges = [edges.get(edge_id) for edge_id in path.edge_ids]
            if (
                not path.edge_ids
                or not set(path.edge_ids).issubset(view_edge_ids)
                or any(edge is None for edge in path_edges)
            ):
                violations.add("INTENT_PATH_OUTSIDE_PROFILE")
                continue
            if path.approved and (
                not set(path.edge_ids).issubset(allowed_path_ids)
                or any(
                    edge is None or edge.review_status != OntologyReviewStatus.APPROVED
                    for edge in path_edges
                )
            ):
                violations.add("INTENT_PATH_NOT_APPROVED")
        return sorted(violations)

    def _verify_additional_validation(
        self,
        session: QuerySession,
        sql: str,
        report: OntologyValidationReport,
    ) -> None:
        if (
            report.sql_hash != sql_sha256(sql)
            or report.intent_version != session.current_intent_version
            or report.ontology_revision_id != session.ontology_revision_id
        ):
            raise OntologyIntegrityError(
                "ADDITIONAL_VALIDATION_BINDING_MISMATCH",
                "追加 validation と session / SQL の binding が一致しません。",
            )

    def _verify_confirmation_request(
        self,
        session: QuerySession,
        artifact: SqlArtifact,
        request: SqlConfirmationRequest,
    ) -> None:
        expected = {
            "artifact_id": artifact.id,
            "ontology_revision_id": session.ontology_revision_id,
            "intent_version": session.current_intent_version,
            "sql_hash": artifact.sql_hash,
            "validation_hash": artifact.validation_report.validation_hash,
            "generation_context_hash": artifact.generation_context_hash,
        }
        actual = request.model_dump(mode="json")
        if actual != expected:
            changed = [key for key, value in expected.items() if actual.get(key) != value]
            raise OntologyIntegrityError(
                "CONFIRMATION_BINDING_MISMATCH",
                f"確認対象が変更されています ({', '.join(changed)})。再確認してください。",
            )

    def _apply_patch_operation(
        self, payload: dict[str, Any], operation: GraphPatchOperation
    ) -> None:
        tokens = [
            token.replace("~1", "/").replace("~0", "~") for token in operation.path[1:].split("/")
        ]
        if not tokens or not tokens[0] or tokens[0] not in self._PATCHABLE_ROOTS:
            raise OntologyIntegrityError(
                "INTENT_PATCH_PATH_FORBIDDEN", "この項目は query session から変更できません。"
            )
        parent: Any = payload
        for token in tokens[:-1]:
            if isinstance(parent, list):
                try:
                    parent = parent[int(token)]
                except (ValueError, IndexError) as exc:
                    raise OntologyIntegrityError(
                        "INTENT_PATCH_PATH_NOT_FOUND", "変更対象の path が見つかりません。"
                    ) from exc
            elif isinstance(parent, dict) and token in parent:
                parent = parent[token]
            else:
                raise OntologyIntegrityError(
                    "INTENT_PATCH_PATH_NOT_FOUND", "変更対象の path が見つかりません。"
                )
        leaf = tokens[-1]
        if isinstance(parent, list):
            if operation.op == "add" and leaf == "-":
                parent.append(deepcopy(operation.value))
                return
            try:
                index = int(leaf)
            except ValueError as exc:
                raise OntologyIntegrityError(
                    "INTENT_PATCH_INDEX_INVALID", "配列の変更位置が正しくありません。"
                ) from exc
            if operation.op == "add":
                if index < 0 or index > len(parent):
                    self._patch_not_found()
                parent.insert(index, deepcopy(operation.value))
            elif 0 <= index < len(parent):
                if operation.op == "replace":
                    parent[index] = deepcopy(operation.value)
                else:
                    parent.pop(index)
            else:
                self._patch_not_found()
            return
        if not isinstance(parent, dict):
            self._patch_not_found()
        if operation.op == "add":
            parent[leaf] = deepcopy(operation.value)
        elif leaf in parent:
            if operation.op == "replace":
                parent[leaf] = deepcopy(operation.value)
            else:
                del parent[leaf]
        else:
            self._patch_not_found()

    @staticmethod
    def _patch_not_found() -> NoReturn:
        raise OntologyIntegrityError(
            "INTENT_PATCH_PATH_NOT_FOUND", "変更対象の path が見つかりません。"
        )

    @staticmethod
    def _deduplicate_findings(
        findings: Sequence[OntologyValidationFinding],
    ) -> list[OntologyValidationFinding]:
        result: list[OntologyValidationFinding] = []
        seen: set[tuple[str, str]] = set()
        for finding in findings:
            key = (finding.code, finding.severity.value)
            if key not in seen:
                seen.add(key)
                result.append(finding)
        return result

    @staticmethod
    def _assert_session_mutable(session: QuerySession) -> None:
        if (
            session.status == QuerySessionStatus.CANCELLED
            and session.error_code == QUERY_SESSION_EXPIRED_CODE
        ):
            raise OntologyStateConflictError(
                QUERY_SESSION_EXPIRED_CODE, QUERY_SESSION_EXPIRED_MESSAGE_JA
            )
        if session.status in {
            QuerySessionStatus.EXECUTING,
            QuerySessionStatus.DONE,
            QuerySessionStatus.ERROR,
            QuerySessionStatus.CANCELLED,
        }:
            raise OntologyStateConflictError(
                "SESSION_NOT_MUTABLE", "実行中または終了済みの query session は変更できません。"
            )

    @staticmethod
    def _current_intent(session: QuerySession) -> Any:
        intent = next(
            (
                item
                for item in reversed(session.intents)
                if item.version == session.current_intent_version
            ),
            None,
        )
        if intent is None:
            raise OntologyIntegrityError(
                "CURRENT_INTENT_NOT_FOUND", "現在の質問解釈 version が見つかりません。"
            )
        return intent

    @staticmethod
    def _current_artifact(session: QuerySession) -> SqlArtifact:
        artifact = next(
            (
                item
                for item in reversed(session.sql_artifacts)
                if item.id == session.current_sql_artifact_id
            ),
            None,
        )
        if artifact is None:
            raise OntologyIntegrityError(
                "CURRENT_SQL_ARTIFACT_NOT_FOUND", "現在の SQL artifact が見つかりません。"
            )
        return artifact

    def _require_revision(self, revision_id: str) -> OntologyRevision:
        revision = self._revisions.get(revision_id)
        if revision is None:
            raise OntologyNotFoundError(
                "ONTOLOGY_REVISION_NOT_FOUND", "指定した Ontology revision が見つかりません。"
            )
        return revision

    def _require_profile_view(self, view_id: str) -> ProfileOntologyView:
        view = self._profile_views.get(view_id)
        if view is None:
            raise OntologyNotFoundError(
                "PROFILE_ONTOLOGY_VIEW_NOT_FOUND",
                "指定したプロファイル範囲が見つかりません。",
            )
        return view

    def _require_session(self, session_id: str) -> QuerySession:
        session = self._sessions.get(session_id)
        if session is None:
            raise OntologyNotFoundError(
                "QUERY_SESSION_NOT_FOUND", "指定した query session が見つかりません。"
            )
        return session


__all__ = [
    "BUSINESS_SCOPE_CHANGED_CODE",
    "BUSINESS_SCOPE_CHANGED_MESSAGE_JA",
    "OntologyGateBlockedError",
    "OntologyIntegrityError",
    "OntologyNotFoundError",
    "OntologyQuerySessionService",
    "OntologyServiceError",
    "OntologyStateConflictError",
    "OntologyVersionConflictError",
    "compute_schema_fingerprint",
    "stable_physical_node_id",
]
