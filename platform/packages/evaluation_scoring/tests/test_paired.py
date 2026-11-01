"""対応のある比較の区間と検定（#60）。決定論（seed 固定）で確かめる。"""

from __future__ import annotations

import pytest

from pr_evaluation_scoring import exact_sign_test, paired_bootstrap_interval


def test_bootstrap_interval_is_reproducible_with_seed() -> None:
    differences = [0.0, 0.5, 1.0, -0.5, 0.25, 0.0, 1.0, 0.5]
    first = paired_bootstrap_interval(differences, seed=7, resamples=500)
    assert first == paired_bootstrap_interval(differences, seed=7, resamples=500)
    assert first != paired_bootstrap_interval(differences, seed=8, resamples=500)
    assert first is not None
    low, high = first
    assert low <= sum(differences) / len(differences) <= high


def test_bootstrap_interval_of_constant_differences_is_a_point() -> None:
    assert paired_bootstrap_interval([0.0] * 5, seed=0) == (0.0, 0.0)
    assert paired_bootstrap_interval([1.0] * 5, seed=0) == (1.0, 1.0)
    assert paired_bootstrap_interval([], seed=0) is None


@pytest.mark.parametrize(
    ("improved", "regressed", "expected"),
    [(0, 0, 1.0), (5, 5, 1.0), (6, 0, 0.03125), (0, 6, 0.03125), (1, 0, 1.0)],
)
def test_exact_sign_test(improved: int, regressed: int, expected: float) -> None:
    assert exact_sign_test(improved, regressed) == pytest.approx(expected)
