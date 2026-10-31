"""Small-to-Big の親子 chunk の組み立てと run payload の入出力。公開 API はここ。"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable, Sequence
from typing import Any

from rag_engine.chunking.constants import (
    ATOMIC_CATEGORIES,
    CHILD_CHUNK_LEVEL,
    CHUNK_METADATA_SCHEMA_VERSION,
    CHUNK_SCHEMA_VERSION,
    CHUNK_STRATEGY,
    DEFAULT_CHILD_SEARCH_TEXT_MAX_CHARS,
    METADATA_ONLY_CATEGORIES,
    PARENT_CHUNK_LEVEL,
    RUN_ID_PATTERN,
    SEARCH_TEXT_SCHEMA_VERSION,
    SECTION_CATEGORIES,
    ChunkingConfig,
    DocumentChunk,
    _boilerplate_text_key,
    _clean_search_text,
    _compact_page_text,
    _int_value,
    _page_number_footer_pattern,
    _required_string,
    _seq_ranges,
    _string_list,
    _text_sha256,
    _token_estimate,
    _unique_refs,
)
from rag_engine.chunking.evidence import _source_record_ref
from rag_engine.chunking.headings import (
    _merge_logical_page_labels,
    _merge_section_path_sources,
    _merge_section_paths,
    _promote_headings,
    _section_path_state_after,
    _section_unit,
    _sentence_boundary_pattern,
)
from rag_engine.chunking.layout import (
    _annotate_layout_records,
    _attached_layout_annotations,
    _attached_layout_annotations_by_target,
    _attached_layout_annotations_for_targets,
    _inline_icons_by_merge_target,
    _is_excluded_page_boilerplate_record,
    _is_page_boilerplate_item,
    _is_section_banner_text,
    _table_contained_visual_ids,
    _visual_group_key,
    _visual_groups,
)
from rag_engine.chunking.metadata import _chunk_metadata, _project_chunk_metadata, _validate_chunk_metadata_v4
from rag_engine.chunking.page_numbers import PageNumber, read_page_number, select_logical_pages
from rag_engine.chunking.records import (
    _buffer_has_body,
    _form_record_text,
    _image_fallback_text,
    _is_attached_atomic_annotation,
    _is_form_like_record,
    _join_records_text,
    _record_can_supply_image_evidence,
    _record_text,
    _record_text_for_chunk,
    _record_visual_role,
    _records_char_count,
)
from rag_engine.chunking.search_text import _child_search_text, _without_screen_examples
from rag_engine.chunking.tables import (
    _paired_picture_ocr_text,
    _table_record_with_metadata,
    _table_row_group_records,
    _table_supplements_by_table,
)
from rag_engine.knowledge.classification import DocumentClassification, classification_from_metadata
from rag_engine.knowledge.document_metadata import extract_first_page_context, normalize_document_metadata
from rag_engine.parsing.decorative_pictures import (
    DECORATIVE_VISUAL_ROLE,
    INLINE_ICON_VISUAL_ROLE,
    classify_picture_record,
    mark_picture_record_role,
)
from rag_engine.parsing.picture_text import link_picture_ocr_records, picture_ocr_text_label
from rag_engine.retrieval.inquiry_conditions import (
    INQUIRY_CHUNK_METADATA_SCHEMA_VERSION,
    build_inquiry_chunk_metadata,
    inquiry_profile_contract_hash,
)


def build_small_to_big_chunks(
    payload: dict[str, Any],
    *,
    source_run_id: str,
    selected_engine_ids: Sequence[str],
    config: ChunkingConfig,
    created_at_utc: str = "",
    source_file_sha256: str = "",
    source_page_count: int = 0,
    classification: DocumentClassification | dict[str, Any] | None = None,
    diagnostics: dict[str, int] | None = None,
) -> list[DocumentChunk]:
    """LayoutRecord payload から親子チャンク集合を構築します。

    diagnostics を指定すると、本文を含めず除外理由ごとのレコード数を加算する。
    入力 payload は変更しない。Picture の役割（`visual_role` など）は record を複製した上で付ける (#798)。
    解析段階（picture_descriptions / visual_artifacts）が record の raw に書いた `visual_role` は、ここで
    閾値判定をやり直さず正として扱う。Vision の実行有無はその時点で決まっているため、chunking 側で役割を
    変えても説明文は増えない。
    """
    source_file_name = str(payload.get("pdf_name") or "")
    document_classification = classification_from_metadata(classification if classification is not None else payload.get("classification"))
    if not source_page_count:
        source_page_count = _source_page_count(payload)
    document_info = normalize_document_metadata(payload.get("document_metadata"), source_file_name=source_file_name)
    engine_labels = _engine_labels(payload.get("engines"))
    records = _records_by_engine(payload.get("records"), selected_engine_ids, diagnostics=diagnostics)
    all_chunks: list[DocumentChunk] = []
    engine_documents: dict[str, dict[str, Any]] = {}
    for engine_id in selected_engine_ids:
        engine_records = records.get(engine_id, [])
        if not engine_records:
            continue
        engine_document = {
            **document_info,
            "first_page_context": extract_first_page_context(
                engine_records,
                engine=engine_id,
                analyzed_pages=[p.get("page") for p in payload.get("pages", []) if isinstance(p, dict)],
                paginated=bool(source_page_count or any(r.get("page") for r in engine_records)),
            ),
        }
        engine_documents[engine_id] = engine_document
        children = _build_child_chunks_for_engine(
            engine_records,
            source_run_id=source_run_id,
            source_file_name=source_file_name,
            source_engine_id=engine_id,
            source_engine_label=engine_labels.get(engine_id, engine_id),
            config=config,
            classification=document_classification,
            diagnostics=diagnostics,
        )
        # 親構築中の検索文生成に間に合うよう、先に子へ文書情報を付ける。
        for child in children:
            child.metadata["document"] = dict(engine_document)
        all_chunks.extend(children)
        all_chunks.extend(
            _build_parent_chunks_for_engine(
                children,
                source_run_id=source_run_id,
                source_file_name=source_file_name,
                source_engine_id=engine_id,
                source_engine_label=engine_labels.get(engine_id, engine_id),
                config=config,
                classification=document_classification,
            )
        )
    for chunk in all_chunks:
        chunk.metadata["document"] = dict(engine_documents[chunk.source_engine_id])
        chunk.metadata = _project_chunk_metadata(chunk.metadata)
    return sorted(
        all_chunks, key=lambda chunk: (chunk.source_engine_id, 0 if chunk.chunk_level == CHILD_CHUNK_LEVEL else 1, chunk.chunk_seq)
    )


def _before_picture_ocr(text: str) -> str:
    """最初の `OCR抽出テキスト:` ラベルより前の本文を返します（ラベルがなければ全文）。"""
    head, _label, _rest = text.partition(f"{picture_ocr_text_label()}:")
    return head


def audit_chunk_retrieval_text(
    chunks: Sequence[DocumentChunk],
    *,
    child_search_text_max_chars: int = DEFAULT_CHILD_SEARCH_TEXT_MAX_CHARS,
) -> dict[str, Any]:
    """child 本文の欠落率と目標超過数を実測し、run 単位の監査値を返します。"""
    children = [chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]
    # 検索文は空白を正規化するため、空白の差だけを本文欠落と誤判定しない。
    # 画面例の値の文は検索用テキストから意図的に除くので（#753）、欠落には数えない。
    # Vision 成功時の OCR ブロックも意図的に除くので（#900）、`OCR抽出テキスト:` より前の本文だけを見る。
    # 保存済み chunk には record が無く Vision の成否を判別できないため、text の形で近似する。
    truncated = sum(
        bool(chunk.text)
        and _clean_search_text(_without_screen_examples(_before_picture_ocr(chunk.text))) not in _clean_search_text(chunk.retrieval_text)
        for chunk in children
    )
    return {
        "child_count": len(children),
        "body_truncated_count": truncated,
        "body_truncated_rate": truncated / len(children) if children else 0.0,
        "over_target_count": sum(len(chunk.retrieval_text) > child_search_text_max_chars for chunk in children),
        "max_search_text_chars": max((len(chunk.retrieval_text) for chunk in children), default=0),
    }


def validate_run_id(value: Any) -> str:
    """run_id を安全な保存パス要素として使える文字列に検証します。"""
    run_id = str(value or "").strip()
    if not run_id:
        raise ValueError("ファイル解析 tab で解析を実行してからチャンキングしてください。")
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("解析結果の run_id が不正です。")
    return run_id


def _build_child_chunks_for_engine(
    records: Sequence[dict[str, Any]],
    *,
    source_run_id: str,
    source_file_name: str,
    source_engine_id: str,
    source_engine_label: str,
    config: ChunkingConfig,
    classification: DocumentClassification,
    diagnostics: dict[str, int] | None = None,
) -> list[DocumentChunk]:
    records = _annotate_layout_records(_promote_headings(records))
    metadata_by_page = _metadata_by_page(records, config)
    visual_group_by_key = _visual_groups(records)
    inline_icons_by_target = _inline_icons_by_merge_target(records)
    attached_annotations_by_target = _attached_layout_annotations_by_target(records)
    table_contained_visual_ids = _table_contained_visual_ids(records)
    consumed_visual_ids: set[str] = set()
    supplements_by_table = _table_supplements_by_table(records)
    consumed_supplement_ids: set[str] = set()
    # ページの境目・図・表をまたいで続く段落（`raw.continues_record_id`。#2001・#2266）の
    # 続きの元 → 続きの record。続きがまだ来ていない段落は、間の図・表で buffer を閉じない。
    continued_by = _continued_by(records)
    seen_record_ids: set[str] = set()
    children: list[DocumentChunk] = []
    text_buffer: list[dict[str, Any]] = []
    section_path: list[str] = []
    section_levels: list[int | None] = []
    section_depths: list[int] = []
    section_sources: list[str] = []  # section_path と同じ長さ。各見出しの出所 (#814)

    def flush_text_buffer() -> None:
        nonlocal text_buffer
        if not text_buffer:
            return
        # 空白だけの buffer を除く。本文の長さでは除去しない。
        if not _buffer_has_body(text_buffer) and all(str(r.get("category") or "") not in SECTION_CATEGORIES for r in text_buffer):
            text_buffer = []
            return
        children.append(
            _child_chunk(
                len(children) + 1,
                text_buffer,
                source_run_id=source_run_id,
                source_file_name=source_file_name,
                source_engine_id=source_engine_id,
                source_engine_label=source_engine_label,
                metadata_by_page=metadata_by_page,
                section_path=section_path,
                section_path_sources=section_sources,
                atomic=False,
                classification=classification,
            )
        )
        text_buffer = []

    def append_inline_icons(record: dict[str, Any]) -> None:
        record_id = str(record.get("id") or "")
        if not record_id:
            return
        text_buffer.extend(inline_icons_by_target.get(record_id, []))

    def buffer_awaits_continuation() -> bool:
        """buffer の段落に、まだ読んでいない続きの record があるか。"""
        pending = (continued_by.get(str(item.get("id") or "")) for item in text_buffer)
        return any(record_id and record_id not in seen_record_ids for record_id in pending)

    def release_text_buffer_for_atomic() -> list[dict[str, Any]]:
        """図・表の前で buffer を閉じ、図・表の子 chunk に付ける見出しの record を返す。

        本文のある buffer は閉じる（見出しは付けない）。見出しだけの buffer は図・表に付ける。
        続きを待つ段落の buffer は閉じず、図・表を単独の子 chunk にする（段落は続きと同じ子 chunk）。
        """
        nonlocal text_buffer
        if not text_buffer:
            return []
        if _buffer_has_body(text_buffer):
            if not buffer_awaits_continuation():
                flush_text_buffer()
            return []
        heading_records = text_buffer
        text_buffer = []
        return heading_records

    def count_exclusion(reason: str) -> None:
        if diagnostics is not None:
            diagnostics[reason] = diagnostics.get(reason, 0) + 1

    for record in sorted(records, key=lambda item: (int(item.get("page") or 0), int(item.get("seq_no") or 0))):
        category = str(record.get("category") or "")
        record_id = str(record.get("id") or "")
        seen_record_ids.add(record_id)
        text = _record_text(record)
        visual_role = _record_visual_role(record)
        if category == "Picture" and record_id in table_contained_visual_ids:
            continue
        if record_id in consumed_supplement_ids:
            continue
        if category == "Picture" and visual_role in {DECORATIVE_VISUAL_ROLE, INLINE_ICON_VISUAL_ROLE}:
            count_exclusion("excluded_decorative_count")
            continue
        if _is_attached_atomic_annotation(record):
            continue
        if category in METADATA_ONLY_CATEGORIES and _is_excluded_page_boilerplate_record(record, metadata_by_page):
            count_exclusion("excluded_page_boilerplate_count")
            continue
        if (
            not text
            and not _form_record_text(record)
            and not _record_can_supply_image_evidence(record)
            and not _is_form_like_record(record)
        ):
            count_exclusion("empty_record_count")
            continue
        if category in SECTION_CATEGORIES and text_buffer and _buffer_has_body(text_buffer):
            flush_text_buffer()
        # 見出しの木の規則は章節ナビゲーション（`layout_section_paths`）と共通 (#1517)。
        section_path, section_levels, section_depths, section_sources = _section_path_state_after(
            (section_path, section_levels, section_depths, section_sources),
            record,
        )
        if category == "Table":
            heading_records = release_text_buffer_for_atomic()
            annotation_records = _attached_layout_annotations(record_id, attached_annotations_by_target)
            # text layer から補った表の原文（#564）は、表と同じ child に入れて表から切り離さない。
            supplement_records = [item for item in supplements_by_table.get(record_id, [])]
            consumed_supplement_ids.update(str(item.get("id") or "") for item in supplement_records)
            table_record = _table_record_with_metadata(record, records)
            row_group_records = _table_row_group_records(table_record, heading_records, config)
            if row_group_records:
                for index, row_record in enumerate(row_group_records):
                    trailing = supplement_records if index == len(row_group_records) - 1 else []
                    children.append(
                        _child_chunk(
                            len(children) + 1,
                            [row_record, *annotation_records, *trailing],
                            source_run_id=source_run_id,
                            source_file_name=source_file_name,
                            source_engine_id=source_engine_id,
                            source_engine_label=source_engine_label,
                            metadata_by_page=metadata_by_page,
                            section_path=section_path,
                            section_path_sources=section_sources,
                            atomic=True,
                            classification=classification,
                        )
                    )
                continue
            children.append(
                _child_chunk(
                    len(children) + 1,
                    [*heading_records, table_record, *annotation_records, *supplement_records],
                    source_run_id=source_run_id,
                    source_file_name=source_file_name,
                    source_engine_id=source_engine_id,
                    source_engine_label=source_engine_label,
                    metadata_by_page=metadata_by_page,
                    section_path=section_path,
                    section_path_sources=section_sources,
                    atomic=True,
                    classification=classification,
                )
            )
            continue
        if category in ATOMIC_CATEGORIES:
            group_key = _visual_group_key(record)
            group = visual_group_by_key.get(group_key) if group_key else None
            group_ids = {str(item.get("id") or "") for item in group or []}
            # 消費済み group の残り record は chunk を作りません。見出しだけの buffer を
            # 退避した後に抜けると、その見出しがどの chunk にも入らなくなります。
            if group_ids & consumed_visual_ids:
                continue
            if category == "Picture":
                picture_records = group or [record]
                ocr_texts = [
                    str(item.get("text") or "") for item in picture_records if str(item.get("raw_type") or "") == "picture_ocr_text"
                ]
                if not ocr_texts and not group:
                    ocr_texts = [_paired_picture_ocr_text(record, records)]
                picture_raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
                vision = picture_raw.get("vision_description") if isinstance(picture_raw.get("vision_description"), dict) else {}
                described = any(vision.get(key) for key in ("visible_screen_names", "visible_buttons", "visible_fields", "operation_steps"))
                if not described and _is_section_banner_text(ocr_texts, section_path, [_record_text(item) for item in text_buffer]):
                    # ページの帯画像（節見出しの再掲 + 著作権表示）。child にせず、buffer もそのまま次へ持ち越す。
                    consumed_visual_ids.update(group_ids or {record_id})
                    continue
            heading_records = release_text_buffer_for_atomic()
            if group:
                consumed_visual_ids.update(group_ids)
                annotation_records = _attached_layout_annotations_for_targets(group_ids, attached_annotations_by_target)
                children.append(
                    _child_chunk(
                        len(children) + 1,
                        [*heading_records, *group, *annotation_records],
                        source_run_id=source_run_id,
                        source_file_name=source_file_name,
                        source_engine_id=source_engine_id,
                        source_engine_label=source_engine_label,
                        metadata_by_page=metadata_by_page,
                        section_path=section_path,
                        section_path_sources=section_sources,
                        atomic=True,
                        classification=classification,
                    )
                )
                continue
            annotation_records = _attached_layout_annotations(record_id, attached_annotations_by_target)
            children.append(
                _child_chunk(
                    len(children) + 1,
                    [*heading_records, record, *annotation_records],
                    source_run_id=source_run_id,
                    source_file_name=source_file_name,
                    source_engine_id=source_engine_id,
                    source_engine_label=source_engine_label,
                    metadata_by_page=metadata_by_page,
                    section_path=section_path,
                    section_path_sources=section_sources,
                    atomic=True,
                    classification=classification,
                )
            )
            continue

        for piece in _split_long_text_record(record, config.child_target_chars):
            if (
                text_buffer
                and _records_char_count(text_buffer) + len(_record_text(piece)) > config.child_target_chars
                and _buffer_has_body(text_buffer)
                and not _continues_buffer(piece, text_buffer, config)
            ):
                flush_text_buffer()
            text_buffer.append(piece)
        append_inline_icons(record)
    flush_text_buffer()
    return children


def _continues_buffer(piece: dict[str, Any], buffer: Sequence[dict[str, Any]], config: ChunkingConfig) -> bool:
    """ページの境目で切れた段落の続き（`raw.continues_record_id`）を、前の段落と同じ child に入れるか。

    取込の文書全体の整理（backend の `pdf_segment_merge`。#2001・#2266）が、前のページの最後の段落の
    続きに印を付ける。続きの元は buffer の末尾でなくてよい（間のページ下端の脚注が buffer に入る）。
    続きの先頭は child の目標の文字数を超えても前の段落と分けない（目標の 2 倍まで）。
    """
    previous_id = _continues_record_id(piece)
    if not previous_id or all(str(item.get("id") or "") != previous_id for item in buffer):
        return False
    return _records_char_count(buffer) + len(_record_text(piece)) <= config.child_target_chars * 2


def _continues_record_id(record: dict[str, Any]) -> str:
    raw = record.get("raw") if isinstance(record.get("raw"), dict) else {}
    return str(raw.get("continues_record_id") or "")


def _continued_by(records: Sequence[dict[str, Any]]) -> dict[str, str]:
    """続きの元の record の id → 続きの record の id（`raw.continues_record_id` の逆引き）。"""
    continued: dict[str, str] = {}
    for record in records:
        previous_id = _continues_record_id(record)
        record_id = str(record.get("id") or "")
        if previous_id and record_id:
            continued.setdefault(previous_id, record_id)
    return continued


def _split_long_text_record(record: dict[str, Any], target_chars: int) -> list[dict[str, Any]]:
    """目標文字数を超える地の文の record を、文末を境界にした複数 record へ分けます。

    表の行や図と違い地の文は分割できます。分割しないと child が検索文の上限を超え、ファイル名や
    節の情報を付ける余地がなくなります。各片は元 record の id・page・seq_no・bbox を引き継ぐため、
    引用位置は元 record を指します。文末のない長文（OCR の連結など）は目標文字数で機械的に切ります。
    Form の項目を持つ record は、片ごとに form_context が重複するため分割しません。
    """
    text = _record_text(record)
    if len(text) <= target_chars or str(record.get("category") or "") not in {"Text", "List-item"} or _is_form_like_record(record):
        return [record]
    pieces: list[str] = []
    current = ""
    for sentence in _sentence_boundary_pattern().split(text):
        for start in range(0, len(sentence), target_chars):
            segment = sentence[start : start + target_chars]
            if current and len(current) + len(segment) > target_chars:
                pieces.append(current)
                current = ""
            current += segment
    if current:
        pieces.append(current)
    return [{**record, "text": piece} for piece in pieces if piece.strip()]


def _build_parent_chunks_for_engine(
    children: Sequence[DocumentChunk],
    *,
    source_run_id: str,
    source_file_name: str,
    source_engine_id: str,
    source_engine_label: str,
    config: ChunkingConfig,
    classification: DocumentClassification,
) -> list[DocumentChunk]:
    parents: list[DocumentChunk] = []
    buffer: list[DocumentChunk] = []

    def flush_parent_buffer() -> None:
        nonlocal buffer
        if not buffer:
            return
        parent_id = f"chunk-{source_engine_id}-p{len(parents) + 1:06d}"
        for child in buffer:
            child.parent_chunk_id = parent_id
        parent = _parent_chunk(
            parent_id,
            len(parents) + 1,
            buffer,
            source_run_id=source_run_id,
            source_file_name=source_file_name,
            source_engine_id=source_engine_id,
            source_engine_label=source_engine_label,
            classification=classification,
        )
        _apply_contextual_child_search_text(buffer, parent=parent, config=config)
        parents.append(parent)
        buffer = []

    for child in children:
        if buffer and _parent_should_flush(buffer, child, config):
            flush_parent_buffer()
        buffer.append(child)
    flush_parent_buffer()
    return parents


def _child_chunk(
    chunk_seq: int,
    records: Sequence[dict[str, Any]],
    *,
    source_run_id: str,
    source_file_name: str,
    source_engine_id: str,
    source_engine_label: str,
    metadata_by_page: dict[int, dict[str, Any]],
    section_path: Sequence[str],
    atomic: bool,
    classification: DocumentClassification | None = None,
    section_path_sources: Sequence[str] = (),
) -> DocumentChunk:
    ordered_records = sorted(records, key=lambda item: (int(item.get("page") or 0), int(item.get("seq_no") or 0)))
    text = _join_records_text(ordered_records)
    # 検索文の土台。Vision 成功時の図の OCR を外した本文で、_apply_contextual_child_search_text が
    # これに文脈を足して retrieval_text を確定する (#900)。回答用の text には OCR を残す。
    retrieval_body = _join_records_text(ordered_records, for_retrieval=True) or text
    if not text:
        text = retrieval_body = _image_fallback_text(ordered_records, source_file_name)
    refs = [_source_record_ref(record) for record in ordered_records]
    pages = [ref["page"] for ref in refs]
    page_start = min(pages)
    page_end = max(pages)
    metadata = _chunk_metadata(
        refs,
        metadata_by_page,
        page_start,
        page_end,
        section_path=section_path,
        section_path_sources=section_path_sources,
        atomic=atomic,
        text=text,
        retrieval_text=text,
        compute_retrieval_profile=False,  # 検索文確定後に _apply_contextual_child_search_text が計算する (#867)
        source_run_id=source_run_id,
        source_file_name=source_file_name,
        classification=classification,
    )
    # 原文と画像の生成説明を回答時に区別できるよう、本文内の位置だけを保持する。
    # 重複して位置が決まらない本文は推測して結び付けない。
    native_ranges = []
    for record in ordered_records:
        body = _record_text_for_chunk(record)
        start = text.find(body) if body else -1
        if record.get("category") in {"Text", "List-item"} and start >= 0 and text.find(body, start + 1) < 0:
            native_ranges.append(dict(record_id=record.get("id"), page=record.get("page"), start=start, end=start + len(body)))
    metadata.setdefault("layout", {})["native_text_ranges"] = native_ranges
    return DocumentChunk(
        chunk_id=f"chunk-{source_engine_id}-c{chunk_seq:06d}",
        chunk_level=CHILD_CHUNK_LEVEL,
        chunk_seq=chunk_seq,
        parent_chunk_id="",
        child_chunk_ids=[],
        text=text,
        retrieval_text=retrieval_body,
        char_count=len(text),
        token_estimate=_token_estimate(text),
        source_run_id=source_run_id,
        source_file_name=source_file_name,
        source_engine_id=source_engine_id,
        source_engine_label=source_engine_label,
        page_start=page_start,
        page_end=page_end,
        source_seq_ranges=_seq_ranges(refs),
        source_record_refs=refs,
        metadata=metadata,
    )


def _parent_chunk(
    chunk_id: str,
    chunk_seq: int,
    children: Sequence[DocumentChunk],
    *,
    source_run_id: str,
    source_file_name: str,
    source_engine_id: str,
    source_engine_label: str,
    classification: DocumentClassification | None = None,
) -> DocumentChunk:
    text = "\n\n".join(child.text for child in children if child.text)
    refs = _unique_refs(ref for child in children for ref in child.source_record_refs)
    page_start = min(child.page_start for child in children)
    page_end = max(child.page_end for child in children)
    metadata = _chunk_metadata(
        refs,
        {},
        page_start,
        page_end,
        section_path=_merge_section_paths(children),
        section_path_sources=_merge_section_path_sources(children),
        atomic=False,
        text=text,
        retrieval_text=text,
        source_run_id=source_run_id,
        source_file_name=source_file_name,
        logical_page_labels=_merge_logical_page_labels(children),
        classification=classification,
    )
    native_ranges = []
    cursor = 0
    for child in children:
        if not child.text:
            continue
        offset = text.find(child.text, cursor)
        if offset < 0:
            continue
        for item in (child.metadata.get("layout") or {}).get("native_text_ranges", []):
            native_ranges.append({**item, "start": offset + item["start"], "end": offset + item["end"]})
        cursor = offset + len(child.text)
    metadata.setdefault("layout", {})["native_text_ranges"] = native_ranges
    return DocumentChunk(
        chunk_id=chunk_id,
        chunk_level=PARENT_CHUNK_LEVEL,
        chunk_seq=chunk_seq,
        parent_chunk_id="",
        child_chunk_ids=[child.chunk_id for child in children],
        text=text,
        # 親の検索 text からも画面の値を外す。keyword search は親 chunk の search_text も引く (#778)。
        # child.retrieval_text はこの時点では _child_chunk が置いた OCR 除外済みの本文なので、それを
        # 連結して Vision 成功時の OCR を親の検索文からも外す (#900)。
        retrieval_text=_without_screen_examples("\n\n".join(child.retrieval_text for child in children if child.retrieval_text)),
        char_count=len(text),
        token_estimate=_token_estimate(text),
        source_run_id=source_run_id,
        source_file_name=source_file_name,
        source_engine_id=source_engine_id,
        source_engine_label=source_engine_label,
        page_start=page_start,
        page_end=page_end,
        source_seq_ranges=_seq_ranges(refs),
        source_record_refs=refs,
        metadata=metadata,
    )


def _apply_contextual_child_search_text(
    children: Sequence[DocumentChunk],
    *,
    parent: DocumentChunk,
    config: ChunkingConfig,
) -> None:
    # child.retrieval_text は _child_chunk が置いた検索用本文（OCR 除外済み）。ここで 1 回だけ文脈付きの
    # 検索文へ置き換えるため、同じ child に 2 回適用してはならない (#900)。
    for child in children:
        search_text, _components = _child_search_text(child, parent=parent, config=config, body=child.retrieval_text)
        child.retrieval_text = search_text
        inquiry = build_inquiry_chunk_metadata(
            text=child.text,
            retrieval_text=search_text,
            source_file_name=child.source_file_name,
            source_categories=child.metadata.get("source_categories") or [],
            page_start=child.page_start,
            page_end=child.page_end,
        )
        if inquiry:
            child.metadata["retrieval_profile"] = inquiry
        else:
            child.metadata.pop("retrieval_profile", None)


def _records_by_engine(
    raw_records: Any, selected_engine_ids: Sequence[str], *, diagnostics: dict[str, int] | None = None
) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(raw_records, list):
        return {}
    selected = set(selected_engine_ids)
    # mark_picture_record_role は record["raw"] を直接更新するため、呼び出し側の payload を汚さないよう
    # record と raw を複製してから扱う。近傍判定（classify_picture_record の records）も複製側で行う (#798)。
    candidates = [
        {**raw_record, "raw": dict(raw_record["raw"]) if isinstance(raw_record.get("raw"), dict) else {}}
        for raw_record in raw_records
        if isinstance(raw_record, dict) and str(raw_record.get("engine") or "") in selected
    ]
    # 図の中の文字（OCR）の record を持ち主の図に結び付け、分割で図と同じ group にする（#3455）。
    link_picture_ocr_records(candidates)
    records: dict[str, list[dict[str, Any]]] = {engine_id: [] for engine_id in selected_engine_ids}
    excluded_picture_groups: set[tuple[Any, ...]] = set()
    for raw_record in candidates:
        engine_id = str(raw_record.get("engine") or "")
        visual_role = classify_picture_record(raw_record, candidates)
        mark_picture_record_role(raw_record, visual_role)
        if visual_role.exclude_from_rag:
            if diagnostics is not None:
                diagnostics["excluded_decorative_count"] = diagnostics.get("excluded_decorative_count", 0) + 1
            if str(raw_record.get("raw_type") or "") != "picture_ocr_text" and (key := _visual_group_key(raw_record)):
                excluded_picture_groups.add(key)
            continue
        # ロゴなど除外した Picture の OCR 集約（同じ bbox）は、単独では本文にならない。Picture 本体と一緒に除外する。
        if str(raw_record.get("raw_type") or "") == "picture_ocr_text" and _visual_group_key(raw_record) in excluded_picture_groups:
            if diagnostics is not None:
                diagnostics["excluded_decorative_count"] = diagnostics.get("excluded_decorative_count", 0) + 1
            continue
        if (
            not _record_text(raw_record)
            and not _record_can_supply_image_evidence(raw_record, visual_role)
            and not visual_role.merge_with_text
            and not _is_form_like_record(raw_record)
        ):
            if diagnostics is not None:
                diagnostics["empty_record_count"] = diagnostics.get("empty_record_count", 0) + 1
            continue
        records.setdefault(engine_id, []).append(raw_record)
    return records


def _effective_engine_ids(payload: dict[str, Any], preferred_engine_ids: Iterable[str]) -> list[str]:
    available = [engine for engine in _engine_labels(payload.get("engines")) if engine]
    available_set = set(available)
    preferred = [engine for engine in preferred_engine_ids if engine in available_set]
    if preferred:
        return preferred
    present = []
    seen = set()
    for record in payload.get("records") or []:
        if not isinstance(record, dict):
            continue
        engine_id = str(record.get("engine") or "")
        if engine_id in seen:
            continue
        if available_set and engine_id not in available_set:
            continue
        seen.add(engine_id)
        present.append(engine_id)
    return present or available


def _engine_labels(raw_engines: Any) -> dict[str, str]:
    labels: dict[str, str] = {}
    if not isinstance(raw_engines, list):
        return labels
    for raw_engine in raw_engines:
        if not isinstance(raw_engine, dict):
            continue
        engine = str(raw_engine.get("engine") or "")
        label = str(raw_engine.get("label") or engine)
        if engine:
            labels[engine] = label
    return labels


def _metadata_by_page(records: Sequence[dict[str, Any]], config: ChunkingConfig | None = None) -> dict[int, dict[str, Any]]:
    config = config or ChunkingConfig()
    metadata: dict[int, dict[str, Any]] = {}
    boilerplate_counts: dict[tuple[str, str], int] = {}
    page_numbers: list[PageNumber] = []
    for record in records:
        page = _int_value(record.get("page"))
        if page is None:
            continue
        page_metadata = metadata.setdefault(page, {})
        category = str(record.get("category") or "")
        record_text = _record_text(record)
        # 頁番号の形のフッターは頁の定型として本文から外す。論理頁は文書全体の並びで確かめてから決める（#2289）。
        page_number = bool(_page_number_footer_pattern().fullmatch(_compact_page_text(record_text))) if category == "Page-footer" else False
        candidate = read_page_number(record_text, page) if page_number else None
        if candidate is not None:
            page_numbers.append(candidate)
        if category not in METADATA_ONLY_CATEGORIES or not record_text:
            continue
        normalized_text = _boilerplate_text_key(record_text)
        boilerplate_counts[(category, normalized_text)] = boilerplate_counts.get((category, normalized_text), 0) + 1
        page_metadata.setdefault("_page_layout_candidates", []).append(
            {
                "record_id": str(record.get("id") or ""),
                "page": page,
                "seq_no": int(record.get("seq_no") or 0),
                "category": category,
                "role": "page_number" if page_number else category,
                "text": record_text,
                "normalized_text": normalized_text,
            }
        )
    logical_pages = select_logical_pages(
        page_numbers,
        page_count=len(metadata),
        min_run_pages=config.page_number_min_run_pages,
        offset_tolerance=config.page_number_offset_tolerance,
    )
    for page, logical_page in logical_pages.items():
        metadata[page]["logical_page"] = logical_page
    for page_metadata in metadata.values():
        candidates = page_metadata.get("_page_layout_candidates") if isinstance(page_metadata.get("_page_layout_candidates"), list) else []
        excluded_layout = []
        for item in candidates:
            key = (str(item.get("category") or ""), str(item.get("normalized_text") or ""))
            item["repeated"] = boilerplate_counts.get(key, 0) > 1
            item["excluded_from_retrieval"] = _is_page_boilerplate_item(item)
            if item["excluded_from_retrieval"]:
                excluded_layout.append(item)
        if excluded_layout:
            page_metadata["excluded_layout"] = excluded_layout
        page_metadata.pop("_page_layout_candidates", None)
    return metadata


def _parent_should_flush(buffer: Sequence[DocumentChunk], child: DocumentChunk, config: ChunkingConfig) -> bool:
    if len(buffer) >= config.parent_max_children:
        return True
    # 親は機能ユニット（番号付き機能見出し）をまたがない。前の機能の末尾と次の機能の先頭が 1 つの親に
    # 入ると、根拠として渡したときに別機能の手順が同じ機能として読まれる。
    if _section_unit(buffer[-1].metadata.get("section_path") or []) != _section_unit(child.metadata.get("section_path") or []):
        return True
    page_start = min([chunk.page_start for chunk in buffer] + [child.page_start])
    page_end = max([chunk.page_end for chunk in buffer] + [child.page_end])
    if page_end - page_start + 1 > config.parent_max_pages:
        return True
    char_count = sum(chunk.char_count for chunk in buffer) + child.char_count + max(0, len(buffer)) * 2
    return char_count > config.parent_target_chars


def _source_page_count(payload: dict[str, Any]) -> int:
    for key in ("source_page_count", "page_count", "total_pages"):
        value = _int_value(payload.get(key))
        if value:
            return value
    pages = payload.get("pages")
    if isinstance(pages, list):
        return len(pages)
    record_pages = [
        page
        for page in (_int_value(record.get("page")) for record in payload.get("records") or [] if isinstance(record, dict))
        if page is not None
    ]
    return max(record_pages) if record_pages else 0


def _payload_classification(payload: dict[str, Any], chunks: Sequence[DocumentChunk]) -> dict[str, Any]:
    raw = payload.get("classification")
    if isinstance(raw, dict):
        return classification_from_metadata(raw).to_metadata()
    for chunk in chunks:
        metadata = chunk.metadata if isinstance(chunk.metadata, dict) else {}
        raw = metadata.get("classification")
        if isinstance(raw, dict):
            return classification_from_metadata(raw).to_metadata()
    return DocumentClassification().to_metadata()


def _create_chunk_run_id(source_run_id: str, config_hash: str) -> str:
    digest = hashlib.sha256()
    digest.update(source_run_id.encode("ascii"))
    digest.update(config_hash.encode("ascii"))
    digest.update(str(time.time_ns()).encode("ascii"))
    return digest.hexdigest()[:16]


def _config_hash(config: ChunkingConfig, selected_engine_ids: Sequence[str]) -> str:
    payload = {
        "schema_version": CHUNK_SCHEMA_VERSION,
        "chunk_metadata_schema_version": CHUNK_METADATA_SCHEMA_VERSION,
        "search_text_schema_version": SEARCH_TEXT_SCHEMA_VERSION,
        "inquiry_chunk_metadata_schema_version": INQUIRY_CHUNK_METADATA_SCHEMA_VERSION,
        "inquiry_profile_contract_hash": inquiry_profile_contract_hash(),
        "strategy": CHUNK_STRATEGY,
        "selected_engine_ids": list(selected_engine_ids),
        "config": config.to_dict(),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _chunking_config_from_payload(payload: Any) -> ChunkingConfig:
    if not isinstance(payload, dict):
        return ChunkingConfig().validate()
    known_keys = set(ChunkingConfig.__dataclass_fields__)
    filtered = {key: value for key, value in payload.items() if key in known_keys}
    return ChunkingConfig(**filtered).validate()


def _normalize_chunk_metadata(metadata: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """保存済み metadata を v3 として検証し、旧形式は補完せず拒否します。"""
    normalized = _validate_chunk_metadata_v4(metadata)
    if normalized["content_hash"] != _text_sha256(str(payload.get("text") or "")):
        raise ValueError("chunk metadata v3 の content_hash が本文と一致しません。再チャンキングしてください。")
    return normalized


def _chunk_from_payload(payload: Any) -> DocumentChunk:
    if not isinstance(payload, dict):
        raise ValueError("chunk がオブジェクトではありません。")
    chunk_level = payload.get("chunk_level")
    if chunk_level not in {PARENT_CHUNK_LEVEL, CHILD_CHUNK_LEVEL}:
        raise ValueError("chunk_level が不正です。")
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    normalized_metadata = _normalize_chunk_metadata(metadata, payload)
    return DocumentChunk(
        chunk_id=_required_string(payload, "chunk_id"),
        chunk_level=chunk_level,
        chunk_seq=int(payload.get("chunk_seq") or 0),
        parent_chunk_id=str(payload.get("parent_chunk_id") or ""),
        child_chunk_ids=_string_list(payload.get("child_chunk_ids")),
        text=_required_string(payload, "text", allow_empty=True),
        retrieval_text=_required_string(payload, "retrieval_text", allow_empty=True),
        char_count=int(payload.get("char_count") or 0),
        token_estimate=int(payload.get("token_estimate") or 0),
        source_run_id=_required_string(payload, "source_run_id"),
        source_file_name=_required_string(payload, "source_file_name", allow_empty=True),
        source_engine_id=_required_string(payload, "source_engine_id"),
        source_engine_label=_required_string(payload, "source_engine_label", allow_empty=True),
        page_start=int(payload.get("page_start") or 0),
        page_end=int(payload.get("page_end") or 0),
        source_seq_ranges=payload.get("source_seq_ranges") if isinstance(payload.get("source_seq_ranges"), list) else [],
        source_record_refs=payload.get("source_record_refs") if isinstance(payload.get("source_record_refs"), list) else [],
        metadata=normalized_metadata,
    )
