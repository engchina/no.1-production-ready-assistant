"""SQL の結果を browser へ渡すときも大整数の桁を保持する（#3171）。"""

import json

import pytest

from app.features.nl2sql.models import QueryResults


@pytest.mark.parametrize("value", [2**53, 2**53 + 1, -(2**53), -(2**53 + 1), 10**37 + 1])
def test_large_integer_is_text_only_in_json(value: int) -> None:
    result = QueryResults(columns=["value"], rows=[{"value": value}], total=1)
    assert result.rows[0]["value"] == value
    assert result.model_dump()["rows"][0]["value"] == value
    assert json.loads(result.model_dump_json())["rows"][0]["value"] == str(value)
    assert result.model_dump(mode="json")["rows"][0]["value"] == str(value)


@pytest.mark.parametrize("value", [2**53 - 1, -(2**53 - 1), 0, 42, True, False, None, 1.25, "001"])
def test_other_values_keep_their_json_types(value: object) -> None:
    result = QueryResults(columns=["value"], rows=[{"value": value}], total=1)
    actual = json.loads(result.model_dump_json())["rows"][0]["value"]
    assert actual == value
    assert type(actual) is type(value)
