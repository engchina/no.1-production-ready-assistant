"""2 つの RAG の評価の、同じケースの対応のある比較（#60）。合成の結果だけの決定論テスト。

全件同値・全件成功・小標本・片側エラー・ケースの id の不一致・評価セットの違い・実行中の変化と、
統計の判定不能と絶対の gate の不合格の区別を確かめる。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch

from app.rag import evaluation_run_cli
from app.rag.evaluation_paired import compare_paired
from app.schemas.evaluation import (
    EvaluationCaseResult,
    EvaluationMetrics,
    EvaluationPairedComparison,
    EvaluationPairedMetric,
    EvaluationRunRequest,
    EvaluationThresholdFailure,
)


def _case(case_id: str, *, status: str = "success", **values: float | None) -> EvaluationCaseResult:
    return EvaluationCaseResult.model_validate(
        {
            "case_id": case_id,
            "trace_id": f"t-{case_id}",
            "status": status,
            "elapsed_ms": 1.0,
            **values,
        }
    )


def _metrics(cases: list[EvaluationCaseResult], **update: Any) -> EvaluationMetrics:
    manifest = {"evaluation_set": {"sha256": "set-1"}, "comparable": True, "settings": {"x": 1}}
    payload: dict[str, Any] = {
        "case_count": len(cases),
        "passed": True,
        "case_results": cases,
        "run_manifest": manifest,
    }
    return EvaluationMetrics.model_validate({**payload, **update})


def _metric(result: EvaluationPairedComparison, name: str) -> EvaluationPairedMetric:
    return next(item for item in result.metrics if item.metric == name)


def test_all_equal_cases_are_unchanged() -> None:
    cases = [_case(f"c{i}", mrr=0.5, response_decision_accuracy=1.0) for i in range(12)]

    result = compare_paired(_metrics(cases), _metrics(cases))

    assert result.status == "compared"
    mrr = _metric(result, "mrr")
    assert (mrr.verdict, mrr.mean_difference, mrr.interval) == ("unchanged", 0.0, (0.0, 0.0))
    assert _metric(result, "answer_correctness").verdict == "not_measured"


def test_all_cases_improving_binary_metric_is_significant() -> None:
    base = [_case(f"c{i}", response_decision_accuracy=0.0) for i in range(12)]
    target = [_case(f"c{i}", response_decision_accuracy=1.0) for i in range(12)]

    decision = _metric(
        compare_paired(_metrics(base), _metrics(target)), "response_decision_accuracy"
    )

    assert decision.binary is True
    assert decision.method == "exact_sign_test"
    assert (decision.improved, decision.regressed, decision.paired_cases) == (12, 0, 12)
    assert decision.p_value == pytest.approx(2 / 2**12)
    assert decision.verdict == "improved"


def test_small_sample_is_not_judged() -> None:
    base = [_case(f"c{i}", mrr=0.0) for i in range(3)]
    target = [_case(f"c{i}", mrr=1.0) for i in range(3)]

    mrr = _metric(compare_paired(_metrics(base), _metrics(target)), "mrr")

    assert mrr.mean_difference == 1.0
    assert mrr.verdict == "insufficient_cases"


def test_one_side_error_is_excluded_not_counted_as_improvement() -> None:
    base = [_case(f"c{i}", mrr=0.5) for i in range(12)]
    target = [_case(f"c{i}", mrr=0.5) for i in range(11)] + [_case("c11", status="error")]

    result = compare_paired(_metrics(base), _metrics(target, error_count=1, passed=False))
    mrr = _metric(result, "mrr")

    assert (mrr.paired_cases, mrr.error_cases, mrr.improved, mrr.regressed) == (11, 1, 0, 0)
    assert mrr.verdict == "unchanged"
    assert result.cases[-1].target_status == "error"
    assert result.cases[-1].differences["mrr"] is None
    # 統計の判定（同値）と、絶対の gate の不合格（失敗したケース）は別に出す。
    assert result.target_gate_failed is True
    assert result.base_gate_failed is False


def test_mixed_continuous_differences_use_seeded_bootstrap() -> None:
    base = [_case(f"c{i}", answer_correctness=0.5) for i in range(12)]
    target = [
        _case(f"c{i}", answer_correctness=0.5 + (0.25 if i % 2 else -0.25)) for i in range(12)
    ]

    first = compare_paired(_metrics(base), _metrics(target), seed=1)
    again = compare_paired(_metrics(base), _metrics(target), seed=1)
    correctness = _metric(first, "answer_correctness")

    assert correctness.method == "paired_bootstrap"
    assert correctness.interval == _metric(again, "answer_correctness").interval
    assert correctness.interval is not None
    assert correctness.interval[0] < 0 < correctness.interval[1]
    assert correctness.verdict == "inconclusive"


def test_case_id_mismatch_and_different_sets_are_not_comparable() -> None:
    base = [_case("c1", mrr=1.0), _case("c2", mrr=1.0)]
    target = [_case("c1", mrr=1.0), _case("c3", mrr=1.0)]

    mismatch = compare_paired(_metrics(base), _metrics(target))
    assert mismatch.status == "not_comparable"
    assert "case_ids_differ" in mismatch.reasons
    assert (mismatch.base_only_case_ids, mismatch.target_only_case_ids) == (["c2"], ["c3"])
    assert mismatch.metrics == []

    other_set = _metrics(base, run_manifest={"evaluation_set": {"sha256": "set-2"}})
    assert compare_paired(_metrics(base), other_set).reasons == ["evaluation_set_differs"]

    unstable = _metrics(
        base, run_manifest={"evaluation_set": {"sha256": "set-1"}, "comparable": False}
    )
    assert compare_paired(_metrics(base), unstable).reasons == ["changed_during_run:target"]


def test_condition_differences_and_old_results() -> None:
    base = [_case("c1", mrr=1.0)]
    changed = _metrics(
        base, run_manifest={"evaluation_set": {"sha256": "set-1"}, "settings": {"x": 2}}
    )

    assert compare_paired(_metrics(base), changed).condition_differences == ["settings.x"]
    old = compare_paired(_metrics(base, run_manifest=None), _metrics(base, run_manifest=None))
    assert old.status == "compared"
    assert old.warnings == ["run_manifest_missing"]


def test_threshold_failure_fails_gate_even_when_improved() -> None:
    base = [_case(f"c{i}", response_decision_accuracy=0.0) for i in range(12)]
    target = [_case(f"c{i}", response_decision_accuracy=1.0) for i in range(12)]
    failure = EvaluationThresholdFailure(metric="mrr", actual=0.1, threshold=0.5)

    result = compare_paired(
        _metrics(base), _metrics(target, threshold_failures=[failure], passed=False)
    )

    assert _metric(result, "response_decision_accuracy").verdict == "improved"
    assert result.target_gate_failed is True
    assert result.target_threshold_failures == [failure]


def _write(path: Path, metrics: EvaluationMetrics) -> Path:
    path.write_text(json.dumps({"data": metrics.model_dump(mode="json")}), encoding="utf-8")
    return path


def test_cli_compare_prints_table_and_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = [_case(f"c{i}", response_decision_accuracy=0.0) for i in range(12)]
    target = [_case(f"c{i}", response_decision_accuracy=1.0) for i in range(12)]
    left = _write(tmp_path / "base.json", _metrics(base))
    right = _write(tmp_path / "target.json", _metrics(target))
    output = tmp_path / "paired.json"

    assert evaluation_run_cli.main(["compare", str(left), str(right), "--output", str(output)]) == 0
    printed = capsys.readouterr().out
    assert "seed 20260" in printed
    assert "response_decision_accuracy" in printed
    assert "improved" in printed
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "compared"
    # 逆向き（悪化）は終了コード 1。
    assert evaluation_run_cli.main(["compare", str(right), str(left)]) == 1


async def test_profile_comparison_in_job_has_paired_result(monkeypatch: MonkeyPatch) -> None:
    from app.api.routes import evaluation as evaluation_route

    class Runner:
        async def run(self, **kwargs: object) -> EvaluationMetrics:
            value = 1.0 if kwargs.get("search_answer_profile_id") else 0.0
            cases = [_case(f"c{i}", response_decision_accuracy=value) for i in range(12)]
            return EvaluationMetrics(case_count=12, passed=True, case_results=cases)

    monkeypatch.setattr(evaluation_route, "EvaluationRunner", Runner)
    monkeypatch.setattr(evaluation_route, "OracleClient", object)
    request = EvaluationRunRequest.model_validate(
        {
            "cases": [{"id": f"c{i}", "query": "q"} for i in range(12)],
            "compare_search_answer_profile_id": "p",
        }
    )

    metrics, _ = await evaluation_route._execute_run(request, time_budget_seconds=60)

    assert metrics.comparison is not None
    paired = metrics.comparison.paired
    assert paired is not None
    assert _metric(paired, "response_decision_accuracy").verdict == "improved"
