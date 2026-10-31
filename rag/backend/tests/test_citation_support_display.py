"""定位のみの引用を意味支持の確認済みとして表示しない。"""

from typing import Any

import pytest

from app.rag.answer_engine import _outcome_from_result, _SearchState
from app.rag.citation_status import citation_support_display
from tests.test_answer_use import _chunk, _EngineResult


def trace(execution: str, statuses: list[str], *, diagnostic: bool = False) -> dict[str, Any]:
    value: dict[str, Any] = {
        "rounds": [
            {
                "accepted": True,
                "review_execution": execution,
                "validation_dimensions": {
                    "claim_support": {
                        "execution": execution,
                        "items": [{"status": status} for status in statuses],
                    }
                },
            }
        ]
    }
    if diagnostic:
        value["semantic_audit_policy"] = {
            "mode": "diagnostic_only",
            "calibration": "not_calibrated",
        }
    return value


@pytest.mark.parametrize(
    "execution,statuses,diagnostic",
    [
        ("not_run", ["unknown"], False),
        ("completed", ["unknown"], False),
        ("partial", ["supported"], False),
        ("error", ["supported"], False),
        ("completed", ["supported", "unknown"], False),
        ("completed", ["supported"], True),
    ],
)
def test_unverified_support_preserves_answer_and_citation_use(
    execution: str, statuses: list[str], diagnostic: bool
) -> None:
    state = _SearchState()
    state.chunks = {"c": _chunk("c")}
    result = _EngineResult([{"chunk_id": "c", "is_model_used": True, "answer_use": "confirmed"}])
    result.generation_trace = trace(execution, statuses, diagnostic=diagnostic)
    outcome = _outcome_from_result(result, state)
    assert outcome.answer == "回答"
    metadata = outcome.citations[0].metadata
    assert metadata["evidence_model_used"] is True
    assert metadata["evidence_answer_use"] == "confirmed"  # 元の使用記録は書き換えない。
    observation = metadata["evidence_claim_support"]
    assert isinstance(observation, dict)
    assert observation["status"] == "unknown"
    assert observation["execution"] == execution


def test_completed_support_is_distinct_from_no_record() -> None:
    assert citation_support_display({}) is None
    assert citation_support_display({"rounds": [{"accepted": True}]}) is None
    assert citation_support_display(trace("completed", ["supported"])) == {
        "scope": "answer",
        "execution": "completed",
        "status": "supported",
        "policy": "recorded",
    }
    empty = citation_support_display(trace("completed", []))
    assert empty is not None and empty["status"] == "unknown"


def test_support_display_survives_stored_answer_snapshot() -> None:
    from app.rag.stored_answer import stored_citation_metadata

    observation = {
        "scope": "answer",
        "execution": "not_run",
        "status": "unknown",
        "policy": "recorded",
    }
    assert (
        stored_citation_metadata({"evidence_claim_support": observation})["evidence_claim_support"]
        == observation
    )


@pytest.mark.parametrize(
    "claim",
    [
        "条件が未入力の場合は入力必須エラーとなり、処理を実行しません。",
        "条件が未入力の場合も処理を実行し、結果を上限件数まで表示します。",
    ],
)
def test_actual_none_generation_keeps_opposite_claims_unverified_for_display(claim: str) -> None:
    from rag_engine.config import get_settings
    from rag_engine.generation import grounded

    spans = [
        {
            "evidence_id": "source-a",
            "label": "E1",
            "source_id": "p-a",
            "source": "synthetic.pdf",
            "function": ("synthetic.pdf", "合成操作"),
            "text": "条件が未入力の場合も処理を実行し、結果を上限件数まで表示します。",
        }
    ]
    calls = []

    def generate(*args: Any, **kwargs: Any) -> Any:
        calls.append(args[3])
        return args[3].model_validate(
            {
                "items": [
                    {
                        "kind": "operation",
                        "text": claim,
                        "citations": [{"evidence_id": "E1", "line_start": 1, "line_end": 1}],
                    }
                ],
                "summary": "",
                "confidence": "low",
            }
        )

    actual = grounded.run_round(
        "操作方法は？",
        spans,
        get_settings(environ={}, dotenv_path=None),
        prompt=grounded.format_functions(spans),
        image_paths=(),
        provider_id=None,
        parse_text=generate,
        parse_images=lambda *args, **kwargs: pytest.fail("画像の呼出しは不要"),
        audit_mode="none",
    )
    entry = next(row for row in actual.checked if row.item.text == claim)
    assert len(calls) == 1 and actual.review_execution == "not_run"
    assert entry.quote_match == "exact" and entry.citation_resolution["authenticity"] == "verified"
    assert not entry.marks
    assert entry.review_assessment == {"effective_support": "unknown"}
    assert actual.audit_calls == 0
    assert "semantic_audit_observation" not in actual.timing
    assert actual.audit is not None and actual.audit.reviews == []
    result = _EngineResult(
        [{"chunk_id": "c", "is_model_used": True, "answer_use": grounded.answer_use(entry)}]
    )
    result.answer_text = grounded.render_answer("", actual.checked).text
    result.generation_trace = {"rounds": [{**actual.trace(), "accepted": True}]}
    state = _SearchState()
    state.chunks = {"c": _chunk("c")}
    outcome = _outcome_from_result(result, state)
    assert claim in outcome.answer
    observation = outcome.citations[0].metadata["evidence_claim_support"]
    assert (
        isinstance(observation, dict)
        and observation["status"] == "unknown"
        and observation["execution"] == "not_run"
    )
