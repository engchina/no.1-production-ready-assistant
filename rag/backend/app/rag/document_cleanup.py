"""文書の削除の後始末（原本・成果物・成果物の prefix を保存先から消す）を実行する（#2452）。

消す対象は、文書の行の削除と同じ transaction で残した記録（``DocumentCleanupStore``）から読む。
同じ記録で何度実行しても同じ結果になる（冪等）。

- 無いファイル・空の prefix は消せたものとして扱う。
- 参照が不正な URI は何度試しても消せないので、警告にして対象から外す。
- 残っている文書（tenant を問わない）が参照する URI と、同じ ID の文書が残っているときの文書の
  prefix は消さない（別の文書のファイルを消さない）。
- 失敗した対象だけを記録に残し、文書の削除の再試行（同じ文書の DELETE）と取込ワーカーの定期の
  後始末（``retry_due_document_cleanups``）でやり直す。
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from app.clients.document_cleanup_store import (
    DocumentCleanupStore,
    DocumentCleanupTargets,
    DocumentCleanupTask,
)
from app.clients.file_storage import FileStorageClient

logger = logging.getLogger(__name__)

SOURCE_MISSING_MESSAGE = "原本ファイルは既に存在しませんでした。"
SOURCE_INVALID_MESSAGE = "原本ファイルの参照パスが不正なため削除できませんでした。"
SOURCE_FAILED_MESSAGE = (
    "文書は削除しましたが、原本ファイルの削除に失敗しました。保存先を確認してください。"
)
ARTIFACT_FAILED_MESSAGE = "文書は削除しましたが、一部の抽出 artifact cache の削除に失敗しました。"
RETRY_NOTICE_MESSAGE = (
    "削除できなかったファイルは記録し、後で自動で削除をやり直します。"
    "同じ文書の削除を実行しても、すぐにやり直せます。"
)


class DocumentCleanupRecords(Protocol):
    """後始末の記録の読み書き（``DocumentCleanupStore``）のうち、後始末の実行が使うもの。"""

    async def list_due(
        self, *, limit: int, min_age_seconds: float
    ) -> Sequence[DocumentCleanupTask]: ...

    async def save_progress(
        self, cleanup_id: str, remaining: DocumentCleanupTargets, *, error_type: str | None
    ) -> None: ...

    async def referenced_uris(self, uris: Sequence[str]) -> set[str]: ...

    async def document_exists(self, document_id: str) -> bool: ...


@dataclass
class DocumentCleanupOutcome:
    """後始末を 1 回実行した結果。"""

    object_deleted: bool = False
    artifact_deleted_count: int = 0
    artifact_delete_failed_count: int = 0
    warning_messages: list[str] = field(default_factory=list)
    remaining: DocumentCleanupTargets = field(default_factory=DocumentCleanupTargets)


def _ref_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


async def run_document_cleanup(
    store: DocumentCleanupRecords,
    storage: FileStorageClient,
    task: DocumentCleanupTask,
) -> DocumentCleanupOutcome:
    """記録の対象を保存先から消し、残った対象を記録に書き戻す。"""
    targets = task.targets
    outcome = DocumentCleanupOutcome()
    if targets.is_empty:
        return outcome
    log_extra = {"document_id": task.document_id, "cleanup_id": task.cleanup_id}
    try:
        referenced = await store.referenced_uris(
            [uri for uri in (targets.source_uri, *targets.artifact_uris) if uri]
        )
        live_document = bool(targets.prefixes) and await store.document_exists(task.document_id)
    except Exception as exc:  # noqa: BLE001 - 確かめられないうちは消さず、記録のまま次の回に回す
        logger.warning(
            "document_cleanup_reference_check_failed",
            extra={**log_extra, "error_type": type(exc).__name__},
        )
        return _deferred_outcome(targets)
    error_types: list[str] = []

    remaining_source: str | None = None
    if targets.source_uri and targets.source_uri in referenced:
        logger.warning("document_cleanup_skipped_referenced_source", extra=log_extra)
    elif targets.source_uri:
        try:
            outcome.object_deleted = await storage.delete(targets.source_uri)
            if not outcome.object_deleted:
                outcome.warning_messages.append(SOURCE_MISSING_MESSAGE)
        except FileNotFoundError:
            outcome.warning_messages.append(SOURCE_MISSING_MESSAGE)
        except ValueError:
            logger.warning("document_source_delete_invalid_reference", extra=log_extra)
            outcome.warning_messages.append(SOURCE_INVALID_MESSAGE)
        except Exception as exc:  # noqa: BLE001 - 失敗は記録に残してやり直す
            remaining_source = targets.source_uri
            error_types.append(type(exc).__name__)
            logger.warning(
                "document_source_delete_failed",
                extra={**log_extra, "error_type": type(exc).__name__},
            )
            outcome.warning_messages.append(SOURCE_FAILED_MESSAGE)

    remaining_artifacts: list[str] = []
    for artifact_uri in targets.artifact_uris:
        if artifact_uri in referenced:
            logger.warning(
                "document_cleanup_skipped_referenced_artifact",
                extra={**log_extra, "artifact_ref_hash": _ref_hash(artifact_uri)},
            )
            continue
        try:
            if await storage.delete(artifact_uri):
                outcome.artifact_deleted_count += 1
        except FileNotFoundError:
            continue
        except ValueError:
            # 参照が不正な URI は何度試しても消せないので、数えて対象から外す。
            outcome.artifact_delete_failed_count += 1
            logger.warning(
                "document_artifact_delete_invalid_reference",
                extra={**log_extra, "artifact_ref_hash": _ref_hash(artifact_uri)},
            )
        except Exception as exc:  # noqa: BLE001 - 失敗は記録に残してやり直す
            outcome.artifact_delete_failed_count += 1
            remaining_artifacts.append(artifact_uri)
            error_types.append(type(exc).__name__)
            logger.info(
                "document_artifact_delete_failed",
                extra={
                    **log_extra,
                    "artifact_ref_hash": _ref_hash(artifact_uri),
                    "error_type": type(exc).__name__,
                },
            )

    # 参照が残っていない過去の取込の成果物も、文書の prefix ごと消す（#303）。
    remaining_prefixes: list[str] = []
    prefixes = targets.prefixes
    if live_document:
        logger.warning("document_cleanup_skipped_live_document_prefix", extra=log_extra)
        prefixes = ()
    for artifact_prefix in prefixes:
        try:
            prefix_result = await storage.delete_prefix(artifact_prefix)
        except Exception as exc:  # noqa: BLE001 - 失敗は記録に残してやり直す
            outcome.artifact_delete_failed_count += 1
            remaining_prefixes.append(artifact_prefix)
            error_types.append(type(exc).__name__)
            logger.warning(
                "document_artifact_prefix_delete_failed",
                extra={**log_extra, "error_type": type(exc).__name__},
            )
            continue
        outcome.artifact_deleted_count += prefix_result.deleted
        if prefix_result.failed:
            outcome.artifact_delete_failed_count += prefix_result.failed
            remaining_prefixes.append(artifact_prefix)
            logger.warning(
                "document_artifact_prefix_delete_partial",
                extra={**log_extra, "failed_count": prefix_result.failed},
            )
    if outcome.artifact_delete_failed_count:
        outcome.warning_messages.append(ARTIFACT_FAILED_MESSAGE)

    outcome.remaining = DocumentCleanupTargets(
        source_uri=remaining_source,
        artifact_uris=tuple(remaining_artifacts),
        prefixes=tuple(remaining_prefixes),
    )
    try:
        await store.save_progress(
            task.cleanup_id,
            outcome.remaining,
            error_type=error_types[-1] if error_types else None,
        )
    except Exception as exc:  # noqa: BLE001 - 記録は前の対象のまま残り、次の回で同じ対象を試す
        logger.warning(
            "document_cleanup_progress_save_failed",
            extra={**log_extra, "error_type": type(exc).__name__},
        )
    if not outcome.remaining.is_empty:
        outcome.warning_messages.append(RETRY_NOTICE_MESSAGE)
    return outcome


def _deferred_outcome(targets: DocumentCleanupTargets) -> DocumentCleanupOutcome:
    """保存先に触れずに、すべての対象を次の回に回した結果。"""
    outcome = DocumentCleanupOutcome(
        artifact_delete_failed_count=len(targets.artifact_uris) + len(targets.prefixes),
        remaining=targets,
    )
    if targets.source_uri:
        outcome.warning_messages.append(SOURCE_FAILED_MESSAGE)
    if outcome.artifact_delete_failed_count:
        outcome.warning_messages.append(ARTIFACT_FAILED_MESSAGE)
    outcome.warning_messages.append(RETRY_NOTICE_MESSAGE)
    return outcome


async def retry_due_document_cleanups(
    *,
    limit: int,
    min_age_seconds: float,
    store: DocumentCleanupRecords | None = None,
    storage: FileStorageClient | None = None,
) -> int:
    """残っている後始末をやり直し、すべて消せた記録の数を返す（取込ワーカーの定期の処理）。"""
    resolved_store: DocumentCleanupRecords = store or DocumentCleanupStore()
    resolved_storage = storage or FileStorageClient()
    completed = 0
    for task in await resolved_store.list_due(limit=limit, min_age_seconds=min_age_seconds):
        outcome = await run_document_cleanup(resolved_store, resolved_storage, task)
        if outcome.remaining.is_empty:
            completed += 1
    return completed
