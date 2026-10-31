"""会話の添付と資料の規則を組み合わせる説明と、要求の根拠計画（#3228）。

添付の表の行と資料の規則をつないで計算する説明は、両者の関係を明記した原文が根拠に無い。添付を資料の操作と
同じに対象の照合（direct / linked / unrelated）に掛けると、linked の原文が無いとして説明がすべて外れ、根拠が
渡っていても拒答になっていた。添付は利用者が明示した入力なので照合に掛けず、作り直しても計画が決まらない説明は
外さずに印を付けて残す（#2738）。計画がはっきり unrelated とした根拠の説明は今までどおり外す。
"""

from __future__ import annotations

from dataclasses import replace

from test_grounded_answer import FakeModel, context, ids, marks, record

from rag_engine.config import get_settings
from rag_engine.dependencies import AnswerDependencies, bind_dependencies
from rag_engine.generation.answering import synthesize_grounded_answer
from rag_engine.generation.grounded import _outside_request_plan, _request_plan_errors
from rag_engine.models.llm import GroundedCitation, GroundedDraft, GroundedItem
from rag_engine.retrieval.attachment_evidence import ATTACHMENT_EVIDENCE_KEY

QUESTION = "添付の作業表の作業費は合計いくらですか？"
RULE = "作業費は一件あたり三百円とする。"
ROWS = "行 1: 作業: A / 件数: 2\n行 2: 作業: B / 件数: 1"
RULE_ITEM = "作業費は一件あたり三百円です。"
ROWS_ITEM = "作業表は A が 2 件、B が 1 件です。"


def _attachment(index: int, text: str):
    attached = record(index, text, "作業表", source="作業表.csv")
    attached.metadata[ATTACHMENT_EVIDENCE_KEY] = True
    return attached


def _ctx():
    return context(record(0, RULE, "作業費"), _attachment(1, ROWS))


def _draft(labels: dict[str, str], scope: list[dict]) -> dict:
    rule, rows = labels[RULE], labels[ROWS]
    return {
        "request_coverage": [{"request_id": "Q1", "evidence_ids": [rows, rule], "evidence_scope": scope}],
        "items": [
            {"kind": "rule", "request_id": "Q1", "text": RULE_ITEM, "citations": [{"quote": RULE, "evidence_id": rule}]},
            {"kind": "rule", "request_id": "Q1", "text": ROWS_ITEM, "citations": [{"quote": "作業: A / 件数: 2", "evidence_id": rows}]},
        ],
        "summary": "作業費の合計は九百円です。",
        "confidence": "high",
    }


def _run(drafts: list[dict]):
    model = FakeModel(drafts, [])

    def unused(*args, **kwargs):
        raise AssertionError("外部呼び出しは不要")

    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        result = synthesize_grounded_answer(
            QUESTION, _ctx(), replace(get_settings(), answer_audit_mode="none"), image_prompt_mode="text_only"
        ).response
    return result, model


def _spans() -> list[dict]:
    from rag_engine.generation.answering import _grounded_spans

    return _grounded_spans(QUESTION, _ctx(), (), None)


def test_attachment_spans_are_marked_and_skip_the_target_matching():
    spans = _spans()
    labels = {span["text"]: span["evidence_id"] for span in spans}
    assert [span["conversation_attachment"] for span in spans if span["text"] == ROWS] == [True]
    assert [span["conversation_attachment"] for span in spans if span["text"] == RULE] == [False]
    # 添付を linked（関係の原文なし）にしても、添付には照合を求めない。資料の規則は今までどおり照合が要る。
    draft = GroundedDraft.model_validate(
        _draft(labels, [{"evidence_id": labels[ROWS], "target": "作業表", "relation": "linked", "link_quote": ""}])
    )
    errors = _request_plan_errors(draft, spans)
    assert errors == [f"Q1: {labels[RULE]} の対象の照合は1件必要です。"]
    rows_span = next(span for span in spans if span["text"] == ROWS)
    rows_item = GroundedItem(kind="rule", request_id="Q1", text=ROWS_ITEM, citations=[GroundedCitation(evidence_id=labels[ROWS])])
    assert not _outside_request_plan(rows_item, rows_span, draft, spans)


def test_unsettled_plan_keeps_the_explanations_with_a_mark_instead_of_refusing():
    labels = ids(_ctx(), QUESTION)
    invalid = _draft(labels, [{"evidence_id": labels[ROWS], "target": "作業表", "relation": "linked", "link_quote": ""}])
    # 作り直しても資料の規則の照合が抜けたまま（実際に観測した形）。
    result, model = _run([invalid, invalid])

    assert "invalid_request_plan" in model.prompts[1][1]
    assert RULE_ITEM in result.answer_text and ROWS_ITEM in result.answer_text
    assert result.envelope["outcome"] != "insufficient_evidence"
    # 計画が決まらなかった規則の説明は、本文に残して「確かめが終わっていない」の印。添付の説明には印を付けない。
    assert marks(result).get(RULE_ITEM) == ["unassessed"]
    assert ROWS_ITEM not in marks(result)


def test_an_explicitly_unrelated_evidence_is_still_excluded():
    labels = ids(_ctx(), QUESTION)
    scope = [
        {"evidence_id": labels[ROWS], "target": "作業表", "relation": "direct", "link_quote": ""},
        {"evidence_id": labels[RULE], "target": "別の作業", "relation": "unrelated", "link_quote": ""},
        {"evidence_id": labels[RULE], "target": "別の作業の費用", "relation": "linked", "link_quote": ""},
    ]
    # 同じ根拠への判断が食い違う（unrelated と linked）計画は作り直しても決まらない。unrelated の判断は守る。
    unsettled = _draft(labels, scope)
    result, _model = _run([unsettled, unsettled])

    assert ROWS_ITEM in result.answer_text
    assert RULE_ITEM not in result.answer_text


def test_a_settled_plan_still_excludes_evidence_outside_it():
    labels = ids(_ctx(), QUESTION)
    settled = _draft(
        labels,
        [
            {"evidence_id": labels[ROWS], "target": "作業表", "relation": "direct", "link_quote": ""},
            {"evidence_id": labels[RULE], "target": "別の作業", "relation": "unrelated", "link_quote": ""},
        ],
    )
    settled["request_coverage"][0]["evidence_ids"] = [labels[ROWS]]
    result, model = _run([settled])

    assert len([schema for schema, _prompt in model.prompts if schema == "GroundedDraftWire"]) == 1
    assert ROWS_ITEM in result.answer_text
    assert RULE_ITEM not in result.answer_text
