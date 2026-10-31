"""主の検索の後に足す検索（追加の検索）の回数の枠（#2395）。

1 回の回答で、主の検索の後に足す検索は、どれも同じ総枠
（`Settings.extra_retrieval_max_queries`）から回数を取る。必須の参照先を先に予約し、
任意の検索は予約していない残数だけを使う。予約で総枠を増やさない。

- CRAG の補正検索（`crag`）
- 業務ガイドの必須取得（`guide_required`）と任意の手順検索（`guide_steps`）
- 小問ごとの検索と根拠の無い要求の再検索（`request_coverage`。#2399・#1279）

種類ごとの上限は任意の検索に適用する。必須取得も予約数と総枠を超えて実行しない。
回数を数えるのはこの module の `ExtraRetrievalBudget` だけにし、種類を足すときも同じ枠から取る。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 種類の名前（記録の key）。
GUIDE_STEPS_KIND = "guide_steps"
GUIDE_REQUIRED_KIND = "guide_required"
REQUEST_COVERAGE_KIND = "request_coverage"


@dataclass
class ExtraRetrievalBudget:
    """1 回の回答の追加の検索の枠。回答ごとに作り、種類ごとに使った回数を数える。"""

    max_queries: int
    used: dict[str, int] = field(default_factory=dict)
    required_reserved: int = 0
    _required_active: bool = field(default=False, repr=False)

    @classmethod
    def from_settings(cls, settings: Any) -> ExtraRetrievalBudget:
        return cls(max_queries=max(0, int(getattr(settings, "extra_retrieval_max_queries", 0) or 0)))

    @property
    def hard_remaining(self) -> int:
        """実行済みの回数を引いた総枠。予約を消費済みとは数えない。"""
        return max(0, self.max_queries - sum(self.used.values()))

    @property
    def remaining(self) -> int:
        """任意の検索で使える残数。必須取得の予約を奪わない。"""
        return max(0, self.hard_remaining - self.required_reserved)

    @property
    def required_remaining(self) -> int:
        """必須取得に予約した、まだ実行していない回数。"""
        return min(max(0, self.required_reserved), self.hard_remaining)

    def reserve_required(self, count: int) -> int:
        """未処理の必須取得の予約を更新する。対象を削らず、回数だけ総枠内に保つ。"""
        self._required_active = True
        self.required_reserved = max(0, min(int(count), self.hard_remaining))
        return self.required_reserved

    def take_required(self, count: int = 1) -> bool:
        """必須取得の実行直前に回数を確保する。失敗・空の結果でも実行分は戻さない。"""
        requested = int(count)
        if requested <= 0 or requested > self.required_remaining:
            return False
        self.required_reserved -= requested
        self.used[GUIDE_REQUIRED_KIND] = self.used.get(GUIDE_REQUIRED_KIND, 0) + requested
        return True

    def release_required(self, count: int = 1) -> None:
        """実行せず原文を再利用できた予約などを解放する。実行済みの回数は変更しない。"""
        self.required_reserved = max(0, self.required_reserved - max(0, int(count)))

    def allowance(self, kind_limit: int) -> int:
        """種類の上限と枠の残りのうち小さい方（その種類が今から取れる回数）。"""
        return max(0, min(int(kind_limit), self.remaining))

    def take(self, kind: str, count: int = 1) -> None:
        """種類 ``kind`` が ``count`` 回の検索をした（枠の残りを超えては数えない）。"""
        taken = max(0, min(int(count), self.remaining))
        if taken:
            self.used[kind] = self.used.get(kind, 0) + taken

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"max_queries": self.max_queries, "used": dict(self.used), "remaining": self.remaining}
        if self._required_active:
            payload["required_reservation"] = {
                "reserved": self.required_remaining,
                "used": self.used.get(GUIDE_REQUIRED_KIND, 0),
                "hard_remaining": self.hard_remaining,
            }
        return payload


__all__ = ["GUIDE_REQUIRED_KIND", "GUIDE_STEPS_KIND", "REQUEST_COVERAGE_KIND", "ExtraRetrievalBudget"]
