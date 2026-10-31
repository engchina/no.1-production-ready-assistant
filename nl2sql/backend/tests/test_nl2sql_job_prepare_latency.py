"""ジョブの準備（`prepare_context`）が、オントロジーの版のグラフの読み込みと runtime の lock を
待たないこと（#1155）。

チャットと SQL 生成の画面のジョブは、同じ worker の `_run_job` を通る。準備の段階は、
業務プロファイルの公開版（business_release_id）を確定する。以前は `ontology_markdown_state` で
確定しており、下書き・公開の Markdown の成果物が指す版ごとに、版のグラフ（nodes / edges /
embedding。1 版で約 3,000 行）を DB から読み、その間オントロジーの runtime の lock を持ち続けて
いた（共有の DB では準備だけで 3 分）。確定に要るのは、公開中の snapshot の ID か、公開中の版の
ID だけなので、成果物と版の header だけを読む。
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Any

import pytest
from test_nl2sql_job_runtime import _PROFILE_ID, _repository, _request, _worker

from app.features.nl2sql import ontology_router
from app.features.nl2sql.models import JobCreateRequest, JobStatus
from app.features.nl2sql.ontology_markdown_workspace import HEAD, MarkdownOntologyWorkspace
from app.features.nl2sql.ontology_models import OntologyRevision, OntologyRevisionStatus
from app.features.nl2sql.ontology_router import OntologyApiRuntime
from app.features.nl2sql.ontology_store import InMemoryOntologyStore, stable_ontology_id
from app.features.nl2sql.service import Nl2SqlService
from app.settings import get_settings

_PREPARE_DEADLINE_SECONDS = 5.0
_DRAFT = "ontology_markdown_draft"
_PUBLISHED = "ontology_markdown_published"


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> Nl2SqlService:
    # in-process の worker を起こさず、テストが worker を明示的に動かす。
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    return _worker(_repository())


@pytest.fixture
def store() -> InMemoryOntologyStore:
    return InMemoryOntologyStore()


@pytest.fixture
def runtime(
    monkeypatch: pytest.MonkeyPatch, service: Nl2SqlService, store: InMemoryOntologyStore
) -> OntologyApiRuntime:
    """ジョブを動かす service に結び付いた runtime（本番と同じ legacy_service is self）。"""

    bound = OntologyApiRuntime(legacy_service=service, store=store)
    monkeypatch.setattr(ontology_router, "ontology_runtime", bound)
    return bound


def _chat_request() -> JobCreateRequest:
    return _request().model_copy(update={"generation_only": True, "use_ontology_context": True})


def _save_revision(
    store: InMemoryOntologyStore,
    revision_id: str,
    status: OntologyRevisionStatus,
    *,
    version: int = 1,
    profile_id: str = "",
) -> None:
    revision = OntologyRevision(
        id=revision_id, version=version, status=status, profile_id=profile_id
    )
    store.save_document(
        "revisions",
        {
            "revision_id": revision_id,
            "status": status.value,
            "schema_fingerprint": "",
            "profile_id": profile_id,
            "payload": revision.model_dump(mode="json"),
        },
    )


def _save_markdown(
    store: InMemoryOntologyStore,
    *,
    profile_id: str,
    revision_id: str,
    artifact_type: str,
    updated_at: str = "2026-09-01T00:00:00Z",
    profile_version: int | None = None,
) -> None:
    markdown = f"# {profile_id} {revision_id} {artifact_type}"
    document: dict[str, Any] = {
        "artifact_id": OntologyApiRuntime._markdown_artifact_id(  # noqa: SLF001
            artifact_type=artifact_type, profile_id=profile_id, revision_id=revision_id
        ),
        "session_id": revision_id,
        "artifact_type": artifact_type,
        "content_hash": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        "content": markdown,
        "profile_id": profile_id,
        "created_at": updated_at,
        "updated_at": updated_at,
    }
    if profile_version is not None:
        document["profile_revision_version"] = profile_version
    store.save_artifact(document)


def _seed_profile_with_unpublished_history(store: InMemoryOntologyStore) -> None:
    """利用者の業務プロファイル ALL と同じ形。

    下書き 3 版・公開済み（後に保管）2 版の成果物があり、公開中の版は
    この業務プロファイルのものではない。
    """

    _save_revision(store, "ontology_revision_global", OntologyRevisionStatus.PUBLISHED, version=9)
    _save_revision(store, "ontology_revision_a", OntologyRevisionStatus.ARCHIVED, version=1)
    _save_revision(store, "ontology_revision_b", OntologyRevisionStatus.ARCHIVED, version=2)
    _save_revision(store, "ontology_revision_c", OntologyRevisionStatus.DRAFT, version=3)
    for revision_id in ("ontology_revision_a", "ontology_revision_b", "ontology_revision_c"):
        _save_markdown(store, profile_id=_PROFILE_ID, revision_id=revision_id, artifact_type=_DRAFT)
    for revision_id in ("ontology_revision_a", "ontology_revision_b"):
        _save_markdown(
            store, profile_id=_PROFILE_ID, revision_id=revision_id, artifact_type=_PUBLISHED
        )
    # 公開中の版の Markdown は別の業務プロファイルのもの（この業務プロファイルの公開版ではない）。
    _save_markdown(
        store,
        profile_id="other-profile",
        revision_id="ontology_revision_global",
        artifact_type=_PUBLISHED,
    )


def _record_graph_reads(monkeypatch: pytest.MonkeyPatch, runtime: OntologyApiRuntime) -> list[str]:
    """版のグラフ・全版の header・カタログの同期を読む runtime の処理の呼び出しを記録する。"""

    calls: list[str] = []
    for name in (
        "ontology_markdown_state",
        "_load_ontology_revision",
        "_load_revision_headers",
        "_load_published_revision",
        "_sync_ontology",
        "_profile_markdown_artifacts",
    ):
        original = getattr(runtime, name)

        def recorder(
            *args: Any, _name: str = name, _original: Any = original, **kwargs: Any
        ) -> Any:
            calls.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(runtime, name, recorder)
    return calls


def _calls_until_generation(
    monkeypatch: pytest.MonkeyPatch, service: Nl2SqlService, calls: list[str]
) -> tuple[list[str], threading.Event]:
    """生成の段階に入った時点（＝準備の段階の終わり）までの呼び出しを取り出す。"""

    before_generation: list[str] = []
    generation_started = threading.Event()
    original = service._generate_selected_engine  # noqa: SLF001

    def generate(*args: Any, **kwargs: Any) -> Any:
        if not generation_started.is_set():
            before_generation.extend(calls)
            generation_started.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_generate_selected_engine", generate)
    return before_generation, generation_started


def test_prepare_resolves_release_without_reading_revision_graphs(
    monkeypatch: pytest.MonkeyPatch,
    service: Nl2SqlService,
    store: InMemoryOntologyStore,
    runtime: OntologyApiRuntime,
) -> None:
    """準備の段階は、版のグラフ・全版の header・全成果物を読まずに公開版を確定する。"""

    _seed_profile_with_unpublished_history(store)
    calls = _record_graph_reads(monkeypatch, runtime)
    before_generation, _ = _calls_until_generation(monkeypatch, service, calls)

    created = service.start_job(_chat_request(), actor_user_uuid="user-1")
    assert service.run_next_nl2sql_job(worker_id="prepare-test", job_id=created.job_id)
    job = service.get_job(created.job_id, actor_user_uuid="user-1")
    assert job is not None
    assert job.status == JobStatus.DONE
    assert job.business_release_id == ""
    assert before_generation == [], f"準備の段階で重い読み取りをした: {before_generation}"


def test_prepare_does_not_wait_for_ontology_runtime_lock(
    monkeypatch: pytest.MonkeyPatch,
    service: Nl2SqlService,
    store: InMemoryOntologyStore,
    runtime: OntologyApiRuntime,
) -> None:
    """別の処理がオントロジーの lock を持ち続けても、準備の段階は待たずに生成へ進む。"""

    _seed_profile_with_unpublished_history(store)
    _, generation_started = _calls_until_generation(monkeypatch, service, [])
    created = service.start_job(_chat_request(), actor_user_uuid="user-1")

    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with runtime._lock:  # noqa: SLF001 - lock を持ったまま DB を読む別の処理の代わり
            held.set()
            release.wait(30)

    holder = threading.Thread(target=hold, name="ontology-lock-holder", daemon=True)
    holder.start()
    assert held.wait(5)
    worker = threading.Thread(
        target=service.run_next_nl2sql_job,
        kwargs={"worker_id": "prepare-test", "job_id": created.job_id},
        name="job-worker",
        daemon=True,
    )
    try:
        started = time.monotonic()
        worker.start()
        reached = generation_started.wait(_PREPARE_DEADLINE_SECONDS)
        elapsed = time.monotonic() - started
    finally:
        release.set()
        holder.join(5)
    worker.join(30)
    assert reached, f"準備の段階がオントロジーの lock を {elapsed:.1f} 秒以上待った"
    job = service.get_job(created.job_id, actor_user_uuid="user-1")
    assert job is not None
    assert job.status == JobStatus.DONE


@pytest.mark.parametrize(
    "case",
    [
        "empty",
        "unpublished_history",
        "published_for_profile",
        "published_for_other_profile_only",
        "draft_artifact_on_published_revision",
        "two_published_revisions",
    ],
)
def test_published_release_id_matches_markdown_state(
    case: str, store: InMemoryOntologyStore, runtime: OntologyApiRuntime
) -> None:
    """軽い確定（`published_release_id`）は、従来の `ontology_markdown_state` と同じ版を選ぶ。"""

    if case == "unpublished_history":
        _seed_profile_with_unpublished_history(store)
    elif case == "published_for_profile":
        _seed_profile_with_unpublished_history(store)
        _save_markdown(
            store,
            profile_id=_PROFILE_ID,
            revision_id="ontology_revision_global",
            artifact_type=_PUBLISHED,
        )
    elif case == "published_for_other_profile_only":
        _save_revision(store, "ontology_revision_global", OntologyRevisionStatus.PUBLISHED)
        _save_markdown(
            store,
            profile_id="other-profile",
            revision_id="ontology_revision_global",
            artifact_type=_PUBLISHED,
        )
    elif case == "draft_artifact_on_published_revision":
        _save_revision(store, "ontology_revision_global", OntologyRevisionStatus.PUBLISHED)
        _save_markdown(
            store,
            profile_id=_PROFILE_ID,
            revision_id="ontology_revision_global",
            artifact_type=_DRAFT,
        )
    elif case == "two_published_revisions":
        # 公開中の版は所有者（PROFILE_ID）ごとに 1 つ。旧共有版と業務プロファイルの版が両方公開中。
        _save_revision(
            store, "ontology_revision_shared", OntologyRevisionStatus.PUBLISHED, version=5
        )
        _save_revision(
            store,
            "ontology_revision_owned",
            OntologyRevisionStatus.PUBLISHED,
            version=2,
            profile_id=_PROFILE_ID,
        )
        _save_markdown(
            store,
            profile_id=_PROFILE_ID,
            revision_id="ontology_revision_shared",
            artifact_type=_PUBLISHED,
            updated_at="2026-09-02T00:00:00Z",
            profile_version=1,
        )
        _save_markdown(
            store,
            profile_id=_PROFILE_ID,
            revision_id="ontology_revision_owned",
            artifact_type=_PUBLISHED,
            updated_at="2026-09-01T00:00:00Z",
            profile_version=2,
        )

    state = runtime.ontology_markdown_state(_PROFILE_ID)
    expected = state.published_revision.id if state.published_revision is not None else ""
    if case == "published_for_profile":
        assert expected == "ontology_revision_global"
    if case == "two_published_revisions":
        assert expected == "ontology_revision_owned"
    assert runtime.published_release_id(_PROFILE_ID) == expected


def test_published_release_id_prefers_markdown_snapshot_head(
    store: InMemoryOntologyStore, runtime: OntologyApiRuntime
) -> None:
    """Markdown の公開（snapshot）があれば、その ID を確定する（従来の head と同じ）。"""

    workspace = MarkdownOntologyWorkspace(runtime)
    store.save_artifact(
        workspace.artifact(
            _PROFILE_ID,
            stable_ontology_id(HEAD, _PROFILE_ID),
            HEAD,
            {"snapshot_id": "ontology_markdown_snapshot_v1"},
        )
    )
    _save_revision(store, "ontology_revision_global", OntologyRevisionStatus.PUBLISHED)
    _save_markdown(
        store,
        profile_id=_PROFILE_ID,
        revision_id="ontology_revision_global",
        artifact_type=_PUBLISHED,
    )
    assert workspace.head(_PROFILE_ID)["snapshot_id"] == "ontology_markdown_snapshot_v1"
    assert runtime.published_release_id(_PROFILE_ID) == "ontology_markdown_snapshot_v1"


def test_worker_pins_release_resolved_without_graphs(
    store: InMemoryOntologyStore, service: Nl2SqlService, runtime: OntologyApiRuntime
) -> None:
    """worker が確定した公開中の版を、job と結果に残す。"""

    _save_revision(store, "ontology_revision_global", OntologyRevisionStatus.PUBLISHED)
    _save_markdown(
        store,
        profile_id=_PROFILE_ID,
        revision_id="ontology_revision_global",
        artifact_type=_PUBLISHED,
    )
    created = service.start_job(_chat_request(), actor_user_uuid="user-1")
    assert service.run_next_nl2sql_job(worker_id="prepare-test", job_id=created.job_id)
    job = service.get_job(created.job_id, actor_user_uuid="user-1")
    assert job is not None
    assert job.business_release_id == "ontology_revision_global"
    assert job.result is not None
    assert job.result.business_release_id == "ontology_revision_global"


def test_prepare_logs_each_step_with_elapsed_time(
    caplog: pytest.LogCaptureFixture, service: Nl2SqlService, runtime: OntologyApiRuntime
) -> None:
    """段階の中の処理ごとの所要時間を、job_id 付きの INFO で出す（質問の本文は出さない）。"""

    caplog.set_level(logging.INFO, logger="app.features.nl2sql.service")
    created = service.start_job(_chat_request(), actor_user_uuid="user-1")
    assert service.run_next_nl2sql_job(worker_id="prepare-test", job_id=created.job_id)

    steps = [
        record
        for record in caplog.records
        if record.getMessage() == "nl2sql_job_stage_step_finished"
    ]
    assert [
        (getattr(record, "stage", None), getattr(record, "step", None)) for record in steps
    ] == [
        ("prepare_context", "business_release"),
        ("prepare_context", "rewrite_question"),
        ("prepare_context", "allowed_objects"),
        ("prepare_context", "row_limit"),
        ("prepare_context", "ontology_context"),
        # 結果の整形の中で、オントロジーの接地確認のグラフを作る時間（チャットでは約 15 秒あった）。
        ("format_results", "ontology_graph"),
    ]
    for record in steps:
        assert record.levelno == logging.INFO
        assert getattr(record, "job_id", None) == created.job_id
        assert getattr(record, "attempt", None) == 1
        assert isinstance(getattr(record, "elapsed_ms", None), int)
    question = _chat_request().question
    assert all(question not in str(value) for record in steps for value in record.__dict__.values())
