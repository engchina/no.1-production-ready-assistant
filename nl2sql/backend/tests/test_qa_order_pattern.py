"""並び順の向きと NULLS 指定を重複させない。"""

import pytest

from app.features.nl2sql.ontology_build import _qa_sql_pattern_from_pair
from app.features.nl2sql.ontology_models import QaPair


@pytest.mark.parametrize(
    "order,expected",
    [
        ("VALUE DESC", "VALUE desc"),
        ("VALUE ASC", "VALUE asc"),
        ("VALUE", "VALUE asc"),
        ("VALUE DESC NULLS LAST", "VALUE desc NULLS LAST"),
        ("VALUE ASC NULLS FIRST", "VALUE asc NULLS FIRST"),
    ],
)
def test_order_direction_appears_once(order: str, expected: str) -> None:
    pair = QaPair(question="値を並べる", sql=f"SELECT VALUE FROM APP.TABLE_A ORDER BY {order}")
    pattern = _qa_sql_pattern_from_pair(pair)
    assert pattern["order_by"] == [expected]
    assert expected in " ".join(pattern["generation_rules_ja"])
