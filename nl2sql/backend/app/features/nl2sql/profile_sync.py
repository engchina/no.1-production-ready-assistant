"""業務 Profile から Oracle DBMS_CLOUD_AI asset へ反映する永続 job。"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from app.settings import get_settings

from .models import (
    ProfileSelectAiProfileRequest,
    ProfileSyncJobData,
    ProfileSyncJobPhase,
    ProfileSyncJobRequest,
    ProfileSyncJobStatus,
    SelectAiCredentialReadinessData,
)
from .ontology_observability import record_job
from .ontology_store import OntologyStore, OntologyVersionConflict, canonical_json
from .oracle_adapter import OracleAdapterError, SelectAiCredentialMissingError
from .select_ai_readiness import (
    assert_select_ai_credential_ready,
    is_credential_missing_oracle_error,
    select_ai_credential_missing_message,
    select_ai_credential_readiness,
)
from .service import Nl2SqlService, nl2sql_service

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {
    ProfileSyncJobStatus.SUCCEEDED,
    ProfileSyncJobStatus.FAILED,
    ProfileSyncJobStatus.CANCELLED,
}
_ORACLE_ERROR_CODE_RE = re.compile(r"\bORA-\d{4,5}\b", re.IGNORECASE)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _profile_sync_public_error(
    exc: Exception,
    phase: ProfileSyncJobPhase = ProfileSyncJobPhase.SYNCING_ORACLE_PROFILE,
) -> tuple[str, str]:
    """既知 Oracle 例外を復旧可能な日本語へ変換し、PL/SQL stack を公開しない。

    Agent アセットの再構築で失敗したときは、DBMS_CLOUD_AI Profile の反映は済んでいるため
    「Oracle Profile の反映に失敗」とは書かず、失敗した工程を示す。
    """
    if isinstance(exc, SelectAiCredentialMissingError) or is_credential_missing_oracle_error(exc):
        return SelectAiCredentialMissingError.code, select_ai_credential_missing_message(exc)
    if phase == ProfileSyncJobPhase.REBUILDING_AGENT_ASSETS:
        message = "Select AI Agent アセットを再構築できませんでした。"
        if isinstance(exc, OracleAdapterError) or _ORACLE_ERROR_CODE_RE.search(str(exc)):
            return (
                "AGENT_ASSETS_REBUILD_FAILED",
                f"{message}データベース設定と Select AI Agent の構成を確認してから"
                "再試行してください。",
            )
        detail = str(exc).strip()
        if detail:
            message = f"{message} {detail[:500]}"
        return "AGENT_ASSETS_REBUILD_FAILED", f"{message} 再試行してください。"
    if isinstance(exc, OracleAdapterError) or _ORACLE_ERROR_CODE_RE.search(str(exc)):
        return (
            "PROFILE_SYNC_FAILED",
            (
                "Oracle Profile の反映に失敗しました。データベース設定と Select AI の"
                "構成を確認してから再試行してください。"
            ),
        )
    detail = str(exc).strip()
    message = "Oracle Profile の反映に失敗しました。"
    if detail:
        message = f"{message} {detail[:500]}"
    return "PROFILE_SYNC_FAILED", f"{message} 再試行してください。"


class ProfileSyncExecutionLost(RuntimeError):
    """中断・別 worker の更新で、この実行の保存権限が失われた。"""


class ProfileSyncService:
    """Oracle Profile 同期を API process / external worker で共有する。"""

    def __init__(
        self,
        *,
        service: Nl2SqlService = nl2sql_service,
        store_provider: Callable[[], OntologyStore] | None = None,
    ) -> None:
        self._service = service
        self._store_provider = store_provider or self._default_store
        self._jobs: dict[str, ProfileSyncJobData] = {}
        self._lock = threading.RLock()
        self._inprocess_jobs: set[str] = set()

    @staticmethod
    def _default_store() -> OntologyStore:
        # Router import cycle を避け、初回 API 呼び出し時に共有 store を解決する。
        from .ontology_router import ontology_runtime

        return ontology_runtime.store

    @property
    def store(self) -> OntologyStore:
        return self._store_provider()

    def start(
        self,
        profile_id: str,
        request: ProfileSyncJobRequest,
        *,
        idempotency_key: str,
    ) -> ProfileSyncJobData:
        if request.confirmation.strip() != "ADMIN_EXECUTE":
            raise ValueError("実行には confirmation=ADMIN_EXECUTE が必要です。")
        profile = self._service.get_profile(profile_id)
        original_name = self._profile_original_name(profile)
        # 要求が省略したら、業務プロファイルの設定（保存のたびに再構築するか）に従う（#1690）。
        rebuild_agent_assets = (
            request.rebuild_agent_assets
            if request.rebuild_agent_assets is not None
            else bool(getattr(profile, "rebuild_agent_assets_on_save", False))
        )
        request_hash = hashlib.sha256(
            canonical_json(
                {
                    "profile_id": profile.id,
                    "profile_etag": profile.etag,
                    "original_name": original_name,
                    "rebuild_agent_assets": rebuild_agent_assets,
                }
            ).encode("utf-8")
        ).hexdigest()
        with self._lock:
            existing = self._idempotent_job(idempotency_key, request_hash)
        if existing is not None:
            return existing
        # 同じ要求の再送（Idempotency-Key）は受付済みの job を返し、新しく受け付けるときだけ
        # Oracle に触る前の事前条件を確かめる（#1683）。
        self.assert_ready()
        with self._lock:
            existing = self._idempotent_job(idempotency_key, request_hash)
            if existing is not None:
                return existing
            job = ProfileSyncJobData(
                job_id=f"profile_sync_{uuid4().hex}",
                profile_id=profile.id,
                profile_etag=profile.etag,
                original_name=original_name,
                rebuild_agent_assets=rebuild_agent_assets,
                created_at=_now(),
                deadline_at=self._new_deadline(),
            )
            reservation = {
                "operation": "profile_sync",
                "idempotency_key": idempotency_key,
                "request_hash": request_hash,
                "resource_id": job.job_id,
                "status": "accepted",
                "created_at": job.created_at,
            }
            try:
                self.store.save_idempotency(reservation)
            except OntologyVersionConflict:
                # 別 worker が同じ unique key を先に確保した。勝者の job を返す。
                for _attempt in range(20):
                    existing = self._idempotent_job(idempotency_key, request_hash)
                    if existing is not None:
                        return existing
                    time.sleep(0.01)
                raise RuntimeError(
                    "同じ Oracle Profile 同期を別 worker が受付中です。再試行してください。"
                ) from None
            try:
                self._save(job)
            except Exception:
                try:
                    self.store.delete_documents(
                        "idempotency",
                        {
                            "operation": "profile_sync",
                            "idempotency_key": idempotency_key,
                        },
                    )
                except Exception:
                    logger.warning(
                        "profile_sync_idempotency_cleanup_failed",
                        exc_info=True,
                        extra={"job_id": job.job_id},
                    )
                raise
        self._dispatch(job.job_id)
        return job.model_copy(deep=True)

    def readiness(self) -> SelectAiCredentialReadinessData:
        """Oracle 反映の事前条件（Select AI の Credential が作成済みか）を副作用なしで返す。"""
        return select_ai_credential_readiness(self._oracle_adapter(), get_settings())

    def assert_ready(self) -> None:
        """Credential が無いと分かったら、job を作らずに止める（#1683）。"""
        assert_select_ai_credential_ready(self._oracle_adapter(), get_settings())

    def _oracle_adapter(self) -> object:
        return getattr(self._service, "_oracle_adapter", None)

    def _idempotent_job(
        self,
        idempotency_key: str,
        request_hash: str,
    ) -> ProfileSyncJobData | None:
        existing = self.store.get_idempotency("profile_sync", idempotency_key)
        if existing is None:
            return None
        if existing.get("request_hash") != request_hash:
            raise ValueError("同じ Idempotency-Key が別の Oracle Profile 同期に使用されています。")
        return self.get(str(existing.get("resource_id") or ""))

    @staticmethod
    def _new_deadline() -> str:
        return (
            datetime.now(UTC)
            + timedelta(seconds=max(1.0, get_settings().nl2sql_profile_sync_job_timeout_seconds))
        ).isoformat()

    @staticmethod
    def _expired(job: ProfileSyncJobData) -> bool:
        timestamp = job.deadline_at or job.created_at or job.started_at
        if not timestamp:
            return True
        try:
            deadline = datetime.fromisoformat(timestamp)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)
            if not job.deadline_at:
                deadline += timedelta(
                    seconds=max(1.0, get_settings().nl2sql_profile_sync_job_timeout_seconds)
                )
            return deadline <= datetime.now(UTC)
        except ValueError:
            return True

    def peek(self, job_id: str) -> ProfileSyncJobData | None:
        """認可前の存在・scope 確認。状態の変更や再 dispatch はしない。"""
        document = self.store.get_job(job_id)
        if document is None or document.get("job_type") != "profile_sync":
            return None
        return ProfileSyncJobData.model_validate(document["payload"])

    def get(self, job_id: str) -> ProfileSyncJobData | None:
        job = self.peek(job_id)
        if job and job.status not in _TERMINAL_STATUSES and self._expired(job):
            self._interrupt(
                job_id,
                "PROFILE_SYNC_TIMEOUT",
                "Oracle 反映の実行期限を超えました。反映済みの可能性があるため、"
                "Oracle Profile と Agent の状態を確認してから再試行してください。",
            )
            return self.peek(job_id)
        return job

    def _dispatch(self, job_id: str) -> None:
        if get_settings().nl2sql_ontology_worker_mode != "inprocess":
            return
        with self._lock:
            if job_id in self._inprocess_jobs:
                return
            self._inprocess_jobs.add(job_id)

        def run() -> None:
            try:
                self._run_safely(job_id)
            finally:
                with self._lock:
                    self._inprocess_jobs.discard(job_id)

        try:
            threading.Thread(target=run, daemon=True, name=f"{job_id}-worker").start()
        except Exception:
            with self._lock:
                self._inprocess_jobs.discard(job_id)
            self._interrupt(
                job_id,
                "PROFILE_SYNC_DISPATCH_FAILED",
                "Oracle 反映を開始できませんでした。再試行してください。",
            )
            raise

    def shutdown(self) -> None:
        with self._lock:
            owned = tuple(self._inprocess_jobs)
        for job_id in owned:
            try:
                self._interrupt(
                    job_id,
                    "PROFILE_SYNC_INTERRUPTED",
                    "サーバーの停止により Oracle 反映の結果を確認できません。"
                    "Oracle Profile と Agent の状態を確認してから再試行してください。",
                )
            except Exception as exc:
                logger.error(
                    "Oracle 反映の中断状態を保存できません。次回取得時に期限を確認します。",
                    extra={"job_id": job_id, "error_type": type(exc).__name__},
                )

    def _interrupt(self, job_id: str, code: str, message: str) -> None:
        for _ in range(3):
            document = self.store.get_job(job_id)
            if not document or document.get("job_type") != "profile_sync":
                return
            job = ProfileSyncJobData.model_validate(document["payload"])
            if job.status in _TERMINAL_STATUSES:
                return
            if code == "PROFILE_SYNC_TIMEOUT" and not self._expired(job):
                return
            failed = job.model_copy(
                update={
                    "status": ProfileSyncJobStatus.FAILED,
                    "phase": ProfileSyncJobPhase.FAILED,
                    "failed_phase": job.phase,
                    "error_code": code,
                    "error_message_ja": message,
                    "finished_at": _now(),
                }
            )
            try:
                self._save(failed, previous=document)
            except OntologyVersionConflict:
                continue
            logger.warning(
                message,
                extra={
                    "job_id": job_id,
                    "profile_id": job.profile_id,
                    "error_code": code,
                    "phase": job.phase.value,
                },
            )
            return
        raise OntologyVersionConflict("profile_sync_interruption_conflict")

    def retry(self, job_id: str) -> ProfileSyncJobData:
        previous = self.get(job_id)
        if previous is None:
            raise KeyError(job_id)
        if previous.status != ProfileSyncJobStatus.FAILED:
            raise ValueError("失敗した Oracle Profile 同期 job だけを再試行できます。")
        with self._lock:
            if job_id in self._inprocess_jobs:
                raise ValueError(
                    "前回の Oracle 呼び出しが終了するまで再試行できません。"
                    "状態を再確認してください。"
                )
        profile = self._service.get_profile(previous.profile_id)
        self.assert_ready()
        job = ProfileSyncJobData(
            job_id=f"profile_sync_{uuid4().hex}",
            profile_id=profile.id,
            profile_etag=profile.etag,
            original_name=previous.original_name.strip() or self._profile_original_name(profile),
            rebuild_agent_assets=previous.rebuild_agent_assets,
            retry_of_job_id=previous.job_id,
            created_at=_now(),
            deadline_at=self._new_deadline(),
        )
        self._save(job)
        self._dispatch(job.job_id)
        return job.model_copy(deep=True)

    def cancel_for_profile(self, profile_id: str) -> int:
        cancelled = 0
        for document in self.store.list_documents("jobs", {"profile_id": profile_id}):
            if document.get("job_type") != "profile_sync":
                continue
            for attempt in range(3):
                job = ProfileSyncJobData.model_validate(document["payload"])
                if job.status in _TERMINAL_STATUSES:
                    break
                cancelled_job = job.model_copy(
                    update={
                        "status": ProfileSyncJobStatus.CANCELLED,
                        "phase": ProfileSyncJobPhase.CANCELLED,
                        "error_code": "PROFILE_DELETED",
                        "error_message_ja": "業務プロファイルが削除されたため同期を中止しました。",
                        "finished_at": _now(),
                    }
                )
                try:
                    self._save(cancelled_job, previous=document)
                except OntologyVersionConflict:
                    if attempt == 2:
                        raise
                    current = self.store.get_job(job.job_id)
                    if current is None:
                        break
                    document = current
                    continue
                cancelled += 1
                break
        return cancelled

    def run_persisted(self, job_id: str) -> ProfileSyncJobData:
        """永続化済み job を安全に実行し、失敗も terminal status へ確定する。"""

        self._run_safely(job_id)
        result = self.get(job_id)
        if result is None:
            raise RuntimeError("Oracle Profile 同期 job の実行結果を取得できません。")
        return result

    def _execute(self, job_id: str) -> ProfileSyncJobData:
        job = self.get(job_id)
        if job is None:
            raise RuntimeError("Oracle Profile 同期 job が見つかりません。")
        # 実行中の再配送は再送しない。終了済み job も再実行しない。
        if job.status != ProfileSyncJobStatus.QUEUED:
            return job
        document = self.store.get_job(job_id)
        if document is None or document["payload"]["status"] != "queued":
            return self.get(job_id) or job
        running = job.model_copy(
            update={
                "status": ProfileSyncJobStatus.RUNNING,
                "phase": ProfileSyncJobPhase.SYNCING_ORACLE_PROFILE,
                "started_at": _now(),
                "error_code": "",
                "error_message_ja": "",
            }
        )
        try:
            document = self._save(running, previous=document, claim=True)
        except OntologyVersionConflict:
            return self.get(job_id) or job
        logger.info(
            "Oracle Profile の反映を開始しました。",
            extra={"job_id": job_id, "profile_id": job.profile_id, "deadline_at": job.deadline_at},
        )
        try:
            self._assert_current_profile(running)
            self._assert_execution(document)
            oracle_result = self._service.upsert_profile_select_ai_profile(
                running.profile_id,
                ProfileSelectAiProfileRequest(
                    confirmation="ADMIN_EXECUTE",
                    reason="profile-sync-job",
                    original_name=running.original_name,
                ),
            )
            if not oracle_result.executed or oracle_result.status == "error":
                # 反映の失敗（Profile の範囲の不一致など）でも、DB Profile 一覧の
                # 同期の要否（profile_list_refresh_*）を画面が読めるように、
                # 結果を job に残す（#2034）。
                running = running.model_copy(update={"oracle_result": oracle_result})
                raise RuntimeError(
                    " ".join(oracle_result.warnings).strip()
                    or "Oracle Profile の反映に失敗しました。"
                )
            running = running.model_copy(update={"oracle_result": oracle_result})
            document = self._save_owned(running, document)
            if running.original_name.strip():
                self._assert_execution(document)
                profile = self._service.clear_profile_select_ai_previous_name(
                    running.profile_id, expected_etag=running.profile_etag
                )
                running = running.model_copy(update={"profile_etag": profile.etag})
                document = self._save_owned(running, document)
            if running.rebuild_agent_assets:
                running = running.model_copy(
                    update={"phase": ProfileSyncJobPhase.REBUILDING_AGENT_ASSETS}
                )
                document = self._save_owned(running, document)
                self._assert_execution(document)
                agent_result = self._service.refresh_select_ai_agent_assets(
                    running.profile_id, profile_already_synced=True
                )
                if not agent_result.refreshed:
                    raise RuntimeError(
                        agent_result.warning or "Select AI Agent asset の再構築に失敗しました。"
                    )
                running = running.model_copy(update={"agent_result": agent_result})
            running = running.model_copy(update={"phase": ProfileSyncJobPhase.VERIFYING})
            document = self._save_owned(running, document)
            self._assert_current_profile(running)
            running = running.model_copy(
                update={
                    "status": ProfileSyncJobStatus.SUCCEEDED,
                    "phase": ProfileSyncJobPhase.SUCCEEDED,
                    "finished_at": _now(),
                }
            )
            self._save_owned(running, document)
            record_job(job_type="profile_sync", status="succeeded")
            logger.info(
                "Oracle Profile の反映が完了しました。",
                extra={"job_id": job_id, "profile_id": job.profile_id},
            )
            return running
        except (ProfileSyncExecutionLost, OntologyVersionConflict):
            logger.info(
                "中断または競合後の Oracle 反映結果を破棄しました。", extra={"job_id": job_id}
            )
        except Exception as exc:
            code, message = _profile_sync_public_error(exc, running.phase)
            oracle_code_match = _ORACLE_ERROR_CODE_RE.search(str(exc))
            logger.warning(
                "Oracle Profile の反映に失敗しました。",
                extra={
                    "job_id": job_id,
                    "profile_id": job.profile_id,
                    "phase": running.phase.value,
                    "error_code": code,
                    "error_type": type(exc).__name__,
                    "oracle_error_code": (
                        oracle_code_match.group(0).upper() if oracle_code_match else ""
                    ),
                },
            )
            failed = running.model_copy(
                update={
                    "status": ProfileSyncJobStatus.FAILED,
                    "phase": ProfileSyncJobPhase.FAILED,
                    "failed_phase": running.phase,
                    "error_code": code,
                    "error_message_ja": message,
                    "finished_at": _now(),
                }
            )
            try:
                self._save_owned(failed, document)
                record_job(job_type="profile_sync", status="failed", error_code=code)
            except (ProfileSyncExecutionLost, OntologyVersionConflict):
                pass
        return self.get(job_id) or job

    def _assert_execution(self, document: dict[str, Any]) -> None:
        current = self.get(str(document["job_id"]))
        latest = self.store.get_job(str(document["job_id"]))
        if (
            current is None
            or current.status != ProfileSyncJobStatus.RUNNING
            or latest is None
            or latest["etag"] != document["etag"]
        ):
            raise ProfileSyncExecutionLost()

    def _save_owned(self, job: ProfileSyncJobData, document: dict[str, Any]) -> dict[str, Any]:
        self._assert_execution(document)
        return self._save(job, previous=document)

    def _run_safely(self, job_id: str) -> None:
        try:
            self._execute(job_id)
        except Exception as exc:
            # DB 保存自体の失敗は期限の読み取り回復に委ね、別実行の状態を上書きしない。
            logger.error(
                "Oracle 反映の実行・保存に失敗しました。DB 接続ログを確認してください。",
                extra={"job_id": job_id, "error_type": type(exc).__name__},
            )

    def _assert_current_profile(self, job: ProfileSyncJobData) -> None:
        profile = self._service.get_profile(job.profile_id)
        if job.profile_etag and profile.etag != job.profile_etag:
            raise RuntimeError(
                "業務プロファイルが同期受付後に更新されました。最新版から再試行してください。"
            )

    @staticmethod
    def _profile_original_name(profile: Any) -> str:
        value = getattr(profile.select_ai_config, "previous_profile_name", "")
        return value.strip() if isinstance(value, str) else ""

    def _save(
        self,
        job: ProfileSyncJobData,
        *,
        previous: dict[str, Any] | None = None,
        claim: bool = False,
    ) -> dict[str, Any]:
        document: dict[str, Any] = {
            **(previous or {}),
            "job_id": job.job_id,
            "job_type": "profile_sync",
            "profile_id": job.profile_id,
            "status": job.status.value,
            "payload": job.model_dump(mode="json"),
        }
        if claim:
            document.update(claimed_by=f"profile-sync:{uuid4().hex}", claimed_at=time.time())
        saved = self.store.save_job(
            document, expected_etag=str(previous["etag"]) if previous else None
        )
        with self._lock:
            self._jobs[job.job_id] = job.model_copy(deep=True)
        return saved


profile_sync_service = ProfileSyncService()


__all__ = ["ProfileSyncService", "profile_sync_service"]
