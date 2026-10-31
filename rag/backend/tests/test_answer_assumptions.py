"""確認を省略する方針から最終生成プロンプトまでの回帰テスト。"""

import json
from pathlib import Path
from typing import Any

import pytest
from rag_engine.generation.citation_wire import generation_draft_wire_model
from rag_engine.generation.grounded import ASSUMPTION_SYSTEM_PROMPT
from rag_engine.knowledge.runtime_knowledge import build_runtime_knowledge_context
from rag_engine.models.llm import GroundedDraft

from app.api.routes import search as search_route
from app.config import Settings
from app.rag.answer_assumptions import with_answer_assumptions
from app.rag.answer_engine import AnswerEngine
from app.rag.search_answer_profile_config import SearchAnswerProfileConfig
from app.schemas.search import SearchRequest
from app.schemas.search_answer_profile_knowledge import ClarificationAnswer
from app.schemas.support_guide import SupportGuideContent
from tests.test_answer_engine import FakeGenAi, FakeOracle, _fake_llm
from tests.test_search_answer_profile_domain_keywords import FakeKnowledgeOracle

QUESTION = "設定の変更方法を教えてください"
PENDING = {
    "condition_id": "rule:r1",
    "question": "設定の対象はどちらですか？",
    "options": ["対象 A", "対象 B"],
}
PAYLOAD: dict[str, Any] = {
    "terms": [],
    "rules": [
        {
            "id": "r1",
            "title": "設定の確認",
            "triggers": ["設定"],
            "content": "対象によって手順が異なる。",
            "clarification": {
                "question": PENDING["question"],
                "options": [{"id": "a", "label": "対象 A"}, {"id": "b", "label": "対象 B"}],
            },
        }
    ],
}


def prepared(policy: str = "assume", **kwargs: Any) -> dict[str, Any]:
    return with_answer_assumptions(
        PAYLOAD, policy=policy, question=QUESTION, conditions={}, guide={}, **kwargs
    )


def test_assumptions_are_scoped_and_do_not_choose_unknown_options(tmp_path: Path) -> None:
    result = prepared()
    assert result["unconfirmed_conditions"] == [PENDING]
    assert "assume_unconfirmed" not in PAYLOAD
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(result), encoding="utf-8")
    context = build_runtime_knowledge_context(QUESTION, tmp_path, path)
    assert context.has_matches
    assert context.to_payload()["unconfirmed_conditions"] == [
        {**PENDING, "question": str(PENDING["question"]).replace("？", "?")}
    ]
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(PAYLOAD), encoding="utf-8")
    assert (
        context.expanded_question
        == build_runtime_knowledge_context(QUESTION, tmp_path, baseline_path).expanded_question
    )
    assert context.scope_context == ""
    prompt = context.prompt_context()
    assert (
        str(PENDING["question"]).replace("？", "?") in prompt
        and "対象 A" in prompt
        and "対象 B" in prompt
    )
    assert prepared(answered_rule_ids=("r1",))["unconfirmed_conditions"] == []
    assert (
        with_answer_assumptions(
            result, policy="confirm", question=QUESTION, conditions={}, guide={}
        ).get("assume_unconfirmed")
        is None
    )


@pytest.mark.parametrize(
    "question,conditions", [(QUESTION + "。対象 A です", {}), (QUESTION, {"rule:r1": "対象 A"})]
)
def test_answered_conditions_are_not_marked_unknown(
    question: str, conditions: dict[str, str]
) -> None:
    result = with_answer_assumptions(
        PAYLOAD,
        policy="assume",
        question=question,
        conditions=conditions,
        guide={},
        confirmed_answers=(ClarificationAnswer(rule_id="r1", option_ids=["a"]),)
        if not conditions
        else (),
    )
    assert result["unconfirmed_conditions"] == []


@pytest.mark.usefixtures("system_access")
@pytest.mark.parametrize("policy", ["confirm", "assume"])
@pytest.mark.parametrize("with_guide", [False, True])
async def test_search_chat_mcp_shared_context_forwards_rule_and_guide_questions(
    monkeypatch: pytest.MonkeyPatch, policy: str, with_guide: bool
) -> None:
    class Oracle(FakeKnowledgeOracle):
        async def get_search_answer_profile(self, profile_id: str) -> Any:
            view = await super().get_search_answer_profile(profile_id)
            assert view is not None
            return view.model_copy(
                update={
                    "config": SearchAnswerProfileConfig(
                        knowledge_base_ids=["kb-1"],
                        clarification_policy=policy,
                        query={"support_guide_enabled": with_guide},
                    )
                }
            )

    oracle = Oracle()
    oracle.payloads[("bv-1", "runtime_knowledge")] = PAYLOAD
    monkeypatch.setattr(search_route, "OracleClient", lambda: oracle)
    guide = SupportGuideContent.model_validate(
        {
            "title": "設定ガイド A",
            "goal": {"expected_result": "設定を更新する", "match_terms": ["設定"]},
            "conditions": [
                {
                    "id": "target",
                    "label": "対象",
                    "question": "対象を教えてください",
                    "allowed_values": ["対象 A", "対象 B"],
                }
            ],
            "steps": [{"id": "open", "title": "設定画面を開く"}],
        }
    )

    async def load(*args: Any) -> Any:
        return ([("guide-a", 1, guide)] if with_guide else []), False

    async def context(*args: Any) -> Any:
        return None

    monkeypatch.setattr(search_route, "load_published_guides", load)
    monkeypatch.setattr(search_route, "support_guide_context", context)
    _, settings, _, profile = await search_route._resolve_query_context(
        SearchRequest(query=QUESTION, search_answer_profile_id="bv-1"),
        Settings(rag_support_guide_semantic_match_enabled=False),
        interactive=True,
    )
    assert profile == "bv-1"
    result = settings.rag_runtime_knowledge
    assert result.get("assume_unconfirmed", False) is (policy == "assume")
    if policy == "assume":
        pending = result["unconfirmed_conditions"]
        assert isinstance(pending, list)
        assert pending[0] == PENDING
        assert len(pending) == (2 if with_guide else 1)
        assert not settings.rag_support_guide.get("short_answer")


@pytest.mark.parametrize("policy", ["confirm", "assume"])
async def test_final_answer_generation_receives_assumptions(
    monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    import rag_engine.adapters.oci as engine_oci

    prompts: list[str] = []

    def llm(system: str, prompt: str, settings: Any, schema: type, **options: Any) -> Any:
        output = _fake_llm(system, prompt, settings, schema, **options)
        if schema in (GroundedDraft, generation_draft_wire_model()):
            prompts.append(system)
            assert system.endswith(ASSUMPTION_SYSTEM_PROMPT) is (policy == "assume")
            if policy == "assume":
                assert str(PENDING["question"]).replace("？", "?") in prompt
                assert "対象 A" in prompt and "対象 B" in prompt
                output.summary = "前提: 設定の対象は未確定です。"
        return output

    monkeypatch.setattr(engine_oci, "parse_text_response", llm)
    outcome = await AnswerEngine(
        Settings(rag_answer_flow="standard_rag"),
        oracle=FakeOracle(),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
        runtime_knowledge_payload=prepared(policy),
    ).run(SearchRequest(query=QUESTION))
    assert prompts
    assert ("前提: 設定の対象は未確定です。" in outcome.answer) is (policy == "assume")
    assert all(
        prompt.endswith(ASSUMPTION_SYSTEM_PROMPT) is (policy == "assume") for prompt in prompts
    )


@pytest.mark.parametrize("answered", [False, True])
async def test_mcp_assume_skips_questions_but_applies_explicit_answers(
    monkeypatch: pytest.MonkeyPatch, answered: bool
) -> None:
    from app.mcp import tools

    class Oracle(FakeKnowledgeOracle):
        async def get_search_answer_profile(self, profile_id: str) -> Any:
            view = await super().get_search_answer_profile(profile_id)
            assert view is not None
            return view.model_copy(
                update={
                    "config": SearchAnswerProfileConfig(
                        knowledge_base_ids=["kb-1"],
                        clarification_policy="assume",
                    )
                }
            )

    oracle = Oracle()
    oracle.payloads[("bv-1", "runtime_knowledge")] = PAYLOAD
    monkeypatch.setattr(tools, "OracleClient", lambda: oracle)
    arguments = tools.SearchInput(
        query=QUESTION,
        search_answer_profile_id="bv-1",
        conditions={"rule:r1": "対象 A"} if answered else {},
    )
    request = SearchRequest(
        query=QUESTION, search_answer_profile_id="bv-1", conditions=arguments.conditions
    )
    result = await tools.rule_clarification_step(arguments, request)
    assert isinstance(result, tuple)
    _, scope = result
    if answered:
        assert scope is not None and "対象 A" in scope.context
    else:
        assert scope is None
