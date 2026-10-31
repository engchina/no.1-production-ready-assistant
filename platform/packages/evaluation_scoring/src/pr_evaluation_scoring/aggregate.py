"""指標の集計（成功したケースのうち測れたものだけの平均と、その分母）と閾値の判定。

測れなかった指標（None）は 0 点にせず、平均にも分母にも入れない。測れた数（`counts`）を必ず
一緒に出し、平均がいくつのケースから求めたかを分かるようにする。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from math import fsum


@dataclass
class MetricAggregate:
    """指標ごとに、測れたケースの値を集める（`names` の指標を、測れなくても出す）。"""

    names: Sequence[str]
    values: dict[str, list[float]] = field(default_factory=dict)

    def add(self, metric: str, value: float | None) -> None:
        if value is not None:
            self.values.setdefault(metric, []).append(float(value))

    def add_all(self, values: Mapping[str, float | None]) -> None:
        for metric, value in values.items():
            self.add(metric, value)

    def means(self) -> dict[str, float | None]:
        """指標ごとの平均（測れたケースが無ければ None。丸めない）。"""
        return {
            metric: fsum(self.values[metric]) / len(self.values[metric])
            if self.values.get(metric)
            else None
            for metric in self.names
        }

    def counts(self) -> dict[str, int]:
        """指標ごとの測れたケースの数（平均の分母）。"""
        return {metric: len(self.values.get(metric, [])) for metric in self.names}


def rounded_mean(values: Sequence[float], digits: int = 4) -> float | None:
    """値の平均を `digits` 桁に丸める（値が無ければ None）。"""
    return round(sum(values) / len(values), digits) if values else None


def count_reasons(counts: dict[str, int], reasons: Iterable[str]) -> None:
    """理由ごとのケースの数に足す。"""
    for reason in reasons:
        counts[reason] = counts.get(reason, 0) + 1


@dataclass(frozen=True)
class ThresholdFailure:
    """閾値を下回った集計の指標。"""

    metric: str
    actual: float
    threshold: float


def threshold_failures(
    thresholds: Mapping[str, float],
    values: Mapping[str, float | None],
    *,
    reference_only: Iterable[str] = (),
) -> list[ThresholdFailure]:
    """閾値を下回った指標（測れなかった指標と、参考だけの指標は判定しない）。"""
    skipped = set(reference_only)
    failures: list[ThresholdFailure] = []
    for metric, threshold in thresholds.items():
        if metric in skipped:
            continue
        actual = values.get(metric)
        if actual is not None and actual < threshold:
            failures.append(ThresholdFailure(metric=metric, actual=actual, threshold=threshold))
    return failures


def overall_passed(
    *,
    failures: Sequence[ThresholdFailure],
    error_count: int,
    incomplete_count: int,
    measured_counts: Mapping[str, int],
    thresholds: Mapping[str, float],
    values: Mapping[str, float | None],
) -> bool | None:
    """評価の合否。

    閾値を下回った指標・失敗したケース・判定を終えられなかったケースが 1 つでもあれば False。
    どの指標も測れなかったか、閾値を付けた指標が測れなかったときは None（合格にしない）。
    """
    if failures or error_count or incomplete_count:
        return False
    if not any(measured_counts.values()) or any(values.get(name) is None for name in thresholds):
        return None
    return True
