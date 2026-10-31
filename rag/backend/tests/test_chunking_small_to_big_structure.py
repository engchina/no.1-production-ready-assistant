"""親子階層（small-to-big）を Docling のレイアウト以外の抽出の構造から作る検証（#3377）。

Markdown・見出しの無い本文・外部の解析器（Unstructured など）の要素・表・構造化レコード JSONL の
どれでも親子に分割し、回答の文脈で親の本文を復元できることを確かめる。データはすべて合成。
"""

from __future__ import annotations

from typing import Any, cast

from rag_parser_core.extraction import infer_document_elements
from rag_parser_core.registry import run_external_adapter
from rag_parser_core.structured_records import (
    DocumentLine,
    RecordField,
    RecordLocation,
    RowLine,
    dump_structured_records,
)

from app.rag.answer_engine import _stored_child, _stored_parents
from app.rag.chunking import Chunk
from app.rag.chunking_small_to_big import (
    PARENT_SOURCE_STRUCTURE,
    SMALL_TO_BIG_PARENT_GROUP_KIND,
    build_parent_child_chunks,
    small_to_big_parent_source,
)
from app.rag.chunking_strategy import SmallToBigParams
from app.schemas.extraction import DocumentElement, StructuredExtraction
from app.schemas.search import RetrievedChunk

SENTENCE = "これは合成の本文の文です。"


def _markdown_extraction(text: str) -> StructuredExtraction:
    """標準の解析(本文から要素を推定する)と同じ形の抽出。"""
    return StructuredExtraction(raw_text=text, elements=infer_document_elements(text))


def _parents(chunks: list[Chunk]) -> dict[str, list[Chunk]]:
    groups: dict[str, list[Chunk]] = {}
    for chunk in chunks:
        groups.setdefault(str(chunk.metadata["parent_chunk_id"]), []).append(chunk)
    return groups


def _assert_parent_child_metadata(chunks: list[Chunk]) -> None:
    assert chunks
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        metadata = chunk.metadata
        assert metadata["chunk_strategy"] == "small_to_big"
        assert metadata["chunk_group_kind"] == SMALL_TO_BIG_PARENT_GROUP_KIND
        assert metadata["chunk_group_id"] == metadata["parent_chunk_id"]
        assert chunk.text in str(metadata["parent_text"])
        assert "chunk_strategy_fallback_reason" not in metadata
        assert "chunk_strategy_requested" not in metadata
    for members in _parents(chunks).values():
        assert [member.metadata["chunk_part_index"] for member in members] == list(
            range(1, len(members) + 1)
        )
        assert {member.metadata["chunk_part_count"] for member in members} == {len(members)}
        assert len({member.metadata["parent_text"] for member in members}) == 1


def test_markdown_headings_make_parents_per_section_and_attach_headings() -> None:
    long_body = SENTENCE * 40
    text = (
        "# 文書の題\n\n"
        f"## 節 A\n\n{long_body}\n\n"
        f"### 節 A-1\n\n{SENTENCE}\n\n"
        f"## 節 B\n\n{SENTENCE}\n"
    )
    extraction = _markdown_extraction(text)
    assert small_to_big_parent_source(extraction) == PARENT_SOURCE_STRUCTURE

    chunks = build_parent_child_chunks(extraction, params=SmallToBigParams(child_target_chars=300))

    _assert_parent_child_metadata(chunks)
    # 見出しだけの子は作らず、見出しは続く本文の子の先頭に入る。
    assert all(chunk.metadata.get("element_kinds") != "title" for chunk in chunks)
    assert chunks[0].text.startswith("# 文書の題\n## 節 A\n")
    # 子は子の目標文字数の中に収まり、重ならない。
    assert all(len(chunk.text) <= 300 + 20 for chunk in chunks)
    # 親は節(H2)をまたがない。文書の題と節 A・その下の節 A-1 は 1 つの親、節 B は別の親。
    sections = [
        {str(member.metadata["section_path"]) for member in members}
        for members in _parents(chunks).values()
    ]
    assert sections == [
        {"文書の題 > 節 A", "文書の題 > 節 A > 節 A-1"},
        {"文書の題 > 節 B"},
    ]
    section_a = chunks[0].metadata["parent_text"]
    assert "節 A-1" in str(section_a) and "節 B" not in str(section_a)
    # 子は元の要素を持つ(回答の根拠の範囲・複数レシピの融合に使う)。
    assert all(chunk.metadata.get("element_ids") for chunk in chunks)


def test_plain_text_without_headings_groups_children_by_size_only() -> None:
    extraction = StructuredExtraction(raw_text="\n\n".join(SENTENCE * 30 for _ in range(8)))

    params = SmallToBigParams(
        child_target_chars=400, parent_target_chars=1200, parent_max_children=3
    )
    chunks = build_parent_child_chunks(extraction, params=params)

    _assert_parent_child_metadata(chunks)
    parents = _parents(chunks)
    assert len(parents) > 1
    for members in parents.values():
        assert len(members) <= 3
        parent_text = str(members[0].metadata["parent_text"])
        assert len(parent_text) <= 1200 or len(members) == 1
        assert parent_text == "\n\n".join(member.text for member in members)


def test_external_parser_elements_respect_page_limit_and_keep_locations() -> None:
    """外部の解析器の要素(見出し・本文・リスト、頁と章節つき)を親子にする。"""
    elements = [
        DocumentElement(kind="title", text="第1章", page_number=1, section_path=["第1章"]),
        DocumentElement(kind="text", text=SENTENCE * 3, page_number=1, section_path=["第1章"]),
        DocumentElement(
            kind="list", text="- 項目 1\n- 項目 2", page_number=2, section_path=["第1章"]
        ),
        DocumentElement(kind="text", text=SENTENCE * 2, page_number=3, section_path=["第1章"]),
        DocumentElement(kind="title", text="第2章", page_number=3, section_path=["第2章"]),
        DocumentElement(kind="text", text=SENTENCE, page_number=3, section_path=["第2章"]),
    ]
    extraction = StructuredExtraction(
        raw_text="\n".join(element.text for element in elements),
        elements=elements,
        parser_artifacts={"source_parser": "external"},
    )

    chunks = build_parent_child_chunks(extraction, params=SmallToBigParams(parent_max_pages=2))

    _assert_parent_child_metadata(chunks)
    for members in _parents(chunks).values():
        pages = [
            page
            for member in members
            for page in (member.metadata.get("page_start"), member.metadata.get("page_end"))
            if isinstance(page, int)
        ]
        assert max(pages) - min(pages) + 1 <= 2
        assert len({str(m.metadata["section_path"]).split(" > ")[0] for m in members}) == 1
    assert all(chunk.metadata.get("page_start") for chunk in chunks)
    assert {str(chunk.metadata["section_path"]) for chunk in chunks} == {"第1章", "第2章"}


def _table(rows: int) -> str:
    lines = ["| 列 A | 列 B |", "| --- | --- |"]
    lines.extend(f"| 値 {index:03d} | {SENTENCE} |" for index in range(rows))
    return "\n".join(lines)


def test_tables_use_table_child_target_and_repeat_header() -> None:
    small_table = _table(12)
    large_table = _table(60)
    elements = [
        DocumentElement(kind="text", text=SENTENCE, section_path=["表"]),
        DocumentElement(kind="table", text=small_table, section_path=["表"]),
        DocumentElement(kind="table", text=large_table, section_path=["表"]),
    ]
    extraction = StructuredExtraction(
        raw_text="\n".join(element.text for element in elements), elements=elements
    )
    params = SmallToBigParams(child_target_chars=300, table_child_target_chars=1200)
    assert 300 < len(small_table) <= 1200 < len(large_table)

    chunks = build_parent_child_chunks(extraction, params=params)

    _assert_parent_child_metadata(chunks)
    tables = [chunk for chunk in chunks if chunk.metadata.get("content_kind") == "table"]
    # 子の目標を超えても表の子の目標の中なら、表は 1 つの子のまま。
    assert tables[0].text == small_table
    # 表の子の目標を超える表は行のまとまりに分け、各部分に列見出しを繰り返す。
    parts = tables[1:]
    assert len(parts) > 1
    assert all(part.text.startswith("| 列 A | 列 B |\n| --- | --- |") for part in parts)
    assert all(part.metadata["chunk_size_target"] == 1200 for part in tables)
    # 表は他の要素と同じ子に結合しない。
    assert chunks[0].text == SENTENCE
    assert chunks[0].metadata["content_kind"] == "text"
    assert all(chunk.text.startswith("| 列 A |") for chunk in tables)


def _csv_extraction(count: int) -> StructuredExtraction:
    lines: list[DocumentLine | RowLine] = [
        DocumentLine(
            record_id="document",
            source_type="csv",
            converter="csv_to_jsonl",
            converter_version="v2",
            header_row=1,
        )
    ]
    for index in range(1, count + 1):
        lines.append(
            RowLine(
                record_id=f"row-{index:06d}",
                source_type="csv",
                location=RecordLocation(row_start=index + 1, row_end=index + 1),
                fields=[
                    RecordField(name="項目", position=1, value=f"値 {index}"),
                    RecordField(name="備考", position=2, value="合成の備考"),
                ],
            )
        )
    result = run_external_adapter(
        "structured_records_jsonl", dump_structured_records(lines), None, ""
    )
    assert result.extraction is not None
    return StructuredExtraction.model_validate(result.extraction.model_dump())


def test_structured_records_keep_one_record_per_child() -> None:
    """構造化レコード JSONL(CSV)は 1 記録 = 1 子、親は続く記録のまとまり(#2457・#3377)。"""
    extraction = _csv_extraction(7)

    chunks = build_parent_child_chunks(extraction, params=SmallToBigParams(parent_max_children=3))

    _assert_parent_child_metadata(chunks)
    assert [chunk.text for chunk in chunks] == [
        f"項目: 値 {index} / 備考: 合成の備考" for index in range(1, 8)
    ]
    assert [len(members) for members in _parents(chunks).values()] == [3, 3, 1]
    assert all(chunk.metadata.get("chunk_record_boundary") is True for chunk in chunks)


def test_parent_ids_are_deterministic() -> None:
    extraction = _markdown_extraction(f"# 題\n\n## 節\n\n{SENTENCE * 10}\n")
    first = build_parent_child_chunks(extraction)
    second = build_parent_child_chunks(extraction)
    assert [chunk.metadata["parent_chunk_id"] for chunk in first] == [
        chunk.metadata["parent_chunk_id"] for chunk in second
    ]


def test_answer_restores_parent_text_for_non_layout_children() -> None:
    """回答の文脈は、見つかった子ではなく親(節)の本文を使う(Docling の親子と同じ経路)。"""
    text = f"# 題\n\n## 節 A\n\n{SENTENCE * 30}\n\n{'別の段落の文です。' * 30}\n"
    chunks = build_parent_child_chunks(
        _markdown_extraction(text), params=SmallToBigParams(child_target_chars=300)
    )
    hit = chunks[-1]
    assert len(_parents(chunks)) == 1 and len(chunks) > 1
    retrieved = RetrievedChunk(
        document_id="doc-1",
        chunk_id=f"doc-1:cs:{hit.index}",
        text=hit.text,
        score=1.0,
        metadata=dict(hit.metadata),
    )

    class _State:
        chunks = {retrieved.chunk_id: retrieved}

    child = _stored_child(retrieved, rrf_score=1.0)
    (parent,) = _stored_parents([child], cast(Any, _State()))
    assert parent.text == hit.metadata["parent_text"]
    assert SENTENCE in parent.text and hit.text in parent.text
