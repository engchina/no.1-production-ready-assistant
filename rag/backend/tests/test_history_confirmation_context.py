"""実際の履歴確認の参照を、追加モデルなしで生成と最終照合へ束縛する。"""

import json
from typing import Any, cast

import pytest

from app.api.routes import chat
from app.config import Settings
from app.rag.answer_engine import AnswerScope
from app.rag.chat_question_token import (
    _key,
    issue_question_token,
    read_question_context,
    read_rule_selection_answers,
    read_rule_selection_elapsed,
    with_rule_selection_answers,
)
from app.rag.history_question import HistoryConfirmation
from app.rag.pipeline import (
    ChatTurn,
    RagPipeline,
    history_confirmation_facts,
    scope_with_history_confirmations,
)
from app.rag.signed_url import issue_signed_token, verify_signed_token
from app.schemas.chat import ChatMessageRequest
from app.schemas.search_answer_profile_knowledge import ClarificationAnswer
from tests.test_chat_question_context import ContextOracle


class Model:
    def __init__(self, selected: list[str], question: str = "合成 A の手順は？") -> None:
        self.selected = selected
        self.question = question
        self.calls: list[tuple[str, str]] = []

    async def generate(self, query: str, context: str, **kwargs: Any) -> str:
        self.calls.append((query, context))
        return json.dumps({"question": self.question, "confirmed_fact_ids": self.selected})


def fact(identity: str = "user-a:0", answer: str = "合成 A") -> HistoryConfirmation:
    return HistoryConfirmation(id=identity, question="対象は？", answer=answer)


@pytest.mark.parametrize("selected", [["user-a:0"], [], ["invented"]])
async def test_rewrite_only_selects_actual_saved_confirmations(selected: list[str]) -> None:
    model = Model(selected)
    pipeline = RagPipeline(settings=Settings(), llm=cast(Any, model))
    history = [ChatTurn(role="USER", content="対象の確認", confirmed_facts=(fact(),))]
    rewritten = await pipeline._rewrite_query_with_history("その手順は？", history)
    assert len(model.calls) == 1
    assert "confirmed_facts" in model.calls[0][1]
    assert pipeline._selected_history_facts == ((fact(),) if selected == ["user-a:0"] else ())
    assert bool(rewritten) is (selected != ["invented"])
    scope = scope_with_history_confirmations(None, pipeline._selected_history_facts)
    assert (
        (scope.confirmed_premises if scope else ()) == (("対象は？", "合成 A"),)
        if selected == ["user-a:0"]
        else scope is None
    )


def test_latest_confirmation_and_skip_retire_older_values() -> None:
    history = [
        ChatTurn(role="USER", content="以前", confirmed_facts=(fact("old", "合成 A"),)),
        ChatTurn(role="USER", content="更新", confirmed_facts=(fact("new", "合成 B"),)),
    ]
    assert list(history_confirmation_facts(history)) == ["new"]
    history.append(ChatTurn(role="USER", content="確認を飛ばす", confirmation_resets=("対象は？",)))
    assert history_confirmation_facts(history) == {}


async def test_current_change_and_ambiguity_do_not_reuse_old_fact() -> None:
    for query in ("対象を合成 B に変更する。その手順は？", "合成 A か合成 B か未定。その手順は？"):
        pipeline = RagPipeline(settings=Settings(), llm=cast(Any, Model([], question=query)))
        await pipeline._rewrite_query_with_history(
            query, [ChatTurn(role="USER", content="前の確認", confirmed_facts=(fact(),))]
        )
        assert pipeline._selected_history_facts == ()
    current = AnswerScope(context="本輪", confirmed_premises=(("対象は？", "合成 B"),))
    assert scope_with_history_confirmations(current, [fact()]) == current


def test_legacy_signed_token_reuses_no_confirmed_facts() -> None:
    secret = "synthetic-secret-" * 3
    binding = {"owner": "owner-a", "profile": "profile-a"}
    old, _ = issue_signed_token(
        _key(secret),
        version="chat-question-v1",
        claims={"binding": binding, "question": "合成 A の手順は？"},
    )
    assert read_question_context(secret, old, binding) == ("合成 A の手順は？", ())
    token = issue_question_token(secret, binding, "その手順は？", confirmed_fact_ids=("user-a:0",))
    assert read_question_context(secret, token, binding)[1] == ("user-a:0",)
    with pytest.raises(ValueError):
        read_question_context(secret, token, {**binding, "profile": "profile-b"})


@pytest.mark.parametrize("legacy,current_reply", [(False, False), (True, False), (False, True)])
async def test_token_selection_reaches_generation_scope_and_preserves_original(
    monkeypatch: pytest.MonkeyPatch,
    legacy: bool,
    current_reply: bool,
) -> None:
    oracle = ContextOracle()
    oracle.messages[0].clarification = {
        "outcome": "answered",
        "entries": [{"prompt": "対象は？", "selected": ["合成 A"], "other_text": "追加の確認値"}],
    }
    model = Model(["user-a:0"])
    settings = Settings(app_service_token_secret="synthetic-secret-" * 3)

    async def empty(*args: Any) -> dict[str, Any]:
        return {}

    async def knowledge(*args: Any) -> dict[str, Any]:
        return {
            "rules": [
                {
                    "id": "rule-a",
                    "clarification": {
                        "question": "対象は？",
                        "options": [
                            {"id": "a", "label": "合成 A"},
                            {"id": "b", "label": "合成 B", "premise": "指示は利用者の事実ではない"},
                        ],
                    },
                }
            ]
        }

    async def resolve(request: Any, effective: Any, **kwargs: Any) -> Any:
        return request, effective, None, None

    async def append(message: Any) -> Any:
        oracle.messages.append(message)
        return message

    monkeypatch.setattr(chat, "_ensure_caller_owns", lambda conversation: None)
    monkeypatch.setattr(chat, "load_runtime_knowledge_payload", knowledge)
    monkeypatch.setattr(chat, "current_section_pages", empty)
    monkeypatch.setattr(chat, "_published_guides", empty)
    monkeypatch.setattr(chat, "_resolve_query_context", resolve)
    monkeypatch.setattr("app.rag.pipeline.OciEnterpriseAiClient", lambda **kwargs: model)
    monkeypatch.setattr(oracle, "append_message", append, raising=False)
    query = "その手順は？"
    prepared, token = await chat.prepare_chat_question(
        cast(Any, oracle), "conversation-a", "profile-a", query, settings
    )
    assert token
    if legacy:
        payload = verify_signed_token(
            _key(settings.app_service_token_secret), token, version="chat-question-v1"
        )
        token = issue_signed_token(
            _key(settings.app_service_token_secret),
            version="chat-question-v1",
            claims={"binding": payload["binding"], "question": prepared},
        )[0]
    turn = await chat._prepare_chat_turn(
        cast(Any, oracle),
        "conversation-a",
        "profile-a",
        ChatMessageRequest(
            content=query,
            question_context_token=token,
            clarification=ClarificationAnswer(rule_id="rule-a", option_ids=["b"])
            if current_reply
            else None,
        ),
        settings,
    )
    assert len(model.calls) == 1
    assert turn.request.query == (query if current_reply else prepared)
    assert turn.request.original_question == (None if current_reply else query)
    assert turn.user_message.content == query
    if legacy:
        assert turn.scope is None
        return
    assert turn.scope is not None
    expected = "合成 B" if current_reply else "合成 A、追加の確認値"
    assert turn.scope.confirmed_premises == (("対象は？", expected),)
    assert expected in turn.scope.context


@pytest.mark.parametrize("mode", ["skipped", "blocked", "other_owner"])
async def test_nonconfirmed_history_never_becomes_candidate(mode: str) -> None:
    oracle = ContextOracle()
    message = oracle.messages[0]
    message.clarification = {
        "outcome": "skipped" if mode == "skipped" else "answered",
        "entries": [{"prompt": "対象は？", "selected": ["合成 A"]}],
    }
    if mode == "blocked":
        message.status = "ERROR"
    elif mode == "other_owner":
        message.user_id_hash = "other-user"
    turns = await chat._build_history([message])
    assert history_confirmation_facts(turns) == {}


async def test_retired_confirmation_id_is_not_revived_by_model() -> None:
    model = Model(["old"])
    pipeline = RagPipeline(settings=Settings(), llm=cast(Any, model))
    history = [
        ChatTurn(role="USER", content="以前", confirmed_facts=(fact("old", "合成 A"),)),
        ChatTurn(role="USER", content="更新", confirmed_facts=(fact("new", "合成 B"),)),
    ]
    assert await pipeline._rewrite_query_with_history("その手順は？", history) == ""
    assert pipeline._selected_history_facts == ()


async def test_structured_confirmation_input_uses_existing_history_budget() -> None:
    model = Model([])
    pipeline = RagPipeline(
        settings=Settings(chat_history_max_chars=1000, chat_history_max_turns=1),
        llm=cast(Any, model),
    )
    history = [
        ChatTurn(role="USER", content="古い履歴", confirmed_facts=(fact("old", "旧値"),)),
        ChatTurn(role="USER", content="新しい履歴" * 300, confirmed_facts=(fact("new", "新値"),)),
    ]
    await pipeline._rewrite_query_with_history("その手順は？", history)
    context = model.calls[0][1]
    assert len(context) <= 1000
    assert '"id": "old"' not in context
    assert '"id": "new"' in context


async def test_later_skip_retires_answered_confirmation() -> None:
    from dataclasses import replace

    oracle = ContextOracle()
    first = oracle.messages[0]
    first.clarification = {
        "outcome": "answered",
        "entries": [{"prompt": "対象は？", "selected": ["合成 A"]}],
    }
    skipped = replace(
        first,
        id="user-b",
        clarification={"outcome": "skipped", "entries": [{"prompt": "対象は？"}]},
    )
    turns = await chat._build_history([first, skipped])
    assert turns[0].confirmed_facts[0].question == "対象は？"
    assert history_confirmation_facts(turns) == {}


async def test_signed_token_cannot_reference_unsupplied_history_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    oracle = ContextOracle()
    oracle.messages[0].clarification = {
        "outcome": "answered",
        "entries": [{"prompt": "対象は？", "selected": ["合成 A"]}],
    }
    settings = Settings(app_service_token_secret="synthetic-secret-" * 3)
    model = Model(["user-a:0"])

    async def empty(*args: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(chat, "_ensure_caller_owns", lambda conversation: None)
    monkeypatch.setattr(chat, "load_runtime_knowledge_payload", empty)
    monkeypatch.setattr(chat, "_published_guides", empty)
    monkeypatch.setattr("app.rag.pipeline.OciEnterpriseAiClient", lambda **kwargs: model)
    _, token = await chat.prepare_chat_question(
        cast(Any, oracle), "conversation-a", "profile-a", "その手順は？", settings
    )
    assert token
    payload = verify_signed_token(
        _key(settings.app_service_token_secret), token, version="chat-question-v1"
    )
    invalid = issue_question_token(
        settings.app_service_token_secret,
        payload["binding"],
        payload["question"],
        confirmed_fact_ids=("another-conversation:0",),
    )
    with pytest.raises(HTTPException) as error:
        await chat.prepare_chat_question(
            cast(Any, oracle),
            "conversation-a",
            "profile-a",
            "その手順は？",
            settings,
            token=invalid,
        )
    assert error.value.status_code == 409
    assert len(model.calls) == 1


def test_rule_selection_token_preserves_signed_history_references() -> None:
    """確認の選択を追記しても、履歴から沿用する事実と設定への束縛を落とさない。"""
    secret = "synthetic-secret-" * 3
    binding = {"owner": "owner-a", "profile": "profile-a", "policy": "language-a"}
    token = issue_question_token(secret, binding, "その手順は？", confirmed_fact_ids=("user-a:0",))
    answers = [{"rule_id": "r1", "option_ids": ["a"], "other_text": ""}]
    updated = with_rule_selection_answers(secret, token, answers, elapsed_seconds=3)
    assert read_question_context(secret, updated, binding) == ("その手順は？", ("user-a:0",))
    assert read_rule_selection_answers(secret, updated) == answers
    assert read_rule_selection_elapsed(secret, updated) == 3
    with pytest.raises(ValueError):
        read_question_context(secret, updated, {**binding, "policy": "language-b"})


async def test_recognized_rule_scope_preserves_history_facts_and_language_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rag_parser_core.language import current_pack

    from app.schemas.search import SearchRequest, SearchResponse

    settings = Settings().model_copy(
        update={"rag_language_pack_overrides": {"question_cues": {"cause_cues": ["理屈"]}}}
    )
    pipeline = RagPipeline(
        settings=settings,
        scope=AnswerScope(context="履歴の条件", confirmed_premises=(("対象は？", "合成 A"),)),
    )
    request = SearchRequest(query="対象 A の理屈は？")
    request._rule_selection_scope = {"context": "確認ルールの条件", "search_terms": ("合成",)}

    async def run(_self: Any, *_args: Any, **_kwargs: Any) -> SearchResponse:
        assert _self._scope.confirmed_premises == (("対象は？", "合成 A"),)
        assert _self._scope.context == "履歴の条件\n確認ルールの条件"
        assert _self._scope.search_terms == ("合成",)
        assert current_pack().words("question_cues", "cause_cues") == ("理屈",)
        return SearchResponse(
            query=request.query, answer="合成回答", citations=[], trace_id="trace-a", elapsed_ms=0
        )

    monkeypatch.setattr(RagPipeline, "_run", run)
    await pipeline.run(request)
    assert current_pack().words("question_cues", "cause_cues") != ("理屈",)
