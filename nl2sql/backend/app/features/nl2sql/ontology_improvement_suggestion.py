"""公開前の確認の「業務説明に追記する改善案」（#1729）。

残ったエラー・警告・補うと精度が上がる項目から、利用者が「業務説明（自然言語）」に追記して
「AI 構築を実行」をやり直すための、業務の言葉の日本語の文を作る。LLM は使わず、診断と物理の
schema（主キーの制約・列の型）と定義（対応する表・リンク型の結合のキー）だけから決定論で組み立てる
（速く、毎回同じ）。分からない値は推測で埋めず「（○○を確認して書いてください）」と空けておく。
既に業務説明に書かれている文は、画面が重ねて出さない（文ごとに返す）。
"""

from __future__ import annotations

import re
from collections.abc import Collection
from typing import Any

from .object_identity import object_match_key
from .ontology_definition_validation import mapping_column_key, mapping_object_key
from .ontology_definitions import (
    BusinessDefinition,
    LinkTypeDefinition,
    ObjectTypeDefinition,
    PropertyDefinition,
)
from .ontology_link_keys import link_join_check, schema_tables, suggest_link_join
from .ontology_terms import VALUE_LABELS, plain_label
from .ontology_unified_model import base_data_type, join_key

_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
# オブジェクト型の文で補える診断（対応する表・主キー・粒度・所属の誤り・関係の多重度）。
_OBJECT_CODES = frozenset(
    {
        "OBJECT_MAPPING_REQUIRED",
        "MAPPING_OUTSIDE_PROFILE",
        "OBJECT_EXCLUDED_WITHOUT_TABLE",
        "OBJECT_IDENTITY_REQUIRED",
        "OBJECT_GRAIN_REQUIRED",
        "PROPERTY_OWNER_MISMATCH",
    }
)
_TYPE_FIELDS = frozenset({"data_type", "physical_type"})


def _ask(what: str) -> str:
    return f"（{what}を確認して書いてください）"


def _japanese(text: str) -> bool:
    return bool(re.search(r"[぀-ヿ一-鿿]", text))


def improvement_suggestions(
    definitions: list[BusinessDefinition],
    findings: list[dict[str, Any]],
    schema: dict[str, Any] | None,
    *,
    excluded: list[BusinessDefinition] | None = None,
) -> list[dict[str, str]]:
    """改善案の文の一覧（`severity` と `text`）。公開を止めるエラーを解く文を先に並べる。"""

    values = [*definitions, *(excluded or [])]
    by_key: dict[str, BusinessDefinition] = {}
    for d in values:
        for key in (d.id, d.api_name):
            if key:
                by_key.setdefault(key, d)
    by_name = {d.api_name: d for d in values}
    tables = schema_tables(schema)
    sentences: dict[str, str] = {}

    def add(severity: str, text: str) -> None:
        current = sentences.get(text)
        if current is None or _SEVERITY_ORDER[severity] < _SEVERITY_ORDER[current]:
            sentences[text] = severity

    def label(name: str) -> str:
        target = by_name.get(name)
        return target.name_ja if target is not None else name

    def link_sentence(link: LinkTypeDefinition, cardinality_choices: str = "") -> str:
        # リンク型の結合のキーと多重度の文。一意でない列でつなぐものは正しい結合のキー（#1760）に
        # し、付け替え先が無ければ結合のキーを空けておく（推測で埋めない）。多重度の競合（#1765）は
        # 多重度を「どちらか」で空けておく。`apply_business_text_links` と
        # `business_text_cardinalities` が読む。
        # キーでつなぐ・判断できない（式）結合のキーはそのまま使う。一意でない列だけ付け替える。
        keyed = link_join_check(link, by_name, schema, tables).keyed is not False
        fix = None if keyed else suggest_link_join(link, by_name, schema, tables)
        head = (
            f"{label(link.source)}（{link.source}）と{label(link.target)}（{link.target}）の"
            f"リンク型『{link.name_ja}』（{link.api_name}）は、"
        )
        if not keyed and fix is None:
            return (
                head
                + _ask("主キー・一意キー・外部キーで一意につながる結合のキーの列")
                + "でつなぐ。"
            )
        join = link.join_expression_sql if keyed else fix.join_expression_sql if fix else ""
        cardinality = link.cardinality if keyed else fix.cardinality if fix else ""
        how = (
            f"{cardinality_choices} かを確認して書いてください"
            if cardinality_choices
            else VALUE_LABELS.get(cardinality, "")
        )
        return f"{head}{join} でつなぐ" + (f"（{how}）" if how else "") + "。"

    def object_sentence(obj: ObjectTypeDefinition) -> str:
        names = [obj.name_ja, *[a for a in obj.aliases if _japanese(a)][:2]]
        head = "・".join(dict.fromkeys(names)) + f"（{obj.api_name}）"
        keys = sorted({mapping_object_key(m) for m in obj.mappings if m.object_name})
        if len(keys) == 1 and keys[0] in tables:
            table = tables[keys[0]]
            where = f"{table['qualified']} の{'ビュー' if table['kind'] == 'view' else '表'}"
        elif keys:
            where = " / ".join(keys) + " の表・ビュー"
        else:
            where = "（対応する表・ビューの名前を確認して書いてください）の表"
            table = None
        primary = [
            column
            for name in obj.primary_key
            if isinstance(prop := by_name.get(name), PropertyDefinition)
            for column in [m.column_name for m in prop.mappings if m.column_name][:1]
        ]
        if not primary and len(keys) == 1 and keys[0] in tables:
            table = tables[keys[0]]
            primary = [table["columns"][c]["column"] for c in table["primary_key"]]
        grain = grain_choices.get(obj.api_name) or obj.grain_ja.strip().rstrip("。")
        if not grain:
            grain = f"1 行が 1 つの{obj.name_ja}" if primary else _ask("1 行が何を表すか")
        key_text = "・".join(primary) if primary else _ask("1 行を特定する列")

        def spaced(text: str) -> str:
            # 物理の名前の前だけ空白を置く（「（…確認して書いてください）」の前には置かない）。
            return text if text.startswith("（") else f" {text}"

        parts = [f"{head}は{spaced(where)}で、{grain}、主キーは{spaced(key_text)}"]
        for link in values:
            if link.kind != "link_type" or getattr(link, "source", "") != obj.api_name:
                continue
            join = getattr(link, "join_expression_sql", "")
            left = join.split("=", 1)[0].strip() if "=" in join and " AND " not in join else ""
            column = object_match_key(left).rsplit(".", 1)[-1] if left else ""
            cardinality = VALUE_LABELS.get(getattr(link, "cardinality", ""), "")
            how = (
                f"（{cardinality}）"
                if cardinality and getattr(link, "cardinality", "unknown") != "unknown"
                else _ask("多重度（1 対多など）")
            )
            target = label(getattr(link, "target", ""))
            via = f"{column} で" if column else _ask("結合のキーの列") + "で"
            parts.append(f"{via}{target}に属する{how}")
        return "、".join(parts) + "。"

    objects_by_severity: dict[str, str] = {}
    # キーの違う粒度の競合（#1759）: オブジェクト型の文の粒度を「どちらか」で空けておく。
    grain_choices: dict[str, str] = {}
    for finding in findings:
        severity = str(finding.get("severity", ""))
        if severity not in _SEVERITY_ORDER:
            continue
        code = str(finding.get("code", ""))
        target = by_key.get(str(finding.get("definition_id", "")))
        if code in _OBJECT_CODES or code == "CARDINALITY_REQUIRED":
            obj = target
            if code == "CARDINALITY_REQUIRED" and target is not None:
                obj = by_name.get(getattr(target, "source", ""))
            if isinstance(target, PropertyDefinition) and code == "PROPERTY_OWNER_MISMATCH":
                obj = by_name.get(target.object_type)
            if isinstance(obj, ObjectTypeDefinition):
                previous = objects_by_severity.get(obj.api_name)
                if previous is None or _SEVERITY_ORDER[severity] < _SEVERITY_ORDER[previous]:
                    objects_by_severity[obj.api_name] = severity
        elif code == "LINK_JOIN_NOT_UNIQUE" and isinstance(target, LinkTypeDefinition):
            add(severity, link_sentence(target))
        elif code == "DEFINITION_CONFLICT":
            detail = finding.get("detail") or {}
            field = str(detail.get("field", ""))
            conflicted = by_name.get(str(detail.get("api_name", ""))) or target
            if conflicted is None or not field:
                continue
            if field == "cardinality" and isinstance(conflicted, LinkTypeDefinition):
                # 多重度はリンク型の文（結合のキーと一緒）で、どちらかを空けて書く（#1765）。
                choices = " か ".join(str(v) for v in detail.get("values", []) if str(v))
                add(severity, link_sentence(conflicted, choices or "多重度"))
                continue
            if field == "grain_ja" and isinstance(conflicted, ObjectTypeDefinition):
                # 粒度はオブジェクト型の文（主キーと一緒）で書く。追記して直すと次の構築に効く。
                choices = " と ".join(str(v) for v in detail.get("values", []) if str(v))
                grain_choices[conflicted.api_name] = _ask(
                    f"{choices} のどちらか" if choices else "1 行が何を表すか"
                )
                previous = objects_by_severity.get(conflicted.api_name)
                if previous is None or _SEVERITY_ORDER[severity] < _SEVERITY_ORDER[previous]:
                    objects_by_severity[conflicted.api_name] = severity
                continue
            owner = label(getattr(conflicted, "object_type", "") or "")
            subject = f"{owner}の{conflicted.name_ja}" if owner else conflicted.name_ja
            columns = [m for m in conflicted.mappings if m.column_name and not m.expression_sql]
            info = (
                tables.get(mapping_object_key(columns[0]), {})
                .get("columns", {})
                .get(mapping_column_key(columns[0]))
                if field in _TYPE_FIELDS and len(columns) == 1
                else None
            )
            if info and info.get("data_type"):
                physical = str(info["data_type"])
                base = VALUE_LABELS.get(base_data_type(physical), "")
                add(
                    severity,
                    f"{subject}は {columns[0].owner}.{columns[0].object_name}."
                    f"{columns[0].column_name} の列で、型は {physical}"
                    + (f"（{base}）" if base else "")
                    + "。",
                )
            elif field == "required":
                add(severity, f"{subject}は{_ask('必須（値が必ずある項目）かどうか')}。")
            else:
                choices = " と ".join(str(v) for v in detail.get("values", []) if str(v))
                what = f"{choices} のどちらか" if choices else plain_label(field)
                add(severity, f"{subject}の{plain_label(field)}は{_ask(what)}。")
        elif code == "METRIC_SEMANTICS_REQUIRED" and target is not None:
            field = str(finding.get("field", ""))
            add(
                severity,
                f"指標「{target.name_ja}」の{plain_label(field)}は{_ask(plain_label(field))}。",
            )
    for name, severity in objects_by_severity.items():
        obj = by_name[name]
        if isinstance(obj, ObjectTypeDefinition):
            add(severity, object_sentence(obj))
    return [
        {"severity": severity, "text": text}
        for text, severity in sorted(sentences.items(), key=lambda item: _SEVERITY_ORDER[item[1]])
    ]


_OBJECT_SENTENCE = re.compile(
    r"（(?P<api>[A-Za-z][A-Za-z0-9_.]*)）は (?P<table>\S+) の(?:表|ビュー)で、"
    r"(?P<grain>[^、。]+)、主キーは ?(?P<key>[^、。]+)"
)


def business_text_grain_objects(business_text: str) -> set[str]:
    """業務説明のオブジェクト型の文で、粒度が書かれた（空けていない）オブジェクト型の API 名。"""

    return {
        match.group("api")
        for match in _OBJECT_SENTENCE.finditer(business_text)
        if "確認して書いてください" not in match.group("grain")
    }


_LINK_SENTENCE = re.compile(
    r"（(?P<source>[A-Za-z][A-Za-z0-9_.]*)）と[^（。]*（(?P<target>[A-Za-z][A-Za-z0-9_.]*)）の"
    r"リンク型『[^』]*』（(?P<api>[A-Za-z][A-Za-z0-9_.]*)）は、(?P<join>[^。]+?) でつなぐ"
    r"(?:（(?P<cardinality>.+?)）)?。"
)
_CARDINALITY_BY_LABEL = {
    label: value
    for value, label in VALUE_LABELS.items()
    if value in {"one_to_one", "one_to_many", "many_to_one", "many_to_many"}
}


def apply_business_text_facts(
    definitions: list[BusinessDefinition],
    business_text: str,
    *,
    previous: Collection[str] | None = None,
) -> list[str]:
    """業務説明に追記された改善案の文（`object_sentence` の形）から、粒度と主キーを定義に入れる。

    AI が業務説明を読み落としても、追記した改善案が次の構築に必ず効くようにする（#1729）。
    「（…を確認して書いてください）」のまま残った所は使わない（推測で埋めない）。`previous`
    （前の定義の API 名）を渡さないときは、値の入った定義を変えない。渡したときは、前の定義に無い
    もの（AI の出力）の粒度・主キーを業務説明の値にする（粒度の正は 前の定義 → 業務説明 → DB の
    主キー。#1759）。前の定義は利用者の定義のため変えない（値が空のときだけ入れる）。入れた定義の
    API 名を返す。
    """

    by_name = {d.api_name: d for d in definitions}
    applied: list[str] = []
    for match in _OBJECT_SENTENCE.finditer(business_text):
        obj = by_name.get(match.group("api"))
        if not isinstance(obj, ObjectTypeDefinition):
            continue
        replace = previous is not None and obj.api_name not in previous
        grain = match.group("grain").strip()
        if (
            (not obj.grain_ja or replace)
            and obj.grain_ja != grain
            and "確認して書いてください" not in grain
        ):
            obj.grain_ja = grain
            applied.append(obj.api_name)
        key = match.group("key").strip()
        if (not obj.primary_key or replace) and "確認して書いてください" not in key:
            columns = {object_match_key(c.strip()) for c in key.split("・") if c.strip()}
            props = [
                d.api_name
                for d in definitions
                if isinstance(d, PropertyDefinition)
                and d.kind == "property"
                and d.object_type == obj.api_name
                and len(d.mappings) == 1
                and d.mappings[0].column_name
                and mapping_column_key(d.mappings[0]) in columns
            ]
            if len(props) == len(columns) and obj.primary_key != props:
                obj.primary_key = props
                applied.append(obj.api_name)
    return list(dict.fromkeys(applied))


def _cardinality_from_text(text: str) -> str:
    """業務説明の多重度の書き方（「多対 1（many-to-one）」「多対 1」「many-to-one」）を値にする。"""

    compact = "".join(str(text or "").split()).casefold()
    if not compact or "確認して書いてください" in compact:
        return ""
    for label, value in _CARDINALITY_BY_LABEL.items():
        japanese, _, english = label.partition("（")
        candidates = {
            "".join(label.split()).casefold(),
            "".join(value.split()).casefold(),
            "".join(japanese.split()).casefold(),
            "".join(english.rstrip("）").split()).casefold(),
        }
        if compact in candidates:
            return value
    return ""


_OBJECT_LINK_CLAUSE = re.compile(
    r"、(?P<column>[A-Za-z][A-Za-z0-9_$#]*) で(?P<target>[^、。（]+)に属する"
    r"（(?P<cardinality>.+?)）(?=[、。])"
)


def business_text_cardinalities(
    definitions: list[BusinessDefinition], business_text: str
) -> dict[str, str]:
    """業務説明の改善案の文から、リンク型ごとの多重度を読む（API 名 → 多重度。#1765）。

    読む文は 2 つ: リンク型の文（`link_sentence`。リンク型の API 名、または両端と結合のキーで
    リンク型を決める）と、オブジェクト型の文の「COLUMN で〈相手〉に属する（多重度）」（起点の
    オブジェクト型・起点の列・相手の名前で決める）。「確認して書いてください」のまま空けた所と、
    多重度の書き方として読めないものは使わない。
    """

    links = [d for d in definitions if isinstance(d, LinkTypeDefinition)]
    by_name = {d.api_name: d for d in definitions}
    result: dict[str, str] = {}

    def names_of(name: str) -> set[str]:
        target = by_name.get(name)
        return {name, *([target.name_ja, *target.aliases] if target is not None else [])}

    for match in _LINK_SENTENCE.finditer(business_text):
        value = _cardinality_from_text(match.group("cardinality") or "")
        if not value:
            continue
        ends = {match.group("source"), match.group("target")}
        join = join_key(match.group("join"))
        for link in links:
            if link.api_name == match.group("api") or (
                {link.source, link.target} == ends and join_key(link.join_expression_sql) == join
            ):
                result[link.api_name] = value
    for match in _OBJECT_SENTENCE.finditer(business_text):
        source = match.group("api")
        rest = business_text[match.end() : business_text.find("。", match.end()) + 1]
        for clause in _OBJECT_LINK_CLAUSE.finditer(rest):
            value = _cardinality_from_text(clause.group("cardinality"))
            if not value:
                continue
            column = object_match_key(clause.group("column"))
            target = clause.group("target").strip()
            for link in links:
                left = (
                    link.join_expression_sql.split("=", 1)[0]
                    if "=" in link.join_expression_sql
                    else ""
                )
                if (
                    link.source == source
                    and " AND " not in link.join_expression_sql.upper()
                    and object_match_key(left.strip()).rsplit(".", 1)[-1] == column
                    and target in names_of(link.target)
                ):
                    result.setdefault(link.api_name, value)
    return result


def apply_business_text_links(
    definitions: list[BusinessDefinition], business_text: str, schema: dict[str, Any] | None
) -> list[str]:
    """業務説明に追記されたリンク型の改善案の文（`link_sentence` の形）で、結合のキーを直す。

    一意でない列でつなぐリンク型（`LINK_JOIN_NOT_UNIQUE`。前の定義・利用者のものも）だけを、文の
    結合のキーがキーでつなぐと確かめられるときに、その結合のキー・多重度にする。改善案は利用者が
    「コピー」か「業務説明に追記」で使ったときだけ業務説明に入るため、確認なしには変わらない
    （#1727・#1760）。キーでつなぐリンク型・空けた所（「確認して書いてください」）は変えない。直した
    リンク型の API 名を返す。
    """

    by_name = {d.api_name: d for d in definitions}
    tables = schema_tables(schema)
    applied: list[str] = []
    for match in _LINK_SENTENCE.finditer(business_text):
        join = match.group("join").strip()
        if "確認して書いてください" in join:
            continue
        ends = {match.group("source"), match.group("target")}
        link = by_name.get(match.group("api"))
        candidates = [
            d
            for d in ([link] if isinstance(link, LinkTypeDefinition) else definitions)
            if isinstance(d, LinkTypeDefinition) and {d.source, d.target} == ends
        ]
        for item in candidates:
            if link_join_check(item, by_name, schema, tables).keyed is not False:
                continue
            fixed = item.model_copy(update={"join_expression_sql": join})
            if link_join_check(fixed, by_name, schema, tables).keyed is not True:
                continue
            item.join_expression_sql = join
            cardinality = _cardinality_from_text(match.group("cardinality") or "")
            if cardinality:
                item.cardinality = cardinality  # type: ignore[assignment]
            applied.append(item.api_name)
    return list(dict.fromkeys(applied))
