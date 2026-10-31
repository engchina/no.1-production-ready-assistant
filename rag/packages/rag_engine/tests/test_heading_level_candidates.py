"""見出しの階層の推定(backend の LLM の段。#2277)に渡す見出しの選び方。"""

from rag_engine.chunking.headings import heading_level_candidates


def _record(page: int, seq_no: int, category: str, text: str, raw: dict | None = None) -> dict:
    return {
        "id": f"docling-p{page}-{seq_no}",
        "engine": "docling",
        "page": page,
        "seq_no": seq_no,
        "bbox": [0, 40, 10, 50],
        "coord_system": "image_top_left",
        "page_width": 100,
        "page_height": 100,
        "category": category,
        "text": text,
        "raw": raw or {},
    }


def test_candidates_are_headings_in_reading_order_with_numbering_level() -> None:
    records = [
        _record(2, 1, "Section-header", "背景"),
        _record(1, 1, "Title", "文書の題"),
        _record(1, 2, "Text", "本文です。"),
        _record(2, 2, "Section-header", "2. 手順"),
        # 深さが決まっている見出し(しおり)は渡さない。
        _record(3, 1, "Section-header", "付録", raw={"heading_level": 1, "heading_source": "pdf_outline"}),
    ]

    candidates = heading_level_candidates(records)

    assert [(record_id, text) for record_id, text, _ in candidates] == [
        ("docling-p1-1", "文書の題"),
        ("docling-p2-1", "背景"),
        ("docling-p2-2", "2. 手順"),
    ]
    levels = {text: level for _, text, level in candidates}
    assert levels["背景"] is None
    assert levels["2. 手順"] is not None
