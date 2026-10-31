"""生成の文脈と生成後の接地確認を、ジョブの要求の項目で分ける（#1172）。

`use_ontology_context` は「SQL の生成の prompt に公開版のオントロジーの文脈を入れる」と
「結果の整形の段階でオントロジーのグラフを読み、生成 SQL の接地を確かめる」の 2 つを兼ねていた。
チャットの画面は接地確認を表示しないため、`include_ontology_grounding=false` で接地確認だけを省く。
経路は SQL 生成と同じ `_run_job` で、未指定（`None`）は今と同じ（`use_ontology_context` に従う）。
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from test_nl2sql_job_grounding_published_only import (
    _bind_runtime,
    _chat_request,
    _publish_legacy_revision,
    _record_calls,
    _run,
)
from test_nl2sql_job_runtime import _repository, _worker

from app.features.nl2sql.models import JobCreateRequest, JobStatus
from app.features.nl2sql.ontology_store import InMemoryOntologyStore
from app.features.nl2sql.service import Nl2SqlService
from app.settings import get_settings

# 接地確認のグラフの読み込み（公開版のグラフと、その場の準備）。
_GRAPH_CALLS = (
    "published_graph_snapshot_for_job",
    "_query_ontology",
    "_sync_ontology",
    "ontology_markdown_state",
)


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> Nl2SqlService:
    # in-process の worker を起こさず、テストが worker を明示的に動かす。
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    return _worker(_repository())


@pytest.fixture
def store() -> InMemoryOntologyStore:
    return InMemoryOntologyStore()


def _without_grounding(*, include_interpretation: bool = False) -> JobCreateRequest:
    return _chat_request().model_copy(
        update={
            "include_ontology_grounding": False,
            "include_interpretation": include_interpretation,
        }
    )


def _record_generation_context(
    monkeypatch: pytest.MonkeyPatch, service: Nl2SqlService
) -> list[str | None]:
    """生成に渡したオントロジーの文脈（prompt に入る公開版の Markdown）を記録する。"""

    contexts: list[str | None] = []
    original = service._generate_selected_engine  # noqa: SLF001

    def recorder(*args: Any, **kwargs: Any) -> Any:
        contexts.append(kwargs.get("ontology_context"))
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_generate_selected_engine", recorder)
    return contexts


def test_request_defaults_follow_use_ontology_context() -> None:
    """未指定は今と同じ: 接地確認は `use_ontology_context` に従う。"""

    request = JobCreateRequest(question="売上")
    assert request.include_ontology_grounding is None
    assert request.ontology_grounding_requested is True
    off = request.model_copy(update={"use_ontology_context": False})
    assert off.ontology_grounding_requested is False
    skipped = request.model_copy(update={"include_ontology_grounding": False})
    assert skipped.ontology_grounding_requested is False
    # 接地確認は公開版の確定（use_ontology_context）が前提。文脈を使わないなら接地確認もしない。
    forced = request.model_copy(
        update={"use_ontology_context": False, "include_ontology_grounding": True}
    )
    assert forced.ontology_grounding_requested is False


def test_grounding_off_does_not_read_ontology_graph(
    monkeypatch: pytest.MonkeyPatch, service: Nl2SqlService, store: InMemoryOntologyStore
) -> None:
    """接地確認を求めないジョブは、公開版があっても整形の段階でグラフを読まない。"""

    release_id = _publish_legacy_revision(service, store)
    runtime = _bind_runtime(monkeypatch, service, store)
    calls = _record_calls(monkeypatch, runtime, _GRAPH_CALLS)

    job = _run(service, _without_grounding())

    assert job.status == JobStatus.DONE
    # 公開版の確定（生成の文脈の前提）は今までどおり行う。
    assert job.business_release_id == release_id
    assert calls == [], f"接地確認を求めていないのにグラフを読んだ: {calls}"
    # 処理手順も求めていないので、解釈の artifact 自体を作らない。
    assert job.result is not None
    assert job.result.interpretation is None


def test_grounding_off_keeps_published_ontology_in_generation_prompt(
    monkeypatch: pytest.MonkeyPatch, service: Nl2SqlService, store: InMemoryOntologyStore
) -> None:
    """接地確認を省いても、生成の prompt には公開版のオントロジーの文脈が入る（未指定と同じ）。"""

    _publish_legacy_revision(service, store)
    _bind_runtime(monkeypatch, service, store)
    contexts = _record_generation_context(monkeypatch, service)

    _run(service, _chat_request())
    _run(service, _without_grounding())

    assert len(contexts) == 2
    default_context, chat_context = contexts
    assert default_context
    assert chat_context == default_context


def test_grounding_off_with_interpretation_omits_grounding_part(
    monkeypatch: pytest.MonkeyPatch, service: Nl2SqlService, store: InMemoryOntologyStore
) -> None:
    """処理手順を求めたときは artifact を作り、接地確認の部分だけを作らない。"""

    _publish_legacy_revision(service, store)
    runtime = _bind_runtime(monkeypatch, service, store)
    calls = _record_calls(monkeypatch, runtime, _GRAPH_CALLS)

    job = _run(service, _without_grounding(include_interpretation=True))

    assert calls == []
    interpretation = job.result.interpretation
    assert interpretation is not None
    assert interpretation.ontology_grounding_enabled is False
    assert interpretation.ontology_graph is None
    assert interpretation.ontology_grounding_skip_reason == ""


def test_grounding_off_logs_skipped_step(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    service: Nl2SqlService,
    store: InMemoryOntologyStore,
) -> None:
    """段階のログで、接地確認を求められなかったため飛ばしたことが分かる。"""

    _publish_legacy_revision(service, store)
    _bind_runtime(monkeypatch, service, store)
    caplog.set_level(logging.INFO, logger="app.features.nl2sql.service")

    job = _run(service, _without_grounding())

    [record] = [
        record
        for record in caplog.records
        if record.getMessage() == "nl2sql_job_stage_step_finished"
        and getattr(record, "step", None) == "ontology_graph"
    ]
    assert getattr(record, "job_id", None) == job.job_id
    assert getattr(record, "stage", None) == "format_results"
    assert getattr(record, "skipped", None) is True
    assert getattr(record, "skip_reason", None) == "grounding_not_requested"


def test_unspecified_grounding_keeps_current_behavior(
    monkeypatch: pytest.MonkeyPatch, service: Nl2SqlService, store: InMemoryOntologyStore
) -> None:
    """未指定（SQL 生成の画面・MCP）は今と同じく、公開版のグラフで接地確認する。"""

    release_id = _publish_legacy_revision(service, store)
    runtime = _bind_runtime(monkeypatch, service, store)
    calls = _record_calls(monkeypatch, runtime, ("published_graph_snapshot_for_job",))

    job = _run(service, _chat_request())

    assert calls == ["published_graph_snapshot_for_job"]
    interpretation = job.result.interpretation
    assert interpretation is not None
    assert interpretation.ontology_grounding_enabled is True
    assert interpretation.ontology_graph is not None
    assert interpretation.ontology_graph.revision_id == release_id
