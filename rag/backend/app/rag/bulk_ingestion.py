"""文書の一括の投入(ファイル準備から)の受付(#2674)。

画面の「一括投入」は、選んだ文書(最大 ``BULK_INGESTION_MAX_DOCUMENTS`` 件)を 1 回の要求で送り、
サーバーがすべての文書を判定して、受け付けた文書の取込 job(QUEUED)を永続化してから結果を返す。
受け付けた job は画面の接続に依らず、取込 worker が並行数の上限を守って順に処理する。

- 判定は文書ごとに 4 つの結果(受付済み・既に処理中・スキップ・受付不可)と理由を返す。全件を
  受け付けていないのに受付済みとは返さない。
- 文書レシピの単位で job を作る責務は 1 件の投入と同じ(レシピ1 の job)。KB の membership・
  検索・回答プロファイルには触らない。
- 再送で重複して登録しない: 同じレシピに待機中・実行中の job があれば作らない(状態で判定し、
  新しい表・列は持たない)。job を作る transaction の中でレシピの行を lock して確かめ直す。
- 一度も取り込んでいない文書(job の履歴・出力が無い UPLOADED)は、文書の状態の読み込み・
  レシピの用意・job の登録をまとめて行う(1 文書ずつの往復をしない)。それ以外(失敗の後の
  やり直しなど)は、出力の初期化を伴う 1 件の投入と同じ処理を、並行数を絞って行う。
- 流量制限は呼び出し側(API)が要求 1 回として数える。一括の中の各文書は単件の流量制限を通らない。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from fastapi import HTTPException

from app.config import Settings
from app.rag.parser_source_guard import (
    check_parser_source,
    parser_source_block,
    settings_convert_source,
)
from app.rag.source_profile import build_source_profile
from app.schemas.document import (
    BulkIngestionItem,
    BulkIngestionOutcome,
    BulkIngestionReasonCount,
    BulkIngestionResult,
    DocumentProcessingConfig,
    FileStatus,
    IngestionJob,
    IngestionJobStatus,
    SourceProfile,
)

logger = logging.getLogger(__name__)

# 1 回の transaction で job を登録する文書の数(Oracle の IN の上限 1000 より小さく)。
BULK_INGESTION_INSERT_BATCH_SIZE = 500
# 出力の初期化を伴う文書(1 件の投入と同じ処理)を同時に処理する数(DB の接続 pool を占有しない数)。
BULK_INGESTION_SINGLE_CONCURRENCY = 4

# 文書の ID(rag_documents.document_id は VARCHAR2(64))。
_DOCUMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")

_RUNNING_STATUSES = frozenset(
    {
        FileStatus.PREPROCESSING,
        FileStatus.INGESTING,
        FileStatus.CHUNKING,
        FileStatus.INDEXING,
    }
)
# 利用者の確認を待っている状態。ファイル準備からやり直すと確認の途中の結果を捨てるため、
# 一括では投入しない。
_AWAITING_CONFIRMATION_STATUSES = frozenset(
    {FileStatus.PREPROCESSED, FileStatus.REVIEW, FileStatus.CHUNKED}
)

REASON_INVALID_DOCUMENT_ID = "invalid_document_id"
REASON_NOT_FOUND = "not_found"
REASON_PROCESSING = "processing"
REASON_JOB_ACTIVE = "job_active"
REASON_ALREADY_INDEXED = "already_indexed"
REASON_AWAITING_CONFIRMATION = "awaiting_confirmation"
REASON_DUPLICATE_CONTENT = "duplicate_content"
REASON_PARSER_UNSUPPORTED = "parser_unsupported"
REASON_CONFLICT = "conflict"
REASON_TEMPORARY_FAILURE = "temporary_failure"

_REASON_MESSAGES: dict[str, str] = {
    REASON_INVALID_DOCUMENT_ID: "文書の ID の形式が正しくありません。",
    REASON_NOT_FOUND: "文書が見つからないか、操作する権限がありません。",
    REASON_PROCESSING: "文書を処理中です。",
    REASON_JOB_ACTIVE: "取込待ちまたは取込中の job があります。",
    REASON_ALREADY_INDEXED: "索引済みです。",
    REASON_AWAITING_CONFIRMATION: (
        "確認待ちの工程があります。文書の詳細から確認して続けてください。"
    ),
    REASON_DUPLICATE_CONTENT: "同一内容の文書が既に登録されています。",
    REASON_TEMPORARY_FAILURE: (
        "一時的な障害で受け付けられませんでした。もう一度一括投入してください"
        "(受付済みの文書は二重に登録しません)。"
    ),
}
_UNSUPPORTED_SOURCE_MESSAGE = "この形式のファイルは取込に対応していません。"


@dataclass(frozen=True, slots=True)
class BulkIngestionDocumentState:
    """一括の判定に使う文書の状態(1 回の問い合わせで複数の文書を読む)。"""

    document_id: str
    file_name: str
    # 一覧の状態(レシピ1の状態。レシピの行が無ければ文書の状態。#2012)。
    status: FileStatus
    # rag_documents.status(1 件の投入の判定と同じ値)。
    document_status: FileStatus
    has_source: bool
    content_type: str | None = None
    file_size_bytes: int | None = None
    content_sha256: str | None = None
    duplicate_of_document_id: str | None = None
    # レシピ1の処理設定(レシピの行が無ければ文書の処理設定)。
    processing_config: Mapping[str, object] = field(default_factory=dict)
    has_default_recipe: bool = True
    has_job_history: bool = False
    # レシピ1(または recipe_id の無い旧い job)の待機中・実行中の job がある。
    has_active_job: bool = False
    # 文書の行に前回の取込の出力(ファイル準備の成果物・抽出結果)が残っている。
    has_outputs: bool = False
    # レシピ1が無いときに作る値の元(rag_documents の行。
    # ``_default_document_recipe_values`` の入力)。
    recipe_seed: Mapping[str, object] = field(default_factory=dict)

    @property
    def fresh(self) -> bool:
        """一度も取り込んでいない(出力の初期化が要らない)文書か。"""
        return (
            self.status == FileStatus.UPLOADED
            and self.document_status == FileStatus.UPLOADED
            and not self.has_job_history
            and not self.has_outputs
        )


@dataclass(frozen=True, slots=True)
class BulkJobDraft:
    """まとめて登録する取込 job(ファイル準備・レシピ1)の下書き。"""

    document_id: str
    parser_profile: str
    quality_warnings: tuple[str, ...]
    has_default_recipe: bool
    recipe_seed: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class BulkInsertResult:
    """まとめて登録した結果。``created`` は document_id → job_id。"""

    created: Mapping[str, str]
    # 登録の transaction の中で見つけた待機中・実行中の job がある文書(別の要求が先に登録した)。
    active: frozenset[str] = frozenset()
    # 登録の transaction の中で見えなくなった文書(削除・権限の変更)。
    missing: frozenset[str] = frozenset()


class BulkIngestionStore(Protocol):
    """一括の投入の保存先(Oracle の実装は ``app.clients.oracle_bulk_ingestion_store``)。"""

    async def load_states(
        self, document_ids: Sequence[str]
    ) -> Mapping[str, BulkIngestionDocumentState]:
        """利用者から見える文書の状態を返す(見えない・無い文書は含めない)。"""
        ...

    async def insert_queued_jobs(
        self,
        drafts: Sequence[BulkJobDraft],
        *,
        queued_at: datetime,
        max_attempts: int,
    ) -> BulkInsertResult:
        """1 つの transaction でレシピ1を用意し、lock して確かめ直し、QUEUED の job を登録する。"""
        ...


SettingsResolver = Callable[[DocumentProcessingConfig], Settings]
SingleEnqueue = Callable[[str], Awaitable[IngestionJob]]
ParserBlockMessage = Callable[[Settings, SourceProfile], Awaitable[str | None]]


@dataclass(slots=True)
class _Decision:
    outcome: BulkIngestionOutcome
    reason: str | None = None
    message: str | None = None
    job_id: str | None = None


def _decision(
    outcome: BulkIngestionOutcome,
    reason: str,
    *,
    message: str | None = None,
    job_id: str | None = None,
) -> _Decision:
    return _Decision(
        outcome=outcome,
        reason=reason,
        message=message if message is not None else _REASON_MESSAGES.get(reason),
        job_id=job_id,
    )


def normalize_document_ids(raw_ids: Iterable[str]) -> tuple[list[str], list[str]]:
    """要求の ID を (受け付ける ID, 形が不正な ID) に分ける。重複は最初の 1 件だけにする。"""
    valid: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for raw in raw_ids:
        document_id = raw.strip()
        if document_id in seen:
            continue
        seen.add(document_id)
        if _DOCUMENT_ID_PATTERN.fullmatch(document_id):
            valid.append(document_id)
        else:
            invalid.append(document_id)
    return valid, invalid


def _batches[T](items: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class _SettingsCache:
    """同じ処理設定の実効設定を 1 回だけ組み立てる(1 万件で同じ設定が大半のため)。"""

    def __init__(self, resolve: SettingsResolver) -> None:
        self._resolve = resolve
        self._cache: dict[str, Settings] = {}

    def get(self, raw_config: Mapping[str, object]) -> Settings:
        key = json.dumps(raw_config, sort_keys=True, ensure_ascii=False, default=str)
        cached = self._cache.get(key)
        if cached is None:
            try:
                config = DocumentProcessingConfig.model_validate(dict(raw_config))
            except Exception:  # noqa: BLE001 - 壊れた永続値は 1 件の投入と同じく global 継承へ縮退する
                config = DocumentProcessingConfig()
            cached = self._resolve(config)
            self._cache[key] = cached
        return cached


class ParserBlockMessages:
    """選んだ解析エンジンで扱えない形式なら、止める理由の文(1 件の投入の 409 と同じ文)。

    判定(``parser_source_block``)は文書ごとに行い、サービスの状態を問い合わせる文
    (``check_parser_source``)は同じ理由・形式で 1 回だけにする。
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str, str, str | None], str] = {}

    async def __call__(self, settings: Settings, source_profile: SourceProfile) -> str | None:
        block = parser_source_block(settings, source_profile)
        if block is None:
            return None
        key = (block.code, block.backend, block.file_format, block.suggested_backend)
        cached = self._cache.get(key)
        if cached is None:
            checked = await check_parser_source(settings, source_profile)
            cached = (checked or block).message
            self._cache[key] = cached
        return cached


def _source_profile(state: BulkIngestionDocumentState) -> SourceProfile:
    """1 件の投入(``_source_profile_for_detail``)と同じ値で source profile を組み立てる。"""
    return build_source_profile(
        original_file_name=state.file_name,
        sanitized_file_name=state.file_name,
        content_type=state.content_type,
        file_size_bytes=state.file_size_bytes,
        content_sha256=state.content_sha256,
        duplicate_of_document_id=state.duplicate_of_document_id,
        data=None,
    )


async def accept_bulk_ingestion(
    raw_document_ids: Sequence[str],
    *,
    store: BulkIngestionStore,
    resolve_settings: SettingsResolver,
    enqueue_single: SingleEnqueue,
    max_attempts: int,
    parser_message: ParserBlockMessage | None = None,
    insert_batch_size: int = BULK_INGESTION_INSERT_BATCH_SIZE,
    single_concurrency: int = BULK_INGESTION_SINGLE_CONCURRENCY,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> BulkIngestionResult:
    """文書を判定し、受け付けた文書の取込 job を永続化してから結果を返す。"""
    valid_ids, invalid_ids = normalize_document_ids(raw_document_ids)
    decisions: dict[str, _Decision] = {
        document_id: _decision(BulkIngestionOutcome.REJECTED, REASON_INVALID_DOCUMENT_ID)
        for document_id in invalid_ids
    }
    states = await store.load_states(valid_ids) if valid_ids else {}
    settings_cache = _SettingsCache(resolve_settings)
    block_message_of = parser_message or ParserBlockMessages()
    fresh_drafts: list[BulkJobDraft] = []
    single_ids: list[str] = []
    for document_id in valid_ids:
        state = states.get(document_id)
        if state is None or not state.has_source:
            decisions[document_id] = _decision(BulkIngestionOutcome.REJECTED, REASON_NOT_FOUND)
            continue
        if state.status in _RUNNING_STATUSES or state.document_status in _RUNNING_STATUSES:
            decisions[document_id] = _decision(
                BulkIngestionOutcome.ALREADY_ACTIVE, REASON_PROCESSING
            )
            continue
        if state.has_active_job:
            decisions[document_id] = _decision(
                BulkIngestionOutcome.ALREADY_ACTIVE, REASON_JOB_ACTIVE
            )
            continue
        if state.status == FileStatus.INDEXED:
            decisions[document_id] = _decision(BulkIngestionOutcome.SKIPPED, REASON_ALREADY_INDEXED)
            continue
        if state.status in _AWAITING_CONFIRMATION_STATUSES:
            decisions[document_id] = _decision(
                BulkIngestionOutcome.SKIPPED, REASON_AWAITING_CONFIRMATION
            )
            continue
        if state.duplicate_of_document_id is not None:
            decisions[document_id] = _decision(
                BulkIngestionOutcome.SKIPPED, REASON_DUPLICATE_CONTENT
            )
            continue
        source_profile = _source_profile(state)
        settings = settings_cache.get(state.processing_config)
        if source_profile.unsupported_reason and not settings_convert_source(
            settings, source_profile
        ):
            decisions[document_id] = _decision(
                BulkIngestionOutcome.SKIPPED,
                source_profile.unsupported_reason,
                message=_UNSUPPORTED_SOURCE_MESSAGE,
            )
            continue
        block_message = await block_message_of(settings, source_profile)
        if block_message is not None:
            decisions[document_id] = _decision(
                BulkIngestionOutcome.REJECTED,
                REASON_PARSER_UNSUPPORTED,
                message=block_message,
            )
            continue
        if state.fresh:
            fresh_drafts.append(
                BulkJobDraft(
                    document_id=document_id,
                    parser_profile=source_profile.parser_profile,
                    quality_warnings=tuple(source_profile.quality_warnings),
                    has_default_recipe=state.has_default_recipe,
                    recipe_seed=state.recipe_seed,
                )
            )
        else:
            single_ids.append(document_id)

    await _insert_fresh_jobs(
        fresh_drafts,
        decisions,
        store=store,
        batch_size=insert_batch_size,
        max_attempts=max_attempts,
        queued_at=clock(),
    )
    await _enqueue_single_jobs(
        single_ids, decisions, enqueue_single=enqueue_single, concurrency=single_concurrency
    )
    ordered = [document_id for document_id in _request_order(raw_document_ids) if document_id]
    return _result(raw_count=len(raw_document_ids), ordered_ids=ordered, decisions=decisions)


def _request_order(raw_document_ids: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in raw_document_ids:
        document_id = raw.strip()
        if document_id in seen:
            continue
        seen.add(document_id)
        ordered.append(document_id)
    return ordered


async def _insert_fresh_jobs(
    drafts: Sequence[BulkJobDraft],
    decisions: dict[str, _Decision],
    *,
    store: BulkIngestionStore,
    batch_size: int,
    max_attempts: int,
    queued_at: datetime,
) -> None:
    """一度も取り込んでいない文書の job を、batch ごとの transaction でまとめて登録する。

    batch の transaction が失敗したら、その batch の文書だけを「一時的な障害」で受付不可にし、
    ほかの batch は続ける(登録できた batch は応答の前に commit 済み)。
    """
    for index, batch in enumerate(_batches(drafts, batch_size)):
        # 同じ要求の job は要求の順に処理されるよう、登録の時刻を batch ごとにずらす。
        batch_queued_at = queued_at + timedelta(microseconds=index)
        try:
            inserted = await store.insert_queued_jobs(
                batch, queued_at=batch_queued_at, max_attempts=max_attempts
            )
        except Exception:
            logger.exception(
                "bulk_ingestion_batch_failed",
                extra={"batch_index": index, "batch_size": len(batch)},
            )
            for draft in batch:
                decisions[draft.document_id] = _decision(
                    BulkIngestionOutcome.REJECTED, REASON_TEMPORARY_FAILURE
                )
            continue
        for draft in batch:
            document_id = draft.document_id
            job_id = inserted.created.get(document_id)
            if job_id is not None:
                decisions[document_id] = _Decision(
                    outcome=BulkIngestionOutcome.ACCEPTED, job_id=job_id
                )
            elif document_id in inserted.active:
                decisions[document_id] = _decision(
                    BulkIngestionOutcome.ALREADY_ACTIVE, REASON_JOB_ACTIVE
                )
            else:
                decisions[document_id] = _decision(BulkIngestionOutcome.REJECTED, REASON_NOT_FOUND)


async def _enqueue_single_jobs(
    document_ids: Sequence[str],
    decisions: dict[str, _Decision],
    *,
    enqueue_single: SingleEnqueue,
    concurrency: int,
) -> None:
    """前回の出力の初期化が要る文書は、1 件の投入と同じ処理を並行数を絞って行う。"""
    if not document_ids:
        return
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def run(document_id: str) -> None:
        async with semaphore:
            decisions[document_id] = await _enqueue_single(document_id, enqueue_single)

    await asyncio.gather(*(run(document_id) for document_id in document_ids))


async def _enqueue_single(document_id: str, enqueue_single: SingleEnqueue) -> _Decision:
    try:
        job = await enqueue_single(document_id)
    except HTTPException as exc:
        if exc.status_code == 404:
            return _decision(BulkIngestionOutcome.REJECTED, REASON_NOT_FOUND)
        return _decision(BulkIngestionOutcome.REJECTED, REASON_CONFLICT, message=str(exc.detail))
    except Exception:
        logger.exception("bulk_ingestion_single_failed", extra={"document_id": document_id})
        return _decision(BulkIngestionOutcome.REJECTED, REASON_TEMPORARY_FAILURE)
    if job.status == IngestionJobStatus.SKIPPED:
        reason = job.skip_reason or REASON_ALREADY_INDEXED
        return _decision(
            BulkIngestionOutcome.SKIPPED,
            reason,
            message=_REASON_MESSAGES.get(reason, _UNSUPPORTED_SOURCE_MESSAGE),
            job_id=job.id,
        )
    return _Decision(outcome=BulkIngestionOutcome.ACCEPTED, job_id=job.id)


def _result(
    *,
    raw_count: int,
    ordered_ids: Sequence[str],
    decisions: Mapping[str, _Decision],
) -> BulkIngestionResult:
    items = [
        BulkIngestionItem(
            document_id=document_id,
            outcome=decisions[document_id].outcome,
            reason=decisions[document_id].reason,
            message=decisions[document_id].message,
            job_id=decisions[document_id].job_id,
        )
        for document_id in ordered_ids
    ]
    counts: dict[BulkIngestionOutcome, int] = dict.fromkeys(BulkIngestionOutcome, 0)
    reasons: dict[tuple[BulkIngestionOutcome, str, str | None], BulkIngestionReasonCount] = {}
    for item in items:
        counts[item.outcome] += 1
        if item.reason is None:
            continue
        key = (item.outcome, item.reason, item.message)
        existing = reasons.get(key)
        if existing is None:
            reasons[key] = BulkIngestionReasonCount(
                outcome=item.outcome, reason=item.reason, count=1, message=item.message
            )
        else:
            existing.count += 1
    return BulkIngestionResult(
        requested_count=raw_count,
        unique_count=len(items),
        accepted_count=counts[BulkIngestionOutcome.ACCEPTED],
        already_active_count=counts[BulkIngestionOutcome.ALREADY_ACTIVE],
        skipped_count=counts[BulkIngestionOutcome.SKIPPED],
        rejected_count=counts[BulkIngestionOutcome.REJECTED],
        reasons=sorted(reasons.values(), key=lambda entry: (-entry.count, entry.reason)),
        items=items,
    )
