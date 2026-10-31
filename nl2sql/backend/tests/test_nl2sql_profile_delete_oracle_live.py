"""実 Oracle AI Database で、業務プロファイルの削除がオントロジーの成果物を残さないか（#1743）。

通常 CI では実行しない。`NL2SQL_RUN_ORACLE_INTEGRATION=1` の明示指定時だけ、一意な名前の一時の
業務プロファイル 2 つと、その成果物・ジョブの記録を作り、片方を削除して確かめ、finally で必ず消す。
LLM は呼ばない。資格情報は出力しない。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values

from app.features.nl2sql.incremental_store import OracleIncrementalNl2SqlRepository
from app.features.nl2sql.models import Nl2SqlProfile
from app.features.nl2sql.ontology_definition_service import profile_ontology_session_id
from app.features.nl2sql.ontology_store import OracleOntologyStore
from app.features.nl2sql.oracle_adapter import OracleNl2SqlAdapter
from app.settings import get_settings

pytestmark = pytest.mark.skipif(
    os.getenv("NL2SQL_RUN_ORACLE_INTEGRATION") != "1",
    reason="NL2SQL_RUN_ORACLE_INTEGRATION=1 の明示指定が必要です。",
)


@pytest.fixture
def live_settings() -> Iterator[Any]:
    """conftest が PLATFORM_ORACLE_USER=APP を強制するため、共通 .env の接続ユーザーへ戻す。"""
    settings = get_settings()
    env_user = str(
        dotenv_values(Path(__file__).resolve().parents[3] / "platform" / ".env").get(
            "PLATFORM_ORACLE_USER"
        )
        or os.environ.get("PLATFORM_ORACLE_USER", "")
    )
    original_user = settings.oracle_user
    if env_user:
        settings.oracle_user = env_user
    try:
        yield settings
    finally:
        settings.oracle_user = original_user


def _artifact(artifact_id: str, session_id: str, profile_id: str | None) -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "session_id": session_id,
        "artifact_type": "ontology_build_checkpoint",
        "content": "{}",
        "content_hash": "0" * 64,
        **({"profile_id": profile_id} if profile_id else {}),
    }


def test_profile_delete_removes_its_ontology_artifacts_on_oracle(live_settings: Any) -> None:
    adapter = OracleNl2SqlAdapter(live_settings)
    repository = OracleIncrementalNl2SqlRepository(connection_factory=adapter.state_connection)
    store = OracleOntologyStore(connection_factory=adapter.state_connection)
    store.ensure_schema()
    tag = uuid.uuid4().hex[:10]
    deleted_id, kept_id, ghost_id = (str(uuid.uuid4()) for _ in range(3))
    profiles = [
        repository.save_profile(
            Nl2SqlProfile(id=profile_id, name=f"T1743_{name}_{tag}"), expected_etag=None
        )
        for profile_id, name in ((deleted_id, "DELETED"), (kept_id, "KEPT"))
    ]
    revision_session = f"ontology_revision_t1743_{tag}"
    ids = {
        # 削除する業務プロファイルの checkpoint と、revision の session の下書き。
        "own_checkpoint": _artifact(
            f"t1743_own_{tag}", profile_ontology_session_id(deleted_id), deleted_id
        ),
        "own_draft": _artifact(f"t1743_draft_{tag}", revision_session, deleted_id),
        # 前に削除した（存在しない）業務プロファイルの残り。
        "ghost": _artifact(f"t1743_ghost_{tag}", profile_ontology_session_id(ghost_id), None),
        # 残すもの: 別の業務プロファイルの成果物と、所有者の無い成果物。
        "kept": _artifact(f"t1743_kept_{tag}", profile_ontology_session_id(kept_id), kept_id),
        "shared": _artifact(f"t1743_shared_{tag}", revision_session, None),
    }
    jobs = {
        "finished": f"t1743_job_done_{tag}",
        "running": f"t1743_job_running_{tag}",
        "global": f"t1743_job_global_{tag}",
    }
    try:
        for document in ids.values():
            store.save_artifact(document)
        for key, profile_id, status in (
            ("finished", deleted_id, "succeeded"),
            ("running", deleted_id, "running"),
            ("global", "-", "succeeded"),
        ):
            store.save_job(
                {
                    "job_id": jobs[key],
                    "job_type": "build",
                    "profile_id": profile_id,
                    "status": status,
                    "payload": {},
                }
            )

        repository.delete_profile(deleted_id, expected_etag=profiles[0].etag)

        remaining = {
            key for key, document in ids.items() if store.get_artifact(document["artifact_id"])
        }
        assert remaining == {"kept", "shared"}
        # 終わったジョブは消し、実行中のジョブ（削除の後に取り消す）と全体のジョブは残す。
        assert store.get_job(jobs["finished"]) is None
        assert store.get_job(jobs["running"]) is not None
        assert store.get_job(jobs["global"]) is not None
        assert repository.get_profile(deleted_id) is None
        assert repository.get_profile(kept_id) is not None
    finally:
        for document in ids.values():
            store.delete_documents("artifacts", {"artifact_id": document["artifact_id"]})
        for job_id in jobs.values():
            store.delete_documents("jobs", {"job_id": job_id})
        for profile in profiles:
            current = repository.get_profile(profile.id)
            if current is not None:
                repository.delete_profile(profile.id, expected_etag=current.etag)


def test_system_schema_initialize_purges_orphan_ontology_records_on_oracle(
    live_settings: Any,
) -> None:
    """system schema の初期化で、存在しない業務プロファイルの成果物・終わったジョブを消す（#1758）。

    実行中のジョブ・全体のジョブ・存在する業務プロファイルの記録・所有者の無い成果物は残す。
    """

    from app.features.settings.system_schema import system_schema_manager

    adapter = OracleNl2SqlAdapter(live_settings)
    repository = OracleIncrementalNl2SqlRepository(connection_factory=adapter.state_connection)
    store = OracleOntologyStore(connection_factory=adapter.state_connection)
    store.ensure_schema()
    tag = uuid.uuid4().hex[:10]
    kept_id, ghost_id = str(uuid.uuid4()), str(uuid.uuid4())
    kept = repository.save_profile(
        Nl2SqlProfile(id=kept_id, name=f"T1758_KEPT_{tag}"), expected_etag=None
    )
    artifacts = {
        "ghost": _artifact(f"t1758_ghost_{tag}", profile_ontology_session_id(ghost_id), ghost_id),
        "kept": _artifact(f"t1758_kept_{tag}", profile_ontology_session_id(kept_id), kept_id),
        "shared": _artifact(f"t1758_shared_{tag}", f"ontology_revision_t1758_{tag}", None),
    }
    jobs = {
        "ghost_done": (ghost_id, "succeeded"),
        "ghost_failed": (ghost_id, "failed"),
        "ghost_running": (ghost_id, "running"),
        "kept_done": (kept_id, "succeeded"),
        "global": ("-", "succeeded"),
    }
    job_ids = {key: f"t1758_{key}_{tag}" for key in jobs}
    try:
        for document in artifacts.values():
            store.save_artifact(document)
        for key, (profile_id, status) in jobs.items():
            store.save_job(
                {
                    "job_id": job_ids[key],
                    "job_type": "build",
                    "profile_id": profile_id,
                    "status": status,
                    "payload": {},
                }
            )

        result = system_schema_manager.initialize()

        assert result["status"] == "ready"
        remaining = {
            key
            for key, document in artifacts.items()
            if store.get_artifact(document["artifact_id"])
        }
        assert remaining == {"kept", "shared"}
        assert {key for key in jobs if store.get_job(job_ids[key]) is not None} == {
            "ghost_running",
            "kept_done",
            "global",
        }
        assert repository.get_profile(kept_id) is not None
    finally:
        for document in artifacts.values():
            store.delete_documents("artifacts", {"artifact_id": document["artifact_id"]})
        for job_id in job_ids.values():
            store.delete_documents("jobs", {"job_id": job_id})
        current = repository.get_profile(kept.id)
        if current is not None:
            repository.delete_profile(kept.id, expected_etag=current.etag)
