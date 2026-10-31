"""retrieval eval の順位つき指標（MRR / hit@k）を守るテスト (#861)。"""

from rag_engine.evaluation.rag_eval import RagEvalCase, RagEvalRetrieval, evaluate_rag_cases, format_rag_eval_summary


def _retriever(mapping):
    return lambda case: mapping[case.case_id]


def test_mrr_and_hit_at_k_use_the_first_rank_of_an_expected_hit():
    cases = [
        RagEvalCase(case_id="a", question="q", expected_child_ids=("c2",)),  # 2 位で命中 → 1/2
        RagEvalCase(case_id="b", question="q", expected_parent_ids=("p9",)),  # 命中なし → 0
        RagEvalCase(case_id="c", question="q", expected_pages=(4,)),  # ページ 1 位 → 1
        RagEvalCase(case_id="d", question="q", expected_terms=("語",)),  # 順位つき指標の分母に入れない
    ]
    retrievals = {
        "a": RagEvalRetrieval(child_ids=("c1", "c2", "c3"), parent_ids=("p1",)),
        "b": RagEvalRetrieval(parent_ids=("p1", "p2")),
        "c": RagEvalRetrieval(pages=(4, 5)),
        "d": RagEvalRetrieval(text="語がある"),
    }
    summary = evaluate_rag_cases(cases, _retriever(retrievals))
    assert summary["mrr"] == round((0.5 + 0.0 + 1.0) / 3, 4)  # _rate は小数 4 桁に丸める
    assert summary["hit_at_k"] == {"1": round(1 / 3, 4), "3": round(2 / 3, 4), "5": round(2 / 3, 4), "10": round(2 / 3, 4)}
    assert [r["first_hit_rank"] for r in summary["results"]] == [2, None, 1, None]
    text = format_rag_eval_summary(summary)
    assert "mrr=0.500" in text and "hit@k=1:33.3%" in text


def test_metrics_are_zero_when_no_case_has_ranked_expectations():
    cases = [RagEvalCase(case_id="a", question="q", expected_terms=("語",))]
    summary = evaluate_rag_cases(cases, _retriever({"a": RagEvalRetrieval(text="")}))
    assert summary["mrr"] == 0.0 and summary["hit_at_k"]["1"] == 0.0
