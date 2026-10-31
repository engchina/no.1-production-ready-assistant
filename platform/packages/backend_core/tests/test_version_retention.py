from __future__ import annotations

import pytest

from pr_backend_core.version_retention import versions_to_prune


@pytest.mark.parametrize(
    ("versions", "keep", "limit", "expected"),
    [
        # 上限以内は消さない。
        ([1, 2, 3], [3], 5, []),
        ([1, 2, 3, 4, 5], [5], 5, []),
        # 6 版目を公開したら最も古い版を消す。
        ([1, 2, 3, 4, 5, 6], [6], 5, [1]),
        # 公開中の古い版は残し、公開中 + 直近の 4 版で計 5 版にする。
        ([1, 2, 3, 4, 5, 6, 7], [2], 5, [1, 3]),
        # 残す版が無ければ直近の 5 版。
        ([1, 2, 3, 4, 5, 6], [None], 5, [1]),
        # 番号の飛び（前に消した版）は詰めない。
        ([2, 3, 4, 5, 6, 7], [7], 5, [2]),
        # 上限 1 未満は 1（残す版だけ）。
        ([1, 2, 3], [2], 0, [1, 3]),
        # 一覧に無い番号は数えない（直近の版で埋める）。
        ([1, 2, 3], [9], 2, [1]),
        # 残す版が上限より多ければ残す版だけ。
        ([1, 2, 3, 4], [1, 2, 3], 2, [4]),
    ],
    ids=[
        "within-limit",
        "at-limit",
        "over-limit",
        "keeps-old-published",
        "no-keep",
        "gaps-kept",
        "limit-floor",
        "keep-missing",
        "keep-over-limit",
    ],
)
def test_versions_to_prune(
    versions: list[int], keep: list[int | None], limit: int, expected: list[int]
) -> None:
    assert versions_to_prune(versions, keep=keep, limit=limit) == expected
