"""長い本文の回答を、実 Oracle AI Database の回答の記録と会話の回答に保存できる（#2342）。

本文が UTF-8 で 8192 バイト以上のとき、本文を ``str``（VARCHAR の bind）で CLOB の列へ渡すと、
回答の記録の ``MERGE`` は ``ORA-03146``、32KB を超える引用の JSON と一緒に渡す会話の回答の
``UPDATE`` は ``ORA-40441`` になっていた。共通の実行（``_execute`` / ``_execute_count``）が
長い文字列を CLOB で送るので、どちらも保存できることを確かめる。
未到達なら oracle_db fixture が skip する。
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.clients.oracle import OracleClient, StoredMessage, _execute

# 合成の本文（日本語 3,000 字 = 9,000 バイト。8192 バイトの境目を超える）。
LONG_ANSWER = "回答の本文です。" * 375
# 32KB を超える引用の JSON（合成の本文の引用 40 件）。
CITATIONS = [
    {
        "chunk_id": f"chunk-{index}",
        "document_id": "document-a",
        "text": "根拠の本文の合成の例。" * 100,
        "score": 0.5,
        "metadata": {"file_name": "document-a.pdf", "page_number": index + 1},
    }
    for index in range(40)
]


def test_long_answer_fixture_crosses_the_failing_sizes() -> None:
    assert len(LONG_ANSWER.encode("utf-8")) >= 8192
    import json

    assert len(json.dumps(CITATIONS, ensure_ascii=False).encode("utf-8")) > 32 * 1024


@pytest.mark.usefixtures("oracle_db")
async def test_long_answer_record_saves_on_real_oracle() -> None:
    client = OracleClient()
    trace_id = uuid4().hex
    record = {
        "trace_id": trace_id,
        "search_answer_profile_id": None,
        "surface": "chat",
        "answer_engine": "test",
        "question": "合成の質問",
        "rewritten_question": "合成の質問（書き換え後）",
        "answer": LONG_ANSWER,
        "citations": CITATIONS,
        "diagnostics": {"execution_steps": [{"name": "合成の工程", "details": ["行"] * 200}]},
        "evaluation_input": None,
    }
    try:
        await client.save_answer_record(record)
        # 同じ trace_id の上書き（MERGE の UPDATE の側）も通る。
        # 回答に付けた注意書き（#2405）も別の列に保存する。
        await client.save_answer_record(
            {
                **record,
                "answer": LONG_ANSWER + "（追記）",
                "answer_notice": "合成の注意書き\n2 行目",
            }
        )
        saved = await client.get_answer_record(trace_id)
    finally:
        # 作った行を消す（回答の記録の削除の API は無い。テストの行だけを trace_id で消す）。
        await client._run_transaction(
            lambda connection: _execute(
                connection,
                "DELETE FROM rag_answer_records WHERE trace_id = :trace_id",
                {"trace_id": trace_id},
            )
        )
    assert saved is not None
    assert saved["answer"] == LONG_ANSWER + "（追記）"
    assert saved["answer_notice"] == "合成の注意書き\n2 行目"
    citations = saved["citations_json"]
    assert isinstance(citations, list)
    assert len(citations) == len(CITATIONS)


@pytest.mark.usefixtures("oracle_db")
async def test_long_chat_answer_saves_on_real_oracle() -> None:
    client = OracleClient()
    profile = await client.ensure_default_search_answer_profile()
    conversation = await client.create_conversation(search_answer_profile_id=profile.id)
    message_id = uuid4().hex
    try:
        await client.append_message(
            StoredMessage(
                id=message_id,
                conversation_id=conversation.id,
                role="ASSISTANT",
                content="",
                status="STREAMING",
                created_at=datetime.now(UTC),
                lease_owner="test-worker",
            )
        )
        saved = await client.finish_chat_message(
            StoredMessage(
                id=message_id,
                conversation_id=conversation.id,
                role="ASSISTANT",
                content=LONG_ANSWER,
                citations=CITATIONS,
                trace_id=uuid4().hex,
                status="COMPLETE",
                elapsed_ms=1234.5,
                created_at=datetime.now(UTC),
            ),
            lease_owner="test-worker",
        )
        messages = await client.list_messages(conversation.id)
    finally:
        await client.delete_conversation(conversation.id)
    assert saved is True
    (message,) = [item for item in messages if item.id == message_id]
    assert message.status == "COMPLETE"
    assert message.content == LONG_ANSWER
    assert len(message.citations) == len(CITATIONS)
