"""長い同一計画の反復だけを途中で止め、通常のJSON・無効化時を維持する。"""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_llm_runaway_output import PROVIDER, _RecordingClient, _text_script

from rag_engine.adapters import oci
from rag_engine.config import get_settings
from rag_engine.models.llm import GroundedDraft
from rag_engine.resources.deadline import Deadline, bind_deadline
from tests.test_llm_stall_resend import USAGE, _Clock


def entries(*, distinct=False):
    return [
        {"request_id": f"R{index if distinct else 1}", "evidence_scope": [{"evidence_id": "E1", "target": "合成の説明。" * 100}]}
        for index in range(64)
    ]


@pytest.mark.parametrize("size", [1, 17, 4096])
def test_long_repeated_objects_are_cut_before_the_full_plan(size):
    text = json.dumps({"request_coverage": entries()}, ensure_ascii=False)
    monitor = oci._RunawayMonitor(draft_arrays=True)
    cut = None
    for offset in range(0, len(text), size):
        if monitor.feed(text[offset : offset + size]):
            cut = offset + size
            break
    assert cut is not None and cut < len(text) // 2
    # 旧来の短い周期の検出には該当しない。
    assert oci._RunawayMonitor().feed(text) is None


def test_different_entries_other_arrays_and_json_strings_are_not_cut():
    for data in [{"request_coverage": entries(distinct=True)}, {"rows": entries()}, {"summary": json.dumps({"items": entries()})}]:
        assert oci._RunawayMonitor(draft_arrays=True).feed(json.dumps(data, ensure_ascii=False)) is None


@pytest.mark.parametrize("enabled", [True, False])
def test_streaming_retries_only_when_existing_guard_is_enabled(enabled):
    clock = _Clock()
    chunks = ['{"request_coverage":['] + [json.dumps(row, ensure_ascii=False) + "," for row in entries()]
    final = SimpleNamespace(
        type="response.completed",
        response=SimpleNamespace(status="completed", output_parsed=GroundedDraft(items=[], summary="", confidence="low"), usage=USAGE),
    )
    scripts = [_text_script(chunks, final=final)]
    if enabled:
        scripts.append(_text_script(['{"items":[]}'], final=final))
    client = _RecordingClient(clock, scripts)
    settings = replace(get_settings(environ={}, dotenv_path=None), llm_runaway_detection=enabled, llm_stall_detection=True)
    with (
        patch.object(oci.time, "monotonic", clock),
        patch.object(oci, "_openai_client_for_provider", return_value=client),
        bind_deadline(Deadline(600)),
    ):
        result = oci._openai_parsed([], settings, GroundedDraft, PROVIDER)
    assert not result.items
    assert len(client.streams) == (2 if enabled else 1)
    assert all(stream.closed for stream in client.streams)


@pytest.mark.parametrize("recovered", [True, False])
def test_repetition_resend_receives_feedback_and_preserves_original_question(recovered):
    from copy import deepcopy

    from rag_engine.generation.citation_wire import generation_draft_wire_model

    clock = _Clock()
    repeated = ['{"request_coverage":['] + [json.dumps(row, ensure_ascii=False) + "," for row in entries()]
    schema = generation_draft_wire_model()
    valid = SimpleNamespace(
        type="response.completed",
        response=SimpleNamespace(status="completed", output_parsed=schema(items=[], summary="", confidence="low"), usage=USAGE),
    )

    class RespondsToFeedback(_RecordingClient):
        def __init__(self):
            super().__init__(clock, [])
            self.inputs = []
            self.budgets = []

        def _stream(self, **kwargs):
            self.inputs.append(deepcopy(kwargs["input"]))
            self.budgets.append(kwargs["max_output_tokens"])
            has_feedback = recovered and len(kwargs["input"]) > 1
            self.scripts.append(_text_script(['{"items":[]}'] if has_feedback else repeated, final=valid))
            return super()._stream(**kwargs)

    client = RespondsToFeedback()
    messages = [{"role": "user", "content": [{"type": "input_text", "text": "合成資料の2つの値を比較してください。"}]}]
    original = deepcopy(messages)
    settings = replace(get_settings(environ={}, dotenv_path=None), llm_runaway_detection=True, llm_stall_detection=True)
    with (
        patch.object(oci.time, "monotonic", clock),
        patch.object(oci, "_openai_client_for_provider", return_value=client),
        bind_deadline(Deadline(600)),
    ):
        if recovered:
            result = oci._openai_parsed(messages, settings, schema, PROVIDER)
            assert not result.items
        else:
            with pytest.raises(oci.LlmRunawayOutput):
                oci._openai_parsed(messages, settings, schema, PROVIDER)
    assert messages == original and client.inputs[0] == original
    assert client.inputs[1][:-1] == original
    feedback = client.inputs[1][-1]["content"][0]["text"]
    assert feedback.startswith("## Step 1:") and "## Step 2:" in feedback
    assert "同じ項目を同じ配列に繰り返さず" in feedback
    assert len(client.streams) == 2
    reserve = len(json.dumps(client.inputs[1][1:], ensure_ascii=False).encode("utf-8"))
    assert client.budgets[0] - client.budgets[1] == reserve


@pytest.mark.parametrize("reason", ["runaway_repetition", "runaway_whitespace"])
def test_feedback_is_confined_to_draft_resends_and_contains_no_rejected_output(reason):
    from rag_engine.models.llm import TextOutput

    messages = [{"role": "user", "content": "元の質問"}]
    assert oci._runaway_retry_messages(messages, TextOutput, reason) is messages
    retried = oci._runaway_retry_messages(messages, GroundedDraft, reason)
    assert retried[:-1] == messages and len(messages) == 1
    assert ("空白だけの出力" if reason.endswith("whitespace") else "同じ内容の反復") in retried[-1]["content"][0]["text"]


@pytest.mark.parametrize("small_budget", [True, False])
def test_nonstreaming_draft_repetition_resend_uses_the_same_feedback(small_budget):
    from test_llm_runaway_output import _NO_STREAM_USAGE, _runaway_incomplete

    class Client:
        def __init__(self):
            self.inputs = []
            self.responses = SimpleNamespace(parse=self.parse)

        def with_options(self, **kwargs):
            return self

        def parse(self, **kwargs):
            self.inputs.append(kwargs["input"])
            if len(kwargs["input"]) == 1:
                return _runaway_incomplete('{"summary":"' + "、同じ説明" * 2000)
            return SimpleNamespace(
                status="completed", output_parsed=GroundedDraft(items=[], summary="", confidence="low"), usage=_NO_STREAM_USAGE
            )

    client = Client()
    messages = [{"role": "user", "content": "合成の質問"}]
    settings = replace(get_settings(environ={}, dotenv_path=None), llm_runaway_detection=True, llm_stall_detection=False)
    with patch.object(oci, "_openai_client_for_provider", return_value=client), oci.bind_output_cap(1 if small_budget else None):
        if small_budget:
            with pytest.raises(oci.LlmRunawayOutput):
                oci._openai_parsed(messages, settings, GroundedDraft, PROVIDER)
            assert client.inputs == [messages, messages]
        else:
            result = oci._openai_parsed(messages, settings, GroundedDraft, PROVIDER)
            assert not result.items and len(client.inputs) == 2
            assert client.inputs[1][:-1] == messages
            assert "同じ内容の反復" in client.inputs[1][-1]["content"][0]["text"]
