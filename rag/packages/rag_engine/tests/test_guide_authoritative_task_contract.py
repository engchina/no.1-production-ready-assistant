"""Guideが決めた本問の要求を主契約へ編成し、原質問と資料支持を混同しない。"""

import json
from copy import deepcopy
from typing import Any

from audit_fixtures import legacy_audit_to_wire_fixture

from rag_engine.config import get_settings
from rag_engine.generation import grounded
from rag_engine.generation.citation_wire import generation_draft_wire_model
from rag_engine.generation.guide_authority import compile_guide_task_contract
from rag_engine.models.audit_wire import decode_audit_wire, is_audit_wire
from rag_engine.models.llm import GroundedCitation, GroundedDraft, GroundedItem

ORIGINAL = "設定を変更したい。"
OPEN = "設定画面を開きます。"
FINISH = "変更後に内容を確認して確定する"
ALTERNATIVE = "別機能の追加操作を行う"
SCOPE: list[dict[str, Any]] = [
    {
        "guide_id": "synthetic-guide",
        "revision": 2,
        "step_id": "open",
        "title": "設定画面を開く",
        "required": True,
        "depends_on": [],
        "requirements": [{"id": "open:0", "text": OPEN, "kind": "completion"}],
    },
    {
        "guide_id": "synthetic-guide",
        "revision": 2,
        "step_id": "finish",
        "title": "変更を確定する",
        "required": True,
        "depends_on": ["open"],
        "requirements": [{"id": "finish:0", "text": FINISH, "kind": "completion"}],
        "done_when": "確定方法を説明した",
    },
    {
        "guide_id": "synthetic-guide",
        "revision": 2,
        "step_id": "alternative",
        "title": ALTERNATIVE,
        "required": False,
        "depends_on": [],
        "requirements": [{"id": "alternative:0", "text": ALTERNATIVE, "kind": "completion"}],
    },
]


def original() -> dict[str, Any]:
    return {"current_request": ORIGINAL, "request_units": [{"id": "Q1", "text": ORIGINAL, "kind": "request"}]}


def test_no_guide_scope_leaves_original_contract_unchanged_and_independent() -> None:
    contract = original()
    result = compile_guide_task_contract(contract, ())
    assert result == contract and result is not contract
    result["request_units"][0]["text"] = "変更した複製"
    assert contract["request_units"][0]["text"] == ORIGINAL


def test_single_request_compiles_required_goals_dependencies_and_requirements_without_changing_id() -> None:
    contract = original()
    contract["request_units"].insert(0, {"id": "Q0", "text": "背景の記述", "kind": "context"})
    before = deepcopy((contract, SCOPE))
    result = compile_guide_task_contract(contract, SCOPE)
    assert result["original_question"] == result["original_current_request"] == ORIGINAL
    assert result["current_request"].startswith(ORIGINAL + "\n")
    assert FINISH in result["current_request"] and '"depends_on": ["open"]' in result["current_request"]
    assert ALTERNATIVE not in result["current_request"]
    assert [row["step_id"] for row in result["required_guide_scope"]] == ["open", "finish"]
    assert [row["step_id"] for row in result["unresolved_guide_scope"]] == ["alternative"]
    assert result["request_units"][0] == contract["request_units"][0]
    unit = result["request_units"][1]
    assert unit["id"] == "Q1" and unit["text"] == result["current_request"] and unit["original_text"] == ORIGINAL
    assert unit["scope_sources"] == ["original_question", "business_guide"]
    assert before == (contract, SCOPE)


def test_multiple_requests_keep_each_owner_and_text_without_guessing_source_association() -> None:
    contract = {
        "current_request": "①設定を変更したい。②履歴を確認したい。",
        "request_units": [
            {"id": "Q1", "text": "設定を変更したい。", "kind": "request"},
            {"id": "Q2", "text": "履歴を確認したい。", "kind": "request"},
        ],
    }
    result = compile_guide_task_contract(contract, SCOPE)
    assert FINISH in result["current_request"]
    assert result["request_units"] == contract["request_units"]
    assert all("scope_sources" not in row and "original_text" not in row for row in result["request_units"])
    assert result["original_question"] == contract["current_request"]


def test_unresolved_branch_does_not_rewrite_the_primary_task_or_become_required() -> None:
    contract = original()
    result = compile_guide_task_contract(contract, [SCOPE[2]])
    assert result["current_request"] == ORIGINAL and result["request_units"] == contract["request_units"]
    assert result["required_guide_scope"] == []
    assert result["unresolved_guide_scope"][0]["step_id"] == "alternative"


def test_compilation_never_turns_guide_source_ids_or_support_claims_into_fact_evidence() -> None:
    scope = deepcopy(SCOPE)
    scope[1].update(
        source_ids=["not-retrieved"],
        evidence_ids=["invented-evidence"],
        support="supported",
        facts="未取得の事実を捏造しない",
        owner_request_id="Q2",
    )
    result = compile_guide_task_contract(original(), scope)
    wire = json.dumps(result, ensure_ascii=False)
    for untrusted in ("not-retrieved", "invented-evidence", "未取得の事実を捏造しない", "owner_request_id"):
        assert untrusted not in wire
    assert all(not {"source_ids", "evidence_ids", "support", "facts"} & row.keys() for row in result["required_guide_scope"])


def test_existing_original_question_metadata_remains_traceable() -> None:
    contract = original()
    contract["original_question"] = "会話で最初に入力された質問"
    result = compile_guide_task_contract(contract, SCOPE)
    assert result["original_question"] == contract["original_question"]
    assert result["original_current_request"] == ORIGINAL


def test_actual_round_audit_primary_question_uses_compiled_task_without_more_calls_or_invented_material() -> None:
    span = {
        "evidence_id": "stable-a",
        "label": "E1",
        "source_id": "child-a",
        "source": "synthetic.pdf",
        "page": 1,
        "text": OPEN,
        "function": ("synthetic.pdf", "合成設定"),
        "tags": [],
    }
    grounded.format_functions([span])
    calls: list[str] = []
    inputs: list[dict[str, Any]] = []

    def parse(system: str, prompt: str, settings: Any, schema: Any, **kwargs: Any) -> Any:
        calls.append(schema.__name__)
        if schema is generation_draft_wire_model():
            return GroundedDraft(
                items=[
                    GroundedItem(kind="operation", request_id="Q1", text=OPEN, citations=[GroundedCitation(evidence_id="E1", quote=OPEN)])
                ],
                summary="",
                confidence="low",
            )
        assert is_audit_wire(schema)
        inputs.append(json.loads(prompt))
        assert schema.expected_request_ids == ("Q1",)
        payload = {
            "reviews": [
                {"index": 0, "support": "supported", "applicability": "matched", "source_applicability": "matched", "reason": "原文支持"}
            ],
            "request_reviews": [{"request_id": "Q1", "status": "partial", "evidence_status": "missing", "reason": "確定の資料が未取得"}],
            "guide_requirement_reviews": [
                {"requirement_id": "open:0", "answer_state": "explained", "answer_quote": OPEN, "item_indexes": [0], "reason": ""},
                {"requirement_id": "finish:0", "answer_state": "not_mentioned", "item_indexes": [], "reason": "未説明"},
                {"requirement_id": "alternative:0", "answer_state": "not_mentioned", "item_indexes": [], "reason": "分岐未確定"},
            ],
            "summary_supported": False,
            "goal_alignment": "partial",
        }
        return decode_audit_wire(json.dumps(legacy_audit_to_wire_fixture(payload, schema), ensure_ascii=False), schema)

    result = grounded.run_round(
        ORIGINAL,
        [span],
        get_settings(environ={}, dotenv_path=None),
        prompt="合成入力",
        image_paths=(),
        provider_id=None,
        parse_text=parse,
        parse_images=parse,
        audit_mode="standard",
        guide_task_scope=SCOPE,
        guide_steps=[("open", "画面を開く", ["child-a"])],
    )
    assert len(calls) == 2 and len(inputs) == 1 and result.audit_calls == 1
    actual = inputs[0]
    assert actual["original_question"] == ORIGINAL
    assert actual["question"] == actual["task_contract"]["current_request"]
    assert actual["question"] != ORIGINAL and FINISH in actual["question"]
    assert ALTERNATIVE not in actual["question"]
    unit = actual["task_contract"]["request_units"][0]
    assert unit["id"] == "Q1" and unit["text"] == actual["question"] and unit["original_text"] == ORIGINAL
    assert actual["evidence"] == {"stable-a": OPEN}
    assert actual["items"][0]["citations"] == [{"evidence_id": "stable-a", "quote": OPEN}]
    assert result.audit is not None and result.audit.request_reviews[0].evidence_status == "unknown"
    raw = result.timing["semantic_audit_observation"]["audit"]
    assert raw["request_reviews"][0]["evidence_status"] == "missing"
    assert raw["reviews"][0]["support"] == "supported" and raw["reviews"][0]["source_applicability"] == "matched"
    assert result.timing["semantic_audit_observation"]["adopted"] is False
    finish = next(row for row in result.guide_steps if row["step_id"] == "finish")
    assert finish["status"] != "explained"
