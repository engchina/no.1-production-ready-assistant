"""実際の最終検証を通し、mode の分流と内部のモデル判定・集計を一緒に確かめる。"""

import json
from typing import Any, Literal, cast

import pytest
from rag_engine.resources.llm_usage import record_llm_usage

from app.config import Settings
from app.rag.answer_engine import AnswerEngine, AnswerOutcome
from app.schemas.search import RetrievedChunk, SearchRequest


@pytest.mark.parametrize("mode", ["none", "standard", "detailed"])
@pytest.mark.parametrize("verdict", ["supported", "unsupported"])
async def test_final_validation_mode_reaches_real_claim_parser_and_keeps_its_verdict(
    monkeypatch: pytest.MonkeyPatch,
    mode: Literal["none", "standard", "detailed"],
    verdict: Literal["supported", "unsupported"],
) -> None:
    from rag_engine.evaluation import answer_eval
    from rag_engine.evaluation.answer_validation import ClaimAuditOutput

    source = "有効期限は最長 30 日です。"
    citation = RetrievedChunk(
        document_id="document-a",
        chunk_id="chunk-a",
        text=source,
        score=1,
        file_name="synthetic.pdf",
        metadata={"page_start": 1},
    )
    loaded: list[tuple[str, str]] = []
    parsed: list[dict[str, Any]] = []

    class Oracle:
        async def retrievable_chunk(self, document_id: str, chunk_id: str) -> RetrievedChunk | None:
            loaded.append((document_id, chunk_id))
            return citation if (document_id, chunk_id) == ("document-a", "chunk-a") else None

        async def accessible_chunk_exists(self, document_id: str, chunk_id: str) -> bool:
            return False

        async def answer_prompt_overrides(self) -> dict[str, Any]:
            return {}

    def parser(system: str, inputs: str, settings: Any, schema: Any, **options: Any) -> Any:
        assert schema is ClaimAuditOutput
        payload = json.loads(inputs)
        assert payload["question"] == "有効期限は何日ですか。"
        evidence = next(item for item in payload["evidence_items"] if item["id"] == "chunk-a")
        assert evidence["text"] == source
        passages = payload["answer_passages"]
        assert len(passages) == 1
        parsed.append({"mode": settings.answer_audit_mode, "schema": schema.__name__})
        # provider の応答の代わりに、同じ呼び出しで固定の利用量も返したことにする。
        record_llm_usage({"input_tokens": 10, "output_tokens": 5})
        return schema.model_validate(
            {
                "claim_checks": [
                    {
                        "answer_quote": passages[0]["text"],
                        "answer_passage_id": passages[0]["id"],
                        "evidence_id": evidence["evidence_id"],
                        "evidence_quote": source if verdict == "supported" else "",
                        "claim_target": "有効期限",
                        "evidence_target": "有効期限",
                        "applicability": "matched",
                        "status": verdict,
                        "source_id": "",
                        "reason": "合成の照合結果",
                    }
                ]
            }
        )

    monkeypatch.setattr(answer_eval, "parse_text_response", parser)
    settings = Settings(rag_answer_audit_mode=mode)
    engine = AnswerEngine(settings=settings, oracle=cast(Any, Oracle()), genai=cast(Any, object()))
    answer = source + "\n根拠：synthetic.pdf p.1"
    outcome = AnswerOutcome(
        answer=answer,
        citations=[citation],
        diagnostics={"outcome": "answered", "envelope": {"outcome": "answered"}},
        context_text=source,
    )
    # validate_answer、validate_answer_claims とスレッド実行は置換しない。
    await engine._validate_outcome(SearchRequest(query="有効期限は何日ですか。"), outcome)
    validation = outcome.diagnostics["answer_validation"]
    assert outcome.answer == answer
    if mode == "none":
        assert loaded == []
        assert parsed == []
        assert validation == {"status": "not_run", "valid": False, "reason": "audit_disabled"}
        return
    assert loaded == [("document-a", "chunk-a")]
    assert parsed == [{"mode": mode, "schema": "ClaimAuditOutput"}]
    assert validation["status"] == "completed"
    assert validation["valid"] is (verdict == "supported")
    assert validation["counts"][verdict] == 1
    assert validation["claim_checks"][0]["status"] == verdict
    assert outcome.diagnostics["execution_steps"][0]["llm_calls"] == 1
    if verdict == "unsupported":
        assert outcome.diagnostics["confidence"] == "low"
        assert outcome.diagnostics["needs_human_review"] is True
        assert outcome.diagnostics["outcome"] == "conditional"
