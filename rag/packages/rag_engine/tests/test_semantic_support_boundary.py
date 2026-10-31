"""主張の意味支持と引用照合の境界。スタブは判定の伝達だけを検証し、判定器の精度を証明しない。"""

import json
from dataclasses import replace

import pytest
from test_grounded_answer import FakeModel, audit, context, draft, ids, item, record

from rag_engine.config import get_settings
from rag_engine.generation import grounded
from rag_engine.generation.answering import _grounded_spans

QUESTION = "対象Aの設定を保存する手順は？"
RELATED = "対象Aの設定画面には、状態と履歴が表示されます。"
OPERATION = "対象Aの設定画面で保存ボタンを押します。"
CONDITION = "対象Aの保存操作は、承認済みの場合だけ実行します。"
CLAIM = "承認済みの場合だけ、対象Aの設定画面で保存ボタンを押します。"


def run_round(ctx, proposal, assessment, *, mode="detailed"):
    config = replace(get_settings(environ={}, dotenv_path=None), answer_audit_mode=mode)
    model = FakeModel([proposal], [assessment])
    result = grounded.run_round(
        QUESTION,
        _grounded_spans(QUESTION, ctx, (), None),
        config,
        prompt="合成の質問と根拠から回答してください。",
        image_paths=(),
        provider_id=None,
        parse_text=model,
        parse_images=lambda *args, **kwargs: pytest.fail("画像呼び出しは不要"),
        audit_mode=mode,
    )
    inputs = [json.loads(prompt) for schema, prompt in model.prompts if schema in {"GroundedAudit", "GroundedFactAudit"}]
    return result, inputs


def test_related_source_and_exact_quote_do_not_override_unsupported_claim():
    ctx = context(record(0, RELATED, "対象Aの設定"))
    eid = ids(ctx, QUESTION)[RELATED]
    result, inputs = run_round(
        ctx,
        draft(item(eid, OPERATION, RELATED, request_id="Q1")),
        audit((0, "unsupported", "matched", ""), summary_supported=False),
    )
    entry = next(row for row in result.checked if row.item.text == OPERATION)
    assert entry.span and entry.quote_match == "exact"
    assert entry.review_assessment["observed_support"] == "unsupported"
    assert entry.review_assessment["effective_support"] == "unknown"
    assert entry.published and result.verified == 0
    assert inputs[0]["items"][0]["quote"] == RELATED
    dimensions = result.trace()["validation_dimensions"]
    assert dimensions["citation_validity"]["items"][0]["citations"][0]["status"] == "matched"
    assert dimensions["claim_support"]["items"][0]["status"] == "unknown"


@pytest.mark.parametrize("support", ["supported", "unsupported"])
def test_record_only_retains_body_but_records_its_actual_applicability_effect(support):
    ctx = context(record(0, OPERATION, "対象Aの設定"))
    eid = ids(ctx, QUESTION)[OPERATION]
    result, _ = run_round(
        ctx,
        draft(item(eid, OPERATION, OPERATION, request_id="Q1"), summary=""),
        audit((0, support, "not_applicable", ""), requests=[("Q1", "partial")], summary_supported=False),
    )
    dimensions = result.trace()["validation_dimensions"]
    assert dimensions["citation_validity"]["items"][0]["citations"][0]["status"] == "matched"
    assert dimensions["claim_support"]["items"][0]["status"] == "unknown"
    assert result.timing["semantic_audit_observation"]["audit"]["reviews"][0]["support"] == support
    application = dimensions["business_applicability"]["items"][0]
    assert application["pre_audit_status"] == "matched"
    assert application["observed_status"] == "not_applicable"
    assert application["status"] == "matched"
    assert application["action"] == "record_only" and application["policy_effect"] == "retain_current_state"
    assert application["confirmation_source"] == "generation_and_basic_checks"
    retained = dimensions["body_retention"]["items"][0]
    assert retained["included"] and not retained["removed"] and retained["confirmed"]
    assert retained["confirmation_scope"] == "citation_and_basic_checks"
    assert dimensions["body_retention"]["stage"] == "generation_round"
    assert dimensions["answer_completeness"]["requests"][0]["status"] == "unknown"


def test_generated_operation_without_a_matching_source_is_not_confirmed():
    ctx = context(record(0, RELATED, "対象Aの設定"))
    proposal = draft(item("unknown", OPERATION, OPERATION, request_id="Q1"))
    result, _ = run_round(ctx, proposal, audit(requests=[("Q1", "missing")], summary_supported=False))
    assert result.verified == 0
    assert all(not row.confirmed for row in result.checked if row.item.text == OPERATION)


def test_multi_citation_payload_preserves_operation_and_condition_for_joint_review():
    ctx = context(record(0, OPERATION, "対象Aの設定"), record(1, CONDITION, "対象Aの設定"))
    refs = ids(ctx, QUESTION)
    proposal = draft(
        {
            "kind": "operation",
            "request_id": "Q1",
            "text": CLAIM,
            "citations": [{"evidence_id": refs[OPERATION], "quote": OPERATION}, {"evidence_id": refs[CONDITION], "quote": CONDITION}],
        }
    )
    result, inputs = run_round(ctx, proposal, audit((0, "supported", "matched", ""), (1, "supported", "matched", "")))
    # 共同支持の判定自体はスタブ。引用の操作と条件が失われないことだけを確認する。
    source_ids = set()
    quotes = set()
    for row in inputs[0]["items"]:
        if row["text"] != CLAIM:
            continue
        assert {(citation["evidence_id"], citation["quote"]) for citation in row["citations"]} == {
            (refs[OPERATION], OPERATION),
            (refs[CONDITION], CONDITION),
        }
        for citation in row["citations"]:
            source_ids.add(citation["evidence_id"])
            quotes.add(citation["quote"])
    assert source_ids == {refs[OPERATION], refs[CONDITION]}
    assert quotes == {OPERATION, CONDITION}
    assert any(row.item.text == CLAIM for row in result.checked)


@pytest.mark.parametrize("execution", ["not_run", "error", "timed_out"])
def test_incomplete_check_has_no_semantic_support_verdict(execution, monkeypatch):
    ctx = context(record(0, OPERATION, "対象Aの設定"))
    eid = ids(ctx, QUESTION)[OPERATION]
    if execution == "error":
        # 空の応答を再試行しても判定が無い場合。未実行と同じ意味支持には昇格させない。
        assessment = audit(summary_supported=False)
        result, _ = run_round(ctx, draft(item(eid, OPERATION, OPERATION)), assessment)
    elif execution == "timed_out":
        model = FakeModel([draft(item(eid, OPERATION, OPERATION))], [])
        original = model.__call__

        def parse(system, prompt, config, schema, **options):
            if schema.__name__ != "GroundedDraftWire":
                raise TimeoutError("合成の監査期限")
            return original(system, prompt, config, schema, **options)

        monkeypatch.setattr(grounded, "deadline_cut_short", lambda: True)
        result = grounded.run_round(
            QUESTION,
            _grounded_spans(QUESTION, ctx, (), None),
            get_settings(environ={}, dotenv_path=None),
            prompt="合成の回答",
            image_paths=(),
            provider_id=None,
            parse_text=parse,
            parse_images=lambda *args, **kwargs: pytest.fail("画像呼び出しは不要"),
        )
    else:
        result, _ = run_round(ctx, draft(item(eid, OPERATION, OPERATION)), {}, mode="none")
    trace = result.trace()
    assert trace["review_execution"] != "completed"
    assert all(not row.review_assessment.get("observed_support") for row in result.checked)
    assert all(row.get("review_assessment", {}).get("effective_support") in {None, "unknown"} for row in trace["items"])
    dimensions = trace["validation_dimensions"]
    assert dimensions["citation_validity"]["execution"] == "completed"
    assert dimensions["claim_support"]["execution"] == execution
    assert all(row["status"] == "unknown" for row in dimensions["claim_support"]["items"])
    assert dimensions["answer_completeness"]["execution"] == execution
    assert dimensions["answer_completeness"]["requests"] == []


def test_irrelevant_citation_observation_does_not_remove_joint_citation_sources():
    ctx = context(record(0, OPERATION, "対象Aの設定"), record(1, CONDITION, "対象Aの設定"), record(2, RELATED, "対象Aの設定"))
    refs = ids(ctx, QUESTION)
    proposal = draft(
        {
            "kind": "operation",
            "request_id": "Q1",
            "text": CLAIM,
            "citations": [{"evidence_id": refs[text], "quote": text} for text in (OPERATION, CONDITION, RELATED)],
        }
    )
    result, inputs = run_round(
        ctx,
        proposal,
        audit((0, "supported", "matched", ""), (1, "supported", "matched", ""), (2, "unsupported", "matched", "")),
    )
    # 各行は部分的に支持する2出典と、関連するだけの1出典を区別する入力を受け取る。
    assert all(len(row["citations"]) == 3 for row in inputs[0]["items"] if row["text"] == CLAIM)
    claim = next(row for row in result.checked if row.item.text == CLAIM)
    retained = {entry.span["evidence_id"] for entry in grounded.item_citations(claim)}
    assert retained == {refs[OPERATION], refs[CONDITION], refs[RELATED]}
    raw = result.timing["semantic_audit_observation"]["audit"]["reviews"]
    assert [row["support"] for row in raw] == ["supported", "supported", "unsupported"]
    assert result.verified == 0


def test_uncited_neighbor_is_not_part_of_the_claims_citation_set():
    ctx = context(record(0, RELATED, "対象Aの設定"), record(1, OPERATION, "対象Aの設定"))
    refs = ids(ctx, QUESTION)
    result, inputs = run_round(
        ctx,
        draft(item(refs[RELATED], OPERATION, RELATED, request_id="Q1")),
        audit((0, "unsupported", "matched", ""), summary_supported=False),
    )
    rows = [row for row in inputs[0]["items"] if row["text"] == OPERATION]
    assert rows
    # 未引用の隣接資料は要求の材料探索に使えても、この主張の引用集合には昇格させない。
    assert all(row["citations"] == [{"evidence_id": refs[RELATED], "quote": RELATED}] for row in rows)
    assert all(row.review_assessment["effective_support"] == "unknown" for row in result.checked if row.item.text == OPERATION)
    assert result.verified == 0


def test_supported_claim_does_not_imply_complete_answer():
    ctx = context(record(0, OPERATION, "対象Aの設定"))
    eid = ids(ctx, QUESTION)[OPERATION]
    result, _ = run_round(
        ctx,
        draft(item(eid, OPERATION, OPERATION, request_id="Q1")),
        audit((0, "supported", "matched", ""), requests=[("Q1", "partial")]),
    )
    dimensions = result.trace()["validation_dimensions"]
    assert dimensions["citation_validity"]["items"][0]["citations"][0]["status"] == "matched"
    assert dimensions["claim_support"]["items"][0]["status"] == "unknown"
    assert dimensions["answer_completeness"]["requests"][0]["status"] == "unknown"
    raw = result.timing["semantic_audit_observation"]["audit"]
    assert raw["reviews"][0]["support"] == "supported"
    assert raw["request_reviews"][0]["status"] == "partial"


def test_unknown_support_is_retained_as_unknown_with_a_warning():
    ctx = context(record(0, RELATED, "対象Aの設定"))
    eid = ids(ctx, QUESTION)[RELATED]
    result, _ = run_round(
        ctx,
        draft(item(eid, OPERATION, RELATED, request_id="Q1")),
        audit((0, "unknown", "matched", ""), requests=[("Q1", "partial")], summary_supported=False),
    )
    entry = next(row for row in result.checked if row.item.text == OPERATION)
    assert entry.published and result.verified == 0
    assert entry.review_assessment["observed_support"] == "unknown"
    assert entry.review_assessment["effective_support"] == "unknown"
    dimensions = result.trace()["validation_dimensions"]
    assert dimensions["claim_support"]["execution"] == "completed"
    assert dimensions["claim_support"]["items"][0]["status"] == "unknown"
    request = dimensions["answer_completeness"]["requests"][0]
    assert request["evidence_status"] == "unknown"
    assert request["status"] != "addressed"


def test_joint_citation_contradiction_is_not_hidden_by_discarding_the_source():
    ctx = context(record(0, OPERATION, "対象Aの設定"), record(1, CONDITION, "対象Aの設定"))
    refs = ids(ctx, QUESTION)
    claim = "未承認の場合だけ、対象Aの設定画面で保存ボタンを押します。"
    proposal = draft(
        {
            "kind": "operation",
            "request_id": "Q1",
            "text": claim,
            "citations": [
                {"evidence_id": refs[OPERATION], "quote": OPERATION},
                {"evidence_id": refs[CONDITION], "quote": CONDITION},
            ],
        }
    )
    result, _ = run_round(ctx, proposal, audit((0, "unsupported", "matched", ""), (1, "contradicted", "matched", "条件が逆")))
    checked = next(row for row in result.checked if row.item.text == claim)
    assert checked.published and not checked.removed
    raw = result.timing["semantic_audit_observation"]["audit"]["reviews"]
    assert [row["support"] for row in raw] == ["unsupported", "contradicted"]
    assert checked.review_assessment["effective_support"] == "unknown"
    assert len(list(grounded.item_citations(checked))) == 2
    assert result.verified == 0


def test_unknown_support_is_retained_without_becoming_a_rewrite_reason():
    ctx = context(record(0, OPERATION, "対象Aの設定"))
    eid = ids(ctx, QUESTION)[OPERATION]
    result, _ = run_round(ctx, draft(item(eid, OPERATION, OPERATION)), audit((0, "unknown", "unknown", "判定不能")))
    assert not result.feedback().get("downgraded_items")
    assert result.trace()["validation_dimensions"]["claim_support"]["items"][0]["status"] == "unknown"
