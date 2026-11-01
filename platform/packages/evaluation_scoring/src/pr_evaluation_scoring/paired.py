"""対応のある比較（同じケースを 2 条件で評価した差）の区間と検定。

- ``paired_bootstrap_interval``: ケースごとの差（target − base）の平均の、配対 bootstrap の
  percentile 区間。ケースを単位に復元抽出する（同じケースの 2 条件は組のまま）。乱数は ``seed`` で
  固定し、同じ入力・seed・反復数なら同じ区間になる。分布を仮定しないが、小標本では区間が狭く
  出やすい（呼び出し側で最小の件数を決める）。
- ``exact_sign_test``: 改善・悪化の件数（同値は除く）の両側の正確な二項検定。二値の指標では
  McNemar の正確検定と同じ。

標準 library だけで書き、製品の型を持たない。
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from math import comb, fsum


def paired_bootstrap_interval(
    differences: Sequence[float],
    *,
    seed: int,
    resamples: int = 2000,
    confidence: float = 0.95,
) -> tuple[float, float] | None:
    """差の平均の配対 bootstrap の percentile 区間（差が無ければ None）。"""
    n = len(differences)
    if n == 0 or resamples <= 0:
        return None
    values = [float(value) for value in differences]
    generator = random.Random(seed)  # noqa: S311  # nosec B311 - 統計の再現のための乱数
    means = sorted(
        fsum(values[generator.randrange(n)] for _ in range(n)) / n for _ in range(resamples)
    )
    alpha = (1.0 - confidence) / 2.0
    low = means[min(resamples - 1, int(alpha * resamples))]
    high = means[max(0, min(resamples - 1, int((1.0 - alpha) * resamples) - 1))]
    return low, high


def exact_sign_test(improved: int, regressed: int) -> float:
    """改善・悪化の件数の両側の正確な二項検定の p 値（どちらも 0 なら 1.0）。"""
    n = improved + regressed
    if n == 0:
        return 1.0
    tail = sum(comb(n, k) for k in range(min(improved, regressed) + 1)) / (1 << n)
    return min(1.0, 2.0 * tail)


__all__ = ["exact_sign_test", "paired_bootstrap_interval"]
