"""ページの境目の段落の続きの判定の行の位置と、図・表の読み飛ばし(#2273)。

PDF は reportlab で書いた合成の文字の層(内容は意味の無い文字の並び)。record の bbox は
ページ画像の px(このテストはページの pt と同じ寸法にする)。
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.config import Settings
from app.rag.pdf_segment_merge import (
    CONSOLIDATION_ARTIFACT,
    ContinuationGeometry,
    consolidate_document_extraction,
)
from app.schemas.extraction import DocumentElement, StructuredExtraction
from tests.fixtures.synthetic_pdf import SyntheticPage, SyntheticPdf, blank_pdf

PAGE = (600.0, 800.0)
LEFT = 100.0
FONT = 10.0
LINE_STEP = 14.0
# Helvetica の "x" の幅(フォントの大きさに対する割合)。行の長さを文字の数で決める。
X_WIDTH = 0.5 * FONT
FULL = 80  # LEFT から 500pt まで。
SHORT = 20
PREVIOUS_TEXT = "前のページの本文は文の途中で"
CURRENT_TEXT = "次のページへ続きます。"


def _write_lines(page: SyntheticPage, top: float, lines: list[tuple[float, int]]) -> None:
    for index, (indent, count) in enumerate(lines):
        page.text(LEFT + indent, top + index * LINE_STEP, "x" * count, size=FONT)


def _pdf(previous: list[tuple[float, int]], current: list[tuple[float, int]]) -> bytes:
    document = SyntheticPdf()
    _write_lines(document.page(*PAGE), 700.0, previous)
    _write_lines(document.page(*PAGE), 60.0, current)
    return document.to_bytes()


def _record(page: int, seq: int, category: str, text: str, bbox: list[float]) -> dict[str, Any]:
    return {
        "id": f"docling-p{page}-{seq}",
        "engine": "docling",
        "page": page,
        "seq_no": seq,
        "bbox": bbox,
        "coord_system": "image_top_left",
        "page_width": PAGE[0],
        "page_height": PAGE[1],
        "category": category,
        "text": text,
        "raw": {},
    }


def _extraction(records: list[dict[str, Any]]) -> StructuredExtraction:
    return StructuredExtraction(
        elements=[
            DocumentElement(text=r["text"] or "図", element_id=r["id"], page_number=r["page"])
            for r in records
        ],
        parser_artifacts={"layout_records": {"version": 1, "pages": [], "records": records}},
    )


def _paragraph_pair() -> StructuredExtraction:
    return _extraction(
        [
            _record(1, 1, "Text", PREVIOUS_TEXT, [LEFT, 690.0, 520.0, 760.0]),
            _record(2, 1, "Text", CURRENT_TEXT, [LEFT, 50.0, 520.0, 110.0]),
        ]
    )


def _continues(result: StructuredExtraction) -> bool:
    layout = cast(dict[str, Any], result.parser_artifacts["layout_records"])
    current = next(r for r in layout["records"] if r["id"] == "docling-p2-1")
    return bool(current["raw"].get("continues_record_id") == "docling-p1-1")


def _rejected(result: StructuredExtraction) -> int:
    report = cast(dict[str, Any], result.parser_artifacts[CONSOLIDATION_ARTIFACT])
    return int(report["continuations_rejected_by_geometry"])


FULL_LINES = [(0.0, FULL)] * 3


@pytest.mark.parametrize(
    ("previous", "current", "continues"),
    [
        (FULL_LINES, FULL_LINES, True),
        # 前の段落の最後の行が右端まで届かない(段落の終わり)。
        ([(0.0, FULL), (0.0, FULL), (0.0, SHORT)], FULL_LINES, False),
        # 次の段落の最初の行が字下げで始まる(新しい段落)。
        (FULL_LINES, [(2 * FONT, FULL - 4), (0.0, FULL), (0.0, FULL)], False),
        # 幅が大きく違う(別の段・別の組み)。
        (FULL_LINES, [(0.0, SHORT), (0.0, SHORT), (0.0, SHORT)], False),
        # 次の段落が 1 行だけ(短い続き)は、字下げと幅を判定しない。
        (FULL_LINES, [(0.0, SHORT)], True),
    ],
    ids=["aligned", "short-last-line", "indented-first-line", "different-width", "single-line"],
)
def test_geometry_decides_page_continuation(
    previous: list[tuple[float, int]], current: list[tuple[float, int]], continues: bool
) -> None:
    result = consolidate_document_extraction(
        _paragraph_pair(), source_bytes=_pdf(previous, current)
    )

    assert _continues(result) is continues
    assert _rejected(result) == int(not continues)


def test_geometry_can_be_disabled_by_settings() -> None:
    pdf = _pdf([(0.0, FULL), (0.0, FULL), (0.0, SHORT)], FULL_LINES)
    settings = Settings(rag_page_continuation_geometry_enabled=False)

    result = consolidate_document_extraction(
        _paragraph_pair(),
        source_bytes=pdf,
        geometry=ContinuationGeometry.from_settings(settings),
    )

    assert _continues(result)


def test_tolerance_comes_from_settings() -> None:
    """右端の許容を広げると、最後の行が短い段落も続きにする(閾値は設定)。"""
    pdf = _pdf([(0.0, FULL), (0.0, FULL), (0.0, FULL - 10)], FULL_LINES)

    strict = consolidate_document_extraction(_paragraph_pair(), source_bytes=pdf)
    loose = consolidate_document_extraction(
        _paragraph_pair(),
        source_bytes=pdf,
        geometry=ContinuationGeometry.from_settings(
            Settings(rag_page_continuation_right_edge_tolerance=10.0)
        ),
    )

    assert not _continues(strict)
    assert _continues(loose)


def test_pdf_without_text_layer_uses_sentence_rule_only() -> None:
    pdf = blank_pdf(PAGE, PAGE)

    result = consolidate_document_extraction(_paragraph_pair(), source_bytes=pdf)

    assert _continues(result)
    assert _rejected(result) == 0


@pytest.mark.parametrize(
    "between",
    [
        [_record(1, 2, "Picture", "", [LEFT, 600.0, 300.0, 680.0])],
        [
            _record(
                1, 2, "Table", "<table><tr><td>a</td></tr></table>", [LEFT, 600.0, 500.0, 680.0]
            ),
            _record(1, 3, "Caption", "表の説明", [LEFT, 680.0, 500.0, 690.0]),
        ],
    ],
    ids=["picture", "table-with-caption"],
)
def test_continuation_skips_visual_blocks_at_page_bottom(between: list[dict[str, Any]]) -> None:
    """ページ下端の図・表(と caption)を読み飛ばして、前の段落の続きを判定する。"""
    records = [
        _record(1, 1, "Text", PREVIOUS_TEXT, [LEFT, 500.0, 520.0, 560.0]),
        *between,
        _record(1, 9, "Page-footer", "1", [LEFT, 780.0, 120.0, 790.0]),
        _record(2, 1, "Text", CURRENT_TEXT, [LEFT, 50.0, 520.0, 110.0]),
    ]

    result = consolidate_document_extraction(_extraction(records), source_bytes=b"")

    assert _continues(result)


def test_continuation_stops_at_heading() -> None:
    records = [
        _record(1, 1, "Text", PREVIOUS_TEXT, [LEFT, 500.0, 520.0, 560.0]),
        _record(1, 2, "Section-header", "2. 次の節", [LEFT, 600.0, 300.0, 620.0]),
        _record(1, 3, "Picture", "", [LEFT, 640.0, 300.0, 700.0]),
        _record(2, 1, "Text", CURRENT_TEXT, [LEFT, 50.0, 520.0, 110.0]),
    ]

    result = consolidate_document_extraction(_extraction(records), source_bytes=b"")

    assert not _continues(result)
