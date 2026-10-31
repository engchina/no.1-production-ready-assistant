"""入力の検証エラー（422）の利用者向けの日本語の文（3 製品共通。#1065）。

FastAPI / Pydantic の検証エラーは、内部の位置（`body.cases.0.query`）と英語の文
（`Field required`）を持つ。画面と API の利用者にはそのまま見せず、次の形にする。

- 自前の検証（`ValueError` / `assert` / `PydanticCustomError`）の日本語の文は、位置も
  `Value error, ` の接頭辞も付けずにそのまま出す（欄の名前は文が持っている前提）。
- Pydantic の組み込みのエラーは `type` ごとの日本語の文にし、利用者が書いた JSON の位置
  （`cases[0].query`。`body` / `query` などの出どころは外す）を前に付ける。
- 技術的な原文（loc・type・msg）は `problem.field_errors[]` の `raw_location` / `code` /
  `raw_message` に残す（画面の「詳細」用）。入力値（`input`）は応答に含めない。
"""

from __future__ import annotations

import re
import typing
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from pydantic_core.core_schema import ErrorType
from starlette.responses import JSONResponse

from ..schemas import ApiResponse

VALIDATION_ERROR_CODE = "REQUEST_VALIDATION_FAILED"
VALIDATION_ERROR_TITLE = "入力内容を確認してください"
VALIDATION_ERROR_TYPE = "urn:production-ready:problem:request-validation-failed"
DEFAULT_VALIDATION_MESSAGE = "リクエストの形式が不正です。"
FALLBACK_FIELD_MESSAGE = "入力内容を確認してください。"

# loc の先頭の、値の出どころ（request の部分）を表す要素。利用者には見せない。
_LOCATION_SOURCES = frozenset({"body", "query", "path", "header", "cookie"})
# 自前の検証の例外の文に Pydantic が付ける接頭辞。
_CUSTOM_MESSAGE_PREFIXES = {
    "value_error": "Value error, ",
    "assertion_error": "Assertion failed, ",
}
# Pydantic の組み込みの error type。これ以外の type は `PydanticCustomError`（自前の文）。
_BUILTIN_ERROR_TYPES = frozenset(typing.get_args(ErrorType))
# union の候補の型など、Pydantic が loc に足す schema の名前（利用者の JSON には無い）。
_SCHEMA_LOCATION_PARTS = frozenset({"str", "int", "float", "bool", "none", "list", "dict"})
# 日本語（ひらがな・カタカナ・漢字・全角の記号）を含むか。自前の文かどうかの判定に使う。
_JAPANESE_RE = re.compile(r"[\u3000-\u30ff\u3400-\u9fff\uff00-\uffef]")

_INTEGER = "整数で入力してください。"
_NUMBER = "数値で入力してください。"
_BOOLEAN = "true か false で指定してください。"
_OBJECT = "オブジェクト（{{ }}）で指定してください。"
_ARRAY = "配列（[ ]）で指定してください。"
_CHOICE = "指定できる値ではありません（指定できる値: {expected}）。"
_JSON = "JSON の形式が正しくありません。"
_URL = "URL の形式で入力してください。"
_DATETIME = "日時の形式で入力してください。"
_DATE = "日付の形式で入力してください。"
_TIME = "時刻の形式で入力してください。"
_UUID = "UUID の形式で入力してください。"

# Pydantic の error の type → 日本語。`{name}` は error の ctx の値で埋める。
_MESSAGES: dict[str, str] = {
    "missing": "必須の項目です。入力してください。",
    "extra_forbidden": "この項目は指定できません。",
    "none_required": "この項目は指定できません。",
    "string_type": "文字列で入力してください。",
    "string_sub_type": "文字列で入力してください。",
    "string_unicode": "文字列で入力してください。",
    "string_too_short": "{min_length} 文字以上で入力してください。",
    "string_too_long": "{max_length} 文字以内で入力してください。",
    "string_pattern_mismatch": "入力形式を確認してください。",
    "string_not_ascii": "半角英数字と記号で入力してください。",
    "int_type": _INTEGER,
    "int_parsing": _INTEGER,
    "int_from_float": _INTEGER,
    "int_parsing_size": "指定できる範囲の整数で入力してください。",
    "float_type": _NUMBER,
    "float_parsing": _NUMBER,
    "finite_number": "有限の数値で入力してください。",
    "decimal_type": _NUMBER,
    "decimal_parsing": _NUMBER,
    "decimal_max_digits": "{max_digits} 桁以内の数値で入力してください。",
    "decimal_max_places": "小数点以下 {decimal_places} 桁以内で入力してください。",
    "decimal_whole_digits": "整数部 {whole_digits} 桁以内で入力してください。",
    "multiple_of": "{multiple_of} の倍数で入力してください。",
    "bool_type": _BOOLEAN,
    "bool_parsing": _BOOLEAN,
    "list_type": _ARRAY,
    "tuple_type": _ARRAY,
    "set_type": _ARRAY,
    "frozen_set_type": _ARRAY,
    "iterable_type": _ARRAY,
    "dict_type": _OBJECT,
    "mapping_type": _OBJECT,
    "model_type": _OBJECT,
    "model_attributes_type": _OBJECT,
    "dataclass_type": _OBJECT,
    "too_short": "{min_length} 件以上指定してください。",
    "too_long": "{max_length} 件以内にしてください。",
    "greater_than": "{gt} より大きい値を入力してください。",
    "greater_than_equal": "{ge} 以上の値を入力してください。",
    "less_than": "{lt} より小さい値を入力してください。",
    "less_than_equal": "{le} 以下の値を入力してください。",
    "literal_error": _CHOICE,
    "enum": _CHOICE,
    "union_tag_invalid": "指定できる種類ではありません（指定できる種類: {expected_tags}）。",
    "union_tag_not_found": "種類（{discriminator}）を指定してください。",
    "json_invalid": _JSON,
    "json_type": _JSON,
    "url_type": _URL,
    "url_parsing": _URL,
    "url_syntax_violation": _URL,
    "url_scheme": "URL のスキームは {expected_schemes} のいずれかにしてください。",
    "url_too_long": "URL は {max_length} 文字以内にしてください。",
    "datetime_type": _DATETIME,
    "datetime_parsing": _DATETIME,
    "datetime_from_date_parsing": _DATETIME,
    "datetime_object_invalid": _DATETIME,
    "timezone_naive": "タイムゾーンを含まない日時で入力してください。",
    "timezone_aware": "タイムゾーンを含む日時で入力してください。",
    "date_type": _DATE,
    "date_parsing": _DATE,
    "date_from_datetime_parsing": _DATE,
    "date_from_datetime_inexact": _DATE,
    "date_past": "過去の日付を入力してください。",
    "date_future": "未来の日付を入力してください。",
    "datetime_past": "過去の日時を入力してください。",
    "datetime_future": "未来の日時を入力してください。",
    "time_type": _TIME,
    "time_parsing": _TIME,
    "time_delta_type": "期間の形式で入力してください。",
    "time_delta_parsing": "期間の形式で入力してください。",
    "uuid_type": _UUID,
    "uuid_parsing": _UUID,
    "uuid_version": _UUID,
}


class _FieldProblem(NamedTuple):
    pointer: str
    code: str
    message: str
    location: str
    raw_location: str
    raw_message: str
    custom: bool

    def public(self) -> dict[str, str]:
        return {
            "pointer": self.pointer,
            "code": self.code,
            "message": self.message,
            "location": self.location,
            "raw_location": self.raw_location,
            "raw_message": self.raw_message,
        }

    def display(self) -> str:
        if self.custom or not self.location:
            return self.message
        return f"{self.location}: {self.message}"


def validation_field_errors(errors: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """検証エラーを、欄ごとの問題（problem 契約の `field_errors`）にする。

    - `pointer`: RFC 6901 JSON Pointer（`/cases/0/query`。画面が欄に結び付ける）
    - `code`: Pydantic の error type（`missing` など。自前の検証は `value_error`）
    - `message`: 欄の横に出す日本語の文（位置を含まない）
    - `location`: 利用者に見せる位置（`cases[0].query`。無ければ空）
    - `raw_location` / `raw_message`: 技術的な原文（画面の「詳細」用）
    """
    return [_field_problem(error).public() for error in errors]


def validation_error_messages(errors: Sequence[Mapping[str, Any]]) -> list[str]:
    """検証エラーを、利用者向けの日本語の文（重複なし・順序は保つ）にする。"""
    messages: list[str] = []
    for error in errors:
        message = _field_problem(error).display()
        if message not in messages:
            messages.append(message)
    return messages


def validation_error_message(error: Mapping[str, Any]) -> str:
    """1 件の検証エラーを `位置: 日本語の文`（位置が無い・自前の文なら文だけ）にする。"""
    return _field_problem(error).display()


def validation_tool_errors(errors: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """MCP のツールの引数の誤り（`details.errors`）の形にする。

    `loc` は利用者に見せる位置（`cases[0].query`）、`message` は日本語の文、`type` と
    `raw_message` は技術的な原文。ツールの入力の loc には要求の出どころが無く、先頭は引数の
    名前なので、`query` などの名前の引数を出どころとして取り除かない（#2505）。
    """
    return [
        {
            "loc": item.location,
            "message": item.message,
            "type": item.code,
            "raw_message": item.raw_message,
        }
        for item in (_field_problem(error, strip_source=False) for error in errors)
    ]


def validation_error_content(
    errors: Sequence[Mapping[str, Any]], *, request_id: str | None = None
) -> dict[str, Any]:
    """422 の応答の本文（ApiResponse の envelope + `error_code` + `problem`）。

    `problem` は NL2SQL の problem 契約（RFC 9457 の語彙）と同じ形で、3 製品の画面の
    `ApiError` がそのまま読む（`field_errors` は欄の横の表示と「詳細」に使う）。
    """
    field_errors = validation_field_errors(errors)
    messages = validation_error_messages(errors) or [DEFAULT_VALIDATION_MESSAGE]
    detail = "\n".join(messages)
    content = ApiResponse[object](data=None, error_messages=messages).model_dump(mode="json")
    content["error_code"] = VALIDATION_ERROR_CODE
    content["problem"] = {
        "type": VALIDATION_ERROR_TYPE,
        "title": VALIDATION_ERROR_TITLE,
        "status": 422,
        "detail": detail,
        "code": VALIDATION_ERROR_CODE,
        "request_id": request_id or "",
        "retryable": False,
        "field_errors": field_errors,
    }
    return content


def validation_error_response(
    errors: Sequence[Mapping[str, Any]], *, request_id: str | None = None
) -> JSONResponse:
    """422 の検証エラーの応答（`install_exception_handlers` と製品の handler が使う）。"""
    headers = {"X-Request-ID": request_id} if request_id else {}
    return JSONResponse(
        status_code=422,
        content=validation_error_content(errors, request_id=request_id),
        headers=headers,
    )


def _field_problem(error: Mapping[str, Any], *, strip_source: bool = True) -> _FieldProblem:
    """1 件の検証エラー。``strip_source`` は loc の先頭の要求の出どころ（`body` など）を除くか。"""
    error_type = str(error.get("type") or "")
    raw_message = str(error.get("msg") or "").strip()
    loc = error.get("loc", ())
    parts = list(loc) if isinstance(loc, list | tuple) else []
    custom = _custom_message(error_type, raw_message)
    # JSON の構文の誤りの loc は本文の中の文字の位置（数）で、欄の位置ではない。
    location_parts = (
        []
        if error_type in {"json_invalid", "json_type"}
        else _user_parts(parts, strip_source=strip_source)
    )
    return _FieldProblem(
        pointer=_json_pointer(location_parts),
        code=error_type or "invalid",
        message=custom or _japanese_message(error_type, error.get("ctx")),
        location=_location(location_parts),
        raw_location=".".join(str(part) for part in parts),
        raw_message=raw_message,
        custom=bool(custom),
    )


def _custom_message(error_type: str, raw_message: str) -> str:
    """自前の検証の日本語の文を返す（無ければ空）。英語の文は利用者に見せない。"""
    prefix = _CUSTOM_MESSAGE_PREFIXES.get(error_type)
    if prefix is not None:
        text = raw_message.removeprefix(prefix).strip()
    elif error_type not in _BUILTIN_ERROR_TYPES:
        text = raw_message  # PydanticCustomError（type も文も自前）
    else:
        return ""
    return text if _JAPANESE_RE.search(text) else ""


def _japanese_message(error_type: str, ctx: object) -> str:
    template = _MESSAGES.get(error_type)
    if template is None:
        return FALLBACK_FIELD_MESSAGE
    values = {str(key): value for key, value in ctx.items()} if isinstance(ctx, Mapping) else {}
    for key in ("expected", "expected_tags", "expected_schemes"):
        if key in values:
            # Pydantic は「'a', 'b' or 'c'」の形にする。英語の or と読点を「、」にする。
            values[key] = str(values[key]).replace(" or ", "、").replace(", ", "、")
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError):
        return FALLBACK_FIELD_MESSAGE


def _user_parts(parts: list[Any], *, strip_source: bool = True) -> list[str | int]:
    """loc から出どころ（`body` など。``strip_source`` のとき）と schema の名前を除く。

    schema の名前は union の候補の型など、Pydantic が loc に足す名前。
    """
    if strip_source and parts and parts[0] in _LOCATION_SOURCES:
        parts = parts[1:]
    user_parts: list[str | int] = []
    for part in parts:
        if isinstance(part, int):
            user_parts.append(part)
            continue
        text = str(part)
        if "[" in text or text in _SCHEMA_LOCATION_PARTS or text.startswith("function-"):
            continue
        user_parts.append(text)
    return user_parts


def _location(parts: Sequence[str | int]) -> str:
    """`["cases", 0, "query"]` → `cases[0].query`。"""
    location = ""
    for part in parts:
        if isinstance(part, int):
            location += f"[{part}]"
        else:
            location += f".{part}" if location else part
    return location


def _json_pointer(parts: Sequence[str | int]) -> str:
    escaped = [str(part).replace("~", "~0").replace("/", "~1") for part in parts]
    return "/" + "/".join(escaped) if escaped else "/"
