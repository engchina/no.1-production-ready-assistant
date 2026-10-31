"""外部 SDK に依存しない既存データ契約。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class AdbHybridSearchUnavailable(RuntimeError):
    """ADB hybrid search を実行できない前提条件不足を表します。"""

    pass


class AdbEmbeddingsUnavailable(AdbHybridSearchUnavailable):
    """対象チャンクの embedding が未作成で検索できない状態を表します。"""

    pass


@dataclass(frozen=True)
class EmbeddingSaveResult:
    """チャンク embedding 保存処理の件数と警告を保持します。"""

    source_run_id: str
    chunk_run_id: str
    document_id: str
    child_count: int
    parent_count: int
    created_count: int
    skipped_count: int
    error_count: int
    errors: tuple[str, ...] = ()
    image_candidate_count: int = 0
    image_created_count: int = 0
    image_skipped_count: int = 0
    image_missing_asset_count: int = 0
    # 空でなければ、より新しいこの run を knowledge base の公開対象として維持した。
    kept_latest_chunk_run_id: str = ""
    # この保存で run を knowledge base の検索対象として公開したか（中断時と、より新しい run がある時は False）。
    published: bool = False
    # 保存に使った embedding のモデルと次元。既存データと混在できるかを利用者が判断するために表示する (#816)。
    embedding_model: str = ""
    embedding_dimensions: int = 0

    @property
    def ok(self) -> bool:
        """処理結果が成功扱いかどうかを返します。"""
        return self.error_count == 0


@dataclass(frozen=True)
class EmbeddingStatusResult:
    """チャンク run ごとの text/image embedding 準備状況を保持します。"""

    source_run_id: str
    chunk_run_id: str
    document_id: str
    child_count: int
    parent_count: int
    embedded_count: int
    missing_count: int
    stale_count: int
    image_embedding_enabled: bool = False
    image_candidate_count: int = 0
    image_embedded_count: int = 0
    image_missing_count: int = 0
    image_stale_count: int = 0
    image_missing_asset_count: int = 0
    # 状態の判定に使った embedding のモデルと次元 (#816)。
    embedding_model: str = ""
    embedding_dimensions: int = 0
    # この文書の active な chunk 行のうち、metadata_json.schema_version が現行版でない件数。検索対象外になる (#819)。
    legacy_row_count: int = 0

    @property
    def text_pending_count(self) -> int:
        """text embedding が未作成の active child chunk 数を返します。"""
        return self.missing_count + self.stale_count

    @property
    def image_pending_count(self) -> int:
        """image embedding が未作成の対象 child chunk 数を返します。"""
        if not self.image_embedding_enabled:
            return 0
        return self.image_missing_count + self.image_stale_count + self.image_missing_asset_count

    @property
    def pending_count(self) -> int:
        """未作成 embedding の合計件数を返します。"""
        return self.text_pending_count + self.image_pending_count

    @property
    def ready(self) -> bool:
        """後続処理に必要な準備が済んでいるかを返します。旧 schema の行が残る文書は検索対象外なので ready にしない。"""
        return self.child_count > 0 and self.pending_count == 0 and self.legacy_row_count == 0


@dataclass(frozen=True)
class StoredChunk:
    """ADB から読み出したチャンク本文と検索用 metadata を保持します。"""

    chunk_uid: str
    chunk_id: str
    chunk_level: str
    chunk_seq: int
    parent_chunk_uid: str
    parent_chunk_id: str
    child_chunk_ids: tuple[str, ...]
    text: str
    retrieval_text: str
    source_run_id: str
    source_file_name: str
    source_engine_id: str
    source_engine_label: str
    page_start: int
    page_end: int
    source_seq_ranges: tuple[dict[str, int], ...]
    source_record_refs: tuple[dict[str, Any], ...]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class HybridSearchResult:
    """hybrid search の順位、score、由来 channel を保持します。"""

    child_chunks: list[StoredChunk]
    all_chunks: list[StoredChunk]
