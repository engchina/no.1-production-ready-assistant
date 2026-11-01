"""処理の経過の段の詳細（回答の記録の工程・パイプラインの工程。#2341）。合成の値で確かめる。"""

from __future__ import annotations

from types import SimpleNamespace

from app.rag.step_details import (
    field_filter_details,
    history_rewrite_details,
    step_details_payload,
)


def test_payload_reads_new_and_legacy_records_and_drops_empty() -> None:
    assert step_details_payload(
        {
            "fields": [{"label": "件数", "value": "3 件"}, {"label": "空", "value": ""}],
            "lines": ["行"],
        }
    ) == {"fields": [{"label": "件数", "value": "3 件"}], "lines": ["行"]}
    # 旧い記録（#2294）の行の list は行として読む。
    assert step_details_payload(["行 1", 2, "行 2"]) == {"lines": ["行 1", "行 2"]}
    assert step_details_payload({"fields": [], "lines": []}) is None
    assert step_details_payload("壊れた値") is None
    assert step_details_payload(None) is None


def test_payload_bounds_and_redacts() -> None:
    payload = step_details_payload(
        {"lines": [f"行 {index}" for index in range(30)] + ["Bearer abc.def"]}
    )
    assert payload is not None
    assert len(payload["lines"]) == 20
    assert payload["truncated"] is True
    redacted = step_details_payload({"fields": [{"label": "接続", "value": "password=hunter2"}]})
    assert redacted == {"fields": [{"label": "接続", "value": "password=***"}]}


def test_history_rewrite_details() -> None:
    history = [
        SimpleNamespace(role="USER", content="a"),
        SimpleNamespace(role="ASSISTANT", content="b"),
    ]
    assert history_rewrite_details("合成の書き換えた質問", history) == {
        "fields": [
            {"label": "参照した会話", "value": "2 件の発言"},
            {"label": "書き換えた質問", "value": "合成の書き換えた質問"},
        ]
    }
    assert history_rewrite_details("", history) == {
        "fields": [
            {"label": "参照した会話", "value": "2 件の発言"},
            {"label": "結果", "value": "書き換えなし（元の質問のまま）"},
        ]
    }


def test_field_filter_details() -> None:
    conditions = [
        SimpleNamespace(name="項目 A", op="eq", value="値 1"),
        SimpleNamespace(name="項目 B", op="gte", value="10"),
    ]
    assert field_filter_details(conditions) == {
        "fields": [{"label": "読み取った条件", "value": "2 件"}],
        "lines": ["項目 A = 値 1", "項目 B ≥ 10"],
    }
    assert field_filter_details([]) == {
        "fields": [{"label": "読み取った条件", "value": "なし（条件で絞らずに検索します）"}]
    }
