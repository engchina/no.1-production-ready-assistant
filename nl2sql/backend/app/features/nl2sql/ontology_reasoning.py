"""OWL 2 RL materialization と SHACL Core gate を含む Ontology publish worker。"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from app.settings import get_settings

from .ontology_models import (
    OntologyPublishJob,
    OntologyPublishStatus,
    OntologyReasoningStatus,
    OntologyRevisionStatus,
    utc_now,
)
from .ontology_observability import (
    observe_stage,
    record_job,
    record_reasoning_triples,
    record_shacl_validation,
)
from .ontology_semantics import (
    ONTOLOGY_RENDERER_VERSION,
    ShaclValidationResult,
    build_semantic_artifacts,
    materialize_local_owl2rl,
    revision_graph_names,
    validate_shacl_core,
)
from .ontology_service import OntologyStateConflictError, OntologyVersionConflictError
from .ontology_store import OntologyStore, OntologyVersionConflict, canonical_json

logger = logging.getLogger(__name__)
_IN_FLIGHT_PUBLISH_STATUSES = frozenset(
    {
        OntologyPublishStatus.QUEUED,
        OntologyPublishStatus.MATERIALIZING,
        OntologyPublishStatus.VALIDATING,
    }
)


class Owl2RlMaterializer(Protocol):
    def materialize(
        self,
        *,
        asserted_turtle: str,
        rdf_graph_name: str,
        inferred_graph_name: str,
    ) -> str:
        """Materialize and return a Turtle representation of the closure."""


class LocalOwl2RlMaterializer:
    def materialize(
        self,
        *,
        asserted_turtle: str,
        rdf_graph_name: str,
        inferred_graph_name: str,
    ) -> str:
        del rdf_graph_name, inferred_graph_name
        return materialize_local_owl2rl(asserted_turtle)


class PublishExecutionLost(RuntimeError):
    """失効した公開 worker は成果物を更新しない。"""


class OntologyPublishService:
    def __init__(
        self,
        runtime: Any,
        *,
        materializer: Owl2RlMaterializer | None = None,
    ) -> None:
        self.runtime = runtime
        self.store: OntologyStore = runtime.store
        self._materializer = materializer or self._default_materializer()
        self._jobs: dict[str, OntologyPublishJob] = {}
        self._lock = threading.RLock()
        self._execution_ids: dict[str, str] = {}
        self._inprocess_jobs: set[str] = set()

    def _default_materializer(self) -> Owl2RlMaterializer:
        settings = get_settings()
        if settings.nl2sql_ontology_reasoning_profile.strip().lower() != "owl2rl":
            raise RuntimeError("Ontology 推論 profile は OWL 2 RL だけを指定できます。")
        return LocalOwl2RlMaterializer()

    def start(
        self,
        revision_id: str,
        *,
        etag: str,
        idempotency_key: str,
        profile_id: str = "",
    ) -> OntologyPublishJob:
        normalized_profile_id = profile_id.strip()
        request_hash = hashlib.sha256(
            canonical_json(
                {
                    "revision_id": revision_id,
                    "etag": etag,
                    "profile_id": normalized_profile_id,
                }
            ).encode("utf-8")
        ).hexdigest()
        existing = self.store.get_idempotency("publish_ontology", idempotency_key)
        if existing is not None:
            if existing.get("request_hash") != request_hash:
                raise OntologyVersionConflictError(
                    "IDEMPOTENCY_KEY_REUSED",
                    "同じ Idempotency-Key が別の公開リクエストに使用されています。",
                )
            restored = self.get(str(existing.get("resource_id") or ""))
            if restored is not None:
                return restored
        ontology = self.runtime.ontology_revision(revision_id)
        if ontology.revision.status != OntologyRevisionStatus.DRAFT:
            raise OntologyStateConflictError(
                "ONTOLOGY_REVISION_NOT_DRAFT",
                "Draft 状態の Ontology revision だけを公開できます。",
            )
        if ontology.revision.etag != etag:
            raise OntologyVersionConflictError(
                "REVISION_ETAG_MISMATCH",
                "Ontology revision が更新されています。再読込してください。",
            )
        active_job = self._active_job_for_revision(
            revision_id,
            etag=etag,
            profile_id=normalized_profile_id,
        )
        if active_job is not None:
            self.store.save_idempotency(
                {
                    "operation": "publish_ontology",
                    "idempotency_key": idempotency_key,
                    "request_hash": request_hash,
                    "resource_id": active_job.id,
                    "status": "accepted",
                }
            )
            return active_job
        job = OntologyPublishJob(
            id=f"ontology_publish_{uuid4().hex}",
            revision_id=revision_id,
            requested_etag=etag,
            profile_id=normalized_profile_id,
        )
        self._save_job(job)
        self.store.save_idempotency(
            {
                "operation": "publish_ontology",
                "idempotency_key": idempotency_key,
                "request_hash": request_hash,
                "resource_id": job.id,
                "status": "accepted",
            }
        )
        if get_settings().nl2sql_ontology_worker_mode == "inprocess":
            with self._lock:
                self._inprocess_jobs.add(job.id)
            threading.Thread(
                target=self._run_safely,
                args=(job.id, etag),
                daemon=True,
                name=f"{job.id}-worker",
            ).start()
        return job.model_copy(deep=True)

    def peek(self, job_id: str) -> OntologyPublishJob | None:
        document = self.store.get_job(job_id)
        if document is None or document.get("job_type") != "publish":
            return None
        return OntologyPublishJob.model_validate(document["payload"])

    @staticmethod
    def _expired(document: Mapping[str, Any]) -> bool:
        job = OntologyPublishJob.model_validate(document["payload"])
        try:
            deadline = datetime.fromisoformat(str(document["deadline_at"]))
        except (KeyError, ValueError, TypeError):
            deadline = job.created_at + timedelta(
                seconds=get_settings().nl2sql_ontology_publish_timeout_seconds
            )
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        return utc_now() >= deadline

    def _recover(self, job_id: str, *, interrupted: bool = False) -> OntologyPublishJob | None:
        for _ in range(3):
            document = self.store.get_job(job_id)
            if document is None or document.get("job_type") != "publish":
                return None
            job = OntologyPublishJob.model_validate(document["payload"])
            recoverable_failure = job.status == OntologyPublishStatus.FAILED and job.error_code in {
                "ONTOLOGY_PUBLISH_TIMEOUT",
                "ONTOLOGY_PUBLISH_INTERRUPTED",
                "ONTOLOGY_PUBLISH_RECOVERY_REQUIRED",
            }
            if job.status not in _IN_FLIGHT_PUBLISH_STATUSES and not recoverable_failure:
                return job
            if not recoverable_failure and not interrupted and not self._expired(document):
                return job
            code = "ONTOLOGY_PUBLISH_INTERRUPTED" if interrupted else "ONTOLOGY_PUBLISH_TIMEOUT"
            message = (
                "バックエンドの停止により公開処理を中断しました。"
                if interrupted
                else "公開処理の実行期限を超えました。"
            ) + "保存済みの版と公開状態を確認してから再実行してください。"
            updates: dict[str, Any] = {
                "status": OntologyPublishStatus.FAILED,
                "error_code": code,
                "error_message_ja": message,
                "finished_at": utc_now(),
            }
            # 公開 commit 後に job 保存だけが失われたケースは再公開しない。
            revision_doc = self.store.get_document("revisions", {"revision_id": job.revision_id})
            revision = revision_doc.get("payload", {}) if revision_doc else {}
            committed = (
                revision.get("status") in {"published", "archived"}
                and revision.get("reasoning_status") == "ready"
                and (
                    revision.get("publish_job_id") == job.id
                    or (
                        not revision.get("publish_job_id")
                        and job.rdf_graph_name
                        and revision.get("rdf_graph_name") == job.rdf_graph_name
                    )
                )
            )
            if recoverable_failure and not committed:
                return job
            if committed:
                try:
                    # draft は公開後に変更不可。コピーだけを再開し、head/推論は再送しない。
                    self.runtime.copy_draft_markdown_to_published(
                        job.revision_id, profile_id=job.profile_id
                    )
                    updates.update(
                        status=OntologyPublishStatus.SUCCEEDED,
                        error_code="",
                        error_message_ja="",
                        rdf_graph_name=revision.get("rdf_graph_name", ""),
                        inferred_graph_name=revision.get("inferred_graph_name", ""),
                        shacl_report_artifact_id=revision.get("shacl_report_artifact_id", ""),
                        warnings_ja=[
                            *job.warnings_ja,
                            "公開済みの版を照合して完了状態を復元しました。再公開はしていません。",
                        ],
                    )
                except Exception:
                    logger.exception(
                        "公開済み Markdown の復元に失敗しました。", extra={"job_id": job_id}
                    )
                    updates.update(
                        error_code="ONTOLOGY_PUBLISH_RECOVERY_REQUIRED",
                        error_message_ja=(
                            "版の公開は完了していますが Markdown の保存確認に失敗しました。"
                            "再公開せず管理者に保存状態の確認を依頼してください。"
                        ),
                    )
            updated = job.model_copy(update=updates)
            try:
                self.store.save_job(
                    {
                        **document,
                        "status": updated.status.value,
                        "payload": updated.model_dump(mode="json"),
                    },
                    expected_etag=document["etag"],
                )
            except OntologyVersionConflict:
                continue
            if revision_doc and revision.get("status") == "draft" and not committed:
                # 別 worker の新しい進捗・公開結果を古い失敗で上書きしない。
                with suppress(OntologyVersionConflict):
                    self.store.save_document(
                        "revisions",
                        {**revision_doc, "payload": {**revision, "reasoning_status": "failed"}},
                        expected_etag=revision_doc["etag"],
                    )
            logger.warning(
                "公開 job の中断状態を回復しました。",
                extra={
                    "job_id": job_id,
                    "revision_id": job.revision_id,
                    "error_code": updated.error_code,
                    "status": updated.status.value,
                },
            )
            return updated
        return self.peek(job_id)

    def get(self, job_id: str) -> OntologyPublishJob | None:
        # API worker の古いキャッシュより永続状態を優先する。
        return self._recover(job_id)

    def shutdown(self) -> None:
        with self._lock:
            owned = tuple(self._inprocess_jobs)
        for job_id in owned:
            try:
                self._recover(job_id, interrupted=True)
            except Exception:
                logger.exception(
                    "公開 job の停止状態を保存できませんでした。", extra={"job_id": job_id}
                )

    def _assert_execution(self, job_id: str) -> dict[str, Any]:
        self._recover(job_id)
        current = self.store.get_job(job_id)
        with self._lock:
            token = self._execution_ids.get(job_id)
        if (
            not current
            or not token
            or current.get("execution_id") != token
            or OntologyPublishJob.model_validate(current["payload"]).status
            not in _IN_FLIGHT_PUBLISH_STATUSES
        ):
            raise PublishExecutionLost(job_id)
        return current

    def _claim(self, job_id: str) -> bool:
        job = self.get(job_id)
        if not job or job.status != OntologyPublishStatus.QUEUED:
            return False
        current = self.store.get_job(job_id)
        if current is None or current["payload"]["status"] != "queued":
            return False
        token = uuid4().hex
        job.status = OntologyPublishStatus.MATERIALIZING
        job.started_at = utc_now()
        try:
            self.store.save_job(
                {
                    **current,
                    "status": job.status.value,
                    "payload": job.model_dump(mode="json"),
                    "execution_id": token,
                    "claimed_at": time.time(),
                },
                expected_etag=current["etag"],
            )
        except OntologyVersionConflict:
            return False
        with self._lock:
            self._execution_ids[job_id] = token
        return True

    def _save_job(self, job: OntologyPublishJob) -> None:
        current = self.store.get_job(job.id)
        if current is not None:
            current = self._assert_execution(job.id)
        document = {
            **(current or {}),
            "job_id": job.id,
            "job_type": "publish",
            "profile_id": job.profile_id or "-",
            "status": job.status.value,
            "payload": job.model_dump(mode="json"),
            "deadline_at": (current or {}).get("deadline_at")
            or (
                job.created_at
                + timedelta(seconds=get_settings().nl2sql_ontology_publish_timeout_seconds)
            ).isoformat(),
        }
        self.store.save_job(document, expected_etag=str(current["etag"]) if current else None)
        with self._lock:
            self._jobs[job.id] = job.model_copy(deep=True)

    def _update(self, job_id: str, **updates: Any) -> OntologyPublishJob:
        current = self.get(job_id)
        if current is None:
            raise RuntimeError("Ontology publish job が見つかりません。")
        updated = current.model_copy(update=updates, deep=True)
        self._save_job(updated)
        return updated

    def _run_safely(self, job_id: str, etag: str) -> None:
        if not self._claim(job_id):
            with self._lock:
                if job_id not in self._execution_ids:
                    self._inprocess_jobs.discard(job_id)
            return
        try:
            self._run_claimed(job_id, etag=etag)
        except (PublishExecutionLost, OntologyVersionConflict):
            logger.info("失効した公開 worker の結果を破棄しました。", extra={"job_id": job_id})
        except Exception as exc:
            logger.exception("Ontology publish worker failed", extra={"job_id": job_id})
            try:
                current = self.peek(job_id)
                revision = (
                    self.store.get_document("revisions", {"revision_id": current.revision_id})
                    if current
                    else None
                )
                if revision and revision.get("payload", {}).get("publish_job_id") == job_id:
                    self._recover(job_id, interrupted=True)
                    return
                self._update(
                    job_id,
                    status=OntologyPublishStatus.FAILED,
                    error_code="ONTOLOGY_PUBLISH_FAILED",
                    error_message_ja="公開処理に失敗しました。保存状態とバックエンドのログを確認してください。",
                    finished_at=utc_now(),
                )
                failed_job = self.peek(job_id)
                if failed_job is not None:
                    self._mark_draft_reasoning_failed(failed_job.revision_id)
            except (PublishExecutionLost, OntologyVersionConflict):
                pass
            record_job(job_type="publish", status="failed", error_code=type(exc).__name__)
        finally:
            with self._lock:
                self._execution_ids.pop(job_id, None)
                self._inprocess_jobs.discard(job_id)

    def _active_job_for_revision(
        self,
        revision_id: str,
        *,
        etag: str,
        profile_id: str = "",
    ) -> OntologyPublishJob | None:
        for document in self.store.list_documents("jobs", {"job_type": "publish"}):
            try:
                job = self.get(str(document["job_id"]))
                if job is None:
                    continue
            except Exception:
                logger.warning(
                    "ontology_publish_job_restore_skipped",
                    exc_info=True,
                    extra={"revision_id": revision_id},
                )
                continue
            if (
                job.revision_id == revision_id
                and job.requested_etag == etag
                and job.profile_id == profile_id
                and job.status in _IN_FLIGHT_PUBLISH_STATUSES
            ):
                with self._lock:
                    self._jobs[job.id] = job.model_copy(deep=True)
                return job.model_copy(deep=True)
        return None

    def _mark_draft_reasoning_failed(self, revision_id: str) -> None:
        ontology = self.runtime.ontology_revision(revision_id)
        if ontology.revision.status != OntologyRevisionStatus.DRAFT:
            logger.warning(
                "ontology_publish_failed_reasoning_status_preserved",
                extra={
                    "revision_id": revision_id,
                    "revision_status": ontology.revision.status.value,
                },
            )
            return
        self.runtime.update_reasoning_status(
            revision_id,
            OntologyReasoningStatus.FAILED,
        )

    def run_persisted(self, job_id: str) -> OntologyPublishJob:
        job = self.get(job_id)
        if job is None:
            raise RuntimeError("Ontology publish job が見つかりません。")
        self._run_safely(job_id, job.requested_etag)
        result = self.get(job_id)
        if result is None:
            raise RuntimeError("Ontology publish job の実行結果を取得できません。")
        return result

    def run(self, job_id: str, *, etag: str) -> OntologyPublishJob:
        if not self._claim(job_id):
            existing = self.get(job_id)
            if existing is None:
                raise RuntimeError("Ontology publish job が見つかりません。")
            return existing
        try:
            return self._run_claimed(job_id, etag=etag)
        finally:
            with self._lock:
                self._execution_ids.pop(job_id, None)

    def _run_claimed(self, job_id: str, *, etag: str) -> OntologyPublishJob:
        initial_job = self.get(job_id)
        if initial_job is None:
            raise RuntimeError("Ontology publish job が見つかりません。")
        ontology = self.runtime.validate_ontology_for_publish(
            initial_job.revision_id,
            etag=etag,
        )
        job = self._update(
            job_id,
            status=OntologyPublishStatus.MATERIALIZING,
            started_at=utc_now(),
        )
        self._assert_execution(job_id)
        self.runtime.update_reasoning_status(
            job.revision_id,
            OntologyReasoningStatus.MATERIALIZING,
        )
        artifacts = build_semantic_artifacts(ontology)
        draft_markdown_reader = getattr(self.runtime, "draft_markdown_for_revision", None)
        draft_markdown = str(
            draft_markdown_reader(job.revision_id, profile_id=job.profile_id)
            if callable(draft_markdown_reader)
            else ""
        ).strip()
        llm_markdown = draft_markdown or artifacts.llm_markdown
        rdf_graph_name, inferred_graph_name = revision_graph_names(job.revision_id)
        with observe_stage("owl2rl_materialize"):
            inferred_turtle = self._materializer.materialize(
                asserted_turtle=artifacts.owl_turtle,
                rdf_graph_name=rdf_graph_name,
                inferred_graph_name=inferred_graph_name,
            )
        from rdflib import Graph

        self._assert_execution(job_id)
        inferred_graph = Graph().parse(data=inferred_turtle, format="turtle")
        record_reasoning_triples(len(inferred_graph))
        self._update(
            job_id,
            status=OntologyPublishStatus.VALIDATING,
            rdf_graph_name=rdf_graph_name,
            inferred_graph_name=inferred_graph_name,
        )
        self.runtime.update_reasoning_status(
            job.revision_id,
            OntologyReasoningStatus.VALIDATING,
            rdf_graph_name=rdf_graph_name,
            inferred_graph_name=inferred_graph_name,
        )
        shacl_enabled = get_settings().nl2sql_ontology_shacl_enabled
        if shacl_enabled:
            with observe_stage("shacl_core_validate"):
                validation = validate_shacl_core(
                    asserted_turtle=artifacts.owl_turtle,
                    inferred_turtle=inferred_turtle,
                    shapes_turtle=artifacts.shacl_turtle,
                )
            record_shacl_validation(conforms=validation.conforms)
        else:
            validation = ShaclValidationResult(
                conforms=True,
                report_text="SHACL Core validation is disabled for rollout.",
                report_turtle="# SHACL Core validation is disabled for rollout.\n",
            )
        self._update(
            job_id,
            shacl_conforms=validation.conforms if shacl_enabled else None,
            warnings_ja=(
                [] if shacl_enabled else ["段階導入設定により SHACL Core 検証をスキップしました。"]
            ),
        )
        artifact_values: Mapping[str, str] = {
            "ontology_owl_turtle": artifacts.owl_turtle,
            "ontology_inferred_turtle": inferred_turtle,
            "ontology_shacl_turtle": artifacts.shacl_turtle,
            "ontology_llm_markdown": llm_markdown,
            "ontology_mermaid": artifacts.mermaid,
            "ontology_shacl_report": validation.report_turtle,
        }
        report_artifact_id = ""
        for artifact_type, content in artifact_values.items():
            self._assert_execution(job_id)
            artifact_id = f"ontology_artifact_{uuid4().hex}"
            self.store.save_artifact(
                {
                    "artifact_id": artifact_id,
                    "session_id": job.revision_id,
                    "artifact_type": artifact_type,
                    "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "content": content,
                    **({"profile_id": job.profile_id} if job.profile_id else {}),
                    "renderer_version": ONTOLOGY_RENDERER_VERSION,
                    "created_at": utc_now(),
                }
            )
            if artifact_type == "ontology_shacl_report":
                report_artifact_id = artifact_id
        if not validation.conforms:
            self._update(
                job_id,
                status=OntologyPublishStatus.FAILED,
                shacl_conforms=False,
                shacl_report_artifact_id=report_artifact_id,
                error_code="ONTOLOGY_SHACL_VIOLATION",
                error_message_ja="SHACL Core の Violation があるため公開を中止しました。",
                finished_at=utc_now(),
            )
            self.runtime.update_reasoning_status(
                job.revision_id,
                OntologyReasoningStatus.FAILED,
                rdf_graph_name=rdf_graph_name,
                inferred_graph_name=inferred_graph_name,
                shacl_report_artifact_id=report_artifact_id,
            )
            record_job(
                job_type="publish",
                status="failed",
                error_code="ONTOLOGY_SHACL_VIOLATION",
            )
            return self.get(job_id) or job
        self._assert_execution(job_id)
        self.runtime.finalize_semantic_publish(
            job.revision_id,
            etag=etag,
            publication_guard=lambda: self._assert_execution(job_id),
            semantic_metadata={
                "publish_job_id": job_id,
                "reasoning_status": OntologyReasoningStatus.READY,
                "rdf_graph_name": rdf_graph_name,
                "inferred_graph_name": inferred_graph_name,
                "shacl_report_artifact_id": report_artifact_id,
                "renderer_version": ONTOLOGY_RENDERER_VERSION,
                "artifact_hashes": {
                    **artifacts.hashes,
                    "llm_markdown": hashlib.sha256(llm_markdown.encode("utf-8")).hexdigest(),
                    "published_markdown": hashlib.sha256(llm_markdown.encode("utf-8")).hexdigest(),
                    "inferred_turtle": hashlib.sha256(inferred_turtle.encode("utf-8")).hexdigest(),
                },
            },
        )
        self._assert_execution(job_id)
        markdown_publisher = getattr(self.runtime, "copy_draft_markdown_to_published", None)
        if callable(markdown_publisher):
            markdown_publisher(job.revision_id, profile_id=job.profile_id)
        published = self._update(
            job_id,
            status=OntologyPublishStatus.SUCCEEDED,
            rdf_graph_name=rdf_graph_name,
            inferred_graph_name=inferred_graph_name,
            shacl_conforms=True if shacl_enabled else None,
            shacl_report_artifact_id=report_artifact_id,
            warnings_ja=(
                [] if shacl_enabled else ["段階導入設定により SHACL Core 検証をスキップしました。"]
            ),
            finished_at=utc_now(),
        )
        record_job(job_type="publish", status="succeeded")
        return published
