"""回答の記録・会話の回答に保存する引用と評価の入力を、使う項目だけにする（#1371）。

検索の結果（``RetrievedChunk``）の ``metadata`` には、索引の内部の項目
（``engine_metadata_json`` / ``engine_search_text`` / ``source_record_refs_json`` など）と
親の本文（``parent_text``）が入っていて、親の本文は子の数だけ重なる。そのまま保存すると
1 回答で 1MB を超えるため、保存するときは読む側が使う項目だけを残す（許可の一覧）。

保存した引用を読むのは次の所だけで、許可の一覧はここから決めている（MCP の ``rag_read_source``
などは chunk を読み直すので、保存した引用は使わない）。

- 画面の引用カード・根拠のプレビュー（``frontend/src/lib/chunk-metadata.ts`` / ``answer-text.ts`` /
  ``bbox.ts`` / ``components/search/CitationCard.tsx`` / ``CitationSourceSheet.tsx``）。
  保存済みの回答の詳細・会話の回答の再表示で同じ部品を使う。
- 保存した回答の評価の決定的な指標（``app/rag/answer_metrics.py`` /
  ``file_processing_evaluation.citation_traceability_coverage``）。
- 会話の回答へのフィードバックの引用の控え
  （``app/api/routes/feedback.py`` の ``_feedback_citations``）。

親子階層（small-to-big）の要素の表示領域は ``engine_metadata_json.layout.display_regions`` から
``display_regions`` へ取り出して持つ（画面は ``metadata.display_regions`` も読む）。

回答の診断（``diagnostics``）には、段落の印・根拠と矛盾して外した段落・回答全体の警告（#2738）を、
契約（``platform/contracts/answer-passages/passage-marks.json``）の項目だけにして持つ
（``passage_mark_record``）。保存済みの過去の回答は書き換えない（無ければ空の一覧として読む）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from app.schemas.search import RetrievedChunk

logger = logging.getLogger(__name__)

# 引用の metadata のうち保存する項目（許可の一覧）。使う所ごとに分けて並べる。
_PAGE_AND_SECTION_KEYS = (
    # 頁・印刷の頁番号・章節・内容の種類・分割の型（引用カードの印、本文の出典行と引用の対応）。
    "page",
    "page_number",
    "page_start",
    "page_end",
    "page_label_start",
    "page_label_end",
    "section_title",
    "section_path",
    "context_header",
    "context_header_enabled",
    "content_kind",
    "chunk_profile",
    "file_name",
    # 表計算の出典行（シート・行の範囲）。
    "sheet_name",
    "row_start",
    "row_end",
    # XML ツリー JSON の段落の引用ラベルと原典の URL（#2024。引用カードの印と出典の行）。
    "xml_tree_document_id",
    "xml_tree_document_title",
    "xml_tree_source_url",
    "xml_tree_citation_label",
    "xml_tree_citation_label_arabic",
    "xml_tree_part",
    "xml_tree_part_label",
)
_EVIDENCE_KEYS = (
    # 回答に使った根拠か・根拠の役割・本文の出典行の番号・検索の順位の印。
    "evidence_model_used",
    # 回答での使われ方（該当確認済み / 参考。#2040）と、親（まとまり）だけを参照した子の
    # 印（#2710）。
    "evidence_answer_use",
    "evidence_claim_support",
    "evidence_parent_used",
    "evidence_role",
    "context_role",
    # 出典行の番号（確定の子）・候補の番号・番号の決め方（#1330・#2710）。
    "answer_citation_lines",
    "answer_citation_candidate_lines",
    "answer_citation_provenance",
    "vector_rank",
    "keyword_rank",
    "rerank_rank",
    # 文書の版（処理レシピ・古い版の印）。
    "recipe_id",
    "recipe_slot_no",
    "document_superseded",
    # 監査・検索の調査（#1376）: 使った分割の版（文書の作り直しで古い chunk_set を片付けた後も
    # どの版の chunk かが分かる）と、ベクトル検索と全文検索を統合した値。
    "chunk_set_id",
    "rrf_score",
    # チャットの添付の根拠の印（添付の ID。#2517。画面は原文のプレビュー・文書を開かない）。
    "chat_attachment_id",
)
_PREVIEW_KEYS = (
    # 根拠のプレビューで開く要素・表のセル。
    "element_ids",
    "table_id",
    "parent_table_id",
    "formula_cell_refs",
    "formula_cell_ref",
    "table_cell_refs",
    "cell_refs",
    "table_cell_ref",
    "cell_ref",
    "table_cell_row",
    "cell_row",
    "row",
    "table_cell_col",
    "cell_col",
    "col",
    # 強調する領域の bbox と座標の解釈（platform/packages/ui/src/lib/bbox.ts の各 key の一覧と
    # 同じ）。原文のプレビュー（rag_read_source の include_preview。#2389）の強調にも使う。
    "bbox",
    "bbox_json",
    "bbox_xyxy",
    "bbox_xywh",
    "bounding_box",
    "boundingBox",
    "polygon",
    "points",
    "coordinates",
    "bbox_mode",
    "bbox_coordinate_mode",
    "bbox_format",
    "coordinate_mode",
    "bbox_unit",
    "bbox_coordinate_unit",
    "coordinate_unit",
    "unit",
    "bbox_x1",
    "bbox_y1",
    "bbox_x2",
    "bbox_y2",
    "x1",
    "y1",
    "x2",
    "y2",
    "left",
    "top",
    "right",
    "bottom",
    "xmin",
    "ymin",
    "xmax",
    "ymax",
    "bbox_x",
    "bbox_y",
    "bbox_width",
    "bbox_height",
    "bbox_right",
    "x",
    "y",
    "width",
    "height",
    "w",
    "h",
    "page_width",
    "page_height",
    "page_w",
    "page_h",
    "bbox_page_width",
    "bbox_page_height",
    "source_page_width",
    "source_page_height",
    "page_rotation",
    "bbox_page_rotation",
    "source_page_rotation",
    "rotation",
    "page_size",
    "pageSize",
    "dimensions",
    # 要素の表示領域（下の _display_regions が engine_metadata_json から取り出す）。
    "display_regions",
)
STORED_CITATION_METADATA_KEYS = frozenset(
    (*_PAGE_AND_SECTION_KEYS, *_EVIDENCE_KEYS, *_PREVIEW_KEYS)
)

# 評価の入力（evidence_items）の根拠ごとに残す項目。rag_engine の evaluate_answer_payload は
# 根拠を ``_evidence_fragments`` / ``source_texts`` で読み、この項目だけを使う（子は children）。
_EVALUATION_EVIDENCE_KEYS = (
    "id",
    "chunk_uid",
    "source",
    "source_run_id",
    "page_start",
    "page_end",
    "text",
)

# 1 回答の記録（引用と評価の入力の JSON の文字数の合計）がこれを超えたら警告を残す。
# 切り詰めはしない（引用を削ると画面の根拠が欠け、評価の入力を削ると評価の結果が変わるため）。
ANSWER_RECORD_WARN_CHARS = 512 * 1024


# 段落の印・外した段落・回答全体の警告（#2738）の記録の key と、各要素に残す項目（許可の一覧）。
PASSAGE_MARK_RECORD_KEYS = ("passage_marks", "removed_passages", "answer_warnings")
_PASSAGE_KEYS = ("text", "state", "reasons")
_REASON_KEYS = ("code", "label", "detail")


def _reason_entries(value: object) -> list[dict[str, str]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [
        {key: str(item.get(key) or "") for key in _REASON_KEYS}
        for item in value
        if isinstance(item, Mapping) and item.get("code")
    ]


def _passage_entry(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or not str(value.get("text") or "").strip():
        return None
    entry: dict[str, Any] = {
        key: str(value.get(key) or "") for key in _PASSAGE_KEYS if key != "reasons" and key in value
    }
    entry["reasons"] = _reason_entries(value.get("reasons"))
    return entry


def passage_mark_record(envelope: Mapping[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    """回答の構造（envelope）から、段落の印・外した段落・回答全体の警告を記録の形で取り出す（#2738）。

    契約の項目（text・state・reasons の code・label・detail）だけを残す。無い・古い記録は空の一覧。
    """
    source = envelope if isinstance(envelope, Mapping) else {}

    def entries(key: str) -> list[Any]:
        value = source.get(key)
        return (
            list(value)
            if isinstance(value, Sequence) and not isinstance(value, str | bytes)
            else []
        )

    return {
        "passage_marks": [
            entry for item in entries("passage_marks") if (entry := _passage_entry(item))
        ],
        "removed_passages": [
            entry for item in entries("removed_passages") if (entry := _passage_entry(item))
        ],
        "answer_warnings": _reason_entries(entries("answer_warnings")),
    }


def stored_citation_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """引用の metadata から保存する項目だけを残す（要素の表示領域は ``display_regions``）。"""
    stored = {
        key: value
        for key, value in metadata.items()
        if key in STORED_CITATION_METADATA_KEYS and value is not None
    }
    regions = _display_regions(metadata)
    if regions is not None:
        stored["display_regions"] = regions
    else:
        stored.pop("display_regions", None)
    return stored


def stored_citation(chunk: RetrievedChunk) -> RetrievedChunk:
    """保存する形の引用（metadata を許可の一覧に絞る。本文・ID・score はそのまま）。"""
    return chunk.model_copy(update={"metadata": stored_citation_metadata(chunk.metadata)})


def stored_citations(chunks: Iterable[RetrievedChunk]) -> list[dict[str, Any]]:
    """保存する引用の JSON（回答の記録・会話の回答の ``citations_json``）。"""
    return [stored_citation(chunk).model_dump(mode="json") for chunk in chunks]


def stored_evaluation_input(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """評価の入力の根拠（evidence_items）を、評価が読む項目だけにする。

    根拠ごとの検索の内部の項目（source_record_refs・display_regions・各 score など）は評価に
    渡らないので落とす。子（children）は本文と ID を残す（評価の引用を最も具体的な子に結ぶため）。
    ``chunk_uid`` が ``id`` と同じなら ``id`` だけにする。
    """
    if value is None:
        return None
    compact = dict(value)
    items = compact.get("evidence_items")
    if isinstance(items, Sequence) and not isinstance(items, str | bytes):
        compact["evidence_items"] = [_evaluation_evidence(item) for item in items]
    return compact


def _evaluation_evidence(item: object) -> object:
    if not isinstance(item, Mapping):
        return item
    compact = {key: item[key] for key in _EVALUATION_EVIDENCE_KEYS if key in item}
    if "chunk_uid" in compact and compact["chunk_uid"] == compact.get("id"):
        del compact["chunk_uid"]
    children = item.get("children")
    if isinstance(children, Sequence) and not isinstance(children, str | bytes) and children:
        compact["children"] = [_evaluation_evidence(child) for child in children]
    return compact


def answer_record_chars(*values: object) -> int:
    """保存する JSON の文字数（大きさの警告に使う）。"""
    return sum(len(json.dumps(value, ensure_ascii=False, default=str)) for value in values)


def warn_if_large_answer_record(
    *, trace_id: str, citations: object, evaluation_input: object
) -> None:
    """1 回答の記録が大きすぎるときに警告を残す（保存は続ける）。"""
    citations_chars = answer_record_chars(citations)
    evaluation_chars = answer_record_chars(evaluation_input)
    if citations_chars + evaluation_chars <= ANSWER_RECORD_WARN_CHARS:
        return
    logger.warning(
        "answer record is large",
        extra={
            "trace_id": trace_id,
            "citations_chars": citations_chars,
            "evaluation_input_chars": evaluation_chars,
            "limit_chars": ANSWER_RECORD_WARN_CHARS,
        },
    )


def _display_regions(metadata: Mapping[str, Any]) -> list[Any] | None:
    """画面（``displayRegionsFromMetadata``）と同じ順で要素の表示領域を探す。"""
    layout = _mapping(_engine_metadata(metadata).get("layout"))
    if layout is None:
        layout = _mapping(metadata.get("layout"))
    raw = layout.get("display_regions") if layout is not None else None
    if raw is None:
        raw = metadata.get("display_regions")
    return raw if isinstance(raw, list) else None


def _engine_metadata(metadata: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = metadata.get("engine_metadata_json")
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return _mapping(raw) or {}


def _mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None
