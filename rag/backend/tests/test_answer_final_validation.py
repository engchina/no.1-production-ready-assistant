"""最終回答も LLM 監査のスイッチに従い MCP と同じ照合を使う。"""

from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.rag.answer_engine import AnswerEngine, AnswerOutcome, AnswerScope
from app.rag.answer_validation import AnswerValidation, EvidenceRef
from app.schemas.search import RetrievedChunk, SearchRequest


@pytest.mark.parametrize("mode", ["none", "standard", "detailed"])
@pytest.mark.parametrize("valid", [True, False])
async def test_final_validation_follows_audit_mode_and_preserves_failed_verdict(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    valid: bool,
) -> None:
    settings = Settings(rag_answer_audit_mode=cast(Any, mode))
    oracle = object()
    engine = AnswerEngine(settings=settings, oracle=cast(Any, oracle), genai=cast(Any, object()))
    citation = RetrievedChunk(
        document_id="doc-synthetic", chunk_id="chunk-synthetic", text="合成の根拠", score=1
    )
    outcome = AnswerOutcome(
        answer="合成の回答",
        citations=[citation],
        diagnostics={"outcome": "answered", "envelope": {"outcome": "answered"}},
    )
    validator = AsyncMock(
        return_value=AnswerValidation(
            status="completed",
            valid=valid,
            counts={"supported": 1 if valid else 0},
            llm_usage={"calls": 1},
        )
    )
    monkeypatch.setattr("app.rag.answer_validation.validate_answer", validator)
    callback = AsyncMock()
    await engine._validate_outcome(
        SearchRequest(query="合成の質問"), outcome, step_callback=callback
    )
    if mode == "none":
        validator.assert_not_awaited()
        callback.assert_not_awaited()
        assert outcome.diagnostics["answer_validation"] == {
            "status": "not_run",
            "valid": False,
            "reason": "audit_disabled",
        }
        return
    validator.assert_awaited_once_with(
        "合成の質問",
        "合成の回答",
        [EvidenceRef(citation.document_id, citation.chunk_id)],
        settings,
        oracle=oracle,
        premises=[],
    )
    assert outcome.diagnostics["answer_validation"]["valid"] is valid
    assert outcome.diagnostics["execution_steps"][0]["llm_calls"] == 1
    assert callback.await_args_list[0].args[:2] == ("資料との照合", "started")
    assert callback.await_args_list[-1].args[:2] == ("資料との照合", "success")
    if not valid:
        assert outcome.diagnostics["needs_human_review"] is True
        assert outcome.diagnostics["confidence"] == "low"
        assert outcome.diagnostics["outcome"] == "conditional"
        assert outcome.diagnostics["envelope"]["outcome"] == "conditional"
        assert "資料との照合" in outcome.diagnostics["insufficient_reason"]


async def test_final_validation_failure_remains_unverified(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = AnswerEngine(
        settings=Settings(rag_answer_audit_mode="standard"),
        oracle=cast(Any, object()),
        genai=cast(Any, object()),
    )
    outcome = AnswerOutcome(answer="合成の回答", citations=[], diagnostics={})
    monkeypatch.setattr(
        "app.rag.answer_validation.validate_answer", AsyncMock(side_effect=TimeoutError())
    )
    await engine._validate_outcome(SearchRequest(query="合成の質問"), outcome)
    assert outcome.diagnostics["answer_validation"] == {"status": "timed_out", "valid": False}
    assert outcome.diagnostics["needs_human_review"] is True
    assert outcome.answer == "合成の回答"


@pytest.mark.parametrize("valid", [None, True, False])
@pytest.mark.parametrize("version", ["", "same", "different"])
async def test_failed_or_unversioned_final_verdict_is_not_reused_as_supported(
    monkeypatch: pytest.MonkeyPatch,
    valid: bool | None,
    version: str,
) -> None:
    from app.rag.answer_engine import ANSWER_CITATION_LINES_KEY
    from app.rag.answer_validation import audited_answer_lines

    diagnostics: dict[str, Any] = {"envelope": {"items": []}}
    if valid is not None:
        diagnostics["answer_validation"] = {
            "valid": valid,
            "status": "completed",
            "reuse_context": version,
            "claim_checks": [
                {"status": "supported", "answer_quote": "合成の回答", "source_id": "chunk-1"}
            ],
        }

    class Oracle:
        async def get_answer_record(self, trace_id: str) -> dict[str, Any]:
            return {
                "answer": "合成の回答",
                "diagnostics_json": diagnostics,
                "citations_json": [
                    {
                        "chunk_id": "chunk-1",
                        "text": "合成の根拠",
                        "metadata": {ANSWER_CITATION_LINES_KEY: [1]},
                    }
                ],
            }

    monkeypatch.setattr(
        "rag_engine.evaluation.answer_validation.audited_answer_items",
        lambda *args: {"合成の回答": ["chunk-1"]},
    )
    reused = await audited_answer_lines(
        cast(Any, Oracle()), ["trace-1"], {"chunk-1": {"text": "合成の根拠"}}, reuse_context="same"
    )
    assert reused == ({"合成の回答": ["chunk-1"]} if valid is True and version == "same" else {})


async def test_final_validation_uses_original_question_and_confirmed_conditions_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        rag_answer_audit_mode="standard",
        rag_support_guide_enabled=True,
        rag_support_guide={
            "known_conditions": [
                {"label": "対象", "value": "合成 A", "source": "user", "state": "known"},
                {"label": "状態", "value": "合成 B", "source": "question", "state": "known"},
                {"label": "未確定", "value": "仮定", "source": "user", "state": "unknown"},
                {"label": "資料の値", "value": "合成 C", "source": "evidence", "state": "known"},
            ],
            "unknown_conditions": [{"label": "未確認", "value": "使用しない"}],
        },
        rag_runtime_knowledge={
            "assume_unconfirmed": True,
            "unconfirmed_conditions": [{"question": "未確認", "options": ["仮定"]}],
        },
    )
    engine = AnswerEngine(
        settings=settings,
        oracle=cast(Any, object()),
        genai=cast(Any, object()),
        scope=AnswerScope(
            context="前提: 管理された指示。利用者が答えた事実ではない。",
            confirmed_premises=(("対象は？", "合成選択肢"),),
        ),
    )
    outcome = AnswerOutcome(answer="合成の回答", citations=[], diagnostics={})
    validator = AsyncMock(return_value=AnswerValidation(status="completed", valid=True))
    monkeypatch.setattr("app.rag.answer_validation.validate_answer", validator)
    await engine._validate_outcome(
        SearchRequest(query="検索用の文", original_question="元の質問のすべての要求"), outcome
    )
    assert validator.await_args is not None
    assert validator.await_args.args[0] == "元の質問のすべての要求"
    assert validator.await_args.kwargs["premises"] == [
        {"question": "対象は？", "answer": "合成選択肢"},
        {"question": "対象", "answer": "合成 A"},
        {"question": "状態", "answer": "合成 B"},
    ]


async def test_final_validation_never_silently_drops_excess_confirmed_conditions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.rag.answer_validation import MAX_PREMISES

    engine = AnswerEngine(
        settings=Settings(rag_answer_audit_mode="standard"),
        oracle=cast(Any, object()),
        genai=cast(Any, object()),
        scope=AnswerScope(
            context="",
            confirmed_premises=tuple((f"条件 {n}", "合成値") for n in range(MAX_PREMISES + 1)),
        ),
    )
    outcome = AnswerOutcome(answer="合成の回答", citations=[], diagnostics={"outcome": "answered"})
    validator = AsyncMock()
    monkeypatch.setattr("app.rag.answer_validation.validate_answer", validator)
    await engine._validate_outcome(SearchRequest(query="合成の質問"), outcome)
    validator.assert_not_awaited()
    assert outcome.diagnostics["answer_validation"]["status"] == "input_too_large"
    assert outcome.diagnostics["answer_validation"]["unassessed_premises_count"] == MAX_PREMISES + 1
    assert outcome.diagnostics["answer_validation"]["valid"] is False
    assert outcome.diagnostics["needs_human_review"] is True
    fields = outcome.diagnostics["execution_steps"][0]["details"]["fields"]
    assert {"label": "照合の実行", "value": "照合の入力上限超過"} in fields
    assert {"label": "未照合の確認条件", "value": str(MAX_PREMISES + 1)} in fields


@pytest.mark.parametrize(
    "selection,other,expected",
    [(["a"], "自由入力", "合成 A、自由入力"), ([], "自由入力", "自由入力")],
)
async def test_resolved_rule_answer_reaches_final_validation_without_configured_premise(
    monkeypatch: pytest.MonkeyPatch,
    selection: list[str],
    other: str,
    expected: str,
) -> None:
    from app.rag.search_answer_profile_knowledge import resolve_clarification
    from app.schemas.search_answer_profile_knowledge import ClarificationAnswer

    payload = {
        "rules": [
            {
                "id": "rule-a",
                "clarification": {
                    "question": "合成の対象は？",
                    "options": [
                        {
                            "id": "a",
                            "label": "合成 A",
                            "premise": "設定した指示はユーザーの事実ではない。",
                        },
                        {"id": "b", "label": "合成 B"},
                    ],
                },
            }
        ]
    }
    resolved = resolve_clarification(
        payload, ClarificationAnswer(rule_id="rule-a", option_ids=selection, other_text=other)
    )
    assert resolved is not None
    scope, _ = resolved
    settings = Settings(rag_answer_audit_mode="standard")
    engine = AnswerEngine(
        settings=settings, oracle=cast(Any, object()), genai=cast(Any, object()), scope=scope
    )
    outcome = AnswerOutcome(answer="合成の回答", citations=[], diagnostics={})
    validator = AsyncMock(return_value=AnswerValidation(status="completed", valid=True))
    monkeypatch.setattr("app.rag.answer_validation.validate_answer", validator)
    await engine._validate_outcome(SearchRequest(query="合成の質問"), outcome)
    assert validator.await_args is not None
    assert validator.await_args.kwargs["premises"] == [
        {"question": "合成の対象は？", "answer": expected}
    ]


def test_disabled_guide_and_unstructured_scope_do_not_create_confirmed_facts() -> None:
    from app.rag.answer_engine import _confirmed_validation_premises

    settings = Settings(
        rag_support_guide_enabled=False,
        rag_support_guide={
            "known_conditions": [
                {"label": "条件", "value": "値", "source": "user", "state": "known"},
            ]
        },
    )
    assert _confirmed_validation_premises(settings, AnswerScope(context="前提: 推測の文章")) == []


async def test_final_validation_records_the_premise_consistency_decisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回答の記録の診断に、両立の比較の採否を残す（#3800。Chat の診断が読む）。"""
    engine = AnswerEngine(
        settings=Settings(rag_answer_audit_mode="standard"),
        oracle=cast(Any, object()),
        genai=cast(Any, object()),
    )
    citation = RetrievedChunk(
        document_id="doc-synthetic", chunk_id="chunk-synthetic", text="合成の根拠", score=1
    )
    outcome = AnswerOutcome(answer="合成の回答", citations=[citation], diagnostics={})
    checks = [
        {
            "answer_passage_id": "A1",
            "status": "completed",
            "decision": "conflict_adopted",
            "adopted": True,
            "fit": "conflicts",
            "constraint_scope": "proposal",
            "passage_mode": "proposal",
        }
    ]
    monkeypatch.setattr(
        "app.rag.answer_validation.validate_answer",
        AsyncMock(
            return_value=AnswerValidation(
                status="completed",
                valid=False,
                counts={"contradicted": 1},
                premise_consistency_checks=checks,
            )
        ),
    )
    await engine._validate_outcome(SearchRequest(query="合成の質問"), outcome)
    assert outcome.diagnostics["answer_validation"]["premise_consistency_checks"] == checks
