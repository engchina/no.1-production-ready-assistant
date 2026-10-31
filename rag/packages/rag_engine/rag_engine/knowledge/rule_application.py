"""候補の関連度と別に、段階・明示優先度・排他設定から適用を決める。自由文の意味上の矛盾は推測しない。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .runtime_knowledge import RuntimeRule

RULE_PURPOSES = ("confirmation", "generation", "both")
RULE_INSTRUCTION_KINDS = ("presentation", "requirement", "verification")


@dataclass(frozen=True)
class RuleApplications:
    selected: tuple[RuntimeRule, ...]
    decisions: tuple[dict[str, Any], ...]


def rule_version(payload: dict[str, Any]) -> str:
    """実行時の設定本文を特定する版。更新時刻の有無に依存させない。"""
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def select_rule_applications(candidates: Sequence[RuntimeRule], limit: int) -> RuleApplications:
    """同順位で異なる排他指示はそのグループ全体を保留し、管理設定の問題として記録する。"""
    decisions: list[dict[str, Any]] = []
    accepted: set[str] = set()
    ordered = sorted(candidates, key=lambda rule: -rule.priority)
    for stage in ("confirmation", "generation"):
        scoped = [rule for rule in ordered if rule.purpose in (stage, "both")]
        eligible = (
            [rule for rule in scoped if rule.content and rule.instruction_kind in RULE_INSTRUCTION_KINDS]
            if stage == "generation"
            else [rule for rule in scoped if rule.confirmation_signature]
        )
        groups: dict[str, list[RuntimeRule]] = {}
        for rule in eligible:
            if rule.exclusive_group:
                groups.setdefault(rule.exclusive_group, []).append(rule)
        rejected: dict[str, str] = {}
        for group in groups.values():
            highest = max(rule.priority for rule in group)
            leaders = [rule for rule in group if rule.priority == highest]
            if (
                len(
                    {
                        (rule.content, rule.instruction_kind) if stage == "generation" else (rule.confirmation_signature, "confirmation")
                        for rule in leaders
                    }
                )
                > 1
            ):
                rejected.update((rule.rule_id, "unresolved_exclusive_conflict") for rule in group)
            else:
                rejected.update((rule.rule_id, "lower_priority") for rule in group if rule.priority < highest)
        seen: set[tuple[str, str]] = set()
        count = 0
        for rule in ordered:
            reason = "selected"
            if rule not in scoped:
                reason = "other_purpose"
            elif stage == "generation" and rule.instruction_kind not in RULE_INSTRUCTION_KINDS:
                reason = "invalid_instruction_kind"
            elif stage == "generation" and not rule.content:
                reason = "no_generation_instruction"
            elif stage == "confirmation" and not rule.confirmation_signature:
                reason = "no_confirmation"
            elif rule.rule_id in rejected:
                reason = rejected[rule.rule_id]
            else:
                signature = (
                    (rule.instruction_kind, rule.content) if stage == "generation" else ("confirmation", rule.confirmation_signature)
                )
                if signature in seen:
                    reason = "duplicate_instruction"
                elif count >= max(1, limit):
                    reason = "application_budget"
                else:
                    seen.add(signature)
                    count += 1
                    accepted.add(rule.rule_id)
            decisions.append(
                {
                    "id": rule.rule_id,
                    "title": rule.title,
                    "version": rule.version,
                    "stage": stage,
                    "accepted": reason == "selected",
                    "reason": reason,
                    "priority": rule.priority,
                    "exclusive_group": rule.exclusive_group,
                }
            )
    return RuleApplications(tuple(rule for rule in ordered if rule.rule_id in accepted), tuple(decisions))
