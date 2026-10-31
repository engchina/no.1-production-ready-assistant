"""同じ説明の異なる部分を、別の引用が共同で支える監査入力を検証する。"""

import json
from dataclasses import replace

from test_grounded_answer import FakeModel, audit, context, draft, ids, record

from rag_engine.config import get_settings
from rag_engine.dependencies import AnswerDependencies, bind_dependencies
from rag_engine.generation import grounded
from rag_engine.generation.answering import synthesize_grounded_answer
from rag_engine.models.llm import GroundedFactAudit


def test_complementary_sources_are_audited_as_one_citation_union():
    question = "対象画面を開き、内容を保存する手順は？"
    opening, saving = "対象画面を開きます。", "内容を保存します。"
    claim = opening + saving
    ctx = context(record(0, opening, "設定", source="手順A.pdf"), record(1, saving, "設定", source="手順B.pdf"))
    labels = ids(ctx, question)
    first, second = labels[opening], labels[saving]
    proposal = {
        "kind": "operation",
        "text": claim,
        "citations": [{"evidence_id": first, "quote": opening}, {"evidence_id": second, "quote": saving}],
    }
    observed = []

    def semantic_review(prompt):
        data = json.loads(prompt)
        observed.append(data)
        expected = {(first, opening), (second, saving)}
        assert len(data["items"]) == 2
        assert len({item["index"] for item in data["items"]}) == 2
        for item in data["items"]:
            assert item["text"] == claim
            assert {(cite["evidence_id"], cite["quote"]) for cite in item["citations"]} == expected
            assert item["quote"] in {opening, saving}
        assert opening in data["evidence"][first]
        assert saving in data["evidence"][second]
        # 原文集合が全体を支え、各出典もそれぞれ別の操作へ寄与するという同次監査の観察。
        verdict = audit(
            *[(item["index"], "supported", "matched", "引用集合が全体を支持し、この引用は対応する操作を支持") for item in data["items"]]
        )
        return {key: value for key, value in verdict.items() if key in GroundedFactAudit.model_fields}

    model = FakeModel([draft(proposal, summary="")], [semantic_review])

    def unused(*args, **kwargs):
        raise AssertionError("外部I/Oは不要")

    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        response = synthesize_grounded_answer(
            question,
            ctx,
            replace(get_settings(environ={}, dotenv_path=None), answer_audit_mode=grounded.AUDIT_MODE_STANDARD),
            image_prompt_mode="text_only",
        ).response
    assert len(observed) == 1
    assert response.answer_text.count(claim) == 1
    assert {row["source"] for row in response.cited_evidence} == {"手順A.pdf", "手順B.pdf"}
    assert response.generation_trace["rounds"][0]["validation_dimensions"]["claim_support"]["items"][0]["status"] == "unknown"
    observation = response.generation_trace["semantic_audit_observation"]
    assert observation["adopted"] is False
    assert [row["support"] for row in observation["audit"]["reviews"]] == ["supported", "supported"]
