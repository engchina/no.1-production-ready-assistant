"""工程の span の利用者向けの名前（`app.rag.span_display`。#3272 / #3451）。

別の製品の画面（Agent の実行の詳細）は RAG の工程の定義を持たず、RAG が span に書いた
名前をそのまま出すので、backend の名前が画面の名前と同じであることを確かめる。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pr_backend_core.observability.tracing import SpanDisplay, SpanUpdate

from app.rag.span_display import (
    ANSWER_STEP_DISPLAY_NAMES,
    ANSWER_STEP_KIND,
    answer_step_display_name,
    span_display_name,
)


def _update(name: str) -> SpanUpdate:
    return SpanUpdate(
        span_id="1" * 16,
        trace_id="a" * 32,
        operation_name="rag.engine.stage",
        status="done",
        started_at=datetime(2026, 10, 26, tzinfo=UTC),
        display=SpanDisplay(kind=ANSWER_STEP_KIND, params={"name": name}),
    )


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("用語・同義語の確認", "一致する用語の別名を検索文に加える"),
        ("回答ルールの確認", "確認と回答に使うルールを選ぶ"),
    ],
    ids=["terms", "rules"],
)
def test_terms_and_rules_steps_have_their_own_names(name: str, label: str) -> None:
    """用語・同義語と回答ルールは別の工程の名前（設定画面のタブの名前と同じ語）にする（#3451）。"""
    assert answer_step_display_name(name) == label
    assert span_display_name(_update(name)) == label


def test_old_combined_step_name_is_not_known() -> None:
    """まとめた工程の名前（旧名）は持たない（知らない工程名はそのまま）。"""
    assert answer_step_display_name("用語・ルールの確認") == "用語・ルールの確認"
    assert "用語・ルールの確認" not in ANSWER_STEP_DISPLAY_NAMES


def test_search_readiness_step_is_not_known() -> None:
    """検索の前提の確認は工程にしない（#3495）。古い記録に残った工程名はそのまま出す。"""
    assert "検索の準備確認" not in ANSWER_STEP_DISPLAY_NAMES
    assert answer_step_display_name("検索の準備確認") == "検索の準備確認"
