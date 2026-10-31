"""合成の操作説明書・CSV・応答で、修正前後を同じ入力で検証する。"""

import csv
import json
import re
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from test_grounded_answer import FakeModel, audit, context, draft, ids, item, observed_audit, record

from rag_engine.config import get_settings
from rag_engine.dependencies import AnswerDependencies, bind_dependencies
from rag_engine.evaluation.answer_validation import (
    ClaimAuditOutput,
    PremiseConsistencyOutput,
    validate_answer_claims,
)
from rag_engine.generation.answering import synthesize_grounded_answer
from rag_engine.retrieval.context_builder import ContextParentEvidence
from rag_engine.retrieval.request_coverage import CoverageBudget, coverage_targets, request_rows, run_targeted_retrievals

EXAMPLES = Path(__file__).with_name("fixtures") / "answer_accuracy"
CASES = json.loads((EXAMPLES / "cases.json").read_text())


@pytest.mark.parametrize("question", [CASES["state"]["question"], "処理 A の出力後の訂正方法を教えてください。"])
def test_original_question_state_is_checked_without_a_clarification_answer(question):
    case = CASES["state"]
    observed = []

    def parse(system, inputs, settings, schema, **options):
        payload = json.loads(inputs)
        passage = payload["answer_passages"][0]
        if schema is PremiseConsistencyOutput:
            observed.append(payload)
            return schema.model_validate(
                {
                    "checks": [
                        {
                            "answer_passage_id": passage["id"],
                            "constraints": "対象は処理 A",
                            "recommendation": "処理 B の確定後の訂正と具体値の設定",
                            "fit": "conflicts",
                            "constraint_scope": "content",
                            "passage_mode": "execution",
                            "constraint_quote": payload["question"],
                            "passage_quote": passage["text"],
                        }
                    ]
                }
            )
        assert schema is ClaimAuditOutput
        evidence = next(e for e in payload["evidence_items"] if e["id"] == "example")
        return schema.model_validate(
            {
                "claim_checks": [
                    {
                        "answer_quote": passage["text"],
                        "answer_passage_id": passage["id"],
                        "evidence_id": evidence["evidence_id"],
                        "evidence_quote": case["evidence"],
                        "source_id": "",
                        "applicability": "matched",
                        "status": "supported",
                        "reason": "資料に記載あり",
                    }
                ]
            }
        )

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims(
            question,
            case["wrong_answer"],
            [
                {"id": "example", "source": "operations.txt", "text": case["evidence"]},
            ],
            settings=None,
        )
    assert len(observed) == 1
    assert observed[0]["question"] == question
    assert result["claim_checks"][0]["status"] == "contradicted"
    assert result["claim_checks"][0]["support_check"] == "premise_conflict"


def test_context_is_required_before_coverage_can_skip_additional_retrieval():
    case = CASES["coverage"]

    def tokenize(text):
        return re.findall("対象選択|設定|入力|方法|開いて", text)

    targets = coverage_targets(case["question"], tokenize)
    rows = request_rows(targets)
    assert rows[0]["status"] == "unassessed"
    rows[0]["answer_status"] = "missing"
    calls = []

    def retrieve(text, queries):
        calls.extend(queries)
        assert any("対象選択" in query for query in queries)
        ctx = context(record(1, case["additional_evidence"], "対象選択の設定"))
        return replace(
            ctx, evidence_tree=(ContextParentEvidence(record=ctx.records[0], role="synthesis_parent", reason="targeted", children=()),)
        )

    results, stop = run_targeted_retrievals(
        rows,
        targets,
        budget=CoverageBudget(),
        retrieve=retrieve,
        tokenize=tokenize,
        searched_queries=("初回の検索文",),
    )
    assert calls and results[0].parents
    assert stop == ""
    after = request_rows(targets)
    assert after[0]["status"] == "unassessed"


@pytest.mark.parametrize("mode", ["none", "detailed"])
@pytest.mark.parametrize("summary_key", ["wrong_summary", "missing_qualifier", "reversed_target", "correct_summary"])
def test_summary_observation_does_not_rewrite_the_generated_conclusion(summary_key, mode):
    case = CASES["summary"]
    assert case["evidence"] in (EXAMPLES / "operations.txt").read_text()
    with (EXAMPLES / "rows.csv").open() as stream:
        retained = [row["id"] for row in csv.DictReader(stream) if row["update_required"] == "true"]
    assert retained == case["retained_ids"]
    ctx = context(record(1, case["evidence"], "CSV の更新", source="operations.txt"))
    evidence_id = ids(ctx, case["question"])[case["evidence"]]
    model = FakeModel(
        [draft(item(evidence_id, case["evidence"], case["evidence"]), summary=case[summary_key])],
        [audit((0, "supported", "matched", ""), requests=[("Q1", "addressed")], summary_supported=summary_key == "correct_summary")],
    )

    def unused(*args, **kwargs):
        raise AssertionError("unexpected I/O")

    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        response = synthesize_grounded_answer(
            case["question"],
            ctx,
            replace(get_settings(), answer_audit_mode=mode),
            image_prompt_mode="text_only",
        ).response
    assert response.answer_text.startswith(case[summary_key])
    assert response.generation_trace["finalization"]["summary"]["status"] == ("unverified" if mode == "none" else "kept")
    if mode == "none":
        assert response.generation_trace["rounds"][0]["review_execution"] == "not_run"
        assert len([name for name, _ in model.prompts if name == "GroundedAudit"]) == 0
    else:
        assert observed_audit(response)["summary_supported"] is (summary_key == "correct_summary")
        assert len([name for name, _ in model.prompts if name == "GroundedAudit"]) == 1
        assert not response.envelope["removed_passages"]
    assert case["evidence"] in response.answer_text
