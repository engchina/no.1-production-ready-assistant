"""オントロジーの Markdown（`render_concepts` の形）を決定論で定義に戻す（#1729）。

公開前の確認は、Markdown を LLM で解析し直すと、同じ表を指す概念を「未解決」にしたり、行の対応を
落としたりして、毎回結果が変わる（公開が止まる）。下書きは `render_concepts` の決まった形
（`#### 種類` → `##### 表示名 (`API 名`)` → `- 項目: 値`）なので、その形の行はここで読む。
読めない行（自由に書き足した文・複雑な項目）は `parse_markdown_rules` が行番号で返し、呼出元は
その行だけを LLM で解析する（#1735）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from .ontology_definitions import BusinessDefinition, DefinitionEvidence
from .ontology_terms import CONCEPT_LABELS, FIELD_LABELS, VALUE_LABELS, plain_label
from .ontology_unified_model import DEFINITIONS

CONCEPT_SECTION = "オントロジーの概念定義"
# #1729 より前の下書きの項目名（今の下書きを読み直すため。新しく書く Markdown には使わない）。
_PREVIOUS_FIELD_LABELS = {
    "説明": "description_ja",
    "業務上の粒度": "grain_ja",
    "表示名の項目": "title_property",
    "加算特性": "additivity",
    "別名": "aliases",
    "対応する表・列": "mappings",
    "識別名": "api_name",
    "名称": "name_ja",
    "選択可能な値": "allowed_values",
    "逆方向の関係名": "inverse_name_ja",
    "中間オブジェクト": "intermediary_object",
    "主識別子": "primary_key",
    "対象オブジェクト": "object_type",
    "データ型": "data_type",
    "関係の起点": "source",
    "関係の終点": "target",
    "結合条件": "join_expression_sql",
    "定義 SQL": "expression_sql",
    "絞り込み条件": "filter_sql",
    "集計粒度": "grain",
    "重複排除キー": "distinct_keys",
    "パラメーター": "parameters",
    "戻り値": "return_type",
    "業務条件": "predicate_sql",
    "対象プロパティ": "property",
    "必須の関係": "required_links",
    "必須の操作": "required_actions",
    "時間プロパティ": "time_property",
    "発生日時プロパティ": "timestamp_property",
}
_FIELDS: dict[str, str] = {
    **_PREVIOUS_FIELD_LABELS,
    **{plain_label(field): field for field in FIELD_LABELS},
    **{label: field for field, label in FIELD_LABELS.items()},
}
_KINDS = {label: kind for kind, label in CONCEPT_LABELS.items()}
_VALUES = {label: value for value, label in VALUE_LABELS.items()}
# 文字列・文字列の一覧・真偽値・対応する表・列だけを読む（それ以外の複雑な項目は LLM に任せる）。
_COMPLEX_FIELDS = frozenset(
    {"implements", "values", "parameters", "assignments", "evidence", "property_mapping"}
)
_HEADING = re.compile(r"^##### (?P<name>.+?) \(`(?P<api>[^`]+)`\)\s*$")
_ITEM = re.compile(r"^- (?P<label>[^:：]+?)[:：] ?(?P<value>.*)$")
_MAPPING = re.compile(r"^(?P<path>[^（]+?)(?:（式: (?P<expression>.+)）)?$")


def _mapping(text: str) -> dict[str, str] | None:
    match = _MAPPING.fullmatch(text.strip())
    if match is None:
        return None
    parts = [p for p in match.group("path").strip().split(".") if p]
    if len(parts) not in (2, 3) or any(" " in p for p in parts):
        return None
    return {
        "owner": parts[0],
        "object_name": parts[1],
        "column_name": parts[2] if len(parts) == 3 else "",
        "expression_sql": match.group("expression") or "",
    }


def _value(field: str, text: str, defaults: dict[str, Any]) -> Any:
    """`- 項目: 値` の値を型に合わせて戻す。読めなければ `ValueError`。"""

    default = defaults.get(field)
    if field in _COMPLEX_FIELDS:
        raise ValueError(field)
    if field == "mappings":
        mappings = [_mapping(part) for part in text.split(" / ")]
        if any(m is None for m in mappings):
            raise ValueError(field)
        return mappings
    if isinstance(default, bool):
        if text not in {"はい", "いいえ"}:
            raise ValueError(field)
        return text == "はい"
    if isinstance(default, list):
        return [part.strip() for part in text.split(" / ") if part.strip()]
    return _VALUES.get(text, text) if field in {"data_type", "cardinality"} else text


@dataclass
class RuleParse:
    """決まった形で読めた定義・行と、読めなかった行（自由な行）の行番号（#1735）。"""

    definitions: list[BusinessDefinition] = field(default_factory=list)
    lines: list[dict[str, Any]] = field(default_factory=list)
    free_lines: list[int] = field(default_factory=list)

    def extraction(self) -> dict[str, Any]:
        """`MarkdownExtraction` と同じ形の dict（自由な行は含まない）。"""

        return {
            "definitions": list(self.definitions),
            "coverage": [],
            "lines": list(self.lines),
            "issues_ja": [],
        }


def parse_markdown_rules(markdown: str) -> RuleParse:
    """`render_concepts` の形の部分を決定論で読み、読めなかった行を `free_lines` に返す（#1735）。

    決まった形の部分は常にここで読む（自由な行が混じっても、下書き全体を LLM に渡さない）。
    読めない項目の行は、その行だけを自由な行にして定義の残りの項目は読む。定義として成り立たない
    見出しの塊（必須の項目が無いなど）は、塊の全部の行を自由な行にする。
    """

    result = RuleParse()
    section, kind = "", ""
    current: dict[str, Any] | None = None
    block: list[int] = []

    def flush(end: int) -> None:
        nonlocal current, block
        if current is None:
            return
        start = current.pop("_start")
        current["evidence"] = [
            DefinitionEvidence(
                source_id="markdown",
                locator=f"line:{start}",
                excerpt_ja=current.pop("_heading"),
                source_kind="manual",
            ).model_dump(mode="json")
        ]
        try:
            definition = DEFINITIONS.validate_python([current])[0]
        except ValidationError:
            result.free_lines.extend(block)
        else:
            result.definitions.append(definition)
            result.lines.append(
                dict(
                    start_line=start,
                    end_line=end,
                    disposition="definition",
                    definition_api_names=[definition.api_name],
                )
            )
        current, block = None, []

    def context(number: int, reason: str) -> None:
        result.lines.append(
            dict(start_line=number, end_line=number, disposition="context", reason_ja=reason)
        )

    last = 0
    for number, raw in enumerate(markdown.splitlines(), 1):
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("## "):
            flush(last)
            section, kind = line[3:].strip(), ""
            context(number, "節の見出し")
        elif section.startswith("Q/A ") and section != CONCEPT_SECTION:
            # Q/A 由来の規則・構造パターンなど、概念の定義の外の節は SQL の生成の文脈として残す。
            context(number, "概念の定義の外の節（SQL の生成の文脈）")
        elif section != CONCEPT_SECTION:
            result.free_lines.append(number)
        elif line.startswith("#### "):
            flush(last)
            kind = _KINDS.get(line[5:].strip(), "")
            if kind:
                context(number, "種類の見出し")
            else:
                result.free_lines.append(number)
        elif match := _HEADING.match(line):
            flush(last)
            if not kind:
                result.free_lines.append(number)
                continue
            current = {
                "kind": kind,
                "name_ja": match.group("name").strip(),
                "api_name": match.group("api").strip(),
                "origin": "manual",
                "_start": number,
                "_heading": line,
            }
            block = [number]
        elif (match := _ITEM.match(line)) and current is not None:
            block.append(number)
            field_name = _FIELDS.get(match.group("label").strip())
            defaults = _defaults(kind)
            try:
                if field_name is None or field_name in {"kind", "api_name"}:
                    raise ValueError(match.group("label"))
                if field_name not in defaults:
                    raise ValueError(field_name)
                current[field_name] = _value(field_name, match.group("value").strip(), defaults)
            except ValueError:
                # 読めない項目の行だけを自由な行にする（定義の残りの項目は読む）。
                result.free_lines.append(number)
                block.remove(number)
                continue
        else:
            flush(last)
            result.free_lines.append(number)
            continue
        last = number
    flush(last)
    result.free_lines = sorted(set(result.free_lines))
    return result


def parse_rendered_markdown(markdown: str) -> dict[str, Any] | None:
    """`render_concepts` の形の Markdown を、`MarkdownExtraction` と同じ形の dict にする。

    1 行でも読めなければ `None`。
    """

    parsed = parse_markdown_rules(markdown)
    if parsed.free_lines or not parsed.definitions:
        return None
    return parsed.extraction()


_DEFAULTS: dict[str, dict[str, Any]] = {}


def _defaults(kind: str) -> dict[str, Any]:
    """種類のモデルの項目と既定値（項目が種類に無ければ読めない行とする）。"""

    if kind not in _DEFAULTS:
        from .ontology_definitions import (
            ActionTypeDefinition,
            BusinessEventDefinition,
            BusinessRuleDefinitionV2,
            EnumerationDefinition,
            FunctionDefinition,
            InterfaceDefinition,
            LinkTypeDefinition,
            MetricDefinitionV2,
            ObjectSetDefinition,
            ObjectTypeDefinition,
            PropertyDefinition,
        )

        models: dict[str, Any] = {
            "object_type": ObjectTypeDefinition,
            "property": PropertyDefinition,
            "shared_property": PropertyDefinition,
            "value_type": PropertyDefinition,
            "link_type": LinkTypeDefinition,
            "function": FunctionDefinition,
            "action_type": ActionTypeDefinition,
            "interface": InterfaceDefinition,
            "metric": MetricDefinitionV2,
            "business_rule": BusinessRuleDefinitionV2,
            "enumeration": EnumerationDefinition,
            "business_event": BusinessEventDefinition,
            "object_set": ObjectSetDefinition,
        }
        _DEFAULTS[kind] = {
            name: info.get_default(call_default_factory=True)
            for name, info in models[kind].model_fields.items()
            if not info.is_required() or name in {"source", "target", "object_type", "property"}
        }
        for name, info in models[kind].model_fields.items():
            if info.is_required() and name not in _DEFAULTS[kind]:
                _DEFAULTS[kind][name] = ""
    return _DEFAULTS[kind]
