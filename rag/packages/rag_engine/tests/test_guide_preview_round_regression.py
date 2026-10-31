"""共有根拠・検索打切り・二組の生成監査の合成再生（#2733）。"""

from dataclasses import replace

import pytest
from test_grounded_answer import FakeModel, audit, draft, item
from test_guide_step_fulfillment import SHARED, SHARED_END, SHARED_GUIDE, SHARED_TERMS, _context, _ids, _knowledge, _record

from rag_engine.config import get_settings
from rag_engine.dependencies import AnswerDependencies, bind_dependencies
from rag_engine.generation.answering import synthesize_grounded_answer


@pytest.mark.parametrize("with_plan", [False, True], ids=["base-plan", "resolved-plan"])
@pytest.mark.parametrize("shared_second", [True, False], ids=["shared-source", "unsearched-no-source"])
@pytest.mark.parametrize("applicability", ["matched", "conditional", "unverified"])
def test_shared_source_with_unsearched_step_survives_two_draft_audit_rounds(shared_second, applicability, with_plan):
    source = _record(0, SHARED, "設定画面", "run:s")
    if with_plan:
        source.metadata["image_evidence"] = [{"visual_structure": {"form_fields": [{"name": "設定 A", "kind": "select"}]}}]
    ctx = _context(source)
    guide = SHARED_GUIDE if shared_second else (SHARED_GUIDE[0], ("s2", "反映ボタンで反映する", ()))
    knowledge = replace(_knowledge(guide, terms=SHARED_TERMS), guide_unsearched_steps=("s2",))
    eid = next(iter(_ids(ctx, knowledge).values()))
    common = item(eid, SHARED_END, SHARED_END, request_id="Q1")
    invalid = item(eid, "合成の追加説明。", "原文に存在しない合成の引用。", request_id="Q1")
    model = FakeModel(
        [draft(common, invalid), draft(common)], [audit((0, "supported", applicability, ""), requests=[("Q1", "addressed")])] * 3
    )

    def unused(*args, **kwargs):
        raise AssertionError("外部 I/O は不要")

    settings = replace(get_settings(environ={}, dotenv_path=None), answer_audit_mode="detailed")
    if with_plan:
        settings = replace(
            settings,
            evidence_conditions=(("kind", "設定 A の種類", (("選択", ()), ("自由入力", ()))),),
            evidence_plans=(((("kind", "選択"),), (("s1", True), ("s2", False)), "guide-a", ""),),
        )
    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        result = synthesize_grounded_answer(
            "設定 A の手順を教えてください", ctx, settings, image_prompt_mode="text_only", runtime_knowledge=knowledge
        ).response
    counts = {name: sum(kind == name for kind, _ in model.prompts) for name in ("GroundedDraftWire", "GroundedAudit")}
    assert counts == {"GroundedDraftWire": 2, "GroundedAudit": 2}
    assert result.answer_text
    coverage = {step["step_id"]: step["status"] for step in result.generation_trace["guide_step_coverage"]}
    assert coverage["s1"] != "explained"
    if not shared_second:
        assert coverage["s2"] == "not_searched"
    steps = {step["step_id"]: step for step in result.generation_trace["guide_step_coverage"]}
    assert steps["s2"]["required"] is (not with_plan)


def test_wrapped_execution_error_retains_the_original_frame_in_safe_diagnostics():
    """これは元の500の再現ではなく、根因の位置がログから消える部分の再現。"""
    from pr_backend_core.logging import safe_exception_fields

    from rag_engine.generation.execution_record import AnswerExecutionError, _execution_step, _record_answer_execution

    def fail_with_attribute_error():
        raise AttributeError("QUESTION_SENTINEL DOCUMENT_SENTINEL TOKEN_SENTINEL")

    @_record_answer_execution
    def synthetic_execution(question):
        with _execution_step("合成の工程"):
            fail_with_attribute_error()

    with pytest.raises(AnswerExecutionError) as raised:
        synthetic_execution("QUESTION_SENTINEL")
    fields = safe_exception_fields(raised.value)
    assert fields["exception_type"] == "AnswerExecutionError"
    assert fields["exception_causes"] == ["AttributeError"]
    causes = fields.get("exception_cause_details", [])
    assert causes and causes[0]["exception_type"] == "AttributeError"
    assert causes[0]["exception_frames"][-1]["function"] == "fail_with_attribute_error"
    assert "SENTINEL" not in str(fields)
