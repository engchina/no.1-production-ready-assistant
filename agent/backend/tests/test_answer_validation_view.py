"""保存済みの警告は権限を通した写しだけで再分類する。外部モデル・保存更新なし。"""

from copy import deepcopy
from typing import Any, cast

import pytest
from fastapi import Request
from pytest import MonkeyPatch

from app.features.agent import router
from app.features.agent.answer_validation import (
    REVIEW_HEADING,
    REVIEW_INTRO,
    _section_lines,
    reason_item,
)
from app.features.agent.answer_validation_view import (
    GUIDE_HISTORY_NOTICE,
    GUIDE_UNVERIFIED_NOTICE,
    project_validation_artifacts,
)
from app.features.agent.evidence_access import withhold_artifacts, withhold_run
from app.features.agent.runtime import Artifact, RunState, RunStatus, runtime_repository


def _artifacts() -> list[Artifact]:
    unknown = reason_item("guide_check", "合成の未確認")
    real = reason_item("guide_check", "合成の順序誤り")
    result: dict[str, Any] = {
        "guide_steps": [{"step_id": "s1", "required": True, "status": "unknown"}],
        "findings": [
            {"code": "guide_step_unexplained", "step_id": "s1", "message": "合成の未確認"},
            {
                "code": "step_order",
                "step_id": "s2",
                "message": "合成の順序誤り",
                "severity": "error",
            },
        ],
    }
    _, lines = _section_lines(result, [], [], [unknown, real])
    # 本文に同じ文字があっても変更しない。
    body = "操作A、次に操作B。\n" + lines[0]
    text = body + f"\n\n**{REVIEW_HEADING}**\n\n{REVIEW_INTRO}\n" + "\n".join(lines)
    return [
        Artifact(
            id="validation",
            name="検証",
            kind="answer_validation",
            content={
                "result": result,
                "answer_warnings": [unknown, real],
                "passage_marks": [{"text": "別の段落", "state": "unverified"}],
                "notice": {"heading": REVIEW_HEADING, "headings": [REVIEW_HEADING], "items": 2},
                "withheld": {"listed": 2},
            },
        ),
        Artifact(
            id="answer",
            name="回答",
            kind="answer",
            content={"text": text, "outcome": {"value": "conditional"}},
        ),
    ]


def test_read_projection_preserves_raw_and_real_warnings_and_body() -> None:
    artifacts = _artifacts()
    before = deepcopy(artifacts)
    views = project_validation_artifacts(artifacts)
    assert artifacts == before
    validation, answer = views
    assert len(validation.content["answer_warnings"]) == 1
    assert validation.content["answer_warnings"][0]["detail"] == "合成の順序誤り"
    assert validation.content["history_diagnostics"]["answer_warnings"] == [
        artifacts[0].content["answer_warnings"][0]
    ]
    assert validation.content["result"] == artifacts[0].content["result"]
    assert validation.content["passage_marks"] == artifacts[0].content["passage_marks"]
    assert validation.content["notice"]["items"] == 1
    assert answer.content["text"].count("合成の未確認") == 1
    assert (
        answer.content["text"].split(f"\n\n**{REVIEW_HEADING}**")[0]
        == artifacts[1].content["text"].split(f"\n\n**{REVIEW_HEADING}**")[0]
    )
    assert answer.content["notice"] == GUIDE_UNVERIFIED_NOTICE
    assert answer.content["outcome"] == artifacts[1].content["outcome"]
    assert project_validation_artifacts(views) == views


@pytest.mark.parametrize("missing", ["guide_steps", "findings"])
def test_missing_structured_history_is_not_guessed(missing: str) -> None:
    artifacts = _artifacts()
    artifacts[0].content["result"].pop(missing)
    views = project_validation_artifacts(artifacts)
    assert views[0].content["answer_warnings"] == artifacts[0].content["answer_warnings"]
    assert views[1].content["text"] == artifacts[1].content["text"]
    assert views[1].content["notice"] == GUIDE_HISTORY_NOTICE


@pytest.mark.parametrize("execution", ["not_run", "error", "timed_out", "partial", "unknown"])
def test_uncompleted_explicit_execution_is_kept_as_unknown(execution: str) -> None:
    artifacts = _artifacts()
    step = artifacts[0].content["result"]["guide_steps"][0]
    step["status"] = "partial"
    step["validation"] = {"body_coverage": {"execution": execution, "status": "unknown"}}
    views = project_validation_artifacts(artifacts)
    assert len(views[0].content["answer_warnings"]) == 1
    assert views[0].content["result"]["guide_steps"][0] == step


def test_ambiguous_step_or_shared_real_warning_is_not_silently_removed() -> None:
    artifacts = _artifacts()
    result = artifacts[0].content["result"]
    result["guide_steps"].append(deepcopy(result["guide_steps"][0]))
    assert project_validation_artifacts(artifacts) == artifacts
    result["guide_steps"].pop()
    result["findings"][1]["message"] = result["findings"][0]["message"]
    assert project_validation_artifacts(artifacts) == artifacts


def test_hidden_validation_step_cannot_reclassify_and_storage_is_unchanged() -> None:
    artifacts = _artifacts()
    artifacts[0].content["result"]["guide_steps"][0]["document_id"] = "hidden"
    before = deepcopy(artifacts)
    views = withhold_artifacts(artifacts, frozenset({"hidden"}))
    assert views[0].content["result"]["guide_steps"] == []
    assert views[0].content["answer_warnings"] == before[0].content["answer_warnings"]
    assert views[1].content == before[1].content
    assert artifacts == before


def test_run_and_artifact_list_share_projection_without_changing_stored_run() -> None:
    run = RunState(
        id="run-synthetic",
        agent_id="agent-synthetic",
        goal="合成質問",
        status=RunStatus.COMPLETED,
        artifacts=_artifacts(),
    )
    before = run.model_dump()
    view = withhold_run(run, frozenset())
    assert view.artifacts == withhold_artifacts(run.artifacts, frozenset())
    assert run.model_dump() == before


@pytest.mark.asyncio
async def test_single_answer_artifact_filters_same_run_context_before_projection(
    monkeypatch: MonkeyPatch,
) -> None:
    artifacts = _artifacts()
    run = RunState(
        id="run-synthetic",
        agent_id="agent-synthetic",
        goal="合成質問",
        status=RunStatus.COMPLETED,
        artifacts=artifacts,
    )
    seen: list[str] = []

    def readable(request: Request, run_id: str) -> RunState:
        seen.append(run_id)
        return run

    async def permissions(
        request: Request, runs: list[RunState], items: list[Artifact]
    ) -> frozenset[str]:
        assert runs == [run]
        assert items == artifacts
        seen.append("evidence_permissions")
        return frozenset()

    monkeypatch.setattr(router, "_readable_run", readable)
    monkeypatch.setattr(router, "_unreadable_evidence", permissions)
    monkeypatch.setattr(runtime_repository, "get_artifact", lambda *args: artifacts[1])
    monkeypatch.setattr(runtime_repository, "list_artifacts", lambda *args: artifacts)
    response = await router.get_run_artifact(run.id, "answer", cast(Request, None))
    assert response.data is not None
    assert response.data.id == "answer"
    assert response.data.content["notice"] == GUIDE_UNVERIFIED_NOTICE
    assert seen == [run.id, "evidence_permissions"]
