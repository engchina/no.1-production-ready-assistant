"""回答ルールの照合方針。候補順位と指示の適用優先度を区別する。"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from rag_engine.matching.analysis import analyze, normalize_text
from rag_engine.matching.conditions import Clause, Condition, ConditionMatch, evaluate, match_clause
from rag_engine.matching.ranking import CandidateMatch, rank_candidates

if TYPE_CHECKING:
    from .runtime_knowledge import RuntimeRule

# 既存の用途別方針を維持する。信頼度・管理者が定めた優先度ではない。
LABEL_WEIGHT = 3.0
CONTENT_WEIGHT = 1.0
CALLER_SELECTED_WEIGHT = 1000.0


@dataclass(frozen=True)
class RuleMatches:
    selected: tuple[RuntimeRule, ...]
    decisions: tuple[dict[str, Any], ...]


def rank_rules(
    question: str,
    rules: Sequence[RuntimeRule],
    limit: int,
    *,
    tokens: Callable[[str], Sequence[str]],
) -> RuleMatches:
    """保存済みの title/triggers・有効状態・入力順・上限の解釈を保つ。"""
    text = analyze(question)
    question_tokens = set(tokens(question))
    candidates: list[CandidateMatch] = []
    pinned: list[tuple[float, int, RuntimeRule]] = []
    by_key: dict[str, RuntimeRule] = {}
    labels_by_key: dict[str, list[str]] = {}
    for index, rule in enumerate(rules):
        if not rule.active:
            continue
        if "pinned" in rule.tags:
            # 既存の呼出側で選択済みのルール。単語の命中・同意の証拠を捏造しない。
            pinned.append((CALLER_SELECTED_WEIGHT, index, rule))
            continue
        clauses = tuple(
            Clause(
                name=f"label:{position}",
                field="title" if label == rule.title else "triggers",
                group=f"label:{normalize_text(label)}",
                alternatives=(label,),
                mode="ascii_word" if re.fullmatch(r"[0-9a-z_]+", normalize_text(label)) else "substring",
                weight=LABEL_WEIGHT,
            )
            for position, label in enumerate(rule.labels())
        )
        condition = (
            evaluate(text, Condition(should=clauses, minimum_should_match=1))
            if clauses
            else (ConditionMatch(False, (), (), ("labels",), text.version))
        )
        shared = tuple(sorted(question_tokens & set(tokens(rule.content))))
        bonus = (
            (match_clause(text, Clause("shared_content", "content", "shared_content", shared, mode="substring", weight=CONTENT_WEIGHT)),)
            if shared
            else ()
        )
        # OR 条件の不命中ラベルは保持しない。未充足理由は残し、多数候補の診断の複製を抑える。
        condition = replace(condition, positive=tuple(match for match in condition.positive if match.matched))
        if not condition.eligible and not bonus:
            continue
        candidates.append(CandidateMatch(rule.rule_id, index, condition, bonus))
        by_key[rule.rule_id] = rule
        labels_by_key[rule.rule_id] = [match.clause.alternatives[0] for match in condition.positive if match.matched]

    ranked = rank_candidates(tuple(candidates), limit=max(1, len(candidates)))
    eligible = [
        (item.candidate.score, item.candidate.order, by_key[item.candidate.key]) for item in ranked if item.candidate.condition.eligible
    ]
    ordered = sorted([*pinned, *eligible], key=lambda item: (-item[0], item[1]))
    ranks = {rule.rule_id: index + 1 for index, (_, _, rule) in enumerate(ordered)}
    selected = tuple(rule for _, _, rule in ordered[: max(1, limit)])
    selected_ids = {rule.rule_id for rule in selected}
    decisions: list[dict[str, Any]] = []
    for item in ranked:
        candidate = item.candidate
        labels = labels_by_key[candidate.key]
        if not labels and not candidate.bonuses:
            continue
        accepted = candidate.key in selected_ids
        reason = "explicit_label" if accepted else "limit" if labels else "shared_words_only"
        decisions.append(
            {
                **item.explanation(),
                "source": "rule",
                "key": candidate.key,
                "matched_labels": labels,
                "accepted": accepted,
                "selected": accepted,
                "reason": reason,
                "rank": ranks.get(candidate.key),
                "offset_source": "question_input",
            }
        )
    for _, _, rule in pinned:
        decisions.append(
            {
                "source": "rule",
                "key": rule.rule_id,
                "matched_labels": [],
                "accepted": rule.rule_id in selected_ids,
                "selected": rule.rule_id in selected_ids,
                "reason": "caller_selected" if rule.rule_id in selected_ids else "limit",
                "score_kind": "ranking_only",
                "score": CALLER_SELECTED_WEIGHT,
                "rank": ranks[rule.rule_id],
                "contributions": [],
                "conditions": [],
                "unmet": [],
                "analysis_version": text.version,
            }
        )
    return RuleMatches(selected, tuple(decisions))
