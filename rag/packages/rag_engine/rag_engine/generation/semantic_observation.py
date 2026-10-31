"""未校正の意味判定を、採否に使う状態から分離する。"""

from collections.abc import Mapping
from typing import Any

from rag_engine.models.llm import GroundedAudit


def observation_policy() -> dict[str, str]:
    return {"mode": "diagnostic_only", "calibration": "not_calibrated"}


def diagnostic_only(timing: Mapping[str, Any]) -> bool:
    policy = timing.get("semantic_audit_policy")
    return isinstance(policy, Mapping) and policy.get("mode") == "diagnostic_only"


def unverified_projection(audit: GroundedAudit) -> GroundedAudit:
    """構造検証・実行記録は保ち、意味判定を根拠の有無や充足に採用しない。"""
    return audit.model_copy(
        deep=True,
        update={
            "reviews": [],
            "application_reviews": [],
            "guide_requirement_reviews": [],
            "unused_evidence_ids": [],
            "request_reviews": [
                review.model_copy(
                    update={
                        "status": "unknown",
                        "evidence_status": "unknown",
                        "evidence_ids": [],
                        "evidence_support": [],
                        "missing_content": "",
                        "reason": "",
                    }
                )
                for review in audit.request_reviews
            ],
            "summary_output_fields": None,
            "summary_basis": [],
        },
    )
