"""業務 Agent の出力の項目と、その値を記録するツール（#2549）。

業務 Agent に出力の項目（key・表示名・型・必須）を定義すると、その Run にだけ
`record_output` を渡す。引数の JSON schema は業務 Agent の項目から作り、値の検証はここ
（コード）で行う。誤り（必須の欠け・型・日付の形）は項目ごとにモデルへ返して直させ、正しく
記録できた行を成果物（`structured_output`）に残す。成果物の形は画面の表
（`toTabularData` の `{columns, rows}`）と同じ。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from datetime import date
from typing import Any, Literal

from pr_backend_core.markdown import normalize_heading_markup
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.features.agent.answer_identifiers import identifier_index
from app.features.agent.tools import (
    ExternalToolError,
    ToolDefinition,
    ToolHandler,
    ToolInvocationContext,
)

RECORD_OUTPUT_TOOL = "record_output"
OUTPUT_KIND = "structured_output"
OUTPUT_NAME = "出力の項目"
# 1 つの業務 Agent に定義できる項目の数、1 回の記録の行の数、値の文字数の上限。
MAX_OUTPUT_FIELDS = 30
MAX_OUTPUT_ROWS = 200
MAX_VALUE_CHARS = 2000
OUTPUT_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

OutputFieldType = Literal["text", "number", "date", "boolean"]
# 品質評価での比べ方（#2562）。exact は型ごとの一致、meaning は意味が同じか（文字の項目だけ）。
OutputFieldMatch = Literal["exact", "meaning"]

_JSON_TYPES: dict[str, str] = {
    "text": "string",
    "number": "number",
    "date": "string",
    "boolean": "boolean",
}
_TYPE_LABELS: dict[str, str] = {
    "text": "文字",
    "number": "数値",
    "date": "日付（YYYY-MM-DD）",
    "boolean": "真偽",
}


class AgentOutputField(BaseModel):
    """出力の項目の 1 つ。"""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=40)
    type: OutputFieldType = "text"
    required: bool = False
    description: str = Field(default="", max_length=200)
    match: OutputFieldMatch = "exact"

    @field_validator("key")
    @classmethod
    def _key(cls, value: str) -> str:
        key = value.strip()
        if not OUTPUT_KEY_PATTERN.fullmatch(key):
            raise ValueError(
                "項目の key は英小文字で始め、英小文字・数字・_ の 40 文字以内にしてください。"
            )
        return key

    @field_validator("label", "description")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _match_only_for_text(self) -> AgentOutputField:
        # 意味で比べるのは文字の項目だけ（数値・日付・真偽は型ごとの一致で比べる）。
        if self.type != "text":
            self.match = "exact"
        return self


def validate_output_fields(fields: Sequence[AgentOutputField]) -> list[AgentOutputField]:
    """項目の一覧の検証（数の上限・key の重複）。違反は ValueError（画面に出す文）。"""
    if len(fields) > MAX_OUTPUT_FIELDS:
        raise ValueError(f"出力の項目は {MAX_OUTPUT_FIELDS} 項目までです。")
    keys = [field.key for field in fields]
    duplicated = sorted({key for key in keys if keys.count(key) > 1})
    if duplicated:
        raise ValueError(f"出力の項目の key が重なっています: {'、'.join(duplicated)}")
    return list(fields)


def output_input_schema(fields: Sequence[AgentOutputField]) -> dict[str, Any]:
    """`record_output` の引数の JSON schema（業務 Agent の項目から作る）。"""
    properties: dict[str, Any] = {}
    for field in fields:
        description = f"{field.label}（{_TYPE_LABELS[field.type]}）"
        if field.description:
            description += f"。{field.description}"
        if not field.required:
            description += "。分からなければ null"
        properties[field.key] = {
            "type": _JSON_TYPES[field.type]
            if field.required
            else [_JSON_TYPES[field.type], "null"],
            "description": description,
        }
    return {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "description": f"記録する行（1〜{MAX_OUTPUT_ROWS} 行。明細が複数なら 1 件 1 行）",
                "items": {
                    "type": "object",
                    "properties": properties,
                    "required": [field.key for field in fields if field.required],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["rows"],
        "additionalProperties": False,
    }


def _value_error(field: AgentOutputField, value: Any) -> str | None:
    """値の型の誤り（無ければ None）。"""
    if field.type == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            return "数値で書いてください。"
    elif field.type == "boolean":
        if not isinstance(value, bool):
            return "true か false で書いてください。"
    elif not isinstance(value, str):
        return "文字列で書いてください。"
    elif len(value) > MAX_VALUE_CHARS:
        return f"{MAX_VALUE_CHARS} 文字以内にしてください。"
    elif field.type == "date":
        try:
            date.fromisoformat(value)
        except ValueError:
            return "日付は YYYY-MM-DD の形で書いてください。"
    return None


def validate_rows(
    fields: Sequence[AgentOutputField], arguments: dict[str, Any]
) -> list[dict[str, Any]]:
    """記録する行を検証して返す。誤りは項目ごとに `ExternalToolError` でモデルへ返す。"""
    rows = arguments.get("rows")
    errors: list[dict[str, Any]] = []
    if not isinstance(rows, list) or not rows:
        errors.append({"loc": ["rows"], "message": "記録する行を 1 行以上渡してください。"})
    elif len(rows) > MAX_OUTPUT_ROWS:
        errors.append({"loc": ["rows"], "message": f"行は {MAX_OUTPUT_ROWS} 行までです。"})
    known = {field.key for field in fields}
    cleaned: list[dict[str, Any]] = []
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            errors.append(
                {"loc": ["rows", index], "message": "行は項目の key と値の object です。"}
            )
            continue
        for key in sorted(set(row) - known):
            errors.append({"loc": ["rows", index, key], "message": "出力の項目に無い key です。"})
        values: dict[str, Any] = {}
        for field in fields:
            value = row.get(field.key)
            if value is None or (isinstance(value, str) and not value.strip()):
                if field.required:
                    errors.append(
                        {"loc": ["rows", index, field.key], "message": "必須の項目です。"}
                    )
                values[field.key] = None
                continue
            problem = _value_error(field, value)
            if problem is not None:
                errors.append({"loc": ["rows", index, field.key], "message": problem})
            values[field.key] = value.strip() if isinstance(value, str) else value
        cleaned.append(values)
    if errors:
        raise ExternalToolError(
            "output.invalid",
            "出力の項目の値に誤りがあります。argument_errors の項目を直して、"
            "すべての行をもう一度記録してください。",
            {"details": {"errors": errors}},
        )
    return cleaned


def output_content(
    fields: Sequence[AgentOutputField], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """成果物の内容（画面の表の形）。`match` は品質評価の比べ方（#2562。画面の表は使わない）。"""
    return {
        "columns": [
            {"name": field.key, "label": field.label, "type": field.type, "match": field.match}
            for field in fields
        ],
        "rows": rows,
    }


def record_output_tool(
    run_id: str, fields: Sequence[AgentOutputField]
) -> tuple[ToolDefinition, ToolHandler]:
    """Run に渡す `record_output`（その業務 Agent の項目の schema と、検証して残す handler）。"""
    # 保存先の形に依らず、dict の項目もモデルとして読む（#2559）。
    columns = [AgentOutputField.model_validate(field) for field in fields]

    def handler(arguments: dict[str, Any], _: ToolInvocationContext) -> dict[str, Any]:
        from app.features.agent.runtime import runtime_repository

        rows = validate_rows(columns, arguments)
        index = identifier_index(runtime_repository.get_run(run_id).steps)
        corrected = 0
        for row in rows:
            for key, value in row.items():
                if not isinstance(value, str):
                    continue
                checked = index.scalar(value)
                if checked.ambiguous:
                    raise ExternalToolError(
                        "output.identifier_ambiguous",
                        "識別子を取得したデータと同じ表記で記録してください。",
                    )
                row[key] = checked.text
                corrected += checked.corrected
        content = output_content(columns, rows)
        if corrected:
            content["identifier_check"] = {"corrected": corrected, "ambiguous": 0}
        runtime_repository.save_builtin_artifact(
            run_id, kind=OUTPUT_KIND, name=OUTPUT_NAME, content=content
        )
        # 表の形で返すと、画面がツールの結果の表としても出すので、件数だけを返す。
        return {"recorded": len(rows)}

    return (
        ToolDefinition(
            name=RECORD_OUTPUT_TOOL,
            title="出力の項目の記録",
            description=(
                "この業務 Agent の出力の項目の値を記録する。回答を書く前に、確かめた値で呼ぶ。"
                "呼び直すと前の記録を置き換えるので、すべての行をまとめて渡す。"
                "記録した値は画面の表に表示される。回答本文に記録の報告・同じ値の一覧・このツール名を書かない。"
            ),
            input_schema=output_input_schema(columns),
            output_schema={"type": "object"},
            side_effects=False,
            audit_tags=["output"],
        ),
        handler,
    )


OUTPUT_RETRY_MESSAGE = (
    f"この業務 Agent には出力の項目があります。{RECORD_OUTPUT_TOOL} で項目の値を記録してから、"
    "回答をもう一度書いてください。確かめられなかった値は、必須でなければ null にしてください。"
)


def output_retried(metadata: object) -> bool:
    """この Run で、出力の項目の記録を差し戻したか（1 回だけ）。"""
    from app.features.agent.tool_grounding import METADATA_KEY

    record = metadata.get(METADATA_KEY) if isinstance(metadata, dict) else None
    return isinstance(record, dict) and bool(record.get("output_retry"))


def output_recorded(artifacts: Sequence[Any]) -> bool:
    """Run が出力の項目を記録したか。"""
    return any(getattr(item, "kind", None) == OUTPUT_KIND for item in artifacts)


# ---- 品質評価の出力の項目の判定（#2558） ----------------------------------------------------

# 評価ケースに書ける期待する出力の行の数。
EVALUATION_EXPECTED_OUTPUT_ROWS_MAX = 50
OutputValue = str | int | float | bool | None
_TRUE_TEXTS = frozenset({"true", "はい", "yes", "○", "◯", "1"})
_FALSE_TEXTS = frozenset({"false", "いいえ", "no", "×", "0"})


class OutputFieldResult(BaseModel):
    """期待する出力の 1 つの項目の判定（期待の行・項目・期待・記録・正誤・比べ方）。

    `reason` は意味の判定の理由（#2568。意味で判定した項目だけ）、`note` は比べ方どおりに
    判定できなかった理由（意味の判定の失敗など。無ければ空）。
    """

    row: int
    field: str
    label: str = ""
    expected: OutputValue = None
    actual: OutputValue = None
    correct: bool
    method: OutputFieldMatch = "exact"
    reason: str = ""
    note: str = ""


def validate_expected_output(rows: list[dict[str, Any]]) -> list[dict[str, OutputValue]]:
    """評価ケースの期待する出力の検証（行の数・key の形・値は文字 / 数値 / 真偽 / null）。"""
    if len(rows) > EVALUATION_EXPECTED_OUTPUT_ROWS_MAX:
        raise ValueError(f"期待する出力は {EVALUATION_EXPECTED_OUTPUT_ROWS_MAX} 行までです。")
    cleaned: list[dict[str, OutputValue]] = []
    for row in rows:
        values: dict[str, OutputValue] = {}
        for key, value in row.items():
            if not isinstance(key, str) or not OUTPUT_KEY_PATTERN.fullmatch(key):
                raise ValueError(
                    "期待する出力の key は、出力の項目の key（英小文字で始まる）にしてください。"
                )
            if isinstance(value, str):
                value = value.strip()[:MAX_VALUE_CHARS]
            elif value is not None and not isinstance(value, bool | int | float):
                raise ValueError("期待する出力の値は、文字・数値・真偽・null にしてください。")
            values[key] = value
        if values:
            cleaned.append(values)
    return cleaned


def _text(value: OutputValue) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value)).casefold().split())


def _as_number(value: OutputValue) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        text = unicodedata.normalize("NFKC", value).strip().replace(",", "")
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _as_bool(value: OutputValue) -> bool | None:
    if isinstance(value, bool):
        return value
    text = _text(value) if value is not None else ""
    if text in _TRUE_TEXTS:
        return True
    if text in _FALSE_TEXTS:
        return False
    return None


def _as_date(value: OutputValue) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(unicodedata.normalize("NFKC", value).strip()[:10])
    except ValueError:
        return None


def _empty(value: OutputValue) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def output_value_matches(expected: OutputValue, actual: OutputValue, field_type: str) -> bool:
    """期待と記録の値が同じか（型ごとの比べ方。null は記録していないことを期待する）。"""
    if _empty(expected) or _empty(actual):
        return _empty(expected) and _empty(actual)
    if field_type == "number":
        left, right = _as_number(expected), _as_number(actual)
        if left is not None and right is not None:
            return abs(left - right) <= 1e-9 * max(1.0, abs(left), abs(right))
    elif field_type == "boolean":
        left_bool, right_bool = _as_bool(expected), _as_bool(actual)
        if left_bool is not None and right_bool is not None:
            return left_bool == right_bool
    elif field_type == "date":
        left_date, right_date = _as_date(expected), _as_date(actual)
        if left_date is not None and right_date is not None:
            return left_date == right_date
    return _text(expected) == _text(actual)


def _pair_rows(
    expected: list[dict[str, OutputValue]], actual: list[dict[str, Any]], key: str | None
) -> list[dict[str, Any] | None]:
    """期待の行ごとに組む記録の行（1 列目の値で組み、組めない行は残った記録の行を順に使う）。"""
    remaining = list(range(len(actual)))
    paired: list[int | None] = [None] * len(expected)
    if key is not None:
        for index, row in enumerate(expected):
            if key not in row or _empty(row[key]):
                continue
            match = next(
                (
                    candidate
                    for candidate in remaining
                    if not _empty(actual[candidate].get(key))
                    and _text(actual[candidate].get(key)) == _text(row[key])
                ),
                None,
            )
            if match is not None:
                paired[index] = match
                remaining.remove(match)
    for index in range(len(expected)):
        if paired[index] is None and remaining:
            paired[index] = remaining.pop(0)
    return [actual[item] if item is not None else None for item in paired]


def compare_output(
    expected: list[dict[str, OutputValue]], content: dict[str, Any] | None
) -> tuple[list[OutputFieldResult], bool | None]:
    """期待する出力と記録した出力（成果物 `structured_output` の内容）を項目ごとに比べる。

    期待する出力が無ければ判定しない（`[]`, None）。記録が無ければ、期待したすべての項目を
    誤りにする。期待の行に書いた項目だけを判定する。
    """
    if not expected:
        return [], None
    columns = content.get("columns") if isinstance(content, dict) else None
    rows = content.get("rows") if isinstance(content, dict) else None
    column_list = (
        [item for item in columns if isinstance(item, dict)] if isinstance(columns, list) else []
    )
    types = {str(item.get("name")): str(item.get("type") or "text") for item in column_list}
    methods: dict[str, OutputFieldMatch] = {
        str(item.get("name")): "meaning"
        for item in column_list
        if item.get("match") == "meaning" and (item.get("type") or "text") == "text"
    }
    labels = {str(item.get("name")): str(item.get("label") or "") for item in column_list}
    actual_rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    key = str(column_list[0].get("name")) if column_list else None
    results: list[OutputFieldResult] = []
    for number, (want, got) in enumerate(
        zip(expected, _pair_rows(expected, actual_rows, key), strict=True), start=1
    ):
        for field, value in want.items():
            actual = got.get(field) if got is not None else None
            results.append(
                OutputFieldResult(
                    row=number,
                    field=field,
                    label=labels.get(field, ""),
                    expected=value,
                    actual=actual,
                    correct=got is not None
                    and output_value_matches(value, actual, types.get(field, "text")),
                    method=methods.get(field, "exact"),
                )
            )
    return results, all(item.correct for item in results)


def meaning_candidates(results: Sequence[OutputFieldResult]) -> list[int]:
    """意味で比べる項目のうち、文字が一致せず、期待・記録の両方に値がある判定の位置。"""
    return [
        index
        for index, item in enumerate(results)
        if item.method == "meaning"
        and not item.correct
        and not _empty(item.expected)
        and not _empty(item.actual)
    ]


def without_output_record_sections(answer: str, report_titles: Sequence[str] = ()) -> str:
    """記録報告の節を、資料との照合の前に外す。"""
    titles = {title.strip().rstrip(":：") for title in report_titles if title.strip()}
    lines: list[str] = []
    skipping = False
    fenced = False
    for line in answer.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        heading = (
            None
            if fenced
            else re.fullmatch(
                r"\s*(?:#{1,6}\s+(.+)|\*\*(.+)\*\*)\s*", normalize_heading_markup(line)
            )
        )
        if heading:
            title = heading.group(1) or heading.group(2)
            skipping = title.strip().rstrip(":：") in titles or bool(
                re.search(rf"(?<![a-zA-Z0-9_]){RECORD_OUTPUT_TOOL}(?![a-zA-Z0-9_])", title)
            )
        if not skipping:
            lines.append(line)
    return "\n".join(lines).strip()
