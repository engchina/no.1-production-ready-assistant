"""履歴の補完を確認・送信で共有し、古い文脈を再利用しない。"""

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import HTTPException

from app.api.routes import chat
from app.clients.oracle import StoredConversation, StoredMessage
from app.config import Settings
from app.rag.chat_question_token import context_digest, issue_question_token, read_question_token
from app.rag.guardrails import GuardrailPolicy
from app.rag.pipeline import RagPipeline
from app.schemas.search_answer_profile import SearchAnswerProfileDetail


class ContextOracle:
    def __init__(self) -> None:
        self.conversation = StoredConversation(
            id="conversation-a",
            search_answer_profile_id="profile-a",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            user_id_hash="owner-a",
        )
        self.messages = [
            StoredMessage(
                id="user-a",
                conversation_id="conversation-a",
                role="USER",
                content="対象 A について。",
                created_at=datetime.now(UTC),
                status="COMPLETE",
            )
        ]
        self.view = SearchAnswerProfileDetail(
            id="profile-a",
            name="合成プロファイル A",
            status="ACTIVE",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    async def get_conversation(self, _id: str) -> StoredConversation | None:
        return self.conversation if _id == self.conversation.id else None

    async def list_messages(self, _id: str) -> list[StoredMessage]:
        return self.messages

    async def get_search_answer_profile(self, _id: str) -> SearchAnswerProfileDetail:
        return self.view


@pytest.fixture
def context_setup(monkeypatch: pytest.MonkeyPatch) -> tuple[ContextOracle, Settings, list[str]]:
    oracle = ContextOracle()
    settings = Settings(app_service_token_secret="synthetic-signing-key-" * 3)
    calls: list[str] = []

    async def empty(*args: Any) -> dict[str, Any]:
        return {}

    async def rewrite(self: Any, query: str, history: Any) -> str:
        calls.append(query)
        assert history[0].role == "USER"
        return "対象 A の手順は？ 条件 B との違いは？"

    monkeypatch.setattr(chat, "load_runtime_knowledge_payload", empty)
    monkeypatch.setattr(chat, "_published_guides", empty)
    monkeypatch.setattr(chat, "_ensure_caller_owns", lambda _conversation: None)
    monkeypatch.setattr(RagPipeline, "_safe_rewritten_query", rewrite)
    return oracle, settings, calls


async def test_clarification_context_is_reused_without_second_rewrite(context_setup: Any) -> None:
    oracle, settings, calls = context_setup
    query = "その手順は？ 条件 B との違いは？"
    question, token = await chat.prepare_chat_question(
        oracle, "conversation-a", "profile-a", query, settings
    )
    assert token
    reused, _ = await chat.prepare_chat_question(
        oracle, "conversation-a", "profile-a", query, settings, token=token
    )
    assert reused == question == "対象 A の手順は？ 条件 B との違いは？"
    assert calls == [query]


@pytest.mark.parametrize("change", ["history", "query", "settings", "profile", "tamper"])
async def test_context_rejects_stale_or_tampered_preparation(
    context_setup: Any, change: str
) -> None:
    oracle, settings, _calls = context_setup
    query = "その手順は？"
    _, token = await chat.prepare_chat_question(
        oracle, "conversation-a", "profile-a", query, settings
    )
    assert token is not None
    if change == "history":
        oracle.messages[0].content = "対象 B について。"
    elif change == "query":
        query = "別の質問は？"
    elif change == "settings":
        settings = settings.model_copy(update={"rag_history_rewrite_enabled": False})
    elif change == "profile":
        oracle.view.name = "合成プロファイル B"
    else:
        token += "invalid"
    with pytest.raises(HTTPException) as error:
        await chat.prepare_chat_question(
            oracle, "conversation-a", "profile-a", query, settings, token=token
        )
    assert error.value.status_code == 409


async def test_context_checks_conversation_scope_and_owner(
    context_setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    oracle, settings, calls = context_setup
    with pytest.raises(HTTPException) as missing:
        await chat.prepare_chat_question(oracle, "missing", "profile-a", "その手順は？", settings)
    assert missing.value.status_code == 404
    with pytest.raises(HTTPException) as profile:
        await chat.prepare_chat_question(
            oracle, "conversation-a", "profile-b", "その手順は？", settings
        )
    assert profile.value.status_code == 404

    def forbidden(_conversation: Any) -> None:
        raise HTTPException(status_code=403)

    monkeypatch.setattr(chat, "_ensure_caller_owns", forbidden)
    with pytest.raises(HTTPException) as owner:
        await chat.prepare_chat_question(
            oracle, "conversation-a", "profile-a", "その手順は？", settings
        )
    assert owner.value.status_code == 403
    assert not calls


async def test_disabled_rewrite_does_not_call_model(context_setup: Any) -> None:
    oracle, settings, calls = context_setup
    settings.rag_history_rewrite_enabled = False
    question, token = await chat.prepare_chat_question(
        oracle, "conversation-a", "profile-a", "その手順は？", settings
    )
    assert question == "その手順は？" and token
    assert not calls


def test_token_binds_subject_and_expires() -> None:
    from app.rag.chat_question_token import _key
    from app.rag.signed_url import SignedTokenError, issue_signed_token

    secret = "synthetic-signing-key-" * 3
    binding = {"owner": "owner-a", "revision": context_digest(["user-a"])}
    token = issue_question_token(secret, binding, "対象 A は？")
    with pytest.raises(ValueError):
        read_question_token(secret, token, {**binding, "owner": "owner-b"})
    expired, _ = issue_signed_token(
        _key(secret),
        version="chat-question-v1",
        claims={"binding": binding, "question": "対象 A は？"},
        now=1,
    )
    with pytest.raises(SignedTokenError):
        read_question_token(secret, expired, binding)


async def test_prepared_query_reaches_search_while_original_is_saved(
    context_setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.schemas.chat import ChatMessageRequest

    oracle, settings, calls = context_setup
    query = "その手順は？ 条件 B との違いは？"
    question, token = await chat.prepare_chat_question(
        oracle, "conversation-a", "profile-a", query, settings
    )
    captured = []

    async def resolve(request: Any, effective: Any, **kwargs: Any) -> Any:
        captured.append(request)
        return request, effective, None, None

    async def append(message: StoredMessage) -> StoredMessage:
        oracle.messages.append(message)
        return message

    monkeypatch.setattr(chat, "_resolve_query_context", resolve)
    monkeypatch.setattr(oracle, "append_message", append, raising=False)
    turn = await chat._prepare_chat_turn(
        oracle,
        "conversation-a",
        "profile-a",
        ChatMessageRequest(content=query, question_context_token=token),
        settings,
    )
    assert captured[0].query == question
    assert turn.request.original_question == query
    assert turn.user_message.content == query
    assert turn.history_question_prepared
    assert calls == [query]


@pytest.mark.parametrize("outcome", ["answered", "skipped"])
async def test_only_explicit_confirmations_enter_safe_user_history(outcome: str) -> None:
    message = StoredMessage(
        id="user-a",
        conversation_id="conversation-a",
        role="USER",
        content="その手順は？",
        status="COMPLETE",
        created_at=datetime.now(UTC),
        clarification={
            "outcome": outcome,
            "entries": [{"prompt": "対象は？", "selected": ["対象 A"]}],
        },
    )
    turns = await chat._build_safe_history([message], GuardrailPolicy(Settings()))
    assert turns[0].role == "USER"
    assert ("対象 A" in turns[0].content) is (outcome == "answered")


async def test_clarification_api_matches_resolved_question(
    context_setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api.routes import search_answer_profile_knowledge as route
    from app.schemas.search_answer_profile_knowledge import ClarificationSuggestRequest

    oracle, settings, calls = context_setup
    settings.rag_support_guide_enabled = False
    oracle.view.config.query.support_guide_enabled = False
    matched: list[str] = []
    monkeypatch.setattr(route, "OracleClient", lambda: oracle)
    monkeypatch.setattr(route, "get_settings", lambda: settings)

    async def empty(*args: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(route, "load_runtime_knowledge_payload", empty)
    monkeypatch.setattr(
        route, "suggest_clarification", lambda _payload, query, **kwargs: matched.append(query)
    )
    result = await route.post_clarification_suggest(
        "profile-a",
        ClarificationSuggestRequest(
            query="その手順は？ 条件 B との違いは？",
            conversation_id="conversation-a",
        ),
    )
    assert matched == ["対象 A の手順は？ 条件 B との違いは？"]
    assert result.data is not None and result.data.question_context_token
    assert len(calls) == 1


async def test_blocked_confirmation_also_excludes_assistant_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.rag.guardrails import GuardrailResult

    policy = GuardrailPolicy(Settings())
    monkeypatch.setattr(
        policy,
        "validate_query",
        lambda text: GuardrailResult(
            allowed="合成の拒否対象" not in text, sanitized_text=text, findings=[]
        ),
    )
    user = StoredMessage(
        id="user-a",
        conversation_id="conversation-a",
        role="USER",
        content="その手順は？",
        status="COMPLETE",
        created_at=datetime.now(UTC),
        clarification={
            "outcome": "answered",
            "entries": [{"prompt": "対象は？", "selected": ["合成の拒否対象"]}],
        },
    )
    assistant = StoredMessage(
        id="assistant-a",
        conversation_id="conversation-a",
        role="ASSISTANT",
        content="合成の回答。",
        reply_to_message_id="user-a",
        status="COMPLETE",
        created_at=datetime.now(UTC),
    )
    assert await chat._build_safe_history([user, assistant], policy) == []


async def test_chat_history_understanding_uses_profile_language_scope(
    context_setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rag_engine.retrieval.task_contract import task_contract
    from rag_parser_core.language import current_pack

    oracle, settings, _ = context_setup
    oracle.view = oracle.view.model_copy(
        update={
            "config": oracle.view.config.model_copy(
                update={"language_pack": {"question_cues": {"cause_cues": ["理屈"]}}}
            )
        }
    )
    global_hash = current_pack().hash
    observed: list[bool] = []

    async def rewrite(self: Any, query: str, history: Any) -> str:
        observed.append(task_contract(query)["asks_cause"])
        return query

    monkeypatch.setattr(RagPipeline, "_safe_rewritten_query", rewrite)
    await chat.prepare_chat_question(
        oracle, "conversation-a", "profile-a", "対象 A の理屈を教えてください。", settings
    )
    assert observed == [True]
    assert current_pack().hash == global_hash


async def test_prepared_question_rejects_changed_global_language_pack(context_setup: Any) -> None:
    from rag_parser_core.language import default_pack, resolve, set_default_pack

    oracle, settings, _ = context_setup
    _, token = await chat.prepare_chat_question(
        oracle, "conversation-a", "profile-a", "その手順は？", settings
    )
    assert token
    old = default_pack()
    try:
        set_default_pack(resolve(old, {"question_cues": {"cause_cues": ["合成"]}}))
        with pytest.raises(HTTPException) as rejected:
            await chat.prepare_chat_question(
                oracle, "conversation-a", "profile-a", "その手順は？", settings, token=token
            )
        assert rejected.value.status_code == 409
    finally:
        set_default_pack(old)


async def test_signed_rule_facts_reach_execution_without_second_recognition(
    context_setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.rag.chat_question_token import (
        read_rule_selection_answers,
        read_rule_selection_elapsed,
        with_rule_selection_answers,
    )
    from app.schemas.chat import ChatMessageRequest

    oracle, settings, _calls = context_setup
    query = "その設定は？"
    _, token = await chat.prepare_chat_question(
        oracle, "conversation-a", "profile-a", query, settings
    )
    assert token
    facts = [{"rule_id": "r1", "option_ids": ["a"], "other_text": ""}]
    token = with_rule_selection_answers(
        settings.app_service_token_secret, token, facts, elapsed_seconds=3
    )
    assert read_rule_selection_answers(settings.app_service_token_secret, token) == facts
    assert read_rule_selection_elapsed(settings.app_service_token_secret, token) == 3
    captured = []

    async def resolve(request: Any, effective: Any, **kwargs: Any) -> Any:
        captured.append((request, kwargs))
        return request, effective, None, None

    async def append(message: StoredMessage) -> StoredMessage:
        return message

    monkeypatch.setattr(chat, "_resolve_query_context", resolve)
    monkeypatch.setattr(oracle, "append_message", append, raising=False)
    await chat._prepare_chat_turn(
        oracle,
        "conversation-a",
        "profile-a",
        ChatMessageRequest(
            content=query,
            question_context_token=token,
        ),
        settings,
    )
    assert captured[0][1]["confirmed_rule_answers"][0].option_ids == ["a"]
    assert captured[0][0]._rule_selection_state == {"prepared": True, "elapsed_seconds": 3}
    with pytest.raises(ValueError):
        read_rule_selection_answers(settings.app_service_token_secret, token + "changed")


@pytest.mark.parametrize("known", [True, False])
async def test_confirmation_api_keeps_unknown_and_shares_positive_choice(
    context_setup: Any,
    monkeypatch: pytest.MonkeyPatch,
    known: bool,
) -> None:
    import json
    from copy import deepcopy

    from app.api.routes import search_answer_profile_knowledge as route
    from app.rag import rule_selection
    from app.rag.chat_question_token import read_rule_selection_answers
    from app.schemas.search_answer_profile_knowledge import ClarificationSuggestRequest
    from tests.test_rule_selection import PAYLOAD

    oracle, settings, _calls = context_setup
    settings.rag_support_guide_enabled = False
    settings.rag_history_rewrite_enabled = False
    oracle.view.config.query.support_guide_enabled = False
    payload = deepcopy(PAYLOAD)
    text = "設定はモードAを選びます" if known else "設定はモードAではない。まだ未定です"
    captured = []

    async def load(*args: Any) -> dict[str, Any]:
        return payload

    class Client:
        async def generate(self, prompt: str, context: str, **kwargs: Any) -> str:
            captured.append(json.loads(context))
            return json.dumps(
                {
                    "conditions": [
                        {
                            "condition_id": "rule-1",
                            "status": "known" if known else "unknown",
                            "option_ids": ["a"] if known else [],
                            "evidence": [text] if known else [],
                        }
                    ]
                }
            )

    monkeypatch.setattr(route, "OracleClient", lambda: oracle)
    monkeypatch.setattr(route, "get_settings", lambda: settings)
    monkeypatch.setattr(route, "load_runtime_knowledge_payload", load)
    monkeypatch.setattr(rule_selection, "condition_client", lambda settings: Client())
    result = await route.post_clarification_suggest(
        "profile-a", ClarificationSuggestRequest(query=text, conversation_id="conversation-a")
    )
    assert result.data is not None
    assert (result.data.suggestion is None) == known
    assert len(captured) == 1
    token = result.data.question_context_token
    assert token
    facts = read_rule_selection_answers(settings.app_service_token_secret, token)
    assert len(facts) == int(known)
    assert captured[0]["user_statements"].endswith(text)
