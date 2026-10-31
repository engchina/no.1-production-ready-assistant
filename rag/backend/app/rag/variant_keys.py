"""variant(取込レシピ)の決定論キー計算 - 層別 materialization の基盤。

設計判断(multi-recipe-variants-decision)の実装基盤。「重複は共有・差分は複製」を
byte 単位の chunk 比較ではなく、artifact 層を「効く軸だけ」で hash して実現する。
各層は影響する取込軸だけをキーに含めるため、下流軸だけ異なる variant は上流層
(chunk_set / embedding)を共有でき、無駄な複製を避けられる。

層の依存:
    extraction_recipe (preprocess/parser)
        -> chunk_set (+ chunking) <- embedding は chunk text 従属で自動共有
        -> metadata 層 (+ field_extraction)
        -> graph 層    (+ graph_profile)
        -> nav 層      (+ navigation_summary / raptor)

すべて決定論(canonical JSON + SHA1)で、CI で実 Oracle なしに検証できる。実際の
materialize / 永続化 / refcount / GC は別モジュール(要 DDL・実 Oracle 検証)で行う。
"""

from __future__ import annotations

import hashlib
import json

from rag_parser_core.language import current_pack
from rag_parser_core.result import UNSTRUCTURED_TEXT_CONTRACT
from rag_parser_core.structured_records import JSONL_RECORD_BOUNDARY

from app.config import (
    FIXED_DELIMITER_STRATEGY,
    PREVIOUS_DEFAULT_CHUNK_DELIMITER,
    SMALL_TO_BIG_SETTING_FIELDS,
    Settings,
)
from app.rag.preprocess_strategy import preprocess_options

# キー算法の版。算法やフィールド構成を変えるときに上げて、旧キーと衝突させない。
KEY_VERSION = "v6"

# 前処理 csv_to_jsonl / excel_to_jsonl / word_to_jsonl の成果物の形式(抽出の ID に入れる。#2457)。
STRUCTURED_RECORDS_FORMAT_ID = "structured_records_jsonl_v1"
# 1 文書あたりの抽出(preprocess x parser 組合せ)上限。組合せ暴発の安全弁。
MAX_EXTRACTIONS_PER_DOCUMENT = 8

# parse/抽出結果を決める共通軸。backend 固有値は選択中のものだけを加える。
_COMMON_EXTRACTION_RECIPE_FIELDS: tuple[str, ...] = (
    "rag_preprocess_profile",
    "rag_preprocess_enabled",
    "rag_parser_adapter_backend",
    # Vision は解析エンジンに関係なく図の要素の本文を変える(#497)。変えると再解析になる。
    "rag_vision_enabled",
)
_BACKEND_EXTRACTION_RECIPE_FIELDS: dict[str, tuple[str, ...]] = {
    "docling": ("rag_parser_docling_enabled",),
    "unstructured": ("rag_parser_unstructured_enabled",),
    "structured_records_jsonl": ("rag_parser_structured_records_jsonl_enabled",),
    "xml_tree_json": ("rag_parser_xml_tree_json_enabled",),
}

# chunk text(と、それに従属する embedding)を決める分割軸。
# extraction_recipe_id + これらが同じなら chunk 集合は同一とみなして共有する。
_CHUNK_SET_FIELDS: tuple[str, ...] = (
    "rag_chunking_strategy",
    "rag_chunk_size",
    "rag_chunk_overlap",
    "rag_chunk_min_chars",
    "rag_chunk_delimiter",
    "rag_chunk_context_header_enabled",
)
# 削除した親子階層(#271)の子 chunk サイズ(rag_chunk_child_size の既定値)。hash 入力から
# 外すと既存の chunk_set_id がすべて変わるため、固定値で残して ID を保つ。
_REMOVED_CHUNK_CHILD_SIZE_HASH_VALUE = 320
# 削除した音声の文字起こしの設定(#3369。rag_parser_asr_enabled の既定値)。hash 入力から外すと
# 既存の extraction_recipe_id(と、それに従う chunk_set_id)がすべて変わるため、固定値で残す。
_REMOVED_ASR_ENABLED_HASH_VALUE = True
_SMALL_TO_BIG_STRATEGY = "small_to_big"
# #599 で分割方式の値と親子階層の 5 項目の属性名を改名した。hash の入力は改名の前の名前のまま
# 計算し、既存の chunk_set_id(chunk・embedding の共有と再処理の対象の特定に使う)を保つ。
# 名前を変えて ID が変わると、すべての文書の Chunk と embedding を作り直すことになる。
# 旧名は hash の入力にだけ使い、設定・保存値としては読まない。
_CHUNK_SET_HASH_NAMES: dict[str, str] = {
    "rag_chunk_child_target_chars": "rag_docrag_child_target_chars",
    "rag_chunk_table_child_target_chars": "rag_docrag_table_child_target_chars",
    "rag_chunk_parent_target_chars": "rag_docrag_parent_target_chars",
    "rag_chunk_parent_max_pages": "rag_docrag_parent_max_pages",
    "rag_chunk_parent_max_children": "rag_docrag_parent_max_children",
}
_CHUNK_SET_HASH_STRATEGY_VALUES: dict[str, str] = {_SMALL_TO_BIG_STRATEGY: "docrag_small_to_big"}
# #2457 で固定分割符の既定を ``\\n\\n`` から記録の境界のマーカーに変えた。分割符を使わない方式の
# hash の入力は、新しい既定のときだけ旧い既定のまま計算し、既存の chunk_set_id を保つ。

# 各派生層が「追加で」依存する軸(chunk_set_id に重ねて hash する)。
_METADATA_FIELDS: tuple[str, ...] = ("rag_field_extraction_enabled",)
_GRAPH_FIELDS: tuple[str, ...] = ("rag_graph_profile",)
_NAV_FIELDS: tuple[str, ...] = ("rag_navigation_summary_enabled", "rag_raptor_enabled")

_HASH_HEX_LEN = 16


def _digest(prefix: str, payload: dict[str, object]) -> str:
    """canonical JSON(キー順非依存)から決定論ハッシュ ID を作る。"""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    # 暗号用途ではなく決定論的 ID 生成のための SHA1(衝突耐性のみ必要)。
    digest = hashlib.sha1(canonical.encode("utf-8"), usedforsecurity=False).hexdigest()[
        :_HASH_HEX_LEN
    ]
    return f"{prefix}_{digest}"


def _fields(settings: Settings, names: tuple[str, ...]) -> dict[str, object]:
    """Settings から対象フィールド値を取り出す(欠落は None)。"""
    return {name: getattr(settings, name, None) for name in names}


def chunk_set_subset(settings: Settings) -> dict[str, object]:
    """chunk_set_id を決める分割軸を返す。

    親子階層（small-to-big）のパラメータは、その方式のときだけ、しかも既定値から変えた項目だけを
    加える。既定値のままなら追加前と同じ ID になり、既存の chunk_set をそのまま使える。
    """
    subset: dict[str, object] = {
        **_fields(settings, _CHUNK_SET_FIELDS),
        "rag_chunk_child_size": _REMOVED_CHUNK_CHILD_SIZE_HASH_VALUE,
    }
    if getattr(settings, "rag_chunking_strategy", None) == _SMALL_TO_BIG_STRATEGY:
        for name in SMALL_TO_BIG_SETTING_FIELDS:
            value = getattr(settings, name, None)
            if value != Settings.model_fields[name].default:
                subset[name] = value
    derived_flags = {
        name: True
        for name in (*_METADATA_FIELDS, *_NAV_FIELDS)
        if getattr(settings, name, False) is True
    }
    if derived_flags:
        subset["derived_content_contract"] = 1
        subset.update(derived_flags)
    return subset


def extraction_recipe_subset(settings: Settings) -> dict[str, object]:
    """ID と同じ非機密 extraction 軸を diagnostics 用に返す。"""
    backend = str(getattr(settings, "rag_parser_adapter_backend", "")).strip().casefold()
    subset: dict[str, object] = {
        **_fields(settings, _COMMON_EXTRACTION_RECIPE_FIELDS),
        **_fields(settings, _BACKEND_EXTRACTION_RECIPE_FIELDS.get(backend, ())),
    }
    # 前処理 excel_to_jsonl の選択肢は抽出結果を変える(#1221)。excel_to_jsonl のときだけ加え、
    # 他の前処理の ID は変えない。成果物の形式(#2457 で構造化レコードの JSONL)を変えたときは
    # csv_to_jsonl / excel_to_jsonl の形式の値を変え、その文書の抽出を作り直しにする。
    # 見出しの階層の推定(#2277)は抽出結果の見出しの階層を変える。有効のときだけ加え、既定(無効)の
    # ID は変えない。
    if backend == "docling":
        subset["docling_text_contract"] = "ordered_list_marker_v1"
    if backend == "unstructured":
        subset["unstructured_text_contract"] = UNSTRUCTURED_TEXT_CONTRACT
    if getattr(settings, "rag_heading_level_inference_enabled", False) is True:
        subset["rag_heading_level_inference_enabled"] = True
    profile = getattr(settings, "rag_preprocess_profile", None)
    if profile == "excel_to_jsonl":
        subset["rag_preprocess_excel_options"] = preprocess_options(settings, profile)
        subset["excel_to_jsonl_format"] = STRUCTURED_RECORDS_FORMAT_ID
    if profile == "csv_to_jsonl":
        subset["csv_to_jsonl_format"] = STRUCTURED_RECORDS_FORMAT_ID
    if profile == "word_to_jsonl":
        subset["word_to_jsonl_format"] = STRUCTURED_RECORDS_FORMAT_ID
    return subset


def compute_extraction_recipe_id(source_sha256: str, settings: Settings) -> str:
    """parse/抽出 recipe の決定論 ID。

    同一原本 + 同一の前処理/Parser なら保存済み extraction を再利用できる。ここが違う
    場合は同じ原本でも再抽出が必要で、review-only の後段索引だけでは安全に作れない。
    """
    payload: dict[str, object] = {
        "v": KEY_VERSION,
        "src": source_sha256,
        "language_pack_ingest_hash": current_pack().ingest_hash,
        **extraction_recipe_subset(settings),
        "rag_parser_asr_enabled": _REMOVED_ASR_ENABLED_HASH_VALUE,
    }
    return _digest("er", payload)


def compute_document_recipe_extraction_id(
    extraction_recipe_id: str,
    recipe_id: str,
    config_revision: int,
) -> str:
    """文書レシピと revision に閉じた extraction artifact ID。"""
    return _digest(
        "er",
        {
            "v": KEY_VERSION,
            "base": extraction_recipe_id,
            "recipe_id": recipe_id,
            "config_revision": config_revision,
        },
    )


def compute_chunk_set_id(source_sha256: str, settings: Settings) -> str:
    """chunk 集合(text + embedding 層)の決定論 ID。

    同一原本 + 同一の前処理/Parser/Chunking なら同じ ID = 共有対象。
    """
    extraction_recipe_id = compute_extraction_recipe_id(source_sha256, settings)
    payload: dict[str, object] = {
        "v": KEY_VERSION,
        "er": extraction_recipe_id,
        **_chunk_set_hash_input(chunk_set_subset(settings)),
    }
    return _digest("cs", payload)


def _chunk_set_hash_input(subset: dict[str, object]) -> dict[str, object]:
    """chunk_set_id の hash の入力を、#599 の改名の前の名前にそろえる(既存の ID を保つ)。"""
    hashed = {_CHUNK_SET_HASH_NAMES.get(name, name): value for name, value in subset.items()}
    strategy = hashed.get("rag_chunking_strategy")
    if (
        strategy != FIXED_DELIMITER_STRATEGY
        and hashed.get("rag_chunk_delimiter") == JSONL_RECORD_BOUNDARY
    ):
        hashed["rag_chunk_delimiter"] = PREVIOUS_DEFAULT_CHUNK_DELIMITER
    if isinstance(strategy, str):
        hashed["rag_chunking_strategy"] = _CHUNK_SET_HASH_STRATEGY_VALUES.get(strategy, strategy)
    return hashed


def compute_metadata_layer_id(chunk_set_id: str, settings: Settings) -> str:
    """メタデータ/項目抽出層の ID(chunk_set に field_extraction を重ねる)。"""
    return _digest(
        "md", {"v": KEY_VERSION, "cs": chunk_set_id, **_fields(settings, _METADATA_FIELDS)}
    )


def compute_graph_layer_id(chunk_set_id: str, settings: Settings) -> str:
    """GraphRAG 層の ID(chunk_set に graph_profile を重ねる)。"""
    return _digest("gr", {"v": KEY_VERSION, "cs": chunk_set_id, **_fields(settings, _GRAPH_FIELDS)})


def compute_nav_layer_id(chunk_set_id: str, settings: Settings) -> str:
    """ナビゲーション/RAPTOR 層の ID(chunk_set に nav 軸を重ねる)。"""
    return _digest("nv", {"v": KEY_VERSION, "cs": chunk_set_id, **_fields(settings, _NAV_FIELDS)})


def compute_layer_ids(source_sha256: str, settings: Settings) -> dict[str, str]:
    """1 取込レシピの全層 ID をまとめて返す(extraction/chunk_set/派生層)。"""
    extraction_recipe_id = compute_extraction_recipe_id(source_sha256, settings)
    chunk_set_id = compute_chunk_set_id(source_sha256, settings)
    return {
        "extraction_recipe_id": extraction_recipe_id,
        "chunk_set_id": chunk_set_id,
        "metadata_layer_id": compute_metadata_layer_id(chunk_set_id, settings),
        "graph_layer_id": compute_graph_layer_id(chunk_set_id, settings),
        "nav_layer_id": compute_nav_layer_id(chunk_set_id, settings),
    }
