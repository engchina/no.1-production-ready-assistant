"""正式回答入口で監査モードの設定・呼出・最終状態をつなげて確認する。"""

import asyncio
import json
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from openai import APITimeoutError
from pr_backend_core.observability.model_purpose import model_purpose_attributes
from rag_engine.generation.citation_wire import generation_draft_wire_model
from rag_engine.resources.deadline import DeadlineExceeded

from app.config import Settings
from app.rag.answer_engine import AnswerEngine, AnswerOutcome, build_engine_settings
from app.rag.answer_validation import AnswerValidation
from app.schemas.search import SearchRequest
from tests.audit_fixtures import is_audit_schema, legacy_audit_result
from tests.test_answer_engine import FakeGenAi, FakeOracle, _fake_llm


@pytest.mark.parametrize("mode", ["none", "standard", "detailed"])
async def test_formal_answer_entry_passes_mode_and_observes_real_generation_audit_calls(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    settings = Settings(
        rag_answer_audit_mode=cast(Any, mode),
        rag_query_strategy="simple_retrieval",
        rag_section_expansion_enabled=False,
    )
    calls: list[tuple[str, str | None, str]] = []
    audit_calls: list[tuple[str, str | None, str]] = []

    def model(system: str, prompt: str, engine_settings: Any, schema: type, **options: Any) -> Any:
        calls.append(
            (
                schema.__name__,
                model_purpose_attributes().get("pr.gen_ai.purpose"),
                engine_settings.answer_audit_mode,
            )
        )
        if is_audit_schema(schema):
            audit_calls.append(calls[-1])
            data = json.loads(prompt)
            return legacy_audit_result(
                schema,
                {
                    "goal_alignment": "aligned",
                    "summary_supported": True,
                    "reviews": [
                        {
                            "index": row["index"],
                            "support": "supported",
                            "applicability": "matched",
                            "reason": "実引用を確認",
                        }
                        for row in data["items"]
                    ],
                    "request_reviews": [
                        {
                            "request_id": "Q1",
                            "status": "partial",
                            "reason": "確認方法の説明を省略",
                            "evidence_status": "unknown",
                        }
                    ],
                },
            )
        return _fake_llm(system, prompt, engine_settings, schema, **options)

    monkeypatch.setattr("rag_engine.adapters.model_api.parse_text_response", model)
    validator = AsyncMock(
        return_value=AnswerValidation(status="completed", valid=False, counts={"unsupported": 1})
    )
    monkeypatch.setattr("app.rag.answer_validation.validate_answer", validator)
    result = await AnswerEngine(
        settings, oracle=cast(Any, FakeOracle()), genai=cast(Any, FakeGenAi())
    ).run(SearchRequest(query="登録方法と登録後の確認方法は？"))
    generation = [row for row in calls if row[0] in {"GroundedDraft", "GroundedDraftWire"}]
    audits = audit_calls
    assert generation and all(row[1:] == ("answer.generate", mode) for row in generation)
    assert all(row[2] == mode for row in calls)
    assert len(generation) == 1  # unknownの材料を漏項だけで盲目的に作り直さない。
    if mode == "none":
        assert audits == []
        validator.assert_not_awaited()
        assert result.diagnostics["answer_validation"] == {
            "status": "not_run",
            "valid": None,
            "reason": "audit_disabled",
            "raw_valid": False,
            "semantic_status": "unknown",
            "automatic_actions": False,
        }
    else:
        assert len(audits) == 1 and audits[0][1] == "answer.audit"
        validator.assert_awaited_once()
        assert result.diagnostics["answer_validation"]["status"] == "completed"
        final_validation = result.diagnostics["answer_validation"]
        assert final_validation["valid"] is None
        assert final_validation["raw_valid"] is False
        assert final_validation["semantic_status"] == "unknown"
        assert final_validation["automatic_actions"] is False
        assert final_validation["counts"] == {"unsupported": 1}
        assert result.diagnostics["needs_human_review"] is True
        observed = result.diagnostics["round_budget"]["rounds"][0]["semantic_audit_observation"][
            "audit"
        ]
        assert observed["request_reviews"][0]["status"] == "partial"
        assert observed["request_reviews"][0]["evidence_status"] == "unknown"


@pytest.mark.parametrize("mode", ["none", "standard", "detailed"])
def test_backend_settings_override_engine_default_for_every_valid_mode(
    mode: str, tmp_path: Any
) -> None:
    assert Settings().rag_answer_audit_mode == "none"
    converted = build_engine_settings(
        Settings(rag_answer_audit_mode=cast(Any, mode)), output_dir=tmp_path
    )
    assert converted.answer_audit_mode == mode


@pytest.mark.parametrize(
    "failure,status",
    [
        (RuntimeError("validation failed"), "error"),
        (TimeoutError(), "timed_out"),
        (httpx.ReadTimeout("timed out"), "timed_out"),
        (APITimeoutError(request=httpx.Request("POST", "https://example.invalid")), "timed_out"),
    ],
)
async def test_final_validation_preserves_distinct_failure_execution_states(
    monkeypatch: pytest.MonkeyPatch, failure: Exception, status: str
) -> None:
    engine = AnswerEngine(
        Settings(rag_answer_audit_mode="standard"),
        oracle=cast(Any, object()),
        genai=cast(Any, object()),
    )
    outcome = AnswerOutcome(answer="合成の回答", citations=[], diagnostics={}, context_text="")
    monkeypatch.setattr("app.rag.answer_validation.validate_answer", AsyncMock(side_effect=failure))
    await engine._validate_outcome(SearchRequest(query="合成の質問"), outcome)
    assert outcome.diagnostics["answer_validation"] == {"status": status, "valid": False}
    assert outcome.answer == "合成の回答"
    assert outcome.diagnostics["needs_human_review"] is True


@pytest.mark.parametrize(
    "failure", [DeadlineExceeded(), PermissionError(), HTTPException(403), asyncio.CancelledError()]
)
async def test_outer_deadline_permission_and_cancellation_are_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    engine = AnswerEngine(
        Settings(rag_answer_audit_mode="standard"),
        oracle=cast(Any, object()),
        genai=cast(Any, object()),
    )
    monkeypatch.setattr("app.rag.answer_validation.validate_answer", AsyncMock(side_effect=failure))
    outcome = AnswerOutcome(answer="合成の回答", citations=[], diagnostics={}, context_text="")
    with pytest.raises(type(failure)):
        await engine._validate_outcome(SearchRequest(query="合成の質問"), outcome)


async def test_empty_answer_is_not_run_for_a_distinct_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator = AsyncMock()
    monkeypatch.setattr("app.rag.answer_validation.validate_answer", validator)
    engine = AnswerEngine(
        Settings(rag_answer_audit_mode="standard"),
        oracle=cast(Any, object()),
        genai=cast(Any, object()),
    )
    outcome = AnswerOutcome(answer="", citations=[], diagnostics={}, context_text="")
    await engine._validate_outcome(SearchRequest(query="合成の質問"), outcome)
    validator.assert_not_awaited()
    assert outcome.diagnostics["answer_validation"]["reason"] == "empty_answer"


async def test_none_can_retrieve_an_explicit_gap_without_enabling_an_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tests.test_answer_engine as existing

    original = existing._fake_llm
    calls: list[str] = []
    audit_schemas: list[str] = []

    def model(system: str, prompt: str, config: Any, schema: type, **options: Any) -> Any:
        calls.append(schema.__name__)
        if is_audit_schema(schema):
            audit_schemas.append(schema.__name__)
        return original(system, prompt, config, schema, **options)

    monkeypatch.setattr(existing, "_fake_llm", model)
    validator = AsyncMock()
    monkeypatch.setattr("app.rag.answer_validation.validate_answer", validator)
    oracle = existing.CoverageOracle()
    result = await existing._answer_with_coverage(
        monkeypatch,
        oracle,
        "受注の登録方法は？請求書の再発行方法は？",
        rag_answer_audit_mode="none",
    )
    assert (
        result.diagnostics["request_coverage"]["after_generation"][0]["re_retrieval"]["applied"]
        is True
    )
    assert audit_schemas == []
    validator.assert_not_awaited()
    assert result.diagnostics["answer_validation"]["reason"] == "audit_disabled"


@pytest.mark.parametrize("mode", ["standard", "detailed"])
async def test_formal_entry_does_not_spend_budget_on_uncalibrated_material_observation(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    import re

    from rag_engine.models.llm import GroundedDraft

    from tests.test_answer_engine import _doc_chunk

    operations = ["対象画面を開きます。", "一覧で対象を選択します。", "内容を確認します。"]
    oracle = FakeOracle()
    oracle.chunks = [_doc_chunk("doc-synthetic", "\n".join(operations))]
    generations, audits = 0, 0

    def model(system: str, prompt: str, config: Any, schema: type, **options: Any) -> Any:
        nonlocal generations, audits
        assert config.answer_audit_mode == mode
        assert config.answer_max_correction_rounds == 2
        if schema in (GroundedDraft, generation_draft_wire_model()):
            generations += 1
            label = re.search(r"\[(E[0-9]+)\]", prompt)
            assert label is not None
            key = label.group(1)
            return GroundedDraft.model_validate(
                {
                    "summary": "",
                    "confidence": "high",
                    "request_coverage": [
                        {
                            "request_id": f"Q{index + 1}",
                            "evidence_ids": [key],
                            "evidence_scope": [
                                {"evidence_id": key, "target": "操作", "relation": "direct"}
                            ],
                        }
                        for index in range(3)
                    ],
                    "items": [
                        {
                            "kind": "operation",
                            "request_id": f"Q{index + 1}",
                            "text": operation,
                            "citations": [
                                {
                                    "evidence_id": key,
                                    "quote": operation,
                                }
                            ],
                        }
                        for index, operation in enumerate(operations[:generations])
                    ],
                }
            )
        if is_audit_schema(schema):
            audits += 1
            data = json.loads(prompt)
            key = next(iter(data["evidence"]))
            requests = []
            for index, operation in enumerate(operations):
                addressed = index < generations
                requests.append(
                    {
                        "request_id": f"Q{index + 1}",
                        "status": "addressed" if addressed else "partial",
                        "reason": "説明済み" if addressed else "原文はあるが操作の説明がない",
                        "evidence_status": "unknown" if addressed else "available",
                        "missing_content": "" if addressed else operation,
                        "evidence_ids": [] if addressed else [key],
                        "evidence_support": []
                        if addressed
                        else [
                            {
                                "evidence_id": key,
                                "quote": operation,
                                "source": data["evidence_sources"][key],
                            }
                        ],
                    }
                )
            return legacy_audit_result(
                schema,
                {
                    "summary_supported": True,
                    "goal_alignment": "aligned",
                    "reviews": [
                        {
                            "index": row["index"],
                            "support": "supported",
                            "applicability": "matched",
                            "reason": "原文どおり",
                        }
                        for row in data["items"]
                    ],
                    "request_reviews": requests,
                },
            )
        return _fake_llm(system, prompt, config, schema, **options)

    monkeypatch.setattr("rag_engine.adapters.model_api.parse_text_response", model)
    monkeypatch.setattr(
        "app.rag.answer_validation.validate_answer",
        AsyncMock(return_value=AnswerValidation(status="completed", valid=True)),
    )
    settings = Settings(
        rag_answer_audit_mode=cast(Any, mode),
        rag_answer_max_correction_rounds=2,
        rag_query_strategy="simple_retrieval",
        rag_section_expansion_enabled=False,
    )
    result = await AnswerEngine(
        settings, oracle=cast(Any, oracle), genai=cast(Any, FakeGenAi())
    ).run(
        SearchRequest(
            query=(
                "1. 対象画面を開く方法は？\n"
                "2. 一覧で対象を選択する方法は？\n"
                "3. 内容を確認する方法は？"
            )
        )
    )
    assert generations == 1, json.dumps(result.diagnostics["round_budget"], ensure_ascii=False)
    assert audits == 1
    assert generations <= 1 + settings.rag_answer_max_correction_rounds
    assert result.diagnostics["answer_validation"]["status"] == "completed"
    assert result.diagnostics["answer_validation"]["valid"] is None
    assert result.diagnostics["answer_validation"]["raw_valid"] is True
    assert result.diagnostics["answer_validation"]["semantic_status"] == "unknown"
    observed = result.diagnostics["round_budget"]["rounds"][0]["semantic_audit_observation"][
        "audit"
    ]
    assert [row["request_id"] for row in observed["request_reviews"]] == ["Q1", "Q2", "Q3"]
    assert [row["missing_content"] for row in observed["request_reviews"][1:]] == operations[1:]
    assert all(row["evidence_status"] == "available" for row in observed["request_reviews"][1:])
    assert [
        row["evidence_support"][0]["quote"] for row in observed["request_reviews"][1:]
    ] == operations[1:]
    assert operations[0] in result.answer


@pytest.mark.parametrize(
    "invalid_id,reason", [(True, "citation_id_invalid"), (False, "citation_range_out_of_bounds")]
)
async def test_formal_none_entry_keeps_invalid_id_and_range_checks(
    monkeypatch: pytest.MonkeyPatch, invalid_id: bool, reason: str
) -> None:
    import re

    from rag_engine.models.llm import GroundedDraft

    from tests.test_answer_engine import _doc_chunk

    oracle = FakeOracle()
    oracle.chunks = [_doc_chunk("doc-synthetic", "対象画面を開きます。" + "説明が続きます。" * 120)]
    schemas: list[str] = []

    def model(system: str, prompt: str, config: Any, schema: type, **options: Any) -> Any:
        schemas.append(schema.__name__)
        if schema in (GroundedDraft, generation_draft_wire_model()):
            found = re.search(r"\[(E[0-9]+)\]", prompt)
            assert found is not None
            return GroundedDraft.model_validate(
                {
                    "summary": "",
                    "confidence": "low",
                    "items": [
                        {
                            "kind": "operation",
                            "text": "資料にない別の説明",
                            "citations": [
                                {
                                    "evidence_id": "absent" if invalid_id else found.group(1),
                                    "lines": "1" if invalid_id else "999",
                                }
                            ],
                        }
                    ],
                }
            )
        return _fake_llm(system, prompt, config, schema, **options)

    monkeypatch.setattr("rag_engine.adapters.model_api.parse_text_response", model)
    validator = AsyncMock()
    monkeypatch.setattr("app.rag.answer_validation.validate_answer", validator)
    result = await AnswerEngine(
        Settings(
            rag_answer_audit_mode="none",
            rag_query_strategy="simple_retrieval",
            rag_section_expansion_enabled=False,
        ),
        oracle=cast(Any, oracle),
        genai=cast(Any, FakeGenAi()),
    ).run(SearchRequest(query="対象の操作手順は？"))
    assert schemas in (["GroundedDraft"], ["GroundedDraftWire"])
    validator.assert_not_awaited()
    marked = result.diagnostics["envelope"]["passage_marks"]
    assert reason in {proof["code"] for row in marked for proof in row["reasons"]}
