"""補写内容の引用を、次回の実際の表示IDへ結び付ける。意味判定の代替にはしない。"""

from copy import deepcopy
from typing import Any

import pytest

from rag_engine.generation.request_recovery import relabel_recovery_reviews, request_recovery_plan


def omitted_review() -> dict[str, Any]:
    return {
        "request_id": "Q1",
        "status": "partial",
        "reviewed": True,
        "evidence_status": "available",
        "evidence_ids": ["stable-save"],
        "missing_content": "保存して確定する操作",
        "reason": "画面を開く説明の後に確定操作がありません。",
        "evidence_support": [{"evidence_id": "stable-save", "source": "文書A", "quote": "保存して確定します。"}],
    }


def test_omitted_action_feedback_uses_its_current_evidence_label() -> None:
    review = omitted_review()
    original = deepcopy(review)
    spans = [
        {"evidence_id": "stable-open", "label": "E9"},
        {"evidence_id": "stable-save", "label": "E2"},
    ]
    [decision] = request_recovery_plan([review], (), {"stable-open", "stable-save"})
    assert decision["action"] == "correction"
    [projected] = relabel_recovery_reviews([review], spans)
    assert projected["evidence_ids"] == ["E2"]
    assert projected["evidence_support"] == [{"evidence_id": "E2", "source": "文書A", "quote": "保存して確定します。"}]
    assert projected["missing_content"] == original["missing_content"]
    assert review == original


def test_joint_direct_material_keeps_every_quote_and_current_label() -> None:
    review = omitted_review()
    review["evidence_ids"].append("stable-condition")
    review["evidence_support"].append({"evidence_id": "stable-condition", "source": "文書B", "quote": "対象が有効な場合だけ確定できます。"})
    [projected] = relabel_recovery_reviews(
        [review], [{"evidence_id": "stable-save", "label": "E3"}, {"evidence_id": "stable-condition", "label": "E1"}]
    )
    assert projected["evidence_ids"] == ["E3", "E1"]
    assert [ref["quote"] for ref in projected["evidence_support"]] == [ref["quote"] for ref in review["evidence_support"]]


@pytest.mark.parametrize(
    "spans",
    [
        [{"evidence_id": "other", "label": "stable-save", "text": "保存して確定します。"}],
        [{"evidence_id": "stable-save", "label": "E2"}, {"evidence_id": "stable-save", "label": "E3"}],
        [{"evidence_id": "stable-save", "label": "E2"}, {"evidence_id": "other", "label": "E2"}],
    ],
)
def test_missing_or_ambiguous_identity_keeps_material_unverified(spans: list[dict[str, Any]]) -> None:
    [projected] = relabel_recovery_reviews([omitted_review()], spans)
    assert projected["evidence_status"] == "unknown"
    assert projected["evidence_ids"] == projected["evidence_support"] == []
    assert projected["missing_content"] == "保存して確定する操作"
    assert projected["evidence_binding"] == "unavailable"


def test_mismatched_support_identity_does_not_bind_the_answered_action() -> None:
    review = omitted_review()
    review["evidence_ids"] = ["stable-open"]
    [projected] = relabel_recovery_reviews(
        [review], [{"evidence_id": "stable-open", "label": "E1"}, {"evidence_id": "stable-save", "label": "E2"}]
    )
    assert projected["evidence_status"] == "unknown"
    assert projected["evidence_ids"] == []


def test_authentic_identity_projection_does_not_prove_missing_action_support() -> None:
    review = omitted_review()
    review["evidence_support"][0]["quote"] = "画面を開きます。"
    [projected] = relabel_recovery_reviews([review], [{"evidence_id": "stable-save", "label": "E2"}])
    # 原文の役割が違う誤判定は意味監査の未解決事項。ID投影だけで解決したとしない。
    assert projected["evidence_support"][0]["quote"] == "画面を開きます。"
    assert "semantic_supported" not in projected


@pytest.mark.parametrize("status", ["unknown", "missing"])
def test_non_available_verdict_is_preserved_without_material_references(status: str) -> None:
    review = omitted_review()
    review.update(evidence_status=status, evidence_ids=[], evidence_support=[])
    [projected] = relabel_recovery_reviews([review], [])
    assert projected["evidence_status"] == status
    assert projected["evidence_ids"] == []
