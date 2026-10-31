"""モデルの接続テストの応答から、思考を除いた本文だけを返す回帰テスト(#1423)。

2026-10-11 に OCI Enterprise AI の `/responses` から実際に返った形(claude-opus-5 と
openai.gpt-oss-120b)を縮めて録画した。`output` の先頭に `type=reasoning` の item が付き、
その `content` に英語の思考(`reasoning_text`)が入る。
"""

from typing import Any

import pytest

from app.features.agent.router import _parse_enterprise_text_response

_REASONING_TEXT = "The user is asking about the capital of Japan. I'll answer in Japanese."


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
        "usage": {"output_tokens": 137, "output_tokens_details": {"reasoning_tokens": 128}},
    }


def test_reasoning_item_is_not_returned_as_answer() -> None:
    response = _recorded_reasoning_response("日本の首都は東京です。")

    assert _parse_enterprise_text_response(response, "") == "日本の首都は東京です。"


def test_response_without_reasoning_is_unchanged() -> None:
    response = _recorded_reasoning_response("日本の首都は東京です。")
    response["output"] = response["output"][1:]

    assert _parse_enterprise_text_response(response, "") == "日本の首都は東京です。"


def test_reasoning_only_response_is_an_error() -> None:
    """本文が空で思考だけの応答は、思考を回答にせず失敗にする。"""
    response = _recorded_reasoning_response("")

    with pytest.raises(ValueError, match="回答 text"):
        _parse_enterprise_text_response(response, "")


@pytest.mark.parametrize(
    "message",
    [
        {"role": "assistant", "content": "東京です。", "reasoning_content": "Thinking..."},
        {"role": "assistant", "content": "<think>Thinking...</think>\n\n東京です。"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "Let me think.", "text": "Hmm."},
                {"type": "text", "text": "東京です。"},
            ],
        },
    ],
    ids=["reasoning-content-field", "think-tag", "content-array"],
)
def test_chat_completions_reasoning_is_dropped(message: dict[str, Any]) -> None:
    response = {"choices": [{"message": message}]}

    assert _parse_enterprise_text_response(response, "").strip() == "東京です。"
