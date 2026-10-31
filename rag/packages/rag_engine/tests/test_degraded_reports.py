"""rag_engine の中の補助の処理の失敗を、呼び出し元の記録（``report_degraded``）に渡す（#3284・#3286）。

補助の処理（要求ごとの根拠の探し直し・用語・同義語と回答ルールの読み込み・rerank・追加の検索文の rerank・CRAG の
根拠確認・全文検索の語の分割・章節の子の並べ替え・条の目録の元の質問の検索）は、失敗しても縮退して
回答を続ける。失敗は処理の種類と例外を ``report_degraded`` に渡し、呼び出し元（backend）が回答の
診断の ``degraded_steps`` に型の名前だけを残す。結果が空なだけのとき（失敗ではない）は渡さない。
診断と工程の詳細には例外の文を入れない。外部 I/O と LLM はすべてスタブ。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from test_answer_generation import _answer_context, _answer_output, _chunk_record, _chunk_run_stub, _crag_grade
from test_request_coverage import _missing, context, parent, split

import rag_engine.generation.answering as answering
from rag_engine.config import get_settings
from rag_engine.dependencies import AnswerDependencies, bind_dependencies
from rag_engine.generation import section_expansion as sections
from rag_engine.generation.answering import AnswerRecord, answer_question_result, rerank_records
from rag_engine.knowledge.runtime_knowledge import build_runtime_knowledge_context, load_runtime_knowledge
from rag_engine.retrieval import text_search_tokenizer as tokenizer
from rag_engine.retrieval.request_coverage import CoverageBudget, run_targeted_retrievals

# 例外の文に入れる値。記録・工程の詳細のどこにも出てはいけない（DB のエラー・SQL・path の代役）。
SECRET = "ORA-00942 SELECT secret_column FROM doc-secret /work/secret-dir"


class Boom(RuntimeError):
    """補助の処理の失敗の代役（型の名前だけが記録に出る）。"""


def _unused(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("呼ばない")


@contextmanager
def _recording(**fields: Any) -> Iterator[list[tuple[str, str]]]:
    """``report_degraded`` を受け取る依存を結び付け、(処理の種類, 例外の型の名前) を集める。"""
    reported: list[tuple[str, str]] = []
    dependencies = AnswerDependencies(
        search=_unused,
        check_ready=_unused,
        parse_text=_unused,
        parse_images=_unused,
        rerank=_unused,
        report_degraded=lambda kind, exc: reported.append((kind, type(exc).__name__)),
        **fields,
    )
    with bind_dependencies(dependencies):
        yield reported


# ---- 要求ごとの根拠の探し直し（#3284） ----


@pytest.mark.parametrize("fail", [True, False], ids=["error", "empty"])
def test_targeted_retrieval_failure_keeps_only_the_type_name(fail: bool) -> None:
    targets, rows = _missing(1)

    def retrieve(text: str, queries: tuple[str, ...]) -> Any:
        if fail:
            raise Boom(SECRET)
        return context()

    with _recording() as reported:
        results, _ = run_targeted_retrievals(rows, targets, budget=CoverageBudget(), retrieve=retrieve, tokenize=split)

    assert results[0].parents == ()
    if fail:
        # 記録に残すのは例外の型の名前だけ（DB のエラーの文を診断に出さない）。
        assert results[0].error == "Boom"
        assert reported == [("request_coverage_retrieval", "Boom")]
    else:
        assert results[0].error == ""
        assert reported == []


def test_targeted_retrieval_with_new_evidence_is_not_reported() -> None:
    targets, rows = _missing(1)

    with _recording() as reported:
        results, _ = run_targeted_retrievals(
            rows, targets, budget=CoverageBudget(), retrieve=lambda text, queries: context(parent("new", "語1")), tokenize=split
        )

    assert results[0].parents and not results[0].error
    assert reported == []


# ---- 用語・同義語と回答ルールの読み込み（#3284） ----


def test_runtime_knowledge_load_error_keeps_only_the_type_name(tmp_path: Path) -> None:
    path = tmp_path / "runtime_knowledge.json"
    path.write_text("{" + SECRET, encoding="utf-8")

    knowledge = load_runtime_knowledge(tmp_path, path)
    runtime = build_runtime_knowledge_context("質問", tmp_path, path)

    assert knowledge.error == "JSONDecodeError" and not knowledge.loaded
    assert isinstance(knowledge.failure, json.JSONDecodeError)
    assert runtime.error == "JSONDecodeError" and isinstance(runtime.failure, json.JSONDecodeError)
    # 画面・記録の payload には例外の文を出さない（例外の object も出さない）。
    payload = json.dumps(runtime.to_payload(), ensure_ascii=False)
    assert SECRET not in payload and "secret-dir" not in payload


@pytest.mark.parametrize("content", [None, '{"terms": [], "rules": []}'], ids=["missing", "empty"])
def test_runtime_knowledge_without_entries_is_not_a_failure(tmp_path: Path, content: str | None) -> None:
    path = tmp_path / "runtime_knowledge.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")

    runtime = build_runtime_knowledge_context("質問", tmp_path, path)

    assert runtime.error == "" and runtime.failure is None


# ---- 回答の全体（用語・同義語と回答ルール / CRAG の根拠確認 / 全文検索の語の分割） ----


def _answer(settings: Any, side_effect: list[Any], *, answer_flow: str) -> tuple[Any, list[tuple[str, str]]]:
    """検索と LLM をスタブにして回答し、回答の中で記録に渡した (処理の種類, 型の名前) を返す。"""
    from grounded_stub import stub_generation

    reported: list[tuple[str, str]] = []
    context = _answer_context([_chunk_record("chunk-safe", "child", 1, "", "available evidence")])
    # 生成は 1 回の LLM 呼び出しに置き換える（生成そのものは test_grounded_answer が確かめる）。
    cleanups: list[Any] = []
    stub_generation(SimpleNamespace(addCleanup=cleanups.append))
    try:
        return _answer_with_stubs(settings, side_effect, answer_flow, context, reported)
    finally:
        for cleanup in reversed(cleanups):
            cleanup()


def _answer_with_stubs(
    settings: Any, side_effect: list[Any], answer_flow: str, context: Any, reported: list[tuple[str, str]]
) -> tuple[Any, list[tuple[str, str]]]:
    with (
        patch("rag_engine.generation.answering.load_latest_or_source_chunk_run", return_value=_chunk_run_stub()),
        patch("rag_engine.generation.answering.check_adb_hybrid_search_ready"),
        patch("rag_engine.generation.answering.build_adb_hybrid_answer_context", return_value=context),
        patch("rag_engine.generation.answering.parse_text_response", side_effect=side_effect),
        patch(
            "rag_engine.generation.answering.report_degraded",
            side_effect=lambda kind, exc: reported.append((kind, type(exc).__name__)),
        ),
    ):
        result = answer_question_result(
            "fallback question",
            "abcdef",
            ["docling"],
            settings,
            query_strategy=answering.SIMPLE_RETRIEVAL_LABEL,
            answer_flow=answer_flow,
        )
    return result, reported


def _kinds(reported: list[tuple[str, str]]) -> set[str]:
    return {kind for kind, _ in reported}


def test_runtime_knowledge_failure_is_reported_and_the_step_shows_only_the_type(tmp_path: Path) -> None:
    path = tmp_path / "runtime_knowledge.json"
    path.write_text("{" + SECRET, encoding="utf-8")
    settings = replace(get_settings(), runtime_knowledge_path=path)

    result, reported = _answer(
        settings,
        [_answer_output(answer="answer", cited_evidence_id="chunk-safe")],
        answer_flow=answering.STANDARD_ANSWER_FLOW_LABEL,
    )

    # 補助情報なしで回答を続ける。
    assert "answer" in result.answer
    assert ("runtime_knowledge", "JSONDecodeError") in reported
    assert "読込エラー: JSONDecodeError" in result.question_display
    assert SECRET not in result.question_display and "secret-dir" not in result.question_display


def test_runtime_knowledge_not_registered_is_not_reported(tmp_path: Path) -> None:
    settings = replace(get_settings(), runtime_knowledge_path=tmp_path / "missing.json")

    _, reported = _answer(
        settings,
        [_answer_output(answer="answer", cited_evidence_id="chunk-safe")],
        answer_flow=answering.STANDARD_ANSWER_FLOW_LABEL,
    )

    assert "runtime_knowledge" not in _kinds(reported)


@pytest.mark.parametrize("fail", [True, False], ids=["error", "graded"])
def test_crag_grade_failure_is_reported(fail: bool) -> None:
    grade = (
        Boom(SECRET)
        if fail
        else _crag_grade(sufficient=True, confidence=0.9, relevant_chunk_ids=["chunk-safe"], support_text="available evidence")
    )

    result, reported = _answer(
        get_settings(),
        [grade, _answer_output(answer="fallback answer", cited_evidence_id="chunk-safe")],
        answer_flow=answering.CRAG_ANSWER_FLOW_LABEL,
    )

    # 評価の失敗は判定不能で、不足を確かめていない。取得済みの根拠で生成へ進み、失敗の診断を保持する（#3837）。
    if fail:
        assert "fallback answer" in result.answer
        assert result.crag_attempts[0]["grade_execution"] == "error"
        assert result.crag_attempts[0]["required_aspect_coverage"] == "unknown"
        assert result.generation_trace["crag_generation_gate"]["reason"] == "grading_incomplete"
        assert result.generation_trace["crag_generation_gate"]["generation_usable"] is False
        assert ("crag_grade", "Boom") in reported
        assert SECRET not in result.question_display
    else:
        assert "fallback answer" in result.answer
        assert "crag_grade" not in _kinds(reported)


@contextmanager
def _sudachi_broken() -> Iterator[None]:
    """Sudachi（辞書）を使えない環境（process ごとの確認の cache を前後で消す）。"""
    tokenizer._sudachi_failure.cache_clear()
    try:
        with patch.object(tokenizer, "_get_sudachi_tokenizer", side_effect=Boom(SECRET)):
            yield
    finally:
        tokenizer._sudachi_failure.cache_clear()


def test_sudachi_unavailable_is_reported_once_per_answer() -> None:
    with _sudachi_broken():
        assert isinstance(tokenizer.sudachi_unavailable(), Boom)
        result, reported = _answer(
            get_settings(),
            [_answer_output(answer="answer", cited_evidence_id="chunk-safe")],
            answer_flow=answering.STANDARD_ANSWER_FLOW_LABEL,
        )

    # 文字種の区切りだけで検索語を作って回答を続け、回答ごとに 1 回だけ記録に渡す。
    assert "answer" in result.answer
    assert reported.count(("text_search_tokenizer", "Boom")) == 1


def test_sudachi_available_is_not_reported() -> None:
    tokenizer._sudachi_failure.cache_clear()
    if tokenizer.sudachi_unavailable() is not None:
        pytest.skip("Sudachi（辞書）が入っていない環境")

    _, reported = _answer(
        get_settings(),
        [_answer_output(answer="answer", cited_evidence_id="chunk-safe")],
        answer_flow=answering.STANDARD_ANSWER_FLOW_LABEL,
    )

    assert "text_search_tokenizer" not in _kinds(reported)


@pytest.mark.parametrize("fail", [True, False], ids=["error", "ok"])
def test_sudachi_split_failure_is_reported(fail: bool) -> None:
    config = tokenizer.normalize_text_search_tokenizer_config(None)

    with (
        _recording() as reported,
        patch.object(tokenizer, "_sudachi_tokens", side_effect=Boom(SECRET) if fail else lambda text, cfg: ["受注"]),
    ):
        terms = tokenizer._content_terms("受注の登録", config, True)

    # 失敗しても文字種の区切りで語を作る。
    assert terms
    assert reported == ([("text_search_tokenizer", "Boom")] if fail else [])


# ---- rerank（原質問・追加の検索文） ----


def _child(index: int) -> AnswerRecord:
    return AnswerRecord(
        id=f"c{index}",
        engine="docling",
        engine_label="Docling",
        page=index,
        seq_no=index,
        category="Chunk",
        text=f"抽出条件を選んで実行ボタンを押します。{index}",
        source="架空仕入業務.pdf",
        chunk_level="child",
        chunk_id=f"c{index}",
        chunk_uid=f"uid:c{index}",
        source_run_id="run",
        metadata={},
    )


def _rerank_settings() -> Any:
    return SimpleNamespace(rerank_model="rerank-test", rerank_enabled=True, document_selection_enabled=False)


@pytest.mark.parametrize("fail", [True, False], ids=["error", "empty"])
def test_rerank_failure_is_reported_and_keeps_the_retrieval_order(monkeypatch: pytest.MonkeyPatch, fail: bool) -> None:
    monkeypatch.setattr(answering, "_rerank_configured", lambda settings: True)

    def rerank(question: str, documents: list[str], settings: Any, top_n: int | None = None) -> list[Any]:
        if fail:
            raise Boom(SECRET)
        return []

    monkeypatch.setattr(answering, "rerank_text_with_scores", rerank)
    records = [_child(i) for i in range(3)]

    with _recording() as reported:
        ranked = rerank_records("抽出条件の指定方法", records, _rerank_settings(), enabled=True, business_domains=(), screen_terms=())

    assert [record.id for record in ranked] == ["c0", "c1", "c2"]
    assert reported == ([("rerank", "Boom")] if fail else [])


@pytest.mark.parametrize("fail", [True, False], ids=["error", "empty"])
def test_extra_query_rerank_failure_is_reported(monkeypatch: pytest.MonkeyPatch, fail: bool) -> None:
    monkeypatch.setattr(answering, "_rerank_configured", lambda settings: True)

    def rerank(question: str, documents: list[str], settings: Any, top_n: int | None = None) -> list[Any]:
        if question != "抽出条件の指定方法":
            if fail:
                raise Boom(SECRET)
            return []
        return [SimpleNamespace(index=index, relevance_score=1.0 - index / 10) for index in range(len(documents))]

    monkeypatch.setattr(answering, "rerank_text_with_scores", rerank)
    records = [_child(i) for i in range(3)]

    with _recording() as reported:
        ranked = rerank_records(
            "抽出条件の指定方法",
            records,
            _rerank_settings(),
            enabled=True,
            business_domains=(),
            screen_terms=(),
            extra_queries=("実行ボタン",),
        )

    # 原質問の rerank は成功したので、原質問の関連度で並べる（rerank の失敗とは数えない）。
    assert [record.id for record in ranked] == ["c0", "c1", "c2"]
    assert reported == ([("rerank_extra_query", "Boom")] if fail else [])


# ---- 章節の子の章節の並べ替え ----


def _outline() -> tuple[sections.SectionOutline, list[sections.SectionInfo]]:
    children = [sections.SectionInfo(f"s{index}", f"第{index}節", 2, "s0") for index in range(1, 4)]
    root = sections.SectionInfo("s0", "第1章", 1, None)
    return sections.SectionOutline("doc", "set", "s1", (root, *children)), children


@pytest.mark.parametrize("fail", [True, False], ids=["error", "empty"])
def test_section_child_rerank_failure_is_reported(fail: bool) -> None:
    outline, children = _outline()

    def rerank(question: str, documents: list[str], settings: Any, top_n: int | None = None) -> list[Any]:
        if fail:
            raise Boom(SECRET)
        return []

    with _recording() as reported:
        ranked, ranking = sections._rank_children("第2節の内容", outline, children, rerank, None)

    # どちらも語の重なりで並べる。記録で失敗と「rerank の結果が空」を分ける。
    assert ranking == "lexical" and len(ranked) == 3
    assert reported == ([("section_child_rerank", "Boom")] if fail else [])


# ---- 条の目録の元の質問の検索 ----


@pytest.mark.parametrize("fail", [True, False], ids=["error", "empty"])
def test_article_original_question_search_failure_is_reported(monkeypatch: pytest.MonkeyPatch, fail: bool) -> None:
    def search(**kwargs: Any) -> Any:
        if fail:
            raise Boom(SECRET)
        return SimpleNamespace(child_chunks=[])

    monkeypatch.setattr(answering, "search_adb_hybrid_chunks", search)
    settings = get_settings(environ={}, dotenv_path=None)
    options = {"chunk_run_id": "", "retrieval_queries": ["書類 表記"], "settings": settings}

    with _recording(article_question=lambda: "取引先から届いた書類の補正を求めるべきか") as reported:
        found = answering._original_question_children("書類 表記", ["書類 表記"], dict(options), None, settings)

    assert found == []
    assert reported == ([("article_original_question", "Boom")] if fail else [])
