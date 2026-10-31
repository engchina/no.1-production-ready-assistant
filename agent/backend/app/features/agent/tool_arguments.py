"""ツールの引数の検証（#2630）。

ツールの実行の境界（組み込み Runtime の function tool・`tool_registry.invoke`）で、引数をツールの
input schema（JSON Schema）と照合する。schema をモデルに渡すことと、返った引数をこちらで確かめる
ことは別の保証なので、呼び先（handler・MCP）へ送る前に必ず確かめる。

- 完成した引数の JSON が読めない・root が object でないときは、空の引数に置き換えずに引数の誤りに
  する（`parse_arguments`）。空の文字列だけは引数なし（`{}`）として読む。
- schema との照合は jsonschema（`$schema` が無ければ Draft 2020-12）で、型・必須・enum・範囲・
  追加の項目・入れ子を見る（`validate_arguments`）。`format` は注釈として扱い、検査しない。
- 外部の `$ref` は取りに行かない（取りに行く関数が常に失敗する registry を使う）。解決できない
  `$ref`・壊れた schema・大きすぎる schema は「検証できない」（`SchemaUnsupported`）として返し、
  検証済みとは扱わない（呼ぶ側が監査に残す）。
- 引数の大きさと入れ子の深さに上限を置き、validator の資源を限る。
- 誤りは値を含まない場所（`argument`）・種類（`code`）・直し方（`message`）だけにする（#2611。
  モデルへ返す・記録する文に、渡された値や秘密を写さない）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError
from jsonschema.protocols import Validator
from jsonschema.validators import validator_for
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource, Unresolvable

logger = logging.getLogger(__name__)

# 引数の誤りの code（モデルへ返すツールの結果・step の記録）。
INVALID_ARGUMENTS_CODE = "tool.invalid_arguments"
INVALID_ARGUMENTS_MESSAGE = (
    "ツールの引数がツールの定義（input schema）に合わないため、実行していません。"
    "argument_errors の場所と直し方に従って引数を直し、呼び直してください。"
)
# モデルへ返す誤りの件数、場所の文字数の上限。
MAX_ARGUMENT_ERRORS = 5
_MAX_PATH_CHARS = 100
# 調べる誤りの数の上限（多すぎる誤りで validator を回し続けない）。
_MAX_COLLECTED_ERRORS = 50
# 引数の JSON の大きさ（文字数）と入れ子の深さの上限。
MAX_ARGUMENT_CHARS = 1_000_000
MAX_ARGUMENT_DEPTH = 64
# 検証に使う schema の大きさ（JSON の文字数）の上限。
MAX_SCHEMA_CHARS = 256_000

_TYPE_LABELS: dict[str, str] = {
    "string": "文字列",
    "integer": "整数",
    "number": "数値",
    "boolean": "真偽値（true / false）",
    "object": "object",
    "array": "配列",
    "null": "null",
}


@dataclass(frozen=True)
class ArgumentError:
    """引数の 1 つの誤り（値を含まない）。"""

    argument: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"argument": self.argument, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class SchemaUnsupported:
    """schema で引数を検証できない（検証済みとは扱わない。理由は監査とログに残す）。"""

    reason: str


def parse_arguments(raw: str | None) -> tuple[dict[str, Any] | None, list[ArgumentError]]:
    """モデルが返した引数の JSON を読む（読めなければ None と誤り）。

    空の文字列（引数なし）だけは `{}` として読む。読めない JSON・object 以外の root は、空の引数に
    置き換えずに誤りにする。
    """
    text = raw if isinstance(raw, str) else ""
    if not text.strip():
        return {}, []
    if len(text) > MAX_ARGUMENT_CHARS:
        return None, [
            ArgumentError(
                "",
                "too_large",
                f"引数の JSON は {MAX_ARGUMENT_CHARS} 文字以内にしてください。",
            )
        ]
    try:
        parsed = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None, [
            ArgumentError(
                "",
                "invalid_json",
                '引数は完全な JSON の object（例: {"name": ...}）で渡してください。'
                "途中で切れた・壊れた JSON は実行しません。",
            )
        ]
    if not isinstance(parsed, dict):
        return None, [
            ArgumentError(
                "",
                "not_object",
                "引数は JSON の object（{ ... }）で渡してください。"
                "配列・文字列・数値は使えません。",
            )
        ]
    return parsed, []


def _reject_constant(_name: str) -> Any:
    # NaN・Infinity は JSON の値ではない（schema の数値の検査をすり抜けさせない）。
    raise ValueError("non-standard JSON constant")


def validate_arguments(
    schema: Mapping[str, Any] | None,
    arguments: Mapping[str, Any],
    *,
    ignore: Collection[str] = (),
) -> list[ArgumentError] | SchemaUnsupported:
    """引数を schema と照合する（誤りの一覧。無ければ空。検証できなければ `SchemaUnsupported`）。

    `ignore` は照合から外す最上位の引数（実行環境が決める非表示の引数。モデルに見せた schema には
    無いが、モデルが渡しても実行環境が上書き・除去する）。
    """
    if not isinstance(schema, Mapping) or not schema:
        # 定義の無いツール（schema が空）は何でも受け付ける契約（検証すべき条件が無い）。
        return []
    depth_error = _depth_error(arguments)
    if depth_error is not None:
        return [depth_error]
    try:
        schema_text = json.dumps(schema, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return SchemaUnsupported("schema_not_json")
    if len(schema_text) > MAX_SCHEMA_CHARS:
        return SchemaUnsupported("schema_too_large")
    validator = _validator(schema_text)
    if isinstance(validator, SchemaUnsupported):
        return validator
    instance = {key: value for key, value in arguments.items() if key not in ignore}
    try:
        found = _collect(validator.iter_errors(instance))
    except Unresolvable:
        return SchemaUnsupported("unresolvable_ref")
    except RecursionError:
        return SchemaUnsupported("schema_too_deep")
    errors: list[ArgumentError] = []
    seen: set[tuple[str, str]] = set()
    for error in sorted(found, key=_error_order):
        for item in _describe(error):
            key = (item.argument, item.code)
            if key in seen:
                continue
            seen.add(key)
            errors.append(item)
    return errors[:MAX_ARGUMENT_ERRORS]


def errors_payload(errors: Iterable[ArgumentError]) -> dict[str, Any]:
    """モデルへ返す引数の誤り（ツールの結果。`reject_call` の payload）。"""
    return {
        "error": INVALID_ARGUMENTS_MESSAGE,
        "error_code": INVALID_ARGUMENTS_CODE,
        "argument_errors": [item.as_dict() for item in list(errors)[:MAX_ARGUMENT_ERRORS]],
    }


def _collect(errors: Iterable[ValidationError]) -> list[ValidationError]:
    collected: list[ValidationError] = []
    for error in errors:
        collected.append(error)
        if len(collected) >= _MAX_COLLECTED_ERRORS:
            break
    return collected


def _error_order(error: ValidationError) -> tuple[str, str]:
    return (_path(error.absolute_path), str(error.validator))


def _no_remote(uri: str) -> Resource[Any]:
    # 外部の $ref は取りに行かない（通信しない。#2630）。
    raise NoSuchResource(ref=uri)  # type: ignore[call-arg]


_REGISTRY: Registry[Any] = Registry(retrieve=_no_remote)  # type: ignore[call-arg]


@lru_cache(maxsize=256)
def _validator(schema_text: str) -> Validator | SchemaUnsupported:
    """schema の validator（同じ schema は作り直さない）。"""
    schema = json.loads(schema_text)
    if not isinstance(schema, dict):
        return SchemaUnsupported("schema_not_object")
    cls = validator_for(schema, default=Draft202012Validator)
    try:
        cls.check_schema(schema)
    except SchemaError:
        return SchemaUnsupported("invalid_schema")
    except Unresolvable:
        return SchemaUnsupported("unresolvable_ref")
    except RecursionError:
        return SchemaUnsupported("schema_too_deep")
    validator: Validator = cls(schema, registry=_REGISTRY)
    return validator


def _depth_error(value: Any) -> ArgumentError | None:
    """入れ子が深すぎれば誤り（再帰せずに数える）。"""
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > MAX_ARGUMENT_DEPTH:
            return ArgumentError(
                "",
                "too_deep",
                f"引数の入れ子は {MAX_ARGUMENT_DEPTH} 段までにしてください。",
            )
        if isinstance(item, Mapping):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list | tuple):
            stack.extend((child, depth + 1) for child in item)
    return None


def _path(parts: Iterable[Any]) -> str:
    """引数の場所（`items.0.name`。呼び先の誤りの `loc` と同じ書き方。#2498）。値は含めない。"""
    names = [" ".join(str(part).split()) for part in parts]
    return ".".join(names)[:_MAX_PATH_CHARS]


def _join(base: str, name: str) -> str:
    name = " ".join(name.split())
    return (f"{base}.{name}" if base else name)[:_MAX_PATH_CHARS]


def _type_label(expected: Any) -> str:
    if isinstance(expected, str):
        return _TYPE_LABELS.get(expected, expected)
    if isinstance(expected, list):
        return " または ".join(_TYPE_LABELS.get(str(item), str(item)) for item in expected)
    return "定義の型"


def _describe(error: ValidationError) -> list[ArgumentError]:
    """jsonschema の誤りを、値を含まない場所・種類・直し方にする。"""
    keyword = str(error.validator)
    path = _path(error.absolute_path)
    expected = error.validator_value
    instance = error.instance
    if keyword == "required":
        names = expected if isinstance(expected, list) else []
        present = instance if isinstance(instance, Mapping) else {}
        return [
            ArgumentError(
                _join(path, str(name)), "required", "必須の引数です。値を渡してください。"
            )
            for name in names
            if isinstance(name, str) and name not in present
        ] or [ArgumentError(path, "required", "必須の引数を渡してください。")]
    if keyword in {"additionalProperties", "unevaluatedProperties"}:
        allowed = set()
        if isinstance(error.schema, Mapping):
            properties = error.schema.get("properties")
            if isinstance(properties, Mapping):
                allowed = set(properties)
        present = instance if isinstance(instance, Mapping) else {}
        extra = [name for name in present if name not in allowed]
        return [
            ArgumentError(
                _join(path, str(name)),
                "additional_properties",
                "定義に無い引数です。この引数を外してください。",
            )
            for name in extra
        ] or [ArgumentError(path, "additional_properties", "定義に無い引数を外してください。")]
    if keyword == "type":
        return [ArgumentError(path, "type", f"{_type_label(expected)}にしてください。")]
    if keyword in {"enum", "const"}:
        return [
            ArgumentError(
                path, keyword, "ツールの定義（enum / const）が決める値のどれかにしてください。"
            )
        ]
    if keyword in {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}:
        bound = expected if isinstance(expected, int | float) else None
        hints = {
            "minimum": "以上",
            "maximum": "以下",
            "exclusiveMinimum": "より大きい値",
            "exclusiveMaximum": "より小さい値",
            "multipleOf": "の倍数",
        }
        if bound is None or isinstance(bound, bool):
            return [ArgumentError(path, keyword, "定義の範囲の値にしてください。")]
        return [ArgumentError(path, keyword, f"{bound} {hints[keyword]}にしてください。")]
    if keyword in {"minLength", "maxLength", "minItems", "maxItems", "minProperties"}:
        unit = "文字" if keyword.endswith("Length") else "件"
        side = "以上" if keyword.startswith("min") else "以下"
        if isinstance(expected, int) and not isinstance(expected, bool):
            return [ArgumentError(path, keyword, f"{expected} {unit}{side}にしてください。")]
        return [ArgumentError(path, keyword, "定義の長さ・件数にしてください。")]
    if keyword == "maxProperties" and isinstance(expected, int):
        return [ArgumentError(path, keyword, f"項目は {expected} 個以下にしてください。")]
    if keyword == "uniqueItems":
        return [ArgumentError(path, keyword, "重複しない値の配列にしてください。")]
    if keyword == "pattern":
        return [ArgumentError(path, keyword, "定義の形式（pattern）に合う文字列にしてください。")]
    return [
        ArgumentError(path, keyword, f"ツールの定義の条件（{keyword}）を満たす値にしてください。")
    ]


def hidden_arguments(
    contract_schema: Mapping[str, Any] | None, visible_schema: Mapping[str, Any] | None
) -> frozenset[str]:
    """呼び先の契約にあり、モデルに見せた schema には無い最上位の引数（実行環境が決めるもの）。"""
    contract = _properties(contract_schema)
    visible = _properties(visible_schema)
    return frozenset(name for name in contract if name not in visible)


def _properties(schema: Mapping[str, Any] | None) -> set[str]:
    properties = schema.get("properties") if isinstance(schema, Mapping) else None
    return {str(name) for name in properties} if isinstance(properties, Mapping) else set()
