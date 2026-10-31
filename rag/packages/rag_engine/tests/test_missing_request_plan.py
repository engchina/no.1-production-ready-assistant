"""生成が要求の根拠計画を返さなかった草稿と、印の付いた説明の節（#3394）。

生成は、要求ごとの根拠計画（request_coverage）を items より先に書く。根拠の多い概要の質問で、モデルが計画を空の
配列で返し、草稿の全体を作り直しても空のままだと、計画の外の説明として全段落に「確かめが終わっていない」の印が
付き、それが「資料により確認できた事項」の見出しの下に並んでいた。

- 計画だけが無い草稿は、草稿の全体を作り直さず、説明が引いた根拠だけを渡して計画を立て直す（本文の生成と根拠の
  照合を分ける。章節の欠けた根拠の事前の計画と同じ呼び出し）。
- 立て直した計画が照合しなかった根拠の説明は外さずに印を付けて残し、unrelated とした根拠の説明は外す。
- 印の付いた説明（確かめていない・注意が要る）は「資料により確認できた事項」に入れず、別の節に置く。
"""

from __future__ import annotations

from dataclasses import replace

from test_grounded_answer import FakeModel, context, ids, marks, record

from rag_engine.config import get_settings
from rag_engine.dependencies import AnswerDependencies, bind_dependencies
from rag_engine.generation.answer_labels import answer_labels
from rag_engine.generation.answering import synthesize_grounded_answer

QUESTION = "資料の概要を教えてください"
INTRO = "この文書は検索の試験のために作成した架空の資料です。"
OUTLINE = "第1章は利用の開始、第2章は日次の点検の手順を説明します。"
INTRO_ITEM = "文書 A は検索の試験のための架空の資料です。"
OUTLINE_ITEM = "文書 B は利用の開始と日次の点検の手順を説明しています。"


def _ctx():
    return context(record(0, INTRO, "はじめに", source="文書A.pdf"), record(1, OUTLINE, "構成", page=3, source="文書B.pdf"))


def _draft(labels: dict[str, str]) -> dict:
    # 計画を空の配列で返した草稿（観測した形）。
    return {
        "request_coverage": [],
        "items": [
            {"kind": "rule", "request_id": "Q1", "text": INTRO_ITEM, "citations": [{"quote": INTRO, "evidence_id": labels[INTRO]}]},
            {"kind": "rule", "request_id": "Q1", "text": OUTLINE_ITEM, "citations": [{"quote": OUTLINE, "evidence_id": labels[OUTLINE]}]},
        ],
        "summary": "",
        "confidence": "high",
    }


def _plan(*scopes: tuple[str, str]) -> dict:
    return {
        "request_coverage": [
            {
                "request_id": "Q1",
                "evidence_scope": [
                    {"evidence_id": label, "target": "資料", "relation": relation, "link_quote": ""} for label, relation in scopes
                ],
                "evidence_ids": [label for label, relation in scopes if relation != "unrelated"],
            }
        ]
    }


def _run(drafts: list, plans: list):
    model = FakeModel(drafts, plans)

    def unused(*args, **kwargs):
        raise AssertionError("外部呼び出しは不要")

    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        result = synthesize_grounded_answer(
            QUESTION, _ctx(), replace(get_settings(), answer_audit_mode="none"), image_prompt_mode="text_only"
        ).response
    return result, model


def _schemas(model: FakeModel) -> list[str]:
    return [schema for schema, _prompt in model.prompts]


def test_missing_plan_is_planned_from_the_cited_evidence_without_regenerating_the_draft():
    labels = ids(_ctx(), QUESTION)
    result, model = _run([_draft(labels)], [_plan((labels[INTRO], "direct"), (labels[OUTLINE], "direct"))])

    # 草稿の全体は作り直さず、計画だけを立て直す。計画には説明が引いた根拠を渡す。
    assert _schemas(model) == ["GroundedDraftWire", "RequestScopePlan"]
    plan_prompt = model.prompts[1][1]
    assert INTRO in plan_prompt and OUTLINE in plan_prompt
    assert INTRO_ITEM in result.answer_text and OUTLINE_ITEM in result.answer_text
    assert marks(result) == {}
    assert result.envelope["outcome"] != "insufficient_evidence"
    assert answer_labels().rules_section in result.answer_text


def test_evidence_the_plan_marks_unrelated_is_excluded_and_the_rest_is_kept():
    labels = ids(_ctx(), QUESTION)
    result, _model = _run([_draft(labels)], [_plan((labels[INTRO], "direct"), (labels[OUTLINE], "unrelated"))])

    assert INTRO_ITEM in result.answer_text
    assert OUTLINE_ITEM not in result.answer_text
    assert marks(result) == {}


def test_evidence_the_plan_does_not_decide_is_kept_with_a_mark():
    labels = ids(_ctx(), QUESTION)
    result, _model = _run([_draft(labels)], [_plan((labels[INTRO], "direct"))])

    # 計画が照合しなかった根拠の説明は外さない（根拠と矛盾した説明ではない。#2738）。
    assert INTRO_ITEM in result.answer_text and OUTLINE_ITEM in result.answer_text
    assert marks(result) == {OUTLINE_ITEM: ["unassessed"]}


def test_marked_explanations_are_not_shown_under_the_confirmed_heading():
    labels = ids(_ctx(), QUESTION)
    result, _model = _run([_draft(labels)], [{"request_coverage": []}])

    # 立て直しても計画が無い。説明は残して印を付けるが、「資料により確認できた事項」には入れない。
    assert set(marks(result)) == {INTRO_ITEM, OUTLINE_ITEM}
    text = result.answer_text
    labels_ = answer_labels()
    assert labels_.rules_section not in text
    marked = text.split(labels_.marked_rules_section + "\n\n", 1)[1]
    assert marked.startswith(labels_.marked_rules_section_note)
    assert INTRO_ITEM in marked and OUTLINE_ITEM in marked


def test_confirmed_and_marked_explanations_are_in_separate_sections():
    labels = ids(_ctx(), QUESTION)
    result, _model = _run([_draft(labels)], [_plan((labels[INTRO], "direct"))])

    text = result.answer_text
    labels_ = answer_labels()
    confirmed = text.split(labels_.rules_section + "\n\n", 1)[1].split("\n\n", 1)[0]
    marked = text.split(labels_.marked_rules_section + "\n\n", 1)[1].split("\n\n", 1)[0]
    assert INTRO_ITEM in confirmed and OUTLINE_ITEM not in confirmed
    assert OUTLINE_ITEM in marked
