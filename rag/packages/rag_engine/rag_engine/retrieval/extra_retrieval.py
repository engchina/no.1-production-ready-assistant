"""主の検索の後に足す検索（追加の検索）の回数の枠（#2395）。

1 回の回答で、主の検索の後に足す検索は、どれも同じ総枠
（`Settings.extra_retrieval_max_queries`）から回数を取る。

- CRAG の補正検索（`crag`）
- 小問ごとの検索と根拠の無い要求の再検索（`request_coverage`。#2399・#1279）

回数を数えるのはこの module の `ExtraRetrievalBudget` だけにし、種類を足すときも同じ枠から取る。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 種類の名前（記録の key）。
REQUEST_COVERAGE_KIND = "request_coverage"


@dataclass
class ExtraRetrievalBudget:
    """1 回の回答の追加の検索の枠。回答ごとに作り、種類ごとに使った回数を数える。"""

    max_queries: int
    used: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: Any) -> ExtraRetrievalBudget:
        return cls(max_queries=max(0, int(getattr(settings, "extra_retrieval_max_queries", 0) or 0)))

    @property
    def remaining(self) -> int:
        """まだ使える回数。"""
        return max(0, self.max_queries - sum(self.used.values()))

    def allowance(self, kind_limit: int) -> int:
        """種類の上限と枠の残りのうち小さい方（その種類が今から取れる回数）。"""
        return max(0, min(int(kind_limit), self.remaining))

    def take(self, kind: str, count: int = 1) -> None:
        """種類 ``kind`` が ``count`` 回の検索をした（枠の残りを超えては数えない）。"""
        taken = max(0, min(int(count), self.remaining))
        if taken:
            self.used[kind] = self.used.get(kind, 0) + taken

    def to_payload(self) -> dict[str, Any]:
        return {"max_queries": self.max_queries, "used": dict(self.used), "remaining": self.remaining}


__all__ = ["REQUEST_COVERAGE_KIND", "ExtraRetrievalBudget"]
