"""丸数字・(1) などで並べた小問の要求単位と、小問ごとの根拠の確保・回答の見出し（#2399）。外部 I/O と LLM は使わない。"""

from __future__ import annotations

import re
from typing import Any

import pytest
from rag_parser_core.language import bind_pack, builtin_pack, resolve

from rag_engine.generation import grounded
from rag_engine.generation.answer_models import AnswerContext, AnswerRecord
from rag_engine.models.llm import GroundedCitation, GroundedItem
from rag_engine.retrieval.context_builder import ContextParentEvidence
from rag_engine.retrieval.request_coverage import (
    CoverageBudget,
    TargetedRetrieval,
    coverage_targets,
    plan_targeted_retrievals,
    re_retrieval_skip_reason,
    request_rows,
    run_targeted_retrievals,
    select_new_parents,
    targeted_queries,
)
from rag_engine.retrieval.task_contract import (
    _sentence_units,
    _with_request_kind,
    definition_targets,
    request_units,
    subquestion_markers,
)

PREAMBLE = "システムAの画面Bについて教えてください。"
LINE_QUESTION = PREAMBLE + "\n①受注の登録方法は？\n②請求書の再発行方法は？\n③承認の担当は誰ですか？"


def _base(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """子要求（「Q1.F1」など）を除いた原文の単位。"""
    return [unit for unit in units if "." not in unit["id"]]


# ---- 小問の分割 ----------------------------------------------------------------------------------


def test_line_start_subquestions_become_request_units_with_preamble() -> None:
    units = _base(request_units(LINE_QUESTION))

    assert [(u["id"], u["kind"], u["text"]) for u in units] == [
        ("Q1", "context", PREAMBLE),
        ("Q2", "request", "①受注の登録方法は？"),
        ("Q3", "request", "②請求書の再発行方法は？"),
        ("Q4", "request", "③承認の担当は誰ですか？"),
    ]
    # 小問の前の共通の前置きは各小問に付き、前置きの文は背景（答える対象ではない）。
    assert [u.get("subquestion") for u in units[1:]] == ["①", "②", "③"]
    assert all(u["preamble"] == PREAMBLE for u in units[1:])


def test_subquestions_written_in_one_sentence_are_split() -> None:
    units = _base(request_units("画面Bの件で①受注の登録方法②請求書の再発行方法を教えてください"))

    assert [(u["kind"], u["text"]) for u in units] == [
        ("context", "画面Bの件で"),
        ("request", "①受注の登録方法"),
        ("request", "②請求書の再発行方法を教えてください"),
    ]
    assert units[1]["preamble"] == "画面Bの件で"


@pytest.mark.parametrize(
    ("question", "labels"),
    [
        ("次の点を教えてください。(1)受注の登録方法 (2)請求書の再発行方法", ["(1)", "(2)"]),
        ("次の点を教えてください。（１）受注の登録方法（２）請求書の再発行方法", ["（１）", "（２）"]),
        ("1) 受注の登録方法\n2) 請求書の再発行方法", ["1)", "2)"]),
        ("問1 受注の登録方法は？ 問2 請求書の再発行方法は？", ["問1", "問2"]),
    ],
    ids=["paren", "fullwidth-paren", "number-paren-line", "question-number"],
)
def test_numbered_subquestion_forms(question: str, labels: list[str]) -> None:
    assert [label for _, label in subquestion_markers(question)] == labels
    assert [u["subquestion"] for u in request_units(question) if u.get("subquestion")] == labels


@pytest.mark.parametrize(
    "question",
    [
        "①と②の違いは何ですか？",
        "手順の③を教えてください。",
        "③の欄には何を入力しますか？",
        "①～③の手順のあとに何をしますか？",
        "②受注の登録方法は？",  # 1 から始まらない
        "①受注の登録方法は？③請求書の再発行方法は？",  # 続かない
        "受注番号10)を入力した後の確認は？2)の画面は？",  # 数で始まる型は区切りの後だけ
    ],
    ids=["compare-steps", "single-step", "field-number", "range", "not-from-one", "gap", "no-boundary"],
)
def test_step_number_references_are_not_subquestions(question: str) -> None:
    assert subquestion_markers(question) == []
    assert not any(u.get("subquestion") for u in request_units(question))


@pytest.mark.parametrize(
    "question",
    [
        "受注の登録方法は？請求書の再発行方法は？",
        "先月から担当が変わった。受注の登録方法を教えてください。",
        "業務と種別を追加したい。",
        "取消済み伝票を変更して再発行したい。方法と影響を教えてください。",
        "画面の①の欄の意味は？",
        "①から③の手順で登録しましたがエラーになります。\n原因を教えてください。",
    ],
    ids=["two-questions", "background", "parallel", "chained-facets", "field-reference", "step-range"],
)
def test_questions_without_subquestion_markers_keep_the_same_units(question: str) -> None:
    units = request_units(question)

    # 小問の印の無い質問は、文ごとの単位（句点・疑問符・改行）と種類がそのまま。
    assert _base(units) == _with_request_kind(_sentence_units(question))
    assert not any("subquestion" in u or "preamble" in u for u in units)


def test_definition_targets_follow_subquestion_ids() -> None:
    question = PREAMBLE + "\n①受注の登録方法は？\n②「区分」の意味は？"
    targets = definition_targets(question)
    ids = {u["id"] for u in request_units(question) if u.get("subquestion")}

    assert targets and all(t["request_id"] in ids for t in targets)


def test_subquestion_markers_come_from_the_language_pack_not_step_markers() -> None:
    # 資料の手順の印（markers.circled_numbers）を変えても、小問の印は変わらない。
    with bind_pack(resolve(builtin_pack(), {"markers": {"circled_numbers": ["⑴-⒇"]}})):
        assert [label for _, label in subquestion_markers(LINE_QUESTION)] == ["①", "②", "③"]
    # 小問の印から丸数字を外せば、丸数字の質問は小問に分けない。
    with bind_pack(resolve(builtin_pack(), {"question_cues": {"subquestion_circled_numbers": ["⑴-⒇"]}})):
        assert subquestion_markers(LINE_QUESTION) == []
    # 言及の語は言語資源。「①は」を言及にしなければ「①は…②は…」を小問にする。
    question = "①は受注の登録方法、②は請求書の再発行方法を教えて"
    assert subquestion_markers(question) == []
    with bind_pack(resolve(builtin_pack(), {"question_cues": {"subquestion_reference_followers": ["の"]}})):
        assert [label for _, label in subquestion_markers(question)] == ["①", "②"]


# ---- 小問ごとの根拠の確保 ----------------------------------------------------------------------

_VOCABULARY = re.compile("受注|登録|請求書|再発行|承認|担当|システムA|画面B")


def split(text: str) -> list[str]:
    return _VOCABULARY.findall(text)


def record(uid: str, text: str) -> AnswerRecord:
    return AnswerRecord(
        id=uid,
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=1,
        category="Chunk",
        text=text,
        source="manual.pdf",
        source_run_id="source",
        chunk_id=uid,
        chunk_uid=f"run:{uid}",
        chunk_level="parent",
    )


def parent(uid: str, text: str) -> ContextParentEvidence:
    return ContextParentEvidence(record=record(uid, text), role="synthesis_parent", reason="top")


def context(*parents: ContextParentEvidence) -> AnswerContext:
    return AnswerContext(records=[p.record for p in parents], text="x", evidence_tree=tuple(parents))


def test_subquestion_targets_judge_body_and_search_with_preamble() -> None:
    targets = coverage_targets(LINE_QUESTION, split)

    assert [t.id for t in targets] == ["Q2", "Q3", "Q4"]
    # 判定は小問の語だけ（前置きの語で割合が上がらない）、検索は前置き＋小問の文。
    assert targets[0].terms == ("受注", "登録")
    assert targets[0].subquestion == "①"
    assert targets[0].query == PREAMBLE + " 受注の登録方法は？"
    assert targeted_queries(targets[0], ()) == (PREAMBLE + " 受注の登録方法は？",)


def test_subquestions_require_explicit_gap_before_search() -> None:
    targets = coverage_targets(LINE_QUESTION, split)
    rows = request_rows(targets)
    assert all(row["status"] == "unassessed" for row in rows)
    assert plan_targeted_retrievals(rows, targets, CoverageBudget())[0] == []
    rows[1]["answer_status"] = "missing"
    plan, stop = plan_targeted_retrievals(rows, targets, CoverageBudget())
    assert [item.target.id for item in plan] == ["Q3"] and not stop


def test_plan_counts_subquestions_and_missing_requests_in_one_place() -> None:
    targets = coverage_targets(LINE_QUESTION, split)
    rows = [{**row, "answer_status": "missing"} for row in request_rows(targets)]

    plan, stop = plan_targeted_retrievals(rows, targets, CoverageBudget(max_subquestions=2))
    assert [p.target.id for p in plan] == ["Q2", "Q3"]
    assert stop == "max_subquestions"
    plan, _ = plan_targeted_retrievals(rows, targets, CoverageBudget(chunks_per_subquestion=0))
    assert plan == []
    assert re_retrieval_skip_reason(rows, CoverageBudget(chunks_per_subquestion=0)) == "no_budget"


def test_each_subquestion_keeps_its_own_top_evidence() -> None:
    targets = coverage_targets(LINE_QUESTION, split)
    rows = [{**row, "answer_status": "missing"} for row in request_rows(targets)]
    calls: list[str] = []

    def retrieve(text: str, queries: tuple[str, ...]) -> AnswerContext:
        calls.append(text)
        key = "q" + str(len(calls))
        return context(*(parent(f"{key}-{n}", text) for n in range(4)))

    results, stop = run_targeted_retrievals(rows, targets, budget=CoverageBudget(), retrieve=retrieve, tokenize=split)

    # 小問ごとに前置き＋小問の文で 1 回ずつ検索する（欠けた要求の max_queries=2 に数えない）。
    assert calls == [PREAMBLE + " 受注の登録方法は？", PREAMBLE + " 請求書の再発行方法は？", PREAMBLE + " 承認の担当は誰ですか？"]
    assert stop == ""
    selected = select_new_parents(results, set(), max_chunks=0)
    by_request: dict[str, list[str]] = {}
    for request_id, chosen in selected:
        by_request.setdefault(request_id, []).append(chosen.record.chunk_uid)
    # 小問ごとに上位の 2 件を確保する（ほかの小問の結果に押し出されない）。
    assert by_request == {
        "Q2": ["run:q1-0", "run:q1-1"],
        "Q3": ["run:q2-0", "run:q2-1"],
        "Q4": ["run:q3-0", "run:q3-1"],
    }


def test_select_new_parents_keeps_shared_cap_for_missing_requests() -> None:
    sub = TargetedRetrieval("Q2", ("a",), tuple(parent(f"a{n}", "x") for n in range(3)), chunk_cap=1)
    missing = TargetedRetrieval("Q5", ("b",), tuple(parent(f"b{n}", "x") for n in range(3)))

    selected = select_new_parents([sub, missing], set(), max_chunks=2)

    assert [(rid, p.record.chunk_uid) for rid, p in selected] == [("Q2", "run:a0"), ("Q5", "run:b0"), ("Q5", "run:b1")]


# ---- 回答の見出し ---------------------------------------------------------------------------------


def _entry(request_id: str, text: str, page: int) -> grounded.CheckedItem:
    span = {"evidence_id": f"E{page}", "source": "manual.pdf", "page": page, "function": ("doc", "f"), "document_scope": ["doc"]}
    return grounded.CheckedItem(
        GroundedItem(
            kind="rule", text=text, citations=[GroundedCitation(evidence_id="", quote=text)], request_id=request_id, applies="matched"
        ),
        span=span,
    )


def test_answer_is_split_by_subquestion_heading_and_only_unsupported_is_gap() -> None:
    units = request_units(LINE_QUESTION)
    subquestions = [u for u in units if u.get("subquestion")]
    checked = [_entry("Q2", "受注は受注入力画面で登録します。", 3), _entry("Q3", "請求書は請求一覧で再発行します。", 5)]

    body, refs = grounded.render_with_citations("要約。", checked, subquestions=subquestions)

    labels = grounded.answer_labels()
    first = labels.subquestion_heading("①", "受注の登録方法は？")
    second = labels.subquestion_heading("②", "請求書の再発行方法は？")
    third = labels.subquestion_heading("③", "承認の担当は誰ですか？")
    assert body.index(first) < body.index("受注入力画面") < body.index(second) < body.index("請求一覧") < body.index(third)
    # 根拠の無い小問だけを「確認できない」にする。
    missing = grounded.missing_request_line({"request_id": "Q4"}, units)
    assert body.count("根拠付きの説明を十分に示せていません") == 1
    assert "取得した資料で確認できませんでした" not in body
    assert body.index(third) < body.index(missing)
    assert len(refs) == 2
    # 見出しは回答の主張ではない（忠実さの照合・評価から外す）。
    assert grounded.is_structural_line(first)


def test_answer_without_subquestion_items_keeps_the_existing_sections() -> None:
    units = request_units(LINE_QUESTION)
    subquestions = [u for u in units if u.get("subquestion")]
    checked = [_entry("", "受注は受注入力画面で登録します。", 3)]

    assert grounded.render_with_citations("要約。", checked, subquestions=subquestions) == grounded.render_with_citations("要約。", checked)


def test_missing_request_uses_bound_language_and_keeps_missing_status() -> None:
    question = "値と条件を答えてください。"
    units = request_units(question)
    audit = grounded.deterministic_audit(question, [])
    assert audit.request_reviews[0].status == "missing"
    with bind_pack(resolve(builtin_pack(), {"answer_labels": {"missing_request_template": "説明不足: {text}（{補足}）"}})):
        line = grounded.missing_request_line({"request_id": units[0]["id"]}, units)
    assert line == "説明不足: 値と条件を答えてください（{補足}）"
    assert audit.request_reviews[0].status == "missing"


def test_audit_missing_subquestion_line_moves_under_its_heading() -> None:
    units = request_units(LINE_QUESTION)
    subquestions = [u for u in units if u.get("subquestion")]
    missing = grounded.missing_request_line({"request_id": "Q3"}, units)
    checked = [_entry("Q2", "受注は受注入力画面で登録します。", 3), _entry("Q3.R1", "請求一覧の参考の記載。", 5)]

    body, _ = grounded.render_with_citations("要約。", checked, [missing], subquestions=subquestions)

    second = grounded.answer_labels().subquestion_heading("②", "請求書の再発行方法は？")
    third = grounded.answer_labels().subquestion_heading("③", "承認の担当は誰ですか？")
    assert body.count(missing) == 1
    assert body.index(second) < body.index(missing) < body.index(third)
