"""根拠計画に写した原文と、同じ行の evidence_id の食い違い（出典 ID の取り違え）を合成資料で再現する（#3804）。

保存した実応答と同じ形を匿名の文で作る: direct の行の原文が指した根拠に無く別の根拠にある・見出しだけの根拠を
本文の原文で照合する。原文の一致は説明の支持の証明ではないので、検査は計画の行の自己矛盾だけを見る。
作り直しても残った取り違えは、原文がある根拠が 1 つに決まるときだけ、その根拠へ付け替えて通常の照合に掛ける。
"""

from dataclasses import replace

import pytest

from rag_engine.generation.grounded import (
    _CITATION_VIEWS,
    REANCHOR_REASON,
    _citation_view,
    _misattributed_scopes,
    _request_plan_errors,
    reanchor_misattributed_citations,
    verify,
)
from rag_engine.models.llm import GroundedDraft

OPEN = "メニューから設定を選び、設定タブの登録画面を開きます。"
STEPS = "(1)設定タブより割当画面を開きます。(2)名前を一覧から選択します。"
COLORS = "(4)項目を追加・削除するときは一覧の項目をクリックします。使える項目は青、使えない項目は白で表示します。"
HEADING = "A【画面説明】"
SUMMARY = "登録と修正を行う画面です。"

SPANS = [
    {"evidence_id": "id-steps", "label": "E1", "text": STEPS},
    {"evidence_id": "id-colors", "label": "E2", "text": COLORS},
    {"evidence_id": "id-open", "label": "E6", "text": OPEN},
    {"evidence_id": "id-heading", "label": "E7", "text": HEADING},
    {"evidence_id": "id-summary", "label": "E8", "text": SUMMARY},
]


def scope(label, quote, relation="direct"):
    return {"evidence_id": label, "target": "対象", "relation": relation, "link_quote": quote}


def operation(text, *labels):
    return {
        "kind": "operation",
        "request_id": "Q1",
        "text": text,
        "citations": [{"evidence_id": label} for label in labels],
    }


def plan_draft(scopes, items, evidence_ids=None):
    return GroundedDraft.model_validate(
        {
            "request_coverage": [
                {
                    "request_id": "Q1",
                    "evidence_ids": evidence_ids if evidence_ids is not None else list(dict.fromkeys(s["evidence_id"] for s in scopes)),
                    "evidence_scope": scopes,
                }
            ],
            "items": items,
            "summary": "設定の手順です。",
            "confidence": "high",
        }
    )


# 保存した実応答と同じずれ: 1 行目は E6 の原文を E1 に、2 行目は E8 の原文を見出しだけの E7 に、4 行目は E1 の原文を E2 に写す。
SHIFTED = [scope("E1", OPEN), scope("E7", SUMMARY), scope("E8", SUMMARY), scope("E2", STEPS)]
SHIFTED_ITEMS = [
    operation("設定タブの登録画面を開く。", "E1"),
    operation("必要な項目を入力して登録する。", "E7", "E8"),
    operation("割当画面で一覧から名前を選ぶ。", "E2"),
]


def test_a_direct_quote_found_only_in_another_evidence_is_a_plan_error_naming_where_it_is():
    draft = plan_draft(SHIFTED, SHIFTED_ITEMS)
    assert _misattributed_scopes(draft.request_coverage[0], SPANS) == {"E1": ["E6"], "E7": ["E8"], "E2": ["E1"]}
    errors = _request_plan_errors(draft, SPANS)
    assert len(errors) == 3
    assert "E1 の照合に写した原文は E1 に無く、E6 にあります" in errors[0]
    assert any("E2 の照合に写した原文は E2 に無く、E1 にあります" in error for error in errors)


def test_quotes_that_do_not_contradict_their_own_evidence_are_not_errors():
    paraphrase = "設定画面を開く手順。"
    cases = [
        [scope("E6", OPEN), scope("E1", STEPS)],  # 指した根拠にある
        [scope("E6", paraphrase)],  # どこにも無い言い換えは取り違えと言い切れない
        [scope("E6", "")],  # 空の原文（direct の既定）
        [scope("E2", STEPS, relation="linked")],  # linked の関係の原文は別の根拠にあってよい
    ]
    for scopes in cases:
        draft = plan_draft(scopes, [operation("設定を開く。", scopes[0]["evidence_id"])])
        assert not _misattributed_scopes(draft.request_coverage[0], SPANS), scopes
        assert not _request_plan_errors(draft, SPANS), scopes
    # 同じ原文が指した根拠と別の根拠の両方にあれば、指した根拠の原文として読める。
    duplicated = [*SPANS, {"evidence_id": "id-copy", "label": "E9", "text": OPEN}]
    draft = plan_draft([scope("E6", OPEN)], [operation("設定を開く。", "E6")])
    assert not _misattributed_scopes(draft.request_coverage[0], duplicated)
    # 計画で使わない行（evidence_ids に無い）の照合は説明の出典にならないので作り直さない。
    draft = plan_draft([scope("E6", OPEN), scope("E1", OPEN)], [operation("設定を開く。", "E6")], evidence_ids=["E6"])
    assert not _request_plan_errors(draft, SPANS)


def test_an_attachment_label_is_not_checked_for_misattribution():
    spans = [{**span, "conversation_attachment": True} if span["label"] == "E1" else span for span in SPANS]
    draft = plan_draft([scope("E1", OPEN)], [operation("設定を開く。", "E1")])
    assert not _misattributed_scopes(draft.request_coverage[0], spans)


def test_a_misattributed_citation_keeps_its_text_and_id_with_an_unassessed_mark():
    draft = plan_draft(SHIFTED, SHIFTED_ITEMS)
    checked, _ = verify("設定の手順は？", draft, SPANS)
    by_text = {entry.item.text: entry for entry in checked if not entry.added}
    # 照合（verify）自身は本文を外さず、別の ID へも付け替えない（付け替えは reanchor_misattributed_citations だけ）。
    assert set(by_text) == {item["text"] for item in SHIFTED_ITEMS}
    first = by_text["設定タブの登録画面を開く。"]
    assert first.span["evidence_id"] == "id-steps"
    assert not first.removed
    assert any(mark["code"] == "unassessed" and "E1" in mark["detail"] for mark in first.marks)


def test_a_consistent_plan_adds_no_misattribution_mark():
    scopes = [scope("E6", OPEN), scope("E1", STEPS)]
    draft = plan_draft(scopes, [operation("設定タブの登録画面を開く。", "E6"), operation("割当画面で名前を選ぶ。", "E1")])
    checked, _ = verify("設定の手順は？", draft, SPANS)
    for entry in checked:
        assert not any("写した原文" in str(mark) for mark in entry.marks), entry.marks


def _context(*extra):
    from test_grounded_answer import context, record

    return context(
        record(0, STEPS, "割当"), record(1, OPEN, "登録"), *(record(2 + index, text, f"別{index}") for index, text in enumerate(extra))
    )


def _answer(drafts, *extra):
    from test_grounded_answer import FakeModel

    from rag_engine.config import get_settings
    from rag_engine.dependencies import AnswerDependencies, bind_dependencies
    from rag_engine.generation.answering import synthesize_grounded_answer

    model = FakeModel(drafts, [])

    def unused(*args, **kwargs):
        raise AssertionError("外部呼び出しは不要")

    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        result = synthesize_grounded_answer(
            "設定の手順は？", _context(*extra), replace(get_settings(), answer_audit_mode="none"), image_prompt_mode="text_only"
        ).response
    return result, model


def _labels(*extra):
    """モデルに見せる表示 ID（[E1] 形式）。"""
    from rag_engine.generation.answering import _grounded_spans

    spans = _grounded_spans("設定の手順は？", _context(*extra), (), None)
    labels = {span["text"]: span["label"] for span in spans}
    return labels[STEPS], labels[OPEN]


def _payload(steps_label, open_label, *, swapped):
    first, second = (steps_label, open_label) if swapped else (open_label, steps_label)
    return {
        "request_coverage": [
            {
                "request_id": "Q1",
                "evidence_ids": [open_label, steps_label],
                "evidence_scope": [
                    {"evidence_id": first, "target": "登録画面", "relation": "direct", "link_quote": OPEN},
                    {"evidence_id": second, "target": "割当画面", "relation": "direct", "link_quote": STEPS},
                ],
            }
        ],
        "items": [
            {"kind": "operation", "request_id": "Q1", "text": OPEN, "citations": [{"quote": OPEN, "evidence_id": first}]},
            {"kind": "operation", "request_id": "Q1", "text": STEPS, "citations": [{"quote": STEPS, "evidence_id": second}]},
        ],
        "summary": "登録画面を開き、割当画面で名前を選びます。",
        "confidence": "high",
    }


def test_a_misattributed_plan_is_repaired_once_with_the_location_of_each_quote():
    steps_label, open_label = _labels()
    result, model = _answer([_payload(steps_label, open_label, swapped=True), _payload(steps_label, open_label, swapped=False)])
    assert result.generation_trace["llm_calls"] == 2
    repair = model.prompts[1][1]
    assert "invalid_request_plan" in repair
    assert f"{steps_label} の照合に写した原文は {steps_label} に無く、{open_label} にあります" in repair
    assert OPEN in result.answer_text and STEPS in result.answer_text
    assert not any("写した原文" in str(mark) for mark in result.envelope["passage_marks"])
    # 作り直しで直れば付け替えない。
    assert "citation_reanchored" not in result.generation_trace["rounds"][0]["timing"]


def test_a_misattribution_left_after_the_one_repair_is_reanchored_to_the_only_evidence_with_the_quote():
    steps_label, open_label = _labels()
    swapped = _payload(steps_label, open_label, swapped=True)
    result, model = _answer([swapped, swapped])
    # 作り直しは一度だけ。残った取り違えは、原文がある根拠が 1 つなのでその根拠へ付け替え、通常の照合で確かめる。
    assert result.generation_trace["llm_calls"] == 2
    timing = result.generation_trace["rounds"][0]["timing"]
    assert "request_plan_unsettled" not in timing
    moved = {(row["from_label"], row["to_label"]): row for row in timing["citation_reanchored"]}
    assert set(moved) == {(steps_label, open_label), (open_label, steps_label)}
    assert all(row["reason"] == REANCHOR_REASON and row["item_indexes"] for row in moved.values())
    assert OPEN in result.answer_text and STEPS in result.answer_text
    assert not any("写した原文" in str(mark) for mark in result.envelope["passage_marks"])


def test_a_misattribution_with_two_evidence_holding_the_quote_stays_marked():
    # 別の機能の根拠にも同じ原文がある。付け替え先が 1 つに決まらないので、従来どおり印を付けて残す。
    extra = OPEN + "続けて保存します。"
    steps_label, open_label = _labels(extra)
    swapped = _payload(steps_label, open_label, swapped=True)
    result, model = _answer([swapped, swapped], extra)
    assert result.generation_trace["llm_calls"] == 2
    timing = result.generation_trace["rounds"][0]["timing"]
    assert timing["request_plan_unsettled"]
    assert all(row["from_label"] != steps_label for row in timing.get("citation_reanchored", []))
    assert "写した原文" in str(result.envelope["passage_marks"])


# 付け替えの単体の検査。行番号を付けた根拠では、原文を含む行だけを引用にする。
NUMBERED = "1. 準備の説明です。\n" + OPEN + "\n3. 後片付けの説明です。"
NUMBERED_SPANS = [
    {"evidence_id": "id-steps", "label": "E1", "text": STEPS},
    {"evidence_id": "id-numbered", "label": "E4", "text": NUMBERED},
    {"evidence_id": "id-colors", "label": "E5", "text": COLORS},
]


@pytest.fixture
def sent_views():
    """モデルへ送った版（行番号つきの引用単位）を、送信時と同じく記録する。"""
    token = _CITATION_VIEWS.set({id(span): (span, _citation_view(span, explicit_units=True)) for span in NUMBERED_SPANS})
    yield
    _CITATION_VIEWS.reset(token)


def test_a_single_evidence_with_the_quote_receives_the_citation_and_is_verified(sent_views):
    draft = plan_draft([scope("E5", OPEN)], [operation("設定タブの登録画面を開く。", "E5")])
    moved, records = reanchor_misattributed_citations(draft, NUMBERED_SPANS)
    assert records == [
        {
            "reason": REANCHOR_REASON,
            "request_id": "Q1",
            "from_label": "E5",
            "from_evidence_id": "id-colors",
            "to_label": "E4",
            "to_evidence_id": "id-numbered",
            "lines": "2",
            "item_indexes": [0],
        }
    ]
    plan = moved.request_coverage[0]
    assert plan.evidence_ids == ["E4"] and [row.evidence_id for row in plan.evidence_scope] == ["E4"]
    assert [(c.evidence_id, c.lines, c.quote) for c in moved.items[0].citations] == [("E4", "2", "")]
    assert moved.items[0].text == draft.items[0].text
    assert not _request_plan_errors(moved, NUMBERED_SPANS)
    # 付け替えた出典は通常の照合に掛かり、原文を含む行だけを引用にした、印の無い説明になる。
    checked, _ = verify("設定の手順は？", moved, NUMBERED_SPANS)
    entry = next(entry for entry in checked if not entry.added)
    assert entry.span["evidence_id"] == "id-numbered"
    assert entry.item.quote == OPEN
    assert not entry.marks and entry.confirmed


def test_a_quote_repeated_in_the_target_cites_its_first_line():
    repeated = "1. 準備の説明です。\n" + OPEN + "\n" + OPEN
    spans = [*NUMBERED_SPANS[:1], {"evidence_id": "id-numbered", "label": "E4", "text": repeated}, NUMBERED_SPANS[2]]
    token = _CITATION_VIEWS.set({id(span): (span, _citation_view(span, explicit_units=True)) for span in spans})
    try:
        draft = plan_draft([scope("E5", OPEN)], [operation("設定タブの登録画面を開く。", "E5")])
        moved, records = reanchor_misattributed_citations(draft, spans)
    finally:
        _CITATION_VIEWS.reset(token)
    # 同じ根拠の中の位置は、原文照合と同じく最初の位置にする（根拠は 1 つに決まっている）。
    assert [row["lines"] for row in records] == ["2"]
    assert moved.items[0].citations[0].lines == "2"


def test_rows_that_swap_ids_are_reanchored_at_once_without_chaining():
    draft = plan_draft(SHIFTED, SHIFTED_ITEMS)
    moved, records = reanchor_misattributed_citations(draft, SPANS)
    assert {(row["from_label"], row["to_label"]) for row in records} == {("E1", "E6"), ("E7", "E8"), ("E2", "E1")}
    # E1 → E6 と E2 → E1 を元の草稿から一度に決める（E2 の出典を E1 から E6 へ続けて動かさない）。
    assert [[c.evidence_id for c in item.citations] for item in moved.items] == [["E6"], ["E8", "E8"], ["E1"]]
    assert not _request_plan_errors(moved, SPANS)
    checked, _ = verify("設定の手順は？", moved, SPANS)
    for entry in checked:
        assert not any("写した原文" in str(mark) for mark in entry.marks), entry.marks
    assert {entry.item.text for entry in checked if not entry.added} == {item["text"] for item in SHIFTED_ITEMS}


@pytest.mark.parametrize(
    ("spans", "scopes"),
    [
        # 原文がある根拠が 2 つ（付け替え先が決まらない）
        ([*SPANS, {"evidence_id": "id-copy", "label": "E9", "text": OPEN}], [scope("E1", OPEN)]),
        # 原文があるのは会話の添付だけ（添付へは付け替えない）
        ([{**span, "conversation_attachment": True} if span["label"] == "E6" else span for span in SPANS], [scope("E1", OPEN)]),
        # 添付と別の根拠の両方にある（添付も候補に数え、1 つに決まらない）
        (
            [*SPANS, {"evidence_id": "id-attached", "label": "E9", "text": OPEN, "conversation_attachment": True}],
            [scope("E1", OPEN)],
        ),
        # 指した根拠にも原文がある（取り違えではない）
        ([*SPANS, {"evidence_id": "id-steps-2", "label": "E1", "text": OPEN}], [scope("E1", OPEN)]),
        # 付け替え先に unrelated の判断が残る（計画が矛盾する）
        (SPANS, [scope("E1", OPEN), scope("E6", "", relation="unrelated")]),
        # 計画に同じ label の行が複数ある
        (SPANS, [scope("E1", OPEN), scope("E1", "設定画面を開く手順。")]),
    ],
    ids=["two-candidates", "attachment-only", "attachment-and-evidence", "quote-in-named", "target-unrelated", "duplicate-rows"],
)
def test_reanchoring_is_skipped_when_the_target_is_not_unique(spans, scopes):
    draft = plan_draft(scopes, [operation("設定タブの登録画面を開く。", "E1")], evidence_ids=["E1"])
    moved, records = reanchor_misattributed_citations(draft, spans)
    assert records == []
    assert moved is draft


def test_duplicate_plans_for_the_same_request_do_not_repeat_an_error():
    draft = plan_draft(SHIFTED, SHIFTED_ITEMS)
    doubled = draft.model_copy(update={"request_coverage": [*draft.request_coverage, *draft.request_coverage]})
    errors = _request_plan_errors(doubled, SPANS)
    assert len(errors) == len(set(errors)) == 3
