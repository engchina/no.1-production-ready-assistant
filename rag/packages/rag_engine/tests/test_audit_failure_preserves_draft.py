"""監査の実行失敗と材料不足を区別し、初回の有用な本文を保つ。"""

import asyncio
from concurrent.futures import CancelledError
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError
from test_grounded_answer import FakeModel, context, draft, ids, item, record

from rag_engine.generation import grounded
from rag_engine.generation.answering import _grounded_spans
from rag_engine.resources.deadline import Deadline, DeadlineExceeded, bind_deadline

QUESTION = "対象Aの画面を開く方法は？"
TEXT = "設定メニューから対象Aの画面を開きます。"


def run_failed_audit(monkeypatch, error, *, deadline=None):
    ctx = context(record(0, TEXT, "対象A"))
    evidence_id = ids(ctx, QUESTION)[TEXT]
    model = FakeModel([draft(item(evidence_id, TEXT, TEXT), summary=TEXT)], [])
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        if deadline is not None:
            deadline()
        raise error

    monkeypatch.setattr(grounded, "_parse_audit", fail)
    current = grounded.run_round(
        QUESTION,
        _grounded_spans(QUESTION, ctx, (), None),
        None,
        prompt="p",
        image_paths=[],
        provider_id=None,
        parse_text=model,
        parse_images=None,
    )
    return current, model, calls


@pytest.mark.parametrize("review", [None, SimpleNamespace(answer_state="unknown")])
def test_unassessed_requirement_is_unknown_not_missing(review):
    assert grounded.requirement_status(review, {"kind": "completion"}, []) == "unknown"


def test_unknown_requirement_cannot_complete_a_guide_step():
    steps = grounded.apply_guide_contracts(
        [{"step_id": "s1", "status": grounded.GUIDE_STEP_EXPLAINED, "required": True}],
        [],
        [{"step_id": "s1", "requirements": [{"id": "r1", "text": "対象Aを確認", "kind": "completion"}]}],
        None,
    )
    assert steps[0]["requirements"][0]["status"] == "unknown"
    assert steps[0]["status"] != grounded.GUIDE_STEP_EXPLAINED


@pytest.mark.parametrize("error", [ValueError("invalid structured output"), RuntimeError("provider failed")])
def test_initial_audit_error_preserves_body_without_marking_support(monkeypatch, error):
    current, model, calls = run_failed_audit(monkeypatch, error)
    assert current.audit is None and current.review_execution == "error"
    assert current.timing["audit_error_type"] == type(error).__name__
    assert current.audit_calls == len(calls) == 1
    assert len(model.prompts) == 1
    entry = next(e for e in current.checked if e.item.text == TEXT)
    assert entry.published and not entry.removed
    # このfixtureは旧自由引用で、整数範囲のproofは持たない。原文一致は独立に確認する。
    assert entry.quote_match == "exact" and entry.item.quote == TEXT
    assert entry.span is not None and TEXT in entry.span["text"]
    assert not any(m["code"] in {"unsupported", "contradicted"} for m in entry.marks)
    dimensions = current.trace()["validation_dimensions"]
    assert all(row["status"] == "unknown" for row in dimensions["claim_support"]["items"])
    assert not current.addressed


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("audit timed out"),
        httpx.ReadTimeout("audit timed out"),
        APITimeoutError(request=httpx.Request("POST", "https://example.invalid")),
    ],
)
def test_actual_audit_timeout_is_timed_out_not_completed(monkeypatch, error):
    current, _, calls = run_failed_audit(monkeypatch, error)
    assert current.review_execution == "timed_out" and current.audit is None
    assert current.audit_calls == len(calls) == 1
    assert any(e.item.text == TEXT and e.published for e in current.checked)
    assert current.verified == 0
    assert all(row["status"] == "unknown" for row in current.trace()["validation_dimensions"]["claim_support"]["items"])


@pytest.mark.parametrize("error", [PermissionError("denied"), CancelledError(), asyncio.CancelledError(), DeadlineExceeded("outer")])
def test_permissions_cancellation_and_outer_deadline_propagate(monkeypatch, error):
    with pytest.raises(type(error)):
        run_failed_audit(monkeypatch, error)


@pytest.mark.parametrize("status", [401, 403])
def test_http_permission_denial_propagates(monkeypatch, status):
    error = httpx.HTTPStatusError("denied", request=httpx.Request("POST", "https://example.invalid"), response=httpx.Response(status))
    with pytest.raises(httpx.HTTPStatusError):
        run_failed_audit(monkeypatch, error)


def test_expired_outer_deadline_is_not_swallowed_with_a_provider_error(monkeypatch):
    outer = Deadline(100)
    with bind_deadline(outer), pytest.raises(RuntimeError):
        run_failed_audit(monkeypatch, RuntimeError("provider failed"), deadline=outer.cancel)


def test_child_deadline_timeout_retains_body(monkeypatch):
    outer = Deadline(100)
    child = Deadline(100, parent=outer)
    with bind_deadline(child):
        current, _, _ = run_failed_audit(monkeypatch, DeadlineExceeded("child"), deadline=child.expire)
    assert current.review_execution == "timed_out"
    assert any(e.item.text == TEXT and e.published for e in current.checked)


def test_generation_failure_is_outside_audit_fallback(monkeypatch):
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        raise RuntimeError("generation failed")

    with pytest.raises(RuntimeError, match="generation failed"):
        grounded.run_round(QUESTION, [], None, prompt="p", image_paths=[], provider_id=None, parse_text=fail, parse_images=None)
    assert calls == [True]


def test_failed_audit_exposes_unknown_and_blocks_same_generated_gap(monkeypatch):
    from rag_engine.generation.request_recovery import request_recovery_plan

    current, _, _ = run_failed_audit(monkeypatch, ValueError("invalid structured output"))
    assert current.unanswered
    for row in current.unanswered:
        assert row["status"] == "unknown" and row["reviewed"] is False
        assert row["review_execution"] == "error"
    decisions = request_recovery_plan(current.unanswered, [row["request_id"] for row in current.unanswered], set())
    assert decisions and all(row["action"] == "retain" and row["status"] == "unknown" for row in decisions)


@pytest.mark.parametrize("execution", ["not_run", "error", "timed_out", "unknown"])
def test_unknown_review_never_becomes_material_missing_with_a_generated_gap(execution):
    from rag_engine.generation.request_recovery import request_recovery_plan

    row = {"request_id": "Q1", "status": "unknown", "reviewed": False, "review_execution": execution}
    [decision] = request_recovery_plan([row], ["Q1"], set())
    assert decision["action"] == "retain" and decision["cause"] == "review_incomplete"


def test_normal_audit_none_without_a_verdict_still_allows_an_explicit_generated_gap():
    from rag_engine.generation.request_recovery import request_recovery_plan

    [decision] = request_recovery_plan([], ["Q1"], set())
    assert decision["action"] == "retrieval" and decision["cause"] == "evidence_missing"


@pytest.mark.parametrize(
    ("states", "order_errors", "expected"),
    [
        (["unknown"], [], False),
        (["addressed", "unknown"], [], False),
        (["missing"], [], False),
        (["partial", "unknown"], [], False),
        (["unknown"], ["s0"], False),
        ([], [], False),
    ],
)
def test_failed_untrusted_guide_observations_never_trigger_semantic_correction(monkeypatch, states, order_errors, expected):
    current, _, _ = run_failed_audit(monkeypatch, ValueError("audit failed"))
    current.guide_steps = [
        {
            "step_id": "s1",
            "title": "対象A",
            "required": True,
            "status": grounded.GUIDE_STEP_UNUSED,
            "evidence_refs": [{"evidence_id": "E1"}],
            "requirements": [{"id": f"r{i}", "text": "操作の確認", "status": state} for i, state in enumerate(states)],
            "order_errors": order_errors,
        }
    ]
    assert bool(current.feedback().get("guide_steps_unexplained")) is expected
