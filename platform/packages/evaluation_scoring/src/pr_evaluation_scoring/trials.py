"""ケースの反復試行の集計（Pass@k・Pass^k・安定性）と、成功率の区間・対応のある差の区間。

- 1 ケースを k 回試行した結果を `True`（成功）・`False`（失敗）・`None`（欠測: 未完了・評価者の
  失敗・取消など）で受け取る。欠測を成功にも失敗にも数えない。
- Pass@k（k 回のどれかが成功）と Pass^k（k 回すべてが成功）は、k 回すべてを判定できたケースだけを
  分母にする。欠測のあるケースは `incomplete` として別に数える。
- 区間の単位はケース（同じケースの反復は独立した問題ではない）。成功率の区間は Wilson の score
  区間で、小標本・0% / 100% でも幅のある区間を返す。ケースごとの成功率（0〜1）の平均に使う場合も、
  [0, 1] に収まる値の分散は平均 p に対して p(1 - p) 以下なので、ケース数を n にした Wilson 区間は
  控えめ（広め）な近似になる（k = 1 なら通常の Wilson 区間と同じ）。
- 対応のある差（同じ定義のケースを 2 条件で評価した差）は、各条件の Wilson 区間を、ケースごとの
  成功率の相関で組み合わせる MOVER（Newcombe 1998 の方法 10 を成功率に広げたもの。Zou & Donner
  2008）で求める。

標準 library だけで書き、製品の型を持たない。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import fsum, sqrt
from typing import Literal

# 両側 95% の正規分位点。
Z_95 = 1.959963984540054

Stability = Literal["stable_pass", "stable_fail", "unstable", "incomplete", "unjudged"]


def wilson_interval(rate: float, n: int, z: float = Z_95) -> tuple[float, float] | None:
    """成功率 `rate`（0〜1）・標本の数 `n` の Wilson の score 区間（n が 0 なら None）。"""
    return _wilson(rate, n, z) if n > 0 else None


def _wilson(rate: float, n: int, z: float) -> tuple[float, float]:
    p = min(1.0, max(0.0, rate))
    z2 = z * z
    center = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = z * sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return max(0.0, center - half), min(1.0, center + half)


@dataclass(frozen=True)
class CaseTrials:
    """1 ケースの k 回の試行の集計。"""

    trials: int
    judged: int
    passed: int
    missing: int
    # 判定できた試行の成功率（判定できた試行が無ければ None）。
    rate: float | None
    # k 回すべてを判定できたときだけ True / False（欠測があれば None）。
    pass_any: bool | None
    pass_all: bool | None
    stability: Stability


def case_trials(outcomes: Sequence[bool | None], trials: int | None = None) -> CaseTrials:
    """1 ケースの試行の結果を集計する（`trials` は求めた回数。結果が足りない分は欠測）。"""
    expected = max(trials if trials is not None else len(outcomes), len(outcomes))
    judged = [item for item in outcomes if item is not None]
    passed = sum(1 for item in judged if item)
    missing = expected - len(judged)
    complete = missing == 0 and expected > 0
    stability: Stability
    if 0 < passed < len(judged):
        # 成功と失敗が両方あれば、欠測があっても不安定と分かる。
        stability = "unstable"
    elif not judged:
        stability = "unjudged"
    elif not complete:
        stability = "incomplete"
    else:
        stability = "stable_pass" if passed == len(judged) else "stable_fail"
    return CaseTrials(
        trials=expected,
        judged=len(judged),
        passed=passed,
        missing=missing,
        rate=passed / len(judged) if judged else None,
        pass_any=passed > 0 if complete else None,
        pass_all=passed == len(judged) if complete else None,
        stability=stability,
    )


@dataclass(frozen=True)
class TrialsSummary:
    """ケース × 試行の集計（区間の単位はケース）。"""

    cases: int
    # 1 回以上判定できたケース（成功率の平均の分母）と、k 回すべてを判定できたケース
    # （Pass@k・Pass^k の分母）。
    judged_cases: int
    complete_cases: int
    incomplete_cases: int
    judged_trials: int
    missing_trials: int
    stable_cases: int
    unstable_cases: int
    # ケースごとの成功率の平均（ケースを等しく重み付ける）と区間。
    mean_rate: float | None
    mean_rate_interval: tuple[float, float] | None
    pass_any_rate: float | None
    pass_any_interval: tuple[float, float] | None
    pass_all_rate: float | None
    pass_all_interval: tuple[float, float] | None


def _share(values: Sequence[bool]) -> tuple[float | None, tuple[float, float] | None]:
    if not values:
        return None, None
    rate = sum(values) / len(values)
    return rate, wilson_interval(rate, len(values))


def summarize_trials(cases: Sequence[CaseTrials]) -> TrialsSummary:
    """ケースごとの集計を、評価全体の集計にする。"""
    rates = [case.rate for case in cases if case.rate is not None]
    complete = [case for case in cases if case.pass_all is not None]
    mean_rate = fsum(rates) / len(rates) if rates else None
    any_rate, any_interval = _share([bool(case.pass_any) for case in complete])
    all_rate, all_interval = _share([bool(case.pass_all) for case in complete])
    return TrialsSummary(
        cases=len(cases),
        judged_cases=len(rates),
        complete_cases=len(complete),
        incomplete_cases=sum(1 for case in cases if case.missing),
        judged_trials=sum(case.judged for case in cases),
        missing_trials=sum(case.missing for case in cases),
        stable_cases=sum(1 for case in cases if case.stability in {"stable_pass", "stable_fail"}),
        unstable_cases=sum(1 for case in cases if case.stability == "unstable"),
        mean_rate=mean_rate,
        mean_rate_interval=wilson_interval(mean_rate, len(rates))
        if mean_rate is not None
        else None,
        pass_any_rate=any_rate,
        pass_any_interval=any_interval,
        pass_all_rate=all_rate,
        pass_all_interval=all_interval,
    )


@dataclass(frozen=True)
class PairedDifference:
    """同じケースを 2 条件で評価した、ケースごとの成功率の差（target − base）。"""

    cases: int
    base_rate: float
    target_rate: float
    difference: float
    interval: tuple[float, float]
    improved: int
    regressed: int
    unchanged: int

    @property
    def conclusive(self) -> bool:
        """区間が 0 をまたがない（差の向きが区間の上で確か）か。"""
        low, high = self.interval
        return low > 0 or high < 0


def _correlation(left: Sequence[float], right: Sequence[float]) -> float:
    n = len(left)
    mean_left, mean_right = fsum(left) / n, fsum(right) / n
    cov = fsum((a - mean_left) * (b - mean_right) for a, b in zip(left, right, strict=True))
    var_left = fsum((a - mean_left) ** 2 for a in left)
    var_right = fsum((b - mean_right) ** 2 for b in right)
    if var_left <= 0 or var_right <= 0:
        # 片方が一定なら相関は定まらない（Newcombe と同じく 0 にする）。
        return 0.0
    return max(-1.0, min(1.0, cov / sqrt(var_left * var_right)))


def paired_difference(
    pairs: Sequence[tuple[float, float]], z: float = Z_95
) -> PairedDifference | None:
    """ケースごとの (base の成功率, target の成功率) の対から、差と MOVER の区間を求める。"""
    if not pairs:
        return None
    base = [min(1.0, max(0.0, left)) for left, _ in pairs]
    target = [min(1.0, max(0.0, right)) for _, right in pairs]
    n = len(pairs)
    p1, p2 = fsum(base) / n, fsum(target) / n
    l1, u1 = _wilson(p1, n, z)
    l2, u2 = _wilson(p2, n, z)
    r = _correlation(base, target)
    difference = p2 - p1
    low = difference - sqrt(
        max(0.0, (p2 - l2) ** 2 + (u1 - p1) ** 2 - 2 * r * (p2 - l2) * (u1 - p1))
    )
    high = difference + sqrt(
        max(0.0, (u2 - p2) ** 2 + (p1 - l1) ** 2 - 2 * r * (u2 - p2) * (p1 - l1))
    )
    return PairedDifference(
        cases=n,
        base_rate=p1,
        target_rate=p2,
        difference=difference,
        interval=(max(-1.0, low), min(1.0, high)),
        improved=sum(1 for left, right in zip(base, target, strict=True) if right > left),
        regressed=sum(1 for left, right in zip(base, target, strict=True) if right < left),
        unchanged=sum(1 for left, right in zip(base, target, strict=True) if right == left),
    )
