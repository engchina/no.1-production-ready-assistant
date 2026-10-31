"""ページの境目で切れた段落の続き(raw.continues_record_id)を同じ child に入れる(#2001)。"""

from rag_engine.chunking import CHILD_CHUNK_LEVEL, ChunkingConfig, build_small_to_big_chunks


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
        "raw_type": category.lower(),
        "raw": raw or {},
    }


def _children(records: list[dict]) -> list:
    chunks = build_small_to_big_chunks(
        {"run_id": "abcdef", "pdf_name": "manual.pdf", "records": records, "engines": [{"engine": "docling", "label": "Docling"}]},
        source_run_id="abcdef",
        selected_engine_ids=["docling"],
        config=ChunkingConfig(
            child_target_chars=60, table_child_target_chars=60, parent_target_chars=400, parent_max_pages=3, parent_max_children=8
        ),
    )
    return [chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]


_HEAD = "1. 概要"
_FIRST = "前のページの最後の段落で、文がまだ終わらずに次のページへ続いている部分のため"
_SECOND = "ここで文が終わります。続きの段落は前の段落と同じ子に入ります。"


def test_continued_paragraph_stays_in_the_same_child_chunk() -> None:
    children = _children(
        [
            _record(1, 1, "Section-header", _HEAD),
            _record(1, 2, "Text", _FIRST),
            _record(2, 1, "Text", _SECOND, raw={"continues_record_id": "docling-p1-2"}),
        ]
    )

    joined = [child for child in children if _FIRST in child.text]
    assert len(joined) == 1
    assert _SECOND in joined[0].text
    # record はページごとに残るため、chunk は両方のページを指す。
    assert (joined[0].page_start, joined[0].page_end) == (1, 2)


def test_paragraph_without_continuation_mark_splits_at_target_size() -> None:
    children = _children(
        [
            _record(1, 1, "Section-header", _HEAD),
            _record(1, 2, "Text", _FIRST),
            _record(2, 1, "Text", _SECOND),
        ]
    )

    first = next(child for child in children if _FIRST in child.text)
    assert _SECOND not in first.text


_TABLE = "<table><tbody><tr><th>項目</th><th>値</th></tr><tr><td>A</td><td>1</td></tr></tbody></table>"


def test_continued_paragraph_skips_table_between_halves() -> None:
    """続きの間に表がある段落(MinerU の continues_prev は図・表をまたぐ。#2266)も同じ子に入れ、表は別の子にする。"""
    children = _children(
        [
            _record(1, 1, "Section-header", _HEAD),
            _record(1, 2, "Text", _FIRST),
            _record(1, 3, "Table", _TABLE),
            _record(2, 1, "Text", _SECOND, raw={"continues_record_id": "docling-p1-2"}),
        ]
    )

    joined = [child for child in children if _FIRST in child.text]
    assert len(joined) == 1
    assert _SECOND in joined[0].text
    assert "項目" not in joined[0].text
    assert any("項目" in child.text and _FIRST not in child.text for child in children)


def test_paragraph_without_continuation_is_closed_before_table() -> None:
    children = _children(
        [
            _record(1, 1, "Section-header", _HEAD),
            _record(1, 2, "Text", _FIRST),
            _record(1, 3, "Table", _TABLE),
            _record(2, 1, "Text", "別の段落です。"),
        ]
    )

    first = next(child for child in children if _FIRST in child.text)
    assert "別の段落です。" not in first.text


def test_continued_paragraph_after_footnote_in_buffer() -> None:
    """前のページの下端の脚注が buffer の末尾にあっても、続きの段落を同じ子に入れる。"""
    children = _children(
        [
            _record(1, 1, "Section-header", _HEAD),
            _record(1, 2, "Text", _FIRST),
            _record(1, 3, "Footnote", "注 1"),
            _record(2, 1, "Text", _SECOND, raw={"continues_record_id": "docling-p1-2"}),
        ]
    )

    joined = [child for child in children if _FIRST in child.text]
    assert len(joined) == 1
    assert _SECOND in joined[0].text
