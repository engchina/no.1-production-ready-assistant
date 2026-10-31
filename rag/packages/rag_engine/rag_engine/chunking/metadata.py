"""chunk metadata（schema v4）の組み立て・検証・投影。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from rag_engine.chunking.constants import (
    CHUNK_METADATA_SCHEMA_VERSION,
    HEADING_SOURCES,
    SECTION_HEADER_HEADING_SOURCE,
    _bool_value,
    _metadata_classification,
    _string_list,
    _text_sha256,
)
from rag_engine.chunking.evidence import (
    _caption_context,
    _display_regions,
    _footnote_context,
    _form_context,
    _form_image_evidence,
    _formula_context,
    _image_evidence,
    _list_context,
    _merged_image_evidence,
    _source_categories,
    _table_image_evidence,
)
from rag_engine.chunking.tables import _table_context
from rag_engine.knowledge.classification import DocumentClassification, classification_from_metadata
from rag_engine.knowledge.document_metadata import normalize_first_page_context
from rag_engine.retrieval.inquiry_conditions import build_inquiry_chunk_metadata


def _chunk_metadata(
    refs: Sequence[dict[str, Any]],
    metadata_by_page: dict[int, dict[str, Any]],
    page_start: int,
    page_end: int,
    *,
    section_path: Sequence[str],
    atomic: bool,
    text: str,
    retrieval_text: str,
    source_run_id: str,
    source_file_name: str,
    logical_page_labels: dict[str, int | str] | None = None,
    classification: DocumentClassification | None = None,
    section_path_sources: Sequence[str] = (),
    compute_retrieval_profile: bool = True,
) -> dict[str, Any]:
    """chunk の metadata（schema v4）を組み立てる。

    compute_retrieval_profile=False は child 用。child の retrieval_profile は検索文が確定した後に
    `_apply_contextual_child_search_text` が計算するため、生成時に計算しても捨てられる (#867)。

    分類は構築中だけ top-level の `classification` に置き、`_project_chunk_metadata` が `document.classification` へ
    移す（`document` は engine ごとの文書情報が後から付くため）。位置系は `layout`、問い合わせ profile は
    `retrieval_profile`、見出しの出所は `section_path_sources` に置く (#814)。
    """
    source_categories = _source_categories(refs)
    labels = logical_page_labels or {
        str(page): metadata_by_page.get(page, {}).get("logical_page")
        for page in range(page_start, page_end + 1)
        if metadata_by_page.get(page, {}).get("logical_page") is not None
    }
    display_regions = _display_regions(refs)
    table_context = _table_context(refs)
    form_context = _form_context(refs)
    image_evidence = _merged_image_evidence(
        _image_evidence(refs, source_run_id=source_run_id, source_file_name=source_file_name),
        _table_image_evidence(
            table_context,
            source_run_id=source_run_id,
            source_file_name=source_file_name,
        ),
        _form_image_evidence(
            refs,
            form_context,
            source_run_id=source_run_id,
            source_file_name=source_file_name,
        ),
    )
    caption_context = _caption_context(refs)
    footnote_context = _footnote_context(refs)
    list_context = _list_context(refs)
    formula_context = _formula_context(refs)
    padded_sources = list(section_path_sources) + [SECTION_HEADER_HEADING_SOURCE] * (len(section_path) - len(section_path_sources))
    clean_section_path = [item for item in section_path if item]
    clean_sources = [source for item, source in zip(section_path, padded_sources, strict=False) if item]
    retrieval_source = retrieval_text or text
    inquiry_metadata = (
        build_inquiry_chunk_metadata(
            text=text,
            retrieval_text=retrieval_source,
            source_file_name=source_file_name,
            source_categories=source_categories,
            page_start=page_start,
            page_end=page_end,
        )
        if compute_retrieval_profile
        else {}
    )
    metadata = {
        "schema_version": CHUNK_METADATA_SCHEMA_VERSION,
        "active": True,
        "atomic": atomic,
        "content_hash": _text_sha256(text),
        "classification": _chunk_classification_metadata(classification),
        "source_categories": source_categories,
        "section_path": clean_section_path,
        "section_path_sources": clean_sources,
    }
    layout = {
        "logical_page_labels": labels,
        "display_regions": display_regions,
        "caption_context": caption_context,
        "footnote_context": footnote_context,
        "list_context": list_context,
        "formula_context": formula_context,
        "form_context": form_context,
    }
    optional = {
        "image_evidence": image_evidence,
        "table_context": table_context,
        "retrieval_profile": inquiry_metadata,
    }
    metadata.update({key: _rounded_coordinates(value) for key, value in optional.items() if value})
    layout = {key: _rounded_coordinates(value) for key, value in layout.items() if value}
    if layout:
        metadata["layout"] = layout
    if "table_context" in metadata:
        metadata["table_context"] = [_without_table_cells(table) for table in metadata["table_context"]]
    return metadata


def _without_table_cells(table: Any) -> Any:
    """表のセル一覧を metadata へ重ねて保存しない。

    セルの内容は chunk の本文（行テキストまたは HTML）にあり、チャンキング後にセル一覧を
    読む処理はない。件数・見出し・行グループ・表内画像など、表を識別する情報は残す。
    """
    if not isinstance(table, dict) or not isinstance(table.get("structure"), dict):
        return table
    return {**table, "structure": {key: value for key, value in table["structure"].items() if key != "rows"}}


def _rounded_coordinates(value: Any) -> Any:
    """座標（bbox）を小数1桁へ丸めた写しを返す。

    解析結果の座標は十数桁の小数で、全 chunk の metadata に繰り返し入る。ページ画像上の
    ハイライトに必要な精度は1画素未満で足り、桁を残しても検索にも回答にも使われない。
    """
    if isinstance(value, dict):
        return {
            key: (
                [round(float(v), 1) if isinstance(v, (int, float)) and not isinstance(v, bool) else v for v in item]
                if key.endswith("bbox") and isinstance(item, list)
                else _rounded_coordinates(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_rounded_coordinates(item) for item in value]
    return value


def _chunk_classification_metadata(
    classification: DocumentClassification | dict[str, Any] | None,
) -> dict[str, Any]:
    """chunk filter に必要な分類だけを保存し、由来パスは run 側へ残します（版は chunk metadata の top に 1 つ）。"""
    normalized = classification_from_metadata(classification).to_metadata()
    return {
        "large_category": normalized["large_category"],
        "middle_category": normalized["middle_category"],
        "small_category": normalized["small_category"],
    }


_CHUNK_METADATA_REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "active",
        "atomic",
        "content_hash",
        "document",
        "section_path",
        "section_path_sources",
        "source_categories",
    }
)
# chunk 直下の任意項目。位置系は layout に、問い合わせ profile は retrieval_profile にまとめる (#814)。
_CHUNK_METADATA_OPTIONAL_KEYS = frozenset({"layout", "image_evidence", "table_context", "retrieval_profile"})
_CHUNK_LAYOUT_KEYS = frozenset(
    {
        "display_regions",
        "logical_page_labels",
        "native_text_ranges",
        "caption_context",
        "footnote_context",
        "list_context",
        "formula_context",
        "form_context",
    }
)


def _project_chunk_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """構築中 metadata を永続化用 v4 whitelist へ投影します。

    構築中は top-level にある分類を `document.classification` へ移し、`layout` の空項目を落とす。
    """
    document = dict(metadata.get("document") or {})
    document["classification"] = _chunk_classification_metadata(_metadata_classification(metadata))
    projected: dict[str, Any] = {
        "schema_version": CHUNK_METADATA_SCHEMA_VERSION,
        "active": _bool_value(metadata.get("active"), default=True),
        "atomic": _bool_value(metadata.get("atomic"), default=False),
        "content_hash": str(metadata.get("content_hash") or ""),
        "document": document,
        "section_path": _string_list(metadata.get("section_path")),
        "section_path_sources": _string_list(metadata.get("section_path_sources")),
        "source_categories": _string_list(metadata.get("source_categories")),
    }
    for key in _CHUNK_METADATA_OPTIONAL_KEYS - {"layout"}:
        value = metadata.get(key)
        if value:
            projected[key] = value
    layout = {key: value for key, value in (metadata.get("layout") or {}).items() if key in _CHUNK_LAYOUT_KEYS and value}
    if layout:
        projected["layout"] = layout
    return _validate_chunk_metadata_v4(projected)


def _validate_chunk_metadata_v4(metadata: Any) -> dict[str, Any]:
    """v4 metadata の型と whitelist を検証し、旧形式を補完せず拒否します。"""
    if not isinstance(metadata, dict) or metadata.get("schema_version") != CHUNK_METADATA_SCHEMA_VERSION:
        raise ValueError("対応していない chunk metadata schema_version です。再チャンキングしてください。")
    keys = set(metadata)
    missing = _CHUNK_METADATA_REQUIRED_KEYS - keys
    unknown = keys - _CHUNK_METADATA_REQUIRED_KEYS - _CHUNK_METADATA_OPTIONAL_KEYS
    if missing or unknown:
        raise ValueError("chunk metadata v4 の必須項目または許可項目が不正です。再チャンキングしてください。")
    if not isinstance(metadata.get("active"), bool) or not isinstance(metadata.get("atomic"), bool):
        raise ValueError("chunk metadata v4 の active/atomic が真偽値ではありません。")
    content_hash = metadata.get("content_hash")
    if not isinstance(content_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", content_hash):
        raise ValueError("chunk metadata v4 の content_hash が不正です。")
    document = metadata.get("document")
    if not isinstance(document, dict):
        raise ValueError("chunk metadata v4 の document が不正です。")
    classification = document.get("classification")
    category_keys = ("large_category", "middle_category", "small_category")
    if (
        not isinstance(classification, dict)
        or set(classification) != set(category_keys)
        or not all(isinstance(classification.get(key), str) for key in category_keys)
    ):
        raise ValueError("chunk metadata v4 の document.classification が不正です。")
    if "first_page_context" in document:
        normalize_first_page_context(document["first_page_context"])
    for key in ("section_path", "section_path_sources", "source_categories"):
        if not isinstance(metadata.get(key), list) or not all(isinstance(item, str) and item for item in metadata[key]):
            raise ValueError(f"chunk metadata v4 の {key} が配列ではありません。")
    if (
        len(metadata["section_path_sources"]) != len(metadata["section_path"])
        or not set(metadata["section_path_sources"]) <= HEADING_SOURCES
    ):
        raise ValueError("chunk metadata v4 の section_path_sources が section_path と対応していません。")
    for key in _CHUNK_METADATA_OPTIONAL_KEYS:
        if key in metadata and not metadata[key]:
            raise ValueError(f"chunk metadata v4 の空の {key} は保存できません。")
    for key in ("image_evidence", "table_context"):
        if key in metadata and not isinstance(metadata[key], list):
            raise ValueError(f"chunk metadata v4 の {key} が配列ではありません。")
    layout = metadata.get("layout")
    if layout is not None:
        if not isinstance(layout, dict) or set(layout) - _CHUNK_LAYOUT_KEYS:
            raise ValueError("chunk metadata v4 の layout に未対応項目があります。")
        for key, value in layout.items():
            if not value:
                raise ValueError(f"chunk metadata v4 の空の layout.{key} は保存できません。")
            if key == "logical_page_labels":
                if not isinstance(value, dict):
                    raise ValueError("chunk metadata v4 の layout.logical_page_labels が不正です。")
            elif not isinstance(value, list):
                raise ValueError(f"chunk metadata v4 の layout.{key} が配列ではありません。")
    profile = metadata.get("retrieval_profile")
    if profile is not None:
        if not isinstance(profile, dict) or set(profile) - {"business_domains", "document_kinds", "active_profiles"}:
            raise ValueError("chunk metadata v4 の retrieval_profile に未対応項目があります。")
        for key in ("business_domains", "document_kinds", "active_profiles"):
            if key in profile and (
                not isinstance(profile[key], list) or not profile[key] or not all(isinstance(item, str) and item for item in profile[key])
            ):
                raise ValueError(f"chunk metadata v4 の retrieval_profile.{key} が不正です。")
    return dict(metadata)
