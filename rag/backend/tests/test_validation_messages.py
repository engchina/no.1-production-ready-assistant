"""入力の検証エラー（422）を 3 製品共通の日本語の文で返すことのテスト（#979 / #1065）。

文の整形そのものは `pr_backend_core.api.validation` のテストが確かめる。ここでは RAG の
handler が共通の整形と problem 契約を使うことを、利用者が JSON を直接書く品質評価で確かめる。
"""

from typing import Any

import pytest

from app.main import app
from tests.support import AsgiTestClient

client = AsgiTestClient(app)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"cases": [{"id": "a"}]}, "cases[0].query: 必須の項目です。入力してください。"),
        (
            {"cases": [{"id": "a", "query": "q", "relevant_document_ids": "doc"}]},
            "cases[0].relevant_document_ids: 配列（[ ]）で指定してください。",
        ),
        ({"cases": [{"id": "a", "query": "q"}], "top_k": "x"}, "top_k: 整数で入力してください。"),
        (
            {"cases": [{"id": "a", "query": "q"}], "suite": "loose"},
            "suite: 指定できる値ではありません（指定できる値: 'standard'、'strict'）。",
        ),
    ],
    ids=["missing", "list-type", "int-parsing", "literal"],
)
def test_evaluation_validation_errors_are_japanese(body: dict[str, Any], expected: str) -> None:
    """JSON を直接書く品質評価の入力の誤りを、JSON の位置と日本語の文で返す。"""
    response = client.post("/api/evaluation/jobs/run", json=body)

    assert response.status_code == 422
    payload = response.json()
    assert expected in payload["error_messages"]
    assert payload["error_code"] == "REQUEST_VALIDATION_FAILED"
    assert payload["problem"]["field_errors"][0]["raw_location"].startswith("body.")


def test_custom_value_errors_are_returned_as_written() -> None:
    """自前の検証の日本語の文は、`Value error, ` の接頭辞と位置を付けずにそのまま返す。"""
    response = client.post(
        "/api/evaluation/jobs/run",
        json={
            "cases": [
                {"id": "same", "query": "質問 A"},
                {"id": "same", "query": "質問 B"},
            ],
        },
    )

    assert response.status_code == 422
    assert response.json()["error_messages"] == ["評価ケースの id が重複しています: same"]


def test_query_parameter_errors_drop_the_source() -> None:
    response = client.get("/api/feedback?limit=1000")

    assert response.status_code == 422
    assert response.json()["error_messages"] == ["limit: 100 以下の値を入力してください。"]
