"""人手の判定を集める共有の部品（区分・記入者の間の一致・揺れ・rubric・合成の対照。#3578）。"""

from __future__ import annotations

import pytest

from pr_evaluation_scoring import (
    ERROR_TYPE_IDS,
    agreed_label,
    applicable_aspects,
    aspect_criteria,
    aspect_ids,
    assign_splits,
    label_variance,
    load_rubric_data,
    pairwise_agreement,
)
from pr_evaluation_scoring.perturbation import PerturbationSource, perturb_items


def test_assign_splits_is_deterministic_and_stratified() -> None:
    strata = {f"c{index:02d}": ("a" if index % 2 else "b") for index in range(20)}
    splits = assign_splits(strata, holdout_ratio=0.7, salt="s")
    assert splits == assign_splits(strata, holdout_ratio=0.7, salt="s")
    for stratum in ("a", "b"):
        units = [unit for unit, value in strata.items() if value == stratum]
        assert sum(splits[unit] == "dev" for unit in units) == 3
    assert assign_splits(strata, holdout_ratio=0.7, salt="other") != splits
    with pytest.raises(ValueError):
        assign_splits(strata, holdout_ratio=0, salt="s")


def test_agreed_label_needs_everyone_to_agree() -> None:
    assert agreed_label(["pass", "pass"]) == "pass"
    assert agreed_label(["pass", "fail"]) is None
    assert agreed_label(["pass", None]) is None
    assert agreed_label([]) is None


def test_pairwise_agreement_counts_only_pass_fail() -> None:
    keys = ["r1", "r2", "r3", "r4"]
    annotations = {
        "a1": {"r1": "pass", "r2": "fail", "r3": "pass", "r4": "cannot_judge"},
        "a2": {"r1": "pass", "r2": "fail", "r3": "fail", "r4": "pass"},
    }
    pairs = pairwise_agreement(keys, annotations)
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair.annotators == ("a1", "a2")
    assert pair.compared == 3
    assert pair.agreement == pytest.approx(0.6667)
    assert pair.kappa == pytest.approx(0.4)


def test_label_variance_skips_missing_items() -> None:
    runs = ({"i1": "pass", "i2": "fail"}, {"i1": "pass", "i2": "-", "i3": "pass"})
    variance = label_variance(runs)
    assert variance.runs == 2
    assert variance.items == 3
    assert variance.unstable_items == ["i2"]
    assert variance.unstable_rate == pytest.approx(0.3333)
    assert label_variance(()).unstable_rate is None


def test_default_rubric_aspects_and_criteria() -> None:
    rubric = load_rubric_data()
    assert aspect_ids(rubric) == ["requirement_coverage", "answer_correctness", "claim_support"]
    assert applicable_aspects(rubric, has_standard_answer=False) == ["claim_support"]
    assert len(applicable_aspects(rubric, has_standard_answer=True)) == 3
    criteria = aspect_criteria(rubric["instructions"], "claim_support")
    assert criteria and criteria != rubric["instructions"]
    assert aspect_criteria(rubric["instructions"], "unknown") == rubric["instructions"]


def test_perturb_items_is_deterministic_and_bounded() -> None:
    sources = tuple(
        PerturbationSource(
            id=f"i{index}",
            answer=f"期限は {index + 3} 日で、上限は {index + 10} 件です。",
            standard_answer=None,
            aspects=("answer_correctness", "claim_support"),
        )
        for index in range(5)
    )
    first = perturb_items(sources, ("swap_values",), perturb_max=2, salt="s")
    assert first == perturb_items(sources, ("swap_values",), perturb_max=2, salt="s")
    assert len(first) == 2
    assert all(item.id == item.base_item_id + "~swap_values" for item in first)
    expected = {"answer_correctness": "fail", "claim_support": "fail"}
    assert all(item.labels == expected for item in first)


def test_error_types_are_unique() -> None:
    assert len(set(ERROR_TYPE_IDS)) == len(ERROR_TYPE_IDS) == 9
