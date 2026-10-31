"""根拠の出所の分離（#2792）。資料の出典を付けるのは検索結果の原文と照合できた説明だけ。

業務ガイド（運用者が設定した答え方の指示）の文を、引いた根拠に無いまま書いた説明は資料の説明にしない。業務ガイドの
窓口・引き継ぐ条件は、システムが業務ガイドの設定から「業務ガイドの案内」の節に出す。
"""

from __future__ import annotations

from dataclasses import replace

from test_grounded_answer import FakeModel, draft, item, marks
from test_grounded_answer import audit as make_audit
from test_guide_contracts import audit, coverage, review
from test_guide_step_fulfillment import GUIDE, STEP_A, STEP_B, _ab_context, _ids, _knowledge, _run

from rag_engine.generation import grounded
from rag_engine.models.llm import GroundedCitation, GroundedItem
from rag_engine.retrieval.guide_steps import GuidePlan

# 合成の業務ガイドの文（手順の目的）。資料（STEP_A・STEP_B）に無い方針を含む。
GUIDE_PURPOSE = "設定 A の値を入力する。値と区分の対応は資料に無いため、区分を推測せず窓口 X に確認するよう補足する。"
GUIDE_TEXT_CLAIM = "値と区分の対応は資料に無いため、区分を推測せず窓口 X に確認します。"


def _entry(text: str, quote: str = STEP_A) -> grounded.CheckedItem:
    return grounded.CheckedItem(
        GroundedItem(kind="rule", text=text, citations=[GroundedCitation(quote=quote, evidence_id="E1")]),
        span={"text": STEP_A, "evidence_id": "E1", "source": "操作説明.pdf", "page": 10, "function": ["設定画面"]},
    )


def test_guide_text_without_evidence_is_marked_but_paraphrase_of_evidence_is_not():
    guide = [GUIDE_PURPOSE]
    copied = _entry(GUIDE_TEXT_CLAIM)
    # 業務ガイドの文は資料の言い回しを写すことが多い。根拠にある語の言い換えは業務ガイドの文にしない。
    paraphrase = _entry("設定画面で、設定 A の値を入力します。")
    gap = grounded.CheckedItem(GroundedItem(kind="gap", text=GUIDE_TEXT_CLAIM))

    assert grounded.mark_guide_text([copied, paraphrase, gap], guide) == 1
    assert [mark["code"] for mark in copied.marks] == ["guide_text"]
    assert copied.state == "unverified" and not copied.confirmed
    assert paraphrase.marks == [] and gap.marks == []


def test_unsourced_items_lose_the_document_citation_and_get_their_own_section():
    copied = _entry(GUIDE_TEXT_CLAIM)
    grounded.mark_guide_text([copied], [GUIDE_PURPOSE])
    sourced = _entry("設定画面で設定 A の値を入力します。")

    assert grounded.detach_unsourced([copied, sourced]) == 1
    rendered = grounded.render_answer("要約", [sourced, copied])

    labels = grounded.answer_labels()
    body, _sep, unsourced = rendered.text.partition(labels.unsourced_section)
    assert "設定 A の値を入力します。\n" + labels.citation_prefix in body
    assert GUIDE_TEXT_CLAIM in unsourced and labels.citation_prefix not in unsourced.split(labels.marked_section)[0]
    assert GUIDE_TEXT_CLAIM not in body
    # 出典の行は資料と照合できた説明の分だけ。
    assert len(rendered.citation_lines) == 1


def test_guide_notes_are_rendered_from_the_guide_settings_without_a_document_citation():
    notes = grounded.guide_notice_lines("窓口 X", ["対象の区分を特定できない場合", "現在の設定値を確かめる場合"], "担当者の承認", True)
    rendered = grounded.render_answer("要約", [_entry("設定画面で設定 A の値を入力します。")], guide_notes=("合成ガイド", notes))

    labels = grounded.answer_labels()
    section = rendered.text.split(labels.guide_notes_section + "\n\n", 1)[1].split("\n\n", 1)[0]
    assert section.splitlines() == [
        labels.guide_notes_note("合成ガイド"),
        labels.item_bullet + labels.guide_handoff("窓口 X", "対象の区分を特定できない場合／現在の設定値を確かめる場合"),
        labels.item_bullet + labels.guide_approval("担当者の承認"),
    ]
    assert labels.citation_prefix not in section
    # 節の見出しと注記はシステムの体裁（主張の検査の対象にしない）。
    assert grounded.is_structural_line(labels.guide_notes_section)
    assert grounded.is_structural_line(labels.guide_notes_note("合成ガイド"))
    assert grounded.guide_notice_lines("", ["条件"]) == []


def test_completion_requirement_can_cite_a_guide_note_but_evidence_requirement_cannot():
    notes = grounded.guide_notice_lines("窓口 X", ["変更の可否を判断する場合"])
    note_index = 1  # items（1 件）の後ろに続く
    completion = {
        "step_id": "a",
        "requirements": [{"id": "a:0", "text": "窓口 X への確認を示した", "kind": "completion"}],
        "depends_on": [],
    }
    result = grounded.apply_guide_contracts(
        coverage("a"),
        [_entry("設定画面で設定 A の値を入力します。")],
        [completion],
        audit({**review("a:0", [note_index]), "answer_quote": "窓口 X へお問い合わせください"}),
        notes,
    )
    assert result[0]["status"] == "explained"

    evidence = {**completion, "requirements": [{**completion["requirements"][0], "kind": "evidence"}]}
    result = grounded.apply_guide_contracts(
        coverage("a"),
        [_entry("設定画面で設定 A の値を入力します。")],
        [evidence],
        audit({**review("a:0", [note_index]), "answer_quote": "窓口 X へお問い合わせください"}),
        notes,
    )
    assert result[0]["status"] != "explained"
    # 案内の行が無ければ、items の外の index は引けない。
    result = grounded.apply_guide_contracts(
        coverage("a"), [_entry("t")], [completion], audit({**review("a:0", [note_index]), "answer_quote": "窓口 X"})
    )
    assert result[0]["status"] != "explained"


def test_plan_carries_the_guide_title_and_notices():
    plan = GuidePlan.from_payload(
        "guide",
        2,
        [{"id": "a", "title": "入力する"}],
        title="合成ガイド",
        notices={"contact": "窓口 X", "handoff_conditions": ["条件 1", " "], "approval_required": True, "approval_note": "承認"},
    )
    assert plan is not None
    assert (plan.title, plan.contact, plan.handoff_conditions, plan.approval_required, plan.approval_note) == (
        "合成ガイド",
        "窓口 X",
        ("条件 1",),
        True,
        "承認",
    )


def test_answer_moves_guide_text_out_of_the_document_sections_and_shows_the_guide_notes():
    """生成が業務ガイドの方針を根拠つきの説明に書いても、資料の出典を付けずに分け、案内の行はシステムが出す。"""
    ctx = _ab_context()
    notes = tuple(grounded.guide_notice_lines("窓口 X", ["区分を特定できない場合"]))
    knowledge = replace(
        _knowledge(GUIDE),
        guide_step_contracts=(
            {"step_id": "s1", "purpose": GUIDE_PURPOSE, "requirements": [], "depends_on": []},
            {"step_id": "s2", "purpose": "反映ボタンで確定する。", "requirements": [], "depends_on": ["s1"]},
        ),
        guide_title="合成ガイド",
        guide_notices=notes,
    )
    ids = _ids(ctx, knowledge)
    model = FakeModel(
        [
            draft(
                item(ids[STEP_A], "設定画面で設定 A の値を入力します。", STEP_A),
                item(ids[STEP_A], GUIDE_TEXT_CLAIM, STEP_A, kind="rule"),
                item(ids[STEP_B], "反映画面で反映ボタンを押して確定します。", STEP_B),
                summary="設定 A を入力して反映します。",
            )
        ],
        [make_audit((0, "supported", "matched", ""), (1, "supported", "matched", ""), (2, "supported", "matched", ""))],
    )

    response = _run(model, ctx, knowledge).response

    labels = grounded.answer_labels()
    text = response.answer_text
    documents = text.split(labels.unsourced_section)[0]
    assert GUIDE_TEXT_CLAIM not in documents
    assert marks(response)[GUIDE_TEXT_CLAIM] == ["guide_text"]
    assert labels.guide_notes_note("合成ガイド") in text
    assert labels.guide_handoff("窓口 X", "区分を特定できない場合") in text
    # 監査は案内の行を items の後ろの index で受け取る。
    audits = [prompt for name, prompt in model.prompts if name == "GroundedAudit"]
    assert '"guide_notes"' in audits[0]


# 合成の分岐の補足（分かっている条件で選んだ分岐の note。#3769）。
BRANCH_NOTE = "権限 B を持たない場合は同じ手順を説明し、登録の作業は担当者 Y へ依頼するよう補足する。"


def _section_order(text: str) -> list[str]:
    """本文の段落のうち、要約と節の見出しだけを順に返す（節の位置の確かめ用）。"""
    labels = grounded.answer_labels()
    headings = {labels.guide_notes_section, labels.rules_section, labels.marked_section, labels.unsourced_section}
    paragraphs = [paragraph.strip() for paragraph in text.split("\n\n")]
    return [paragraphs[0], *(paragraph for paragraph in paragraphs[1:] if paragraph in headings)]


def test_guide_notes_first_places_the_single_guide_section_right_after_the_summary():
    notes = grounded.guide_notice_lines("窓口 X", ["区分を特定できない場合"], branch_notes=[BRANCH_NOTE])
    entry = _entry("設定画面で設定 A の値を入力します。")
    labels = grounded.answer_labels()

    first = grounded.render_answer("要約", [entry], guide_notes=("合成ガイド", notes), guide_notes_first=True)
    default = grounded.render_answer("要約", [entry], guide_notes=("合成ガイド", notes))

    # 要約の直後に案内の節を置き、資料の説明はその後ろ。節は 1 つだけ（末尾に重ねない）。
    assert first.text.startswith("要約\n\n" + labels.guide_notes_section + "\n\n" + labels.guide_notes_note("合成ガイド") + "\n")
    assert first.text.count(labels.guide_notes_section) == 1 and first.text.count(BRANCH_NOTE) == 1
    assert first.text.index(BRANCH_NOTE) < first.text.index("設定 A の値を入力します。")
    # 置き場所だけが変わり、節の行と出典の行は既定と同じ。
    assert sorted(first.text.split("\n")) == sorted(default.text.split("\n"))
    assert first.citation_lines == default.citation_lines
    # 既定は従来どおり資料の説明の後ろ。
    assert default.text.index(BRANCH_NOTE) > default.text.index("設定 A の値を入力します。")
    # 案内の行が無ければ節を作らない。
    assert (
        labels.guide_notes_section
        not in grounded.render_answer("要約", [entry], guide_notes=("合成ガイド", []), guide_notes_first=True).text
    )


def _guide_knowledge(condition_notices: tuple[str, ...]):
    notes = tuple(grounded.guide_notice_lines("窓口 X", ["区分を特定できない場合"], branch_notes=condition_notices))
    return replace(
        _knowledge(GUIDE),
        guide_step_contracts=(
            {"step_id": "s1", "purpose": "設定 A の値を入力する。", "requirements": [], "depends_on": []},
            {"step_id": "s2", "purpose": "反映ボタンで確定する。", "requirements": [], "depends_on": ["s1"]},
        ),
        guide_title="合成ガイド",
        guide_notices=notes,
        guide_condition_notices=condition_notices,
    )


def _answer_text(knowledge) -> str:
    ctx = _ab_context()
    ids = _ids(ctx, knowledge)
    # 生成は分岐の補足を本文に書かない（利用者が自分で行う手順だけを書く）。
    model = FakeModel(
        [
            draft(
                item(ids[STEP_A], "設定画面で設定 A の値を入力します。", STEP_A),
                item(ids[STEP_B], "反映画面で反映ボタンを押して確定します。", STEP_B),
                summary="設定 A を入力して反映します。",
            )
        ],
        [make_audit((0, "supported", "matched", ""), (1, "supported", "matched", ""))],
    )
    return _run(model, ctx, knowledge).response.answer_text


def test_condition_selected_branch_note_is_shown_right_after_the_summary_even_if_the_draft_omits_it():
    """分かっている条件で選んだ分岐の補足は、生成が書かなくても要約の直後の案内の節に出す（#3769）。"""
    labels = grounded.answer_labels()
    text = _answer_text(_guide_knowledge((BRANCH_NOTE,)))

    order = _section_order(text)
    assert order[:2] == ["設定 A を入力して反映します。", labels.guide_notes_section]
    assert text.count(labels.guide_notes_section) == 1 and text.count(BRANCH_NOTE) == 1
    assert text.index(BRANCH_NOTE) < text.index("設定画面で設定 A の値を入力します。")
    # 案内の節は資料の出典を付けない（資料の内容として示さない）。
    section = text.split(labels.guide_notes_section + "\n\n", 1)[1].split("\n\n", 1)[0]
    assert labels.citation_prefix not in section
    assert section.splitlines()[0] == labels.guide_notes_note("合成ガイド")


def test_guide_notes_without_a_condition_selected_branch_stay_after_the_document_sections():
    labels = grounded.answer_labels()
    text = _answer_text(_guide_knowledge(()))

    assert text.count(labels.guide_notes_section) == 1
    assert text.index(labels.guide_notes_section) > text.index("設定画面で設定 A の値を入力します。")
