"""候補順位と説明。スコアを信頼度・指示優先度に昇格させない。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .conditions import ClauseMatch, ConditionMatch


@dataclass(frozen=True)
class Contribution:
    match: ClauseMatch
    score: float


@dataclass(frozen=True)
class CandidateMatch:
    key: str
    order: int
    condition: ConditionMatch
    bonuses: tuple[ClauseMatch, ...] = ()

    @property
    def contributions(self) -> tuple[Contribution, ...]:
        # dis_max と同じく一つの意味の最大の寄与を使う。別名・繰り返しで票を増やさない。
        best: dict[str, ClauseMatch] = {}
        for match in (*self.condition.positive, *self.bonuses):
            if match.matched and (match.clause.group not in best or match.clause.weight > best[match.clause.group].clause.weight):
                best[match.clause.group] = match
        return tuple(Contribution(match, match.clause.weight) for match in best.values())

    @property
    def score(self) -> float:
        return sum(item.score for item in self.contributions) if self.condition.eligible else 0.0


@dataclass(frozen=True)
class RankedMatch:
    candidate: CandidateMatch
    rank: int | None
    selected: bool
    reason: str

    def explanation(self) -> dict[str, Any]:
        """原文は複製せず範囲を返す。権限付き記録の保存・表示は呼出側が制御する。"""
        candidate = self.candidate
        return {
            "key": candidate.key,
            "analysis_version": candidate.condition.analysis_version,
            "score_kind": "ranking_only",
            "score": candidate.score,
            "rank": self.rank,
            "selected": self.selected,
            "reason": self.reason,
            "unmet": list(candidate.condition.unmet),
            "contributions": [
                {
                    "condition": item.match.clause.name,
                    "field": item.match.clause.field,
                    "group": item.match.clause.group,
                    "mode": item.match.clause.mode,
                    "score": item.score,
                    "spans": [{"start": span.start, "end": span.end} for span in item.match.spans],
                }
                for item in candidate.contributions
                if candidate.condition.eligible
            ],
            "conditions": [
                {
                    "name": item.clause.name,
                    "matched": item.matched,
                    "spans": [{"start": span.start, "end": span.end} for span in item.spans],
                }
                for item in (*candidate.condition.positive, *candidate.condition.excluded)
            ],
        }


def rank_candidates(candidates: tuple[CandidateMatch, ...], *, limit: int) -> tuple[RankedMatch, ...]:
    if limit < 1:
        raise ValueError("候補上限は 1 以上にしてください。")
    if len({item.key for item in candidates}) != len(candidates):
        raise ValueError("候補キーを重複させないでください。")
    eligible = sorted((item for item in candidates if item.condition.eligible), key=lambda item: (-item.score, item.order, item.key))
    ranks = {item.key: index + 1 for index, item in enumerate(eligible)}
    return tuple(
        RankedMatch(
            item,
            ranks.get(item.key),
            ranks.get(item.key, limit + 1) <= limit,
            "unmet_condition" if not item.condition.eligible else "selected" if ranks[item.key] <= limit else "limit",
        )
        for item in candidates
    )
