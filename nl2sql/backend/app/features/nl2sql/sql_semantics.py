"""Oracle SQL を sqlglot AST から Ontology semantic graph へ変換する。

本番の安全判断で正規表現 fallback は行わない。構文を完全に解析できない SQL、複数
statement、SELECT 系以外の statement は blocker report を返し、呼び出し側がそのまま
ユーザーへ説明できるようにする。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any
from uuid import uuid4

from .ontology_models import (
    OntologyValidationFinding,
    OntologyValidationReport,
    SqlAggregate,
    SqlColumnReference,
    SqlCteDefinition,
    SqlGroupExpression,
    SqlJoinReference,
    SqlLineage,
    SqlOrderExpression,
    SqlPredicate,
    SqlProjection,
    SqlSemanticAnalysis,
    SqlSemanticGraph,
    SqlSetOperation,
    SqlSubqueryReference,
    SqlTableReference,
    SqlWindowExpression,
    ValidationSeverity,
)
from .oracle_dialect import OracleConversion
from .sql_lexing import prepare_oracle_query


def sql_sha256(sql: str) -> str:
    """確認後の置換を検出するため SQL の byte 列をそのまま hash 化する。"""

    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def _stable_element_id(prefix: str, *parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _report_hash(report: OntologyValidationReport) -> str:
    payload = report.model_dump(mode="json", exclude={"validation_hash"})
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _build_report(
    *,
    sql_hash: str,
    intent_version: int,
    ontology_revision_id: str,
    findings: list[OntologyValidationFinding],
    intent_coverage: float = 0.0,
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
        intent_coverage=intent_coverage,
        findings=findings,
        passed_count=passed,
        warning_count=warnings,
        blocker_count=blockers,
    )
    report.validation_hash = _report_hash(report)
    return report


def _blocker_analysis(
    sql: str,
    *,
    code: str,
    message_ja: str,
    intent_version: int,
    ontology_revision_id: str,
) -> SqlSemanticAnalysis:
    sql_hash = sql_sha256(sql)
    report = _build_report(
        sql_hash=sql_hash,
        intent_version=intent_version,
        ontology_revision_id=ontology_revision_id,
        findings=[
            OntologyValidationFinding(
                id=_stable_element_id("finding", code, sql_hash),
                code=code,
                severity=ValidationSeverity.BLOCKER,
                message_ja=message_ja,
                suggested_action_ja="SQL を再生成し、構文と対象文を確認してください。",
            )
        ],
    )
    return SqlSemanticAnalysis(graph=None, validation=report)


def _sql(expression: Any) -> str:
    try:
        return str(expression.sql(dialect=OracleConversion, pretty=False))
    except Exception:  # pragma: no cover - sqlglot extension expression の防御
        return str(expression)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    return str(value)


def _column_display(column: Any) -> str:
    parts = [
        _text(getattr(column, "catalog", "")),
        _text(getattr(column, "db", "")),
        _text(getattr(column, "table", "")),
        _text(getattr(column, "name", "")),
    ]
    return ".".join(part for part in parts if part)


def _column_reference_sql(column: Any) -> str:
    """列参照を SQL の表記で返す。引用された部分は `"Amount"` と引用符を残す。

    `_column_display` は引用符を外すため、`"Amount"` と `AMOUNT` を区別できない。
    `referenced_columns` を物理列と照合する側（オントロジーの SQL 接地）が引用規則で解釈できる
    よう、引用の有無を保つ（#573）。引用されていない部分は SQL に書かれたままの値になる。
    """

    parts = []
    for key in ("catalog", "db", "table", "this"):
        identifier = column.args.get(key)
        name = _text(identifier)
        if not name:
            continue
        quoted = _identifier_quoted(identifier)
        parts.append('"' + name.replace('"', '""') + '"' if quoted else name)
    return ".".join(parts)


def _columns(expression: Any, exp: Any) -> list[str]:
    if expression is None:
        return []
    values = {_column_reference_sql(column) for column in expression.find_all(exp.Column)}
    return sorted(value for value in values if value)


def _scope_for(expression: Any, scope_ids: dict[int, str], exp: Any) -> str:
    current = expression
    while current is not None:
        scope = scope_ids.get(id(current))
        if scope is not None:
            return scope
        current = getattr(current, "parent", None)
    return "scope_root"


def _column_clause(column: Any, exp: Any) -> str:
    ancestors: list[Any] = []
    current = getattr(column, "parent", None)
    while current is not None and not isinstance(current, exp.Select):
        ancestors.append(current)
        current = getattr(current, "parent", None)

    if any(isinstance(item, exp.Window) for item in ancestors):
        return "window"
    clause_types: list[tuple[Any, str]] = [
        (exp.Where, "where"),
        (exp.Having, "having"),
        (exp.Qualify, "qualify"),
        (exp.Join, "join"),
        (exp.Group, "group"),
        (exp.Order, "order"),
    ]
    for ancestor in ancestors:
        for clause_type, label in clause_types:
            if isinstance(ancestor, clause_type):
                return label
    return "select"


def _source_name(source: Any, exp: Any) -> str:
    if source is None:
        return ""
    if isinstance(source, exp.Table):
        catalog = _text(getattr(source, "catalog", ""))
        owner = _text(getattr(source, "db", ""))
        name = _text(getattr(source, "name", ""))
        qualified = ".".join(part for part in (catalog, owner, name) if part)
        alias = _text(getattr(source, "alias", ""))
        return f"{qualified} {alias}".strip() if alias else qualified
    alias = _text(getattr(source, "alias", ""))
    if alias:
        return alias
    return _sql(source)


def _from_sources(select: Any) -> list[Any]:
    from_expression = select.args.get("from_") or select.args.get("from")
    if from_expression is None:
        return []
    sources: list[Any] = []
    primary = getattr(from_expression, "this", None)
    if primary is not None:
        sources.append(primary)
    sources.extend(list(getattr(from_expression, "expressions", []) or []))
    return sources


def _identifier_quoted(identifier: Any) -> bool:
    """sqlglot の Identifier が二重引用符付きで書かれていたか。"""

    return bool(getattr(identifier, "args", {}).get("quoted")) if identifier is not None else False


def _normalize_qualifier(value: str) -> str:
    return value.replace('"', "").strip().upper()


def _source_qualifiers(source: Any, exp: Any) -> set[str]:
    """ON 句の列 qualifier から source を引くための正規化 key 集合を作る。"""
    values: set[str] = set()
    alias = _normalize_qualifier(_text(getattr(source, "alias", "")))
    if alias:
        values.add(alias)
    if isinstance(source, exp.Table):
        name = _normalize_qualifier(_text(getattr(source, "name", "")))
        if name:
            values.add(name)
    return values


def _condition_left_source(
    condition: Any,
    exp: Any,
    right_source: Any,
    known_sources: list[Any],
) -> str:
    """ON 句が実際に結ぶ左端点を、列の qualifier から解決する。

    star join(hub 表へ複数表を join する形)では「直前の join の右表」が左端点とは
    限らないため、位置ではなく ON 句を正とする。解決できない場合は "" を返し、
    呼び出し側の位置ベース fallback に委ねる。
    """
    if condition is None:
        return ""
    right_qualifiers = _source_qualifiers(right_source, exp)
    candidates: dict[str, Any] = {}
    for source in known_sources:
        for qualifier in _source_qualifiers(source, exp):
            candidates.setdefault(qualifier, source)
    matched: list[Any] = []
    for column in condition.find_all(exp.Column):
        qualifier = _normalize_qualifier(_text(getattr(column, "table", "")))
        if not qualifier or qualifier in right_qualifiers:
            continue
        source = candidates.get(qualifier)
        if source is None:
            return ""
        if not any(source is item for item in matched):
            matched.append(source)
    if len(matched) != 1:
        return ""
    return _source_name(matched[0], exp)


def _join_type(join: Any) -> str:
    side = _text(join.args.get("side")).lower()
    kind = _text(join.args.get("kind")).lower()
    method = _text(join.args.get("method")).lower()
    values = [item for item in (method, side, kind) if item]
    return "_".join(values) if values else "inner"


def _using_columns(join: Any) -> list[str]:
    using = join.args.get("using")
    if using is None:
        return []
    if isinstance(using, list):
        return [_text(value) for value in using if _text(value)]
    expressions = getattr(using, "expressions", None)
    if expressions:
        return [_text(value) for value in expressions if _text(value)]
    value = _text(using)
    return [value] if value else []


def _integer_limit(root: Any) -> int | None:
    limit = root.args.get("limit") if hasattr(root, "args") else None
    if limit is None:
        return None
    expression = getattr(limit, "expression", None)
    if expression is None:
        expression = limit.args.get("count")
    if expression is None:
        expression = getattr(limit, "this", None)
    raw = _text(expression)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _iter_query_scopes(root: Any, exp: Any) -> list[Any]:
    scopes = list(root.find_all(exp.Select))
    if isinstance(root, exp.Select) and all(id(item) != id(root) for item in scopes):
        scopes.insert(0, root)
    return scopes


def _set_operator(node: Any, exp: Any) -> str:
    if isinstance(node, exp.Union):
        return "union" if bool(node.args.get("distinct", True)) else "union_all"
    if isinstance(node, exp.Intersect):
        return "intersect"
    return "except"


def _partition_expressions(window: Any) -> Iterable[Any]:
    partition = window.args.get("partition_by")
    if partition is None:
        return []
    if isinstance(partition, list):
        return partition
    expressions = getattr(partition, "expressions", None)
    return expressions or [partition]


def _order_expressions(order: Any) -> Iterable[Any]:
    if order is None:
        return []
    return getattr(order, "expressions", None) or []


def _is_projection_wildcard(projection: Any, exp: Any) -> bool:
    """COUNT(*) と列展開の SELECT * を区別する。"""

    expression = projection.this if isinstance(projection, exp.Alias) else projection
    return isinstance(expression, exp.Star) or (
        isinstance(expression, exp.Column) and isinstance(expression.this, exp.Star)
    )


def parse_oracle_sql(
    sql: str,
    *,
    intent_version: int = 1,
    ontology_revision_id: str = "unbound",
) -> SqlSemanticAnalysis:
    """Oracle SQL を完全解析し、semantic graph と parser validation を返す。"""

    if not sql.strip():
        return _blocker_analysis(
            sql,
            code="SQL_EMPTY",
            message_ja="SQL が空のため意味を確認できません。",
            intent_version=intent_version,
            ontology_revision_id=ontology_revision_id,
        )

    try:
        import sqlglot
        from sqlglot import exp
        from sqlglot.errors import ErrorLevel, ParseError
    except ImportError:
        return _blocker_analysis(
            sql,
            code="SQL_PARSER_UNAVAILABLE",
            message_ja="SQL AST parser を利用できないため、安全性を確認できません。",
            intent_version=intent_version,
            ontology_revision_id=ontology_revision_id,
        )

    try:
        statements = sqlglot.parse(
            prepare_oracle_query(sql), read=OracleConversion, error_level=ErrorLevel.RAISE
        )
    except (ParseError, ValueError, TypeError) as exc:
        detail = str(exc).splitlines()[0][:240]
        return _blocker_analysis(
            sql,
            code="SQL_PARSE_FAILED",
            message_ja=f"Oracle SQL を完全に解析できませんでした: {detail}",
            intent_version=intent_version,
            ontology_revision_id=ontology_revision_id,
        )
    except Exception as exc:  # pragma: no cover - parser 自体の予期しない障害
        return _blocker_analysis(
            sql,
            code="SQL_PARSE_FAILED",
            message_ja=f"Oracle SQL の解析中にエラーが発生しました: {type(exc).__name__}",
            intent_version=intent_version,
            ontology_revision_id=ontology_revision_id,
        )

    statements = [statement for statement in statements if statement is not None]
    if len(statements) != 1:
        return _blocker_analysis(
            sql,
            code="SQL_MULTIPLE_STATEMENTS",
            message_ja=(
                "複数の SQL 文は実行できません。SELECT / WITH の文を1つだけ入力してください。"
            ),
            intent_version=intent_version,
            ontology_revision_id=ontology_revision_id,
        )

    root = statements[0]
    assert root is not None  # nosec B101
    if isinstance(root, exp.Subquery):
        root = root.this
    query_type = getattr(exp, "Query", None)
    is_query = query_type is not None and isinstance(root, query_type)
    if not is_query:
        return _blocker_analysis(
            sql,
            code="SQL_NOT_READ_ONLY_QUERY",
            message_ja="SELECT / WITH の文だけを実行できます。SQL の種類を確認してください。",
            intent_version=intent_version,
            ontology_revision_id=ontology_revision_id,
        )

    # SELECT 文でも INTO は書込みを伴い、FOR UPDATE / FOR SHARE は row lock を
    # 取得する。NL2SQL の read-only 境界ではどちらも SELECT-only とみなさない。
    unsafe_select = next(
        (
            select
            for select in _iter_query_scopes(root, exp)
            if select.args.get("into") is not None or bool(select.args.get("locks"))
        ),
        None,
    )
    if unsafe_select is not None:
        return _blocker_analysis(
            sql,
            code="SQL_QUERY_HAS_SIDE_EFFECT",
            message_ja="SELECT INTO または FOR UPDATE/SHARE を含む SQL は実行できません。",
            intent_version=intent_version,
            ontology_revision_id=ontology_revision_id,
        )

    sql_hash = sql_sha256(sql)
    query_scopes = _iter_query_scopes(root, exp)
    scope_ids = {id(scope): f"scope_{index + 1}" for index, scope in enumerate(query_scopes)}
    scope_ids.setdefault(id(root), "scope_root")

    cte_nodes = list(root.find_all(exp.CTE))
    cte_names = {_text(getattr(cte, "alias_or_name", "")).upper() for cte in cte_nodes}
    ctes: list[SqlCteDefinition] = []
    for index, cte in enumerate(cte_nodes, start=1):
        name = _text(getattr(cte, "alias_or_name", ""))
        dependencies = sorted(
            {
                _text(getattr(table, "name", ""))
                for table in cte.this.find_all(exp.Table)
                if _text(getattr(table, "name", "")).upper() in cte_names
                and _text(getattr(table, "name", "")).upper() != name.upper()
            }
        )
        ctes.append(
            SqlCteDefinition(
                id=_stable_element_id("cte", name, index),
                name=name,
                scope_id=_scope_for(cte.this, scope_ids, exp),
                query_sql=_sql(cte.this),
                depends_on=dependencies,
            )
        )

    # コピー上で Oracle の非引用識別子を正規化し、可視な CTE のみを解決する。
    # semantic graph の表記は原文どおり保つ。
    from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
    from sqlglot.optimizer.scope import Scope, traverse_scope

    normalized = normalize_identifiers(root.copy(), dialect="oracle")
    original_tables = list(root.find_all(exp.Table))
    original_ids = {
        id(copied): id(original)
        for original, copied in zip(original_tables, normalized.find_all(exp.Table), strict=True)
    }
    cte_reference_ids = {
        original_ids[id(table)]
        for scope in traverse_scope(normalized)
        for table in scope.tables
        if not table.db
        and not table.catalog
        # 透視後の alias は sources を上書きするため、可視 CTE の元の表名を引く。
        and isinstance(scope.cte_sources.get(table.name), Scope)
    }
    # Oracle の再帰 WITH は RECURSIVE keyword を持たないため sqlglot の
    # scope では未解決になる。UNION ALL の再帰側に限って自己参照を補う。
    for cte in normalized.find_all(exp.CTE):
        union = cte.this
        if (
            not isinstance(union, exp.Union)
            or union.args.get("distinct") is not False
            or not cte.alias_column_names
        ):
            continue
        for table in union.expression.find_all(exp.Table):
            if table.name == cte.alias and not table.db and not table.catalog:
                cte_reference_ids.add(original_ids[id(table)])
    tables: list[SqlTableReference] = []
    for index, table in enumerate(root.find_all(exp.Table), start=1):
        catalog = _text(getattr(table, "catalog", ""))
        owner = _text(getattr(table, "db", ""))
        name = _text(getattr(table, "name", ""))
        alias = _text(getattr(table, "alias", ""))
        qualified_name = ".".join(part for part in (catalog, owner, name) if part)
        tables.append(
            SqlTableReference(
                id=_stable_element_id("table", index, qualified_name, alias),
                scope_id=_scope_for(table, scope_ids, exp),
                catalog=catalog,
                owner=owner,
                name=name,
                alias=alias,
                qualified_name=qualified_name,
                is_cte=id(table) in cte_reference_ids,
                source_sql=_sql(table),
                owner_quoted=_identifier_quoted(table.args.get("db")),
                name_quoted=_identifier_quoted(table.args.get("this")),
            )
        )

    columns: list[SqlColumnReference] = []
    seen_columns: set[tuple[str, str, str, str]] = set()
    for index, column in enumerate(root.find_all(exp.Column), start=1):
        scope_id = _scope_for(column, scope_ids, exp)
        clause = _column_clause(column, exp)
        expression_sql = _sql(column)
        key = (scope_id, clause, expression_sql, _column_display(column))
        if key in seen_columns:
            continue
        seen_columns.add(key)
        columns.append(
            SqlColumnReference(
                id=_stable_element_id("column", index, *key),
                scope_id=scope_id,
                catalog=_text(getattr(column, "catalog", "")),
                owner=_text(getattr(column, "db", "")),
                table=_text(getattr(column, "table", "")),
                name=_text(getattr(column, "name", "")),
                clause=clause,
                expression_sql=expression_sql,
                owner_quoted=_identifier_quoted(column.args.get("db")),
                table_quoted=_identifier_quoted(column.args.get("table")),
                name_quoted=_identifier_quoted(column.args.get("this")),
            )
        )

    joins: list[SqlJoinReference] = []
    for select in query_scopes:
        scope_id = scope_ids[id(select)]
        sources = _from_sources(select)
        known_sources = list(sources)
        chained_source = _source_name(sources[0], exp) if sources else ""
        for index, join in enumerate(select.args.get("joins") or [], start=1):
            right_source = _source_name(join.this, exp)
            condition = join.args.get("on")
            # ON 句が結ぶ実際の左端点を優先し、解決できないときだけ位置ベースへ戻す。
            left_source = (
                _condition_left_source(condition, exp, join.this, known_sources) or chained_source
            )
            if join.this is not None:
                known_sources.append(join.this)
            using_columns = _using_columns(join)
            join_type = _join_type(join)
            method = _text(join.args.get("method")).lower()
            condition_sql = _sql(condition) if condition is not None else ""
            join_id = _stable_element_id("join", scope_id, index, left_source, right_source)
            joins.append(
                SqlJoinReference(
                    id=join_id,
                    scope_id=scope_id,
                    left_source=left_source,
                    right_source=right_source,
                    join_type=join_type,
                    condition_sql=condition_sql,
                    using_columns=using_columns,
                    referenced_columns=_columns(condition, exp),
                    is_cartesian=(
                        "cross" in join_type
                        or (not condition_sql and not using_columns and method != "natural")
                    ),
                )
            )
            chained_source = right_source or chained_source

    projections: list[SqlProjection] = []
    lineage: list[SqlLineage] = []
    aggregates: list[SqlAggregate] = []
    aggregate_seen: set[tuple[str, str]] = set()
    filters: list[SqlPredicate] = []
    having: list[SqlPredicate] = []
    groups: list[SqlGroupExpression] = []
    orders: list[SqlOrderExpression] = []

    for select in query_scopes:
        scope_id = scope_ids[id(select)]
        for index, projection in enumerate(select.expressions, start=1):
            expression_sql = _sql(projection)
            output_name = _text(getattr(projection, "alias", "")) or _text(
                getattr(projection, "output_name", "")
            )
            referenced = _columns(projection, exp)
            projection_id = _stable_element_id("projection", scope_id, index, expression_sql)
            projections.append(
                SqlProjection(
                    id=projection_id,
                    scope_id=scope_id,
                    output_name=output_name or expression_sql,
                    expression_sql=expression_sql,
                    referenced_columns=referenced,
                    contains_aggregate=projection.find(exp.AggFunc) is not None,
                    contains_window=projection.find(exp.Window) is not None,
                    contains_wildcard=_is_projection_wildcard(projection, exp),
                )
            )
            lineage.append(
                SqlLineage(
                    id=_stable_element_id("lineage", projection_id),
                    scope_id=scope_id,
                    output_name=output_name or expression_sql,
                    source_columns=referenced,
                )
            )
        predicate_specs = [
            ("where", select.args.get("where"), filters),
            ("having", select.args.get("having"), having),
            ("qualify", select.args.get("qualify"), filters),
        ]
        for clause, wrapper, destination in predicate_specs:
            if wrapper is None:
                continue
            expression = getattr(wrapper, "this", wrapper)
            destination.append(
                SqlPredicate(
                    id=_stable_element_id("predicate", scope_id, clause, _sql(expression)),
                    scope_id=scope_id,
                    clause=clause,
                    expression_sql=_sql(expression),
                    referenced_columns=_columns(expression, exp),
                )
            )

        group = select.args.get("group")
        for index, expression in enumerate(getattr(group, "expressions", []) or [], start=1):
            groups.append(
                SqlGroupExpression(
                    id=_stable_element_id("group", scope_id, index, _sql(expression)),
                    scope_id=scope_id,
                    expression_sql=_sql(expression),
                    referenced_columns=_columns(expression, exp),
                )
            )

        order = select.args.get("order")
        for index, expression in enumerate(_order_expressions(order), start=1):
            ordered_expression = getattr(expression, "this", expression)
            orders.append(
                SqlOrderExpression(
                    id=_stable_element_id("order", scope_id, index, _sql(expression)),
                    scope_id=scope_id,
                    expression_sql=_sql(expression),
                    direction="desc" if bool(expression.args.get("desc")) else "asc",
                    referenced_columns=_columns(ordered_expression, exp),
                )
            )

    # Projection 以外 (HAVING / ORDER BY / window 等) の aggregate も graph に残す。
    for aggregate in root.find_all(exp.AggFunc):
        scope_id = _scope_for(aggregate, scope_ids, exp)
        aggregate_sql = _sql(aggregate)
        aggregate_key = (scope_id, aggregate_sql)
        if aggregate_key in aggregate_seen:
            continue
        aggregate_seen.add(aggregate_key)
        aggregates.append(
            SqlAggregate(
                id=_stable_element_id("aggregate", scope_id, aggregate_sql),
                scope_id=scope_id,
                function_name=getattr(aggregate, "key", type(aggregate).__name__).upper(),
                expression_sql=aggregate_sql,
                referenced_columns=_columns(aggregate, exp),
            )
        )

    windows: list[SqlWindowExpression] = []
    for index, window in enumerate(root.find_all(exp.Window), start=1):
        scope_id = _scope_for(window, scope_ids, exp)
        partition_by = [_sql(item) for item in _partition_expressions(window)]
        order_by = [_sql(item) for item in _order_expressions(window.args.get("order"))]
        windows.append(
            SqlWindowExpression(
                id=_stable_element_id("window", scope_id, index, _sql(window)),
                scope_id=scope_id,
                expression_sql=_sql(window),
                partition_by=partition_by,
                order_by=order_by,
                referenced_columns=_columns(window, exp),
            )
        )

    set_nodes = list(root.find_all(exp.Union))
    set_nodes.extend(root.find_all(exp.Intersect))
    set_nodes.extend(root.find_all(exp.Except))
    set_operations = [
        SqlSetOperation(
            id=_stable_element_id("set", index, _sql(node)),
            scope_id=_scope_for(node, scope_ids, exp),
            operator=_set_operator(node, exp),
            expression_sql=_sql(node),
        )
        for index, node in enumerate(set_nodes, start=1)
    ]

    subquery_items: list[tuple[Any, str]] = []
    seen_subquery_nodes: set[int] = set()
    for subquery in root.find_all(exp.Subquery):
        query = subquery.this
        seen_subquery_nodes.add(id(query))
        subquery_items.append((query, _text(getattr(subquery, "alias", ""))))
    set_operation_types = (exp.Union, exp.Intersect, exp.Except)
    for select in query_scopes:
        parent = getattr(select, "parent", None)
        if parent is None or id(select) in seen_subquery_nodes:
            continue
        if isinstance(parent, (exp.CTE, exp.Subquery, *set_operation_types)):
            continue
        # EXISTS / IN 等は sqlglot 上 Subquery wrapper を持たないため明示して残す。
        seen_subquery_nodes.add(id(select))
        subquery_items.append((select, ""))
    subqueries = [
        SqlSubqueryReference(
            id=_stable_element_id("subquery", index, _sql(query)),
            scope_id=_scope_for(query, scope_ids, exp),
            alias=alias,
            query_sql=_sql(query),
        )
        for index, (query, alias) in enumerate(subquery_items, start=1)
    ]

    warnings: list[str] = []
    if any(not column.table for column in columns):
        warnings.append("修飾されていない列があります。複数表で同名列がある場合は確認が必要です。")
    if any(projection.contains_wildcard for projection in projections):
        warnings.append("SELECT * は列 scope を広げるため、明示的な列指定を推奨します。")

    graph = SqlSemanticGraph(
        sql_hash=sql_hash,
        statement_type=getattr(root, "key", type(root).__name__).upper(),
        raw_sql=sql,
        ctes=ctes,
        tables=tables,
        columns=columns,
        joins=joins,
        projections=projections,
        filters=filters,
        aggregates=aggregates,
        groups=groups,
        having=having,
        orders=orders,
        limit=_integer_limit(root),
        windows=windows,
        set_operations=set_operations,
        subqueries=subqueries,
        lineage=lineage,
        parse_warnings=warnings,
    )
    finding = OntologyValidationFinding(
        id=_stable_element_id("finding", "SQL_AST_PARSED", sql_hash),
        code="SQL_AST_PARSED",
        severity=ValidationSeverity.PASS,
        message_ja="Oracle SQL を AST として完全に解析しました。",
        sql_element_ids=[item.id for item in tables]
        + [item.id for item in joins]
        + [item.id for item in projections],
    )
    report = _build_report(
        sql_hash=sql_hash,
        intent_version=intent_version,
        ontology_revision_id=ontology_revision_id,
        findings=[finding],
    )
    return SqlSemanticAnalysis(graph=graph, validation=report)


__all__ = ["parse_oracle_sql", "sql_sha256"]
