"""ケースの反復試行の集計と区間（Pass@k・Pass^k・欠測・Wilson・MOVER）。"""

from __future__ import annotations

import pytest

from pr_evaluation_scoring import (
    case_trials,
    paired_difference,
    summarize_trials,
    wilson_interval,
)


def test_wilson_interval_has_width_at_zero_and_full_rates() -> None:
    assert wilson_interval(0.5, 0) is None
    low, high = wilson_interval(0.5, 10) or (0, 0)
    assert (round(low, 4), round(high, 4)) == (0.2366, 0.7634)
    # 5 件すべて成功でも、区間の下限は 1 にならない（小標本の 100% を確かとしない）。
    low, high = wilson_interval(1.0, 5) or (0, 0)
    assert high == 1.0 and round(low, 4) == round(5 / (5 + 1.959963984540054**2), 4)
    low, high = wilson_interval(0.0, 5) or (1, 1)
    assert low == 0.0 and 0.4 < high < 0.5


def test_case_trials_separates_pass_any_pass_all_and_missing() -> None:
    all_pass = case_trials([True, True, True])
    assert (all_pass.pass_any, all_pass.pass_all, all_pass.stability) == (True, True, "stable_pass")
    flaky = case_trials([True, False, True])
    assert (flaky.pass_any, flaky.pass_all, flaky.stability) == (True, False, "unstable")
    assert flaky.rate == pytest.approx(2 / 3)
    all_fail = case_trials([False, False, False])
    assert (all_fail.pass_any, all_fail.pass_all, all_fail.stability) == (
        False,
        False,
        "stable_fail",
    )
    # 欠測のある k 回は全成功に数えない（Pass^k / Pass@k は不明）。
    short = case_trials([True, True, None])
    assert (short.pass_any, short.pass_all, short.stability) == (None, None, "incomplete")
    assert (short.judged, short.missing, short.rate) == (2, 1, 1.0)
    # 結果が求めた回数に足りない分も欠測にする。
    missing = case_trials([True], trials=3)
    assert (missing.missing, missing.pass_all) == (2, None)
    # 成功と失敗が両方あれば、欠測があっても不安定と分かる。
    assert case_trials([True, False, None]).stability == "unstable"
    assert case_trials([None, None]).stability == "unjudged"


def test_same_mean_with_different_stability_is_distinguished() -> None:
    # どちらも試行の平均は 50%。A は毎回同じ（半分のケースが常に成功）、B は毎回ぶれる。
    stable = summarize_trials(
        [case_trials([True, True]), case_trials([True, True])]
        + [case_trials([False, False]), case_trials([False, False])]
    )
    flaky = summarize_trials([case_trials([True, False]) for _ in range(4)])
    assert stable.mean_rate == flaky.mean_rate == 0.5
    assert (stable.pass_all_rate, flaky.pass_all_rate) == (0.5, 0.0)
    assert (stable.pass_any_rate, flaky.pass_any_rate) == (0.5, 1.0)
    assert (stable.stable_cases, stable.unstable_cases) == (4, 0)
    assert (flaky.stable_cases, flaky.unstable_cases) == (0, 4)


def test_summary_excludes_incomplete_cases_from_pass_k() -> None:
    summary = summarize_trials(
        [
            case_trials([True, True, True]),
            case_trials([True, False, True]),
            case_trials([True, None, None]),
            case_trials([None, None, None]),
        ]
    )
    assert (summary.cases, summary.judged_cases, summary.complete_cases) == (4, 3, 2)
    assert (summary.incomplete_cases, summary.missing_trials, summary.judged_trials) == (2, 5, 7)
    assert summary.pass_all_rate == 0.5 and summary.pass_any_rate == 1.0
    assert summary.mean_rate == pytest.approx((1 + 2 / 3 + 1) / 3)
    assert summary.pass_all_interval is not None and summary.mean_rate_interval is not None
    empty = summarize_trials([])
    assert empty.mean_rate is None and empty.pass_all_interval is None


def test_paired_difference_keeps_cases_as_the_unit() -> None:
    assert paired_difference([]) is None
    # 同じ結果なら差は 0 だが、5 ケースでは区間は広い（差が無いと断定しない）。
    same = paired_difference([(1.0, 1.0)] * 5)
    assert same is not None
    assert same.difference == 0 and same.interval[0] < -0.3 and same.interval[1] > 0.3
    assert not same.conclusive
    # 40 ケースすべてで改善すれば、区間は 0 をまたがない。
    better = paired_difference([(0.0, 1.0)] * 40)
    assert better is not None and better.conclusive and better.interval[0] > 0.5
    assert (better.improved, better.regressed, better.unchanged) == (40, 0, 0)
    # 相関の高い対（同じケースが両条件で同じ向きに動く）は、独立とみなすより区間が狭い。
    pairs = [(1.0, 1.0)] * 10 + [(0.0, 0.0)] * 8 + [(0.0, 1.0)] * 2
    correlated = paired_difference(pairs)
    independent = paired_difference(
        [(1.0, 0.0)] * 2 + [(1.0, 1.0)] * 8 + [(0.0, 1.0)] * 4 + [(0.0, 0.0)] * 6
    )
    assert correlated is not None and independent is not None
    assert correlated.difference == pytest.approx(0.1)
    width = correlated.interval[1] - correlated.interval[0]
    assert width < independent.interval[1] - independent.interval[0]
    assert -1.0 <= correlated.interval[0] < correlated.difference < correlated.interval[1] <= 1.0
