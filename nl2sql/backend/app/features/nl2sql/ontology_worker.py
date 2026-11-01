"""Oracle 永続 queue を処理する独立 Ontology worker。"""

from __future__ import annotations

import argparse
import logging
import socket
import time
from typing import Any, cast

from app.settings import get_settings

logger = logging.getLogger(__name__)

# 以前の版にあり、今は処理しない job の種類（#7）。
_REMOVED_JOB_TYPES = frozenset({"profile_sync"})

# 実行前に失敗した job を即時に再 claim できる回数。超えたら claim 期限切れの回収に任せる。
_MAX_IMMEDIATE_RECLAIMS = 3


class OntologyWorker:
    """queued job を ETag 付き更新で 1 件だけ claim して処理する。"""

    def __init__(
        self,
        runtime: Any,
        build_service: Any,
        publish_service: Any,
    ) -> None:
        self.runtime = runtime
        self.build_service = build_service
        self.publish_service = publish_service
        self.worker_id = f"{socket.gethostname()}:{id(self)}"

    def claim_next(self) -> dict[str, Any] | None:
        settings = get_settings()
        now = time.time()
        claim_timeout = max(30.0, settings.nl2sql_ontology_worker_claim_timeout_seconds)
        queued = self.runtime.store.list_documents("jobs", {"status": "queued"})
        in_flight_statuses = ("claimed", "running", "materializing", "validating")
        stale_claims = [
            document
            for status in in_flight_statuses
            for document in self.runtime.store.list_documents("jobs", {"status": status})
            if float(document.get("claimed_at") or 0.0) <= now - claim_timeout
        ]
        # worker が異常終了しても、期限切れ claim を ETag 付きで安全に回収する。
        jobs = [*queued, *stale_claims]
        jobs.sort(key=lambda item: (str(item.get("created_at") or ""), str(item["job_id"])))
        for document in jobs:
            claimed = dict(document)
            claimed["claimed_from_status"] = document["status"]
            claimed["status"] = "claimed"
            claimed["claimed_by"] = self.worker_id
            claimed["claimed_at"] = now
            try:
                return cast(
                    dict[str, Any],
                    self.runtime.store.save_job(
                        claimed,
                        expected_etag=str(document["etag"]),
                    ),
                )
            except Exception:
                # 別 worker が先に claim した場合は次の候補を試す。
                logger.debug("ontology_job_claim_conflict", exc_info=True)
        return None

    def process_one(self) -> bool:
        document = self.claim_next()
        if document is None:
            return False
        job_id = str(document["job_id"])
        job_type = str(document.get("job_type") or "")
        logger.info("ontology_job_started", extra={"job_id": job_id, "job_type": job_type})
        try:
            self._dispatch(document, job_id, job_type)
        except Exception:
            # 実行側が状態を進める前に落ちた job を claim のまま残さない(claim 期限まで固まるため)
            self._release_claim(job_id)
            raise
        logger.info("ontology_job_finished", extra={"job_id": job_id, "job_type": job_type})
        return True

    def _dispatch(self, document: dict[str, Any], job_id: str, job_type: str) -> None:
        if job_type == "build":
            self.build_service.run_persisted(job_id)
        elif job_type == "markdown_prepare":
            from .ontology_markdown_workspace import MarkdownOntologyWorkspace

            MarkdownOntologyWorkspace(self.runtime).run_preparation(
                str(document["profile_id"]), job_id
            )
        elif job_type == "publish":
            self.publish_service.run_persisted(job_id)
        elif job_type == "definition_validation":
            from .ontology_definition_data_validation import run_validation_job

            run_validation_job(self.runtime, job_id)
        elif job_type in _REMOVED_JOB_TYPES:
            # 削除した機能（業務プロファイルの DB 側の AI profile への反映。#7）の job が
            # 以前の版から残っていても、claim と失敗を繰り返さないよう取り消して終える。
            self.runtime.store.save_job(
                {
                    **document,
                    "status": "cancelled",
                    "error_message_ja": "この処理は提供を終了しました。",
                },
                expected_etag=str(document["etag"]),
            )
        else:
            raise RuntimeError(f"未対応の Ontology job type です: {job_type}")

    def _release_claim(self, job_id: str) -> None:
        """自分の claim のまま残った job を claim 前の状態へ戻す(best-effort)。

        実行側(run_persisted 等)が running 等へ進めた job は、各 job type の回復処理に任せる。
        同じ job で連続して失敗する場合は即時の再 claim を繰り返さず、claim 期限切れの
        回収(claim_next の stale 判定)に任せる。
        """

        for _attempt in range(3):
            try:
                current = self.runtime.store.get_job(job_id)
                if (
                    current is None
                    or current.get("status") != "claimed"
                    or current.get("claimed_by") != self.worker_id
                ):
                    return
                failures = int(current.get("claim_failures") or 0) + 1
                if failures >= _MAX_IMMEDIATE_RECLAIMS:
                    logger.warning(
                        "ontology_job_claim_kept_after_failures",
                        extra={"job_id": job_id, "claim_failures": failures},
                    )
                    return
                self.runtime.store.save_job(
                    {
                        **{
                            key: value
                            for key, value in current.items()
                            if key not in {"etag", "created_at", "updated_at"}
                        },
                        "status": str(current.get("claimed_from_status") or "queued"),
                        "claimed_by": "",
                        "claimed_at": 0.0,
                        "claim_failures": failures,
                    },
                    expected_etag=str(current["etag"]),
                )
                return
            except Exception:
                logger.warning(
                    "ontology_job_claim_release_failed", exc_info=True, extra={"job_id": job_id}
                )


def main() -> None:
    parser = argparse.ArgumentParser(description="NL2SQL Ontology worker")
    parser.add_argument("--once", action="store_true", help="1 件だけ確認して終了する")
    args = parser.parse_args()

    # FastAPI と同じ store/runtime を再利用するが、HTTP server は起動しない。
    from .ontology_router import (
        ontology_build_service,
        ontology_publish_service,
        ontology_runtime,
    )

    worker = OntologyWorker(
        ontology_runtime,
        ontology_build_service,
        ontology_publish_service,
    )
    if args.once:
        worker.process_one()
        return
    poll_seconds = max(0.2, get_settings().nl2sql_ontology_worker_poll_seconds)
    while True:
        try:
            processed = worker.process_one()
        except Exception:
            # 1 件の異常(DB 瞬断等)で worker プロセスを落とさない
            logger.exception("ontology_worker_iteration_failed")
            processed = False
        if not processed:
            time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
