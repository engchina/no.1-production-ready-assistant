"""Chunking アダプター(分割戦略)のテスト。"""

import pytest
from pytest import MonkeyPatch
from rag_pipeline_core import chunking as core_chunking

from app.config import Settings
from app.rag.chunking import CHUNKING_STRATEGIES, chunk_extraction_with_strategy
from app.rag.chunking_strategy import (
    CHUNKING_STRATEGY_ORDER,
    SmallToBigParams,
    chunking_runtime_settings,
    normalize_chunking_strategy,
    resolve_chunking_params,
)
from app.schemas.extraction import DocumentElement, StructuredExtraction


def _sample_extraction() -> StructuredExtraction:
    return StructuredExtraction(
        raw_text=(
            "# 概要\n本文の一文目です。本文の二文目です。本文の三文目です。\n"
            "## 詳細\n詳細の一文目です。詳細の二文目です。詳細の三文目です。"
        ),
        elements=[
            DocumentElement(kind="title", text="第1章 概要", page_number=1),
            DocumentElement(
                kind="text",
                text="本文の一文目です。本文の二文目です。本文の三文目です。",
                page_number=1,
            ),
            DocumentElement(kind="title", text="第2章 詳細", page_number=2),
            DocumentElement(
                kind="text",
                text="詳細の一文目です。詳細の二文目です。詳細の三文目です。",
                page_number=2,
            ),
        ],
    )


def test_all_strategies_stamp_chunk_strategy_metadata() -> None:
    """全戦略が chunk_strategy metadata と text 計量を一貫して付ける。"""
    extraction = _sample_extraction()
    for strategy in CHUNKING_STRATEGIES:
        chunks = chunk_extraction_with_strategy(
            extraction,
            strategy=strategy,
            chunk_size=60,
            overlap=10,
        )
        assert chunks, strategy
        assert all(chunk.metadata["chunk_strategy"] == strategy for chunk in chunks), strategy
        assert all(chunk.metadata["text_chars"] == len(chunk.text) for chunk in chunks), strategy
        assert [chunk.index for chunk in chunks] == list(range(len(chunks))), strategy


def test_unknown_strategy_falls_back_to_structure_aware() -> None:
    """未知の戦略名は structure_aware へ安全に fallback する。"""
    extraction = _sample_extraction()
    chunks = chunk_extraction_with_strategy(extraction, strategy="does_not_exist")
    assert chunks
    assert all(chunk.metadata["chunk_strategy"] == "structure_aware" for chunk in chunks)


def test_legacy_sentence_window_aliases_to_recursive_character() -> None:
    """撤去済み sentence_window は recursive_character として処理する(既存設定互換)。"""
    extraction = _sample_extraction()
    chunks = chunk_extraction_with_strategy(
        extraction,
        strategy="sentence_window",
        chunk_size=400,
        overlap=0,
    )
    assert chunks
    assert all(chunk.metadata["chunk_strategy"] == "recursive_character" for chunk in chunks)


def test_page_level_groups_by_page() -> None:
    """page_level はページ単位でまとめ、page_start/page_end をページごとに保つ。"""
    extraction = _sample_extraction()
    chunks = chunk_extraction_with_strategy(
        extraction,
        strategy="page_level",
        chunk_size=400,
        overlap=0,
    )
    pages = {(chunk.metadata.get("page_start"), chunk.metadata.get("page_end")) for chunk in chunks}
    assert pages == {(1, 1), (2, 2)}


def test_page_level_applies_overlap_only_inside_page() -> None:
    """ページ内の再分割には overlap を使い、次ページへは持ち越さない。"""
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(kind="text", text="1111。2222。3333。", page_number=1),
            DocumentElement(kind="text", text="次ページです。", page_number=2),
        ]
    )

    chunks = chunk_extraction_with_strategy(
        extraction,
        strategy="page_level",
        chunk_size=10,
        overlap=2,
    )

    page_one = [chunk for chunk in chunks if chunk.metadata.get("page_start") == 1]
    assert len(page_one) >= 2
    assert all(
        current.text.startswith(previous.text[-2:].strip())
        for previous, current in zip(page_one, page_one[1:], strict=False)
    )
    page_two = next(chunk for chunk in chunks if chunk.metadata.get("page_start") == 2)
    assert not page_two.text.startswith(page_one[-1].text[-2:].strip())


def test_page_level_falls_back_without_pages() -> None:
    """ページ情報がない文書では章節単位へ fallback する。"""
    extraction = StructuredExtraction(
        raw_text="# 章\n本文の一文目です。本文の二文目です。",
    )
    chunks = chunk_extraction_with_strategy(extraction, strategy="page_level", chunk_size=400)
    assert chunks
    assert all(chunk.metadata["chunk_strategy"] == "page_level" for chunk in chunks)


def test_markdown_heading_keeps_one_chunk_per_section() -> None:
    """markdown_heading は十分大きい chunk_size で章節ごとに 1 chunk にまとめる。"""
    extraction = _sample_extraction()
    chunks = chunk_extraction_with_strategy(
        extraction,
        strategy="markdown_heading",
        chunk_size=2000,
        overlap=0,
    )
    sections = {chunk.metadata.get("section_title") for chunk in chunks}
    assert "概要" in sections
    assert "詳細" in sections
    assert len(chunks) == 2


def test_markdown_heading_applies_overlap_only_inside_section() -> None:
    """見出し内の再分割には overlap を使い、次の見出しへは持ち越さない。"""
    extraction = StructuredExtraction(raw_text="# 第一章\n1111。2222。3333。\n# 第二章\n次章です。")

    chunks = chunk_extraction_with_strategy(
        extraction,
        strategy="markdown_heading",
        chunk_size=10,
        overlap=2,
    )

    first_section = [chunk for chunk in chunks if chunk.metadata.get("section_title") == "第一章"]
    assert len(first_section) >= 2
    assert all(
        current.text.startswith(previous.text[-2:].strip())
        for previous, current in zip(first_section, first_section[1:], strict=False)
    )
    second_section = next(
        chunk for chunk in chunks if chunk.metadata.get("section_title") == "第二章"
    )
    assert not second_section.text.startswith(first_section[-1].text[-2:].strip())


def test_structure_overlap_stays_inside_element_group_and_skips_table_boundary() -> None:
    """element group 内だけを重ね、次章や table chunk へ overlap を持ち越さない。"""
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(
                kind="text",
                text="AAAA。BBBB。CCCC。",
                section_path=["第一章"],
            ),
            DocumentElement(
                kind="text",
                text="DDDD。EEEE。FFFF。",
                section_path=["第二章"],
            ),
            DocumentElement(
                kind="table",
                text="|列|値|\n|---|---|\n|A|111111|\n|B|222222|\n|C|333333|",
                section_path=["第二章"],
            ),
        ]
    )

    chunks = chunk_extraction_with_strategy(
        extraction,
        strategy="structure_aware",
        chunk_size=10,
        overlap=2,
    )

    first = [chunk for chunk in chunks if chunk.metadata.get("section_path") == "第一章"]
    second = [
        chunk
        for chunk in chunks
        if chunk.metadata.get("section_path") == "第二章"
        and chunk.metadata.get("content_kind") == "text"
    ]
    tables = [chunk for chunk in chunks if chunk.metadata.get("content_kind") == "table"]
    assert len(first) >= 2
    assert first[1].text.startswith(first[0].text[-2:].strip())
    assert not second[0].text.startswith(first[-1].text[-2:].strip())
    assert len(tables) >= 2
    assert not tables[1].text.startswith(tables[0].text[-2:].strip())


def test_min_chars_absorbs_small_chunks() -> None:
    """min_chars 未満の微小 chunk は同一 group 内の隣接 chunk へ吸収する。"""
    # 節の末尾の短い文だけが残る本文(収まらなかった文は次の chunk の先頭にする。#3380)。
    extraction = StructuredExtraction(
        raw_text="# 概要\n本文の長い一文目の文章です。短い。本文の長い三文目の文章です。短い。"
    )
    without_absorb = chunk_extraction_with_strategy(
        extraction,
        strategy="recursive_character",
        chunk_size=20,
        overlap=0,
        min_chars=0,
    )
    with_absorb = chunk_extraction_with_strategy(
        extraction,
        strategy="recursive_character",
        chunk_size=20,
        overlap=0,
        min_chars=15,
    )
    assert len(with_absorb) < len(without_absorb)
    assert all(chunk.metadata["text_chars"] == len(chunk.text) for chunk in with_absorb)


def test_fixed_size_ignores_min_chars(monkeypatch: MonkeyPatch) -> None:
    """fixed_size は chunk_size / overlap だけで決まり、min_chars 吸収を通さない。"""

    def fake_fixed_size(*_: object, **__: object) -> list[core_chunking.Chunk]:
        return [
            core_chunking.Chunk(
                text="十分な長さの固定長 chunk",
                index=0,
                start_offset=0,
                end_offset=12,
                metadata={"chunk_group_id": "g", "content_kind": "text"},
            ),
            core_chunking.Chunk(
                text="短い",
                index=1,
                start_offset=13,
                end_offset=15,
                metadata={"chunk_group_id": "g", "content_kind": "text"},
            ),
        ]

    monkeypatch.setattr(core_chunking, "_chunk_fixed_size", fake_fixed_size)

    chunks = core_chunking.chunk_extraction_with_strategy(
        _sample_extraction(),
        strategy="fixed_size",
        chunk_size=400,
        overlap=0,
        min_chars=200,
    )

    assert [chunk.text for chunk in chunks] == ["十分な長さの固定長 chunk", "短い"]
    assert all(chunk.metadata["chunk_strategy"] == "fixed_size" for chunk in chunks)


def test_resolve_chunking_params_reads_settings() -> None:
    """Settings から chunking パラメータを解決する。"""
    settings = Settings(
        rag_chunking_strategy="recursive_character",
        rag_chunk_size=1000,
        rag_chunk_overlap=80,
        rag_chunk_min_chars=30,
        rag_chunk_delimiter="---",
    )
    params = resolve_chunking_params(settings)
    assert params.strategy == "recursive_character"
    assert params.chunk_size == 1000
    assert params.overlap == 80
    assert params.min_chars == 30
    assert params.delimiter == "---"


def test_chunking_strategy_order_puts_small_to_big_where_parent_child_was() -> None:
    """親子階層（small-to-big）は、削除した親子階層の位置(再帰文字分割の次)に並ぶ。"""
    assert CHUNKING_STRATEGY_ORDER == (
        "structure_aware",
        "recursive_character",
        "small_to_big",
        "markdown_heading",
        "page_level",
        "fixed_size",
        "fixed_delimiter",
    )
    assert "hierarchical_parent_child" not in CHUNKING_STRATEGIES


def test_resolve_chunking_params_reads_small_to_big_params() -> None:
    """親子階層（small-to-big）の 5 項目を Settings から解決する。既定は rag_poc と同じ。"""
    defaults = resolve_chunking_params(Settings(rag_chunking_strategy="small_to_big"))
    assert defaults.strategy == "small_to_big"
    assert defaults.small_to_big == SmallToBigParams(
        child_target_chars=1000,
        table_child_target_chars=3000,
        parent_target_chars=6000,
        parent_max_pages=3,
        parent_max_children=12,
    )
    custom = resolve_chunking_params(
        Settings(
            rag_chunking_strategy="small_to_big",
            rag_chunk_child_target_chars=600,
            rag_chunk_table_child_target_chars=1200,
            rag_chunk_parent_target_chars=3000,
            rag_chunk_parent_max_pages=2,
            rag_chunk_parent_max_children=6,
        )
    )
    assert custom.small_to_big == SmallToBigParams(600, 1200, 3000, 2, 6)


def test_chunking_runtime_settings_orders_and_marks_selected() -> None:
    """runtime snapshot は戦略を既定順で並べ、選択戦略を 1 件だけ marked にする。"""
    settings = Settings(rag_chunking_strategy="page_level")
    runtime = chunking_runtime_settings(settings)
    assert tuple(status.name for status in runtime.strategies) == CHUNKING_STRATEGY_ORDER
    selected = [status.name for status in runtime.strategies if status.selected]
    assert selected == ["page_level"]


def test_normalize_chunking_strategy_defaults_to_small_to_big() -> None:
    # 未知の値は既定(親子階層（small-to-big）。#594)へ寄せる。
    assert normalize_chunking_strategy("nope") == "small_to_big"
    assert normalize_chunking_strategy("page_level") == "page_level"
    # 撤去済み戦略は後継へ読み替える。
    assert normalize_chunking_strategy("sentence_window") == "recursive_character"
    # 削除した親子階層は親子階層（small-to-big）として扱う(#271)。
    assert normalize_chunking_strategy("hierarchical_parent_child") == "small_to_big"
    assert normalize_chunking_strategy("small_to_big") == "small_to_big"


@pytest.mark.parametrize(
    ("text", "size", "overlap", "expected"),
    [
        ("abcdefghij", 8, 6, ["abcdefgh", "cdefghij"]),
        ("abcdefgh", 8, 6, ["abcdefgh"]),
        ("abc", 8, 6, ["abc"]),
        ("abcdefghij", 8, 0, ["abcdefgh", "ij"]),
        ("abcde", 4, 3, ["abcd", "bcde"]),
        ("", 8, 6, []),
    ],
)
def test_fixed_size_stops_when_the_last_window_covers_the_end(
    text: str, size: int, overlap: int, expected: list[str]
) -> None:
    chunks = chunk_extraction_with_strategy(
        StructuredExtraction(raw_text=text),
        strategy="fixed_size",
        chunk_size=size,
        overlap=overlap,
    )
    assert [chunk.text for chunk in chunks] == expected
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))
    assert all(chunk.metadata["text_chars"] == len(chunk.text) for chunk in chunks)
    covered = {offset for chunk in chunks for offset in range(chunk.start_offset, chunk.end_offset)}
    assert covered == set(range(len(text)))


def test_fixed_size_offsets_follow_trimmed_window_text() -> None:
    source = "abcd efgh ijkl"
    chunks = chunk_extraction_with_strategy(
        StructuredExtraction(raw_text=source),
        strategy="fixed_size",
        chunk_size=5,
        overlap=1,
    )
    assert chunks[1].text == "efgh"
    assert (chunks[1].start_offset, chunks[1].end_offset) == (5, 9)
    assert all(source[chunk.start_offset : chunk.end_offset] == chunk.text for chunk in chunks)
