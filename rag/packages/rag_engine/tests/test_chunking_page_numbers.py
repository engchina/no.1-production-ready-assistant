"""頁番号のフッターから論理頁を決める規則のテスト（#2289）。合成の record だけを使う。"""

from rag_parser_core.language import bind_pack, builtin_pack, resolve

from rag_engine.chunking import (
    CHILD_CHUNK_LEVEL,
    ChunkingConfig,
    _boilerplate_text_key,
    _metadata_by_page,
    build_small_to_big_chunks,
)
from rag_engine.chunking.page_numbers import ARABIC, KANJI, ROMAN, read_page_number


def _footer(page: int, text: str) -> dict:
    return {
        "id": f"docling-p{page}-2",
        "engine": "docling",
        "page": page,
        "seq_no": 2,
        "bbox": [0, 95, 10, 99],
        "coord_system": "image_top_left",
        "page_width": 100,
        "page_height": 100,
        "category": "Page-footer",
        "text": text,
        "raw_type": "page-footer",
        "raw": {},
    }


def _body(page: int) -> dict:
    return {
        **_footer(page, f"本文 {page}"),
        "id": f"docling-p{page}-1",
        "seq_no": 1,
        "bbox": [0, 40, 10, 50],
        "category": "Text",
        "raw_type": "text",
    }


def _logical_pages(footers: dict[int, str], config: ChunkingConfig | None = None, pages: int | None = None) -> dict[int, object]:
    records = []
    for page in range(1, (pages or max(footers)) + 1):
        records.append(_body(page))
        if page in footers:
            records.append(_footer(page, footers[page]))
    metadata = _metadata_by_page(records, config or ChunkingConfig())
    return {page: item["logical_page"] for page, item in metadata.items() if "logical_page" in item}


def test_outlier_page_number_is_not_used():
    # 3 頁目だけ並びから外れた数（図の番号など）。並びに乗る頁だけを論理頁にする。
    assert _logical_pages({1: "1", 2: "2", 3: "37", 4: "4", 5: "5"}) == {1: 1, 2: 2, 4: 4, 5: 5}


def test_short_run_inside_a_longer_run_is_not_used():
    # 4・5 頁目は 2 頁続けて差が一定だが、長い並びの途中にあるので外れた数として使わない。
    footers = {page: str(page + 10) for page in range(1, 9)}
    footers.update({4: "40", 5: "41"})
    assert _logical_pages(footers) == {1: 11, 2: 12, 3: 13, 6: 16, 7: 17, 8: 18}


def test_offset_is_kept_and_pages_without_footer_do_not_break_the_run():
    assert _logical_pages({2: "- 12 -", 3: "13", 5: "15 頁"}, pages=5) == {2: 12, 3: 13, 5: 15}


def test_roman_front_matter_and_arabic_body_are_separate_runs():
    footers = {1: "i", 2: "ii", 3: "III", 4: "1", 5: "2", 6: "3"}
    assert _logical_pages(footers) == {1: "i", 2: "ii", 3: "III", 4: 1, 5: 2, 6: 3}


def test_kanji_and_full_width_page_numbers_are_read():
    assert _logical_pages({1: "－ 一 －", 2: "二頁", 3: "三", 4: "Ⅳ"}) == {1: 1, 2: 2, 3: 3}
    assert read_page_number("Ⅳ", 4).system == ROMAN
    assert read_page_number("十二", 1).value == 12
    assert read_page_number("十二", 1).system == KANJI
    assert read_page_number("１２", 1).system == ARABIC
    assert read_page_number("iiii", 1) is None
    assert read_page_number("〇", 1) is None


def test_single_page_document_uses_its_page_number():
    assert _logical_pages({1: "31"}) == {1: 31}


def test_lone_page_number_in_a_longer_document_is_not_used():
    assert _logical_pages({3: "7"}, pages=5) == {}


def test_min_run_pages_and_offset_tolerance_come_from_the_config():
    # 3 頁目に番号の無い挿入頁があり、差が 1 ずれる。
    footers = {1: "1", 2: "2", 4: "3", 5: "4"}
    strict = ChunkingConfig(page_number_min_run_pages=3).validate()
    tolerant = ChunkingConfig(page_number_min_run_pages=3, page_number_offset_tolerance=1).validate()
    assert _logical_pages(footers, strict) == {}
    assert _logical_pages(footers, tolerant) == {1: 1, 2: 2, 4: 3, 5: 4}
    assert _logical_pages(footers) == {1: 1, 2: 2, 4: 3, 5: 4}


def test_number_systems_come_from_the_language_pack():
    footers = {1: "壱", 2: "弐", 3: "参"}
    assert _logical_pages(footers) == {}
    custom = resolve(builtin_pack(), {"numbers": {"kanji_digits": ["壱=1", "弐=2", "参=3"], "roman_digits": []}})
    with bind_pack(custom):
        assert _logical_pages(footers) == {1: 1, 2: 2, 3: 3}
        assert _logical_pages({1: "i", 2: "ii", 3: "iii"}) == {}


def test_boilerplate_key_masks_roman_and_kanji_page_numbers():
    assert _boilerplate_text_key("Guide ii") == _boilerplate_text_key("Guide iv")
    assert _boilerplate_text_key("操作の手引き 三頁") == _boilerplate_text_key("操作の手引き 十二頁")
    assert _boilerplate_text_key("- 三 -") == _boilerplate_text_key("12") == "#"
    # 章の番号は伏せない（章ごとに違う柱を同じにしない）。英語の語はローマ数字にしない。
    assert _boilerplate_text_key("第三章 概要") != _boilerplate_text_key("第四章 概要")
    assert _boilerplate_text_key("Civil guide") != _boilerplate_text_key("Vivid guide")


def test_logical_page_labels_reach_chunk_metadata_and_search_text():
    records = []
    for page, label in enumerate(("i", "ii", "1", "2"), start=1):
        records.extend([_body(page), _footer(page, label)])
    chunks = build_small_to_big_chunks(
        {"run_id": "abcdef", "pdf_name": "manual.pdf", "records": records, "engines": [{"engine": "docling", "label": "Docling"}]},
        source_run_id="abcdef",
        selected_engine_ids=["docling"],
        config=ChunkingConfig(parent_max_pages=5),
    )
    labels: dict[str, object] = {}
    for chunk in chunks:
        if chunk.chunk_level == CHILD_CHUNK_LEVEL:
            labels.update(chunk.metadata["layout"]["logical_page_labels"])
            assert "ii" not in chunk.text.split()
    assert labels == {"1": "i", "2": "ii", "3": 1, "4": 2}
    assert any("p.2=logical ii" in chunk.retrieval_text for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)
