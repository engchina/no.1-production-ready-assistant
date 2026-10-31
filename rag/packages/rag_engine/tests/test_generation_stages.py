"""複数の小問では計画と生成を分け、原文と質問の条件を保持する。"""

import json
from typing import Any

import pytest

from rag_engine.config import get_settings
from rag_engine.generation import grounded
from rag_engine.generation.citation_wire import generation_draft_wire_model
from rag_engine.models.llm import RequestScopePlan
from rag_engine.resources.runtime import bind_prompt_overrides

QUESTION = "同じ版の資料を使ってください。①項目Aの意味は？②項目Bの意味は？"
TEXTS = ["項目Aは入力を区別する識別子です。", "項目Bは処理の状態を示します。"]


def spans() -> list[dict[str, Any]]:
    result = [
        {
            "evidence_id": f"source-{index}",
            "label": f"E{index}",
            "text": text,
            "source": "synthetic.txt",
            "source_id": "doc-a",
            "page": 1,
            "function": ("synthetic.txt", "項目の定義"),
            "origin": "document_text",
            "tags": [],
        }
        for index, text in enumerate(TEXTS, 1)
    ]
    grounded.format_functions(result)
    return result


def run(question: str, parse: Any) -> grounded.Round:
    evidence = spans()
    return grounded.run_round(
        question,
        evidence,
        get_settings(environ={}, dotenv_path=None),
        prompt="原質問: " + question + "\n" + grounded.evidence_block_text(evidence),
        image_paths=(),
        provider_id=None,
        parse_text=parse,
        parse_images=parse,
        audit_mode="none",
    )


def plan(question: str = QUESTION) -> dict[str, Any]:
    units = [unit for unit in grounded.task_contract(question)["request_units"] if unit.get("subquestion")]
    return {
        "request_coverage": [
            {
                "request_id": unit["id"],
                "evidence_ids": [f"E{index}"],
                "evidence_scope": [{"evidence_id": f"E{index}", "target": "項目", "relation": "direct"}],
            }
            for index, unit in enumerate(units, 1)
        ]
    }


def draft(requests: list[str]) -> dict[str, Any]:
    return {
        "summary": "項目の定義を示します。",
        "confidence": "high",
        "items": [
            {
                "kind": "rule",
                "request_id": request_id,
                "text": text,
                "citations": [{"evidence_id": f"E{index}", "line_start": 1, "line_end": 1}],
            }
            for index, (request_id, text) in enumerate(zip(requests, TEXTS, strict=True), 1)
        ],
    }


def test_subquestions_are_planned_before_generation_with_original_context() -> None:
    calls: list[type] = []
    planned = plan()

    def parse(system: str, prompt: str, settings: Any, schema: type, **kwargs: Any) -> Any:
        calls.append(schema)
        if schema is RequestScopePlan:
            inputs = json.loads(prompt)
            assert inputs["question"] == QUESTION
            assert [row["text"] for row in inputs["evidence"]] == TEXTS
            return RequestScopePlan.model_validate(planned)
        assert calls[0] is RequestScopePlan
        assert "request_coverage" not in schema.model_json_schema()["properties"]
        assert QUESTION in prompt and all(text in prompt for text in TEXTS)
        return schema.model_validate(draft([row["request_id"] for row in planned["request_coverage"]]))

    result = run(QUESTION, parse)
    assert len(calls) == 2 and result.audit_calls == 0
    assert result.draft.request_coverage == RequestScopePlan.model_validate(planned).request_coverage
    assert [entry.item.text for entry in result.checked if not entry.added] == TEXTS
    assert result.timing["generation_calls"] == 2


def test_a_simple_question_keeps_one_generation_call() -> None:
    calls = []

    def parse(system: str, prompt: str, settings: Any, schema: type, **kwargs: Any) -> Any:
        calls.append(schema)
        return generation_draft_wire_model().model_validate({"summary": "", "confidence": "low", "items": []})

    run("項目Aの意味は？", parse)
    assert calls == [generation_draft_wire_model()]


@pytest.mark.parametrize(
    "bad_plan",
    [
        {"request_coverage": []},
        {"request_coverage": [*plan()["request_coverage"], {"request_id": "foreign", "evidence_ids": ["E1"]}]},
        {"request_coverage": [{**row, "evidence_ids": ["unknown"]} for row in plan()["request_coverage"]]},
        {"request_coverage": [*plan()["request_coverage"], plan()["request_coverage"][0]]},
    ],
    ids=["missing", "foreign-request", "unknown-evidence", "duplicate-request"],
)
def test_an_invalid_plan_does_not_start_generation(bad_plan: dict[str, Any]) -> None:
    calls = []

    def parse(system: str, prompt: str, settings: Any, schema: type, **kwargs: Any) -> Any:
        calls.append(schema)
        assert schema is RequestScopePlan
        return RequestScopePlan.model_validate(bad_plan)

    with pytest.raises(grounded.DraftGenerationInvalid):
        run(QUESTION, parse)
    assert calls == [RequestScopePlan]


def test_each_generation_stage_uses_its_saved_prompt() -> None:
    with bind_prompt_overrides(
        {
            "generation.system": "## Step 1: 単純な質問の保存済み指示",
            "generation.plannedSystem": "## Step 1: 根拠計画後の保存済み指示",
        }
    ):
        assert grounded.generation_system_prompt(QUESTION) == "## Step 1: 根拠計画後の保存済み指示"
        assert grounded.generation_system_prompt("項目Aの意味は？") == "## Step 1: 単純な質問の保存済み指示"
