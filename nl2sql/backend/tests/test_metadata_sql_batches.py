"""コメント・アノテーション・ドメインの SQL 生成の組の分け方（#1719）。"""

from __future__ import annotations

import pytest
from pr_system_settings.enterprise_ai_errors import EnterpriseAiIncomplete

from app.features.nl2sql.enterprise_ai_client import EnterpriseAiDirectError
from app.features.nl2sql.metadata_sql_batches import (
    filter_object_blocks,
    metadata_fallback_reason,
    metadata_sql_output_tokens,
    plan_metadata_sql_batches,
    split_object_blocks,
)


@pytest.mark.parametrize(
    ("columns", "expected"),
    [
        ([5] * 5, [[0, 1, 2, 3, 4]]),
        ([10] * 12, [list(range(10)), [10, 11]]),
        ([60, 60, 10], [[0], [1, 2]]),
        ([150, 5], [[0], [1]]),
        ([1] * 11, [list(range(10)), [10]]),
        ([], []),
    ],
    ids=["few", "by-columns", "column-limit", "big-table-alone", "object-limit", "empty"],
)
def test_plan_batches_keeps_order_and_limits(columns: list[int], expected: list[list[int]]) -> None:
    assert plan_metadata_sql_batches(columns) == expected


def test_filter_object_blocks_keeps_untagged_blocks() -> None:
    text = "OBJECT: APP.A\nCOLUMNS:\n- X: NUMBER\n\nOBJECT: APP.B\n- Y\n\n補足の文"
    assert [name for name, _block in split_object_blocks(text)] == ["APP.A", "APP.B", ""]
    assert filter_object_blocks(text, lambda name: name == "APP.B") == (
        "OBJECT: APP.B\n- Y\n\n補足の文"
    )


def test_output_tokens_grow_with_columns() -> None:
    assert metadata_sql_output_tokens(1, 10) < metadata_sql_output_tokens(10, 100)


def test_fallback_reason_names_output_limit() -> None:
    error = EnterpriseAiDirectError(
        "未完了",
        code="incomplete",
        incomplete=EnterpriseAiIncomplete(status="incomplete", reason="max_output_tokens"),
    )
    assert "出力の上限（tokens）" in metadata_fallback_reason(error)
    assert "SQL をそのまま使えません" in metadata_fallback_reason(ValueError("x"))
