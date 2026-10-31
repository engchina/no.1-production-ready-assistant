"""SQL の生成のジョブの接地確認は、公開版のオントロジーだけを使う（#1168）。

オントロジーの使い方は「公開版があればその公開版だけを使い、無ければ使わない。ジョブの中で
その場で準備（構築・同期）しない」。SQL の生成の prompt の文脈（準備の段階）はこの規則どおり
だが、結果の整形の段階の接地確認は、公開版の無い業務プロファイルで `_query_ontology` →
`_sync_ontology`（DB のカタログ全体からグラフを同期・構築）に進み、共有の DB で 14.8〜103 秒
かかっていた。さらに runtime の lock を持ったまま DB を読むため、lock を持つ別の処理を待った。
チャットと SQL 生成の画面は同じ `_run_job` を通る。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import pytest
from test_nl2sql_job_prepare_latency import _PUBLISHED, _save_markdown
from test_nl2sql_job_runtime import _PROFILE_ID, _repository, _request, _worker

from app.features.nl2sql import ontology_router
from app.features.nl2sql.models import AllowedObjects, JobCreateRequest, JobStatus
from app.features.nl2sql.ontology_catalog import SchemaOntology
from app.features.nl2sql.ontology_markdown_workspace import (
    HEAD,
    SNAPSHOT,
    MarkdownOntologyWorkspace,
)
from app.features.nl2sql.ontology_models import OntologyRevisionStatus
from app.features.nl2sql.ontology_router import OntologyApiRuntime
from app.features.nl2sql.ontology_service import OntologyNotFoundError
from app.features.nl2sql.ontology_store import InMemoryOntologyStore, stable_ontology_id
from app.features.nl2sql.service import Nl2SqlService
from app.settings import get_settings

_FORMAT_DEADLINE_SECONDS = 5.0
_SNAPSHOT_ID = "ontology_markdown_snapshot_grounding_v1"
# 公開版の無い業務プロファイルの接地確認で、呼んではならない「その場の準備」とグラフの読み込み。
_PREPARATION_CALLS = ("_query_ontology", "_sync_ontology", "ontology_markdown_state")


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> Nl2SqlService:
    # in-process の worker を起こさず、テストが worker を明示的に動かす。
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    return _worker(_repository())


@pytest.fixture
def store() -> InMemoryOntologyStore:
    return InMemoryOntologyStore()


def _bind_runtime(
    monkeypatch: pytest.MonkeyPatch, service: Nl2SqlService, store: InMemoryOntologyStore
) -> OntologyApiRuntime:
    """ジョブを動かす service に結び付いた runtime（本番と同じ legacy_service is self）。"""

    bound = OntologyApiRuntime(legacy_service=service, store=store)
    monkeypatch.setattr(ontology_router, "ontology_runtime", bound)
    return bound


def _chat_request() -> JobCreateRequest:
    return _request().model_copy(update={"generation_only": True, "use_ontology_context": True})


def _record_calls(
    monkeypatch: pytest.MonkeyPatch, runtime: OntologyApiRuntime, names: tuple[str, ...]
) -> list[str]:
    calls: list[str] = []
    for name in names:
        original = getattr(runtime, name)

        def recorder(
            *args: Any, _name: str = name, _original: Any = original, **kwargs: Any
        ) -> Any:
            calls.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(runtime, name, recorder)
    return calls


def _built_graph(service: Nl2SqlService, store: InMemoryOntologyStore) -> SchemaOntology:
    """テストの準備として、カタログからグラフを作って公開し、store に保存する（ジョブの外）。"""

    builder = OntologyApiRuntime(legacy_service=service, store=store)
    with builder._lock:  # noqa: SLF001 - 準備の経路（オントロジーの画面の同期）の代わり
        return builder._query_ontology()  # noqa: SLF001


def _publish_legacy_revision(service: Nl2SqlService, store: InMemoryOntologyStore) -> str:
    """旧方式: 公開中の版と、この業務プロファイルの公開の Markdown。"""

    ontology = _built_graph(service, store)
    assert ontology.revision.status == OntologyRevisionStatus.PUBLISHED
    _save_markdown(
        store, profile_id=_PROFILE_ID, revision_id=ontology.revision.id, artifact_type=_PUBLISHED
    )
    return ontology.revision.id


def _publish_markdown_snapshot(
    monkeypatch: pytest.MonkeyPatch, service: Nl2SqlService, store: InMemoryOntologyStore
) -> str:
    """Markdown の公開: snapshot（グラフを含む）と、それを指す head。版の行は作らない。

    このテストの snapshot は接地確認のグラフだけを持つ（業務定義・範囲の hash を持たない）ため、
    準備の段階の prompt の文脈（`published_context`、#1155 のテストが検証する）は使わない。
    """

    monkeypatch.setattr(service, "_job_published_ontology_markdown", lambda **_kwargs: None)

    built = _built_graph(service, store)
    graph = built.model_copy(
        update={"revision": built.revision.model_copy(update={"id": _SNAPSHOT_ID})}, deep=True
    )
    # snapshot だけで公開版が決まるように、カタログから作った版の行は消す（読まれたら失敗する）。
    for collection in ("revisions", "nodes", "edges"):
        store.delete_documents(collection, {"revision_id": built.revision.id})
    workspace = MarkdownOntologyWorkspace(OntologyApiRuntime(legacy_service=service, store=store))
    store.save_artifact(
        workspace.artifact(
            _PROFILE_ID,
            _SNAPSHOT_ID,
            SNAPSHOT,
            {"id": _SNAPSHOT_ID, "markdown": "# 注文", "graph": graph.model_dump(mode="json")},
        )
    )
    store.save_artifact(
        workspace.artifact(
            _PROFILE_ID,
            stable_ontology_id(HEAD, _PROFILE_ID),
            HEAD,
            {"snapshot_id": _SNAPSHOT_ID},
        )
    )
    return _SNAPSHOT_ID


def _run(service: Nl2SqlService, request: JobCreateRequest) -> Any:
    created = service.start_job(request, actor_user_uuid="user-1")
    assert service.run_next_nl2sql_job(worker_id="grounding-test", job_id=created.job_id)
    job = service.get_job(created.job_id, actor_user_uuid="user-1")
    assert job is not None
    return job


def test_no_published_ontology_skips_grounding_without_preparing(
    monkeypatch: pytest.MonkeyPatch, service: Nl2SqlService, store: InMemoryOntologyStore
) -> None:
    """公開版が無い業務プロファイルでは、接地確認のグラフをその場で同期・構築しない。"""

    runtime = _bind_runtime(monkeypatch, service, store)
    calls = _record_calls(
        monkeypatch, runtime, (*_PREPARATION_CALLS, "ontology_revision", "_load_ontology_revision")
    )

    job = _run(service, _chat_request())

    assert job.status == JobStatus.DONE
    assert job.business_release_id == ""
    assert calls == [], f"公開版が無いのにオントロジーを準備・読み込みした: {calls}"
    interpretation = job.result.interpretation
    assert interpretation is not None
    assert interpretation.ontology_grounding_enabled is True
    assert interpretation.ontology_graph is None
    assert interpretation.ontology_grounding_skip_reason == "no_published_ontology"
    # 失敗ではないので、失敗の警告にしない。
    assert not any("失敗" in warning for warning in interpretation.warnings)


def test_no_published_ontology_logs_skipped_grounding_step(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    service: Nl2SqlService,
    store: InMemoryOntologyStore,
) -> None:
    """段階のログで、公開版が無いため接地確認を飛ばしたことが分かる。"""

    _bind_runtime(monkeypatch, service, store)
    caplog.set_level(logging.INFO, logger="app.features.nl2sql.service")

    job = _run(service, _chat_request())

    [record] = [
        record
        for record in caplog.records
        if record.getMessage() == "nl2sql_job_stage_step_finished"
        and getattr(record, "step", None) == "ontology_graph"
    ]
    assert getattr(record, "job_id", None) == job.job_id
    assert getattr(record, "stage", None) == "format_results"
    assert getattr(record, "skipped", None) is True
    assert getattr(record, "skip_reason", None) == "no_published_ontology"


def test_ontology_off_does_not_mark_grounding_as_skipped(
    monkeypatch: pytest.MonkeyPatch, service: Nl2SqlService, store: InMemoryOntologyStore
) -> None:
    """「公開版オントロジーを使う」が OFF のときは、従来どおり接地確認そのものを無効にする。"""

    runtime = _bind_runtime(monkeypatch, service, store)
    calls = _record_calls(monkeypatch, runtime, _PREPARATION_CALLS)
    request = _request().model_copy(
        update={"use_ontology_context": False, "include_interpretation": True}
    )

    job = _run(service, request)

    assert calls == []
    interpretation = job.result.interpretation
    assert interpretation is not None
    assert interpretation.ontology_grounding_enabled is False
    assert interpretation.ontology_grounding_skip_reason == ""


@pytest.mark.parametrize("published", ["none", "legacy_revision", "markdown_snapshot"])
def test_format_does_not_wait_for_ontology_runtime_lock(
    published: str,
    monkeypatch: pytest.MonkeyPatch,
    service: Nl2SqlService,
    store: InMemoryOntologyStore,
) -> None:
    """別の処理がオントロジーの lock を持ち続けても、結果の整形は待たずに終わる。"""

    if published == "legacy_revision":
        _publish_legacy_revision(service, store)
    elif published == "markdown_snapshot":
        _publish_markdown_snapshot(monkeypatch, service, store)
    runtime = _bind_runtime(monkeypatch, service, store)
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
        kwargs={"worker_id": "grounding-test", "job_id": created.job_id},
        name="job-worker",
        daemon=True,
    )
    try:
        started = time.monotonic()
        worker.start()
        worker.join(_FORMAT_DEADLINE_SECONDS)
        finished = not worker.is_alive()
        elapsed = time.monotonic() - started
    finally:
        release.set()
        holder.join(5)
    worker.join(30)
    assert finished, f"ジョブがオントロジーの lock を {elapsed:.1f} 秒以上待った"
    job = service.get_job(created.job_id, actor_user_uuid="user-1")
    assert job is not None
    assert job.status == JobStatus.DONE
    assert job.result is not None
    interpretation = job.result.interpretation
    assert interpretation is not None
    if published == "none":
        assert interpretation.ontology_grounding_skip_reason == "no_published_ontology"
    else:
        assert interpretation.ontology_graph is not None
        assert interpretation.ontology_graph.nodes


@pytest.mark.parametrize("published", ["legacy_revision", "markdown_snapshot"])
def test_published_release_is_the_only_grounding_graph(
    published: str,
    monkeypatch: pytest.MonkeyPatch,
    service: Nl2SqlService,
    store: InMemoryOntologyStore,
) -> None:
    """公開版があれば、その公開版のグラフだけで接地確認する（同期・構築しない）。"""

    release_id = (
        _publish_legacy_revision(service, store)
        if published == "legacy_revision"
        else _publish_markdown_snapshot(monkeypatch, service, store)
    )
    # 公開版の読み込みが runtime のキャッシュに頼らないよう、新しい runtime で動かす。
    runtime = _bind_runtime(monkeypatch, service, store)
    calls = _record_calls(monkeypatch, runtime, _PREPARATION_CALLS)

    job = _run(service, _chat_request())

    assert job.status == JobStatus.DONE
    assert job.business_release_id == release_id
    assert calls == [], f"公開版があるのにオントロジーを同期・準備した: {calls}"
    interpretation = job.result.interpretation
    assert interpretation is not None
    assert interpretation.ontology_grounding_skip_reason == ""
    graph = interpretation.ontology_graph
    assert graph is not None
    assert graph.revision_id == release_id
    technical_names = {str(node.get("technical_name")) for node in graph.nodes}
    assert any(name.startswith("APP.ORDERS") for name in technical_names)

    # 版は不変なので、2 回目のジョブは公開版のグラフを読み直さない。
    reads = _record_calls(monkeypatch, runtime, ("_read_published_release_graph",))
    second = _run(service, _chat_request())
    assert second.result.interpretation.ontology_graph is not None
    assert reads == []


def test_snapshot_of_another_profile_is_not_used_for_grounding(
    monkeypatch: pytest.MonkeyPatch, service: Nl2SqlService, store: InMemoryOntologyStore
) -> None:
    """別の業務プロファイルの snapshot の ID では、キャッシュの有無にかかわらず接地確認しない。"""

    _publish_markdown_snapshot(monkeypatch, service, store)
    runtime = _bind_runtime(monkeypatch, service, store)
    profile = service.get_profile(_PROFILE_ID)
    other = profile.model_copy(update={"id": "other-profile"})
    allowed = AllowedObjects()

    with pytest.raises(OntologyNotFoundError):
        runtime.published_graph_snapshot_for_job(
            profile=other, allowed=allowed, release_id=_SNAPSHOT_ID
        )
    snapshot = runtime.published_graph_snapshot_for_job(
        profile=profile, allowed=allowed, release_id=_SNAPSHOT_ID
    )
    assert snapshot["revision_id"] == _SNAPSHOT_ID
    with pytest.raises(OntologyNotFoundError):
        runtime.published_graph_snapshot_for_job(
            profile=other, allowed=allowed, release_id=_SNAPSHOT_ID
        )


def test_empty_release_is_rejected_without_preparing(
    monkeypatch: pytest.MonkeyPatch, service: Nl2SqlService, store: InMemoryOntologyStore
) -> None:
    """公開版の ID が空なら、その場でグラフを作らずに拒む。"""

    runtime = _bind_runtime(monkeypatch, service, store)
    calls = _record_calls(monkeypatch, runtime, _PREPARATION_CALLS)

    with pytest.raises(OntologyNotFoundError):
        runtime.published_graph_snapshot_for_job(
            profile=service.get_profile(_PROFILE_ID), allowed=AllowedObjects(), release_id=""
        )
    assert calls == []
