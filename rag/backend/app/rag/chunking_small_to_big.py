"""親子階層（small-to-big）(rag_poc の Small-to-Big 親子分割)を backend の Chunk へ写す。

親は回答の文脈に渡す大きい単位、子は検索に当てる小さい単位(LangChain の ParentDocumentRetriever・
LlamaIndex の階層の分割と同じ考え方)。子だけを索引単位として返し、親の本文と ID は子の metadata
(``chunk_group_id`` / ``parent_text``)に保持して、既存の group sibling 展開と回答文脈で使う。
親子の作り方は抽出の形で選ぶ(解析エンジンの名前では選ばない。#3377):

- 解析の ``parser_artifacts["layout_records"]``(Docling の LayoutRecord)があれば、
  ``rag_engine.chunking.build_small_to_big_chunks`` で親子チャンクを作る。Oracle Text と embedding
  には rag_poc の search_text を使う。文書の 1 ページ目の本文(回答の「文書の背景」)は先頭の chunk
  にだけ載せ、保存のときに chunk set へ 1 つだけ移す(#557。検索用 text には入れない)。
- Excel の前処理(``excel_to_jsonl``)の行の記録(``rag_parser_core.sheet_records``)は、子は 1 記録
  (表の 1 行・手順書の 1 手順)= 1 chunk、親は同じシート(手順書は同じ章)の続く記録を
  ``parent_target_chars`` / ``parent_max_children`` までまとめた表の一部(#1349)。表頭より上の行
  (前書き)は記録と混ぜず、単独の子(親は自分)にする。
- それ以外のすべての抽出(Markdown・テキスト・Unstructured・OCI の解析・Office など)は、抽出の構造
  から親子を作る(``build_structure_parent_child_chunks``)。子は構造認識と同じ要素の分割を子の目標
  文字数で行い(表は表の子の目標。列見出しを繰り返す)、構造化レコードの記録の境界がある抽出は
  1 記録 = 1 子にする(#2457)。親は続く子を、文書を分ける最初の見出しの段(節)をまたがず、親の
  文字数・子の数・頁の上限の中でまとめる。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

from rag_parser_core.extraction import (
    StructuredExtraction,
    is_derived_content,
    strip_formatting_marks,
)
from rag_parser_core.parsed_table import ParsedCell, ParsedTableGrid, grid_rows_metadata
from rag_parser_core.sheet_records import SHEET_RECORD_CONTENT_KIND
from rag_parser_core.structured_records import has_record_boundaries
from rag_pipeline_core.chunking import (
    FIGURE_TEXT_SOURCE_KEY,
    FIGURE_TEXT_SOURCE_OCR,
    FIGURE_TEXT_SOURCE_VISION,
    Chunk,
    chunk_derived_elements,
    chunk_extraction,
    chunk_extraction_with_strategy,
    without_derived_elements,
)

if TYPE_CHECKING:
    from rag_engine.chunking.constants import ChunkingConfig, DocumentChunk

    from app.rag.chunking_strategy import SmallToBigParams

SMALL_TO_BIG_STRATEGY = "small_to_big"
LAYOUT_ARTIFACT = "layout_records"
# chunk の source_parser(解析エンジンの名前 + "_layout")。Docling は "docling_layout"。
LAYOUT_SOURCE_PARSER = "docling_layout"
# record の engine → 分割の結果に出す解析エンジンの表示名。
_ENGINE_LABELS = {"docling": "Docling"}
# 検索・embedding 用 text(rag_poc の retrieval_text)を載せる metadata key。
ENGINE_SEARCH_TEXT_KEY = "engine_search_text"
# 文書の物理 1 ページ目の本文(回答の「文書の背景」。rag_poc の first_page_context)を JSON で
# 載せる metadata key(#557)。chunk ごとに同じ本文(最大 8000 文字)を持たせないよう、先頭の
# chunk にだけ載せる。保存では chunk の行から外し、chunk set に 1 つだけ保存する。
FIRST_PAGE_CONTEXT_KEY = "first_page_context_json"
# 親子を作った入力(取込の trace の ``chunk_parent_source``)。
PARENT_SOURCE_LAYOUT = "layout_records"
PARENT_SOURCE_SHEET_RECORDS = "sheet_records"
PARENT_SOURCE_STRUCTURE = "extraction_structure"
# 行の記録の抽出(``sheet_records_extraction``)が parser_artifacts に刻む版の key。
SHEET_RECORDS_ARTIFACT_KEY = "sheet_records_format_version"
# 行の記録の子に、親(表の一部)のシートの場所を残す key。回答の文脈の親の出典に使う(#1349)。
PARENT_ROW_START_KEY = "parent_row_start"
PARENT_ROW_END_KEY = "parent_row_end"
PARENT_CELL_RANGE_KEY = "parent_cell_range"
SMALL_TO_BIG_PARENT_GROUP_KIND = "small_to_big_parent"


def layout_source_parser(engine: str) -> str:
    """親子階層の chunk の source_parser。record の engine(解析エンジン)が分かる値にする。"""
    name = (engine or "docling").strip().casefold() or "docling"
    return LAYOUT_SOURCE_PARSER if name == "docling" else f"{name}_layout"


def has_layout_records(extraction: StructuredExtraction) -> bool:
    layout = extraction.parser_artifacts.get(LAYOUT_ARTIFACT)
    return isinstance(layout, Mapping) and bool(layout.get("records"))


def has_sheet_records(extraction: StructuredExtraction) -> bool:
    """Excel の前処理の行の記録(``sheet_records_extraction``)の抽出か(#1349)。"""
    return extraction.parser_artifacts.get(SHEET_RECORDS_ARTIFACT_KEY) is not None and any(
        element.content_kind == SHEET_RECORD_CONTENT_KIND for element in extraction.elements
    )


def small_to_big_parent_source(extraction: StructuredExtraction) -> str:
    """親子階層の親子をどの入力から作るか(``build_parent_child_chunks`` の振り分けと同じ順)。"""
    if has_layout_records(extraction):
        return PARENT_SOURCE_LAYOUT
    if has_sheet_records(extraction):
        return PARENT_SOURCE_SHEET_RECORDS
    return PARENT_SOURCE_STRUCTURE


def small_to_big_chunking_config(params: SmallToBigParams | None = None) -> ChunkingConfig:
    """設定の 5 項目を rag_engine の ChunkingConfig へ写す。

    検索用テキストの 3 項目(contextual search text)は rag_poc と同じく既定値のまま使う。
    """
    from rag_engine.chunking.constants import ChunkingConfig

    if params is None:
        return ChunkingConfig().validate()
    return ChunkingConfig(
        child_target_chars=params.child_target_chars,
        table_child_target_chars=params.table_child_target_chars,
        parent_target_chars=params.parent_target_chars,
        parent_max_pages=params.parent_max_pages,
        parent_max_children=params.parent_max_children,
    ).validate()


def build_parent_child_chunks(
    extraction: StructuredExtraction,
    *,
    source_name: str = "",
    params: SmallToBigParams | None = None,
    diagnostics: dict[str, int] | None = None,
) -> list[Chunk]:
    """抽出を親子に分割し、子チャンクを索引用 Chunk として返す。

    ``params`` は文書分割の設定(親子階層（small-to-big）の 5 項目)。None は rag_poc の既定値。
    入力の振り分けは ``small_to_big_parent_source``(どの解析の結果でも親子にする。#3377)。
    """
    # oracle client からも search_text 判定で import されるため、分割実装は使用時に読む。
    from rag_engine.chunking.builder import build_small_to_big_chunks
    from rag_engine.chunking.constants import CHILD_CHUNK_LEVEL

    derived = [
        element
        for element in extraction.elements
        if is_derived_content(element.content_kind, element.metadata)
    ]
    if derived:
        originals = build_parent_child_chunks(
            without_derived_elements(extraction),
            source_name=source_name,
            params=params,
            diagnostics=diagnostics,
        )
        return [
            *originals,
            *chunk_derived_elements(
                derived,
                start_index=len(originals),
                chunk_size=small_to_big_chunking_config(params).child_target_chars,
                strategy=SMALL_TO_BIG_STRATEGY,
            ),
        ]
    source = small_to_big_parent_source(extraction)
    if source == PARENT_SOURCE_SHEET_RECORDS:
        return build_sheet_parent_child_chunks(extraction, params=params)
    if source == PARENT_SOURCE_STRUCTURE:
        return build_structure_parent_child_chunks(extraction, params=params)
    layout = cast("Mapping[str, Any]", extraction.parser_artifacts[LAYOUT_ARTIFACT])
    records = [dict(record) for record in _items(layout.get("records")) if isinstance(record, dict)]
    for record in records:
        # 要素の本文と同じく、書式の飾りの結合の線を分割と索引の本文に入れない(#3354)。
        # 保存済みの抽出を分割し直すときにも当てる(record の raw は解析の出力のまま)。
        if isinstance(record.get("text"), str):
            record["text"] = strip_formatting_marks(record["text"])
    pages = [dict(page) for page in _items(layout.get("pages")) if isinstance(page, dict)]
    engines = sorted({str(record.get("engine") or "docling") for record in records})
    payload: dict[str, Any] = {
        "pdf_name": source_name,
        "records": records,
        "pages": pages,
        "engines": [
            {"engine": engine, "label": _ENGINE_LABELS.get(engine, engine)} for engine in engines
        ],
        "classification": {},
        "document_metadata": {},
    }
    chunks = build_small_to_big_chunks(
        payload,
        source_run_id=_source_run_id(records),
        selected_engine_ids=engines,
        config=small_to_big_chunking_config(params),
        source_page_count=len(pages),
        diagnostics=diagnostics,
    )
    parents = {chunk.chunk_id: chunk for chunk in chunks if chunk.chunk_level != CHILD_CHUNK_LEVEL}
    children = [chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]
    page_sizes = {
        int(page.get("page") or 0): (page.get("width"), page.get("height")) for page in pages
    }
    tables = _layout_tables(records)
    backend_chunks = [
        _backend_chunk(child, index, parents.get(child.parent_chunk_id), page_sizes, tables)
        for index, child in enumerate(children)
    ]
    # rag_engine は子ごとに同じ 1 ページ目の本文を document に持たせるので、先頭の子から
    # 1 つだけ取る。
    document = children[0].metadata.get("document") if children else None
    first_page = document.get("first_page_context") if isinstance(document, Mapping) else None
    if backend_chunks and isinstance(first_page, Mapping):
        backend_chunks[0].metadata[FIRST_PAGE_CONTEXT_KEY] = json.dumps(
            first_page, ensure_ascii=False
        )
    return backend_chunks


def build_structure_parent_child_chunks(
    extraction: StructuredExtraction,
    *,
    params: SmallToBigParams | None = None,
) -> list[Chunk]:
    """抽出の構造(見出しの階層・要素の順・頁)から親子に分割する(#3377)。

    解析エンジンを問わない汎用の親子。子は構造認識と同じ要素の分割(同じ節・同じ種類の要素だけを
    つなぎ、表は他の要素と結合しない)を ``child_target_chars`` で、表は ``table_child_target_chars``
    で行のまとまりに分ける(列見出しを繰り返す)。子どうしは重ねない。構造化レコード JSONL の記録の
    境界がある抽出は 1 記録 = 1 子にする(#2457)。要素が無く本文だけの抽出は、本文の見出しと文の
    境目で子に分ける。

    親は続く子を、節(文書を分ける最初の見出しの段。``_section_unit_depth``)をまたがず、本文の合計が
    ``parent_target_chars`` 以下・``parent_max_children`` 件以下・``parent_max_pages`` 頁以下になる
    までまとめる。節より上の段だけの子(文書の題・前書き)は、続く節の先頭の親に入れる。見出しの無い
    文書は順と大きさだけでまとめる。親の本文は子の本文をつないだもので、子の metadata
    (``parent_text``)に持たせ、回答の文脈で親を復元する。
    """
    from app.rag.chunking_strategy import SmallToBigParams

    resolved = params or SmallToBigParams()
    if has_record_boundaries(extraction):
        children = chunk_extraction_with_strategy(extraction, strategy="fixed_delimiter")
    else:
        children = chunk_extraction(
            extraction,
            chunk_size=resolved.child_target_chars,
            overlap=0,
            table_chunk_size=resolved.table_child_target_chars,
        )
    children = _attach_headings(children)
    depth = _section_unit_depth(children)
    groups: list[list[Chunk]] = []
    for chunk in children:
        current = groups[-1] if groups else None
        if current is not None and _joins_structure_parent(current, chunk, resolved, depth):
            current.append(chunk)
        else:
            groups.append([chunk])
    result: list[Chunk] = []
    for group in groups:
        parent_text = "\n\n".join(chunk.text for chunk in group)
        parent_id = _structure_parent_id(group, parent_text)
        for part_index, chunk in enumerate(group, start=1):
            result.append(
                replace(
                    chunk,
                    index=len(result),
                    metadata={
                        **chunk.metadata,
                        "chunk_strategy": SMALL_TO_BIG_STRATEGY,
                        "parent_chunk_id": parent_id,
                        "chunk_group_id": parent_id,
                        "chunk_group_kind": SMALL_TO_BIG_PARENT_GROUP_KIND,
                        "chunk_part_index": part_index,
                        "chunk_part_count": len(group),
                        "parent_text": parent_text,
                        **_parent_page_metadata(group),
                        "engine_chunk_seq": len(result),
                    },
                )
            )
    return result


def _parent_page_metadata(group: list[Chunk]) -> dict[str, int]:
    from app.rag.context_page_scope import positive

    starts = [
        positive(chunk.metadata.get("page_start") or chunk.metadata.get("page_number"))
        for chunk in group
    ]
    ends = [
        positive(
            chunk.metadata.get("page_end")
            or chunk.metadata.get("page_start")
            or chunk.metadata.get("page_number")
        )
        for chunk in group
    ]
    if not starts or any(value is None for value in [*starts, *ends]):
        return {}
    return {
        "parent_page_start": min(value for value in starts if value is not None),
        "parent_page_end": max(value for value in ends if value is not None),
    }


def _section_parts(chunk: Chunk) -> tuple[str, ...]:
    """子の見出しの階層(``section_path`` は「 > 」でつないだ文字列)。"""
    return tuple(
        part.strip()
        for part in str(chunk.metadata.get("section_path") or "").split(" > ")
        if part.strip()
    )


def _is_heading_only(chunk: Chunk) -> bool:
    return chunk.metadata.get("element_kinds") == "title"


def _extends_section(heading: Chunk, chunk: Chunk) -> bool:
    """``chunk`` が ``heading`` の見出しの下(同じ節か、その下の節)にあるか。"""
    path = _section_parts(heading)
    return bool(path) and _section_parts(chunk)[: len(path)] == path


def _attach_headings(children: list[Chunk]) -> list[Chunk]:
    """見出しだけの子を、その見出しの下の最初の本文の子の先頭に付ける。

    構造認識の要素の分割は、見出しと続く本文が子の目標を超えると見出しだけの子を作る。見出しだけの
    子は検索に当てる単位にならないので、Docling の親子と同じく見出しを本文の子に含める。表・記録の
    子(本文を行として読む)と、下に本文の無い見出しには付けずにそのまま残す。
    """
    result: list[Chunk] = []
    pending: list[Chunk] = []
    for chunk in children:
        if _is_heading_only(chunk):
            if pending and not _extends_section(pending[-1], chunk):
                result.extend(pending)
                pending = []
            pending.append(chunk)
            continue
        attachable = chunk.metadata.get("content_kind") not in {"table", SHEET_RECORD_CONTENT_KIND}
        if pending and attachable and _extends_section(pending[-1], chunk):
            chunk = _with_heading_prefix(pending, chunk)
        else:
            result.extend(pending)
        pending = []
        result.append(chunk)
    result.extend(pending)
    return [replace(chunk, index=index) for index, chunk in enumerate(result)]


def _with_heading_prefix(headings: list[Chunk], chunk: Chunk) -> Chunk:
    text = "\n".join([*(heading.text for heading in headings), chunk.text])
    element_ids = [
        element_id
        for item in (*headings, chunk)
        for element_id in str(item.metadata.get("element_ids") or "").split(",")
        if element_id
    ]
    kinds = {
        kind
        for item in (*headings, chunk)
        for kind in str(item.metadata.get("element_kinds") or "").split(",")
        if kind
    }
    return replace(
        chunk,
        text=text,
        start_offset=headings[0].start_offset,
        metadata={
            **chunk.metadata,
            "element_ids": ",".join(dict.fromkeys(element_ids)),
            "element_kinds": ",".join(sorted(kinds)),
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "text_chars": len(text),
        },
    )


def _section_unit_depth(children: list[Chunk]) -> int:
    """親がまたがない節の段(文書を 2 つ以上に分ける最初の見出しの段)。見出しが無ければ 0。

    文書の題(H1)が 1 つだけで、その下の H2 で章を分ける文書は 2、題が無く H2 で分ける文書は 1。
    """
    paths = [path for chunk in children if (path := _section_parts(chunk))]
    if not paths:
        return 0
    deepest = max(len(path) for path in paths)
    for depth in range(1, deepest + 1):
        if len({path[:depth] for path in paths if len(path) >= depth}) > 1:
            return depth
    return deepest


def _page_bounds(chunk: Chunk) -> tuple[int, int] | None:
    start = chunk.metadata.get("page_start") or chunk.metadata.get("page_number")
    end = chunk.metadata.get("page_end") or start
    if not isinstance(start, int) or not isinstance(end, int) or start <= 0:
        return None
    return start, max(start, end)


def _joins_structure_parent(
    group: list[Chunk], chunk: Chunk, params: SmallToBigParams, depth: int
) -> bool:
    """続く子を同じ親にまとめるか(同じ節で、親の上限の中に収まる)。"""
    if len(group) >= params.parent_max_children:
        return False
    joined = sum(len(item.text) for item in group) + 2 * len(group) + len(chunk.text)
    if joined > params.parent_target_chars:
        return False
    pages = [bounds for item in (*group, chunk) if (bounds := _page_bounds(item))]
    if pages and (
        max(end for _, end in pages) - min(start for start, _ in pages) + 1
        > params.parent_max_pages
    ):
        return False
    previous = _section_parts(group[-1])[:depth]
    current = _section_parts(chunk)[:depth]
    if previous == current:
        return True
    # 節より上の段だけの子(文書の題・前書き)は、続く節の先頭に付ける。
    return (
        all(len(_section_parts(item)) < depth for item in group)
        and current[: len(previous)] == previous
    )


def _structure_parent_id(group: list[Chunk], parent_text: str) -> str:
    payload = {
        "group_kind": SMALL_TO_BIG_PARENT_GROUP_KIND,
        "element_ids": [chunk.metadata.get("element_ids") for chunk in group],
        "offsets": [[chunk.start_offset, chunk.end_offset] for chunk in group],
        "text_sha256": hashlib.sha256(parent_text.encode("utf-8")).hexdigest(),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:32]


def build_sheet_parent_child_chunks(
    extraction: StructuredExtraction,
    *,
    params: SmallToBigParams | None = None,
) -> list[Chunk]:
    """Excel の行の記録を、行を子・表の一部を親にして分割する(#1349)。

    子は構造認識と同じ 1 記録 = 1 chunk(列名つきの値と、行のシート・セル範囲を持つ。他の行と
    結合しない)。親は同じシート・同じ章の続く記録を、本文の合計が ``parent_target_chars`` 以下・
    ``parent_max_children`` 件以下になるまでまとめる。親の本文と場所は子の metadata
    (``parent_text`` / ``parent_cell_range`` など)に持ち、回答の文脈で親を復元する。
    前書き(表頭より上の行)など記録でない chunk は、親を自分 1 つにする。
    """
    from app.rag.chunking_strategy import SmallToBigParams

    resolved = params or SmallToBigParams()
    children = chunk_extraction(extraction, chunk_size=resolved.child_target_chars, overlap=0)
    groups: list[list[Chunk]] = []
    for chunk in children:
        current = groups[-1] if groups else None
        if current is not None and _joins_sheet_parent(current, chunk, resolved):
            current.append(chunk)
        else:
            groups.append([chunk])
    result: list[Chunk] = []
    for group in groups:
        parent_text = "\n".join(chunk.text for chunk in group)
        parent_id = _sheet_parent_id(group, parent_text)
        location = _sheet_parent_location(group)
        for part_index, chunk in enumerate(group, start=1):
            result.append(
                replace(
                    chunk,
                    index=len(result),
                    metadata={
                        **chunk.metadata,
                        "chunk_strategy": SMALL_TO_BIG_STRATEGY,
                        "parent_chunk_id": parent_id,
                        "chunk_group_id": parent_id,
                        "chunk_group_kind": SMALL_TO_BIG_PARENT_GROUP_KIND,
                        "chunk_part_index": part_index,
                        "chunk_part_count": len(group),
                        "parent_text": parent_text,
                        **_parent_page_metadata(group),
                        "engine_chunk_seq": len(result),
                        **location,
                    },
                )
            )
    return result


def _parent_section(chunk: Chunk) -> str:
    """親をまとめる節(シートと、手順書なら章。``section_path`` の先頭の 2 つ)。"""
    parts = str(chunk.metadata.get("section_path") or "").split(" > ")
    return " > ".join(parts[:2])


def _joins_sheet_parent(group: list[Chunk], chunk: Chunk, params: SmallToBigParams) -> bool:
    """続く記録を同じ親にまとめるか(同じシート・同じ表・同じ章の記録で、親の上限の中に収まる)。"""
    first = group[0]
    if not all(
        item.metadata.get("content_kind") == SHEET_RECORD_CONTENT_KIND for item in (first, chunk)
    ):
        return False
    if first.metadata.get("sheet_name") != chunk.metadata.get("sheet_name"):
        return False
    # 1 シートを複数の表に分けたときは、表をまたいで親にしない(#2516)。
    if first.metadata.get("sheet_table_index") != chunk.metadata.get("sheet_table_index"):
        return False
    if _parent_section(first) != _parent_section(chunk):
        return False
    if len(group) >= params.parent_max_children:
        return False
    joined = sum(len(item.text) for item in group) + len(group) + len(chunk.text)
    return joined <= params.parent_target_chars


def _sheet_parent_id(group: list[Chunk], parent_text: str) -> str:
    payload = {
        "group_kind": SMALL_TO_BIG_PARENT_GROUP_KIND,
        "sheet_name": group[0].metadata.get("sheet_name"),
        "element_ids": [chunk.metadata.get("element_ids") for chunk in group],
        "text_sha256": hashlib.sha256(parent_text.encode("utf-8")).hexdigest(),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:32]


def _sheet_parent_location(group: list[Chunk]) -> dict[str, str | int]:
    """親(表の一部)の行の範囲とセル範囲。子のシートが 1 つで、場所が分かるときだけ。"""
    if len({chunk.metadata.get("sheet_name") for chunk in group}) != 1:
        return {}
    rows: list[tuple[int, int]] = []
    columns: list[tuple[str, str]] = []
    for chunk in group:
        row_start = chunk.metadata.get("row_start")
        row_end = chunk.metadata.get("row_end")
        bounds = _cell_range_columns(str(chunk.metadata.get("cell_range") or ""))
        if not isinstance(row_start, int) or not isinstance(row_end, int) or bounds is None:
            return {}
        rows.append((row_start, row_end))
        columns.append(bounds)
    row_start = min(start for start, _ in rows)
    row_end = max(end for _, end in rows)
    first = min((item[0] for item in columns), key=_column_number)
    last = max((item[1] for item in columns), key=_column_number)
    return {
        PARENT_ROW_START_KEY: row_start,
        PARENT_ROW_END_KEY: row_end,
        PARENT_CELL_RANGE_KEY: f"{first}{row_start}:{last}{row_end}",
    }


def _cell_range_columns(cell_range: str) -> tuple[str, str] | None:
    start, _, end = cell_range.partition(":")
    first = "".join(char for char in start if char.isalpha())
    last = "".join(char for char in (end or start) if char.isalpha())
    return (first, last) if first and last else None


def _column_number(letters: str) -> int:
    number = 0
    for char in letters.upper():
        number = number * 26 + (ord(char) - 64)
    return number


def _items(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def engine_search_text(
    metadata: Mapping[str, object], *, include_context_header: bool = True
) -> str | None:
    """親子階層の検索用 text に、選択された文脈ヘッダを付ける。"""
    value = metadata.get(ENGINE_SEARCH_TEXT_KEY)
    if not isinstance(value, str) or not value.strip():
        return None
    header = str(metadata.get("context_header") or "").strip() if include_context_header else ""
    return f"{header}\n{value}" if header and not value.startswith(f"{header}\n") else value


def _backend_chunk(
    child: DocumentChunk,
    index: int,
    parent: DocumentChunk | None,
    page_sizes: Mapping[int, tuple[object, object]],
    tables: Mapping[str, LayoutTable] | None = None,
) -> Chunk:
    metadata = child.metadata
    section_path = [str(part) for part in metadata.get("section_path") or [] if str(part)]
    element_ids = [
        str(ref.get("record_id"))
        for ref in child.source_record_refs
        if isinstance(ref, Mapping) and ref.get("record_id")
    ]
    width, height = page_sizes.get(child.page_start, (None, None))
    bbox = _first_region_bbox(metadata, child.page_start)
    group_id = child.parent_chunk_id or child.chunk_id
    content_kind = _content_kind(metadata)
    return Chunk(
        text=child.text,
        index=index,
        start_offset=0,
        end_offset=len(child.text),
        metadata={
            # 図の本文の出どころ（根拠の種類。#1282）。図の chunk だけに付ける。
            **(
                {FIGURE_TEXT_SOURCE_KEY: _figure_text_source(child.source_record_refs)}
                if content_kind == "figure"
                else {}
            ),
            "source_parser": layout_source_parser(child.source_engine_id),
            "chunk_strategy": SMALL_TO_BIG_STRATEGY,
            "engine_chunk_id": child.chunk_id,
            "parent_chunk_id": group_id,
            "chunk_group_id": group_id,
            "chunk_group_kind": "small_to_big_parent",
            "parent_text": parent.text if parent is not None else "",
            "parent_page_start": parent.page_start if parent is not None else None,
            "parent_page_end": parent.page_end if parent is not None else None,
            ENGINE_SEARCH_TEXT_KEY: child.retrieval_text,
            "content_kind": content_kind,
            "section_path": " > ".join(section_path) or None,
            "page_number": child.page_start or None,
            "page_start": child.page_start or None,
            "page_end": child.page_end or None,
            "page_width": width if isinstance(width, int | float) else None,
            "page_height": height if isinstance(height, int | float) else None,
            "bbox": json.dumps(bbox) if bbox else None,
            "bbox_unit": "absolute" if bbox else None,
            "element_ids": ",".join(element_ids) or None,
            "atomic": bool(metadata.get("atomic")),
            "text_sha256": hashlib.sha256(child.text.encode("utf-8")).hexdigest(),
            # metadata v4 全体(display_regions / image_evidence 等)は JSON で保持する。
            # 1 ページ目の本文は chunk ごとに複製しない(chunk set に 1 つ保存する。#557)。
            "engine_metadata_json": json.dumps(
                _without_first_page_context(metadata), ensure_ascii=False, default=str
            ),
            # 回答根拠の record 対応(bbox ハイライト)に使う。
            "source_record_refs_json": json.dumps(
                child.source_record_refs, ensure_ascii=False, default=str
            ),
            "source_seq_ranges_json": json.dumps(child.source_seq_ranges, default=str),
            "engine_chunk_seq": child.chunk_seq,
            # 解析の表の格子の行（row_basis=parsed_table。#3429）。表の子だけに付ける。
            **(
                _layout_table_locator(child.source_record_refs, tables)
                if content_kind == "table" and tables
                else {}
            ),
        },
    )


@dataclass(frozen=True)
class LayoutTable:
    """layout_records の表の record の、解析の表の番号と格子(#3429)。

    ``rows`` は分割(``rag_engine`` の表の行のまとまり)がその record に使った表の構造の行 → 格子の
    行。頁をまたいで続く表(#3547)は、元の表と続きの表を 1 つの格子にし、続きの表の行を元の表の
    行の後ろに続けて数える(続きの表の先頭の表頭の行は対応させない)。None は構造の行と格子の行が
    同じ(頁をまたがない表)。
    """

    table_index: int
    grid: ParsedTableGrid
    rows: Mapping[int, int] | None = None

    def grid_rows(self, rows: Iterable[int]) -> list[int]:
        """構造の行 → 格子の行(対応の無い行は除く)。"""
        if self.rows is None:
            return list(rows)
        return [self.rows[row] for row in rows if row in self.rows]

    def record_rows(self) -> list[int]:
        """record が含む格子の行(続きの表は繰り返した表頭を除いたデータの行)。"""
        if self.rows is None:
            return list(range(1, self.grid.row_count + 1))
        return sorted(set(self.rows.values()))


def _layout_tables(records: list[dict[str, Any]]) -> dict[str, LayoutTable]:
    """layout_records の表の record の id → 解析の表の番号と格子(#3429)。

    格子は分割(``rag_engine`` の表の行のまとまり)と同じ読み方(``parse_html_table_structure``)で
    表の HTML から作り、行の番号を行のまとまりの ``row_start`` / ``row_end`` とそろえる。番号は
    頁と読み順の順で、頁をまたいで続く表(``raw.continues_record_id``)は前の表と同じ番号にする。
    頁をまたいで続く表は、元の表から続きの表を順にたどって 1 つの格子にする(#3547。
    ``_continued_table``)。格子を作らない表: HTML でない表・Vision などで書き直した表
    (``table_enhanced_html``。続きの表に前の表の表頭を付けたもの〔``table_header_carried``〕は
    除く)・続きの形が元の表と合わない表(その表から後ろの続き)。
    """
    from rag_engine.parsing.table_structure import parse_html_table_structure

    ordered = sorted(
        (
            record
            for record in records
            if str(record.get("category") or "") == "Table"
            and str(record.get("raw_type") or "") != "table_unassigned_text"
            and str(record.get("text") or "").strip()
        ),
        key=lambda record: (int(record.get("page") or 0), int(record.get("seq_no") or 0)),
    )
    by_id = {str(record.get("id") or ""): record for record in ordered}
    next_of: dict[str, str] = {}
    for record in ordered:
        previous = str(_raw(record).get("continues_record_id") or "")
        if previous in by_id:
            next_of.setdefault(previous, str(record.get("id") or ""))
    numbers: dict[str, int] = {}
    tables: dict[str, LayoutTable] = {}
    for record in ordered:
        record_id = str(record.get("id") or "")
        raw = _raw(record)
        previous = str(raw.get("continues_record_id") or "")
        numbers[record_id] = numbers.get(previous) or len(set(numbers.values())) + 1
        if previous in by_id or raw.get("table_enhanced_html"):
            # 続きの表は元の表から作る。書き直した表は格子を作らない。
            continue
        text = str(record.get("text") or "")
        if "<table" not in text.casefold():
            continue
        grid = _layout_table_grid(parse_html_table_structure(text), raw)
        if grid is None:
            continue
        chain: list[dict[str, Any]] = []
        current = next_of.get(record_id)
        while current and current in by_id and len(chain) < len(ordered):
            chain.append(by_id[current])
            current = next_of.get(current)
        if not chain:
            tables[record_id] = LayoutTable(numbers[record_id], grid)
            continue
        tables.update(_continued_table(numbers[record_id], record_id, grid, chain))
    return tables


def _continued_table(
    table_index: int,
    record_id: str,
    grid: ParsedTableGrid,
    chain: list[dict[str, Any]],
) -> dict[str, LayoutTable]:
    """頁をまたいで続く表を 1 つの格子にし、record ごとの構造の行 → 格子の行を作る(#3547)。

    元の表の表頭(``grid.header_rows`` 行)が要る。続きの表の構造(分割と同じく ``table_enhanced_html``
    があればそれ)の先頭が元の表と同じ表頭の行なら(繰り返した表頭・取込が前の表から付けた表頭)
    表頭として対応させず、表頭が無ければ先頭から数える。列の数が違う・表頭が違う・書き直した続きの
    表は、その表から後ろを格子に入れない(前までの表の行の番号は変わらない)。Azure Document
    Intelligence の頁をまたぐ表と同じく、表全体の格子の行の番号にする。
    """
    from rag_engine.parsing.table_structure import parse_html_table_structure

    header_rows = grid.header_rows
    if header_rows < 1:
        return {}
    header_keys = [grid.row_key(row) for row in range(1, header_rows + 1)]
    columns = _grid_columns(grid)
    cells = list(grid.cells)
    maps: dict[str, dict[int, int]] = {
        record_id: {row: row for row in range(1, grid.row_count + 1)}
    }
    offset = grid.row_count
    for piece in chain:
        raw = _raw(piece)
        enhanced = str(raw.get("table_enhanced_html") or "")
        carried = bool(enhanced) and raw.get("table_header_carried") is True
        if enhanced and not carried:
            break
        structure = parse_html_table_structure(enhanced or str(piece.get("text") or ""))
        # 取込が付けた表頭の行の分だけ、Docling のセルの領域(元の表の行)をずらす。
        piece_grid = _layout_table_grid(
            structure, raw, bbox_row_shift=header_rows if carried else 0
        )
        if piece_grid is None or _grid_columns(piece_grid) != columns:
            break
        piece_header = piece_grid.header_rows
        if piece_header and (
            piece_header != header_rows
            or [piece_grid.row_key(row) for row in range(1, header_rows + 1)] != header_keys
        ):
            break
        if carried and not piece_header:
            break
        data_rows = range(piece_header + 1, piece_grid.row_count + 1)
        maps[str(piece.get("id") or "")] = {row: offset + row - piece_header for row in data_rows}
        cells.extend(
            replace(cell, row=offset + cell.row - piece_header)
            for cell in piece_grid.cells
            if cell.row > piece_header
        )
        offset += len(data_rows)
    combined = ParsedTableGrid.build(cells)
    if combined is None or combined.header_rows != header_rows:
        return {}
    return {piece_id: LayoutTable(table_index, combined, rows) for piece_id, rows in maps.items()}


def _grid_columns(grid: ParsedTableGrid) -> int:
    return max((cell.col + cell.col_span - 1 for cell in grid.cells), default=0)


def _layout_table_grid(
    structure: Mapping[str, Any], raw: Mapping[str, Any], *, bbox_row_shift: int = 0
) -> ParsedTableGrid | None:
    """``parse_html_table_structure`` の行・セル(1 始まり)を格子にする。

    セルの領域は Docling が出した値(``raw.table_cell_bboxes``。record の元の表の行・列)。
    ``bbox_row_shift`` は構造の先頭に足した行の数(続きの表に付けた表頭。その行は領域を持たない)。
    """
    boxes = {
        (entry.get("row"), entry.get("col")): entry.get("bbox")
        for entry in _items(raw.get("table_cell_bboxes"))
        if isinstance(entry, Mapping)
    }
    cells: list[ParsedCell] = []
    for row in _items(structure.get("rows")):
        for cell in _items(row.get("cells") if isinstance(row, Mapping) else None):
            if not isinstance(cell, Mapping):
                continue
            row_index, col_index = (
                int(cell.get("row_index") or 0),
                int(cell.get("column_index") or 0),
            )
            if row_index < 1 or col_index < 1:
                return None
            box = (
                boxes.get((row_index - 1 - bbox_row_shift, col_index - 1))
                if row_index > bbox_row_shift
                else None
            )
            cells.append(
                ParsedCell(
                    row=row_index,
                    col=col_index,
                    text=str(cell.get("text") or ""),
                    row_span=max(1, int(cell.get("rowspan") or 1)),
                    col_span=max(1, int(cell.get("colspan") or 1)),
                    header=cell.get("is_header") is True,
                    bbox=_cell_bbox(box),
                )
            )
    return ParsedTableGrid.build(cells)


def _layout_table_locator(refs: object, tables: Mapping[str, LayoutTable]) -> dict[str, str | int]:
    """表の子が行として含む解析の表の格子の行(表の行のまとまりはその範囲、表全体は全行)。

    1 つの表だけを含む子にだけ付ける。行のまとまりの ``row_start`` / ``row_end`` はデータの行で、
    先頭に付く列見出しは行として含まないので数えない。
    """
    table_refs = [
        ref
        for ref in _items(refs)
        if isinstance(ref, Mapping)
        and str(ref.get("category") or "") == "Table"
        and str(ref.get("raw_type") or "") != "table_unassigned_text"
    ]
    if len(table_refs) != 1:
        return {}
    ref = table_refs[0]
    table = tables.get(str(ref.get("source_table_record_id") or ref.get("record_id") or ""))
    if table is None:
        return {}
    group = ref.get("table_row_group")
    if isinstance(group, Mapping):
        start, end = int(group.get("row_start") or 0), int(group.get("row_end") or 0)
        if start < 1 or end < start:
            return {}
        rows = table.grid_rows(range(start, end + 1))
    else:
        rows = table.record_rows()
    return grid_rows_metadata(table.grid, table.table_index, rows) if rows else {}


def _cell_bbox(value: object) -> tuple[float, float, float, float] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    if not all(isinstance(item, int | float) and not isinstance(item, bool) for item in value):
        return None
    x0, y0, x1, y1 = (float(item) for item in value)
    return (x0, y0, x1, y1)


def _raw(record: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = record.get("raw")
    return raw if isinstance(raw, Mapping) else {}


def _without_first_page_context(metadata: Mapping[str, Any]) -> Mapping[str, Any]:
    """chunk に保存する metadata から document.first_page_context を外す(入力は変更しない)。"""
    document = metadata.get("document")
    if not isinstance(document, Mapping) or "first_page_context" not in document:
        return metadata
    return {
        **metadata,
        "document": {key: value for key, value in document.items() if key != "first_page_context"},
    }


def _figure_text_source(refs: object) -> str:
    """図の本文を Vision（VLM）が書いたか(vision)、解析の OCR・キャプションのままか(ocr)。

    図の record に Vision のモデルが残り、失敗の印が無ければ VLM の説明とみなす。
    """
    for ref in refs if isinstance(refs, list) else []:
        if not isinstance(ref, Mapping) or str(ref.get("category") or "") != "Picture":
            continue
        if str(ref.get("raw_type") or "") == "picture_ocr_text":
            continue
        if str(ref.get("vision_model") or "").strip() and not ref.get("vision_error"):
            return FIGURE_TEXT_SOURCE_VISION
    return FIGURE_TEXT_SOURCE_OCR


def _content_kind(metadata: Mapping[str, Any]) -> str:
    categories = {str(value) for value in metadata.get("source_categories") or []}
    if "Table" in categories:
        return "table"
    if "Picture" in categories:
        return "figure"
    return "text"


def _first_region_bbox(metadata: Mapping[str, Any], page: int) -> list[float] | None:
    """先頭ページの表示領域(複数 box)を包含する bbox を返す。"""
    layout = metadata.get("layout")
    regions = layout.get("display_regions") if isinstance(layout, Mapping) else None
    for region in regions or []:
        if not isinstance(region, Mapping) or int(region.get("page") or 0) != page:
            continue
        boxes = [
            box["bbox"]
            for box in region.get("boxes") or []
            if isinstance(box, Mapping)
            and isinstance(box.get("bbox"), list)
            and len(box["bbox"]) == 4
        ]
        if boxes:
            return [
                float(min(box[0] for box in boxes)),
                float(min(box[1] for box in boxes)),
                float(max(box[2] for box in boxes)),
                float(max(box[3] for box in boxes)),
            ]
    return None


def _source_run_id(records: list[dict[str, Any]]) -> str:
    """rag_poc の run_id 形式(16 桁 hex)を record 群の内容から決定論的に作る。"""
    digest = hashlib.sha256(
        json.dumps([record.get("id") for record in records], ensure_ascii=False).encode("utf-8")
    )
    return digest.hexdigest()[:16]
