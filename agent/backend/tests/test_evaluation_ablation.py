"""実行補助の除去実験（#69）: 評価の Run だけで補助を外す・比較の表・判定・門から外す。

業務 Agent の回答と判定のモデルは `ScriptedModel`、RAG は契約どおりの fake（`mcp_support`）で、
外部へは接続しない。
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

import anyio
import pytest
from agents.testing import assistant_message
from mcp_support import fake_product_mcp
from pytest import MonkeyPatch
from security_support import client
from test_evaluation import _judgement, _script, no_background  # noqa: F401 - fixture

from app.features.agent import evaluation
from app.features.agent.evaluation import (
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationJob,
    evaluation_set_store,
    evaluation_store,
    run_evaluation_job,
)
from app.features.agent.evaluation_ablation import ablation_report
from app.features.agent.evaluation_aids import DISABLED_AIDS_METADATA_KEY, aid_enabled
from app.features.agent.runtime import (
    EVALUATION_DRY_RUN_KEY,
    AgentProfile,
    RunCreateRequest,
    reserved_run_metadata_keys,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry

AGENT_ID = "test69-agent"
SKILL_ID = "test69-skill"
USER_UUID = "11111111-2222-3333-4444-555555550069"
CASE = EvaluationCase(id="c1", question="制度 A の上限は？", expected="上限は 5 万円")


@pytest.fixture(autouse=True)
def _sequential(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(evaluation, "evaluation_concurrency", lambda: 1)


@pytest.fixture
def agent(monkeypatch: MonkeyPatch) -> Iterator[None]:
    fake_product_mcp(monkeypatch)
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name="資料で答える",
            instructions="資料を検索してから答える。",
            mcp_requirements=[SkillMcpRequirement(server_id="rag", tool_names=["rag_search"])],
        )
    )
    runtime_repository.create_agent(
        AgentProfile(id=AGENT_ID, name="除去実験の業務 Agent", skill_ids=[SKILL_ID])
    )
    evaluation_store.clear()
    evaluation_set_store.clear()
    try:
        yield
    finally:
        evaluation_store.clear()
        evaluation_set_store.clear()
        repository: Any = runtime_repository
        with repository._lock:  # noqa: SLF001 - テストの後始末
            for run_id in [
                run_id for run_id, run in repository._runs.items() if run.agent_id == AGENT_ID
            ]:
                repository._runs.pop(run_id)
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent(AGENT_ID)
        with contextlib.suppress(KeyError, ValueError):
            skill_registry.remove(SKILL_ID)


def _job(*disabled: str, trials: int = 1) -> EvaluationJob:
    return evaluation_store.create(
        EvaluationJob(
            agent_id=AGENT_ID,
            agent_name="除去実験の業務 Agent",
            set_id="evset_69",
            set_name="除去実験",
            created_by_user_uuid=USER_UUID,
            trials=trials,
            judge_fingerprint="judge-a",
            disabled_aids=list(disabled),
            results=[EvaluationCaseResult(case=CASE, trial=t) for t in range(1, trials + 1)],
        )
    )


def _run(job: EvaluationJob) -> EvaluationJob:
    anyio.run(run_evaluation_job, job.id)
    return evaluation_store.get(job.id)


def test_disabled_aid_changes_the_code_path_only_in_evaluation_runs(
    monkeypatch: MonkeyPatch, agent: None
) -> None:
    """全構成では検索せずに答えると差し戻して拒答し、補助を外した評価では差し戻さない。"""
    del agent
    answer = "制度 A の上限は 5 万円です。"
    model = _script(
        monkeypatch,
        # 全構成: 検索せずに答える → 差し戻し → また検索せずに答える → 拒答の文に置き換える。
        [assistant_message(answer)],
        [assistant_message(answer)],
        _judgement("incorrect", 0.0, "拒答しています。"),
        # tool_grounding を外した評価: 差し戻さずにそのまま判定する。
        [assistant_message(answer)],
        _judgement("correct", 1.0, "要点を満たしています。"),
    )

    full = _run(_job())
    ablated = _run(_job("tool_grounding"))

    assert model.remaining_steps == 0
    full_run = runtime_repository.get_run(str(full.results[0].run_id))
    ablated_run = runtime_repository.get_run(str(ablated.results[0].run_id))
    assert DISABLED_AIDS_METADATA_KEY not in full_run.metadata
    assert ablated_run.metadata[DISABLED_AIDS_METADATA_KEY] == ["tool_grounding"]
    assert aid_enabled(full_run.id, "tool_grounding")
    assert not aid_enabled(ablated_run.id, "tool_grounding")

    report = ablation_report(full, [ablated], runtime_repository.get_run)

    [row] = report["rows"]
    assert row["name"] == "no-tool_grounding"
    # 外した補助のコード経路は、全構成では動き、外した評価では動かなかった。
    assert row["observed"]["tool_grounding"]["base"] >= 1
    assert row["observed"]["tool_grounding"]["target"] == 0
    assert row["improved_questions"] == [CASE.question]
    # 1 ケースでは区間が 0 をまたぐ: 不要の証明にしない。手間の差を添える。
    assert row["decision"] == "undetermined"
    assert "不要の証明ではない" in row["reason"] and "モデルの呼び出し" in row["reason"]
    assert report["base"]["metrics"]["model_calls"]["total"] == 2
    assert row["metrics"]["model_calls"]["total"] == 1
    assert row["metrics"]["latency_ms"]["p50"] is not None


def test_aid_metadata_is_ignored_outside_evaluation_runs(agent: None) -> None:
    """評価の Run（dry-run）でなければ、補助を外す印を読まない（利用者の Run で外せない）。"""
    del agent

    def created(metadata: dict[str, Any]) -> str:
        return runtime_repository.create_builtin_run(
            RunCreateRequest(goal="x", agent_id=AGENT_ID, metadata=metadata),
            created_by_user_uuid=USER_UUID,
        ).id

    marker = {DISABLED_AIDS_METADATA_KEY: ["tool_grounding"]}
    assert aid_enabled(created(marker), "tool_grounding")
    assert not aid_enabled(created({**marker, EVALUATION_DRY_RUN_KEY: True}), "tool_grounding")
    # 予約の key なので、利用者の Run の作成の要求（API）では断られる。
    assert reserved_run_metadata_keys(marker) == [DISABLED_AIDS_METADATA_KEY]


def _completed(*disabled: str, correct: bool = True, trials: int = 1) -> EvaluationJob:
    job = _job(*disabled, trials=trials)
    results = [
        item.model_copy(
            update={
                "status": evaluation.CaseStatus.JUDGED,
                "judgement": evaluation.EvaluationJudgement(
                    verdict="correct" if correct else "incorrect",
                    score=1.0 if correct else 0.0,
                    summary="判定",
                    missing_points=[],
                ),
                "duration_ms": 100,
            }
        )
        for item in job.results
    ]
    stored = job.model_copy(update={"results": results, "status": evaluation.JobStatus.COMPLETED})
    evaluation_store.restore(stored)
    return stored


def test_not_applicable_and_not_comparable(agent: None) -> None:
    del agent
    full = _completed()
    # 基準で動いていない補助（Run の記録が無い＝縮小・検証・差し戻しが 0 回）。
    unused = _completed("context_compaction")
    other_trials = _completed("final_validation", trials=2)
    again = _completed()

    report = ablation_report(full, [unused, other_trials, again], lambda _run_id: None)

    rows = {row["name"]: row for row in report["rows"]}
    assert rows["no-context_compaction"]["decision"] == "not_applicable"
    assert rows["no-final_validation"]["decision"] == "not_comparable"
    assert "試行の回数が違う" in rows["no-final_validation"]["reason"]
    # 全構成をもう一度流した評価は、揺れ・順序の偏りの目安として並べる。
    assert rows["full-again"]["decision"] == "baseline_repeat"
    assert report["order"][0] == full.id


@pytest.mark.usefixtures("agent", "no_background")
def test_api_starts_ablation_and_returns_the_table(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr("app.features.agent.router.judge_fingerprint", lambda: "judge-a")
    body = {
        "agent_id": AGENT_ID,
        "name": "除去実験",
        "cases": [{"id": "c1", "question": CASE.question, "expected": CASE.expected}],
    }
    set_id = client.post("/api/evaluation-sets", json=body).json()["data"]["id"]
    full = _completed()
    ablated = _completed("final_validation", correct=False)
    table = client.get(
        "/api/evaluations/ablation",
        params={"base_job_id": full.id, "target_job_id": [ablated.id]},
    )
    assert table.status_code == 200, table.text
    [row] = table.json()["data"]["rows"]
    assert row["regressed_questions"] == [CASE.question]

    # 外せない補助（権限・承認など）は受け付けない。
    refused = client.post(
        "/api/evaluations", json={"set_id": set_id, "disabled_aids": ["approval"]}
    )
    assert refused.status_code == 422
    started = client.post(
        "/api/evaluations",
        json={
            "set_id": set_id,
            "agent_version": "draft",
            "disabled_aids": ["tool_grounding", "final_validation", "tool_grounding"],
        },
    )
    assert started.status_code == 202, started.text
    assert started.json()["data"]["disabled_aids"] == ["final_validation", "tool_grounding"]
    same = client.get(
        "/api/evaluations/ablation", params={"base_job_id": full.id, "target_job_id": [full.id]}
    )
    assert same.status_code == 422


def test_ablation_jobs_are_not_the_previous_result(agent: None) -> None:
    """補助を外した評価は「前回」にも公開の門の基準にもしない（公開する構成の評価ではない）。"""
    del agent
    ablated = _completed("final_validation")
    later = _completed()

    assert evaluation_store.with_previous(later).previous_job_id is None
    assert evaluation_store.with_previous(_completed()).previous_job_id == later.id
    assert ablated.disabled_aids == ["final_validation"]
