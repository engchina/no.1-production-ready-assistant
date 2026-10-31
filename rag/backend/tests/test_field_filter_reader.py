"""質問から抽出項目の条件を読み取る(Metadata Filter Extraction。#652)。"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from app.rag.extraction_field_adapter import FieldDefinition
from app.rag.field_filter_reader import (
    merge_field_conditions,
    parse_field_conditions,
    read_field_conditions,
)
from app.schemas.search import EXTRACTION_FIELD_FILTER_KEY, ExtractionFieldCondition

FIELDS = [
    FieldDefinition(name="契約日", value_type="date"),
    FieldDefinition(name="金額", value_type="number"),
    FieldDefinition(name="取引先", value_type="string"),
]


class FakeLlm:
    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str, str]] = []

    async def generate(self, prompt: str, context: str, *, system_prompt: str) -> str:
        self.calls.append((prompt, context, system_prompt))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_parse_keeps_only_defined_fields_with_valid_types() -> None:
    raw = """```json
    [
      {"name": "契約日", "op": "gte", "value": "2025-01-01"},
      {"name": "金額", "op": "gte", "value": "100,000"},
      {"name": "取引先", "op": "gte", "value": "A社"},
      {"name": "担当者", "op": "eq", "value": "山田"},
      {"name": "金額", "op": "gte", "value": "200000"},
      {"name": "金額", "op": "lte", "value": "十万"}
    ]
    ```"""
    conditions = parse_field_conditions(raw, FIELDS)
    # 型は定義を正とし、値は比較できる形へそろえる。定義に無い項目・型に合わない演算子や値・
    # 同じ項目と演算子の重複は捨てる。
    assert [condition.model_dump() for condition in conditions] == [
        {"name": "契約日", "value_type": "date", "op": "gte", "value": "2025-01-01"},
        {"name": "金額", "value_type": "number", "op": "gte", "value": "100000"},
    ]


def test_parse_ignores_broken_output() -> None:
    assert parse_field_conditions("条件はありません", FIELDS) == []
    assert parse_field_conditions('{"name": "金額"}', FIELDS) == []


async def test_read_passes_definitions_and_today() -> None:
    llm = FakeLlm('[{"name": "取引先", "op": "eq", "value": "A社"}]')
    conditions = await read_field_conditions(
        "A社の契約は？",
        FIELDS,
        llm,  # type: ignore[arg-type]
        today=date(2026, 9, 30),
    )
    assert [condition.value for condition in conditions] == ["A社"]
    question, context, _system = llm.calls[0]
    assert question == "A社の契約は？"
    assert "2026-09-30" in context
    assert "契約日" in context


async def test_read_skips_llm_without_definitions_and_survives_errors() -> None:
    llm = FakeLlm("[]")
    assert await read_field_conditions("質問", [], llm) == []  # type: ignore[arg-type]
    assert llm.calls == []
    failing = FakeLlm(RuntimeError("timeout"))
    assert await read_field_conditions("質問", FIELDS, failing) == []  # type: ignore[arg-type]


def _condition(name: str, value_type: Any, op: Any, value: str) -> ExtractionFieldCondition:
    return ExtractionFieldCondition(name=name, value_type=value_type, op=op, value=value)


def test_merge_prefers_manual_conditions() -> None:
    manual = json.dumps([{"name": "金額", "value_type": "number", "op": "lte", "value": "50000"}])
    filters = {"knowledge_base_id": "kb-1", EXTRACTION_FIELD_FILTER_KEY: manual}
    auto = [
        _condition("金額", "number", "gte", "100000"),
        _condition("契約日", "date", "gte", "2025-01-01"),
    ]
    merged, added = merge_field_conditions(filters, auto)
    # 手で指定した項目(金額)には読み取った条件を足さない。
    assert [condition.name for condition in added] == ["契約日"]
    assert merged["knowledge_base_id"] == "kb-1"
    assert [item["name"] for item in json.loads(merged[EXTRACTION_FIELD_FILTER_KEY])] == [
        "金額",
        "契約日",
    ]


def test_merge_without_auto_keeps_filters() -> None:
    filters = {"knowledge_base_id": "kb-1"}
    assert merge_field_conditions(filters, []) == (filters, [])
