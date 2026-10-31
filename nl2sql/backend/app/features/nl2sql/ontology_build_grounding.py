"""型付き Q/A 関係を入力 SQL の物理列へ接地する。SQL は実行しない。"""

from typing import Any, cast

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

from .object_identity import split_match_key
from .ontology_definition_validation import checked_expression, schema_objects
from .ontology_definitions import BusinessDefinition, ConceptCoverage, LinkTypeDefinition
from .ontology_models import OntologyBuildExtraction
from .ontology_sql_validation import (
    identifier_token,
    physical_column,
    physical_table,
    token_identifier,
    validated_sql,
)


def _query_columns(sql: str, physical: dict[str, set[str]]) -> set[str]:
    """alias・CTE の投影を解決し、物理列の完全名を集める。"""
    statements = sqlglot.parse(sql, read="oracle")
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        return set()
    query = cast(exp.Expression, statements[0])
    query_schema = dict(physical)
    for scope in traverse_scope(query):
        for _, source in scope.selected_sources.values():
            # CTE 名や別スキーマの同名業務表をシステム表と取り違えない。
            if not isinstance(source, exp.Table) or source.catalog:
                continue
            owner = identifier_token(source.args.get("db"))
            if identifier_token(source.this) != "DUAL" or owner not in {"", "SYS"}:
                continue
            if not owner and any(split_match_key(key)[-1] == "DUAL" for key in physical):
                owner = split_match_key(physical_table(source, physical))[0]
            else:
                owner = "SYS"
                query_schema.setdefault("SYS.DUAL", {"DUMMY"})
            source.set("db", token_identifier(owner))
    query = validated_sql(query, query_schema)
    columns: set[str] = set()
    for scope in traverse_scope(query):
        for column in scope.columns:
            try:
                columns.add(physical_column(column, physical, scope))
            except ValueError:
                # 計算された派生列・補助 DUAL 列は業務列の根拠にしない。
                # 元の業務列は内側の scope で収集する。
                continue
    return columns


def ground_qa_links(
    extraction: OntologyBuildExtraction,
    qa_sql_texts: list[str] | None,
    schema: dict[str, Any],
) -> OntologyBuildExtraction:
    """同一 Q/A に列の裏付けがない関係だけを除外し、対象付きの理由を保持する。"""
    if qa_sql_texts is None or not any(d.kind == "link_type" for d in extraction.definitions):
        return extraction
    physical = schema_objects(schema)
    queries = []
    for sql in qa_sql_texts:
        try:
            queries.append(_query_columns(sql, physical))
        except (ValueError, sqlglot.errors.SqlglotError):
            # 解釈不能な SQL を根拠として採用しない。他の Q/A の成功分は利用する。
            continue
    accepted: list[LinkTypeDefinition] = []
    errors: dict[int, str] = {}
    for index, definition in enumerate(extraction.definitions):
        if definition.kind != "link_type":
            continue
        try:
            condition = checked_expression(definition.join_expression_sql)
            if isinstance(condition, exp.Query):
                raise ValueError("単独の JOIN 条件式が必要です。")
            condition = validated_sql(condition, physical)
            columns = {
                physical_column(column, physical) for column in condition.find_all(exp.Column)
            }
            if not columns or not any(columns <= query for query in queries):
                raise ValueError("JOIN 列を同一 Q/A の SQL 内に確認できません。")
        except (ValueError, sqlglot.errors.SqlglotError) as exc:
            errors[index] = str(exc)
            continue
        accepted.append(definition)

    kept: list[BusinessDefinition] = []
    warnings = list(extraction.warnings_ja)
    for index, definition in enumerate(extraction.definitions):
        if index in errors:
            # 同じ入力の追加抽出で空欄が補完済みなら、未補完版も最終統合へ渡す。
            # 元の根拠・別名・他フィールドの競合を失わず、非空の不正な式は隠さない。
            if definition.kind == "link_type" and not definition.join_expression_sql.strip():
                completed = next(
                    (
                        item
                        for item in accepted
                        if item.api_name == definition.api_name
                        or (definition.id and item.id == definition.id)
                    ),
                    None,
                )
                if completed is not None:
                    kept.append(
                        definition.model_copy(
                            update={"join_expression_sql": completed.join_expression_sql}
                        )
                    )
                    continue
            warnings.append(
                f"Q/A 関係 {definition.api_name} / join_expression_sql を採用しません: "
                f"{errors[index]}"
            )
            continue
        kept.append(definition)
    coverage = list(extraction.coverage)
    if not any(d.kind == "link_type" for d in kept):
        coverage = [c for c in coverage if c.kind != "link_type"]
        coverage.append(
            ConceptCoverage(
                kind="link_type",
                status="insufficient_evidence",
                reason_ja="Q/A の SQL で裏付けられた関係がありません。",
            )
        )
    return extraction.model_copy(
        update={"definitions": kept, "warnings_ja": warnings, "coverage": coverage}
    )
