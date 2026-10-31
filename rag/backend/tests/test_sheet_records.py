"""Excel の行の記録（前処理 excel_to_jsonl）を要素・chunk にし、場所を残す（#1221・#1349・#2457）。

前処理のサービス（openpyxl）は別の venv で動くため、ここでは前処理が作る行の記録
（`SheetRecordsDocument`）を直接作って構造化レコードの JSONL にし、parser の側（rag_parser_core）と
chunking（rag_pipeline_core）と前処理の契約を確かめる。
データはすべて合成。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from rag_parser_core.capabilities import adapter_supports_source
from rag_parser_core.extraction import StructuredExtraction
from rag_parser_core.preprocess import ConvertHealth, ConvertOutcome, ConvertResponse
from rag_parser_core.preprocess_service import create_preprocess_app
from rag_parser_core.registry import run_external_adapter
from rag_parser_core.sheet_records import (
    TABLE_CELLS_KEY,
    TABLE_ROW_KEY,
    SheetBlock,
    SheetColumn,
    SheetPreambleRow,
    SheetRecords,
    SheetRecordsDocument,
)
from rag_parser_core.source import SourceProfile
from rag_parser_core.structured_records import (
    STRUCTURED_RECORDS_CONTENT_TYPE,
    dump_structured_records,
    sheet_records_to_lines,
)

from app.rag.chunking import chunk_extraction_with_strategy
from app.rag.source_profile import build_source_profile

RAG_DIR = Path(__file__).resolve().parents[2]


def _xlsx_profile() -> SourceProfile:
    return build_source_profile(
        original_file_name="経費コード.xlsx",
        sanitized_file_name="経費コード.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        file_size_bytes=16,
        content_sha256="a" * 64,
    )


def _jsonl(document: SheetRecordsDocument) -> bytes:
    return dump_structured_records(
        sheet_records_to_lines(document, converter="excel_to_jsonl", converter_version="v4")
    )


def _document() -> SheetRecordsDocument:
    rows = [
        SheetBlock(
            kind="row",
            row_start=row,
            row_end=row,
            cell_range=f"A{row}:C{row}",
            values={"コード": f"A0{row}", "名称": f"費目{row}", "区分": "旅費"},
        )
        for row in (3, 4, 6)
    ]
    return SheetRecordsDocument(
        source_format="xlsx",
        sheets=[
            SheetRecords(
                name="コード表",
                header_row=2,
                mode="table",
                preamble=[
                    SheetPreambleRow(row_number=1, cell_range="A1:A1", text="費目のコードの一覧")
                ],
                blocks=rows,
            ),
            SheetRecords(
                name="手順",
                header_row=1,
                mode="procedure",
                blocks=[
                    SheetBlock(
                        kind="procedure_step",
                        row_start=3,
                        row_end=4,
                        cell_range="A3:D4",
                        section_path=["事前準備", "アカウントの登録"],
                        lines=[
                            "セクション: 事前準備",
                            "作業項目: アカウントの登録",
                            "作業内容: 権限",
                        ],
                    )
                ],
            ),
        ],
    )


def test_only_structured_records_jsonl_reads_sheet_records() -> None:
    """行の記録は構造化レコード JSONL だけが要素にする（Docling・Unstructured は診断で止める）。"""
    profile = _xlsx_profile()
    assert adapter_supports_source("docling", source_profile=profile, content_type="") is False
    for backend in ("docling", "unstructured", "xml_tree_json"):
        assert not adapter_supports_source(
            backend, source_profile=profile, content_type=f"{STRUCTURED_RECORDS_CONTENT_TYPE}; x=1"
        )
        stopped = run_external_adapter(
            backend, _jsonl(_document()), profile, STRUCTURED_RECORDS_CONTENT_TYPE
        )
        assert stopped.extraction is None
    assert adapter_supports_source(
        "structured_records_jsonl",
        source_profile=profile,
        content_type=f"{STRUCTURED_RECORDS_CONTENT_TYPE}; x=1",
    )

    result = run_external_adapter(
        "structured_records_jsonl", _jsonl(_document()), profile, STRUCTURED_RECORDS_CONTENT_TYPE
    )

    assert result.extraction is not None
    assert result.parser_version == "structured_records_jsonl_v1"
    elements = result.extraction.elements
    # シート名だけの見出しの要素は作らない（節と文脈の見出しに入る）。
    assert all(element.kind == "text" for element in elements)
    assert all(element.metadata.get("sheet_name") for element in elements)
    record = next(element for element in elements if element.metadata.get("row_start") == 3)
    assert record.text == "コード: A03 / 名称: 費目3 / 区分: 旅費"
    assert record.section_path == ["コード表"]
    assert record.content_kind == "record"
    assert record.metadata["cell_range"] == "A3:C3"
    assert (record.metadata["cell_column_start"], record.metadata["cell_column_end"]) == ("A", "C")
    step = next(
        element
        for element in elements
        if element.section_path[:1] == ["手順"] and element.kind == "text"
    )
    assert step.section_path == ["手順", "事前準備", "アカウントの登録"]
    assert step.text.splitlines()[0] == "セクション: 事前準備"
    assert (step.metadata["row_start"], step.metadata["row_end"]) == (3, 4)

    broken = run_external_adapter(
        "structured_records_jsonl", b"{}", profile, STRUCTURED_RECORDS_CONTENT_TYPE
    )
    assert broken.extraction is None
    assert "structured_records_invalid:unknown_schema@line1" in broken.warnings


def _extraction(document: SheetRecordsDocument | None = None) -> StructuredExtraction:
    result = run_external_adapter(
        "structured_records_jsonl",
        _jsonl(document or _document()),
        _xlsx_profile(),
        STRUCTURED_RECORDS_CONTENT_TYPE,
    )
    assert result.extraction is not None
    return result.extraction


def test_chunks_carry_sheet_rows_and_cell_ranges() -> None:
    """1 行（1 記録）を 1 chunk にし、行・前書きと結合しない（#1349）。"""
    chunks = chunk_extraction_with_strategy(
        _extraction(), strategy="structure_aware", chunk_size=800, overlap=120, min_chars=120
    )

    rows = [chunk for chunk in chunks if chunk.text.startswith("コード: ")]
    # 行ごとに 1 chunk。場所はその行（空行の 5 行目は記録にならず、元の行番号のまま）。
    assert [chunk.text for chunk in rows] == [
        "コード: A03 / 名称: 費目3 / 区分: 旅費",
        "コード: A04 / 名称: 費目4 / 区分: 旅費",
        "コード: A06 / 名称: 費目6 / 区分: 旅費",
    ]
    assert [
        (meta["sheet_name"], meta["row_start"], meta["row_end"], meta["cell_range"])
        for meta in (chunk.metadata for chunk in rows)
    ] == [("コード表", 3, 3, "A3:C3"), ("コード表", 4, 4, "A4:C4"), ("コード表", 6, 6, "A6:C6")]
    assert all(chunk.metadata["content_kind"] == "record" for chunk in rows)
    # 行ごとに別の group にし、兄弟として他の行を文脈に足さない（min_chars の吸収もしない）。
    assert len({chunk.metadata["chunk_group_id"] for chunk in rows}) == len(rows)
    assert {chunk.metadata["chunk_group_kind"] for chunk in rows} == {"sheet_record"}
    # 表頭より上の説明は本文の chunk になり、行と混ざらない。シート名だけの chunk は作らない。
    preamble = next(chunk for chunk in chunks if "費目のコードの一覧" in chunk.text)
    assert preamble.text == "費目のコードの一覧"
    assert (preamble.metadata["row_start"], preamble.metadata["cell_range"]) == (1, "A1:A1")
    assert all(chunk.metadata.get("sheet_name") for chunk in chunks)
    step = next(chunk.metadata for chunk in chunks if chunk.text.startswith("セクション: "))
    assert step["sheet_name"] == "手順"
    assert (step["row_start"], step["row_end"], step["cell_range"]) == (3, 4, "A3:D4")
    assert step["section_path"] == "手順 > 事前準備 > アカウントの登録"
    assert len(chunks) == 5


def _table_document(*, header_paths: bool = True) -> SheetRecordsDocument:
    """2 段の表頭の表（合成）。``header_paths=False`` は表頭の段を記録していない古い記録。"""
    paths: dict[str, list[str] | None] = {
        "A": ["項目A"],
        "B": ["区分 / 甲", "値A"],
        "C": ["区分 / 甲", "値B"],
    }
    names = {"A": "項目A", "B": "区分 / 甲 / 値A", "C": "区分 / 甲 / 値B"}
    columns = [
        SheetColumn(
            name=names[letter], column=letter, header_path=paths[letter] if header_paths else None
        )
        for letter in "ABC"
    ]
    rows = [
        SheetBlock(
            kind="row",
            row_start=row,
            row_end=row,
            cell_range=f"A{row}:C{row}",
            # 空の値のセルは本文にもセルにも入らない。
            values={
                names["A"]: f"行{row}",
                names["B"]: str(row),
                names["C"]: "" if row == 4 else "x",
            },
        )
        for row in (3, 4)
    ]
    return SheetRecordsDocument(
        source_format="xlsx",
        sheets=[
            SheetRecords(
                name="表A",
                header_row=1,
                header_row_count=2,
                mode="table",
                columns=columns,
                preamble=[],
                blocks=rows,
            ),
            _document().sheets[1],
        ],
    )


def _table_chunks(strategy: str, document: SheetRecordsDocument) -> list[Any]:
    extraction = _extraction(document)
    if strategy == "small_to_big":
        from app.rag.chunking_small_to_big import build_parent_child_chunks

        return build_parent_child_chunks(extraction)
    return chunk_extraction_with_strategy(
        extraction, strategy=strategy, chunk_size=800, overlap=120, min_chars=120
    )


@pytest.mark.parametrize("strategy", ["structure_aware", "small_to_big", "fixed_delimiter"])
def test_table_row_chunks_carry_cell_coordinates_and_header_rows(strategy: str) -> None:
    """表の 1 行の chunk は、行とセルの列・表頭の段を持つ（どの分割でも。#3262）。"""
    chunks = _table_chunks(strategy, _table_document())
    rows = [chunk.metadata for chunk in chunks if chunk.metadata.get("sheet_name") == "表A"]
    assert [meta[TABLE_ROW_KEY] for meta in rows] == [3, 4]
    assert [json.loads(meta[TABLE_CELLS_KEY]) for meta in rows] == [
        [["A", ["項目A"]], ["B", ["区分 / 甲", "値A"]], ["C", ["区分 / 甲", "値B"]]],
        [["A", ["項目A"]], ["B", ["区分 / 甲", "値A"]]],
    ]
    # 手順（複数行の記録）は表の 1 行ではない。
    step = next(chunk.metadata for chunk in chunks if chunk.metadata.get("sheet_name") == "手順")
    assert TABLE_ROW_KEY not in step and TABLE_CELLS_KEY not in step


def test_table_row_chunks_of_old_records_have_no_cells() -> None:
    """表頭の段を記録していない記録は、行の番号だけを持ち、セルの表頭を推測しない（#3262）。"""
    chunks = _table_chunks("structure_aware", _table_document(header_paths=False))
    rows = [chunk.metadata for chunk in chunks if chunk.metadata.get("sheet_name") == "表A"]
    assert [meta[TABLE_ROW_KEY] for meta in rows] == [3, 4]
    assert all(TABLE_CELLS_KEY not in meta for meta in rows)


def test_long_record_stays_in_one_chunk() -> None:
    """chunk_size を超える 1 行も途中で切らない（構造を壊さない理由つきの超過）。"""
    long_value = "長い説明。" * 80
    document = SheetRecordsDocument(
        source_format="xlsx",
        sheets=[
            SheetRecords(
                name="説明",
                header_row=1,
                blocks=[
                    SheetBlock(
                        kind="row",
                        row_start=2,
                        row_end=2,
                        cell_range="A2:B2",
                        values={"項目": "保管", "説明": long_value},
                    )
                ],
            )
        ],
    )
    chunks = chunk_extraction_with_strategy(
        _extraction(document), strategy="structure_aware", chunk_size=200, overlap=20
    )
    assert len(chunks) == 1
    assert chunks[0].text == f"項目: 保管 / 説明: {long_value}"
    assert chunks[0].metadata["chunk_size_compliance"] == "overflow_justified"
    assert chunks[0].metadata["chunk_size_overflow_reason"] == "atomic_block"


def test_small_to_big_uses_rows_as_children_and_table_parts_as_parents() -> None:
    """親子階層は行を子、同じシートの続く行（表の一部）を親にする。縮退しない（#1349）。"""
    from app.rag.chunking_small_to_big import (
        PARENT_SOURCE_SHEET_RECORDS,
        build_parent_child_chunks,
        small_to_big_parent_source,
    )
    from app.rag.chunking_strategy import SmallToBigParams

    rows = [
        SheetBlock(
            kind="row",
            row_start=row,
            row_end=row,
            cell_range=f"A{row}:B{row}",
            values={"コード": f"C{row}", "名称": f"品目{row}"},
        )
        for row in range(3, 8)
    ]
    document = SheetRecordsDocument(
        source_format="xlsx",
        sheets=[
            SheetRecords(
                name="品目",
                header_row=2,
                preamble=[SheetPreambleRow(row_number=1, cell_range="A1:A1", text="品目の一覧")],
                blocks=rows,
            ),
            SheetRecords(
                name="別表",
                header_row=1,
                blocks=[
                    SheetBlock(
                        kind="row",
                        row_start=2,
                        row_end=2,
                        cell_range="A2:B2",
                        values={"コード": "Z1", "名称": "別品目"},
                    )
                ],
            ),
        ],
    )
    extraction = _extraction(document)
    assert small_to_big_parent_source(extraction) == PARENT_SOURCE_SHEET_RECORDS

    chunks = build_parent_child_chunks(
        extraction, params=SmallToBigParams(parent_target_chars=6000, parent_max_children=3)
    )

    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.metadata["chunk_strategy"] == "small_to_big" for chunk in chunks)
    assert all("chunk_strategy_fallback_reason" not in chunk.metadata for chunk in chunks)
    # 子は 1 行 1 chunk（前書き 1・品目の 5 行・別表の 1 行）。
    assert [chunk.metadata["cell_range"] for chunk in chunks] == [
        "A1:A1",
        "A3:B3",
        "A4:B4",
        "A5:B5",
        "A6:B6",
        "A7:B7",
        "A2:B2",
    ]
    groups: dict[str, list[Any]] = {}
    for chunk in chunks:
        groups.setdefault(str(chunk.metadata["chunk_group_id"]), []).append(chunk)
    # 親は前書き（自分だけ）・品目の 3 行（件数の上限）・残りの 2 行・別表（シートをまたがない）。
    assert [len(members) for members in groups.values()] == [1, 3, 2, 1]
    preamble, first, second, other = groups.values()
    assert preamble[0].metadata["parent_text"] == "品目の一覧"
    assert first[0].metadata["parent_text"] == "\n".join(chunk.text for chunk in first)
    assert [chunk.metadata["chunk_part_index"] for chunk in first] == [1, 2, 3]
    assert {chunk.metadata["chunk_part_count"] for chunk in first} == {3}
    assert {chunk.metadata["chunk_group_kind"] for chunk in chunks} == {"small_to_big_parent"}
    assert {
        (
            chunk.metadata["parent_row_start"],
            chunk.metadata["parent_row_end"],
            chunk.metadata["parent_cell_range"],
        )
        for chunk in second
    } == {(6, 7, "A6:B7")}
    assert other[0].metadata["sheet_name"] == "別表"
    assert other[0].metadata["parent_text"] == "コード: Z1 / 名称: 別品目"

    # 親の本文の上限（文字数）でも分ける。
    small = build_parent_child_chunks(
        extraction, params=SmallToBigParams(parent_target_chars=40, parent_max_children=12)
    )
    sizes = [
        sum(1 for chunk in small if chunk.metadata["chunk_group_id"] == group_id)
        for group_id in dict.fromkeys(chunk.metadata["chunk_group_id"] for chunk in small)
    ]
    assert sizes == [1, 2, 2, 1, 1]
    assert all(len(str(chunk.metadata["parent_text"])) <= 40 for chunk in small)


def test_small_to_big_groups_procedure_steps_by_chapter() -> None:
    """手順書は同じ章の手順だけを 1 つの親にする。"""
    from app.rag.chunking_small_to_big import build_parent_child_chunks

    def step(row: int, chapter: str, title: str) -> SheetBlock:
        return SheetBlock(
            kind="procedure_step",
            row_start=row,
            row_end=row,
            cell_range=f"A{row}:C{row}",
            section_path=[chapter, title],
            lines=[f"作業項目: {title}"],
        )

    document = SheetRecordsDocument(
        source_format="xlsx",
        sheets=[
            SheetRecords(
                name="手順",
                header_row=1,
                mode="procedure",
                blocks=[
                    step(2, "準備", "登録"),
                    step(3, "準備", "確認"),
                    step(4, "実施", "起動"),
                ],
            )
        ],
    )
    chunks = build_parent_child_chunks(_extraction(document))
    assert [chunk.metadata["chunk_part_count"] for chunk in chunks] == [2, 2, 1]
    assert chunks[0].metadata["chunk_group_id"] == chunks[1].metadata["chunk_group_id"]
    assert chunks[2].metadata["chunk_group_id"] != chunks[1].metadata["chunk_group_id"]


def test_small_to_big_does_not_join_split_tables_of_one_sheet() -> None:
    """1 シートを複数の表に分けたとき、親は表をまたがない（#2516）。"""
    from app.rag.chunking_small_to_big import build_parent_child_chunks

    def row(number: int) -> SheetBlock:
        return SheetBlock(
            kind="row",
            row_start=number,
            row_end=number,
            cell_range=f"A{number}:B{number}",
            values={"列A": f"a{number}", "列B": str(number)},
        )

    document = SheetRecordsDocument(
        source_format="xlsx",
        sheets=[
            SheetRecords(
                name="表",
                header_row=1,
                table_index=1,
                region="A1:B3",
                blocks=[row(2), row(3)],
            ),
            SheetRecords(
                name="表",
                header_row=6,
                table_index=2,
                region="A6:B8",
                blocks=[row(7), row(8)],
            ),
        ],
    )
    chunks = build_parent_child_chunks(_extraction(document))
    assert [chunk.metadata["sheet_table_index"] for chunk in chunks] == [1, 1, 2, 2]
    groups = [chunk.metadata["chunk_group_id"] for chunk in chunks]
    assert groups[0] == groups[1]
    assert groups[2] == groups[3]
    assert groups[1] != groups[2]


def _excel_to_jsonl_converter() -> ModuleType:
    """前処理のサービスの変換（別の venv で動く）を、ファイルの場所から読み込む。"""
    pytest.importorskip("openpyxl")
    package_dir = RAG_DIR / "services" / "preprocess" / "excel_to_jsonl" / "app"
    # 変換は同じ package の module（grid・layout。#2516）を相対で読むので、package として読み込む。
    name = "excel_to_jsonl_app_1349"
    spec = importlib.util.spec_from_file_location(
        name, package_dir / "__init__.py", submodule_search_locations=[str(package_dir)]
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    # dataclass の定義が module を引くため、読み込みの間だけ登録する。
    sys.modules[name] = package
    try:
        spec.loader.exec_module(package)
        module = importlib.import_module(f"{name}.converters")
    finally:
        for key in [key for key in sys.modules if key == name or key.startswith(f"{name}.")]:
            sys.modules.pop(key, None)
    return module


@pytest.mark.parametrize("strategy", ["structure_aware", "small_to_big"])
def test_system_ledger_becomes_row_chunks_and_preamble(strategy: str) -> None:
    """評価セットの台帳は前書きと、1 行ずつの chunk になり、行は列名と値を持つ（#1349）。

    行の数は台帳の原稿（`sources/system-ledger.workbook.json`。#1352 で 80 行）から決める。
    """
    from rag_parser_core.structured_records import (
        StructuredRecordsDocument,
        parse_structured_records,
        structured_records_extraction,
    )

    from app.rag.chunking_small_to_big import build_parent_child_chunks

    converter = _excel_to_jsonl_converter()
    source = RAG_DIR / "evaluation" / "multi-hop" / "system-ledger.xlsx"
    outcome = converter.convert(
        source.read_bytes(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "excel_to_jsonl",
        None,
    )
    assert outcome.converted is True
    document = parse_structured_records(outcome.derived_bytes)
    assert isinstance(document, StructuredRecordsDocument)
    extraction = structured_records_extraction(
        document, source_parser="structured_records", parser_backend="docling", parser_version="v1"
    )
    if strategy == "small_to_big":
        chunks = build_parent_child_chunks(extraction)
    else:
        chunks = chunk_extraction_with_strategy(
            extraction, strategy=strategy, chunk_size=800, overlap=120, min_chars=120
        )

    workbook = json.loads(
        (source.parent / "sources" / "system-ledger.workbook.json").read_text(encoding="utf-8")
    )
    ledger_ids = [row[0] for row in workbook["sheets"][0]["rows"]]
    # 前書きの行（#1406 で重要度の順位を足して 3 行）→ 表頭 → 台帳の行。
    preamble_rows = len(workbook["sheets"][0]["preamble"])
    first_row = preamble_rows + 2
    last_row = preamble_rows + 1 + len(ledger_ids)
    assert len(chunks) == 1 + len(ledger_ids)
    preamble, *rows = chunks
    assert preamble.metadata["content_kind"] == "text"
    assert preamble.metadata["cell_range"] == f"A1:A{preamble_rows}"
    assert preamble.text.startswith("サンプル社のシステム台帳")
    # 重要度の順位は前書きの chunk に入る（重要度を比べる問の根拠。#1406）。
    assert "A が最も高く、B、C の順に低くなります" in preamble.text
    assert "SYS-" not in preamble.text
    columns = ["システムID", "正式名", "略称・別表記", "担当部署", "重要度", "機密区分"]
    assert [chunk.metadata["cell_range"] for chunk in rows] == [
        f"A{row}:F{row}" for row in range(first_row, last_row + 1)
    ]
    for system_id, chunk in zip(ledger_ids, rows, strict=True):
        assert chunk.metadata["content_kind"] == "record"
        assert chunk.metadata["sheet_name"] == "システム台帳"
        assert chunk.metadata["section_path"] == "システム台帳"
        assert chunk.text.startswith(f"システムID: {system_id} / 正式名: ")
        assert [part.split(": ", 1)[0] for part in chunk.text.split(" / ")] == columns
        # 他の行のシステム ID は入らない（1 行の根拠を引用で数えられる）。
        assert re.findall(r"SYS-\d+", chunk.text) == [system_id]
    assert rows[2].text == (
        "システムID: SYS-103 / 正式名: 人事評価システム / 略称・別表記: HRM / 担当部署: 人"
        " / 重要度: A / 機密区分: 極秘"
    )
    if strategy == "small_to_big":
        # 行は表の連続した一部（親）の子。親の範囲は行の範囲をつないだもので、親の本文は子の本文。
        # 前書きは自分だけの親。
        groups: dict[str, list[Any]] = {}
        for chunk in rows:
            groups.setdefault(str(chunk.metadata["chunk_group_id"]), []).append(chunk)
        for children in groups.values():
            first = children[0].metadata["cell_range"].split(":")[0]
            last = children[-1].metadata["cell_range"].split(":")[1]
            assert {child.metadata["parent_cell_range"] for child in children} == {
                f"{first}:{last}"
            }
            assert children[0].metadata["parent_text"] == "\n".join(
                child.text for child in children
            )
        assert [str(chunk.metadata["chunk_group_id"]) for chunk in rows] == [
            group_id for group_id, children in groups.items() for _ in children
        ]
        assert str(preamble.metadata["chunk_group_id"]) not in groups
    else:
        assert len({chunk.metadata["chunk_group_id"] for chunk in chunks}) == len(chunks)


def test_excel_with_a_two_row_header_cites_cells_and_header_rows() -> None:
    """前処理から根拠の locator まで、2 段の表頭の表の行を行・列・表頭の段で引用できる（#3262）。

    合成の表を前処理の変換（openpyxl）で JSONL にし、要素化・分割・locator の順に通す。
    """
    import io

    import openpyxl  # type: ignore[import-untyped]
    from rag_parser_core.structured_records import (
        StructuredRecordsDocument,
        parse_structured_records,
        structured_records_extraction,
    )

    from app.mcp.tools import _locator

    converter = _excel_to_jsonl_converter()
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    assert worksheet is not None
    worksheet.title = "表A"
    worksheet["A1"] = "項目A"
    worksheet.merge_cells("A1:A2")
    worksheet["B1"] = "区分甲"
    worksheet.merge_cells("B1:C1")
    worksheet["B2"], worksheet["C2"] = "値A", "値B"
    for row in range(3, 7):
        worksheet.append([f"行{row}", row, row * 10])
    buffer = io.BytesIO()
    workbook.save(buffer)
    outcome = converter.convert(
        buffer.getvalue(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "excel_to_jsonl",
        None,
        options={"header_row": 1, "header_row_count": 2},
    )
    assert outcome.converted is True
    document = parse_structured_records(outcome.derived_bytes)
    assert isinstance(document, StructuredRecordsDocument)
    extraction = structured_records_extraction(
        document, source_parser="structured_records", parser_backend="docling", parser_version="v1"
    )
    chunks = chunk_extraction_with_strategy(
        extraction, strategy="structure_aware", chunk_size=800, overlap=120, min_chars=120
    )
    first = next(chunk for chunk in chunks if chunk.metadata.get("row_start") == 3)
    locator = _locator(dict(first.metadata)).model_dump()
    assert (locator["sheet_name"], locator["table_row"], locator["cell_range"]) == (
        "表A",
        3,
        "A3:C3",
    )
    assert locator["table_cells"] == [
        {"table_col": "A", "header_path": ["項目A"], "bbox": None},
        {"table_col": "B", "header_path": ["区分甲", "値A"], "bbox": None},
        {"table_col": "C", "header_path": ["区分甲", "値B"], "bbox": None},
    ]


def test_preprocess_service_passes_options_only_to_converters_that_take_them() -> None:
    received: dict[str, Any] = {}

    def with_options(
        source_bytes: bytes,
        content_type: str,
        preprocess_profile: str,
        source_profile: SourceProfile | None,
        *,
        options: dict[str, Any] | None = None,
    ) -> ConvertOutcome:
        received["options"] = options
        return ConvertOutcome(
            converted=True,
            converter_name="stub",
            converter_version="v1",
            derived_bytes=b"{}",
            derived_content_type=STRUCTURED_RECORDS_CONTENT_TYPE,
        )

    def without_options(
        source_bytes: bytes,
        content_type: str,
        preprocess_profile: str,
        source_profile: SourceProfile | None,
    ) -> ConvertOutcome:
        return ConvertOutcome.passthrough(reason="stub")

    def health() -> ConvertHealth:
        return ConvertHealth(status="ok", supported_profiles=["excel_to_jsonl"])

    payload = {
        "content_type": "application/vnd.ms-excel",
        "preprocess_profile": "excel_to_jsonl",
        "options": json.dumps({"header_row": 2, "mode": "table"}),
    }
    files = {"file": ("a.xlsx", b"PK", "application/octet-stream")}
    client = TestClient(create_preprocess_app(converter=with_options, health_probe=health))
    response = client.post("/convert", files=files, data=payload)
    assert response.status_code == 200
    assert ConvertResponse.model_validate(response.json()).converted is True
    assert received["options"] == {"header_row": 2, "mode": "table"}

    # 選択肢の無い前処理（office_to_pdf など）は options を受け取らない。
    other = TestClient(create_preprocess_app(converter=without_options, health_probe=health))
    assert other.post("/convert", files=files, data=payload).status_code == 200


def test_recipe_excel_options_reach_the_preprocess_service(monkeypatch: Any) -> None:
    """文書レシピの excel_options が設定に解決され、前処理のサービスへ JSON で渡る。"""
    import httpx

    from app.clients.preprocess_service import PreprocessServiceClient
    from app.config import Settings
    from app.rag.kb_adapter_config import KnowledgeBaseAdapterConfig
    from app.rag.preprocess_strategy import preprocess_options
    from app.rag.variant_keys import compute_extraction_recipe_id
    from app.schemas.document import DocumentProcessingConfig

    recipe = DocumentProcessingConfig.model_validate(
        {
            "preprocess_profile": "excel_to_jsonl",
            "excel_options": {"mode": "table", "header_row": 2},
        }
    )
    config = KnowledgeBaseAdapterConfig(ingestion=recipe)
    base = Settings(_env_file=None, rag_preprocess_enabled=True)
    settings = base.model_copy(update=config.settings_overrides("ingestion"))
    options = preprocess_options(settings, "excel_to_jsonl")
    assert options is not None
    assert (options["mode"], options["header_row"]) == ("table", 2)
    assert preprocess_options(settings, "office_to_pdf") is None

    # excel_to_jsonl のときだけ、選択肢が抽出の ID を変える。
    other = base.model_copy(
        update={"rag_preprocess_profile": "excel_to_jsonl", "rag_preprocess_excel_options": {}}
    )
    assert compute_extraction_recipe_id("s" * 64, settings) != compute_extraction_recipe_id(
        "s" * 64, other
    )
    pdf = base.model_copy(update={"rag_preprocess_profile": "office_to_pdf"})
    assert compute_extraction_recipe_id("s" * 64, pdf) == compute_extraction_recipe_id(
        "s" * 64, pdf.model_copy(update={"rag_preprocess_excel_options": {"mode": "table"}})
    )

    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8", errors="replace")
        sent["has_options"] = 'name="options"' in body and '"header_row": 2' in body
        return httpx.Response(
            200,
            json={
                "converted": True,
                "converter_name": "excel_to_jsonl",
                "converter_version": "v2",
                "derived_content_base64": "e30=",
                "derived_content_type": STRUCTURED_RECORDS_CONTENT_TYPE,
            },
        )

    transport = httpx.MockTransport(handler)
    original = httpx.Client.__init__

    def patched(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched)
    PreprocessServiceClient(settings).convert(
        b"PK", content_type="application/octet-stream", profile="excel_to_jsonl", options=options
    )
    assert sent["has_options"] is True

    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DocumentProcessingConfig.model_validate({"excel_options": {"header_row": 0}})
    with pytest.raises(ValidationError):
        DocumentProcessingConfig.model_validate({"excel_options": {"unknown": 1}})


def test_answer_records_carry_sheet_location_for_citations() -> None:
    """回答フローへ渡す chunk に表計算の場所を入れ、親はシートの範囲をまとめる（#1224）。"""
    from app.rag.answer_engine import _merged_sheet_location, _sheet_location

    assert _sheet_location(
        {"sheet_name": "費目コード", "row_start": 3, "row_end": 6, "cell_range": "A3:D6"}
    ) == {"sheet_name": "費目コード", "row_start": 3, "row_end": 6, "cell_range": "A3:D6"}
    assert _sheet_location({"page_start": 2}) is None
    merged = _merged_sheet_location(
        [
            {"sheet_name": "費目コード", "cell_range": "B3:D4"},
            {"sheet_name": "費目コード", "cell_range": "A6:C9"},
        ]
    )
    assert merged == {
        "sheet_name": "費目コード",
        "row_start": 3,
        "row_end": 9,
        "cell_range": "A3:D9",
    }
    # 別のシートが混ざる親は場所をまとめない（頁と同じく、確かな場所だけを出す）。
    assert _merged_sheet_location([{"sheet_name": "a"}, {"sheet_name": "b"}]) is None


def test_answer_parent_of_sheet_rows_uses_the_table_part_location() -> None:
    """行の記録の親（表の一部）は、見つかった子ではなく親の本文の範囲を場所にする（#1349）。"""
    from app.rag.answer_engine import _parent_sheet_location, _stored_child, _stored_parents
    from app.schemas.search import RetrievedChunk

    assert _parent_sheet_location(
        {
            "sheet_name": "台帳",
            "parent_row_start": 4,
            "parent_row_end": 11,
            "parent_cell_range": "A4:F11",
        }
    ) == {"sheet_name": "台帳", "row_start": 4, "row_end": 11, "cell_range": "A4:F11"}
    assert _parent_sheet_location({"sheet_name": "台帳", "parent_cell_range": "A4"}) is None

    chunk = RetrievedChunk(
        document_id="doc-1",
        chunk_id="doc-1:cs:5",
        text="システムID: SYS-105",
        score=1.0,
        metadata={
            "sheet_name": "台帳",
            "row_start": 8,
            "row_end": 8,
            "cell_range": "A8:F8",
            "chunk_group_id": "g1",
            "parent_text": "システムID: SYS-104\nシステムID: SYS-105",
            "chunk_strategy": "small_to_big",
            "parent_row_start": 7,
            "parent_row_end": 8,
            "parent_cell_range": "A7:F8",
        },
    )

    class _State:
        chunks = {chunk.chunk_id: chunk}

    child = _stored_child(chunk, rrf_score=1.0)
    assert child.metadata["sheet_location"]["cell_range"] == "A8:F8"
    (parent,) = _stored_parents([child], cast(Any, _State()))
    assert parent.text == "システムID: SYS-104\nシステムID: SYS-105"
    assert parent.metadata["sheet_location"] == {
        "sheet_name": "台帳",
        "row_start": 7,
        "row_end": 8,
        "cell_range": "A7:F8",
    }


def test_low_confidence_header_becomes_extraction_warning_and_ranges_are_validated() -> None:
    """表頭の推定の信頼度が低いシートは抽出の warning になり、範囲は保存時に検証する（#1229）。"""
    import pytest
    from pydantic import ValidationError
    from rag_parser_core.sheet_records import HeaderDetection, SheetDiagnostic

    from app.schemas.document import DocumentProcessingConfig

    document = _document()
    document.sheets[0].header_detection = HeaderDetection(
        method="detected", confidence=0.4, reason="数値の行"
    )
    document.sheets[0].diagnostics = [SheetDiagnostic(code="header_low_confidence", detail="0.40")]
    result = run_external_adapter(
        "structured_records_jsonl",
        _jsonl(document),
        _xlsx_profile(),
        STRUCTURED_RECORDS_CONTENT_TYPE,
    )
    assert result.extraction is not None
    assert result.extraction.warnings == ["excel_header_low_confidence:コード表"]

    recipe = DocumentProcessingConfig.model_validate(
        {"excel_options": {"ranges": [" 手順!A1:D80 ", "A3:F200", ""]}}
    )
    assert recipe.excel_options is not None
    assert recipe.excel_options.ranges == ["手順!A1:D80", "A3:F200"]
    with pytest.raises(ValidationError):
        DocumentProcessingConfig.model_validate({"excel_options": {"ranges": ["A1"]}})


def test_column_roles_label_values_in_chunks_and_match_the_answer_prompt() -> None:
    """列の役割（#1281）は値の列の本文に表示され、回答の生成の規則と同じ表示を使う。"""
    import pytest
    from pydantic import ValidationError
    from rag_engine.generation.grounded import GENERATE_SYSTEM_PROMPT
    from rag_parser_core.sheet_records import COLUMN_ROLE_TEXT_LABELS, SheetColumn

    from app.schemas.document import DocumentProcessingConfig

    document = SheetRecordsDocument(
        source_format="xlsx",
        sheets=[
            SheetRecords(
                name="パラメータ一覧",
                header_row=3,
                columns=[
                    SheetColumn(name="パラメータ名", column="A"),
                    SheetColumn(name="既定値", column="B", role="default", role_method="detected"),
                    SheetColumn(name="設定例", column="C", role="example", role_method="detected"),
                    SheetColumn(name="説明", column="D", role="definition", role_method="detected"),
                ],
                blocks=[
                    SheetBlock(
                        kind="row",
                        row_start=4,
                        row_end=4,
                        cell_range="A4:D4",
                        values={
                            "パラメータ名": "session_timeout_minutes",
                            "既定値": "30",
                            "設定例": "60",
                            "説明": "ログインが切れるまでの分数",
                        },
                    )
                ],
            )
        ],
    )
    result = run_external_adapter(
        "structured_records_jsonl",
        _jsonl(document),
        _xlsx_profile(),
        STRUCTURED_RECORDS_CONTENT_TYPE,
    )
    assert result.extraction is not None
    chunks = chunk_extraction_with_strategy(
        result.extraction, strategy="structure_aware", chunk_size=800, overlap=0
    )
    assert any(
        "既定値［資料の既定値］: 30 / 設定例［例示の値］: 60 / 説明: ログイン" in chunk.text
        for chunk in chunks
    )
    # 回答の生成の規則は、本文に付く表示と同じ文字で列の種類を説明する。
    for label in COLUMN_ROLE_TEXT_LABELS.values():
        assert f"［{label}］" in GENERATE_SYSTEM_PROMPT

    # 文書レシピの選択肢で、判定の有無と列ごとの役割を指定できる。役割の名前は検証する。
    recipe = DocumentProcessingConfig.model_validate(
        {"excel_options": {"column_role_detection": "off", "column_roles": {" D ": "example"}}}
    )
    assert recipe.excel_options is not None
    assert recipe.excel_options.column_roles == {"D": "example"}
    with pytest.raises(ValidationError):
        DocumentProcessingConfig.model_validate(
            {"excel_options": {"column_roles": {"D": "actual"}}}
        )
