"""補写する操作の材料 ID と、引用済みの主張の支持を混同しない。意味判定はスタブ。"""

import json

import pytest
from test_grounded_answer import FakeModel, audit, context, draft, ids, item, record

from rag_engine.config import get_settings
from rag_engine.generation import grounded
from rag_engine.generation.answering import _grounded_spans
from rag_engine.generation.request_recovery import request_recovery_plan

QUESTION = "対象Aの設定を保存する手順は？"
OPEN = "対象Aの設定画面を開きます。"
RELATED = "対象Aの設定画面には状態と履歴が表示されます。"
SAVE = "対象Aの設定画面で保存ボタンを押します。"


@pytest.mark.parametrize("cited,claim,support", [(OPEN, OPEN, "supported"), (RELATED, SAVE, "unsupported")])
def test_uncited_direct_material_uses_a_stable_id_without_backfilling_claim_support(cited, claim, support):
    ctx = context(record(0, cited, "対象Aの設定"), record(1, SAVE, "対象Aの設定"))
    refs = ids(ctx, QUESTION)
    seen = {}

    def review(prompt):
        payload = json.loads(prompt)
        material = next(row for row in payload["unused_evidence"] if row["text"] == SAVE)
        seen["input"] = payload
        result = audit((0, support, "matched", ""), requests=[("Q1", "partial")], summary_supported=False)
        result["request_reviews"][0].update(
            evidence_status="available",
            evidence_ids=[material["evidence_id"]],
            missing_content=SAVE,
            evidence_support=[{"evidence_id": material["evidence_id"], "source": material["source"], "quote": SAVE}],
        )
        return result

    model = FakeModel([draft(item(refs[cited], claim, cited, request_id="Q1"), summary="")], [review])
    spans = _grounded_spans(QUESTION, ctx, (), None)
    result = grounded.run_round(
        QUESTION,
        spans,
        get_settings(environ={}, dotenv_path=None),
        prompt="合成の回答と材料",
        image_paths=(),
        provider_id=None,
        parse_text=model,
        parse_images=lambda *args, **kwargs: pytest.fail("画像は不要"),
    )
    material = seen["input"]["unused_evidence"][0]
    assert material["evidence_id"] == refs[SAVE]
    assert material["evidence_id"] in {row["evidence_id"] for row in spans}
    assert material["evidence_id"] not in {row["label"] for row in spans}
    plan = request_recovery_plan(result.unanswered, (), {row["evidence_id"] for row in spans})
    assert plan[0]["action"] == "retain" and plan[0]["evidence_ids"] == []
    observation = result.timing["semantic_audit_observation"]
    assert observation["adopted"] is False
    raw = observation["audit"]["request_reviews"][0]
    assert raw["evidence_status"] == "available" and raw["evidence_ids"] == [refs[SAVE]]
    assert raw["evidence_support"] == [{"evidence_id": refs[SAVE], "source": material["source"], "quote": SAVE}]
    assert result.checked[0].review_assessment["observed_support"] == support
    assert result.checked[0].review_assessment["effective_support"] == "unknown"
    assert seen["input"]["items"][0]["citations"] == [{"evidence_id": refs[cited], "quote": cited}]


def test_display_label_changes_do_not_change_the_material_identity():
    span = {"evidence_id": "Estable", "label": "E1", "text": SAVE, "source": "sample.pdf", "function": ("設定",)}
    first = grounded._unused_evidence(QUESTION, [span], set())
    second = grounded._unused_evidence(QUESTION, [{**span, "label": "E9"}], set())
    assert first == second
    assert first[0]["evidence_id"] == "Estable"


@pytest.mark.parametrize("conflicting", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_request_reviews_are_unknown_without_last_wins_or_extra_calls(conflicting, reverse):
    ctx = context(record(0, OPEN, "対象Aの設定"))
    refs = ids(ctx, QUESTION)
    assessment = audit((0, "supported", "matched", ""), requests=[("Q1", "addressed")])
    first = assessment["request_reviews"][0]
    first.update(evidence_status="available", evidence_ids=[refs[OPEN]])
    second = {**first, "status": "missing", "evidence_status": "missing", "evidence_ids": []} if conflicting else dict(first)
    assessment["request_reviews"] = [second, first] if reverse else [first, second]
    model = FakeModel([draft(item(refs[OPEN], OPEN, OPEN, request_id="Q1"), summary="")], [assessment])
    spans = _grounded_spans(QUESTION, ctx, (), None)
    result = grounded.run_round(
        QUESTION,
        spans,
        get_settings(environ={}, dotenv_path=None),
        prompt="合成の要求監査",
        image_paths=(),
        provider_id=None,
        parse_text=model,
        parse_images=lambda *args, **kwargs: pytest.fail("画像は不要"),
    )
    assert len(model.prompts) == 2 and result.audit_calls == 1
    assert result.addressed == set()
    [unverified] = result.unanswered
    assert unverified["request_id"] == "Q1" and unverified["status"] == "unknown"
    assert unverified["reviewed"] is False
    assert [row["action"] for row in request_recovery_plan(result.unanswered, (), {row["evidence_id"] for row in spans})] == ["retain"]
    dimension = result.trace()["validation_dimensions"]["answer_completeness"]
    assert dimension["execution"] == "partial"
    assert dimension["duplicate_request_ids"] == ["Q1"]
    [request] = dimension["requests"]
    assert request["reviewed"] is False
    assert request["review_execution"] == "error"
    assert request["evidence_status"] == "unknown" and request["evidence_ids"] == []
    assert request["status"] == "unknown"
