"""必須参照の取得記録を、業務ルールと分けて表示する。"""

from collections.abc import Mapping, Sequence
from typing import Any

from rag_engine.generation.answer_labels import answer_labels


def acquisition_notices(rows: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """候補本文を保持できない対象だけを、意味支持とは別の未完了として示す。"""
    steps = list(dict.fromkeys(step for row in rows if not row.get("retained_source_ids") for step in row.get("step_ids", ())))
    if not steps:
        return ()
    return (answer_labels().guide_acquisition_incomplete_template.format(steps="、".join(steps)),)


def prompt_acquisition_summary(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """未完了の参照を手順ごとに集約し、参照数に比例した入力増加を避ける。"""
    by_step: dict[str, dict[str, Any]] = {}
    reasons = {"disabled", "deadline", "required_budget_exhausted", "no_candidates", "body_not_retained"}
    for row in rows:
        if row.get("retained_source_ids"):
            continue
        reason = str(row.get("reason") or "unknown")
        if reason not in reasons:
            reason = "retrieval_error" if row.get("execution") == "error" else "unknown"
        for step in dict.fromkeys(row.get("step_ids", ())):
            entry = by_step.setdefault(step, {"step_id": step, "status": "unknown", "incomplete_ref_count": 0, "reasons": {}})
            entry["incomplete_ref_count"] += 1
            entry["reasons"][reason] = entry["reasons"].get(reason, 0) + 1
    return list(by_step.values())
