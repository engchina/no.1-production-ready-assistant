"""原文定位と、意味支持の実行記録を混同しない表示用の情報。採否は変更しない。"""

from collections.abc import Mapping
from typing import Any


def citation_support_display(trace: object) -> dict[str, str] | None:
    """回答全体で意味支持を確認できた場合だけ表示上の確認済みを許す。旧記録は推測しない。"""
    if not isinstance(trace, Mapping):
        return None
    policy = trace.get("semantic_audit_policy")
    diagnostic = isinstance(policy, Mapping) and policy.get("mode") == "diagnostic_only"
    rounds = trace.get("rounds")
    adopted = (
        next(
            (row for row in reversed(rounds) if isinstance(row, Mapping) and row.get("accepted")),
            None,
        )
        if isinstance(rounds, list)
        else None
    )
    if not diagnostic and (adopted is None or "review_execution" not in adopted):
        return None
    execution = str(adopted.get("review_execution", "not_run")) if adopted else "not_run"
    dimensions: Any = adopted.get("validation_dimensions", {}) if adopted else {}
    support = dimensions.get("claim_support", {}) if isinstance(dimensions, Mapping) else {}
    rows = support.get("items") if isinstance(support, Mapping) else None
    completed = (
        execution == "completed" and support.get("execution") == "completed"
        if isinstance(support, Mapping)
        else False
    )
    supported = (
        completed
        and not diagnostic
        and isinstance(rows, list)
        and bool(rows)
        and all(isinstance(row, Mapping) and row.get("status") == "supported" for row in rows)
    )
    return {
        "scope": "answer",
        "execution": execution,
        "status": "supported" if supported else "unknown",
        "policy": "diagnostic_only" if diagnostic else "recorded",
    }
