"""業務 Agent の品質評価（#776）の決定論テスト。

業務 Agent の回答と判定のモデルは、どちらも SDK の `ScriptedModel`（台本どおりに応答する）に
差し替え、モデルの API へは接続しない。ケースは 1 件ずつ「回答 → 判定」の順にモデルを呼ぶ。
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
from agents.testing import ScriptedModel, assistant_message, function_call
from pytest import MonkeyPatch
from security_support import ProductionAuth, client, enable_production_auth, login

from app.features.agent import ai_calls, builtin_runtime, evaluation
from app.features.agent.ai_calls import retry_model_target
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.control_plane_store import (
    FileItemStore,
    restore_control_plane,
    set_control_plane_store,
)
from app.features.agent.evaluation import (
    EVALUATION_JOBS_RETENTION_DAYS,
    CaseStatus,
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationJob,
    JobStatus,
    JudgeVerdict,
    cases_fingerprint,
    evaluation_set_store,
    evaluation_store,
    judge_output_meaning,
    run_evaluation_job,
)
from app.features.agent.evaluation_excel import parse_cases_xlsx
from app.features.agent.output_fields import (
    RECORD_OUTPUT_TOOL,
    AgentOutputField,
    OutputFieldResult,
    compare_output,
    output_content,
    output_value_matches,
)
from app.features.agent.runtime import (
    EVALUATION_DRY_RUN_MESSAGE,
    AgentProfile,
    AgentProfilePatch,
    Artifact,
    RunCreateRequest,
    RunStatus,
    run_cancel_record,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.tools import ToolDefinition, ToolPermissionLevel, tool_registry
from app.security.dependencies import local_debug_principal
from app.security.permissions import ADMIN, MENU_EVALUATION, MENU_RUNS
from app.security.service import set_security_service

AGENT_ID = "eval-776-agent"
SKILL_ID = "eval-776-skill"
WRITE = "eval776_register"
USER_UUID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _sequential_cases(monkeypatch: MonkeyPatch) -> None:
    """台本のモデルは呼ばれた順に応答するため、ケースを 1 件ずつ実行する（並行は別のテスト）。"""
    monkeypatch.setattr(evaluation, "evaluation_concurrency", lambda: 1)


@pytest.fixture
def agent(tmp_path: Path) -> Iterator[None]:
    set_control_plane_store(FileItemStore(tmp_path / "items.json"))
    tool_registry.register(
        ToolDefinition(
            name=WRITE,
            description="評価のテスト用の登録ツール",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            output_schema={"type": "object"},
            permission_level=ToolPermissionLevel.WRITE,
            side_effects=True,
        ),
        lambda _arguments, _context: {},
    )
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name="評価のテストの Skill",
            instructions="登録は eval776_register で行う。",
            mcp_requirements=[SkillMcpRequirement(server_id="control-plane", tool_names=[WRITE])],
        )
    )
    runtime_repository.create_agent(
        AgentProfile(
            id=AGENT_ID,
            name="経理の Agent",
            instructions="経理の質問に答える。",
            skill_ids=[SKILL_ID],
        )
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
                run = repository._runs.pop(run_id)
                for approval in run.approvals:
                    repository._approvals.pop(approval.id, None)
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent(AGENT_ID)
        with contextlib.suppress(KeyError, ValueError):
            skill_registry.remove(SKILL_ID)
        tool_registry._definitions.pop(WRITE, None)  # noqa: SLF001 - テスト用の登録を戻す
        tool_registry._handlers.pop(WRITE, None)  # noqa: SLF001
        set_control_plane_store(None)


def _script(monkeypatch: MonkeyPatch, *steps: Any) -> ScriptedModel:
    model = ScriptedModel(list(steps))
    monkeypatch.setattr(
        builtin_runtime,
        "resolve_model_target",
        lambda model_id="": ModelTarget(
            model_id=model_id or "judge-model",
            endpoint="https://llm.example",
            project_id="",
            api_key="k",
        ),
    )
    monkeypatch.setattr(builtin_runtime, "model_factory", lambda _target: model)
    return model


def _judgement(verdict: str, score: float, summary: str, missing: list[str] | None = None) -> Any:
    return [
        assistant_message(
            json.dumps(
                {
                    "verdict": verdict,
                    "score": score,
                    "summary": summary,
                    "missing_points": missing or [],
                },
                ensure_ascii=False,
            )
        )
    ]


def _job(*cases: EvaluationCase, set_id: str = "evset_test") -> EvaluationJob:
    return evaluation_store.create(
        EvaluationJob(
            agent_id=AGENT_ID,
            agent_name="経理の Agent",
            set_id=set_id,
            set_name="経理の評価",
            created_by_user_uuid=USER_UUID,
            results=[EvaluationCaseResult(case=case) for case in cases],
        )
    )


def _case(case_id: str, question: str, expected: str, tools: list[str] | None = None) -> Any:
    return EvaluationCase(
        id=case_id, question=question, expected=expected, expected_tools=tools or []
    )


def test_cases_are_run_as_agent_runs_and_judged(monkeypatch: MonkeyPatch, agent: None) -> None:
    del agent
    model = _script(
        monkeypatch,
        [assistant_message("今月の売上は 300 万円です。")],
        _judgement("correct", 1.0, "要点を満たしています。"),
        [assistant_message("分かりません。")],
        _judgement("incorrect", 0.2, "件数が答えられていません。", ["契約件数は 12 件"]),
        [assistant_message("経費は 50 万円です。")],
        # 判定のモデルが schema に合わない出力を返したケース。
        [assistant_message("判定できません")],
    )
    job = _job(
        _case("c1", "今月の売上は？", "300 万円"),
        _case("c2", "今月の契約件数は？", "12 件"),
        _case("c3", "今月の経費は？", "50 万円"),
    )

    anyio.run(run_evaluation_job, job.id)

    finished = evaluation_store.get(job.id)
    assert finished.status == JobStatus.COMPLETED
    first, second, third = finished.results
    assert first.status == CaseStatus.JUDGED
    assert first.judgement is not None and first.judgement.verdict == JudgeVerdict.CORRECT
    assert second.judgement is not None and second.judgement.missing_points == ["契約件数は 12 件"]
    assert third.status == CaseStatus.JUDGE_FAILED
    assert third.error and "判定のモデルの呼び出しに失敗しました" in third.error
    # 出力の上限で止まったのではないので、再試行しない（#1845）。
    assert (first.judge_attempts, third.judge_attempts) == (1, 1)
    summary = finished.summary
    assert (summary.total, summary.correct, summary.incorrect, summary.errors) == (3, 1, 1, 1)
    # 判定できなかったケースは合格率の分母から除く（回答の誤りと区別する。#1845）。
    assert summary.unjudged == 1
    assert summary.pass_rate == pytest.approx(1 / 2)
    assert summary.average_score == pytest.approx(0.6)
    # 期待するツールを指定していないケースは、ツールの選択を判定しない。
    assert (summary.tool_cases, summary.tool_accuracy) == (0, None)

    run = runtime_repository.get_run(str(first.run_id))
    assert run.created_by_user_uuid == USER_UUID
    assert run.metadata["evaluation_job_id"] == job.id
    assert run.metadata["evaluation_dry_run"] is True
    judge_input = model.calls[1].input
    assert isinstance(judge_input, list)
    prompt = str(judge_input[0]["content"])
    assert "## 期待する回答の要点\n300 万円" in prompt
    # 実際の回答は区切りの tag で囲んだデータとして渡す（#92）。
    assert (
        "## 実際の回答\n<evaluated_answer>\n今月の売上は 300 万円です。\n</evaluated_answer>"
    ) in prompt


def test_tools_needing_approval_are_not_run_and_tool_selection_is_judged(
    monkeypatch: MonkeyPatch, agent: None
) -> None:
    """評価の Run は承認が要るツールを実行せず（dry-run）、回答まで続ける（#776）。"""
    del agent
    executed: list[Any] = []

    def record(arguments: Any, _context: Any) -> dict[str, Any]:
        executed.append(arguments)
        return {}

    tool_registry._handlers[WRITE] = record  # noqa: SLF001 - 実行されたかを数える
    model = _script(
        monkeypatch,
        [function_call(WRITE, {"query": "登録"}, call_id="call-776")],
        [assistant_message("登録の内容を確認しました。実際の登録は承認の後に行われます。")],
        [assistant_message("登録しませんでした。")],
        _judgement("incorrect", 0.1, "ツールを使っていません。"),
    )
    job = _job(
        _case("with-tool", "取引先を登録して", "登録の手順", [WRITE]),
        _case("without-tool", "取引先を登録して", "登録の手順", ["rag_search"]),
    )

    anyio.run(run_evaluation_job, job.id)

    first, second = evaluation_store.get(job.id).results
    assert first.status == CaseStatus.APPROVAL_BLOCKED
    assert first.judgement is None
    assert first.blocked_tool_calls == [WRITE]
    assert first.executed_tool_calls == []
    assert first.tool_calls == [WRITE]
    assert first.tool_selection_correct is True
    assert (second.tool_calls, second.tool_selection_correct) == ([], False)
    # 承認が要るツールは実行していない。step には「評価中のため実行していない」を残す。
    assert executed == []
    run = runtime_repository.get_run(str(first.run_id))
    assert run.status == RunStatus.COMPLETED
    [step] = run.steps
    assert (step.status, step.tool_result and step.tool_result.error_code) == (
        "cancelled",
        "evaluation.dry_run",
    )
    # モデルには実行していないことを伝えて、回答を完成させる。
    assert EVALUATION_DRY_RUN_MESSAGE in str(model.calls[1].input)
    summary = evaluation_store.get(job.id).summary
    assert (summary.approval_blocked, summary.judged_cases, summary.pass_rate) == (1, 1, 0)
    assert (summary.tool_cases, summary.tool_correct, summary.tool_accuracy) == (2, 1, 0.5)


def test_failed_runs_are_not_judged(monkeypatch: MonkeyPatch, agent: None) -> None:
    del agent
    _script(monkeypatch, RuntimeError("model down"))
    job = _job(_case("c1", "今月の売上は？", "300 万円"))

    anyio.run(run_evaluation_job, job.id)

    [failed] = evaluation_store.get(job.id).results
    assert failed.status == CaseStatus.RUN_FAILED
    assert failed.error and "モデルの呼び出しに失敗しました" in failed.error
    summary = evaluation_store.get(job.id).summary
    assert (summary.errors, summary.pass_rate, summary.average_score) == (1, 0.0, None)


def test_cancelled_job_does_not_run_remaining_cases(monkeypatch: MonkeyPatch, agent: None) -> None:
    del agent
    model = _script(monkeypatch)
    job = _job(_case("a", "a", "b"), _case("c", "c", "d"))
    evaluation_store.cancel(job.id)

    anyio.run(run_evaluation_job, job.id)

    cancelled = evaluation_store.get(job.id)
    assert cancelled.status == JobStatus.CANCELLED
    assert [item.status for item in cancelled.results] == [CaseStatus.CANCELLED] * 2
    assert model.calls == ()


@pytest.fixture
def no_background(monkeypatch: MonkeyPatch) -> list[str]:
    """API のテストは job を作るだけ（実行は上のテストで確かめる）。"""
    started: list[str] = []

    async def record(job_id: str) -> None:
        started.append(job_id)

    monkeypatch.setattr("app.features.agent.router.run_evaluation_job", record)
    return started


SET_BODY: dict[str, Any] = {
    "agent_id": AGENT_ID,
    "name": "経理の評価",
    "description": "月次の問い合わせ",
    "cases": [
        {"question": "今月の売上は？", "expected": "300 万円", "expected_tools": [" rag_search "]},
        {"id": "custom", "question": "経費は？", "expected": "50 万円"},
    ],
}


def test_evaluation_sets_api(agent: None, no_background: list[str], tmp_path: Path) -> None:
    del agent
    created = client.post("/api/evaluation-sets", json=SET_BODY)
    assert created.status_code == 200, created.text
    evaluation_set = created.json()["data"]
    assert [case["id"] for case in evaluation_set["cases"]] == ["case-1", "custom"]
    assert evaluation_set["cases"][0]["expected_tools"] == ["rag_search"]
    set_id = evaluation_set["id"]
    # 保存先に残る（再起動しても使える）。
    assert set_id in json.loads((tmp_path / "items.json").read_text())["evaluation_set"]

    listed = client.get("/api/evaluation-sets", params={"agent_id": AGENT_ID}).json()["data"]
    assert [(item["id"], item["case_count"]) for item in listed["sets"]] == [(set_id, 2)]

    updated = client.put(
        f"/api/evaluation-sets/{set_id}", json={**SET_BODY, "name": "経理の評価（改）"}
    )
    assert updated.json()["data"]["name"] == "経理の評価（改）"
    moved = client.put(f"/api/evaluation-sets/{set_id}", json={**SET_BODY, "agent_id": "default"})
    assert moved.status_code == 422

    started = client.post("/api/evaluations", json={"set_id": set_id})
    assert started.status_code == 202, started.text
    job = started.json()["data"]
    assert (job["set_id"], job["set_name"], len(job["results"])) == (set_id, "経理の評価（改）", 2)
    assert client.post("/api/evaluations", json={"set_id": set_id}).status_code == 409
    # 実行中の評価が使っている評価セットは削除できない。
    assert client.delete(f"/api/evaluation-sets/{set_id}").status_code == 409
    client.post(f"/api/evaluations/{job['id']}/cancel")
    sets = client.get("/api/evaluation-sets").json()["data"]["sets"]
    assert sets[0]["last_job_status"] == "cancelled"
    assert client.delete(f"/api/evaluation-sets/{set_id}").status_code == 200
    assert client.get(f"/api/evaluation-sets/{set_id}").status_code == 404


def test_cancelling_an_evaluation_records_who_cancelled_the_running_runs(agent: None) -> None:
    """評価の取消で取り消した Run に、操作した人を残す（実行の詳細の「取消」。#3348）。"""
    del agent
    job = _job(_case("one", "合成の質問", "合成の回答"))
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(agent_id=AGENT_ID, goal="合成の質問", draft=True)
    )
    evaluation_store.update_result(job.id, 0, status=CaseStatus.RUNNING, run_id=run.id)
    assert client.post(f"/api/evaluations/{job.id}/cancel").status_code == 200
    record = run_cancel_record(runtime_repository.get_run(run.id))
    assert record is not None
    assert (record.via, record.cancelled_by) == ("screen", local_debug_principal().login_user_id)


def test_previous_run_of_the_same_set_is_compared(agent: None) -> None:
    del agent
    first = _job(_case("c1", "q", "x"), set_id="evset_compare")
    evaluation_store.update(first.id, status=JobStatus.COMPLETED)
    evaluation_store.update_result(
        first.id,
        0,
        status=CaseStatus.JUDGED,
        judgement=evaluation.EvaluationJudgement(
            verdict=JudgeVerdict.INCORRECT, score=0.0, summary="誤り", missing_points=[]
        ),
    )
    second = _job(_case("c1", "q", "x"), set_id="evset_compare")

    data = client.get(f"/api/evaluations/{second.id}").json()["data"]

    assert data["previous_job_id"] == first.id
    assert data["previous_summary"]["pass_rate"] == 0.0
    # 前回が無い評価は比較しない。
    assert client.get(f"/api/evaluations/{first.id}").json()["data"]["previous_summary"] is None


def _finished_job(created_at: datetime, *, set_id: str = "evset_history") -> EvaluationJob:
    judged = EvaluationCaseResult(
        case=_case("c1", "q", "x"),
        status=CaseStatus.JUDGED,
        judgement=evaluation.EvaluationJudgement(
            verdict=JudgeVerdict.CORRECT, score=1.0, summary="正しい", missing_points=[]
        ),
    )
    job = EvaluationJob(
        agent_id=AGENT_ID,
        set_id=set_id,
        status=JobStatus.COMPLETED,
        results=[judged],
        created_at=created_at,
        finished_at=created_at,
    )
    evaluation_store.restore(job)
    return job


def test_history_is_kept_without_limit_and_listed_page_by_page(agent: None) -> None:
    """評価の履歴は期間・件数で消さずに残し（#3235）、ページで読む（#794）。"""
    del agent
    now = datetime.now(UTC)
    assert (EVALUATION_JOBS_RETENTION_DAYS, evaluation.EVALUATION_JOBS_MAX) == (0, 0)
    oldest = _finished_job(now - timedelta(days=3650))
    jobs = [_finished_job(now - timedelta(days=300 - index)) for index in range(60)]

    assert evaluation_store.prune() == ()
    # 新しい評価を始めても、古い履歴は消えない。
    latest = _job(_case("c1", "q", "x"), set_id="evset_history")

    first = client.get("/api/evaluations", params={"set_id": "evset_history"}).json()["data"]
    assert (first["total"], first["offset"], first["limit"], len(first["items"])) == (62, 0, 25, 25)
    assert first["items"][0]["id"] == latest.id
    last = client.get(
        "/api/evaluations", params={"set_id": "evset_history", "offset": 60, "limit": 10}
    ).json()["data"]
    assert [item["id"] for item in last["items"]] == [jobs[0].id, oldest.id]
    assert client.get("/api/evaluations", params={"limit": 101}).status_code == 422
    # 前回との比較は、残っているすべての履歴から探す。
    second = client.get(f"/api/evaluations/{jobs[1].id}").json()["data"]
    assert second["previous_job_id"] == jobs[0].id
    assert client.get(f"/api/evaluations/{oldest.id}").status_code == 200


def test_history_prune_applies_only_configured_limits(
    agent: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """保持期間・件数の上限を決めたときだけ、終わった job を古い順に消す。"""
    del agent
    now = datetime.now(UTC)
    expired = _finished_job(now - timedelta(days=40))
    kept = _finished_job(now - timedelta(days=10))
    monkeypatch.setattr(evaluation, "EVALUATION_JOBS_RETENTION_DAYS", 30)

    assert evaluation_store.prune() == (expired.id,)
    assert client.get(f"/api/evaluations/{kept.id}").status_code == 200

    monkeypatch.setattr(evaluation, "EVALUATION_JOBS_RETENTION_DAYS", 0)
    newer = _finished_job(now - timedelta(days=5))
    monkeypatch.setattr(evaluation, "EVALUATION_JOBS_MAX", 1)
    assert evaluation_store.prune() == (kept.id,)
    assert client.get(f"/api/evaluations/{newer.id}").status_code == 200


def test_cases_can_be_imported_and_exported_as_excel(agent: None) -> None:
    del agent
    template = client.get("/api/evaluation-sets/template.xlsx")
    assert template.status_code == 200
    assert template.headers["content-type"].startswith("application/vnd.openxmlformats")
    parsed = client.post(
        "/api/evaluation-sets/parse-xlsx",
        files={"file": ("cases.xlsx", template.content, "application/octet-stream")},
    )
    assert parsed.status_code == 200, parsed.text
    [sample] = parsed.json()["data"]["cases"]
    assert (sample["id"], sample["expected_tools"], sample["expected_tools_in_order"]) == (
        "expense-deadline",
        ["rag_retrieve_evidence", "rag_validate_answer"],
        True,
    )

    set_id = client.post("/api/evaluation-sets", json=SET_BODY).json()["data"]["id"]
    exported = client.get(f"/api/evaluation-sets/{set_id}/cases.xlsx")
    assert exported.status_code == 200
    cases = parse_cases_xlsx(exported.content)
    assert [(case.id, case.question, case.expected_tools) for case in cases] == [
        ("case-1", "今月の売上は？", ["rag_search"]),
        ("custom", "経費は？", []),
    ]
    assert [case.expected_tools_in_order for case in cases] == [False, False]

    broken = client.post(
        "/api/evaluation-sets/parse-xlsx",
        files={"file": ("cases.xlsx", b"not excel", "application/octet-stream")},
    )
    assert broken.status_code == 422
    assert "Excel" in broken.text


def test_sets_and_jobs_are_restored_and_running_jobs_are_interrupted(agent: None) -> None:
    del agent
    set_id = client.post("/api/evaluation-sets", json=SET_BODY).json()["data"]["id"]
    job = _job(_case("c1", "q", "x"), set_id=set_id)
    evaluation_store.update(job.id, status=JobStatus.RUNNING)

    evaluation_store.clear()
    evaluation_set_store.clear()
    restored = restore_control_plane()

    assert (restored["evaluation_set"], restored["evaluation_job"]) == (1, 1)
    assert evaluation_set_store.get(set_id).name == "経理の評価"
    interrupted = evaluation_store.get(job.id)
    assert interrupted.status == JobStatus.FAILED
    assert interrupted.error and "再起動" in interrupted.error
    assert interrupted.results[0].status == CaseStatus.CANCELLED


@pytest.mark.parametrize(
    "cases",
    [
        [],
        [{"question": "  ", "expected": "x"}],
        [
            {"id": "a", "question": "q", "expected": "x"},
            {"id": "a", "question": "q", "expected": "y"},
        ],
        [{"question": f"q{index}", "expected": "x"} for index in range(51)],
        [{"question": "q", "expected": "x", "expected_tools": [f"t{i}" for i in range(11)]}],
    ],
    ids=["empty", "blank-question", "duplicate-id", "too-many", "too-many-tools"],
)
def test_invalid_sets_are_rejected(agent: None, cases: list[dict[str, Any]]) -> None:
    del agent
    response = client.post("/api/evaluation-sets", json={**SET_BODY, "cases": cases})
    assert response.status_code == 422


def test_cases_without_id_get_an_unused_id_after_a_case_is_removed(agent: None) -> None:
    """ケースを削除して足しても、id を省いたケースに使われていない id を付けて保存する（#965）。"""
    del agent
    body = {
        **SET_BODY,
        "cases": [{"question": f"質問 {index}", "expected": "要点"} for index in range(1, 4)],
    }
    created = client.post("/api/evaluation-sets", json=body).json()["data"]
    assert [case["id"] for case in created["cases"]] == ["case-1", "case-2", "case-3"]

    # 1 件目を削除し、id を省いたケースを末尾に足す（位置の番号は 3 で、残った case-3 と重なる）。
    cases = [*created["cases"][1:], {"question": "質問 4", "expected": "要点"}]
    updated = client.put(f"/api/evaluation-sets/{created['id']}", json={**body, "cases": cases})
    assert updated.status_code == 200, updated.text
    assert [case["id"] for case in updated.json()["data"]["cases"]] == [
        "case-2",
        "case-3",
        "case-4",
    ]

    # 後ろのケースが明示した id（case-1）も、前の省いたケースには付けない。
    numbered = client.post(
        "/api/evaluation-sets",
        json={
            **body,
            "cases": [
                {"question": "質問 a", "expected": "要点"},
                {"id": "case-1", "question": "質問 b", "expected": "要点"},
            ],
        },
    )
    assert numbered.status_code == 200, numbered.text
    assert [case["id"] for case in numbered.json()["data"]["cases"]] == ["case-2", "case-1"]


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> Iterator[ProductionAuth]:
    yield enable_production_auth(monkeypatch)
    set_security_service(None)


def test_evaluation_permissions(
    auth: ProductionAuth, agent: None, no_background: list[str]
) -> None:
    del agent
    auth.user_with_permissions("runs-776", [MENU_RUNS], agent_ids=[AGENT_ID])
    denied = client.post("/api/evaluation-sets", json=SET_BODY, headers=login("runs-776"))
    assert denied.status_code == 403

    # 評価の Run は始めた利用者の Run なので、利用できない業務 Agent の評価セットは作れない。
    auth.user_with_permissions("eval-other-776", [MENU_EVALUATION], agent_ids=["default"])
    other = login("eval-other-776")
    assert client.post("/api/evaluation-sets", json=SET_BODY, headers=other).status_code == 403

    evaluator = auth.user_with_permissions("eval-776", [MENU_EVALUATION], agent_ids=[AGENT_ID])
    headers = login("eval-776")
    set_id = client.post("/api/evaluation-sets", json=SET_BODY, headers=headers).json()["data"][
        "id"
    ]
    created = client.post("/api/evaluations", json={"set_id": set_id}, headers=headers)
    assert created.status_code == 202, created.text
    job_id = created.json()["data"]["id"]
    assert evaluation_store.get(job_id).created_by_user_uuid == evaluator.user_uuid
    # 利用できない業務 Agent の評価セット・評価は一覧に出さず、読めない。
    assert client.get("/api/evaluation-sets", headers=other).json()["data"]["sets"] == []
    assert client.get("/api/evaluations", headers=other).json()["data"]["items"] == []
    assert client.get(f"/api/evaluations/{job_id}", headers=other).status_code == 404
    assert client.get(f"/api/evaluation-sets/{set_id}", headers=other).status_code == 404


def test_draft_evaluation_needs_agent_admin(
    auth: ProductionAuth, agent: None, no_background: list[str]
) -> None:
    """下書きの評価は下書きの実行と同じく Agent 管理だけ（#2522）。既定は公開中の版。"""
    del agent
    runtime_repository.patch_agent(AGENT_ID, AgentProfilePatch(instructions="作業中の指示"))
    auth.user_with_permissions("eval-2522", [MENU_EVALUATION], agent_ids=[AGENT_ID])
    headers = login("eval-2522")
    set_id = client.post("/api/evaluation-sets", json=SET_BODY, headers=headers).json()["data"][
        "id"
    ]

    draft = client.post(
        "/api/evaluations", json={"set_id": set_id, "agent_version": "draft"}, headers=headers
    )
    assert draft.status_code == 403, draft.text
    assert "Agent 管理" in draft.json()["error_messages"][0]

    # 公開していない変更があっても、Agent 管理でなければ既定は公開中の版。
    default = client.post("/api/evaluations", json={"set_id": set_id}, headers=headers)
    assert default.status_code == 202, default.text
    assert default.json()["data"]["agent_version"] == 1

    # Agent 管理は、公開していない変更があれば既定で下書きを評価する（同時に動く評価は 1 つ）。
    evaluation_store.clear()
    auth.user_with_permissions("admin-2522", [ADMIN])
    admin = login("admin-2522")
    other_set = client.post("/api/evaluation-sets", json=SET_BODY, headers=admin).json()["data"]
    admin_default = client.post("/api/evaluations", json={"set_id": other_set["id"]}, headers=admin)
    assert admin_default.status_code == 202, admin_default.text
    assert admin_default.json()["data"]["agent_version"] == "draft"


def test_settle_waits_for_the_dispatcher(monkeypatch: MonkeyPatch, agent: None) -> None:
    """dispatcher のモードでは自分で実行せず、別プロセスの実行を待つ（二重に実行しない）。"""
    del agent
    from app.settings import get_settings

    monkeypatch.setattr(get_settings(), "agent_runtime_dispatch_mode", "dispatcher")
    monkeypatch.setattr(evaluation, "settle_poll_seconds", 0.01)
    executed: list[str] = []

    async def fake_execute(run_id: str) -> None:
        executed.append(run_id)

    monkeypatch.setattr(builtin_runtime, "execute_run", fake_execute)
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="q", agent_id=AGENT_ID), created_by_user_uuid=USER_UUID
    )

    async def dispatcher_completes() -> None:
        await anyio.sleep(0.05)
        runtime_repository.cancel_run(run.id)

    async def scenario() -> Any:
        async with anyio.create_task_group() as group:
            group.start_soon(dispatcher_completes)
            settled = await evaluation._settle_run(run.id, 5.0)  # noqa: SLF001 - 待ち方の確認
        return settled

    settled = anyio.run(scenario)
    assert settled.status == RunStatus.CANCELLED
    assert executed == []


# ---- #1432: 1 ケースの上限時間 ----


def test_case_timeout_defaults_to_rag_wait_times_rag_budget(monkeypatch: MonkeyPatch) -> None:
    """既定（0）は接続 `rag` の待ち時間 ×（RAG の予算 + 1）。RAG を使う Run を 300 秒で切らない。"""
    from app.settings import Settings, get_settings

    defaults = Settings(_env_file=None)
    assert defaults.agent_evaluation_case_timeout_seconds == 0
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_evaluation_case_timeout_seconds", 0.0)
    monkeypatch.setattr(settings, "agent_max_rag_calls_per_run", 6)
    monkeypatch.setattr(evaluation, "_rag_timeout_seconds", lambda: 630.0)
    assert evaluation.case_timeout_seconds() == 630 * (6 + 1) == 4410
    # RAG の予算が無制限なら、Run のツールの呼び出しの上限を回数に使う。
    monkeypatch.setattr(settings, "agent_max_rag_calls_per_run", 0)
    monkeypatch.setattr(settings, "agent_max_tool_calls_per_run", 20)
    assert evaluation.case_timeout_seconds() == 630 * (20 + 1)
    # 自動の値は 300 秒〜86400 秒に収める。
    monkeypatch.setattr(settings, "agent_max_rag_calls_per_run", 1)
    monkeypatch.setattr(evaluation, "_rag_timeout_seconds", lambda: 10.0)
    assert evaluation.case_timeout_seconds() == evaluation.CASE_TIMEOUT_MIN_SECONDS
    monkeypatch.setattr(settings, "agent_max_rag_calls_per_run", 1000)
    monkeypatch.setattr(evaluation, "_rag_timeout_seconds", lambda: 900.0)
    assert evaluation.case_timeout_seconds() == evaluation.CASE_TIMEOUT_MAX_SECONDS


def test_case_timeout_uses_saved_rag_connection_and_explicit_setting(
    monkeypatch: MonkeyPatch,
) -> None:
    """画面で保存した接続 `rag` の待ち時間を使い、明示の設定はそれより優先する。"""
    from app.features.agent.config import runtime_config_store
    from app.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "agent_evaluation_case_timeout_seconds", 0.0)
    monkeypatch.setattr(settings, "agent_max_rag_calls_per_run", 6)
    connections = {config.server_id: config for config in runtime_config_store.list_mcp_servers()}
    connections["rag"] = connections["rag"].model_copy(update={"timeout_seconds": 900.0})
    monkeypatch.setattr(runtime_config_store, "_mcp_servers", connections)
    assert evaluation.case_timeout_seconds() == 900 * 7
    monkeypatch.setattr(settings, "agent_evaluation_case_timeout_seconds", 1800.0)
    assert evaluation.case_timeout_seconds() == 1800
    monkeypatch.setattr(settings, "agent_evaluation_case_timeout_seconds", 100_000.0)
    assert evaluation.case_timeout_seconds() == evaluation.CASE_TIMEOUT_MAX_SECONDS


def test_case_times_out_with_configured_limit(monkeypatch: MonkeyPatch, agent: None) -> None:
    """上限時間までに Run が決着しなければ、Run を取り消して時間切れ（実際の上限の秒数）にする。"""
    del agent
    from app.settings import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "agent_runtime_dispatch_mode", "dispatcher")
    monkeypatch.setattr(settings, "agent_evaluation_case_timeout_seconds", 0.05)
    monkeypatch.setattr(evaluation, "settle_poll_seconds", 0.01)
    job = _job(_case("c1", "今月の売上は？", "300 万円"))

    anyio.run(run_evaluation_job, job.id)

    result = evaluation_store.get(job.id).results[0]
    assert result.status == CaseStatus.TIMED_OUT
    assert result.error == "上限時間（0.05 秒）までに回答が出ませんでした。"
    assert result.run_id is not None
    assert runtime_repository.get_run(result.run_id).status == RunStatus.CANCELLED


# ---- #810: フィードバックから評価ケース・テンプレートの評価ケース・評価する版 ----


def _answered_run(question: str, *, tools: list[str], comment: str = "") -> str:
    """回答が出た Run（呼んだツールの step と、管理者の評価のコメントを持つ）。"""
    from app.features.agent.runtime import FeedbackRating, FeedbackReason, RunFeedback
    from app.features.agent.tools import ToolCall

    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal=question, agent_id=AGENT_ID), created_by_user_uuid=USER_UUID
    )
    runtime_repository.record_builtin_dry_run_steps(
        run.id, [ToolCall(name=name, arguments={}) for name in tools]
    )
    runtime_repository.complete_builtin_run(run.id, "分かりません。")
    if comment:
        runtime_repository.set_run_feedback(
            run.id,
            RunFeedback(
                rating=FeedbackRating.NOT_HELPFUL,
                reason=FeedbackReason.INCORRECT,
                comment=comment,
            ),
            admin=True,
        )
    return run.id


def test_case_draft_and_append_from_a_run(agent: None, tmp_path: Path) -> None:
    """フィードバック・Run の詳細から評価ケースを足す（#810）。"""
    del agent
    run_id = _answered_run(
        "  先月の売上は？ ",
        tools=["rag__rag_search", WRITE, "rag__rag_search"],
        comment="300 万円と答えること",
    )
    draft = client.get(f"/api/runs/{run_id}/evaluation-case")
    assert draft.status_code == 200, draft.text
    data = draft.json()["data"]
    assert data["question"] == "先月の売上は？"
    assert data["expected"] == "300 万円と答えること"
    assert data["expected_tools"] == ["rag__rag_search", WRITE]
    assert (data["agent_id"], data["source_run_id"], data["existing_set_ids"]) == (
        AGENT_ID,
        run_id,
        [],
    )

    set_id = client.post("/api/evaluation-sets", json=SET_BODY).json()["data"]["id"]
    case = {
        "question": data["question"],
        "expected": data["expected"],
        "expected_tools": data["expected_tools"],
        "source_run_id": run_id,
    }
    added = client.post(f"/api/evaluation-sets/{set_id}/cases", json=case)
    assert added.status_code == 200, added.text
    cases = added.json()["data"]["cases"]
    assert [item["id"] for item in cases] == ["case-1", "custom", "case-3"]
    assert cases[-1]["source_run_id"] == run_id
    # 保存先に残る。
    saved = json.loads((tmp_path / "items.json").read_text())["evaluation_set"][set_id]
    assert saved["cases"][-1]["source_run_id"] == run_id
    # 同じ質問（空白の違いだけ）は足さない。下書きは既にある評価セットを知らせる。
    duplicate = client.post(
        f"/api/evaluation-sets/{set_id}/cases", json={**case, "question": "先月の売上は？  "}
    )
    assert duplicate.status_code == 409
    assert "同じ質問" in duplicate.json()["error_messages"][0]
    again = client.get(f"/api/runs/{run_id}/evaluation-case").json()["data"]
    assert again["existing_set_ids"] == [set_id]
    # 期待する回答の要点は必須。
    blank = client.post(f"/api/evaluation-sets/{set_id}/cases", json={**case, "expected": " "})
    assert blank.status_code == 422

    # 評価セットを保存し直しても（画面・Excel が出どころを送らなくても）出どころを保つ。
    resaved = client.put(
        f"/api/evaluation-sets/{set_id}",
        json={**SET_BODY, "cases": [*SET_BODY["cases"], {**case, "id": "case-3"}]},
    )
    without_source = [
        {key: value for key, value in item.items() if key != "source_run_id"}
        for item in resaved.json()["data"]["cases"]
    ]
    resaved = client.put(
        f"/api/evaluation-sets/{set_id}", json={**SET_BODY, "cases": without_source}
    )
    assert resaved.json()["data"]["cases"][-1]["source_run_id"] == run_id


def test_append_case_keeps_the_set_limits(agent: None) -> None:
    del agent
    full = {
        **SET_BODY,
        "cases": [
            {"question": f"q{index}", "expected": "x"}
            for index in range(evaluation.EVALUATION_MAX_CASES)
        ],
    }
    set_id = client.post("/api/evaluation-sets", json=full).json()["data"]["id"]
    response = client.post(
        f"/api/evaluation-sets/{set_id}/cases", json={"question": "新しい質問", "expected": "x"}
    )
    assert response.status_code == 409
    assert "50 件まで" in response.json()["error_messages"][0]
    # 別の業務 Agent の Run を出どころにはできない。
    other_set = client.post("/api/evaluation-sets", json=SET_BODY).json()["data"]["id"]
    foreign = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="q", agent_id="default"), created_by_user_uuid=USER_UUID
    )
    try:
        mismatch = client.post(
            f"/api/evaluation-sets/{other_set}/cases",
            json={"question": "別", "expected": "x", "source_run_id": foreign.id},
        )
        assert mismatch.status_code == 422
    finally:
        repository: Any = runtime_repository
        with repository._lock:  # noqa: SLF001 - テストの後始末
            repository._runs.pop(foreign.id, None)
    missing = client.post(
        "/api/evaluation-sets/evset_missing/cases", json={"question": "q", "expected": "x"}
    )
    assert missing.status_code == 404
    assert client.get("/api/runs/run_missing/evaluation-case").status_code == 404


def test_evaluation_set_from_the_agent_template(agent: None) -> None:
    """テンプレートから作った業務 Agent は、その評価ケースで評価セットを作れる（#810）。"""
    del agent
    from app.features.agent.templates import find_template

    template = find_template("expense-assistant")
    assert template is not None
    created = client.post(
        "/api/agents",
        json={
            "id": "eval-810-template",
            "name": "経費精算",
            "skill_ids": [],
            "template_id": template.id,
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["data"]["template_id"] == template.id
    try:
        response = client.post(
            "/api/evaluation-sets/from-template", json={"agent_id": "eval-810-template"}
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["agent_id"] == "eval-810-template"
        assert data["name"] == "経費精算（テンプレート）"
        assert [item["question"] for item in data["cases"]] == [
            case.question for case in template.evaluation_cases
        ]
        # テンプレートを使っていない業務 Agent は作れない。
        plain = client.post("/api/evaluation-sets/from-template", json={"agent_id": AGENT_ID})
        assert plain.status_code == 409
        # 知らないテンプレートでは業務 Agent を作らない。
        unknown = client.post(
            "/api/agents",
            json={"id": "eval-810-unknown", "name": "x", "skill_ids": [], "template_id": "nope"},
        )
        assert unknown.status_code == 400
    finally:
        evaluation_set_store.clear()
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent("eval-810-template")


def test_evaluation_records_and_pins_the_agent_version(
    monkeypatch: MonkeyPatch, agent: None, no_background: list[str]
) -> None:
    """評価する版を選び、job に残し、Run を始めたときの版に固定する（#810）。"""
    del agent
    from app.features.agent.runtime import AgentProfilePatch

    set_id = client.post("/api/evaluation-sets", json=SET_BODY).json()["data"]["id"]
    # 公開していない変更が無ければ、既定は公開中の版。
    published = client.post("/api/evaluations", json={"set_id": set_id}).json()["data"]
    assert published["agent_version"] == 1
    client.post(f"/api/evaluations/{published['id']}/cancel")
    listed = client.get("/api/evaluations").json()["data"]["items"]
    assert listed[0]["agent_version"] == 1

    # 公開していない変更があれば、既定は下書き。
    runtime_repository.patch_agent(AGENT_ID, AgentProfilePatch(instructions="下書きの指示"))
    draft = client.post("/api/evaluations", json={"set_id": set_id}).json()["data"]
    assert draft["agent_version"] == "draft"
    client.post(f"/api/evaluations/{draft['id']}/cancel")
    chosen = client.post(
        "/api/evaluations", json={"set_id": set_id, "agent_version": "published"}
    ).json()["data"]
    assert chosen["agent_version"] == 1
    client.post(f"/api/evaluations/{chosen['id']}/cancel")
    invalid = client.post("/api/evaluations", json={"set_id": set_id, "agent_version": "v9"})
    assert invalid.status_code == 422

    # 下書きの Run と、版に固定した Run（評価の途中で公開し直しても版は変わらない）。
    _script(
        monkeypatch,
        [assistant_message("下書きの回答")],
        _judgement("correct", 1.0, "良い"),
        [assistant_message("v1 の回答")],
        _judgement("correct", 1.0, "良い"),
    )
    draft_job = evaluation_store.create(
        EvaluationJob(
            agent_id=AGENT_ID,
            set_id="evset_versions",
            agent_version="draft",
            created_by_user_uuid=USER_UUID,
            results=[EvaluationCaseResult(case=_case("c1", "q", "x"))],
        )
    )
    anyio.run(run_evaluation_job, draft_job.id)
    draft_run = runtime_repository.get_run(
        str(evaluation_store.get(draft_job.id).results[0].run_id)
    )
    assert draft_run.metadata["agent_version"] == "draft"

    runtime_repository.publish_agent(AGENT_ID)
    pinned_job = evaluation_store.create(
        EvaluationJob(
            agent_id=AGENT_ID,
            set_id="evset_versions",
            agent_version=1,
            created_by_user_uuid=USER_UUID,
            results=[EvaluationCaseResult(case=_case("c1", "q", "x"))],
        )
    )
    anyio.run(run_evaluation_job, pinned_job.id)
    pinned = evaluation_store.get(pinned_job.id)
    assert pinned.status == JobStatus.COMPLETED
    assert runtime_repository.get_run(str(pinned.results[0].run_id)).metadata["agent_version"] == 1


def test_unpublished_agent_is_evaluated_as_a_draft(agent: None, no_background: list[str]) -> None:
    """公開した版の無い業務 Agent は下書きで評価し、公開中の版は選べない（#810）。"""
    del agent
    runtime_repository.create_agent(
        AgentProfile(id="eval-810-draft", name="下書きだけ", versioned=True, skill_ids=[])
    )
    try:
        body = {**SET_BODY, "agent_id": "eval-810-draft"}
        set_id = client.post("/api/evaluation-sets", json=body).json()["data"]["id"]
        refused = client.post(
            "/api/evaluations", json={"set_id": set_id, "agent_version": "published"}
        )
        assert refused.status_code == 409
        assert "公開した版がありません" in refused.json()["error_messages"][0]
        started = client.post("/api/evaluations", json={"set_id": set_id})
        assert started.status_code == 202, started.text
        assert started.json()["data"]["agent_version"] == "draft"
        client.post(f"/api/evaluations/{started.json()['data']['id']}/cancel")
    finally:
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent("eval-810-draft")


def test_previous_run_is_compared_only_with_the_same_cases(agent: None) -> None:
    """評価ケースを変えた後の結果とは比べず、比べた版を返す（#810）。"""
    del agent
    base = datetime.now(UTC) - timedelta(hours=3)

    def finished(cases: list[EvaluationCase], version: Any, hours: int) -> EvaluationJob:
        job = EvaluationJob(
            agent_id=AGENT_ID,
            set_id="evset_fp",
            agent_version=version,
            status=JobStatus.COMPLETED,
            results=[EvaluationCaseResult(case=case) for case in cases],
            created_at=base + timedelta(hours=hours),
        )
        evaluation_store.restore(job)
        return job

    same = [_case("c1", "q", "x")]
    first = finished(same, 1, 0)
    # 評価ケースを変えた評価（比べる相手にしない）。
    finished([_case("c1", "q", "y")], 2, 1)
    latest = finished([_case("c1", " q ", "x")], "draft", 2)

    data = client.get(f"/api/evaluations/{latest.id}").json()["data"]
    assert data["previous_job_id"] == first.id
    assert data["previous_agent_version"] == 1
    assert data["agent_version"] == "draft"
    assert data["previous_created_at"] is not None


def _incomplete() -> builtin_runtime.BuiltinRuntimeError:
    return builtin_runtime.BuiltinRuntimeError(
        builtin_runtime.MODEL_OUTPUT_INCOMPLETE_CODE,
        "モデル「judge-model」の応答が出力の上限に達して未完了でした。",
    )


def test_judge_stopped_at_the_output_limit_is_retried_once(
    monkeypatch: MonkeyPatch, agent: None
) -> None:
    """判定の出力が上限で止まったら広げて 1 回再試行し、それでも止まれば判定できない（#1845）。"""
    del agent
    _script(
        monkeypatch,
        [assistant_message("第 3 日曜は MES です。")],
        [assistant_message("経費は 50 万円です。")],
    )
    monkeypatch.setattr(ai_calls, "enterprise_ai_retry_output_token_budget", lambda *_a, **_k: 0)
    budgets: list[int] = []
    outcomes: list[Exception | evaluation.EvaluationJudgement] = [
        _incomplete(),
        evaluation.EvaluationJudgement(
            verdict=JudgeVerdict.CORRECT,
            score=1.0,
            summary="要点を満たしています。",
            missing_points=[],
        ),
        _incomplete(),
        _incomplete(),
    ]

    async def judge_once(
        _case: EvaluationCase, _answer: str, target: ModelTarget, *_args: Any
    ) -> evaluation.EvaluationJudgement:
        budgets.append(target.max_output_tokens)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(evaluation, "_judge_once", judge_once)
    job = _job(_case("retried", "第 3 日曜は？", "MES"), _case("unjudged", "経費は？", "50 万円"))

    anyio.run(run_evaluation_job, job.id)

    finished = evaluation_store.get(job.id)
    retried, unjudged = finished.results
    assert (retried.status, retried.judge_attempts) == (CaseStatus.JUDGED, 2)
    assert retried.judgement is not None and retried.judgement.verdict == JudgeVerdict.CORRECT
    assert (unjudged.status, unjudged.judge_attempts) == (CaseStatus.JUDGE_FAILED, 2)
    assert unjudged.judgement is None
    assert unjudged.error and "判定できなかった" in unjudged.error
    # 広げられない（もうモデルの出力の上限）ときは、同じ上限で送り直す。
    assert len(budgets) == 4 and len(set(budgets)) == 1
    summary = finished.summary
    assert (summary.correct, summary.incorrect, summary.unjudged, summary.errors) == (1, 0, 1, 1)
    assert summary.pass_rate == 1.0


def test_judge_retry_widens_the_output_limit(monkeypatch: MonkeyPatch) -> None:
    """広げられるときはモデルの出力の上限まで広げて送り直す（#1845）。"""
    target = ModelTarget(
        model_id="judge-model",
        endpoint="https://llm.example",
        project_id="",
        api_key="k",
        max_output_tokens=8192,
    )
    monkeypatch.setattr(
        ai_calls,
        "enterprise_ai_retry_output_token_budget",
        lambda _settings, _model, *, current: 32768 if current < 32768 else None,
    )
    assert retry_model_target(target).max_output_tokens == 32768
    widest = retry_model_target(
        ModelTarget(
            model_id="judge-model",
            endpoint="https://llm.example",
            project_id="",
            api_key="k",
            max_output_tokens=32768,
        )
    )
    assert widest.max_output_tokens == 32768


@pytest.mark.parametrize(
    ("calls", "in_order", "selection", "order"),
    [
        (
            ["rag__rag_retrieve_evidence", "rag__rag_lookup_guides", "rag__rag_search"],
            True,
            True,
            True,
        ),
        # 間にほかのツールを挟んでもよい。
        (
            ["rag__rag_list_profiles", "rag__rag_retrieve_evidence", "rag__rag_search"],
            True,
            True,
            True,
        ),
        (["rag__rag_search", "rag__rag_retrieve_evidence"], True, False, False),
        (["rag__rag_retrieve_evidence"], True, False, False),
        # 順を指定しないケースは順を判定しない。
        (["rag__rag_search", "rag__rag_retrieve_evidence"], False, True, None),
    ],
    ids=["in-order", "other-tools-between", "wrong-order", "missing", "order-not-required"],
)
def test_expected_tool_order(
    calls: list[str], in_order: bool, selection: bool, order: bool | None
) -> None:
    """期待するツールの順（先頭が最初に呼ぶツール）を判定に使う（#1845）。"""
    case = EvaluationCase(
        question="q",
        expected="x",
        expected_tools=["rag_retrieve_evidence", "rag_search"],
        expected_tools_in_order=in_order,
    )
    assert evaluation.tool_order_correct(case, calls) is order
    assert evaluation.tool_selection_correct(case, calls) is selection


def test_tool_order_is_judged_in_a_job_and_changes_the_case_fingerprint(
    monkeypatch: MonkeyPatch, agent: None
) -> None:
    del agent
    _script(
        monkeypatch,
        [function_call(WRITE, {"query": "登録"}, call_id="call-1845")],
        [assistant_message("登録しました（評価のため実行していません）。")],
        _judgement("correct", 1.0, "手順を答えています。"),
    )
    ordered = EvaluationCase(
        id="ordered",
        question="取引先を登録して",
        expected="登録の手順",
        expected_tools=["rag_search", WRITE],
        expected_tools_in_order=True,
    )
    job = _job(ordered)

    anyio.run(run_evaluation_job, job.id)

    [result] = evaluation_store.get(job.id).results
    assert result.tool_calls == [WRITE]
    assert (result.tool_selection_correct, result.tool_order_correct) == (False, False)
    unordered = ordered.model_copy(update={"expected_tools_in_order": False})
    reversed_order = ordered.model_copy(update={"expected_tools": [WRITE, "rag_search"]})
    fingerprints = {
        evaluation.cases_fingerprint(
            EvaluationJob(agent_id=AGENT_ID, results=[EvaluationCaseResult(case=item)])
        )
        for item in (ordered, unordered, reversed_order)
    }
    assert len(fingerprints) == 3


# ---- Agent をまたいだ比較（#1845） ----------------------------------------------------------

OTHER_AGENT_ID = "eval-1845-other-agent"


def _judged(case: EvaluationCase, verdict: JudgeVerdict, score: float) -> EvaluationCaseResult:
    return EvaluationCaseResult(
        case=case,
        status=CaseStatus.JUDGED,
        judgement=evaluation.EvaluationJudgement(
            verdict=verdict, score=score, summary="判定", missing_points=[]
        ),
        tool_calls=["rag__rag_search"],
        run_id=f"run-{case.id}",
    )


def _completed(agent_id: str, *results: EvaluationCaseResult, set_id: str = "evset_x") -> Any:
    job = EvaluationJob(
        agent_id=agent_id,
        agent_name=agent_id,
        set_id=set_id,
        set_name="横断の評価",
        status=JobStatus.COMPLETED,
        results=list(results),
    )
    evaluation_store.restore(job)
    return job


def test_compare_jobs_aligns_cases_by_question() -> None:
    """同じ質問のケースどうしを並べ、合否で改善・悪化・変化なしを決める（#1845）。"""
    q1 = _case("a1", "締め日は？", "25 日")
    q2 = _case("a2", "承認者は？", "部長")
    q3 = _case("a3", "経費は？", "50 万円")
    only_base = _case("a4", "前だけの質問", "x")
    base = EvaluationJob(
        agent_id=AGENT_ID,
        results=[
            _judged(q1, JudgeVerdict.INCORRECT, 0.2),
            _judged(q2, JudgeVerdict.CORRECT, 1.0),
            _judged(q3, JudgeVerdict.CORRECT, 0.9),
            _judged(only_base, JudgeVerdict.CORRECT, 1.0),
        ],
    )
    target = EvaluationJob(
        agent_id=OTHER_AGENT_ID,
        results=[
            # 評価セットが違っても、空白をそろえた質問が同じなら対応させる（ID は違ってよい）。
            _judged(_case("b1", " 締め日は？ ", "25 日"), JudgeVerdict.CORRECT, 1.0),
            EvaluationCaseResult(
                case=_case("b2", "承認者は？", "部長"), status=CaseStatus.RUN_FAILED
            ),
            _judged(_case("b3", "経費は？", "50 万円"), JudgeVerdict.CORRECT, 0.8),
            _judged(_case("b5", "後だけの質問", "y"), JudgeVerdict.INCORRECT, 0.0),
        ],
    )

    comparison = evaluation.compare_jobs(base, target)

    assert [row.change for row in comparison.rows] == [
        "improved",
        "regressed",
        "unchanged",
        "base_only",
        "target_only",
    ]
    assert (
        comparison.improved,
        comparison.regressed,
        comparison.unchanged,
        comparison.base_only,
        comparison.target_only,
    ) == (1, 1, 1, 1, 1)
    first = comparison.rows[0]
    assert first.base is not None and first.target is not None
    assert (first.base.case_id, first.base.status, first.base.passed) == ("a1", "incorrect", False)
    assert (first.target.case_id, first.target.status, first.target.score) == ("b1", "correct", 1.0)
    regressed = comparison.rows[1]
    assert regressed.target is not None and regressed.target.status == "run_failed"
    assert comparison.rows[3].target is None and comparison.rows[4].base is None
    assert comparison.base.agent_id == AGENT_ID and comparison.target.agent_id == OTHER_AGENT_ID


@pytest.fixture
def other_agent(agent: None) -> Iterator[None]:
    del agent
    runtime_repository.create_agent(
        AgentProfile(
            id=OTHER_AGENT_ID,
            name="経理の Agent（新しいスキル）",
            instructions="経理の質問に答える。",
            skill_ids=[SKILL_ID],
        )
    )
    try:
        yield
    finally:
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent(OTHER_AGENT_ID)


def test_set_can_be_evaluated_with_another_agent_and_compared(
    other_agent: None, no_background: list[str]
) -> None:
    """評価セットをほかの業務 Agent で評価し、2 つの job を API で比べる（#1845）。"""
    del other_agent
    set_id = client.post("/api/evaluation-sets", json=SET_BODY).json()["data"]["id"]
    started = client.post(
        "/api/evaluations",
        json={"set_id": set_id, "agent_id": OTHER_AGENT_ID, "agent_version": "draft"},
    )
    assert started.status_code == 202, started.text
    job = started.json()["data"]
    assert (job["agent_id"], job["set_id"]) == (OTHER_AGENT_ID, set_id)
    assert job["agent_name"] == "経理の Agent（新しいスキル）"
    assert no_background == [job["id"]]
    evaluation_store.cancel(job["id"])
    missing = client.post("/api/evaluations", json={"set_id": set_id, "agent_id": "no-such-agent"})
    assert missing.status_code in {403, 404}

    case = _case("c1", "締め日は？", "25 日")
    base = _completed(AGENT_ID, _judged(case, JudgeVerdict.INCORRECT, 0.0), set_id="evset_y")
    target = _completed(OTHER_AGENT_ID, _judged(case, JudgeVerdict.CORRECT, 1.0), set_id="evset_y")
    compared = client.get(
        "/api/evaluations/compare",
        params={"base_job_id": base.id, "target_job_id": target.id},
    )
    assert compared.status_code == 200, compared.text
    data = compared.json()["data"]
    assert (data["improved"], data["regressed"]) == (1, 0)
    assert data["rows"][0]["change"] == "improved"
    assert (data["base"]["agent_id"], data["target"]["agent_id"]) == (AGENT_ID, OTHER_AGENT_ID)
    same = client.get(
        "/api/evaluations/compare", params={"base_job_id": base.id, "target_job_id": base.id}
    )
    assert same.status_code == 422
    unknown = client.get(
        "/api/evaluations/compare", params={"base_job_id": base.id, "target_job_id": "eval_none"}
    )
    assert unknown.status_code == 404
    # 前回との比較は同じ業務 Agent の job だけ（ほかの業務 Agent の評価を「前回」にしない）。
    latest = _completed(OTHER_AGENT_ID, _judged(case, JudgeVerdict.CORRECT, 1.0), set_id="evset_y")
    assert evaluation_store.with_previous(latest).previous_job_id == target.id
    assert evaluation_store.with_previous(target).previous_job_id is None


# ---- 問い返しへの答え（#1845） -------------------------------------------------------------

_NL2SQL_PENDING = {
    "kind": "nl2sql",
    "connection": "nl2sql",
    "condition_id": "req-table-1",
    "label": "対象の表",
    "question": "どの表の売上を使いますか？",
    "options": ["受注（SALES_ORDERS）", "請求（INVOICES）"],
    "candidates": [
        {"id": "cand-orders", "label": "受注（SALES_ORDERS）"},
        {"id": "cand-invoices", "label": "請求（INVOICES）"},
    ],
    "requirement_session_id": "sess-1",
}
_RAG_PENDING = {
    "kind": "rag",
    "condition_id": "account_type",
    "label": "アカウントの種類",
    "question": "どのアカウントの種類ですか？",
    "options": ["個別", "共有"],
}


def _pending_run(*pending: dict[str, Any]) -> Any:
    from app.features.agent.runtime import RunState
    from app.features.agent.support_task import SUPPORT_TASK_KIND

    return RunState(
        id="run_pending",
        goal="今月の売上は？",
        agent_id=AGENT_ID,
        runtime_id="builtin",
        status=RunStatus.COMPLETED,
        artifacts=[
            Artifact(
                name="支援タスク",
                kind=SUPPORT_TASK_KIND,
                content={"pending_clarifications": list(pending)},
            )
        ],
    )


def _answer(item_id: str = "", options: list[str] | None = None, other: str = "") -> Any:
    return evaluation.EvaluationClarificationAnswer(
        item_id=item_id, option_ids=options or [], other_text=other
    )


def test_clarification_answers_follow_the_shared_answer_shape() -> None:
    """答えは共通の ClarificationCard と同じ形（item_id / option_ids / other_text）。"""
    answer = _answer(" req-table-1 ", [" cand-orders ", "cand-orders"], " ")
    assert (answer.item_id, answer.option_ids, answer.other_text) == (
        "req-table-1",
        ["cand-orders"],
        "",
    )
    with pytest.raises(ValueError, match="候補か自由入力"):
        _answer("req-table-1")
    case = EvaluationCase.model_validate(
        {"question": "q", "expected": "x", "clarification_answers": [{"option_ids": ["個別"]}]}
    )
    assert case.clarification_answers[0].item_id == ""


def test_clarification_answers_match_items_by_id_then_in_order() -> None:
    case = EvaluationCase(
        question="今月の売上は？",
        expected="300 万円",
        clarification_answers=[
            _answer(options=["共有"]),
            # 候補の表示名でも当てる（NL2SQL の候補の id は質問ごとに作られる）。
            _answer("req-table-1", ["受注（SALES_ORDERS）"]),
            _answer(other="使わない"),
        ],
    )
    used: set[int] = set()
    answered = evaluation.answer_clarifications(
        case, _pending_run(_NL2SQL_PENDING, _RAG_PENDING), used
    )
    assert answered is not None
    assert answered.run_id == "run_pending"
    assert [item.item_id for item in answered.questions] == ["req-table-1", "account_type"]
    assert [item.kind for item in answered.questions] == ["nl2sql", "rag"]
    assert answered.message == "対象の表: 受注（SALES_ORDERS）\nアカウントの種類: 共有"
    assert used == {0, 1}
    # 使った答えは次の往復で使わない。残りの item_id の無い答え（自由入力）を当てる。
    again = evaluation.answer_clarifications(case, _pending_run(_RAG_PENDING), used)
    assert again is not None and again.message == "アカウントの種類: 使わない"
    assert evaluation.answer_clarifications(case, _pending_run(_RAG_PENDING), used) is None
    # 候補の id は表示名にして送る。問い返しが無ければ None。
    by_id = EvaluationCase(
        question="q", expected="x", clarification_answers=[_answer(options=["cand-invoices"])]
    )
    answered_by_id = evaluation.answer_clarifications(by_id, _pending_run(_NL2SQL_PENDING), set())
    assert answered_by_id is not None and answered_by_id.message == "対象の表: 請求（INVOICES）"
    assert evaluation.answer_clarifications(by_id, _pending_run(), set()) is None


def _patch_pending(monkeypatch: MonkeyPatch, pending_for: dict[str, dict[str, Any]]) -> None:
    """Run の質問（goal）ごとに、支援タスクの確かめ中の問いを差し替える。"""

    def fake(run: Any) -> dict[str, Any] | None:
        item = pending_for.get(run.goal)
        return {"pending_clarifications": [item]} if item else None

    monkeypatch.setattr(evaluation, "run_support_task", fake)


def test_case_answers_the_clarification_and_judges_the_last_answer(
    monkeypatch: MonkeyPatch, agent: None
) -> None:
    """問い返しで止まったら、答えを同じ会話の次の Run で送り、最後の回答を判定する（#1845）。"""
    del agent
    model = _script(
        monkeypatch,
        [assistant_message("どの表の売上を使いますか？ 受注（SALES_ORDERS）/ 請求（INVOICES）")],
        [assistant_message("今月の売上は 300 万円です。")],
        _judgement("correct", 1.0, "要点を満たしています。"),
    )
    _patch_pending(monkeypatch, {"今月の売上は？": _NL2SQL_PENDING})
    case = EvaluationCase(
        id="clarified",
        question="今月の売上は？",
        expected="300 万円",
        clarification_answers=[_answer(options=["cand-orders"])],
    )
    job = _job(case)

    anyio.run(run_evaluation_job, job.id)

    [result] = evaluation_store.get(job.id).results
    assert result.status == CaseStatus.JUDGED
    assert result.judgement is not None and result.judgement.verdict == JudgeVerdict.CORRECT
    assert result.answer == "今月の売上は 300 万円です。"
    assert result.ended_with_clarification is False
    [round_] = result.clarification_rounds
    assert round_.message == "対象の表: 受注（SALES_ORDERS）"
    # 画面の共通の ClarificationRecord に出す、確認事項ごとの答え（候補の id は表示名。#1861）。
    [question] = round_.questions
    assert question.question == "どの表の売上を使いますか？"
    assert question.selected == ["受注（SALES_ORDERS）"]
    assert question.other_text == ""
    first = runtime_repository.get_run(round_.run_id)
    last = runtime_repository.get_run(str(result.run_id))
    assert last.id != first.id
    # 同じ会話（thread）の続きとして、同じ利用者・同じ評価の Run で送る。
    assert last.thread_id == first.thread_id
    assert last.goal == "対象の表: 受注（SALES_ORDERS）"
    assert last.created_by_user_uuid == USER_UUID
    assert last.metadata["evaluation_job_id"] == job.id
    assert last.metadata["evaluation_clarification_round"] == 1
    assert last.metadata["evaluation_dry_run"] is True
    judge_prompt = str(model.calls[2].input)
    assert "問い返しへの利用者の答え（1 回目）" in judge_prompt
    assert "対象の表: 受注（SALES_ORDERS）" in judge_prompt


def test_case_without_matching_answer_is_judged_as_it_is(
    monkeypatch: MonkeyPatch, agent: None
) -> None:
    """答えが評価ケースに無い問い返しは、そのまま判定して「問い返しで終わった」を残す。"""
    del agent
    _script(
        monkeypatch,
        [assistant_message("どの期間の傾向を見ますか？")],
        _judgement("correct", 1.0, "あいまいな質問を確かめています。"),
    )
    _patch_pending(monkeypatch, {"売上の傾向は？": _RAG_PENDING})
    job = _job(_case("vague", "売上の傾向は？", "期間を確かめる"))

    anyio.run(run_evaluation_job, job.id)

    [result] = evaluation_store.get(job.id).results
    assert result.status == CaseStatus.JUDGED
    assert result.clarification_rounds == []
    assert result.ended_with_clarification is True


def test_clarification_rounds_stop_at_the_limit(monkeypatch: MonkeyPatch, agent: None) -> None:
    """往復は EVALUATION_CLARIFICATION_ROUNDS_MAX 回まで（同じ問いを聞き続ける Agent で止まる）。"""
    del agent
    _script(
        monkeypatch,
        [assistant_message("どの表ですか？")],
        [assistant_message("どの表ですか？（2 回目）")],
        [assistant_message("どの表ですか？（3 回目）")],
        [assistant_message("どの表ですか？（4 回目）")],
        _judgement("incorrect", 0.0, "答えていません。"),
    )

    def fake(_run: Any) -> dict[str, Any]:
        return {"pending_clarifications": [_NL2SQL_PENDING]}

    monkeypatch.setattr(evaluation, "run_support_task", fake)
    case = EvaluationCase(
        question="今月の売上は？",
        expected="300 万円",
        clarification_answers=[_answer(options=["cand-orders"]) for _ in range(4)],
    )
    job = _job(case)

    anyio.run(run_evaluation_job, job.id)

    [result] = evaluation_store.get(job.id).results
    assert len(result.clarification_rounds) == evaluation.EVALUATION_CLARIFICATION_ROUNDS_MAX == 3
    assert result.ended_with_clarification is True
    assert result.answer == "どの表ですか？（4 回目）"


def test_clarification_answers_change_the_case_fingerprint() -> None:
    base = EvaluationCase(question="q", expected="x")
    answered = base.model_copy(update={"clarification_answers": [_answer(options=["a"])]})
    other = base.model_copy(update={"clarification_answers": [_answer(options=["b"])]})
    fingerprints = {
        evaluation.cases_fingerprint(
            EvaluationJob(agent_id=AGENT_ID, results=[EvaluationCaseResult(case=item)])
        )
        for item in (base, answered, other)
    }
    assert len(fingerprints) == 3


def test_clarification_answers_round_trip_through_excel() -> None:
    """Excel の「問い返しへの答え」の列（1 行 1 件。#1845）。"""
    from app.features.agent.evaluation_excel import format_answers, parse_answers

    answers = [
        _answer("req-table-1", ["受注（SALES_ORDERS）", "cand-orders"]),
        _answer(other="今月の確定分だけ"),
        _answer("account_type", ["個別"], "検証用"),
    ]
    text = format_answers(answers)
    assert text == (
        "req-table-1 = 受注（SALES_ORDERS）, cand-orders\n"
        "その他: 今月の確定分だけ\n"
        "account_type = 個別 その他: 検証用"
    )
    assert parse_answers(text) == answers
    # 全角の「＝」「、」「その他：」も読む。ID の無い行は順に当てる答え。
    assert parse_answers("req-table-1 ＝ 受注、請求\nその他：月末") == [
        _answer("req-table-1", ["受注", "請求"]),
        _answer(other="月末"),
    ]
    assert parse_answers("") == []


# ---- 上限つきの並行（#2004） --------------------------------------------------------------


class _HttpError(Exception):
    """openai の APIStatusError と同じく ``status_code`` と ``response.headers`` を持つスタブ。"""

    def __init__(self, status: int) -> None:
        super().__init__(f"Error code: {status}")
        self.status_code = status
        self.response = type("Resp", (), {"status_code": status, "headers": {}})()


class _RoutedModel(ScriptedModel):
    """業務 Agent の回答と判定を、質問・指示で返し分ける（並行でも順番に依存しない）。

    呼び出しごとに少し待ち、同時に動いた数を数える。``judge_errors`` は判定の最初の呼び出しから
    順に出す例外。
    """

    def __init__(self, answers: dict[str, str], judge_errors: list[Exception]) -> None:
        super().__init__([])
        self.answers = answers
        self.judge_errors = judge_errors
        self.in_flight = {"answer": 0, "judge": 0}
        self.peak = {"answer": 0, "judge": 0}
        self.counts = {"answer": 0, "judge": 0}
        self.delay = 0.02

    async def get_response(
        self, system_instructions: Any, input: Any, *args: Any, **kwargs: Any
    ) -> Any:
        role = "judge" if system_instructions == evaluation.JUDGE_INSTRUCTIONS else "answer"
        self.counts[role] += 1
        self.in_flight[role] += 1
        self.peak[role] = max(self.peak[role], self.in_flight[role])
        try:
            await anyio.sleep(self.delay)
            if role == "judge":
                if self.judge_errors:
                    raise self.judge_errors.pop(0)
                step: Any = _judgement("correct", 1.0, "要点を満たしています。")
            else:
                text = json.dumps(input, ensure_ascii=False)
                answer = next(
                    (value for key, value in self.answers.items() if key in text), "分かりません。"
                )
                step = [assistant_message(answer)]
            return await ScriptedModel([step]).get_response(
                system_instructions, input, *args, **kwargs
            )
        finally:
            self.in_flight[role] -= 1


def _routed(
    monkeypatch: MonkeyPatch, answers: dict[str, str], judge_errors: list[Exception]
) -> tuple[_RoutedModel, list[ModelTarget]]:
    model = _RoutedModel(answers, judge_errors)
    targets: list[ModelTarget] = []
    _script(monkeypatch)

    def factory(target: ModelTarget) -> Any:
        targets.append(target)
        return model

    monkeypatch.setattr(builtin_runtime, "model_factory", factory)

    async def no_wait(_seconds: float) -> None:
        await anyio.sleep(0)

    monkeypatch.setattr(ai_calls, "_sleep", no_wait)
    return model, targets


def test_cases_run_with_bounded_concurrency_and_429_waits_globally(
    monkeypatch: MonkeyPatch, agent: None
) -> None:
    """並行の上限を超えず、429 は全体で待って並行の数を下げ、結果を失わずケースの順に残す。"""
    del agent
    monkeypatch.setattr(evaluation, "evaluation_concurrency", lambda: 2)
    callers: list[Any] = []
    original = ai_calls.make_ai_caller

    def capture(stage: str, *, max_concurrency: int) -> Any:
        caller = original(stage, max_concurrency=max_concurrency)
        callers.append(caller)
        return caller

    monkeypatch.setattr(evaluation, "make_ai_caller", capture)
    answers = {f"質問{number}": f"回答{number}" for number in range(1, 5)}
    model, targets = _routed(monkeypatch, answers, [_HttpError(429), _HttpError(429)])
    job = _job(*(_case(f"c{number}", f"質問{number}", f"回答{number}") for number in range(1, 5)))

    anyio.run(run_evaluation_job, job.id)

    finished = evaluation_store.get(job.id)
    assert finished.status == JobStatus.COMPLETED
    assert [item.status for item in finished.results] == [CaseStatus.JUDGED] * 4
    assert [item.answer for item in finished.results] == ["回答1", "回答2", "回答3", "回答4"]
    # 上限（2）まで同時に実行し、超えない。
    assert model.peak["answer"] == 2
    assert model.peak["judge"] <= 2
    # 429 の 2 回は実行器が再試行し（SDK の再試行は切る）、並行の数を下げた。
    assert model.counts["judge"] == 6
    (caller,) = callers
    assert caller.retries == 2
    assert caller.status().concurrency == 1
    # 判定の呼び出し（6 回）だけ SDK の再試行を切り、業務 Agent の Run は SDK の既定のまま。
    assert sorted(str(target.sdk_max_retries) for target in targets) == ["0"] * 6 + ["None"] * 4


def test_permission_error_of_judge_stops_job(monkeypatch: MonkeyPatch, agent: None) -> None:
    """判定のモデルの権限の誤り（403）は再試行せず、残りのケースを始めずに job を止める。"""
    del agent
    model, _targets = _routed(monkeypatch, {"質問1": "回答1"}, [_HttpError(403)])
    job = _job(*(_case(f"c{number}", f"質問{number}", f"回答{number}") for number in range(1, 4)))

    anyio.run(run_evaluation_job, job.id)

    finished = evaluation_store.get(job.id)
    assert finished.status == JobStatus.FAILED
    assert finished.error and "権限" in finished.error
    first, second, third = finished.results
    assert first.status == CaseStatus.JUDGE_FAILED
    assert first.error and "権限" in first.error
    assert (second.status, third.status) == (CaseStatus.CANCELLED, CaseStatus.CANCELLED)
    assert model.counts == {"answer": 1, "judge": 1}


def test_judge_retry_lowers_reasoning_effort_when_the_limit_cannot_widen(
    monkeypatch: MonkeyPatch,
) -> None:
    """広げられないときは、推論の量を 1 段下げて送り直す（#2023。low は下げない）。"""
    monkeypatch.setattr(ai_calls, "enterprise_ai_retry_output_token_budget", lambda *_a, **_k: None)

    def target(effort: str) -> ModelTarget:
        return ModelTarget(
            model_id="judge-model",
            endpoint="https://llm.example",
            project_id="",
            api_key="k",
            reasoning_effort=effort,
        )

    assert retry_model_target(target("medium")).reasoning_effort == "low"  # noqa: SLF001
    assert retry_model_target(target("low")).reasoning_effort == "low"  # noqa: SLF001


# ---- 出力の項目の判定（#2558） ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("expected", "actual", "field_type", "matches"),
    [
        ("Ａ－１", "a-1", "text", True),
        ("品目  甲", "品目 甲", "text", True),
        ("品目甲", "品目乙", "text", False),
        (1200, "1,200", "number", True),
        (1200, 1200.0, "number", True),
        (1200, "1,201", "number", False),
        ("2026-10-01", "2026-10-01", "date", True),
        ("2026-10-01", "2026-10-02", "date", False),
        (True, "はい", "boolean", True),
        (False, True, "boolean", False),
        (None, None, "text", True),
        (None, "値", "text", False),
        ("値", None, "text", False),
    ],
    ids=[
        "text-width-case",
        "text-space",
        "text-diff",
        "number-separator",
        "number-float",
        "number-diff",
        "date-same",
        "date-diff",
        "bool-japanese",
        "bool-diff",
        "null-null",
        "null-expected",
        "missing-actual",
    ],
)
def test_output_value_matches(expected: Any, actual: Any, field_type: str, matches: bool) -> None:
    assert output_value_matches(expected, actual, field_type) is matches


OUTPUT_CONTENT = {
    "columns": [
        {"name": "item", "label": "項目", "type": "text"},
        {"name": "amount", "label": "金額", "type": "number"},
        {"name": "matched", "label": "一致", "type": "boolean"},
    ],
    "rows": [
        {"item": "B", "amount": 800, "matched": False},
        {"item": "A", "amount": 1200, "matched": True},
    ],
}


def test_compare_output_pairs_rows_by_first_column() -> None:
    results, correct = compare_output(
        [{"item": "A", "amount": 1200}, {"item": "B", "matched": True}], OUTPUT_CONTENT
    )

    assert correct is False
    assert [(item.row, item.field, item.actual, item.correct) for item in results] == [
        (1, "item", "A", True),
        (1, "amount", 1200, True),
        (2, "item", "B", True),
        (2, "matched", False, False),
    ]
    assert results[1].label == "金額"
    # 期待する出力が無ければ判定しない。記録が無ければすべて誤り。
    assert compare_output([], OUTPUT_CONTENT) == ([], None)
    missing, missing_correct = compare_output([{"item": "A"}], None)
    assert missing_correct is False and missing[0].actual is None


def test_output_fields_are_judged_in_a_job(monkeypatch: MonkeyPatch, agent: None) -> None:
    del agent
    runtime_repository.patch_agent(
        AGENT_ID,
        AgentProfilePatch(
            output_fields=[
                AgentOutputField(key="item", label="項目", required=True),
                AgentOutputField(key="amount", label="金額", type="number"),
            ]
        ),
    )
    _script(
        monkeypatch,
        [
            function_call(
                RECORD_OUTPUT_TOOL,
                {"rows": [{"item": "A", "amount": 1200}, {"item": "B", "amount": 900}]},
                call_id="call-out",
            )
        ],
        [assistant_message("A は 1200、B は 900 です。")],
        _judgement("correct", 1.0, "要点を満たしています。"),
    )
    case = EvaluationCase(
        id="c1",
        question="書類の値を読んで",
        expected="A と B の金額",
        expected_output=[{"item": "A", "amount": "1,200"}, {"item": "B", "amount": 800}],
    )
    job = evaluation_store.create(
        EvaluationJob(
            agent_id=AGENT_ID,
            agent_name="経理の Agent",
            set_id="evset_output",
            set_name="出力の評価",
            agent_version="draft",
            created_by_user_uuid=USER_UUID,
            results=[EvaluationCaseResult(case=case)],
        )
    )

    anyio.run(run_evaluation_job, job.id)

    finished = evaluation_store.get(job.id)
    [result] = finished.results
    assert result.status == CaseStatus.JUDGED
    assert result.field_correct is False
    assert [(item.row, item.field, item.correct) for item in result.field_results] == [
        (1, "item", True),
        (1, "amount", True),
        (2, "item", True),
        (2, "amount", False),
    ]
    summary = finished.summary
    assert (summary.field_cases, summary.field_correct, summary.field_accuracy) == (1, 0, 0.0)
    assert (summary.field_items, summary.field_items_correct) == (4, 3)
    assert summary.field_item_accuracy == pytest.approx(0.75)
    # 合格率は回答の文の判定だけで決める。
    assert summary.pass_rate == 1.0


def test_expected_output_changes_the_case_fingerprint() -> None:
    base = EvaluationCase(id="c1", question="質問", expected="要点")
    with_output = base.model_copy(update={"expected_output": [{"item": "A"}]})
    first = EvaluationJob(
        agent_id=AGENT_ID,
        agent_name="a",
        set_id="s",
        set_name="s",
        results=[EvaluationCaseResult(case=base)],
    )
    second = first.model_copy(update={"results": [EvaluationCaseResult(case=with_output)]})
    assert cases_fingerprint(first) != cases_fingerprint(second)


def test_expected_output_is_validated() -> None:
    with pytest.raises(ValueError, match="key"):
        EvaluationCase(id="c", question="q", expected="e", expected_output=[{"Item": "A"}])
    with pytest.raises(ValueError):
        EvaluationCase(id="c", question="q", expected="e", expected_output=[{"item": ["A"]}])


def test_expected_output_round_trips_through_excel(agent: None) -> None:
    del agent
    body = {
        **SET_BODY,
        "cases": [
            {
                "question": "書類の値は？",
                "expected": "金額",
                "expected_output": [{"item": "A", "amount": 1200, "matched": True}],
            }
        ],
    }
    set_id = client.post("/api/evaluation-sets", json=body).json()["data"]["id"]
    exported = client.get(f"/api/evaluation-sets/{set_id}/cases.xlsx")
    [case] = parse_cases_xlsx(exported.content)
    assert case.expected_output == [{"item": "A", "amount": 1200, "matched": True}]


# ---- 意味で比べる文字の項目（#2562） -------------------------------------------------------


def _meaning(*items: tuple[int, bool]) -> Any:
    return [
        assistant_message(
            json.dumps(
                {
                    "items": [
                        {"number": number, "same": same, "reason": f"理由 {number}"}
                        for number, same in items
                    ]
                },
                ensure_ascii=False,
            )
        )
    ]


def test_match_meaning_is_only_for_text_fields() -> None:
    assert AgentOutputField(key="note", label="メモ", match="meaning").match == "meaning"
    number = AgentOutputField(key="amount", label="金額", type="number", match="meaning")
    assert number.match == "exact"
    content = output_content([number], [{"amount": 1}])
    assert content["columns"][0]["match"] == "exact"


def test_text_fields_are_judged_by_meaning_in_a_job(monkeypatch: MonkeyPatch, agent: None) -> None:
    del agent
    runtime_repository.patch_agent(
        AGENT_ID,
        AgentProfilePatch(
            output_fields=[
                AgentOutputField(key="item", label="項目", required=True),
                AgentOutputField(key="code", label="記号"),
                AgentOutputField(key="reason", label="確かめたこと", match="meaning"),
            ]
        ),
    )
    model = _script(
        monkeypatch,
        [
            function_call(
                RECORD_OUTPUT_TOOL,
                {
                    "rows": [
                        {"item": "A", "code": "X-1", "reason": "台帳と書類の金額が同じ"},
                        {"item": "B", "code": "Y-2", "reason": "期日は同じ"},
                        {"item": "C", "code": "Z-3", "reason": "一致"},
                    ]
                },
                call_id="call-out",
            )
        ],
        [assistant_message("A・B・C を確かめました。")],
        _meaning((1, True), (2, False)),
        _judgement("correct", 1.0, "要点を満たしています。"),
    )
    case = EvaluationCase(
        id="c1",
        question="書類の値を確かめて",
        expected="A・B・C の結果",
        expected_output=[
            {"item": "A", "code": "X-1", "reason": "書類と台帳で金額が一致した"},
            {"item": "B", "code": "Y-9", "reason": "期日が違う"},
            {"item": "C", "reason": "一致"},
        ],
    )
    job = evaluation_store.create(
        EvaluationJob(
            agent_id=AGENT_ID,
            agent_name="Agent A",
            set_id="evset_meaning",
            set_name="意味の評価",
            agent_version="draft",
            created_by_user_uuid=USER_UUID,
            results=[EvaluationCaseResult(case=case)],
        )
    )

    anyio.run(run_evaluation_job, job.id)

    [result] = evaluation_store.get(job.id).results
    assert result.status == CaseStatus.JUDGED
    judged = {(item.row, item.field): item for item in result.field_results}
    # 言い換えは意味で正しく、違う内容は誤り。文字が一致した項目は意味の判定に渡さない。
    assert judged[(1, "reason")].correct is True
    assert judged[(1, "reason")].method == "meaning"
    # 意味で判定した項目には理由を残す（#2568）。文字で一致した項目は判定に渡さないので理由は無い。
    assert judged[(1, "reason")].reason == "理由 1"
    assert judged[(2, "reason")].reason == "理由 2"
    assert judged[(3, "reason")].reason == ""
    assert judged[(2, "reason")].correct is False
    assert judged[(3, "reason")].correct is True
    # 完全に一致で比べる項目は、似ていても誤り。
    assert judged[(2, "code")].correct is False
    assert judged[(2, "code")].method == "exact"
    assert result.field_correct is False
    meaning_input = json.dumps(model.calls[2].input, ensure_ascii=False)
    assert "書類と台帳で金額が一致した" in meaning_input and "期日は同じ" in meaning_input
    assert "Y-9" not in meaning_input and "## 項目 3" not in meaning_input
    assert len(model.calls) == 4


def test_exact_fields_do_not_call_the_model(monkeypatch: MonkeyPatch) -> None:
    model = _script(monkeypatch)
    results = [
        OutputFieldResult(row=1, field="code", expected="X-1", actual="X-2", correct=False),
        OutputFieldResult(
            row=1, field="reason", expected="値", actual=None, correct=False, method="meaning"
        ),
    ]

    assert anyio.run(judge_output_meaning, results) == results
    assert not model.calls


def test_meaning_failure_keeps_the_text_result(monkeypatch: MonkeyPatch) -> None:
    async def fail(*_args: Any) -> Any:
        raise RuntimeError("model down")

    monkeypatch.setattr(evaluation, "_judge_meaning_once", fail)
    _script(monkeypatch)
    results = [
        OutputFieldResult(
            row=1, field="reason", expected="甲", actual="乙", correct=False, method="meaning"
        )
    ]

    [item] = anyio.run(judge_output_meaning, results)

    assert item.correct is False
    assert item.note == evaluation.MEANING_FAILED_NOTE


def test_summary_counts_the_cases_behind_the_pass_rate() -> None:
    """合格率の分母になったケースの数（#2569）。判定できなかったケースは数えない。"""
    case = EvaluationCase(id="c", question="q", expected="e")
    results = [
        _judged(case, JudgeVerdict.CORRECT, 1.0),
        _judged(case, JudgeVerdict.INCORRECT, 0.0),
        EvaluationCaseResult(case=case, status=CaseStatus.RUN_FAILED),
        EvaluationCaseResult(case=case, status=CaseStatus.JUDGE_FAILED),
    ]

    summary = evaluation.summarize(results)

    assert summary.judged_cases == 3
    assert summary.pass_rate == pytest.approx(1 / 3)


@pytest.mark.parametrize("calls_tool", [True, False])
def test_explicit_approval_expectation_is_checked_without_judge(
    monkeypatch: MonkeyPatch, agent: None, calls_tool: bool
) -> None:
    del agent
    responses = (
        (
            [function_call(WRITE, {"query": "合成の入力"}, call_id="approval-case")],
            [assistant_message("合成の回答")],
        )
        if calls_tool
        else ([assistant_message("合成の回答")],)
    )
    _script(monkeypatch, *responses)
    case = EvaluationCase(
        question="合成の質問",
        expected="承認による停止",
        expected_tools=[WRITE],
        expected_approval=True,
    )
    job = _job(case)
    anyio.run(run_evaluation_job, job.id)
    [result] = evaluation_store.get(job.id).results
    assert result.status == CaseStatus.JUDGED
    assert result.judgement is not None
    assert (result.judgement.verdict == JudgeVerdict.CORRECT) is calls_tool
    assert result.judge_attempts == 0
    assert result.executed_tool_calls == []


@pytest.mark.parametrize("status", [CaseStatus.APPROVAL_BLOCKED, CaseStatus.DEFINITION_CHANGED])
def test_unassessed_case_is_not_a_regression(status: CaseStatus) -> None:
    case = EvaluationCase(question="合成の質問", expected="合成の回答")
    blocked = EvaluationCaseResult(case=case, status=status, blocked_tool_calls=["synthetic_tool"])
    summary = evaluation.summarize([blocked])
    assert (summary.judged_cases, summary.pass_rate) == (0, None)
    assert summary.approval_blocked == int(status == CaseStatus.APPROVAL_BLOCKED)
    correct = _judged(case, JudgeVerdict.CORRECT, 1)
    for before, after in [(correct, blocked), (blocked, correct)]:
        comparison = evaluation.compare_jobs(
            EvaluationJob(agent_id=AGENT_ID, results=[before]),
            EvaluationJob(agent_id=AGENT_ID, results=[after]),
        )
        assert comparison.rows[0].change == "not_comparable"
        assert comparison.improved == comparison.regressed == 0


def test_approval_expectation_roundtrips_and_changes_fingerprint() -> None:
    from app.features.agent.evaluation_excel import export_set_xlsx

    case = EvaluationCase(question="合成の質問", expected="合成の回答", expected_approval=True)
    evaluation_set = evaluation.EvaluationSet(
        agent_id=AGENT_ID, name="合成の評価セット", cases=[case]
    )
    [parsed] = parse_cases_xlsx(export_set_xlsx(evaluation_set))
    assert parsed.expected_approval is True
    old = EvaluationJob(
        agent_id=AGENT_ID,
        results=[EvaluationCaseResult(case=case.model_copy(update={"expected_approval": False}))],
    )
    new = EvaluationJob(agent_id=AGENT_ID, results=[EvaluationCaseResult(case=case)])
    assert cases_fingerprint(old) != cases_fingerprint(new)


def test_output_fingerprint_orders_mixed_json_values_without_type_comparison() -> None:
    cases = [
        EvaluationCase(
            id=f"synthetic-{index}",
            question="合成の質問",
            expected="合成の回答",
            expected_output=[{"value": value, "other": "同じ値"}],
        )
        for index, value in enumerate([None, 1, "1", True])
    ]

    def job(items: list[EvaluationCase]) -> EvaluationJob:
        return EvaluationJob(
            agent_id=AGENT_ID, results=[EvaluationCaseResult(case=case) for case in items]
        )

    original = cases_fingerprint(job(cases))
    reordered = [
        case.model_copy(
            update={
                "expected_output": [
                    dict(reversed(list(row.items()))) for row in case.expected_output
                ]
            }
        )
        for case in reversed(cases)
    ]
    assert original == cases_fingerprint(job(reordered))
    changed = [
        *cases[:-1],
        cases[-1].model_copy(update={"expected_output": [{"value": False, "other": "同じ値"}]}),
    ]
    assert original != cases_fingerprint(job(changed))


@pytest.mark.parametrize("when", ["queued", "between_cases", "during_judge"])
def test_draft_change_invalidates_the_entire_job(
    monkeypatch: MonkeyPatch, agent: None, when: str
) -> None:
    del agent
    _script(
        monkeypatch,
        [assistant_message("合成の回答")],
        _judgement("correct", 1, "合成の判定"),
        [assistant_message("合成の回答")],
        _judgement("correct", 1, "合成の判定"),
    )
    profile = next(item for item in runtime_repository.list_agents() if item.id == AGENT_ID)
    job = evaluation_store.update(
        _job(
            _case("first", "合成の質問1", "合成の回答", ["synthetic_tool"]),
            _case("second", "合成の質問2", "合成の回答"),
        ).id,
        agent_version="draft",
        definition_hash=evaluation.draft_definition_hash(profile),
    )

    def change() -> None:
        runtime_repository.patch_agent(
            AGENT_ID, AgentProfilePatch(instructions="変更後の合成の指示")
        )

    if when == "queued":
        change()
    elif when == "during_judge":
        original = evaluation.judge_answer

        async def judge(*args: Any, **kwargs: Any) -> Any:
            result = await original(*args, **kwargs)
            change()
            return result

        monkeypatch.setattr(evaluation, "judge_answer", judge)
    else:
        original_settle = evaluation._settle_run
        calls = 0

        async def settle(*args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 2:
                change()
            return await original_settle(*args, **kwargs)

        monkeypatch.setattr(evaluation, "_settle_run", settle)
    anyio.run(run_evaluation_job, job.id)
    final = evaluation_store.get(job.id)
    assert final.status == JobStatus.FAILED
    assert all(
        item.status == CaseStatus.DEFINITION_CHANGED and item.judgement is None
        for item in final.results
    )
    assert final.summary.pass_rate is None and final.summary.judged_cases == 0
    assert final.summary.approval_blocked == 0
    assert final.summary.tool_accuracy is None
    assert all(item.tool_selection_correct is None for item in final.results)


def test_queued_evaluation_does_not_execute_changed_draft(
    monkeypatch: MonkeyPatch, agent: None
) -> None:
    del agent
    _script(monkeypatch)
    profile = next(item for item in runtime_repository.list_agents() if item.id == AGENT_ID)
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(
            agent_id=AGENT_ID,
            goal="合成の質問",
            draft=True,
            metadata={"_evaluation_definition_hash": evaluation.draft_definition_hash(profile)},
        )
    )
    runtime_repository.patch_agent(AGENT_ID, AgentProfilePatch(instructions="変更後の合成の指示"))
    assert runtime_repository.begin_builtin_run(run.id) is None
    assert runtime_repository.get_run(run.id).status == RunStatus.FAILED


def test_definition_hash_tracks_skills_output_and_model_settings(
    monkeypatch: MonkeyPatch, agent: None
) -> None:
    del agent
    _script(monkeypatch)
    profile = next(item for item in runtime_repository.list_agents() if item.id == AGENT_ID)
    original = evaluation.draft_definition_hash(profile)
    skill = skill_registry.get(SKILL_ID)
    assert skill is not None
    skill_registry.upsert_custom(skill.model_copy(update={"instructions": "変更後の合成の指示"}))
    assert evaluation.draft_definition_hash(profile) != original
    output = profile.model_copy(
        update={"output_fields": [AgentOutputField(key="synthetic", label="合成の項目")]}
    )
    assert evaluation.draft_definition_hash(output) != evaluation.draft_definition_hash(profile)
    before = evaluation.draft_definition_hash(profile)
    monkeypatch.setattr(
        builtin_runtime,
        "resolve_model_target",
        lambda model_id="": ModelTarget(
            model_id="different",
            endpoint="https://llm.example",
            project_id="",
            api_key="synthetic-secret",
            max_output_tokens=1234,
        ),
    )
    after = evaluation.draft_definition_hash(profile)
    assert after != before and "synthetic-secret" not in after


def test_definition_change_cancels_other_running_cases(
    monkeypatch: MonkeyPatch, agent: None
) -> None:
    import asyncio

    del agent
    monkeypatch.setattr(evaluation, "evaluation_concurrency", lambda: 2)
    second_started = asyncio.Event()
    cancelled = asyncio.Event()
    runs: list[str] = []
    original_cancel = runtime_repository.cancel_run

    def cancel(run_id: str) -> Any:
        result = original_cancel(run_id)
        cancelled.set()
        return result

    monkeypatch.setattr(runtime_repository, "cancel_run", cancel)

    async def run_case(
        job: Any, index: int, case: Any, store: Any, repository: Any, caller: Any
    ) -> None:
        run = repository.create_builtin_run(
            RunCreateRequest(agent_id=AGENT_ID, goal="合成の質問", draft=True)
        )
        runs.append(run.id)
        store.update_result(job.id, index, status=CaseStatus.RUNNING, run_id=run.id)
        if index == 0:
            await second_started.wait()
            raise evaluation.EvaluationDefinitionChanged("合成の条件変更")
        second_started.set()
        await asyncio.wait_for(cancelled.wait(), timeout=1)

    monkeypatch.setattr(evaluation, "_run_case", run_case)
    job = _job(_case("one", "合成の質問1", "合成の回答"), _case("two", "合成の質問2", "合成の回答"))
    anyio.run(run_evaluation_job, job.id)
    assert all(runtime_repository.get_run(run_id).status == RunStatus.CANCELLED for run_id in runs)
    assert all(
        item.status == CaseStatus.DEFINITION_CHANGED
        for item in evaluation_store.get(job.id).results
    )
