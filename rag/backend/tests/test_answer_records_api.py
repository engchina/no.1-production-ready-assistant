"""保存済み回答の一覧・詳細 API。"""

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from app.api.routes import search as search_route
from app.main import app
from tests.support import AsgiTestClient

client = AsgiTestClient(app)

RECORD: dict[str, Any] = {
    "trace_id": "trace-1",
    "search_answer_profile_id": "bv-1",
    "surface": "search",
    "answer_engine": "grounded",
    "question": "受注の登録方法は？",
    "rewritten_question": None,
    "created_at": datetime(2026, 9, 25, tzinfo=UTC),
}
PREVIEW_TRACE_ID = "guide-preview-" + "0" * 32


class FakeAnswerOracle:
    def __init__(self) -> None:
        self.list_calls: list[dict[str, Any]] = []
        self.count_calls: list[dict[str, Any]] = []
        self.evaluations: dict[str, dict[str, Any]] = {}
        # 見る人が今見られない文書（保存した引用の確かめ直し。#3252）。既定はすべて見られる。
        self.unreadable_documents: set[str] = set()

    async def accessible_document_ids(self, document_ids: list[str]) -> set[str]:
        return {item for item in document_ids if item not in self.unreadable_documents}

    async def list_answer_records(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.list_calls.append(kwargs)
        return [{**RECORD, "confidence": "high"}]

    async def count_answer_records(self, **kwargs: Any) -> int:
        self.count_calls.append(kwargs)
        return 23

    async def save_answer_evaluation(self, trace_id: str, evaluation: dict[str, Any]) -> bool:
        self.evaluations[trace_id] = evaluation
        return True

    async def get_answer_record(self, trace_id: str) -> dict[str, Any] | None:
        if trace_id == PREVIEW_TRACE_ID:
            return {
                **RECORD,
                "trace_id": trace_id,
                "answer": "回答",
                "citations_json": [],
                "diagnostics_json": {"guide_preview": {"guide_id": "g-1", "draft_revision": 2}},
                "evaluation_input_json": {"question": "受注の登録方法は？", "answer_text": "回答"},
            }
        if trace_id == "legacy":
            return {
                **RECORD,
                "trace_id": "legacy",
                "answer": "回答",
                "citations_json": [],
                "diagnostics_json": {},
                "evaluation_input_json": None,
            }
        if trace_id != "trace-1":
            return None
        return {
            "evaluation_input_json": {"question": "受注の登録方法は？", "answer_text": "回答"},
            **RECORD,
            "answer": "受注番号を入力して登録します。",
            "citations_json": [
                {"document_id": "doc-1", "chunk_id": "doc-1:c1", "text": "受注番号", "score": 0.9}
            ],
            "diagnostics_json": {"confidence": "high", "evidence_tree": []},
        }


@pytest.fixture
def fake_oracle(monkeypatch: pytest.MonkeyPatch) -> FakeAnswerOracle:
    fake = FakeAnswerOracle()
    monkeypatch.setattr(search_route, "OracleClient", lambda *args, **kwargs: fake)
    return fake


def test_list_and_get_saved_answers(fake_oracle: FakeAnswerOracle) -> None:
    listed = client.get(
        "/api/search/answers", params={"search_answer_profile_id": "bv-1", "limit": 5}
    )

    assert listed.status_code == 200
    page = listed.json()["data"]
    assert page["items"][0]["confidence"] == "high"
    assert fake_oracle.list_calls == [
        {"search_answer_profile_id": "bv-1", "limit": 5, "offset": 0, "trace_ids": None}
    ]

    detail = client.get("/api/search/answers/trace-1")
    data = detail.json()["data"]
    assert data["answer"] == "受注番号を入力して登録します。"
    assert data["citations"][0]["chunk_id"] == "doc-1:c1"
    assert data["answer_diagnostics"]["confidence"] == "high"
    assert client.get("/api/search/answers/missing").status_code == 404


def test_list_saved_answers_returns_total_and_paging(
    fake_oracle: FakeAnswerOracle,
) -> None:
    """回答履歴は総件数とページング（limit / offset / has_next）を返す（#304）。"""
    listed = client.get(
        "/api/search/answers",
        params={"search_answer_profile_id": "bv-1", "limit": 10, "offset": 10},
    )

    assert listed.status_code == 200
    page = listed.json()["data"]
    assert page["total"] == 23
    assert page["limit"] == 10
    assert page["offset"] == 10
    assert page["has_next"] is True
    assert len(page["items"]) == 1
    assert fake_oracle.count_calls == [{"search_answer_profile_id": "bv-1", "trace_ids": None}]
    # 既定は 1 ページ 25 件（共通の Pagination の既定と同じ。#1952）。
    client.get("/api/search/answers", params={"search_answer_profile_id": "bv-1"})
    assert fake_oracle.list_calls[-1]["limit"] == 25


def test_list_saved_answers_filters_trace_ids(fake_oracle: FakeAnswerOracle) -> None:
    """チャットは会話の回答の trace_id で保存の有無を引き当てる（重複・空白は除く）。"""
    listed = client.get(
        "/api/search/answers",
        params=[
            ("search_answer_profile_id", "bv-1"),
            ("trace_id", " trace-1 "),
            ("trace_id", "trace-2"),
            ("trace_id", "trace-1"),
        ],
    )

    assert listed.status_code == 200
    assert fake_oracle.list_calls[-1]["trace_ids"] == ["trace-1", "trace-2"]
    assert fake_oracle.count_calls[-1]["trace_ids"] == ["trace-1", "trace-2"]
    blank = client.get("/api/search/answers", params=[("trace_id", "  ")])
    too_many = client.get(
        "/api/search/answers", params=[("trace_id", f"t-{index}") for index in range(101)]
    )
    assert blank.status_code == 422
    assert too_many.status_code == 422


def test_saved_answer_evaluation_is_not_exposed(fake_oracle: FakeAnswerOracle) -> None:
    assert "/api/search/answers/{trace_id}/evaluation" not in app.openapi()["paths"]
    response = client.post(
        "/api/search/answers/trace-1/evaluation", json={"standard_answer": "合成の回答"}
    )
    assert response.status_code == 404
    assert fake_oracle.evaluations == {}


def test_oracle_rows_turn_json_decimals_into_numbers() -> None:
    """Oracle の JSON 列の Decimal を int / float に戻す(#678 / #695)。"""
    from decimal import Decimal

    from app.clients.oracle import _row_to_dict

    row = _row_to_dict(
        (
            {"evidence_items": [{"score": Decimal("0.5"), "rank": Decimal(2)}]},
            [Decimal(3)],
            Decimal("1.5"),
        ),
        [("payload_json",), ("items_json",), ("score",)],
    )
    item = row["payload_json"]["evidence_items"][0]  # type: ignore[index]
    assert item == {"score": 0.5, "rank": 2}
    assert type(item["rank"]) is int
    assert row["items_json"] == [3]
    # NUMBER 列の値はそのまま。
    assert row["score"] == Decimal("1.5")
    json.dumps([row["payload_json"], row["items_json"]])


def test_guide_preview_answers_are_kept_out_of_history() -> None:
    from app.clients.oracle import _answer_record_list_where
    from app.rag.support_guide_runtime import GUIDE_PREVIEW_TRACE_PREFIX

    where, _ = _answer_record_list_where(search_answer_profile_id="bv-1", trace_ids=None)
    assert f"trace_id NOT LIKE '{GUIDE_PREVIEW_TRACE_PREFIX}%'" in where
