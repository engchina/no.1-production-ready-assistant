"""根拠計画の evidence_ids に、送った根拠のどれにも当たらない ID（業務ガイドの要件 ID の形）がある草稿（#3825）。

保存した実応答と同じ形を匿名の文で作る: 計画の evidence_ids に `<要件>:<番号>` の形の ID を書く。照合を足しても
直らないので、根拠の ID でないことを指摘して作り直させ、残っても計画に正しく載った根拠の説明を未確認にしない。
"""

from dataclasses import replace

from rag_engine.generation.grounded import _plan_undecided_errors, _request_plan_errors, _unknown_plan_label_errors, verify
from rag_engine.models.llm import GroundedDraft

OPEN = "メニューから設定を選び、設定タブの登録画面を開きます。"
STEPS = "(1)設定タブより割当画面を開きます。(2)名前を一覧から選択します。"
GUIDE_IDS = ["step:0", "step:1"]

SPANS = [
    {"evidence_id": "id-open", "label": "E1", "text": OPEN},
    {"evidence_id": "id-steps", "label": "E2", "text": STEPS},
]


def scope(label, quote):
    return {"evidence_id": label, "target": "対象", "relation": "direct", "link_quote": quote}


def cited(text, *pairs):
    return {"kind": "operation", "request_id": "Q1", "text": text, "citations": [{"evidence_id": e, "quote": q} for e, q in pairs]}


def plan_draft(evidence_ids, scopes, items):
    return GroundedDraft.model_validate(
        {
            "request_coverage": [{"request_id": "Q1", "evidence_ids": evidence_ids, "evidence_scope": scopes}],
            "items": items,
            "summary": "設定の手順です。",
            "confidence": "high",
        }
    )


def unknown_error(label):
    return (
        f"Q1: {label} は Evidence の ID ではありません。"
        "evidence_ids には [E1] 形式の根拠の番号だけを書き、業務ガイドの要件は guide_coverage の requirement_id に書いてください。"
    )


def test_a_label_that_names_no_sent_evidence_is_reported_as_not_an_evidence_id():
    draft = plan_draft(["E1", *GUIDE_IDS, "E9"], [scope("E1", OPEN)], [cited(OPEN, ("E1", OPEN))])
    errors = _request_plan_errors(draft, SPANS)
    assert errors == [unknown_error(label) for label in [*GUIDE_IDS, "E9"]]
    assert not any("対象の照合は1件必要" in error for error in errors)
    # 計画の内外の判定を未決にする不整合だけを数えるときは、根拠でない ID の指摘を含めない。
    assert _request_plan_errors(draft, SPANS, unknown_labels=False) == []
    assert _unknown_plan_label_errors(draft, SPANS) == errors


def test_a_sent_evidence_label_without_a_scope_decision_keeps_the_scope_error():
    draft = plan_draft(["E1", "E2", GUIDE_IDS[0]], [scope("E1", OPEN)], [cited(OPEN, ("E1", OPEN))])
    assert _request_plan_errors(draft, SPANS) == [unknown_error(GUIDE_IDS[0]), "Q1: E2 の対象の照合は1件必要です。"]
    assert _request_plan_errors(draft, SPANS, unknown_labels=False) == ["Q1: E2 の対象の照合は1件必要です。"]


def test_unknown_labels_alone_do_not_mark_a_properly_planned_item_unassessed():
    # 説明は計画に載った E1 と、計画に無い E2 を引く。根拠でない ID だけを理由に未決にしなければ、
    # 計画の外の出典（E2）は外れ、計画の内の出典（E1）で説明が残る。
    draft = plan_draft(["E1", *GUIDE_IDS], [scope("E1", OPEN)], [cited(OPEN, ("E1", OPEN), ("E2", STEPS))])
    unsettled = [*_request_plan_errors(draft, SPANS, unknown_labels=False), *_plan_undecided_errors(draft, SPANS)]
    assert unsettled == []
    checked, _ = verify("設定の手順は？", draft, SPANS, plan_unsettled=unsettled)
    entry = next(entry for entry in checked if not entry.added)
    assert entry.span["evidence_id"] == "id-open"
    assert not any(mark["code"] == "unassessed" for mark in entry.marks), entry.marks


def test_an_item_whose_evidence_is_outside_the_plan_is_still_undecided():
    # 根拠でない ID を無視しても、計画に載っていない根拠だけを引く説明は今までどおり未決として印を付けて残す。
    draft = plan_draft(["E1", *GUIDE_IDS], [scope("E1", OPEN)], [cited(STEPS, ("E2", STEPS))])
    unsettled = [*_request_plan_errors(draft, SPANS, unknown_labels=False), *_plan_undecided_errors(draft, SPANS)]
    assert unsettled == ["Q1: E2 の対象を計画で照合できませんでした。"]
    checked, _ = verify("設定の手順は？", draft, SPANS, plan_unsettled=unsettled)
    entry = next(entry for entry in checked if not entry.added)
    assert any(mark["code"] == "unassessed" for mark in entry.marks)


def _answer(drafts):
    from test_grounded_answer import FakeModel, context, record

    from rag_engine.config import get_settings
    from rag_engine.dependencies import AnswerDependencies, bind_dependencies
    from rag_engine.generation.answering import synthesize_grounded_answer

    ctx = context(record(0, OPEN, "登録"), record(1, STEPS, "割当"))
    model = FakeModel(drafts, [])

    def unused(*args, **kwargs):
        raise AssertionError("外部呼び出しは不要")

    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        result = synthesize_grounded_answer(
            "設定の手順は？", ctx, replace(get_settings(), answer_audit_mode="none"), image_prompt_mode="text_only"
        ).response
    return result, model


def _labels():
    """モデルに見せる表示 ID（[E1] 形式）。"""
    from test_grounded_answer import context, record

    from rag_engine.generation.answering import _grounded_spans

    spans = _grounded_spans("設定の手順は？", context(record(0, OPEN, "登録"), record(1, STEPS, "割当")), (), None)
    labels = {span["text"]: span["label"] for span in spans}
    return labels[OPEN], labels[STEPS]


def test_the_repair_names_the_unknown_label_and_an_unfixed_one_does_not_mark_the_planned_item():
    open_label, steps_label = _labels()
    payload = {
        "request_coverage": [
            {
                "request_id": "Q1",
                "evidence_ids": [open_label, *GUIDE_IDS],
                "evidence_scope": [{"evidence_id": open_label, "target": "登録画面", "relation": "direct", "link_quote": OPEN}],
            }
        ],
        "items": [
            {
                "kind": "operation",
                "request_id": "Q1",
                "text": OPEN,
                "citations": [{"quote": OPEN, "evidence_id": open_label}, {"quote": STEPS, "evidence_id": steps_label}],
            }
        ],
        "summary": "登録画面を開きます。",
        "confidence": "high",
    }
    # 作り直しも同じ ID を残す（直さなかった）。
    result, model = _answer([payload, payload])
    assert result.generation_trace["llm_calls"] == 2
    repair = model.prompts[1][1]
    assert "invalid_request_plan" in repair
    for label in GUIDE_IDS:
        assert f"{label} は Evidence の ID ではありません。" in repair
        assert f"{label} の対象の照合は1件必要" not in repair
    timing = result.generation_trace["rounds"][0]["timing"]
    assert timing["request_plan_ignored"] == [unknown_error(label) for label in GUIDE_IDS]
    assert "request_plan_unsettled" not in timing
    assert OPEN in result.answer_text
    assert not any(reason["code"] == "unassessed" for mark in result.envelope["passage_marks"] for reason in mark["reasons"])
