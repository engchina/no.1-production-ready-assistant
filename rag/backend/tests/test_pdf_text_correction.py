"""原文で裏付けられる結合下線の復元と、章節・分割への反映。"""

from __future__ import annotations

from typing import Any

import pytest

from app.rag import pdf_text_correction
from app.rag.chunking import chunk_extraction
from app.rag.chunking_small_to_big import build_parent_child_chunks
from app.rag.navigation import build_navigation_tree
from app.rag.pdf_segment_merge import ContinuationGeometry, consolidate_document_extraction
from app.rag.pdf_text_correction import _restore_source_characters, correct_docling_pdf_text
from app.schemas.extraction import DocumentElement, StructuredExtraction
from tests.fixtures.synthetic_pdf import SyntheticPdf, blank_pdf


@pytest.mark.parametrize(
    ("parsed", "original", "expected"),
    [
        ("Sample\u033201", "Sample_01", "Sample_01"),
        ("Sample\u033201", "Sample\u033201", "Sample\u033201"),
        ("A\u0332B\u0332", "A_B\u0332", "A_B\u0332"),
        ("A\u0332B", "A-XB", "A\u0332B"),
        ("A\u0332B", "Other_B", "A\u0332B"),
        ("A\u0332B", "", "A\u0332B"),
        ("A\u0332 B", "A_B", "A_ B"),
        ("09:00\u301c17:00", "09:00\uff5e17:00", "09:00\uff5e17:00"),
        ("09:00\u301c17:00", "09:00\u301c17:00", "09:00\u301c17:00"),
        ("09:00\u301c17:00", "09:00\uff5e18:00", "09:00\u301c17:00"),
        ("A\u033201\u301c02", "A_01\uff5e02", "A_01\uff5e02"),
    ],
)
def test_restore_requires_original_text(parsed: str, original: str, expected: str) -> None:
    assert _restore_source_characters(parsed, original) == expected


def test_wave_character_restoration_preserves_evidence_matching() -> None:
    from app.rag.evaluation import evidence_matches_chunk
    from app.schemas.evaluation import EvaluationEvidence
    from app.schemas.search import RetrievedChunk

    original = "09:00\uff5e17:00"
    document = SyntheticPdf()
    page = document.page()
    record = {
        "id": "body-1",
        "engine": "docling",
        "category": "Text",
        "page": 1,
        "text": "09:00\u301c17:00",
        "bbox": list(page.text(50, 70, original, font="japan")),
        "page_width": page.width,
        "page_height": page.height,
        "coord_system": "image_top_left",
    }
    source = document.to_bytes()
    chunk = RetrievedChunk(
        document_id="doc-1",
        chunk_id="chunk-1",
        text=record["text"],
        score=1,
        metadata={"page_number": 1},
    )
    expected = EvaluationEvidence(id="e1", document_id="doc-1", page_start=1, text=original)
    assert not evidence_matches_chunk(expected, chunk)
    assert correct_docling_pdf_text([record], source) == {"body-1"}
    assert record["text"] == original
    assert evidence_matches_chunk(expected, chunk.model_copy(update={"text": record["text"]}))


def _fixture(rotation: int = 0) -> tuple[bytes, dict[str, Any]]:
    document = SyntheticPdf()
    page = document.page(rotation=rotation)
    rect = page.text(50, 70, "Synthetic_01_Title")
    page.text(50, 150, "Other_02_Title")
    record = {
        "id": "title-1",
        "engine": "docling",
        "category": "Section-header",
        "page": 1,
        "seq_no": 1,
        "text": "Synthetic\u033201\u0332Title",
        "bbox": list(rect),
        "page_width": page.width,
        "page_height": page.height,
        "coord_system": "image_top_left",
        "raw": {"text": "Synthetic\u033201\u0332Title"},
    }
    return document.to_bytes(), record


def test_corrects_only_same_position_and_is_idempotent() -> None:
    pdf, record = _fixture()
    assert correct_docling_pdf_text([record], pdf) == {"title-1"}
    assert record["text"] == "Synthetic_01_Title"
    assert correct_docling_pdf_text([record], pdf) == set()
    # 原始の parser 出力は診断のため保持する。
    assert "\u0332" in record["raw"]["text"]


@pytest.mark.parametrize("kind", ["Table", "Picture"])
def test_does_not_flatten_table_or_image_text(kind: str) -> None:
    pdf, record = _fixture()
    record["category"] = kind
    assert correct_docling_pdf_text([record], pdf) == set()


def test_does_not_use_matching_text_from_another_position() -> None:
    pdf, record = _fixture()
    record["bbox"] = [50.0, 135.0, 200.0, 155.0]
    assert correct_docling_pdf_text([record], pdf) == set()
    assert "\u0332" in record["text"]


@pytest.mark.parametrize("changed", ["page", "bounds", "scale", "rotation", "invalid_pdf"])
def test_unreadable_sources_are_preserved(changed: str) -> None:
    pdf, record = _fixture()
    if changed == "page":
        record["page"] = 2
    elif changed == "bounds":
        record["bbox"][0] = float("nan")
    elif changed == "scale":
        record["page_width"] = 0
    elif changed == "rotation":
        pdf, _rotated = _fixture(rotation=90)
    else:
        pdf = b"not a pdf"
    assert correct_docling_pdf_text([record], pdf) == set()
    assert "\u0332" in record["text"]


def test_consolidation_corrects_text_paths_navigation_and_chunks() -> None:
    pdf, record = _fixture()
    extraction = StructuredExtraction(
        raw_text=record["text"] + "\n\nSynthetic paragraph.\n\nAdditional image description.",
        elements=[
            DocumentElement(
                kind="title",
                text=record["text"],
                element_id="title-1",
                page_number=1,
                section_path=[record["text"]],
            ),
            DocumentElement(
                kind="text",
                text="Synthetic paragraph.",
                element_id="body-1",
                page_number=1,
                section_path=[record["text"]],
            ),
        ],
        parser_artifacts={
            "layout_records": {
                "records": [
                    record,
                    {
                        **record,
                        "id": "body-1",
                        "category": "Text",
                        "seq_no": 2,
                        "text": "Synthetic paragraph.",
                        "raw": {},
                    },
                ]
            }
        },
    )
    corrected = consolidate_document_extraction(
        extraction, source_bytes=pdf, geometry=ContinuationGeometry(enabled=False)
    )
    # 抽出の正規化は書式の飾りの線を除き（#3354）、原文で裏付けた所だけ下線の文字に戻る。
    assert extraction.elements[0].text == "Synthetic01Title"
    assert corrected.elements[0].text == "Synthetic_01_Title"
    assert corrected.raw_text.endswith("\n\nAdditional image description.")
    assert "\u0332" not in corrected.raw_text
    assert all(element.section_path == ["Synthetic_01_Title"] for element in corrected.elements)
    assert build_navigation_tree(corrected)[0].title == "Synthetic_01_Title"
    chunks = chunk_extraction(corrected, chunk_size=500, overlap=0)
    assert chunks
    assert all("\u0332" not in str(chunk.metadata) + chunk.text for chunk in chunks)
    parent_child_chunks = build_parent_child_chunks(corrected)
    assert parent_child_chunks
    assert all("\u0332" not in str(chunk.metadata) + chunk.text for chunk in parent_child_chunks)
    again = consolidate_document_extraction(
        corrected, source_bytes=pdf, geometry=ContinuationGeometry(enabled=False)
    )
    assert again == corrected


def test_no_text_layer_and_other_parser_are_not_corrected() -> None:
    pdf, record = _fixture()
    record["engine"] = "other_engine"
    assert correct_docling_pdf_text([record], pdf) == set()
    record["engine"] = "docling"
    empty = blank_pdf()
    assert correct_docling_pdf_text([record], empty) == set()


def test_normal_text_does_not_open_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    pdf, record = _fixture()
    record["text"] = "Synthetic_01_Title"

    def unexpected_open(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("正常な文字では PDF を開かない")

    monkeypatch.setattr(pdf_text_correction, "PdfSource", unexpected_open)
    assert correct_docling_pdf_text([record], pdf) == set()
