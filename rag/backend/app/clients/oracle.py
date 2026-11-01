"""Oracle AI Database クライアント。

AI Vector Search によるベクトル検索（`EMBEDDING_VECTOR_COLUMN_TYPE`。#46）と
Oracle Text による keyword retrieval を担う。外部ベクトル DB は使わない。
"""

import asyncio
import hashlib
import importlib
import inspect
import json
import logging
import math
import re
import threading
from array import array
from collections.abc import Awaitable, Callable, Collection, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol, TypeVar, cast
from uuid import uuid4

from pr_backend_core.api import offset_fetch_binds, offset_fetch_clause
from pr_backend_core.oracle_dsn import apply_cli_connect_mode, dsn_without_tns_retry
from pr_backend_core.oracle_errors import ORACLE_ERROR_CODE_RE, oracle_error_codes
from pr_backend_core.oracle_pool import (
    DEFAULT_WAIT_TIMEOUT_MS,
    PoolCloser,
    close_in_background,
    recover_pool_error,
)
from pr_backend_core.oracle_session import init_oracle_session
from pr_system_settings.embedding import EMBEDDING_VECTOR_COLUMN_TYPE
from pydantic import ValidationError
from rag_engine.resources.tracing import observe_call
from rag_engine.retrieval.search_stages import TEXT_SEARCH_STAGE, VECTOR_SEARCH_STAGE

from app.clients.language_pack_release_store import LanguagePackReleaseStore
from app.clients.oracle_read_stats import (
    current_db_call_stats,
    lobs_as_values_enabled,
    read_rows_per_round_trip,
    rows_per_round_trip,
)
from app.config import Settings, get_settings, settings_today
from app.rag.chunking import Chunk
from app.rag.chunking_small_to_big import FIRST_PAGE_CONTEXT_KEY, engine_search_text
from app.rag.extraction_field_adapter import (
    FieldDefinition,
    FieldSchemaStore,
    field_schema_from_json,
)
from app.rag.graph_index import (
    GraphEntity,
    GraphEntityChunkLink,
    GraphIndex,
    GraphRelationship,
)
from app.rag.ingestion_language import ingestion_release_job_id
from app.rag.kb_adapter_config import (
    KnowledgeBaseAdapterConfig,
    KnowledgeBaseQueryConfig,
    parse_adapter_config,
)
from app.rag.language_pack_releases import active_release
from app.rag.request_context import (
    access_scope_context,
    current_audit_request_context,
    unrestricted_access_scope,
)
from app.rag.search_answer_profile_config import (
    SearchAnswerProfileConfig,
    dump_search_answer_profile_config,
    parse_search_answer_profile_config,
)
from app.rag.source_profile import build_source_profile
from app.rag.support_guide_runtime import GUIDE_PREVIEW_TRACE_PREFIX
from app.rag.vector_index_adapter import default_vector_index_build, resolve_vector_index_adapter
from app.schemas.classification import CLASSIFICATION_CATEGORY_KEYS, category_label
from app.schemas.common import JsonValue
from app.schemas.document import (
    INGESTION_CONTINUATION_PHASES,
    INGESTION_HEAVY_PHASES,
    DocumentChunkView,
    DocumentClassification,
    DocumentDetail,
    DocumentPreprocessArtifact,
    DocumentProcessingConfig,
    DocumentSummary,
    FileStatus,
    IngestionJob,
    IngestionJobLease,
    IngestionJobPhase,
    IngestionJobProgress,
    IngestionJobStatus,
    IngestionSegment,
)
from app.schemas.extraction import StructuredExtraction
from app.schemas.knowledge_base import (
    DEFAULT_KNOWLEDGE_BASE_DESCRIPTION,
    DEFAULT_KNOWLEDGE_BASE_NAME,
    KnowledgeBaseDetail,
    KnowledgeBaseRef,
    KnowledgeBaseStatus,
    KnowledgeBaseSummary,
)
from app.schemas.language_pack_release import (
    LanguagePackDocumentUse,
    LanguagePackIndexRelation,
    LanguagePackRecipeUse,
)
from app.schemas.search import (
    EXTRACTION_FIELD_FILTER_KEY,
    INCLUDE_SUPERSEDED_FILTER_KEY,
    PAGE_RANGES_FILTER_KEY,
    RetrievedChunk,
    SearchMode,
    include_superseded_filter_value,
    parse_extraction_field_filter,
    parse_page_ranges,
)
from app.schemas.search_answer_profile import (
    DEFAULT_SEARCH_ANSWER_PROFILE_DESCRIPTION,
    DEFAULT_SEARCH_ANSWER_PROFILE_NAME,
    SearchAnswerProfileDetail,
    SearchAnswerProfileKnowledgeBaseRef,
    SearchAnswerProfileStatus,
    SearchAnswerProfileSummary,
    SearchAnswerProfileTarget,
)

logger = logging.getLogger(__name__)

TOKEN_PATTERN = re.compile(r"[a-z0-9_]+|[ぁ-んァ-ン一-龯々ー]+", re.IGNORECASE)
ORACLE_TEXT_LEXER_PREFERENCE = "RAG_TEXT_WORLD_LEXER"
ORACLE_TEXT_STOPLIST = "RAG_TEXT_STOPLIST"
ORACLE_TEXT_LEXER = "WORLD_LEXER"
CONVERSATION_TITLE_MAX_CHARS = 80
# 主検索の HNSW 索引(schema DDL・oracle_schema.vector_index_reindex_sql と同じ名前)。
RAG_CHUNKS_VECTOR_INDEX_NAME = "RAG_CHUNKS_EMBEDDING_HNSW_IDX"
ORACLE_TEXT_STOP_WORDS = (
    "の",
    "は",
    "が",
    "を",
    "に",
    "へ",
    "で",
    "と",
    "も",
    "か",
    "です",
    "ます",
    "なん",
    "んで",
)
SEARCHABLE_FILE_STATUSES = {FileStatus.INDEXED}
type MetadataValue = JsonValue
type DbCallRunner = Callable[[Callable[[], Any]], Awaitable[Any]]
T = TypeVar("T")
DocumentT = TypeVar("DocumentT", bound=DocumentSummary)


def _to_vector_bind(embedding: Sequence[float]) -> "array[float]":
    """embedding を Oracle VECTOR(FLOAT32) へバインド可能な float32 配列に変換する。

    python-oracledb は VECTOR 列に list を渡すと配列バインドと誤認するため、
    array('f', ...) として渡す必要がある。
    """
    return array("f", (float(value) for value in embedding))


WALLET_PASSWORD_REQUIRED_ERROR = (  # nosec B105 - パスワードではなくエラーメッセージ定数
    "Oracle Wallet に自動ログイン用の cwallet.sso がないため、Wallet パスワードが必要です。"
    " Wallet パスワードを入力するか、cwallet.sso を含む Wallet ZIP をアップロードしてください。"
)


class OracleCursorProtocol(Protocol):
    """python-oracledb cursor の最小インターフェース。"""

    description: Sequence[Sequence[Any]] | None

    def setinputsizes(self, **kwargs: object) -> Any:
        """bind 型を明示する。"""

    def execute(self, statement: str, parameters: Mapping[str, object] | None = None) -> Any:
        """SQL を実行する。"""

    def executemany(self, statement: str, parameters: Sequence[Mapping[str, object]]) -> Any:
        """同一 SQL を複数 bind で実行する。"""

    def fetchone(self) -> Any:
        """1 行取得する。"""

    def fetchall(self) -> Sequence[Any]:
        """全行取得する。"""

    def close(self) -> Any:
        """cursor を閉じる。"""


class OracleConnectionProtocol(Protocol):
    """python-oracledb connection の最小インターフェース。"""

    def cursor(self) -> OracleCursorProtocol:
        """cursor を返す。"""

    def commit(self) -> Any:
        """transaction を commit する。"""

    def rollback(self) -> Any:
        """transaction を rollback する。"""

    def close(self) -> Any:
        """connection を閉じる。"""


class OraclePoolProtocol(Protocol):
    """python-oracledb pool の最小インターフェース。"""

    def acquire(self) -> OracleConnectionProtocol:
        """connection を取得する。"""

    def close(self, force: bool = False) -> Any:
        """pool を閉じる。"""


@dataclass
class StoredDocument:
    """テスト補助で使うドキュメント行。"""

    id: str
    file_name: str
    status: FileStatus
    uploaded_at: datetime
    object_storage_path: str | None = None
    preprocess_artifact: dict[str, object] | None = None
    content_type: str | None = None
    file_size_bytes: int | None = None
    content_sha256: str | None = None
    duplicate_of_document_id: str | None = None
    tenant_id_hash: str | None = None
    category_name: str | None = None
    indexed_at: datetime | None = None
    extraction: dict[str, object] = field(default_factory=dict)
    error_message: str | None = None
    classification: dict[str, object] | None = None
    superseded_by_document_id: str | None = None
    superseded_at: datetime | None = None


@dataclass(frozen=True)
class OlderDocumentVersion:
    """検索で当たった文書を置き換えた旧版(#1405)。"""

    document_id: str
    file_name: str | None
    # 文書のタイトル(先頭の見出し。版・年度を含むことが多い)。見出しが無ければ None。
    title: str | None
    # この旧版を置き換えた新しい版(検索で当たった文書)。
    superseded_by_document_id: str


@dataclass
class StoredKnowledgeBase:
    """ナレッジベース行。"""

    id: str
    name: str
    status: KnowledgeBaseStatus
    created_at: datetime
    updated_at: datetime
    tenant_id_hash: str | None = None
    description: str | None = None
    default_search_mode: SearchMode = SearchMode.HYBRID
    retrieval_config: dict[str, object] = field(default_factory=dict)
    archived_at: datetime | None = None
    document_count: int = 0
    indexed_document_count: int = 0
    error_document_count: int = 0
    searchable_chunk_count: int = 0


@dataclass
class StoredSearchAnswerProfile:
    """検索・回答プロファイル(Search Answer Profile)行。"""

    id: str
    name: str
    status: SearchAnswerProfileStatus
    created_at: datetime
    updated_at: datetime
    tenant_id_hash: str | None = None
    description: str | None = None
    profile_config: dict[str, object] = field(default_factory=dict)
    archived_at: datetime | None = None


@dataclass
class StoredConversation:
    """チャット会話(conversation)行。検索・回答プロファイル配下に置く。"""

    id: str
    search_answer_profile_id: str
    created_at: datetime
    updated_at: datetime
    title: str | None = None
    status: str = "ACTIVE"
    message_count: int = 0
    tenant_id_hash: str | None = None
    user_id_hash: str | None = None


@dataclass
class StoredMessage:
    """チャットメッセージ(message)行。ASSISTANT は生成モデル・引用・trace を保持する。"""

    id: str
    conversation_id: str
    role: str
    content: str
    created_at: datetime
    reply_to_message_id: str | None = None
    model: str | None = None
    citations: list[dict[str, object]] = field(default_factory=list)
    trace_id: str | None = None
    status: str = "COMPLETE"
    elapsed_ms: float | None = None
    tenant_id_hash: str | None = None
    user_id_hash: str | None = None
    # 回答の処理の経過の記録（共通の span の表の trace id・書けなかった数・工程の詳細。
    # `app.rag.trace_store.TraceRecord` の JSON。#3226）。段階の無いメッセージ（USER）は None。読
    # むときは `parse_trace_record` を通す（古い形式の段のイベントの一覧は読まない）。
    progress: dict[str, Any] | None = None
    # 作成中（STREAMING）の回答を作っているプロセスと、その最後の heartbeat（#1175）。
    lease_owner: str | None = None
    heartbeat_at: datetime | None = None
    # 回答前に確認した内容（USER のメッセージだけ。``ChatClarificationRecord`` の JSON。#1839）。
    clarification: dict[str, Any] | None = None
    # 回答に付けた注意書き（ASSISTANT の回答だけ。#2405）。会話の履歴の本文（content）とは分ける。
    answer_notice: str | None = None
    # 質問に添付したファイル（USER のメッセージだけ。名前・種類・大きさ。#2517）。添付が期限で
    # 消えても発話の表示に残す。添付の無い質問は None。
    attachments: list[dict[str, Any]] | None = None
    # 回答の段落の印・根拠と矛盾して外した段落・回答全体の警告（ASSISTANT の回答だけ。#2738。
    # ``passage_marks`` / ``removed_passages`` / ``answer_warnings`` の dict）。メッセージの列には
    # 保存せず、回答の記録（rag_answer_records の diagnostics_json）から trace_id で読む。
    # 記録の無い回答（保存の失敗・保存期間の経過・この機能より前の回答）は None。
    passage_record: dict[str, Any] | None = None
    # 回答のモデルへ添付した根拠の画像の件数（ASSISTANT の回答だけ。回答の記録の診断の
    # ``models.attached_images``。#3444）。画像を添付しなかった回答・記録の無い回答は 0。
    answer_attached_images: int = 0


_SHARED_ORACLE_POOL: OraclePoolProtocol | None = None
# 共有 pool を作ったときの (pool の id, 接続の設定の指紋)（#2102）。別の製品・別の worker が
# データベースの設定を保存し、`get_settings()` が読み直したら、次に借りるときに新しい設定で pool を
# 作り直す。ここで作っていない pool（テストが差し込んだもの）は作り直さない。
_SHARED_ORACLE_POOL_KEY: tuple[int, str] | None = None
# 共有 pool の作成・入れ替え（#820）。捨てた pool のうち貸し出し中で閉じられなかったものは、
# 終了時（`close_oracle_pool`）に閉じる。
_SHARED_ORACLE_POOL_LOCK = threading.Lock()
_RETIRED_ORACLE_POOLS: list[OraclePoolProtocol] = []
# 捨てた pool を閉じる処理を走らせる（既定は別のスレッド。テストは同期にする）。
_ORACLE_POOL_CLOSER: PoolCloser = close_in_background
_ORACLE_CLIENT_INITIALIZED_LIB_DIR: str | None = None
# 共有 pool の min(開いておく接続の数)。backend の API は起動時に ``warm_oracle_pool`` で
# ``RAG_ORACLE_POOL_MIN`` にし、作り直す pool も同じにする。取込の job のプロセスなどは 1(#1498)。
_SHARED_ORACLE_POOL_MIN = 1
_DB_TEST_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="oracle_db_test_")


class DocumentDeleteBlockedByRunningIngestionError(RuntimeError):
    """実行中 ingestion job があるため document 削除を止めた。"""

    safe_for_user = True


class OracleWalletPasswordRequiredError(RuntimeError):
    """パスワード必須 Wallet を対話プロンプトなしで止める。"""

    safe_for_user = True


class OracleConnectionTimeoutError(TimeoutError):
    """Oracle 接続テストが所定時間内に終わらないときのユーザー向けエラー。"""

    safe_for_user = True


KNOWLEDGE_BASE_NAME_CONFLICT_MESSAGE = (
    "同じ名前のナレッジベース（アーカイブ済みを含む）がすでにあります。別の名前を指定してください。"
)
DEFAULT_ONLY_MEMBERSHIP_REMOVE_MESSAGE = (
    "DEFAULT にだけ所属する文書は外せません。先に別のナレッジベースへ追加してください。"
)
ARCHIVED_SEARCH_ANSWER_PROFILE_UPDATE_MESSAGE = (
    "アーカイブ済みの検索・回答プロファイルは変更できません。"
)


class DocumentSectionsConflictError(ValueError):
    """文書の章節を、読み込んだ後にほかの人が保存していた(#713)。"""


class DocumentReviewEditConflictError(ValueError):
    """抽出確認の修正を保存できない状態だった(#2616)。

    レシピが確認待ちでない・処理中・抽出結果が無い、または読み込んだ後にほかの保存が同じ要素・
    表セルを変えていた。文言は利用者に見せてよい日本語。
    """


@dataclass(frozen=True)
class ReviewedExtractionWrite:
    """抽出確認の修正を当てた抽出成果の書き込み内容(#2616)。"""

    extraction_recipe_id: str
    source_sha256: str
    extraction: Mapping[str, object]
    recipe_subset: Mapping[str, object] | None
    status: str


class KnowledgeBaseNameConflictError(ValueError):
    """同じ tenant に同じ名前（大文字小文字を区別しない）のナレッジベースがある。

    名前の一意制約（`rag_knowledge_bases_tenant_name_uidx`）はアーカイブ済みも含むため、
    作成・改名の前に確かめて 409 で返す（制約違反のまま 500 にしない。#282）。
    """

    def __init__(self) -> None:
        super().__init__(KNOWLEDGE_BASE_NAME_CONFLICT_MESSAGE)


class OracleClient:
    """Oracle AI Database 接続・ベクトル検索クライアント。"""

    def __init__(
        self,
        settings: Settings | None = None,
        pool: OraclePoolProtocol | None = None,
        db_call_runner: DbCallRunner | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._pool_instance = pool
        self._db_call_runner = db_call_runner or _run_db_call_in_thread

    async def vector_search(
        self,
        embedding: list[float],
        top_k: int,
        filters: dict[str, str] | None = None,
    ) -> list[RetrievedChunk]:
        """AI Vector Search で近傍チャンクを取得する。

        例: SELECT ... ORDER BY VECTOR_DISTANCE(embedding, :v, COSINE) FETCH FIRST :k ROWS ONLY
        """
        self._validate_embedding_width(embedding, "query embedding")
        # 処理の経過の段(文書の検索の中の DB の検索。#3436・#3443)。
        with observe_call("stage", VECTOR_SEARCH_STAGE):
            return await self._vector_search_with_oracle(embedding, top_k, filters or {})

    async def keyword_search(
        self,
        query: str,
        top_k: int,
        filters: dict[str, str] | None = None,
    ) -> list[RetrievedChunk]:
        """Oracle Text 相当のキーワード検索を行う。"""
        with observe_call("stage", TEXT_SEARCH_STAGE):
            return await self._keyword_search_with_oracle(query, top_k, filters or {})

    async def hybrid_search(
        self,
        query: str,
        embedding: list[float],
        top_k: int,
        mode: SearchMode = SearchMode.HYBRID,
        filters: dict[str, str] | None = None,
    ) -> list[RetrievedChunk]:
        """ベクトル検索とキーワード検索を Reciprocal Rank Fusion で統合する。"""
        if mode == SearchMode.VECTOR:
            return await self.vector_search(embedding, top_k, filters)
        if mode == SearchMode.KEYWORD:
            return await self.keyword_search(query, top_k, filters)

        # vector と keyword は独立しているので並行に読む(#1490)。融合は順位だけで決まる。
        vector_hits, keyword_hits = await asyncio.gather(
            self.vector_search(embedding, top_k, filters),
            self.keyword_search(query, top_k, filters),
        )
        return self._fuse_hybrid_hits(vector_hits, keyword_hits, top_k)

    async def search_many(
        self,
        searches: Sequence[tuple[str, list[float], SearchMode]],
        top_k: int,
        filters: dict[str, str] | None = None,
    ) -> list[list[RetrievedChunk]]:
        """複数の検索文の検索を、検索文ごとの vector / keyword の SQL を並行に読む(#2800)。

        返す値は ``searches`` の順に、``hybrid_search(query, embedding, top_k, mode, filters)``
        と同じ(候補・順位・score・本文・metadata)。
        """
        if not searches:
            return []
        filters = filters or {}
        branch_kinds: list[str] = []
        binds: dict[str, object] = {}
        plan: list[tuple[int | None, int | None, SearchMode]] = []
        for query, embedding, mode in searches:
            vector_branch: int | None = None
            keyword_branch: int | None = None
            if mode in {SearchMode.VECTOR, SearchMode.HYBRID}:
                self._validate_embedding_width(embedding, "query embedding")
                vector_branch = len(branch_kinds)
                branch_kinds.append("vector")
                binds[f"embedding_{vector_branch}"] = _to_vector_bind(embedding)
            if mode in {SearchMode.KEYWORD, SearchMode.HYBRID}:
                text_query = _oracle_text_query(query, settings=self._settings)
                if text_query is not None:
                    keyword_branch = len(branch_kinds)
                    branch_kinds.append("keyword")
                    binds[f"query_{keyword_branch}"] = text_query
            plan.append((vector_branch, keyword_branch, mode))
        branch_rows: list[list[tuple[int, dict[str, object]]]] = [[] for _ in branch_kinds]
        # 検索文ごとの vector / keyword の検索は 1 本ずつの SQL で並行に読む(#2800)。まとめた SQL
        # (branch ごとの CTE。#1498)は、optimizer が文書の集合の副問い合わせを行ごとの FILTER にし、
        # chunk の全件を走査する計画を選んでいた(検証環境で vector 2 本 20 秒・keyword 4 本 6.5 秒 →
        # 1 本ずつ vector 0.6 秒・keyword 0.1 秒)。
        # vector の検索と keyword の検索は、それぞれ処理の経過の段(文書の検索の中の DB の検索)にする
        # (rag_engine の検索の実装と同じ名前。#3436・#3443)。2 つの段は並行に読むので重なる。
        vector_reads: list[Awaitable[None]] = [
            self._read_vector_branch(
                binds[f"embedding_{n}"], top_k=top_k, filters=filters, into=branch_rows[n]
            )
            for n, kind in enumerate(branch_kinds)
            if kind == "vector"
        ]
        keyword_reads: list[Awaitable[None]] = [
            self._read_keyword_branch(
                str(binds[f"query_{n}"]), top_k=top_k, filters=filters, into=branch_rows[n]
            )
            for n, kind in enumerate(branch_kinds)
            if kind == "keyword"
        ]
        await asyncio.gather(
            _observed_reads(VECTOR_SEARCH_STAGE, vector_reads),
            _observed_reads(TEXT_SEARCH_STAGE, keyword_reads),
        )
        branch_hits: list[list[RetrievedChunk]] = []
        for kind, rows in zip(branch_kinds, branch_rows, strict=True):
            ordered = [row for _rank, row in sorted(rows, key=lambda item: item[0])]
            branch_hits.append(
                [
                    _with_retrieval_metadata(
                        chunk,
                        retrieval_mode=kind,
                        **{
                            f"{kind}_rank": rank,
                            f"{kind}_score": round(_float_value(row.get("score", 0.0)), 6),
                        },
                    )
                    for rank, (chunk, row) in enumerate(_distinct_text_hits(ordered), start=1)
                ]
            )
        results: list[list[RetrievedChunk]] = []
        for vector_branch, keyword_branch, mode in plan:
            vector_hits = branch_hits[vector_branch] if vector_branch is not None else []
            keyword_hits = branch_hits[keyword_branch] if keyword_branch is not None else []
            if mode == SearchMode.VECTOR:
                results.append(vector_hits)
            elif mode == SearchMode.KEYWORD:
                results.append(keyword_hits)
            else:
                results.append(self._fuse_hybrid_hits(vector_hits, keyword_hits, top_k))
        return results

    def _fuse_hybrid_hits(
        self,
        vector_hits: list[RetrievedChunk],
        keyword_hits: list[RetrievedChunk],
        top_k: int,
    ) -> list[RetrievedChunk]:
        """vector と keyword の候補を Reciprocal Rank Fusion で統合する(順位だけで決まる)。"""
        vector_ids = {hit.chunk_id for hit in vector_hits}
        keyword_ids = {hit.chunk_id for hit in keyword_hits}
        fused: dict[str, RetrievedChunk] = {}
        scores: dict[str, float] = {}
        retrieval_metadata: dict[str, dict[str, MetadataValue]] = {}
        for rank, hit in enumerate(vector_hits, start=1):
            fused[hit.chunk_id] = hit
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + _rrf(
                rank, self._settings.rag_rrf_k
            )
            retrieval_metadata.setdefault(hit.chunk_id, {})["vector_rank"] = rank
            retrieval_metadata[hit.chunk_id]["vector_score"] = hit.score
        for rank, hit in enumerate(keyword_hits, start=1):
            fused[hit.chunk_id] = hit
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + _rrf(
                rank, self._settings.rag_rrf_k
            )
            retrieval_metadata.setdefault(hit.chunk_id, {})["keyword_rank"] = rank
            retrieval_metadata[hit.chunk_id]["keyword_score"] = hit.score
        ranked_ids = sorted(
            scores,
            key=lambda chunk_id: _retrieved_chunk_score_sort_key(
                fused[chunk_id],
                scores[chunk_id],
            ),
        )[:top_k]
        fused_count = len(ranked_ids)
        branch_metadata: dict[str, MetadataValue] = {
            "retrieval_vector_count": len(vector_hits),
            "retrieval_keyword_count": len(keyword_hits),
            "retrieval_overlap_count": len(vector_ids & keyword_ids),
            "retrieval_fused_count": fused_count,
            "retrieval_fusion_dropped_count": max(0, len(fused) - fused_count),
        }
        return [
            _with_retrieval_metadata(
                fused[chunk_id].model_copy(update={"score": round(scores[chunk_id], 6)}),
                retrieval_mode=_hybrid_retrieval_mode(retrieval_metadata[chunk_id]),
                rrf_k=self._settings.rag_rrf_k,
                rrf_score=round(scores[chunk_id], 6),
                **branch_metadata,
                **retrieval_metadata[chunk_id],
            )
            for chunk_id in ranked_ids
        ]

    async def fetch_knowledge_base_subgraph(
        self,
        knowledge_base_id: str,
        *,
        limit: int,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        """KG の entity(node)/relationship(edge) を KB scope で可視化用に取得する。

        confidence 降順で node/edge を上限件数まで取り、端点が node 集合に含まれる
        edge のみ返す(部分グラフの整合)。グラフ未構築なら空を返す。
        """
        # entity / relationship の knowledge_base_id は取込時の所属のスナップショットなので、
        # KB から外した文書の要素を出さないよう、今も所属している文書の行だけに絞る(#274)。
        node_rows = await self._fetch_all(
            _render_sql(
                """
            SELECT * FROM (
                SELECT
                    e.entity_id,
                    e.canonical_name,
                    e.entity_type,
                    NVL(e.confidence, 1) AS confidence
                FROM rag_graph_entities e
                WHERE e.knowledge_base_id = :kb
                  AND {membership_sql}
                  AND {active_sql}
                ORDER BY NVL(e.confidence, 1) DESC, e.canonical_name ASC, e.entity_id ASC
            ) WHERE ROWNUM <= :limit
            """,
                membership_sql=_graph_current_membership_predicate("e"),
                active_sql=_graph_active_chunk_set_predicate("e"),
            ),
            {"kb": knowledge_base_id, "limit": limit},
        )
        nodes: list[dict[str, object]] = [
            {
                "id": _optional_str(row.get("entity_id")) or "",
                "name": _optional_str(row.get("canonical_name")) or "",
                "type": _optional_str(row.get("entity_type")),
                "confidence": round(_float_value(row.get("confidence")), 6),
            }
            for row in node_rows
        ]
        node_ids = {node["id"] for node in nodes if node["id"]}
        if not node_ids:
            return nodes, []
        edge_rows = await self._fetch_all(
            _render_sql(
                """
            SELECT * FROM (
                SELECT
                    r.relationship_id,
                    r.source_entity_id,
                    r.target_entity_id,
                    r.relationship_type,
                    NVL(r.confidence, 1) AS confidence
                FROM rag_graph_relationships r
                WHERE r.knowledge_base_id = :kb
                  AND {membership_sql}
                  AND {active_sql}
                ORDER BY NVL(r.confidence, 1) DESC, r.relationship_id ASC
            ) WHERE ROWNUM <= :limit
            """,
                membership_sql=_graph_current_membership_predicate("r"),
                active_sql=_graph_active_chunk_set_predicate("r"),
            ),
            {"kb": knowledge_base_id, "limit": limit},
        )
        edges: list[dict[str, object]] = [
            {
                "id": _optional_str(row.get("relationship_id")) or "",
                "source": _optional_str(row.get("source_entity_id")) or "",
                "target": _optional_str(row.get("target_entity_id")) or "",
                "type": _optional_str(row.get("relationship_type")),
                "confidence": round(_float_value(row.get("confidence")), 6),
            }
            for row in edge_rows
        ]
        return nodes, [
            edge for edge in edges if edge["source"] in node_ids and edge["target"] in node_ids
        ]

    async def context_group_siblings(
        self,
        anchors: list[RetrievedChunk],
        *,
        max_chunks_per_group: int,
        neighbor_child_count: int | None = None,
    ) -> list[RetrievedChunk]:
        """命中した成果物から、親の出典復元用または前後補完用の原文を読む。"""
        if max_chunks_per_group <= 0 or not anchors:
            return []
        return await self._context_group_siblings_with_oracle(
            anchors,
            max_chunks_per_group=max_chunks_per_group,
            neighbor_child_count=neighbor_child_count,
        )

    async def has_retrieval_chunks(self, filters: dict[str, str]) -> bool:
        """検索と同じ条件(KB・分類・文書名など)で、検索対象の chunk が 1 件でもあるかを返す。

        質問が名指しした文書名がナレッジベースに無いときに、その条件を外すために使う(#546)。
        """
        where_sql, binds = _oracle_retrieval_where(filters)
        row = await self._fetch_one(
            _render_sql(
                """
            SELECT 1 AS found
            FROM rag_chunks c
            JOIN rag_documents d ON d.document_id = c.document_id
            WHERE {where_sql}
              AND ROWNUM = 1
            """,
                where_sql=where_sql,
            ),
            binds,
        )
        return row is not None

    async def document_classifications(
        self, document_ids: Sequence[str]
    ) -> dict[str, dict[str, object]]:
        """文書の分類(rag_documents.classification)を document_id ごとにまとめて返す。

        分類の無い文書は含めない。分類は chunk に焼き込まず、回答のときに読む(#545)。
        """
        ids = _unique_optional_sequence(list(document_ids))
        if not ids:
            return {}
        in_sql, binds = _oracle_in_predicate("d.document_id", "classification_document", ids)
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT d.document_id, d.classification
            FROM rag_documents d
            WHERE {in_sql}
              AND {access_sql}
            """,
                in_sql=in_sql,
                access_sql=_oracle_access_predicate_sql(alias="d"),
            ),
            _with_tenant_bind(binds, alias="d"),
        )
        classifications: dict[str, dict[str, object]] = {}
        for row in rows:
            classification = _json_loads(row.get("classification"))
            if classification:
                classifications[str(row["document_id"])] = classification
        return classifications

    async def retrieval_large_categories(self, filters: dict[str, str]) -> list[str]:
        """検索と同じ条件(KB・分類・文書名など)の文書に保存済みの大分類を DISTINCT で返す。

        質問が名指しした業務を、検索範囲の大分類の語の一覧から見つけるために使う(#553)。
        値は保存どおりに返す(比較の正規化は呼び出し側で行う)。
        """
        where_sql, binds = _oracle_retrieval_where(filters)
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT large_category FROM (
                SELECT DISTINCT
                    JSON_VALUE(d.classification, '$.large_category') AS large_category
                FROM rag_documents d
                WHERE d.classification IS NOT NULL
                  AND EXISTS (
                      SELECT 1
                      FROM rag_chunks c
                      WHERE c.document_id = d.document_id
                        AND {where_sql}
                  )
            )
            WHERE large_category IS NOT NULL
            ORDER BY large_category
            """,
                where_sql=where_sql,
            ),
            binds,
        )
        return [str(row["large_category"]) for row in rows if row.get("large_category")]

    async def retrieval_scope_state(self, filters: dict[str, str]) -> str:
        """検索と同じ条件の chunk 集合の状態を表す短い値(画面目録の cache の key。#554)。

        文書の追加・削除・再索引(chunk_id が変わる)・文書名の変更で変わる。
        """
        where_sql, binds = _oracle_retrieval_where(filters)
        row = await self._fetch_one(
            _render_sql(
                """
            SELECT COUNT(*) AS chunk_count,
                   NVL(SUM(ORA_HASH(c.chunk_id || '/' || d.file_name)), 0) AS chunk_hash
            FROM rag_chunks c
            JOIN rag_documents d ON d.document_id = c.document_id
            WHERE {where_sql}
            """,
                where_sql=where_sql,
            ),
            binds,
        )
        if row is None:
            return "0:0"
        return f"{row.get('chunk_count') or 0}:{row.get('chunk_hash') or 0}"

    async def count_documents_outside_language_pack(self, pack_hash: str) -> int:
        """今の言語資源(hash)で分割していない、有効な文書の数(#2181。分割し直しが要る文書)。

        言語資源の hash を残す前に分割した文書(hash の無い chunk)も数える。
        """
        row = await self._fetch_one(
            """
            SELECT COUNT(DISTINCT c.document_id) AS document_count
            FROM rag_chunks c
            JOIN rag_chunk_sets cs ON cs.chunk_set_id = c.chunk_set_id AND cs.is_active = 1
            JOIN rag_documents d ON d.document_id = c.document_id
            WHERE d.superseded_by_document_id IS NULL
              AND NVL(JSON_VALUE(c.metadata_json, '$.language_pack_hash'), '-') <> :pack_hash
            """,
            {"pack_hash": pack_hash},
        )
        return _int_value((row or {}).get("document_count"))

    async def retrieval_screen_sections(
        self, filters: dict[str, str]
    ) -> list[tuple[str, str, int]]:
        """検索と同じ条件の chunk の(文書名、見出しの列、chunk 数)を DISTINCT で返す(#554)。

        見出しの列は chunk の ``section_path``(「 > 」でつないだ文字列)。文書ごとに読み順で返す。
        """
        where_sql, binds = _oracle_retrieval_where(filters)
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT d.file_name,
                   JSON_VALUE(c.metadata_json, '$.section_path') AS section_path,
                   COUNT(*) AS chunk_count
            FROM rag_chunks c
            JOIN rag_documents d ON d.document_id = c.document_id
            WHERE {where_sql}
              AND JSON_VALUE(c.metadata_json, '$.section_path') IS NOT NULL
            GROUP BY d.file_name, JSON_VALUE(c.metadata_json, '$.section_path')
            ORDER BY d.file_name, MIN(c.chunk_index)
            """,
                where_sql=where_sql,
            ),
            binds,
        )
        return [
            (str(row["file_name"]), str(row["section_path"]), _int_value(row.get("chunk_count")))
            for row in rows
            if row.get("file_name") and row.get("section_path")
        ]

    async def retrieval_screen_chunks(
        self, filters: dict[str, str], *, file_name: str, heading: str, limit: int
    ) -> list[RetrievedChunk]:
        """検索と同じ条件で、文書名が一致し見出しの列に ``heading`` を含む chunk を返す(#554)。

        SQL は見出しの部分一致で候補を絞り、見出しの列の要素としての一致は呼び出し側で確かめる。
        """
        where_sql, binds = _oracle_retrieval_where(filters)
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT * FROM (
                SELECT
                    c.document_id,
                    c.chunk_id,
                    c.chunk_text,
                    c.metadata_json,
                    c.chunk_index,
                    c.chunk_set_id,
                    d.file_name,
                    d.category_name,
                    d.superseded_by_document_id,
                    0 AS score
                FROM rag_chunks c
                JOIN rag_documents d ON d.document_id = c.document_id
                WHERE {where_sql}
                  AND d.file_name = :screen_file_name
                  AND INSTR(JSON_VALUE(c.metadata_json, '$.section_path'), :screen_heading) > 0
                ORDER BY c.document_id, c.chunk_index, c.chunk_id
            ) WHERE ROWNUM <= :screen_limit
            """,
                where_sql=where_sql,
            ),
            {
                **binds,
                "screen_file_name": file_name,
                "screen_heading": heading,
                "screen_limit": max(1, int(limit)),
            },
        )
        return [_retrieved_chunk_from_row(row) for row in rows]

    async def retrieval_reference_chunks(
        self,
        filters: dict[str, str],
        *,
        document_id: str,
        section_path: str,
        chunk_set_id: str | None = None,
        limit: int,
    ) -> list[RetrievedChunk]:
        """交差参照の参照先の節(``section_path`` とその下の節)の chunk を読み順に返す(#1280)。

        見え方は検索と同じ条件(tenant・利用できるナレッジベース・有効な chunk_set)。
        ``chunk_set_id`` を渡すと、その版(参照元と同じレシピの出力)だけを読む。
        chunk_set ごとにまとめて並べるので、先頭の chunk_set の chunk から順に返る。
        """
        scoped = {**filters, "document_id": document_id}
        if chunk_set_id:
            scoped["chunk_set_id"] = chunk_set_id
        where_sql, binds = _oracle_retrieval_where(scoped)
        prefix = f"{section_path} > "
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT * FROM (
                SELECT
                    c.document_id,
                    c.chunk_id,
                    c.chunk_text,
                    c.metadata_json,
                    c.chunk_index,
                    c.chunk_set_id,
                    d.file_name,
                    d.category_name,
                    d.superseded_by_document_id,
                    0 AS score
                FROM rag_chunks c
                JOIN rag_documents d ON d.document_id = c.document_id
                WHERE {where_sql}
                  AND (
                      JSON_VALUE(c.metadata_json, '$.section_path') = :reference_section_path
                      OR SUBSTR(
                          JSON_VALUE(c.metadata_json, '$.section_path'),
                          1,
                          :reference_prefix_length
                      ) = :reference_section_prefix
                  )
                ORDER BY c.chunk_set_id, c.chunk_index, c.chunk_id
            ) WHERE ROWNUM <= :reference_limit
            """,
                where_sql=where_sql,
            ),
            {
                **binds,
                "reference_section_path": section_path,
                "reference_section_prefix": prefix,
                "reference_prefix_length": len(prefix),
                "reference_limit": max(1, int(limit)),
            },
        )
        return [_retrieved_chunk_from_row(row) for row in rows]

    async def retrieval_chunk_set_chunks(
        self,
        filters: dict[str, str],
        *,
        document_id: str,
        chunk_set_id: str,
        chunk_indices: Sequence[int] = (),
        element_id: str = "",
        limit: int,
    ) -> list[RetrievedChunk]:
        """文書の 1 つの版(chunk_set)の chunk を、検索と同じ条件で読み順に返す(#2641)。

        検索で当たった派生の chunk が指す原文を読む。``chunk_indices`` は chunk の番号(RAPTOR の
        要約の子)、``element_id`` は解析の要素の ID(項目抽出の値の元の要素。部分一致で絞るので、
        完全一致は呼び出し側で確かめる)で絞る。どちらも無ければ空。見え方は検索と同じ条件
        (tenant・利用できるナレッジベース・有効な chunk_set)で、版は ``filters`` の
        ``chunk_set_id``(有効でない版も読む実験の条件)ではなく、有効な版の中で絞る。
        """
        indices = sorted({int(index) for index in chunk_indices if int(index) >= 0})
        element = element_id.strip()
        if not document_id or not chunk_set_id or (not indices and not element):
            return []
        scoped = {key: value for key, value in filters.items() if key != "chunk_set_id"}
        where_sql, binds = _oracle_retrieval_where({**scoped, "document_id": document_id})
        predicates: list[str] = []
        source_binds: dict[str, object] = {
            "source_chunk_set_id": chunk_set_id,
            "source_limit": max(1, int(limit)),
        }
        if indices:
            index_binds = {
                f"source_chunk_index_{position}": index
                for position, index in enumerate(indices[:_MAX_SOURCE_CHUNK_INDICES])
            }
            placeholders = ", ".join(f":{key}" for key in index_binds)
            predicates.append(f"c.chunk_index IN ({placeholders})")
            source_binds.update(index_binds)
        if element:
            predicates.append(
                "DBMS_LOB.INSTR("
                "JSON_VALUE(c.metadata_json, '$.element_ids' RETURNING CLOB), :source_element_id"
                ") > 0"
            )
            source_binds["source_element_id"] = element
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT
                c.document_id,
                c.chunk_id,
                c.chunk_text,
                c.metadata_json,
                c.chunk_index,
                c.chunk_set_id,
                d.file_name,
                d.category_name,
                d.superseded_by_document_id,
                0 AS score
            FROM rag_chunks c
            JOIN rag_documents d ON d.document_id = c.document_id
            WHERE {where_sql}
              AND c.chunk_set_id = :source_chunk_set_id
              AND ({source_sql})
            ORDER BY c.chunk_index, c.chunk_id
            FETCH FIRST :source_limit ROWS ONLY
            """,
                where_sql=where_sql,
                source_sql=" AND ".join(predicates),
            ),
            {**binds, **source_binds},
        )
        return [_retrieved_chunk_from_row(row) for row in rows]

    async def retrieval_reference_documents(
        self, filters: dict[str, str], *, title: str, limit: int
    ) -> list[tuple[str, str, tuple[str, ...]]]:
        """検索と同じ条件の文書のうち、文書名かタイトルに ``title`` を含むものを返す(#1280・#1400)。

        1 つの文書名で ``retrieval_reference_documents_many`` を呼ぶ(返す値と並びは同じ)。
        """
        found = await self.retrieval_reference_documents_many(filters, titles=[title], limit=limit)
        return found.get(title, [])

    async def retrieval_reference_documents_many(
        self, filters: dict[str, str], *, titles: Sequence[str], limit: int
    ) -> dict[str, list[tuple[str, str, tuple[str, ...]]]]:
        """検索と同じ条件の文書のうち、文書名かタイトルに各 ``titles`` を含むもの(#1280・#1400)。

        返す値は文書名ごとの (id, 文書名, タイトルの候補) の一覧(当たりの無い文書名は含めない)。
        タイトルの候補は、見出しの列(``section_path``)が文書名を含む chunk の、見出しの列の先頭の
        見出し(文書のタイトル。「定期保守計画 2026年度」)を読み順に並べたもの。どれを文書の
        タイトルとみなすか(番号の無い見出しか)と、別名での照合は呼び出し側
        (``cross_references.document_name_match``)が決める。

        文書名は ``cross_references.title_key`` の形(NFKC・小文字・空白と区切りの記号を除く)で
        渡す。保存した文書名・見出しが全角の英数字でも当たるよう、全角の形でも比べる。

        他の文書への交差参照(「経費精算マニュアルの「権限」を参照」「時間帯は定期保守計画で
        確かめます」)の参照先の文書を、検索範囲(ナレッジベース)の中だけから探す。旧版の文書は
        検索と同じく既定では含めない。見出しの列は本文・埋め込みと同じ表(rag_chunks)にあり、
        照合には範囲の chunk を全件読むため、複数の文書名を 1 回の読み取りで照合する(#2137)。
        文書名ごとの件数(``limit``)と並び(文書名の短い順・文書・読み順)は 1 つずつの照合と同じ。
        """
        unique_titles = _unique_optional_sequence([title for title in titles if title])
        if not unique_titles:
            return {}
        where_sql, binds = _oracle_retrieval_where(filters)
        # 範囲の chunk を見出しの列ごとに 1 行にして 1 回だけ読み、文書名との照合・文書名ごとの
        # 件数と並びはここで決める(#2800)。文書名の行と範囲の chunk を SQL の中で INSTR で結ぶと、
        # 範囲の chunk を文書名の数だけ読み直し、1 回 9 秒かかった(見出しの列ごとに読むと 1 秒)。
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT x.document_id, x.file_name, x.section_path,
                   MIN(x.chunk_index) AS first_chunk_index
            FROM (
                SELECT d.document_id,
                       d.file_name,
                       c.chunk_index,
                       JSON_VALUE(c.metadata_json, '$.section_path') AS section_path
                FROM rag_chunks c
                JOIN rag_documents d ON d.document_id = c.document_id
                WHERE {where_sql}
            ) x
            GROUP BY x.document_id, x.file_name, x.section_path
            """,
                where_sql=where_sql,
            ),
            binds,
        )
        return _match_reference_documents(rows, unique_titles, limit=max(1, int(limit)))

    async def retrieval_older_versions(
        self, filters: dict[str, str], document_ids: Sequence[str], *, limit: int
    ) -> list[OlderDocumentVersion]:
        """検索と同じ条件の中の、``document_ids`` の文書を置き換えた旧版を返す(#1405)。

        旧版は既定で検索しないため、検索で当たった今の版に旧版があっても結果からは分からない。
        MCP の根拠の結果に旧版(文書名とタイトル。タイトルは版・年度を含むことが多い)を示し、旧版の
        年度を名指しする質問で ``include_superseded`` を渡す手がかりにする。``filters`` は検索の
        条件(ナレッジベース・分類など)で、旧版を含めた検索と同じ範囲(利用者の権限・データの範囲を
        含む)の中の旧版だけを返す。タイトルは、見出しのある最初の chunk の見出しの列の先頭の
        見出し(「定期保守計画 2025年度」)。
        """
        ids = _unique_sequence(list(document_ids))
        if not ids:
            return []
        where_sql, binds = _oracle_retrieval_where(
            {**filters, INCLUDE_SUPERSEDED_FILTER_KEY: "true"}
        )
        in_sql, in_binds = _oracle_in_predicate(
            "d.superseded_by_document_id", "older_version_of", ids
        )
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT * FROM (
                SELECT t.document_id, t.file_name, t.superseded_by_document_id, t.root_heading
                FROM (
                    SELECT d.document_id,
                           d.file_name,
                           d.superseded_by_document_id,
                           d.superseded_at,
                           CASE
                               WHEN INSTR(JSON_VALUE(c.metadata_json, '$.section_path'), ' > ') > 0
                               THEN SUBSTR(
                                   JSON_VALUE(c.metadata_json, '$.section_path'),
                                   1,
                                   INSTR(JSON_VALUE(c.metadata_json, '$.section_path'), ' > ') - 1
                               )
                               ELSE JSON_VALUE(c.metadata_json, '$.section_path')
                           END AS root_heading,
                           ROW_NUMBER() OVER (
                               PARTITION BY d.document_id
                               ORDER BY
                                   CASE
                                       WHEN JSON_VALUE(c.metadata_json, '$.section_path') IS NULL
                                       THEN 1
                                       ELSE 0
                                   END,
                                   c.chunk_index
                           ) AS heading_rank
                    FROM rag_chunks c
                    JOIN rag_documents d ON d.document_id = c.document_id
                    WHERE {where_sql}
                      AND {in_sql}
                ) t
                WHERE t.heading_rank = 1
                ORDER BY t.superseded_at DESC NULLS LAST, t.file_name, t.document_id
            ) WHERE ROWNUM <= :older_version_limit
            """,
                where_sql=where_sql,
                in_sql=in_sql,
            ),
            {**binds, **in_binds, "older_version_limit": max(1, int(limit))},
        )
        versions: list[OlderDocumentVersion] = []
        for row in rows:
            document_id = _optional_str(row.get("document_id"))
            superseded_by = _optional_str(row.get("superseded_by_document_id"))
            if document_id is None or superseded_by is None:
                continue
            versions.append(
                OlderDocumentVersion(
                    document_id=document_id,
                    file_name=_optional_str(row.get("file_name")),
                    title=_optional_str(row.get("root_heading")),
                    superseded_by_document_id=superseded_by,
                )
            )
        return versions

    async def retrievable_chunk(self, document_id: str, chunk_id: str) -> RetrievedChunk | None:
        """検索と同じ見え方の条件で 1 件の chunk を返す（MCP の ``rag_read_source``。#1219）。

        tenant・利用できるナレッジベース・INDEXED の文書・有効な chunk_set に限る。ID を知って
        いても、検索で見えない chunk は返さない。新しい版に置き換えた文書(旧版)も文書としては
        有効なので読める(#1248。根拠の ``superseded`` で旧版と分かる)。有効期間では絞らない
        (過去の基準日の検索で返った根拠を、日付を跨いでも読める。#3249)。
        """
        where_sql, binds = _oracle_retrieval_where(
            {INCLUDE_SUPERSEDED_FILTER_KEY: "true"}, effective_period=False
        )
        row = await self._fetch_one(
            _render_sql(
                """
            SELECT
                c.document_id,
                c.chunk_id,
                c.chunk_text,
                c.metadata_json,
                c.chunk_index,
                c.chunk_set_id,
                d.file_name,
                d.category_name,
                d.superseded_by_document_id,
                0 AS score
            FROM rag_chunks c
            JOIN rag_documents d ON d.document_id = c.document_id
            WHERE {where_sql}
              AND c.document_id = :read_document_id
              AND c.chunk_id = :read_chunk_id
            """,
                where_sql=where_sql,
            ),
            {**binds, "read_document_id": document_id, "read_chunk_id": chunk_id},
        )
        return _retrieved_chunk_from_row(row) if row is not None else None

    async def accessible_chunk_exists(self, document_id: str, chunk_id: str) -> bool:
        """利用者が見られる文書に、その chunk の行が（古い版を含めて）残っているか（#1219）。

        ``retrievable_chunk`` で見えないとき、古い版（有効でない chunk_set）か、無い・見えないかを
        分けるために使う。
        """
        row = await self._fetch_one(
            _render_sql(
                """
            SELECT 1 AS found
            FROM rag_chunks c
            JOIN rag_documents d ON d.document_id = c.document_id
            WHERE c.document_id = :read_document_id
              AND c.chunk_id = :read_chunk_id
              AND {access_predicate}
            """,
                access_predicate=_oracle_access_predicate_sql(alias="d"),
            ),
            _with_tenant_bind({"read_document_id": document_id, "read_chunk_id": chunk_id}),
        )
        return row is not None

    async def chunk_set_extraction_recipe_ids(self, chunk_set_ids: Sequence[str]) -> dict[str, str]:
        """chunk_set_id ごとの解析の結果の ID（根拠の要素の定位子に使う。#1330）。"""
        ids = _unique_optional_sequence(list(chunk_set_ids))
        if not ids:
            return {}
        in_sql, binds = _oracle_in_predicate("cs.chunk_set_id", "locator_chunk_set", ids)
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT cs.chunk_set_id, cs.extraction_recipe_id
            FROM rag_chunk_sets cs
            WHERE {in_sql}
              AND cs.extraction_recipe_id IS NOT NULL
            """,
                in_sql=in_sql,
            ),
            binds,
        )
        return {
            str(row["chunk_set_id"]): str(row["extraction_recipe_id"])
            for row in rows
            if row.get("chunk_set_id") and row.get("extraction_recipe_id")
        }

    async def retrievable_element_chunk(
        self, document_id: str, extraction_recipe_id: str, element_id: str
    ) -> RetrievedChunk | None:
        """要素の定位子が指す要素を含む、今の有効な chunk を返す（#1330）。

        ``retrievable_chunk`` と同じ見え方の条件（tenant・ナレッジベース・INDEXED・
        有効な chunk_set）に、同じ解析の結果（``extraction_recipe_id``）の chunk_set を加える。
        文書分割の設定を変えて chunk を作り直しても、同じ要素を含む chunk を返す。
        1 つの要素が複数の chunk に分かれたときは、文書の中で最初の chunk を返す。
        """
        where_sql, binds = _oracle_retrieval_where(
            {INCLUDE_SUPERSEDED_FILTER_KEY: "true"}, effective_period=False
        )
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT
                c.document_id,
                c.chunk_id,
                c.chunk_text,
                c.metadata_json,
                c.chunk_index,
                c.chunk_set_id,
                d.file_name,
                d.category_name,
                d.superseded_by_document_id,
                0 AS score
            FROM rag_chunks c
            JOIN rag_documents d ON d.document_id = c.document_id
            JOIN rag_chunk_sets locator_cs
              ON locator_cs.chunk_set_id = c.chunk_set_id
             AND locator_cs.extraction_recipe_id = :read_extraction_recipe_id
            WHERE {where_sql}
              AND c.document_id = :read_document_id
              AND DBMS_LOB.INSTR(
                  JSON_VALUE(c.metadata_json, '$.element_ids' RETURNING CLOB), :read_element_id
              ) > 0
            ORDER BY c.chunk_index
            FETCH FIRST 50 ROWS ONLY
            """,
                where_sql=where_sql,
            ),
            {
                **binds,
                "read_document_id": document_id,
                "read_extraction_recipe_id": extraction_recipe_id,
                "read_element_id": element_id,
            },
        )
        from app.rag.element_locator import chunk_element_ids

        for row in rows:
            chunk = _retrieved_chunk_from_row(row)
            # INSTR は部分一致なので、要素の ID の完全一致で確かめる
            # （p1-1 と p1-10 を取り違えない）。
            if element_id in chunk_element_ids(chunk.metadata):
                return chunk
        return None

    async def accessible_document_exists(self, document_id: str) -> bool:
        """利用者が見られる文書か（要素の定位子が今の解析の結果に無いときの判定。#1330）。

        解析をやり直すと古い解析の結果の chunk_set は消えるため、古い定位子かどうかは、文書が
        見えるかどうかで分ける（見える文書の、今は無い要素 = 古い版）。
        """
        row = await self._fetch_one(
            _render_sql(
                """
            SELECT 1 AS found
            FROM rag_documents d
            WHERE d.document_id = :read_document_id
              AND {access_predicate}
            """,
                access_predicate=_oracle_access_predicate_sql(alias="d"),
            ),
            _with_tenant_bind({"read_document_id": document_id}),
        )
        return row is not None

    async def accessible_document_ids(self, document_ids: Sequence[str]) -> set[str]:
        """``document_ids`` のうち、利用者が今見られる文書の ID（保存した根拠の確かめ直し。#3252）。

        ``accessible_document_exists`` と同じ条件（tenant・文書 / 分類 / ナレッジベースの範囲。
        重複アップロードの正本は複製の KB で見える）を、IN の上限より少ない数ずつまとめて当てる。
        削除した文書は行が無いので含まない。
        """
        ids = _unique_optional_sequence(document_ids)
        readable: set[str] = set()
        for start in range(0, len(ids), _EVALUATION_LOCATION_BATCH):
            batch = ids[start : start + _EVALUATION_LOCATION_BATCH]
            in_sql, in_binds = _oracle_in_predicate("d.document_id", "readable_document", batch)
            rows = await self._fetch_all(
                _render_sql(
                    """
                SELECT d.document_id
                FROM rag_documents d
                WHERE {in_sql}
                  AND {access_predicate}
                """,
                    in_sql=in_sql,
                    access_predicate=_oracle_access_predicate_sql(alias="d"),
                ),
                _with_tenant_bind(in_binds),
            )
            readable.update(str(row["document_id"]) for row in rows if row.get("document_id"))
        return readable

    async def document_reading_index(
        self, document_id: str, *, chunk_set_id: str | None = None, limit: int = 5000
    ) -> tuple[str | None, list[dict[str, object]]]:
        """文書を順に読むための chunk の目次（本文は読まない。#1332）。

        MCP の rag_outline / rag_read_document が使う。検索と同じ見え方の条件（tenant・
        ナレッジベース・INDEXED・有効な chunk_set）で、文書の 1 つの chunk_set（``chunk_set_id``
        が無ければ、有効な chunk_set のうちレシピの番号が最も小さいもの）の chunk を、chunk の順に
        ``chunk_index`` / ``chunk_id`` / ``section_path`` / ``page_start`` / ``page_end`` /
        ``chars`` で返す。複数レシピの文書は同じ本文を別の分け方で持つだけなので、1 つだけを読む。
        """
        where_sql, binds = _oracle_retrieval_where(
            {INCLUDE_SUPERSEDED_FILTER_KEY: "true"}, effective_period=False
        )
        chunk_set_sql = (
            ":read_chunk_set_id"
            if chunk_set_id is not None
            else """(
                SELECT primary_cs.chunk_set_id
                FROM rag_chunk_sets primary_cs
                JOIN rag_document_recipes primary_r ON primary_r.recipe_id = primary_cs.recipe_id
                WHERE primary_cs.document_id = :read_document_id
                  AND primary_cs.is_active = 1
                  AND primary_cs.status = 'INDEXED'
                ORDER BY primary_r.slot_no, primary_cs.chunk_set_id
                FETCH FIRST 1 ROWS ONLY
            )"""
        )
        query_binds: dict[str, object] = {
            **binds,
            "read_document_id": document_id,
            "read_limit": max(1, int(limit)),
        }
        if chunk_set_id is not None:
            query_binds["read_chunk_set_id"] = chunk_set_id
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT
                c.chunk_set_id,
                c.chunk_index,
                c.chunk_id,
                JSON_VALUE(c.metadata_json, '$.section_path') AS section_path,
                JSON_VALUE(c.metadata_json, '$.page_start' RETURNING NUMBER) AS page_start,
                JSON_VALUE(c.metadata_json, '$.page_end' RETURNING NUMBER) AS page_end,
                DBMS_LOB.GETLENGTH(c.chunk_text) AS chars
            FROM rag_chunks c
            JOIN rag_documents d ON d.document_id = c.document_id
            WHERE {where_sql}
              AND c.document_id = :read_document_id
              AND c.chunk_set_id = {chunk_set_sql}
            ORDER BY c.chunk_index
            FETCH FIRST :read_limit ROWS ONLY
            """,
                where_sql=where_sql,
                chunk_set_sql=chunk_set_sql,
            ),
            query_binds,
        )
        if not rows:
            return None, []
        return str(rows[0]["chunk_set_id"]), [
            {
                "chunk_index": _int_value(row.get("chunk_index")),
                "chunk_id": str(row["chunk_id"]),
                "section_path": _optional_str(row.get("section_path")),
                "page_start": _optional_int(row.get("page_start")),
                "page_end": _optional_int(row.get("page_end")),
                "chars": _int_value(row.get("chars")),
            }
            for row in rows
        ]

    async def evaluation_document_locations(
        self, document_ids: Sequence[str]
    ) -> dict[str, tuple[int | None, tuple[str, ...]]]:
        """文書ごとの最後のページの番号とシート名（品質評価のテンプレートの「文書一覧」。#2086）。

        検索と同じ見え方の条件（tenant・INDEXED・有効な chunk_set）の chunk の metadata から求める
        （本文は読まない）。ページ・シートの無い文書は (None, ()) で、chunk の無い文書は結果に
        含めない。
        """
        locations: dict[str, tuple[int | None, tuple[str, ...]]] = {}
        unique_ids = _unique_optional_sequence(document_ids)
        for offset in range(0, len(unique_ids), _EVALUATION_LOCATION_BATCH):
            batch = unique_ids[offset : offset + _EVALUATION_LOCATION_BATCH]
            where_sql, binds = _oracle_retrieval_where(
                {INCLUDE_SUPERSEDED_FILTER_KEY: "true"}, effective_period=False
            )
            in_sql, in_binds = _oracle_in_predicate("c.document_id", "location_document_id", batch)
            rows = await self._fetch_all(
                _render_sql(
                    """
                SELECT
                    c.document_id,
                    MAX(
                        COALESCE(
                            JSON_VALUE(c.metadata_json, '$.page_end' RETURNING NUMBER),
                            JSON_VALUE(c.metadata_json, '$.page_start' RETURNING NUMBER),
                            JSON_VALUE(c.metadata_json, '$.page_number' RETURNING NUMBER)
                        )
                    ) AS max_page,
                    LISTAGG(
                        DISTINCT JSON_VALUE(c.metadata_json, '$.sheet_name'), CHR(31)
                        ON OVERFLOW TRUNCATE '' WITHOUT COUNT
                    ) WITHIN GROUP (
                        ORDER BY JSON_VALUE(c.metadata_json, '$.sheet_name')
                    ) AS sheet_names
                FROM rag_chunks c
                JOIN rag_documents d ON d.document_id = c.document_id
                WHERE {where_sql}
                  AND {in_sql}
                GROUP BY c.document_id
                """,
                    where_sql=where_sql,
                    in_sql=in_sql,
                ),
                {**binds, **in_binds},
            )
            for row in rows:
                sheets = str(row.get("sheet_names") or "")
                locations[str(row["document_id"])] = (
                    _optional_int(row.get("max_page")),
                    tuple(name for name in sheets.split(chr(31)) if name.strip()),
                )
        return locations

    async def readable_document_chunks(
        self, document_id: str, *, chunk_set_id: str, from_index: int, limit: int
    ) -> list[RetrievedChunk]:
        """文書の 1 つの chunk_set の chunk を、``from_index`` から chunk の順に読む（#1332）。

        見え方の条件は ``document_reading_index`` と同じ。chunk_set が有効でなくなっていれば空。
        """
        where_sql, binds = _oracle_retrieval_where(
            {INCLUDE_SUPERSEDED_FILTER_KEY: "true"}, effective_period=False
        )
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT
                c.document_id,
                c.chunk_id,
                c.chunk_text,
                c.metadata_json,
                c.chunk_index,
                c.chunk_set_id,
                d.file_name,
                d.category_name,
                d.superseded_by_document_id,
                0 AS score
            FROM rag_chunks c
            JOIN rag_documents d ON d.document_id = c.document_id
            WHERE {where_sql}
              AND c.document_id = :read_document_id
              AND c.chunk_set_id = :read_chunk_set_id
              AND c.chunk_index >= :read_from_index
            ORDER BY c.chunk_index
            FETCH FIRST :read_limit ROWS ONLY
            """,
                where_sql=where_sql,
            ),
            {
                **binds,
                "read_document_id": document_id,
                "read_chunk_set_id": chunk_set_id,
                "read_from_index": max(0, int(from_index)),
                "read_limit": max(1, int(limit)),
            },
        )
        return [_retrieved_chunk_from_row(row) for row in rows]

    async def chunk_set_first_page_contexts(
        self, chunk_set_ids: Sequence[str]
    ) -> dict[str, dict[str, object]]:
        """chunk set の文書の 1 ページ目の本文(first_page_context)を chunk_set_id ごとに返す。

        回答の「文書の背景」に使う。ヒットした chunk set の分を IN で 1 回だけ読む。値の無い
        chunk set は含めない(#557)。
        """
        ids = _unique_optional_sequence(list(chunk_set_ids))
        if not ids:
            return {}
        in_sql, binds = _oracle_in_predicate("cs.chunk_set_id", "first_page_chunk_set", ids)
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT cs.chunk_set_id, cs.first_page_context
            FROM rag_chunk_sets cs
            JOIN rag_documents d ON d.document_id = cs.document_id
            WHERE {in_sql}
              AND cs.first_page_context IS NOT NULL
              AND {access_sql}
            """,
                in_sql=in_sql,
                access_sql=_oracle_access_predicate_sql(alias="d"),
            ),
            _with_tenant_bind(binds, alias="d"),
        )
        contexts: dict[str, dict[str, object]] = {}
        for row in rows:
            context = _json_loads(row.get("first_page_context"))
            if context:
                contexts[str(row["chunk_set_id"])] = context
        return contexts

    async def create_document(
        self,
        file_name: str,
        object_storage_path: str,
        content_type: str | None,
        file_size_bytes: int | None = None,
        content_sha256: str | None = None,
        duplicate_of_document_id: str | None = None,
        knowledge_base_ids: Sequence[str] | None = None,
    ) -> DocumentDetail:
        """ドキュメント行を作成する。"""
        return await self._create_document_with_oracle(
            file_name=file_name,
            object_storage_path=object_storage_path,
            content_type=content_type,
            file_size_bytes=file_size_bytes,
            content_sha256=content_sha256,
            duplicate_of_document_id=duplicate_of_document_id,
            knowledge_base_ids=knowledge_base_ids,
        )

    async def create_knowledge_base(
        self,
        *,
        name: str,
        description: str | None = None,
        default_search_mode: SearchMode = SearchMode.HYBRID,
        retrieval_config: Mapping[str, object] | None = None,
    ) -> KnowledgeBaseDetail:
        """ナレッジベースを作成する。"""
        return await self._create_knowledge_base_with_oracle(
            name=name,
            description=description,
            default_search_mode=default_search_mode,
            retrieval_config=dict(retrieval_config or {}),
        )

    async def list_knowledge_bases(
        self,
        *,
        status: KnowledgeBaseStatus | None = None,
        query: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        knowledge_base_ids: Sequence[str] | None = None,
    ) -> list[KnowledgeBaseSummary]:
        """ナレッジベース一覧を返す。`knowledge_base_ids` を渡すとその ID に絞る(#302)。"""
        return await self._list_knowledge_bases_with_oracle(
            status=status,
            query=query,
            limit=limit,
            offset=offset,
            knowledge_base_ids=knowledge_base_ids,
        )

    async def count_knowledge_bases(
        self,
        *,
        status: KnowledgeBaseStatus | None = None,
        query: str | None = None,
        knowledge_base_ids: Sequence[str] | None = None,
    ) -> int:
        """条件に一致するナレッジベース数を返す。"""
        return await self._count_knowledge_bases_with_oracle(
            status=status, query=query, knowledge_base_ids=knowledge_base_ids
        )

    async def get_knowledge_base(
        self,
        knowledge_base_id: str,
    ) -> KnowledgeBaseDetail | None:
        """ナレッジベース詳細を返す。"""
        return await self._get_knowledge_base_with_oracle(knowledge_base_id)

    async def update_knowledge_base(
        self,
        knowledge_base_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        default_search_mode: SearchMode | None = None,
        retrieval_config: Mapping[str, object] | None = None,
        update_fields: set[str] | None = None,
    ) -> KnowledgeBaseDetail:
        """ナレッジベースの基本情報を更新する。"""
        return await self._update_knowledge_base_with_oracle(
            knowledge_base_id=knowledge_base_id,
            name=name,
            description=description,
            default_search_mode=default_search_mode,
            retrieval_config=(dict(retrieval_config) if retrieval_config is not None else None),
            update_fields=update_fields,
        )

    async def archive_knowledge_base(self, knowledge_base_id: str) -> KnowledgeBaseDetail:
        """ナレッジベースをアーカイブする。文書・chunk は削除しない。"""
        return await self._archive_knowledge_base_with_oracle(knowledge_base_id)

    async def assign_documents_to_knowledge_base(
        self,
        knowledge_base_id: str,
        document_ids: Sequence[str],
    ) -> KnowledgeBaseDetail:
        """既存文書をナレッジベースへ追加する。"""
        return await self._assign_documents_to_knowledge_base_with_oracle(
            knowledge_base_id,
            document_ids,
        )

    async def remove_document_from_knowledge_base(
        self,
        knowledge_base_id: str,
        document_id: str,
    ) -> KnowledgeBaseDetail:
        """文書をナレッジベースから外す。文書自体は削除しない。"""
        return await self._remove_document_from_knowledge_base_with_oracle(
            knowledge_base_id,
            document_id,
        )

    async def replace_document_knowledge_bases(
        self,
        document_id: str,
        knowledge_base_ids: Sequence[str],
    ) -> list[KnowledgeBaseRef]:
        """文書の所属ナレッジベースを指定リストへ置換する。"""
        return await self._replace_document_knowledge_bases_with_oracle(
            document_id,
            knowledge_base_ids,
        )

    async def list_document_knowledge_bases(self, document_id: str) -> list[KnowledgeBaseRef]:
        """文書の所属ナレッジベース一覧を返す。"""
        return await self._list_document_knowledge_bases_with_oracle(document_id)

    async def get_knowledge_base_extraction_fields(
        self, knowledge_base_id: str
    ) -> list[FieldDefinition] | None:
        """KB の項目抽出の定義を返す。未設定(全体の既定に従う)と KB が無いときは None(#548)。"""
        row = await self._fetch_one(
            """
            SELECT kb.extraction_fields
            FROM rag_knowledge_bases kb
            WHERE kb.knowledge_base_id = :knowledge_base_id
              AND {knowledge_base_access_sql}
            """.format(
                knowledge_base_access_sql=_oracle_knowledge_base_access_predicate_sql(alias="kb"),
            ),
            _with_tenant_bind({"knowledge_base_id": knowledge_base_id}),
        )
        return _knowledge_base_extraction_fields(row)

    async def update_knowledge_base_extraction_fields(
        self,
        knowledge_base_id: str,
        fields: list[FieldDefinition] | None,
    ) -> None:
        """KB の項目抽出の定義を保存する。None は未設定に戻す(全体の既定に従う。#548)。"""
        value = None if fields is None else FieldSchemaStore(fields=fields).model_dump()

        def operation(connection: OracleConnectionProtocol) -> None:
            _execute(
                connection,
                _render_sql(
                    """
                    UPDATE rag_knowledge_bases
                    SET extraction_fields = :extraction_fields,
                        updated_at = SYSTIMESTAMP
                    WHERE knowledge_base_id = :knowledge_base_id
                      AND {knowledge_base_access_sql}
                    """,
                    knowledge_base_access_sql=_oracle_knowledge_base_access_predicate_sql(),
                ),
                _with_tenant_bind(
                    {
                        "knowledge_base_id": knowledge_base_id,
                        "extraction_fields": _json_bind(value),
                    }
                ),
                input_sizes=_json_input_sizes("extraction_fields"),
            )

        await self._run_transaction(operation)

    async def list_document_extraction_field_sets(
        self, document_id: str
    ) -> list[list[FieldDefinition] | None]:
        """文書が属する有効な KB ごとの項目抽出の定義(None は既定に従う)を作成の古い順に返す。"""
        field_sets = await self.list_documents_extraction_field_sets([document_id])
        return field_sets.get(document_id, [])

    async def list_documents_extraction_field_sets(
        self, document_ids: Sequence[str]
    ) -> dict[str, list[list[FieldDefinition] | None]]:
        """複数の文書について、属する有効な KB ごとの項目抽出の定義を 1 回の問い合わせで返す。

        文書 ID ごとに、KB の作成の古い順の定義(None は既定に従う)を並べる。有効な KB に属さない
        文書は結果に含めない(呼び出し側で全体の既定を使う。#548 / #550)。
        """
        if not document_ids:
            return {}
        in_sql, in_binds = _oracle_in_predicate(
            "dkb.document_id", "extraction_document_id", list(document_ids)
        )
        rows = await self._fetch_all(
            """
            SELECT dkb.document_id, kb.extraction_fields
            FROM rag_document_knowledge_bases dkb
            JOIN rag_knowledge_bases kb
                ON kb.knowledge_base_id = dkb.knowledge_base_id
            JOIN rag_documents d
                ON d.document_id = dkb.document_id
            WHERE {in_sql}
              AND kb.status = 'ACTIVE'
              AND {document_access_sql}
              AND {knowledge_base_access_sql}
            ORDER BY dkb.document_id ASC, kb.created_at ASC, kb.knowledge_base_id ASC
            """.format(
                in_sql=in_sql,
                document_access_sql=_oracle_access_predicate_sql(alias="d"),
                knowledge_base_access_sql=_oracle_knowledge_base_access_predicate_sql(alias="kb"),
            ),
            _with_tenant_bind(in_binds),
        )
        field_sets: dict[str, list[list[FieldDefinition] | None]] = {}
        for row in rows:
            field_sets.setdefault(str(row["document_id"]), []).append(
                _knowledge_base_extraction_fields(row)
            )
        return field_sets

    async def list_knowledge_base_extraction_field_sets(
        self, knowledge_base_ids: Sequence[str]
    ) -> list[list[FieldDefinition] | None]:
        """指定した有効な KB ごとの項目抽出の定義を作成の古い順に返す(検索の絞り込みの候補)。"""
        if not knowledge_base_ids:
            return []
        in_sql, in_binds = _oracle_in_predicate(
            "kb.knowledge_base_id", "extraction_kb_id", list(knowledge_base_ids)
        )
        rows = await self._fetch_all(
            """
            SELECT kb.extraction_fields
            FROM rag_knowledge_bases kb
            WHERE {in_sql}
              AND kb.status = 'ACTIVE'
              AND {knowledge_base_access_sql}
            ORDER BY kb.created_at ASC, kb.knowledge_base_id ASC
            """.format(
                in_sql=in_sql,
                knowledge_base_access_sql=_oracle_knowledge_base_access_predicate_sql(alias="kb"),
            ),
            _with_tenant_bind(in_binds),
        )
        return [_knowledge_base_extraction_fields(row) for row in rows]

    # ------------------------------------------------------------------
    # variant materialization: chunk_set / KB binding 永続層
    # dedup/refcount/GC の計算は app.rag.variant_planner(決定論)が担い、
    # 本メソッド群はその計画を Oracle へ反映する手。refcount は binding 件数から導出する。
    # ------------------------------------------------------------------

    async def ensure_default_document_recipe(self, document_id: str) -> dict[str, object]:
        """レシピ1が無ければ文書 legacy 状態から補完する(書き込みの経路だけで呼ぶ)。

        GET(`list_document_recipes` / `get_document_recipe`)からは呼ばない。レシピ1は文書の登録・
        migration・ジョブの投入・レシピの追加と編集の transaction で作る(#341)。
        """

        def operation(connection: OracleConnectionProtocol) -> dict[str, object]:
            return _ensure_default_document_recipe_row(connection, document_id)

        return await self._run_transaction(operation)

    async def list_language_reprocessing_targets(
        self, document_ids: Sequence[str]
    ) -> list[dict[str, object]]:
        """選択文書の実在するレシピだけを、同じ利用範囲で一度に読む。"""
        if not document_ids:
            return []
        return await self._fetch_all(
            """
            SELECT d.document_id, d.file_name, r.recipe_id, r.slot_no, r.config_revision,
                cs.language_pack_release_id AS used_release_id,
                cs.language_pack_ingest_hash AS used_ingest_hash
            FROM rag_documents d
            JOIN rag_document_recipes r ON r.document_id = d.document_id
            LEFT JOIN rag_chunk_sets cs ON cs.recipe_id = r.recipe_id AND cs.is_active = 1
            JOIN JSON_TABLE(:selected_ids, '$[*]'
                COLUMNS (document_id VARCHAR2(64) PATH '$')) selected
                ON selected.document_id = d.document_id
            WHERE {access_sql}
            ORDER BY {name_order}, NLSSORT(d.file_name, 'NLS_SORT=JAPANESE_M'),
                d.file_name, d.document_id, r.slot_no
            """.format(
                access_sql=_oracle_access_predicate_sql(alias="d"),
                name_order=_oracle_natural_name_order_sql("d.file_name"),
            ),
            _with_tenant_bind({"selected_ids": json.dumps(list(dict.fromkeys(document_ids)))}),
        )

    async def evaluation_corpus_rows(
        self, knowledge_base_ids: Sequence[str]
    ) -> list[dict[str, object]]:
        """評価の条件（#59）に残す、検索に出る chunk_set と、その内容の版を返す（読み取りだけ）。

        利用者の範囲（tenant・文書・KB）の中の配信中（is_serving）の chunk_set ごとに、レシピの版・
        抽出の元の hash と更新時刻・chunk と vector の件数・更新時刻を返す。``knowledge_base_ids``
        を渡すとその KB の文書だけ（空なら範囲の全部）。削除・権限の剥奪・active の切替・抽出の
        更新で行が変わるため、評価の前後で比べて実行中の更新を検出できる。
        """
        ids = [kb_id for kb_id in dict.fromkeys(knowledge_base_ids) if kb_id][:200]
        binds: dict[str, object] = {f"corpus_kb_{index}": kb_id for index, kb_id in enumerate(ids)}
        kb_sql = ""
        if ids:
            placeholders = ", ".join(f":corpus_kb_{index}" for index in range(len(ids)))
            kb_sql = (
                "AND d.document_id IN (SELECT dkb.document_id "
                "FROM rag_document_knowledge_bases dkb "
                f"WHERE dkb.knowledge_base_id IN ({placeholders}))"
            )
        return await self._fetch_all(
            f"""
            SELECT
                d.document_id, d.status AS document_status, cs.chunk_set_id, cs.recipe_id,
                cs.extraction_recipe_id, cs.language_pack_ingest_hash,
                cs.status AS chunk_set_status, cs.chunk_count, cs.vector_count,
                cs.updated_at AS chunk_set_updated_at,
                r.config_revision, r.materialized_revision,
                e.source_sha256, e.updated_at AS extraction_updated_at
            FROM rag_documents d
            JOIN rag_chunk_sets cs ON cs.document_id = d.document_id AND cs.is_serving = 1
            LEFT JOIN rag_document_recipes r ON r.recipe_id = cs.recipe_id
            LEFT JOIN rag_document_extractions e
              ON e.document_id = cs.document_id AND e.extraction_recipe_id = cs.extraction_recipe_id
            WHERE {_oracle_access_predicate_sql(alias="d")} {kb_sql}
            ORDER BY d.document_id, cs.chunk_set_id
            """,  # nosec B608 - 値はすべて bind。組み立てるのは bind の名前と固定の条件だけ。
            _with_tenant_bind(binds),
        )

    async def list_document_recipes(self, document_id: str) -> list[dict[str, object]]:
        """文書レシピを active chunk_set の件数とともに返す(読み取りだけ。書き込まない)。

        レシピ行が 1 件も無い既存の文書(migration 前に登録され、ジョブも未投入)は、文書の状態から
        レシピ1を仮の行として返す。行は、最初の書き込み(設定の保存・ジョブの投入など)で作る。
        """
        rows = await self._fetch_all(
            """
            SELECT
                r.recipe_id, r.document_id, r.slot_no, r.processing_config, r.status,
                r.failed_phase, r.preprocess_artifact, r.active_extraction_recipe_id,
                r.config_revision, r.materialized_revision, r.error_message,
                r.started_at, r.finished_at, r.created_at, r.updated_at,
                cs.chunk_set_id AS active_chunk_set_id,
                NVL(cs.chunk_count, 0) AS chunk_count,
                NVL(cs.vector_count, 0) AS vector_count,
                cs.status AS chunk_set_status,
                (
                    SELECT p.chunk_set_id FROM rag_chunk_sets p
                    WHERE p.recipe_id = r.recipe_id AND p.status = 'CHUNKED' AND p.is_active = 0
                    ORDER BY p.updated_at DESC, p.chunk_set_id DESC
                    FETCH FIRST 1 ROWS ONLY
                ) AS pending_chunk_set_id
            FROM rag_document_recipes r
            LEFT JOIN rag_chunk_sets cs
              ON cs.recipe_id = r.recipe_id AND cs.is_active = 1
            JOIN rag_documents d ON d.document_id = r.document_id
            WHERE r.document_id = :document_id
              AND {document_access_sql}
            ORDER BY r.slot_no
            """.format(document_access_sql=_oracle_access_predicate_sql(alias="d")),
            _with_tenant_bind({"document_id": document_id}),
        )
        if rows:
            return [_document_recipe_row(row) for row in rows]
        virtual = await self._virtual_default_document_recipe(document_id)
        if virtual is None:
            raise KeyError(f"document_id={document_id} は存在しません。")
        return [virtual]

    async def _virtual_default_document_recipe(self, document_id: str) -> dict[str, object] | None:
        """レシピ行が 1 件も無い文書のレシピ1を、文書の状態から組み立てる(書き込まない)。

        文書が無い・見えない、またはレシピ行がある(レシピ1を削除して他のレシピだけがある)
        ときは None。抽出結果などの大きな JSON 列は読まない。
        """
        row = await self._fetch_one(
            _render_sql(
                """
                SELECT
                    d.document_id, d.tenant_id_hash, d.status, d.processing_config,
                    d.preprocess_artifact, d.error_message, d.uploaded_at, d.indexed_at,
                    (
                        SELECT COUNT(*) FROM rag_document_recipes r
                        WHERE r.document_id = d.document_id
                    ) AS recipe_count
                FROM rag_documents d
                WHERE d.document_id = :document_id
                  AND {access_predicate}
                """,
                access_predicate=_oracle_access_predicate_sql(alias="d"),
            ),
            _with_tenant_bind({"document_id": document_id}),
        )
        if row is None or _int_value(row.get("recipe_count")) > 0:
            return None
        return _document_recipe_row(_default_document_recipe_values(row))

    async def get_document_recipe(
        self, document_id: str, recipe_id: str
    ) -> dict[str, object] | None:
        """文書内の指定レシピを返す。"""
        row = await self._fetch_one(
            """
            SELECT
                r.recipe_id, r.document_id, r.slot_no, r.processing_config, r.status,
                r.failed_phase, r.preprocess_artifact, r.active_extraction_recipe_id,
                r.config_revision, r.materialized_revision, r.error_message,
                r.started_at, r.finished_at, r.created_at, r.updated_at,
                cs.chunk_set_id AS active_chunk_set_id,
                NVL(cs.chunk_count, 0) AS chunk_count,
                NVL(cs.vector_count, 0) AS vector_count,
                cs.status AS chunk_set_status,
                (
                    SELECT p.chunk_set_id FROM rag_chunk_sets p
                    WHERE p.recipe_id = r.recipe_id AND p.status = 'CHUNKED' AND p.is_active = 0
                    ORDER BY p.updated_at DESC, p.chunk_set_id DESC
                    FETCH FIRST 1 ROWS ONLY
                ) AS pending_chunk_set_id
            FROM rag_document_recipes r
            LEFT JOIN rag_chunk_sets cs
              ON cs.recipe_id = r.recipe_id AND cs.is_active = 1
            JOIN rag_documents d ON d.document_id = r.document_id
            WHERE r.document_id = :document_id
              AND r.recipe_id = :recipe_id
              AND {document_access_sql}
            """.format(document_access_sql=_oracle_access_predicate_sql(alias="d")),
            _with_tenant_bind({"document_id": document_id, "recipe_id": recipe_id}),
        )
        if row is not None:
            return _document_recipe_row(row)
        if recipe_id != default_document_recipe_id(document_id):
            return None
        # レシピ行の無い既存の文書のレシピ1(読み取りでは作らない。#341)。
        return await self._virtual_default_document_recipe(document_id)

    async def get_latest_recipe_chunk_set(
        self,
        recipe_id: str,
        *,
        status: str | None = None,
        active: bool | None = None,
    ) -> dict[str, object] | None:
        """レシピの最新 chunk_set を工程再開用に返す。"""
        clauses = ["recipe_id = :recipe_id"]
        binds: dict[str, object] = {"recipe_id": recipe_id}
        if status is not None:
            clauses.append("status = :status")
            binds["status"] = status
        if active is not None:
            clauses.append("is_active = :is_active")
            binds["is_active"] = 1 if active else 0
        return await self._fetch_one(
            f"""
            SELECT chunk_set_id, document_id, recipe_id, extraction_recipe_id,
                   recipe_subset, status, chunk_count, vector_count, is_active,
                   created_at, updated_at, language_pack_release_id, language_pack_ingest_hash
            FROM rag_chunk_sets
            WHERE {" AND ".join(clauses)}
            ORDER BY updated_at DESC, chunk_set_id DESC
            FETCH FIRST 1 ROWS ONLY
            """,
            binds,
        )

    async def create_document_recipe(
        self, document_id: str, *, copy_from_recipe_id: str | None = None
    ) -> dict[str, object]:
        """親文書をロックして最大3件を保証し、新しい空き slot にレシピを作る。"""
        await self.ensure_default_document_recipe(document_id)

        def operation(connection: OracleConnectionProtocol) -> dict[str, object]:
            document = _select_document_for_update(connection, document_id)
            if document is None:
                raise KeyError(f"document_id={document_id} は存在しません。")
            rows = _fetch_all(
                connection,
                """
                SELECT recipe_id, slot_no, processing_config
                FROM rag_document_recipes
                WHERE document_id = :document_id
                ORDER BY slot_no
                """,
                {"document_id": document_id},
            )
            if len(rows) >= 3:
                raise ValueError("レシピは最大3件です。")
            used = {_int_value(row.get("slot_no")) for row in rows}
            slot_no = next(slot for slot in (1, 2, 3) if slot not in used)
            processing_config: dict[str, object] = {}
            if copy_from_recipe_id is not None:
                source = next(
                    (row for row in rows if str(row.get("recipe_id")) == copy_from_recipe_id),
                    None,
                )
                if source is None:
                    raise KeyError("複製元レシピが見つかりません。")
                processing_config = _json_loads(source.get("processing_config"))
            now = datetime.now(UTC)
            recipe_id = uuid4().hex
            _execute(
                connection,
                """
                INSERT INTO rag_document_recipes (
                    recipe_id, document_id, slot_no, tenant_id_hash, processing_config,
                    status, config_revision, created_at, updated_at
                ) VALUES (
                    :recipe_id, :document_id, :slot_no, :tenant_id_hash, :processing_config,
                    'UPLOADED', 1, :created_at, :updated_at
                )
                """,
                {
                    "recipe_id": recipe_id,
                    "document_id": document_id,
                    "slot_no": slot_no,
                    "tenant_id_hash": _current_tenant_id_hash(),
                    "processing_config": _json_bind(processing_config),
                    "created_at": now,
                    "updated_at": now,
                },
                input_sizes=_json_input_sizes("processing_config"),
            )
            row = _fetch_one(
                connection,
                "SELECT * FROM rag_document_recipes WHERE recipe_id = :recipe_id",
                {"recipe_id": recipe_id},
            )
            assert row is not None
            return _document_recipe_row(row)

        return await self._run_transaction(operation)

    async def update_document_recipe_config(
        self,
        document_id: str,
        recipe_id: str,
        config: DocumentProcessingConfig,
    ) -> dict[str, object]:
        """活動中 job が無いレシピの明示設定を保存し revision を進める。"""

        def operation(connection: OracleConnectionProtocol) -> dict[str, object]:
            _ensure_recipe_row_if_default(connection, document_id, recipe_id)
            recipe = _select_document_recipe_for_update(connection, document_id, recipe_id)
            if recipe is None:
                raise KeyError("レシピが見つかりません。")
            active = _fetch_one(
                connection,
                """
                SELECT job_id FROM rag_ingestion_jobs
                WHERE recipe_id = :recipe_id
                  AND document_id = :document_id
                  AND status IN ('QUEUED', 'RUNNING')
                FETCH FIRST 1 ROWS ONLY
                """,
                {"recipe_id": recipe_id, "document_id": document_id},
            )
            if active is not None:
                raise ValueError("処理中または待機中のレシピは編集できません。")
            _execute(
                connection,
                """
                UPDATE rag_document_recipes
                SET processing_config = :processing_config,
                    config_revision = config_revision + 1,
                    error_message = NULL,
                    failed_phase = NULL,
                    updated_at = SYSTIMESTAMP
                WHERE recipe_id = :recipe_id AND document_id = :document_id
                """,
                {
                    "recipe_id": recipe_id,
                    "document_id": document_id,
                    "processing_config": _json_bind(
                        config.model_dump(mode="json", exclude_none=True)
                    ),
                },
                input_sizes=_json_input_sizes("processing_config"),
            )
            row = _fetch_one(
                connection,
                "SELECT * FROM rag_document_recipes WHERE recipe_id = :recipe_id",
                {"recipe_id": recipe_id},
            )
            if row is None:
                raise KeyError("レシピが見つかりません。")
            return _document_recipe_row(row)

        return await self._run_transaction(operation)

    async def delete_document_recipe(self, document_id: str, recipe_id: str) -> int:
        """最後の1件と活動中 job を保護し、レシピ固有の chunk を削除する。"""

        def operation(connection: OracleConnectionProtocol) -> int:
            document = _select_document_for_update(connection, document_id)
            if document is None:
                raise KeyError("ドキュメントが見つかりません。")
            _ensure_recipe_row_if_default(connection, document_id, recipe_id)
            if _select_document_recipe_for_update(connection, document_id, recipe_id) is None:
                raise KeyError("レシピが見つかりません。")
            count_row = _fetch_one(
                connection,
                "SELECT COUNT(*) AS cnt FROM rag_document_recipes WHERE document_id = :document_id",
                {"document_id": document_id},
            )
            if _int_value((count_row or {}).get("cnt")) <= 1:
                raise ValueError("レシピは少なくとも1件必要です。")
            active = _fetch_one(
                connection,
                """
                SELECT job_id FROM rag_ingestion_jobs
                WHERE recipe_id = :recipe_id
                  AND document_id = :document_id
                  AND status IN ('QUEUED', 'RUNNING')
                FETCH FIRST 1 ROWS ONLY
                """,
                {"recipe_id": recipe_id, "document_id": document_id},
            )
            if active is not None:
                raise ValueError("処理中または待機中のレシピは削除できません。")
            count_chunks = _fetch_one(
                connection,
                "SELECT COUNT(*) AS cnt FROM rag_chunk_sets WHERE recipe_id = :recipe_id",
                {"recipe_id": recipe_id},
            )
            stale_sets = _fetch_all(
                connection,
                "SELECT chunk_set_id FROM rag_chunk_sets WHERE recipe_id = :recipe_id",
                {"recipe_id": recipe_id},
            )
            for stale_set in stale_sets:
                stale_chunk_set_id = str(stale_set["chunk_set_id"])
                entity_rows = _fetch_all(
                    connection,
                    "SELECT entity_id FROM rag_graph_entities WHERE chunk_set_id = :chunk_set_id",
                    {"chunk_set_id": stale_chunk_set_id},
                )
                _delete_graph_rows_for_chunk_set(
                    connection,
                    chunk_set_id=stale_chunk_set_id,
                    entity_ids=[str(row["entity_id"]) for row in entity_rows],
                )
            _execute(
                connection,
                """
                DELETE FROM rag_chunks
                WHERE chunk_set_id IN (
                    SELECT chunk_set_id FROM rag_chunk_sets WHERE recipe_id = :recipe_id
                )
                """,
                {"recipe_id": recipe_id},
            )
            _execute(
                connection,
                "DELETE FROM rag_chunk_sets WHERE recipe_id = :recipe_id",
                {"recipe_id": recipe_id},
            )
            _execute(
                connection,
                """
                DELETE FROM rag_document_recipes
                WHERE recipe_id = :recipe_id AND document_id = :document_id
                """,
                {"recipe_id": recipe_id, "document_id": document_id},
            )
            return _int_value((count_chunks or {}).get("cnt"))

        return await self._run_transaction(operation)

    async def update_document_recipe_status(
        self,
        *,
        recipe_id: str,
        status: FileStatus,
        failed_phase: IngestionJobPhase | None = None,
        error_message: str | None = None,
        preprocess_artifact: DocumentPreprocessArtifact | None = None,
        active_extraction_recipe_id: str | None = None,
    ) -> None:
        """レシピ単位の工程状態と成果物ポインタを更新する。"""
        updates = {
            "recipe_id": recipe_id,
            "status": status.value,
            "failed_phase": failed_phase.value if failed_phase is not None else None,
            "error_message": error_message,
            "preprocess_artifact": (
                _json_bind(preprocess_artifact.model_dump(mode="json"))
                if preprocess_artifact is not None
                else None
            ),
            "active_extraction_recipe_id": active_extraction_recipe_id,
        }

        def operation(connection: OracleConnectionProtocol) -> None:
            _execute(
                connection,
                """
                UPDATE rag_document_recipes
                SET status = :status,
                    failed_phase = :failed_phase,
                    error_message = :error_message,
                    preprocess_artifact = COALESCE(JSON(:preprocess_artifact), preprocess_artifact),
                    active_extraction_recipe_id = COALESCE(
                        :active_extraction_recipe_id, active_extraction_recipe_id
                    ),
                    started_at = CASE
                        WHEN :status IN ('PREPROCESSING', 'INGESTING', 'CHUNKING', 'INDEXING')
                        THEN SYSTIMESTAMP ELSE started_at
                    END,
                    finished_at = CASE
                        WHEN :status IN ('INDEXED', 'ERROR') THEN SYSTIMESTAMP ELSE NULL
                    END,
                    updated_at = SYSTIMESTAMP
                WHERE recipe_id = :recipe_id
                """,
                updates,
                input_sizes=_json_input_sizes("preprocess_artifact"),
            )

        await self._run_transaction(operation)

    async def activate_recipe_chunk_set(
        self,
        *,
        recipe_id: str,
        chunk_set_id: str,
        extraction_recipe_id: str | None = None,
        materialized_revision: int | None = None,
    ) -> None:
        """成功出力を active に原子切替し、同レシピの旧出力を GC する。"""

        def operation(connection: OracleConnectionProtocol) -> None:
            locked_recipe = _fetch_one(
                connection,
                """
                SELECT recipe_id FROM rag_document_recipes
                WHERE recipe_id = :recipe_id
                FOR UPDATE
                """,
                {"recipe_id": recipe_id},
            )
            if locked_recipe is None:
                raise KeyError("レシピが見つかりません。")
            selected = _fetch_one(
                connection,
                """
                SELECT chunk_set_id FROM rag_chunk_sets
                WHERE chunk_set_id = :chunk_set_id AND status = 'INDEXED'
                """,
                {"chunk_set_id": chunk_set_id},
            )
            if selected is None:
                raise ValueError("索引済みの出力のみ active にできます。")
            _execute(
                connection,
                """
                UPDATE rag_chunk_sets
                SET recipe_id = :recipe_id, updated_at = SYSTIMESTAMP
                WHERE chunk_set_id = :chunk_set_id
                """,
                {"recipe_id": recipe_id, "chunk_set_id": chunk_set_id},
            )
            _execute(
                connection,
                """
                UPDATE rag_chunk_sets
                SET is_active = CASE WHEN chunk_set_id = :chunk_set_id THEN 1 ELSE 0 END,
                    updated_at = SYSTIMESTAMP
                WHERE recipe_id = :recipe_id
                """,
                {"recipe_id": recipe_id, "chunk_set_id": chunk_set_id},
            )
            # None の bind は VARCHAR2 として送られ、NUMBER 列との COALESCE が ORA-00932 に
            # なるため NUMBER へ CAST する。省略時は config_revision を記録する(#568)。
            _execute(
                connection,
                """
                UPDATE rag_document_recipes
                SET status = 'INDEXED',
                    failed_phase = NULL,
                    error_message = NULL,
                    active_extraction_recipe_id = COALESCE(
                        :extraction_recipe_id, active_extraction_recipe_id
                    ),
                    materialized_revision = COALESCE(
                        CAST(:materialized_revision AS NUMBER), config_revision
                    ),
                    finished_at = SYSTIMESTAMP,
                    updated_at = SYSTIMESTAMP
                WHERE recipe_id = :recipe_id
                """,
                {
                    "recipe_id": recipe_id,
                    "extraction_recipe_id": extraction_recipe_id,
                    "materialized_revision": materialized_revision,
                },
            )
            stale_sets = _fetch_all(
                connection,
                """
                SELECT chunk_set_id FROM rag_chunk_sets
                WHERE recipe_id = :recipe_id AND chunk_set_id <> :chunk_set_id
                """,
                {"recipe_id": recipe_id, "chunk_set_id": chunk_set_id},
            )
            for stale_set in stale_sets:
                stale_chunk_set_id = str(stale_set["chunk_set_id"])
                entity_rows = _fetch_all(
                    connection,
                    "SELECT entity_id FROM rag_graph_entities WHERE chunk_set_id = :chunk_set_id",
                    {"chunk_set_id": stale_chunk_set_id},
                )
                _delete_graph_rows_for_chunk_set(
                    connection,
                    chunk_set_id=stale_chunk_set_id,
                    entity_ids=[str(row["entity_id"]) for row in entity_rows],
                )
            _execute(
                connection,
                """
                DELETE FROM rag_chunks
                WHERE chunk_set_id IN (
                    SELECT chunk_set_id FROM rag_chunk_sets
                    WHERE recipe_id = :recipe_id AND chunk_set_id <> :chunk_set_id
                )
                """,
                {"recipe_id": recipe_id, "chunk_set_id": chunk_set_id},
            )
            _execute(
                connection,
                """
                DELETE FROM rag_chunk_sets
                WHERE recipe_id = :recipe_id AND chunk_set_id <> :chunk_set_id
                """,
                {"recipe_id": recipe_id, "chunk_set_id": chunk_set_id},
            )

        await self._run_transaction(operation)

    async def delete_document_extractions_except(
        self, *, document_id: str, keep_extraction_ids: Sequence[str]
    ) -> list[str]:
        """plan に無い extraction を削除する(GC)。keep が空なら何もしない(安全側)。

        chunk_set GC を先に走らせた後に呼ぶ前提(参照されない抽出だけ残る)。削除した id を返す。
        """
        keep = list(dict.fromkeys(keep_extraction_ids))
        if not keep:
            return []
        keep_in_sql, keep_binds = _oracle_in_predicate("extraction_recipe_id", "keep_ex", keep)
        binds: dict[str, object] = {"document_id": document_id, **keep_binds}

        def operation(connection: OracleConnectionProtocol) -> list[str]:
            rows = _fetch_all(
                connection,
                _render_sql(
                    """
                    SELECT extraction_recipe_id FROM rag_document_extractions
                    WHERE document_id = :document_id AND NOT ({keep_in_sql})
                    """,
                    keep_in_sql=keep_in_sql,
                ),
                binds,
            )
            removed = [str(next(iter(row.values()))) for row in rows]
            _execute(
                connection,
                _render_sql(
                    """
                    DELETE FROM rag_document_extractions
                    WHERE document_id = :document_id AND NOT ({keep_in_sql})
                    """,
                    keep_in_sql=keep_in_sql,
                ),
                binds,
            )
            return removed

        return await self._run_transaction(operation)

    async def upsert_chunk_set(
        self,
        *,
        chunk_set_id: str,
        document_id: str,
        recipe_id: str | None = None,
        extraction_recipe_id: str | None = None,
        recipe_subset: Mapping[str, object] | None = None,
        status: str = "INGESTING",
    ) -> None:
        """chunk_set(chunk text/embedding 層)を冪等に作成する。既存なら updated_at のみ更新。

        ``extraction_recipe_id`` を渡すと親抽出 recipe への所属を記録する。
        """
        release = active_release()
        tenant = current_audit_request_context().tenant_id_hash
        binds = {
            "chunk_set_id": chunk_set_id,
            "document_id": document_id,
            "recipe_id": recipe_id,
            "extraction_recipe_id": extraction_recipe_id,
            "tenant_id_hash": tenant,
            "recipe_subset": _json_bind(recipe_subset),
            "status": status,
            "language_pack_release_id": release.id if release else None,
            "language_pack_ingest_hash": release.ingest_hash if release else None,
            "language_pack_job_id": ingestion_release_job_id(),
        }

        def operation(connection: OracleConnectionProtocol) -> None:
            _execute(
                connection,
                """
                MERGE INTO rag_chunk_sets t
                USING (SELECT :chunk_set_id AS chunk_set_id FROM dual) s
                ON (t.chunk_set_id = s.chunk_set_id)
                WHEN MATCHED THEN UPDATE SET
                    t.recipe_id = COALESCE(:recipe_id, t.recipe_id),
                    t.extraction_recipe_id =
                        COALESCE(:extraction_recipe_id, t.extraction_recipe_id),
                    t.recipe_subset = COALESCE(JSON(:recipe_subset), t.recipe_subset),
                    t.updated_at = SYSTIMESTAMP
                WHEN NOT MATCHED THEN INSERT
                    (chunk_set_id, document_id, recipe_id, extraction_recipe_id, tenant_id_hash,
                     recipe_subset, status, language_pack_release_id,
                     language_pack_ingest_hash, language_pack_job_id)
                    VALUES (
                        :chunk_set_id, :document_id, :recipe_id, :extraction_recipe_id,
                        :tenant_id_hash, :recipe_subset, :status, :language_pack_release_id,
                        :language_pack_ingest_hash, :language_pack_job_id
                    )
                """,
                binds,
                input_sizes=_json_input_sizes("recipe_subset"),
            )

        await self._run_transaction(operation)

    async def mark_chunk_set_indexed(
        self,
        *,
        chunk_set_id: str,
        chunk_count: int,
        vector_count: int,
        metrics: Mapping[str, object] | None = None,
    ) -> None:
        """chunk_set を INDEXED にし、件数/metrics を記録する。"""
        binds = {
            "chunk_set_id": chunk_set_id,
            "chunk_count": chunk_count,
            "vector_count": vector_count,
            "metrics_json": _json_dumps(metrics) if metrics is not None else None,
        }

        def operation(connection: OracleConnectionProtocol) -> None:
            _execute(
                connection,
                """
                UPDATE rag_chunk_sets
                SET status = 'INDEXED',
                    language_pack_built_at = SYSTIMESTAMP,
                    chunk_count = :chunk_count,
                    vector_count = :vector_count,
                    metrics_json = :metrics_json,
                    updated_at = SYSTIMESTAMP
                WHERE chunk_set_id = :chunk_set_id
                """,
                binds,
            )

        await self._run_transaction(operation)

    async def set_document_serving_chunk_set(
        self,
        *,
        document_id: str,
        chunk_set_id: str,
    ) -> None:
        """文書の serving chunk_set を指定の 1 つに設定する(他は is_serving=0)。

        3 層モデル: serving は文書単位で 1 つ。retrieval はこの chunk_set だけを検索対象にする
        (Phase 3 の「別レシピで試す→昇格」も serving の付け替えで表現する)。
        """
        binds = {"document_id": document_id, "chunk_set_id": chunk_set_id}

        def operation(connection: OracleConnectionProtocol) -> None:
            _execute(
                connection,
                """
                UPDATE rag_chunk_sets
                SET is_serving = CASE WHEN chunk_set_id = :chunk_set_id THEN 1 ELSE 0 END,
                    updated_at = SYSTIMESTAMP
                WHERE document_id = :document_id
                """,
                binds,
            )

        await self._run_transaction(operation)

    async def mark_chunk_set_chunked(
        self,
        *,
        chunk_set_id: str,
        chunk_count: int,
        metrics: Mapping[str, object] | None = None,
    ) -> None:
        """chunk_set を CHUNKED にし、chunk 件数を記録する。"""
        binds = {
            "chunk_set_id": chunk_set_id,
            "chunk_count": chunk_count,
            "metrics_json": _json_dumps(metrics) if metrics is not None else None,
        }

        def operation(connection: OracleConnectionProtocol) -> None:
            _execute(
                connection,
                """
                UPDATE rag_chunk_sets
                SET status = 'CHUNKED',
                    chunk_count = :chunk_count,
                    vector_count = 0,
                    metrics_json = :metrics_json,
                    updated_at = SYSTIMESTAMP
                WHERE chunk_set_id = :chunk_set_id
                """,
                binds,
            )

        await self._run_transaction(operation)

    async def get_chunk_set(self, chunk_set_id: str) -> dict[str, object] | None:
        """chunk_set の状態(status/件数/親 extraction_id/is_serving)を返す。キーは小文字化。"""
        row = await self._fetch_one(
            """
            SELECT chunk_set_id, document_id, recipe_id, extraction_recipe_id, recipe_subset,
                   status, chunk_count, vector_count, is_serving, is_active
            FROM rag_chunk_sets WHERE chunk_set_id = :chunk_set_id
            """,
            {"chunk_set_id": chunk_set_id},
        )
        if row is None:
            return None
        normalized = {str(key).lower(): value for key, value in row.items()}
        normalized["recipe_subset"] = _json_loads(normalized.get("recipe_subset"))
        return normalized

    async def upsert_document_extraction_artifact(
        self,
        *,
        document_id: str,
        extraction_recipe_id: str,
        source_sha256: str | None,
        recipe_subset: Mapping[str, object] | None = None,
        extraction: Mapping[str, object] | None = None,
        status: str = "planned_only",
        reason: str | None = None,
        metrics: Mapping[str, object] | None = None,
    ) -> None:
        """extraction recipe 単位の抽出 artifact 状態を冪等に保存する。"""
        if extraction is not None and (release := active_release()):
            saved_artifacts = extraction.get("parser_artifacts")
            artifacts = dict(saved_artifacts) if isinstance(saved_artifacts, Mapping) else {}
            artifacts["language_pack_release_id"] = release.id
            artifacts["language_pack_ingest_hash"] = release.ingest_hash
            extraction = {**extraction, "parser_artifacts": artifacts}
        tenant = current_audit_request_context().tenant_id_hash
        binds = {
            "document_id": document_id,
            "extraction_recipe_id": extraction_recipe_id,
            "source_sha256": source_sha256,
            "tenant_id_hash": tenant,
            "recipe_subset": _json_bind(recipe_subset),
            "extraction_json": _json_bind(extraction),
            "status": status,
            "reason": reason,
            "metrics_json": _json_bind(metrics),
        }

        def operation(connection: OracleConnectionProtocol) -> None:
            _merge_document_extraction_artifact(
                connection, binds, update_extraction=extraction is not None
            )

        await self._run_transaction(operation)

    async def get_document_extraction_artifact(
        self,
        *,
        document_id: str,
        extraction_recipe_id: str,
    ) -> dict[str, object] | None:
        """指定 extraction recipe の永続状態を返す。"""
        row = await self._fetch_one(
            """
            SELECT document_id, extraction_recipe_id, source_sha256, status, reason,
                   recipe_subset, extraction_json, metrics_json
            FROM rag_document_extractions
            WHERE document_id = :document_id
              AND extraction_recipe_id = :extraction_recipe_id
            """,
            {"document_id": document_id, "extraction_recipe_id": extraction_recipe_id},
        )
        if row is None:
            return None
        normalized = {str(key).lower(): value for key, value in row.items()}
        normalized["recipe_subset"] = _json_loads(normalized.get("recipe_subset"))
        normalized["extraction_json"] = _json_loads(normalized.get("extraction_json"))
        normalized["metrics_json"] = _json_loads(normalized.get("metrics_json"))
        return normalized

    async def save_document_recipe_review_extraction(
        self,
        *,
        document_id: str,
        recipe_id: str,
        build: Callable[[Mapping[str, object], Mapping[str, object]], ReviewedExtractionWrite],
    ) -> str:
        """抽出確認の修正を、レシピの行ロックの下で最新の抽出成果へ当てて保存する(#2616)。

        レシピの行を ``FOR UPDATE`` で押さえてから、レシピの状態と今の抽出成果を読み、
        ``build(レシピの行, 抽出成果の行)`` が作った成果を書き、レシピの成果の参照を切り替える。
        同じレシピの保存・承認・job の投入はこの行ロックで直列になるため、読んだ後に別の保存が
        入って先の修正を上書きすることはない(修正は要素・表セルの差分として最新の成果へ当てる)。
        確認待ちでない・処理中・抽出結果が無いときは ``DocumentReviewEditConflictError``。
        ``build`` の例外は transaction を rollback してそのまま返す。書いた成果の ID を返す。
        """
        tenant = current_audit_request_context().tenant_id_hash

        def operation(connection: OracleConnectionProtocol) -> str:
            _ensure_recipe_row_if_default(connection, document_id, recipe_id)
            locked = _select_document_recipe_for_update(connection, document_id, recipe_id)
            if locked is None:
                raise KeyError("レシピが見つかりません。")
            recipe = _document_recipe_row(locked)
            if recipe["status"] != FileStatus.REVIEW.value:
                raise DocumentReviewEditConflictError("確認待ちのレシピのみ修正できます。")
            active_job = _fetch_one(
                connection,
                """
                SELECT job_id FROM rag_ingestion_jobs
                WHERE recipe_id = :recipe_id
                  AND document_id = :document_id
                  AND status IN ('QUEUED', 'RUNNING')
                FETCH FIRST 1 ROWS ONLY
                """,
                {"recipe_id": recipe_id, "document_id": document_id},
            )
            if active_job is not None:
                raise DocumentReviewEditConflictError("このレシピは処理中または待機中です。")
            source_extraction_recipe_id = recipe["active_extraction_recipe_id"]
            if source_extraction_recipe_id is None:
                raise DocumentReviewEditConflictError("修正対象の抽出結果がありません。")
            artifact_row = _fetch_one(
                connection,
                """
                SELECT document_id, extraction_recipe_id, source_sha256, status, reason,
                       recipe_subset, extraction_json, metrics_json
                FROM rag_document_extractions
                WHERE document_id = :document_id
                  AND extraction_recipe_id = :extraction_recipe_id
                """,
                {
                    "document_id": document_id,
                    "extraction_recipe_id": source_extraction_recipe_id,
                },
            )
            artifact = (
                {str(key).lower(): value for key, value in artifact_row.items()}
                if artifact_row is not None
                else {}
            )
            artifact["recipe_subset"] = _json_loads(artifact.get("recipe_subset"))
            artifact["extraction_json"] = _json_loads(artifact.get("extraction_json"))
            artifact["metrics_json"] = _json_loads(artifact.get("metrics_json"))
            if artifact_row is None or not artifact["extraction_json"]:
                raise DocumentReviewEditConflictError("修正対象の抽出結果がありません。")
            write = build(recipe, artifact)
            _merge_document_extraction_artifact(
                connection,
                {
                    "document_id": document_id,
                    "extraction_recipe_id": write.extraction_recipe_id,
                    "source_sha256": write.source_sha256,
                    "tenant_id_hash": tenant,
                    "recipe_subset": _json_bind(write.recipe_subset),
                    "extraction_json": _json_bind(write.extraction),
                    "status": write.status,
                    "reason": None,
                    "metrics_json": _json_bind(None),
                },
                update_extraction=True,
            )
            _execute(
                connection,
                """
                UPDATE rag_document_recipes
                SET active_extraction_recipe_id = :extraction_recipe_id,
                    failed_phase = NULL,
                    error_message = NULL,
                    finished_at = NULL,
                    updated_at = SYSTIMESTAMP
                WHERE recipe_id = :recipe_id
                """,
                {"recipe_id": recipe_id, "extraction_recipe_id": write.extraction_recipe_id},
            )
            return write.extraction_recipe_id

        return await self._run_transaction(operation)

    async def upsert_artifact_layer(
        self,
        *,
        layer_id: str,
        layer_kind: str,
        parent_chunk_set_id: str,
        document_id: str,
        requested: bool,
        status: str,
        reason: str | None = None,
        metrics: Mapping[str, object] | None = None,
        input_fingerprint: Mapping[str, object] | None = None,
    ) -> None:
        """chunk_set 派生 layer の実体化状態と、作ったときの入力の指紋(#550)を保存する。"""
        tenant = current_audit_request_context().tenant_id_hash
        binds = {
            "layer_id": layer_id,
            "layer_kind": layer_kind,
            "parent_chunk_set_id": parent_chunk_set_id,
            "document_id": document_id,
            "tenant_id_hash": tenant,
            "requested": 1 if requested else 0,
            "status": status,
            "reason": reason,
            "metrics_json": _json_dumps(metrics) if metrics is not None else None,
            "input_fingerprint": (
                _json_dumps(input_fingerprint) if input_fingerprint is not None else None
            ),
        }

        def operation(connection: OracleConnectionProtocol) -> None:
            _execute(
                connection,
                """
                MERGE INTO rag_artifact_layers t
                USING (SELECT :layer_id AS layer_id FROM dual) s
                ON (t.layer_id = s.layer_id)
                WHEN MATCHED THEN UPDATE SET
                    t.layer_kind = :layer_kind,
                    t.parent_chunk_set_id = :parent_chunk_set_id,
                    t.document_id = :document_id,
                    t.requested = :requested,
                    t.status = :status,
                    t.reason = :reason,
                    t.metrics_json = :metrics_json,
                    t.input_fingerprint = :input_fingerprint,
                    t.updated_at = SYSTIMESTAMP
                WHEN NOT MATCHED THEN INSERT
                    (layer_id, layer_kind, parent_chunk_set_id, document_id, tenant_id_hash,
                     requested, status, reason, metrics_json, input_fingerprint)
                    VALUES
                    (:layer_id, :layer_kind, :parent_chunk_set_id, :document_id, :tenant_id_hash,
                     :requested, :status, :reason, :metrics_json, :input_fingerprint)
                """,
                binds,
            )

        await self._run_transaction(operation)

    async def list_artifact_layers_for_chunk_sets(
        self,
        chunk_set_ids: Sequence[str],
    ) -> dict[str, dict[str, object]]:
        """chunk_set 群に紐づく layer 状態を layer_id keyed で返す。"""
        ids = list(dict.fromkeys(chunk_set_ids))
        if not ids:
            return {}
        in_sql, binds = _oracle_in_predicate("parent_chunk_set_id", "cs", ids)
        rows = await self._fetch_all(
            _render_sql(
                """
                SELECT layer_id, layer_kind, parent_chunk_set_id, requested, status, reason,
                       metrics_json, input_fingerprint
                FROM rag_artifact_layers
                WHERE {in_sql}
                """,
                in_sql=in_sql,
            ),
            binds,
        )
        result: dict[str, dict[str, object]] = {}
        for row in rows:
            normalized = {str(key).lower(): value for key, value in row.items()}
            normalized["requested"] = int(str(normalized.get("requested") or 0)) == 1
            normalized["metrics_json"] = _json_loads(normalized.get("metrics_json"))
            normalized["input_fingerprint"] = _json_loads(normalized.get("input_fingerprint"))
            result[str(normalized["layer_id"])] = normalized
        return result

    async def list_serving_artifact_layer_fingerprints(
        self,
        document_ids: Sequence[str],
    ) -> list[dict[str, object]]:
        """文書の検索対象の chunk_set の派生 layer のうち、指紋のある行を返す(一覧の判定。#550)。

        検索対象はレシピの active chunk_set と、レシピを持たない(legacy の plan の)配信中の
        chunk_set。レシピの設定で層 ID を作り直して今も要求されているかを確かめるため、レシピ
        (無ければ文書)の処理設定も返す。抽出結果などの大きな JSON 列は読まない。
        """
        ids = list(dict.fromkeys(document_ids))
        if not ids:
            return []
        in_sql, binds = _oracle_in_predicate("l.document_id", "doc", ids)
        rows = await self._fetch_all(
            _render_sql(
                """
                SELECT l.document_id, l.layer_id, l.layer_kind, l.parent_chunk_set_id,
                       l.input_fingerprint, cs.recipe_id,
                       r.processing_config AS recipe_processing_config,
                       d.processing_config AS document_processing_config
                FROM rag_artifact_layers l
                JOIN rag_chunk_sets cs ON cs.chunk_set_id = l.parent_chunk_set_id
                JOIN rag_documents d ON d.document_id = l.document_id
                LEFT JOIN rag_document_recipes r ON r.recipe_id = cs.recipe_id
                WHERE {in_sql}
                  AND l.requested = 1
                  AND l.input_fingerprint IS NOT NULL
                  AND (cs.is_active = 1 OR (cs.recipe_id IS NULL AND cs.is_serving = 1))
                  AND {document_access_sql}
                """,
                in_sql=in_sql,
                document_access_sql=_oracle_access_predicate_sql(alias="d"),
            ),
            _with_tenant_bind(binds),
        )
        result: list[dict[str, object]] = []
        for row in rows:
            normalized = {str(key).lower(): value for key, value in row.items()}
            for key in (
                "input_fingerprint",
                "recipe_processing_config",
                "document_processing_config",
            ):
                normalized[key] = _json_loads(normalized.get(key))
            result.append(normalized)
        return result

    async def list_document_knowledge_base_configs(
        self, document_id: str
    ) -> list[tuple[str, KnowledgeBaseAdapterConfig]]:
        """文書の所属 KB id と各 adapter_config を返す(variant_planner の plan 入力)。"""
        rows = await self._fetch_all(
            """
            SELECT
                kb.knowledge_base_id,
                kb.retrieval_config
            FROM rag_document_knowledge_bases dkb
            JOIN rag_knowledge_bases kb
                ON kb.knowledge_base_id = dkb.knowledge_base_id
            JOIN rag_documents d
                ON d.document_id = dkb.document_id
            WHERE dkb.document_id = :document_id
              AND {document_access_sql}
              AND {knowledge_base_access_sql}
            ORDER BY kb.knowledge_base_id ASC
            """.format(
                document_access_sql=_oracle_access_predicate_sql(alias="d"),
                knowledge_base_access_sql=_oracle_knowledge_base_access_predicate_sql(alias="kb"),
            ),
            _with_tenant_bind({"document_id": document_id}),
        )
        return [
            (
                str(row["knowledge_base_id"]),
                parse_adapter_config(_json_loads(row.get("retrieval_config"))),
            )
            for row in rows
        ]

    async def get_document_serving_chunk_set_id(self, document_id: str) -> str | None:
        """文書の配信中(is_serving=1)chunk_set id を返す。無ければ None。

        昇格済みで recipe が global と乖離していても DB の is_serving を正本にする
        (実験時の候補 demote = 現 serving の再アサートに使う)。複数 serving の旧データは
        created_at 最新を返す(安全側で1つに収束させる)。
        """
        row = await self._fetch_one(
            """
            SELECT chunk_set_id FROM rag_chunk_sets
            WHERE document_id = :document_id AND is_serving = 1
            ORDER BY created_at DESC, chunk_set_id
            FETCH FIRST 1 ROWS ONLY
            """,
            {"document_id": document_id},
        )
        return str(next(iter(row.values()))) if row else None

    async def list_document_chunk_sets(self, document_id: str) -> list[dict[str, object]]:
        """文書の chunk_set 一覧(状態/件数/所属・配信 KB)を返す。variant 可視化に使う。

        3 層モデル: chunk_set は文書単位(KB 非依存)。所属 KB は文書の membership
        (rag_document_knowledge_bases)で全 chunk_set 共通、配信中かは cs.is_serving=1 で決まる。
        created_at 昇順で安定。
        """
        rows = await self._fetch_all(
            """
            SELECT cs.chunk_set_id AS chunk_set_id,
                   cs.recipe_id AS recipe_id,
                   cs.extraction_recipe_id AS extraction_recipe_id,
                   cs.status AS status,
                   cs.chunk_count AS chunk_count,
                   cs.vector_count AS vector_count,
                   cs.is_serving AS is_serving,
                   cs.is_active AS is_active,
                   cs.created_at AS created_at,
                   cs.language_pack_release_id, cs.language_pack_ingest_hash,
                   cs.language_pack_job_id, cs.language_pack_built_at,
                   ex.recipe_subset AS extraction_recipe
            FROM rag_chunk_sets cs
            LEFT JOIN rag_document_extractions ex
              ON ex.document_id = cs.document_id
             AND ex.extraction_recipe_id = cs.extraction_recipe_id
            WHERE cs.document_id = :document_id
            ORDER BY cs.created_at, cs.chunk_set_id
            """,
            {"document_id": document_id},
        )
        member_kb_ids = [kb.id for kb in await self.list_document_knowledge_bases(document_id)]
        result: list[dict[str, object]] = []
        for row in rows:
            norm = {str(key).lower(): value for key, value in row.items()}
            recipe = _json_loads(norm.get("extraction_recipe"))
            is_serving = int(str(norm.get("is_serving") or 0)) == 1
            result.append(
                {
                    "chunk_set_id": str(norm["chunk_set_id"]),
                    "recipe_id": (
                        str(norm["recipe_id"]) if norm.get("recipe_id") is not None else None
                    ),
                    "extraction_recipe_id": (
                        str(norm["extraction_recipe_id"])
                        if norm.get("extraction_recipe_id") is not None
                        else None
                    ),
                    "status": str(norm["status"]),
                    "chunk_count": int(str(norm["chunk_count"] or 0)),
                    "vector_count": int(str(norm["vector_count"] or 0)),
                    "is_serving": is_serving,
                    "is_active": int(str(norm.get("is_active") or 0)) == 1,
                    "created_at": norm.get("created_at"),
                    "language_pack_release_id": norm.get("language_pack_release_id"),
                    "language_pack_ingest_hash": norm.get("language_pack_ingest_hash"),
                    "language_pack_job_id": norm.get("language_pack_job_id"),
                    "language_pack_built_at": norm.get("language_pack_built_at"),
                    "extraction_id": None,
                    "parser": recipe.get("rag_parser_adapter_backend"),
                    "preprocess": recipe.get("rag_preprocess_profile"),
                    "knowledge_base_ids": list(member_kb_ids),
                    "serving_knowledge_base_ids": list(member_kb_ids) if is_serving else [],
                }
            )
        return result

    async def clone_recipe_chunk_set_chunks(
        self, *, recipe_id: str, source_chunk_set_id: str, target_chunk_set_id: str
    ) -> None:
        """有効なレシピの Chunk を別候補へ複製し、embedding を作り直す。"""
        binds = _with_tenant_bind(
            {
                "recipe_id": recipe_id,
                "source": source_chunk_set_id,
                "target": target_chunk_set_id,
            }
        )

        def operation(connection: OracleConnectionProtocol) -> None:
            _execute(
                connection,
                _render_sql(
                    """
                INSERT INTO rag_chunks (
                    chunk_id, document_id, tenant_id_hash, chunk_index,
                    chunk_text, search_text, metadata_json, embedding, chunk_set_id
                )
                SELECT c.document_id || ':' || :target || ':' || TO_CHAR(c.chunk_index),
                    c.document_id, c.tenant_id_hash, c.chunk_index, c.chunk_text, c.search_text,
                    JSON_MERGEPATCH(c.metadata_json, JSON_OBJECT(
                        'chunk_id' VALUE c.document_id || ':' || :target || ':'
                            || TO_CHAR(c.chunk_index),
                        'chunk_set_id' VALUE :target RETURNING CLOB
                    ) RETURNING CLOB), NULL, :target
                FROM rag_chunks c
                JOIN rag_chunk_sets src ON src.chunk_set_id = c.chunk_set_id
                JOIN rag_chunk_sets dst ON dst.chunk_set_id = :target
                    AND dst.recipe_id = src.recipe_id AND dst.document_id = src.document_id
                JOIN rag_documents d ON d.document_id = c.document_id
                WHERE src.chunk_set_id = :source AND src.recipe_id = :recipe_id
                    AND src.is_active = 1 AND src.status = 'INDEXED'
                    AND {access_predicate}
            """,
                    access_predicate=_oracle_access_predicate_sql(alias="d"),
                ),
                binds,
            )
            _execute(
                connection,
                """
                UPDATE rag_chunk_sets SET first_page_context = (
                    SELECT first_page_context FROM rag_chunk_sets WHERE chunk_set_id = :source
                ) WHERE chunk_set_id = :target AND recipe_id = :recipe_id
            """,
                {
                    "source": source_chunk_set_id,
                    "target": target_chunk_set_id,
                    "recipe_id": recipe_id,
                },
            )

        await self._run_transaction(operation)

    async def graph_entity_counts_for_chunk_sets(
        self, chunk_set_ids: Sequence[str]
    ) -> dict[str, int]:
        """一覧の関係情報の状態を、指定した集合ごとにまとめて読む。"""
        if not chunk_set_ids:
            return {}
        predicate, binds = _oracle_in_predicate("chunk_set_id", "graph_sets", chunk_set_ids)
        rows = await self._fetch_all(
            f"SELECT chunk_set_id, COUNT(*) AS entity_count FROM rag_graph_entities "
            f"WHERE {predicate} GROUP BY chunk_set_id",
            binds,
        )
        return {str(row["chunk_set_id"]): int(str(row["entity_count"])) for row in rows}

    async def count_chunk_set_chunks(self, chunk_set_id: str) -> int:
        """指定 chunk_set の chunk 行数(chunk_set 単位の件数記録用)。"""
        row = await self._fetch_one(
            "SELECT COUNT(*) AS cnt FROM rag_chunks WHERE chunk_set_id = :chunk_set_id",
            {"chunk_set_id": chunk_set_id},
        )
        return int(str(next(iter(row.values())))) if row else 0

    async def delete_document_chunk_sets_except(
        self, *, document_id: str, keep_chunk_set_ids: Sequence[str]
    ) -> list[str]:
        """plan に無い chunk_set(とその chunk、未タグ chunk)を削除する。keep だけ残す。

        複数 materialization の cleanup。keep が空なら何もしない(安全側・現行 chunk は保持)。
        削除した chunk_set id を返す。
        """
        keep = list(dict.fromkeys(keep_chunk_set_ids))
        if not keep:
            return []
        keep_in_sql, keep_binds = _oracle_in_predicate("chunk_set_id", "keep_cs", keep)
        binds: dict[str, object] = {"document_id": document_id, **keep_binds}

        def operation(connection: OracleConnectionProtocol) -> list[str]:
            rows = _fetch_all(
                connection,
                _render_sql(
                    """
                    SELECT chunk_set_id FROM rag_chunk_sets
                    WHERE document_id = :document_id AND NOT ({keep_in_sql})
                    """,
                    keep_in_sql=keep_in_sql,
                ),
                binds,
            )
            removed = [str(next(iter(row.values()))) for row in rows]
            _execute(
                connection,
                _render_sql(
                    """
                    DELETE FROM rag_chunks
                    WHERE document_id = :document_id
                      AND (NOT ({keep_in_sql}) OR chunk_set_id IS NULL)
                    """,
                    keep_in_sql=keep_in_sql,
                ),
                binds,
            )
            _execute(
                connection,
                _render_sql(
                    """
                    DELETE FROM rag_chunk_sets
                    WHERE document_id = :document_id AND NOT ({keep_in_sql})
                    """,
                    keep_in_sql=keep_in_sql,
                ),
                binds,
            )
            return removed

        return await self._run_transaction(operation)

    async def delete_stale_document_chunk_sets(
        self, *, document_id: str, keep_chunk_set_id: str
    ) -> list[str]:
        """文書の keep 以外の chunk_set と、その chunk(+未タグ chunk)を削除する。

        取込設定変更時の旧 chunk_set GC。keep の chunk だけを残し、別 chunk_set の chunk・
        未タグ(NULL)chunk・別 chunk_set 行を削除する(binding は FK cascade)。削除した
        chunk_set id を返す。
        """
        binds = {"document_id": document_id, "keep_chunk_set_id": keep_chunk_set_id}

        def operation(connection: OracleConnectionProtocol) -> list[str]:
            rows = _fetch_all(
                connection,
                """
                SELECT chunk_set_id FROM rag_chunk_sets
                WHERE document_id = :document_id AND chunk_set_id <> :keep_chunk_set_id
                """,
                binds,
            )
            removed = [str(next(iter(row.values()))) for row in rows]
            # keep 以外の chunk と未タグ(NULL)chunk を削除する(挿入時タグの一貫性を保つ)。
            _execute(
                connection,
                """
                DELETE FROM rag_chunks
                WHERE document_id = :document_id
                  AND (chunk_set_id <> :keep_chunk_set_id OR chunk_set_id IS NULL)
                """,
                binds,
            )
            _execute(
                connection,
                """
                DELETE FROM rag_chunk_sets
                WHERE document_id = :document_id AND chunk_set_id <> :keep_chunk_set_id
                """,
                binds,
            )
            return removed

        return await self._run_transaction(operation)

    async def create_search_answer_profile(
        self,
        *,
        name: str,
        description: str | None = None,
        config: SearchAnswerProfileConfig | None = None,
    ) -> SearchAnswerProfileDetail:
        """検索・回答プロファイルを作成する。"""
        return await self._create_search_answer_profile_with_oracle(
            name=name,
            description=description,
            config=config or SearchAnswerProfileConfig(),
        )

    async def ensure_default_search_answer_profile(self) -> SearchAnswerProfileDetail:
        """tenant ごとの DEFAULT KB と DEFAULT 検索・回答プロファイルを取得または作成する。

        利用者の範囲で探すと範囲外の DEFAULT を重複作成するため、範囲を外して確認する（#214）。
        """
        with unrestricted_access_scope():
            return await self._run_transaction(_ensure_default_search_answer_profile)

    async def list_search_answer_profiles(
        self,
        *,
        status: SearchAnswerProfileStatus | None = None,
        query: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        search_answer_profile_ids: Sequence[str] | None = None,
    ) -> list[SearchAnswerProfileSummary]:
        """検索・回答プロファイル一覧を返す。参照 KB の
        うちアーカイブ済み・存在しない件数も埋める(#302)。


        `search_answer_profile_ids` を渡すとその ID に絞る（権限管理の選択済みの名前の解決。#608）。
        """
        stored = await self._list_search_answer_profiles_with_oracle(
            status=status,
            query=query,
            limit=limit,
            offset=offset,
            search_answer_profile_ids=search_answer_profile_ids,
        )
        return await self._with_knowledge_base_reference_counts(stored)

    async def _with_knowledge_base_reference_counts(
        self,
        views: Sequence[StoredSearchAnswerProfile],
    ) -> list[SearchAnswerProfileSummary]:
        """一覧の検索・回答プロファイルごとに、参照 KB のアーカイブ済み・存在しない件数を数える。

        ページ内の参照 KB をまとめて 1 回で解決する(tenant 内。利用者の KB 範囲では絞らない。
        詳細の `knowledge_bases` と同じ範囲)。
        """
        summaries = [_to_search_answer_profile_summary(view) for view in views]
        referenced = {
            view.id: parse_search_answer_profile_config(
                view.profile_config
            ).normalized_knowledge_base_ids()
            for view in views
        }
        all_ids = _unique_optional_sequence(
            [knowledge_base_id for ids in referenced.values() for knowledge_base_id in ids]
        )
        if not all_ids:
            return summaries
        refs = await self._resolve_knowledge_base_refs(all_ids)
        status_by_id = {ref.id: ref.status for ref in refs}
        return [
            summary.model_copy(
                update={
                    "archived_knowledge_base_count": sum(
                        1
                        for knowledge_base_id in referenced[summary.id]
                        if status_by_id.get(knowledge_base_id) == KnowledgeBaseStatus.ARCHIVED
                    ),
                    "missing_knowledge_base_count": sum(
                        1
                        for knowledge_base_id in referenced[summary.id]
                        if knowledge_base_id not in status_by_id
                    ),
                }
            )
            for summary in summaries
        ]

    async def count_search_answer_profiles(
        self,
        *,
        status: SearchAnswerProfileStatus | None = None,
        query: str | None = None,
        search_answer_profile_ids: Sequence[str] | None = None,
    ) -> int:
        """条件に一致する検索・回答プロファイル数を返す。"""
        return await self._count_search_answer_profiles_with_oracle(
            status=status, query=query, search_answer_profile_ids=search_answer_profile_ids
        )

    async def get_search_answer_profile(
        self,
        search_answer_profile_id: str,
    ) -> SearchAnswerProfileDetail | None:
        """検索・回答プロファイル詳細を返す。参照 KB の名前も解決して埋める。"""
        view = await self._get_search_answer_profile_with_oracle(search_answer_profile_id)
        if view is None:
            return None
        knowledge_base_ids = view.config.normalized_knowledge_base_ids()
        refs = await self._resolve_knowledge_base_refs(knowledge_base_ids)
        resolved_ids = {ref.id for ref in refs}
        missing_ids = [
            knowledge_base_id
            for knowledge_base_id in _unique_optional_sequence(knowledge_base_ids)
            if knowledge_base_id not in resolved_ids
        ]
        # 参照先のアーカイブ済み・存在しない KB は検索対象にならない。画面で警告する(#302)。
        return view.model_copy(
            update={
                "knowledge_bases": refs,
                "missing_knowledge_base_ids": missing_ids,
                "archived_knowledge_base_count": sum(
                    1 for ref in refs if ref.status == KnowledgeBaseStatus.ARCHIVED
                ),
                "missing_knowledge_base_count": len(missing_ids),
            }
        )

    async def search_answer_profile_targets(
        self,
        *,
        search_answer_profile_ids: Sequence[str] | None = None,
    ) -> list[SearchAnswerProfileTarget]:
        """推薦の候補にする ACTIVE の検索・回答プロファイルと、検索できる参照 KB(#1850)。

        プロファイルは利用者が使えるものだけ(監査 context の範囲)。参照 KB は ACTIVE で、利用者が
        使える KB だけにする(検索の範囲と同じ)。`search_answer_profile_ids` を渡すとその中に絞る。
        """
        if search_answer_profile_ids is not None and not search_answer_profile_ids:
            return []
        stored = await self._list_search_answer_profiles_with_oracle(
            status=SearchAnswerProfileStatus.ACTIVE,
            query=None,
            limit=None,
            offset=0,
            search_answer_profile_ids=search_answer_profile_ids,
        )
        referenced = {
            view.id: parse_search_answer_profile_config(
                view.profile_config
            ).normalized_knowledge_base_ids()
            for view in stored
        }
        all_ids = _unique_optional_sequence(
            [knowledge_base_id for ids in referenced.values() for knowledge_base_id in ids]
        )
        refs = await self._resolve_knowledge_base_refs(all_ids) if all_ids else []
        allowed = current_audit_request_context().allowed_knowledge_base_ids
        usable = {
            ref.id: KnowledgeBaseRef(id=ref.id, name=ref.name)
            for ref in refs
            if ref.status == KnowledgeBaseStatus.ACTIVE and (allowed is None or ref.id in allowed)
        }
        return [
            SearchAnswerProfileTarget(
                id=view.id,
                name=view.name,
                description=view.description,
                knowledge_bases=[
                    usable[knowledge_base_id]
                    for knowledge_base_id in _unique_optional_sequence(referenced[view.id])
                    if knowledge_base_id in usable
                ],
            )
            for view in stored
        ]

    async def knowledge_base_ids_by_retrieved_document(
        self,
        document_ids: Sequence[str],
        knowledge_base_ids: Sequence[str],
    ) -> dict[str, set[str]]:
        """検索で当たった文書(chunk の document_id)ごとに、その文書を検索の範囲に入れる KB(#1850)。

        検索の KB の絞り込み(`_oracle_retrieval_where`)と同じく、文書が直接属する KB に加えて、KB に
        属する重複の文書が正本(当たった文書)の chunk を使うときは、その重複の KB も数える。
        `knowledge_base_ids` の中の ACTIVE で利用者が使える KB だけを返す。
        """
        documents = _unique_optional_sequence(document_ids)
        knowledge_bases = _unique_optional_sequence(knowledge_base_ids)
        if not documents or not knowledge_bases:
            return {}
        document_sql, document_binds = _oracle_in_predicate(
            "d.document_id", "hit_document_id", documents
        )
        knowledge_base_sql, knowledge_base_binds = _oracle_in_predicate(
            "dkb.knowledge_base_id", "hit_knowledge_base_id", knowledge_bases
        )
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT d.document_id, dkb.knowledge_base_id
            FROM rag_documents d
            JOIN rag_document_knowledge_bases dkb
              ON dkb.document_id = d.document_id
            JOIN rag_knowledge_bases kb
              ON kb.knowledge_base_id = dkb.knowledge_base_id
            WHERE {document_sql}
              AND {knowledge_base_sql}
              AND kb.status = 'ACTIVE'
              AND {document_access_sql}
              AND {knowledge_base_access_sql}
            UNION
            SELECT d.document_id, dkb.knowledge_base_id
            FROM rag_documents d
            JOIN rag_documents duplicate_d
              ON duplicate_d.duplicate_of_document_id = d.document_id
            JOIN rag_document_knowledge_bases dkb
              ON dkb.document_id = duplicate_d.document_id
            JOIN rag_knowledge_bases kb
              ON kb.knowledge_base_id = dkb.knowledge_base_id
            WHERE {document_sql}
              AND {knowledge_base_sql}
              AND kb.status = 'ACTIVE'
              AND {document_access_sql}
              AND {duplicate_document_access_sql}
              AND {duplicate_reuse_sql}
              AND {knowledge_base_access_sql}
            """,
                document_sql=document_sql,
                knowledge_base_sql=knowledge_base_sql,
                document_access_sql=_oracle_access_predicate_sql(alias="d"),
                duplicate_document_access_sql=_oracle_access_predicate_sql(alias="duplicate_d"),
                duplicate_reuse_sql=_duplicate_reuses_canonical_sql(include_superseded=False),
                knowledge_base_access_sql=_oracle_knowledge_base_access_predicate_sql(alias="kb"),
            ),
            _with_tenant_bind({**document_binds, **knowledge_base_binds}),
        )
        result: dict[str, set[str]] = {}
        for row in rows:
            result.setdefault(str(row["document_id"]), set()).add(str(row["knowledge_base_id"]))
        return result

    async def list_access_target_ids(self) -> tuple[set[str], set[str]]:
        """tenant 内の全検索・回答プロファイル・全ナレッジベースの ID（アーカイブ済みを含む）。

        権限管理で指定された ID の存在確認に使う。利用者の対象範囲では絞らない（#214）。
        """
        tenant_binds = _with_tenant_bind({})
        view_rows = await self._fetch_all(
            _render_sql(
                (
                    "SELECT search_answer_profile_id FROM rag_"
                    "search_answer_profiles WHERE {tenant_sql}"
                ),
                tenant_sql=_oracle_tenant_predicate(),
            ),
            tenant_binds,
        )
        base_rows = await self._fetch_all(
            _render_sql(
                "SELECT knowledge_base_id FROM rag_knowledge_bases WHERE {tenant_sql}",
                tenant_sql=_oracle_tenant_predicate(),
            ),
            tenant_binds,
        )
        return (
            {str(row["search_answer_profile_id"]) for row in view_rows},
            {str(row["knowledge_base_id"]) for row in base_rows},
        )

    async def save_answer_record(self, record: Mapping[str, object]) -> None:
        """回答を保存する(同じ trace_id は上書き)。"""
        binds = {
            "trace_id": record["trace_id"],
            "search_answer_profile_id": record.get("search_answer_profile_id"),
            "surface": record["surface"],
            "answer_engine": record["answer_engine"],
            "question": record["question"],
            "rewritten_question": record.get("rewritten_question") or None,
            "answer": record["answer"],
            "citations_json": _json_bind(record.get("citations") or []),
            "diagnostics_json": _json_bind(record.get("diagnostics") or {}),
            "evaluation_input_json": _json_bind(record.get("evaluation_input")),
            # 回答に付けた注意書き（#2405）。
            "answer_notice": record.get("answer_notice") or None,
            # 持ち主（回答を生成した利用者）。一覧・詳細・評価・削除を持ち主へ絞る（#304）。
            "user_id_hash": current_audit_request_context().user_id_hash,
        }

        def operation(connection: OracleConnectionProtocol) -> None:
            _execute(
                connection,
                """
                MERGE INTO rag_answer_records target
                USING (SELECT :trace_id AS trace_id FROM dual) source
                ON (target.trace_id = source.trace_id)
                WHEN MATCHED THEN UPDATE SET
                    target.search_answer_profile_id = :search_answer_profile_id,
                    target.surface = :surface,
                    target.answer_engine = :answer_engine,
                    target.question = :question,
                    target.rewritten_question = :rewritten_question,
                    target.answer = :answer,
                    target.citations_json = :citations_json,
                    target.diagnostics_json = :diagnostics_json,
                    target.evaluation_input_json = :evaluation_input_json,
                    target.answer_notice = :answer_notice,
                    target.evaluation_json = NULL
                WHEN NOT MATCHED THEN INSERT (
                    trace_id, search_answer_profile_id, surface, answer_engine, question,
                    rewritten_question, answer, citations_json, diagnostics_json,
                    evaluation_input_json, answer_notice, user_id_hash
                ) VALUES (
                    :trace_id, :search_answer_profile_id, :surface, :answer_engine, :question,
                    :rewritten_question, :answer, :citations_json, :diagnostics_json,
                    :evaluation_input_json, :answer_notice, :user_id_hash
                )
                """,
                binds,
                input_sizes=_json_input_sizes(
                    "citations_json", "diagnostics_json", "evaluation_input_json"
                ),
            )

        await self._run_transaction(operation)

    async def list_answer_records(
        self,
        *,
        search_answer_profile_id: str | None,
        limit: int,
        offset: int = 0,
        trace_ids: Sequence[str] | None = None,
    ) -> list[dict[str, object]]:
        """保存済み回答を新しい順に返す(本文・JSON は含めない一覧用)。

        利用できる検索・回答プロファイルが制限されているときは、その検索・回答プロファイルの回答だけを返す（#214）。
        持ち主の回答だけを返す（SYSTEM_ADMIN と `rag.feedback.manage` は全件。#304）。
        `trace_ids` を渡すとその回答だけにする（チャットが会話の回答を引き当てる）。
        """
        where, binds = _answer_record_list_where(
            search_answer_profile_id=search_answer_profile_id, trace_ids=trace_ids
        )
        binds.update(offset_fetch_binds(offset=offset, limit=limit))
        rows = await self._fetch_all(
            f"""
            SELECT trace_id, search_answer_profile_id, surface, answer_engine, question,
                   rewritten_question, created_at,
                   JSON_VALUE(diagnostics_json, '$.confidence') AS confidence
            FROM rag_answer_records
            WHERE {where}
            ORDER BY created_at DESC, trace_id DESC
            {offset_fetch_clause()}
            """,
            binds,
        )
        return rows

    async def count_answer_records(
        self,
        *,
        search_answer_profile_id: str | None,
        trace_ids: Sequence[str] | None = None,
    ) -> int:
        """`list_answer_records` と同じ条件の件数（ページングの総件数。#304）。"""
        where, binds = _answer_record_list_where(
            search_answer_profile_id=search_answer_profile_id, trace_ids=trace_ids
        )
        row = await self._fetch_one(
            f"SELECT COUNT(*) AS count_value FROM rag_answer_records WHERE {where}",
            binds,
        )
        return _row_count_value(row)

    async def get_answer_record(self, trace_id: str) -> dict[str, object] | None:
        """保存済み回答を 1 件返す（利用できる検索・回答プロファイルの回答だけ。#214）。"""
        row = await self._fetch_one(
            _render_sql(
                """
            SELECT trace_id, search_answer_profile_id, surface, answer_engine, question,
                   rewritten_question, answer, citations_json, diagnostics_json,
                   evaluation_input_json, evaluation_json, answer_notice, created_at
            FROM rag_answer_records
            WHERE trace_id = :trace_id
              AND {scope_sql}
            """,
                scope_sql=_answer_record_scope_sql(),
            ),
            _with_answer_record_scope_bind({"trace_id": trace_id}),
        )
        if row is None:
            return None
        for key in (
            "citations_json",
            "diagnostics_json",
            "evaluation_input_json",
            "evaluation_json",
        ):
            value = row.get(key)
            if isinstance(value, str):
                row[key] = json.loads(value)
        return row

    async def list_answer_prompts(self) -> dict[str, dict[str, object]]:
        """編集した回答生成のプロンプトを {key: {content, updated_at}} で返す(未編集は含めない)。"""
        rows = await self._fetch_all(
            "SELECT prompt_key, content, updated_at FROM rag_answer_prompts", {}
        )
        return {
            str(row["prompt_key"]): {
                "content": str(row.get("content") or ""),
                "updated_at": row.get("updated_at"),
            }
            for row in rows
        }

    async def save_answer_prompt(self, key: str, content: str) -> None:
        """回答生成のプロンプトを保存する(同じ key は上書き)。"""

        def operation(connection: OracleConnectionProtocol) -> None:
            _execute(
                connection,
                """
                MERGE INTO rag_answer_prompts target
                USING (SELECT :prompt_key AS prompt_key FROM dual) source
                ON (target.prompt_key = source.prompt_key)
                WHEN MATCHED THEN UPDATE SET
                    target.content = :content,
                    target.updated_at = SYSTIMESTAMP
                WHEN NOT MATCHED THEN INSERT (prompt_key, content)
                VALUES (:prompt_key, :content)
                """,
                {"prompt_key": key, "content": content},
            )

        await self._run_transaction(operation)

    async def delete_answer_prompt(self, key: str) -> bool:
        """保存した回答生成のプロンプトを消して既定値へ戻す。消した場合 True。"""

        def operation(connection: OracleConnectionProtocol) -> int:
            return _execute_count(
                connection,
                "DELETE FROM rag_answer_prompts WHERE prompt_key = :prompt_key",
                {"prompt_key": key},
            )

        return await self._run_transaction(operation) > 0

    async def answer_prompt_overrides(self) -> dict[str, str]:
        """回答・解析へ渡す回答生成のプロンプトの上書き({key: content})。"""
        return {
            key: str(value["content"]) for key, value in (await self.list_answer_prompts()).items()
        }

    async def save_answer_evaluation(self, trace_id: str, evaluation: Mapping[str, object]) -> bool:
        """保存済み 回答へ標準回答による評価結果を保存する(再評価は上書き)。"""

        def operation(connection: OracleConnectionProtocol) -> bool:
            if not _fetch_all(
                connection,
                _render_sql(
                    "SELECT trace_id FROM rag_answer_records "
                    "WHERE trace_id = :trace_id AND {scope_sql}",
                    scope_sql=_answer_record_scope_sql(),
                ),
                _with_answer_record_scope_bind({"trace_id": trace_id}),
            ):
                return False
            _execute(
                connection,
                """
                UPDATE rag_answer_records
                SET evaluation_json = :evaluation_json
                WHERE trace_id = :trace_id
                """,
                {"trace_id": trace_id, "evaluation_json": _json_bind(dict(evaluation))},
                input_sizes=_json_input_sizes("evaluation_json"),
            )
            return True

        return await self._run_transaction(operation)

    async def purge_answer_records(self, retention_days: int) -> int:
        """保持期間を過ぎた回答を削除し、削除件数を返す（0 以下は無期限で削除しない）。"""
        if retention_days <= 0:
            return 0

        # ponytail: created_at 単独 index なしの全走査。記録が大量になったら index を足す。
        def operation(connection: OracleConnectionProtocol) -> int:
            return _execute_count(
                connection,
                "DELETE FROM rag_answer_records "
                "WHERE created_at < SYSTIMESTAMP - NUMTODSINTERVAL(:days, 'DAY')",
                {"days": retention_days},
            )

        return await self._run_transaction(operation)

    async def get_search_answer_profile_knowledge(
        self,
        search_answer_profile_id: str,
        kind: str,
    ) -> dict[str, object] | None:
        """検索・回答プロファイルの知識 payload(JSON)を返す。未登録は None。"""
        row = await self._fetch_one(
            """
            SELECT payload_json
            FROM rag_search_answer_profile_knowledge
            WHERE search_answer_profile_id = :search_answer_profile_id AND kind = :kind
            """,
            {"search_answer_profile_id": search_answer_profile_id, "kind": kind},
        )
        if row is None:
            return None
        payload = row.get("payload_json")
        if isinstance(payload, str):
            payload = json.loads(payload)
        return payload if isinstance(payload, dict) else None

    async def save_search_answer_profile_knowledge(
        self,
        search_answer_profile_id: str,
        kind: str,
        payload: Mapping[str, object],
    ) -> None:
        """検索・回答プロファイルの知識 payload を upsert する(revision を進める)。"""
        binds = {
            "search_answer_profile_id": search_answer_profile_id,
            "kind": kind,
            "payload_json": _json_bind(dict(payload)),
        }

        def operation(connection: OracleConnectionProtocol) -> None:
            _execute(
                connection,
                """
                MERGE INTO rag_search_answer_profile_knowledge target
                USING (
                    SELECT :search_answer_profile_id AS search_answer_profile_id,
                           :kind AS kind FROM dual
                ) source
                ON (
                    target.search_answer_profile_id = source.search_answer_profile_id
                    AND target.kind = source.kind
                )
                WHEN MATCHED THEN UPDATE SET
                    target.payload_json = :payload_json,
                    target.revision = target.revision + 1,
                    target.updated_at = SYSTIMESTAMP
                WHEN NOT MATCHED THEN INSERT (search_answer_profile_id, kind, payload_json)
                    VALUES (source.search_answer_profile_id, source.kind, :payload_json)
                """,
                binds,
                input_sizes=_json_input_sizes("payload_json"),
            )

        await self._run_transaction(operation)

    async def get_document_sections(self, document_id: str) -> dict[str, object] | None:
        """人が修正した文書の章節(#713)。未保存は None。"""
        row = await self._fetch_one(
            """
            SELECT sections_json, revision, updated_at
            FROM rag_document_sections
            WHERE document_id = :document_id
            """,
            {"document_id": document_id},
        )
        if row is None:
            return None
        sections = row.get("sections_json")
        if isinstance(sections, str):
            sections = json.loads(sections)
        return {
            # 章節の一覧(古い形)か、{sections, removed_source_ids}(#721)。
            "sections": sections if isinstance(sections, list | dict) else [],
            "revision": int(cast(int, row.get("revision") or 1)),
            "updated_at": row.get("updated_at"),
        }

    async def save_document_sections(
        self,
        document_id: str,
        sections: Mapping[str, object],
        *,
        base_revision: int | None,
    ) -> int:
        """人が修正した章節を保存し、新しい revision を返す(#713)。

        ``base_revision`` は読み込んだときの revision(未保存なら None)。保存済みの revision と
        違えば ``DocumentSectionsConflictError``(読み込んだ後にほかの人が保存した)。
        """
        binds = {"document_id": document_id, "sections_json": _json_bind(dict(sections))}

        def operation(connection: OracleConnectionProtocol) -> int:
            row = _fetch_one(
                connection,
                """
                SELECT revision FROM rag_document_sections
                WHERE document_id = :document_id
                FOR UPDATE
                """,
                {"document_id": document_id},
            )
            current = int(cast(int, row["revision"])) if row is not None else None
            if current != base_revision:
                raise DocumentSectionsConflictError(document_id)
            if current is None:
                _execute(
                    connection,
                    """
                    INSERT INTO rag_document_sections (document_id, sections_json)
                    VALUES (:document_id, :sections_json)
                    """,
                    binds,
                    input_sizes=_json_input_sizes("sections_json"),
                )
                return 1
            _execute(
                connection,
                """
                UPDATE rag_document_sections
                SET sections_json = :sections_json,
                    revision = revision + 1,
                    updated_at = SYSTIMESTAMP
                WHERE document_id = :document_id
                """,
                binds,
                input_sizes=_json_input_sizes("sections_json"),
            )
            return current + 1

        return await self._run_transaction(operation)

    async def delete_document_sections(self, document_id: str) -> None:
        """人が修正した章節を消し、抽出結果の章節に戻す(#713)。"""

        def operation(connection: OracleConnectionProtocol) -> None:
            _execute(
                connection,
                "DELETE FROM rag_document_sections WHERE document_id = :document_id",
                {"document_id": document_id},
            )

        await self._run_transaction(operation)

    async def list_search_answer_profile_chunk_texts(
        self,
        knowledge_base_ids: Sequence[str],
        *,
        limit: int,
    ) -> list[tuple[str, str, str]]:
        """検索・回答プロファイルが参照する KB の配信中 chunk を
         (chunk_id, document_id, text) で返す。


        ドメインキーワード候補の TF-IDF コーパスに使う。
        """
        ids = [kb_id for kb_id in dict.fromkeys(knowledge_base_ids) if kb_id][:100]
        if not ids:
            return []
        placeholders = ", ".join(f":kb_{index}" for index in range(len(ids)))
        binds: dict[str, object] = {f"kb_{index}": kb_id for index, kb_id in enumerate(ids)}
        binds["limit"] = max(1, int(limit))
        rows = await self._fetch_all(
            f"""
            SELECT c.chunk_id, c.document_id, c.search_text
            FROM rag_chunks c
            JOIN rag_chunk_sets cs ON cs.chunk_set_id = c.chunk_set_id AND cs.is_serving = 1
            WHERE c.document_id IN (
                SELECT dkb.document_id
                FROM rag_document_knowledge_bases dkb
                WHERE dkb.knowledge_base_id IN ({placeholders})
            )
            ORDER BY c.document_id, c.chunk_index
            FETCH FIRST :limit ROWS ONLY
            """,
            binds,
        )
        return [
            (str(row["chunk_id"]), str(row["document_id"]), str(row.get("search_text") or ""))
            for row in rows
        ]

    async def update_search_answer_profile(
        self,
        search_answer_profile_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        config: SearchAnswerProfileConfig | None = None,
        update_fields: set[str] | None = None,
    ) -> SearchAnswerProfileDetail:
        """検索・回答プロファイルを更新する。"""
        return await self._update_search_answer_profile_with_oracle(
            search_answer_profile_id=search_answer_profile_id,
            name=name,
            description=description,
            config=config,
            update_fields=update_fields,
        )

    async def update_question_understanding(
        self, search_answer_profile_id: str, overrides: dict[str, dict[str, object]]
    ) -> SearchAnswerProfileDetail:
        """行をロックして質問理解の項目だけを置換し、他の設定を保持する。"""
        return await self._update_search_answer_profile_with_oracle(
            search_answer_profile_id=search_answer_profile_id,
            name=None,
            description=None,
            config=None,
            update_fields={"config"},
            language_pack_patch=overrides,
        )

    async def archive_search_answer_profile(
        self, search_answer_profile_id: str
    ) -> SearchAnswerProfileDetail:
        """検索・回答プロファイルをアーカイブする。参照 KB・文書は変更しない。"""
        return await self._archive_search_answer_profile_with_oracle(search_answer_profile_id)

    # --- 回答生成設定 / Prompt 版 -----------------------------------------

    # --- チャット会話 / メッセージ -----------------------------------------

    async def create_conversation(
        self,
        *,
        search_answer_profile_id: str,
        title: str | None = None,
    ) -> StoredConversation:
        """検索・回答プロファイル配下にチャット会話を作成する。"""
        now = datetime.now(UTC)
        conversation = StoredConversation(
            id=uuid4().hex,
            search_answer_profile_id=search_answer_profile_id,
            title=title,
            status="ACTIVE",
            message_count=0,
            tenant_id_hash=_current_tenant_id_hash(),
            user_id_hash=current_audit_request_context().user_id_hash,
            created_at=now,
            updated_at=now,
        )

        def operation(connection: OracleConnectionProtocol) -> StoredConversation:
            _execute(
                connection,
                """
                INSERT INTO rag_conversations (
                    conversation_id,
                    search_answer_profile_id,
                    tenant_id_hash,
                    user_id_hash,
                    title,
                    status,
                    message_count,
                    created_at,
                    updated_at
                ) VALUES (
                    :conversation_id,
                    :search_answer_profile_id,
                    :tenant_id_hash,
                    :user_id_hash,
                    :title,
                    :status,
                    :message_count,
                    :created_at,
                    :updated_at
                )
                """,
                _conversation_binds(conversation),
            )
            return conversation

        return await self._run_transaction(operation)

    async def list_conversations(
        self,
        *,
        search_answer_profile_id: str | None = None,
        updated_since: datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[StoredConversation]:
        """会話一覧を更新が新しい順に返す(tenant/user scope で絞る)。

        `updated_since` は会話の履歴の期間(最後の更新がこの時刻以後の会話だけ。#2340)。
        """
        where_sql, binds = _oracle_conversation_where(
            search_answer_profile_id=search_answer_profile_id, updated_since=updated_since
        )
        binds.update(offset_fetch_binds(offset=offset, limit=limit))
        paging_sql = offset_fetch_clause(with_limit=limit is not None)
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT
                c.conversation_id,
                c.search_answer_profile_id,
                c.tenant_id_hash,
                c.user_id_hash,
                c.title,
                c.status,
                c.message_count,
                c.created_at,
                c.updated_at
            FROM rag_conversations c
            WHERE {where_sql}
            ORDER BY c.updated_at DESC, c.created_at DESC
            {paging_sql}
            """,
                where_sql=where_sql,
                paging_sql=paging_sql,
            ),
            binds,
        )
        return [_stored_conversation_from_row(row) for row in rows]

    async def count_conversations(
        self,
        *,
        search_answer_profile_id: str | None = None,
        updated_since: datetime | None = None,
    ) -> int:
        """条件に一致する会話数を返す(`list_conversations` と同じ条件)。"""
        where_sql, binds = _oracle_conversation_where(
            search_answer_profile_id=search_answer_profile_id, updated_since=updated_since
        )
        row = await self._fetch_one(
            _render_sql(
                """
            SELECT COUNT(*) AS count_value
            FROM rag_conversations c
            WHERE {where_sql}
            """,
                where_sql=where_sql,
            ),
            binds,
        )
        return _row_count_value(row)

    async def get_conversation(self, conversation_id: str) -> StoredConversation | None:
        """会話の現在状態を返す(tenant/user scope)。"""
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT
                c.conversation_id,
                c.search_answer_profile_id,
                c.tenant_id_hash,
                c.user_id_hash,
                c.title,
                c.status,
                c.message_count,
                c.created_at,
                c.updated_at
            FROM rag_conversations c
            WHERE c.conversation_id = :conversation_id
              AND {access_sql}
            """,
                access_sql=_oracle_conversation_access_predicate_sql(alias="c"),
            ),
            _with_conversation_access_bind({"conversation_id": conversation_id}),
        )
        if not rows:
            return None
        return _stored_conversation_from_row(rows[0])

    async def delete_conversation(self, conversation_id: str) -> None:
        """会話を削除する。メッセージは FK の ON DELETE CASCADE で消える。"""

        def operation(connection: OracleConnectionProtocol) -> None:
            if _select_conversation(connection, conversation_id) is None:
                raise KeyError(f"conversation_id={conversation_id} は存在しません。")
            _execute(
                connection,
                _render_sql(
                    """
                DELETE FROM rag_conversations
                WHERE conversation_id = :conversation_id
                  AND {access_sql}
                """,
                    access_sql=_oracle_conversation_access_predicate_sql(),
                ),
                _with_conversation_access_bind({"conversation_id": conversation_id}),
            )

        await self._run_transaction(operation)

    async def rename_conversation(self, conversation_id: str, title: str) -> StoredConversation:
        """会話タイトルを変更する。"""

        def operation(connection: OracleConnectionProtocol) -> StoredConversation:
            existing = _select_conversation(connection, conversation_id)
            if existing is None:
                raise KeyError(f"conversation_id={conversation_id} は存在しません。")
            now = datetime.now(UTC)
            _execute(
                connection,
                _render_sql(
                    """
                UPDATE rag_conversations
                SET title = :title, updated_at = :updated_at
                WHERE conversation_id = :conversation_id
                  AND {access_sql}
                """,
                    access_sql=_oracle_conversation_access_predicate_sql(),
                ),
                _with_conversation_access_bind(
                    {
                        "conversation_id": conversation_id,
                        "title": title,
                        "updated_at": now,
                    }
                ),
            )
            existing.title = title
            existing.updated_at = now
            return existing

        return await self._run_transaction(operation)

    async def append_message(self, message: StoredMessage) -> StoredMessage:
        """会話にメッセージを 1 件追加し、会話の message_count / updated_at を更新する。"""
        now = datetime.now(UTC)
        stored = StoredMessage(
            id=message.id or uuid4().hex,
            conversation_id=message.conversation_id,
            reply_to_message_id=message.reply_to_message_id,
            role=message.role,
            model=message.model,
            content=message.content,
            citations=message.citations,
            trace_id=message.trace_id,
            status=message.status,
            elapsed_ms=message.elapsed_ms,
            tenant_id_hash=_current_tenant_id_hash(),
            user_id_hash=current_audit_request_context().user_id_hash,
            created_at=message.created_at or now,
            progress=message.progress,
            lease_owner=message.lease_owner,
            # 回答前に確認した内容（#1839）。渡し忘れると clarification_json が
            # 常に NULL になる（#1877）。
            clarification=message.clarification,
            answer_notice=message.answer_notice,
            attachments=message.attachments,
        )

        def operation(connection: OracleConnectionProtocol) -> StoredMessage:
            if _select_conversation(connection, stored.conversation_id) is None:
                raise KeyError(f"conversation_id={stored.conversation_id} は存在しません。")
            _execute(
                connection,
                """
                INSERT INTO rag_messages (
                    message_id,
                    conversation_id,
                    reply_to_message_id,
                    tenant_id_hash,
                    user_id_hash,
                    role,
                    model,
                    content,
                    citations_json,
                    trace_id,
                    status,
                    elapsed_ms,
                    progress_json,
                    lease_owner,
                    heartbeat_at,
                    clarification_json,
                    answer_notice,
                    attachments_json,
                    created_at
                ) VALUES (
                    :message_id,
                    :conversation_id,
                    :reply_to_message_id,
                    :tenant_id_hash,
                    :user_id_hash,
                    :role,
                    :model,
                    :content,
                    :citations_json,
                    :trace_id,
                    :status,
                    :elapsed_ms,
                    :progress_json,
                    :lease_owner,
                    CASE WHEN :lease_owner IS NULL THEN NULL ELSE SYSTIMESTAMP END,
                    :clarification_json,
                    :answer_notice,
                    :attachments_json,
                    :created_at
                )
                """,
                _message_binds(stored),
            )
            _execute(
                connection,
                _render_sql(
                    """
                UPDATE rag_conversations
                SET title = CASE
                        WHEN title IS NULL
                         AND :generated_title IS NOT NULL
                         AND NOT EXISTS (
                             SELECT 1
                             FROM rag_messages previous
                             WHERE previous.conversation_id = :conversation_id
                               AND previous.role = 'USER'
                               AND previous.message_id <> :message_id
                         )
                        THEN :generated_title
                        ELSE title
                    END,
                    message_count = message_count + 1,
                    updated_at = :updated_at
                WHERE conversation_id = :conversation_id
                  AND {access_sql}
                """,
                    # 発話を足せるのは会話の持ち主だけ（route が確かめる。#3244）。
                    access_sql=_oracle_conversation_access_predicate_sql(),
                ),
                _with_conversation_access_bind(
                    {
                        "conversation_id": stored.conversation_id,
                        "message_id": stored.id,
                        "generated_title": (
                            _conversation_title_from_message(stored.content)
                            if stored.role == "USER"
                            else None
                        ),
                        "updated_at": now,
                    }
                ),
            )
            return stored

        return await self._run_transaction(operation)

    async def list_messages(
        self,
        conversation_id: str,
        *,
        limit: int | None = None,
    ) -> list[StoredMessage]:
        """会話のメッセージを作成順に返す(tenant/user scope)。"""
        binds: dict[str, object] = {"conversation_id": conversation_id}
        if limit is not None:
            binds["limit"] = limit
            paging_sql = "FETCH NEXT :limit ROWS ONLY"
        else:
            paging_sql = ""
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT
                m.message_id,
                m.conversation_id,
                m.reply_to_message_id,
                m.tenant_id_hash,
                m.user_id_hash,
                m.role,
                m.model,
                m.content,
                m.citations_json,
                m.trace_id,
                m.status,
                m.elapsed_ms,
                m.progress_json,
                m.lease_owner,
                m.heartbeat_at,
                m.clarification_json,
                m.answer_notice,
                m.attachments_json,
                m.created_at,
                JSON_QUERY(r.diagnostics_json, '$.passage_marks' RETURNING CLOB)
                    AS passage_marks_json,
                JSON_QUERY(r.diagnostics_json, '$.removed_passages' RETURNING CLOB)
                    AS removed_passages_json,
                JSON_QUERY(r.diagnostics_json, '$.answer_warnings' RETURNING CLOB)
                    AS answer_warnings_json,
                JSON_VALUE(r.diagnostics_json, '$.models.attached_images' RETURNING NUMBER)
                    AS answer_attached_images
            FROM rag_messages m
            JOIN rag_conversations c
              ON c.conversation_id = m.conversation_id
            LEFT JOIN rag_answer_records r
              ON r.trace_id = m.trace_id
             AND m.role = 'ASSISTANT'
            WHERE m.conversation_id = :conversation_id
              AND {access_sql}
            ORDER BY m.created_at ASC, m.message_id ASC
            {paging_sql}
            """,
                access_sql=_oracle_conversation_access_predicate_sql(alias="c"),
                paging_sql=paging_sql,
            ),
            _with_conversation_access_bind(binds),
        )
        return [_stored_message_from_row(row) for row in rows]

    async def update_chat_message_progress(
        self, message_id: str, *, lease_owner: str, progress: dict[str, Any]
    ) -> bool:
        """作成中の回答の処理の経過の記録を保存し、heartbeat を進める(#1175・#3226)。

        このプロセスが作っている ``STREAMING`` の行だけを更新する。取消・中断で
        ``STREAMING`` でなくなっていたら False(作成を止める合図)。
        """

        def operation(connection: OracleConnectionProtocol) -> int:
            return _execute_count(
                connection,
                """
                UPDATE rag_messages
                SET progress_json = :progress_json,
                    heartbeat_at = SYSTIMESTAMP
                WHERE message_id = :message_id
                  AND status = 'STREAMING'
                  AND lease_owner = :lease_owner
                """,
                {
                    "message_id": message_id,
                    "lease_owner": lease_owner,
                    "progress_json": _json_dumps(progress),
                },
            )

        return await self._run_transaction(operation) > 0

    async def heartbeat_chat_message(self, message_id: str, *, lease_owner: str) -> bool:
        """作成中の回答の heartbeat を DB の時刻で書く(#1175)。

        ``STREAMING`` でなくなっていたら(別のプロセスへの取消・中断)False。
        """

        def operation(connection: OracleConnectionProtocol) -> int:
            return _execute_count(
                connection,
                """
                UPDATE rag_messages
                SET heartbeat_at = SYSTIMESTAMP
                WHERE message_id = :message_id
                  AND status = 'STREAMING'
                  AND lease_owner = :lease_owner
                """,
                {"message_id": message_id, "lease_owner": lease_owner},
            )

        return await self._run_transaction(operation) > 0

    async def finish_chat_message(self, message: StoredMessage, *, lease_owner: str) -> bool:
        """作成中の回答に最終の状態(回答・失敗)を書く(#1175)。

        このプロセスが作っている ``STREAMING`` の行だけを更新する。取消・中断の後なら書かずに
        False を返す(利用者の停止・中断の判定を上書きしない)。
        """
        binds = {
            **_message_binds(message),
            "lease_owner": lease_owner,
            "updated_at": datetime.now(UTC),
        }

        def operation(connection: OracleConnectionProtocol) -> int:
            updated = _execute_count(
                connection,
                """
                UPDATE rag_messages
                SET content = :content,
                    model = :model,
                    citations_json = :citations_json,
                    trace_id = :trace_id,
                    status = :status,
                    elapsed_ms = :elapsed_ms,
                    progress_json = :progress_json,
                    answer_notice = :answer_notice,
                    heartbeat_at = SYSTIMESTAMP
                WHERE message_id = :message_id
                  AND status = 'STREAMING'
                  AND lease_owner = :lease_owner
                """,
                binds,
            )
            if updated:
                _execute(
                    connection,
                    _render_sql(
                        """
                    UPDATE rag_conversations
                    SET updated_at = :updated_at
                    WHERE conversation_id = :conversation_id
                      AND {access_sql}
                    """,
                        access_sql=_oracle_conversation_access_predicate_sql(),
                    ),
                    _with_conversation_access_bind(binds),
                )
            return updated

        return await self._run_transaction(operation) > 0

    async def close_streaming_chat_message(
        self,
        message_id: str,
        *,
        status: str,
        content: str,
        lease_owner: str | None = None,
        stale_seconds: float | None = None,
        scoped: bool = True,
        progress: dict[str, Any] | None = None,
    ) -> bool:
        """作成中(``STREAMING``)の回答を停止(``CANCELLED``)・中断(``ERROR``)にする(#1175)。

        - ``lease_owner`` だけ: そのプロセスが作っている行だけ(作成の task の後始末)。
        - ``lease_owner`` と ``stale_seconds``: そのプロセスの行か、heartbeat が DB の時刻で
          ``stale_seconds`` を超えて途絶えた行だけ(会話の取得のときの中断の判定)。
        - どちらも無し: ``STREAMING`` の行(利用者の停止)。
        ``scoped`` のときは会話の範囲(tenant・作成した利用者・検索・回答プロファイル)に閉じる。
        ``progress`` は閉じるときに保存する処理の経過の記録(作成していたプロセスの記録。#3226)。
        None なら保存済みの記録のまま(処理の経過の終端はメッセージの状態で出す)。
        """
        if status not in {"CANCELLED", "ERROR"}:
            raise ValueError(f"status={status} では閉じられません。")
        conditions = ["m.message_id = :message_id", "m.status = 'STREAMING'"]
        binds: dict[str, object] = {
            "message_id": message_id,
            "status": status,
            "content": content,
        }
        if lease_owner is not None and stale_seconds is not None:
            conditions.append(
                "(m.lease_owner = :lease_owner OR m.heartbeat_at IS NULL "
                "OR m.heartbeat_at < SYSTIMESTAMP - NUMTODSINTERVAL(:stale_seconds, 'SECOND'))"
            )
            binds["lease_owner"] = lease_owner
            binds["stale_seconds"] = float(stale_seconds)
        elif lease_owner is not None:
            conditions.append("m.lease_owner = :lease_owner")
            binds["lease_owner"] = lease_owner
        if scoped:
            conditions.append(
                _render_sql(
                    """
                EXISTS (
                    SELECT 1
                    FROM rag_conversations c
                    WHERE c.conversation_id = m.conversation_id
                      AND {access_sql}
                )
                """,
                    access_sql=_oracle_conversation_access_predicate_sql(alias="c"),
                )
            )
            binds = _with_conversation_access_bind(binds)
        where_sql = " AND ".join(conditions)
        statement = (
            "UPDATE rag_messages m SET m.status = :status, m.content = :content"
            + (", m.progress_json = :progress_json" if progress is not None else "")
            + " WHERE "
            + where_sql
        )

        if progress is not None:
            binds["progress_json"] = _json_dumps(progress)

        def operation(connection: OracleConnectionProtocol) -> int:
            return _execute_count(connection, statement, binds)

        return await self._run_transaction(operation) > 0

    async def get_chat_message(self, conversation_id: str, message_id: str) -> StoredMessage | None:
        """会話の 1 つのメッセージ(tenant/user scope)。段階の polling / SSE で読み直す(#1359)。

        引用などの大きい列は読まない(段階・状態・作成しているプロセスだけ)。
        """
        row = await self._fetch_one(
            _render_sql(
                """
            SELECT
                m.message_id,
                m.conversation_id,
                m.reply_to_message_id,
                m.role,
                m.model,
                m.status,
                m.progress_json,
                m.lease_owner,
                m.heartbeat_at,
                m.created_at
            FROM rag_messages m
            JOIN rag_conversations c
              ON c.conversation_id = m.conversation_id
            WHERE m.conversation_id = :conversation_id
              AND m.message_id = :message_id
              AND {access_sql}
            """,
                access_sql=_oracle_conversation_access_predicate_sql(alias="c"),
            ),
            _with_conversation_access_bind(
                {"conversation_id": conversation_id, "message_id": message_id}
            ),
        )
        return None if row is None else _stored_message_from_row(row)

    async def create_ingestion_job(
        self, job: IngestionJob, *, expected_current_release_id: str | None = None
    ) -> IngestionJob:
        """取込 job を永続化する。"""
        return await self._create_ingestion_job_with_oracle(
            job, expected_current_release_id=expected_current_release_id
        )

    async def get_ingestion_job(self, job_id: str) -> IngestionJob | None:
        """取込 job の現在状態を返す。"""
        return await self._get_ingestion_job_with_oracle(job_id)

    async def list_ingestion_jobs(
        self,
        *,
        status: IngestionJobStatus | None = None,
        limit: int | None = None,
        offset: int = 0,
        oldest_first: bool = False,
    ) -> list[IngestionJob]:
        """取込 job 一覧を返す。

        既定は新しい順(UI 用)。``oldest_first=True`` でキュー消費向けに
        古い順(FIFO)で返し、ワーカーが滞留 job を starvation させないようにする。
        """
        return await self._list_ingestion_jobs_with_oracle(
            status=status,
            limit=limit,
            offset=offset,
            oldest_first=oldest_first,
        )

    async def list_document_ingestion_jobs(
        self,
        document_id: str,
        *,
        status: IngestionJobStatus | None = None,
    ) -> list[IngestionJob]:
        """指定 document の取込 job 一覧を返す。"""
        return await self._list_document_ingestion_jobs_with_oracle(
            document_id,
            status=status,
        )

    async def replace_ingestion_segments(
        self,
        document_id: str,
        segments: Sequence[IngestionSegment],
    ) -> list[IngestionSegment]:
        """指定 document の segment checkpoint を置換する。"""
        return await self._replace_ingestion_segments_with_oracle(document_id, segments)

    async def list_ingestion_segments(self, document_id: str) -> list[IngestionSegment]:
        """指定 document の segment checkpoint 一覧を返す。"""
        return await self._list_ingestion_segments_with_oracle(document_id)

    async def update_ingestion_segment(
        self,
        segment_id: str,
        *,
        status: str | None = None,
        attempt_count: int | None = None,
        artifact_path: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> IngestionSegment | None:
        """segment checkpoint の状態を更新する。"""
        return await self._update_ingestion_segment_with_oracle(
            segment_id,
            status=status,
            attempt_count=attempt_count,
            artifact_path=artifact_path,
            error_code=error_code,
            error_message=error_message,
        )

    async def count_ingestion_jobs(self, *, status: IngestionJobStatus | None = None) -> int:
        """アクセス可能な取込 job 件数を返す。"""
        return await self._count_ingestion_jobs_with_oracle(status=status)

    async def recover_stale_ingestion_jobs(
        self,
        *,
        stale_before: datetime,
        limit: int,
        lease_ttl_seconds: float | None = None,
        exclude_lease_owner: str | None = None,
    ) -> list[IngestionJob]:
        """stale RUNNING job を再キューまたは失敗へ戻し、戻した job を返す。

        - heartbeat のある job は、heartbeat が DB の時計(``SYSTIMESTAMP``)で ``lease_ttl_seconds``
          より前に途絶えたもの(lease の TTL 切れ)を stale とする(#357)。heartbeat も DB の時計で
          書くため、worker の時計のずれに左右されない(#359)。省略時は設定の TTL を使う。
        - heartbeat の無い job(lease 導入前の行・lease を持たない実行)は、従来どおり開始が
          ``stale_before`` より前のものを stale とする。
        - ``exclude_lease_owner`` の lease の job(呼び出した worker 自身が実行中の job)は戻さない。
        """
        return await self._recover_stale_ingestion_jobs_with_oracle(
            stale_before=stale_before,
            lease_ttl_seconds=(
                lease_ttl_seconds
                if lease_ttl_seconds is not None
                else self._settings.ingestion_queue_lease_ttl_seconds
            ),
            limit=limit,
            exclude_lease_owner=exclude_lease_owner,
        )

    async def claim_ingestion_job(
        self,
        job_id: str,
        *,
        started_at: datetime,
        lease_owner: str | None = None,
    ) -> IngestionJob | None:
        """QUEUED job を row lock 付きで RUNNING へ遷移し、実行権を獲得する。

        ``lease_owner``(取込 worker の識別子)を渡すと lease を取り、``heartbeat_at`` を DB の時刻
        (``SYSTIMESTAMP``)にする。以後は worker の heartbeat で lease を延長する(#357 / #359)。
        """
        return await self._claim_ingestion_job_with_oracle(
            job_id, started_at=started_at, lease_owner=lease_owner
        )

    async def record_ingestion_startup_failure(
        self, job: IngestionJob, *, expected_attempt_count: int
    ) -> bool:
        """claim 前の失敗を数え、上限では job と対象を同じ transaction で失敗にする。

        claim と同じ親文書の lock を先に取り、別 worker の claim・取消・完了後の書き込みと
        重ねない。取得時から試行回数が変わった job と lease のある job は更新しない。
        """
        message = "取込処理を開始できませんでした。実行環境を確認して再実行してください。"
        attempts = expected_attempt_count + 1
        terminal = attempts >= job.max_attempts
        status = IngestionJobStatus.FAILED if terminal else IngestionJobStatus.QUEUED

        def operation(connection: OracleConnectionProtocol) -> bool:
            parent = _fetch_one(
                connection,
                _render_sql(
                    """
                    SELECT document_id FROM rag_documents
                    WHERE document_id = :document_id AND {access_predicate}
                    FOR UPDATE
                    """,
                    access_predicate=_oracle_access_predicate_sql(),
                ),
                _with_tenant_bind({"document_id": job.document_id}),
            )
            if parent is None:
                return False
            changed = _execute_count(
                connection,
                """
                UPDATE rag_ingestion_jobs
                SET attempt_count = :attempt_count, status = :status,
                    error_message = :error_message,
                    finished_at = CASE WHEN :status = 'FAILED' THEN SYSTIMESTAMP ELSE NULL END
                WHERE job_id = :job_id AND document_id = :document_id
                  AND status = 'QUEUED' AND lease_owner IS NULL
                  AND attempt_count = :expected_attempt_count
                  AND NOT EXISTS (
                      SELECT 1 FROM rag_ingestion_jobs active_job
                      WHERE active_job.document_id = :document_id
                        AND active_job.status = 'RUNNING'
                  )
                """,
                {
                    "job_id": job.id,
                    "document_id": job.document_id,
                    "attempt_count": attempts,
                    "expected_attempt_count": expected_attempt_count,
                    "status": status.value,
                    "error_message": message,
                },
            )
            if not changed:
                return False
            if terminal:
                if job.recipe_id is not None:
                    _execute(
                        connection,
                        """
                        UPDATE rag_document_recipes
                        SET status = 'ERROR', failed_phase = :phase,
                            error_message = :error_message, finished_at = SYSTIMESTAMP,
                            updated_at = SYSTIMESTAMP
                        WHERE recipe_id = :recipe_id AND document_id = :document_id
                        """,
                        {
                            "recipe_id": job.recipe_id,
                            "document_id": job.document_id,
                            "phase": job.phase.value,
                            "error_message": message,
                        },
                    )
                else:
                    _execute(
                        connection,
                        """
                        UPDATE rag_documents SET status = 'ERROR', error_message = :error_message
                        WHERE document_id = :document_id
                        """,
                        {"document_id": job.document_id, "error_message": message},
                    )
            return True

        return await self._run_transaction(operation)

    async def heartbeat_ingestion_jobs(self, *, lease_owner: str) -> int:
        """``lease_owner`` の lease の RUNNING job の heartbeat を更新し、更新件数を返す(#357)。

        heartbeat の時刻は DB の時計(``SYSTIMESTAMP``)で書き、stale の判定と同じ時計にする(#359)。
        """
        return await self._heartbeat_ingestion_jobs_with_oracle(lease_owner=lease_owner)

    async def update_ingestion_job_progress(
        self,
        job_id: str,
        progress: IngestionJobProgress | None,
        *,
        lease_owner: str | None = None,
    ) -> bool:
        """RUNNING の job の抽出の段の進捗を書き、書けたかを返す(#1999)。

        ``progress`` が None なら消す(再試行の始め)。``lease_owner`` を渡すと自分の lease の
        job だけに書く(lease を失った古い実行が新しい実行の進捗を上書きしない)。
        """
        return await self._update_ingestion_job_progress_with_oracle(
            job_id, progress, lease_owner=lease_owner
        )

    async def requeue_leased_ingestion_jobs(self, *, lease_owner: str) -> list[IngestionJob]:
        """``lease_owner`` の lease の RUNNING job を QUEUED に戻し、戻した job を返す(#357)。

        worker の停止で実行を打ち切った job を、次の worker が最初からやり直せるようにする。
        claim で増やした attempt は戻す(停止は job の失敗ではないため)。文書・レシピの status は
        stale の回復と同じく工程を始める前の状態へ戻す(レシピの job はレシピ行だけ)。
        """
        return await self._requeue_leased_ingestion_jobs_with_oracle(lease_owner=lease_owner)

    async def list_dispatchable_ingestion_jobs(
        self,
        *,
        limit: int,
        exclude_job_ids: Sequence[str] = (),
        exclude_phases: Collection[IngestionJobPhase] = (),
    ) -> list[IngestionJob]:
        """worker が今 claim できる QUEUED job を取る順番(``ingestion_dispatch_order_sql``)に返す。

        同じ文書の別の job が RUNNING の job(claim は必ず失敗する)と、``exclude_job_ids``
        (呼び出した worker が実行中の job)を除き、後ろの QUEUED job が流れるようにする(#357)。
        ``exclude_phases`` は枠が埋まった段(重い段の上限に達したときの PREPROCESS・EXTRACT)を
        除き、後ろの軽い段の job を取れるようにする(#2027)。
        """
        return await self._list_dispatchable_ingestion_jobs_with_oracle(
            limit=limit, exclude_job_ids=exclude_job_ids, exclude_phases=exclude_phases
        )

    async def list_ingestion_queue_positions(
        self,
        *,
        job_ids: Sequence[str] = (),
        document_ids: Sequence[str] = (),
    ) -> list[dict[str, object]]:
        """QUEUED の job ごとに、worker が claim する順で前にある job の数を返す(#2026)。

        ``job_ids`` か ``document_ids`` で対象の QUEUED の job を選ぶ(両方とも空なら
        問い合わせない)。
        前にある job は ``list_dispatchable_ingestion_jobs`` と同じ集合・順(claim できる QUEUED の
        job を ``ingestion_dispatch_order_sql`` の順。#2027)で、同じ枠の段(重い段の job は重い段、
        軽い段の job は軽い段)の job だけを数える。重い段の上限に達した worker は重い段を除いて
        軽い段を取るため、軽い段の job は前の重い段の job を待たない(段ごとの枠)。worker はすべての
        文書の job を同じキューで処理するため、前の件数は利用者が見られる文書に絞らない(件数だけを
        返す)。

        行は ``job_id``・``document_id``・``recipe_id``・``phase``・``queued_at``・
        ``jobs_ahead`` と、job のレシピの ``recipe_slot_no``・文書の最も小さいレシピの
        ``first_slot_no``(一覧の状態はレシピ1の状態のため)。
        """
        if not job_ids and not document_ids:
            return []
        return await self._list_ingestion_queue_positions_with_oracle(
            job_ids=job_ids, document_ids=document_ids
        )

    async def transition_ingestion_job(
        self,
        job_id: str,
        *,
        from_statuses: Collection[IngestionJobStatus],
        to_status: IngestionJobStatus,
        error_message: str | None = None,
        finished_at: datetime | None = None,
        lease_owner: str | None = None,
    ) -> IngestionJob | None:
        """job が ``from_statuses`` のときだけ ``to_status`` へ遷移する(compare-and-set)。

        状態の確認と書き込みを 1 回の条件付き UPDATE で行い、cancel と完了・失敗の
        書き込みが互いの最終状態を上書きしないようにする(#305)。遷移元の状態でなければ
        0 行更新になり ``None`` を返す。``error_message`` / ``finished_at`` は None でも書く。

        ``lease_owner``(job を実行している worker の識別子)を渡すと、その lease の行だけを
        遷移する(#359)。heartbeat が途絶えて別の worker が claim し直した後に、古い実行の完了・
        失敗が新しい実行の状態を上書きしないようにする。lease を持たない実行(利用者の取り消し・
        lease 導入前の経路)は渡さず、従来どおり状態だけを条件にする。
        """
        return await self._transition_ingestion_job_with_oracle(
            job_id,
            from_statuses=from_statuses,
            to_status=to_status,
            error_message=error_message,
            finished_at=finished_at,
            lease_owner=lease_owner,
        )

    async def get_ingestion_job_lease(self, job_id: str) -> IngestionJobLease | None:
        """job の状態と lease の持ち主を返す(worker が自分の実行かどうかを確かめる。#359)。

        worker は request の文脈を持たないが、``get_ingestion_job`` と同じ範囲の条件で読む。
        """
        return await self._get_ingestion_job_lease_with_oracle(job_id)

    async def find_document_by_content_hash(self, content_sha256: str) -> DocumentSummary | None:
        """同一 content hash の既存ドキュメントを返す。"""
        return await self._find_document_by_content_hash_with_oracle(content_sha256)

    async def document_language_pack_uses(
        self,
        document_ids: Sequence[str],
        *,
        current_release_id: str | None = None,
    ) -> dict[str, LanguagePackDocumentUse]:
        if not document_ids:
            return {}
        if current_release_id:
            current = await LanguagePackReleaseStore(self).get(current_release_id)
        else:
            from app.rag.published_language import initialized_release_store

            current = await (await initialized_release_store(self)).current()
        if current is None:
            raise ValueError("比較対象の言語資源の版が見つかりません。")
        predicate, binds = _oracle_in_predicate("cs.document_id", "language_document", document_ids)
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT cs.document_id, cs.recipe_id, cs.chunk_set_id,
                cs.language_pack_release_id, cs.language_pack_ingest_hash,
                cs.language_pack_job_id, cs.language_pack_built_at, rel.version,
                (SELECT j.language_pack_release_id FROM rag_ingestion_jobs j
                 WHERE j.document_id = cs.document_id AND j.recipe_id = cs.recipe_id
                    AND j.status IN ('QUEUED', 'RUNNING')
                 ORDER BY j.queued_at DESC FETCH FIRST 1 ROWS ONLY) AS target_release_id
            FROM rag_chunk_sets cs JOIN rag_documents d ON d.document_id = cs.document_id
            LEFT JOIN rag_language_pack_releases rel ON rel.release_id = cs.language_pack_release_id
            WHERE {predicate} AND cs.is_active = 1 AND {access}
            ORDER BY cs.recipe_id, cs.chunk_set_id
        """,
                predicate=predicate,
                access=_oracle_access_predicate_sql(alias="d"),
            ),
            _with_tenant_bind(binds),
        )
        result = {
            identity: LanguagePackDocumentUse(
                current_release_id=current.id, current_version=current.version
            )
            for identity in document_ids
        }
        from app.rag.language_pack_releases import index_relation

        for row in rows:
            identity = str(row["document_id"])
            used_id = _optional_str(row.get("language_pack_release_id"))
            used_hash = _optional_str(row.get("language_pack_ingest_hash"))
            result[identity].recipes.append(
                LanguagePackRecipeUse(
                    recipe_id=_optional_str(row.get("recipe_id")),
                    chunk_set_id=str(row["chunk_set_id"]),
                    release_id=used_id,
                    version=_optional_int(row.get("version")),
                    ingest_hash=used_hash,
                    relation=index_relation(
                        used_release_id=used_id, used_ingest_hash=used_hash, current=current
                    ),
                    built_at=_optional_datetime(row.get("language_pack_built_at")),
                    job_id=_optional_str(row.get("language_pack_job_id")),
                    target_release_id=_optional_str(row.get("target_release_id")),
                )
            )
        job_predicate, job_binds = _oracle_in_predicate(
            "j.document_id", "language_job_document", document_ids
        )
        targets = await self._fetch_all(
            _render_sql(
                """
            SELECT j.document_id, j.recipe_id, j.language_pack_release_id
            FROM rag_ingestion_jobs j JOIN rag_documents d ON d.document_id = j.document_id
            WHERE {predicate} AND j.status IN ('QUEUED', 'RUNNING') AND {access}
            ORDER BY j.queued_at DESC
        """,
                predicate=job_predicate,
                access=_oracle_access_predicate_sql(alias="d"),
            ),
            _with_tenant_bind(job_binds),
        )
        for row in targets:
            use = result[str(row["document_id"])]
            recipe_id = _optional_str(row.get("recipe_id"))
            if not any(recipe.recipe_id == recipe_id for recipe in use.recipes):
                use.recipes.append(
                    LanguagePackRecipeUse(
                        recipe_id=recipe_id,
                        relation=LanguagePackIndexRelation.UNRECORDED,
                        target_release_id=_optional_str(row.get("language_pack_release_id")),
                    )
                )
        return result

    async def list_documents(
        self,
        status: FileStatus | None = None,
        query: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        knowledge_base_id: str | None = None,
        queued_phase: IngestionJobPhase | None = None,
        language_pack_relation: LanguagePackIndexRelation | None = None,
        current_release_id: str | None = None,
    ) -> list[DocumentSummary]:
        """ドキュメント一覧を返す。

        ``status`` は一覧に出す状態(レシピ1の状態)。``queued_phase`` を渡さないときは、順番待ちの
        文書(一覧に「〈工程〉の順番待ち」と出る)を除く。``queued_phase`` はレシピ1の取込の job が
        順番待ちの段(#2677)。
        """
        return await self._list_documents_with_oracle(
            status=status,
            query=query,
            limit=limit,
            offset=offset,
            knowledge_base_id=knowledge_base_id,
            queued_phase=queued_phase,
            language_pack_relation=language_pack_relation,
            current_release_id=current_release_id,
        )

    async def list_document_ids(
        self,
        *,
        status: FileStatus | None = None,
        query: str | None = None,
        knowledge_base_id: str | None = None,
        queued_phase: IngestionJobPhase | None = None,
        language_pack_relation: LanguagePackIndexRelation | None = None,
        current_release_id: str | None = None,
        limit: int,
    ) -> tuple[list[str], int]:
        """一覧と同じ条件・順(ファイル名の自然順)の文書の ID を最大 ``limit`` 件と、
        条件に一致する件数。

        一覧の「条件に一致する文書をすべて選ぶ」の一括の操作(#2674)で使う。ID だけを読む。
        """
        where_sql, binds = _oracle_document_where(
            status=status,
            query=query,
            knowledge_base_id=knowledge_base_id,
            queued_phase=queued_phase,
            language_pack_relation=language_pack_relation,
            current_release_id=current_release_id,
        )
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT document_id
            FROM rag_documents
            WHERE {where_sql}
            ORDER BY {name_order_sql}
            FETCH FIRST :id_limit ROWS ONLY
            """,
                where_sql=where_sql,
                name_order_sql=_oracle_document_list_order_sql(),
            ),
            {**binds, "id_limit": limit},
        )
        total = await self.count_documents(
            status=status,
            query=query,
            knowledge_base_id=knowledge_base_id,
            queued_phase=queued_phase,
            language_pack_relation=language_pack_relation,
            current_release_id=current_release_id,
        )
        return [str(row["document_id"]) for row in rows], total

    async def list_duplicate_documents(self, document_ids: Sequence[str]) -> list[DocumentSummary]:
        """指定した正本を参照する重複文書を、所属 KB 付きで返す（削除の影響の確認。#303）。

        正本の削除は利用者の範囲外の複製の参照も外すため、複製は tenant だけで絞る。
        所属 KB は利用者が見られるものだけを付ける。
        """
        unique_document_ids = _unique_optional_sequence(document_ids)
        if not unique_document_ids:
            return []
        document_filter_sql, document_binds = _oracle_in_predicate(
            "duplicate_of_document_id",
            "duplicate_of_document_id",
            unique_document_ids,
        )
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT
                document_id,
                file_name,
                status,
                content_type,
                file_size_bytes,
                content_sha256,
                duplicate_of_document_id,
                uploaded_at,
                indexed_at
            FROM rag_documents
            WHERE {document_filter_sql}
              AND {tenant_sql}
            ORDER BY uploaded_at DESC, document_id DESC
            """,
                document_filter_sql=document_filter_sql,
                tenant_sql=_oracle_tenant_predicate(),
            ),
            _with_tenant_bind(document_binds),
        )
        summaries = [_to_document_summary(_stored_document_from_row(row)) for row in rows]
        return await self._attach_knowledge_base_refs_to_documents(summaries)

    async def list_document_extractions(self) -> list[dict[str, object]]:
        """アクセス可能な document の extraction JSON だけを返す。"""
        return await self._list_document_extractions_with_oracle()

    async def count_documents(
        self,
        status: FileStatus | None = None,
        query: str | None = None,
        knowledge_base_id: str | None = None,
        queued_phase: IngestionJobPhase | None = None,
        language_pack_relation: LanguagePackIndexRelation | None = None,
        current_release_id: str | None = None,
    ) -> int:
        """条件に一致するドキュメント数を返す(条件は ``list_documents`` と同じ)。"""
        return await self._count_documents_with_oracle(
            status=status,
            query=query,
            knowledge_base_id=knowledge_base_id,
            queued_phase=queued_phase,
            language_pack_relation=language_pack_relation,
            current_release_id=current_release_id,
        )

    async def count_document_chunks(self, document_id: str) -> int:
        """指定 document の検索可能なチャンク行数を返す。"""
        return await self._count_document_chunks_with_oracle(document_id)

    async def list_document_chunks(self, document_id: str) -> list[DocumentChunkView]:
        """指定 document の chunk/citation 可視化用 metadata を返す。"""
        return await self._list_document_chunks_with_oracle(document_id)

    async def get_document(self, document_id: str) -> DocumentDetail | None:
        """ドキュメント詳細を返す(抽出結果などの JSON 列を含む)。"""
        detail = await self._get_document_with_oracle(document_id)
        if detail is None:
            return None
        uses = await self.document_language_pack_uses([document_id])
        return detail.model_copy(update={"language_pack": uses.get(document_id)})

    async def document_exists(self, document_id: str) -> bool:
        """利用者から見える文書があるかだけを返す(JSON 列は読まない。#341)。"""
        row = await self._fetch_one(
            _render_sql(
                """
                SELECT document_id
                FROM rag_documents
                WHERE document_id = :document_id
                  AND {access_predicate}
                """,
                access_predicate=_oracle_access_predicate_sql(),
            ),
            _with_tenant_bind({"document_id": document_id}),
        )
        return row is not None

    async def get_document_summary(self, document_id: str) -> DocumentSummary | None:
        """文書の状態・ファイル情報だけを返す(JSON 列と所属 KB は読まない。#341)。

        ポーリングされる API の存在確認・状態確認に使う。抽出結果・分類・ファイル準備の成果物が
        必要な経路は ``get_document`` を使う。
        """
        row = await self._fetch_one(
            _render_sql(
                """
                SELECT
                    {columns}
                FROM rag_documents
                WHERE document_id = :document_id
                  AND {access_predicate}
                """,
                columns=_DOCUMENT_STATE_COLUMNS_SQL.strip(),
                access_predicate=_oracle_access_predicate_sql(),
            ),
            _with_tenant_bind({"document_id": document_id}),
        )
        if row is None:
            return None
        return _to_document_summary(_stored_document_from_row(row))

    async def get_document_processing_config(self, document_id: str) -> DocumentProcessingConfig:
        """文書単位の処理レシピ上書きを返す。未設定は全項目継承。"""
        row = await self._fetch_one(
            _render_sql(
                """
                SELECT processing_config
                FROM rag_documents
                WHERE document_id = :document_id
                  AND {access_predicate}
                """,
                access_predicate=_oracle_access_predicate_sql(),
            ),
            _with_tenant_bind({"document_id": document_id}),
        )
        if row is None:
            raise KeyError(f"document_id={document_id} は存在しません。")
        try:
            return DocumentProcessingConfig.model_validate(
                _json_loads(row.get("processing_config"))
            )
        except Exception:  # noqa: BLE001 - 壊れた永続値は global 継承へ安全に縮退する
            logger.warning(
                "文書処理設定の復元に失敗したため global 継承へ縮退します。",
                exc_info=True,
            )
            return DocumentProcessingConfig()

    async def update_document_processing_config(
        self,
        document_id: str,
        config: DocumentProcessingConfig,
    ) -> DocumentProcessingConfig:
        """文書単位の処理レシピ上書きを保存する。"""
        payload = config.model_dump(mode="json", exclude_none=True)

        def operation(connection: OracleConnectionProtocol) -> DocumentProcessingConfig:
            if _select_document_state(connection, document_id) is None:
                raise KeyError(f"document_id={document_id} は存在しません。")
            _execute(
                connection,
                """
                UPDATE rag_documents
                SET processing_config = :processing_config
                WHERE document_id = :document_id
                """,
                {
                    "document_id": document_id,
                    "processing_config": _json_bind(payload),
                },
                input_sizes=_json_input_sizes("processing_config"),
            )
            return config

        return await self._run_transaction(operation)

    async def delete_document(self, document_id: str) -> bool:
        """ドキュメントと関連 chunk/index/ingestion 行を削除する。"""
        return await self._delete_document_with_oracle(document_id)

    async def update_document_status(
        self,
        document_id: str,
        status: FileStatus,
        error_message: str | None = None,
    ) -> DocumentDetail:
        """ドキュメント状態を更新する。"""
        return await self._update_document_status_with_oracle(
            document_id=document_id,
            status=status,
            error_message=error_message,
        )

    async def save_document_classification(
        self,
        document_id: str,
        classification: DocumentClassification,
    ) -> DocumentDetail:
        """文書の分類と有効期間を保存する(空なら列を NULL に戻す)。"""

        def operation(connection: OracleConnectionProtocol) -> DocumentDetail:
            _execute(
                connection,
                _render_sql(
                    """
                UPDATE rag_documents
                SET classification = :classification
                WHERE document_id = :document_id
                  AND {access_predicate}
                """,
                    access_predicate=_oracle_access_predicate_sql(),
                ),
                _with_tenant_bind(
                    {
                        "document_id": document_id,
                        "classification": (
                            None
                            if classification.is_empty()
                            else _json_dumps(
                                classification.model_dump(mode="json", exclude_none=True)
                            )
                        ),
                    }
                ),
            )
            return _select_document_detail_with_refs(connection, document_id)

        return await self._run_transaction(operation)

    async def set_document_superseded_by(
        self,
        document_id: str,
        superseded_by_document_id: str | None,
    ) -> DocumentDetail:
        """文書を置き換えた新しい版を設定・解除する(#1248)。

        - ``None`` は解除(今有効な版に戻す)。
        - 自分自身・循環(A→B→A。新しい版の側の置き換えをたどって元の文書に戻る)は
          ``DocumentSupersessionError``。新しい版の文書が利用者から見えないときも同じ。
        - 同じ新しい版を設定し直しても、置き換えた日時は変えない。
        - 文書が見えないときは ``KeyError``。
        """

        def operation(connection: OracleConnectionProtocol) -> DocumentDetail:
            # 2 つの文書の行を ID の順にロックし、逆向きの同時の設定(A→B と B→A)が
            # 両方とも循環の検証を通らないようにする。
            locked = {
                lock_id: _select_document_for_update(connection, lock_id) is not None
                for lock_id in sorted({document_id, superseded_by_document_id or document_id})
            }
            if not locked[document_id]:
                raise KeyError(f"document_id={document_id} は存在しません。")
            if superseded_by_document_id is not None:
                _validate_superseded_by(connection, document_id, superseded_by_document_id)
            _execute(
                connection,
                _render_sql(
                    """
                UPDATE rag_documents
                SET superseded_at = CASE
                        WHEN :superseded_by_document_id IS NULL THEN NULL
                        WHEN superseded_by_document_id = :superseded_by_document_id
                            THEN superseded_at
                        ELSE SYSTIMESTAMP
                    END,
                    superseded_by_document_id = :superseded_by_document_id
                WHERE document_id = :document_id
                  AND {access_predicate}
                """,
                    access_predicate=_oracle_access_predicate_sql(),
                ),
                _with_tenant_bind(
                    {
                        "document_id": document_id,
                        "superseded_by_document_id": superseded_by_document_id,
                    }
                ),
            )
            return _select_document_detail_with_refs(connection, document_id)

        return await self._run_transaction(operation)

    async def list_document_classifications_for_normalization(
        self, *, limit: int, after_document_id: str | None
    ) -> list[tuple[str, object]]:
        """運用 CLI 用に全 tenant の文書の分類を document_id 順に返す(API では使わない。#547)。"""
        rows = await self._fetch_all(
            """
            SELECT document_id, classification
            FROM rag_documents
            WHERE classification IS NOT NULL
              AND (:after_document_id IS NULL OR document_id > :after_document_id)
            ORDER BY document_id ASC
            FETCH NEXT :limit ROWS ONLY
            """,
            {"after_document_id": after_document_id, "limit": limit},
        )
        return [(str(row["document_id"]), _json_loads(row.get("classification"))) for row in rows]

    async def update_document_classification_for_normalization(
        self, document_id: str, classification: dict[str, object] | None
    ) -> None:
        """運用 CLI 用に正規化した分類だけを上書きする(tenant の範囲を越える。#547)。"""

        def operation(connection: OracleConnectionProtocol) -> None:
            _execute(
                connection,
                """
                UPDATE rag_documents
                SET classification = :classification
                WHERE document_id = :document_id
                """,
                {
                    "document_id": document_id,
                    "classification": None if not classification else _json_dumps(classification),
                },
            )

        await self._run_transaction(operation)

    async def save_preprocess_artifact(
        self,
        document_id: str,
        artifact: DocumentPreprocessArtifact | None,
    ) -> DocumentDetail:
        """ファイル準備の出力ファイル情報を保存する。"""
        return await self._save_preprocess_artifact_with_oracle(document_id, artifact)

    async def reset_document_ingestion_outputs(
        self,
        document_id: str,
        *,
        status: FileStatus = FileStatus.UPLOADED,
        error_message: str | None = None,
        clear_preprocess_artifact: bool = False,
    ) -> DocumentDetail:
        """再取込前に旧抽出・索引・checkpoint と派生状態を破棄する。"""
        return await self._reset_document_ingestion_outputs_with_oracle(
            document_id=document_id,
            status=status,
            error_message=error_message,
            clear_preprocess_artifact=clear_preprocess_artifact,
        )

    async def reset_document_chunk_outputs(
        self,
        document_id: str,
        *,
        status: FileStatus = FileStatus.REVIEW,
        error_message: str | None = None,
    ) -> DocumentDetail:
        """CHUNK 再処理前に chunk 以降だけを破棄する。"""
        return await self._reset_document_chunk_outputs_with_oracle(
            document_id=document_id,
            status=status,
            error_message=error_message,
        )

    async def reset_document_index_outputs(
        self,
        document_id: str,
        *,
        status: FileStatus = FileStatus.CHUNKED,
        error_message: str | None = None,
    ) -> DocumentDetail:
        """INDEX 再処理前に embedding/index/binding だけを破棄する。"""
        return await self._reset_document_index_outputs_with_oracle(
            document_id=document_id,
            status=status,
            error_message=error_message,
        )

    async def save_extraction(
        self, document_id: str, extraction: StructuredExtraction
    ) -> DocumentDetail:
        """VLM/LLM の抽出本文を保存する。"""
        return await self._save_extraction_with_oracle(document_id, extraction)

    async def save_index(
        self,
        document_id: str,
        extraction: StructuredExtraction,
        chunks: list[Chunk],
        embeddings: list[list[float]],
        chunk_set_id: str | None = None,
    ) -> list[RetrievedChunk]:
        """構造化抽出と chunk/vector を 1 transaction で保存する。

        chunk_set_id を渡すと、その chunk_set の chunk だけを置換・タグ付けする(複数 chunk_set
        共存)。None は文書の全 chunk を置換し未タグ保存(現行挙動・後方互換)。
        """
        if len(chunks) != len(embeddings):
            raise ValueError("chunks と embeddings の件数が一致しません。")
        for index, embedding in enumerate(embeddings):
            self._validate_embedding_width(embedding, f"chunk embedding[{index}]")
        return await self._save_index_with_oracle(
            document_id,
            extraction,
            chunks,
            embeddings,
            chunk_set_id=chunk_set_id,
        )

    async def save_chunk_preview(
        self,
        document_id: str,
        extraction: StructuredExtraction,
        chunks: list[Chunk],
        chunk_set_id: str | None = None,
    ) -> list[RetrievedChunk]:
        """embedding なしで chunk/citation preview 用 chunk 行を保存する。"""
        return await self._save_index_with_oracle(
            document_id,
            extraction,
            chunks,
            [None] * len(chunks),
            chunk_set_id=chunk_set_id,
        )

    async def list_chunk_set_chunks(self, chunk_set_id: str) -> list[DocumentChunkView]:
        """指定 chunk_set の chunk/citation metadata を返す。"""
        return await self._list_chunk_set_chunks_with_oracle(chunk_set_id)

    async def update_chunk_set_embeddings(
        self,
        *,
        chunk_set_id: str,
        chunks: list[Chunk],
        embeddings: list[list[float]],
    ) -> None:
        """承認済み chunk 行へ embedding を一括反映する。"""
        if len(chunks) != len(embeddings):
            raise ValueError("chunks と embeddings の件数が一致しません。")
        for index, embedding in enumerate(embeddings):
            self._validate_embedding_width(embedding, f"chunk embedding[{index}]")
        await self._update_chunk_set_embeddings_with_oracle(
            chunk_set_id=chunk_set_id,
            chunks=chunks,
            embeddings=embeddings,
        )

    async def replace_document_graph_index(
        self,
        document_id: str,
        graph_index: GraphIndex,
        *,
        chunk_set_id: str | None = None,
    ) -> None:
        """指定 chunk_set（legacy は document）の GraphRAG-lite index を置換する。"""
        await self._replace_document_graph_index_with_oracle(
            document_id,
            graph_index,
            chunk_set_id=chunk_set_id,
        )

    async def save_search_audit_event(self, event: Mapping[str, object]) -> None:
        """脱機密化済み検索監査イベントを Oracle audit table へ保存する。"""
        await self._run_transaction(
            lambda connection: _execute(
                connection,
                """
                INSERT INTO rag_search_audit (
                    event_type,
                    trace_id,
                    request_id,
                    tenant_id_hash,
                    user_id_hash,
                    outcome,
                    search_mode,
                    query_hash,
                    query_chars,
                    filter_keys,
                    memory_plan_id,
                    top_k,
                    rerank_top_n,
                    query_variant_count,
                    retrieved_count,
                    reranked_count,
                    deduplicated_count,
                    context_diversified_count,
                    context_group_expanded_count,
                    context_expanded_count,
                    context_adaptive_expanded_count,
                    context_dependency_promoted_count,
                    context_compressed_count,
                    context_compression_saved_chars,
                    agent_memory_retrieved_count,
                    agent_memory_writeback_count,
                    agent_memory_writeback_status,
                    evidence_count,
                    support_count,
                    structure_count,
                    history_count,
                    resolver_rejected_count,
                    insufficient_context_count,
                    citation_count,
                    context_chars,
                    context_window_chars,
                    document_ids,
                    knowledge_base_ids,
                    config_fingerprint,
                    elapsed_ms,
                    error_stage,
                    error_type
                ) VALUES (
                    :event_type,
                    :trace_id,
                    :request_id,
                    :tenant_id_hash,
                    :user_id_hash,
                    :outcome,
                    :search_mode,
                    :query_hash,
                    :query_chars,
                    :filter_keys,
                    :memory_plan_id,
                    :top_k,
                    :rerank_top_n,
                    :query_variant_count,
                    :retrieved_count,
                    :reranked_count,
                    :deduplicated_count,
                    :context_diversified_count,
                    :context_group_expanded_count,
                    :context_expanded_count,
                    :context_adaptive_expanded_count,
                    :context_dependency_promoted_count,
                    :context_compressed_count,
                    :context_compression_saved_chars,
                    :agent_memory_retrieved_count,
                    :agent_memory_writeback_count,
                    :agent_memory_writeback_status,
                    :evidence_count,
                    :support_count,
                    :structure_count,
                    :history_count,
                    :resolver_rejected_count,
                    :insufficient_context_count,
                    :citation_count,
                    :context_chars,
                    :context_window_chars,
                    :document_ids,
                    :knowledge_base_ids,
                    :config_fingerprint,
                    :elapsed_ms,
                    :error_stage,
                    :error_type
                )
                """,
                _search_audit_binds(event),
            )
        )

    async def save_ingestion_audit_event(self, event: Mapping[str, object]) -> None:
        """脱機密化済み取込監査イベントを Oracle audit table へ保存する。"""
        await self._run_transaction(
            lambda connection: _execute(
                connection,
                """
                INSERT INTO rag_ingestion_audit (
                    event_type,
                    trace_id,
                    request_id,
                    tenant_id_hash,
                    user_id_hash,
                    document_id,
                    outcome,
                    source_sha256,
                    source_bytes,
                    document_type,
                    extraction_confidence,
                    parser_backend,
                    parser_profile,
                    segment_count,
                    fallback_count,
                    failed_segment_count,
                    chunk_count,
                    vector_count,
                    elapsed_ms,
                    error_type,
                    error_message
                ) VALUES (
                    :event_type,
                    :trace_id,
                    :request_id,
                    :tenant_id_hash,
                    :user_id_hash,
                    :document_id,
                    :outcome,
                    :source_sha256,
                    :source_bytes,
                    :document_type,
                    :extraction_confidence,
                    :parser_backend,
                    :parser_profile,
                    :segment_count,
                    :fallback_count,
                    :failed_segment_count,
                    :chunk_count,
                    :vector_count,
                    :elapsed_ms,
                    :error_type,
                    :error_message
                )
                """,
                _ingestion_audit_binds(event),
            )
        )

    async def save_feedback(
        self,
        feedback: Mapping[str, object],
        *,
        details: Mapping[str, object] | None = None,
    ) -> str:
        """feedback metadata と任意の調査用本文を同一 transaction で追記する。"""
        feedback_id = _audit_str(feedback, "feedback_id", uuid4().hex)
        binds = _citation_feedback_binds(feedback, feedback_id=feedback_id)

        def operation(connection: OracleConnectionProtocol) -> None:
            _execute(
                connection,
                """
                INSERT INTO rag_citation_feedback (
                    feedback_id,
                    trace_id,
                    search_answer_profile_id,
                    target_type,
                    source_surface,
                    document_id,
                    chunk_id,
                    tenant_id_hash,
                    user_id_hash,
                    rating,
                    reason,
                    comment_hash,
                    comment_chars
                ) VALUES (
                    :feedback_id,
                    :trace_id,
                    :search_answer_profile_id,
                    :target_type,
                    :source_surface,
                    :document_id,
                    :chunk_id,
                    :tenant_id_hash,
                    :user_id_hash,
                    :rating,
                    :reason,
                    :comment_hash,
                    :comment_chars
                )
                """,
                binds,
            )
            if details is not None:
                _execute(
                    connection,
                    """
                    INSERT INTO rag_feedback_details (
                        feedback_id,
                        tenant_id_hash,
                        message_id,
                        content_source,
                        question_text,
                        answer_text,
                        comment_text,
                        corrected_answer_text,
                        citations_json,
                        search_text
                    ) VALUES (
                        :feedback_id,
                        :tenant_id_hash,
                        :message_id,
                        :content_source,
                        :question_text,
                        :answer_text,
                        :comment_text,
                        :corrected_answer_text,
                        :citations_json,
                        :search_text
                    )
                    """,
                    _feedback_detail_binds(
                        details,
                        feedback_id=feedback_id,
                        tenant_id_hash=_optional_str(binds.get("tenant_id_hash")),
                    ),
                )

        await self._run_transaction(operation)
        return feedback_id

    async def list_current_feedback(self, trace_id: str) -> list[dict[str, object]]:
        """現在の利用者・trace について対象ごとの最新 feedback を返す。"""
        context = current_audit_request_context()
        if context.user_id_hash is None:
            return []
        rows = await self._fetch_all(
            _render_sql(
                """
            WITH ranked_feedback AS (
                SELECT
                    f.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY f.target_type, NVL(f.document_id, '-'), NVL(f.chunk_id, '-')
                        ORDER BY f.created_at DESC, f.feedback_id DESC
                    ) AS feedback_rank
                FROM rag_citation_feedback f
                WHERE f.trace_id = :trace_id
                  AND f.user_id_hash = :user_id_hash
                  AND {tenant_sql}
            )
            SELECT
                f.feedback_id,
                f.trace_id,
                f.search_answer_profile_id,
                f.target_type,
                f.source_surface,
                f.document_id,
                f.chunk_id,
                fd.message_id,
                f.rating,
                f.reason,
                fd.comment_text AS "comment",
                fd.corrected_answer_text AS corrected_answer,
                f.created_at
            FROM ranked_feedback f
            LEFT JOIN rag_feedback_details fd ON fd.feedback_id = f.feedback_id
            WHERE f.feedback_rank = 1
            ORDER BY f.target_type, f.document_id, f.chunk_id
            """,
                tenant_sql=_oracle_tenant_predicate(alias="f"),
            ),
            _with_tenant_bind({"trace_id": trace_id, "user_id_hash": context.user_id_hash}),
        )
        for row in rows:
            row.pop("feedback_rank", None)
        return rows

    async def get_feedback_message_context(
        self,
        message_id: str,
        trace_id: str,
    ) -> dict[str, object] | None:
        """現在ユーザーの assistant message から質問・回答・根拠を取得する。"""
        row = await self._fetch_one(
            _render_sql(
                """
                SELECT
                    assistant.message_id,
                    c.user_id_hash AS conversation_user_id_hash,
                    user_message.content AS question_text,
                    assistant.content AS answer_text,
                    assistant.citations_json
                FROM rag_messages assistant
                JOIN rag_conversations c
                  ON c.conversation_id = assistant.conversation_id
                LEFT JOIN rag_messages user_message
                  ON user_message.message_id = assistant.reply_to_message_id
                 AND user_message.role = 'USER'
                WHERE assistant.message_id = :message_id
                  AND assistant.trace_id = :trace_id
                  AND assistant.role = 'ASSISTANT'
                  AND {access_sql}
                """,
                # 見える会話の回答を引く（評価は持ち主だけ。呼ぶ側が持ち主で確かめる。#3244）。
                access_sql=_oracle_conversation_access_predicate_sql(alias="c"),
            ),
            _with_conversation_access_bind({"message_id": message_id, "trace_id": trace_id}),
        )
        if row is None:
            return None
        return {
            "message_id": row.get("message_id"),
            "conversation_user_id_hash": row.get("conversation_user_id_hash"),
            "question_text": row.get("question_text"),
            "answer_text": row.get("answer_text"),
            "citations": _json_object_list(row.get("citations_json")),
            "content_source": "chat_message",
        }

    async def get_chat_answer_owner_hash(self, trace_id: str) -> str | None:
        """trace の回答があるチャットの会話の持ち主（`user_id_hash`）。見える会話に無ければ None。

        持ち主の分からない古い会話（`user_id_hash` が NULL）は空文字。

        メッセージ ID の無いチャットの評価も、持ち主だけに送らせるために使う（#3244）。
        """
        row = await self._fetch_one(
            _render_sql(
                """
                SELECT c.user_id_hash
                FROM rag_messages m
                JOIN rag_conversations c
                  ON c.conversation_id = m.conversation_id
                WHERE m.trace_id = :trace_id
                  AND m.role = 'ASSISTANT'
                  AND {access_sql}
                FETCH FIRST 1 ROWS ONLY
                """,
                access_sql=_oracle_conversation_access_predicate_sql(alias="c"),
            ),
            _with_conversation_access_bind({"trace_id": trace_id}),
        )
        if row is None:
            return None
        # 持ち主の分からない古い会話は空（誰も評価を足せない。会話に無い trace の None と分ける）。
        owner = row.get("user_id_hash")
        return owner if isinstance(owner, str) and owner else ""

    async def list_feedback_dashboard_rows(
        self,
        *,
        search_answer_profile_id: str | None,
        target_type: str | None,
        rating: str | None,
        reason: str | None,
        period_days: int | None,
        search_query: str | None,
        sort_order: str,
        limit: int,
        offset: int,
    ) -> tuple[
        list[dict[str, object]],
        int,
        list[dict[str, object]],
        list[dict[str, object]],
    ]:
        """有効な最新 feedback の明細、総数、集計 group を返す。"""
        where_sql, binds = _feedback_dashboard_filters(
            search_answer_profile_id=search_answer_profile_id,
            target_type=target_type,
            rating=rating,
            reason=reason,
            period_days=period_days,
            search_query=search_query,
        )
        previous_where_sql, previous_binds = _feedback_dashboard_filters(
            search_answer_profile_id=search_answer_profile_id,
            target_type=target_type,
            rating=rating,
            reason=reason,
            period_days=period_days,
            search_query=search_query,
            previous_period=True,
        )
        cte = _feedback_latest_cte()
        # メッセージ・会話・監査は、フィードバックを送った本人のものだけを結び付ける
        # （他人の trace を指定しても他人の会話を出さない。#457）。
        # SQL の中に `--` のコメントを書かない（実行前に改行をつぶすため、後ろがコメントになる）。
        # メッセージは全件に順位を付けてから結合せず、フィードバックごとに最新の 1 件だけを引く
        # （OUTER APPLY。#471）。
        order_sql = (
            "f.created_at ASC, f.feedback_id ASC"
            if sort_order == "oldest"
            else "f.created_at DESC, f.feedback_id DESC"
        )
        rows = await self._fetch_all(
            f"""
            {cte}
            SELECT
                f.feedback_id,
                f.trace_id,
                f.search_answer_profile_id,
                bv.name AS search_answer_profile_name,
                f.target_type,
                f.source_surface,
                f.document_id,
                f.chunk_id,
                d.file_name,
                f.rating,
                f.reason,
                f.created_at,
                COALESCE(fd.message_id, m.message_id) AS message_id,
                m.model,
                c.conversation_id,
                c.title AS conversation_title,
                DBMS_LOB.SUBSTR(fd.question_text, 240, 1) AS question_preview,
                DBMS_LOB.SUBSTR(fd.comment_text, 160, 1) AS comment_preview,
                CASE WHEN fd.comment_text IS NULL THEN 0 ELSE 1 END AS has_comment
            FROM latest_feedback f
            LEFT JOIN rag_feedback_details fd ON fd.feedback_id = f.feedback_id
            LEFT JOIN rag_search_answer_profiles bv
              ON bv.search_answer_profile_id = f.search_answer_profile_id
             AND NVL(bv.tenant_id_hash, '__GLOBAL__') = NVL(f.tenant_id_hash, '__GLOBAL__')
            OUTER APPLY (
                SELECT am.message_id, am.conversation_id, am.model, am.elapsed_ms
                FROM rag_messages am
                WHERE am.role = 'ASSISTANT'
                  AND NVL(am.tenant_id_hash, '__GLOBAL__') = NVL(f.tenant_id_hash, '__GLOBAL__')
                  AND NVL(am.user_id_hash, '__NONE__') = NVL(f.user_id_hash, '__NONE__')
                  AND (
                        (fd.message_id IS NOT NULL AND am.message_id = fd.message_id)
                        OR (fd.message_id IS NULL AND am.trace_id = f.trace_id)
                      )
                ORDER BY am.created_at DESC, am.message_id DESC
                FETCH FIRST 1 ROWS ONLY
            ) m
            LEFT JOIN rag_conversations c
              ON c.conversation_id = m.conversation_id
             AND NVL(c.tenant_id_hash, '__GLOBAL__') = NVL(f.tenant_id_hash, '__GLOBAL__')
            LEFT JOIN rag_documents d
              ON d.document_id = f.document_id
             AND NVL(d.tenant_id_hash, '__GLOBAL__') = NVL(f.tenant_id_hash, '__GLOBAL__')
            WHERE {where_sql}
            ORDER BY {order_sql}
            {offset_fetch_clause()}
            """,
            _with_tenant_bind({**binds, **offset_fetch_binds(offset=offset, limit=limit)}),
        )
        count_row = await self._fetch_one(
            f"""
            {cte}
            SELECT COUNT(*) AS total
            FROM latest_feedback f
            LEFT JOIN rag_feedback_details fd ON fd.feedback_id = f.feedback_id
            WHERE {where_sql}
            """,
            _with_tenant_bind(binds),
        )
        groups = await self._fetch_all(
            f"""
            {cte}
            SELECT f.target_type, f.rating, f.reason, COUNT(*) AS item_count
            FROM latest_feedback f
            LEFT JOIN rag_feedback_details fd ON fd.feedback_id = f.feedback_id
            WHERE {where_sql}
            GROUP BY f.target_type, f.rating, f.reason
            """,
            _with_tenant_bind(binds),
        )
        previous_groups: list[dict[str, object]] = []
        if period_days is not None:
            previous_groups = await self._fetch_all(
                f"""
                {cte}
                SELECT f.target_type, f.rating, f.reason, COUNT(*) AS item_count
                FROM latest_feedback f
                LEFT JOIN rag_feedback_details fd ON fd.feedback_id = f.feedback_id
                WHERE {previous_where_sql}
                GROUP BY f.target_type, f.rating, f.reason
                """,
                _with_tenant_bind(previous_binds),
            )
        return rows, _int_value((count_row or {}).get("total")), groups, previous_groups

    async def feedback_exists(self, feedback_id: str) -> bool:
        """送った利用者にかかわらず、利用できる検索・回答プロファイルに feedback があるか（#408）。

        詳細を送信者で絞って見つからなかったとき、他人の分（403）とない ID（404）を分けるために
        使う（NL2SQL のジョブと同じ応答）。本文は読まない。
        """
        row = await self._fetch_one(
            _render_sql(
                """
                SELECT 1 AS found
                FROM rag_citation_feedback f
                WHERE f.feedback_id = :feedback_id
                  AND {tenant_sql}
                  AND {scope_sql}
                FETCH NEXT 1 ROWS ONLY
                """,
                tenant_sql=_oracle_tenant_predicate(alias="f"),
                scope_sql=" AND ".join(
                    _search_answer_profile_scope_predicates("f.search_answer_profile_id")
                    or ["1 = 1"]
                ),
            ),
            _with_search_answer_profile_scope_bind(_with_tenant_bind({"feedback_id": feedback_id})),
        )
        return row is not None

    async def get_feedback_detail(self, feedback_id: str) -> dict[str, object] | None:
        """feedback 全文と trace 診断を返す。

        SYSTEM_ADMIN 以外は、自分が送った feedback だけ（#408）。
        """
        owner_predicates, owner_binds = _feedback_owner_scope("f.user_id_hash")
        # メッセージ・会話・監査は、フィードバックを送った本人のものだけを結び付ける（#457）。
        # 全件に順位を付けてから結合せず、フィードバックごとに最新の 1 件だけを引く
        # （OUTER APPLY。#471）。
        row = await self._fetch_one(
            _render_sql(
                """
                SELECT
                    f.feedback_id,
                    f.trace_id,
                    f.search_answer_profile_id,
                    bv.name AS search_answer_profile_name,
                    f.target_type,
                    f.source_surface,
                    f.document_id,
                    f.chunk_id,
                    d.file_name,
                    f.rating,
                    f.reason,
                    f.created_at,
                    COALESCE(fd.message_id, m.message_id) AS message_id,
                    m.model,
                    c.conversation_id,
                    c.title AS conversation_title,
                    fd.content_source,
                    fd.question_text AS question,
                    fd.answer_text AS answer,
                    fd.comment_text AS "comment",
                    fd.corrected_answer_text AS corrected_answer,
                    fd.citations_json,
                    DBMS_LOB.SUBSTR(fd.question_text, 240, 1) AS question_preview,
                    DBMS_LOB.SUBSTR(fd.comment_text, 160, 1) AS comment_preview,
                    CASE WHEN fd.comment_text IS NULL THEN 0 ELSE 1 END AS has_comment,
                    a.outcome,
                    a.search_mode,
                    COALESCE(a.elapsed_ms, m.elapsed_ms) AS elapsed_ms,
                    a.retrieved_count,
                    a.reranked_count,
                    a.citation_count,
                    a.config_fingerprint
                FROM rag_citation_feedback f
                LEFT JOIN rag_feedback_details fd ON fd.feedback_id = f.feedback_id
                LEFT JOIN rag_search_answer_profiles bv
                  ON bv.search_answer_profile_id = f.search_answer_profile_id
                 AND NVL(bv.tenant_id_hash, '__GLOBAL__') = NVL(f.tenant_id_hash, '__GLOBAL__')
                OUTER APPLY (
                    SELECT am.message_id, am.conversation_id, am.model, am.elapsed_ms
                    FROM rag_messages am
                    WHERE am.role = 'ASSISTANT'
                      AND NVL(am.tenant_id_hash, '__GLOBAL__') = NVL(f.tenant_id_hash, '__GLOBAL__')
                      AND NVL(am.user_id_hash, '__NONE__') = NVL(f.user_id_hash, '__NONE__')
                      AND (
                            (fd.message_id IS NOT NULL AND am.message_id = fd.message_id)
                            OR (fd.message_id IS NULL AND am.trace_id = f.trace_id)
                          )
                    ORDER BY am.created_at DESC, am.message_id DESC
                    FETCH FIRST 1 ROWS ONLY
                ) m
                LEFT JOIN rag_conversations c
                  ON c.conversation_id = m.conversation_id
                 AND NVL(c.tenant_id_hash, '__GLOBAL__') = NVL(f.tenant_id_hash, '__GLOBAL__')
                LEFT JOIN rag_documents d
                  ON d.document_id = f.document_id
                 AND NVL(d.tenant_id_hash, '__GLOBAL__') = NVL(f.tenant_id_hash, '__GLOBAL__')
                OUTER APPLY (
                    SELECT sa.*
                    FROM rag_search_audit sa
                    WHERE sa.trace_id = f.trace_id
                      AND NVL(sa.tenant_id_hash, '__GLOBAL__') = NVL(f.tenant_id_hash, '__GLOBAL__')
                      AND NVL(sa.user_id_hash, '__NONE__') = NVL(f.user_id_hash, '__NONE__')
                    ORDER BY sa.created_at DESC, sa.audit_id DESC
                    FETCH FIRST 1 ROWS ONLY
                ) a
                WHERE f.feedback_id = :feedback_id
                  AND {feedback_tenant_sql}
                  AND {feedback_scope_sql}
                """,
                feedback_tenant_sql=_oracle_tenant_predicate(alias="f"),
                feedback_scope_sql=" AND ".join(
                    [
                        *_search_answer_profile_scope_predicates("f.search_answer_profile_id"),
                        *owner_predicates,
                    ]
                    or ["1 = 1"]
                ),
            ),
            _with_search_answer_profile_scope_bind(
                _with_tenant_bind({"feedback_id": feedback_id, **owner_binds})
            ),
        )
        if row is None:
            return None
        result = dict(row)
        result["citations"] = _json_object_list(result.pop("citations_json", None))
        result["execution"] = {
            "outcome": result.pop("outcome", None),
            "search_mode": result.pop("search_mode", None),
            "elapsed_ms": result.pop("elapsed_ms", None),
            "retrieved_count": result.pop("retrieved_count", None),
            "reranked_count": result.pop("reranked_count", None),
            "citation_count": result.pop("citation_count", None),
            "config_fingerprint": result.pop("config_fingerprint", None),
        }
        return result

    async def save_evaluation_artifact(self, artifact: Mapping[str, object]) -> str:
        """nightly / staging 評価 artifact を query 原文なしで Oracle へ保存する。"""
        evaluation_run_id = _audit_str(artifact, "evaluation_run_id", uuid4().hex)
        binds = _evaluation_artifact_binds(artifact, evaluation_run_id=evaluation_run_id)
        await self._run_transaction(
            lambda connection: _execute(
                connection,
                """
                INSERT INTO rag_evaluation_runs (
                    evaluation_run_id,
                    tenant_id_hash,
                    knowledge_base_ids,
                    request_json,
                    result_json,
                    result_sha256,
                    passed
                ) VALUES (
                    :evaluation_run_id,
                    :tenant_id_hash,
                    :knowledge_base_ids,
                    :request_json,
                    :result_json,
                    :result_sha256,
                    :passed
                )
                """,
                binds,
            )
        )
        return evaluation_run_id

    def is_connection_configured(self) -> bool:
        """実 DB に接続する設定（または明示の pool）があるかを返す。"""
        return _oracle_connection_configured(self)

    async def get_vector_index_build_params(self) -> tuple[int, int] | None:
        """主検索索引の実際の (NEIGHBORS, EFCONSTRUCTION) を返す(#562)。

        ``v$vector_graph_index`` を読む。接続設定がない・V$ を読む権限がない・索引がない
        (メモリ上のグラフがない)場合は None(確認できない)を返し、例外にしない。
        """
        if not _oracle_connection_configured(self):
            return None
        try:
            row = await self._db_call_runner(
                lambda: self._run_with_connection(
                    lambda connection: _fetch_one(
                        connection,
                        """
                        SELECT num_neighbors, ef_construction
                          FROM v$vector_graph_index
                         WHERE owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')
                           AND index_name = :index_name
                         FETCH FIRST 1 ROWS ONLY
                        """,
                        {"index_name": RAG_CHUNKS_VECTOR_INDEX_NAME},
                    )
                )
            )
        except Exception as exc:
            logger.warning(
                "vector_index_build_params_unavailable", extra=oracle_error_log_fields(exc)
            )
            return None
        if row is None or row.get("num_neighbors") is None or row.get("ef_construction") is None:
            return None
        return int(cast(int, row["num_neighbors"])), int(cast(int, row["ef_construction"]))

    async def create_evaluation_job(self, job: Mapping[str, object]) -> None:
        """品質評価の job を RUNNING で作る（#390）。heartbeat の時刻は DB の時計にする。"""

        await self._run_transaction(
            lambda connection: _execute(
                connection,
                """
                INSERT INTO rag_evaluation_jobs (
                    job_id,
                    kind,
                    status,
                    tenant_id_hash,
                    user_id_hash,
                    total_cases,
                    completed_cases,
                    lease_owner,
                    heartbeat_at,
                    time_limit_seconds,
                    created_at,
                    started_at,
                    updated_at
                ) VALUES (
                    :job_id,
                    :kind,
                    'RUNNING',
                    :tenant_id_hash,
                    :user_id_hash,
                    :total_cases,
                    0,
                    :lease_owner,
                    SYSTIMESTAMP,
                    :time_limit_seconds,
                    SYSTIMESTAMP,
                    SYSTIMESTAMP,
                    SYSTIMESTAMP
                )
                """,
                {
                    "job_id": job["job_id"],
                    "kind": job["kind"],
                    "tenant_id_hash": job.get("tenant_id_hash"),
                    "user_id_hash": job.get("user_id_hash"),
                    "total_cases": job["total_cases"],
                    "lease_owner": job["lease_owner"],
                    "time_limit_seconds": job["time_limit_seconds"],
                },
            )
        )

    async def get_evaluation_job(
        self,
        job_id: str,
        *,
        tenant_id_hash: str | None,
        user_id_hash: str | None,
    ) -> dict[str, object] | None:
        """投入した利用者（tenant と user の hash）の品質評価の job を 1 件返す。"""
        return await self._fetch_one(
            f"""
            SELECT
                job_id,
                kind,
                status,
                total_cases,
                completed_cases,
                current_case_id,
                current_case_started_at,
                time_limit_seconds,
                error_message,
                result_json,
                evaluation_run_id,
                created_at,
                started_at,
                finished_at,
                heartbeat_at
            FROM rag_evaluation_jobs
            WHERE job_id = :job_id
              AND {_EVALUATION_JOB_OWNER_SQL}
            """,
            {"job_id": job_id, "tenant_id_hash": tenant_id_hash, "user_id_hash": user_id_hash},
        )

    async def update_evaluation_job_progress(
        self,
        job_id: str,
        *,
        lease_owner: str,
        completed_cases: int,
        current_case_id: str | None,
    ) -> bool:
        """自分の lease の RUNNING job の進捗と heartbeat を更新する。更新できなければ False。

        取り消し・時間切れの回復で RUNNING でなくなった job には書かない。
        """

        def operation(connection: OracleConnectionProtocol) -> int:
            return _execute_count(
                connection,
                """
                UPDATE rag_evaluation_jobs
                SET completed_cases = :completed_cases,
                    current_case_id = :current_case_id,
                    current_case_started_at =
                        CASE WHEN :case_started = 1 THEN SYSTIMESTAMP ELSE NULL END,
                    heartbeat_at = SYSTIMESTAMP,
                    updated_at = SYSTIMESTAMP
                WHERE job_id = :job_id
                  AND status = 'RUNNING'
                  AND lease_owner = :lease_owner
                """,
                {
                    "job_id": job_id,
                    "lease_owner": lease_owner,
                    "completed_cases": completed_cases,
                    "current_case_id": current_case_id,
                    "case_started": 1 if current_case_id is not None else 0,
                },
            )

        return await self._run_transaction(operation) > 0

    async def heartbeat_evaluation_job(self, job_id: str, *, lease_owner: str) -> bool:
        """自分の lease の RUNNING job の heartbeat を DB の時刻で更新する。

        更新できなければ（RUNNING でなくなった） False。
        """

        def operation(connection: OracleConnectionProtocol) -> int:
            return _execute_count(
                connection,
                """
                UPDATE rag_evaluation_jobs
                SET heartbeat_at = SYSTIMESTAMP
                WHERE job_id = :job_id
                  AND status = 'RUNNING'
                  AND lease_owner = :lease_owner
                """,
                {"job_id": job_id, "lease_owner": lease_owner},
            )

        return await self._run_transaction(operation) > 0

    async def finish_evaluation_job(
        self,
        job_id: str,
        *,
        lease_owner: str,
        status: str,
        result: Mapping[str, object] | None,
        error_message: str | None,
        evaluation_run_id: str | None,
    ) -> bool:
        """自分の lease の RUNNING job を終わらせる（成功・失敗）。書けなければ False。"""

        def operation(connection: OracleConnectionProtocol) -> int:
            cursor_count = _execute_count_with_input_sizes(
                connection,
                """
                UPDATE rag_evaluation_jobs
                SET status = :status,
                    result_json = :result_json,
                    error_message = :error_message,
                    evaluation_run_id = :evaluation_run_id,
                    completed_cases = CASE
                        WHEN :status = 'SUCCEEDED' THEN total_cases
                        ELSE completed_cases
                    END,
                    current_case_id = NULL,
                    current_case_started_at = NULL,
                    lease_owner = NULL,
                    finished_at = SYSTIMESTAMP,
                    updated_at = SYSTIMESTAMP
                WHERE job_id = :job_id
                  AND status = 'RUNNING'
                  AND lease_owner = :lease_owner
                """,
                {
                    "job_id": job_id,
                    "lease_owner": lease_owner,
                    "status": status,
                    "result_json": _json_bind(dict(result)) if result is not None else None,
                    "error_message": _truncate_text(error_message, 2000),
                    "evaluation_run_id": evaluation_run_id,
                },
                input_sizes=_json_input_sizes("result_json"),
            )
            return cursor_count

        return await self._run_transaction(operation) > 0

    async def cancel_evaluation_job(
        self,
        job_id: str,
        *,
        tenant_id_hash: str | None,
        user_id_hash: str | None,
        error_message: str,
    ) -> bool:
        """投入した利用者の RUNNING job を取り消す。取り消せなければ False。

        実行中のプロセスは、次の heartbeat か進捗の更新で RUNNING でなくなったことを知り、
        評価を止める（別のプロセスが実行している場合も止まる）。
        """

        def operation(connection: OracleConnectionProtocol) -> int:
            return _execute_count(
                connection,
                f"""
                UPDATE rag_evaluation_jobs
                SET status = 'CANCELLED',
                    error_message = :error_message,
                    current_case_id = NULL,
                    current_case_started_at = NULL,
                    lease_owner = NULL,
                    finished_at = SYSTIMESTAMP,
                    updated_at = SYSTIMESTAMP
                WHERE job_id = :job_id
                  AND status = 'RUNNING'
                  AND {_EVALUATION_JOB_OWNER_SQL}
                """,
                {
                    "job_id": job_id,
                    "tenant_id_hash": tenant_id_hash,
                    "user_id_hash": user_id_hash,
                    "error_message": _truncate_text(error_message, 2000),
                },
            )

        return await self._run_transaction(operation) > 0

    async def fail_stale_evaluation_jobs(
        self,
        *,
        stale_seconds: float,
        error_message: str,
        lease_owner: str | None = None,
    ) -> int:
        """heartbeat が途絶えた RUNNING job を失敗にする（DB の時計で判定する）。

        `lease_owner` を渡すと、heartbeat にかかわらずその lease の job だけを失敗にする
        （backend の停止で、実行中の評価を打ち切ったとき）。
        """
        if lease_owner is not None:
            condition = "lease_owner = :lease_owner"
            binds: dict[str, object] = {"lease_owner": lease_owner}
        else:
            condition = (
                "(heartbeat_at IS NULL "
                "OR heartbeat_at < SYSTIMESTAMP - NUMTODSINTERVAL(:stale_seconds, 'SECOND'))"
            )
            binds = {"stale_seconds": float(stale_seconds)}

        def operation(connection: OracleConnectionProtocol) -> int:
            return _execute_count(
                connection,
                f"""
                UPDATE rag_evaluation_jobs
                SET status = 'FAILED',
                    error_message = :error_message,
                    current_case_id = NULL,
                    current_case_started_at = NULL,
                    lease_owner = NULL,
                    finished_at = SYSTIMESTAMP,
                    updated_at = SYSTIMESTAMP
                WHERE status = 'RUNNING'
                  AND {condition}
                """,
                {**binds, "error_message": _truncate_text(error_message, 2000)},
            )

        return await self._run_transaction(operation)

    async def count_running_evaluation_jobs(self) -> int:
        """実行中（RUNNING）の品質評価の job の数を返す（同時に動かす数の上限の判定）。"""
        row = await self._fetch_one(
            "SELECT COUNT(*) AS running_count FROM rag_evaluation_jobs WHERE status = 'RUNNING'"
        )
        return int(cast(int, row.get("running_count") or 0)) if row else 0

    async def purge_evaluation_jobs(self, *, retention_days: int) -> int:
        """保持期間を過ぎた、終わった品質評価の job を消す（0 以下は無期限で消さない）。"""
        if retention_days <= 0:
            return 0

        def operation(connection: OracleConnectionProtocol) -> int:
            return _execute_count(
                connection,
                """
                DELETE FROM rag_evaluation_jobs
                WHERE status <> 'RUNNING'
                  AND created_at < SYSTIMESTAMP - NUMTODSINTERVAL(:retention_days, 'DAY')
                """,
                {"retention_days": int(retention_days)},
            )

        return await self._run_transaction(operation)

    async def _read_vector_branch(
        self,
        embedding_bind: object,
        *,
        top_k: int,
        filters: dict[str, str],
        into: list[tuple[int, dict[str, object]]],
    ) -> None:
        """vector の検索 1 本の行を (順位, 行) で ``into`` に足す(``search_many`` の branch)。"""
        for rank, row in enumerate(
            await self._vector_rows(embedding_bind, top_k=top_k, filters=filters), start=1
        ):
            into.append((rank, row))

    async def _read_keyword_branch(
        self,
        text_query: str,
        *,
        top_k: int,
        filters: dict[str, str],
        into: list[tuple[int, dict[str, object]]],
    ) -> None:
        """keyword の検索 1 本の行を (順位, 行) で ``into`` に足す(``search_many`` の branch)。"""
        for rank, row in enumerate(
            await self._keyword_rows(text_query, top_k=top_k, filters=filters), start=1
        ):
            into.append((rank, row))

    async def _vector_rows(
        self, embedding_bind: object, *, top_k: int, filters: dict[str, str]
    ) -> list[dict[str, object]]:
        """vector の検索 1 本の行(``_SEARCH_HIT_COLUMNS`` と ``score``。近い順)。"""
        statement, binds = vector_search_statement(
            self._settings, embedding_bind, top_k=top_k, filters=filters
        )
        with read_rows_per_round_trip(_SEARCH_FETCH_ROWS):
            return await self._fetch_all(statement, binds)

    async def _vector_search_with_oracle(
        self, embedding: list[float], top_k: int, filters: dict[str, str]
    ) -> list[RetrievedChunk]:
        """Oracle AI Vector Search で近傍 chunk を取得する。"""
        rows = await self._vector_rows(_to_vector_bind(embedding), top_k=top_k, filters=filters)
        return [
            _with_retrieval_metadata(
                chunk,
                retrieval_mode="vector",
                vector_rank=rank,
                vector_score=round(_float_value(row.get("score", 0.0)), 6),
            )
            for rank, (chunk, row) in enumerate(_distinct_text_hits(rows), start=1)
        ]

    async def _keyword_rows(
        self, text_query: str, *, top_k: int, filters: dict[str, str]
    ) -> list[dict[str, object]]:
        """keyword の検索 1 本の行(``_SEARCH_HIT_COLUMNS`` と ``score``。score の高い順)。"""
        where_sql, binds = _oracle_retrieval_where(filters)
        binds.update({"query": text_query, "top_k": top_k})
        # 上位の行を命中の列だけで決め、本文・metadata はその行だけ読む(#2137)。
        with read_rows_per_round_trip(_SEARCH_FETCH_ROWS):
            return await self._fetch_all(
                _render_sql(
                    """
                    SELECT {columns}, kw.score
                    FROM ({ranked}) kw
                    JOIN rag_chunks c ON c.chunk_id = kw.chunk_id
                    JOIN rag_documents d ON d.document_id = c.document_id
                    LEFT JOIN rag_chunk_sets cs ON cs.chunk_set_id = c.chunk_set_id
                    LEFT JOIN rag_document_recipes r ON r.recipe_id = cs.recipe_id
                    ORDER BY kw.sort_key DESC, c.document_id ASC, c.chunk_index ASC, c.chunk_id ASC
                    """,
                    columns=_SEARCH_HIT_COLUMNS,
                    ranked=_keyword_search_sql(
                        select_list=(
                            "c.chunk_id, c.text_score AS sort_key, c.text_score / 100 AS score"
                        ),
                        query_bind="query",
                        where_sql=where_sql,
                    ),
                ),
                binds,
            )

    async def _keyword_search_with_oracle(
        self, query: str, top_k: int, filters: dict[str, str]
    ) -> list[RetrievedChunk]:
        """Oracle Text で keyword chunk を取得する。"""
        text_query = _oracle_text_query(query, settings=self._settings)
        if text_query is None:
            return []
        rows = await self._keyword_rows(text_query, top_k=top_k, filters=filters)
        return [
            _with_retrieval_metadata(
                chunk,
                retrieval_mode="keyword",
                keyword_rank=rank,
                keyword_score=round(_float_value(row.get("score", 0.0)), 6),
            )
            for rank, (chunk, row) in enumerate(_distinct_text_hits(rows), start=1)
        ]

    async def _context_group_siblings_with_oracle(
        self,
        anchors: list[RetrievedChunk],
        *,
        max_chunks_per_group: int,
        neighbor_child_count: int | None = None,
    ) -> list[RetrievedChunk]:
        """親子の出典復元と非親子の前後補完に使う原文を、保存された成果物から読む。

        親子は同じ親 group、非親子は同じ chunk_set の前後を独立した上限で読む。

        chunk_set を持つ起点は ``_CONTEXT_ANCHOR_BATCH_SIZE`` 件ずつ 1 回の SQL にまとめ(#1490)、
        sibling の本文は chunk ごとに 1 回だけ読む(#1498)。
        起点ごとの条件・並び・件数は 1 件ずつ読むとき(``_context_group_siblings_for_anchor``)と
        同じで、返す順も起点の順・起点ごとの並びのまま。chunk_set の無い起点(レシピ導入前の文書)
        だけは 1 件ずつ読む。
        """
        if max_chunks_per_group <= 0:
            return []
        targets: list[tuple[RetrievedChunk, str, int]] = []
        for anchor in anchors:
            parent_group = _chunk_group_id_from_retrieved(anchor)
            if anchor.metadata.get("chunk_strategy") == "small_to_big" and parent_group is None:
                continue
            group_id = (
                parent_group if anchor.metadata.get("chunk_strategy") == "small_to_big" else ""
            )
            anchor_index = _chunk_index_from_retrieved(anchor)
            if group_id is None or anchor_index is None:
                continue
            targets.append((anchor, group_id, anchor_index))
        rows_by_target: dict[int, list[dict[str, object]]] = {}
        batched = [
            (position, target)
            for position, target in enumerate(targets)
            if _metadata_str(target[0].metadata.get("chunk_set_id")) is not None
        ]
        batches = [
            batched[start : start + _CONTEXT_ANCHOR_BATCH_SIZE]
            for start in range(0, len(batched), _CONTEXT_ANCHOR_BATCH_SIZE)
        ]
        # 起点が 1 回の SQL の上限を超えたときの複数回の SQL は、並行に読む(#3461)。
        with read_rows_per_round_trip(_SEARCH_FETCH_ROWS):
            batch_rows = await asyncio.gather(
                *(
                    self._fetch_all(
                        *_context_group_siblings_batch_sql(
                            [target for _position, target in batch],
                            max_chunks_per_group=max_chunks_per_group,
                            neighbor_child_count=neighbor_child_count,
                        )
                    )
                    for batch in batches
                )
            )
        for batch, rows in zip(batches, batch_rows, strict=True):
            ranked: dict[int, list[tuple[int, dict[str, object]]]] = {}
            for row in rows:
                for anchor_ord, context_rank in _anchor_positions(row.get("anchor_positions")):
                    position = batch[anchor_ord][0]
                    ranked.setdefault(position, []).append((context_rank, row))
            for position, pairs in ranked.items():
                rows_by_target[position] = [
                    row
                    for _rank, row in sorted(
                        pairs,
                        key=lambda pair: (
                            pair[0],
                            _int_value(pair[1].get("chunk_index")),
                            str(pair[1].get("chunk_id") or ""),
                        ),
                    )
                ]
        for position, (anchor, group_id, anchor_index) in enumerate(targets):
            if _metadata_str(anchor.metadata.get("chunk_set_id")) is None:
                rows_by_target[position] = await self._context_group_siblings_for_anchor(
                    anchor,
                    group_id=group_id,
                    anchor_index=anchor_index,
                    max_chunks_per_group=max_chunks_per_group,
                    neighbor_child_count=neighbor_child_count,
                )
        siblings: list[RetrievedChunk] = []
        for position, (anchor, group_id, anchor_index) in enumerate(targets):
            for row in rows_by_target.get(position, ()):
                sibling = _retrieved_chunk_from_row(row).model_copy(update={"score": anchor.score})
                sibling_index = _chunk_index_from_retrieved(sibling)
                if sibling_index is None:
                    continue
                siblings.append(
                    _with_context_group_metadata(
                        sibling,
                        anchor=anchor,
                        group_id=group_id,
                        distance=sibling_index - anchor_index,
                    )
                )
        return siblings

    async def _context_group_siblings_for_anchor(
        self,
        anchor: RetrievedChunk,
        *,
        group_id: str,
        anchor_index: int,
        max_chunks_per_group: int,
        neighbor_child_count: int | None = None,
    ) -> list[dict[str, object]]:
        """1 つの起点と同じ chunk group の sibling の行を、起点に近い順に読む。"""
        where_sql, binds = _context_anchor_retrieval_where(anchor)
        binds.update(
            {
                "chunk_group_id": group_id,
                "anchor_index": anchor_index,
                "anchor_chunk_id": anchor.chunk_id,
                "max_chunks_per_group": max_chunks_per_group,
                "neighbor_child_count": (
                    (max_chunks_per_group + 1) // 2
                    if neighbor_child_count is None
                    else max(0, neighbor_child_count)
                ),
            }
        )
        return await self._fetch_all(
            _render_sql(
                """
            SELECT *
            FROM (
                SELECT
                    c.document_id,
                    c.chunk_id,
                    c.chunk_text,
                    c.metadata_json,
                    c.chunk_index,
                    c.chunk_set_id,
                    d.file_name,
                    d.category_name,
                    d.superseded_by_document_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY CASE WHEN :chunk_group_id IS NOT NULL
                                     OR c.chunk_index < :anchor_index THEN 0 ELSE 1 END
                        ORDER BY ABS(c.chunk_index - :anchor_index), c.chunk_index, c.chunk_id
                    ) AS context_rank,
                    0 AS score
                FROM rag_chunks c
                JOIN rag_documents d ON d.document_id = c.document_id
                WHERE {where_sql}
                  AND (:chunk_group_id IS NULL
                       OR JSON_VALUE(c.metadata_json, '$.chunk_group_id') = :chunk_group_id)
                  AND c.chunk_id <> :anchor_chunk_id
                ORDER BY
                    ABS(c.chunk_index - :anchor_index) ASC,
                    c.chunk_index ASC,
                    c.chunk_id ASC
            )
            WHERE context_rank <= CASE WHEN :chunk_group_id IS NULL
                                      THEN :neighbor_child_count
                                      ELSE :max_chunks_per_group END
            """,
                where_sql=where_sql,
            ),
            binds,
        )

    async def _create_document_with_oracle(
        self,
        file_name: str,
        object_storage_path: str,
        content_type: str | None,
        file_size_bytes: int | None,
        content_sha256: str | None,
        duplicate_of_document_id: str | None,
        knowledge_base_ids: Sequence[str] | None,
    ) -> DocumentDetail:
        """Oracle document table へ文書行を作成する。"""
        document_id = uuid4().hex
        uploaded_at = datetime.now(UTC)
        document = StoredDocument(
            id=document_id,
            file_name=file_name,
            status=FileStatus.UPLOADED,
            uploaded_at=uploaded_at,
            object_storage_path=object_storage_path,
            content_type=content_type,
            file_size_bytes=file_size_bytes,
            content_sha256=content_sha256,
            duplicate_of_document_id=duplicate_of_document_id,
            tenant_id_hash=_current_tenant_id_hash(),
        )
        requested_knowledge_base_ids = _unique_optional_sequence(knowledge_base_ids or [])

        def operation(connection: OracleConnectionProtocol) -> DocumentDetail:
            knowledge_bases = (
                [
                    _require_active_knowledge_base(connection, knowledge_base_id)
                    for knowledge_base_id in requested_knowledge_base_ids
                ]
                if requested_knowledge_base_ids
                else [_ensure_default_knowledge_base(connection, DEFAULT_KNOWLEDGE_BASE_NAME)]
            )
            _execute(
                connection,
                """
                INSERT INTO rag_documents (
                    document_id,
                    file_name,
                    status,
                    tenant_id_hash,
                    object_storage_path,
                    content_type,
                    file_size_bytes,
                    content_sha256,
                    duplicate_of_document_id,
                    uploaded_at
                ) VALUES (
                    :document_id,
                    :file_name,
                    :status,
                    :tenant_id_hash,
                    :object_storage_path,
                    :content_type,
                    :file_size_bytes,
                    :content_sha256,
                    :duplicate_of_document_id,
                    :uploaded_at
                )
                """,
                _document_binds(document),
            )
            _insert_document_knowledge_base_rows(
                connection,
                document_id=document.id,
                knowledge_base_ids=[knowledge_base.id for knowledge_base in knowledge_bases],
            )
            # レシピ1は登録と同じ transaction で作る。GET /recipes では作らない(#341)。
            _merge_default_document_recipe(
                connection,
                _default_document_recipe_values(
                    {
                        "document_id": document.id,
                        "tenant_id_hash": document.tenant_id_hash,
                        "status": document.status.value,
                        "error_message": None,
                        "uploaded_at": document.uploaded_at,
                        "indexed_at": None,
                    }
                ),
            )
            return _to_document_detail(document).model_copy(
                update={
                    "knowledge_bases": [
                        _to_knowledge_base_ref(knowledge_base) for knowledge_base in knowledge_bases
                    ]
                }
            )

        return await self._run_transaction(operation)

    async def _create_knowledge_base_with_oracle(
        self,
        *,
        name: str,
        description: str | None,
        default_search_mode: SearchMode,
        retrieval_config: dict[str, object],
    ) -> KnowledgeBaseDetail:
        """Oracle knowledge base table へ行を作成する。"""
        now = datetime.now(UTC)
        knowledge_base = StoredKnowledgeBase(
            id=uuid4().hex,
            tenant_id_hash=_current_tenant_id_hash(),
            name=name,
            description=description,
            status=KnowledgeBaseStatus.ACTIVE,
            default_search_mode=default_search_mode,
            retrieval_config=retrieval_config,
            created_at=now,
            updated_at=now,
        )

        def operation(connection: OracleConnectionProtocol) -> KnowledgeBaseDetail:
            # 名前の一意制約は tenant 全体（利用者の KB 範囲外・アーカイブ済みを含む）なので、
            # tenant だけで探して 409 にする。
            if _select_knowledge_base_by_name(connection, name) is not None:
                raise KnowledgeBaseNameConflictError()
            with _knowledge_base_name_conflict_guard():
                _insert_knowledge_base(connection, knowledge_base)
            return _to_knowledge_base_detail(knowledge_base)

        return await self._run_transaction(operation)

    async def _list_knowledge_bases_with_oracle(
        self,
        *,
        status: KnowledgeBaseStatus | None,
        query: str | None,
        limit: int | None,
        offset: int,
        knowledge_base_ids: Sequence[str] | None = None,
    ) -> list[KnowledgeBaseSummary]:
        """Oracle knowledge base table から一覧取得する。"""
        where_sql, binds = _oracle_knowledge_base_where(
            status=status, query=query, knowledge_base_ids=knowledge_base_ids
        )
        binds.update(offset_fetch_binds(offset=offset, limit=limit))
        paging_sql = offset_fetch_clause(with_limit=limit is not None)
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT
                kb.knowledge_base_id,
                kb.tenant_id_hash,
                kb.name,
                kb.description,
                kb.status,
                kb.default_search_mode,
                kb.retrieval_config,
                kb.created_at,
                kb.updated_at,
                kb.archived_at,
                COUNT(DISTINCT dkb.document_id) AS document_count,
                COUNT(DISTINCT CASE WHEN d.status = 'INDEXED' THEN d.document_id END)
                    AS indexed_document_count,
                COUNT(DISTINCT CASE WHEN d.status = 'ERROR' THEN d.document_id END)
                    AS error_document_count,
                COUNT(c.chunk_id) AS searchable_chunk_count
            FROM rag_knowledge_bases kb
            LEFT JOIN rag_document_knowledge_bases dkb
                ON dkb.knowledge_base_id = kb.knowledge_base_id
            LEFT JOIN rag_documents d
                ON d.document_id = dkb.document_id
               AND {document_access_sql}
            LEFT JOIN rag_chunks c
                ON c.document_id = d.document_id
               AND d.status = 'INDEXED'
            WHERE {where_sql}
            GROUP BY
                kb.knowledge_base_id,
                kb.tenant_id_hash,
                kb.name,
                kb.description,
                kb.status,
                kb.default_search_mode,
                kb.retrieval_config,
                kb.created_at,
                kb.updated_at,
                kb.archived_at
            ORDER BY
                CASE WHEN UPPER(kb.name) = 'DEFAULT' THEN 0 ELSE 1 END,
                {name_order_sql},
                NLSSORT(kb.name, 'NLS_SORT=JAPANESE_M'),
                kb.name ASC,
                kb.knowledge_base_id ASC
            {paging_sql}
            """,
                where_sql=where_sql,
                document_access_sql=_oracle_access_predicate_sql(alias="d"),
                paging_sql=paging_sql,
                name_order_sql=_oracle_natural_name_order_sql("kb.name"),
            ),
            binds,
        )
        return [_to_knowledge_base_summary(_stored_knowledge_base_from_row(row)) for row in rows]

    async def _count_knowledge_bases_with_oracle(
        self,
        *,
        status: KnowledgeBaseStatus | None,
        query: str | None,
        knowledge_base_ids: Sequence[str] | None = None,
    ) -> int:
        """Oracle knowledge base table の件数を取得する。"""
        where_sql, binds = _oracle_knowledge_base_where(
            status=status, query=query, knowledge_base_ids=knowledge_base_ids
        )
        row = await self._fetch_one(
            _render_sql(
                """
            SELECT COUNT(*) AS count_value
            FROM rag_knowledge_bases kb
            WHERE {where_sql}
            """,
                where_sql=where_sql,
            ),
            binds,
        )
        return _row_count_value(row)

    async def _get_knowledge_base_with_oracle(
        self,
        knowledge_base_id: str,
    ) -> KnowledgeBaseDetail | None:
        """Oracle knowledge base table から詳細取得する。"""
        rows = await self._fetch_all(
            """
            SELECT
                kb.knowledge_base_id,
                kb.tenant_id_hash,
                kb.name,
                kb.description,
                kb.status,
                kb.default_search_mode,
                kb.retrieval_config,
                kb.created_at,
                kb.updated_at,
                kb.archived_at,
                COUNT(DISTINCT dkb.document_id) AS document_count,
                COUNT(DISTINCT CASE WHEN d.status = 'INDEXED' THEN d.document_id END)
                    AS indexed_document_count,
                COUNT(DISTINCT CASE WHEN d.status = 'ERROR' THEN d.document_id END)
                    AS error_document_count,
                COUNT(c.chunk_id) AS searchable_chunk_count
            FROM rag_knowledge_bases kb
            LEFT JOIN rag_document_knowledge_bases dkb
                ON dkb.knowledge_base_id = kb.knowledge_base_id
            LEFT JOIN rag_documents d
                ON d.document_id = dkb.document_id
               AND {document_access_sql}
            LEFT JOIN rag_chunks c
                ON c.document_id = d.document_id
               AND d.status = 'INDEXED'
            WHERE kb.knowledge_base_id = :knowledge_base_id
              AND {knowledge_base_access_sql}
            GROUP BY
                kb.knowledge_base_id,
                kb.tenant_id_hash,
                kb.name,
                kb.description,
                kb.status,
                kb.default_search_mode,
                kb.retrieval_config,
                kb.created_at,
                kb.updated_at,
                kb.archived_at
            """.format(
                document_access_sql=_oracle_access_predicate_sql(alias="d"),
                knowledge_base_access_sql=_oracle_knowledge_base_access_predicate_sql(alias="kb"),
            ),
            _with_tenant_bind({"knowledge_base_id": knowledge_base_id}),
        )
        if not rows:
            return None
        return _to_knowledge_base_detail(_stored_knowledge_base_from_row(rows[0]))

    async def _update_knowledge_base_with_oracle(
        self,
        *,
        knowledge_base_id: str,
        name: str | None,
        description: str | None,
        default_search_mode: SearchMode | None,
        retrieval_config: dict[str, object] | None,
        update_fields: set[str] | None,
    ) -> KnowledgeBaseDetail:
        """Oracle knowledge base table を更新する。"""
        fields = update_fields or {
            field_name
            for field_name, value in {
                "name": name,
                "description": description,
                "default_search_mode": default_search_mode,
                "retrieval_config": retrieval_config,
            }.items()
            if value is not None
        }

        def operation(connection: OracleConnectionProtocol) -> KnowledgeBaseDetail:
            # アーカイブ済みの KB は名前・説明も変えない（画面の読み取り専用と同じ。#961）。
            existing = _require_active_knowledge_base(connection, knowledge_base_id)
            if (
                "name" in fields
                and name is not None
                and existing.name.casefold() == DEFAULT_KNOWLEDGE_BASE_NAME.casefold()
            ):
                raise ValueError("DEFAULT ナレッジベースの名前は変更できません。")
            if (
                "name" in fields
                and name is not None
                and name.casefold() != existing.name.casefold()
            ):
                conflicting = _select_knowledge_base_by_name(connection, name)
                if conflicting is not None and conflicting.id != existing.id:
                    raise KnowledgeBaseNameConflictError()
            updated = existing
            now = datetime.now(UTC)
            if fields:
                updated = existing
                if "name" in fields and name is not None:
                    updated = updated_copy_knowledge_base(updated, name=name)
                if "description" in fields:
                    updated = updated_copy_knowledge_base(updated, description=description)
                if "default_search_mode" in fields and default_search_mode is not None:
                    updated = updated_copy_knowledge_base(
                        updated,
                        default_search_mode=default_search_mode,
                    )
                if "retrieval_config" in fields:
                    updated = updated_copy_knowledge_base(
                        updated,
                        retrieval_config=retrieval_config or {},
                    )
                updated = updated_copy_knowledge_base(updated, updated_at=now)
                with _knowledge_base_name_conflict_guard():
                    _execute(
                        connection,
                        _render_sql(
                            """
                    UPDATE rag_knowledge_bases
                    SET
                        name = :name,
                        description = :description,
                        default_search_mode = :default_search_mode,
                        retrieval_config = :retrieval_config,
                        updated_at = :updated_at
                    WHERE knowledge_base_id = :knowledge_base_id
                      AND {knowledge_base_access_sql}
                    """,
                            knowledge_base_access_sql=(
                                _oracle_knowledge_base_access_predicate_sql()
                            ),
                        ),
                        _knowledge_base_binds(updated),
                    )
            return _to_knowledge_base_detail(updated)

        return await self._run_transaction(operation)

    async def _archive_knowledge_base_with_oracle(
        self,
        knowledge_base_id: str,
    ) -> KnowledgeBaseDetail:
        """Oracle knowledge base table の status を ARCHIVED にする。"""

        def operation(connection: OracleConnectionProtocol) -> KnowledgeBaseDetail:
            existing = _select_knowledge_base(connection, knowledge_base_id)
            if existing is None:
                raise KeyError(f"knowledge_base_id={knowledge_base_id} は存在しません。")
            if existing.name.casefold() == DEFAULT_KNOWLEDGE_BASE_NAME.casefold():
                raise ValueError("DEFAULT ナレッジベースはアーカイブできません。")
            now = datetime.now(UTC)
            archived = updated_copy_knowledge_base(
                existing,
                status=KnowledgeBaseStatus.ARCHIVED,
                updated_at=now,
                archived_at=now,
            )
            _execute(
                connection,
                _render_sql(
                    """
                UPDATE rag_knowledge_bases
                SET
                    status = :status,
                    updated_at = :updated_at,
                    archived_at = :archived_at
                WHERE knowledge_base_id = :knowledge_base_id
                  AND {knowledge_base_access_sql}
                """,
                    knowledge_base_access_sql=_oracle_knowledge_base_access_predicate_sql(),
                ),
                _knowledge_base_binds(archived),
            )
            return _to_knowledge_base_detail(archived)

        return await self._run_transaction(operation)

    async def _create_search_answer_profile_with_oracle(
        self,
        *,
        name: str,
        description: str | None,
        config: SearchAnswerProfileConfig,
    ) -> SearchAnswerProfileDetail:
        """Oracle search answer profile table へ行を作成する。"""
        now = datetime.now(UTC)
        view = StoredSearchAnswerProfile(
            id=uuid4().hex,
            tenant_id_hash=_current_tenant_id_hash(),
            name=name,
            description=description,
            status=SearchAnswerProfileStatus.ACTIVE,
            profile_config=dump_search_answer_profile_config(config),
            created_at=now,
            updated_at=now,
        )

        def operation(connection: OracleConnectionProtocol) -> SearchAnswerProfileDetail:
            _insert_search_answer_profile(connection, view)
            return _to_search_answer_profile_detail(view)

        return await self._run_transaction(operation)

    async def _list_search_answer_profiles_with_oracle(
        self,
        *,
        status: SearchAnswerProfileStatus | None,
        query: str | None,
        limit: int | None,
        offset: int,
        search_answer_profile_ids: Sequence[str] | None = None,
    ) -> list[StoredSearchAnswerProfile]:
        """Oracle search answer profile table から一覧取得する(集計前の保存値)。"""
        where_sql, binds = _oracle_search_answer_profile_where(
            status=status, query=query, search_answer_profile_ids=search_answer_profile_ids
        )
        binds.update(offset_fetch_binds(offset=offset, limit=limit))
        paging_sql = offset_fetch_clause(with_limit=limit is not None)
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT
                bv.search_answer_profile_id,
                bv.tenant_id_hash,
                bv.name,
                bv.description,
                bv.status,
                bv.profile_config,
                bv.created_at,
                bv.updated_at,
                bv.archived_at
            FROM rag_search_answer_profiles bv
            WHERE {where_sql}
            ORDER BY
                CASE WHEN UPPER(bv.name) = 'DEFAULT' THEN 0 ELSE 1 END,
                {name_order_sql},
                NLSSORT(bv.name, 'NLS_SORT=JAPANESE_M'),
                bv.name ASC,
                bv.search_answer_profile_id ASC
            {paging_sql}
            """,
                where_sql=where_sql,
                paging_sql=paging_sql,
                name_order_sql=_oracle_natural_name_order_sql("bv.name"),
            ),
            binds,
        )
        return [_stored_search_answer_profile_from_row(row) for row in rows]

    async def _count_search_answer_profiles_with_oracle(
        self,
        *,
        status: SearchAnswerProfileStatus | None,
        query: str | None,
        search_answer_profile_ids: Sequence[str] | None = None,
    ) -> int:
        """Oracle search answer profile table の件数を取得する。"""
        where_sql, binds = _oracle_search_answer_profile_where(
            status=status, query=query, search_answer_profile_ids=search_answer_profile_ids
        )
        row = await self._fetch_one(
            _render_sql(
                """
            SELECT COUNT(*) AS count_value
            FROM rag_search_answer_profiles bv
            WHERE {where_sql}
            """,
                where_sql=where_sql,
            ),
            binds,
        )
        return _row_count_value(row)

    async def _get_search_answer_profile_with_oracle(
        self,
        search_answer_profile_id: str,
    ) -> SearchAnswerProfileDetail | None:
        """Oracle search answer profile table から詳細取得する(KB 名は未解決)。"""
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT
                bv.search_answer_profile_id,
                bv.tenant_id_hash,
                bv.name,
                bv.description,
                bv.status,
                bv.profile_config,
                bv.created_at,
                bv.updated_at,
                bv.archived_at
            FROM rag_search_answer_profiles bv
            WHERE bv.search_answer_profile_id = :search_answer_profile_id
              AND {access_sql}
            """,
                access_sql=" AND ".join(
                    _oracle_search_answer_profile_access_predicates(alias="bv")
                ),
            ),
            _with_search_answer_profile_scope_bind(
                _with_tenant_bind({"search_answer_profile_id": search_answer_profile_id})
            ),
        )
        if not rows:
            return None
        return _to_search_answer_profile_detail(_stored_search_answer_profile_from_row(rows[0]))

    async def _update_search_answer_profile_with_oracle(
        self,
        *,
        search_answer_profile_id: str,
        name: str | None,
        description: str | None,
        config: SearchAnswerProfileConfig | None,
        update_fields: set[str] | None,
        language_pack_patch: dict[str, dict[str, object]] | None = None,
    ) -> SearchAnswerProfileDetail:
        """Oracle search answer profile table を更新する。"""
        fields = update_fields or {
            field_name
            for field_name, value in {
                "name": name,
                "description": description,
                "config": config,
            }.items()
            if value is not None
        }

        def operation(connection: OracleConnectionProtocol) -> SearchAnswerProfileDetail:
            requested_config = config
            # 部分更新と主フォームを直列にして、他の設定の同時保存を失わない。
            _execute(
                connection,
                _render_sql(
                    "SELECT search_answer_profile_id FROM rag_search_answer_profiles "
                    "WHERE search_answer_profile_id = :search_answer_profile_id "
                    "AND {access_sql} FOR UPDATE",
                    access_sql=" AND ".join(_oracle_search_answer_profile_access_predicates()),
                ),
                _with_search_answer_profile_scope_bind(
                    _with_tenant_bind({"search_answer_profile_id": search_answer_profile_id})
                ),
            )
            existing = _select_search_answer_profile(connection, search_answer_profile_id)
            if existing is None:
                raise KeyError(
                    f"search_answer_profile_id={search_answer_profile_id} は存在しません。"
                )
            if language_pack_patch is None and config is not None:
                from app.rag.question_understanding import question_understanding_overrides

                current_config = _to_search_answer_profile_detail(existing).config
                if question_understanding_overrides(
                    config.language_pack
                ) != question_understanding_overrides(current_config.language_pack):
                    raise ValueError(
                        "質問の理解の設定が変わりました。最新情報を取得して、専用のタブから保存してください。"
                    )
            if language_pack_patch is not None:
                from app.rag.question_understanding import replace_question_understanding

                current_config = _to_search_answer_profile_detail(existing).config
                requested_config = SearchAnswerProfileConfig.model_validate(
                    {
                        **current_config.model_dump(),
                        "language_pack": replace_question_understanding(
                            current_config.language_pack, language_pack_patch
                        ),
                    }
                )
            if existing.status != SearchAnswerProfileStatus.ACTIVE:
                # アーカイブ済みは編集・保存しない（画面の読み取り専用と同じ。#961）。
                raise ValueError(ARCHIVED_SEARCH_ANSWER_PROFILE_UPDATE_MESSAGE)
            is_default = existing.name.casefold() == DEFAULT_SEARCH_ANSWER_PROFILE_NAME.casefold()
            if is_default and "name" in fields:
                raise ValueError("DEFAULT 検索・回答プロファイルの名前は変更できません。")
            if is_default and "config" in fields and requested_config is not None:
                default_knowledge_base = _select_knowledge_base_by_name(
                    connection,
                    DEFAULT_KNOWLEDGE_BASE_NAME,
                )
                if default_knowledge_base is None or requested_config.knowledge_base_ids != [
                    default_knowledge_base.id
                ]:
                    raise ValueError("DEFAULT 検索・回答プロファイルの参照 KB は変更できません。")
            updated = existing
            now = datetime.now(UTC)
            if fields:
                if "name" in fields and name is not None:
                    updated = updated_copy_search_answer_profile(updated, name=name)
                if "description" in fields:
                    updated = updated_copy_search_answer_profile(updated, description=description)
                if "config" in fields and requested_config is not None:
                    updated = updated_copy_search_answer_profile(
                        updated,
                        profile_config=dump_search_answer_profile_config(requested_config),
                    )
                updated = updated_copy_search_answer_profile(updated, updated_at=now)
                _execute(
                    connection,
                    _render_sql(
                        """
                    UPDATE rag_search_answer_profiles
                    SET
                        name = :name,
                        description = :description,
                        profile_config = :profile_config,
                        updated_at = :updated_at
                    WHERE search_answer_profile_id = :search_answer_profile_id
                      AND {tenant_sql}
                    """,
                        tenant_sql=_oracle_tenant_predicate(),
                    ),
                    _search_answer_profile_binds(updated),
                )
            return _to_search_answer_profile_detail(updated)

        return await self._run_transaction(operation)

    async def _archive_search_answer_profile_with_oracle(
        self,
        search_answer_profile_id: str,
    ) -> SearchAnswerProfileDetail:
        """Oracle search answer profile table の status を ARCHIVED にする。"""

        def operation(connection: OracleConnectionProtocol) -> SearchAnswerProfileDetail:
            existing = _select_search_answer_profile(connection, search_answer_profile_id)
            if existing is None:
                raise KeyError(
                    f"search_answer_profile_id={search_answer_profile_id} は存在しません。"
                )
            if existing.name.casefold() == DEFAULT_SEARCH_ANSWER_PROFILE_NAME.casefold():
                raise ValueError("DEFAULT 検索・回答プロファイルはアーカイブできません。")
            now = datetime.now(UTC)
            archived = updated_copy_search_answer_profile(
                existing,
                status=SearchAnswerProfileStatus.ARCHIVED,
                updated_at=now,
                archived_at=now,
            )
            _execute(
                connection,
                _render_sql(
                    """
                UPDATE rag_search_answer_profiles
                SET
                    status = :status,
                    updated_at = :updated_at,
                    archived_at = :archived_at
                WHERE search_answer_profile_id = :search_answer_profile_id
                  AND {tenant_sql}
                """,
                    tenant_sql=_oracle_tenant_predicate(),
                ),
                _search_answer_profile_binds(archived),
            )
            return _to_search_answer_profile_detail(archived)

        return await self._run_transaction(operation)

    async def _resolve_knowledge_base_refs(
        self,
        knowledge_base_ids: Sequence[str],
    ) -> list[SearchAnswerProfileKnowledgeBaseRef]:
        """参照 KB ID 群から存在する KB の {id, name, status} を tenant scope で解決する。

        アーカイブ済みの KB も返す(status で見分ける。#302)。存在しない KB は落とす。
        """
        ids = _unique_optional_sequence(knowledge_base_ids)
        if not ids:
            return []
        in_sql, in_binds = _oracle_in_predicate("kb.knowledge_base_id", "ref_kb_id", ids)
        binds = _with_tenant_bind({})
        binds.update(in_binds)
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT kb.knowledge_base_id, kb.name, kb.status
            FROM rag_knowledge_bases kb
            WHERE {in_sql}
              AND {tenant_sql}
            """,
                in_sql=in_sql,
                tenant_sql=_oracle_tenant_predicate(alias="kb"),
            ),
            binds,
        )
        by_id = {str(row["knowledge_base_id"]): row for row in rows}
        # 入力順を保ち、存在しない KB は落とす。
        return [
            SearchAnswerProfileKnowledgeBaseRef(
                id=knowledge_base_id,
                name=str(by_id[knowledge_base_id]["name"]),
                status=_knowledge_base_status(by_id[knowledge_base_id].get("status")),
            )
            for knowledge_base_id in ids
            if knowledge_base_id in by_id
        ]

    async def _assign_documents_to_knowledge_base_with_oracle(
        self,
        knowledge_base_id: str,
        document_ids: Sequence[str],
    ) -> KnowledgeBaseDetail:
        """Oracle membership table へ文書所属を追加する。"""
        unique_document_ids = _unique_sequence(document_ids)

        def operation(connection: OracleConnectionProtocol) -> KnowledgeBaseDetail:
            knowledge_base = _require_active_knowledge_base(connection, knowledge_base_id)
            for document_id in unique_document_ids:
                if _select_document_state(connection, document_id) is None:
                    raise KeyError(f"document_id={document_id} は存在しません。")
            _executemany(
                connection,
                """
                MERGE INTO rag_document_knowledge_bases target
                USING (
                    SELECT
                        :knowledge_base_id AS knowledge_base_id,
                        :document_id AS document_id
                    FROM dual
                ) source
                ON (
                    target.knowledge_base_id = source.knowledge_base_id
                    AND target.document_id = source.document_id
                )
                WHEN NOT MATCHED THEN
                    INSERT (
                        knowledge_base_id,
                        document_id,
                        tenant_id_hash,
                        assigned_at,
                        assigned_by_user_id_hash
                    ) VALUES (
                        :knowledge_base_id,
                        :document_id,
                        :tenant_id_hash,
                        :assigned_at,
                        :assigned_by_user_id_hash
                    )
                """,
                [
                    _document_knowledge_base_binds(
                        knowledge_base_id=knowledge_base_id,
                        document_id=document_id,
                    )
                    for document_id in unique_document_ids
                ],
            )
            return _to_knowledge_base_detail(knowledge_base)

        return await self._run_transaction(operation)

    async def _remove_document_from_knowledge_base_with_oracle(
        self,
        knowledge_base_id: str,
        document_id: str,
    ) -> KnowledgeBaseDetail:
        """Oracle membership table から文書所属を削除する。"""

        def operation(connection: OracleConnectionProtocol) -> KnowledgeBaseDetail:
            # アーカイブ済みの KB は所属の追加と同じく解除もしない（#961）。
            knowledge_base = _require_active_knowledge_base(connection, knowledge_base_id)
            if _select_document_state(connection, document_id) is None:
                raise KeyError(f"document_id={document_id} は存在しません。")
            # 文書は 1 つ以上の KB に所属させる（knowledge-base-management.md §6.2）。
            # 最後の所属を外すときは DEFAULT へ移し、未所属の文書を作らない
            # （利用者の KB 範囲外の所属も数える）。
            member_ids = _select_document_membership_knowledge_base_ids(connection, document_id)
            if member_ids == [knowledge_base_id]:
                if knowledge_base.name.casefold() == DEFAULT_KNOWLEDGE_BASE_NAME.casefold():
                    raise ValueError(DEFAULT_ONLY_MEMBERSHIP_REMOVE_MESSAGE)
                default_knowledge_base = _ensure_default_knowledge_base(
                    connection, DEFAULT_KNOWLEDGE_BASE_NAME
                )
                _insert_document_knowledge_base_rows(
                    connection,
                    document_id=document_id,
                    knowledge_base_ids=[default_knowledge_base.id],
                )
            _execute(
                connection,
                _render_sql(
                    """
                DELETE FROM rag_document_knowledge_bases
                WHERE knowledge_base_id = :knowledge_base_id
                  AND document_id = :document_id
                  AND {knowledge_base_membership_access_sql}
                """,
                    knowledge_base_membership_access_sql=(
                        _oracle_membership_access_predicate_sql()
                    ),
                ),
                _with_tenant_bind(
                    {
                        "knowledge_base_id": knowledge_base_id,
                        "document_id": document_id,
                    }
                ),
            )
            return _to_knowledge_base_detail(knowledge_base)

        return await self._run_transaction(operation)

    async def _replace_document_knowledge_bases_with_oracle(
        self,
        document_id: str,
        knowledge_base_ids: Sequence[str],
    ) -> list[KnowledgeBaseRef]:
        """Oracle membership table の文書所属を置換する。"""
        unique_knowledge_base_ids = _unique_sequence(knowledge_base_ids)

        def operation(connection: OracleConnectionProtocol) -> list[KnowledgeBaseRef]:
            if _select_document_state(connection, document_id) is None:
                raise KeyError(f"document_id={document_id} は存在しません。")
            knowledge_bases = [
                _require_active_knowledge_base(connection, knowledge_base_id)
                for knowledge_base_id in unique_knowledge_base_ids
            ]
            _execute(
                connection,
                _render_sql(
                    """
                DELETE FROM rag_document_knowledge_bases
                WHERE document_id = :document_id
                  AND {knowledge_base_membership_access_sql}
                """,
                    knowledge_base_membership_access_sql=(
                        _oracle_membership_access_predicate_sql()
                    ),
                ),
                _with_tenant_bind({"document_id": document_id}),
            )
            _executemany(
                connection,
                """
                INSERT INTO rag_document_knowledge_bases (
                    knowledge_base_id,
                    document_id,
                    tenant_id_hash,
                    assigned_at,
                    assigned_by_user_id_hash
                ) VALUES (
                    :knowledge_base_id,
                    :document_id,
                    :tenant_id_hash,
                    :assigned_at,
                    :assigned_by_user_id_hash
                )
                """,
                [
                    _document_knowledge_base_binds(
                        knowledge_base_id=knowledge_base_id,
                        document_id=document_id,
                    )
                    for knowledge_base_id in unique_knowledge_base_ids
                ],
            )
            return [_to_knowledge_base_ref(knowledge_base) for knowledge_base in knowledge_bases]

        return await self._run_transaction(operation)

    async def _list_document_knowledge_bases_with_oracle(
        self,
        document_id: str,
    ) -> list[KnowledgeBaseRef]:
        """Oracle membership table から文書所属を取得する。"""
        rows = await self._fetch_all(
            """
            SELECT
                kb.knowledge_base_id,
                kb.name
            FROM rag_document_knowledge_bases dkb
            JOIN rag_knowledge_bases kb
                ON kb.knowledge_base_id = dkb.knowledge_base_id
            JOIN rag_documents d
                ON d.document_id = dkb.document_id
            WHERE dkb.document_id = :document_id
              AND {document_access_sql}
              AND {knowledge_base_access_sql}
            ORDER BY kb.name ASC, kb.knowledge_base_id ASC
            """.format(
                document_access_sql=_oracle_access_predicate_sql(alias="d"),
                knowledge_base_access_sql=_oracle_knowledge_base_access_predicate_sql(alias="kb"),
            ),
            _with_tenant_bind({"document_id": document_id}),
        )
        return [
            KnowledgeBaseRef(id=str(row["knowledge_base_id"]), name=str(row["name"]))
            for row in rows
        ]

    async def _document_knowledge_base_refs_by_document_id_with_oracle(
        self,
        document_ids: Sequence[str],
    ) -> dict[str, list[KnowledgeBaseRef]]:
        """複数 document の所属ナレッジベースをまとめて取得する。"""
        unique_document_ids = _unique_optional_sequence(document_ids)
        if not unique_document_ids:
            return {}
        document_filter_sql, document_binds = _oracle_in_predicate(
            "dkb.document_id",
            "document_id",
            unique_document_ids,
        )
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT
                dkb.document_id,
                kb.knowledge_base_id,
                kb.name
            FROM rag_document_knowledge_bases dkb
            JOIN rag_knowledge_bases kb
              ON kb.knowledge_base_id = dkb.knowledge_base_id
            WHERE {document_filter_sql}
              AND {knowledge_base_access_sql}
            ORDER BY dkb.document_id ASC, kb.name ASC, kb.knowledge_base_id ASC
            """,
                document_filter_sql=document_filter_sql,
                knowledge_base_access_sql=_oracle_knowledge_base_access_predicate_sql(alias="kb"),
            ),
            _with_tenant_bind(document_binds),
        )
        refs_by_document_id: dict[str, list[KnowledgeBaseRef]] = {
            document_id: [] for document_id in unique_document_ids
        }
        for row in rows:
            document_id = str(row["document_id"])
            refs_by_document_id.setdefault(document_id, []).append(
                KnowledgeBaseRef(id=str(row["knowledge_base_id"]), name=str(row["name"]))
            )
        return refs_by_document_id

    async def _attach_knowledge_base_refs_to_documents(
        self,
        documents: Sequence[DocumentT],
    ) -> list[DocumentT]:
        """DocumentSummary/Detail へ所属 KB 参照と、新しい版の文書名(#1248)を付与する。"""
        if not documents:
            return []
        refs_by_document_id = await self._document_knowledge_base_refs_by_document_id_with_oracle(
            [document.id for document in documents]
        )
        superseded_ids = _superseded_by_document_ids(documents)
        file_names: dict[str, str] = {}
        if superseded_ids:
            statement, binds = _document_file_names_query(superseded_ids)
            rows = await self._fetch_all(statement, binds)
            file_names = {str(row["document_id"]): str(row["file_name"]) for row in rows}
        return _with_superseded_file_names(
            [
                document.model_copy(
                    update={"knowledge_bases": refs_by_document_id.get(document.id, [])}
                )
                for document in documents
            ],
            file_names,
        )

    async def _create_ingestion_job_with_oracle(
        self, job: IngestionJob, *, expected_current_release_id: str | None = None
    ) -> IngestionJob:
        """Oracle ingestion job table へ job を作成する。"""

        def operation(connection: OracleConnectionProtocol) -> IngestionJob:
            if expected_current_release_id is not None:
                state = _fetch_one(
                    connection,
                    "SELECT current_release_id FROM rag_language_pack_state "
                    "WHERE singleton_id = 1 FOR UPDATE",
                    {},
                )
                if state is None or state.get("current_release_id") != expected_current_release_id:
                    raise ValueError("言語資源の現在版が変わりました。差分を再確認してください。")
            if job.recipe_id is None:
                if _select_document_state(connection, job.document_id) is None:
                    raise KeyError(f"document_id={job.document_id} は存在しません。")
            else:
                _ensure_recipe_row_if_default(connection, job.document_id, job.recipe_id)
                recipe = _select_document_recipe_for_update(
                    connection, job.document_id, job.recipe_id
                )
                if recipe is None:
                    raise KeyError("レシピが見つかりません。")
                current_revision = _int_value(recipe.get("config_revision"))
                if job.recipe_revision is None or job.recipe_revision != current_revision:
                    raise ValueError("レシピ設定が更新されました。再読み込みしてください。")
                active = _fetch_one(
                    connection,
                    """
                    SELECT job_id
                    FROM rag_ingestion_jobs
                    WHERE document_id = :document_id
                      AND recipe_id = :recipe_id
                      AND status IN ('QUEUED', 'RUNNING')
                    FETCH FIRST 1 ROWS ONLY
                    """,
                    {"document_id": job.document_id, "recipe_id": job.recipe_id},
                )
                if active is not None:
                    raise ValueError("このレシピは処理中または待機中です。")
            _execute_ingestion_job_insert(
                connection,
                INGESTION_JOB_INSERT_SQL,
                _ingestion_job_binds(job),
            )
            return job

        return await self._run_transaction(operation)

    async def pin_ingestion_job_language_pack(self, job_id: str, release_id: str) -> str:
        def operation(connection: OracleConnectionProtocol) -> str:
            _execute(
                connection,
                """
                UPDATE rag_ingestion_jobs SET language_pack_release_id = :release_id
                WHERE job_id = :job_id AND language_pack_release_id IS NULL
            """,
                {"job_id": job_id, "release_id": release_id},
            )
            row = _fetch_one(
                connection,
                """
                SELECT language_pack_release_id FROM rag_ingestion_jobs WHERE job_id = :job_id
            """,
                {"job_id": job_id},
            )
            if row is None or not row.get("language_pack_release_id"):
                raise ValueError("処理ジョブの言語資源の版を確定できません。")
            return str(row["language_pack_release_id"])

        return await self._run_transaction(operation)

    async def _get_ingestion_job_with_oracle(self, job_id: str) -> IngestionJob | None:
        """Oracle ingestion job table から job を取得する。"""
        rows = await self._fetch_ingestion_job_rows(
            _render_sql(
                """
            SELECT
                j.job_id,
                j.document_id,
                j.recipe_id,
                j.recipe_revision,
                j.language_pack_release_id,
                j.status,
                j.phase,
                j.parser_profile,
                j.quality_warnings,
                j.settings_overrides,
                j.skip_reason,
                j.error_message,
                j.attempt_count,
                j.max_attempts,
                j.queued_at,
                j.started_at,
                j.finished_at,
                j.progress_json,
                d.file_name AS document_file_name
            FROM rag_ingestion_jobs j
            JOIN rag_documents d
              ON d.document_id = j.document_id
            WHERE j.job_id = :job_id
              AND {document_access_sql}
            """,
                document_access_sql=_oracle_access_predicate_sql(alias="d"),
            ),
            _with_tenant_bind({"job_id": job_id}),
        )
        return None if not rows else _ingestion_job_from_row(rows[0])

    async def _get_ingestion_job_lease_with_oracle(self, job_id: str) -> IngestionJobLease | None:
        """job の status と lease_owner を読む(#359)。"""
        row = await self._fetch_one(
            _render_sql(
                """
            SELECT j.status, j.lease_owner
            FROM rag_ingestion_jobs j
            JOIN rag_documents d
              ON d.document_id = j.document_id
            WHERE j.job_id = :job_id
              AND {document_access_sql}
            """,
                document_access_sql=_oracle_access_predicate_sql(alias="d"),
            ),
            _with_tenant_bind({"job_id": job_id}),
        )
        if row is None:
            return None
        return IngestionJobLease(
            status=_ingestion_job_status(row.get("status")),
            lease_owner=_optional_str(row.get("lease_owner")),
        )

    async def _list_ingestion_jobs_with_oracle(
        self,
        *,
        status: IngestionJobStatus | None,
        limit: int | None,
        offset: int,
        oldest_first: bool = False,
    ) -> list[IngestionJob]:
        """Oracle ingestion job table から job を取得する。"""
        binds: dict[str, object] = dict(offset_fetch_binds(offset=offset, limit=limit))
        status_clause = ""
        if status is not None:
            binds["ingestion_job_status"] = status.value
            status_clause = "AND j.status = :ingestion_job_status"
        limit_clause = offset_fetch_clause(with_limit=limit is not None)
        order_clause = (
            "ORDER BY j.queued_at ASC, j.job_id ASC"
            if oldest_first
            else "ORDER BY j.queued_at DESC, j.job_id DESC"
        )
        rows = await self._fetch_ingestion_job_rows(
            _render_sql(
                """
            SELECT
                j.job_id,
                j.document_id,
                j.recipe_id,
                j.recipe_revision,
                j.language_pack_release_id,
                j.status,
                j.phase,
                j.parser_profile,
                j.quality_warnings,
                j.settings_overrides,
                j.skip_reason,
                j.error_message,
                j.attempt_count,
                j.max_attempts,
                j.queued_at,
                j.started_at,
                j.finished_at,
                j.progress_json,
                d.file_name AS document_file_name
            FROM rag_ingestion_jobs j
            JOIN rag_documents d
              ON d.document_id = j.document_id
            WHERE {document_access_sql}
              {status_clause}
            {order_clause}
            {limit_clause}
            """,
                document_access_sql=_oracle_access_predicate_sql(alias="d"),
                status_clause=status_clause,
                order_clause=order_clause,
                limit_clause=limit_clause,
            ),
            _with_tenant_bind(binds),
        )
        return [_ingestion_job_from_row(row) for row in rows]

    async def _list_document_ingestion_jobs_with_oracle(
        self,
        document_id: str,
        *,
        status: IngestionJobStatus | None,
    ) -> list[IngestionJob]:
        """Oracle ingestion job table から指定 document の job を取得する。"""
        binds: dict[str, object] = {"document_id": document_id}
        status_clause = ""
        if status is not None:
            binds["ingestion_job_status"] = status.value
            status_clause = "AND j.status = :ingestion_job_status"
        rows = await self._fetch_ingestion_job_rows(
            _render_sql(
                """
            SELECT
                j.job_id,
                j.document_id,
                j.recipe_id,
                j.recipe_revision,
                j.language_pack_release_id,
                j.status,
                j.phase,
                j.parser_profile,
                j.quality_warnings,
                j.settings_overrides,
                j.skip_reason,
                j.error_message,
                j.attempt_count,
                j.max_attempts,
                j.queued_at,
                j.started_at,
                j.finished_at,
                j.progress_json,
                d.file_name AS document_file_name
            FROM rag_ingestion_jobs j
            JOIN rag_documents d
              ON d.document_id = j.document_id
            WHERE j.document_id = :document_id
              AND {document_access_sql}
              {status_clause}
            ORDER BY j.queued_at DESC, j.job_id DESC
            """,
                document_access_sql=_oracle_access_predicate_sql(alias="d"),
                status_clause=status_clause,
            ),
            _with_tenant_bind(binds),
        )
        return [_ingestion_job_from_row(row) for row in rows]

    async def _replace_ingestion_segments_with_oracle(
        self,
        document_id: str,
        segments: Sequence[IngestionSegment],
    ) -> list[IngestionSegment]:
        """Oracle segment checkpoint table を document scope で置換する。"""
        normalized_segments = [
            segment.model_copy(update={"document_id": document_id}) for segment in segments
        ]
        recipe_id = next(
            (segment.recipe_id for segment in normalized_segments if segment.recipe_id is not None),
            None,
        )

        def operation(connection: OracleConnectionProtocol) -> list[IngestionSegment]:
            if _select_document_state(connection, document_id) is None:
                raise KeyError(f"document_id={document_id} は存在しません。")
            recipe_scope_sql = "AND recipe_id = :recipe_id" if recipe_id is not None else ""
            delete_binds: dict[str, object] = {"document_id": document_id}
            if recipe_id is not None:
                delete_binds["recipe_id"] = recipe_id
            _execute(
                connection,
                _render_sql(
                    """
                DELETE FROM rag_ingestion_segments
                WHERE document_id = :document_id
                  {recipe_scope_sql}
                  AND EXISTS (
                      SELECT 1
                      FROM rag_documents d
                      WHERE d.document_id = rag_ingestion_segments.document_id
                        AND {document_access_sql}
                  )
                """,
                    document_access_sql=_oracle_access_predicate_sql(alias="d"),
                    recipe_scope_sql=recipe_scope_sql,
                ),
                _with_tenant_bind(delete_binds),
            )
            if normalized_segments:
                _executemany(
                    connection,
                    """
                    INSERT INTO rag_ingestion_segments (
                        segment_id,
                        document_id,
                        recipe_id,
                        tenant_id_hash,
                        status,
                        parser_backend,
                        parser_profile,
                        page_start,
                        page_end,
                        attempt_count,
                        artifact_path,
                        error_code,
                        error_message
                    ) VALUES (
                        :segment_id,
                        :document_id,
                        :recipe_id,
                        :tenant_id_hash,
                        :status,
                        :parser_backend,
                        :parser_profile,
                        :page_start,
                        :page_end,
                        :attempt_count,
                        :artifact_path,
                        :error_code,
                        :error_message
                    )
                    """,
                    [_ingestion_segment_binds(segment) for segment in normalized_segments],
                )
            return list(normalized_segments)

        return await self._run_transaction(operation)

    async def _list_ingestion_segments_with_oracle(
        self,
        document_id: str,
    ) -> list[IngestionSegment]:
        """Oracle segment checkpoint table から document scope で取得する。"""
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT
                s.segment_id,
                s.document_id,
                s.recipe_id,
                s.status,
                s.parser_backend,
                s.parser_profile,
                s.page_start,
                s.page_end,
                s.attempt_count,
                s.artifact_path,
                s.error_code,
                s.error_message
            FROM rag_ingestion_segments s
            JOIN rag_documents d
              ON d.document_id = s.document_id
            WHERE s.document_id = :document_id
              AND {document_access_sql}
            ORDER BY
                COALESCE(s.page_start, 0) ASC,
                COALESCE(s.page_end, 0) ASC,
                s.segment_id ASC
            """,
                document_access_sql=_oracle_access_predicate_sql(alias="d"),
            ),
            _with_tenant_bind({"document_id": document_id}),
        )
        return [_ingestion_segment_from_row(row) for row in rows]

    async def _update_ingestion_segment_with_oracle(
        self,
        segment_id: str,
        *,
        status: str | None,
        attempt_count: int | None,
        artifact_path: str | None,
        error_code: str | None,
        error_message: str | None,
    ) -> IngestionSegment | None:
        """Oracle segment checkpoint table の状態を更新する。"""
        updates: dict[str, object] = {}
        if status is not None:
            updates["status"] = status
        if attempt_count is not None:
            updates["attempt_count"] = attempt_count
        if artifact_path is not None:
            updates["artifact_path"] = artifact_path
        if error_code is not None:
            updates["error_code"] = error_code
        if error_message is not None:
            updates["error_message"] = error_message
        if not updates:
            rows = await self._fetch_ingestion_segment_by_id(segment_id)
            return None if not rows else _ingestion_segment_from_row(rows[0])
        set_sql = ", ".join(f"{column} = :{column}" for column in updates)
        binds = {"segment_id": segment_id, **updates}

        def operation(connection: OracleConnectionProtocol) -> IngestionSegment | None:
            _execute(
                connection,
                _render_sql(
                    """
                UPDATE rag_ingestion_segments
                SET {set_sql},
                    updated_at = SYSTIMESTAMP
                WHERE segment_id = :segment_id
                  AND EXISTS (
                      SELECT 1
                      FROM rag_documents d
                      WHERE d.document_id = rag_ingestion_segments.document_id
                        AND {document_access_sql}
                  )
                """,
                    set_sql=set_sql,
                    document_access_sql=_oracle_access_predicate_sql(alias="d"),
                ),
                _with_tenant_bind(binds),
            )
            rows = _fetch_all(
                connection,
                _render_sql(
                    """
                SELECT
                    s.segment_id,
                    s.document_id,
                    s.recipe_id,
                    s.status,
                    s.parser_backend,
                    s.parser_profile,
                    s.page_start,
                    s.page_end,
                    s.attempt_count,
                    s.artifact_path,
                    s.error_code,
                    s.error_message
                FROM rag_ingestion_segments s
                JOIN rag_documents d
                  ON d.document_id = s.document_id
                WHERE s.segment_id = :segment_id
                  AND {document_access_sql}
                """,
                    document_access_sql=_oracle_access_predicate_sql(alias="d"),
                ),
                _with_tenant_bind({"segment_id": segment_id}),
            )
            return None if not rows else _ingestion_segment_from_row(rows[0])

        return await self._run_transaction(operation)

    async def _fetch_ingestion_segment_by_id(
        self,
        segment_id: str,
    ) -> list[dict[str, object]]:
        return await self._fetch_all(
            _render_sql(
                """
            SELECT
                s.segment_id,
                s.document_id,
                s.recipe_id,
                s.status,
                s.parser_backend,
                s.parser_profile,
                s.page_start,
                s.page_end,
                s.attempt_count,
                s.artifact_path,
                s.error_code,
                s.error_message
            FROM rag_ingestion_segments s
            JOIN rag_documents d
              ON d.document_id = s.document_id
            WHERE s.segment_id = :segment_id
              AND {document_access_sql}
            """,
                document_access_sql=_oracle_access_predicate_sql(alias="d"),
            ),
            _with_tenant_bind({"segment_id": segment_id}),
        )

    async def _count_ingestion_jobs_with_oracle(
        self,
        *,
        status: IngestionJobStatus | None,
    ) -> int:
        """Oracle ingestion job table の件数を取得する。"""
        binds: dict[str, object] = {}
        status_clause = ""
        if status is not None:
            binds["ingestion_job_status"] = status.value
            status_clause = "AND j.status = :ingestion_job_status"
        row = await self._fetch_one(
            _render_sql(
                """
            SELECT COUNT(*) AS count_value
            FROM rag_ingestion_jobs j
            JOIN rag_documents d
              ON d.document_id = j.document_id
            WHERE {document_access_sql}
              {status_clause}
            """,
                document_access_sql=_oracle_access_predicate_sql(alias="d"),
                status_clause=status_clause,
            ),
            _with_tenant_bind(binds),
        )
        return _row_count_value(row)

    async def _recover_stale_ingestion_jobs_with_oracle(
        self,
        *,
        stale_before: datetime,
        lease_ttl_seconds: float,
        limit: int,
        exclude_lease_owner: str | None,
    ) -> list[IngestionJob]:
        """stale RUNNING job を QUEUED/FAILED へ戻し、固着した文書状態も復旧する。

        サブプロセスがクラッシュした場合、job は RUNNING、文書は
        PREPROCESSING/INGESTING/CHUNKING/INDEXING のまま残る。job だけ QUEUED へ
        戻しても文書が INGESTING のままだと、再投入された job が取込中ガード(409)で
        必ず失敗し、永久に固着する。これを防ぐため:

        - job を再キューする際は、文書も再実行可能な状態(EXTRACT→UPLOADED /
          INDEX→REVIEW)へ戻す。
        - 試行上限超過で job を失敗させる際は、文書も ERROR へ戻す。
        - QUEUED/RUNNING の job が一つも無いのに
          PREPROCESSING/INGESTING/CHUNKING/INDEXING で取り残された文書(過去の
          デッドロックで FAILED になった job しか持たない等)は ERROR へ戻し、
          利用者が再試行できるようにする。

        stale の判定は heartbeat の TTL(heartbeat の無い行は開始からの経過時間)で行い、
        job を戻す UPDATE にも同じ条件を入れる。SELECT の後に heartbeat が打たれた job や、
        完了した job は戻さない(#357)。
        """
        now = datetime.now(UTC)
        stale_binds: dict[str, object] = {
            "stale_before": stale_before,
            "lease_ttl_seconds": lease_ttl_seconds,
        }
        if exclude_lease_owner is not None:
            stale_binds["exclude_lease_owner"] = exclude_lease_owner
        exclude_own_lease = exclude_lease_owner is not None
        stale_error_message = "取込ジョブが規定回数を超えて停止しました。"
        orphan_error_message = "取込処理が中断されたため停止しました。再実行してください。"

        def operation(connection: OracleConnectionProtocol) -> list[IngestionJob]:
            rows = _fetch_ingestion_job_rows(
                connection,
                _render_sql(
                    """
                SELECT
                    j.job_id,
                    j.document_id,
                    j.recipe_id,
                    j.recipe_revision,
                j.language_pack_release_id,
                    j.status,
                    j.phase,
                    j.parser_profile,
                    j.quality_warnings,
                    j.settings_overrides,
                    j.skip_reason,
                    j.error_message,
                    j.attempt_count,
                    j.max_attempts,
                    j.queued_at,
                    j.started_at,
                    j.finished_at
                FROM rag_ingestion_jobs j
                JOIN rag_documents d
                  ON d.document_id = j.document_id
                WHERE j.status = 'RUNNING'
                  AND {stale_predicate}
                  AND {document_access_sql}
                ORDER BY COALESCE(j.heartbeat_at, j.started_at, j.queued_at) ASC, j.job_id ASC
                FETCH FIRST :limit ROWS ONLY
                """,
                    stale_predicate=_stale_running_ingestion_job_predicate_sql(
                        alias="j", exclude_own_lease=exclude_own_lease
                    ),
                    document_access_sql=_oracle_access_predicate_sql(alias="d"),
                ),
                _with_tenant_bind({**stale_binds, "limit": limit}),
                default_max_attempts=self._settings.ingestion_job_max_attempts,
            )
            stale_jobs: list[IngestionJob] = []
            for job in (_ingestion_job_from_row(row) for row in rows):
                if job.attempt_count >= job.max_attempts:
                    failed_count = _execute_count(
                        connection,
                        _render_sql(
                            """
                        UPDATE rag_ingestion_jobs
                        SET status = 'FAILED',
                            error_message = :error_message,
                            finished_at = :finished_at
                        WHERE job_id = :job_id
                          AND status = 'RUNNING'
                          AND {stale_predicate}
                          AND EXISTS (
                              SELECT 1
                              FROM rag_documents d
                              WHERE d.document_id = rag_ingestion_jobs.document_id
                                AND {document_access_sql}
                          )
                        """,
                            stale_predicate=_stale_running_ingestion_job_predicate_sql(
                                alias=None, exclude_own_lease=exclude_own_lease
                            ),
                            document_access_sql=_oracle_access_predicate_sql(alias="d"),
                        ),
                        _with_tenant_bind(
                            {
                                **stale_binds,
                                "job_id": job.id,
                                "error_message": stale_error_message,
                                "finished_at": now,
                            }
                        ),
                    )
                    if failed_count == 0:
                        # SELECT の後に heartbeat が打たれた・完了した job は戻さない。
                        continue
                    stale_jobs.append(job)
                    if job.recipe_id is not None:
                        self._reset_document_recipe_status_inline(
                            connection,
                            recipe_id=job.recipe_id,
                            phase=job.phase,
                            status=FileStatus.ERROR,
                            error_message=stale_error_message,
                        )
                    else:
                        self._reset_document_status_inline(
                            connection,
                            document_id=job.document_id,
                            status=FileStatus.ERROR,
                            error_message=stale_error_message,
                        )
                    continue
                requeued_count = _execute_count(
                    connection,
                    _render_sql(
                        """
                    UPDATE rag_ingestion_jobs
                    SET status = 'QUEUED',
                        error_message = NULL,
                        started_at = NULL,
                        finished_at = NULL,
                        lease_owner = NULL,
                        heartbeat_at = NULL
                    WHERE job_id = :job_id
                      AND status = 'RUNNING'
                      AND {stale_predicate}
                      AND EXISTS (
                          SELECT 1
                          FROM rag_documents d
                          WHERE d.document_id = rag_ingestion_jobs.document_id
                            AND {document_access_sql}
                      )
                    """,
                        stale_predicate=_stale_running_ingestion_job_predicate_sql(
                            alias=None, exclude_own_lease=exclude_own_lease
                        ),
                        document_access_sql=_oracle_access_predicate_sql(alias="d"),
                    ),
                    _with_tenant_bind({**stale_binds, "job_id": job.id}),
                )
                if requeued_count == 0:
                    continue
                stale_jobs.append(job)
                self._restore_statuses_for_requeued_job_inline(connection, job)

            # QUEUED/RUNNING の job が無いのに active status で取り残された文書を
            # ERROR へ戻す(過去のデッドロックで固着した文書の自己復旧)。
            orphan_rows = _fetch_all(
                connection,
                _render_sql(
                    """
                SELECT d.document_id
                FROM rag_documents d
                WHERE d.status IN ('PREPROCESSING', 'INGESTING', 'CHUNKING', 'INDEXING')
                  AND {document_access_sql}
                  AND NOT EXISTS (
                      SELECT 1
                      FROM rag_ingestion_jobs j
                      WHERE j.document_id = d.document_id
                        AND j.status IN ('QUEUED', 'RUNNING')
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM rag_document_recipes r
                      WHERE r.document_id = d.document_id
                        AND r.status IN ('PREPROCESSING', 'INGESTING', 'CHUNKING', 'INDEXING')
                  )
                """,
                    document_access_sql=_oracle_access_predicate_sql(alias="d"),
                ),
                _with_tenant_bind({}),
            )
            for orphan_row in orphan_rows:
                orphan_document_id = orphan_row.get("document_id")
                if not isinstance(orphan_document_id, str):
                    continue
                self._reset_document_status_inline(
                    connection,
                    document_id=orphan_document_id,
                    status=FileStatus.ERROR,
                    error_message=orphan_error_message,
                )
            orphan_recipe_rows = _fetch_all(
                connection,
                _render_sql(
                    """
                SELECT r.recipe_id, r.status
                FROM rag_document_recipes r
                JOIN rag_documents d ON d.document_id = r.document_id
                WHERE r.status IN ('PREPROCESSING', 'INGESTING', 'CHUNKING', 'INDEXING')
                  AND {document_access_sql}
                  AND NOT EXISTS (
                      SELECT 1
                      FROM rag_ingestion_jobs j
                      WHERE j.recipe_id = r.recipe_id
                        AND j.status IN ('QUEUED', 'RUNNING')
                  )
                """,
                    document_access_sql=_oracle_access_predicate_sql(alias="d"),
                ),
                _with_tenant_bind({}),
            )
            for orphan_recipe in orphan_recipe_rows:
                recipe_id = orphan_recipe.get("recipe_id")
                if not isinstance(recipe_id, str):
                    continue
                self._reset_document_recipe_status_inline(
                    connection,
                    recipe_id=recipe_id,
                    phase=_failed_phase_for_recipe_status(orphan_recipe.get("status")),
                    status=FileStatus.ERROR,
                    error_message=orphan_error_message,
                )
            return stale_jobs

        return await self._run_transaction(operation)

    @classmethod
    def _restore_statuses_for_requeued_job_inline(
        cls,
        connection: OracleConnectionProtocol,
        job: IngestionJob,
    ) -> None:
        """QUEUED に戻した job の対象を、工程を始める前の状態へ戻す(同じトランザクション内)。

        レシピの job はレシピ行だけを戻し、文書(全レシピの集約)には触れない(#305)。
        """
        if job.recipe_id is not None:
            cls._reset_document_recipe_status_inline(
                connection,
                recipe_id=job.recipe_id,
                phase=job.phase,
                status=_restore_recipe_status_for_job_phase(job.phase),
                error_message=None,
            )
            return
        cls._reset_document_status_inline(
            connection,
            document_id=job.document_id,
            status=_restore_status_for_job_phase(job.phase),
            error_message=None,
        )

    @staticmethod
    def _reset_document_status_inline(
        connection: OracleConnectionProtocol,
        *,
        document_id: str,
        status: FileStatus,
        error_message: str | None,
    ) -> None:
        """recovery トランザクション内で固着文書の状態を復旧する。

        PREPROCESSING/INGESTING/CHUNKING/INDEXING に取り残された文書だけを対象にする。
        ERROR へ戻す場合も段階レビュー用の抽出/chunk 成果物は保持する。
        """
        _execute(
            connection,
            _render_sql(
                """
            UPDATE rag_documents
            SET status = :status,
                error_message = :error_message
            WHERE document_id = :document_id
              AND status IN ('PREPROCESSING', 'INGESTING', 'CHUNKING', 'INDEXING')
              AND {access_predicate}
            """,
                access_predicate=_oracle_access_predicate_sql(),
            ),
            _with_tenant_bind(
                {
                    "document_id": document_id,
                    "status": status.value,
                    "error_message": error_message,
                }
            ),
        )

    @staticmethod
    def _reset_document_recipe_status_inline(
        connection: OracleConnectionProtocol,
        *,
        recipe_id: str,
        phase: IngestionJobPhase,
        status: FileStatus,
        error_message: str | None,
    ) -> None:
        """固着した対象 recipe だけを安定状態または ERROR へ戻す。"""
        _execute(
            connection,
            _render_sql(
                """
            UPDATE rag_document_recipes r
            SET status = CASE
                    WHEN :status = 'UPLOADED'
                     AND :phase = 'PREPROCESS'
                     AND EXISTS (
                         SELECT 1 FROM rag_chunk_sets cs
                         WHERE cs.recipe_id = r.recipe_id
                           AND cs.is_active = 1
                           AND cs.status = 'INDEXED'
                     )
                    THEN 'INDEXED'
                    ELSE :status
                END,
                failed_phase = CASE WHEN :status = 'ERROR' THEN :phase ELSE NULL END,
                error_message = :error_message,
                started_at = NULL,
                finished_at = CASE WHEN :status = 'ERROR' THEN SYSTIMESTAMP ELSE NULL END,
                updated_at = SYSTIMESTAMP
            WHERE r.recipe_id = :recipe_id
              AND r.status IN ('PREPROCESSING', 'INGESTING', 'CHUNKING', 'INDEXING')
              AND EXISTS (
                  SELECT 1 FROM rag_documents d
                  WHERE d.document_id = r.document_id
                    AND {document_access_sql}
              )
            """,
                document_access_sql=_oracle_access_predicate_sql(alias="d"),
            ),
            _with_tenant_bind(
                {
                    "recipe_id": recipe_id,
                    "phase": phase.value,
                    "status": status.value,
                    "error_message": error_message,
                }
            ),
        )

    async def _claim_ingestion_job_with_oracle(
        self,
        job_id: str,
        *,
        started_at: datetime,
        lease_owner: str | None,
    ) -> IngestionJob | None:
        """QUEUED job をロックして RUNNING へ遷移し、lease を取る。

        lease を持つ実行は ``heartbeat_at`` を DB の時刻(``SYSTIMESTAMP``)にする(#359)。
        lease を持たない実行(``lease_owner`` なし)は ``heartbeat_at`` を NULL にし、stale の判定を
        従来どおり開始からの経過時間にする(heartbeat を打つ worker がいないため)。
        """
        heartbeat_sql = "SYSTIMESTAMP" if lease_owner is not None else "NULL"

        def operation(connection: OracleConnectionProtocol) -> IngestionJob | None:
            queued = _fetch_one(
                connection,
                """
                SELECT document_id FROM rag_ingestion_jobs
                WHERE job_id = :job_id AND status = 'QUEUED'
                """,
                {"job_id": job_id},
            )
            if queued is None:
                return None
            # 同一文書の別 job と同時 claim されないよう親行を直列化する。
            _fetch_one(
                connection,
                """
                SELECT document_id FROM rag_documents
                WHERE document_id = :document_id
                FOR UPDATE
                """,
                {"document_id": queued["document_id"]},
            )
            rows = _fetch_ingestion_job_rows(
                connection,
                _render_sql(
                    """
                SELECT
                    j.job_id,
                    j.document_id,
                    j.recipe_id,
                    j.recipe_revision,
                j.language_pack_release_id,
                    j.status,
                    j.phase,
                    j.parser_profile,
                    j.quality_warnings,
                    j.settings_overrides,
                    j.skip_reason,
                    j.error_message,
                    j.attempt_count,
                    j.max_attempts,
                    j.queued_at,
                    j.started_at,
                    j.finished_at
                FROM rag_ingestion_jobs j
                JOIN rag_documents d
                  ON d.document_id = j.document_id
                WHERE j.job_id = :job_id
                  AND j.status = 'QUEUED'
                  AND NOT EXISTS (
                      SELECT 1 FROM rag_ingestion_jobs active_job
                      WHERE active_job.document_id = j.document_id
                        AND active_job.status = 'RUNNING'
                        AND active_job.job_id <> j.job_id
                  )
                  AND {document_access_sql}
                FOR UPDATE SKIP LOCKED
                """,
                    document_access_sql=_oracle_access_predicate_sql(alias="d"),
                ),
                _with_tenant_bind({"job_id": job_id}),
                default_max_attempts=self._settings.ingestion_job_max_attempts,
            )
            if not rows:
                return None
            job = _ingestion_job_from_row(rows[0])
            attempt_count = job.attempt_count + 1
            _execute(
                connection,
                _render_sql(
                    """
                UPDATE rag_ingestion_jobs
                SET status = 'RUNNING',
                    attempt_count = :attempt_count,
                    started_at = :started_at,
                    error_message = NULL,
                    finished_at = NULL,
                    lease_owner = :lease_owner,
                    heartbeat_at = {heartbeat_sql}
                WHERE job_id = :job_id
                  AND EXISTS (
                      SELECT 1
                      FROM rag_documents d
                      WHERE d.document_id = rag_ingestion_jobs.document_id
                        AND {document_access_sql}
                  )
                """,
                    heartbeat_sql=heartbeat_sql,
                    document_access_sql=_oracle_access_predicate_sql(alias="d"),
                ),
                _with_tenant_bind(
                    {
                        "job_id": job_id,
                        "attempt_count": attempt_count,
                        "started_at": started_at,
                        "lease_owner": lease_owner,
                    }
                ),
            )
            return job.model_copy(
                update={
                    "status": IngestionJobStatus.RUNNING,
                    "attempt_count": attempt_count,
                    "started_at": started_at,
                    "error_message": None,
                    "finished_at": None,
                }
            )

        return await self._run_transaction(operation)

    async def _heartbeat_ingestion_jobs_with_oracle(self, *, lease_owner: str) -> int:
        """自分の lease の RUNNING job の heartbeat_at を DB の時刻(``SYSTIMESTAMP``)に更新する。

        lease_owner は worker プロセスごとに一意(host・pid・乱数)なので、tenant の条件は付けない
        (worker は request の文脈を持たない)。RUNNING でなくなった job(完了・取り消し・回復済み)
        には書かない。stale の判定も DB の時計で行うため、worker の時計には依存しない(#359)。
        """

        def operation(connection: OracleConnectionProtocol) -> int:
            return _execute_count(
                connection,
                """
                UPDATE rag_ingestion_jobs
                SET heartbeat_at = SYSTIMESTAMP
                WHERE lease_owner = :lease_owner
                  AND status = 'RUNNING'
                """,
                {"lease_owner": lease_owner},
            )

        return await self._run_transaction(operation)

    async def _update_ingestion_job_progress_with_oracle(
        self,
        job_id: str,
        progress: IngestionJobProgress | None,
        *,
        lease_owner: str | None,
    ) -> bool:
        """job の行の progress_json を書く。worker は request の文脈を持たないため tenant の条件は
        付けず、job_id・RUNNING・lease の持ち主で絞る(heartbeat と同じ)。"""
        lease_clause = "AND lease_owner = :lease_owner" if lease_owner is not None else ""
        binds: dict[str, object] = {
            "job_id": job_id,
            "progress_json": (
                None if progress is None else _json_dumps(progress.model_dump(mode="json"))
            ),
        }
        if lease_owner is not None:
            binds["lease_owner"] = lease_owner

        def operation(connection: OracleConnectionProtocol) -> int:
            return _execute_count(
                connection,
                f"""
                UPDATE rag_ingestion_jobs
                SET progress_json = :progress_json
                WHERE job_id = :job_id
                  AND status = 'RUNNING'
                  {lease_clause}
                """,
                binds,
            )

        return await self._run_transaction(operation) > 0

    async def _requeue_leased_ingestion_jobs_with_oracle(
        self,
        *,
        lease_owner: str,
    ) -> list[IngestionJob]:
        """自分の lease の RUNNING job を QUEUED に戻し、文書・レシピも工程の前へ戻す。"""

        def operation(connection: OracleConnectionProtocol) -> list[IngestionJob]:
            rows = _fetch_ingestion_job_rows(
                connection,
                _render_sql(
                    """
                SELECT
                    j.job_id,
                    j.document_id,
                    j.recipe_id,
                    j.recipe_revision,
                j.language_pack_release_id,
                    j.status,
                    j.phase,
                    j.parser_profile,
                    j.quality_warnings,
                    j.settings_overrides,
                    j.skip_reason,
                    j.error_message,
                    j.attempt_count,
                    j.max_attempts,
                    j.queued_at,
                    j.started_at,
                    j.finished_at
                FROM rag_ingestion_jobs j
                JOIN rag_documents d
                  ON d.document_id = j.document_id
                WHERE j.status = 'RUNNING'
                  AND j.lease_owner = :lease_owner
                  AND {document_access_sql}
                ORDER BY j.job_id ASC
                """,
                    document_access_sql=_oracle_access_predicate_sql(alias="d"),
                ),
                _with_tenant_bind({"lease_owner": lease_owner}),
                default_max_attempts=self._settings.ingestion_job_max_attempts,
            )
            requeued: list[IngestionJob] = []
            for job in (_ingestion_job_from_row(row) for row in rows):
                # claim で増やした attempt を戻す(停止は job の失敗ではない)。RUNNING かつ自分の
                # lease のときだけ書き、その間に完了・取り消し・回復された job は戻さない。
                count = _execute_count(
                    connection,
                    _render_sql(
                        """
                    UPDATE rag_ingestion_jobs
                    SET status = 'QUEUED',
                        attempt_count = CASE
                            WHEN attempt_count > 0 THEN attempt_count - 1
                            ELSE 0
                        END,
                        error_message = NULL,
                        started_at = NULL,
                        finished_at = NULL,
                        lease_owner = NULL,
                        heartbeat_at = NULL
                    WHERE job_id = :job_id
                      AND status = 'RUNNING'
                      AND lease_owner = :lease_owner
                      AND EXISTS (
                          SELECT 1
                          FROM rag_documents d
                          WHERE d.document_id = rag_ingestion_jobs.document_id
                            AND {document_access_sql}
                      )
                    """,
                        document_access_sql=_oracle_access_predicate_sql(alias="d"),
                    ),
                    _with_tenant_bind({"job_id": job.id, "lease_owner": lease_owner}),
                )
                if count == 0:
                    continue
                self._restore_statuses_for_requeued_job_inline(connection, job)
                requeued.append(
                    job.model_copy(
                        update={
                            "status": IngestionJobStatus.QUEUED,
                            "attempt_count": max(job.attempt_count - 1, 0),
                            "error_message": None,
                            "started_at": None,
                            "finished_at": None,
                        }
                    )
                )
            return requeued

        return await self._run_transaction(operation)

    async def _list_dispatchable_ingestion_jobs_with_oracle(
        self,
        *,
        limit: int,
        exclude_job_ids: Sequence[str],
        exclude_phases: Collection[IngestionJobPhase] = (),
    ) -> list[IngestionJob]:
        """claim できる QUEUED job を取る順番に返す(同じ文書の job が RUNNING のものを除く)。"""
        binds: dict[str, object] = {
            "limit": limit,
            **ingestion_dispatch_order_binds(self._settings),
        }
        exclude_clause = ""
        unique_excluded = _unique_optional_sequence(exclude_job_ids)
        if unique_excluded:
            in_sql, in_binds = _oracle_in_predicate("j.job_id", "exclude_job_id", unique_excluded)
            exclude_clause = f"AND NOT ({in_sql})"
            binds.update(in_binds)
        excluded_phases = sorted({IngestionJobPhase(phase).value for phase in exclude_phases})
        if excluded_phases:
            in_sql, in_binds = _oracle_in_predicate("j.phase", "exclude_phase", excluded_phases)
            exclude_clause += f" AND NOT ({in_sql})"
            binds.update(in_binds)
        rows = await self._fetch_ingestion_job_rows(
            _render_sql(
                """
            SELECT
                j.job_id,
                j.document_id,
                j.recipe_id,
                j.recipe_revision,
                j.language_pack_release_id,
                j.status,
                j.phase,
                j.parser_profile,
                j.quality_warnings,
                j.settings_overrides,
                j.skip_reason,
                j.error_message,
                j.attempt_count,
                j.max_attempts,
                j.queued_at,
                j.started_at,
                j.finished_at
            FROM rag_ingestion_jobs j
            JOIN rag_documents d
              ON d.document_id = j.document_id
            WHERE j.status = 'QUEUED'
              AND NOT EXISTS (
                  SELECT 1 FROM rag_ingestion_jobs active_job
                  WHERE active_job.document_id = j.document_id
                    AND active_job.status = 'RUNNING'
              )
              {exclude_clause}
              AND {document_access_sql}
            ORDER BY {dispatch_order_sql}
            FETCH FIRST :limit ROWS ONLY
            """,
                exclude_clause=exclude_clause,
                document_access_sql=_oracle_access_predicate_sql(alias="d"),
                dispatch_order_sql=ingestion_dispatch_order_sql("j"),
            ),
            _with_tenant_bind(binds),
        )
        return [_ingestion_job_from_row(row) for row in rows]

    async def _list_ingestion_queue_positions_with_oracle(
        self,
        *,
        job_ids: Sequence[str],
        document_ids: Sequence[str],
    ) -> list[dict[str, object]]:
        """対象の QUEUED の job と、取る順番で前にある同じ枠の段の claim できる job の数を読む。

        対象は画面の 1 ページ分(多くて一覧の表示件数)。前の件数は QUEUED の job(キュー)全体に
        取る順番の window を掛けて数える(claim できない job は数えないが、順番には並べる)。
        """
        binds: dict[str, object] = dict(ingestion_dispatch_order_binds(self._settings))
        targets: list[str] = []
        if job_ids:
            in_sql, in_binds = _oracle_in_predicate("t.job_id", "queue_job_id", job_ids)
            targets.append(in_sql)
            binds.update(in_binds)
        if document_ids:
            in_sql, in_binds = _oracle_in_predicate(
                "t.document_id", "queue_document_id", document_ids
            )
            targets.append(in_sql)
            binds.update(in_binds)
        return await self._fetch_all(
            _render_sql(
                """
            WITH queued AS (
                SELECT
                    j.job_id,
                    j.phase,
                    j.queued_at,
                    {dispatch_key_sql} AS dispatch_key,
                    {phase_rank_sql} AS phase_rank,
                    CASE WHEN EXISTS (
                        SELECT 1 FROM rag_ingestion_jobs active_job
                        WHERE active_job.document_id = j.document_id
                          AND active_job.status = 'RUNNING'
                    ) THEN 0 ELSE 1 END AS claimable
                FROM rag_ingestion_jobs j
                WHERE j.status = 'QUEUED'
            ),
            queue_order AS (
                SELECT
                    q.job_id,
                    COALESCE(
                        SUM(q.claimable) OVER (
                            PARTITION BY {phase_class_sql}
                            ORDER BY {dispatch_order_sql}
                            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                        ),
                        0
                    ) AS jobs_ahead
                FROM queued q
            )
            SELECT
                t.job_id,
                t.document_id,
                t.recipe_id,
                t.phase,
                t.queued_at,
                r.slot_no AS recipe_slot_no,
                (
                    SELECT MIN(first_recipe.slot_no)
                    FROM rag_document_recipes first_recipe
                    WHERE first_recipe.document_id = t.document_id
                ) AS first_slot_no,
                qo.jobs_ahead
            FROM rag_ingestion_jobs t
            JOIN queue_order qo
              ON qo.job_id = t.job_id
            JOIN rag_documents d
              ON d.document_id = t.document_id
            LEFT JOIN rag_document_recipes r
              ON r.recipe_id = t.recipe_id
            WHERE t.status = 'QUEUED'
              AND ({target_sql})
              AND {document_access_sql}
            ORDER BY t.queued_at ASC, t.job_id ASC
            """,
                target_sql=" OR ".join(targets),
                document_access_sql=_oracle_access_predicate_sql(alias="d"),
                dispatch_key_sql=ingestion_dispatch_key_sql("j"),
                phase_rank_sql=ingestion_phase_rank_sql("j"),
                phase_class_sql=ingestion_phase_class_sql("q"),
                dispatch_order_sql=ingestion_dispatch_order_sql(
                    "q", dispatch_key="q.dispatch_key", phase_rank="q.phase_rank"
                ),
            ),
            _with_tenant_bind(binds),
        )

    async def _transition_ingestion_job_with_oracle(
        self,
        job_id: str,
        *,
        from_statuses: Collection[IngestionJobStatus],
        to_status: IngestionJobStatus,
        error_message: str | None,
        finished_at: datetime | None,
        lease_owner: str | None = None,
    ) -> IngestionJob | None:
        """遷移元の status(と lease の持ち主)を WHERE に入れた UPDATE で job の状態を遷移する。"""
        statuses = list(dict.fromkeys(from_statuses))
        if not statuses:
            raise ValueError("遷移元の status を 1 つ以上指定してください。")
        status_binds = {
            f"from_status_{index}": status.value for index, status in enumerate(statuses)
        }
        status_placeholders = ", ".join(f":{name}" for name in status_binds)
        binds: dict[str, object] = {
            "job_id": job_id,
            "to_status": to_status.value,
            "error_message": error_message,
            "finished_at": finished_at,
            **status_binds,
        }
        lease_sql = ""
        if lease_owner is not None:
            # 別の worker が claim し直した行(lease の持ち主が変わった行)は書かない(#359)。
            lease_sql = "AND lease_owner = :lease_owner"
            binds["lease_owner"] = lease_owner

        def operation(connection: OracleConnectionProtocol) -> IngestionJob | None:
            updated_count = _execute_count(
                connection,
                _render_sql(
                    """
                UPDATE rag_ingestion_jobs
                SET status = :to_status,
                    error_message = :error_message,
                    finished_at = :finished_at
                WHERE job_id = :job_id
                  AND status IN ({status_placeholders})
                  {lease_sql}
                  AND EXISTS (
                      SELECT 1
                      FROM rag_documents d
                      WHERE d.document_id = rag_ingestion_jobs.document_id
                        AND {document_access_sql}
                  )
                """,
                    status_placeholders=status_placeholders,
                    lease_sql=lease_sql,
                    document_access_sql=_oracle_access_predicate_sql(alias="d"),
                ),
                _with_tenant_bind(binds),
            )
            if updated_count == 0:
                return None
            rows = _fetch_ingestion_job_rows(
                connection,
                _render_sql(
                    """
                SELECT
                    j.job_id,
                    j.document_id,
                    j.recipe_id,
                    j.recipe_revision,
                j.language_pack_release_id,
                    j.status,
                    j.phase,
                    j.parser_profile,
                    j.quality_warnings,
                    j.settings_overrides,
                    j.skip_reason,
                    j.error_message,
                    j.attempt_count,
                    j.max_attempts,
                    j.queued_at,
                    j.started_at,
                    j.finished_at
                FROM rag_ingestion_jobs j
                JOIN rag_documents d
                  ON d.document_id = j.document_id
                WHERE j.job_id = :job_id
                  AND {document_access_sql}
                """,
                    document_access_sql=_oracle_access_predicate_sql(alias="d"),
                ),
                _with_tenant_bind({"job_id": job_id}),
                default_max_attempts=self._settings.ingestion_job_max_attempts,
            )
            return None if not rows else _ingestion_job_from_row(rows[0])

        return await self._run_transaction(operation)

    async def _find_document_by_content_hash_with_oracle(
        self, content_sha256: str
    ) -> DocumentSummary | None:
        """Oracle document table から content hash で既存文書を取得する。"""
        where_sql, binds = _oracle_document_where()
        binds["content_sha256"] = content_sha256
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT *
            FROM (
                SELECT
                    document_id,
                    file_name,
                    status,
                    tenant_id_hash,
                    category_name,
                    object_storage_path,
                    content_type,
                    file_size_bytes,
                    content_sha256,
                    duplicate_of_document_id,
                    extraction,
                    error_message,
                    uploaded_at,
                    indexed_at
                FROM rag_documents
                WHERE {where_sql}
                  AND content_sha256 = :content_sha256
                ORDER BY
                    CASE WHEN duplicate_of_document_id IS NULL THEN 0 ELSE 1 END,
                    uploaded_at ASC
            )
            WHERE ROWNUM <= 1
            """,
                where_sql=where_sql,
            ),
            binds,
        )
        if not rows:
            return None
        summaries = await self._attach_knowledge_base_refs_to_documents(
            [_to_document_summary(_stored_document_from_row(rows[0]))]
        )
        return summaries[0]

    async def _list_documents_with_oracle(
        self,
        status: FileStatus | None,
        query: str | None,
        limit: int | None,
        offset: int,
        knowledge_base_id: str | None,
        queued_phase: IngestionJobPhase | None = None,
        language_pack_relation: LanguagePackIndexRelation | None = None,
        current_release_id: str | None = None,
    ) -> list[DocumentSummary]:
        """Oracle document table から一覧取得する。"""
        where_sql, binds = _oracle_document_where(
            status=status,
            query=query,
            knowledge_base_id=knowledge_base_id,
            queued_phase=queued_phase,
            language_pack_relation=language_pack_relation,
            current_release_id=current_release_id,
        )
        binds.update(offset_fetch_binds(offset=offset, limit=limit))
        limit_clause = offset_fetch_clause(with_limit=limit is not None)
        # 一覧は取込中にポーリングされる。要約に使わない JSON 列(抽出結果など)は読まない(#341)。
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT
                {columns}
            FROM rag_documents
            WHERE {where_sql}
            ORDER BY {name_order_sql}
            {limit_clause}
            """,
                columns=_DOCUMENT_LIST_COLUMNS_SQL.strip(),
                where_sql=where_sql,
                name_order_sql=_oracle_document_list_order_sql(),
                limit_clause=limit_clause,
            ),
            binds,
        )
        summaries = [
            _to_document_summary(_stored_document_from_row(row)).model_copy(
                update={"ingestion_active": _int_value(row.get("ingestion_active")) == 1}
            )
            for row in rows
        ]
        summaries = await self._attach_knowledge_base_refs_to_documents(summaries)
        uses = await self.document_language_pack_uses(
            [item.id for item in summaries], current_release_id=current_release_id
        )
        return [item.model_copy(update={"language_pack": uses.get(item.id)}) for item in summaries]

    async def _list_document_extractions_with_oracle(self) -> list[dict[str, object]]:
        """Oracle document table から extraction JSON だけを取得する。"""
        where_sql, binds = _oracle_document_where()
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT extraction
            FROM rag_documents
            WHERE {where_sql}
            """,
                where_sql=where_sql,
            ),
            binds,
        )
        return [_json_loads(row.get("extraction")) for row in rows]

    async def _count_documents_with_oracle(
        self,
        status: FileStatus | None,
        query: str | None,
        knowledge_base_id: str | None,
        queued_phase: IngestionJobPhase | None = None,
        language_pack_relation: LanguagePackIndexRelation | None = None,
        current_release_id: str | None = None,
    ) -> int:
        """Oracle document table の件数を取得する。"""
        where_sql, binds = _oracle_document_where(
            status=status,
            query=query,
            knowledge_base_id=knowledge_base_id,
            queued_phase=queued_phase,
            language_pack_relation=language_pack_relation,
            current_release_id=current_release_id,
        )
        row = await self._fetch_one(
            _render_sql(
                "SELECT COUNT(*) AS count_value FROM rag_documents WHERE {where_sql}",
                where_sql=where_sql,
            ),
            binds,
        )
        return _row_count_value(row)

    async def _count_document_chunks_with_oracle(self, document_id: str) -> int:
        """Oracle chunk/vector table の document 別検索可能件数を取得する。"""
        where_sql, binds = _oracle_retrieval_where({"document_id": document_id})
        row = await self._fetch_one(
            _render_sql(
                """
            SELECT COUNT(*) AS count_value
            FROM rag_chunks c
            JOIN rag_documents d ON d.document_id = c.document_id
            WHERE {where_sql}
            """,
                where_sql=where_sql,
            ),
            binds,
        )
        return _row_count_value(row)

    async def _list_document_chunks_with_oracle(
        self,
        document_id: str,
    ) -> list[DocumentChunkView]:
        """Oracle chunk/vector table から embedding を除いた chunk view を返す。"""
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT
                c.document_id,
                c.chunk_id,
                c.chunk_text,
                c.metadata_json,
                c.chunk_index
            FROM rag_chunks c
            JOIN rag_documents d ON d.document_id = c.document_id
            WHERE c.document_id = :document_id
              AND {access_predicate}
            ORDER BY c.chunk_index ASC, c.chunk_id ASC
            """,
                access_predicate=_oracle_access_predicate_sql(alias="d"),
            ),
            _with_tenant_bind({"document_id": document_id}),
        )
        return [_document_chunk_view_from_row(row) for row in rows]

    async def _list_chunk_set_chunks_with_oracle(
        self,
        chunk_set_id: str,
    ) -> list[DocumentChunkView]:
        """Oracle chunk/vector table から指定 chunk_set の chunk view を返す。"""
        rows = await self._fetch_all(
            _render_sql(
                """
            SELECT
                c.document_id,
                c.chunk_id,
                c.chunk_text,
                c.metadata_json,
                c.chunk_index
            FROM rag_chunks c
            JOIN rag_documents d ON d.document_id = c.document_id
            WHERE c.chunk_set_id = :chunk_set_id
              AND {access_predicate}
            ORDER BY c.chunk_index ASC, c.chunk_id ASC
            """,
                access_predicate=_oracle_access_predicate_sql(alias="d"),
            ),
            _with_tenant_bind({"chunk_set_id": chunk_set_id}),
        )
        return [_document_chunk_view_from_row(row) for row in rows]

    async def _update_chunk_set_embeddings_with_oracle(
        self,
        *,
        chunk_set_id: str,
        chunks: list[Chunk],
        embeddings: list[list[float]],
    ) -> None:
        """既存 chunk_set の chunk 行に embedding を反映する。"""

        def chunk_id_for(chunk: Chunk) -> str:
            document_id = str(chunk.metadata.get("document_id") or "")
            return str(
                chunk.metadata.get("chunk_id") or f"{document_id}:{chunk_set_id}:{chunk.index}"
            )

        rows = [
            {
                "chunk_id": chunk_id_for(chunk),
                "chunk_set_id": chunk_set_id,
                "embedding": _to_vector_bind(embedding),
                "search_text": _chunk_search_text(chunk),
                "metadata_json": _json_dumps(
                    {
                        **chunk.metadata,
                        "chunk_id": chunk_id_for(chunk),
                        "chunk_index": chunk.index,
                    }
                ),
            }
            for chunk, embedding in zip(chunks, embeddings, strict=True)
        ]

        def operation(connection: OracleConnectionProtocol) -> None:
            if rows:
                _executemany(
                    connection,
                    """
                    UPDATE rag_chunks
                    SET embedding = :embedding,
                        search_text = :search_text,
                        metadata_json = :metadata_json
                    WHERE chunk_id = :chunk_id
                      AND chunk_set_id = :chunk_set_id
                    """,
                    rows,
                    input_sizes=_json_input_sizes("metadata_json"),
                )
            count_rows = _fetch_all(
                connection,
                """
                SELECT COUNT(*) AS cnt
                FROM rag_chunks
                WHERE chunk_set_id = :chunk_set_id
                  AND embedding IS NOT NULL
                """,
                {"chunk_set_id": chunk_set_id},
            )
            vector_count = int(str(next(iter(count_rows[0].values())))) if count_rows else 0
            if vector_count != len(chunks):
                raise ValueError(
                    f"chunk_set の embedding 保存件数が一致しません。"
                    f"expected={len(chunks)}, actual={vector_count}"
                )

        await self._run_transaction(operation)

    async def _get_document_with_oracle(self, document_id: str) -> DocumentDetail | None:
        """Oracle document table から詳細取得する。"""
        row = await self._fetch_one(
            _render_sql(
                """
            SELECT
                document_id,
                file_name,
                status,
                tenant_id_hash,
                category_name,
                object_storage_path,
                preprocess_artifact,
                content_type,
                file_size_bytes,
                content_sha256,
                duplicate_of_document_id,
                extraction,
                classification,
                error_message,
                uploaded_at,
                indexed_at,
                superseded_by_document_id,
                superseded_at
            FROM rag_documents
            WHERE document_id = :document_id
              AND {access_predicate}
            """,
                access_predicate=_oracle_access_predicate_sql(),
            ),
            _with_tenant_bind({"document_id": document_id}),
        )
        if row is None:
            return None
        details = await self._attach_knowledge_base_refs_to_documents(
            [_to_document_detail(_stored_document_from_row(row))]
        )
        return details[0]

    async def _update_document_status_with_oracle(
        self,
        document_id: str,
        status: FileStatus,
        error_message: str | None,
    ) -> DocumentDetail:
        """Oracle document table の状態を更新する。"""

        def operation(connection: OracleConnectionProtocol) -> DocumentDetail:
            existing = _select_document_state(connection, document_id)
            if existing is None:
                raise KeyError(f"document_id={document_id} は存在しません。")
            if status == FileStatus.INGESTING:
                _execute(
                    connection,
                    """
                    DELETE FROM rag_chunks
                    WHERE document_id = :document_id
                    """,
                    {"document_id": document_id},
                )
                _execute(
                    connection,
                    _render_sql(
                        """
                    UPDATE rag_documents
                    SET
                        status = :status,
                        error_message = :error_message,
                        extraction = NULL
                    WHERE document_id = :document_id
                      AND {access_predicate}
                    """,
                        access_predicate=_oracle_access_predicate_sql(),
                    ),
                    _with_tenant_bind(
                        {
                            "document_id": document_id,
                            "status": status.value,
                            "error_message": error_message,
                        }
                    ),
                )
            elif status == FileStatus.INDEXED:
                _execute(
                    connection,
                    _render_sql(
                        """
                    UPDATE rag_documents
                    SET
                        status = :status,
                        error_message = :error_message,
                        indexed_at = COALESCE(indexed_at, SYSTIMESTAMP)
                    WHERE document_id = :document_id
                      AND {access_predicate}
                    """,
                        access_predicate=_oracle_access_predicate_sql(),
                    ),
                    _with_tenant_bind(
                        {
                            "document_id": document_id,
                            "status": status.value,
                            "error_message": error_message,
                        }
                    ),
                )
            else:
                _execute(
                    connection,
                    _render_sql(
                        """
                    UPDATE rag_documents
                    SET
                        status = :status,
                        error_message = :error_message
                    WHERE document_id = :document_id
                      AND {access_predicate}
                    """,
                        access_predicate=_oracle_access_predicate_sql(),
                    ),
                    _with_tenant_bind(
                        {
                            "document_id": document_id,
                            "status": status.value,
                            "error_message": error_message,
                        }
                    ),
                )
            document = _select_document(connection, document_id)
            if document is None:
                raise KeyError(f"document_id={document_id} は存在しません。")
            return _to_document_detail(document).model_copy(
                update={
                    "knowledge_bases": _select_document_knowledge_base_refs(
                        connection,
                        document_id,
                    )
                }
            )

        return await self._run_transaction(operation)

    async def _save_preprocess_artifact_with_oracle(
        self,
        document_id: str,
        artifact: DocumentPreprocessArtifact | None,
    ) -> DocumentDetail:
        """Oracle document table へファイル準備出力情報を保存する。"""

        def operation(connection: OracleConnectionProtocol) -> DocumentDetail:
            _execute(
                connection,
                _render_sql(
                    """
                UPDATE rag_documents
                SET preprocess_artifact = :preprocess_artifact
                WHERE document_id = :document_id
                  AND {access_predicate}
                """,
                    access_predicate=_oracle_access_predicate_sql(),
                ),
                _with_tenant_bind(
                    {
                        "document_id": document_id,
                        "preprocess_artifact": (
                            _json_dumps(artifact.model_dump(mode="json"))
                            if artifact is not None
                            else None
                        ),
                    }
                ),
            )
            document = _select_document(connection, document_id)
            if document is None:
                raise KeyError(f"document_id={document_id} は存在しません。")
            return _to_document_detail(document).model_copy(
                update={
                    "knowledge_bases": _select_document_knowledge_base_refs(
                        connection,
                        document_id,
                    )
                }
            )

        return await self._run_transaction(operation)

    async def _reset_document_ingestion_outputs_with_oracle(
        self,
        *,
        document_id: str,
        status: FileStatus,
        error_message: str | None,
        clear_preprocess_artifact: bool,
    ) -> DocumentDetail:
        """同一文書の再取込開始時に、旧実体化結果を transaction 内で初期化する。"""

        def operation(connection: OracleConnectionProtocol) -> DocumentDetail:
            existing = _select_document_state(connection, document_id)
            if existing is None:
                raise KeyError(f"document_id={document_id} は存在しません。")

            graph_entity_ids = _select_graph_entity_ids_for_document(connection, document_id)
            _delete_graph_rows_for_document(
                connection,
                document_id=document_id,
                entity_ids=graph_entity_ids,
            )
            _execute(
                connection,
                _render_sql(
                    """
                DELETE FROM rag_ingestion_segments
                WHERE document_id = :document_id
                  AND EXISTS (
                      SELECT 1
                      FROM rag_documents d
                      WHERE d.document_id = rag_ingestion_segments.document_id
                        AND {access_predicate}
                  )
                """,
                    access_predicate=_oracle_access_predicate_sql(alias="d"),
                ),
                _with_tenant_bind({"document_id": document_id}),
            )
            _execute(
                connection,
                _render_sql(
                    """
                DELETE FROM rag_artifact_layers
                WHERE document_id = :document_id
                  AND EXISTS (
                      SELECT 1
                      FROM rag_documents d
                      WHERE d.document_id = rag_artifact_layers.document_id
                        AND {access_predicate}
                  )
                """,
                    access_predicate=_oracle_access_predicate_sql(alias="d"),
                ),
                _with_tenant_bind({"document_id": document_id}),
            )
            _execute(
                connection,
                """
                DELETE FROM rag_chunks
                WHERE document_id = :document_id
                """,
                {"document_id": document_id},
            )
            _execute(
                connection,
                _render_sql(
                    """
                DELETE FROM rag_chunk_sets
                WHERE document_id = :document_id
                  AND EXISTS (
                      SELECT 1
                      FROM rag_documents d
                      WHERE d.document_id = rag_chunk_sets.document_id
                        AND {access_predicate}
                  )
                """,
                    access_predicate=_oracle_access_predicate_sql(alias="d"),
                ),
                _with_tenant_bind({"document_id": document_id}),
            )
            _execute(
                connection,
                _render_sql(
                    """
                DELETE FROM rag_document_extractions
                WHERE document_id = :document_id
                  AND EXISTS (
                      SELECT 1
                      FROM rag_documents d
                      WHERE d.document_id = rag_document_extractions.document_id
                        AND {access_predicate}
                  )
                """,
                    access_predicate=_oracle_access_predicate_sql(alias="d"),
                ),
                _with_tenant_bind({"document_id": document_id}),
            )
            preprocess_reset_sql = (
                "preprocess_artifact = NULL," if clear_preprocess_artifact else ""
            )
            _execute(
                connection,
                _render_sql(
                    """
                UPDATE rag_documents
                SET
                    status = :status,
                    error_message = :error_message,
                    __PREPROCESS_RESET__
                    extraction = NULL,
                    indexed_at = NULL
                WHERE document_id = :document_id
                  AND {access_predicate}
                """.replace("__PREPROCESS_RESET__", preprocess_reset_sql),
                    access_predicate=_oracle_access_predicate_sql(),
                ),
                _with_tenant_bind(
                    {
                        "document_id": document_id,
                        "status": status.value,
                        "error_message": error_message,
                    }
                ),
            )
            document = _select_document(connection, document_id)
            if document is None:
                raise KeyError(f"document_id={document_id} は存在しません。")
            return _to_document_detail(document).model_copy(
                update={
                    "knowledge_bases": _select_document_knowledge_base_refs(
                        connection,
                        document_id,
                    )
                }
            )

        return await self._run_transaction(operation)

    async def _reset_document_chunk_outputs_with_oracle(
        self,
        *,
        document_id: str,
        status: FileStatus,
        error_message: str | None,
    ) -> DocumentDetail:
        """同一文書の再 chunk 前に、chunk 以降を transaction 内で初期化する。"""

        def operation(connection: OracleConnectionProtocol) -> DocumentDetail:
            existing = _select_document_state(connection, document_id)
            if existing is None:
                raise KeyError(f"document_id={document_id} は存在しません。")

            graph_entity_ids = _select_graph_entity_ids_for_document(connection, document_id)
            _delete_graph_rows_for_document(
                connection,
                document_id=document_id,
                entity_ids=graph_entity_ids,
            )
            _execute(
                connection,
                _render_sql(
                    """
                DELETE FROM rag_artifact_layers
                WHERE document_id = :document_id
                  AND EXISTS (
                      SELECT 1 FROM rag_documents d
                      WHERE d.document_id = rag_artifact_layers.document_id
                        AND {access_predicate}
                  )
                """,
                    access_predicate=_oracle_access_predicate_sql(alias="d"),
                ),
                _with_tenant_bind({"document_id": document_id}),
            )
            _execute(
                connection,
                "DELETE FROM rag_chunks WHERE document_id = :document_id",
                {"document_id": document_id},
            )
            _execute(
                connection,
                _render_sql(
                    """
                DELETE FROM rag_chunk_sets
                WHERE document_id = :document_id
                  AND EXISTS (
                      SELECT 1 FROM rag_documents d
                      WHERE d.document_id = rag_chunk_sets.document_id
                        AND {access_predicate}
                  )
                """,
                    access_predicate=_oracle_access_predicate_sql(alias="d"),
                ),
                _with_tenant_bind({"document_id": document_id}),
            )
            _execute(
                connection,
                _render_sql(
                    """
                UPDATE rag_documents
                SET status = :status,
                    error_message = :error_message,
                    indexed_at = NULL
                WHERE document_id = :document_id
                  AND {access_predicate}
                """,
                    access_predicate=_oracle_access_predicate_sql(),
                ),
                _with_tenant_bind(
                    {
                        "document_id": document_id,
                        "status": status.value,
                        "error_message": error_message,
                    }
                ),
            )
            document = _select_document(connection, document_id)
            if document is None:
                raise KeyError(f"document_id={document_id} は存在しません。")
            return _to_document_detail(document).model_copy(
                update={
                    "knowledge_bases": _select_document_knowledge_base_refs(
                        connection,
                        document_id,
                    )
                }
            )

        return await self._run_transaction(operation)

    async def _reset_document_index_outputs_with_oracle(
        self,
        *,
        document_id: str,
        status: FileStatus,
        error_message: str | None,
    ) -> DocumentDetail:
        """同一文書の再 index 前に、embedding/index/binding だけ初期化する。"""

        def operation(connection: OracleConnectionProtocol) -> DocumentDetail:
            existing = _select_document_state(connection, document_id)
            if existing is None:
                raise KeyError(f"document_id={document_id} は存在しません。")

            graph_entity_ids = _select_graph_entity_ids_for_document(connection, document_id)
            _delete_graph_rows_for_document(
                connection,
                document_id=document_id,
                entity_ids=graph_entity_ids,
            )
            _execute(
                connection,
                _render_sql(
                    """
                DELETE FROM rag_artifact_layers
                WHERE document_id = :document_id
                  AND EXISTS (
                      SELECT 1 FROM rag_documents d
                      WHERE d.document_id = rag_artifact_layers.document_id
                        AND {access_predicate}
                  )
                """,
                    access_predicate=_oracle_access_predicate_sql(alias="d"),
                ),
                _with_tenant_bind({"document_id": document_id}),
            )
            _execute(
                connection,
                "UPDATE rag_chunks SET embedding = NULL WHERE document_id = :document_id",
                {"document_id": document_id},
            )
            _execute(
                connection,
                _render_sql(
                    """
                UPDATE rag_chunk_sets
                SET status = 'CHUNKED',
                    vector_count = 0,
                    updated_at = SYSTIMESTAMP
                WHERE document_id = :document_id
                  AND EXISTS (
                      SELECT 1 FROM rag_documents d
                      WHERE d.document_id = rag_chunk_sets.document_id
                        AND {access_predicate}
                  )
                """,
                    access_predicate=_oracle_access_predicate_sql(alias="d"),
                ),
                _with_tenant_bind({"document_id": document_id}),
            )
            _execute(
                connection,
                _render_sql(
                    """
                UPDATE rag_documents
                SET status = :status,
                    error_message = :error_message,
                    indexed_at = NULL
                WHERE document_id = :document_id
                  AND {access_predicate}
                """,
                    access_predicate=_oracle_access_predicate_sql(),
                ),
                _with_tenant_bind(
                    {
                        "document_id": document_id,
                        "status": status.value,
                        "error_message": error_message,
                    }
                ),
            )
            document = _select_document(connection, document_id)
            if document is None:
                raise KeyError(f"document_id={document_id} は存在しません。")
            return _to_document_detail(document).model_copy(
                update={
                    "knowledge_bases": _select_document_knowledge_base_refs(
                        connection,
                        document_id,
                    )
                }
            )

        return await self._run_transaction(operation)

    async def _delete_document_with_oracle(self, document_id: str) -> bool:
        """Oracle document table と関連 chunk/vector/ingestion 行を同一 transaction で削除する。"""

        def operation(connection: OracleConnectionProtocol) -> bool:
            return delete_document_rows(connection, document_id)

        return await self._run_transaction(operation, retry_deadlocks=True)

    async def _save_extraction_with_oracle(
        self,
        document_id: str,
        extraction: StructuredExtraction,
    ) -> DocumentDetail:
        """Oracle document table へ構造化抽出を保存する。"""

        if release := active_release():
            extraction = extraction.model_copy(
                update={
                    "parser_artifacts": {
                        **extraction.parser_artifacts,
                        "language_pack_release_id": release.id,
                        "language_pack_ingest_hash": release.ingest_hash,
                    }
                }
            )

        def operation(connection: OracleConnectionProtocol) -> DocumentDetail:
            _execute(
                connection,
                _render_sql(
                    """
                UPDATE rag_documents
                SET extraction = :extraction
                WHERE document_id = :document_id
                  AND {access_predicate}
                """,
                    access_predicate=_oracle_access_predicate_sql(),
                ),
                _with_tenant_bind(
                    {
                        "document_id": document_id,
                        "extraction": _json_dumps(extraction.to_document_payload()),
                    }
                ),
            )
            document = _select_document(connection, document_id)
            if document is None:
                raise KeyError(f"document_id={document_id} は存在しません。")
            return _to_document_detail(document).model_copy(
                update={
                    "knowledge_bases": _select_document_knowledge_base_refs(
                        connection,
                        document_id,
                    )
                }
            )

        return await self._run_transaction(operation)

    @staticmethod
    def _chunk_insert_rows(
        document_id: str,
        document: StoredDocument,
        chunks: list[Chunk],
        embeddings: Sequence[Sequence[float] | None],
        chunk_set_id: str | None = None,
    ) -> list[dict[str, object]]:
        """rag_chunks へ挿入する bind row を構築する。chunk_set_id=None は未タグ(後方互換)。

        chunk_id は chunk_set_id 指定時 ``document:chunk_set:index`` で chunk_set 間衝突を避ける。
        None のときは現行どおり ``document:index``。
        """

        def chunk_id_for(index: int) -> str:
            if chunk_set_id is not None:
                return f"{document_id}:{chunk_set_id}:{index}"
            return f"{document_id}:{index}"

        return [
            {
                "chunk_id": chunk_id_for(chunk.index),
                "document_id": document_id,
                "tenant_id_hash": document.tenant_id_hash,
                "chunk_index": chunk.index,
                "chunk_text": chunk.text,
                "search_text": _chunk_search_text(chunk),
                "metadata_json": _json_dumps(
                    {
                        "document_id": document_id,
                        "chunk_id": chunk_id_for(chunk.index),
                        "chunk_index": chunk.index,
                        "start_offset": chunk.start_offset,
                        "end_offset": chunk.end_offset,
                        # 1 ページ目の本文は chunk の行に入れず chunk set に保存する(#557)。
                        **{
                            key: value
                            for key, value in chunk.metadata.items()
                            if key != FIRST_PAGE_CONTEXT_KEY
                        },
                    }
                ),
                "embedding": None if embedding is None else _to_vector_bind(embedding),
                "chunk_set_id": chunk_set_id,
            }
            for chunk, embedding in zip(chunks, embeddings, strict=True)
        ]

    @staticmethod
    def _retrieved_chunks_from_insert_rows(
        document_id: str,
        document: StoredDocument,
        rows: Sequence[Mapping[str, object]],
    ) -> list[RetrievedChunk]:
        """保存直後の chunk row を API 返却用 schema へ変換する。"""
        return [
            RetrievedChunk(
                document_id=document_id,
                chunk_id=str(row["chunk_id"]),
                text=str(row["chunk_text"]),
                score=1.0,
                file_name=document.file_name,
                category_name=document.category_name,
                metadata=_json_loads(row["metadata_json"]),
            )
            for row in rows
        ]

    async def _save_index_with_oracle(
        self,
        document_id: str,
        extraction: StructuredExtraction,
        chunks: list[Chunk],
        embeddings: Sequence[Sequence[float] | None],
        chunk_set_id: str | None = None,
    ) -> list[RetrievedChunk]:
        """抽出 payload と chunk/vector を同一 Oracle transaction で置換する。

        chunk_set_id を渡すと chunk 置換を **その chunk_set に限定**(他 chunk_set の chunk は
        残す)し、挿入 chunk をその chunk_set でタグ付けする。None は文書の全 chunk を置換し
        未タグで保存する(現行挙動・後方互換)。

        親子階層で分割した文書の 1 ページ目の本文(先頭の chunk の ``FIRST_PAGE_CONTEXT_KEY``)は、
        chunk set に 1 つだけ保存する(#557)。chunk set の行は chunk の保存の後に
        ``upsert_chunk_set`` が作るため、無ければここで作る(状態などは後の upsert / mark が書く)。
        """
        first_page_context = (
            next(
                (
                    json.loads(str(chunk.metadata[FIRST_PAGE_CONTEXT_KEY]))
                    for chunk in chunks
                    if chunk.metadata.get(FIRST_PAGE_CONTEXT_KEY)
                ),
                None,
            )
            if chunk_set_id is not None
            else None
        )

        def operation(connection: OracleConnectionProtocol) -> list[RetrievedChunk]:
            document = _select_document_state(connection, document_id)
            if document is None:
                raise KeyError(f"document_id={document_id} は存在しません。")
            _execute(
                connection,
                _render_sql(
                    """
                UPDATE rag_documents
                SET extraction = :extraction
                WHERE document_id = :document_id
                  AND {access_predicate}
                """,
                    access_predicate=_oracle_access_predicate_sql(),
                ),
                _with_tenant_bind(
                    {
                        "document_id": document_id,
                        "extraction": _json_dumps(extraction.to_document_payload()),
                    }
                ),
            )
            # chunk_set_id 指定時はその chunk_set の chunk だけ置換する(複数 chunk_set 共存可)。
            chunk_set_clause = (
                "AND chunk_set_id = :chunk_set_id" if chunk_set_id is not None else ""
            )
            delete_binds: dict[str, object] = {"document_id": document_id}
            if chunk_set_id is not None:
                delete_binds["chunk_set_id"] = chunk_set_id
            _execute(
                connection,
                _render_sql(
                    """
                DELETE FROM rag_chunks
                WHERE document_id = :document_id
                  {chunk_set_clause}
                """,
                    chunk_set_clause=chunk_set_clause,
                ),
                delete_binds,
            )
            rows = self._chunk_insert_rows(
                document_id, document, chunks, embeddings, chunk_set_id=chunk_set_id
            )
            if rows:
                _executemany(
                    connection,
                    """
                    INSERT INTO rag_chunks (
                        chunk_id,
                        document_id,
                        tenant_id_hash,
                        chunk_index,
                        chunk_text,
                        search_text,
                        metadata_json,
                        embedding,
                        chunk_set_id
                    ) VALUES (
                        :chunk_id,
                        :document_id,
                        :tenant_id_hash,
                        :chunk_index,
                        :chunk_text,
                        :search_text,
                        :metadata_json,
                        :embedding,
                        :chunk_set_id
                    )
                    """,
                    rows,
                    input_sizes=_json_input_sizes("metadata_json"),
                )
            if first_page_context is not None:
                _execute(
                    connection,
                    """
                    MERGE INTO rag_chunk_sets t
                    USING (SELECT :chunk_set_id AS chunk_set_id FROM dual) s
                    ON (t.chunk_set_id = s.chunk_set_id)
                    WHEN MATCHED THEN UPDATE SET
                        t.first_page_context = :first_page_context
                    WHEN NOT MATCHED THEN INSERT
                        (chunk_set_id, document_id, tenant_id_hash, first_page_context)
                        VALUES (
                            :chunk_set_id, :document_id, :tenant_id_hash, :first_page_context
                        )
                    """,
                    {
                        "chunk_set_id": chunk_set_id,
                        "document_id": document_id,
                        "tenant_id_hash": document.tenant_id_hash,
                        "first_page_context": _json_bind(first_page_context),
                    },
                    input_sizes=_json_input_sizes("first_page_context"),
                )
            return self._retrieved_chunks_from_insert_rows(document_id, document, rows)

        return await self._run_transaction(operation)

    async def _replace_document_graph_index_with_oracle(
        self,
        document_id: str,
        graph_index: GraphIndex,
        *,
        chunk_set_id: str | None = None,
    ) -> None:
        """関係情報の表の chunk_set scope を置換する。"""

        def operation(connection: OracleConnectionProtocol) -> None:
            if _select_document_state(connection, document_id) is None:
                raise KeyError(f"document_id={document_id} は存在しません。")
            if chunk_set_id is not None:
                rows = _fetch_all(
                    connection,
                    "SELECT entity_id FROM rag_graph_entities WHERE chunk_set_id = :chunk_set_id",
                    {"chunk_set_id": chunk_set_id},
                )
                existing_entity_ids = [
                    str(row["entity_id"]) for row in rows if row.get("entity_id")
                ]
                _delete_graph_rows_for_chunk_set(
                    connection,
                    chunk_set_id=chunk_set_id,
                    entity_ids=existing_entity_ids,
                )
            else:
                existing_entity_ids = _select_graph_entity_ids_for_document(connection, document_id)
                _delete_graph_rows_for_document(
                    connection,
                    document_id=document_id,
                    entity_ids=existing_entity_ids,
                )
            if graph_index.entities:
                _executemany(
                    connection,
                    """
                    INSERT INTO rag_graph_entities (
                        entity_id,
                        chunk_set_id,
                        tenant_id_hash,
                        knowledge_base_id,
                        canonical_name,
                        entity_type,
                        description,
                        confidence,
                        source_document_ids
                    ) VALUES (
                        :entity_id,
                        :chunk_set_id,
                        :tenant_id_hash,
                        :knowledge_base_id,
                        :canonical_name,
                        :entity_type,
                        :description,
                        :confidence,
                        :source_document_ids
                    )
                    """,
                    [
                        {**_graph_entity_binds(entity), "chunk_set_id": chunk_set_id}
                        for entity in graph_index.entities
                    ],
                    input_sizes=_json_input_sizes("source_document_ids"),
                )
            if graph_index.relationships:
                _executemany(
                    connection,
                    """
                    INSERT INTO rag_graph_relationships (
                        relationship_id,
                        chunk_set_id,
                        tenant_id_hash,
                        knowledge_base_id,
                        source_entity_id,
                        target_entity_id,
                        relationship_type,
                        description,
                        confidence,
                        source_document_ids
                    ) VALUES (
                        :relationship_id,
                        :chunk_set_id,
                        :tenant_id_hash,
                        :knowledge_base_id,
                        :source_entity_id,
                        :target_entity_id,
                        :relationship_type,
                        :description,
                        :confidence,
                        :source_document_ids
                    )
                    """,
                    [
                        {
                            **_graph_relationship_binds(relationship),
                            "chunk_set_id": chunk_set_id,
                        }
                        for relationship in graph_index.relationships
                    ],
                    input_sizes=_json_input_sizes("source_document_ids"),
                )
            if graph_index.entity_chunk_links:
                _executemany(
                    connection,
                    """
                    INSERT INTO rag_graph_entity_chunks (
                        entity_id,
                        chunk_id,
                        chunk_set_id,
                        document_id,
                        tenant_id_hash,
                        relevance_score
                    ) VALUES (
                        :entity_id,
                        :chunk_id,
                        :chunk_set_id,
                        :document_id,
                        :tenant_id_hash,
                        :relevance_score
                    )
                    """,
                    [
                        {
                            **_graph_entity_chunk_link_binds(link),
                            "chunk_set_id": chunk_set_id,
                        }
                        for link in graph_index.entity_chunk_links
                    ],
                )

        await self._run_transaction(operation)

    async def _fetch_one(
        self, statement: str, binds: Mapping[str, object] | None = None
    ) -> dict[str, object] | None:
        """Oracle から 1 行を取得する。"""
        rows = await self._fetch_all(statement, binds)
        return rows[0] if rows else None

    async def _fetch_all(
        self, statement: str, binds: Mapping[str, object] | None = None
    ) -> list[dict[str, object]]:
        """Oracle から行を dict として取得する。"""

        def fetch() -> list[dict[str, object]]:
            return cast(
                list[dict[str, object]],
                self._run_with_connection(
                    lambda connection: _fetch_all(connection, statement, binds or {})
                ),
            )

        try:
            return cast(list[dict[str, object]], await self._db_call_runner(fetch))
        except Exception as exc:
            if not is_transient_oracle_error(exc):
                raise
            logger.warning("oracle_read_retry", extra=oracle_error_log_fields(exc))
            return cast(list[dict[str, object]], await self._db_call_runner(fetch))

    async def _fetch_ingestion_job_rows(
        self, statement: str, binds: Mapping[str, object] | None = None
    ) -> list[dict[str, object]]:
        """max_attempts 列が未適用の旧 queue table でも ingestion job 行を取得する。"""
        try:
            return await self._fetch_all(statement, binds)
        except Exception as exc:
            if not _is_missing_ingestion_job_max_attempts_error(exc):
                raise
            legacy_binds = _with_default_max_attempts_bind(
                binds or {},
                self._settings.ingestion_job_max_attempts,
            )
            return await self._fetch_all(
                _legacy_ingestion_job_max_attempts_select_sql(statement),
                legacy_binds,
            )

    async def _run_transaction(
        self, operation: Callable[[OracleConnectionProtocol], T], *, retry_deadlocks: bool = False
    ) -> T:
        """Oracle transaction を同期 SDK thread で実行する。"""
        return cast(
            T,
            await self._db_call_runner(
                lambda: self._run_transaction_sync(operation, retry_deadlocks=retry_deadlocks)
            ),
        )

    def _run_transaction_sync(
        self, operation: Callable[[OracleConnectionProtocol], T], *, retry_deadlocks: bool = False
    ) -> T:
        """1 transaction を実行する。一時的な接続断は新しい接続で 1 回だけやり直す(#341)。

        やり直すのは、commit の前(``operation`` の中)で一時的な接続断
        (``is_session_dead`` / ``isrecoverable``)になった場合だけ。接続が切れた transaction は
        DB 側で rollback されるため、``operation`` を最初から実行し直しても二重に反映しない。
        commit の途中で切れた場合は反映済みか分からないため、やり直さずに例外を返す。
        削除が明示的に指定した場合は、ORA-00060 も全体の rollback 成功後に一度だけ再試行する。
        rollback / close の失敗(切断後の ``DPY-1001`` など)はログに残し、元の例外を隠さない。
        """
        for attempt in (1, 2):
            connection = self._acquire_connection()
            committing = False
            try:
                result = operation(connection)
                committing = True
                connection.commit()
                return result
            except Exception as exc:
                # rollback / close の例外で、元の失敗段階と下位コードを失わないよう先に記録する。
                logger.warning(
                    "oracle_transaction_failed",
                    extra={
                        **oracle_error_log_fields(exc),
                        "transaction_phase": "commit" if committing else "operation",
                        "transaction_attempt": attempt,
                    },
                )
                rolled_back = _rollback_quietly(connection, exc)
                retryable = is_transient_oracle_error(exc) or (
                    retry_deadlocks and rolled_back and "ORA-00060" in oracle_error_codes(exc)
                )
                if committing or attempt > 1 or not retryable:
                    raise
                logger.warning("oracle_transaction_retry", extra=oracle_error_log_fields(exc))
            finally:
                _close_quietly(connection)
        raise RuntimeError("unreachable: transaction の再試行は 1 回まで")  # pragma: no cover

    def _run_with_connection(self, operation: Callable[[OracleConnectionProtocol], Any]) -> Any:
        connection = self._acquire_connection()
        try:
            result = operation(connection)
        except Exception:
            with suppress(Exception):
                connection.close()
            raise
        else:
            connection.close()
            return result

    def _acquire_connection(self) -> OracleConnectionProtocol:
        """pool から connection を取得する。"""
        return self.acquire_connection()

    def acquire_connection(self) -> OracleConnectionProtocol:
        """共有 pool から connection を借りる（返すのは呼び出し側の `close()`）。

        DB の停止中に作った pool が起動後も接続を返さない（`DPY-4005`）ときは、その pool を捨てて
        新しい pool で 1 回だけやり直す。接続できないときは pool を捨てて例外を返す（次の要求は
        新しい pool から始める）。判定は 3 製品共通の `recover_pool_error`（#820）。
        """
        pool = self._pool()
        try:
            return pool.acquire()
        except Exception as exc:
            if self._pool_instance is not None:
                raise  # 明示の pool（テスト・呼び出し側の管理）は作り直さない
            action = recover_pool_error(pool, exc)
            if action == "raise":
                raise
            _discard_shared_pool(pool, reason=action, exc=exc)
            if action == "discard":
                raise
        fresh = self._pool()
        try:
            return fresh.acquire()
        except Exception as exc:
            if recover_pool_error(fresh, exc) != "raise":
                _discard_shared_pool(fresh, reason="retry_failed", exc=exc)
            raise

    def connection_pool(self) -> OraclePoolProtocol:
        """共有 connection pool を返す。"""
        return self._pool()

    def _pool(self) -> OraclePoolProtocol:
        """python-oracledb connection pool を遅延初期化する。"""
        if self._pool_instance is not None:
            return self._pool_instance
        global _SHARED_ORACLE_POOL, _SHARED_ORACLE_POOL_KEY
        key = _shared_pool_settings_key(self._settings)
        stale: OraclePoolProtocol | None = None
        try:
            with _SHARED_ORACLE_POOL_LOCK:
                if _SHARED_ORACLE_POOL is not None:
                    recorded = _SHARED_ORACLE_POOL_KEY
                    if (
                        recorded is None
                        or recorded[0] != id(_SHARED_ORACLE_POOL)
                        or recorded[1] == key
                    ):
                        return _SHARED_ORACLE_POOL
                    # 接続の設定が変わった（別の製品・別の worker が保存した。#2102）。古い pool は
                    # 貸し出し中の接続を止めずに閉じ、新しい設定で作り直す。
                    stale, _SHARED_ORACLE_POOL = _SHARED_ORACLE_POOL, None
                    _SHARED_ORACLE_POOL_KEY = None
                return self._create_shared_pool_locked(key)
        finally:
            if stale is not None:
                _retire_shared_pool(stale)

    def _create_shared_pool_locked(self, key: str) -> OraclePoolProtocol:
        """共有 pool を作る（`_SHARED_ORACLE_POOL_LOCK` の中で呼ぶ）。"""
        global _SHARED_ORACLE_POOL, _SHARED_ORACLE_POOL_KEY
        oracledb = importlib.import_module("oracledb")
        _init_oracle_client(oracledb, self._settings)
        pool_kwargs = shared_pool_connect_kwargs(self._settings)
        pool_kwargs.update(
            {
                "min": min(_SHARED_ORACLE_POOL_MIN, self._settings.oracle_pool_max),
                "max": self._settings.oracle_pool_max,
                "increment": 1,
                # 接続を返せないときに無期限に待たない（壊れた pool を作り直すため。#820）。
                "getmode": oracledb.POOL_GETMODE_TIMEDWAIT,
                "wait_timeout": DEFAULT_WAIT_TIMEOUT_MS,
                # result cache を使わない（ADB の内部エラーと接続断を避ける。#333）。
                "session_callback": init_oracle_session,
            }
        )
        _SHARED_ORACLE_POOL = oracledb.create_pool(**pool_kwargs)
        _SHARED_ORACLE_POOL_KEY = (id(_SHARED_ORACLE_POOL), key)
        return _SHARED_ORACLE_POOL

    def _validate_embedding_width(self, embedding: list[float], label: str) -> None:
        """Oracle の `EMBEDDING_VECTOR_COLUMN_TYPE` の列に保存/検索できる幅か検証する。"""
        expected_dim = self._settings.genai_embedding_dim
        if len(embedding) != expected_dim:
            raise ValueError(
                f"{label} の次元数が不正です。expected={expected_dim}, actual={len(embedding)}"
            )


async def _run_db_call_in_thread(operation: Callable[[], Any]) -> Any:
    """同期 python-oracledb 呼び出しを event loop 外で実行する。"""
    return await asyncio.to_thread(operation)


async def _run_db_test_call_in_thread(operation: Callable[[], Any]) -> Any:
    """接続テスト専用の小さい thread pool で DB 呼び出しを実行する。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_DB_TEST_EXECUTOR, operation)


def _oracle_error_object(exc: BaseException) -> object | None:
    """python-oracledb の例外が持つ ``_Error``(``exc.args[0]``)を返す。"""
    return exc.args[0] if exc.args else None


def is_transient_oracle_error(exc: BaseException) -> bool:
    """新しい接続でやり直せる一時的な接続断か(python-oracledb の判定をそのまま使う)。

    ``is_session_dead``(``DPY-4011`` など、接続が切れた)か ``isrecoverable``
    (``ORA-01033`` など、DB が一時的に使えない)が立っている例外だけを一時的とみなす。
    """
    error = _oracle_error_object(exc)
    return bool(getattr(error, "is_session_dead", False) or getattr(error, "isrecoverable", False))


def oracle_error_log_fields(exc: BaseException) -> dict[str, object]:
    """エラーログに必ず付ける Oracle のエラー情報(Oracle の例外でなければ値は None)。

    ``oracle_error_code`` は Oracle のエラー番号(``ORA-00600`` なら 600、driver の ``DPY-*`` は 0)、
    ``full_code`` は ``ORA-00600`` / ``DPY-4011`` のような接頭辞付きのコード。
    ``oracle_error_codes`` は原因の連鎖と下位の Oracle Text のコードも含む。本文は返さない。
    """
    error = _oracle_error_object(exc)
    code = getattr(error, "code", None)
    full_code = getattr(error, "full_code", None)
    return {
        "error_type": type(exc).__name__,
        "oracle_error_code": code if isinstance(code, int) else None,
        "full_code": (
            full_code.upper()
            if isinstance(full_code, str) and ORACLE_ERROR_CODE_RE.fullmatch(full_code)
            else None
        ),
        "oracle_error_codes": oracle_error_codes(exc),
        "is_session_dead": bool(getattr(error, "is_session_dead", False)),
        "isrecoverable": bool(getattr(error, "isrecoverable", False)),
    }


def _rollback_quietly(connection: OracleConnectionProtocol, original: BaseException) -> bool:
    """rollback する。失敗しても(切断後の ``DPY-1001`` など)ログに残して元の例外を優先する。"""
    try:
        connection.rollback()
        return True
    except Exception as rollback_error:  # noqa: BLE001 - 元の例外を隠さない
        logger.warning(
            "oracle_rollback_failed",
            extra={
                **oracle_error_log_fields(rollback_error),
                "original_error_type": type(original).__name__,
                "original_full_code": oracle_error_log_fields(original)["full_code"],
            },
        )
        return False


def _close_quietly(connection: OracleConnectionProtocol) -> None:
    """接続を pool に返す。切断済みの接続の close の失敗はログだけにする。"""
    try:
        connection.close()
    except Exception as close_error:  # noqa: BLE001 - 結果・元の例外を優先する
        logger.warning("oracle_connection_close_failed", extra=oracle_error_log_fields(close_error))


def _fetch_all(
    connection: OracleConnectionProtocol,
    statement: str,
    binds: Mapping[str, object],
) -> list[dict[str, object]]:
    cursor = connection.cursor()
    started = perf_counter()
    try:
        if (fetch_rows := rows_per_round_trip()) is not None:
            # 1 回の往復で受け取る行の数(execute の前に決める。#1498)。
            cursor.prefetchrows = fetch_rows  # type: ignore[attr-defined]
            cursor.arraysize = fetch_rows  # type: ignore[attr-defined]
        normalized = _normalize_sql(statement)
        cursor.execute(
            normalized,
            _binds_for_sql(normalized, binds),
            **_lob_value_execute_options(cursor),
        )
        rows = cursor.fetchall()
        result = [_row_to_dict(row, cursor.description) for row in rows]
    except Exception:
        with suppress(Exception):
            cursor.close()
        raise
    else:
        cursor.close()
        _record_db_execute(started, rows=len(result))
        return result


def _lob_value_execute_options(cursor: object) -> dict[str, object]:
    """``read_lobs_as_values()`` の中なら、LOB を値で受け取る ``execute`` の引数(#1490)。

    CLOB は str、BLOB は bytes で届き、``_read_db_value`` が ``.read()`` した値と同じになる。
    ``fetch_lobs`` を受けない cursor(テストの代役など)には渡さない。
    """
    if not lobs_as_values_enabled() or not _execute_accepts_fetch_lobs(type(cursor)):
        return {}
    return {"fetch_lobs": False}


# cursor の型ごとに、``execute`` が ``fetch_lobs`` を受けるか(python-oracledb の Cursor は受ける)。
_FETCH_LOBS_SUPPORT: dict[type, bool] = {}


def _execute_accepts_fetch_lobs(cursor_type: type) -> bool:
    supported = _FETCH_LOBS_SUPPORT.get(cursor_type)
    if supported is None:
        execute = getattr(cursor_type, "execute", None)
        try:
            supported = execute is not None and (
                "fetch_lobs" in inspect.signature(execute).parameters
            )
        except (TypeError, ValueError):
            supported = False
        _FETCH_LOBS_SUPPORT[cursor_type] = supported
    return supported


def _fetch_one(
    connection: OracleConnectionProtocol,
    statement: str,
    binds: Mapping[str, object],
) -> dict[str, object] | None:
    rows = _fetch_all(connection, statement, binds)
    return rows[0] if rows else None


def _execute(
    connection: OracleConnectionProtocol,
    statement: str,
    binds: Mapping[str, object],
    *,
    input_sizes: Mapping[str, object] | None = None,
) -> None:
    cursor = connection.cursor()
    started = perf_counter()
    try:
        normalized = _normalize_sql(statement)
        sql_binds = _binds_for_sql(normalized, binds)
        sizes = _large_text_input_sizes(
            sql_binds, _binds_for_sql(normalized, input_sizes) if input_sizes else None
        )
        if sizes:
            cursor.setinputsizes(**sizes)
        cursor.execute(normalized, sql_binds)
    finally:
        cursor.close()
        _record_db_execute(started)


# 型を指定しない文字列の bind を CLOB にする長さ（UTF-8 のバイト数。#2342）。
LARGE_TEXT_BIND_BYTES = 4000


def _large_text_input_sizes(
    binds: Mapping[str, object],
    input_sizes: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """bind の型の指定に、長い文字列（``LARGE_TEXT_BIND_BYTES`` 以上）の CLOB を足す（#2342）。

    python-oracledb（thin）は ``str`` を VARCHAR で送る。8192 バイト以上の VARCHAR の bind を
    CLOB の列へ渡すと、同じ bind を 2 か所で使う ``MERGE`` は ``ORA-03146``（接続も壊れる）、
    32KB を超える別の bind（LONG で送る）と混ざる ``UPDATE`` は値が壊れて JSON 列が
    ``ORA-40441`` になる。長い本文の回答で回答の記録と会話の回答を保存できなかった（#2342）。
    長い文字列は CLOB で送る（VARCHAR2 の列にも暗黙の変換で入る。値は切り詰めない）。
    呼び出し側が型を指定した bind はそのままにする。
    """
    sizes = dict(input_sizes or {})
    clob: object | None = None
    for name, value in binds.items():
        if name in sizes or not isinstance(value, str) or not _is_large_text(value):
            continue
        if clob is None:
            clob = importlib.import_module("oracledb").DB_TYPE_CLOB
        sizes[name] = clob
    return sizes


def _is_large_text(value: str) -> bool:
    # 1 文字は UTF-8 で 4 バイトまで。短い文字列は符号化せずに判定する。
    if len(value) * 4 < LARGE_TEXT_BIND_BYTES:
        return False
    if len(value) >= LARGE_TEXT_BIND_BYTES:
        return True
    return len(value.encode("utf-8")) >= LARGE_TEXT_BIND_BYTES


def _execute_count(
    connection: OracleConnectionProtocol,
    statement: str,
    binds: Mapping[str, object],
) -> int:
    """DML を実行して影響行数を返す。"""
    cursor = connection.cursor()
    started = perf_counter()
    try:
        normalized = _normalize_sql(statement)
        # `_with_tenant_bind` の予防的な bind を含めて渡せるよう、SQL に現れる名前へ絞る。
        sql_binds = _binds_for_sql(normalized, binds)
        sizes = _large_text_input_sizes(sql_binds)
        if sizes:
            cursor.setinputsizes(**sizes)
        cursor.execute(normalized, sql_binds)
        return int(getattr(cursor, "rowcount", 0) or 0)
    finally:
        cursor.close()
        _record_db_execute(started)


def _record_db_execute(started: float, *, rows: int = 0) -> None:
    """計測の範囲(``track_db_calls``)の中なら、SQL の 1 回の実行を数える(#1490)。"""
    stats = current_db_call_stats()
    if stats is not None:
        stats.record_execute(rows=rows, seconds=perf_counter() - started)


# 品質評価の job を、投入した利用者（tenant と user の hash）だけが見られるようにする条件（#390）。
# hash が無い（認証なしの開発環境など）場合は、hash の無い job だけを対象にする。
_EVALUATION_JOB_OWNER_SQL = (
    "(tenant_id_hash = :tenant_id_hash OR (tenant_id_hash IS NULL AND :tenant_id_hash IS NULL)) "
    "AND (user_id_hash = :user_id_hash OR (user_id_hash IS NULL AND :user_id_hash IS NULL))"
)


def _truncate_text(value: str | None, limit: int) -> str | None:
    """VARCHAR2 の列に収まるように文字数で切る。"""
    if value is None:
        return None
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _execute_count_with_input_sizes(
    connection: OracleConnectionProtocol,
    statement: str,
    binds: Mapping[str, object],
    *,
    input_sizes: Mapping[str, object],
) -> int:
    """JSON 列などの input size を指定して DML を実行し、影響行数を返す。"""
    cursor = connection.cursor()
    try:
        normalized = _normalize_sql(statement)
        sql_binds = _binds_for_sql(normalized, binds)
        sizes = _large_text_input_sizes(sql_binds, _binds_for_sql(normalized, input_sizes))
        if sizes:
            cursor.setinputsizes(**sizes)
        cursor.execute(normalized, sql_binds)
        return int(getattr(cursor, "rowcount", 0) or 0)
    finally:
        cursor.close()


def _executemany(
    connection: OracleConnectionProtocol,
    statement: str,
    rows: Sequence[Mapping[str, object]],
    *,
    input_sizes: Mapping[str, object] | None = None,
) -> None:
    """``input_sizes`` は bind の型(JSON 列は ``_json_input_sizes`` の CLOB)。

    JSON 型の列へ文字列を bind したまま executemany すると、32KB を超える行が混ざったときに
    ORA-40441(JSON の構文の誤り)になる(#2043)。JSON 列を書く executemany は必ず CLOB で bind する。
    """
    cursor = connection.cursor()
    try:
        # 長い文字列の列は、どの行かに長い値があれば全行を CLOB で送る（#2342）。
        sizes = dict(input_sizes or {})
        for row in rows:
            sizes = _large_text_input_sizes(row, sizes)
        if sizes:
            cursor.setinputsizes(**sizes)
        cursor.executemany(_normalize_sql(statement), rows)
    finally:
        cursor.close()


def delete_document_rows(connection: OracleConnectionProtocol, document_id: str) -> bool:
    """文書と関連 chunk/graph/ingestion 行を、呼び出し側の transaction の中で削除する。

    文書が無ければ False。実行中の取込 job があれば
    ``DocumentDeleteBlockedByRunningIngestionError``。
    文書の削除と同じ transaction で後始末の記録を残すとき(#2452)にも使う。
    """
    # 参照を外す行も含めて ID 順にロックする。先に権限を確認し、対象が消えたら何もしない。
    locked = _fetch_all(
        connection,
        _render_sql(
            """
        SELECT {columns}
        FROM rag_documents
        WHERE (document_id = :document_id
            OR duplicate_of_document_id = :document_id
            OR superseded_by_document_id = :document_id)
          AND {tenant_sql}
          AND EXISTS (
              SELECT 1 FROM rag_documents target
              WHERE target.document_id = :document_id AND {access_sql}
          )
        ORDER BY document_id
        FOR UPDATE
        """,
            columns=_DOCUMENT_STATE_COLUMNS_SQL.strip(),
            tenant_sql=_oracle_tenant_predicate(),
            access_sql=_oracle_access_predicate_sql(alias="target"),
        ),
        _with_tenant_bind({"document_id": document_id}),
    )
    if not any(str(row.get("document_id")) == document_id for row in locked):
        return False
    graph_entity_ids = _select_graph_entity_ids_for_document(connection, document_id)
    _delete_graph_rows_for_document(
        connection,
        document_id=document_id,
        entity_ids=graph_entity_ids,
    )
    _execute(
        connection,
        _render_sql(
            """
        UPDATE rag_documents
        SET duplicate_of_document_id = NULL
        WHERE duplicate_of_document_id = :document_id
          AND {tenant_sql}
        """,
            # 正本を消す前に FK の参照を外す。利用者の範囲外の複製も対象にする。
            tenant_sql=_oracle_tenant_predicate(),
        ),
        _with_tenant_bind({"document_id": document_id}),
    )
    _execute(
        connection,
        _render_sql(
            """
        UPDATE rag_documents
        SET superseded_by_document_id = NULL,
            superseded_at = NULL
        WHERE superseded_by_document_id = :document_id
          AND {tenant_sql}
        """,
            # 新しい版を消したら、それに置き換えられていた文書を今有効な版に戻す(#1248)。
            # 参照が残ると、旧版は検索から外れたままになる。
            tenant_sql=_oracle_tenant_predicate(),
        ),
        _with_tenant_bind({"document_id": document_id}),
    )
    _execute(
        connection,
        _render_sql(
            """
        DELETE FROM rag_ingestion_segments
        WHERE document_id = :document_id
          AND EXISTS (
              SELECT 1
              FROM rag_documents d
              WHERE d.document_id = rag_ingestion_segments.document_id
                AND {access_predicate}
          )
        """,
            access_predicate=_oracle_access_predicate_sql(alias="d"),
        ),
        _with_tenant_bind({"document_id": document_id}),
    )
    _execute(
        connection,
        _render_sql(
            """
        DELETE FROM rag_ingestion_jobs
        WHERE document_id = :document_id
          AND status <> 'RUNNING'
          AND EXISTS (
              SELECT 1
              FROM rag_documents d
              WHERE d.document_id = rag_ingestion_jobs.document_id
                AND {access_predicate}
          )
        """,
            access_predicate=_oracle_access_predicate_sql(alias="d"),
        ),
        _with_tenant_bind({"document_id": document_id}),
    )
    running_jobs = _fetch_all(
        connection,
        _render_sql(
            """
        SELECT j.job_id
        FROM rag_ingestion_jobs j
        JOIN rag_documents d
          ON d.document_id = j.document_id
        WHERE j.document_id = :document_id
          AND j.status = 'RUNNING'
          AND {document_access_sql}
        """,
            document_access_sql=_oracle_access_predicate_sql(alias="d"),
        ),
        _with_tenant_bind({"document_id": document_id}),
    )
    if running_jobs:
        raise DocumentDeleteBlockedByRunningIngestionError(
            "取込ジョブが実行中のため削除できません。先にキャンセルしてください。"
        )
    _execute(
        connection,
        """
        DELETE FROM rag_chunks
        WHERE document_id = :document_id
        """,
        {"document_id": document_id},
    )
    _execute(
        connection,
        _render_sql(
            """
        DELETE FROM rag_documents
        WHERE document_id = :document_id
          AND {access_predicate}
        """,
            access_predicate=_oracle_access_predicate_sql(),
        ),
        _with_tenant_bind({"document_id": document_id}),
    )
    return True


def _select_graph_entity_ids_for_document(
    connection: OracleConnectionProtocol,
    document_id: str,
) -> list[str]:
    """指定 document に紐づく既存 graph entity id を取得する。"""
    rows = _fetch_all(
        connection,
        _render_sql(
            """
        SELECT DISTINCT entity_id
        FROM (
            SELECT entity_id
            FROM rag_graph_entity_chunks
            WHERE document_id = :document_id
              AND {graph_access_sql}
            UNION
            SELECT entity_id
            FROM rag_graph_entities
            WHERE JSON_EXISTS(
                      source_document_ids,
                      '$[*]?(@ == $document_id)'
                      PASSING :document_id AS "document_id"
                  )
              AND {graph_access_sql}
        )
        """,
            graph_access_sql=_oracle_tenant_predicate(),
        ),
        _with_tenant_bind({"document_id": document_id}),
    )
    return [str(row["entity_id"]) for row in rows if row.get("entity_id")]


def _delete_graph_rows_for_chunk_set(
    connection: OracleConnectionProtocol,
    *,
    chunk_set_id: str,
    entity_ids: Sequence[str],
) -> None:
    """指定 chunk_set の関係情報の行だけを FK 順に削除する。"""
    binds = _with_tenant_bind({"chunk_set_id": chunk_set_id})
    for table in (
        "rag_graph_relationships",
        "rag_graph_entity_chunks",
    ):
        _execute(
            connection,
            _render_sql(
                f"""
                DELETE FROM {table}
                WHERE chunk_set_id = :chunk_set_id
                  AND {{graph_access_sql}}
                """,
                graph_access_sql=_oracle_tenant_predicate(),
            ),
            binds,
        )
    unique_entity_ids = _unique_optional_sequence(entity_ids)
    if not unique_entity_ids:
        return
    entity_sql, entity_binds = _oracle_in_predicate(
        "entity_id",
        "graph_entity_id",
        unique_entity_ids,
    )
    _execute(
        connection,
        _render_sql(
            """
            DELETE FROM rag_graph_entities
            WHERE chunk_set_id = :chunk_set_id
              AND {entity_sql}
              AND {graph_access_sql}
            """,
            entity_sql=entity_sql,
            graph_access_sql=_oracle_tenant_predicate(),
        ),
        _with_tenant_bind({"chunk_set_id": chunk_set_id, **entity_binds}),
    )


def _delete_graph_rows_for_document(
    connection: OracleConnectionProtocol,
    *,
    document_id: str,
    entity_ids: Sequence[str],
) -> None:
    """指定 document の関係情報の行を FK 順に削除する。"""
    unique_entity_ids = _unique_optional_sequence(entity_ids)
    if unique_entity_ids:
        source_sql, source_binds = _oracle_in_predicate(
            "source_entity_id",
            "graph_source_entity_id",
            unique_entity_ids,
        )
        target_sql, target_binds = _oracle_in_predicate(
            "target_entity_id",
            "graph_target_entity_id",
            unique_entity_ids,
        )
        _execute(
            connection,
            _render_sql(
                """
            DELETE FROM rag_graph_relationships
            WHERE {graph_access_sql}
              AND ({source_sql} OR {target_sql})
            """,
                graph_access_sql=_oracle_tenant_predicate(),
                source_sql=source_sql,
                target_sql=target_sql,
            ),
            _with_tenant_bind({**source_binds, **target_binds}),
        )
    _execute(
        connection,
        _render_sql(
            """
        DELETE FROM rag_graph_entity_chunks
        WHERE document_id = :document_id
          AND {graph_access_sql}
        """,
            graph_access_sql=_oracle_tenant_predicate(),
        ),
        _with_tenant_bind({"document_id": document_id}),
    )
    if unique_entity_ids:
        entity_sql, entity_binds = _oracle_in_predicate(
            "entity_id",
            "graph_entity_id",
            unique_entity_ids,
        )
        _execute(
            connection,
            _render_sql(
                """
            DELETE FROM rag_graph_entities
            WHERE {entity_sql}
              AND {graph_access_sql}
            """,
                entity_sql=entity_sql,
                graph_access_sql=_oracle_tenant_predicate(),
            ),
            _with_tenant_bind(entity_binds),
        )


def _search_audit_binds(event: Mapping[str, object]) -> dict[str, object]:
    """RagSearchAuditEvent JSON を Oracle bind 値へ変換する。"""
    return {
        "event_type": _audit_str(event, "event_type", "rag.search"),
        "trace_id": _audit_str(event, "trace_id", ""),
        "request_id": _audit_optional_str(event, "request_id"),
        "tenant_id_hash": _audit_optional_str(event, "tenant_id_hash"),
        "user_id_hash": _audit_optional_str(event, "user_id_hash"),
        "outcome": _audit_str(event, "outcome", "error"),
        "search_mode": _audit_str(event, "mode", "hybrid"),
        "query_hash": _audit_str(event, "query_hash", ""),
        "query_chars": _audit_int(event, "query_chars"),
        "filter_keys": _audit_json(event.get("filter_keys", [])),
        "memory_plan_id": _audit_optional_str(event, "memory_plan_id"),
        "top_k": _audit_optional_int(event, "top_k"),
        "rerank_top_n": _audit_optional_int(event, "rerank_top_n"),
        "query_variant_count": _audit_int(event, "query_variant_count", default=1),
        "retrieved_count": _audit_int(event, "retrieved_count"),
        "reranked_count": _audit_int(event, "reranked_count"),
        "deduplicated_count": _audit_int(event, "deduplicated_count"),
        "context_diversified_count": _audit_int(event, "context_diversified_count"),
        "context_group_expanded_count": _audit_int(event, "context_group_expanded_count"),
        "context_expanded_count": _audit_int(event, "context_expanded_count"),
        "context_adaptive_expanded_count": _audit_int(
            event,
            "context_adaptive_expanded_count",
        ),
        "context_dependency_promoted_count": _audit_int(
            event,
            "context_dependency_promoted_count",
        ),
        "context_compressed_count": _audit_int(event, "context_compressed_count"),
        "context_compression_saved_chars": _audit_int(
            event,
            "context_compression_saved_chars",
        ),
        "agent_memory_retrieved_count": _audit_int(
            event,
            "agent_memory_retrieved_count",
        ),
        "agent_memory_writeback_count": _audit_int(
            event,
            "agent_memory_writeback_count",
        ),
        "agent_memory_writeback_status": _audit_str(
            event,
            "agent_memory_writeback_status",
            "skipped",
        ),
        "evidence_count": _audit_int(event, "evidence_count"),
        "support_count": _audit_int(event, "support_count"),
        "structure_count": _audit_int(event, "structure_count"),
        "history_count": _audit_int(event, "history_count"),
        "resolver_rejected_count": _audit_int(event, "resolver_rejected_count"),
        "insufficient_context_count": _audit_int(event, "insufficient_context_count"),
        "citation_count": _audit_int(event, "citation_count"),
        "context_chars": _audit_int(event, "context_chars"),
        "context_window_chars": _audit_optional_int(event, "context_window_chars"),
        "document_ids": _audit_json(event.get("document_ids", [])),
        "knowledge_base_ids": _audit_json(event.get("knowledge_base_ids", [])),
        "config_fingerprint": _audit_optional_str(event, "config_fingerprint"),
        "elapsed_ms": _audit_float(event, "elapsed_ms"),
        "error_stage": _audit_optional_str(event, "error_stage"),
        "error_type": _audit_optional_str(event, "error_type"),
    }


def _ingestion_audit_binds(event: Mapping[str, object]) -> dict[str, object]:
    """RagIngestionAuditEvent JSON を Oracle bind 値へ変換する。"""
    return {
        "event_type": _audit_str(event, "event_type", "rag.ingestion"),
        "trace_id": _audit_str(event, "trace_id", ""),
        "request_id": _audit_optional_str(event, "request_id"),
        "tenant_id_hash": _audit_optional_str(event, "tenant_id_hash"),
        "user_id_hash": _audit_optional_str(event, "user_id_hash"),
        "document_id": _audit_str(event, "document_id", ""),
        "outcome": _audit_str(event, "outcome", "error"),
        "source_sha256": _audit_str(event, "source_sha256", ""),
        "source_bytes": _audit_int(event, "source_bytes"),
        "document_type": _audit_optional_str(event, "document_type"),
        "extraction_confidence": _audit_optional_float(event, "extraction_confidence"),
        "parser_backend": _audit_optional_str(event, "parser_backend"),
        "parser_profile": _audit_optional_str(event, "parser_profile"),
        "segment_count": _audit_int(event, "segment_count"),
        "fallback_count": _audit_int(event, "fallback_count"),
        "failed_segment_count": _audit_int(event, "failed_segment_count"),
        "chunk_count": _audit_int(event, "chunk_count"),
        "vector_count": _audit_int(event, "vector_count"),
        "elapsed_ms": _audit_float(event, "elapsed_ms"),
        "error_type": _audit_optional_str(event, "error_type"),
        "error_message": _audit_optional_str(event, "error_message"),
    }


def _citation_feedback_binds(
    feedback: Mapping[str, object],
    *,
    feedback_id: str,
) -> dict[str, object]:
    """FeedbackRequest を Oracle bind 値へ変換する。"""
    context = current_audit_request_context()
    return {
        "feedback_id": feedback_id,
        "trace_id": _audit_str(feedback, "trace_id", ""),
        "search_answer_profile_id": _audit_optional_str(feedback, "search_answer_profile_id"),
        "target_type": _audit_str(feedback, "target_type", "citation"),
        "source_surface": _audit_optional_str(feedback, "source_surface"),
        "document_id": _audit_optional_str(feedback, "document_id"),
        "chunk_id": _audit_optional_str(feedback, "chunk_id"),
        "tenant_id_hash": _audit_optional_str(feedback, "tenant_id_hash") or context.tenant_id_hash,
        "user_id_hash": _audit_optional_str(feedback, "user_id_hash") or context.user_id_hash,
        "rating": _audit_str(feedback, "rating", "not_helpful"),
        "reason": _audit_optional_str(feedback, "reason"),
        "comment_hash": _audit_optional_str(feedback, "comment_hash"),
        "comment_chars": _audit_int(feedback, "comment_chars"),
    }


def _feedback_detail_binds(
    details: Mapping[str, object],
    *,
    feedback_id: str,
    tenant_id_hash: str | None,
) -> dict[str, object]:
    """調査用の本文を audit metadata とは別 table へ保存する。"""
    question = _audit_optional_str(details, "question_text")
    answer = _audit_optional_str(details, "answer_text")
    comment = _audit_optional_str(details, "comment_text")
    corrected_answer = _audit_optional_str(details, "corrected_answer_text")
    search_text = (
        _text_search_index_text(
            "\n".join(value for value in (question, answer, comment, corrected_answer) if value)
        )
        or None
    )
    citations = details.get("citations", [])
    if not isinstance(citations, Sequence) or isinstance(citations, str | bytes | bytearray):
        citations = []
    return {
        "feedback_id": feedback_id,
        "tenant_id_hash": tenant_id_hash,
        "message_id": _audit_optional_str(details, "message_id"),
        "content_source": _audit_str(details, "content_source", "search_snapshot"),
        "question_text": question,
        "answer_text": answer,
        "comment_text": comment,
        "corrected_answer_text": corrected_answer,
        "citations_json": _json_dumps(citations),
        "search_text": search_text,
    }


def _feedback_latest_cte() -> str:
    """追記履歴から利用者・対象ごとの最新票だけを選ぶ CTE。"""
    return _render_sql(
        """
        WITH ranked_feedback AS (
            SELECT
                f.*,
                ROW_NUMBER() OVER (
                    PARTITION BY
                        NVL(f.tenant_id_hash, '__GLOBAL__'),
                        CASE
                            WHEN f.user_id_hash IS NULL THEN f.feedback_id
                            ELSE f.user_id_hash
                        END,
                        f.trace_id,
                        f.target_type,
                        NVL(f.document_id, '-'),
                        NVL(f.chunk_id, '-')
                    ORDER BY f.created_at DESC, f.feedback_id DESC
                ) AS feedback_rank
            FROM rag_citation_feedback f
            WHERE {tenant_sql}
        ),
        latest_feedback AS (
            SELECT * FROM ranked_feedback WHERE feedback_rank = 1
        )
        """,
        tenant_sql=_oracle_tenant_predicate(alias="f"),
    )


def _feedback_dashboard_filters(
    *,
    search_answer_profile_id: str | None,
    target_type: str | None,
    rating: str | None,
    reason: str | None,
    period_days: int | None,
    search_query: str | None,
    previous_period: bool = False,
) -> tuple[str, dict[str, object]]:
    """列名を固定した feedback 一覧 filter を組み立てる。

    利用できる検索・回答プロファイルが制限されているときは、その検索
    ・回答プロファイルの feedback だけにする（#214）。

    SYSTEM_ADMIN 以外は、自分が送った feedback だけにする（#408）。一覧・件数・集計（今期と
    前期）はすべてこの条件を使うので、集計の数字も同じ範囲になる。
    """
    owner_predicates, owner_binds = _feedback_owner_scope("f.user_id_hash")
    clauses = [
        "1 = 1",
        *_search_answer_profile_scope_predicates("f.search_answer_profile_id"),
        *owner_predicates,
    ]
    binds: dict[str, object] = _with_search_answer_profile_scope_bind(owner_binds)
    for column, value in (
        ("search_answer_profile_id", search_answer_profile_id),
        ("target_type", target_type),
        ("rating", rating),
        ("reason", reason),
    ):
        if value is not None:
            clauses.append(f"f.{column} = :{column}")
            binds[column] = value
    if period_days is not None:
        if previous_period:
            clauses.extend(
                [
                    "f.created_at >= SYSTIMESTAMP - NUMTODSINTERVAL(:previous_period_days, 'DAY')",
                    "f.created_at < SYSTIMESTAMP - NUMTODSINTERVAL(:period_days, 'DAY')",
                ]
            )
            binds["previous_period_days"] = period_days * 2
        else:
            clauses.append("f.created_at >= SYSTIMESTAMP - NUMTODSINTERVAL(:period_days, 'DAY')")
        binds["period_days"] = period_days
    if search_query:
        text_query = _oracle_text_query(search_query)
        if text_query is None:
            clauses.append("1 = 0")
        else:
            clauses.append("CONTAINS(fd.search_text, :feedback_query, 1) > 0")
            binds["feedback_query"] = text_query
    return " AND ".join(clauses), binds


def _evaluation_artifact_binds(
    artifact: Mapping[str, object],
    *,
    evaluation_run_id: str,
) -> dict[str, object]:
    """評価 artifact を query/context 原文なしの Oracle bind 値へ変換する。"""
    context = current_audit_request_context()
    request_summary = artifact.get("request_summary", {})
    result_summary = artifact.get("result_summary", {})
    result_json = _audit_json(result_summary if isinstance(result_summary, Mapping) else {})
    knowledge_base_ids = artifact.get("knowledge_base_ids", [])
    return {
        "evaluation_run_id": evaluation_run_id,
        "tenant_id_hash": _audit_optional_str(artifact, "tenant_id_hash") or context.tenant_id_hash,
        "knowledge_base_ids": _audit_json(
            knowledge_base_ids
            if isinstance(knowledge_base_ids, Sequence)
            and not isinstance(knowledge_base_ids, str | bytes | bytearray)
            else []
        ),
        "request_json": _audit_json(
            request_summary if isinstance(request_summary, Mapping) else {}
        ),
        "result_json": result_json,
        "result_sha256": hashlib.sha256(result_json.encode("utf-8")).hexdigest(),
        "passed": 1 if bool(artifact.get("passed")) else 0,
    }


def _graph_entity_binds(entity: GraphEntity) -> dict[str, object]:
    return {
        "entity_id": entity.entity_id,
        "tenant_id_hash": _current_tenant_id_hash(),
        "knowledge_base_id": entity.knowledge_base_id,
        "canonical_name": entity.canonical_name,
        "entity_type": entity.entity_type,
        "description": entity.description,
        "confidence": entity.confidence,
        "source_document_ids": _audit_json(entity.source_document_ids),
    }


def _graph_relationship_binds(relationship: GraphRelationship) -> dict[str, object]:
    return {
        "relationship_id": relationship.relationship_id,
        "tenant_id_hash": _current_tenant_id_hash(),
        "knowledge_base_id": relationship.knowledge_base_id,
        "source_entity_id": relationship.source_entity_id,
        "target_entity_id": relationship.target_entity_id,
        "relationship_type": relationship.relationship_type,
        "description": relationship.description,
        "confidence": relationship.confidence,
        "source_document_ids": _audit_json(relationship.source_document_ids),
    }


def _graph_entity_chunk_link_binds(link: GraphEntityChunkLink) -> dict[str, object]:
    return {
        "entity_id": link.entity_id,
        "chunk_id": link.chunk_id,
        "document_id": link.document_id,
        "tenant_id_hash": _current_tenant_id_hash(),
        "relevance_score": link.relevance_score,
    }


def _audit_json(value: object) -> str:
    """Oracle JSON 列へ入れる低機密 metadata を JSON 文字列化する(NaN は null。#2043)。"""
    return _json_dumps(value)


def _audit_str(event: Mapping[str, object], key: str, default: str) -> str:
    value = event.get(key)
    return value if isinstance(value, str) and value else default


def _audit_optional_str(event: Mapping[str, object], key: str) -> str | None:
    value = event.get(key)
    return value if isinstance(value, str) and value else None


def _audit_int(event: Mapping[str, object], key: str, default: int = 0) -> int:
    value = event.get(key)
    return int(value) if isinstance(value, int | float) else default


def _audit_optional_int(event: Mapping[str, object], key: str) -> int | None:
    value = event.get(key)
    return int(value) if isinstance(value, int | float) else None


def _audit_float(event: Mapping[str, object], key: str, default: float = 0.0) -> float:
    value = event.get(key)
    return float(value) if isinstance(value, int | float) else default


def _audit_optional_float(event: Mapping[str, object], key: str) -> float | None:
    value = event.get(key)
    return float(value) if isinstance(value, int | float) else None


def _stale_running_ingestion_job_predicate_sql(
    *,
    alias: str | None,
    exclude_own_lease: bool,
) -> str:
    """RUNNING job が stale(worker が止まった)かを判定する SQL 条件(#357)。

    - heartbeat のある job: heartbeat が DB の時計で ``:lease_ttl_seconds`` 秒より前
      (lease の TTL 切れ)。heartbeat も DB の時計で書き、worker の時計のずれに左右されない(#359)。
    - heartbeat の無い job(lease 導入前の行など): 開始が ``:stale_before`` より前。
    - ``exclude_own_lease``: ``:exclude_lease_owner`` の lease の job は stale にしない。
    """
    prefix = f"{alias}." if alias else ""
    predicate = (
        "("
        f"({prefix}heartbeat_at IS NULL"
        f" AND COALESCE({prefix}started_at, {prefix}queued_at) < :stale_before)"
        f" OR {prefix}heartbeat_at"
        " < SYSTIMESTAMP - NUMTODSINTERVAL(:lease_ttl_seconds, 'SECOND')"
        ")"
    )
    if exclude_own_lease:
        predicate += (
            f" AND ({prefix}lease_owner IS NULL OR {prefix}lease_owner <> :exclude_lease_owner)"
        )
    return predicate


def _fetch_ingestion_job_rows(
    connection: OracleConnectionProtocol,
    statement: str,
    binds: Mapping[str, object],
    *,
    default_max_attempts: int,
) -> list[dict[str, object]]:
    """max_attempts 列がない旧 ingestion job table では既定値列として読み替える。"""
    try:
        return _fetch_all(connection, statement, binds)
    except Exception as exc:
        if not _is_missing_ingestion_job_max_attempts_error(exc):
            raise
        return _fetch_all(
            connection,
            _legacy_ingestion_job_max_attempts_select_sql(statement),
            _with_default_max_attempts_bind(binds, default_max_attempts),
        )


# 取込 job の INSERT。1 件の投入と一括の投入(#2674)で共通。
INGESTION_JOB_INSERT_SQL = """
                INSERT INTO rag_ingestion_jobs (
                    job_id,
                    document_id,
                    recipe_id,
                    recipe_revision,
                    language_pack_release_id,
                    tenant_id_hash,
                    status,
                    phase,
                    parser_profile,
                    quality_warnings,
                    settings_overrides,
                    skip_reason,
                    error_message,
                    attempt_count,
                    max_attempts,
                    queued_at,
                    started_at,
                    finished_at
                ) VALUES (
                    :job_id,
                    :document_id,
                    :recipe_id,
                    :recipe_revision,
                    :language_pack_release_id,
                    :tenant_id_hash,
                    :status,
                    :phase,
                    :parser_profile,
                    :quality_warnings,
                    :settings_overrides,
                    :skip_reason,
                    :error_message,
                    :attempt_count,
                    :max_attempts,
                    :queued_at,
                    :started_at,
                    :finished_at
                )
                """


def _execute_ingestion_job_insert(
    connection: OracleConnectionProtocol,
    statement: str,
    binds: Mapping[str, object],
) -> None:
    """max_attempts 列がない旧 ingestion job table では該当列を省いて INSERT する。"""
    try:
        _execute(connection, statement, binds)
    except Exception as exc:
        if not _is_missing_ingestion_job_max_attempts_error(exc):
            raise
        _execute(
            connection,
            _legacy_ingestion_job_max_attempts_insert_sql(statement),
            binds,
        )


def _is_unique_constraint_violation(exc: Exception) -> bool:
    """一意制約違反（ORA-00001）かどうか。"""
    return "ORA-00001" in str(exc).upper()


@contextmanager
def _knowledge_base_name_conflict_guard() -> Iterator[None]:
    """ナレッジベース名の一意制約違反を 409 用の例外へ読み替える（同時作成の競合）。"""
    try:
        yield
    except Exception as exc:
        if _is_unique_constraint_violation(exc):
            raise KnowledgeBaseNameConflictError() from exc
        raise


def _is_missing_ingestion_job_max_attempts_error(exc: Exception) -> bool:
    message = str(exc).upper()
    return "ORA-00904" in message and "MAX_ATTEMPTS" in message


def _legacy_ingestion_job_max_attempts_select_sql(statement: str) -> str:
    return _replace_exact_sql_line(
        statement,
        "j.max_attempts,",
        ":default_max_attempts AS max_attempts,",
    )


def _legacy_ingestion_job_max_attempts_insert_sql(statement: str) -> str:
    return _remove_exact_sql_lines(statement, {"max_attempts,", ":max_attempts,"})


def _with_default_max_attempts_bind(
    binds: Mapping[str, object],
    default_max_attempts: int,
) -> dict[str, object]:
    return {**binds, "default_max_attempts": default_max_attempts}


def _replace_exact_sql_line(statement: str, old: str, new: str) -> str:
    lines = []
    for line in statement.splitlines():
        if line.strip() == old:
            indent = line[: len(line) - len(line.lstrip())]
            lines.append(f"{indent}{new}")
            continue
        lines.append(line)
    return "\n".join(lines)


def _remove_exact_sql_lines(statement: str, stripped_lines: set[str]) -> str:
    lines = [line for line in statement.splitlines() if line.strip() not in stripped_lines]
    return "\n".join(lines)


def _select_document(
    connection: OracleConnectionProtocol,
    document_id: str,
) -> StoredDocument | None:
    rows = _fetch_all(
        connection,
        _render_sql(
            """
        SELECT
            document_id,
            file_name,
            status,
            tenant_id_hash,
            category_name,
            object_storage_path,
            preprocess_artifact,
            content_type,
            file_size_bytes,
            content_sha256,
            duplicate_of_document_id,
            extraction,
            classification,
            error_message,
            uploaded_at,
            indexed_at,
            superseded_by_document_id,
            superseded_at
        FROM rag_documents
        WHERE document_id = :document_id
          AND {access_predicate}
        """,
            access_predicate=_oracle_access_predicate_sql(),
        ),
        _with_tenant_bind({"document_id": document_id}),
    )
    return None if not rows else _stored_document_from_row(rows[0])


def _select_document_detail_with_refs(
    connection: OracleConnectionProtocol,
    document_id: str,
) -> DocumentDetail:
    """transaction の中で文書の詳細を、所属 KB と新しい版の文書名付きで読む。"""
    document = _select_document(connection, document_id)
    if document is None:
        raise KeyError(f"document_id={document_id} は存在しません。")
    detail = _to_document_detail(document).model_copy(
        update={"knowledge_bases": _select_document_knowledge_base_refs(connection, document_id)}
    )
    superseded_ids = _superseded_by_document_ids([detail])
    if not superseded_ids:
        return detail
    statement, binds = _document_file_names_query(superseded_ids)
    rows = _fetch_all(connection, statement, binds)
    file_names = {str(row["document_id"]): str(row["file_name"]) for row in rows}
    return _with_superseded_file_names([detail], file_names)[0]


class DocumentSupersessionError(ValueError):
    """文書の版の置き換えを保存できない(自分自身・循環・見えない新しい版。#1248)。"""


# 置き換えの連鎖をたどる上限(循環の検出。通常の版の連鎖はこれより十分短い)。
_SUPERSESSION_CHAIN_LIMIT = 100


def _validate_superseded_by(
    connection: OracleConnectionProtocol,
    document_id: str,
    superseded_by_document_id: str,
) -> None:
    """新しい版の文書を検証する(自分自身・見えない文書・循環を拒む。#1248)。"""
    if superseded_by_document_id == document_id:
        raise DocumentSupersessionError("文書自身を新しい版にはできません。")
    if _select_document_state(connection, superseded_by_document_id) is None:
        raise DocumentSupersessionError("新しい版の文書が見つかりません。")
    # 新しい版の側の置き換えをたどり、元の文書に戻ったら循環。利用者から見えない文書を
    # 経由する循環も拒むため、たどるときは tenant だけで絞る。
    current: str | None = superseded_by_document_id
    visited: set[str] = set()
    while current is not None and current not in visited:
        if len(visited) >= _SUPERSESSION_CHAIN_LIMIT:
            raise DocumentSupersessionError("版の置き換えの連鎖が長すぎます。")
        visited.add(current)
        row = _fetch_one(
            connection,
            _render_sql(
                """
            SELECT superseded_by_document_id
            FROM rag_documents
            WHERE document_id = :chain_document_id
              AND {tenant_sql}
            """,
                tenant_sql=_oracle_tenant_predicate(),
            ),
            _with_tenant_bind({"chain_document_id": current}),
        )
        current = _optional_str(row.get("superseded_by_document_id")) if row else None
        if current == document_id:
            raise DocumentSupersessionError(
                "新しい版の文書が、この文書に置き換えられています(版の置き換えが循環します)。"
            )


def _superseded_by_document_ids(documents: Sequence[DocumentSummary]) -> list[str]:
    return _unique_optional_sequence(
        [
            document.superseded_by_document_id
            for document in documents
            if document.superseded_by_document_id
        ]
    )


def _document_file_names_query(document_ids: Sequence[str]) -> tuple[str, dict[str, object]]:
    """利用者が見られる文書の文書名を読む SQL(新しい版の文書名。#1248)。"""
    in_sql, binds = _oracle_in_predicate("s.document_id", "name_document", document_ids)
    return (
        _render_sql(
            """
        SELECT s.document_id, s.file_name
        FROM rag_documents s
        WHERE {in_sql}
          AND {access_sql}
        """,
            in_sql=in_sql,
            access_sql=_oracle_access_predicate_sql(alias="s"),
        ),
        _with_tenant_bind(binds, alias="s"),
    )


def _with_superseded_file_names[D: DocumentSummary](
    documents: Sequence[D], file_names: Mapping[str, str]
) -> list[D]:
    return [
        document.model_copy(
            update={"superseded_by_file_name": file_names.get(document.superseded_by_document_id)}
        )
        if document.superseded_by_document_id
        else document
        for document in documents
    ]


# rag_documents の JSON 列(extraction / preprocess_artifact / classification / processing_config)を
# 除いた列。存在確認・状態確認・chunk の保存に使う(#341)。大きな JSON 列(行の外の LOB)を
# 必要の無い経路で読まない(ADB の result cache の不具合 #333 の引き金になった)。
_DOCUMENT_STATE_COLUMNS_SQL = """
            document_id,
            file_name,
            status,
            tenant_id_hash,
            category_name,
            object_storage_path,
            content_type,
            file_size_bytes,
            content_sha256,
            duplicate_of_document_id,
            error_message,
            uploaded_at,
            indexed_at,
            superseded_by_document_id,
            superseded_at
"""

# 一覧の「状態」(#2012)。文書の詳細が既定で出す状態(`slot_no` の最も小さいレシピの状態)と
# 同じ正本から出す。取込の job はレシピの job で、工程の状態はレシピの行だけに書き、
# rag_documents.status は変えない(#504)ため、文書の列のままでは投入前の状態が残る。
# レシピの行が無い文書(migration 前)は文書の状態。一覧の列・状態の絞り込み・件数で同じ式を
# 使う(FROM rag_documents を別名なしで読む SQL 用)。
_DOCUMENT_LIST_STATUS_SQL = """COALESCE(
                (
                    SELECT MIN(r.status) KEEP (DENSE_RANK FIRST ORDER BY r.slot_no)
                    FROM rag_document_recipes r
                    WHERE r.document_id = rag_documents.document_id
                ),
                rag_documents.status
            )"""

# 一覧の行が「〈工程〉の順番待ち」と出る段(#2026・#2677)。一覧の API の `_attach_queue_waits` と
# 同じ選び方で、レシピ1(`slot_no` の最も小さいレシピ。レシピの行が無い文書は job のレシピも無いもの)
# の QUEUED の job のうち、投入の古い順(`queued_at`・`job_id`)の最初の job の段。順番待ちで
# なければ NULL。状態の絞り込み・件数で使う(FROM rag_documents を別名なしで読む SQL 用)。
# DECODE は NULL どうしを一致とみなす(レシピの行が無い文書の job)。
_DOCUMENT_LIST_QUEUED_PHASE_SQL = """(
                SELECT MIN(qj.phase) KEEP (DENSE_RANK FIRST ORDER BY qj.queued_at, qj.job_id)
                FROM rag_ingestion_jobs qj
                LEFT JOIN rag_document_recipes qr
                  ON qr.recipe_id = qj.recipe_id
                WHERE qj.document_id = rag_documents.document_id
                  AND qj.status = 'QUEUED'
                  AND DECODE(
                      qr.slot_no,
                      (
                          SELECT MIN(fr.slot_no)
                          FROM rag_document_recipes fr
                          WHERE fr.document_id = rag_documents.document_id
                      ),
                      1,
                      0
                  ) = 1
            )"""

# 一覧の列。状態を `_DOCUMENT_LIST_STATUS_SQL` に置き換え、待機中・実行中の取込 job があるかを
# 足す(投入直後の job は worker が拾うまでレシピの状態が変わらないため、一覧の定期の取り直しの
# 判定に使う)。
_DOCUMENT_LIST_COLUMNS_SQL = (
    _DOCUMENT_STATE_COLUMNS_SQL.replace(
        "\n            status,\n", f"\n            {_DOCUMENT_LIST_STATUS_SQL} AS status,\n", 1
    ).rstrip()
    + """,
            CASE WHEN EXISTS (
                SELECT 1
                FROM rag_ingestion_jobs j
                WHERE j.document_id = rag_documents.document_id
                  AND j.status IN ('QUEUED', 'RUNNING')
            ) THEN 1 ELSE 0 END AS ingestion_active
"""
)


def _select_document_state(
    connection: OracleConnectionProtocol,
    document_id: str,
) -> StoredDocument | None:
    """文書の状態だけを読む(JSON 列は読まない。``extraction`` 等は空のまま返す)。"""
    rows = _fetch_all(
        connection,
        _render_sql(
            """
        SELECT
            {columns}
        FROM rag_documents
        WHERE document_id = :document_id
          AND {access_predicate}
        """,
            columns=_DOCUMENT_STATE_COLUMNS_SQL.strip(),
            access_predicate=_oracle_access_predicate_sql(),
        ),
        _with_tenant_bind({"document_id": document_id}),
    )
    return None if not rows else _stored_document_from_row(rows[0])


def _select_document_for_update(
    connection: OracleConnectionProtocol,
    document_id: str,
) -> dict[str, object] | None:
    return _fetch_one(
        connection,
        _render_sql(
            """
        SELECT document_id
        FROM rag_documents
        WHERE document_id = :document_id
          AND {access_predicate}
        FOR UPDATE
        """,
            access_predicate=_oracle_access_predicate_sql(),
        ),
        _with_tenant_bind({"document_id": document_id}),
    )


def _merge_document_extraction_artifact(
    connection: OracleConnectionProtocol,
    binds: Mapping[str, object],
    *,
    update_extraction: bool,
) -> None:
    """抽出 artifact を document_id + extraction_recipe_id で作るか更新する。"""
    extraction_update = (
        "            t.extraction_json = :extraction_json,\n" if update_extraction else ""
    )
    _execute(
        connection,
        f"""
        MERGE INTO rag_document_extractions t
        USING (
            SELECT :document_id AS document_id,
                   :extraction_recipe_id AS extraction_recipe_id
            FROM dual
        ) s
        ON (t.document_id = s.document_id
            AND t.extraction_recipe_id = s.extraction_recipe_id)
        WHEN MATCHED THEN UPDATE SET
            t.source_sha256 = :source_sha256,
            t.recipe_subset = :recipe_subset,
{extraction_update}            t.status = :status,
            t.reason = :reason,
            t.metrics_json = :metrics_json,
            t.updated_at = SYSTIMESTAMP
        WHEN NOT MATCHED THEN INSERT
            (document_id, extraction_recipe_id, source_sha256, tenant_id_hash,
             recipe_subset, extraction_json, status, reason, metrics_json)
            VALUES
            (:document_id, :extraction_recipe_id, :source_sha256, :tenant_id_hash,
             :recipe_subset, :extraction_json, :status, :reason, :metrics_json)
        """,
        dict(binds),
        input_sizes=_json_input_sizes(
            "recipe_subset",
            "extraction_json",
            "metrics_json",
        ),
    )


def _select_document_recipe_for_update(
    connection: OracleConnectionProtocol,
    document_id: str,
    recipe_id: str,
) -> dict[str, object] | None:
    return _fetch_one(
        connection,
        _render_sql(
            """
        SELECT r.*
        FROM rag_document_recipes r
        JOIN rag_documents d ON d.document_id = r.document_id
        WHERE r.document_id = :document_id
          AND r.recipe_id = :recipe_id
          AND {access_predicate}
        FOR UPDATE OF r.recipe_id
        """,
            access_predicate=_oracle_access_predicate_sql(alias="d"),
        ),
        _with_tenant_bind({"document_id": document_id, "recipe_id": recipe_id}),
    )


def default_document_recipe_id(document_id: str) -> str:
    """文書のレシピ1(slot 1)の決定論的な ID。migration の STANDARD_HASH と同じ値。"""
    return hashlib.sha256(f"{document_id}:recipe:1".encode()).hexdigest()


def _default_document_recipe_values(row: Mapping[str, object]) -> dict[str, object]:
    """文書の legacy 状態(rag_documents の行)からレシピ1の値を組み立てる。"""
    document_id = str(row["document_id"])
    status = _file_status(row.get("status") or FileStatus.UPLOADED.value)
    return {
        "recipe_id": default_document_recipe_id(document_id),
        "document_id": document_id,
        "slot_no": 1,
        "tenant_id_hash": row.get("tenant_id_hash"),
        "processing_config": row.get("processing_config"),
        "status": status.value,
        "failed_phase": None,
        "preprocess_artifact": row.get("preprocess_artifact"),
        "active_extraction_recipe_id": None,
        "config_revision": 1,
        "materialized_revision": 1 if status == FileStatus.INDEXED else None,
        "error_message": row.get("error_message"),
        "started_at": None,
        "finished_at": row.get("indexed_at"),
        "created_at": row.get("uploaded_at"),
        "updated_at": row.get("uploaded_at"),
    }


# レシピ1が無ければ作る(あれば何もしない)。文書の登録・ensure・一括の投入(#2674)で共通。
DEFAULT_DOCUMENT_RECIPE_MERGE_SQL = """
        MERGE INTO rag_document_recipes r
        USING (SELECT :document_id AS document_id, 1 AS slot_no FROM dual) s
        ON (r.document_id = s.document_id AND r.slot_no = s.slot_no)
        WHEN NOT MATCHED THEN INSERT (
            recipe_id, document_id, slot_no, tenant_id_hash, processing_config,
            status, preprocess_artifact, config_revision, materialized_revision,
            error_message, created_at, updated_at, finished_at
        ) VALUES (
            :recipe_id, :document_id, 1, :tenant_id_hash, :processing_config,
            :status, :preprocess_artifact, 1, :materialized_revision,
            :error_message, :created_at, SYSTIMESTAMP, :finished_at
        )
        """
DEFAULT_DOCUMENT_RECIPE_MERGE_JSON_BINDS = ("processing_config", "preprocess_artifact")


def default_document_recipe_merge_binds(values: Mapping[str, object]) -> dict[str, object]:
    """``DEFAULT_DOCUMENT_RECIPE_MERGE_SQL`` の bind(``_default_document_recipe_values`` の値)。"""
    return {
        "recipe_id": values["recipe_id"],
        "document_id": values["document_id"],
        "tenant_id_hash": values.get("tenant_id_hash"),
        "processing_config": _json_bind(_json_loads(values.get("processing_config"))),
        "status": values["status"],
        "preprocess_artifact": _json_bind(_json_loads(values.get("preprocess_artifact")) or None),
        "materialized_revision": values.get("materialized_revision"),
        "error_message": values.get("error_message"),
        "created_at": values.get("created_at"),
        "finished_at": values.get("finished_at"),
    }


def _merge_default_document_recipe(
    connection: OracleConnectionProtocol,
    values: Mapping[str, object],
) -> None:
    """レシピ1が無ければ作る(あれば何もしない)。文書の登録と ensure の共通の書き込み。"""
    _execute(
        connection,
        DEFAULT_DOCUMENT_RECIPE_MERGE_SQL,
        default_document_recipe_merge_binds(values),
        input_sizes=_json_input_sizes(*DEFAULT_DOCUMENT_RECIPE_MERGE_JSON_BINDS),
    )


def _select_default_document_recipe(
    connection: OracleConnectionProtocol,
    document_id: str,
) -> dict[str, object] | None:
    return _fetch_one(
        connection,
        _render_sql(
            """
        SELECT r.*
        FROM rag_document_recipes r
        JOIN rag_documents d ON d.document_id = r.document_id
        WHERE r.document_id = :document_id
          AND r.slot_no = 1
          AND {access_predicate}
        """,
            access_predicate=_oracle_access_predicate_sql(alias="d"),
        ),
        _with_tenant_bind({"document_id": document_id}),
    )


def _ensure_default_document_recipe_row(
    connection: OracleConnectionProtocol,
    document_id: str,
) -> dict[str, object]:
    """書き込みの transaction の中で、レシピ1が無ければ文書の状態から作って返す。

    レシピ1があれば MERGE もせず、文書の JSON 列も読まない(ジョブの投入のたびに書かない)。
    """
    existing = _select_default_document_recipe(connection, document_id)
    if existing is not None:
        return _document_recipe_row(existing)
    document = _fetch_one(
        connection,
        _render_sql(
            """
        SELECT
            d.document_id, d.tenant_id_hash, d.status, d.processing_config,
            d.preprocess_artifact, d.error_message, d.uploaded_at, d.indexed_at
        FROM rag_documents d
        WHERE d.document_id = :document_id
          AND {access_predicate}
        """,
            access_predicate=_oracle_access_predicate_sql(alias="d"),
        ),
        _with_tenant_bind({"document_id": document_id}),
    )
    if document is None:
        raise KeyError(f"document_id={document_id} は存在しません。")
    _merge_default_document_recipe(connection, _default_document_recipe_values(document))
    created = _select_default_document_recipe(connection, document_id)
    if created is None:
        raise RuntimeError("レシピ1の作成に失敗しました。")
    return _document_recipe_row(created)


def _ensure_recipe_row_if_default(
    connection: OracleConnectionProtocol,
    document_id: str,
    recipe_id: str,
) -> None:
    """GET が仮の行で返したレシピ1を、最初の書き込みの transaction で実体化する。"""
    if recipe_id == default_document_recipe_id(document_id):
        _ensure_default_document_recipe_row(connection, document_id)


def _select_knowledge_base(
    connection: OracleConnectionProtocol,
    knowledge_base_id: str,
) -> StoredKnowledgeBase | None:
    rows = _fetch_all(
        connection,
        _render_sql(
            """
        SELECT
            knowledge_base_id,
            tenant_id_hash,
            name,
            description,
            status,
            default_search_mode,
            retrieval_config,
            created_at,
            updated_at,
            archived_at,
            0 AS document_count,
            0 AS indexed_document_count,
            0 AS error_document_count,
            0 AS searchable_chunk_count
        FROM rag_knowledge_bases
        WHERE knowledge_base_id = :knowledge_base_id
          AND {knowledge_base_access_sql}
        """,
            knowledge_base_access_sql=_oracle_knowledge_base_access_predicate_sql(),
        ),
        _with_tenant_bind({"knowledge_base_id": knowledge_base_id}),
    )
    return None if not rows else _stored_knowledge_base_from_row(rows[0])


def _select_search_answer_profile(
    connection: OracleConnectionProtocol,
    search_answer_profile_id: str,
) -> StoredSearchAnswerProfile | None:
    rows = _fetch_all(
        connection,
        _render_sql(
            """
        SELECT
            search_answer_profile_id,
            tenant_id_hash,
            name,
            description,
            status,
            profile_config,
            created_at,
            updated_at,
            archived_at
        FROM rag_search_answer_profiles
        WHERE search_answer_profile_id = :search_answer_profile_id
          AND {access_sql}
        """,
            access_sql=" AND ".join(_oracle_search_answer_profile_access_predicates()),
        ),
        _with_search_answer_profile_scope_bind(
            _with_tenant_bind({"search_answer_profile_id": search_answer_profile_id})
        ),
    )
    return None if not rows else _stored_search_answer_profile_from_row(rows[0])


def _select_search_answer_profile_by_name(
    connection: OracleConnectionProtocol,
    name: str,
) -> StoredSearchAnswerProfile | None:
    rows = _fetch_all(
        connection,
        _render_sql(
            """
        SELECT
            search_answer_profile_id,
            tenant_id_hash,
            name,
            description,
            status,
            profile_config,
            created_at,
            updated_at,
            archived_at
        FROM rag_search_answer_profiles
        WHERE LOWER(name) = :search_answer_profile_name
          AND {tenant_sql}
        """,
            tenant_sql=_oracle_tenant_predicate(),
        ),
        _with_tenant_bind({"search_answer_profile_name": name.casefold()}),
    )
    return None if not rows else _stored_search_answer_profile_from_row(rows[0])


def _select_conversation(
    connection: OracleConnectionProtocol,
    conversation_id: str,
) -> StoredConversation | None:
    rows = _fetch_all(
        connection,
        _render_sql(
            """
        SELECT
            conversation_id,
            search_answer_profile_id,
            tenant_id_hash,
            user_id_hash,
            title,
            status,
            message_count,
            created_at,
            updated_at
        FROM rag_conversations
        WHERE conversation_id = :conversation_id
          AND {access_sql}
        """,
            access_sql=_oracle_conversation_access_predicate_sql(),
        ),
        _with_conversation_access_bind({"conversation_id": conversation_id}),
    )
    return None if not rows else _stored_conversation_from_row(rows[0])


def _select_knowledge_base_by_name(
    connection: OracleConnectionProtocol,
    name: str,
) -> StoredKnowledgeBase | None:
    rows = _fetch_all(
        connection,
        _render_sql(
            """
        SELECT
            knowledge_base_id,
            tenant_id_hash,
            name,
            description,
            status,
            default_search_mode,
            retrieval_config,
            created_at,
            updated_at,
            archived_at,
            0 AS document_count,
            0 AS indexed_document_count,
            0 AS error_document_count,
            0 AS searchable_chunk_count
        FROM rag_knowledge_bases
        WHERE LOWER(name) = :knowledge_base_name
          AND {tenant_sql}
        """,
            # DEFAULT の存在確認に使う。利用者の KB 範囲で絞ると、範囲外の DEFAULT を
            # 「ない」と判断して重複 INSERT するため、tenant だけで探す（#214）。
            tenant_sql=_oracle_tenant_predicate(),
        ),
        _with_tenant_bind({"knowledge_base_name": name.casefold()}),
    )
    return None if not rows else _stored_knowledge_base_from_row(rows[0])


def _insert_knowledge_base(
    connection: OracleConnectionProtocol,
    knowledge_base: StoredKnowledgeBase,
) -> None:
    _execute(
        connection,
        """
        INSERT INTO rag_knowledge_bases (
            knowledge_base_id,
            tenant_id_hash,
            name,
            description,
            status,
            default_search_mode,
            retrieval_config,
            created_at,
            updated_at,
            archived_at
        ) VALUES (
            :knowledge_base_id,
            :tenant_id_hash,
            :name,
            :description,
            :status,
            :default_search_mode,
            :retrieval_config,
            :created_at,
            :updated_at,
            :archived_at
        )
        """,
        _knowledge_base_binds(knowledge_base),
    )


def _insert_search_answer_profile(
    connection: OracleConnectionProtocol,
    view: StoredSearchAnswerProfile,
) -> None:
    _execute(
        connection,
        """
        INSERT INTO rag_search_answer_profiles (
            search_answer_profile_id,
            tenant_id_hash,
            name,
            description,
            status,
            profile_config,
            created_at,
            updated_at,
            archived_at
        ) VALUES (
            :search_answer_profile_id,
            :tenant_id_hash,
            :name,
            :description,
            :status,
            :profile_config,
            :created_at,
            :updated_at,
            :archived_at
        )
        """,
        _search_answer_profile_binds(view),
    )


def _ensure_default_knowledge_base(
    connection: OracleConnectionProtocol,
    name: str,
) -> StoredKnowledgeBase:
    existing = _select_knowledge_base_by_name(connection, name)
    if existing is not None:
        if existing.status != KnowledgeBaseStatus.ACTIVE:
            raise ValueError("DEFAULT ナレッジベースがアーカイブ済みです。")
        if (existing.description or "").strip():
            return existing
        # 説明が必須になる前（#521）に作った DEFAULT は、既定の説明を補う（改名できないため）。
        described = updated_copy_knowledge_base(
            existing,
            description=DEFAULT_KNOWLEDGE_BASE_DESCRIPTION,
            updated_at=datetime.now(UTC),
        )
        _execute(
            connection,
            _render_sql(
                """
            UPDATE rag_knowledge_bases
            SET
                description = :description,
                updated_at = :updated_at
            WHERE knowledge_base_id = :knowledge_base_id
              AND {tenant_sql}
            """,
                tenant_sql=_oracle_tenant_predicate(),
            ),
            _knowledge_base_binds(described),
        )
        return described
    now = datetime.now(UTC)
    knowledge_base = StoredKnowledgeBase(
        id=uuid4().hex,
        tenant_id_hash=_current_tenant_id_hash(),
        name=name,
        description=DEFAULT_KNOWLEDGE_BASE_DESCRIPTION,
        status=KnowledgeBaseStatus.ACTIVE,
        default_search_mode=SearchMode.HYBRID,
        retrieval_config={},
        created_at=now,
        updated_at=now,
    )
    _insert_knowledge_base(connection, knowledge_base)
    return knowledge_base


def _ensure_default_search_answer_profile(
    connection: OracleConnectionProtocol,
) -> SearchAnswerProfileDetail:
    knowledge_base = _ensure_default_knowledge_base(connection, DEFAULT_KNOWLEDGE_BASE_NAME)
    existing = _select_search_answer_profile_by_name(connection, DEFAULT_SEARCH_ANSWER_PROFILE_NAME)
    now = datetime.now(UTC)
    if existing is None:
        view = StoredSearchAnswerProfile(
            id=uuid4().hex,
            tenant_id_hash=_current_tenant_id_hash(),
            name=DEFAULT_SEARCH_ANSWER_PROFILE_NAME,
            description=DEFAULT_SEARCH_ANSWER_PROFILE_DESCRIPTION,
            status=SearchAnswerProfileStatus.ACTIVE,
            profile_config=dump_search_answer_profile_config(
                SearchAnswerProfileConfig(knowledge_base_ids=[knowledge_base.id])
            ),
            created_at=now,
            updated_at=now,
        )
        _insert_search_answer_profile(connection, view)
        return _to_search_answer_profile_detail(view)

    config = parse_search_answer_profile_config(existing.profile_config)
    normalized_config = config.model_copy(update={"knowledge_base_ids": [knowledge_base.id]})
    # 説明が必須になる前（#521）に作った DEFAULT は、既定の説明を補う（改名できないため）。
    described = bool((existing.description or "").strip())
    if (
        existing.status == SearchAnswerProfileStatus.ACTIVE
        and existing.archived_at is None
        and config == normalized_config
        and described
    ):
        return _to_search_answer_profile_detail(existing)

    normalized = updated_copy_search_answer_profile(
        existing,
        description=existing.description
        if described
        else DEFAULT_SEARCH_ANSWER_PROFILE_DESCRIPTION,
        status=SearchAnswerProfileStatus.ACTIVE,
        profile_config=dump_search_answer_profile_config(normalized_config),
        updated_at=now,
        archived_at=None,
    )
    _execute(
        connection,
        _render_sql(
            """
        UPDATE rag_search_answer_profiles
        SET
            description = :description,
            status = :status,
            profile_config = :profile_config,
            updated_at = :updated_at,
            archived_at = :archived_at
        WHERE search_answer_profile_id = :search_answer_profile_id
          AND {tenant_sql}
        """,
            tenant_sql=_oracle_tenant_predicate(),
        ),
        _search_answer_profile_binds(normalized),
    )
    return _to_search_answer_profile_detail(normalized)


def _require_active_knowledge_base(
    connection: OracleConnectionProtocol,
    knowledge_base_id: str,
) -> StoredKnowledgeBase:
    knowledge_base = _select_knowledge_base(connection, knowledge_base_id)
    if knowledge_base is None:
        raise KeyError(f"knowledge_base_id={knowledge_base_id} は存在しません。")
    if knowledge_base.status != KnowledgeBaseStatus.ACTIVE:
        raise ValueError("アーカイブ済みナレッジベースは変更できません。")
    return knowledge_base


def _insert_document_knowledge_base_rows(
    connection: OracleConnectionProtocol,
    *,
    document_id: str,
    knowledge_base_ids: Sequence[str],
) -> None:
    unique_knowledge_base_ids = _unique_optional_sequence(knowledge_base_ids)
    if not unique_knowledge_base_ids:
        return
    _executemany(
        connection,
        """
        INSERT INTO rag_document_knowledge_bases (
            knowledge_base_id,
            document_id,
            tenant_id_hash,
            assigned_at,
            assigned_by_user_id_hash
        ) VALUES (
            :knowledge_base_id,
            :document_id,
            :tenant_id_hash,
            :assigned_at,
            :assigned_by_user_id_hash
        )
        """,
        [
            _document_knowledge_base_binds(
                knowledge_base_id=knowledge_base_id,
                document_id=document_id,
            )
            for knowledge_base_id in unique_knowledge_base_ids
        ],
    )


def _select_document_membership_knowledge_base_ids(
    connection: OracleConnectionProtocol,
    document_id: str,
) -> list[str]:
    """文書の所属 KB ID を tenant 全体で返し、所属の行をロックする。

    最後の所属を外すかの判定に使う。利用者の KB 範囲で絞ると範囲外の所属を見落として
    DEFAULT へ移してしまうため、tenant だけで絞る。同時に別の KB から外す操作と競合しても
    未所属にならないよう、行をロックする。
    """
    rows = _fetch_all(
        connection,
        _render_sql(
            """
        SELECT knowledge_base_id
        FROM rag_document_knowledge_bases
        WHERE document_id = :document_id
          AND {tenant_sql}
        FOR UPDATE
        """,
            tenant_sql=_oracle_tenant_predicate(),
        ),
        _with_tenant_bind({"document_id": document_id}),
    )
    return sorted(str(row["knowledge_base_id"]) for row in rows)


def _select_document_knowledge_base_refs(
    connection: OracleConnectionProtocol,
    document_id: str,
) -> list[KnowledgeBaseRef]:
    rows = _fetch_all(
        connection,
        _render_sql(
            """
        SELECT
            kb.knowledge_base_id,
            kb.name
        FROM rag_document_knowledge_bases dkb
        JOIN rag_knowledge_bases kb
          ON kb.knowledge_base_id = dkb.knowledge_base_id
        WHERE dkb.document_id = :document_id
          AND {knowledge_base_access_sql}
        ORDER BY kb.name ASC, kb.knowledge_base_id ASC
        """,
            knowledge_base_access_sql=_oracle_knowledge_base_access_predicate_sql(alias="kb"),
        ),
        _with_tenant_bind({"document_id": document_id}),
    )
    return [
        KnowledgeBaseRef(id=str(row["knowledge_base_id"]), name=str(row["name"])) for row in rows
    ]


def _row_to_dict(row: object, description: Sequence[Sequence[Any]] | None) -> dict[str, object]:
    if isinstance(row, Mapping):
        return {str(key).lower(): _read_db_value(value) for key, value in row.items()}
    if not isinstance(row, Sequence) or isinstance(row, str | bytes | bytearray):
        raise ValueError("Oracle row の形式が不正です。")
    columns = _description_columns(description)
    return {column: _read_db_value(value) for column, value in zip(columns, row, strict=False)}


def _description_columns(description: Sequence[Sequence[Any]] | None) -> list[str]:
    if description is None:
        return []
    columns: list[str] = []
    for item in description:
        if not item:
            continue
        columns.append(str(item[0]).lower())
    return columns


def _read_db_value(value: object) -> object:
    read = getattr(value, "read", None)
    if callable(read):
        stats = current_db_call_stats()
        if stats is not None:
            stats.record_lob_read()
        return read()
    if isinstance(value, dict | list):
        # Oracle の JSON 列は数値を Decimal で返す。json.dumps に渡せるよう int / float に
        # 戻す（#695）。
        return _json_numbers(value)
    return value


def _json_numbers(value: object) -> object:
    if isinstance(value, dict):
        return {key: _json_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_numbers(item) for item in value]
    if isinstance(value, Decimal):
        return _json_default(value)
    return value


def _normalize_sql(statement: str) -> str:
    return re.sub(r"\s+", " ", statement).strip()


_BIND_NAME_RE = re.compile(r":([a-zA-Z_]\w*)")


def _binds_for_sql(statement: str, binds: Mapping[str, object]) -> dict[str, object]:
    """SQL text に現れる placeholder のバインドだけを残す。

    アクセス scope のバインド(tenant / document / category / knowledge base)は
    `_with_tenant_bind` が予防的に superset で付与するため、特定クエリで使われない
    placeholder が混ざる。oracledb thin モードは未使用の bind を渡すと DPY-4008 を出す
    ので、実行直前に SQL に現れる名前だけへ絞り込む。
    """
    referenced = set(_BIND_NAME_RE.findall(statement))
    return {name: value for name, value in binds.items() if name in referenced}


INGESTION_DISPATCH_PRIORITY_WINDOW_BIND = "dispatch_priority_window_seconds"


def _sql_phase_list(phases: Collection[IngestionJobPhase]) -> str:
    return ", ".join(f"'{phase.value}'" for phase in sorted(phases, key=lambda item: item.value))


def ingestion_dispatch_order_sql(
    alias: str = "j",
    *,
    dispatch_key: str | None = None,
    phase_rank: str | None = None,
) -> str:
    """取込の QUEUED job を取る順番(``ORDER BY`` / ``OVER (ORDER BY ...)`` の中身。#2027)。

    worker の取る順番と、順番待ちの表示の「前に N 件」(#2026)は、この 1 つの式で数える。
    bind は ``ingestion_dispatch_order_binds`` が返す。``alias`` は ``rag_ingestion_jobs`` の別名。
    ``dispatch_key`` / ``phase_rank`` には、内側の問い合わせで ``ingestion_dispatch_key_sql`` /
    ``ingestion_phase_rank_sql`` を列にしたときの列名を渡せる(window の中で subquery を避ける)。

    1. 文書(その取込の最初の段の投入)の古い順。続きの段(Chunk 作成・索引)の job は、同じ文書・
       同じレシピの最初の段(ファイル準備・抽出)の job の投入の時刻まで繰り上げる。始めた文書を
       新しい文書の最初の段より先に進め(深さ優先 / パイプライン)、最初の文書が早く索引まで届く。
       投入の順(FIFO)では、続きの段がキューの末尾に付き、全件が 1 段ずつそろって進んでいた。
    2. 繰り上げは最大 ``RAG_INGESTION_QUEUE_PIPELINE_PRIORITY_WINDOW_SECONDS`` 秒(aging)。
       それより前から待っている job は追い越さず、新しい文書の最初の段が飢餓にならない。
       昔の抽出の後に手で投入した Chunk 作成が、待っている job を大きく追い越すこともない。
    3. 同じ時刻なら後の段を先に、最後は job の投入の古い順と ``job_id``。
    """
    key = dispatch_key or ingestion_dispatch_key_sql(alias)
    rank = phase_rank or ingestion_phase_rank_sql(alias)
    return f"""
        {key} ASC,
        {rank} DESC,
        {alias}.queued_at ASC,
        {alias}.job_id ASC"""


def ingestion_dispatch_key_sql(alias: str = "j") -> str:
    """取る順番の第 1 の鍵(文書の取込の最初の段の投入の時刻。繰り上げは上限まで。#2027)。"""
    continuation = _sql_phase_list(INGESTION_CONTINUATION_PHASES)
    start = _sql_phase_list(INGESTION_HEAVY_PHASES)
    a = alias
    return f"""CASE WHEN {a}.phase IN ({continuation}) THEN GREATEST(
            COALESCE(
                (
                    SELECT MAX(start_job.queued_at)
                    FROM rag_ingestion_jobs start_job
                    WHERE start_job.document_id = {a}.document_id
                      AND DECODE(start_job.recipe_id, {a}.recipe_id, 1, 0) = 1
                      AND start_job.phase IN ({start})
                      AND start_job.queued_at <= {a}.queued_at
                ),
                {a}.queued_at
            ),
            {a}.queued_at
                - NUMTODSINTERVAL(:{INGESTION_DISPATCH_PRIORITY_WINDOW_BIND}, 'SECOND')
        ) ELSE {a}.queued_at END"""


def ingestion_phase_rank_sql(alias: str = "j") -> str:
    """取る順番の第 2 の鍵(後の段ほど大きい。同じ時刻なら後の段を先に取る)。"""
    return (
        f"CASE {alias}.phase WHEN 'INDEX' THEN 3 WHEN 'CHUNK' THEN 2 "
        "WHEN 'EXTRACT' THEN 1 ELSE 0 END"
    )


def ingestion_phase_class_sql(alias: str = "j") -> str:
    """job の段の枠(重い段 ``HEAVY`` / 軽い段 ``LIGHT``)。順番待ちの前の件数を枠ごとに数える。"""
    return (
        f"CASE WHEN {alias}.phase IN ({_sql_phase_list(INGESTION_HEAVY_PHASES)}) "
        "THEN 'HEAVY' ELSE 'LIGHT' END"
    )


def ingestion_dispatch_order_binds(settings: Settings) -> dict[str, object]:
    """``ingestion_dispatch_order_sql`` の bind(#2027)。"""
    return {
        INGESTION_DISPATCH_PRIORITY_WINDOW_BIND: (
            settings.ingestion_queue_pipeline_priority_window_seconds
        )
    }


def _oracle_document_list_order_sql() -> str:
    """管理一覧と全件選択の ID 一覧で、ページング前の順序をそろえる。"""
    return (
        _oracle_natural_name_order_sql("rag_documents.file_name")
        + ", NLSSORT(file_name, 'NLS_SORT=JAPANESE_M'), file_name ASC, document_id ASC"
    )


def _oracle_natural_name_order_sql(expression: str) -> str:
    """名前の数字の連続を桁数と値で比較する。SQL の内部の列式だけを受け取る。

    全角の数字をそろえ、先頭のゼロを外した桁数を固定幅で前置する。
    対象の名前は最大 512 文字なので桁数は 3 桁で表せる。数値への変換はせず、
    Oracle の NUMBER の精度より長い数字も比較する。ページングの前に使う。
    """
    normalized = f"TRANSLATE({expression}, '０１２３４５６７８９', '0123456789')"
    significant = "COALESCE(LTRIM(name_token, '0'), '0')"
    return f"""NLSSORT((
        SELECT LISTAGG(
            CASE WHEN REGEXP_LIKE(name_token, '^[0-9]+$')
                THEN LPAD(LENGTH({significant}), 3, '0') || {significant}
                ELSE name_token
            END, ''
        ) WITHIN GROUP (ORDER BY token_index)
        FROM (
            SELECT LEVEL AS token_index,
                REGEXP_SUBSTR({normalized}, '[0-9]+|[^0-9]+', 1, LEVEL) AS name_token
            FROM dual
            CONNECT BY LEVEL <= REGEXP_COUNT({normalized}, '[0-9]+|[^0-9]+')
        )
    ), 'NLS_SORT=JAPANESE_M_CI')"""


def _render_sql(template: str, **parts: str) -> str:
    """内部生成した SQL 断片だけを template へ埋め込む。bind 値はここに渡さない。"""
    rendered = template
    for name, value in parts.items():
        rendered = rendered.replace(f"{{{name}}}", value)
    return rendered


def _language_pack_relation_sql(relation: LanguagePackIndexRelation) -> str:
    active = """SELECT 1 FROM rag_chunk_sets cs
        WHERE cs.document_id = rag_documents.document_id AND cs.is_active = 1"""
    known = "cs.language_pack_release_id IS NOT NULL AND cs.language_pack_ingest_hash IS NOT NULL"
    current_hash = (
        "(SELECT ingest_hash FROM rag_language_pack_releases "
        "WHERE release_id = :current_language_release_id)"
    )
    different = f"EXISTS ({active} AND {known} AND cs.language_pack_ingest_hash <> {current_hash})"
    missing = (
        f"(NOT EXISTS ({active}) OR EXISTS ({active} AND "
        "(cs.language_pack_release_id IS NULL OR cs.language_pack_ingest_hash IS NULL)))"
    )
    if relation == LanguagePackIndexRelation.DIFFERENT:
        return different
    if relation == LanguagePackIndexRelation.UNRECORDED:
        return f"NOT ({different}) AND {missing}"
    same = f"EXISTS ({active}) AND NOT ({different}) AND NOT ({missing})"
    other = f"EXISTS ({active} AND cs.language_pack_release_id <> :current_language_release_id)"
    return (
        f"{same} AND {other}"
        if relation == LanguagePackIndexRelation.EQUIVALENT
        else f"{same} AND NOT ({other})"
    )


def _oracle_document_where(
    *,
    status: FileStatus | None = None,
    query: str | None = None,
    knowledge_base_id: str | None = None,
    queued_phase: IngestionJobPhase | None = None,
    language_pack_relation: LanguagePackIndexRelation | None = None,
    current_release_id: str | None = None,
) -> tuple[str, dict[str, object]]:
    clauses = _oracle_access_predicates()
    binds = _with_tenant_bind({})
    if status is not None:
        # 一覧に出す状態(レシピ1の状態)で絞り込む(#2012)。
        clauses.append(f"{_DOCUMENT_LIST_STATUS_SQL} = :status")
        binds["status"] = status.value
        if queued_phase is None:
            # 順番待ちの行は「〈工程〉の順番待ち」と出るため、状態の絞り込みには入れない(#2677)。
            clauses.append(f"{_DOCUMENT_LIST_QUEUED_PHASE_SQL} IS NULL")
    if queued_phase is not None:
        # 一覧の行が「〈工程〉の順番待ち」と出る段で絞り込む(#2677)。
        clauses.append(f"{_DOCUMENT_LIST_QUEUED_PHASE_SQL} = :queued_phase")
        binds["queued_phase"] = queued_phase.value
    if query and query.strip():
        clauses.append(
            "(LOWER(file_name) LIKE :query ESCAPE '\\' "
            "OR LOWER(category_name) LIKE :query ESCAPE '\\')"
        )
        binds["query"] = _like_pattern(query)
    knowledge_base_ids = _filter_id_values(knowledge_base_id)
    if knowledge_base_ids:
        knowledge_base_filter_sql, knowledge_base_binds = _oracle_in_predicate(
            "dkb.knowledge_base_id",
            "filter_knowledge_base_id",
            knowledge_base_ids,
        )
        # 所属の一覧はアーカイブ済みの KB でも所属を返す（アーカイブは文書を消さない。
        # 検索対象から外す ACTIVE 条件は検索の SQL（_oracle_retrieval_where）だけが持つ。#282）。
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM rag_document_knowledge_bases dkb
                JOIN rag_knowledge_bases kb
                  ON kb.knowledge_base_id = dkb.knowledge_base_id
                WHERE dkb.document_id = rag_documents.document_id
                  AND {knowledge_base_filter_sql}
                  AND {knowledge_base_access_sql}
            )
            """.format(
                knowledge_base_filter_sql=knowledge_base_filter_sql,
                knowledge_base_access_sql=_oracle_knowledge_base_access_predicate_sql(alias="kb"),
            )
        )
        binds.update(knowledge_base_binds)
    if language_pack_relation is not None:
        if not current_release_id:
            raise ValueError("言語資源の比較対象版を指定してください。")
        clauses.append("superseded_by_document_id IS NULL")
        clauses.append(_language_pack_relation_sql(language_pack_relation))
        binds["current_language_release_id"] = current_release_id
    return " AND ".join(clauses), binds


# 品質評価のテンプレートの「文書一覧」で、1 回の問い合わせに渡す文書の数
# （IN の上限 1000 より少なく）。
_EVALUATION_LOCATION_BATCH = 500


def _oracle_in_predicate(
    column: str,
    bind_prefix: str,
    values: Sequence[str],
) -> tuple[str, dict[str, object]]:
    """可変長 IN 条件を bind 付きで生成する。"""
    unique_values = _unique_sequence(values)
    binds: dict[str, object] = {
        f"{bind_prefix}_{index}": value for index, value in enumerate(unique_values)
    }
    placeholders = ", ".join(f":{key}" for key in binds)
    return f"{column} IN ({placeholders})", binds


def _oracle_knowledge_base_where(
    *,
    status: KnowledgeBaseStatus | None = None,
    query: str | None = None,
    knowledge_base_ids: Sequence[str] | None = None,
) -> tuple[str, dict[str, object]]:
    clauses = _oracle_knowledge_base_access_predicates(alias="kb")
    binds = _with_tenant_bind({})
    if knowledge_base_ids is not None:
        ids = _unique_optional_sequence(knowledge_base_ids)
        if not ids:
            # 空の ID 指定は「どれにも一致しない」(IN () は SQL として不正)。
            clauses.append("1 = 0")
        else:
            in_sql, in_binds = _oracle_in_predicate("kb.knowledge_base_id", "filter_kb_id", ids)
            clauses.append(in_sql)
            binds.update(in_binds)
    if status is not None:
        clauses.append("kb.status = :knowledge_base_status")
        binds["knowledge_base_status"] = status.value
    if query and query.strip():
        clauses.append(
            "(LOWER(kb.name) LIKE :knowledge_base_query ESCAPE '\\' "
            "OR LOWER(kb.description) LIKE :knowledge_base_query ESCAPE '\\')"
        )
        binds["knowledge_base_query"] = _like_pattern(query)
    return " AND ".join(clauses), binds


def _oracle_search_answer_profile_where(
    *,
    status: SearchAnswerProfileStatus | None = None,
    query: str | None = None,
    search_answer_profile_ids: Sequence[str] | None = None,
) -> tuple[str, dict[str, object]]:
    clauses = _oracle_search_answer_profile_access_predicates(alias="bv")
    binds = _with_search_answer_profile_scope_bind(_with_tenant_bind({}))
    if search_answer_profile_ids is not None:
        ids = _unique_optional_sequence(search_answer_profile_ids)
        if not ids:
            # 空の ID 指定は「どれにも一致しない」(IN () は SQL として不正)。
            clauses.append("1 = 0")
        else:
            in_sql, in_binds = _oracle_in_predicate(
                "bv.search_answer_profile_id", "filter_bv_id", ids
            )
            clauses.append(in_sql)
            binds.update(in_binds)
    if status is not None:
        clauses.append("bv.status = :search_answer_profile_status")
        binds["search_answer_profile_status"] = status.value
    if query and query.strip():
        clauses.append(
            "(LOWER(bv.name) LIKE :search_answer_profile_query ESCAPE '\\' "
            "OR LOWER(bv.description) LIKE :search_answer_profile_query ESCAPE '\\')"
        )
        binds["search_answer_profile_query"] = _like_pattern(query)
    return " AND ".join(clauses), binds


_CHUNK_PAGE_START_SQL = (
    "COALESCE(JSON_VALUE(c.metadata_json, '$.page_start' RETURNING NUMBER), "
    "JSON_VALUE(c.metadata_json, '$.page_number' RETURNING NUMBER))"
)
_CHUNK_PAGE_END_SQL = (
    "COALESCE(JSON_VALUE(c.metadata_json, '$.page_end' RETURNING NUMBER), "
    "JSON_VALUE(c.metadata_json, '$.page_start' RETURNING NUMBER), "
    "JSON_VALUE(c.metadata_json, '$.page_number' RETURNING NUMBER))"
)
# 表計算の chunk のシート名と行の範囲(#1221 の sheet_name・row_start・row_end。#2663)。行の記録の
# 子は自分の行、表の一部の親は parent_row_start〜parent_row_end を持つ。
_CHUNK_SHEET_SQL = "JSON_VALUE(c.metadata_json, '$.sheet_name')"
_CHUNK_ROW_START_SQL = (
    "COALESCE(JSON_VALUE(c.metadata_json, '$.row_start' RETURNING NUMBER), "
    "JSON_VALUE(c.metadata_json, '$.parent_row_start' RETURNING NUMBER))"
)
_CHUNK_ROW_END_SQL = (
    "COALESCE(JSON_VALUE(c.metadata_json, '$.row_end' RETURNING NUMBER), "
    "JSON_VALUE(c.metadata_json, '$.row_start' RETURNING NUMBER), "
    "JSON_VALUE(c.metadata_json, '$.parent_row_end' RETURNING NUMBER))"
)


# 重複の文書(同じ内容のアップロード)が自前の索引を持つか(#1381)。持つ重複は自分の chunk で
# 検索され、正本(`duplicate_of_document_id`)を検索の範囲に入れない。
_DUPLICATE_HAS_OWN_INDEX_SQL = """
    EXISTS (
        SELECT 1
        FROM rag_chunk_sets own_cs
        JOIN rag_document_recipes own_r
          ON own_r.recipe_id = own_cs.recipe_id
        WHERE own_cs.document_id = duplicate_d.document_id
          AND own_cs.is_active = 1
          AND own_cs.status = 'INDEXED'
    )
"""


def _duplicate_reuses_canonical_sql(*, include_superseded: bool) -> str:
    """KB に属する重複の文書が、正本の chunk を検索に使う条件(#1381)。

    重複の取込を省いた文書(自前の索引が無い)だけが正本の chunk を使う。自前の索引を持つ重複
    (取込を進めた文書)まで正本を範囲に入れると、別のナレッジベースの同じ内容の文書(版の登録・
    レシピの違う文書)が検索に混ざる。旧版の除外は KB に属する重複の側の登録で判定する
    (重複を旧版として登録したら、その重複から正本へは届かない)。
    """
    predicates = [f"NOT {_DUPLICATE_HAS_OWN_INDEX_SQL.strip()}"]
    if not include_superseded:
        predicates.append("duplicate_d.superseded_by_document_id IS NULL")
    return " AND ".join(predicates)


def _oracle_retrieval_where(
    filters: dict[str, str], *, effective_period: bool = True
) -> tuple[str, dict[str, object]]:
    """検索と同じ見え方の条件(``c`` = chunk と ``d`` = 文書を join した SQL の WHERE)。

    ``effective_period=False`` は有効期間で絞らない(ID で読む入口。#3249)。検索で有効期間を当てて
    選んだ根拠を、ID で読み直すときに今日の日付で変えない。tenant・認可の範囲・INDEXED・有効な
    chunk_set の条件は同じに当てる。

    文書の側の条件で文書の集合を副問い合わせで先に決め(``c.document_id IN (...)``)、
    chunk の側の条件を並べる(#2800)。両方を 1 つの WHERE に並べると、ナレッジベースの
    所属・重複の文書の正本・有効期間の副問い合わせを optimizer が行ごとの FILTER にし、
    範囲の chunk を読むだけの SQL が 1 回 9 秒かかった(分けると 0.7 秒。読む行は同じ)。
    """
    document_sql, chunk_sql, binds = _oracle_retrieval_where_split(
        filters, effective_period=effective_period
    )
    return (
        f"c.document_id IN (SELECT d.document_id FROM rag_documents d WHERE {document_sql})"
        f" AND {chunk_sql}",
        binds,
    )


# chunk の側（``c``・``cs``・``r``）の列を読む条件（#2800。それ以外は文書の側の条件）。
_CHUNK_SIDE_CLAUSE = re.compile(r"(?<![\w.$])(?:c|cs|r)\.")


def _oracle_retrieval_where_split(
    filters: dict[str, str], *, effective_period: bool = True, vector_prefilter: bool = False
) -> tuple[str, str, dict[str, object]]:
    """``_oracle_retrieval_where`` と同じ条件を、文書の側（``d`` だけ）と chunk の側に
    分ける(#2800)。

    vector の検索は、文書の側の条件で文書の集合を先に決め、chunk には ``document_id`` の集合と
    chunk の側の条件だけを付けて近傍を探す(``_vector_search_sql``)。条件を 1 つの WHERE に
    並べると、文書の側の副問い合わせ（ナレッジベースの所属と重複の文書の正本・有効期間）を
    optimizer が行ごとの FILTER にし、近傍の検索が chunk の全件の走査になっていた(検証環境で
    1 回 9 秒 → 分けて 0.7 秒。上位の結果は同じ)。

    ``vector_prefilter=True`` は active な chunk_set の条件を、vector の索引の前で絞れる形
    (``_ACTIVE_CHUNK_PREFILTER_SQL``)にする(vector の検索だけ。#74)。
    """
    clauses, binds = _oracle_retrieval_clauses(
        filters, effective_period=effective_period, vector_prefilter=vector_prefilter
    )
    chunk = [clause for clause in clauses if _CHUNK_SIDE_CLAUSE.search(clause)]
    document = [clause for clause in clauses if not _CHUNK_SIDE_CLAUSE.search(clause)]
    return " AND ".join(document) or "1 = 1", " AND ".join(chunk) or "1 = 1", binds


# 検索する chunk(各レシピの active な chunk_set と、レシピの無い文書の未タグの chunk)を、
# vector の索引の前で絞れる形にした条件(vector の検索だけが使う。#74)。下の EXISTS ... OR ... と
# 同じ行を選ぶ。相関の副問い合わせを OR で結ぶと、Oracle は条件を vector の索引の前の絞り込み
# (PRE-FILTER)に入れられず、索引が近い順に k 件を返した後で捨てる。再取込の途中の chunk_set
# (active でない)が問に近いと k 件のうち数件しか残らない(合成の 5 千 chunk で、索引を使った
# 計画の再現率は文書 1 件の範囲で 0.72、絞り込みなしで最小 0.7)。
# OR を使わず、1 つの列だけの式の IN / NOT IN にする(2 つの列を使う式・OR は PRE-FILTER に
# 入らない。Oracle Database 23.9 で確かめた):
# - active な chunk_set か、未タグ(印の値 ``_UNTAGGED_CHUNK_SET``。生成される chunk_set の ID は
#   ``#`` を含まない)。
# - レシピのある文書に残った未タグの chunk(旧 schema の残り)ではない。
_UNTAGGED_CHUNK_SET = "#untagged"
_ACTIVE_CHUNK_PREFILTER_SQL = f"""
            NVL(c.chunk_set_id, '{_UNTAGGED_CHUNK_SET}') IN (
                SELECT active_cs.chunk_set_id
                FROM rag_chunk_sets active_cs
                JOIN rag_document_recipes active_r
                  ON active_r.recipe_id = active_cs.recipe_id
                WHERE active_cs.is_active = 1
                  AND active_cs.status = 'INDEXED'
                UNION ALL
                SELECT '{_UNTAGGED_CHUNK_SET}' FROM dual
            )
            AND c.chunk_id NOT IN (
                SELECT residue_c.chunk_id
                FROM rag_chunks residue_c
                JOIN rag_document_recipes residue_r
                  ON residue_r.document_id = residue_c.document_id
                WHERE residue_c.chunk_set_id IS NULL
            )
            """


def _oracle_retrieval_clauses(
    filters: dict[str, str], *, effective_period: bool = True, vector_prefilter: bool = False
) -> tuple[list[str], dict[str, object]]:
    clauses = ["d.status = 'INDEXED'", *_oracle_access_predicates(alias="d")]
    binds = _with_tenant_bind({}, alias="d")
    # 実験: chunk_set_id を明示すると is_serving に関係なくその chunk_set だけを検索する。
    explicit_chunk_set_id = (filters.get("chunk_set_id") or "").strip()
    if not explicit_chunk_set_id and vector_prefilter:
        clauses.append(_ACTIVE_CHUNK_PREFILTER_SQL)
    elif not explicit_chunk_set_id:
        # 1 文書 1〜3 レシピ: 各レシピの直近成功出力だけを常に融合検索する。
        # 旧 schema の未タグ chunk はレシピ行がまだ無い文書に限り後方互換で採用する。
        clauses.append("""
            (
                EXISTS (
                    SELECT 1
                    FROM rag_chunk_sets active_cs
                    JOIN rag_document_recipes active_r
                      ON active_r.recipe_id = active_cs.recipe_id
                    WHERE active_cs.chunk_set_id = c.chunk_set_id
                      AND active_cs.is_active = 1
                      AND active_cs.status = 'INDEXED'
                )
                OR (
                    c.chunk_set_id IS NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM rag_document_recipes legacy_r
                        WHERE legacy_r.document_id = c.document_id
                    )
                )
            )
            """)
    knowledge_base_ids = _filter_id_values(filters.get("knowledge_base_id"))
    knowledge_base_filter_sql = ""
    if knowledge_base_ids:
        knowledge_base_filter_sql, knowledge_base_binds = _oracle_in_predicate(
            "dkb.knowledge_base_id",
            "filter_knowledge_base_id",
            knowledge_base_ids,
        )
        binds.update(knowledge_base_binds)
        knowledge_base_filter_sql = f"AND {knowledge_base_filter_sql}"
    if (
        knowledge_base_ids
        or access_scope_context("knowledge_base_scope").allowed_knowledge_base_ids is not None
    ):
        # ナレッジベースに所属する文書と、所属する重複の文書の正本（#2124）。相関の副問い合わせを
        # OR で結ぶと optimizer が semi join に変えられず、全文検索・vector の検索が返した行ごとに
        # 副問い合わせを実行する（DB 全体の命中数 × ナレッジベースの文書数に比例して遅くなり、
        # Oracle Text の SCORE も一部の行でずれる）。IN (... UNION ALL ...) にすると文書の集合を
        # 1 回だけ作る。
        clauses.append(
            """
            d.document_id IN (
                SELECT dkb.document_id
                FROM rag_document_knowledge_bases dkb
                JOIN rag_knowledge_bases kb
                  ON kb.knowledge_base_id = dkb.knowledge_base_id
                WHERE kb.status = 'ACTIVE'
                  AND {knowledge_base_access_sql}
                  {knowledge_base_filter_sql}
                UNION ALL
                SELECT duplicate_d.duplicate_of_document_id
                FROM rag_document_knowledge_bases dkb
                JOIN rag_knowledge_bases kb
                  ON kb.knowledge_base_id = dkb.knowledge_base_id
                JOIN rag_documents duplicate_d
                  ON duplicate_d.document_id = dkb.document_id
                WHERE kb.status = 'ACTIVE'
                  AND {knowledge_base_access_sql}
                  {knowledge_base_filter_sql}
                  AND {duplicate_document_access_sql}
                  AND {duplicate_reuse_sql}
            )
            """.format(
                duplicate_document_access_sql=_oracle_access_predicate_sql(alias="duplicate_d"),
                duplicate_reuse_sql=_duplicate_reuses_canonical_sql(
                    include_superseded=_includes_superseded_documents(filters)
                ),
                knowledge_base_access_sql=_oracle_knowledge_base_access_predicate_sql(alias="kb"),
                knowledge_base_filter_sql=knowledge_base_filter_sql,
            )
        )
    for key, value in filters.items():
        cleaned = value.strip()
        if not cleaned:
            continue
        if key == "document_id":
            clauses.append("d.document_id = :filter_document_id")
            binds["filter_document_id"] = cleaned
        elif key == "chunk_set_id":
            clauses.append("c.chunk_set_id = :filter_chunk_set_id")
            binds["filter_chunk_set_id"] = cleaned
        elif key == "status":
            clauses.append("d.status = :filter_status")
            binds["filter_status"] = cleaned
        elif key == "file_name":
            clauses.append("LOWER(d.file_name) LIKE :filter_file_name ESCAPE '\\'")
            binds["filter_file_name"] = _like_pattern(cleaned)
        elif key == "category_name":
            clauses.append("LOWER(d.category_name) LIKE :filter_category_name ESCAPE '\\'")
            binds["filter_category_name"] = _like_pattern(cleaned)
        elif key == "knowledge_base_id":
            continue
        elif key == "content_kind":
            clauses.append(
                "LOWER(JSON_VALUE(c.metadata_json, '$.content_kind')) = :filter_content_kind"
            )
            binds["filter_content_kind"] = cleaned.casefold()
        elif key == "section_title":
            clauses.append(
                "LOWER(JSON_VALUE(c.metadata_json, '$.section_title')) "
                "LIKE :filter_section_title ESCAPE '\\'"
            )
            binds["filter_section_title"] = _like_pattern(cleaned)
        elif key == "section_path":
            clauses.append(
                "LOWER(JSON_VALUE(c.metadata_json, '$.section_path')) "
                "LIKE :filter_section_path ESCAPE '\\'"
            )
            binds["filter_section_path"] = _like_pattern(cleaned)
        elif key == "source_acl":
            clauses.append(
                "LOWER(JSON_VALUE(c.metadata_json, '$.source_acl')) = :filter_source_acl"
            )
            binds["filter_source_acl"] = cleaned.casefold()
        elif key == "document_version":
            clauses.append(
                "LOWER(JSON_VALUE(c.metadata_json, '$.document_version')) "
                "= :filter_document_version"
            )
            binds["filter_document_version"] = cleaned.casefold()
        # ページは chunk の page_start〜page_end(無ければ page_number)との重なりで判定する。
        # small_to_big 以外の分割は page_number を持たないため、page_number だけで見ると
        # その chunk がすべて外れる(#717)。
        elif key == "page_number_min":
            clauses.append(f"{_CHUNK_PAGE_END_SQL} >= :filter_page_number_min")
            binds["filter_page_number_min"] = int(cleaned)
        elif key == "page_number_max":
            clauses.append(f"{_CHUNK_PAGE_START_SQL} <= :filter_page_number_max")
            binds["filter_page_number_max"] = int(cleaned)
        elif key == PAGE_RANGES_FILTER_KEY:
            range_clauses: list[str] = []
            for index, page_range in enumerate(parse_page_ranges(cleaned)):
                parts = [f"d.document_id = :filter_pr_doc_{index}"]
                binds[f"filter_pr_doc_{index}"] = page_range.document_id
                if page_range.sheet_name is not None:
                    # 表計算のシートの行の範囲(#2663)。chunk のシート名と行の範囲の重なりで
                    # 判定する。
                    parts.append(f"{_CHUNK_SHEET_SQL} = :filter_pr_sheet_{index}")
                    binds[f"filter_pr_sheet_{index}"] = page_range.sheet_name
                    if page_range.row_end is not None:
                        parts.append(f"{_CHUNK_ROW_START_SQL} <= :filter_pr_row_end_{index}")
                        binds[f"filter_pr_row_end_{index}"] = page_range.row_end
                    if page_range.row_start is not None:
                        parts.append(f"{_CHUNK_ROW_END_SQL} >= :filter_pr_row_start_{index}")
                        binds[f"filter_pr_row_start_{index}"] = page_range.row_start
                    range_clauses.append("(" + " AND ".join(parts) + ")")
                    continue
                if page_range.page_end is not None:
                    parts.append(f"{_CHUNK_PAGE_START_SQL} <= :filter_pr_end_{index}")
                    binds[f"filter_pr_end_{index}"] = page_range.page_end
                if page_range.page_start is not None:
                    parts.append(f"{_CHUNK_PAGE_END_SQL} >= :filter_pr_start_{index}")
                    binds[f"filter_pr_start_{index}"] = page_range.page_start
                range_clauses.append("(" + " AND ".join(parts) + ")")
            if range_clauses:
                clauses.append("(" + " OR ".join(range_clauses) + ")")
        elif key == "uploaded_from":
            clauses.append("d.uploaded_at >= :filter_uploaded_from")
            binds["filter_uploaded_from"] = _parse_filter_datetime(cleaned, end_of_day=False)
        elif key == "uploaded_to":
            clauses.append("d.uploaded_at <= :filter_uploaded_to")
            binds["filter_uploaded_to"] = _parse_filter_datetime(cleaned, end_of_day=True)
        elif key == "indexed_from":
            clauses.append("d.indexed_at >= :filter_indexed_from")
            binds["filter_indexed_from"] = _parse_filter_datetime(cleaned, end_of_day=False)
        elif key == "indexed_to":
            clauses.append("d.indexed_at <= :filter_indexed_to")
            binds["filter_indexed_to"] = _parse_filter_datetime(cleaned, end_of_day=True)
        elif key == "content_kinds":
            kinds = [part.strip().casefold() for part in cleaned.split(",") if part.strip()]
            if kinds:
                predicate, kind_binds = _oracle_in_predicate(
                    "LOWER(JSON_VALUE(c.metadata_json, '$.content_kind'))",
                    "filter_content_kind_in",
                    kinds,
                )
                clauses.append(predicate)
                binds.update(kind_binds)
        elif key in _CLASSIFICATION_FILTER_KEYS or key == "as_of":
            continue  # 分類と有効期間は _classification_where でまとめて付ける。
        elif key == INCLUDE_SUPERSEDED_FILTER_KEY:
            continue  # 旧版の除外はループの後でまとめて付ける。
        elif key == EXTRACTION_FIELD_FILTER_KEY:
            field_clauses, field_binds = _extraction_field_where(cleaned)
            clauses.extend(field_clauses)
            binds.update(field_binds)
        else:
            raise ValueError(f"未対応の検索フィルターです: {key}")
    classification_clauses, classification_binds = _classification_where(
        filters, effective_period=effective_period
    )
    clauses.extend(classification_clauses)
    binds.update(classification_binds)
    if not _includes_superseded_documents(filters):
        clauses.append(_NOT_SUPERSEDED_SQL)
    return clauses, binds


# 交差参照の文書名の照合で文書名から除く文字(title_key の NFKC 前の形も含む。#1280)。
_REFERENCE_TITLE_NOISE_CHARS = " \u3000_-・:、。,.()[]「」『』【】（）＿－：，．"
_REFERENCE_TITLE_NOISE = frozenset(_REFERENCE_TITLE_NOISE_CHARS)


def _reference_title_key(value: str) -> str:
    """文書名・見出しの列を照合の形にする(小文字にし、空白と区切りの記号を除く)。"""
    return "".join(char for char in value.lower() if char not in _REFERENCE_TITLE_NOISE)


def _match_reference_documents(
    rows: Sequence[Mapping[str, object]], titles: Sequence[str], *, limit: int
) -> dict[str, list[tuple[str, str, tuple[str, ...]]]]:
    """範囲の (文書, 文書名, 見出しの列, 最初の chunk の番号) の行を、各文書名と照合する(#2800)。

    文書名か見出しの列が文書名(全角の英数字の形も)を含む行を、文書名ごとに (文書, 見出しの列の
    先頭の見出し) でまとめ、文書名の短い順・文書・最初の chunk の順に ``limit`` 件までを返す。
    """
    candidates: list[tuple[str, str, str, str, str | None, int]] = []
    for row in rows:
        document_id, file_name = row.get("document_id"), row.get("file_name")
        if not document_id or not file_name:
            continue
        section_path = row.get("section_path")
        path = str(section_path) if section_path is not None else None
        candidates.append(
            (
                str(document_id),
                str(file_name),
                _reference_title_key(str(file_name)),
                _reference_title_key(path) if path is not None else "",
                path.split(" > ", 1)[0] if path is not None else None,
                _int_value(row.get("first_chunk_index")),
            )
        )
    result: dict[str, list[tuple[str, str, tuple[str, ...]]]] = {}
    for title in titles:
        needles = (title, _full_width_ascii(title))
        groups: dict[tuple[str, str, str | None], int] = {}
        for document_id, file_name, name_key, path_key, root, first in candidates:
            if not any(needle in name_key or needle in path_key for needle in needles):
                continue
            key = (document_id, file_name, root)
            groups[key] = min(first, groups.get(key, first))
        ranked = sorted(groups.items(), key=lambda item: (len(item[0][1]), item[0][0], item[1]))
        documents: dict[str, tuple[str, list[str]]] = {}
        for (document_id, file_name, root), _first in ranked[:limit]:
            _name, headings = documents.setdefault(document_id, (file_name, []))
            if root and root not in headings:
                headings.append(root)
        if documents:
            result[title] = [
                (document_id, file_name, tuple(headings))
                for document_id, (file_name, headings) in documents.items()
            ]
    return result


def _full_width_ascii(value: str) -> str:
    """半角の英数字を全角にした形(保存した文書名・見出しの全角の英数字と比べるため。#1400)。"""
    return "".join(
        chr(ord(char) + 0xFEE0) if char.isascii() and char.isalnum() else char for char in value
    )


# 新しい版に置き換えた文書(旧版)を除く述語(#1248)。
_NOT_SUPERSEDED_SQL = "d.superseded_by_document_id IS NULL"
# 派生の chunk の原文を chunk の番号で読むときの、1 回の番号の数の上限(bind の数を抑える。#2641)。
_MAX_SOURCE_CHUNK_INDICES = 200


def _includes_superseded_documents(filters: Mapping[str, str]) -> bool:
    """旧版の文書を検索に含めるか(#1248)。

    既定では含めない。``include_superseded=true`` のときと、文書を ``document_id`` で
    名指ししたとき(文書の chunk 数・根拠の前後の補完など、特定の文書を読む経路)は含める。
    """
    if (filters.get("document_id") or "").strip():
        return True
    return include_superseded_filter_value(filters.get(INCLUDE_SUPERSEDED_FILTER_KEY))


_CLASSIFICATION_FILTER_KEYS = CLASSIFICATION_CATEGORY_KEYS


def _category_label_sql(expression: str) -> str:
    """`category_label` の番号の接頭辞の除去を SQL で行う(後ろに 1 文字以上あるときだけ外す)。"""
    return f"REGEXP_REPLACE({expression}, '^[0-9]+_(.)', '\\1')"


# 項目の条件の演算子と、型ごとの比べる列(#549)。SQL に埋めるのはこの許可リストの値だけで、
# 項目名と値は bind で渡す。
_EXTRACTION_FIELD_SQL_OPERATORS = {"eq": "=", "gte": ">=", "lte": "<="}
_EXTRACTION_FIELD_SQL_COLUMNS = {
    "string": "{alias}.field_text",
    "bool": "LOWER({alias}.field_text)",
    "number": "{alias}.field_number",
    "date": "{alias}.field_date",
}


def _extraction_field_where(value: str) -> tuple[list[str], dict[str, object]]:
    """項目抽出の値の条件を、文書単位の EXISTS にする(#549)。

    文書の採用中の抽出(active な chunk_set が参照する `rag_document_extractions` の行)の
    `extraction_json.fields` を JSON_TABLE で行にし、項目名と値を比べる。number / date に
    変換できない値と、項目の無い文書は一致しない(除かれる)。条件どうしは AND。
    """
    clauses: list[str] = []
    binds: dict[str, object] = {}
    for index, condition in enumerate(parse_extraction_field_filter(value)):
        alias = f"fx_f{index}"
        column = _EXTRACTION_FIELD_SQL_COLUMNS[condition.value_type].format(alias=alias)
        operator = _EXTRACTION_FIELD_SQL_OPERATORS[condition.op]
        typed_value: object = condition.value
        if condition.value_type == "number":
            typed_value = Decimal(condition.value)
        elif condition.value_type == "date":
            typed_value = date.fromisoformat(condition.value)
        binds[f"filter_field_{index}_name"] = condition.name
        binds[f"filter_field_{index}_value"] = typed_value
        clauses.append(
            f"""
            EXISTS (
                SELECT 1
                FROM rag_document_extractions fx{index},
                     JSON_TABLE(
                         fx{index}.extraction_json, '$.fields[*]'
                         COLUMNS (
                             field_name VARCHAR2(120) PATH '$.name',
                             field_text VARCHAR2(4000) PATH '$.value',
                             field_number NUMBER PATH '$.value' NULL ON ERROR,
                             field_date DATE PATH '$.value' NULL ON ERROR
                         )
                     ) {alias}
                WHERE fx{index}.document_id = d.document_id
                  AND EXISTS (
                      SELECT 1
                      FROM rag_chunk_sets fx_cs{index}
                      WHERE fx_cs{index}.document_id = fx{index}.document_id
                        AND fx_cs{index}.extraction_recipe_id = fx{index}.extraction_recipe_id
                        AND fx_cs{index}.is_active = 1
                        AND fx_cs{index}.status = 'INDEXED'
                  )
                  AND {alias}.field_name = :filter_field_{index}_name
                  AND {column} {operator} :filter_field_{index}_value
            )
            """
        )
    return clauses, binds


def _classification_where(
    filters: Mapping[str, str], *, effective_period: bool = True
) -> tuple[list[str], dict[str, object]]:
    """文書の分類と有効期間の述語(rag_poc の _classification_filter_sql と同じ意味)。

    - 分類は指定した項目だけを、番号の接頭辞を除いた名前の一致で絞る(`10_業務A` と `業務A` を
      同じ分類として扱う。rag_engine の `_category_label` と同じ規則。#547)。表記(NFKC・空白)は
      保存時と検索の入力で同じ正規化を通すので、SQL では接頭辞だけを外す。
    - 有効期間は基準日(未指定なら今日)で絞る。期間のない文書は除外しない。終了日は排他的。
      ISO 日付の文字列比較は時系列順と一致する。``effective_period=False`` (ID で読む入口。#3249)
      は有効期間で絞らない。
    """
    clauses: list[str] = []
    binds: dict[str, object] = {}
    for key in _CLASSIFICATION_FILTER_KEYS:
        if label := category_label(filters.get(key)):
            column = f"JSON_VALUE(d.classification, '$.{key}')"
            clauses.append(f"{_category_label_sql(column)} = :filter_{key}")
            binds[f"filter_{key}"] = label
    if not effective_period:
        return clauses, binds
    clauses.append(
        "COALESCE(JSON_VALUE(d.classification, '$.effective_from'), :filter_as_of) <= :filter_as_of"
    )
    clauses.append(
        "COALESCE(JSON_VALUE(d.classification, '$.effective_to'), '9999-12-31') > :filter_as_of"
    )
    binds["filter_as_of"] = (filters.get("as_of") or "").strip() or settings_today().isoformat()
    return clauses, binds


# 前後の文脈(同じ chunk group の sibling)を 1 回の SQL で読む起点の数(#1490)。足りない分は
# 空の起点で埋め、SQL の文を同じにする(解析済みの文を使い回す)。
# 前後の文脈を 1 回の SQL で読む起点の数(回答の検索の候補の上限 150 件を 1 回で読む。#1498)。
_CONTEXT_ANCHOR_BATCH_SIZE = 150
_CONTEXT_ANCHOR_DOCUMENT_PLACEHOLDER = "context_anchor_document"
_CONTEXT_ANCHOR_CHUNK_SET_PLACEHOLDER = "context_anchor_chunk_set"


def _context_group_siblings_batch_sql(
    targets: Sequence[tuple[RetrievedChunk, str, int]],
    *,
    max_chunks_per_group: int,
    neighbor_child_count: int | None = None,
) -> tuple[str, dict[str, object]]:
    """chunk_set を持つ起点の sibling を 1 回で読む SQL と bind(#1490)。

    起点ごとの条件は ``_context_anchor_retrieval_where``(文書と chunk_set で絞る)と同じで、
    文書と chunk_set の値だけを起点の表(``a``)から読む。起点ごとの並び
    (起点からの距離 → chunk_index → chunk_id)と件数の上限も同じ。

    行は sibling の chunk ごとに 1 行(本文と metadata は 1 回だけ読む。#1498)。
    ``anchor_positions`` は、その chunk を sibling に持つ起点の ``起点の位置:起点の中の順位`` を
    ``,`` でつないだもの(起点の位置は ``targets`` の位置。整数だけなので値はそのまま戻る)。
    """
    if len(targets) > _CONTEXT_ANCHOR_BATCH_SIZE:
        raise ValueError("前後の文脈の起点が 1 回の SQL の上限を超えています。")
    # 前後の文脈は起点と同じ文書。有効期間(文書の単位)は起点を選んだ検索で当て済みなので、
    # 今日の日付で絞り直さない(過去の基準日の検索で起点の前後が落ちないように。#3268)。
    where_sql, binds = _oracle_retrieval_where(
        {
            "document_id": _CONTEXT_ANCHOR_DOCUMENT_PLACEHOLDER,
            "chunk_set_id": _CONTEXT_ANCHOR_CHUNK_SET_PLACEHOLDER,
        },
        effective_period=False,
    )
    if (
        binds.pop("filter_document_id", None) != _CONTEXT_ANCHOR_DOCUMENT_PLACEHOLDER
        or binds.pop("filter_chunk_set_id", None) != _CONTEXT_ANCHOR_CHUNK_SET_PLACEHOLDER
    ):
        raise RuntimeError("前後の文脈の検索条件を組み立てられません。")
    where_sql = re.sub(r":filter_document_id\b", "a.document_id", where_sql)
    where_sql = re.sub(r":filter_chunk_set_id\b", "a.chunk_set_id", where_sql)
    anchor_rows: list[str] = []
    for ordinal in range(_CONTEXT_ANCHOR_BATCH_SIZE):
        anchor_rows.append(
            "SELECT "
            f"CAST(:ca_ord_{ordinal} AS NUMBER) AS anchor_ord, "
            f"CAST(:ca_doc_{ordinal} AS VARCHAR2(64)) AS document_id, "
            f"CAST(:ca_set_{ordinal} AS VARCHAR2(64)) AS chunk_set_id, "
            f"CAST(:ca_group_{ordinal} AS VARCHAR2(4000)) AS chunk_group_id, "
            f"CAST(:ca_index_{ordinal} AS NUMBER) AS anchor_index, "
            f"CAST(:ca_chunk_{ordinal} AS VARCHAR2(128)) AS anchor_chunk_id "
            "FROM dual"
        )
        if ordinal < len(targets):
            anchor, group_id, anchor_index = targets[ordinal]
            values: tuple[object, ...] = (
                ordinal,
                anchor.document_id,
                _metadata_str(anchor.metadata.get("chunk_set_id")),
                group_id,
                anchor_index,
                anchor.chunk_id,
            )
        else:
            # 空の起点(どの chunk にも一致しない)。
            values = (None, None, None, None, None, None)
        for name, value in zip(
            ("ord", "doc", "set", "group", "index", "chunk"), values, strict=True
        ):
            binds[f"ca_{name}_{ordinal}"] = value
    binds["max_chunks_per_group"] = max_chunks_per_group
    binds["neighbor_child_count"] = (
        (max_chunks_per_group + 1) // 2
        if neighbor_child_count is None
        else max(0, neighbor_child_count)
    )
    # chunk group の id(metadata_json の JSON_VALUE)は、起点の文書・chunk_set の chunk ごとに
    # 1 回だけ読む(``grp``。NO_MERGE で結合の外に置く)。結合の条件に書くと(起点 × 同じ文書の
    # chunk)の組ごとに CLOB の JSON を解き、起点 150 件で数秒かかっていた(#3461)。起点の表は
    # WITH にせず 2 か所に書く(WITH にすると optimizer が同じ遅い計画を選んだ)。検索の条件・
    # 並び・件数は変えない。
    statement = _render_sql(
        """
        SELECT
            c.document_id,
            c.chunk_id,
            c.chunk_text,
            c.metadata_json,
            c.chunk_index,
            c.chunk_set_id,
            d.file_name,
            d.category_name,
            d.superseded_by_document_id,
            0 AS score,
            p.anchor_positions
        FROM (
            SELECT
                chunk_id,
                LISTAGG(anchor_ord || ':' || context_rank, ',')
                    WITHIN GROUP (ORDER BY anchor_ord, context_rank) AS anchor_positions
            FROM (
                SELECT
                    a.anchor_ord,
                    c.chunk_id,
                    CASE WHEN a.chunk_group_id IS NULL THEN :neighbor_child_count
                         ELSE :max_chunks_per_group END AS context_limit,
                    ROW_NUMBER() OVER (
                        PARTITION BY a.anchor_ord,
                            CASE WHEN a.chunk_group_id IS NOT NULL
                                 OR c.chunk_index < a.anchor_index THEN 0 ELSE 1 END
                        ORDER BY
                            ABS(c.chunk_index - a.anchor_index) ASC,
                            c.chunk_index ASC,
                            c.chunk_id ASC
                    ) AS context_rank
                FROM ({anchor_rows}) a
                JOIN (
                    SELECT /*+ NO_MERGE */
                        g.chunk_id,
                        g.document_id,
                        g.chunk_set_id,
                        JSON_VALUE(g.metadata_json, '$.chunk_group_id') AS chunk_group_id
                    FROM rag_chunks g
                    WHERE (g.document_id, g.chunk_set_id) IN (
                        SELECT x.document_id, x.chunk_set_id FROM ({anchor_rows}) x
                    )
                ) grp
                  ON grp.document_id = a.document_id
                 AND grp.chunk_set_id = a.chunk_set_id
                 AND (a.chunk_group_id IS NULL OR grp.chunk_group_id = a.chunk_group_id)
                JOIN rag_chunks c ON c.chunk_id = grp.chunk_id
                JOIN rag_documents d ON d.document_id = c.document_id
                WHERE {where_sql}
                  AND c.chunk_id <> a.anchor_chunk_id
            )
            WHERE context_rank <= context_limit
            GROUP BY chunk_id
        ) p
        JOIN rag_chunks c ON c.chunk_id = p.chunk_id
        JOIN rag_documents d ON d.document_id = c.document_id
        """,
        anchor_rows=" UNION ALL ".join(anchor_rows),
        where_sql=where_sql,
    )
    return statement, binds


def _anchor_positions(value: object) -> list[tuple[int, int]]:
    """前後の文脈の SQL の ``anchor_positions`` を (起点の位置, 順位) の列に戻す。

    値は整数と区切りだけ(``"0:1,3:2"``。``_context_group_siblings_batch_sql``)。
    """
    text = _optional_str(value)
    if text is None:
        return []
    positions: list[tuple[int, int]] = []
    for item in text.split(","):
        anchor_ord, _, context_rank = item.partition(":")
        positions.append((int(anchor_ord), int(context_rank)))
    return positions


def _context_anchor_retrieval_where(
    anchor: RetrievedChunk,
) -> tuple[str, dict[str, object]]:
    """context 補完を anchor と同じ recipe lineage に限定する。

    有効期間は起点を選んだ検索で当て済み(同じ文書)なので絞り直さない(#3268)。
    """
    chunk_set_id = _metadata_str(anchor.metadata.get("chunk_set_id"))
    filters = {"document_id": anchor.document_id}
    if chunk_set_id is not None:
        filters["chunk_set_id"] = chunk_set_id
        return _oracle_retrieval_where(filters, effective_period=False)

    where_sql, binds = _oracle_retrieval_where(filters, effective_period=False)
    recipe_id = _metadata_str(anchor.metadata.get("recipe_id"))
    if recipe_id is not None:
        binds["anchor_recipe_id"] = recipe_id
        return (
            f"{where_sql} AND EXISTS ("
            "SELECT 1 FROM rag_chunk_sets anchor_cs "
            "WHERE anchor_cs.chunk_set_id = c.chunk_set_id "
            "AND anchor_cs.recipe_id = :anchor_recipe_id "
            "AND anchor_cs.is_active = 1 AND anchor_cs.status = 'INDEXED'"
            ")",
            binds,
        )

    # recipe 導入前の未タグ文書だけを許可し、複数 active recipe の横断混入を防ぐ。
    return (
        f"{where_sql} AND c.chunk_set_id IS NULL AND NOT EXISTS ("
        "SELECT 1 FROM rag_document_recipes anchor_legacy_r "
        "WHERE anchor_legacy_r.document_id = c.document_id"
        ")",
        binds,
    )


def _parse_filter_datetime(value: str, *, end_of_day: bool) -> datetime:
    """検証済みの ISO 8601 日付/日時を tz-aware datetime へ変換する。

    date-only（YYYY-MM-DD）の `_to` 境界は当日全体を含めるため、その日の終端へ寄せる。
    naive な入力は UTC として解釈する。
    """
    raw = value.strip()
    candidate = f"{raw[:-1]}+00:00" if raw.endswith("Z") else raw
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if end_of_day and len(raw) == 10:
        parsed = parsed + timedelta(days=1) - timedelta(microseconds=1)
    return parsed


def _graph_active_chunk_set_predicate(alias: str) -> str:
    """成功して有効化された chunk_set だけを表示し、未タグの行は所属で判定する。"""
    return (
        f"({alias}.chunk_set_id IS NULL OR EXISTS ("
        "SELECT 1 FROM rag_chunk_sets graph_cs "
        f"WHERE graph_cs.chunk_set_id = {alias}.chunk_set_id "
        "AND graph_cs.is_active = 1))"
    )


def _graph_current_membership_predicate(alias: str) -> str:
    """graph 行の KB(取込時のスナップショット)に、元文書が今も所属しているかの predicate。

    元文書は ``source_document_ids``(JSON 配列)で判定する。chunk_set を持たない旧来の行にも
    効くよう、chunk_set 経由では判定しない。knowledge_base_id が NULL の行(取込時にどの KB にも
    所属していなかった文書)は KB scope を持たないため、従来どおり KB 指定なしの検索だけが対象に
    する(KB 指定・KB 権限の predicate が NULL を除外する)。
    """
    return (
        f"({alias}.knowledge_base_id IS NULL OR EXISTS ("
        "SELECT 1 FROM rag_document_knowledge_bases member_dkb "
        f"WHERE member_dkb.knowledge_base_id = {alias}.knowledge_base_id "
        f"AND JSON_EXISTS({alias}.source_document_ids, "
        "'$[*]?(@ == $member_document_id)' "
        'PASSING member_dkb.document_id AS "member_document_id")'
        "))"
    )


def _oracle_vector_fetch_clause(*, top_k: int, target_accuracy: int) -> str:
    """Oracle AI Vector Search の approximate top-k 句を安全な整数 literal で返す。"""
    top_k_literal = _bounded_int_literal(top_k, name="top_k", minimum=1, maximum=1000)
    target_accuracy_literal = _bounded_int_literal(
        target_accuracy,
        name="oracle_vector_target_accuracy",
        minimum=1,
        maximum=100,
    )
    return (
        f"FETCH APPROX FIRST {top_k_literal} ROWS ONLY "
        f"WITH TARGET ACCURACY {target_accuracy_literal}"
    )


def vector_search_statement(
    settings: Settings,
    embedding_bind: object,
    *,
    top_k: int,
    filters: dict[str, str],
    exact: bool = False,
) -> tuple[str, dict[str, object]]:
    """回答の検索の vector 検索 1 本の SQL と bind(``_vector_rows`` が実行するもの)。

    ``exact=True`` は同じ条件・並び・類似度の下限で、近似の句だけを ``FETCH EXACT`` にした
    基準の SQL(近似の検索の再現率を測る評価の比較用。#74)。target accuracy 100 は exact の
    検索そのものではないため、基準には使わない。
    """
    document_sql, chunk_sql, binds = _oracle_retrieval_where_split(filters, vector_prefilter=True)
    binds.update({"embedding": embedding_bind, "min_similarity": settings.rag_min_similarity})
    if exact:
        top_k_literal = _bounded_int_literal(top_k, name="top_k", minimum=1, maximum=1000)
        fetch_clause = f"FETCH EXACT FIRST {top_k_literal} ROWS ONLY"
    else:
        # backend の中の決定論の解決(メモ化した値。#828・#3218)。
        vector_index = resolve_vector_index_adapter(settings)
        fetch_clause = _oracle_vector_fetch_clause(
            top_k=top_k, target_accuracy=vector_index.target_accuracy
        )
    statement = _vector_search_sql(
        select_list=f"{_SEARCH_HIT_COLUMNS}, {_vector_score_sql('embedding')} AS score",
        embedding_bind="embedding",
        document_sql=document_sql,
        chunk_sql=chunk_sql,
        fetch_clause=fetch_clause,
    )
    return statement, binds


# 回答の検索の候補の列(本文・metadata と、文書・レシピの列。``_retrieved_chunk_from_row`` が読む)。
_SEARCH_HIT_COLUMNS = """
    c.document_id,
    c.chunk_id,
    c.chunk_text,
    c.metadata_json,
    c.chunk_index,
    c.chunk_set_id,
    cs.recipe_id,
    r.slot_no AS recipe_slot_no,
    d.file_name,
    d.category_name,
    d.superseded_by_document_id"""
# 検索の候補の SQL で、1 回の往復で受け取る行の数(python-oracledb の既定は 100 行。#1498)。
_SEARCH_FETCH_ROWS = 1000


def _vector_score_sql(embedding_bind: str) -> str:
    return f"1 - VECTOR_DISTANCE(c.embedding, :{embedding_bind}, COSINE)"


def _vector_search_sql(
    *, select_list: str, embedding_bind: str, document_sql: str, chunk_sql: str, fetch_clause: str
) -> str:
    """vector 検索の SQL(条件・並び・件数)。

    1 本の検索文の検索(``_vector_search_with_oracle``)と、複数の検索文の検索(``search_many``)で
    同じものを使う。条件は文書の側
    (``document_sql``。``d`` だけ)と chunk の側(``chunk_sql``)に分けて受け取る
    (``_oracle_retrieval_where_split``。#2800)。文書の集合を先に決め、近傍の検索の並びは距離
    だけにする(Oracle AI Vector Search の近似の検索は ``ORDER BY`` が距離だけのときに vector の
    索引を使う)。同じ距離の行の並び(文書・chunk の番号)と類似度の下限は外側で決める(近傍の
    検索の中に類似度の条件を置くと、optimizer が chunk の全件を走査する計画を選ぶことがある。
    近い順に取った後で下限を掛けても、下限を掛けてから近い順に取るのと同じ結果)。
    """
    return _render_sql(
        """
        SELECT * FROM (
            SELECT {select_list},
                VECTOR_DISTANCE(c.embedding, :{embedding_bind}, COSINE) AS vector_sort_key,
                c.document_id AS vector_document_id,
                c.chunk_index AS vector_chunk_index,
                c.chunk_id AS vector_chunk_id
            FROM rag_chunks c
            JOIN rag_documents d ON d.document_id = c.document_id
            LEFT JOIN rag_chunk_sets cs ON cs.chunk_set_id = c.chunk_set_id
            LEFT JOIN rag_document_recipes r ON r.recipe_id = cs.recipe_id
            WHERE c.document_id IN (SELECT d.document_id FROM rag_documents d WHERE {document_sql})
              AND {chunk_sql}
            ORDER BY VECTOR_DISTANCE(c.embedding, :{embedding_bind}, COSINE)
            {fetch_clause}
        )
        WHERE 1 - vector_sort_key >= :min_similarity
        ORDER BY vector_sort_key ASC, vector_document_id ASC,
            vector_chunk_index ASC, vector_chunk_id ASC
        """,
        select_list=select_list,
        document_sql=document_sql,
        chunk_sql=chunk_sql,
        embedding_bind=embedding_bind,
        fetch_clause=fetch_clause,
    )


def _keyword_search_sql(*, select_list: str, query_bind: str, where_sql: str) -> str:
    """Oracle Text の keyword 検索の SQL(条件・並び・件数。``_vector_search_sql`` と同じく共有)。

    ``c`` は Oracle Text の命中(``_keyword_hits_view_sql``)で、本文・埋め込みを持たない。
    ``select_list`` は ``c`` の命中の列(``c.text_score`` が ``SCORE(1)``)と ``d``・``cs``・``r`` の
    列だけを読める。本文が要るときは、決まった上位の行だけを ``rag_chunks`` から読む
    (``_keyword_search_with_oracle``)。
    """
    return _render_sql(
        """
        SELECT *
        FROM (
            SELECT {select_list}
            FROM {hits_view}
            JOIN rag_documents d ON d.document_id = c.document_id
            LEFT JOIN rag_chunk_sets cs ON cs.chunk_set_id = c.chunk_set_id
            LEFT JOIN rag_document_recipes r ON r.recipe_id = cs.recipe_id
            WHERE {where_sql}
            ORDER BY
                c.text_score DESC,
                c.document_id ASC,
                c.chunk_index ASC,
                c.chunk_id ASC
        )
        WHERE ROWNUM <= :top_k
        """,
        select_list=select_list,
        hits_view=_keyword_hits_view_sql(query_bind=query_bind, where_sql=where_sql),
        where_sql=where_sql,
    )


def _keyword_hits_view_sql(*, query_bind: str, where_sql: str) -> str:
    """Oracle Text の命中(chunk ごとに 1 行と ``SCORE(1)``)の view ``c``(#2137)。

    ``ROWNUM`` で view を区切り(view の併合と条件の押し込みを止める)、Oracle Text の検索を
    1 回だけ実行する。区切らないと、optimizer が文書の側から nested loop を組んで検索を文書の
    数だけ繰り返し、毎回 DB 全体の命中を返す(文書 153 件・命中 3 万件で 1 本 20 秒)。
    ナレッジベース・文書・chunk_set の条件は、この命中を絞る側(``where_sql``)で使う。
    読む列は条件と並びに要る列だけ(``metadata_json`` は条件が使うときだけ)。
    """
    metadata_column = ", hit.metadata_json" if "c.metadata_json" in where_sql else ""
    return _render_sql(
        """(
            SELECT hit.chunk_id, hit.document_id, hit.chunk_index,
                   hit.chunk_set_id{metadata_column},
                   SCORE(1) AS text_score, ROWNUM AS hit_number
            FROM rag_chunks hit
            WHERE CONTAINS(hit.search_text, :{query_bind}, 1) > 0
        ) c""",
        metadata_column=metadata_column,
        query_bind=query_bind,
    )


def _oracle_tenant_predicate(*, alias: str | None = None) -> str:
    tenant_id_hash = _current_tenant_id_hash()
    if tenant_id_hash is None:
        return "1 = 1"
    column = f"{alias}.tenant_id_hash" if alias else "tenant_id_hash"
    return f"{column} = :tenant_id_hash"


def _oracle_conversation_access_predicate_sql(*, alias: str | None = None) -> str:
    """会話を tenant と、認証時は作成ユーザーへ閉じる。

    利用できる検索・回答プロファイルが制限されているときは、その検索・回答プロファイルの会話だけにする（#214）。
    system_admin（`all_users_visible`。#1889）は全員の会話を見て、運用の操作（停止・削除）をする。
    発話の追加・名前の変更・回答の評価は持ち主だけで、route が先に 403 にする（#3244）。
    利用者の分からない context（文脈が無い・利用者の無い context）は、全員の会話ではなく 0 件に
    する（fail-closed。`_feedback_owner_scope`・実行履歴と同じ。#3250）。
    """
    predicates = [_oracle_tenant_predicate(alias=alias)]
    context = access_scope_context("conversation_scope")
    if not context.all_users_visible:
        if context.user_id_hash is None:
            predicates.append("1 = 0")
        else:
            column = f"{alias}.user_id_hash" if alias else "user_id_hash"
            predicates.append(f"{column} = :conversation_user_id_hash")
    predicates.extend(
        _search_answer_profile_scope_predicates(
            f"{alias}.search_answer_profile_id" if alias else "search_answer_profile_id"
        )
    )
    return " AND ".join(predicates)


def _oracle_access_predicates(*, alias: str | None = None) -> list[str]:
    """tenant と認可済み document/category scope を SQL predicate にする。"""
    context = access_scope_context("document_scope")
    predicates = [_oracle_tenant_predicate(alias=alias)]
    document_column = f"{alias}.document_id" if alias else "document_id"
    if context.allowed_document_ids is not None:
        if not context.allowed_document_ids:
            predicates.append("1 = 0")
        else:
            placeholders = ", ".join(
                f":access_document_id_{index}"
                for index, _ in enumerate(sorted(context.allowed_document_ids))
            )
            predicates.append(f"{document_column} IN ({placeholders})")
    category_column = f"{alias}.category_name" if alias else "category_name"
    if context.allowed_category_names is not None:
        if not context.allowed_category_names:
            predicates.append("1 = 0")
        else:
            placeholders = ", ".join(
                f":access_category_name_{index}"
                for index, _ in enumerate(sorted(context.allowed_category_names))
            )
            predicates.append(f"LOWER({category_column}) IN ({placeholders})")
    predicates.extend(_oracle_document_knowledge_base_scope_predicates(alias=alias))
    return predicates


def _oracle_document_knowledge_base_scope_predicates(*, alias: str | None = None) -> list[str]:
    """利用できるナレッジベースが制限されているとき、文書をその KB に属するものへ絞る（#214）。

    重複アップロード（`duplicate_of_document_id`）は複製側が KB に属し、chunk は正本側に
    ある。検索と同じく、複製が許可 KB に属する正本も見えるようにする。
    """
    allowed = access_scope_context("knowledge_base_scope").allowed_knowledge_base_ids
    if allowed is None:
        return []
    if not allowed:
        return ["1 = 0"]
    document_column = f"{alias}.document_id" if alias else "rag_documents.document_id"
    placeholders = ", ".join(
        f":access_knowledge_base_id_{index}" for index, _ in enumerate(sorted(allowed))
    )
    return [
        f"""
        (
            EXISTS (
                SELECT 1
                FROM rag_document_knowledge_bases scope_dkb
                WHERE scope_dkb.document_id = {document_column}
                  AND scope_dkb.knowledge_base_id IN ({placeholders})
            )
            OR EXISTS (
                SELECT 1
                FROM rag_documents scope_duplicate
                JOIN rag_document_knowledge_bases scope_duplicate_dkb
                  ON scope_duplicate_dkb.document_id = scope_duplicate.document_id
                WHERE scope_duplicate.duplicate_of_document_id = {document_column}
                  AND scope_duplicate_dkb.knowledge_base_id IN ({placeholders})
            )
        )
        """
    ]


def _oracle_search_answer_profile_access_predicates(*, alias: str | None = None) -> list[str]:
    """tenant と、利用できる検索・回答プロファイル（#214）を SQL predicate にする。

    bind は `_with_search_answer_profile_scope_bind` で足す（`_with_tenant_bind` には含めない）。
    """
    predicates = [_oracle_tenant_predicate(alias=alias)]
    predicates.extend(
        _search_answer_profile_scope_predicates(
            f"{alias}.search_answer_profile_id" if alias else "search_answer_profile_id"
        )
    )
    return predicates


def _search_answer_profile_scope_predicates(column: str) -> list[str]:
    """利用できる検索・回答プロファイルが制限されているとき、列をその検索・回答プロファイルへ絞る。"""
    allowed = access_scope_context("search_answer_profile_scope").allowed_search_answer_profile_ids
    if allowed is None:
        return []
    if not allowed:
        return ["1 = 0"]
    placeholders = ", ".join(
        f":access_search_answer_profile_id_{index}" for index, _ in enumerate(sorted(allowed))
    )
    return [f"{column} IN ({placeholders})"]


def _feedback_owner_scope(column: str) -> tuple[list[str], dict[str, object]]:
    """利用者フィードバックを、送った利用者で絞る条件と bind（#408。NL2SQL の実行履歴と同じ）。

    SYSTEM_ADMIN（構成管理者・local の利用者を含む。`feedback_all_users`）は絞らない。それ以外は
    自分の `user_id_hash` の行だけ。送信者のない古い行（`user_id_hash IS NULL`）は等号で落ちるので、
    SYSTEM_ADMIN だけが見る。利用者の分からない context は、全件ではなく 0 件にする（fail-closed）。
    """
    context = access_scope_context("feedback_scope")
    if context.feedback_all_users:
        return [], {}
    if context.user_id_hash is None:
        return ["1 = 0"], {}
    return (
        [f"{column} = :feedback_owner_user_id_hash"],
        {"feedback_owner_user_id_hash": context.user_id_hash},
    )


def _answer_record_owner_hash() -> str | None:
    """保存済みの回答を絞る持ち主。None は持ち主で絞らない（#304）。

    SYSTEM_ADMIN と `rag.feedback.manage` を持つ利用者（`answer_records_unrestricted`）は全件。
    利用者のない context は、`_answer_record_scope_predicates` が 0 件にする（fail-closed。#3250）。
    持ち主のない行（#304 より前の回答で、持ち主を補えなかったもの）は、絞るときは見せない。
    """
    context = current_audit_request_context()
    if context.answer_records_unrestricted:
        return None
    return context.user_id_hash


def _answer_record_scope_predicates() -> list[str]:
    """回答履歴を、利用できる検索・回答プロファイル（#214）と持ち主（#304）の回答へ絞る。"""
    predicates = _search_answer_profile_scope_predicates("search_answer_profile_id")
    context = access_scope_context("answer_record_scope")
    if not context.answer_records_unrestricted:
        if context.user_id_hash is None:
            predicates.append("1 = 0")
        else:
            predicates.append("user_id_hash = :answer_owner_user_id_hash")
    return predicates


def _answer_record_scope_sql() -> str:
    """回答履歴を、利用できる検索・回答プロファイルの回答へ絞る（検索・回答プロファイルなしの回答は制限時に見せない）。

    持ち主の回答だけにする（#304）。bind は `_with_answer_record_scope_bind` で足す。
    """
    return " AND ".join(_answer_record_scope_predicates() or ["1 = 1"])


def _with_answer_record_scope_bind(binds: Mapping[str, object]) -> dict[str, object]:
    """`_answer_record_scope_sql` の bind（検索・回答プロファイルの範囲と持ち主）を足す。"""
    resolved = _with_search_answer_profile_scope_bind(binds)
    owner = _answer_record_owner_hash()
    if owner is not None:
        resolved["answer_owner_user_id_hash"] = owner
    return resolved


def _answer_record_list_where(
    *, search_answer_profile_id: str | None, trace_ids: Sequence[str] | None
) -> tuple[str, dict[str, object]]:
    """回答履歴の一覧・件数の WHERE と bind（範囲・持ち主・検索・回答プロファイル・trace_id）。

    業務ガイドの下書きで試した回答（trace_id の接頭辞。#1288）は利用者の回答の履歴に出さない。
    """
    clauses = [
        _answer_record_scope_sql(),
        f"trace_id NOT LIKE '{GUIDE_PREVIEW_TRACE_PREFIX}%'",
    ]
    binds = _with_answer_record_scope_bind({})
    if search_answer_profile_id:
        clauses.append("search_answer_profile_id = :search_answer_profile_id")
        binds["search_answer_profile_id"] = search_answer_profile_id
    if trace_ids is not None:
        if not trace_ids:
            clauses.append("1 = 0")
        else:
            placeholders = ", ".join(f":trace_id_{index}" for index in range(len(trace_ids)))
            clauses.append(f"trace_id IN ({placeholders})")
            binds.update({f"trace_id_{index}": value for index, value in enumerate(trace_ids)})
    return " AND ".join(clauses), binds


def _with_search_answer_profile_scope_bind(binds: Mapping[str, object]) -> dict[str, object]:
    """`_search_answer_profile_scope_predicates` の bind を足す。"""
    resolved = dict(binds)
    allowed = current_audit_request_context().allowed_search_answer_profile_ids
    if allowed is not None:
        for index, search_answer_profile_id in enumerate(sorted(allowed)):
            resolved[f"access_search_answer_profile_id_{index}"] = search_answer_profile_id
    return resolved


def _oracle_access_predicate_sql(*, alias: str | None = None) -> str:
    """tenant と認可 scope の predicate を AND で結合する。"""
    return " AND ".join(_oracle_access_predicates(alias=alias))


def _oracle_knowledge_base_access_predicates(*, alias: str | None = None) -> list[str]:
    """tenant と認可済み knowledge base scope を SQL predicate にする。"""
    context = access_scope_context("knowledge_base_scope")
    predicates = [_oracle_tenant_predicate(alias=alias)]
    knowledge_base_column = f"{alias}.knowledge_base_id" if alias else "knowledge_base_id"
    if context.allowed_knowledge_base_ids is not None:
        if not context.allowed_knowledge_base_ids:
            predicates.append("1 = 0")
        else:
            placeholders = ", ".join(
                f":access_knowledge_base_id_{index}"
                for index, _ in enumerate(sorted(context.allowed_knowledge_base_ids))
            )
            predicates.append(f"{knowledge_base_column} IN ({placeholders})")
    return predicates


def _oracle_knowledge_base_access_predicate_sql(*, alias: str | None = None) -> str:
    """tenant と knowledge base scope の predicate を AND で結合する。"""
    return " AND ".join(_oracle_knowledge_base_access_predicates(alias=alias))


def _oracle_membership_access_predicate_sql() -> str:
    """membership table に対する tenant / knowledge base scope predicate。"""
    predicates = [_oracle_tenant_predicate()]
    context = access_scope_context("knowledge_base_scope")
    if context.allowed_knowledge_base_ids is not None:
        if not context.allowed_knowledge_base_ids:
            predicates.append("1 = 0")
        else:
            placeholders = ", ".join(
                f":access_knowledge_base_id_{index}"
                for index, _ in enumerate(sorted(context.allowed_knowledge_base_ids))
            )
            predicates.append(f"knowledge_base_id IN ({placeholders})")
    return " AND ".join(predicates)


def _with_tenant_bind(
    binds: Mapping[str, object],
    *,
    alias: str | None = None,
) -> dict[str, object]:
    del alias
    resolved = dict(binds)
    context = current_audit_request_context()
    if context.tenant_id_hash is not None:
        resolved["tenant_id_hash"] = context.tenant_id_hash
    if context.allowed_document_ids is not None:
        for index, document_id in enumerate(sorted(context.allowed_document_ids)):
            resolved[f"access_document_id_{index}"] = document_id
    if context.allowed_category_names is not None:
        for index, category_name in enumerate(sorted(context.allowed_category_names)):
            resolved[f"access_category_name_{index}"] = category_name
    if context.allowed_knowledge_base_ids is not None:
        for index, knowledge_base_id in enumerate(sorted(context.allowed_knowledge_base_ids)):
            resolved[f"access_knowledge_base_id_{index}"] = knowledge_base_id
    return resolved


def _with_conversation_access_bind(binds: Mapping[str, object]) -> dict[str, object]:
    """会話 access predicate 用の tenant/user/検索・回答プロファイル範囲の bind を足す。"""
    resolved = _with_search_answer_profile_scope_bind(_with_tenant_bind(binds))
    context = current_audit_request_context()
    if context.user_id_hash is not None and not context.all_users_visible:
        resolved["conversation_user_id_hash"] = context.user_id_hash
    return resolved


def _like_pattern(value: str) -> str:
    escaped = value.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _unique_sequence(values: Sequence[str]) -> list[str]:
    unique_values = _unique_optional_sequence(values)
    if not unique_values:
        raise ValueError("ID を 1 件以上指定してください。")
    return unique_values


def _unique_optional_sequence(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    unique_values: list[str] = []
    for value in values:
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        unique_values.append(cleaned)
    return unique_values


def _filter_id_values(value: str | None) -> list[str]:
    if value is None:
        return []
    return _unique_optional_sequence(value.split(","))


def _document_binds(document: StoredDocument) -> dict[str, object]:
    return {
        "document_id": document.id,
        "file_name": document.file_name,
        "status": document.status.value,
        "tenant_id_hash": document.tenant_id_hash,
        "object_storage_path": document.object_storage_path,
        "content_type": document.content_type,
        "file_size_bytes": document.file_size_bytes,
        "content_sha256": document.content_sha256,
        "duplicate_of_document_id": document.duplicate_of_document_id,
        "uploaded_at": document.uploaded_at,
    }


def _knowledge_base_binds(knowledge_base: StoredKnowledgeBase) -> dict[str, object]:
    return {
        "knowledge_base_id": knowledge_base.id,
        "tenant_id_hash": knowledge_base.tenant_id_hash,
        "name": knowledge_base.name,
        "description": knowledge_base.description,
        "status": knowledge_base.status.value,
        "default_search_mode": knowledge_base.default_search_mode.value,
        "retrieval_config": _json_dumps(knowledge_base.retrieval_config),
        "created_at": knowledge_base.created_at,
        "updated_at": knowledge_base.updated_at,
        "archived_at": knowledge_base.archived_at,
    }


def _search_answer_profile_binds(view: StoredSearchAnswerProfile) -> dict[str, object]:
    return {
        "search_answer_profile_id": view.id,
        "tenant_id_hash": view.tenant_id_hash,
        "name": view.name,
        "description": view.description,
        "status": view.status.value,
        "profile_config": _json_dumps(view.profile_config),
        "created_at": view.created_at,
        "updated_at": view.updated_at,
        "archived_at": view.archived_at,
    }


def _conversation_binds(conversation: StoredConversation) -> dict[str, object]:
    return {
        "conversation_id": conversation.id,
        "search_answer_profile_id": conversation.search_answer_profile_id,
        "tenant_id_hash": conversation.tenant_id_hash,
        "user_id_hash": conversation.user_id_hash,
        "title": conversation.title,
        "status": conversation.status,
        "message_count": conversation.message_count,
        "created_at": conversation.created_at,
        "updated_at": conversation.updated_at,
    }


def _conversation_title_from_message(content: str) -> str | None:
    """最初の質問を一覧向けの短い会話タイトルへ整える。"""
    title = " ".join(content.split())
    if not title:
        return None
    if len(title) <= CONVERSATION_TITLE_MAX_CHARS:
        return title
    return f"{title[: CONVERSATION_TITLE_MAX_CHARS - 1]}…"


def _message_binds(message: StoredMessage) -> dict[str, object]:
    return {
        "message_id": message.id,
        "conversation_id": message.conversation_id,
        "reply_to_message_id": message.reply_to_message_id,
        "tenant_id_hash": message.tenant_id_hash,
        "user_id_hash": message.user_id_hash,
        "role": message.role,
        "model": message.model,
        "content": message.content,
        "citations_json": _json_dumps(message.citations),
        "trace_id": message.trace_id,
        "status": message.status,
        "elapsed_ms": message.elapsed_ms,
        "progress_json": None if message.progress is None else _json_dumps(message.progress),
        "lease_owner": message.lease_owner,
        "clarification_json": (
            None if message.clarification is None else _json_dumps(message.clarification)
        ),
        "answer_notice": message.answer_notice,
        "attachments_json": (
            None if message.attachments is None else _json_dumps(message.attachments)
        ),
        "created_at": message.created_at,
    }


def _oracle_conversation_where(
    *,
    search_answer_profile_id: str | None = None,
    updated_since: datetime | None = None,
) -> tuple[str, dict[str, object]]:
    clauses = [_oracle_conversation_access_predicate_sql(alias="c")]
    binds = _with_conversation_access_bind({})
    if search_answer_profile_id is not None:
        clauses.append("c.search_answer_profile_id = :search_answer_profile_id")
        binds["search_answer_profile_id"] = search_answer_profile_id
    if updated_since is not None:
        # 会話の履歴の期間(最後の更新が直近 N 日。#2340)。保存と同じ UTC の時刻で比べる。
        clauses.append("c.updated_at >= :updated_since")
        binds["updated_since"] = updated_since
    return " AND ".join(clauses), binds


def _document_knowledge_base_binds(
    *,
    knowledge_base_id: str,
    document_id: str,
) -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "knowledge_base_id": knowledge_base_id,
        "document_id": document_id,
        "tenant_id_hash": _current_tenant_id_hash(),
        "assigned_at": now,
        "assigned_by_user_id_hash": current_audit_request_context().user_id_hash,
    }


def _ingestion_job_binds(job: IngestionJob) -> dict[str, object]:
    return {
        "job_id": job.id,
        "document_id": job.document_id,
        "recipe_id": job.recipe_id,
        "recipe_revision": job.recipe_revision,
        "language_pack_release_id": job.language_pack_release_id,
        "tenant_id_hash": _current_tenant_id_hash(),
        "status": job.status.value,
        "phase": job.phase.value,
        "parser_profile": job.parser_profile,
        "quality_warnings": _json_dumps(job.quality_warnings),
        "settings_overrides": (
            _json_dumps(job.settings_overrides) if job.settings_overrides is not None else None
        ),
        "skip_reason": job.skip_reason,
        "error_message": job.error_message,
        "attempt_count": job.attempt_count,
        "max_attempts": job.max_attempts,
        "queued_at": job.queued_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


def _ingestion_segment_binds(segment: IngestionSegment) -> dict[str, object]:
    return {
        "segment_id": segment.segment_id,
        "document_id": segment.document_id,
        "recipe_id": segment.recipe_id,
        "tenant_id_hash": _current_tenant_id_hash(),
        "status": segment.status,
        "parser_backend": segment.parser_backend,
        "parser_profile": segment.parser_profile,
        "page_start": segment.page_start,
        "page_end": segment.page_end,
        "attempt_count": segment.attempt_count,
        "artifact_path": segment.artifact_path,
        "error_code": segment.error_code,
        "error_message": segment.error_message,
    }


def _stored_document_from_row(row: Mapping[str, object]) -> StoredDocument:
    return StoredDocument(
        id=str(row["document_id"]),
        file_name=str(row["file_name"]),
        status=_file_status(row["status"]),
        uploaded_at=_datetime_value(row.get("uploaded_at")),
        object_storage_path=_optional_str(row.get("object_storage_path")),
        preprocess_artifact=_json_loads(row.get("preprocess_artifact")) or None,
        content_type=_optional_str(row.get("content_type")),
        file_size_bytes=_optional_int(row.get("file_size_bytes")),
        content_sha256=_optional_str(row.get("content_sha256")),
        duplicate_of_document_id=_optional_str(row.get("duplicate_of_document_id")),
        tenant_id_hash=_optional_str(row.get("tenant_id_hash")),
        category_name=_optional_str(row.get("category_name")),
        indexed_at=_optional_datetime(row.get("indexed_at")),
        extraction=_json_loads(row.get("extraction")),
        error_message=_optional_str(row.get("error_message")),
        classification=_json_loads(row.get("classification")) or None,
        superseded_by_document_id=_optional_str(row.get("superseded_by_document_id")),
        superseded_at=_optional_datetime(row.get("superseded_at")),
    )


def _stored_knowledge_base_from_row(row: Mapping[str, object]) -> StoredKnowledgeBase:
    return StoredKnowledgeBase(
        id=str(row["knowledge_base_id"]),
        tenant_id_hash=_optional_str(row.get("tenant_id_hash")),
        name=str(row["name"]),
        description=_optional_str(row.get("description")),
        status=_knowledge_base_status(row.get("status")),
        default_search_mode=_search_mode(row.get("default_search_mode")),
        retrieval_config=_json_loads(row.get("retrieval_config")),
        created_at=_datetime_value(row.get("created_at")),
        updated_at=_datetime_value(row.get("updated_at")),
        archived_at=_optional_datetime(row.get("archived_at")),
        document_count=_int_value(row.get("document_count")),
        indexed_document_count=_int_value(row.get("indexed_document_count")),
        error_document_count=_int_value(row.get("error_document_count")),
        searchable_chunk_count=_int_value(row.get("searchable_chunk_count")),
    )


def _stored_search_answer_profile_from_row(row: Mapping[str, object]) -> StoredSearchAnswerProfile:
    return StoredSearchAnswerProfile(
        id=str(row["search_answer_profile_id"]),
        tenant_id_hash=_optional_str(row.get("tenant_id_hash")),
        name=str(row["name"]),
        description=_optional_str(row.get("description")),
        status=_search_answer_profile_status(row.get("status")),
        profile_config=_json_loads(row.get("profile_config")),
        created_at=_datetime_value(row.get("created_at")),
        updated_at=_datetime_value(row.get("updated_at")),
        archived_at=_optional_datetime(row.get("archived_at")),
    )


def _stored_conversation_from_row(row: Mapping[str, object]) -> StoredConversation:
    return StoredConversation(
        id=str(row["conversation_id"]),
        search_answer_profile_id=str(row["search_answer_profile_id"]),
        tenant_id_hash=_optional_str(row.get("tenant_id_hash")),
        user_id_hash=_optional_str(row.get("user_id_hash")),
        title=_optional_str(row.get("title")),
        status=str(row.get("status") or "ACTIVE"),
        message_count=_int_value(row.get("message_count")),
        created_at=_datetime_value(row.get("created_at")),
        updated_at=_datetime_value(row.get("updated_at")),
    )


def _json_object_or_none(value: object) -> dict[str, Any] | None:
    """JSON の object の列（CLOB の文字列か、読み込み済みの dict）。無い・壊れた値は None。"""
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value))
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _stored_message_from_row(row: Mapping[str, object]) -> StoredMessage:
    return StoredMessage(
        id=str(row["message_id"]),
        conversation_id=str(row["conversation_id"]),
        reply_to_message_id=_optional_str(row.get("reply_to_message_id")),
        tenant_id_hash=_optional_str(row.get("tenant_id_hash")),
        user_id_hash=_optional_str(row.get("user_id_hash")),
        role=str(row.get("role") or "USER"),
        model=_optional_str(row.get("model")),
        content=_optional_str(row.get("content")) or "",
        citations=_json_object_list(row.get("citations_json")),
        trace_id=_optional_str(row.get("trace_id")),
        status=str(row.get("status") or "COMPLETE"),
        elapsed_ms=_optional_float(row.get("elapsed_ms")),
        created_at=_datetime_value(row.get("created_at")),
        progress=_json_object_or_none(row.get("progress_json")),
        lease_owner=_optional_str(row.get("lease_owner")),
        clarification=_json_object_or_none(row.get("clarification_json")),
        answer_notice=_optional_str(row.get("answer_notice")),
        attachments=(
            _json_object_list(row.get("attachments_json"))
            if row.get("attachments_json") is not None
            else None
        ),
        heartbeat_at=(
            _datetime_value(row.get("heartbeat_at"))
            if row.get("heartbeat_at") is not None
            else None
        ),
        passage_record=_message_passage_record(row),
        answer_attached_images=max(0, _optional_int(row.get("answer_attached_images")) or 0),
    )


_MESSAGE_PASSAGE_COLUMNS = (
    ("passage_marks", "passage_marks_json"),
    ("removed_passages", "removed_passages_json"),
    ("answer_warnings", "answer_warnings_json"),
)


def _message_passage_record(row: Mapping[str, object]) -> dict[str, Any] | None:
    """回答の記録から読んだ段落の印の 3 つ（#2738）。どれも無ければ None。"""
    values: dict[str, Any] = {}
    for key, column in _MESSAGE_PASSAGE_COLUMNS:
        raw = row.get(column)
        if isinstance(raw, str | bytes):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = None
        if isinstance(raw, list):
            values[key] = raw
    return values or None


def _ingestion_job_from_row(row: Mapping[str, object]) -> IngestionJob:
    return IngestionJob(
        id=str(row["job_id"]),
        document_id=str(row["document_id"]),
        document_file_name=_optional_str(row.get("document_file_name")),
        recipe_id=_optional_str(row.get("recipe_id")),
        recipe_revision=_optional_int(row.get("recipe_revision")),
        language_pack_release_id=_optional_str(row.get("language_pack_release_id")),
        status=_ingestion_job_status(row.get("status")),
        phase=_ingestion_job_phase(row.get("phase")),
        parser_profile=str(row.get("parser_profile") or "enterprise_ai_generic"),
        quality_warnings=_json_string_list(row.get("quality_warnings")),
        settings_overrides=_json_loads(row.get("settings_overrides")) or None,
        skip_reason=_optional_str(row.get("skip_reason")),
        error_message=_optional_str(row.get("error_message")),
        attempt_count=_int_value(row.get("attempt_count")),
        max_attempts=max(1, _int_value(row.get("max_attempts")) or 3),
        queued_at=_datetime_value(row.get("queued_at")),
        started_at=_optional_datetime(row.get("started_at")),
        finished_at=_optional_datetime(row.get("finished_at")),
        progress=_ingestion_job_progress(row.get("progress_json")),
    )


def _ingestion_job_progress(value: object) -> IngestionJobProgress | None:
    """job の行の進捗（#1999）。無い・読めない値は None（画面は進捗を出さない）。"""
    payload = _json_loads(value)
    if not payload:
        return None
    try:
        return IngestionJobProgress.model_validate(payload)
    except ValidationError:
        return None


def _ingestion_segment_from_row(row: Mapping[str, object]) -> IngestionSegment:
    return IngestionSegment(
        segment_id=str(row["segment_id"]),
        document_id=str(row["document_id"]),
        recipe_id=_optional_str(row.get("recipe_id")),
        status=str(row.get("status") or "QUEUED"),
        parser_backend=str(row.get("parser_backend") or "enterprise_ai"),
        parser_profile=str(row.get("parser_profile") or "enterprise_ai_generic"),
        page_start=_optional_int(row.get("page_start")),
        page_end=_optional_int(row.get("page_end")),
        attempt_count=_int_value(row.get("attempt_count")),
        artifact_path=_optional_str(row.get("artifact_path")),
        error_code=_optional_str(row.get("error_code")),
        error_message=_optional_str(row.get("error_message")),
    )


def _document_recipe_row(row: Mapping[str, object]) -> dict[str, object]:
    """Oracle のレシピ行を API 組み立て用の小文字 dict へ正規化する。"""
    normalized = {str(key).lower(): value for key, value in row.items()}
    return {
        "recipe_id": str(normalized["recipe_id"]),
        "document_id": str(normalized["document_id"]),
        "slot_no": _int_value(normalized.get("slot_no")),
        "processing_config": _json_loads(normalized.get("processing_config")),
        "status": str(normalized.get("status") or FileStatus.UPLOADED.value),
        "failed_phase": _optional_str(normalized.get("failed_phase")),
        "preprocess_artifact": _json_loads(normalized.get("preprocess_artifact")) or None,
        "active_extraction_recipe_id": _optional_str(normalized.get("active_extraction_recipe_id")),
        "active_chunk_set_id": _optional_str(normalized.get("active_chunk_set_id")),
        "chunk_count": _int_value(normalized.get("chunk_count")),
        "vector_count": _int_value(normalized.get("vector_count")),
        "chunk_set_status": _optional_str(normalized.get("chunk_set_status")),
        "pending_chunk_set_id": _optional_str(normalized.get("pending_chunk_set_id")),
        "config_revision": max(1, _int_value(normalized.get("config_revision")) or 1),
        "materialized_revision": _optional_int(normalized.get("materialized_revision")),
        "error_message": _optional_str(normalized.get("error_message")),
        "started_at": _optional_datetime(normalized.get("started_at")),
        "finished_at": _optional_datetime(normalized.get("finished_at")),
        "created_at": _datetime_value(normalized.get("created_at")),
        "updated_at": _datetime_value(normalized.get("updated_at")),
    }


def _distinct_text_hits(
    rows: Sequence[Mapping[str, object]],
) -> list[tuple[RetrievedChunk, Mapping[str, object]]]:
    """検索の候補の行を chunk にし、本文が同じ chunk を 1 件にまとめる(#2147)。

    同じ内容の文書を 2 回取り込む(整形だけが違うファイルなど)と、本文が同じ chunk が別々の候補に
    なって候補の枠を占める。位置は最初に出た順位のまま、代表は chunk_id の最小のもの(vector と
    keyword の候補で同じ代表になり、融合で 1 件に数える)。本文の無い chunk はまとめない。
    """
    hits = [(_retrieved_chunk_from_row(row), row) for row in rows]
    representative: dict[str, tuple[RetrievedChunk, Mapping[str, object]]] = {}
    for hit in hits:
        text = hit[0].text
        if text and (
            text not in representative or hit[0].chunk_id < representative[text][0].chunk_id
        ):
            representative[text] = hit
    distinct: list[tuple[RetrievedChunk, Mapping[str, object]]] = []
    seen: set[str] = set()
    for hit in hits:
        text = hit[0].text
        if not text:
            distinct.append(hit)
        elif text not in seen:
            seen.add(text)
            distinct.append(representative[text])
    return distinct


def _retrieved_chunk_from_row(row: Mapping[str, object]) -> RetrievedChunk:
    metadata = _metadata_from_json(row.get("metadata_json"))
    metadata.setdefault("document_id", str(row["document_id"]))
    chunk_set_id = _optional_str(row.get("chunk_set_id"))
    recipe_id = _optional_str(row.get("recipe_id"))
    recipe_slot_no = _optional_int(row.get("recipe_slot_no"))
    if chunk_set_id is not None:
        metadata["chunk_set_id"] = chunk_set_id
    if recipe_id is not None:
        metadata["recipe_id"] = recipe_id
    if recipe_slot_no is not None:
        metadata["recipe_slot_no"] = recipe_slot_no
    metadata.setdefault("chunk_id", str(row["chunk_id"]))
    chunk_index = row.get("chunk_index")
    if "chunk_index" not in metadata and chunk_index is not None:
        metadata["chunk_index"] = _int_value(chunk_index)
    if superseded_by := _optional_str(row.get("superseded_by_document_id")):
        # 新しい版に置き換えた文書(旧版)の chunk(#1248)。``include_superseded`` で含めたときや
        # 根拠の本文を読むときだけ出る。回答の出典の文書名に「(旧版)」を付けるのに使う。
        metadata["document_superseded"] = True
        metadata["superseded_by_document_id"] = superseded_by
    return RetrievedChunk(
        document_id=str(row["document_id"]),
        chunk_id=str(row["chunk_id"]),
        text=str(row["chunk_text"]),
        score=round(_float_value(row.get("score", 0.0)), 6),
        file_name=_optional_str(row.get("file_name")),
        category_name=_optional_str(row.get("category_name")),
        metadata=metadata,
    )


def _chunk_search_text(chunk: Chunk) -> str:
    """Oracle Text には文脈ヘッダを含め、表示本文は chunk.text のまま保つ。

    索引する文字列は、質問の側と同じ文字の形（NFKC・波線・ダッシュ）にそろえる（#1336）。
    WORLD_LEXER は全角 / 半角の違いは同一視するが、互換文字（``①`` ``Ⅴ`` ``㈱`` ``㌔`` ``℃``）は
    同一視しない。質問の側はこれらを NFKC で ``1`` ``V`` などにするため、索引の側がそのままだと
    当たらない。
    表示の本文（``chunk_text``）・親の本文・引用の原文は正規化しない（原文の照合と位置がずれるため）。
    """
    if search_text := engine_search_text(chunk.metadata):
        # 親子階層の chunk は、文書・節・親要約・表/図文脈を前置した rag_poc の search_text を
        # 索引する。
        return _text_search_index_text(search_text)
    header = str(chunk.metadata.get("context_header") or "").strip()
    return _text_search_index_text(f"{header}\n{chunk.text}" if header else chunk.text)


def _text_search_index_text(text: str) -> str:
    """Oracle Text の索引に入れる文字列を、質問の語と同じ形に正規化する（#1336）。"""
    from rag_engine.retrieval.text_search_tokenizer import normalize_text_search_index_text

    return normalize_text_search_index_text(text)


def _document_chunk_view_from_row(row: Mapping[str, object]) -> DocumentChunkView:
    """rag_chunks row を UI 用 chunk view へ変換する。"""
    metadata = _metadata_from_json(row.get("metadata_json"))
    metadata.setdefault("document_id", str(row["document_id"]))
    metadata.setdefault("chunk_id", str(row["chunk_id"]))
    chunk_index = _optional_metadata_int(metadata.get("chunk_index"))
    if chunk_index is None:
        chunk_index = _int_value(row.get("chunk_index"))
    return DocumentChunkView(
        document_id=str(row["document_id"]),
        chunk_id=str(row["chunk_id"]),
        chunk_index=chunk_index,
        text=str(row["chunk_text"]),
        page_start=_optional_metadata_int(metadata.get("page_start")),
        page_end=_optional_metadata_int(metadata.get("page_end")),
        bbox=_bbox_from_metadata(metadata.get("bbox")),
        section_path=_metadata_str(metadata.get("section_path")),
        content_kind=_metadata_str(metadata.get("content_kind")),
        chunk_group_id=_metadata_str(metadata.get("chunk_group_id")),
        source_parser=_metadata_str(metadata.get("source_parser")),
        element_ids=_element_ids_from_metadata(metadata.get("element_ids")),
        metadata=metadata,
    )


def _metadata_from_json(value: object) -> dict[str, MetadataValue]:
    decoded = _json_loads(value)
    return {str(key): _coerce_metadata_value(item) for key, item in decoded.items()}


def _coerce_metadata_value(item: object) -> MetadataValue:
    """JSON 由来の値を MetadataValue へ正規化する。

    Oracle の JSON 列は数値を Decimal で返すため、int/float に戻す。
    list/dict は citation lineage として構造を保ったまま返す。
    """
    if item is None or isinstance(item, bool):
        return item
    if isinstance(item, Decimal):
        return int(item) if item == item.to_integral_value() else float(item)
    if isinstance(item, str | int | float):
        return item
    if isinstance(item, Mapping):
        return {str(key): _coerce_metadata_value(value) for key, value in item.items()}
    if isinstance(item, Sequence) and not isinstance(item, str | bytes | bytearray):
        return [_coerce_metadata_value(value) for value in item]
    return str(item)


def _optional_metadata_int(value: object) -> int | None:
    """metadata scalar から optional int を読む。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _metadata_str(value: object) -> str | None:
    """metadata scalar から空でない文字列を読む。"""
    if isinstance(value, str | int | float):
        cleaned = str(value).strip()
        return cleaned or None
    return None


def _element_ids_from_metadata(value: object) -> list[str]:
    """chunk metadata の element_ids を list にする。"""
    if isinstance(value, list):
        return [
            str(item).strip() for item in value if isinstance(item, str | int) and str(item).strip()
        ]
    text = _metadata_str(value)
    if not text:
        return []
    if text.startswith("["):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, list):
            return [
                str(item).strip()
                for item in decoded
                if isinstance(item, str | int) and str(item).strip()
            ]
    return [part.strip() for part in text.split(",") if part.strip()]


def _bbox_from_metadata(value: object) -> list[float] | None:
    """chunk metadata の bbox JSON 文字列 / 配列を list[float] に戻す。"""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
    else:
        decoded = value
    if not isinstance(decoded, list) or len(decoded) not in {4, 8}:
        return None
    bbox: list[float] = []
    for item in decoded:
        if isinstance(item, bool) or not isinstance(item, int | float):
            return None
        bbox.append(float(item))
    return bbox


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_dumps(value: object) -> str:
    """JSON 列へ書く値を JSON の文字列にする。NaN / ±Infinity は ``null`` にする(#2043)。

    ``json.dumps`` の既定は NaN を ``NaN`` と書き、JSON の規格に合わないため Oracle の JSON 列が
    ORA-40441 で拒む。ふだんは ``allow_nan=False`` の 1 回で書き、失敗したときだけ値を直す。
    """
    try:
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), default=_json_default, allow_nan=False
        )
    except ValueError:
        paths: list[str] = []
        cleaned = _without_non_finite(value, "$", paths)
        logger.warning(
            "json_non_finite_replaced",
            extra={"json_paths": paths[:20], "json_path_count": len(paths)},
        )
        return json.dumps(
            cleaned,
            ensure_ascii=False,
            separators=(",", ":"),
            default=_json_default,
            allow_nan=False,
        )


def _without_non_finite(value: object, path: str, paths: list[str]) -> object:
    """NaN / ±Infinity の float を None にした値を返す(直した場所の path を ``paths`` に足す)。"""
    if isinstance(value, float) and not math.isfinite(value):
        paths.append(path)
        return None
    if isinstance(value, Decimal) and not value.is_finite():
        paths.append(path)
        return None
    if isinstance(value, Mapping):
        return {
            key: _without_non_finite(item, f"{path}.{key}", paths) for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [
            _without_non_finite(item, f"{path}[{index}]", paths) for index, item in enumerate(value)
        ]
    return value


def _json_bind(value: object | None) -> str | None:
    """JSON 型の列へ書く値を、JSON の文字列にする(``_json_input_sizes`` の CLOB で bind する)。

    OSON(``DB_TYPE_JSON``)で bind すると、python-oracledb(thin)の OSON の符号化が数百 KB を
    超える値で極端に遅く(800KB で約 48 秒)、GIL を持ったまま backend 全体を止める(#1363)。
    JSON の文字列を CLOB で渡し、DB 側で JSON 型にする(読み出しの形は変わらない)。
    """
    if value is None:
        return None
    return _json_dumps(value)


def _knowledge_base_extraction_fields(
    row: Mapping[str, object] | None,
) -> list[FieldDefinition] | None:
    """`rag_knowledge_bases.extraction_fields` の値を読む(NULL と行なしは None。#548)。"""
    return field_schema_from_json(row.get("extraction_fields") if row is not None else None)


def _json_input_sizes(*names: str) -> dict[str, object]:
    """``_json_bind`` の値の bind 型。32KB を超える文字列も渡せるよう CLOB にする(#1363)。

    OSON(``DB_TYPE_JSON``)は使わない(``_json_bind`` の説明)。``COALESCE`` など JSON 型の列と
    型をそろえる式では、SQL の側で ``JSON(:name)`` にする。
    """
    oracledb = importlib.import_module("oracledb")
    return {name: oracledb.DB_TYPE_CLOB for name in names}


def _json_loads(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    text = str(value).strip()
    if not text:
        return {}
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if isinstance(decoded, Mapping):
        return {str(key): item for key, item in decoded.items()}
    return {}


def _json_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [str(item) for item in value]
    text = str(value).strip()
    if not text:
        return []
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(decoded, Sequence) and not isinstance(decoded, str | bytes | bytearray):
        return [str(item) for item in decoded]
    return []


def _json_string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [str(item) for item in value]
    text = str(value).strip()
    if not text:
        return []
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(decoded, Sequence) and not isinstance(decoded, str | bytes | bytearray):
        return [str(item) for item in decoded]
    return []


def _row_count_value(row: Mapping[str, object] | None) -> int:
    if row is None:
        return 0
    value = row.get("count_value")
    return _int_value(value)


def _file_status(value: object) -> FileStatus:
    if isinstance(value, FileStatus):
        return value
    return FileStatus(str(value))


def _ingestion_job_status(value: object) -> IngestionJobStatus:
    if isinstance(value, IngestionJobStatus):
        return value
    return IngestionJobStatus(str(value or IngestionJobStatus.QUEUED.value))


def _ingestion_job_phase(value: object) -> IngestionJobPhase:
    if isinstance(value, IngestionJobPhase):
        return value
    return IngestionJobPhase(str(value or IngestionJobPhase.PREPROCESS.value))


def _restore_status_for_job_phase(phase: IngestionJobPhase) -> FileStatus:
    if phase == IngestionJobPhase.INDEX:
        return FileStatus.CHUNKED
    if phase == IngestionJobPhase.CHUNK:
        return FileStatus.REVIEW
    return FileStatus.UPLOADED


def _restore_recipe_status_for_job_phase(phase: IngestionJobPhase) -> FileStatus:
    if phase == IngestionJobPhase.INDEX:
        return FileStatus.CHUNKED
    if phase == IngestionJobPhase.CHUNK:
        return FileStatus.REVIEW
    if phase == IngestionJobPhase.EXTRACT:
        return FileStatus.PREPROCESSED
    return FileStatus.UPLOADED


def _failed_phase_for_recipe_status(value: object) -> IngestionJobPhase:
    return {
        FileStatus.PREPROCESSING.value: IngestionJobPhase.PREPROCESS,
        FileStatus.INGESTING.value: IngestionJobPhase.EXTRACT,
        FileStatus.CHUNKING.value: IngestionJobPhase.CHUNK,
        FileStatus.INDEXING.value: IngestionJobPhase.INDEX,
    }.get(str(value), IngestionJobPhase.PREPROCESS)


def _knowledge_base_status(value: object) -> KnowledgeBaseStatus:
    if isinstance(value, KnowledgeBaseStatus):
        return value
    return KnowledgeBaseStatus(str(value or KnowledgeBaseStatus.ACTIVE.value))


def _search_answer_profile_status(value: object) -> SearchAnswerProfileStatus:
    if isinstance(value, SearchAnswerProfileStatus):
        return value
    return SearchAnswerProfileStatus(str(value or SearchAnswerProfileStatus.ACTIVE.value))


def _search_mode(value: object) -> SearchMode:
    if isinstance(value, SearchMode):
        return value
    return SearchMode(str(value or SearchMode.HYBRID.value))


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return _int_value(value)


def _datetime_value(value: object) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is not None and value.utcoffset() is not None:
            return value
        return value.replace(tzinfo=UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            return parsed
        return parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    return _datetime_value(value)


def _float_value(value: object) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, int | float | Decimal):
        return float(value)
    if isinstance(value, str | bytes | bytearray):
        return float(value)
    raise ValueError(f"数値に変換できない値です: {value!r}")


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return _float_value(value)


def _json_object_list(value: object) -> list[dict[str, object]]:
    """JSON 列を dict のリストとして読み出す(citations 等)。非 dict 要素は除外する。"""
    if value is None:
        return []
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [
            {str(key): item for key, item in element.items()}
            for element in value
            if isinstance(element, Mapping)
        ]
    text = str(value).strip()
    if not text:
        return []
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(decoded, list):
        return [
            {str(key): item for key, item in element.items()}
            for element in decoded
            if isinstance(element, Mapping)
        ]
    return []


def _int_value(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float | Decimal):
        return int(value)
    if isinstance(value, str | bytes | bytearray):
        return int(value)
    raise ValueError(f"整数に変換できない値です: {value!r}")


def _bounded_int_literal(value: int, *, name: str, minimum: int, maximum: int) -> str:
    """SQL grammar position に使う整数 literal を範囲検証して返す。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} は整数で指定してください。")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} は {minimum} から {maximum} の範囲で指定してください。")
    return str(value)


def warm_oracle_pool(settings: Settings | None = None) -> bool:
    """backend の API の起動時に共有 pool を作り、接続を ``RAG_ORACLE_POOL_MIN`` 本開く(#1498)。

    起動直後の問で接続の作成(1 本 0.5〜1 秒)を待たないため。このプロセスで作り直す pool も同じ
    min にする。python-oracledb の Thin mode は min までの接続を pool の中のスレッドで開くため、
    ここでは待たない(Thick mode は作る間だけ待つ。呼び出し側はスレッドで呼ぶ)。接続の設定が
    無ければ何もせず False を返す。
    """
    global _SHARED_ORACLE_POOL_MIN
    settings = settings or get_settings()
    client = OracleClient(settings)
    if not client.is_connection_configured():
        return False
    _SHARED_ORACLE_POOL_MIN = max(1, min(settings.oracle_pool_min, settings.oracle_pool_max))
    client.connection_pool()
    return True


def close_oracle_pool() -> None:
    """共有 Oracle pool を閉じる。アプリ終了時・DB 設定の保存時に呼び出す。"""
    global _SHARED_ORACLE_POOL, _SHARED_ORACLE_POOL_KEY
    with _SHARED_ORACLE_POOL_LOCK:
        pools = [*_RETIRED_ORACLE_POOLS]
        _RETIRED_ORACLE_POOLS.clear()
        pool = _SHARED_ORACLE_POOL
        _SHARED_ORACLE_POOL = None
        _SHARED_ORACLE_POOL_KEY = None
    for retired in pools:
        with suppress(Exception):
            retired.close(force=True)
    if pool is not None:
        pool.close(force=True)


def _shared_pool_settings_key(settings: Settings) -> str:
    """共有 pool の接続の設定の指紋（資格情報を含むため、生値は保持・出力しない。#2102）。

    `get_settings()` が `.env` を読み直して接続先・資格情報・Wallet が変わったら、pool を
    作り直す。接続引数（`shared_pool_connect_kwargs`）は Wallet のファイルを見るので、借りるたびに
    作らず、元の設定の値で比べる。
    """
    payload = json.dumps(
        [
            settings.oracle_user,
            settings.oracle_password,
            settings.oracle_dsn,
            settings.resolved_oracle_wallet_dir,
            settings.oracle_wallet_password,
            settings.oracle_client_lib_dir,
            getattr(settings, "oracle_tcp_connect_timeout_seconds", None),
            settings.oracle_pool_max,
        ],
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _retire_shared_pool(pool: OraclePoolProtocol) -> None:
    """設定が変わって使わなくなった共有 pool を閉じる（貸し出し中なら終了時に閉じる。#2102）。"""

    def close() -> None:
        try:
            pool.close()
        except Exception:  # noqa: BLE001 - 貸し出し中。終了時に閉じる
            with _SHARED_ORACLE_POOL_LOCK:
                _RETIRED_ORACLE_POOLS.append(pool)

    _ORACLE_POOL_CLOSER(close)
    logger.info("oracle_pool_settings_changed", extra={"pool": "rag"})


def _discard_shared_pool(pool: OraclePoolProtocol, *, reason: str, exc: BaseException) -> None:
    """壊れた共有 pool を捨てる（次に借りるときに作り直す。#820）。

    閉じるのは別のスレッド。貸し出し中の接続があって閉じられなければ、その接続を使っている処理は
    止めずに終了時に閉じる。
    """
    global _SHARED_ORACLE_POOL
    with _SHARED_ORACLE_POOL_LOCK:
        if _SHARED_ORACLE_POOL is not pool:
            return  # 別のスレッドが入れ替え済み
        _SHARED_ORACLE_POOL = None

    def close() -> None:
        try:
            pool.close()
        except Exception:  # noqa: BLE001 - 貸し出し中。終了時に閉じる
            with _SHARED_ORACLE_POOL_LOCK:
                _RETIRED_ORACLE_POOLS.append(pool)

    # `pool.close()` は試している接続が終わるまで戻らないことがあるため、要求と lock を止めない。
    _ORACLE_POOL_CLOSER(close)
    logger.warning(
        "oracle_pool_discarded",
        extra={"pool": "rag", "reason": reason, **oracle_error_log_fields(exc)},
    )


async def test_oracle_connection(
    settings: Settings | None = None,
    db_call_runner: DbCallRunner | None = None,
) -> None:
    """Oracle へ 1 回だけ接続し、最小クエリで疎通を確認する。"""
    effective_settings = settings or get_settings()
    runner = db_call_runner or _run_db_test_call_in_thread
    timeout_seconds = float(getattr(effective_settings, "oracle_db_test_timeout_seconds", 15.0))
    try:
        await asyncio.wait_for(
            runner(lambda: _test_oracle_connection_sync(effective_settings)),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        raise OracleConnectionTimeoutError(
            f"Oracle AI Database の接続テストが {timeout_seconds:g} 秒でタイムアウトしました。"
            "データベースの起動状態、Wallet サービス名、ネットワーク到達性を確認してください。"
        ) from exc


def _test_oracle_connection_sync(settings: Settings) -> None:
    """同期 SDK で Oracle 接続を検証する。"""
    oracledb = importlib.import_module("oracledb")
    _init_oracle_client(oracledb, settings)
    connect_kwargs = _oracle_connect_kwargs(
        settings,
        extra={
            "dsn": _oracle_connection_test_dsn(settings),
            "retry_count": 0,
            "retry_delay": 0,
        },
    )

    connection = oracledb.connect(**connect_kwargs)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT 1 FROM DUAL")
            cursor.fetchone()
        finally:
            cursor.close()
    finally:
        connection.close()


def shared_pool_connect_kwargs(settings: Settings | None = None) -> dict[str, object]:
    """共有 pool の接続引数（pool の引数を除く）。接続の失敗の診断の直接の接続も使う（#1744）。

    常駐の server は設定の DSN のまま（Wallet の別名の TNS の再試行を持ち、一時の切断に強い）。
    CLI（`enable_cli_connect_mode` を呼んだプロセス）は、TNS の再試行を外し、短い
    `tcp_connect_timeout` にする（DB の停止中に 1〜2 分待たない。#1779）。
    資格情報を含むため、ログ・応答に出さない。
    """
    return apply_cli_connect_mode(_oracle_connect_kwargs(settings or get_settings()))


def _oracle_connect_kwargs(
    settings: Settings,
    *,
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """python-oracledb connect/create_pool に渡す共通 kwargs を作る。"""
    kwargs: dict[str, object] = {
        "user": settings.oracle_user,
        "dsn": settings.oracle_dsn,
    }
    if extra:
        kwargs.update(extra)
    tcp_connect_timeout = float(getattr(settings, "oracle_tcp_connect_timeout_seconds", 10.0))
    if tcp_connect_timeout > 0:
        kwargs["tcp_connect_timeout"] = tcp_connect_timeout
    if settings.oracle_password.strip():
        kwargs["password"] = settings.oracle_password
    _add_wallet_kwargs(settings, kwargs)
    return kwargs


def _init_oracle_client(oracledb: Any, settings: Settings) -> None:
    """PLATFORM_ORACLE_CLIENT_LIB_DIR があるときは nl2sql と同じ thick client を使う。"""
    global _ORACLE_CLIENT_INITIALIZED_LIB_DIR
    lib_dir = settings.oracle_client_lib_dir.strip()
    if not lib_dir or lib_dir == _ORACLE_CLIENT_INITIALIZED_LIB_DIR:
        return
    init_oracle_client = getattr(oracledb, "init_oracle_client", None)
    if not callable(init_oracle_client):
        return
    init_oracle_client(lib_dir=lib_dir)
    _ORACLE_CLIENT_INITIALIZED_LIB_DIR = lib_dir


def _oracle_connection_configured(client: OracleClient) -> bool:
    """実 DB に接続する設定または明示 pool があるかを返す。"""
    settings = client._settings
    return (
        client._pool_instance is not None
        or bool(settings.oracle_user.strip() and settings.oracle_dsn.strip())
        or _SHARED_ORACLE_POOL is not None
    )


def _oracle_connection_test_dsn(settings: Settings) -> str:
    """接続テストでは Wallet alias の長い retry 設定を外した descriptor を使う（#1214）。"""
    return dsn_without_tns_retry(
        settings.oracle_dsn, settings.resolved_oracle_wallet_dir.strip() or None
    )


def _add_wallet_kwargs(settings: Settings, kwargs: dict[str, object]) -> None:
    """Wallet 設定を kwargs に追加する。パスワード要求プロンプトは事前に防ぐ。"""
    wallet_dir = settings.resolved_oracle_wallet_dir.strip()
    if not wallet_dir:
        return

    wallet_path = Path(wallet_dir).expanduser()
    if not wallet_path.is_dir():
        return

    wallet_password = _oracle_wallet_password(settings)
    if not wallet_password and _wallet_requires_password(wallet_path):
        raise OracleWalletPasswordRequiredError(WALLET_PASSWORD_REQUIRED_ERROR)

    resolved_wallet_path = str(wallet_path)
    kwargs["config_dir"] = resolved_wallet_path
    kwargs["wallet_location"] = resolved_wallet_path
    if wallet_password:
        kwargs["wallet_password"] = wallet_password


def _oracle_wallet_password(settings: Settings) -> str:
    """Wallet password は専用値がなければ DB password を使う。"""
    return settings.oracle_wallet_password.strip() or settings.oracle_password.strip()


def _wallet_requires_password(wallet_path: Path) -> bool:
    """自動ログイン Wallet がなく、秘密鍵が暗号化されていればパスワード必須。"""
    try:
        files = [path for path in wallet_path.iterdir() if path.is_file()]
    except OSError:
        return False
    names = {path.name.lower() for path in files}
    if "ewallet.p12" in names:
        return True
    encrypted_pem_exists = any(
        path.suffix.lower() == ".pem" and _pem_file_is_encrypted(path) for path in files
    )
    if encrypted_pem_exists:
        return True
    return "cwallet.sso" not in names


def _pem_file_is_encrypted(path: Path) -> bool:
    """暗号化 PEM の代表的な marker だけを少量読み取って判定する。"""
    try:
        head = path.read_bytes()[:4096]
    except OSError:
        return False
    text = head.decode("utf-8", errors="ignore").upper()
    return "BEGIN ENCRYPTED PRIVATE KEY" in text or "PROC-TYPE: 4,ENCRYPTED" in text


def oracle_search_answer_profile_knowledge_schema_sql(
    table_name: str = "rag_search_answer_profile_knowledge",
) -> str:
    """検索・回答プロファイル単位の知識(ドメインキーワード / Approved FAQ / 用語・同義語 /
    回答ルール)の DDL。

    rag_poc の JSON payload 形式をそのまま 1 行 1 種別で保持する。
    """

    return f"""
CREATE TABLE {table_name} (
    search_answer_profile_id  VARCHAR2(64) NOT NULL,
    kind              VARCHAR2(32) NOT NULL,
    payload_json      JSON NOT NULL,
    revision          NUMBER(19) DEFAULT 1 NOT NULL,
    updated_at        TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT {table_name}_pk PRIMARY KEY (search_answer_profile_id, kind),
    CONSTRAINT {table_name}_kind_ck CHECK (
        kind IN ('domain_keywords', 'approved_faq', 'runtime_knowledge')
    )
);
""".strip()


def oracle_document_sections_schema_sql(
    table_name: str = "rag_document_sections",
) -> str:
    """人が修正した文書の章節の DDL(#713)。

    文書ごとに 1 行。章節は「順序 + 階層」の平らな一覧の JSON で、すべての処理レシピで共有する。
    文書を消したら一緒に消す。
    """

    return f"""
CREATE TABLE {table_name} (
    document_id    VARCHAR2(64) NOT NULL,
    sections_json  JSON NOT NULL,
    revision       NUMBER(19) DEFAULT 1 NOT NULL,
    updated_at     TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT {table_name}_pk PRIMARY KEY (document_id),
    CONSTRAINT {table_name}_document_fk FOREIGN KEY (document_id)
        REFERENCES rag_documents (document_id) ON DELETE CASCADE
);
""".strip()


def oracle_answer_record_schema_sql(
    table_name: str = "rag_answer_records",
) -> str:
    """回答の保存 table DDL(rag_poc の answer JSON 保存に相当)。

    `user_id_hash` は回答を生成した利用者（持ち主。#304）。一覧・詳細・評価・削除を持ち主へ絞る。
    """

    return f"""
CREATE TABLE {table_name} (
    trace_id            VARCHAR2(64) PRIMARY KEY,
    search_answer_profile_id    VARCHAR2(64),
    surface             VARCHAR2(16) NOT NULL,
    answer_engine       VARCHAR2(32) NOT NULL,
    question            CLOB NOT NULL,
    rewritten_question  CLOB,
    answer              CLOB NOT NULL,
    citations_json      JSON NOT NULL,
    diagnostics_json    JSON NOT NULL,
    evaluation_input_json JSON,
    evaluation_json     JSON,
    answer_notice       CLOB,
    user_id_hash        CHAR(64),
    created_at          TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT {table_name}_surface_ck CHECK (surface IN ('search', 'chat'))
);

CREATE INDEX {table_name}_profile_idx
    ON {table_name} (search_answer_profile_id, created_at DESC);

CREATE INDEX {table_name}_owner_idx
    ON {table_name} (user_id_hash, search_answer_profile_id, created_at DESC);
""".strip()


def oracle_answer_prompt_schema_sql(table_name: str = "rag_answer_prompts") -> str:
    """編集した回答生成のプロンプトの保存 table DDL(rag_poc の prompts/<key>.txt に相当)。"""
    return f"""
CREATE TABLE {table_name} (
    prompt_key  VARCHAR2(64) PRIMARY KEY,
    content     CLOB NOT NULL,
    updated_at  TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
)
""".strip()


def oracle_knowledge_base_schema_sql(
    knowledge_base_table: str = "rag_knowledge_bases",
    membership_table: str = "rag_document_knowledge_bases",
    document_table: str = "rag_documents",
) -> str:
    """Oracle knowledge base / document membership table の DDL 例を返す。"""
    return f"""
CREATE TABLE {knowledge_base_table} (
    knowledge_base_id     VARCHAR2(64) PRIMARY KEY,
    tenant_id_hash        CHAR(64),
    name                  VARCHAR2(256) NOT NULL,
    description           VARCHAR2(2000),
    status                VARCHAR2(32) DEFAULT 'ACTIVE' NOT NULL,
    default_search_mode   VARCHAR2(16) DEFAULT 'hybrid' NOT NULL,
    retrieval_config      JSON,
    extraction_fields     JSON,
    created_at            TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at            TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    archived_at           TIMESTAMP WITH TIME ZONE,
    CONSTRAINT {knowledge_base_table}_status_ck
        CHECK (status IN ('ACTIVE', 'ARCHIVED')),
    CONSTRAINT {knowledge_base_table}_mode_ck
        CHECK (default_search_mode IN ('hybrid', 'vector', 'keyword'))
);

CREATE UNIQUE INDEX {knowledge_base_table}_tenant_name_uidx
    ON {knowledge_base_table} (
        NVL(tenant_id_hash, '__GLOBAL__'),
        LOWER(name)
    );

CREATE INDEX {knowledge_base_table}_tenant_status_idx
    ON {knowledge_base_table} (tenant_id_hash, status, updated_at DESC);

CREATE TABLE {membership_table} (
    knowledge_base_id        VARCHAR2(64) NOT NULL,
    document_id              VARCHAR2(64) NOT NULL,
    tenant_id_hash           CHAR(64),
    assigned_at              TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    assigned_by_user_id_hash CHAR(64),
    PRIMARY KEY (knowledge_base_id, document_id),
    CONSTRAINT {membership_table}_kb_fk
        FOREIGN KEY (knowledge_base_id)
        REFERENCES {knowledge_base_table} (knowledge_base_id)
        ON DELETE CASCADE,
    CONSTRAINT {membership_table}_doc_fk
        FOREIGN KEY (document_id)
        REFERENCES {document_table} (document_id)
        ON DELETE CASCADE
);

CREATE INDEX {membership_table}_document_idx
    ON {membership_table} (document_id, knowledge_base_id);

CREATE INDEX {membership_table}_tenant_kb_idx
    ON {membership_table} (tenant_id_hash, knowledge_base_id, assigned_at DESC);
""".strip()


def oracle_search_answer_profile_schema_sql(
    table_name: str = "rag_search_answer_profiles",
) -> str:
    """Oracle search answer profile(検索・回答プロファイル)table の DDL 例を返す。

    参照 KB は ``profile_config`` JSON 内に ID 群として保持する(多対多。link table 不要で
    DDL を最小化する)。検索・回答の上書きも同 JSON へ束ねる。
    """
    return f"""
CREATE TABLE {table_name} (
    search_answer_profile_id   VARCHAR2(64) PRIMARY KEY,
    tenant_id_hash     CHAR(64),
    name               VARCHAR2(256) NOT NULL,
    description        VARCHAR2(2000),
    status             VARCHAR2(32) DEFAULT 'ACTIVE' NOT NULL,
    profile_config        JSON,
    created_at         TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at         TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    archived_at        TIMESTAMP WITH TIME ZONE,
    CONSTRAINT {table_name}_status_ck
        CHECK (status IN ('ACTIVE', 'ARCHIVED'))
);

CREATE UNIQUE INDEX {table_name}_tenant_name_uidx
    ON {table_name} (
        NVL(tenant_id_hash, '__GLOBAL__'),
        LOWER(name)
    );

CREATE INDEX {table_name}_tenant_status_idx
    ON {table_name} (tenant_id_hash, status, updated_at DESC);
""".strip()


def oracle_role_access_schema_sql() -> str:
    """ロールに付ける RAG の権限と対象範囲（検索・回答プロファイル・ナレッジベース）の DDL（#214）。

    ロール本体は platform の共通認証の `PLATFORM_ROLES`（`apply_platform_auth_schema`）。
    `RAG_ROLE_PERMISSIONS` は製品をまたぐ権限昇格の判定（platform）も読む。
    """
    return """
CREATE TABLE rag_role_permissions (
    role_id          VARCHAR2(36) NOT NULL,
    permission_code  VARCHAR2(128) NOT NULL,
    CONSTRAINT rag_role_permissions_pk PRIMARY KEY (role_id, permission_code),
    CONSTRAINT rag_role_permissions_role_fk
        FOREIGN KEY (role_id)
        REFERENCES platform_roles (role_id)
        ON DELETE CASCADE
);

CREATE TABLE rag_role_search_answer_profiles (
    role_id           VARCHAR2(36) NOT NULL,
    search_answer_profile_id  VARCHAR2(64) NOT NULL,
    CONSTRAINT rag_role_search_answer_profiles_pk PRIMARY KEY (role_id, search_answer_profile_id),
    CONSTRAINT rag_role_search_answer_profiles_role_fk
        FOREIGN KEY (role_id)
        REFERENCES platform_roles (role_id)
        ON DELETE CASCADE,
    CONSTRAINT rag_role_search_answer_profiles_view_fk
        FOREIGN KEY (search_answer_profile_id)
        REFERENCES rag_search_answer_profiles (search_answer_profile_id)
        ON DELETE CASCADE
);

CREATE INDEX rag_role_search_answer_profiles_view_idx
    ON rag_role_search_answer_profiles (search_answer_profile_id);

CREATE TABLE rag_role_knowledge_bases (
    role_id            VARCHAR2(36) NOT NULL,
    knowledge_base_id  VARCHAR2(64) NOT NULL,
    CONSTRAINT rag_role_knowledge_bases_pk PRIMARY KEY (role_id, knowledge_base_id),
    CONSTRAINT rag_role_knowledge_bases_role_fk
        FOREIGN KEY (role_id)
        REFERENCES platform_roles (role_id)
        ON DELETE CASCADE,
    CONSTRAINT rag_role_knowledge_bases_kb_fk
        FOREIGN KEY (knowledge_base_id)
        REFERENCES rag_knowledge_bases (knowledge_base_id)
        ON DELETE CASCADE
);

CREATE INDEX rag_role_knowledge_bases_kb_idx
    ON rag_role_knowledge_bases (knowledge_base_id)
""".strip()


def oracle_conversation_schema_sql(
    table_name: str = "rag_conversations",
    search_answer_profile_table: str = "rag_search_answer_profiles",
) -> str:
    """チャット会話(conversation)table の DDL 例を返す。検索・回答プロファイル配下に置く。"""
    return f"""
CREATE TABLE {table_name} (
    conversation_id    VARCHAR2(64) DEFAULT RAWTOHEX(SYS_GUID()) PRIMARY KEY,
    search_answer_profile_id   VARCHAR2(64) NOT NULL,
    tenant_id_hash     CHAR(64),
    user_id_hash       CHAR(64),
    title              VARCHAR2(400),
    status             VARCHAR2(16) DEFAULT 'ACTIVE' NOT NULL,
    message_count      NUMBER(10) DEFAULT 0 NOT NULL,
    created_at         TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at         TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT {table_name}_status_ck
        CHECK (status IN ('ACTIVE', 'ARCHIVED')),
    CONSTRAINT {table_name}_search_answer_profile_fk
        FOREIGN KEY (search_answer_profile_id)
        REFERENCES {search_answer_profile_table} (search_answer_profile_id)
        ON DELETE CASCADE
);

CREATE INDEX {table_name}_tenant_profile_updated_idx
    ON {table_name} (tenant_id_hash, search_answer_profile_id, updated_at DESC);

CREATE INDEX {table_name}_search_answer_profile_idx
    ON {table_name} (search_answer_profile_id);
""".strip()


def oracle_message_schema_sql(
    table_name: str = "rag_messages",
    conversation_table: str = "rag_conversations",
) -> str:
    """チャットメッセージ(message)table の DDL 例を返す。

    比較カラムのグルーピングに ``reply_to_message_id``(ASSISTANT→対応する USER)を使う。
    作成中の回答(``STREAMING``)は段階(``progress_json``)・作成しているプロセス(``lease_owner``)・
    heartbeat(``heartbeat_at``)を持つ(#1175)。利用者の停止は ``CANCELLED``。
    """
    return f"""
CREATE TABLE {table_name} (
    message_id           VARCHAR2(64) DEFAULT RAWTOHEX(SYS_GUID()) PRIMARY KEY,
    conversation_id      VARCHAR2(64) NOT NULL,
    reply_to_message_id  VARCHAR2(64),
    tenant_id_hash       CHAR(64),
    user_id_hash         CHAR(64),
    role                 VARCHAR2(16) NOT NULL,
    model                VARCHAR2(160),
    content              CLOB,
    citations_json       JSON,
    trace_id             VARCHAR2(64),
    status               VARCHAR2(16) DEFAULT 'COMPLETE' NOT NULL,
    elapsed_ms           NUMBER(12, 3),
    progress_json        JSON,
    lease_owner          VARCHAR2(128),
    heartbeat_at         TIMESTAMP WITH TIME ZONE,
    clarification_json   CLOB,
    answer_notice        CLOB,
    attachments_json     CLOB,
    created_at           TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT {table_name}_role_ck
        CHECK (role IN ('USER', 'ASSISTANT', 'SYSTEM')),
    CONSTRAINT {table_name}_status_ck
        CHECK (status IN ('STREAMING', 'COMPLETE', 'ERROR', 'CANCELLED')),
    CONSTRAINT {table_name}_conversation_fk
        FOREIGN KEY (conversation_id)
        REFERENCES {conversation_table} (conversation_id)
        ON DELETE CASCADE
);

CREATE INDEX {table_name}_conversation_created_idx
    ON {table_name} (conversation_id, created_at);

CREATE INDEX {table_name}_tenant_created_idx
    ON {table_name} (tenant_id_hash, created_at DESC);

CREATE INDEX {table_name}_trace_idx
    ON {table_name} (trace_id);

CREATE INDEX {table_name}_reply_to_idx
    ON {table_name} (reply_to_message_id);
""".strip()


def oracle_document_recipe_schema_sql() -> str:
    """文書ごとに 1〜3 件の独立した処理レシピを保持する DDL。"""
    return """
CREATE TABLE rag_document_recipes (
    recipe_id                   VARCHAR2(64) PRIMARY KEY,
    document_id                 VARCHAR2(64) NOT NULL,
    slot_no                     NUMBER(1) NOT NULL,
    tenant_id_hash              CHAR(64),
    processing_config           JSON,
    status                      VARCHAR2(32) DEFAULT 'UPLOADED' NOT NULL,
    failed_phase                VARCHAR2(16),
    preprocess_artifact         JSON,
    active_extraction_recipe_id VARCHAR2(64),
    config_revision             NUMBER(10) DEFAULT 1 NOT NULL,
    materialized_revision       NUMBER(10),
    error_message               VARCHAR2(2000),
    started_at                  TIMESTAMP WITH TIME ZONE,
    finished_at                 TIMESTAMP WITH TIME ZONE,
    created_at                  TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at                  TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT rag_document_recipes_document_fk
        FOREIGN KEY (document_id) REFERENCES rag_documents (document_id) ON DELETE CASCADE,
    CONSTRAINT rag_document_recipes_slot_ck CHECK (slot_no BETWEEN 1 AND 3),
    CONSTRAINT rag_document_recipes_status_ck CHECK (
        status IN ('UPLOADED', 'PREPROCESSING', 'PREPROCESSED', 'INGESTING', 'REVIEW',
                   'CHUNKING', 'CHUNKED', 'INDEXING', 'INDEXED', 'ERROR')
    ),
    CONSTRAINT rag_document_recipes_phase_ck CHECK (
        failed_phase IS NULL OR failed_phase IN ('PREPROCESS', 'EXTRACT', 'CHUNK', 'INDEX')
    ),
    CONSTRAINT rag_document_recipes_revision_ck CHECK (
        config_revision >= 1
        AND (materialized_revision IS NULL OR materialized_revision >= 1)
    ),
    CONSTRAINT rag_document_recipes_slot_uq UNIQUE (document_id, slot_no)
);

CREATE INDEX rag_document_recipes_status_idx
    ON rag_document_recipes (tenant_id_hash, status, updated_at DESC);
""".strip()


def oracle_ingestion_job_schema_sql(
    table_name: str = "rag_ingestion_jobs",
    document_table: str = "rag_documents",
    recipe_table: str = "rag_document_recipes",
) -> str:
    """Oracle ingestion job table の DDL 例を返す。"""
    language_fk = (
        "rag_ingest_job_lang_release_fk"
        if table_name == "rag_ingestion_jobs"
        else f"{table_name}_lang_release_fk"
    )
    return f"""
CREATE TABLE {table_name} (
    job_id           VARCHAR2(64) PRIMARY KEY,
    document_id      VARCHAR2(64) NOT NULL,
    recipe_id        VARCHAR2(64),
    recipe_revision  NUMBER(10),
    language_pack_release_id VARCHAR2(64),
    tenant_id_hash   CHAR(64),
    status           VARCHAR2(32) NOT NULL,
    phase            VARCHAR2(16) DEFAULT 'PREPROCESS' NOT NULL,
    parser_profile   VARCHAR2(80) NOT NULL,
    quality_warnings JSON,
    settings_overrides JSON,
    skip_reason      VARCHAR2(256),
    error_message    VARCHAR2(2000),
    attempt_count    NUMBER(5) DEFAULT 0 NOT NULL,
    max_attempts     NUMBER(5) DEFAULT 3 NOT NULL,
    queued_at        TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    started_at       TIMESTAMP WITH TIME ZONE,
    finished_at      TIMESTAMP WITH TIME ZONE,
    lease_owner      VARCHAR2(128),
    heartbeat_at     TIMESTAMP WITH TIME ZONE,
    progress_json    CLOB,
    CONSTRAINT {language_fk}
        FOREIGN KEY (language_pack_release_id) REFERENCES rag_language_pack_releases (release_id),
    CONSTRAINT {table_name}_status_ck
        CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'SKIPPED', 'CANCELLED')),
    CONSTRAINT {table_name}_phase_ck
        CHECK (phase IN ('PREPROCESS', 'EXTRACT', 'CHUNK', 'INDEX')),
    CONSTRAINT {table_name}_attempts_ck
        CHECK (attempt_count >= 0 AND max_attempts >= 1),
    CONSTRAINT {table_name}_document_fk
        FOREIGN KEY (document_id)
        REFERENCES {document_table} (document_id)
        ON DELETE CASCADE,
    CONSTRAINT {table_name}_recipe_fk
        FOREIGN KEY (recipe_id)
        REFERENCES {recipe_table} (recipe_id)
        ON DELETE CASCADE
);

CREATE INDEX {table_name}_tenant_queued_idx
    ON {table_name} (tenant_id_hash, status, queued_at DESC);

CREATE INDEX {table_name}_document_idx
    ON {table_name} (document_id, queued_at DESC);

CREATE INDEX {table_name}_recipe_idx
    ON {table_name} (recipe_id, status, queued_at DESC);

CREATE INDEX {table_name}_lease_idx
    ON {table_name} (lease_owner, status);
""".strip()


def oracle_ingestion_segment_schema_sql(
    table_name: str = "rag_ingestion_segments",
    document_table: str = "rag_documents",
    recipe_table: str = "rag_document_recipes",
) -> str:
    """Oracle ingestion segment checkpoint table の DDL 例を返す。"""
    return f"""
CREATE TABLE {table_name} (
    segment_id     VARCHAR2(128) PRIMARY KEY,
    document_id    VARCHAR2(64) NOT NULL,
    recipe_id      VARCHAR2(64),
    tenant_id_hash CHAR(64),
    status         VARCHAR2(32) DEFAULT 'QUEUED' NOT NULL,
    parser_backend VARCHAR2(80) DEFAULT 'enterprise_ai' NOT NULL,
    parser_profile VARCHAR2(80) DEFAULT 'enterprise_ai_generic' NOT NULL,
    page_start     NUMBER(10),
    page_end       NUMBER(10),
    attempt_count  NUMBER(5) DEFAULT 0 NOT NULL,
    artifact_path  VARCHAR2(1024),
    error_code     VARCHAR2(128),
    error_message  VARCHAR2(2000),
    created_at     TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at     TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT {table_name}_status_ck
        CHECK (status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')),
    CONSTRAINT {table_name}_attempts_ck
        CHECK (attempt_count >= 0),
    CONSTRAINT {table_name}_page_range_ck
        CHECK (page_start IS NULL OR page_end IS NULL OR page_start <= page_end),
    CONSTRAINT {table_name}_document_fk
        FOREIGN KEY (document_id)
        REFERENCES {document_table} (document_id)
        ON DELETE CASCADE,
    CONSTRAINT {table_name}_recipe_fk
        FOREIGN KEY (recipe_id)
        REFERENCES {recipe_table} (recipe_id)
        ON DELETE CASCADE
);

CREATE INDEX {table_name}_document_status_idx
    ON {table_name} (document_id, status, page_start, page_end);

CREATE INDEX {table_name}_tenant_status_idx
    ON {table_name} (tenant_id_hash, status, updated_at DESC);

CREATE INDEX {table_name}_recipe_status_idx
    ON {table_name} (recipe_id, status, updated_at DESC);
""".strip()


def oracle_vector_index_create_sql(
    *,
    table: str = "rag_chunks",
    index: str = "rag_chunks_embedding_hnsw_idx",
    target_accuracy: int | None = None,
    neighbors: int | None = None,
    efconstruction: int | None = None,
    distance: str | None = None,
) -> str:
    """主検索の HNSW 索引を作る DDL(末尾の ``;`` まで)を返す。

    値を渡さなければ既定の検索精度の推奨ビルド(``default_vector_index_build``。#1990)で作る。
    schema の作成の DDL と、画面の再作成の SQL(``oracle_schema.vector_index_reindex_sql``)の
    共通の形。
    """
    build = default_vector_index_build()
    accuracy = build.target_accuracy if target_accuracy is None else target_accuracy
    graph_neighbors = build.neighbors if neighbors is None else neighbors
    graph_efconstruction = build.efconstruction if efconstruction is None else efconstruction
    return (
        f"CREATE VECTOR INDEX {index}\n"
        f"    ON {table} (embedding)\n"
        f"    ORGANIZATION INMEMORY NEIGHBOR GRAPH\n"
        f"    DISTANCE {distance or build.distance}\n"
        f"    WITH TARGET ACCURACY {int(accuracy)}\n"
        f"    PARAMETERS (\n"
        f"        TYPE HNSW,\n"
        f"        NEIGHBORS {int(graph_neighbors)},\n"
        f"        EFCONSTRUCTION {int(graph_efconstruction)}\n"
        f"    );"
    )


def oracle_vector_schema_sql(table_name: str = "rag_chunks") -> str:
    """Oracle AI Database の `EMBEDDING_VECTOR_COLUMN_TYPE` の列 + HNSW index の DDL を返す。

    HNSW 索引は既定の検索精度の推奨ビルドで作る(#1990)。既存の環境の索引は作り直さない
    (システムテーブルの更新は無い索引だけを作る。再作成は画面の SQL を DBA が適用する)。"""
    return f"""
{oracle_text_preferences_sql()}

CREATE TABLE {table_name} (
    chunk_id        VARCHAR2(128) PRIMARY KEY,
    document_id     VARCHAR2(64) NOT NULL,
    tenant_id_hash  CHAR(64),
    chunk_index     NUMBER NOT NULL,
    chunk_text      CLOB NOT NULL,
    search_text     CLOB NOT NULL,
    metadata_json   JSON,
    embedding       {EMBEDDING_VECTOR_COLUMN_TYPE},
    chunk_set_id    VARCHAR2(64),
    created_at      TIMESTAMP DEFAULT SYSTIMESTAMP
);

{oracle_vector_index_create_sql(table=table_name, index=f"{table_name}_embedding_hnsw_idx")}

CREATE INDEX {table_name}_text_idx
    ON {table_name} (search_text)
    INDEXTYPE IS CTXSYS.CONTEXT
    {oracle_text_index_parameters_sql()};

CREATE INDEX {table_name}_tenant_document_idx
    ON {table_name} (tenant_id_hash, document_id, chunk_index);

CREATE INDEX {table_name}_chunk_set_idx
    ON {table_name} (chunk_set_id, chunk_index);
""".strip()


def oracle_text_preferences_sql(
    *,
    lexer_preference: str = ORACLE_TEXT_LEXER_PREFERENCE,
    lexer: str = ORACLE_TEXT_LEXER,
    stoplist: str = ORACLE_TEXT_STOPLIST,
) -> str:
    """Oracle Text 用 lexer / stoplist preference を冪等に用意する DDL。"""
    stopword_calls = "\n".join(f"    add_stopword('{word}');" for word in ORACLE_TEXT_STOP_WORDS)
    return f"""
DECLARE
    v_count NUMBER;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM ctx_user_preferences
    WHERE pre_name = '{lexer_preference}'
      AND pre_class = 'LEXER';

    IF v_count = 0 THEN
        CTX_DDL.CREATE_PREFERENCE('{lexer_preference}', '{lexer}');
    END IF;
END;
/

DECLARE
    v_count NUMBER;
    PROCEDURE add_stopword(p_word VARCHAR2) IS
    BEGIN
        CTX_DDL.ADD_STOPWORD('{stoplist}', p_word);
    EXCEPTION
        WHEN OTHERS THEN
            NULL;
    END;
BEGIN
    SELECT COUNT(*) INTO v_count
    FROM ctx_user_stoplists
    WHERE spl_name = '{stoplist}';

    IF v_count = 0 THEN
        CTX_DDL.CREATE_STOPLIST('{stoplist}', 'BASIC_STOPLIST');
    END IF;

{stopword_calls}
END;
/
""".strip()


def oracle_text_index_parameters_sql(
    *,
    lexer_preference: str = ORACLE_TEXT_LEXER_PREFERENCE,
    stoplist: str = ORACLE_TEXT_STOPLIST,
) -> str:
    """Oracle Text index の multilingual query 用 parameter 句を返す。"""
    return f"PARAMETERS ('LEXER {lexer_preference} STOPLIST {stoplist} SYNC (ON COMMIT)')"


def oracle_chunk_set_schema_sql() -> str:
    """変換成果(chunk_set / extraction / artifact layer)の永続層 DDL を返す。

    3 層モデル: chunk_set は文書単位(KB 非依存)。配信中かは ``is_serving`` 列で持つ。
    所属 KB は ``rag_document_knowledge_bases`` の membership が正本(別表 binding は持たない)。
    """
    return """
CREATE TABLE rag_chunk_sets (
    chunk_set_id    VARCHAR2(64) PRIMARY KEY,
    document_id     VARCHAR2(64) NOT NULL,
    recipe_id       VARCHAR2(64),
    extraction_recipe_id VARCHAR2(64),
    language_pack_release_id VARCHAR2(64),
    language_pack_ingest_hash VARCHAR2(64),
    language_pack_job_id VARCHAR2(64),
    language_pack_built_at TIMESTAMP WITH TIME ZONE,
    tenant_id_hash  CHAR(64),
    recipe_subset   JSON,
    status          VARCHAR2(32) DEFAULT 'INGESTING' NOT NULL,
    chunk_count     NUMBER(10) DEFAULT 0 NOT NULL,
    vector_count    NUMBER(10) DEFAULT 0 NOT NULL,
    is_serving      NUMBER(1) DEFAULT 1 NOT NULL,
    is_active       NUMBER(1) DEFAULT 0 NOT NULL,
    metrics_json    JSON,
    first_page_context JSON,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT rag_chunk_set_lang_release_fk FOREIGN KEY (language_pack_release_id)
        REFERENCES rag_language_pack_releases (release_id),
    CONSTRAINT rag_chunk_sets_document_fk
        FOREIGN KEY (document_id) REFERENCES rag_documents (document_id) ON DELETE CASCADE,
    CONSTRAINT rag_chunk_sets_recipe_fk
        FOREIGN KEY (recipe_id) REFERENCES rag_document_recipes (recipe_id) ON DELETE CASCADE,
    CONSTRAINT rag_chunk_sets_status_ck
        CHECK (status IN ('INGESTING', 'CHUNKED', 'INDEXED', 'ERROR')),
    CONSTRAINT rag_chunk_sets_serving_ck CHECK (is_serving IN (0, 1)),
    CONSTRAINT rag_chunk_sets_active_ck CHECK (is_active IN (0, 1))
);

CREATE INDEX rag_chunk_sets_document_idx
    ON rag_chunk_sets (document_id, status);

CREATE INDEX rag_chunk_sets_serving_idx
    ON rag_chunk_sets (document_id, is_serving);

CREATE INDEX rag_chunk_sets_recipe_idx
    ON rag_chunk_sets (recipe_id, status, updated_at DESC);

CREATE UNIQUE INDEX rag_chunk_sets_recipe_active_uidx
    ON rag_chunk_sets (CASE WHEN is_active = 1 THEN recipe_id END);

CREATE INDEX rag_chunk_sets_extraction_idx
    ON rag_chunk_sets (document_id, extraction_recipe_id);

CREATE TABLE rag_document_extractions (
    document_id          VARCHAR2(64) NOT NULL,
    extraction_recipe_id VARCHAR2(64) NOT NULL,
    source_sha256        CHAR(64),
    tenant_id_hash       CHAR(64),
    recipe_subset        JSON,
    extraction_json      JSON,
    status               VARCHAR2(32) DEFAULT 'planned_only' NOT NULL,
    reason               VARCHAR2(2000),
    metrics_json         JSON,
    created_at           TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at           TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT rag_document_extractions_pk
        PRIMARY KEY (document_id, extraction_recipe_id),
    CONSTRAINT rag_doc_ext_document_fk
        FOREIGN KEY (document_id) REFERENCES rag_documents (document_id) ON DELETE CASCADE,
    CONSTRAINT rag_doc_ext_status_ck
        CHECK (
            status IN ('not_requested', 'planned_only', 'materialized', 'needs_reingest', 'error')
        )
);

CREATE INDEX rag_doc_ext_status_idx
    ON rag_document_extractions (document_id, status);

CREATE TABLE rag_artifact_layers (
    layer_id            VARCHAR2(64) PRIMARY KEY,
    layer_kind          VARCHAR2(32) NOT NULL,
    parent_chunk_set_id VARCHAR2(64) NOT NULL,
    document_id         VARCHAR2(64) NOT NULL,
    tenant_id_hash      CHAR(64),
    requested           NUMBER(1) DEFAULT 1 NOT NULL,
    status              VARCHAR2(32) DEFAULT 'planned_only' NOT NULL,
    reason              VARCHAR2(2000),
    metrics_json        JSON,
    input_fingerprint   JSON,
    created_at          TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at          TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT rag_artifact_layers_chunk_set_fk
        FOREIGN KEY (parent_chunk_set_id)
        REFERENCES rag_chunk_sets (chunk_set_id) ON DELETE CASCADE,
    CONSTRAINT rag_artifact_layers_document_fk
        FOREIGN KEY (document_id) REFERENCES rag_documents (document_id) ON DELETE CASCADE,
    CONSTRAINT rag_artifact_layers_requested_ck CHECK (requested IN (0, 1)),
    CONSTRAINT rag_artifact_layers_kind_ck
        CHECK (layer_kind IN ('metadata', 'graph', 'navigation')),
    CONSTRAINT rag_artifact_layers_status_ck
        CHECK (
            status IN ('not_requested', 'planned_only', 'materialized', 'needs_reingest', 'error')
        )
);

CREATE INDEX rag_artifact_layers_document_idx ON rag_artifact_layers (document_id);

CREATE INDEX rag_artifact_layers_parent_idx
    ON rag_artifact_layers (parent_chunk_set_id, layer_kind, status);
""".strip()


def oracle_document_schema_sql(table_name: str = "rag_documents") -> str:
    """Oracle document table の DDL 例を返す。"""
    return f"""
CREATE TABLE {table_name} (
    document_id              VARCHAR2(64) PRIMARY KEY,
    file_name                VARCHAR2(512) NOT NULL,
    status                   VARCHAR2(32) NOT NULL,
    tenant_id_hash           CHAR(64),
    category_name            VARCHAR2(256),
    object_storage_path      VARCHAR2(1024),
    preprocess_artifact      JSON,
    processing_config        JSON,
    classification           JSON,
    content_type             VARCHAR2(255),
    file_size_bytes          NUMBER(19),
    content_sha256           CHAR(64),
    duplicate_of_document_id VARCHAR2(64),
    extraction               JSON,
    error_message            VARCHAR2(2000),
    uploaded_at              TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    indexed_at               TIMESTAMP WITH TIME ZONE,
    -- 文書の版(#1248)。この文書を置き換えた新しい版の文書と、置き換えた日時。NULL は今有効な版。
    -- 自己参照の FK は持たず、存在・自己参照・循環はアプリで検証する。
    superseded_by_document_id VARCHAR2(64),
    superseded_at            TIMESTAMP WITH TIME ZONE,
    CONSTRAINT {table_name}_status_ck
        CHECK (status IN (
            'UPLOADED', 'PREPROCESSING', 'PREPROCESSED', 'INGESTING', 'REVIEW',
            'CHUNKING', 'CHUNKED', 'INDEXING', 'INDEXED', 'ERROR'
        )),
    CONSTRAINT {table_name}_duplicate_fk
        FOREIGN KEY (duplicate_of_document_id) REFERENCES {table_name} (document_id)
);

CREATE INDEX {table_name}_duplicate_idx ON {table_name} (duplicate_of_document_id);

CREATE INDEX {table_name}_content_sha256_idx
    ON {table_name} (content_sha256);

CREATE INDEX {table_name}_status_uploaded_idx
    ON {table_name} (status, uploaded_at DESC);

CREATE INDEX {table_name}_tenant_status_uploaded_idx
    ON {table_name} (tenant_id_hash, status, uploaded_at DESC);
""".strip()


def oracle_search_audit_schema_sql(table_name: str = "rag_search_audit") -> str:
    """RAG 検索監査 table の DDL 例を返す。"""
    return f"""
CREATE TABLE {table_name} (
    audit_id              VARCHAR2(64) DEFAULT RAWTOHEX(SYS_GUID()) PRIMARY KEY,
    event_type            VARCHAR2(32) DEFAULT 'rag.search' NOT NULL,
    trace_id              VARCHAR2(64) NOT NULL,
    request_id            VARCHAR2(128),
    tenant_id_hash        CHAR(64),
    user_id_hash          CHAR(64),
    outcome               VARCHAR2(32) NOT NULL,
    search_mode           VARCHAR2(16) NOT NULL,
    query_hash            CHAR(64) NOT NULL,
    query_chars           NUMBER(10) NOT NULL,
    filter_keys           JSON,
    memory_plan_id        VARCHAR2(32),
    top_k                 NUMBER(10),
    rerank_top_n          NUMBER(10),
    query_variant_count   NUMBER(10) DEFAULT 1 NOT NULL,
    retrieved_count       NUMBER(10) DEFAULT 0 NOT NULL,
    reranked_count        NUMBER(10) DEFAULT 0 NOT NULL,
    deduplicated_count    NUMBER(10) DEFAULT 0 NOT NULL,
    context_diversified_count NUMBER(10) DEFAULT 0 NOT NULL,
    context_group_expanded_count NUMBER(10) DEFAULT 0 NOT NULL,
    context_expanded_count NUMBER(10) DEFAULT 0 NOT NULL,
    context_adaptive_expanded_count NUMBER(10) DEFAULT 0 NOT NULL,
    context_dependency_promoted_count NUMBER(10) DEFAULT 0 NOT NULL,
    context_compressed_count NUMBER(10) DEFAULT 0 NOT NULL,
    context_compression_saved_chars NUMBER(10) DEFAULT 0 NOT NULL,
    agent_memory_retrieved_count NUMBER(10) DEFAULT 0 NOT NULL,
    agent_memory_writeback_count NUMBER(10) DEFAULT 0 NOT NULL,
    agent_memory_writeback_status VARCHAR2(32) DEFAULT 'skipped' NOT NULL,
    evidence_count        NUMBER(10) DEFAULT 0 NOT NULL,
    support_count         NUMBER(10) DEFAULT 0 NOT NULL,
    structure_count       NUMBER(10) DEFAULT 0 NOT NULL,
    history_count         NUMBER(10) DEFAULT 0 NOT NULL,
    resolver_rejected_count NUMBER(10) DEFAULT 0 NOT NULL,
    insufficient_context_count NUMBER(10) DEFAULT 0 NOT NULL,
    citation_count        NUMBER(10) DEFAULT 0 NOT NULL,
    context_chars         NUMBER(10) DEFAULT 0 NOT NULL,
    context_window_chars  NUMBER(10),
    document_ids          JSON,
    knowledge_base_ids    JSON,
    config_fingerprint    CHAR(64),
    elapsed_ms            NUMBER(12, 3) NOT NULL,
    error_stage           VARCHAR2(64),
    error_type            VARCHAR2(128),
    created_at            TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT {table_name}_outcome_ck
        CHECK (outcome IN ('success', 'blocked', 'no_results', 'error')),
    CONSTRAINT {table_name}_search_mode_ck
        CHECK (search_mode IN ('hybrid', 'vector', 'keyword')),
    CONSTRAINT {table_name}_agent_memory_status_ck
        CHECK (agent_memory_writeback_status IN ('skipped', 'saved', 'failed'))
);

CREATE INDEX {table_name}_trace_idx
    ON {table_name} (trace_id);

CREATE INDEX {table_name}_tenant_created_idx
    ON {table_name} (tenant_id_hash, created_at DESC);

CREATE INDEX {table_name}_created_outcome_idx
    ON {table_name} (created_at DESC, outcome);

CREATE INDEX {table_name}_query_hash_idx
    ON {table_name} (query_hash);

CREATE INDEX {table_name}_config_idx
    ON {table_name} (config_fingerprint);
""".strip()


def oracle_ingestion_audit_schema_sql(table_name: str = "rag_ingestion_audit") -> str:
    """RAG 取込監査 table の DDL 例を返す。"""
    return f"""
CREATE TABLE {table_name} (
    audit_id               VARCHAR2(64) DEFAULT RAWTOHEX(SYS_GUID()) PRIMARY KEY,
    event_type             VARCHAR2(32) DEFAULT 'rag.ingestion' NOT NULL,
    trace_id               VARCHAR2(64) NOT NULL,
    request_id             VARCHAR2(128),
    tenant_id_hash         CHAR(64),
    user_id_hash           CHAR(64),
    document_id            VARCHAR2(64) NOT NULL,
    outcome                VARCHAR2(32) NOT NULL,
    source_sha256          CHAR(64) NOT NULL,
    source_bytes           NUMBER(19) NOT NULL,
    document_type          VARCHAR2(128),
    extraction_confidence  NUMBER(6, 5),
    parser_backend         VARCHAR2(80),
    parser_profile         VARCHAR2(80),
    segment_count          NUMBER(10) DEFAULT 0 NOT NULL,
    fallback_count         NUMBER(10) DEFAULT 0 NOT NULL,
    failed_segment_count   NUMBER(10) DEFAULT 0 NOT NULL,
    chunk_count            NUMBER(10) DEFAULT 0 NOT NULL,
    vector_count           NUMBER(10) DEFAULT 0 NOT NULL,
    elapsed_ms             NUMBER(12, 3) NOT NULL,
    error_type             VARCHAR2(128),
    error_message          VARCHAR2(2000),
    created_at             TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT {table_name}_outcome_ck
        CHECK (outcome IN ('success', 'error'))
);

CREATE INDEX {table_name}_trace_idx
    ON {table_name} (trace_id);

CREATE INDEX {table_name}_tenant_created_idx
    ON {table_name} (tenant_id_hash, created_at DESC);

CREATE INDEX {table_name}_document_created_idx
    ON {table_name} (document_id, created_at DESC);

CREATE INDEX {table_name}_parser_created_idx
    ON {table_name} (parser_backend, parser_profile, created_at DESC);

CREATE INDEX {table_name}_source_sha256_idx
    ON {table_name} (source_sha256);
""".strip()


def oracle_knowledge_graph_schema_sql() -> str:
    """関係情報(文書と章・節の見出しのつながり)の table DDL を返す。"""
    return """
CREATE TABLE rag_graph_entities (
    entity_id          VARCHAR2(64) PRIMARY KEY,
    chunk_set_id       VARCHAR2(64),
    tenant_id_hash     CHAR(64),
    knowledge_base_id  VARCHAR2(64),
    canonical_name     VARCHAR2(512) NOT NULL,
    entity_type        VARCHAR2(128),
    description        CLOB,
    confidence         NUMBER(6, 5),
    source_document_ids JSON,
    created_at         TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    updated_at         TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE INDEX rag_graph_entities_chunk_set_idx
    ON rag_graph_entities (chunk_set_id);

CREATE INDEX rag_graph_entities_tenant_name_idx
    ON rag_graph_entities (tenant_id_hash, canonical_name);

CREATE TABLE rag_graph_relationships (
    relationship_id    VARCHAR2(64) PRIMARY KEY,
    chunk_set_id       VARCHAR2(64),
    tenant_id_hash     CHAR(64),
    knowledge_base_id  VARCHAR2(64),
    source_entity_id   VARCHAR2(64) NOT NULL,
    target_entity_id   VARCHAR2(64) NOT NULL,
    relationship_type  VARCHAR2(128) NOT NULL,
    description        CLOB,
    confidence         NUMBER(6, 5),
    source_document_ids JSON,
    created_at         TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT rag_graph_rel_source_fk
        FOREIGN KEY (source_entity_id) REFERENCES rag_graph_entities (entity_id),
    CONSTRAINT rag_graph_rel_target_fk
        FOREIGN KEY (target_entity_id) REFERENCES rag_graph_entities (entity_id)
);

CREATE INDEX rag_graph_rel_source_idx
    ON rag_graph_relationships (tenant_id_hash, source_entity_id);

CREATE INDEX rag_graph_rel_target_idx
    ON rag_graph_relationships (tenant_id_hash, target_entity_id);

CREATE TABLE rag_graph_entity_chunks (
    entity_id          VARCHAR2(64) NOT NULL,
    chunk_id           VARCHAR2(128) NOT NULL,
    chunk_set_id       VARCHAR2(64),
    document_id        VARCHAR2(64) NOT NULL,
    tenant_id_hash     CHAR(64),
    relevance_score    NUMBER(8, 6) DEFAULT 1 NOT NULL,
    created_at         TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT rag_graph_entity_chunks_pk PRIMARY KEY (entity_id, chunk_id),
    CONSTRAINT rag_graph_entity_chunks_entity_fk
        FOREIGN KEY (entity_id) REFERENCES rag_graph_entities (entity_id)
);

CREATE INDEX rag_graph_entity_chunks_chunk_idx
    ON rag_graph_entity_chunks (tenant_id_hash, chunk_id);

CREATE INDEX rag_graph_entity_chunks_chunk_set_idx
    ON rag_graph_entity_chunks (chunk_set_id);
""".strip()


def oracle_feedback_schema_sql(table_name: str = "rag_citation_feedback") -> str:
    """回答/引用 feedback の低機密保存 table DDL を返す。"""
    return f"""
CREATE TABLE {table_name} (
    feedback_id       VARCHAR2(64) DEFAULT RAWTOHEX(SYS_GUID()) PRIMARY KEY,
    trace_id          VARCHAR2(64) NOT NULL,
    search_answer_profile_id  VARCHAR2(64),
    target_type       VARCHAR2(16) DEFAULT 'citation' NOT NULL,
    source_surface    VARCHAR2(16),
    document_id       VARCHAR2(64),
    chunk_id          VARCHAR2(128),
    tenant_id_hash    CHAR(64),
    user_id_hash      CHAR(64),
    rating            VARCHAR2(32) NOT NULL,
    reason            VARCHAR2(64),
    comment_hash      CHAR(64),
    comment_chars     NUMBER(10) DEFAULT 0 NOT NULL,
    created_at        TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT {table_name}_rating_ck
        CHECK (rating IN ('helpful', 'not_helpful')),
    CONSTRAINT {table_name}_target_ck
        CHECK (target_type IN ('answer', 'citation')),
    CONSTRAINT {table_name}_surface_ck
        CHECK (source_surface IS NULL OR source_surface IN ('search', 'chat')),
    CONSTRAINT {table_name}_shape_ck
        CHECK (
            (target_type = 'answer' AND document_id IS NULL AND chunk_id IS NULL)
            OR (target_type = 'citation' AND document_id IS NOT NULL AND chunk_id IS NOT NULL)
        ),
    CONSTRAINT {table_name}_reason_ck
        CHECK (
            (rating = 'helpful' AND reason IS NULL)
            OR (
                rating = 'not_helpful'
                AND (
                    (target_type = 'answer' AND reason IN (
                        'incorrect', 'incomplete', 'not_relevant', 'answer_untrusted',
                        'missing_knowledge', 'outdated_source', 'ambiguous_question'
                    ))
                    OR (target_type = 'citation' AND reason IN (
                        'missing_evidence', 'not_relevant', 'answer_untrusted'
                    ))
                )
            )
        )
);

CREATE INDEX {table_name}_trace_idx
    ON {table_name} (trace_id);

CREATE INDEX {table_name}_tenant_created_idx
    ON {table_name} (tenant_id_hash, created_at DESC);

CREATE INDEX rag_feedback_business_created_idx
    ON {table_name} (tenant_id_hash, search_answer_profile_id, created_at DESC);

CREATE INDEX rag_feedback_user_trace_idx
    ON {table_name} (tenant_id_hash, user_id_hash, trace_id, created_at DESC);
""".strip()


def oracle_feedback_details_schema_sql(
    table_name: str = "rag_feedback_details",
    feedback_table: str = "rag_citation_feedback",
) -> str:
    """管理者調査用の feedback 本文・根拠 snapshot table DDL を返す。"""
    return f"""
CREATE TABLE {table_name} (
    feedback_id       VARCHAR2(64) PRIMARY KEY,
    tenant_id_hash    CHAR(64),
    message_id        VARCHAR2(64),
    content_source    VARCHAR2(32) NOT NULL,
    question_text     CLOB,
    answer_text       CLOB,
    comment_text      VARCHAR2(1000 CHAR),
    corrected_answer_text CLOB,
    citations_json    JSON,
    search_text       CLOB,
    created_at        TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT {table_name}_feedback_fk
        FOREIGN KEY (feedback_id)
        REFERENCES {feedback_table} (feedback_id)
        ON DELETE CASCADE,
    CONSTRAINT {table_name}_source_ck
        CHECK (content_source IN ('chat_message', 'search_snapshot'))
);

CREATE INDEX {table_name}_tenant_created_idx
    ON {table_name} (tenant_id_hash, created_at DESC);

CREATE INDEX {table_name}_message_idx
    ON {table_name} (message_id);

CREATE INDEX {table_name}_text_idx
    ON {table_name} (search_text)
    INDEXTYPE IS CTXSYS.CONTEXT
    {oracle_text_index_parameters_sql()};
""".strip()


def oracle_evaluation_artifact_schema_sql(
    table_name: str = "rag_evaluation_runs",
) -> str:
    """nightly / staging の評価結果 artifact table DDL を返す。"""
    return f"""
CREATE TABLE {table_name} (
    evaluation_run_id VARCHAR2(64) DEFAULT RAWTOHEX(SYS_GUID()) PRIMARY KEY,
    tenant_id_hash    CHAR(64),
    knowledge_base_ids JSON,
    request_json      JSON NOT NULL,
    result_json       JSON NOT NULL,
    result_sha256     CHAR(64) NOT NULL,
    passed            NUMBER(1) DEFAULT 0 NOT NULL,
    created_at        TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL
);

CREATE INDEX {table_name}_tenant_created_idx
    ON {table_name} (tenant_id_hash, created_at DESC);

CREATE INDEX {table_name}_result_hash_idx
    ON {table_name} (result_sha256);
""".strip()


def oracle_evaluation_job_schema_sql(
    table_name: str = "rag_evaluation_jobs",
) -> str:
    """品質評価の job（非同期の実行・進捗・取り消し。#390）の table DDL を返す。

    評価の入力（query の本文）は保存しない。job は投入を受けた backend のプロセスで動き、
    そのプロセスが止まったら heartbeat が途絶えて失敗になる（別のプロセスは引き継がない）。
    """
    return f"""
CREATE TABLE {table_name} (
    job_id                  VARCHAR2(64) PRIMARY KEY,
    kind                    VARCHAR2(16) NOT NULL,
    status                  VARCHAR2(16) NOT NULL,
    tenant_id_hash          VARCHAR2(64),
    user_id_hash            VARCHAR2(64),
    total_cases             NUMBER(10) DEFAULT 0 NOT NULL,
    completed_cases         NUMBER(10) DEFAULT 0 NOT NULL,
    current_case_id         VARCHAR2(200 CHAR),
    current_case_started_at TIMESTAMP WITH TIME ZONE,
    lease_owner             VARCHAR2(128),
    heartbeat_at            TIMESTAMP WITH TIME ZONE,
    time_limit_seconds      NUMBER(10) NOT NULL,
    error_message           VARCHAR2(2000 CHAR),
    result_json             JSON,
    evaluation_run_id       VARCHAR2(64),
    created_at              TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    started_at              TIMESTAMP WITH TIME ZONE,
    finished_at             TIMESTAMP WITH TIME ZONE,
    updated_at              TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT {table_name}_kind_ck CHECK (kind IN ('run', 'calibration')),
    CONSTRAINT {table_name}_status_ck
        CHECK (status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED'))
);

CREATE INDEX {table_name}_status_idx
    ON {table_name} (status, heartbeat_at);

CREATE INDEX {table_name}_owner_created_idx
    ON {table_name} (tenant_id_hash, user_id_hash, created_at DESC)
""".strip()


def _metadata_value_equals(
    metadata: Mapping[str, MetadataValue],
    key: str,
    expected: str,
) -> bool:
    """chunk metadata の文字列値を case-insensitive に完全一致で見る。"""
    value = metadata.get(key)
    return isinstance(value, str) and value.casefold() == expected.casefold()


def _metadata_value_contains(
    metadata: Mapping[str, MetadataValue],
    key: str,
    expected: str,
) -> bool:
    """chunk metadata の文字列値を case-insensitive に部分一致で見る。"""
    value = metadata.get(key)
    return isinstance(value, str) and expected.casefold() in value.casefold()


def _current_tenant_id_hash() -> str | None:
    """現在の request context にある tenant hash を返す。"""
    return current_audit_request_context().tenant_id_hash


def _retrieved_chunk_score_sort_key(
    chunk: RetrievedChunk,
    score: float,
) -> tuple[float, str, int, str]:
    """RRF score 降順、document/chunk 昇順の安定した検索順を返す。"""
    chunk_index = chunk.metadata.get("chunk_index")
    stable_index = chunk_index if isinstance(chunk_index, int) else 0
    return (-score, chunk.document_id, stable_index, chunk.chunk_id)


async def _observed_reads(stage: str, reads: Sequence[Awaitable[None]]) -> None:
    """読み取りを並行に読み、全体を 1 つの処理の経過の段(``observe_call("stage", stage)``)にする。

    読むものが無ければ段を出さない(#3443)。
    """
    if not reads:
        return
    with observe_call("stage", stage):
        await asyncio.gather(*reads)


def _with_retrieval_metadata(
    chunk: RetrievedChunk,
    **metadata: MetadataValue,
) -> RetrievedChunk:
    """検索スコアの説明用 metadata を既存 metadata に追加する。"""
    return chunk.model_copy(update={"metadata": {**chunk.metadata, **metadata}})


def _chunk_index_from_retrieved(chunk: RetrievedChunk) -> int | None:
    """RetrievedChunk metadata から安全に chunk_index を読む。"""
    value = chunk.metadata.get("chunk_index")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned and cleaned.lstrip("-").isdigit():
            return int(cleaned)
    return None


def _chunk_group_id_from_retrieved(chunk: RetrievedChunk) -> str | None:
    """RetrievedChunk metadata から chunk_group_id を読む。"""
    return _chunk_group_id_from_metadata(chunk.metadata)


def _chunk_group_id_from_metadata(metadata: Mapping[str, MetadataValue]) -> str | None:
    """metadata の chunk_group_id を空白除去済み文字列として読む。"""
    value = metadata.get("chunk_group_id")
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _with_context_group_metadata(
    chunk: RetrievedChunk,
    *,
    anchor: RetrievedChunk,
    group_id: str,
    distance: int,
) -> RetrievedChunk:
    """同一 group context と anchor の対応を citation metadata に残す。"""
    return chunk.model_copy(
        update={
            "metadata": {
                **chunk.metadata,
                "context_group_expanded": anchor.metadata.get("chunk_strategy") != "small_to_big",
                "context_group_purpose": (
                    "parent_source_mapping"
                    if anchor.metadata.get("chunk_strategy") == "small_to_big"
                    else "neighbor_context"
                ),
                "context_anchor_chunk_id": anchor.chunk_id,
                "context_group_id": group_id,
                "context_group_distance": distance,
            }
        }
    )


def _hybrid_retrieval_mode(metadata: dict[str, MetadataValue]) -> str:
    """hybrid 検索結果がどの検索経路から来たかを返す。"""
    has_vector = "vector_rank" in metadata
    has_keyword = "keyword_rank" in metadata
    if has_vector and has_keyword:
        return "hybrid"
    if has_vector:
        return "vector"
    return "keyword"


def _to_document_summary(document: StoredDocument) -> DocumentSummary:
    return DocumentSummary(
        id=document.id,
        file_name=document.file_name,
        status=document.status,
        category_name=document.category_name,
        content_type=document.content_type,
        file_size_bytes=document.file_size_bytes,
        content_sha256=document.content_sha256,
        duplicate_of_document_id=document.duplicate_of_document_id,
        uploaded_at=document.uploaded_at,
        indexed_at=document.indexed_at,
        superseded_by_document_id=document.superseded_by_document_id,
        superseded_at=document.superseded_at,
        source_profile=build_source_profile(
            original_file_name=document.file_name,
            sanitized_file_name=document.file_name,
            content_type=document.content_type,
            file_size_bytes=document.file_size_bytes,
            content_sha256=document.content_sha256,
            duplicate_of_document_id=document.duplicate_of_document_id,
        ),
    )


def _to_document_detail(document: StoredDocument) -> DocumentDetail:
    return DocumentDetail(
        id=document.id,
        file_name=document.file_name,
        status=document.status,
        category_name=document.category_name,
        content_type=document.content_type,
        file_size_bytes=document.file_size_bytes,
        content_sha256=document.content_sha256,
        duplicate_of_document_id=document.duplicate_of_document_id,
        uploaded_at=document.uploaded_at,
        indexed_at=document.indexed_at,
        superseded_by_document_id=document.superseded_by_document_id,
        superseded_at=document.superseded_at,
        object_storage_path=document.object_storage_path,
        preprocess_artifact=(
            DocumentPreprocessArtifact.model_validate(document.preprocess_artifact)
            if document.preprocess_artifact
            else None
        ),
        extraction=document.extraction,
        error_message=document.error_message,
        classification=(
            DocumentClassification.model_validate(document.classification)
            if document.classification
            else None
        ),
        source_profile=build_source_profile(
            original_file_name=document.file_name,
            sanitized_file_name=document.file_name,
            content_type=document.content_type,
            file_size_bytes=document.file_size_bytes,
            content_sha256=document.content_sha256,
            duplicate_of_document_id=document.duplicate_of_document_id,
        ),
    )


def _to_knowledge_base_ref(knowledge_base: StoredKnowledgeBase) -> KnowledgeBaseRef:
    return KnowledgeBaseRef(id=knowledge_base.id, name=knowledge_base.name)


def _to_knowledge_base_summary(
    knowledge_base: StoredKnowledgeBase,
) -> KnowledgeBaseSummary:
    return KnowledgeBaseSummary(
        id=knowledge_base.id,
        name=knowledge_base.name,
        description=knowledge_base.description,
        status=knowledge_base.status,
        default_search_mode=knowledge_base.default_search_mode,
        document_count=knowledge_base.document_count,
        indexed_document_count=knowledge_base.indexed_document_count,
        error_document_count=knowledge_base.error_document_count,
        searchable_chunk_count=knowledge_base.searchable_chunk_count,
        created_at=knowledge_base.created_at,
        updated_at=knowledge_base.updated_at,
        archived_at=knowledge_base.archived_at,
    )


def _to_knowledge_base_detail(
    knowledge_base: StoredKnowledgeBase,
) -> KnowledgeBaseDetail:
    adapter_config = parse_adapter_config(knowledge_base.retrieval_config)
    return KnowledgeBaseDetail(
        **_to_knowledge_base_summary(knowledge_base).model_dump(),
        retrieval_config=knowledge_base.retrieval_config,
        adapter_config=adapter_config,
        legacy_query_config_ignored=adapter_config.query != KnowledgeBaseQueryConfig(),
    )


def updated_copy_knowledge_base(
    knowledge_base: StoredKnowledgeBase,
    **changes: object,
) -> StoredKnowledgeBase:
    return replace(knowledge_base, **cast(Any, changes))


def _to_search_answer_profile_summary(
    view: StoredSearchAnswerProfile,
) -> SearchAnswerProfileSummary:
    config = parse_search_answer_profile_config(view.profile_config)
    return SearchAnswerProfileSummary(
        id=view.id,
        name=view.name,
        description=view.description,
        status=view.status,
        knowledge_base_count=len(config.normalized_knowledge_base_ids()),
        created_at=view.created_at,
        updated_at=view.updated_at,
        archived_at=view.archived_at,
    )


def _to_search_answer_profile_detail(view: StoredSearchAnswerProfile) -> SearchAnswerProfileDetail:
    config = parse_search_answer_profile_config(view.profile_config)
    return SearchAnswerProfileDetail(
        **_to_search_answer_profile_summary(view).model_dump(),
        config=config,
        knowledge_bases=[],
    )


def updated_copy_search_answer_profile(
    view: StoredSearchAnswerProfile,
    **changes: object,
) -> StoredSearchAnswerProfile:
    return replace(view, **cast(Any, changes))


def _tokens(text: str) -> list[str]:
    return [match.group(0).lower() for match in TOKEN_PATTERN.finditer(text)]


def oracle_text_terms(query: str, *, settings: Settings | None = None) -> list[str]:
    """全文検索（Oracle Text）の語。検索と同じ分割で、診断に出す（#588）。"""
    from rag_engine.retrieval.text_search_tokenizer import tokenize_text_search_query

    return tokenize_text_search_query(query, domain_keywords=_text_search_domain_keywords(settings))


def _oracle_text_query(query: str, *, settings: Settings | None = None) -> str | None:
    """質問文から Oracle Text の CONTAINS の query を作る（#588）。

    分割は rag_engine の 1 つの方式（Sudachi の C / A を主に、文字種の区切り・漢字の部分語・
    送り仮名を除いた形を補う。Sudachi が無ければ文字種の区切りだけ）。検索・回答プロファイルの
    ドメインキーワードは 1 語として優先する。語は重み付きの ``ACCUM`` で結ぶ。
    索引の側（WORLD_LEXER）は変えない。
    """
    from rag_engine.retrieval.text_search_tokenizer import oracle_text_query_for_question

    return (
        oracle_text_query_for_question(
            query, domain_keywords=_text_search_domain_keywords(settings)
        )
        or None
    )


def _text_search_domain_keywords(settings: Settings | None) -> list[str]:
    return list(settings.rag_domain_keywords) if settings is not None else []


def _rrf(rank: int, constant: int = 60) -> float:
    return 1.0 / (constant + rank)
