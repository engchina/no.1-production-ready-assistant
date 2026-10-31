"""チャットの会話・メッセージの保存を、実 Oracle AI Database で確かめる。

未到達なら oracle_db fixture が skip する。作った会話はテストの中で削除する
（メッセージは FK の ON DELETE CASCADE で消える）。
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.clients.oracle import OracleClient, StoredMessage, _execute


@pytest.mark.usefixtures("oracle_db")
async def test_user_message_clarification_round_trips_on_real_oracle() -> None:
    """USER のメッセージの「回答前に確認した内容」を保存し、読み直せる（#1877）。"""
    client = OracleClient()
    profile = await client.ensure_default_search_answer_profile()
    conversation = await client.create_conversation(search_answer_profile_id=profile.id)
    clarification = {
        "outcome": "answered",
        "entries": [{"prompt": "権限の付与先は？", "selected": ["グループ"], "other_text": ""}],
    }
    clarified_id = uuid4().hex
    plain_id = uuid4().hex
    try:
        await client.append_message(
            StoredMessage(
                id=clarified_id,
                conversation_id=conversation.id,
                role="USER",
                content="アクセス権限を付与したい",
                created_at=datetime.now(UTC),
                clarification=clarification,
            )
        )
        await client.append_message(
            StoredMessage(
                id=plain_id,
                conversation_id=conversation.id,
                role="USER",
                content="確認の無い質問",
                created_at=datetime.now(UTC),
            )
        )

        messages = await client.list_messages(conversation.id)
    finally:
        await client.delete_conversation(conversation.id)

    # 同じ時刻の行の並びは決まらないので、ID で引いて確かめる。
    by_id = {message.id: message.clarification for message in messages}
    assert by_id == {clarified_id: clarification, plain_id: None}


@pytest.mark.usefixtures("oracle_db")
async def test_assistant_answer_notice_round_trips_on_real_oracle() -> None:
    """ASSISTANT の回答に付けた注意書き（#2405）を、本文とは別の列に保存して読み直せる。"""
    client = OracleClient()
    profile = await client.ensure_default_search_answer_profile()
    conversation = await client.create_conversation(search_answer_profile_id=profile.id)
    user_id = uuid4().hex
    answer_id = uuid4().hex
    lease_owner = f"test:{uuid4().hex}"
    try:
        await client.append_message(
            StoredMessage(
                id=user_id,
                conversation_id=conversation.id,
                role="USER",
                content="合成の質問",
                created_at=datetime.now(UTC),
            )
        )
        await client.append_message(
            StoredMessage(
                id=answer_id,
                conversation_id=conversation.id,
                role="ASSISTANT",
                content="",
                reply_to_message_id=user_id,
                status="STREAMING",
                lease_owner=lease_owner,
                created_at=datetime.now(UTC),
            )
        )
        finished = await client.finish_chat_message(
            StoredMessage(
                id=answer_id,
                conversation_id=conversation.id,
                role="ASSISTANT",
                content="合成の回答",
                reply_to_message_id=user_id,
                status="COMPLETE",
                created_at=datetime.now(UTC),
                answer_notice="合成の注意書き\n2 行目",
            ),
            lease_owner=lease_owner,
        )
        messages = await client.list_messages(conversation.id)
    finally:
        await client.delete_conversation(conversation.id)

    assert finished is True
    by_id = {message.id: message for message in messages}
    assert by_id[answer_id].content == "合成の回答"
    assert by_id[answer_id].answer_notice == "合成の注意書き\n2 行目"
    assert by_id[user_id].answer_notice is None


@pytest.mark.usefixtures("oracle_db")
async def test_assistant_passage_marks_are_read_from_the_answer_record_on_real_oracle() -> None:
    """回答の段落の印（#2738）は回答の記録から trace_id で読み、会話のメッセージに付ける。"""
    client = OracleClient()
    profile = await client.ensure_default_search_answer_profile()
    conversation = await client.create_conversation(search_answer_profile_id=profile.id)
    trace_id = uuid4().hex
    answer_id = uuid4().hex
    user_id = uuid4().hex
    marks = [
        {
            "text": "・合成の説明",
            "state": "unverified",
            "reasons": [{"code": "unsupported", "label": "根拠で確かめられない", "detail": ""}],
        }
    ]
    removed = [{"text": "合成の矛盾", "reasons": [{"code": "contradicted", "label": "根拠と矛盾"}]}]
    try:
        await client.save_answer_record(
            {
                "trace_id": trace_id,
                "search_answer_profile_id": profile.id,
                "surface": "chat",
                "answer_engine": "test",
                "question": "合成の質問",
                "answer": "合成の回答",
                "citations": [],
                "diagnostics": {"passage_marks": marks, "removed_passages": removed},
            }
        )
        for message in (
            StoredMessage(
                id=user_id,
                conversation_id=conversation.id,
                role="USER",
                content="合成の質問",
                trace_id=trace_id,
                created_at=datetime.now(UTC),
            ),
            StoredMessage(
                id=answer_id,
                conversation_id=conversation.id,
                role="ASSISTANT",
                content="合成の回答",
                reply_to_message_id=user_id,
                trace_id=trace_id,
                created_at=datetime.now(UTC),
            ),
        ):
            await client.append_message(message)
        messages = await client.list_messages(conversation.id)
    finally:
        await client.delete_conversation(conversation.id)
        await client._run_transaction(
            lambda connection: _execute(
                connection,
                "DELETE FROM rag_answer_records WHERE trace_id = :trace_id",
                {"trace_id": trace_id},
            )
        )

    by_id = {message.id: message for message in messages}
    assert by_id[answer_id].passage_record == {
        "passage_marks": marks,
        "removed_passages": removed,
    }
    assert by_id[user_id].passage_record is None
