"""選択肢の出現と、利用者が肯定した選択を分離する。実モデルは呼ばない。"""

import json
from typing import Any

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.rag import rule_selection
from app.rag.rule_selection import apply_rule_selection_scope, recognize_rule_selections
from app.rag.search_answer_profile_knowledge import (
    suggest_clarification,
    unanswered_rule_clarifications,
)
from app.schemas.search import SearchRequest
from app.schemas.search_answer_profile_knowledge import ClarificationAnswer

PAYLOAD: dict[str, Any] = {
    "rules": [
        {
            "id": "rule-1",
            "title": "設定",
            "triggers": ["設定"],
            "content": "資料に沿って答える",
            "clarification": {
                "question": "どのモードですか？",
                "multiple": False,
                "options": [
                    {
                        "id": "a",
                        "label": "モードA",
                        "search_terms": ["検索A"],
                        "premise": "対象はモードA",
                        "sections": [
                            {
                                "document_id": "doc-1",
                                "section_id": "section-1",
                                "title": "項目",
                                "page_start": 2,
                                "page_end": 2,
                            }
                        ],
                    },
                    {"id": "b", "label": "モードB"},
                ],
            },
        }
    ]
}


@pytest.mark.parametrize(
    "text",
    [
        "モードAを使います。設定を教えて",
        "モードAではありません。設定の対象は未定",
        "モードAかモードBか分かりません。設定",
        "モードAとモードBの設定を比較",
        "前はモードA、今は未定。設定",
        "モードA-拡張の設定",
        "検索Aの設定",
    ],
)
def test_text_mention_is_never_a_confirmed_selection(text: str) -> None:
    assert suggest_clarification(PAYLOAD, text) is not None


@pytest.mark.parametrize(
    "ids,valid", [(["a"], True), (["a", "b"], False), (["absent"], False), (["a", "a"], False)]
)
def test_structured_choice_validates_current_ids_and_cardinality(
    ids: list[str], valid: bool
) -> None:
    answer = ClarificationAnswer(rule_id="rule-1", option_ids=ids)
    assert bool(unanswered_rule_clarifications(PAYLOAD, "設定", answers=[answer])) is not valid


async def test_positive_paraphrase_recognized_once_and_applied_to_same_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    text = "設定を教えて。最初の方式を使っています。"

    class Client:
        async def generate(self, prompt: str, context: str, **kwargs: Any) -> str:
            calls.append(json.loads(context))
            return json.dumps(
                {
                    "conditions": [
                        {
                            "condition_id": "rule-1",
                            "status": "known",
                            "option_ids": ["a"],
                            "evidence": ["最初の方式を使っています。"],
                        }
                    ]
                }
            )

    monkeypatch.setattr(rule_selection, "condition_client", lambda settings: Client())
    answers = await recognize_rule_selections(PAYLOAD, text, Settings())
    assert len(calls) == 1 and calls[0]["conditions"][0]["multiple"] is False
    assert suggest_clarification(PAYLOAD, text, answers=answers) is None
    applied = apply_rule_selection_scope(SearchRequest(query=text), PAYLOAD, answers)
    assert json.loads(applied.filters["page_ranges"])[0]["page_start"] == 2
    assert applied._rule_selection_scope["search_terms"] == ("検索A",)
    assert applied._rule_selection_scope["confirmed_premises"] == (
        ("どのモードですか？", "モードA"),
    )
    assert applied._rule_selection_scope["confirmed_premise_sources"] == (
        ("どのモードですか？", "モードA", "question"),
    )
    assert "対象はモードA" in applied._rule_selection_scope["context"]
    assert "_rule_selection_scope" not in applied.model_dump()
    assert await recognize_rule_selections(PAYLOAD, text, Settings(), answers=answers) == answers
    assert len(calls) == 1


@pytest.mark.parametrize(
    "status,ids,evidence",
    [
        ("unknown", [], []),
        ("conflicting", ["a", "b"], ["設定"]),
        ("known", ["absent"], ["設定"]),
        ("known", ["a", "b"], ["設定"]),
        ("known", ["a"], ["原文にない文"]),
        ("known", ["a"], []),
        ("known", [], ["設定"]),
    ],
)
async def test_unknown_or_unproven_values_keep_confirmation(
    monkeypatch: pytest.MonkeyPatch, status: str, ids: list[str], evidence: list[str]
) -> None:
    class Client:
        async def generate(self, *args: Any, **kwargs: Any) -> str:
            return json.dumps(
                {
                    "conditions": [
                        {
                            "condition_id": "rule-1",
                            "status": status,
                            "option_ids": ids,
                            "evidence": evidence,
                        }
                    ]
                }
            )

    monkeypatch.setattr(rule_selection, "condition_client", lambda settings: Client())
    answers = await recognize_rule_selections(PAYLOAD, "モードAではない。設定は未定", Settings())
    assert not answers
    assert suggest_clarification(PAYLOAD, "設定", answers=answers) is not None


async def test_batch_limit_and_failure_do_not_guess(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "rules": [
            {
                **PAYLOAD["rules"][0],
                "id": f"rule-{index}",
                "title": f"設定{index}",
                "content": f"説明{index}",
            }
            for index in range(40)
        ]
    }
    calls = []
    clarification = unanswered_rule_clarifications(PAYLOAD, "設定")[0][1]
    monkeypatch.setattr(
        rule_selection,
        "unanswered_rule_clarifications",
        lambda data, query, **kwargs: [(rule, clarification) for rule in data["rules"]],
    )

    class Client:
        async def generate(self, prompt: str, context: str, **kwargs: Any) -> str:
            calls.append(json.loads(context))
            raise TimeoutError()

    monkeypatch.setattr(rule_selection, "condition_client", lambda settings: Client())
    assert not await recognize_rule_selections(payload, "設定", Settings())
    assert len(calls) == 1
    assert len(calls[0]["conditions"]) == rule_selection.MAX_RULE_SELECTIONS


async def test_no_matching_conditions_does_not_call_model(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(settings: Settings) -> Any:
        raise AssertionError("モデルを呼ばない")

    monkeypatch.setattr(rule_selection, "condition_client", unavailable)
    assert not await recognize_rule_selections(PAYLOAD, "無関係", Settings())


async def test_scope_uses_current_section_pages_and_never_widens_explicit_filter() -> None:
    answer = ClarificationAnswer(rule_id="rule-1", option_ids=["a"])
    request = SearchRequest(query="設定")
    scoped = apply_rule_selection_scope(
        request, PAYLOAD, [answer], current_pages={("doc-1", "section-1"): (7, 8)}
    )
    assert json.loads(scoped.filters["page_ranges"])[0]["page_start"] == 7
    restricted = request.model_copy(
        update={
            "filters": {
                "page_ranges": json.dumps(
                    [{"document_id": "doc-1", "page_start": 1, "page_end": 1}]
                )
            }
        }
    )
    with pytest.raises(HTTPException) as conflict:
        apply_rule_selection_scope(restricted, PAYLOAD, [answer])
    assert conflict.value.status_code == 422
    assert not restricted._rule_selection_scope


async def test_shared_resolution_reuses_recognition_and_deducts_elapsed_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import search as route
    from app.rag.search_answer_profile_config import SearchAnswerProfileConfig
    from tests.test_search_answer_profile_domain_keywords import FakeKnowledgeOracle

    class Oracle(FakeKnowledgeOracle):
        async def get_search_answer_profile(self, profile_id: str) -> Any:
            view = await super().get_search_answer_profile(profile_id)
            assert view is not None
            return view.model_copy(
                update={
                    "config": SearchAnswerProfileConfig(
                        knowledge_base_ids=["kb-1"],
                        clarification_policy="assume",
                        query={"support_guide_enabled": False},
                    )
                }
            )

    oracle = Oracle()
    oracle.payloads[("bv-1", "runtime_knowledge")] = PAYLOAD
    calls = []

    async def recognize(*args: Any, **kwargs: Any) -> tuple[ClarificationAnswer, ...]:
        calls.append(args[1])
        kwargs["usage"]["elapsed_seconds"] = 4.0
        return (ClarificationAnswer(rule_id="rule-1", option_ids=["b"]),)

    async def pages(*args: Any) -> dict[Any, Any]:
        return {}

    monkeypatch.setattr(route, "OracleClient", lambda: oracle)
    monkeypatch.setattr(route, "permitted_knowledge_base_ids", lambda _ids: None)
    monkeypatch.setattr(route, "recognize_rule_selections", recognize)
    monkeypatch.setattr(route, "current_section_pages", pages)
    request = SearchRequest(query="設定はモードBです", search_answer_profile_id="bv-1")
    effective, settings, *_ = await route._resolve_query_context(
        request,
        Settings(rag_answer_timeout_seconds=30),
        interactive=True,
    )
    assert settings.rag_answer_timeout_seconds == 26
    assert "モードB" in effective._rule_selection_scope["context"]
    repeated, settings, *_ = await route._resolve_query_context(
        effective, settings, interactive=True
    )
    assert settings.rag_answer_timeout_seconds == 26
    assert len(calls) == 1
    assert repeated._rule_selection_state["elapsed_seconds"] == 4


@pytest.mark.parametrize("multiple,expected", [(True, True), (False, False)])
async def test_explicit_multiple_choices_respect_condition_cardinality(
    monkeypatch: pytest.MonkeyPatch,
    multiple: bool,
    expected: bool,
) -> None:
    from copy import deepcopy

    payload = deepcopy(PAYLOAD)
    payload["rules"][0]["clarification"]["multiple"] = multiple
    source = "設定にはモードAとモードBの両方を選びます"

    class Client:
        async def generate(self, *args: Any, **kwargs: Any) -> str:
            return json.dumps(
                {
                    "conditions": [
                        {
                            "condition_id": "rule-1",
                            "status": "known",
                            "option_ids": ["a", "b"],
                            "evidence": [source],
                        }
                    ]
                }
            )

    monkeypatch.setattr(rule_selection, "condition_client", lambda settings: Client())
    answers = await recognize_rule_selections(payload, source, Settings())
    assert bool(answers) is expected


async def test_single_complete_label_does_not_call_semantic_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from copy import deepcopy

    payload = deepcopy(PAYLOAD)
    payload["rules"][0]["triggers"] = ["モードA"]

    def unavailable(settings: Settings) -> Any:
        raise AssertionError("完全な選択は追加の意味認識を要しない")

    monkeypatch.setattr(rule_selection, "condition_client", unavailable)
    result = await recognize_rule_selections(payload, "モードA", Settings())
    assert len(result) == 1 and result[0].option_ids == ["a"]


async def test_guardrail_blocks_recognition_before_model_and_masks_allowed_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.rag.guardrails import GuardrailResult

    calls = []

    class Policy:
        allowed = False

        def __init__(self, settings: Settings) -> None:
            pass

        def validate_query(self, query: str) -> GuardrailResult:
            return GuardrailResult(
                allowed=self.allowed, sanitized_text="設定は [MASK]", findings=[]
            )

    class Client:
        async def generate(self, prompt: str, context: str, **kwargs: Any) -> str:
            calls.append(json.loads(context)["user_statements"])
            return '{"conditions": []}'

    monkeypatch.setattr(rule_selection, "GuardrailPolicy", Policy)
    monkeypatch.setattr(rule_selection, "condition_client", lambda settings: Client())
    assert not await recognize_rule_selections(PAYLOAD, "設定は非公開の値", Settings())
    assert not calls
    Policy.allowed = True
    assert not await recognize_rule_selections(PAYLOAD, "設定は非公開の値", Settings())
    assert calls == ["設定は [MASK]"]
