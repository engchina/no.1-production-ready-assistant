"""合成の拒答と根拠のない操作を、主張の判定からケースの失敗理由まで検証する。"""

import json
from pathlib import Path
from typing import Any

import pytest
from rag_engine.evaluation.answer_eval import evaluate_answer_payload

from app.config import Settings
from app.rag.answer_metrics import score_answer_evaluation
from app.rag.evaluation import score_case_answers, summarize_answer_judgement
from app.schemas.evaluation import EvaluationCase
from app.schemas.search import RetrievedChunk, SearchDiagnostics, SearchResponse

CASES = json.loads((Path(__file__).parent / "fixtures/abstention_evaluation.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_abstention_claims_and_case_failure_reasons(
    case: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_model_call(*args: Any, **kwargs: Any) -> None:
        pytest.fail("定型の不足説明と出典のない主張はコードで判定する")

    monkeypatch.setattr(
        "rag_engine.evaluation.answer_eval.parse_text_response", unexpected_model_call
    )
    citations = [
        RetrievedChunk(document_id="doc-a", chunk_id="chunk-a", text="項目 B の説明。", score=0.5)
    ]
    evaluated = evaluate_answer_payload(
        {
            "question": "項目 A の設定方法は？",
            "answer_text": case["answer"],
            "allow_without_standard": True,
            "six_metrics": True,
            "insufficient_reason": "設定方法を確認できません。",
            "evidence_items": [
                {"id": "chunk-a", "source": "synthetic.txt", "text": "項目 B の説明。"}
            ],
        },
        object(),
    )
    assert evaluated["status"] == "completed"
    scored = score_answer_evaluation(
        evaluated,
        answer=case["answer"],
        citations=citations,
        insufficient_reason="設定方法を確認できません。",
        settings=Settings(),
    )
    judgement = summarize_answer_judgement(scored)
    assert judgement.claim_support_rate == case["support"]
    if case.get("unassessed"):
        assert judgement.claims_supported is None
        assert judgement.passed is None
    else:
        assert judgement.claims_supported is not case["unsupported"]
    response = SearchResponse(
        answer=case["answer"],
        citations=citations,
        trace_id="synthetic-trace",
        elapsed_ms=1,
        diagnostics=SearchDiagnostics(answer={"outcome": "insufficient_evidence"}),
    )
    result = score_case_answers(
        EvaluationCase(
            id=case["id"], query="項目 A の設定方法は？", expected_response_decision="hold"
        ),
        [(response, {})],
        judgement,
    )
    assert result.response_decision_accuracy == 1.0
    assert ("unsupported_claim" in result.failure_reasons) is case["unsupported"]
    assert ("claim_unassessed" in result.failure_reasons) is bool(case.get("unassessed"))


@pytest.mark.parametrize("raw", ["わかりません", "分かりません", "不明です"])
def test_generation_and_judgement_share_custom_abstention_labels(raw: str) -> None:
    from unittest.mock import Mock

    from rag_engine.generation.answer_payload import parse_answer_response
    from rag_engine.generation.service import GenerationService
    from rag_engine.models.contracts import GenerationRequest
    from rag_parser_core.language import bind_pack, builtin_pack, resolve

    summary = "合成の根拠不足の案内です。"
    guidance = "合成の確認の案内です。"
    pack = resolve(
        builtin_pack(),
        {
            "answer_labels": {
                "insufficient_evidence_summary": summary,
                "empty_search_guidance": guidance,
                "abstention_guidance": guidance,
            }
        },
    )
    generator = Mock()
    with bind_pack(pack):
        generated = GenerationService(generator).generate(GenerationRequest("項目 A は？", ()))
        assert generated.text == summary
        normalized = parse_answer_response(raw)
        assert normalized.answer_text == summary + guidance
        evaluated = evaluate_answer_payload(
            {"answer_text": normalized.answer_text, "allow_without_standard": True},
            object(),
        )
    generator.generate.assert_not_called()
    assert evaluated["status"] == "completed"
    assert all(claim["status"] == "not_a_claim" for claim in evaluated["claim_checks"])
    assert pack.ingest_hash == builtin_pack().ingest_hash
