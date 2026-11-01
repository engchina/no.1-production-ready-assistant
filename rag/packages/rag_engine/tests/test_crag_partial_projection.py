"""CRAG の部分回答の扱い（project_partial_answer）。監査なしで確かめた部分が増えても説明を外さない（#3843）。

合成の根拠・説明だけを使い、モデルの API は呼ばない。
"""

from dataclasses import dataclass, field

import pytest

from rag_engine.generation import crag_generation
from rag_engine.generation.answer_models import AnswerContext
from rag_engine.generation.grounded import CheckedItem
from rag_engine.models.llm import GroundedCitation, GroundedItem

SPANS = [
    {"evidence_id": "E1", "label": "E1", "source_id": "P1", "text": "手順Aを行います。続けて手順Bを行います。"},
    {"evidence_id": "E2", "label": "E2", "source_id": "P2", "text": "項目Cを入力します。"},
    {"evidence_id": "E3", "label": "E3", "source_id": "P3", "text": "画面Dで結果を確かめます。"},
]
# 回答できる部分の候補（観点・写し）。先頭から n 件を、評価が写しを確かめた部分にする。
PARTS = [
    ("requested_result", "P1", "手順Aを行います。"),
    ("procedure", "P2", "項目Cを入力します。"),
    ("verification", "P3", "画面Dで結果を確かめます。"),
]


@dataclass
class Current:
    checked: list
    timing: dict = field(default_factory=lambda: {"audit": "none", "semantic_audit_policy": {"mode": "diagnostic_only"}})
    summary: str = "合成の要約。"
    applications: list = field(default_factory=lambda: ["合成の当てはめ"])


def claim(text, span, quote, *, support="unknown"):
    item = GroundedItem(kind="operation", text=text, citations=[GroundedCitation(evidence_id=span["evidence_id"], lines="1", quote=quote)])
    return CheckedItem(item, span, review_assessment={"effective_support": support})


def claims():
    return [
        claim("手順Aを行う。", SPANS[0], "手順Aを行います。"),
        claim("手順Bを行う。", SPANS[0], "続けて手順Bを行います。"),
        claim("項目Cを入力する。", SPANS[1], "項目Cを入力します。"),
    ]


def view(verified, *, coverage_complete=False):
    """先頭から verified 件の部分を、写しを確かめた部分（supported）にした評価の生成の入口の見え方。"""
    checks = [
        {"aspect": aspect, "status": "supported" if coverage_complete or index < verified else "unknown", "evidence": []}
        for index, (aspect, _source, _quote) in enumerate(PARTS)
    ]
    for index, (_aspect, source, quote) in enumerate(PARTS):
        if checks[index]["status"] == "supported":
            checks[index]["evidence"] = [{"source_id": source, "quote": quote}]
    parts = [
        {
            "aspect": aspect,
            "claim": quote,
            "status": "supported" if index < verified else "unknown",
            "evidence": [{"source_id": source, "quote": quote}],
        }
        for index, (aspect, source, quote) in enumerate(PARTS)
    ]
    assessment = {"grade_execution": "completed", "aspect_checks": checks, "answerable_parts": parts}
    return crag_generation.generation_view(assessment, SPANS, AnswerContext(records=[], text=""))


def project(verified, *, audited=False, entries=None):
    current = Current(checked=entries if entries is not None else claims())
    return crag_generation.project_partial_answer(current, view(verified), SPANS, audited=audited)


def published_texts(current):
    return [entry.item.text for entry in current.checked if entry.published]


def mark_codes(current):
    return {entry.item.text: [mark["code"] for mark in entry.marks] for entry in current.checked if entry.published}


def test_one_verified_part_keeps_every_claim_and_marks_only_the_unbound_ones():
    out, record = project(1)
    assert view(1)["generation_decision"] == crag_generation.GATE_VERIFIED_PART
    assert published_texts(out) == ["手順Aを行う。", "手順Bを行う。", "項目Cを入力する。"]
    # 確かめた写し（手順A）を引く説明は印なし、ほかは本文に残して unassessed の印。
    assert mark_codes(out) == {"手順Aを行う。": [], "手順Bを行う。": ["unassessed"], "項目Cを入力する。": ["unassessed"]}
    assert out.checked[1].marks[0]["detail"] == crag_generation.CRAG_UNBOUND_CLAIM_DETAIL
    # 要約・当てはめは置き換えない（監査なしの警告は通常の検査が付ける）。
    assert (out.summary, out.applications) == ("合成の要約。", ["合成の当てはめ"])
    assert record == {
        "applied": True,
        "omitted_claims": [],
        "bound_claims": 1,
        "marked_claims": 2,
        "added_quotes": 0,
        "summary_replaced": False,
        "retained_reason": "unaudited_partial_answer_keeps_claims_with_marks",
    }


def test_zero_verified_parts_leave_the_answer_unchanged():
    entries = claims()
    current = Current(checked=entries)
    out, record = crag_generation.project_partial_answer(current, view(0), SPANS, audited=False)
    assert out is current and out.checked is entries
    assert record == {"applied": False, "reason": crag_generation.GATE_UNVERIFIED_PART}
    assert all(not entry.marks for entry in entries)


def test_claim_citing_a_verified_quote_stays_unmarked_and_the_uncited_quote_is_added_as_direct_source():
    # 写しの一部だけを引く説明・別の出典で確かめた写しを引く説明も、確かめた写しに結び付く（幅と空白だけを同一視）。
    partial_quote = claim("手順Aを行う（一部）。", SPANS[0], "手順Ａを 行います")
    other_source = claim("手順Bを行う。", SPANS[0], "続けて手順Bを行います。")
    other_source.also.append(claim("手順Bを行う。", SPANS[1], "項目Cを入力します。"))
    out, record = project(2, entries=[partial_quote, other_source])
    assert mark_codes(out) == {"手順Aを行う（一部）。": [], "手順Bを行う。": []}
    # 写しの全体を引く説明が無い写し（E1）だけを、確かめた直接の原文として足す（説明は外さない）。
    added = [entry for entry in out.checked if entry.quote_only]
    assert [(entry.span["evidence_id"], entry.item.quote) for entry in added] == [("E1", "手順Aを行います。")]
    assert (record["bound_claims"], record["marked_claims"], record["added_quotes"]) == (2, 0, 1)


def test_claim_that_already_has_an_unverified_mark_is_not_marked_twice():
    entries = claims()
    entries[1].mark("unsupported", "合成の不支持")
    out, record = project(1, entries=entries)
    assert mark_codes(out)["手順Bを行う。"] == ["unsupported"]
    assert record["marked_claims"] == 1


@pytest.mark.parametrize("more", [1, 2, 3], ids=["0to1", "1to2", "2to3"])
def test_more_verified_parts_never_remove_claims(more):
    fewer, _ = project(more - 1)
    after, _ = project(more)
    assert published_texts(after) == published_texts(fewer) == ["手順Aを行う。", "手順Bを行う。", "項目Cを入力する。"]
    if more > 1:
        # 確かめた部分が 1 件以上の回どうしでは、印の付いた説明が増えない。
        marked = {text for text, codes in mark_codes(after).items() if codes}
        assert marked <= {text for text, codes in mark_codes(fewer).items() if codes}


def test_complete_coverage_is_not_projected():
    current = Current(checked=claims())
    out, record = crag_generation.project_partial_answer(current, view(3, coverage_complete=True), SPANS, audited=False)
    assert out is current and record == {"applied": False}


def test_adopted_semantic_audit_keeps_only_supported_claims_within_verified_parts():
    # 意味監査を採用したとき（diagnostic_only でない）の扱いは変えない。
    entries = [
        claim("手順Aを行う。", SPANS[0], "手順Aを行います。", support="supported"),
        claim("手順Bを行う。", SPANS[0], "続けて手順Bを行います。", support="supported"),
        claim("手順Aを行う（不支持）。", SPANS[0], "手順Aを行います。", support="unsupported"),
    ]
    current = Current(checked=entries, timing={"audit": "standard"})
    out, record = crag_generation.project_partial_answer(current, view(1), SPANS, audited=True)
    assert [entry.item.text for entry in out.checked] == ["手順Aを行う。"]
    assert [row["text"] for row in record["omitted_claims"]] == ["手順Bを行う。", "手順Aを行う（不支持）。"]
    assert record["summary_replaced"] and out.applications == []
