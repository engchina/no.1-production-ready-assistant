"""業務ガイドの分からなかった条件を、回答の根拠で決める（#2404）。

業務ガイドの照合（``support_guide_runtime``）は質問の文と利用者が渡した値だけで条件を決める。
回答の検索の後に、rag_engine が根拠の画面の図の項目の種類で値の決まる条件を決め
（``generation.form_field_kinds``）、回答の記録の ``form_field_kinds.resolved_conditions`` に残す。
ここでは、

- 照合で分からなかった選択の条件を rag_engine に渡す形にする（``evidence_condition_candidates``
  と ``guide_evidence_conditions``。選択肢が項目の種類に対応するかは rag_engine が言語資源で
  決める）。
- 根拠で条件が決まり得る値の組ごとに、その条件を既知にした業務ガイドの手順の計画（残る手順と必須か・
  業務ガイドのルールの本文）を前もって作り、rag_engine に渡す（``evidence_plans`` と
  ``guide_evidence_plans``。#2689）。rag_engine は条件が決まった時点でその計画に切り替え、外れた分岐
  （「分からない場合」の手順など）を必須から外し、決まった分岐の手順を必須にする。
- 回答の後に、決まった条件を業務ガイドの要約の「分かっている条件」（出所 ``document``）へ移し、
  分岐で答える理由の条件がすべて決まったら「答える」にする（``with_evidence_conditions``）。

業務ガイドの schema は変えない。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from rag_engine.generation.form_field_kinds import condition_kind_options

from app.rag.support_guide_runtime import (
    ConditionState,
    GuideMatch,
    guide_rule,
    guide_step_plan,
)

# rag_support_guide（回答の設定）の key。照合で分からなかった選択の条件の選択肢と言い換え。
EVIDENCE_CONDITIONS_KEY = "evidence_conditions"
# rag_support_guide の key。根拠で条件が決まったときの手順の計画（#2689）。
EVIDENCE_PLANS_KEY = "evidence_plans"
# 根拠で決まった条件の出所（業務ガイドの条件の出所の「資料」と同じ語）。
EVIDENCE_SOURCE = "document"


def evidence_condition_candidates(match: GuideMatch) -> list[dict[str, Any]]:
    """照合で分からなかった（矛盾していない）選択の条件の、選択肢と言い換え。"""
    candidates: list[dict[str, Any]] = []
    for state in match.states:
        condition = state.condition
        if state.known or state.conflicting or condition.type != "enum":
            continue
        candidates.append(
            {
                "id": condition.id,
                "label": condition.label,
                "options": [
                    {"value": value, "aliases": list(condition.value_aliases.get(value, []))}
                    for value in condition.allowed_values
                ],
            }
        )
    return candidates


def guide_evidence_conditions(
    guide: Mapping[str, object] | None,
) -> tuple[tuple[str, str, tuple[tuple[str, tuple[str, ...]], ...]], ...]:
    """rag_engine の ``evidence_conditions`` の形。

    (条件の id, 名前, ((選択肢, (言い換え, ...)), ...)) の並び。
    """
    items = (guide or {}).get(EVIDENCE_CONDITIONS_KEY)
    if not isinstance(items, list):
        return ()
    result: list[tuple[str, str, tuple[tuple[str, tuple[str, ...]], ...]]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        options = tuple(
            (
                str(option.get("value") or ""),
                tuple(str(alias) for alias in option.get("aliases") or [] if str(alias)),
            )
            for option in item.get("options") or []
            if isinstance(option, Mapping) and str(option.get("value") or "")
        )
        condition_id, label = str(item.get("id") or ""), str(item.get("label") or "")
        if condition_id and label and options:
            result.append((condition_id, label, options))
    return tuple(result)


def with_known_conditions(match: GuideMatch, values: Mapping[str, str]) -> GuideMatch:
    """``values``（条件の id → 値）の条件を資料で分かった（出所 ``document``）ことにした照合の写し。

    不明の条件から外し、分岐で答える（branch）としていて不明の条件が無くなれば「答える」（answer）にする
    （``with_evidence_conditions`` の要約と同じ決め方）。
    """
    states = tuple(
        ConditionState(state.condition, values[state.condition.id], EVIDENCE_SOURCE)
        if state.condition.id in values
        else state
        for state in match.states
    )
    unknown = tuple(condition for condition in match.unknown if condition.id not in values)
    decision = "answer" if match.decision == "branch" and not unknown else match.decision
    return replace(match, states=states, unknown=unknown, decision=decision)


def evidence_plans(match: GuideMatch) -> list[dict[str, Any]]:
    """根拠で決まり得る条件の値の組ごとの手順の計画（#2689）。

    rag_engine は根拠の画面の図の項目の種類が 1 つに決まったとき、その種類に対応する選択肢で
    条件を決める（``form_field_kinds.resolve_conditions``）。種類ごとに決まる値の組を同じ規則で
    求め、その組を既知にした照合から、残る手順と必須か（``guide_step_plan``）と業務ガイドの
    ルールの本文（``guide_rule``）を作る。
    """
    candidates = evidence_condition_candidates(match)
    mappings = {
        str(item["id"]): condition_kind_options(
            (option["value"], tuple(option["aliases"])) for option in item["options"]
        )
        for item in candidates
    }
    kinds = dict.fromkeys(kind for mapping in mappings.values() for kind in mapping)
    plans: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for kind in kinds:
        values = {
            condition_id: mapping[kind]
            for condition_id, mapping in mappings.items()
            if kind in mapping
        }
        key = tuple(sorted(values.items()))
        if not values or key in seen:
            continue
        seen.add(key)
        resolved = with_known_conditions(match, values)
        rule = guide_rule(resolved)
        plans.append(
            {
                "conditions": [{"id": cid, "value": value} for cid, value in key],
                "steps": [
                    {"id": step["id"], "required": step["required"]}
                    for step in guide_step_plan(resolved)
                ],
                "rule_id": rule["id"],
                "rule_content": rule["content"],
            }
        )
    return plans


def guide_evidence_plans(
    guide: Mapping[str, object] | None,
) -> tuple[tuple[tuple[tuple[str, str], ...], tuple[tuple[str, bool], ...], str, str], ...]:
    """rag_engine の ``evidence_plans`` の形。

    (決まった条件の (id, 値) の列, 残る手順の (id, 必須か) の列, 業務ガイドのルールの id,
    そのルールの本文)。
    """
    items = (guide or {}).get(EVIDENCE_PLANS_KEY)
    if not isinstance(items, list):
        return ()
    result: list[tuple[tuple[tuple[str, str], ...], tuple[tuple[str, bool], ...], str, str]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        conditions = tuple(
            (str(entry.get("id") or ""), str(entry.get("value") or ""))
            for entry in item.get("conditions") or []
            if isinstance(entry, Mapping) and entry.get("id") and entry.get("value")
        )
        steps = tuple(
            (str(entry.get("id")), entry.get("required") is not False)
            for entry in item.get("steps") or []
            if isinstance(entry, Mapping) and entry.get("id")
        )
        if conditions:
            result.append(
                (
                    conditions,
                    steps,
                    str(item.get("rule_id") or ""),
                    str(item.get("rule_content") or ""),
                )
            )
    return tuple(result)


def resolved_evidence_conditions(diagnostics: Mapping[str, Any]) -> list[dict[str, str]]:
    """回答の記録の、根拠の画面の図の項目の種類で決まった条件（id・名前・値・種類）。"""
    payload = diagnostics.get("form_field_kinds")
    items = payload.get("resolved_conditions") if isinstance(payload, Mapping) else None
    if not isinstance(items, list):
        return []
    return [
        {key: str(item.get(key) or "") for key in ("id", "label", "value", "kind")}
        for item in items
        if isinstance(item, Mapping) and item.get("id") and item.get("value")
    ]


def with_evidence_conditions(
    guide: Mapping[str, Any], diagnostics: Mapping[str, Any]
) -> dict[str, Any]:
    """根拠で決まった条件を業務ガイドの要約の「分かっている条件」へ移したコピー。

    分からなかった条件がすべて決まり、分岐で答える（branch）としていたなら「答える」（answer）にする。
    確認・引き継ぎ（clarify / handoff）は回答を作らないので、ここには来ない。
    """
    resolved = {item["id"]: item for item in resolved_evidence_conditions(diagnostics)}
    if not resolved:
        return dict(guide)
    known = [item for item in guide.get("known_conditions") or [] if isinstance(item, Mapping)]
    known_ids = {str(item.get("id")) for item in known}
    added = [
        {
            "id": item["id"],
            "label": item["label"],
            "value": item["value"],
            "source": EVIDENCE_SOURCE,
            "state": "known",
        }
        for item in resolved.values()
        if item["id"] not in known_ids
    ]
    unknown = [
        item
        for item in guide.get("unknown_conditions") or []
        if isinstance(item, Mapping) and str(item.get("id")) not in resolved
    ]
    result = {**guide, "known_conditions": [*known, *added], "unknown_conditions": unknown}
    if guide.get("decision") == "branch" and not unknown:
        result["decision"] = "answer"
    return result


__all__ = [
    "EVIDENCE_CONDITIONS_KEY",
    "EVIDENCE_PLANS_KEY",
    "EVIDENCE_SOURCE",
    "evidence_condition_candidates",
    "evidence_plans",
    "guide_evidence_conditions",
    "guide_evidence_plans",
    "resolved_evidence_conditions",
    "with_evidence_conditions",
    "with_known_conditions",
]
