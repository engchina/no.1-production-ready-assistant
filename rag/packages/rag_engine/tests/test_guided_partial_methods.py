"""未知の現状と、資料にある一般的な方法を分離する合成の処理回帰。

監査の意味判断は人手で定めた oracle。実モデルの判定能力・伝送は検証しない。
"""

import json
from typing import Any

import pytest
from audit_fixtures import legacy_audit_to_wire_fixture

from rag_engine.config import get_settings
from rag_engine.generation import grounded
from rag_engine.generation.citation_wire import generation_draft_wire_model
from rag_engine.generation.request_recovery import request_recovery_plan
from rag_engine.models.audit_wire import decode_audit_wire, is_audit_wire
from rag_engine.models.llm import ExternalDataItem, GroundedCitation, GroundedDraft, GroundedItem, RequestScopePlan

QUESTION = "①画面Aの項目Xの現在値と変更登録方法は？②一覧の文字列Yの現在値と変更登録方法は？③対象Tの記号を非表示にできるか？"
METHOD_A = "画面Aで対象を選択し、項目Xを変更して登録します。"
METHOD_B = "文字列Yは一覧の既存行を選択し、内容を変更して登録します。"
CONDITION_C = "機能Zを備える対象に限り、記号を表示しないよう制御できます。"
UNCONDITIONAL_C = "対象Tの記号を表示しないよう制御できます。"
TEXTS = (METHOD_A, METHOD_B, CONDITION_C)
CONTRACTS = [
    {"step_id": key, "requirements": [{"id": f"{key}:0", "text": text}], "depends_on": []}
    for key, text in zip(("a", "b", "c"), TEXTS, strict=True)
]


def spans() -> list[dict[str, Any]]:
    rows = [
        {
            "evidence_id": f"stable-{key}",
            "label": f"E{i}",
            "source_id": f"source-{key}",
            "source": f"document_{key}.pdf",
            "function": (f"document_{key}.pdf", f"合成方法{key}"),
            "text": text,
        }
        for i, (key, text) in enumerate(zip(("a", "b", "c"), TEXTS, strict=True), 1)
    ]
    grounded.format_functions(rows)
    return rows


def proposal(*, omit_methods: bool, unconditional: bool) -> GroundedDraft:
    items = (
        []
        if omit_methods
        else [
            GroundedItem(kind="operation", text=text, request_id=f"Q{i}", citations=[GroundedCitation(evidence_id=f"E{i}", lines="1")])
            for i, text in enumerate(TEXTS[:2], 1)
        ]
    )
    items.append(
        GroundedItem(
            kind="rule",
            text=UNCONDITIONAL_C if unconditional else CONDITION_C,
            request_id="Q3",
            applies="matched" if unconditional else "conditional",
            condition="" if unconditional else "機能Zを備える対象に限る",
            citations=[GroundedCitation(evidence_id="E3", lines="1")],
        )
    )
    return GroundedDraft(
        items=items,
        summary="",
        confidence="low",
        external_data_required=True,
        external_data_items=[
            ExternalDataItem(text="項目Xと文字列Yの現在値および相違の原因", who_can_answer="system", already_known=False, decides=True),
            ExternalDataItem(text="対象Tが機能Zを備えるか", who_can_answer="document", already_known=False, decides=True),
        ],
    )


def run(*, omit_methods: bool = False, unconditional: bool = False, unknown_c: bool = False) -> tuple[Any, list[dict[str, Any]]]:
    evidence = spans()
    draft = proposal(omit_methods=omit_methods, unconditional=unconditional)
    audit_inputs: list[dict[str, Any]] = []
    calls: list[str] = []

    def parser(system: str, prompt: str, settings: Any, schema: Any, **kwargs: Any) -> Any:
        calls.append(schema.__name__)
        if schema is RequestScopePlan:
            return schema.model_validate(
                {
                    "request_coverage": [
                        {
                            "request_id": f"Q{index}",
                            "evidence_ids": [f"E{index}"],
                            "evidence_scope": [{"evidence_id": f"E{index}", "target": text, "relation": "direct"}],
                        }
                        for index, text in enumerate(TEXTS, 1)
                    ]
                }
            )
        if issubclass(schema, generation_draft_wire_model()):
            return draft
        assert is_audit_wire(schema)
        inputs = json.loads(prompt)
        audit_inputs.append(inputs)
        assert schema.expected_request_ids == ("Q1", "Q2", "Q3")
        requests = []
        for index, key in enumerate(("a", "b", "c")):
            missing_method = omit_methods and index < 2
            row: dict[str, Any] = {
                "request_id": f"Q{index + 1}",
                "status": "partial",
                "reason": "現状または適用条件は未確認",
                "evidence_status": "available" if missing_method else "unknown",
            }
            if missing_method:
                row.update(
                    missing_content=TEXTS[index],
                    evidence_ids=[f"stable-{key}"],
                    evidence_support=[{"evidence_id": f"stable-{key}", "source": f"document_{key}.pdf", "quote": TEXTS[index]}],
                )
            requests.append(row)
        observations = []
        for i, key in enumerate(("a", "b", "c")):
            indexes = [] if omit_methods and i < 2 else [0 if omit_methods else i]
            observations.append(
                {
                    "requirement_id": f"{key}:0",
                    "answer_state": "explained" if indexes else "not_mentioned",
                    "answer_quote": draft.items[indexes[0]].text if indexes else "",
                    "item_indexes": indexes,
                    "reason": "",
                }
            )
        reviews = [
            {
                "index": i,
                "support": ("unknown" if unknown_c else "unsupported") if unconditional and item.request_id == "Q3" else "supported",
                "applicability": "conditional" if item.request_id == "Q3" else "matched",
                "condition": "機能Zを備える対象に限る" if item.request_id == "Q3" else "",
                "reason": "合成の局所観察",
            }
            for i, item in enumerate(draft.items)
        ]
        payload = {
            "reviews": reviews,
            "request_reviews": requests,
            "guide_requirement_reviews": observations,
            "summary_supported": False,
            "goal_alignment": "partial",
            "unused_evidence_ids": ["stable-a", "stable-b"] if omit_methods else [],
        }
        return decode_audit_wire(json.dumps(legacy_audit_to_wire_fixture(payload, schema), ensure_ascii=False), schema)

    result = grounded.run_round(
        QUESTION,
        evidence,
        get_settings(environ={}, dotenv_path=None),
        prompt="合成の入力",
        image_paths=(),
        provider_id=None,
        parse_text=parser,
        parse_images=parser,
        audit_mode="detailed",
        assume_unconfirmed=True,
        guide_steps=[(key, TEXTS[i], [f"source-{key}"]) for i, key in enumerate(("a", "b", "c"))],
        guide_step_contracts=CONTRACTS,
    )
    assert len(calls) == 3 and calls[0] == "RequestScopePlan" and len(audit_inputs) == 1 and result.audit_calls == 1
    return result, audit_inputs


def test_unknown_current_values_do_not_discard_supported_general_methods() -> None:
    result, inputs = run()
    methods = {row.item.text: row for row in result.checked}
    for text in TEXTS[:2]:
        assert methods[text].published and methods[text].confirmed
    assert methods[CONDITION_C].published
    assert methods[CONDITION_C].review_assessment["applicability"] == "conditional"
    assert "機能Zを備える対象に限り" in methods[CONDITION_C].item.text
    assert methods[CONDITION_C].review_assessment["effective_support"] == "unknown"
    assert result.verified == 0
    confirmations = grounded.confirmation_lines(result.draft, QUESTION, spans())
    rendered = grounded.render(result.summary, result.checked, confirmations=confirmations)
    assert METHOD_A in rendered and METHOD_B in rendered and CONDITION_C in rendered
    assert "機能Zを備える対象に限り" in rendered
    assert "資料: 対象Tが機能Zを備えるか" in rendered
    assert "実データ: 項目Xと文字列Yの現在値および相違の原因" in rendered
    assert all(step["status"] == "unknown" for step in result.guide_steps[:2])
    raw = result.timing["semantic_audit_observation"]["audit"]
    assert all(row["answer_state"] == "explained" for row in raw["guide_requirement_reviews"][:2])
    assert not result.unused
    assert inputs[0]["guide_contracts"]
    plan = request_recovery_plan(result.unanswered, (), {"stable-a", "stable-b", "stable-c"})
    assert len(plan) == 3 and all(row["action"] == "retain" for row in plan)
    assert all(row["evidence_status"] == "unknown" for row in result.unanswered)


def test_available_omitted_methods_keep_direct_quotes_as_diagnostics_without_correction() -> None:
    result, _inputs = run(omit_methods=True)
    plan = request_recovery_plan(result.unanswered, (), {"stable-a", "stable-b", "stable-c"})
    assert [(row["request_id"], row["action"], row["evidence_ids"]) for row in plan] == [
        ("Q1", "retain", []),
        ("Q2", "retain", []),
        ("Q3", "retain", []),
    ]
    raw = result.timing["semantic_audit_observation"]["audit"]["request_reviews"]
    for row, text, key in zip(raw[:2], TEXTS[:2], ("a", "b"), strict=True):
        assert row["missing_content"] == text
        assert row["evidence_support"] == [{"evidence_id": f"stable-{key}", "source": f"document_{key}.pdf", "quote": text}]
    assert not result.feedback().get("unanswered_requests")
    assert not any(row["action"] == "retrieval" for row in plan)


@pytest.mark.parametrize("unknown_c", [False, True])
def test_unknown_target_capability_does_not_certify_unconditional_application(unknown_c: bool) -> None:
    result, _inputs = run(unconditional=True, unknown_c=unknown_c)
    methods = {row.item.text: row for row in result.checked}
    assert all(methods[text].published and methods[text].confirmed for text in TEXTS[:2])
    target = methods[UNCONDITIONAL_C]
    assert target.published and target.item.text == UNCONDITIONAL_C
    assert target.review_assessment["effective_support"] == "unknown"
    assert target.review_assessment["observed_support"] == ("unknown" if unknown_c else "unsupported")
    assert result.verified == 0
    assert result.guide_steps[2]["status"] != "explained"
