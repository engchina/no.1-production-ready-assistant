"""JSON 列へ書く値の NaN / Infinity を null にする（#2043。ORA-40441 を防ぐ）。"""

import json
import math
from decimal import Decimal

from app.clients.oracle import _audit_json, _json_dumps


def test_finite_values_are_written_as_is() -> None:
    assert _json_dumps({"a": 1.5, "b": [1, "x"], "c": None}) == '{"a":1.5,"b":[1,"x"],"c":null}'


def test_non_finite_values_become_null() -> None:
    value = {
        "score": math.nan,
        "bbox": [1.0, math.inf, -math.inf],
        "nested": {"table": ({"cell": float("nan")},)},
        "decimal": Decimal("NaN"),
    }

    text = _json_dumps(value)

    assert json.loads(text) == {
        "score": None,
        "bbox": [1.0, None, None],
        "nested": {"table": [{"cell": None}]},
        "decimal": None,
    }
    # JSON の規格に合う（NaN / Infinity の字句を含まない）。
    assert "NaN" not in text and "Infinity" not in text


def test_audit_json_uses_the_same_rule() -> None:
    assert _audit_json({"x": math.nan}) == '{"x":null}'
