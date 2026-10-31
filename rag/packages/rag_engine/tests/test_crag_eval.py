"""crag eval の挙動を保護するテスト。"""

import json
from pathlib import Path

import pytest

from rag_engine.evaluation.crag_eval import (
    CragEvalCase,
    CragEvalVariantResult,
    evaluate_crag_goldset,
    format_crag_eval_summary,
    load_crag_goldset,
)
from rag_engine.evaluation.rag_eval import (
    RagEvalCase,
    RagEvalRetrieval,
    evaluate_rag_cases,
    format_rag_eval_summary,
    load_rag_eval_dataset,
)
from rag_engine.generation.answering import AnswerContext, AnswerRecord
from rag_engine.retrieval.context_builder import ContextChildEvidence, ContextParentEvidence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GOLDSET = PROJECT_ROOT / "eval" / "crag_goldset.json"
RETRIEVAL_DATASET = PROJECT_ROOT / "eval" / "retrieval_representative_questions.json"
# 評価データは顧客の問い合わせ由来なので repo 外にある。ローカルにある環境だけで照合する。
needs_goldset = pytest.mark.skipif(not GOLDSET.is_file(), reason="ローカルの eval/crag_goldset.json がない")
needs_retrieval_dataset = pytest.mark.skipif(
    not RETRIEVAL_DATASET.is_file(), reason="ローカルの eval/retrieval_representative_questions.json がない"
)


@needs_goldset
def test_crag_goldset_has_minimum_representative_cases():
    cases = load_crag_goldset(GOLDSET)

    assert len(cases) >= 15
    assert len({case.case_id for case in cases}) == len(cases)


@needs_goldset
def test_crag_goldset_summary_keeps_crag_at_least_as_good_as_standard():
    cases = load_crag_goldset(GOLDSET)

    summary = evaluate_crag_goldset(cases)
    text = format_crag_eval_summary(summary)

    assert "CRAG goldset cases:" in text
    assert "standard_rag" in text
    assert "crag_rerank" in text
    assert summary["variants"]["crag"]["retrieval_hit_rate"] >= summary["variants"]["standard_rag"]["retrieval_hit_rate"]
    assert summary["variants"]["crag_rerank"]["retrieval_hit_rate"] >= summary["variants"]["crag"]["retrieval_hit_rate"]
    assert summary["variants"]["crag"]["answer_correctness_rate"] >= summary["variants"]["standard_rag"]["answer_correctness_rate"]
    assert summary["variants"]["crag"]["crag_score"] >= summary["variants"]["standard_rag"]["crag_score"]
    assert "crag_score=" in text


def test_crag_score_penalizes_wrong_answers_more_than_missing_answers():
    def case(case_id, answer):
        return CragEvalCase(case_id, "質問", (), ("赤枠", "実行"), {"crag": CragEvalVariantResult(answer=answer)})

    cases = (case("perfect", "赤枠の実行ボタンを押します。"), case("missing", "わかりません。"), case("wrong", "青枠を押します。"))

    metrics = evaluate_crag_goldset(cases)["variants"]["crag"]

    assert metrics["judgements"] == {"Perfect": 1, "Acceptable": 0, "Missing": 1, "Incorrect": 1}
    assert metrics["crag_score"] == 0.0
    # variant の回答がないケースは Missing（0 点）で、誤答のように減点しない。
    assert evaluate_crag_goldset(cases)["variants"]["standard_rag"]["crag_score"] == 0.0


@needs_goldset
def test_crag_eval_uses_injected_judge_with_ground_truth():
    cases = load_crag_goldset(GOLDSET)
    seen = []

    def judge(case, answer):
        seen.append(case.ground_truth)
        return "Acceptable"

    summary = evaluate_crag_goldset(cases, judge)

    assert all(seen)
    assert summary["variants"]["crag"]["crag_score"] == 0.5


def test_parent_child_rag_eval_scores_parent_child_and_page_hits():
    cases = (
        RagEvalCase(
            case_id="case-1",
            question="needle?",
            expected_parent_ids=("parent-1",),
            expected_child_ids=("child-1",),
            expected_pages=(2,),
            expected_terms=("needle", "evidence"),
        ),
        RagEvalCase(
            case_id="case-2",
            question="missing?",
            expected_parent_ids=("parent-2",),
            expected_child_ids=("child-2",),
            expected_pages=(4,),
            expected_terms=("missing", "evidence"),
        ),
    )
    retrievals = {
        "case-1": RagEvalRetrieval(parent_ids=("parent-1",), child_ids=("child-1",), pages=(2,), text="needle evidence"),
        "case-2": RagEvalRetrieval(parent_ids=("other-parent",), child_ids=("other-child",), pages=(5,), text="wrong context"),
    }

    summary = evaluate_rag_cases(cases, lambda case: retrievals[case.case_id])
    text = format_rag_eval_summary(summary)

    assert summary["case_count"] == 2
    assert summary["parent_hit_rate"] == 0.5
    assert summary["child_hit_rate"] == 0.5
    assert summary["page_hit_rate"] == 0.5
    assert summary["term_hit_rate"] == 0.5
    assert "Parent-child RAG eval cases: 2" in text
    assert "term_hit=50.0%" in text


@needs_retrieval_dataset
def test_retrieval_representative_dataset_has_30_to_50_cases_and_diverse_tags():
    cases = load_rag_eval_dataset(RETRIEVAL_DATASET)
    payload = json.loads(RETRIEVAL_DATASET.read_text(encoding="utf-8"))
    tags = {tag for case in payload["cases"] for tag in case.get("tags", [])}

    assert 30 <= len(cases) <= 50
    assert len(cases) == 40
    assert all(case.question for case in cases)
    assert sum(1 for case in cases if case.expected_terms) >= 35
    assert {"visual", "table", "form", "alias", "no_answer", "comparison"} <= tags


def test_parent_child_rag_eval_extracts_anchor_children_from_answer_context():
    parent_record = _chunk_record("parent-1", "parent", page=2, page_end=3, text="parent synthesis")
    anchor_child = _chunk_record("child-1", "child", page=3, page_end=3, text="anchor text")
    neighbor_child = _chunk_record("child-2", "child", page=2, page_end=2, text="neighbor text")
    context = AnswerContext(
        records=[parent_record],
        text="context",
        evidence_tree=(
            ContextParentEvidence(
                record=parent_record,
                role="synthesis_parent",
                reason="parent_of_retrieved_child",
                children=(
                    ContextChildEvidence(anchor_child, "retrieved_anchor", "top_k_child", retrieval_rank=1),
                    ContextChildEvidence(neighbor_child, "neighbor_context", "adjacent_child"),
                ),
            ),
        ),
    )

    retrieval = RagEvalRetrieval.from_answer_context(context)

    assert retrieval.parent_ids == ("parent-1",)
    assert retrieval.child_ids == ("child-1",)
    assert retrieval.pages == (2, 3)


def _chunk_record(record_id: str, level: str, *, page: int, page_end: int, text: str) -> AnswerRecord:
    return AnswerRecord(
        id=record_id,
        engine="docling",
        engine_label="Docling",
        page=page,
        seq_no=1,
        category="ParentChunk" if level == "parent" else "ChildChunk",
        text=text,
        chunk_id=record_id,
        chunk_level=level,
        chunk_seq=1,
        page_end=page_end,
    )
