"""Docling 解析結果を比較ビューア用の LayoutRecord へ変換するアダプター。"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Any

from rag_parser_core.extraction import ExtractionTableCell
from rag_parser_core.language import current_pack
from rag_parser_core.language.markers import marker_patterns

import rag_engine.resources.model_pool as model_pool
from rag_engine.adapters.parsers.base import (
    AdapterAvailability,
    AnalysisContext,
    extra_install_command,
    has_module,
    missing_dependency_message,
    object_to_plain,
)
from rag_engine.config import Settings
from rag_engine.models.layout import LayoutRecord
from rag_engine.parsing.categories import normalize_category
from rag_engine.parsing.content_labels import content_labels
from rag_engine.parsing.coordinates import clamp_bbox, pdf_bottom_left_to_image_top_left
from rag_engine.parsing.picture_text import format_docling_picture_ocr_text
from rag_engine.parsing.progress import ACTIVE_PAGE_PROGRESS, PageProgress
from rag_engine.parsing.rendering import pdf_image_object_bounds, pdf_text_lines_in_bbox
from rag_engine.parsing.table_structure import misplaced_cell_repairs, unassigned_table_lines

DOCLING_ROW_TOLERANCE_RATIO = 0.005
# 段組の判定に使う本文ブロックの最小幅（ページ幅比）。3 段組の 1 段（約 28%）は通し、
# 表の周辺で Docling が出す「2019」「年」のような断片テキストは除く。
DOCLING_COLUMN_BLOCK_MIN_WIDTH_RATIO = 0.2


class DoclingAdapter:
    """ローカル Docling DocumentConverter を使う標準解析アダプター。"""

    engine_id = "docling"
    label = "Docling"

    def __init__(self, settings: Settings):
        self.settings = settings

    def availability(self) -> AdapterAvailability:
        """必要な依存と設定が揃っているかを返します。"""
        if not has_module("docling"):
            return AdapterAvailability(False, missing_dependency_message("Docling", extra_install_command("docling")))
        return AdapterAvailability(True, f"Docling を `{self.settings.docling_device}` device で実行し、provenance bbox を読み取ります。")

    def preload(self) -> None:
        """DocumentConverter を事前に用意して常駐させる（UI の「ロード」ボタン用）。"""
        _prepare_docling_env(self.settings)
        self._converter()

    def _converter(self):
        signature = (
            self.settings.docling_device,
            self.settings.docling_num_threads,
            self.settings.docling_do_ocr,
            self.settings.docling_do_table_structure,
        )
        return model_pool.get(self.engine_id, lambda: _build_docling_converter(self.settings), signature=signature)

    def analyze(self, context: AnalysisContext) -> list[LayoutRecord]:
        """指定ページを変換して LayoutRecord を返し、部分成功・失敗時は RuntimeError を送出します。"""
        _prepare_docling_env(self.settings)
        converter = self._converter()
        # 解析対象ページだけ変換する（既定は PDF 全ページ変換で、1 ページ指定でも全体を処理してしまう）
        page_numbers = [page.page for page in context.pages]
        progress = PageProgress(context.run_dir, page_numbers)
        token = ACTIVE_PAGE_PROGRESS.set(progress)
        try:
            result = converter.convert(str(context.pdf_path), page_range=(min(page_numbers), max(page_numbers)))
        except Exception:
            progress.finish(False)
            raise
        finally:
            ACTIVE_PAGE_PROGRESS.reset(token)
        # Docling は raises_on_error=True でも PARTIAL_SUCCESS を返すため、
        # 欠落ページを含む結果で保存済みの全ファイル解析入力を上書きさせない。
        status = getattr(result.status, "value", result.status)
        # 飛び飛びのページ指定では最小〜最大の連続範囲を変換するため、指定外ページだけが
        # 失敗した partial_success は成功とする。全ページ解析では指定＝全ページなので対象外。
        succeeded = status == "success" or (status == "partial_success" and progress.completed == progress.expected)
        progress.finish(succeeded)
        if not succeeded:
            details = "; ".join(str(error.error_message) for error in result.errors)
            raise RuntimeError(f"Docling の解析が完了していません ({status})。{details}")
        document = result.document
        if hasattr(document, "export_to_dict"):
            payload = document.export_to_dict()
        elif hasattr(document, "model_dump"):
            payload = document.model_dump()
        else:
            payload = object_to_plain(document)
        return self._records_from_payload(payload, context, table_html_by_ref=_table_html_by_ref(document))

    def _records_from_payload(
        self,
        payload: dict[str, Any],
        context: AnalysisContext,
        table_html_by_ref: dict[str, str] | None = None,
    ) -> list[LayoutRecord]:
        page_lookup = {page.page: page for page in context.pages}
        counters: dict[int, int] = {}
        records: list[LayoutRecord] = []
        candidates: list[tuple[str, dict[str, Any]]] = []
        for key in ("texts", "tables", "pictures", "groups", "forms", "key_value_items"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.extend((key, item) for item in value if isinstance(item, dict))

        text_entries = [(index, item) for index, (key, item) in enumerate(candidates) if key == "texts"]
        texts_by_ref = {self_ref: item for _, item in text_entries if (self_ref := _item_self_ref(item))}
        pictures_by_ref = {self_ref: item for key, item in candidates if key == "pictures" and (self_ref := _item_self_ref(item))}
        picture_refs = set(pictures_by_ref)
        # 図内の箇条書きなどは group を挟んだ孫になる。直接の子だけを見ると、除外も OCR 集約も
        # されずに通常の Text として残るため、祖先の Picture までたどって対応付ける。
        nodes = _document_nodes(payload)
        picture_ref_by_text = {
            id(item): picture_ref for _, item in text_entries if (picture_ref := _picture_ancestor_ref(item, nodes)) in picture_refs
        }
        # caption は図の外にある文書テキスト（画面経路の行など）で、図内の文字ではない。
        # 図の子として捨てず、OCR 集約にも入れず、通常の record として残す。
        caption_ids = {id(item) for _, item in text_entries if _is_caption_item(item)}
        texts_by_picture_ref: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        for source_index, item in text_entries:
            if id(item) in picture_ref_by_text and id(item) not in caption_ids:
                texts_by_picture_ref.setdefault(picture_ref_by_text[id(item)], []).append((source_index, item))
        # 画面キャプチャの上に置かれた番号付き手順（吹き出し）は図内の文字ではなく文書の手順。caption と同様に
        # 通常の Text record として残し、行が分かれたものは先頭の item に 1 件にまとめる (#646)。
        instruction_heads: set[int] = set()
        merged_lines: set[int] = set()
        for picture_ref, picture in pictures_by_ref.items():
            for page_number in sorted(
                {
                    int(prov.get("page_no"))
                    for prov in picture.get("prov", []) or []
                    if isinstance(prov, dict) and isinstance(prov.get("page_no"), int)
                }
            ):
                page = page_lookup.get(page_number)
                if page is None:
                    continue
                ordered = _ordered_picture_children(
                    picture,
                    page_number=page_number,
                    page=page,
                    texts_by_ref=texts_by_ref,
                    parented_texts=texts_by_picture_ref.get(picture_ref, []),
                )
                for run in _instruction_runs(
                    [item for _, item in ordered if id(item) not in merged_lines and id(item) not in instruction_heads]
                ):
                    head, *rest = run
                    head["text"] = "".join(str(item.get("text") or "").strip() for item in run)
                    head[INSTRUCTION_CALLOUT_FLAG] = True
                    instruction_heads.add(id(head))
                    for item in rest:
                        item[INSTRUCTION_CALLOUT_FLAG] = True
                        merged_lines.add(id(item))

        # 画面キャプチャの text layer に由来する短いラベル（「管理者」「55555」）は、Docling が図の子にしなくても
        # 図の bbox 内にあれば図の文字。OCR 集約に入れ、独立した Text record（本文の根拠）にしない (#655)。
        # 吹き出しの手順（#646）、caption、文末を持つ文は残す。図の領域には、その図を描いた PDF の埋め込み画像の
        # 配置も含める（Docling の図の bbox が画像より狭いと、画像の端のタイトル・目盛りが見出し・本文になる。#3485）。
        image_regions: dict[str, list[tuple[int, list[float]]]] | None = None
        for source_index, item in text_entries:
            if id(item) in picture_ref_by_text or id(item) in caption_ids or id(item) in instruction_heads or id(item) in merged_lines:
                continue
            if not _is_short_label(str(item.get("text") or "")):
                continue
            if image_regions is None:
                image_regions = _picture_image_regions(pictures_by_ref, page_lookup, context.pdf_path)
            picture_ref = _containing_picture_ref(item, pictures_by_ref, page_lookup, image_regions=image_regions)
            if picture_ref:
                picture_ref_by_text[id(item)] = picture_ref
                texts_by_picture_ref.setdefault(picture_ref, []).append((source_index, item))

        for collection, item in candidates:
            if collection == "texts" and id(item) in merged_lines:
                continue  # 先頭の手順 item にまとめた行
            if (
                collection == "texts"
                and id(item) in picture_ref_by_text
                and id(item) not in caption_ids
                and id(item) not in instruction_heads
                and not self.settings.docling_keep_picture_child_text
            ):
                continue
            label = str(item.get("label") or item.get("name") or "text")
            # Docling は目次を TableItem(label=document_index) で返す。label だけで分類すると
            # Text 扱いになり、表に無い text フィールドを読んで内容が空になる。
            category = "Table" if collection == "tables" else normalize_category(label)
            provenances = [entry for entry in item.get("prov", []) or [] if isinstance(entry, dict)]
            # 表は HTML を決める前に text layer で隣の行へ入ったセルの文字を戻す。
            # 修正後の行で未割当の判定をするため、同じ行を後続の補足にも使う。
            table_lines: list[str] = []
            if collection == "tables":
                table_lines = _table_native_lines(item, context.pdf_path, provenances, page_lookup)
                repairs = _repair_table_cells(item, table_lines)
                if repairs:
                    item["table_cell_repairs"] = repairs
                cell_bboxes = _table_cell_bboxes(item, provenances, page_lookup)
                if cell_bboxes:
                    item[TABLE_CELL_BBOXES_KEY] = cell_bboxes
            text = _item_text(item, category, table_html_by_ref or {})
            # ページまたぎ等で prov が複数ある item は、全文を prov ごとに複製すると
            # チャンクと検索結果が重複する。charspan で分割できなければ最初の 1 件だけに持たせる。
            source_text = str(item.get("text") or item.get("orig") or item.get("caption") or "")
            restored_prefix = text[: -len(source_text)] if source_text and text != source_text and text.endswith(source_text) else ""
            span_texts = (
                _charspan_texts(source_text if restored_prefix else text, provenances) if category not in {"Table", "Picture"} else None
            )
            if span_texts is not None and restored_prefix:
                span_texts[0] = restored_prefix + span_texts[0]
            text_emitted = False
            aggregate_pages: set[int] = set()
            for prov_index, provenance in enumerate(provenances):
                page_number = int(provenance.get("page_no") or provenance.get("pageNo") or 0)
                page = page_lookup.get(page_number)
                if not page:
                    continue
                bbox_payload = provenance.get("bbox") or {}
                bbox = _docling_bbox_to_image(bbox_payload, page)
                if not bbox:
                    continue
                if span_texts is not None:
                    record_text = span_texts[prov_index]
                else:
                    record_text = "" if text_emitted else text
                    text_emitted = True
                counters[page_number] = counters.get(page_number, 0) + 1
                records.append(
                    LayoutRecord(
                        id=f"docling-p{page_number}-{counters[page_number]}",
                        engine=self.engine_id,
                        page=page_number,
                        seq_no=counters[page_number],
                        bbox=bbox,
                        coord_system="image_top_left",
                        page_width=page.width,
                        page_height=page.height,
                        category=category,
                        text=record_text,
                        confidence=None,
                        raw_type=label,
                        raw=item,
                    )
                )
                if collection == "tables" and record_text and table_lines:
                    unassigned = _table_unassigned_text(
                        item,
                        table_lines,
                        other_texts=[str(entry.get("text") or "") for _, entry in text_entries if _item_has_page(entry, page_number)],
                    )
                    if unassigned:
                        counters[page_number] += 1
                        records.append(
                            LayoutRecord(
                                id=f"docling-p{page_number}-{counters[page_number]}",
                                engine=self.engine_id,
                                page=page_number,
                                seq_no=counters[page_number],
                                bbox=list(bbox),
                                coord_system="image_top_left",
                                page_width=page.width,
                                page_height=page.height,
                                category="Text",
                                text=unassigned,
                                confidence=None,
                                raw_type="table_unassigned_text",
                                raw={
                                    "aggregation": "table_unassigned_native_text",
                                    "source_table_ref": _item_self_ref(item),
                                },
                            )
                        )
                # OCR 集約はページ単位の子テキストから作るため、同じページの prov では 1 回だけ作る。
                if collection != "pictures" or page_number in aggregate_pages:
                    continue
                aggregate_pages.add(page_number)
                aggregate = _picture_text_aggregate(
                    item,
                    page_number=page_number,
                    page=page,
                    texts_by_ref=texts_by_ref,
                    parented_texts=texts_by_picture_ref.get(_item_self_ref(item), []),
                )
                if aggregate is None:
                    continue
                aggregate_text, child_refs = aggregate
                counters[page_number] += 1
                records.append(
                    LayoutRecord(
                        id=f"docling-p{page_number}-{counters[page_number]}",
                        engine=self.engine_id,
                        page=page_number,
                        seq_no=counters[page_number],
                        bbox=list(bbox),
                        coord_system="image_top_left",
                        page_width=page.width,
                        page_height=page.height,
                        category="Picture",
                        text=aggregate_text,
                        confidence=None,
                        raw_type="picture_ocr_text",
                        raw={
                            "aggregation": "docling_picture_children",
                            "source_picture_ref": _item_self_ref(item),
                            "child_refs": child_refs,
                        },
                    )
                )
        return _sort_and_renumber_records(records, _reading_order_keys(payload), _multi_column_pages(payload, page_lookup))


def _charspan_texts(text: str, provenances: list[dict[str, Any]]) -> list[str] | None:
    """複数 prov の charspan で本文を分割する。1 件でも不正なら None（分割しない）。"""
    if len(provenances) < 2:
        return None
    pieces: list[str] = []
    for provenance in provenances:
        span = provenance.get("charspan")
        if not isinstance(span, (list, tuple)) or len(span) != 2:
            return None
        start, end = span
        if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(text):
            return None
        pieces.append(text[start:end].strip())
    return pieces


def _item_self_ref(item: dict[str, Any]) -> str:
    return str(item.get("self_ref") or "")


def _item_parent_ref(item: dict[str, Any]) -> str:
    parent = item.get("parent")
    return str(parent.get("$ref") or "") if isinstance(parent, dict) else ""


def _is_caption_item(item: dict[str, Any]) -> bool:
    return normalize_category(str(item.get("label") or item.get("name") or "text")) == "Caption"


INSTRUCTION_CALLOUT_FLAG = "instruction_callout"


# 番号付き手順の行頭（①、⑴、(1)、1.）と、手順の文末。画面キャプチャの上の吹き出しは行が途中で分かれる。
@lru_cache(maxsize=16)
def _instruction_regexes(_pack_hash: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """手順の行頭と文末(丸数字・番号の括弧と閉じ・手順の文末の語は言語資源。#2181)。"""
    m = marker_patterns()
    return (
        re.compile(rf"^\s*(?:{m.circled}|{m.number_open}\s*\d{{1,2}}\s*{m.number_close}|\d{{1,2}}\s*{m.number_closer})"),
        re.compile(rf"{m.instruction_ending}\s*$"),
    )


def _instruction_start() -> re.Pattern[str]:
    return _instruction_regexes(current_pack().hash)[0]


def _instruction_end() -> re.Pattern[str]:
    return _instruction_regexes(current_pack().hash)[1]


_INSTRUCTION_MAX_LINES = 4


def _ordered_picture_children(
    picture: dict[str, Any],
    *,
    page_number: int,
    page: Any,
    texts_by_ref: dict[str, dict[str, Any]],
    parented_texts: list[tuple[int, dict[str, Any]]],
) -> list[tuple[str, dict[str, Any]]]:
    """Picture の Text 子要素を Docling の明示順（直接の子）→ 座標順（孫）で並べる。caption は含めない。"""
    child_refs = [
        str(child.get("$ref") or "") for child in picture.get("children", []) or [] if isinstance(child, dict) and child.get("$ref")
    ]
    direct_ref_set = set(child_refs)
    ordered: list[tuple[str, dict[str, Any]]] = []
    for child_ref in child_refs:
        item = texts_by_ref.get(child_ref)
        # caption は図の外の文書テキストなので、図内文字の OCR 集約には入れない（通常の record として残る）。
        if item is not None and _item_has_page(item, page_number) and not _is_caption_item(item):
            ordered.append((child_ref, item))

    fallback = [
        (source_index, item)
        for source_index, item in parented_texts
        if _item_self_ref(item) not in direct_ref_set and _item_has_page(item, page_number)
    ]
    fallback = _sort_picture_fallback_texts(fallback, page_number, page)
    ordered.extend((_item_self_ref(item), item) for _, item in fallback)
    return ordered


def _instruction_runs(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """図の子テキストから番号付き手順の行の並びを取り出す (#646)。

    列挙記号で始まる行から、文末（。・ます・ください）で終わる行までを 1 つの手順とする。
    行が途中で分かれた吹き出し（「①新しい分類コード、名」「称を入力します。」）を 1 件にまとめるため。
    文末に届かない並び（画面上の番号だけの丸数字、UI ラベル）は手順にしない。
    """
    runs: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for item in items:
        # 丸数字は NFKC で普通の数字になり行頭判定から漏れるため、正規化前の文字で判定する。
        text = str(item.get("text") or "").strip()
        if _instruction_start().match(text):
            current = [item]
        elif current:
            current.append(item)
        else:
            continue
        if _instruction_end().search(text):
            runs.append(current)
            current = []
        elif len(current) >= _INSTRUCTION_MAX_LINES:
            current = []
    return runs


def _is_short_label(text: str) -> bool:
    """文でも手順でもない短い行（UI ラベル・値）。句点・ます・ください で終わらず、24 文字以内、列挙記号で始まらない。"""
    stripped = text.strip()
    compact = re.sub(r"\s+", "", stripped)
    return bool(compact) and len(compact) <= 24 and not _instruction_end().search(stripped) and not _instruction_start().match(stripped)


def _containing_picture_ref(
    item: dict[str, Any],
    pictures_by_ref: dict[str, dict[str, Any]],
    page_lookup: dict[int, Any],
    *,
    image_regions: dict[str, list[tuple[int, list[float]]]] | None = None,
) -> str:
    """item の bbox を完全に含む同じページの Picture の self_ref。無ければ空文字。

    先に Docling の図の bbox で探し、無ければ図を描いた埋め込み画像の配置（``_picture_image_regions``）で探す。
    """
    for provenance in item.get("prov", []) or []:
        if not isinstance(provenance, dict):
            continue
        page_number = int(provenance.get("page_no") or provenance.get("pageNo") or 0)
        page = page_lookup.get(page_number)
        box = _docling_bbox_to_image(provenance.get("bbox") or {}, page) if page else None
        if not box:
            continue
        for picture_ref, picture in pictures_by_ref.items():
            for picture_prov in picture.get("prov", []) or []:
                if not isinstance(picture_prov, dict) or int(picture_prov.get("page_no") or picture_prov.get("pageNo") or 0) != page_number:
                    continue
                area = _docling_bbox_to_image(picture_prov.get("bbox") or {}, page)
                if area and _box_contains(area, box):
                    return picture_ref
        for picture_ref, regions in (image_regions or {}).items():
            if any(region_page == page_number and _box_contains(region, box) for region_page, region in regions):
                return picture_ref
    return ""


def _box_contains(area: Sequence[float], box: Sequence[float], *, tolerance: float = 2.0) -> bool:
    return (
        area[0] - tolerance <= box[0] and area[1] - tolerance <= box[1] and box[2] <= area[2] + tolerance and box[3] <= area[3] + tolerance
    )


# 図の文字を探す領域に足す埋め込み画像の条件。図の bbox の大部分が画像に入り（``_PICTURE_INSIDE_IMAGE``）、図が画像の
# 大部分を占める（``_PICTURE_SHARE_OF_IMAGE``）ときだけ、その画像を図の画像とみなす。全ページのスキャン画像・背景の
# 画像・複数の図をまとめた画像で、図の領域を広げない。
_PICTURE_INSIDE_IMAGE = 0.8
_PICTURE_SHARE_OF_IMAGE = 0.5


def _picture_image_regions(
    pictures_by_ref: dict[str, dict[str, Any]], page_lookup: dict[int, Any], pdf_path: Any
) -> dict[str, list[tuple[int, list[float]]]]:
    """図ごとの、その図を描いた PDF の埋め込み画像の配置（ページ, 画像の座標の bbox）。

    Docling の図の bbox は画像より狭いことがあり、画像の端の文字（グラフのタイトル・軸の目盛り）が図の外になる。
    画像の中の文字は図の文字なので、図を描いた画像の配置も図の領域にする（#3485）。PDF でない入力・読めない
    PDF では空（解析は止めない）。
    """
    if not pictures_by_ref or not str(pdf_path).lower().endswith(".pdf"):
        return {}
    images_by_page: dict[int, list[list[float]]] = {}
    regions: dict[str, list[tuple[int, list[float]]]] = {}
    for picture_ref, picture in pictures_by_ref.items():
        for provenance in picture.get("prov", []) or []:
            if not isinstance(provenance, dict):
                continue
            page_number = int(provenance.get("page_no") or provenance.get("pageNo") or 0)
            page = page_lookup.get(page_number)
            area = _docling_bbox_to_image(provenance.get("bbox") or {}, page) if page else None
            if not area:
                continue
            if page_number not in images_by_page:
                try:
                    bounds = pdf_image_object_bounds(pdf_path, page_number)
                except Exception:
                    bounds = []
                images_by_page[page_number] = [
                    list(pdf_bottom_left_to_image_top_left(bound, page.pdf_width, page.pdf_height, page.width, page.height))
                    for bound in bounds
                ]
            regions.setdefault(picture_ref, []).extend(
                (page_number, image) for image in images_by_page[page_number] if _picture_drawn_by_image(area, image)
            )
    return {picture_ref: found for picture_ref, found in regions.items() if found}


def _picture_drawn_by_image(picture: Sequence[float], image: Sequence[float]) -> bool:
    picture_area = _box_area(picture)
    image_area = _box_area(image)
    if picture_area <= 0 or image_area <= 0:
        return False
    overlap = _box_area([max(picture[0], image[0]), max(picture[1], image[1]), min(picture[2], image[2]), min(picture[3], image[3])])
    return overlap >= picture_area * _PICTURE_INSIDE_IMAGE and picture_area >= image_area * _PICTURE_SHARE_OF_IMAGE


def _box_area(box: Sequence[float]) -> float:
    return max(0.0, float(box[2]) - float(box[0])) * max(0.0, float(box[3]) - float(box[1]))


def _picture_text_aggregate(
    picture: dict[str, Any],
    *,
    page_number: int,
    page: Any,
    texts_by_ref: dict[str, dict[str, Any]],
    parented_texts: list[tuple[int, dict[str, Any]]],
) -> tuple[str, list[str]] | None:
    """Picture の直接 Text 子要素を Docling の読み順で 1 件にまとめる。"""
    ordered = [
        (ref, item)
        for ref, item in _ordered_picture_children(
            picture, page_number=page_number, page=page, texts_by_ref=texts_by_ref, parented_texts=parented_texts
        )
        if not item.get(INSTRUCTION_CALLOUT_FLAG)
    ]  # 吹き出しの手順は通常の record に残す

    pieces: list[str] = []
    used_refs: list[str] = []
    for child_ref, item in ordered:
        text = str(item.get("text") or item.get("orig") or item.get("caption") or "")
        if not text.strip():
            continue
        pieces.append(text)
        used_refs.append(child_ref)
    if not pieces:
        return None
    return format_docling_picture_ocr_text("\n".join(pieces)), used_refs


def _table_native_lines(
    item: dict[str, Any], pdf_path: Any, provenances: Sequence[dict[str, Any]], page_lookup: dict[int, Any]
) -> list[str]:
    """表の最初の prov の範囲にある PDF text layer の行を返します。

    text layer のない入力（画像・スキャン PDF）や取得の失敗では空を返し、解析は止めない。
    ページをまたぐ表は最初のページの行だけを使う。
    """
    if not str(pdf_path).lower().endswith(".pdf"):
        return []
    for provenance in provenances:
        page_number = int(provenance.get("page_no") or provenance.get("pageNo") or 0)
        if page_number not in page_lookup:
            continue
        try:
            return list(pdf_text_lines_in_bbox(pdf_path, page_number, provenance.get("bbox") or {}))
        except Exception:
            return []
    return []


# 表のセルの領域（record の bbox と同じ画像の座標）。{"row", "col", "bbox"}（行・列は Docling の格子の
# 0 始まりの左上）の並び。根拠の場所の解析の表のセルの領域に使う（#3429）。
TABLE_CELL_BBOXES_KEY = "table_cell_bboxes"


def _table_cell_bboxes(item: dict[str, Any], provenances: Sequence[dict[str, Any]], page_lookup: dict[int, Any]) -> list[dict[str, Any]]:
    """Docling の表のセルの bbox を、record の bbox と同じ画像の座標にする。

    1 頁だけの表のときだけ作る（頁をまたぐ表のセルは、どの頁の座標か Docling の出力から決まらない）。
    """
    pages = {int(entry.get("page_no") or entry.get("pageNo") or 0) for entry in provenances}
    page = page_lookup.get(next(iter(pages))) if len(pages) == 1 else None
    if page is None:
        return []
    boxes: list[dict[str, Any]] = []
    for cell in (item.get("data") or {}).get("table_cells") or []:
        if not isinstance(cell, dict) or not isinstance(cell.get("bbox"), dict):
            continue
        row = _nonnegative_int(cell.get("start_row_offset_idx"))
        col = _nonnegative_int(cell.get("start_col_offset_idx"))
        bbox = _docling_bbox_to_image(cell["bbox"], page)
        if row is None or col is None or not bbox or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            continue
        boxes.append({"row": row, "col": col, "bbox": [round(float(value), 2) for value in bbox]})
    return boxes


def docling_table_cells(item: dict[str, Any]) -> list[ExtractionTableCell] | None:
    """Docling の表の格子（``data.table_cells``）を共通抽出の表のセルにする（#3429）。

    結合セルは左上の 1 つ（行・列は 0 始まり、span は end - start）。列の見出し・行の見出しの印
    （``column_header`` / ``row_header``）と、1 頁の表のセルの領域（``TABLE_CELL_BBOXES_KEY``。record の
    bbox と同じ座標）を持つ。位置の無いセル・同じ位置のセルがあれば、格子として読まないので None。
    """
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    boxes = {
        (entry.get("row"), entry.get("col")): entry.get("bbox")
        for entry in item.get(TABLE_CELL_BBOXES_KEY) or []
        if isinstance(entry, dict)
    }
    cells: list[ExtractionTableCell] = []
    starts: set[tuple[int, int]] = set()
    for cell in data.get("table_cells") or []:
        if not isinstance(cell, dict):
            return None
        row, col = _nonnegative_int(cell.get("start_row_offset_idx")), _nonnegative_int(cell.get("start_col_offset_idx"))
        if row is None or col is None or (row, col) in starts:
            return None
        starts.add((row, col))
        end_row, end_col = _nonnegative_int(cell.get("end_row_offset_idx")), _nonnegative_int(cell.get("end_col_offset_idx"))
        cells.append(
            ExtractionTableCell(
                row=row,
                col=col,
                text=str(cell.get("text") or ""),
                row_span=max(1, (end_row - row) if end_row is not None else 1),
                col_span=max(1, (end_col - col) if end_col is not None else 1),
                bbox=boxes.get((row, col)),
                metadata={key: True for key in ("column_header", "row_header") if cell.get(key) is True},
            )
        )
    return cells or None


def _table_grid_rows(item: dict[str, Any]) -> list[list[tuple[str, bool]]]:
    """Docling の grid を (本文, 自セルか) の行列にします。span で覆われた位置は False。"""
    rows: list[list[tuple[str, bool]]] = []
    for row_index, row in enumerate(((item.get("data") or {}).get("grid")) or []):
        if not isinstance(row, list):
            continue
        cells = []
        for col_index, cell in enumerate(row):
            if not isinstance(cell, dict):
                continue
            start_row = _nonnegative_int(cell.get("start_row_offset_idx"))
            start_col = _nonnegative_int(cell.get("start_col_offset_idx"))
            own = start_row in (None, row_index) and start_col in (None, col_index)
            cells.append((str(cell.get("text") or ""), own))
        rows.append(cells)
    return rows


def _repair_table_cells(item: dict[str, Any], lines: Sequence[str]) -> list[dict[str, Any]]:
    """隣の行へ入ったセルの文字を text layer で戻し、grid と table_cells の両方に反映します。

    修正の判定は misplaced_cell_repairs に従う。戻り値は raw に保存する修正の記録で、
    修正がなければ空。grid の各セル dict と table_cells の対応 dict を直接書き換える。
    """
    data = item.get("data") or {}
    grid = data.get("grid") or []
    applied: list[dict[str, Any]] = []
    for row, col, text in misplaced_cell_repairs(_table_grid_rows(item), lines):
        cell = grid[row][col]
        applied.append({"row": row, "column": col, "before": str(cell.get("text") or ""), "after": text})
        cell["text"] = text
        for entry in data.get("table_cells") or []:
            if (
                isinstance(entry, dict)
                and entry is not cell
                and _nonnegative_int(entry.get("start_row_offset_idx")) == row
                and _nonnegative_int(entry.get("start_col_offset_idx")) == col
            ):
                entry["text"] = text
    return applied


def _table_unassigned_text(item: dict[str, Any], lines: Sequence[str], *, other_texts: Sequence[str] = ()) -> str:
    """表のどの行のセルにも収まらなかった text layer の原文を、補足 record の本文として返します。

    グリフの bbox が不正な PDF では、Docling のセル割り当てが文字を落とす・隣の行へ入れることがある。
    隣の行へ入った分は _repair_table_cells で戻し、それでも残る欠けた行を原文で補う。
    lines は _table_native_lines で取った表の範囲の行。
    """
    grid = ((item.get("data") or {}).get("grid")) or []
    rows = [[str(cell.get("text") or "") for cell in row if isinstance(cell, dict)] for row in grid if isinstance(row, list)]
    if not rows:
        return ""
    missing = unassigned_table_lines(lines, rows, other_texts)
    return "\n".join([content_labels().text("table_unassigned") + ":", *missing]) if missing else ""


def _item_has_page(item: dict[str, Any], page_number: int) -> bool:
    return any(
        int(provenance.get("page_no") or provenance.get("pageNo") or 0) == page_number
        for provenance in item.get("prov", []) or []
        if isinstance(provenance, dict)
    )


def _item_page_sort_key(item: dict[str, Any], page_number: int, page: Any) -> tuple[float, float, float, float]:
    for provenance in item.get("prov", []) or []:
        if not isinstance(provenance, dict):
            continue
        provenance_page = int(provenance.get("page_no") or provenance.get("pageNo") or 0)
        if provenance_page != page_number:
            continue
        bbox = _docling_bbox_to_image(provenance.get("bbox") or {}, page)
        if bbox:
            return bbox[1], bbox[0], bbox[3], bbox[2]
    return float("inf"), float("inf"), float("inf"), float("inf")


def _sort_picture_fallback_texts(
    entries: list[tuple[int, dict[str, Any]]],
    page_number: int,
    page: Any,
) -> list[tuple[int, dict[str, Any]]]:
    decorated = [(source_index, item, _item_page_sort_key(item, page_number, page)) for source_index, item in entries]
    decorated.sort(key=lambda entry: (*entry[2], entry[0]))
    row_tolerance = max(1.0, float(page.height) * DOCLING_ROW_TOLERANCE_RATIO)
    rows: list[list[tuple[int, dict[str, Any], tuple[float, float, float, float]]]] = []
    row_top: float | None = None
    for entry in decorated:
        top = entry[2][0]
        if row_top is None or top - row_top > row_tolerance:
            rows.append([entry])
            row_top = top
        else:
            rows[-1].append(entry)

    ordered: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        row.sort(key=lambda entry: (entry[2][1], entry[2][0], entry[2][2], entry[2][3], entry[0]))
        ordered.extend((source_index, item) for source_index, item, _ in row)
    return ordered


def _table_html_by_ref(document: Any) -> dict[str, str]:
    """Docling の TableItem を self_ref ごとの HTML に変換する。"""
    table_html: dict[str, str] = {}
    for table in getattr(document, "tables", []) or []:
        self_ref = str(getattr(table, "self_ref", "") or "")
        export_to_html = getattr(table, "export_to_html", None)
        if not self_ref or not callable(export_to_html):
            continue
        try:
            html = str(export_to_html(doc=document, add_caption=False) or "").strip()
        except Exception:
            # 1 件の HTML 化失敗で解析全体を失敗させず、dict payload の後備変換へ任せる。
            continue
        if html:
            table_html[self_ref] = html
    return table_html


def _item_text(item: dict[str, Any], category: str, table_html_by_ref: dict[str, str]) -> str:
    if category == "Picture":
        # Picture 本体は Vision 説明用として空のまま残す。OCR は別の合成レコードへ格納する。
        return ""
    if category == "Table":
        self_ref = str(item.get("self_ref") or "")
        native_html = table_html_by_ref.get(self_ref, "").strip()
        # セルを修復した表は、Docling 文書から出した HTML に修正が反映されないため dict から作り直す。
        if native_html and not item.get("table_cell_repairs"):
            return native_html
        fallback_html = _table_data_to_html(item.get("data")) or native_html
        if fallback_html:
            return fallback_html
    text = str(item.get("text") or item.get("orig") or item.get("caption") or "")
    if str(item.get("label") or "").casefold() == "list_item":
        marker = str(item.get("marker") or "").strip()
        # 原文の番号を優先し、番号が本文にも入っている場合は重ねない。箇条書き記号は従来どおり。
        if marker and any(character.isdecimal() for character in marker):
            stripped = text.lstrip()
            original = str(item.get("orig") or "").strip()
            if original.startswith(marker) and original[len(marker) :].lstrip() == stripped:
                return original
            if not stripped.startswith(marker) or (len(stripped) > len(marker) and stripped[len(marker)].isdecimal()):
                return f"{marker} {stripped}"
    return text


def _table_data_to_html(data: Any) -> str:
    """Docling の table_cells から、表示可能な安全な HTML table を作る。"""
    if not isinstance(data, dict) or not isinstance(data.get("table_cells"), list):
        return ""

    parsed_cells: list[tuple[int, int, int, int, int, dict[str, Any]]] = []
    for source_index, cell in enumerate(data["table_cells"]):
        if not isinstance(cell, dict):
            continue
        row = _nonnegative_int(cell.get("start_row_offset_idx"))
        col = _nonnegative_int(cell.get("start_col_offset_idx"))
        if row is None or col is None:
            continue
        end_row = _nonnegative_int(cell.get("end_row_offset_idx"))
        end_col = _nonnegative_int(cell.get("end_col_offset_idx"))
        row_span = max(1, (end_row - row) if end_row is not None and end_row > row else _positive_int(cell.get("row_span")))
        col_span = max(1, (end_col - col) if end_col is not None and end_col > col else _positive_int(cell.get("col_span")))
        parsed_cells.append((row, col, row_span, col_span, source_index, cell))
    if not parsed_cells:
        return ""

    parsed_cells.sort(key=lambda entry: (entry[0], entry[1], entry[4]))
    cells_by_start: dict[tuple[int, int], tuple[int, int, dict[str, Any]]] = {}
    covered: set[tuple[int, int]] = set()
    for row, col, row_span, col_span, _, cell in parsed_cells:
        if (row, col) in covered or (row, col) in cells_by_start:
            continue
        cells_by_start[(row, col)] = (row_span, col_span, cell)
        for covered_row in range(row, row + row_span):
            for covered_col in range(col, col + col_span):
                if (covered_row, covered_col) != (row, col):
                    covered.add((covered_row, covered_col))

    inferred_rows = max(row + row_span for row, _, row_span, _, _, _ in parsed_cells)
    inferred_cols = max(col + col_span for _, col, _, col_span, _, _ in parsed_cells)
    num_rows = max(_positive_int(data.get("num_rows")), inferred_rows)
    num_cols = max(_positive_int(data.get("num_cols")), inferred_cols)
    rows: list[str] = []
    for row in range(num_rows):
        columns: list[str] = []
        col = 0
        while col < num_cols:
            if (row, col) in covered:
                col += 1
                continue
            cell_info = cells_by_start.get((row, col))
            if cell_info is None:
                columns.append("<td></td>")
                col += 1
                continue
            row_span, col_span, cell = cell_info
            tag = "th" if any(cell.get(key) is True for key in ("column_header", "row_header", "row_section")) else "td"
            attributes = ""
            if row_span > 1:
                attributes += f' rowspan="{row_span}"'
            if col_span > 1:
                attributes += f' colspan="{col_span}"'
            cell_text = escape(str(cell.get("text") or ""), quote=True)
            cell_text = cell_text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
            columns.append(f"<{tag}{attributes}>{cell_text}</{tag}>")
            col += col_span
        rows.append(f"<tr>{''.join(columns)}</tr>")
    return f"<table><tbody>{''.join(rows)}</tbody></table>"


def _nonnegative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _positive_int(value: Any) -> int:
    parsed = _nonnegative_int(value)
    return parsed if parsed is not None and parsed > 0 else 1


def _document_nodes(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    for key in ("body", "furniture"):
        if isinstance(payload.get(key), dict):
            nodes[f"#/{key}"] = payload[key]
    for key in ("texts", "tables", "pictures", "groups", "forms", "key_value_items"):
        for item in payload.get(key) or []:
            if isinstance(item, dict) and _item_self_ref(item):
                nodes[_item_self_ref(item)] = item
    return nodes


def _reading_order_keys(payload: dict[str, Any]) -> dict[str, tuple[int, int, int]]:
    """self_ref ごとに Docling の読み順で比較できる sort key を返す。

    テキストは texts 配列の順序が読み順。body ツリーはリスト項目を group へまとめるため、
    深さ優先でたどると項目の間にある本文が後ろへ回る。表・図は配列順では本文との前後が
    分からないので、ツリー上で直前までに現れたテキストの後ろへ置く。
    """
    nodes = _document_nodes(payload)
    text_index = {
        _item_self_ref(item): index
        for index, item in enumerate(payload.get("texts") or [])
        if isinstance(item, dict) and _item_self_ref(item)
    }
    keys: dict[str, tuple[int, int, int]] = {}
    visited: set[str] = set()
    latest_text = -1
    stack = [ref for ref in ("#/furniture", "#/body") if ref in nodes]
    while stack:
        ref = stack.pop()
        if ref in visited:
            continue
        visited.add(ref)
        if ref in text_index:
            latest_text = max(latest_text, text_index[ref])
            keys[ref] = (text_index[ref], 0, 0)
        else:
            keys[ref] = (latest_text, 1, len(visited))
        children = [str(child.get("$ref") or "") for child in nodes[ref].get("children", []) or [] if isinstance(child, dict)]
        stack.extend(child for child in reversed(children) if child in nodes and child not in visited)
    return keys


def _multi_column_pages(payload: dict[str, Any], page_lookup: dict[int, Any]) -> set[int]:
    """本文の読み順が「直前のブロックより完全に上、かつ完全に右」へ移るページを段組とみなす。

    同じ行に並ぶ項目（ラベルと値など）は縦に重なるため該当しない。ページヘッダー・フッター、
    図表の内側のテキスト、段として成立しない幅の断片は、本文の流れと無関係に位置が飛ぶため
    判定から除く。
    """
    nodes = _document_nodes(payload)
    pages: set[int] = set()
    previous: tuple[int, list[float]] | None = None
    for item in payload.get("texts") or []:
        if not isinstance(item, dict):
            continue
        if normalize_category(str(item.get("label") or "text")) in {"Page-header", "Page-footer"}:
            continue
        if _has_visual_ancestor(item, nodes):
            continue
        for provenance in item.get("prov", []) or []:
            if not isinstance(provenance, dict):
                continue
            page_number = int(provenance.get("page_no") or provenance.get("pageNo") or 0)
            page = page_lookup.get(page_number)
            bbox = _docling_bbox_to_image(provenance.get("bbox") or {}, page) if page else None
            if not bbox or bbox[2] - bbox[0] < page.width * DOCLING_COLUMN_BLOCK_MIN_WIDTH_RATIO:
                continue
            if previous and previous[0] == page_number and bbox[3] <= previous[1][1] and bbox[0] >= previous[1][2]:
                pages.add(page_number)
            previous = (page_number, bbox)
    return pages


def _picture_ancestor_ref(item: dict[str, Any], nodes: dict[str, dict[str, Any]]) -> str:
    """parent をたどって最初に現れる Picture の self_ref を返す。無ければ空文字。"""
    ref = _item_parent_ref(item)
    seen: set[str] = set()
    while ref and ref not in seen:
        if ref.startswith("#/pictures/"):
            return ref
        seen.add(ref)
        ref = _item_parent_ref(nodes.get(ref) or {})
    return ""


def _has_visual_ancestor(item: dict[str, Any], nodes: dict[str, dict[str, Any]]) -> bool:
    ref = _item_parent_ref(item)
    seen: set[str] = set()
    while ref and ref not in seen:
        if ref.startswith(("#/pictures/", "#/tables/")):
            return True
        seen.add(ref)
        ref = _item_parent_ref(nodes.get(ref) or {})
    return False


def _sort_and_renumber_records(
    records: list[LayoutRecord],
    reading_keys: dict[str, tuple[int, int, int]] | None = None,
    reading_order_pages: set[int] | None = None,
) -> list[LayoutRecord]:
    """ページごとに並べ直して連番を振り直す。通常は bbox の視覚行順、段組ページは Docling の読み順。

    視覚行順（上端 → 左端）は単段組の文書で安定しているが、多段組では左右の段が交互に並ぶ。
    reading_order_pages のページは reading_keys で並べる。key が 1 件でも欠けるページは
    視覚行順のままにする。
    """
    indexed_by_page: dict[int, list[tuple[int, LayoutRecord]]] = {}
    for source_index, record in enumerate(records):
        indexed_by_page.setdefault(record.page, []).append((source_index, record))

    ordered: list[LayoutRecord] = []
    for page_number in sorted(indexed_by_page):
        if page_number in (reading_order_pages or set()):
            # OCR 集約・表の補足 record は元の Picture / Table と同じ key にし、生成順で本体の直後へ置く。
            keyed = [
                (
                    (reading_keys or {}).get(
                        str(record.raw.get("source_picture_ref") or record.raw.get("source_table_ref") or record.raw.get("self_ref") or "")
                    ),
                    source_index,
                    record,
                )
                for source_index, record in indexed_by_page[page_number]
            ]
            if all(key is not None for key, _, _ in keyed):
                keyed.sort(key=lambda entry: (entry[0], entry[1]))
                for sequence, (_, _, record) in enumerate(keyed, start=1):
                    record.seq_no = sequence
                    record.id = f"docling-p{page_number}-{sequence}"
                    ordered.append(record)
                continue
        page_records = sorted(
            indexed_by_page[page_number],
            key=lambda entry: (
                entry[1].bbox[1],
                entry[1].bbox[0],
                entry[1].bbox[3],
                entry[1].bbox[2],
                entry[0],
            ),
        )
        page_height = max((record.page_height for _, record in page_records), default=0.0)
        row_tolerance = max(1.0, page_height * DOCLING_ROW_TOLERANCE_RATIO)
        rows: list[list[tuple[int, LayoutRecord]]] = []
        row_top: float | None = None
        for entry in page_records:
            top = entry[1].bbox[1]
            if row_top is None or top - row_top > row_tolerance:
                rows.append([entry])
                row_top = top
            else:
                rows[-1].append(entry)

        sequence = 0
        for row in rows:
            row.sort(
                key=lambda entry: (
                    entry[1].bbox[0],
                    entry[1].bbox[1],
                    entry[1].bbox[3],
                    entry[1].bbox[2],
                    entry[0],
                )
            )
            for _, record in row:
                sequence += 1
                record.seq_no = sequence
                record.id = f"docling-p{page_number}-{sequence}"
                ordered.append(record)
    return ordered


def _prepare_docling_env(settings: Settings) -> None:
    """互換起動だけキャッシュ環境を設定する。SDK はホストのプロセス環境を変更しない。"""
    from rag_engine.resources.runtime import current_runtime

    if current_runtime() is not None:
        return
    cache_root = (settings.output_dir / "_cache" / "docling").resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    # Docling と OCR / PyTorch 系のキャッシュをプロジェクト配下へ寄せる。
    os.environ.setdefault("XDG_CACHE_HOME", str(cache_root / "xdg"))
    os.environ.setdefault("HF_HOME", str(cache_root / "huggingface"))
    os.environ.setdefault("TORCH_HOME", str(cache_root / "torch"))
    os.environ.setdefault("EASYOCR_MODULE_PATH", str(cache_root / "easyocr"))
    os.environ.setdefault("MPLCONFIGDIR", str(cache_root / "matplotlib"))
    os.environ.setdefault("DOCLING_DEVICE", settings.docling_device)
    os.environ.setdefault("DOCLING_NUM_THREADS", str(settings.docling_num_threads))
    os.environ.setdefault("OMP_NUM_THREADS", str(settings.docling_num_threads))
    os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("ONNXRUNTIME_DISABLE_TELEMETRY", "1")


def _docling_artifacts_path(cache_dir: Path) -> Path | None:
    """取得済みの Docling のモデルの場所を返す(無ければ None。#829)。

    配備の手順は ``docling-tools models download`` でモデルを ``<docling の cache_dir>/models``
    (既定 ``~/.cache/docling/models``)へ取得する。この場所を ``artifacts_path`` に渡さないと、
    Docling は converter の作成のたびに HuggingFace Hub へ最新の版を問い合わせ、外へ出られない
    環境では接続の確立を待ち続ける。``DOCLING_ARTIFACTS_PATH`` を明示したときは Docling が
    それを読むので、ここでは上書きしない。取得していない(取得に失敗した)ときは None を返し、
    今までどおり初回の解析で HuggingFace Hub から取得させる。
    """
    if os.environ.get("DOCLING_ARTIFACTS_PATH", "").strip():
        return None
    models_dir = cache_dir / "models"
    try:
        if models_dir.is_dir() and any(models_dir.iterdir()):
            return models_dir
    except OSError:
        return None
    return None


def _build_docling_converter(settings: Settings):
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        AcceleratorDevice,
        AcceleratorOptions,
        PdfPipelineOptions,
        RapidOcrOptions,
        TableStructureOptions,
    )
    from docling.datamodel.settings import settings as docling_settings
    from docling.document_converter import DocumentConverter, PdfFormatOption

    from rag_engine.adapters.parsers.docling_progress import ProgressPdfPipeline

    device_value = settings.docling_device.lower()
    device = AcceleratorDevice.CPU if device_value == "cpu" else device_value
    pipeline_options = PdfPipelineOptions()
    artifacts_path = _docling_artifacts_path(Path(docling_settings.cache_dir))
    if artifacts_path is not None:
        pipeline_options.artifacts_path = artifacts_path
    pipeline_options.do_ocr = settings.docling_do_ocr
    pipeline_options.do_table_structure = settings.docling_do_table_structure
    pipeline_options.accelerator_options = AcceleratorOptions(num_threads=settings.docling_num_threads, device=device)
    if settings.docling_do_table_structure:
        pipeline_options.table_structure_options = TableStructureOptions(do_cell_matching=True)
    if settings.docling_do_ocr:
        pipeline_options.ocr_options = RapidOcrOptions()
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_cls=ProgressPdfPipeline, pipeline_options=pipeline_options)}
    )


def _docling_bbox_to_image(bbox: dict[str, Any], page) -> list[float] | None:
    if not bbox:
        return None
    left = bbox.get("l")
    top = bbox.get("t")
    right = bbox.get("r")
    bottom = bbox.get("b")
    if None in (left, top, right, bottom):
        return None
    origin = str(bbox.get("coord_origin") or bbox.get("coordOrigin") or "TOPLEFT").upper()
    raw = [float(left), float(top), float(right), float(bottom)]
    if origin == "BOTTOMLEFT":
        return pdf_bottom_left_to_image_top_left(raw, page.pdf_width, page.pdf_height, page.width, page.height)
    return clamp_bbox(
        [
            raw[0] * page.width / page.pdf_width,
            raw[1] * page.height / page.pdf_height,
            raw[2] * page.width / page.pdf_width,
            raw[3] * page.height / page.pdf_height,
        ],
        page.width,
        page.height,
    )
