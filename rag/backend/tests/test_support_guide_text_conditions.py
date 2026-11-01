"""自由入力条件の認識と確認。資料・値は合成、モデルの API は決定論スタブ。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from app.api.routes import search as search_route
from app.api.routes import search_answer_profile_knowledge as knowledge_route
from app.config import get_settings
from app.main import app
from app.rag import support_guide_conditions as recognition
from app.rag.support_guide_runtime import (
    GuideMatch,
    guide_clarification,
    match_guide,
    resolve_guide_clarification,
)
from app.schemas.search import SearchRequest
from app.schemas.search_answer_profile_knowledge import RuleClarification
from app.schemas.support_guide import SupportGuideContent
from tests.support import AsgiTestClient
from tests.test_support_guide_matching import _ProfileOracle, _published_store

QUESTION = "資料 A の項目 B を確認したい"
GUIDE = SupportGuideContent.model_validate(
    {
        "title": "資料の項目を確認する",
        "goal": {"expected_result": "指定した項目を確認する", "match_terms": ["資料"]},
        "conditions": [
            {
                "id": "target",
                "label": "対象の資料と項目",
                "type": "text",
                "question": "対象の資料と項目を教えてください。",
            }
        ],
        "steps": [{"id": "check", "title": "指定された対象を確認する"}],
    }
)
GUIDES = [("g-text", 1, GUIDE)]


def stub(monkeypatch: pytest.MonkeyPatch, output: Any = None, *, error: bool = False) -> list[Any]:
    calls: list[Any] = []

    class Client:
        async def generate(self, _prompt: str, context: str, **kwargs: Any) -> str:
            assert kwargs["system_prompt"] == recognition.CONDITION_SYSTEM_PROMPT
            assert all(f"## Step {step}:" in kwargs["system_prompt"] for step in range(1, 5))
            calls.append((json.loads(context), kwargs))
            if error:
                raise RuntimeError("synthetic failure")
            return (
                output
                if isinstance(output, str)
                else json.dumps(
                    output
                    if output is not None
                    else {
                        "conditions": [
                            {
                                "condition_id": "target",
                                "status": "known",
                                "values": ["資料 A の項目 B"],
                            }
                        ]
                    }
                )
            )

    monkeypatch.setattr(recognition, "condition_client", lambda _settings: Client())
    return calls


def matched(
    *, provided: dict[str, str] | None = None, content: SupportGuideContent = GUIDE
) -> GuideMatch:
    result = match_guide([("g-text", 1, content)], QUESTION, provided)
    assert result is not None
    return result


async def test_exact_question_span_makes_text_condition_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = stub(monkeypatch)
    result = await recognition.resolve_text_conditions(matched(), QUESTION, get_settings())
    assert isinstance(result, GuideMatch)
    assert result.decision == "answer"
    assert result.summary()["known_conditions"] == [
        {
            "id": "target",
            "label": "対象の資料と項目",
            "value": "資料 A の項目 B",
            "source": "question",
            "state": "known",
        }
    ]
    assert calls[0][0]["question"] == QUESTION
    assert "response_schema" in calls[0][1]
    assert calls[0][1]["system_prompt"] == recognition.CONDITION_SYSTEM_PROMPT
    assert all(f"## Step {step}:" in calls[0][1]["system_prompt"] for step in range(1, 5))
    from app.rag.answer_prompts import readonly_prompt_stages

    stage = next(
        item for item in readonly_prompt_stages() if item["id"] == "support_guide_conditions"
    )
    prompts = stage["prompts"]
    assert isinstance(prompts, list)
    system = next(item for item in prompts if item["id"] == "system")
    assert system["content"] == calls[0][1]["system_prompt"]


@pytest.mark.parametrize(
    "output",
    [
        {
            "conditions": [
                {"condition_id": "target", "status": "known", "values": ["存在しない項目"]}
            ]
        },
        {"conditions": [{"condition_id": "target", "status": "known", "values": [""]}]},
        {"conditions": [{"condition_id": "other", "status": "known", "values": ["資料 A"]}]},
        {"conditions": [{"condition_id": "target", "status": "unknown", "values": []}]},
        {"conditions": [{"condition_id": "target", "status": "known", "values": ["資料 A"]}] * 2},
        "invalid json",
    ],
)
async def test_missing_invalid_or_invented_values_keep_confirmation(
    monkeypatch: pytest.MonkeyPatch, output: Any
) -> None:
    stub(monkeypatch, output)
    result = await recognition.resolve_text_conditions(matched(), QUESTION, get_settings())
    assert isinstance(result, GuideMatch)
    assert result.decision == "clarify"
    assert not result.summary()["known_conditions"]


async def test_conflicting_values_are_not_chosen(monkeypatch: pytest.MonkeyPatch) -> None:
    stub(
        monkeypatch,
        {
            "conditions": [
                {"condition_id": "target", "status": "conflicting", "values": ["資料 A", "項目 B"]}
            ]
        },
    )
    result = await recognition.resolve_text_conditions(matched(), QUESTION, get_settings())
    assert isinstance(result, GuideMatch)
    assert result.states[0].status == "conflicting"
    assert result.decision == "clarify"


async def test_explicit_values_and_document_conditions_do_not_call_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = stub(monkeypatch, error=True)
    result = await recognition.resolve_text_conditions(
        matched(provided={"target": "明示値"}), QUESTION, get_settings()
    )
    assert isinstance(result, GuideMatch) and result.states[0].value == "明示値"
    document = GUIDE.model_copy(
        update={"conditions": [GUIDE.conditions[0].model_copy(update={"source": "document"})]}
    )
    result = await recognition.resolve_text_conditions(
        matched(content=document), QUESTION, get_settings()
    )
    assert isinstance(result, GuideMatch) and not result.states[0].known
    assert calls == []


async def test_model_failure_keeps_confirmation_without_logging_input(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    stub(monkeypatch, error=True)
    result = await recognition.resolve_text_conditions(matched(), QUESTION, get_settings())
    assert isinstance(result, GuideMatch) and result.decision == "clarify"
    assert QUESTION not in caplog.text
    assert "synthetic failure" not in caplog.text


def test_text_confirmation_accepts_answer_and_retains_the_condition() -> None:
    suggestion = guide_clarification(matched())
    assert suggestion is not None
    rule_id, _, question = suggestion
    assert question.options == [] and question.allow_other
    conditions, context = resolve_guide_clarification(GUIDES, rule_id, [], "資料 C の項目 D") or (
        {},
        "",
    )
    assert conditions == {"target": "資料 C の項目 D"}
    assert "資料 C の項目 D" in context
    after = matched(provided=conditions)
    assert after.decision == "answer"
    for source in ["document", "tool"]:
        content = GUIDE.model_copy(
            update={"conditions": [GUIDE.conditions[0].model_copy(update={"source": source})]}
        )
        answer = resolve_guide_clarification([("g-text", 1, content)], rule_id, [], "資料 C")
        assert answer is None or not answer[0]


def test_empty_choices_require_free_text_and_one_choice_is_invalid() -> None:
    RuleClarification(question="対象は？", options=[])
    with pytest.raises(ValidationError):
        RuleClarification(question="対象は？", options=[], allow_other=False)
    with pytest.raises(ValidationError):
        RuleClarification(question="対象は？", options=[{"id": "a", "label": "A"}])


@pytest.mark.usefixtures("system_access")
@pytest.mark.parametrize("preview", [False, True])
async def test_search_and_draft_preview_use_the_same_recognition(
    monkeypatch: pytest.MonkeyPatch, preview: bool
) -> None:
    from app.rag.support_guide_runtime import GuidePreview

    stub(monkeypatch)
    monkeypatch.setattr(search_route, "OracleClient", lambda: _ProfileOracle())
    monkeypatch.setattr(
        search_route, "SupportGuideStore", _published_store([] if preview else GUIDES)
    )
    _, settings, _, _ = await search_route._resolve_query_context(
        SearchRequest(query=QUESTION, search_answer_profile_id="bv-1"),
        get_settings(),
        guide_preview=GuidePreview("g-text", 1, GUIDE, None) if preview else None,
    )
    assert settings.rag_support_guide["decision"] == "answer"
    known: Any = settings.rag_support_guide["known_conditions"]
    assert known[0]["source"] == "question"


def test_chat_preflight_shows_free_text_only_when_not_recognized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(knowledge_route, "OracleClient", lambda: _ProfileOracle())
    monkeypatch.setattr(search_route, "SupportGuideStore", _published_store(GUIDES))
    client = AsgiTestClient(app)
    stub(monkeypatch)
    response = client.post(
        "/api/search-answer-profiles/bv-1/clarifications/suggest", json={"query": QUESTION}
    )
    assert response.status_code == 200
    assert response.json()["data"]["suggestion"] is None
    stub(monkeypatch, {"conditions": []})
    response = client.post(
        "/api/search-answer-profiles/bv-1/clarifications/suggest",
        json={"query": "資料を確認したい"},
    )
    assert response.status_code == 200
    suggestion = response.json()["data"]["suggestion"]
    assert suggestion["clarification"]["options"] == []


async def test_separate_explicit_spans_are_preserved_without_inventing_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub(
        monkeypatch,
        {
            "conditions": [
                {"condition_id": "target", "status": "known", "values": ["資料 A", "項目 B"]}
            ]
        },
    )
    result = await recognition.resolve_text_conditions(matched(), QUESTION, get_settings())
    assert isinstance(result, GuideMatch)
    assert result.states[0].value == "資料 A、項目 B"
    assert result.decision == "answer"


@pytest.mark.usefixtures("system_access")
async def test_retrieval_without_answer_does_not_add_model_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = stub(monkeypatch)
    monkeypatch.setattr(search_route, "OracleClient", lambda: _ProfileOracle())
    monkeypatch.setattr(search_route, "SupportGuideStore", _published_store(GUIDES))
    await search_route._resolve_query_context(
        SearchRequest(query=QUESTION, search_answer_profile_id="bv-1", generate_answer=False),
        get_settings(),
    )
    assert not calls


async def test_chat_carries_the_users_confirmed_text_into_the_next_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from app.api.routes.chat import _guide_user_context
    from app.clients.oracle import StoredMessage

    def message(role: str, content: str, *, outcome: str = "answered") -> StoredMessage:
        return StoredMessage(
            id=role,
            conversation_id="conv-a",
            role=role,
            content=content,
            created_at=datetime.now(UTC),
            clarification={
                "outcome": outcome,
                "entries": [{"prompt": "対象の資料と項目は？", "other_text": "資料 A の項目 B"}],
            },
        )

    contexts = _guide_user_context(
        [
            message("USER", "資料を確認したい"),
            message("ASSISTANT", "資料 C の項目 D"),
            message("USER", "後続の質問", outcome="skipped"),
        ]
    )
    assert len(contexts) == 2
    assert "資料 A の項目 B" in contexts[0]
    assert "資料 A の項目 B" not in contexts[1]
    assert "資料 C" not in "\n".join(contexts)
    calls = stub(monkeypatch)
    text = "\n".join([*contexts, "同じ資料を続けて確認したい"])
    result = await recognition.resolve_text_conditions(matched(), text, get_settings())
    assert isinstance(result, GuideMatch) and result.decision == "answer"
    assert "利用者の答え" in calls[0][0]["question"]
