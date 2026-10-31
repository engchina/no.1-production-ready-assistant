"""構造化レコード JSONL の解析の抽出を固定分割で 1 記録 1 chunk にする検証（#2457）。

データはすべて合成。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from rag_parser_core.registry import ParserRegistryResult, run_external_adapter
from rag_parser_core.sheet_records import (
    SheetBlock,
    SheetColumn,
    SheetRecords,
    SheetRecordsDocument,
)
from rag_parser_core.structured_records import (
    JSONL_RECORD_BOUNDARY,
    STRUCTURED_RECORDS_CONTENT_TYPE,
    DocumentLine,
    RecordBoundaryError,
    RecordField,
    RecordLocation,
    RowLine,
    dump_structured_records,
    sheet_records_to_lines,
)

from app.rag.chunking import CHUNKING_STRATEGIES, Chunk, chunk_extraction_with_strategy
from app.schemas.extraction import StructuredExtraction

BACKEND = "structured_records_jsonl"


def _csv_extraction(*values: str) -> StructuredExtraction:
    lines: list[DocumentLine | RowLine] = [
        DocumentLine(
            record_id="document",
            source_type="csv",
            converter="csv_to_jsonl",
            converter_version="v2",
            header_row=1,
        )
    ]
    for index, value in enumerate(values, start=1):
        lines.append(
            RowLine(
                record_id=f"row-{index:06d}",
                source_type="csv",
                location=RecordLocation(row_start=index * 2, row_end=index * 2 + 1),
                fields=[
                    RecordField(name="項目", position=1, value=f"値 {index}"),
                    RecordField(name="備考", position=2, value=value),
                ],
            )
        )
    extraction = run_external_adapter(BACKEND, dump_structured_records(lines), None, "").extraction
    assert extraction is not None
    # backend は parser のサービスの応答を検証してから使う（raw_text の正規化を含む）。
    return StructuredExtraction.model_validate(extraction.model_dump())


def _excel_extraction() -> StructuredExtraction:
    document = SheetRecordsDocument(
        source_format="xlsx",
        sheets=[
            SheetRecords(
                name="表 1",
                header_row=1,
                columns=[
                    SheetColumn(name="名前", column="A"),
                    SheetColumn(name="既定値", column="B", role="default", role_method="detected"),
                ],
                blocks=[
                    SheetBlock(
                        kind="row",
                        row_start=row,
                        row_end=row,
                        cell_range=f"A{row}:B{row}",
                        values={"名前": f"設定 {row}", "既定値": str(row * 10)},
                    )
                    for row in (2, 3)
                ],
            )
        ],
    )
    data = dump_structured_records(
        sheet_records_to_lines(document, converter="excel_to_jsonl", converter_version="v4")
    )
    extraction = run_external_adapter(BACKEND, data, None, "").extraction
    assert extraction is not None
    return StructuredExtraction.model_validate(extraction.model_dump())


def _fixed(extraction: StructuredExtraction, delimiter: str = "\\n\\n") -> list[Chunk]:
    return chunk_extraction_with_strategy(
        extraction, strategy="fixed_delimiter", chunk_size=200, overlap=0, delimiter=delimiter
    )


def test_fixed_delimiter_makes_one_chunk_per_record_without_markers() -> None:
    long_note = "長い備考。" * 80
    extraction = _csv_extraction("一行目\n\n二行目", JSONL_RECORD_BOUNDARY, long_note)
    chunks = _fixed(extraction)
    assert [chunk.text for chunk in chunks] == [element.text for element in extraction.elements]
    # 原文の改行（分割符の既定値 \n\n を含む）で切らず、chunk size を超える記録も再分割しない。
    assert chunks[0].text == "項目: 値 1 / 備考: 一行目\n\n二行目"
    assert len(chunks[2].text) > 200
    # 原文の同じ文字列は本文として残り、境界のマーカー（の行）は残らない。
    assert chunks[1].text == f"項目: 値 2 / 備考: {JSONL_RECORD_BOUNDARY}"
    for chunk in chunks:
        assert JSONL_RECORD_BOUNDARY not in chunk.text.splitlines()
        assert "\\JSONL_RECORD_BOUNDARY" not in chunk.text
        assert chunk.text == chunk.text.strip()
    assert all(chunk.metadata["chunk_record_boundary"] is True for chunk in chunks)
    assert all(chunk.metadata["chunk_strategy"] == "fixed_delimiter" for chunk in chunks)
    assert [chunk.index for chunk in chunks] == [0, 1, 2]


def test_fixed_delimiter_keeps_csv_record_metadata() -> None:
    chunks = _fixed(_csv_extraction("A", "B"))
    first = chunks[0].metadata
    assert first["structured_record_id"] == "row-000001"
    assert (first["row_start"], first["row_end"]) == (2, 3)
    assert first["csv_header_row"] == 1
    assert first["content_kind"] == "record"
    assert first["element_ids"] == "row-000001"
    assert chunks[0].metadata["chunk_group_id"] != chunks[1].metadata["chunk_group_id"]


def test_fixed_delimiter_keeps_excel_record_metadata() -> None:
    chunks = _fixed(_excel_extraction())
    assert [chunk.text for chunk in chunks] == [
        "名前: 設定 2 / 既定値［資料の既定値］: 20",
        "名前: 設定 3 / 既定値［資料の既定値］: 30",
    ]
    metadata = chunks[1].metadata
    assert metadata["sheet_name"] == "表 1"
    assert metadata["cell_range"] == "A3:B3"
    assert (metadata["row_start"], metadata["row_end"]) == (3, 3)
    assert metadata["sheet_column_roles"] == "既定値=default"
    assert metadata["structured_record_id"] == "sheet-000-r3"
    assert metadata["section_path"] == "表 1"


def _word_extraction() -> StructuredExtraction:
    """Word の JSONL(見出し・表を含むまとまり・大きい節を分けた 2 部分。合成)の解析の抽出。"""
    from rag_parser_core.structured_records import (
        PassageLine,
        RecordPart,
        SectionLine,
        WordLocation,
        WordParagraph,
        WordTable,
    )

    def location(start: int, end: int | None = None) -> WordLocation:
        return WordLocation(body_start=start, body_end=end or start)

    lines: list[Any] = [
        DocumentLine(
            record_id="document",
            source_type="docx",
            converter="word_to_jsonl",
            converter_version="v1",
            location_basis="original",
        ),
        SectionLine(
            record_id="s0001",
            source_type="docx",
            heading="1 概要",
            level=1,
            heading_path=["1 概要"],
            location=location(1),
        ),
        PassageLine(
            record_id="p0001",
            source_type="docx",
            parent_id="s0001",
            section_path=["1 概要"],
            location=WordLocation(body_start=2, body_end=4, table_indices=[1]),
            blocks=[
                WordParagraph(
                    text="値は次のとおり。\n\n改行を含む。", body_index=2, paragraph_index=2
                ),
                WordTable(
                    body_index=3,
                    table_index=1,
                    header_rows=1,
                    rows=[["名前", "値"], ["A", "1"]],
                    row_end=2,
                ),
                WordParagraph(text="注: 再起動の後に有効。", body_index=4, paragraph_index=3),
            ],
        ),
        *[
            PassageLine(
                record_id=f"p000{index + 1}",
                source_type="docx",
                parent_id="s0001",
                section_path=["1 概要"],
                location=location(5 + index),
                blocks=[
                    WordParagraph(
                        text=f"部分 {index}", body_index=5 + index, paragraph_index=4 + index
                    )
                ],
                part=RecordPart(
                    group_id="s0001-content", index=index, count=2, group_location=location(5, 6)
                ),
            )
            for index in (1, 2)
        ],
    ]
    result = run_external_adapter(BACKEND, dump_structured_records(lines), None, "")
    assert result.extraction is not None
    return StructuredExtraction.model_validate(result.extraction.model_dump())


def test_fixed_delimiter_keeps_word_records_and_heading_path() -> None:
    """Word の記録(前処理 word_to_jsonl)も 1 記録 1 chunk にし、見出しの階層と位置を引き継ぐ。"""
    chunks = _fixed(_word_extraction())
    assert [chunk.text.splitlines()[0] for chunk in chunks] == ["1 概要", "1 概要", "1 概要"]
    # 段落の中の \n\n(分割符の以前の既定値)では切らない。表と注記は同じ chunk。
    assert "値は次のとおり。\n\n改行を含む。" in chunks[0].text
    assert "| 名前 | 値 |" in chunks[0].text and chunks[0].text.endswith("注: 再起動の後に有効。")
    first = chunks[0].metadata
    assert first["section_path"] == "1 概要"
    assert first["record_kind"] == "passage"
    assert (first["word_body_start"], first["word_body_end"]) == (2, 4)
    assert first["word_table_indices"] == "1"
    assert first["word_location_basis"] == "original"
    assert first["parent_element_ids"] == "s0001"
    second = chunks[1].metadata
    assert (second["word_part_group"], second["word_part_index"], second["word_part_count"]) == (
        "s0001-content",
        1,
        2,
    )
    assert (second["word_part_body_start"], second["word_part_body_end"]) == (5, 6)
    for chunk in chunks:
        assert JSONL_RECORD_BOUNDARY not in chunk.text
        assert "page_start" not in chunk.metadata


def test_single_record_without_boundary_is_one_chunk() -> None:
    chunks = _fixed(_csv_extraction("一件"))
    assert [chunk.text for chunk in chunks] == ["項目: 値 1 / 備考: 一件"]


def test_boundary_count_mismatch_is_diagnosed() -> None:
    extraction = _csv_extraction("A", "B", "C")
    broken = extraction.model_copy(
        update={"raw_text": extraction.raw_text.replace(f"\n{JSONL_RECORD_BOUNDARY}\n", "\n", 1)}
    )
    with pytest.raises(RecordBoundaryError) as error:
        _fixed(broken)
    assert error.value.code == "boundary_count_mismatch"
    assert "boundaries=1 records=3" in str(error.value)


def test_record_larger_than_chunk_limit_is_diagnosed() -> None:
    extraction = _csv_extraction("A")
    huge = extraction.model_copy(update={"raw_text": "x" * 40_000})
    with pytest.raises(RecordBoundaryError) as error:
        _fixed(huge)
    assert error.value.code == "record_too_large"


@pytest.mark.parametrize(
    "strategy", [name for name in CHUNKING_STRATEGIES if name != "fixed_delimiter"]
)
def test_other_strategies_never_keep_the_markers(strategy: str) -> None:
    extraction = _csv_extraction("A", JSONL_RECORD_BOUNDARY, "C")
    chunks = chunk_extraction_with_strategy(
        extraction, strategy=strategy, chunk_size=200, overlap=0
    )
    text = "\n".join(chunk.text for chunk in chunks)
    assert text
    assert f"\n{JSONL_RECORD_BOUNDARY}\n" not in text
    assert "\\JSONL_RECORD_BOUNDARY" not in text
    # 原文の同じ文字列は本文として残る。
    assert f"備考: {JSONL_RECORD_BOUNDARY}" in text


def test_other_extractions_keep_the_configured_delimiter() -> None:
    extraction = StructuredExtraction(raw_text="一段落目\n\n二段落目")
    assert [chunk.text for chunk in _fixed(extraction)] == ["一段落目", "二段落目"]


# ---- 取込の経路（使用エンジンで選んだときだけ。#2457）---------------------------------------


def _settings(**updates: object) -> Any:
    from app.config import Settings

    base = Settings().model_copy(
        update={
            "rag_parser_adapter_backend": "docling",
            "rag_parser_docling_enabled": True,
            "rag_preprocess_profile": "csv_to_jsonl",
        }
    )
    return base.model_copy(update=updates)


def _pipeline(settings: Any) -> Any:
    from app.rag.ingestion import IngestionPipeline

    return IngestionPipeline(
        vlm=cast(Any, object()),
        genai=cast(Any, object()),
        oracle=cast(Any, object()),
        object_storage=cast(Any, object()),
        settings=settings,
    )


def _patch_runner(monkeypatch: pytest.MonkeyPatch, called: list[str]) -> None:
    def _runner(
        self: object,
        backend: str,
        source_bytes: bytes,
        source_profile: object,
        content_type: str,
        *,
        fail_fast: bool = False,
    ) -> ParserRegistryResult:
        called.append(backend)
        return run_external_adapter(backend, source_bytes, None, content_type)

    monkeypatch.setattr(
        "app.clients.parser_service.ParserServiceClient.runner", _runner, raising=True
    )


def _jsonl(*values: str) -> bytes:
    lines: list[DocumentLine | RowLine] = [
        DocumentLine(
            record_id="document",
            source_type="csv",
            converter="csv_to_jsonl",
            converter_version="v2",
        )
    ]
    lines.extend(
        RowLine(
            record_id=f"row-{index:06d}",
            source_type="csv",
            location=RecordLocation(row_start=index + 1, row_end=index + 1),
            fields=[RecordField(name="項目", position=1, value=value)],
        )
        for index, value in enumerate(values, start=1)
    )
    return dump_structured_records(lines)


def test_partition_source_uses_structured_records_service_when_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []
    _patch_runner(monkeypatch, called)
    pipeline = _pipeline(_settings(rag_parser_adapter_backend=BACKEND))
    result = pipeline._partition_source(
        parse_bytes=_jsonl("A", "B"),
        source_profile=None,
        content_type=STRUCTURED_RECORDS_CONTENT_TYPE,
    )
    assert called == [BACKEND]
    assert result.parser_backend == BACKEND
    assert result.extraction is not None
    assert result.extraction.raw_text == f"項目: A\n{JSONL_RECORD_BOUNDARY}\n項目: B"
    pipeline._raise_if_selected_parser_was_not_used(result)


def test_converted_legacy_excel_is_parsed_not_stopped_by_original_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧形式の .xls を Excel→JSONL で変換したら、派生物を原本の形式で止めない(#2470)。"""
    from app.rag.parser_source_guard import converted_source_profile
    from app.rag.source_profile import build_source_profile

    called: list[str] = []
    _patch_runner(monkeypatch, called)
    pipeline = _pipeline(_settings(rag_parser_adapter_backend=BACKEND))
    original = build_source_profile(
        original_file_name="old.xls",
        sanitized_file_name="old.xls",
        content_type="application/vnd.ms-excel",
        file_size_bytes=16,
        content_sha256="a" * 64,
    )
    assert original.unsupported_reason == "legacy_office_binary_not_supported"
    stopped = pipeline._partition_source(
        parse_bytes=_jsonl("A"),
        source_profile=original,
        content_type=STRUCTURED_RECORDS_CONTENT_TYPE,
    )
    assert stopped.unsupported_reason == "legacy_office_binary_not_supported"
    assert called == []
    result = pipeline._partition_source(
        parse_bytes=_jsonl("A", "B"),
        source_profile=converted_source_profile(original, converted=True),
        content_type=STRUCTURED_RECORDS_CONTENT_TYPE,
    )
    assert called == [BACKEND]
    assert result.unsupported_reason is None
    assert result.extraction is not None


def test_invalid_jsonl_with_structured_records_engine_stops_with_clear_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.rag.ingestion import IngestionUserError

    called: list[str] = []
    _patch_runner(monkeypatch, called)
    pipeline = _pipeline(_settings(rag_parser_adapter_backend=BACKEND))
    result = pipeline._partition_source(
        parse_bytes=b'{"items": [1]}\n',
        source_profile=None,
        content_type=STRUCTURED_RECORDS_CONTENT_TYPE,
    )
    with pytest.raises(IngestionUserError) as exc_info:
        pipeline._raise_if_selected_parser_was_not_used(result)
    message = str(exc_info.value)
    assert "構造化レコード JSONL" in message
    assert "structured_records_invalid:unknown_schema@line1" in message


def test_record_boundary_failure_message_names_the_reason() -> None:
    from app.rag.ingestion import record_boundary_failure_message

    message = record_boundary_failure_message(RecordBoundaryError("boundary_count_mismatch"))
    assert "記録の境界の数" in message
    assert "structured_records_boundary_invalid:boundary_count_mismatch" in message


def test_text_for_chunking_and_field_extraction_drop_the_markers() -> None:
    from app.rag.ingestion import _text_for_chunking

    extraction = _csv_extraction("A", "B")
    text = _text_for_chunking(extraction)
    assert JSONL_RECORD_BOUNDARY not in text
    assert text == "\n".join(element.text for element in extraction.elements)


def test_extraction_recipe_includes_structured_records_flag_only_for_its_engine() -> None:
    from app.rag.variant_keys import extraction_recipe_subset

    flag = "rag_parser_structured_records_jsonl_enabled"
    assert flag not in extraction_recipe_subset(_settings())
    subset = extraction_recipe_subset(_settings(rag_parser_adapter_backend=BACKEND, **{flag: True}))
    assert subset[flag] is True


# ---- 表の根拠の行・セルの座標（取込から根拠の locator まで。#3262）------------------------------

_RAG_DIR = Path(__file__).resolve().parents[2]


def _csv_to_jsonl_converter() -> ModuleType:
    """前処理のサービスの CSV の変換（別の venv で動く純 Python）を、ファイルの場所から読み込む。"""
    path = _RAG_DIR / "services" / "preprocess" / "csv_to_jsonl" / "app" / "converters.py"
    spec = importlib.util.spec_from_file_location("csv_to_jsonl_converters_3262", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _chunks_of(extraction: StructuredExtraction, strategy: str) -> list[Chunk]:
    if strategy == "small_to_big":
        from app.rag.chunking_small_to_big import build_parent_child_chunks

        return build_parent_child_chunks(extraction)
    return chunk_extraction_with_strategy(
        extraction, strategy=strategy, chunk_size=800, overlap=120, min_chars=120
    )


def _locators(chunks: list[Chunk]) -> dict[int, dict[str, Any]]:
    """記録の chunk の locator（開始の行 → locator）。"""
    from app.mcp.tools import _locator

    found = [_locator(dict(chunk.metadata)).model_dump() for chunk in chunks]
    return {locator["row_start"]: locator for locator in found if locator["row_basis"]}


@pytest.mark.parametrize("strategy", ["structure_aware", "small_to_big", "fixed_delimiter"])
def test_csv_rows_cite_lines_and_cells_from_ingest_to_locator(strategy: str) -> None:
    """CSV の前処理の変換から根拠の locator まで、記録の番号・物理行・セル・原本の表頭が保たれる。

    重複した表頭（列名は補う）・空の値・引用符の中の改行（複数の物理行）を含む合成の CSV。
    """
    source = '区分,値,値\n区分 A,10,\n"区分 B\n続き",20,30\n区分 C,30,40\n'.encode()
    outcome = _csv_to_jsonl_converter().convert(source, "text/csv", "csv_to_jsonl", None)
    assert outcome.converted is True
    extraction = run_external_adapter(BACKEND, outcome.derived_bytes, None, "").extraction
    assert extraction is not None
    chunks = _chunks_of(StructuredExtraction.model_validate(extraction.model_dump()), strategy)
    locators = _locators(chunks)
    # 行は記録の番号（RFC 7111 の行。表頭の記録が 1）。物理行は別に持つ。
    assert sorted(locators) == [2, 3, 4]
    first, multiline, last = locators[2], locators[3], locators[4]
    assert (first["row_basis"], first["row_start"], first["row_end"], first["table_row"]) == (
        "csv",
        2,
        2,
        2,
    )
    assert (first["source_line_start"], first["source_line_end"]) == (2, 2)
    # 空の値のセルは入らない。
    assert first["table_cells"] == [
        {"table_col": "A", "header_path": ["区分"], "bbox": None},
        {"table_col": "B", "header_path": ["値"], "bbox": None},
    ]
    # 引用符の中の改行を含む記録も 1 つの行（記録の番号 3）で、物理行は 3〜4。
    assert (multiline["row_start"], multiline["row_end"], multiline["table_row"]) == (3, 3, 3)
    assert (multiline["source_line_start"], multiline["source_line_end"]) == (3, 4)
    assert [cell["table_col"] for cell in multiline["table_cells"]] == ["A", "B", "C"]
    # 後ろの記録は、記録の番号（4）と物理行（5）がずれる。重複した表頭の列（列名は「値_1」）の
    # 表頭は、原本の文字のまま。
    assert (last["table_row"], last["source_line_start"]) == (4, 5)
    assert last["table_cells"][2] == {"table_col": "C", "header_path": ["値"], "bbox": None}
    for locator in locators.values():
        assert (locator["sheet_name"], locator["cell_range"], locator["table_index"]) == (
            None,
            None,
            None,
        )


def _word_table_extraction() -> StructuredExtraction:
    """Word の表（合成）: 表頭 2 段・単位の段・表の後の注記と、大きい表を分けた続きの部分。"""
    from rag_parser_core.structured_records import (
        PassageLine,
        SectionLine,
        WordLocation,
        WordParagraph,
        WordTable,
    )

    header = [["区分", "期間", "期間"], ["", "開始", "終了"], ["", "（日）", "（日）"]]
    lines: list[Any] = [
        DocumentLine(
            record_id="document",
            source_type="docx",
            converter="word_to_jsonl",
            converter_version="v1",
            location_basis="original",
        ),
        SectionLine(
            record_id="s0001",
            source_type="docx",
            heading="1 表",
            level=1,
            heading_path=["1 表"],
            location=WordLocation(body_start=1, body_end=1),
        ),
        PassageLine(
            record_id="p0001",
            source_type="docx",
            parent_id="s0001",
            section_path=["1 表"],
            location=WordLocation(body_start=2, body_end=3, table_indices=[2]),
            blocks=[
                WordTable(
                    body_index=2,
                    table_index=2,
                    header_rows=3,
                    rows=[*header, ["区分 A", "1", "10"], ["区分 B", "2", "20"]],
                    row_start=4,
                    row_end=5,
                ),
                WordParagraph(text="注: 合成の注記。", body_index=3, paragraph_index=2),
            ],
        ),
        # 続きの部分（表頭を繰り返す）。
        PassageLine(
            record_id="p0002",
            source_type="docx",
            parent_id="s0001",
            section_path=["1 表"],
            location=WordLocation(body_start=2, body_end=2, table_indices=[2]),
            blocks=[
                WordTable(
                    body_index=2,
                    table_index=2,
                    header_rows=3,
                    rows=[*header, ["区分 C", "3", "30"], ["区分 D", "4", "40"]],
                    row_start=6,
                    row_end=7,
                )
            ],
        ),
    ]
    result = run_external_adapter(BACKEND, dump_structured_records(lines), None, "")
    assert result.extraction is not None
    return StructuredExtraction.model_validate(result.extraction.model_dump())


@pytest.mark.parametrize("strategy", ["structure_aware", "small_to_big", "fixed_delimiter"])
def test_word_table_parts_cite_the_original_table_rows(strategy: str) -> None:
    """Word の表の根拠は、本文の表の番号と元の表の行の範囲を持ち、表として検査できる。

    多段の表頭・単位の段・注記を含む表と、表頭を繰り返した続きの部分（合成）。続きの部分も元の
    表の行の番号を持ち（行の番号を振り直さない）、本文は検査（表の行・列の対応）が読める表。
    """
    from app.mcp.tools import _locator, _table_binding

    chunks = _chunks_of(_word_table_extraction(), strategy)
    tables = [chunk for chunk in chunks if chunk.metadata.get("row_basis") == "word_table"]
    assert len(tables) == 2
    first, second = (_locator(dict(chunk.metadata)).model_dump() for chunk in tables)
    assert (first["row_basis"], first["table_index"], first["row_start"], first["row_end"]) == (
        "word_table",
        2,
        4,
        5,
    )
    assert (second["table_index"], second["row_start"], second["row_end"]) == (2, 6, 7)
    for chunk, locator in zip(tables, (first, second), strict=True):
        # 複数行のまとまりはセルの座標を持たない（行の範囲だけ）。
        assert (locator["table_row"], locator["table_cells"]) == (None, [])
        # 表頭の段（単位の段を含む）は列ごとに 1 つの見出しの行になり、本文は検査が読める表。
        assert "| 区分 | 期間 / 開始 / （日） | 期間 / 終了 / （日） |" in chunk.text
        assert _table_binding(chunk.text, dict(chunk.metadata)) == "checkable"
    # 注記は表の後ろに同じまとまりで残る。
    assert tables[0].text.endswith("注: 合成の注記。")
