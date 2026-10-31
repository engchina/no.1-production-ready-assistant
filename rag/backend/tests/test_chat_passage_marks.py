"""会話のメッセージの段落の印（#2738）。会話を開き直したときの読み元。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from app.api.routes import chat as chat_route
from app.clients.oracle import StoredMessage, _message_passage_record, _stored_message_from_row

MARKS = [
    {
        "text": "・合成の説明",
        "state": "warning",
        "reasons": [{"code": "item_check", "label": "システムの検査で注意が必要", "detail": "値"}],
    }
]


def test_rows_read_the_marks_of_the_answer_record() -> None:
    """回答の記録の JSON（文字列か値）を読み、無い・壊れた値は捨てる。"""
    row = {
        "passage_marks_json": json.dumps(MARKS, ensure_ascii=False),
        "removed_passages_json": [],
        "answer_warnings_json": "{壊れた",
    }
    assert _message_passage_record(row) == {"passage_marks": MARKS, "removed_passages": []}
    assert _message_passage_record({}) is None


def _message(status: str, record: dict[str, object] | None) -> StoredMessage:
    return StoredMessage(
        id="m1",
        conversation_id="c1",
        role="ASSISTANT",
        content="合成の回答",
        status=status,
        created_at=datetime.now(UTC),
        passage_record=record,
    )


def test_chat_message_carries_the_marks_of_a_finished_answer() -> None:
    message = chat_route._to_chat_message(_message("COMPLETE", {"passage_marks": MARKS}))
    assert message.passage_marks == MARKS
    assert (message.removed_passages, message.answer_warnings) == ([], [])
    # 作成中の回答・記録の無い回答（この機能より前の回答）は空。
    streaming = chat_route._to_chat_message(_message("STREAMING", {"passage_marks": MARKS}))
    assert streaming.passage_marks == []
    assert chat_route._to_chat_message(_message("COMPLETE", None)).passage_marks == []


def test_chat_message_carries_the_number_of_attached_images() -> None:
    """回答のモデルへ根拠の画像を添付した回答は、その件数を見出し用に返す（#3444）。"""
    row = {
        "message_id": "m1",
        "conversation_id": "c1",
        "role": "ASSISTANT",
        "status": "COMPLETE",
        "created_at": datetime.now(UTC),
        "answer_attached_images": Decimal(2),
    }
    stored = _stored_message_from_row(row)
    assert stored.answer_attached_images == 2
    assert chat_route._to_chat_message(stored).answer_attached_images == 2
    # 画像を添付しなかった回答・記録の無い回答（null）は 0。
    for value in (None, 0, -1):
        stored = _stored_message_from_row({**row, "answer_attached_images": value})
        assert stored.answer_attached_images == 0
    # 作成中の回答には出さない。
    streaming = chat_route._to_chat_message(
        _stored_message_from_row({**row, "status": "STREAMING"})
    )
    assert streaming.answer_attached_images == 0
