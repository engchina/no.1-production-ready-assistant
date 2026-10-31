"""推論(思考)を返すモデルの応答から、本文と構造化出力だけを取り出す回帰テスト(#1423)。

2026-10-11 に OCI Enterprise AI の `/responses` から実際に返った形(claude-opus-5 と
openai.gpt-oss-120b)を縮めて録画した。`output` の先頭に `type=reasoning` の item が付き、
その `content` に英語の思考(`reasoning_text`)が入る。
"""

import json
from typing import Any

import pytest

from app.features.nl2sql.enterprise_ai_client import _parse_response
from app.features.nl2sql.structured_outputs import SqlOutput, TextOutput, response_format

_REASONING_TEXT = (
    'The user is asking "What is the capital of Japan?" The evidence clearly states it. '
    'I could answer {"text": "wrong"} but I\'ll answer in one sentence in Japanese.'
)


def _recorded_reasoning_response(message_text: str) -> dict[str, Any]:
    return {
        "id": "resp_recorded",
        "object": "response",
        "status": "completed",
        "model": "claude-opus-5",
        "reasoning": None,
        "text": None,
        "output": [
            {
                "id": "rs_recorded",
                "summary": [],
                "type": "reasoning",
                "content": [{"text": _REASONING_TEXT, "type": "reasoning_text"}],
                "encrypted_content": None,
                "status": None,
            },
            {
                "id": "msg_recorded",
                "content": [
                    {
                        "annotations": [],
                        "text": message_text,
                        "type": "output_text",
                        "logprobs": None,
                    }
                ],
                "role": "assistant",
                "status": "completed",
                "type": "message",
            },
        ],
        "usage": {
            "input_tokens": 150,
            "output_tokens": 137,
            "output_tokens_details": {"reasoning_tokens": 128},
        },
    }


def test_plain_text_drops_reasoning_item() -> None:
    """構造化出力なしの本文に、reasoning item の思考を入れない。"""
    response = _recorded_reasoning_response("\n\n日本の首都は東京です。")

    assert _parse_response(response, response_path="", response_format=None) == (
        "日本の首都は東京です。"
    )


def test_plain_text_without_reasoning_is_unchanged() -> None:
    response = _recorded_reasoning_response("日本の首都は東京です。")
    response["output"] = response["output"][1:]

    assert _parse_response(response, response_path="", response_format=None) == (
        "日本の首都は東京です。"
    )


def test_structured_output_parses_after_reasoning_with_braces() -> None:
    """思考に `{...}` があっても、構造化出力の JSON は message の本文から検証する。"""
    expected = {"sql": "SELECT 1 FROM DUAL", "explanation": "定数を返す"}
    response = _recorded_reasoning_response(json.dumps(expected, ensure_ascii=False))

    parsed = _parse_response(response, response_path="", response_format=response_format(SqlOutput))

    assert json.loads(parsed) == expected


def _content_array_response(answer_text: str) -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "Let me think.", "text": "Hmm."},
                        {"type": "reasoning_text", "text": "Reasoning in English."},
                        {"type": "output_text", "text": answer_text},
                    ],
                }
            }
        ]
    }


def test_content_array_drops_thinking_blocks() -> None:
    """content の配列の思考の block(thinking / reasoning_text)は本文に入れない。"""
    response = _content_array_response("東京です。")

    assert _parse_response(response, response_path="", response_format=None) == "東京です。"


def test_content_array_structured_output_drops_thinking_blocks() -> None:
    response = _content_array_response('{"text": "東京です。"}')

    structured = _parse_response(
        response, response_path="", response_format=response_format(TextOutput)
    )
    assert json.loads(structured) == {"text": "東京です。"}


@pytest.mark.parametrize(
    "message",
    [
        {"role": "assistant", "content": "東京です。", "reasoning_content": "Thinking..."},
        {"role": "assistant", "content": "<think>Thinking...</think>\n\n東京です。"},
        {"role": "assistant", "content": "<thinking>\nThinking...\n</thinking>東京です。"},
    ],
    ids=["reasoning-content-field", "think-tag", "thinking-tag"],
)
def test_chat_completions_reasoning_is_dropped(message: dict[str, Any]) -> None:
    """Chat Completions の `reasoning_content` と本文の先頭の思考のタグを除く。"""
    response = {"choices": [{"message": message}]}

    assert _parse_response(response, response_path="", response_format=None) == "東京です。"


def test_structured_output_strips_leading_think_tag() -> None:
    response = {"output_text": '<think>{"text": "wrong"}</think>{"text": "東京です。"}'}

    structured = _parse_response(
        response, response_path="", response_format=response_format(TextOutput)
    )

    assert json.loads(structured) == {"text": "東京です。"}
