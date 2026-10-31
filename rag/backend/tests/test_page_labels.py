"""PDF の印刷の頁番号（ページラベル）を chunk の場所に付ける（#1244）。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.rag.page_labels import (
    PAGE_LABELS_ARTIFACT_KEY,
    apply_page_labels,
    labels_from_artifacts,
    page_labels_artifact,
    pdf_page_labels,
)


def _pdf_with_labels(nums: str | None) -> bytes:
    """3 頁の空の PDF。nums があれば /PageLabels（例: 前付きを小文字のローマ数字）を付ける。"""
    labels = f" /PageLabels << /Nums [{nums}] >>" if nums else ""
    objects = [
        f"<< /Type /Catalog /Pages 2 0 R{labels} >>",
        "<< /Type /Pages /Kids [3 0 R 4 0 R 5 0 R] /Count 3 >>",
        *["<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>"] * 3,
    ]
    body = b"%PDF-1.7\n"
    offsets = []
    for number, text in enumerate(objects, start=1):
        offsets.append(len(body))
        body += f"{number} 0 obj\n{text}\nendobj\n".encode()
    xref = len(body)
    body += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    body += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    body += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return body


def test_pdf_page_labels_keeps_only_labels_that_differ_from_the_physical_page() -> None:
    labeled = _pdf_with_labels("0 << /S /r >> 2 << /S /D /St 1 >>")
    # 前付きの 2 頁（i, ii）の後の本文は 1 から数える。3 枚目の「1」は物理頁（3）と違うので残す。
    assert pdf_page_labels(labeled) == {1: "i", 2: "ii", 3: "1"}
    # 物理頁と同じラベルは残さない。
    assert pdf_page_labels(_pdf_with_labels("0 << /S /D /St 1 >>")) == {}
    assert pdf_page_labels(_pdf_with_labels(None)) == {}
    assert pdf_page_labels(b"not a pdf") == {}


def test_labels_round_trip_through_artifacts_and_reach_chunks() -> None:
    labels = {1: "i", 2: "ii", 3: "1"}
    artifact = page_labels_artifact(labels)
    assert artifact is not None
    assert labels_from_artifacts({PAGE_LABELS_ARTIFACT_KEY: artifact}) == labels
    assert page_labels_artifact({}) is None
    assert labels_from_artifacts({PAGE_LABELS_ARTIFACT_KEY: "{broken"}) == {}
    assert labels_from_artifacts({}) == {}

    chunks = [
        SimpleNamespace(metadata={"page_start": 2, "page_end": 3}),
        SimpleNamespace(metadata={"page_number": "1"}),
        SimpleNamespace(metadata={"page_start": 9}),
        SimpleNamespace(metadata={"sheet_name": "表"}),
    ]
    apply_page_labels(chunks, labels)
    assert (chunks[0].metadata["page_label_start"], chunks[0].metadata["page_label_end"]) == (
        "ii",
        "1",
    )
    assert chunks[1].metadata["page_label_start"] == "i"
    assert "page_label_start" not in chunks[2].metadata
    assert "page_label_start" not in chunks[3].metadata


def test_ingestion_records_labels_only_for_pdfs() -> None:
    from app.rag.ingestion import _extraction_with_page_labels
    from app.schemas.extraction import StructuredExtraction

    extraction = StructuredExtraction(raw_text="本文")
    labeled = _extraction_with_page_labels(
        extraction, _pdf_with_labels("0 << /S /r >>"), "application/pdf"
    )
    assert labels_from_artifacts(labeled.parser_artifacts) == {1: "i", 2: "ii", 3: "iii"}
    assert _extraction_with_page_labels(extraction, b"text", "text/plain") is extraction


class _TrackingLock:
    """PDFium の lock の代わり。中に入っているスレッドの数の最大と、持ち主を記録する。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._meta = threading.Lock()
        self._owner: int | None = None
        self._depth = 0
        self.active = 0
        self.max_active = 0
        self.entries = 0

    def __enter__(self) -> _TrackingLock:
        self._lock.acquire()
        self._depth += 1
        if self._depth == 1:
            self._owner = threading.get_ident()
            with self._meta:
                self.active += 1
                self.entries += 1
                self.max_active = max(self.max_active, self.active)
        return self

    def __exit__(self, *_exc: object) -> None:
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            with self._meta:
                self.active -= 1
        self._lock.release()

    def held_by_current_thread(self) -> bool:
        return self._owner == threading.get_ident()


def test_pdf_page_labels_calls_pdfium_only_inside_the_shared_lock_across_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """並行する取込から呼ばれても、PDFium を開く・読む・閉じるは process の lock の中（#3362）。"""
    import pypdfium2  # type: ignore[import-untyped]

    from app.rag import page_labels

    lock = _TrackingLock()
    monkeypatch.setattr(page_labels, "PDFIUM_LOCK", lock)
    real_document = pypdfium2.PdfDocument
    outside_lock: list[str] = []

    def check(operation: str) -> None:
        if not lock.held_by_current_thread():
            outside_lock.append(operation)

    class _CheckedDocument:
        def __init__(self, source: bytes) -> None:
            check("open")
            self._document = real_document(source)

        def __len__(self) -> int:
            check("len")
            return len(self._document)

        def get_page_label(self, index: int) -> str:
            check("get_page_label")
            return str(self._document.get_page_label(index))

        def close(self) -> None:
            check("close")
            self._document.close()

    monkeypatch.setattr(pypdfium2, "PdfDocument", _CheckedDocument)
    labeled = _pdf_with_labels("0 << /S /r >> 2 << /S /D /St 1 >>")
    expected = {1: "i", 2: "ii", 3: "1"}
    calls = 8 * 25
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _index: pdf_page_labels(labeled), range(calls)))

    assert results == [expected] * calls
    assert outside_lock == []
    assert lock.entries == calls
    assert lock.max_active == 1


def test_pdf_page_labels_shares_the_rag_engine_pdfium_lock() -> None:
    """RAG backend と rag_engine（Vision の描画など）が同じ 1 つの lock を使う（#3362）。"""
    from rag_engine.parsing import rendering, table_vision

    from app.rag import page_labels

    assert vars(page_labels)["PDFIUM_LOCK"] is rendering.PDFIUM_LOCK
    assert vars(table_vision)["PDFIUM_LOCK"] is rendering.PDFIUM_LOCK
