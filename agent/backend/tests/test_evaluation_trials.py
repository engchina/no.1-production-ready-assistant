"""品質評価の反復試行（#62）: ケース × 試行・Pass@k / Pass^k・欠測・対応のある比較・予算・評価者。

業務 Agent の回答と判定のモデルは `ScriptedModel`（台本どおりに応答する）で、モデルの API へは
接続しない。集計の数学（Wilson・MOVER）は platform の `pr_evaluation_scoring` のテストが確かめる。
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from agents.testing import assistant_message
from pytest import MonkeyPatch
from security_support import client
from test_evaluation import (  # noqa: F401 - fixture を使う
    AGENT_ID,
    SET_BODY,
    USER_UUID,
    _case,
    _judged,
    _judgement,
    _script,
    _sequential_cases,
    agent,
    no_background,
)

from app.features.agent import evaluation
from app.features.agent.evaluation import (
    CaseStatus,
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationJob,
    JobStatus,
    JudgeVerdict,
    evaluation_store,
    run_evaluation_job,
)
from app.features.agent.runtime import runtime_repository
from app.settings import get_settings


def _trials(case: EvaluationCase, *outcomes: Any) -> list[EvaluationCaseResult]:
    """1 ケースの試行（True = 正しい・False = 誤り・それ以外はケースの状態）。"""
    results: list[EvaluationCaseResult] = []
    for trial, outcome in enumerate(outcomes, start=1):
        if isinstance(outcome, bool):
            verdict = JudgeVerdict.CORRECT if outcome else JudgeVerdict.INCORRECT
            result = _judged(case, verdict, float(outcome))
        else:
            result = EvaluationCaseResult(case=case, status=outcome)
        results.append(result.model_copy(update={"trial": trial}))
    return results


def test_summary_separates_mean_pass_hat_k_pass_at_k_and_missing() -> None:
    """全試行成功・一部失敗・試行不足・評価者の失敗を、既知の合成データから集計する。"""
    results = [
        *_trials(_case("all", "全部成功", "x"), True, True, True),
        *_trials(_case("flaky", "一部失敗", "x"), True, False, True),
        # 実行の失敗は不合格に数える（欠測ではない）。
        *_trials(_case("fail", "全部失敗", "x"), False, CaseStatus.RUN_FAILED, False),
        # 未完了・取消は欠測（全成功に数えない）。
        *_trials(_case("short", "試行不足", "x"), True, CaseStatus.PENDING, CaseStatus.CANCELLED),
        # 評価者の失敗も欠測（合格率の分母から除く）。
        *_trials(_case("judge", "評価者の失敗", "x"), True, CaseStatus.JUDGE_FAILED, True),
    ]

    summary = evaluation.summarize(results)

    assert (summary.trials, summary.cases, summary.total) == (3, 5, 15)
    # 合格率は判定できた試行の平均（8 / 12）。ケース数と試行数を分けて出す。
    assert (summary.judged_trials, summary.judged_cases, summary.correct) == (12, 5, 8)
    assert summary.pass_rate == pytest.approx(8 / 12)
    # Pass@k / Pass^k の分母は k 回すべてを判定できたケース（all・flaky・fail）。
    assert (summary.complete_cases, summary.incomplete_cases, summary.missing_trials) == (3, 2, 3)
    assert summary.pass_hat_k == pytest.approx(1 / 3)
    assert summary.pass_at_k == pytest.approx(2 / 3)
    assert (summary.stable_cases, summary.unstable_cases) == (2, 1)
    # 区間（単位はケース）は 0〜1 に収まり、点推定を含む。
    for rate, interval in [
        (summary.pass_rate, summary.pass_rate_interval),
        (summary.pass_hat_k, summary.pass_hat_k_interval),
        (summary.pass_at_k, summary.pass_at_k_interval),
    ]:
        assert rate is not None and interval is not None
        assert 0.0 <= interval[0] <= rate <= interval[1] <= 1.0

    job = EvaluationJob(agent_id=AGENT_ID, trials=3, results=results)
    stability = {item.case_id: (item.stability, item.pass_hat_k) for item in job.case_trials}
    assert stability == {
        "all": ("stable_pass", True),
        "flaky": ("unstable", False),
        "fail": ("stable_fail", False),
        "short": ("incomplete", None),
        "judge": ("incomplete", None),
    }


def test_same_mean_with_different_stability_is_distinguished() -> None:
    stable = evaluation.summarize(
        [
            *_trials(_case("a", "a", "x"), True, True),
            *_trials(_case("b", "b", "x"), False, False),
        ]
    )
    flaky = evaluation.summarize(
        [
            *_trials(_case("a", "a", "x"), True, False),
            *_trials(_case("b", "b", "x"), False, True),
        ]
    )
    assert stable.pass_rate == flaky.pass_rate == 0.5
    assert (stable.pass_hat_k, flaky.pass_hat_k) == (0.5, 0.0)
    assert (stable.unstable_cases, flaky.unstable_cases) == (0, 2)


def test_single_trial_summary_keeps_the_previous_meaning() -> None:
    """既定の 1 回は、以前と同じ合格率・ケースの数（Pass^k は 1 回の合格率と同じ）。"""
    summary = evaluation.summarize(
        [
            _judged(_case("a", "a", "x"), JudgeVerdict.CORRECT, 1.0),
            _judged(_case("b", "b", "x"), JudgeVerdict.INCORRECT, 0.0),
        ]
    )
    assert (summary.trials, summary.judged_cases, summary.pass_rate) == (1, 2, 0.5)
    assert summary.pass_hat_k == summary.pass_at_k == 0.5


@pytest.mark.usefixtures("agent")
def test_repeated_trials_run_each_case_k_times(monkeypatch: MonkeyPatch) -> None:
    """ケース × 試行を Run にし、試行の番号を Run に残す。同じケースの成否のぶれを出す。"""
    _script(
        monkeypatch,
        [assistant_message("売上は 300 万円です。")],
        _judgement("correct", 1.0, "良い"),
        [assistant_message("経費は 50 万円です。")],
        _judgement("correct", 1.0, "良い"),
        [assistant_message("売上は分かりません。")],
        _judgement("incorrect", 0.0, "数値が無い"),
        [assistant_message("経費は 50 万円です。")],
        _judgement("correct", 1.0, "良い"),
    )
    sales, costs = _case("c1", "売上は？", "300 万円"), _case("c2", "経費は？", "50 万円")
    job = evaluation_store.create(
        EvaluationJob(
            agent_id=AGENT_ID,
            set_id="evset_trials",
            trials=2,
            created_by_user_uuid=USER_UUID,
            results=[
                EvaluationCaseResult(case=case, trial=trial)
                for trial in (1, 2)
                for case in (sales, costs)
            ],
        )
    )

    anyio.run(run_evaluation_job, job.id)

    finished = evaluation_store.get(job.id)
    assert finished.status == JobStatus.COMPLETED
    trials = [
        runtime_repository.get_run(str(result.run_id)).metadata["evaluation_trial"]
        for result in finished.results
    ]
    assert trials == [1, 1, 2, 2]
    summary = finished.summary
    assert (summary.pass_rate, summary.pass_hat_k, summary.pass_at_k) == (0.75, 0.5, 1.0)
    assert [item.stability for item in finished.case_trials] == ["unstable", "stable_pass"]


@pytest.mark.usefixtures("agent")
def test_cancel_stops_the_remaining_trials(monkeypatch: MonkeyPatch) -> None:
    """取消は反復にも効く。未実行の試行は欠測で、Pass^k の分母に入れない。"""
    _script(monkeypatch)
    case = _case("c1", "売上は？", "300 万円")
    job = evaluation_store.create(
        EvaluationJob(
            agent_id=AGENT_ID,
            trials=3,
            results=[EvaluationCaseResult(case=case, trial=trial) for trial in (1, 2, 3)],
        )
    )
    evaluation_store.cancel(job.id)

    anyio.run(run_evaluation_job, job.id)

    summary = evaluation_store.get(job.id).summary
    assert (summary.missing_trials, summary.complete_cases, summary.pass_hat_k) == (3, 0, None)


@pytest.mark.usefixtures("agent", "no_background")
def test_create_evaluation_orders_trials_and_enforces_the_budget(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(evaluation, "judge_fingerprint", lambda: "judge-a")
    monkeypatch.setattr("app.features.agent.router.judge_fingerprint", lambda: "judge-a")
    set_id = client.post("/api/evaluation-sets", json=SET_BODY).json()["data"]["id"]

    created = client.post("/api/evaluations", json={"set_id": set_id, "trials": 3})
    assert created.status_code == 202, created.text
    data = created.json()["data"]
    assert (data["trials"], data["judge_fingerprint"]) == (3, "judge-a")
    # 試行の順に、各試行は評価ケースの順。
    assert [(item["trial"], item["case"]["id"]) for item in data["results"]] == [
        (1, "case-1"),
        (1, "custom"),
        (2, "case-1"),
        (2, "custom"),
        (3, "case-1"),
        (3, "custom"),
    ]
    assert data["summary"]["cases"] == 2 and data["summary"]["total"] == 6
    client.post(f"/api/evaluations/{data['id']}/cancel")

    # ケース 2 件 × 3 回 = 6 回の実行は、予算 5 回を超える。
    monkeypatch.setattr(get_settings(), "agent_evaluation_max_trial_runs", 5)
    over = client.post("/api/evaluations", json={"set_id": set_id, "trials": 3})
    assert over.status_code == 422, over.text
    assert "実行の予算（5 回）" in over.json()["error_messages"][0]
    too_many = client.post(
        "/api/evaluations",
        json={"set_id": set_id, "trials": evaluation.EVALUATION_TRIALS_MAX + 1},
    )
    assert too_many.status_code == 422


def test_compare_pairs_only_cases_with_the_same_definition() -> None:
    """対応のある比較は同じ定義のケースだけ。質問が同じでも期待が違えば統計に入れない。"""
    same = _case("a", "締め日は？", "25 日")
    changed = _case("b", "承認者は？", "部長")
    base = EvaluationJob(
        agent_id=AGENT_ID,
        trials=2,
        judge_fingerprint="judge-a",
        results=[*_trials(same, False, False), *_trials(changed, False, False)],
    )
    target = EvaluationJob(
        agent_id=AGENT_ID,
        trials=3,
        judge_fingerprint="judge-b",
        results=[
            *_trials(same, True, True, False),
            *_trials(_case("b", "承認者は？", "課長"), True, True, True),
        ],
    )

    comparison = evaluation.compare_jobs(base, target)

    assert [row.same_definition for row in comparison.rows] == [True, False]
    assert (comparison.same_cases, comparison.definition_mismatch) == (False, 1)
    # 評価者の指紋が違えば、業務 Agent の差と評価者の差を分けられないと示す。
    assert comparison.same_judge is False
    first = comparison.rows[0]
    assert first.base is not None and first.target is not None
    assert (first.target.trials, first.target.passed_trials, first.target.status) == (
        3,
        2,
        "unstable",
    )
    assert first.change == "improved"
    paired = comparison.paired
    assert paired is not None and paired.cases == 1
    assert paired.difference == pytest.approx(2 / 3)
    # 1 ケースでは区間が 0 をまたぐ（少数標本の差を確かとしない）。
    assert paired.interval[0] < 0 < paired.interval[1] and not paired.conclusive


def test_publish_regression_message_reports_the_paired_difference() -> None:
    """回帰の門の拒否の文に、同じケースの対応比較の差と区間を添える（#62）。"""
    from app.features.agent.router import _paired_note

    cases = [_case(f"c{index}", f"質問 {index}", "x") for index in range(4)]
    baseline = EvaluationJob(
        agent_id=AGENT_ID, results=[*(_judged(case, JudgeVerdict.CORRECT, 1) for case in cases)]
    )
    draft = EvaluationJob(
        agent_id=AGENT_ID,
        results=[
            _judged(cases[0], JudgeVerdict.INCORRECT, 0),
            *(_judged(case, JudgeVerdict.CORRECT, 1) for case in cases[1:]),
        ],
    )
    note = _paired_note(baseline, draft)
    assert "同じケース 4 件の対応比較では差 -25 pt" in note
    assert "区間が 0 をまたぐため差は不確か" in note
