"""出力の検査の実施・省略・検査不能を progress / chat / trace で区別する。"""

import asyncio
from typing import Any, cast

import pytest
from pr_backend_core.chat_progress import fold_chat_progress_events
from pr_backend_core.config.tracing import TracingSettings
from pr_backend_core.observability.tracing import (
    configure_tracing,
    operation_trace,
    shutdown_tracing,
)

from app.clients.oci_guardrails import GuardrailInspection
from app.config import Settings
from app.rag.guardrails import GuardrailPolicy
from app.rag.pipeline import SearchStageProgress, _observe_stage, guardrail_stage_outcome
from app.rag.step_details import answer_guardrail_details
from tests.span_progress_support import RecordingTrace

pytestmark = pytest.mark.usefixtures("trace_body_enabled")


@pytest.mark.parametrize(
    "enabled,inspection,expected_outcome,expected_status,expected_verdict",
    [
        (False, GuardrailInspection(), "skipped", "skipped", "無効のため省略"),
        (True, GuardrailInspection(), "success", "done", "問題なし"),
        (True, GuardrailInspection(prompt_injection=True), "success", "done", "回答を止めました"),
        (True, None, "error", "failed", "安全検査を完了できず、回答を止めました"),
    ],
    ids=["disabled", "clean", "blocked", "unavailable"],
)
async def test_inspection_outcome_reaches_progress_chat_and_trace(
    enabled: bool,
    inspection: GuardrailInspection | None,
    expected_outcome: str,
    expected_status: str,
    expected_verdict: str,
) -> None:
    calls: list[str] = []

    class Client:
        def inspect_text(self, text: str) -> GuardrailInspection | None:
            calls.append(text)
            return inspection

    policy = GuardrailPolicy(
        Settings(rag_guardrails_input_enabled=False, rag_guardrails_output_enabled=enabled),
        Client(),
    )
    events: list[SearchStageProgress] = []
    tracker = RecordingTrace(model_id="synthetic")

    async def capture(event: SearchStageProgress) -> None:
        events.append(event)

    configure_tracing(TracingSettings(), service_name="synthetic")
    try:
        with operation_trace(
            "task", operation_id="synthetic", listener=tracker.listener()
        ) as summary:
            result = await _observe_stage(
                "synthetic",
                "answer_guardrail",
                asyncio.to_thread(policy.validate_answer, "合成の回答"),
                progress_callback=capture,
                result_outcome=guardrail_stage_outcome,
                result_details=lambda value: answer_guardrail_details(value, "合成の回答"),
            )
        assert calls == (["合成の回答"] if enabled else [])
        assert [event.outcome for event in events] == ["started", expected_outcome]
        # 処理の経過は span（#3226）。検査の段の span に、種類と検査の結果の詳細を付ける。
        rows = fold_chat_progress_events(tracker.events).step_list()
        step = next(row for row in rows if row.kind == "answer_guardrail")
        assert step.status == expected_status
        assert step.details is not None
        fields = [(field.label, field.value) for field in step.details.fields]
        assert fields[0] == ("結果", expected_verdict)
        if expected_outcome == "error":
            assert fields[1] == ("外部の検査", "OCI Guardrails を利用できませんでした")
        assert (
            next(
                span for span in summary.spans if span.operation_name == "rag.answer_guardrail"
            ).status
            == expected_status
        )
        assert result.allowed == (
            not enabled or inspection is not None and not inspection.prompt_injection
        )
    finally:
        shutdown_tracing()


async def test_cancelled_inspection_does_not_report_clean_or_skipped() -> None:
    events: list[SearchStageProgress] = []

    async def capture(event: SearchStageProgress) -> None:
        events.append(event)

    async def cancelled() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _observe_stage(
            "synthetic", "answer_guardrail", cancelled(), progress_callback=capture
        )
    assert [event.outcome for event in events] == ["started", "cancelled"]


@pytest.mark.parametrize(
    "final_text,allowed,changed",
    [
        ("合成の回答", True, False),
        ("[機微情報]", True, True),
        ("安全チェックで回答を停止しました。", False, True),
    ],
)
async def test_saved_validation_and_evaluation_follow_the_final_body(
    monkeypatch: pytest.MonkeyPatch,
    final_text: str,
    allowed: bool,
    changed: bool,
) -> None:
    from app.rag.answer_engine import AnswerEngine, AnswerOutcome
    from app.rag.guardrails import GuardrailResult
    from app.rag.pipeline import RagPipeline
    from app.schemas.search import SearchRequest
    from tests.test_answer_engine import FakeGenAi, SavingOracle

    async def engine_run(
        self: AnswerEngine, request: SearchRequest, **kwargs: Any
    ) -> AnswerOutcome:
        return AnswerOutcome(
            answer="合成の回答",
            citations=[],
            context_text="合成の資料",
            diagnostics={
                "outcome": "answered",
                "answer_validation": {
                    "status": "completed",
                    "valid": True,
                    "reuse_context": "version-a",
                },
            },
            evaluation_input={"answer_text": "合成の回答", "evidence_items": []},
        )

    monkeypatch.setattr(AnswerEngine, "run", engine_run)
    monkeypatch.setattr(
        GuardrailPolicy,
        "validate_answer",
        lambda *args, **kwargs: GuardrailResult(
            allowed=allowed,
            sanitized_text=final_text,
            findings=[],
        ),
    )
    oracle = SavingOracle()
    response = await RagPipeline(
        settings=Settings(rag_guardrails_input_enabled=False),
        oracle=cast(Any, oracle),
        genai=cast(Any, FakeGenAi()),
    ).run(SearchRequest(query="合成の質問", knowledge_base_ids=["kb-1"]))
    assert response.answer == final_text == oracle.saved[-1]["answer"]
    assert response.diagnostics.answer is not None
    validation = response.diagnostics.answer["answer_validation"]
    assert isinstance(validation, dict)
    assert validation["valid"] is (not changed)
    assert validation["status"] == ("body_changed" if changed else "completed")
    assert validation["reuse_context"] == ("" if changed else "version-a")
    if changed:
        assert "claim_checks" not in validation and "counts" not in validation
        assert isinstance(validation["previous_body_validation"], dict)
    assert oracle.saved[-1]["evaluation_input"]["answer_text"] == final_text
