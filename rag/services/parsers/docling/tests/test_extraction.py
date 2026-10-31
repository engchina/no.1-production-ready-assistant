"""LayoutRecord → StructuredExtraction 変換と /parse の options 受け渡し。"""

import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from rag_engine.models.layout import LayoutRecord, PageImage
from rag_parser_core.extraction import StructuredExtraction
from rag_parser_core.parsed_table import PARSED_TABLE_GRID_KEY, parsed_table_bindings

from app import extraction as extraction_module
from app.extraction import LAYOUT_ARTIFACT, layout_to_extraction
from app.main import app


def _record(record_id: str, category: str, text: str, **raw: object) -> LayoutRecord:
    return LayoutRecord(
        id=record_id,
        engine="docling",
        page=1,
        seq_no=int(record_id.rsplit("-", 1)[-1]),
        bbox=[10.0, 20.0, 110.0, 60.0],
        coord_system="image_top_left",
        page_width=1000,
        page_height=1400,
        category=category,
        text=text,
        raw_type=category.lower(),
        raw=dict(raw),
    )


def test_layout_records_map_to_elements_tables_assets_and_layout_artifact() -> None:
    page = PageImage(
        page=1, width=1000, height=1400, pdf_width=600, pdf_height=840, image_path="/x.png"
    )
    records = [
        _record("docling-p1-1", "Title", "申請手順"),
        _record("docling-p1-2", "Section-header", "入力画面"),
        _record("docling-p1-3", "Text", "受付番号を入力します。"),
        _record("docling-p1-4", "Table", "<table><tr><td>A</td></tr></table>"),
        _record(
            "docling-p1-5",
            "Picture",
            "説明",
            vision_status="succeeded",
            vision_description={"retrieval_text": "登録ボタンの画面"},
        ),
    ]

    extraction = layout_to_extraction(records, [page], source_page_count=1)

    kinds = [element.kind for element in extraction.elements]
    assert kinds == ["title", "title", "text", "table", "figure"]
    body = extraction.elements[2]
    assert body.section_path == ["申請手順", "入力画面"]
    assert body.bbox == [10.0, 20.0, 110.0, 60.0]
    assert body.metadata["page_width"] == 1000.0
    assert body.metadata["bbox_unit"] == "absolute"
    assert extraction.pages[0].width == 1000.0
    assert extraction.tables[0].element_id == "docling-p1-4"
    assert extraction.tables[0].metadata["html"].startswith("<table>")
    assert extraction.assets[0].summary == "登録ボタンの画面"
    assert extraction.elements[4].metadata["vision_retrieval_text"] == "登録ボタンの画面"
    layout = extraction.parser_artifacts[LAYOUT_ARTIFACT]
    assert isinstance(layout, dict)
    assert [record["id"] for record in layout["records"]] == [r.id for r in records]
    assert "image_path" not in layout["pages"][0]


def _grid_cell(row: int, col: int, text: str, **extra: object) -> dict[str, object]:
    row_span = int(extra.pop("row_span", 1))  # type: ignore[call-overload]
    col_span = int(extra.pop("col_span", 1))  # type: ignore[call-overload]
    return {
        "start_row_offset_idx": row,
        "end_row_offset_idx": row + row_span,
        "start_col_offset_idx": col,
        "end_col_offset_idx": col + col_span,
        "text": text,
        **extra,
    }


def test_table_cells_are_the_docling_grid_and_bind_to_the_table_text() -> None:
    """表のセルは Docling の格子で、本文の行と対応する(#3429)。

    結合セルは左上の 1 つ、列の見出しの印と、1 頁の表のセルの領域を持つ。
    """
    page = PageImage(
        page=1, width=1000, height=1400, pdf_width=600, pdf_height=840, image_path="/x.png"
    )
    html = (
        '<table><tr><th rowspan="2">Item</th><th colspan="2">H1</th></tr>'
        "<tr><th>Plan</th><th>Actual</th></tr><tr><td>Alpha</td><td>10</td><td>11</td></tr></table>"
    )
    raw = {
        "data": {
            "table_cells": [
                _grid_cell(0, 0, "Item", row_span=2, column_header=True),
                _grid_cell(0, 1, "H1", col_span=2, column_header=True),
                _grid_cell(1, 1, "Plan", column_header=True),
                _grid_cell(1, 2, "Actual", column_header=True),
                _grid_cell(2, 0, "Alpha", row_header=True),
                _grid_cell(2, 1, "10"),
                _grid_cell(2, 2, "11"),
            ]
        },
        "table_cell_bboxes": [{"row": 2, "col": 1, "bbox": [10.0, 20.0, 30.0, 40.0]}],
    }
    extraction = layout_to_extraction(
        [_record("docling-p1-1", "Table", html, **raw), _record("docling-p1-2", "Table", "x")],
        [page],
        source_page_count=1,
    )

    grid, plain = extraction.tables
    assert grid.metadata[PARSED_TABLE_GRID_KEY] == 1
    assert [(c.row, c.col, c.row_span, c.col_span) for c in grid.cells[:2]] == [
        (0, 0, 2, 1),
        (0, 1, 1, 2),
    ]
    assert grid.cells[0].metadata == {"column_header": True}
    assert grid.cells[5].bbox == [10.0, 20.0, 30.0, 40.0]
    # 格子の無い表(セルの無い表)は格子の印を付けない。
    assert PARSED_TABLE_GRID_KEY not in plain.metadata and plain.cells == []
    binding = parsed_table_bindings(extraction)["docling-p1-1"]
    assert (binding.table_index, binding.text_rows, binding.grid.header_rows) == (1, (1, 2, 3), 2)


def test_element_section_paths_follow_heading_numbers_like_chunks() -> None:
    """要素の section_path は chunk と同じ規則で番号から深さを決める(#1517)。

    Docling の見出しの level は PDF ではすべて 1。「3.3」は「第 3 章」の子、その後の「第 4 章」は
    3.3 の下ではなく第 3 章の兄弟になる(章節ナビゲーションの見出しの木の元)。
    """
    page = PageImage(
        page=1, width=1000, height=1400, pdf_width=600, pdf_height=840, image_path="/x.png"
    )
    records = [
        _record("docling-p1-1", "Section-header", "サーバー室の定期点検要領", level=1),
        _record("docling-p1-2", "Section-header", "第 3 章 設備ごとの点検", level=1),
        _record("docling-p1-3", "Section-header", "3.3 消火設備", level=1),
        _record("docling-p1-4", "Text", "消火器の圧力を確かめる。"),
        _record("docling-p1-5", "Section-header", "第 4 章 点検を中止する条件", level=1),
        _record("docling-p1-6", "Text", "漏水を見つけたら中止する。"),
    ]

    extraction = layout_to_extraction(records, [page], source_page_count=1)

    paths = {element.element_id: element.section_path for element in extraction.elements}
    assert paths["docling-p1-4"] == [
        "サーバー室の定期点検要領",
        "第 3 章 設備ごとの点検",
        "3.3 消火設備",
    ]
    assert paths["docling-p1-6"] == ["サーバー室の定期点検要領", "第 4 章 点検を中止する条件"]


def test_parse_endpoint_ignores_legacy_vision_options() -> None:
    """Vision は backend の共通の段が行う(#497)。旧 backend の parser_options は使わない。"""
    captured: dict[str, object] = {}

    def fake_analyze(source_bytes: bytes, **kwargs: object) -> StructuredExtraction:
        captured.update(kwargs, size=len(source_bytes))
        return StructuredExtraction(raw_text="ok")

    options = '{"vision_enabled": true, "image_retrieval_prompt": "独自 {{image_metadata}}"}'
    with patch("app.main.analyze_source", side_effect=fake_analyze):
        response = TestClient(app).post(
            "/parse",
            files={"file": ("manual.pdf", b"%PDF-1.4", "application/pdf")},
            data={"content_type": "application/pdf", "parser_options": options},
        )

    assert response.status_code == 200
    assert response.json()["extraction"]["raw_text"] == "ok"
    assert captured["file_name"] == "manual.pdf"
    assert "vision_enabled" not in captured
    assert "image_retrieval_prompt" not in captured


def test_layout_artifact_has_no_service_vision_summary() -> None:
    page = PageImage(
        page=1, width=1000, height=1400, pdf_width=600, pdf_height=840, image_path="/x.png"
    )
    picture = _record("docling-p1-1", "Picture", "")
    picture.raw_type = "picture"

    extraction = layout_to_extraction([picture], [page], source_page_count=1)

    assert "vision" not in extraction.parser_artifacts
    assert extraction.assets[0].summary is None
    assert extraction.assets[0].metadata["vision_status"] == ""


def test_parse_progress_is_readable_only_while_parsing(tmp_path: Path) -> None:
    """取込の worker は解析の間だけ同じ id でページの進捗を読める(#1999)。"""
    progress_id = "a" * 32
    seen: dict[str, object] = {}
    client = TestClient(app)

    def fake_analyze(source_bytes: bytes, **kwargs: object) -> StructuredExtraction:
        seen["progress_id"] = kwargs["progress_id"]
        run_dir = tmp_path / "run"
        run_dir.mkdir()
        extraction_module._ACTIVE_RUNS[progress_id] = run_dir
        # ページの描画中(progress.json の前)は準備中を返す。
        seen["preparing"] = client.get(f"/parse/progress/{progress_id}").json()
        payload = {
            "engine": "docling",
            "stage": "parsing_pages",
            "total_pages": 82,
            "completed_pages": 40,
        }
        (run_dir / "progress.json").write_text(json.dumps(payload), encoding="utf-8")
        seen["parsing"] = client.get(f"/parse/progress/{progress_id}").json()
        extraction_module._ACTIVE_RUNS.pop(progress_id, None)
        return StructuredExtraction(raw_text="ok")

    with patch("app.main.analyze_source", side_effect=fake_analyze):
        response = client.post(
            "/parse",
            files={"file": ("manual.pdf", b"%PDF-1.4", "application/pdf")},
            data={"content_type": "application/pdf", "progress_id": progress_id},
        )

    assert response.status_code == 200
    assert seen["progress_id"] == progress_id
    assert seen["preparing"] == {"engine": "docling", "stage": "preparing_pages"}
    assert seen["parsing"] == {
        "engine": "docling",
        "stage": "parsing_pages",
        "total_pages": 82,
        "completed_pages": 40,
    }
    # 解析の後・形の違う id は 404(パスの一部に使うため形を絞る)。
    assert client.get(f"/parse/progress/{progress_id}").status_code == 404
    assert client.get("/parse/progress/short").status_code == 404


def test_parse_ignores_malformed_progress_id() -> None:
    captured: dict[str, object] = {}

    def fake_analyze(source_bytes: bytes, **kwargs: object) -> StructuredExtraction:
        captured.update(kwargs)
        return StructuredExtraction(raw_text="ok")

    with patch("app.main.analyze_source", side_effect=fake_analyze):
        response = TestClient(app).post(
            "/parse",
            files={"file": ("manual.pdf", b"%PDF-1.4", "application/pdf")},
            data={"content_type": "application/pdf", "progress_id": "../x"},
        )

    assert response.status_code == 200
    assert captured["progress_id"] is None
