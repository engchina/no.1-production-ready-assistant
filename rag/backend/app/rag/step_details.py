"""処理の経過の段の詳細（回答の記録の工程・パイプラインの工程。#2294 / #2341）。

`rag_engine` の工程の記録（``execution_steps[].details``）と、パイプラインの工程
（会話を踏まえた質問の書き換え・検索条件の抽出）の詳細を、
3 製品共通の詳細の形（``pr_backend_core.chat_progress`` の ``ChatProgressStepDetails``
= 画面の ``RunStepDetails``: 見出しと値 ``fields``・行 ``lines``・``truncated``）にそろえる。
上限で切り、秘密らしい値を伏せる（共通の規則）。旧い記録の行の list（#2294）は行として読む。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pr_backend_core.chat_progress import ChatProgressStepDetails, chat_progress_details


def step_details_model(value: object) -> ChatProgressStepDetails | None:
    """記録の詳細（共有の形の dict か旧い行の list）を詳細の model にする。無ければ None。"""

    if isinstance(value, list | tuple):
        data: dict[str, Any] = {"lines": [item for item in value if isinstance(item, str)]}
    elif isinstance(value, Mapping):
        data = {
            "fields": value.get("fields"),
            "lines": value.get("lines"),
            "code": value.get("code"),
            "truncated": value.get("truncated") is True or None,
        }
    else:
        return None
    details = ChatProgressStepDetails.model_validate(data)
    return None if details.is_empty() else details


def step_details_payload(value: object) -> dict[str, Any] | None:
    """記録の詳細を保存の JSON（値の無い項目は省く）にする。中身が無ければ None。"""

    details = step_details_model(value)
    return details.model_dump() if details is not None else None


def pipeline_step_details(
    fields: Iterable[tuple[str, object]] = (), lines: Iterable[str] = ()
) -> dict[str, Any] | None:
    """パイプラインの工程の詳細（見出しと値・行）を保存の JSON にする。中身が無ければ None。"""

    details = chat_progress_details(fields=list(fields), lines=list(lines))
    return details.model_dump() if details is not None else None


_CONDITION_OPERATORS = {"eq": "=", "gte": "≥", "lte": "≤"}


def history_rewrite_details(rewritten: str, history: Iterable[Any]) -> dict[str, Any] | None:
    """会話を踏まえた質問の書き換えの詳細（書き換えた質問・参照した会話の数）。"""

    turns = sum(1 for _ in history)
    return pipeline_step_details(
        fields=[
            ("参照した会話", f"{turns} 件の発言"),
            ("書き換えた質問", rewritten)
            if rewritten
            else ("結果", "書き換えなし（元の質問のまま）"),
        ]
    )


def field_filter_details(conditions: Iterable[Any]) -> dict[str, Any] | None:
    """質問から読み取った抽出項目の条件（項目・比べ方・値）。"""

    items = list(conditions)
    lines = [
        f"{getattr(item, 'name', '')} {_CONDITION_OPERATORS.get(getattr(item, 'op', ''), '=')} "
        f"{getattr(item, 'value', '')}"
        for item in items
    ]
    summary = f"{len(items)} 件" if items else "なし（条件で絞らずに検索します）"
    return pipeline_step_details(fields=[("読み取った条件", summary)], lines=lines)
