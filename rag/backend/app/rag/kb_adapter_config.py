"""KB 単位の構築設定(per-knowledge-base build overrides)。

業界の RAG 製品(Dify / RAGFlow / FastGPT 等)に倣い、Parser / Chunking /
索引構築系の既定値を **ナレッジベース単位** で上書きできるようにする。
ただし確定スタック(OCI Enterprise AI / OCI Generative AI Cohere / Oracle AI Database)は
不変で、上書きは既存 preset の選択に限定する。

設計:

* KB が持つ正規の上書きは ``ingestion`` のみ。文書取込の瞬間に owning KB の設定で
  確定し(取込時スナップショット)、後から KB を変えても既存チャンクは作り直さない。
* ``query`` は V1 の互換読み取り用 legacy フィールドとして受け取るが、KB から runtime
  settings へは反映しない。検索・回答方針は Search Answer Profile が正本。
* Embedding/Rerank(Cohere v4/1536)・DB・OCI・Object Storage はグローバル固定で
  KB 別にしない(ベクトル次元混在不可など物理制約)。
* 永続化は既存 ``rag_knowledge_bases.retrieval_config`` JSON カラムを再利用し、
  DDL 変更を避ける。保存形は :func:`dump_adapter_config` 参照。
* 検索時の解決順は request 明示 > Search Answer Profile > グローバル既定。KB query 設定は使わない。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from rag_parser_core.sheet_records import ExcelOptions

from app.config import (
    CHUNK_CHILD_TARGET_CHARS_MAX,
    CHUNK_CHILD_TARGET_CHARS_MIN,
    CHUNK_OVERLAP_MAX_CHARS,
    CHUNK_PARENT_MAX_CHILDREN_MAX,
    CHUNK_PARENT_MAX_CHILDREN_MIN,
    CHUNK_PARENT_MAX_PAGES_MAX,
    CHUNK_PARENT_MAX_PAGES_MIN,
    CHUNK_PARENT_TARGET_CHARS_MAX,
    CHUNK_PARENT_TARGET_CHARS_MIN,
    CHUNK_SIZE_MAX_CHARS,
    CHUNK_SIZE_MIN_CHARS,
    CHUNK_TABLE_CHILD_TARGET_CHARS_MAX,
    CHUNK_TABLE_CHILD_TARGET_CHARS_MIN,
    REMOVED_PARSER_ADAPTER_BACKENDS,
    AnswerFlow,
    ChunkingStrategy,
    GraphProfile,
    ParserAdapterBackend,
    PreprocessProfile,
    QueryStrategy,
    Settings,
    VectorIndexProfile,
    is_roadmap_parser_adapter_backend,
    normalize_legacy_chunking_strategy_value,
    normalize_removed_preprocess_profile,
    reject_roadmap_parser_adapter_backend,
)

logger = logging.getLogger(__name__)

ADAPTER_CONFIG_VERSION = 2

# 実体の抽出で名前・属性にする列名（文書レシピの選択肢。#1362）。
EntityColumnName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)
]

AdapterConfigScope = Literal["ingestion", "query"]

# KB 設定フィールド -> Settings フィールドのマッピング(scope ごとの allowlist)。
# ここに載っていない Settings フィールドは KB 単位で上書きできない。
_INGESTION_FIELD_MAP: dict[str, str] = {
    "preprocess_profile": "rag_preprocess_profile",
    # 前処理 excel_to_jsonl の選択肢(#1221。前処理が excel_to_jsonl のときだけ効く)。
    "excel_options": "rag_preprocess_excel_options",
    "parser_adapter_backend": "rag_parser_adapter_backend",
    "parser_docling_enabled": "rag_parser_docling_enabled",
    "parser_unstructured_enabled": "rag_parser_unstructured_enabled",
    "parser_mineru_enabled": "rag_parser_mineru_enabled",
    "parser_dots_ocr_enabled": "rag_parser_dots_ocr_enabled",
    "parser_structured_records_jsonl_enabled": "rag_parser_structured_records_jsonl_enabled",
    "parser_xml_tree_json_enabled": "rag_parser_xml_tree_json_enabled",
    # 図・画像を AI で読み取る(Vision)。解析エンジンに関係なく効く(#497)。
    "vision_enabled": "rag_vision_enabled",
    # 見出しの階層の推定(#2277)。原本全体の整理の後に抽出結果の見出しの階層を変える。
    "heading_level_inference_enabled": "rag_heading_level_inference_enabled",
    "chunking_strategy": "rag_chunking_strategy",
    "chunk_size": "rag_chunk_size",
    "chunk_overlap": "rag_chunk_overlap",
    "chunk_min_chars": "rag_chunk_min_chars",
    # 固定分割の分割符(分割方式が fixed_delimiter のときだけ効く。#2457)。
    "chunk_delimiter": "rag_chunk_delimiter",
    # 親子階層（small-to-big）の分割パラメータ(分割方式が small_to_big のときだけ効く)。
    "chunk_child_target_chars": "rag_chunk_child_target_chars",
    "chunk_table_child_target_chars": "rag_chunk_table_child_target_chars",
    "chunk_parent_target_chars": "rag_chunk_parent_target_chars",
    "chunk_parent_max_pages": "rag_chunk_parent_max_pages",
    "chunk_parent_max_children": "rag_chunk_parent_max_children",
    # 取込側の高度軸(現状グローバルのみだった adapter を KB 上書き対象へ拡張)。
    # いずれも取込パイプラインが self._settings から読むため、KB 上書きが取込に効く。
    "graph_profile": "rag_graph_profile",
    # 実体の層（#1362。既定 ON。#1388）。索引の保存の後に実体と「実体と chunk の関連」を作る。
    "entity_index_enabled": "rag_entity_index_enabled",
    "entity_name_columns": "rag_entity_name_columns",
    "entity_attribute_columns": "rag_entity_attribute_columns",
    "field_extraction_enabled": "rag_field_extraction_enabled",
    "navigation_summary_enabled": "rag_navigation_summary_enabled",
    "auto_parse_after_preprocess_enabled": "rag_auto_parse_after_preprocess_enabled",
    "auto_chunk_after_extract_enabled": "rag_auto_chunk_after_extract_enabled",
    "auto_index_after_chunk_enabled": "rag_auto_index_after_chunk_enabled",
}
_QUERY_FIELD_MAP: dict[str, str] = {
    "query_strategy": "rag_query_strategy",
    "answer_flow": "rag_answer_flow",
    "neighbor_child_count": "rag_neighbor_child_count",
    "rerank_enabled": "rag_rerank_enabled",
    "screen_linking_enabled": "rag_screen_linking_enabled",
    "request_coverage_retrieval_enabled": "rag_request_coverage_retrieval_enabled",
    # 業務ガイドの手順ごとの検索(#2395)。None は全体の既定(on / 上限 4)。
    "guide_step_retrieval_enabled": "rag_guide_step_retrieval_enabled",
    "guide_step_max_queries": "rag_guide_step_max_queries",
    "guide_step_deadline_seconds": "rag_guide_step_deadline_seconds",
    "auto_field_filter_enabled": "rag_auto_field_filter_enabled",
    # 実体の 1 段の拡張(#1388)。None は Settings の既定(拡張は on。#1402 / 上限は 6)。
    "entity_expansion_enabled": "rag_entity_expansion_enabled",
    "entity_expansion_max_chunks": "rag_entity_expansion_max_chunks",
    # 関係情報で検索の候補を足す(#2055)。None は全体の既定(OFF / 上限 6)。
    "graph_expansion_enabled": "rag_graph_expansion_enabled",
    "graph_expansion_max_chunks": "rag_graph_expansion_max_chunks",
    # 章節の拡張(#1436)。None は Settings の既定(拡張は on / 4 段 / 50%)。
    "section_expansion_enabled": "rag_section_expansion_enabled",
    "section_expansion_max_levels": "rag_section_expansion_max_levels",
    "section_expansion_input_token_percent": (  # nosec B105 - 設定の名前
        "rag_section_expansion_input_token_percent"
    ),
    # rerank の後、起点に採る同じ文書の chunk の上限(#2167)。None は Settings の既定(5)。
    "max_anchors_per_document": "rag_max_anchors_per_document",
    # 業務ガイドの照合(#2398)。None は Settings の既定(どちらも OFF)。
    "support_guide_enabled": "rag_support_guide_enabled",
    "support_guide_semantic_match_enabled": "rag_support_guide_semantic_match_enabled",
    "support_guide_close_match_clarification_enabled": (
        "rag_support_guide_close_match_clarification_enabled"
    ),
}

# 外部 parser adapter backend -> その有効化 feature flag(Settings フィールド名)。
# KB が backend を明示選択したのに flag が無効だと取込が Enterprise AI へ fallback
# してしまうため、backend 選択を flag 有効化の意思表示として扱う。
_EXTERNAL_PARSER_BACKEND_FLAGS: dict[str, str] = {
    "docling": "rag_parser_docling_enabled",
    "unstructured": "rag_parser_unstructured_enabled",
    "structured_records_jsonl": "rag_parser_structured_records_jsonl_enabled",
    "xml_tree_json": "rag_parser_xml_tree_json_enabled",
    "mineru": "rag_parser_mineru_enabled",
    "dots_ocr": "rag_parser_dots_ocr_enabled",
}


class KbAdapterConfigError(ValueError):
    """KB 構築設定がグローバル設定と整合しないときに送出する。"""


# 削除した文書解析エンジン(#270)の有効化フラグ。保存済みの KB 構築設定・文書レシピ・
# 取込ジョブの snapshot に残っていても読めるよう、検証前に取り除く。
_REMOVED_PARSER_FLAG_FIELDS = frozenset(
    f"parser_{backend}_enabled" for backend in REMOVED_PARSER_ADAPTER_BACKENDS
)
# Vision を解析エンジンに依存しない ``vision_enabled`` へ統合した(#497)ときの旧 key。
# 読み込むときに ``vision_enabled`` へ移す。優先は新しい key の明示値 > Docling の Vision >
# 図表 VLM 要約。
_LEGACY_VISION_FIELDS: tuple[str, ...] = ("parser_docling_vision_enabled", "asset_summary_enabled")


def _migrate_legacy_vision_fields(data: dict[object, object]) -> dict[object, object]:
    """旧 key(Docling の Vision・図表 VLM 要約)を ``vision_enabled`` へ移し、旧 key は捨てる。"""
    if not any(key in data for key in _LEGACY_VISION_FIELDS):
        return data
    migrated = {key: value for key, value in data.items() if key not in _LEGACY_VISION_FIELDS}
    if migrated.get("vision_enabled") is None:
        for key in _LEGACY_VISION_FIELDS:
            value = data.get(key)
            if value is not None:
                migrated["vision_enabled"] = value
                break
    return migrated


def _clean_persisted_ingestion_config(data: object) -> object:
    """保存値・入力の取込設定から削除済みの項目を除き、旧値を読み替える(KnowledgeBaseIngestionConfig)。

    削除済みエンジン(#270)と準備中(ロードマップ)のエンジン(#3374)は「global 既定の文書解析エンジンを
    継承」(None)へ寄せる。準備中のエンジンを選ぶ保存(処理レシピの PUT)は、入力のモデル
    (``DocumentProcessingConfigInput``)が検証のエラーにする。
    """
    if not isinstance(data, dict):
        return data
    cleaned = {
        key: value
        for key, value in _migrate_legacy_vision_fields(data).items()
        if key not in _REMOVED_PARSER_FLAG_FIELDS
    }
    backend = cleaned.get("parser_adapter_backend")
    if isinstance(backend, str) and backend.strip().casefold() in REMOVED_PARSER_ADAPTER_BACKENDS:
        cleaned["parser_adapter_backend"] = None
    elif is_roadmap_parser_adapter_backend(backend):
        logger.warning(
            "保存済みの処理レシピの解析エンジン %s は準備中（ロードマップ）のため使わず、"
            "全体の既定を継承します。",
            backend,
        )
        cleaned["parser_adapter_backend"] = None
    # 削除したファイル準備の変換方式(#2042)は「変換なし」へ寄せる(ログに出す)。
    if "preprocess_profile" in cleaned:
        cleaned["preprocess_profile"] = normalize_removed_preprocess_profile(
            cleaned["preprocess_profile"]
        )
    return cleaned


def reject_roadmap_parser_backend_input(data: object) -> object:
    """処理レシピの保存の入力で、準備中(ロードマップ)の解析エンジンを検証のエラーにする(#3374)。"""
    if isinstance(data, Mapping):
        reject_roadmap_parser_adapter_backend(data.get("parser_adapter_backend"))
        # MinerU の flag(parser_mineru_enabled)で選ぶことも止める(#3359)。
        if data.get("parser_mineru_enabled") is True:
            reject_roadmap_parser_adapter_backend("mineru")
    return data


class KnowledgeBaseIngestionConfig(BaseModel):
    """取込時(Parser / Chunking)の KB 上書き。None はグローバル継承。"""

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def drop_removed_parser_engines(cls, data: object) -> object:
        """削除済みエンジン(#270)の保存値を「global 既定の文書解析エンジンを継承」へ寄せる。

        保存値は KB の ``retrieval_config``、文書・レシピの ``processing_config``、取込ジョブの
        ``settings_overrides`` に JSON で残る。読み込みは全てこのモデルの検証を通るため、ここで
        正規化すれば取込・画面の両方が壊れず、次回保存時に旧値は消える。文書レシピの
        ``DocumentProcessingConfig`` は ``extra="forbid"`` なので、旧フラグも検証前に取り除く。
        Vision の旧 key(#497)も同じ理由でここで ``vision_enabled`` へ移す。
        Oracle 上の JSON を書き換える migration は持たない(値の置換だけで済み、再保存で消えるため)。
        """
        return _clean_persisted_ingestion_config(data)

    preprocess_profile: PreprocessProfile | None = None
    # 前処理 excel_to_jsonl の選択肢(#1221)。None はグローバル継承。
    excel_options: ExcelOptions | None = None
    parser_adapter_backend: ParserAdapterBackend | None = None
    parser_docling_enabled: bool | None = None
    parser_unstructured_enabled: bool | None = None
    parser_mineru_enabled: bool | None = None
    parser_dots_ocr_enabled: bool | None = None
    parser_structured_records_jsonl_enabled: bool | None = None
    parser_xml_tree_json_enabled: bool | None = None
    vision_enabled: bool | None = None
    chunking_strategy: ChunkingStrategy | None = None
    chunk_size: int | None = Field(
        default=None,
        ge=CHUNK_SIZE_MIN_CHARS,
        le=CHUNK_SIZE_MAX_CHARS,
    )
    chunk_overlap: int | None = Field(default=None, ge=0, le=CHUNK_OVERLAP_MAX_CHARS)
    chunk_min_chars: int | None = Field(default=None, ge=0, le=2000)
    # 固定分割の分割符(escape 表現。#2457)。None はグローバル継承。
    chunk_delimiter: str | None = Field(default=None, min_length=1, max_length=256)
    chunk_child_target_chars: int | None = Field(
        default=None, ge=CHUNK_CHILD_TARGET_CHARS_MIN, le=CHUNK_CHILD_TARGET_CHARS_MAX
    )
    chunk_table_child_target_chars: int | None = Field(
        default=None,
        ge=CHUNK_TABLE_CHILD_TARGET_CHARS_MIN,
        le=CHUNK_TABLE_CHILD_TARGET_CHARS_MAX,
    )
    chunk_parent_target_chars: int | None = Field(
        default=None, ge=CHUNK_PARENT_TARGET_CHARS_MIN, le=CHUNK_PARENT_TARGET_CHARS_MAX
    )
    chunk_parent_max_pages: int | None = Field(
        default=None, ge=CHUNK_PARENT_MAX_PAGES_MIN, le=CHUNK_PARENT_MAX_PAGES_MAX
    )
    chunk_parent_max_children: int | None = Field(
        default=None, ge=CHUNK_PARENT_MAX_CHILDREN_MIN, le=CHUNK_PARENT_MAX_CHILDREN_MAX
    )
    # 取込側の高度軸(KB 上書き対象へ拡張)。None はグローバル継承。
    graph_profile: GraphProfile | None = None
    # 実体の層（#1362）。None はグローバル継承（既定 ON。#1388）。列名は表の行の名前・属性にする列。
    entity_index_enabled: bool | None = None
    entity_name_columns: list[EntityColumnName] | None = Field(default=None, max_length=20)
    entity_attribute_columns: list[EntityColumnName] | None = Field(default=None, max_length=40)
    heading_level_inference_enabled: bool | None = None
    field_extraction_enabled: bool | None = None
    navigation_summary_enabled: bool | None = None
    auto_parse_after_preprocess_enabled: bool | None = None
    auto_chunk_after_extract_enabled: bool | None = None
    auto_index_after_chunk_enabled: bool | None = None

    @field_validator("chunk_delimiter")
    @classmethod
    def _strip_chunk_delimiter(cls, value: str | None) -> str | None:
        """global の ``RAG_CHUNK_DELIMITER`` と同じく前後の空白を除き、空は受け付けない。"""
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("chunk_delimiter を入力してください。")
        return stripped

    @model_validator(mode="before")
    @classmethod
    def _normalize_removed_chunking_values(cls, data: object) -> object:
        """削除した分割方式の保存値を読み替える(文書レシピ・KB の保存済み JSON)。

        親子階層(hierarchical_parent_child)は親子階層（small-to-big）として扱い、その専用値
        ``chunk_child_size`` は捨てる。次に保存すると新しい値だけが残る(#271)。
        """
        if not isinstance(data, Mapping):
            return data
        values = {key: value for key, value in data.items() if key != "chunk_child_size"}
        if "chunking_strategy" in values:
            values["chunking_strategy"] = normalize_legacy_chunking_strategy_value(
                values["chunking_strategy"]
            )
        return values


class KnowledgeBaseQueryConfig(BaseModel):
    """検索・回答時の上書き。

    Search Answer Profile の query 設定として使う。KB 内に残る同形の値は legacy として読み取りは
    できるが、KB から検索 runtime へは反映しない。
    """

    model_config = ConfigDict(extra="ignore")

    # 旧 standard の回答エンジンの上書き(検索モード・検索オプション・根拠確認・回答スタイル。
    # retrieval_strategy / retrieval_* / post_retrieval_pipeline / generation_profile)は
    # #595 で削除した。保存済みの値は ``extra=ignore`` で読み込み時に捨て、次回保存で消える。
    vector_index_profile: VectorIndexProfile | None = Field(
        default=None,
        exclude=True,
        description=(
            "legacy 読み取り専用。共有検索インデックスは Search Answer Profile で上書きしない。"
        ),
    )
    # 品質評価(評価スイート)は検索・回答プロファイルで上書きし
    # ない。評価 API は検索・回答プロファイルを受け取らず、
    #
    # グローバル設定と request の suite だけで決まるため(#301)。保存済みの
    # ``evaluation_suite`` は ``extra=ignore`` で読み込み時に捨て、次回保存で消える。

    # 回答エンジンの選択(``answer_engine``)は #594 で削除した(回答は回答フローだけ)。保存済みの
    # 値は ``extra=ignore`` で読み込み時に捨て、次回保存で消える。
    # 回答フローの設定。None はグローバル継承。
    query_strategy: QueryStrategy | None = None
    answer_flow: AnswerFlow | None = None
    neighbor_child_count: int | None = Field(default=None, ge=0, le=20)
    rerank_enabled: bool | None = None
    # 画面目録で操作画面を探す(#554)。LLM の呼び出しが 1 回増える。
    screen_linking_enabled: bool | None = None
    # 根拠の無い要求だけを再検索する(#1279)。LLM は呼ばない(検索が 1 要求 1 回増える)。
    request_coverage_retrieval_enabled: bool | None = None
    # 照合した業務ガイドの手順ごとに検索し、手順の参照の頁の根拠を入れる(#2395)。LLM は呼ばない
    # (検索が手順ごとに増える)。None は全体の既定(on)。
    guide_step_retrieval_enabled: bool | None = None
    # 手順ごとの検索の回数の上限(参照の頁に絞った検索を含む)と期限(秒)。None は全体の既定(4 / 60)。
    # ON / OFF と同じく、検索・回答プロファイルでは保存・適用しない(PROFILE_GLOBAL_QUERY_FIELDS)。
    guide_step_max_queries: int | None = Field(default=None, ge=0, le=10)
    guide_step_deadline_seconds: float | None = Field(default=None, ge=0, le=60)
    # 質問から抽出項目の条件を読み取る(#652)。LLM の呼び出しが 1 回増える。
    auto_field_filter_enabled: bool | None = None
    # 質問と上位の候補の実体から、実体の表との SQL の join で関連する chunk を 1 段だけ足す
    # (#1362 / #1388)。LLM は呼ばない。実体・別名索引を作った文書だけが対象。
    # 検索方法の全体設定(既定 OFF)だけで決め、プロファイルでは保存・適用しない。
    entity_expansion_enabled: bool | None = None
    # 1 回の検索で足す chunk 数の上限。None は既定(6)。
    entity_expansion_max_chunks: int | None = Field(default=None, ge=1, le=20)
    # 関係情報(文書構造索引の構築で作った章節・表の見出しの entity と chunk の対応)で検索の候補を
    # 足す(#2055)。LLM は呼ばない。None は全体の既定(OFF。検索方法の画面)。
    graph_expansion_enabled: bool | None = None
    # 1 回の検索で足す chunk 数の上限。None は全体の既定(6)。ON / OFF と同じく、検索・回答
    # プロファイルでは保存・適用しない(PROFILE_GLOBAL_QUERY_FIELDS)。
    graph_expansion_max_chunks: int | None = Field(default=None, ge=1, le=20)
    # 当たった chunk の章節から親の章節へ段階的に広げる(#1436)。None は既定(on)、false は使わない。
    # 段ごとに CRAG の判定(LLM)が 1 回増える。上限(段・根拠の入力 token の割合・秒)も選べる。
    section_expansion_enabled: bool | None = None
    section_expansion_max_levels: int | None = Field(default=None, ge=1, le=8)
    section_expansion_input_token_percent: int | None = Field(default=None, ge=10, le=90)
    # rerank の後、起点(上位 top_k 件)に採る同じ文書の chunk の上限(#2167)。None は既定(5)、
    # 0 は上限なし。LLM は呼ばない。
    max_anchors_per_document: int | None = Field(default=None, ge=0, le=20)
    # 業務ガイドの意味の照合(#2398)。質問ごとに埋め込みの呼び出しが 1 回増える。None は全体の既定
    # (OFF)。
    # 未指定・null は OFF。既存プロファイルを自動的に有効へ変更しない。
    support_guide_enabled: bool | None = None
    support_guide_semantic_match_enabled: bool | None = None
    # 上位の 2 つの業務ガイドの点が近いとき、どのガイドかを確かめる(#2398)。None は全体の既定(OFF)。
    support_guide_close_match_clarification_enabled: bool | None = None


class KnowledgeBaseAdapterConfig(BaseModel):
    """KB 単位の構築設定。query は legacy 互換読み取り用。"""

    model_config = ConfigDict(extra="ignore")

    version: int = ADAPTER_CONFIG_VERSION
    ingestion: KnowledgeBaseIngestionConfig = Field(default_factory=KnowledgeBaseIngestionConfig)
    query: KnowledgeBaseQueryConfig = Field(default_factory=KnowledgeBaseQueryConfig)

    def is_empty(self) -> bool:
        """正規の KB 構築上書きが 1 件も無ければ True。legacy query は判定に含めない。"""
        return not self._scope_overrides("ingestion")

    def _scope_overrides(self, scope: AdapterConfigScope) -> dict[str, object]:
        """scope の非 None 上書きを {KB フィールド名: 値} で返す。"""
        section = self.ingestion if scope == "ingestion" else self.query
        return {key: value for key, value in section.model_dump().items() if value is not None}

    def settings_overrides(self, scope: AdapterConfigScope) -> dict[str, object]:
        """scope の上書きを {Settings フィールド名: 値} へ変換する。"""
        if scope == "query":
            # KB query は legacy。検索・回答方針は Search Answer Profile が正本なので反映しない。
            return {}
        field_map = _INGESTION_FIELD_MAP if scope == "ingestion" else _QUERY_FIELD_MAP
        overrides = {
            field_map[key]: value
            for key, value in self._scope_overrides(scope).items()
            if key in field_map
        }
        if scope == "ingestion":
            _ensure_external_parser_backend_enabled(overrides)
        return overrides


def _ensure_external_parser_backend_enabled(overrides: dict[str, object]) -> None:
    """外部 parser backend を選択したら対応 feature flag も有効化する。

    KB 設定 UI は backend だけを選ばせ、feature flag は別に持たない。グローバルの
    flag 既定は無効なので、backend 選択だけでは取込時に Enterprise AI へ fallback
    してしまう。backend 選択を「その adapter を使いたい」という明示の意思として扱い、
    KB が flag を明示的に False に上書きしていない限り True を注入する。
    パッケージ未導入時は parser registry 側が安全に fallback する。
    """
    backend = overrides.get("rag_parser_adapter_backend")
    if not isinstance(backend, str):
        return
    flag_field = _EXTERNAL_PARSER_BACKEND_FLAGS.get(backend.strip().casefold())
    if flag_field is None:
        return
    overrides.setdefault(flag_field, True)


def parse_adapter_config(
    raw: Mapping[str, object] | None,
) -> KnowledgeBaseAdapterConfig:
    """``retrieval_config`` JSON から KB 構築設定を寛容に復元する。

    旧来の free-form ``retrieval_config`` や未知キーは ``extra=ignore`` で捨て、
    壊れた値があっても空設定へ縮退して取込/検索を止めない。
    """
    if not raw:
        return KnowledgeBaseAdapterConfig()
    try:
        return KnowledgeBaseAdapterConfig.model_validate(dict(raw))
    except Exception:  # noqa: BLE001 - 壊れた永続値は空設定へ縮退する
        logger.warning("KB 構築設定の復元に失敗したため空設定へ縮退します。", exc_info=True)
        return KnowledgeBaseAdapterConfig()


def dump_adapter_config(config: KnowledgeBaseAdapterConfig) -> dict[str, object]:
    """KB 構築設定を ``retrieval_config`` カラムへ保存する dict へ変換する。

    V2 では ``query`` を保存しない。既存 JSON に残る query は parse できるが、次回保存で
    自然に落とす。
    """
    return {
        "version": ADAPTER_CONFIG_VERSION,
        "ingestion": config.ingestion.model_dump(mode="json"),
    }


def _validate_chunk_consistency(settings: Settings) -> None:
    """overlay 後の chunk パラメータ整合性を再検査する(Settings の起動時検証と同等)。"""
    if settings.rag_chunk_overlap >= settings.rag_chunk_size:
        raise KbAdapterConfigError("chunk_overlap は chunk_size より小さくしてください。")
    if settings.rag_chunk_min_chars >= settings.rag_chunk_size:
        raise KbAdapterConfigError("chunk_min_chars は chunk_size より小さくしてください。")


def resolve_effective_settings(
    global_settings: Settings,
    config: KnowledgeBaseAdapterConfig,
    *,
    scope: AdapterConfigScope,
) -> Settings:
    """グローバル設定へ KB の scope 上書きを重ねた有効 Settings を返す。

    上書きが無ければ ``global_settings`` をそのまま返す。``model_copy`` は
    バリデータを再実行しないため、ingestion scope では chunk 整合性を明示的に
    再検査し、矛盾があれば :class:`KbAdapterConfigError` を送出する。
    """
    if scope == "query":
        return global_settings
    overrides = config.settings_overrides(scope)
    if not overrides:
        return global_settings
    merged = global_settings.model_copy(update=overrides)
    if scope == "ingestion":
        _validate_chunk_consistency(merged)
    return merged


def _resolved_field_value(
    section: KnowledgeBaseIngestionConfig | KnowledgeBaseQueryConfig,
    kb_field: str,
    global_settings: Settings,
    settings_field: str,
) -> object:
    """override があればその値、無ければグローバル設定の対応値を返す。"""
    override = getattr(section, kb_field, None)
    if override is not None:
        return override
    return getattr(global_settings, settings_field)


def resolve_effective_adapter_config(
    global_settings: Settings,
    config: KnowledgeBaseAdapterConfig,
) -> KnowledgeBaseAdapterConfig:
    """KB 構築上書きをグローバル既定で埋めた「解決済み」設定を返す(UI 表示用)。

    ingestion 各フィールドは override があればその値、無ければグローバル設定の対応値で埋める。
    query は KB では legacy ignored のため空で返す。
    ``materialize`` には使わず **表示専用**。継承行に「実際に効く値」を出すために使う。
    """
    ingestion = {
        kb_field: _resolved_field_value(config.ingestion, kb_field, global_settings, settings_field)
        for kb_field, settings_field in _INGESTION_FIELD_MAP.items()
    }
    return KnowledgeBaseAdapterConfig(
        ingestion=KnowledgeBaseIngestionConfig(**ingestion),
        query=KnowledgeBaseQueryConfig(),
    )


def apply_adapter_config_or_global(
    global_settings: Settings,
    config: KnowledgeBaseAdapterConfig,
    *,
    scope: AdapterConfigScope,
) -> tuple[Settings, bool]:
    """有効 Settings と「上書きが適用されたか」を返す堅牢版。

    KB 設定がグローバルと矛盾する場合はグローバルへ縮退し、取込/検索を止めない。
    戻り値 2 番目は上書きが実際に効いたかどうか。
    """
    try:
        merged = resolve_effective_settings(global_settings, config, scope=scope)
    except KbAdapterConfigError:
        logger.warning(
            "KB 構築設定(%s)がグローバル設定と矛盾するためグローバルへ縮退します。",
            scope,
            exc_info=True,
        )
        return global_settings, False
    return merged, merged is not global_settings


def compose_query_settings(
    global_settings: Settings,
    overlays: Sequence[KnowledgeBaseQueryConfig],
) -> tuple[Settings, bool]:
    """Search Answer Profile の query 上書きを precedence 順(後勝ち)に重ねた Settings を返す。

    各 overlay の非 None フィールドだけが上位を上書きする(per-field merge)。解決順
    呼び出し側が ``overlays`` を **低優先 → 高優先** の順で渡すことで表現する
    (後の overlay が前を上書き)。

    1 件でも有効な上書きがあれば 2 番目は True。
    """
    merged_overrides: dict[str, object] = {}
    for overlay in overlays:
        # 後の overlay(高優先)が前を上書きする per-field merge。
        merged_overrides.update(query_settings_overrides(overlay))
    if not merged_overrides:
        return global_settings, False
    return global_settings.model_copy(update=merged_overrides), True


def query_settings_overrides(query: KnowledgeBaseQueryConfig) -> dict[str, object]:
    """Search Answer Profile query 設定を {Settings フィールド名: 値} へ変換する。"""
    values = {key: value for key, value in query.model_dump().items() if value is not None}
    return {
        settings_field: values[key]
        for key, settings_field in _QUERY_FIELD_MAP.items()
        if key in values
    }
