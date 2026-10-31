"""13分類を正本とする候補統合・可読 Markdown・接地 graph の決定論的投影。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Collection, Iterable
from typing import Any

from pydantic import TypeAdapter

from .object_identity import catalog_match_key, object_match_key, object_part_name
from .ontology_catalog import SchemaOntology
from .ontology_definition_validation import mapping_column_key, mapping_object_key
from .ontology_definitions import (
    ActionTypeDefinition,
    BusinessDefinition,
    DefinitionEvidence,
    DefinitionMapping,
    LinkTypeDefinition,
    MetricDefinitionV2,
    ObjectTypeDefinition,
    PropertyDefinition,
)
from .ontology_link_keys import link_join_check, schema_tables
from .ontology_models import (
    BusinessRuleDefinition,
    BusinessRuleExpression,
    JoinCondition,
    MetricAggregation,
    MetricDefinition,
    OntologyEdge,
    OntologyNode,
    OntologyProvenance,
    PhysicalMapping,
    PhysicalObjectRef,
)
from .ontology_sql_validation import identifier_token
from .ontology_store import canonical_json, stable_ontology_id
from .ontology_terms import CONCEPT_LABELS, FIELD_LABELS, VALUE_LABELS, plain_label

CONCEPT_ORDER = tuple(CONCEPT_LABELS)
DEFINITIONS = TypeAdapter(list[BusinessDefinition])
PHYSICAL_KINDS = {"schema", "table", "view", "column"}


def api_name(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9_.]", "_", value)
    if result != value or len(result) > 128:
        result = result[:100] + "_" + hashlib.sha256(value.encode()).hexdigest()[:16]
    return (result if result and result[0].isalpha() else "concept_" + result)[:128]


def physical_node_index(nodes: Iterable[OntologyNode]) -> dict[str, OntologyNode]:
    """物理 node（schema / 表 / ビュー / 列）を `object_identity` の照合キーで引く索引。

    キーはカタログ上の owner / object / column 名から作る（`SALES."Mixed_Case".AMOUNT`）。
    technical_name を大文字化したキーでは `SALES."Mixed_Case"` と大文字の同名表
    `SALES.MIXED_CASE` を区別できない（#573）。引用が不要な名前のキーは従来と同じ値になる。
    """

    index: dict[str, OntologyNode] = {}
    for node in nodes:
        if node.kind.value not in PHYSICAL_KINDS:
            continue
        metadata = node.metadata
        parts = [
            str(metadata.get(key) or "")
            for key in ("owner", "object_name", "column_name")
            if str(metadata.get(key) or "")
        ]
        key = catalog_match_key(*parts) if parts else object_match_key(node.technical_name)
        index.setdefault(key, node)
    return index


def sql_column_key(column: Any) -> str:
    """SQL の列参照（`OWNER.OBJECT.COLUMN`、各部は引用可）を照合キーにする。"""

    return ".".join(identifier_token(part) for part in column.parts)


def metric_definition_projection(
    definition: MetricDefinitionV2,
    concept_ids: dict[str, str],
    physical: dict[str, OntologyNode],
) -> MetricDefinition:
    """13分類の指標を引導式 SQL の既存読み取り契約へ可逆な意味で投影する。"""
    import sqlglot
    from sqlglot import exp

    columns = set()
    for sql in (definition.expression_sql, definition.filter_sql):
        if not sql.strip():
            continue
        try:
            tree = sqlglot.parse_one(sql, read="oracle")
            for col in tree.find_all(exp.Column):
                node = physical.get(sql_column_key(col))
                if node and node.kind.value == "column":
                    columns.add(node.id)
        except sqlglot.errors.SqlglotError:
            pass  # 検証は公開前ゲート。ここでは原式・条件を保持する。
    return MetricDefinition(
        id=stable_ontology_id("metric_definition", definition.id),
        metric_node_id=definition.id,
        expression_sql=definition.expression_sql,
        filter_sql=definition.filter_sql,
        aggregation=(
            definition.aggregation.lower()
            if definition.aggregation.lower() in MetricAggregation._value2member_map_
            else "none"
        ),
        base_column_node_ids=sorted(columns),
        grain_node_ids=[concept_ids[name] for name in definition.grain if name in concept_ids],
        distinct_key_node_ids=[
            concept_ids[name] for name in definition.distinct_keys if name in concept_ids
        ],
        unit=definition.unit,
        currency=definition.currency,
        additivity=definition.additivity,
        null_policy_ja=definition.null_policy_ja,
        description_ja=definition.description_ja,
    )


def _legacy_rule_sql(expression: BusinessRuleExpression, nodes: dict[str, OntologyNode]) -> Any:
    """固定ルール AST を物理列の SQL 条件へ変換する。解決不能は呼出元に返す。"""
    from sqlglot import exp

    op = expression.operator
    if op in {"all", "any", "not"}:
        children = [_legacy_rule_sql(child, nodes) for child in expression.children]
        if op == "not":
            return exp.not_(children[0])
        return exp.and_(*children) if op == "all" else exp.or_(*children)
    node = nodes.get(expression.property_node_id)
    if node is None or len(node.physical_mappings) != 1:
        raise ValueError("ルールの参照プロパティを一意に解決できません。")
    mapping = node.physical_mappings[0]
    if mapping.expression_sql or len(mapping.column_refs) != 1:
        raise ValueError("ルールの物理列を一意に解決できません。")
    col = mapping.column_refs[0]
    left = exp.column(col.column_name, table=col.object_name, db=col.owner or None, quoted=True)
    if op in {"is_null", "not_null"}:
        condition = exp.Is(this=left, expression=exp.Null())
        return exp.not_(condition) if op == "not_null" else condition
    if op in {"in", "not_in"}:
        membership = exp.In(this=left, expressions=[exp.convert(v) for v in expression.values])
        return exp.not_(membership) if op == "not_in" else membership
    comparison = {
        "eq": exp.EQ,
        "ne": exp.NEQ,
        "lt": exp.LT,
        "lte": exp.LTE,
        "gt": exp.GT,
        "gte": exp.GTE,
    }[op]
    return comparison(this=left, expression=exp.convert(expression.value))


def legacy_definitions(graph: SchemaOntology) -> list[BusinessDefinition]:
    """旧 graph の明示的な identity を保った変換。物理名だけで業務概念を併合しない。"""
    names = {
        n.id: (
            str(n.metadata["definition"]["api_name"])
            if isinstance(n.metadata.get("definition"), dict)
            else api_name(n.technical_name or n.id)
        )
        for n in graph.nodes
    }
    business_nodes = [n for n in graph.nodes if n.kind.value not in PHYSICAL_KINDS]
    original_names = dict(names)
    for node in business_nodes:
        # 旧 technical_name が同じ表名でも、異なる stable ID は別概念である。
        if not isinstance(node.metadata.get("definition"), dict) and any(
            other.id != node.id and original_names[other.id] == original_names[node.id]
            for other in business_nodes
        ):
            names[node.id] = api_name(
                f"{node.technical_name or 'concept'}_"
                f"{hashlib.sha256(node.id.encode()).hexdigest()[:12]}"
            )
    legacy_kinds = {
        "business_entity": "object_type",
        "business_term": "property",
        "enum_value": "enumeration",
    }
    result: list[dict[str, Any]] = []
    for node in graph.nodes:
        if node.kind.value in PHYSICAL_KINDS:
            continue
        if node.kind.value == "enum_value" and node.metadata.get("derived_from_definition_id"):
            continue  # 新形式の列挙メンバーは親 Enumeration の values に保存されている。
        if isinstance(node.metadata.get("definition"), dict):
            result.append(node.metadata["definition"])
            continue
        kind = legacy_kinds.get(node.kind.value, node.kind.value)
        if kind not in CONCEPT_LABELS or kind == "link_type":
            continue
        mappings = [
            {
                "owner": m.object_ref.owner,
                "object_name": m.object_ref.object_name,
                "column_name": c.column_name if c else "",
                "expression_sql": m.expression_sql,
            }
            for m in node.physical_mappings
            for c in (m.column_refs if m.column_refs else [None])
        ]
        value: dict[str, Any] = dict(
            kind=kind,
            id=node.id,
            api_name=names[node.id],
            name_ja=node.business_name_ja,
            description_ja=node.description_ja,
            aliases=node.aliases,
            mappings=mappings,
            origin="legacy",
            evidence=[
                dict(
                    source_id=e.source_document_id,
                    locator=e.locator,
                    excerpt_ja=e.excerpt_ja,
                    source_sha256=e.source_sha256,
                    source_kind="inference",
                )
                for e in node.provenance.evidence
            ],
        )
        if kind == "metric":
            metric = node.metadata.get("metric_definition", {})
            for field in ("expression_sql", "aggregation", "unit", "filter_sql", "null_policy_ja"):
                if metric.get(field):
                    value[field] = metric[field]
        if kind == "business_rule" and node.business_rule_definition:
            rule = node.business_rule_definition
            value["applies_to"] = [names[i] for i in rule.applies_to_node_ids if i in names]
            # statement と元の固定 AST は変換可否に関わらず残す。LLM に渡す
            # Markdown 自体に元の条件を含め、解釈不能な条件を消さない。
            value["description_ja"] = "\n".join(
                dict.fromkeys(
                    filter(
                        None,
                        [
                            node.description_ja,
                            rule.statement_ja,
                            "旧ルール定義: " + canonical_json(rule.model_dump(mode="json")),
                        ],
                    )
                )
            )
            value["severity"] = "warning" if rule.severity.value in {"info", "warning"} else "error"
            if rule.expression:
                try:
                    value["predicate_sql"] = _legacy_rule_sql(
                        rule.expression, {n.id: n for n in graph.nodes}
                    ).sql(dialect="oracle")
                except (ValueError, TypeError, KeyError):
                    value["missing_information_ja"] = [
                        "旧ルールの条件を SQL に変換できません。保持した元定義を確認してください。"
                    ]
        if kind in {"business_event", "action_type", "object_set"}:
            targets = [
                e.target_node_id
                for e in graph.edges
                if e.source_node_id == node.id and e.target_node_id in names
            ]
            value["object_type"] = names[targets[0]] if targets else ""
        if kind == "enumeration" and node.enum_value_definition:
            enum = node.enum_value_definition
            value["property"] = names.get(enum.property_node_id, "")
            value["values"] = [{"code": enum.code, "label_ja": enum.label_ja}]
        result.append(value)
    business_ids = {v["id"] for v in result}
    for edge in graph.edges:
        if isinstance(edge.metadata.get("definition"), dict):
            result.append(edge.metadata["definition"])
        elif (
            edge.kind.value == "business_relationship"
            and {edge.source_node_id, edge.target_node_id} <= business_ids
        ):
            joins = [
                f"{c.left.owner}.{c.left.object_name}.{c.left.column_name} {c.operator} "
                f"{c.right.owner}.{c.right.object_name}.{c.right.column_name}"
                for c in edge.join_conditions
            ]
            result.append(
                dict(
                    kind="link_type",
                    id=edge.id,
                    api_name=api_name(edge.id),
                    name_ja=edge.relationship_name_ja,
                    description_ja=edge.description_ja,
                    source=names[edge.source_node_id],
                    target=names[edge.target_node_id],
                    cardinality=edge.cardinality.value,
                    join_expression_sql=" AND ".join(joins),
                    origin="legacy",
                )
            )
    return DEFINITIONS.validate_python(result)


def concept_name_key(name: str) -> str:
    # `ADMIN.DEPARTMENT` と `admin_department` のような表記の差は同じ名前とみなす（#1729）。
    return name.replace("_", "").replace(".", "").casefold()


def resolve_concept_name(name: str, definitions: list[BusinessDefinition]) -> str:
    if any(d.api_name == name for d in definitions):
        return name
    matches = {
        d.api_name for d in definitions if concept_name_key(d.api_name) == concept_name_key(name)
    }
    return next(iter(matches)) if len(matches) == 1 else name


def is_reconciled_spelling_issue(
    message: str, original: list[BusinessDefinition], merged: list[BusinessDefinition]
) -> bool:
    """LLM の表記差診断は、機械的な統合結果で解消を確認できたものだけ除外する。"""
    target, separator, detail = message.partition(" / ")
    if not separator or len(original) <= len(merged):
        return False
    canonical = resolve_concept_name(target, merged)
    definition = next((d for d in merged if d.api_name == canonical), None)
    if definition is None:
        return False
    if definition.kind == "object_type" and re.fullmatch(
        r"プロパティ命名競合: (snake_case vs PascalCase|PascalCase vs snake_case)", detail
    ):
        properties = [d for d in original if d.kind == "property" and d.object_type == target]
        return bool(properties) and all(
            any(
                m.kind == "property"
                and m.object_type == canonical
                and m.api_name == resolve_concept_name(d.api_name, merged)
                for m in merged
            )
            for d in properties
        )
    return (
        definition.kind == "property"
        and definition.data_type == "number"
        and bool(re.fullmatch(r"データ型: (number vs integer|integer vs number)", detail))
        and {
            d.data_type
            for d in original
            if d.kind == "property" and resolve_concept_name(d.api_name, merged) == canonical
        }
        == {"integer", "number"}
    )


def _normalize_definition_spellings(
    definitions: list[BusinessDefinition],
) -> list[BusinessDefinition]:
    """同じ属性・物理列に限定して表記差を統合する。別の業務属性は併合しない。"""
    values = [d.model_copy(deep=True) for d in definitions]
    groups: dict[tuple[Any, ...], list[PropertyDefinition]] = {}
    for d in values:
        # 種類名の ID（AI が全定義に `object_type` / `property` などを付けた値）は同一性の根拠に
        # しない。そのままでは別の表・列の定義が 1 つに併合される（#1720）。
        if d.id.casefold() in CONCEPT_LABELS:
            d.id = ""
        # api_name が種類名だけ（`link_type` / `property` など）の定義は、物理の対応（所属・列・
        # 両端）から名前を作る。そのままでは別の関係・別の列の定義が 1 つに併合される（#1729）。
        if is_kind_name(d.api_name) and (derived := derived_api_name(d)):
            d.api_name = derived
        _normalize_property_type(d)
        for m in d.mappings:
            # 列そのものの式（列 `ADMIN.T.C` に式 `ADMIN.T.C`）は列の対応と同じ（#1720）。
            if m.column_name and object_match_key(m.expression_sql) == (
                f"{mapping_object_key(m)}.{mapping_column_key(m)}"
            ):
                m.expression_sql = ""
        if d.kind == "object_type":
            for m in d.mappings:
                # 表への対応を列 SQL として誤生成した場合だけ、構造化された表対応を正とする。
                if not m.column_name and object_match_key(m.expression_sql) == mapping_object_key(
                    m
                ):
                    m.expression_sql = ""
        if d.kind != "property" or not d.mappings:
            continue
        columns = tuple(
            sorted(
                {
                    mapping_column_key(m)
                    for m in d.mappings
                    if m.column_name and not m.expression_sql
                }
            )
        )
        if not columns or any(not m.column_name or m.expression_sql for m in d.mappings):
            continue
        key = (concept_name_key(d.api_name), concept_name_key(d.object_type), columns)
        groups.setdefault(key, []).append(d)
    for group in groups.values():
        first = group[0]
        identity = next((d.id for d in group if d.id), "")
        for d in group:
            d.api_name = first.api_name
            d.id = identity
        types = {getattr(d, "data_type", "") for d in group}
        if types == {"integer", "number"}:
            # integer は number の部分集合。根拠なしに整数制約を追加しない。
            for d in group:
                d.data_type = "number"
    for d_index, d in enumerate(values):
        raw = d.model_dump(mode="json")
        for field, _ in definition_references(d):
            if isinstance(raw.get(field), str):
                raw[field] = resolve_concept_name(raw[field], values)
            elif isinstance(raw.get(field), list) and all(isinstance(v, str) for v in raw[field]):
                raw[field] = [resolve_concept_name(v, values) for v in raw[field]]
        for field in ("source", "target"):
            if isinstance(raw.get(field), str):
                raw[field] = resolve_concept_name(raw[field], values)
        for impl in raw.get("implements", []):
            impl["interface"] = resolve_concept_name(impl["interface"], values)
            for binding in impl["property_mapping"]:
                binding["property"] = resolve_concept_name(binding["property"], values)
        for assignment in raw.get("assignments", []):
            assignment["property"] = resolve_concept_name(assignment["property"], values)
        normalized = DEFINITIONS.validate_python([raw])[0]
        # 未指定の既定値を明示的な変更と誤認し、統合競合を増やさない。
        normalized.__pydantic_fields_set__ = set(d.model_fields_set)
        values[d_index] = normalized
    return values


_UNION_REFERENCE_FIELDS = frozenset(
    {
        "properties",
        "dependencies",
        "applies_to",
        "affected_properties",
        "required_links",
        "required_actions",
        "extends",
    }
)


_UNKNOWN_IS_EMPTY_FIELDS = frozenset({"cardinality", "additivity"})


def rewrite_references(
    values: list[BusinessDefinition], remap: dict[str, str]
) -> list[BusinessDefinition]:
    """概念の参照（api_name）を remap に従って書き換える。"""

    if not remap:
        return values
    rewritten = []
    for d in values:
        value = d.model_dump(mode="json")
        fields = {field for field, _ in definition_references(d)} | {"source", "target"}
        for field in fields - {"implements", "affected_properties"}:
            field_value = value.get(field)
            if isinstance(field_value, str):
                value[field] = remap.get(field_value, field_value)
            elif isinstance(field_value, list):
                value[field] = [remap.get(v, v) if isinstance(v, str) else v for v in field_value]
        for impl in value.get("implements", []):
            impl["interface"] = remap.get(impl["interface"], impl["interface"])
            for binding in impl["property_mapping"]:
                binding["property"] = remap.get(binding["property"], binding["property"])
        for assignment in value.get("assignments", []):
            assignment["property"] = remap.get(assignment["property"], assignment["property"])
        if "affected_properties" in value:
            value["affected_properties"] = [remap.get(v, v) for v in value["affected_properties"]]
        rewritten.append(value)
    return DEFINITIONS.validate_python(rewritten)


class ConflictNote(str):
    """統合の競合の説明（利用者が読む平易な 2 文）。どの定義のものかを `api_name` で持つ。"""

    api_name: str
    field: str
    values: tuple[str, ...]
    # 粒度の競合だけ: それぞれの粒度の文（元の値）とキーの列の組（DB の主キーで決める。#1759）。
    grain_choices: tuple[tuple[str, frozenset[str]], ...] = ()

    def __new__(
        cls, text: str, api_name: str = "", field: str = "", values: tuple[str, ...] = ()
    ) -> ConflictNote:
        note = super().__new__(cls, text)
        note.api_name, note.field, note.values = api_name, field, values
        return note

    def detail(self) -> dict[str, Any]:
        return {"api_name": self.api_name, "field": self.field, "values": list(self.values)}


def _plain_value(field: str, value: Any) -> str:
    if isinstance(value, bool):
        text = "はい" if value else "いいえ"
    elif isinstance(value, str) and value in VALUE_LABELS:
        text = VALUE_LABELS[value]
    else:
        text = _human(value)
    text = " ".join(text.split())
    return text if len(text) <= 80 else text[:79] + "…"


def _conflict_note(
    prior: BusinessDefinition, field: str, current: Any, value: Any, names: dict[str, str]
) -> ConflictNote:
    owner = names.get(getattr(prior, "object_type", "") or "", "")
    subject = f"『{owner}』の『{prior.name_ja}』" if owner else f"『{prior.name_ja}』"
    label = plain_label(field)
    used, other = _plain_value(field, current), _plain_value(field, value)
    return ConflictNote(
        f"{subject}の{label}が 2 通り（{used} と {other}）作られました。{used} を使います。"
        f"違う場合は Markdown の『{prior.name_ja}』の「{label}」の行を直してください。",
        prior.api_name,
        field,
        (used, other),
    )


def _grain_key_columns(
    obj: BusinessDefinition, columns: dict[tuple[str, str], frozenset[str]]
) -> frozenset[str]:
    """オブジェクト型の粒度を決めるキーの列の組（主キーのプロパティの列。分からなければ空）。

    主キーのプロパティが同じ AI の出力に無いときは、名前を列名とみなす（`_ObjectPropertyRepair`
    と同じ規則）。
    """

    if not isinstance(obj, ObjectTypeDefinition):
        return frozenset()
    keys: set[str] = set()
    for name in obj.primary_key:
        mapped = columns.get((obj.api_name, name)) or columns.get(("", name))
        if mapped:
            keys.update(mapped)
        elif re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
            keys.add(object_match_key(name))
        else:
            return frozenset()
    return frozenset(keys)


_GRAIN_PHRASE = re.compile(
    r"1\s*行(?:が|は)\s*1\s*(?:つ|人|件|個|本|社)?の|ごとに\s*1\s*行|単位|[\s。]"
)


def _grain_text_key(text: str) -> str:
    """粒度の文の言い換え（「1 行が 1 つの部署」「部署ごとに 1 行」「部署」）をそろえる照合キー。"""

    return _GRAIN_PHRASE.sub("", str(text or "")).casefold()


def _grain_differs(
    prior: BusinessDefinition,
    item: BusinessDefinition,
    columns: dict[tuple[str, str], frozenset[str]],
) -> tuple[frozenset[str], frozenset[str]] | None:
    """2 つのオブジェクト型の粒度が本当に違うか（違えば両方のキーの列の組。同じなら `None`）。

    - 両方の主キーの列の組が分かれば、それで比べる。
    - 分からなくても、同じ表・ビューに対応するなら 1 行の意味は同じ（文の言い換え）。
    - 表も分からなければ、言い換えを除いた文で比べる（「受注単位」と「明細単位」は違う）。
    """

    keys = (_grain_key_columns(prior, columns), _grain_key_columns(item, columns))
    if all(keys):
        return keys if keys[0] != keys[1] else None
    tables = _table_signature(prior), _table_signature(item)
    if tables[0] and tables[0] == tables[1]:
        return None
    texts = (
        _grain_text_key(getattr(prior, "grain_ja", "")),
        _grain_text_key(getattr(item, "grain_ja", "")),
    )
    return keys if all(texts) and texts[0] != texts[1] else None


def _grain_conflict_note(
    prior: ObjectTypeDefinition,
    used: str,
    other: str,
    keys: tuple[frozenset[str], frozenset[str]],
) -> ConflictNote:
    """キーの違う粒度の競合（平易な 2 文。どちらかと、業務説明への書き方を示す。#1759）。"""

    first, second = (_plain_value("grain_ja", v) for v in (used, other))
    shown = [
        f"{text}（{'・'.join(sorted(k))} ごと）" if k else text
        for text, k in zip((first, second), keys, strict=True)
    ]
    note = ConflictNote(
        f"『{prior.name_ja}』の粒度（1 行が何を表すか）が、違う 2 通り"
        f"（{shown[0]} と {shown[1]}）作られました。"
        f"{first} を使いますが、違う場合は業務説明に正しい粒度と主キーを書いて"
        "（改善案の文を使えます）、構築し直してください。",
        prior.api_name,
        "grain_ja",
        (first, second),
    )
    note.grain_choices = ((used, keys[0]), (other, keys[1]))
    return note


def _cardinality_conflict_note(prior: LinkTypeDefinition, used: str, other: str) -> ConflictNote:
    """AI の出力どうしの多重度の競合（平易な 2 文。どちらかと、業務説明への書き方。#1765）。

    結合のキーと両端のキー・業務説明で決まるものは、構築が `settle_link_cardinalities` で除く。
    """

    first, second = (_plain_value("cardinality", v) for v in (used, other))
    return ConflictNote(
        f"『{prior.name_ja}』の多重度が、結合のキーと両端のキーからは決まらない 2 通り"
        f"（{first} と {second}）作られました。{first} を使いますが、違う場合は業務説明に"
        "正しい多重度を書いて（改善案の文を使えます）、構築し直してください。",
        prior.api_name,
        "cardinality",
        (first, second),
    )


def settle_link_cardinalities(
    conflicts: Iterable[str],
    definitions: list[BusinessDefinition],
    schema: dict[str, Any] | None,
    *,
    remap: dict[str, str] | None = None,
    previous: Collection[str] = (),
    decided: dict[str, str] | None = None,
) -> list[str]:
    """AI 構築のリンク型の多重度を、業務説明と DB のキーで決め、決まった競合を除く（#1765）。

    多重度の正は 前の定義（`merge_definitions` が競合にしない。ここでも変えない）→ 利用者の
    業務説明の文（`decided`。リンク型の API 名 → 多重度）→ 結合のキーと両端の主キー・一意キー
    （`link_cardinality`）の順。前の定義に無い（AI の）リンク型だけを、決まった値にする。どれでも
    決まらない競合だけを残す（平易な 2 文の警告になる）。
    """

    from .ontology_link_keys import link_cardinality

    tables = schema_tables(schema)
    by_name = {d.api_name: d for d in definitions}
    settled: set[str] = set()
    for d in definitions:
        if not isinstance(d, LinkTypeDefinition) or d.api_name in previous:
            continue
        value = (decided or {}).get(d.api_name) or link_cardinality(d, by_name, schema, tables)
        if value in {"one_to_one", "one_to_many", "many_to_one", "many_to_many"}:
            d.cardinality = value  # type: ignore[assignment]
            settled.add(d.api_name)
    kept: list[str] = []
    for note in conflicts:
        if isinstance(note, ConflictNote) and note.field == "cardinality":
            name = (remap or {}).get(note.api_name, note.api_name)
            if name in settled or name in previous:
                continue
        kept.append(note)
    return kept


def settle_grain_conflicts(
    conflicts: Iterable[str],
    definitions: list[BusinessDefinition],
    schema: dict[str, Any] | None,
    *,
    remap: dict[str, str] | None = None,
    decided: Collection[str] = (),
) -> list[str]:
    """AI 構築の粒度の競合を、業務説明と DB の主キー・一意キーで決まるものは除く（#1759）。

    粒度の正は 前の定義（`merge_definitions` が競合にしない）→ 利用者の業務説明の文（`decided`。
    `apply_business_text_facts` が入れたオブジェクト型）→ DB の主キー・一意キーの制約の列の組に
    当たる方、の順。DB で決まったときは、その方の粒度の文を使う。どれでも決まらない競合だけを残す。
    """

    tables = schema_tables(schema)
    by_name = {d.api_name: d for d in definitions}
    kept: list[str] = []
    for note in conflicts:
        choices = getattr(note, "grain_choices", ())
        if not isinstance(note, ConflictNote) or note.field != "grain_ja" or not choices:
            kept.append(note)
            continue
        name = (remap or {}).get(note.api_name, note.api_name)
        if name in decided:
            continue
        obj = by_name.get(name)
        table_keys = {mapping_object_key(m) for m in getattr(obj, "mappings", []) if m.object_name}
        unique = (
            tables[next(iter(table_keys))]["unique_keys"]
            if len(table_keys) == 1 and next(iter(table_keys)) in tables
            else []
        )
        matched = [text for text, keys in choices if keys in unique]
        if isinstance(obj, ObjectTypeDefinition) and len(matched) == 1:
            obj.grain_ja = matched[0]
            continue
        kept.append(note)
    return kept


def _description_parts(text: str) -> list[str]:
    """説明の文の単位（行と、Markdown の 1 行に並べた ` / ` の区切り）。空白の違いは無視する。"""

    return [
        " ".join(part.split())
        for line in str(text or "").splitlines()
        for part in line.split(" / ")
        if part.strip()
    ]


def _merged_description(current: str, value: str) -> str:
    """説明をまとめる。同じ文は重ねず、まだ無い文だけを後ろに足す（#1736）。"""

    seen = {part.casefold() for part in _description_parts(current)}
    added = [
        part
        for part in dict.fromkeys(_description_parts(value))
        if part.casefold() not in seen and not any(part.casefold() in known for known in seen)
    ]
    if not added:
        return current
    if not current.strip():
        return "\n".join(added)
    return "\n".join([current, *added])


def _action_assigned_properties(values: Iterable[BusinessDefinition]) -> set[str]:
    """アクション型（Action Type）が値を書き換えるプロパティ（更新可能の根拠）。"""

    names: set[str] = set()
    for d in values:
        if isinstance(d, ActionTypeDefinition):
            names.update(a.property for a in d.assignments if a.property)
            names.update(d.affected_properties)
    return names


def merge_definitions(
    profile_id: str,
    definitions: list[BusinessDefinition],
    previous: Collection[str] = (),
) -> tuple[list[BusinessDefinition], list[str]]:
    """同じ概念の定義を 1 つにまとめる。

    同一性の根拠は (種類, API 名) と明示的な安定 ID。ただし、物理の表・列（リンク型は両端と
    結合のキー）が食い違う定義は、名前・ID が同じでも別の概念にする（#1729）。競合は
    `ConflictNote`（利用者が読む平易な文）で返す。

    `previous` は前の定義（下書き → 公開版）の API 名（AI 構築のとき。先に並べる）。前の定義の
    説明・更新可能は正にし、AI の出力の言い換えを連結しない・上書きしない（#1736）。説明は
    同じ文を重ねない。AI の出力どうしで更新可能（Writable）が食い違うときは、アクション型が
    値を書き換えるプロパティだけを更新可能にし、競合にしない（推測で更新可能にしない）。
    """

    merged: dict[tuple[str, str], BusinessDefinition] = {}
    conflicts: list[str] = []
    remap: dict[str, str] = {}
    # AI の出力どうしで更新可能が食い違ったプロパティ（統合の後に、アクション型で決める）。
    writable_by_action: list[BusinessDefinition] = []
    normalized = _normalize_definition_spellings(definitions)
    names: dict[str, str] = {}
    kinds: dict[str, set[str]] = {}
    # (所属のオブジェクト型, プロパティの API 名) → 列。粒度をキーの列の組で比べる（#1759）。
    property_columns: dict[tuple[str, str], frozenset[str]] = {}
    for d in normalized:
        names.setdefault(d.api_name, d.name_ja)
        kinds.setdefault(d.api_name, set()).add(d.kind)
        if isinstance(d, PropertyDefinition) and d.kind == "property":
            mapped = frozenset(mapping_column_key(m) for m in d.mappings if _plain_column(m))
            if mapped:
                property_columns.setdefault((d.object_type, d.api_name), mapped)
                property_columns.setdefault(("", d.api_name), mapped)
    for original in normalized:
        item = original.model_copy(deep=True)
        # 単一バッチ内の重複も除去する。複数定義の併合時だけに限定しない。
        for field in ("aliases", "evidence", "missing_information_ja", "mappings"):
            unique = {
                canonical_json(v.model_dump(mode="json") if hasattr(v, "model_dump") else v): v
                for v in getattr(item, field)
            }
            setattr(item, field, list(unique.values()))
        identity = (item.kind, item.api_name)
        # 安定 ID の明示的一致も同一性の根拠にする。名前や同じ表だけでは併合しない。
        prior = merged.get(identity) or next(
            (d for d in merged.values() if item.id and d.id == item.id and d.kind == item.kind),
            None,
        )
        if prior is not None and not same_physical_concept(prior, item):
            # 同じ名前・ID でも別の列・別の関係。別の概念として残す（#1729）。
            base = derived_api_name(item) or item.api_name
            name = base if base != prior.api_name else f"{base}_2"
            suffix = 2
            while any(d.api_name == name for d in merged.values()):
                suffix += 1
                name = f"{base}_{suffix}"
            item.api_name, item.id = name, ""
            identity, prior = (item.kind, item.api_name), None
        if prior is None:
            item.id = item.id or stable_ontology_id(
                "profile_concept", profile_id, item.kind, item.api_name
            )
            merged[identity] = item
            continue
        remap[item.api_name] = prior.api_name
        # 粒度は、併合する前のそれぞれの主キーの列の組と対応する表で比べる（#1759）。
        grain_differs = _grain_differs(prior, item, property_columns)
        for field, value in item.model_dump(mode="json").items():
            if field in {"id", "api_name", "kind", "origin", "review_status", "implementation_key"}:
                continue
            current = getattr(prior, field)
            if field in {"aliases", "evidence", "missing_information_ja", "mappings"}:
                if field == "mappings":
                    for left in current:
                        for right in item.mappings:
                            if (
                                (left.owner, left.object_name, left.column_name)
                                == (right.owner, right.object_name, right.column_name)
                                and left.expression_sql
                                and right.expression_sql
                                and left.expression_sql != right.expression_sql
                            ):
                                conflicts.append(
                                    _conflict_note(
                                        prior,
                                        "expression_sql",
                                        left.expression_sql,
                                        right.expression_sql,
                                        names,
                                    )
                                )
                unique = {
                    canonical_json(v.model_dump(mode="json") if hasattr(v, "model_dump") else v): v
                    for v in [*current, *getattr(item, field)]
                }
                setattr(prior, field, list(unique.values()))
            elif field in _UNION_REFERENCE_FIELDS and isinstance(value, list):
                # 参照の一覧は和集合にする（抽出の回ごとに一部だけ挙げても競合にしない。#1720）。
                setattr(prior, field, list(dict.fromkeys([*current, *getattr(item, field)])))
            elif field == "description_ja" and value and value != current:
                if _looks_physical_name(current, prior):
                    # 表名・API 名だけの説明（`V_DEPT_PROJECT`）は、業務の説明に置き換える。
                    prior.description_ja = _merged_description("", str(value))
                elif prior.api_name not in previous:
                    prior.description_ja = _merged_description(str(current), str(value))
            elif field == "name_ja" and value and value != current:
                if _looks_physical_name(current, prior) and not _looks_physical_name(value, prior):
                    # 列名そのまま（`BUDGET`）より業務の名前（「予算」）を表示名にする（#1729）。
                    prior.aliases = list(dict.fromkeys([*prior.aliases, current]))
                    prior.name_ja = str(value)
                else:
                    prior.aliases = list(dict.fromkeys([*prior.aliases, str(value)]))
            elif (
                isinstance(value, bool)
                and current != value
                and field in item.model_fields_set
                and field in prior.model_fields_set
            ):
                if field == "writable" and item.origin == "ai":
                    # 前の定義の値が正。AI の出力どうしはアクション型で決める（#1736）。
                    if prior.api_name not in previous and prior.origin == "ai":
                        writable_by_action.append(prior)
                    continue
                conflicts.append(_conflict_note(prior, field, current, value, names))
            elif field == "grain_ja" and isinstance(prior, ObjectTypeDefinition):
                # 粒度（1 行が何を表すか）は文ではなく、1 行を特定するキーの列の組で比べる。
                # 前の定義（下書き → 公開版）の粒度が正で、AI の出力の言い換え（「1 行が 1 つの
                # 部署」と「部署」）は競合にしない。AI の出力どうしで本当に違う粒度（違うキー）を
                # 指すときだけ知らせる（業務説明・DB の主キーで決まれば後で除く。#1759）。
                if not current and value:
                    prior.grain_ja = str(value)
                elif (
                    value and current != value and prior.api_name not in previous and grain_differs
                ):
                    conflicts.append(
                        _grain_conflict_note(prior, str(current), str(value), grain_differs)
                    )
            elif field == "inverse_name_ja":
                # 逆向きの名前は表示のための名前。構築の回ごとの言い換えは競合にしない。
                if not current and value:
                    setattr(prior, field, str(value))
            elif field in {"shared_property", "value_type"} and current != value:
                # 正しい種類を指す方を使う（AI が自分の API 名などを書いた参照は競合にしない）。
                if field not in kinds.get(str(current), set()) and field in kinds.get(
                    str(value), set()
                ):
                    setattr(prior, field, value)
                elif field in kinds.get(str(current), set()) and field in kinds.get(
                    str(value), set()
                ):
                    conflicts.append(_conflict_note(prior, field, current, value, names))
            elif (
                field == "cardinality"
                and isinstance(prior, LinkTypeDefinition)
                and prior.api_name in previous
                and "unknown" not in {current, value}
            ):
                # 前の定義（下書き → 公開版）の多重度が正。AI の出力の揺れは競合にしない（#1765）。
                # AI の出力どうしの食い違いは、構築が業務説明・DB のキーで決める
                # （`settle_link_cardinalities`）。
                continue
            elif (
                field == "cardinality"
                and isinstance(prior, LinkTypeDefinition)
                and "unknown" not in {current, value}
                and current != value
                and item.origin == "ai"
            ):
                # AI の出力どうし。Markdown（利用者）どうしの食い違いは今どおり Markdown を直す文。
                conflicts.append(_cardinality_conflict_note(prior, str(current), str(value)))
            elif field in _UNKNOWN_IS_EMPTY_FIELDS and "unknown" in {current, value}:
                # unknown は未設定と同じ。分かった値を使い、競合にしない（#1720）。
                if current == "unknown":
                    setattr(prior, field, getattr(item, field))
            elif not current and value:
                setattr(prior, field, getattr(item, field))
            elif field == "physical_type" and value and current != value:
                # 同じ列の物理の型の表記差（`NUMBER(10)` と `NUMBER(10,0)`）は業務の型が同じなら
                # 競合にしない。
                if base_data_type(str(current)) != base_data_type(str(value)):
                    conflicts.append(_conflict_note(prior, field, current, value, names))
            elif value and current != getattr(item, field):
                # 同一性が一致しても意味の違いは隠さない。
                conflicts.append(
                    _conflict_note(prior, field, prior.model_dump(mode="json")[field], value, names)
                )
    for d in merged.values():
        d.aliases = _clean_aliases(d)
    if writable_by_action:
        assigned = {remap.get(n, n) for n in _action_assigned_properties(merged.values())}
        for d in writable_by_action:
            if isinstance(d, PropertyDefinition):
                d.writable = d.api_name in assigned
    values = rewrite_references(list(merged.values()), remap)
    unique_conflicts = list({str(c): c for c in conflicts}.values())
    return sorted(values, key=lambda d: (CONCEPT_ORDER.index(d.kind), d.api_name)), sorted(
        unique_conflicts
    )


def _looks_physical_name(name: Any, d: BusinessDefinition) -> bool:
    """表示名が物理の名前（列名・API 名）のままか。"""

    text = str(name or "").strip()
    if not text:
        return True
    physical = {object_match_key(m.column_name) for m in d.mappings if m.column_name}
    physical.update({object_match_key(m.object_name) for m in d.mappings if m.object_name})
    return bool(re.fullmatch(r"[A-Za-z0-9_.]+", text)) and (
        object_match_key(text) in physical or concept_name_key(text) == concept_name_key(d.api_name)
    )


def _schema_columns(schema: dict[str, Any] | None) -> dict[str, dict[str, dict[str, Any]]]:
    """schema context の object → 列の照合キー → 列の情報。"""

    result: dict[str, dict[str, dict[str, Any]]] = {}
    for obj in (schema or {}).get("objects", []):
        key = (
            f"{object_match_key(str(obj.get('owner', '')))}."
            f"{object_match_key(str(obj.get('object_name', '')))}"
        )
        columns = result.setdefault(key, {})
        for column in obj.get("columns", []):
            if isinstance(column, dict):
                name = str(column.get("column_name", column.get("column", column.get("name", ""))))
                columns[object_match_key(name)] = {**column, "column": name}
            else:
                columns[object_match_key(str(column))] = {"column": str(column)}
    return result


def _column_keys(prop: BusinessDefinition | None, name: str) -> list[str]:
    """参照名が指す列の照合キー。プロパティならその列、未定義なら名前を列名とみなす。"""

    if isinstance(prop, PropertyDefinition):
        keys = [mapping_column_key(m) for m in prop.mappings if m.column_name]
        if keys:
            return keys
    bare = name.rsplit(".", 1)[-1]
    return [object_match_key(bare)] if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", bare) else []


class _ObjectPropertyRepair:
    """1 つのオブジェクトの properties / 識別子 / 表示名の参照を、その表の列へ付け替える。"""

    def __init__(
        self,
        profile_id: str,
        obj: ObjectTypeDefinition,
        by_name: dict[str, BusinessDefinition],
        columns: dict[str, dict[str, dict[str, Any]]],
        added: list[BusinessDefinition],
    ) -> None:
        self.profile_id = profile_id
        self.obj = obj
        self.by_name = by_name
        self.columns = columns
        self.added = added
        self.tables = list(dict.fromkeys(mapping_object_key(m) for m in obj.mappings))
        self.own = [
            d
            for d in by_name.values()
            if isinstance(d, PropertyDefinition)
            and d.kind == "property"
            and d.object_type == obj.api_name
        ]
        self.cache: dict[str, str | None] = {}

    def _mapped(self, table: str, key: str, mappings: Iterable[Any]) -> bool:
        return any(
            mapping_object_key(m) == table and m.column_name and mapping_column_key(m) == key
            for m in mappings
        )

    def resolve(self, name: str) -> str | None:
        if name not in self.cache:
            self.cache[name] = self._resolve(name)
        return self.cache[name]

    def _resolve(self, name: str) -> str | None:
        obj = self.obj
        prop = self.by_name.get(name)
        # オブジェクト型の properties が共有プロパティを指すときは、このオブジェクト型の表の同じ列の
        # プロパティ（共有プロパティを参照する）に付け替える（Foundry の共有プロパティ。#1729）。
        shared = prop if prop is not None and prop.kind == "shared_property" else None
        if (
            prop is not None
            and shared is None
            and (
                prop.kind != "property"  # 種類の誤りは検証の診断に任せる
                or (isinstance(prop, PropertyDefinition) and prop.object_type == obj.api_name)
            )
        ):
            return name
        if (
            shared is None
            and isinstance(prop, PropertyDefinition)
            and not prop.object_type
            and all(mapping_object_key(m) in self.tables for m in prop.mappings)
        ):
            # 所属の無いプロパティは、参照したオブジェクトの表の列なら、そのオブジェクトに属させる。
            prop.object_type = obj.api_name
            self.own.append(prop)
            return name
        target = next(
            (
                (table, key)
                for key in _column_keys(prop, name)
                for table in self.tables
                if self._mapped(table, key, obj.mappings) or key in self.columns.get(table, {})
            ),
            None,
        )
        if target is None:
            return None
        table, key = target
        existing = next((d for d in self.own if self._mapped(table, key, d.mappings)), None)
        if existing is not None:
            if shared is not None and not existing.shared_property:
                existing.shared_property = shared.api_name
            return existing.api_name
        source = next(m for m in obj.mappings if mapping_object_key(m) == table)
        column_info = self.columns.get(table, {}).get(key, {})
        column_name = next(
            (m.column_name for m in obj.mappings if self._mapped(table, key, [m])),
            str(column_info.get("column") or key),
        )
        base = name.rsplit(".", 1)[-1]
        new_name = name if prop is None else f"{obj.api_name}_{base}"
        suffix = 2
        while new_name in self.by_name:
            new_name = f"{obj.api_name}_{base}_{suffix}"
            suffix += 1
        template = prop if isinstance(prop, PropertyDefinition) else None
        clone = PropertyDefinition(
            api_name=new_name,
            id=stable_ontology_id("profile_concept", self.profile_id, "property", new_name),
            name_ja=(
                template.name_ja
                if template
                else str(column_info.get("logical_name") or column_name)
            ),
            description_ja=template.description_ja if template else "",
            aliases=list(template.aliases) if template else [],
            object_type=obj.api_name,
            data_type=(template.data_type if template else str(column_info.get("data_type") or "")),
            unit=template.unit if template else "",
            currency=template.currency if template else "",
            allowed_values=list(template.allowed_values) if template else [],
            shared_property=(
                shared.api_name
                if shared is not None
                else template.shared_property
                if template
                else ""
            ),
            value_type=template.value_type if template else "",
            mappings=[
                DefinitionMapping(
                    owner=source.owner, object_name=source.object_name, column_name=column_name
                )
            ],
            evidence=[
                DefinitionEvidence(
                    source_id="schema",
                    locator=f"{table}.{key}",
                    source_kind="database",
                    assertion="mappings",
                )
            ],
            origin=template.origin if template else "ai",
        )
        self.by_name[new_name] = clone
        self.added.append(clone)
        self.own.append(clone)
        return new_name

    def names(self, names: list[str]) -> list[str]:
        return list(dict.fromkeys(r for n in names if (r := self.resolve(n))))


# AI が空の項目を書き写した不足情報（「description_ja: 記載なし」「主キーが不明」など）。
# 構造の検査（識別子・粒度・型・基数）が同じことを info で出すため、重ねない（#1720）。
_STRUCTURAL_NOTE = re.compile(
    r"(description_ja|primary_key|grain_ja|title_property|cardinality|data_type|主キー|"
    # LLM が Markdown の解析で書く、空の項目の説明（「主識別子は…ここでは空欄にする」など。#1729）。
    r"主識別子|識別子|表示名項目|粒度|多重度|基数|データ型|記載なし|空欄)"
)
_CONTENT_EXEMPT_FIELDS = frozenset(
    {
        "kind",
        "id",
        "api_name",
        "name_ja",
        "origin",
        "review_status",
        "evidence",
        "missing_information_ja",
        "aliases",
    }
)


def _is_ungrounded(d: BusinessDefinition) -> bool:
    """graph に置けない AI の出力（種類ごとの空の雛形・端点の無い参照）。

    資料から抽出した物理の対応の無いオブジェクトなど、中身のある定義は残す。
    """

    if d.api_name.casefold() in CONCEPT_LABELS and not d.mappings:
        return True  # api_name が種類名だけの雛形（object_type・property など）
    if d.kind == "link_type":
        return not (getattr(d, "source", "") and getattr(d, "target", ""))
    if d.kind in {"action_type", "business_event", "object_set"}:
        return not getattr(d, "object_type", "")
    if d.kind == "enumeration":
        return not getattr(d, "property", "")
    defaults = {
        name: info.get_default(call_default_factory=True)
        for name, info in type(d).model_fields.items()
    }
    return not d.description_ja.strip() and all(
        value == defaults.get(field)
        for field, value in d.model_dump().items()
        if field not in _CONTENT_EXEMPT_FIELDS
    )


def _prune_ai_output(
    values: list[BusinessDefinition], previous: Collection[str] = ()
) -> tuple[list[BusinessDefinition], dict[str, str]]:
    """AI の出力から、graph に置けない雛形と、構造の検査と重なる不足情報を除く。"""

    kept = [d for d in values if not _is_ungrounded(d)]
    for d in kept:
        d.missing_information_ja = [
            note for note in d.missing_information_ja if not _STRUCTURAL_NOTE.search(note)
        ]
    # 同じオブジェクトの同じ列に対応するプロパティが名前違いで 2 つあれば 1 つにする。
    remap: dict[str, str] = {}
    groups: dict[tuple[str, str], list[BusinessDefinition]] = {}
    for d in kept:
        if not (isinstance(d, PropertyDefinition) and d.kind == "property"):
            continue
        columns = [
            m
            for m in d.mappings
            if m.column_name
            and (
                not m.expression_sql
                or object_match_key(m.expression_sql)
                == f"{mapping_object_key(m)}.{mapping_column_key(m)}"
            )
        ]
        if len(columns) != len(d.mappings) or len({mapping_column_key(m) for m in columns}) != 1:
            continue
        key = (
            d.object_type,
            f"{mapping_object_key(columns[0])}.{mapping_column_key(columns[0])}",
        )
        groups.setdefault(key, []).append(d)
    rank = _prefer_previous(previous)
    for group in groups.values():
        # 残すのは前の定義の名前（#1734）。消す方の空でない項目（共有プロパティの参照など）と
        # 名前は残す方に寄せる（#1729）。
        survivor = min(group, key=rank)
        for d in group:
            if d is not survivor:
                _absorb(survivor, d)
                remap[d.api_name] = survivor.api_name
    kept = [d for d in kept if d.api_name not in remap]
    # プロパティの対応は所属オブジェクトの表・ビューの列だけにする（他の表の列を 1 つの
    # プロパティに束ねない）。所属オブジェクトの表に 1 つも無ければ元のまま検証に任せる。
    tables = {
        d.api_name: {mapping_object_key(m) for m in d.mappings}
        for d in kept
        if isinstance(d, ObjectTypeDefinition) and d.mappings
    }
    for d in kept:
        if isinstance(d, PropertyDefinition) and d.kind == "property" and d.object_type in tables:
            own = [m for m in d.mappings if mapping_object_key(m) in tables[d.object_type]]
            if own:
                d.mappings = own
    return rewrite_references(kept, remap), remap


# ---------------------------------------------------------------------------
# 業務の型と物理の型（#1729）
# ---------------------------------------------------------------------------

BASE_DATA_TYPES = ("string", "integer", "number", "boolean", "date", "datetime")
_BASE_TYPE_ALIASES = {
    "decimal": "number",
    "double": "number",
    "float": "number",
    "long": "integer",
    "int": "integer",
    "timestamp": "datetime",
    "text": "string",
    "bool": "boolean",
}
_PHYSICAL_TYPE = re.compile(
    r"(?P<name>[A-Za-z_][A-Za-z_ 0-9]*?)\s*"
    r"(?:\(\s*(?P<precision>\d+|\*)\s*(?:,\s*(?P<scale>-?\d+)\s*)?(?:\s+(?:BYTE|CHAR))?\))?"
    r"(?:\s+WITH(?:\s+LOCAL)?\s+TIME\s+ZONE)?",
    re.IGNORECASE,
)


def base_data_type(value: str) -> str:
    """業務の型（Base Type）。物理の型（`NUMBER(12,2)`）・訳（「整数（integer）」）も受け付ける。"""

    text = str(value or "").strip()
    lowered = text.casefold()
    if not text:
        return ""
    if lowered in BASE_DATA_TYPES:
        return lowered
    for base in BASE_DATA_TYPES:
        if VALUE_LABELS.get(base, "").casefold() == lowered:
            return base
    if lowered in _BASE_TYPE_ALIASES:
        return _BASE_TYPE_ALIASES[lowered]
    match = _PHYSICAL_TYPE.fullmatch(text)
    if match is None:
        return ""
    name = " ".join(match.group("name").upper().split())
    precision, scale = match.group("precision"), match.group("scale")
    if name in {"VARCHAR2", "NVARCHAR2", "VARCHAR", "CHAR", "NCHAR", "CLOB", "NCLOB", "LONG"}:
        return "string"
    if name == "NUMBER":
        if precision and precision != "*" and (scale is None or int(scale) == 0):
            return "integer"
        return "number"
    if name in {"INTEGER", "SMALLINT"}:
        return "integer"
    if name in {"FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE", "NUMERIC", "REAL"}:
        return "number"
    if name == "DATE":
        return "date"
    if name.startswith("TIMESTAMP"):
        return "datetime"
    if name == "BOOLEAN":
        return "boolean"
    return ""


def _is_physical_type(value: str) -> bool:
    text = str(value or "").strip()
    lowered = text.casefold()
    return bool(base_data_type(text)) and not (
        lowered in BASE_DATA_TYPES
        or lowered in _BASE_TYPE_ALIASES
        or any(VALUE_LABELS.get(b, "").casefold() == lowered for b in BASE_DATA_TYPES)
    )


def _type_family(value: str) -> str:
    base = base_data_type(value)
    return "number" if base == "integer" else base


def _normalize_property_type(d: BusinessDefinition) -> None:
    """データ型を業務の型にそろえ、物理の型（精度・桁）は `physical_type` に移す。"""

    if not isinstance(d, PropertyDefinition):
        return
    raw = d.data_type.strip()
    if _is_physical_type(raw):
        d.physical_type = d.physical_type or raw
    base = base_data_type(raw)
    if base:
        d.data_type = base
    if d.physical_type and not d.data_type:
        d.data_type = base_data_type(d.physical_type)


def link_source_nullable(
    link: BusinessDefinition,
    values: Iterable[BusinessDefinition],
    columns: dict[str, dict[str, dict[str, Any]]],
) -> bool | None:
    """リンク型の起点側の結合のキーの列に NULL 可の列があるか（DB の列から。分からなければ None）。

    任意の関係（Optional）の根拠にする（起点の行が終点を持たないことがあるか。#1736）。
    """

    if not isinstance(link, LinkTypeDefinition) or not link.join_expression_sql.strip():
        return None
    source = next(
        (d for d in values if isinstance(d, ObjectTypeDefinition) and d.api_name == link.source),
        None,
    )
    tables = _table_signature(source) if source is not None else frozenset()
    if len(tables) != 1:
        return None
    table = next(iter(tables))
    nullable: list[bool] = []
    for condition in re.split(r"\s+AND\s+", link.join_expression_sql.strip(), flags=re.I):
        sides = [object_match_key(p.strip()) for p in condition.split("=")]
        if len(sides) != 2:
            return None
        own = [side for side in sides if side.rsplit(".", 1)[0] == table]
        if len(own) != 1:
            return None
        info = columns.get(table, {}).get(own[0].rsplit(".", 1)[-1])
        if info is None or not isinstance(info.get("nullable"), bool):
            return None
        nullable.append(info["nullable"])
    return any(nullable) if nullable else None


def _fill_link_optional(
    values: list[BusinessDefinition], columns: dict[str, dict[str, dict[str, Any]]]
) -> None:
    """結合のキーのあるリンク型の任意の関係は、DB の列の NULL 可で決める。

    必須（Required）と同じく DB の列が正（#1736）。
    """

    for d in values:
        nullable = link_source_nullable(d, values, columns)
        if isinstance(d, LinkTypeDefinition) and nullable is not None:
            d.optional = nullable


def _fill_property_types(
    values: list[BusinessDefinition], columns: dict[str, dict[str, dict[str, Any]]]
) -> None:
    """1 つの列に対応するプロパティは、schema の列の型から業務の型・物理の型を補う。"""

    for d in values:
        _normalize_property_type(d)
        if not isinstance(d, PropertyDefinition):
            continue
        targets = [m for m in d.mappings if m.column_name and not m.expression_sql]
        if len(targets) != 1:
            continue
        info = columns.get(mapping_object_key(targets[0]), {}).get(
            mapping_column_key(targets[0]), {}
        )
        physical = str(info.get("data_type") or "").strip()
        if not physical:
            continue
        # 物理の型は DB の列が正（AI・Markdown の書いた型と違えば、業務の型も列に合わせる）。
        d.physical_type = physical
        if base_data_type(physical) and _type_family(d.data_type) != _type_family(physical):
            d.data_type = base_data_type(physical)
        if isinstance(info.get("nullable"), bool) and d.kind == "property":
            # 必須（Required）も DB の列が正（NOT NULL の列は必須）。
            d.required = not info["nullable"]


SCHEMA_RESOLVED_FIELDS = frozenset({"data_type", "physical_type", "required"})


def schema_resolved_conflicts(
    conflicts: Iterable[str],
    definitions: list[BusinessDefinition],
    schema: dict[str, Any] | None,
    remap: dict[str, str] | None = None,
) -> list[str]:
    """DB の列の型・NOT NULL で値が決まった競合と、同じ列のプロパティの統合で同じ定義を指すように
    なった参照の競合を除く（利用者が直すことは無い。#1729）。

    `remap` は統合で消えた名前 → 残した名前（`repair_definitions_report` の結果）。
    """

    columns = _schema_columns(schema)
    by_name = {d.api_name: d for d in definitions}

    def same_after_merge(note: ConflictNote) -> bool:
        # 参照の食い違い（主キー・タイトルのプロパティなど）が、同じ列のプロパティの統合で
        # 同じ定義を指すようになったもの。
        tokens = [
            {(remap or {}).get(t.strip(), t.strip()) for t in str(v).split(" / ")}
            for v in note.values
        ]
        return len(tokens) == 2 and tokens[0] == tokens[1]

    def same_column(note: ConflictNote) -> bool:
        # 主キー・タイトルのプロパティの食い違いが、同じ名前の列（別の表のプロパティを指した
        # AI の参照は、修復でこのオブジェクト型の列のプロパティに付け替える）を指すもの。
        if note.field not in {"primary_key", "title_property", "properties"}:
            return False
        columns = []
        for value in note.values:
            names = [t.strip() for t in str(value).split(" / ") if t.strip()]
            keys = set()
            for name in names:
                target = by_name.get((remap or {}).get(name, name))
                mapped = [m for m in getattr(target, "mappings", []) if _plain_column(m)]
                if len(mapped) == 1:
                    keys.add(mapping_column_key(mapped[0]))
                elif target is None and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name):
                    # 定義の無い名前は列名とみなして修復する（`_ObjectPropertyRepair`）。
                    keys.add(object_match_key(name))
                else:
                    keys.add(f"?{name}")
            columns.append(keys)
        return len(columns) == 2 and columns[0] == columns[1]

    def resolved(note: str) -> bool:
        if not isinstance(note, ConflictNote):
            return False
        if same_after_merge(note) or same_column(note):
            return True
        d = by_name.get((remap or {}).get(note.api_name, note.api_name))
        if note.field == "optional" and d is not None:
            # 任意の関係は、起点の結合のキーの列の NULL 可で決まる（#1736）。
            return link_source_nullable(d, definitions, columns) is not None
        if note.field not in SCHEMA_RESOLVED_FIELDS:
            return False
        if d is not None and d.kind in {"shared_property", "value_type"}:
            # 共有プロパティ・値型の型・必須は、所属のプロパティ（DB の列が正）が持つ。
            return True
        targets = [m for m in getattr(d, "mappings", []) if _plain_column(m)]
        return len(targets) == 1 and mapping_column_key(targets[0]) in columns.get(
            mapping_object_key(targets[0]), {}
        )

    return [note for note in conflicts if not resolved(note)]


def _borrow_business_names(values: list[BusinessDefinition]) -> None:
    """表示名が列名のまま（`BUDGET`）のプロパティは、同じ列名の別のプロパティの業務の名前を使う。

    AI が一部のオブジェクト型（ビューなど）にだけ日本語の名前を付けたとき、同じ名前の列は同じ
    意味として名前をそろえる（列名から推測で訳さない）。
    """

    named: dict[str, str] = {}
    for d in values:
        if isinstance(d, PropertyDefinition) and not _looks_physical_name(d.name_ja, d):
            for m in d.mappings:
                if _plain_column(m):
                    named.setdefault(mapping_column_key(m), d.name_ja)
    for d in values:
        if not isinstance(d, PropertyDefinition) or not _looks_physical_name(d.name_ja, d):
            continue
        keys = {mapping_column_key(m) for m in d.mappings if _plain_column(m)}
        if len(keys) == 1 and (name := named.get(next(iter(keys)))):
            d.aliases = list(dict.fromkeys([*d.aliases, d.name_ja]))
            d.name_ja = name


# ---------------------------------------------------------------------------
# 同じ物理の表・列・関係を指す概念の統合（#1729）
# ---------------------------------------------------------------------------

# 概念 ID（`profile_concept_<hex>`）などの内部の識別子。別名（Alias）にしない。
_INTERNAL_IDENTIFIER = re.compile(r"^[a-z][a-z_]*_[0-9a-f]{12,}$")


def is_kind_name(value: str) -> bool:
    """`object_type` / `property` など、種類の名前だけの値（同一性の根拠にしない）。"""

    return str(value or "").strip().casefold() in CONCEPT_LABELS


def _identifier(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_").lower()
    return (text if text and text[0].isalpha() else f"concept_{text}")[:120]


def _plain_column(m: DefinitionMapping) -> bool:
    """式の無い列の対応（列そのものの式 `ADMIN.T.C` も同じ）。"""

    return bool(m.column_name) and (
        not m.expression_sql
        or object_match_key(m.expression_sql) == f"{mapping_object_key(m)}.{mapping_column_key(m)}"
    )


def _column_signature(d: BusinessDefinition) -> frozenset[str]:
    return frozenset(
        f"{mapping_object_key(m)}.{mapping_column_key(m)}" for m in d.mappings if _plain_column(m)
    )


def _table_signature(d: BusinessDefinition) -> frozenset[str]:
    return frozenset(mapping_object_key(m) for m in d.mappings if m.object_name)


def join_key(sql: str) -> str:
    """結合条件の照合キー（`A.X = B.Y AND …` の左右・並びの違いを無視する）。"""

    parts = []
    for condition in re.split(r"\s+AND\s+", sql.strip(), flags=re.IGNORECASE):
        sides = [s for s in condition.split("=") if s.strip()]
        if len(sides) != 2:
            return " ".join(sql.upper().split())
        parts.append("=".join(sorted(object_match_key(s.strip()) for s in sides)))
    return " AND ".join(sorted(parts))


def same_physical_concept(left: BusinessDefinition, right: BusinessDefinition) -> bool:
    """同じ名前の 2 つの定義が、物理の表・列（関係は両端と結合のキー）で食い違わないか。

    名前や ID（種類の名前など）が同じでも、別の列・別の両端を指す定義は別の概念にする（#1729）。
    """

    if left.kind == "link_type" and right.kind == "link_type":
        ends = [
            frozenset(concept_name_key(getattr(d, f, "")) for f in ("source", "target"))
            for d in (left, right)
        ]
        if all("" not in e for e in ends) and ends[0] != ends[1]:
            return False
        joins = [join_key(getattr(d, "join_expression_sql", "")) for d in (left, right)]
        return not (all(joins) and joins[0] != joins[1])
    if left.kind == "object_type" and right.kind == "object_type":
        tables = [_table_signature(left), _table_signature(right)]
        return not (all(tables) and tables[0] != tables[1])
    columns = [_column_signature(left), _column_signature(right)]
    if all(columns) and columns[0] != columns[1]:
        return False
    owners = [concept_name_key(getattr(d, "object_type", "")) for d in (left, right)]
    return not (all(owners) and owners[0] != owners[1])


def derived_api_name(d: BusinessDefinition) -> str:
    """物理の対応（所属・列・両端）から作る API 名。作れなければ空。"""

    if d.kind == "link_type":
        source, target = getattr(d, "source", ""), getattr(d, "target", "")
        return _identifier(f"{source}_to_{target}") if source and target else ""
    columns = [m for m in d.mappings if m.column_name]
    if columns:
        owner = getattr(d, "object_type", "") or columns[0].object_name
        return _identifier(f"{owner}_{columns[0].column_name}")
    tables = [m for m in d.mappings if m.object_name]
    return _identifier(tables[0].object_name) if tables else ""


def _clean_aliases(d: BusinessDefinition, extra: Iterable[str] = ()) -> list[str]:
    seen = {d.name_ja.strip().casefold(), d.api_name.casefold()}
    result: list[str] = []
    for alias in [*d.aliases, *extra]:
        text = str(alias or "").strip()
        if not text or text.casefold() in seen or _INTERNAL_IDENTIFIER.match(text):
            continue
        seen.add(text.casefold())
        result.append(text)
    return result


def _absorb(survivor: BusinessDefinition, other: BusinessDefinition) -> None:
    """other の名前・別名・空でない項目を survivor に寄せる（survivor の値を優先する）。"""

    survivor.aliases = _clean_aliases(survivor, [other.name_ja, *other.aliases])
    for field in ("aliases", "evidence", "missing_information_ja", "mappings"):
        current = getattr(survivor, field)
        unique = {
            canonical_json(v.model_dump(mode="json") if hasattr(v, "model_dump") else v): v
            for v in [*current, *getattr(other, field)]
        }
        setattr(survivor, field, list(unique.values()))
    for field in type(survivor).model_fields:
        if field in {"id", "api_name", "kind", "name_ja", "origin", "review_status"}:
            continue
        if field in {"aliases", "evidence", "missing_information_ja", "mappings"}:
            continue
        current, value = getattr(survivor, field), getattr(other, field, None)
        if field in _UNION_REFERENCE_FIELDS and isinstance(current, list):
            setattr(survivor, field, list(dict.fromkeys([*current, *(value or [])])))
        elif (field in _UNKNOWN_IS_EMPTY_FIELDS and current == "unknown" and value) or (
            current in ("", [], None) and value not in ("", [], None)
        ):
            setattr(survivor, field, value)
    survivor.aliases = _clean_aliases(survivor)


def _owned(values: list[BusinessDefinition], name: str) -> list[PropertyDefinition]:
    return [
        d
        for d in values
        if isinstance(d, PropertyDefinition) and d.kind == "property" and d.object_type == name
    ]


def _name_keys(d: BusinessDefinition) -> set[str]:
    keys = {concept_name_key(d.api_name), d.name_ja.strip().casefold()}
    keys.update(a.strip().casefold() for a in d.aliases)
    return {k for k in keys if k}


def _unify_objects(
    values: list[BusinessDefinition],
) -> tuple[list[BusinessDefinition], dict[str, str]]:
    """同じ表・ビューを指すオブジェクト型を 1 つにまとめる（#1729）。

    表の無いオブジェクト型は、所属のプロパティの列がすべて 1 つの表にあればその表を対応に
    する。なお表が無ければ、名前・別名が一致する表のあるオブジェクト型に寄せる（「部署」と
    「部門」と `department` など）。残すのはプロパティ・関係の多い方で、他方の名前は別名にする。
    """

    by_name = {d.api_name: d for d in values}
    objects = [d for d in values if isinstance(d, ObjectTypeDefinition)]
    for obj in objects:
        if obj.mappings:
            continue
        props = [
            *_owned(values, obj.api_name),
            *[p for n in obj.properties if isinstance(p := by_name.get(n), PropertyDefinition)],
        ]
        tables = {mapping_object_key(m): m for p in props for m in p.mappings if m.column_name}
        if len(tables) == 1:
            source = next(iter(tables.values()))
            obj.mappings = [DefinitionMapping(owner=source.owner, object_name=source.object_name)]

    def score(obj: ObjectTypeDefinition) -> tuple[int, int, bool, bool]:
        properties = set(obj.properties) | {p.api_name for p in _owned(values, obj.api_name)}
        links = sum(
            obj.api_name in {getattr(d, "source", ""), getattr(d, "target", "")}
            for d in values
            if d.kind == "link_type"
        )
        return (len(properties), links, obj.origin != "legacy", "." not in obj.api_name)

    remap: dict[str, str] = {}

    def final(name: str) -> str:
        while name in remap:
            name = remap[name]
        return name

    groups: dict[frozenset[str], list[ObjectTypeDefinition]] = {}
    for obj in objects:
        signature = _table_signature(obj)
        if signature and all(not m.column_name and not m.expression_sql for m in obj.mappings):
            groups.setdefault(signature, []).append(obj)
    for group in groups.values():
        survivor = max(group, key=score)
        for other in group:
            if other is not survivor:
                _absorb(survivor, other)
                remap[other.api_name] = survivor.api_name
    for obj in objects:
        if obj.mappings or obj.api_name in remap:
            continue
        keys = _name_keys(obj)
        matches = {
            final(o.api_name)
            for o in objects
            if o is not obj and o.mappings and keys & _name_keys(o)
        }
        if len(matches) == 1:
            _absorb(by_name[next(iter(matches))], obj)
            remap[obj.api_name] = next(iter(matches))
    if not remap:
        return values, {}
    remap = {name: final(name) for name in remap}
    kept = [d for d in values if not (d.kind == "object_type" and d.api_name in remap)]
    return rewrite_references(kept, remap), remap


def _prefer_previous(
    previous: Collection[str],
) -> Callable[[BusinessDefinition], tuple[bool, bool]]:
    """統合で残す定義の順位: 前の定義（下書き・公開版）の名前 → AI 以外が作ったもの。

    同じ順位なら先に出たものを残す（`min` は最初の最小を返す）。利用者が付けた名前・前の名前を、
    AI の名付けの揺れで上書きしない（#1734）。
    """

    return lambda d: (d.api_name not in previous, d.origin == "ai")


def _dedupe_properties(
    values: list[BusinessDefinition], previous: Collection[str] = ()
) -> tuple[list[BusinessDefinition], dict[str, str]]:
    """同じオブジェクト型の同じ列に対応するプロパティを 1 つにする（名前が違っても）。

    残すのは前の定義の名前（#1734）。
    """

    remap: dict[str, str] = {}
    groups: dict[tuple[str, frozenset[str]], list[BusinessDefinition]] = {}
    for d in values:
        if not (isinstance(d, PropertyDefinition) and d.kind == "property"):
            continue
        columns = _column_signature(d)
        if not columns or len(columns) != len(d.mappings):
            continue
        groups.setdefault((d.object_type, columns), []).append(d)
    rank = _prefer_previous(previous)
    for group in groups.values():
        survivor = min(group, key=rank)
        for other in group:
            if other is not survivor:
                _absorb(survivor, other)
                remap[other.api_name] = survivor.api_name
    if not remap:
        return values, {}
    kept = [d for d in values if not (d.kind == "property" and d.api_name in remap)]
    return rewrite_references(kept, remap), remap


def shared_property_columns(values: list[BusinessDefinition]) -> dict[str, frozenset[str]]:
    """共有プロパティ・値型の API 名 → それを使うプロパティの物理の列（と自身の列）の組。"""

    used: dict[str, set[str]] = {}
    for d in values:
        if d.kind in {"shared_property", "value_type"}:
            used.setdefault(d.api_name, set()).update(_column_signature(d))
        if isinstance(d, PropertyDefinition) and d.kind == "property":
            for field in ("shared_property", "value_type"):
                name = getattr(d, field, "")
                if name:
                    used.setdefault(name, set()).update(_column_signature(d))
    return {name: frozenset(columns) for name, columns in used.items()}


def _dedupe_shared(
    values: list[BusinessDefinition], previous: Collection[str] = ()
) -> tuple[list[BusinessDefinition], dict[str, str]]:
    """同じ物の共有プロパティ・値型を 1 つにする（再構築で同じものが増えない。#1734）。

    同一性は名前ではなく、それを使うプロパティの物理の列の組（と業務の型）で決める。列の組が
    分からないもの（どのプロパティからも使われていない）は、表示名と業務の型が同じものに寄せる。
    残すのは前の定義の名前で、他方の名前は別名（altLabel）にする。
    """

    shared = [d for d in values if d.kind in {"shared_property", "value_type"}]
    if len(shared) < 2:
        return values, {}
    columns = shared_property_columns(values)
    parent: dict[int, BusinessDefinition] = {id(d): d for d in shared}

    def root(d: BusinessDefinition) -> BusinessDefinition:
        while parent[id(d)] is not d:
            d = parent[id(d)]
        return d

    def join(left: BusinessDefinition, right: BusinessDefinition) -> None:
        a, b = root(left), root(right)
        if a is not b:
            parent[id(b)] = a

    by_columns: dict[tuple[str, frozenset[str], str], BusinessDefinition] = {}
    by_name: dict[tuple[str, str, str], BusinessDefinition] = {}
    for d in shared:
        family = _type_family(getattr(d, "data_type", ""))
        signature = columns.get(d.api_name, frozenset())
        if signature:
            first = by_columns.setdefault((d.kind, signature, family), d)
            if first is not d:
                join(first, d)
        name_key = (d.kind, d.name_ja.strip(), family)
        first = by_name.setdefault(name_key, d)
        # 列の組が食い違う同じ名前の 2 つは別の物（名前では併合しない）。片方が未使用なら寄せる。
        if first is not d and not (
            signature and columns.get(first.api_name) and signature != columns[first.api_name]
        ):
            join(first, d)
    groups: dict[int, list[BusinessDefinition]] = {}
    for d in shared:
        groups.setdefault(id(root(d)), []).append(d)
    rank = _prefer_previous(previous)
    remap: dict[str, str] = {}
    for group in groups.values():
        if len(group) < 2:
            continue
        survivor = min(
            group, key=lambda d: (*rank(d)[:2], -len(columns.get(d.api_name, ())), d.api_name)
        )
        for other in group:
            if other is not survivor:
                _absorb(survivor, other)
                remap[other.api_name] = survivor.api_name
    if not remap:
        return values, {}
    kept = [
        d
        for d in values
        if not (d.kind in {"shared_property", "value_type"} and d.api_name in remap)
    ]
    rewritten = rewrite_references(kept, remap)
    for item in rewritten:
        # 自分を指す参照（AI が共有プロパティ自身に書いた shared_property）は付けない。
        if isinstance(item, PropertyDefinition) and item.shared_property == item.api_name:
            item.shared_property = ""
    return rewritten, remap


def _drop_unused_shared(
    values: list[BusinessDefinition],
) -> tuple[list[BusinessDefinition], list[BusinessDefinition]]:
    """どのプロパティからも使われない共有プロパティのうち、AI が作ったものを外す（#1734）。

    利用者が作った（AI 以外の）ものは残す。外したものは呼出元が利用者に伝え、前の版にあれば
    公開前の確認の差分に「削除」として出る。
    """

    columns = shared_property_columns(values)
    referenced = {
        name
        for d in values
        for _field, name in definition_references(d)
        if not (d.kind == "shared_property" and name == d.api_name)
    }
    dropped: list[BusinessDefinition] = [
        d
        for d in values
        if d.kind == "shared_property"
        and d.origin == "ai"
        and not columns.get(d.api_name)
        and d.api_name not in referenced
    ]
    if not dropped:
        return values, []
    return [d for d in values if all(d is not x for x in dropped)], dropped


def _dedupe_links(
    values: list[BusinessDefinition], previous: Collection[str] = ()
) -> tuple[list[BusinessDefinition], dict[str, str]]:
    """同じ両端・同じ結合のキーのリンク型を 1 つにする（残すのは前の定義の名前。#1736）。"""

    remap: dict[str, str] = {}
    groups: dict[tuple[frozenset[str], str], list[BusinessDefinition]] = {}
    for d in values:
        if d.kind != "link_type":
            continue
        join = join_key(getattr(d, "join_expression_sql", ""))
        if not join:
            continue
        key = (frozenset((getattr(d, "source", ""), getattr(d, "target", ""))), join)
        groups.setdefault(key, []).append(d)
    rank = _prefer_previous(previous)
    for group in groups.values():
        survivor = min(group, key=rank)
        for d in group:
            if d is not survivor:
                _absorb(survivor, d)
                remap[d.api_name] = survivor.api_name
    if not remap:
        return values, {}
    kept = [d for d in values if not (d.kind == "link_type" and d.api_name in remap)]
    return rewrite_references(kept, remap), remap


def _drop_unmapped_objects(
    values: list[BusinessDefinition],
) -> tuple[list[BusinessDefinition], list[BusinessDefinition]]:
    """まとめても表・ビューが決まらないオブジェクト型と、それに属する定義を外す（#1729）。

    graph に置けないため公開を止めず、今回の公開に含めない（best effort）。外した定義は呼出元が
    利用者に伝える。
    """

    dropped: list[BusinessDefinition] = [
        d for d in values if d.kind == "object_type" and not d.mappings
    ]
    if not dropped:
        return values, []
    names = {d.api_name for d in dropped}
    kept = [d for d in values if d.api_name not in names or d.kind != "object_type"]
    while True:
        cascade = [
            d
            for d in kept
            if (d.kind == "property" and getattr(d, "object_type", "") in names)
            or (
                d.kind == "link_type"
                and {getattr(d, "source", ""), getattr(d, "target", "")} & names
            )
            or (
                d.kind in {"action_type", "business_event", "object_set"}
                and getattr(d, "object_type", "") in names
            )
            or (d.kind == "enumeration" and getattr(d, "property", "") in names)
        ]
        if not cascade:
            break
        dropped.extend(cascade)
        names.update(d.api_name for d in cascade)
        kept = [d for d in kept if all(d is not c for c in cascade)]
    cleaned = []
    for d in kept:
        raw = d.model_dump(mode="json")
        for field, value in definition_references(d):
            current = raw.get(field)
            if isinstance(current, str) and current in names:
                raw[field] = ""
            elif isinstance(current, list) and value in names:
                raw[field] = [v for v in current if not (isinstance(v, str) and v in names)]
        cleaned.append(raw)
    return DEFINITIONS.validate_python(cleaned), dropped


# ---------------------------------------------------------------------------
# 外部キー・同じ名前と型の ID 列からのリンク型（#1720・#1729）
# ---------------------------------------------------------------------------


def view_dependency_pairs(schema: dict[str, Any] | None) -> set[frozenset[str]]:
    """ビューと、その元の表・ビュー（`OWNER.OBJECT` の照合キー）の組。"""

    return {
        frozenset(
            (
                object_match_key(str(r.get("source_object", ""))),
                object_match_key(str(r.get("target_object", ""))),
            )
        )
        for r in (schema or {}).get("relationships", [])
        if r.get("kind") == "view_dependency"
    }


def _infer_links(
    profile_id: str, values: list[BusinessDefinition], schema: dict[str, Any] | None
) -> list[BusinessDefinition]:
    """表の間の関係を、外部キーの制約か、同じ名前・型の ID 列から作る（AI が作らなかった分）。

    同じ名前の列は、相手の表の主キー（制約。制約の無い表は「表名_ID」）のときだけ関係にする。
    `ID` だけの列名や、両方の主キーが同じ列（1 対 1 か判断できない）は推測しない。
    """

    tables = schema_tables(schema)
    objects: dict[str, ObjectTypeDefinition] = {}
    for d in values:
        if isinstance(d, ObjectTypeDefinition):
            signature = _table_signature(d)
            if len(signature) == 1 and next(iter(signature)) in tables:
                objects.setdefault(next(iter(signature)), d)
    by_name = {d.api_name: d for d in values}

    def unique_keys(table_key: str) -> list[frozenset[str]]:
        # 主キー・一意キーの制約と、オブジェクト型の主キー（ビューには制約が無い）の列の組。
        keys = list(tables[table_key]["unique_keys"])
        columns: set[str] = set()
        for name in objects[table_key].primary_key:
            prop = by_name.get(name)
            mapped = [
                m
                for m in getattr(prop, "mappings", [])
                if _plain_column(m) and mapping_object_key(m) == table_key
            ]
            if len(mapped) != 1:
                return keys
            columns.add(mapping_column_key(mapped[0]))
        if columns:
            keys.append(frozenset(columns))
        return keys

    def to_one(source_key: str, source_columns: Iterable[str]) -> str:
        # 起点の列の組が起点の主キー・一意キーなら 1 対 1、そうでなければ多対 1（#1736）。
        return (
            "one_to_one" if frozenset(source_columns) in unique_keys(source_key) else "many_to_one"
        )

    existing = {
        frozenset((getattr(d, "source", ""), getattr(d, "target", "")))
        for d in values
        if d.kind == "link_type"
    }
    names = {d.api_name for d in values}
    created: list[BusinessDefinition] = []

    def add(
        source_key: str,
        target_key: str,
        pairs: list[tuple[str, str]],
        cardinality: str,
        description: str,
    ) -> None:
        source, target = objects[source_key], objects[target_key]
        left, right = tables[source_key], tables[target_key]
        join = " AND ".join(
            f"{left['qualified']}.{left['columns'][a]['column']} = "
            f"{right['qualified']}.{right['columns'][b]['column']}"
            for a, b in pairs
        )
        if frozenset((source.api_name, target.api_name)) in existing:
            # AI が結合のキーの無いリンク型を作っていれば、制約・同じ名前の列の結合のキーで
            # 補う（向きが逆なら多重度も逆にする。#1733）。
            for d in values:
                if (
                    isinstance(d, LinkTypeDefinition)
                    and {d.source, d.target} == {source.api_name, target.api_name}
                    and not d.join_expression_sql.strip()
                ):
                    d.join_expression_sql = join
                    if d.cardinality == "unknown":
                        reverse = {"many_to_one": "one_to_many", "one_to_many": "many_to_one"}
                        oriented: Any = (
                            cardinality
                            if d.source == source.api_name
                            else reverse.get(cardinality, cardinality)
                        )
                        d.cardinality = oriented
            return
        existing.add(frozenset((source.api_name, target.api_name)))
        name = _identifier(f"{source.api_name}_to_{target.api_name}")
        suffix = 2
        while name in names:
            name = _identifier(f"{source.api_name}_to_{target.api_name}_{suffix}")
            suffix += 1
        names.add(name)
        optional = any(left["columns"][a].get("nullable", True) is not False for a, _ in pairs)
        created.append(
            LinkTypeDefinition(
                api_name=name,
                id=stable_ontology_id("profile_concept", profile_id, "link_type", name),
                name_ja=f"{source.name_ja}の{target.name_ja}",
                inverse_name_ja=f"{target.name_ja}の{source.name_ja}",
                description_ja=description,
                source=source.api_name,
                target=target.api_name,
                cardinality=cardinality,
                optional=optional,
                join_expression_sql=join,
                evidence=[
                    DefinitionEvidence(
                        source_id="schema",
                        locator=join,
                        source_kind="database",
                        assertion="join_expression_sql",
                    )
                ],
                origin="ai",
            )
        )

    for relationship in (schema or {}).get("relationships", []):
        if relationship.get("kind") != "foreign_key":
            continue
        source_key = object_match_key(str(relationship.get("source_object", "")))
        target_key = object_match_key(str(relationship.get("target_object", "")))
        if source_key not in objects or target_key not in objects or source_key == target_key:
            continue
        pairs = []
        for condition in relationship.get("join_conditions", []):
            left = object_match_key(str(condition.get("left", ""))).rsplit(".", 1)[-1]
            right = object_match_key(str(condition.get("right", ""))).rsplit(".", 1)[-1]
            if left in tables[source_key]["columns"] and right in tables[target_key]["columns"]:
                pairs.append((left, right))
        if not pairs:
            continue
        cardinality = str(relationship.get("cardinality") or "many_to_one")
        if cardinality not in {"one_to_one", "one_to_many", "many_to_one", "many_to_many"}:
            cardinality = "many_to_one"
        add(
            source_key,
            target_key,
            pairs,
            cardinality,
            f"外部キーの制約（{relationship['id']}）による関係。"
            if relationship.get("id")
            else "外部キーの制約による関係。",
        )

    def key_columns(table_key: str) -> list[str]:
        table = tables[table_key]
        if table["primary_key"]:
            return list(table["primary_key"])
        guess = object_match_key(f"{table['object_name']}_ID")
        if table["kind"] != "view" and not table["has_constraints"] and guess in table["columns"]:
            return [guess]
        return []

    for source_key in objects:
        for target_key in objects:
            if source_key == target_key:
                continue
            keys = key_columns(target_key)
            if len(keys) != 1 or keys[0] == "ID":
                continue
            column = keys[0]
            left, right = tables[source_key]["columns"], tables[target_key]["columns"]
            if column not in left or tables[source_key]["primary_key"] == [column]:
                continue
            # 型が分からない（空・読めない）列は、一致とも不一致ともしない。名前と相手の主キーとの
            # 対応で補う（#1733）。
            types = {
                _type_family(str(left[column].get("data_type", ""))),
                _type_family(str(right[column].get("data_type", ""))),
            } - {""}
            if len(types) > 1:
                continue
            add(
                source_key,
                target_key,
                [(column, column)],
                to_one(source_key, [column]),
                f"両方の表・ビューにある同じ名前・型の列 {right[column]['column']} で結合する関係"
                "（外部キーの制約は無い）。",
            )
    return created


class RepairResult:
    """`repair_definitions` の結果。統合で消えた名前（→ 残した名前）と、外した定義を持つ。"""

    def __init__(
        self,
        definitions: list[BusinessDefinition],
        remap: dict[str, str],
        dropped: list[BusinessDefinition],
        unused_shared: list[BusinessDefinition] | None = None,
        unmapped_properties: list[BusinessDefinition] | None = None,
        keyless_links: list[BusinessDefinition] | None = None,
        nonkey_links: list[BusinessDefinition] | None = None,
        nonkey_columns: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self.definitions = definitions
        self.remap = remap
        self.dropped = dropped
        # どのプロパティからも使われないため外した、AI の共有プロパティ（#1734）。
        self.unused_shared = unused_shared or []
        # 対応する列が決まらないため外した、AI のプロパティ（#1736）。
        self.unmapped_properties = unmapped_properties or []
        # ビューの依存だけで結合のキーが無いため外した、AI のリンク型（#1736）。
        self.keyless_links = keyless_links or []
        # 主キー・一意キー・外部キーでない列でつなぎ、付け替えられないため外した AI のリンク型
        # （結合のキーは AI の出力のまま）と、その結合のキーの列（`OBJECT.COLUMN`。#1741）。
        self.nonkey_links = nonkey_links or []
        self.nonkey_columns = nonkey_columns or {}


def repair_definitions_report(
    profile_id: str,
    definitions: list[BusinessDefinition],
    schema: dict[str, Any] | None = None,
    *,
    ai_output: bool = False,
    previous: Collection[str] = (),
) -> RepairResult:
    """AI の出力・Markdown の解析結果を graph にできる形へ直す（#1720・#1729）。

    1. 同じ表・ビューを指すオブジェクト型を 1 つにまとめる（表の無い方は名前・別名で寄せる）。
    2. オブジェクト型の properties / 主キー（Primary Key）が他のオブジェクト型のプロパティや未定義の
       名前を指すときは、そのオブジェクト型の表の列に対応するプロパティを作って付け替える。
    3. 同じオブジェクト型の同じ列のプロパティ、同じ両端・結合のキーのリンク型を 1 つにする。
    4. まとめても表が決まらないオブジェクト型は外す（公開を止めない。外した定義を返す）。
    5. プロパティの型を業務の型（Base Type）と物理の型（Physical Type）に分ける。
    `ai_output` のときは、graph に置けない雛形・構造の検査と重なる不足情報を除き、外部キーか
    同じ名前・型の ID 列から AI が作らなかったリンク型（Link Type）を足し、どのプロパティからも
    使われない AI の共有プロパティを外す。`previous` は前の定義（下書き・公開版）の API 名で、
    同じ列のプロパティ・同じ物の共有プロパティをまとめるときに残す名前にする（#1734）。
    `ai_output` のときは、対応する列の無い AI のプロパティは、共有プロパティ・値型の列か API 名と
    同じ列（所属の表のもの）で補い、決まらなければ外す。結合のキーの無い AI のリンク型は、同じ
    両端のリンク型に寄せ、ビューの依存だけのものは外す（#1736）。前の定義と利用者の定義は外さない。
    """

    values = [d.model_copy(deep=True) for d in definitions]
    pruned: dict[str, str] = {}
    if ai_output:
        # AI 構築の出力だけ。利用者が Markdown に書いた定義は消さず、検証の診断に任せる。
        values, pruned = _prune_ai_output(values, previous)
    _separate_name_clashes(values)
    for d in values:
        # 空の項目の書き写しは構造の検査（主キー・粒度・型・多重度の info）と重なるため除く。
        d.missing_information_ja = [
            note for note in d.missing_information_ja if not _STRUCTURAL_NOTE.search(note)
        ]
    values, remap = _unify_objects(values)
    remap = {**pruned, **remap}
    _repair_owner_references(values)
    by_name: dict[str, BusinessDefinition] = {d.api_name: d for d in values}
    columns = _schema_columns(schema)
    added: list[BusinessDefinition] = []
    for obj in [d for d in values if isinstance(d, ObjectTypeDefinition)]:
        repair = _ObjectPropertyRepair(profile_id, obj, by_name, columns, added)
        obj.properties = repair.names(obj.properties)
        obj.primary_key = repair.names(obj.primary_key)
        if obj.title_property:
            obj.title_property = repair.resolve(obj.title_property) or ""
    values = [*values, *added]
    if ai_output:
        _recover_property_mappings(values, columns, previous)
    values, property_remap = _dedupe_properties(values, previous)
    values, link_remap = _dedupe_links(values, previous)
    values, shared_remap = _dedupe_shared(values, previous)
    remap.update(property_remap)
    remap.update(link_remap)
    remap.update(shared_remap)
    values, dropped = _drop_unmapped_objects(values)
    _fill_property_types(values, columns)
    _borrow_business_names(values)
    if ai_output:
        _repair_optional_references(values)
    nonkey: dict[str, tuple[str, tuple[str, ...]]] = {}
    if ai_output and schema:
        # 主キー・一意キー・外部キーでない列の結合のキーは外し、外部キー・同じ名前の ID 列の
        # 結合のキーで付け替える（#1741）。
        nonkey = _unkey_nonunique_links(values, schema, previous)
        values = [*values, *_infer_links(profile_id, values, schema)]
    _fill_link_optional(values, columns)
    unused: list[BusinessDefinition] = []
    unmapped: list[BusinessDefinition] = []
    keyless: list[BusinessDefinition] = []
    nonkey_dropped: list[BusinessDefinition] = []
    if ai_output:
        # 補った結合のキーが既存のリンク型と同じなら 1 つにし、残りの結合のキーの無いものを
        # 同じ両端で 1 つにする（#1736）。
        values, inferred_remap = _dedupe_links(values, previous)
        remap.update(inferred_remap)
        values, nonkey_dropped = _drop_nonkey_links(values, nonkey)
        values, keyless_remap, keyless = _settle_keyless_links(values, schema, previous)
        remap.update(keyless_remap)
        values, unmapped = _drop_unmapped_properties(values, previous)
        values, unused = _drop_unused_shared(values)
    ordered = sorted(values, key=lambda d: (CONCEPT_ORDER.index(d.kind), d.api_name))
    return RepairResult(
        ordered,
        remap,
        dropped,
        unused,
        unmapped,
        keyless,
        nonkey_dropped,
        {d.api_name: nonkey[d.api_name][1] for d in nonkey_dropped},
    )


def _removable_ai(d: BusinessDefinition, previous: Collection[str]) -> bool:
    """AI 構築が外してよい定義（今回の AI の出力で、前の定義にも利用者の定義にも無い）。"""

    return d.origin == "ai" and d.api_name not in previous


def _without_references(
    values: list[BusinessDefinition], names: Collection[str]
) -> list[BusinessDefinition]:
    """外した定義を指す参照（オブジェクト型の properties・主キーなど）を空にする。"""

    cleaned = []
    for d in values:
        raw = d.model_dump(mode="json")
        for field, value in definition_references(d):
            current = raw.get(field)
            if isinstance(current, str) and current in names:
                raw[field] = ""
            elif isinstance(current, list) and value in names:
                raw[field] = [v for v in current if not (isinstance(v, str) and v in names)]
        cleaned.append(raw)
    return DEFINITIONS.validate_python(cleaned)


def _recover_property_mappings(
    values: list[BusinessDefinition],
    columns: dict[str, dict[str, dict[str, Any]]],
    previous: Collection[str],
) -> None:
    """対応する列の無い AI のプロパティに、所属のオブジェクト型の表の列を補う（#1736）。

    根拠は物理の列だけ: (1) 参照する共有プロパティ・値型の対応のうち、所属の表の列がちょうど 1 つ
    (2) 所属の表がちょうど 1 つで、API 名（所属の API 名の接頭辞を除く）と同じ名前の列が DB にある。
    決まらなければそのままにする（`_drop_unmapped_properties` が外す）。
    """

    by_name = {d.api_name: d for d in values}
    for d in values:
        if not (
            isinstance(d, PropertyDefinition)
            and d.kind == "property"
            and not d.mappings
            and _removable_ai(d, previous)
        ):
            continue
        owner = by_name.get(d.object_type)
        if not isinstance(owner, ObjectTypeDefinition):
            continue
        sources = {mapping_object_key(m): m for m in owner.mappings if m.object_name}
        borrowed = {
            (mapping_object_key(m), mapping_column_key(m)): m
            for name in (d.shared_property, d.value_type)
            if name and name != d.api_name and (shared := by_name.get(name)) is not None
            for m in shared.mappings
            if _plain_column(m) and mapping_object_key(m) in sources
        }
        found: tuple[str, str] | None = None
        if len(borrowed) == 1:
            (table, _key), mapping = next(iter(borrowed.items()))
            found = (table, mapping.column_name)
        elif not borrowed and len(sources) == 1:
            table = next(iter(sources))
            prefix = f"{owner.api_name}_"
            for bare in dict.fromkeys([d.api_name, d.api_name.removeprefix(prefix)]):
                info = columns.get(table, {}).get(object_match_key(bare))
                if info is not None:
                    found = (table, str(info.get("column") or bare))
                    break
        if found is None:
            continue
        table, column = found
        source = sources[table]
        d.mappings = [
            DefinitionMapping(
                owner=source.owner, object_name=source.object_name, column_name=column
            )
        ]
        d.evidence = [
            *d.evidence,
            DefinitionEvidence(
                source_id="schema",
                locator=f"{table}.{object_match_key(column)}",
                source_kind="database",
                assertion="mappings",
            ),
        ]


def _drop_unmapped_properties(
    values: list[BusinessDefinition], previous: Collection[str]
) -> tuple[list[BusinessDefinition], list[BusinessDefinition]]:
    """対応する列が決まらない AI のプロパティを外す（graph に置けず、公開を止めるため。#1736）。

    前の定義と利用者が書いたプロパティは外さない（検証の診断で直してもらう）。外したものは
    呼出元が構築の注記で知らせる。
    """

    dropped: list[BusinessDefinition] = [
        d
        for d in values
        if isinstance(d, PropertyDefinition)
        and d.kind == "property"
        and not d.mappings
        and _removable_ai(d, previous)
    ]
    if not dropped:
        return values, []
    names = {d.api_name for d in dropped}
    kept = [d for d in values if all(d is not x for x in dropped)]
    return _without_references(kept, names), dropped


def _unkey_nonunique_links(
    values: list[BusinessDefinition],
    schema: dict[str, Any] | None,
    previous: Collection[str],
) -> dict[str, tuple[str, tuple[str, ...]]]:
    """主キー・一意キー・外部キーでない列でつなぐ AI のリンク型の結合のキーを外す（#1741）。

    外した後は `_infer_links` が、外部キーの制約か同じ名前・型の ID 列の結合のキーを補う
    （付け替え）。戻り値は外したリンク型の API 名 →（元の結合のキー, 結合のキーの列）。
    前の定義と利用者のリンク型は変えない（公開前の確認の警告で直してもらう）。
    """

    by_name = {d.api_name: d for d in values}
    tables = schema_tables(schema)
    result: dict[str, tuple[str, tuple[str, ...]]] = {}
    for d in values:
        if not isinstance(d, LinkTypeDefinition) or not _removable_ai(d, previous):
            continue
        check = link_join_check(d, by_name, schema, tables)
        if check.keyed is not False:
            continue
        result[d.api_name] = (d.join_expression_sql, check.columns)
        d.join_expression_sql = ""
        # 多重度は一意でない列から決めたもの。付け替えた結合のキーで決め直す。
        d.cardinality = "unknown"
    return result


def _drop_nonkey_links(
    values: list[BusinessDefinition], nonkey: dict[str, tuple[str, tuple[str, ...]]]
) -> tuple[list[BusinessDefinition], list[BusinessDefinition]]:
    """付け替えられなかった（一意でない列でしかつなげない）AI のリンク型を外す（#1741）。

    同じ両端に結合のキーのあるリンク型があれば外さず、`_settle_keyless_links` がそちらに寄せる。
    外したものは元の結合のキーに戻して返す（構築の注記に使う）。
    """

    if not nonkey:
        return values, []
    keyed_ends = {
        frozenset((d.source, d.target))
        for d in values
        if isinstance(d, LinkTypeDefinition) and d.join_expression_sql.strip()
    }
    links = [
        d
        for d in values
        if isinstance(d, LinkTypeDefinition)
        and d.api_name in nonkey
        and not d.join_expression_sql.strip()
        and frozenset((d.source, d.target)) not in keyed_ends
    ]
    if not links:
        return values, []
    kept = [d for d in values if all(d is not x for x in links)]
    for link in links:
        link.join_expression_sql = nonkey[link.api_name][0]
    dropped: list[BusinessDefinition] = list(links)
    return _without_references(kept, {d.api_name for d in dropped}), dropped


_JOIN_NOTE = re.compile(r"(join_expression_sql|join|結合)", re.IGNORECASE)


def _settle_keyless_links(
    values: list[BusinessDefinition],
    schema: dict[str, Any] | None,
    previous: Collection[str],
) -> tuple[list[BusinessDefinition], dict[str, str], list[BusinessDefinition]]:
    """結合のキーの無い AI のリンク型を、同じ両端で 1 つにする（#1736）。

    - 同じ両端に結合のキーのあるリンク型があれば、そちらに寄せる（名前は別名にする）。
    - 同じ両端に結合のキーの無いものしか無ければ、1 つにまとめる（残すのは前の定義の名前）。
    - ビューと、その元の表・ビューの間の依存（data lineage）だけで結合のキーが無いものは外す
      （結合できる列の対応が無く、リンク型にならない）。
    前の定義と利用者のリンク型は外さない。結合のキーのあるリンク型の、結合のキーの不足情報は除く。
    """

    links = [d for d in values if isinstance(d, LinkTypeDefinition)]
    for d in links:
        if d.join_expression_sql.strip():
            d.missing_information_ja = [
                note for note in d.missing_information_ja if not _JOIN_NOTE.search(note)
            ]
    tables: dict[str, str] = {}
    for obj in values:
        if isinstance(obj, ObjectTypeDefinition):
            signature = _table_signature(obj)
            if len(signature) == 1:
                tables[obj.api_name] = next(iter(signature))
    lineage = view_dependency_pairs(schema)
    groups: dict[frozenset[str], list[LinkTypeDefinition]] = {}
    for d in links:
        groups.setdefault(frozenset((d.source, d.target)), []).append(d)
    rank = _prefer_previous(previous)
    remap: dict[str, str] = {}
    dropped: list[BusinessDefinition] = []
    for ends, group in groups.items():
        keyless = [d for d in group if not d.join_expression_sql.strip()]
        removable = [d for d in keyless if _removable_ai(d, previous)]
        if not removable:
            continue
        keyed = [d for d in group if d.join_expression_sql.strip()]
        kept = [d for d in group if all(d is not r for r in removable)]
        if keyed or kept:
            survivor = min(kept, key=lambda d: (*rank(d), not d.join_expression_sql.strip()))
        elif frozenset(tables.get(name, "") for name in ends) in lineage:
            dropped.extend(removable)
            continue
        else:
            survivor = min(removable, key=rank)
        for d in removable:
            if d is not survivor:
                _absorb(survivor, d)
                remap[d.api_name] = survivor.api_name
    if not remap and not dropped:
        return values, {}, []
    gone = {id(d) for d in dropped} | {
        id(d) for d in links if d.api_name in remap and remap[d.api_name] != d.api_name
    }
    kept_values = [d for d in values if id(d) not in gone]
    kept_values = rewrite_references(kept_values, remap)
    if dropped:
        kept_values = _without_references(kept_values, {d.api_name for d in dropped})
    return kept_values, remap, dropped


def _repair_owner_references(values: list[BusinessDefinition]) -> None:
    """所属・両端が無いオブジェクト型（`object_type` など種類の名前）を指すとき、物理の表で直す。

    プロパティは列の表、リンク型は結合のキーの表が、ちょうど 1 つのオブジェクト型の表なら、
    その オブジェクト型に付け替える（名前ではなく物理の表・列を根拠にする。#1729・#1734）。
    決まらなければそのままにし、検証の診断に任せる。
    """

    objects = {d.api_name for d in values if d.kind == "object_type"}
    by_table: dict[str, list[str]] = {}
    for d in values:
        if isinstance(d, ObjectTypeDefinition):
            signature = _table_signature(d)
            if len(signature) == 1:
                by_table.setdefault(next(iter(signature)), []).append(d.api_name)

    def owner_of(table: str) -> str:
        owners = by_table.get(table, [])
        return owners[0] if len(owners) == 1 else ""

    for d in values:
        if isinstance(d, PropertyDefinition) and d.kind == "property":
            if d.object_type in objects:
                continue
            tables = {mapping_object_key(m) for m in d.mappings if _plain_column(m)}
            owner = owner_of(next(iter(tables))) if len(tables) == 1 else ""
            if owner:
                d.object_type = owner
        elif isinstance(d, LinkTypeDefinition):
            if d.source in objects and d.target in objects:
                continue
            sides: list[tuple[str, ...]] = []
            for condition in re.split(r"\s+AND\s+", d.join_expression_sql.strip(), flags=re.I):
                parts = [p.strip() for p in condition.split("=")]
                if len(parts) != 2:
                    sides = []
                    break
                sides.append(tuple(object_match_key(p).rsplit(".", 1)[0] for p in parts))
            ends = {tuple(owner_of(t) for t in pair) for pair in sides}
            if len(ends) != 1:
                continue
            left, right = next(iter(ends))
            if not left or not right or left == right:
                continue
            if d.source in objects and d.target not in objects:
                d.target = right if d.source == left else left if d.source == right else d.target
            elif d.target in objects and d.source not in objects:
                d.source = left if d.target == right else right if d.target == left else d.source
            elif d.source not in objects and d.target not in objects:
                d.source, d.target = left, right


def _separate_name_clashes(values: list[BusinessDefinition]) -> None:
    """種類の違う定義が同じ API 名（`project_id` のプロパティと共有プロパティなど）のとき、
    オブジェクト型・プロパティ・リンク型を残し、他方に種類の名前を付けて分ける（#1729）。

    共有プロパティ・値型の参照（`shared_property` / `value_type`）は分けた名前に付け替える。
    """

    priority = {"object_type": 0, "property": 1, "link_type": 2}
    groups: dict[str, list[BusinessDefinition]] = {}
    for d in values:
        groups.setdefault(d.api_name, []).append(d)
    names = {d.api_name for d in values}
    renamed: dict[tuple[str, str], str] = {}
    for name, group in groups.items():
        if len({d.kind for d in group}) < 2:
            continue
        keep = min(group, key=lambda d: priority.get(d.kind, 9))
        for d in group:
            if d is keep or d.kind == keep.kind:
                continue
            new = _identifier(f"{name}_{d.kind}")
            suffix = 2
            while new in names:
                new = _identifier(f"{name}_{d.kind}_{suffix}")
                suffix += 1
            names.add(new)
            renamed[(d.kind, name)] = new
            d.api_name = new
    if not renamed:
        return
    for d in values:
        for field in ("shared_property", "value_type"):
            current = getattr(d, field, "")
            if current and (field, current) in renamed:
                setattr(d, field, renamed[(field, current)])


def _repair_optional_references(values: list[BusinessDefinition]) -> None:
    """AI のプロパティの共有プロパティ・値型の参照が別の種類を指すとき、同じ名前の正しい種類に
    付け替え、無ければ外す（任意の項目なので graph を壊さず公開を止めない。#1729）。"""

    for field, kind in (("shared_property", "shared_property"), ("value_type", "value_type")):
        candidates = [d for d in values if d.kind == kind]
        for d in values:
            name = getattr(d, field, "") if isinstance(d, PropertyDefinition) else ""
            if not name or any(c.api_name == name for c in candidates):
                continue
            target = next((v for v in values if v.api_name == name), None)
            label = target.name_ja if target is not None else ""
            matches = [
                c
                for c in candidates
                if c.name_ja == (label or d.name_ja)
                or concept_name_key(c.api_name).startswith(concept_name_key(name))
            ]
            setattr(d, field, matches[0].api_name if len(matches) == 1 else "")
        for d in values:
            # 業務の型が違う共有プロパティ・値型は、同じ意味とはいえないため参照を外す。
            target = next((c for c in candidates if c.api_name == getattr(d, field, "")), None)
            if (
                isinstance(d, PropertyDefinition)
                and isinstance(target, PropertyDefinition)
                and target.data_type
                and d.data_type
                and _type_family(target.data_type) != _type_family(d.data_type)
            ):
                setattr(d, field, "")


def repair_definitions(
    profile_id: str,
    definitions: list[BusinessDefinition],
    schema: dict[str, Any] | None = None,
    *,
    ai_output: bool = False,
) -> list[BusinessDefinition]:
    """`repair_definitions_report` の定義だけを返す。"""

    return repair_definitions_report(
        profile_id, definitions, schema, ai_output=ai_output
    ).definitions


def _human(value: Any) -> str:
    if isinstance(value, dict):
        if value.get("owner") and value.get("object_name"):
            physical = ".".join(
                str(value[k]) for k in ("owner", "object_name", "column_name") if value.get(k)
            )
            expression = str(value.get("expression_sql") or "")
            # 列そのものの式（`ADMIN.T.C`）は重複になるため書かない（#1720）。
            if not expression or object_match_key(expression) == object_match_key(physical):
                return physical
            return physical + f"（式: {expression}）"
        return "、".join(
            f"{FIELD_LABELS.get(k, k)}: {_human(v)}"
            for k, v in value.items()
            if v not in ("", [], None)
        )
    if isinstance(value, list):
        return " / ".join(_human(v) for v in value)
    if isinstance(value, bool):
        return "はい" if value else "いいえ"
    return str(value).replace("\n", " / ")


def render_concepts(
    definitions: list[BusinessDefinition],
    conflicts: list[str] | None = None,
    coverage: list[Any] | None = None,
) -> str:
    lines = ["## オントロジーの概念定義"]
    for kind, label in CONCEPT_LABELS.items():
        matches = [d for d in definitions if d.kind == kind]
        if not matches:
            continue
        lines.extend(["", f"#### {label}", ""])
        for d in matches:
            lines.append(f"##### {d.name_ja} (`{d.api_name}`)")
            defaults = {
                name: info.get_default(call_default_factory=True)
                for name, info in type(d).model_fields.items()
            }
            for field, value in d.model_dump(mode="json").items():
                # 既定値（「必須: いいえ」「多重度: unknown」など）は書かない（#1720）。
                if field not in {"kind"} and value == defaults.get(field, object()):
                    continue
                if field in {
                    "id",
                    "api_name",
                    "name_ja",
                    "kind",
                    "review_status",
                    "origin",
                    "implementation_key",
                    "evidence",
                    "missing_information_ja",
                } or value in ("", [], None):
                    continue
                shown = (
                    VALUE_LABELS[value]
                    if field in {"data_type", "cardinality"} and value in VALUE_LABELS
                    else _human(value)
                )
                lines.append(f"- {FIELD_LABELS.get(field, field)}: {shown}")
            # 不足情報（missing_information_ja）は公開前の確認の「補うと精度が上がる項目」に出す。
            # Markdown の本文には書かない（#1720）。
            lines.append("")
    # 診断と根拠の構造は保存成果物に保持する。業務本文には混ぜない。
    return "\n".join(lines).rstrip() + "\n"


_CARDINALITY_JA = {
    key: VALUE_LABELS[key] for key in ("one_to_one", "one_to_many", "many_to_one", "many_to_many")
}


def _physical_label(mapping: Any) -> str:
    """`OWNER.OBJECT[.COLUMN]`。式が列そのものでないときだけ式を添える。"""

    path = ".".join(p for p in (mapping.owner, mapping.object_name, mapping.column_name) if p)
    expression = " ".join(str(mapping.expression_sql or "").split())
    if expression and object_match_key(expression) != object_match_key(path):
        return f"{path}（式: {expression}）" if mapping.column_name else expression
    return path


def _inline(text: str) -> str:
    return " ".join(str(text or "").split())


def _joined(value: Any) -> str:
    if isinstance(value, list):
        return "、".join(_inline(str(v)) for v in value if str(v).strip())
    return _inline(str(value or ""))


def _details(pairs: Iterable[tuple[str, Any]]) -> str:
    return "、".join(f"{label}: {text}" for label, value in pairs if (text := _joined(value)))


def _description(text: str) -> str:
    """統合で行ごとに足された説明を、重複を除いて 1 行にする。"""

    return " / ".join(dict.fromkeys(line.strip() for line in text.splitlines() if line.strip()))


def _with_details(head: str, details: str, description: str = "") -> str:
    text = head + (f"（{details}）" if details else "")
    return text + (f" {_description(description)}" if _description(description) else "")


def render_llm_context(definitions: Iterable[BusinessDefinition], qa_markdown: str = "") -> str:
    """Select AI / Select AI Agent / LLM に業務の知識として渡すオントロジーの Markdown（#1720）。

    読み手は LLM。SQL の生成に効く業務の知識（オブジェクトと表、プロパティと列、関係の結合条件、
    指標の式、業務ルール、用語と同義語）だけを簡潔に書く。根拠・検証の状態・不足情報・書き方の
    説明・空の項目は入れない（それらは公開前の確認と画面のために定義に保存している）。
    """

    values = list(definitions)
    by_name = {d.api_name: d for d in values}

    def label(name: str) -> str:
        target = by_name.get(name)
        return target.name_ja if target is not None else name

    def physical(d: BusinessDefinition) -> str:
        return " / ".join(dict.fromkeys(_physical_label(m) for m in d.mappings))

    def aliases(d: BusinessDefinition) -> list[str]:
        return [a for a in dict.fromkeys(d.aliases) if a and a != d.name_ja]

    lines: list[str] = ["# 業務オントロジー"]
    objects = [d for d in values if isinstance(d, ObjectTypeDefinition)]
    properties = [d for d in values if isinstance(d, PropertyDefinition) and d.kind == "property"]
    owned: dict[str, list[PropertyDefinition]] = {}
    for prop in properties:
        owned.setdefault(prop.object_type, []).append(prop)
    enumerations = {getattr(d, "property", ""): d for d in values if d.kind == "enumeration"}

    def property_line(prop: PropertyDefinition, indent: str = "") -> str:
        enum = enumerations.get(prop.api_name)
        allowed = list(prop.allowed_values)
        if enum is not None:
            allowed = [
                f"{v.code}={v.label_ja}" if v.label_ja and v.label_ja != v.code else v.code
                for v in getattr(enum, "values", [])
            ]
        details = _details(
            [
                ("列", physical(prop)),
                (plain_label("data_type"), VALUE_LABELS.get(prop.data_type, prop.data_type)),
                (plain_label("physical_type"), prop.physical_type),
                (plain_label("unit"), prop.unit),
                (plain_label("currency"), prop.currency),
                (plain_label("allowed_values"), allowed),
                (plain_label("aliases"), aliases(prop)),
            ]
        )
        return indent + "- " + _with_details(prop.name_ja, details, prop.description_ja)

    if objects:
        lines.extend(["", f"## {CONCEPT_LABELS['object_type']}"])
        for obj in objects:
            head = f"### {obj.name_ja}"
            table = " / ".join(
                dict.fromkeys(f"{m.owner}.{m.object_name}" for m in obj.mappings if m.object_name)
            )
            lines.extend(["", head + (f"（{table}）" if table else "")])
            if _description(obj.description_ja):
                lines.append(_description(obj.description_ja))
            facts = [
                (FIELD_LABELS["aliases"], aliases(obj)),
                (FIELD_LABELS["primary_key"], [label(n) for n in obj.primary_key]),
                (FIELD_LABELS["grain_ja"], obj.grain_ja),
                (
                    FIELD_LABELS["title_property"],
                    label(obj.title_property) if obj.title_property else "",
                ),
            ]
            lines.extend(f"- {name}: {value}" for name, raw in facts if (value := _joined(raw)))
            props = owned.get(obj.api_name, [])
            listed = [by_name[n] for n in obj.properties if n in by_name]
            ordered: list[PropertyDefinition] = []
            for prop in [p for p in listed if isinstance(p, PropertyDefinition)] + props:
                if all(prop is not seen for seen in ordered):
                    ordered.append(prop)
            if ordered:
                lines.append(f"- {FIELD_LABELS['properties']}:")
                lines.extend(property_line(p, "  ") for p in ordered)
    orphans = [p for p in properties if p.object_type not in {o.api_name for o in objects}]
    shared = [
        d for d in values if isinstance(d, PropertyDefinition) and d.kind == "shared_property"
    ]
    if shared:
        lines.extend(["", f"## {CONCEPT_LABELS['shared_property']}"])
        lines.extend(property_line(p) for p in shared)
    if orphans:
        lines.extend(["", f"## 所属の無い{CONCEPT_LABELS['property']}"])
        lines.extend(property_line(p) for p in orphans)
    links = [d for d in values if d.kind == "link_type"]
    if links:
        lines.extend(["", f"## {CONCEPT_LABELS['link_type']}"])
        for link in links:
            intermediary = getattr(link, "intermediary_object", "")
            details = _details(
                [
                    (
                        plain_label("cardinality"),
                        _CARDINALITY_JA.get(getattr(link, "cardinality", ""), ""),
                    ),
                    (plain_label("join_expression_sql"), getattr(link, "join_expression_sql", "")),
                    (
                        plain_label("intermediary_object"),
                        label(intermediary) if intermediary else "",
                    ),
                    (plain_label("inverse_name_ja"), getattr(link, "inverse_name_ja", "")),
                ]
            )
            head = (
                f"{label(getattr(link, 'source', ''))} → {label(getattr(link, 'target', ''))}"
                f"「{link.name_ja}」"
            )
            lines.append("- " + _with_details(head, details, link.description_ja))
    metrics = [d for d in values if isinstance(d, MetricDefinitionV2)]
    if metrics:
        lines.extend(["", f"## {CONCEPT_LABELS['metric']}"])
        for metric in metrics:
            # semantic layer の書き方（測定値・集計方法・ディメンション・時間のディメンション・
            # 絞り込みの条件）にそろえる（#1729）。
            details = _details(
                [
                    ("測定値", metric.expression_sql),
                    (plain_label("aggregation"), metric.aggregation),
                    (plain_label("grain"), [label(n) for n in metric.grain]),
                    (
                        plain_label("time_property"),
                        label(metric.time_property) if metric.time_property else "",
                    ),
                    (plain_label("time_policy_ja"), metric.time_policy_ja),
                    (plain_label("filter_sql"), metric.filter_sql),
                    (plain_label("distinct_keys"), [label(n) for n in metric.distinct_keys]),
                    (plain_label("unit"), metric.unit),
                    (plain_label("currency"), metric.currency),
                    (plain_label("null_policy_ja"), metric.null_policy_ja),
                    (plain_label("aliases"), aliases(metric)),
                ]
            )
            lines.append("- " + _with_details(metric.name_ja, details, metric.description_ja))
    rules = [d for d in values if d.kind == "business_rule"]
    if rules:
        lines.extend(["", f"## {CONCEPT_LABELS['business_rule']}"])
        for rule in rules:
            details = _details(
                [
                    (plain_label("predicate_sql"), getattr(rule, "predicate_sql", "")),
                    (
                        plain_label("applies_to"),
                        [label(n) for n in getattr(rule, "applies_to", [])],
                    ),
                ]
            )
            lines.append("- " + _with_details(rule.name_ja, details, rule.description_ja))
    others = [d for d in values if d.kind in {"function", "object_set", "business_event"}]
    if others:
        lines.extend(["", "## その他の定義"])
        for d in others:
            details = _details(
                [
                    (plain_label("expression_sql"), getattr(d, "expression_sql", "")),
                    (plain_label("filter_sql"), getattr(d, "filter_sql", "")),
                    (
                        "対象",
                        label(target) if (target := getattr(d, "object_type", "")) else "",
                    ),
                    (plain_label("return_type"), getattr(d, "return_type", "")),
                    (plain_label("aliases"), aliases(d)),
                ]
            )
            lines.append("- " + _with_details(d.name_ja, details, d.description_ja))
    if qa_markdown.strip():
        lines.extend(["", qa_markdown.strip()])
    return "\n".join(lines).rstrip() + "\n" if len(lines) > 1 else ""


def definition_references(d: BusinessDefinition) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    for field in (
        "object_type",
        "property",
        "shared_property",
        "value_type",
        "time_property",
        "timestamp_property",
        "title_property",
        "intermediary_object",
    ):
        if getattr(d, field, ""):
            refs.append((field, getattr(d, field)))
    for field in (
        "dependencies",
        "applies_to",
        "extends",
        "required_links",
        "required_actions",
        "grain",
        "distinct_keys",
        "affected_properties",
        "primary_key",
    ):
        refs.extend((field, value) for value in getattr(d, field, []))
    refs.extend(
        ("properties", value) for value in getattr(d, "properties", []) if isinstance(value, str)
    )
    refs.extend(("implements", value.interface) for value in getattr(d, "implements", []))
    refs.extend(
        ("properties", p.property) for i in getattr(d, "implements", []) for p in i.property_mapping
    )
    refs.extend(("affected_properties", a.property) for a in getattr(d, "assignments", []))
    return list(dict.fromkeys(refs))


def _join_conditions(sql: str, physical: dict[str, OntologyNode]) -> list[JoinCondition]:
    """単純な物理列比較を SQL 接地の既存 join 契約へ投影する。原式は定義に保持。"""
    import sqlglot
    from sqlglot import exp

    if not sql:
        return []
    try:
        expression = sqlglot.parse_one(sql, read="oracle")
    except sqlglot.errors.SqlglotError:
        return []
    comparisons: dict[type[Any], Any] = {
        exp.EQ: "=",
        exp.NEQ: "!=",
        exp.LT: "<",
        exp.LTE: "<=",
        exp.GT: ">",
        exp.GTE: ">=",
    }
    result: list[JoinCondition] = []
    for part in expression.flatten() if isinstance(expression, exp.And) else [expression]:
        if (
            type(part) not in comparisons
            or not isinstance(part.this, exp.Column)
            or not isinstance(part.expression, exp.Column)
        ):
            continue
        pair = [physical.get(sql_column_key(col)) for col in (part.this, part.expression)]
        left, right = pair
        if (
            left is not None
            and right is not None
            and left.kind.value == "column"
            and right.kind.value == "column"
        ):
            result.append(
                JoinCondition(
                    left=left.physical_mappings[0].column_refs[0],
                    right=right.physical_mappings[0].column_refs[0],
                    operator=comparisons[type(part)],
                    ordinal=len(result) + 1,
                )
            )
    return result


def project_graph(
    base: SchemaOntology, definitions: list[BusinessDefinition], revision_id: str | None = None
) -> SchemaOntology:
    from .ontology_graph_semantics import enum_graph_members, reference_semantics

    rid = revision_id or base.revision.id
    nodes = [
        n.model_copy(update={"revision_id": rid}, deep=True)
        for n in base.nodes
        if n.kind.value in PHYSICAL_KINDS
    ]
    physical = physical_node_index(nodes)
    by_name = {d.api_name: d for d in definitions}
    edges = [
        e.model_copy(update={"revision_id": rid}, deep=True)
        for e in base.edges
        if e.source_node_id in {n.id for n in nodes} and e.target_node_id in {n.id for n in nodes}
    ]
    provenance = OntologyProvenance(
        source_kind="manual", source_detail="確認済み Markdown の派生定義"
    )
    old_nodes = {n.id: n for n in base.nodes}
    old_edges = {e.id: e for e in base.edges}
    for d in definitions:
        if d.kind == "link_type":
            if d.source not in by_name or d.target not in by_name:
                continue
            edges.append(
                OntologyEdge(
                    id=d.id,
                    revision_id=rid,
                    kind="link_type",
                    source_node_id=by_name[d.source].id,
                    target_node_id=by_name[d.target].id,
                    relationship_name_ja=d.name_ja,
                    description_ja=d.description_ja,
                    cardinality=d.cardinality,
                    join_conditions=_join_conditions(d.join_expression_sql, physical),
                    provenance=old_edges[d.id].provenance if d.id in old_edges else provenance,
                    review_status="approved",
                    metadata={"definition": d.model_dump(mode="json"), "api_name": d.api_name},
                )
            )
            continue
        mappings = []
        for m in d.mappings:
            obj = physical.get(mapping_object_key(m))
            col = physical.get(f"{mapping_object_key(m)}.{mapping_column_key(m)}")
            mappings.append(
                PhysicalMapping(
                    object_ref=PhysicalObjectRef(
                        # 物理 node の object_ref と同じくカタログ上の名前（`"Mixed_Case"` →
                        # Mixed_Case、`orders` → ORDERS）。画面の SQL 接地が同じ規則で照合する。
                        owner=object_part_name(m.owner),
                        object_name=object_part_name(m.object_name),
                        node_id=obj.id if obj else "",
                        object_type="view" if obj and obj.kind.value == "view" else "table",
                    ),
                    column_refs=(
                        [col.physical_mappings[0].column_refs[0].model_copy(deep=True)]
                        if col
                        else []
                    ),
                    expression_sql=m.expression_sql,
                )
            )
            target = col or obj
            if target:
                edges.append(
                    OntologyEdge(
                        id=stable_ontology_id("mapping", d.id, target.id),
                        revision_id=rid,
                        kind="maps_to",
                        source_node_id=d.id,
                        target_node_id=target.id,
                        relationship_name_ja="物理マッピング",
                        provenance=provenance,
                        review_status="approved",
                    )
                )
        kwargs: dict[str, Any] = {}
        if d.kind == "business_rule":
            targets = [by_name[r].id for r in d.applies_to if r in by_name]
            kwargs["business_rule_definition"] = BusinessRuleDefinition(
                rule_kind="constraint",
                statement_ja=d.description_ja or d.name_ja,
                applies_to_node_ids=targets or [d.id],
            )
        nodes.append(
            OntologyNode(
                id=d.id,
                revision_id=rid,
                kind=d.kind,
                technical_name=d.api_name,
                business_name_ja=d.name_ja,
                description_ja=d.description_ja,
                aliases=d.aliases,
                physical_mappings=mappings,
                provenance=old_nodes[d.id].provenance if d.id in old_nodes else provenance,
                review_status="approved",
                metadata={
                    "definition": d.model_dump(mode="json"),
                    "concept_kind": d.kind,
                    **(
                        {
                            "metric_definition": metric_definition_projection(
                                d, {name: item.id for name, item in by_name.items()}, physical
                            ).model_dump(mode="json")
                        }
                        if d.kind == "metric" and d.expression_sql
                        else {}
                    ),
                },
                **kwargs,
            )
        )
    for d in definitions:
        if d.kind == "link_type":
            continue
        for field, ref in definition_references(d):
            referenced = by_name.get(ref)
            if not referenced:
                continue
            edge_kind, predicate = reference_semantics(d, field)
            # Link Type 自体は辺。参照はその両端へ投影し、元の参照名も残す。
            targets = (
                [by_name[n].id for n in (referenced.source, referenced.target) if n in by_name]
                if referenced.kind == "link_type"
                else [referenced.id]
            )
            for target_id in targets:
                edges.append(
                    OntologyEdge(
                        id=stable_ontology_id("concept_ref", d.id, field, ref, target_id),
                        revision_id=rid,
                        kind=edge_kind,
                        source_node_id=d.id,
                        target_node_id=target_id,
                        relationship_name_ja=FIELD_LABELS.get(field, field),
                        provenance=provenance,
                        review_status="approved",
                        metadata={
                            "reference_field": field,
                            "reference_api_name": ref,
                            "semantic_predicate": predicate,
                            "reference_definition_id": referenced.id,
                            "reference_definition_kind": referenced.kind,
                        },
                    )
                )
    enum_nodes, enum_edges = enum_graph_members(definitions, rid, provenance)
    nodes.extend(enum_nodes)
    edges.extend(enum_edges)
    return SchemaOntology(
        revision=base.revision.model_copy(update={"id": rid}),
        nodes=nodes,
        edges=list({e.id: e for e in edges}.values()),
    )
