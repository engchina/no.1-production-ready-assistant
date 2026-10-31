"""必須の取得を予約しても追加検索の総枠を増やさない。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rag_engine.retrieval.extra_retrieval import ExtraRetrievalBudget


def test_required_reservation_protects_five_of_eight_slots() -> None:
    budget = ExtraRetrievalBudget(8)
    assert budget.reserve_required(5) == 5
    assert budget.remaining == 3
    assert budget.allowance(4) == 3

    budget.take("crag", 2)
    budget.take("request_coverage", 10)
    assert budget.used == {"crag": 2, "request_coverage": 1}
    assert budget.remaining == 0
    assert budget.required_remaining == 5
    assert budget.take_required(5)
    assert not budget.take_required()
    assert sum(budget.used.values()) == 8
    assert budget.used["guide_required"] == 5
    reservation = budget.to_payload()["required_reservation"]
    assert reservation["reserved"] == 0
    assert reservation["used"] == 5
    assert reservation["hard_remaining"] == 0


def test_all_slots_reserved_still_allow_required_claims() -> None:
    budget = ExtraRetrievalBudget(8)
    assert budget.reserve_required(8) == 8
    budget.take("guide_steps", 4)
    assert budget.used == {}
    assert budget.allowance(4) == 0
    assert budget.take_required(8)
    assert budget.used == {"guide_required": 8}
    assert budget.required_remaining == 0
    assert budget.remaining == 0


def test_required_claim_is_atomic_when_reservation_is_insufficient() -> None:
    budget = ExtraRetrievalBudget(8)
    budget.reserve_required(3)
    assert not budget.take_required(4)
    assert budget.used == {}
    assert budget.required_remaining == 3
    assert budget.remaining == 5


def test_failed_required_attempt_remains_counted() -> None:
    budget = ExtraRetrievalBudget(1)
    budget.reserve_required(1)

    def retrieve() -> None:
        assert budget.take_required()
        raise TimeoutError("合成の取得失敗")

    with pytest.raises(TimeoutError):
        retrieve()
    assert budget.used == {"guide_required": 1}
    assert budget.reserve_required(1) == 0
    assert not budget.take_required()


def test_reuse_releases_a_slot_and_later_queue_can_reserve_it() -> None:
    budget = ExtraRetrievalBudget(8)
    budget.reserve_required(8)
    budget.release_required()
    assert budget.required_remaining == 7
    assert budget.remaining == 1
    assert budget.used == {}
    assert budget.take_required(7)
    assert budget.reserve_required(1) == 1
    assert budget.take_required()
    assert budget.used == {"guide_required": 8}


def test_long_required_queue_cannot_expand_the_hard_limit() -> None:
    planned_targets = tuple(f"target-{index}" for index in range(12))
    budget = ExtraRetrievalBudget(8)
    assert budget.reserve_required(len(planned_targets)) == 8
    claimed = [target for target in planned_targets if budget.take_required()]
    assert len(claimed) == 8
    assert len(planned_targets) == 12
    assert budget.used == {"guide_required": 8}
    assert budget.reserve_required(4) == 0


def test_no_guide_preserves_legacy_allowance_take_and_payload() -> None:
    budget = ExtraRetrievalBudget(8)
    assert budget.allowance(4) == 4
    budget.take("crag", 2)
    assert budget.allowance(10) == 6
    assert budget.to_payload() == {"max_queries": 8, "used": {"crag": 2}, "remaining": 6}
    budget.take("request_coverage", 10)
    assert budget.used == {"crag": 2, "request_coverage": 6}
    assert budget.remaining == 0
    assert "required_reservation" not in budget.to_payload()


def test_spent_budget_cannot_create_a_required_reservation() -> None:
    budget = ExtraRetrievalBudget(8)
    budget.take("crag", 8)
    assert budget.reserve_required(5) == 0
    assert not budget.take_required()
    assert budget.used == {"crag": 8}
    assert budget.to_payload()["required_reservation"]["hard_remaining"] == 0


@pytest.mark.parametrize("maximum", [0, -1])
def test_nonpositive_hard_limit_allows_no_calls(maximum: int) -> None:
    budget = ExtraRetrievalBudget(maximum)
    assert budget.reserve_required(5) == 0
    assert not budget.take_required()
    budget.take("crag", 1)
    assert budget.used == {}
    assert budget.remaining == budget.required_remaining == 0


@pytest.mark.parametrize("count", [0, -1])
def test_nonpositive_claim_and_release_do_not_consume_or_release(count: int) -> None:
    budget = ExtraRetrievalBudget(8)
    budget.reserve_required(5)
    assert not budget.take_required(count)
    budget.release_required(count)
    budget.take("crag", count)
    assert budget.required_remaining == 5
    assert budget.used == {}
    assert budget.allowance(count) == 0


@pytest.mark.parametrize("count", [0, -1])
def test_nonpositive_reservation_clears_only_uncalled_slots(count: int) -> None:
    budget = ExtraRetrievalBudget(8)
    budget.reserve_required(5)
    assert budget.take_required()
    assert budget.reserve_required(count) == 0
    assert budget.used == {"guide_required": 1}
    assert budget.required_remaining == 0
    assert budget.remaining == 7


def test_reservation_updates_instead_of_accumulating_or_resetting_usage() -> None:
    budget = ExtraRetrievalBudget(8)
    budget.reserve_required(5)
    assert budget.take_required(2)
    assert budget.reserve_required(2) == 2
    assert budget.required_remaining == 2
    assert budget.remaining == 4
    assert budget.reserve_required(20) == 6
    assert budget.used == {"guide_required": 2}
    assert budget.take_required(6)
    assert sum(budget.used.values()) == 8


def test_release_cannot_refund_executed_calls_or_invent_capacity() -> None:
    budget = ExtraRetrievalBudget(8)
    budget.reserve_required(5)
    assert budget.take_required(2)
    budget.release_required(99)
    assert budget.required_remaining == 0
    assert budget.used == {"guide_required": 2}
    assert budget.remaining == 6
    assert budget.reserve_required(99) == 6


def test_budget_instances_and_payload_usage_are_isolated() -> None:
    first = ExtraRetrievalBudget(8)
    second = ExtraRetrievalBudget(8)
    first.reserve_required(5)
    assert first.take_required()
    payload = first.to_payload()
    payload["used"]["guide_required"] = 99
    assert first.used == {"guide_required": 1}
    assert second.used == {}
    assert second.remaining == 8
    assert "required_reservation" not in second.to_payload()


def test_settings_constructor_keeps_nonpositive_limits_disabled() -> None:
    budget = ExtraRetrievalBudget.from_settings(SimpleNamespace(extra_retrieval_max_queries=-1))
    assert budget.max_queries == 0
    assert budget.reserve_required(1) == 0
    assert not budget.take_required()
