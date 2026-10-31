"""ドキュメント関連スキーマ。"""

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)
from rag_parser_core.extraction import strip_formatting_marks
from rag_parser_core.source import SourceModality, SourcePreviewKind, SourceProfile

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
    ChunkingStrategy,
)
from app.rag.kb_adapter_config import (
    KnowledgeBaseIngestionConfig,
    reject_roadmap_parser_backend_input,
)
from app.schemas.classification import (
    CLASSIFICATION_CATEGORY_KEYS,
    normalize_category_value,
)
from app.schemas.common import JsonValue
from app.schemas.knowledge_base import KnowledgeBaseRef
from app.schemas.language_pack_release import LanguagePackDocumentUse

__all__ = [
    "SourceModality",
    "SourcePreviewKind",
    "SourceProfile",
]


class FileStatus(StrEnum):
    """ファイル処理状態。

    RAG はアップロード後に段階ごとに取込む。PREPROCESS は PREPROCESSED、EXTRACT は
    REVIEW、CHUNK は CHUNKED で停止でき、INDEX だけが検索対象の INDEXED へ進める。
    PREPROCESSED / REVIEW / CHUNKED / 実行中状態は検索対象に含めず、INDEXED のみを
    検索可能とする。
    """

    UPLOADED = "UPLOADED"
    PREPROCESSING = "PREPROCESSING"
    PREPROCESSED = "PREPROCESSED"
    INGESTING = "INGESTING"
    REVIEW = "REVIEW"
    CHUNKING = "CHUNKING"
    CHUNKED = "CHUNKED"
    INDEXING = "INDEXING"
    INDEXED = "INDEXED"
    ERROR = "ERROR"


class BatchUploadFailedItem(BaseModel):
    """batch upload で個別に失敗したファイル。"""

    file_name: str
    status_code: int
    message: str
    source_profile: SourceProfile | None = None


class IngestionJobStatus(StrEnum):
    """取込 job 状態。"""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


class IngestionJobPhase(StrEnum):
    """取込 job の処理フェーズ。

    EXTRACT は parse/抽出を行い REVIEW で停止する前段、
    CHUNK は保存済み抽出から chunk を作成して CHUNKED で停止する中段、
    INDEX は承認済み chunk から embedding→索引を行う後段。
    """

    PREPROCESS = "PREPROCESS"
    EXTRACT = "EXTRACT"
    CHUNK = "CHUNK"
    INDEX = "INDEX"


# 解析サービスの CPU を使う重い段（ファイル準備・抽出）。取込を始める最初の段でもある。worker は
# 重い段の同時実行数を全体の枠より小さく絞り、軽い段（Chunk 作成・索引）が待ち続けないようにする
# (#2027)。重い段の枠は job の中の解析の区間だけで持ち、その後の項目抽出などの LLM の応答待ちでは
# 返す(#2702。``app.rag.ingestion_slots``)。
INGESTION_HEAVY_PHASES: frozenset[IngestionJobPhase] = frozenset(
    {IngestionJobPhase.PREPROCESS, IngestionJobPhase.EXTRACT}
)
# 取込の後の段（前の段が終わった文書の続き）。
INGESTION_CONTINUATION_PHASES: frozenset[IngestionJobPhase] = frozenset(
    {IngestionJobPhase.CHUNK, IngestionJobPhase.INDEX}
)


IngestionParseSegmentStatus = Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]


class IngestionParseSegmentProgress(BaseModel):
    """PDF の段（ページの範囲）1 つの解析の状態（#2001）。

    worker は段の状態を抽出セグメント（``rag_ingestion_segments``）と同じ時に書き換え、同じ値を
    job の進捗にも載せる。画面は段のカード・「抽出: N / M ページ」・解析の bar をこの 1 つの記録から
    作り、食い違わないようにする。``completed`` は解析中の段の中で終わったページの数。
    """

    segment_id: str
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    status: IngestionParseSegmentStatus = "QUEUED"
    completed: int = Field(default=0, ge=0)
    # 前の試行で成功した段の抽出結果を使い、解析し直さなかった。
    reused: bool = False


class IngestionParseProgress(BaseModel):
    """抽出の中の解析の段（Docling などの解析サービス）のページの進捗（#1999・#2001）。

    PDF を段（既定 10 ページ）ごとに解析するときは ``segments`` に段ごとの状態を持ち、
    ``completed`` / ``total`` は解析中の段の中のページの数（段が終わるたびに次の段に移る）。
    すべての段が終わったら ``finished`` で、``completed`` / ``total`` は原本全体のページ数。
    段に分けずに原本全体を 1 回で解析するとき（PDF 以外・段が 1 つ）は ``segments`` が空で、
    ``completed`` / ``total`` は原本全体のページの数。ページの完了は解析サービスの進捗
    （Docling の ``progress.json``）を取込の worker が読んで写す。``total`` は解析サービスが
    ページ数を返すまで None。
    """

    engine: str | None = None
    unit: Literal["page"] = "page"
    completed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    total: int | None = Field(default=None, ge=0)
    finished: bool = False
    segments: list[IngestionParseSegmentProgress] = Field(default_factory=list)


class IngestionVisionProgress(BaseModel):
    """抽出の中の図・表の説明（Vision）の段の件数の進捗（#1999）。

    値は Vision の段の記録（``vision/progress.json``）の ``counts`` / ``total_known`` /
    ``total_final`` から作る。表の対象は図の後に見つかるため、``total_final`` が偽の間は
    ``total_known`` が増えうる。
    """

    phase: str | None = None
    succeeded: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    running: int = Field(default=0, ge=0)
    pending: int = Field(default=0, ge=0)
    total_known: int = Field(default=0, ge=0)
    total_final: bool = False
    current_page: int | None = Field(default=None, ge=1)
    current_kind: Literal["picture", "table"] | None = None
    finished: bool = False
    # 段ごとの解析と並べて説明するとき（#2002）: 今説明している段のページの範囲、説明の終わった
    # 段の数と段の数（並べないときは 0）。件数は説明の始まった段の合計。
    segment_page_start: int | None = Field(default=None, ge=1)
    segment_page_end: int | None = Field(default=None, ge=1)
    segments_completed: int = Field(default=0, ge=0)
    segments_total: int = Field(default=0, ge=0)


class IngestionCallProgress(BaseModel):
    """取込の段の AI の呼び出しの状態（#2002・#2067）。

    段は Vision・項目抽出・ナビゲーション要約・RAPTOR 要約・Embedding・リランク。

    上限つきの並行の実行器（``pr_backend_core.bounded_calls``）の状態を写す。``waiting_reason`` は
    待っている理由（``rate_limited`` = API の流量の制限、``breaker_open`` = 失敗が続いたための
    一時停止、
    ``final_retry`` = 失敗した件の最後の再試行の前の待ち、ほかはエラーの種類ごとの再試行の待ち）、
    ``waiting_seconds`` は残りの秒数。``concurrency`` は今の並行の数（429 で減らし、成功が続くと
    ``max_concurrency`` まで戻す）。
    """

    stage: Literal[
        "vision",
        "heading_levels",
        "field_extraction",
        "navigation_summary",
        "raptor_summary",
        "embedding",
        "rerank",
    ]
    succeeded: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    retries: int = Field(default=0, ge=0)
    total: int | None = Field(default=None, ge=0)
    concurrency: int = Field(default=1, ge=1)
    max_concurrency: int = Field(default=1, ge=1)
    in_flight: int = Field(default=0, ge=0)
    waiting_reason: str | None = None
    waiting_seconds: int | None = Field(default=None, ge=0)
    finished: bool = False


class IngestionJobProgress(BaseModel):
    """実行中の取込 job の、抽出の中の段の進捗（#1999）。

    ``stage`` は今の段（``parse`` = 解析サービスのページの解析、``vision`` = 図・表の説明）。
    ``updated_at`` は worker が最後に進捗を書いた時刻（画面が「何分進んでいないか」を出す）。
    """

    stage: Literal["parse", "vision"]
    parse: IngestionParseProgress | None = None
    vision: IngestionVisionProgress | None = None
    # 今の（最後の）段の AI の呼び出しの状態（待ちの理由・並行の数。#2002）。
    calls: IngestionCallProgress | None = None
    updated_at: datetime


class IngestionJob(BaseModel):
    """キュー投入された取込 job。"""

    id: str
    document_id: str
    # 一覧で「どのファイルか」を示すための文書のファイル名（rag_documents.file_name）。
    # 一覧・取得の SELECT が文書表と JOIN して埋める。作成直後の応答などでは None（#306）。
    document_file_name: str | None = None
    recipe_id: str | None = None
    recipe_revision: int | None = Field(default=None, ge=1)
    language_pack_release_id: str | None = None
    status: IngestionJobStatus
    phase: IngestionJobPhase = IngestionJobPhase.PREPROCESS
    parser_profile: str
    quality_warnings: list[str] = Field(default_factory=list)
    # レシピ実験(Phase 3b)ジョブが持つ候補レシピ上書き(rag_* キー)。通常取込では None。
    settings_overrides: dict[str, object] | None = None
    skip_reason: str | None = None
    error_message: str | None = None
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # 実行中の抽出の段の進捗（#1999）。一覧・取得の応答だけが持つ。読めない値は None。
    progress: IngestionJobProgress | None = None


class IngestionJobLease(BaseModel):
    """取込 job の状態と lease の持ち主(worker が自分の実行かどうかを確かめる。#359)。

    API の応答には出さない(``lease_owner`` は worker の host・pid を含むため)。
    """

    status: IngestionJobStatus
    lease_owner: str | None = None


class DocumentClassification(BaseModel):
    """文書の分類と有効期間(rag_poc の document.classification / effective_from / effective_to)。

    文書のメタデータで、レシピを切り替えても変わらない。ACL に使う category_name とは別に持つ。
    有効期間の終了日は排他的(effective_to の当日は期間外)。
    分類の値は `normalize_category_value` で表記をそろえて保存する(#547)。
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    large_category: str | None = Field(default=None, max_length=200)
    middle_category: str | None = Field(default=None, max_length=200)
    small_category: str | None = Field(default=None, max_length=200)
    effective_from: date | None = None
    effective_to: date | None = None

    @field_validator(*CLASSIFICATION_CATEGORY_KEYS, mode="before")
    @classmethod
    def _normalize_category(cls, value: object) -> object:
        return normalize_category_value(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _normalize(self) -> "DocumentClassification":
        if self.effective_from and self.effective_to and self.effective_from >= self.effective_to:
            raise ValueError("有効期間の終了日は開始日より後の日付にしてください。")
        return self

    def is_empty(self) -> bool:
        return not any(self.model_dump().values())


class IngestionQueueWait(BaseModel):
    """取込の job が worker の空きを待っている(順番待ち)ことの表示用の情報(#2026)。

    「人の確認が要る(確認待ち)」と「システムを待っている(順番待ち)」を画面で分けるために、
    待っている工程・前にある job の数・同時に処理できる数を返す。一覧と詳細で同じ値を返す。
    """

    phase: IngestionJobPhase
    # 待っている job。抽出の完了から自動進行の job の投入までの猶予の間(#733)は None。
    job_id: str | None = None
    queued_at: datetime | None = None
    # worker が claim する順(``ingestion_dispatch_order_sql``。#2027)で前にある、同じ枠の段の
    # claim できる QUEUED の job の数。猶予の間は None。
    jobs_ahead: int | None = Field(default=None, ge=0)
    # 待っている段の枠(重い段は `RAG_INGESTION_QUEUE_HEAVY_PHASE_CONCURRENCY`、軽い段は
    # `RAG_INGESTION_QUEUE_WORKER_CONCURRENCY`。auto は vCPU から決めた値)。
    concurrency: int = Field(ge=1)


class DocumentSummary(BaseModel):
    """一覧表示用のドキュメント要約。"""

    id: str
    file_name: str
    language_pack: LanguagePackDocumentUse | None = None
    status: FileStatus
    category_name: str | None = None
    content_type: str | None = None
    file_size_bytes: int | None = None
    content_sha256: str | None = None
    duplicate_of_document_id: str | None = None
    uploaded_at: datetime
    indexed_at: datetime | None = None
    knowledge_bases: list[KnowledgeBaseRef] = Field(default_factory=list)
    source_profile: SourceProfile | None = None
    # 検索対象(active)のレシピの派生情報レイヤーに、作り直しが必要なものがあるか(#550)。
    # 一覧の API だけが埋める(詳細はレイヤーごとの ``rebuild_required`` を見る)。
    layers_rebuild_required: bool = False
    # 待機中・実行中の取込 job があるか(#2012)。一覧の API だけが埋め、一覧の定期の取り直しに使う
    # (投入直後の job は worker が拾うまで状態が変わらない)。
    ingestion_active: bool = False
    # 一覧に出す状態(レシピ1)の取込の job が worker の空きを待っているとき(#2026)。一覧の API
    # だけが埋める(詳細は工程の ``queue_wait``)。
    queue_wait: IngestionQueueWait | None = None
    # 文書の版(#1248)。この文書を置き換えた新しい版の文書。NULL は今有効な版。
    # 置き換え済み(旧版)の文書の chunk は、既定では回答の検索の対象から外す。
    superseded_by_document_id: str | None = None
    # 新しい版の文書名(利用者が見られる文書のときだけ埋める)。
    superseded_by_file_name: str | None = None
    superseded_at: datetime | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_superseded(self) -> bool:
        """新しい版に置き換えた文書(旧版)か。"""
        return self.superseded_by_document_id is not None


class DuplicateDocumentRef(BaseModel):
    """重複判定で参照している既存ドキュメントの表示用摘要。"""

    id: str
    file_name: str
    status: FileStatus
    uploaded_at: datetime
    indexed_at: datetime | None = None


class DocumentPreprocessArtifact(BaseModel):
    """ファイル準備で生成・選択された抽出入力ファイル。"""

    derivation_id: str
    profile: str
    converted: bool = False
    converter_name: str | None = None
    converter_version: str | None = None
    source_content_type: str | None = None
    source_sha256: str | None = None
    object_storage_path: str | None = None
    content_type: str | None = None
    sha256: str | None = None
    file_name: str
    page_map: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class DocumentPreviewPage(BaseModel):
    """プレビューのページ画像 1 ページの表示寸法(pt。ページの /Rotate を反映した向き)。"""

    page_number: int
    width: float
    height: float


class DocumentPreviewPages(BaseModel):
    """プレビューのページ画像の一覧(PDF のページごとに画像を描き、bbox の強調を重ねる。#349)。"""

    page_count: int
    pages: list[DocumentPreviewPage] = Field(default_factory=list)


class DocumentSupersededByRequest(BaseModel):
    """文書を置き換えた新しい版の設定・解除(#1248)。null で解除(今有効な版に戻す)。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    superseded_by_document_id: str | None = Field(default=None, max_length=64)

    @field_validator("superseded_by_document_id", mode="after")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        return value or None


class DocumentDetail(DocumentSummary):
    """詳細表示用。VLM/LLM の抽出本文とメタデータを含む。"""

    object_storage_path: str | None = None
    preprocess_artifact: DocumentPreprocessArtifact | None = None
    extraction: dict[str, object] = Field(default_factory=dict)
    error_message: str | None = None
    duplicate_source: DuplicateDocumentRef | None = None
    classification: DocumentClassification | None = None


class ParserSourceNotice(BaseModel):
    """選んだ文書解析エンジン(既定は Docling)で扱えない形式の案内(取込を始める前に止める。#286)。"""

    code: str = Field(description="parser_source_unsupported")
    backend: str = Field(description="取込で使う文書解析エンジン(既定レシピ)。")
    file_format: str = Field(description="原本の拡張子(無ければ content type)。")
    suggested_backend: str | None = Field(
        default=None, description="この形式を扱える文書解析エンジン(例: unstructured)。"
    )
    message: str = Field(description="利用者向けの理由と対処。")


class UploadResult(BaseModel):
    """アップロード結果。"""

    id: str
    file_name: str
    status: FileStatus
    file_size_bytes: int
    content_sha256: str
    duplicate_of_document_id: str | None = None
    knowledge_bases: list[KnowledgeBaseRef] = Field(default_factory=list)
    source_profile: SourceProfile
    ingestion_started: bool = False
    parser_notice: ParserSourceNotice | None = Field(
        default=None,
        description="既定の文書解析エンジンで扱えない形式のとき、取込を始める前の案内(#286)。",
    )


class BatchUploadResult(BaseModel):
    """複数ファイル upload の結果。"""

    items: list[UploadResult] = Field(default_factory=list)
    failed_items: list[BatchUploadFailedItem] = Field(default_factory=list)
    total_count: int = 0
    uploaded_count: int = 0
    failed_count: int = 0


class DocumentChunkView(BaseModel):
    """UI で chunk/citation を可視化するための非 embedding chunk view。"""

    document_id: str
    chunk_id: str
    chunk_index: int = 0
    text: str
    page_start: int | None = None
    page_end: int | None = None
    bbox: list[float] | None = None
    section_path: str | None = None
    content_kind: str | None = None
    chunk_group_id: str | None = None
    source_parser: str | None = None
    element_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class DocumentLayerStatusName(StrEnum):
    """文書 chunk_set の派生情報レイヤー状態。"""

    NOT_REQUESTED = "not_requested"
    PLANNED_ONLY = "planned_only"
    MATERIALIZED = "materialized"
    NEEDS_REINGEST = "needs_reingest"
    ERROR = "error"


class DocumentMaterializationLayerStatus(BaseModel):
    """chunk_set に紐づく派生情報レイヤーの現在状態。"""

    layer_id: str | None = None
    requested: bool = False
    status: DocumentLayerStatusName = DocumentLayerStatusName.NOT_REQUESTED
    reason: str | None = None
    # 作ったときの入力(項目の定義など)が今の設定と違い、作り直しが必要か(#550)。status とは
    # 別の印で、今の設定との比較で決まる(保存しない)。指紋の無い古い行は False(不明)。
    rebuild_required: bool = False
    # 変わった入力の名前(``app.rag.layer_fingerprint`` の *_INPUT)。画面が表示名にする。
    rebuild_inputs: list[str] = Field(default_factory=list)


class DocumentChunkSetLayerStatuses(BaseModel):
    """chunk_set から派生する情報レイヤーの状態一覧。"""

    metadata: DocumentMaterializationLayerStatus = Field(
        default_factory=DocumentMaterializationLayerStatus
    )
    graph: DocumentMaterializationLayerStatus = Field(
        default_factory=DocumentMaterializationLayerStatus
    )
    navigation: DocumentMaterializationLayerStatus = Field(
        default_factory=DocumentMaterializationLayerStatus
    )


class DocumentChunkSet(BaseModel):
    """文書の chunk_set(variant = 1 レシピのチャンク集合)1 件分の状態・件数・所属/配信 KB。"""

    chunk_set_id: str
    extraction_recipe_id: str | None = None
    extraction_status: DocumentLayerStatusName = DocumentLayerStatusName.NOT_REQUESTED
    extraction_reason: str | None = None
    status: str
    chunk_count: int = 0
    vector_count: int = 0
    is_serving: bool = True
    created_at: datetime | None = None
    extraction_id: str | None = None
    parser: str | None = None
    preprocess: str | None = None
    knowledge_base_ids: list[str] = Field(default_factory=list)
    serving_knowledge_base_ids: list[str] = Field(default_factory=list)
    layer_statuses: DocumentChunkSetLayerStatuses = Field(
        default_factory=DocumentChunkSetLayerStatuses
    )


class DocumentChunkPreviewRequest(BaseModel):
    """保存しない分割プレビュー用の一時 chunking 上書き。"""

    chunking_strategy: ChunkingStrategy | None = None
    chunk_size: int | None = Field(
        default=None,
        ge=CHUNK_SIZE_MIN_CHARS,
        le=CHUNK_SIZE_MAX_CHARS,
    )
    chunk_overlap: int | None = Field(default=None, ge=0, le=CHUNK_OVERLAP_MAX_CHARS)
    chunk_min_chars: int | None = Field(default=None, ge=0, le=2000)
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

    _FIELD_TO_SETTING = {
        "chunking_strategy": "rag_chunking_strategy",
        "chunk_size": "rag_chunk_size",
        "chunk_overlap": "rag_chunk_overlap",
        "chunk_min_chars": "rag_chunk_min_chars",
        "chunk_delimiter": "rag_chunk_delimiter",
        "chunk_child_target_chars": "rag_chunk_child_target_chars",
        "chunk_table_child_target_chars": "rag_chunk_table_child_target_chars",
        "chunk_parent_target_chars": "rag_chunk_parent_target_chars",
        "chunk_parent_max_pages": "rag_chunk_parent_max_pages",
        "chunk_parent_max_children": "rag_chunk_parent_max_children",
    }

    def settings_overrides(self) -> dict[str, object]:
        return {
            setting: getattr(self, field)
            for field, setting in self._FIELD_TO_SETTING.items()
            if getattr(self, field) is not None
        }


class DocumentChunkPreviewStats(BaseModel):
    """分割結果の軽量な文字数統計。"""

    chunk_count: int = Field(ge=0)
    min_chars: int = Field(ge=0)
    average_chars: float = Field(ge=0)
    max_chars: int = Field(ge=0)
    overflow_count: int = Field(ge=0)
    embedding_overflow_count: int = Field(ge=0)


class DocumentChunkPreviewResponse(BaseModel):
    """DB を変更しないレシピ別 chunk preview。"""

    chunks: list[DocumentChunkView] = Field(default_factory=list)
    stats: DocumentChunkPreviewStats
    warnings: list[str] = Field(default_factory=list)


# 章節の抽出規則の方式(#715。app/rag/section_rules.py)。
SectionRulesMode = Literal["parser", "legal", "official", "numbered", "custom"]


class DocumentProcessingConfig(KnowledgeBaseIngestionConfig):
    """文書単位の処理レシピ上書き。None は global 既定を継承する。"""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def discard_global_only_settings(cls, value: object) -> object:
        if isinstance(value, dict):
            return {
                key: item for key, item in value.items() if key != "chunk_context_header_enabled"
            }
        return value

    # 章節の抽出規則の方式。章節の表示と、取込時に要約する章節に適用する。
    section_rules_mode: SectionRulesMode | None = None


class DocumentProcessingConfigInput(DocumentProcessingConfig):
    """処理レシピの保存の入力(``PUT /documents/{id}/recipes/{recipe_id}``)。

    保存済みの値は読み込み時に準備中(ロードマップ)の解析エンジンを継承へ寄せるが、新しく選ぶ保存は
    理由の分かる検証のエラー(422)にする(#3374)。親の検証(削除済みの項目の読み替え)はその後に通す。
    """

    @model_validator(mode="before")
    @classmethod
    def reject_roadmap_parser_backend(cls, data: object) -> object:
        return reject_roadmap_parser_backend_input(data)


class DocumentRecipeStepStatus(StrEnum):
    """文書レシピの各工程の表示状態。"""

    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class DocumentRecipeStep(BaseModel):
    """文書レシピ 1 工程の状態。"""

    phase: IngestionJobPhase
    status: DocumentRecipeStepStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error_message: str | None = None
    # status が QUEUED の工程の順番待ちの情報(#2026)。
    queue_wait: IngestionQueueWait | None = None


class DocumentRecipeView(BaseModel):
    """文書に属する 1〜3 件の処理レシピ。"""

    recipe_id: str
    document_id: str
    slot_no: int = Field(ge=1, le=3)
    status: FileStatus
    failed_phase: IngestionJobPhase | None = None
    processing_config: DocumentProcessingConfig = Field(default_factory=DocumentProcessingConfig)
    effective_processing_config: DocumentProcessingConfig = Field(
        default_factory=DocumentProcessingConfig
    )
    preprocess_artifact: DocumentPreprocessArtifact | None = None
    active_extraction_recipe_id: str | None = None
    active_chunk_set_id: str | None = None
    # Embedding / 索引の前の保存済み chunk_set(最新の CHUNKED。INDEX の再実行が使う。#2467)。
    pending_chunk_set_id: str | None = None
    chunk_count: int = Field(default=0, ge=0)
    vector_count: int = Field(default=0, ge=0)
    config_revision: int = Field(default=1, ge=1)
    materialized_revision: int | None = Field(default=None, ge=1)
    searchable: bool = False
    needs_reprocessing: bool = False
    error_message: str | None = None
    steps: list[DocumentRecipeStep] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class DocumentRecipeCreateRequest(BaseModel):
    """文書レシピ追加。指定があれば既存レシピの明示設定を複製する。"""

    copy_from_recipe_id: str | None = Field(default=None, max_length=64)


class DocumentRecipeDeleteResult(BaseModel):
    """文書レシピ削除結果。"""

    recipe_id: str
    document_id: str
    removed_chunk_set_count: int = Field(default=0, ge=0)


class DocumentExtractionExportFormat(StrEnum):
    """構造化抽出の監査用 export 形式。"""

    JSON = "json"
    MARKDOWN = "markdown"
    HTML = "html"
    CHUNKS = "chunks"


class DocumentExtractionExport(BaseModel):
    """Docling 風に extraction を非 embedding 形式で確認する view。"""

    document_id: str
    file_name: str
    format: DocumentExtractionExportFormat
    content_type: str
    content: str = ""
    payload: dict[str, object] = Field(default_factory=dict)
    chunks: list[DocumentChunkView] = Field(default_factory=list)
    parser_backend: str | None = None
    parser_profile: str | None = None
    page_count: int = 0
    element_count: int = 0
    table_count: int = 0
    asset_count: int = 0


class IngestionSegment(BaseModel):
    """文書取込 segment の checkpoint/status view。"""

    segment_id: str
    document_id: str
    recipe_id: str | None = None
    status: str
    parser_backend: str = "enterprise_ai"
    parser_profile: str = "enterprise_ai_generic"
    page_start: int | None = None
    page_end: int | None = None
    progress_unit: str = "source"
    progress_start: int | None = None
    progress_end: int | None = None
    attempt_count: int = Field(default=0, ge=0)
    artifact_path: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class DocumentElementTextEdit(BaseModel):
    """REVIEW 中の人手修正: 要素 1 件のテキスト差し替え。"""

    element_id: str = Field(..., max_length=128)
    text: str = Field(default="", max_length=200000)
    # 読み込んだときの文。保存のときに今の文と違えば、ほかの保存が先に変えていたため 409(#2616)。
    base_text: str = Field(..., max_length=200000)


class DocumentTableCellTextEdit(BaseModel):
    """REVIEW 中の人手修正: 表セル 1 件のテキスト差し替え(table_id + row + col で同定)。"""

    table_id: str = Field(..., max_length=128)
    row: int = Field(..., ge=0)
    col: int = Field(..., ge=0)
    text: str = Field(default="", max_length=200000)
    # 読み込んだときの文。保存のときに今の文と違えば、ほかの保存が先に変えていたため 409(#2616)。
    base_text: str = Field(..., max_length=200000)


class DocumentReviewEditsRequest(BaseModel):
    """REVIEW 中に保存する構造化要素・表セルのテキスト修正。"""

    element_edits: list[DocumentElementTextEdit] = Field(default_factory=list, max_length=5000)
    table_cell_edits: list[DocumentTableCellTextEdit] = Field(
        default_factory=list, max_length=20000
    )


class DocumentApproveRequest(DocumentReviewEditsRequest):
    """承認リクエスト。raw_text は旧クライアントとの後方互換用。"""

    raw_text: str | None = Field(default=None, max_length=2000000)


class DocumentDeleteResult(BaseModel):
    """ドキュメント削除結果。"""

    id: str
    file_name: str
    object_storage_path: str | None = None
    object_deleted: bool = False
    artifact_deleted_count: int = 0
    artifact_delete_failed_count: int = 0


class DocumentDeleteImpact(BaseModel):
    """削除の前に確認する影響（#303）。

    重複文書は chunk を持たず、正本の chunk を所属 KB の検索対象として使う。正本を消すと
    その KB の検索対象から内容が消えるため、自前の索引を持たない重複文書の件数と所属 KB を返す。
    """

    document_id: str
    duplicate_count: int = 0
    knowledge_bases: list[KnowledgeBaseRef] = Field(default_factory=list)


# 一括の投入(#2674)で 1 回の要求に入れられる文書の数。画面の「条件に一致する文書を
# すべて選ぶ」の上限と同じ値にする(frontend の BULK_INGESTION_MAX_DOCUMENTS)。
BULK_INGESTION_MAX_DOCUMENTS = 10_000


class BulkIngestionRequest(BaseModel):
    """文書の一括の投入(ファイル準備から)の要求(#2674)。

    ID の形は 1 件ずつ確かめ、不正な ID は要求全体を拒まずに「受付不可」として返す。
    同じ ID の重複は 1 件として扱う。
    """

    model_config = ConfigDict(extra="forbid")

    document_ids: list[str] = Field(min_length=1, max_length=BULK_INGESTION_MAX_DOCUMENTS)


class BulkIngestionOutcome(StrEnum):
    """一括の投入の 1 文書の結果。"""

    # 取込 job(QUEUED)を永続化した。画面を閉じても worker が順に処理する。
    ACCEPTED = "accepted"
    # 待機中・実行中の job がある(二重に登録しない)。
    ALREADY_ACTIVE = "already_active"
    # 処理が要らない(索引済み・確認待ち・重複・未対応の形式)。job は作らない。
    SKIPPED = "skipped"
    # 受け付けられない(見つからない・権限が無い・ID が不正・解析エンジンで扱えない・
    # 競合・一時的な障害)。
    REJECTED = "rejected"


class BulkIngestionItem(BaseModel):
    """一括の投入の 1 文書の結果。``reason`` は機械が読む理由、``message`` は利用者向けの文。"""

    document_id: str
    outcome: BulkIngestionOutcome
    reason: str | None = None
    message: str | None = None
    job_id: str | None = None


class BulkIngestionReasonCount(BaseModel):
    """結果と理由ごとの件数(画面の内訳)。``message`` はその理由の最初の文書の文。"""

    outcome: BulkIngestionOutcome
    reason: str
    count: int
    message: str | None = None


class BulkIngestionResult(BaseModel):
    """一括の投入の結果。``accepted`` の件数の job を、応答の前に永続化している。"""

    requested_count: int
    unique_count: int
    accepted_count: int = 0
    already_active_count: int = 0
    skipped_count: int = 0
    rejected_count: int = 0
    reasons: list[BulkIngestionReasonCount] = Field(default_factory=list)
    items: list[BulkIngestionItem] = Field(default_factory=list)


class DocumentIdList(BaseModel):
    """条件に一致する文書の ID(一括の操作の対象。新しい順)。``total`` は条件に一致する件数。"""

    ids: list[str] = Field(default_factory=list)
    total: int = 0
    limit: int


SectionOrigin = Literal["extraction", "manual"]
# 1 文書の章節の上限(画面で扱える量。抽出の章節は通常これより少ない)。
MAX_DOCUMENT_SECTIONS = 500


class DocumentSection(BaseModel):
    """文書の章節 1 件(#713)。並び順 + 階層(1〜6)で木を表す(親は並びから決まる)。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    title: str = Field(max_length=200)
    level: int = Field(ge=1, le=6)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    origin: SectionOrigin = "manual"
    # 抽出の章節から来たものは、その章節の id。
    source_section_id: str | None = Field(default=None, max_length=64)
    # 抽出の章節の名前・ページを人が変えたか。変えていなければ抽出のやり直しに合わせる。
    edited: bool = False
    # 人が修正した後の抽出のやり直しで、新しく足した章節(保存すると外れる。#721)。
    added_from_extraction: bool = False

    @field_validator("title")
    @classmethod
    def _require_title(cls, value: str) -> str:
        # 抽出の章節の名前と同じく書式の飾りの結合の線を除く（線を除く前の保存値も。#3434）。
        cleaned = strip_formatting_marks(value).strip()
        if not cleaned:
            raise ValueError("章節の名前を入力してください。")
        return cleaned


class DocumentSectionsData(BaseModel):
    """文書の章節。人の修正があればそれを、無ければ抽出結果の章節を返す(#713)。"""

    document_id: str
    source: SectionOrigin
    # 抽出結果の章節を作った方式(#715)。
    rules_mode: SectionRulesMode = "parser"
    summary_rules_match: bool | None = None
    sections: list[DocumentSection] = Field(default_factory=list)
    extraction_section_count: int = 0
    page_count: int | None = None
    # 人の修正の版(未保存は None)。保存のときに base_revision として返す。
    revision: int | None = None
    updated_at: datetime | None = None


class DocumentSectionsSaveRequest(BaseModel):
    """人が修正した章節の保存(#713)。"""

    model_config = ConfigDict(extra="forbid")

    sections: list[DocumentSection] = Field(max_length=MAX_DOCUMENT_SECTIONS)
    base_revision: int | None = None
    # ページの上限の確認に使う抽出結果のレシピ(省略は既定のレシピ)。
    recipe_id: str | None = Field(default=None, max_length=128)
