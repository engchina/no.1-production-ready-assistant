"""設定 API のスキーマ。secret はレスポンスに含めない。"""

from datetime import datetime
from typing import Literal

# OCI 認証の schema は3製品共通（platform の pr_system_settings。#100）。互換のため re-export する。
# データベース設定の schema は3製品共通（pr_system_settings。#108）。互換のため re-export する。
from pr_system_settings.database import AdbInfoData as AdbInfoData
from pr_system_settings.database import AdbOperationStatus as AdbOperationStatus
from pr_system_settings.database import AdbSettingsUpdate as AdbSettingsUpdate
from pr_system_settings.database import DatabaseConnectionTestResult as DatabaseConnectionTestResult
from pr_system_settings.database import DatabaseConnectionTestStatus as DatabaseConnectionTestStatus
from pr_system_settings.database import DatabaseSettingsData as DatabaseSettingsData
from pr_system_settings.database import DatabaseSettingsUpdate as DatabaseSettingsUpdate

# モデル設定の schema は3製品共通（pr_system_settings。#103）。互換のため re-export する。
from pr_system_settings.model import (
    EnterpriseAiModelEntrySettings as EnterpriseAiModelEntrySettings,
)
from pr_system_settings.model import EnterpriseAiModelSettings as EnterpriseAiModelSettings
from pr_system_settings.model import EnterpriseAiVlmInputMode as EnterpriseAiVlmInputMode
from pr_system_settings.model import GenerativeAiModelSettings as GenerativeAiModelSettings
from pr_system_settings.model import ModelSettingsData as ModelSettingsData
from pr_system_settings.model import ModelSettingsPayload as ModelSettingsPayload
from pr_system_settings.model import ModelSettingsSecretSource as ModelSettingsSecretSource
from pr_system_settings.model import ModelSettingsTestRequest as ModelSettingsTestRequest
from pr_system_settings.model import ModelSettingsTestResult as ModelSettingsTestResult
from pr_system_settings.model import ModelSettingsTestStatus as ModelSettingsTestStatus
from pr_system_settings.model import ModelSettingsTestTargetType as ModelSettingsTestTargetType
from pr_system_settings.oci import OciConfigField as OciConfigField
from pr_system_settings.oci import OciConfigReadData as OciConfigReadData
from pr_system_settings.oci import OciConfigReadRequest as OciConfigReadRequest
from pr_system_settings.oci import OciConfigTestResult as OciConfigTestResult
from pr_system_settings.oci import OciConfigTestStage as OciConfigTestStage
from pr_system_settings.oci import OciConfigTestStageKey as OciConfigTestStageKey
from pr_system_settings.oci import OciConfigTestStageStatus as OciConfigTestStageStatus
from pr_system_settings.oci import OciConfigTestStatus as OciConfigTestStatus
from pr_system_settings.oci import OciObjectStorageNamespaceData as OciObjectStorageNamespaceData
from pr_system_settings.oci import (
    OciObjectStorageNamespaceRequest as OciObjectStorageNamespaceRequest,
)
from pr_system_settings.oci import OciObjectStorageSettingsUpdate as OciObjectStorageSettingsUpdate
from pr_system_settings.oci import OciPrivateKeyUploadData as OciPrivateKeyUploadData
from pr_system_settings.oci import OciSettingsData as OciSettingsData
from pr_system_settings.oci import OciSettingsUpdate as OciSettingsUpdate
from pr_system_settings.system_schema import (
    SystemSchemaOperation,
    SystemSchemaOrphanOperation,
    SystemSchemaStatus,
)
from pr_system_settings.system_schema import SystemTableColumnData as SystemTableColumnData
from pr_system_settings.system_schema import (
    SystemTableDestructiveMigrationData as SystemTableDestructiveMigrationData,
)
from pr_system_settings.system_schema import SystemTableForeignKeyData as SystemTableForeignKeyData
from pr_system_settings.system_schema import SystemTableOperationState as SystemTableOperationState
from pr_system_settings.system_schema import (
    SystemTablesDeleteOrphansRequest as SystemTablesDeleteOrphansRequest,
)
from pr_system_settings.system_schema import (
    SystemTablesInitializeRequest as SystemTablesInitializeRequest,
)

# アップロード保存先の schema は3製品共通（platform の pr_system_settings。#97）。
# 互換のため re-export する。
from pr_system_settings.upload_storage import (
    UploadStorageSettingsData as UploadStorageSettingsData,
)
from pr_system_settings.upload_storage import (
    UploadStorageSettingsUpdate as UploadStorageSettingsUpdate,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from rag_parser_core.structured_records import JSONL_RECORD_BOUNDARY

from app.config import (
    CHUNK_CHILD_TARGET_CHARS_DEFAULT,
    CHUNK_CHILD_TARGET_CHARS_MAX,
    CHUNK_CHILD_TARGET_CHARS_MIN,
    CHUNK_OVERLAP_MAX_CHARS,
    CHUNK_PARENT_MAX_CHILDREN_DEFAULT,
    CHUNK_PARENT_MAX_CHILDREN_MAX,
    CHUNK_PARENT_MAX_CHILDREN_MIN,
    CHUNK_PARENT_MAX_PAGES_DEFAULT,
    CHUNK_PARENT_MAX_PAGES_MAX,
    CHUNK_PARENT_MAX_PAGES_MIN,
    CHUNK_PARENT_TARGET_CHARS_DEFAULT,
    CHUNK_PARENT_TARGET_CHARS_MAX,
    CHUNK_PARENT_TARGET_CHARS_MIN,
    CHUNK_SIZE_MAX_CHARS,
    CHUNK_SIZE_MIN_CHARS,
    CHUNK_TABLE_CHILD_TARGET_CHARS_DEFAULT,
    CHUNK_TABLE_CHILD_TARGET_CHARS_MAX,
    CHUNK_TABLE_CHILD_TARGET_CHARS_MIN,
    AnswerFlow,
    ChunkingStrategy,
    EvaluationSuite,
    GraphProfile,
    ParserAdapterBackend,
    PreprocessProfile,
    QueryStrategy,
    VectorIndexProfile,
)
from app.schemas.document import DocumentProcessingConfig
from app.schemas.language_pack_release import LanguagePackRelease

ParserAdapterBackendName = Literal[
    "docling",
    "unstructured",
    "structured_records_jsonl",
    "xml_tree_json",
]
ParserAdapterScoreBackendName = Literal[
    "local",
    "docling",
    "unstructured",
]
ParserAdapterStatus = Literal["active", "available", "disabled", "ignored", "missing"]
ParserAdapterScoreStatus = Literal[
    "recommended",
    "eligible",
    "available",
    "disabled",
    "ignored",
    "missing",
]

_CHUNKING_STRATEGIES_WITH_MIN_CHARS: set[ChunkingStrategy] = {
    "structure_aware",
    "recursive_character",
    "markdown_heading",
    "page_level",
}


# 状態・操作の型と、操作の状態・初期化の request は 3 製品共通（#325）。
SystemTableSchemaStatus = SystemSchemaStatus
SystemTableOperationResult = SystemSchemaOperation


class SystemTableObjectData(BaseModel):
    """system schema manifest の 1 object。"""

    name: str
    object_type: str


class SystemTableMetadata(BaseModel):
    """管理対象テーブルの dictionary metadata。"""

    name: str
    exists: bool
    estimated_rows: int | None = None
    created_at: str | None = None
    last_analyzed_at: str | None = None


class SystemObjectMetadata(SystemTableMetadata):
    """管理対象の 1 object（テーブル・索引・Oracle Text の設定）の存在と統計。"""

    object_type: str


class SystemTablesStatusData(BaseModel):
    """RAG system table の read-only status。"""

    status: SystemTableSchemaStatus
    schema_version: str
    schema_head: str
    applied_versions: list[str]
    pending_versions: list[str]
    # 未適用の、データを消す migration（「作成・更新」の前に承認が要る。#619）。
    pending_destructive_migrations: list[SystemTableDestructiveMigrationData] = Field(
        default_factory=list
    )
    expected_object_count: int
    existing_object_count: int
    expected_table_count: int
    existing_table_count: int
    missing_objects: list[SystemTableObjectData]
    retired_objects: list[SystemTableObjectData]
    # 既存の表に無い正本の列（台帳が適用済みでも欠けた列。状態は outdated。#2450）。
    missing_columns: list[SystemTableColumnData] = Field(default_factory=list)
    # 既存の表に無い正本の外部キー（更新で足す）と、既存の行を検査していない外部キーに残る
    # 参照先の無い行（#505）。
    missing_foreign_keys: list[SystemTableForeignKeyData] = Field(default_factory=list)
    orphaned_foreign_keys: list[SystemTableForeignKeyData] = Field(default_factory=list)
    # 削除規則が正本と違う外部キーと、無効化された外部キー（更新で直す。#511）。
    mismatched_foreign_keys: list[SystemTableForeignKeyData] = Field(default_factory=list)
    disabled_foreign_keys: list[SystemTableForeignKeyData] = Field(default_factory=list)
    tables: list[SystemTableMetadata]
    # 全管理 object（詳細の一覧。概要の object の件数と同じ。#658）。
    objects: list[SystemObjectMetadata] = Field(default_factory=list)
    operation_state: SystemTableOperationState


class SystemTablesOperationData(SystemTablesStatusData):
    """DDL operation 後の状態と件数。"""

    operation: SystemTableOperationResult
    dropped_object_count: int
    created_object_count: int


class SystemTablesOrphanDeletionData(SystemTablesStatusData):
    """参照先のない行の削除（#511）の後の状態と、削除した行数・対象の外部キー。"""

    operation: SystemSchemaOrphanOperation
    deleted_row_count: int
    foreign_key: SystemTableForeignKeyData


class HuggingFaceSettingsData(BaseModel):
    """HuggingFace モデルダウンロード設定の表示用データ(token 実値は返さない)。"""

    endpoint: str
    token_configured: bool
    config_source: Literal["runtime"]


class HuggingFaceSettingsUpdate(BaseModel):
    """HuggingFace 設定の更新 payload。

    token は未指定または空文字なら既存値を保持する。clear_token が true の場合だけ削除する。
    """

    model_config = ConfigDict(extra="forbid")

    endpoint: str = Field(default="", max_length=512)
    token: str | None = Field(default=None, max_length=4096)
    clear_token: bool = False

    @field_validator("endpoint")
    @classmethod
    def strip_text(cls, value: str) -> str:
        """前後空白を設定値へ混入させない。"""
        return value.strip()


class ParserAdapterStatusData(BaseModel):
    """任意 parser adapter の feature flag / package readiness。"""

    backend: ParserAdapterBackendName
    package_name: str
    import_name: str
    distribution_name: str | None = None
    install_package: str
    enabled: bool
    selected: bool
    installed: bool
    status: ParserAdapterStatus
    version: str | None = None
    warning_code: str | None = None


class ParserAdapterScorecardEntryData(BaseModel):
    """parser backend 推奨 scorecard の 1 行。"""

    backend: ParserAdapterScoreBackendName
    rank: int
    score: float
    status: ParserAdapterScoreStatus
    recommended: bool
    executable: bool
    selected: bool
    enabled: bool
    installed: bool
    metric_source: str
    metric_count: int
    signals: dict[str, float] = Field(default_factory=dict)
    reason_codes: list[str] = Field(default_factory=list)
    warning_codes: list[str] = Field(default_factory=list)


class ParserAdapterScorecardData(BaseModel):
    """parser backend の評価駆動推奨。"""

    selected_backend: ParserAdapterBackend
    recommended_backend: ParserAdapterScoreBackendName
    metrics_source: str
    metrics_applied_to: ParserAdapterScoreBackendName | None = None
    entries: list[ParserAdapterScorecardEntryData]


class ParserAdapterSourceRouteData(BaseModel):
    """source kind ごとの adapter routing evidence。"""

    source_kind: str
    candidate_order: list[ParserAdapterScoreBackendName] = Field(default_factory=list)
    attempted_order: list[ParserAdapterScoreBackendName] = Field(default_factory=list)
    active_order: list[ParserAdapterScoreBackendName] = Field(default_factory=list)
    selected_backend: ParserAdapterScoreBackendName
    reason_codes: list[str] = Field(default_factory=list)
    warning_codes: list[str] = Field(default_factory=list)


class ParserAdapterBackendSourceMatrixData(BaseModel):
    """runtime 設定から見た backend-source routing matrix。"""

    evidence_source: Literal["runtime_routes"]
    required_source_kinds: list[str] = Field(default_factory=list)
    covered_source_kinds: list[str] = Field(default_factory=list)
    missing_source_kinds: list[str] = Field(default_factory=list)
    backend_source_kinds: dict[ParserAdapterScoreBackendName, list[str]] = Field(
        default_factory=dict
    )
    route_evidence: list[ParserAdapterSourceRouteData] = Field(default_factory=list)


class ParserBackendCapabilityData(BaseModel):
    """backend が処理できる原本形式の宣言(rag_parser_core.capabilities 正本)。"""

    backend: str
    modalities: list[str] = Field(default_factory=list)
    extensions: list[str] = Field(default_factory=list)


class ParserAdapterSettingsData(BaseModel):
    """任意 parser adapter 設定の非機密 runtime snapshot。"""

    adapter_backend: ParserAdapterBackend
    effective_order: list[ParserAdapterBackendName]
    adapters: list[ParserAdapterStatusData]
    scorecard: ParserAdapterScorecardData
    source_routes: list[ParserAdapterSourceRouteData] = Field(default_factory=list)
    backend_source_kind_matrix: ParserAdapterBackendSourceMatrixData
    capabilities: list[ParserBackendCapabilityData] = Field(default_factory=list)
    # 「解析後の処理」の全体の既定（#528）。文書のレシピで上書きしないときに使う。
    # 保存先は parser-settings.json ではなく RAG の backend/.env（RAG_VISION_ENABLED など）。
    vision_enabled: bool = False
    heading_level_inference_enabled: bool = False
    field_extraction_enabled: bool = False
    navigation_summary_enabled: bool = False
    raptor_enabled: bool = False
    navigation_summary_max_nodes: int
    # 図・表の説明（Vision）を同時に呼ぶ数の上限（RAG_VISION_CONCURRENCY。#2002）。
    vision_concurrency: int = 4
    config_source: Literal["runtime"]


# 「解析後の処理」の項目（#528）。parser の選択（parser-settings.json）とは保存先が違う。
POST_PARSE_SETTING_FIELDS = (
    "vision_enabled",
    "heading_level_inference_enabled",
    "field_extraction_enabled",
    "navigation_summary_enabled",
    "raptor_enabled",
    "vision_concurrency",
)


class ParserAdapterSettingsUpdate(BaseModel):
    """任意 parser adapter feature flags と「解析後の処理」の更新 payload。

    `adapter_backend` を省略したときは解析エンジンの設定を変えない
    （「解析後の処理」だけを保存する）。
    省略した「解析後の処理」の項目も変えない。
    """

    adapter_backend: ParserAdapterBackend | None = None
    docling_enabled: bool | None = None
    unstructured_enabled: bool | None = None
    structured_records_jsonl_enabled: bool | None = None
    xml_tree_json_enabled: bool | None = None
    vision_enabled: bool | None = None
    heading_level_inference_enabled: bool | None = None
    field_extraction_enabled: bool | None = None
    navigation_summary_enabled: bool | None = None
    raptor_enabled: bool | None = None
    vision_concurrency: int | None = Field(default=None, ge=1, le=16)

    @field_validator("adapter_backend", mode="before")
    @classmethod
    def reject_auto_backend(cls, value: object) -> object:
        if str(value).strip().casefold() == "auto":
            raise ValueError(
                "parser adapter の旧既定値は廃止されました。明示的な解析方式を選択してください。"
            )
        return value


ChunkingStrategyName = ChunkingStrategy


class PreprocessProfileStatusData(BaseModel):
    """前処理(Preprocess)段階の 1 変換プリセットの選択状態と実行基盤。"""

    name: PreprocessProfile
    origin: str
    recommended_for: list[str] = Field(default_factory=list)
    selected: bool
    in_process: bool = False
    requires_service: bool = False
    available: bool = True


class PreprocessSettingsData(BaseModel):
    """ファイル準備設定の非機密 runtime snapshot。"""

    profile: PreprocessProfile
    service_enabled: bool
    service_url: str
    canonical_artifact_prefix: str
    profiles: list[PreprocessProfileStatusData] = Field(default_factory=list)
    config_source: Literal["runtime"]


class PreprocessSettingsUpdate(BaseModel):
    """ファイル準備設定の更新 payload。"""

    profile: PreprocessProfile


class ChunkingStrategyStatusData(BaseModel):
    """chunks 段階の 1 分割戦略の選択状態と適用場面。"""

    name: ChunkingStrategyName
    origin: str
    recommended_for: list[str] = Field(default_factory=list)
    selected: bool


# 言語資源(#2181)。項目の定義と検証は ``rag_parser_core.language``(画面は項目の種類で欄を出す)。
class LanguagePackFieldData(BaseModel):
    section: str
    key: str
    kind: str
    choices: list[str] = Field(default_factory=list)
    # 取込のときに使う書き方か(変えたら文書の再分割が要る)。
    ingest: bool
    # 1 つの語の長さの上限(回答の定型文のような文の項目は長い)。
    max_chars: int
    # 数値マッピングの値の下限。
    min_value: int = 0


class LanguagePackData(BaseModel):
    """言語資源の画面の値(項目の定義・組み込みの値・全体の上書き・重ねた値)。"""

    fields: list[LanguagePackFieldData]
    builtin: dict[str, dict[str, object]]
    overrides: dict[str, dict[str, object]]
    effective: dict[str, dict[str, object]]
    hash: str
    # 今の言語資源で分割していない文書の数(分割し直しが要る。DB に届かなければ None)。
    outdated_documents: int | None = None
    unrecorded_documents: int | None = None
    current_release: LanguagePackRelease | None = None


class LanguagePackUpdate(BaseModel):
    """全体の上書き(組み込みと違う項目だけ。空なら組み込みに戻す)。"""

    overrides: dict[str, dict[str, object]] = Field(default_factory=dict)


class LanguagePackHistoryVersion(BaseModel):
    """言語資源の保存の版(新しい順)。"""

    id: str
    saved_at: str
    saved_by: str | None = None
    hash: str
    # 取込の項目の hash(前の版と違えば、戻すと文書の分割し直しが要る)。
    ingest_hash: str | None = None
    # 前の版に戻したときの、戻した元の版の id。
    restored_from: str | None = None
    # 組み込みと違う項目の数。
    changed_fields: int


class LanguagePackHistoryData(BaseModel):
    versions: list[LanguagePackHistoryVersion]


class LanguagePackPreviewRequest(BaseModel):
    """例の文を、保存した言語資源と編集中の上書きのそれぞれで読む。"""

    text: str = Field(min_length=1, max_length=4000)
    overrides: dict[str, dict[str, object]] = Field(default_factory=dict)


class LanguagePackPreviewReference(BaseModel):
    label: str
    kind: str
    key: str
    document_title: str | None = None


class LanguagePackPreviewResult(BaseModel):
    references: list[LanguagePackPreviewReference]
    # 文書名の照合に使う形(版・年度・括弧書きを外した形を含む)。
    title_keys: list[str]
    # 見出しとして読んだときの鍵(種類:鍵)。
    heading_keys: list[str]


class LanguagePackPreviewData(BaseModel):
    saved: LanguagePackPreviewResult
    draft: LanguagePackPreviewResult


class ChunkingSettingsData(BaseModel):
    """文書分割設定の非機密 runtime snapshot。"""

    strategy: ChunkingStrategyName
    chunk_size: int
    overlap: int
    min_chars: int
    delimiter: str
    context_header_enabled: bool
    # 親子階層（small-to-big。`small_to_big`）の分割パラメータ(rag_poc と同じ 5 項目)。
    chunk_child_target_chars: int
    chunk_table_child_target_chars: int
    chunk_parent_target_chars: int
    chunk_parent_max_pages: int
    chunk_parent_max_children: int
    strategies: list[ChunkingStrategyStatusData] = Field(default_factory=list)
    config_source: Literal["runtime"]


class ChunkingSettingsUpdate(BaseModel):
    """文書分割設定の更新 payload。"""

    strategy: ChunkingStrategyName
    chunk_size: int = Field(
        default=800,
        ge=CHUNK_SIZE_MIN_CHARS,
        le=CHUNK_SIZE_MAX_CHARS,
    )
    overlap: int = Field(default=120, ge=0, le=CHUNK_OVERLAP_MAX_CHARS)
    min_chars: int = Field(default=120, ge=0, le=2000)
    delimiter: str = Field(default=JSONL_RECORD_BOUNDARY, min_length=1, max_length=256)
    chunk_child_target_chars: int = Field(
        default=CHUNK_CHILD_TARGET_CHARS_DEFAULT,
        ge=CHUNK_CHILD_TARGET_CHARS_MIN,
        le=CHUNK_CHILD_TARGET_CHARS_MAX,
    )
    chunk_table_child_target_chars: int = Field(
        default=CHUNK_TABLE_CHILD_TARGET_CHARS_DEFAULT,
        ge=CHUNK_TABLE_CHILD_TARGET_CHARS_MIN,
        le=CHUNK_TABLE_CHILD_TARGET_CHARS_MAX,
    )
    chunk_parent_target_chars: int = Field(
        default=CHUNK_PARENT_TARGET_CHARS_DEFAULT,
        ge=CHUNK_PARENT_TARGET_CHARS_MIN,
        le=CHUNK_PARENT_TARGET_CHARS_MAX,
    )
    chunk_parent_max_pages: int = Field(
        default=CHUNK_PARENT_MAX_PAGES_DEFAULT,
        ge=CHUNK_PARENT_MAX_PAGES_MIN,
        le=CHUNK_PARENT_MAX_PAGES_MAX,
    )
    chunk_parent_max_children: int = Field(
        default=CHUNK_PARENT_MAX_CHILDREN_DEFAULT,
        ge=CHUNK_PARENT_MAX_CHILDREN_MIN,
        le=CHUNK_PARENT_MAX_CHILDREN_MAX,
    )

    @field_validator("delimiter")
    @classmethod
    def normalize_delimiter(cls, value: str) -> str:
        """分割符の前後空白を設定値へ混入させない。"""
        delimiter = value.strip()
        if not delimiter:
            raise ValueError("delimiter を入力してください。")
        return delimiter

    @model_validator(mode="after")
    def validate_chunk_bounds(self) -> "ChunkingSettingsUpdate":
        """chunk size と各パラメータの整合性を保存前に検証する。"""
        if self.strategy == "fixed_delimiter":
            return self
        if self.overlap >= self.chunk_size:
            raise ValueError("overlap は chunk_size より小さくしてください。")
        if (
            self.strategy in _CHUNKING_STRATEGIES_WITH_MIN_CHARS
            and self.min_chars >= self.chunk_size
        ):
            raise ValueError("min_chars は chunk_size より小さくしてください。")
        return self


class AnswerRecordSettingsData(BaseModel):
    """回答の記録の保持設定。retention_days=0 は無期限。"""

    retention_days: int = Field(ge=0, le=3650)
    config_source: Literal["runtime"] = "runtime"


class AnswerPromptView(BaseModel):
    """編集できるプロンプト(回答生成 vlm_answer / 図・画像の読み取り image_retrieval)。"""

    key: str
    content: str
    default_content: str
    customized: bool
    required_placeholders: list[str]
    required_tokens: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None


class AnswerPromptPart(BaseModel):
    id: str
    content: str
    prompt_key: str | None = None


class AnswerPromptStage(BaseModel):
    """取込から評価までの 1 段の指示と、編集可能な指示の key。"""

    id: str
    # 処理の順の区分(answer_prompts.PROMPT_PHASES)。画面はこの順に見出しを付けて並べる。
    phase: str
    prompts: list[AnswerPromptPart]


class AnswerPromptsData(BaseModel):
    prompts: list[AnswerPromptView]
    stages: list[AnswerPromptStage]


class AnswerPromptUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=50_000)


class AnsweringSettingsData(BaseModel):
    """回答の検索と生成の全体既定(回答エンジン。#593)。

    全体設定だけで決め、検索・回答プロファイルでは上書きしない
    (``PROFILE_GLOBAL_QUERY_FIELDS``)。値は backend/.env の `RAG_*`(回答の設定)に保存する。
    """

    query_strategy: QueryStrategy
    answer_flow: AnswerFlow
    answer_audit_mode: Literal["none", "standard", "detailed"]
    answer_evidence_refinement_mode: Literal["legacy", "cited_only", "cited_required"]
    neighbor_child_count: int = Field(ge=0, le=20)
    rerank_enabled: bool
    screen_linking_enabled: bool
    request_coverage_retrieval_enabled: bool
    auto_field_filter_enabled: bool
    # 関係情報(章節・表の見出しの entity と chunk の対応)で検索の候補を足す(#2055。既定 OFF)。
    entity_expansion_enabled: bool
    entity_expansion_max_chunks: int
    section_expansion_enabled: bool
    section_expansion_max_levels: int
    section_expansion_input_token_percent: int
    graph_expansion_enabled: bool
    config_source: Literal["runtime"] = "runtime"


class AnsweringSettingsUpdate(BaseModel):
    """回答の検索と生成の全体既定の更新 payload(送った項目だけを変える)。"""

    query_strategy: QueryStrategy | None = None
    answer_flow: AnswerFlow | None = None
    answer_audit_mode: Literal["none", "standard", "detailed"] | None = None
    answer_evidence_refinement_mode: Literal["legacy", "cited_only", "cited_required"] | None = None
    neighbor_child_count: int | None = Field(default=None, ge=0, le=20)
    rerank_enabled: bool | None = None
    screen_linking_enabled: bool | None = None
    request_coverage_retrieval_enabled: bool | None = None
    auto_field_filter_enabled: bool | None = None
    graph_expansion_enabled: bool | None = None

    entity_expansion_enabled: bool | None = None
    entity_expansion_max_chunks: int | None = Field(default=None, ge=1, le=20)
    section_expansion_enabled: bool | None = None
    section_expansion_max_levels: int | None = Field(default=None, ge=1, le=8)
    section_expansion_input_token_percent: int | None = Field(default=None, ge=10, le=90)


class AnswerRecordSettingsUpdate(BaseModel):
    """回答の記録の保持設定の更新 payload。"""

    retention_days: int = Field(ge=0, le=3650)


class FieldDefinitionData(BaseModel):
    """抽出対象 field の宣言(PoweRAG/LangExtract 由来)。"""

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    value_type: Literal["string", "number", "date", "bool"] = "string"

    @field_validator("name")
    @classmethod
    def _strip_non_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("field name は空にできません。")
        return cleaned


class ExtractionFieldsSettingsData(BaseModel):
    """field 抽出 schema 定義の snapshot。

    `uses_standard` が真なら全体の既定を一度も保存しておらず、`fields` は標準の項目(#556)。
    """

    fields: list[FieldDefinitionData] = Field(default_factory=list)
    uses_standard: bool = False
    config_source: Literal["runtime"] = "runtime"


class ExtractionFieldsSettingsUpdate(BaseModel):
    """field 抽出 schema 定義の更新 payload（文書解析の「解析後の処理」で編集する。#528）。"""

    fields: list[FieldDefinitionData] = Field(default_factory=list, max_length=50)


class KnowledgeBaseExtractionFieldsData(BaseModel):
    """ナレッジベースの項目抽出の定義(#548)。

    `inherits_default` が真なら KB の定義は無く、`fields` は全体の既定(文書解析の設定)。
    """

    inherits_default: bool
    fields: list[FieldDefinitionData] = Field(default_factory=list)


class KnowledgeBaseExtractionFieldsUpdate(BaseModel):
    """ナレッジベースの項目抽出の定義の更新 payload。`fields` が null なら全体の既定に戻す。"""

    fields: list[FieldDefinitionData] | None = Field(default=None, max_length=50)


class SearchExtractionFieldsData(BaseModel):
    """検索の絞り込みに使える項目(検索対象の KB の定義の和集合。#549)。"""

    fields: list[FieldDefinitionData] = Field(default_factory=list)


class PipelineSettingsData(BaseModel):
    """設定の概要: 工程の自動進行と、レシピ 11 項目の全体の既定（#528）。

    `recipe_defaults` は文書のレシピで何も上書きしないときの実効値（レシピの
    「グローバル設定に従う」と同じ解決）。読み取り専用で、各項目はそれぞれの設定画面で変える。
    """

    auto_parse_after_preprocess_enabled: bool
    auto_chunk_after_extract_enabled: bool
    auto_index_after_chunk_enabled: bool
    recipe_defaults: DocumentProcessingConfig
    vector_index_profile: VectorIndexProfile
    config_source: Literal["runtime"] = "runtime"


class PipelineSettingsUpdate(BaseModel):
    """工程の自動進行の更新 payload。省略した項目は変えない。"""

    auto_parse_after_preprocess_enabled: bool | None = None
    auto_chunk_after_extract_enabled: bool | None = None
    auto_index_after_chunk_enabled: bool | None = None


VectorIndexProfileName = VectorIndexProfile
# 実際の索引と推奨ビルドの比較結果(#562)。unknown = 実際の値を確認できない。
VectorIndexBuildStatus = Literal["match", "reprovision", "unknown"]


class VectorIndexProfileStatusData(BaseModel):
    """索引/検索精度の 1 プロファイルの選択状態と推奨値。"""

    name: VectorIndexProfileName
    origin: str
    recommended_for: list[str] = Field(default_factory=list)
    selected: bool
    target_accuracy: int
    neighbors: int
    efconstruction: int
    distance: str
    index_status: VectorIndexBuildStatus


class VectorIndexSettingsData(BaseModel):
    """検索インデックス設定の非機密 runtime snapshot。"""

    profile: VectorIndexProfileName
    target_accuracy: int
    neighbors: int
    efconstruction: int
    distance: str
    requires_reprovision: bool
    index_status: VectorIndexBuildStatus
    # 実際の索引の値。確認できないときは None。
    actual_neighbors: int | None = None
    actual_efconstruction: int | None = None
    profiles: list[VectorIndexProfileStatusData] = Field(default_factory=list)
    reindex_sql: str = ""
    config_source: Literal["runtime"]


class VectorIndexSettingsUpdate(BaseModel):
    """検索インデックス設定の更新 payload。"""

    profile: VectorIndexProfileName


EvaluationSuiteName = EvaluationSuite


class EvaluationSuiteStatusData(BaseModel):
    """評価の 1 スイートの選択状態と閾値。"""

    name: EvaluationSuiteName
    origin: str
    recommended_for: list[str] = Field(default_factory=list)
    selected: bool
    thresholds: dict[str, float] = Field(default_factory=dict)


class EvaluationSettingsData(BaseModel):
    """品質評価設定の非機密 runtime snapshot。"""

    suite: EvaluationSuiteName
    thresholds: dict[str, float] = Field(default_factory=dict)
    suites: list[EvaluationSuiteStatusData] = Field(default_factory=list)
    config_source: Literal["runtime"]


class EvaluationSettingsUpdate(BaseModel):
    """品質評価設定の更新 payload。"""

    suite: EvaluationSuiteName


GraphProfileName = GraphProfile


class GraphProfileStatusData(BaseModel):
    """文書構造索引の構築の 1 プロファイル(off = 構築しない / entities = 構築する)の選択状態。"""

    name: GraphProfileName
    selected: bool


class EntityIndexSettingsData(BaseModel):
    """実体・別名索引を構築する全体の既定。"""

    enabled: bool


class EntityIndexSettingsUpdate(BaseModel):
    """文書レシピで上書きできる索引構築の既定。"""

    enabled: bool


class GraphSettingsData(BaseModel):
    """関係情報設定の非機密 runtime snapshot。"""

    profile: GraphProfileName
    enabled: bool
    profiles: list[GraphProfileStatusData] = Field(default_factory=list)
    config_source: Literal["runtime"]


class GraphSettingsUpdate(BaseModel):
    """関係情報設定の更新 payload。"""

    profile: GraphProfileName


class GuardrailSettingsData(BaseModel):
    """OCI Guardrails の有効状態と設定の準備状況。"""

    input_enabled: bool
    output_enabled: bool
    oci_configured: bool
    oci_warning_code: str | None = None
    config_source: str = "runtime"


class GuardrailSettingsUpdate(BaseModel):
    """OCI Guardrails の有効・無効だけを更新する。"""

    model_config = ConfigDict(extra="forbid")
    input_enabled: bool
    output_enabled: bool
