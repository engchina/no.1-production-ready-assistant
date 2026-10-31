"""判定の一致の集計（#3418・#2577）。"""

from __future__ import annotations

import pytest

from pr_evaluation_scoring import (
    CalibrationThresholds,
    LabelPair,
    agreement,
    calibration_failures,
    cohen_kappa,
)

LABELS = ("pass", "fail")


def _pairs(rows: list[tuple[str, str | None]]) -> list[LabelPair]:
    return [LabelPair(item_id=f"i{index}", human=h, judge=j) for index, (h, j) in enumerate(rows)]


def test_agreement_counts_confusion_kappa_and_false_rates() -> None:
    rows: list[tuple[str, str | None]] = [
        ("pass", "pass"),
        ("pass", "pass"),
        ("pass", "fail"),
        ("fail", "fail"),
        ("fail", "pass"),
        ("fail", "fail"),
        ("fail", None),
    ]
    result = agreement(_pairs(rows), LABELS, positive="pass", negative="fail")
    assert (result.total, result.judged, result.unjudged) == (7, 6, 1)
    assert result.coverage == round(6 / 7, 4)
    assert result.accuracy == round(4 / 6, 4)
    assert result.confusion == {"pass": {"pass": 2, "fail": 1}, "fail": {"pass": 1, "fail": 2}}
    # 観測の一致 4/6、偶然の一致 0.5 → κ = (0.6667 - 0.5) / 0.5。
    assert result.kappa == round((4 / 6 - 0.5) / 0.5, 4)
    assert result.false_pass_rate == round(1 / 3, 4)
    assert result.false_fail_rate == round(1 / 3, 4)


def test_kappa_edge_cases() -> None:
    assert cohen_kappa([], LABELS) is None
    # 人手も評価器も 1 つのラベルだけなら、偶然の一致が 1 で κ は決められない。
    assert cohen_kappa([("pass", "pass"), ("pass", "pass")], LABELS) is None
    assert cohen_kappa([("pass", "pass"), ("fail", "fail")], LABELS) == 1.0
    assert cohen_kappa([("pass", "fail"), ("fail", "pass")], LABELS) == -1.0


def test_unknown_human_labels_are_rejected_and_unknown_judge_labels_are_unjudged() -> None:
    with pytest.raises(ValueError, match="語彙"):
        agreement(_pairs([("maybe", "pass")]), LABELS)
    result = agreement(_pairs([("pass", "cannot_judge")]), LABELS)
    assert (result.judged, result.unjudged, result.accuracy, result.kappa) == (0, 1, None, None)
    assert result.false_pass_rate is None


def test_calibration_failures_check_every_holdout_threshold() -> None:
    thresholds = CalibrationThresholds(min_items=4, min_kappa=0.5, max_false_pass_rate=0.0)
    perfect = agreement(
        _pairs([("pass", "pass"), ("pass", "pass"), ("fail", "fail"), ("fail", "fail")]),
        LABELS,
        positive="pass",
        negative="fail",
    )
    assert calibration_failures(perfect, thresholds) == []
    noisy = agreement(
        _pairs([("pass", "pass"), ("fail", "pass"), ("fail", None)]),
        LABELS,
        positive="pass",
        negative="fail",
    )
    failures = calibration_failures(noisy, thresholds)
    assert [item.split(" ")[0] for item in failures] == ["holdout", "判定率", "κ", "誤合格の率"]
    assert calibration_failures(None, thresholds) == ["holdout の項目がありません"]
    # 測れない値（None）は不合格にし、None を文に出さない（#3585）。
    unmeasured = agreement(
        _pairs([("pass", "pass"), ("pass", "pass"), ("pass", "pass"), ("pass", None)]),
        LABELS,
        positive="pass",
        negative="fail",
    )
    assert (unmeasured.kappa, unmeasured.false_pass_rate) == (None, None)
    failures = calibration_failures(unmeasured, thresholds)
    assert failures == [
        "判定率 0.75 < 0.95",
        "κ —（判定した行が無いか、人と評価器の判定がすべて同じで、測れません）",
        "誤合格の率 —（人が fail にした行がありません）",
    ]
    assert not any("None" in item for item in failures)
    empty = agreement([], LABELS, positive="pass", negative="fail")
    assert calibration_failures(empty, CalibrationThresholds(min_items=0))[0] == (
        "判定率 —（項目がありません）"
    )
    assert CalibrationThresholds().provenance() == {
        "split": "holdout",
        "min_items": 50,
        "min_kappa": 0.6,
        "max_false_pass_rate": 0.1,
        "min_coverage": 0.95,
    }
