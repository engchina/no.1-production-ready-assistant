"""明示した照合方式、同義語グループ、all / any / exclude の判定と理由。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from .analysis import AnalyzedText, Span, normalize_text

MatchMode = Literal["exact", "term", "phrase", "substring", "ascii_word"]


@dataclass(frozen=True)
class Clause:
    """alternatives は一つの意味の別名。同じ group の寄与は順位層で一票にする。"""

    name: str
    field: str
    group: str
    alternatives: tuple[str, ...]
    mode: MatchMode = "term"
    weight: float = 1.0

    def __post_init__(self) -> None:
        if not self.name or not self.field or not self.group or not self.alternatives:
            raise ValueError("条件名・フィールド・同義語グループ・照合語を指定してください。")
        if any(not value.strip() for value in self.alternatives):
            raise ValueError("空の照合語は指定できません。")
        if self.mode not in ("exact", "term", "phrase", "substring", "ascii_word"):
            raise ValueError("未対応の照合方式です。")
        if not math.isfinite(self.weight) or self.weight < 0:
            raise ValueError("重みは有限の非負数にしてください。")


@dataclass(frozen=True)
class Condition:
    """必須・選択・除外。除外語は自然文の否定の理解を意味しない。"""

    must: tuple[Clause, ...] = ()
    should: tuple[Clause, ...] = ()
    exclude: tuple[Clause, ...] = ()
    minimum_should_match: int = 0

    def __post_init__(self) -> None:
        if not self.must and not self.should:
            raise ValueError("少なくとも一つの肯定条件を指定してください。")
        names = [clause.name for clause in (*self.must, *self.should, *self.exclude)]
        if len(names) != len(set(names)):
            raise ValueError("条件名を重複させないでください。")
        groups = {clause.group for clause in self.should}
        if not 0 <= self.minimum_should_match <= len(groups):
            raise ValueError("最低一致数は選択条件の異なる同義語グループ数以内にしてください。")
        if not self.must and not self.minimum_should_match:
            raise ValueError("選択条件だけの場合は最低一致数を指定してください。")


@dataclass(frozen=True)
class ClauseMatch:
    clause: Clause
    spans: tuple[Span, ...]

    @property
    def matched(self) -> bool:
        return bool(self.spans)


@dataclass(frozen=True)
class ConditionMatch:
    eligible: bool
    positive: tuple[ClauseMatch, ...]
    excluded: tuple[ClauseMatch, ...]
    unmet: tuple[str, ...]
    analysis_version: str


def match_clause(text: AnalyzedText, clause: Clause, *, max_spans: int = 8) -> ClauseMatch:
    """反復・別名の数はスコアにしない。返す原文範囲は上限内、重複なし。"""
    if max_spans < 1:
        raise ValueError("命中範囲の上限は 1 以上にしてください。")
    spans: set[Span] = set()
    words = {(span.start, span.end) for span in text.words} if clause.mode == "term" else set()
    forms = dict.fromkeys(normalize_text(value, text.policy) for value in clause.alternatives)
    for form in forms:
        if not form:
            continue
        for start, end in _occurrences(text.text, form):
            if clause.mode == "ascii_word" and (
                (start and text.text[start - 1] in "0123456789abcdefghijklmnopqrstuvwxyz_")
                or (end < len(text.text) and text.text[end] in "0123456789abcdefghijklmnopqrstuvwxyz_")
            ):
                continue
            if clause.mode == "exact" and (start != 0 or end != len(text.text)):
                continue
            if clause.mode == "term" and (start, end) not in words:
                continue
            if clause.mode == "phrase":
                # 英数字の途中だけの phrase は除く。無空白の日中語は設定した文字列の範囲で照合する。
                if (
                    form[0].isascii()
                    and form[0].isalnum()
                    and start
                    and text.text[start - 1].isascii()
                    and (text.text[start - 1].isalnum() or text.text[start - 1] == "_")
                ):
                    continue
                if (
                    form[-1].isascii()
                    and form[-1].isalnum()
                    and end < len(text.text)
                    and text.text[end].isascii()
                    and (text.text[end].isalnum() or text.text[end] == "_")
                ):
                    continue
            spans.add(text.source_span(start, end))
            if len(spans) >= max_spans:
                return ClauseMatch(clause, tuple(sorted(spans)))
    return ClauseMatch(clause, tuple(sorted(spans)))


def _occurrences(text: str, form: str):
    offset = 0
    while (start := text.find(form, offset)) >= 0:
        end = start + len(form)
        yield start, end
        offset = end


def evaluate(text: AnalyzedText, condition: Condition) -> ConditionMatch:
    """適用・同意・権限を決めず、照合条件を満たしたかだけを返す。"""
    must = tuple(match_clause(text, clause) for clause in condition.must)
    should = tuple(match_clause(text, clause) for clause in condition.should)
    excluded = tuple(match_clause(text, clause) for clause in condition.exclude)
    unmet = [match.clause.name for match in must if not match.matched]
    groups = {match.clause.group for match in should if match.matched}
    if len(groups) < condition.minimum_should_match:
        unmet.append("minimum_should_match")
    unmet.extend(match.clause.name for match in excluded if match.matched)
    return ConditionMatch(not unmet, (*must, *should), excluded, tuple(unmet), text.version)
