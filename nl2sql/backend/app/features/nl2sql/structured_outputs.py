"""Responses API Structured Outputs の wire 契約とローカル検証。"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field


class TextOutput(BaseModel):
    text: str


class SqlOutput(BaseModel):
    sql: str
    explanation: str = ""


class AnalysisOutput(BaseModel):
    analysis: str


# DB 管理チャットの SQL の生成（#2264）。質問には SQL で答える（SQL の無い応答は作り直させる。
# #2333）。
# docstring は JSON schema の description としてモデルに渡るので、経緯はこのコメントに書く。
class DbAdminChatSqlOutput(BaseModel):
    sql: str = Field(description="質問に答える・作業を行う Oracle の SQL（1 文以上）。")
    explanation: str = Field(default="", description="SQL が何をするかの短い説明。")


# DB 管理チャットの、存在しない列・object を参照する文の作り直し（#2352）。
class DbAdminChatRepairedStatement(BaseModel):
    index: int = Field(description="直した文の番号（context の文の番号）。")
    sql: str = Field(description="直した 1 文。")


class DbAdminChatRepairOutput(BaseModel):
    statements: list[DbAdminChatRepairedStatement] = Field(default_factory=list)


class DbAdminChatAnalysisOutput(BaseModel):
    """DB 管理チャットの失敗の分析（#2264）。直せないときは corrected_sql が空。"""

    cause: str
    fix: str
    conclusion: str
    corrected_sql: str = Field(
        default="", description="失敗した対象の 1 文を直した SQL。直せないときは空文字列。"
    )


class StructureOutput(BaseModel):
    logical_structure: str


class QuestionOutput(BaseModel):
    question: str


class CommentItemOutput(BaseModel):
    object_name: str
    object_type: str
    suggested_comment: str


class CommentsOutput(BaseModel):
    suggestions: list[CommentItemOutput]


def strict_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """domain の既定値は変更せず、送信する全 object を closed/required にする。"""
    result = copy.deepcopy(dict(schema))

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object" or "properties" in node:
                if node.get("additionalProperties") not in (None, False):
                    raise ValueError("Structured Outputs には固定 field の object が必要です。")
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for group in ("properties", "$defs", "definitions"):
                for value in node.get(group, {}).values():
                    visit(value)
            for key in ("items", "anyOf", "oneOf", "allOf"):
                if key in node:
                    visit(node[key])
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(result)
    return result


def response_format(model: type[BaseModel], *, name: str | None = None) -> dict[str, Any]:
    return format_schema(
        strict_schema(model.model_json_schema()), name or model.__name__.strip("_")[:64]
    )


def format_schema(schema: dict[str, Any], name: str) -> dict[str, Any]:
    return {"type": "json_schema", "name": name, "schema": schema, "strict": True}


def validate_json_output(raw: str, output_format: Mapping[str, Any]) -> str:
    """切出し/補修せず全文を検証。生応答や validation value は例外文に含めない。"""
    try:
        value = json.loads(raw)
        validator = Draft202012Validator(output_format["schema"])
        if not isinstance(value, dict) or not validator.is_valid(value):
            raise ValueError("schema mismatch")
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError(
            "Structured Outputs の応答が指定 JSON Schema を満たしていません。"
        ) from exc
    return raw
