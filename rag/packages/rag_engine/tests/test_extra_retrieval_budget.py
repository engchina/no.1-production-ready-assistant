"""追加検索の枠は種類をまたいで一つで、総枠を超えて数えない。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from rag_engine.retrieval.extra_retrieval import ExtraRetrievalBudget


def test_allowance_take_and_payload_share_one_limit() -> None:
    budget = ExtraRetrievalBudget(8)
    assert budget.allowance(4) == 4
    budget.take("crag", 2)
    assert budget.allowance(10) == 6
    assert budget.to_payload() == {"max_queries": 8, "used": {"crag": 2}, "remaining": 6}
    budget.take("request_coverage", 10)
    assert budget.used == {"crag": 2, "request_coverage": 6}
    assert budget.remaining == 0


@pytest.mark.parametrize("maximum", [0, -1])
def test_nonpositive_limit_allows_no_calls(maximum: int) -> None:
    budget = ExtraRetrievalBudget(maximum)
    budget.take("crag", 1)
    assert budget.used == {}
    assert budget.remaining == 0


@pytest.mark.parametrize("value", [0, -1, None])
def test_settings_constructor_keeps_nonpositive_limits_disabled(value: object) -> None:
    assert ExtraRetrievalBudget.from_settings(SimpleNamespace(extra_retrieval_max_queries=value)).remaining == 0


def test_budget_instances_and_payload_usage_are_isolated() -> None:
    first, second = ExtraRetrievalBudget(4), ExtraRetrievalBudget(4)
    first.take("crag", 1)
    payload = first.to_payload()
    payload["used"]["crag"] = 99
    assert first.used == {"crag": 1}
    assert second.used == {}
