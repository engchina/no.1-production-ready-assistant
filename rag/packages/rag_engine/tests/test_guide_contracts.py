"""手順の全要件と順序の検査。"""

import pytest

from rag_engine.generation import grounded
from rag_engine.models.llm import GroundedAudit, GroundedCitation, GroundedItem
from rag_engine.retrieval.guide_steps import GuidePlan


def entry():
    return grounded.CheckedItem(
        GroundedItem(kind="operation", text="対象を選択します", citations=[GroundedCitation(evidence_id="", quote="対象を選択します")]),
        span={"text": "対象を選択します", "evidence_id": "E1"},
    )


def audit(*reviews):
    return GroundedAudit(reviews=[], summary_supported=True, goal_alignment="aligned", guide_requirement_reviews=list(reviews))


_STATES = {"addressed": "explained", "partial": "partly_explained", "missing": "not_mentioned"}


def review(key, indexes, status="addressed"):
    """要件の監査の観察（#2800）。status は充足の名前（addressed / partial / missing）か観察の名前。"""
    state = _STATES.get(status, status)
    return {"requirement_id": key, "answer_state": state, "item_indexes": indexes, "reason": "", "answer_quote": "対象を選択します"}


def contract(step, *requirements, dependencies=()):
    return {
        "step_id": step,
        "requirements": [{"id": f"{step}:{i}", "text": text} for i, text in enumerate(requirements)],
        "depends_on": dependencies,
    }


def coverage(*ids):
    return [{"step_id": key, "status": "explained", "required": True} for key in ids]


def test_all_requirements_required_even_when_primary_operation_is_explained():
    result = grounded.apply_guide_contracts(
        coverage("a"), [entry()], [contract("a", "対象選択", "保存して確定")], audit(review("a:0", [0]), review("a:1", [], "missing"))
    )
    assert result[0]["status"] == "partial"
    assert result[0]["requirements"][1]["status"] == "missing"


@pytest.mark.parametrize(
    "reviews", [[], [review("a:0", [0]), review("a:0", [0])], [review("a:0", [-1])], [review("a:0", [20])], [review("other", [0])]]
)
def test_omitted_duplicate_and_invalid_reviews_cannot_complete_requirement(reviews):
    result = grounded.apply_guide_contracts(coverage("a"), [entry()], [contract("a", "対象選択")], audit(*reviews))
    assert result[0]["status"] != "explained"


@pytest.mark.parametrize("flag", ["marked", "added"])
def test_unverified_and_system_added_quote_do_not_complete_requirement(flag):
    item = entry()
    if flag == "marked":
        item.mark("unassessed", "未確認")
    else:
        item.added = True
    result = grounded.apply_guide_contracts(coverage("a"), [item], [contract("a", "対象選択")], audit(review("a:0", [0])))
    assert result[0]["status"] != "explained"


def test_dependency_order_is_checked():
    contracts = [contract("open", "画面を開く"), contract("edit", "値を変更", dependencies=["open"])]
    result = grounded.apply_guide_contracts(
        coverage("open", "edit"), [entry(), entry()], contracts, audit(review("open:0", [1]), review("edit:0", [0]))
    )
    assert result[1]["status"] == "partial"
    assert result[1]["order_errors"] == ["open"]
    result = grounded.apply_guide_contracts(
        coverage("open", "edit"), [entry(), entry()], contracts, audit(review("open:0", [0]), review("edit:0", [1]))
    )
    assert all(step["status"] == "explained" for step in result)


def test_all_requirements_can_use_several_supported_items():
    result = grounded.apply_guide_contracts(
        coverage("a"), [entry(), entry()], [contract("a", "対象選択", "保存して確定")], audit(review("a:0", [0]), review("a:1", [1]))
    )
    assert result[0]["status"] == "explained"


def test_plan_preserves_configured_requirements_and_dependencies():
    plan = GuidePlan.from_payload(
        "guide",
        1,
        [{"id": "a", "purpose": "値を編集", "evidence_requirements": ["入力方法"], "done_when": "保存済み", "depends_on": ["open"]}],
    )
    assert plan is not None
    assert [r["text"] for r in plan.step_contracts[0]["requirements"]] == ["入力方法", "保存済み"]
    assert [r["kind"] for r in plan.step_contracts[0]["requirements"]] == ["evidence", "completion"]
    assert plan.step_contracts[0]["depends_on"] == ["open"]
    assert plan.step_contracts[0]["purpose"] == "値を編集"


def test_missing_requirement_is_reported_in_answer_without_asserting_it_as_fact():
    result = grounded.apply_guide_contracts(
        coverage("a"), [entry()], [contract("a", "対象選択", "保存して確定")], audit(review("a:0", [0]), review("a:1", [], "missing"))
    )
    result[0]["title"] = "編集"
    assert "保存して確定" in grounded.guide_step_gap_line(result[0])
    assert "確かめられていない" in grounded.guide_step_gap_line(result[0])


def test_requirement_to_explain_unknown_scope_can_use_an_explicit_gap():
    gap = grounded.CheckedItem(GroundedItem(kind="gap", text="列の定義を確認できません。"))
    result = grounded.apply_guide_contracts(
        coverage("a"),
        [gap],
        [contract("a", "列が未確認ならその範囲を明示する")],
        audit({**review("a:0", [0]), "answer_quote": "列の定義を確認できません。"}),
    )
    assert result[0]["status"] == "explained"


@pytest.mark.parametrize("quote", ["", "資料にあるので案内できます", "別の操作を説明します"])
def test_review_requires_a_quote_that_actually_exists_in_answer(quote):
    result = grounded.apply_guide_contracts(
        coverage("a"), [entry()], [contract("a", "対象選択")], audit({**review("a:0", [0]), "answer_quote": quote})
    )
    assert result[0]["status"] != "explained"


def test_source_quote_is_not_a_proof_of_what_the_answer_explained():
    # モデルは引いた item の説明の文ではなく根拠の原文を写すことが多い（#2811）。引いた item に属する文なら受ける。
    paraphrase = grounded.CheckedItem(
        GroundedItem(
            kind="operation", text="一覧で対象を選ぶ", citations=[GroundedCitation(evidence_id="", quote="一覧から対象を選択します")]
        ),
        span={"text": "一覧から対象を選択します", "evidence_id": "E1"},
    )
    result = grounded.apply_guide_contracts(
        coverage("a"), [paraphrase], [contract("a", "対象選択")], audit({**review("a:0", [0]), "answer_quote": "一覧から対象を選択します"})
    )
    assert result[0]["status"] == "unknown"


@pytest.mark.parametrize("indexes", [[0, 1], [1, 0]])
def test_requirement_quote_can_combine_excerpts_from_several_items(indexes):
    items = [
        grounded.CheckedItem(
            GroundedItem(
                kind="operation",
                text="一覧で対象を選択します。続けて詳細を開きます。",
                citations=[GroundedCitation(evidence_id="", quote="一覧で対象を選択します。")],
            ),
            span={"text": "一覧で対象を選択します。続けて詳細を開きます。", "evidence_id": "E1"},
        ),
        grounded.CheckedItem(
            GroundedItem(
                kind="operation",
                text="内容を確認します。保存ボタンで確定します。",
                citations=[GroundedCitation(evidence_id="", quote="保存ボタンで確定します。")],
            ),
            span={"text": "内容を確認します。保存ボタンで確定します。", "evidence_id": "E2"},
        ),
    ]
    observation = {**review("a:0", indexes), "answer_quote": "一覧で対象を選択します。保存ボタンで確定します。"}
    result = grounded.apply_guide_contracts(coverage("a"), items, [contract("a", "対象を選び保存する")], audit(observation))
    assert result[0]["status"] == "explained"


def test_requirement_quote_requires_each_combined_excerpt_to_be_answer_text():
    items = [
        grounded.CheckedItem(
            GroundedItem(
                kind="rule",
                text="担当者は一覧で対象を選びます。",
                citations=[GroundedCitation(evidence_id="", quote="一覧の対象を担当者が選択します。")],
            ),
            span={"text": "一覧の対象を担当者が選択します。", "evidence_id": "E1"},
        ),
        grounded.CheckedItem(
            GroundedItem(
                kind="rule",
                text="承認後、1日以内に設定します。",
                citations=[GroundedCitation(evidence_id="", quote="承認後、一日以内に設定します。")],
            ),
            span={"text": "承認後、一日以内に設定します。", "evidence_id": "E2"},
        ),
    ]
    observation = {**review("a:0", [0, 1]), "answer_quote": "担当者は一覧で対象を選びます。承認後、一日以内に設定します。"}
    result = grounded.apply_guide_contracts(coverage("a"), items, [contract("a", "担当者と設定までの期間")], audit(observation))
    assert result[0]["status"] == "unknown"


@pytest.mark.parametrize(
    "quote",
    ["対象を選択します\n資料にない操作です", "対象を選択します。別の手順を実行します。", "対象を選択しません。対象を選択します。"],
)
def test_combined_requirement_quote_rejects_invented_or_uncited_excerpts(quote):
    uncited = grounded.CheckedItem(
        GroundedItem(
            kind="operation", text="別の手順を実行します", citations=[GroundedCitation(evidence_id="", quote="別の手順を実行します")]
        ),
        span={"text": "別の手順を実行します", "evidence_id": "E2"},
    )
    result = grounded.apply_guide_contracts(
        coverage("a"), [entry(), uncited], [contract("a", "対象選択")], audit({**review("a:0", [0]), "answer_quote": quote})
    )
    assert result[0]["status"] != "explained"


def test_combined_requirement_quote_does_not_accept_a_changed_number():
    item = grounded.CheckedItem(
        GroundedItem(
            kind="rule",
            text="担当者が実行します。2日以内に設定します。",
            citations=[GroundedCitation(evidence_id="", quote="担当者が実行します。2日以内に設定します。")],
        ),
        span={"text": "担当者が実行します。2日以内に設定します。", "evidence_id": "E1"},
    )
    observation = {**review("a:0", [0]), "answer_quote": "担当者が実行します。1日以内に設定します。"}
    result = grounded.apply_guide_contracts(coverage("a"), [item], [contract("a", "担当者と期間")], audit(observation))
    assert result[0]["status"] != "explained"


@pytest.mark.parametrize(("kind", "explained"), [("gap", True), ("operation", False)])
def test_stated_unavailable_may_omit_the_quote_only_for_gap_lines(kind, explained):
    item = grounded.CheckedItem(
        GroundedItem(
            kind=kind,
            text="列の定義を確認できません。",
            citations=[GroundedCitation(evidence_id="", quote="" if kind == "gap" else "列の定義を確認できません。")],
        ),
        span=None if kind == "gap" else {"text": "列の定義を確認できません。", "evidence_id": "E1"},
    )
    result = grounded.apply_guide_contracts(
        coverage("a"),
        [item],
        [contract("a", "列が未確認ならその範囲を明示する")],
        audit({**review("a:0", [0], "stated_unavailable"), "answer_quote": ""}),
    )
    assert (result[0]["status"] == "explained") is explained


def test_one_operation_does_not_complete_a_distinct_missing_step():
    result = grounded.apply_guide_contracts(
        coverage("a", "b"),
        [entry()],
        [contract("a", "対象選択"), contract("b", "保存確定")],
        audit(review("a:0", [0]), {**review("b:0", [], "missing"), "answer_quote": ""}),
    )
    assert [step["status"] for step in result] == ["explained", "unused"]


def test_generation_receives_requirements_and_missing_audit_rows_remain_unverified():
    from dataclasses import replace

    from test_grounded_answer import FakeModel, draft, item
    from test_grounded_answer import audit as make_audit
    from test_guide_step_fulfillment import GUIDE, STEP_A, STEP_B, _ab_context, _audits, _drafts, _ids, _knowledge, _run

    from rag_engine.models.llm import GuideRequirementReview

    ctx = _ab_context()
    knowledge = replace(_knowledge(GUIDE), guide_step_contracts=(contract("s1", STEP_A), contract("s2", STEP_B, dependencies=["s1"])))
    labels = _ids(ctx, knowledge)
    incomplete = make_audit((0, "supported", "matched", ""), (1, "supported", "matched", ""), requests=[("Q1", "addressed")])
    complete = {
        **incomplete,
        "guide_requirement_reviews": [
            GuideRequirementReview(requirement_id="s1:0", answer_state="explained", item_indexes=[0], answer_quote=STEP_A, reason=""),
            GuideRequirementReview(requirement_id="s2:0", answer_state="explained", item_indexes=[1], answer_quote=STEP_B, reason=""),
        ],
    }
    model = FakeModel([draft(item(labels[STEP_A], STEP_A, STEP_A), item(labels[STEP_B], STEP_B, STEP_B))], [incomplete, complete])
    result = _run(model, ctx, knowledge).response
    assert "guide_contracts" in _drafts(model)[0]
    steps = result.generation_trace["rounds"][0]["guide_steps"]
    assert all(step["status"] != "explained" for step in steps)
    assert all(requirement["status"] == "unknown" for step in steps for requirement in step["requirements"])
    assert len(_audits(model)) == 1


@pytest.mark.parametrize("supported", [True, False])
def test_evidence_condition_checks_support_without_requiring_its_instruction_in_answer(supported):
    plan = GuidePlan.from_payload(
        "guide",
        1,
        [{"id": "a", "evidence_requirements": ["対象の項目名と入力先が同じ行にあること"], "done_when": "対象を選択する操作を説明する"}],
    )
    assert plan is not None
    item = entry()
    if not supported:
        item.mark("unassessed", "引用元の行を確認できない")
    result = grounded.apply_guide_contracts(coverage("a"), [item], plan.step_contracts, audit(review("a:0", [0]), review("a:1", [0])))
    assert (result[0]["status"] == "explained") is supported
    assert "同じ行" not in item.item.text


def test_missing_following_step_does_not_invalidate_supported_entry_step():
    plan = GuidePlan.from_payload(
        "guide",
        1,
        [
            {"id": "open", "evidence_requirements": ["共通画面への参照"], "done_when": "対象を選択する入口を示す"},
            {"id": "edit", "done_when": "対象のコードを変更する", "depends_on": ["open"]},
        ],
    )
    assert plan is not None
    result = grounded.apply_guide_contracts(
        coverage("open", "edit"),
        [entry()],
        plan.step_contracts,
        audit(review("open:0", [0]), review("open:1", [0]), review("edit:0", [], "missing")),
    )
    assert result[0]["status"] == "explained"
    assert result[1]["status"] == "unused"


@pytest.mark.parametrize("repair", [True, False])
def test_invalid_answer_proof_uses_one_bounded_audit_retry(repair):
    from dataclasses import replace

    from test_grounded_answer import FakeModel, draft, item
    from test_grounded_answer import audit as make_audit
    from test_guide_step_fulfillment import GUIDE, STEP_A, STEP_B, _ab_context, _audits, _ids, _knowledge, _run

    text = "設定 A の値を入力してください。"
    ctx = _ab_context()
    knowledge = replace(
        _knowledge(GUIDE), guide_step_contracts=(contract("s1", "入力方法"), contract("s2", "確定操作", dependencies=["s1"]))
    )
    labels = _ids(ctx, knowledge)
    base = make_audit((0, "supported", "matched", ""), (1, "supported", "matched", ""), requests=[("Q1", "addressed")])
    bad = {
        **base,
        "guide_requirement_reviews": [
            {**review("s1:0", [0]), "answer_quote": "別の操作を説明します"},
            {**review("s2:0", [1]), "answer_quote": STEP_B},
        ],
    }
    good = {
        **base,
        "guide_requirement_reviews": [
            {**review("s1:0", [0]), "answer_quote": text},
            {**review("s2:0", [1]), "answer_quote": STEP_B},
        ],
    }
    model = FakeModel([draft(item(labels[STEP_A], text, STEP_A), item(labels[STEP_B], STEP_B, STEP_B))], [bad, good if repair else bad])
    result = _run(model, ctx, knowledge).response
    audits = _audits(model)
    assert audits[1]["validation_error"]["invalid_answer_quotes"] == ["s1:0"]
    first_round = result.generation_trace["rounds"][0]
    assert first_round["audit_calls"] == 2
    first_step = next(s for s in first_round["guide_steps"] if s["step_id"] == "s1")
    assert first_step["status"] == "unknown"
    assert first_round["timing"]["semantic_audit_observation"]["adopted"] is False


def test_partly_explained_quote_does_not_retry_the_audit():
    # 一部だけの観察は引用によらず partial なので、監査を作り直さない（#2811）。
    from dataclasses import replace

    from test_grounded_answer import FakeModel, draft, item
    from test_grounded_answer import audit as make_audit
    from test_guide_step_fulfillment import GUIDE, STEP_A, STEP_B, _ab_context, _ids, _knowledge, _run

    ctx = _ab_context()
    knowledge = replace(
        _knowledge(GUIDE), guide_step_contracts=(contract("s1", "入力方法"), contract("s2", "確定操作", dependencies=["s1"]))
    )
    labels = _ids(ctx, knowledge)
    base = make_audit((0, "supported", "matched", ""), (1, "supported", "matched", ""), requests=[("Q1", "addressed")])
    partial = {
        **base,
        "guide_requirement_reviews": [
            {**review("s1:0", [0], "partial"), "answer_quote": "別の操作を説明します"},
            {**review("s2:0", [1]), "answer_quote": STEP_B},
        ],
    }
    model = FakeModel([draft(item(labels[STEP_A], STEP_A, STEP_A), item(labels[STEP_B], STEP_B, STEP_B))], [partial])
    result = _run(model, ctx, knowledge).response
    assert result.generation_trace["rounds"][0]["audit_calls"] == 1
