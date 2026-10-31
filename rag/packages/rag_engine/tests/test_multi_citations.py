"""1 つの説明に複数の出典を付ける（#3526）。

規則を定めた条項と、それを説明する手順書が同じ内容を述べるとき、説明を 1 回だけ出し、出典を文書ごとに並べる。
各出典は出典ごとに原文照合・決定的な検査・監査に掛け、通らない出典だけを外す（説明は残す）。条で構成された
文書の条を出典にした説明も、設定した監査の型に従う。
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from test_grounded_answer import FakeModel, audit, context, draft, ids, marks, observed_audit, record

from rag_engine.config import get_settings
from rag_engine.dependencies import AnswerDependencies, bind_dependencies
from rag_engine.generation import grounded
from rag_engine.generation.answering import synthesize_grounded_answer
from rag_engine.models.llm import GroundedFactAudit, GroundedItem
from rag_engine.retrieval.article_catalog import ARTICLE_UNIT_KEY

QUESTION = "申請書の提出期限を教えてください"
RULE = "第三条\n申請書は、事由の発生した日から三十日以内に提出しなければならない。"
GUIDE = "提出の手順\n申請書は、事由の発生した日から三十日以内に提出しなければならない。\n窓口で受付印を受けます。"
CLAIM = "申請書は、事由の発生した日から三十日以内に提出します。"
QUOTE = "申請書は、事由の発生した日から三十日以内に提出しなければならない。"


def _context(*, article: bool = False):
    rule = record(0, RULE, "総則", page=1, source="規則A.xml")
    if article:
        rule = replace(rule, metadata={**rule.metadata, ARTICLE_UNIT_KEY: True})
    return context(rule, record(1, GUIDE, "提出の手順", page=4, source="手順書B.pdf"))


def _cite(*pairs: tuple[str, str], text: str = CLAIM, **extra) -> dict:
    return {"kind": "rule", "text": text, "citations": [{"evidence_id": eid, "quote": quote} for eid, quote in pairs], **extra}


def _run(model, ctx, mode=grounded.AUDIT_MODE_NONE):
    def unused(*a, **k):
        return (_ for _ in ()).throw(AssertionError("unexpected I/O"))

    settings = replace(get_settings(), answer_audit_mode=mode)
    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        return synthesize_grounded_answer(QUESTION, ctx, settings, image_prompt_mode="text_only").response


def _labels(ctx):
    labels = ids(ctx, QUESTION)
    return labels[RULE], labels[GUIDE]


def _audit_inputs(model):
    return [json.loads(prompt) for name, prompt in model.prompts if name in {"GroundedAudit", "GroundedFactAudit"}]


def _citation_lines(text: str) -> list[str]:
    prefix = grounded.answer_labels().citation_prefix
    return [line for line in text.splitlines() if line.startswith(prefix)]


def _sources(result) -> set[str]:
    return {entry["source"] for entry in result.cited_evidence}


def test_schema_lists_citations_and_the_model_does_not_write_quotes():
    schema = GroundedItem.model_json_schema()
    citation = schema["$defs"]["GroundedCitation"]
    assert schema["properties"]["citations"]["items"] == {"$ref": "#/$defs/GroundedCitation"}
    assert set(citation["properties"]) == {"evidence_id", "lines"}
    assert citation["required"] == ["evidence_id"]
    # 検査の工程は最初の出典を読む。
    item = GroundedItem.model_validate(_cite(("E1", "原文1"), ("E2", "原文2")))
    assert (item.evidence_id, item.quote) == ("E1", "原文1")
    assert [cited.evidence_id for cited in item.single_citations()] == ["E1", "E2"]
    assert item.with_citation(quote="差し替え").citations[1].quote == "原文2"


def test_one_claim_supported_by_two_documents_is_shown_once_with_a_citation_line_per_document():
    ctx = _context()
    rule, guide = _labels(ctx)
    model = FakeModel([draft(_cite((rule, QUOTE), (guide, QUOTE)), summary="")], [])

    result = _run(model, ctx)

    assert result.answer_text.count(CLAIM) == 1
    lines = _citation_lines(result.answer_text)
    assert len(lines) == 2 and "規則A.xml" in lines[0] and "手順書B.pdf" in lines[1]
    # 出典行ごとの根拠と、回答で引用した箇所（MCP の used_in_answer の元）に両方の文書が入る。
    citation_lines = result.generation_trace["citation_lines"]
    assert [ref["evidence_ids"] for ref in citation_lines] == [[rule], [guide]]
    assert _sources(result) == {"規則A.xml", "手順書B.pdf"}
    assert {entry["answer_use"] for entry in result.cited_evidence} == {"confirmed"}
    item = result.generation_trace["rounds"][0]["items"][0]
    assert [cited["evidence_id"] for cited in item["also"]] == [guide]
    assert not marks(result)


def test_a_citation_whose_quote_is_not_in_the_evidence_is_dropped_and_the_claim_kept():
    ctx = _context()
    rule, guide = _labels(ctx)
    model = FakeModel([draft(_cite((rule, QUOTE), (guide, "窓口で申請書を郵送します。")))], [])

    result = _run(model, ctx)

    assert result.answer_text.count(CLAIM) == 1
    assert len(_citation_lines(result.answer_text)) == 1
    assert _sources(result) == {"規則A.xml"}
    assert CLAIM not in marks(result)
    assert all(reasons == ["summary_unaudited"] for reasons in marks(result).values())
    dropped = result.generation_trace["rounds"][0]["dropped"]
    assert [entry.get("citation_dropped") for entry in dropped] == [True]
    assert all(entry["ignored"] for entry in dropped)
    # 外した出典は是正の指摘・要約の保留の理由にしない。
    assert result.answer_text.startswith("集計表出力")


def test_a_citation_outside_the_request_plan_is_dropped_without_holding_the_summary():
    ctx = _context()
    rule, guide = _labels(ctx)
    plan = {
        "request_id": "Q1",
        "evidence_scope": [
            {"evidence_id": rule, "target": "申請書", "relation": "direct"},
            {"evidence_id": guide, "target": "受付印", "relation": "unrelated"},
        ],
        "evidence_ids": [rule],
    }
    value = {**draft(_cite((rule, QUOTE), (guide, QUOTE), request_id="Q1")), "request_coverage": [plan]}
    model = FakeModel([value], [])

    result = _run(model, ctx)

    assert len(_citation_lines(result.answer_text)) == 1
    assert _sources(result) == {"規則A.xml"}
    assert grounded.answer_labels().neutral_summary not in result.answer_text
    dropped = result.generation_trace["rounds"][0]["dropped"]
    assert all(entry.get("citation_dropped") and "scope_excluded" not in entry for entry in dropped)


def test_when_no_citation_checks_out_the_first_is_kept_with_its_mark():
    ctx = _context()
    rule, guide = _labels(ctx)
    model = FakeModel([draft(_cite((rule, "存在しない原文です。"), (guide, "これも存在しない原文です。")))], [])

    result = _run(model, ctx)

    assert result.answer_text.split(grounded.answer_labels().marked_section)[0].count(CLAIM) == 1
    assert marks(result)[CLAIM] == ["quote_unmatched"]
    dropped = result.generation_trace["rounds"][0]["dropped"]
    assert [bool(entry.get("citation_dropped")) for entry in dropped] == [False, True]


def _audit_by_evidence(unsupported: set[str]):
    """出典の根拠ごとに支持・不支持を返す監査（出典ごとの index で答える）。"""

    def respond(prompt: str) -> dict:
        items = json.loads(prompt)["items"]
        reviews = [
            (entry["index"], "unsupported" if entry["evidence_id"] in unsupported else "supported", "matched", "") for entry in items
        ]
        value = audit(*reviews, requests=[("Q1", "addressed")])
        return {key: value[key] for key in value if key in GroundedFactAudit.model_fields}

    return respond


def test_each_citation_is_audited_but_raw_unsupported_does_not_drop_it():
    ctx = _context()
    rule, guide = _labels(ctx)
    model = FakeModel([draft(_cite((rule, QUOTE), (guide, QUOTE)))], [_audit_by_evidence({guide})])

    result = _run(model, ctx, grounded.AUDIT_MODE_STANDARD)

    inputs = _audit_inputs(model)[0]
    assert sorted(entry["evidence_id"] for entry in inputs["items"]) == sorted([rule, guide])
    assert len({entry["index"] for entry in inputs["items"]}) == 2
    assert _sources(result) == {"規則A.xml", "手順書B.pdf"}
    assert result.answer_text.count(CLAIM) == 1
    assert not marks(result)
    assert [row["support"] for row in observed_audit(result)["reviews"]] == ["supported", "unsupported"]


def test_raw_unsupported_first_citation_is_retained_beside_supported_second():
    ctx = _context()
    rule, guide = _labels(ctx)
    model = FakeModel([draft(_cite((rule, QUOTE), (guide, QUOTE)))], [_audit_by_evidence({rule})])

    result = _run(model, ctx, grounded.AUDIT_MODE_STANDARD)

    assert _sources(result) == {"規則A.xml", "手順書B.pdf"}
    assert result.answer_text.count(CLAIM) == 1
    assert not marks(result)
    assert [row["support"] for row in observed_audit(result)["reviews"]] == ["unsupported", "supported"]
    dropped = result.generation_trace["rounds"][0]["dropped"]
    assert not any(entry.get("citation_dropped") and entry["evidence_id"] == rule for entry in dropped)


def test_article_citations_do_not_enable_an_audit_in_none_mode():
    ctx = _context(article=True)
    rule, guide = _labels(ctx)
    other = "窓口で受付印を受けます。"
    model = FakeModel([draft(_cite((rule, QUOTE)), _cite((guide, other), text=other))], [])

    result = _run(model, ctx)

    assert [name for name, _prompt in model.prompts] == ["GroundedDraftWire"]
    assert not _audit_inputs(model)
    assert other not in marks(result)
    trace = result.generation_trace
    assert trace["audit_mode"] == grounded.AUDIT_MODE_NONE
    assert trace["rounds"][0]["audit_calls"] == 0
    semantic = trace["rounds"][0]["validation_dimensions"]["claim_support"]
    assert semantic["execution"] == "not_run"
    assert all(row["status"] == "unknown" for row in semantic["items"])


@pytest.mark.parametrize("mode", [grounded.AUDIT_MODE_STANDARD, grounded.AUDIT_MODE_DETAILED])
def test_enabled_audit_modes_review_both_article_and_other_sources(mode):
    ctx = _context(article=True)
    rule, guide = _labels(ctx)
    other = "窓口で受付印を受けます。"
    model = FakeModel([draft(_cite((rule, QUOTE)), _cite((guide, other), text=other))], [_audit_by_evidence({rule})])
    result = _run(model, ctx, mode)
    inputs = _audit_inputs(model)[0]
    assert {entry["evidence_id"] for entry in inputs["items"]} == {rule, guide}
    assert "unsupported" not in marks(result).get(CLAIM, [])
    assert "unsupported" in [row["support"] for row in observed_audit(result)["reviews"]]
    assert other not in marks(result)
    assert result.generation_trace["rounds"][0]["audit_calls"] == 1


def test_without_article_citations_the_audit_mode_none_calls_no_audit():
    ctx = _context()
    rule, _guide = _labels(ctx)
    model = FakeModel([draft(_cite((rule, QUOTE)))], [])

    result = _run(model, ctx)

    assert [name for name, _prompt in model.prompts] == ["GroundedDraftWire"]
    assert "article_audit_items" not in result.generation_trace["rounds"][0]["timing"]


def test_a_citation_quoting_only_the_section_heading_is_dropped():
    """節の見出しだけを引いた出典は説明を支えないので外し、本文を引いた出典を主にする（#3541）。"""
    heading = "第三条 提出期限"
    rule = record(0, f"{heading}\n{QUOTE}", heading, page=1, source="規則A.xml")
    rule = replace(rule, metadata={**rule.metadata, "section_path_sources": ["section_header"] * len(rule.metadata["section_path"])})
    ctx = context(rule, record(1, GUIDE, "提出の手順", page=4, source="手順書B.pdf"))
    labels = ids(ctx, QUESTION)
    model = FakeModel([draft(_cite((labels[rule.text], heading), (labels[GUIDE], QUOTE)))], [])

    result = _run(model, ctx)

    assert _sources(result) == {"手順書B.pdf"}
    dropped = result.generation_trace["rounds"][0]["dropped"]
    assert [entry["reason"] for entry in dropped if entry.get("citation_dropped")] == [grounded.HEADING_ONLY_REASON]


def test_a_claim_citing_only_the_section_heading_is_marked_without_denial_of_source_authenticity():
    """出典が見出しだけの説明は確かめた説明にしない: 原文に無い引用と同じ印を付け、出典行を付けない（#3544）。"""
    heading = "第三条 提出期限"
    rule = record(0, f"{heading}\n{QUOTE}", heading, page=1, source="規則A.xml")
    rule = replace(rule, metadata={**rule.metadata, "section_path_sources": ["section_header"] * len(rule.metadata["section_path"])})
    ctx = context(rule)
    labels = ids(ctx, QUESTION)
    model = FakeModel([draft(_cite((labels[rule.text], heading)))], [])

    result = _run(model, ctx)

    assert marks(result)[CLAIM] == ["citation_heading_only"]
    citation = result.generation_trace["rounds"][0]["validation_dimensions"]["citation_validity"]["items"][0]["citations"][0]
    assert citation["status"] == "matched" and citation["reason_code"] == "citation_heading_only"
    assert not _citation_lines(result.answer_text)
    assert {entry["answer_use"] for entry in result.cited_evidence} <= {"reference"}
    dropped = result.generation_trace["rounds"][0]["dropped"]
    # 是正の指摘（ignored ではない）に直し方を返す。
    assert [(entry["reason"], entry.get("kept"), entry.get("ignored")) for entry in dropped] == [(grounded.HEADING_ONLY_REASON, True, None)]


def test_a_claim_citing_the_body_line_under_the_heading_is_confirmed():
    heading = "第三条 提出期限"
    rule = record(0, f"{heading}\n{QUOTE}", heading, page=1, source="規則A.xml")
    rule = replace(rule, metadata={**rule.metadata, "section_path_sources": ["section_header"] * len(rule.metadata["section_path"])})
    ctx = context(rule)
    labels = ids(ctx, QUESTION)
    model = FakeModel([draft(_cite((labels[rule.text], QUOTE)))], [])

    result = _run(model, ctx)

    assert CLAIM not in marks(result)
    assert all(reasons == ["summary_unaudited"] for reasons in marks(result).values())
    assert len(_citation_lines(result.answer_text)) == 1


def test_heading_only_quotes_require_structural_heading_provenance():
    span = {"section_path": ["第2章 手続", "2.8 期限が重なる場合"], "section_path_sources": ["section_header", "section_header"]}
    assert grounded.heading_only_quote("2.8 期限が重なる場合", span)
    assert grounded.heading_only_quote("期限が重なる場合", span)
    # 見出しに番号・記号だけを足した 1 行も見出し。`>` でつないだ見出しも同じ。
    assert grounded.heading_only_quote(
        "【2.8 期限が重なる場合】", {"section_path": ["期限が重なる場合"], "section_path_sources": ["section_header"]}
    )
    assert grounded.heading_only_quote("第2章 手続 > 2.8 期限が重なる場合", span)
    # 見出しに無い文字を含む行・複数行は本文。
    assert not grounded.heading_only_quote("期限が重なる場合は、先の期限を優先する。", span)
    assert not grounded.heading_only_quote("2.8 期限が重なる場合\n先の期限を優先する。", span)
    assert not grounded.heading_only_quote("期限が重なる場合", {"section_path": []})


def test_one_item_same_document_and_page_keeps_all_confirmed_chunks_in_one_line():
    first_text = "合成の項目 A。\n合成の保存操作を実行します。"
    second_text = "合成の項目 B。\n合成の保存操作を実行します。"
    quote = "合成の保存操作を実行します。"
    ctx = context(
        record(0, first_text, "合成 A", page=1, source="document-a.pdf"),
        record(1, second_text, "合成 B", page=1, source="document-a.pdf"),
    )
    labels = ids(ctx, QUESTION)
    first, second = labels[first_text], labels[second_text]
    model = FakeModel([draft(_cite((first, quote), (second, quote), text=quote), summary="")], [])
    result = _run(model, ctx)
    assert result.answer_text.count(quote) == 1
    assert len(_citation_lines(result.answer_text)) == 1
    assert result.generation_trace["citation_lines"][0]["evidence_ids"] == [first, second]
