"""引用の表示・提取と生応答の受管診断を、同じ版の合成入力で検証する。"""

import hashlib
import json
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from typing import Any

import httpx
import pytest
from openai import OpenAI

from rag_engine.adapters import model_api
from rag_engine.config import get_settings
from rag_engine.generation import grounded
from rag_engine.generation.citation_wire import canonical_generation_draft, generation_draft_wire_model
from rag_engine.models.llm import GroundedCitation, GroundedDraft, GroundedItem

LONG = "対象を選択します。" * 30 + "\n" + "結果を保存します。" * 30


def span(index: int = 1, text: str = LONG) -> dict[str, Any]:
    return {
        "evidence_id": f"stable-{index}",
        "label": f"E{index}",
        "source_id": f"parent-{index}",
        "source": "synthetic.pdf",
        "text": text,
        "function": ("synthetic.pdf", "対象操作"),
        "tags": [],
        "source_order": (0, index, 0),
        "child_ranges": [{"id": f"child-{index}", "start": 0, "end": len(text)}],
    }


def wire_value(evidence_id: str = "E1") -> dict[str, Any]:
    return {
        "request_coverage": [
            {
                "request_id": "Q1",
                "evidence_ids": [evidence_id],
                "evidence_scope": [{"evidence_id": evidence_id, "target": "対象", "relation": "direct"}],
            }
        ],
        "items": [
            {
                "kind": "operation",
                "text": "対象の結果を確認します。",
                "request_id": "Q1",
                "citations": [{"evidence_id": evidence_id, "line_start": 2, "line_end": 2}],
            }
        ],
        "summary": "合成の回答です。",
        "confidence": "low",
    }


def run_round(spans: list[dict[str, Any]], prompt: str, parse: Any = None) -> grounded.Round:
    def fake(*args: Any, **kwargs: Any) -> Any:
        assert args[3] is generation_draft_wire_model()
        return args[3].model_validate(wire_value(spans[0]["label"]))

    return grounded.run_round(
        "対象の操作方法は？",
        spans,
        get_settings(environ={}, dotenv_path=None),
        prompt=prompt,
        image_paths=(),
        provider_id=None,
        parse_text=parse or fake,
        parse_images=lambda *_a, **_kw: pytest.fail("画像は不要"),
        audit_mode="none",
    )


def test_one_frozen_view_supplies_the_numbered_prompt_and_original_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = span()
    actual_units = grounded.citation_units
    calls = []

    def units(text: str, **kwargs: bool) -> list[tuple[int, int]]:
        calls.append(text)
        return actual_units(text, **kwargs)

    monkeypatch.setattr(grounded, "citation_units", units)
    prompt = grounded.format_functions([evidence])
    assert calls == [LONG]
    view = grounded._CITATION_VIEWS.get()[id(evidence)][1]
    assert view.rendered in prompt
    with pytest.raises(FrozenInstanceError):
        view.text = "別の版"  # type: ignore[misc]
    proposed = GroundedItem(kind="operation", text="対象の結果を確認します。", citations=[GroundedCitation(evidence_id="E1", lines="2")])
    cited, error = grounded.cited_quote(proposed, [evidence])
    begin, end = view.units[1]
    assert not error and cited.quote == view.text[begin:end].strip()
    assert cited.citations[0]._extraction["view_sha256"] == view.view_sha256
    assert cited.citations[0]._extraction["sent_view_status"] == "prepared"
    assert cited.citations[0]._extraction["prepared_view_sha256"] == view.view_sha256


def test_snapshots_and_labels_do_not_cross_siblings_or_reordered_rounds() -> None:
    first, second = span(1), span(2, LONG.replace("対象", "別対象"))
    first_prompt = grounded.format_functions([first, second])
    result = run_round([first, second], first_prompt)
    snapshot = result.citation_trace["input"]
    assert [row["source_id"] for row in snapshot["evidence"]] == ["parent-1", "parent-2"]
    reordered = [dict(second, label="E1"), dict(first, label="E2")]
    second_prompt = grounded.format_functions(reordered)
    next_result = run_round(reordered, second_prompt)
    assert next_result.checked[0].span["evidence_id"] == second["evidence_id"]
    assert next_result.checked[0].item.quote in second["text"]
    assert next_result.citation_trace["input"]["evidence"][0]["evidence_id"] == second["evidence_id"]
    assert result.citation_trace["input"] == snapshot
    assert snapshot["evidence"][0]["text"] == first["text"]


def test_unrelated_cached_view_is_not_reported_as_this_round_input() -> None:
    previous = span(9)
    grounded.format_functions([previous])
    current = span(1)
    result = run_round([current], "合成の入力。表示のスナップショットは未記録。")
    snapshot = result.citation_trace["input"]
    assert snapshot["snapshot_status"] == "not_recorded"
    assert snapshot["evidence"] == []
    assert snapshot["unrecorded_evidence_ids"] == [current["evidence_id"]]
    assert previous["text"] not in json.dumps(result.citation_trace, ensure_ascii=False)


def test_current_input_snapshot_records_actual_block_and_missing_provider_raw_explicitly() -> None:
    evidence = span()
    prompt = "入力の前置き\n" + grounded.format_functions([evidence])
    result = run_round([evidence], prompt)
    snapshot = result.citation_trace["input"]
    row = snapshot["evidence"][0]
    assert snapshot["snapshot_status"] == "recorded"
    assert snapshot["transport_capture"] == "not_recorded"
    assert row["present_in_prompt"] and row["prompt_block"] in prompt
    assert row["prompt_block_sha256"] == hashlib.sha256(row["prompt_block"].encode()).hexdigest()
    output = result.citation_trace["outputs"][0]
    assert output["raw_output_status"] == "not_recorded" and output["provider_output_text"] is None
    assert output["input_prompt_sha256"] == hashlib.sha256(prompt.encode()).hexdigest()
    assert result.citation_trace["transport_attempts_recorded"] is False
    assert result.checked[0].citation_resolution["sent_view_status"] == "matched"


def test_prepared_block_absent_from_actual_prompt_is_not_recorded_as_sent() -> None:
    evidence = span()
    grounded.format_functions([evidence])
    result = run_round([evidence], "今回の入力には証拠ブロックが含まれていません。")
    assert result.citation_trace["input"]["snapshot_status"] == "partial"
    assert not result.citation_trace["input"]["evidence"][0]["present_in_prompt"]
    proof = result.checked[0].citation_resolution
    assert proof["sent_view_status"] == "not_in_prompt"
    assert "sent_view_sha256" not in proof
    assert proof["prepared_view_sha256"] == proof["view_sha256"]
    # 原文の実在と、モデルへの送達・意味支持は別の記録。
    assert proof["authenticity"] == "verified"
    assert result.review_execution == "not_run"


@pytest.mark.parametrize("streamed", [False, True])
def test_sdk_raw_wire_is_private_and_separate_from_canonical_and_telemetry(
    streamed: bool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from test_bound_audit_transport import response_bytes

    schema = generation_draft_wire_model()
    raw = json.dumps(schema.model_validate(wire_value()).model_dump(), ensure_ascii=False, indent=2) + "\n"
    sent: list[dict[str, Any]] = []
    telemetry: list[Any] = []
    private_requests: list[Any] = []
    private_responses: list[Any] = []

    def transport(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        body, headers = response_bytes(raw, streamed)
        return httpx.Response(200, content=body, headers=headers)

    class Observation:
        def request(self, value: object, *, schema: object = None) -> None:
            private_requests.append((value, schema))

        def response(self, value: object) -> None:
            private_responses.append(value)

        def usage(self, value: Any) -> None:
            telemetry.append(value)

    @contextmanager
    def observe(*args: Any, **kwargs: Any) -> Any:
        telemetry.append((args, kwargs))
        yield Observation()

    monkeypatch.setattr(model_api, "observe_call", observe)
    settings = get_settings(
        environ={
            "ENTERPRISE_AI_DEFAULT_MODEL": "synthetic",
            "ENTERPRISE_AI_ENDPOINT": "https://mock.invalid/v1",
            "ENTERPRISE_AI_API_KEY": "mock-only",
        },
        dotenv_path=None,
        llm_stall_resends=0,
    )
    client = OpenAI(
        api_key="mock-only",
        base_url="https://mock.invalid/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    monkeypatch.setattr(model_api, "_openai_client_for_provider", lambda *_a: client)
    monkeypatch.setattr(model_api, "_stall_detection_enabled", lambda _s: streamed)

    def parse(system: str, prompt: str, _settings: Any, output_schema: Any, **_options: Any) -> Any:
        return model_api._parse_text_once(system, prompt, settings, output_schema, settings.llm_providers[settings.default_answer_llm])

    evidence = span()
    prompt = grounded.format_functions([evidence])
    try:
        result = run_round([evidence], prompt, parse)
    finally:
        client.close()
    assert len(sent) == 1
    assert private_requests == [(sent[0], sent[0]["text"]["format"])]
    assert private_responses[-1].output_text == raw
    if streamed:
        assert private_responses[:-1]
        assert all(value["status"] == "in_progress" for value in private_responses[:-1])
    else:
        assert len(private_responses) == 1
    assert sent[0]["text"]["format"]["strict"] is True
    assert sent[0]["input"][0]["content"] == result.citation_trace["input"]["system_prompt"]
    assert sent[0]["input"][1]["content"] == result.citation_trace["input"]["prompt"]
    assert "_provider_output_text" not in json.dumps(sent[0])
    output = result.citation_trace["outputs"][0]
    assert output["provider_output_text"] == raw and output["raw_output_status"] == "recorded"
    assert output["provider_capture_scope"] == "returned_response_only"
    assert output["parsed_output"]["items"][0]["citations"][0]["line_start"] == 2
    assert output["canonical_output"]["items"][0]["citations"][0]["lines"] == "2"
    assert "line_start" not in output["canonical_output"]["items"][0]["citations"][0]
    assert result.draft._provider_output_text == raw
    assert "_provider_output_text" not in result.draft.model_dump_json()
    assert "_provider_output_text" not in json.dumps(GroundedDraft.model_json_schema())
    assert raw not in json.dumps(result.timing, ensure_ascii=False)
    assert raw not in json.dumps(telemetry, ensure_ascii=False)
    assert raw not in caplog.text
    assert result.trace()["generation_citation_trace"]["outputs"][0]["provider_output_text"] == raw


def test_wire_conversion_preserves_private_raw_but_does_not_invent_missing_raw() -> None:
    value = generation_draft_wire_model().model_validate(wire_value())
    assert canonical_generation_draft(value)._provider_output_text is None
    value._provider_output_text = "{  合成の原始応答  }"
    canonical = canonical_generation_draft(value)
    assert canonical._provider_output_text == value._provider_output_text
    assert "合成の原始応答" not in canonical.model_dump_json()
