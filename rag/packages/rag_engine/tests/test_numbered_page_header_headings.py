"""新章の初出が頁ヘッダに分類されても、章境界と表の所属を保持する。"""

from copy import deepcopy

from rag_engine.chunking import CHILD_CHUNK_LEVEL, ChunkingConfig, build_small_to_big_chunks, layout_section_paths
from rag_engine.chunking.headings import _promote_headings


def _record(page, seq, category, text):
    return dict(
        id=f"r-{page}-{seq}",
        engine="docling",
        page=page,
        seq_no=seq,
        bbox=[10, 10, 900, 60],
        coord_system="image_top_left",
        page_width=1000,
        page_height=1400,
        category=category,
        text=text,
        raw={},
    )


def _records():
    return [
        _record(1, 1, "Section-header", "第1章 範囲 A"),
        _record(1, 2, "Text", "最初の本文。"),
        _record(2, 1, "Page-header", "第2章 範囲 B"),
        _record(2, 2, "Section-header", "測定結果"),
        _record(2, 3, "Table", "<table><tr><td>値 B</td><td>42</td></tr></table>"),
        _record(3, 1, "Page-header", "第2章 範囲 B"),
        _record(3, 2, "Text", "続きの本文。"),
        _record(3, 3, "Page-footer", "3 / 3"),
    ]


def test_first_numbered_page_header_starts_new_section_but_repeated_header_does_not():
    records = _records()
    original = deepcopy(records)
    promoted = _promote_headings(records)
    assert promoted[2]["category"] == "Section-header"
    assert promoted[5]["category"] == "Page-header"
    paths = layout_section_paths(records)
    assert paths["r-2-3"] == ["第2章 範囲 B", "測定結果"]
    assert paths["r-3-2"] == ["第2章 範囲 B", "測定結果"]
    assert records == original
    assert _promote_headings(promoted) == promoted


def test_real_parent_child_split_keeps_table_in_new_chapter():
    chunks = build_small_to_big_chunks(
        {"pdf_name": "synthetic.pdf", "records": _records(), "engines": [{"engine": "docling", "label": "Docling"}]},
        source_run_id="a12345",
        selected_engine_ids=["docling"],
        config=ChunkingConfig(),
    )
    children = [c for c in chunks if c.chunk_level == CHILD_CHUNK_LEVEL and "値 B" in c.text]
    assert children
    for child in children:
        assert "第2章 範囲 B" in child.metadata["section_path"]
        assert "第1章 範囲 A" not in child.metadata["section_path"]
        parent = next(c for c in chunks if c.chunk_id == child.parent_chunk_id)
        assert "最初の本文" not in parent.text


def test_numbers_and_sentences_are_not_promoted_without_matching_section_series():
    records = [_record(1, 1, "Section-header", "概要"), _record(2, 1, "Page-header", "第2章 範囲 B"), _record(2, 2, "Page-footer", "2 / 3")]
    assert _promote_headings(records) == records
    records = [
        _record(1, 1, "Section-header", "第1章 範囲 A"),
        _record(2, 1, "Page-header", "第2章を参照してください。"),
        _record(2, 2, "Page-header", "2 / 3"),
    ]
    assert _promote_headings(records) == records
