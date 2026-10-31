"""最終の検証で、表計算・CSV の 1 行の記録の根拠を同じ表に組み直す（#2594）。

取込（前処理の変換・要素化・分割）から根拠の読み直しまでを合成の CSV で通す。監査（モデル）は
スタブ。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from pytest import MonkeyPatch
from rag_engine.generation.table_binding import unbound_table_values
from rag_engine.generation.table_records import TABLE_RECORD_KEY, record_tables
from rag_parser_core.registry import run_external_adapter

from app.config import get_settings
from app.rag.answer_validation import EvidenceRef, table_record, validate_answer
from app.rag.chunking import chunk_extraction_with_strategy
from app.schemas.extraction import StructuredExtraction
from app.schemas.search import RetrievedChunk

_RAG_DIR = Path(__file__).resolve().parents[2]


def _csv_converter() -> ModuleType:
    path = _RAG_DIR / "services" / "preprocess" / "csv_to_jsonl" / "app" / "converters.py"
    spec = importlib.util.spec_from_file_location("csv_to_jsonl_converters_2594", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _csv_chunks(document_id: str, chunk_set_id: str) -> list[RetrievedChunk]:
    source = "区分,方式,上限値\n区分 A,方式 X,120\n区分 B,方式 Y,300\n区分 C,方式 Y,45\n".encode()
    outcome = _csv_converter().convert(source, "text/csv", "csv_to_jsonl", None)
    assert outcome.converted is True
    extraction = run_external_adapter(
        "structured_records_jsonl", outcome.derived_bytes, None, ""
    ).extraction
    assert extraction is not None
    chunks = chunk_extraction_with_strategy(
        StructuredExtraction.model_validate(extraction.model_dump()), strategy="fixed_delimiter"
    )
    return [
        RetrievedChunk(
            document_id=document_id,
            chunk_id=f"{document_id}-c{index}",
            text=chunk.text,
            score=0.0,
            file_name="table.csv",
            metadata={**dict(chunk.metadata), "chunk_set_id": chunk_set_id},
        )
        for index, chunk in enumerate(chunks)
    ]


class _Oracle:
    def __init__(self, chunks: list[RetrievedChunk]) -> None:
        self.chunks = {(chunk.document_id, chunk.chunk_id): chunk for chunk in chunks}

    async def retrievable_chunk(self, document_id: str, chunk_id: str) -> Any:
        return self.chunks.get((document_id, chunk_id))

    async def accessible_chunk_exists(self, document_id: str, chunk_id: str) -> bool:
        return False


def test_table_record_needs_cell_coordinates_of_a_spreadsheet_or_csv_row() -> None:
    cells = '[["A",["区分"]],["B",["値"]]]'
    record = table_record(
        "d1", {"row_basis": "csv", "table_row": 3, "table_cells_json": cells, "chunk_set_id": "s1"}
    )
    assert record == {
        "group": '["d1", "s1", "csv", null, null]',
        "row": 3,
        "cells": [["A", ["区分"]], ["B", ["値"]]],
    }
    # 表計算はシートと表の番号で同じ表を分ける。
    sheet = table_record(
        "d1",
        {"sheet_name": "表", "sheet_table_index": 2, "table_row": 5, "table_cells_json": cells},
    )
    assert sheet is not None and sheet["group"] == '["d1", null, "sheet", "表", 2]'
    # セルの座標の無い根拠・Word のまとまり・解析の表の行・壊れた値は持たない。
    assert table_record("d1", {"row_basis": "csv", "table_row": 3}) is None
    assert (
        table_record("d1", {"row_basis": "word_table", "table_row": 3, "table_cells_json": cells})
        is None
    )
    assert (
        table_record("d1", {"row_basis": "parsed_table", "table_row": 3, "table_cells_json": cells})
        is None
    )
    assert table_record("d1", {"row_basis": "csv", "table_row": 3, "table_cells_json": "["}) is None


async def test_validation_rebuilds_rows_of_the_same_table(monkeypatch: MonkeyPatch) -> None:
    """読み直した 1 行の記録の根拠に同じ表の印を付け、検査が組み直した表で別の行の値を見つける。"""
    chunks = _csv_chunks("d1", "set-1") + _csv_chunks("d2", "set-2")
    captured: dict[str, Any] = {}

    def fake_claims(
        question: str, answer: str, items: list[dict[str, Any]], settings: Any, **_: Any
    ) -> dict[str, Any]:
        captured["items"] = items
        return {
            "status": "completed",
            "counts": {"supported": 1},
            "claim_checks": [],
            "evidence_truncated": False,
        }

    import rag_engine.evaluation.answer_validation as engine_validation

    monkeypatch.setattr(engine_validation, "validate_answer_claims", fake_claims)
    refs = [EvidenceRef(chunk.document_id, chunk.chunk_id) for chunk in chunks]
    await validate_answer("q", "a", refs, get_settings(), oracle=cast(Any, _Oracle(chunks)))
    items = captured["items"]
    records = [item[TABLE_RECORD_KEY] for item in items]
    # 文書（と処理の版）ごとに別の表。行は記録の番号（表頭が 1）。
    assert len({record["group"] for record in records}) == 2
    assert sorted(record["row"] for record in records) == [2, 2, 3, 3, 4, 4]
    first = [item for item in items if item["id"].startswith("d1-")]
    tables = record_tables(first)
    assert len(tables) == 1
    texts = [item["text"] for item in first]
    assert unbound_table_values("区分 B の上限値は 45 です。", texts) == []
    assert unbound_table_values("区分 B の上限値は 45 です。", texts + tables) == [("45", "区分 B")]
    assert unbound_table_values("区分 B の上限値は 300 です。", texts + tables) == []
