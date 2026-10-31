"""回答用の原文が明示されたページ・シート範囲に収まることを確認する。"""

from collections.abc import Mapping
from typing import Any

from app.schemas.search import parse_page_ranges

CONTEXT_PAGE_SCOPE = "context_page_ranges"


def positive(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(str(value))
    except (ValueError, TypeError):
        return None
    return number if number > 0 else None


def contained(
    document_id: str, metadata: Mapping[str, Any], scope: str, *, parent: bool = False
) -> bool:
    """重なりではなく全文の包含を要求する。範囲不明の本文は推測して採らない。"""
    if not scope:
        return True
    prefix = "parent_" if parent else ""
    for item in parse_page_ranges(scope):
        if item.document_id != document_id:
            continue
        if item.sheet_name is not None:
            if metadata.get("sheet_name") != item.sheet_name:
                continue
            if item.row_start is None and item.row_end is None:
                return True
            start, end = (
                positive(metadata.get(f"{prefix}row_start")),
                positive(metadata.get(f"{prefix}row_end")),
            )
            if (
                start is not None
                and end is not None
                and start <= end
                and (item.row_start is None or start >= item.row_start)
                and (item.row_end is None or end <= item.row_end)
            ):
                return True
            continue
        if item.page_start is None and item.page_end is None:
            return True
        start = positive(metadata.get(f"{prefix}page_start"))
        end = positive(metadata.get(f"{prefix}page_end"))
        if not parent:
            start = start or positive(metadata.get("page_number"))
            end = end or start
        if (
            start is not None
            and end is not None
            and start <= end
            and (item.page_start is None or start >= item.page_start)
            and (item.page_end is None or end <= item.page_end)
        ):
            return True
    return False
