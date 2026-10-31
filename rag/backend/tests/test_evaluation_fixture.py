"""リポジトリ同梱 golden set テンプレートの契約テスト。"""

import json
from pathlib import Path

from app.rag.search_load_cli import SearchLoadScenario
from app.schemas.evaluation import EvaluationRunRequest


def test_golden_set_example_matches_evaluation_run_schema() -> None:
    """example golden set は評価 API の request schema と一致する。"""
    repo_root = Path(__file__).resolve().parents[2]
    payload = json.loads(
        (repo_root / "evaluation/golden-set.example.json").read_text(encoding="utf-8")
    )

    request = EvaluationRunRequest.model_validate(payload)

    assert request.cases
    assert request.filters == {"status": "INDEXED"}
    assert request.rag_overrides is not None
    assert request.rag_overrides.rrf_k == 60
    assert request.thresholds is not None
    assert all(case.id and case.query for case in request.cases)
    # 答えるべき質問・答えるべきでない質問・標準回答のあるケースを含む(#591)。
    assert any(case.expects_answer for case in request.cases)
    assert any(not case.expects_answer for case in request.cases)
    assert any(case.standard_answer for case in request.cases)


def test_search_load_example_matches_load_schema() -> None:
    """search load example は p95 gate CLI の scenario schema と一致する。"""
    repo_root = Path(__file__).resolve().parents[2]
    payload = json.loads(
        (repo_root / "evaluation/search-load.example.json").read_text(encoding="utf-8")
    )

    scenario = SearchLoadScenario.model_validate(payload)

    assert scenario.cases
    assert scenario.repeat > 0
    assert scenario.concurrency > 0
    assert scenario.thresholds.server_p95_ms is not None
    assert all(case.top_k > 0 for case in scenario.cases)
    assert all(value > 0 for value in scenario.thresholds.stage_p95_ms.values())


def test_business_support_set_covers_every_category_and_its_corpus_exists() -> None:
    """業務支援の合成の評価セット（#1231）は 5 分類をすべて持ち、参照する資料が同梱されている。"""
    from app.rag.evaluation_corpus_cli import referenced_files

    corpus = Path(__file__).resolve().parents[2] / "evaluation/business-support"
    payload = json.loads((corpus / "business-support.json").read_text(encoding="utf-8"))
    request = EvaluationRunRequest.model_validate(payload)

    assert {case.category for case in request.cases} == {
        "document_answerable",
        "clarification_required",
        "environment_data_required",
        "knowledge_missing",
        "conflicting_sources",
    }
    assert all(case.expected_outcomes for case in request.cases)
    # 資料に答えの無い質問は、拒答か人への引き継ぎだけを正しいとする。
    for case in request.cases:
        if case.category == "knowledge_missing":
            assert case.answerable is False
            assert set(case.expected_outcomes) <= {"insufficient_evidence", "needs_human"}
    names = referenced_files(payload)
    assert names
    assert all((corpus / name).is_file() for name in names)
    assert all(
        (corpus / "sources" / f"{Path(name).stem}.html").is_file()
        for name in names
        if name.endswith(".pdf")
    )


def test_business_support_set_has_splits_turns_and_evidence_from_its_corpus() -> None:
    """区分・往復・既知の条件・必要な根拠（#1284）は、同梱の資料だけを使う。"""
    import re

    from app.rag.evaluation_handling import contains_normalized

    corpus = Path(__file__).resolve().parents[2] / "evaluation/business-support"
    payload = json.loads((corpus / "business-support.json").read_text(encoding="utf-8"))
    request = EvaluationRunRequest.model_validate(payload)

    # すべてのケースに区分があり、holdout は 3 分の 1 前後で、5 分類のどれも含む。
    assert all(case.split in {"dev", "holdout"} for case in request.cases)
    holdout = [case for case in request.cases if case.split == "holdout"]
    assert 0.25 <= len(holdout) / len(request.cases) <= 0.45
    assert {case.category for case in holdout} == {
        "document_answerable",
        "clarification_required",
        "environment_data_required",
        "knowledge_missing",
        "conflicting_sources",
    }
    # 複数往復と既知の条件のケースがある。
    assert any(case.turns for case in request.cases)
    assert any(case.conditions for case in request.cases)
    # 必要な根拠の語句は、指した資料の原稿に実際に書いてある（架空の値を足さない）。
    evidence = [item for case in request.cases for item in case.required_evidence]
    assert evidence
    for item in evidence:
        assert item.document_id is not None and item.document_id.startswith("file:")
        # 評価セットの根拠は本文の語句で書く（ページ・章節だけの根拠は Excel の取込で作る。#2065）。
        assert item.text is not None, item.id
        source = corpus / "sources" / f"{Path(item.document_id.removeprefix('file:')).stem}.html"
        text = re.sub(r"<[^>]+>", "", source.read_text(encoding="utf-8"))
        assert contains_normalized(text, item.text), item.id


def test_business_support_guides_are_valid_and_match_the_set() -> None:
    """C（業務ガイドあり）の業務ガイド（#1289）は、検証を通り、同梱の資料だけを参照する。

    評価セットの条件の id（`target` / `approved`）と値は、業務ガイドの条件の選択肢にある。
    """
    from app.rag.support_guide import has_errors, validate_content
    from app.schemas.support_guide import SupportGuideContent

    corpus = Path(__file__).resolve().parents[2] / "evaluation/business-support"
    payload = json.loads((corpus / "support-guides.json").read_text(encoding="utf-8"))
    guides = [SupportGuideContent.model_validate(raw) for raw in payload["guides"]]
    assert guides
    for guide in guides:
        assert not has_errors(validate_content(guide)), guide.title
        for reference in guide.references:
            assert reference.document_id.startswith("file:")
            assert (corpus / reference.document_id.removeprefix("file:")).is_file()
    allowed = {
        condition.id: set(condition.allowed_values)
        for guide in guides
        for condition in guide.conditions
    }
    cases = EvaluationRunRequest.model_validate(
        json.loads((corpus / "business-support.json").read_text(encoding="utf-8"))
    ).cases
    used = [
        (condition_id, value)
        for case in cases
        for conditions in [case.conditions, *(turn.conditions for turn in case.turns)]
        for condition_id, value in conditions.items()
    ]
    assert used
    for condition_id, value in used:
        assert value in allowed[condition_id], (condition_id, value)
