"""採用した回答の三つの検証軸を混同しない。"""

from app.rag.answer_engine import adopted_review_contract


def test_adopted_dimensions_preserve_separate_results_and_execution() -> None:
    dimensions = {
        "citation_validity": {"execution": "completed", "items": [{"status": "matched"}]},
        "claim_support": {"execution": "timed_out", "items": [{"status": "unknown"}]},
        "answer_completeness": {"execution": "timed_out", "requests": []},
    }
    result = adopted_review_contract(
        {
            "rounds": [
                {
                    "accepted": True,
                    "review_execution": "timed_out",
                    "validation_dimensions": dimensions,
                    "items": [],
                },
                {"accepted": False, "review_execution": "completed", "items": []},
            ]
        }
    )
    assert result["execution"] == "timed_out"
    assert result["dimensions"] == dimensions


def test_old_records_do_not_invent_completed_dimensions() -> None:
    assert adopted_review_contract(None) == {"execution": "not_run", "items": []}
    assert "dimensions" not in adopted_review_contract({"rounds": [{"accepted": True}]})
