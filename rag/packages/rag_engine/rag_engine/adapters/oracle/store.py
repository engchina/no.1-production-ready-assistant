"""ADB に保存したチャンク embedding を使う hybrid search と保存処理。"""

from __future__ import annotations

import array
import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from rag_engine.adapters.model_api import (
    EMBEDDING_INPUT_TYPE_SEARCH_DOCUMENT,
    EMBEDDING_INPUT_TYPE_SEARCH_QUERY,
    _is_retryable_error,
    embed_images,
    embed_query,
    embed_texts,
)
from rag_engine.adapters.oracle.connection import connect_adb_thin, load_adb_settings
from rag_engine.chunking import (
    CHILD_CHUNK_LEVEL,
    CHUNK_METADATA_SCHEMA_VERSION,
    PARENT_CHUNK_LEVEL,
    ChunkingResult,
    DocumentChunk,
    load_latest_or_source_chunk_run,
)
from rag_engine.config import DEFAULT_EMBEDDING_OUTPUT_DIMENSIONS, DEFAULT_TIMEZONE, RRF_K, Settings
from rag_engine.knowledge.classification import (
    DEFAULT_CATEGORY_VALUE,
    ClassificationFilter,
    business_today,
    classification_filter_from_values,
    with_effective_as_of,
)
from rag_engine.knowledge.domain_keywords import load_domain_keywords
from rag_engine.models.storage import (
    AdbEmbeddingsUnavailable,
    AdbHybridSearchUnavailable,
    EmbeddingSaveResult,
    EmbeddingStatusResult,
    HybridSearchResult,
    StoredChunk,
)
from rag_engine.parsing.decorative_pictures import (
    DECORATIVE_VISUAL_ROLE,
    INLINE_ICON_VISUAL_ROLE,
    visual_role_from_ref,
)
from rag_engine.parsing.picture_text import fold_picture_ocr_entries
from rag_engine.resources.tracing import observe_call
from rag_engine.retrieval.inquiry_conditions import (
    InquiryConditionParse,
    InquiryMetadataFilter,
    profile_channel_rankings,
)
from rag_engine.retrieval.scope import (
    RETRIEVAL_SCOPE_CURRENT_CHUNK_RUN,
    RETRIEVAL_SCOPE_KNOWLEDGE_BASE,
    AdbSearchReadiness,
    normalize_retrieval_scope,
)
from rag_engine.retrieval.search_stages import CHUNK_POOL_LOAD_STAGE, TEXT_SEARCH_STAGE, VECTOR_SEARCH_STAGE
from rag_engine.retrieval.text_search_tokenizer import (
    MAX_ORACLE_TEXT_QUERY_CHARS,
    MAX_TEXT_SEARCH_TOKENS,
    TextSearchTokenizerConfig,
    oracle_text_query_for_question,
)

# 原質問以外の検索文（用語補助・LLM拡張・語彙・CRAGの書き換え）全体の票の合計。
# 各検索文を 1.0 で足すと、検索文が10本前後のとき原質問の票が1割になり、無関係な
# 拡張語が1本入るだけで多数決が別機能へ寄る。原質問を主軸に保ち、派生は補助に留める。
DERIVED_QUERY_TOTAL_WEIGHT = 1.0
# ponytail: プロセス内の単純な TTL キャッシュ。CRAG の各回（最大3回）が同じ検索範囲の
# 全 chunk を読み直すのを避ける。複数プロセス構成や即時反映が必要になれば、保存側の
# 版番号をキーに含める形へ置き換える。
POOL_CACHE_TTL_SECONDS = 120.0
_pool_cache: dict[tuple[Any, ...], tuple[float, list[StoredChunk]]] = {}
ADB_EMBEDDING_DIMENSIONS = DEFAULT_EMBEDDING_OUTPUT_DIMENSIONS
MAX_ORACLE_TEXT_TERMS = MAX_TEXT_SEARCH_TOKENS
EMBEDDING_MODALITY_TEXT = "text"
EMBEDDING_MODALITY_IMAGE = "image"


EmbeddingFunction = Callable[..., list[list[float]]]
ImageEmbeddingFunction = Callable[..., list[list[float]]]
QueryEmbeddingFunction = Callable[[str, Settings], list[float]]


@dataclass(frozen=True)
class _ImageEmbeddingCandidate:
    chunk: DocumentChunk
    image_path: Path
    image_id: str
    image_hash: str


@dataclass
class _HybridScore:
    chunk_uid: str
    rrf_score: float = 0.0
    vector_rank: int | None = None
    text_rank: int | None = None
    profile_rank: int | None = None
    vector_distance: float | None = None
    text_score: float | None = None
    profile_score: float | None = None
    first_query_index: int = 1_000_000
    channels: set[str] = field(default_factory=set)
    channel_ranks: dict[str, int] = field(default_factory=dict)
    channel_scores: dict[str, float] = field(default_factory=dict)
    # 融合順位では候補上限から外れたが、いずれかの検索質問の上位として候補に残した理由（"vector:q3" など）。
    reserved_by: list[str] = field(default_factory=list)


# 各検索質問（原質問・派生・書き換え）の vector / keyword ごとに候補へ必ず残す上位件数 (#669)。
RESERVED_CANDIDATES_PER_QUERY = 3


def _reserve_query_top_candidates(
    scores: Sequence[_HybridScore],
    vector_rankings: Sequence[Sequence[tuple[str, Any]]],
    text_rankings: Sequence[Sequence[tuple[str, Any]]],
    *,
    is_candidate: Callable[[str], bool],
    limit: int,
    per_query: int = RESERVED_CANDIDATES_PER_QUERY,
) -> list[_HybridScore]:
    """融合順位の上位 limit 件に、各検索質問の上位 per_query 件を足した候補列を返す。

    RRF は多くの質問に一致する chunk を優遇するため、要求ごとに答えが別の節にある問い合わせでは、
    1 つの派生質問にしか一致しない節が候補上限の外へ落ちる（「①出荷明細なし ②担当者なし」のような複数条件の質問で、
    2 つ目の条件の節の chunk がサブ質問単独では 1 位、融合では 15 位・24 位）。質問ごとの順位は
    channel の順位として保持されているので、候補（child）の上位 per_query 件を末尾に追加する。
    追加した chunk は `reserved_by` にその質問と channel を記録する。融合順位自体は変えない。
    """
    by_uid = {score.chunk_uid: score for score in scores}
    chosen: list[_HybridScore] = list(scores[:limit])
    chosen_uids = {score.chunk_uid for score in chosen}
    for name, channel_rankings in (("vector", vector_rankings), ("keyword", text_rankings)):
        for query_index, ranking in enumerate(channel_rankings):
            kept = 0
            for chunk_uid, _ in ranking:
                if kept >= per_query:
                    break
                score = by_uid.get(chunk_uid)
                if score is None or not is_candidate(chunk_uid):
                    continue
                kept += 1
                if chunk_uid in chosen_uids:
                    continue
                score.reserved_by.append(f"{name}:q{query_index + 1}")
                chosen.append(score)
                chosen_uids.add(chunk_uid)
    return chosen


def save_latest_chunk_embeddings(
    *,
    output_dir: str | Path,
    run_id: Any,
    settings: Settings,
    preferred_engine_ids: Iterable[str] = (),
    embedder: EmbeddingFunction = embed_texts,
    image_embedder: ImageEmbeddingFunction = embed_images,
) -> EmbeddingSaveResult:
    """指定 run の最新チャンク実行に対して embedding 保存を実行します。"""
    chunk_run = load_latest_or_source_chunk_run(output_dir, run_id)
    if chunk_run is None:
        raise ValueError("Run chunking before creating and saving embeddings.")
    return save_chunk_run_embeddings(
        chunk_run,
        settings,
        preferred_engine_ids=preferred_engine_ids,
        embedder=embedder,
        image_embedder=image_embedder,
    )


def save_chunk_run_embeddings(
    chunk_run: ChunkingResult,
    settings: Settings,
    *,
    preferred_engine_ids: Iterable[str] = (),
    embedder: EmbeddingFunction = embed_texts,
    image_embedder: ImageEmbeddingFunction = embed_images,
    connection: Any | None = None,
) -> EmbeddingSaveResult:
    """active child chunk の text/image embedding を ADB に保存します。"""
    _pool_cache.clear()  # 保存後の検索で古い chunk 一覧を使わない
    selected_chunks = _active_chunks(chunk_run.chunks, preferred_engine_ids)
    children = [chunk for chunk in selected_chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]
    parents = [chunk for chunk in selected_chunks if chunk.chunk_level == PARENT_CHUNK_LEVEL]
    document_id = _document_id(chunk_run)
    if not children:
        return EmbeddingSaveResult(
            source_run_id=chunk_run.source_run_id,
            chunk_run_id=chunk_run.chunk_run_id,
            document_id=document_id,
            child_count=0,
            parent_count=len(parents),
            created_count=0,
            skipped_count=0,
            error_count=1,
            errors=("選択したエンジンに active child chunk がないため、Embedding を保存できません。",),
        )

    if connection is None:
        adb_settings = _connection_settings(settings)
        with connect_adb_thin(adb_settings) as owned_connection:
            return save_chunk_run_embeddings(
                chunk_run,
                settings,
                preferred_engine_ids=preferred_engine_ids,
                embedder=embedder,
                image_embedder=image_embedder,
                connection=owned_connection,
            )

    created_count = 0
    skipped_count = 0
    image_created_count = 0
    image_skipped_count = 0
    image_candidates: list[_ImageEmbeddingCandidate] = []
    image_missing_asset_count = 0
    kept_latest_chunk_run_id = ""
    errors: list[str] = []
    # Oracle は空文字を NULL として扱うため、空の検索 text は NOT NULL 違反で文書全体を失敗させる。
    blank_ids = {chunk.chunk_id for chunk in selected_chunks if not _retrieval_text(chunk).strip()}
    if blank_ids:
        errors.extend(f"{chunk_id}: 検索 text が空のため保存しません。" for chunk_id in sorted(blank_ids))
        selected_chunks = [chunk for chunk in selected_chunks if chunk.chunk_id not in blank_ids]
        children = [chunk for chunk in children if chunk.chunk_id not in blank_ids]
    try:
        # 途中失敗した run を公開しないよう、既存文書の latest は全 batch の完了後に切り替える。
        _upsert_document(connection, chunk_run, document_id, publish=False)
        _upsert_chunk_run(connection, chunk_run, document_id, children, parents)
        # 今回の保存対象から外れた chunk（外した engine など）を検索に残さない。対象の chunk は
        # 直後の MERGE が active='Y' に戻す。同じ transaction なので途中の状態は検索から見えない。
        connection.cursor().execute(
            "UPDATE rag_chunks SET active = 'N', updated_at_utc = SYSTIMESTAMP WHERE chunk_run_id = :chunk_run_id AND active = 'Y'",
            {"chunk_run_id": chunk_run.chunk_run_id},
        )
        _upsert_chunks(connection, chunk_run, document_id, selected_chunks)
        _prune_stale_text_embeddings(connection, chunk_run, children, settings)

        existing_hashes = _existing_embedding_hashes(
            connection,
            [_chunk_uid(chunk_run.chunk_run_id, chunk.chunk_id) for chunk in children],
            settings.embedding_model,
            _adb_embedding_dimensions(settings),
            embedding_modality=_text_modality_filter(connection),
        )
        missing = [
            chunk
            for chunk in children
            if _retrieval_text_hash(chunk) not in existing_hashes.get(_chunk_uid(chunk_run.chunk_run_id, chunk.chunk_id), set())
        ]
        skipped_count = len(children) - len(missing)

        for batch in _batches(missing, max(1, int(settings.embedding_batch_size or 1))):
            saved, batch_errors = _embed_isolating_failures(
                batch,
                lambda items: embedder(
                    [_retrieval_text(chunk) for chunk in items],
                    settings,
                    input_type=EMBEDDING_INPUT_TYPE_SEARCH_DOCUMENT,
                ),
                label=lambda chunk: chunk.chunk_id,
            )
            errors.extend(batch_errors)
            if saved:
                _upsert_embeddings(connection, chunk_run, [chunk for chunk, _ in saved], [vector for _, vector in saved], settings)
            # batch ごとに確定し、後続の失敗で取得済み（課金済み）の embedding を失わない。
            connection.commit()
            created_count += len(saved)

        if _image_embedding_enabled(settings):
            image_candidates, image_missing_asset_count, unresolved_image_chunk_ids = _image_embedding_candidates(
                children,
                output_dir=settings.output_dir,
            )
            _prune_image_embeddings(
                connection,
                chunk_run,
                children,
                image_candidates,
                settings,
                preserve_chunk_ids=unresolved_image_chunk_ids,
            )
            existing_image_hashes = _existing_embedding_hashes(
                connection,
                [_chunk_uid(chunk_run.chunk_run_id, candidate.chunk.chunk_id) for candidate in image_candidates],
                settings.embedding_model,
                _adb_embedding_dimensions(settings),
                embedding_modality=EMBEDDING_MODALITY_IMAGE,
            )
            missing_image_candidates = [
                candidate
                for candidate in image_candidates
                if candidate.image_hash
                not in existing_image_hashes.get(_chunk_uid(chunk_run.chunk_run_id, candidate.chunk.chunk_id), set())
            ]
            image_skipped_count = len(image_candidates) - len(missing_image_candidates)
            skipped_count += image_skipped_count
            for batch in _batches(missing_image_candidates, max(1, int(settings.embedding_batch_size or 1))):
                saved_images, batch_errors = _embed_isolating_failures(
                    batch,
                    lambda items: image_embedder(
                        [candidate.image_path for candidate in items],
                        settings,
                        texts=[_retrieval_text(candidate.chunk) for candidate in items],
                        input_type=EMBEDDING_INPUT_TYPE_SEARCH_DOCUMENT,
                    ),
                    label=lambda candidate: f"{candidate.chunk.chunk_id} image {candidate.image_id}",
                )
                errors.extend(batch_errors)
                if saved_images:
                    _upsert_image_embeddings(
                        connection,
                        chunk_run,
                        [candidate for candidate, _ in saved_images],
                        [vector for _, vector in saved_images],
                        settings,
                    )
                connection.commit()
                image_created_count += len(saved_images)
                created_count += len(saved_images)

        kept_latest_chunk_run_id = _newer_published_chunk_run_id(connection, document_id, chunk_run)
        if not kept_latest_chunk_run_id:
            _upsert_document(connection, chunk_run, document_id, publish=True)
        _deactivate_superseded_chunk_runs(connection, document_id, kept_latest_chunk_run_id or chunk_run.chunk_run_id)
        connection.commit()
        # 冒頭の clear から commit までに走った検索が、保存前の chunk 一覧を cache し直している場合がある。
        _pool_cache.clear()
    except Exception as exc:
        # 未確定の batch だけを戻す。確定済みの embedding は次回実行で hash 一致により skip される。
        _rollback(connection)
        _pool_cache.clear()  # 確定済みの batch があるため、失敗時も古い chunk 一覧を残さない
        return EmbeddingSaveResult(
            source_run_id=chunk_run.source_run_id,
            chunk_run_id=chunk_run.chunk_run_id,
            document_id=document_id,
            child_count=len(children),
            parent_count=len(parents),
            created_count=created_count,
            skipped_count=skipped_count,
            error_count=max(1, len(children) + len(image_candidates) - skipped_count - created_count),
            errors=(*errors, _short_error(exc)),
            image_candidate_count=len(image_candidates),
            image_created_count=image_created_count,
            image_skipped_count=image_skipped_count,
            image_missing_asset_count=image_missing_asset_count,
            embedding_model=settings.embedding_model,
            embedding_dimensions=settings.embedding_output_dimensions,
        )

    return EmbeddingSaveResult(
        source_run_id=chunk_run.source_run_id,
        chunk_run_id=chunk_run.chunk_run_id,
        document_id=document_id,
        child_count=len(children),
        parent_count=len(parents),
        created_count=created_count,
        skipped_count=skipped_count,
        error_count=len(errors),
        errors=tuple(errors),
        image_candidate_count=len(image_candidates),
        image_created_count=image_created_count,
        image_skipped_count=image_skipped_count,
        image_missing_asset_count=image_missing_asset_count,
        kept_latest_chunk_run_id=kept_latest_chunk_run_id,
        published=not kept_latest_chunk_run_id,
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_output_dimensions,
    )


def delete_source_documents(
    settings: Settings,
    *,
    source_file_sha256: str,
    source_file_name: str,
    document_ids: Sequence[str] = (),
    connection: Any | None = None,
) -> int:
    """アップロード元ファイルに紐づく文書を ADB から削除し、削除した文書数を返します。

    rag_documents を消すと外部キーの ON DELETE CASCADE で chunk run・chunk・embedding も消える。
    内容の SHA-256 とファイル名が一致する行に加え、ローカルの chunk run から求めた document_ids
    （SHA を保存していない過去の行）も対象にする。接続・実行の例外は呼び出し元へ伝播する。
    """
    if connection is None:
        with connect_adb_thin(_connection_settings(settings)) as owned_connection:
            return delete_source_documents(
                settings,
                source_file_sha256=source_file_sha256,
                source_file_name=source_file_name,
                document_ids=document_ids,
                connection=owned_connection,
            )
    binds: dict[str, Any] = {"source_file_sha256": source_file_sha256, "source_file_name": source_file_name}
    conditions = ["(source_file_sha256 = :source_file_sha256 AND source_file_name = :source_file_name)"]
    ids = {f"document_id_{index}": value for index, value in enumerate(dict.fromkeys(document_ids))}
    if ids:
        binds.update(ids)
        conditions.append(f"document_id IN ({', '.join(':' + key for key in ids)})")
    cursor = connection.cursor()
    try:
        cursor.execute(f"DELETE FROM rag_documents WHERE {' OR '.join(conditions)}", binds)
        deleted = int(getattr(cursor, "rowcount", 0) or 0)
        connection.commit()
    except Exception:
        _rollback(connection)
        raise
    _pool_cache.clear()  # 削除した文書の chunk 一覧を検索に残さない
    return deleted


def _deactivate_superseded_chunk_runs(connection: Any, document_id: str, published_chunk_run_id: str) -> None:
    """文書の公開中 run 以外の chunk と chunk run を停用します。

    検索は `_scope_sql` で `latest_chunk_run_id` 一致を要求するため公開中 run だけを見るが、
    `active` はそれと独立に残るため「公開されていないのに active」な行が再チャンキングのたびに積もる。
    `active` が公開状態と一致していないと、運用調査の集計や chunk run 指定の検索が実態とずれる。
    呼び出し元の transaction に載せ、公開の切り替えと同時に確定させる。
    """
    if not published_chunk_run_id:
        return
    binds = {"document_id": document_id, "chunk_run_id": published_chunk_run_id}
    cursor = connection.cursor()
    cursor.execute(
        "UPDATE rag_chunks SET active = 'N', updated_at_utc = SYSTIMESTAMP "
        "WHERE document_id = :document_id AND chunk_run_id <> :chunk_run_id AND active = 'Y'",
        binds,
    )
    cursor.execute(
        "UPDATE rag_chunk_runs SET active = 'N', updated_at_utc = SYSTIMESTAMP "
        "WHERE document_id = :document_id AND chunk_run_id <> :chunk_run_id AND active = 'Y'",
        binds,
    )


def _newer_published_chunk_run_id(connection: Any, document_id: str, chunk_run: ChunkingResult) -> str:
    """公開中の run が chunk_run より後に作成されていればその ID を返します。

    古い run の再保存で knowledge base の検索対象が黙って巻き戻るのを防ぐ。作成時刻を
    記録していない過去の文書行は比較できないため、従来どおり保存した run を公開する。
    """
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT latest_chunk_run_id,
               JSON_VALUE(metadata_json, '$.chunk_run_created_at_utc' NULL ON ERROR)
        FROM rag_documents
        WHERE document_id = :document_id
        """,
        {"document_id": document_id},
    )
    row = cursor.fetchone()
    if not row:
        return ""
    published_run_id, published_created_at = str(row[0] or ""), str(row[1] or "")
    if published_run_id == chunk_run.chunk_run_id or published_created_at <= str(chunk_run.created_at_utc or ""):
        return ""
    return published_run_id


def _embed_isolating_failures[T](
    items: Sequence[T],
    embed: Callable[[Sequence[T]], list[list[float]]],
    *,
    label: Callable[[T], str],
) -> tuple[list[tuple[T, list[float]]], list[str]]:
    """batch を embedding し、入力起因の失敗は 1 件ずつ再実行して問題の item だけを除外します。

    返り値は (保存できる item と vector の対, 除外した item の error)。retry 対象の一時障害と、
    複数件の batch が全件失敗した場合（設定・認証の誤り）は呼び出し元へ送出し、保存を中断します。
    """

    def checked(batch: Sequence[T]) -> list[list[float]]:
        vectors = embed(batch)
        if len(vectors) != len(batch):
            raise RuntimeError(f"Embedding count mismatch: expected {len(batch)}, got {len(vectors)}")
        return vectors

    try:
        return list(zip(items, checked(items), strict=False)), []
    except Exception as exc:
        if _is_retryable_error(exc):
            raise
        if len(items) == 1:
            return [], [f"{label(items[0])}: {_short_error(exc)}"]
    saved: list[tuple[T, list[float]]] = []
    errors: list[str] = []
    for item in items:
        try:
            saved.append((item, checked([item])[0]))
        except Exception as exc:
            if _is_retryable_error(exc):
                raise
            errors.append(f"{label(item)}: {_short_error(exc)}")
    if not saved:
        raise RuntimeError(errors[0])
    return saved, errors


def list_embedded_source_files(settings: Settings, *, connection: Any | None = None) -> set[tuple[str, str]]:
    """現在の embedding model / dimensions で Embedding が 1 件以上ある文書の (SHA-256, ファイル名) を返します。

    アップロード済み一覧の Embedding 列に使う。一覧の表示ごとに 1 回だけ実行し、ファイルごとには問い合わせない。
    接続・実行の例外は呼び出し元へ伝播する（呼び出し元が「未確認」として扱う）。
    """
    if connection is None:
        with connect_adb_thin(_connection_settings(settings)) as owned_connection:
            return list_embedded_source_files(settings, connection=owned_connection)
    cursor = connection.cursor()
    # ponytail: SHA を保存していない過去の rag_documents 行は数えない。必要になれば document_id でも照合する。
    cursor.execute(
        """
        SELECT DISTINCT d.source_file_sha256, d.source_file_name
        FROM rag_documents d
        JOIN rag_chunks c ON c.document_id = d.document_id
        JOIN rag_chunk_embeddings e ON e.chunk_uid = c.chunk_uid
        WHERE d.source_file_sha256 IS NOT NULL
          AND e.embedding_model = :embedding_model
          AND e.embedding_dimensions = :embedding_dimensions
        """,
        {"embedding_model": settings.embedding_model, "embedding_dimensions": _adb_embedding_dimensions(settings)},
    )
    return {(str(sha256), str(name)) for sha256, name in cursor}


def load_chunk_embedding_status(
    chunk_run: ChunkingResult,
    settings: Settings,
    *,
    preferred_engine_ids: Iterable[str] = (),
    connection: Any | None = None,
) -> EmbeddingStatusResult:
    """チャンク run の embedding 保存状況を ADB から集計します。"""
    selected_chunks = _active_chunks(chunk_run.chunks, preferred_engine_ids)
    children = [chunk for chunk in selected_chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]
    parents = [chunk for chunk in selected_chunks if chunk.chunk_level == PARENT_CHUNK_LEVEL]
    document_id = _document_id(chunk_run)

    if connection is None:
        adb_settings = _connection_settings(settings)
        with connect_adb_thin(adb_settings) as owned_connection:
            return load_chunk_embedding_status(
                chunk_run,
                settings,
                preferred_engine_ids=preferred_engine_ids,
                connection=owned_connection,
            )

    existing_hashes = _existing_embedding_hashes(
        connection,
        [_chunk_uid(chunk_run.chunk_run_id, chunk.chunk_id) for chunk in children],
        settings.embedding_model,
        _adb_embedding_dimensions(settings),
        # image 行だけがある chunk を stale ではなく missing と数える。
        embedding_modality=_text_modality_filter(connection),
    )
    embedded_count = 0
    missing_count = 0
    stale_count = 0
    for chunk in children:
        hashes = existing_hashes.get(_chunk_uid(chunk_run.chunk_run_id, chunk.chunk_id), set())
        if not hashes:
            missing_count += 1
        elif _retrieval_text_hash(chunk) in hashes:
            embedded_count += 1
        else:
            stale_count += 1

    image_embedding_enabled = _image_embedding_enabled(settings)
    image_candidates: list[_ImageEmbeddingCandidate] = []
    image_embedded_count = 0
    image_missing_count = 0
    image_stale_count = 0
    image_missing_asset_count = 0
    if image_embedding_enabled:
        image_candidates, image_missing_asset_count, _ = _image_embedding_candidates(
            children,
            output_dir=settings.output_dir,
        )
        existing_image_hashes = _existing_embedding_hashes(
            connection,
            [_chunk_uid(chunk_run.chunk_run_id, candidate.chunk.chunk_id) for candidate in image_candidates],
            settings.embedding_model,
            _adb_embedding_dimensions(settings),
            embedding_modality=EMBEDDING_MODALITY_IMAGE,
        )
        for candidate in image_candidates:
            hashes = existing_image_hashes.get(
                _chunk_uid(chunk_run.chunk_run_id, candidate.chunk.chunk_id),
                set(),
            )
            if not hashes:
                image_missing_count += 1
            elif candidate.image_hash in hashes:
                image_embedded_count += 1
            else:
                image_stale_count += 1

    return EmbeddingStatusResult(
        source_run_id=chunk_run.source_run_id,
        chunk_run_id=chunk_run.chunk_run_id,
        document_id=document_id,
        legacy_row_count=_legacy_schema_row_count(connection, document_id),
        child_count=len(children),
        parent_count=len(parents),
        embedded_count=embedded_count,
        missing_count=missing_count,
        stale_count=stale_count,
        image_embedding_enabled=image_embedding_enabled,
        image_candidate_count=len(image_candidates),
        image_embedded_count=image_embedded_count,
        image_missing_count=image_missing_count,
        image_stale_count=image_stale_count,
        image_missing_asset_count=image_missing_asset_count,
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_output_dimensions,
    )


def _legacy_schema_row_count(connection: Any, document_id: str) -> int:
    """文書の active な chunk 行のうち metadata_json.schema_version が現行版でない件数 (#819)。

    検索は `schema_version = CHUNK_METADATA_SCHEMA_VERSION` で絞るため、この行は無言で検索対象外になる。
    再チャンキングと「Embedding作成・ADB保存」の再実行が必要なことを状態表示で案内する。
    """
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM rag_chunks c
        WHERE c.document_id = :document_id
          AND c.active = 'Y'
          AND COALESCE(JSON_VALUE(c.metadata_json, '$.schema_version' RETURNING NUMBER NULL ON ERROR), 0)
              <> :chunk_metadata_schema_version
        """,
        {"document_id": document_id, "chunk_metadata_schema_version": CHUNK_METADATA_SCHEMA_VERSION},
    )
    row = next(iter(cursor), None)
    return int(row[0] or 0) if row else 0


def _legacy_schema_document_count(
    connection: Any,
    chunk_run_id: str,
    *,
    retrieval_scope: str = RETRIEVAL_SCOPE_CURRENT_CHUNK_RUN,
) -> int:
    """検索範囲内で、active な chunk 行の metadata_json.schema_version が現行版でない文書の数 (#948)。

    範囲の絞り方は検索 SQL と同じ `_scope_sql`。分類フィルタと engine の絞り込みは掛けない
    （旧 schema の行は分類の保存場所も違い、フィルタで評価できないため、範囲内の総数を示す）。
    """
    scope = normalize_retrieval_scope(retrieval_scope)
    scope_join_sql, scope_where_sql, scope_binds = _scope_sql("c", scope, chunk_run_id)
    cursor = connection.cursor()
    cursor.execute(
        f"""
        SELECT COUNT(DISTINCT c.document_id)
        FROM rag_chunks c
        {scope_join_sql}
        WHERE c.chunk_level = :chunk_level
          AND c.active = 'Y'
          AND COALESCE(JSON_VALUE(c.metadata_json, '$.schema_version' RETURNING NUMBER NULL ON ERROR), 0)
              <> :chunk_metadata_schema_version
          {scope_where_sql}
        """,
        {"chunk_level": CHILD_CHUNK_LEVEL, "chunk_metadata_schema_version": CHUNK_METADATA_SCHEMA_VERSION, **scope_binds},
    )
    row = next(iter(cursor), None)
    return int(row[0] or 0) if row else 0


def _embedding_model_line(model: str, dimensions: int) -> str:
    """表示用の「embedding モデル（model）」行。既存データと混在できるかの判断材料 (#816)。"""
    label = model or "未設定"
    return f"- embedding モデル（model）: {label}" + (f" / {dimensions} 次元（dimensions）" if dimensions else "")


def format_embedding_status_result(result: EmbeddingStatusResult) -> str:
    """embedding 状態 結果を表示用に整形します。

    ラベルは日本語（英語）の併記。直上のチャンキング結果と重複する子 / 親チャンク数は出さない (#816)。
    """
    if result.legacy_row_count:
        status = (
            "旧 schema（legacy）: この文書の ADB 行は現行の chunk metadata 版でないため検索対象外です。"
            "再チャンキングのうえ「Embedding作成・ADB保存」を再実行してください"
        )
    elif result.ready:
        status = "検索可能（ready）"
    elif result.embedded_count > 0 or result.image_embedded_count > 0:
        status = "一部のみ（partial）: 未保存・再作成待ちの embedding があります。「Embedding作成・ADB保存」を実行してください"
    else:
        status = "未保存（not saved）: 「Embedding作成・ADB保存」を実行してください"

    lines = [
        "### Embedding / ADB 状態（status）",
        f"- チャンク実行 ID（Chunk Run ID）: `{result.chunk_run_id}`",
        f"- 文書 ID（Document ID）: `{result.document_id}`",
        _embedding_model_line(result.embedding_model, result.embedding_dimensions),
        f"- 本文 embedding 保存済み（saved text embeddings）: {result.embedded_count} / {result.child_count}",
        f"- 本文 embedding 未保存（missing）: {result.missing_count}",
        f"- 本文 embedding 再作成待ち（stale）: {result.stale_count}",
    ]
    if result.image_embedding_enabled:
        lines.extend(
            [
                "- 画像 embedding（image embeddings）: 有効（enabled）",
                f"- 画像 embedding 候補（candidates）: {result.image_candidate_count}",
                f"- 画像 embedding 保存済み（saved）: {result.image_embedded_count} / {result.image_candidate_count}",
                f"- 画像 embedding 未保存（missing）: {result.image_missing_count}",
                f"- 画像 embedding 再作成待ち（stale）: {result.image_stale_count}",
                f"- 画像ファイル欠落（image assets missing）: {result.image_missing_asset_count}",
            ]
        )
    if result.legacy_row_count:
        lines.append(f"- 旧 schema の chunk 行（legacy rows）: {result.legacy_row_count} 件")
    lines.append(f"- 状態（status）: {status}")
    return "\n".join(lines)


def format_embedding_save_result(result: EmbeddingSaveResult) -> str:
    """embedding save 結果を表示用に整形します。

    ラベルは日本語（英語）の併記。knowledge base への公開状態は結果にかかわらず 1 行出す (#816)。
    """
    lines = [
        "### Embedding / ADB 保存結果（save result）",
        f"- チャンク実行 ID（Chunk Run ID）: `{result.chunk_run_id}`",
        f"- 文書 ID（Document ID）: `{result.document_id}`",
        _embedding_model_line(result.embedding_model, result.embedding_dimensions),
        f"- 作成・更新した embedding（created/updated）: {result.created_count}",
        f"- 再利用した embedding（skipped）: {result.skipped_count}",
        f"- エラー（errors）: {result.error_count}",
    ]
    if result.image_candidate_count or result.image_missing_asset_count:
        lines.extend(
            [
                f"- 画像 embedding 候補（candidates）: {result.image_candidate_count}",
                f"- 画像 embedding 作成・更新（created/updated）: {result.image_created_count}",
                f"- 画像 embedding 再利用（skipped）: {result.image_skipped_count}",
                f"- 画像ファイル欠落（image assets missing）: {result.image_missing_asset_count}",
            ]
        )
    if result.kept_latest_chunk_run_id:
        published = f"より新しい run `{result.kept_latest_chunk_run_id}` を公開中のため、この run は検索対象に切り替えていません"
    elif result.published:
        published = "この run を検索対象として公開しました"
    else:
        published = "エラーで中断したため公開していません（確定済みの embedding は次回の保存で再利用されます）"
    lines.append(f"- knowledge base 公開（published）: {published}")
    lines.extend(f"- エラー詳細（error detail）: {error}" for error in result.errors)
    return "\n".join(lines)


def check_adb_hybrid_search_ready(
    *,
    chunk_run_id: str = "",
    settings: Settings,
    preferred_engine_ids: Iterable[str] = (),
    retrieval_scope: str = RETRIEVAL_SCOPE_CURRENT_CHUNK_RUN,
    classification_filter: ClassificationFilter | None = None,
) -> AdbSearchReadiness:
    """ADB hybrid search の前提条件を検査し、不足時は説明付き例外を送出します。

    現行版の Embedding が範囲内に 1 件もなければ例外。旧 schema の文書が混在するだけでは例外にせず、
    その文書数を戻り値で返す（検索は続行できるが、それらの文書は無言で検索対象外になるため）。
    """
    try:
        adb_settings = _connection_settings(settings)
        with connect_adb_thin(adb_settings) as connection:
            count = _available_embedding_count(
                connection,
                chunk_run_id,
                settings.embedding_model,
                _adb_embedding_dimensions(settings),
                preferred_engine_ids,
                retrieval_scope=retrieval_scope,
                classification_filter=with_effective_as_of(classification_filter, business_today(settings.timezone)),
            )
            legacy_documents = _legacy_schema_document_count(connection, chunk_run_id, retrieval_scope=retrieval_scope)
    except Exception as exc:
        raise AdbHybridSearchUnavailable(
            "ADB hybrid search is unavailable. Create the schema and configure ADB first: " + _short_error(exc)
        ) from exc
    if count <= 0:
        raise AdbEmbeddingsUnavailable("No ADB embeddings were found for the selected retrieval scope. Run Embedding/ADB save first.")
    return AdbSearchReadiness(legacy_document_count=legacy_documents)


def search_adb_hybrid_chunks(
    *,
    chunk_run_id: str = "",
    retrieval_queries: Sequence[str],
    settings: Settings,
    preferred_engine_ids: Iterable[str] = (),
    candidate_limit: int = 24,
    query_embedder: QueryEmbeddingFunction = embed_query,
    inquiry_conditions: InquiryConditionParse | None = None,
    retrieval_scope: str = RETRIEVAL_SCOPE_CURRENT_CHUNK_RUN,
    classification_filter: ClassificationFilter | None = None,
    extra_text_queries: Sequence[str] = (),
    vector_only_queries: Sequence[str] = (),
) -> HybridSearchResult:
    """text vector、Oracle Text、任意 image vector を RRF で融合してチャンクを検索します。

    extra_text_queries は組み立て済みの Oracle Text 検索式（`{基準月}` など。#730 の対象語の部分語）。
    質問文の変種とは別に Oracle Text だけで検索し、派生検索文と同じ重みで融合する。各式の上位
    `RESERVED_CANDIDATES_PER_QUERY` 件は候補に確保される。
    vector_only_queries は retrieval_queries のうち vector（image vector を含む）検索だけに使い、
    Oracle Text 検索には使わない検索文（HyDE の仮説文。#911）。融合の重みは検索文の位置で決まり変わらない。
    """
    queries = _dedupe_queries(retrieval_queries)
    vector_only_keys = {_query_key(query) for query in vector_only_queries}
    if not queries:
        return HybridSearchResult(child_chunks=[], all_chunks=[])
    preferred_engine_ids = tuple(preferred_engine_ids)
    scope = normalize_retrieval_scope(retrieval_scope)
    # 基準日はこの検索の初めに 1 回だけ決める(#3249。未指定なら設定の時間帯の今日)。
    category_filter = with_effective_as_of(classification_filter or classification_filter_from_values(), business_today(settings.timezone))

    try:
        adb_settings = _connection_settings(settings)
        with connect_adb_thin(adb_settings) as connection:
            pool_key = (
                hashlib.sha256(repr(adb_settings).encode()).hexdigest(),
                chunk_run_id,
                scope,
                tuple(preferred_engine_ids),
                repr(category_filter),
                settings.embedding_model,
                _adb_embedding_dimensions(settings),
            )
            cached = _pool_cache.get(pool_key)
            all_chunks = cached[1] if cached and time.monotonic() - cached[0] < POOL_CACHE_TTL_SECONDS else None
            # Embedding の有無は検索範囲ごとに1回確認すれば足りる。
            if all_chunks is None and (
                _available_embedding_count(
                    connection,
                    chunk_run_id,
                    settings.embedding_model,
                    _adb_embedding_dimensions(settings),
                    preferred_engine_ids,
                    retrieval_scope=scope,
                    classification_filter=category_filter,
                )
                <= 0
            ):
                raise AdbEmbeddingsUnavailable(
                    "No ADB embeddings were found for the selected retrieval scope. Run Embedding/ADB save first."
                )

            # 知識ベースに無い資料名で候補が 0 件になるのを避ける (#1082)。以降は解決済みの filter を使う。
            metadata_filter = _resolved_metadata_filter(
                connection,
                chunk_run_id,
                inquiry_conditions.metadata_filter if inquiry_conditions else None,
                retrieval_scope=scope,
            )
            rankings: list[list[tuple[str, float | None]]] = []
            image_rankings: list[list[tuple[str, float | None]]] = []
            limit = max(1, int(candidate_limit or 1))
            query_weights = derived_query_weights(len(queries), enabled=getattr(settings, "original_query_weighting_enabled", True))
            # 埋め込み（`gen_ai.embeddings` の span）の後の DB の検索は、それぞれ工程の span にする（処理の経過で、文書の
            # 検索の中の今の作業を末端の段として出す。#3436）。
            query_vectors = _query_vectors(queries, settings, query_embedder)
            with observe_call("stage", VECTOR_SEARCH_STAGE):
                for query_vector in query_vectors:
                    rankings.append(
                        _vector_search(
                            connection,
                            chunk_run_id,
                            query_vector,
                            settings,
                            preferred_engine_ids,
                            limit,
                            metadata_filter,
                            retrieval_scope=scope,
                            classification_filter=category_filter,
                        )
                    )
                    if _image_embedding_enabled(settings):
                        image_rankings.append(
                            _vector_search(
                                connection,
                                chunk_run_id,
                                query_vector,
                                settings,
                                preferred_engine_ids,
                                limit,
                                metadata_filter,
                                retrieval_scope=scope,
                                classification_filter=category_filter,
                                embedding_modality=EMBEDDING_MODALITY_IMAGE,
                            )
                        )
            text_rankings: list[list[tuple[str, float | None]]] = []
            text_query_weights: list[float] = []
            seen_text_queries: set[str] = set()
            derived_weight = query_weights[1] if len(query_weights) > 1 else 1.0
            domain_keywords = (
                settings.domain_keywords_override
                if settings.domain_keywords_override is not None
                else load_domain_keywords(settings.output_dir)
            )
            tokenizer_config = _text_search_tokenizer_config(settings)
            # 全文検索の上限は vector 専用の検索文（HyDE の仮説文）を除いてから数える (#921)。重みは元の位置で決める。
            text_candidates = [(index, query) for index, query in enumerate(queries) if _query_key(query) not in vector_only_keys]
            # 質問文の変種の全文検索と、組み立て済みの検索式（extra_text_queries）の全文検索を 1 つの工程にする
            # （融合の順と重みは変えない）。検索するものが無ければ工程を出さない。
            text_variants = text_candidates[: _text_search_query_variant_limit(settings)]
            with observe_call("stage", TEXT_SEARCH_STAGE) if text_variants or extra_text_queries else nullcontext():
                for query_index, query in text_variants:
                    text_query = oracle_text_query(
                        query,
                        domain_keywords=domain_keywords,
                        tokenizer_config=tokenizer_config,
                    )
                    if not text_query or text_query in seen_text_queries:
                        continue
                    seen_text_queries.add(text_query)
                    text_query_weights.append(query_weights[query_index])
                    text_rankings.append(
                        _text_search(
                            connection,
                            chunk_run_id,
                            text_query,
                            settings,
                            preferred_engine_ids,
                            limit,
                            metadata_filter,
                            retrieval_scope=scope,
                            classification_filter=category_filter,
                        )
                    )
                for text_query in extra_text_queries:
                    if not text_query or text_query in seen_text_queries:
                        continue
                    seen_text_queries.add(text_query)
                    text_query_weights.append(derived_weight)
                    text_rankings.append(
                        _text_search(
                            connection,
                            chunk_run_id,
                            text_query,
                            settings,
                            preferred_engine_ids,
                            limit,
                            metadata_filter,
                            retrieval_scope=scope,
                            classification_filter=category_filter,
                        )
                    )
            if all_chunks is None:
                with observe_call("stage", CHUNK_POOL_LOAD_STAGE):
                    all_chunks = _load_chunk_run_chunks(
                        connection,
                        chunk_run_id,
                        preferred_engine_ids,
                        retrieval_scope=scope,
                        classification_filter=category_filter,
                    )
                if len(_pool_cache) >= 8:
                    _pool_cache.clear()
                _pool_cache[pool_key] = (time.monotonic(), all_chunks)
            profile_source_chunks = _chunks_matching_metadata_filter(
                all_chunks,
                metadata_filter,
                category_filter,
            )
            # 比較実験用。質問理解の profile に合う chunk を第4のチャネルとして融合するかどうか。
            profile_rankings = (
                profile_channel_rankings(
                    profile_source_chunks,
                    inquiry_conditions,
                    limit=limit,
                )
                if getattr(settings, "profile_channel_enabled", True)
                else []
            )
            # 業務語の一致は SQL の除外条件ではなく、融合後の加点にする (#848)。
            business_rankings = _business_match_ranking(
                profile_source_chunks,
                metadata_filter,
                rankings,
                text_rankings,
                limit=limit,
            )
            scores = _combine_hybrid_scores(
                rankings,
                text_rankings,
                [*profile_rankings, *business_rankings],
                image_vector_rankings=image_rankings,
                image_vector_weight=_image_embedding_rrf_weight(settings),
                query_weights=query_weights,
                text_query_weights=text_query_weights,
            )
    except AdbHybridSearchUnavailable:
        raise
    except Exception as exc:
        raise AdbHybridSearchUnavailable("ADB hybrid search failed: " + _short_error(exc)) from exc

    by_uid = {chunk.chunk_uid: chunk for chunk in all_chunks}

    def is_child(chunk_uid: str) -> bool:
        chunk = by_uid.get(chunk_uid)
        return chunk is not None and chunk.chunk_level == CHILD_CHUNK_LEVEL

    candidates = _reserve_query_top_candidates(
        scores, rankings, text_rankings, is_candidate=is_child, limit=max(1, int(candidate_limit or 1))
    )
    child_scores: list[tuple[_HybridScore, StoredChunk]] = []
    for score in candidates:
        chunk = by_uid.get(score.chunk_uid)
        if chunk is None or chunk.chunk_level != CHILD_CHUNK_LEVEL:
            continue
        child_scores.append((score, chunk))
    child_scores.sort(key=_hybrid_chunk_sort_key)
    child_chunks = [_chunk_with_hybrid_metadata(chunk, score) for score, chunk in child_scores]
    return HybridSearchResult(child_chunks=child_chunks, all_chunks=all_chunks)


def _query_vectors(queries: Sequence[str], settings: Settings, query_embedder: QueryEmbeddingFunction) -> list[list[float]]:
    """検索文の embedding を検索文と同じ順で返す。

    既定の `embed_query` は 1 件ずつモデルの API へ往復するため、既定のままなら `embed_texts` で 1 回にまとめる
    （byte 上限を超える分は `embed_texts` が分割する）。差し替えられた embedder は契約どおり 1 件ずつ呼ぶ (#876)。
    """
    if query_embedder is embed_query:
        vectors = embed_texts(list(queries), settings, input_type=EMBEDDING_INPUT_TYPE_SEARCH_QUERY)
        if len(vectors) != len(queries):
            raise RuntimeError("Embedding query response count did not match the query count.")
        return vectors
    return [query_embedder(query, settings) for query in queries]


def derived_query_weights(count: int, *, enabled: bool = True) -> list[float]:
    """先頭（原質問）は 1.0、派生検索文は合計が DERIVED_QUERY_TOTAL_WEIGHT になるよう等分する。"""
    if not enabled or count <= 1:
        return [1.0] * count
    return [1.0] + [DERIVED_QUERY_TOTAL_WEIGHT / (count - 1)] * (count - 1)


def oracle_text_query(
    query: str,
    domain_keywords: Sequence[str] | None = None,
    tokenizer_config: TextSearchTokenizerConfig | None = None,
) -> str:
    """質問とドメインキーワードから Oracle Text CONTAINS query（重み付きの ACCUM。#588）を構築します。"""
    return oracle_text_query_for_question(
        query,
        domain_keywords=domain_keywords,
        config=tokenizer_config,
        max_tokens=MAX_ORACLE_TEXT_TERMS,
        max_chars=MAX_ORACLE_TEXT_QUERY_CHARS,
    )


def _text_search_tokenizer_config(settings: Settings) -> TextSearchTokenizerConfig:
    return TextSearchTokenizerConfig(
        sudachi_dict_type=settings.text_search_tokenizer_sudachi_dict,
        sudachi_config_path=settings.text_search_tokenizer_sudachi_config,
        latin_stemmer=settings.text_search_tokenizer_latin_stemmer,
    )


def _text_search_query_variant_limit(settings: Settings) -> int:
    return max(1, int(getattr(settings, "text_search_query_variant_limit", 6) or 6))


def _adb_embedding_dimensions(settings: Settings) -> int:
    dimensions = int(settings.embedding_output_dimensions)
    if dimensions != ADB_EMBEDDING_DIMENSIONS:
        raise RuntimeError(
            f"ADB embedding schema expects {ADB_EMBEDDING_DIMENSIONS} dimensions; "
            f"got {dimensions}. Rebuild the schema for a new embedding dimension."
        )
    return dimensions


def _active_chunks(chunks: Sequence[DocumentChunk], preferred_engine_ids: Iterable[str]) -> list[DocumentChunk]:
    preferred = {engine for engine in preferred_engine_ids if engine}
    selected = []
    for chunk in chunks:
        if preferred and chunk.source_engine_id not in preferred:
            continue
        metadata = chunk.metadata if isinstance(chunk.metadata, dict) else {}
        if metadata.get("schema_version") != CHUNK_METADATA_SCHEMA_VERSION:
            continue
        if not _bool_value(metadata.get("active"), default=True):
            continue
        selected.append(chunk)
    return selected


def _image_embedding_enabled(settings: Settings) -> bool:
    return _bool_value(getattr(settings, "image_embedding_enabled", False), default=False)


def _image_embedding_rrf_weight(settings: Settings) -> float:
    try:
        return max(0.0, float(getattr(settings, "image_embedding_rrf_weight", 0.75) or 0.0))
    except (TypeError, ValueError):
        return 0.75


def _image_embedding_candidates(
    chunks: Sequence[DocumentChunk],
    *,
    output_dir: str | Path,
) -> tuple[list[_ImageEmbeddingCandidate], int, set[str]]:
    """代表画像候補と、asset 未解決の child chunk ID を返します。

    同じ画像（複数 chunk が共有する form page など）は最初の chunk だけに割り当てる。同一 vector を
    chunk ごとに作ると embedding 費用が増え、image channel の上位を同じ画像の重複命中が占めるため。
    """
    root = Path(output_dir)
    candidates: list[_ImageEmbeddingCandidate] = []
    missing_asset_count = 0
    unresolved_chunk_ids: set[str] = set()
    seen_image_ids: set[str] = set()
    for chunk in chunks:
        missing_asset_for_chunk = False
        candidate_found = False
        for image in _chunk_image_evidence(chunk):
            image_id = str(image.get("image_id") or image.get("record_id") or chunk.chunk_id).strip()
            image_key = _evidence_image_key(image_id) if image_id else ""
            if image_key and image_key in seen_image_ids:
                continue
            path = _resolve_embedding_image_path(image, chunk, root)
            if path is None or not path.is_file():
                missing_asset_for_chunk = True
                continue
            candidates.append(
                _ImageEmbeddingCandidate(
                    chunk=chunk,
                    image_path=path,
                    image_id=image_id or chunk.chunk_id,
                    image_hash=_image_embedding_hash(chunk, image, path),
                )
            )
            if image_key:
                seen_image_ids.add(image_key)
            candidate_found = True
            break
        if missing_asset_for_chunk and not candidate_found:
            missing_asset_count += 1
            unresolved_chunk_ids.add(chunk.chunk_id)
    return candidates, missing_asset_count, unresolved_chunk_ids


def _chunk_image_evidence(chunk: DocumentChunk) -> list[dict[str, Any]]:
    metadata = chunk.metadata if isinstance(chunk.metadata, dict) else {}
    images: list[dict[str, Any]] = []
    raw_images = metadata.get("image_evidence")
    if isinstance(raw_images, list):
        images.extend(dict(image) for image in raw_images if isinstance(image, dict))
    table_context = metadata.get("table_context")
    if isinstance(table_context, list):
        for table in table_context:
            if not isinstance(table, dict):
                continue
            raw_visuals = table.get("visual_evidence")
            if isinstance(raw_visuals, list):
                images.extend(dict(image) for image in raw_visuals if isinstance(image, dict))

    filtered: list[dict[str, Any]] = []
    seen: set[str] = set()
    # 図の中の文字（OCR）の entry は持ち主の図にまとめる（同じ図を 2 回数えない。#3455）。
    for image in fold_picture_ocr_entries(images):
        if visual_role_from_ref(image) in {DECORATIVE_VISUAL_ROLE, INLINE_ICON_VISUAL_ROLE}:
            continue
        if _bool_value(image.get("rag_excluded"), default=False):
            continue
        image_id = str(image.get("image_id") or image.get("record_id") or "").strip()
        key = _evidence_image_key(image_id) if image_id else ""
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        filtered.append(image)
    return filtered


def _resolve_embedding_image_path(
    image: dict[str, Any],
    chunk: DocumentChunk,
    output_dir: Path,
) -> Path | None:
    source_run_id = str(image.get("source_run_id") or chunk.source_run_id).strip()
    candidates: list[Path] = []
    for key in ("crop_path", "vision_crop", "context_crop_path", "vision_context_crop"):
        value = str(image.get(key) or "").strip()
        if not value:
            continue
        path = Path(value)
        candidates.append(path if path.is_absolute() else output_dir / source_run_id / path)
    page = _int_value(image.get("page")) or chunk.page_start
    if source_run_id and page > 0:
        candidates.append(output_dir / source_run_id / "pages" / f"page_{page:04d}.png")
        candidates.append(output_dir / source_run_id / "pages_preview" / f"page_{page:04d}.png")
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0] if candidates else None


def _image_embedding_hash(chunk: DocumentChunk, image: dict[str, Any], image_path: Path) -> str:
    hasher = hashlib.sha256()
    hasher.update(b"rag-image-embedding-v2\0")
    hasher.update(_retrieval_text_hash(chunk).encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(str(image.get("image_id") or image.get("record_id") or chunk.chunk_id).encode("utf-8"))
    hasher.update(b"\0")
    # 保存先のパスは含めない。output_dir の移動や別マシンでの実行で全画像が再 embedding されるため。
    hasher.update(image_path.read_bytes())
    return hasher.hexdigest()


def _first_page_contexts(chunks: Iterable[Any]) -> dict[str, Any]:
    """エンジンごとの第1ページ背景。chunk ごとに同じ本文（最大8000文字）を持たせないために集約する。"""
    contexts: dict[str, Any] = {}
    for chunk in chunks:
        document = (chunk.metadata or {}).get("document") if isinstance(chunk.metadata, dict) else None
        if isinstance(document, dict) and isinstance(document.get("first_page_context"), dict):
            contexts.setdefault(str(chunk.source_engine_id or ""), document["first_page_context"])
    return contexts


def _upsert_document(connection: Any, chunk_run: ChunkingResult, document_id: str, *, publish: bool = True) -> None:
    """文書行を upsert します。publish=False では既存文書の latest run と metadata を変更しません。

    既存行は UPDATE、無ければ INSERT の 2 文に分ける。1 文の MERGE で
    `CASE WHEN publish ... ELSE d.metadata_json END` と目標表の CLOB を自己参照すると、
    環境によって ORA-30926 になる (#615)。publish=False の UPDATE は latest と metadata_json に触れない。
    UPDATE が 0 行のあとの INSERT が ORA-00001（並行登録）になった場合は UPDATE をやり直す。
    新規文書の publish=False は latest と metadata_json を NULL で INSERT する。batch ごとの commit で
    未完成の run が公開されないようにするため (#875)。公開は最後の publish=True の UPDATE が行う。
    """
    metadata = {
        "first_page_contexts": _first_page_contexts(chunk_run.chunks),
        "schema_version": 1,
        "source_run_id": chunk_run.source_run_id,
        "chunk_run_id": chunk_run.chunk_run_id,
        # 公開中の run との新旧比較に使う（_newer_published_chunk_run_id）。
        "chunk_run_created_at_utc": chunk_run.created_at_utc,
        "selected_engine_ids": chunk_run.selected_engine_ids,
        "config_hash": chunk_run.config_hash,
        "classification": chunk_run.classification or {},
        "document": chunk_run.document_metadata,
    }
    source_binds = {
        "document_id": document_id,
        "source_file_name": chunk_run.source_file_name,
        "source_file_sha256": chunk_run.source_file_sha256,
        "source_page_count": chunk_run.source_page_count,
    }
    latest_binds = {
        **source_binds,
        "source_run_id": chunk_run.source_run_id,
        "chunk_run_id": chunk_run.chunk_run_id,
        "metadata_json": _json_dumps(metadata),
    }
    unpublished_binds = {**source_binds, "source_run_id": None, "chunk_run_id": None, "metadata_json": None}

    def execute(sql: str, binds: dict[str, Any]) -> int:
        """文ごとに cursor を作る。setinputsizes は cursor に残り、placeholder のない文で DPY-4008 になる。"""
        cursor = connection.cursor()
        if "metadata_json" in binds:
            # 型指定なしの文字列は VARCHAR2 で bind され CLOB 列と型が合わない (#533)。
            # 32767 byte を超える metadata も VARCHAR2 の上限で失敗するため、常に CLOB として渡す。
            _set_input_sizes(cursor, metadata_json="CLOB")
        cursor.execute(sql, binds)
        return int(getattr(cursor, "rowcount", 0) or 0)

    def update_existing() -> int:
        if publish:
            return execute(
                """
                UPDATE rag_documents SET
                  source_file_name = :source_file_name,
                  source_file_sha256 = :source_file_sha256,
                  source_page_count = :source_page_count,
                  latest_source_run_id = :source_run_id,
                  latest_chunk_run_id = :chunk_run_id,
                  metadata_json = :metadata_json,
                  updated_at_utc = SYSTIMESTAMP
                WHERE document_id = :document_id
                """,
                latest_binds,
            )
        return execute(
            """
            UPDATE rag_documents SET
              source_file_name = :source_file_name,
              source_file_sha256 = :source_file_sha256,
              source_page_count = :source_page_count,
              updated_at_utc = SYSTIMESTAMP
            WHERE document_id = :document_id
            """,
            source_binds,
        )

    if update_existing():
        return
    try:
        execute(
            """
            INSERT INTO rag_documents (
              document_id,
              source_file_name,
              source_file_sha256,
              source_page_count,
              latest_source_run_id,
              latest_chunk_run_id,
              metadata_json
            ) VALUES (
              :document_id,
              :source_file_name,
              :source_file_sha256,
              :source_page_count,
              :source_run_id,
              :chunk_run_id,
              :metadata_json
            )
            """,
            latest_binds if publish else unpublished_binds,
        )
    except Exception as exc:
        # 同じ文書を別 session が先に INSERT した。その行に対して UPDATE をやり直す。
        if "ORA-00001" not in str(exc):
            raise
        if not update_existing():
            raise


def _upsert_chunk_run(
    connection: Any,
    chunk_run: ChunkingResult,
    document_id: str,
    children: Sequence[DocumentChunk],
    parents: Sequence[DocumentChunk],
) -> None:
    connection.cursor().execute(
        """
        MERGE INTO rag_chunk_runs r
        USING (
          SELECT
            :chunk_run_id AS chunk_run_id,
            :document_id AS document_id,
            :source_run_id AS source_run_id,
            :source_file_name AS source_file_name,
            :selected_engine_ids_json AS selected_engine_ids_json,
            :config_hash AS config_hash,
            :config_json AS config_json,
            :active AS active,
            :child_count AS child_count,
            :parent_count AS parent_count
          FROM dual
        ) s
        ON (r.chunk_run_id = s.chunk_run_id)
        WHEN MATCHED THEN UPDATE SET
          r.document_id = s.document_id,
          r.source_run_id = s.source_run_id,
          r.source_file_name = s.source_file_name,
          r.selected_engine_ids_json = s.selected_engine_ids_json,
          r.config_hash = s.config_hash,
          r.config_json = s.config_json,
          r.active = s.active,
          r.child_count = s.child_count,
          r.parent_count = s.parent_count,
          r.updated_at_utc = SYSTIMESTAMP
        WHEN NOT MATCHED THEN INSERT (
          chunk_run_id,
          document_id,
          source_run_id,
          source_file_name,
          selected_engine_ids_json,
          config_hash,
          config_json,
          active,
          child_count,
          parent_count
        ) VALUES (
          s.chunk_run_id,
          s.document_id,
          s.source_run_id,
          s.source_file_name,
          s.selected_engine_ids_json,
          s.config_hash,
          s.config_json,
          s.active,
          s.child_count,
          s.parent_count
        )
        """,
        {
            "chunk_run_id": chunk_run.chunk_run_id,
            "document_id": document_id,
            "source_run_id": chunk_run.source_run_id,
            "source_file_name": chunk_run.source_file_name,
            "selected_engine_ids_json": _json_dumps(chunk_run.selected_engine_ids),
            "config_hash": chunk_run.config_hash,
            "config_json": _json_dumps(chunk_run.config.to_dict()),
            "active": _flag(chunk_run.active),
            "child_count": len(children),
            "parent_count": len(parents),
        },
    )


def _upsert_chunks(
    connection: Any,
    chunk_run: ChunkingResult,
    document_id: str,
    chunks: Sequence[DocumentChunk],
) -> None:
    rows = [_chunk_row(chunk_run, document_id, chunk) for chunk in chunks]
    if not rows:
        return
    cursor = connection.cursor()
    _set_input_sizes(
        cursor,
        synthesis_text="CLOB",
        search_text="CLOB",
        child_chunk_ids_json="CLOB",
        source_seq_ranges_json="CLOB",
        source_record_refs_json="CLOB",
        display_regions_json="CLOB",
        metadata_json="CLOB",
    )
    cursor.executemany(
        """
        MERGE INTO rag_chunks c
        USING (
          SELECT
            :chunk_uid AS chunk_uid,
            :document_id AS document_id,
            :chunk_run_id AS chunk_run_id,
            :source_run_id AS source_run_id,
            :chunk_id AS chunk_id,
            :chunk_level AS chunk_level,
            :chunk_seq AS chunk_seq,
            :parent_chunk_uid AS parent_chunk_uid,
            :parent_chunk_id AS parent_chunk_id,
            :child_chunk_ids_json AS child_chunk_ids_json,
            :source_file_name AS source_file_name,
            :source_engine_id AS source_engine_id,
            :source_engine_label AS source_engine_label,
            :page_start AS page_start,
            :page_end AS page_end,
            :source_seq_ranges_json AS source_seq_ranges_json,
            :source_record_refs_json AS source_record_refs_json,
            :display_regions_json AS display_regions_json,
            :synthesis_text AS synthesis_text,
            :search_text AS search_text,
            :content_hash AS content_hash,
            :search_text_hash AS search_text_hash,
            :char_count AS char_count,
            :token_estimate AS token_estimate,
            :contains_picture AS contains_picture,
            :contains_table AS contains_table,
            :atomic AS atomic,
            :active AS active,
            :metadata_json AS metadata_json
          FROM dual
        ) s
        ON (c.chunk_uid = s.chunk_uid)
        WHEN MATCHED THEN UPDATE SET
          c.document_id = s.document_id,
          c.chunk_run_id = s.chunk_run_id,
          c.source_run_id = s.source_run_id,
          c.chunk_id = s.chunk_id,
          c.chunk_level = s.chunk_level,
          c.chunk_seq = s.chunk_seq,
          c.parent_chunk_uid = s.parent_chunk_uid,
          c.parent_chunk_id = s.parent_chunk_id,
          c.child_chunk_ids_json = s.child_chunk_ids_json,
          c.source_file_name = s.source_file_name,
          c.source_engine_id = s.source_engine_id,
          c.source_engine_label = s.source_engine_label,
          c.page_start = s.page_start,
          c.page_end = s.page_end,
          c.source_seq_ranges_json = s.source_seq_ranges_json,
          c.source_record_refs_json = s.source_record_refs_json,
          c.display_regions_json = s.display_regions_json,
          c.synthesis_text = s.synthesis_text,
          c.search_text = s.search_text,
          c.content_hash = s.content_hash,
          c.search_text_hash = s.search_text_hash,
          c.char_count = s.char_count,
          c.token_estimate = s.token_estimate,
          c.contains_picture = s.contains_picture,
          c.contains_table = s.contains_table,
          c.atomic = s.atomic,
          c.active = s.active,
          c.metadata_json = s.metadata_json,
          c.updated_at_utc = SYSTIMESTAMP
        WHEN NOT MATCHED THEN INSERT (
          chunk_uid,
          document_id,
          chunk_run_id,
          source_run_id,
          chunk_id,
          chunk_level,
          chunk_seq,
          parent_chunk_uid,
          parent_chunk_id,
          child_chunk_ids_json,
          source_file_name,
          source_engine_id,
          source_engine_label,
          page_start,
          page_end,
          source_seq_ranges_json,
          source_record_refs_json,
          display_regions_json,
          synthesis_text,
          search_text,
          content_hash,
          search_text_hash,
          char_count,
          token_estimate,
          contains_picture,
          contains_table,
          atomic,
          active,
          metadata_json
        ) VALUES (
          s.chunk_uid,
          s.document_id,
          s.chunk_run_id,
          s.source_run_id,
          s.chunk_id,
          s.chunk_level,
          s.chunk_seq,
          s.parent_chunk_uid,
          s.parent_chunk_id,
          s.child_chunk_ids_json,
          s.source_file_name,
          s.source_engine_id,
          s.source_engine_label,
          s.page_start,
          s.page_end,
          s.source_seq_ranges_json,
          s.source_record_refs_json,
          s.display_regions_json,
          s.synthesis_text,
          s.search_text,
          s.content_hash,
          s.search_text_hash,
          s.char_count,
          s.token_estimate,
          s.contains_picture,
          s.contains_table,
          s.atomic,
          s.active,
          s.metadata_json
        )
        """,
        rows,
    )


def _upsert_embeddings(
    connection: Any,
    chunk_run: ChunkingResult,
    chunks: Sequence[DocumentChunk],
    embeddings: Sequence[Sequence[float]],
    settings: Settings,
) -> None:
    rows = [
        {
            "chunk_uid": _chunk_uid(chunk_run.chunk_run_id, chunk.chunk_id),
            "chunk_run_id": chunk_run.chunk_run_id,
            "embedding_model": settings.embedding_model,
            "embedding_dimensions": _adb_embedding_dimensions(settings),
            "embedding_input_type": EMBEDDING_INPUT_TYPE_SEARCH_DOCUMENT,
            "embedding_text_hash": _retrieval_text_hash(chunk),
            "embedding_vector": array.array("f", [float(value) for value in embedding]),
        }
        for chunk, embedding in zip(chunks, embeddings, strict=False)
    ]
    if not rows:
        return
    cursor = connection.cursor()
    _set_input_sizes(cursor, embedding_vector="VECTOR")
    cursor.executemany(
        """
        MERGE INTO rag_chunk_embeddings e
        USING (
          SELECT
            :chunk_uid AS chunk_uid,
            :chunk_run_id AS chunk_run_id,
            :embedding_model AS embedding_model,
            :embedding_dimensions AS embedding_dimensions,
            :embedding_input_type AS embedding_input_type,
            :embedding_text_hash AS embedding_text_hash,
            :embedding_vector AS embedding_vector
          FROM dual
        ) s
        ON (
          e.chunk_uid = s.chunk_uid
          AND e.embedding_model = s.embedding_model
          AND e.embedding_dimensions = s.embedding_dimensions
          AND e.embedding_text_hash = s.embedding_text_hash
        )
        WHEN MATCHED THEN UPDATE SET
          e.embedding_vector = s.embedding_vector,
          e.embedding_input_type = s.embedding_input_type,
          e.last_embedded_at_utc = SYSTIMESTAMP
        WHEN NOT MATCHED THEN INSERT (
          chunk_uid,
          chunk_run_id,
          embedding_model,
          embedding_dimensions,
          embedding_input_type,
          embedding_text_hash,
          embedding_vector
        ) VALUES (
          s.chunk_uid,
          s.chunk_run_id,
          s.embedding_model,
          s.embedding_dimensions,
          s.embedding_input_type,
          s.embedding_text_hash,
          s.embedding_vector
        )
        """,
        rows,
    )


def _upsert_image_embeddings(
    connection: Any,
    chunk_run: ChunkingResult,
    candidates: Sequence[_ImageEmbeddingCandidate],
    embeddings: Sequence[Sequence[float]],
    settings: Settings,
) -> None:
    rows = [
        {
            "chunk_uid": _chunk_uid(chunk_run.chunk_run_id, candidate.chunk.chunk_id),
            "chunk_run_id": chunk_run.chunk_run_id,
            "embedding_model": settings.embedding_model,
            "embedding_dimensions": _adb_embedding_dimensions(settings),
            "embedding_input_type": EMBEDDING_INPUT_TYPE_SEARCH_DOCUMENT,
            "embedding_modality": EMBEDDING_MODALITY_IMAGE,
            "embedding_text_hash": candidate.image_hash,
            "embedding_vector": array.array("f", [float(value) for value in embedding]),
        }
        for candidate, embedding in zip(candidates, embeddings, strict=False)
    ]
    if not rows:
        return
    cursor = connection.cursor()
    _set_input_sizes(cursor, embedding_vector="VECTOR")
    cursor.executemany(
        """
        MERGE INTO rag_chunk_embeddings e
        USING (
          SELECT
            :chunk_uid AS chunk_uid,
            :chunk_run_id AS chunk_run_id,
            :embedding_model AS embedding_model,
            :embedding_dimensions AS embedding_dimensions,
            :embedding_input_type AS embedding_input_type,
            :embedding_modality AS embedding_modality,
            :embedding_text_hash AS embedding_text_hash,
            :embedding_vector AS embedding_vector
          FROM dual
        ) s
        ON (
          e.chunk_uid = s.chunk_uid
          AND e.embedding_model = s.embedding_model
          AND e.embedding_dimensions = s.embedding_dimensions
          AND e.embedding_input_type = s.embedding_input_type
          AND e.embedding_modality = s.embedding_modality
          AND e.embedding_text_hash = s.embedding_text_hash
        )
        WHEN MATCHED THEN UPDATE SET
          e.embedding_vector = s.embedding_vector,
          e.last_embedded_at_utc = SYSTIMESTAMP
        WHEN NOT MATCHED THEN INSERT (
          chunk_uid,
          chunk_run_id,
          embedding_model,
          embedding_dimensions,
          embedding_input_type,
          embedding_modality,
          embedding_text_hash,
          embedding_vector
        ) VALUES (
          s.chunk_uid,
          s.chunk_run_id,
          s.embedding_model,
          s.embedding_dimensions,
          s.embedding_input_type,
          s.embedding_modality,
          s.embedding_text_hash,
          s.embedding_vector
        )
        """,
        rows,
    )


def _prune_stale_text_embeddings(
    connection: Any,
    chunk_run: ChunkingResult,
    chunks: Sequence[DocumentChunk],
    settings: Settings,
) -> None:
    """現在の検索 text と hash が一致しない text vector を削除します。

    検索は hash 一致の行しか使わないため結果は変わらないが、残すと不要な vector が増え続ける。
    別 model・別次元の行は、設定を戻したときに再利用できるよう残す。
    """
    modality = _text_modality_filter(connection)
    rows = [
        {
            "chunk_uid": _chunk_uid(chunk_run.chunk_run_id, chunk.chunk_id),
            "embedding_model": settings.embedding_model,
            "embedding_dimensions": _adb_embedding_dimensions(settings),
            **({"embedding_modality": modality} if modality else {}),
            "embedding_text_hash": _retrieval_text_hash(chunk),
        }
        for chunk in chunks
    ]
    if not rows:
        return
    connection.cursor().executemany(
        f"""
        DELETE FROM rag_chunk_embeddings
        WHERE chunk_uid = :chunk_uid
          AND embedding_model = :embedding_model
          AND embedding_dimensions = :embedding_dimensions
          {"AND embedding_modality = :embedding_modality" if modality else ""}
          AND embedding_text_hash <> :embedding_text_hash
        """,
        rows,
    )


def _text_modality_filter(connection: Any) -> str | None:
    """text 行に限定する modality 値を返します。列のない旧 schema では None（限定しない）。

    migrate_image_embedding_modality.sql は image embedding を使う場合だけの任意 migration で、
    未適用の schema には image 行が存在しないため、限定しなくても text 行だけが対象になる。
    """
    cursor = connection.cursor()
    cursor.execute("SELECT COUNT(*) FROM user_tab_cols WHERE table_name = 'RAG_CHUNK_EMBEDDINGS' AND column_name = 'EMBEDDING_MODALITY'")
    row = cursor.fetchone()
    return EMBEDDING_MODALITY_TEXT if row and int(row[0] or 0) > 0 else None


def _prune_image_embeddings(
    connection: Any,
    chunk_run: ChunkingResult,
    chunks: Sequence[DocumentChunk],
    candidates: Sequence[_ImageEmbeddingCandidate],
    settings: Settings,
    *,
    preserve_chunk_ids: Iterable[str] = (),
) -> None:
    """現在候補と異なる画像vectorを削除し、asset未解決のchunkは既存vectorを保護します。"""
    preserved = {str(chunk_id) for chunk_id in preserve_chunk_ids if str(chunk_id)}
    current_hashes = {_chunk_uid(chunk_run.chunk_run_id, candidate.chunk.chunk_id): candidate.image_hash for candidate in candidates}
    rows = [
        {
            "chunk_uid": _chunk_uid(chunk_run.chunk_run_id, chunk.chunk_id),
            "embedding_model": settings.embedding_model,
            "embedding_dimensions": _adb_embedding_dimensions(settings),
            "embedding_modality": EMBEDDING_MODALITY_IMAGE,
            "embedding_text_hash": current_hashes.get(_chunk_uid(chunk_run.chunk_run_id, chunk.chunk_id)),
        }
        for chunk in chunks
        if chunk.chunk_id not in preserved
    ]
    if not rows:
        return
    connection.cursor().executemany(
        """
        DELETE FROM rag_chunk_embeddings
        WHERE chunk_uid = :chunk_uid
          AND embedding_model = :embedding_model
          AND embedding_dimensions = :embedding_dimensions
          AND embedding_modality = :embedding_modality
          AND (:embedding_text_hash IS NULL OR embedding_text_hash <> :embedding_text_hash)
        """,
        rows,
    )


def _existing_embedding_hashes(
    connection: Any,
    chunk_uids: Sequence[str],
    embedding_model: str,
    dimensions: int,
    *,
    embedding_modality: str | None = None,
) -> dict[str, set[str]]:
    existing: dict[str, set[str]] = {}
    for batch in _batches(list(chunk_uids), 900):
        if not batch:
            continue
        binds: dict[str, Any] = {"embedding_model": embedding_model, "embedding_dimensions": dimensions}
        modality_sql = ""
        if embedding_modality:
            binds["embedding_modality"] = embedding_modality
            modality_sql = "AND embedding_modality = :embedding_modality"
        placeholders = []
        for index, chunk_uid in enumerate(batch):
            name = f"chunk_uid_{index}"
            placeholders.append(f":{name}")
            binds[name] = chunk_uid
        cursor = connection.cursor()
        cursor.execute(
            f"""
            SELECT chunk_uid, embedding_text_hash
            FROM rag_chunk_embeddings
            WHERE embedding_model = :embedding_model
              AND embedding_dimensions = :embedding_dimensions
              {modality_sql}
              AND chunk_uid IN ({", ".join(placeholders)})
            """,
            binds,
        )
        for chunk_uid, text_hash in cursor:
            existing.setdefault(str(chunk_uid), set()).add(str(text_hash or ""))
    return existing


def _available_embedding_count(
    connection: Any,
    chunk_run_id: str,
    embedding_model: str,
    dimensions: int,
    preferred_engine_ids: Iterable[str],
    *,
    retrieval_scope: str = RETRIEVAL_SCOPE_CURRENT_CHUNK_RUN,
    classification_filter: ClassificationFilter | None = None,
) -> int:
    scope = normalize_retrieval_scope(retrieval_scope)
    engine_sql, engine_binds = _engine_filter_sql("c", preferred_engine_ids)
    scope_join_sql, scope_where_sql, scope_binds = _scope_sql("c", scope, chunk_run_id)
    classification_sql, classification_binds = _classification_filter_sql("c", classification_filter)
    cursor = connection.cursor()
    cursor.execute(
        f"""
        SELECT COUNT(*)
        FROM rag_chunks c
        JOIN rag_chunk_embeddings e
          ON e.chunk_uid = c.chunk_uid
        {scope_join_sql}
        WHERE c.chunk_level = :chunk_level
          AND c.active = 'Y'
          AND JSON_VALUE(c.metadata_json, '$.schema_version' RETURNING NUMBER NULL ON ERROR) = :chunk_metadata_schema_version
          AND e.embedding_model = :embedding_model
          AND e.embedding_dimensions = :embedding_dimensions
          AND e.embedding_text_hash = c.search_text_hash
          {scope_where_sql}
          {engine_sql}
          {classification_sql}
        """,
        {
            "chunk_level": CHILD_CHUNK_LEVEL,
            "chunk_metadata_schema_version": CHUNK_METADATA_SCHEMA_VERSION,
            "embedding_model": embedding_model,
            "embedding_dimensions": dimensions,
            **scope_binds,
            **engine_binds,
            **classification_binds,
        },
    )
    row = cursor.fetchone()
    return int(row[0] or 0) if row else 0


def _embedding_modality_sql(embedding_modality: str) -> tuple[str, dict[str, Any]]:
    if embedding_modality == EMBEDDING_MODALITY_IMAGE:
        return "AND e.embedding_modality = :embedding_modality", {"embedding_modality": EMBEDDING_MODALITY_IMAGE}
    return "AND e.embedding_text_hash = c.search_text_hash", {}


def _vector_search(
    connection: Any,
    chunk_run_id: str,
    query_vector: Sequence[float],
    settings: Settings,
    preferred_engine_ids: Iterable[str],
    limit: int,
    metadata_filter: InquiryMetadataFilter | None = None,
    *,
    retrieval_scope: str = RETRIEVAL_SCOPE_CURRENT_CHUNK_RUN,
    classification_filter: ClassificationFilter | None = None,
    embedding_modality: str = EMBEDDING_MODALITY_TEXT,
) -> list[tuple[str, float | None]]:
    scope = normalize_retrieval_scope(retrieval_scope)
    engine_sql, engine_binds = _engine_filter_sql("c", preferred_engine_ids)
    scope_join_sql, scope_where_sql, scope_binds = _scope_sql("c", scope, chunk_run_id)
    metadata_sql, metadata_binds = _metadata_filter_sql("c", metadata_filter)
    classification_sql, classification_binds = _classification_filter_sql("c", classification_filter)
    embedding_sql, embedding_binds = _embedding_modality_sql(embedding_modality)
    cursor = connection.cursor()
    _set_input_sizes(cursor, query_vector="VECTOR")
    # ORDER BY の副キーで同距離時の順位を決定的にする。副キーがあると optimizer は vector index を
    # 使わず exact search になるため、schema に vector index は作成しない（#467 で実行計画を確認）。
    cursor.execute(
        f"""
        SELECT
          c.chunk_uid,
          VECTOR_DISTANCE(e.embedding_vector, :query_vector, COSINE) AS vector_distance
        FROM rag_chunks c
        JOIN rag_chunk_embeddings e
          ON e.chunk_uid = c.chunk_uid
        {scope_join_sql}
        WHERE c.chunk_level = :chunk_level
          AND c.active = 'Y'
          AND JSON_VALUE(c.metadata_json, '$.schema_version' RETURNING NUMBER NULL ON ERROR) = :chunk_metadata_schema_version
          AND e.embedding_model = :embedding_model
          AND e.embedding_dimensions = :embedding_dimensions
          {embedding_sql}
          {scope_where_sql}
          {engine_sql}
          {metadata_sql}
          {classification_sql}
        ORDER BY VECTOR_DISTANCE(e.embedding_vector, :query_vector, COSINE)
          , c.source_file_name
          , c.page_start
          , c.chunk_seq
        FETCH FIRST {max(1, int(limit))} ROWS ONLY
        """,
        {
            "query_vector": array.array("f", [float(value) for value in query_vector]),
            "chunk_level": CHILD_CHUNK_LEVEL,
            "chunk_metadata_schema_version": CHUNK_METADATA_SCHEMA_VERSION,
            "embedding_model": settings.embedding_model,
            "embedding_dimensions": _adb_embedding_dimensions(settings),
            **embedding_binds,
            **scope_binds,
            **engine_binds,
            **metadata_binds,
            **classification_binds,
        },
    )
    return [(str(row[0]), _optional_float(row[1])) for row in cursor]


def _text_search(
    connection: Any,
    chunk_run_id: str,
    text_query: str,
    settings: Settings,
    preferred_engine_ids: Iterable[str],
    limit: int,
    metadata_filter: InquiryMetadataFilter | None = None,
    *,
    retrieval_scope: str = RETRIEVAL_SCOPE_CURRENT_CHUNK_RUN,
    classification_filter: ClassificationFilter | None = None,
) -> list[tuple[str, float | None]]:
    scope = normalize_retrieval_scope(retrieval_scope)
    engine_sql, engine_binds = _engine_filter_sql("c", preferred_engine_ids)
    scope_join_sql, scope_where_sql, scope_binds = _scope_sql("c", scope, chunk_run_id)
    metadata_sql, metadata_binds = _metadata_filter_sql("c", metadata_filter)
    classification_sql, classification_binds = _classification_filter_sql("c", classification_filter)
    cursor = connection.cursor()
    cursor.execute(
        f"""
        SELECT c.chunk_uid, SCORE(1) AS text_score
        FROM rag_chunks c
        JOIN rag_chunk_embeddings e
          ON e.chunk_uid = c.chunk_uid
        {scope_join_sql}
        WHERE c.chunk_level = :chunk_level
          AND c.active = 'Y'
          AND JSON_VALUE(c.metadata_json, '$.schema_version' RETURNING NUMBER NULL ON ERROR) = :chunk_metadata_schema_version
          AND e.embedding_model = :embedding_model
          AND e.embedding_dimensions = :embedding_dimensions
          AND e.embedding_text_hash = c.search_text_hash
          AND CONTAINS(c.search_text, :text_query, 1) > 0
          {scope_where_sql}
          {engine_sql}
          {metadata_sql}
          {classification_sql}
        ORDER BY SCORE(1) DESC, c.source_file_name, c.page_start, c.chunk_seq
        FETCH FIRST {max(1, int(limit))} ROWS ONLY
        """,
        {
            "chunk_level": CHILD_CHUNK_LEVEL,
            "chunk_metadata_schema_version": CHUNK_METADATA_SCHEMA_VERSION,
            "embedding_model": settings.embedding_model,
            "embedding_dimensions": _adb_embedding_dimensions(settings),
            "text_query": text_query,
            **scope_binds,
            **engine_binds,
            **metadata_binds,
            **classification_binds,
        },
    )
    return [(str(row[0]), _optional_float(row[1])) for row in cursor]


def _load_chunk_run_chunks(
    connection: Any,
    chunk_run_id: str,
    preferred_engine_ids: Iterable[str],
    *,
    retrieval_scope: str = RETRIEVAL_SCOPE_CURRENT_CHUNK_RUN,
    classification_filter: ClassificationFilter | None = None,
) -> list[StoredChunk]:
    scope = normalize_retrieval_scope(retrieval_scope)
    engine_sql, engine_binds = _engine_filter_sql("c", preferred_engine_ids)
    scope_join_sql, scope_where_sql, scope_binds = _scope_sql("c", scope, chunk_run_id)
    classification_sql, classification_binds = _classification_filter_sql("c", classification_filter)
    cursor = connection.cursor()
    cursor.execute(
        f"""
        SELECT
          c.chunk_uid,
          c.chunk_id,
          c.chunk_level,
          c.chunk_seq,
          c.parent_chunk_uid,
          c.parent_chunk_id,
          c.child_chunk_ids_json,
          c.synthesis_text,
          c.search_text,
          c.source_run_id,
          c.source_file_name,
          c.source_engine_id,
          c.source_engine_label,
          c.page_start,
          c.page_end,
          c.source_seq_ranges_json,
          c.source_record_refs_json,
          c.display_regions_json,
          c.metadata_json,
          c.document_id
        FROM rag_chunks c
        {scope_join_sql}
        WHERE c.active = 'Y'
          AND JSON_VALUE(c.metadata_json, '$.schema_version' RETURNING NUMBER NULL ON ERROR) = :chunk_metadata_schema_version
          {scope_where_sql}
          {engine_sql}
          {classification_sql}
        ORDER BY c.source_file_name, c.source_engine_id, CASE c.chunk_level WHEN 'child' THEN 0 ELSE 1 END, c.chunk_seq
        """,
        {
            "chunk_metadata_schema_version": CHUNK_METADATA_SCHEMA_VERSION,
            **scope_binds,
            **engine_binds,
            **classification_binds,
        },
    )
    rows = list(cursor)
    chunks = [_stored_chunk_from_row(row) for row in rows]
    _restore_first_page_contexts(connection, chunks, [str(row[19] or "") if len(row) > 19 else "" for row in rows])
    return chunks


def _combine_hybrid_scores(
    vector_rankings: Sequence[Sequence[tuple[str, float | None]]],
    text_rankings: Sequence[Sequence[tuple[str, float | None]]],
    profile_rankings: Sequence[tuple[str, float, Sequence[tuple[str, float | None]]]] = (),
    *,
    image_vector_rankings: Sequence[Sequence[tuple[str, float | None]]] = (),
    image_vector_weight: float = 1.0,
    query_weights: Sequence[float] | None = None,
    text_query_weights: Sequence[float] | None = None,
) -> list[_HybridScore]:
    """各チャネルの順位を RRF で融合する。

    query_weights は vector / image の ranking と、text_query_weights は全文検索の ranking と
    同じ並び。全文検索は空や重複の query を省くため、重みは位置から推測せず明示で受け取る。
    未指定は従来どおり全て 1.0。
    """
    scores: dict[str, _HybridScore] = {}

    def ensure(chunk_uid: str, query_index: int) -> _HybridScore:
        score = scores.setdefault(chunk_uid, _HybridScore(chunk_uid=chunk_uid))
        score.first_query_index = min(score.first_query_index, query_index)
        return score

    def add_channel(
        score: _HybridScore,
        *,
        channel: str,
        rank: int,
        channel_score: float | None = None,
    ) -> None:
        score.channels.add(channel)
        current_rank = score.channel_ranks.get(channel)
        if current_rank is None or rank < current_rank:
            score.channel_ranks[channel] = rank
        if channel_score is not None:
            current_score = score.channel_scores.get(channel)
            score.channel_scores[channel] = channel_score if current_score is None else max(current_score, channel_score)

    def weight(weights: Sequence[float] | None, index: int) -> float:
        return float(weights[index]) if weights is not None and index < len(weights) else 1.0

    for query_index, ranking in enumerate(vector_rankings):
        channel = f"vector:q{query_index + 1}"
        for rank, (chunk_uid, distance) in enumerate(ranking, start=1):
            score = ensure(chunk_uid, query_index)
            score.rrf_score += weight(query_weights, query_index) / (RRF_K + rank)
            score.vector_rank = rank if score.vector_rank is None else min(score.vector_rank, rank)
            if distance is not None:
                score.vector_distance = distance if score.vector_distance is None else min(score.vector_distance, distance)
            add_channel(
                score,
                channel=channel,
                rank=rank,
                channel_score=(1.0 / (1.0 + distance)) if distance is not None else None,
            )

    image_weight = max(0.0, float(image_vector_weight or 0.0))
    for query_index, ranking in enumerate(image_vector_rankings):
        channel = f"image_vector:q{query_index + 1}"
        for rank, (chunk_uid, distance) in enumerate(ranking, start=1):
            score = ensure(chunk_uid, query_index)
            score.rrf_score += image_weight * weight(query_weights, query_index) / (RRF_K + rank)
            if distance is not None:
                score.vector_distance = distance if score.vector_distance is None else min(score.vector_distance, distance)
            add_channel(
                score,
                channel=channel,
                rank=rank,
                channel_score=(1.0 / (1.0 + distance)) if distance is not None else None,
            )

    for query_index, ranking in enumerate(text_rankings):
        channel = "keyword:oracle_text" if len(text_rankings) == 1 else f"keyword:oracle_text:q{query_index + 1}"
        for rank, (chunk_uid, text_score) in enumerate(ranking, start=1):
            score = ensure(chunk_uid, query_index)
            score.rrf_score += weight(text_query_weights, query_index) / (RRF_K + rank)
            score.text_rank = rank if score.text_rank is None else min(score.text_rank, rank)
            if text_score is not None:
                score.text_score = text_score if score.text_score is None else max(score.text_score, text_score)
            add_channel(score, channel=channel, rank=rank, channel_score=text_score)

    for channel_index, (channel, weight, ranking) in enumerate(profile_rankings):
        channel_name = str(channel or f"profile:{channel_index + 1}")
        channel_weight = max(0.0, float(weight or 0.0))
        for rank, (chunk_uid, profile_score) in enumerate(ranking, start=1):
            score = ensure(chunk_uid, 1_000 + channel_index)
            score.rrf_score += channel_weight / (RRF_K + rank)
            score.profile_rank = rank if score.profile_rank is None else min(score.profile_rank, rank)
            if profile_score is not None:
                score.profile_score = profile_score if score.profile_score is None else max(score.profile_score, profile_score)
            add_channel(score, channel=channel_name, rank=rank, channel_score=profile_score)

    return sorted(
        scores.values(),
        key=lambda score: (
            -score.rrf_score,
            -(score.text_score or 0.0),
            -(score.profile_score or 0.0),
            score.vector_distance if score.vector_distance is not None else 1.0e12,
            score.vector_rank if score.vector_rank is not None else 1_000_000,
            score.text_rank if score.text_rank is not None else 1_000_000,
            score.profile_rank if score.profile_rank is not None else 1_000_000,
            score.first_query_index,
            score.chunk_uid,
        ),
    )


def _hybrid_chunk_sort_key(item: tuple[_HybridScore, StoredChunk]) -> tuple[Any, ...]:
    score, chunk = item
    return (
        -score.rrf_score,
        -(score.text_score or 0.0),
        -(score.profile_score or 0.0),
        score.vector_distance if score.vector_distance is not None else 1.0e12,
        chunk.page_start,
        chunk.chunk_seq,
        score.vector_rank if score.vector_rank is not None else 1_000_000,
        score.text_rank if score.text_rank is not None else 1_000_000,
        score.profile_rank if score.profile_rank is not None else 1_000_000,
        score.first_query_index,
        chunk.chunk_uid,
    )


def _chunk_with_hybrid_metadata(chunk: StoredChunk, score: _HybridScore) -> StoredChunk:
    metadata = dict(chunk.metadata)
    metadata["adb_hybrid"] = {
        "rrf_score": score.rrf_score,
        "vector_rank": score.vector_rank,
        "text_rank": score.text_rank,
        "profile_rank": score.profile_rank,
        "vector_distance": score.vector_distance,
        "text_score": score.text_score,
        "profile_score": score.profile_score,
        "retrieval_channels": sorted(score.channels),
        "channel_ranks": dict(sorted(score.channel_ranks.items())),
        "channel_scores": dict(sorted(score.channel_scores.items())),
        "reserved_by": list(score.reserved_by),
    }
    return replace(chunk, metadata=metadata)


def _stored_chunk_from_row(row: Sequence[Any]) -> StoredChunk:
    child_chunk_ids = _json_list(row[6])
    source_seq_ranges = tuple(_dict_items(_json_list(row[15])))
    source_record_refs = tuple(item for item in _json_list(row[16]) if isinstance(item, dict))
    display_regions = _json_list(row[17])
    metadata = _json_dict(row[18])
    if display_regions:
        layout = metadata.get("layout") if isinstance(metadata.get("layout"), dict) else {}
        metadata["layout"] = {**layout, "display_regions": layout.get("display_regions") or display_regions}
    return StoredChunk(
        chunk_uid=str(row[0] or ""),
        chunk_id=str(row[1] or ""),
        chunk_level=str(row[2] or ""),
        chunk_seq=int(row[3] or 0),
        parent_chunk_uid=str(row[4] or ""),
        parent_chunk_id=str(row[5] or ""),
        child_chunk_ids=tuple(str(item) for item in child_chunk_ids if str(item)),
        text=_lob_to_str(row[7]),
        retrieval_text=_lob_to_str(row[8]),
        source_run_id=str(row[9] or ""),
        source_file_name=str(row[10] or ""),
        source_engine_id=str(row[11] or ""),
        source_engine_label=str(row[12] or ""),
        page_start=int(row[13] or 0),
        page_end=int(row[14] or 0),
        source_seq_ranges=source_seq_ranges,
        source_record_refs=source_record_refs,
        metadata=metadata,
    )


def _chunk_row(chunk_run: ChunkingResult, document_id: str, chunk: DocumentChunk) -> dict[str, Any]:
    metadata = chunk.metadata if isinstance(chunk.metadata, dict) else {}
    source_categories = set(metadata.get("source_categories") or [])
    contains_picture = "Picture" in source_categories or bool(metadata.get("image_evidence"))
    contains_table = "Table" in source_categories or bool(metadata.get("table_context"))
    parent_uid = _chunk_uid(chunk_run.chunk_run_id, chunk.parent_chunk_id) if chunk.parent_chunk_id else ""
    return {
        "chunk_uid": _chunk_uid(chunk_run.chunk_run_id, chunk.chunk_id),
        "document_id": document_id,
        "chunk_run_id": chunk_run.chunk_run_id,
        "source_run_id": chunk.source_run_id,
        "chunk_id": chunk.chunk_id,
        "chunk_level": chunk.chunk_level,
        "chunk_seq": chunk.chunk_seq,
        "parent_chunk_uid": parent_uid,
        "parent_chunk_id": chunk.parent_chunk_id,
        "child_chunk_ids_json": _json_dumps(chunk.child_chunk_ids),
        "source_file_name": chunk.source_file_name,
        "source_engine_id": chunk.source_engine_id,
        "source_engine_label": chunk.source_engine_label,
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
        "source_seq_ranges_json": _json_dumps(chunk.source_seq_ranges),
        "source_record_refs_json": _json_dumps(chunk.source_record_refs),
        "display_regions_json": _json_dumps(_display_regions_from_metadata(metadata)),
        "synthesis_text": chunk.text,
        "search_text": _retrieval_text(chunk),
        # 保存済み診断値ではなく、ADB に書き込む実際の本文を常に hash 化する。
        "content_hash": _sha256_text(chunk.text),
        "search_text_hash": _retrieval_text_hash(chunk),
        "char_count": chunk.char_count,
        "token_estimate": chunk.token_estimate,
        "contains_picture": _flag(contains_picture),
        "contains_table": _flag(contains_table),
        "atomic": _flag(metadata.get("atomic")),
        "active": _flag(_bool_value(metadata.get("active"), default=True)),
        # 表示領域は専用列 display_regions_json に保存し、読込時に metadata へ戻す。
        # 同じ内容を metadata_json にも入れると、検索のたびに読む全 chunk の転送量が約3割増える。
        "metadata_json": _json_dumps(_row_metadata(metadata)),
    }


def _row_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """chunk 行へ保存する metadata。別の場所に1回だけ保存する内容を除く（入力は変更しない）。

    第1ページ背景は rag_documents に文書・エンジンごとに1回保存する。全 chunk に同じ本文を
    持たせると、表の多い1ページ文書では metadata の約6割を占め、検索のたびに全 chunk 分を読む。
    """
    row = dict(metadata)
    if isinstance(row.get("layout"), dict) and "display_regions" in row["layout"]:
        layout = {key: value for key, value in row["layout"].items() if key != "display_regions"}
        if layout:
            row["layout"] = layout
        else:
            del row["layout"]
    if isinstance(row.get("document"), dict) and "first_page_context" in row["document"]:
        row["document"] = {key: value for key, value in row["document"].items() if key != "first_page_context"}
    return row


def _restore_first_page_contexts(connection: Any, chunks: list[StoredChunk], document_ids: Sequence[str]) -> None:
    """読込んだ chunk へ第1ページ背景を戻す。旧形式の行（chunk 自身が持つ）はそのまま使う。

    同じ文書・エンジンの chunk は同じ辞書を共有する。後続の処理はこの値を変更しない。
    """
    missing = sorted(
        {
            doc
            for chunk, doc in zip(chunks, document_ids, strict=False)
            if doc and "first_page_context" not in (chunk.metadata.get("document") or {})
        }
    )
    contexts: dict[str, dict[str, Any]] = {}
    for start in range(0, len(missing), 500):
        batch = missing[start : start + 500]
        binds = {f"d{index}": value for index, value in enumerate(batch)}
        cursor = connection.cursor()
        cursor.execute(
            f"SELECT document_id, metadata_json FROM rag_documents WHERE document_id IN ({', '.join(':' + key for key in binds)})", binds
        )
        for document_id, raw in cursor:
            stored = _json_dict(raw).get("first_page_contexts")
            contexts[str(document_id)] = stored if isinstance(stored, dict) else {}
    for chunk, document_id in zip(chunks, document_ids, strict=False):
        context = contexts.get(document_id, {}).get(chunk.source_engine_id)
        document = chunk.metadata.get("document")
        if isinstance(context, dict) and isinstance(document, dict) and "first_page_context" not in document:
            chunk.metadata["document"] = {**document, "first_page_context": context}


def _engine_filter_sql(alias: str, preferred_engine_ids: Iterable[str]) -> tuple[str, dict[str, str]]:
    engines = []
    seen = set()
    for engine in preferred_engine_ids:
        value = str(engine or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        engines.append(value)
    if not engines:
        return "", {}
    binds: dict[str, str] = {}
    placeholders = []
    for index, engine in enumerate(engines):
        name = f"engine_{index}"
        placeholders.append(f":{name}")
        binds[name] = engine
    return f"AND {alias}.source_engine_id IN ({', '.join(placeholders)})", binds


def _scope_sql(alias: str, retrieval_scope: str, chunk_run_id: str) -> tuple[str, str, dict[str, str]]:
    scope = normalize_retrieval_scope(retrieval_scope)
    if scope == RETRIEVAL_SCOPE_KNOWLEDGE_BASE:
        return (
            f"JOIN rag_documents d ON d.document_id = {alias}.document_id",
            f"AND d.latest_chunk_run_id = {alias}.chunk_run_id",
            {},
        )
    return "", f"AND {alias}.chunk_run_id = :chunk_run_id", {"chunk_run_id": str(chunk_run_id or "")}


def _classification_filter_sql(
    alias: str,
    classification_filter: ClassificationFilter | None,
) -> tuple[str, dict[str, str]]:
    """分類と有効期間の WHERE 句を返す。

    有効期間は as_of で常に絞り、検索と同じ query で pre-filter する。as_of は検索の入口
    （``search_adb_hybrid_chunks`` / ``check_adb_hybrid_search_ready``）が決めて渡す。直接呼ばれて未指定のときは
    既定の時間帯（``DEFAULT_TIMEZONE``）の今日にする（OS の時間帯は使わない。#3249）。
    effective_from / effective_to が無い chunk は無界として除外しない。終了日は排他的。
    ISO 日付は文字列比較で時系列順になるため、JSON_VALUE の文字列をそのまま比較する。
    """
    as_of = with_effective_as_of(classification_filter, business_today(DEFAULT_TIMEZONE)).as_of
    clauses = [
        f"COALESCE(JSON_VALUE({alias}.metadata_json, '$.document.effective_from'), :as_of) <= :as_of",
        f"COALESCE(JSON_VALUE({alias}.metadata_json, '$.document.effective_to'), :as_of_open_end) > :as_of",
    ]
    binds: dict[str, str] = {"as_of": as_of, "as_of_open_end": "9999-12-31"}
    if classification_filter is None or not classification_filter.active:
        return "AND " + " AND ".join(clauses), binds
    for key, value in classification_filter.to_metadata().items():
        bind_name = f"classification_{key}"
        clauses.append(
            f"COALESCE(JSON_VALUE({alias}.metadata_json, '$.document.classification.{key}'), :classification_default_{key}) = :{bind_name}"
        )
        binds[bind_name] = value
        binds[f"classification_default_{key}"] = DEFAULT_CATEGORY_VALUE
    return "AND " + " AND ".join(clauses), binds


# 業務フィルタはこの値だけを参照する。診断情報・キー・原文の偶然の一致を除外する。
_BUSINESS_METADATA_FIELDS = (
    ("retrieval_profile", "business_domains"),
    ("retrieval_profile", "document_kinds"),
    ("document", "classification", "large_category"),
    ("document", "classification", "middle_category"),
    ("document", "classification", "small_category"),
)


def _business_term_variants(term: str) -> tuple[str, ...]:
    """番号付き分類と旧ラベルを同じ業務値として比較する。その他は完全一致する。"""
    label = re.sub(r"^\d+_", "", term)
    return tuple(dict.fromkeys((term, label)))


def _business_metadata_values(metadata: dict[str, Any]) -> set[str]:
    """許可された業務項目の文字列値を取り出す。欠損・null・数値・オブジェクトは無視する。"""
    values: set[str] = set()
    for *namespaces, key in _BUSINESS_METADATA_FIELDS:
        bucket: Any = metadata
        for namespace in namespaces:
            bucket = bucket.get(namespace) if isinstance(bucket, dict) else None
        value = bucket.get(key) if isinstance(bucket, dict) else None
        items = value if isinstance(value, list) else [value]
        values.update(variant for item in items if isinstance(item, str) for variant in _business_term_variants(item))
    return values


def _resolved_metadata_filter(
    connection: Any,
    chunk_run_id: str,
    metadata_filter: InquiryMetadataFilter | None,
    *,
    retrieval_scope: str,
) -> InquiryMetadataFilter | None:
    """検索範囲に一致する文書が無いファイル名条件を落とした filter を返す。

    ファイル名は SQL の WHERE になるため（#601）、質問が挙げた資料が知識ベースに無いと候補が 0 件になり、
    根拠なしとして拒否される。利用者は自分の手元の資料名を書くことがあり、質問文からの抽出が節をまたいで
    崩れることもある。どの文書にも一致しない条件は絞り込みの役に立たないので、無条件検索に戻す。
    ページ番号はファイル名と組でだけ使う条件なので一緒に落とす (#1082)。
    """
    terms = _dedupe_filter_terms(metadata_filter.source_file_terms if metadata_filter else ())[:8]
    if not terms:
        return metadata_filter
    scope = normalize_retrieval_scope(retrieval_scope)
    scope_join_sql, scope_where_sql, binds = _scope_sql("c", scope, chunk_run_id)
    clauses = []
    for index, term in enumerate(terms):
        name = f"resolve_source_file_{index}"
        clauses.append(f"LOWER(c.source_file_name) LIKE :{name} ESCAPE '\\'")
        binds[name] = _like_pattern(term)
    cursor = connection.cursor()
    try:
        cursor.execute(
            f"""
            SELECT 1 FROM rag_chunks c
            {scope_join_sql}
            WHERE c.active = 'Y'
              {scope_where_sql}
              AND ({" OR ".join(clauses)})
              AND ROWNUM = 1
            """,
            binds,
        )
        if cursor.fetchone():
            return metadata_filter
    finally:
        cursor.close()
    return replace(metadata_filter, source_file_terms=(), page_numbers=())


def _metadata_filter_sql(alias: str, metadata_filter: InquiryMetadataFilter | None) -> tuple[str, dict[str, str]]:
    """質問が明示した条件（ファイル名・ページ）だけを SQL の WHERE にし、候補数制限より前に適用する。

    業務語（`metadata_terms`）は WHERE に入れない。照合先の `retrieval_profile.business_domains / document_kinds` は
    chunk 本文からの推定値で、本文に業務名の出ない手順ページが SQL の段階で落ちていた。業務語の一致は
    `_business_match_ranking` の channel として融合後の順位に加点する (#848)。
    """
    if metadata_filter is None or not metadata_filter.active:
        return "", {}

    groups: list[str] = []
    binds: dict[str, str] = {}
    source_file_terms = _dedupe_filter_terms(metadata_filter.source_file_terms)
    page_numbers = _dedupe_page_numbers(metadata_filter.page_numbers)

    if source_file_terms:
        clauses = []
        for index, term in enumerate(source_file_terms[:8]):
            name = f"metadata_source_file_{index}"
            clauses.append(f"LOWER({alias}.source_file_name) LIKE :{name} ESCAPE '\\'")
            binds[name] = _like_pattern(term)
        groups.append("(" + " OR ".join(clauses) + ")")

    if page_numbers:
        # chunk がページをまたぐ場合も含める（page_start <= N <= page_end）。
        clauses = []
        for index, number in enumerate(page_numbers):
            name = f"metadata_page_{index}"
            clauses.append(f"({alias}.page_start <= :{name} AND {alias}.page_end >= :{name})")
            binds[name] = number
        groups.append("(" + " OR ".join(clauses) + ")")

    if not groups:
        return "", {}
    return "AND " + " AND ".join(groups), binds


BUSINESS_MATCH_CHANNEL = "business_match"


def _business_match_ranking(
    matched_chunks: Sequence[StoredChunk],
    metadata_filter: InquiryMetadataFilter | None,
    rankings: Sequence[Sequence[tuple[str, float | None]]],
    text_rankings: Sequence[Sequence[tuple[str, float | None]]],
    *,
    limit: int,
) -> list[tuple[str, float, list[tuple[str, float | None]]]]:
    """質問の業務語に一致する候補を、融合に加える 1 channel の順位として返す (#848)。

    一致する chunk（`_chunks_matching_metadata_filter` の結果）のうち vector / 全文検索の候補に入ったものを、
    各 channel での最良順位の順に並べる。RRF ではこの channel の分だけ加点され、一致しない chunk も候補に残る。
    業務語が無いか一致が無ければ空。
    """
    if metadata_filter is None or not metadata_filter.metadata_terms or not matched_chunks:
        return []
    best_rank: dict[str, int] = {}
    for ranking in (*rankings, *text_rankings):
        for rank, (chunk_uid, _) in enumerate(ranking, start=1):
            best_rank[chunk_uid] = min(best_rank.get(chunk_uid, rank), rank)
    matched = [chunk.chunk_uid for chunk in matched_chunks if chunk.chunk_uid in best_rank]
    if not matched:
        return []
    ordered = sorted(dict.fromkeys(matched), key=lambda uid: (best_rank[uid], uid))[: max(1, int(limit or 1))]
    return [(BUSINESS_MATCH_CHANNEL, 1.0, [(uid, None) for uid in ordered])]


def _chunks_matching_metadata_filter(
    chunks: Sequence[StoredChunk],
    metadata_filter: InquiryMetadataFilter | None,
    classification_filter: ClassificationFilter | None = None,
) -> list[StoredChunk]:
    current_chunks = [
        chunk
        for chunk in chunks
        if isinstance(chunk.metadata, dict) and chunk.metadata.get("schema_version") == CHUNK_METADATA_SCHEMA_VERSION
    ]
    if (metadata_filter is None or not metadata_filter.active) and (classification_filter is None or not classification_filter.active):
        return current_chunks

    source_file_terms = _dedupe_filter_terms(metadata_filter.source_file_terms if metadata_filter else ())[:8]
    metadata_terms = _dedupe_filter_terms(metadata_filter.metadata_terms if metadata_filter else ())[:12]
    page_numbers = _dedupe_page_numbers(metadata_filter.page_numbers if metadata_filter else ())
    selected = []
    for chunk in current_chunks:
        source_key = _filter_key(chunk.source_file_name)
        source_ok = not source_file_terms or any(_filter_key(term) in source_key for term in source_file_terms)
        page_ok = not page_numbers or _chunk_covers_page(chunk, page_numbers)
        business_values = _business_metadata_values(chunk.metadata)
        metadata_ok = not metadata_terms or any(
            variant in business_values for term in metadata_terms for variant in _business_term_variants(term)
        )
        classification_ok = _chunk_matches_classification_filter(chunk, classification_filter)
        if source_ok and page_ok and metadata_ok and classification_ok:
            selected.append(chunk)
    return selected


def _dedupe_page_numbers(values: Iterable[Any]) -> list[int]:
    """SQL と後段フィルタで同じ上限（8 件）と順序を使うため、正の整数だけを重複なく返す。"""
    numbers = [number for number in (_int_value(value) for value in values) if number and number > 0]
    return list(dict.fromkeys(numbers))[:8]


def _chunk_covers_page(chunk: StoredChunk, page_numbers: Sequence[int]) -> bool:
    """SQL の `page_start <= N AND page_end >= N` と同じ判定。page_end 欠損時は page_start だけの chunk とみなす。"""
    start = _int_value(chunk.page_start)
    end = _int_value(chunk.page_end)
    if start is None:
        return False
    end = start if end is None else end
    return any(start <= number <= end for number in page_numbers)


def _chunk_matches_classification_filter(
    chunk: StoredChunk,
    classification_filter: ClassificationFilter | None,
) -> bool:
    if classification_filter is None or not classification_filter.active:
        return True
    metadata = chunk.metadata if isinstance(chunk.metadata, dict) else {}
    document = metadata.get("document") if isinstance(metadata.get("document"), dict) else {}
    classification = document.get("classification") if isinstance(document.get("classification"), dict) else {}
    for key, value in classification_filter.to_metadata().items():
        actual = str(classification.get(key) or DEFAULT_CATEGORY_VALUE).strip()
        if actual != value:
            return False
    return True


def _dedupe_filter_terms(values: Iterable[str]) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = _filter_key(text)
        if not key or key in seen:
            continue
        selected.append(text)
        seen.add(key)
    return selected


def _filter_key(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")).casefold())


def _like_pattern(value: str) -> str:
    escaped = re.sub(r"([%_\\])", r"\\\1", unicodedata.normalize("NFKC", str(value or "")).casefold())
    return f"%{escaped}%"


def _chunk_uid(chunk_run_id: str, chunk_id: str) -> str:
    return f"{chunk_run_id}:{chunk_id}"


def document_id_for_chunk_run(chunk_run: ChunkingResult) -> str:
    """chunk run を保存するときに使う ADB の document_id を返します（削除対象の特定用）。"""
    return _document_id(chunk_run)


def _document_id(chunk_run: ChunkingResult) -> str:
    """PDF hash を優先し、SDK の独立索引では明示文書 ID を安定したキーにする。"""
    if not chunk_run.source_file_sha256 and chunk_run.source_document_id:
        # seed の接頭辞は package の改名(#599)の前の名前のまま。変えると既存の document_id が変わる。
        seed = "docrag:document:" + chunk_run.source_document_id
        return "doc-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    # 識別子がない過去 manifest と PDF のある run は既存 ADB のキーを維持する。
    seed = "|".join(
        [
            chunk_run.source_file_sha256,
            chunk_run.source_file_name,
            str(chunk_run.source_page_count),
        ]
    )
    return "doc-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def _retrieval_text(chunk: DocumentChunk) -> str:
    return str(chunk.retrieval_text or chunk.text or "")


def _retrieval_text_hash(chunk: DocumentChunk) -> str:
    """実際の embedding 入力を hash 化し、古い metadata による誤った再利用を防ぐ。"""
    return _sha256_text(_retrieval_text(chunk))


def _display_regions_from_metadata(metadata: dict[str, Any]) -> list[Any]:
    layout = metadata.get("layout") if isinstance(metadata.get("layout"), dict) else {}
    raw = layout.get("display_regions")
    return raw if isinstance(raw, list) else []


def _sha256_text(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _flag(value: Any) -> str:
    return "Y" if _bool_value(value, default=False) else "N"


def _bool_value(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "active", "on"}:
            return True
        if normalized in {"false", "0", "no", "n", "inactive", "off"}:
            return False
    if value is None:
        return default
    return bool(value)


def _int_value(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _evidence_image_key(image_id: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(image_id or "").lower()).strip("-")


def _set_input_sizes(cursor: Any, **types: str) -> None:
    setter = getattr(cursor, "setinputsizes", None)
    if not callable(setter):
        return
    try:
        import oracledb
    except ImportError:
        return
    type_map = {
        "CLOB": oracledb.DB_TYPE_CLOB,
        "VECTOR": oracledb.DB_TYPE_VECTOR,
    }
    setter(**{name: type_map[type_name] for name, type_name in types.items() if type_name in type_map})


def _query_key(query: str) -> str:
    """検索文の同一性判定キー（NFKC・casefold・空白除去）。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(query or "").strip()).casefold())


def _dedupe_queries(queries: Sequence[str]) -> tuple[str, ...]:
    selected: list[str] = []
    seen: set[str] = set()
    for query in queries:
        text = str(query or "").strip()
        key = _query_key(text)
        if not key or key in seen:
            continue
        selected.append(text)
        seen.add(key)
    return tuple(selected)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _read_json(value: Any, expected: type) -> Any:
    """Oracle native JSON・文字列・LOB を読み、壊れた非空値は握り潰さない。

    SQL NULL と空文字は空コンテナとして扱う。返却値は複製し、後段の
    metadata 補完がドライバの元オブジェクトを変更しないようにする。
    不正な JSON または型不一致は内容を含まない ValueError を送出する。
    """
    reader = getattr(value, "read", None)
    if callable(reader):
        value = reader()
    if value is None or value == "" or value == b"":
        return expected()
    if isinstance(value, (str, bytes, bytearray)):
        try:
            value = json.loads(value)
        except (ValueError, UnicodeError):
            raise ValueError("Oracle JSON フィールドを解析できません") from None
    if not isinstance(value, expected):
        raise ValueError("Oracle JSON フィールドの型が一致しません")
    return deepcopy(value)


def _json_list(value: Any) -> list[Any]:
    """配列型の Oracle JSON を元値から独立した list として返す。"""
    return _read_json(value, list)


def _json_dict(value: Any) -> dict[str, Any]:
    """オブジェクト型の Oracle JSON を元値から独立した dict として返す。"""
    return _read_json(value, dict)


def _dict_items(values: Sequence[Any]) -> list[dict[str, int]]:
    items = []
    for value in values:
        if not isinstance(value, dict):
            continue
        items.append(
            {
                "page": int(value.get("page") or 0),
                "seq_start": int(value.get("seq_start") or 0),
                "seq_end": int(value.get("seq_end") or value.get("seq_start") or 0),
            }
        )
    return items


def _lob_to_str(value: Any) -> str:
    if value is None:
        return ""
    reader = getattr(value, "read", None)
    if callable(reader):
        return str(reader() or "")
    return str(value)


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _batches[T](items: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    step = max(1, int(size or 1))
    for start in range(0, len(items), step):
        yield items[start : start + step]


def _rollback(connection: Any) -> None:
    rollback = getattr(connection, "rollback", None)
    if callable(rollback):
        rollback()


def _short_error(exc: Exception) -> str:
    text = str(exc).strip().splitlines()
    return text[0] if text else exc.__class__.__name__


def _connection_settings(settings: Settings):
    """SDK は明示した接続設定だけを使い、互換直接呼び出しは旧設定読込を維持する。"""
    from rag_engine.resources.runtime import current_runtime

    configured = getattr(settings, "adb_settings", None)
    if configured is not None:
        return configured
    if current_runtime() is not None:
        raise AdbHybridSearchUnavailable("Configure Settings.adb_settings before using Oracle storage")
    return load_adb_settings()
