"""回答対象の原文も検索文と同じ安全境界で検査する。"""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.clients.oci_guardrails import GuardrailInspection, OciGuardrailsClient, PiiSpan
from app.config import Settings, get_settings
from app.main import app
from app.rag import pipeline as pipeline_module
from app.rag.guardrails import GuardrailResult
from app.rag.pipeline import RagPipeline
from app.rag.run_history import RunRecorder
from app.schemas.search import SearchRequest, SearchResponse
from tests.support import AsgiTestClient


@pytest.fixture
def boundary(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {"checks": [], "requests": [], "findings": []}

    def inspect(self: object, text: str) -> GuardrailInspection | None:
        captured["checks"].append(text)
        if text == "合成の攻撃":
            return GuardrailInspection(prompt_injection=True)
        if text == "合成の不適切な内容":
            return GuardrailInspection(moderation_categories=("HATE",))
        if text == "合成の検査障害":
            raise RuntimeError("合成の検査障害")
        if text == "合成の空応答":
            return None
        email = "synthetic@example.com"
        return GuardrailInspection(
            pii_spans=(PiiSpan(text.index(email), len(email), "EMAIL"),) if email in text else ()
        )

    async def enter(self: object, request: SearchRequest, **kwargs: Any) -> SearchResponse:
        captured["requests"].append(request)
        return SearchResponse(
            answer="",
            trace_id=kwargs["trace_id"],
            guardrail_warnings=kwargs["query_guardrail"].warnings,
            elapsed_ms=0,
        )

    monkeypatch.setattr(OciGuardrailsClient, "inspect_text", inspect)
    monkeypatch.setattr(RagPipeline, "_run_engine", enter)
    monkeypatch.setattr(RagPipeline, "_run_engine_retrieval", enter)
    monkeypatch.setattr(
        pipeline_module,
        "record_guardrail_findings",
        lambda stage, findings, outcome: captured["findings"].extend(findings),
    )
    return captured


@pytest.mark.parametrize("mode", ["answer", "search", "retrieval-only"])
@pytest.mark.parametrize("prechecked", [False, True])
@pytest.mark.parametrize(
    "original", ["合成の攻撃", "合成の不適切な内容", "合成の検査障害", "合成の空応答"]
)
async def test_original_question_blocks_before_any_engine(
    boundary: dict[str, Any], mode: str, original: str, prechecked: bool
) -> None:
    checked = GuardrailResult(allowed=True, sanitized_text="合成の安全な検索文", findings=[])
    response = await RagPipeline(
        settings=Settings(rag_guardrails_input_enabled=True, rag_guardrails_output_enabled=True)
    ).run(
        SearchRequest(
            query="合成の安全な検索文",
            original_question=original,
            generate_answer=mode == "answer",
            retrieval_only=mode == "retrieval-only",
        ),
        query_guardrail_result=checked if prechecked else None,
    )
    assert boundary["requests"] == []
    assert boundary["checks"] == ([original] if prechecked else ["合成の安全な検索文", original])
    assert response.diagnostics.retrieval_strategy_adapter == "blocked"
    assert response.guardrail_warnings
    assert boundary["findings"]


@pytest.mark.parametrize("prechecked", [False, True])
@pytest.mark.parametrize("retrieval_only", [False, True])
async def test_both_inputs_are_masked_and_findings_preserved(
    boundary: dict[str, Any], prechecked: bool, retrieval_only: bool
) -> None:
    query = "検索 synthetic@example.com"
    result = GuardrailResult(allowed=True, sanitized_text="検査済みの検索文", findings=[])
    original = "元の質問 synthetic@example.com"
    request = SearchRequest(query=query, original_question=original, retrieval_only=retrieval_only)
    response = await RagPipeline(
        settings=Settings(rag_guardrails_input_enabled=True, rag_guardrails_output_enabled=True)
    ).run(request, query_guardrail_result=result if prechecked else None)
    passed = boundary["requests"][0]
    assert "synthetic@example.com" not in passed.query
    assert passed.original_question == "元の質問 [機微情報]"
    assert request.original_question == original
    assert response.guardrail_warnings
    assert len(boundary["findings"]) == (1 if prechecked else 2)
    assert boundary["checks"] == ([original] if prechecked else [query, original])


@pytest.mark.parametrize("original", [None, "合成の質問"])
async def test_missing_or_identical_original_does_not_add_inspection(
    boundary: dict[str, Any], original: str | None
) -> None:
    await RagPipeline(
        settings=Settings(rag_guardrails_input_enabled=True, rag_guardrails_output_enabled=True)
    ).run(SearchRequest(query="合成の質問", original_question=original))
    assert boundary["checks"] == ["合成の質問"]
    assert len(boundary["requests"]) == 1


async def test_disabled_guardrails_preserves_original(boundary: dict[str, Any]) -> None:
    await RagPipeline(
        settings=Settings(rag_guardrails_input_enabled=False, rag_guardrails_output_enabled=False)
    ).run(SearchRequest(query="合成の質問", original_question="合成の攻撃"))
    assert boundary["checks"] == []
    assert boundary["requests"][0].original_question == "合成の攻撃"


@pytest.mark.parametrize("generate_answer", [False, True])
@pytest.mark.parametrize("original", ["合成の攻撃", "元の質問 synthetic@example.com"])
def test_rest_checks_original_before_entering_engine(
    monkeypatch: pytest.MonkeyPatch,
    boundary: dict[str, Any],
    generate_answer: bool,
    original: str,
) -> None:
    monkeypatch.setattr(get_settings(), "rag_guardrails_input_enabled", True)
    monkeypatch.setattr(get_settings(), "rag_guardrails_output_enabled", True)
    monkeypatch.setattr(RunRecorder, "record", AsyncMock())
    response = AsgiTestClient(app).post(
        "/api/search",
        json={
            "query": "合成の検索文",
            "original_question": original,
            "generate_answer": generate_answer,
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["guardrail_warnings"]
    if original == "合成の攻撃":
        assert boundary["requests"] == []
        assert data["diagnostics"]["retrieval_strategy_adapter"] == "blocked"
    else:
        assert boundary["requests"][0].original_question == "元の質問 [機微情報]"
